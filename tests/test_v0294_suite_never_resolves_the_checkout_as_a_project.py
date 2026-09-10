# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canary: under the suite's defaults the MCP server never resolves THIS
checkout as the project — so nothing it writes (deferral rows, the CLAUDE.md
reminder block) can land here. See the W-PROJECT-DIR block in conftest.py.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _server():
    return importlib.import_module("weaviate_mcp.server")


def test_the_suite_pins_a_scratch_project_dir() -> None:
    pinned = os.environ.get("CLAUDE_PROJECT_DIR")
    assert pinned, "CLAUDE_PROJECT_DIR is not pinned — the MCP would fall back to its module root (this checkout)"
    assert Path(pinned).is_dir()
    assert REPO not in (Path(pinned), *Path(pinned).parents), pinned


def test_resolution_context_is_the_scratch_project_not_the_checkout() -> None:
    srv = _server()
    root, kind = srv._resolution_context()
    assert kind == srv._CTX_WORKSPACE, (root, kind)
    assert root != srv._MODULE_OWN_ROOT, "resolved the checkout (module root) as the project"
    assert root == Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()


def test_without_the_pin_the_checkout_would_be_the_answer(monkeypatch, tmp_path) -> None:
    """The leave-alone half, and the reason the pin exists: strip the key, run
    from a cwd with no project above it, and the module root answers."""
    srv = _server()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    root, kind = srv._resolution_context()
    assert (root, kind) == (srv._MODULE_OWN_ROOT, srv._CTX_MODULE) or kind == srv._CTX_CWD


def test_opt_out_file_decision_is_absent_and_others_are_pinned() -> None:
    conftest = importlib.import_module("tests.conftest")
    assert conftest._claude_project_dir_pin_for("test_project_resolution.py") is None
    assert conftest._claude_project_dir_pin_for("test_anything_else.py") == str(conftest._SCRATCH_PROJECT_DIR)
