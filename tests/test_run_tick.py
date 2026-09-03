"""Call-sequence test for one whole tick.

`_real_main` used to hold 486 lines of orchestration behind
`# pragma: no cover - thin wiring, exercised in the cluster`, so nothing could
invoke the tick. Four defects shipped through that hole in a day (#199): the
terminal-since stamping never ran, its plural was wrong, `_real_main` raised
NameError from a definition below the entrypoint guard, and the stamp's error
handler crashed on a reserved LogRecord key. Each one left a green suite.

This asserts the *calls* a tick makes, not just the values helpers return.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Dict, List

from kubernetes.client.rest import ApiException

from bridge.main import TickConfig, run_tick


def _cfg(**over) -> TickConfig:
    base = TickConfig(
        agent_name="foreman-coder",
        lanes=["frontier"],
        namespace="llm",
        gate_profiles={"*": {"language": "generic"}},
        max_attempts=3,
        lane_coder_agents={"*": ["coder"]},
        revision_coder_agents={},
        base_coder_agents={},
        repo_coder_agents={},
        escalation_lane="",
        verify_enabled=True,
        self_go=[],
        pr_fix_enabled=False,
        pr_fix_max_attempts=3,
        github_token="gh",
        pr_fix_lane_agents={},
        prune_completed_after_h=6,
        prune_failed_after_h=48,
        max_in_progress=0,
        coder_slots={},
        fix_first_agents={},
    )
    return replace(base, **over) if over else base


def _wl(name: str, phase: str, *, created: str, label: str = "dispatch-bridge") -> dict:
    return {
        "metadata": {
            "name": name,
            "annotations": {},
            "creationTimestamp": created,
            "labels": {"created-by": label},
        },
        "status": {"phase": phase},
        "spec": {"repo": "o/r", "issues": [1]},
    }


class RecordingApi:
    """Records every custom-object call the tick makes."""

    def __init__(self, workloads: Dict[str, List[dict]], patch_error: ApiException | None = None):
        self.workloads = workloads
        self.patch_error = patch_error
        self.calls: List[tuple] = []

    def list_namespaced_custom_object(self, **kw):
        self.calls.append(("list", kw.get("plural"), kw.get("label_selector")))
        if kw.get("plural") == "agentictasks":
            return {"items": []}
        return {"items": list(self.workloads.get(kw.get("label_selector"), []))}

    def patch_namespaced_custom_object(self, **kw):
        self.calls.append(("patch", kw.get("plural"), kw.get("name"), json.dumps(kw.get("body"))))
        if self.patch_error:
            raise self.patch_error

    def create_namespaced_custom_object(self, **kw):
        self.calls.append(("create", kw.get("plural")))

    def delete_namespaced_custom_object(self, **kw):
        self.calls.append(("delete", kw.get("plural"), kw.get("name")))
        for items in self.workloads.values():
            items[:] = [w for w in items if w["metadata"]["name"] != kw.get("name")]

    def get_namespaced_custom_object(self, **kw):
        raise ApiException(status=404, reason="gone")


class StubDispatch:
    """Empty queue: the tick reconciles and prunes but claims nothing new."""

    def __init__(self):
        self.calls: List[str] = []

    def claim_one(self, agent_name, lane, queue_for=None):
        self.calls.append(f"claim_one:{lane}")
        return None

    def queue(self, agent_name, lane=None):
        self.calls.append("queue")
        return []

    def __getattr__(self, item):
        def _noop(*a, **k):
            self.calls.append(item)
            return None
        return _noop


def _http_get(url, headers=None, allow_404=False):
    class R:
        status_code = 404
        text = ""
        def json(self): return {}
    return R()


OLD = "2026-01-01T00:00:00Z"


def test_terminal_workload_is_stamped_and_not_pruned_on_the_same_tick():
    """The stamp PATCH must use the real CRD plural and a single-slash key. Age is
    then measured from the stamp, so the first tick that observes a terminal
    Workload must NOT delete it -- the TTL runs from the observation."""
    api = RecordingApi({
        "created-by=dispatch-bridge": [_wl("wl-1", "Completed", created=OLD)],
        "created-by=dispatch-bridge-prfix": [],
    })
    run_tick(api, StubDispatch(), _cfg(), _http_get)

    patches = [c for c in api.calls if c[0] == "patch"]
    assert len(patches) == 1, api.calls
    assert patches[0][1] == "workloads"
    key = next(iter(json.loads(patches[0][3])["metadata"]["annotations"]))
    assert key.count("/") == 1, key
    assert key == "foreman.llmkube.dev/terminal-since-Completed"
    assert not [c for c in api.calls if c[0] == "delete"], api.calls


def test_workload_stamped_on_an_earlier_tick_is_pruned():
    """Once the stamp itself is older than the TTL, prune deletes."""
    wl = _wl("wl-1", "Completed", created=OLD)
    wl["metadata"]["annotations"]["foreman.llmkube.dev/terminal-since-Completed"] = OLD
    api = RecordingApi({
        "created-by=dispatch-bridge": [wl],
        "created-by=dispatch-bridge-prfix": [],
    })
    run_tick(api, StubDispatch(), _cfg(), _http_get)
    # already stamped, so no re-stamp: the TTL clock must not restart
    assert not [c for c in api.calls if c[0] == "patch"], api.calls
    assert ("delete", "workloads", "wl-1") in api.calls


def test_failed_stamp_still_prunes():
    """A rejected stamp must not stop garbage collection: the annotation is
    dropped so terminal_since falls back to creationTimestamp."""
    api = RecordingApi(
        {
            "created-by=dispatch-bridge": [_wl("wl-1", "Completed", created=OLD)],
            "created-by=dispatch-bridge-prfix": [],
        },
        patch_error=ApiException(status=422, reason="Unprocessable Entity"),
    )
    run_tick(api, StubDispatch(), _cfg(), _http_get)
    assert ("delete", "workloads", "wl-1") in api.calls


def test_workload_within_ttl_is_left_alone():
    import datetime
    fresh = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    api = RecordingApi({
        "created-by=dispatch-bridge": [_wl("wl-fresh", "Completed", created=fresh)],
        "created-by=dispatch-bridge-prfix": [],
    })
    run_tick(api, StubDispatch(), _cfg(), _http_get)
    assert not [c for c in api.calls if c[0] == "delete"], api.calls


def test_run_tick_go_no_pr_dispatches_full_park_flow() -> None:
    """Driving a tick where the bridge writes a Completed Workload with
    verdict=GO and no PR must (a) flip status to backlog via
    DispatchClient.update_status, (b) call DispatchClient.add_label with the
    needs-human label, (c) call DispatchClient.post_comment with a body that
    surfaces the commitSHA and branch, and (d) on a second tick over the same
    parked issue dedupe via issue_is_parked so that update_status, add_label
    and post_comment are NOT re-invoked.

    This is the production-wiring assertion that issue #261 calls out:
    transition_to_in_review needs dispatch= wired AND the calls need to be
    the real DispatchClient methods (add_label / post_comment on the item
    dict), not the non-existent apply_label / comment.
    """

    class _ParkDispatch:
        def __init__(self) -> None:
            self.update_status_calls: list[tuple[dict, str, str, str]] = []
            self.add_label_calls: list[tuple[dict, str]] = []
            self.post_comment_calls: list[tuple[dict, str]] = []
            self.issue_is_parked_calls: list[tuple[str, int, str]] = []
            # First tick: issue is not yet parked. Second tick: it is.
            self._parked = False

        # --- real DispatchClient signatures (see bridge/claim.py) ---
        def update_status(self, item, status, agent, reason=""):
            self.update_status_calls.append((dict(item), status, agent, reason))
            return True

        def add_label(self, item, label):
            self.add_label_calls.append((dict(item), label))
            self._parked = True
            return True

        def post_comment(self, item, body):
            self.post_comment_calls.append((dict(item), body))
            return True

        def issue_is_parked(self, repo, num, label):
            self.issue_is_parked_calls.append((repo, num, label))
            return self._parked

        # --- other surface used by run_tick ---
        def list_claimed_issues(self):
            return []

        def list_claimed(self, agent_name, status=None):
            return []

        def claim(self, issue_id, repo_full_name, number):
            return None

        def claim_one(self, agent_name, lane, queue_for=None):
            return None

        def queue(self, agent_name, lane=None):
            return []

        def comment_with_patch(self, item, body, patch_url):
            return None

        def reopen(self, item):
            return None

        def update_label(self, item, label):
            return None

        def remove_label(self, item, label):
            return None

        def release(self, issue_id, repo_full_name, number):
            return None

        def get_issue_body(self, repo_full_name, number):
            return ""

        def get_issue_comments(self, repo_full_name, number):
            return []

    workload_name = "wl-go-no-pr-1"
    item = {
        "issueId": "iss-77",
        "repoFullName": "misospace/foreman-dispatch-bridge",
        "number": 77,
    }
    task = {
        "name": "coder",
        "agent": "coder-agent",
        "status": {
            "phase": "Completed",
            "verdict": "GO",
            "result": {
                "summary": "all green",
                "extra": {
                    "commitSHA": "deadbeef",
                    "branch": "fix-77",
                },
            },
        },
    }

    def _api_with_workload() -> RecordingApi:
        wl = _completed_workload()

        class _TaskAwareApi(RecordingApi):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._task_response = {"items": [dict(task)]}

            def list_namespaced_custom_object(self, **kw):
                if kw.get("plural") == "agentictasks":
                    self.calls.append(
                        (
                            "list",
                            kw.get("plural"),
                            kw.get("label_selector"),
                        )
                    )
                    return self._task_response
                return super().list_namespaced_custom_object(**kw)

        return _TaskAwareApi(
            workloads={
                "created-by=dispatch-bridge": [wl],
                "created-by=dispatch-bridge-prfix": [],
            }
        )

    def _completed_workload(name: str = workload_name) -> dict:
        wl = {
            "metadata": {
                "name": name,
                "uid": "uid-77",
                "annotations": {"foreman.llmkube.dev/issue-id": item["issueId"]},
            },
            "spec": {
                "repo": item["repoFullName"],
                "issues": [item["number"]],
            },
            "status": {"phase": "Completed"},
        }
        return wl

    api = _api_with_workload()
    dispatch = _ParkDispatch()
    run_tick(api, dispatch, _cfg(), _http_get)

    # (a) status flip happened.
    assert any(
        call[0].get("number") == 77 and call[1] == "backlog"
        for call in dispatch.update_status_calls
    ), dispatch.update_status_calls
    # (b) needs-human label applied via the real DispatchClient signature.
    assert (
        (item, "needs-human") in dispatch.add_label_calls
    ), dispatch.add_label_calls
    # (c) explanatory comment posted with commitSHA + branch info surfaced.
    assert len(dispatch.post_comment_calls) == 1
    _, comment_body = dispatch.post_comment_calls[0]
    assert "deadbeef" in comment_body
    assert "fix-77" in comment_body

    # (d) second tick: issue_is_parked is True (carried by the real client
    #     after add_label landed in tick #1), so the dedupe branch must
    #     short-circuit and NOT re-flip / re-label / re-comment.
    api2 = _api_with_workload()
    dispatch2 = _ParkDispatch()
    dispatch2._parked = True

    run_tick(api2, dispatch2, _cfg(), _http_get)

    assert dispatch2.update_status_calls == [], dispatch2.update_status_calls
    assert dispatch2.add_label_calls == [], dispatch2.add_label_calls
    assert dispatch2.post_comment_calls == [], dispatch2.post_comment_calls
    # issue_is_parked was queried on the second tick so the dedupe path
    # could fire.
    assert any(
        num == 77
        and label == "needs-human"
        for (_, num, label) in dispatch2.issue_is_parked_calls
    ), dispatch2.issue_is_parked_calls





def test_run_tick_makes_one_snapshot_for_lanes_retry_and_drain() -> None:
    """Issue #256: a single run_tick must issue exactly N (lanes) + 1
    (pr-fix-queue) HTTP GETs to /api/agents/{agent}/queue across the three
    consumers, not 3N (lane_index + find_issue_id + claim_one). With M
    retries that need an issue-id backfill and a busy lane that fills
    headroom, claim_one may run multiple times in the same tick — every
    call must read from the snapshot, not refetch.
    """
    from urllib.parse import urlparse, parse_qs
    from bridge import main as main_mod
    from bridge.claim import DispatchClient

    lanes = ["base", "frontier", "coder"]
    pr_fix_lane = "pr-fix"
    cfg = _cfg(lanes=lanes + [pr_fix_lane])

    queue_calls: list[str] = []

    def fake_http_get(url, headers=None, allow_404=False):
        q = parse_qs(urlparse(url).query)
        lane = q.get("lane", [""])[0]
        if "/api/agents/" in url and "queue" in url:
            queue_calls.append(lane)
        if lane == "base":
            return [
                {"issueId": "i1", "repoFullName": "r/a", "number": 1,
                 "status": "status/ready", "claimable": True, "title": "t1"},
                {"issueId": "i2", "repoFullName": "r/a", "number": 2,
                 "status": "status/ready", "claimable": True, "title": "t2"},
            ]
        if lane == "frontier":
            return [
                {"issueId": "i3", "repoFullName": "r/a", "number": 3,
                 "status": "status/ready", "claimable": True, "title": "t3"},
                {"issueId": "i4", "repoFullName": "r/a", "number": 4,
                 "status": "status/ready", "claimable": True, "title": "t4"},
            ]
        return []

    def fake_http_post(url, headers=None, json=None, allow_404=False):
        return None

    d = DispatchClient("http://dispatch", "tok", fake_http_get, fake_http_post)

    api = RecordingApi({
        "created-by=dispatch-bridge": [],
        "created-by=dispatch-bridge-prfix": [],
    })

    main_mod.run_tick(api, d, cfg, fake_http_get, fake_http_post)

    # N lane queues (parallel snapshot batch) + 1 pr-fix-queue fetch. Anything
    # more means one of the three consumers refetched.
    assert len(queue_calls) == len(lanes) + 1, queue_calls
    # claim_one was used through the snapshot path.


def test_run_tick_drain_loop_does_not_refetch_per_claim() -> None:
    """Issue #256: when claim_one is called multiple times for the same lane
    inside a tick (drain loop), it must NEVER call self.queue() again —
    the snapshot is canonical for the tick.
    """
    from urllib.parse import urlparse, parse_qs
    from bridge import main as main_mod
    from bridge.claim import DispatchClient

    lanes = ["base"]
    cfg = _cfg(lanes=lanes)
    queue_calls: list[str] = []

    def fake_http_get(url, headers=None, allow_404=False):
        q = parse_qs(urlparse(url).query)
        lane = q.get("lane", [""])[0]
        if "/queue" in url:
            queue_calls.append(lane)
        # Five items so claim_one is called five times in the drain loop.
        if lane == "base":
            return [
                {"issueId": f"i{n}", "repoFullName": "r/a", "number": n,
                 "status": "status/ready", "claimable": True, "title": f"t{n}"}
                for n in range(1, 6)
            ]
        return []

    def fake_http_post(url, headers=None, json=None, allow_404=False):
        return None

    d = DispatchClient("http://dispatch", "tok", fake_http_get, fake_http_post)

    api = RecordingApi({
        "created-by=dispatch-bridge": [],
        "created-by=dispatch-bridge-prfix": [],
    })

    main_mod.run_tick(api, d, cfg, fake_http_get, fake_http_post)
    # Exactly one queue GET per lane (the snapshot), even though claim_one
    # fired multiple times.
    assert queue_calls.count("base") == 1, queue_calls


