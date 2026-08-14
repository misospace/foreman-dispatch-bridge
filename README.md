# foreman-dispatch-bridge

The integration point between [misospace/dispatch](https://github.com/misospace/dispatch)
(GitHub issue assignment layer) and [LLMKube Foreman](https://github.com/defilantech/LLMKube)
(agentic execution): a CronJob that claims groomed, ready issues from dispatch
lane queues and materializes Foreman `Workload`s for them — then shepherds the
failures.

```
dispatch lanes ──claim──► bridge ──create──► Workload ──► code → [verify] → review
                              │
                              ├─ retry pass: Failed Workloads are deleted and
                              │  recreated (≤ RETRY_MAX_ATTEMPTS), carrying the
                              │  reviewer's NO-GO findings into the retry coder's
                              │  prompt (explicit spec.pipeline + payload.prompt)
                              └─ escalation: exhausted issues are re-laned to
                                 ESCALATION_LANE + unclaimed; the next tick
                                 claims them there with that lane's coder Agent
```

`[verify]` is optional — controlled by `VERIFY_ENABLED`. When disabled the bridge
relies on repository CI instead of the Foreman gate. Requires Foreman >= 0.9.9.

Each tick (one CronJob run): reconcile failures first, then claim one ready
issue per lane.

## Configuration (env)

| Env | Default | Meaning |
|---|---|---|
| `DISPATCH_URL` | `http://dispatch.llm:3000` | dispatch base URL |
| `DISPATCH_AGENT_TOKEN` | *(required)* | Bearer token for the dispatch API |
| `DISPATCH_AGENT_NAME` | `foreman/coder` | queue identity (use a dash, not a slash) |
| `DISPATCH_LANES` | `local,cloud,frontier` | lanes polled per tick |
| `FOREMAN_NAMESPACE` | `llm` | namespace for Workloads |
| `GATEPROFILE_MAP` | *(empty)* | JSON `{repo: GateProfile}` with `"*"` wildcard |
| `LANE_CODER_AGENTS` | *(empty)* | JSON `{lane: coderAgentName}` with `"*"` wildcard; wins over `BASE_CODER_AGENTS` |
| `BASE_CODER_AGENTS` | *(empty)* | JSON `{language: coderAgentName}` with `"*"` wildcard; routes the base lane's coder by the repo's `GATEPROFILE_MAP` language |
| `ESCALATION_LANE` | *(empty = off)* | lane exhausted issues re-lane into |
| `RETRY_MAX_ATTEMPTS` | `3` | attempts before escalate/tombstone |
| `PR_FIX_ENABLED` | *(off)* | enable the PR-fix drain/reconcile loop |
| `PR_FIX_MAX_ATTEMPTS` | `3` | pr-fix attempts before BLOCKED/tombstone |
| `GITHUB_TOKEN` | *(empty)* | used to check a PR's `mergeable_state` before marking a pr-fix FIXED (unauthenticated if unset) |
| `VERIFY_ENABLED` | `true` | set to `false` to omit the verify step and rely on repository CI (requires Foreman >= 0.9.9). Older Foreman versions reject Workloads without the required `verifierAgentRef`. |
| `VERDICT_SELF_GO` | *(empty)* | when set to a truthy value (`true`/`1`/`yes`/`on`) the bridge accepts terminal `pass`/`GO` verdicts directly; when empty/false the bridge requires an external reviewer and `self-Go` verdicts are treated as a NO-GO escalation. |
| `MAX_IN_PROGRESS` | `0` | maximum concurrent in-progress Workloads per lane (0 = unlimited). |
| `PRUNE_COMPLETED_AFTER_HOURS` | `6` | hours a Completed Workload is retained before the bridge deletes it. |
| `PRUNE_FAILED_AFTER_HOURS` | `48` | hours a Failed Workload is retained before the bridge tombstones + deletes it. |
| `REVISION_CODER_AGENTS` | *(empty)* | JSON `{repo: coderAgentName}` with `"*"` wildcard; selects the coder Agent used by the revision lane. |
| `REPO_CODER_AGENTS` | *(empty)* | JSON `{repo: coderAgentName}` with `"*"` wildcard; selects the coder Agent used for issue-path Workloads on that repo. |
| `CODER_AGENT_SLOTS` | *(empty)* | JSON `{key: int}` mapping lane/repo keys to the maximum number of coder Agent slots they can occupy; falls back to `1` per key. |
| `PR_FIX_LANE_AGENTS` | *(empty)* | JSON `{lane: coderAgentName}` with `"*"` wildcard; selects the coder Agent used by the pr-fix lane (otherwise the normal coder selection applies). |
| `DELETE_WORKLOAD_TIMEOUT_S` | `60` | seconds the bridge waits for a Workload delete to complete before timing out and proceeding. |
| `LOG_FORMAT` | `json` | log format; `json` for structured logs, `plain` for human-readable. |
| `LOG_LEVEL` | `info` | log level; one of `debug`, `info`, `warning`, `error`. |

PR-fix retries **preserve** the pipeline shape set at creation — `rebuild_prfix_manifest`
reuses the existing spec, so toggling this env variable after a PR-fix Workload exists
has no effect on its retries. Issue-path retries always pick up the **current** env value
each attempt because they rebuild from scratch via `build_workload`.

## RBAC

The bridge needs, in `FOREMAN_NAMESPACE`:
- `workloads.foreman.llmkube.dev`: `create`, `get`, `list`, `delete`
- `agentictasks.foreman.llmkube.dev`: `get`, `list` (reads a failed Workload's
  review findings to build feedback-carrying retries)

## Development

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

Pure-logic modules (`claim`, `workload`, `retry`) take injected transports and
k8s callables, so the tests run without a cluster or network.

## Releases

Tag `vX.Y.Z` → CI publishes `ghcr.io/misospace/foreman-dispatch-bridge:X.Y.Z`
and creates the GitHub release. Deployed via
[home-ops](https://github.com/joryirving/home-ops) (`kubernetes/apps/base/llm/dispatch/foreman-dispatch-bridge/`),
where the full pipeline is documented in the app README.

## History

Extracted from [joryirving/containers](https://github.com/joryirving/containers)
at 0.5.1 (fresh history); versions continue from 0.6.0.
