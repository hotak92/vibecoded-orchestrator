# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (fresh Windows install): install printed "model gateway not
importable (ModuleNotFoundError)" and held back every gateway agent.

Root cause: ``install.py`` and the ``install-bundle`` child it spawns with
``sys.executable`` run under the SYSTEM interpreter, while ``model_router``
is installed into the orchestrator venv only. Simulated here with
``python -I -S`` (no site-packages, so no editable install of the package —
exactly the system interpreter's view) and only the checkout on ``sys.path``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_AGENT = REPO_ROOT / "templates" / "agents" / "module-gateway" / "qwen-implementer.md"


def _system_python(body: str) -> subprocess.CompletedProcess:
    code = (f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n"
            + textwrap.dedent(body))
    # child_env() pins the checkout's import roots on PYTHONPATH, but `-I`
    # ignores every PYTHON* variable and `-S` skips site-packages: the child
    # sees ONLY the stdlib plus the checkout root inserted above — the
    # system interpreter's view.
    return subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True,
                          text=True, timeout=120, cwd=str(REPO_ROOT),
                          env=child_env(WEAVIATE_URL="http://127.0.0.1:9"))


def test_the_simulated_interpreter_really_lacks_the_package():
    res = _system_python("""
import importlib.util
print(importlib.util.find_spec("model_router") is None)
""")
    assert res.stdout.strip() == "True", res.stderr


def test_gateway_definitions_resolve_their_vendor_outside_the_venv():
    """The bundle's per-vendor gate (`definition_vendor`) — pre-fix it raised
    ImportError (via `vco_lib.vscode_settings`' module-level model_router
    import), so every gateway definition was held back."""
    res = _system_python(f"""
from pathlib import Path
from vco_lib.module_gated_delivery import definition_vendor
print(definition_vendor(Path({str(GATEWAY_AGENT)!r})))
""")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "qwen", (res.stdout, res.stderr)


def test_the_model_grid_key_probe_builds_outside_the_venv():
    """The CLAUDE.md model grid's probe — pre-fix the import failed and the
    render printed "model gateway not importable ... rendering Claude rows
    only". Built, never called (calling it would consult the key store)."""
    res = _system_python("""
from vco_lib.model_selection import vendor_key_state_probe
print(callable(vendor_key_state_probe()))
""")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "True"


def test_an_installed_copy_wins_over_the_checkout(tmp_path, monkeypatch):
    """ensure_importable APPENDS: a model_router already importable (the
    venv's) is never shadowed, and nothing is added to sys.path."""
    from vco_lib import gateway_package as gp

    before = list(sys.path)
    assert gp.ensure_importable() is True
    assert sys.path == before


def test_a_checkout_without_the_package_fails_loudly_and_changes_nothing(tmp_path, monkeypatch):
    """Review V02103-FINAL2 I-3: the package is shipped, so its absence is a
    broken install — an ImportError naming the path and the remedy, never a
    quiet False the caller may skip past. Red-proof: restore `return False`
    → this fails."""
    import pytest

    from vco_lib import gateway_package as gp

    monkeypatch.setattr(gp.importlib.util, "find_spec", lambda _n: None)
    before = list(sys.path)
    with pytest.raises(ImportError) as info:
        gp.ensure_importable(tmp_path)
    assert "broken VCO install" in str(info.value)
    assert str(tmp_path / gp.GATEWAY_PACKAGE_REL) in str(info.value)
    assert "--update" in str(info.value)
    assert sys.path == before


def test_the_append_resolves_the_same_files_the_editable_install_serves():
    """The supported-path rationale, pinned: under the system interpreter the
    package resolves to THIS checkout's files — the same bytes the venv's
    editable install points at — so the append cannot serve another copy."""
    res = _system_python("""
from vco_lib.gateway_package import ensure_importable
ensure_importable()
import model_router
print(model_router.__file__)
""")
    assert res.returncode == 0, res.stderr
    assert Path(res.stdout.strip()).resolve() == (
        REPO_ROOT / "claude_mcp_servers" / "model_router" / "__init__.py").resolve()


def test_gateway_ensure_reports_a_broken_install_on_stderr(monkeypatch, capsys):
    from vco_lib import gateway_ensure, gateway_package

    def broken(*_a, **_k):
        raise ImportError("broken VCO install: test")

    monkeypatch.setattr(gateway_package, "ensure_importable", broken)
    assert gateway_ensure._gateway_has_run_here(None) is False
    assert "broken VCO install" in capsys.readouterr().err
