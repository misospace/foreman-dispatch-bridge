import pytest
from bridge.models import ClaimedItem
from bridge.retry import attempt_of, item_from_workload, reconcile_failures, refresh_lane


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
