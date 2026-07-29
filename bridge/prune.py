from datetime import datetime, timezone
from typing import Callable, Optional

# Terminal phases eligible for garbage collection. A Completed Workload has
# already opened its PR (which lives on GitHub independently); a Failed one
# that is still Failed at prune time has been left alone by reconcile
# (retries exhausted / blocked), so both are tombstones once past their TTL.
COMPLETED_PHASE = "Completed"
FAILED_PHASE = "Failed"

ListWorkloads = Callable[[], list]      # () -> list of Workload manifests (dicts)
DeleteWorkload = Callable[[str], None]  # (name) -> None
ResetIssue = Callable[[int], bool]      # (issue_number) -> success


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse a Foreman RFC3339 timestamp (trailing 'Z') into an aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def terminal_since(wl: dict) -> Optional[datetime]:
    """Best-effort timestamp of when a Workload entered its terminal state.

    Foreman doesn't populate status.completionTime, so use the latest condition
    lastTransitionTime (the terminal transition), falling back to the object's
    creationTimestamp when conditions carry no usable stamp.
    """
    st = wl.get("status") or {}
    stamps = [_parse_ts(c.get("lastTransitionTime")) for c in (st.get("conditions") or [])]
    stamps = [s for s in stamps if s]
    if stamps:
        return max(stamps)
    return _parse_ts((wl.get("metadata") or {}).get("creationTimestamp"))


def prunable_workloads(
    workloads: list, now: datetime, completed_ttl_seconds: int, failed_ttl_seconds: int
) -> list[tuple[str, str]]:
    """(name, phase) pairs of terminal Workloads whose age past their terminal
    transition exceeds the per-phase TTL.

    Completed and Failed have independent TTLs; a TTL <= 0 disables pruning for
    that phase (belt-and-suspenders off switch). Non-terminal Workloads, those
    still within TTL, and those with no resolvable timestamp are left untouched.
    """
    ttl_for = {COMPLETED_PHASE: completed_ttl_seconds, FAILED_PHASE: failed_ttl_seconds}
    results = []
    for wl in workloads:
        phase = ((wl.get("status") or {}).get("phase")) or ""
        ttl = ttl_for.get(phase, 0)
        if ttl <= 0:
            continue
        since = terminal_since(wl)
        if since is None:
            continue
        if (now - since).total_seconds() >= ttl:
            name = (wl.get("metadata") or {}).get("name")
            if name:
                results.append((name, phase))
    return results


def _extract_issue_number(workload_name: str) -> Optional[int]:
    """Extract the issue number from a Workload name like ``issue-42``."""
    if workload_name.startswith("issue-"):
        try:
            return int(workload_name[len("issue-"):])
        except ValueError:
            return None
    return None


def prune_workloads(
    list_workloads: ListWorkloads,
    delete_workload: DeleteWorkload,
    now: Optional[datetime] = None,
    completed_ttl_seconds: int = 0,
    failed_ttl_seconds: int = 0,
    reset_issue: Optional[ResetIssue] = None,
):
    """Delete terminal bridge Workloads past their per-phase TTL, yielding a log
    line per deletion. Runs last in the tick, after reconcile has already
    retried anything retryable, so a still-terminal Workload past its TTL is
    genuinely done. Each delete is best-effort: a failure is logged and the
    next tick retries it.

    When *reset_issue* is provided and a Failed Workload is pruned, the claimed
    issue is reset to ``status/ready`` so it can be re-claimed on the next tick.
    Completed Workloads are NOT reset (their PR already exists).
    """
    if completed_ttl_seconds <= 0 and failed_ttl_seconds <= 0:
        return
    now = now or datetime.now(timezone.utc)
    for name, phase in prunable_workloads(
        list_workloads(), now, completed_ttl_seconds, failed_ttl_seconds
    ):
        try:
            delete_workload(name)
            yield f"prune:deleted:{name}"
        except Exception as e:  # best-effort GC; never break the tick on a delete
            yield f"prune:delete-failed:{name}:{e}"

        # Reset the claimed issue to ready so it can be re-claimed.
        # Only for Failed workloads — Completed ones already have their PR open.
        if reset_issue is not None and phase == FAILED_PHASE:
            issue_number = _extract_issue_number(name)
            if issue_number is not None:
                try:
                    reset_issue(issue_number)
                    yield f"prune:reset-issue:{issue_number}"
                except Exception as e:  # best-effort; next tick reconcile catches it
                    yield f"prune:reset-issue-failed:{issue_number}:{e}"
