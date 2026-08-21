from dataclasses import dataclass
from typing import Optional

from bridge.workload import (
    CODER_AGENT, VERIFIER_AGENT, ATTEMPT_ANNOTATION, SIGNATURE_ANNOTATION,
    PROGRESS_ANNOTATION, LANE_CODER_WILDCARD, gate_profile_for,
)

# Lane values dispatch assigns to a PR-fix item. NEEDS_HUMAN is never actioned
# (dispatch enqueues those already BLOCKED, so they never reach `queued`).
ACTIONABLE_LANES = ("NORMAL", "ESCALATED")


@dataclass(frozen=True)
class PrFixItem:
    repo: str
    pr: int
    issue: Optional[int]
    branch: Optional[str]
    head_sha: Optional[str]
    lane: str
    type: str
    reason: str
    feedback: list  # list[str]


def parse_pr_fix_item(raw) -> Optional[PrFixItem]:
    """Parse one /api/pr-fix-queue/queued element into a PrFixItem.

    Returns None when the element is not a dict or lacks the repo/pr keys
    that make it addressable (an unusable item the actuator must skip)."""
    if not isinstance(raw, dict):
        return None
    repo = raw.get("repo")
    pr = raw.get("pr")
    if not repo or not isinstance(pr, int):
        return None
    feedback = raw.get("feedback")
    return PrFixItem(
        repo=str(repo),
        pr=pr,
        issue=raw.get("issue") if isinstance(raw.get("issue"), int) else None,
        branch=raw.get("branch") or None,
        head_sha=raw.get("headSha") or None,
        lane=str(raw.get("lane") or ""),
        type=str(raw.get("type") or "OTHER"),
        reason=str(raw.get("reason") or ""),
        feedback=[str(f) for f in feedback] if isinstance(feedback, list) else [],
    )



DEFAULT_PRFIX_LANE_AGENTS = {"NORMAL": "coder", "ESCALATED": "coder-frontier"}

_TYPE_HEADERS = {
    "CI_FAILURE": "CI failure:",
    "REVIEW_FEEDBACK": "Review feedback:",
    "MERGE_CONFLICT": "Merge conflict:",
}


def pr_fix_coder_for(lane: str, lane_agents: dict) -> str:
    """Resolve a PrFixLane to a coder Agent name: exact, then "*", else "coder"."""
    if not lane_agents:
        return CODER_AGENT
    return lane_agents.get(lane) or lane_agents.get(LANE_CODER_WILDCARD) or CODER_AGENT


def assemble_fix_prompt(item: "PrFixItem") -> str:
    """Build the code step's payload.prompt from the item: a type header,
    the reason, then each feedback line as a bullet."""
    lines = []
    header = _TYPE_HEADERS.get(item.type)
    if header:
        lines.append(header)
    if item.reason:
        lines.append(item.reason)
    for fb in item.feedback:
        lines.append(f"- {fb}")
    return "\n".join(lines)


PRFIX_CREATED_BY = "dispatch-bridge-prfix"
PRFIX_REPO_ANNOTATION = "foreman.llmkube.dev/prfix-repo"
PRFIX_PR_ANNOTATION = "foreman.llmkube.dev/prfix-pr"

# Bound on a *progressing* retry series (each retry surfaces a fresh failure
# class). Distinct from PR_FIX_MAX_ATTEMPTS, which caps the same-failure
# thrashing series. Lets converging work keep runway while walls are cut off.
PR_FIX_PROGRESS_MAX_ATTEMPTS = 8


def failure_signature(item):
    """Normalize the failure surface of a PrFixItem into a stable token.

    Returns the empty string when there is no feedback at all (so the first
    retry against an unknown surface still decrements the budget rather
    than being treated as 'progress' from a missing baseline).
    """
    if item is None:
        return ""
    type_token = (getattr(item, "type", "") or "").strip().lower() or "unknown"
    feedback = list(getattr(item, "feedback", None) or [])
    if not feedback:
        return f"{type_token}::no-feedback"
    head = feedback[0].strip().lower()
    head = " ".join(head.split())
    if not head:
        return f"{type_token}::no-feedback"
    return f"{type_token}::{head[:160]}"


def prfix_workload_name(item: "PrFixItem") -> str:
    owner_repo = item.repo.replace("/", "-").lower()
    return f"prfix-{owner_repo}-{item.pr}"


def build_fix_workload(item, namespace, gate_profile, agent_name, coder_agent, attempt=1,
                       verify_enabled: bool = True, self_go: list[str] | None = None,
                       signature: str = "") -> dict:
    """Explicit code -> verify pipeline that amends the PR's head branch.

    reviseFromBranch makes the executor fetch and check out the PR branch;
    allowOverwrite lets the push force-with-lease the existing ref.
    branchStrategy must be "rebase" for that checkout to happen at all: the CRD
    default is "reset", which cuts the branch fresh from the current base tip and
    ignores reviseFromBranch, so each attempt would start without the previous
    attempt's commits.

    When verify_enabled is False, only the issue-fix step is emitted (gateless).
    gateProfile still propagates: coders use it for self-gates/language routing.

    ``signature`` is the failure signature that produced this attempt; the
    next reconcile compares the *new* failure signature against this stored
    value to decide whether the last fix made progress. Empty string omits
    the annotation (fresh drain where the caller hasn't decided to track).
    """
    n = item.pr
    code_payload = {
        "repo": item.repo,
        "branch": item.branch,
        "reviseFromBranch": item.branch,
        "branchStrategy": "rebase",
        "allowOverwrite": True,
        "prompt": assemble_fix_prompt(item),
    }
    if item.issue is not None:
        code_payload["issue"] = item.issue
    spec = {
        "intent": f"fix PR #{n}",
        "repo": item.repo,
        "pipeline": [
            {"name": f"fix-{n}", "kind": "issue-fix",
             "agentRef": {"name": coder_agent}, "payload": code_payload},
        ],
    }
    if verify_enabled:
        verify_payload = {"repo": item.repo, "branch": item.branch}
        if item.issue is not None:
            verify_payload["issue"] = item.issue
        spec["pipeline"].append({
            "name": f"fixverify-{n}", "kind": "verify",
            "agentRef": {"name": VERIFIER_AGENT}, "dependsOn": [f"fix-{n}"],
            "payload": verify_payload,
        })
    if self_go:
        spec["verdictPolicy"] = {"selfGO": list(self_go)}
    if gate_profile:
        spec["gateProfile"] = gate_profile
    annotations = {
        ATTEMPT_ANNOTATION: str(attempt),
        PRFIX_REPO_ANNOTATION: item.repo,
        PRFIX_PR_ANNOTATION: str(n),
    }
    if signature:
        annotations[SIGNATURE_ANNOTATION] = signature
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": prfix_workload_name(item),
            "namespace": namespace,
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": item.lane},
            "annotations": annotations,
        },
        "spec": spec,
    }


def drain_pr_fixes(list_queued, existing_prfix_names, create_workload,
                   gate_profiles, lane_agents, agent_name, namespace,
                   verify_enabled: bool = True, self_go: list[str] | None = None) -> list:
    """Create a fix Workload per newly-QUEUED item. list_queued returns raw
    dicts already filtered to actionable lanes by the API query. An item is
    skipped when it has no branch (nothing to amend) or already has an
    in-flight prfix Workload (reconcile owns it; the item stays QUEUED). One
    bad item never aborts the pass."""
    lane_agents = lane_agents or {}
    results = []
    for raw in list_queued():
        item = parse_pr_fix_item(raw)
        if item is None:
            results.append("unparseable:skip")
            continue
        tag = f"{item.repo}#{item.pr}"
        if not item.branch:
            results.append(f"{tag}:skip:no-branch")
            continue
        name = prfix_workload_name(item)
        if name in existing_prfix_names:
            results.append(f"{tag}:skip:in-flight")
            continue
        try:
            manifest = build_fix_workload(
                item, namespace, gate_profile_for(item.repo, gate_profiles),
                agent_name, pr_fix_coder_for(item.lane, lane_agents), attempt=1,
                verify_enabled=verify_enabled,
                signature=failure_signature(item),
                self_go=self_go,
            )
            create_workload(manifest)
            results.append(f"{tag}:created:{name}")
        except Exception as e:
            results.append(f"{tag}:error:{e}")
    return results


# GitHub check-run conclusions that mean "this check did not pass". The API
# emits "failure" — never "failed". The earlier spelling matched nothing, so a
# red PR classified as "ok" and got marked FIXED off unverified success.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)
PENDING_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})


_TERMINAL = ("Succeeded", "Completed", "Failed")

# PR lifecycle states that no amount of coder work can advance.
_TERMINAL_PR = ("merged", "closed")


def rebuild_prfix_manifest(wl: dict, attempt: int, signature: str = "") -> dict:
    """Reconstruct a clean, create-able manifest from a listed fix Workload,
    overriding the attempt annotation. Strips server-managed metadata and
    status so it can be re-created under the same name after delete.

    ``signature`` is the failure signature that produced this attempt; the
    next reconcile compares the *new* failure signature against this stored
    value to decide whether the last fix made progress. Empty string leaves
    any prior signature annotation untouched so a missing-baseline retry
    doesn't wipe the comparison baseline for subsequent ticks."""
    meta = wl.get("metadata") or {}
    ann = dict(meta.get("annotations") or {})
    ann[ATTEMPT_ANNOTATION] = str(attempt)
    if signature:
        ann[SIGNATURE_ANNOTATION] = signature
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "labels": dict(meta.get("labels") or {}),
            "annotations": ann,
        },
        "spec": wl.get("spec") or {},
    }


def _prfix_key(wl: dict):
    ann = (wl.get("metadata") or {}).get("annotations") or {}
    pr = ann.get(PRFIX_PR_ANNOTATION)
    return ann.get(PRFIX_REPO_ANNOTATION), (int(pr) if pr and pr.isdigit() else None)


def next_prfix_lane(lane: str) -> Optional[str]:
    """Next tier up the coder-escalation ladder (ACTIONABLE_LANES order), or
    None when already at the top. NORMAL -> ESCALATED -> None."""
    try:
        idx = ACTIONABLE_LANES.index(lane)
    except ValueError:
        return None
    return ACTIONABLE_LANES[idx + 1] if idx + 1 < len(ACTIONABLE_LANES) else None


def _prfix_current_coder(wl: dict) -> Optional[str]:
    """The coder Agent currently on the fix Workload's issue-fix step."""
    for step in ((wl.get("spec") or {}).get("pipeline") or []):
        if step.get("kind") == "issue-fix":
            return (step.get("agentRef") or {}).get("name")
    return None


def escalate_prfix_manifest(wl: dict, next_lane: str, next_coder: str, signature: str = "") -> dict:
    """Rebuild the fix Workload for the next escalation tier: swap the issue-fix
    coder, flip the lane label, and reset the attempt to 1 (a fresh budget on the
    stronger coder). The failure signature annotation carries over unchanged so
    a thrashing series that escalates still sees the same baseline on the next
    tier; ``signature`` lets the caller overwrite it when the new tier produced
    a different surface (e.g. the fix Workload is being re-launched against a
    freshly-seen failure)."""
    m = rebuild_prfix_manifest(wl, attempt=1, signature=signature)
    m["metadata"].setdefault("labels", {})["lane"] = next_lane
    for step in (m["spec"].get("pipeline") or []):
        if step.get("kind") == "issue-fix":
            step["agentRef"] = {"name": next_coder}
    return m


def reconcile_pr_fixes(list_prfix_workloads, delete_workload, create_workload,
                       mark_pr_fix, pr_is_mergeable=lambda repo, pr: "ok", max_attempts=3,
                       lane_agents=None,
                       get_pr_fix_signature=lambda repo, pr: "",
                       progress_max_attempts=PR_FIX_PROGRESS_MAX_ATTEMPTS) -> list:
    """Settle prior fix Workloads: Succeeded -> verify the PR is actually
    mergeable (pr_is_mergeable) before marking FIXED, delete only if the mark
    succeeded (else leave the tombstone so the next tick retries the mark);
    a Succeeded Workload whose PR is still conflicting is treated like a
    Failed one (retried under the attempt cap, or BLOCKED at the cap) since
    the fix workload's own success says nothing about mergeability; Failed
    under the attempt cap -> delete + recreate at attempt+1; Failed at
    the cap -> mark BLOCKED + leave a tombstone. Non-terminal Workloads are
    untouched. Per-Workload isolation so one wedged delete/create/mark cannot
    abort the pass or the drain that follows."""
    results = []
    for wl in list_prfix_workloads():
        meta = wl.get("metadata") or {}
        name = meta.get("name") or "?"
        phase = (wl.get("status") or {}).get("phase") or ""
        if phase not in _TERMINAL:
            continue
        repo, pr = _prfix_key(wl)
        ann = meta.get("annotations") or {}
        try:
            attempt = int(ann.get(ATTEMPT_ANNOTATION, "1") or "1")
            merge_status = "ok"
            # Consulted for EVERY terminal phase, not just Succeeded. A Failed
            # Workload used to retry purely against the attempt cap, so a merged
            # PR burned the whole budget and then escalated to the frontier coder
            # to fix something that had already landed (#118).
            if repo and pr is not None:
                merge_status = pr_is_mergeable(repo, pr)

            # A merged or closed PR is terminal: resolve the item and drop the
            # Workload instead of retrying or escalating. Resolving it also keeps
            # drain_pr_fixes from recreating the Workload on the next tick.
            if merge_status in _TERMINAL_PR and repo and pr is not None:
                outcome = "FIXED" if merge_status == "merged" else "STALE"
                if mark_pr_fix(repo, pr, outcome, f"PR is {merge_status}; nothing left to fix"):
                    delete_workload(name)
                    results.append(f"{name}:pr-{merge_status}")
                else:
                    results.append(f"{name}:pr-{merge_status}:mark-failed")
                continue

            ok = False
            if phase in ("Succeeded", "Completed") and merge_status == "ok":
                if repo and pr is not None:
                    ok = mark_pr_fix(repo, pr, "FIXED", f"foreman fix Workload {name} succeeded")
            if ok:
                delete_workload(name)
                results.append(f"{name}:fixed")
            # Genuine merge conflict (DIRTY/CONFLICTING). A coder cannot resolve
            # a conflict introduced by an unrelated merge; one determination is
            # enough, and the conflict will not resolve itself between ticks.
            # Mark BLOCKED + drop the Workload without burning the attempt
            # budget. (#163)
            elif merge_status in ("dirty", "conflicting"):
                if repo and pr is not None:
                    mark_pr_fix(
                        repo, pr, "BLOCKED",
                        f"foreman fix abandoned: PR has a merge conflict; "
                        f"coder cannot resolve ({name})",
                    )
                delete_workload(name)
                results.append(f"{name}:not-mergeable-giveup:{attempt}/{max_attempts}")
            # Blocked for a non-check reason (awaiting required review, merge
            # queue not ready, etc.). The coder cannot unblock it, and it is
            # not a verdict failure either. Don't burn a retry attempt; just
            # leave the Workload for the next reconcile tick. (#163)
            elif merge_status == "blocked" and attempt < max_attempts:
                results.append(f"{name}:blocked:{attempt}/{max_attempts}")
            # checks_pending -> don't burn a retry attempt; leave the workload
            # for the next reconcile tick to pick up (unless at the cap)
            elif merge_status == "checks_pending" and attempt < max_attempts:
                results.append(f"{name}:checks-pending:{attempt}/{max_attempts}")
            # Mark failed, still failing check, or Failed phase -> retry or BLOCKED
            elif attempt < max_attempts:
                # Signature-aware budgeting: charge the attempt budget by
                # *failure signature*, not by attempt count (#133). A retry
                # against the same wall still ticks attempt++; a retry against
                # a *different* failure surface counts as progress and gets a
                # fresh per-tier attempt (=1) plus a separate, larger progress
                # budget as the runaway bound. The attempt cap itself only
                # bounds the thrashing-on-the-same-wall case.
                prev_sig = ann.get(SIGNATURE_ANNOTATION, "") or ""
                try:
                    new_sig = get_pr_fix_signature(repo, pr) if (repo and pr is not None) else ""
                except Exception:
                    new_sig = ""
                progress = 0
                try:
                    progress = int(ann.get(PROGRESS_ANNOTATION, "0") or "0")
                except Exception:
                    progress = 0
                progressed = bool(prev_sig) and bool(new_sig) and prev_sig != new_sig
                if progressed:
                    next_progress = progress + 1
                    if next_progress > progress_max_attempts:
                        # Runaway bound: too many *progressing* retries, each
                        # surface-fix lasted one tick. Same escalation/BLOCKED
                        # shape as the exhausted-attempt branch below.
                        if repo and pr is not None:
                            mark_pr_fix(
                                repo, pr, "BLOCKED",
                                f"foreman fix exhausted {next_progress}/{progress_max_attempts} "
                                f"progressing retries; last surface: {new_sig} ({name})",
                            )
                        delete_workload(name)
                        results.append(
                            f"{name}:progress-giveup:{next_progress}/{progress_max_attempts}"
                        )
                        continue
                    delete_workload(name)
                    manifest = rebuild_prfix_manifest(
                        wl, attempt=1, signature=new_sig,
                    )
                    manifest["metadata"]["annotations"][PROGRESS_ANNOTATION] = str(next_progress)
                    create_workload(manifest)
                    tag = "not-mergeable-retry-progress" if merge_status == "checks_failed" else "retry-progress"
                    results.append(
                        f"{name}:{tag}:{next_progress}/{progress_max_attempts}"
                    )
                else:
                    delete_workload(name)
                    create_workload(rebuild_prfix_manifest(
                        wl, attempt + 1,
                        signature=new_sig if new_sig else "",
                    ))
                    tag = "not-mergeable-retry" if merge_status == "checks_failed" else "retry"
                    results.append(f"{name}:{tag}:{attempt + 1}/{max_attempts}")
            else:
                # Tier exhausted at the attempt cap. Before giving up, escalate to
                # the next coder tier (NORMAL -> ESCALATED) with a fresh attempt
                # budget, so a fix the base coder can't do gets the stronger coder
                # rather than dead-ending on a human. Only BLOCK when there is no
                # higher tier (i.e. the escalated tier is itself exhausted).
                current_lane = (meta.get("labels") or {}).get("lane", "")
                nxt = next_prfix_lane(current_lane)
                next_coder = pr_fix_coder_for(nxt, lane_agents or {}) if nxt else None
                if nxt and next_coder and next_coder != _prfix_current_coder(wl):
                    delete_workload(name)
                    create_workload(escalate_prfix_manifest(wl, nxt, next_coder))
                    results.append(f"{name}:escalate:{current_lane or 'NORMAL'}->{nxt}")
                else:
                    if repo and pr is not None:
                        note = (
                            f"foreman fix Workload {name} succeeded but PR still has a "
                            f"failing check after {attempt}/{max_attempts} attempts"
                            if merge_status == "checks_failed" else
                            f"foreman fix exhausted {attempt}/{max_attempts} attempts on "
                            f"{current_lane or 'NORMAL'} (all coder tiers exhausted) ({name})"
                        )
                        mark_pr_fix(repo, pr, "BLOCKED", note)
                    tag = "not-mergeable-giveup" if merge_status == "checks_failed" else "giveup"
                    results.append(f"{name}:{tag}:{attempt}/{max_attempts}")
        except Exception as e:
            results.append(f"{name}:error:{e}")
    return results
