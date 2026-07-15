import json
import os
import time
from functools import partial
from typing import Any, Callable, Optional

from bridge.models import ClaimedItem
from bridge.prfix import (
    ACTIONABLE_LANES,
    DEFAULT_PRFIX_LANE_AGENTS,
    PRFIX_CREATED_BY,
    drain_pr_fixes,
    reconcile_pr_fixes,
)
from bridge.prune import prune_workloads
from bridge.retry import DEFAULT_MAX_ATTEMPTS, feedback_from_tasks, reconcile_failures
from bridge.workload import (
    build_workload,
    coder_agent_for,
    gate_profile_for,
    parse_base_coder_agents,
    parse_gate_profiles,
    parse_lane_coder_agents,
    revision_coder_agent_for,
)

ClaimOne = Callable[[str, str], Optional[ClaimedItem]]  # (agent_name, lane) -> item | None


def run_once(
    lanes: list,
    agent_name: str,
    claim_one: ClaimOne,
    create_workload: Callable[[dict], None],
    namespace: str,
    gate_profiles: Optional[dict] = None,
    lane_coder_agents: Optional[dict] = None,
    revision_coder_agents: Optional[dict] = None,
    base_coder_agents: Optional[dict] = None,
    in_progress: int = 0,
    max_in_progress: int = 0,
) -> list:
    """Claim one ready issue per lane and materialize a Workload for each. Returns per-lane outcomes.

    gate_profiles maps "owner/repo" -> a Foreman GateProfile dict; the matching
    profile (or the "*" wildcard) is stamped on each Workload so non-Go repos
    run their own language gate. None/empty leaves gateProfile off (Go default).

    lane_coder_agents maps a lane -> a coder Agent name (with "*" wildcard), so
    an escalation lane can route to a stronger (e.g. cloud-proxy) coder. Those
    mappings are language-agnostic and win outright.

    base_coder_agents maps a repo's language (via gate_profiles) -> a coder
    Agent name (with "*" wildcard), so the base lane routes a Python repo to
    coder-python, a Node repo to coder-node, etc. None/empty routes every lane
    to the default coder (legacy behavior).

    max_in_progress (when > 0) caps how many issues are worked at once. Each lane
    is drained up to the remaining headroom: claiming continues until the lane has
    no more claimable work or in_progress reaches the cap, so a backlog fills the
    available capacity in one tick instead of one issue per tick. in_progress is
    the current count of active (non-terminal) bridge Workloads, supplied by the
    caller. Retries are not gated here (they re-run already-claimed work).
    """
    gate_profiles = gate_profiles or {}
    lane_coder_agents = lane_coder_agents or {}
    revision_coder_agents = revision_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    results = []
    for lane in lanes:
        created_here = 0
        while True:
            if max_in_progress and in_progress >= max_in_progress:
                # Only flag a lane as capped if it never got to claim anything;
                # a lane that filled the headroom is recorded by its created lines.
                if created_here == 0:
                    results.append(f"{lane}:capped:{in_progress}/{max_in_progress}")
                break
            item = claim_one(agent_name, lane)
            if item is None:
                if created_here == 0:
                    results.append(f"{lane}:empty")
                break
            language = gate_profiles.get(item.repo, {}).get("language")
            manifest = build_workload(
                item,
                namespace,
                gate_profile_for(item.repo, gate_profiles),
                agent_name,
                coder_agent=coder_agent_for(item.lane, language, lane_coder_agents, base_coder_agents),
                revision_coder_agent=revision_coder_agent_for(item.lane, revision_coder_agents),
            )
            create_workload(manifest)
            in_progress += 1
            created_here += 1
            results.append(f"{lane}:created:{manifest['metadata']['name']}")
    return results


def http_get(request_get: Callable[..., Any], url: str, headers: dict) -> Any:
    response = request_get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def http_post(request_post: Callable[..., Any], url: str, headers: dict, payload: dict) -> Any:
    response = request_post(url, headers=headers, json=payload, timeout=30)
    if response.status_code == 409:  # already claimed by another agent
        return None
    response.raise_for_status()
    return response.json()


def create_workload(
    api: Any,
    namespace: str,
    manifest: dict,
    api_exception: type[Exception],
) -> None:
    try:
        api.create_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="workloads",
            body=manifest,
        )
    except api_exception as exc:
        if exc.status != 409:  # 409 = Workload already exists -> idempotent no-op
            raise


def list_bridge_workloads(api: Any, namespace: str) -> list:
    response = api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="workloads",
        label_selector="created-by=dispatch-bridge",
    )
    return response.get("items", [])


def list_failed_workloads(api: Any, namespace: str) -> list:
    return [
        workload
        for workload in list_bridge_workloads(api, namespace)
        if (workload.get("status") or {}).get("phase") == "Failed"
    ]


def count_active_workloads(api: Any, namespace: str) -> int:
    # Non-terminal bridge Workloads = issues currently being worked. Drives
    # the in-progress cap so claiming stops once the working set is full.
    terminal = {"Completed", "Failed"}
    return sum(
        1
        for workload in list_bridge_workloads(api, namespace)
        if ((workload.get("status") or {}).get("phase") or "") not in terminal
    )


def delete_workload(
    api: Any,
    namespace: str,
    name: str,
    api_exception: type[Exception],
    delete_options_factory: Callable[..., Any],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    # Foreground delete + poll: the retry recreates the same name, so the old
    # object (and its owned AgenticTasks) must be fully gone first.
    try:
        api.delete_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="workloads",
            name=name,
            body=delete_options_factory(propagation_policy="Foreground"),
        )
    except api_exception as exc:
        if exc.status == 404:  # already gone
            return
        raise
    for _ in range(60):  # up to ~60s for cascade to complete
        try:
            api.get_namespaced_custom_object(
                group="foreman.llmkube.dev",
                version="v1alpha1",
                namespace=namespace,
                plural="workloads",
                name=name,
            )
        except api_exception as exc:
            if exc.status == 404:
                return
            raise
        sleep(1)
    raise TimeoutError(f"workload {name} still terminating after 60s")


def list_workload_tasks(api: Any, namespace: str, workload_name: str) -> list:
    response = api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="agentictasks",
        label_selector=f"foreman.llmkube.dev/workload={workload_name}",
    )
    return response.get("items", [])


def feedback_for(api: Any, namespace: str, workload_name: str) -> str:
    try:
        return feedback_from_tasks(list_workload_tasks(api, namespace, workload_name))
    except Exception as exc:  # feedback is best-effort; never block a retry on it
        print(f"{workload_name}:feedback-lookup-failed:{exc}")
        return ""


def lookup_issue_id(
    dispatch: Any,
    agent_name: str,
    lanes: list,
    item: ClaimedItem,
) -> str:
    try:
        return dispatch.find_issue_id(agent_name, lanes, item.repo, item.issue_number)
    except Exception as exc:  # best-effort; missing id just means no escalation
        print(f"{item.repo}#{item.issue_number}:issue-id-lookup-failed:{exc}")
        return ""


def escalate_workload(
    dispatch: Any,
    agent_name: str,
    escalation_lane: str,
    max_attempts: int,
    item: ClaimedItem,
) -> bool:
    reason = (
        f"bridge escalation: {max_attempts} failed attempts in lane "
        f"'{item.lane or '?'}' for {item.repo}#{item.issue_number}"
    )
    return dispatch.escalate(item, escalation_lane, reason, agent_name)


def list_prfix_workloads(api: Any, namespace: str) -> list:
    response = api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="workloads",
        label_selector=f"created-by={PRFIX_CREATED_BY}",
    )
    return response.get("items", [])


def mark_pr_fix(dispatch: Any, repo: str, pr: int, status: str, note: str = "") -> bool:
    try:
        return dispatch.mark_pr_fix(repo, pr, status, note)
    except Exception as exc:  # best-effort; tombstone remains, next tick retries
        print(f"prfix-mark-failed:{repo}#{pr}:{status}:{exc}")
        return False


def pr_is_mergeable(
    request_get: Callable[..., Any],
    github_token: str,
    repo: str,
    pr: int,
) -> bool:
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        data = http_get(request_get, f"https://api.github.com/repos/{repo}/pulls/{pr}", headers)
    except Exception as exc:  # best-effort; retried next tick under the attempt cap
        print(f"prfix-mergeable-check-failed:{repo}#{pr}:{exc}")
        return False
    state = str((data or {}).get("mergeable_state") or "").lower()
    return state not in ("dirty", "conflicting")


def list_queued_pr_fixes(dispatch: Any) -> list:
    return dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES))


def _workload_name(workload: dict) -> str:
    return str((workload.get("metadata") or {}).get("name") or "")


def list_terminal_candidates(api: Any, namespace: str) -> list:
    workloads = list_bridge_workloads(api, namespace) + list_prfix_workloads(api, namespace)
    candidates = []
    seen_names = set()
    for workload in workloads:
        name = _workload_name(workload)
        if name and name in seen_names:
            continue
        if name:
            seen_names.add(name)
        candidates.append(workload)
    return candidates


def _real_main() -> None:
    import requests
    from kubernetes import client, config

    from bridge.claim import DispatchClient

    base_url = os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000")
    token = os.environ["DISPATCH_AGENT_TOKEN"]
    agent_name = os.environ.get("DISPATCH_AGENT_NAME", "foreman/coder")
    lanes = [lane.strip() for lane in os.environ.get("DISPATCH_LANES", "local,cloud,frontier").split(",") if lane.strip()]
    namespace = os.environ.get("FOREMAN_NAMESPACE", "llm")
    gate_profiles = parse_gate_profiles(os.environ.get("GATEPROFILE_MAP"))
    max_attempts = int(os.environ.get("RETRY_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS)))
    # Lane -> coder Agent map, e.g. '{"*": "coder", "frontier": "coder-frontier"}'.
    lane_coder_agents = parse_lane_coder_agents(os.environ.get("LANE_CODER_AGENTS"))
    # Lane -> revision-tuned coder Agent map (Workload.spec.revisionCoderAgentRef).
    revision_coder_agents = parse_lane_coder_agents(os.environ.get("REVISION_CODER_AGENTS"))
    # Language -> coder Agent map for the base lane, e.g.
    # '{"python": "coder-python", "node": "coder-node", "go": "coder-go", "*": "coder"}'.
    # Explicit lane_coder_agents entries (e.g. frontier) still win outright.
    base_coder_agents = parse_base_coder_agents(os.environ.get("BASE_CODER_AGENTS"))
    # When set, exhausted Workloads outside this lane escalate into it (re-lane +
    # unclaim) instead of tombstoning. Empty disables escalation.
    escalation_lane = os.environ.get("ESCALATION_LANE", "").strip()
    pr_fix_enabled = os.environ.get("PR_FIX_ENABLED", "").strip().lower() in ("1", "true", "yes")
    pr_fix_max_attempts = int(os.environ.get("PR_FIX_MAX_ATTEMPTS", "3"))
    github_token = os.environ.get("GITHUB_TOKEN", "")
    raw_lane_agents = os.environ.get("PR_FIX_LANE_AGENTS", "").strip()
    pr_fix_lane_agents = json.loads(raw_lane_agents) if raw_lane_agents else dict(DEFAULT_PRFIX_LANE_AGENTS)
    # Terminal-Workload GC: a Completed Workload has already opened its PR (which
    # lives on GitHub), and a Failed one still Failed at prune time has been left
    # by reconcile (retries exhausted). Delete each once past its per-phase TTL so
    # terminal objects stop accumulating. Failed gets a longer TTL for triage. 0
    # disables a phase.
    prune_completed_after_h = int(os.environ.get("PRUNE_COMPLETED_AFTER_HOURS", "6"))
    prune_failed_after_h = int(os.environ.get("PRUNE_FAILED_AFTER_HOURS", "48"))

    dispatch = DispatchClient(
        base_url,
        token,
        partial(http_get, requests.get),
        partial(http_post, requests.post),
    )

    config.load_incluster_config()
    api = client.CustomObjectsApi()
    create = partial(create_workload, api, namespace, api_exception=client.exceptions.ApiException)
    list_failed = partial(list_failed_workloads, api, namespace)
    count_active = partial(count_active_workloads, api, namespace)
    delete = partial(
        delete_workload,
        api,
        namespace,
        api_exception=client.exceptions.ApiException,
        delete_options_factory=client.V1DeleteOptions,
    )
    feedback = partial(feedback_for, api, namespace)
    lookup = partial(lookup_issue_id, dispatch, agent_name, lanes)
    escalate = partial(escalate_workload, dispatch, agent_name, escalation_lane, max_attempts)

    # Retry failed workloads first (so a re-run this tick uses the current config),
    # then claim new work.
    for line in reconcile_failures(
        agent_name,
        list_failed,
        create,
        delete,
        namespace,
        gate_profiles,
        max_attempts,
        escalate=escalate if escalation_lane else None,
        escalation_lane=escalation_lane,
        lane_coder_agents=lane_coder_agents,
        base_coder_agents=base_coder_agents,
        lookup_issue_id=lookup,
        feedback_for=feedback,
    ):
        print(line)

    # Cap concurrent in-progress work so the pipeline drains a bounded set
    # instead of claiming the whole backlog at once (0 = uncapped).
    max_in_progress = int(os.environ.get("MAX_IN_PROGRESS", "0"))
    active = count_active() if max_in_progress else 0
    for line in run_once(
        lanes,
        agent_name,
        dispatch.claim_one,
        create,
        namespace,
        gate_profiles,
        lane_coder_agents,
        revision_coder_agents,
        base_coder_agents=base_coder_agents,
        in_progress=active,
        max_in_progress=max_in_progress,
    ):
        print(line)

    if pr_fix_enabled:
        list_prfix = partial(list_prfix_workloads, api, namespace)
        mark = partial(mark_pr_fix, dispatch)
        mergeable = partial(pr_is_mergeable, requests.get, github_token)

        # GitHub's own merge-state, not the fix workload's exit status, is the
        # source of truth for "did this PR actually become mergeable". Only
        # DIRTY/CONFLICTING block a FIXED mark; other states (CLEAN, UNSTABLE,
        # BEHIND, BLOCKED, UNKNOWN, ...) count as mergeable. A lookup failure
        # is treated as *not* mergeable (conservative): reconcile_pr_fixes
        # just retries under its attempt cap rather than falsely marking
        # FIXED off an unverified success, which is the bug this closes.
        for line in reconcile_pr_fixes(
            list_prfix,
            delete,
            create,
            mark,
            pr_is_mergeable=mergeable,
            max_attempts=pr_fix_max_attempts,
            lane_agents=pr_fix_lane_agents,
        ):
            print(line)

        existing = {(workload.get("metadata") or {}).get("name") for workload in list_prfix()}
        for line in drain_pr_fixes(
            partial(list_queued_pr_fixes, dispatch),
            existing,
            create,
            gate_profiles,
            pr_fix_lane_agents,
            agent_name,
            namespace,
        ):
            print(line)

    # Garbage-collect terminal Workloads last, after reconcile has already
    # retried anything retryable this tick — so a still-terminal Workload past
    # its TTL is genuinely done. Covers both issue (created-by=dispatch-bridge)
    # and pr-fix (created-by=dispatch-bridge-prfix) Workloads.
    for line in prune_workloads(
        partial(list_terminal_candidates, api, namespace),
        delete,
        completed_ttl_seconds=prune_completed_after_h * 3600,
        failed_ttl_seconds=prune_failed_after_h * 3600,
    ):
        print(line)


if __name__ == "__main__":
    _real_main()
