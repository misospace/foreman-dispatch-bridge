"""Tests for bridge.env startup validation."""

import os
from unittest.mock import patch


def test_passes_when_required_vars_set():
    """validate_env() returns normally when DISPATCH_AGENT_TOKEN is set."""
    from bridge.env import validate_env

    with patch.dict(os.environ, {"DISPATCH_AGENT_TOKEN": "secret-token"}):
        validate_env()  # should not raise


def test_exits_when_dispatch_agent_token_missing():
    """validate_env() calls sys.exit(1) when DISPATCH_AGENT_TOKEN is absent."""
    import importlib

    import bridge.env

    with patch.dict(os.environ, {}, clear=True):
        importlib.reload(bridge.env)

        from bridge.env import validate_env

        try:
            validate_env()
            assert False, "validate_env() should have called sys.exit"
        except SystemExit as exc:
            assert exc.code == 1


def test_error_message_names_missing_var():
    """stderr output includes the missing variable name and FATAL."""
    import io
    import importlib
    import sys

    import bridge.env

    with patch.dict(os.environ, {}, clear=True):
        importlib.reload(bridge.env)

        from bridge.env import validate_env

        stderr_capture = io.StringIO()
        with patch.object(sys, "stderr", stderr_capture):
            try:
                validate_env()
            except SystemExit:
                pass

        output = stderr_capture.getvalue()
        assert "DISPATCH_AGENT_TOKEN" in output
        assert "FATAL" in output
