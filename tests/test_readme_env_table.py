"""Guardrail: README.md must document every env var the bridge reads.

Issue #97: the README previously missed 9 env vars (VERDICT_SELF_GO,
MAX_IN_PROGRESS, PRUNE_COMPLETED_AFTER_HOURS, PRUNE_FAILED_AFTER_HOURS,
REVISION_CODER_AGENTS, PR_FIX_LANE_AGENTS, DELETE_WORKLOAD_TIMEOUT_S,
LOG_FORMAT, LOG_LEVEL). This test extracts every name referenced in
``os.environ.get(...)`` / ``os.environ[...]`` calls across the bridge
package and asserts each one appears inside a Markdown table row of
``README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
BRIDGE_DIR = REPO_ROOT / "bridge"

# Match `.get("FOO")`, `.get('FOO')`, `os.environ["FOO"]`, `os.environ['FOO']`,
# and the equivalent form `os.getenv("FOO")`. The variable name must be
# uppercase + underscore (Python convention for module-level constants).
ENV_VAR_RE = re.compile(r'(?:os\.environ(?:_array)?\.get|os\.environ|os\.getenv)\s*[\[(]\s*["\']([A-Z][A-Z0-9_]+)["\']')


def _env_vars_read_by_bridge() -> set[str]:
    seen: set[str] = set()
    for path in BRIDGE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        seen.update(ENV_VAR_RE.findall(text))
    return seen


def _env_vars_documented_in_readme() -> set[str]:
    text = README_PATH.read_text(encoding="utf-8")
    # A "documented" entry is a back-tick-wrapped token in a table row
    # within the Configuration section, stopping at the next `## ` heading.
    config_match = re.search(r"^## Configuration.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    section = config_match.group(0) if config_match else text
    found: set[str] = set()
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        found.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", line))
    return found


def test_readme_documents_every_env_var_the_bridge_reads():
    read_by_bridge = _env_vars_read_by_bridge()
    documented = _env_vars_documented_in_readme()

    missing = sorted(read_by_bridge - documented)
    assert not missing, f"README.md Configuration table is missing env vars the bridge reads: {missing}. See issue #97."


def test_readme_does_not_claim_a_wrong_default_for_max_in_progress():
    """The MAX_IN_PROGRESS default in code is "0"; README must match.

    The code's `MAX_IN_PROGRESS` is treated as "0 = unlimited". A previous
    PR review (PR #141) flagged a stale `10` default in this row.
    """
    text = README_PATH.read_text(encoding="utf-8")
    config_match = re.search(r"^## Configuration.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    section = config_match.group(0) if config_match else text
    row = re.search(r"\|\s*`MAX_IN_PROGRESS`\s*\|\s*`([^`]*)`\s*\|", section)
    assert row is not None, "MAX_IN_PROGRESS row missing from README Configuration table"
    assert row.group(1).strip() == "0", f"MAX_IN_PROGRESS default in README is {row.group(1)!r}; code default is '0'."
