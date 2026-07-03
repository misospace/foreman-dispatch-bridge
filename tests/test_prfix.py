from bridge.prfix import PrFixItem, parse_pr_fix_item, assemble_fix_prompt, pr_fix_coder_for, DEFAULT_PRFIX_LANE_AGENTS


def _item(**kw):
    base = dict(repo="o/r", pr=1, issue=None, branch="b", head_sha=None,
               lane="NORMAL", type="OTHER", reason="", feedback=[])
    base.update(kw)
    return PrFixItem(**base)


def test_assemble_fix_prompt_ci_failure():
    p = assemble_fix_prompt(_item(type="CI_FAILURE", reason="pytest failed",
                                  feedback=["test_a failed", "test_b failed"]))
    assert p.startswith("CI failure:")
    assert "pytest failed" in p
    assert "- test_a failed" in p and "- test_b failed" in p


def test_assemble_fix_prompt_review_and_other_headers():
    assert assemble_fix_prompt(_item(type="REVIEW_FEEDBACK", reason="r")).startswith("Review feedback:")
    assert assemble_fix_prompt(_item(type="MERGE_CONFLICT", reason="r")).startswith("Merge conflict:")
    # OTHER has no header prefix, just the reason.
    assert assemble_fix_prompt(_item(type="OTHER", reason="just this")).strip() == "just this"


def test_pr_fix_coder_for_precedence():
    agents = {"NORMAL": "coder", "ESCALATED": "coder-frontier"}
    assert pr_fix_coder_for("ESCALATED", agents) == "coder-frontier"
    assert pr_fix_coder_for("NORMAL", agents) == "coder"
    assert pr_fix_coder_for("NORMAL", {"*": "c2"}) == "c2"        # wildcard
    assert pr_fix_coder_for("NORMAL", {}) == "coder"             # fallback
    assert DEFAULT_PRFIX_LANE_AGENTS == {"NORMAL": "coder", "ESCALATED": "coder-frontier"}


def test_parse_pr_fix_item_full():
    raw = {
        "repo": "misospace/miso-gallery", "pr": 295, "issue": 252,
        "branch": "foreman/wl-x/issue-252", "headSha": "abc123",
        "lane": "NORMAL", "type": "CI_FAILURE", "reason": "pytest failed",
        "feedback": ["tests/test_x.py::test_y failed", "AssertionError"],
    }
    item = parse_pr_fix_item(raw)
    assert item == PrFixItem(
        repo="misospace/miso-gallery", pr=295, issue=252,
        branch="foreman/wl-x/issue-252", head_sha="abc123",
        lane="NORMAL", type="CI_FAILURE", reason="pytest failed",
        feedback=["tests/test_x.py::test_y failed", "AssertionError"],
    )


def test_parse_pr_fix_item_missing_optionals():
    item = parse_pr_fix_item({"repo": "o/r", "pr": 7, "lane": "ESCALATED", "type": "OTHER", "reason": "x"})
    assert item.issue is None and item.branch is None and item.head_sha is None
    assert item.feedback == []


def test_parse_pr_fix_item_unusable_returns_none():
    assert parse_pr_fix_item({"pr": 7}) is None          # no repo
    assert parse_pr_fix_item({"repo": "o/r"}) is None     # no pr
    assert parse_pr_fix_item("not a dict") is None


from bridge.prfix import (
    prfix_workload_name, build_fix_workload,
    PRFIX_REPO_ANNOTATION, PRFIX_PR_ANNOTATION, PRFIX_CREATED_BY,
)


def test_prfix_workload_name_deterministic_sanitized():
    assert prfix_workload_name(_item(repo="misospace/miso-gallery", pr=295)) == "prfix-misospace-miso-gallery-295"


def test_build_fix_workload_code_verify_only_no_review():
    item = _item(repo="o/r", pr=9, issue=42, branch="foreman/wl-x/issue-42",
                 type="REVIEW_FEEDBACK", reason="address comments", feedback=["use Rel not prefix"])
    wl = build_fix_workload(item, namespace="llm", gate_profile={"language": "python"},
                            agent_name="foreman-coder", coder_agent="coder", attempt=1)
    assert wl["metadata"]["name"] == "prfix-o-r-9"
    assert wl["metadata"]["namespace"] == "llm"
    assert wl["metadata"]["labels"]["created-by"] == PRFIX_CREATED_BY
    assert wl["metadata"]["labels"]["lane"] == "NORMAL"
    assert wl["metadata"]["annotations"][PRFIX_REPO_ANNOTATION] == "o/r"
    assert wl["metadata"]["annotations"][PRFIX_PR_ANNOTATION] == "9"
    assert wl["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "1"
    steps = wl["spec"]["pipeline"]
    kinds = [s["kind"] for s in steps]
    assert kinds == ["issue-fix", "verify"]                     # code + verify only, NO review
    code = steps[0]
    assert code["agentRef"] == {"name": "coder"}
    assert code["payload"]["branch"] == "foreman/wl-x/issue-42"
    assert code["payload"]["reviseFromBranch"] == "foreman/wl-x/issue-42"
    assert code["payload"]["allowOverwrite"] is True
    assert code["payload"]["issue"] == 42
    assert "address comments" in code["payload"]["prompt"]
    assert wl["spec"]["gateProfile"] == {"language": "python"}
    assert "openPullRequest" not in code["payload"]


def test_build_fix_workload_omits_issue_when_absent():
    wl = build_fix_workload(_item(repo="o/r", pr=9, issue=None, branch="b"),
                            "llm", None, "a", "coder")
    assert "issue" not in wl["spec"]["pipeline"][0]["payload"]
    assert "gateProfile" not in wl["spec"]
