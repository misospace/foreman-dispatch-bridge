# foreman-dispatch-bridge

The integration point between [misospace/dispatch](https://github.com/misospace/dispatch)
(GitHub issue assignment layer) and [LLMKube Foreman](https://github.com/defilantech/LLMKube)
(agentic execution): a CronJob that claims groomed, ready issues from dispatch
lane queues and materializes Foreman `Workload`s for them — then shepherds the
failures.

```
dispatch lanes ──claim──► bridge ──create──► Workload ──► code → verify → review
                            │
                            ├─ retry pass: Failed Workloads are deleted and
                            │  recreated (≤ RETRY_MAX_ATTEMPTS), carrying the
                            │  reviewer's NO-GO findings into the retry coder's
                            │  prompt (explicit spec.pipeline + payload.prompt)
                            └─ escalation: exhausted issues are re-laned to
                               ESCALATION_LANE + unclaimed; the next tick
                               claims them there with that lane's coder Agent
```

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
| `LANE_CODER_AGENTS` | *(empty)* | JSON `{lane: coderAgentName}` with `"*"` wildcard |
| `ESCALATION_LANE` | *(empty = off)* | lane exhausted issues re-lane into |
| `RETRY_MAX_ATTEMPTS` | `3` | attempts before escalate/tombstone |

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
