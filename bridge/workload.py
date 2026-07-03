import json
from typing import Optional

from bridge.models import ClaimedItem

# Default coder when a lane has no explicit mapping; Ornith reviews, deterministic gate verifies.
CODER_AGENT = "coder"
VERIFIER_AGENT = "gate"
REVIEWER_AGENTS = ["reviewer"]

# Fallback key in a lane->coder-agent map: its agent applies to any lane that
# has no entry of its own.
LANE_CODER_WILDCARD = "*"

# Fallback key in a gate-profile map: its profile applies to any repo that has
# no entry of its own.
GATE_PROFILE_WILDCARD = "*"

# Annotation keys the bridge stamps on each Workload so the failed-workload
# retry loop can read attempt count + the dispatch identity needed to unclaim.
ATTEMPT_ANNOTATION = "foreman.llmkube.dev/attempt"
ISSUE_ID_ANNOTATION = "foreman.llmkube.dev/issue-id"
AGENT_NAME_ANNOTATION = "foreman.llmkube.dev/agent-name"


def parse_gate_profiles(raw: Optional[str]) -> dict:
    """Parse the GATEPROFILE_MAP env var (JSON object: repo -> GateProfile).

    Empty or absent -> {}, so every Workload omits gateProfile and Foreman
    falls back to its Go gate (unchanged behavior). Each value is passed
    through verbatim as Workload.spec.gateProfile, so the full CRD shape is
    expressible from config:

        {
          "misospace/dispatch": {"language": "node",
                                 "commands": {"test": "corepack pnpm i && corepack pnpm test"}},
          "misospace/miso-gallery": {"language": "python",
                                     "commands": {"test": "pip install -q -e . && pytest -q"}},
          "*": {"language": "generic"}
        }

    A bare {"language": "node"} uses the preset's stock image (node:22), which
    ships no eslint/prettier/test deps -- set commands (install-in-command) or
    a pre-baked image for repos with real toolchains.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    return json.loads(raw)


def gate_profile_for(repo: str, gate_profiles: dict) -> Optional[dict]:
    """Resolve a repo's gate profile: exact match, then the "*" wildcard, else None."""
    if not gate_profiles:
        return None
    return gate_profiles.get(repo) or gate_profiles.get(GATE_PROFILE_WILDCARD)


def parse_lane_coder_agents(raw: Optional[str]) -> dict:
    """Parse the LANE_CODER_AGENTS env var (JSON object: lane -> coder Agent name).

    Empty or absent -> {}, so every lane routes to the default coder Agent
    (unchanged behavior). Example wiring the escalation tier to a cloud-proxy
    coder:

        {"*": "coder", "frontier": "coder-frontier"}
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    return json.loads(raw)


def coder_agent_for(lane: str, lane_coder_agents: dict) -> str:
    """Resolve a lane's coder Agent: exact match, then "*", else the default coder."""
    if not lane_coder_agents:
        return CODER_AGENT
    return lane_coder_agents.get(lane) or lane_coder_agents.get(LANE_CODER_WILDCARD) or CODER_AGENT


def revision_coder_agent_for(lane: str, revision_coder_agents: dict) -> str:
    """Resolve a lane's revision-tuned coder Agent (Workload.spec.revisionCoderAgentRef,
    LLMKube#959): exact match, then "*", else "" (unset -> controller falls back to the
    base coder and warns)."""
    if not revision_coder_agents:
        return ""
    return revision_coder_agents.get(lane) or revision_coder_agents.get(LANE_CODER_WILDCARD) or ""


def workload_name(item: ClaimedItem) -> str:
    owner_repo = item.repo.replace("/", "-").lower()
    return f"wl-{owner_repo}-{item.issue_number}"


def _pipeline_steps(
    item: ClaimedItem, name: str, coder_agent: str, feedback: str, allow_overwrite: bool = False
) -> list:
    """Explicit spec.pipeline mirroring the operator's issues-path decomposition
    (code -> verify -> review-*), with the retry feedback injected as the code
    step's payload.prompt — the only channel that reaches the coder's user
    prompt ("Issue context"). Branch naming matches the issues path so re-runs
    keep the same branch. gateProfile still propagates: the operator stamps the
    Workload default onto every rendered step."""
    n = item.issue_number
    branch = f"foreman/{name}/issue-{n}"
    payload = {"repo": item.repo, "issue": n, "branch": branch}
    steps = [
        {
            "name": f"code-{n}",
            "kind": "issue-fix",
            "agentRef": {"name": coder_agent},
            "payload": (
                {**payload, "prompt": feedback, "allowOverwrite": True}
                if allow_overwrite
                else {**payload, "prompt": feedback}
            ),
        },
        {
            "name": f"verify-{n}",
            "kind": "verify",
            "agentRef": {"name": VERIFIER_AGENT},
            "dependsOn": [f"code-{n}"],
            "payload": dict(payload),
        },
    ]
    for i, reviewer in enumerate(REVIEWER_AGENTS):
        steps.append({
            "name": f"review-{n}-{i}",
            "kind": "review",
            "agentRef": {"name": reviewer},
            "dependsOn": [f"verify-{n}"],
            "payload": dict(payload),
        })
    return steps


def build_workload(
    item: ClaimedItem,
    namespace: str,
    gate_profile: Optional[dict] = None,
    agent_name: str = "",
    attempt: int = 1,
    coder_agent: str = CODER_AGENT,
    feedback: str = "",
    revision_coder_agent: str = "",
) -> dict:
    # A retry reuses its predecessor's deterministic branch name; if that
    # attempt pushed, the retry's push dies non-fast-forward (#1). Foreman's
    # opt-in spec.allowOverwrite (LLMKube#948) lets the coder replace its own
    # stale ref via force-with-lease. First attempts must NOT set it: their
    # branch shouldn't exist, and failing loudly preserves the audit trail.
    allow_overwrite = attempt > 1
    if feedback:
        # Retry with context: explicit pipeline so payload.prompt can carry the
        # previous attempt's review findings / failure to the coder.
        spec = {
            "intent": item.intent,
            "repo": item.repo,
            "pipeline": _pipeline_steps(
                item, workload_name(item), coder_agent, feedback, allow_overwrite
            ),
        }
    else:
        spec = {
            "intent": item.intent,
            "repo": item.repo,
            "issues": [item.issue_number],
            "coderAgentRef": {"name": coder_agent},
            "verifierAgentRef": {"name": VERIFIER_AGENT},
            "reviewerAgentRefs": [{"name": name} for name in REVIEWER_AGENTS],
        }
        if revision_coder_agent:
            spec["revisionCoderAgentRef"] = {"name": revision_coder_agent}
    if allow_overwrite:
        # Top-level as well: the operator stamps it onto issue-path payloads
        # (LLMKube#948); harmless duplication on the pipeline path. Ignored by
        # pre-#948 CRDs (extra-field pruning), so this is inert until foreman
        # ships it.
        spec["allowOverwrite"] = True
    if gate_profile:
        # Passed through verbatim. Foreman >= 0.8.23 copies Workload.spec.gateProfile
        # onto every decomposed AgenticTask (the coder self-gate + verify Job), so a
        # non-Go repo runs its own language gate instead of the Go default.
        spec["gateProfile"] = gate_profile
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": workload_name(item),
            "namespace": namespace,
            "labels": {"created-by": "dispatch-bridge", "lane": item.lane},
            # attempt drives the retry cap; issue-id + agent-name let the retry
            # loop unclaim the dispatch issue when retries are exhausted.
            "annotations": {
                ATTEMPT_ANNOTATION: str(attempt),
                ISSUE_ID_ANNOTATION: item.issue_id,
                AGENT_NAME_ANNOTATION: agent_name,
            },
        },
        "spec": spec,
    }
