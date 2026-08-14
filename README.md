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
| `CODER_AGENT_SLOTS` | `{}` | JSON map `{coderAgent: slotCount}` capping each coder's in-flight Workloads; `"*"` wildcard covers unnamed agents. Empty keeps the legacy issue-number split. |
| `ESCALATION_LANE` | *(empty = off)* | lane exhausted issues re-lane into |
| `RETRY_MAX_ATTEMPTS` | `3` | attempts before escalate/tombstone |
| `PR_FIX_ENABLED` | *(off)* | enable the PR-fix drain/reconcile loop |
| `PR_FIX_MAX_ATTEMPTS` | `3` | pr-fix attempts before BLOCKED/tombstone |
| `GITHUB_TOKEN` | *(empty)* | used to check a PR's `mergeable_state` before marking a pr-fix FIXED (unauthenticated if unset) |
| `VERIFY_ENABLED` | `true` | set to `false` to omit the verify step and rely on repository CI (requires Foreman >= 0.9.9). Older Foreman versions reject Workloads without the required `verifierAgentRef`. |
| `VERDICT_SELF_GO` | _(unset)_ | Comma-separated lanes whose terminal Workload verdict the bridge is allowed to auto-mark `selfGo` (recommended for use with `foreman dispatch run --wait`) |
| `MAX_IN_PROGRESS` | `0` | Maximum concurrent in-progress Workloads per lane (`0` disables the cap, i.e. unlimited) |
| `PRUNE_COMPLETED_AFTER_HOURS` | `6` | GC age, in hours, after which Completed Workloads are pruned (set to `0` to disable the sweeper) |
| `PRUNE_FAILED_AFTER_HOURS` | `48` | GC age, in hours, after which Failed/Crashed/Cancelled Workloads are pruned (set to `0` to disable the sweeper) |
| `REVISION_CODER_AGENTS` | `{}` | JSON map `{revisionLane: coderAgentName}` overriding the agent used when a revision prompt reaches the coder stage |
| `REPO_CODER_AGENTS` | `{}` | JSON map `{repoFullName: coderAgentName}` overriding the agent used when a generic revision prompt reaches the coder stage |
| `PR_FIX_LANE_AGENTS` | `{}` | JSON map `{prFixLane: coderAgentName}` overriding the agent used when a PR-fix prompt reaches the coder stage |
| `DELETE_WORKLOAD_TIMEOUT_S` | `60` | Seconds the bridge waits for the cluster to acknowledge a Workload deletion request before giving up |
| `LOG_FORMAT` | `json` | Log line format — `json` (structured, default) or `plain` (human-readable) |
| `LOG_LEVEL` | `INFO` | Minimum log level emitted — one of `DEBUG`, `INFO`, `WARNING`, `ERROR` |

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
