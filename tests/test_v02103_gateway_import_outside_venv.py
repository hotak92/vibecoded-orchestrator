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


def test_a_checkout_without_the_package_changes_nothing(tmp_path, monkeypatch):
    from vco_lib import gateway_package as gp

    monkeypatch.setattr(gp.importlib.util, "find_spec", lambda _n: None)
    before = list(sys.path)
    assert gp.ensure_importable(tmp_path) is False
    assert sys.path == before
