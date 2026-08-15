import functools
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional
from kubernetes import client, config
from bridge.env import validate_env
from bridge.logging_setup import configure as configure_logging
from bridge.models import ClaimedItem
from bridge.workload import (
    _parse_json_map,
    ISSUE_ID_ANNOTATION,
    build_workload,
    coder_agent_for,
    coder_candidates,
    coders_saturated,
    revision_coder_agent_for,
    gate_profile_for,
    parse_gate_profiles,
    parse_self_go,
    parse_lane_coder_agents,
    parse_base_coder_agents,
    parse_repo_coder_agents,
    parse_coder_agent_slots,
)
from bridge.retry import (
    reconcile_failures,
    feedback_from_tasks,
    branch_pushed,
    declared_escalation,
    DEFAULT_MAX_ATTEMPTS,
)
from bridge.prfix import (
    reconcile_pr_fixes, drain_pr_fixes, classify_check_runs, classify_pr_lifecycle,
    DEFAULT_PRFIX_LANE_AGENTS, ACTIONABLE_LANES, PRFIX_CREATED_BY,
)
from bridge.prune import prune_workloads
from bridge.reconcile import reconcile_stranded_issues, release_stuck_claims
from bridge.review_transition import transition_to_in_review

# Token redaction for error messages.
_TOKEN_RE = re.compile(
    r'(Bearer\s+)([A-Za-z0-9_\-\.]+)',
    re.IGNORECASE,
)


def _redact_token(text: str) -> str:
    """Replace Bearer tokens with *** in *text* (used for error messages)."""
    return _TOKEN_RE.sub(r"\1***", text)


logger = logging.getLogger("bridge.main")

# Single label that flags an issue as needing a human decision. Picked so the
# operator's worklist is one query: `label:needs-human is:open` — see issue #142.
NEEDS_HUMAN_LABEL = "needs-human"


def _format_escalation_comment(item: "ClaimedItem", reason: str, branch: "str | None" = None) -> str:
    """Render the comment body for a parked-for-human escalation.

    NOTE: the body deliberately does NOT use an ``@foreman`` mention form,
    even though one might seem helpful. ``foreman`` is a real GitHub account
    belonging to a person with no connection to this project; writing
    ``@foreman`` here would notify a stranger every time the loop parks
    work, and this loop parks work often. The project name collides with
    a real handle and must stay in plain text. If you are tempted to add
    a mention here, don't — see issue #142.
    """
    lines = [
        "**Needs a human decision**",
        "",
        reason,
    ]
    url = f"https://github.com/{item.repo}/issues/{item.issue_number}"
    lines.append("")
    lines.append(f"Issue: {url}")
    if branch:
        lines.append(f"Workload/branch: {branch}")
    lines.append("")
    lines.append("_Posted by the foreman-dispatch-bridge._")
    return "\n".join(lines)


ClaimOne = Callable[[str, str], Optional[ClaimedItem]]  # (agent_name, lane) -\u003e item | None

DELETE_WORKLOAD_TIMEOUT_S = int(os.environ.get("DELETE_WORKLOAD_TIMEOUT_S", "60"))


def _delete_workload(
    api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    *,
    timeout: int = DELETE_WORKLOAD_TIMEOUT_S,
) -> None:
    """Delete a Workload CR and poll until it disappears.

    Raises ``TimeoutError`` if the resource is still present after *timeout* seconds.
    """
    try:
        api.delete_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="workloads",
            name=name,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 404:  # already gone
            return
        raise
    for _ in range(timeout):
        try:
            api.get_namespaced_custom_object(
                group="foreman.llmkube.dev",
                version="v1alpha1",
                namespace=namespace,
                plural="workloads",
                name=name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise
        time.sleep(1)
    raise TimeoutError(f"workload {name} still terminating after {timeout}s")


def _parse_bool_env(raw: str, default: bool = True) -> bool:
    """Parse a boolean env var: false values include 'false', '0', 'no' (case-insensitive)."""
    stripped = raw.strip()
    if not stripped:
        return default
    if stripped.lower() in ("false", "0", "no"):
        return False
    return True


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
    repo_coder_agents: Optional[dict] = None,
    in_progress: int = 0,
    max_in_progress: int = 0,
    verify_enabled: bool = True,
    self_go: list[str] | None = None,
    agent_load: Optional[dict] = None,
    agent_slots: Optional[dict] = None,
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

    repo_coder_agents maps a repo full name -> a coder Agent name, checked after
    lane_coder_agents and before base_coder_agents. gateProfile.language is an
    enum, so every repo outside its presets is "generic" and base_coder_agents
    collapses them onto one coder; a GDScript repo and an Elixir repo both need
    "generic" and different runtimes. None/empty is unchanged behavior.

    max_in_progress (when > 0) caps how many issues are worked at once. Each lane
    is drained up to the remaining headroom: claiming continues until the lane has
    no more claimable work or in_progress reaches the cap, so a backlog fills the
    available capacity in one tick instead of one issue per tick. in_progress is
    the current count of active (non-terminal) bridge Workloads, supplied by the
    caller. Retries are not gated here (they re-run already-claimed work).

    agent_slots (coder Agent -> capacity) makes coder selection capacity-aware:
    a lane's work goes to the candidate with the most idle slots rather than to
    whichever the issue number happens to hash onto. When every candidate for a
    lane is already full, the lane does not claim at all this tick — claiming
    would only park an issue on a saturated coder and hide it from the next
    tick's routing. agent_load is the starting per-coder count; it is updated as
    Workloads are created so later claims in the same tick see the fresh load.
    Empty/absent agent_slots keeps the legacy issue-number split.
    """
    gate_profiles = gate_profiles or {}
    lane_coder_agents = lane_coder_agents or {}
    revision_coder_agents = revision_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    repo_coder_agents = repo_coder_agents or {}
    agent_slots = agent_slots or {}
    load = dict(agent_load or {})
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
            candidates = coder_candidates(lane, lane_coder_agents)
            if coders_saturated(candidates, load, agent_slots):
                # Every coder this lane can reach is full. Leave the issue
                # unclaimed so the next tick can route it once a slot frees.
                if created_here == 0:
                    busy = ",".join(sorted(candidates))
                    results.append(f"{lane}:coders-busy:{busy}")
                break
            item = claim_one(agent_name, lane)
            if item is None:
                if created_here == 0:
                    results.append(f"{lane}:empty")
                break
            language = gate_profiles.get(item.repo, {}).get("language")
            coder_agent = coder_agent_for(
                item.lane, language, lane_coder_agents, base_coder_agents,
                repo=item.repo, repo_coder_agents=repo_coder_agents,
                issue_number=item.issue_number,
                agent_load=load, agent_slots=agent_slots,
            )
            manifest = build_workload(
                item,
                namespace,
                gate_profile_for(item.repo, gate_profiles),
                agent_name,
                coder_agent=coder_agent,
                revision_coder_agent=revision_coder_agent_for(item.lane, revision_coder_agents),
                verify_enabled=verify_enabled,
                self_go=self_go,
            )
            create_workload(manifest)
            in_progress += 1
            created_here += 1
            load[coder_agent] = load.get(coder_agent, 0) + 1
            results.append(f"{lane}:created:{manifest['metadata']['name']}")
    return results


def _check_dispatch_url(base_url: str) -> None:
    """Warn if DISPATCH_URL uses cleartext HTTP (issue #54)."""
    if base_url.startswith("http://"):
        logging.warning(
            "DISPATCH_URL uses cleartext HTTP (%s); Bearer tokens are sent unencrypted. "
            "Use https:// or restrict access with network policies.",
            base_url,
        )


def _real_main() -> None:  # pragma: no cover - thin wiring, exercised in the cluster
    from bridge.claim import DispatchClient

    validate_env()
    configure_logging()

    base_url = os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000")
    _check_dispatch_url(base_url)
    token = os.environ["DISPATCH_AGENT_TOKEN"]
    agent_name = os.environ.get("DISPATCH_AGENT_NAME", "foreman/coder")
    lanes = [part.strip() for part in os.environ.get("DISPATCH_LANES", "local,cloud,frontier").split(",") if part.strip()]
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
    repo_coder_agents = parse_repo_coder_agents(os.environ.get("REPO_CODER_AGENTS"))
    # When set, exhausted Workloads outside this lane escalate into it (re-lane +
    # unclaim) instead of tombstoning. Empty disables escalation.
    escalation_lane = os.environ.get("ESCALATION_LANE", "").strip()
    verify_enabled = _parse_bool_env(os.environ.get("VERIFY_ENABLED", ""), default=True)
    self_go = parse_self_go(os.environ.get("VERDICT_SELF_GO"))
    pr_fix_enabled = os.environ.get("PR_FIX_ENABLED", "").strip().lower() in ("1", "true", "yes")
    pr_fix_max_attempts = int(os.environ.get("PR_FIX_MAX_ATTEMPTS", "3"))
    github_token = os.environ.get("GITHUB_TOKEN", "")
    _raw_lane_agents = os.environ.get("PR_FIX_LANE_AGENTS", "").strip()
    pr_fix_lane_agents = (
        _parse_json_map(_raw_lane_agents, "PR_FIX_LANE_AGENTS")
        if _raw_lane_agents
        else dict(DEFAULT_PRFIX_LANE_AGENTS)
    )
    # Terminal-Workload GC: a Completed Workload has already opened its PR (which
    # lives on GitHub), and a Failed one still Failed at prune time has been left
    # by reconcile (retries exhausted). Delete each once past its per-phase TTL so
    # terminal objects stop accumulating. Failed gets a longer TTL for triage. 0
    # disables a phase.
    prune_completed_after_h = int(os.environ.get("PRUNE_COMPLETED_AFTER_HOURS", "6"))
    prune_failed_after_h = int(os.environ.get("PRUNE_FAILED_AFTER_HOURS", "48"))

    def http_get(url, headers, allow_404=False):
        from bridge.http_retry import http_get as _http_get
        r = _http_get(url, headers=headers)
        if allow_404 and r.status_code == 404:
            return r
        r.raise_for_status()
        return r.json()

    def http_post(url, headers, payload):
        from bridge.http_retry import http_post as _http_post
        r = _http_post(url, headers=headers, json=payload)
        if r.status_code == 409:  # already claimed by another agent
            return None
        r.raise_for_status()
        return r.json()

    dispatch = DispatchClient(base_url, token, http_get, http_post)

    try:
        config.load_incluster_config()
    except Exception as e:
        raise SystemExit(f"Failed to load Kubernetes in-cluster config: {e}") from e

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

    def active_workloads() -> list:
        # Non-terminal bridge Workloads = issues currently being worked.
        terminal = {"Completed", "Failed"}
        return [
            wl for wl in list_bridge_workloads()
            if ((wl.get("status") or {}).get("phase") or "") not in terminal
        ]

    def count_active_workloads() -> int:
        # Drives the in-progress cap so claiming stops once the working set is full.
        return len(active_workloads())

    def load_by_coder_agent() -> dict:
        # Per-coder view of the same non-terminal set the cap already counts, so
        # capacity-aware routing costs no extra API call and keeps no state
        # between ticks. Workloads written before coderAgentRef was stamped
        # contribute nothing, which reads as idle rather than as a wrong agent.
        load: dict = {}
        for wl in active_workloads():
            name = ((wl.get("spec") or {}).get("coderAgentRef") or {}).get("name")
            if name:
                load[name] = load.get(name, 0) + 1
        return load

    delete_workload = functools.partial(
        _delete_workload, api, namespace, timeout=DELETE_WORKLOAD_TIMEOUT_S
    )

    def list_workload_tasks(workload_name: str) -> list:
        resp = api.list_namespaced_custom_object(
            group="foreman.llmkube.dev", version="v1alpha1",
            namespace=namespace, plural="agentictasks",
            label_selector=f"foreman.llmkube.dev/workload={workload_name}",
        )
        return resp.get("items", [])

    def declared_escalation_for(workload_name: str) -> "str | None":
        """The escalation reason the coder declared, if any. Best-effort: a lookup
        failure reports None, which leaves the normal retry path untouched."""
        try:
            return declared_escalation(list_workload_tasks(workload_name))
        except Exception as e:
            logger.warning(
                "declared-escalation-read-failed",
                extra={"workload": workload_name, "error": _redact_token(repr(e))},
            )
            return None

    def park_for_human(item: ClaimedItem, reason: str) -> bool:
        """Move a declared-escalation issue to status/backlog so the loop stops
        serving it and a human sees it in triage.

        backlog is the right resting place: the agent queue treats it as
        triage-only, so the issue is neither re-claimed nor invisible. Returns
        False on failure so the caller can fall through to a normal retry rather
        than dropping the work."""
        if not item.issue_id:
            logger.warning(
                "park-for-human-skipped-no-issue-id",
                extra={"repo": item.repo, "number": item.issue_number, "reason": reason},
            )
            return False
        payload = {
            "issueId": item.issue_id,
            "repoFullName": item.repo,
            "number": item.issue_number,
        }
        ok = dispatch.update_status(payload, "backlog", agent_name)
        label_applied = False
        comment_posted = False
        if ok:
            # Label and comment are independent best-effort operations: a
            # comment failure must not lose the label that makes the issue
            # findable on the operator's worklist. See issue #142.
            label_applied = bool(dispatch.add_label(payload, NEEDS_HUMAN_LABEL))
            comment_posted = bool(
                dispatch.post_comment(payload, _format_escalation_comment(item, reason))
            )
            logger.info(
                "parked-for-human",
                extra={
                    "repo": item.repo,
                    "number": item.issue_number,
                    "reason": reason,
                    "label_applied": label_applied,
                    "comment_posted": comment_posted,
                },
            )
        return bool(ok)

    def issue_state_for(item: ClaimedItem) -> "str | None":
        """Cached state of the workload's issue, or None when unknown.

        Best-effort by design: DispatchClient.issue_state already returns None on a
        404 or transport failure, and reconcile_failures treats None as "retry as
        normal". Only an explicit "closed" cancels a retry."""
        return dispatch.issue_state(item.repo, item.issue_number)

    def branch_pushed_for(workload_name: str, branch: str, repo: str) -> bool:
        """Did this Workload's task branch reach the remote? Drives whether the
        retry may overwrite it. Best-effort: on lookup failure report False, which
        makes the retry cut a fresh branch and (if a stale ref exists) fail loudly
        with PUSH-FAILED rather than force-push over work it cannot see.

        Grounded evidence (#132): ask the remote ``GET /repos/{repo}/branches/{branch}``
        first. A prior attempt of THIS workload must have pushed a deterministic
        ``foreman/wl-<workload>/issue-<n>`` branch; if it exists, the next
        retry may safely pair ``reviseFromBranch`` + ``allowOverwrite`` and skip
        the wasted full cycle that the prior attempt spent on a
        non-fast-forward push. On API failure fail closed (return False) and
        fall through to the task-CR scan as corroboration only — the task-CR
        scan alone misses the case where a Workload instance died without
        recording any verdict (coder Jobs killed at the deadline, agent
        restarts) but had already pushed.
        """
        tasks = list_workload_tasks(workload_name)
        try:
            url = (
                f"https://api.github.com/repos/{repo}/branches/"
                f"{urllib.parse.quote(branch, safe='/')}"
            )
            gh_headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if github_token:
                gh_headers["Authorization"] = f"Bearer {github_token}"
            r = http_get(url, headers=gh_headers, allow_404=True)
        except Exception:
            r = None
        if r is not None and getattr(r, "status_code", 0) == 200:
            return True
        try:
            return branch_pushed(tasks, remote_branch_exists=False)
        except Exception as e:
            logger.warning(
                "branch-evidence-lookup-failed",
                extra={"workload": workload_name, "error": _redact_token(repr(e))},
            )
            return False

    def feedback_for(workload_name: str) -> str:
        try:
            return feedback_from_tasks(list_workload_tasks(workload_name))
        except Exception as e:  # feedback is best-effort; never block a retry on it
            logger.warning(
                "feedback-lookup-failed",
                extra={"workload": workload_name, "error": _redact_token(repr(e))},
            )
            return ""

    def lookup_issue_id(item: ClaimedItem) -> str:
        try:
            return dispatch.find_issue_id(agent_name, lanes, item.repo, item.issue_number)
        except Exception as e:  # best-effort; missing id just means no escalation
            logger.warning(
                "issue-id-lookup-failed",
                extra={
                    "repo": item.repo,
                    "issue_number": item.issue_number,
                    "error": _redact_token(repr(e)),
                },
            )
            return ""

    def escalate(item: ClaimedItem) -> bool:
        reason = (
            f"bridge escalation: {max_attempts} failed attempts in lane "
            f"'{item.lane or '?'}' for {item.repo}#{item.issue_number}"
        )
        return dispatch.escalate(item, escalation_lane, reason, agent_name)

    # A Workload's lane label froze when it was created, so a retry cannot see a
    # lane that changed since. One pass over the queues gives every retry in this
    # tick dispatch's current view.
    try:
        current_lane_for = dispatch.lane_index(agent_name, lanes)
    except Exception as e:  # best-effort; falling back to the label is the old behavior
        current_lane_for = {}
        logger.warning("lane-index-failed", extra={"error": _redact_token(repr(e))})

    # Retry failed workloads first (so a re-run this tick uses the current config),
    # then claim new work.
    for line in reconcile_failures(
        agent_name, list_failed_workloads, create_workload, delete_workload,
        namespace, gate_profiles, max_attempts,
        escalate=escalate if escalation_lane else None,
        escalation_lane=escalation_lane,
        lane_coder_agents=lane_coder_agents,
        base_coder_agents=base_coder_agents,
        repo_coder_agents=repo_coder_agents,
        lookup_issue_id=lookup_issue_id,
        current_lane_for=current_lane_for,
        feedback_for=feedback_for,
        verify_enabled=verify_enabled,
        self_go=self_go,
        branch_pushed_for=branch_pushed_for,
        issue_state_for=issue_state_for,
        declared_escalation_for=declared_escalation_for,
        park_for_human=park_for_human,
    ):
        logger.info(line)

    # Cap concurrent in-progress work so the pipeline drains a bounded set
    # instead of claiming the whole backlog at once (0 = uncapped).
    max_in_progress = int(os.environ.get("MAX_IN_PROGRESS", "0"))
    active = count_active_workloads() if max_in_progress else 0
    coder_slots = parse_coder_agent_slots(os.environ.get("CODER_AGENT_SLOTS"))
    coder_load = load_by_coder_agent() if coder_slots else {}
    for line in run_once(
        lanes, agent_name, dispatch.claim_one, create_workload, namespace,
        gate_profiles, lane_coder_agents, revision_coder_agents,
        base_coder_agents=base_coder_agents,
        repo_coder_agents=repo_coder_agents,
        in_progress=active, max_in_progress=max_in_progress,
        verify_enabled=verify_enabled,
        self_go=self_go,
        agent_load=coder_load, agent_slots=coder_slots,
    ):
        logger.info(line)

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
                logger.warning(
                    "prfix-mark-failed",
                    extra={
                        "repo": repo,
                        "pr": pr,
                        "status": status,
                        "error": _redact_token(repr(e)),
                    },
                )
                return False

        # GitHub's own merge-state, not the fix workload's exit status, is the
        # source of truth for "did this PR actually become mergeable". Only
        # DIRTY/CONFLICTING block a FIXED mark; other states (CLEAN, UNSTABLE,
        # BEHIND, BLOCKED, UNKNOWN, ...) count as mergeable. A lookup failure
        # is treated as *not* mergeable (conservative): reconcile_pr_fixes
        # just retries under its attempt cap rather than falsely marking
        # FIXED off an unverified success, which is the bug this closes.
        def pr_is_mergeable(repo, pr) -> str:
            """Return a mergeability status string.

            Returns one of:
                "ok"             – PR is mergeable
                "dirty"          – new commits pushed
                "conflicting"    – merge conflicts
                "checks_failed"  – required checks failed / timed out / cancelled
                "checks_pending" – checks still queued or in progress
                "merged"         – PR is merged; nothing left to fix
                "closed"         – PR closed unmerged; the item is stale
            """
            headers = {"Accept": "application/vnd.github+json"}
            if github_token:
                headers["Authorization"] = f"Bearer {github_token}"

            # Fetch PR data for mergeable_state and head SHA
            try:
                data = http_get(f"https://api.github.com/repos/{repo}/pulls/{pr}", headers)
            except Exception as e:
                logger.warning(
                    "prfix-mergeable-check-failed",
                    extra={"repo": repo, "pr": pr, "error": _redact_token(repr(e))},
                )
                return "checks_pending"

            # A merged or closed PR cannot be advanced by anything the loop does.
            # Checked before mergeable_state because GitHub reports
            # mergeable_state=unknown for a merged PR, which reads as mergeable and
            # sent the fix loop through its full attempt budget — including the
            # escalation to the frontier coder — against work that had already
            # landed (#118).
            lifecycle = classify_pr_lifecycle(data)
            if lifecycle:
                return lifecycle

            state = str((data or {}).get("mergeable_state") or "").lower()

            # Existing guards for dirty/conflicting
            if state == "dirty":
                return "dirty"
            if state == "conflicting":
                return "conflicting"

            # Treat unstable (required checks failed) as checks_failed
            if state == "unstable":
                return "checks_failed"
            # blocked means required statuses are pending or other blockers
            if state == "blocked":
                return "checks_pending"

            # Additionally verify check-runs on the head commit
            try:
                head_sha = (data or {}).get("head", {}).get("sha")
                if head_sha:
                    check_runs_url = (
                        f"https://api.github.com/repos/{repo}/commits/"
                        f"{head_sha}/check-runs"
                    )
                    cr_data = http_get(check_runs_url, headers)
                    check_runs = (cr_data or {}).get("check_runs", [])

                    verdict = classify_check_runs(check_runs)
                    if verdict != "ok":
                        if not check_runs:
                            logger.info(
                                "prfix-no-check-runs",
                                extra={"repo": repo, "pr": pr, "sha": head_sha},
                            )
                        return verdict
            except Exception as exc:
                logger.error(
                    "prfix-check-runs-error",
                    extra={"repo": repo, "pr": pr, "error": repr(exc)},
                )
                # If we can't reach the API, fall through to "ok" (optimistic)

            return "ok"

        for line in reconcile_pr_fixes(
            list_prfix_workloads, delete_workload, create_workload,
            mark_pr_fix, pr_is_mergeable=pr_is_mergeable, max_attempts=pr_fix_max_attempts,
            lane_agents=pr_fix_lane_agents,
        ):
            logger.info(line)

        existing = {
            (wl.get("metadata") or {}).get("name") for wl in list_prfix_workloads()
        }
        for line in drain_pr_fixes(
            lambda: dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES)),
            existing, create_workload,
            gate_profiles, pr_fix_lane_agents, agent_name, namespace,
            verify_enabled=verify_enabled,
            self_go=self_go,
        ):
            logger.info(line)

    # Transition completed Workloads with an open PR to status/in-review.
    # Runs after claim/retry/pr-fix drains and before reconcile/prune so the
    # stranded-issue reconcile sees the updated label: a completed Workload
    # whose issue just moved to in-review is no longer "stranded in-progress"
    # and won't be reset to ready.
    for line in transition_to_in_review(
        list_bridge_workloads,
        lambda name: list_workload_tasks(name),
        lambda item, status, agent: dispatch.update_status(item, status, agent),
        agent_name,
    ):
        logger.info(line)

    # Reconcile stranded in-progress issues whose Workload no longer exists
    # (e.g. after terminal-workload GC or manual deletion). Runs before prune
    # so that any issues reset to ready can be re-claimed on the next tick.
    bridge_wl_names = {
        name for wl in list_bridge_workloads()
        if (name := (wl.get("metadata") or {}).get("name")) is not None
    }
    for line in reconcile_stranded_issues(
        dispatch, agent_name, bridge_wl_names,
    ):
        logger.info(line)

    # Release claims stuck at status/ready with no Workload behind them. Such an
    # issue is served at the head of the queue and refused on every claim, and
    # claim_one skips it rather than starving the lane — so it is invisible while
    # being permanently unreachable. Runs alongside the stranded reconcile and
    # before prune, so a released issue is claimable on the next tick.
    for line in release_stuck_claims(
        dispatch, agent_name, bridge_wl_names,
    ):
        logger.info(line)

    # Garbage-collect terminal Workloads last, after reconcile has already
    # retried anything retryable this tick — so a still-terminal Workload past
    # its TTL is genuinely done. Covers both issue (created-by=dispatch-bridge)
    # and pr-fix (created-by=dispatch-bridge-prfix) Workloads.
    def list_terminal_candidates() -> list:
        out = list(list_bridge_workloads())
        resp = api.list_namespaced_custom_object(
            group="foreman.llmkube.dev", version="v1alpha1",
            namespace=namespace, plural="workloads",
            label_selector=f"created-by={PRFIX_CREATED_BY}",
        )
        out.extend(resp.get("items", []))
        return out

    def _reset_issue(wl: dict) -> None:
        """Reset a claimed issue to ready so it can be re-claimed.

        Extracts full identity from the Workload manifest annotations + spec.
        """
        spec = wl.get("spec") or {}
        ann = (wl.get("metadata") or {}).get("annotations") or {}
        issues = spec.get("issues") or [0]
        item = {
            "issueId": ann.get(ISSUE_ID_ANNOTATION, ""),
            "repoFullName": spec.get("repo", ""),
            "number": int(issues[0]),
        }
        dispatch.update_status(item, "ready", agent_name)

    for line in prune_workloads(
        list_terminal_candidates, delete_workload,
        completed_ttl_seconds=prune_completed_after_h * 3600,
        failed_ttl_seconds=prune_failed_after_h * 3600,
        reset_issue=_reset_issue,
    ):
        logger.info(line)


if __name__ == "__main__":
    _real_main()


# ---------------------------------------------------------------------------
# BridgeRuntime: testable seam for the per-cycle k8s queries.
# ---------------------------------------------------------------------------
#
# The closures in _real_main (count_active_workloads, list_terminal_candidates,
# list_workload_tasks, etc.) all wrap a CustomObjectsApi + namespace pair. The
# helpers below take those two as arguments and are exercised directly by
# tests/test_bridge_runtime.py; BridgeRuntime is a thin wrapper that bundles
# them up for _real_main to call.
#
# Phases that indicate a Workload no longer needs reconcile attention.
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Cancelled", "Timeout"})


def _list_workloads_by_label(
    api: client.CustomObjectsApi,
    namespace: str,
    label_selector: str,
) -> List[Dict[str, Any]]:
    """Return all Workload items matching *label_selector* in *namespace*."""
    response = api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="workloads",
        label_selector=label_selector,
    )
    return list(response.get("items", []))


def _list_bridge_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads labelled with ``created-by=dispatch-bridge``."""
    return _list_workloads_by_label(api, namespace, "created-by=dispatch-bridge")


def _list_failed_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads that finished in a non-success terminal phase."""
    failed: List[Dict[str, Any]] = []
    for workload in _list_bridge_workloads(api, namespace):
        phase = workload.get("status", {}).get("phase")
        if phase in {"Failed", "Timeout", "Cancelled"}:
            failed.append(workload)
    return failed


def _active_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads that are still in progress (non-terminal phase)."""
    active: List[Dict[str, Any]] = []
    for workload in _list_bridge_workloads(api, namespace):
        phase = workload.get("status", {}).get("phase")
        if phase not in _TERMINAL_PHASES:
            active.append(workload)
    return active


def _count_active_workloads(
    api: client.CustomObjectsApi,
    namespace: str,
    *,
    include_prfix: bool = False,
    prfix_created_by: Optional[str] = None,
) -> int:
    """Count non-terminal bridge Workloads in *namespace*."""
    workloads = _active_workloads(api, namespace)
    if include_prfix and prfix_created_by:
        for workload in _list_workloads_by_label(
            api, namespace, f"created-by={prfix_created_by}"
        ):
            phase = workload.get("status", {}).get("phase")
            if phase not in _TERMINAL_PHASES:
                workloads.append(workload)
    return len(workloads)


def _load_by_coder_agent(
    api: client.CustomObjectsApi, namespace: str
) -> Dict[str, int]:
    """Count active bridge Workloads grouped by coderAgentRef.name."""
    load: Dict[str, int] = {}
    for workload in _active_workloads(api, namespace):
        ref = (
            workload.get("spec", {})
            .get("coderAgentRef", {})
            .get("name")
        )
        if not ref:
            continue
        load[ref] = load.get(ref, 0) + 1
    return load


def _list_terminal_candidates(
    api: client.CustomObjectsApi,
    namespace: str,
    prfix_created_by: str,
) -> List[Dict[str, Any]]:
    """Concatenate bridge + pr-fix Workloads for pruning.

    The caller filters by phase before deleting. The function does not
    deduplicate because Kubernetes CR names are globally unique.
    """
    bridge = _list_bridge_workloads(api, namespace)
    prfix = _list_workloads_by_label(
        api, namespace, f"created-by={prfix_created_by}"
    )
    return list(bridge) + list(prfix)


def _list_workload_tasks(
    api: client.CustomObjectsApi,
    namespace: str,
    workload_name: str,
) -> List[Dict[str, Any]]:
    """Return AgenticTask items belonging to *workload_name*."""
    response = api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="agentictasks",
        label_selector=f"foreman.llmkube.dev/workload={workload_name}",
    )
    return list(response.get("items", []))


class BridgeRuntime:
    """Wires the bridge's per-cycle k8s queries into testable methods.

    ``_real_main`` constructs a single instance and binds the closures
    previously defined inline to its methods. Tests instantiate the
    class directly with a fake API.
    """

    def __init__(
        self,
        api: client.CustomObjectsApi,
        namespace: str,
        prfix_created_by: str,
    ) -> None:
        self.api = api
        self.namespace = namespace
        self.prfix_created_by = prfix_created_by

    def list_bridge_workloads(self) -> List[Dict[str, Any]]:
        return _list_bridge_workloads(self.api, self.namespace)

    def list_failed_workloads(self) -> List[Dict[str, Any]]:
        return _list_failed_workloads(self.api, self.namespace)

    def active_workloads(self) -> List[Dict[str, Any]]:
        return _active_workloads(self.api, self.namespace)

    def count_active_workloads(self, *, include_prfix: bool = False) -> int:
        return _count_active_workloads(
            self.api,
            self.namespace,
            include_prfix=include_prfix,
            prfix_created_by=self.prfix_created_by,
        )

    def load_by_coder_agent(self) -> Dict[str, int]:
        return _load_by_coder_agent(self.api, self.namespace)

    def list_terminal_candidates(self) -> List[Dict[str, Any]]:
        return _list_terminal_candidates(
            self.api, self.namespace, self.prfix_created_by
        )

    def list_workload_tasks(self, workload_name: str) -> List[Dict[str, Any]]:
        return _list_workload_tasks(self.api, self.namespace, workload_name)

    def delete_workload(self, name: str) -> None:
        _delete_workload(
            self.api, self.namespace, name, timeout=DELETE_WORKLOAD_TIMEOUT_S
        )
