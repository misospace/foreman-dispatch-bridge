"""Unit tests for issue #46: strand recovery of in-progress issue claims.

Three acceptance cases:
1. A Failed Workload pruned by GC leaves its issue claimable (status/ready).
2. An in-progress issue whose Workload was deleted manually is reset to
   `ready` on the next tick (when it has no open PR).
3. An in-progress issue with an open PR is left alone.
"""
from bridge.reconcile import reconcile_in_progress_issues


def test_no_workload_resets_to_ready():
    """GC-pruned or manually-deleted Workload => issue must be reset to ready."""
    reset_calls = []
    items = [
        {"repoFullName": "foreman-dispatch-bridge", "issueNumber": 42},
    ]

    def list_in_progress():
        return items

    def reset_to_ready(item):
        reset_calls.append(item)
        return True

    def has_live_workload(repo, n):
        return False  # no Workload -> stranded

    out = reconcile_in_progress_issues(
        agent_name="foreman/coder",
        list_in_progress=list_in_progress,
        reset_to_ready=reset_to_ready,
        has_live_workload=has_live_workload,
    )
    assert reset_calls == items
    assert out == ["foreman-dispatch-bridge#42:reset:ready"]


def test_live_workload_is_left_alone():
    """A live Workload means the claim is NOT stranded; no reset."""
    reset_calls = []
    items = [{"repoFullName": "dispatch", "issueNumber": 7}]

    def list_in_progress():
        return items

    def reset_to_ready(item):
        reset_calls.append(item)
        return True

    def has_live_workload(repo, n):
        return True  # live Workload -> not stranded

    out = reconcile_in_progress_issues(
        agent_name="foreman/coder",
        list_in_progress=list_in_progress,
        reset_to_ready=reset_to_ready,
        has_live_workload=has_live_workload,
    )
    assert reset_calls == []
    assert out == ["dispatch#7:live:skip"]


def test_open_pr_is_left_alone():
    """An in-progress issue with an open PR is in human review; do NOT re-run."""
    reset_calls = []
    items = [
        {"repoFullName": "llmkube-images", "issueNumber": 99, "openPR": True},
    ]

    def list_in_progress():
        return items

    def reset_to_ready(item):
        reset_calls.append(item)
        return True

    def has_live_workload(repo, n):
        return False  # no Workload, but open PR still wins

    out = reconcile_in_progress_issues(
        agent_name="foreman/coder",
        list_in_progress=list_in_progress,
        reset_to_ready=reset_to_ready,
        has_live_workload=has_live_workload,
        has_open_pr=lambda it: bool(it.get("openPR")),
    )
    assert reset_calls == []
    assert out == ["llmkube-images#99:in-review:skip"]


def test_reset_failure_does_not_abort_pass():
    """One failing reset must not block other issues from being rescued."""
    reset_calls = []
    items = [
        {"repoFullName": "repo-a", "issueNumber": 1},
        {"repoFullName": "repo-b", "issueNumber": 2},
        {"repoFullName": "repo-c", "issueNumber": 3},
    ]

    def list_in_progress():
        return items

    def reset_to_ready(item):
        reset_calls.append(item["repoFullName"])
        return item["repoFullName"] != "repo-b"  # repo-b's reset fails

    def has_live_workload(repo, n):
        return False

    out = reconcile_in_progress_issues(
        agent_name="foreman/coder",
        list_in_progress=list_in_progress,
        reset_to_ready=reset_to_ready,
        has_live_workload=has_live_workload,
    )
    assert reset_calls == ["repo-a", "repo-b", "repo-c"]
    assert out == [
        "repo-a#1:reset:ready",
        "repo-b#2:reset:failed",
        "repo-c#3:reset:ready",
    ]


def test_exception_on_one_issue_does_not_abort_pass():
    """A thrown exception for one issue must not stop the others."""
    items = [
        {"repoFullName": "ok-repo", "issueNumber": 10},
        {"repoFullName": "boom-repo", "issueNumber": 11},
    ]

    def list_in_progress():
        return items

    def reset_to_ready(item):
        if item["repoFullName"] == "boom-repo":
            raise RuntimeError("dispatch 503")
        return True

    def has_live_workload(repo, n):
        return False

    out = reconcile_in_progress_issues(
        agent_name="foreman/coder",
        list_in_progress=list_in_progress,
        reset_to_ready=reset_to_ready,
        has_live_workload=has_live_workload,
    )
    assert out[0] == "ok-repo#10:reset:ready"
    assert out[1].startswith("boom-repo#11:error:")
