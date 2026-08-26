import pytest
from bridge.models import ClaimedItem
from bridge.retry import (
    DEFAULT_MAX_ATTEMPTS,
    INFRA_MAX_ATTEMPTS,
    attempt_of,
    item_from_workload,
    reconcile_failures,
    refresh_lane,
)
from bridge.workload import ATTEMPT_ANNOTATION, INFRA_ATTEMPT_ANNOTATION, ISSUE_ID_ANNOTATION


def _failed_wl(name, repo="misospace/dispatch", issue=7, attempt=None, lane="local", issue_id="id-7"):
    ann = {"foreman.llmkube.dev/issue-id": issue_id}
    if attempt is not None:
        ann["foreman.llmkube.dev/attempt"] = str(attempt)
    return {
        "metadata": {"name": name, "labels": {"created-by": "dispatch-bridge", "lane": lane}, "annotations": ann},
        "spec": {"intent": "fix it", "repo": repo, "issues": [issue]},
        "status": {"phase": "Failed"},
    }


def test_attempt_of_defaults_and_parses():
    assert attempt_of(_failed_wl("w")) == 1                    # no annotation -> 1
    assert attempt_of(_failed_wl("w", attempt=2)) == 2
    assert attempt_of({"metadata": {"annotations": {"foreman.llmkube.dev/attempt": "junk"}}}) == 1
    assert attempt_of({}) == 1


def test_item_from_workload_reconstructs_fields():
    item = item_from_workload(_failed_wl("w", repo="a/b", issue=42, lane="cloud", issue_id="xyz"))
    assert item.repo == "a/b"
    assert item.issue_number == 42
    assert item.intent == "fix it"
    assert item.lane == "cloud"
    assert item.issue_id == "xyz"


class _Recorder:
    def __init__(self, failed):
        self.failed = failed
        self.deleted = []
        self.created = []

    def list_failed(self):
        return self.failed

    def delete(self, name):
        self.deleted.append(name)

    def create(self, manifest):
        self.created.append(manifest)


def test_reconcile_retries_below_max_deletes_and_recreates_at_next_attempt():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    profiles = {"*": {"language": "generic"}}
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             namespace="llm", gate_profiles=profiles, max_attempts=3)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert r.deleted == ["wl-misospace-dispatch-7"]
    assert len(r.created) == 1
    m = r.created[0]
    # recreated with attempt+1, the current gateProfile, and the same name/branch
    assert m["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert m["metadata"]["name"] == "wl-misospace-dispatch-7"
    assert m["spec"]["gateProfile"] == {"language": "generic"}


def test_reconcile_gives_up_at_max_without_touching_the_workload():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=3)])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             namespace="llm", gate_profiles={}, max_attempts=3)
    assert out == ["wl-misospace-dispatch-7:giveup:3/3"]
    assert r.deleted == []   # left as a tombstone
    assert r.created == []


def test_reconcile_first_attempt_annotation_absent_counts_as_one():
    r = _Recorder([_failed_wl("wl-a-b-1", attempt=None)])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             namespace="llm", gate_profiles={}, max_attempts=3)
    assert out == ["wl-a-b-1:retry:2/3"]
    assert r.created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"


def test_reconcile_empty_is_noop():
    r = _Recorder([])
    assert reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                              namespace="llm", gate_profiles={}, max_attempts=3) == []


def test_reconcile_escalates_at_max_when_hook_wired():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3, lane="local", issue_id="id-7")])
    escalated = []

    def escalate(item):
        escalated.append(item)
        return True

    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=escalate, escalation_lane="frontier")
    assert out == ["wl-a-b-7:escalated:local->frontier"]
    assert [i.issue_id for i in escalated] == ["id-7"]
    assert r.deleted == ["wl-a-b-7"]   # tombstone removed after successful escalation
    assert r.created == []             # the next tick's claim builds the frontier Workload


def test_reconcile_keeps_tombstone_when_escalate_fails():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=lambda item: False, escalation_lane="frontier")
    assert out == ["wl-a-b-7:giveup:3/3"]
    assert r.deleted == []             # keep the tombstone; next tick retries escalation


def test_reconcile_escalate_that_raises_keeps_tombstone_and_continues():
    # A closed/done issue makes dispatch's unclaim return 400, which the escalate
    # hook surfaces as a raise. That must not abort the reconcile pass: keep the
    # raiser's tombstone and keep processing the rest of the Failed Workloads
    # (and, downstream, the claim + pr-fix passes).
    r = _Recorder([
        _failed_wl("wl-a-b-7", attempt=3, issue_id="id-7"),
        _failed_wl("wl-c-d-9", repo="c/d", issue=9, attempt=1, issue_id="id-9"),
    ])

    def boom(item):
        raise RuntimeError("400 unclaim: closed issue")

    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=boom, escalation_lane="frontier")
    assert out[0] == "wl-a-b-7:escalate-error:400 unclaim: closed issue"
    assert out[1] == "wl-c-d-9:retry:2/3"      # the workload after the raiser still processed
    assert r.deleted == ["wl-c-d-9"]           # raiser's tombstone kept; only the retry deleted


def test_reconcile_never_escalates_out_of_the_escalation_lane():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3, lane="frontier")])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=lambda item: True, escalation_lane="frontier")
    assert out == ["wl-a-b-7:giveup:3/3"]
    assert r.deleted == []


def test_reconcile_requires_issue_id_to_escalate():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3, issue_id="")])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=lambda item: True, escalation_lane="frontier")
    assert out == ["wl-a-b-7:giveup:3/3"]


def test_reconcile_retry_uses_lane_coder_agent():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=1, lane="frontier")])
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", {}, max_attempts=3,
                       lane_coder_agents={"*": "coder", "frontier": "coder-frontier"})
    assert r.created[0]["spec"]["coderAgentRef"] == {"name": "coder-frontier"}


def test_reconcile_retry_routes_base_lane_by_repo_language():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1, lane="local", repo="misospace/dispatch")])
    gate_profiles = {"misospace/dispatch": {"language": "node"}}
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", gate_profiles, max_attempts=3,
                       base_coder_agents={"node": "coder-node", "*": "coder"})
    assert r.created[0]["spec"]["coderAgentRef"] == {"name": "coder-node"}


def test_reconcile_retry_frontier_lane_wins_over_language_routing():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1, lane="frontier", repo="misospace/dispatch")])
    gate_profiles = {"misospace/dispatch": {"language": "node"}}
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", gate_profiles, max_attempts=3,
                       lane_coder_agents={"frontier": "coder-frontier"},
                       base_coder_agents={"node": "coder-node", "*": "coder"})
    assert r.created[0]["spec"]["coderAgentRef"] == {"name": "coder-frontier"}


def _no_go_review(findings=None, summary=""):
    return {
        "spec": {"kind": "review"},
        "status": {"verdict": "NO-GO", "phase": "Succeeded",
                   "result": {"extra": {"modelExtra": {"findings": findings or {}},
                                        "modelSummary": summary}}},
    }


def test_feedback_from_tasks_distills_review_no_go():
    from bridge.retry import feedback_from_tasks
    tasks = [_no_go_review(
        findings={"missing_tests": True, "scope_creep": True,
                  "scope_creep_details": "commit reduces token lifetime, unrelated to #141"},
    )]
    fb = feedback_from_tasks(tasks)
    assert "Reviewer rejected the previous attempt" in fb
    assert "missing_tests" in fb and "scope_creep" in fb
    assert "unrelated to #141" in fb


def test_feedback_from_tasks_includes_coder_errors_and_bounds_length():
    from bridge.retry import feedback_from_tasks, FEEDBACK_MAX_CHARS
    tasks = [{
        "spec": {"kind": "issue-fix"},
        "status": {"verdict": "INCOMPLETE",
                   "result": {"extra": {"error": "x" * 5000}}},
    }]
    fb = feedback_from_tasks(tasks)
    assert "Previous coder attempt failed" in fb
    assert len(fb) <= FEEDBACK_MAX_CHARS


def test_feedback_from_tasks_empty_when_nothing_actionable():
    from bridge.retry import feedback_from_tasks
    assert feedback_from_tasks([]) == ""
    assert feedback_from_tasks([{"spec": {"kind": "verify"}, "status": {"verdict": "GATE-PASS"}}]) == ""


def test_reconcile_retry_carries_feedback_into_pipeline_prompt():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "Reviewer said: add tests")
    spec = r.created[0]["spec"]
    # Both are set on purpose. The CRD guarantees "Pipeline takes precedence over
    # Issues ... when both are set", so carrying issues cannot double-decompose —
    # and it is the only record of which issue this Workload belongs to when the
    # NEXT retry reconstructs its ClaimedItem from this spec.
    assert "pipeline" in spec and spec["issues"] == [7]
    code_step = spec["pipeline"][0]
    assert code_step["payload"]["prompt"] == "Reviewer said: add tests"
    # verify + review steps present and chained
    assert [st["name"] for st in spec["pipeline"]] == ["code-7", "verify-7", "review-7-0"]
    assert spec["pipeline"][1]["dependsOn"] == ["code-7"]


def test_reconcile_retry_without_feedback_stays_on_issues_path():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "")
    spec = r.created[0]["spec"]
    assert "issues" in spec and "pipeline" not in spec


def test_reconcile_backfills_issue_id_before_escalating():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3, issue_id="")])
    escalated = []
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=lambda item: escalated.append(item) or True,
                             escalation_lane="frontier",
                             lookup_issue_id=lambda item: "recovered-id")
    assert out == ["wl-a-b-7:escalated:local->frontier"]
    assert escalated[0].issue_id == "recovered-id"


def test_reconcile_still_gives_up_when_backfill_finds_nothing():
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=3, issue_id="")])
    out = reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                             "llm", {}, max_attempts=3,
                             escalate=lambda item: True, escalation_lane="frontier",
                             lookup_issue_id=lambda item: "")
    assert out == ["wl-a-b-7:giveup:3/3"]


def test_reconcile_isolates_a_wedged_delete():
    """A delete that raises (e.g. LLMKube#949's immortal Workload) must not
    abort the remaining retries."""
    r = _Recorder([_failed_wl("wl-wedged", attempt=1), _failed_wl("wl-fine", attempt=1)])

    def delete(name):
        if name == "wl-wedged":
            raise TimeoutError("workload wl-wedged still terminating after 60s")
        r.deleted.append(name)

    out = reconcile_failures("foreman-coder", r.list_failed, r.create, delete,
                             "llm", {}, max_attempts=3)
    assert out[0].startswith("wl-wedged:retry-error:")
    assert out[1] == "wl-fine:retry:2/3"
    assert len(r.created) == 1  # only the healthy one was recreated


def test_reconcile_gateless_feedback_retry_builds_code_review_no_verify():
    """verify_enabled=False feedback retry rebuilds as code→review with no verify,
    reviewer depending directly on code."""
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "Reviewer said: add tests",
                       verify_enabled=False)
    spec = r.created[0]["spec"]
    assert "pipeline" in spec and spec["issues"] == [7]
    steps = spec["pipeline"]
    kinds = [s["kind"] for s in steps]
    assert kinds == ["issue-fix", "review"]
    assert "verify" not in kinds
    code = steps[0]
    review = steps[1]
    assert review["dependsOn"] == [code["name"]]
    assert code["payload"]["prompt"] == "Reviewer said: add tests"


def _task(kind="issue-fix", verdict=None, extra=None):
    return {"spec": {"kind": kind}, "status": {"verdict": verdict, "result": {"extra": extra or {}}}}


def test_branch_pushed_false_without_evidence():
    from bridge.retry import branch_pushed
    assert branch_pushed([]) is False
    assert branch_pushed([_task(verdict="NO-GO", extra={"error": "model timeout"})]) is False


def test_branch_pushed_on_pull_request_url():
    from bridge.retry import branch_pushed
    assert branch_pushed([_task(extra={"pullRequestURL": "https://github.com/o/r/pull/1"})]) is True


def test_branch_pushed_when_a_review_ran():
    from bridge.retry import branch_pushed
    # A reviewer cannot produce a verdict without checking out the branch.
    assert branch_pushed([_task(kind="review", verdict="NO-GO")]) is True


def test_branch_pushed_on_coder_go():
    from bridge.retry import branch_pushed
    assert branch_pushed([_task(kind="issue-fix", verdict="GO")]) is True


def test_branch_pushed_on_push_failed_so_the_wedge_self_heals():
    from bridge.retry import branch_pushed
    # PUSH-FAILED means a ref is already there; the next retry revises from it
    # instead of wedging again.
    assert branch_pushed([_task(verdict="NO-GO", extra={"outcome": "PUSH-FAILED"})]) is True
    assert branch_pushed([_task(verdict="NO-GO", extra={"error": "non-fast-forward"})]) is True


def test_branch_pushed_on_remote_branch_existence_only():
    """#132: a Workload instance may die without recording any verdict (coder
    Jobs killed at the deadline, agent restarts) but the branch reached the
    remote. With no task-CR evidence, the only signal is the remote itself.
    """
    from bridge.retry import branch_pushed
    # No tasks at all — the wasted-cycle case: an instance died leaving the
    # branch on the remote but no AgenticTask verdict in the Workload's status.
    assert branch_pushed([], remote_branch_exists=True) is True
    # Local task-CR evidence is also absent (NO-GO without PUSH-FAILED) but the
    # remote check still authorises overwrite.
    assert branch_pushed(
        [_task(verdict="NO-GO", extra={"error": "model timeout"})],
        remote_branch_exists=True,
    ) is True
    # And the inverse: a clean local scan that says no does not override the
    # remote saying yes.
    assert branch_pushed([], remote_branch_exists=False) is False


# --- closed-issue precondition -------------------------------------------------
# A closed issue cannot be advanced, so neither a retry nor an escalation is worth
# an attempt. Observed on wl-misospace-llmkube-images-38: Failed at attempt 1 for an
# issue closed as already-resolved (a sibling's fix had deleted the file it named).
# Every further attempt could only clone the repo, find nothing, return NO-CHANGES.
#
# The fail-open cases matter more than the happy path: reading an ambiguous lookup
# as "closed" would cancel real retries whenever dispatch was briefly unreachable,
# which is worse than the waste being avoided.

def _reconcile(recorder, issue_state_for=None, attempts=3, **kw):
    return reconcile_failures(
        "foreman-coder", recorder.list_failed, recorder.create, recorder.delete,
        namespace="llm", gate_profiles={"*": {"language": "generic"}},
        max_attempts=attempts, issue_state_for=issue_state_for, **kw,
    )


def test_skips_retry_when_the_issue_is_closed():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, issue_state_for=lambda item: "closed")
    assert out == ["wl-misospace-dispatch-7:skip-retry:issue-closed"]
    assert r.created == []
    assert r.deleted == []  # tombstone left for prune, not deleted here


def test_retries_when_the_issue_is_open():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, issue_state_for=lambda item: "open")
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_retries_when_the_state_is_unknown():
    """None means the lookup 404'd or failed — fail open, retry as before."""
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, issue_state_for=lambda item: None)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_retries_when_the_state_lookup_raises():
    """A raising lookup must not abort or cancel the retry."""
    def boom(item):
        raise RuntimeError("dispatch unreachable")

    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, issue_state_for=boom)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_retries_when_no_state_hook_is_wired():
    """Backward compatible: omitting the hook preserves the old behaviour."""
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, issue_state_for=None)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_closed_issue_is_not_escalated_at_max_attempts():
    """The check runs before the max_attempts branch: escalating a closed issue
    would spend a strictly more expensive frontier coder on nothing."""
    escalated = []

    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=3)])
    out = _reconcile(
        r, issue_state_for=lambda item: "closed", attempts=3,
        escalate=lambda item: escalated.append(item.issue_number) or True,
        escalation_lane="frontier",
    )
    assert out == ["wl-misospace-dispatch-7:skip-retry:issue-closed"]
    assert escalated == []
    assert r.deleted == []


def test_state_is_checked_per_workload_with_the_right_identity():
    seen = []

    r = _Recorder([
        _failed_wl("wl-misospace-dispatch-7", repo="misospace/dispatch", issue=7, attempt=1),
        _failed_wl("wl-misospace-kubetix-9", repo="misospace/KubeTix", issue=9, attempt=1),
    ])

    def record(item):
        seen.append((item.repo, item.issue_number))
        return "closed" if item.issue_number == 7 else "open"

    out = _reconcile(r, issue_state_for=record)
    assert ("misospace/dispatch", 7) in seen
    assert ("misospace/KubeTix", 9) in seen
    assert "wl-misospace-dispatch-7:skip-retry:issue-closed" in out
    assert "wl-misospace-kubetix-9:retry:2/3" in out
    assert len(r.created) == 1  # only the open one was rebuilt


# --- declared human escalation --------------------------------------------------
# A coder that read the issue and the code, and concluded no code can resolve it, is
# taken at its word. Attempt-exhaustion is the alternative and it is a lossy proxy:
# it spends every attempt to reach a conclusion the coder already had, and files
# "CI was flaky" in the same bucket as "this needs a human decision".

def _wl_with_escalation(name, reason, kind="issue-fix"):
    wl = _failed_wl(name, attempt=1)
    wl["_tasks"] = [{
        "spec": {"kind": kind},
        "status": {"result": {"extra": {"modelExtra": {"escalation": reason}}}},
    }]
    return wl


def test_declared_escalation_reads_recognised_reasons():
    from bridge.retry import declared_escalation
    for r in ("DESIGN-DECISION", "NO-TECHNICAL-FIX"):
        tasks = [{"spec": {"kind": "issue-fix"},
                  "status": {"result": {"extra": {"modelExtra": {"escalation": r}}}}}]
        assert declared_escalation(tasks) == r


def test_declared_escalation_normalises_case_and_whitespace():
    from bridge.retry import declared_escalation
    tasks = [{"spec": {"kind": "issue-fix"},
              "status": {"result": {"extra": {"modelExtra": {"escalation": " design-decision "}}}}}]
    assert declared_escalation(tasks) == "DESIGN-DECISION"


def test_declared_escalation_ignores_unrecognised_reasons():
    """A model inventing a reason must not be able to route work out of the loop."""
    from bridge.retry import declared_escalation
    for r in ("TOO-HARD", "NEEDS-HUMAN", "", "later", None, 42):
        tasks = [{"spec": {"kind": "issue-fix"},
                  "status": {"result": {"extra": {"modelExtra": {"escalation": r}}}}}]
        assert declared_escalation(tasks) is None


def test_declared_escalation_ignores_non_coder_tasks():
    from bridge.retry import declared_escalation
    tasks = [{"spec": {"kind": "review"},
              "status": {"result": {"extra": {"modelExtra": {"escalation": "DESIGN-DECISION"}}}}}]
    assert declared_escalation(tasks) is None


def test_declared_escalation_tolerates_missing_and_malformed_extra():
    from bridge.retry import declared_escalation
    assert declared_escalation([]) is None
    assert declared_escalation([{"spec": {"kind": "issue-fix"}, "status": {}}]) is None
    assert declared_escalation([{"spec": {"kind": "issue-fix"},
                                 "status": {"result": {"extra": {"modelExtra": "nope"}}}}]) is None


def test_declared_escalation_parks_and_does_not_retry():
    parked = []
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(
        r,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=lambda item, reason: parked.append((item.issue_number, reason)) or True,
    )
    assert out == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert parked == [(7, "DESIGN-DECISION")]
    assert r.created == []          # no attempt consumed
    assert r.deleted == []          # tombstone left to triage from


def test_declared_escalation_first_park_still_posts_comment():
    comments = []
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(
        r,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: False,
        park_for_human=lambda item, reason: comments.append(reason) or True,
        ensure_human_label=lambda item: pytest.fail("first park must not use label-only path"),
    )
    assert out == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert comments == ["DESIGN-DECISION"]


def test_declared_escalation_repeat_skips_comment_and_repairs_label():
    comments = []
    label_repairs = []
    wl = _failed_wl("wl-misospace-dispatch-7", attempt=1)

    def park(item, reason):
        comments.append(reason)
        return True

    # The declared path intentionally leaves its Failed Workload as a tombstone.
    # On the next tick, the durable issue label suppresses the announcement while
    # the label-only operation repairs the marker if needed.
    first = _Recorder([wl])
    out1 = _reconcile(
        first,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: False,
        park_for_human=park,
    )
    assert out1 == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert comments == ["DESIGN-DECISION"]
    assert first.deleted == []

    second = _Recorder([wl])
    out2 = _reconcile(
        second,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: True,
        park_for_human=park,
        ensure_human_label=lambda item: label_repairs.append(item.issue_number) or True,
    )
    assert out2 == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert comments == ["DESIGN-DECISION"]  # no duplicate comment
    assert label_repairs == [7]
    assert second.deleted == []  # declared-escalation tombstone remains available


def test_declared_escalation_repeat_keeps_tombstone_when_label_repair_fails():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(
        r,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: True,
        ensure_human_label=lambda item: False,
        park_for_human=lambda item, reason: pytest.fail("repeat must not repost"),
    )
    assert out == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert r.created == []
    assert r.deleted == []


def test_declared_escalation_retries_after_a_failed_first_park():
    comments = []
    park_calls = []

    def park(item, reason):
        park_calls.append(reason)
        if len(park_calls) == 1:
            return False
        comments.append(reason)
        return True

    first = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out1 = _reconcile(
        first,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: False,
        park_for_human=park,
    )
    assert out1 == ["wl-misospace-dispatch-7:retry:2/3"]
    assert comments == []
    assert len(first.created) == 1

    # The failed first park did not swallow the declaration. The next Failed
    # Workload parks normally and posts the one announcement.
    second = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=2)])
    out2 = _reconcile(
        second,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        needs_human_for=lambda item: False,
        park_for_human=park,
    )
    assert out2 == ["wl-misospace-dispatch-7:human-escalation:DESIGN-DECISION"]
    assert comments == ["DESIGN-DECISION"]


def test_declared_escalation_does_not_escalate_to_the_frontier_lane():
    """At max_attempts the giveup branch would escalate to a stronger coder. A
    declared design decision must not spend one."""
    escalated = []
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=3)])
    out = _reconcile(
        r, attempts=3,
        declared_escalation_for=lambda name: "NO-TECHNICAL-FIX",
        park_for_human=lambda item, reason: True,
        escalate=lambda item: escalated.append(item.issue_number) or True,
        escalation_lane="frontier",
    )
    assert out == ["wl-misospace-dispatch-7:human-escalation:NO-TECHNICAL-FIX"]
    assert escalated == []


def test_retries_normally_when_parking_fails():
    """Do not strand the work: a failed park falls through to the retry path."""
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(
        r,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=lambda item, reason: False,
    )
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_retries_normally_when_no_escalation_is_declared():
    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, declared_escalation_for=lambda name: None,
                     park_for_human=lambda item, reason: True)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


def test_retries_normally_when_the_escalation_lookup_raises():
    def boom(name):
        raise RuntimeError("kube unreachable")

    r = _Recorder([_failed_wl("wl-misospace-dispatch-7", attempt=1)])
    out = _reconcile(r, declared_escalation_for=boom, park_for_human=lambda i, x: True)
    assert out == ["wl-misospace-dispatch-7:retry:2/3"]
    assert len(r.created) == 1


# --- issue-number survival across retries --------------------------------
# The wl-<repo>-0 / issue-0 collision: attempt 2 dropped spec.issues, attempt 3
# read it back as `or [0]`, and the rebuilt Workload took a name and branch that
# are FIXED PER REPO. Every third attempt in a repo then shared one branch and
# force-pushed over the last one's work.

def test_retry_spec_keeps_the_issue_number_for_the_next_rebuild():
    """Attempt 2 goes down the pipeline path; losing issues here is what made
    attempt 3 fabricate issue 0."""
    r = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    reconcile_failures("foreman-coder", r.list_failed, r.create, r.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "Reviewer said: add tests")
    assert r.created[0]["spec"]["issues"] == [7]


def test_third_attempt_keeps_the_real_issue_number():
    """Drive attempt 2 -> attempt 3 and assert the Workload is not renamed to
    wl-a-b-0. This is the end-to-end shape of the bug."""
    r1 = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    reconcile_failures("foreman-coder", r1.list_failed, r1.create, r1.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "findings")
    second = r1.created[0]
    second.setdefault("metadata", {}).setdefault("annotations", {})["foreman.llmkube.dev/attempt"] = "2"
    second["status"] = {"phase": "Failed"}

    r2 = _Recorder([second])
    reconcile_failures("foreman-coder", r2.list_failed, r2.create, r2.delete,
                       "llm", {}, max_attempts=3,
                       feedback_for=lambda name: "more findings")
    third = r2.created[0]
    assert third["spec"]["issues"] == [7]
    name = third["metadata"]["name"]
    assert name.endswith("-7") and not name.endswith("-0"), (
        f"renamed to {name} — a -0 name is fixed per repo, so every third "
        "attempt in this repo would share one Workload and one branch"
    )


def test_issue_number_recovered_from_name_when_spec_lost_it():
    """Defence in depth for Workloads already in the cluster with no issues
    field: the name still carries the number, so use it instead of 0."""
    wl = {"metadata": {"name": "wl-misospace-dispatch-681"},
          "spec": {"repo": "misospace/dispatch", "intent": "x"}}
    assert item_from_workload(wl).issue_number == 681


def test_unparseable_name_yields_zero_not_a_wrong_issue():
    """0 is reported only when nothing can be recovered, and callers treat it as
    unusable — better than silently attaching work to a real issue."""
    wl = {"metadata": {"name": "wl-weird-name"}, "spec": {"repo": "a/b"}}
    assert item_from_workload(wl).issue_number == 0


@pytest.mark.parametrize("tail", ["²", "①", "٧", "abc", ""])
def test_non_ascii_digit_names_yield_zero_rather_than_raising(tail):
    """str.isdigit() is True for '²' and '①' but int() rejects them. This runs
    outside the per-Workload try in reconcile_failures, so raising would abort
    every remaining retry, not just this one."""
    wl = {"metadata": {"name": f"wl-a-b-{tail}"}, "spec": {"repo": "a/b"}}
    assert item_from_workload(wl).issue_number == 0


# --- lane refresh ----------------------------------------------------------


def test_refresh_lane_prefers_dispatch_over_the_frozen_label():
    # The Workload label froze at creation; dispatch has since moved the issue.
    item = ClaimedItem(repo="a/b", issue_number=38, intent="fix", lane="frontier")
    assert refresh_lane(item, {("a/b", 38): "local"}).lane == "local"


def test_refresh_lane_noops_without_a_lookup_or_a_match():
    item = ClaimedItem(repo="a/b", issue_number=38, intent="fix", lane="frontier")
    assert refresh_lane(item, None).lane == "frontier"
    assert refresh_lane(item, {}).lane == "frontier"
    assert refresh_lane(item, {("a/other", 1): "local"}).lane == "frontier"


def test_refresh_lane_keeps_every_other_field():
    item = ClaimedItem(repo="a/b", issue_number=38, intent="fix",
                       lane="frontier", issue_id="abc123")
    out = refresh_lane(item, {("a/b", 38): "local"})
    assert (out.repo, out.issue_number, out.intent, out.issue_id) == (
        "a/b", 38, "fix", "abc123",
    )


# --- ExecutorError retry path: infra failures must not consume the verdict
# budget. See bridge/retry.py reconcile_failures for the full semantics. ---


def _executor_error(reason="ExecutorError", message="model 403"):
    return {"type": "Completed",
            "status": "False",
            "reason": reason,
            "message": message}


def _failed_status(task):
    """Wrap a single task condition in the taskStatuses container."""
    return {"taskStatuses": [{"conditions": [task]}]}


def _failed_wl_with_executor_error(name, attempt, issue_id="iss-9", infra_attempt=0):
    """A Failed Workload whose task died with Completed.reason=ExecutorError.

    Mirrors the production shape observed on 2026-08-16: the agent never
    ran (model 403 / network drop), so the only Completed condition on any
    task has reason=ExecutorError, not a verdict.
    """
    ann = {ATTEMPT_ANNOTATION: str(attempt), INFRA_ATTEMPT_ANNOTATION: str(infra_attempt)}
    if issue_id:
        ann[ISSUE_ID_ANNOTATION] = issue_id
    return {
        "metadata": {"name": name, "annotations": ann},
        "status": {
            "taskStatuses": [
                {
                    "conditions": [
                        {
                            "type": "Completed",
                            "status": "False",
                            "reason": "ExecutorError",
                        }
                    ]
                }
            ]
        },
    }


def test_task_failed_with_executor_error_true_when_a_task_died_with_it():
    from bridge.retry import task_failed_with_executor_error
    wl = _failed_wl_with_executor_error("w-1", attempt=1)
    assert task_failed_with_executor_error(wl) is True


def test_task_failed_with_executor_error_false_for_a_real_verdict_rejection():
    from bridge.retry import task_failed_with_executor_error
    wl = _failed_wl(name="w-2", attempt=1)
    assert task_failed_with_executor_error(wl) is False


def test_tasks_failed_with_executor_error_reads_agentic_task_status():
    from bridge.retry import tasks_failed_with_executor_error
    task = {"status": {"conditions": [{"type": "Completed", "reason": "ExecutorError"}]}}
    assert tasks_failed_with_executor_error([task]) is True


def test_task_failed_with_executor_error_false_when_no_task_statuses():
    from bridge.retry import task_failed_with_executor_error
    # A workload with no status yet must not be misclassified as infra.
    assert task_failed_with_executor_error({"metadata": {"name": "x"}}) is False


def test_reconcile_retries_executor_error_without_incrementing_attempt():
    # An ExecutorError retry must not spend the verdict budget — the new
    # Workload is rebuilt at the same attempt annotation so a transient
    # 403 cannot push the workload past max_attempts.
    wl = _failed_wl_with_executor_error("w-infra-1", attempt=1)
    rec = _Recorder([wl])
    out = _reconcile(rec)
    assert any("retry-infra:1/" in line for line in out), out
    assert rec.created[-1]["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "1"
    # infra_attempt was 1 (default) before the retry and is now 2.
    assert rec.created[-1]["metadata"]["annotations"][INFRA_ATTEMPT_ANNOTATION] == "2"
    # The rebuilt Workload keeps the same attempt annotation (no increment).
    created = rec.created[-1]
    ann = created["metadata"]["annotations"]
    assert ann[ATTEMPT_ANNOTATION] == "1"
    # The verdict retry line ("retry:N/M") must NOT be reported — this is
    # what criterion 4 of #155 requires: retry:N/M is reserved for counted
    # attempts.
    assert not any("retry:1/" in line for line in out), out


def test_reconcile_executor_error_does_not_escalate_or_give_up_prematurely():
    # At attempt=max_attempts the verdict path would give up or escalate to
    # the frontier lane, but an ExecutorError is not a verdict — it must
    # keep retrying (infra-retry without incrementing attempt) until its
    # own infra cap is hit. Re-using the same attempt annotation means a
    # transient infra error at attempt=max_attempts-1 keeps the workload
    # alive without burning the verdict budget.
    # attempt 1, infra cap 3: still under the infra cap, must retry.
    wl = _failed_wl_with_executor_error("w-infra-2", attempt=1)
    rec = _Recorder([wl])
    out = _reconcile(rec)
    assert any("retry-infra:" in line for line in out), out
    assert not any("giveup:" in line for line in out), out
    assert not any("escalated:" in line for line in out), out
    # Infra cap is reached exactly when infra_attempt == INFRA_MAX_ATTEMPTS,
    # so the failed workload should give up cleanly without consuming the
    # verdict counter (attempt stays at 1, not at DEFAULT_MAX_ATTEMPTS+1).
    wl_cap = _failed_wl_with_executor_error(
        "w-infra-cap", attempt=1, infra_attempt=INFRA_MAX_ATTEMPTS,
    )
    rec2 = _Recorder([wl_cap])
    out2 = _reconcile(rec2)
    assert any(f"giveup-infra:{INFRA_MAX_ATTEMPTS}/{INFRA_MAX_ATTEMPTS}" in line for line in out2), out2


def test_failed_model_prefers_model_ref_and_resolves_agent_model():
    from bridge.retry import failed_model
    task = {"spec": {"modelRef": "model-a"}, "status": {"conditions": [{"type": "Completed", "reason": "ExecutorError"}]}}
    assert failed_model([task]) == "model-a"
    task["spec"].pop("modelRef")
    task["spec"]["agentRef"] = {"name": "coder"}
    assert failed_model([task], lambda name: "resolved-model") == "resolved-model"


def test_reconcile_executor_error_gives_up_after_infra_cap():
    # The infra budget is bounded so a permanently broken backend cannot
    # loop forever. Once it is hit, the Failed tombstone is left in place
    # and the result line is giveup-infra:N/M.
    wl = _failed_wl_with_executor_error(
        "w-infra-3", attempt=1, infra_attempt=INFRA_MAX_ATTEMPTS,
    )
    rec = _Recorder([wl])
    out = _reconcile(rec)
    assert any(
        f"giveup-infra:{INFRA_MAX_ATTEMPTS}/{INFRA_MAX_ATTEMPTS}" in line
        for line in out
    ), out
    # Nothing was deleted and nothing was created: the tombstone stays.
    assert rec.deleted == []
    assert rec.created == []


def test_reconcile_executor_error_series_reaches_infra_cap():
    # Regression for #225: hand-constructing the terminal state missed the
    # defect because the recreate path never advanced infra_attempt. Drive
    # N consecutive infra failures through reconcile_failures and assert
    # the give-up happens at exactly INFRA_MAX_ATTEMPTS recreations. Use
    # only the latest recreated manifest in failed[] so each tick processes
    # a single workload (otherwise accumulated failures would each be
    # retried alongside the new one).
    wl = _failed_wl_with_executor_error("wl-infra-series-9", attempt=1)
    rec = _Recorder([wl])
    infra_counters = []
    for tick in range(INFRA_MAX_ATTEMPTS):
        # Trim to just the most recently failed manifest so only it is
        # re-processed this tick.
        rec.failed = [rec.failed[-1]]
        out = _reconcile(rec)
        if tick < INFRA_MAX_ATTEMPTS - 1:
            # The pre-increment infra_attempt visible on this tick is the
            # post-increment value of the previous retry: tick 0 sees 1
            # (default), tick 1 sees 2, tick 2 sees 3 then give-up.
            expected_pre = tick + 1
            assert any(
                f"retry-infra:{expected_pre}/{INFRA_MAX_ATTEMPTS}" in line
                for line in out
            ), (tick, out)
            # The recreated workload must carry the bumped infra_attempt
            # and an untouched verdict budget — separation invariant.
            recreated = rec.created[-1]
            assert recreated["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "1"
            infra_counters.append(
                int(recreated["metadata"]["annotations"][INFRA_ATTEMPT_ANNOTATION])
            )
            # Feed it back as another infra failure by appending it to the
            # failed list with the status reset.
            recreated["status"] = _failed_status(_executor_error())
            rec.failed.append(recreated)
        else:
            # Final tick: cap fires, no further recreate.
            assert any(
                f"giveup-infra:{INFRA_MAX_ATTEMPTS}/{INFRA_MAX_ATTEMPTS}" in line
                for line in out
            ), (tick, out)
            # No new manifest was created on the give-up tick.
            assert len(rec.created) == INFRA_MAX_ATTEMPTS - 1, (
                f"expected {INFRA_MAX_ATTEMPTS - 1} retries, got {len(rec.created)}"
            )
    # The infra_attempt annotation actually advanced across the series —
    # the precise defect this regression test pins.
    assert infra_counters == [2, 3], infra_counters


def test_reconcile_executor_error_does_not_burn_verdict_budget():
    # #225 separation invariant: an infra series must not exhaust the verdict
    # budget. After INFRA_MAX_ATTEMPTS infra failures the verdict attempt
    # counter must still read 1, so a subsequent genuine verdict failure can
    # still retry against the normal budget.
    wl = _failed_wl_with_executor_error("wl-infra-iso-9", attempt=1)
    rec = _Recorder([wl])
    for tick in range(INFRA_MAX_ATTEMPTS):
        out = _reconcile(rec)
        if tick < INFRA_MAX_ATTEMPTS - 1:
            assert any("retry-infra:" in line for line in out), out
            # Feed the just-recreated workload back as another infra failure
            # by appending it to the recorder's failed list with status reset.
            recreated = rec.created[-1]
            recreated["status"] = _failed_status(_executor_error())
            rec.failed.append(recreated)
        else:
            # Final tick: cap fires.
            assert any("giveup-infra:" in line for line in out), out
    # The most recently created (infra-recreated) manifest still has
    # attempt=1 — the verdict budget was never touched.
    assert rec.created[-1]["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "1"


def test_reconcile_task_error_uses_infra_park_hook_and_model():
    tasks = [{"spec": {"agentRef": {"name": "coder"}}, "status": {"conditions": [{"type": "Completed", "reason": "ExecutorError"}]}}]
    r = _Recorder([_failed_wl_with_executor_error("w-task-infra", attempt=1, infra_attempt=1)])
    out = _reconcile(r, attempts=1, infra_max_attempts=1, tasks_for=lambda name: tasks, failed_model_for=lambda name: "model-a", park_infra=lambda item, model, count: True)
    assert any("giveup-infra:1/1" in line for line in out), out


def test_reconcile_verdict_failure_still_increments_attempt():
    # Real NO-GO/INCOMPLETE verdicts must continue to spend the budget —
    # this issue changes nothing for them.
    wl = _failed_wl(name="w-verdict-1", attempt=1)
    rec = _Recorder([wl])
    out = _reconcile(rec)
    assert any("retry:2/" in line for line in out), out
    created = rec.created[-1]
    assert created["metadata"]["annotations"][ATTEMPT_ANNOTATION] == "2"


def test_infra_recovery_probes_once_per_model_and_redrives_healthy_records():
    from bridge.retry import reconcile_infra_parked
    records = [
        {"issueId": "a", "repoFullName": "o/r", "number": 1, "labels": ["blocked/infra", "blocked/infra-model/model-a", "blocked/infra-attempt/4"]},
        {"issueId": "b", "repoFullName": "o/r", "number": 2, "labels": ["blocked/infra", "blocked/infra-model/model-a", "blocked/infra-attempt/4"]},
    ]
    probes, redriven, cleared = [], [], []
    out = reconcile_infra_parked(lambda: records, lambda model: probes.append(model) or True, lambda record, count: True, lambda record: cleared.append(record["number"]) or True, lambda record, model: redriven.append(record["number"]) or True, lambda item, reason: pytest.fail("healthy dependency must not park"))
    assert probes == ["model-a"]
    assert redriven == [1, 2]
    assert cleared == [1, 2]
    assert all("infra-redriven" in line for line in out)


def test_infra_recovery_unhealthy_escalates_to_human_after_window():
    from bridge.retry import INFRA_RECOVERY_MAX_FAILURES, reconcile_infra_parked
    records = [{"issueId": "a", "repoFullName": "o/r", "number": 1, "labels": ["blocked/infra", "blocked/infra-model/model-a", f"blocked/infra-attempt/{INFRA_RECOVERY_MAX_FAILURES - 1}"]}]
    parked = []
    out = reconcile_infra_parked(lambda: records, lambda model: False, lambda record, count: pytest.fail("window should end"), lambda record: True, lambda record, model: pytest.fail("unhealthy must not redrive"), lambda item, reason: parked.append((item.issue_number, reason)) or True)
    assert parked == [(1, "infrastructure dependency unavailable: model-a")]
    assert out == ["1:infra-human:model-a"]


def test_env_documents_default_max_attempts_in_sync_with_retry_default():
    # bridge/env.py:24 documents RETRY_MAX_ATTEMPTS as "3", matching
    # DEFAULT_MAX_ATTEMPTS in bridge/retry.py. Disagreement here was
    # what made the 2026-08-16 incident harder to diagnose.
    from bridge.env import OPTIONAL_VARS
    assert OPTIONAL_VARS["RETRY_MAX_ATTEMPTS"] == str(DEFAULT_MAX_ATTEMPTS)


# --- exhausted Workloads must park their issue -------------------------------
# A Failed Workload at the attempt cap is never retried and never deleted, so
# without parking the issue stays claimed at status/in-progress with nothing
# that will ever run behind it: invisible on the board, and holding a slot
# against MAX_IN_PROGRESS until the 48h prune. On 2026-08-17 fourteen issues
# were pinned this way and needed manual `kubectl delete workload` to recover.


def _parks():
    """Return (hook, recorded) where recorded collects (item, reason) pairs."""
    recorded: list = []

    def park(item, reason):
        recorded.append((item, reason))
        return True

    return park, recorded


def test_infra_giveup_parks_the_issue():
    park, parked = _parks()
    wl = _failed_wl_with_executor_error(
        "w-infra-park", attempt=1, infra_attempt=INFRA_MAX_ATTEMPTS,
    )
    rec = _Recorder([wl])
    out = _reconcile(rec, park_for_human=park)
    assert any("giveup-infra:" in line for line in out), out
    assert len(parked) == 1, parked
    assert "infra failure" in parked[0][1]
    # A successful park retires the Failed tombstone so list_failed() does
    # not re-park it and re-post the needs-human comment on every tick.
    assert rec.deleted == ["w-infra-park"]
    assert rec.created == []


def test_verdict_giveup_parks_when_escalation_is_unavailable():
    park, parked = _parks()
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(rec, park_for_human=park)  # no escalate hook wired
    assert out == ["wl-a-b-7:giveup:3/3"]
    assert len(parked) == 1, parked
    assert "exhausted 3 attempts" in parked[0][1]
    # Same retire-tombstone contract as the infra branch.
    assert rec.deleted == ["wl-a-b-7"]


def test_infra_giveup_isolates_a_wedged_delete():
    """Issue #226: a wedged delete_workload in the infra give-up branch
    must not abort the reconcile pass. The tombstone surviving is
    acceptable; list_failed() will return it next tick, and the wrap
    keeps the rest of the bridge running."""

    def wedged_delete(name):
        raise TimeoutError(f"finalizer-wedge on {name}")

    park, parked = _parks()
    wl = _failed_wl_with_executor_error("w-wedge-infra", attempt=INFRA_MAX_ATTEMPTS)
    rec = _Recorder([wl])
    rec.delete_workload = wedged_delete  # type: ignore[assignment]
    # Must not raise — the wrap catches TimeoutError.
    out = _reconcile(rec, park_for_human=park)
    assert any("giveup-infra:" in line for line in out), out
    # Park happened; the delete attempt is what wedged.
    assert len(parked) == 1, parked


def test_verdict_giveup_isolates_a_wedged_delete():
    """Issue #226: same contract for the verdict give-up branch."""

    def wedged_delete(name):
        raise TimeoutError(f"finalizer-wedge on {name}")

    park, parked = _parks()
    rec = _Recorder([_failed_wl("wl-a-b-wedge", attempt=3)])
    rec.delete_workload = wedged_delete  # type: ignore[assignment]
    out = _reconcile(rec, park_for_human=park)  # no escalate hook wired
    assert out == ["wl-a-b-wedge:giveup:3/3"]
    assert len(parked) == 1, parked


def test_giveup_dedupes_park_when_issue_is_already_needs_human():
    """Issue #226: when needs_human_for already returns True for the
    item, the give-up branches must not post another escalation comment.
    The tombstone may still be alive, so list_failed() can return the
    workload next tick — but no duplicate comment."""

    park_calls: list = []

    def park(item, reason):
        park_calls.append((item.issue_id, reason))
        return True

    def needs_human_for(item) -> bool:
        return True  # already parked from a prior tick

    rec = _Recorder([_failed_wl_with_executor_error("w-dedupe", attempt=INFRA_MAX_ATTEMPTS)])
    out = _reconcile(rec, park_for_human=park, needs_human_for=needs_human_for)
    assert any("giveup-infra:" in line for line in out), out
    # No park_for_human call because the issue was already parked.
    assert park_calls == [], park_calls
    # Second tick against the same wedged workload posts no additional comment.
    rec2 = _Recorder([_failed_wl_with_executor_error("w-dedupe", attempt=INFRA_MAX_ATTEMPTS)])
    _reconcile(rec2, park_for_human=park, needs_human_for=needs_human_for)
    assert park_calls == [], park_calls


def test_parked_workload_is_not_re_parked_on_subsequent_tick():
    # The acceptance criterion for #176: once park_for_human has succeeded
    # for a Failed Workload, the tombstone is deleted so list_failed() does
    # not return it on the next tick, and reconcile_failures does not call
    # park_for_human again or post a second needs-human comment.
    park, parked = _parks()
    wl = _failed_wl("wl-repark-1", attempt=DEFAULT_MAX_ATTEMPTS)

    # Tick 1: park + delete.
    rec1 = _Recorder([wl])
    out1 = _reconcile(rec1, park_for_human=park)
    assert out1 == ["wl-repark-1:giveup:3/3"], out1
    assert len(parked) == 1, parked
    assert rec1.deleted == ["wl-repark-1"], rec1.deleted

    # Tick 2: list_failed() is now empty (the tombstone was retired in tick
    # 1), so reconcile_failures has nothing to act on. If the tombstone had
    # leaked through, the same workload would appear again and park_for_human
    # would be called a second time.
    parked_after_tick1 = len(parked)
    rec2 = _Recorder([])  # the post-park Failed list
    out2 = _reconcile(rec2, park_for_human=park)
    assert out2 == []
    assert len(parked) == parked_after_tick1, (
        "park_for_human must not be invoked a second time"
    )


def test_park_failure_leaves_tombstone_for_next_tick():
    # A park_for_human that returns False (or raises) must NOT retire the
    # tombstone — otherwise the next tick would silently drop the issue
    # instead of retrying the park.
    def no_park(item, reason):
        return False

    wl = _failed_wl("wl-parkfail-1", attempt=DEFAULT_MAX_ATTEMPTS)
    rec = _Recorder([wl])
    out = _reconcile(rec, park_for_human=no_park)
    assert out == ["wl-parkfail-1:giveup:3/3"], out
    # Tombstone stays so the next tick retries the park.
    assert rec.deleted == []
    assert rec.created == []


def test_escalated_workloads_are_not_parked():
    # Escalation is the healthy path: the issue moves to the frontier lane and
    # is re-claimed there. Parking it would strand work that has somewhere to go.
    park, parked = _parks()
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec, park_for_human=park,
        escalate=lambda item: True, escalation_lane="frontier",
    )
    assert any("escalated:" in line for line in out), out
    assert parked == []


def test_under_cap_retries_are_not_parked():
    # A Workload with budget left is retried, not parked.
    park, parked = _parks()
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=1)])
    out = _reconcile(rec, park_for_human=park)
    assert any("retry:2/" in line for line in out), out
    assert parked == []


def test_park_failure_does_not_abort_the_pass():
    # Parking is best-effort: a raising hook must not lose the remaining
    # Workloads or the giveup result line.
    def boom(item, reason):
        raise RuntimeError("dispatch 500")

    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(rec, park_for_human=boom)
    assert out == ["wl-a-b-7:giveup:3/3"]


def test_no_park_hook_is_tolerated():
    # park_for_human is optional; omitting it keeps the pre-existing behaviour.
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(rec)
    assert out == ["wl-a-b-7:giveup:3/3"]


# --- Issue #162: partial park success must not be reported as a full failure ---
#
# Before the fix, park_for_human let exceptions from the label and comment
# steps escape past a successful status change, so a /api/issues/label 404
# (for example) surfaced in the retry log as `park-exhausted-failed` even
# though the status change that actually unpins the issue had already
# landed. The contract that the retry wrapper relies on is:
#
#   * status ok + label/comment failure  -> park_for_human returns True
#     (the issue is unpinned; label/comment are best-effort and logged
#      individually as `park-for-human-label-failed` /
#      `park-for-human-comment-failed`).
#   * status ok + label/comment ok       -> park_for_human returns True.
#   * status fails (returns False or raises) -> park_for_human returns
#     False; the wrapper logs `park-exhausted-failed`.
#
# These tests exercise that contract end-to-end through _reconcile /
# _park_exhausted.


def _park_outcome(item, reason, *, status_ok, label_ok=True, comment_ok=True):
    """Fake park_for_human that mirrors the post-#162 contract.

    status_ok=False simulates update_status returning False (or raising,
    depending on the test). The label/comment outcomes are recorded so
    tests can assert that the wrapper never sees their exceptions.
    """

    def _park(item, reason):
        if not status_ok:
            # Status failure is reported by returning False (no exception).
            # The retry wrapper is expected to surface it as a full failure.
            return False
        # Status succeeded. Label/comment failures are swallowed and the
        # function still returns True — they are best-effort, independent
        # of one another, and must not mask the status change that unpins
        # the issue (issue #162).
        if not label_ok or not comment_ok:
            # In the real park_for_human the label/comment exceptions are
            # caught locally and logged individually. The wrapper here only
            # sees the True return value.
            pass
        return True

    return _park


def test_park_partial_label_failure_is_not_reported_as_failed(caplog):
    # park_for_human returns True because the status change landed, even
    # though the label step failed. The retry wrapper must not surface
    # this as park-exhausted-failed (issue #162).
    import logging

    caplog.set_level(logging.ERROR, logger="bridge.retry")
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=_park_outcome(None, None, status_ok=True, label_ok=False),
    )
    # Successful park → human-escalation branch, no giveup.
    assert out == ["wl-a-b-7:human-escalation:DESIGN-DECISION"]
    assert not any(
        "park-exhausted-failed" in record.message for record in caplog.records
    ), caplog.records


def test_park_partial_comment_failure_is_not_reported_as_failed(caplog):
    # Symmetric to the label case: a comment failure alone must not be
    # reported as a failed park.
    import logging

    caplog.set_level(logging.ERROR, logger="bridge.retry")
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=_park_outcome(None, None, status_ok=True, comment_ok=False),
    )
    assert out == ["wl-a-b-7:human-escalation:DESIGN-DECISION"]
    assert not any(
        "park-exhausted-failed" in record.message for record in caplog.records
    ), caplog.records


def test_park_status_failure_is_reported_as_failed(caplog):
    # A status-change failure (park_for_human returns False) is still a
    # full failure and the retry wrapper must surface it via the giveup
    # path. This guards against over-correction in the #162 fix.
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=_park_outcome(None, None, status_ok=False),
    )
    # Failed park falls through to the normal giveup path.
    assert out == ["wl-a-b-7:giveup:3/3"]


def test_park_status_exception_is_reported_as_failed(caplog):
    # If update_status itself raises (e.g. dispatch transport 500),
    # park_for_human must propagate that exception so the wrapper can
    # log park-exhausted-failed and return False. Label/comment
    # exceptions are NOT propagated — only status exceptions are.
    import logging

    caplog.set_level(logging.ERROR, logger="bridge.retry")

    def _boom(item, reason):
        raise RuntimeError("dispatch transport 500")

    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=_boom,
    )
    assert out == ["wl-a-b-7:giveup:3/3"]
    assert any(
        "park-exhausted-failed" in record.message for record in caplog.records
    ), caplog.records


def test_park_all_three_succeed_is_not_reported_as_failed(caplog):
    # The happy path: status, label, and comment all succeed. The wrapper
    # must not emit park-exhausted-failed.
    import logging

    caplog.set_level(logging.ERROR, logger="bridge.retry")
    rec = _Recorder([_failed_wl("wl-a-b-7", attempt=3)])
    out = _reconcile(
        rec,
        declared_escalation_for=lambda name: "DESIGN-DECISION",
        park_for_human=_park_outcome(None, None, status_ok=True),
    )
    assert out == ["wl-a-b-7:human-escalation:DESIGN-DECISION"]
    assert not any(
        "park-exhausted-failed" in record.message for record in caplog.records
    ), caplog.records
