# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F4: legacy .vscode MCP_* key auto-prune.

The 4 inert legacy MCP_* keys (MCP_WEAVIATE_SERVER / MCP_PYTHON /
MCP_OLLAMA_SERVER / MCP_PYTHONPATH) in `.vscode/settings.json`'s
`claude-code.env` block are now auto-pruned (default ON, no env gate) rather
than merely deferred. `_autoprune_legacy_vscode_mcp_env_keys`:

  * ACT: parses the file (dict + `claude-code.env` dict), deletes exactly the 4
    keys, preserves everything else, atomic-writes valid JSON, records an
    auto-resolution, returns True.
  * LEAVE-ALONE: unparseable JSONC / trailing-comma / unexpected shape → returns
    False (caller falls back to the deferral); the file is untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0283_deferral_emit_fake import (  # noqa: E402
    install_fake_deferral_emit,
    read_auto_resolutions,
)

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402


def _write_settings(folder: Path, obj) -> Path:
    p = folder / ".vscode" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, str):
        p.write_text(obj, encoding="utf-8")
    else:
        p.write_text(json.dumps(obj, indent=4), encoding="utf-8")
    return p


def _detect(folder: Path) -> dict:
    return project_init._detect_legacy_vscode_mcp_env_keys(folder)


# ---------------------------------------------------------------------------
# ACT: only the 4 keys removed, siblings + other settings intact, valid JSON.
# ---------------------------------------------------------------------------

def test_act_prunes_only_legacy_keys(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, {
        "files.watcherExclude": {"**/.git/**": True},
        "claude-code.env": {
            "MCP_PYTHON": "/home/foo/.venv/bin/python",
            "MCP_WEAVIATE_SERVER": "/home/foo/c/weaviate_mcp/server.py",
            "MCP_OLLAMA_SERVER": "/home/foo/c/ollama_mcp/server.py",
            "MCP_PYTHONPATH": "/home/foo/c/claude_mcp_servers",
            "WEAVIATE_URL": "http://localhost:8081",  # sibling, must survive
        },
    })
    detection = _detect(tmp_path)
    assert detection["action"] == "detected"

    pruned = project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection)
    assert pruned is True

    data = json.loads(settings.read_text(encoding="utf-8"))
    # Other top-level settings intact.
    assert data["files.watcherExclude"] == {"**/.git/**": True}
    env = data["claude-code.env"]
    # The 4 legacy keys gone.
    for k in ("MCP_PYTHON", "MCP_WEAVIATE_SERVER", "MCP_OLLAMA_SERVER", "MCP_PYTHONPATH"):
        assert k not in env, f"{k} should have been pruned"
    # The sibling env key survives.
    assert env["WEAVIATE_URL"] == "http://localhost:8081"

    # A subsequent detection reports "none" — the file is clean.
    assert _detect(tmp_path)["action"] == "none"

    # B-F9: auto-resolution recorded.
    rows = read_auto_resolutions(tmp_path)
    assert any(
        r["condition_id"] == "legacy_vscode_mcp_env_keys_present"
        and r["action"] == "pruned_inert_vscode_mcp_keys"
        for r in rows
    ), rows


def test_act_leaves_empty_env_block_in_place(tmp_path: Path) -> None:
    """When the only keys were the 4 legacy ones, the now-empty
    claude-code.env dict is LEFT in place (conservative — prune keys, don't
    restructure)."""
    settings = _write_settings(tmp_path, {
        "claude-code.env": {
            "MCP_PYTHON": "/x",
            "MCP_WEAVIATE_SERVER": "/y",
            "MCP_OLLAMA_SERVER": "/z",
            "MCP_PYTHONPATH": "/w",
        },
    })
    detection = _detect(tmp_path)
    assert project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection) is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "claude-code.env" in data
    assert data["claude-code.env"] == {}


# ---------------------------------------------------------------------------
# LEAVE-ALONE: unparseable JSONC / trailing-comma → returns False, untouched.
# ---------------------------------------------------------------------------

def test_leave_alone_jsonc_trailing_comma_returns_false(tmp_path: Path) -> None:
    raw = '{\n  "claude-code.env": {\n    "MCP_PYTHON": "/x",  // comment\n  },\n}\n'
    settings = _write_settings(tmp_path, raw)
    # Detection itself reports "unparseable" for JSONC.
    detection = {"action": "detected", "keys": ["MCP_PYTHON"],
                 "file": ".vscode/settings.json"}
    pruned = project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection)
    assert pruned is False, "unparseable JSONC must NOT be auto-pruned"
    # File byte-identical.
    assert settings.read_text(encoding="utf-8") == raw


def test_leave_alone_env_block_not_dict_returns_false(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"claude-code.env": "not-a-dict"})
    detection = {"action": "detected", "keys": ["MCP_PYTHON"],
                 "file": ".vscode/settings.json"}
    assert project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection) is False


def test_leave_alone_no_target_keys_returns_false(tmp_path: Path) -> None:
    """If the detection carried keys but the file no longer has any of the 4
    legacy keys, the prune is a no-op → False (caller keeps historical
    behaviour)."""
    _write_settings(tmp_path, {"claude-code.env": {"WEAVIATE_URL": "x"}})
    detection = {"action": "detected", "keys": ["MCP_PYTHON"],
                 "file": ".vscode/settings.json"}
    assert project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection) is False


# ---------------------------------------------------------------------------
# Write-failure fallback: caller emits the deferral (function returns False).
# ---------------------------------------------------------------------------

def test_write_failure_returns_false(tmp_path: Path, monkeypatch) -> None:
    _write_settings(tmp_path, {
        "claude-code.env": {"MCP_PYTHON": "/x", "OTHER": "keep"},
    })
    detection = _detect(tmp_path)
    assert detection["action"] == "detected"

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    # Force the atomic write to fail.
    monkeypatch.setattr("vco_lib.atomic.atomic_write_text", _boom)
    pruned = project_init._autoprune_legacy_vscode_mcp_env_keys(tmp_path, detection)
    assert pruned is False, "a write error must fall back to the deferral (False)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
