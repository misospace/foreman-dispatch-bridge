# Fix-Queue Actuator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bridge pass that drains dispatch's PR-fix queue by spawning a foreman fix Workload per queued item, so PRs that fail CI or get change-requested are re-worked by the pipeline.

**Architecture:** A new pure-logic module `bridge/prfix.py` (DI style, mirroring `bridge/retry.py`) plus two `DispatchClient` methods and thin wiring in `bridge/main.py`. Two sub-phases per tick — reconcile (settle prior fix Workloads, mark items terminal) then drain (create fix Workloads for newly-QUEUED items) — mirroring `reconcile_failures` → `run_once`.

**Tech Stack:** Python 3, `pytest`, dependency-injected callables (no live cluster/dispatch in tests). Follows the existing `bridge/` module conventions exactly.

## Global Constraints

- Pure logic lives in `bridge/prfix.py`; all I/O (queue list, workload list/create/delete, mark) is injected as callables — unit tests never touch a cluster or dispatch. (Matches `bridge/retry.py`.)
- `DispatchClient` methods use `self._get(url, headers)` and `self._post(url, headers, payload)`; `_post` returns `None` on HTTP 409 and the parsed JSON otherwise.
- Fix Workload name: `prfix-<owner>-<repo>-<pr>`, lowercased, `/` → `-` (distinct from issue Workloads' `wl-...`).
- Actionable lanes are exactly `("NORMAL", "ESCALATED")`. `NEEDS_HUMAN` is never actioned (dispatch enqueues those already `BLOCKED`, so they never appear in `queued`).
- Fix pipeline is **code → verify only** — no reviewer step, no `openPullRequest`.
- Bounding: default 3 attempts, then mark `BLOCKED`. The bridge never re-lanes (no API).
- The whole pass is gated by `PR_FIX_ENABLED` (default off) — ships dark.
- No inline code comments narrating a change; keep module docstrings/comments in the existing style but do not add change-narration.
- No new Kubernetes RBAC (fix Workloads are `workloads`, already covered).

## File Structure

- `bridge/prfix.py` (**create**) — `PrFixItem` dataclass; `parse_pr_fix_item`, `pr_fix_coder_for`, `assemble_fix_prompt`, `prfix_workload_name`, `build_fix_workload`, `rebuild_prfix_manifest`, `reconcile_pr_fixes`, `drain_pr_fixes`; constants.
- `bridge/claim.py` (**modify**) — add `DispatchClient.list_pr_fix_queued` and `DispatchClient.mark_pr_fix`.
- `bridge/main.py` (**modify**) — parse new env, build in-cluster/HTTP closures, invoke the pass after `run_once`.
- `tests/test_prfix.py` (**create**) — unit tests for all pure logic.
- `home-ops` bridge HelmRelease (**modify, separate repo**) — add `PR_FIX_ENABLED`, `PR_FIX_MAX_ATTEMPTS`, `PR_FIX_LANE_AGENTS` env. (Documented in Task 7; applied when 0.8.28 is deployed.)

---

### Task 1: PrFixItem model + parsing

**Files:**
- Create: `bridge/prfix.py`
- Test: `tests/test_prfix.py`

**Interfaces:**
- Consumes: nothing (foundation).
- Produces: `PrFixItem` frozen dataclass; `parse_pr_fix_item(raw: dict) -> Optional[PrFixItem]` (returns `None` when `repo` or `pr` is missing/unusable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prfix.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: FAIL with `ImportError: cannot import name 'PrFixItem' from 'bridge.prfix'` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/prfix.py
from dataclasses import dataclass
from typing import Optional

# Lane values dispatch assigns to a PR-fix item. NEEDS_HUMAN is never actioned
# (dispatch enqueues those already BLOCKED, so they never reach `queued`).
ACTIONABLE_LANES = ("NORMAL", "ESCALATED")


@dataclass(frozen=True)
class PrFixItem:
    repo: str
    pr: int
    issue: Optional[int]
    branch: Optional[str]
    head_sha: Optional[str]
    lane: str
    type: str
    reason: str
    feedback: list  # list[str]


def parse_pr_fix_item(raw) -> Optional[PrFixItem]:
    """Parse one /api/pr-fix-queue/queued element into a PrFixItem.

    Returns None when the element is not a dict or lacks the repo/pr keys
    that make it addressable (an unusable item the actuator must skip)."""
    if not isinstance(raw, dict):
        return None
    repo = raw.get("repo")
    pr = raw.get("pr")
    if not repo or not isinstance(pr, int):
        return None
    feedback = raw.get("feedback")
    return PrFixItem(
        repo=str(repo),
        pr=pr,
        issue=raw.get("issue") if isinstance(raw.get("issue"), int) else None,
        branch=raw.get("branch") or None,
        head_sha=raw.get("headSha") or None,
        lane=str(raw.get("lane") or ""),
        type=str(raw.get("type") or "OTHER"),
        reason=str(raw.get("reason") or ""),
        feedback=[str(f) for f in feedback] if isinstance(feedback, list) else [],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bridge/prfix.py tests/test_prfix.py
git commit -s -m "feat: PrFixItem model + parse_pr_fix_item"
```

---

### Task 2: Prompt assembly + lane→coder resolution

**Files:**
- Modify: `bridge/prfix.py`
- Test: `tests/test_prfix.py`

**Interfaces:**
- Consumes: `PrFixItem` (Task 1).
- Produces: `assemble_fix_prompt(item: PrFixItem) -> str`; `pr_fix_coder_for(lane: str, lane_agents: dict) -> str` (exact → `"*"` → `"coder"`); constant `DEFAULT_PRFIX_LANE_AGENTS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prfix.py (append)
from bridge.prfix import assemble_fix_prompt, pr_fix_coder_for, DEFAULT_PRFIX_LANE_AGENTS


def _item(**kw):
    base = dict(repo="o/r", pr=1, issue=None, branch="b", head_sha=None,
               lane="NORMAL", type="OTHER", reason="", feedback=[])
    base.update(kw)
    return PrFixItem(**base)


def test_assemble_fix_prompt_ci_failure():
    p = assemble_fix_prompt(_item(type="CI_FAILURE", reason="pytest failed",
                                  feedback=["test_a failed", "test_b failed"]))
    assert p.startswith("CI failure:")
    assert "pytest failed" in p
    assert "- test_a failed" in p and "- test_b failed" in p


def test_assemble_fix_prompt_review_and_other_headers():
    assert assemble_fix_prompt(_item(type="REVIEW_FEEDBACK", reason="r")).startswith("Review feedback:")
    assert assemble_fix_prompt(_item(type="MERGE_CONFLICT", reason="r")).startswith("Merge conflict:")
    # OTHER has no header prefix, just the reason.
    assert assemble_fix_prompt(_item(type="OTHER", reason="just this")).strip() == "just this"


def test_pr_fix_coder_for_precedence():
    agents = {"NORMAL": "coder", "ESCALATED": "coder-frontier"}
    assert pr_fix_coder_for("ESCALATED", agents) == "coder-frontier"
    assert pr_fix_coder_for("NORMAL", agents) == "coder"
    assert pr_fix_coder_for("NORMAL", {"*": "c2"}) == "c2"        # wildcard
    assert pr_fix_coder_for("NORMAL", {}) == "coder"             # fallback
    assert DEFAULT_PRFIX_LANE_AGENTS == {"NORMAL": "coder", "ESCALATED": "coder-frontier"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: FAIL with `ImportError: cannot import name 'assemble_fix_prompt'`.

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/prfix.py (append)
from bridge.workload import CODER_AGENT  # "coder"

PRFIX_CODER_WILDCARD = "*"
DEFAULT_PRFIX_LANE_AGENTS = {"NORMAL": "coder", "ESCALATED": "coder-frontier"}

_TYPE_HEADERS = {
    "CI_FAILURE": "CI failure:",
    "REVIEW_FEEDBACK": "Review feedback:",
    "MERGE_CONFLICT": "Merge conflict:",
}


def pr_fix_coder_for(lane: str, lane_agents: dict) -> str:
    """Resolve a PrFixLane to a coder Agent name: exact, then "*", else "coder"."""
    if not lane_agents:
        return CODER_AGENT
    return lane_agents.get(lane) or lane_agents.get(PRFIX_CODER_WILDCARD) or CODER_AGENT


def assemble_fix_prompt(item: "PrFixItem") -> str:
    """Build the code step's payload.prompt from the item: a type header,
    the reason, then each feedback line as a bullet."""
    lines = []
    header = _TYPE_HEADERS.get(item.type)
    if header:
        lines.append(header)
    if item.reason:
        lines.append(item.reason)
    for fb in item.feedback:
        lines.append(f"- {fb}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bridge/prfix.py tests/test_prfix.py
git commit -s -m "feat: fix-prompt assembly + lane->coder resolution"
```

---

### Task 3: Fix Workload builder

**Files:**
- Modify: `bridge/prfix.py`
- Test: `tests/test_prfix.py`

**Interfaces:**
- Consumes: `PrFixItem` (Task 1), `pr_fix_coder_for`/`assemble_fix_prompt` (Task 2), `gate_profile_for` from `bridge.workload`.
- Produces: `prfix_workload_name(item) -> str`; `build_fix_workload(item, namespace, gate_profile, agent_name, coder_agent, attempt=1) -> dict`; annotation constants `PRFIX_REPO_ANNOTATION`, `PRFIX_PR_ANNOTATION`, label `PRFIX_CREATED_BY`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prfix.py (append)
from bridge.prfix import (
    prfix_workload_name, build_fix_workload,
    PRFIX_REPO_ANNOTATION, PRFIX_PR_ANNOTATION, PRFIX_CREATED_BY,
)


def test_prfix_workload_name_deterministic_sanitized():
    assert prfix_workload_name(_item(repo="misospace/miso-gallery", pr=295)) == "prfix-misospace-miso-gallery-295"


def test_build_fix_workload_code_verify_only_no_review():
    item = _item(repo="o/r", pr=9, issue=42, branch="foreman/wl-x/issue-42",
                 type="REVIEW_FEEDBACK", reason="address comments", feedback=["use Rel not prefix"])
    wl = build_fix_workload(item, namespace="llm", gate_profile={"language": "python"},
                            agent_name="foreman-coder", coder_agent="coder", attempt=1)
    assert wl["metadata"]["name"] == "prfix-o-r-9"
    assert wl["metadata"]["namespace"] == "llm"
    assert wl["metadata"]["labels"]["created-by"] == PRFIX_CREATED_BY
    assert wl["metadata"]["labels"]["lane"] == "NORMAL"
    assert wl["metadata"]["annotations"][PRFIX_REPO_ANNOTATION] == "o/r"
    assert wl["metadata"]["annotations"][PRFIX_PR_ANNOTATION] == "9"
    assert wl["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "1"
    steps = wl["spec"]["pipeline"]
    kinds = [s["kind"] for s in steps]
    assert kinds == ["issue-fix", "verify"]                     # code + verify only, NO review
    code = steps[0]
    assert code["agentRef"] == {"name": "coder"}
    assert code["payload"]["branch"] == "foreman/wl-x/issue-42"
    assert code["payload"]["reviseFromBranch"] == "foreman/wl-x/issue-42"
    assert code["payload"]["allowOverwrite"] is True
    assert code["payload"]["issue"] == 42
    assert "address comments" in code["payload"]["prompt"]
    assert wl["spec"]["gateProfile"] == {"language": "python"}
    assert "openPullRequest" not in code["payload"]


def test_build_fix_workload_omits_issue_when_absent():
    wl = build_fix_workload(_item(repo="o/r", pr=9, issue=None, branch="b"),
                            "llm", None, "a", "coder")
    assert "issue" not in wl["spec"]["pipeline"][0]["payload"]
    assert "gateProfile" not in wl["spec"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: FAIL with `ImportError: cannot import name 'prfix_workload_name'`.

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/prfix.py (append)
from bridge.workload import (
    VERIFIER_AGENT, ATTEMPT_ANNOTATION, gate_profile_for,
)

PRFIX_CREATED_BY = "dispatch-bridge-prfix"
PRFIX_REPO_ANNOTATION = "foreman.llmkube.dev/prfix-repo"
PRFIX_PR_ANNOTATION = "foreman.llmkube.dev/prfix-pr"


def prfix_workload_name(item: "PrFixItem") -> str:
    owner_repo = item.repo.replace("/", "-").lower()
    return f"prfix-{owner_repo}-{item.pr}"


def build_fix_workload(item, namespace, gate_profile, agent_name, coder_agent, attempt=1) -> dict:
    """Explicit code -> verify pipeline that amends the PR's head branch.

    reviseFromBranch (LLMKube#967) makes the executor fetch + check out the PR
    branch so the coder edits real prior work; allowOverwrite (#948) lets the
    push force-with-lease the existing ref. No reviewer/openPullRequest: the PR
    already exists and its own CI + external reviewer re-gate the update."""
    n = item.pr
    code_payload = {
        "repo": item.repo,
        "branch": item.branch,
        "reviseFromBranch": item.branch,
        "allowOverwrite": True,
        "prompt": assemble_fix_prompt(item),
    }
    if item.issue is not None:
        code_payload["issue"] = item.issue
    verify_payload = {"repo": item.repo, "branch": item.branch}
    if item.issue is not None:
        verify_payload["issue"] = item.issue
    spec = {
        "intent": f"fix PR #{n}",
        "repo": item.repo,
        "pipeline": [
            {"name": f"fix-{n}", "kind": "issue-fix",
             "agentRef": {"name": coder_agent}, "payload": code_payload},
            {"name": f"fixverify-{n}", "kind": "verify",
             "agentRef": {"name": VERIFIER_AGENT}, "dependsOn": [f"fix-{n}"],
             "payload": verify_payload},
        ],
    }
    if gate_profile:
        spec["gateProfile"] = gate_profile
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": prfix_workload_name(item),
            "namespace": namespace,
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": item.lane},
            "annotations": {
                ATTEMPT_ANNOTATION: str(attempt),
                PRFIX_REPO_ANNOTATION: item.repo,
                PRFIX_PR_ANNOTATION: str(n),
            },
        },
        "spec": spec,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bridge/prfix.py tests/test_prfix.py
git commit -s -m "feat: build_fix_workload (code+verify pipeline on the PR branch)"
```

---

### Task 4: Drain pass

**Files:**
- Modify: `bridge/prfix.py`
- Test: `tests/test_prfix.py`

**Interfaces:**
- Consumes: `parse_pr_fix_item`, `pr_fix_coder_for`, `build_fix_workload`, `prfix_workload_name`, `ACTIONABLE_LANES`, `DEFAULT_PRFIX_LANE_AGENTS`; `gate_profile_for` from `bridge.workload`.
- Produces: `drain_pr_fixes(list_queued, existing_prfix_names, create_workload, gate_profiles, lane_agents, agent_name, namespace) -> list[str]`. `list_queued() -> list[dict]` (already lane-filtered by the caller's API query); `existing_prfix_names: set[str]`; `create_workload(manifest: dict) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prfix.py (append)
from bridge.prfix import drain_pr_fixes


def _raw(repo="o/r", pr=1, lane="NORMAL", branch="b", **kw):
    d = {"repo": repo, "pr": pr, "lane": lane, "branch": branch, "type": "OTHER", "reason": "x"}
    d.update(kw)
    return d


def test_drain_creates_for_new_items():
    created = []
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(repo="o/r", pr=5)],
        existing_prfix_names=set(),
        create_workload=created.append,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert len(created) == 1 and created[0]["metadata"]["name"] == "prfix-o-r-5"
    assert out == ["o/r#5:created:prfix-o-r-5"]


def test_drain_skips_in_flight_and_branchless():
    created = []
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(pr=5), _raw(pr=6, branch=None)],
        existing_prfix_names={"prfix-o-r-5"},          # 5 already in flight
        create_workload=created.append,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert created == []
    assert "o/r#5:skip:in-flight" in out and "o/r#6:skip:no-branch" in out


def test_drain_isolates_per_item_failure():
    created = []
    def create(m):
        if m["metadata"]["name"] == "prfix-o-r-5":
            raise RuntimeError("boom")
        created.append(m)
    out = drain_pr_fixes(
        list_queued=lambda: [_raw(pr=5), _raw(pr=6)],
        existing_prfix_names=set(), create_workload=create,
        gate_profiles={}, lane_agents={}, agent_name="a", namespace="llm",
    )
    assert [m["metadata"]["name"] for m in created] == ["prfix-o-r-6"]   # 6 still created
    assert any("o/r#5:error:" in line for line in out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: FAIL with `ImportError: cannot import name 'drain_pr_fixes'`.

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/prfix.py (append)
def drain_pr_fixes(list_queued, existing_prfix_names, create_workload,
                   gate_profiles, lane_agents, agent_name, namespace) -> list:
    """Create a fix Workload per newly-QUEUED item. list_queued returns raw
    dicts already filtered to actionable lanes by the API query. An item is
    skipped when it has no branch (nothing to amend) or already has an
    in-flight prfix Workload (reconcile owns it; the item stays QUEUED). One
    bad item never aborts the pass."""
    lane_agents = lane_agents or {}
    results = []
    for raw in list_queued():
        item = parse_pr_fix_item(raw)
        if item is None:
            results.append("unparseable:skip")
            continue
        tag = f"{item.repo}#{item.pr}"
        if not item.branch:
            results.append(f"{tag}:skip:no-branch")
            continue
        name = prfix_workload_name(item)
        if name in existing_prfix_names:
            results.append(f"{tag}:skip:in-flight")
            continue
        try:
            manifest = build_fix_workload(
                item, namespace, gate_profile_for(item.repo, gate_profiles),
                agent_name, pr_fix_coder_for(item.lane, lane_agents), attempt=1,
            )
            create_workload(manifest)
            results.append(f"{tag}:created:{name}")
        except Exception as e:  # per-item isolation
            results.append(f"{tag}:error:{e}")
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bridge/prfix.py tests/test_prfix.py
git commit -s -m "feat: drain_pr_fixes (create fix Workloads for queued items)"
```

---

### Task 5: Reconcile pass

**Files:**
- Modify: `bridge/prfix.py`
- Test: `tests/test_prfix.py`

**Interfaces:**
- Consumes: annotation constants + labels (Task 3), `ATTEMPT_ANNOTATION`.
- Produces: `rebuild_prfix_manifest(wl: dict, attempt: int) -> dict` (clean create-able manifest from a listed Workload, attempt overridden); `reconcile_pr_fixes(list_prfix_workloads, delete_workload, create_workload, mark_pr_fix, max_attempts=3) -> list[str]`. `list_prfix_workloads() -> list[dict]` (listed Workload manifests with the `created-by=dispatch-bridge-prfix` label); `delete_workload(name) -> None`; `mark_pr_fix(repo, pr, status, note) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prfix.py (append)
from bridge.prfix import reconcile_pr_fixes, rebuild_prfix_manifest, PRFIX_CREATED_BY


def _wl(pr, phase, attempt=1, name=None):
    name = name or f"prfix-o-r-{pr}"
    return {
        "metadata": {
            "name": name, "namespace": "llm",
            "labels": {"created-by": PRFIX_CREATED_BY, "lane": "NORMAL"},
            "annotations": {
                "foreman.llmkube.dev/attempt": str(attempt),
                "foreman.llmkube.dev/prfix-repo": "o/r",
                "foreman.llmkube.dev/prfix-pr": str(pr),
            },
        },
        "spec": {"repo": "o/r", "pipeline": [{"name": f"fix-{pr}"}]},
        "status": {"phase": phase},
    }


def test_rebuild_prfix_manifest_bumps_attempt_and_strips_status():
    fresh = rebuild_prfix_manifest(_wl(5, "Failed", attempt=1), attempt=2)
    assert fresh["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert "status" not in fresh
    assert fresh["metadata"]["name"] == "prfix-o-r-5"
    assert "resourceVersion" not in fresh["metadata"] and "uid" not in fresh["metadata"]


def test_reconcile_succeeded_marks_fixed_and_deletes():
    marks, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Succeeded")],
        delete_workload=deleted.append,
        create_workload=lambda m: (_ for _ in ()).throw(AssertionError("no recreate")),
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status)),
    )
    assert marks == [("o/r", 5, "FIXED")]
    assert deleted == ["prfix-o-r-5"]
    assert out == ["prfix-o-r-5:fixed"]


def test_reconcile_failed_under_max_deletes_and_recreates():
    created, deleted = [], []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=1)],
        delete_workload=deleted.append, create_workload=created.append,
        mark_pr_fix=lambda *a: (_ for _ in ()).throw(AssertionError("no mark")),
        max_attempts=3,
    )
    assert deleted == ["prfix-o-r-5"]
    assert created[0]["metadata"]["annotations"]["foreman.llmkube.dev/attempt"] == "2"
    assert out == ["prfix-o-r-5:retry:2/3"]


def test_reconcile_failed_at_max_marks_blocked():
    marks = []
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Failed", attempt=3)],
        delete_workload=lambda n: None, create_workload=lambda m: None,
        mark_pr_fix=lambda repo, pr, status, note: marks.append((repo, pr, status, note)),
        max_attempts=3,
    )
    assert marks[0][:3] == ("o/r", 5, "BLOCKED")
    assert "3/3" in marks[0][3]                       # note carries attempt count
    assert out == ["prfix-o-r-5:giveup:3/3"]


def test_reconcile_ignores_nonterminal_and_isolates_errors():
    marks = []
    def delete(n):
        raise RuntimeError("wedged")
    out = reconcile_pr_fixes(
        list_prfix_workloads=lambda: [_wl(5, "Running"), _wl(6, "Failed", attempt=1)],
        delete_workload=delete, create_workload=lambda m: None,
        mark_pr_fix=lambda *a: marks.append(a), max_attempts=3,
    )
    assert not any("prfix-o-r-5" in line for line in out)     # Running: untouched
    assert any("prfix-o-r-6:error:" in line for line in out)  # delete raised, isolated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: FAIL with `ImportError: cannot import name 'reconcile_pr_fixes'`.

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/prfix.py (append)
_TERMINAL = ("Succeeded", "Completed", "Failed")


def rebuild_prfix_manifest(wl: dict, attempt: int) -> dict:
    """Reconstruct a clean, create-able manifest from a listed fix Workload,
    overriding the attempt annotation. Strips server-managed metadata and
    status so it can be re-created under the same name after delete."""
    meta = wl.get("metadata") or {}
    ann = dict(meta.get("annotations") or {})
    ann[ATTEMPT_ANNOTATION] = str(attempt)
    return {
        "apiVersion": "foreman.llmkube.dev/v1alpha1",
        "kind": "Workload",
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "labels": dict(meta.get("labels") or {}),
            "annotations": ann,
        },
        "spec": wl.get("spec") or {},
    }


def _prfix_key(wl: dict):
    ann = (wl.get("metadata") or {}).get("annotations") or {}
    pr = ann.get(PRFIX_PR_ANNOTATION)
    return ann.get(PRFIX_REPO_ANNOTATION), (int(pr) if pr and pr.isdigit() else None)


def reconcile_pr_fixes(list_prfix_workloads, delete_workload, create_workload,
                       mark_pr_fix, max_attempts=3) -> list:
    """Settle prior fix Workloads: Succeeded -> mark FIXED + delete; Failed
    under the attempt cap -> delete + recreate at attempt+1; Failed at the cap
    -> mark BLOCKED + leave a tombstone. Non-terminal Workloads are untouched.
    Per-Workload isolation so one wedged delete/create/mark cannot abort the
    pass or the drain that follows."""
    results = []
    for wl in list_prfix_workloads():
        name = ((wl.get("metadata") or {}).get("name")) or "?"
        phase = ((wl.get("status") or {}).get("phase")) or ""
        if phase not in _TERMINAL:
            continue
        repo, pr = _prfix_key(wl)
        ann = (wl.get("metadata") or {}).get("annotations") or {}
        attempt = int(ann.get(ATTEMPT_ANNOTATION, "1") or "1")
        try:
            if phase in ("Succeeded", "Completed"):
                if repo and pr is not None:
                    mark_pr_fix(repo, pr, "FIXED", f"foreman fix Workload {name} succeeded")
                delete_workload(name)
                results.append(f"{name}:fixed")
            elif attempt < max_attempts:
                delete_workload(name)
                create_workload(rebuild_prfix_manifest(wl, attempt + 1))
                results.append(f"{name}:retry:{attempt + 1}/{max_attempts}")
            else:
                if repo and pr is not None:
                    mark_pr_fix(repo, pr, "BLOCKED",
                                f"foreman fix exhausted {attempt}/{max_attempts} attempts ({name})")
                results.append(f"{name}:giveup:{attempt}/{max_attempts}")
        except Exception as e:  # per-Workload isolation
            results.append(f"{name}:error:{e}")
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prfix.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bridge/prfix.py tests/test_prfix.py
git commit -s -m "feat: reconcile_pr_fixes (mark FIXED/BLOCKED, bounded retry)"
```

---

### Task 6: DispatchClient methods

**Files:**
- Modify: `bridge/claim.py`
- Test: `tests/test_claim.py` (append; if absent, create with the same import style as `tests/test_workload.py`)

**Interfaces:**
- Consumes: existing `DispatchClient(base_url, token, http_get, http_post)` with `self._get(url, headers)` / `self._post(url, headers, payload)`.
- Produces: `DispatchClient.list_pr_fix_queued(lanes: list[str]) -> list[dict]` (one GET per lane, concatenated; `[]` on a non-list response); `DispatchClient.mark_pr_fix(repo, pr, status, note="") -> bool` (`True` when the POST returns non-None).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claim.py (append or create)
from bridge.claim import DispatchClient


def test_list_pr_fix_queued_queries_each_lane():
    calls = []
    def http_get(url, headers):
        calls.append(url)
        return [{"repo": "o/r", "pr": 1}] if "NORMAL" in url else [{"repo": "o/r", "pr": 2}]
    c = DispatchClient("http://d", "t", http_get, lambda *a: {})
    items = c.list_pr_fix_queued(["NORMAL", "ESCALATED"])
    assert {i["pr"] for i in items} == {1, 2}
    assert any("lane=NORMAL" in u and "/api/pr-fix-queue/queued" in u for u in calls)
    assert any("lane=ESCALATED" in u for u in calls)


def test_mark_pr_fix_posts_payload():
    seen = {}
    def http_post(url, headers, payload):
        seen["url"] = url; seen["payload"] = payload
        return {"ok": True}
    c = DispatchClient("http://d", "t", lambda *a: [], http_post)
    assert c.mark_pr_fix("o/r", 5, "FIXED", "done") is True
    assert seen["url"].endswith("/api/pr-fix-queue/mark")
    assert seen["payload"] == {"repo": "o/r", "pr": 5, "status": "FIXED", "note": "done"}


def test_mark_pr_fix_false_when_post_returns_none():
    c = DispatchClient("http://d", "t", lambda *a: [], lambda *a: None)
    assert c.mark_pr_fix("o/r", 5, "FIXED") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claim.py -q`
Expected: FAIL with `AttributeError: 'DispatchClient' object has no attribute 'list_pr_fix_queued'`.

- [ ] **Step 3: Write minimal implementation**

```python
# bridge/claim.py — add these methods to DispatchClient
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claim.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add bridge/claim.py tests/test_claim.py
git commit -s -m "feat: DispatchClient.list_pr_fix_queued + mark_pr_fix"
```

---

### Task 7: Wire the pass into main + config

**Files:**
- Modify: `bridge/main.py`
- Modify (separate repo, documented only): `home-ops` bridge HelmRelease env.

**Interfaces:**
- Consumes: everything above — `reconcile_pr_fixes`, `drain_pr_fixes`, `DEFAULT_PRFIX_LANE_AGENTS`, `ACTIONABLE_LANES`, `PRFIX_CREATED_BY`, `DispatchClient.list_pr_fix_queued`/`mark_pr_fix`, and the existing `api`/`dispatch`/`create_workload`/`delete_workload` closures in `_real_main`.
- Produces: the running pass. (Wiring is `# pragma: no cover` like the rest of `_real_main`; correctness is covered by Tasks 1–6.)

- [ ] **Step 1: Add the env parsing and closures in `_real_main`**

In `bridge/main.py`, add the import:

```python
from bridge.prfix import (
    reconcile_pr_fixes, drain_pr_fixes, prfix_workload_name,
    DEFAULT_PRFIX_LANE_AGENTS, ACTIONABLE_LANES, PRFIX_CREATED_BY,
)
import json
```

In `_real_main`, after the existing env parsing (near `escalation_lane = ...`):

```python
    pr_fix_enabled = os.environ.get("PR_FIX_ENABLED", "").strip().lower() in ("1", "true", "yes")
    pr_fix_max_attempts = int(os.environ.get("PR_FIX_MAX_ATTEMPTS", "3"))
    _raw_lane_agents = os.environ.get("PR_FIX_LANE_AGENTS", "").strip()
    pr_fix_lane_agents = json.loads(_raw_lane_agents) if _raw_lane_agents else dict(DEFAULT_PRFIX_LANE_AGENTS)
```

- [ ] **Step 2: Add the pass after `run_once`**

After the existing `for line in run_once(...)` loop in `_real_main`:

```python
    if pr_fix_enabled:
        def list_prfix_workloads() -> list:
            resp = api.list_namespaced_custom_object(
                group="foreman.llmkube.dev", version="v1alpha1",
                namespace=namespace, plural="workloads",
                label_selector=f"created-by={PRFIX_CREATED_BY}",
            )
            return resp.get("items", [])

        def mark_pr_fix(repo, pr, status, note=""):
            try:
                dispatch.mark_pr_fix(repo, pr, status, note)
            except Exception as e:  # best-effort; tombstone remains, next tick retries
                print(f"prfix-mark-failed:{repo}#{pr}:{status}:{e}")

        for line in reconcile_pr_fixes(
            list_prfix_workloads, delete_workload, create_workload,
            mark_pr_fix, max_attempts=pr_fix_max_attempts,
        ):
            print(line)

        existing = {
            (wl.get("metadata") or {}).get("name") for wl in list_prfix_workloads()
        }
        for line in drain_pr_fixes(
            lambda: dispatch.list_pr_fix_queued(list(ACTIONABLE_LANES)),
            existing, create_workload,
            gate_profiles, pr_fix_lane_agents, agent_name, namespace,
        ):
            print(line)
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (all — the new prfix + claim tests plus the existing suite; `_real_main` remains uncovered by design).

- [ ] **Step 4: Bump version + document env (home-ops)**

Bump the bridge to the next version (e.g. `0.6.2` if PR #8 not yet released, else `0.6.3`) per the repo's release convention (git tag `v<version>` → the Release workflow builds the image). In the `home-ops` bridge HelmRelease, add the env (leave `PR_FIX_ENABLED` off until foreman ≥ 0.8.28 is deployed, since the fix Workload needs #967's `reviseFromBranch`):

```yaml
              PR_FIX_ENABLED: "false"
              PR_FIX_MAX_ATTEMPTS: "3"
              PR_FIX_LANE_AGENTS: '{"NORMAL":"coder","ESCALATED":"coder-frontier"}'
```

- [ ] **Step 5: Commit**

```bash
git add bridge/main.py
git commit -s -m "feat: wire the PR-fix actuator pass into the bridge tick"
```

---

## Self-Review

**1. Spec coverage:**
- Two-pass reconcile→drain: Tasks 4 (drain) + 5 (reconcile), wired Task 7 in that order. ✓
- Dispatch client methods: Task 6. ✓
- Fix Workload shape (code+verify, reviseFromBranch, allowOverwrite, no review/PR-open): Task 3. ✓
- Lane→coder (PrFixLane vocab), feedback assembly: Task 2. ✓
- Bounding 3→BLOCKED, no re-lane: Task 5. ✓
- Skip NEEDS_HUMAN: `ACTIONABLE_LANES=("NORMAL","ESCALATED")` used in Task 7's `list_pr_fix_queued` call. ✓
- Config flags + dark ship: Task 7. ✓
- Drain dedup vs in-flight, branchless skip, per-item isolation: Task 4. ✓
- Surfacing (dispatch#557): out of scope by design — no task, correct.
- `evidenceKeys` deferred: not parsed — correct.

**2. Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**3. Type consistency:** `PrFixItem` fields identical across Tasks 1–4. `build_fix_workload(item, namespace, gate_profile, agent_name, coder_agent, attempt=1)` signature consistent between Task 3 (def) and Task 4 (call). `mark_pr_fix(repo, pr, status, note)` consistent between Task 5 (callable contract), Task 6 (method), Task 7 (closure). `ATTEMPT_ANNOTATION` reused from `bridge.workload`. Workload label `created-by=dispatch-bridge-prfix` consistent (Task 3 sets, Task 5/7 select). ✓
