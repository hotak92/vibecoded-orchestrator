# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 D-10 / D-12 — server.py resolution + tier-parse safety.

- D-12: a malformed KG_TIER_*/CODE_TIER_* env value must NOT crash the MCP
  at import; _safe_float warns and falls back to the calibrated default.
- D-10: with a relative file_path + unset KG_BASE_DIR, store_knowledge_node
  prefers CLAUDE_PROJECT_DIR (not the orchestrator-inferred base).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("VCT_DISABLE_HUB_RESOLVER", "1")


def _import_server_with_env(env_overrides: dict[str, str]):
    """Import server.py in a subprocess with the given env, returning the
    subprocess result — the only way to test module-scope import behaviour
    cleanly (env is read at import time)."""
    env = os.environ.copy()
    env.pop("VCT_VENV", None)
    env["VCT_DISABLE_HUB_RESOLVER"] = "1"
    env.update(env_overrides)
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import weaviate_mcp.server as s; "
        "print('MIN', s._TIER_THRESHOLDS['min']); "
        "print('CODEMIN', s._CODE_TIER_THRESHOLDS['min'])"
    ) % str(MCP)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_malformed_kg_tier_min_does_not_crash_import():
    res = _import_server_with_env({"KG_TIER_MIN": "0,42"})
    assert res.returncode == 0, f"import crashed: {res.stderr}"
    assert "MIN 0.42" in res.stdout, res.stdout


def test_malformed_code_tier_full_does_not_crash_import():
    res = _import_server_with_env({"CODE_TIER_FULL": "zzz"})
    assert res.returncode == 0, f"import crashed: {res.stderr}"
    # CODE min default is 0.22 and unaffected; the bad FULL just falls back.
    assert "CODEMIN 0.22" in res.stdout, res.stdout


def test_empty_string_tier_env_uses_default():
    res = _import_server_with_env({"KG_TIER_MIN": ""})
    assert res.returncode == 0, f"import crashed: {res.stderr}"
    assert "MIN 0.42" in res.stdout, res.stdout


def test_valid_override_still_honored():
    res = _import_server_with_env({"KG_TIER_MIN": "0.5"})
    assert res.returncode == 0, res.stderr
    assert "MIN 0.5" in res.stdout, res.stdout


def test_d10_prefers_claude_project_dir_for_relative_path(tmp_path, monkeypatch):
    """Relative file_path + unset KG_BASE_DIR → resolve under
    CLAUDE_PROJECT_DIR, not the server-inferred orchestrator base."""
    server = importlib.import_module("weaviate_mcp.server")
    proj = tmp_path / "myproj"
    proj.mkdir()
    monkeypatch.setattr(server, "KG_BASE_DIR", "", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.delenv("KG_BASE_DIR", raising=False)

    resolved = server._resolve_project_root_for_deferral()
    assert resolved is not None
    assert resolved.resolve() == proj.resolve(), (
        "D-10: relative-path fallback must resolve to CLAUDE_PROJECT_DIR, "
        f"got {resolved}"
    )
    # And it must NOT be the orchestrator-inferred base.
    assert resolved.resolve() != server._SERVER_INFERRED_BASE.resolve()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
