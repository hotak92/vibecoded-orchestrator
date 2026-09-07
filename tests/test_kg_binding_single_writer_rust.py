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

TEST-CODE EXCLUSION (v0.2.92)
-----------------------------
The rule this lint enforces is about production ARCHITECTURE: "a shipped code
path must not open-code binding SQL". A ``#[cfg(test)]`` module constructing an
adversarial fixture is not a call site, and the gate was flagging two of them:

* ``vct-hub/src/config_api.rs`` (in ``mod tests``), and
* ``src/commands/project_state_populate/shared_kg_binding.rs`` (ditto).

Both write ``collection_name = ''`` through raw SQL *precisely because the
canonical row writer refuses to produce an empty name* — the assertion under
test is the resolver's empty-name filter, and there is no non-raw way to reach
that state. Production code in both files IS routed through
``db::bindings_writer``. Flagging them made a real architectural gate red for a
non-violation, which is how gates get disabled.

Weakening a gate has to earn its keep, so the exclusion is deliberately narrow
and fails CLOSED:

* it skips the annotated ITEM (brace-balanced, or semicolon-terminated), not
  "everything after the first marker" — a file may gate a mid-file test helper
  and then continue with production code;
* it uses a Rust-aware lexer, so a brace inside a string/comment cannot end the
  skip early and expose test code as production (noisy) or, worse, end it late;
* it takes the STRICT reading of the predicate: only ``cfg(test)`` and
  ``cfg(all(test, …))``. ``cfg(any(test, debug_assertions))`` still COMPILES
  into a shipped binary, so for this question it is production;
* anything it cannot delimit confidently is skipped NOT AT ALL.

The machinery lives in ``tests/common/rust_source.py`` (shared home; see its
docstring for the two older lints still carrying private copies).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.rust_source import (  # noqa: E402
    cfg_test_line_numbers,
    strip_rust_comments,
)

_RS_ROOT = REPO_ROOT / "launcher" / "src-tauri"

_BINDING_TABLES = ("project_kg_bindings", "project_codegraph_bindings")

# INSERT (incl. INSERT OR IGNORE/REPLACE) / UPDATE against a binding table.
# Whitespace-flexible so multi-line raw-string SQL still matches.
_MUTATION_RE_TEMPLATE = (
    r"(?is)\b(?:UPDATE\s+{tbl}\b"
    r"|INSERT(?:\s+OR\s+\w+)?\s+INTO\s+{tbl}\b)"
)

# Allowlist: paths (relative to _RS_ROOT) where binding SQL is expected.
_ALLOWED = frozenset(
    {
        Path("vct-launcher-core/src/db/bindings_writer.rs"),
        Path("vct-launcher-core/src/db/project_state.rs"),
        Path("vct-launcher-core/src/db/access.rs"),
        Path("vct-launcher-core/src/db/migrations.rs"),
    }
)

_SKIP_DIR_PARTS = {"target", ".git", "node_modules"}


def _patterns() -> list[re.Pattern]:
    return [
        re.compile(_MUTATION_RE_TEMPLATE.replace("{tbl}", re.escape(t)))
        for t in _BINDING_TABLES
    ]


def _iter_rs_files(root: Path):
    for path in root.rglob("*.rs"):
        if any(part in _SKIP_DIR_PARTS for part in path.parts):
            continue
        yield path


def scan_binding_sql(content: str, rel: str) -> list[str]:
    """Binding-SQL violations in ONE Rust source, `#[cfg(test)]` items excluded.

    Split out from the tree walk so the exclusion can be exercised against
    synthetic sources (below) instead of only against whatever the real tree
    happens to contain today.

    Matches against comment-stripped source (``strip_rust_comments`` — keeps
    string-literal content verbatim, since the SQL text we're hunting for
    lives INSIDE `.execute("...")` string arguments) so a `///`/`//` doc
    comment that merely *mentions* a binding table in prose is not flagged as
    a production call site. `#[cfg(test)]` exclusion still runs against the
    ORIGINAL ``content`` (``cfg_test_line_numbers`` scrubs internally as
    needed) — comment-stripping preserves line numbers 1:1, so the two line
    number spaces stay comparable.
    """
    test_lines = cfg_test_line_numbers(content)
    code = "\n".join(strip_rust_comments(content))
    violations: list[str] = []
    for pat in _patterns():
        for m in pat.finditer(code):
            lineno = code.count("\n", 0, m.start()) + 1
            if lineno in test_lines:
                continue  # adversarial fixture, not a production call site
            snippet = " ".join(m.group(0).split())
            violations.append(
                f"{rel}:{lineno}: `{snippet[:80]}` — route binding writes "
                f"through db::bindings_writer / the canonical Db methods."
            )
    return violations


def scan_tree(root: Path, allowed=frozenset()) -> list[str]:
    """Every violation under ``root``, skipping ``allowed`` relative paths."""
    violations: list[str] = []
    for path in _iter_rs_files(root):
        rel = path.relative_to(root)
        if rel in allowed:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        violations.extend(scan_binding_sql(content, str(rel)))
    return violations


def test_no_binding_table_sql_outside_writer_files() -> None:
    """No ``.rs`` file outside the allowlisted DB-layer writer files may issue a
    direct INSERT/UPDATE against a binding table."""
    assert _RS_ROOT.is_dir(), f"launcher src-tauri not found at {_RS_ROOT}"
    violations = scan_tree(_RS_ROOT, _ALLOWED)

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


# ═══════════════════════════════════════════════════════════════════════════
# the exclusion cannot be fooled into skipping PRODUCTION code
# ═══════════════════════════════════════════════════════════════════════════
#
# A gate made weaker has to prove it is still a gate. Every case below is a
# shape that would let real binding SQL through if the skip were implemented as
# "ignore everything after the first `#[cfg(test)]`".

_PROD_SQL = (
    '    conn.execute("UPDATE project_kg_bindings SET collection_name = \'x\'", [])'
    ".unwrap();\n"
)


def test_a_plain_production_violation_is_flagged() -> None:
    """Baseline: without any cfg-test involvement the gate still bites."""
    src = "fn repoint(conn: &Connection) {\n" + _PROD_SQL + "}\n"
    assert len(scan_binding_sql(src, "x.rs")) == 1


def test_production_code_after_a_test_module_is_still_flagged() -> None:
    """The shape the naive 'cut at the first marker' rule gets wrong."""
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn seeds_an_empty_name() {\n"
        '        db.execute("UPDATE project_kg_bindings SET collection_name = \'\'", [])'
        ".unwrap();\n"
        "    }\n"
        "}\n"
        "\n"
        "fn repoint_later(conn: &Connection) {\n"
        + _PROD_SQL
        + "}\n"
    )
    found = scan_binding_sql(src, "x.rs")
    assert len(found) == 1, found
    assert ":10:" in found[0], found  # the production line, not the fixture


def test_cfg_test_on_a_semicolon_item_does_not_blind_the_rest() -> None:
    """``#[cfg(test)] mod tests;`` / ``use`` gate ONE item, not the file."""
    for gated in ("mod tests;", "use std::fs;"):
        src = (
            "#[cfg(test)]\n"
            f"{gated}\n"
            "\n"
            "fn repoint(conn: &Connection) {\n"
            + _PROD_SQL
            + "}\n"
        )
        assert len(scan_binding_sql(src, "x.rs")) == 1, gated


def test_a_commented_out_cfg_test_is_not_an_attribute() -> None:
    """The marker must be CODE — a doc comment mentioning it gates nothing."""
    for lead in ("// #[cfg(test)]", "/// #[cfg(test)]", '// "#[cfg(test)]"'):
        src = (
            f"{lead}\n"
            "fn repoint(conn: &Connection) {\n"
            + _PROD_SQL
            + "}\n"
        )
        assert len(scan_binding_sql(src, "x.rs")) == 1, lead


def test_a_doc_comment_mentioning_binding_sql_is_not_flagged() -> None:
    """A `///`/`//` comment that merely MENTIONS a binding table in prose is
    not a call site — only actual `.execute("...")` string content counts.

    (v0.2.92 WP-G register-34: prior to routing through
    ``strip_rust_comments`` this was a false positive — the regex matched raw
    ``content`` and could not tell prose from a live SQL string argument.)
    """
    for comment in (
        '// See also: conn.execute("UPDATE project_kg_bindings SET x = 1")',
        '/// Do NOT `INSERT INTO project_codegraph_bindings` directly — use'
        " db::bindings_writer.",
        "/* historical note: this file used to UPDATE project_kg_bindings"
        " inline */",
    ):
        src = f"{comment}\nfn repoint(conn: &Connection) {{\n" + _PROD_SQL + "}\n"
        found = scan_binding_sql(src, "x.rs")
        # Exactly the real call site in _PROD_SQL should still be flagged —
        # the comment itself must contribute zero violations.
        assert len(found) == 1, (comment, found)


def test_a_brace_inside_a_string_does_not_end_the_skip_early() -> None:
    """A stateless stripper would close the test module at the string's ``}``
    and then read the fixture SQL as production."""
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    const TPL: &str = "a } brace } in a string";\n'
        "    #[test]\n"
        "    fn seeds() {\n"
        '        db.execute("UPDATE project_kg_bindings SET collection_name = \'\'", [])'
        ".unwrap();\n"
        "    }\n"
        "}\n"
    )
    assert scan_binding_sql(src, "x.rs") == []


def test_a_raw_string_and_a_nested_block_comment_do_not_end_the_skip() -> None:
    """Rust-specific lexer shapes: ``r#"…"#`` and NESTED ``/* /* */ */``."""
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    const Q: &str = r#"SELECT "}" FROM t"#;\n'
        "    /* outer /* inner } */ still comment } */\n"
        "    #[test]\n"
        "    fn seeds() {\n"
        '        db.execute("INSERT INTO project_codegraph_bindings VALUES (1)", [])'
        ".unwrap();\n"
        "    }\n"
        "}\n"
    )
    assert scan_binding_sql(src, "x.rs") == []


def test_cfg_any_test_is_treated_as_production() -> None:
    """``any(test, debug_assertions)`` COMPILES into a shipped binary, so for
    an architectural single-writer question it is production code.

    (The bare-prints lint answers a different question and treats it as test;
    that divergence is deliberate — see ``tests/common/rust_source.py``.)
    """
    src = (
        '#[cfg(any(test, debug_assertions))]\n'
        "fn helper(conn: &Connection) {\n"
        + _PROD_SQL
        + "}\n"
    )
    assert len(scan_binding_sql(src, "x.rs")) == 1


def test_an_undelimitable_gated_item_skips_nothing() -> None:
    """Fail CLOSED: an unbalanced ``#[cfg(test)]`` block excludes no lines, so
    a lexer mistake can only make the gate noisy, never blind."""
    src = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn seeds() {\n"
        + _PROD_SQL
    )  # deliberately never closed
    assert len(scan_binding_sql(src, "x.rs")) == 1


def test_the_two_real_fixtures_are_excluded_by_being_test_gated() -> None:
    """Pin the real-tree reason those two files stopped failing.

    Without this the suite could go green for the WRONG reason (a broken regex
    matching nothing) and nobody would notice.
    """
    cases = [
        (_RS_ROOT / "vct-hub" / "src" / "config_api.rs", "UPDATE project_kg_bindings"),
        (
            _RS_ROOT / "src" / "commands" / "project_state_populate"
            / "shared_kg_binding.rs",
            "UPDATE project_kg_bindings",
        ),
    ]
    for path, needle in cases:
        assert path.is_file(), f"missing {path}"
        content = path.read_text(encoding="utf-8")
        hits = [
            content.count("\n", 0, m.start()) + 1
            for m in re.finditer(re.escape(needle), content)
        ]
        assert hits, f"{path.name}: the raw-SQL fixture is gone — if it was "
        "migrated to the row writer, delete this pin"
        gated = cfg_test_line_numbers(content)
        for lineno in hits:
            assert lineno in gated, (
                f"{path.name}:{lineno} raw binding SQL is NOT inside a "
                f"#[cfg(test)] item — that is a real violation, not a fixture"
            )
