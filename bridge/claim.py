import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from urllib.parse import urlencode
from bridge.models import ClaimedItem
from bridge.http_retry import _redact_token

logger = logging.getLogger("bridge.claim")

# Upper bound on the ThreadPoolExecutor size used by DispatchClient.queues()
# and DispatchClient.list_pr_fix_queued(). DISPATCH_LANES is a freely
# configurable comma list, so without a cap a misconfigured value (e.g. 200
# lanes) would spawn one thread — and one in-flight dispatch HTTP request —
# per lane on every tick, OOMKilling the CronJob pod and saturating
# dispatch's connection pool (#255).
MAX_LANE_WORKERS = 16

# Process-lifetime latch so the over-cap DISPATCH_LANES warning is emitted
# once per process instead of once per tick.
_lane_cap_warned = False


def warn_if_lane_cap_exceeded(lanes):
    """Log a single WARNING if ``lanes`` exceeds MAX_LANE_WORKERS.

    Called from ``bridge.main._real_main`` once per process. The warning is
    gated by the DISPATCH_LANES_WARN env var (default on) so a misconfigured
    deployment surfaces the issue without spamming the logs every tick.
    """
    global _lane_cap_warned
    if _lane_cap_warned:
        return
    if len(lanes) <= MAX_LANE_WORKERS:
        return
    if os.environ.get("DISPATCH_LANES_WARN", "1").strip().lower() in ("0", "false", "no"):
        return
    _lane_cap_warned = True
    logger.warning(
        "DISPATCH_LANES has %d lanes but the bridge caps concurrent lane "
        "fetches at %d workers; lanes beyond the cap are still watched, "
        "just with fewer in-flight requests per tick. Split into multiple "
        "bridge deployments if you need more concurrency. Set "
        "DISPATCH_LANES_WARN=0 to silence this warning.",
        len(lanes), MAX_LANE_WORKERS,
    )

# The only issue states issue_state will report. Anything else is reported as
# unknown (None), so a caller fails open instead of acting on a value it does not
# understand.
ISSUE_STATES = frozenset({"open", "closed"})

# Injected transports so the client is testable without network.
# http_get(url, headers) -> parsed JSON ; http_post(url, headers, json) -> parsed JSON | None
HttpGet = Callable[[str, dict], object]
HttpPost = Callable[[str, dict, dict], object]

# Bridge-side guard for Renovate bot issues, mirroring dispatch's
# isRenovateIssue (src/lib/issue-filters.ts). The queue already omits these by
# default (includeRenovate=false); this only covers the gap where a dispatch
# version or config serves them. It deliberately uses the same narrow criteria
# — dashboard substrings, update prefixes, bot labels — NOT a bare "renovate"
# substring, which used to drop every issue *about* Renovate (issue #216).
# Known trade-off, kept on purpose to stay consistent with dispatch: the
# `dependencies` label is a criterion here too, so a hand-written issue
# carrying that label is unclaimable. If that ever bites, change both sides.
_RENOVATE_TITLE_SUBSTRINGS = ("dependency dashboard", "renovate dashboard")
_RENOVATE_TITLE_PREFIXES = ("update dep", "update image")
_RENOVATE_LABELS = frozenset({"renovate", "dependencies", "automated"})


def _renovate_reason(item: dict) -> Optional[str]:
    """Return a skip reason if *item* matches dispatch's Renovate criteria, else None."""
    title = str(item.get("title") or "").lower()
    for substring in _RENOVATE_TITLE_SUBSTRINGS:
        if substring in title:
            return f"renovate-title-substring:{substring}"
    for prefix in _RENOVATE_TITLE_PREFIXES:
        if title.startswith(prefix):
            return f"renovate-title-prefix:{prefix}"
    for label in item.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.lower() in _RENOVATE_LABELS:
            return f"renovate-label:{name.lower()}"
    return None


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
    """Yield every claimable, ready, lane-matching, non-bot queue item, in queue
    (ranked) order. Items skipped by a bridge-side filter are logged with their
    issue number and reason (issue #216) — bot-filter skips at INFO, mechanical
    lane/status/claimable skips at DEBUG — so an empty lane is distinguishable
    from a filtered one without one INFO line per item per tick. Callers claim
    them in turn so one un-claimable head item can't hide the rest of the lane."""
    for item in items:
        if not isinstance(item, dict):
            continue
        number = _number(item)
        skip = _renovate_reason(item)
        # Bot-filter skips are the #216 failure mode (a lane silently starved
        # by a bridge-side filter), so they log at INFO. The mechanical skips
        # below are expected noise — dispatch filters lane/status server-side
        # before the queue reaches us — and log at DEBUG so an unfiltered queue
        # ever handed to select_candidates doesn't spam one INFO line per item.
        if skip is None and (_lane(item) or lane) != lane:
            skip = f"lane-mismatch:{_lane(item)}"
        if skip is None and _status(item) != "status/ready":
            skip = f"not-ready:{_status(item)}"
        if skip is None and item.get("claimable") is not True and item.get("agentMatch") is not True:
            skip = "not-claimable"
        if skip is not None:
            log = logger.info if skip.startswith("renovate-") else logger.debug
            log(
                "candidate-skipped",
                extra={"number": number, "reason": skip, "lane": lane},
            )
            continue
        yield item


def select_item(items: list, lane: str) -> Optional[dict]:
    """First claimable, ready, lane-matching, non-bot queue item (or None)."""
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

    # -- thin wrappers that redact the Bearer token from any exception before
    #    re-raising, so tracebacks / error strings never expose real tokens.

    def _http_get(self, url: str) -> object:
        try:
            return self._get(url, self._headers())
        except Exception as exc:
            exc.args = tuple(_redact_token(str(a)) for a in exc.args)
            raise

    def _http_post(self, url: str, payload: dict) -> Optional[object]:
        try:
            return self._post(url, self._headers(), payload)
        except Exception as exc:
            exc.args = tuple(_redact_token(str(a)) for a in exc.args)
            raise

    def queue(self, agent_name: str, lane: str) -> list:
        url = f"{self._base}/api/agents/{agent_name}/queue?lane={lane}&includeClaimed=true"
        data = self._http_get(url)
        return data if isinstance(data, list) else []

    def queues(self, agent_name: str, lanes: list) -> dict:
        """Fetch every lane queue in parallel and return {lane: [items]}.

        Each lane is fetched concurrently via ThreadPoolExecutor so the total
        wall-clock time is bounded by the slowest single lane rather than the
        sum of all lane latencies.  If a single lane fails its HTTP call, the
        error is logged and that lane's result is an empty list (the caller can
        distinguish this from a genuinely empty queue by checking the log).
        """
        if not lanes:
            return {}

        results = {}
        with ThreadPoolExecutor(max_workers=min(len(lanes), MAX_LANE_WORKERS)) as executor:
            futures = {executor.submit(self.queue, agent_name, lane): lane for lane in lanes}
            for future in as_completed(futures):
                lane = futures[future]
                try:
                    results[lane] = future.result()
                except Exception as exc:
                    logger.warning("lane-fetch-failed", extra={"lane": lane, "error": str(exc)})
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
        return self._http_post(f"{self._base}/api/issues/claim", payload) is not None

    def claim_one(
        self,
        agent_name: str,
        lane: str,
        queue_for: Optional[Callable[[str], list]] = None,
    ) -> Optional[ClaimedItem]:
        """Claim the first queue candidate that can be claimed, skipping any whose
        claim POST fails (e.g. 409 already-claimed). Returns None only when the
        queue has no candidate the agent can claim, so a single stuck head-of-queue
        item no longer starves the lane.

        ``queue_for(lane) -> list`` is an optional injectable override used by
        ``run_tick`` to read the lane from a single per-tick snapshot instead of
        issuing a fresh HTTP GET for every lane in every claim attempt (#256).
        """
        if queue_for is None:
            queue_for = lambda lane_name: self.queue(agent_name, lane_name)  # noqa: E731
        for item in select_candidates(queue_for(lane), lane):
            try:
                if self.claim(item, agent_name):
                    return to_claimed_item(item, lane)
            except Exception as e:
                logger.warning(
                    "claim-failed",
                    extra={"number": item.get("number"), "error": repr(e)},
                )
                continue
        return None

    def set_lane(self, item: ClaimedItem, lane: str, reason: str) -> bool:
        """Record an explicit lane classification for the issue (manual override)."""
        payload = {
            "model": "bridge-escalation",
            "classification": {"lane": lane, "confidence": "high", "reason": reason},
        }
        url = f"{self._base}/api/issues/{item.issue_id}/lane"
        return self._http_post(url, payload) is not None

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
            return self._http_post(f"{self._base}/api/issues/unclaim", payload) is not None
        except Exception as e:
            status = getattr(e, "response", None)
            if status and getattr(status, "status_code", None) == 400:
                return True  # already released / terminal — effectively unclaimed
            raise

    def find_issue_id(
        self,
        agent_name: str,
        lanes: list,
        repo: str,
        issue_number: int,
        queue_snapshot: Optional[dict] = None,
    ) -> str:
        """Recover a dispatch issue id by repo+number from the lane queues
        (includeClaimed=true, so claimed items are visible). Used to backfill
        Workloads whose issue-id annotation predates bridge 0.3.0.

        ``queue_snapshot`` is an optional pre-fetched {lane: [items]} map used
        by ``run_tick`` so the per-tick snapshot is reused instead of refetching
        every lane from dispatch (#256). When omitted, falls back to issuing a
        fresh parallel HTTP batch via ``self.queues``.
        """
        if queue_snapshot is None:
            queue_snapshot = self.queues(agent_name, lanes)
        # Preserve original lane order for deterministic first-match semantics.
        for lane in lanes:
            for item in queue_snapshot.get(lane, []):
                if not isinstance(item, dict):
                    continue
                if item.get("repoFullName") == repo and int(_number(item) or 0) == issue_number:
                    return str(item.get("issueId") or item.get("id") or "")
        return ""

    def lane_index(
        self,
        agent_name: str,
        lanes: list,
        queue_snapshot: Optional[dict] = None,
    ) -> dict:
        """Map (repo, issue number) -> the lane dispatch currently has it in.

        One pass over the same lane queues find_issue_id walks
        (includeClaimed=true, so in-flight issues are visible). Retries read a
        Workload's lane off its label, which froze at creation time; this is how
        they see a lane that changed underneath them (a manual de-escalation, or
        a groomer reclassification).

        ``queue_snapshot`` is an optional pre-fetched {lane: [items]} map used
        by ``run_tick`` so the per-tick snapshot is reused instead of refetching
        every lane from dispatch (#256). When omitted, falls back to issuing a
        fresh parallel HTTP batch via ``self.queues``.
        """
        if queue_snapshot is None:
            queue_snapshot = self.queues(agent_name, lanes)
        index: dict = {}
        # Reverse lane order so the first lane listed wins on a duplicate, matching
        # find_issue_id's first-match semantics.
        for lane in reversed(lanes):
            for item in queue_snapshot.get(lane, []):
                if not isinstance(item, dict):
                    continue
                repo = item.get("repoFullName")
                number = int(_number(item) or 0)
                if repo and number:
                    index[(str(repo), number)] = lane
        return index

    def escalate(self, item: ClaimedItem, lane: str, reason: str, agent_name: str) -> bool:
        """Move a given-up issue to the escalation lane and release the claim.

        Set the lane first, release the claim last: this keeps the operation
        atomic from the bridge's point of view. If ``set_lane`` fails the
        issue is still claimed in its original lane (so reconcile_failures
        will retry next tick). If ``unclaim`` fails after a successful lane
        move, the issue is in the escalation lane and still claimed — the
        next tick re-attempts the release rather than re-claiming into the
        original lane with a stale deterministic Workload name.
        """
        if not self.set_lane(item, lane, reason):
            return False
        return self.unclaim(item, agent_name)

    def list_pr_fix_queued(self, lanes: list) -> list:
        """List QUEUED PR-fix items across the given lanes (one GET per lane,
        concatenated in lane order). A non-list response for a lane contributes
        nothing; failures are logged."""
        if not lanes:
            return []

        def _fetch_lane(lane: str) -> list:
            url = f"{self._base}/api/pr-fix-queue/queued?lane={lane}"
            data = self._http_get(url)
            return data if isinstance(data, list) else []

        results = {}
        with ThreadPoolExecutor(max_workers=min(len(lanes), MAX_LANE_WORKERS)) as executor:
            futures = {executor.submit(_fetch_lane, lane): lane for lane in lanes}
            for future in as_completed(futures):
                lane = futures[future]
                try:
                    results[lane] = future.result()
                except Exception as exc:
                    logger.warning("pr-fix-lane-fetch-failed", extra={"lane": lane, "error": str(exc)})
                    results[lane] = []

        # Preserve lane order for deterministic concatenation.
        items = []
        for lane in lanes:
            items.extend(results.get(lane, []))
        return items

    def mark_pr_fix(self, repo: str, pr: int, status: str, note: str = "") -> bool:
        """Transition a PR-fix item's status (QUEUED/FIXED/BLOCKED/...)."""
        payload = {"repo": repo, "pr": pr, "status": status, "note": note}
        return self._http_post(f"{self._base}/api/pr-fix-queue/mark", payload) is not None

    def _issue_state_data(self, repo: str, number: int) -> Optional[dict]:
        """Return the unfiltered cached issue snapshot, or None if unknown."""
        query = urlencode({"repo": repo, "number": number})
        try:
            data = self._http_get(f"{self._base}/api/issues/state?{query}")
        except Exception as e:
            logger.warning(
                "issue-state-lookup-failed",
                extra={"repo": repo, "number": number, "error": repr(e)},
            )
            return None
        return data if isinstance(data, dict) else None

    def issue_state(self, repo: str, number: int) -> Optional[str]:
        """Return the cached state of repo#number ("open"/"closed"), or None if unknown.

        None covers every ambiguous outcome: a 404 (the issue is not in dispatch's
        cache), a transport error, or a malformed response. Callers MUST treat None
        as "no answer" and proceed as they would have without the check — never as
        "closed". Reading absence as closure would cancel legitimate work whenever
        the lookup merely failed, which is a worse outcome than the waste such a
        check is trying to avoid.

        /api/issues/state applies no filters beyond identity, unlike /api/issues
        (Renovate exclusion, excluded labels, open-only default), so a 404 here
        really does mean "not cached" rather than "filtered out".
        """
        data = self._issue_state_data(repo, number)
        if data is None:
            return None
        state = data.get("state")
        if not isinstance(state, str):
            return None
        state = state.strip().lower()
        # Normalise to the documented contract. An unrecognised value (a future
        # dispatch state, a typo, "merged") becomes None rather than being passed
        # through: None is the fail-open answer, whereas returning an unknown string
        # would silently read as "not closed" and quietly bypass the check the
        # caller added it for.
        return state if state in ISSUE_STATES else None

    def list_issues(self) -> list:
        """List open cached issues, including non-claimable backlog items."""
        data = self._http_get(f"{self._base}/api/issues")
        if not isinstance(data, list):
            return []
        result = []
        for issue in data:
            if not isinstance(issue, dict):
                continue
            item = dict(issue)
            repository = item.get("repository") or {}
            repo = item.get("repoFullName") or (
                repository.get("fullName") if isinstance(repository, dict) else None
            )
            if repo:
                item["repoFullName"] = repo
            issue_id = item.get("issueId") or item.get("id")
            if issue_id:
                item["issueId"] = issue_id
            result.append(item)
        return result

    def issue_is_parked(
        self, repo: str, number: int, marker: str
    ) -> Optional[bool]:
        """Return whether an issue is already parked for human triage.

        ``status/backlog`` is the durable resting state. The marker is checked
        too because an operator can restore the status while leaving the bridge's
        human-triage label in place. Unknown or malformed responses stay unknown
        so callers can fail open and attempt the full park.
        """
        data = self._issue_state_data(repo, number)
        if data is None:
            return None
        labels = data.get("labels")
        if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
            return None
        return marker in labels or "status/backlog" in labels

    def list_claimed(self, agent_name: str, status: str = "") -> list:
        """List issues currently claimed by *agent_name* (across all lanes).

        *status* selects which claimed status to list; the dispatch default is
        in-progress. Pass "ready" to find stuck claims: issues still holding this
        agent's label while back at status/ready, which no reaper could see while
        the endpoint only returned in-progress. Older dispatch versions ignore the
        parameter and return in-progress, which is a harmless no-op here."""
        url = f"{self._base}/api/issues/claimed?agentName={agent_name}"
        if status:
            url += f"&status={status}"
        data = self._http_get(url)
        return data if isinstance(data, list) else []

    def update_status(
        self, item: dict, status: str, agent_name: str, blocked_reason: str = ""
    ) -> bool:
        """Update the status label of an issue with full identity.

        *item* is a claimed-item dict (from ``list_claimed``) carrying at least
        ``issueId``, ``repoFullName``, and ``number``. *status* is the bare
        label value (e.g. ``"ready"``); a ``status/`` prefix is stripped
        defensively.
        """
        if status.startswith("status/"):
            status = status[len("status/"):]
        payload = {
            "issueId": item.get("issueId") or item.get("id") or "",
            "repoFullName": item.get("repoFullName"),
            "issueNumber": int(item.get("number") or item.get("issueNumber") or 0),
            "status": status,
            "agentName": agent_name,
        }
        # dispatch rejects status=blocked without a non-empty reason (400), so a
        # park that omits it silently fails and the issue keeps its in-progress
        # slot — the very thing parking exists to release (dispatch#862).
        if status == "blocked" and blocked_reason.strip():
            payload["blockedReason"] = blocked_reason.strip()[:500]
        return self._http_post(f"{self._base}/api/issues/status", payload) is not None

    def replace_labels(self, item: dict, remove: list[str], add: list[str]) -> bool:
        """Remove and add labels, returning false if any operation fails."""
        ok = True
        for label in remove:
            try:
                ok = self.remove_label(item, label) and ok
            except Exception:
                logger.exception("dispatch-remove-label-failed", extra={"label": label})
                ok = False
        for label in add:
            try:
                ok = self.add_label(item, label) and ok
            except Exception:
                logger.exception("dispatch-add-label-failed", extra={"label": label})
                ok = False
        return ok

    def add_label(self, item: dict, label: str) -> bool:
        """Add a label to an issue. Best-effort: failures do not raise."""
        payload = {
            "issueId": item.get("issueId") or item.get("id") or "",
            "repoFullName": item.get("repoFullName"),
            "issueNumber": int(item.get("number") or item.get("issueNumber") or 0),
            "label": label,
        }
        return self._http_post(f"{self._base}/api/issues/label", payload) is not None

    def remove_label(self, item: dict, label: str) -> bool:
        """Remove a label from an issue. Best-effort: failures do not raise.

        Wired by the groomer so a re-groomed issue clears its `needs-human`
        marker once work is ready again.
        """
        payload = {
            "issueId": item.get("issueId") or item.get("id") or "",
            "repoFullName": item.get("repoFullName"),
            "issueNumber": int(item.get("number") or item.get("issueNumber") or 0),
            "label": label,
        }
        return self._http_post(f"{self._base}/api/issues/unlabel", payload) is not None

    def post_comment(self, item: dict, body: str) -> bool:
        """Post a comment on an issue. Best-effort: failures do not raise."""
        payload = {
            "issueId": item.get("issueId") or item.get("id") or "",
            "repoFullName": item.get("repoFullName"),
            "issueNumber": int(item.get("number") or item.get("issueNumber") or 0),
            "body": body,
        }
        return self._http_post(f"{self._base}/api/issues/comment", payload) is not None
