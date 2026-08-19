"""Tests for bridge.review_transition — flip completed Workloads to in-review."""

from bridge.review_transition import transition_to_in_review


# ── Helpers ────────────────────────────────────────────────────────────────

def _wl(name, phase="Completed", attempt="2", issue_id="iss-42",
        repo="misospace/foreman-dispatch-bridge", issues=None):
    if issues is None:
        issues = [42]
    return {
        "metadata": {
            "name": name,
            "annotations": {
                "foreman.llmkube.dev/attempt": attempt,
                "foreman.llmkube.dev/issue-id": issue_id,
            },
        },
        "spec": {"repo": repo, "issues": issues},
        "status": {"phase": phase},
    }


def _wl_already_resolved(name, **kwargs):
    """A Completed Workload whose task carries a Completed condition with
    reason=AllAlreadyResolved — no fix attempted, so no PR by definition."""
    wl = _wl(name, **kwargs)
    wl["status"]["taskStatuses"] = [
        {
            "name": "code-42",
            "conditions": [
                {"type": "Completed", "reason": "AllAlreadyResolved",
                 "message": "1 issue(s) already resolved at run time (no fix attempted)"},
            ],
        }
    ]
    return wl


def _task(kind, verdict="GO", pr_url=None):
    status = {"phase": "Succeeded", "verdict": verdict}
    result = {}
    extra = {}
    if pr_url:
        extra["pullRequestURL"] = pr_url
    if extra:
        result["extra"] = extra
    if verdict:
        result["verdict"] = verdict
    if result:
        status["result"] = result
    return {"spec": {"kind": kind}, "status": status}


# ── Tests ──────────────────────────────────────────────────────────────────


class TestTransitionToInReview:
    def test_transitions_completed_workload_with_pr(self):
        """A Completed Workload whose review task opened a PR → in-review."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("review", pr_url="https://github.com/a/b/pull/42")],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent = updated[0]
        assert status == "in-review"
        assert agent == "foreman-coder"
        assert item["issueId"] == "iss-42"
        assert item["repoFullName"] == "misospace/foreman-dispatch-bridge"
        assert item["number"] == 42
        assert any("in-review" in line for line in out)

    def test_skips_workload_without_pr(self):
        """A Completed Workload whose review didn't open a PR → skip."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("review", pr_url=None)],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert updated == []
        assert any("skip" in line for line in out)

    def test_transitions_already_resolved_workload_to_done(self):
        """A Completed Workload with reason=AllAlreadyResolved and no PR →
        status/done, releasing the in-progress claim (#169)."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl_already_resolved("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("issue-fix", pr_url=None)],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent = updated[0]
        assert status == "done"
        assert agent == "foreman-coder"
        assert item["issueId"] == "iss-42"
        assert item["repoFullName"] == "misospace/foreman-dispatch-bridge"
        assert item["number"] == 42
        assert any("done" in line for line in out)

    def test_already_resolved_with_pr_still_goes_in_review(self):
        """The PR path wins: a Workload that has a PR transitions to
        in-review even if a task also carries the AllAlreadyResolved reason."""
        updated = []
        wl = _wl_already_resolved("wl-a-b-42")
        out = transition_to_in_review(
            list_workloads=lambda: [wl],
            list_workload_tasks=lambda name: [_task("review", pr_url="https://github.com/a/b/pull/42")],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "in-review"
        assert any("in-review" in line for line in out)

    def test_already_resolved_update_status_failure_is_caught(self):
        """A failed update_status on the already-resolved path is logged, not fatal."""
        def fail_update(item, status, agent):
            raise RuntimeError("API down")
        out = transition_to_in_review(
            list_workloads=lambda: [_wl_already_resolved("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("issue-fix", pr_url=None)],
            update_status=fail_update,
            agent_name="foreman-coder",
        )
        assert any("error" in line.lower() for line in out)

    def test_skips_non_completed_workload(self):
        """A Dispatched (in-flight) Workload → skip (not done yet)."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42", phase="Dispatched")],
            list_workload_tasks=lambda name: [_task("review", pr_url="https://github.com/a/b/pull/42")],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert updated == []
        assert any("skip" in line for line in out)

    def test_skips_prfix_workload(self):
        """PR-fix Workloads (created-by=dispatch-bridge-prfix) are not issue
        workloads — the pr-fix queue handles their lifecycle."""
        updated = []
        wl = _wl("prfix-a-b-42")
        wl["metadata"]["labels"] = {"created-by": "dispatch-bridge-prfix"}
        out = transition_to_in_review(
            list_workloads=lambda: [wl],
            list_workload_tasks=lambda name: [_task("review", pr_url="https://github.com/a/b/pull/42")],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert updated == []
        assert any("skip" in line for line in out)

    def test_multiple_workloads_mixed(self):
        """Process each independently: one with PR → transition, one without → skip."""
        updated = []
        wls = [_wl("wl-a-b-42"), _wl("wl-a-b-43")]
        tasks_map = {
            "wl-a-b-42": [_task("review", pr_url="https://github.com/a/b/pull/42")],
            "wl-a-b-43": [_task("review", pr_url=None)],
        }
        out = transition_to_in_review(
            list_workloads=lambda: wls,
            list_workload_tasks=lambda name: tasks_map.get(name, []),
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "in-review"
        assert any("42" in line and "in-review" in line for line in out)
        assert any("43" in line and "skip" in line for line in out)

    def test_update_status_failure_is_caught(self):
        """A failed update_status call is logged, not fatal."""
        def fail_update(item, status, agent):
            raise RuntimeError("API down")
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("review", pr_url="https://github.com/a/b/pull/42")],
            update_status=fail_update,
            agent_name="foreman-coder",
        )
        assert any("error" in line.lower() for line in out)

    def test_no_workloads(self):
        """Empty cluster → nothing to do."""
        out = transition_to_in_review(
            list_workloads=lambda: [],
            list_workload_tasks=lambda name: [],
            update_status=lambda *a: None,
            agent_name="foreman-coder",
        )
        assert out == []

    def test_reads_pr_from_code_task_if_review_missing(self):
        """Some pipelines (ad-hoc dispatch) are code-only with no review step.
        The PR URL can also appear on the code task's result.extra.pullRequestURL."""
        updated = []
        transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("issue-fix", pr_url="https://github.com/a/b/pull/42")],
            update_status=lambda item, status, agent: updated.append((item, status, agent)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "in-review"
