"""Tests for bridge.reconcile — stranded in-progress issue recovery."""

from unittest.mock import MagicMock, patch

import pytest

from bridge.reconcile import reconcile_stranded_issues


class FakeDispatchClient:
    """Minimal DispatchClient stand-in for tests."""

    def __init__(self):
        self._claimed = []
        self._status_updates = []
        self._open_prs = {}  # issue_number -> bool

    def list_claimed(self, agent_name):
        return self._claimed

    def update_status(self, issue_number, status):
        self._status_updates.append((issue_number, status))

    def has_open_pr(self, issue_number):
        return self._open_prs.get(issue_number, False)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def dispatch():
    return FakeDispatchClient()


@pytest.fixture
def check_open_pr(dispatch):
    return dispatch.has_open_pr


# ── Tests ───────────────────────────────────────────────────────────────────

class TestReconcileStrandedIssues:
    """reconcile_stranded_issues resets in-progress issues with no Workload."""

    def test_resets_issue_with_no_workload_and_no_open_pr(
        self, dispatch, check_open_pr
    ):
        """An in-progress issue with no backing Workload and no open PR is
        reset to ready."""
        dispatch._claimed = [{"number": 42}]
        workload_names = {"issue-99"}  # issue-42 not present

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert len(results) == 1
        assert "issue 42" in results[0]
        assert "ready" in results[0]
        assert dispatch._status_updates == [(42, "status/ready")]

    def test_skips_issue_with_live_workload(
        self, dispatch, check_open_pr
    ):
        """An in-progress issue whose Workload still exists is left alone."""
        dispatch._claimed = [{"number": 42}]
        workload_names = {"issue-42"}  # Workload exists

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert results == []
        assert dispatch._status_updates == []

    def test_skips_issue_with_open_pr(
        self, dispatch, check_open_pr
    ):
        """An in-progress issue with an open PR is left alone (human-side review)."""
        dispatch._claimed = [{"number": 42}]
        dispatch._open_prs[42] = True
        workload_names = {}  # No Workload

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert results == []
        assert dispatch._status_updates == []

    def test_no_claimed_issues(self, dispatch, check_open_pr):
        """When there are no claimed issues, nothing happens."""
        dispatch._claimed = []
        workload_names = {}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert results == []

    def test_multiple_issues_mixed(self, dispatch, check_open_pr):
        """Only issues with no Workload and no open PR are reset."""
        dispatch._claimed = [
            {"number": 1},   # no WL, no PR → reset
            {"number": 2},   # has WL → skip
            {"number": 3},   # no WL, has PR → skip
            {"number": 4},   # no WL, no PR → reset
        ]
        dispatch._open_prs[3] = True
        workload_names = {"issue-2"}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert len(results) == 2
        assert any("issue 1" in r for r in results)
        assert any("issue 4" in r for r in results)
        assert dispatch._status_updates == [(1, "status/ready"), (4, "status/ready")]

    def test_update_status_failure_is_caught(
        self, dispatch, check_open_pr
    ):
        """A failed update_status call is logged but doesn't crash the tick."""
        dispatch._claimed = [{"number": 42}]
        workload_names = {}

        def fail_update(*a, **kw):
            raise RuntimeError("API down")

        dispatch.update_status = fail_update

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert results == []  # No success messages

    def test_missing_issue_number_skipped(
        self, dispatch, check_open_pr
    ):
        """A claimed item with no 'number' key is skipped."""
        dispatch._claimed = [{"title": "no number"}]
        workload_names = {}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names, check_open_pr)

        assert results == []
