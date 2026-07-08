from datetime import datetime, timedelta, timezone

from bridge.prune import prunable_workloads, prune_workloads, terminal_since

NOW = datetime(2026, 7, 8, 20, 0, 0, tzinfo=timezone.utc)


def _wl(name, phase, *, last_transition=None, created=None):
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
    return {"metadata": md, "status": st}


def _ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_terminal_since_uses_latest_condition():
    wl = _wl("w", "Failed", last_transition="2026-07-08T19:41:13Z")
    assert terminal_since(wl) == datetime(2026, 7, 8, 19, 41, 13, tzinfo=timezone.utc)


def test_terminal_since_falls_back_to_creation_timestamp():
    wl = _wl("w", "Completed", created="2026-07-08T12:30:02Z")
    assert terminal_since(wl) == datetime(2026, 7, 8, 12, 30, 2, tzinfo=timezone.utc)


def test_terminal_since_none_when_no_timestamp():
    assert terminal_since({"metadata": {}, "status": {"phase": "Failed"}}) is None


def test_completed_pruned_past_ttl_but_kept_within():
    old = _wl("wl-old", "Completed", last_transition=_ago(7))
    fresh = _wl("wl-fresh", "Completed", last_transition=_ago(3))
    names = prunable_workloads([old, fresh], NOW, 6 * 3600, 48 * 3600)
    assert names == ["wl-old"]


def test_failed_uses_its_own_longer_ttl():
    # 7h-old Failed survives the 48h failed TTL even though it exceeds the 6h
    # completed TTL — the phases are independent.
    failed = _wl("wl-failed", "Failed", last_transition=_ago(7))
    assert prunable_workloads([failed], NOW, 6 * 3600, 48 * 3600) == []
    old_failed = _wl("wl-failed-old", "Failed", last_transition=_ago(50))
    assert prunable_workloads([old_failed], NOW, 6 * 3600, 48 * 3600) == ["wl-failed-old"]


def test_non_terminal_never_pruned():
    running = _wl("wl-running", "Dispatched", last_transition=_ago(100))
    assert prunable_workloads([running], NOW, 6 * 3600, 48 * 3600) == []


def test_ttl_zero_disables_phase():
    old = _wl("wl-old", "Completed", last_transition=_ago(100))
    assert prunable_workloads([old], NOW, 0, 48 * 3600) == []


def test_missing_timestamp_skipped():
    wl = {"metadata": {"name": "w"}, "status": {"phase": "Completed"}}
    assert prunable_workloads([wl], NOW, 6 * 3600, 48 * 3600) == []


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
        _wl("wl-old", "Completed", last_transition=_ago(7)),
        _wl("wl-fresh", "Completed", last_transition=_ago(1)),
    ])
    out = list(prune_workloads(r.list, r.delete, NOW, 6 * 3600, 48 * 3600))
    assert r.deleted == ["wl-old"]
    assert out == ["prune:deleted:wl-old"]


def test_prune_workloads_delete_failure_is_logged_not_raised():
    r = _Recorder([_wl("wl-old", "Completed", last_transition=_ago(7))], fail_on=["wl-old"])
    out = list(prune_workloads(r.list, r.delete, NOW, 6 * 3600, 48 * 3600))
    assert r.deleted == []
    assert out == ["prune:delete-failed:wl-old:boom"]


def test_prune_workloads_noop_when_both_ttls_disabled():
    r = _Recorder([_wl("wl-old", "Completed", last_transition=_ago(100))])
    out = list(prune_workloads(r.list, r.delete, NOW, 0, 0))
    assert out == []
    assert r.deleted == []
