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
