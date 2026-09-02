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
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent, _reason = updated[0]
        assert status == "in-review"
        assert agent == "foreman-coder"
        assert item["issueId"] == "iss-42"
        assert item["repoFullName"] == "misospace/foreman-dispatch-bridge"
        assert item["number"] == 42
        assert any("in-review" in line for line in out)

    def test_no_pr_without_verdict_parks(self):
        """A Completed Workload with no PR and no verdict → parked as blocked
        (absent signal is not GO), reported to dispatch (#213)."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("review", pr_url=None, verdict=None)],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "blocked"
        # dispatch rejects a park with no reason (400), so the reason must be
        # carried, not left for a human to reconstruct.
        assert updated[0][3].strip()
        assert "absent" in updated[0][3]
        assert any("blocked" in line for line in out)

    def test_transitions_already_resolved_workload_to_done(self):
        """A Completed Workload with reason=AllAlreadyResolved and no PR →
        status/done, releasing the in-progress claim (#169)."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl_already_resolved("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task("issue-fix", pr_url=None)],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent, _reason = updated[0]
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
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "in-review"
        assert any("in-review" in line for line in out)

    def test_already_resolved_update_status_failure_is_caught(self):
        """A failed update_status on the already-resolved path is logged, not fatal."""
        def fail_update(item, status, agent, reason=""):
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
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
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
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert updated == []
        assert any("skip" in line for line in out)

    def test_multiple_workloads_mixed(self):
        """Process each independently: one with PR → transition, one without → skip."""
        updated = []
        wls = [_wl("wl-a-b-42"), _wl("wl-a-b-43", issue_id="iss-43")]
        tasks_map = {
            "wl-a-b-42": [_task("review", pr_url="https://github.com/a/b/pull/42")],
            "wl-a-b-43": [_task("review", pr_url=None, verdict=None)],
        }
        out = transition_to_in_review(
            list_workloads=lambda: wls,
            list_workload_tasks=lambda name: tasks_map.get(name, []),
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 2
        statuses = {u[0]["issueId"]: u[1] for u in updated}
        assert statuses["iss-42"] == "in-review"
        assert statuses["iss-43"] == "blocked"
        assert any("42" in line and "in-review" in line for line in out)
        assert any("43" in line and "blocked" in line for line in out)

    def test_update_status_failure_is_caught(self):
        """A failed update_status call is logged, not fatal."""
        def fail_update(item, status, agent, reason=""):
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
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "in-review"


# ── No-PR verdict routing (#213) ───────────────────────────────────────────


def _task_with_signal(verdict=None, summary=None, extra=None):
    """A coder task carrying status.verdict / status.result.summary / extra."""
    status = {"phase": "Succeeded"}
    if verdict is not None:
        status["verdict"] = verdict
    result = {}
    if summary is not None:
        result["summary"] = summary
    if extra is not None:
        result["extra"] = extra
    if result:
        status["result"] = result
    return {"spec": {"kind": "issue-fix"}, "status": status}


class TestNoPrVerdictRouting:
    def test_incomplete_no_pr_parks_and_reports(self):
        """No PR + INCOMPLETE verdict → issue parked as blocked, reported."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [
                _task_with_signal(verdict="INCOMPLETE", summary="tests failing")
            ],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent, _reason = updated[0]
        assert status == "blocked"
        assert agent == "foreman-coder"
        assert item["issueId"] == "iss-42"
        assert item["number"] == 42
        assert any("blocked" in line for line in out)
        assert any("INCOMPLETE" in line for line in out)
        assert any("tests failing" in line for line in out)

    def test_go_no_pr_parks_for_human_with_commit_info(self):
        """No PR + GO verdict → parked for a human with reason that records
        commitSHA/branch. Resting state, not a silent anomaly."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [
                _task_with_signal(
                    verdict="GO",
                    summary="change complete",
                    extra={"commitSHA": "abc123", "branch": "fix-42"},
                )
            ],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        item, status, agent, reason = updated[0]
        assert status == "backlog"
        assert "abc123" in reason
        assert "fix-42" in reason
        assert any("parked" in line for line in out)
        assert any("abc123" in line for line in out)
        assert any("fix-42" in line for line in out)

    def test_go_no_pr_without_commit_info_still_parks(self):
        """GO with no PR and no commitSHA/branch → still parked for a human."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task_with_signal(verdict="GO")],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "backlog"
        assert any("parked" in line for line in out)

    def test_go_no_pr_park_posts_label_and_comment_once(self):
        """First pass with dispatch: applies needs-human label and posts
        exactly one comment, then transitions status to backlog."""
        updated = []
        labels = []
        comments = []

        class FakeDispatch:
            def issue_is_parked(self, repo, num, label):
                return False

            def add_label(self, item, label):
                labels.append((item, label))

            def post_comment(self, item, body):
                comments.append((item, body))

        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [
                _task_with_signal(
                    verdict="GO",
                    summary="change complete",
                    extra={"commitSHA": "abc123", "branch": "fix-42"},
                )
            ],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
            dispatch=FakeDispatch(),
        )
        assert len(updated) == 1
        assert updated[0][1] == "backlog"
        assert labels == [
            (
                {
                    "issueId": "iss-42",
                    "repoFullName": "misospace/foreman-dispatch-bridge",
                    "number": 42,
                },
                "needs-human",
            )
        ]
        assert len(comments) == 1
        assert "abc123" in comments[0][1]
        assert "fix-42" in comments[0][1]
        assert any("parked" in line for line in out)

    def test_go_no_pr_second_pass_is_no_op_when_already_parked(self):
        """Second pass over the same parked state: dedupe via issue_is_parked
        skips the comment and the status flip, so no duplicate comments are
        posted and the issue does not re-enter the coder pipeline."""
        updated = []
        labels = []
        comments = []

        class FakeDispatch:
            def __init__(self):
                self._parked = False

            def issue_is_parked(self, repo, num, label):
                return self._parked

            def add_label(self, item, label):
                self._parked = True
                labels.append((item, label))

            def post_comment(self, item, body):
                comments.append((item, body))

        def wl_factory():
            return [_wl("wl-a-b-42")]

        def tasks_factory(name):
            return [
                _task_with_signal(
                    verdict="GO",
                    summary="change complete",
                    extra={"commitSHA": "abc123", "branch": "fix-42"},
                )
            ]

        def update_factory(item, status, agent, reason=""):
            updated.append((item, status, agent, reason))

        # First pass: parks the issue.
        dispatch = FakeDispatch()
        transition_to_in_review(
            list_workloads=wl_factory,
            list_workload_tasks=tasks_factory,
            update_status=update_factory,
            agent_name="foreman-coder",
            dispatch=dispatch,
        )
        assert len(updated) == 1
        assert len(labels) == 1
        assert len(comments) == 1

        # Second pass over the same state: must be a no-op for the
        # side-effects that would post duplicates.
        out2 = transition_to_in_review(
            list_workloads=wl_factory,
            list_workload_tasks=tasks_factory,
            update_status=update_factory,
            agent_name="foreman-coder",
            dispatch=dispatch,
        )
        # Status is not flipped again on the second pass.
        assert len(updated) == 1
        # No additional label or comment.
        assert len(labels) == 1
        assert len(comments) == 1
        # Result line still records the parked state, with a replay marker.
        assert any("replay" in line for line in out2)

    def test_all_already_resolved_still_goes_to_done(self):
        """AllAlreadyResolved keeps its done path even with a verdict present."""
        updated = []
        wl = _wl_already_resolved("wl-a-b-42")
        out = transition_to_in_review(
            list_workloads=lambda: [wl],
            list_workload_tasks=lambda name: [
                _task_with_signal(verdict="INCOMPLETE", summary="no fix attempted")
            ],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "done"
        assert any("done" in line for line in out)

    def test_raising_dispatch_call_still_returns_result_line(self):
        """Fails open: a raising update_status on the park path appends an
        error result line and does not abort the pass."""
        def fail_update(item, status, agent, reason=""):
            raise RuntimeError("API down")
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [
                _task_with_signal(verdict="INCOMPLETE", summary="tests failing")
            ],
            update_status=fail_update,
            agent_name="foreman-coder",
        )
        assert any("error" in line.lower() for line in out)
        assert any("API down" in line for line in out)

    def test_absent_verdict_parks(self):
        """No verdict on any task → treated as non-GO: parked, not GO."""
        updated = []
        out = transition_to_in_review(
            list_workloads=lambda: [_wl("wl-a-b-42")],
            list_workload_tasks=lambda name: [_task_with_signal()],
            update_status=lambda item, status, agent, reason="": updated.append((item, status, agent, reason)),
            agent_name="foreman-coder",
        )
        assert len(updated) == 1
        assert updated[0][1] == "blocked"
        assert any("blocked" in line for line in out)
