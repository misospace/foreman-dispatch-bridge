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


def test_env_registry_covers_every_env_var_the_bridge_reads():
    """``OPTIONAL_VARS``/``REQUIRED_VARS`` in bridge/env.py must cover all reads.

    Issue #173: ``OPTIONAL_VARS`` is the reference registry of supported
    configuration, but it drifted from what the bridge actually reads
    (FIX_FIRST_AGENTS, LOG_FORMAT, LOG_LEVEL, REPO_CODER_AGENTS were missing).
    This test asserts no env var read by the bridge falls outside
    ``REQUIRED_VARS ∪ OPTIONAL_VARS`` so the registry cannot drift again.
    """
    from bridge.env import OPTIONAL_VARS, REQUIRED_VARS

    read_by_bridge = _env_vars_read_by_bridge()
    registered = set(REQUIRED_VARS) | set(OPTIONAL_VARS)

    missing = sorted(read_by_bridge - registered)
    assert not missing, (
        f"bridge/env.py registry (REQUIRED_VARS ∪ OPTIONAL_VARS) is missing env "
        f"vars the bridge reads: {missing}. See issue #173."
    )


def test_dispatch_agent_name_default_agrees_across_readme_registry_and_code():
    """The DISPATCH_AGENT_NAME default must agree in all three places.

    Issue #253: the README row documented ``foreman/coder`` while its own
    description said "use a dash, not a slash", and the same slashed default
    lived in ``bridge/env.py``'s ``OPTIONAL_VARS`` and the
    ``os.environ.get(...)`` fallback in ``bridge/main.py``. This test pins the
    README row, the env registry, and the code default to a single value so
    the three cannot drift apart again.
    """
    from bridge.env import OPTIONAL_VARS

    text = README_PATH.read_text(encoding="utf-8")
    config_match = re.search(r"^## Configuration.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    section = config_match.group(0) if config_match else text
    row = re.search(r"\|\s*`DISPATCH_AGENT_NAME`\s*\|\s*`([^`]*)`\s*\|", section)
    assert row is not None, "DISPATCH_AGENT_NAME row missing from README Configuration table"
    readme_default = row.group(1).strip()

    registry_default = OPTIONAL_VARS["DISPATCH_AGENT_NAME"]

    main_src = (BRIDGE_DIR / "main.py").read_text(encoding="utf-8")
    code_match = re.search(
        r'os\.environ\.get\(\s*["\']DISPATCH_AGENT_NAME["\']\s*,\s*["\']([^"\']*)["\']\s*\)',
        main_src,
    )
    assert code_match is not None, "DISPATCH_AGENT_NAME os.environ.get default missing from bridge/main.py"
    code_default = code_match.group(1)

    assert readme_default == registry_default == code_default, (
        f"DISPATCH_AGENT_NAME defaults disagree: README={readme_default!r}, "
        f"bridge/env.py={registry_default!r}, bridge/main.py={code_default!r}. See issue #253."
    )
    assert "/" not in readme_default, (
        f"DISPATCH_AGENT_NAME default {readme_default!r} contains a slash; "
        "the README row itself says to use a dash, not a slash. See issue #253."
    )


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
