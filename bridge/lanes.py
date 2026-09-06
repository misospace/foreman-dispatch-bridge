"""Lane topology discovered from Dispatch, so the bridge can resolve lanes by
ROLE instead of hardcoding one deployment's lane ids.

Every lane decision here used to key off a literal id belonging to a single
deployment, in three different spellings: `local,frontier` for the poll list,
`ESCALATION_LANE=frontier` (a setting whose name is a role and whose value is
an id), and `NORMAL`/`ESCALATED` in PR_FIX_LANE_AGENTS. Renaming a lane in
Dispatch silently broke the link, and a new adopter had to hand-sync lane names
across two systems.

Dispatch owns the vocabulary and now publishes it at `GET /api/lanes`
(misospace/dispatch#947), including each lane's role and whether it is
claimable, with roles validated unique across claimable lanes. This module
holds that answer for the tick and resolves lookups against it.

Module-level state rather than a parameter threaded through every signature,
mirroring Dispatch's own `setLaneConfig` / `resetLaneConfig`: the topology is
one fact for the whole tick, and the alternative is a new argument on a dozen
functions that all want the same value.

EXPLICIT CONFIG ALWAYS WINS. Discovery only fills in what was not configured,
and an unreachable endpoint leaves an empty topology rather than raising: a
CronJob tick must never fail because a new endpoint is down.
"""

import logging
from typing import Optional

logger = logging.getLogger("bridge.lanes")

# Key that matches any lane in the agent maps. Mirrors workload.LANE_CODER_WILDCARD;
# duplicated here to keep this module free of import cycles.
WILDCARD = "*"

ROLE_DEFAULT = "default"
ROLE_ESCALATION = "escalation"

# lane id -> role, for every lane Dispatch reported with one.
_roles: dict = {}
# lane ids Dispatch reported as claimable, in configured order.
_claimable: list = []


def set_topology(lanes) -> None:
    """Record the lane set from `GET /api/lanes`.

    Accepts the raw decoded payload. Anything that is not a list of objects
    carrying a string `id` is ignored, so a malformed or partial response
    degrades to "no topology known" rather than poisoning resolution.
    """
    global _roles, _claimable
    roles: dict = {}
    claimable: list = []
    if isinstance(lanes, list):
        for lane in lanes:
            if not isinstance(lane, dict):
                continue
            lane_id = lane.get("id")
            if not isinstance(lane_id, str) or not lane_id.strip():
                continue
            lane_id = lane_id.strip()
            role = lane.get("role")
            if isinstance(role, str) and role.strip():
                roles[lane_id] = role.strip()
            if lane.get("claimable") is True:
                claimable.append(lane_id)
    _roles = roles
    _claimable = claimable


def reset_topology() -> None:
    """Forget the discovered topology (used between tests and on failure)."""
    global _roles, _claimable
    _roles = {}
    _claimable = []


def role_of(lane: Optional[str]) -> Optional[str]:
    """The role Dispatch assigned to `lane`, or None when unknown."""
    if not lane:
        return None
    return _roles.get(lane)


def lane_for_role(role: str) -> Optional[str]:
    """The claimable lane holding `role`, or None.

    Dispatch validates roles unique across claimable lanes, so this is a
    well-defined question rather than a first-match guess. Non-claimable lanes
    are ignored even if they carry a stale role: only claimable lanes are
    resolution targets.
    """
    for lane_id in _claimable:
        if _roles.get(lane_id) == role:
            return lane_id
    return None


def claimable_lanes() -> list:
    """Every claimable lane Dispatch reported, in configured order."""
    return list(_claimable)


def lookup(mapping: Optional[dict], lane: Optional[str]):
    """Resolve a lane-keyed agent map, accepting a lane id OR a role name.

    Precedence, and the order matters:

      1. an exact lane id  ("frontier")
      2. the lane's role   ("escalation")
      3. the wildcard      ("*")

    Id before role so a deployment can still target one specific lane when it
    has several sharing a role, or override the role-level default for one of
    them. Role before wildcard so role-keyed config is not swallowed by a
    catch-all. Returns None when nothing matches, leaving the caller's own
    fallback in charge.
    """
    if not mapping:
        return None
    if lane and lane in mapping:
        return mapping[lane]
    role = role_of(lane)
    if role and role in mapping:
        return mapping[role]
    return mapping.get(WILDCARD)


def resolve_polled_lanes(configured: list, fallback: list) -> list:
    """Which lanes this tick should poll.

    Explicit configuration wins outright. Otherwise every claimable lane
    Dispatch reported, so a deployment that adds a lane does not also have to
    edit the bridge. With neither, the caller's historical default.
    """
    if configured:
        return list(configured)
    discovered = claimable_lanes()
    if discovered:
        logger.info("lanes:discovered:%s", ",".join(discovered))
        return discovered
    return list(fallback)


def resolve_escalation_lane(configured: str) -> str:
    """Which lane exhausted work escalates into.

    Explicit configuration wins. Otherwise the claimable lane whose role is
    "escalation" — the whole point of the role, and what ESCALATION_LANE was
    always trying to name. Empty string when neither is available, which the
    caller already treats as "escalation disabled".
    """
    if configured:
        return configured
    discovered = lane_for_role(ROLE_ESCALATION)
    if discovered:
        logger.info("lanes:escalation-by-role:%s", discovered)
        return discovered
    return ""
