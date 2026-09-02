import json
from pathlib import Path

import pytest
from bridge.models import ClaimedItem
from bridge.claim import select_item, select_candidates, to_claimed_item, DispatchClient

SAMPLE = json.loads(Path("tests/fixtures/dispatch_claim_sample.json").read_text())


def test_select_item_picks_first_ready_claimable_non_renovate():
    item = select_item(SAMPLE, "local")
    # #42 is ready+claimable; #7 is renovate; #99 is backlog.
    assert item["number"] == 42


def test_select_item_skips_renovate_and_non_ready():
    only_bad = [i for i in SAMPLE if i["number"] in (7, 99)]
    assert select_item(only_bad, "local") is None


def test_select_item_respects_lane():
    assert select_item(SAMPLE, "frontier") is None


# ── renovate filter scope (issue #216) ────────────────────────────────────────
# The old filter was a bare "renovate" substring on the title, which dropped
# every issue *about* Renovate. The guard now mirrors dispatch's isRenovateIssue:
# dashboard substrings, update prefixes, and bot labels only.


def _ready_local(number, title, labels=None):
    return {
        "number": number, "repoFullName": "o/r", "issueId": f"i{number}",
        "title": title, "lane": "local",
        "labels": labels if labels is not None else ["status/ready"],
        "claimable": True,
    }


def test_title_mentioning_renovate_mid_sentence_is_claimable():
    # The two real issues that were starved on 2026-08-23: both merely mention
    # Renovate in the title and must be yielded.
    queue = [
        _ready_local(55, "[P3] No requirements-dev.txt pin management — dev-only deps not managed by Renovate"),
        _ready_local(199, "[P3] Renovate config still groups coder apps that were consolidated into apps/llmkube-coder"),
    ]
    assert [c["number"] for c in select_candidates(queue, "local")] == [55, 199]


def test_renovate_dashboard_titles_are_not_claimable():
    queue = [
        _ready_local(1, "Renovate Dashboard"),
        _ready_local(2, "Dependency Dashboard for October"),
    ]
    assert select_item(queue, "local") is None


def test_update_dependency_title_is_not_claimable():
    queue = [_ready_local(1, "Update dependency pytest from 8.0.0 to 8.1.0")]
    assert select_item(queue, "local") is None


def test_update_image_title_is_not_claimable():
    queue = [_ready_local(1, "update image python from 3.12 to 3.13")]
    assert select_item(queue, "local") is None


def test_renovate_label_is_not_claimable():
    queue = [_ready_local(1, "Tune the dependency update schedule",
                          labels=["status/ready", "renovate"])]
    assert select_item(queue, "local") is None


def test_skipped_candidate_is_logged_with_number_and_reason(caplog):
    import logging
    queue = [_ready_local(7, "Update dependency pytest from 8.0.0 to 8.1.0")]
    with caplog.at_level(logging.INFO, logger="bridge.claim"):
        assert select_item(queue, "local") is None
    records = [r for r in caplog.records if r.message == "candidate-skipped"]
    assert len(records) == 1
    assert records[0].number == 7
    assert records[0].reason == "renovate-title-prefix:update dep"
    assert records[0].lane == "local"
    # The bot filter is the #216 failure mode: it must be visible at INFO.
    assert records[0].levelno == logging.INFO


def test_mechanical_skips_log_at_debug_not_info(caplog):
    """lane-mismatch / not-ready / not-claimable skips are expected noise
    (dispatch filters server-side); they must not spam INFO on a queue that
    select_candidates didn't fetch per-lane."""
    import logging
    queue = [
        _ready_local(1, "other lane"),  # ready+claimable but wrong lane (set below)
        _ready_local(2, "backlog item", labels=["status/backlog"]),
        _ready_local(3, "not claimable", ),
    ]
    queue[0]["lane"] = "frontier"
    queue[2]["claimable"] = False
    with caplog.at_level(logging.DEBUG, logger="bridge.claim"):
        assert select_item(queue, "local") is None
    records = [r for r in caplog.records if r.message == "candidate-skipped"]
    assert [(r.number, r.reason, r.levelno) for r in records] == [
        (1, "lane-mismatch:frontier", logging.DEBUG),
        (2, "not-ready:status/backlog", logging.DEBUG),
        (3, "not-claimable", logging.DEBUG),
    ]
    # At INFO (the default operational level) the mechanical skips are silent.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bridge.claim"):
        select_item(queue, "local")
    assert [r for r in caplog.records if r.message == "candidate-skipped"] == []


def test_to_claimed_item_maps_dispatch_fields():
    item = to_claimed_item(SAMPLE[0], "local")
    assert item == ClaimedItem(
        repo="joryirving/home-ops", issue_number=42,
        intent="Fix the flaky reconcile test", lane="local",
        issue_id="iss_abc123",
    )


def test_claim_one_queue_then_claim():
    captured = {}

    def fake_get(url, headers):
        captured["get_url"] = url
        return SAMPLE

    def fake_post(url, headers, payload):
        captured["claim_payload"] = payload
        return {"ok": True}

    client = DispatchClient("http://d/", "tok", http_get=fake_get, http_post=fake_post)
    item = client.claim_one("foreman/coder", "local")
    assert item == ClaimedItem(
        repo="joryirving/home-ops", issue_number=42,
        intent="Fix the flaky reconcile test", lane="local",
        issue_id="iss_abc123",
    )
    assert captured["get_url"] == "http://d/api/agents/foreman/coder/queue?lane=local&includeClaimed=true"
    assert captured["claim_payload"] == {
        "issueId": "iss_abc123", "repoFullName": "joryirving/home-ops",
        "issueNumber": 42, "agentName": "foreman/coder",
    }


def test_claim_one_returns_none_on_409_conflict():
    client = DispatchClient("http://d", "tok",
                            http_get=lambda u, h: SAMPLE,
                            http_post=lambda u, h, p: None)  # None == 409 already claimed
    assert client.claim_one("foreman/coder", "local") is None


def test_claim_one_empty_queue():
    client = DispatchClient("http://d", "tok",
                            http_get=lambda u, h: [],
                            http_post=lambda u, h, p: {"ok": True})
    assert client.claim_one("foreman/coder", "local") is None


def test_select_candidates_yields_all_ready_claimable_in_order():
    # SAMPLE: #42 ready+claimable; #7 renovate; #99 backlog → only #42 qualifies.
    assert [c["number"] for c in select_candidates(SAMPLE, "local")] == [42]


def test_claim_one_advances_past_failed_claim():
    # Two claimable, ready, local items. The head (#1) 409s; claim_one must skip
    # it and claim the next (#2) instead of starving the lane.
    queue = [
        {"number": 1, "repoFullName": "a/b", "issueId": "i1", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "head"},
        {"number": 2, "repoFullName": "a/b", "issueId": "i2", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "next"},
    ]
    posted = []

    def fake_post(url, headers, payload):
        posted.append(payload["issueNumber"])
        return None if payload["issueNumber"] == 1 else {"ok": True}

    client = DispatchClient("http://d", "tok",
                            http_get=lambda u, h: queue, http_post=fake_post)
    item = client.claim_one("foreman-coder", "local")
    assert item is not None and item.issue_number == 2
    assert posted == [1, 2]  # tried the head (failed), then advanced to the next


def test_claim_one_survives_transient_http_error():
    """A transient HTTP error (e.g. ConnectionError) on the first candidate must
    be caught, logged, and skipped so claim_one continues to the next candidate
    instead of crashing the entire tick."""
    queue = [
        {"number": 1, "repoFullName": "a/b", "issueId": "i1", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "head"},
        {"number": 2, "repoFullName": "a/b", "issueId": "i2", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "next"},
    ]
    call_count = [0]

    def flaky_post(url, headers, payload):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("DNS resolution failed")
        return {"ok": True}

    client = DispatchClient("http://d", "tok",
                            http_get=lambda u, h: queue, http_post=flaky_post)
    item = client.claim_one("foreman-coder", "local")
    assert item is not None and item.issue_number == 2
    assert call_count[0] == 2  # first call raised, second succeeded


def _client_recording_posts(responses=None):
    from bridge.claim import DispatchClient
    posts = []
    resp = list(responses or [])

    def http_post(url, headers, payload):
        posts.append((url, payload))
        return resp.pop(0) if resp else {}

    return DispatchClient("http://d", "tok", lambda u, h: [], http_post), posts


def test_set_lane_posts_manual_classification():
    from bridge.models import ClaimedItem
    c, posts = _client_recording_posts()
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.set_lane(item, "frontier", "3 failed attempts") is True
    url, payload = posts[0]
    assert url == "http://d/api/issues/id-7/lane"
    assert payload["model"] == "bridge-escalation"
    assert payload["classification"] == {"lane": "frontier", "confidence": "high",
                                         "reason": "3 failed attempts"}


def test_unclaim_posts_release():
    from bridge.models import ClaimedItem
    c, posts = _client_recording_posts()
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.unclaim(item, "foreman-coder") is True
    url, payload = posts[0]
    assert url == "http://d/api/issues/unclaim"
    assert payload == {"issueId": "id-7", "repoFullName": "a/b", "issueNumber": 7,
                       "agentName": "foreman-coder"}


def test_unclaim_treats_400_as_success():
    """Dispatch returns 400 for closed/done/already-unclaimed issues.
    Treat this as success — the issue is effectively released either way."""
    from bridge.models import ClaimedItem
    import requests as req

    def http_post_400(url, headers, payload):
        r = req.HTTPError("400 Bad Request")
        r.response = type("Response", (), {"status_code": 400})()
        raise r

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post_400)
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.unclaim(item, "foreman-coder") is True


def test_unclaim_non_400_error_still_raises():
    """Non-400 errors (500, network) must propagate — don't swallow real failures."""
    from bridge.models import ClaimedItem
    import requests as req

    def http_post_500(url, headers, payload):
        r = req.HTTPError("500 Server Error")
        r.response = type("Response", (), {"status_code": 500})()
        raise r

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post_500)
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    try:
        c.unclaim(item, "foreman-coder")
        assert False, "should have raised"
    except req.HTTPError:
        pass


def test_escalate_succeeds_when_unclaim_400():
    """set_lane success + unclaim 400 -> escalation succeeds (issue is moved + release is a no-op)."""
    from bridge.models import ClaimedItem
    import requests as req

    calls = []

    def http_post_mixed(url, headers, payload):
        calls.append(url)
        if "unclaim" in url:
            r = req.HTTPError("400 Bad Request")
            r.response = type("Response", (), {"status_code": 400})()
            raise r
        return {}

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post_mixed)
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is True
    assert calls == ["http://d/api/issues/id-7/lane", "http://d/api/issues/unclaim"]


def test_escalate_stops_after_failed_set_lane():
    """set_lane failure -> unclaim must NOT run; issue stays claimed in original lane."""
    from bridge.models import ClaimedItem
    # First POST (set_lane) -> None (failure); unclaim must NOT run.
    c, posts = _client_recording_posts(responses=[None])
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is False
    assert len(posts) == 1


def test_escalate_lane_then_unclaim():
    """Atomic escalation: lane is set BEFORE the claim is released (issue #99)."""
    from bridge.models import ClaimedItem
    c, posts = _client_recording_posts(responses=[{}, {}])
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is True
    assert [u for u, _ in posts] == ["http://d/api/issues/id-7/lane", "http://d/api/issues/unclaim"]


def test_escalate_set_lane_failure_does_not_release_claim():
    """Issue #99: a failed set_lane must not release the claim back to the original lane.

    Previously escalate() did ``unclaim and set_lane``: if the unclaim succeeded
    but set_lane failed, the issue was released into its ORIGINAL lane while
    reconcile_failures kept a Failed tombstone. On the next tick the issue
    could be re-claimed with the same deterministic Workload name that now
    collides with the tombstone -- a 409 no-op leaves the issue claimed with
    no backing Workload. The fix orders the calls so the release is the last
    step: a failed set_lane leaves the issue still claimed in its original
    lane.
    """
    from bridge.models import ClaimedItem

    posts = []

    def http_post(url, headers, payload):
        posts.append((url, payload))
        if url.endswith("/lane"):
            # Simulate dispatch lane endpoint down/broken.
            return None
        # unclaim would never be reached; provide a sentinel so any call here
        # fails the test loudly.
        raise AssertionError(f"unclaim must not be called when set_lane fails; got {url}")

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post)
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is False
    # Only the set_lane attempt was made; the issue is still claimed in the
    # original lane (no unclaim POST, no release), so reconcile_failures can
    # safely retry without leaving a re-claimable / stalled state.
    assert len(posts) == 1
    assert posts[0][0] == "http://d/api/issues/id-7/lane"


def test_escalate_set_lane_exception_does_not_release_claim():
    """Same invariant when set_lane raises instead of returning a falsy body."""
    from bridge.models import ClaimedItem

    posts = []

    def http_post(url, headers, payload):
        posts.append((url, payload))
        if url.endswith("/lane"):
            raise RuntimeError("dispatch lane endpoint down")
        raise AssertionError(f"unclaim must not be called when set_lane raises; got {url}")

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post)
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    import pytest
    with pytest.raises(RuntimeError):
        c.escalate(item, "frontier", "r", "foreman-coder")
    # Bug class: if unclaim had already run, the issue would be released into
    # its original lane with reconcile_failures keeping a Failed tombstone.
    # The atomic ordering must keep the claim intact.
    assert len(posts) == 1
    assert posts[0][0] == "http://d/api/issues/id-7/lane"


def test_find_issue_id_scans_lanes_and_matches_repo_number():
    from bridge.claim import DispatchClient
    queues = {
        "local": [{"repoFullName": "a/b", "number": 9, "issueId": "id-9"}],
        "frontier": [{"repoFullName": "a/b", "number": 7, "issueId": "id-7"}],
    }

    def http_get(url, headers):
        lane = url.split("lane=")[1].split("&")[0]
        return queues.get(lane, [])

    c = DispatchClient("http://d", "tok", http_get, lambda u, h, p: {})
    assert c.find_issue_id("agent", ["local", "frontier"], "a/b", 7) == "id-7"
    assert c.find_issue_id("agent", ["local", "frontier"], "a/b", 99) == ""


def test_list_pr_fix_queued_queries_each_lane():
    calls = []
    def http_get(url, headers):
        calls.append(url)
        return [{"repo": "o/r", "pr": 1}] if "NORMAL" in url else [{"repo": "o/r", "pr": 2}]
    c = DispatchClient("http://d", "t", http_get, lambda *a: {})
    items = c.list_pr_fix_queued(["NORMAL", "ESCALATED"])
    assert {i["pr"] for i in items} == {1, 2}
    assert any("lane=NORMAL" in u and "/api/pr-fix-queue/queued" in u for u in calls)
    assert any("lane=ESCALATED" in u for u in calls)


def test_mark_pr_fix_posts_payload():
    seen = {}
    def http_post(url, headers, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True}
    c = DispatchClient("http://d", "t", lambda *a: [], http_post)
    assert c.mark_pr_fix("o/r", 5, "FIXED", "done") is True
    assert seen["url"].endswith("/api/pr-fix-queue/mark")
    assert seen["payload"] == {"repo": "o/r", "pr": 5, "status": "FIXED", "note": "done"}


def test_mark_pr_fix_false_when_post_returns_none():
    c = DispatchClient("http://d", "t", lambda *a: [], lambda *a: None)
    assert c.mark_pr_fix("o/r", 5, "FIXED") is False


# ── update_status contract ──────────────────────────────────────────────────


def test_update_status_posts_full_identity_payload():
    """update_status must POST {issueId, repoFullName, issueNumber, status, agentName}."""
    posts = []

    def http_post(url, headers, payload):
        posts.append((url, payload))
        return {"ok": True}

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post)
    item = {"issueId": "iss_abc", "repoFullName": "a/b", "number": 42}
    assert c.update_status(item, "ready", "foreman-coder") is True
    url, payload = posts[0]
    assert url == "http://d/api/issues/status"
    assert payload == {
        "issueId": "iss_abc",
        "repoFullName": "a/b",
        "issueNumber": 42,
        "status": "ready",
        "agentName": "foreman-coder",
    }


def test_update_status_strips_status_prefix():
    """A 'status/ready' input is normalized to bare 'ready'."""
    posts = []

    def http_post(url, headers, payload):
        posts.append(payload)
        return {"ok": True}

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post)
    item = {"issueId": "iss_1", "repoFullName": "a/b", "number": 7}
    c.update_status(item, "status/ready", "agent")
    assert posts[0]["status"] == "ready"


def test_update_status_accepts_current_lane_key():
    """Items from list_claimed use 'currentLane'; update_status must not require it."""
    posts = []

    def http_post(url, headers, payload):
        posts.append(payload)
        return {"ok": True}

    c = DispatchClient("http://d", "tok", lambda u, h: [], http_post)
    item = {"issueId": "iss_x", "repoFullName": "o/r", "number": 99, "currentLane": "local"}
    c.update_status(item, "ready", "agent")
    assert posts[0]["issueNumber"] == 99


def test_replace_labels_removes_and_adds_in_order():
    c, posts = _client_recording_posts()
    item = {"issueId": "i", "repoFullName": "o/r", "number": 1}
    assert c.replace_labels(item, ["old"], ["new"]) is True
    assert [url.rsplit("/", 1)[-1] for url, _ in posts] == ["unlabel", "label"]


def test_update_status_false_when_post_returns_none():
    c = DispatchClient("http://d", "t", lambda u, h: [], lambda u, h, p: None)
    item = {"issueId": "iss_1", "repoFullName": "a/b", "number": 1}
    assert c.update_status(item, "ready", "agent") is False


def test_has_open_pr_removed():
    """has_open_pr must not exist on DispatchClient (replaced by hasOpenPr field)."""
    c = DispatchClient("http://d", "t", lambda u, h: [], lambda u, h, p: {})
    assert not hasattr(c, "has_open_pr")


# ── list_claimed contract ───────────────────────────────────────────────────


def test_list_claimed_returns_items_with_has_open_pr():
    """list_claimed returns raw dicts from the endpoint; items carry hasOpenPr."""
    claimed_response = [
        {"issueId": "iss_1", "number": 42, "repoFullName": "a/b",
         "currentLane": "local", "labels": ["status/in-progress"], "hasOpenPr": False},
        {"issueId": "iss_2", "number": 99, "repoFullName": "a/b",
         "currentLane": "cloud", "labels": ["status/in-progress"], "hasOpenPr": True},
    ]

    def http_get(url, headers):
        return claimed_response

    c = DispatchClient("http://d", "tok", http_get, lambda u, h, p: {})
    result = c.list_claimed("foreman-coder")
    assert len(result) == 2
    assert result[0]["hasOpenPr"] is False
    assert result[1]["hasOpenPr"] is True


# --- issue_state ---------------------------------------------------------------
# The contract that matters: None for every ambiguous outcome, never a guess.
# Callers cancel work on an explicit "closed", so a wrong answer here cancels real
# retries.

def _client(get_impl):
    from bridge.claim import DispatchClient
    return DispatchClient("http://d", "tok", get_impl, lambda *a, **k: {})


def test_issue_state_returns_open():
    c = _client(lambda url, headers: {"state": "open", "number": 38})
    assert c.issue_state("misospace/llmkube-images", 38) == "open"


def test_issue_state_returns_closed():
    c = _client(lambda url, headers: {"state": "closed", "number": 38})
    assert c.issue_state("misospace/llmkube-images", 38) == "closed"


def test_issue_state_encodes_repo_and_number_in_the_query():
    seen = {}

    def get(url, headers):
        seen["url"] = url
        return {"state": "open"}

    _client(get).issue_state("misospace/llmkube-images", 38)
    assert "/api/issues/state?" in seen["url"]
    # the slash in the repo must survive as a value, not split the path
    assert "repo=misospace%2Fllmkube-images" in seen["url"]
    assert "number=38" in seen["url"]


def test_issue_state_none_on_http_error():
    """http_get raises for status, so a 404 arrives as an exception -> unknown."""
    def boom(url, headers):
        raise RuntimeError("404 Not Found")

    assert _client(boom).issue_state("o/n", 1) is None


def test_issue_state_none_on_non_dict_response():
    assert _client(lambda url, headers: ["unexpected"]).issue_state("o/n", 1) is None
    assert _client(lambda url, headers: None).issue_state("o/n", 1) is None


def test_issue_state_none_when_state_missing_or_empty():
    assert _client(lambda url, headers: {"number": 1}).issue_state("o/n", 1) is None
    assert _client(lambda url, headers: {"state": ""}).issue_state("o/n", 1) is None
    assert _client(lambda url, headers: {"state": 42}).issue_state("o/n", 1) is None


def test_issue_state_normalises_case_and_whitespace():
    assert _client(lambda url, headers: {"state": "OPEN"}).issue_state("o/n", 1) == "open"
    assert _client(lambda url, headers: {"state": " Closed "}).issue_state("o/n", 1) == "closed"


def test_issue_state_none_for_an_unrecognised_state():
    """An unknown value must read as unknown, not as 'not closed'.

    Passing it through would silently bypass the caller's check: reconcile_failures
    only skips on an explicit "closed", so any other string quietly means "retry".
    None is the same behaviour but honest about why.
    """
    for weird in ("merged", "draft", "locked", "unknown", "OPENISH"):
        assert _client(lambda url, headers: {"state": weird}).issue_state("o/n", 1) is None


def test_list_issues_returns_open_snapshots():
    from bridge.claim import DispatchClient
    c = DispatchClient("http://d", "tok", lambda url, headers: [{"id": "i", "number": 1}], lambda *args: {})
    assert c.list_issues()[0]["id"] == "i"
    assert c.list_issues()[0]["number"] == 1


def test_issue_is_parked_when_backlog_or_marker_label_is_present():
    assert _client(lambda url, headers: {"labels": ["status/backlog"]}).issue_is_parked(
        "o/n", 1, "needs-human"
    ) is True
    assert _client(lambda url, headers: {"labels": ["needs-human"]}).issue_is_parked(
        "o/n", 1, "needs-human"
    ) is True
    assert _client(lambda url, headers: {"labels": ["status/ready"]}).issue_is_parked(
        "o/n", 1, "needs-human"
    ) is False


def test_issue_is_parked_fails_open_when_label_snapshot_is_unknown():
    assert _client(lambda url, headers: {"state": "open"}).issue_is_parked(
        "o/n", 1, "needs-human"
    ) is None
    assert _client(lambda url, headers: None).issue_is_parked(
        "o/n", 1, "needs-human"
    ) is None


# --- Parallel lane-fetching tests ---

class TestFindIssueIdParallel:
    """find_issue_id uses queues() which fetches lanes in parallel."""

    def test_finds_across_parallel_lanes(self):
        client = _client(lambda *a, **kw: [
            {"repoFullName": "org/repo", "number": 42, "issueId": "id-42"}
        ])
        assert client.find_issue_id("a", ["l1", "l2"], "org/repo", 42) == "id-42"

    def test_empty_lanes_returns_empty_string(self):
        client = _client(lambda *a, **kw: [])
        assert client.find_issue_id("a", [], "org/repo", 42) == ""


class TestQueues:
    """queues() fetches all lanes in parallel and returns {lane: [items]}."""

    def test_returns_dict_with_lane_keys(self):
        client = _client(lambda *a, **kw: [{"id": 1}])
        result = client.queues("a", ["l1", "l2"])
        assert set(result.keys()) == {"l1", "l2"}
        assert result["l1"] == [{"id": 1}]
        assert result["l2"] == [{"id": 1}]

    def test_empty_lanes_returns_empty_dict(self):
        client = _client(lambda *a, **kw: [])
        result = client.queues("a", [])
        assert result == {}

    def test_lane_failure_logged_and_empty_list(self, caplog):
        call_count = [0]

        def side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("timeout")
            return [{"id": 2}]

        client = _client(side_effect)
        result = client.queues("a", ["l1", "l2"])
        assert result["l1"] == []
        assert result["l2"] == [{"id": 2}]
        assert any("lane-fetch-failed" in r.message for r in caplog.records)

    def test_parallel_execution(self):
        """All lanes are fetched concurrently (verified with a barrier)."""
        import threading
        barrier = threading.Barrier(3, timeout=5)
        started = threading.Event()
        completed = threading.Event()

        def side_effect(*a, **kw):
            started.set()
            barrier.wait()  # synchronize all threads
            completed.set()
            return []

        client = _client(side_effect)
        result = client.queues("a", ["l1", "l2", "l3"])
        assert set(result.keys()) == {"l1", "l2", "l3"}
        assert started.is_set()
        assert completed.is_set()


class TestListPrFixQueuedParallel:
    """list_pr_fix_queued fetches lanes in parallel, preserves lane order."""

    def test_concatenates_in_lane_order(self):
        call_count = [0]

        def side_effect(*a, **kw):
            call_count[0] += 1
            return [{"id": call_count[0]}]

        client = _client(side_effect)
        result = client.list_pr_fix_queued(["NORMAL", "ESCALATED"])
        assert len(result) == 2

    def test_empty_lanes_returns_empty_list(self):
        client = _client(lambda *a, **kw: [])
        assert client.list_pr_fix_queued([]) == []

    def test_lane_failure_logged_and_skipped(self, caplog):
        call_count = [0]

        def side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("timeout")
            return [{"id": 2}]

        client = _client(side_effect)
        result = client.list_pr_fix_queued(["l1", "l2"])
        assert len(result) == 1
        assert any("pr-fix-lane-fetch-failed" in r.message for r in caplog.records)

    def test_parallel_execution(self):
        """All lanes are fetched concurrently (verified with a barrier)."""
        import threading
        barrier = threading.Barrier(2, timeout=5)
        started = threading.Event()
        completed = threading.Event()

        def side_effect(*a, **kw):
            started.set()
            barrier.wait()  # synchronize all threads
            completed.set()
            return []

        client = _client(side_effect)
        result = client.list_pr_fix_queued(["NORMAL", "ESCALATED"])
        assert result == []
        assert started.is_set()
        assert completed.is_set()


# ── token redaction in error messages (issue #35) ────────────────────────────


def test_http_get_wraps_exception_args_with_redacted_token():
    """_http_get must redact Bearer tokens from exception args before re-raising."""
    import requests as req

    def http_get(url, headers):
        r = req.HTTPError("500 Server Error: Bearer eyJhbGciOiJIUzI1NiJ9.secret")
        r.response = type("Response", (), {"status_code": 500})()
        raise r

    c = DispatchClient("http://d", "tok", http_get, lambda *a: {})
    try:
        c._http_get("http://d/api/test")
        assert False, "should have raised"
    except req.HTTPError as exc:
        msg = str(exc)
        assert "Bearer ***" in msg
        assert "eyJhbGciOiJIUzI1NiJ9.secret" not in msg


def test_http_post_wraps_exception_args_with_redacted_token():
    """_http_post must redact Bearer tokens from exception args before re-raising."""
    import requests as req

    def http_post(url, headers, payload):
        r = req.HTTPError("500 Server Error: Bearer eyJhbGciOiJIUzI1NiJ9.secret")
        r.response = type("Response", (), {"status_code": 500})()
        raise r

    c = DispatchClient("http://d", "tok", lambda *a: {}, http_post)
    try:
        c._http_post("http://d/api/test", {})
        assert False, "should have raised"
    except req.HTTPError as exc:
        msg = str(exc)
        assert "Bearer ***" in msg
        assert "eyJhbGciOiJIUzI1NiJ9.secret" not in msg


def test_redact_token_does_not_touch_non_tokens():
    """_redact_token should leave non-token strings unchanged."""
    from bridge.claim import _redact_token

    assert _redact_token("no token here") == "no token here"
    assert _redact_token("Bearer ***") == "Bearer ***"  # already redacted
    assert _redact_token("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret") == \
        "Authorization: Bearer ***"


class TestLaneWorkerCap:
    """#255: the per-tick ThreadPoolExecutor must be capped at MAX_LANE_WORKERS
    so a misconfigured DISPATCH_LANES cannot spawn hundreds of threads."""

    @staticmethod
    def _captured_max_workers(method, n_lanes):
        import bridge.claim as claim_mod
        from unittest.mock import patch

        captured = {}

        class _SpyExecutor(claim_mod.ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                captured["max_workers"] = kwargs.get("max_workers")
                super().__init__(*args, **kwargs)

        def get_impl(url, headers):
            return {"items": []}

        c = _client(get_impl)
        with patch.object(claim_mod, "ThreadPoolExecutor", _SpyExecutor):
            if method == "queues":
                c.queues("a/b", [f"lane{i}" for i in range(n_lanes)])
            else:
                c.list_pr_fix_queued([f"lane{i}" for i in range(n_lanes)])
        return captured["max_workers"]

    @pytest.mark.parametrize("n_lanes", [1, 3, 16, 17, 100])
    def test_queues_max_workers_capped(self, n_lanes):
        from bridge.claim import MAX_LANE_WORKERS
        assert self._captured_max_workers("queues", n_lanes) == min(n_lanes, MAX_LANE_WORKERS)

    @pytest.mark.parametrize("n_lanes", [1, 3, 16, 17, 100])
    def test_list_pr_fix_queued_max_workers_capped(self, n_lanes):
        from bridge.claim import MAX_LANE_WORKERS
        assert self._captured_max_workers("list_pr_fix_queued", n_lanes) == min(n_lanes, MAX_LANE_WORKERS)

    def test_queues_zero_lanes_spawns_no_executor(self):
        # min(0, MAX_LANE_WORKERS) == 0, but ThreadPoolExecutor rejects
        # max_workers=0, so the empty-lane path must short-circuit before
        # constructing the pool.
        import bridge.claim as claim_mod
        from unittest.mock import patch

        created = []

        class _SpyExecutor(claim_mod.ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                created.append(kwargs.get("max_workers"))
                super().__init__(*args, **kwargs)

        c = _client(lambda url, headers: {"items": []})
        with patch.object(claim_mod, "ThreadPoolExecutor", _SpyExecutor):
            assert c.queues("a/b", []) == {}
        assert created == []

    def test_list_pr_fix_queued_zero_lanes_spawns_no_executor(self):
        import bridge.claim as claim_mod
        from unittest.mock import patch

        created = []

        class _SpyExecutor(claim_mod.ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                created.append(kwargs.get("max_workers"))
                super().__init__(*args, **kwargs)

        c = _client(lambda url, headers: [])
        with patch.object(claim_mod, "ThreadPoolExecutor", _SpyExecutor):
            assert c.list_pr_fix_queued([]) == []
        assert created == []

    def test_queues_still_returns_all_lanes_above_cap(self):
        c = _client(lambda url, headers: {"items": []})
        lanes = [f"lane{i}" for i in range(20)]
        assert c.queues("a/b", lanes) == {lane: [] for lane in lanes}

    def test_list_pr_fix_queued_still_returns_all_lanes_above_cap(self):
        c = _client(lambda url, headers: [])
        lanes = [f"lane{i}" for i in range(20)]
        assert c.list_pr_fix_queued(lanes) == []

    def test_warn_if_lane_cap_exceeded_logs_once(self, caplog):
        import bridge.claim as claim_mod
        claim_mod._lane_cap_warned = False
        try:
            with caplog.at_level("WARNING", logger="bridge.claim"):
                claim_mod.warn_if_lane_cap_exceeded([f"lane{i}" for i in range(20)])
                claim_mod.warn_if_lane_cap_exceeded([f"lane{i}" for i in range(20)])
            warnings = [r for r in caplog.records if r.levelname == "WARNING" and "DISPATCH_LANES" in r.getMessage()]
            assert len(warnings) == 1
            assert "20" in warnings[0].getMessage()
        finally:
            claim_mod._lane_cap_warned = False

    def test_warn_silent_at_or_below_cap(self, caplog):
        import bridge.claim as claim_mod
        claim_mod._lane_cap_warned = False
        try:
            with caplog.at_level("WARNING", logger="bridge.claim"):
                claim_mod.warn_if_lane_cap_exceeded(["local", "cloud", "frontier"])
            assert not [r for r in caplog.records if "DISPATCH_LANES" in r.getMessage()]
        finally:
            claim_mod._lane_cap_warned = False

    def test_warn_gated_by_env_flag(self, caplog, monkeypatch):
        import bridge.claim as claim_mod
        claim_mod._lane_cap_warned = False
        monkeypatch.setenv("DISPATCH_LANES_WARN", "0")
        try:
            with caplog.at_level("WARNING", logger="bridge.claim"):
                claim_mod.warn_if_lane_cap_exceeded([f"lane{i}" for i in range(20)])
            assert not [r for r in caplog.records if "DISPATCH_LANES" in r.getMessage()]
        finally:
            claim_mod._lane_cap_warned = False
