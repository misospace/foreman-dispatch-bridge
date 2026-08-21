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

    def claim_one(self, agent_name, lane):
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
