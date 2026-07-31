"""Reconcile stranded in-progress issues whose Workload no longer exists.

An issue can end up permanently stranded in `status/in-progress` with no
Workload backing it (e.g. after terminal-workload GC or manual deletion).
This module resets such issues to `status/ready` so the next tick re-claims
them, provided they have no open PR (an open PR means human-side review work).
"""

import logging

from bridge.claim import DispatchClient
from bridge.models import ClaimedItem
from bridge.workload import workload_name

logger = logging.getLogger("bridge.reconcile")


def reconcile_stranded_issues(
    dispatch: DispatchClient,
    agent_name: str,
    workload_names: set[str],
) -> list[str]:
    """Reset in-progress issues with no backing Workload to ready.

    Lists all ``status/in-progress`` issues claimed for *agent_name*.  For each,
    checks whether a live Workload exists (by deterministic name
    ``wl-<owner>-<repo>-<number>``).  If none exists **and** the item's
    ``hasOpenPr`` field is false, resets it to ``status/ready`` via the
    dispatch API with full identity so the next tick re-claims it.

    Returns a list of human-readable log lines describing each action taken.
    """
    results: list[str] = []

    claimed = dispatch.list_claimed(agent_name)
    if not claimed:
        return results

    for item in claimed:
        issue_number = item.get("number")
        if issue_number is None:
            continue

        repo = item.get("repoFullName") or ""
        ci = ClaimedItem(repo=repo, issue_number=int(issue_number), intent="", lane="")
        wl_name = workload_name(ci)

        if wl_name in workload_names:
            continue

        if item.get("hasOpenPr"):
            logger.info(
                "issue %d: in-progress with no Workload but has open PR — skipping",
                issue_number,
            )
            continue

        try:
            dispatch.update_status(item, "ready", agent_name)
            msg = f"issue {issue_number}: reset to ready (no Workload, no open PR)"
            logger.info(msg)
            results.append(msg)
        except Exception:
            logger.exception(
                "issue %d: failed to reset to ready", issue_number
            )

    return results
