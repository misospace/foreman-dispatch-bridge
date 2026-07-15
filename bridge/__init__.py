"""Public surface of the foreman-dispatch-bridge package.

Re-exports the key symbols so consumers (and IDE auto-complete) can
``from bridge import <symbol>`` without needing to know which submodule owns
them. Existing ``from bridge.<submodule> import ...`` imports keep working
because every name is sourced from its original module.
"""

from bridge.main import run_once
from bridge.models import ClaimedItem
from bridge.prfix import drain_pr_fixes, reconcile_pr_fixes
from bridge.prune import prune_workloads
from bridge.retry import reconcile_failures
from bridge.workload import build_workload

__all__ = [
    "ClaimedItem",
    "build_workload",
    "drain_pr_fixes",
    "prune_workloads",
    "reconcile_failures",
    "reconcile_pr_fixes",
    "run_once",
]