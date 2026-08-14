"""Regression guardrail for issue #97.

The README's Configuration table must list every env var the bridge reads.
The test scans ``bridge/`` for ``os.environ[...]`` reads and compares them
to the set of back-tick tokens in the table rows of README.md's
Configuration section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "bridge"
README = REPO_ROOT / "README.md"


_ENV_READ_PATTERN = re.compile(
    r"""os\.(?:
        environ_(?:array\.)?get\(
            [^,)]*,\s*["'][A-Z_][A-Z0-9_]*["']
        |
        environ\[\s*["']([A-Z_][A-Z0-9_]*)["']\s*\]
        |
        environ\[\s*"(?:[^"\\]|\\.)*"\s*\]\s*if\s+["']([A-Z_][A-Z0-9_]*)["']
    )""",
    re.VERBOSE,
)


def _env_vars_read_by_bridge() -> set[str]:
    read: set[str] = set()
    for path in BRIDGE_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _ENV_READ_PATTERN.finditer(text):
            for grp in match.groups():
                if grp and grp.isupper() and grp.startswith(("RETRY", "PR_", "LOG_")) or grp in {
                    "DISPATCH_AGENT_TOKEN",
                }:
                    read.add(grp)
    # Also collect ``os.environ.get("FOO", ...)`` style reads (the default
    # token lives inside the first argument string).
    for path in BRIDGE_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'os\.environ(?:\.get)?\(\s*["\']([A-Z_][A-Z0-9_]*)["\']',
            text,
        ):
            name = match.group(1)
            if name.isupper():
                read.add(name)
        # Subscript form: ``os.environ["FOO"]``
        for match in re.finditer(
            r'os\.environ\[\s*["\']([A-Z_][A-Z0-9_]*)["\']\s*\]',
            text,
        ):
            name = match.group(1)
            if name.isupper():
                read.add(name)
    return read


_TABLE_ROW_PATTERN = re.compile(r"^\|\s*`([A-Z_][A-Z0-9_]*)`\s*\|", re.MULTILINE)


def _env_vars_documented_in_readme() -> set[str]:
    text = README.read_text(encoding="utf-8")

    # Restrict to the Configuration section to avoid prose mentions.
    config_match = re.search(
        r"^##\s*Configuration[^#]*?(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    section = config_match.group(0) if config_match else text
    return set(_TABLE_ROW_PATTERN.findall(section))


@pytest.mark.skipif(
    not BRIDGE_DIR.exists() or not README.exists(),
    reason="repo layout changed",
)
def test_readme_documents_every_env_var_the_bridge_reads() -> None:
    read_by_bridge = _env_vars_read_by_bridge()
    documented = _env_vars_documented_in_readme()

    missing = sorted(read_by_bridge - documented)
    assert not missing, (
        "README.md Configuration table is missing env vars that the bridge "
        f"reads: {missing}. See issue #97."
    )
