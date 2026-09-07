# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE on-disk ``launcher.db`` fixture for the Python test suite.

v0.2.92 duplication-merge (PLAN-EXTENSION §3.4). Before this change ``tests/``
carried ~20 hand-rolled ``CREATE TABLE projects …`` fixtures (plus ~14
near-variants that declared ``app_state`` / ``module_settings`` /
``project_kg_bindings`` by hand), each a partial, drifting guess at the
launcher's schema — column sets that the real DB had already outgrown, ``NOT
NULL`` constraints the real DB enforces and the guess did not, and a 20th copy
of the migration applier inside the step-22 integration fixture. A test that
passes against a guessed schema proves nothing about the DB the launcher
actually writes.

**This module owns NO DDL.** It APPLIES the real migration SQL from
``launcher/src-tauri/vct-launcher-core/src/db/migrations/NNN_*.sql`` — the same
files ``vct_launcher_core::db::migrations::apply`` ``include_str!``s — so the
fixture's schema IS the launcher's schema, by construction, and cannot drift
from it. ``tests/test_launcher_db_fixture_schema.py`` pins that: every SQL file
on disk is in the Rust runner's list and vice versa, the fixture applies all
of them, and every column the Rust binding readers ``SELECT`` exists here.

Reversed growth direction, on purpose: a NEW launcher.db test never writes
``CREATE TABLE`` — it calls :func:`make_launcher_db` (or the row helpers) and
gets the real schema. The step-22 integration fixture imports
:func:`apply_migrations` from here rather than carrying its own copy.

Production code opens the file read-only through the discovery chain, so
tests activate a fixture DB by setting ``VCT_LAUNCHER_DB_PATH`` (or by
monkeypatching the resolver the module under test exposes).

Real-schema facts a migrating test must respect (they were invisible under
the hand-rolled guesses):

* ``projects.folder_path`` is UNIQUE and ``projects.slug`` has a UNIQUE
  index — two seeded projects need distinct folders and slugs.
* ``projects.host`` is ``base`` | ``mao`` | ``orchestrator_root`` (CHECK).
* ``created_at`` / ``updated_at`` / ``granted_at`` / ``registered_at`` are
  ``NOT NULL`` — :func:`insert_rows` fills them with "now" when omitted.
* ``app_state.updated_at`` is ``NOT NULL`` (the hand-rolled two-column
  ``app_state`` never had it).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "migrations"
)

#: Columns :func:`insert_rows` fills with the current unix-ms timestamp when
#: the caller omits them and the schema says NOT NULL. Names, not tables: the
#: real schema uses these consistently.
_TIMESTAMP_COLUMNS = frozenset({
    "created_at", "updated_at", "granted_at", "registered_at", "started_at",
    "applied_at", "next_attempt_at",
})


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# The applier — moved here from tests/integration/step22_multi_project/fixture.py
# ---------------------------------------------------------------------------


def migration_files() -> list[Path]:
    """The shipped ``NNN_*.sql`` files in version order (the Rust runner's
    ``include_str!`` list is pinned against this by the schema test)."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations found in {MIGRATIONS_DIR}")
    return files


def apply_migrations(db_path: Path, *, up_to: Optional[int] = None) -> int:
    """Apply every shipped launcher.db migration in version order.

    Mirrors what ``vct_launcher_core::db::migrations::apply`` does at
    runtime, in pure Python — no cargo build in the fixture path. Returns
    the number of migrations applied by THIS call (idempotent: re-running
    on an up-to-date DB applies 0).

    ``up_to`` STOPS the chain after that version (inclusive) — the one
    sanctioned way to model a genuinely half-migrated DB (e.g. ``up_to=33``
    for a pre-034 ``module_settings.project_id NOT NULL`` shape). It raises
    if no migration above ``up_to`` exists, because a helper that claims to
    build a pre-N DB while N is the latest would silently build the current
    schema instead.

    Migration 013 toggles ``PRAGMA foreign_keys`` outside its transaction.
    ``sqlite3.executescript`` issues each ``;``-separated statement
    individually; the connection is opened with ``isolation_level=None`` so
    the pragma is not swallowed by an implicit transaction (which would
    render the off→on toggle a no-op).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    applied = 0
    try:
        # The migrations tracking table the Rust runner uses.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  INTEGER NOT NULL
            )
            """
        )
        already: set[int] = {
            int(row[0]) for row in conn.execute("SELECT version FROM _schema_migrations")
        }
        files = migration_files()
        if up_to is not None:
            versions = [int(f.name.split("_", 1)[0]) for f in files]
            if not any(v > up_to for v in versions):
                raise ValueError(
                    f"up_to={up_to}: no migration above it exists (latest is "
                    f"{max(versions)}) — this would not be a half-migrated DB"
                )
        for sql_path in files:
            try:
                version = int(sql_path.name.split("_", 1)[0])
            except ValueError:
                continue
            if up_to is not None and version > up_to:
                break
            if version in already:
                continue
            conn.executescript(sql_path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO _schema_migrations (version, description, applied_at) "
                "VALUES (?, ?, ?)",
                (version, sql_path.stem, now_ms()),
            )
            applied += 1
    finally:
        conn.close()
    return applied


# ---------------------------------------------------------------------------
# Creating a DB
# ---------------------------------------------------------------------------


def _resolve_db_path(where: Union[str, Path]) -> Path:
    """``where`` may be a directory (→ ``<dir>/launcher.db``) or a file path."""
    p = Path(where)
    if p.is_dir() or (not p.suffix and not p.exists()):
        p.mkdir(parents=True, exist_ok=True)
        return p / "launcher.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def create_empty_launcher_db(db_path: Path, *, up_to: Optional[int] = None) -> Path:
    """Create a schema-only ``launcher.db`` (real schema, zero projects).

    Distinct from "no DB at all": a readable-but-empty DB is RESOLVABLE, so
    detectors may proceed; an absent DB is UNRESOLVABLE and they must not.
    ``up_to`` stops the migration chain (see :func:`apply_migrations`).
    """
    db_path = _resolve_db_path(db_path)
    apply_migrations(db_path, up_to=up_to)
    return db_path


def make_launcher_db(
    where: Union[str, Path],
    *,
    projects: Iterable[Mapping[str, Any]] = (),
    app_state: Optional[Mapping[str, Any]] = None,
    module_settings: Iterable[Sequence[Any]] = (),
    kg_access: Iterable[Sequence[Any]] = (),
    codegraph_access: Iterable[Sequence[Any]] = (),
    diagram_access: Iterable[Sequence[Any]] = (),
) -> Path:
    """One call, real schema, seeded.

    ``where`` is a directory (``tmp_path`` → ``tmp_path/launcher.db``) or an
    explicit ``.db`` path. ``projects`` takes mappings in the keyword shape of
    :func:`add_project` (``project_id`` defaults to ``p<N>`` by position).
    ``module_settings`` rows are ``(project_id, module_id, key, value)``;
    ``kg_access`` rows ``(project_id, collection_name, access_level)``;
    ``codegraph_access`` / ``diagram_access`` rows
    ``(grantor_project_id, grantee_project_id[, access_level])``.
    Anything else goes through :func:`insert_rows` afterwards.
    """
    db_path = create_empty_launcher_db(Path(where))
    for idx, spec in enumerate(projects, start=1):
        kwargs = dict(spec)
        kwargs.setdefault("project_id", f"p{idx}")
        add_project(db_path, **kwargs)
    for key, value in (app_state or {}).items():
        set_app_state(db_path, key, value)
    for pid, mid, key, value in module_settings:
        add_module_setting(db_path, pid, mid, key, value)
    for row in kg_access:
        grant_kg_access(db_path, *row)
    for row in codegraph_access:
        grant_codegraph_access(db_path, *row)
    for row in diagram_access:
        grant_diagram_access(db_path, *row)
    return db_path


def seed_launcher_db(
    db_path: Path, projects: Iterable[Mapping[str, object]] = (),
) -> Path:
    """Create the DB and register every mapping in ``projects``.

    Each mapping takes the keyword names of :func:`add_project`; ``project_id``
    defaults to ``p<N>`` by position. (Kept for the pre-merge callers;
    :func:`make_launcher_db` is the same thing with more seed channels.)
    """
    return make_launcher_db(db_path, projects=projects)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Row helpers — every one INSERTs into the REAL table shape
# ---------------------------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    """Plain read/write connection for a test that wants to poke the DB."""
    return sqlite3.connect(str(db_path))


def insert_rows(
    db_path: Path, table: str, rows: Iterable[Mapping[str, Any]],
) -> None:
    """Generic INSERT of dict rows into any real table.

    Fills the NOT-NULL timestamp columns in :data:`_TIMESTAMP_COLUMNS` with
    ``now_ms()`` when a row omits them, so a test seeding e.g.
    ``project_diagrams`` does not have to know which of the eleven columns
    are mandatory. Every other NOT-NULL column without a default must be
    supplied — ``sqlite3.IntegrityError`` names the one that is not.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            raise ValueError(f"unknown launcher.db table {table!r}")
        required_ts = {
            name for _, name, _ty, notnull, dflt, _pk in info
            if notnull and dflt is None and name in _TIMESTAMP_COLUMNS
        }
        for row in rows:
            data = dict(row)
            for col in required_ts:
                data.setdefault(col, now_ms())
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()),
            )
        conn.commit()
    finally:
        conn.close()


def add_project(
    db_path: Path,
    *,
    project_id: str,
    name: str,
    folder_path: Union[str, Path],
    slug: Optional[str] = None,
    host: str = "base",
    kg_primary: Optional[str] = None,
    kg_shared: Optional[str] = None,
    codegraph_prefix: Optional[str] = None,
    kg_extra_roles: Optional[Mapping[str, str]] = None,
    kg_embedding_model: Optional[str] = None,
    kg_embedding_dim: Optional[int] = None,
    codegraph_embedding_model: Optional[str] = None,
    codegraph_embedding_dim: Optional[int] = None,
    codegraph_enabled: bool = True,
    kg_dir_path: Optional[str] = None,
    rl_port: Optional[int] = None,
    created_at: Optional[int] = None,
    updated_at: Optional[int] = None,
) -> None:
    """Register one project (+ optional KG / code-graph bindings).

    ``kg_primary`` / ``codegraph_prefix`` are the values the LAUNCHER wrote —
    i.e. the collections the project actually reads. Leaving them ``None``
    reproduces a project registered but not yet bootstrapped/analyzed.

    ``kg_extra_roles`` seeds ``project_kg_bindings`` rows under roles OTHER than
    primary/shared — in practice ``{"archive": "Custom_Store"}``. The REAL
    schema (migration 002) constrains ``role`` to ``primary`` | ``shared`` |
    ``archive`` with a CHECK; the pre-merge hand-rolled fixture had no CHECK
    and its docstring called the column free-text, which let a test assert on
    a ``future_role`` row production cannot write. Every legal extra row
    still names a LIVE bound class, which is why the keep-set must be
    role-unfiltered (v0.2.92 F-4).

    ``host`` is ``"base"`` unless the test is modelling the orchestrator root
    (``"orchestrator_root"``) or a MAO project (``"mao"``).
    """
    ts = now_ms()
    insert_rows(db_path, "projects", [{
        "id": project_id,
        "name": name,
        "folder_path": str(folder_path),
        "host": host,
        "slug": slug if slug is not None else name.lower(),
        "rl_port": rl_port,
        "created_at": created_at if created_at is not None else ts,
        "updated_at": updated_at if updated_at is not None else ts,
    }])
    roles: list[tuple[str, Optional[str]]] = [
        ("primary", kg_primary), ("shared", kg_shared),
    ]
    roles.extend((r, c) for r, c in (kg_extra_roles or {}).items())
    binding_rows = [
        {
            "project_id": project_id,
            "role": role,
            "collection_name": coll,
            "embedding_model": kg_embedding_model,
            "embedding_dim": kg_embedding_dim,
            "kg_dir_path": kg_dir_path,
        }
        for role, coll in roles if coll
    ]
    if binding_rows:
        insert_rows(db_path, "project_kg_bindings", binding_rows)
    if codegraph_prefix:
        insert_rows(db_path, "project_codegraph_bindings", [{
            "project_id": project_id,
            "collection_prefix": codegraph_prefix,
            "embedding_model": codegraph_embedding_model,
            "embedding_dim": codegraph_embedding_dim,
            "enabled": 1 if codegraph_enabled else 0,
        }])


def add_kg_binding(
    db_path: Path, project_id: str, role: str, collection_name: str, **cols: Any,
) -> None:
    """One ``project_kg_bindings`` row (for tests that bind after registering)."""
    insert_rows(db_path, "project_kg_bindings", [{
        "project_id": project_id, "role": role,
        "collection_name": collection_name, **cols,
    }])


def add_codegraph_binding(
    db_path: Path, project_id: str, collection_prefix: str, **cols: Any,
) -> None:
    insert_rows(db_path, "project_codegraph_bindings", [{
        "project_id": project_id, "collection_prefix": collection_prefix, **cols,
    }])


def set_app_state(db_path: Path, key: str, value: Any) -> None:
    """Upsert one ``app_state`` row (``value`` stored as-is; the launcher
    writes JSON-encoded strings, so pass what production would)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def add_module_setting(
    db_path: Path, project_id: Optional[str], module_id: str, key: str, value: Any,
) -> None:
    """One ``module_settings`` row. ``project_id=None`` is the host-wide row
    shape migration 034 made legal (``module_settings_nullable_project``)."""
    insert_rows(db_path, "module_settings", [{
        "project_id": project_id, "module_id": module_id,
        "setting_key": key, "setting_value": value,
    }])


def grant_kg_access(
    db_path: Path, project_id: str, collection_name: str, access_level: str = "read",
) -> None:
    insert_rows(db_path, "kg_collection_access", [{
        "project_id": project_id, "collection_name": collection_name,
        "access_level": access_level,
    }])


def grant_codegraph_access(
    db_path: Path, grantor: str, grantee: str, access_level: str = "read",
) -> None:
    insert_rows(db_path, "codegraph_access", [{
        "grantor_project_id": grantor, "grantee_project_id": grantee,
        "access_level": access_level,
    }])


def grant_diagram_access(
    db_path: Path, grantor: str, grantee: str, access_level: str = "read",
) -> None:
    insert_rows(db_path, "diagram_access", [{
        "grantor_project_id": grantor, "grantee_project_id": grantee,
        "access_level": access_level,
    }])


# ───────────────────────────────────────────────────────────────────────────
# UNREADABLE-DB shapes (v0.2.92 F-1)
#
# SQLite opens LAZILY: ``sqlite3.connect("file:…?mode=ro", uri=True)`` succeeds
# on a file that is not a database at all, and the FIRST QUERY is what raises.
# Both shapes below therefore reach production code as "connection opened
# fine", and both were empirically shown to make the legacy-drop guards ALLOW
# the drop of a live, named collection before F-1. Neither is hypothetical: a
# crash during a launcher write, a half-restored backup, or a stray file at
# ``VCT_LAUNCHER_DB_PATH`` produces them.
#
# These are the ONLY places this module creates a table by hand, and neither
# is a launcher table: they model a DB that is NOT a launcher.db.
# ───────────────────────────────────────────────────────────────────────────


def create_corrupt_launcher_db(db_path: Path) -> Path:
    """Write a NON-SQLite byte blob at ``db_path`` (opens ok, queries raise)."""
    db_path.write_bytes(
        b"\x00\x01this is not a sqlite database\xff\xfe" + b"\x7f" * 64
    )
    return db_path


def create_foreign_schema_launcher_db(db_path: Path) -> Path:
    """A VALID SQLite database with no VCO tables (``projects`` absent)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE something_else (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    return db_path
