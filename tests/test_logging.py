"""Tests for the structured JSON logging setup (issue #51)."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys

import pytest

from bridge.logging_setup import JsonFormatter, configure


@pytest.fixture
def captured_logs(monkeypatch):
    """Return a list of parsed JSON log records emitted to stdout."""
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_info_emits_single_line_json(captured_logs):
    logger = logging.getLogger("bridge.test.json.info")
    logger.info("hello")
    records = _records(captured_logs)
    assert len(records) == 1
    rec = records[0]
    assert rec["msg"] == "hello"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "bridge.test.json.info"
    # The acceptance criterion is "tick output in JSON format is parseable by jq",
    # so we explicitly verify a single-line JSON object that jq can parse.
    raw_lines = [line for line in captured_logs.getvalue().splitlines() if line]
    assert len(raw_lines) == 1
    parsed = json.loads(raw_lines[0])
    assert parsed["msg"] == "hello"


def test_extra_fields_become_top_level_keys(captured_logs):
    logger = logging.getLogger("bridge.test.json.extra")
    logger.warning(
        "feedback-lookup-failed",
        extra={"issue_id": "abc-1", "fallback": "no-feedback"},
    )
    [record] = _records(captured_logs)
    assert record["level"] == "WARNING"
    assert record["issue_id"] == "abc-1"
    assert record["fallback"] == "no-feedback"


def test_warning_level_round_trips(captured_logs):
    logger = logging.getLogger("bridge.test.json.warning")
    logger.warning("issue-id-lookup-failed")
    [record] = _records(captured_logs)
    assert record["level"] == "WARNING"


def test_error_level_round_trips(captured_logs):
    logger = logging.getLogger("bridge.test.json.error")
    logger.error("prfix-check-runs-error")
    [record] = _records(captured_logs)
    assert record["level"] == "ERROR"


def test_timestamp_is_iso8601_utc(captured_logs):
    logger = logging.getLogger("bridge.test.json.ts")
    logger.info("ts-check")
    [record] = _records(captured_logs)
    ts = record["ts"]
    assert ts.endswith("Z")
    # 2026-08-04T12:34:56.789Z -> 24 chars
    assert len(ts) == 24
    # Round-trip via datetime
    from datetime import datetime
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    assert parsed.tzinfo is None  # 'Z' marker means UTC, datetime treats as naive


def test_stdlib_attributes_are_not_leaked(captured_logs):
    logger = logging.getLogger("bridge.test.json.no_leak")
    logger.info("no-leak")
    [record] = _records(captured_logs)
    # These are stdlib LogRecord attributes that should never appear in our
    # JSON output. ``msg`` is intentionally promoted to a top-level key by
    # the formatter (it carries the user-facing message), so it's allowed.
    for forbidden in ("pathname", "filename", "module", "lineno", "args",
                      "levelname", "levelno", "funcName", "thread",
                      "process", "exc_info"):
        assert forbidden not in record, f"{forbidden!r} leaked into JSON payload"


def test_jq_can_parse_emit(captured_logs):
    """Acceptance criterion: tick output in JSON format is parseable by jq."""
    logger = logging.getLogger("bridge.test.json.jq")
    logger.info("tick-complete", extra={"lane": "local", "claimed": 1})

    raw = captured_logs.getvalue()
    # Structural requirement: each line is a valid JSON object.
    lines = [line for line in raw.splitlines() if line]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["msg"] == "tick-complete"
    assert parsed["lane"] == "local"
    assert parsed["claimed"] == 1

    # On a runner with jq available, prove the round-trip explicitly. This
    # is the acceptance criterion from issue #51.
    jq = subprocess.run(
        ["jq", "-r", ".msg"],
        input=raw,
        capture_output=True,
        text=True,
    )
    if jq.returncode == 0:
        assert jq.stdout.strip() == "tick-complete"


def test_configure_is_idempotent():
    """Calling configure twice must not duplicate handlers."""
    configure(force=True)
    configure(force=True)
    configure()
    configure()
    handler_signatures = [id(h) for h in logging.getLogger().handlers]
    assert len(handler_signatures) == len(set(handler_signatures))


def test_plain_format_is_human_readable(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "plain")
    configure(force=True)
    formatter = logging.getLogger().handlers[0].formatter
    # The plain formatter is NOT a JsonFormatter.
    assert not isinstance(formatter, JsonFormatter)


def test_no_print_in_bridge_source():
    """All bridge/* output should flow through the logging framework."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "bridge"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            # Skip docstring/comment lines.
            if stripped.startswith("#"):
                continue
            if "print(" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "print() found in bridge/ — issue #51 requires structured logging:\n"
        + "\n".join(offenders)
    )


def test_module_is_importable():
    """Smoke: bridge.logging_setup imports cleanly under the project's pyver."""
    import bridge.logging_setup  # noqa: F401