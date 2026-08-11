import pytest
from bridge.models import ClaimedItem
from bridge.main import run_once, _parse_bool_env
from bridge.workload import _parse_json_map

LANES = ["local", "cloud", "frontier"]


def _claim_stub(mapping):
    # mapping: lane -> ClaimedItem | None. Each lane's item is served once, then
    # None, so run_once's per-lane drain loop terminates.
    served = set()

    def claim_one(agent_name, lane):
        if lane in served:
            return None
        served.add(lane)
        return mapping.get(lane)
    return claim_one


def test_creates_one_workload_per_claimed_lane():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
                   created.append, namespace="llm")
    assert res == ["local:created:wl-a-b-3", "cloud:empty", "frontier:empty"]
    assert len(created) == 1
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"
    assert created[0]["metadata"]["labels"]["lane"] == "local"


def test_all_empty_creates_nothing():
    created = []
    res = run_once(LANES, "foreman/coder", _claim_stub({}), created.append, namespace="llm")
    assert res == ["local:empty", "cloud:empty", "frontier:empty"]
    assert created == []


def test_stamps_matching_gate_profile_on_workload():
    created = []
    item = ClaimedItem(repo="misospace/dispatch", issue_number=7, intent="fix", lane="local")
    profiles = {"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
             created.append, namespace="llm", gate_profiles=profiles)
    assert created[0]["spec"]["gateProfile"] == {"language": "node"}


def test_unmatched_repo_falls_back_to_wildcard_gate_profile():
    created = []
    item = ClaimedItem(repo="misospace/windowstead", issue_number=1, intent="fix", lane="cloud")
    profiles = {"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}
    run_once(LANES, "foreman/coder", _claim_stub({"cloud": item}),
             created.append, namespace="llm", gate_profiles=profiles)
    assert created[0]["spec"]["gateProfile"] == {"language": "generic"}


def test_no_gate_profiles_leaves_workload_without_one():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append, namespace="llm")
    assert "gateProfile" not in created[0]["spec"]


def test_passes_agent_name_and_lane_through():
    seen = []
    def claim_one(agent_name, lane):
        seen.append((agent_name, lane))
        return None
    run_once(LANES, "foreman/coder", claim_one, lambda m: None, namespace="llm")
    assert seen == [("foreman/coder", "local"), ("foreman/coder", "cloud"), ("foreman/coder", "frontier")]


def test_cap_stops_claiming_at_max_in_progress():
    # Already at the cap: no lane should claim, each reports capped.
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item, "cloud": item, "frontier": item}),
                   created.append, namespace="llm", in_progress=10, max_in_progress=10)
    assert created == []
    assert all(r.endswith("capped:10/10") for r in res)


def test_cap_allows_claims_up_to_remaining_headroom():
    # 9 in progress, cap 10: exactly one more claim, then capped.
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item, "cloud": item, "frontier": item}),
                   created.append, namespace="llm", in_progress=9, max_in_progress=10)
    assert len(created) == 1
    assert res[0] == "local:created:wl-a-b-3"
    assert res[1].endswith("capped:10/10") and res[2].endswith("capped:10/10")


def test_drains_lane_up_to_headroom():
    # A lane with 3 ready items and headroom for 2 claims both, then stops at cap.
    created = []
    items = [ClaimedItem(repo="a/b", issue_number=n, intent="x", lane="local")
             for n in (1, 2, 3)]

    def claim_one(agent_name, lane):
        return items.pop(0) if lane == "local" and items else None

    res = run_once(["local"], "foreman/coder", claim_one, created.append,
                   namespace="llm", in_progress=8, max_in_progress=10)
    assert len(created) == 2  # only the 2 slots of headroom
    assert res == ["local:created:wl-a-b-1", "local:created:wl-a-b-2"]


def test_drains_whole_lane_when_uncapped():
    # No cap: a lane with multiple ready items is fully drained in one tick.
    created = []
    items = [ClaimedItem(repo="a/b", issue_number=n, intent="x", lane="local")
             for n in (1, 2, 3)]

    def claim_one(agent_name, lane):
        return items.pop(0) if lane == "local" and items else None

    res = run_once(["local"], "foreman/coder", claim_one, created.append,
                   namespace="llm")
    assert len(created) == 3
    assert res == ["local:created:wl-a-b-1", "local:created:wl-a-b-2", "local:created:wl-a-b-3"]


def test_cap_zero_is_uncapped():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
             created.append, namespace="llm", in_progress=999, max_in_progress=0)
    assert len(created) == 1  # cap 0 => no gating even with high in_progress


def test_base_lane_routes_to_python_coder_by_repo_language():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=1, intent="fix", lane="local")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    base_coder_agents = {"python": "coder-python", "node": "coder-node", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-python"


def test_base_lane_routes_to_node_coder_by_repo_language():
    created = []
    item = ClaimedItem(repo="misospace/dispatch", issue_number=2, intent="fix", lane="local")
    gate_profiles = {"misospace/dispatch": {"language": "node"}}
    base_coder_agents = {"python": "coder-python", "node": "coder-node", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-node"


def test_base_lane_no_gate_profile_falls_back_to_wildcard_coder():
    created = []
    item = ClaimedItem(repo="misospace/unmapped", issue_number=3, intent="fix", lane="local")
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_base_lane_generic_language_falls_back_to_wildcard_coder():
    created = []
    item = ClaimedItem(repo="misospace/generic-repo", issue_number=4, intent="fix", lane="local")
    gate_profiles = {"misospace/generic-repo": {"language": "generic"}}
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_frontier_lane_wins_over_language_routing():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=5, intent="fix", lane="frontier")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    lane_coder_agents = {"frontier": "coder-frontier"}
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"frontier": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles,
             lane_coder_agents=lane_coder_agents, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-frontier"


def test_empty_base_coder_agents_is_legacy_behavior():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=6, intent="fix", lane="local")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_parse_bool_env_true_values():
    assert _parse_bool_env("true") is True
    assert _parse_bool_env("TRUE") is True
    assert _parse_bool_env("1") is True
    assert _parse_bool_env("yes") is True
    assert _parse_bool_env("") is True  # empty -> default


def test_parse_bool_env_false_values():
    assert _parse_bool_env("false") is False
    assert _parse_bool_env("FALSE") is False
    assert _parse_bool_env("0") is False
    assert _parse_bool_env("no") is False
    assert _parse_bool_env("NO") is False


def test_parse_bool_env_default_when_empty():
    assert _parse_bool_env("", default=True) is True
    assert _parse_bool_env("", default=False) is False


def test_verify_enabled_false_omits_verifier():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", verify_enabled=False)
    assert "verifierAgentRef" not in created[0]["spec"]
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_verify_enabled_true_keeps_verifier():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm")
    assert created[0]["spec"]["verifierAgentRef"]["name"] == "gate"


def test_parse_json_map_invalid_pr_fix_lane_agents_raises_value_error():
    """Invalid JSON in PR_FIX_LANE_AGENTS must raise ValueError with context."""
    bad = "{'lane': 'local'}"  # single quotes are not valid JSON
    try:
        _parse_json_map(bad, "PR_FIX_LANE_AGENTS")
        assert False, "expected ValueError"
    except ValueError as exc:
        msg = str(exc)
        assert "PR_FIX_LANE_AGENTS" in msg
        assert bad[:80] in msg


def test_delete_workload_respects_timeout():
    """Assert delete_workload exits within the configured timeout window."""
    from unittest.mock import MagicMock
    from kubernetes import client

    import bridge.main

    mock_api = MagicMock()
    call_count = 0

    def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise client.exceptions.ApiException(status=404)

    mock_api.get_namespaced_custom_object.side_effect = fake_get

    # Use a small timeout so the test runs quickly
    bridge.main._delete_workload(mock_api, "default", "test-wl", timeout=3)

    # Should have polled at most 3 times (timeout value) before getting 404
    assert call_count <= 3


def test_delete_workload_raises_timeout():
    """Assert delete_workload raises TimeoutError when poll exhausts the window."""
    from unittest.mock import MagicMock

    import bridge.main

    mock_api = MagicMock()
    call_count = 0

    def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"metadata": {"name": "test-wl"}}

    mock_api.get_namespaced_custom_object.side_effect = fake_get

    timeout = 2
    with pytest.raises(TimeoutError) as exc_info:
        bridge.main._delete_workload(mock_api, "default", "test-wl", timeout=timeout)

    assert call_count == timeout
    assert str(timeout) in str(exc_info.value)


class TestDispatchUrlWarning:
    """Tests for cleartext HTTP warning on DISPATCH_URL (issue #54)."""

    def test_warning_emitted_for_http_url(self, caplog):
        import logging

        import bridge.main

        with caplog.at_level(logging.WARNING):
            bridge.main._check_dispatch_url("http://dispatch.llm:3000")
        assert any(
            "cleartext HTTP" in record.message for record in caplog.records
        )

    def test_no_warning_for_https_url(self, caplog):
        import logging

        import bridge.main

        with caplog.at_level(logging.WARNING):
            bridge.main._check_dispatch_url("https://dispatch.llm:3000")
        assert not any(
            "cleartext HTTP" in record.message for record in caplog.records
        )
