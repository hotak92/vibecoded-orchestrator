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
* The launcher DB is materialised as a tmp SQLite file carrying the REAL
  launcher schema (``tests.common.launcher_db_fixture`` applies the shipped
  migrations), then seeded with the rows the verifier reads (projects +
  project_modules).

The Weaviate-class check is tested via heavy mocking (no live
Weaviate); the hub-allowlist check is tested via stubbing the
``_http_get_json`` helper.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    create_empty_launcher_db,
    insert_rows,
)
from vco_lib.cli import verify_diagrams as vd  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================


def _seed_launcher_db(db_path: Path) -> None:
    """Create a launcher DB with the REAL schema and the verifier's rows.

    The schema comes from the shipped migrations (all 44 of them), so every
    table ``verify_diagrams.DIAGRAMS_TABLES`` looks for — ``project_diagrams``,
    ``diagram_snapshots``, ``diagram_access``, ``project_mcp_tool_grants``,
    ``project_modules``, ``diagram_index_retry`` — exists in its real shape.
    The pre-merge version hand-rolled all six, and three of them had columns
    the launcher never wrote (``diagram_snapshots.project_diagram_id``,
    ``diagram_access.owner_project_id`` / ``.permission``).

    Seeded rows: one project (``p-1``) and its
    ``project_modules('diagrams', enabled=1)`` row.
    """
    create_empty_launcher_db(db_path)
    add_project(
        db_path,
        project_id="p-1",
        name="demo",
        folder_path="/tmp/does-not-matter",
        slug="demo",
    )
    insert_rows(db_path, "project_modules", [{
        "project_id": "p-1", "module_name": "diagrams", "enabled": 1,
    }])


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
    # .claude/env — the managed block, rendered by the projection's OWN
    # block builder (what `apply` writes), with a user line outside it.
    from vco_lib.config_projection import _build_managed_block

    (folder / ".claude" / "env").write_text(
        'export MY_OWN="kept"\n' + _build_managed_block({
            "KG_COLLECTION": "Demo_KnowledgeGraph",
            "DIAGRAMS_COLLECTION": "Demo_Diagrams",
            "VCT_DIAGRAMS_ACCESS_LIST": "",
        }),
        encoding="utf-8",
    )
    # .vscode/settings.json — NOT a surface `apply` writes by default; left
    # deliberately stale so every env test also proves it is not compared.
    vscode = {
        "claude-code.env": {
            "KG_COLLECTION": "Acme_KnowledgeGraph",
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
    # DELIBERATE migration-level simulation: the DB carries the real
    # schema, so `_schema_migrations` holds every shipped version (max
    # is far above 22). Deleting everything from 22 up reproduces a
    # launcher.db that stopped BELOW migration 022 — the state
    # `_check_migration_022` reports as `max(...)=<n> < 22`. (The
    # pre-merge fixture seeded version 22 alone, so deleting that one row
    # was enough; it is not, against the real migration set.)
    conn = sqlite3.connect(str(launcher_db))
    conn.execute("DELETE FROM _schema_migrations WHERE version >= 22")
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


_DIAG_ENV = {
    "KG_COLLECTION": "Demo_KnowledgeGraph",
    "DIAGRAMS_COLLECTION": "Demo_Diagrams",
    "VCT_DIAGRAMS_ACCESS_LIST": "",
}


def _stub_env(monkeypatch, env=None):
    monkeypatch.setattr(vd, "_project_env_from_db", lambda _pid: dict(env or _DIAG_ENV))


def _edit_settings_env(project_folder, **changes):
    path = project_folder / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data["env"].pop(key, None)
        else:
            data["env"][key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def _as_jsonc(path: Path) -> None:
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text("// my note\n" + text[:-1].rstrip() + ",\n}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        json.loads(path.read_text(encoding="utf-8"))


def test_env_projection_missing_key(monkeypatch, project_folder):
    """ACT: a diagrams key missing from an applied surface is drift."""
    _edit_settings_env(project_folder, DIAGRAMS_COLLECTION=None)
    _stub_env(monkeypatch)
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_FAIL
    assert "DIAGRAMS_COLLECTION on .claude/settings.json" in result.detail


def test_env_projection_ignores_the_vscode_surface(monkeypatch, project_folder):
    """LEAVE-ALONE (v0.2.97): ``apply`` does not write ``.vscode/settings.json``
    by default, so its content is not evidence. RED before: the fixture's
    stale VS Code block (and a project with none at all) was FAIL forever."""
    _stub_env(monkeypatch)
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_OK, result.detail
    (project_folder / ".vscode" / "settings.json").unlink()
    assert vd._check_env_projection("p-1", project_folder, fix=False).status == vd.STATUS_OK


def test_env_projection_reads_a_jsonc_settings_file(monkeypatch, project_folder):
    """JSONC (v0.2.97). RED before: the strict reader read it as empty and
    reported every key as drift; the two hook checks said "cannot parse"."""
    _as_jsonc(project_folder / ".claude" / "settings.json")
    _stub_env(monkeypatch)
    assert vd._check_env_projection("p-1", project_folder, fix=False).status == vd.STATUS_OK
    assert vd._check_pretooluse_hooks(project_folder).status == vd.STATUS_OK
    assert vd._check_post_delete_hook(project_folder).status == vd.STATUS_OK


def test_env_projection_an_omitted_key_is_correct_when_absent(monkeypatch, project_folder):
    """The projection OMITS ``VCT_DIAGRAMS_ACCESS_LIST`` when no peer granted
    diagram read, and ``apply`` removes it. RED before: that was reported as a
    "Phase 0.B gap" FAIL for every project without diagram grants."""
    env = {k: v for k, v in _DIAG_ENV.items() if k != "VCT_DIAGRAMS_ACCESS_LIST"}
    _edit_settings_env(project_folder, VCT_DIAGRAMS_ACCESS_LIST=None)
    from vco_lib.config_projection import _build_managed_block

    (project_folder / ".claude" / "env").write_text(_build_managed_block(env), encoding="utf-8")
    _stub_env(monkeypatch, env)
    assert vd._check_env_projection("p-1", project_folder, fix=False).status == vd.STATUS_OK


def test_env_projection_key_not_in_canonical(monkeypatch, project_folder):
    """ACT twin: a key the projection omits but a surface still carries is
    drift (``apply`` would remove it)."""
    _stub_env(monkeypatch, {"KG_COLLECTION": "Demo_KnowledgeGraph"})
    result = vd._check_env_projection("p-1", project_folder, fix=False)
    assert result.status == vd.STATUS_FAIL
    assert "DIAGRAMS_COLLECTION on .claude/settings.json: expected '<absent>'" in result.detail


@pytest.mark.parametrize("rel,raw", [
    (".claude/settings.json", b"{ not jsonc at all"),
    (".claude/settings.json", b"[1, 2]\n"),
    (".claude/settings.json", b"\xff\xfe{\"env\": {}}"),
    (".claude/env", b"export KG_COLLECTION=\"\xff\xfe\"\n"),
])
def test_env_projection_unreadable_is_cannot_verify(monkeypatch, project_folder, rel, raw):
    """UNREADABLE (v0.2.97): never OK, never FAIL — and ``--fix`` writes
    nothing to a file it cannot read."""
    (project_folder / rel).write_bytes(raw)
    _stub_env(monkeypatch)
    fake_apply = mock.Mock()
    monkeypatch.setitem(sys.modules, "vco_lib.config_projection", mock.Mock(
        apply_project_env=fake_apply, project_env_from_db=mock.Mock()))
    for fix in (False, True):
        result = vd._check_env_projection("p-1", project_folder, fix=fix)
        assert result.status == vd.STATUS_CANNOT_VERIFY, result.detail
        assert rel in result.detail
    fake_apply.assert_not_called()


@pytest.mark.parametrize("check", ["_check_pretooluse_hooks", "_check_post_delete_hook"])
def test_hook_checks_unreadable_settings_is_cannot_verify(project_folder, check):
    (project_folder / ".claude" / "settings.json").write_bytes(b"{ broken")
    result = getattr(vd, check)(project_folder)
    assert result.status == vd.STATUS_CANNOT_VERIFY
    assert "cannot read settings.json" in result.detail


def test_cannot_verify_maps_to_exit_two():
    report = vd._ProjectVerifyReport(
        project_id="p", project_name="n", project_folder="f",
        checks=[vd._CheckResult("a", vd.STATUS_FAIL, "x"),
                vd._CheckResult("b", vd.STATUS_CANNOT_VERIFY, "y")])
    assert report.exit_code() == vd.EXIT_ENV_PROBLEM
    assert "1 CANNOT VERIFY" in vd._format_human(report)


def test_env_projection_fix_delegates(monkeypatch, project_folder):
    """``--fix`` re-resolves the canonical bundle and calls
    :func:`apply_project_env` with the bundle and apply's DEFAULT surfaces,
    then re-reads the surfaces.

    Regression guard for code-review B5: the prior call site invoked
    ``apply_project_env(expected, project_folder=...)`` — wrong type AND
    wrong kwarg — every ``--fix`` invocation that reached this branch
    died with ``KeyError("project_root")`` because ``expected`` was a
    flat env mapping, not a ProjectEnvBundle.

    v0.2.97: no ``surfaces=`` — it used to force the VS Code surface too.
    """
    _stub_env(monkeypatch)
    _edit_settings_env(project_folder, DIAGRAMS_COLLECTION="Drifted")
    (project_folder / ".vscode" / "settings.json").unlink()
    fake_bundle = {
        "canonical_env": dict(_DIAG_ENV),
        "project_id": "p-1",
        "project_root": project_folder,
    }

    def _write_back(bundle, **_kw):
        _edit_settings_env(project_folder, DIAGRAMS_COLLECTION="Demo_Diagrams")
        return {"claude_settings_json": ["DIAGRAMS_COLLECTION"], "claude_env": []}

    fake_from_db = mock.Mock(return_value=fake_bundle)
    fake_apply = mock.Mock(side_effect=_write_back)
    monkeypatch.setitem(sys.modules, "vco_lib.config_projection", mock.Mock(
        apply_project_env=fake_apply, project_env_from_db=fake_from_db))
    result = vd._check_env_projection("p-1", project_folder, fix=True)
    assert result.status == vd.STATUS_FIXED, result.detail

    fake_apply.assert_called_once()
    call_args = fake_apply.call_args
    assert call_args.args[0] is fake_bundle
    assert call_args.kwargs == {}, "apply's default surfaces, nothing forced"
    fake_from_db.assert_called_once_with("p-1")
    assert not (project_folder / ".vscode" / "settings.json").exists()


def test_env_projection_fix_that_does_not_repair_is_fix_failed(monkeypatch, project_folder):
    """FIXED means "re-read and matching", not "a write ran"."""
    _stub_env(monkeypatch)
    _edit_settings_env(project_folder, DIAGRAMS_COLLECTION="Drifted")
    monkeypatch.setitem(sys.modules, "vco_lib.config_projection", mock.Mock(
        apply_project_env=mock.Mock(return_value={}),
        project_env_from_db=mock.Mock(return_value={"canonical_env": {}})))
    result = vd._check_env_projection("p-1", project_folder, fix=True)
    assert result.status == vd.STATUS_FIX_FAILED
    assert "still 1 drift entries after apply" in result.detail


def test_env_projection_fix_with_the_real_writer_creates_no_vscode(tmp_path, monkeypatch):
    """The REAL ``apply_project_env`` behind --fix: the drifted surface is
    repaired, the user's hooks survive, and no ``.vscode/`` appears."""
    from vco_lib import config_projection as cp

    folder = tmp_path / "fresh"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": []}, "env": {"DIAGRAMS_COLLECTION": "Drifted"}}),
        encoding="utf-8")
    _stub_env(monkeypatch)
    monkeypatch.setattr(cp, "project_env_from_db", lambda _pid: {
        "canonical_env": dict(_DIAG_ENV), "project_id": "p-1", "project_root": folder})
    result = vd._check_env_projection("p-1", folder, fix=True)
    assert result.status == vd.STATUS_FIXED, result.detail
    assert not (folder / ".vscode").exists()
    assert json.loads((folder / ".claude" / "settings.json").read_text())["hooks"] == {"Stop": []}


def test_env_projection_fix_apply_failure(monkeypatch, project_folder):
    """``apply_project_env`` raising ConfigProjectionError surfaces as
    STATUS_FIX_FAILED (not a silent success). The real contract signals
    failure by raising — not by an "ok" return field."""
    _stub_env(monkeypatch)
    _edit_settings_env(project_folder, DIAGRAMS_COLLECTION="Drifted")
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


# ---------------------------------------------------------------------------
# v0.2.91 (WP-D item 4): stale-env hub-token fallback.
#
# THE SEAM: `$VCT_HUB_TOKEN` wins over the on-disk token, and the hub
# rotates that file on every start. A shell that exported the token before
# an update therefore made this VERIFY command 401 — and because
# `HTTPError` subclasses `URLError`, the 401 landed in the "hub not
# reachable" arm and the check reported SKIP, blaming the hub. A verify
# command that silently stops verifying (and misattributes the cause) is
# the honesty class this release closes.
#
# THE SKIP SEMANTICS ARE UNCHANGED: an un-rescued refusal re-raises the
# ORIGINAL exception, so the same STATUS_SKIP with the same message text
# is produced. Both halves are pinned below.
# ---------------------------------------------------------------------------

_STALE_ENV_TOKEN = "stale-env-token-v0291-not-a-real-secret"
_FRESH_DISK_TOKEN = "fresh-disk-token-v0291-not-a-real-secret"


@pytest.fixture(autouse=True)
def _reset_stale_env_latch():
    """The latch is MODULE-level (a `--all` run verifies many projects in
    ONE process), so it must not leak between tests."""
    vd._test_reset_stale_env_state()
    yield
    vd._test_reset_stale_env_state()


@pytest.fixture
def stale_env_pin(tmp_path: Path, monkeypatch):
    """Fresh token on disk, STALE token exported."""
    state = tmp_path / "vct-state"
    state.mkdir()
    (state / "hub.token").write_text(_FRESH_DISK_TOKEN, encoding="utf-8")
    monkeypatch.setattr(vd, "_on_disk_hub_token", lambda: _FRESH_DISK_TOKEN)
    monkeypatch.setenv("VCT_HUB_TOKEN", _STALE_ENV_TOKEN)
    monkeypatch.delenv("VCT_HUB_TOKEN_STRICT", raising=False)
    return state


def _hub_accepting(expected: str, seen: list):
    """`_http_get_json` stub: raises HTTPError 401 unless the bearer matches."""
    import urllib.error

    def _stub_get(url: str, token, *, timeout: float = 5.0):
        seen.append(token)
        if token != expected:
            raise urllib.error.HTTPError(
                url=url, code=401, msg="Unauthorized", hdrs=None, fp=None
            )
        return {"default_allow_all": True}

    return _stub_get


def test_hub_allowlist_stale_pin_is_retried_and_check_runs(
    monkeypatch, stale_env_pin, capsys
):
    """RED-PROOF: pre-v0.2.91 the 401 was caught by the URLError arm and
    the check reported SKIP ("hub not reachable") — a silent non-verify."""
    seen: list = []
    monkeypatch.setattr(vd, "_http_get_json", _hub_accepting(_FRESH_DISK_TOKEN, seen))
    result = vd._check_hub_allowlist("p-1")

    assert result.status == vd.STATUS_OK, result.detail
    # First probe presents the stale pin, retries with the on-disk token;
    # the SECOND mcp_name then rides the latch directly.
    assert seen == [_STALE_ENV_TOKEN, _FRESH_DISK_TOKEN, _FRESH_DISK_TOKEN]
    assert vd._IGNORE_ENV_HUB_TOKEN is True
    # The definitive line goes to stderr — never stdout (the `--json`
    # machine contract).
    captured = capsys.readouterr()
    assert vd.STALE_ENV_TOKEN_MESSAGE in captured.err
    assert vd.STALE_ENV_TOKEN_MESSAGE not in captured.out


def test_hub_allowlist_strict_pin_keeps_the_skip(monkeypatch, stale_env_pin):
    """LEAVE-ALONE: the guard keeps the pin authoritative → the historical
    SKIP with the historical message."""
    monkeypatch.setenv("VCT_HUB_TOKEN_STRICT", "1")
    seen: list = []
    monkeypatch.setattr(vd, "_http_get_json", _hub_accepting(_FRESH_DISK_TOKEN, seen))
    result = vd._check_hub_allowlist("p-1")

    assert result.status == vd.STATUS_SKIP
    assert "hub not reachable" in result.detail
    assert seen == [_STALE_ENV_TOKEN]
    assert vd._IGNORE_ENV_HUB_TOKEN is False


def test_hub_allowlist_retry_also_refused_keeps_the_original_diagnostic(
    monkeypatch, stale_env_pin
):
    """LEAVE-ALONE: both tokens refused → the ORIGINAL exception is
    re-raised, so the skip text is byte-identical to pre-v0.2.91."""
    seen: list = []
    monkeypatch.setattr(
        vd, "_http_get_json", _hub_accepting("a-third-token-nobody-has", seen)
    )
    result = vd._check_hub_allowlist("p-1")

    assert result.status == vd.STATUS_SKIP
    assert "hub not reachable" in result.detail
    assert seen == [_STALE_ENV_TOKEN, _FRESH_DISK_TOKEN]
    assert vd._IGNORE_ENV_HUB_TOKEN is False


def test_hub_allowlist_non_credential_error_is_not_retried(monkeypatch, stale_env_pin):
    """LEAVE-ALONE: a 500 is not a credential problem — one request, and
    the historical failure classification."""
    import urllib.error

    seen: list = []

    def _stub_get(url: str, token, *, timeout: float = 5.0):
        seen.append(token)
        raise urllib.error.HTTPError(
            url=url, code=500, msg="Server Error", hdrs=None, fp=None
        )

    monkeypatch.setattr(vd, "_http_get_json", _stub_get)
    result = vd._check_hub_allowlist("p-1")

    # HTTPError subclasses URLError → the historical "hub not reachable"
    # SKIP arm, reached on the FIRST probe with no retry.
    assert result.status == vd.STATUS_SKIP
    assert seen == [_STALE_ENV_TOKEN]


def test_hub_allowlist_identical_tokens_make_one_request_each(monkeypatch, tmp_path):
    """LEAVE-ALONE: the pin is not stale — nothing extra happens."""
    monkeypatch.setattr(vd, "_on_disk_hub_token", lambda: _FRESH_DISK_TOKEN)
    monkeypatch.setenv("VCT_HUB_TOKEN", _FRESH_DISK_TOKEN)
    seen: list = []
    monkeypatch.setattr(vd, "_http_get_json", _hub_accepting(_FRESH_DISK_TOKEN, seen))
    result = vd._check_hub_allowlist("p-1")

    assert result.status == vd.STATUS_OK
    assert seen == [_FRESH_DISK_TOKEN, _FRESH_DISK_TOKEN]
