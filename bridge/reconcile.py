"""Reconcile stranded in-progress issues whose Workload no longer exists.

An issue can end up permanently stranded in `status/in-progress` with no
Workload backing it (e.g. after terminal-workload GC or manual deletion).
This module resets such issues to `status/ready` so the next tick re-claims
them, provided they have no open PR (an open PR means human-side review work).
"""

import logging
from typing import Callable

from bridge.claim import DispatchClient

logger = logging.getLogger("bridge.reconcile")


def _workload_name_for_issue(issue_number: int) -> str:
    """Return the Workload name for a given issue number."""
    return f"issue-{issue_number}"


def reconcile_stranded_issues(
    dispatch: DispatchClient,
    agent_name: str,
    workload_names: set[str],
    check_open_pr: Callable[[int], bool],
) -> list[str]:
    """Reset in-progress issues with no backing Workload to ready.

    Lists all ``status/in-progress`` issues claimed for *agent_name*.  For each,
    checks whether a live Workload exists (by name).  If none exists **and** the
    issue has no open PR, resets it to ``status/ready`` via the dispatch API so
    the next tick re-claims it.

    Returns a list of human-readable log lines describing each action taken.
    """
    results: list[str] = []

    # Fetch claimed (in-progress) issues for this agent
    claimed = dispatch.list_claimed(agent_name)
    if not claimed:
        return results

    for item in claimed:
        issue_number = item.get("number")
        if issue_number is None:
            continue

        wl_name = _workload_name_for_issue(issue_number)

        # If a Workload still exists, nothing to do.
        if wl_name in workload_names:
            continue

        # If there's an open PR, leave it alone (human-side review work).
        if check_open_pr(issue_number):
            logger.info(
                "issue %d: in-progress with no Workload but has open PR — skipping",
                issue_number,
            )
            continue

        # Reset to ready so the next tick can re-claim it.
        try:
            dispatch.update_status(issue_number, "status/ready")
            msg = f"issue {issue_number}: reset to ready (no Workload, no open PR)"
            logger.info(msg)
            results.append(msg)
        except Exception:
            logger.exception(
                "issue %d: failed to reset to ready", issue_number
            )

    return results
