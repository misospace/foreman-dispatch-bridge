"""Regression tests for issue #37: pytest.ini warning enforcement.

These tests verify that the pytest configuration treats warnings as errors,
requires a minimum pytest version, and enforces strict marker/config usage.
"""
import warnings

import pytest


def test_filterwarnings_is_set_to_error(pytestconfig):
    """pytest.ini must promote warnings to errors (filterwarnings = error)."""
    # The `filterwarnings` ini option yields a list of filter spec strings
    # (e.g. ["error"]). With `filterwarnings = error`, every emitted
    # warning becomes a test failure.
    spec = pytestconfig.getini("filterwarnings")
    assert spec, "pytest.ini is missing the required `filterwarnings` setting"
    actions = [entry.split(":", 1)[0].strip() for entry in spec]
    assert "error" in actions, (
        f"Expected an `error` action in filterwarnings; got: {spec!r}"
    )


def test_minversion_is_set(pytestconfig):
    """pytest.ini must declare a minimum pytest version."""
    minversion = pytestconfig.getini("minversion")
    assert minversion, "pytest.ini is missing the required `minversion` setting"
    # Sanity-check the value is parseable as a version tuple.
    parts = minversion.split(".")
    assert all(p.isdigit() for p in parts), f"minversion not a version: {minversion!r}"


def test_addopts_enforces_strict(pytestconfig):
    """`addopts` must include --strict-markers and --strict-config."""
    addopts = pytestconfig.getini("addopts")
    assert "--strict-markers" in addopts, (
        f"--strict-markers missing from addopts: {addopts!r}"
    )
    assert "--strict-config" in addopts, (
        f"--strict-config missing from addopts: {addopts!r}"
    )


def test_deprecation_warning_is_promoted_to_error():
    """A DeprecationWarning emitted at test time must fail the test."""
    with pytest.raises(DeprecationWarning):
        warnings.warn("regression: warnings should be errors", DeprecationWarning)