# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 duplication-merge (PLAN-EXTENSION §3.4) — the shared launcher.db
test fixture IS the launcher's schema, and stays that way.

``tests/common/launcher_db_fixture.py`` replaced ~20 hand-rolled
``CREATE TABLE projects …`` guesses. Its whole value is that it owns NO DDL
and applies the REAL migration SQL, so the four pins below are what keep
the replacement honest:

1. **The fixture owns no DDL** (AST: no string constant containing
   ``CREATE TABLE`` except the ``_schema_migrations`` bookkeeping table the
   Rust runner also creates, and the deliberate ``something_else`` table of
   the foreign-schema shape).
2. **The Rust runner and the on-disk SQL agree** — every ``NNN_*.sql`` file
   is ``include_str!``'d by ``migrations.rs`` and vice versa. The fixture
   walks the directory; the launcher walks the list. A file that exists in
   only one place would make the fixture's schema diverge from the
   launcher's silently.
3. **The fixture applies all of them** — ``_schema_migrations`` row count
   equals the file count, and re-applying is a no-op.
4. **Every column the Rust binding readers ``SELECT`` exists** in the
   fixture DB (``projects``, ``project_kg_bindings``,
   ``project_codegraph_bindings`` — the tables the identity resolver
   reads). This is the "equals a fresh launcher's" check in the strongest
   form available without a cargo build: the columns production actually
   reads are present with the fixture's schema.
5. **No test in ``tests/`` hand-rolls ``CREATE TABLE projects`` any more**
   (the §3.4 straggler proof as a test; the migration-applying tests and the
   fixture module itself are the documented exceptions).

Red-proofed: a stray ``.sql`` file in the migrations directory → (2) FAIL;
a ``CREATE TABLE projects`` string literal in a test → (5) FAIL; a column
renamed in the SQL but not in the Rust reader → (4) FAIL.
"""
from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.common import launcher_db_fixture as fx  # noqa: E402

CORE_DB = REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
MIGRATIONS_RS = CORE_DB / "migrations.rs"
PROJECT_STATE_RS = CORE_DB / "project_state.rs"

#: Tests that APPLY migrations or test the migration machinery itself — the
#: better habit already; they may name tables in DDL strings on purpose.
MIGRATION_TEST_ALLOWLIST = {
    "tests/test_schema_migration_runner.py",
    "tests/test_v0274_migration_delivery.py",
    "tests/test_v52_ag_schema_versions.py",
    "tests/test_migration_022_diagrams.py",
    "tests/test_schema_regenerate.py",
    "tests/common/launcher_db_fixture.py",
}

_CREATE_PROJECTS = re.compile(r"create\s+table\s+(if\s+not\s+exists\s+)?projects\b", re.I)


# --------------------------------------------------------------------- (1)


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_fixture_owns_no_launcher_ddl():
    # SQL statements start with the verb; prose that MENTIONS ``CREATE TABLE``
    # mid-sentence (the module docstring) is documentation, not DDL.
    ddl = [
        s for s in _string_constants(REPO_ROOT / "tests" / "common" / "launcher_db_fixture.py")
        if re.match(r"\s*create\s+table", s, re.I)
    ]
    for stmt in ddl:
        assert "_schema_migrations" in stmt or "something_else" in stmt, (
            "the fixture grew DDL of its own — it must APPLY the real "
            f"migrations, never declare a launcher table: {stmt[:80]!r}"
        )


# --------------------------------------------------------------------- (2)


def test_rust_runner_and_sql_directory_agree():
    listed = set(re.findall(r'include_str!\("migrations/([^"]+)"\)', MIGRATIONS_RS.read_text(encoding="utf-8")))
    on_disk = {p.name for p in fx.migration_files()}
    assert listed == on_disk, (
        f"only in migrations.rs: {sorted(listed - on_disk)}; "
        f"only on disk: {sorted(on_disk - listed)}"
    )


# --------------------------------------------------------------------- (3)


def test_fixture_applies_every_migration_and_is_idempotent(tmp_path):
    db = fx.create_empty_launcher_db(tmp_path / "launcher.db")
    n_files = len(fx.migration_files())
    conn = sqlite3.connect(str(db))
    try:
        (n_rows,) = conn.execute("SELECT COUNT(*) FROM _schema_migrations").fetchone()
        versions = [r[0] for r in conn.execute("SELECT version FROM _schema_migrations ORDER BY version")]
    finally:
        conn.close()
    assert n_rows == n_files
    assert versions == sorted(versions)
    assert fx.apply_migrations(db) == 0, "second apply must be a no-op"


# --------------------------------------------------------------------- (4)


def _rust_selected_columns(table: str) -> set[str]:
    """Columns named in ``SELECT … FROM <table>`` statements in
    ``project_state.rs`` (the binding readers). ``SELECT 1`` / ``COUNT``
    shapes contribute nothing."""
    src = PROJECT_STATE_RS.read_text(encoding="utf-8")
    cols: set[str] = set()
    # One Rust string literal at a time, so a lazy `.*?` cannot bridge two
    # statements and collect columns of unrelated tables.
    for lit in re.finditer(r'"((?:[^"\\]|\\.)*)"', src, re.S):
        sql = lit.group(1)
        m = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s+" + re.escape(table) + r"\b", sql, re.S)
        if not m:
            continue
        for raw in m.group(1).split(","):
            name = raw.strip().split(".")[-1].strip()
            if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                cols.add(name)
    assert cols, f"no SELECT … FROM {table} found in {PROJECT_STATE_RS.name}"
    return cols


@pytest.mark.parametrize(
    "table", ["projects", "project_kg_bindings", "project_codegraph_bindings"],
)
def test_every_column_the_rust_reader_selects_exists(tmp_path, table):
    db = fx.create_empty_launcher_db(tmp_path / "launcher.db")
    conn = sqlite3.connect(str(db))
    try:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()
    want = _rust_selected_columns(table)
    assert want <= have, f"{table}: Rust reads {sorted(want - have)} which the fixture DB lacks"


def test_seeded_project_round_trips_through_the_real_reader(tmp_path, monkeypatch):
    """Not just shape: a project seeded through the fixture is what
    ``vco_lib.launcher_db_reader`` resolves (through the production discovery
    chain, activated by ``VCT_LAUNCHER_DB_PATH``)."""
    from vco_lib import launcher_db_reader

    proj = tmp_path / "proj"
    proj.mkdir()
    db = fx.make_launcher_db(
        tmp_path, projects=[{
            "project_id": "p1", "name": "Acme", "folder_path": proj,
            "kg_primary": "Acme_KnowledgeGraph", "kg_shared": "Shared_KG",
            "codegraph_prefix": "Acme",
        }],
    )
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    names, resolvable = launcher_db_reader.kg_binding_keep_set()
    assert resolvable
    assert {"Acme_KnowledgeGraph", "Shared_KG"} <= set(names)


# --------------------------------------------------------------------- (5)


def test_no_test_hand_rolls_the_projects_table():
    offenders: list[str] = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in MIGRATION_TEST_ALLOWLIST or path == Path(__file__).resolve():
            continue
        if any(part in {"__pycache__", "fixtures"} for part in path.parts):
            continue
        try:
            constants = _string_constants(path)
        except SyntaxError:
            continue
        if any(_CREATE_PROJECTS.search(s) for s in constants):
            offenders.append(rel)
    assert not offenders, (
        "hand-rolled `CREATE TABLE projects` fixtures — use "
        f"tests.common.launcher_db_fixture.make_launcher_db: {sorted(offenders)}"
    )
