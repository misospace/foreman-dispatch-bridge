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
                       agent_name="foreman-coder", last_transition=None, created=None,
                       created_by="dispatch-bridge", terminal_since_stamp=None):
    """Workload manifest with spec identity fields for prune reset."""
    base = _wl(name, phase, last_transition=last_transition, created=created,
               terminal_since_stamp=terminal_since_stamp)
    base["spec"] = {"repo": repo, "issues": issues or [42]}
    labels = base["metadata"].setdefault("labels", {})
    labels["created-by"] = created_by
    ann = base["metadata"].setdefault("annotations", {})
    ann["foreman.llmkube.dev/issue-id"] = issue_id
    ann["foreman.llmkube.dev/agent-name"] = agent_name
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
