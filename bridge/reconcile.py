"""Reconcile `status/in-progress` issue claims against live Workloads.

Issue claims can be stranded when the Workload backing them is removed
(GC after PRUNE_FAILED_AFTER_HOURS, or a manual `kubectl delete workload`)
without the issue's status being touched. The bridge is the only component
that sees both sides, so this pass resets stranded claims back to
`status/ready` so the next tick re-claims them.

Strand detection: an issue is `status/in-progress` and claimed for this
agent, but no live Workload exists for it AND it has no open PR (an open
PR means the work is human-side and not re-runnable by the bridge).
"""
from typing import Callable, List


def reconcile_in_progress_issues(
    agent_name: str,
    list_in_progress: Callable[[], List[dict]],
    reset_to_ready: Callable[[dict], bool],
    has_live_workload: Callable[[str, int], bool],
    has_open_pr: Callable[[dict], bool] = lambda _item: False,
) -> List[str]:
    """Reset stranded in-progress issues back to ready.

    The four callables are dependency-injected so this module is unit-testable
    without the dispatch / kubernetes clients. ``list_in_progress`` returns
    items with at least ``repoFullName`` and ``issueNumber``; ``has_open_pr``
    must return False (or be omitted) for an issue to be considered stranded.

    Per-issue isolation: a reset failure on one issue must not abort the
    pass (others may be rescuable). Returns one log line per issue handled.
    """
    results = []
    for item in list_in_progress() or []:
        if not isinstance(item, dict):
            continue
        repo = item.get("repoFullName") or item.get("repo")
        try:
            issue_number = int(item.get("issueNumber") or item.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if not repo or not issue_number:
            continue
        try:
            # Open PR => human-side review; not the bridge's job to re-run.
            if has_open_pr(item):
                results.append(f"{repo}#{issue_number}:in-review:skip")
                continue
            # Live Workload exists => the claim is not stranded; nothing to do.
            if has_live_workload(repo, issue_number):
                results.append(f"{repo}#{issue_number}:live:skip")
                continue
            # Stranded: reset so the next tick re-claims it.
            if reset_to_ready(item):
                results.append(f"{repo}#{issue_number}:reset:ready")
            else:
                results.append(f"{repo}#{issue_number}:reset:failed")
        except Exception as e:
            results.append(f"{repo}#{issue_number}:error:{e}")
    return results
