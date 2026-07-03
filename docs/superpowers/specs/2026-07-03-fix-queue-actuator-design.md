# Fix-Queue Actuator Design

**Goal:** Drain dispatch's PR-fix queue by spawning a foreman fix Workload per queued item, so a PR that fails CI or gets change-requested is re-worked by the pipeline instead of a human — closing the loop from "PR opened" back to "PR mergeable."

**Status:** Approved design (2026-07-03). Next step: implementation plan.

## Context

Dispatch already owns the queue and its population. `pr-followup/sync` (scheduled every 15m) and `pr-followup/webhook` enqueue `PrFixQueueItem` rows for tracked PRs that need work. The bridge is purely a **consumer**: it drains QUEUED items and reports outcomes back via the existing API. No dispatch changes are in scope.

### Dispatch API (already exists, unchanged)

- `GET /api/pr-fix-queue/queued?lane=&include_blocked=&prioritize_by_type=` → array of items, type-prioritized.
- `POST /api/pr-fix-queue/mark {repo, pr, status, note}` → transition status. Valid statuses: `QUEUED`, `FIXED`, `BLOCKED`, `STALE`, `IGNORED`. Appends a `PrFixHistory` row. **Only status — there is no lane-mutation endpoint for fix items.**
- `POST /api/pr-fix-queue/enqueue` → add/upsert an item (unique on `[repo, pr]`).

`PrFixQueueItem` fields the actuator reads: `repo`, `pr`, `issue?`, `branch?` (PR head), `headSha?`, `lane` (`NORMAL` | `ESCALATED` | `NEEDS_HUMAN`), `type` (`MERGE_CONFLICT` | `CI_FAILURE` | `REVIEW_FEEDBACK` | `OTHER`), `reason`, `feedback[]`. `evidenceKeys[]` (blob log refs) is **out of scope for v1**.

Dispatch marks items `STALE` itself when the upstream PR merges/closes (`reconcileStalePrFixItems`, via `issues/reconcile`), so the actuator never needs to detect staleness.

## Decisions (locked)

1. **Scope: lane-gated, not type-gated.** Drain every `NORMAL` and `ESCALATED` item regardless of `type`. Skip `NEEDS_HUMAN` (dispatch enqueues those already `BLOCKED`, so they never appear in `queued` anyway). `MERGE_CONFLICT` / `OTHER` get the same coder+feedback path as the rest, with whatever `reason`/`feedback[]` exists.
2. **Fix pipeline: code → verify, no review, no PR-open.** The PR already exists; a force-with-lease push updates it, and the PR's own CI + external reviewer re-gate. An in-Workload Ornith review would duplicate that and add strix latency.
3. **Bounding: 3 attempts, then `BLOCKED`.** The bridge cannot re-lane (no API), so on exhaustion it marks `BLOCKED` for human/dispatch re-triage. The item's existing lane picks the coder tier on every attempt.
4. **Gallery PRs (295/296/297): manual `enqueue`.** They are `joryirving`-authored, so `pr-followup` won't track them. Seed them once via `POST /api/pr-fix-queue/enqueue` after the actuator ships; the actuator then drains them like any item. No dispatch change.

## Architecture

A new module `bridge/prfix.py` (pure functions + dependency injection, mirroring `bridge/retry.py`), driven by a third pass added to `bridge/main.py` after `reconcile_failures` and `run_once`. The pass has two sub-phases, exactly mirroring the retry loop's structure:

- **reconcile** — inspect `prfix-*` fix Workloads created on prior ticks; mark their queue items terminal and retry-or-block the failed ones.
- **drain** — poll newly-QUEUED items and create a fix Workload per new one.

Ordering (reconcile before drain) matches `reconcile_failures` running before `run_once`: settle prior work against the current config before claiming new work.

### File structure

- `bridge/prfix.py` (new) — pure logic: `PrFixItem` dataclass, feedback assembly, lane→coder resolution, fix-Workload builder, `reconcile_pr_fixes`, `drain_pr_fixes`. All I/O (list/create/delete queue+workload, mark) injected as callables — cluster-free and dispatch-free unit tests.
- `bridge/claim.py` (modify) — add two `DispatchClient` methods: `list_pr_fix_queued(lanes)` and `mark_pr_fix(repo, pr, status, note)`.
- `bridge/main.py` (modify) — parse the new env, build the in-cluster/HTTP closures (reusing the existing `api` and `dispatch` handles), invoke the new pass.
- `tests/test_prfix.py` (new) — unit tests for the pure logic.

## Components

### PrFixItem

Parsed from a `queued` array element:

```python
@dataclass(frozen=True)
class PrFixItem:
    repo: str
    pr: int
    issue: Optional[int]
    branch: Optional[str]
    head_sha: Optional[str]
    lane: str      # NORMAL | ESCALATED
    type: str      # MERGE_CONFLICT | CI_FAILURE | REVIEW_FEEDBACK | OTHER
    reason: str
    feedback: list[str]
```

An item with no `branch` cannot be fixed (nothing to check out) — it is skipped in drain and logged, never enqueued as a Workload.

### Fix Workload shape

Deterministic name `prfix-<owner>-<repo>-<pr>` (own namespace, distinct from `wl-<owner>-<repo>-<issue>` issue Workloads → no name collision; re-create is a 409 idempotent no-op). Explicit `spec.pipeline`, **code → verify** only:

- **code** step (`kind: issue-fix`): `payload.branch = payload.reviseFromBranch = item.branch`, `payload.allowOverwrite = true`, `payload.prompt =` assembled feedback (below), `payload.issue = item.issue` when present (so foreman's `fetchIssueBodyIfNeeded` appends the original issue body under the feedback, per LLMKube #967). `agentRef` resolved by lane (below).
- **verify** step (`kind: verify`): `dependsOn: [code]`, the per-repo gate profile from `GATEPROFILE_MAP` (real lint).
- No reviewer steps, no `openPullRequest`.

`spec.gateProfile` = the repo's profile from `gate_profiles` (reuse `gate_profile_for`). Labels: `created-by=dispatch-bridge-prfix`, `lane=<item.lane>`. Annotations: `foreman.llmkube.dev/attempt`, `foreman.llmkube.dev/prfix-repo`, `foreman.llmkube.dev/prfix-pr` (so reconcile can recover the queue key from the Workload).

### Feedback assembly

`assemble_fix_prompt(item) -> str`: a type-labeled header (`"CI failure:"`, `"Review feedback:"`, `"Merge conflict:"`, `""` for OTHER) followed by `item.reason`, then each `item.feedback[]` entry as a bullet. This becomes the code step's `payload.prompt` — the only channel that reaches the coder's user prompt.

### Lane → coder

New env `PR_FIX_LANE_AGENTS`, a JSON map in the `PrFixLane` vocabulary (distinct from the issue-lane vocabulary that `LANE_CODER_AGENTS` uses), default `{"NORMAL": "coder", "ESCALATED": "coder-frontier"}`. `pr_fix_coder_for(lane, map)` resolves exact → `"*"` wildcard → `"coder"` fallback.

## Reconcile & bounding

`reconcile_pr_fixes(list_prfix_workloads, delete_workload, create_workload, mark_pr_fix, ...)`:

For each `prfix-*` Workload that is terminal:
- **Succeeded** → `mark_pr_fix(repo, pr, "FIXED", note)`; delete the Workload (cleanup).
- **Failed & attempt < max** → delete + recreate at `attempt+1` (same name/branch; `reviseFromBranch` re-amends the PR branch). Reuses the current config.
- **Failed & attempt ≥ max** → `mark_pr_fix(repo, pr, "BLOCKED", note)` with the failure summary; leave the Workload as a tombstone (audit record; matches the retry loop's give-up behavior).

`attempt` is read from the Workload annotation. `repo`/`pr` are recovered from the annotations. Per-Workload `try/except` isolation so one wedged delete/create cannot abort the pass (matches `reconcile_failures`).

## Drain

`drain_pr_fixes(list_pr_fix_queued, list_prfix_workloads, create_workload, ...)`:

1. `items = list_pr_fix_queued(actionable_lanes)` where `actionable_lanes = ["NORMAL", "ESCALATED"]`.
2. Build the set of existing `prfix-*` Workload names (in-flight fixes reconcile owns).
3. For each item: skip if it has no `branch`; skip if `prfix-<...>` already exists (reconcile owns it, item stays QUEUED safely); otherwise `create_workload(build_fix_workload(item, attempt=1, ...))`.

Per-item `try/except` isolation. The item is **not** marked on creation — it stays QUEUED until reconcile observes the Workload's terminal outcome. This is the coordination primitive: QUEUED + an in-flight `prfix-*` Workload = "being fixed"; the deterministic name prevents duplicate Workloads.

## Configuration

- `PR_FIX_ENABLED` (default `false`) — feature flag; the whole pass is a no-op when unset, so it ships dark and is enabled by env when ready.
- `PR_FIX_MAX_ATTEMPTS` (default `3`).
- `PR_FIX_LANE_AGENTS` (default `{"NORMAL":"coder","ESCALATED":"coder-frontier"}`).

No new Kubernetes RBAC: fix Workloads are `workloads`, covered by the bridge's existing `create/get/list/delete`. The new dispatch calls use the existing `DISPATCH_AGENT_TOKEN` (same bearer auth as every other agent endpoint).

## Error handling

- A `queued` fetch that raises aborts only the drain phase (reconcile already ran); logged, next tick retries. Never crashes the bridge.
- A `mark` that raises is logged per-item; the Workload tombstone remains, so the next tick re-attempts the mark (idempotent — marking an already-FIXED item FIXED is a no-op transition).
- Per-item and per-Workload isolation throughout, matching the retry loop.

## Testing

Unit tests in `tests/test_prfix.py`, all with injected callables (no cluster, no dispatch), following `retry.py`'s test style:

- `PrFixItem` parsing (full item, missing optionals, missing branch).
- `assemble_fix_prompt` per type; reason + feedback ordering.
- `pr_fix_coder_for` precedence (exact / wildcard / fallback).
- `build_fix_workload` shape: name, pipeline steps (code+verify only, no review), `reviseFromBranch`/`allowOverwrite`/`issue`/prompt on the code payload, gate profile, labels, annotations.
- `reconcile_pr_fixes` transitions: succeeded→FIXED+delete, failed<max→delete+recreate at attempt+1, failed≥max→BLOCKED+tombstone, per-Workload isolation on a raising delete.
- `drain_pr_fixes`: creates for new items, skips items with an in-flight `prfix-*` Workload, skips branchless items, per-item isolation, `NEEDS_HUMAN`/other lanes never actioned.

## Surfacing NEEDS_HUMAN / BLOCKED to the operator

The actuator produces two "a human should look" signals: `NEEDS_HUMAN` items (dispatch enqueues these straight to `BLOCKED`, so they never reach the actuator) and items the actuator marks `BLOCKED` after 3 failed attempts. In both cases the item ends as `status=BLOCKED` with a descriptive `PrFixHistory` note.

**Today there is no active surfacing.** BLOCKED items are visible only via `GET /api/pr-fix-queue/queued?include_blocked=true` — no PR label, comment, board section, or notification. The bridge cannot close this gap itself (its secret carries only `DISPATCH_AGENT_TOKEN`, no GitHub credentials, and surfacing is not a consumer's job).

The actuator's contribution is to make the `BLOCKED` mark carry a **specific, actionable note** (the failure summary + attempt count + Workload name) so dispatch has the material to surface. Actually surfacing it — the recommended mechanism being a `needs-human` GitHub label + a one-line comment on the PR when an item transitions to `BLOCKED` (dispatch already owns GitHub label operations via claim/unclaim), and a BLOCKED section on the dispatch board — is a **dispatch-side change, tracked separately** (misospace/dispatch#557). It is out of scope for the bridge actuator but is the answer to "how do I see these."

## Out of scope (v1)

- `evidenceKeys[]` blob-log fetching (feedback[] + reason only).
- Bridge-driven re-laning NORMAL→ESCALATED on exhaustion (no dispatch API; exhaustion → BLOCKED).
- `MERGE_CONFLICT`-specific rebase handling (uses the same coder+feedback path).
- Teaching `pr-followup` to track non-bot-authored PRs (a dispatch change; gallery PRs are seeded manually instead).

## Dependencies

- Foreman must ship LLMKube #967 (`reviseFromBranch` executor restore) and #948 (`allowOverwrite`) — both merged; live once ≥ 0.8.28 deploys. Until then a fix Workload rebuilds from the base branch rather than amending, so the actuator's `PR_FIX_ENABLED` should stay off until 0.8.28 is in the cluster.
