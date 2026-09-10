# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94 W-SHADOW: the suite imports THIS checkout, never another one.

The defect: `vco_lib` sits at the repo root, so pytest's rootdir prepend keeps
it honest — but `weaviate_mcp` sits one directory deeper, under
`claude_mcp_servers/`, and nothing put that on `sys.path`. The maintainer's
venv carries `_editable_impl_weaviate_mcp.pth`, which puts ANOTHER checkout's
`claude_mcp_servers` there at interpreter start, so 60 test files that
`import weaviate_mcp` were measuring the code of the RUNNING install rather
than the code being released. Measured from `/tmp` with `PYTHONPATH` unset:

    vco_lib      : <checkout>/vco_lib/__init__.py
    weaviate_mcp : <VCO_dev>/claude_mcp_servers/weaviate_mcp/__init__.py

`scripts/pre-ship-check.sh` happens to pin both roots; nothing else did, and a
gate that only some invocations satisfy is not a gate.

This file is the canary for both halves — the in-process pin in
`tests/conftest.py` and the child-process pin in `tests/common/child_env.py`.
It asserts the OUTCOME (where the modules actually came from, named in the
failure message) and the MECHANISM (the checkout's roots really are ahead of
any rival on the path), because on a clean CI runner with no stray `.pth` the
outcome assertion passes for free and would pin nothing.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = REPO_ROOT / "claude_mcp_servers"


def _inside_checkout(module_file: object) -> bool:
    if not isinstance(module_file, str):
        return False
    return Path(module_file).resolve().is_relative_to(REPO_ROOT)


def test_vco_lib_is_imported_from_this_checkout():
    import vco_lib

    assert _inside_checkout(vco_lib.__file__), (
        f"vco_lib resolved to {vco_lib.__file__} — OUTSIDE {REPO_ROOT}. The "
        f"tests would be measuring another tree's code."
    )


def test_weaviate_mcp_is_imported_from_this_checkout():
    import weaviate_mcp

    assert _inside_checkout(weaviate_mcp.__file__), (
        f"weaviate_mcp resolved to {weaviate_mcp.__file__} — OUTSIDE "
        f"{REPO_ROOT}. That is the `_editable_impl_weaviate_mcp.pth` shadow: "
        f"put {MCP_ROOT} on sys.path (tests/conftest.py does this)."
    )


def test_weaviate_mcp_server_is_imported_from_this_checkout():
    """The submodule the suite actually drives, not just the package root."""
    server = importlib.import_module("weaviate_mcp.server")

    assert _inside_checkout(server.__file__), (
        f"weaviate_mcp.server resolved to {server.__file__} — OUTSIDE "
        f"{REPO_ROOT}."
    )


def test_the_checkouts_import_roots_lead_sys_path():
    """The MECHANISM, so this canary still pins something on a clean runner.

    Both roots must be present AND ahead of any rival entry that could serve
    the same package names — a second `claude_mcp_servers` later on the path
    is harmless, one earlier is the bug.
    """
    path = [str(Path(p).resolve()) for p in sys.path if p]
    for root in (str(REPO_ROOT), str(MCP_ROOT)):
        assert root in path, f"{root} is not on sys.path: {path[:6]}"

    rivals = [
        p for p in path
        if Path(p).name == "claude_mcp_servers" and p != str(MCP_ROOT)
    ]
    if rivals:
        assert path.index(str(MCP_ROOT)) < min(path.index(r) for r in rivals), (
            f"a foreign claude_mcp_servers ({rivals}) precedes this "
            f"checkout's {MCP_ROOT} on sys.path — imports would resolve there"
        )


def test_a_spawned_child_also_imports_from_this_checkout():
    """The child half, driven — `child_env()` pins BOTH roots or it pins one.

    Not a source scan: we spawn a real interpreter with the real helper's
    environment and ask it where the two packages came from. Pre-fix this
    child printed `<VCO_dev>/claude_mcp_servers/weaviate_mcp/__init__.py`
    while its `vco_lib` came from the checkout — one process, two trees.
    """
    from tests.common.child_env import child_env

    env = child_env()
    assert str(MCP_ROOT) in env["PYTHONPATH"].split(os.pathsep), (
        f"child PYTHONPATH omits {MCP_ROOT}: {env['PYTHONPATH']}"
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import vco_lib, weaviate_mcp;"
            "print(vco_lib.__file__);print(weaviate_mcp.__file__)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    reported = proc.stdout.split()
    assert len(reported) == 2, proc.stdout
    for module_file in reported:
        assert _inside_checkout(module_file), (
            f"a spawned child imported {module_file} — OUTSIDE {REPO_ROOT}"
        )


# ---------------------------------------------------------------------------
# The eviction branch — a mutation, so both halves get a test
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, file: object) -> None:
        self.__file__ = file


def test_eviction_drops_a_module_imported_from_another_tree():
    """`sys.modules` outranks `sys.path`, so the path pin alone is not enough."""
    from tests import conftest

    foreign = "/home/somebody/VCO_dev/claude_mcp_servers/weaviate_mcp/__init__.py"
    cache = {
        "weaviate_mcp": _FakeModule(foreign),
        "weaviate_mcp.server": _FakeModule(foreign.replace("__init__", "server")),
        "vco_lib": _FakeModule(foreign),  # different package — must be untouched
    }
    evicted = conftest._evict_modules_imported_outside(
        "weaviate_mcp", REPO_ROOT, cache
    )
    assert sorted(evicted) == ["weaviate_mcp", "weaviate_mcp.server"]
    assert set(cache) == {"vco_lib"}


def test_eviction_leaves_a_module_from_this_checkout_alone():
    """The leave-alone half: reloading a correct module would be pure risk."""
    from tests import conftest

    mine = str(REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "__init__.py")
    cache = {
        "weaviate_mcp": _FakeModule(mine),
        # A module with no `__file__` (namespace package) is not evidence of
        # anything — leave it rather than guess.
        "weaviate_mcp.pkg": _FakeModule(None),
    }
    assert conftest._evict_modules_imported_outside(
        "weaviate_mcp", REPO_ROOT, cache
    ) == []
    assert set(cache) == {"weaviate_mcp", "weaviate_mcp.pkg"}
