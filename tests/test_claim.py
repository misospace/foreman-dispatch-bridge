import json
from pathlib import Path
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


def test_claim_one_skips_transient_http_error_and_continues(capsys):
    # Regression for #50: a transient HTTP error (ConnectionError) on the
    # claim POST must not crash claim_one — it should be logged and the loop
    # should advance to the next candidate. This keeps the surrounding tick
    # (and downstream pr-fix/prune passes) alive during partial outages.
    queue = [
        {"number": 1, "repoFullName": "a/b", "issueId": "i1", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "head"},
        {"number": 2, "repoFullName": "a/b", "issueId": "i2", "lane": "local",
         "labels": ["status/ready"], "claimable": True, "title": "next"},
    ]
    posted = []

    def fake_post(url, headers, payload):
        posted.append(payload["issueNumber"])
        if payload["issueNumber"] == 1:
            raise ConnectionError("dispatch API unreachable")
        return {"ok": True}

    client = DispatchClient("http://d", "tok",
                            http_get=lambda u, h: queue, http_post=fake_post)
    item = client.claim_one("foreman-coder", "local")
    out = capsys.readouterr().out
    assert item is not None and item.issue_number == 2
    assert posted == [1, 2]  # tried head (raised), then advanced to next
    assert "claim-error" in out and "#1" in out and "ConnectionError" in out


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
    """unclaim 400 + set_lane success -> escalation succeeds (issue is released + re-laned)."""
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
    assert calls == ["http://d/api/issues/unclaim", "http://d/api/issues/id-7/lane"]


def test_escalate_stops_after_failed_unclaim():
    from bridge.models import ClaimedItem
    # First POST (unclaim) -> None (failure); set_lane must NOT run.
    c, posts = _client_recording_posts(responses=[None])
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is False
    assert len(posts) == 1


def test_escalate_unclaim_then_lane():
    from bridge.models import ClaimedItem
    c, posts = _client_recording_posts(responses=[{}, {}])
    item = ClaimedItem(repo="a/b", issue_number=7, intent="t", lane="local", issue_id="id-7")
    assert c.escalate(item, "frontier", "r", "foreman-coder") is True
    assert [u for u, _ in posts] == ["http://d/api/issues/unclaim", "http://d/api/issues/id-7/lane"]


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
        seen["url"] = url; seen["payload"] = payload
        return {"ok": True}
    c = DispatchClient("http://d", "t", lambda *a: [], http_post)
    assert c.mark_pr_fix("o/r", 5, "FIXED", "done") is True
    assert seen["url"].endswith("/api/pr-fix-queue/mark")
    assert seen["payload"] == {"repo": "o/r", "pr": 5, "status": "FIXED", "note": "done"}


def test_mark_pr_fix_false_when_post_returns_none():
    c = DispatchClient("http://d", "t", lambda *a: [], lambda *a: None)
    assert c.mark_pr_fix("o/r", 5, "FIXED") is False
