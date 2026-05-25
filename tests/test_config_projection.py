# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``vco_lib.config_projection`` (Phase 0.B contract).

Covers:

  1. :func:`project_env_from_db` — pure DB resolver, all canonical keys
     present / conditionally-omitted, sanitization round-trip.
  2. :func:`apply_project_env` — three surface writers, deep-merge
     preservation of user keys (Bug-4 regression guard), atomic write
     discipline, BEGIN/END marker idempotence.
  3. CLI entry points — `apply`, `list-keys`, `from-db` happy paths +
     error envelopes.
  4. Cross-OS atomicity — tempfile lands in target dir, ``os.replace``
     overwrites.

The byte-identical-to-Rust parity test lives in
``tests/test_config_projection_byte_identical.py`` (separate file so
this one stays fast and Rust-toolchain-free).

Run: pytest tests/test_config_projection.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib.config_projection import (
    CLAUDE_ENV_MANAGED_BEGIN,
    CLAUDE_ENV_MANAGED_END,
    ConfigProjectionError,
    DbUnreachable,
    ProjectNotFound,
    apply_project_env,
    list_canonical_keys,
    project_env_from_db,
)


# ─── DB fixture ─────────────────────────────────────────────────────────


def _make_launcher_db(
    db_path: Path,
    *,
    project_id: str = "proj-001",
    project_name: str = "Demo Project",
    project_folder: str = "/tmp/demo",
    project_slug: str = "demo-project",
    extra_projects: list[tuple[str, str, str, str]] | None = None,
    kg_bindings: dict[str, str] | None = None,
    kg_access: list[tuple[str, str]] | None = None,
    codegraph_access: list[tuple[str, str]] | None = None,
    diagram_access: list[tuple[str, str]] | None = None,
    module_settings: list[tuple[str, str, str, str]] | None = None,
) -> None:
    """Build a minimal launcher.db with the schema this module reads.

    Tables created: projects, project_kg_bindings, kg_collection_access,
    codegraph_access, module_settings. Schema mirrors the migrations in
    ``launcher/src-tauri/vct-launcher-core/src/db/migrations/`` (just
    enough columns for the resolver — not the full schema).

    Args:
        kg_bindings: ``{role: collection_name}`` rows to insert for
            ``project_id``.
        kg_access: list of (collection_name, access_level) rows for
            ``project_id``.
        codegraph_access: list of (grantor_project_id, access_level)
            rows where ``project_id`` is the grantee.
        diagram_access: list of (grantor_project_id, access_level)
            rows where ``project_id`` is the grantee (v0.2.34 A7 —
            sibling of codegraph_access but reads grantor.name for
            the env-side CSV value).
        module_settings: list of (project_id, module_id, key, value)
            with ``value`` being a JSON string.
        extra_projects: additional (id, name, folder_path, slug) rows
            for cross-project tests (e.g. codegraph_access joins).
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            slug TEXT NOT NULL
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT,
            PRIMARY KEY (project_id, role)
        );
        CREATE TABLE kg_collection_access (
            project_id TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            access_level TEXT NOT NULL,
            PRIMARY KEY (project_id, collection_name)
        );
        CREATE TABLE codegraph_access (
            grantor_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grantor_project_id, grantee_project_id)
        );
        CREATE TABLE diagram_access (
            grantor_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grantor_project_id, grantee_project_id)
        );
        CREATE TABLE module_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            module_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            UNIQUE(project_id, module_id, setting_key)
        );
        """
    )
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
        (project_id, project_name, project_folder, project_slug),
    )
    for row in extra_projects or []:
        cur.execute(
            "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
            row,
        )
    for role, coll in (kg_bindings or {}).items():
        cur.execute(
            "INSERT INTO project_kg_bindings (project_id, role, collection_name) "
            "VALUES (?, ?, ?)",
            (project_id, role, coll),
        )
    for coll, level in kg_access or []:
        cur.execute(
            "INSERT INTO kg_collection_access (project_id, collection_name, access_level) "
            "VALUES (?, ?, ?)",
            (project_id, coll, level),
        )
    for grantor, level in codegraph_access or []:
        cur.execute(
            "INSERT INTO codegraph_access (grantor_project_id, grantee_project_id, "
            "access_level, granted_at) VALUES (?, ?, ?, ?)",
            (grantor, project_id, level, 0),
        )
    for grantor, level in diagram_access or []:
        cur.execute(
            "INSERT INTO diagram_access (grantor_project_id, grantee_project_id, "
            "access_level, granted_at) VALUES (?, ?, ?, ?)",
            (grantor, project_id, level, 0),
        )
    for pid, mid, key, value in module_settings or []:
        cur.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value) "
            "VALUES (?, ?, ?, ?)",
            (pid, mid, key, value),
        )
    conn.commit()
    conn.close()


# ─── project_env_from_db tests ──────────────────────────────────────────


def test_from_db_minimal_project(tmp_path: Path) -> None:
    """A bare project (no bindings, no access matrix, no settings) yields
    a bundle with the canonical defaults and sanitized derived names."""
    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    _make_launcher_db(
        db, project_id="p1", project_name="My App",
        project_folder=str(project_folder), project_slug="my-app",
    )

    bundle = project_env_from_db("p1", db_path=db)

    env = bundle["canonical_env"]
    assert bundle["project_id"] == "p1"
    assert bundle["project_root"] == project_folder
    # Defaults derived from sanitized name.
    assert env["PROJECT_NAME"] == "My App"
    assert env["CODE_GRAPH_PROJECT"] == "MyApp"
    assert env["KG_COLLECTION"] == "MyApp_KnowledgeGraph"
    assert env["DEVELOPMENT_COLLECTION"] == "MyApp_Development"
    assert env["SHARED_KG_COLLECTION"] == "VibeCodedOrchestrator_KnowledgeGraph"
    assert env["SHARED_KG_WRITE_DISABLED"] == "false"
    assert env["SHARED_KG_OPT_OUT"] == "false"
    assert env["ACTIVE_EMBEDDING"] == "qwen3"
    assert env["WEAVIATE_URL"] == "http://localhost:8081"
    assert env["WEAVIATE_PORT"] == "8081"
    assert env["OLLAMA_URL"] == "http://localhost:11435"
    assert env["OLLAMA_PORT"] == "11435"
    assert env["CODE_EMBED_URL"] == "http://localhost:11440"
    assert env["CODE_EMBED_PORT"] == "11440"
    # Conditional keys are absent (no peers granted, no orchestrator root).
    assert "VCT_KG_ACCESS_LIST" not in env
    assert "VCT_CODE_GRAPH_ACCESS_LIST" not in env
    # v0.2.34 A7 — diagrams access is independent; no peers ⇒ no key.
    assert "VCT_DIAGRAMS_ACCESS_LIST" not in env
    assert "VCT_ORCHESTRATOR_ROOT" not in env
    assert "VCT_INFRASTRUCTURE_DIR" not in env
    # GITHUB_TOKEN never resolved by this contract.
    assert "GITHUB_TOKEN" not in env


def test_from_db_with_kg_bindings(tmp_path: Path) -> None:
    """KG binding rows override sanitized-name defaults."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        kg_bindings={
            "primary": "MyKnowledgeGraph",
            "shared": "TeamSharedKG",
            "archive": "MyDev",
        },
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["KG_COLLECTION"] == "MyKnowledgeGraph"
    assert env["SHARED_KG_COLLECTION"] == "TeamSharedKG"
    assert env["DEVELOPMENT_COLLECTION"] == "MyDev"


def test_from_db_kg_access_list_strips_self_and_shared(tmp_path: Path) -> None:
    """``VCT_KG_ACCESS_LIST`` excludes the project's own and shared
    collections; peer prefix is the collection name minus the
    ``_KnowledgeGraph`` / ``_Development`` suffix."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        kg_bindings={
            "primary": "X_KnowledgeGraph",
            "shared": "VibeCodedOrchestrator_KnowledgeGraph",
            "archive": "X_Development",
        },
        kg_access=[
            # Self + shared — should be stripped.
            ("X_KnowledgeGraph", "write"),
            ("X_Development", "read"),
            ("VibeCodedOrchestrator_KnowledgeGraph", "read"),
            # Peers — should appear (suffix stripped → prefix).
            ("Foo_KnowledgeGraph", "read"),
            ("Bar_Development", "read"),
            # Excluded by access level.
            ("Baz_KnowledgeGraph", "none"),
        ],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["VCT_KG_ACCESS_LIST"] == "Bar,Foo"


def test_from_db_codegraph_access_list(tmp_path: Path) -> None:
    """``VCT_CODE_GRAPH_ACCESS_LIST`` carries the grantor project slugs
    (sorted+deduped, no self)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="grantee", project_name="G",
        project_folder=str(proj), project_slug="grantee",
        extra_projects=[
            ("a", "Alpha", "/tmp/a", "alpha"),
            ("b", "Beta", "/tmp/b", "beta"),
        ],
        codegraph_access=[
            ("a", "read"),
            ("b", "read"),
        ],
    )
    env = project_env_from_db("grantee", db_path=db)["canonical_env"]
    assert env["VCT_CODE_GRAPH_ACCESS_LIST"] == "alpha,beta"


def test_from_db_diagram_access_list_emits_grantor_names(tmp_path: Path) -> None:
    """v0.2.34 A7: ``VCT_DIAGRAMS_ACCESS_LIST`` carries grantor project
    NAMES (display names — distinct from VCT_CODE_GRAPH_ACCESS_LIST
    which carries slugs)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="grantee", project_name="Grantee",
        project_folder=str(proj), project_slug="grantee",
        extra_projects=[
            ("a", "Alpha Project", "/tmp/a", "alpha"),
            ("b", "Beta", "/tmp/b", "beta"),
        ],
        diagram_access=[
            ("a", "read"),
            ("b", "read"),
        ],
    )
    env = project_env_from_db("grantee", db_path=db)["canonical_env"]
    # Note: names sorted alphabetically; the MCP-side sanitiser handles
    # the space in "Alpha Project" by replacing it with `_`.
    assert env["VCT_DIAGRAMS_ACCESS_LIST"] == "Alpha Project,Beta"


def test_from_db_diagram_access_independent_of_kg_access(tmp_path: Path) -> None:
    """KG-only grants must NOT populate ``VCT_DIAGRAMS_ACCESS_LIST``;
    diagram-only grants must NOT populate ``VCT_KG_ACCESS_LIST``.

    This is the v0.2.34 A7 bug-fix regression guard. Pre-A7 the MCP fell
    back to VCT_KG_ACCESS_LIST when VCT_DIAGRAMS_ACCESS_LIST was unset,
    which meant (1) granting KG access leaked diagram visibility and
    (2) granting only diagram access was invisible to the MCP because
    no KG row existed to piggyback on. The split contract here makes
    each surface track its own access matrix."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        kg_bindings={
            "primary": "X_KnowledgeGraph",
            "shared": "VibeCodedOrchestrator_KnowledgeGraph",
            "archive": "X_Development",
        },
        # KG access only — diagram access not granted.
        kg_access=[("Peer_KnowledgeGraph", "read")],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["VCT_KG_ACCESS_LIST"] == "Peer"
    assert "VCT_DIAGRAMS_ACCESS_LIST" not in env


def test_from_db_diagram_only_grant_populates_only_diagrams_list(
    tmp_path: Path,
) -> None:
    """Diagram-only grant: VCT_DIAGRAMS_ACCESS_LIST populated, KG list
    absent. Pre-v0.2.34 A7 this case was completely invisible to the
    MCP (it would not surface the peer)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        extra_projects=[("a", "Alpha", "/tmp/a", "alpha")],
        diagram_access=[("a", "read")],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["VCT_DIAGRAMS_ACCESS_LIST"] == "Alpha"
    assert "VCT_KG_ACCESS_LIST" not in env


def test_from_db_diagram_and_kg_grants_populate_both(tmp_path: Path) -> None:
    """Both grants present → both env keys present, independently."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        kg_bindings={
            "primary": "X_KnowledgeGraph",
            "shared": "VibeCodedOrchestrator_KnowledgeGraph",
            "archive": "X_Development",
        },
        extra_projects=[("a", "Alpha", "/tmp/a", "alpha")],
        kg_access=[("Foo_KnowledgeGraph", "read")],
        diagram_access=[("a", "read")],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["VCT_KG_ACCESS_LIST"] == "Foo"
    assert env["VCT_DIAGRAMS_ACCESS_LIST"] == "Alpha"


def test_from_db_diagram_access_level_none_filtered(tmp_path: Path) -> None:
    """``access_level='none'`` rows must not appear in the env var
    (mirrors the kg_access_list filtering rule)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        extra_projects=[
            ("a", "Alpha", "/tmp/a", "alpha"),
            ("b", "Beta", "/tmp/b", "beta"),
        ],
        diagram_access=[
            ("a", "read"),
            ("b", "none"),
        ],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["VCT_DIAGRAMS_ACCESS_LIST"] == "Alpha"
    assert "Beta" not in env["VCT_DIAGRAMS_ACCESS_LIST"]


def test_apply_diagrams_access_list_signal_to_remove(tmp_path: Path) -> None:
    """A canonical key absent from the bundle but present in the existing
    surface is DELETED (signal-to-remove semantics). The diagrams list
    needs the same behaviour as the KG list so a grant-revoke flow
    actually wipes the env var."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "KG_COLLECTION": "TestKG",
            "VCT_DIAGRAMS_ACCESS_LIST": "OldPeer1,OldPeer2",  # stale
            "OPENAI_API_BASE": "preserved",
        },
    }))

    bundle = _bundle(tmp_path)  # no VCT_DIAGRAMS_ACCESS_LIST in bundle
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert "VCT_DIAGRAMS_ACCESS_LIST" not in data["env"]
    assert data["env"]["OPENAI_API_BASE"] == "preserved"


def test_from_db_shared_kg_write_disabled(tmp_path: Path) -> None:
    """``shared_kg_write_disabled`` reads from module_settings →
    orchestrator-core."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
        module_settings=[
            ("x", "orchestrator-core", "shared_kg_write_disabled", "true"),
        ],
    )
    env = project_env_from_db("x", db_path=db)["canonical_env"]
    assert env["SHARED_KG_WRITE_DISABLED"] == "true"
    assert env["SHARED_KG_OPT_OUT"] == "true"  # legacy alias mirrors


def test_from_db_orchestrator_root_emits_portability_keys(tmp_path: Path) -> None:
    """Passing ``orchestrator_root`` emits the two portability keys."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    orch = tmp_path / "vco-clone"
    orch.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj),
    )
    env = project_env_from_db(
        "x", db_path=db, orchestrator_root=orch,
    )["canonical_env"]
    assert env["VCT_ORCHESTRATOR_ROOT"] == str(orch)
    assert env["VCT_INFRASTRUCTURE_DIR"] == str(orch / "infrastructure")


def test_from_db_project_not_found(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="real", project_folder=str(proj))
    with pytest.raises(ProjectNotFound):
        project_env_from_db("ghost", db_path=db)


def test_from_db_missing_db_file(tmp_path: Path) -> None:
    with pytest.raises(DbUnreachable):
        project_env_from_db("x", db_path=tmp_path / "no-such.db")


# ─── apply_project_env tests ────────────────────────────────────────────


def _bundle(project_root: Path, **env_overrides: str) -> dict:
    """Build a bundle with sensible defaults for surface-writer tests."""
    env = {
        "KG_COLLECTION": "TestKG",
        "DEVELOPMENT_COLLECTION": "TestDev",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        "SHARED_KG_WRITE_DISABLED": "false",
        "SHARED_KG_OPT_OUT": "false",
        "PROJECT_NAME": "Test",
        "CODE_GRAPH_PROJECT": "Test",
        "ACTIVE_EMBEDDING": "qwen3",
        "WEAVIATE_URL": "http://localhost:8081",
        "WEAVIATE_PORT": "8081",
        "OLLAMA_URL": "http://localhost:11435",
        "OLLAMA_PORT": "11435",
        "CODE_EMBED_URL": "http://localhost:11440",
        "CODE_EMBED_PORT": "11440",
    }
    env.update(env_overrides)
    return {
        "canonical_env": env,
        "project_id": "test-id",
        "project_root": project_root,
    }


def test_apply_creates_claude_settings_json_fresh(tmp_path: Path) -> None:
    """No existing settings.json → fresh file with env sub-block."""
    bundle = _bundle(tmp_path)
    report = apply_project_env(bundle, surfaces=["claude_settings_json"])

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert "env" in data
    assert data["env"]["KG_COLLECTION"] == "TestKG"
    assert data["env"]["WEAVIATE_PORT"] == "8081"
    assert "KG_COLLECTION" in report["claude_settings_json"]


def test_apply_preserves_user_keys_in_settings_json(tmp_path: Path) -> None:
    """Deep-merge: canonical keys overwritten, user keys preserved.

    THIS IS THE BUG-4 REGRESSION GUARD. Pre-PR-145 the entire env sub-
    block was replaced wholesale, silently dropping any user-added key
    at that level. We assert here that a user's OPENAI_API_BASE
    override survives a config_projection apply."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "KG_COLLECTION": "StaleValue",       # canonical — must be overwritten
            "OPENAI_API_BASE": "https://custom", # user-added — must survive
            "MY_DEBUG_FLAG": "1",                # user-added — must survive
        },
        "hooks": {"PreToolUse": []},             # sibling block — must survive
    }, indent=2))

    bundle = _bundle(tmp_path, KG_COLLECTION="FreshValue")
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert data["env"]["KG_COLLECTION"] == "FreshValue"
    assert data["env"]["OPENAI_API_BASE"] == "https://custom"
    assert data["env"]["MY_DEBUG_FLAG"] == "1"
    assert data["hooks"] == {"PreToolUse": []}


def test_apply_removes_omitted_canonical_keys(tmp_path: Path) -> None:
    """A canonical key absent from the bundle but present in the
    existing surface is DELETED (signal-to-remove semantics)."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "KG_COLLECTION": "TestKG",
            "VCT_KG_ACCESS_LIST": "OldPeer1,OldPeer2",  # stale; bundle omits it
            "OPENAI_API_BASE": "preserved",
        },
    }))

    bundle = _bundle(tmp_path)  # no VCT_KG_ACCESS_LIST in bundle
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert "VCT_KG_ACCESS_LIST" not in data["env"]
    assert data["env"]["OPENAI_API_BASE"] == "preserved"


def test_apply_creates_claude_env_with_bracket_markers(tmp_path: Path) -> None:
    """``.claude/env`` is created with BEGIN/END markers and export lines."""
    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_env"])

    env_path = tmp_path / ".claude" / "env"
    text = env_path.read_text()
    assert CLAUDE_ENV_MANAGED_BEGIN in text
    assert CLAUDE_ENV_MANAGED_END in text
    assert 'export KG_COLLECTION="TestKG"' in text
    assert 'export ACTIVE_EMBEDDING="qwen3"' in text


def test_apply_claude_env_preserves_user_lines_outside_markers(
    tmp_path: Path,
) -> None:
    """Lines outside the BEGIN/END markers are preserved byte-for-byte."""
    env_path = tmp_path / ".claude" / "env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# user added this on 2026-01-15\n"
        'export MY_OVERRIDE="custom"\n'
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        "# old managed content\n"
        'export KG_COLLECTION="StaleKG"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
        "# user trailer comment\n"
    )

    bundle = _bundle(tmp_path, KG_COLLECTION="FreshKG")
    apply_project_env(bundle, surfaces=["claude_env"])

    text = env_path.read_text()
    # User content above markers preserved.
    assert text.startswith("# user added this on 2026-01-15\n")
    assert 'export MY_OVERRIDE="custom"' in text
    # Managed content replaced.
    assert 'export KG_COLLECTION="FreshKG"' in text
    assert "StaleKG" not in text
    # User trailer preserved.
    assert "# user trailer comment" in text


def test_apply_claude_env_idempotent(tmp_path: Path) -> None:
    """Two applies in a row produce byte-identical output."""
    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_env"])
    first = (tmp_path / ".claude" / "env").read_bytes()
    apply_project_env(bundle, surfaces=["claude_env"])
    second = (tmp_path / ".claude" / "env").read_bytes()
    assert first == second


def test_apply_settings_json_idempotent(tmp_path: Path) -> None:
    """Two applies in a row produce byte-identical settings.json."""
    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])
    first = (tmp_path / ".claude" / "settings.json").read_bytes()
    apply_project_env(bundle, surfaces=["claude_settings_json"])
    second = (tmp_path / ".claude" / "settings.json").read_bytes()
    assert first == second


def test_apply_settings_json_malformed_existing_resets(tmp_path: Path) -> None:
    """A malformed existing settings.json is replaced with a fresh object."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text("not-valid-json{{{")

    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert data["env"]["KG_COLLECTION"] == "TestKG"


def test_apply_settings_json_non_object_root_resets(tmp_path: Path) -> None:
    """A non-object root JSON value is replaced with a fresh object."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text('["array", "instead", "of", "object"]')

    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert isinstance(data, dict)
    assert data["env"]["KG_COLLECTION"] == "TestKG"


def test_apply_vscode_surface_opt_in(tmp_path: Path) -> None:
    """The .vscode/settings.json surface is opt-in via the ``surfaces`` arg."""
    bundle = _bundle(tmp_path)
    # Default surfaces: vscode NOT included.
    apply_project_env(bundle)
    assert not (tmp_path / ".vscode" / "settings.json").exists()

    # Explicit opt-in.
    apply_project_env(bundle, surfaces=["vscode_settings_json"])
    vscode = tmp_path / ".vscode" / "settings.json"
    assert vscode.exists()
    data = json.loads(vscode.read_text())
    assert data["claude-code.env"]["KG_COLLECTION"] == "TestKG"


def test_apply_vscode_preserves_user_keys(tmp_path: Path) -> None:
    """The ``claude-code.env`` sub-block deep-merge preserves user keys."""
    vscode = tmp_path / ".vscode" / "settings.json"
    vscode.parent.mkdir()
    vscode.write_text(json.dumps({
        "editor.fontSize": 14,
        "claude-code.env": {
            "KG_COLLECTION": "Stale",
            "MY_VSCODE_VAR": "preserved",
        },
        "python.pythonPath": "/usr/bin/python3",
    }, indent=2))

    bundle = _bundle(tmp_path, KG_COLLECTION="Fresh")
    apply_project_env(bundle, surfaces=["vscode_settings_json"])

    data = json.loads(vscode.read_text())
    assert data["claude-code.env"]["KG_COLLECTION"] == "Fresh"
    assert data["claude-code.env"]["MY_VSCODE_VAR"] == "preserved"
    assert data["editor.fontSize"] == 14
    assert data["python.pythonPath"] == "/usr/bin/python3"


def test_apply_unknown_surface_raises(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ConfigProjectionError, match="unknown surface"):
        apply_project_env(bundle, surfaces=["bogus"])


def test_apply_returns_audit_report(tmp_path: Path) -> None:
    """The returned dict lists which canonical keys landed per surface."""
    bundle = _bundle(tmp_path)
    report = apply_project_env(bundle, surfaces=["claude_settings_json", "claude_env"])
    assert set(report.keys()) == {"claude_settings_json", "claude_env"}
    assert "KG_COLLECTION" in report["claude_settings_json"]
    assert "KG_COLLECTION" in report["claude_env"]
    # Audit is sorted for deterministic logging.
    assert report["claude_settings_json"] == sorted(report["claude_settings_json"])


def test_apply_writes_atomic_no_tempfile_leak(tmp_path: Path) -> None:
    """After a successful apply, no .tmp files remain in target dirs."""
    bundle = _bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json", "claude_env"])
    stragglers = list((tmp_path / ".claude").glob("*.tmp")) + list(
        (tmp_path / ".claude").glob("*.tmp*")
    )
    assert not stragglers, f"tempfile leak: {stragglers}"


def test_apply_escapes_double_quotes_in_shell_env(tmp_path: Path) -> None:
    """Values containing double quotes are backslash-escaped in
    ``.claude/env``. Rare on POSIX; legitimate on Windows paths."""
    bundle = _bundle(tmp_path, PROJECT_NAME='Foo "Bar" Baz')
    apply_project_env(bundle, surfaces=["claude_env"])
    text = (tmp_path / ".claude" / "env").read_text()
    assert r'export PROJECT_NAME="Foo \"Bar\" Baz"' in text


# ─── CLI tests ──────────────────────────────────────────────────────────


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m vco_lib.config_projection`` and capture output."""
    cmd = [sys.executable, "-m", "vco_lib.config_projection", *args]
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    # Ensure the repo's vco_lib is importable.
    repo_root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_cli_list_keys_json() -> None:
    result = _run_cli("list-keys", "--json")
    assert result.returncode == 0, result.stderr
    keys = json.loads(result.stdout)
    assert "KG_COLLECTION" in keys
    assert "VCT_KG_ACCESS_LIST" in keys
    # Sorted output for deterministic auditing.
    assert keys == sorted(keys)


def test_cli_list_keys_plain() -> None:
    result = _run_cli("list-keys")
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert "KG_COLLECTION" in lines


def test_cli_from_db_happy(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="x", project_name="X", project_folder=str(proj))
    result = _run_cli("from-db", "--project-id", "x", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["project_id"] == "x"
    assert out["canonical_env"]["KG_COLLECTION"] == "X_KnowledgeGraph"


def test_cli_from_db_project_not_found_exits_2(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="real", project_folder=str(proj))
    result = _run_cli("from-db", "--project-id", "ghost", "--db-path", str(db))
    assert result.returncode == 2
    err = json.loads(result.stderr)
    assert err["error"] == "project_not_found"


def test_cli_from_db_missing_db_exits_3(tmp_path: Path) -> None:
    result = _run_cli(
        "from-db", "--project-id", "x", "--db-path", str(tmp_path / "no.db")
    )
    assert result.returncode == 3
    err = json.loads(result.stderr)
    assert err["error"] == "db_unreachable"


def test_cli_apply_writes_surfaces(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="x", project_name="X", project_folder=str(proj))
    result = _run_cli(
        "apply",
        "--project-id", "x",
        "--db-path", str(db),
        "--surfaces", "claude_settings_json,claude_env",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert "claude_settings_json" in out["report"]
    assert "claude_env" in out["report"]
    # Files actually exist.
    assert (proj / ".claude" / "settings.json").exists()
    assert (proj / ".claude" / "env").exists()


# ─── list_canonical_keys ────────────────────────────────────────────────


def test_canonical_keys_includes_expected() -> None:
    """The closed set includes every key the legacy Rust writer manages."""
    keys = list_canonical_keys()
    # Direct-write targets called out in the Phase 0.B brief.
    assert "VCT_KG_ACCESS_LIST" in keys
    assert "VCT_CODE_GRAPH_ACCESS_LIST" in keys
    assert "SHARED_KG_WRITE_DISABLED" in keys
    # v0.2.34 A7 — diagrams cross-project access list.
    assert "VCT_DIAGRAMS_ACCESS_LIST" in keys
    # Foundational keys.
    for k in ("KG_COLLECTION", "PROJECT_NAME", "ACTIVE_EMBEDDING"):
        assert k in keys


def test_canonical_keys_returns_fresh_set() -> None:
    """Each call returns a fresh set so mutation doesn't leak."""
    a = list_canonical_keys()
    a.add("FAKE_KEY")
    b = list_canonical_keys()
    assert "FAKE_KEY" not in b
