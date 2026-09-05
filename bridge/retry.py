import logging
import re
from dataclasses import replace
from typing import Callable, Optional

from bridge.models import ClaimedItem
from bridge.workload import (
    build_workload,
    coder_agent_for,
    gate_profile_for,
    _branch_name,
    ATTEMPT_ANNOTATION,
    INFRA_ATTEMPT_ANNOTATION,
    ISSUE_ID_ANNOTATION,
)

logger = logging.getLogger("bridge.retry")

# How many total coder attempts before the bridge stops retrying a Workload and
# leaves it as a Failed tombstone for human triage. Override via env.
DEFAULT_MAX_ATTEMPTS = 3
# Executor errors (model 403, network drop) are infrastructure failures,
# not rejections by the model. Retrying them must not consume the verdict
# budget reserved for genuine NO-GO/INCOMPLETE responses, but it must
# still be bounded so a permanently broken backend cannot loop forever.
INFRA_MAX_ATTEMPTS = 3
# At the 15-minute CronJob cadence, keep a failed dependency recoverable for 24h.
INFRA_RECOVERY_MAX_FAILURES = 96
INFRA_BLOCKED_LABEL = "blocked/infra"
INFRA_MODEL_LABEL_PREFIX = "blocked/infra-model/"
INFRA_ATTEMPT_LABEL_PREFIX = "blocked/infra-attempt/"
NEEDS_HUMAN_LABEL = "needs-human"

ListFailed = Callable[[], list]         # () -> list of Failed Workload manifests (dicts)
DeleteWorkload = Callable[[str], None]  # (name) -> None; blocks until the object is gone
Escalate = Callable[[ClaimedItem], bool]  # (item) -> True when re-laned + unclaimed
LookupIssueId = Callable[[ClaimedItem], str]   # (item) -> dispatch issue id, "" if not found
FeedbackFor = Callable[[str], str]             # (workload name) -> retry feedback text, "" if none
BranchPushedFor = Callable[[str, str, str], bool]  # (workload name, branch, repo) -> did its task branch reach the remote
IssueStateFor = Callable[[ClaimedItem], Optional[str]]  # (item) -> "open"/"closed", None if unknown
DeclaredEscalationFor = Callable[[str], Optional[str]]  # (workload name) -> declared reason or None
NeedsHumanFor = Callable[[ClaimedItem], Optional[bool]]  # (item) -> parked state, None if unknown
EnsureHumanLabel = Callable[[ClaimedItem], bool]  # (item) -> label is present after the call
TasksFor = Callable[[str], list]
FailedModelFor = Callable[[str], str]


# Bounds the feedback block injected into a retry's coder prompt.
FEEDBACK_MAX_CHARS = 2000


def feedback_from_tasks(tasks: list) -> str:
    """Distill a failed Workload's task results into a retry prompt block.

    Sources, in order of usefulness: a reviewer NO-GO's structured findings
    (missing_tests / scope_creep / *_details), then reviewer summaries, then
    coder failure errors. Returns "" when there is nothing actionable, so the
    caller falls back to a plain (issues-path) retry.
    """
    notes = []
    for t in tasks or []:
        spec = t.get("spec") or {}
        st = t.get("status") or {}
        ex = (st.get("result") or {}).get("extra") or {}
        kind = spec.get("kind")
        if kind == "review" and st.get("verdict") == "NO-GO":
            me = ex.get("modelExtra") or {}
            findings = me.get("findings") or {}
            flags = sorted(k for k, v in findings.items() if v is True and not k.endswith("_details"))
            details = [f"{k}: {v}" for k, v in sorted(findings.items()) if isinstance(v, str) and v]
            summary = ex.get("modelSummary") or ""
            parts = []
            if flags:
                parts.append("findings: " + ", ".join(flags))
            parts.extend(details)
            if summary:
                parts.append(summary)
            if parts:
                notes.append("Reviewer rejected the previous attempt (NO-GO). " + "; ".join(parts))
        elif kind == "issue-fix" and st.get("verdict") in ("NO-GO", "INCOMPLETE"):
            err = ex.get("error") or ""
            if err:
                notes.append(f"Previous coder attempt failed: {err}")
            # A failed attempt may still have pushed a commit (e.g. the push
            # was rejected after the work was done, or the run ran out of
            # budget mid-edit). The retry starts on that branch, so the commit
            # is already in its workspace — name it so the coder builds on it
            # instead of spending its edit budget rediscovering its own work.
            commit = ex.get("commitSHA") or ""
            branch = ex.get("branch") or ""
            summary = ex.get("modelSummary") or ""
            if commit:
                where = f" on {branch}" if branch else ""
                what = f": {summary}" if summary else ""
                notes.append(
                    f"Previous coder attempt pushed {commit}{where}{what}. "
                    "That work is in your workspace and has NOT shipped — the "
                    "issue is still open. Build on it; do not redo it."
                )
    if not notes:
        return ""
    text = (
        "A previous automated attempt at this issue was rejected. "
        "Address this feedback in your fix:\n- " + "\n- ".join(notes)
    )
    return text[:FEEDBACK_MAX_CHARS]


# The escalation reasons a coder may declare. Anything else is ignored (treated as
# no declaration), so a typo or a future value cannot silently divert work out of
# the loop — the same reasoning as ISSUE_STATES in claim.py.
DECLARED_ESCALATIONS = frozenset(
    {"DESIGN-DECISION", "NO-TECHNICAL-FIX", "BUDGET-EXHAUSTED"}
)

# Of those, the ones that mean a person is needed. They are *determinations*:
# the model looked at the work and concluded code cannot settle it, so re-running
# it would spend an attempt on a decision already made.
#
# BUDGET-EXHAUSTED is deliberately not among them. Running out of turns is a
# resource limit, not a judgement about the work, and the right answer is another
# attempt — under the ordinary cap, and consuming one, so a coder cannot buy
# unlimited turns by declaring it repeatedly (#274).
PARKING_ESCALATIONS = frozenset({"DESIGN-DECISION", "NO-TECHNICAL-FIX"})


def declared_escalation(tasks: list) -> Optional[str]:
    """Return the escalation reason a coder declared on this Workload, if any.

    A coder that can see the work is not solvable by code — it needs a product or
    design decision, or there is no technical fix — says so through submit_result's
    free-form ``extra``, which foreman surfaces at
    ``status.result.extra.modelExtra``. That is a first-hand judgement from the
    model that read the issue and the code.

    Without this the only route to a human is attempt-exhaustion, which is a lossy
    proxy: it spends every attempt to reach a conclusion the coder already had on
    the first read, and it files "CI was flaky" in the same bucket as "this needs
    Jory's judgement" with no way to tell them apart afterwards.

    Only values in DECLARED_ESCALATIONS count. An unrecognised string returns None
    and the workload retries as before: a model inventing a reason must not be able
    to route work out of the loop.
    """
    for t in tasks or []:
        spec = t.get("spec") or {}
        if spec.get("kind") not in ("issue-fix", "code"):
            continue
        extra = ((t.get("status") or {}).get("result") or {}).get("extra") or {}
        model_extra = extra.get("modelExtra") or {}
        if not isinstance(model_extra, dict):
            continue
        reason = model_extra.get("escalation")
        if isinstance(reason, str) and reason.strip().upper() in DECLARED_ESCALATIONS:
            return reason.strip().upper()
    return None


def branch_pushed(tasks: list, remote_branch_exists: bool = False) -> bool:
    """True when a failed Workload's task branch is known to have reached the remote.

    Evidence, any one of which is sufficient:
      - ``remote_branch_exists`` (preferred when available): the deterministic
        task branch ``foreman/wl-<workload>/issue-<n>`` is present on the
        remote. A prior attempt of this same Workload must have pushed it.
        This is what recovers the wasted-cycle class where a Workload instance
        died without recording any verdict (coder Jobs killed at the deadline,
        agent restarts) but had already pushed (#132). On API failure the
        caller passes False and the task-CR scan below is the fallback.
      - ``pullRequestURL`` on any task: a PR exists, so the branch was pushed.
      - a review task ran: the reviewer fetches and checks out the branch, so it
        could not have produced a verdict without one.
      - a coder task returned GO: the coder pushes before reporting GO.
      - a PUSH-FAILED outcome: the push was rejected non-fast-forward, which
        means a branch is already there. This is what makes a wedge self-heal —
        the failure itself becomes the evidence the next retry needs.

    Absent evidence, return False: the caller then sets neither reviseFromBranch
    (which would hard-fail on a branch that was never pushed, LLMKube#1365) nor
    allowOverwrite (which would force-push base over real work, #101).
    """
    if remote_branch_exists:
        return True
    for t in tasks or []:
        spec = t.get("spec") or {}
        st = t.get("status") or {}
        ex = (st.get("result") or {}).get("extra") or {}
        if ex.get("pullRequestURL"):
            return True
        if spec.get("kind") == "review" and st.get("verdict"):
            return True
        if spec.get("kind") in ("issue-fix", "code") and st.get("verdict") == "GO":
            return True
        outcome = str(ex.get("outcome") or "")
        error = str(ex.get("error") or "")
        if "PUSH-FAILED" in outcome or "non-fast-forward" in error:
            return True
    return False


def _park_exhausted_factory(
    park_for_human: Optional[Callable[..., bool]],
    current_lane_for: Optional[dict],
    lookup_issue_id: Optional[LookupIssueId],
    needs_human_for: Optional[NeedsHumanFor] = None,
) -> Callable[..., bool]:
    """Build the _park_exhausted closure used by the giveup branches.

    Parking is best-effort: a failure here must not abort the reconcile pass,
    because the remaining Workloads and the downstream claim/pr-fix passes
    still need to run. The tombstone is left in place either way, so a failed
    park degrades to today's behaviour rather than losing the work.

    If `needs_human_for` is supplied, an issue that is already parked is
    treated as already-handled and park_for_human is not called again, so a
    wedged delete_workload (which leaves the tombstone alive and triggers
    list_failed() to return the same workload on every tick) does not repost
    the same needs-human escalation comment each tick. This mirrors the
    dedupe the declared-escalation path already uses.
    """

    def _park_exhausted(wl: dict, reason: str, path: str = "exhausted-attempts") -> bool:
        if park_for_human is None:
            return False
        try:
            item = refresh_lane(item_from_workload(wl), current_lane_for)
            if not item.issue_id and lookup_issue_id:
                item = replace(item, issue_id=lookup_issue_id(item) or "")
            if needs_human_for is not None and needs_human_for(item) is True:
                # Already parked by an earlier tick — do not double-comment.
                return True
            return bool(park_for_human(item, reason, path=path))
        except Exception:
            logger.exception(
                "park-exhausted-failed",
                extra={"workload": (wl.get("metadata") or {}).get("name")},
            )
            return False

    return _park_exhausted


def attempt_of(wl: dict) -> int:
    """Read the attempt counter off a Workload; absent/garbage -> 1."""
    ann = (wl.get("metadata") or {}).get("annotations") or {}
    try:
        return max(1, int(ann.get(ATTEMPT_ANNOTATION, "1")))
    except (TypeError, ValueError):
        return 1


def infra_attempt_of(wl: dict) -> int:
    """Read the infra-attempt counter off a Workload; absent/garbage -> 1.

    Mirrors ``attempt_of``'s default of 1 so the first infra failure retries
    as ``retry-infra:1/M`` and the gate fires on the N-th retry rather than
    the (N+1)-th.
    """
    ann = (wl.get("metadata") or {}).get("annotations") or {}
    try:
        return max(1, int(ann.get(INFRA_ATTEMPT_ANNOTATION, "1")))
    except (TypeError, ValueError):
        return 1


def task_failed_with_executor_error(wl: dict) -> bool:
    """Return True if any task in the Workload has a Completed condition with reason=ExecutorError.

    An ExecutorError on a Completed condition means the request never
    reached the agent (model 403, network drop, etc.), so the failure
    is an infrastructure problem — not a real rejection from the model.
    Such a failure must not consume the verdict retry budget, because
    the agent has not actually passed judgment on the work yet.
    """
    status = wl.get("status") or {}
    task_statuses = status.get("taskStatuses") or []
    for ts in task_statuses:
        for cond in (ts.get("conditions") or []):
            if cond.get("type") == "Completed" and cond.get("reason") == "ExecutorError":
                return True
    return False


def tasks_failed_with_executor_error(tasks: list) -> bool:
    """Return True when a child AgenticTask records ExecutorError."""
    for task in tasks or []:
        for condition in ((task.get("status") or {}).get("conditions") or []):
            if condition.get("type") == "Completed" and condition.get("reason") == "ExecutorError":
                return True
    return False


# Words that appear where a model name would sit in an error message but are
# never a model. A capture matching one of these means the scrape misfired.
_NON_MODEL_TOKENS = frozenset(
    {"name", "ref", "id", "type", "spec", "model", "deployment", "none", "null", "nil"}
)


def failed_model(tasks: list, model_for_agent: Optional[Callable[[str], str]] = None) -> str:
    """Resolve the model behind the child task that failed before the loop.

    Foreman versions that do not stamp ``spec.modelRef`` may still include the
    model in the executor condition message; retain that evidence before falling
    back to the Agent name.
    """
    for task in tasks or []:
        if not tasks_failed_with_executor_error([task]):
            continue
        spec = task.get("spec") or {}
        model = spec.get("modelRef")
        if isinstance(model, str) and model.strip():
            return model.strip()
        extra = ((task.get("status") or {}).get("result") or {}).get("extra") or {}
        for key in ("model", "modelName"):
            model = extra.get(key)
            if isinstance(model, str) and model.strip():
                return model.strip()
        condition_text = " ".join(
            str(condition.get("message") or "")
            for condition in ((task.get("status") or {}).get("conditions") or [])
        )
        # Scraping a model out of free-form error text is guesswork, so it must
        # fail closed. The original pattern took the token straight after
        # "model", and LLMKube writes "model name=llama-nvidia", so it captured
        # the literal word "name". That was then written into
        # blocked/infra-model/name and probed forever against a model that
        # cannot exist: misospace/dispatch#899 reached
        # blocked/infra-attempt/92 that way.
        #
        # Allow a structural word between the keyword and the value, and reject
        # a capture that is itself structural, so an unrecognised shape falls
        # through to the agentRef lookup below — which resolves a real model
        # from the Agent CR instead of guessing.
        match = re.search(
            r"(?:model|deployment)(?:[ _-]?(?:name|ref|id))?[ =:'\"]+([A-Za-z0-9._/-]+)",
            condition_text,
            re.IGNORECASE,
        )
        if match and match.group(1).lower() not in _NON_MODEL_TOKENS:
            return match.group(1)
        agent_ref = spec.get("agentRef") or {}
        agent = agent_ref.get("name") if isinstance(agent_ref, dict) else ""
        if isinstance(agent, str) and agent.strip():
            return (model_for_agent(agent) if model_for_agent else agent) or agent
    return "unknown"


def _issue_labels(record: dict) -> list[str]:
    labels = record.get("labels") or []
    return [
        name for label in labels
        for name in [label.get("name") if isinstance(label, dict) else label]
        if isinstance(name, str)
    ]


def infra_model_from_labels(record: dict) -> str:
    for label in _issue_labels(record):
        if label.startswith(INFRA_MODEL_LABEL_PREFIX):
            return label[len(INFRA_MODEL_LABEL_PREFIX):] or "unknown"
    return "unknown"


def infra_failure_count(record: dict) -> int:
    counts = []
    for label in _issue_labels(record):
        if label.startswith(INFRA_ATTEMPT_LABEL_PREFIX):
            try:
                counts.append(int(label[len(INFRA_ATTEMPT_LABEL_PREFIX):]))
            except ValueError:
                pass
    return max(counts, default=0)


def infra_marker_labels(record: dict) -> list[str]:
    return [
        label for label in _issue_labels(record)
        if label == INFRA_BLOCKED_LABEL
        or label.startswith(INFRA_MODEL_LABEL_PREFIX)
        or label.startswith(INFRA_ATTEMPT_LABEL_PREFIX)
    ]


def claimed_item_from_issue(record: dict) -> ClaimedItem:
    repository = record.get("repository") or {}
    return ClaimedItem(
        repo=str(record.get("repoFullName") or (repository.get("fullName") if isinstance(repository, dict) else "") or ""),
        issue_number=int(record.get("number") or record.get("issueNumber") or 0),
        intent=str(record.get("title") or ""),
        lane=str(record.get("currentLane") or ""),
        issue_id=str(record.get("issueId") or record.get("id") or ""),
    )


def reconcile_infra_parked(
    list_parked: Callable[[], list],
    probe_model: Callable[[str], bool],
    record_failure: Callable[[dict, int], bool],
    clear_marker: Callable[[dict], bool],
    redrive: Callable[[dict, str], bool],
    park_for_human: Callable[..., bool],
    max_failures: int = INFRA_RECOVERY_MAX_FAILURES,
) -> list[str]:
    """Recover issue markers left after an infrastructure Workload was deleted."""
    records = list_parked() or []
    health = {}
    for model in sorted({infra_model_from_labels(r) for r in records}):
        # Never probe a model we could not identify. A parked record whose
        # model resolved to "unknown" (or to a structural word the scrape
        # rejected) has no backend to ask about: probing it fails every time,
        # so the record can never be redriven and never reaches the failure
        # cap that would park it for a human. It just spins.
        #
        # Treat it as unhealthy WITHOUT calling out, so the failure counter
        # below advances and the record reaches a human on the normal path.
        if model in ("", "unknown"):
            health[model] = False
            logger.info("infra-model-unidentified", extra={"model": model})
            continue
        try:
            health[model] = bool(probe_model(model))
        except Exception:
            health[model] = False
            logger.exception("infra-model-probe-failed", extra={"model": model})

    results = []
    for record in records:
        model = infra_model_from_labels(record)
        number = record.get("number") or "?"
        if health.get(model, False):
            try:
                recovered = bool(redrive(record, model))
                if recovered:
                    recovered = bool(clear_marker(record))
            except Exception:
                recovered = False
                logger.exception("infra-redrive-failed", extra={"issue": number, "model": model})
            results.append(f"{number}:infra-{'redriven' if recovered else 'redrive-error'}:{model}")
            continue

        count = infra_failure_count(record)
        if count + 1 >= max_failures:
            try:
                parked = bool(
                    park_for_human(
                        claimed_item_from_issue(record),
                        f"infrastructure dependency unavailable: {model}",
                        path="exhausted-infra",
                    )
                )
            except Exception:
                parked = False
                logger.exception("infra-human-park-failed", extra={"issue": number, "model": model})
            if parked:
                clear_marker(record)
            results.append(f"{number}:infra-human:{model}")
        else:
            try:
                record_failure(record, count + 1)
            except Exception:
                logger.exception("infra-failure-record-failed", extra={"issue": number, "model": model})
            results.append(f"{number}:infra-unhealthy:{model}:{count + 1}")
    return results


def _issue_from_name(name: str) -> int:
    """Recover the issue number from a Workload name (`wl-<owner>-<repo>-<n>`).

    The spec used to lose `issues` on a retry, and the caller then substituted 0,
    which silently became a real-looking issue number: the rebuilt Workload took
    the name wl-<repo>-0 and branch issue-0, both fixed per repo, so every third
    attempt in that repo collided on one branch. The name still carries the right
    number at that point, so recovering it here keeps a dropped field from
    escalating into overwritten work. Returns 0 when the suffix is not a number,
    which callers treat as unusable rather than as issue 0.
    """
    tail = name.rsplit("-", 1)[-1]
    # isascii() before isdigit(): isdigit() is True for '²' and '①', which int()
    # rejects. RFC 1123 keeps those out of real object names, but this function is
    # called outside the per-Workload try in reconcile_failures, so a raise here
    # would abort the whole sweep rather than one retry.
    return int(tail) if tail.isascii() and tail.isdigit() else 0


def item_from_workload(wl: dict) -> ClaimedItem:
    """Reconstruct the ClaimedItem from a Workload so build_workload can re-render
    it (with the CURRENT gateProfile/config) on retry."""
    meta = wl.get("metadata") or {}
    spec = wl.get("spec") or {}
    labels = meta.get("labels") or {}
    ann = meta.get("annotations") or {}
    issues = spec.get("issues") or []
    number = int(issues[0]) if issues else _issue_from_name(meta.get("name") or "")
    if not number:
        logger.error(
            "workload-missing-issue-number",
            extra={"workload": meta.get("name"), "repo": spec.get("repo")},
        )
    return ClaimedItem(
        repo=str(spec.get("repo") or ""),
        issue_number=number,
        intent=str(spec.get("intent") or ""),
        lane=str(labels.get("lane") or ""),
        issue_id=str(ann.get(ISSUE_ID_ANNOTATION) or ""),
    )


def refresh_lane(item: ClaimedItem, current_lane_for: Optional[dict]) -> ClaimedItem:
    """Re-read the item's lane from dispatch's current view.

    item_from_workload takes the lane off the Workload's label, which was
    stamped when the Workload was created and never updated. A lane that
    changed since — a manual de-escalation, or a groomer reclassification —
    was therefore invisible to every retry, which kept rebuilding the Workload
    with the coder of the lane it no longer belonged to.

    No-ops when the lookup is absent or has nothing for this issue, so a
    dispatch hiccup leaves the Workload's own lane in place rather than
    blanking it.
    """
    if not current_lane_for:
        return item
    lane = current_lane_for.get((item.repo, item.issue_number))
    if not lane or lane == item.lane:
        return item
    return replace(item, lane=lane)


def reconcile_failures(
    agent_name: str,
    list_failed: ListFailed,
    create_workload: Callable[[dict], None],
    delete_workload: DeleteWorkload,
    namespace: str,
    gate_profiles: dict,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    escalate: Optional[Escalate] = None,
    escalation_lane: str = "",
    lane_coder_agents: Optional[dict] = None,
    base_coder_agents: Optional[dict] = None,
    repo_coder_agents: Optional[dict] = None,
    lookup_issue_id: Optional[LookupIssueId] = None,
    current_lane_for: Optional[dict] = None,
    feedback_for: Optional[FeedbackFor] = None,
    verify_enabled: bool = True,
    self_go: list[str] | None = None,
    branch_pushed_for: Optional[BranchPushedFor] = None,
    issue_state_for: Optional[IssueStateFor] = None,
    declared_escalation_for: Optional[DeclaredEscalationFor] = None,
    park_for_human: Optional[Callable[..., bool]] = None,
    infra_max_attempts: int = INFRA_MAX_ATTEMPTS,
    tasks_for: Optional[TasksFor] = None,
    failed_model_for: Optional[FailedModelFor] = None,
    park_infra: Optional[Callable[[ClaimedItem, str, int], bool]] = None,
    needs_human_for: Optional[NeedsHumanFor] = None,
    ensure_human_label: Optional[EnsureHumanLabel] = None,
) -> list:
    """Retry Failed bridge Workloads, bounded by max_attempts.

    For each Failed Workload:
      - attempt < max_attempts: delete it and recreate a fresh one at attempt+1,
        so it re-runs with the current config (gateProfile, agent refs). The name
        is deterministic, so delete-then-recreate reuses the same name/branch;
        delete_workload must block until the old object is gone.
      - attempt >= max_attempts, not yet in the escalation lane, and an escalate
        hook is wired: move the issue to the escalation lane + release the claim,
        then delete the Workload. The next tick claims it from the escalation
        lane and builds a fresh Workload with that lane's coder Agent. If the
        escalate call fails, keep the tombstone so the next tick retries it.
      - attempt >= max_attempts otherwise (already escalated, or no hook): leave
        it as a Failed tombstone AND park the issue (status/backlog +
        needs-human) so a human can actually find it. The tombstone alone is
        not a worklist: a claimed issue with an exhausted Workload is invisible
        on the board and holds a slot against MAX_IN_PROGRESS until the prune.

    A Workload whose task failed with `reason: ExecutorError` on its Completed
    condition is treated as an infrastructure failure (the request never
    reached the agent). Such Workloads retry against a separate budget
    (`infra_max_attempts`) without incrementing the verdict counter, so a
    transient 403 or network drop does not consume the budget reserved for
    genuine rejections. At the cap, `park_infra` records the failed model on
    the issue before the tombstone is retired.

    A declared human escalation normally parks the issue through
    `park_for_human`. When `needs_human_for` confirms that the issue already has
    the durable parked marker, the bridge uses `ensure_human_label` instead and
    skips the comment. This leaves the Failed Workload available for triage
    without posting the same announcement on every tick. An unknown lookup fails
    open to the full park path; a failed label-only repair keeps the tombstone and
    retries the label repair on the next tick.

    Returns per-Workload outcome strings.
    """
    lane_coder_agents = lane_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    repo_coder_agents = repo_coder_agents or {}
    results = []
    _park_exhausted = _park_exhausted_factory(
        park_for_human, current_lane_for, lookup_issue_id, needs_human_for
    )
    for wl in list_failed():
        name = (wl.get("metadata") or {}).get("name") or "?"
        attempt = attempt_of(wl)
        infra_attempt = infra_attempt_of(wl)
        # An ExecutorError on a task's Completed condition means the request
        # never reached the agent (model 403, network drop, etc.) — so the
        # failure is an infrastructure problem and must not consume the
        # verdict retry budget. Such Workloads get their own budget below.
        tasks = []
        if tasks_for:
            try:
                tasks = tasks_for(name) or []
            except Exception as e:
                logger.warning(
                    "infra-task-lookup-failed",
                    extra={"workload": name, "error": repr(e)},
                )
        is_infra = task_failed_with_executor_error(wl) or tasks_failed_with_executor_error(tasks)

        # A closed issue cannot be advanced by anything we do here, so neither a
        # retry nor an escalation is worth an attempt. Checked before the
        # max_attempts branch because escalating a closed issue to the frontier
        # lane wastes a strictly more expensive coder.
        #
        # Observed: wl-misospace-llmkube-images-38 sat Failed at attempt 1 for an
        # issue closed as already-resolved (a sibling's fix had deleted the file it
        # named). Each further attempt could only clone the repo, find nothing to
        # do, and return NO-CHANGES.
        #
        # Fails OPEN on anything ambiguous: only an explicit "closed" skips. None
        # means the lookup 404'd or errored, and treating that as closed would
        # cancel real retries whenever dispatch was briefly unreachable.
        if issue_state_for is not None:
            item_for_state = item_from_workload(wl)
            try:
                state = issue_state_for(item_for_state)
            except Exception as e:
                state = None
                logger.warning(
                    "issue-state-check-failed",
                    extra={"workload": name, "error": repr(e)},
                )
            if state == "closed":
                msg = f"{name}:skip-retry:issue-closed"
                logger.info(msg)
                results.append(msg)
                continue

        # A coder that declared the work needs a human is taken at its word: no
        # retry, no escalation to a stronger coder, and no attempt consumed. The
        # model read the issue and the code; re-running the same prompt two more
        # times cannot turn a design decision into a patch.
        #
        # The issue moves to status/backlog, which is triage-only — the agent queue
        # filters it out (claimableOnly excludes backlog), so the loop stops serving
        # it while it stays visible to a human instead of sitting claimed and
        # invisible. The Failed workload is left as the tombstone to triage from,
        # matching what max_attempts giveup already does.
        #
        # Once the issue has durable parked state, do not post the same escalation
        # comment again. Still run the label-only callback so a race or partial
        # label failure can repair the marker without announcing twice.
        #
        # Fails open: if the park callback is missing or fails, fall through to the
        # normal retry path rather than dropping the work on the floor.
        if declared_escalation_for is not None:
            try:
                reason = declared_escalation_for(name)
            except Exception as e:
                reason = None
                logger.warning(
                    "declared-escalation-lookup-failed",
                    extra={"workload": name, "error": repr(e)},
                )
            if reason and reason not in PARKING_ESCALATIONS:
                # A non-parking declaration (BUDGET-EXHAUSTED) is a request for
                # another go, not for a person. Fall through to the ordinary
                # retry path below, which charges an attempt and escalates at
                # the cap like any other failure.
                logger.info(
                    "declared-escalation-retrying",
                    extra={"workload": name, "reason": reason},
                )
                reason = None
            if reason:
                item_h = refresh_lane(item_from_workload(wl), current_lane_for)
                if not item_h.issue_id and lookup_issue_id:
                    item_h = replace(item_h, issue_id=lookup_issue_id(item_h) or "")

                already_parked = False
                if needs_human_for is not None:
                    try:
                        already_parked = needs_human_for(item_h) is True
                    except Exception as e:
                        logger.warning(
                            "needs-human-lookup-failed",
                            extra={"workload": name, "error": repr(e)},
                        )

                if already_parked:
                    # The label/backlog state is the durable idempotency marker.
                    # A separate callback keeps this path label-only, so
                    # park_for_human cannot accidentally post another comment.
                    if ensure_human_label is not None:
                        try:
                            if not ensure_human_label(item_h):
                                logger.warning(
                                    "ensure-needs-human-label-failed",
                                    extra={"workload": name, "reason": reason},
                                )
                        except Exception as e:
                            logger.warning(
                                "ensure-needs-human-label-failed",
                                extra={
                                    "workload": name,
                                    "reason": reason,
                                    "error": repr(e),
                                },
                            )
                    # Parking already succeeded on an earlier tick. Keep the
                    # tombstone for triage and retry only the label repair above;
                    # never rerun the coder because that would spend an attempt on
                    # a decision already made by the model.
                    msg = f"{name}:human-escalation:{reason}"
                    logger.info(msg)
                    results.append(msg)
                    continue
                else:
                    parked = False
                    if park_for_human is not None:
                        try:
                            parked = bool(park_for_human(item_h, reason, path="declared-human"))
                        except Exception as e:
                            logger.warning(
                                "park-for-human-failed",
                                extra={"workload": name, "reason": reason, "error": repr(e)},
                            )
                    if parked:
                        msg = f"{name}:human-escalation:{reason}"
                        logger.info(msg)
                        results.append(msg)
                        continue

                # Could not park it — do not silently swallow the declaration, but
                # do not strand the work either: fall through and retry as normal.
                logger.warning(
                    "human-escalation-not-parked",
                    extra={"workload": name, "reason": reason},
                )
        if is_infra and infra_attempt >= infra_max_attempts:
            # Infra-error retries have their own budget so a permanently
            # broken backend cannot loop forever. Leave the Failed tombstone
            # in place — escalating to the frontier lane would only pay for
            # a stronger model to hit the same 403. A human triages from the
            # lingering Workload.
            #
            # Park the issue as well, or "a human triages" never happens: the
            # issue stays claimed at status/in-progress with no Workload that
            # will ever run again, invisible on the board and holding a slot
            # against MAX_IN_PROGRESS until the 48h prune. Parking moves it to
            # backlog with needs-human so it appears on the operator worklist.
            item = refresh_lane(item_from_workload(wl), current_lane_for)
            model = "unknown"
            if failed_model_for:
                try:
                    model = failed_model_for(name) or "unknown"
                except Exception as e:
                    logger.warning(
                        "infra-model-lookup-failed",
                        extra={"workload": name, "error": repr(e)},
                    )
            if park_infra is not None:
                try:
                    parked = bool(park_infra(item, model, attempt))
                except Exception:
                    logger.exception("park-infra-failed", extra={"workload": name, "model": model})
                    parked = False
            else:
                parked = _park_exhausted(
                    wl, f"infra failure after {attempt} attempts (e.g. model 403 "
                    "or network error that never reached the agent)",
                    path="exhausted-infra",
                )
            results.append(f"{name}:giveup-infra:{infra_attempt}/{infra_max_attempts}")
            # Tombstone is retired only after the issue marker is safely written;
            # a failed park leaves it for the next tick. The delete itself is
            # best-effort: a wedged finalizer (LLMKube#949) raises TimeoutError
            # after 60s, which would otherwise abort the whole reconcile pass
            # and block every downstream stage. Leaving the tombstone alive is
            # acceptable — list_failed() returns it next tick, where the wrap
            # lets the rest of the bridge run again.
            if parked:
                try:
                    delete_workload(name)
                except Exception:
                    logger.exception(
                        "giveup-infra-delete-failed",
                        extra={"workload": name},
                    )
            continue
        if attempt >= max_attempts and not is_infra:
            item = refresh_lane(item_from_workload(wl), current_lane_for)
            if not item.issue_id and lookup_issue_id:
                # Workloads created before the issue-id annotation (bridge <0.3.0)
                # carry "" forever through retries; recover the id from the
                # dispatch queue so they can still escalate.
                item = replace(item, issue_id=lookup_issue_id(item) or "")
            can_escalate = (
                escalate is not None
                and escalation_lane
                and item.lane != escalation_lane
                and item.issue_id
            )
            # An escalate hook that raises (dispatch 400 on unclaim for a
            # closed/done issue) must not abort the rest of the reconcile pass
            # and the downstream claim/pr-fix passes. Treat a raise like a False
            # return: keep the tombstone, the next tick retries the escalation.
            try:
                escalated = bool(can_escalate and escalate is not None and escalate(item))
                if escalated:
                    delete_workload(name)
            except Exception as e:
                results.append(f"{name}:escalate-error:{e}")
                continue
            if escalated:
                results.append(f"{name}:escalated:{item.lane or '?'}->{escalation_lane}")
            else:
                # Same reasoning as the infra branch: without parking, an
                # unescalatable exhausted Workload pins its issue in-progress
                # forever.
                parked = _park_exhausted(
                    wl, f"exhausted {attempt} attempts with no escalation lane "
                    "available",
                )
                results.append(f"{name}:giveup:{attempt}/{max_attempts}")
                # Same as the infra branch above: a successful park retires the
                # tombstone so list_failed() does not re-park it next tick.
                # The delete itself is best-effort: a wedged finalizer
                # (LLMKube#949) raises TimeoutError after 60s, which would
                # otherwise abort the whole reconcile pass and block every
                # downstream stage.
                if parked:
                    try:
                        delete_workload(name)
                    except Exception:
                        logger.exception(
                            "giveup-verdict-delete-failed",
                            extra={"workload": name},
                        )
            continue
        item = refresh_lane(item_from_workload(wl), current_lane_for)
        # Backfill issue-id BEFORE the delete so the rebuilt Workload carries
        # it (matches the escalation branch's behaviour). Workloads created
        # before the issue-id annotation (bridge <0.3.0) carry "" forever
        # through retries without this.
        if not item.issue_id and lookup_issue_id:
            item = replace(item, issue_id=lookup_issue_id(item) or "")
        # Collect the previous attempt's review findings / failure BEFORE the
        # delete (the tasks go with the Workload). A retry that knows why it
        # was rejected beats a blind identical re-run.
        feedback = feedback_for(name) if feedback_for else ""
        # Same reason as feedback: the tasks are deleted with the Workload, so the
        # branch evidence has to be read BEFORE the delete below.
        task_branch = _branch_name(item)
        revise_from = (
            task_branch
            if branch_pushed_for and branch_pushed_for(name, task_branch, item.repo)
            else ""
        )
        # Per-workload isolation: a delete that wedges (e.g. a Workload whose
        # deletion never completes, LLMKube#949) or a create that races must
        # not abort the rest of the reconcile pass and the claim pass — one
        # bad Workload previously crashed the whole bridge run every tick.
        try:
            delete_workload(name)
            language = gate_profiles.get(item.repo, {}).get("language")
            # Infra errors are not real rejections — the request never reached
            # the agent — so a retry against the same backend must not spend
            # the verdict budget. Real verdicts increment as before. Infra
            # failures have their own counter (infra-attempt) so a permanently
            # broken backend still gives up after INFRA_MAX_ATTEMPTS retries.
            if is_infra:
                next_attempt = attempt
                next_infra_attempt = infra_attempt + 1
            else:
                next_attempt = attempt + 1
                next_infra_attempt = infra_attempt
            manifest = build_workload(
                item,
                namespace,
                gate_profile_for(item.repo, gate_profiles),
                agent_name,
                next_attempt,
                infra_attempt=next_infra_attempt,
                coder_agent=coder_agent_for(
                    item.lane, language, lane_coder_agents, base_coder_agents,
                    repo=item.repo, repo_coder_agents=repo_coder_agents,
                    issue_number=item.issue_number,
                ),
                feedback=feedback,
                verify_enabled=verify_enabled,
                self_go=self_go,
                revise_from_branch=revise_from,
            )
            create_workload(manifest)
        except Exception as e:
            results.append(f"{name}:retry-error:{e}")
            continue
        if is_infra:
            results.append(f"{name}:retry-infra:{infra_attempt}/{infra_max_attempts}")
        else:
            results.append(f"{name}:retry:{attempt + 1}/{max_attempts}")
    return results
