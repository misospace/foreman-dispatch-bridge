"""Retry wrapper for HTTP calls to Dispatch and GitHub APIs.

Provides exponential-backoff retry on transient failures (timeouts, 429, 5xx)
while passing through semantic client errors (4xx except 429) immediately.
"""

import re
import time
import logging

import requests

logger = logging.getLogger(__name__)

# Status codes that indicate a transient condition worth retrying.
_RETRYABLE_STATUS_CODES = frozenset((429, 500, 502, 503, 504))


# ── Token redaction helpers ────────────────────────────────────────────

# Matches common token patterns: Bearer tokens, raw token strings, and
# Authorization header values.  The pattern is intentionally broad to catch
# leaked secrets in error messages and tracebacks.
_TOKEN_REDACTION_PATTERNS = [
    # "Authorization: Bearer <token>" or "Bearer <token>"
    re.compile(r"(Bearer )[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    # "Authorization: Basic <encoded>"
    re.compile(r"(Basic )[A-Za-z0-9+/=]+", re.IGNORECASE),
]

_REDACTED = "***"


def redact_tokens(text: str) -> str:
    """Return *text* with any embedded tokens replaced by ``***``.

    This is used to sanitise error messages, tracebacks, and log lines
    before they reach the operator-visible output so that secrets never
    leak through exception chains.
    """
    result = text
    for pattern in _TOKEN_REDACTION_PATTERNS:
        result = pattern.sub(r"\1" + _REDACTED, result)
    return result


def _is_retryable(exc_or_response):
    """Return True when *exc_or_response* represents a transient failure."""
    if isinstance(exc_or_response, requests.exceptions.Timeout):
        return True
    if isinstance(exc_or_response, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc_or_response, requests.exceptions.HTTPError):
        # HTTPError wraps a Response — check the status code.
        resp = getattr(exc_or_response, "response", None)
        if resp is not None:
            return resp.status_code in _RETRYABLE_STATUS_CODES
        # No response attached (e.g. raised manually) — not retryable.
        return False
    if isinstance(exc_or_response, requests.exceptions.RequestException):
        # Other request-level errors (DNS, SSL, etc.) are transient.
        return True
    if isinstance(exc_or_response, requests.Response):
        return exc_or_response.status_code in _RETRYABLE_STATUS_CODES
    return False


def retry_request(func, *, retries=2, base_delay=0.5, max_delay=16.0, backoff_factor=2.0):
    """Call *func* with exponential backoff on transient failures.

    Parameters
    ----------
    func:
        Zero-argument callable that returns a ``requests.Response``.
    retries:
        Maximum number of retry attempts (not counting the initial call).
    base_delay, max_delay, backoff_factor:
        Exponential backoff parameters passed to :func:`backoff`.

    Returns
    -------
    requests.Response
        The response from the last attempt (may still be an error status).

    Raises
    ------
    requests.exceptions.RequestException
        If all attempts fail with a transient error.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = func()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == retries:
                raise
            delay = min(base_delay * (backoff_factor ** attempt), max_delay)
            logger.warning(
                "http-retry: attempt %d failed (%s), retrying in %.1fs",
                attempt + 1, redact_tokens(str(exc)), delay,
            )
            time.sleep(delay)
            continue

        # Got a response — check if it's a retryable status code.
        if _is_retryable(resp):
            # For 429 honour Retry-After header when present.
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), max_delay)
                    except ValueError:
                        delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                else:
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
            else:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)

            if attempt == retries:
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    raise requests.HTTPError(
                        redact_tokens(str(exc)), response=exc.response
                    ) from None
            logger.warning(
                "http-retry: attempt %d returned %d, retrying in %.1fs",
                attempt + 1, resp.status_code, delay,
            )
            time.sleep(delay)
            continue

        # Non-retryable response — return immediately.
        return resp

    # Should not reach here, but raise the last transient exception if we do.
    if last_exc is not None:
        raise last_exc
    raise requests.exceptions.RequestException("unexpected retry loop exit")


def http_get(url, headers=None, timeout=20, **retry_kwargs):
    """GET with automatic retry on transient failures."""
    headers = headers or {}

    def _call():
        return requests.get(url, headers=headers, timeout=timeout)

    return retry_request(_call, **retry_kwargs)


def http_post(url, headers=None, json=None, timeout=30, **retry_kwargs):
    """POST with automatic retry on transient failures."""
    headers = headers or {}

    def _call():
        return requests.post(url, headers=headers, json=json, timeout=timeout)

    return retry_request(_call, **retry_kwargs)
