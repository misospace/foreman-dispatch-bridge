"""Structured logging configuration for the foreman-dispatch-bridge.

The bridge historically emitted operational output through bare ``print()``
calls, which made it impossible for downstream tooling to parse a tick's
output reliably. This module replaces that ad-hoc path with a standard
``logging`` configuration that can emit either human-readable text (the
historical default) or one-JSON-object-per-line output (parseable by
``jq`` and friends) selected via the ``LOG_FORMAT`` env var.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict


# Standard :class:`logging.LogRecord` attributes we never want to leak into
# the structured payload: they are surfaced via the explicit keys below, or
# are implementation noise that does not belong in operator-facing logs.
_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Render :class:`logging.LogRecord` instances as single-line JSON.

    Standard keys: ``ts`` (ISO-8601 UTC), ``level``, ``logger``, ``msg``.
    Any extra attribute attached to the record via
    ``logger.info(..., extra={...})`` is included under its own key, so
    callers can attach structured fields such as ``lane``, ``status``, or
    ``workload`` without re-parsing the message string.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - stdlib name
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        # ``default=str`` lets non-JSON-serializable extras (e.g. ``Enum``)
        # fall through as their string form rather than crashing the tick.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging() -> None:
    """Configure the root logger according to ``LOG_FORMAT`` / ``LOG_LEVEL``.

    Idempotent: replaces any previously installed handler. Safe to call
    from tests to reconfigure output capture between cases.

    Environment variables:

    * ``LOG_FORMAT`` -- ``json`` for structured output, ``text`` (default)
      for the historical human-readable format.
    * ``LOG_LEVEL`` -- standard level name (``DEBUG``, ``INFO``, ``WARNING``,
      ``ERROR``). Defaults to ``INFO``.
    """
    fmt = os.environ.get("LOG_FORMAT", "text").strip().lower()
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))

    root = logging.getLogger()
    # ``clear()`` keeps the configuration idempotent; a re-import of
    # ``bridge.main`` during a test suite must not stack handlers.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


__all__ = ["JsonFormatter", "configure_logging"]
