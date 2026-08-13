from bridge.models import ClaimedItem
from bridge.workload import (
    _branch_name,
    _parse_json_map,
    workload_name,
    build_workload,
    parse_gate_profiles,
    gate_profile_for,
    parse_self_go,
    coder_agent_for,
    coder_candidates,
    coders_saturated,
    parse_coder_agent_slots,
    _pick_coder,
    _slots_for,
    CODER_AGENT,
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


def test_build_workload_first_attempt_omits_allow_overwrite():
    # First attempt must NOT set allowOverwrite: bare allowOverwrite without
    # reviseFromBranch force-pushes base over any existing work — silent data
    # loss when the re-run produces an empty diff (issue #101).
    wl = build_workload(ITEM, namespace="llm", attempt=1)
    assert "allowOverwrite" not in wl["spec"]
    assert "reviseFromBranch" not in wl["spec"]


BRANCH = "foreman/wl-joryirving-home-ops-42/issue-42"


def test_build_workload_retry_sets_allow_overwrite_on_issues_path():
    # Overwrite is gated on branch evidence, not on attempt: the caller passes the
    # branch only when the failed Workload's tasks prove it was pushed.
    wl = build_workload(ITEM, namespace="llm", attempt=2, revise_from_branch=BRANCH)
    assert wl["spec"]["allowOverwrite"] is True
    assert wl["spec"]["reviseFromBranch"] == BRANCH
    assert "pipeline" not in wl["spec"]


def test_build_workload_retry_sets_allow_overwrite_on_pipeline_code_step():
    wl = build_workload(
        ITEM, namespace="llm", attempt=2, feedback="reviewer said no", revise_from_branch=BRANCH
    )
    assert wl["spec"]["allowOverwrite"] is True
    assert wl["spec"]["reviseFromBranch"] == BRANCH
    code = [s for s in wl["spec"]["pipeline"] if s["kind"] == "issue-fix"]
    assert len(code) == 1 and code[0]["payload"]["allowOverwrite"] is True
    verify = [s for s in wl["spec"]["pipeline"] if s["kind"] == "verify"]
    assert "allowOverwrite" not in verify[0]["payload"]


def test_build_workload_pipeline_review_step_opens_pr():
    # Explicit pipelines must set openPullRequest per step (the issues path gets
    # it stamped from spec.openPullRequest). Without it a retried workload
    # reviews GO but never opens a PR — the reviewed work strands on its branch.
    wl = build_workload(ITEM, namespace="llm", attempt=2, feedback="reviewer said no")
    review = [s for s in wl["spec"]["pipeline"] if s["kind"] == "review"]
    assert len(review) == 1
    assert review[0]["payload"]["openPullRequest"] is True


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


# --- Gateless mode (verify_enabled=False) ---


def test_build_workload_gateless_omits_verifier_agent_ref():
    """Issues path: verify_enabled=False must not stamp verifierAgentRef so
    Foreman 0.9.9+ wires review directly to code."""
    wl = build_workload(ITEM, namespace="llm", verify_enabled=False)
    assert "verifierAgentRef" not in wl["spec"]
    assert wl["spec"]["coderAgentRef"]["name"] == "coder"
    assert wl["spec"]["reviewerAgentRefs"] == [{"name": "reviewer"}]


def test_build_workload_gateless_feedback_pipeline_has_no_verify_step():
    """Feedback path: verify_enabled=False must produce code → review (no verify),
    with review steps depending on code instead of verify."""
    wl = build_workload(ITEM, namespace="llm", attempt=2, feedback="reviewer said no",
                        verify_enabled=False)
    steps = wl["spec"]["pipeline"]
    kinds = [s["kind"] for s in steps]
    assert "verify" not in kinds
    assert kinds.count("issue-fix") == 1
    assert kinds.count("review") == 1
    review = [s for s in steps if s["kind"] == "review"][0]
    code = [s for s in steps if s["kind"] == "issue-fix"][0]
    assert review["dependsOn"] == [code["name"]]


def test_build_workload_default_keeps_verifier():
    """Default verify_enabled=True keeps the verifier (backward compat)."""
    wl = build_workload(ITEM, namespace="llm")
    assert wl["spec"]["verifierAgentRef"]["name"] == "gate"


def test_build_workload_gateless_preserves_gate_profile():
    """Gateless issue-path Workloads still carry gateProfile for coder self-gate
    and language routing."""
    profile = {"language": "python", "commands": {"test": "pytest -q"}}
    wl = build_workload(ITEM, namespace="llm", gate_profile=profile, verify_enabled=False)
    assert wl["spec"]["gateProfile"] == profile
    assert "verifierAgentRef" not in wl["spec"]


def test_parse_json_map_empty_and_none():
    assert _parse_json_map(None) == {}
    assert _parse_json_map("") == {}
    assert _parse_json_map("  ") == {}


def test_parse_json_map_valid():
    assert _parse_json_map('{"a": 1}') == {"a": 1}


def test_parse_json_map_invalid_json_raises_value_error_with_context():
    """Invalid JSON must raise ValueError with the config name and raw prefix."""
    bad = "{'language': 'python'}"  # single quotes are not valid JSON
    try:
        _parse_json_map(bad, "GATEPROFILE_MAP")
        assert False, "expected ValueError"
    except ValueError as exc:
        msg = str(exc)
        assert "GATEPROFILE_MAP" in msg
        assert bad[:80] in msg


# ── verdictPolicy.selfGO (LLMKube #1075) ──────────────────────────────────


def test_parse_self_go_handles_absent_empty_and_whitespace():
    assert parse_self_go(None) == []
    assert parse_self_go("") == []
    assert parse_self_go("   ") == []
    assert parse_self_go("code-fix, docs ,packaging") == ["code-fix", "docs", "packaging"]


def test_build_workload_omits_verdict_policy_by_default():
    """Unset must leave Foreman's own default policy untouched."""
    wl = build_workload(ITEM, namespace="llm")
    assert "verdictPolicy" not in wl["spec"]
    wl = build_workload(ITEM, namespace="llm", self_go=[])
    assert "verdictPolicy" not in wl["spec"]


def test_build_workload_stamps_verdict_policy_when_set():
    classes = ["code-fix", "docs", "packaging", "config", "ci-policy"]
    wl = build_workload(ITEM, namespace="llm", self_go=classes)
    assert wl["spec"]["verdictPolicy"] == {"selfGO": classes}


def test_build_workload_stamps_verdict_policy_on_the_retry_pipeline_path():
    """The feedback path builds an explicit pipeline; policy must ride along."""
    wl = build_workload(ITEM, namespace="llm", feedback="reviewer said X",
                        self_go=["code-fix", "ci-policy"])
    assert wl["spec"]["verdictPolicy"] == {"selfGO": ["code-fix", "ci-policy"]}
    assert "pipeline" in wl["spec"]


def test_build_workload_verdict_policy_is_copied_not_aliased():
    classes = ["code-fix"]
    wl = build_workload(ITEM, namespace="llm", self_go=classes)
    classes.append("ci-policy")
    assert wl["spec"]["verdictPolicy"]["selfGO"] == ["code-fix"]


def test_branch_name_deterministic():
    item = ClaimedItem(
        repo="misospace/dispatch", issue_number=42, intent="fix bug",
        lane="base", issue_id="1001",
    )
    assert _branch_name(item) == "foreman/wl-misospace-dispatch-42/issue-42"


def test_branch_name_special_chars():
    item = ClaimedItem(
        repo="Foo/Bar-Baz", issue_number=99, intent="fix",
        lane="base", issue_id="1002",
    )
    assert _branch_name(item) == "foreman/wl-foo-bar-baz-99/issue-99"


# --- #101 follow-up: overwrite is gated on branch EVIDENCE, not the attempt counter.
# The first fix keyed on `attempt > 1`, which cannot distinguish "attempt 1 failed
# before it pushed" (reviseFromBranch would hard-fail per LLMKube#1365) from
# "unclaim -> ready reset the counter while the branch survived" (a bare push wedges).

def test_build_workload_omits_overwrite_fields_without_branch_evidence():
    item = ClaimedItem(repo="o/r", issue_number=7, intent="t", lane="local", issue_id="i")
    spec = build_workload(item, "llm")["spec"]
    assert "allowOverwrite" not in spec
    assert "reviseFromBranch" not in spec


def test_build_workload_pairs_overwrite_with_revise_branch():
    item = ClaimedItem(repo="o/r", issue_number=7, intent="t", lane="local", issue_id="i")
    branch = "foreman/wl-o-r-7/issue-7"
    spec = build_workload(item, "llm", revise_from_branch=branch)["spec"]
    assert spec["allowOverwrite"] is True
    assert spec["reviseFromBranch"] == branch


def test_build_workload_attempt_alone_never_sets_overwrite():
    """A high attempt counter is not evidence; only a branch name is."""
    item = ClaimedItem(repo="o/r", issue_number=7, intent="t", lane="local", issue_id="i")
    spec = build_workload(item, "llm", attempt=5)["spec"]
    assert "allowOverwrite" not in spec
    assert "reviseFromBranch" not in spec


# --- per-repo coder routing -------------------------------------------------
# gateProfile.language is an enum (go|python|rust|node|generic), so every repo
# outside those presets is "generic" and BASE_CODER_AGENTS collapses them onto
# one coder. windowstead (GDScript) and pinchflat (Elixir) both need "generic"
# and different runtimes. A coder without the runtime cannot run the tests it
# writes: windowstead#321 shipped a test file that did not parse.

def test_repo_mapping_beats_language():
    assert coder_agent_for(
        "local", "generic", {}, {"generic": "coder", "*": "coder"},
        repo="misospace/windowstead", repo_coder_agents={"misospace/windowstead": "coder-godot"},
    ) == "coder-godot"


def test_two_generic_repos_get_different_coders():
    """The whole point: language cannot distinguish these two."""
    m = {"misospace/windowstead": "coder-godot", "misospace/pinchflat": "coder-elixir"}
    assert coder_agent_for("local", "generic", {}, {"*": "coder"},
                           repo="misospace/windowstead", repo_coder_agents=m) == "coder-godot"
    assert coder_agent_for("local", "generic", {}, {"*": "coder"},
                           repo="misospace/pinchflat", repo_coder_agents=m) == "coder-elixir"


def test_unmapped_repo_still_routes_by_language():
    assert coder_agent_for(
        "local", "python", {}, {"python": "coder-python", "*": "coder"},
        repo="misospace/other", repo_coder_agents={"misospace/windowstead": "coder-godot"},
    ) == "coder-python"


def test_lane_escalation_still_outranks_the_repo_mapping():
    """Deliberate: an escalation lane overrides, so a repo coder cannot silently
    outrank the frontier tier. The cost is that an escalated attempt loses the
    runtime — documented, not accidental."""
    assert coder_agent_for(
        "frontier", "generic", {"frontier": "coder-frontier"}, {"*": "coder"},
        repo="misospace/windowstead", repo_coder_agents={"misospace/windowstead": "coder-godot"},
    ) == "coder-frontier"


def test_absent_map_is_unchanged_behaviour():
    assert coder_agent_for("local", "node", {}, {"node": "coder-node", "*": "coder"}) == "coder-node"
    assert coder_agent_for("local", "generic", {}, {}) == CODER_AGENT


# --- lane rotation ------------------------------------------------------------
# A lane mapping may be a list: the lane's work splits across coders by
# issue % len. Deterministic so retries land on the coder whose backend already
# holds the prompt cache; availability is litellm-fallback's job, not routing's.

def test_lane_list_splits_by_issue_number():
    m = {"*": ["coder", "coder-strix"]}
    assert coder_agent_for("local", "node", m, {}, issue_number=42) == "coder"
    assert coder_agent_for("local", "node", m, {}, issue_number=43) == "coder-strix"


def test_same_issue_always_gets_the_same_coder():
    """Retries must not migrate: the coder's backend holds the issue's cache."""
    m = {"*": ["coder", "coder-strix"]}
    picks = {coder_agent_for("local", "go", m, {}, issue_number=7) for _ in range(5)}
    assert len(picks) == 1


def test_explicit_lane_list_also_rotates():
    m = {"local": ["coder", "coder-strix"], "frontier": "coder-frontier"}
    assert coder_agent_for("local", None, m, {}, issue_number=10) == "coder"
    assert coder_agent_for("local", None, m, {}, issue_number=11) == "coder-strix"
    assert coder_agent_for("frontier", None, m, {}, issue_number=11) == "coder-frontier"


def test_single_string_mapping_unchanged():
    assert coder_agent_for("local", None, {"*": "coder"}, {}, issue_number=99) == "coder"


def test_empty_list_falls_through_to_default():
    assert coder_agent_for("local", None, {"*": []}, {}, issue_number=1) == CODER_AGENT


def test_missing_issue_number_still_resolves():
    m = {"*": ["coder", "coder-strix"]}
    assert coder_agent_for("local", None, m, {}) == "coder"


def test_lane_list_survives_the_env_json_round_trip():
    """The exact string home-ops will set."""
    m = _parse_json_map('{"*": ["coder", "coder-strix"], "frontier": "coder-frontier"}')
    assert coder_agent_for("local", None, m, {}, issue_number=8) == "coder"
    assert coder_agent_for("local", None, m, {}, issue_number=9) == "coder-strix"


def test_language_tier_rotates_so_every_repo_is_covered():
    """The wildcard-lane list alone never fires for python/node/go repos — the
    language tier resolves first. Lists must rotate at every tier or only
    generic repos split."""
    base = {"python": ["coder-python", "coder-strix"], "*": ["coder", "coder-strix"]}
    assert coder_agent_for("local", "python", {}, base, issue_number=4) == "coder-python"
    assert coder_agent_for("local", "python", {}, base, issue_number=5) == "coder-strix"


def test_repo_tier_rotates_too():
    m = {"misospace/windowstead": ["coder-godot", "coder-strix"]}
    assert coder_agent_for("local", "generic", {}, {}, repo="misospace/windowstead",
                           repo_coder_agents=m, issue_number=310) == "coder-godot"
    assert coder_agent_for("local", "generic", {}, {}, repo="misospace/windowstead",
                           repo_coder_agents=m, issue_number=311) == "coder-strix"


# --- capacity-aware coder selection ---------------------------------------


def test_parse_coder_agent_slots_empty_is_empty_dict():
    assert parse_coder_agent_slots("") == {}
    assert parse_coder_agent_slots(None) == {}


def test_pick_coder_without_slots_is_legacy_issue_split():
    # No slots configured: unchanged issue % len behavior.
    agents = ["coder", "coder-frontier"]
    assert _pick_coder(agents, 4) == "coder"
    assert _pick_coder(agents, 5) == "coder-frontier"


def test_pick_coder_prefers_the_agent_with_free_slots():
    # coder is full, coder-frontier is idle: the issue number would have picked
    # coder, capacity picks the one that can start now.
    agents = ["coder", "coder-frontier"]
    slots = {"coder": 1, "coder-frontier": 4}
    assert _pick_coder(agents, 4, {"coder": 1}, slots) == "coder-frontier"


def test_pick_coder_prefers_idle_local_over_busy_cloud():
    agents = ["coder", "coder-frontier"]
    slots = {"coder": 1, "coder-frontier": 4}
    assert _pick_coder(agents, 5, {"coder-frontier": 4}, slots) == "coder"


def test_pick_coder_ties_fall_back_to_issue_number():
    # Equal free capacity: deterministic on issue number, so a retry lands on the
    # backend that already holds its prompt cache.
    agents = ["coder", "coder-frontier"]
    slots = {"coder": 2, "coder-frontier": 2}
    assert _pick_coder(agents, 4, {}, slots) == "coder"
    assert _pick_coder(agents, 5, {}, slots) == "coder-frontier"


def test_pick_coder_unlisted_agent_defaults_to_wildcard_then_one():
    assert _slots_for("coder", {"coder": 3}) == 3
    assert _slots_for("other", {"coder": 3, "*": 5}) == 5
    assert _slots_for("other", {"coder": 3}) == 1


def test_slots_for_rejects_bad_values_rather_than_benching_a_coder():
    # A typo must not silently take a coder out of rotation.
    assert _slots_for("coder", {"coder": 0}) == 1
    assert _slots_for("coder", {"coder": -2}) == 1
    assert _slots_for("coder", {"coder": "two"}) == 1


def test_pick_coder_single_element_and_string_unchanged():
    slots = {"coder": 1}
    assert _pick_coder(["coder"], 7, {"coder": 9}, slots) == "coder"
    assert _pick_coder("coder", 7, {"coder": 9}, slots) == "coder"
    assert _pick_coder([], 7, {}, slots) is None


def test_coder_candidates_resolves_lane_then_wildcard():
    agents = {"*": ["coder"], "frontier": ["coder-frontier"]}
    assert coder_candidates("frontier", agents) == ["coder-frontier"]
    assert coder_candidates("local", agents) == ["coder"]
    assert coder_candidates("local", {"local": "solo"}) == ["solo"]
    assert coder_candidates("local", {}) == []


def test_coders_saturated_only_when_every_candidate_is_full():
    slots = {"coder": 1, "coder-frontier": 2}
    assert coders_saturated(["coder"], {"coder": 1}, slots) is True
    assert coders_saturated(["coder", "coder-frontier"], {"coder": 1}, slots) is False
    # Unconfigured slots never hold work back.
    assert coders_saturated(["coder"], {"coder": 99}, {}) is False
    assert coders_saturated([], {}, slots) is False


def test_coder_agent_for_picks_free_slot_within_the_lane_tier():
    agents = {"*": ["coder", "coder-frontier"]}
    slots = {"coder": 1, "coder-frontier": 4}
    # Issue 4 would hash to coder; coder is full, so capacity wins.
    assert coder_agent_for(
        "local", None, agents, issue_number=4,
        agent_load={"coder": 1}, agent_slots=slots,
    ) == "coder-frontier"
