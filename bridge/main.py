import os
import time
from typing import Callable, Optional
from bridge.models import ClaimedItem
from bridge.workload import (
    build_workload,
    coder_agent_for,
    revision_coder_agent_for,
    gate_profile_for,
    parse_gate_profiles,
    parse_lane_coder_agents,
    parse_base_coder_agents,
)
from bridge.retry import reconcile_failures, feedback_from_tasks, DEFAULT_MAX_ATTEMPTS
from bridge.prfix import (
    reconcile_pr_fixes, drain_pr_fixes, prfix_workload_name,
    DEFAULT_PRFIX_LANE_AGENTS, ACTIONABLE_LANES, PRFIX_CREATED_BY,
)
import json

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

    max_in_progress (when > 0) caps how many issues are worked at once: claiming
    stops once in_progress reaches the cap, so the pipeline works a bounded set
    instead of the whole backlog. in_progress is the current count of active
    (non-terminal) bridge Workloads, supplied by the caller. Retries are not
    gated here (they re-run already-claimed work).
    """
    gate_profiles = gate_profiles or {}
    lane_coder_agents = lane_coder_agents or {}
    revision_coder_agents = revision_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    results = []
    for lane in lanes:
        if max_in_progress and in_progress >= max_in_progress:
            results.append(f"{lane}:capped:{in_progress}/{max_in_progress}")
            continue
        item = claim_one(agent_name, lane)
        if item is None:
            results.append(f"{lane}:empty")
            continue
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
        results.append(f"{lane}:created:{manifest['metadata']['name']}")
    return results


def _real_main() -> None:  # pragma: no cover - thin wiring, exercised in the cluster
    import requests
    from kubernetes import client, config
    from bridge.claim import DispatchClient

    base_url = os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000")
    token = os.environ["DISPATCH_AGENT_TOKEN"]
    agent_name = os.environ.get("DISPATCH_AGENT_NAME", "foreman/coder")
    lanes = [l.strip() for l in os.environ.get("DISPATCH_LANES", "local,cloud,frontier").split(",") if l.strip()]
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
    _raw_lane_agents = os.environ.get("PR_FIX_LANE_AGENTS", "").strip()
    pr_fix_lane_agents = json.loads(_raw_lane_agents) if _raw_lane_agents else dict(DEFAULT_PRFIX_LANE_AGENTS)

    def http_get(url, headers):
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def http_post(url, headers, payload):
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 409:  # already claimed by another agent
            return None
        r.raise_for_status()
        return r.json()

    dispatch = DispatchClient(base_url, token, http_get, http_post)

    config.load_incluster_config()
    api = client.CustomObjectsApi()

    def create_workload(manifest: dict) -> None:
        try:
            api.create_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=namespace, plural="workloads", body=manifest,
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:  # 409 = Workload already exists -> idempotent no-op
                raise

    def list_bridge_workloads() -> list:
        resp = api.list_namespaced_custom_object(
            group="foreman.llmkube.dev", version="v1alpha1",
            namespace=namespace, plural="workloads",
            label_selector="created-by=dispatch-bridge",
        )
        return resp.get("items", [])

    def list_failed_workloads() -> list:
        return [
            wl for wl in list_bridge_workloads()
            if (wl.get("status") or {}).get("phase") == "Failed"
        ]

    def count_active_workloads() -> int:
        # Non-terminal bridge Workloads = issues currently being worked. Drives
        # the in-progress cap so claiming stops once the working set is full.
        terminal = {"Completed", "Failed"}
        return sum(
            1 for wl in list_bridge_workloads()
            if ((wl.get("status") or {}).get("phase") or "") not in terminal
        )

    def delete_workload(name: str) -> None:
        # Foreground delete + poll: the retry recreates the same name, so the old
        # object (and its owned AgenticTasks) must be fully gone first.
        try:
            api.delete_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=namespace, plural="workloads", name=name,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:  # already gone
                return
            raise
        for _ in range(60):  # up to ~60s for cascade to complete
            try:
                api.get_namespaced_custom_object(
                    group="foreman.llmkube.dev", version="v1alpha1",
                    namespace=namespace, plural="workloads", name=name,
                )
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(1)
        raise TimeoutError(f"workload {name} still terminating after 60s")

    def list_workload_tasks(workload_name: str) -> list:
        resp = api.list_namespaced_custom_object(
            group="foreman.llmkube.dev", version="v1alpha1",
            namespace=namespace, plural="agentictasks",
            label_selector=f"foreman.llmkube.dev/workload={workload_name}",
        )
        return resp.get("items", [])

    def feedback_for(workload_name: str) -> str:
        try:
            return feedback_from_tasks(list_workload_tasks(workload_name))
        except Exception as e:  # feedback is best-effort; never block a retry on it
            print(f"{workload_name}:feedback-lookup-failed:{e}")
            return ""

    def lookup_issue_id(item: ClaimedItem) -> str:
        try:
            return dispatch.find_issue_id(agent_name, lanes, item.repo, item.issue_number)
        except Exception as e:  # best-effort; missing id just means no escalation
            print(f"{item.repo}#{item.issue_number}:issue-id-lookup-failed:{e}")
            return ""

    def escalate(item: ClaimedItem) -> bool:
        reason = (
            f"bridge escalation: {max_attempts} failed attempts in lane "
            f"'{item.lane or '?'}' for {item.repo}#{item.issue_number}"
        )
        return dispatch.escalate(item, escalation_lane, reason, agent_name)

    # Retry failed workloads first (so a re-run this tick uses the current config),
    # then claim new work.
    for line in reconcile_failures(
        agent_name, list_failed_workloads, create_workload, delete_workload,
        namespace, gate_profiles, max_attempts,
        escalate=escalate if escalation_lane else None,
        escalation_lane=escalation_lane,
        lane_coder_agents=lane_coder_agents,
        base_coder_agents=base_coder_agents,
        lookup_issue_id=lookup_issue_id,
        feedback_for=feedback_for,
    ):
        print(line)

    # Cap concurrent in-progress work so the pipeline drains a bounded set
    # instead of claiming the whole backlog at once (0 = uncapped).
    max_in_progress = int(os.environ.get("MAX_IN_PROGRESS", "0"))
    active = count_active_workloads() if max_in_progress else 0
    for line in run_once(
        lanes, agent_name, dispatch.claim_one, create_workload, namespace,
        gate_profiles, lane_coder_agents, revision_coder_agents,
        base_coder_agents=base_coder_agents,
        in_progress=active, max_in_progress=max_in_progress,
    ):
        print(line)

    if pr_fix_enabled:
        def list_prfix_workloads() -> list:
            resp = api.list_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=namespace, plural="workloads",
                label_selector=f"created-by={PRFIX_CREATED_BY}",
            )
            return resp.get("items", [])

        def mark_pr_fix(repo, pr, status, note=""):
            try:
                return dispatch.mark_pr_fix(repo, pr, status, note)
            except Exception as e:  # best-effort; tombstone remains, next tick retries
                print(f"prfix-mark-failed:{repo}#{pr}:{status}:{e}")
                return False

        # GitHub's own merge-state, not the fix workload's exit status, is the
        # source of truth for "did this PR actually become mergeable". Only
        # DIRTY/CONFLICTING block a FIXED mark; other states (CLEAN, UNSTABLE,
        # BEHIND, BLOCKED, UNKNOWN, ...) count as mergeable. A lookup failure
        # is treated as *not* mergeable (conservative): reconcile_pr_fixes
        # just retries under its attempt cap rather than falsely marking
        # FIXED off an unverified success, which is the bug this closes.
        def pr_is_mergeable(repo, pr) -> bool:
            headers = {"Accept": "application/vnd.github+json"}
            if github_token:
                headers["Authorization"] = f"Bearer {github_token}"
            try:
                data = http_get(f"https://api.github.com/repos/{repo}/pulls/{pr}", headers)
            except Exception as e:  # best-effort; retried next tick under the attempt cap
                print(f"prfix-mergeable-check-failed:{repo}#{pr}:{e}")
                return False
            state = str((data or {}).get("mergeable_state") or "").lower()
            return state not in ("dirty", "conflicting")

        for line in reconcile_pr_fixes(
            list_prfix_workloads, delete_workload, create_workload,
            mark_pr_fix, pr_is_mergeable=pr_is_mergeable, max_attempts=pr_fix_max_attempts,
        ):
            print(line)

        existing = {
            (wl.get("metadata") or {}).get("name") for wl in list_prfix_workloads()
        }
        for line in drain_pr_fixes(
            lambda: dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES)),
            existing, create_workload,
            gate_profiles, pr_fix_lane_agents, agent_name, namespace,
        ):
            print(line)


if __name__ == "__main__":
    _real_main()
