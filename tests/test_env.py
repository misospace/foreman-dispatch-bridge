"""Tests for bridge.env — startup environment validation."""

import os
import sys
from unittest.mock import patch

import pytest


class TestValidateEnv:
    """validate_env() must exit 1 when required vars are missing."""

    def test_passes_when_required_vars_set(self):
        with patch.dict(os.environ, {"DISPATCH_AGENT_TOKEN": "secret"}, clear=False):
            from bridge.env import validate_env

            # Must not raise or exit
            validate_env()

    def test_exits_when_dispatch_agent_token_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "DISPATCH_AGENT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            from bridge.env import validate_env

            with pytest.raises(SystemExit) as exc_info:
                validate_env()
            assert exc_info.value.code == 1

    def test_error_message_names_missing_var(self, capsys):
        env = {k: v for k, v in os.environ.items() if k != "DISPATCH_AGENT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            # Force reimport so the module sees the patched env
            import importlib

            import bridge.env

            importlib.reload(bridge.env)

            with pytest.raises(SystemExit):
                bridge.env.validate_env()

        stderr = capsys.readouterr().err
        assert "DISPATCH_AGENT_TOKEN" in stderr
        assert "FATAL" in stderr
