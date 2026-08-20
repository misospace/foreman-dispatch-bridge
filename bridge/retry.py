import logging
from dataclasses import replace
from typing import Callable, Optional

from bridge.models import ClaimedItem
from bridge.workload import (
    build_workload,
    coder_agent_for,
    gate_profile_for,
    _branch_name,
    ATTEMPT_ANNOTATION,
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

ListFailed = Callable[[], list]         # () -> list of Failed Workload manifests (dicts)
DeleteWorkload = Callable[[str], None]  # (name) -> None; blocks until the object is gone
Escalate = Callable[[ClaimedItem], bool]  # (item) -> True when re-laned + unclaimed
LookupIssueId = Callable[[ClaimedItem], str]   # (item) -> dispatch issue id, "" if not found
FeedbackFor = Callable[[str], str]             # (workload name) -> retry feedback text, "" if none
BranchPushedFor = Callable[[str, str, str], bool]  # (workload name, branch, repo) -> did its task branch reach the remote
IssueStateFor = Callable[[ClaimedItem], Optional[str]]  # (item) -> "open"/"closed", None if unknown
DeclaredEscalationFor = Callable[[str], Optional[str]]  # (workload name) -> declared reason or None


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
DECLARED_ESCALATIONS = frozenset({"DESIGN-DECISION", "NO-TECHNICAL-FIX"})


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
    park_for_human: Optional[Callable[[ClaimedItem, str], bool]],
    current_lane_for: Optional[dict],
    lookup_issue_id: Optional[LookupIssueId],
) -> Callable[[dict, str], bool]:
    """Build the _park_exhausted closure used by the giveup branches.

    Parking is best-effort: a failure here must not abort the reconcile pass,
    because the remaining Workloads and the downstream claim/pr-fix passes
    still need to run. The tombstone is left in place either way, so a failed
    park degrades to today's behaviour rather than losing the work.
    """

    def _park_exhausted(wl: dict, reason: str) -> bool:
        if park_for_human is None:
            return False
        try:
            item = refresh_lane(item_from_workload(wl), current_lane_for)
            if not item.issue_id and lookup_issue_id:
                item = replace(item, issue_id=lookup_issue_id(item) or "")
            return bool(park_for_human(item, reason))
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
    park_for_human: Optional[Callable[[ClaimedItem, str], bool]] = None,
    infra_max_attempts: int = INFRA_MAX_ATTEMPTS,
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
    genuine rejections.

    Returns per-Workload outcome strings.
    """
    lane_coder_agents = lane_coder_agents or {}
    base_coder_agents = base_coder_agents or {}
    repo_coder_agents = repo_coder_agents or {}
    results = []
    _park_exhausted = _park_exhausted_factory(
        park_for_human, current_lane_for, lookup_issue_id
    )
    for wl in list_failed():
        name = (wl.get("metadata") or {}).get("name") or "?"
        attempt = attempt_of(wl)
        # An ExecutorError on a task's Completed condition means the request
        # never reached the agent (model 403, network drop, etc.) — so the
        # failure is an infrastructure problem and must not consume the
        # verdict retry budget. Such Workloads get their own budget below.
        is_infra = task_failed_with_executor_error(wl)

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
            if reason:
                item_h = refresh_lane(item_from_workload(wl), current_lane_for)
                if not item_h.issue_id and lookup_issue_id:
                    item_h = replace(item_h, issue_id=lookup_issue_id(item_h) or "")
                parked = False
                if park_for_human is not None:
                    try:
                        parked = bool(park_for_human(item_h, reason))
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
        if is_infra and attempt >= infra_max_attempts:
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
            parked = _park_exhausted(
                wl, f"infra failure after {attempt} attempts (e.g. model 403 "
                "or network error that never reached the agent)",
            )
            results.append(f"{name}:giveup-infra:{attempt}/{infra_max_attempts}")
            # Tombstone must be retired on a successful park or the next tick
            # re-lists it and re-posts the needs-human comment every tick until
            # the 48h prune. A failed park keeps the tombstone so the next tick
            # retries the park.
            if parked:
                delete_workload(name)
            continue
        if attempt >= max_attempts:
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
                if parked:
                    delete_workload(name)
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
            # the verdict budget. Real verdicts increment as before.
            next_attempt = attempt + 1 if not is_infra else attempt
            manifest = build_workload(
                item,
                namespace,
                gate_profile_for(item.repo, gate_profiles),
                agent_name,
                next_attempt,
                coder_agent_for(
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
            results.append(f"{name}:retry-infra:{attempt}/{infra_max_attempts}")
        else:
            results.append(f"{name}:retry:{attempt + 1}/{max_attempts}")
    return results
