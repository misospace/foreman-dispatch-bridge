import re
from datetime import datetime, timedelta, timezone

import pytest


from bridge.prune import (
    prunable_workloads,
    prune_workloads,
    stamp_terminal_since,
    terminal_since_key,
    TERMINAL_SINCE_ANNOTATION_FALLBACK,
    terminal_since,
)

NOW = datetime(2026, 7, 8, 20, 0, 0, tzinfo=timezone.utc)


def _wl(name, phase, *, last_transition=None, created=None, terminal_since_stamp=None):
    md = {"name": name}
    if created:
        md["creationTimestamp"] = created
    st = {}
    if phase:
        st["phase"] = phase
    if last_transition is not None:
        st["conditions"] = [
            {"type": "Planned", "lastTransitionTime": "2026-07-01T00:00:00Z"},
            {"type": "Completed", "lastTransitionTime": last_transition},
        ]
    if terminal_since_stamp is not None:
        md.setdefault("annotations", {})[
            terminal_since_key(phase)
        ] = terminal_since_stamp
    return {"metadata": md, "status": st}


def _ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_terminal_since_uses_bridge_stamped_annotation():
    """The bridge-owned terminal-since annotation is the source of truth for
    age, because Foreman's controller rewrites condition lastTransitionTimes
    on every reconcile (issue #170)."""
    wl = _wl("w", "Failed", last_transition="2026-07-08T19:41:13Z",
             terminal_since_stamp="2026-07-08T13:00:00Z")
    assert terminal_since(wl) == datetime(2026, 7, 8, 13, 0, 0, tzinfo=timezone.utc)


def test_terminal_since_falls_back_to_creation_timestamp_when_no_stamp():
    """Workloads that went terminal before the bridge could stamp them fall
    back to creationTimestamp, which the controller also does not rewrite."""
    wl = _wl("w", "Completed", created="2026-07-08T12:30:02Z")
    assert terminal_since(wl) == datetime(2026, 7, 8, 12, 30, 2, tzinfo=timezone.utc)


def test_terminal_since_none_when_no_timestamp():
    assert terminal_since({"metadata": {}, "status": {"phase": "Failed"}}) is None


def test_terminal_since_ignores_refreshed_condition_timestamps():
    """Regression for #170: a Workload whose condition lastTransitionTime was
    refreshed minutes ago but which actually went terminal hours earlier (per
    the bridge-stamped annotation) must still report the older timestamp."""
    wl = _wl("w", "Completed",
             last_transition="2026-07-08T19:50:00Z",  # refreshed 10 min ago
             terminal_since_stamp="2026-07-08T01:00:00Z")  # actually terminal 19h ago
    assert terminal_since(wl) == datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc)


def test_stamp_terminal_since_first_call_writes_annotation():
    wl = _wl("w", "Completed")
    stamp = stamp_terminal_since(wl, now=NOW)
    assert stamp == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert wl["metadata"]["annotations"][
        terminal_since_key("Completed")
    ] == "2026-07-08T20:00:00Z"


def test_stamp_terminal_since_is_idempotent():
    """Re-stamping must not advance the timestamp, or prune TTL never elapses."""
    wl = _wl("w", "Failed")
    stamp_terminal_since(wl, now=NOW - timedelta(hours=10))
    stamp_terminal_since(wl, now=NOW)  # second tick
    assert wl["metadata"]["annotations"][
        terminal_since_key("Failed")
    ] == (NOW - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Regression for issue #227 ---
# A declared-escalation issue parked for a human (or one whose attempts are
# exhausted) is parked on purpose. prune_workloads must retire the Failed
# Workload tombstone without resetting the issue to "ready" -- otherwise the
# coder re-runs every TTL, burning an attempt budget we have already spent.


def _failure_wl(name, *, hours_old=49):
    """Failed Workload past the 48h prune TTL, attached to a real issue."""
    md = {"name": name, "creationTimestamp": _ago(hours_old + 24)}
    md["labels"] = {"created-by": "dispatch-bridge"}
    md["annotations"] = {
        terminal_since_key("Failed"): _ago(hours_old),
        "foreman.llmkube.dev/repo": "misospace/foreman-dispatch-bridge",
        "foreman.llmkube.dev/issueNumber": "227",
    }
    return {
        "metadata": md,
        "status": {"phase": "Failed"},
        "spec": {"repo": "misospace/foreman-dispatch-bridge", "issues": ["227"]},
    }


def test_prune_skips_reset_when_issue_parked_for_human():
    """Prune retires the tombstone but does NOT reset the parked issue.

    Regression for issue #227: declared-escalation Workloads that were parked
    in backlog for a human were being reset to ``ready`` by prune every TTL,
    so the coder re-ran the same issue forever. The is_parked_for_human
    callback is the durable signal that the issue carries the ``needs-human``
    label (set by retry.py on the declared-escalation and exhausted paths).
    """
    deleted = []
    resets = []

    def list_workloads():
        return [_failure_wl("w-parked")]

    def delete_workload(name):
        deleted.append(name)

    def reset_issue(wl):
        resets.append(wl["metadata"]["name"])

    def is_parked_for_human(wl):
        # Pretend the issue still carries the needs-human label -- the durable
        # marker that bridges Workload deletes.
        return wl.get("metadata", {}).get("name") == "w-parked"

    log = list(prune_workloads(
        list_workloads,
        delete_workload,
        now=NOW,
        failed_ttl_seconds=int(timedelta(hours=48).total_seconds()),
        reset_issue=reset_issue,
        is_parked_for_human=is_parked_for_human,
    ))

    assert deleted == ["w-parked"], log
    assert resets == [], "parked Failed must not be reset to ready"
    assert any(line == "prune:reset-issue-skipped-parked:w-parked" for line in log), log


def test_prune_resets_ordinary_failed_workload():
    """Sanity guard: the parked-skip does not regress ordinary Failed GC.

    A plain (not-parked) Failed Workload past the TTL must still be reset, so
    the coder can pick it up next tick. This is the behaviour #227 explicitly
    preserves.
    """
    deleted = []
    resets = []

    def list_workloads():
        return [_failure_wl("w-plain")]

    def delete_workload(name):
        deleted.append(name)

    def reset_issue(wl):
        resets.append(wl["metadata"]["name"])

    def is_parked_for_human(wl):
        # Nothing parked -- plain retryable failure.
        return False

    log = list(prune_workloads(
        list_workloads,
        delete_workload,
        now=NOW,
        failed_ttl_seconds=int(timedelta(hours=48).total_seconds()),
        reset_issue=reset_issue,
        is_parked_for_human=is_parked_for_human,
    ))

    assert deleted == ["w-plain"], log
    assert resets == ["w-plain"], log
    assert not any("skipped-parked" in line for line in log), log


def test_prune_without_is_parked_for_human_preserves_legacy_behaviour():
    """Backward-compat: callers that do not pass the new callback see the same
    reset behaviour as before, since they have not opted in to the parked
    detection. Wired-up callers (bridge/main.py) pass the callback."""
    deleted = []
    resets = []

    def list_workloads():
        return [_failure_wl("w-legacy")]

    def delete_workload(name):
        deleted.append(name)

    def reset_issue(wl):
        resets.append(wl["metadata"]["name"])

    log = list(prune_workloads(
        list_workloads,
        delete_workload,
        now=NOW,
        failed_ttl_seconds=int(timedelta(hours=48).total_seconds()),
        reset_issue=reset_issue,
    ))

    assert deleted == ["w-legacy"], log
    assert resets == ["w-legacy"], log


def test_stamp_terminal_since_noop_for_non_terminal_phase():
    wl = _wl("w", "Dispatched")
    assert stamp_terminal_since(wl, now=NOW) is None
    assert "annotations" not in wl["metadata"]


def test_completed_pruned_past_ttl_but_kept_within():
    old = _wl("wl-old", "Completed", terminal_since_stamp=_ago(7))
    fresh = _wl("wl-fresh", "Completed", terminal_since_stamp=_ago(3))
    results = prunable_workloads([old, fresh], NOW, 6 * 3600, 48 * 3600)
    assert results == [("wl-old", "Completed")]


def test_failed_uses_its_own_longer_ttl():
    # 7h-old Failed survives the 48h failed TTL even though it exceeds the 6h
    # completed TTL — the phases are independent.
    failed = _wl("wl-failed", "Failed", terminal_since_stamp=_ago(7))
    assert prunable_workloads([failed], NOW, 6 * 3600, 48 * 3600) == []
    old_failed = _wl("wl-failed-old", "Failed", terminal_since_stamp=_ago(50))
    assert prunable_workloads([old_failed], NOW, 6 * 3600, 48 * 3600) == [("wl-failed-old", "Failed")]


def test_non_terminal_never_pruned():
    running = _wl("wl-running", "Dispatched", terminal_since_stamp=_ago(100))
    assert prunable_workloads([running], NOW, 6 * 3600, 48 * 3600) == []


def test_ttl_zero_disables_phase():
    old = _wl("wl-old", "Completed", terminal_since_stamp=_ago(100))
    assert prunable_workloads([old], NOW, 0, 48 * 3600) == []


def test_missing_timestamp_skipped():
    wl = {"metadata": {"name": "w"}, "status": {"phase": "Completed"}}
    assert prunable_workloads([wl], NOW, 6 * 3600, 48 * 3600) == []


def test_prune_handles_refreshed_condition_timestamps():
    """Acceptance #1 / #4 for #170: a Workload whose condition timestamps
    have been refreshed since it went terminal must still be pruned when its
    bridge-stamped age exceeds the TTL."""
    wl = _wl("wl-stale", "Completed",
             last_transition=_ago(1),  # controller refreshed it an hour ago
             terminal_since_stamp=_ago(7))  # actually terminal 7 hours ago
    assert prunable_workloads([wl], NOW, 6 * 3600, 48 * 3600) == [("wl-stale", "Completed")]


class _Recorder:
    def __init__(self, workloads, fail_on=()):
        self.workloads = workloads
        self.deleted = []
        self.fail_on = set(fail_on)

    def list(self):
        return self.workloads

    def delete(self, name):
        if name in self.fail_on:
            raise RuntimeError("boom")
        self.deleted.append(name)


def test_prune_workloads_deletes_and_logs():
    r = _Recorder([
        _wl("wl-old", "Completed", terminal_since_stamp=_ago(7)),
        _wl("wl-fresh", "Completed", terminal_since_stamp=_ago(1)),
    ])
    out = list(prune_workloads(r.list, r.delete, NOW, 6 * 3600, 48 * 3600))
    assert r.deleted == ["wl-old"]
    assert out == ["prune:deleted:wl-old"]


def test_prune_workloads_delete_failure_is_logged_not_raised():
    r = _Recorder([_wl("wl-old", "Completed", terminal_since_stamp=_ago(7))], fail_on=["wl-old"])
    out = list(prune_workloads(r.list, r.delete, NOW, 6 * 3600, 48 * 3600))
    assert r.deleted == []
    assert out == ["prune:delete-failed:wl-old:boom"]


def test_prune_workloads_noop_when_both_ttls_disabled():
    r = _Recorder([_wl("wl-old", "Completed", terminal_since_stamp=_ago(100))])
    out = list(prune_workloads(r.list, r.delete, NOW, 0, 0))
    assert out == []
    assert r.deleted == []


# ── reset_issue tests (callback receives workload manifest) ─────────────────


def _wl_with_identity(name, phase, *, repo="a/b", issues=None, issue_id="iss_1",
                       last_transition=None, created=None,
                       created_by="dispatch-bridge", terminal_since_stamp=None):
    """Workload manifest with spec identity fields for prune reset."""
    base = _wl(name, phase, last_transition=last_transition, created=created,
               terminal_since_stamp=terminal_since_stamp)
    base["spec"] = {"repo": repo, "issues": issues or [42]}
    labels = base["metadata"].setdefault("labels", {})
    labels["created-by"] = created_by
    ann = base["metadata"].setdefault("annotations", {})
    ann["foreman.llmkube.dev/issue-id"] = issue_id
    return base


class _RecorderWithReset(_Recorder):
    def __init__(self, workloads, fail_on=()):
        super().__init__(workloads, fail_on=fail_on)
        self.reset_calls = []

    def reset(self, wl):
        self.reset_calls.append(wl)


def test_prune_resets_failed_workload_passes_manifest():
    """A pruned Failed Workload passes the full manifest to reset_issue."""
    wl = _wl_with_identity("wl-a-b-42", "Failed", terminal_since_stamp=_ago(50),
                           repo="a/b", issues=[42], issue_id="iss_42")
    r = _RecorderWithReset([wl])
    out = list(prune_workloads(
        r.list, r.delete, NOW, 6 * 3600, 48 * 3600,
        reset_issue=r.reset,
    ))
    assert r.deleted == ["wl-a-b-42"]
    assert len(r.reset_calls) == 1
    passed_wl = r.reset_calls[0]
    assert passed_wl["spec"]["repo"] == "a/b"
    assert passed_wl["spec"]["issues"] == [42]
    assert passed_wl["metadata"]["annotations"]["foreman.llmkube.dev/issue-id"] == "iss_42"
    assert "prune:deleted:wl-a-b-42" in out
    assert "prune:reset-issue:wl-a-b-42" in out


def test_prune_does_not_reset_completed_workload():
    """A pruned Completed Workload does NOT call reset_issue (PR already opened)."""
    wl = _wl_with_identity("wl-a-b-99", "Completed", terminal_since_stamp=_ago(7))
    r = _RecorderWithReset([wl])
    out = list(prune_workloads(
        r.list, r.delete, NOW, 6 * 3600, 48 * 3600,
        reset_issue=r.reset,
    ))
    assert r.deleted == ["wl-a-b-99"]
    assert r.reset_calls == []
    assert "prune:reset-issue" not in " ".join(out)


def test_prune_reset_failure_is_logged_not_raised():
    """A failed reset_issue call is logged but doesn't crash the tick."""
    wl = _wl_with_identity("wl-a-b-42", "Failed", terminal_since_stamp=_ago(50))

    def fail_reset(wl):
        raise RuntimeError("API down")

    out = list(prune_workloads(
        lambda: [wl], lambda name: None, NOW, 6 * 3600, 48 * 3600,
        reset_issue=fail_reset,
    ))
    assert any("prune:reset-issue-failed" in line for line in out)


def test_prune_no_reset_when_callback_not_provided():
    """Without reset_issue, prune behaves as before (no reset)."""
    r = _Recorder([
        _wl_with_identity("wl-a-b-42", "Failed", terminal_since_stamp=_ago(50)),
    ])
    out = list(prune_workloads(r.list, r.delete, NOW, 6 * 3600, 48 * 3600))
    assert r.deleted == ["wl-a-b-42"]
    assert "prune:reset-issue" not in " ".join(out)


def test_prune_reset_skips_workloads_without_identity():
    """Workloads missing spec.repo or spec.issues are not reset (can't derive identity)."""
    wl = _wl("wl-no-identity", "Failed", terminal_since_stamp=_ago(50))
    wl["spec"] = {}
    r = _RecorderWithReset([wl])
    out = list(prune_workloads(
        r.list, r.delete, NOW, 6 * 3600, 48 * 3600,
        reset_issue=r.reset,
    ))
    assert r.deleted == ["wl-no-identity"]
    assert r.reset_calls == []
    assert any("prune:reset-issue-skipped" in line for line in out)


def test_prune_reset_skips_prfix_workload():
    """PR-fix Workloads are deleted but NOT reset even with repo/issues identity."""
    wl = _wl_with_identity(
        "wl-prfix-42", "Failed", terminal_since_stamp=_ago(50),
        repo="a/b", issues=[42], created_by="dispatch-bridge-prfix",
    )
    r = _RecorderWithReset([wl])
    out = list(prune_workloads(
        r.list, r.delete, NOW, 6 * 3600, 48 * 3600,
        reset_issue=r.reset,
    ))
    assert r.deleted == ["wl-prfix-42"]
    assert r.reset_calls == []
    assert any("prune:reset-issue-skipped" in line for line in out)


# Kubernetes' own validation: optional DNS-subdomain prefix, one slash, then a
# name of alphanumerics/-/_/. starting and ending alphanumeric. The API server
# rejects anything else with 422, which is what a second slash produced.
_ANNOTATION_KEY = re.compile(
    r"^(?:[a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?"
    r"([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]$"
)


@pytest.mark.parametrize("phase", ["Completed", "Failed"])
def test_terminal_since_key_is_a_valid_annotation_key(phase):
    """A second slash made every stamp fail 422 Unprocessable Entity, silently:
    "…/terminal-since/Completed" is not a valid key."""
    key = terminal_since_key(phase)
    assert key.count("/") == 1, key
    assert _ANNOTATION_KEY.match(key), key


def test_fallback_annotation_key_is_also_valid():
    assert _ANNOTATION_KEY.match(TERMINAL_SINCE_ANNOTATION_FALLBACK)
