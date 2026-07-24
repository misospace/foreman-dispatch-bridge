"""Tests for the structured logging added in issue #51.

These verify the three acceptance bullets:

* all ``print()`` calls in ``bridge/main.py`` are gone (covered by the
  grep test at the top of the file);
* the bridge configures a ``logging`` handler at import time;
* the configured handler emits one JSON object per line, parseable by
  ``jq`` (i.e. by :mod:`json`).
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


def test_no_print_in_bridge_runtime_code():
    """The whole point of the issue: no ``print()`` in bridge runtime code.

    The only allowed reference to ``print(`` in the entire ``bridge/``
    package is the explanatory comment in ``bridge/logging_setup.py``
    (the docstring describing what the module replaces).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py", "print(", "bridge/"],
        cwd=repo_root, capture_output=True, text=True,
    )
    # Strip the docstring occurrence before asserting.
    offenders = [
        line for line in result.stdout.splitlines()
        if "logging_setup.py" not in line
        and "docstring" not in line  # defensive: docstrings never reach here
    ]
    assert offenders == [], (
        "bridge/ still contains print() calls:\n  " + "\n  ".join(offenders)
    )


def test_bridge_main_configures_logging_at_import():
    """Importing :mod:`bridge.main` installs a handler on the root logger.

    Operators rely on this: even an early-import traceback or a one-off
    ``logging.warning(...)`` from a dependency should be routable through
    the JSON formatter.
    """
    # If this fails, ``bridge.main`` is not calling ``configure_logging``
    # at import time and a bare import will leave the root logger with
    # whatever defaults Python picked up first.
    import bridge.main  # noqa: F401  (imported for side-effects)
    root = logging.getLogger()
    assert any(
        getattr(h, "formatter", None) is not None
        for h in root.handlers
    ), "bridge.main must install a configured logging handler at import"


# ---------------------------------------------------------------------------
# JsonFormatter unit tests
# ---------------------------------------------------------------------------


def test_json_formatter_emits_parseable_line(monkeypatch):
    """A single info record round-trips through ``json.loads`` and ``jq``.

    This is the acceptance check from the issue ("tick output in JSON
    format is parseable by ``jq``"). We exercise both: Python's parser
    and a real ``jq`` invocation if the binary is on PATH.
    """
    from bridge.logging_setup import JsonFormatter, configure_logging

    monkeypatch.setenv("LOG_FORMAT", "json")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    configure_logging()
    logging.getLogger("bridge.test").info("hello %s", "world", extra={"lane": "frontier"})

    line = buf.getvalue().strip()
    assert line, "no log output captured"
    payload = json.loads(line)  # JSON parseable -> jq parseable
    assert payload["level"] == "INFO"
    assert payload["logger"] == "bridge.test"
    assert payload["msg"] == "hello world"
    # The ISO timestamp must be present and look like a UTC instant.
    assert "T" in payload["ts"] and payload["ts"].endswith("+00:00")
    # Structured ``extra`` fields are promoted to top-level keys.
    assert payload["lane"] == "frontier"

    # If ``jq`` is on PATH, verify it too. Skip silently otherwise so
    # this test doesn't depend on host tooling.
    if subprocess.run(["which", "jq"], capture_output=True).returncode == 0:
        proc = subprocess.run(
            ["jq", "-e", ".msg == \"hello world\" and .lane == \"frontier\""],
            input=line, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr


def test_json_formatter_routes_levels_correctly(monkeypatch):
    """``_log_line`` must promote error/warning keywords to the right level.

    This guards the keyword lists against accidental rename regressions
    in the upstream ``reconcile_*`` modules.
    """
    import bridge.main as bridge_main
    from bridge.logging_setup import configure_logging

    monkeypatch.setenv("LOG_FORMAT", "json")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    configure_logging()

    cases = [
        ("mywl:claim-failed:500", "ERROR"),
        ("mywl:claim-http-error:timeout", "ERROR"),
        ("mywl:retry-error:kaboom", "ERROR"),
        ("mywl:retry:1/3", "INFO"),
        ("mywl:created:workload-x", "INFO"),
        ("mywl:giveup:3/3", "WARNING"),
        ("mywl:exhausted:3/3", "WARNING"),
        ("mywl:unparseable:bad-yaml", "WARNING"),
    ]
    for line, expected_level in cases:
        buf.seek(0); buf.truncate(0)
        bridge_main._log_line(line)
        payload = json.loads(buf.getvalue().strip())
        assert payload["level"] == expected_level, line
        # Structured fields are populated for every record.
        assert payload["subject"] == "mywl"
        assert payload["action"]


def test_text_formatter_remains_human_readable(monkeypatch):
    """Default text mode keeps the legacy ``asctime level name: msg`` shape.

    Local operators and CI logs still get readable output without any
    configuration beyond the default ``LOG_FORMAT`` unset.
    """
    from bridge.logging_setup import configure_logging

    monkeypatch.delenv("LOG_FORMAT", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    configure_logging()
    logging.getLogger("bridge.test").info("hello")

    line = buf.getvalue().strip()
    # Not JSON, just the standard "ts LEVEL logger: msg" layout.
    assert not line.startswith("{")
    assert "INFO" in line
    assert "bridge.test" in line
    assert line.endswith("hello")


def test_configure_logging_is_idempotent(monkeypatch):
    """Re-imports (e.g. from a reloader or test) must not stack handlers."""
    from bridge.logging_setup import configure_logging

    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()
    first = list(logging.getLogger().handlers)
    configure_logging()
    second = list(logging.getLogger().handlers)
    assert len(first) == 1 and len(second) == 1, (
        "configure_logging stacked handlers: %d -> %d" % (len(first), len(second))
    )


# ---------------------------------------------------------------------------
# End-to-end: tick output is JSON
# ---------------------------------------------------------------------------


def test_run_once_results_round_trip_through_log(monkeypatch, capsys):
    """End-to-end: a result yielded by ``run_once`` is JSON after logging.

    ``run_once`` itself returns a list of strings — the per-tick
    ``print(line)`` (now ``_log_line(line)``) is the path operators see.
    This test exercises the same helper on realistic output and
    confirms every emitted line is parseable JSON, which is the
    acceptance check from the issue.
    """
    from bridge.logging_setup import configure_logging
    import bridge.main as bridge_main

    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()  # ensure JSON formatter for the run we're about to drive

    # Realistic mix of result lines, one per action category, drawn from
    # the actions ``run_once`` / ``reconcile_failures`` /
    # ``reconcile_pr_fixes`` / ``prune_workloads`` actually emit.
    sample = [
        "frontier:empty",
        "local:created:wl-x",
        "wl-y:claim-failed:500 server error",
        "wl-z:giveup:3/3 attempts",
        "wl-q:retry:2/3",
    ]
    for line in sample:
        bridge_main._log_line(line)

    out = capsys.readouterr().out
    json_lines = [
        line for line in out.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    assert len(json_lines) == len(sample), (
        f"expected {len(sample)} JSON lines, got {len(json_lines)}:\n{out!r}"
    )
    for line in json_lines:
        payload = json.loads(line)  # raises if any line is not valid JSON
        # Every record carries the standard envelope.
        assert {"ts", "level", "logger", "msg"} <= payload.keys()
        # The ``subject`` / ``action`` structured fields populated by
        # ``_log_line`` are present and non-empty.
        assert payload["subject"]
        assert payload["action"]
