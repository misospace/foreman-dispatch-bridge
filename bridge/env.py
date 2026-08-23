"""Startup validation for required environment variables.

``validate_env()`` must be called before any cluster or network interaction so
that a misconfigured deployment fails fast with a clear error message instead of
crashing mid-tick with an uncaught ``KeyError``.
"""

import os
import sys
from typing import Dict, List

# Required env vars — no default; missing values cause sys.exit(1).
REQUIRED_VARS: List[str] = [
    "DISPATCH_AGENT_TOKEN",
]

# Optional env vars with their defaults (documented for reference).
OPTIONAL_VARS: Dict[str, str] = {
    "DISPATCH_URL": "http://dispatch.llm:3000",
    "DISPATCH_AGENT_NAME": "foreman/coder",
    "DISPATCH_LANES": "local,cloud,frontier",
    "FOREMAN_NAMESPACE": "llm",
    "GATEPROFILE_MAP": "",
    "RETRY_MAX_ATTEMPTS": "3",
    "LANE_CODER_AGENTS": "",
    "REVISION_CODER_AGENTS": "",
    "BASE_CODER_AGENTS": "",
    "CODER_AGENT_SLOTS": "",
    "ESCALATION_LANE": "",
    "VERIFY_ENABLED": "true",
    "VERDICT_SELF_GO": "",
    "PR_FIX_ENABLED": "",
    "PR_FIX_MAX_ATTEMPTS": "3",
    "GITHUB_TOKEN": "",
    "PR_FIX_LANE_AGENTS": "",
    "PRUNE_COMPLETED_AFTER_HOURS": "6",
    "PRUNE_FAILED_AFTER_HOURS": "48",
    "MAX_IN_PROGRESS": "0",
    "DELETE_WORKLOAD_TIMEOUT_S": "60",
    "FIX_FIRST_AGENTS": "",
    "REPO_CODER_AGENTS": "",
    "LOG_FORMAT": "json",
    "LOG_LEVEL": "INFO",
    "INFRA_PROBE_ENABLED": "true",
    "INFRA_PROBE_URL": "http://litellm.llm:4000/v1",
    "INFRA_PROBE_API_KEY": "",
}


def validate_env() -> None:
    """Check that all required env vars are present; exit 1 if any are missing.

    Prints a multi-line message to stderr listing every missing variable and
    exits with code 1. Does not interact with the cluster or network.
    """
    missing = [name for name in REQUIRED_VARS if name not in os.environ]
    if missing:
        lines = [
            "FATAL: Missing required environment variables:",
        ]
        for name in missing:
            lines.append(f"  - {name}")
        lines.append("Set them before starting the bridge.")
        sys.stderr.write("\n".join(lines) + "\n")
        sys.exit(1)
