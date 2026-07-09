# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""X-1 / v0.2.76: Rust-side single-writer lint for the binding tables.

The Python companion (``tests/test_kg_binding_heal_single_writer.py``) pins the
Python side: only ``vco_lib.kg_binding_heal`` may mutate the KG-binding tables.
This test pins the RUST side: the base upsert / heal SQL for
``project_kg_bindings`` / ``project_codegraph_bindings`` lives ONLY in the
launcher-core DB layer's designated writer files, and every other ``.rs`` file
must route through the canonical ``Db`` methods (ideally via
``db::bindings_writer``) rather than open-coding an ``INSERT`` / ``UPDATE``.

Why a source-scan lint (not a runtime guard): a runtime guard can't see a NEW
``.rs`` file that starts issuing binding SQL. A grep-style lint fails at CI time
the moment a second Rust writer appears, and points the author at the single-
writer home.

Allowlisted writer files (the DB-layer home where binding SQL legitimately
lives):

* ``db/bindings_writer.rs`` — the single-writer entry point (delegation +
  derive-name-then-write; no raw SQL of its own today, but allowlisted so a
  future hoist of the base SQL lands here without tripping the lint);
* ``db/project_state.rs``  — the canonical ``set_project_kg_binding`` /
  ``set_project_codegraph_binding`` upsert methods;
* ``db/access.rs``         — the case-rebind / cross-prefix-adoption heal SQL;
* ``db/migrations.rs``     — schema seed/backfill + test-fixture rows.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_RS_ROOT = REPO_ROOT / "launcher" / "src-tauri"

_BINDING_TABLES = ("project_kg_bindings", "project_codegraph_bindings")

# INSERT (incl. INSERT OR IGNORE/REPLACE) / UPDATE against a binding table.
# Whitespace-flexible so multi-line raw-string SQL still matches.
_MUTATION_RE_TEMPLATE = (
    r"(?is)\b(?:UPDATE\s+{tbl}\b"
    r"|INSERT(?:\s+OR\s+\w+)?\s+INTO\s+{tbl}\b)"
)

# Allowlist: paths (relative to _RS_ROOT) where binding SQL is expected.
_ALLOWED = {
    Path("vct-launcher-core/src/db/bindings_writer.rs"),
    Path("vct-launcher-core/src/db/project_state.rs"),
    Path("vct-launcher-core/src/db/access.rs"),
    Path("vct-launcher-core/src/db/migrations.rs"),
}

_SKIP_DIR_PARTS = {"target", ".git", "node_modules"}


def _patterns() -> list[re.Pattern]:
    return [
        re.compile(_MUTATION_RE_TEMPLATE.replace("{tbl}", re.escape(t)))
        for t in _BINDING_TABLES
    ]


def _iter_rs_files():
    for path in _RS_ROOT.rglob("*.rs"):
        if any(part in _SKIP_DIR_PARTS for part in path.parts):
            continue
        yield path


def test_no_binding_table_sql_outside_writer_files() -> None:
    """No ``.rs`` file outside the allowlisted DB-layer writer files may issue a
    direct INSERT/UPDATE against a binding table."""
    assert _RS_ROOT.is_dir(), f"launcher src-tauri not found at {_RS_ROOT}"
    patterns = _patterns()
    violations: list[str] = []

    for path in _iter_rs_files():
        rel = path.relative_to(_RS_ROOT)
        if rel in _ALLOWED:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in patterns:
            for m in pat.finditer(content):
                lineno = content.count("\n", 0, m.start()) + 1
                snippet = " ".join(m.group(0).split())
                violations.append(
                    f"{rel}:{lineno}: `{snippet[:80]}` — route binding writes "
                    f"through db::bindings_writer / the canonical Db methods."
                )

    assert not violations, (
        f"{len(violations)} Rust binding single-writer violation(s):\n"
        + "\n".join(violations)
        + "\n\nThe binding-table SQL lives in the DB-layer writer files "
        "(db/bindings_writer.rs, project_state.rs, access.rs, migrations.rs). "
        "New call sites must route through Db::set_project_kg_binding / "
        "set_project_codegraph_binding (via db::bindings_writer), not open-code "
        "their own INSERT/UPDATE."
    )


def test_allowlisted_writer_files_exist() -> None:
    """Guard against a stale allowlist (renamed/removed writer file)."""
    for rel in _ALLOWED:
        assert (_RS_ROOT / rel).is_file(), (
            f"allowlisted writer file missing: {rel} — update the allowlist "
            "if the DB-layer writer home moved."
        )


def test_bindings_writer_module_declared() -> None:
    """``db::bindings_writer`` must be declared in the db mod.rs."""
    mod_rs = _RS_ROOT / "vct-launcher-core" / "src" / "db" / "mod.rs"
    assert mod_rs.is_file(), f"missing {mod_rs}"
    assert "pub mod bindings_writer;" in mod_rs.read_text(encoding="utf-8"), (
        "db::bindings_writer must be declared in vct-launcher-core/src/db/mod.rs"
    )
