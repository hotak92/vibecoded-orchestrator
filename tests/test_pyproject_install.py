# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the root ``pyproject.toml`` packaging contract.

What these tests pin (regression guard for the Phase 0.C packaging
work):

* ``pyproject.toml`` exists at repo root and is valid TOML.
* The ``[project.scripts] vco`` entry-point string resolves to a real
  callable on a real module (catches typos like
  ``vco_lib.cli.__main___:main`` or renames that break the install).
* The packaged ``vco_lib`` tree (per ``[tool.hatch.build.targets.wheel]
  packages``) covers every ``vco_lib*`` Python module currently in the
  repo — i.e. nobody added a new ``vco_lib/something.py`` without it
  being shipped by the wheel.
* Version + name + build-backend match what install.py / launcher
  expect (``vibecoded-orchestrator`` / hatchling).

These tests do NOT invoke ``pip install`` — that's covered by the
``test_install`` smoke suite and (for the orchestrator-self path) by
install.py's CI integration test. The goal here is to catch authoring
errors in pyproject.toml the moment they land, before pip ever sees
them.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Stdlib tomllib lives in 3.11+; that's our pinned minimum so we don't
# need the tomli backport.
import tomllib  # noqa: E402  — local stdlib import after pathmunge below

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    """Load + parse pyproject.toml. Fails loudly if missing — the
    whole file IS the contract this test guards."""
    assert PYPROJECT.exists(), (
        f"pyproject.toml missing at {PYPROJECT}. The `vco` CLI relies on "
        "its [project.scripts] entry; install.py's pip-install-editable "
        "step also requires it. Don't delete this file without first "
        "restoring the scripts/vco shim wrappers."
    )
    with PYPROJECT.open("rb") as fp:
        return tomllib.load(fp)


def _resolve_entry_point(entry: str):
    """Resolve a ``module.path:callable_name`` entry-point string the
    same way ``importlib.metadata`` + console-script wheels do.

    Returns the callable. Raises ImportError / AttributeError on failure
    so test output names the actual problem (broken module path vs
    missing callable)."""
    module_path, _, attr = entry.partition(":")
    assert module_path, f"entry point missing module path: {entry!r}"
    assert attr, f"entry point missing callable: {entry!r}"
    module = importlib.import_module(module_path)
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pyproject_parses_as_valid_toml():
    """Sanity: pyproject.toml must round-trip through tomllib."""
    data = _load_pyproject()
    assert isinstance(data, dict)
    # Smoke that the top-level tables are present — the rest of the
    # tests rely on them.
    assert "build-system" in data, "pyproject.toml missing [build-system]"
    assert "project" in data, "pyproject.toml missing [project]"


def test_build_backend_is_hatchling():
    """hatchling is the chosen build backend. If this changes,
    install.py's pip-install-editable step needs re-validation
    against the new backend's editable-install semantics."""
    data = _load_pyproject()
    build_system = data["build-system"]
    assert build_system["build-backend"] == "hatchling.build", (
        f"unexpected build-backend: {build_system.get('build-backend')!r}. "
        "If you intentionally switched away from hatchling, update this "
        "test and re-verify `pip install -e .` works in install.py."
    )
    requires = build_system.get("requires", [])
    assert any("hatchling" in r for r in requires), (
        "build-system.requires must include hatchling"
    )


def test_project_name_and_distribution_metadata():
    """The dist name on PyPI / in pip's metadata is contract."""
    project = _load_pyproject()["project"]
    assert project["name"] == "vibecoded-orchestrator"
    # version is bumped per-release; just confirm it parses as a non-empty string.
    version = project.get("version")
    assert isinstance(version, str) and version, (
        "project.version must be a non-empty string"
    )
    # Python pin: stdlib `tomllib` is the reason for >=3.11; lowering
    # this floor requires adding `tomli` to dependencies.
    assert project.get("requires-python", "").startswith(">=3.11"), (
        "requires-python must stay >=3.11 (stdlib tomllib dependency)"
    )


def test_console_script_vco_resolves_to_callable():
    """[project.scripts] vco = "..." must resolve to a real callable.

    This is the meat: the contract that breaks the install when
    violated. If `pip install -e .` registers a `vco` console-script
    whose target doesn't import, the user sees a baffling
    `ModuleNotFoundError` on the first `vco --help` rather than a
    pip-install-time error.
    """
    project = _load_pyproject()["project"]
    scripts = project.get("scripts", {})
    assert "vco" in scripts, (
        "[project.scripts] missing `vco` — that's the whole CLI entry "
        "point. Removing it would silently break `pip install -e .` "
        "users who expect the `vco` command on PATH."
    )
    entry = scripts["vco"]
    assert entry == "vco_lib.cli.__main__:main", (
        f"Unexpected `vco` entry point: {entry!r}. The expected target "
        "is `vco_lib.cli.__main__:main` (see vco_lib/cli/__main__.py)."
    )
    func = _resolve_entry_point(entry)
    assert callable(func), f"{entry} resolved but is not callable: {func!r}"


def test_console_script_vco_returns_int_when_called():
    """Console-script entry points must return an int exit code.

    pip-generated launchers call `sys.exit(main())`; if `main()`
    returns None, exit code is 0 (works but masks bugs); if it raises,
    user sees a traceback. Verify the happy path returns int.
    """
    func = _resolve_entry_point("vco_lib.cli.__main__:main")
    # --help triggers argparse's SystemExit(0) — exactly what the CLI
    # should do on success. Catch it explicitly so the test passes.
    with pytest.raises(SystemExit) as exc:
        func(["--help"])
    assert exc.value.code == 0


def test_wheel_packages_cover_all_vco_lib_modules():
    """Catch the footgun: someone adds `vco_lib/new_thing.py` but
    forgets to ensure it ships in the wheel.

    Hatchling's default is "ship everything in the listed packages
    recursively" — so as long as `vco_lib` is in `packages`, every
    `.py` file underneath ships. This test confirms the package IS
    listed; if someone changes the package list to be more selective
    (e.g. switches to `packages = ["vco_lib.cli"]`), this test fails
    loudly.
    """
    data = _load_pyproject()
    wheel_cfg = data.get("tool", {}).get("hatch", {}).get(
        "build", {}).get("targets", {}).get("wheel", {})
    packages = wheel_cfg.get("packages", [])
    assert "vco_lib" in packages, (
        "[tool.hatch.build.targets.wheel] must ship the top-level "
        "`vco_lib` package. Current packages: "
        f"{packages!r}. Restricting to a subpackage would silently "
        "drop modules from the wheel."
    )


def test_mcp_extras_listed_when_required_by_install_py():
    """install.py's `_install_requirements` uses `pip install -e .[mcp]`
    to pull the full server stack. Verify the `mcp` extra exists and
    pins the headline server-side deps.
    """
    project = _load_pyproject()["project"]
    extras = project.get("optional-dependencies", {})
    assert "mcp" in extras, (
        "Missing [project.optional-dependencies] mcp = [...]. install.py "
        "depends on `.[mcp]` to install the MCP server stack."
    )
    mcp = extras["mcp"]
    assert any(d.startswith("mcp") for d in mcp), (
        "mcp extra must include `mcp` (the Python SDK)"
    )
    assert any(d.startswith("weaviate-client") for d in mcp), (
        "mcp extra must include weaviate-client"
    )
