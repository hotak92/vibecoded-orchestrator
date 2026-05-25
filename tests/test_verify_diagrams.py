# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco verify-diagrams``.

Mirrors the style of ``test_verify_pins.py`` /
``test_verify_env_projection.py``:

* Each individual check has a happy-path + a fail-path test.
* Stubs Phase 0.B dependencies (``_resolve_project_folder`` /
  ``_list_registered_projects``) via monkey-patching so the test suite
  doesn't depend on a live launcher DB.
* Uses ``tmp_path`` for on-disk fixtures (.claude folder layouts,
  CLAUDE.md, hook scripts).
* The launcher DB is materialised in-memory or as a tmp SQLite file
  with just enough schema to exercise the verifier (projects +
  _schema_migrations + project_modules + the diagrams-related tables).

The Weaviate-class check is tested via heavy mocking (no live
Weaviate); the hub-allowlist check is tested via stubbing the
``_http_get_json`` helper.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.cli import verify_diagrams as vd  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================


def _seed_launcher_db(db_path: Path) -> None:
    """Create a launcher DB matching the schema this verifier reads.

    Includes:
      * _schema_migrations (max version 22)
      * projects table with one row
      * Every table from migration 022
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE _schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        );

        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE
        );

        CREATE TABLE project_diagrams (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL
        );

        CREATE TABLE diagram_snapshots (
            id INTEGER PRIMARY KEY,
            project_diagram_id INTEGER NOT NULL
        );

        CREATE TABLE diagram_access (
            id INTEGER PRIMARY KEY,
            owner_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            permission TEXT NOT NULL
        );

        CREATE TABLE project_mcp_tool_grants (
            project_id TEXT NOT NULL,
            mcp_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (project_id, mcp_name, tool_name)
        );

        CREATE TABLE project_modules (
            project_id TEXT NOT NULL,
            module_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            registered_at INTEGER NOT NULL,
            PRIMARY KEY (project_id, module_name)
        );

        CREATE TABLE diagram_index_retry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL,
            last_error_at INTEGER
        );
        """
    )
    now = int(time.time())
    cur.execute(
        "INSERT INTO _schema_migrations (version, description, applied_at) "
        "VALUES (?, ?, ?)",
        (22, "diagrams", now),
    )
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?,?,?,?)",
        ("p-1", "demo", "/tmp/does-not-matter", "demo"),
    )
    cur.execute(
        "INSERT INTO project_modules "
        "(project_id, module_name, enabled, registered_at) "
        "VALUES (?, ?, ?, ?)",
        ("p-1", "diagrams", 1, now),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def launcher_db(tmp_path, monkeypatch) -> Path:
    """Materialise a launcher DB and patch the path resolver."""
    db_path = tmp_path / "launcher.db"
    _seed_launcher_db(db_path)
    monkeypatch.setattr(vd, "_resolve_launcher_db_path", lambda: db_path)
    return db_path


@pytest.fixture
def project_folder(tmp_path) -> Path:
    """Materialise a fully-wired project on-disk for the happy path."""
    folder = tmp_path / "project"
    (folder / ".claude" / "hooks").mkdir(parents=True)
    (folder / ".vscode").mkdir()
    # Hook scripts
    for stem in vd.HOOK_SCRIPT_NAMES:
        p = folder / ".claude" / "hooks" / f"{stem}.sh"
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    # settings.json with both PreToolUse entries + PostToolUse Bash entry
    settings = {
        "env": {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "bash .claude/hooks/"
                                "pre-diagram-path-validation.sh"
                            ),
                        }
                    ],
                },
                {
                    "matcher": "mcp__mermaid__.*|mcp__excalidraw__.*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "bash .claude/hooks/"
                                "pre-diagram-path-validation.sh"
                            ),
                        }
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/post-file-delete.sh",
                        }
                    ],
                }
            ],
        },
    }
    (folder / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    # .claude/env shell file
    (folder / ".claude" / "env").write_text(
        "KG_COLLECTION=Demo_KnowledgeGraph\n"
        "DIAGRAMS_COLLECTION=Demo_Diagrams\n"
        "VCT_DIAGRAMS_ACCESS_LIST=\n",
        encoding="utf-8",
    )
    # .vscode/settings.json
    vscode = {
        "claude-code.env": {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        }
    }
    (folder / ".vscode" / "settings.json").write_text(
        json.dumps(vscode), encoding="utf-8"
    )
    # CLAUDE.md with the diagrams section header
    (folder / "CLAUDE.md").write_text(
        "# Project\n\n## Diagrams (Mermaid + Excalidraw)\n\nbody\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def claude_json(tmp_path, monkeypatch) -> Path:
    """Materialise a ~/.claude.json that registers both wrappers."""
    p = tmp_path / ".claude.json"
    payload = {
        "mcpServers": {
            "mermaid": {
                "command": "python",
                "args": ["-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
            },
            "excalidraw": {
                "command": "python",
                "args": ["-m", "claude_mcp_servers.wrappers.excalidraw_proxy"],
            },
        }
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(vd, "_claude_json_path", lambda: p)
    return p


def _args(
    project_id: str | None = "p-1",
    *,
    json_mode: bool = False,
    fix: bool = False,
    all_: bool = False,
    quick: bool = True,
) -> argparse.Namespace:
    """Build a Namespace mirroring the argparse output. ``--quick`` is
    True by default in tests so unrelated Weaviate/hub probes are
    skipped — individual tests opt back in by passing ``quick=False``."""
    return argparse.Namespace(
        project_id=project_id,
        json=json_mode,
        fix=fix,
        all=all_,
        quick=quick,
    )


# ===========================================================================
# Check 1 — project row in launcher DB
# ===========================================================================


def test_project_row_happy(launcher_db):
    result, row = vd._check_project_row("p-1")
    assert result.status == vd.STATUS_OK
    assert row is not None
    assert row["id"] == "p-1"
    assert row["name"] == "demo"


def test_project_row_missing(launcher_db):
    result, row = vd._check_project_row("does-not-exist")
    assert result.status == vd.STATUS_FAIL
    assert row is None
    assert result.fix_hint is not None


def test_project_row_db_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vd, "_resolve_launcher_db_path",
        lambda: tmp_path / "nope.db",
    )
    result, row = vd._check_project_row("p-1")
    assert result.status == vd.STATUS_FAIL
    assert row is None


# ===========================================================================
# Check 2 — project_modules row
# ===========================================================================


def test_project_modules_row_present(launcher_db):
    result = vd._check_project_modules_row("p-1", fix=False)
    assert result.status == vd.STATUS_OK


def test_project_modules_row_missing(launcher_db):
    # Delete the row, re-check.
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DELETE FROM project_modules WHERE project_id='p-1'")
    conn.commit()
    conn.close()
    result = vd._check_project_modules_row("p-1", fix=False)
    assert result.status == vd.STATUS_FAIL


def test_project_modules_row_fix(launcher_db):
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DELETE FROM project_modules WHERE project_id='p-1'")
    conn.commit()
    conn.close()
    result = vd._check_project_modules_row("p-1", fix=True)
    assert result.status == vd.STATUS_FIXED
    # Re-check confirms the row landed.
    confirm = vd._check_project_modules_row("p-1", fix=False)
    assert confirm.status == vd.STATUS_OK


def test_project_modules_row_disabled(launcher_db):
    conn = sqlite3.connect(str(launcher_db))
    conn.execute(
        "UPDATE project_modules SET enabled=0 WHERE project_id='p-1'"
    )
    conn.commit()
    conn.close()
    result = vd._check_project_modules_row("p-1", fix=False)
    assert result.status == vd.STATUS_FAIL


# ===========================================================================
# Check 3 — migration 022
# ===========================================================================


def test_migration_022_applied(launcher_db):
    result = vd._check_migration_022()
    assert result.status == vd.STATUS_OK


def test_migration_022_too_old(launcher_db):
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DELETE FROM _schema_migrations WHERE version=22")
    conn.commit()
    conn.close()
    result = vd._check_migration_022()
    assert result.status == vd.STATUS_FAIL
    assert "< 22" in result.detail


def test_migration_022_table_missing(launcher_db):
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DROP TABLE diagram_index_retry")
    conn.commit()
    conn.close()
    result = vd._check_migration_022()
    assert result.status == vd.STATUS_FAIL
    assert "diagram_index_retry" in result.detail


# ===========================================================================
# Check 4 — MCP wrappers in ~/.claude.json
# ===========================================================================


def test_mcp_wrappers_registered(claude_json):
    result = vd._check_mcp_wrappers()
    assert result.status == vd.STATUS_OK


def test_mcp_wrappers_missing(tmp_path, monkeypatch):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(vd, "_claude_json_path", lambda: p)
    result = vd._check_mcp_wrappers()
    assert result.status == vd.STATUS_FAIL
    assert "mermaid" in result.detail
    assert "excalidraw" in result.detail


def test_mcp_wrappers_wrong_module(tmp_path, monkeypatch):
    p = tmp_path / ".claude.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mermaid": {
                        "command": "npx",
                        "args": ["-y", "claude-mermaid@1.0"],
                    },
                    "excalidraw": {
                        "command": "python",
                        "args": [
                            "-m",
                            "claude_mcp_servers.wrappers.excalidraw_proxy",
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vd, "_claude_json_path", lambda: p)
    result = vd._check_mcp_wrappers()
    assert result.status == vd.STATUS_FAIL
    assert "mermaid" in result.detail


def test_mcp_wrappers_json_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vd, "_claude_json_path",
        lambda: tmp_path / "nope.json",
    )
    result = vd._check_mcp_wrappers()
    assert result.status == vd.STATUS_FAIL


# ===========================================================================
# Check 5 — hub allowlist HTTP route
# ===========================================================================


def test_hub_allowlist_happy(monkeypatch):
    calls: list[str] = []

    def _stub_get(url: str, token: str | None, *, timeout: float = 5.0):
        calls.append(url)
        return {"default_allow_all": True, "denied_tools": []}

    monkeypatch.setattr(vd, "_http_get_json", _stub_get)
    monkeypatch.setattr(vd, "_vct_hub_token", lambda: "test-token")
    result = vd._check_hub_allowlist("p-1")
    assert result.status == vd.STATUS_OK
    assert any("mermaid" in u for u in calls)
    assert any("excalidraw" in u for u in calls)


def test_hub_allowlist_no_token_skips(monkeypatch):
    monkeypatch.setattr(vd, "_vct_hub_token", lambda: None)
    result = vd._check_hub_allowlist("p-1")
    assert result.status == vd.STATUS_SKIP


def test_hub_allowlist_unreachable_skips(monkeypatch):
    import urllib.error

    def _stub_get(url: str, token: str | None, *, timeout: float = 5.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(vd, "_http_get_json", _stub_get)
    monkeypatch.setattr(vd, "_vct_hub_token", lambda: "tok")
    result = vd._check_hub_allowlist("p-1")
    assert result.status == vd.STATUS_SKIP


def test_hub_allowlist_non_object_response(monkeypatch):
    def _stub_get(url: str, token: str | None, *, timeout: float = 5.0):
        return "not an object"

    monkeypatch.setattr(vd, "_http_get_json", _stub_get)
    monkeypatch.setattr(vd, "_vct_hub_token", lambda: "tok")
    result = vd._check_hub_allowlist("p-1")
    assert result.status == vd.STATUS_FAIL


# ===========================================================================
# Check 6 — env projection
# ===========================================================================


def test_env_projection_happy(monkeypatch, project_folder):
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
    )
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_OK


def test_env_projection_missing_key(monkeypatch, project_folder):
    # Strip DIAGRAMS_COLLECTION from .vscode/settings.json
    vscode = {"claude-code.env": {"KG_COLLECTION": "Demo_KnowledgeGraph"}}
    (project_folder / ".vscode" / "settings.json").write_text(
        json.dumps(vscode), encoding="utf-8"
    )
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
    )
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_FAIL
    assert "DIAGRAMS_COLLECTION" in result.detail


def test_env_projection_key_not_in_canonical(monkeypatch, project_folder):
    """Canonical projection lacks DIAGRAMS_COLLECTION → reports the gap
    as a drift entry. This is the Phase 0.B gap path."""
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {"KG_COLLECTION": "Demo_KnowledgeGraph"},
    )
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_FAIL
    assert "DIAGRAMS_COLLECTION" in result.detail
    assert "Phase 0.B gap" in result.detail


def test_env_projection_fix_delegates(monkeypatch, project_folder):
    """``--fix`` re-resolves the canonical bundle and calls
    :func:`apply_project_env` with the (bundle, surfaces=...) contract.

    Regression guard for code-review B5: the prior call site invoked
    ``apply_project_env(expected, project_folder=...)`` — wrong type AND
    wrong kwarg — every ``--fix`` invocation that reached this branch
    died with ``KeyError("project_root")`` because ``expected`` was a
    flat env mapping, not a ProjectEnvBundle.
    """
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
    )
    # Break one surface.
    (project_folder / ".vscode" / "settings.json").write_text(
        "{}", encoding="utf-8"
    )

    # Stub the real config_projection contract: project_env_from_db
    # returns a ProjectEnvBundle, apply_project_env writes surfaces and
    # returns a per-surface key report.
    fake_bundle = {
        "canonical_env": {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
        },
        "project_id": "p-1",
        "project_root": project_folder,
    }
    fake_from_db = mock.Mock(return_value=fake_bundle)
    fake_apply = mock.Mock(return_value={
        "claude_settings_json": ["KG_COLLECTION", "DIAGRAMS_COLLECTION"],
        "claude_env": ["KG_COLLECTION", "DIAGRAMS_COLLECTION"],
        "vscode_settings_json": ["KG_COLLECTION", "DIAGRAMS_COLLECTION"],
    })
    fake_cp = mock.Mock(
        apply_project_env=fake_apply,
        project_env_from_db=fake_from_db,
    )
    monkeypatch.setitem(sys.modules, "vco_lib.config_projection", fake_cp)
    result = vd._check_env_projection("p-1", project_folder, fix=True)
    assert result.status == vd.STATUS_FIXED, result.detail

    # Real contract: apply_project_env(bundle, surfaces=(...)). The
    # bundle must be the ProjectEnvBundle (NOT the flat expected env)
    # and project_folder must NOT appear as a kwarg.
    fake_apply.assert_called_once()
    call_args = fake_apply.call_args
    assert call_args.args[0] is fake_bundle
    assert "project_folder" not in call_args.kwargs
    # All 3 surfaces should be written (drift detector reads all 3).
    surfaces = tuple(call_args.kwargs.get("surfaces", ()))
    assert set(surfaces) == {
        "claude_settings_json",
        "claude_env",
        "vscode_settings_json",
    }
    # Bundle re-resolution wired through project_env_from_db.
    fake_from_db.assert_called_once_with("p-1")


def test_env_projection_fix_apply_failure(monkeypatch, project_folder):
    """``apply_project_env`` raising ConfigProjectionError surfaces as
    STATUS_FIX_FAILED (not a silent success). The real contract signals
    failure by raising — not by an "ok" return field."""
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
    )
    (project_folder / ".vscode" / "settings.json").write_text(
        "{}", encoding="utf-8"
    )
    fake_bundle = {
        "canonical_env": {"KG_COLLECTION": "Demo_KnowledgeGraph"},
        "project_id": "p-1",
        "project_root": project_folder,
    }
    fake_apply = mock.Mock(side_effect=RuntimeError("surface write failed"))
    fake_cp = mock.Mock(
        apply_project_env=fake_apply,
        project_env_from_db=mock.Mock(return_value=fake_bundle),
    )
    monkeypatch.setitem(sys.modules, "vco_lib.config_projection", fake_cp)
    result = vd._check_env_projection("p-1", project_folder, fix=True)
    assert result.status == vd.STATUS_FIX_FAILED
    assert "surface write failed" in result.detail


# ===========================================================================
# Check 7 — Weaviate Diagrams class
# ===========================================================================


def test_weaviate_class_quick_skips():
    result = vd._check_weaviate_class("demo", fix=False, quick=True)
    assert result.status == vd.STATUS_SKIP


def test_weaviate_class_no_client(monkeypatch):
    # Pretend weaviate-client is not installed.
    fake_modules = dict(sys.modules)
    fake_modules.pop("weaviate", None)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _patched_import(name, *args, **kwargs):
        if name == "weaviate" or name.startswith("weaviate."):
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _patched_import)
    result = vd._check_weaviate_class("demo", fix=False, quick=False)
    assert result.status == vd.STATUS_SKIP
    assert "weaviate-client" in result.detail


def test_weaviate_class_present(monkeypatch):
    fake_collection = mock.Mock()
    fake_client = mock.Mock()
    fake_client.collections.list_all.return_value = {
        "Demo_Diagrams": fake_collection,
    }
    fake_module = mock.Mock(connect_to_custom=mock.Mock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "weaviate", fake_module)
    # Force the inner import to resolve to our fake.
    result = vd._check_weaviate_class("demo", fix=False, quick=False)
    assert result.status == vd.STATUS_OK
    assert "Demo_Diagrams" in result.detail


def test_weaviate_class_missing(monkeypatch):
    fake_client = mock.Mock()
    fake_client.collections.list_all.return_value = {"Other_Collection": object()}
    fake_module = mock.Mock(connect_to_custom=mock.Mock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "weaviate", fake_module)
    result = vd._check_weaviate_class("demo", fix=False, quick=False)
    assert result.status == vd.STATUS_FAIL


def test_weaviate_class_unreachable_skips(monkeypatch):
    fake_module = mock.Mock(
        connect_to_custom=mock.Mock(side_effect=OSError("connection refused")),
    )
    monkeypatch.setitem(sys.modules, "weaviate", fake_module)
    result = vd._check_weaviate_class("demo", fix=False, quick=False)
    assert result.status == vd.STATUS_SKIP


# ===========================================================================
# Check 8 — PreToolUse hooks
# ===========================================================================


def test_pretooluse_hooks_happy(project_folder):
    result = vd._check_pretooluse_hooks(project_folder)
    assert result.status == vd.STATUS_OK


def test_pretooluse_hooks_missing_mcp_matcher(project_folder):
    settings = json.loads(
        (project_folder / ".claude" / "settings.json").read_text()
    )
    # Drop the MCP-matcher entry.
    settings["hooks"]["PreToolUse"] = [
        e for e in settings["hooks"]["PreToolUse"]
        if "mcp__" not in str(e.get("matcher", ""))
    ]
    (project_folder / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    result = vd._check_pretooluse_hooks(project_folder)
    assert result.status == vd.STATUS_FAIL
    assert "mcp__" in result.detail


def test_pretooluse_hooks_no_settings(tmp_path):
    folder = tmp_path / "empty"
    (folder / ".claude").mkdir(parents=True)
    result = vd._check_pretooluse_hooks(folder)
    assert result.status == vd.STATUS_FAIL


# ===========================================================================
# Check 9 — post-file-delete hook
# ===========================================================================


def test_post_delete_hook_happy(project_folder):
    result = vd._check_post_delete_hook(project_folder)
    assert result.status == vd.STATUS_OK


def test_post_delete_hook_missing(project_folder):
    settings = json.loads(
        (project_folder / ".claude" / "settings.json").read_text()
    )
    settings["hooks"]["PostToolUse"] = []
    (project_folder / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    result = vd._check_post_delete_hook(project_folder)
    assert result.status == vd.STATUS_FAIL


# ===========================================================================
# Check 10 — hook scripts on disk
# ===========================================================================


def test_hook_scripts_happy(project_folder):
    result = vd._check_hook_scripts_on_disk(project_folder)
    assert result.status == vd.STATUS_OK


def test_hook_scripts_missing(tmp_path):
    folder = tmp_path / "empty"
    (folder / ".claude" / "hooks").mkdir(parents=True)
    result = vd._check_hook_scripts_on_disk(folder)
    assert result.status == vd.STATUS_FAIL


def test_hook_scripts_accepts_ps1_only(tmp_path):
    folder = tmp_path / "win"
    hooks = folder / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for stem in vd.HOOK_SCRIPT_NAMES:
        (hooks / f"{stem}.ps1").write_text("# stub\n", encoding="utf-8")
    result = vd._check_hook_scripts_on_disk(folder)
    assert result.status == vd.STATUS_OK


# ===========================================================================
# Check 11 — indexer importable
# ===========================================================================


def test_indexer_importable_happy():
    result = vd._check_indexer_importable()
    assert result.status == vd.STATUS_OK


# ===========================================================================
# Check 12 — path validator round-trip
# ===========================================================================


def test_path_validator_happy():
    result = vd._check_path_validator()
    assert result.status == vd.STATUS_OK


# ===========================================================================
# Check 13 — CLAUDE.md section
# ===========================================================================


def test_claude_md_section_present(project_folder):
    result = vd._check_claude_md_section(project_folder)
    assert result.status == vd.STATUS_OK


def test_claude_md_section_missing(project_folder):
    (project_folder / "CLAUDE.md").write_text(
        "# Just a project, no diagrams here.\n", encoding="utf-8"
    )
    result = vd._check_claude_md_section(project_folder)
    assert result.status == vd.STATUS_FAIL


def test_claude_md_section_no_file_skips(tmp_path):
    folder = tmp_path / "no-claude-md"
    folder.mkdir()
    result = vd._check_claude_md_section(folder)
    assert result.status == vd.STATUS_SKIP


# ===========================================================================
# Orchestration — cmd_verify_diagrams + JSON schema
# ===========================================================================


def _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json):
    """Wire every monkey-patchable dependency so a full happy-path
    invocation works with --quick (no Weaviate/hub probe)."""
    # Make project lookup land in our tmp folder, not the DB's
    # /tmp/does-not-matter.
    conn = sqlite3.connect(str(launcher_db))
    conn.execute(
        "UPDATE projects SET folder_path=? WHERE id='p-1'",
        (str(project_folder),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        vd, "_project_env_from_db",
        lambda _pid: {
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        },
    )
    # Use the launcher folder_path; resolver wouldn't be called when
    # the column is non-empty.


def test_orchestration_full_happy(
    monkeypatch, launcher_db, project_folder, claude_json, capsys
):
    _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json)
    code = vd.cmd_verify_diagrams(_args(quick=True))
    out = capsys.readouterr().out
    assert "verify-diagrams: demo" in out
    assert "Summary:" in out
    assert code == vd.EXIT_OK


def test_orchestration_json_schema(
    monkeypatch, launcher_db, project_folder, claude_json, capsys
):
    _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json)
    code = vd.cmd_verify_diagrams(_args(json_mode=True, quick=True))
    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert payload["command"] == "verify-diagrams"
    assert payload["project_id"] == "p-1"
    assert "checks" in payload
    assert "summary" in payload
    assert "exit_code" in payload
    assert payload["overall"] in {"ok", "fail", "env_problem", "fix_failed"}
    # Every check has the documented shape.
    for c in payload["checks"]:
        assert "name" in c
        assert "status" in c
        assert "detail" in c
    assert code == vd.EXIT_OK


def test_orchestration_project_not_found(monkeypatch, launcher_db, capsys):
    code = vd.cmd_verify_diagrams(_args(project_id="ghost"))
    assert code == vd.EXIT_ENV_PROBLEM


def test_orchestration_json_exit_code_matches_shell(
    monkeypatch, launcher_db, capsys
):
    """Regression: the JSON payload's ``exit_code`` field must equal
    the shell exit code, even when the orchestrator chose
    EXIT_ENV_PROBLEM (2) over the report's internal exit_code (1).
    """
    code = vd.cmd_verify_diagrams(_args(project_id="ghost", json_mode=True))
    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert payload["exit_code"] == code == vd.EXIT_ENV_PROBLEM
    assert payload["overall"] == "env_problem"


def test_orchestration_missing_positional(launcher_db, capsys):
    code = vd.cmd_verify_diagrams(_args(project_id=None, all_=False))
    assert code == vd.EXIT_ENV_PROBLEM


def test_orchestration_all_and_positional_conflict(launcher_db, capsys):
    code = vd.cmd_verify_diagrams(_args(project_id="p-1", all_=True))
    assert code == vd.EXIT_ENV_PROBLEM


def test_quick_skips_slow_checks(
    monkeypatch, launcher_db, project_folder, claude_json, capsys
):
    _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json)
    code = vd.cmd_verify_diagrams(_args(json_mode=True, quick=True))
    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["hub_allowlist"]["status"] == vd.STATUS_SKIP
    assert by_name["weaviate_diagrams_class"]["status"] == vd.STATUS_SKIP
    assert code == vd.EXIT_OK


def test_all_iterates(
    monkeypatch, launcher_db, project_folder, claude_json, capsys
):
    _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json)
    monkeypatch.setattr(
        vd, "_list_registered_projects",
        lambda: [{"id": "p-1", "slug": "demo", "folder": str(project_folder)}],
    )
    code = vd.cmd_verify_diagrams(
        _args(project_id=None, all_=True, json_mode=True, quick=True)
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert "projects" in payload
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["project_id"] == "p-1"
    assert code == vd.EXIT_OK


def test_fix_invocation_on_project_modules(
    monkeypatch, launcher_db, project_folder, claude_json
):
    _wire_full_happy_path(monkeypatch, launcher_db, project_folder, claude_json)
    # Break the project_modules row.
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DELETE FROM project_modules WHERE project_id='p-1'")
    conn.commit()
    conn.close()
    code = vd.cmd_verify_diagrams(_args(quick=True, fix=True))
    # --fix repairs the missing row → status FIXED → exit_code stays OK.
    assert code == vd.EXIT_OK


def test_overall_label_mapping():
    assert vd._overall_label(vd.EXIT_OK) == "ok"
    assert vd._overall_label(vd.EXIT_FAIL) == "fail"
    assert vd._overall_label(vd.EXIT_ENV_PROBLEM) == "env_problem"
    assert vd._overall_label(vd.EXIT_FIX_FAILED) == "fix_failed"
    assert vd._overall_label(999) == "unknown"
