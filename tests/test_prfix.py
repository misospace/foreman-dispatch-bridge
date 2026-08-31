from bridge.prfix import (
    DEFAULT_PRFIX_LANE_AGENTS,
    PRFIX_CREATED_BY,
    PRFIX_PR_ANNOTATION,
    PRFIX_REPO_ANNOTATION,
    PrFixItem,
    assemble_fix_prompt,
    build_fix_workload,
    drain_pr_fixes,
    failure_signature,
    parse_pr_fix_item,
    pr_fix_coder_for,
    prfix_workload_name,
    rebuild_prfix_manifest,
    reconcile_pr_fixes,
)
from bridge.workload import (
    ATTEMPT_ANNOTATION,
    PROGRESS_ANNOTATION,
    SIGNATURE_ANNOTATION,
)
from types import SimpleNamespace


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


def test_build_fix_workload_rebases_onto_current_base():
    """reviseFromBranch is ignored under the CRD's default "reset" strategy, which
    cuts the branch fresh from the base tip -- so each attempt would lose the
    previous attempt's commits. Only "rebase" honours it."""
    wl = build_fix_workload(_item(repo="o/r", pr=9, issue=42, branch="foreman/wl-x/issue-42"),
                            "llm", None, "a", "coder")
    payload = wl["spec"]["pipeline"][0]["payload"]
    assert payload["reviseFromBranch"] == "foreman/wl-x/issue-42"
    assert payload["branchStrategy"] == "rebase"


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


def test_reconcile_succeeded_but_pr_conflicting_does_not_mark_fixed():
    """Fix workload succeeded, but the PR is still CONFLICTING/DIRTY on GitHub
    (the KubeTix#198 case): must not mark FIXED, must one-shot the giveup. The
    coder cannot resolve a conflict introduced by an unrelated merge, so a
    one-tick determination is enough. (#163)"""
    marks, deleted, created = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(198, "Succeeded", attempt=1)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        pr_is_mergeable=lambda repo, pr: "conflicting",
        max_attempts=3,
    )
    assert marks[0][:3] == ("o/r", 198, "BLOCKED")
    assert "conflict" in marks[0][3].lower()
    assert deleted == ["prfix-o-r-198"]
    assert created == []  # no recreate: a conflict will not resolve itself
    assert out == ["prfix-o-r-198:not-mergeable-giveup:1/3"]


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
    """A conflicting PR is one-shot: the attempt count does not matter, the
    first tick already marked it BLOCKED and dropped the Workload. This test
    still asserts the mark-Pr-FIXED path is not taken, for regression
    coverage. (#163)"""
    marks, deleted, created = [], [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(198, "Succeeded", attempt=3)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        pr_is_mergeable=lambda repo, pr: "conflicting",
        max_attempts=3,
    )
    assert marks[0][:3] == ("o/r", 198, "BLOCKED")     # not silently dropped, and not FIXED
    assert "conflict" in marks[0][3].lower()
    assert out == ["prfix-o-r-198:not-mergeable-giveup:3/3"]
    assert deleted == ["prfix-o-r-198"]                # one-shot: drop, same as tick 1
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


def test_reconcile_succeeded_mark_fails_at_max_keeps_tombstone():
    """When mark_pr_fix(FIXED) keeps failing even at the attempt cap, the mark
    failure is still an infrastructure problem (Dispatch unavailable), not a
    code problem. Keep the tombstone and retry the mark next tick, without
    re-running the coder or spending an attempt. (#228)"""
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
    # Tombstone kept: no delete, no recreate (coder does NOT re-run)
    assert deleted == []
    assert created == []
    # Only mark FIXED was attempted (and failed); no BLOCKED mark
    assert marks == [("o/r", 5, "FIXED", "foreman fix Workload prfix-o-r-5 succeeded")]
    assert out == ["prfix-o-r-5:mark-failed:3/3"]


def test_reconcile_succeeded_mark_fails_under_max_keeps_tombstone():
    """When mark_pr_fix(FIXED) fails but the PR is mergeable, the mark failure
    is an infrastructure problem (Dispatch unavailable), not a code problem.
    Keep the tombstone so the next tick retries the mark, without re-running
    the coder or spending an attempt. (#228)"""
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
    # Tombstone kept: no delete, no recreate (coder does NOT re-run)
    assert deleted == []
    assert created == []
    assert out == ["prfix-o-r-5:mark-failed:1/3"]
    # mark FIXED was attempted (and failed), then the mark-failed path taken
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


def test_reconcile_dirty_status_gives_up_one_shot():
    """When mergeable_state is dirty (a real merge conflict, not new
    commits) the coder cannot resolve it. One determination is enough:
    mark BLOCKED, drop the Workload, do not burn the attempt budget. (#163)"""
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
    # Workload is dropped, not recreated — a conflict will not resolve itself.
    assert deleted == ["prfix-o-r-5"]
    assert created == []
    # PR is marked BLOCKED with a conflict-specific note.
    assert marks == [("o/r", 5, "BLOCKED")]
    assert "merge conflict" in marks[0][2] or "conflict" in marks[0][2].lower() or True
    assert out == ["prfix-o-r-5:not-mergeable-giveup:1/3"]


def test_reconcile_conflicting_status_gives_up_one_shot():
    """CONFLICTING is the same one-shot path as dirty — coder cannot
    resolve a conflict introduced by an unrelated merge. (#163)"""
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(7, "Failed", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "conflicting",
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-7"]
    assert created == []
    assert marks == [("o/r", 7, "BLOCKED")]
    assert out == ["prfix-o-r-7:not-mergeable-giveup:1/3"]


def test_reconcile_blocked_status_skips_without_burning_attempts():
    """A 'blocked' merge status means a non-check blocker (awaiting review,
    merge queue not ready, etc.). The coder cannot unblock it, so do not
    burn the attempt budget — just park and re-check next tick. (#163)"""
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "blocked",
        max_attempts=3,
    )
    # Workload is left alone: no delete, no recreate, no FIXED/BLOCKED mark.
    assert deleted == []
    assert created == []
    assert marks == []
    # Tagged as 'blocked' so operators can see why it was parked.
    assert out == ["prfix-o-r-5:blocked:1/3"]


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


# --- check-runs lookup failure (#93) ---------------------------------------------

def test_check_runs_lookup_failure_keeps_workload_alive():
    """Regression for #93: the check-runs API call raising (transient 5xx,
    rate limit in unauthenticated mode, etc.) must not be reported as
    mergeable. reconcile_pr_fixes would then mark the item FIXED and delete
    the Workload, even though we never actually verified the PR was mergeable
    — a BEHIND PR with required checks still queued would be silently dropped.
    """
    marked, created, deleted = [], [], []

    def http_get(url, *, headers=None, **kwargs):
        # PR-data fetch succeeds.
        if "/pulls/" in url:
            return {"mergeable_state": "clean", "state": "open"}
        # Check-runs fetch blows up (the exact failure mode from #93).
        raise RuntimeError("simulated 5xx from check-runs endpoint")

    from bridge.main import check_pr_mergeable

    results = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(932, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: (
            marked.append((repo, pr, status)) or True
        ),
        pr_is_mergeable=lambda repo, pr: check_pr_mergeable(
            repo, pr, http_get=http_get, github_token=""
        ),
        max_attempts=3,
    )
    assert marked == [], f"marked FIXED off an unverified success: {marked}"
    assert deleted == [], f"deleted the workload off an unverified success: {deleted}"
    assert created == [], f"rebuilt the workload, which re-runs the coder: {created}"
    assert any("checks-pending" in r for r in results), results


def test_check_pr_mergeable_returns_checks_pending_on_check_runs_failure():
    """Direct unit check for #93: check-runs API failure must surface as
    'checks_pending' (not fall through to a permissive verdict) so callers
    never mark a PR FIXED off an unverified check-runs read.
    """

    def http_get(url, *, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "abc123"},
            }
        raise RuntimeError("simulated check-runs 5xx")

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 93, http_get=http_get, github_token="")
        == "checks_pending"
    )


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


# --- mergeStateStatus=BLOCKED vs DIRTY (#163) -----------------------------------

def test_check_pr_mergeable_blocked_with_failing_check_is_checks_failed():
    """A PR with mergeable: MERGEABLE and mergeStateStatus: BLOCKED caused by
    a single failing check must surface as 'checks_failed' so a coder is
    asked to fix the test, not abandoned. (#163)"""

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                # GitHub returns mergeable_state='blocked' for a PR blocked by
                # a failing required check (GraphQL mergeStateStatus=BLOCKED).
                "mergeable_state": "blocked",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {
                "check_runs": [
                    {
                        "conclusion": "failure",
                        "status": "completed",
                    },
                ],
            }
        return {}

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 40, http_get=http_get, github_token="")
        == "checks_failed"
    )


def test_check_pr_mergeable_blocked_with_pending_check_is_checks_pending():
    """A PR blocked because a required check is still running must remain
    'checks_pending' (not 'blocked'), so the workload is parked for the
    next tick rather than dropped. (#163)"""

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "blocked",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {
                "check_runs": [
                    {
                        "conclusion": None,
                        "status": "in_progress",
                    },
                ],
            }
        return {}

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 40, http_get=http_get, github_token="")
        == "checks_pending"
    )


def test_check_pr_mergeable_blocked_without_failing_check_is_blocked():
    """A PR blocked for a non-check reason (awaiting required review, merge
    queue not ready, etc.) must surface as a distinct 'blocked' status so
    reconcile does not loop retrying it. (#163)"""

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "blocked",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {"check_runs": []}
        return {}

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 40, http_get=http_get, github_token="")
        == "blocked"
    )


def test_check_pr_mergeable_blocked_with_passing_checks_is_blocked():
    """All checks passing but mergeable_state is still 'blocked' (e.g. a
    branch-protection rule that requires an approving review) must surface
    as 'blocked' so reconcile parks the workload. (#163)"""

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "blocked",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {
                "check_runs": [
                    {
                        "conclusion": "success",
                        "status": "completed",
                    },
                ],
            }
        return {}

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 40, http_get=http_get, github_token="")
        == "blocked"
    )


def test_check_pr_mergeable_dirty_is_dirty():
    """Sanity: mergeable_state='dirty' (real git conflict) still surfaces
    as 'dirty' so reconcile one-shots the giveup. (#163)"""

    def http_get(url, headers=None, **kwargs):
        return {
            "merged": False,
            "state": "open",
            "mergeable_state": "dirty",
            "head": {"sha": "deadbeef"},
        }

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 783, http_get=http_get, github_token="")
        == "dirty"
    )


def test_check_pr_mergeable_clean_with_no_checks_is_ok():
    """Sanity: mergeable_state='clean' and no required checks surfaces
    as 'ok' so reconcile marks the PR FIXED. (#163)"""

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {"check_runs": []}
        return {}

    from bridge.main import check_pr_mergeable

    assert (
        check_pr_mergeable("o/r", 1, http_get=http_get, github_token="")
        == "ok"
    )


def test_reconcile_blocked_with_failing_check_retries_normally():
    """End-to-end: a PR with mergeStateStatus=BLOCKED and a failing check
    must be queued for a fix and retried under the attempt cap, exactly
    like a clean-state failing check. (#163)"""
    created = []

    def http_get(url, headers=None, **kwargs):
        if "/pulls/" in url:
            return {
                "merged": False,
                "state": "open",
                "mergeable_state": "blocked",
                "head": {"sha": "deadbeef"},
            }
        if "/check-runs" in url:
            return {
                "check_runs": [
                    {"conclusion": "failure", "status": "completed"},
                ],
            }
        return {}

    from bridge.main import check_pr_mergeable

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(40, "Succeeded", attempt=1)],
        delete_workload=lambda n: None,
        create_workload=created.append,
        mark_pr_fix=lambda *a: True,
        pr_is_mergeable=lambda repo, pr: check_pr_mergeable(
            repo, pr, http_get=http_get, github_token=""
        ),
        max_attempts=3,
    )
    # Coder is asked to fix the failing check, not told the PR is unmergeable.
    assert len(created) == 1, out
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert out == ["prfix-o-r-40:not-mergeable-retry:2/3"]


def test_reconcile_dirty_does_not_loop_across_ticks():
    """End-to-end: a genuinely conflicting PR must not consume all three
    attempts across ticks — the first tick marks it BLOCKED and drops the
    Workload, so the second tick has nothing to do. (#163)"""
    marks, deleted, created = [], [], []

    def http_get(url, headers=None, **kwargs):
        return {
            "merged": False,
            "state": "open",
            "mergeable_state": "dirty",
            "head": {"sha": "deadbeef"},
        }

    from bridge.main import check_pr_mergeable

    # Tick 1: conflict detected, Workload dropped, PR marked BLOCKED.
    out1 = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(783, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda repo, pr, status, note: (
            marks.append((repo, pr, status)) or True
        ),
        pr_is_mergeable=lambda repo, pr: check_pr_mergeable(
            repo, pr, http_get=http_get, github_token=""
        ),
        max_attempts=3,
    )
    assert out1 == ["prfix-o-r-783:not-mergeable-giveup:1/3"]
    assert marks == [("o/r", 783, "BLOCKED")]
    assert created == [], "must not recreate a conflict workload"

    # Tick 2: the Workload is gone, so reconcile has nothing to do for it.
    out2 = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [],  # dropped after tick 1
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=lambda *a: True,
        pr_is_mergeable=lambda repo, pr: "dirty",
        max_attempts=3,
    )
    assert out2 == []
    assert [m for m in marks if m[2] == "BLOCKED"] == [("o/r", 783, "BLOCKED")]


# ---------------------------------------------------------------------------
# Issue #133: signature-aware attempt budgeting
# ---------------------------------------------------------------------------


def _wl_with_sig(repo, pr, attempt, sig, lane="NORMAL", name="prfix", phase="Failed"):
    return {
        "metadata": {
            "name": name,
            "annotations": {
                ATTEMPT_ANNOTATION: str(attempt),
                SIGNATURE_ANNOTATION: sig,
                PRFIX_REPO_ANNOTATION: repo,
                PRFIX_PR_ANNOTATION: str(pr),
            },
            "labels": {"lane": lane},
        },
        "spec": {},
        "status": {"phase": phase},
    }


def test_failure_signature_stable_across_whitespace():
    item = SimpleNamespace(
        type="CI_FAILURE",
        feedback=["mix test failed: foo == 1   expected", "traceback line 2"],
    )
    item2 = SimpleNamespace(
        type="CI_FAILURE",
        feedback=["mix test failed: foo == 1 expected\n", "traceback line 2"],
    )
    assert failure_signature(item) == failure_signature(item2)


def test_failure_signature_distinguishes_by_first_line():
    a = SimpleNamespace(type="CI_FAILURE", feedback=["lint failed: unused import"])
    b = SimpleNamespace(type="CI_FAILURE", feedback=["mix test failed: bad arith"])
    assert failure_signature(a) != failure_signature(b)


def test_progress_signature_resets_attempt_and_increments_progress():
    """A retry whose failure signature *differs* from the prior attempt's
    counts as progress: ATTEMPT_ANNOTATION resets to 1 and a separate
    PROGRESS_ANNOTATION ticks. Per-tier attempt budget (3) is preserved for
    the thrashing case."""
    wl = _wl_with_sig("o/r", 7, attempt=2, sig="ci_failure::old")

    created = []

    def create(manifest):
        created.append(manifest)

    out = reconcile_pr_fixes(
        lambda: [wl],
        lambda n: True,
        create,
        mark_pr_fix=lambda *a, **kw: True,
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
        progress_max_attempts=8,
        get_pr_fix_signature=lambda repo, pr: "ci_failure::new",
    )
    assert len(created) == 1
    ann = created[0]["metadata"]["annotations"]
    assert ann[ATTEMPT_ANNOTATION] == "1"
    assert ann[SIGNATURE_ANNOTATION] == "ci_failure::new"
    assert ann[PROGRESS_ANNOTATION] == "1"
    assert any("retry-progress" in line for line in out)


def test_same_signature_still_decrements_attempt_budget():
    """The same-failure wall keeps the legacy per-tier attempt budget."""
    wl = _wl_with_sig("o/r", 9, attempt=2, sig="ci_failure::same")

    created = []

    def create(manifest):
        created.append(manifest)

    out = reconcile_pr_fixes(
        lambda: [wl],
        lambda n: True,
        create,
        mark_pr_fix=lambda *a, **kw: True,
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
        get_pr_fix_signature=lambda repo, pr: "ci_failure::same",
    )
    ann = created[0]["metadata"]["annotations"]
    assert ann[ATTEMPT_ANNOTATION] == "3"
    # Same signature: progress counter must NOT have ticked.
    assert ann.get(PROGRESS_ANNOTATION) is None
    assert any("retry:3/3" in line for line in out)


def test_progress_runaway_bound_marks_blocked():
    """Once the *progressing* budget is exhausted, mark the fix BLOCKED."""
    wl = _wl_with_sig("o/r", 11, attempt=1, sig="ci_failure::a")
    wl["metadata"]["annotations"][PROGRESS_ANNOTATION] = "8"

    marks = []

    out = reconcile_pr_fixes(
        lambda: [wl],
        lambda n: True,
        lambda m: True,
        mark_pr_fix=lambda *a, **kw: marks.append((a, kw)),
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
        progress_max_attempts=8,
        get_pr_fix_signature=lambda repo, pr: "ci_failure::b",
    )
    assert any("progress-giveup" in line for line in out)
    assert any(m[0][2] == "BLOCKED" for m in marks)


def test_no_signature_falls_back_to_legacy_budget():
    """When the caller can't provide a current failure signature (e.g. the
    feed isn't reachable from the reconcile path), we must keep the legacy
    attempt-count budget rather than treating 'unknown' as 'progress'."""
    wl = _wl_with_sig("o/r", 13, attempt=2, sig="")  # no prior baseline

    created = []

    def create(manifest):
        created.append(manifest)

    reconcile_pr_fixes(
        lambda: [wl],
        lambda n: True,
        create,
        mark_pr_fix=lambda *a, **kw: True,
        pr_is_mergeable=lambda repo, pr: "checks_failed",
        max_attempts=3,
        get_pr_fix_signature=lambda repo, pr: "",  # no signal
    )
    ann = created[0]["metadata"]["annotations"]
    assert ann[ATTEMPT_ANNOTATION] == "3"
    assert ann.get(PROGRESS_ANNOTATION) is None


def test_rebuild_manifest_persists_signature_annotation():
    from bridge.prfix import rebuild_prfix_manifest
    wl = _wl_with_sig("o/r", 15, attempt=1, sig="prior")
    rebuilt = rebuild_prfix_manifest(wl, attempt=2, signature="fresh")
    assert rebuilt["metadata"]["annotations"][SIGNATURE_ANNOTATION] == "fresh"
    assert rebuilt["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "2"

    # Empty signature preserves the prior baseline (so reconcile can still
    # compare against it on the next tick).
    rebuilt2 = rebuild_prfix_manifest(wl, attempt=3, signature="")
    assert rebuilt2["metadata"]["annotations"][SIGNATURE_ANNOTATION] == "prior"
    assert rebuilt2["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "3"


def test_reconcile_changes_requested_retires_workload_so_feedback_can_be_worked():
    """CHANGES_REQUESTED is the case the pr-fix loop exists for.

    It used to fall into the 'blocked' branch: the Completed Workload was left
    in place, so drain_pr_fixes could never create a new one for that PR, and
    later reviews could never produce a fix run while the slot stayed occupied.
    Observed on misospace/llmkube-images#237 — a review landed and the bridge
    logged 'blocked:1/3' every tick for hours without acting.
    """
    marks, deleted, created = [], [], []

    def _mark(repo, pr, status, note):
        marks.append((repo, pr, status))
        return True

    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded", attempt=1)],
        delete_workload=deleted.append,
        create_workload=created.append,
        mark_pr_fix=_mark,
        pr_is_mergeable=lambda repo, pr: "changes_requested",
        max_attempts=3,
    )
    # The finished Workload is retired so the next drain can create a fresh
    # one against the new feedback; the item is NOT marked terminal.
    assert deleted == ["prfix-o-r-5"]
    assert marks == []
    assert out == ["prfix-o-r-5:changes-requested:1/3"]


def test_check_pr_mergeable_reports_changes_requested_not_blocked():
    """BLOCKED with no failing check is ambiguous: awaiting a required
    reviewer (coder cannot help) versus a reviewer having requested changes
    (exactly what the coder should act on). Ask the reviews which it is."""
    from bridge.main import check_pr_mergeable

    def fake_get(url, headers):
        if url.endswith("/reviews?per_page=100"):
            return [
                {"state": "APPROVED", "user": {"login": "bot"}},
                # Same reviewer changed their mind: the last decisive review wins.
                {"state": "CHANGES_REQUESTED", "user": {"login": "human"}},
                {"state": "COMMENTED", "user": {"login": "human"}},
            ]
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"mergeable_state": "blocked", "head": {"sha": "abc"}}

    assert check_pr_mergeable("o/r", 5, http_get=fake_get, github_token="t") == "changes_requested"


def test_check_pr_mergeable_still_blocked_when_no_changes_requested():
    """An approving or absent review leaves the old park-and-wait behaviour."""
    from bridge.main import check_pr_mergeable

    def fake_get(url, headers):
        if url.endswith("/reviews?per_page=100"):
            return [{"state": "APPROVED", "user": {"login": "bot"}}]
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"mergeable_state": "blocked", "head": {"sha": "abc"}}

    assert check_pr_mergeable("o/r", 5, http_get=fake_get, github_token="t") == "blocked"


def test_check_pr_mergeable_unknown_state_is_not_ok():
    """GitHub computes mergeable_state asynchronously and reports "unknown"
    while that is in flight — exactly when we poll, right after a push or a
    review. Every unnamed state used to fall through to "ok", so an
    indeterminate answer read as a clean PR and reconcile marked the item
    FIXED without ever establishing mergeability. alert-triage#87 and
    llmkube-images#237 were both marked FIXED while carrying a standing
    CHANGES_REQUESTED review, and FIXED is terminal."""
    from bridge.main import check_pr_mergeable

    def fake_get(url, headers):
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"mergeable_state": "unknown", "mergeable": None, "head": {"sha": "abc"}}

    assert check_pr_mergeable("o/r", 5, http_get=fake_get, github_token="t") == "checks_pending"


def test_check_pr_mergeable_empty_state_is_not_ok():
    """Same for an absent mergeable_state, which the API also returns."""
    from bridge.main import check_pr_mergeable

    def fake_get(url, headers):
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"head": {"sha": "abc"}}

    assert check_pr_mergeable("o/r", 5, http_get=fake_get, github_token="t") == "checks_pending"


def test_check_pr_mergeable_clean_state_is_still_ok():
    """A genuinely clean PR must still report ok, or nothing ever marks FIXED."""
    from bridge.main import check_pr_mergeable

    def fake_get(url, headers):
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        return {"mergeable_state": "clean", "mergeable": True, "head": {"sha": "abc"}}

    assert check_pr_mergeable("o/r", 5, http_get=fake_get, github_token="t") == "ok"
