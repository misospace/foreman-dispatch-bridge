"""Structured JSON logging for the foreman-dispatch bridge.

The bridge runs inside a Kubernetes pod where `kubectl logs` only sees raw
text; structured JSON output lets operators ingest tick events into a log
pipeline (Loki, Elasticsearch, Cloud Logging, etc.) and filter on fields
without regex hacking.

Output format (one JSON object per line):

    {"ts": "2026-08-04T12:34:56.789Z", "level": "INFO", "logger":
    "bridge.main", "msg": "tick-complete", ...extra fields...}

`extra` fields can be passed via ``logger.info("tick-complete",
extra={"foo": "bar"})`` and will appear as top-level keys in the JSON object.

Configuration is environment-driven:

* ``LOG_FORMAT``  — ``json`` (default) or ``plain`` (human-readable for
  local dev). JSON is the only format that satisfies the issue's
  acceptance criterion that tick output must be parseable by ``jq``.
* ``LOG_LEVEL``   — ``INFO`` (default), ``WARNING``, ``ERROR``, ``DEBUG``.

Call :func:`configure` exactly once at process start (from ``main``).
Subsequent calls are no-ops so importing the module from tests is safe.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Mapping

_CONFIGURED = False

_DEFAULT_LEVEL = "INFO"
_DEFAULT_FORMAT = "json"

# Map stdlib log levels to structured severity strings.
_LEVEL_TO_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    The object always contains ``ts``, ``level``, ``logger``, ``msg``.
    Anything passed via ``extra={"...": ...}`` is merged into the top
    level so consumers can filter on it directly.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso_timestamp(record.created),
            "level": _LEVEL_TO_SEVERITY.get(record.levelno, "INFO"),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Promote stdlib "extra" fields to top-level keys. Skip internal
        # LogRecord attributes so we don't leak ``args``/``pathname``/etc.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS:
                continue
            payload[key] = _coerce(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError):
            # Fallback: stringify any non-serialisable value so we still
            # produce a valid JSON object even if a caller passes weird
            # extra data.
            safe = {
                k: (v if _is_json_safe(v) else repr(v))
                for k, v in payload.items()
            }
            return json.dumps(safe, separators=(",", ":"))


_RESERVED_RECORD_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "taskName",
})


def _iso_timestamp(created_epoch: float) -> str:
    """Format a unix timestamp as an ISO-8601 UTC string with milliseconds.

    Always uses ``Z`` rather than ``+00:00`` because most log shippers
    treat ``Z`` as the canonical UTC marker.
    """
    seconds = int(created_epoch)
    millis = int(round((created_epoch - seconds) * 1000))
    # Normalise rounding edge cases (e.g. 999.9999ms -> 1000ms).
    if millis >= 1000:
        seconds += 1
        millis -= 1000
    time_struct = time.gmtime(seconds)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time_struct)
    return f"{base}.{millis:03d}Z"


def _coerce(value: Any) -> Any:
    """Coerce non-JSON-native values into something ``json.dumps`` accepts."""
    if isinstance(value, Mapping):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    return value


def _is_json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _resolve_level() -> int:
    name = os.environ.get("LOG_LEVEL", _DEFAULT_LEVEL).upper()
    return getattr(logging, name, logging.INFO)


def _resolve_format() -> str:
    return os.environ.get("LOG_FORMAT", _DEFAULT_FORMAT).lower()


def configure(*, force: bool = False) -> logging.Logger:
    """Configure root logging for the bridge.

    Idempotent; the ``force`` flag is for tests that want to re-apply the
    configuration (e.g. after monkeypatching env vars).
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return logging.getLogger("bridge")

    level = _resolve_level()
    fmt_name = _resolve_format()

    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt_name == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )

    root = logging.getLogger()
    # Replace any previously installed handlers so we don't double-log.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # The HTTP retry module is chatty at INFO; leave it at INFO but
    # callers can dial it up via LOG_LEVEL=DEBUG.
    _CONFIGURED = True
    return logging.getLogger("bridge")