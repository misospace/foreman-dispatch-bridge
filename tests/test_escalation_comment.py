"""Tests for the parked-for-human escalation comment format (issue #142)."""

import types

from bridge.main import NEEDS_HUMAN_LABEL, _format_escalation_comment


def _item(repo: str = "misospace/foreman-dispatch-bridge", number: int = 142):
    return types.SimpleNamespace(repo=repo, issue_number=number)


def test_format_includes_github_issue_url():
    body = _format_escalation_comment(_item(), "needs a product call")
    assert (
        "https://github.com/misospace/foreman-dispatch-bridge/issues/142"
        in body
    ), body


def test_format_includes_reason_verbatim():
    body = _format_escalation_comment(_item(), "DESIGN-DECISION: pick a queue shape")
    assert "DESIGN-DECISION: pick a queue shape" in body
    # header + reason, no mention
    assert "Needs a human decision" in body


def test_format_includes_branch_link_when_supplied():
    body = _format_escalation_comment(
        _item(), "DESIGN-DECISION", branch="foreman/wl-x-y/issue-142"
    )
    assert "foreman/wl-x-y/issue-142" in body


def test_format_omits_branch_line_when_not_supplied():
    body = _format_escalation_comment(_item(), "NO-TECHNICAL-FIX: upstream bug")
    assert "Workload/branch:" not in body


def test_format_does_not_use_at_foreman_mention():
    """The project name collides with a real GitHub handle; we must never
    ping @foreman from this template. See issue #142.
    """
    body = _format_escalation_comment(_item(), "needs eyes")
    assert "@foreman" not in body


def test_label_constant_is_needs_human():
    assert NEEDS_HUMAN_LABEL == "needs-human"