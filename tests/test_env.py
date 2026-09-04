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


# --- Documented defaults must match the ones the bridge actually applies ---


def test_verify_enabled_documented_default_matches_the_applied_default():
    """OPTIONAL_VARS documents defaults; _real_main decides them. Two sources of
    truth, and only one of them is read at runtime.

    VERIFY_ENABLED is opt-in: the deployment relies on repository CI and has no
    verifier Agent, so a silent flip back to on would emit verify steps whose
    agentRef does not resolve. _real_main is `pragma: no cover` thin wiring, so
    the applied default is asserted from source rather than by calling it.
    """
    import re
    from pathlib import Path

    from bridge.env import OPTIONAL_VARS
    from bridge.main import _parse_bool_env

    documented = _parse_bool_env(OPTIONAL_VARS["VERIFY_ENABLED"])
    assert documented is False, "VERIFY_ENABLED must be documented as off by default"

    source = (Path(__file__).resolve().parent.parent / "bridge" / "main.py").read_text()
    m = re.search(
        r'VERIFY_ENABLED"\s*,\s*""\s*\)\s*,\s*default=(True|False)\s*\)', source
    )
    assert m, "could not find the VERIFY_ENABLED default in bridge/main.py"
    applied = m.group(1) == "True"
    assert applied == documented, (
        f"bridge/main.py applies default={m.group(1)} while OPTIONAL_VARS documents "
        f"{OPTIONAL_VARS['VERIFY_ENABLED']!r}"
    )
