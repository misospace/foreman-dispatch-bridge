"""Tests for bridge.branch_lifecycle — fix-branch lifecycle tracking."""

from datetime import datetime, timedelta, timezone

import pytest

from bridge.branch_lifecycle import (
    DEFAULT_STALE_DAYS,
    BranchInfo,
    BranchLifecycleReport,
    evaluate_branches,
    extract_issue_number,
    format_report,
    get_stale_branches,
    get_unmerged_branches,
    is_foreman_branch,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────

def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# ── is_foreman_branch / extract_issue_number ────────────────────────────

class TestIsForemanBranch:
    def test_matches_standard(self):
        assert is_foreman_branch("origin/foreman/repo/issue-33") is True

    def test_matches_with_owner_repo(self):
        assert is_foreman_branch("origin/foreman/wl-misospace-foreman-dispatch-bridge-33-code-33/issue-33") is True

    def test_rejects_main(self):
        assert is_foreman_branch("origin/main") is False

    def test_rejects_local_branch(self):
        assert is_foreman_branch("feature/something") is False

    def test_rejects_non_issue_branch(self):
        assert is_foreman_branch("origin/foreman/repo/chore-123") is False


class TestExtractIssueNumber:
    def test_extracts_number(self):
        assert extract_issue_number("origin/foreman/repo/issue-46") == 46

    def test_returns_none_for_non_match(self):
        assert extract_issue_number("origin/main") is None

    def test_large_numbers(self):
        assert extract_issue_number("origin/foreman/repo/issue-9999") == 9999


# ── evaluate_branches ───────────────────────────────────────────────────

class TestEvaluateBranches:
    def test_empty_input(self):
        report = evaluate_branches([], {}, now=NOW)
        assert report.total == 0

    def test_skips_non_foreman_branches(self):
        report = evaluate_branches(
            ["origin/main", "feature/foo"],
            {"origin/main": NOW},
            now=NOW,
        )
        assert report.total == 0

    def test_active_branch(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(5)},
            now=NOW,
        )
        assert report.total == 1
        assert report.active_count == 1
        assert report.stale_count == 0

    def test_stale_branch(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(20)},
            now=NOW,
        )
        assert report.total == 1
        assert report.stale_count == 1

    def test_merged_branch_not_stale(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(20)},
            merged_branches={"origin/foreman/repo/issue-33"},
            now=NOW,
        )
        assert report.total == 1
        assert report.merged_count == 1
        assert report.stale_count == 0

    def test_no_commit_date_not_stale(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {},  # no date info
            now=NOW,
        )
        assert report.total == 1
        assert report.active_count == 1

    def test_boundary_exactly_stale_days(self):
        """Branch exactly at stale_days boundary is considered stale."""
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(DEFAULT_STALE_DAYS)},
            now=NOW,
        )
        assert report.stale_count == 1

    def test_boundary_one_second_before_stale(self):
        """Branch one second before the threshold is still active."""
        just_before = NOW - timedelta(days=DEFAULT_STALE_DAYS, seconds=-1)
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": just_before},
            now=NOW,
        )
        assert report.active_count == 1

    def test_multiple_branches_mixed(self):
        branches = [
            "origin/foreman/repo/issue-33",
            "origin/foreman/repo/issue-34",
            "origin/foreman/repo/issue-35",
            "origin/main",  # non-foreman, skipped
        ]
        dates = {
            "origin/foreman/repo/issue-33": _days_ago(20),  # stale
            "origin/foreman/repo/issue-34": _days_ago(5),   # active
            "origin/foreman/repo/issue-35": _days_ago(10),  # active
        }
        report = evaluate_branches(branches, dates, now=NOW)
        assert report.total == 3
        assert report.stale_count == 1
        assert report.active_count == 2

    def test_custom_stale_days(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(10)},
            stale_days=7,
            now=NOW,
        )
        assert report.stale_count == 1


# ── get_stale_branches / get_unmerged_branches ──────────────────────────

class TestGetStaleBranches:
    def test_returns_only_stale(self):
        report = evaluate_branches(
            [
                "origin/foreman/repo/issue-33",
                "origin/foreman/repo/issue-34",
            ],
            {
                "origin/foreman/repo/issue-33": _days_ago(20),  # stale
                "origin/foreman/repo/issue-34": _days_ago(5),   # active
            },
            now=NOW,
        )
        stale = get_stale_branches(report)
        assert len(stale) == 1
        assert stale[0].name == "origin/foreman/repo/issue-33"

    def test_empty_when_none_stale(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(5)},
            now=NOW,
        )
        assert get_stale_branches(report) == []


class TestGetUnmergedBranches:
    def test_returns_all_non_merged(self):
        report = evaluate_branches(
            [
                "origin/foreman/repo/issue-33",
                "origin/foreman/repo/issue-34",
            ],
            {
                "origin/foreman/repo/issue-33": _days_ago(20),  # stale, unmerged
                "origin/foreman/repo/issue-34": _days_ago(5),   # active, unmerged
            },
            merged_branches={"origin/foreman/repo/issue-34"},
            now=NOW,
        )
        unmerged = get_unmerged_branches(report)
        assert len(unmerged) == 1
        assert unmerged[0].name == "origin/foreman/repo/issue-33"


# ── format_report ───────────────────────────────────────────────────────

class TestFormatReport:
    def test_includes_counts(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(20)},
            now=NOW,
        )
        text = format_report(report)
        assert "Stale" in text
        assert "1" in text

    def test_includes_branch_details(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(20)},
            now=NOW,
        )
        text = format_report(report)
        assert "issue-33" in text
        assert "STALE" in text

    def test_merged_flag(self):
        report = evaluate_branches(
            ["origin/foreman/repo/issue-33"],
            {"origin/foreman/repo/issue-33": _days_ago(20)},
            merged_branches={"origin/foreman/repo/issue-33"},
            now=NOW,
        )
        text = format_report(report)
        assert "MERGED" in text

    def test_empty_report(self):
        report = BranchLifecycleReport()
        text = format_report(report)
        assert "0 branches tracked" in text


# ── BranchInfo repr ─────────────────────────────────────────────────────

class TestBranchInfoRepr:
    def test_active_repr(self):
        info = BranchInfo(
            name="origin/foreman/repo/issue-33",
            issue_number=33,
            last_commit_date=_days_ago(5),
        )
        assert "active" in repr(info)

    def test_stale_repr(self):
        info = BranchInfo(
            name="origin/foreman/repo/issue-33",
            issue_number=33,
            last_commit_date=_days_ago(20),
            stale=True,
        )
        assert "stale" in repr(info)

    def test_merged_repr(self):
        info = BranchInfo(
            name="origin/foreman/repo/issue-33",
            issue_number=33,
            last_commit_date=_days_ago(20),
            merged=True,
        )
        assert "merged" in repr(info)
