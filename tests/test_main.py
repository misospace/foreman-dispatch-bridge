from bridge.models import ClaimedItem
from bridge.main import run_once

LANES = ["local", "cloud", "frontier"]


def _claim_stub(mapping):
    # mapping: lane -> ClaimedItem | None
    def claim_one(agent_name, lane):
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


def test_cap_zero_is_uncapped():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
             created.append, namespace="llm", in_progress=999, max_in_progress=0)
    assert len(created) == 1  # cap 0 => no gating even with high in_progress
