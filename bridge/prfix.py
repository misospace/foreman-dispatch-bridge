from dataclasses import dataclass
from typing import Optional

from bridge.workload import (
    CODER_AGENT, VERIFIER_AGENT, ATTEMPT_ANNOTATION, gate_profile_for,
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


PRFIX_CODER_WILDCARD = "*"
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
    return lane_agents.get(lane) or lane_agents.get(PRFIX_CODER_WILDCARD) or CODER_AGENT


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


def prfix_workload_name(item: "PrFixItem") -> str:
    owner_repo = item.repo.replace("/", "-").lower()
    return f"prfix-{owner_repo}-{item.pr}"


def build_fix_workload(item, namespace, gate_profile, agent_name, coder_agent, attempt=1) -> dict:
    """Explicit code -> verify pipeline that amends the PR's head branch.

    reviseFromBranch makes the executor fetch and check out the PR branch;
    allowOverwrite lets the push force-with-lease the existing ref."""
    n = item.pr
    code_payload = {
        "repo": item.repo,
        "branch": item.branch,
        "reviseFromBranch": item.branch,
        "allowOverwrite": True,
        "prompt": assemble_fix_prompt(item),
    }
    if item.issue is not None:
        code_payload["issue"] = item.issue
    verify_payload = {"repo": item.repo, "branch": item.branch}
    if item.issue is not None:
        verify_payload["issue"] = item.issue
    spec = {
        "intent": f"fix PR #{n}",
        "repo": item.repo,
        "pipeline": [
            {"name": f"fix-{n}", "kind": "issue-fix",
             "agentRef": {"name": coder_agent}, "payload": code_payload},
            {"name": f"fixverify-{n}", "kind": "verify",
             "agentRef": {"name": VERIFIER_AGENT}, "dependsOn": [f"fix-{n}"],
             "payload": verify_payload},
        ],
    }
    if gate_profile:
        spec["gateProfile"] = gate_profile
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": prfix_workload_name(item),
            "namespace": namespace,
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": item.lane},
            "annotations": {
                ATTEMPT_ANNOTATION: str(attempt),
                PRFIX_REPO_ANNOTATION: item.repo,
                PRFIX_PR_ANNOTATION: str(n),
            },
        },
        "spec": spec,
    }


def drain_pr_fixes(list_queued, existing_prfix_names, create_workload,
                   gate_profiles, lane_agents, agent_name, namespace) -> list:
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
            )
            create_workload(manifest)
            results.append(f"{tag}:created:{name}")
        except Exception as e:
            results.append(f"{tag}:error:{e}")
    return results


_TERMINAL = ("Succeeded", "Completed", "Failed")


def rebuild_prfix_manifest(wl: dict, attempt: int) -> dict:
    """Reconstruct a clean, create-able manifest from a listed fix Workload,
    overriding the attempt annotation. Strips server-managed metadata and
    status so it can be re-created under the same name after delete."""
    meta = wl.get("metadata") or {}
    ann = dict(meta.get("annotations") or {})
    ann[ATTEMPT_ANNOTATION] = str(attempt)
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


def escalate_prfix_manifest(wl: dict, next_lane: str, next_coder: str) -> dict:
    """Rebuild the fix Workload for the next escalation tier: swap the issue-fix
    coder, flip the lane label, and reset the attempt to 1 (a fresh budget on the
    stronger coder)."""
    m = rebuild_prfix_manifest(wl, attempt=1)
    m["metadata"].setdefault("labels", {})["lane"] = next_lane
    for step in (m["spec"].get("pipeline") or []):
        if step.get("kind") == "issue-fix":
            step["agentRef"] = {"name": next_coder}
    return m


def reconcile_pr_fixes(list_prfix_workloads, delete_workload, create_workload,
                       mark_pr_fix, pr_is_mergeable=lambda repo, pr: True, max_attempts=3,
                       lane_agents=None) -> list:
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
            still_conflicting = (
                phase in ("Succeeded", "Completed")
                and repo and pr is not None
                and not pr_is_mergeable(repo, pr)
            )
            ok = False
            if phase in ("Succeeded", "Completed") and not still_conflicting:
                if repo and pr is not None:
                    ok = mark_pr_fix(repo, pr, "FIXED", f"foreman fix Workload {name} succeeded")
            if ok:
                delete_workload(name)
                results.append(f"{name}:fixed")
            # Mark failed, still conflicting, or Failed phase -> retry or BLOCKED
            elif attempt < max_attempts:
                delete_workload(name)
                create_workload(rebuild_prfix_manifest(wl, attempt + 1))
                tag = "not-mergeable-retry" if still_conflicting else "retry"
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
                            f"foreman fix Workload {name} succeeded but PR is still not "
                            f"mergeable after {attempt}/{max_attempts} attempts"
                            if still_conflicting else
                            f"foreman fix exhausted {attempt}/{max_attempts} attempts on "
                            f"{current_lane or 'NORMAL'} (all coder tiers exhausted) ({name})"
                        )
                        mark_pr_fix(repo, pr, "BLOCKED", note)
                    tag = "not-mergeable-giveup" if still_conflicting else "giveup"
                    results.append(f"{name}:{tag}:{attempt}/{max_attempts}")
        except Exception as e:
            results.append(f"{name}:error:{e}")
    return results
