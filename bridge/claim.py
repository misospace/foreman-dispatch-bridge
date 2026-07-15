import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional
from bridge.models import ClaimedItem

# Max worker threads for parallel lane queue fetches. Dispatch endpoints
# are independent so a small pool is enough to overlap their latency.
_LANE_POOL_MAX_WORKERS = 8

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

    def _fetch_one_queue(self, agent_name: str, lane: str) -> list:
        return self.queue(agent_name, lane)

    def queues(self, agent_name: str, lanes: Iterable[str]) -> dict:
        """Fetch the queue for each lane in ``lanes`` concurrently and return
        ``{lane: [items]}``. Independent GETs run in parallel so total wall
        time approaches the slowest single call instead of the sum of all of
        them. Order of returned keys matches the input lanes (each value is
        always a list, even on per-lane transport failure)."""
        lanes = list(lanes)
        if not lanes:
            return {}
        results: dict = {}
        if len(lanes) == 1:
            lane = lanes[0]
            results[lane] = self._fetch_one_queue(agent_name, lane)
            return results
        with ThreadPoolExecutor(max_workers=min(_LANE_POOL_MAX_WORKERS, len(lanes))) as pool:
            future_to_lane = {
                pool.submit(self._fetch_one_queue, agent_name, lane): lane
                for lane in lanes
            }
            for future, lane in future_to_lane.items():
                try:
                    results[lane] = future.result()
                except Exception:
                    # Mirror the single-lane contract: a failed GET yields no
                    # items, not an exception that aborts the whole batch.
                    results[lane] = []
        return results

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
        for lane, items in self.queues(agent_name, lanes).items():
            for item in items:
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

    def _fetch_pr_fix_queued(self, lane: str) -> list:
        url = f"{self._base}/api/pr-fix-queue/queued?lane={lane}"
        data = self._get(url, self._headers())
        return data if isinstance(data, list) else []

    def list_pr_fix_queued(self, lanes: list) -> list:
        """List QUEUED PR-fix items across the given lanes (one GET per lane,
        fetched concurrently and concatenated). A non-list response for a lane
        contributes nothing, and a per-lane failure is silently skipped so one
        lane being unavailable can't drop the whole PR-fix batch."""
        lanes = list(lanes)
        if not lanes:
            return []
        if len(lanes) == 1:
            return self._fetch_pr_fix_queued(lanes[0])
        items: list = []
        with ThreadPoolExecutor(max_workers=min(_LANE_POOL_MAX_WORKERS, len(lanes))) as pool:
            futures = [pool.submit(self._fetch_pr_fix_queued, lane) for lane in lanes]
            for future in futures:
                try:
                    items.extend(future.result() or [])
                except Exception:
                    continue
        return items

    def mark_pr_fix(self, repo: str, pr: int, status: str, note: str = "") -> bool:
        """Transition a PR-fix item's status (QUEUED/FIXED/BLOCKED/...)."""
        payload = {"repo": repo, "pr": pr, "status": status, "note": note}
        return self._post(f"{self._base}/api/pr-fix-queue/mark", self._headers(), payload) is not None
