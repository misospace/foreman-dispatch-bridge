from dataclasses import dataclass
from typing import Optional

from bridge.workload import CODER_AGENT

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
