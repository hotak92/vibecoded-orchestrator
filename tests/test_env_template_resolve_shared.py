# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.40 W40-C: pin the launcher.db-driven SHARED_KG fallback resolver.

Before v0.2.40, ``project_env_from_db`` (and its
``project_env_template_from_db`` projection) used a hardcoded
``shared_kg_default="VibeCodedOrchestrator_KnowledgeGraph"`` parameter
as the fallback when a project had no ``shared`` KG binding row. The
issue: when the canonical name flipped across releases (v0.2.12 PR-26,
v0.2.23 B1), users who'd been on the old canonical got stranded behind
a stale const default until their launcher.db happened to be re-synced.

W40-C makes the fallback DB-driven:

  * ``shared_kg_default=None`` (the new default) triggers a read of
    ``project_kg_bindings(slug='orchestrator-root', role='primary').
    collection_name`` from launcher.db. That value is the source of
    truth for the shared-KG name on every machine that has run the
    launcher at least once.
  * Explicit string overrides still bypass the DB-read.
  * Soft-fail throughout: DB missing / unreadable / orchestrator-root
    row absent / binding empty → falls back to the bundled
    ``_LAST_RESORT_SHARED_KG_NAME`` const (same value as the prior
    hardcoded default — matches the Rust
    ``LAST_RESORT_SHARED_KG_COLLECTION``).

This test pins all three branches of the soft-fall-through chain so any
future regression on the DB-read priority chain trips CI.

Cross-language invariant: the Python ``_LAST_RESORT_SHARED_KG_NAME``
const must equal the Rust ``LAST_RESORT_SHARED_KG_COLLECTION`` const +
the ``vco_lib.project_init._SHARED_KG_NAME`` const. That invariant is
pinned by ``tests/test_shared_kg_constant_consistency.py``.

Run: pytest tests/test_env_template_resolve_shared.py -v
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.config_projection import (  # noqa: E402
    _LAST_RESORT_SHARED_KG_NAME,
    _resolve_shared_kg_default_from_launcher_db,
    project_env_from_db,
)
from vco_lib.env_template import project_env_template_from_db  # noqa: E402


# ─── DB fixture ─────────────────────────────────────────────────────────


def _make_db(
    db_path: Path,
    *,
    orchestrator_primary_name: str | None = None,
) -> None:
    """Build a minimal launcher.db.

    The schema must match what ``_resolve_shared_kg_default_from_launcher_db``
    reads (``projects.id``, ``projects.slug``,
    ``project_kg_bindings.project_id``, ``.role``, ``.collection_name``).

    When ``orchestrator_primary_name`` is provided, seeds an
    ``orchestrator-root`` project row + a primary binding row pointing
    at the given name. ``None`` → no orchestrator-root row (forces the
    last-resort fallback).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        # Schema must mirror the production launcher.db tables the
        # resolver reads. Keep it minimal — only the columns the
        # resolver SELECTs.
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
            """
        )
        if orchestrator_primary_name is not None:
            cur.execute(
                "INSERT INTO projects (id, name, folder_path, slug) "
                "VALUES (?, ?, ?, ?)",
                (
                    "root-id-001",
                    "VibeCoded Orchestrator",
                    "/tmp/fake-orch-root",
                    "orchestrator-root",
                ),
            )
            cur.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name) VALUES (?, ?, ?)",
                ("root-id-001", "primary", orchestrator_primary_name),
            )
        conn.commit()
    finally:
        conn.close()


def _make_db_with_project(
    db_path: Path,
    *,
    project_id: str,
    project_name: str,
    project_folder: str,
    project_slug: str,
    orchestrator_primary_name: str | None = None,
    project_shared_binding: str | None = None,
) -> None:
    """Build a launcher.db with a TARGET project row + optional
    orchestrator-root seed.

    Mirrors ``_make_launcher_db`` from ``test_env_template.py`` but
    keeps schema minimal (only the columns the W40-C resolver reads).
    Used for the integration tests that round-trip through
    ``project_env_from_db`` / ``project_env_template_from_db``.
    """
    conn = sqlite3.connect(str(db_path))
    try:
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
            "INSERT INTO projects (id, name, folder_path, slug) "
            "VALUES (?, ?, ?, ?)",
            (project_id, project_name, project_folder, project_slug),
        )
        if project_shared_binding is not None:
            cur.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name) VALUES (?, ?, ?)",
                (project_id, "shared", project_shared_binding),
            )
        if orchestrator_primary_name is not None:
            cur.execute(
                "INSERT INTO projects (id, name, folder_path, slug) "
                "VALUES (?, ?, ?, ?)",
                (
                    "root-id-001",
                    "VibeCoded Orchestrator",
                    "/tmp/fake-orch-root-2",
                    "orchestrator-root",
                ),
            )
            cur.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name) VALUES (?, ?, ?)",
                ("root-id-001", "primary", orchestrator_primary_name),
            )
        conn.commit()
    finally:
        conn.close()


# ─── _resolve_shared_kg_default_from_launcher_db direct tests ────────────


class TestResolverDirect:
    """Unit-level coverage of the W40-C resolver helper itself."""

    def test_returns_orchestrator_root_primary_when_present(
        self, tmp_path: Path
    ) -> None:
        """Happy path: orchestrator-root row exists, primary binding
        is non-empty → resolver returns the binding's collection_name
        (NOT the const)."""
        db = tmp_path / "launcher.db"
        _make_db(db, orchestrator_primary_name="MyCustom_KnowledgeGraph")
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == "MyCustom_KnowledgeGraph"
        # Defensive: must NOT be the bundled const.
        assert out != _LAST_RESORT_SHARED_KG_NAME

    def test_returns_orchestrator_root_with_canonical_value(
        self, tmp_path: Path
    ) -> None:
        """When the orchestrator-root binding happens to name the
        canonical value, the resolver still returns from the DB
        (not the const fall-through). Distinction matters because
        the resolution-source matters for logging / debugging."""
        db = tmp_path / "launcher.db"
        _make_db(
            db,
            orchestrator_primary_name="VibeCodedOrchestrator_KnowledgeGraph",
        )
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == "VibeCodedOrchestrator_KnowledgeGraph"

    def test_falls_back_to_const_when_no_orchestrator_root_row(
        self, tmp_path: Path
    ) -> None:
        """No orchestrator-root project row → resolver returns the
        bundled const."""
        db = tmp_path / "launcher.db"
        _make_db(db, orchestrator_primary_name=None)
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_falls_back_to_const_when_db_file_missing(
        self, tmp_path: Path
    ) -> None:
        """launcher.db file does not exist → resolver returns the
        bundled const (soft-fail)."""
        db = tmp_path / "does-not-exist.db"
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_falls_back_to_const_when_binding_empty(
        self, tmp_path: Path
    ) -> None:
        """orchestrator-root row exists but primary binding has empty
        collection_name → resolver returns the bundled const."""
        db = tmp_path / "launcher.db"
        _make_db(db, orchestrator_primary_name="")
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_falls_back_to_const_when_binding_whitespace_only(
        self, tmp_path: Path
    ) -> None:
        """orchestrator-root row exists but primary binding is just
        whitespace → treated as empty → resolver returns the bundled
        const."""
        db = tmp_path / "launcher.db"
        _make_db(db, orchestrator_primary_name="   ")
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_falls_back_to_const_when_orchestrator_row_missing_binding(
        self, tmp_path: Path
    ) -> None:
        """orchestrator-root project row exists but has NO primary
        binding row at all → resolver returns the bundled const."""
        db = tmp_path / "launcher.db"
        # Hand-build DB with project row only (no binding row).
        conn = sqlite3.connect(str(db))
        try:
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
                """
            )
            cur.execute(
                "INSERT INTO projects (id, name, folder_path, slug) "
                "VALUES (?, ?, ?, ?)",
                (
                    "root-id-002",
                    "VibeCoded Orchestrator",
                    "/tmp/fake-orch-root-3",
                    "orchestrator-root",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_never_raises_on_corrupt_db(self, tmp_path: Path) -> None:
        """Soft-fail: a corrupt / malformed DB returns the const
        rather than propagating the sqlite3 error."""
        db = tmp_path / "launcher.db"
        db.write_bytes(b"this is not a sqlite database, even slightly")
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME


# ─── Integration via project_env_from_db / project_env_template_from_db ──


class TestProjectEnvFromDbIntegration:
    """End-to-end: project_env_from_db consults the resolver when
    shared_kg_default=None (the new W40-C default)."""

    def test_default_none_uses_orchestrator_root_binding(
        self, tmp_path: Path
    ) -> None:
        """The target project has no explicit `shared` binding row;
        the resolver picks up the orchestrator-root primary binding
        (rather than the stale const)."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "myproj"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-001",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name="ForkBrand_KnowledgeGraph",
            project_shared_binding=None,
        )
        # shared_kg_default=None is the new default.
        bundle = project_env_from_db("proj-001", db_path=db)
        env = bundle["canonical_env"]
        assert env["SHARED_KG_COLLECTION"] == "ForkBrand_KnowledgeGraph"

    def test_default_none_falls_back_to_const_when_no_root(
        self, tmp_path: Path
    ) -> None:
        """No orchestrator-root row → resolver returns the const →
        env's SHARED_KG_COLLECTION reflects that."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "myproj"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-002",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name=None,
            project_shared_binding=None,
        )
        bundle = project_env_from_db("proj-002", db_path=db)
        env = bundle["canonical_env"]
        assert env["SHARED_KG_COLLECTION"] == _LAST_RESORT_SHARED_KG_NAME

    def test_explicit_string_default_still_wins(
        self, tmp_path: Path
    ) -> None:
        """Caller passing an explicit `shared_kg_default="..."` keeps
        the legacy behaviour: the resolver is BYPASSED. This is
        important for white-label install scripts that need a deterministic
        fallback regardless of DB state."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "myproj"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-003",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name="WouldBeUsedIfDefaultWasNone_KG",
            project_shared_binding=None,
        )
        bundle = project_env_from_db(
            "proj-003",
            db_path=db,
            shared_kg_default="ExplicitOverride_KG",
        )
        env = bundle["canonical_env"]
        # Explicit string wins over both the DB-read and the const.
        assert env["SHARED_KG_COLLECTION"] == "ExplicitOverride_KG"

    def test_explicit_shared_binding_wins_over_default_resolution(
        self, tmp_path: Path
    ) -> None:
        """When the TARGET project has its own `shared` binding row,
        BOTH the const and the orchestrator-root resolver are
        irrelevant — the explicit row wins."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "myproj"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-004",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name="OrchRoot_KG",
            project_shared_binding="MyExplicitShared_KG",
        )
        bundle = project_env_from_db("proj-004", db_path=db)
        env = bundle["canonical_env"]
        assert env["SHARED_KG_COLLECTION"] == "MyExplicitShared_KG"


class TestProjectEnvTemplateIntegration:
    """End-to-end via the env_template projection."""

    def test_template_uses_orchestrator_root_binding_when_default_none(
        self, tmp_path: Path
    ) -> None:
        """The .env template projection passes through the same DB-driven
        fallback as the full env bundle."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "p"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-005",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name="ForkBrand_KnowledgeGraph",
            project_shared_binding=None,
        )
        keys = project_env_template_from_db("proj-005", db_path=db)
        assert keys["SHARED_KG_COLLECTION"] == "ForkBrand_KnowledgeGraph"

    def test_template_falls_back_to_const_when_no_root_binding(
        self, tmp_path: Path
    ) -> None:
        """Symmetric soft-fall-through in the template projection."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "p"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-006",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name=None,
            project_shared_binding=None,
        )
        keys = project_env_template_from_db("proj-006", db_path=db)
        assert keys["SHARED_KG_COLLECTION"] == _LAST_RESORT_SHARED_KG_NAME

    def test_template_explicit_override_wins(self, tmp_path: Path) -> None:
        """White-label / test override still works."""
        db = tmp_path / "launcher.db"
        folder = tmp_path / "p"
        folder.mkdir()
        _make_db_with_project(
            db,
            project_id="proj-007",
            project_name="Demo",
            project_folder=str(folder),
            project_slug="demo",
            orchestrator_primary_name="OrchRoot_KG",
            project_shared_binding=None,
        )
        keys = project_env_template_from_db(
            "proj-007",
            db_path=db,
            shared_kg_default="ExplicitTemplateOverride_KG",
        )
        assert keys["SHARED_KG_COLLECTION"] == "ExplicitTemplateOverride_KG"


class TestCrossSurfaceConsistency:
    """The W40-C const must equal the legacy hardcoded default so the
    bundled-default behaviour is byte-identical to pre-W40-C for fresh
    installs that lack an orchestrator-root project."""

    def test_const_matches_legacy_hardcoded_default(self) -> None:
        """The bundled const is what every prior surface used as the
        hardcoded default — the rename is purely an audit signal, not a
        value change."""
        assert _LAST_RESORT_SHARED_KG_NAME == "VibeCodedOrchestrator_KnowledgeGraph"
