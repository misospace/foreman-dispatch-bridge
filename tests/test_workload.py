from bridge.models import ClaimedItem
from bridge.workload import (
    workload_name,
    build_workload,
    parse_gate_profiles,
    gate_profile_for,
)

ITEM = ClaimedItem(repo="joryirving/home-ops", issue_number=42,
                   intent="Fix the flaky reconcile test", lane="local")


def test_workload_name_is_deterministic_and_sanitized():
    assert workload_name(ITEM) == "wl-joryirving-home-ops-42"


def test_build_workload_uses_single_coder_gate_reviewer():
    wl = build_workload(ITEM, namespace="llm")
    assert wl["spec"]["coderAgentRef"]["name"] == "coder"
    assert wl["spec"]["verifierAgentRef"]["name"] == "gate"
    assert wl["spec"]["reviewerAgentRefs"] == [{"name": "reviewer"}]


def test_build_workload_structure():
    wl = build_workload(ITEM, namespace="llm")
    assert wl["apiVersion"] == "foreman.llmkube.dev/v1alpha1"
    assert wl["kind"] == "Workload"
    assert wl["metadata"]["namespace"] == "llm"
    assert wl["metadata"]["labels"] == {"created-by": "dispatch-bridge", "lane": "local"}
    assert wl["spec"]["repo"] == "joryirving/home-ops"
    assert wl["spec"]["issues"] == [42]
    assert wl["spec"]["intent"] == "Fix the flaky reconcile test"


def test_build_workload_omits_gate_profile_by_default():
    # No profile -> no gateProfile key, so Foreman keeps its Go default.
    assert "gateProfile" not in build_workload(ITEM, namespace="llm")["spec"]


def test_build_workload_stamps_retry_annotations():
    item = ClaimedItem(repo="a/b", issue_number=9, intent="x", lane="local", issue_id="id-9")
    ann = build_workload(item, namespace="llm", agent_name="foreman-coder", attempt=2)["metadata"]["annotations"]
    assert ann["foreman.llmkube.dev/attempt"] == "2"
    assert ann["foreman.llmkube.dev/issue-id"] == "id-9"
    assert ann["foreman.llmkube.dev/agent-name"] == "foreman-coder"


def test_build_workload_defaults_attempt_to_one():
    assert build_workload(ITEM, namespace="llm")["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "1"


def test_build_workload_passes_gate_profile_through_verbatim():
    profile = {"language": "node", "commands": {"test": "corepack pnpm i && corepack pnpm test"}}
    wl = build_workload(ITEM, namespace="llm", gate_profile=profile)
    assert wl["spec"]["gateProfile"] == profile


def test_parse_gate_profiles_empty_is_empty_dict():
    assert parse_gate_profiles(None) == {}
    assert parse_gate_profiles("") == {}
    assert parse_gate_profiles("   ") == {}


def test_parse_gate_profiles_parses_json_map():
    raw = '{"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}'
    assert parse_gate_profiles(raw) == {
        "misospace/dispatch": {"language": "node"},
        "*": {"language": "generic"},
    }


def test_gate_profile_for_prefers_exact_match_then_wildcard():
    profiles = {"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}
    assert gate_profile_for("misospace/dispatch", profiles) == {"language": "node"}
    # Unmatched repo falls back to the wildcard.
    assert gate_profile_for("misospace/miso-chat", profiles) == {"language": "generic"}


def test_gate_profile_for_returns_none_when_no_match_and_no_wildcard():
    assert gate_profile_for("misospace/miso-chat", {"misospace/dispatch": {"language": "node"}}) is None
    assert gate_profile_for("a/b", {}) is None


def test_parse_lane_coder_agents_empty_is_empty_dict():
    from bridge.workload import parse_lane_coder_agents
    assert parse_lane_coder_agents(None) == {}
    assert parse_lane_coder_agents("") == {}
    assert parse_lane_coder_agents("  ") == {}


def test_coder_agent_for_prefers_exact_then_wildcard_then_default():
    from bridge.workload import coder_agent_for
    agents = {"*": "coder", "frontier": "coder-frontier"}
    assert coder_agent_for("frontier", None, agents) == "coder-frontier"
    assert coder_agent_for("local", None, agents) == "coder"
    assert coder_agent_for("local", None, {"frontier": "coder-frontier"}) == "coder"  # no wildcard -> default
    assert coder_agent_for("anything", None, {}) == "coder"


def test_parse_base_coder_agents_empty_is_empty_dict():
    from bridge.workload import parse_base_coder_agents
    assert parse_base_coder_agents(None) == {}
    assert parse_base_coder_agents("") == {}
    assert parse_base_coder_agents("  ") == {}


def test_parse_base_coder_agents_parses_json_map():
    from bridge.workload import parse_base_coder_agents
    raw = '{"python": "coder-python", "node": "coder-node", "go": "coder-go", "*": "coder"}'
    assert parse_base_coder_agents(raw) == {
        "python": "coder-python", "node": "coder-node", "go": "coder-go", "*": "coder",
    }


def test_coder_agent_for_routes_base_lane_by_language():
    from bridge.workload import coder_agent_for
    base = {"python": "coder-python", "node": "coder-node", "go": "coder-go", "*": "coder"}
    assert coder_agent_for("local", "python", {}, base) == "coder-python"
    assert coder_agent_for("local", "node", {}, base) == "coder-node"
    assert coder_agent_for("local", "go", {}, base) == "coder-go"


def test_coder_agent_for_base_lane_falls_back_to_wildcard_for_unknown_language():
    from bridge.workload import coder_agent_for
    base = {"python": "coder-python", "*": "coder"}
    assert coder_agent_for("local", "generic", {}, base) == "coder"
    assert coder_agent_for("local", None, {}, base) == "coder"


def test_coder_agent_for_explicit_lane_wins_over_language():
    # Escalation (frontier) and revision tiers are language-agnostic: an
    # explicit per-lane mapping wins outright regardless of the repo's language.
    from bridge.workload import coder_agent_for
    lane_agents = {"frontier": "coder-frontier"}
    base = {"python": "coder-python", "*": "coder"}
    assert coder_agent_for("frontier", "python", lane_agents, base) == "coder-frontier"
    assert coder_agent_for("frontier", "node", lane_agents, base) == "coder-frontier"


def test_coder_agent_for_empty_base_coder_agents_is_legacy_behavior():
    from bridge.workload import coder_agent_for
    assert coder_agent_for("local", "python", {}, {}) == "coder"
    assert coder_agent_for("local", "python", {}, None) == "coder"


def test_build_workload_uses_explicit_coder_agent():
    wl = build_workload(ITEM, "llm", coder_agent="coder-frontier")
    assert wl["spec"]["coderAgentRef"] == {"name": "coder-frontier"}


def test_build_workload_first_attempt_sets_allow_overwrite():
    # allowOverwrite is now always set (not just attempt > 1): branchStrategy=
    # reset makes the task branch re-derivable, and `attempt` is not a reliable
    # branch-existence signal — a status-reset re-dispatch resets it to 1 while
    # the pushed branch persists, which is what wedged retries on PUSH-FAILED.
    wl = build_workload(ITEM, namespace="llm", attempt=1)
    assert wl["spec"]["allowOverwrite"] is True


def test_build_workload_retry_sets_allow_overwrite_on_issues_path():
    wl = build_workload(ITEM, namespace="llm", attempt=2)
    assert wl["spec"]["allowOverwrite"] is True
    assert "pipeline" not in wl["spec"]


def test_build_workload_retry_sets_allow_overwrite_on_pipeline_code_step():
    wl = build_workload(ITEM, namespace="llm", attempt=2, feedback="reviewer said no")
    assert wl["spec"]["allowOverwrite"] is True
    code = [s for s in wl["spec"]["pipeline"] if s["kind"] == "issue-fix"]
    assert len(code) == 1 and code[0]["payload"]["allowOverwrite"] is True
    verify = [s for s in wl["spec"]["pipeline"] if s["kind"] == "verify"]
    assert "allowOverwrite" not in verify[0]["payload"]


def test_revision_coder_agent_for_prefers_exact_then_wildcard_then_empty():
    from bridge.workload import revision_coder_agent_for
    agents = {"*": "coder-revision", "frontier": "coder-revision-frontier"}
    assert revision_coder_agent_for("frontier", agents) == "coder-revision-frontier"
    assert revision_coder_agent_for("local", agents) == "coder-revision"
    assert revision_coder_agent_for("local", {"frontier": "x"}) == ""  # no wildcard -> unset
    assert revision_coder_agent_for("anything", {}) == ""  # unset -> controller falls back + warns


def test_build_workload_omits_revision_coder_ref_by_default():
    wl = build_workload(ITEM, namespace="llm")
    assert "revisionCoderAgentRef" not in wl["spec"]


def test_build_workload_stamps_revision_coder_ref_when_set():
    wl = build_workload(ITEM, namespace="llm", revision_coder_agent="coder-revision")
    assert wl["spec"]["revisionCoderAgentRef"] == {"name": "coder-revision"}


def test_build_workload_feedback_path_has_no_revision_coder_ref():
    # revisionCoderAgentRef is a WorkloadSpec field for the controller's issues-path
    # iteration loop; the explicit-pipeline feedback path has no reviewerAgentRefs to
    # iterate, so it must not carry the field.
    wl = build_workload(ITEM, namespace="llm", feedback="do better", revision_coder_agent="coder-revision")
    assert "pipeline" in wl["spec"]
    assert "revisionCoderAgentRef" not in wl["spec"]
