"""Startup environment validation for the dispatch bridge.

Validates that all required environment variables are present before any
cluster or network interaction occurs.  Exits with a clear, actionable
error message on failure so misconfigured deployments fail fast at
container start rather than crashing inside the tick loop.
"""

import os
import sys
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Required env vars — must be set; no default is acceptable.
# ---------------------------------------------------------------------------
REQUIRED_VARS: List[str] = [
    "DISPATCH_AGENT_TOKEN",
]

# ---------------------------------------------------------------------------
# Optional env vars with their defaults (documented for reference).
# These are NOT validated at startup; a missing value simply means the
# default will be used at runtime.  The list is kept here so reviewers can
# see every consumed env var in one place.
# ---------------------------------------------------------------------------
OPTIONAL_VARS: Dict[str, str] = {
    "DISPATCH_URL": "http://dispatch.llm:3000",
    "DISPATCH_AGENT_NAME": "foreman/coder",
    "DISPATCH_LANES": "local,cloud,frontier",
    "FOREMAN_NAMESPACE": "llm",
    "LANE_CODER_AGENTS": "",
    "REVISION_CODER_AGENTS": "",
    "BASE_CODER_AGENTS": "",
    "GATE_PROFILES": "",
    "MAX_ATTEMPTS": "3",
    "ESCALATION_LANE": "",
    "PRUNE_COMPLETED_AFTER_HOURS": "6",
    "PRUNE_FAILED_AFTER_HOURS": "48",
    "MAX_IN_PROGRESS": "0",
    "PR_FIX_ENABLED": "false",
    "PR_FIX_INTERVAL_S": "300",
    "PR_FIX_MAX_ATTEMPTS": "3",
    "GITHUB_TOKEN": "",
    "VERIFY_ENABLED": "false",
}


def validate_env() -> None:
    """Check that every required env var is set.

    Exits the process with code 1 and a clear message listing all missing
    variables if any are absent.
    """
    missing = [name for name in REQUIRED_VARS if name not in os.environ]
    if missing:
        lines = [
            "FATAL: required environment variable(s) not set:",
        ]
        for name in missing:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append("Set the above variables and restart the container.")
        print("\n".join(lines), file=sys.stderr)
        sys.exit(1)
