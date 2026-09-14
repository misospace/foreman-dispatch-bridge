"""Guardrail: the project version must never drift from the release-please manifest again.

Issue #330 (the reopened class of #174): pyproject.toml pinned
``version = "0.7.0"`` while .release-please-manifest.json had already
reached 0.7.8 — the same drift #174 fixed exactly once, by hand, which
proves that a one-time bump is not a fix. The two halves of the fix
lived in the release tooling, not this suite:

* ``release-please-config.json`` switched from ``release-type: simple``
  to ``release-type: python``. The simple strategy only bumps
  .release-please-manifest.json and CHANGELOG.md; the python strategy
  additionally rewrites ``[project].version`` in pyproject.toml (its
  PyProjectToml updater, which reads the PEP 621 ``[project]`` table)
  inside the same "release: X.Y.Z" PR as the manifest bump. This repo
  ships no ``__init__.py``/``version.py``/``setup.py``/``setup.cfg``
  candidates, so pyproject.toml is the only file that updater touches
  — it cannot rewrite anything in the runtime code.
* ``uv.lock`` was removed (#331), so the project is no longer pinned to
  a stale version in a second place in the tree.

So on main the invariant this file pins is:

* ``pyproject.toml``'s ``[project].version`` equals the "." entry of
  ``.release-please-manifest.json`` (the version the last release
  tagged), and
* ``release-please-config.json`` still uses the ``python`` release
  type, so the next release keeps the two in sync automatically.

If a future release lands with a drifted pyproject.toml, the first
test goes red in the very CI run that could have caught the drift, and
the second test tells you the tooling was re-nerfed in the meantime.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MANIFEST_PATH = REPO_ROOT / ".release-please-manifest.json"
CONFIG_PATH = REPO_ROOT / "release-please-config.json"


def _manifest_version() -> str:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    root = manifest.get(".")
    assert isinstance(root, str) and root, (
        f".release-please-manifest.json has no root \".\" entry: {manifest!r}"
    )
    return root


def _pyproject_version() -> str:
    with PYPROJECT_PATH.open("rb") as f:
        project = tomllib.load(f).get("project", {})
    version = project.get("version")
    assert isinstance(version, str) and version, (
        f"pyproject.toml has no [project].version (or it is dynamic): {project!r}"
    )
    return version


def test_pyproject_version_matches_release_manifest():
    """pyproject.toml must carry the same version the last release tagged.

    This is the assertion issue #174/#330 keep re-filing: with release-type
    python the release PR updates both files together, so this can only
    fail if the updater was re-nerfed or someone hand-edited one of the
    two files.
    """
    manifest_version = _manifest_version()
    pyproject_version = _pyproject_version()
    assert pyproject_version == manifest_version, (
        f"pyproject.toml is at {pyproject_version} while .release-please-manifest.json "
        f"is at {manifest_version}. Bump pyproject.toml to match the manifest and check "
        "release-please-config.json is still release-type python so the next release "
        "keeps the two in sync (see #174, #330)."
    )


def test_release_config_still_uses_the_python_release_type():
    """The python strategy is what makes the version bump automatic.

    The release-please ``simple`` strategy only updates the manifest and the
    changelog, which is exactly how the #174 drift class happened twice.
    ``python`` is the first strategy to also rewrite pyproject.toml
    (setup.cfg/setup.py/version.py are absent here, so nothing else is
    touched).
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    release_type = config.get("release-type")
    assert release_type == "python", (
        f"release-please-config.json uses release-type {release_type!r}; only the "
        "\"python\" strategy updates pyproject.toml on release, and anything else "
        "lets the version drift from .release-please-manifest.json again (see #174, "
        "#330)."
    )


@pytest.mark.parametrize(
    ("pyproject_version", "manifest_version", "ok"),
    [
        ("0.9.0", "0.9.0", True),
        ("0.7.0", "0.7.8", False),  # the exact state #330 was filed against
        ("0.7.0", "0.9.0", False),  # current main before this fix
    ],
)
def test_version_agreement_logic(pyproject_version: str, manifest_version: str, ok: bool) -> None:
    """The comparison is pure equality; pin the cases the real files have been in
    so a loosening (substring match, prefix match) fails here instead of in
    the real assertion."""
    assert (pyproject_version == manifest_version) is ok
