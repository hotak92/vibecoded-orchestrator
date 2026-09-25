# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py's POST-VENV phase: every in-process callable is stdlib-only or
carries a working fallback — DERIVED from ``main()``'s AST.

Why this gate exists on top of ``test_v0296_install_pre_venv_is_stdlib_only``:

* the v0296 gate pins the PRE-venv callables and sweeps install.py's
  function-local imports against MODULE-SCOPE chains only;
* install.py never re-execs into the venv it creates at ``_create_venv``, so
  the process for the WHOLE run is whatever interpreter launched it — on a
  fresh clone (the only state a first install is ever in) the system Python
  with no third-party packages at all;
* v0.2.97 shipped the hole: ``_migrate_install_dotenv_openai_key`` imported
  ``vco_lib.openai_key`` (a clean module scope, invisible to the module-scope
  walker) and called ``migrate_dotenv_openai_key``, whose BODY did
  ``from vco_lib import agent_secrets`` → whose module scope imports
  ``vco_lib.project_config`` → ``import requests``. install-smoke went red on
  all platforms at step 9 with ``ModuleNotFoundError: No module named
  'requests'`` while every gate was green.

So the boundary here is the ``_create_venv`` CALL inside ``main()`` (found,
not hard-coded — if it moves, this test says so), and the question is the
sharper one ``tests.common.import_chain.CallImportIndex`` answers: when this
callable RUNS, which venv packages' imports actually execute — through called
function bodies, same-module calls, and the module scopes of every
``vco_lib`` module the chain imports.

A flagged callable must either stop importing the chain in-process (the
v0.2.97 fix for the migration: run it as a venv subprocess, tier A) or catch
the ImportError and degrade — the walker already treats an import inside a
``try`` whose handler catches ImportError/ModuleNotFoundError/Exception/bare
as the sanctioned fallback shape, so ``ALLOWED`` below stays EMPTY: a walker
exception is a judgement about the RUNTIME fallback, and that is pinned by a
behavioural test instead (``test_v0297_openai_env_migration_venv_subprocess``).

The walker's own regression tests live at the bottom of this file: a gate
that silently resolves nothing proves nothing.
"""

from __future__ import annotations

import ast

import pytest

from tests.common.import_chain import (
    REPO_ROOT as REPO,
    CallImportIndex,
    requirements_import_roots,
    third_party_reachable_from,
    third_party_reachable_from_call,
)

#: The packages the install's dependency step puts in the venv — the exact
#: set whose absence on the system interpreter defines the failure class.
#: Derived from requirements.txt (see the v0296 gate for the hand-list
#: caution this inherits).
POST_VENV_PACKAGES = requirements_import_roots()

#: callable -> why its in-process third-party reach is survivable. EMPTY and
#: meant to stay that way: guarded imports are recognised structurally by the
#: walker (see the module docstring), and every other shape is a bug.
ALLOWED: dict[str, str] = {}


def _main_post_venv_surface() -> tuple[CallImportIndex, list[ast.stmt]]:
    """install.py's index plus ``main()``'s statements AFTER the ``_create_venv``
    call — the derived boundary, in one place so every test below agrees."""
    idx = CallImportIndex(REPO / "install.py", name="install.py")
    main_node = idx.toplevel.get("main")
    assert isinstance(main_node, ast.FunctionDef), "main() is gone from install.py"
    for i, stmt in enumerate(main_node.body):
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "_create_venv"
            ):
                return idx, main_node.body[i + 1 :]
    raise AssertionError(
        "main() never calls _create_venv — the boundary this gate is derived "
        "from has moved; re-derive it rather than pinning a step number"
    )


def _post_venv_callees(idx: CallImportIndex, post: list[ast.stmt]) -> set[str]:
    """Toplevel install.py functions main() calls directly after the venv."""
    return {
        sub.func.id
        for stmt in post
        for sub in ast.walk(stmt)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and isinstance(
            idx.toplevel.get(sub.func.id), (ast.FunctionDef, ast.AsyncFunctionDef)
        )
    }


def test_the_boundary_is_derived_and_the_surface_is_real():
    """The walk must not prove nothing: post-venv statements exist and include
    the step-9 ``.env`` entry whose migration shipped the v0.2.97 defect."""
    idx, post = _main_post_venv_surface()
    assert post, "nothing runs after _create_venv in main()?"
    callees = _post_venv_callees(idx, post)
    assert "_write_env_config" in callees, (
        f"step 9 (_write_env_config) is not a direct post-venv callee of "
        f"main() — the gate's exemplar path is gone; callees: {sorted(callees)}"
    )


def test_no_post_venv_callee_reaches_a_venv_package_in_process():
    """Every function main() calls IN-PROCESS after the venv exists must be
    stdlib-only transitively (guarded fallbacks excepted).

    This is the test that was red on HEAD's
    ``_migrate_install_dotenv_openai_key``: reached through
    ``main → _write_env_config → _migrate_install_dotenv_openai_key →
    vco_lib.openai_key.migrate_dotenv_openai_key → agent_secrets →
    project_config → requests``, all in-process, all unguarded.
    """
    idx, post = _main_post_venv_surface()
    callees = _post_venv_callees(idx, post)
    assert len(callees) > 10, (
        f"the walk collapsed ({len(callees)} callees) — it proves nothing"
    )
    violations = []
    for name in sorted(callees):
        if name in ALLOWED:
            continue
        for package, via in sorted(idx.third_party_from_call(name).items()):
            if package in POST_VENV_PACKAGES:
                violations.append(
                    f"main() -> {name}() reaches {package!r} in-process ({via})"
                )
    assert not violations, (
        "after _create_venv this process is STILL the interpreter that "
        "launched install.py (it never re-execs into the venv), so an "
        "in-process import of a venv package must be guarded by a working "
        "fallback or moved out of process (tier A: `<venv-python> -m "
        "vco_lib.<module>`). Either fix the callable, or — only if the "
        "fallback is a reasoned runtime degradation — pin it behaviourally "
        "and list it in ALLOWED with the reason:\n  "
        + "\n  ".join(violations)
    )


def test_no_post_venv_inline_import_in_main_reaches_a_venv_package():
    """main()'s OWN body after the boundary, not just its callees'.

    ``main()`` imports several ``vco_lib`` modules inline (containers, the
    step-9 reprojection); those imports run in-process on the launching
    interpreter, so both the target module's SCOPE and the symbols main then
    calls are checked — the scope because importing executes it, the symbols
    because the module-scope question alone is exactly what missed the v0296
    hole one level down.
    """
    idx, post = _main_post_venv_surface()
    violations: list[str] = []
    for stmt in post:
        for sub in ast.walk(stmt):
            if not isinstance(sub, (ast.Import, ast.ImportFrom)):
                continue
            guarded = any(lo <= sub.lineno <= hi for lo, hi in idx.guarded)
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if module.startswith("vco_lib"):
                    if not guarded:
                        short = module.split(".", 1)[1] if "." in module else ""
                        if short:
                            hits = {
                                p: v
                                for p, v in third_party_reachable_from(short).items()
                                if p in POST_VENV_PACKAGES
                            }
                            if hits:
                                violations.append(
                                    f"main() imports {module} at line {sub.lineno} "
                                    f"— its module scope reaches {hits}"
                                )
                        for alias in sub.names:
                            if short:
                                got = {
                                    p: v
                                    for p, v in third_party_reachable_from_call(
                                        short, alias.asname or alias.name
                                    ).items()
                                    if p in POST_VENV_PACKAGES
                                }
                                if got:
                                    violations.append(
                                        f"main() uses {module}.{alias.name} "
                                        f"(line {sub.lineno}) — it reaches {got}"
                                    )
                elif module and not guarded:
                    root = module.split(".")[0]
                    if root in POST_VENV_PACKAGES:
                        violations.append(
                            f"main() imports {root!r} directly at line {sub.lineno}"
                        )
            else:  # ast.Import
                for alias in sub.names:
                    if alias.name.startswith("vco_lib") or guarded:
                        continue
                    root = alias.name.split(".")[0]
                    if root in POST_VENV_PACKAGES:
                        violations.append(
                            f"main() imports {root!r} directly at line {sub.lineno}"
                        )
    assert not violations, (
        "main() runs its post-venv statements on the launching interpreter "
        "too — an inline import there follows the same rule as its callees:\n  "
        + "\n  ".join(violations)
    )


# ─── the walker itself: a gate that resolves nothing proves nothing ────────


def test_the_walker_sees_the_chain_that_shipped_the_bug():
    """``migrate_dotenv_openai_key`` — the venv-side function the subprocess
    now runs — must still be SEEN reaching ``requests`` by the walk. The
    install path is clean only because the subprocess boundary moved the
    call out of process, and this pins that the walker would notice the
    in-process spelling come back."""
    reached = third_party_reachable_from_call("openai_key", "migrate_dotenv_openai_key")
    assert "requests" in reached, (
        "the interprocedural walker no longer descends from a called symbol's "
        "body into imported vco_lib module scopes — it would report clean "
        "for the exact chain that was red on all five CI platforms"
    )


def test_the_subprocess_entries_are_clean():
    """The tier-A wrappers install.py actually calls in-process must not reach
    any venv package — ``migrate_dotenv_openai_key_via`` (the reporting entry)
    and ``run_dotenv_migration_under`` (the subprocess driver) are stdlib +
    ``subprocess``/``json`` only."""
    for symbol in ("migrate_dotenv_openai_key_via", "run_dotenv_migration_under"):
        reached = third_party_reachable_from_call("openai_key", symbol)
        hits = {p: v for p, v in reached.items() if p in POST_VENV_PACKAGES}
        assert not hits, f"{symbol} reaches venv packages in-process: {hits}"


def test_an_unguarded_function_local_import_is_reported():
    """A function-local import in a walked body (not module scope) is charged
    to the callable — the v0296 hole. ``resolve_openai_api_key``'s body does
    ``from vco_lib import agent_secrets`` unguarded; the walk must see it."""
    reached = third_party_reachable_from_call("openai_key", "resolve_openai_api_key")
    assert "requests" in reached, (
        "the walker stopped charging function-local vco_lib imports to the "
        "callable that contains them"
    )


def test_a_guarded_import_is_not_reported():
    """The sanctioned fallback shape: an import inside a try whose handler
    catches ImportError-class failures must not be charged."""
    idx = CallImportIndex(REPO / "install.py", name="install.py")
    # _enrich_slot_change is the reference guarded shape (step 7c).
    reached = idx.third_party_from_call("_enrich_slot_change")
    assert not reached, (
        f"_enrich_slot_change is the sanctioned guarded-fallback reference "
        f"shape; the walker stopped recognising its guard: {reached}"
    )


@pytest.mark.parametrize(
    ("module", "symbol"),
    [
        ("openai_key", "migrate_dotenv_openai_key_via"),
        ("openai_key", "run_dotenv_migration_under"),
    ],
)
def test_the_single_shot_helper_resolves(module: str, symbol: str) -> None:
    """``third_party_reachable_from_call`` resolves its module (a silent None
    skip here would make several tests above vacuous)."""
    from tests.common.import_chain import resolve_vco_lib_module

    assert resolve_vco_lib_module(module) is not None
