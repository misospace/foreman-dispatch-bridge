"""Regression test for the public API exposed by bridge/__init__.py.

Issue #36 asks the package to re-export the key public symbols so
``from bridge import <symbol>`` works without first knowing the submodule
layout. This test guards that contract.
"""

import bridge
from bridge import (
    ClaimedItem,
    build_workload,
    drain_pr_fixes,
    prune_workloads,
    reconcile_failures,
    reconcile_pr_fixes,
    run_once,
)
from bridge.main import run_once as run_once_from_main
from bridge.models import ClaimedItem as ClaimedItem_from_models
from bridge.prfix import drain_pr_fixes as drain_pr_fixes_from_prfix
from bridge.prfix import reconcile_pr_fixes as reconcile_pr_fixes_from_prfix
from bridge.prune import prune_workloads as prune_workloads_from_prune
from bridge.retry import reconcile_failures as reconcile_failures_from_retry
from bridge.workload import build_workload as build_workload_from_workload


EXPECTED_PUBLIC_API = [
    "ClaimedItem",
    "build_workload",
    "drain_pr_fixes",
    "prune_workloads",
    "reconcile_failures",
    "reconcile_pr_fixes",
    "run_once",
]


def test_all_lists_every_public_symbol():
    assert set(bridge.__all__) == set(EXPECTED_PUBLIC_API)


def test_all_names_are_actually_importable_from_bridge():
    for name in EXPECTED_PUBLIC_API:
        assert hasattr(bridge, name), f"bridge.{name} missing"


def test_reexports_share_identity_with_source_modules():
    # Re-exports must be the same object as the original module's binding,
    # not a wrapper -- otherwise ``isinstance`` checks against the public
    # name would silently disagree with checks against the submodule name.
    assert ClaimedItem is ClaimedItem_from_models
    assert run_once is run_once_from_main
    assert build_workload is build_workload_from_workload
    assert reconcile_failures is reconcile_failures_from_retry
    assert reconcile_pr_fixes is reconcile_pr_fixes_from_prfix
    assert drain_pr_fixes is drain_pr_fixes_from_prfix
    assert prune_workloads is prune_workloads_from_prune


def test_star_import_is_bounded_to_all():
    import importlib

    mod = importlib.import_module("bridge")
    star_ns = {name: getattr(mod, name) for name in mod.__all__}
    assert set(star_ns) == set(EXPECTED_PUBLIC_API)