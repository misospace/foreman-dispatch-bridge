"""Transition completed Workloads to in-review in dispatch.

When a Foreman Workload completes and a PR has been opened (by the coder
or the reviewer), the dispatch issue should move from ``status/in-progress``
to ``status/in-review``. This prevents the reconcile loop from re-dispatching
an issue whose work is already under review on GitHub — a re-dispatch that
would cut a fresh branch from main and clobber the reviewed commit via
``allowOverwrite``.

The PR URL is read from AgenticTask ``status.result.extra.pullRequestURL`` on
any succeeded task belonging to the Workload (review or code). Only Workloads
created by ``dispatch-bridge`` (not ``dispatch-bridge-prfix``) are eligible;
pr-fix Workloads have their own lifecycle through the pr-fix queue.

This module is a pure function taking injected callables so it's testable
without a cluster or network.
"""

from typing import Callable

# Re-exported for main.py
PRFIX_CREATED_BY = "dispatch-bridge-prfix"
ATTEMPT_ANNOTATION = "foreman.llmkube.dev/attempt"
ISSUE_ID_ANNOTATION = "foreman.llmkube.dev/issue-id"

# Shared with bridge.main.park_for_human — the parking dedupe is keyed on
# this label, so the comment + status flip below is a no-op on repeated
# cycles over the same parked issue.
NEEDS_HUMAN_LABEL = "needs-human"

ListWorkloads = Callable[[], list]                       # () -> Workload manifests
ListWorkloadTasks = Callable[[str], list]                # (wl name) -> AgenticTask manifests
UpdateStatus = Callable[..., bool]  # (item, status, agent[, blocked_reason]) -> success


def _workload_is_completed(wl: dict) -> bool:
    return ((wl.get("status") or {}).get("phase") or "") == "Completed"


def _workload_is_prfix(wl: dict) -> bool:
    labels = (wl.get("metadata") or {}).get("labels") or {}
    return labels.get("created-by") == PRFIX_CREATED_BY


def _extract_pr_url(tasks: list) -> str:
    """Read pullRequestURL from any succeeded task's result.extra."""
    for t in tasks or []:
        result = (t.get("status") or {}).get("result") or {}
        extra = result.get("extra") or {}
        url = extra.get("pullRequestURL")
        if url:
            return str(url)
    return ""


def _extract_task_signal(tasks: list) -> tuple[str, str, dict]:
    """Read verdict, result.summary, and result.extra from the last task that carries a verdict.

    The coder task's status already carries the routing signal:
    ``status.verdict`` (``GO`` / ``INCOMPLETE``), ``status.result.summary``,
    and ``status.result.extra`` (``commitSHA``, ``branch``, ...). An absent
    verdict is returned as ``""`` so callers can treat it as non-GO.
    """
    verdict = ""
    summary = ""
    extra: dict = {}
    for t in tasks or []:
        status = t.get("status") or {}
        v = status.get("verdict")
        if not v:
            continue
        verdict = str(v)
        result = status.get("result") or {}
        summary = str(result.get("summary") or "")
        extra = result.get("extra") or {}
    return verdict, summary, extra


def _workload_all_already_resolved(wl: dict) -> bool:
    """Return True if any task carries a Completed condition with reason=AllAlreadyResolved.

    A Workload that ends AllAlreadyResolved has no PR by definition (no fix was
    attempted), so it can never take the in-review path. Without this check it
    lands in skip:no-pr and strands its issue in status/in-progress forever,
    permanently consuming a MAX_IN_PROGRESS claim slot.
    """
    status = wl.get("status") or {}
    for ts in status.get("taskStatuses") or []:
        for cond in ts.get("conditions") or []:
            if cond.get("type") == "Completed" and cond.get("reason") == "AllAlreadyResolved":
                return True
    return False


def _item_from_workload(wl: dict) -> dict:
    """Build the identity dict update_status consumes."""
    spec = wl.get("spec") or {}
    ann = (wl.get("metadata") or {}).get("annotations") or {}
    issues = spec.get("issues") or [0]
    return {
        "issueId": ann.get(ISSUE_ID_ANNOTATION, ""),
        "repoFullName": spec.get("repo", ""),
        "number": int(issues[0]),
    }


def transition_to_in_review(
    list_workloads: ListWorkloads,
    list_workload_tasks: ListWorkloadTasks,
    update_status: UpdateStatus,
    agent_name: str,
    dispatch=None,
) -> list[str]:
    """Flip completed bridge Workloads with an open PR to ``status/in-review``.

    Scans every non-prfix bridge Workload. For each that is ``Completed``, reads
    its AgenticTasks looking for a ``pullRequestURL`` in any succeeded task's
    result. If found, calls ``update_status(item, "in-review", agent_name)``.

    Per-Workload isolation: a status update that raises is logged and skipped;
    it never aborts the pass. Returns human-readable log lines.
    """
    results: list[str] = []
    for wl in list_workloads():
        name = (wl.get("metadata") or {}).get("name") or "?"

        if not _workload_is_completed(wl):
            results.append(f"{name}:skip:not-completed")
            continue

        if _workload_is_prfix(wl):
            results.append(f"{name}:skip:prfix")
            continue

        tasks = list_workload_tasks(name)
        pr_url = _extract_pr_url(tasks)
        if not pr_url:
            if _workload_all_already_resolved(wl):
                # The work already exists on main; the honest destination is
                # status/done, which releases the in-progress claim.
                item = _item_from_workload(wl)
                try:
                    update_status(item, "done", agent_name)
                    results.append(f"{name}:done:already-resolved")
                except Exception as e:
                    results.append(f"{name}:error:{e}")
                continue
            # No PR and not AllAlreadyResolved: route on the coder task's
            # verdict instead of silently skipping (#213). A silent skip leaves
            # the issue in status/in-progress with no Workload that will run
            # again, so it holds a MAX_IN_PROGRESS slot and re-cycles.
            verdict, summary, extra = _extract_task_signal(tasks)
            if verdict != "GO":
                # Non-GO (or absent) verdict: park the issue out of agent
                # circulation so it stops being re-claimed. "blocked" is the
                # status that does this today (see update_status in claim.py).
                item = _item_from_workload(wl)
                # dispatch requires a reason to park an issue as blocked, and
                # rejects the call outright without one. Carry the verdict and
                # summary we already have rather than leaving a human to guess
                # why this landed in the blocked column.
                reason = f"Coder returned verdict {verdict or 'absent'} with no PR"
                if summary:
                    reason = f"{reason}: {summary}"
                try:
                    update_status(item, "blocked", agent_name, reason)
                    results.append(
                        f"{name}:blocked:no-pr:verdict={verdict or 'absent'}"
                        + (f":{summary}" if summary else "")
                    )
                except Exception as e:
                    results.append(f"{name}:error:{e}")
                continue
            # GO with no PR is a distinct anomaly: the coder reported success
            # but no PR exists. Park for a human so the loop does not silently
            # re-run the full pipeline every reconciliation cycle. The reason
            # records commitSHA/branch so an operator can tell whether the push
            # failed or the PR step did. ``needs-human`` is the existing
            # dedupe key: repeated cycles reuse the parked label and skip the
            # comment, so we never post duplicates.
            item = _item_from_workload(wl)
            commit_sha = extra.get("commitSHA") or "none"
            branch_name = extra.get("branch") or "none"
            reason = (
                f"Coder reported GO but no PR was opened "
                f"(commitSHA={commit_sha}, branch={branch_name})."
                + (f" {summary}" if summary else "")
            )
            repo = item.get("repoFullName") or ""
            issue_number = int(item.get("number") or 0)
            already_parked = bool(
                dispatch
                and repo
                and issue_number
                and dispatch.issue_is_parked(
                    repo, issue_number, NEEDS_HUMAN_LABEL
                )
            )
            if not already_parked:
                try:
                    update_status(item, "backlog", agent_name, reason)
                except Exception as e:
                    results.append(f"{name}:error:{e}")
                    continue
                if dispatch is not None and repo and issue_number:
                    try:
                        dispatch.add_label(item, NEEDS_HUMAN_LABEL)
                    except Exception:
                        # Label application is best-effort: the status flip
                        # above already breaks the reconciliation loop, and a
                        # second pass will retry the label.
                        pass
                    try:
                        dispatch.post_comment(
                            item,
                            _parked_for_human_comment(
                                repo, issue_number, reason
                            ),
                        )
                    except Exception:
                        pass
            results.append(
                f"{name}:parked:go-no-pr"
                f":commitSHA={commit_sha}"
                f":branch={branch_name}"
                + (":replay" if already_parked else "")
            )
            continue

        item = _item_from_workload(wl)
        try:
            update_status(item, "in-review", agent_name)
            results.append(f"{name}:in-review:{pr_url}")
        except Exception as e:
            results.append(f"{name}:error:{e}")

    return results


def _parked_for_human_comment(
    repo: str, issue_number: int, reason: str, path: str = "go-no-pr"
) -> str:
    """Format the ``needs-human`` comment for a parked GO-with-no-PR outcome.

    Mirrors the escalation comment style used by ``bridge.main.park_for_human``
    so a human reading the issue sees the same shape regardless of which path
    parked it. Kept local to this file to avoid the circular import
    ``bridge.main`` would create (it imports this module).

    ``path`` is rendered as a stable ``path: <value>`` tag in the header so a
    triage flow can grep on ``path:`` to group issues by cause without opening
    the Workload (issue #260).
    """
    header = ":rotating_light: coder reported GO but no PR was opened."
    if path:
        header += f" (`path: {path}`)"
    lines = [
        header,
        "",
        f"**Reason:** {reason}",
        "",
        f"**Issue:** {repo}#{issue_number}",
        "This issue is parked for a human. A reconciliation pass will not "
        "re-claim it while the ``needs-human`` label is present.",
    ]
    return "\n".join(lines)
