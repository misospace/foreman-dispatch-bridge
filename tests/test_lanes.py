"""Lane resolution by role (issue #292).

The behaviour these pin is "explicit config always wins, discovery only fills
gaps, and a lane id beats a role beats the wildcard". Each of those is a rule
someone will be tempted to simplify away later.
"""

import pytest

from bridge import lanes


TOPOLOGY = [
    {"id": "local", "title": "Local", "claimable": True, "role": "default"},
    {"id": "frontier", "title": "Frontier", "claimable": True, "role": "escalation"},
    {"id": "backlog", "title": "Backlog", "claimable": False},
]


@pytest.fixture(autouse=True)
def _clean():
    lanes.reset_topology()
    yield
    lanes.reset_topology()


# --- parsing -----------------------------------------------------------------


def test_reads_roles_and_claimable_lanes():
    lanes.set_topology(TOPOLOGY)
    assert lanes.role_of("local") == "default"
    assert lanes.role_of("frontier") == "escalation"
    assert lanes.role_of("backlog") is None
    assert lanes.claimable_lanes() == ["local", "frontier"]


def test_malformed_payloads_degrade_to_no_topology():
    # A partial or garbage response must not poison resolution: the caller's
    # explicit config has to remain the answer.
    for payload in (None, {}, "nope", 42, [None, 3, "x"], [{"no_id": 1}], [{"id": "  "}]):
        lanes.reset_topology()
        lanes.set_topology(payload)
        assert lanes.claimable_lanes() == []
        assert lanes.role_of("local") is None


def test_ignores_a_non_string_or_blank_role():
    lanes.set_topology([{"id": "a", "claimable": True, "role": 7}, {"id": "b", "claimable": True, "role": " "}])
    assert lanes.role_of("a") is None
    assert lanes.role_of("b") is None


def test_claimable_must_be_exactly_true():
    # Truthy-but-not-True values are a config smell, not a claimable lane.
    lanes.set_topology([{"id": "a", "claimable": "yes"}, {"id": "b", "claimable": 1}])
    assert lanes.claimable_lanes() == []


# --- role lookup -------------------------------------------------------------


def test_lane_for_role_ignores_non_claimable_lanes():
    # Only claimable lanes are resolution targets, so a parked lane carrying a
    # stale role is inert rather than a competing answer.
    lanes.set_topology(
        [
            {"id": "shelf", "title": "Shelf", "claimable": False, "role": "escalation"},
            {"id": "frontier", "title": "Frontier", "claimable": True, "role": "escalation"},
        ]
    )
    assert lanes.lane_for_role("escalation") == "frontier"


def test_lane_for_role_returns_none_when_unknown():
    lanes.set_topology(TOPOLOGY)
    assert lanes.lane_for_role("nonesuch") is None


# --- agent-map precedence ----------------------------------------------------


def test_lane_id_wins_over_role():
    # The rule that lets a deployment override one lane while others keep the
    # role-level default. Easy to "simplify" into role-first; this fails if so.
    lanes.set_topology(TOPOLOGY)
    mapping = {"frontier": "by-id", "escalation": "by-role", "*": "by-wildcard"}
    assert lanes.lookup(mapping, "frontier") == "by-id"


def test_role_wins_over_wildcard():
    lanes.set_topology(TOPOLOGY)
    mapping = {"escalation": "by-role", "*": "by-wildcard"}
    assert lanes.lookup(mapping, "frontier") == "by-role"


def test_wildcard_is_the_last_resort():
    lanes.set_topology(TOPOLOGY)
    assert lanes.lookup({"*": "by-wildcard"}, "frontier") == "by-wildcard"


def test_role_keys_do_not_resolve_without_a_topology():
    # Discovery unavailable: a role-keyed map cannot be resolved, so the caller
    # falls through to its own default rather than guessing.
    mapping = {"escalation": "by-role"}
    assert lanes.lookup(mapping, "frontier") is None


def test_lookup_handles_empty_and_missing_inputs():
    lanes.set_topology(TOPOLOGY)
    assert lanes.lookup(None, "frontier") is None
    assert lanes.lookup({}, "frontier") is None
    assert lanes.lookup({"*": "w"}, None) == "w"


# --- polled lanes ------------------------------------------------------------


def test_explicit_lanes_win_over_discovery():
    lanes.set_topology(TOPOLOGY)
    assert lanes.resolve_polled_lanes(["frontier"], ["fallback"]) == ["frontier"]


def test_discovers_claimable_lanes_when_unconfigured():
    lanes.set_topology(TOPOLOGY)
    assert lanes.resolve_polled_lanes([], ["fallback"]) == ["local", "frontier"]


def test_falls_back_when_neither_configured_nor_discoverable():
    assert lanes.resolve_polled_lanes([], ["local", "cloud", "frontier"]) == [
        "local",
        "cloud",
        "frontier",
    ]


# --- escalation lane ---------------------------------------------------------


def test_explicit_escalation_lane_wins():
    lanes.set_topology(TOPOLOGY)
    assert lanes.resolve_escalation_lane("somewhere-else") == "somewhere-else"


def test_escalation_lane_resolved_by_role():
    lanes.set_topology(TOPOLOGY)
    assert lanes.resolve_escalation_lane("") == "frontier"


def test_escalation_empty_when_no_role_and_no_config():
    # "" is what the caller already treats as escalation-disabled, so an
    # unreachable endpoint degrades to the pre-existing behaviour.
    lanes.set_topology([{"id": "only", "claimable": True, "role": "default"}])
    assert lanes.resolve_escalation_lane("") == ""
    lanes.reset_topology()
    assert lanes.resolve_escalation_lane("") == ""


# --- integration with the real lane-keyed call sites -------------------------


def test_pr_fix_coder_resolves_by_role():
    """PR_FIX_LANE_AGENTS was the third spelling of this idea (NORMAL/ESCALATED).
    Keying it by Dispatch's role names collapses it into one vocabulary."""
    from bridge.prfix import pr_fix_coder_for

    lanes.set_topology(TOPOLOGY)
    assert pr_fix_coder_for("frontier", {"escalation": "coder-frontier"}) == "coder-frontier"
    # id still wins
    assert (
        pr_fix_coder_for("frontier", {"frontier": "by-id", "escalation": "by-role"}) == "by-id"
    )


def test_revision_coder_resolves_by_role():
    from bridge.workload import revision_coder_agent_for

    lanes.set_topology(TOPOLOGY)
    assert revision_coder_agent_for("frontier", {"escalation": "coder-revision"}) == "coder-revision"
    assert revision_coder_agent_for("frontier", {}) == ""


def test_lane_coder_agents_resolve_by_role():
    from bridge.workload import coder_agent_for

    lanes.set_topology(TOPOLOGY)
    picked = coder_agent_for(
        lane="frontier",
        repo="o/r",
        issue_number=1,
        language="python",
        lane_coder_agents={"escalation": "coder-frontier"},
    )
    assert picked == "coder-frontier"
