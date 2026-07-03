from bridge.prfix import PrFixItem, parse_pr_fix_item


def test_parse_pr_fix_item_full():
    raw = {
        "repo": "misospace/miso-gallery", "pr": 295, "issue": 252,
        "branch": "foreman/wl-x/issue-252", "headSha": "abc123",
        "lane": "NORMAL", "type": "CI_FAILURE", "reason": "pytest failed",
        "feedback": ["tests/test_x.py::test_y failed", "AssertionError"],
    }
    item = parse_pr_fix_item(raw)
    assert item == PrFixItem(
        repo="misospace/miso-gallery", pr=295, issue=252,
        branch="foreman/wl-x/issue-252", head_sha="abc123",
        lane="NORMAL", type="CI_FAILURE", reason="pytest failed",
        feedback=["tests/test_x.py::test_y failed", "AssertionError"],
    )


def test_parse_pr_fix_item_missing_optionals():
    item = parse_pr_fix_item({"repo": "o/r", "pr": 7, "lane": "ESCALATED", "type": "OTHER", "reason": "x"})
    assert item.issue is None and item.branch is None and item.head_sha is None
    assert item.feedback == []


def test_parse_pr_fix_item_unusable_returns_none():
    assert parse_pr_fix_item({"pr": 7}) is None          # no repo
    assert parse_pr_fix_item({"repo": "o/r"}) is None     # no pr
    assert parse_pr_fix_item("not a dict") is None
