"""Reconcile stranded claimed issues whose Workload no longer exists.

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


def release_stuck_claims(
    dispatch: DispatchClient,
    agent_name: str,
    workload_names: set[str],
) -> list[str]:
    """Release claims on ``status/ready`` issues that have no backing Workload.

    An issue can end up at status/ready while still carrying this agent's label —
    an unclaim that only reset the status, a groom, a manual edit. Dispatch serves
    it at the head of the queue (the queue keeps work claimed by the requesting
    agent), but the claim is refused as a conflict with itself, and ``claim_one``
    skips a failing candidate so the lane never starves. The issue is therefore
    served first, refused, and skipped on every tick, indefinitely: two p0s sat
    that way for 20 days while throughput looked healthy.

    Dispatch's own fix makes a self-claim idempotent. This is the second layer, so
    a stuck claim is released rather than silently skipped even when the two sides
    are on mismatched versions. An issue with a live Workload or an open PR is left
    alone — that is work in flight, not a stuck claim.

    Returns human-readable log lines describing each release.
    """
    results: list[str] = []

    for item in dispatch.list_claimed(agent_name, status="ready") or []:
        issue_number = item.get("number")
        if issue_number is None:
            continue

        ci = ClaimedItem(
            repo=item.get("repoFullName") or "",
            issue_number=int(issue_number),
            intent="",
            lane="",
            issue_id=str(item.get("issueId") or ""),
        )

        if workload_name(ci) in workload_names:
            continue

        if item.get("hasOpenPr"):
            continue

        try:
            dispatch.unclaim(ci, agent_name)
            msg = f"issue {issue_number}: released stuck claim (ready, no Workload, no open PR)"
            logger.info(msg)
            results.append(msg)
        except Exception:
            logger.exception("issue %d: failed to release stuck claim", issue_number)

    return results
