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


from bridge.prfix import drain_pr_fixes


def _raw(repo="o/r", pr=1, lane="NORMAL", branch="b", **kw):
    d = {"repo": repo, "pr": pr, "lane": lane, "branch": branch, "type": "OTHER", "reason": "x"}
    d.update(kw)
    return d


def test_drain_creates_for_new_items():
    created = []
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(repo="o/r", pr=5)],
        existing_prfix_names=set(),
        create_workload=created.append,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert len(created) == 1 and created[0]["metadata"]["name"] == "prfix-o-r-5"
    assert out == ["o/r#5:created:prfix-o-r-5"]


def test_drain_skips_in_flight_and_branchless():
    created = []
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(pr=5), _raw(pr=6, branch=None)],
        existing_prfix_names={"prfix-o-r-5"},          # 5 already in flight
        create_workload=created.append,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert created == []
    assert "o/r#5:skip:in-flight" in out and "o/r#6:skip:no-branch" in out


def test_drain_isolates_per_item_failure():
    created = []
    def create(m):
        if m["metadata"]["name"] == "prfix-o-r-5":
            raise RuntimeError("boom")
        created.append(m)
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(pr=5), _raw(pr=6)],
        existing_prfix_names=set(), create_workload=create,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert [m["metadata"]["name"] for m in created] == ["prfix-o-r-6"]   # 6 still created
    assert any("o/r#5:error:" in line for line in out)


from bridge.prfix import reconcile_pr_fixes, rebuild_prfix_manifest, PRFIX_CREATED_BY


def _wl(pr, phase, attempt=1, name=None):
    name = name or f"prfix-o-r-{pr}"
    return {
        "metadata": {
            "name": name, "namespace": "llm",
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": "NORMAL"},
            "annotations": {
                "foreman.llmkube.dev/attempt": str(attempt),
                "foreman.llmkube.dev/prfix-repo": "o/r",
                "foreman.llmkube.dev/prfix-pr": str(pr),
            },
        },
        "spec": {"repo": "o/r", "pipeline": [{"name": f"fix-{pr}"}]},
        "status": {"phase": phase},
    }


def test_rebuild_prfix_manifest_bumps_attempt_and_strips_status():
    wl = _wl(5, "Failed", attempt=1)
    wl["metadata"]["resourceVersion"] = "123"
    wl["metadata"]["uid"] = "abc-uid"
    fresh = rebuild_prfix_manifest(wl, attempt=2)
    assert fresh["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert "status" not in fresh
    assert "resourceVersion" not in fresh["metadata"]
    assert "uid" not in fresh["metadata"]
    assert fresh["metadata"]["name"] == "prfix-o-r-5"
    assert fresh["metadata"]["labels"] == {"created-by": PRFIX_CREATED_BY, "lane": "NORMAL"}
    assert fresh["metadata"]["annotations"]["foreman.llmkube.dev/prfix-repo"] == "o/r"
    assert fresh["metadata"]["annotations"]["foreman.llmkube.dev/prfix-pr"] == "5"


def test_reconcile_succeeded_marks_fixed_and_deletes():
    marks, deleted = [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded")],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=_mark,
    )
    assert marks == [("o/r", 5, "FIXED")]
    assert deleted == ["prfix-o-r-5"]
    assert out == ["prfix-o-r-5:fixed"]


def test_reconcile_succeeded_mark_fails_keeps_workload():
    deleted = []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded")],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=lambda *a: False,
    )
    assert deleted == []
    assert out == ["prfix-o-r-5:fixed-mark-pending"]


def test_reconcile_failed_under_max_deletes_and_recreates():
    created, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=1)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda *a: (_ for _ in ()).throw(AssertionError("no mark")),
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-5"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert out == ["prfix-o-r-5:retry:2/3"]


def test_reconcile_failed_at_max_marks_blocked():
    marks, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=3)],
        delete_workload=deleted.append, create_workload=lambda m: None,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        max_attempts=3,
    )
    assert marks[0][:3] == ("o/r", 5, "BLOCKED")
    assert "3/3" in marks[0][3]                       # note carries attempt count
    assert out == ["prfix-o-r-5:giveup:3/3"]
    assert deleted == []


def test_reconcile_ignores_nonterminal_and_isolates_errors():
    marks = []
    def delete(n):
        raise RuntimeError("wedged")
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Running"), _wl(6, "Failed", attempt=1)],
        delete_workload=delete, create_workload=lambda m: None,
        mark_pr_fix=lambda *a: marks.append(a), max_attempts=3,
    )
    assert not any("prfix-o-r-5" in line for line in out)     # Running: untouched
    assert any("prfix-o-r-6:error:" in line for line in out)  # delete raised, isolated
