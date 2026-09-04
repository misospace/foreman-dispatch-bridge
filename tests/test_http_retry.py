"""Tests for bridge.http_retry — retry wrapper for Dispatch / GitHub HTTP calls."""

from unittest.mock import patch, MagicMock

import pytest
import requests

from bridge.http_retry import (
    _is_retryable,
    retry_request,
    http_get,
    http_post,
    _retry_k8s_request,
    _is_k8s_retryable,
)


# ── _is_retryable helpers ──────────────────────────────────────────────

class TestIsRetryable:
    def test_timeout_is_retryable(self):
        assert _is_retryable(requests.exceptions.Timeout()) is True

    def test_connection_error_is_retryable(self):
        assert _is_retryable(requests.exceptions.ConnectionError()) is True

    def test_request_exception_is_retryable(self):
        assert _is_retryable(requests.exceptions.RequestException()) is True

    def test_429_response_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 429
        assert _is_retryable(resp) is True

    def test_500_response_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 500
        assert _is_retryable(resp) is True

    def test_502_response_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 502
        assert _is_retryable(resp) is True

    def test_503_response_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 503
        assert _is_retryable(resp) is True

    def test_504_response_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 504
        assert _is_retryable(resp) is True

    def test_200_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        assert _is_retryable(resp) is False

    def test_400_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400
        assert _is_retryable(resp) is False

    def test_401_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        assert _is_retryable(resp) is False

    def test_403_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 403
        assert _is_retryable(resp) is False

    def test_404_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        assert _is_retryable(resp) is False

    def test_409_response_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 409
        assert _is_retryable(resp) is False

    def test_http_error_with_503_is_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 503
        exc = requests.exceptions.HTTPError(response=resp)
        assert _is_retryable(exc) is True

    def test_http_error_with_400_not_retryable(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400
        exc = requests.exceptions.HTTPError(response=resp)
        assert _is_retryable(exc) is False

    def test_http_error_without_response_not_retryable(self):
        exc = requests.exceptions.HTTPError("manual error")
        assert _is_retryable(exc) is False

    def test_unknown_object_not_retryable(self):
        assert _is_retryable("not a response or exception") is False


# ── retry_request behaviour ────────────────────────────────────────────

class TestRetryRequest:
    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_success_first_attempt(self, mock_sleep):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            return resp

        result = retry_request(func, retries=2, base_delay=0.01)
        assert result is resp
        assert call_count == 1
        mock_sleep.assert_not_called()

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_retries_on_503(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                resp = MagicMock(spec=requests.Response)
                resp.status_code = 503
                return resp
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 200
            return resp

        result = retry_request(func, retries=3, base_delay=0.01)
        assert result.status_code == 200
        assert call_count == 3
        assert mock_sleep.call_count == 2

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_retries_on_timeout(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.Timeout()
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 200
            return resp

        result = retry_request(func, retries=2, base_delay=0.01)
        assert result.status_code == 200
        assert call_count == 2

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_no_retry_on_400(self, mock_sleep):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400

        def func():
            return resp

        result = retry_request(func, retries=2, base_delay=0.01)
        assert result is resp
        mock_sleep.assert_not_called()

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_no_retry_on_404(self, mock_sleep):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404

        def func():
            return resp

        result = retry_request(func, retries=2, base_delay=0.01)
        assert result is resp
        mock_sleep.assert_not_called()

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_no_retry_on_409(self, mock_sleep):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 409

        def func():
            return resp

        result = retry_request(func, retries=2, base_delay=0.01)
        assert result is resp
        mock_sleep.assert_not_called()

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_raises_after_exhausted_retries(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 503
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("503")
            return resp

        with pytest.raises(requests.exceptions.HTTPError):
            retry_request(func, retries=2, base_delay=0.01)
        assert call_count == 3  # initial + 2 retries

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_raises_transient_exception_after_exhausted(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            raise requests.exceptions.Timeout()

        with pytest.raises(requests.exceptions.Timeout):
            retry_request(func, retries=2, base_delay=0.01)
        assert call_count == 3

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_no_retry_on_non_transient_exception(self, mock_sleep):
        def func():
            raise requests.exceptions.HTTPError("400 Bad Request")

        with pytest.raises(requests.exceptions.HTTPError):
            retry_request(func, retries=2, base_delay=0.01)
        mock_sleep.assert_not_called()

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_exponential_backoff(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 503
            if call_count <= 3:
                return resp
            resp.status_code = 200
            return resp

        retry_request(func, retries=4, base_delay=0.1, backoff_factor=2.0)
        # Delays should be 0.1, 0.2, 0.4
        calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(calls) == 3
        assert pytest.approx(calls[0]) == 0.1
        assert pytest.approx(calls[1]) == 0.2
        assert pytest.approx(calls[2]) == 0.4

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_max_delay_cap(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 503
            if call_count <= 5:
                return resp
            resp.status_code = 200
            return resp

        retry_request(func, retries=5, base_delay=1.0, backoff_factor=2.0, max_delay=2.0)
        calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert all(d <= 2.0 for d in calls)

    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_429_honours_retry_after(self, mock_sleep):
        call_count = 0

        def func():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 429
            resp.headers = {"Retry-After": "5"}
            if call_count <= 1:
                return resp
            resp.status_code = 200
            resp.headers = {}
            return resp

        retry_request(func, retries=2, base_delay=0.01)
        assert mock_sleep.call_args_list[0].args[0] == 5.0


# ── http_get / http_post convenience wrappers ──────────────────────────

class TestHttpGet:
    @patch("bridge.http_retry.requests.get")
    def test_success(self, mock_get):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        mock_get.return_value = resp
        result = http_get("http://example.com", headers={"X": "1"})
        assert result is resp
        mock_get.assert_called_once_with(
            "http://example.com", headers={"X": "1"}, timeout=20,
        )

    @patch("bridge.http_retry.requests.get")
    def test_default_headers(self, mock_get):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        mock_get.return_value = resp
        http_get("http://example.com")
        mock_get.assert_called_once_with(
            "http://example.com", headers={}, timeout=20,
        )


class TestHttpPost:
    @patch("bridge.http_retry.requests.post")
    def test_success(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        mock_post.return_value = resp
        result = http_post(
            "http://example.com", headers={"X": "1"}, json={"key": "val"},
        )
        assert result is resp
        mock_post.assert_called_once_with(
            "http://example.com", headers={"X": "1"}, json={"key": "val"}, timeout=30,
        )

    @patch("bridge.http_retry.requests.post")
    def test_default_headers(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        mock_post.return_value = resp
        http_post("http://example.com", json={"key": "val"})
        mock_post.assert_called_once_with(
            "http://example.com", headers={}, json={"key": "val"}, timeout=30,
        )

# ── shared token redaction (issue #177) ──────────────────────────────────────


def test_redact_token_lives_in_http_retry_and_is_shared():
    """_TOKEN_RE / _redact_token are defined once in http_retry and shared."""
    from bridge import claim, http_retry, main

    assert http_retry._redact_token(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret"
    ) == "Authorization: Bearer ***"
    assert "eyJhbGciOiJIUzI1NiJ9.secret" not in http_retry._redact_token(
        "Bearer eyJhbGciOiJIUzI1NiJ9.secret"
    )
    # Both modules must use the exact same shared objects, not local copies.
    assert main._redact_token is http_retry._redact_token
    assert claim._redact_token is http_retry._redact_token


# ── kubernetes client retry wrapper (issue #257) ────────────────────────────


def _k8s_exc(status: int):
    """Build a kubernetes.client.ApiException with the given status."""
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status, reason="boom")


def test_k8s_retry_503_eventually_succeeds():
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _k8s_exc(503)
        return {"items": []}

    with patch("time.sleep") as mock_sleep:
        result = _retry_k8s_request(call, base_delay=0.01)

    assert result == {"items": []}
    assert calls["n"] == 2
    mock_sleep.assert_called_once()


def test_k8s_retry_504_eventually_succeeds():
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _k8s_exc(504)
        return "ok"

    with patch("time.sleep"):
        assert _retry_k8s_request(call, base_delay=0.01) == "ok"
    assert calls["n"] == 3


def test_k8s_retry_429_eventually_succeeds():
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _k8s_exc(429)
        return {}

    with patch("time.sleep"):
        assert _retry_k8s_request(call, base_delay=0.01) == {}
    assert calls["n"] == 2


def test_k8s_retry_4xx_raised_immediately():
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _k8s_exc(404)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(Exception):
            _retry_k8s_request(call, base_delay=0.01)

    assert calls["n"] == 1
    mock_sleep.assert_not_called()


def test_k8s_retry_403_never_retried():
    """RBAC Forbidden is never retried — it just amplifies noise."""
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _k8s_exc(403)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(Exception):
            _retry_k8s_request(call, base_delay=0.01)

    assert calls["n"] == 1
    mock_sleep.assert_not_called()


def test_k8s_retry_exhausted_raises_original():
    """After exhausting retries the original ApiException propagates."""
    from kubernetes.client.exceptions import ApiException

    sentinel = _k8s_exc(503)
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise sentinel

    with patch("time.sleep"):
        with pytest.raises(ApiException) as excinfo:
            _retry_k8s_request(call, retries=2, base_delay=0.01)

    assert excinfo.value is sentinel
    assert calls["n"] == 3  # initial + 2 retries


def test_k8s_retry_non_api_exception_propagates_immediately():
    class NotAnApiException(Exception):
        pass

    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise NotAnApiException("nope")

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(NotAnApiException):
            _retry_k8s_request(call, base_delay=0.01)

    assert calls["n"] == 1
    mock_sleep.assert_not_called()


def test_is_k8s_retryable_classifies_status_codes():
    assert _is_k8s_retryable(_k8s_exc(429))
    assert _is_k8s_retryable(_k8s_exc(500))
    assert _is_k8s_retryable(_k8s_exc(502))
    assert _is_k8s_retryable(_k8s_exc(503))
    assert _is_k8s_retryable(_k8s_exc(504))
    assert not _is_k8s_retryable(_k8s_exc(400))
    assert not _is_k8s_retryable(_k8s_exc(403))
    assert not _is_k8s_retryable(_k8s_exc(404))
    assert not _is_k8s_retryable(_k8s_exc(409))
    assert not _is_k8s_retryable(RuntimeError("not an api exception"))
