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

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_kg_binding,
    create_corrupt_launcher_db,
    create_empty_launcher_db,
    make_launcher_db,
)
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
    """Build a launcher.db (REAL schema — v0.2.92 §3.4).

    When ``orchestrator_primary_name`` is provided, seeds an
    ``orchestrator-root`` project row + a primary binding row pointing
    at the given name. ``None`` → no orchestrator-root row (forces the
    last-resort fallback).

    The binding is written with :func:`add_kg_binding` rather than
    ``add_project(kg_primary=...)`` on purpose: two tests below pass ``""``
    and ``"   "`` to pin "row EXISTS but its collection_name is blank", and
    the ``add_project`` shortcut skips falsy collection names — which would
    silently degrade those cases into "no binding row at all" (a DIFFERENT
    branch of the resolver that another test already covers).
    """
    if orchestrator_primary_name is None:
        create_empty_launcher_db(db_path)
        return
    make_launcher_db(
        db_path,
        projects=[{
            "project_id": "root-id-001",
            "name": "VibeCoded Orchestrator",
            "folder_path": "/tmp/fake-orch-root",
            "slug": "orchestrator-root",
            "host": "orchestrator_root",
        }],
    )
    add_kg_binding(db_path, "root-id-001", "primary", orchestrator_primary_name)


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
    orchestrator-root seed, on the REAL launcher schema (v0.2.92 §3.4).

    Used for the integration tests that round-trip through
    ``project_env_from_db`` / ``project_env_template_from_db``.
    """
    projects: list[dict[str, object]] = [{
        "project_id": project_id,
        "name": project_name,
        "folder_path": project_folder,
        "slug": project_slug,
    }]
    if orchestrator_primary_name is not None:
        projects.append({
            "project_id": "root-id-001",
            "name": "VibeCoded Orchestrator",
            # Distinct folder_path/slug: both are UNIQUE in the real schema.
            "folder_path": "/tmp/fake-orch-root-2",
            "slug": "orchestrator-root",
            "host": "orchestrator_root",
        })
    make_launcher_db(db_path, projects=projects)
    if project_shared_binding is not None:
        add_kg_binding(db_path, project_id, "shared", project_shared_binding)
    if orchestrator_primary_name is not None:
        add_kg_binding(
            db_path, "root-id-001", "primary", orchestrator_primary_name,
        )


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
        # Project row only — deliberately NO project_kg_bindings row.
        make_launcher_db(
            db,
            projects=[{
                "project_id": "root-id-002",
                "name": "VibeCoded Orchestrator",
                "folder_path": "/tmp/fake-orch-root-3",
                "slug": "orchestrator-root",
                "host": "orchestrator_root",
            }],
        )
        out = _resolve_shared_kg_default_from_launcher_db(db_path=db)
        assert out == _LAST_RESORT_SHARED_KG_NAME

    def test_never_raises_on_corrupt_db(self, tmp_path: Path) -> None:
        """Soft-fail: a corrupt / malformed DB returns the const
        rather than propagating the sqlite3 error."""
        db = create_corrupt_launcher_db(tmp_path / "launcher.db")
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
