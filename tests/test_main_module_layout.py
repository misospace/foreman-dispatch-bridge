"""The `if __name__ == "__main__"` guard must be the last top-level statement.

Running the module as a script executes its body top to bottom, so any def or
class *after* the guard does not exist when `_real_main()` is called. That made
every definition below the guard unreachable in the cluster while still being
importable — and therefore testable — which is how a NameError reached
production (`_list_terminal_candidates` referenced from `_real_main`).
"""

from __future__ import annotations

import ast
import pathlib


def _module_ast() -> ast.Module:
    src = pathlib.Path("bridge/main.py").read_text()
    return ast.parse(src)


def _entrypoint_guard(tree: ast.Module) -> ast.If:
    for node in tree.body:
        if isinstance(node, ast.If) and ast.dump(node.test).find("__main__") != -1:
            return node
    raise AssertionError('no `if __name__ == "__main__"` guard found')


def test_entrypoint_guard_is_the_last_top_level_statement() -> None:
    tree = _module_ast()
    guard = _entrypoint_guard(tree)
    after = [n for n in tree.body if n.lineno > guard.lineno]
    names = [getattr(n, "name", type(n).__name__) for n in after]
    assert after == [], (
        "definitions after the entrypoint guard are unreachable when the module "
        f"runs as a script: {names}"
    )


def test_real_main_only_calls_names_defined_above_the_guard() -> None:
    """Every same-module name `_real_main` calls must be defined above the guard.

    Considers only names defined in this module; imported names are always bound
    at import time and cannot be affected by source order.
    """
    tree = _module_ast()
    guard = _entrypoint_guard(tree)
    defs = {
        n.name: n.lineno
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    real_main = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_real_main"
    )
    called = {
        node.func.id
        for node in ast.walk(real_main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    late = sorted(c for c in called if c in defs and defs[c] > guard.lineno)
    assert late == [], (
        f"_real_main calls module-level names defined after the guard: {late}"
    )
