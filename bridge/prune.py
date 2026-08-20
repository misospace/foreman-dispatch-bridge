from datetime import datetime, timezone
from typing import Callable, Optional

# Terminal phases eligible for garbage collection. A Completed Workload has
# already opened its PR (which lives on GitHub independently); a Failed one
# that is still Failed at prune time has been left alone by reconcile
# (retries exhausted / blocked), so both are tombstones once past their TTL.
COMPLETED_PHASE = "Completed"
FAILED_PHASE = "Failed"

# Bridge-owned annotation stamped the first tick a Workload is seen in a
# terminal phase. Used by `terminal_since` so prune age is measured against a
# timestamp the Foreman controller does not rewrite (issue #170). The
# fallback key is written by older bridges / for one-time migrations where
# the per-phase split is unknown.
TERMINAL_SINCE_ANNOTATION_PREFIX = "foreman.llmkube.dev/terminal-since"
TERMINAL_SINCE_ANNOTATION_FALLBACK = "foreman.llmkube.dev/terminal-since"

ListWorkloads = Callable[[], list]      # () -> list of Workload manifests (dicts)
DeleteWorkload = Callable[[str], None]  # (name) -> None
ResetIssue = Callable[[dict], None]     # (workload_manifest) -> None (return ignored)


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

    Foreman's controller rewrites condition lastTransitionTimes on every
    reconcile, so the latest condition timestamp is not a stable age signal
    (issue #170: prune TTL never elapses because the maximum across conditions
    is repeatedly refreshed). The bridge owns the timestamp it depends on:
    the first tick a Workload is seen in a terminal phase stamps an annotation
    we then read here. Pre-existing terminal Workloads (no annotation yet)
    fall back to metadata.creationTimestamp, which the controller also does
    not rewrite, so they still age out.
    """
    annotations = (wl.get("metadata") or {}).get("annotations") or {}
    phase = ((wl.get("status") or {}).get("phase")) or ""
    if phase in (COMPLETED_PHASE, FAILED_PHASE):
        for key in (
            f"{TERMINAL_SINCE_ANNOTATION_PREFIX}/{phase}",
            TERMINAL_SINCE_ANNOTATION_FALLBACK,
        ):
            stamped = annotations.get(key)
            ts = _parse_ts(stamped)
            if ts is not None:
                return ts
    return _parse_ts((wl.get("metadata") or {}).get("creationTimestamp"))


def stamp_terminal_since(wl: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Stamp the bridge-owned terminal-since annotation on a Workload manifest
    in place and return the stamp written. No-op (returns None) if the manifest
    is not in a terminal phase or already carries a stamp for that phase, so
    repeated reconciles do not advance the timestamp the prune TTL reads.

    The stamp is the moment the bridge first observed the terminal phase;
    that is the definition of "how long has this been terminal" we want.
    """
    phase = ((wl.get("status") or {}).get("phase")) or ""
    if phase not in (COMPLETED_PHASE, FAILED_PHASE):
        return None
    md = wl.setdefault("metadata", {})
    annotations = md.setdefault("annotations", {})
    key = f"{TERMINAL_SINCE_ANNOTATION_PREFIX}/{phase}"
    existing = annotations.get(key)
    if existing:
        ts = _parse_ts(existing)
        if ts is not None:
            return existing
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    annotations[key] = stamp
    return stamp


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


ISSUE_CREATED_BY = "dispatch-bridge"


def _is_issue_workload(wl: dict) -> bool:
    """Return True if this is an issue Workload (created-by=dispatch-bridge).

    PR-fix Workloads (created-by=dispatch-bridge-prfix) must not have their
    issues callback invoked even if their spec someday carries repo/issues.
    """
    labels = (wl.get("metadata") or {}).get("labels") or {}
    return labels.get("created-by") == ISSUE_CREATED_BY


def _has_identity(wl: dict) -> bool:
    """Return True if the workload manifest carries enough identity to reset."""
    spec = wl.get("spec") or {}
    return bool(spec.get("repo")) and bool(spec.get("issues"))


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

    When *reset_issue* is provided and a Failed Workload is pruned, the full
    workload manifest is passed to the callback so it can extract identity
    (issueId, repo, issueNumber, agentName) from annotations and spec.
    Completed Workloads are NOT reset (their PR already exists).
    """
    if completed_ttl_seconds <= 0 and failed_ttl_seconds <= 0:
        return
    now = now or datetime.now(timezone.utc)
    workloads = list_workloads()
    wl_by_name = {(wl.get("metadata") or {}).get("name"): wl for wl in workloads}
    for name, phase in prunable_workloads(
        workloads, now, completed_ttl_seconds, failed_ttl_seconds
    ):
        try:
            delete_workload(name)
            yield f"prune:deleted:{name}"
        except Exception as e:  # best-effort GC; never break the tick on a delete
            yield f"prune:delete-failed:{name}:{e}"

        if reset_issue is not None and phase == FAILED_PHASE:
            wl = wl_by_name.get(name) or {}
            if not _is_issue_workload(wl):
                yield f"prune:reset-issue-skipped:{name}"
                continue
            if not _has_identity(wl):
                yield f"prune:reset-issue-skipped:{name}"
                continue
            try:
                reset_issue(wl)
                yield f"prune:reset-issue:{name}"
            except Exception as e:  # best-effort; next tick reconcile catches it
                yield f"prune:reset-issue-failed:{name}:{e}"
