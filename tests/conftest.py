# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pytest fixtures shared across the orchestrator test suite.

v0.2.46 KG-AUTO-HEAL-E + v0.2.47 RL-6c (paired ship): the autouse fixture
below forces ``VCT_DISABLE_HUB_RESOLVER=1`` for every test EXCEPT those
in the opt-out list (which explicitly exercise the hub-resolver path).

**Why this is needed**: on developer machines where the launcher's
``vct-hub`` is running (the maintainer box, every contributor's local
setup), tests that monkey-patch ``KG_COLLECTION`` /
``SHARED_KG_COLLECTION`` / ``VCT_KG_ACCESS_LIST`` env vars were silently
losing to the hub-resolved values — both ``_try_resolve_project_config()``
in ``claude_mcp_servers/weaviate_mcp/server.py`` AND
``vco_lib.project_config.resolve()`` itself were called BEFORE the env-
fallback chain, and the hub returned the project's REAL bindings. Tests
passed on CI (no hub running) but failed locally with confusing diff
messages like:

    AssertionError: '[peer:Alpha]' != '[self]'
    AssertionError: 'VibeCodedOrchestrator_KnowledgeGraph' != 'AcmeTeam_SharedKG'

The gate at ``vco_lib.project_config.resolve`` short-circuits to
``HubUnreachable`` when ``VCT_DISABLE_HUB_RESOLVER`` is truthy. The
calling script's try/except then falls through to its env-var
fallback path, which is what the tests have been setting up.

The opt-out list contains tests that EXPLICITLY exercise the resolver
path (they spawn a mock hub and want the production code path to
actually reach the mocked function). The opt-out is intentionally
explicit so an accidentally-broken hub-resolver test surfaces loudly
rather than silently picking up the live machine's hub config.

References:
- ``vco_lib/project_config.py::resolve`` — the gate (v0.2.46).
- ``claude_mcp_servers/weaviate_mcp/server.py::_try_resolve_project_config``
  — the same gate at the MCP layer (v0.2.47 RL-6c).
- ``knowledge/concepts/launcher-hub-single-writer-principle.md`` — why
  the hub is the production source of truth (but tests need a hatch).
- ``knowledge/concepts/parallel-pr-coordination-gotchas-2026-05-10.md``
  §14 — the lesson cluster this conftest closes.
"""
from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _weaviate_importable(python_exe: str) -> bool:
    """True iff `python_exe -c 'import weaviate'` succeeds. Soft: any spawn
    error (missing interpreter, timeout) is treated as not-importable."""
    try:
        r = subprocess.run(
            [python_exe, "-c", "import weaviate"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


@functools.lru_cache(maxsize=None)
def _resolve_analyzer_python_cached(vct_venv: str, install_root: str) -> str | None:
    """Resolution body, memoized on the env inputs that steer it (so a test that
    manipulates ``$VCT_VENV`` / ``$VCT_INSTALL_ROOT`` gets a fresh resolution
    rather than a stale cached path). The subprocess ``import weaviate`` probe is
    the expensive part; caching per-env avoids re-spawning it across the several
    analyzer-spawning tests in one run."""
    candidates: list[str] = []

    def _venv_pythons(venv_dir: Path) -> list[str]:
        return [
            str(venv_dir / "bin" / "python"),
            str(venv_dir / "bin" / "python3"),
            str(venv_dir / "Scripts" / "python.exe"),
        ]

    if vct_venv:
        candidates.extend(_venv_pythons(Path(vct_venv)))
        candidates.append(vct_venv)  # in case $VCT_VENV is the python itself

    if install_root:
        candidates.extend(_venv_pythons(Path(install_root) / ".venv"))

    candidates.extend(_venv_pythons(_REPO_ROOT / ".venv"))

    for cand in candidates:
        if Path(cand).exists() and _weaviate_importable(cand):
            return cand

    # Last resort: the pytest runner's own interpreter, but ONLY if it can
    # import weaviate (the canonical case — the suite runs under the VCO venv).
    if _weaviate_importable(sys.executable):
        return sys.executable

    return None


def resolve_analyzer_python() -> str | None:
    """v0.2.84 PLAN-v0284 (review T-2, one-concern-one-home): resolve the Python
    interpreter that tests must use to spawn ``analyze_code_graph.py`` and other
    scripts that ``import weaviate`` at module load.

    Bare ``shutil.which("python3")`` resolves the SYSTEM python, which on most
    machines (and CI without a global weaviate-client) fails the analyzer's
    module-level ``import weaviate`` — the analyzer then exits 1 with
    "weaviate-client not installed" BEFORE reaching the G5 worktree guard, so the
    g5-guard tests observed the wrong exit/stderr (false red).

    Resolution order (mirrors ``templates/hooks/_lib/resolve-vco-venv.sh`` tiers
    1→2→4, the canonical VCO-venv chain), returning the FIRST candidate whose
    interpreter can ``import weaviate``:

      1. ``$VCT_VENV`` explicit override — ``$VCT_VENV/bin/python`` (POSIX) /
         ``$VCT_VENV/Scripts/python.exe`` (Windows).
      2. ``$VCT_INSTALL_ROOT/.venv`` — launcher-provided canonical venv.
      3. ``<repo>/.venv`` — orchestrator-clone fallback.
      4. ``sys.executable`` — the interpreter running pytest, IF it can import
         weaviate (the suite is meant to run under the VCO venv, so this is the
         common hit).

    Returns the resolved interpreter path, or ``None`` when NONE can import
    weaviate — callers must ``pytest.skip`` with a clear reason rather than
    spawn a python that will crash at import and yield a misleading result.
    """
    return _resolve_analyzer_python_cached(
        os.environ.get("VCT_VENV", "").strip(),
        os.environ.get("VCT_INSTALL_ROOT", "").strip(),
    )


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_tracked_files_against_install_pollution():
    """Restore the repo's tracked ``CLAUDE.md`` + remove a repo-root
    ``UPDATE_DEFERRED.md`` after the test session.

    Several tests run the real ``install.py --update`` as a subprocess.
    install.py's orchestrator-self step materializes
    ``<install_root>/CLAUDE.md`` and the deferral flow writes
    ``<install_root>/.claude/context/UPDATE_DEFERRED.md`` — and ``install_root``
    resolves to the directory install.py LIVES in (this repo), regardless of
    the subprocess cwd. So those tests splice a ``vco-deferral-reminder`` block
    into the repo's TRACKED ``CLAUDE.md`` and drop a (gitignored)
    ``UPDATE_DEFERRED.md``. The ``CLAUDE.md`` mutation is the real hazard: it is
    a tracked file, so a later ``git add -A`` could commit install cruft into
    the public repo (which is NOT an installed clone and must carry no install
    artifacts).

    This session-scoped guard snapshots both at session start and restores /
    removes them at session end, so the suite never leaves the repo dirty —
    independent of WHICH test pollutes (current or future). Best practice for
    new tests remains: run install.py with ``--skip-materialize-claude-dir`` or
    target a tmp install root. Soft-fail: cleanup errors never fail the session.
    """
    repo_root = Path(__file__).resolve().parent.parent
    claude_md = repo_root / "CLAUDE.md"
    deferred = repo_root / ".claude" / "context" / "UPDATE_DEFERRED.md"

    claude_before = claude_md.read_bytes() if claude_md.is_file() else None
    deferred_existed = deferred.is_file()

    try:
        yield
    finally:
        try:
            if claude_before is not None:
                if not claude_md.is_file() or claude_md.read_bytes() != claude_before:
                    claude_md.write_bytes(claude_before)
            elif claude_md.is_file():
                claude_md.unlink()  # didn't exist before the session
        except OSError:
            pass
        try:
            if not deferred_existed and deferred.is_file():
                deferred.unlink()
        except OSError:
            pass


# Test files that explicitly exercise the hub-resolver and MUST run with
# the gate UNSET (so `vco_lib.project_config.resolve` reaches its HTTP
# probe + their mock-patches actually fire). The autouse fixture below
# clears the env var for these files; sets it for everyone else.
_RESOLVER_OPT_OUT_FILES = frozenset({
    # Tests that mock or call resolve() directly and need the production
    # code path to NOT short-circuit:
    "test_caller_migration_step18.py",
    "test_project_resolution.py",
    "test_project_config.py",       # v0.2.46: also exercises resolve() directly
})


@pytest.fixture(autouse=True)
def _disable_hub_resolver_in_tests(request):
    """Force ``_try_resolve_project_config`` to fall through to env-only
    resolution for tests that DON'T explicitly exercise the resolver.

    The KG / access-list / diagrams / shared-KG cluster (~26 tests across
    6+ files) needs env-only resolution to keep their injected env vars
    intact. The resolver-test cluster (`test_caller_migration_step18.py`,
    `test_project_resolution.py`, `test_project_config.py`) needs the
    resolver enabled so their ``mock.patch("vco_lib.project_config.
    resolve", ...)`` calls have an effect. We discriminate by test file
    name; the opt-out list above is the canonical record of "tests that
    test the hub-resolver itself".

    The fixture restores the prior env state in its finally block (so a
    test that sets the var itself isn't broken by this fixture).
    """
    test_file = request.node.fspath.basename
    if test_file in _RESOLVER_OPT_OUT_FILES:
        # Resolver tests: ensure the env var is NOT set so the production
        # code's guard doesn't short-circuit.
        prev = os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
        try:
            yield
        finally:
            if prev is not None:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev
    else:
        # Default path: env-only resolution. Set the var if it wasn't
        # already; restore the prior value (which may be None) afterward.
        prev = os.environ.get("VCT_DISABLE_HUB_RESOLVER")
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
            else:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev
