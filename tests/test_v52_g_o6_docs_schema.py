# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-G + V52-O.6 — docs + schema-migration regression tests.

V52-G: `launcher/supabase/migrations/20260516_telemetry_events.sql` was
rewritten 2026-06-09 to be idempotent against the production database's
drift (deployed table predates this file and was missing
`server_received_at`). Every `CREATE INDEX` must use `IF NOT EXISTS`, and
the `server_received_at` column must be added via `ADD COLUMN IF NOT EXISTS`
so the migration is safe to re-run on the drifted prod DB AND on a fresh
checkout.

V52-O.6: `templates/ORCHESTRATOR-CLAUDE.md.template` was updated 2026-06-09
to name the asymmetry between KG (shared collection auto-merged on every
read) and code graph (no shared default; cross-project only via explicit
`codegraph_access` grants). The asymmetry section MUST exist in the
Storage Systems area of the template so future-installed projects inherit
the docs.

See v0.2.52 backlog § V52-G + § V52-O.6.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_MIGRATION = _REPO / "launcher" / "supabase" / "migrations" / "20260516_telemetry_events.sql"
_TEMPLATE = _REPO / "templates" / "ORCHESTRATOR-CLAUDE.md.template"


# ---------------------------------------------------------------------------
# Setup — both files must exist
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    """V52-G: the migration we're regression-testing must be on disk."""
    assert _MIGRATION.is_file(), f"missing {_MIGRATION}"


def test_template_file_exists() -> None:
    """V52-O.6: the CLAUDE.md template we updated must be on disk."""
    assert _TEMPLATE.is_file(), f"missing {_TEMPLATE}"


# ---------------------------------------------------------------------------
# V52-G — migration drift-tolerance assertions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migration_sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_all_create_index_use_if_not_exists(migration_sql: str) -> None:
    """Every CREATE INDEX in the migration must use IF NOT EXISTS.

    A bare `CREATE INDEX` against an already-existing index name errors out
    on re-run, which would break the drift-tolerance contract. The whole
    point of the V52-G rewrite was to make this migration safe to re-run.
    """
    # Find every CREATE INDEX statement (case-insensitive, possibly with
    # `CREATE UNIQUE INDEX` variant — we don't have any of those today
    # but the assertion should still hold if we ever add one).
    pattern = re.compile(
        r"create\s+(?:unique\s+)?index\s+(?!if\s+not\s+exists)",
        flags=re.IGNORECASE,
    )
    offenders = pattern.findall(migration_sql)
    assert not offenders, (
        f"found {len(offenders)} CREATE INDEX statement(s) missing "
        f"`IF NOT EXISTS` in {_MIGRATION.name} — would fail on re-run "
        f"against the drifted production DB. Offenders: {offenders}"
    )


def test_migration_server_received_at_added_via_alter(migration_sql: str) -> None:
    """The `server_received_at` column must be addable via ADD COLUMN IF NOT EXISTS.

    The original migration only had it in `CREATE TABLE IF NOT EXISTS`,
    which is a no-op when the table already exists — leaving the column
    missing on the drifted prod DB. V52-G's fix is to add an
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS server_received_at ...`
    that reconciles the drift.
    """
    # Match `alter table ... add column if not exists server_received_at`
    # across whitespace + line breaks.
    pattern = re.compile(
        r"alter\s+table\s+[\w\.]+\s+add\s+column\s+if\s+not\s+exists\s+"
        r"server_received_at",
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert pattern.search(migration_sql), (
        "V52-G regression: the migration must contain "
        "`ALTER TABLE ... ADD COLUMN IF NOT EXISTS server_received_at ...` "
        "so the column is added to the drifted prod DB before the "
        "indexes referencing it are created."
    )


def test_migration_index_referencing_server_received_at_uses_if_not_exists(
    migration_sql: str,
) -> None:
    """The two indexes that reference `server_received_at` must be idempotent.

    These are the two indexes that the original migration tried to
    create against a non-existent column on the prod DB — the proximate
    cause of V52-G. Verify both still exist (the index names are part
    of the schema contract — query planners + observability dashboards
    rely on the names) AND both use IF NOT EXISTS.
    """
    expected_indexes = [
        "telemetry_events_event_type_received_idx",
        "telemetry_events_machine_hash_idx",
    ]
    for idx_name in expected_indexes:
        pattern = re.compile(
            r"create\s+index\s+if\s+not\s+exists\s+" + re.escape(idx_name),
            flags=re.IGNORECASE,
        )
        assert pattern.search(migration_sql), (
            f"V52-G regression: index `{idx_name}` must be created with "
            f"`CREATE INDEX IF NOT EXISTS` (idempotent). Either it's "
            f"missing IF NOT EXISTS or the index name was renamed — "
            f"both would break downstream dashboards."
        )


def test_migration_documents_v52_g_rewrite_rationale(migration_sql: str) -> None:
    """The migration's preamble must document why it was rewritten.

    Future agents looking at the file should see (a) that this is the
    V52-G rewrite, (b) that the deployed table predates the original
    migration, (c) that the rewrite makes the script idempotent against
    that drift, (d) the assumption that the columns the edge function
    inserts into already exist on the deployed table. Without this
    context someone might "clean up" the redundant-looking
    ADD COLUMN IF NOT EXISTS block and re-break prod.
    """
    text = migration_sql.lower()
    # Loose matches — exact wording can vary; the markers must be there.
    assert "v52-g" in text, "migration preamble must reference V52-G"
    assert "drift" in text, (
        "migration preamble must mention drift (the underlying problem)"
    )
    assert "idempotent" in text, (
        "migration preamble must document its idempotence guarantee — "
        "this is the V52-G contract"
    )


def test_migration_does_not_drop_existing_data(migration_sql: str) -> None:
    """V52-G guarantee: the rewrite must NOT contain destructive statements.

    The migration runs against a DB with 277+ production events as of
    2026-06-05; any DROP TABLE / TRUNCATE / DELETE would erase real
    user-opt-in telemetry data. Reject if any such statement slipped in.
    """
    forbidden = [
        re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
        re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
        # Bare `truncate` (no `table` keyword) — pg accepts both forms.
        re.compile(r"\btruncate\s+public\.telemetry_events\b", re.IGNORECASE),
        re.compile(
            r"\bdelete\s+from\s+public\.telemetry_events\b", re.IGNORECASE
        ),
    ]
    for pat in forbidden:
        matches = pat.findall(migration_sql)
        assert not matches, (
            f"V52-G regression: migration contains a destructive "
            f"statement matching `{pat.pattern}` — would erase "
            f"production telemetry data. Matches: {matches}"
        )


# ---------------------------------------------------------------------------
# V52-O.6 — CLAUDE.md asymmetry docs assertions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_md() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_template_has_asymmetry_section_header(template_md: str) -> None:
    """V52-O.6: the asymmetry section must exist and be named.

    The whole point of the docs update is that future agents should
    NOT have to derive the KG-vs-codegraph difference from the
    individual subsystem entries — it should be named, with a header
    they can find via a literal grep.
    """
    # Accept any of these header shapes — being lenient on punctuation
    # so a future maintainer can rephrase without breaking the test.
    patterns = [
        r"KG\s+vs\s+Code\s+Graph",
        r"Code\s+Graph\s+vs\s+KG",
        r"asymmetry\s+between\s+KG\s+and\s+code\s+graph",
    ]
    matched = any(
        re.search(p, template_md, flags=re.IGNORECASE) for p in patterns
    )
    assert matched, (
        "V52-O.6 regression: the ORCHESTRATOR-CLAUDE.md.template must "
        "contain a named section explaining the KG-vs-codegraph "
        "asymmetry (try header `KG vs Code Graph — the cross-project "
        "access asymmetry`)."
    )


def test_template_asymmetry_section_in_storage_systems(template_md: str) -> None:
    """V52-O.6: the asymmetry section must live inside Storage Systems.

    Burying it elsewhere (e.g. in a footnote, or under Voice +
    Communication) would defeat the goal — agents reading about
    Storage Systems to understand the data model should see the
    asymmetry there.
    """
    storage_match = re.search(
        r"^## Storage Systems\s*$", template_md, flags=re.MULTILINE
    )
    assert storage_match, "template must have a `## Storage Systems` header"

    # Find the start of the next top-level `##` heading after Storage Systems.
    after = template_md[storage_match.end():]
    next_heading = re.search(r"^## (?!##)", after, flags=re.MULTILINE)
    assert next_heading, "could not find end of Storage Systems section"

    storage_body = after[: next_heading.start()]

    # The asymmetry header/section MUST live inside that body.
    assert re.search(
        r"KG\s+vs\s+Code\s+Graph", storage_body, flags=re.IGNORECASE
    ) or re.search(
        r"asymmetry", storage_body, flags=re.IGNORECASE
    ), (
        "V52-O.6 regression: the asymmetry section must be INSIDE the "
        "Storage Systems section (not in a separate top-level section)."
    )


def test_template_explicitly_names_no_shared_codegraph(template_md: str) -> None:
    """V52-O.6: the docs must spell out that there's no shared code-graph default.

    The subtle thing the asymmetry exists to communicate is that the
    KG pattern (shared collection auto-merged on every read) does NOT
    apply to code graph. Future agents should be able to grep for
    `SHARED_CODE_GRAPH` in the template + see that it's documented as
    explicitly absent.
    """
    # The template should mention that SHARED_CODE_GRAPH_COLLECTION
    # doesn't exist / shouldn't be added. Accept several phrasings.
    patterns = [
        r"no\s+`?SHARED_CODE_GRAPH_COLLECTION`?",
        r"there\s+is\s+no\s+`?SHARED_CODE_GRAPH",
        r"the\s+concept\s+doesn'?t\s+exist",
        r"no\s+shared\s+default",
    ]
    matched = any(
        re.search(p, template_md, flags=re.IGNORECASE) for p in patterns
    )
    assert matched, (
        "V52-O.6 regression: the template must explicitly state that "
        "code graph has NO shared-collection default (so a future "
        "agent doesn't add one for misguided 'symmetry')."
    )


def test_template_documents_codegraph_access_tables(template_md: str) -> None:
    """V52-O.6: the docs must name the launcher.db tables involved.

    Naming the tables (`codegraph_access`, `project_codegraph_extra_paths`)
    + the matching env vars (`VCT_CODE_GRAPH_ACCESS_LIST`,
    `VCT_KG_ACCESS_LIST`) gives an agent enough breadcrumbs to find the
    code without re-deriving the architecture.
    """
    must_mention = [
        "codegraph_access",
        "VCT_CODE_GRAPH_ACCESS_LIST",
        "VCT_KG_ACCESS_LIST",
        "project_codegraph_extra_paths",
    ]
    missing = [m for m in must_mention if m not in template_md]
    assert not missing, (
        f"V52-O.6 regression: the asymmetry section must name the "
        f"concrete tables + env vars. Missing: {missing}"
    )


def test_template_explains_why_asymmetry(template_md: str) -> None:
    """V52-O.6: the docs must include 1-2 paragraphs explaining the WHY.

    The plan specified: 'Add 1-2 paragraphs explaining "Why the
    asymmetry?" — codegraph isn't auto-merged because most projects
    don't care about cross-tenant code; KG is auto-merged because
    shared knowledge is the value-prop'.

    Verify both the rationale-for-KG-sharing and the
    rationale-for-codegraph-NOT-sharing show up.
    """
    # Look for the "Why" framing — accept the section header OR an
    # inline phrase.
    has_why_framing = bool(
        re.search(
            r"why\s+(?:the\s+)?asymmetry", template_md, flags=re.IGNORECASE
        )
    )
    assert has_why_framing, (
        "V52-O.6 regression: the asymmetry section must explicitly "
        "frame the WHY (try a `**Why the asymmetry?**` paragraph)."
    )

    # KG-side rationale: shared knowledge IS the value-prop.
    kg_rationale_patterns = [
        r"cross-project\s+value",
        r"useful\s+in\s+another\s+(?:app|project)",
        r"pattern\s+you\s+learned",
    ]
    assert any(
        re.search(p, template_md, flags=re.IGNORECASE)
        for p in kg_rationale_patterns
    ), (
        "V52-O.6 regression: the WHY must include the KG-side "
        "rationale (shared knowledge is cross-project value)."
    )

    # Code-graph-side rationale: leak risk / cross-tenant concern.
    cg_rationale_patterns = [
        r"leak",
        r"cross-tenant",
        r"proprietary",
        r"sensitive\s+source",
    ]
    assert any(
        re.search(p, template_md, flags=re.IGNORECASE)
        for p in cg_rationale_patterns
    ), (
        "V52-O.6 regression: the WHY must include the code-graph-side "
        "rationale (auto-merging would leak cross-tenant code)."
    )


def test_template_includes_comparison_table(template_md: str) -> None:
    """V52-O.6: the asymmetry section must include a comparison table.

    The plan specifies 'add a clear comparison table showing the KG vs
    codegraph design difference'. A markdown table is the right shape
    because it makes the symmetric/asymmetric concerns row-aligned and
    diff-grep-friendly.
    """
    # A markdown comparison table looks like:
    #   | something | KG | Code Graph |
    #   |---|---|---|
    # Find the section, then check for a table inside it.
    asym_match = re.search(
        r"###\s+KG\s+vs\s+Code\s+Graph",
        template_md,
        flags=re.IGNORECASE,
    )
    assert asym_match, "could not locate the asymmetry section header"

    # Take a 5000-char window after the header.
    section_body = template_md[asym_match.end() : asym_match.end() + 5000]

    # Look for a markdown table-delimiter row (pipes + dashes).
    table_pat = re.compile(r"\|\s*-+\s*\|", flags=re.MULTILINE)
    assert table_pat.search(section_body), (
        "V52-O.6 regression: the asymmetry section must include a "
        "markdown comparison table (`|---|---|---|` delimiter row)."
    )
