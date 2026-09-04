import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional
from kubernetes import client, config
from bridge.env import validate_env
from bridge.logging_setup import configure as configure_logging
from bridge.models import ClaimedItem
from bridge.workload import (
    _parse_json_map,
    ISSUE_ID_ANNOTATION,
    _branch_name,
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
    INFRA_BLOCKED_LABEL,
    INFRA_MODEL_LABEL_PREFIX,
    INFRA_ATTEMPT_LABEL_PREFIX,
    INFRA_RECOVERY_MAX_FAILURES,
    failed_model,
    claimed_item_from_issue,
    reconcile_infra_parked,
    infra_marker_labels,
)
from bridge.prfix import (
    reconcile_pr_fixes, drain_pr_fixes,
    DEFAULT_PRFIX_LANE_AGENTS, ACTIONABLE_LANES, PRFIX_CREATED_BY,
    FAILING_CONCLUSIONS, PENDING_STATUSES, failure_signature,
)
from bridge.prune import prune_workloads, stamp_terminal_since, terminal_since_key
from bridge.reconcile import reconcile_stranded_issues, release_stuck_claims
from bridge.review_transition import transition_to_in_review
from bridge.http_retry import _redact_token, _retry_k8s_request


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


ClaimOne = Callable[..., Optional[ClaimedItem]]  # (agent_name, lane) -\u003e item | None

def _delete_workload(
    api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    *,
    timeout: int = 60,
) -> None:
    """Delete a Workload CR and poll until it disappears.

    Raises ``TimeoutError`` if the resource is still present after *timeout* seconds.
    """
    try:
        _retry_k8s_request(lambda: api.delete_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="workloads",
            name=name,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        ))
    except client.exceptions.ApiException as exc:
        if exc.status == 404:  # already gone
            return
        raise
    for _ in range(timeout):
        try:
            _retry_k8s_request(lambda: api.get_namespaced_custom_object(
                group="foreman.llmkube.dev",
                version="v1alpha1",
                namespace=namespace,
                plural="workloads",
                name=name,
            ))
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


def _parse_fix_first_agents(raw: Optional[str]) -> set:
    """Parse the FIX_FIRST_AGENTS env var into a set of agent names.

    Accepts a JSON list (`["coder", "fixer"]`), a comma-separated string
    (`coder,fixer`), whitespace-separated names, or a single bare name
    (`coder`). Empty/missing returns an empty set, so callers can pass the
    result through unconditionally and treat an unset env as the historical
    "every coder is always in the rotation" behavior.

    Unparseable JSON falls back to a comma/whitespace split, which is what
    an operator is most likely to have typed in a Deployment.
    """
    if not raw:
        return set()
    text = raw.strip()
    if not text:
        return set()
    # Try JSON first: it is the documented form in the issue sketch.
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return {str(name).strip() for name in parsed if str(name).strip()}
        except (ValueError, TypeError):
            pass
    # Fall back to a delimiter split. Comma is the most common operator
    # form; whitespace is the loose reading for "coder fixer" or
    # newline-separated lists.
    parts = [p.strip().strip('"\'') for p in re.split(r"[,\s]+", text) if p.strip()]
    return {p for p in parts if p}



def update_pull_request_branch(repo, pr, *, http_put, github_token) -> bool:
    """Merge the base branch into a conflicting PR's head branch.

    The cheap half of #269: most conflicts on a foreman branch are just the
    base having moved, and GitHub resolves those server-side for free. A real
    content conflict returns 422, which is the signal to let a coder rebase it
    instead. Never raises — any failure here means the caller falls through to
    the coder path, which is the correct outcome for every reason this could
    not be done.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        r = http_put(
            f"https://api.github.com/repos/{repo}/pulls/{pr}/update-branch",
            headers=headers,
            json={},
        )
    except Exception as e:
        logger.info(
            "prfix-branch-update-failed",
            extra={"repo": repo, "pr": pr, "error": _redact_token(repr(e))},
        )
        return False
    if r.status_code in (200, 202):
        logger.info("prfix-branch-updated", extra={"repo": repo, "pr": pr})
        return True
    logger.info(
        "prfix-branch-update-declined",
        extra={"repo": repo, "pr": pr, "status": r.status_code},
    )
    return False


def check_pr_mergeable(repo, pr, *, http_get, github_token) -> str:
    """Return a mergeability status string.

    Returns one of:
        "ok"             – PR is mergeable
        "dirty"          – new commits pushed
        "conflicting"    – merge conflicts
        "checks_failed"  – required checks failed / timed out / cancelled
        "checks_pending" – checks still queued or in progress
        "merged"         – PR is merged; nothing left to fix
        "closed"         – PR closed unmerged; the item is stale

    A lookup failure (PR-data fetch or check-runs fetch) is treated as *not*
    mergeable: we return "checks_pending" so reconcile_pr_fixes just retries
    under its attempt cap rather than falsely marking FIXED off an unverified
    success (#93).
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

    if data.get("merged"):
        return "merged"
    if data.get("state") == "closed":
        return "closed"

    state = str((data or {}).get("mergeable_state") or "").lower()

    if state == "dirty":
        return "dirty"
    if state == "conflicting":
        return "conflicting"

    # GitHub computes mergeable_state asynchronously and reports "unknown"
    # (with mergeable: null) while that is in flight, which is exactly when we
    # poll: right after a push or a review. Every state this function does not
    # name falls through to "ok" at the bottom, so an indeterminate answer used
    # to read as a clean PR — reconcile then marked the item FIXED on a PR
    # whose mergeability was never established. Both alert-triage#87 and
    # llmkube-images#237 were marked FIXED while carrying a standing
    # CHANGES_REQUESTED review, and FIXED is terminal, so nothing re-queued
    # them.
    #
    # Treat it the way a failed check-runs lookup is already treated (#93):
    # not mergeable, retry next tick, do not burn an attempt. Uncertainty must
    # not resolve to success on the path that marks work complete.
    if state in ("unknown", ""):
        logger.info(
            "prfix-merge-state-unknown",
            extra={"repo": repo, "pr": pr, "mergeable": (data or {}).get("mergeable")},
        )
        return "checks_pending"

    # For unstable/blocked, distinguish a failing check (fixable by a coder)
    # from a non-check blocker (awaiting review, etc.) by looking at the
    # check-runs on the head commit. Returning early on "blocked" with
    # "checks_pending" without consulting check-runs caused #163: a PR with
    # mergeStateStatus=BLOCKED caused by a failing check was misread as a
    # pending-checks state, burned three attempts, and was abandoned.
    inspect_blockers = state in ("unstable", "blocked")

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

            has_failed = False
            has_pending = False
            saw_any = False
            for cr in check_runs or []:
                saw_any = True
                conclusion = str((cr or {}).get("conclusion") or "").lower()
                status = str((cr or {}).get("status") or "").lower()
                if conclusion in FAILING_CONCLUSIONS:
                    has_failed = True
                elif status in PENDING_STATUSES or (not conclusion and status != "completed"):
                    has_pending = True

            if has_failed:
                return "checks_failed"
            if has_pending:
                return "checks_pending"
            if not saw_any:
                logger.info(
                    "prfix-no-check-runs",
                    extra={"repo": repo, "pr": pr, "sha": head_sha},
                )
    except Exception as exc:
        logger.error(
            "prfix-check-runs-error",
            extra={"repo": repo, "pr": pr, "error": repr(exc)},
        )
        # A lookup failure is treated as not mergeable (conservative): #93
        # reconcile_pr_fixes just retries under its attempt cap rather
        # than falsely marking FIXED off an unverified success.
        return "checks_pending"

    # If mergeable_state is "unstable" or "blocked" with no failing check
    # to blame (no failing check, no pending check, no checks at all), the
    # PR is blocked for a non-check reason (e.g. awaiting required review,
    # merge queue not ready). The coder cannot resolve this; do not burn
    # retry attempts on it. (#163)
    if inspect_blockers:
        # BLOCKED with no failing or pending check is ambiguous: it covers both
        # "awaiting a required reviewer / merge queue" (the coder cannot help)
        # and "a reviewer requested changes" (the coder is exactly what should
        # act). Treating both as "blocked" parked PRs forever holding a slot
        # while actionable feedback sat unread. Ask the reviews which it is.
        try:
            reviews = http_get(
                f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews?per_page=100",
                headers,
            )
            # Last decisive review per reviewer wins; COMMENTED and DISMISSED
            # carry no verdict, matching GitHub's own review-decision rule.
            latest: dict[str, str] = {}
            for rv in reviews or []:
                st = str((rv or {}).get("state") or "").upper()
                if st not in ("APPROVED", "CHANGES_REQUESTED"):
                    continue
                who = str(((rv or {}).get("user") or {}).get("login") or "")
                if who:
                    latest[who] = st
            if "CHANGES_REQUESTED" in latest.values():
                return "changes_requested"
        except Exception as exc:
            logger.error(
                "prfix-reviews-error",
                extra={"repo": repo, "pr": pr, "error": repr(exc)},
            )
            # Unknown: fall through to "blocked" so an API failure parks the PR
            # rather than burning attempts on a guess.
        return "blocked"

    return "ok"


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
    fix_first_agents: Optional[set] = None,
    queue_for: Optional[Callable[[str], list]] = None,
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
            candidates = coder_candidates(
                lane, lane_coder_agents,
                agent_load=load, agent_slots=agent_slots,
                fix_first_agents=fix_first_agents,
            )
            if coders_saturated(candidates, load, agent_slots):
                # Every coder this lane can reach is full. Leave the issue
                # unclaimed so the next tick can route it once a slot frees.
                if created_here == 0:
                    busy = ",".join(sorted(candidates))
                    results.append(f"{lane}:coders-busy:{busy}")
                break
            item = claim_one(agent_name, lane, queue_for=queue_for)
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
                fix_first_agents=fix_first_agents,
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


# ---------------------------------------------------------------------------
# Shared per-coder-load helpers.
# ---------------------------------------------------------------------------
#
# These are defined before _real_main because the production load_by_coder_agent
# closure delegates to them and _real_main runs at module load (the if __name__
# block) — anything it calls must already be bound. They double as the testable
# seam exercised by tests/test_bridge_runtime.py and bundled by BridgeRuntime.
#
# Phases that indicate a Workload no longer needs reconcile attention.
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Cancelled", "Timeout", "Completed"})

# Phases at which an AgenticTask is done. "Completed" is task-terminal even
# while its enclosing Workload is still reconciling.
_TASK_TERMINAL_PHASES = frozenset(
    {"Succeeded", "Failed", "Cancelled", "Timeout", "Completed"}
)

# Label linking an AgenticTask back to the Workload that owns it.
_TASK_WORKLOAD_LABEL = "foreman.llmkube.dev/workload"


def _list_workloads_by_label(
    api: client.CustomObjectsApi,
    namespace: str,
    label_selector: str,
) -> List[Dict[str, Any]]:
    """Return all Workload items matching *label_selector* in *namespace*."""
    response = _retry_k8s_request(lambda: api.list_namespaced_custom_object(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace=namespace,
        plural="workloads",
        label_selector=label_selector,
    ))
    return list(response.get("items", []))


def _list_bridge_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads labelled with ``created-by=dispatch-bridge``."""
    return _list_workloads_by_label(api, namespace, "created-by=dispatch-bridge")


def _list_agentic_tasks_by_workload(
    api: client.CustomObjectsApi, namespace: str
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """List AgenticTasks once and group them by their Workload label.

    ``None`` means the task list could not be resolved. Callers use that result
    to fail closed and keep every coder ref busy for the tick.
    """
    try:
        response = _retry_k8s_request(lambda: api.list_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="agentictasks",
            label_selector=_TASK_WORKLOAD_LABEL,
        ))
        items = response.get("items", [])
        if not isinstance(items, list):
            return None
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for task in items:
            if not isinstance(task, dict):
                return None
            metadata = task.get("metadata") or {}
            labels = metadata.get("labels") or {}
            if not isinstance(metadata, dict) or not isinstance(labels, dict):
                return None
            workload_name = labels.get(_TASK_WORKLOAD_LABEL)
            if not workload_name:
                return None
            if not isinstance(task.get("spec"), dict):
                return None
            if not isinstance(task.get("status"), dict):
                return None
            grouped.setdefault(workload_name, []).append(task)
        return grouped
    except Exception:
        return None


def _coder_agent_name(workload: Dict[str, Any]) -> Optional[str]:
    """Return a Workload's coder ref, or None when it cannot be resolved."""
    spec = workload.get("spec") or {}
    if not isinstance(spec, dict):
        return None
    ref = spec.get("coderAgentRef") or {}
    if not isinstance(ref, dict):
        return None
    return ref.get("name")


def _active_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads that are still in progress (non-terminal phase)."""
    active: List[Dict[str, Any]] = []
    for workload in _list_bridge_workloads(api, namespace):
        phase = (workload.get("status") or {}).get("phase")
        if phase not in _TERMINAL_PHASES:
            active.append(workload)
    return active


def _coder_still_busy(tasks: List[Dict[str, Any]]) -> bool:
    """Fail-closed busy check for one Workload's tasks (#180).

    Returns ``True`` (busy) unless the Workload has at least one ``issue-fix``
    task and every one of them is in a terminal phase — a running review task
    does NOT keep the coder busy. Task-list parsing rejects missing labels and
    malformed objects before this helper runs; missing or unknown issue-fix
    phases remain busy here.
    """
    issue_fix: List[Dict[str, Any]] = []
    for task in tasks:
        spec = task.get("spec")
        if not isinstance(spec, dict):
            return True
        if spec.get("kind") != "issue-fix":
            continue
        status = task.get("status")
        if not isinstance(status, dict):
            return True
        issue_fix.append(task)
    if not issue_fix:
        return True
    return any(
        task["status"].get("phase") not in _TASK_TERMINAL_PHASES
        for task in issue_fix
    )


def _load_by_coder_agent(
    api: client.CustomObjectsApi,
    namespace: str,
    *,
    workloads: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, int]:
    """Count active bridge Workloads grouped by coderAgentRef.name.

    A Workload keeps counting toward an agent's load until its ``issue-fix``
    task(s) all reach a terminal phase; a running review no longer holds the
    coder busy (#180). Bridge Workloads are listed once and AgenticTasks are
    listed once (selector ``foreman.llmkube.dev/workload``) then grouped in
    memory by that metadata label, so capacity-aware routing never becomes a
    per-Workload task query. Fails closed: a missing/unknown task phase, a task
    missing the workload label, a Workload with no ``issue-fix`` task, or a
    task-list exception all count the coder ref as busy. Workloads written
    before coderAgentRef was stamped contribute nothing.
    """
    def _refs(workloads: List[Dict[str, Any]]) -> Dict[str, int]:
        load: Dict[str, int] = {}
        for workload in workloads:
            ref = _coder_agent_name(workload)
            if ref:
                load[ref] = load.get(ref, 0) + 1
        return load

    workloads = _active_workloads(api, namespace) if workloads is None else workloads
    tasks_by_workload = _list_agentic_tasks_by_workload(api, namespace)
    if tasks_by_workload is None:
        return _refs(workloads)

    load: Dict[str, int] = {}
    for workload in workloads:
        ref = _coder_agent_name(workload)
        if not ref:
            continue
        metadata = workload.get("metadata") or {}
        wl_name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(wl_name, str) or _coder_still_busy(
            tasks_by_workload.get(wl_name, [])
        ):
            load[ref] = load.get(ref, 0) + 1
    return load


@dataclass
class TickConfig:
    """Everything the tick needs from the environment.

    Built once by _real_main so run_tick takes no implicit inputs and can be
    driven from a test. See issue #199: the orchestration used to read os.environ
    directly, which is why nothing could invoke it.
    """
    agent_name: str
    lanes: List[str]
    namespace: str
    gate_profiles: dict
    max_attempts: int
    lane_coder_agents: dict
    revision_coder_agents: dict
    base_coder_agents: dict
    repo_coder_agents: dict
    escalation_lane: str
    verify_enabled: bool
    self_go: List[str]
    pr_fix_enabled: bool
    pr_fix_max_attempts: int
    github_token: str
    pr_fix_lane_agents: dict
    prune_completed_after_h: int
    prune_failed_after_h: int
    max_in_progress: int
    coder_slots: dict
    fix_first_agents: set
    delete_workload_timeout_s: int = 60


def run_tick(
    api,
    dispatch,
    cfg: TickConfig,
    http_get: Callable,
    probe_model: Optional[Callable[[str], bool]] = None,
) -> None:
    """Run one reconcile cycle. All k8s and dispatch access goes through the
    two injected clients, so a test can supply fakes and assert the call
    sequence."""

    def create_workload(manifest: dict) -> None:
        try:
            _retry_k8s_request(lambda: api.create_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=cfg.namespace, plural="workloads", body=manifest,
            ))
        except client.exceptions.ApiException as e:
            if e.status != 409:  # 409 = Workload already exists -> idempotent no-op
                raise

    # The behaviours below used to live as closures inlined here; they are now
    # sourced from a single BridgeRuntime instance so production and tests share
    # one implementation (#199).
    bridge = BridgeRuntime(api=api, namespace=cfg.namespace, prfix_created_by=PRFIX_CREATED_BY, delete_workload_timeout_s=cfg.delete_workload_timeout_s)
    list_bridge_workloads = bridge.list_bridge_workloads
    list_failed_workloads = bridge.list_failed_workloads
    count_active_workloads = bridge.count_active_workloads
    load_by_coder_agent = bridge.load_by_coder_agent
    delete_workload = bridge.delete_workload
    list_workload_tasks = bridge.list_workload_tasks

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
        """Move a declared-escalation issue to status/backlog and announce it.

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
        ok = dispatch.update_status(payload, "backlog", cfg.agent_name)
        label_applied = False
        comment_posted = False
        if ok:
            # Label and comment are independent best-effort operations: a
            # comment failure must not lose the label that makes the issue
            # findable on the operator's worklist. See issue #142. Their
            # exceptions must not propagate past a successful status change
            # either — otherwise the retry wrapper would log the whole park
            # as failed even though the status change that actually unpins
            # the issue already landed (issue #162).
            try:
                label_applied = bool(
                    dispatch.add_label(payload, NEEDS_HUMAN_LABEL)
                )
            except Exception:
                logger.exception(
                    "park-for-human-label-failed",
                    extra={
                        "repo": item.repo,
                        "number": item.issue_number,
                        "reason": reason,
                    },
                )
            try:
                comment_posted = bool(
                    dispatch.post_comment(
                        payload, _format_escalation_comment(item, reason)
                    )
                )
            except Exception:
                logger.exception(
                    "park-for-human-comment-failed",
                    extra={
                        "repo": item.repo,
                        "number": item.issue_number,
                        "reason": reason,
                    },
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

    def parked_for_human(item: ClaimedItem) -> "bool | None":
        """Return the cached needs-human marker used to suppress duplicate posts."""
        if not item.issue_id:
            return None
        return dispatch.issue_is_parked(item.repo, item.issue_number, NEEDS_HUMAN_LABEL)

    def ensure_human_label(item: ClaimedItem) -> bool:
        """Repair the durable marker without posting another escalation comment."""
        if not item.issue_id:
            return False
        payload = {
            "issueId": item.issue_id,
            "repoFullName": item.repo,
            "number": item.issue_number,
        }
        try:
            return bool(dispatch.add_label(payload, NEEDS_HUMAN_LABEL))
        except Exception:
            logger.exception(
                "park-for-human-label-failed",
                extra={"repo": item.repo, "number": item.issue_number},
            )
            return False

    def model_for_agent(agent_name: str) -> str:
        try:
            response = _retry_k8s_request(lambda: api.get_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=cfg.namespace, plural="agents", name=agent_name,
            ))
            spec = response.get("spec") or {}
            return str((spec.get("providerConfig") or {}).get("model") or spec.get("model") or agent_name)
        except Exception:
            return agent_name

    def failed_model_for(workload_name: str) -> str:
        return failed_model(list_workload_tasks(workload_name), model_for_agent)

    def park_infra(item: ClaimedItem, model: str, count: int) -> bool:
        if not item.issue_id:
            return False
        payload = {"issueId": item.issue_id, "repoFullName": item.repo, "number": item.issue_number}
        safe_model = re.sub(r"[^A-Za-z0-9._/-]+", "-", model).strip("-")[:50] or "unknown"
        if not dispatch.update_status(payload, "backlog", cfg.agent_name):
            return False
        return dispatch.replace_labels(
            payload,
            [NEEDS_HUMAN_LABEL, "status/blocked"],
            [
                INFRA_BLOCKED_LABEL,
                f"{INFRA_MODEL_LABEL_PREFIX}{safe_model}",
                f"{INFRA_ATTEMPT_LABEL_PREFIX}{count}",
            ],
        )

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
            if cfg.github_token:
                gh_headers["Authorization"] = f"Bearer {cfg.github_token}"
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
            return dispatch.find_issue_id(
                cfg.agent_name, cfg.lanes, item.repo, item.issue_number,
                queue_snapshot=queue_snapshot,
            )
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
            f"bridge escalation: {cfg.max_attempts} failed attempts in lane "
            f"'{item.lane or '?'}' for {item.repo}#{item.issue_number}"
        )
        return dispatch.escalate(item, cfg.escalation_lane, reason, cfg.agent_name)

    # Single per-tick snapshot of every lane queue, reused by every consumer
    # below so the bridge makes one parallel batch of GETs to
    # /api/agents/{agent}/queue instead of three (issue #256). When the snapshot
    # fetch fails we fall back to the empty map and let the consumers re-fetch
    # on demand, preserving the previous best-effort behaviour.
    try:
        queue_snapshot = dispatch.queues(cfg.agent_name, cfg.lanes)
    except Exception as e:  # best-effort; consumers fall back to per-call GETs
        queue_snapshot = {}
        logger.warning(
            "queue-snapshot-failed", extra={"error": _redact_token(repr(e))}
        )

    def queue_for(lane: str) -> list:
        return queue_snapshot.get(lane, [])

    # A Workload's lane label froze when it was created, so a retry cannot see a
    # lane that changed since. One pass over the queues gives every retry in this
    # tick dispatch's current view.
    try:
        current_lane_for = dispatch.lane_index(
            cfg.agent_name, cfg.lanes, queue_snapshot=queue_snapshot,
        )
    except Exception as e:  # best-effort; falling back to the label is the old behavior
        current_lane_for = {}
        logger.warning("lane-index-failed", extra={"error": _redact_token(repr(e))})

    # Retry failed workloads first (so a re-run this tick uses the current config),
    # then claim new work.
    for line in reconcile_failures(
        cfg.agent_name, list_failed_workloads, create_workload, delete_workload,
        cfg.namespace, cfg.gate_profiles, cfg.max_attempts,
        escalate=escalate if cfg.escalation_lane else None,
        escalation_lane=cfg.escalation_lane,
        lane_coder_agents=cfg.lane_coder_agents,
        base_coder_agents=cfg.base_coder_agents,
        repo_coder_agents=cfg.repo_coder_agents,
        lookup_issue_id=lookup_issue_id,
        current_lane_for=current_lane_for,
        feedback_for=feedback_for,
        verify_enabled=cfg.verify_enabled,
        self_go=cfg.self_go,
        branch_pushed_for=branch_pushed_for,
        issue_state_for=issue_state_for,
        declared_escalation_for=declared_escalation_for,
        park_for_human=park_for_human,
        needs_human_for=parked_for_human,
        ensure_human_label=ensure_human_label,
        tasks_for=list_workload_tasks,
        failed_model_for=failed_model_for,
        park_infra=park_infra,
    ):
        logger.info(line)

    def list_infra_parked() -> list:
        try:
            return [
                issue for issue in dispatch.list_issues()
                if INFRA_BLOCKED_LABEL in (issue.get("labels") or [])
            ]
        except Exception as e:
            logger.warning("infra-parked-list-failed", extra={"error": repr(e)})
            return []

    def clear_infra_marker(issue: dict) -> bool:
        payload = {
            "issueId": issue.get("issueId") or issue.get("id") or "",
            "repoFullName": issue.get("repoFullName"),
            "number": issue.get("number"),
        }
        return dispatch.replace_labels(payload, infra_marker_labels(issue), [])

    def record_infra_failure(issue: dict, count: int) -> bool:
        payload = {
            "issueId": issue.get("issueId") or issue.get("id") or "",
            "repoFullName": issue.get("repoFullName"),
            "number": issue.get("number"),
        }
        old = infra_marker_labels(issue)
        model = [label for label in old if label.startswith(INFRA_MODEL_LABEL_PREFIX)]
        return dispatch.replace_labels(
            payload,
            [label for label in old if label.startswith(INFRA_ATTEMPT_LABEL_PREFIX)],
            model + [f"{INFRA_ATTEMPT_LABEL_PREFIX}{count}"],
        )

    def redrive_infra(issue: dict, model: str) -> bool:
        item = claimed_item_from_issue(issue)
        if not item.lane:
            item = replace(item, lane=cfg.lanes[0] if cfg.lanes else "local")
        language = cfg.gate_profiles.get(item.repo, {}).get("language")
        branch = _branch_name(item)
        manifest = build_workload(
            item, cfg.namespace, gate_profile_for(item.repo, cfg.gate_profiles),
            cfg.agent_name, attempt=1,
            coder_agent=coder_agent_for(
                item.lane, language, cfg.lane_coder_agents, cfg.base_coder_agents,
                repo=item.repo, repo_coder_agents=cfg.repo_coder_agents,
                issue_number=item.issue_number,
            ), verify_enabled=cfg.verify_enabled, self_go=cfg.self_go,
            revise_from_branch=branch,
        )
        create_workload(manifest)
        payload = {
            "issueId": item.issue_id,
            "repoFullName": item.repo,
            "number": item.issue_number,
        }
        status_ok = dispatch.update_status(payload, "in-progress", cfg.agent_name)
        label_ok = dispatch.remove_label(payload, NEEDS_HUMAN_LABEL)
        return bool(status_ok and label_ok)

    if probe_model:
        for line in reconcile_infra_parked(
            list_infra_parked,
            probe_model,
            record_infra_failure,
            clear_infra_marker,
            redrive_infra,
            park_for_human,
            max_failures=INFRA_RECOVERY_MAX_FAILURES,
        ):
            logger.info(line)

    # Cap concurrent in-progress work so the pipeline drains a bounded set
    # instead of claiming the whole backlog at once (0 = uncapped).
    active = count_active_workloads() if cfg.max_in_progress else 0
    coder_load = load_by_coder_agent() if cfg.coder_slots else {}
    # Fix-first work-stealing (issue #134): named agents are removed from the
    # issue rotation while they still hold fix work or have a full slot, so
    # the fix lane's single slot stays uncontended. A JSON list ["coder"] or
    # a comma-separated "coder,fixer" both parse to {"coder"} (etc.).
    for line in run_once(
        cfg.lanes, cfg.agent_name, dispatch.claim_one, create_workload, cfg.namespace,
        cfg.gate_profiles, cfg.lane_coder_agents, cfg.revision_coder_agents,
        base_coder_agents=cfg.base_coder_agents,
        repo_coder_agents=cfg.repo_coder_agents,
        in_progress=active, max_in_progress=cfg.max_in_progress,
        verify_enabled=cfg.verify_enabled,
        self_go=cfg.self_go,
        agent_load=coder_load, agent_slots=cfg.coder_slots,
        fix_first_agents=cfg.fix_first_agents,
        queue_for=queue_for,
    ):
        logger.info(line)

    if cfg.pr_fix_enabled:
        def list_prfix_workloads() -> list:
            resp = _retry_k8s_request(
                lambda: api.list_namespaced_custom_object(
                    group="foreman.llmkube.dev", version="v1alpha1",
                    namespace=cfg.namespace, plural="workloads",
                    label_selector=f"created-by={PRFIX_CREATED_BY}",
                ),
                retries=2,
                base_delay=0.5,
                max_delay=16.0,
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

        def pr_is_mergeable(repo, pr) -> str:
            return check_pr_mergeable(
                repo, pr, http_get=http_get, github_token=cfg.github_token
            )

        def update_pr_branch(repo, pr) -> bool:
            from bridge.http_retry import http_put

            return update_pull_request_branch(
                repo, pr, http_put=http_put, github_token=cfg.github_token
            )

        # Build a one-per-tick {repo, pr} -> failure-signature map so the
        # reconcile path can compare the *current* failure surface against
        # the signature stored on each fix Workload's annotation (#133).
        # We snapshot the queued PR-fix items once and reuse across all
        # failed workloads in this tick; this stays stateless across ticks
        # because the signature itself is persisted on the Workload.
        pr_fix_signatures = {}
        try:
            for item in dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES)) or []:
                repo = item.get("repo")
                pr = item.get("pr")
                if repo is None or pr is None:
                    continue
                pr_fix_signatures[(repo, pr)] = failure_signature(item)
        except Exception as exc:
            logger.warning("pr-fix-signature-snapshot-failed", extra={"error": str(exc)})

        def get_pr_fix_signature(repo, pr) -> str:
            return pr_fix_signatures.get((repo, pr), "")

        for line in reconcile_pr_fixes(
            list_prfix_workloads, delete_workload, create_workload,
            mark_pr_fix,
            pr_is_mergeable=pr_is_mergeable,
            max_attempts=cfg.pr_fix_max_attempts,
            lane_agents=cfg.pr_fix_lane_agents,
            get_pr_fix_signature=get_pr_fix_signature,
            update_pr_branch=update_pr_branch,
        ):
            logger.info(line)

        existing = {
            (wl.get("metadata") or {}).get("name") for wl in list_prfix_workloads()
        }
        for line in drain_pr_fixes(
            lambda: dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES)),
            existing, create_workload,
            cfg.gate_profiles, cfg.pr_fix_lane_agents, cfg.agent_name, cfg.namespace,
            verify_enabled=cfg.verify_enabled,
            self_go=cfg.self_go,
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
        lambda item, status, agent, reason="": dispatch.update_status(
            item, status, agent, reason
        ),
        cfg.agent_name,
        dispatch=dispatch,
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
        dispatch, cfg.agent_name, bridge_wl_names,
    ):
        logger.info(line)

    # Release claims stuck at status/ready with no Workload behind them. Such an
    # issue is served at the head of the queue and refused on every claim, and
    # claim_one skips it rather than starving the lane — so it is invisible while
    # being permanently unreachable. Runs alongside the stranded reconcile and
    # before prune, so a released issue is claimable on the next tick.
    for line in release_stuck_claims(
        dispatch, cfg.agent_name, bridge_wl_names,
    ):
        logger.info(line)

    # Garbage-collect terminal Workloads last, after reconcile has already
    # retried anything retryable this tick — so a still-terminal Workload past
    # its TTL is genuinely done. Covers both issue (created-by=dispatch-bridge)
    # and pr-fix (created-by=dispatch-bridge-prfix) Workloads.
    # Delegates rather than re-listing inline: BridgeRuntime.list_terminal_candidates
    # also stamps the bridge-owned terminal-since annotation the prune TTL reads
    # (#170). An inline copy silently skipped that, leaving prune to fall back
    # to creationTimestamp.
    list_terminal_candidates = bridge.list_terminal_candidates

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
        dispatch.update_status(item, "ready", cfg.agent_name)

    def _is_parked_for_human(wl: dict) -> bool:
        """True if the Workload's issue is parked for a human (needs-human).

        Used by prune_workloads to tombstone-retire a Failed Workload whose
        issue has been deliberately parked (declared-escalation path in
        bridge/retry.py) without resetting the issue back to ready. Without
        this callback the parked-Failed oscillate-and-burn bug from issue #227
        comes back: every prune TTL flips the parked issue to ready, the coder
        re-claims it, and the coder burns attempt budgets forever (#262).

        Best-effort: any failure (transport, 404, malformed response) is
        treated as "not parked" so prune still completes the GC pass; this
        matches the treat-as-not-parked semantics in bridge/prune.py.
        """
        spec = wl.get("spec") or {}
        issues = spec.get("issues") or [0]
        repo = spec.get("repo", "")
        try:
            number = int(issues[0])
        except (TypeError, ValueError):
            return False
        try:
            return bool(dispatch.issue_is_parked(repo, number, "needs-human"))
        except Exception:  # noqa: BLE001 - best-effort, fail-open
            return False

    for line in prune_workloads(
        list_terminal_candidates, delete_workload,
        completed_ttl_seconds=cfg.prune_completed_after_h * 3600,
        failed_ttl_seconds=cfg.prune_failed_after_h * 3600,
        reset_issue=_reset_issue,
        is_parked_for_human=_is_parked_for_human,
    ):
        logger.info(line)


def _real_main() -> None:  # pragma: no cover - thin wiring, exercised in the cluster
    from bridge.claim import DispatchClient, warn_if_lane_cap_exceeded

    validate_env()
    configure_logging()

    base_url = os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000")
    _check_dispatch_url(base_url)
    token = os.environ["DISPATCH_AGENT_TOKEN"]
    agent_name = os.environ.get("DISPATCH_AGENT_NAME", "foreman-coder")
    lanes = [part.strip() for part in os.environ.get("DISPATCH_LANES", "local,cloud,frontier").split(",") if part.strip()]
    warn_if_lane_cap_exceeded(lanes)
    namespace = os.environ.get("FOREMAN_NAMESPACE", "llm")
    gate_profiles = parse_gate_profiles(os.environ.get("GATEPROFILE_MAP"))
    max_attempts = int(os.environ.get("RETRY_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS)))
    delete_workload_timeout_s = int(os.environ.get("DELETE_WORKLOAD_TIMEOUT_S", "60"))
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
    max_in_progress = int(os.environ.get("MAX_IN_PROGRESS", "0"))
    coder_slots = parse_coder_agent_slots(os.environ.get("CODER_AGENT_SLOTS"))
    fix_first_agents = _parse_fix_first_agents(os.environ.get("FIX_FIRST_AGENTS"))

    cfg = TickConfig(
        agent_name=agent_name,
        lanes=lanes,
        namespace=namespace,
        gate_profiles=gate_profiles,
        max_attempts=max_attempts,
        lane_coder_agents=lane_coder_agents,
        revision_coder_agents=revision_coder_agents,
        base_coder_agents=base_coder_agents,
        repo_coder_agents=repo_coder_agents,
        escalation_lane=escalation_lane,
        verify_enabled=verify_enabled,
        self_go=self_go,
        pr_fix_enabled=pr_fix_enabled,
        pr_fix_max_attempts=pr_fix_max_attempts,
        github_token=github_token,
        pr_fix_lane_agents=pr_fix_lane_agents,
        prune_completed_after_h=prune_completed_after_h,
        prune_failed_after_h=prune_failed_after_h,
        max_in_progress=max_in_progress,
        coder_slots=coder_slots,
        fix_first_agents=fix_first_agents,
        delete_workload_timeout_s=delete_workload_timeout_s,
    )
    probe_model = None
    probe_enabled = os.environ.get("INFRA_PROBE_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    if probe_enabled:
        probe_url = os.environ.get("INFRA_PROBE_URL", "http://litellm.llm:4000/v1").rstrip("/")
        probe_key = os.environ.get("INFRA_PROBE_API_KEY", "")

        def probe_model(model: str) -> bool:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply OK."}],
                "max_tokens": 1,
            }
            headers = {"Authorization": f"Bearer {probe_key}"} if probe_key else {}
            try:
                from bridge.http_retry import http_post as _http_post
                response = _http_post(
                    f"{probe_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
                return isinstance(data, dict) and bool(data.get("choices"))
            except Exception as e:
                logger.info(
                    "infra-model-unhealthy",
                    extra={"model": model, "error": _redact_token(repr(e))},
                )
                return False

    run_tick(api, dispatch, cfg, http_get, probe_model=probe_model)


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
def _list_failed_workloads(
    api: client.CustomObjectsApi, namespace: str
) -> List[Dict[str, Any]]:
    """List Workloads that finished in a non-success terminal phase."""
    failed: List[Dict[str, Any]] = []
    for workload in _list_bridge_workloads(api, namespace):
        phase = workload.get("status", {}).get("phase")
        if phase in {"Failed"}:
            failed.append(workload)
    return failed


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


def _list_terminal_candidates(
    api: client.CustomObjectsApi,
    namespace: str,
    prfix_created_by: str,
) -> List[Dict[str, Any]]:
    """Concatenate bridge + pr-fix Workloads for pruning.

    The caller filters by phase before deleting. The function does not
    deduplicate because Kubernetes CR names are globally unique.

    For every terminal Workload the bridge first sees (issue #170), we
    stamp a bridge-owned annotation recording the observation moment, so
    prune TTL is measured against a timestamp the Foreman controller
    cannot rewrite. The stamp is persisted back to the cluster so it
    survives a restart.
    """
    bridge = _list_bridge_workloads(api, namespace)
    prfix = _list_workloads_by_label(
        api, namespace, f"created-by={prfix_created_by}"
    )
    items: List[Dict[str, Any]] = list(bridge) + list(prfix)
    for wl in items:
        phase = (wl.get("status") or {}).get("phase") or ""
        if phase not in ("Completed", "Failed"):
            continue
        meta = wl.get("metadata") or {}
        annotations = meta.get("annotations") or {}
        annotation_key = terminal_since_key(phase)
        if annotations.get(annotation_key):
            # Already stamped on a prior tick; idempotent no-op so the TTL
            # timestamp does not advance on subsequent reconciles.
            continue
        stamp = stamp_terminal_since(wl)
        if stamp is None:
            continue
        name = meta.get("name")
        if not name:
            continue
        try:
            _retry_k8s_request(lambda: api.patch_namespaced_custom_object(
                group="foreman.llmkube.dev",
                version="v1alpha1",
                namespace=namespace,
                plural="workloads",
                name=name,
                body={
                    "metadata": {
                        "annotations": {annotation_key: stamp},
                    },
                },
            ))
        except client.ApiException as exc:
            # stamp_terminal_since mutated this manifest in place. The PATCH did
            # not land, so drop the annotation again: leaving it makes
            # terminal_since read "now" for every terminal Workload on every
            # tick, and prune can then never reach a TTL.
            # read the live dict: stamp_terminal_since may have created it
            ((wl.get("metadata") or {}).get("annotations") or {}).pop(annotation_key, None)
            # "name" is a reserved LogRecord attribute; passing it in extra makes
            # logging raise KeyError. repr(ApiException) is empty -- the client
            # passes http_resp as a keyword, so args is () -- and status/reason/
            # body carry the cause.
            logger.warning(
                "stamp-terminal-since-failed",
                extra={
                    "status": getattr(exc, "status", None),
                    "reason": getattr(exc, "reason", None),
                    "body": (getattr(exc, "body", None) or "")[:400],
                    "workload": name,
                },
            )
    return items


def _list_workload_tasks(
    api: client.CustomObjectsApi,
    namespace: str,
    workload_name: str,
) -> List[Dict[str, Any]]:
    """Return AgenticTask items belonging to *workload_name*."""
    response = _retry_k8s_request(
        lambda: api.list_namespaced_custom_object(
            group="foreman.llmkube.dev",
            version="v1alpha1",
            namespace=namespace,
            plural="agentictasks",
            label_selector=f"foreman.llmkube.dev/workload={workload_name}",
        ),
        retries=2,
        base_delay=0.5,
        max_delay=16.0,
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
        delete_workload_timeout_s: int = 60,
    ) -> None:
        self.api = api
        self.namespace = namespace
        self.prfix_created_by = prfix_created_by
        self.delete_workload_timeout_s = delete_workload_timeout_s

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
            self.api, self.namespace, name, timeout=self.delete_workload_timeout_s
        )


if __name__ == "__main__":
    _real_main()
