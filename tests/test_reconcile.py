"""Tests for bridge.reconcile — stranded in-progress issue recovery."""

import pytest

from bridge.reconcile import reconcile_stranded_issues


class FakeDispatchClient:
    """Minimal DispatchClient stand-in for tests."""

    def __init__(self):
        self._claimed = []
        self._status_updates = []
        self._label_changes = []
        self._comments = []

    def list_claimed(self, agent_name):
        return self._claimed

    def update_status(self, item, status, agent_name):
        self._status_updates.append((item, status, agent_name))

    def add_label(self, item, label):
        self._label_changes.append(("add", item, label))
        return True

    def remove_label(self, item, label):
        self._label_changes.append(("remove", item, label))
        return True

    def post_comment(self, item, body):
        self._comments.append((item, body))
        return True


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def dispatch():
    return FakeDispatchClient()


def _claimed(issue_id="iss_1", number=42, repo="a/b", has_open_pr=False, lane="local"):
    return {
        "issueId": issue_id,
        "number": number,
        "repoFullName": repo,
        "currentLane": lane,
        "labels": ["status/in-progress"],
        "hasOpenPr": has_open_pr,
    }


# ── Tests ───────────────────────────────────────────────────────────────────

class TestReconcileStrandedIssues:
    """reconcile_stranded_issues resets in-progress issues with no Workload."""

    def test_resets_issue_with_no_workload_and_no_open_pr(self, dispatch):
        """An in-progress issue with no backing Workload and no open PR is
        reset to ready via update_status with full identity."""
        dispatch._claimed = [_claimed(number=42)]
        workload_names = {"wl-other-99"}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert len(results) == 1
        assert "42" in results[0]
        assert "ready" in results[0]
        item, status, agent = dispatch._status_updates[0]
        assert item["issueId"] == "iss_1"
        assert item["repoFullName"] == "a/b"
        assert item["number"] == 42
        assert status == "ready"
        assert agent == "test-agent"

    def test_skips_issue_with_live_workload_real_naming(self, dispatch):
        """An in-progress issue whose Workload still exists (by wl-<owner>-<repo>-<n>
        naming) is left alone."""
        dispatch._claimed = [_claimed(number=42, repo="a/b")]
        workload_names = {"wl-a-b-42"}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []
        assert dispatch._status_updates == []

    def test_skips_issue_with_has_open_pr_true(self, dispatch):
        """An in-progress issue with hasOpenPr=True is left alone (human-side review)."""
        dispatch._claimed = [_claimed(number=42, has_open_pr=True)]
        workload_names = set()

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []
        assert dispatch._status_updates == []

    def test_no_claimed_issues(self, dispatch):
        """When there are no claimed issues, nothing happens."""
        dispatch._claimed = []
        workload_names = set()

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []

    def test_multiple_issues_mixed(self, dispatch):
        """Only issues with no Workload and hasOpenPr=False are reset."""
        dispatch._claimed = [
            _claimed(issue_id="i1", number=1, has_open_pr=False),
            _claimed(issue_id="i2", number=2, has_open_pr=False),
            _claimed(issue_id="i3", number=3, has_open_pr=True),
            _claimed(issue_id="i4", number=4, has_open_pr=False),
        ]
        workload_names = {"wl-a-b-2"}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert len(results) == 2
        assert any("1" in r for r in results)
        assert any("4" in r for r in results)
        items = [u[0] for u in dispatch._status_updates]
        assert items[0]["number"] == 1
        assert items[1]["number"] == 4

    def test_update_status_failure_is_caught(self, dispatch):
        """A failed update_status call is logged but doesn't crash the tick."""
        dispatch._claimed = [_claimed(number=42)]
        workload_names = set()

        def fail_update(*a, **kw):
            raise RuntimeError("API down")

        dispatch.update_status = fail_update

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []

    def test_missing_issue_number_skipped(self, dispatch):
        """A claimed item with no 'number' key is skipped."""
        dispatch._claimed = [{"issueId": "x", "title": "no number", "hasOpenPr": False}]
        workload_names = set()

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []

    def test_non_dict_item_is_skipped(self, dispatch):
        """A non-dict element in the claimed list is logged and skipped, not
        crashing the tick — matching select_candidates' isinstance guard."""
        dispatch._claimed = [42, "string", None, _claimed(number=42)]
        workload_names = set()

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert len(results) == 1
        assert "42" in results[0]

    def test_non_integer_number_is_skipped(self, dispatch):
        """A claimed item whose 'number' is not an integer is logged and skipped,
        not crashing the tick with a ValueError."""
        dispatch._claimed = [_claimed(number="abc")]
        workload_names = set()

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []

    def test_workload_naming_uses_repo_and_number(self, dispatch):
        """Workload name is derived as wl-<owner-lower>-<repo-lower>-<number>
        matching bridge.workload.workload_name."""
        dispatch._claimed = [_claimed(number=7, repo="Owner/Repo")]
        workload_names = {"wl-owner-repo-7"}

        results = reconcile_stranded_issues(dispatch, "test-agent", workload_names)

        assert results == []
        assert dispatch._status_updates == []

    def test_signature_has_no_check_open_pr(self):
        """reconcile_stranded_issues takes (dispatch, agent_name, workload_names)."""
        import inspect
        sig = inspect.signature(reconcile_stranded_issues)
        params = list(sig.parameters.keys())
        assert params == ["dispatch", "agent_name", "workload_names"]


# --- stuck claims: status/ready while still holding this agent's label.
# Served at the head of the queue, refused on every claim (a conflict with
# itself), and skipped by claim_one so the lane never starves — permanently
# unreachable while throughput looks healthy. Two p0s sat 20 days that way.

class _StuckDispatch:
    """Fake dispatch for the stuck-claim reaper.

    *always* mimics a dispatch without the `status` parameter: it ignores the
    filter and returns the items regardless, which is what an older version does.
    """

    def __init__(self, ready_items, always=False):
        self._ready = ready_items
        self._always = always
        self.unclaimed = []

    def list_claimed(self, agent_name, status=""):
        if self._always:
            return self._ready
        return self._ready if status == "ready" else []

    def unclaim(self, item, agent_name):
        self.unclaimed.append(item.issue_number)
        return True


def _ready_item(number, repo="misospace/llmkube-images", has_pr=False, labels=None):
    return {
        "number": number,
        "repoFullName": repo,
        "issueId": f"id-{number}",
        "hasOpenPr": has_pr,
        "labels": ["status/ready", "agent/foreman-coder"] if labels is None else labels,
    }


def test_release_stuck_claims_releases_ready_claim_without_workload():
    from bridge.reconcile import release_stuck_claims
    d = _StuckDispatch([_ready_item(34)])
    out = release_stuck_claims(d, "foreman-coder", set())
    assert d.unclaimed == [34]
    assert out and "released stuck claim" in out[0]


def test_release_stuck_claims_skips_issue_with_live_workload():
    from bridge.reconcile import release_stuck_claims
    d = _StuckDispatch([_ready_item(34)])
    release_stuck_claims(d, "foreman-coder", {"wl-misospace-llmkube-images-34"})
    assert d.unclaimed == []


def test_release_stuck_claims_skips_issue_with_open_pr():
    from bridge.reconcile import release_stuck_claims
    d = _StuckDispatch([_ready_item(34, has_pr=True)])
    release_stuck_claims(d, "foreman-coder", set())
    assert d.unclaimed == []


def test_release_stuck_claims_noop_when_nothing_ready():
    from bridge.reconcile import release_stuck_claims
    d = _StuckDispatch([])
    assert release_stuck_claims(d, "foreman-coder", set()) == []
    assert d.unclaimed == []


def test_release_stuck_claims_ignores_items_that_are_not_ready():
    """Defensive: a dispatch without the `status` parameter ignores it and returns
    in-progress issues. Those belong to the stranded reconcile; unclaiming them
    here would be an unintended change. The label check makes this version-safe."""
    from bridge.reconcile import release_stuck_claims

    d = _StuckDispatch(
        [_ready_item(42, labels=["status/in-progress", "agent/foreman-coder"])],
        always=True,
    )
    assert release_stuck_claims(d, "foreman-coder", set()) == []
    assert d.unclaimed == []


@pytest.mark.parametrize("labels", [None, []], ids=["absent", "empty"])
def test_release_stuck_claims_warns_when_labels_are_unusable(caplog, labels):
    """Absent or empty labels both mean the status cannot be confirmed. Skipping
    silently would disable the reaper with no signal — the same shape that hid two
    p0s. An item from this endpoint always carries at least status + agent."""
    from bridge.reconcile import release_stuck_claims

    item = _ready_item(42)
    if labels is None:
        del item["labels"]
    else:
        item["labels"] = labels
    d = _StuckDispatch([item])

    with caplog.at_level("WARNING"):
        assert release_stuck_claims(d, "foreman-coder", set()) == []
    assert d.unclaimed == []
    assert "no usable labels" in caplog.text
