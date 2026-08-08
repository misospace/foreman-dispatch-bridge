import pytest
from bridge.prfix import (
    DEFAULT_PRFIX_LANE_AGENTS,
    PRFIX_CREATED_BY,
    PRFIX_PR_ANNOTATION,
    PRFIX_REPO_ANNOTATION,
    PrFixItem,
    assemble_fix_prompt,
    build_fix_workload,
    drain_pr_fixes,
    parse_pr_fix_item,
    pr_fix_coder_for,
    prfix_workload_name,
    classify_check_runs,
    classify_pr_lifecycle,
    rebuild_prfix_manifest,
    reconcile_pr_fixes,
)


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


def test_build_fix_workload_gateless_emits_issue_fix_only():
    """Gateless PR fixes rely on repository CI, so no fixverify step is emitted."""
    wl = build_fix_workload(
        _item(repo="o/r", pr=9, issue=42, branch="b"),
        "llm", {"language": "python"}, "a", "coder", verify_enabled=False,
    )
    steps = wl["spec"]["pipeline"]
    assert [s["kind"] for s in steps] == ["issue-fix"]
    assert steps[0]["name"] == "fix-9"
    assert wl["spec"]["gateProfile"] == {"language": "python"}


def test_build_fix_workload_default_keeps_verify_step():
    """Default verify_enabled=True preserves the existing code → verify pipeline."""
    wl = build_fix_workload(_item(repo="o/r", pr=9, branch="b"),
                            "llm", None, "a", "coder")
    assert [s["kind"] for s in wl["spec"]["pipeline"]] == ["issue-fix", "verify"]


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


def test_drain_gateless_creates_issue_fix_only_no_verify():
    """verify_enabled=False drain creates a Workload with issue-fix only, no verify."""
    created = []
    drain_pr_fixes(
        list_queued=lambda: [_raw(repo="o/r", pr=5)],
        existing_prfix_names=set(), create_workload=created.append,
        gate_profiles={"o/r": {"language": "python"}}, lane_agents={}, agent_name="a", namespace="llm",
        verify_enabled=False,
    )
    assert len(created) == 1
    steps = created[0]["spec"]["pipeline"]
    assert [s["kind"] for s in steps] == ["issue-fix"]
    assert "verify" not in [s["kind"] for s in steps]
    assert created[0]["spec"]["gateProfile"] == {"language": "python"}


def _wl(pr, phase, attempt=1, name=None, lane="NORMAL", coder="coder"):
    name = name or f"prfix-o-r-{pr}"
    return {
        "metadata": {
            "name": name, "namespace": "llm",
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": lane},
            "annotations": {
                "foreman.llmkube.dev/attempt": str(attempt),
                "foreman.llmkube.dev/prfix-repo": "o/r",
                "foreman.llmkube.dev/prfix-pr": str(pr),
            },
        },
        "spec": {"repo": "o/r", "pipeline": [
            {"name": f"fix-{pr}", "kind": "issue-fix", "agentRef": {"name": coder}},
        ]},
        "status": {"phase": phase},
    }


def test_rebuild_prfix_manifest_preserves_gateless_shape():
    """Rebuilding a gateless PR-fix Workload preserves the issue-fix-only pipeline
    and increments the attempt counter — verify_enabled toggles after creation
    do not affect PR-fix retries because rebuild reuses the existing spec."""
    wl = _wl(9, "Failed", attempt=1)
    wl["spec"]["pipeline"] = [
        {"name": "fix-9", "kind": "issue-fix"},
    ]
    fresh = rebuild_prfix_manifest(wl, attempt=2)
    assert [s["kind"] for s in fresh["spec"]["pipeline"]] == ["issue-fix"]
    assert len(fresh["spec"]["pipeline"]) == 1
    assert fresh["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"


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


def test_reconcile_normal_at_max_escalates_to_frontier():
    # NORMAL tier exhausted -> escalate to ESCALATED (coder-frontier) with a fresh
    # attempt budget, NOT straight to BLOCKED/needs-human.
    created, deleted, marks = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=3, lane="NORMAL", coder="coder")],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda *a: marks.append(a),
        max_attempts=3, lane_agents=DEFAULT_PRFIX_LANE_AGENTS,
    )
    assert marks == []                       # not blocked
    assert deleted == ["prfix-o-r-5"]
    esc = created[0]
    assert esc["metadata"]["labels"]["lane"] == "ESCALATED"
    assert esc["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "1"  # fresh budget
    fixstep = next(s for s in esc["spec"]["pipeline"] if s.get("kind") == "issue-fix")
    assert fixstep["agentRef"]["name"] == "coder-frontier"
    assert out == ["prfix-o-r-5:escalate:NORMAL->ESCALATED"]


def test_reconcile_escalated_at_max_marks_blocked():
    # Top of the ladder (ESCALATED) exhausted -> genuinely BLOCKED, no further tier.
    created, marks, deleted = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=3, lane="ESCALATED", coder="coder-frontier")],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        max_attempts=3, lane_agents=DEFAULT_PRFIX_LANE_AGENTS,
    )
    assert created == []                     # no further escalation
    assert deleted == []                     # blocked, not recreated
    assert marks[0][:3] == ("o/r", 5, "BLOCKED")
    assert "3/3" in marks[0][3]              # note carries attempt count
    assert out == ["prfix-o-r-5:giveup:3/3"]


def test_reconcile_succeeded_but_pr_conflicting_retries_instead_of_fixed():
    """Fix workload succeeded, but the PR is still CONFLICTING/DIRTY on GitHub
    (the KubeTix#198 case): must not mark FIXED, must retry like a Failed one."""
    created, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(198, "Succeeded", attempt=1)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda *a: (_ for _ in ()).throw(AssertionError("must not mark FIXED")),
        pr_is_mergeable=lambda repo, pr: "conflicting",
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-198"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert out == ["prfix-o-r-198:not-mergeable-retry:2/3"]


def test_reconcile_succeeded_and_mergeable_marks_fixed():
    marks, deleted = [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded")],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "ok",
    )
    assert marks == [("o/r", 5, "FIXED")]
    assert deleted == ["prfix-o-r-5"]
    assert out == ["prfix-o-r-5:fixed"]


def test_reconcile_succeeded_still_conflicting_at_max_marks_blocked_not_fixed():
    marks, deleted, created = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(198, "Succeeded", attempt=3)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        pr_is_mergeable=lambda repo, pr: "conflicting",
        max_attempts=3,
    )
    assert marks[0][:3] == ("o/r", 198, "BLOCKED")     # not silently dropped, and not FIXED
    assert "not mergeable" in marks[0][3]
    assert out == ["prfix-o-r-198:not-mergeable-giveup:3/3"]
    assert deleted == []                                # tombstone kept, same as the Failed/giveup path
    assert created == []


def test_reconcile_default_pr_is_mergeable_preserves_prior_behavior():
    """No pr_is_mergeable injected -> defaults to always-mergeable, matching
    pre-fix behavior for callers that don't check (backward compatible)."""
    marks, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded")],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status)) or True,
    )
    assert marks == [("o/r", 5, "FIXED")]
    assert out == ["prfix-o-r-5:fixed"]


def test_reconcile_succeeded_mark_fails_at_max_gives_up_blocked():
    """When mark_pr_fix(FIXED) keeps failing at the attempt cap, stop retrying
    and mark BLOCKED so the PR doesn't sit stuck forever."""
    marks, deleted, created = [], [], []
    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status, note))
        return False  # mark always fails
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=3)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=_mark,
        max_attempts=3,
    )
    # First call was mark FIXED (failed), second is mark BLOCKED
    assert marks[0][:3] == ("o/r", 5, "FIXED")
    assert marks[1][:3] == ("o/r", 5, "BLOCKED")  # gave up, surfaced BLOCKED
    assert out == ["prfix-o-r-5:giveup:3/3"]
    assert deleted == []                          # tombstone kept


def test_reconcile_succeeded_mark_fails_under_max_retries():
    """When mark_pr_fix(FIXED) fails but we're under the attempt cap,
    delete + recreate so the next tick gets a fresh attempt."""
    marks, deleted, created = [], [], []
    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status, note))
        return False  # mark always fails
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=_mark,
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-5"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert out == ["prfix-o-r-5:retry:2/3"]
    # mark FIXED was attempted (and failed), then retry path taken
    assert marks == [("o/r", 5, "FIXED", "foreman fix Workload prfix-o-r-5 succeeded")]


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


def test_reconcile_checks_pending_does_not_burn_retry():
    """When CI checks are still pending, the workload is left alone and no
    retry attempt is consumed — the next reconcile tick will pick it up."""
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "checks_pending",
        max_attempts=3,
    )
    # Workload is NOT deleted or recreated — checks_pending is non-terminal
    assert deleted == []
    assert created == []
    # mark FIXED was NOT attempted
    assert marks == []
    assert out == ["prfix-o-r-5:checks-pending:1/3"]


def test_reconcile_checks_failed_retries_under_cap():
    """When CI checks have failed, the workload is retried (delete + recreate)
    under the attempt cap."""
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-5"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert marks == []
    assert out == ["prfix-o-r-5:not-mergeable-retry:2/3"]


def test_reconcile_dirty_status_retries():
    """When mergeable_state is dirty (new commits pushed), the workload is
    retried under the attempt cap."""
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "dirty",
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-5"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert marks == []
    assert out == ["prfix-o-r-5:not-mergeable-retry:2/3"]


def test_reconcile_checks_pending_at_cap_blocks():
    """Even at the attempt cap, checks_pending should not burn a retry —
    but since we're at the cap, it falls through to BLOCKED."""
    marks, deleted = [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=3)],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "checks_pending",
        max_attempts=3,
    )
    # At the cap with checks_pending -> BLOCKED (not FIXED)
    assert marks[0][:3] == ("o/r", 5, "BLOCKED")
    assert deleted == []


def test_build_fix_workload_stamps_verdict_policy():
    from bridge.prfix import build_fix_workload, parse_pr_fix_item
    item = parse_pr_fix_item({"repo": "o/r", "pr": 7, "branch": "foreman/x",
                              "lane": "NORMAL", "reason": "r", "feedback": ["f"]})
    wl = build_fix_workload(item, "llm", None, "agent", "coder",
                            self_go=["code-fix", "ci-policy"])
    assert wl["spec"]["verdictPolicy"] == {"selfGO": ["code-fix", "ci-policy"]}
    wl = build_fix_workload(item, "llm", None, "agent", "coder")
    assert "verdictPolicy" not in wl["spec"]


# --- classify_check_runs ------------------------------------------------
# These exist because the logic they cover used to be a closure inside main.py
# that every test replaced with a lambda, so two defects sat in it unnoticed.

@pytest.mark.parametrize("check_runs", [[], None, ()])
def test_no_check_runs_is_pending_not_ok(check_runs):
    """The misospace/dispatch#731 force-push loop. During the GitHub Actions
    outage no check run was ever created; the old code set neither flag and fell
    through to "ok", so reconcile marked the fix FIXED without verifying
    anything, dispatch re-queued the still-broken PR, and the coder force-pushed
    again. GitHub's own combined status for such a commit is "pending"."""
    assert classify_check_runs(check_runs) == "checks_pending"


def test_failure_is_the_spelling_github_actually_emits():
    """The old code tested for "failed", which the API never returns, so a red
    PR classified as "ok". Verified against real check-run data."""
    assert classify_check_runs([{"conclusion": "failure", "status": "completed"}]) == "checks_failed"


@pytest.mark.parametrize(
    "conclusion", ["timed_out", "cancelled", "action_required", "startup_failure", "stale"]
)
def test_other_non_passing_conclusions_are_failures(conclusion):
    assert classify_check_runs([{"conclusion": conclusion, "status": "completed"}]) == "checks_failed"


def test_all_successful_is_ok():
    """The negative cases only mean something if the passing case still passes."""
    runs = [{"conclusion": "success", "status": "completed"} for _ in range(3)]
    assert classify_check_runs(runs) == "ok"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested"])
def test_unfinished_checks_are_pending(status):
    assert classify_check_runs([{"conclusion": None, "status": status}]) == "checks_pending"


def test_a_failure_outranks_pending_and_success():
    """A red check must not be masked by a sibling still running: reporting
    pending would leave the workload waiting instead of retrying the fix."""
    runs = [
        {"conclusion": "success", "status": "completed"},
        {"conclusion": None, "status": "in_progress"},
        {"conclusion": "failure", "status": "completed"},
    ]
    assert classify_check_runs(runs) == "checks_failed"


def test_skipped_and_neutral_do_not_count_as_failures():
    """Skipped path filters and neutral results are normal on green PRs;
    treating them as failures would retry fixes against healthy branches."""
    runs = [
        {"conclusion": "skipped", "status": "completed"},
        {"conclusion": "neutral", "status": "completed"},
        {"conclusion": "success", "status": "completed"},
    ]
    assert classify_check_runs(runs) == "ok"


def test_outage_does_not_mark_a_fix_workload_FIXED():
    """End-to-end guard on the actual damage: with no check runs, reconcile must
    NOT mark FIXED and must NOT rebuild the workload (which is what re-ran the
    coder and force-pushed). It leaves the workload for the next tick."""
    marked, created, deleted = [], [], []
    results = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(731, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: (
            marked.append((repo, pr, status)) or True
        ),
        pr_is_mergeable=lambda repo, pr: classify_check_runs([]),
        max_attempts=3,
    )
    assert marked == [], f"marked FIXED off an unverified success: {marked}"
    assert created == [], f"rebuilt the workload, which re-runs the coder: {created}"
    assert deleted == []
    assert any("checks-pending" in r for r in results), results


def test_check_runs_api_error_does_not_mark_fixed():
    """#93: When the check-runs API call fails (5xx, rate limit), pr_is_mergeable
    must return "checks_pending" so the workload stays and reconcile retries under
    the attempt cap — never mark FIXED off an unverified success."""
    marked = []
    created = []
    deleted = []
    results = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(467, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marked.append((repo, pr, status)) or True,
        # Simulate check-runs API failure by returning "checks_pending"
        # (this is what pr_is_mergeable now returns on exception)
        pr_is_mergeable=lambda repo, pr: "checks_pending",
        max_attempts=3,
    )
    assert marked == [], f"marked FIXED off an unverified success: {marked}"
    assert created == [], f"rebuilt the workload: {created}"
    assert deleted == []
    assert any("checks-pending" in r for r in results), results


# --- terminal PRs (#118) ----------------------------------------------------
# A Failed fix Workload used to retry purely against the attempt cap, so a merged
# PR burned all three attempts AND escalated to the frontier coder. Observed live
# on prfix-misospace-pr-reviewer-action-467 (merged, attempt=3, lane=ESCALATED)
# and prfix-misospace-kubetix-327 (merged, attempt=2).

@pytest.mark.parametrize("phase", ["Failed", "Succeeded", "Completed"])
def test_merged_pr_is_resolved_not_retried(phase):
    marks, created, deleted = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(467, phase, attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status)) or True,
        pr_is_mergeable=lambda repo, pr: "merged",
        max_attempts=3,
    )
    assert created == [], f"retried a merged PR: {created}"
    assert deleted == ["prfix-o-r-467"]
    assert marks == [("o/r", 467, "FIXED")]
    assert out == ["prfix-o-r-467:pr-merged"]


def test_merged_pr_does_not_escalate_at_the_attempt_cap():
    """The expensive half of #118: at the cap the loop escalated to the frontier
    coder rather than giving up, so a merged PR bought frontier tokens."""
    created = []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(467, "Failed", attempt=3)],
        delete_workload=lambda n: None,
        create_workload=created.append,
        mark_pr_fix=lambda *a: True,
        pr_is_mergeable=lambda repo, pr: "merged",
        max_attempts=3,
        lane_agents=DEFAULT_PRFIX_LANE_AGENTS,
    )
    assert created == [], f"escalated a merged PR: {created}"
    assert out == ["prfix-o-r-467:pr-merged"]


def test_closed_unmerged_pr_is_marked_stale():
    marks = []
    reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(9, "Failed", attempt=1)],
        delete_workload=lambda n: None,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("must not retry")),
        mark_pr_fix=lambda repo, pr, status, note: marks.append(status) or True,
        pr_is_mergeable=lambda repo, pr: "closed",
        max_attempts=3,
    )
    assert marks == ["STALE"]


def test_workload_is_kept_when_the_mark_fails():
    """Deleting on a failed mark would lose the item entirely — leave the
    tombstone so the next tick retries the mark, matching the FIXED path."""
    deleted = []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(467, "Failed", attempt=1)],
        delete_workload=deleted.append,
        create_workload=lambda m: None,
        mark_pr_fix=lambda *a: False,
        pr_is_mergeable=lambda repo, pr: "merged",
        max_attempts=3,
    )
    assert deleted == []
    assert out == ["prfix-o-r-467:pr-merged:mark-failed"]


def test_open_pr_still_retries_normally():
    """The guard must not swallow live work."""
    created = []
    reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=1)],
        delete_workload=lambda n: None,
        create_workload=created.append,
        mark_pr_fix=lambda *a: True,
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
    )
    assert len(created) == 1


@pytest.mark.parametrize("data,expected", [
    ({"merged": True, "state": "closed", "mergeable_state": "unknown"}, "merged"),
    ({"merged": False, "state": "closed"}, "closed"),
    ({"merged": False, "state": "open", "mergeable_state": "clean"}, None),
    ({"state": "OPEN"}, None),
    ({}, None),
    (None, None),
])
def test_classify_pr_lifecycle(data, expected):
    """mergeable_state=unknown on a merged PR reads as mergeable, which is what
    sent the loop through its full budget — so merged must win over it."""
    assert classify_pr_lifecycle(data) == expected
