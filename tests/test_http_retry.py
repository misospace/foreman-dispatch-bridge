"""Tests for bridge.http_retry — retry wrapper for Dispatch / GitHub HTTP calls."""

from unittest.mock import patch, MagicMock

import pytest
import requests

from bridge.http_retry import (
    _is_retryable,
    retry_request,
    http_get,
    http_post,
    redact_tokens,
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


# ── redact_tokens — token sanitisation ────────────────────────────────

class TestRedactTokens:
    def test_redacts_bearer_token(self):
        msg = "Authorization: Bearer ghp_abc123XYZ"
        assert redact_tokens(msg) == "Authorization: Bearer ***"

    def test_redacts_bearer_in_error_string(self):
        msg = "HTTPError: 401 Client Error: Unauthorized (Bearer gho_secret-token-here)"
        result = redact_tokens(msg)
        assert "gho_secret-token-here" not in result
        assert "Bearer ***" in result

    def test_redacts_basic_auth(self):
        msg = "Authorization: Basic dXNlcjpwYXNz"
        assert redact_tokens(msg) == "Authorization: Basic ***"

    def test_case_insensitive_bearer(self):
        msg = "bearer ghp_lowercase_token"
        assert redact_tokens(msg) == "bearer ***"

    def test_no_false_positives(self):
        msg = "The bearer of good news arrived"
        # "bearer" followed by a space and then a non-token word should not match
        # since the regex requires [A-Za-z0-9_\-\.]+ after "Bearer "
        result = redact_tokens(msg)
        assert "bearer" in result.lower()

    def test_empty_string(self):
        assert redact_tokens("") == ""

    def test_no_token_in_text(self):
        msg = "All systems operational"
        assert redact_tokens(msg) == msg

    def test_multiple_tokens_redacted(self):
        msg = "Bearer ghp_first and Bearer gho_second"
        result = redact_tokens(msg)
        assert "ghp_first" not in result
        assert "gho_second" not in result
        assert result.count("***") == 2

    def test_token_in_repr_exception(self):
        # Simulate repr() of an exception that contains a token
        msg = "HTTPError('401 Client Error: Unauthorized for url: https://api.example.com (Bearer ghp_leaked)')"
        result = redact_tokens(msg)
        assert "ghp_leaked" not in result
        assert "Bearer ***" in result


# ── retry_request redacts tokens from HTTPError messages ───────────────

class TestRetryRequestTokenRedaction:
    @patch("bridge.http_retry.time.sleep", return_value=None)
    def test_http_error_message_redacted(self, mock_sleep):
        """When retries are exhausted with a 5xx error, the raised HTTPError
        must not contain the raw token from the request."""

        def func():
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 503
            resp.request = MagicMock()
            resp.request.headers = {"Authorization": "Bearer ghp_secret_token_123"}
            # Make raise_for_status actually raise for 5xx
            resp.raise_for_status.side_effect = requests.HTTPError(
                "503 Server Error: Service Unavailable (Bearer ghp_secret_token_123)"
            )
            resp.raise_for_status.side_effect.response = resp
            return resp

        with pytest.raises(requests.exceptions.HTTPError) as exc_info:
            retry_request(func, retries=1, base_delay=0.01)

        error_msg = str(exc_info.value)
        assert "ghp_secret_token_123" not in error_msg
