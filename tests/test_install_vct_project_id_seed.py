# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.49 SB1 — pin that install.py / vco_lib.config_projection seed
``VCT_PROJECT_ID`` into ``.claude/env`` from launcher.db when available.

Closes the Phase-8 access-matrix WRITE-gate silent-bypass: pre-SB1 the
gate at ``claude_mcp_servers/weaviate_mcp/server.py::store_knowledge_node``
+ the ``templates/hooks/post-file-edit.{sh,ps1}`` siblings returned
allow-without-audit when ``VCT_PROJECT_ID`` was empty. SB1 makes the
empty-PID branch emit a metric + UPDATE_DEFERRED.md entry, AND seeds
the env var at install time so the empty-PID branch only fires for
genuinely pre-v0.2.49 projects that haven't been re-registered.

Tests in this module pin the SEED PATH (not the gate-skipped surface
itself — those live in tests for server.py / the bash hook).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib.config_projection import (
    project_env_from_db,
    list_canonical_keys,
)


# ─── Helper — one project row on the REAL launcher.db schema ────────────


def _make_launcher_db(
    db_path: Path,
    *,
    project_id: str,
    project_name: str,
    project_folder: str,
    project_slug: str = "proj",
) -> None:
    """One-project launcher.db on the REAL (migration-applied) schema.

    The old inline DDL declared six tables by hand with a guessed column
    subset — ``projects`` without ``host`` / ``created_at`` / ``updated_at``,
    ``module_settings`` with a NOT NULL ``project_id`` that migration 034
    made nullable. ``project_env_from_db`` reads every one of those tables,
    so the guess was the thing under test as much as the code was. No KG
    bindings / access rows are seeded: these tests only assert
    ``VCT_PROJECT_ID`` reaches the canonical env.
    """
    make_launcher_db(
        db_path,
        projects=[{
            "project_id": project_id,
            "name": project_name,
            "folder_path": project_folder,
            "slug": project_slug,
        }],
    )


# ─── SB1 contract: VCT_PROJECT_ID lands in the canonical env ────────────


def test_canonical_keys_set_includes_vct_project_id() -> None:
    """The contract's closed key list MUST contain ``VCT_PROJECT_ID``.

    Other surfaces (the Rust ``CANONICAL_INSTALL_ENV_KEYS``, the
    `_write_shell_env_managed_block` writer, the unregister sweep)
    iterate this set to decide what to write / strip; missing here
    means VCT_PROJECT_ID would never reach disk.
    """
    keys = list_canonical_keys()
    assert "VCT_PROJECT_ID" in keys, (
        "VCT_PROJECT_ID missing from list_canonical_keys() — SB1 seed "
        "path is broken; the gate's empty-PID branch will fire on "
        "every v0.2.49 install"
    )


def test_project_env_from_db_emits_vct_project_id(tmp_path: Path) -> None:
    """``project_env_from_db`` MUST emit ``VCT_PROJECT_ID`` set to the
    launcher.db project UUID.

    This is the canonical SEED PATH: the launcher (and install.py's
    ``_backfill_code_graph_project_env``) call ``project_env_from_db``
    → ``apply_project_env`` to write ``.claude/env`` from authoritative
    DB state. Pre-SB1 the resulting env was missing VCT_PROJECT_ID,
    causing the gate's empty-PID branch to fire silently.
    """
    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "myproj"
    project_folder.mkdir()
    _make_launcher_db(
        db,
        project_id="abc-123-uuid",
        project_name="My Project",
        project_folder=str(project_folder),
    )

    bundle = project_env_from_db("abc-123-uuid", db_path=db)
    env = bundle["canonical_env"]
    assert env.get("VCT_PROJECT_ID") == "abc-123-uuid", (
        "VCT_PROJECT_ID not emitted by project_env_from_db — Phase-8 "
        "WRITE gate silent-bypass still active"
    )


def test_project_env_from_db_emits_project_id_with_special_chars(
    tmp_path: Path,
) -> None:
    """Edge case: project_id values containing characters that need
    shell escaping must round-trip cleanly. UUIDs typically don't have
    these but the contract should not pre-suppose UUIDv4 — the
    launcher could later allow user-provided identifiers.
    """
    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "p"
    project_folder.mkdir()
    pid = 'weird "id" with spaces'
    _make_launcher_db(
        db, project_id=pid, project_name="P", project_folder=str(project_folder),
    )

    env = project_env_from_db(pid, db_path=db)["canonical_env"]
    assert env.get("VCT_PROJECT_ID") == pid


def test_apply_project_env_writes_vct_project_id_to_claude_env(
    tmp_path: Path,
) -> None:
    """End-to-end: ``apply_project_env(bundle)`` writes
    ``VCT_PROJECT_ID="..."`` to ``.claude/env`` between the managed
    block markers.

    This is the single-writer assertion: hooks and the MCP server read
    VCT_PROJECT_ID from `.claude/env` (sourced into env) or directly
    from `${PROJECT_ROOT}/.claude/env` parsing. If the contract writes
    it here, every downstream consumer is correct by construction.
    """
    from vco_lib.config_projection import apply_project_env

    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    _make_launcher_db(
        db,
        project_id="seed-test-uuid",
        project_name="SeedTest",
        project_folder=str(project_folder),
    )

    # Resolve from DB → write surfaces.
    bundle = project_env_from_db("seed-test-uuid", db_path=db)
    # Override project_root so .claude/env writes land in tmp_path,
    # not in /tmp/demo / wherever the DB folder_path points.
    bundle = dict(bundle)
    bundle["project_root"] = project_folder
    apply_project_env(bundle, surfaces=["claude_env"])

    env_file = project_folder / ".claude" / "env"
    assert env_file.exists(), ".claude/env was not written"

    body = env_file.read_text(encoding="utf-8")
    assert 'export VCT_PROJECT_ID="seed-test-uuid"' in body, (
        f"VCT_PROJECT_ID export missing from .claude/env body:\n{body}"
    )


def test_apply_project_env_settings_json_includes_vct_project_id(
    tmp_path: Path,
) -> None:
    """Mirror of the above for ``.claude/settings.json``'s env block:
    the canonical key MUST also appear in the JSON surface so VS Code
    + the launcher both see the same value on every editor.
    """
    import json
    from vco_lib.config_projection import apply_project_env

    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    _make_launcher_db(
        db,
        project_id="json-seed-uuid",
        project_name="JsonSeed",
        project_folder=str(project_folder),
    )

    bundle = project_env_from_db("json-seed-uuid", db_path=db)
    bundle = dict(bundle)
    bundle["project_root"] = project_folder
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    settings_file = project_folder / ".claude" / "settings.json"
    assert settings_file.exists(), ".claude/settings.json was not written"

    parsed = json.loads(settings_file.read_text(encoding="utf-8"))
    assert parsed["env"]["VCT_PROJECT_ID"] == "json-seed-uuid", (
        "VCT_PROJECT_ID missing from .claude/settings.json env block — "
        "Phase-8 gate's empty-PID branch will fire for VS Code users"
    )


# ─── install.py path: _emit_orchestrator_root_env_keys backfill ─────────


def test_install_emit_orchestrator_root_env_keys_includes_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install.py::_emit_orchestrator_root_env_keys`` is called on
    every orchestrator-root install. It writes the 3 portability keys
    (VCT_ORCHESTRATOR_ROOT, VCT_INFRASTRUCTURE_DIR, KG_BASE_DIR)
    directly to .claude/env BEFORE the launcher's first boot.

    SB1: when launcher.db already has the orchestrator-root project
    row (re-install / --update path), the helper must also resolve
    its project_id and emit VCT_PROJECT_ID. Without this, an --update
    pass leaves .claude/env without the key until the next launcher
    boot's apply_project_env pass — the gate's empty-PID branch fires
    in the meantime.
    """
    # Stage a fake launcher.db with one project row whose folder_path
    # matches the install_root we're about to feed.
    install_root = tmp_path / "myorch"
    install_root.mkdir()

    # The launcher.db lives at ~/.vct/launcher.db by default — point
    # VCT_STATE_DIR at tmp_path so _resolve_project_id_by_folder
    # discovers OUR test DB.
    state_dir = tmp_path / "vct_state"
    state_dir.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))

    db_path = state_dir / "launcher.db"
    _make_launcher_db(
        db_path,
        project_id="install-uuid-001",
        project_name="MyOrch",
        project_folder=str(install_root.resolve()),
    )

    # Import after env is set so the function picks up VCT_STATE_DIR.
    import install
    install._emit_orchestrator_root_env_keys(install_root)

    env_file = install_root / ".claude" / "env"
    assert env_file.exists(), ".claude/env was not written"

    body = env_file.read_text(encoding="utf-8")
    assert 'export VCT_PROJECT_ID="install-uuid-001"' in body, (
        f"install.py did not seed VCT_PROJECT_ID into .claude/env. "
        f"Body:\n{body}"
    )
    # The portability keys must still land (we didn't regress them).
    assert 'export VCT_ORCHESTRATOR_ROOT=' in body
    assert 'export VCT_INFRASTRUCTURE_DIR=' in body
    assert 'export KG_BASE_DIR=' in body


def test_install_emit_orchestrator_root_env_keys_no_db_skips_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh install: launcher.db doesn't exist yet. The helper still
    writes the 3 portability keys but VCT_PROJECT_ID stays absent —
    the gate's empty-PID branch will fire at first WRITE (correctly,
    per design) and the SB1 deferral guides the user.

    Once the launcher boots + the user opens the orchestrator project,
    the launcher's apply_project_env pass backfills VCT_PROJECT_ID via
    project_env_from_db (covered by the test above).
    """
    install_root = tmp_path / "myorch"
    install_root.mkdir()
    state_dir = tmp_path / "vct_state_empty"  # exists but no launcher.db
    state_dir.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))

    import install
    install._emit_orchestrator_root_env_keys(install_root)

    env_file = install_root / ".claude" / "env"
    assert env_file.exists()
    body = env_file.read_text(encoding="utf-8")
    # Portability keys present.
    assert 'export VCT_ORCHESTRATOR_ROOT=' in body
    # SB1: when no DB, VCT_PROJECT_ID is OMITTED (the gate's empty-PID
    # branch will fire at runtime and surface the deferral).
    assert 'export VCT_PROJECT_ID' not in body, (
        "No launcher.db means no project_id to seed; helper must NOT "
        "fabricate one. The gate-skipped deferral surface is the "
        "correct UX here."
    )
