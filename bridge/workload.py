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


def _parse_json_map(raw: Optional[str], name: str = "config") -> dict:
    """Shared parser for the JSON-object env vars (gate profiles, lane/language
    coder-agent maps): empty/absent -> {}, else json.loads. Values pass through
    verbatim so the full CRD shape is expressible from config.

    On invalid JSON, raises ValueError with context about which config source
    failed and a prefix of the offending value.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {name}: {exc} — got: {raw[:80]!r}"
        ) from exc


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
    return _parse_json_map(raw, "GATEPROFILE_MAP")


# Work classes a coder's own GO may self-certify (LLMKube proposal #1075).
# Foreman's default is [code-fix, docs, packaging, config]: ci-policy and
# release-policy are excluded because the in-workspace gate can tell a
# workflow still parses but not whether its logic is correct. That default
# makes every CI/workflow chore terminal here — the coder does the work,
# pushes a correct branch, then the policy demotes its GO to
# NO-GO/NEEDS-VERIFICATION and reviewers cascade to INCOMPLETE, discarding
# the work. Fleets whose PRs run the changed workflow, get an AI review, and
# are merged by hand can widen the list; empty/unset keeps Foreman's default.
def parse_self_go(raw: str | None) -> list[str]:
    """Parse VERDICT_SELF_GO (comma-separated work classes) into a list."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


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
    return _parse_json_map(raw, "LANE_CODER_AGENTS")


def parse_base_coder_agents(raw: Optional[str]) -> dict:
    """Parse the BASE_CODER_AGENTS env var (JSON object: language -> coder Agent name).

    Same shape/parser as LANE_CODER_AGENTS, keyed by the repo's programming
    language instead of lane. Empty or absent -> {}, so the base lane routes
    to the default coder Agent (unchanged behavior). Example:

        {"python": "coder-python", "node": "coder-node", "go": "coder-go", "*": "coder"}
    """
    return _parse_json_map(raw, "BASE_CODER_AGENTS")


def parse_repo_coder_agents(raw: Optional[str]) -> dict:
    """Parse the REPO_CODER_AGENTS env var (JSON object: repo -> coder Agent name).

    Keyed by repo full name, like GATEPROFILE_MAP, because language cannot
    express this: Workload.spec.gateProfile.language is an enum
    (go|python|rust|node|generic), so every repo outside those presets is
    "generic" and BASE_CODER_AGENTS collapses them onto one coder. windowstead
    (GDScript) and pinchflat (Elixir) both need "generic" and both need a
    different runtime in the coder pod. Example:

        {"misospace/windowstead": "coder-godot"}
    """
    return _parse_json_map(raw, "REPO_CODER_AGENTS")


def parse_coder_agent_slots(raw: Optional[str]) -> dict:
    """Parse the CODER_AGENT_SLOTS env var (JSON object: coder Agent -> slot count).

    Empty or absent -> {}, which keeps the legacy issue-number split (see
    _pick_coder). Set it to make selection capacity-aware. The "*" key is the
    default for any agent not named explicitly:

        {"coder": 1, "coder-frontier": 8, "*": 1}
    """
    return _parse_json_map(raw, "CODER_AGENT_SLOTS")


def _slots_for(agent: str, slots: dict) -> int:
    """This agent's concurrent-Workload capacity: explicit entry, else the "*"
    default, else 1. A non-positive or non-integer value is treated as 1 so a
    typo cannot silently take a coder out of rotation."""
    raw = slots.get(agent, slots.get(LANE_CODER_WILDCARD, 1))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1


def free_slots(agent: str, load: dict, slots: dict) -> int:
    """Idle capacity for one coder: its slots minus the Workloads it already holds."""
    return _slots_for(agent, slots) - int(load.get(agent, 0) or 0)


def filter_fix_first(
    candidates: list,
    fix_first_agents: Optional[set] = None,
    agent_load: Optional[dict] = None,
    agent_slots: Optional[dict] = None,
) -> list:
    """Drop fix-first agents that still have fix work or a full slot.

    Fix-first agents opt into the issue rotation only when their fix lane is
    idle: no held Workloads on the fix path and a free capacity slot. An
    agent with active fix work would otherwise split its single slot between
    an in-flight fix and a fresh issue, and the fix path is supposed to keep
    that slot uncontended.

    Non-fix-first agents pass through. A fix-first agent with no declared
    slots only needs load == 0 to qualify — the slot count guard is skipped
    rather than refusing it for missing capacity config.
    """
    if not fix_first_agents or not candidates:
        return list(candidates)
    load = agent_load or {}
    slots = agent_slots or {}
    kept = []
    for agent in candidates:
        if agent not in fix_first_agents:
            kept.append(agent)
            continue
        if int(load.get(agent, 0) or 0) > 0:
            continue
        if slots and free_slots(agent, load, slots) <= 0:
            continue
        kept.append(agent)
    return kept


def coder_candidates(
    lane: str, lane_coder_agents: dict,
    agent_load: Optional[dict] = None,
    agent_slots: Optional[dict] = None,
    fix_first_agents: Optional[set] = None,
) -> list:
    """The language-agnostic candidates for a lane: its explicit mapping, else the
    wildcard. Normalized to a list so a single name and a list are handled alike.

    Only the lane tier is resolvable before an issue is claimed — the repo and
    language tiers need the claimed item — so this is what a pre-claim capacity
    check can see.

    fix_first_agents (an optional set of agent names) opts those agents out of
    the issue rotation while they still hold fix work or have a full slot, so
    the fix path's single slot stays uncontended. See issue #134.
    """
    lane_coder_agents = lane_coder_agents or {}
    value = lane_coder_agents.get(lane)
    if value is None:
        value = lane_coder_agents.get(LANE_CODER_WILDCARD)
    if value is None:
        return []
    base = list(value) if isinstance(value, list) else [value]
    return filter_fix_first(base, fix_first_agents, agent_load, agent_slots)


def coders_saturated(candidates: list, load: dict, slots: dict) -> bool:
    """True when every candidate is already at capacity.

    False when slots are unconfigured: without declared capacity there is no
    basis to hold work back, so the legacy behavior stands.
    """
    if not slots or not candidates:
        return False
    return all(free_slots(a, load or {}, slots) <= 0 for a in candidates)


def _pick_coder(value, issue_number: Optional[int], load: Optional[dict] = None,
                slots: Optional[dict] = None,
                fix_first_agents: Optional[set] = None):
    """Resolve a lane mapping that may be one Agent name or a list of them.

    With CODER_AGENT_SLOTS configured, a list resolves to the candidate with the
    most idle capacity, so work lands on a coder that can start it now instead of
    queueing behind a busy one. Candidates tied on free capacity fall back to the
    legacy `issue % len` choice, which keeps a retry on the backend that already
    holds its prompt cache.

    Without slots configured this is the historical split: by issue number,
    stateless (it survives the CronJob's per-tick world), even-ish, and
    deterministic. That split is load-oblivious, so coders of very different
    throughput still got an equal share — a single-slot local model and an
    uncapped cloud proxy each took half a lane.

    Model availability is still NOT this function's job: a down model is covered
    by litellm fallbacks at request time. This routes on declared capacity, not
    on health.

    fix_first_agents (an optional set of agent names) opts fix-first agents out
    while they still hold fix work or have a full slot, so the fix lane's
    single slot stays uncontended. A fix-first agent that has dropped out is
    skipped in the capacity ranking and the deterministic tie-break, so it
    cannot win via a saturated fallback either. See issue #134.
    """
    if not isinstance(value, list):
        return value
    if not value:
        return None
    if fix_first_agents:
        # Drop the fix-first agents that still have fix work or no free slot.
        # Without a fix-first_agents config this is a no-op.
        kept = []
        for agent in value:
            if agent in fix_first_agents:
                if (load or {}).get(agent, 0) > 0:
                    continue
                if slots and free_slots(agent, load or {}, slots) <= 0:
                    continue
            kept.append(agent)
        if not kept:
            return None
        value = kept
    if slots:
        free = [free_slots(agent, load or {}, slots) for agent in value]
        best = max(free)
        tied = [agent for agent, f in zip(value, free) if f == best]
        return tied[(issue_number or 0) % len(tied)]
    return value[(issue_number or 0) % len(value)]


def coder_agent_for(
    lane: str, language: Optional[str], lane_coder_agents: dict,
    base_coder_agents: Optional[dict] = None, repo: Optional[str] = None,
    repo_coder_agents: Optional[dict] = None, issue_number: Optional[int] = None,
    agent_load: Optional[dict] = None, agent_slots: Optional[dict] = None,
    fix_first_agents: Optional[set] = None,
) -> str:
    """Resolve a lane's coder Agent.

    Explicit per-lane mappings (e.g. the frontier escalation lane -> a
    cloud-proxy coder) are language-agnostic and win outright. Then an exact
    repo match, then the repo's language via base_coder_agents (exact match,
    then its own "*" wildcard). Falls back to the lane wildcard, then the
    hardcoded default coder.

    The repo tier exists because language cannot express a per-repo runtime:
    gateProfile.language is an enum, so GDScript and Elixir repos are both
    "generic" and share one coder. A coder without the repo's runtime cannot
    run the tests it just wrote — windowstead#321 shipped a test file that did
    not parse, because nothing executed it before the PR opened.

    Kept BELOW the lane tier deliberately: an escalation lane still overrides,
    so a repo-specific coder does not silently outrank the frontier tier. The
    consequence is that an escalated attempt loses the runtime again.

    agent_load (coder -> Workloads it currently holds) and agent_slots (coder ->
    capacity) make the choice within a tier's candidate list capacity-aware. They
    do not reorder the tiers: a tier that resolves still wins outright, so a
    busy repo-specific coder is never bypassed for a lane wildcard that lacks
    the repo's runtime.
    """
    lane_coder_agents = lane_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    repo_coder_agents = repo_coder_agents or {}
    load = agent_load or {}
    slots = agent_slots or {}
    explicit = _pick_coder(
        lane_coder_agents.get(lane), issue_number, load, slots,
        fix_first_agents=fix_first_agents,
    )
    if explicit:
        return explicit
    by_repo = (
        _pick_coder(
            repo_coder_agents.get(repo), issue_number, load, slots,
            fix_first_agents=fix_first_agents,
        ) if repo else None
    )
    if by_repo:
        return by_repo
    if base_coder_agents:
        by_lang = _pick_coder(
            base_coder_agents.get(language) or base_coder_agents.get(LANE_CODER_WILDCARD),
            issue_number, load, slots,
            fix_first_agents=fix_first_agents,
        )
        if by_lang:
            return by_lang
    return (
        _pick_coder(
            lane_coder_agents.get(LANE_CODER_WILDCARD), issue_number, load, slots,
            fix_first_agents=fix_first_agents,
        )
        or CODER_AGENT
    )


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


def _branch_name(item: ClaimedItem) -> str:
    """Deterministic task branch name matching Foreman's issues-path convention."""
    return f"foreman/{workload_name(item)}/issue-{item.issue_number}"


def _pipeline_steps(
    item: ClaimedItem, name: str, coder_agent: str, feedback: str,
    allow_overwrite: bool = False, verify_enabled: bool = True,
) -> list:
    """Explicit spec.pipeline mirroring the operator's issues-path decomposition
    (code -> verify -> review-*), with the retry feedback injected as the code
    step's payload.prompt — the only channel that reaches the coder's user
    prompt ("Issue context"). Branch naming matches the issues path so re-runs
    keep the same branch. gateProfile still propagates: the operator stamps the
    Workload default onto every rendered step.

    When verify_enabled is False, the pipeline skips the verify step entirely:
    code -> review (reviewer dependsOn code). Existing behavior when True."""
    n = item.issue_number
    branch = f"foreman/{name}/issue-{n}"
    payload = {"repo": item.repo, "issue": n, "branch": branch}
    code_name = f"code-{n}"
    steps = [
        {
            "name": code_name,
            "kind": "issue-fix",
            "agentRef": {"name": coder_agent},
            "payload": (
                {**payload, "prompt": feedback, "allowOverwrite": True}
                if allow_overwrite
                else {**payload, "prompt": feedback}
            ),
        },
    ]
    if verify_enabled:
        steps.append({
            "name": f"verify-{n}",
            "kind": "verify",
            "agentRef": {"name": VERIFIER_AGENT},
            "dependsOn": [code_name],
            "payload": dict(payload),
        })
    for i, reviewer in enumerate(REVIEWER_AGENTS):
        depends = [f"verify-{n}"] if verify_enabled else [code_name]
        steps.append({
            "name": f"review-{n}-{i}",
            "kind": "review",
            "agentRef": {"name": reviewer},
            "dependsOn": depends,
            # Explicit pipelines set openPullRequest PER STEP — unlike the
            # issues path, where the operator stamps it from
            # Workload.spec.openPullRequest (default on). Without it a retried
            # workload reviews GO and never opens a PR, stranding the reviewed
            # work on its branch (this path is only taken on retry-with-feedback).
            "payload": {**payload, "openPullRequest": True},
        })
    return steps


def build_workload(
    item: ClaimedItem,
    namespace: str,
    gate_profile: dict | None = None,
    agent_name: str = "",
    attempt: int = 1,
    coder_agent: str = CODER_AGENT,
    feedback: str = "",
    revision_coder_agent: str = "",
    verify_enabled: bool = True,
    self_go: list[str] | None = None,
    revise_from_branch: str = "",
) -> dict:
    # Overwriting a task branch is gated on EVIDENCE that prior work exists, not
    # on the attempt counter. Callers pass revise_from_branch only when the failed
    # Workload's own tasks prove a branch was pushed (see retry.branch_pushed).
    #
    # Two failure modes this avoids, both real:
    #   - Bare allowOverwrite (no reviseFromBranch) tells Foreman to cut the
    #     branch fresh from base and force-push over whatever is there. When the
    #     re-run produces an empty diff, that destroys the prior commit and
    #     autocloses the PR (#101).
    #   - reviseFromBranch pointing at a branch that was never pushed makes
    #     Foreman 0.9.14+ hard-fail the task ("revision restore failed:
    #     reviseFromBranch not found on push remote", LLMKube#1365). An attempt
    #     counter cannot tell these apart: attempt 1 can fail before it ever
    #     pushes, and an unclaim -> ready re-dispatch resets the counter to 1
    #     while the pushed branch survives.
    #
    # With no evidence we set neither field. If a stale branch does exist, the
    # push fails non-fast-forward — loud, recoverable, and PUSH-FAILED is itself
    # evidence that the NEXT retry uses to revise from the branch instead.
    allow_overwrite = bool(revise_from_branch)
    if feedback:
        # Retry with context: explicit pipeline so payload.prompt can carry the
        # previous attempt's review findings / failure to the coder.
        spec: dict[str, object] = {
            "intent": item.intent,
            "repo": item.repo,
            # Carried even though the pipeline is explicit: this is the only
            # record of which issue the Workload belongs to, and the NEXT retry
            # reconstructs its ClaimedItem from this spec. Dropping it renamed
            # the third attempt to wl-<repo>-0 on branch issue-0 — one name and
            # one branch shared by every third attempt in the repo, so retries
            # force-pushed over each other.
            "issues": [item.issue_number],
            "pipeline": _pipeline_steps(
                item, workload_name(item), coder_agent, feedback, allow_overwrite,
                verify_enabled=verify_enabled,
            ),
        }
    else:
        spec = {
            "intent": item.intent,
            "repo": item.repo,
            "issues": [item.issue_number],
            "coderAgentRef": {"name": coder_agent},
            "reviewerAgentRefs": [{"name": name} for name in REVIEWER_AGENTS],
        }
        if verify_enabled:
            spec["verifierAgentRef"] = {"name": VERIFIER_AGENT}
        if revision_coder_agent:
            spec["revisionCoderAgentRef"] = {"name": revision_coder_agent}
    if allow_overwrite:
        # Always paired: reviseFromBranch makes the executor check out the prior
        # attempt's branch, and allowOverwrite lets the push force-with-lease that
        # ref. Setting the second without the first is the #101 data-loss bug.
        spec["reviseFromBranch"] = revise_from_branch
        spec["allowOverwrite"] = True
    if self_go:
        # Passed through verbatim; the operator stamps it onto every decomposed
        # AgenticTask. Unset leaves Foreman's default policy untouched.
        spec["verdictPolicy"] = {"selfGO": list(self_go)}
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
