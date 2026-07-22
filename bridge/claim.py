import re
from typing import Callable, Optional
from bridge.models import ClaimedItem

# Injected transports so the client is testable without network.
# http_get(url, headers) -> parsed JSON ; http_post(url, headers, json) -> parsed JSON | None
HttpGet = Callable[[str, dict], object]
HttpPost = Callable[[str, dict, dict], object]

_RENOVATE_RE = re.compile(r"renovate", re.IGNORECASE)


def _number(item: dict):
    return item.get("number") or item.get("issueNumber")


def _lane(item: dict):
    return item.get("lane") or item.get("currentLane")


def _status(item: dict) -> Optional[str]:
    for label in item.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.startswith("status/"):
            return name
    return item.get("status")


def select_candidates(items: list, lane: str):
    """Yield every claimable, ready, lane-matching, non-renovate queue item, in
    queue (ranked) order. Callers claim them in turn so one un-claimable head
    item can't hide the rest of the lane."""
    for item in items:
        if not isinstance(item, dict):
            continue
        if _RENOVATE_RE.search(str(item.get("title") or "")):
            continue
        if (_lane(item) or lane) != lane:
            continue
        if _status(item) != "status/ready":
            continue
        if item.get("claimable") is not True and item.get("agentMatch") is not True:
            continue
        yield item


def select_item(items: list, lane: str) -> Optional[dict]:
    """First claimable, ready, lane-matching, non-renovate queue item (or None)."""
    return next(select_candidates(items, lane), None)


def to_claimed_item(item: dict, lane: str) -> ClaimedItem:
    return ClaimedItem(
        repo=item["repoFullName"],
        issue_number=int(_number(item)),
        intent=str(item.get("title") or ""),
        lane=_lane(item) or lane,
        issue_id=str(item.get("issueId") or item.get("id") or ""),
    )


class DispatchClient:
    """Two-step dispatch claim: GET the lane queue, select an item, POST a claim."""

    def __init__(self, base_url: str, token: str, http_get: HttpGet, http_post: HttpPost):
        self._base = base_url.rstrip("/")
        self._token = token
        self._get = http_get
        self._post = http_post

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def queue(self, agent_name: str, lane: str) -> list:
        url = f"{self._base}/api/agents/{agent_name}/queue?lane={lane}&includeClaimed=true"
        data = self._get(url, self._headers())
        return data if isinstance(data, list) else []

    def claim(self, item: dict, agent_name: str) -> bool:
        payload = {
            "issueId": item.get("issueId") or item.get("id"),
            "repoFullName": item.get("repoFullName"),
            "issueNumber": int(_number(item)),
            "agentName": agent_name,
        }
        # http_post returns None on 409 (already claimed by someone else).
        return self._post(f"{self._base}/api/issues/claim", self._headers(), payload) is not None

    def claim_one(self, agent_name: str, lane: str) -> Optional[ClaimedItem]:
        """Claim the first queue candidate that can be claimed, skipping any whose
        claim POST fails (e.g. 409 already-claimed). Returns None only when the
        queue has no candidate the agent can claim, so a single stuck head-of-queue
        item no longer starves the lane."""
        for item in select_candidates(self.queue(agent_name, lane), lane):
            if self.claim(item, agent_name):
                return to_claimed_item(item, lane)
        return None

    def set_lane(self, item: ClaimedItem, lane: str, reason: str) -> bool:
        """Record an explicit lane classification for the issue (manual override)."""
        payload = {
            "model": "bridge-escalation",
            "classification": {"lane": lane, "confidence": "high", "reason": reason},
        }
        url = f"{self._base}/api/issues/{item.issue_id}/lane"
        return self._post(url, self._headers(), payload) is not None

    def unclaim(self, item: ClaimedItem, agent_name: str) -> bool:
        """Release the bridge's claim so the issue is claimable again.

        Treats 400 as success: the issue may already be unclaimed, closed, or in
        a terminal state — either way it won't be re-served to the original agent."""
        payload = {
            "issueId": item.issue_id,
            "repoFullName": item.repo,
            "issueNumber": item.issue_number,
            "agentName": agent_name,
        }
        try:
            return self._post(f"{self._base}/api/issues/unclaim", self._headers(), payload) is not None
        except Exception as e:
            status = getattr(e, "response", None)
            if status and getattr(status, "status_code", None) == 400:
                return True  # already released / terminal — effectively unclaimed
            raise

    def find_issue_id(self, agent_name: str, lanes: list, repo: str, issue_number: int) -> str:
        """Recover a dispatch issue id by repo+number from the lane queues
        (includeClaimed=true, so claimed items are visible). Used to backfill
        Workloads whose issue-id annotation predates bridge 0.3.0."""
        for lane in lanes:
            for item in self.queue(agent_name, lane):
                if not isinstance(item, dict):
                    continue
                if item.get("repoFullName") == repo and int(_number(item) or 0) == issue_number:
                    return str(item.get("issueId") or item.get("id") or "")
        return ""

    def escalate(self, item: ClaimedItem, lane: str, reason: str, agent_name: str) -> bool:
        """Move a given-up issue to the escalation lane and release the claim.

        Unclaim first, then lane: if set_lane fails the issue is at least
        released (so something else can pick it up). If unclaim fails the
        issue stays in its original lane — no partial escalation.
        """
        return self.unclaim(item, agent_name) and self.set_lane(item, lane, reason)

    def list_pr_fix_queued(self, lanes: list) -> list:
        """List QUEUED PR-fix items across the given lanes (one GET per lane,
        concatenated). A non-list response for a lane contributes nothing."""
        items = []
        for lane in lanes:
            url = f"{self._base}/api/pr-fix-queue/queued?lane={lane}"
            data = self._get(url, self._headers())
            if isinstance(data, list):
                items.extend(data)
        return items

    def mark_pr_fix(self, repo: str, pr: int, status: str, note: str = "") -> bool:
        """Transition a PR-fix item's status (QUEUED/FIXED/BLOCKED/...)."""
        payload = {"repo": repo, "pr": pr, "status": status, "note": note}
        return self._post(f"{self._base}/api/pr-fix-queue/mark", self._headers(), payload) is not None

    def list_in_progress(self, agent_name: str, lanes: list) -> list:
        """List issues that are `status/in-progress` AND claimed for this agent.

        Used by the strand-recovery reconcile pass to find issues whose
        Workload was deleted (GC or manual) without resetting the claim.
        Returns one entry per claimed in-progress issue with at least
        ``repoFullName``, ``issueNumber``, and ``issueId``.
        """
        out = []
        for lane in lanes:
            url = f"{self._base}/api/agents/{agent_name}/in-progress?lane={lane}"
            data = self._get(url, self._headers())
            if isinstance(data, list):
                out.extend(item for item in data if isinstance(item, dict))
        return out

    def reset_to_ready(self, item: dict) -> bool:
        """Reset a stranded `status/in-progress` claim back to `status/ready`.

        Best-effort: returns False (without raising) on a 4xx so the reconcile
        loop can move on to the next issue. 5xx still propagates so a true
        outage is visible in logs.
        """
        payload = {
            "issueId": item.get("issueId") or item.get("id"),
            "repoFullName": item.get("repoFullName") or item.get("repo"),
            "issueNumber": int(item.get("issueNumber") or item.get("number") or 0),
        }
        try:
            return self._post(f"{self._base}/api/issues/reset-ready", self._headers(), payload) is not None
        except Exception as e:
            status = getattr(e, "response", None)
            code = getattr(status, "status_code", None) if status else None
            if code is not None and 400 <= code < 500:
                return False
            raise
