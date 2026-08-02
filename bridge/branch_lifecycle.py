"""Fix-branch lifecycle management for Foreman-dispatched branches.

Provides tooling to detect, track, and flag unmerged fix branches that have
exceeded a configurable staleness threshold. This addresses source-of-truth
confusion when accumulated fix branches exist on origin but are never merged
into main.

Policy
------
- Fix branches matching ``foreman/*/issue-*`` pattern are tracked.
- Branches older than *stale_days* (default 14) without a merge into the
  target branch are flagged as stale.
- The module exposes a CLI entry-point and programmatic API so it can be
  invoked from CI (scheduled workflow) or ad-hoc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Default staleness threshold in days.
DEFAULT_STALE_DAYS = 14

# Pattern matching Foreman fix branches:
#   origin/foreman/<owner-repo>/issue-<number>
FOREMAN_BRANCH_RE = re.compile(
    r"^origin/foreman/.+/issue-(\d+)$"
)


@dataclass(frozen=True)
class BranchInfo:
    """Immutable snapshot of a single fix branch's metadata."""

    name: str
    issue_number: int
    last_commit_date: Optional[datetime]
    merged: bool = False
    stale: bool = field(default=False, compare=False)

    def __repr__(self) -> str:
        status = "merged" if self.merged else ("stale" if self.stale else "active")
        return f"BranchInfo({self.name!r}, issue={self.issue_number}, {status})"


@dataclass
class BranchLifecycleReport:
    """Aggregate report of all tracked fix branches."""

    branches: list[BranchInfo] = field(default_factory=list)
    stale_count: int = 0
    merged_count: int = 0
    active_count: int = 0

    def add(self, info: BranchInfo) -> None:
        self.branches.append(info)
        if info.merged:
            self.merged_count += 1
        elif info.stale:
            self.stale_count += 1
        else:
            self.active_count += 1

    @property
    def total(self) -> int:
        return len(self.branches)


def is_foreman_branch(name: str) -> bool:
    """Return True if *name* matches the Foreman fix-branch pattern."""
    return FOREMAN_BRANCH_RE.match(name) is not None


def extract_issue_number(name: str) -> Optional[int]:
    """Extract the issue number from a Foreman branch name, or None."""
    m = FOREMAN_BRANCH_RE.match(name)
    if m:
        return int(m.group(1))
    return None


def evaluate_branches(
    branch_names: list[str],
    last_commit_dates: dict[str, Optional[datetime]],
    merged_branches: Optional[set[str]] = None,
    now: Optional[datetime] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> BranchLifecycleReport:
    """Evaluate a list of branch names against the lifecycle policy.

    Parameters
    ----------
    branch_names:
        Full branch refs (e.g. ``origin/foreman/repo/issue-33``).
    last_commit_dates:
        Mapping from branch name to its most recent commit datetime.
    merged_branches:
        Set of branch names that have been merged into the target branch.
    now:
        Reference time for staleness calculation (defaults to UTC now).
    stale_days:
        Number of days after which a non-merged branch is considered stale.

    Returns
    -------
    BranchLifecycleReport with all matching Foreman branches classified.
    """
    now = now or datetime.now(timezone.utc)
    merged = merged_branches or set()
    report = BranchLifecycleReport()

    for name in branch_names:
        if not is_foreman_branch(name):
            continue

        issue_num = extract_issue_number(name)
        if issue_num is None:
            continue

        commit_date = last_commit_dates.get(name)
        merged_flag = name in merged

        # Determine staleness: non-merged + older than threshold
        stale_flag = False
        if not merged_flag and commit_date is not None:
            age_seconds = (now - commit_date).total_seconds()
            stale_flag = age_seconds >= stale_days * 86400

        info = BranchInfo(
            name=name,
            issue_number=issue_num,
            last_commit_date=commit_date,
            merged=merged_flag,
            stale=stale_flag,
        )
        report.add(info)

    return report


def format_report(report: BranchLifecycleReport) -> str:
    """Human-readable summary of the lifecycle report."""
    lines = [f"Fix-branch lifecycle report ({report.total} branches tracked):"]
    lines.append(f"  Active : {report.active_count}")
    lines.append(f"  Stale  : {report.stale_count}")
    lines.append(f"  Merged : {report.merged_count}")

    if report.branches:
        lines.append("")
        for b in sorted(report.branches, key=lambda x: x.issue_number):
            flag = "MERGED" if b.merged else ("STALE" if b.stale else "OK")
            date_str = ""
            if b.last_commit_date is not None:
                date_str = f" (last: {b.last_commit_date:%Y-%m-%d})"
            lines.append(f"  [{flag}] {b.name}{date_str}")

    return "\n".join(lines)


def get_stale_branches(report: BranchLifecycleReport) -> list[BranchInfo]:
    """Return only the stale (non-merged, past threshold) branches."""
    return [b for b in report.branches if b.stale]


def get_unmerged_branches(report: BranchLifecycleReport) -> list[BranchInfo]:
    """Return all non-merged branches regardless of staleness."""
    return [b for b in report.branches if not b.merged]
