# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Versioned schema-migration runner (v0.2.60, verified NO-OP today).

This module is the single, generalized successor to the two hardcoded
migration scripts that ``install.py:_run_schema_migration_scripts`` ran
unconditionally on every install/update. It is driven by the EXISTING
registry (``vco_lib.artifact_version_registry`` +
``vco_lib.schema_versions.CANONICAL_VERSIONS``) — it does NOT invent a
parallel versioning system.

Design spec: ``.claude/context/plans/SPEC-v0260-migration-runner.md``.
Binding policy (derived-collection migration, user-locked 2026-06-16) lives in
``.claude/context/plans/PLAN-v0260-consolidated-update-system.md`` §5.

The runner walks ``migrations/<artifact_type>/<from>_to_<to>.<ext>`` edge
files, compares each artifact's DB-recorded version against canonical, and
applies the ascending contiguous edge(s) needed to reach canonical. The
BINDING POLICY for DERIVED Weaviate collections is:

  1. NOT stale (live fingerprint unchanged) → DO NOTHING.
  2. Stale AND a data-preserving migration script exists for the edge → run
     it (preserves vectors/data, NO re-embed). Preferred even for derived.
  3. Stale AND schema changed AND no preserving script → surface a
     regenerate-or-defer decision. The runner NEVER drops on its own; it
     records a ``pending_regenerate`` entry (GUI modal) and, headless, writes
     a ``schema_migration_needs_choice`` deferral. Re-embed/drop is the LAST
     resort, gated on explicit user choice.

NO-OP TODAY: ``migrations/`` ships empty → every artifact is either
NEVER_MATERIALIZED (registered at canonical, one idempotent DB write) or
UP_TO_DATE + not-stale (DO NOTHING). ``apply_edge`` is never called. Proven by
``tests/test_schema_migration_runner.py`` T7 + T8.

Piece 3 (the richer ``live_fingerprint_stale`` Weaviate fingerprint) and
Piece 4 (the launcher regenerate-or-defer modal + Tauri pair) are built later;
the ``live_drift_probe`` parameter is the injection seam Piece 3 swaps cleanly.
Today it defaults to ``vco_lib.project_init.detect_kg_schema_drift``.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import artifact_version_registry as avr
from . import schema_versions as sv

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeResult",
    "MigrationEdge",
    "MigrationRunReport",
    "RUST_OWNED_TYPES",
    "WEAVIATE_DERIVED_TYPES",
    "build_deferral_entries",
    "codegraph_class_names_for_prefix",
    "discover_edges",
    "resolve_codegraph_migration_inputs",
    "run_schema_migrations",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Edge filename grammar: ``<from>_to_<to>.<ext>``, ext ∈ {sql, sh, ps1, py}.
#: ``to`` MUST equal ``from + 1`` (one edge per release bump; the runner
#: asserts contiguity). SPEC §1 rule 1.
_EDGE_RE = re.compile(r"^(?P<from>\d+)_to_(?P<to>\d+)\.(?P<ext>sql|sh|ps1|py)$")

#: The DERIVED Weaviate collection artifact_types that participate in the
#: BINDING POLICY (live-fingerprint gate + regenerate-or-defer). These are the
#: Weaviate-backed entries of ``ARTIFACT_STATE_CLASSIFICATION`` that are
#: classified ``derived`` AND map to a live collection class. SPEC §2.2.
WEAVIATE_DERIVED_TYPES: frozenset[str] = frozenset(
    {
        "kg_collection",
        "shared_kg_collection",
        "development_collection",
        "diagrams_collection",
        "codegraph_collection",
    }
)

#: Artifact_types whose migrations are OWNED BY THE RUST / ``_schema_migrations``
#: LAYER, not by a Python per-edge script under ``migrations/<type>/``. These
#: are NOT Weaviate collections (so they skip the BINDING POLICY blocks) and
#: they have NO ``migrations/<type>/<from>_to_<to>.<ext>`` ladder by design —
#: their real schema changes are applied at LAUNCHER STARTUP by
#: ``launcher/src-tauri/vct-launcher-core/src/db/migrations.rs::MIGRATIONS``.
#:
#: The Python ``artifact_schema_versions`` registry tracks such a type only as
#: a VERSION-FLOOR ("refuse to start if launcher.db is ahead"; see
#: ``schema_versions.py`` LAUNCHER_DB_TABLE_SET_VERSION docstring) — it was
#: never meant to run its own edges. So when ``run_schema_migrations`` sees a
#: stored < canonical gap for one of these, the correct action is a REGISTER-
#: ONLY version advance (record the new canonical, demand NO edge script),
#: NOT the ``user_curated`` no-edge ``schema_migration_script_missing`` error.
#: Without this, every launcher.db migration bump re-fires a harmless-but-noisy
#: deferral on the next ``install.py --update`` (latent since v0.2.52; surfaced
#: by the v0.2.65 token-secret-index migration which bumped the constant 34→35
#: with no Python edge, as no launcher.db bump ever ships one).
#:
#: STRICT MEMBERSHIP RULE: a type belongs here ONLY if its schema is applied by
#: the Rust ``_schema_migrations`` runner. Do NOT add a genuinely user-curated
#: content shape (e.g. a JSON-payload shape whose forward migration must
#: actually rewrite stored rows) — that needs a real Python edge, and a
#: register-only advance would silently skip the data migration.
RUST_OWNED_TYPES: frozenset[str] = frozenset(
    {
        # launcher.db table-set version: tracks the highest applied entry in
        # migrations.rs::MIGRATIONS (applied at launcher startup). The Python
        # tracker is a version-floor only — see schema_versions.py's
        # LAUNCHER_DB_TABLE_SET_VERSION docstring + 033_artifact_schema_versions
        # .sql ("matches _schema_migrations max version").
        "launcher_db_table_set",
        # DELIBERATELY NOT HERE: ``rl_events_payload_shape``. Despite also being
        # orchestrator-wide + derived-but-not-Weaviate, its versioned shape is
        # the JSON content of ``rl_events.payload_json`` — written + migrated by
        # the PYTHON RL telemetry layer (claude_mcp_servers/rl_client/
        # telemetry_writer.py builds the v3 event; migrate_rl_jsonl_to_db.py
        # forward-migrates v2→v3 payloads). It is classified ``user_curated``
        # ("historical telemetry data"): a future bump must actually rewrite
        # stored payloads via a real Python edge, NOT a register-only advance.
        # The Rust migration 025 only creates the rl_events TABLE (covered by
        # launcher_db_table_set), not its payload-content shape.
    }
)

#: Artifact_types that are ORCHESTRATOR-WIDE — keyed ``project_id=NULL`` in the
#: registry, migrated only by the ROOT (orchestrator self-)update, never by a
#: per-project bundle update. STRUCTURAL CORRECTION (2026-06-16): the root
#: hosts the shared KG + the launcher.db-/orchestrator-global Layer-5 schemas.
#: Everything else is PER-PROJECT (keyed by the project's real project_id) and
#: migrated by that project's own bundle update. SPEC §2.1 + audit C2/C3.
ORCHESTRATOR_WIDE_TYPES: frozenset[str] = frozenset(
    {
        # Layer 1 — the shared KG lives on the root host (project_id=NULL).
        "shared_kg_collection",
        # Layer 5 — launcher.db / orchestrator-global telemetry shapes.
        "launcher_db_table_set",
        "rl_events_payload_shape",
    }
)

#: The 5 code-graph Weaviate class suffixes that compose the single
#: ``codegraph_collection`` artifact_type. All share one recorded version; the
#: runner resolves every ``<prefix>_<suffix>`` live class name under the
#: project's codegraph prefix. SPEC structural fix (codegraph is migratable).
_CODEGRAPH_CLASS_SUFFIXES: tuple[str, ...] = (
    "CodeModule",
    "CodeClass",
    "CodeFunction",
    "CodeAPI",
    "CodeInteraction",
)

#: Subprocess timeout for ``.sh``/``.ps1``/``.py`` edge scripts.
#:
#: v0.2.74 (Fable-review F5): raised 300 → 3600 and made env-overridable
#: (``VCT_EDGE_TIMEOUT_SECS``). The old 300s was a per-process deadline on a
#: WORKING path — exactly what the project's "no global deadlines; per-chunk
#: guards only" rule forbids: the 6_to_7 purge iterates + ``delete_by_id``s
#: O(N) rows, and on precisely the I/O-degraded machines it targets (the
#: mmap-storm boxes) a 16k-row purge could legitimately exceed 300s, get
#: SIGKILLed mid-purge, and write a failure deferral every update until enough
#: partial passes accumulated (monotone but noisy). The edge's INTERNAL ops
#: are already per-call bounded (weaviate client connect/read timeouts), so
#: this outer cap is a last-resort backstop against a truly wedged layer, not
#: a working-path deadline. Override per-machine: ``VCT_EDGE_TIMEOUT_SECS=0``
#: disables the cap entirely (None → subprocess.run waits indefinitely).
def _resolve_edge_timeout() -> "Optional[int]":
    raw = os.environ.get("VCT_EDGE_TIMEOUT_SECS", "").strip()
    if raw:
        try:
            v = int(raw)
            return None if v <= 0 else v
        except ValueError:
            pass  # malformed → fall through to the default
    return 3600


_EDGE_SUBPROCESS_TIMEOUT = _resolve_edge_timeout()

#: HIGH-2 machine-readable edge stdout sentinels (v0.2.74 migration delivery).
#: An edge subprocess that exits rc=0 is NOT proof the edge did its job — the
#: codegraph edges print "nothing to patch" and ``return 0`` when the codegraph
#: prefix is unresolvable from THEIR env (the A1 second-order trap: rc=0 with
#: zero work FALSELY advances the recorded version, permanently masking the
#: collection as migrated). Each edge now prints ONE of these sentinels on its
#: own stdout line so the runner can DISTINGUISH "applied" from "no-op because
#: scope was empty" and refuse to advance on the latter:
#:   * ``EDGE_NOOP_NO_PREFIX=1`` — the edge could not resolve a codegraph
#:     prefix / project scope from its env, so it touched NOTHING. rc=0 but the
#:     runner MUST NOT advance the recorded version (surface a deferral instead).
#:   * ``EDGE_APPLIED=1``        — the edge ran its real body (any number of
#:     rows/props touched, including zero for an already-migrated collection).
#:     Safe to advance.
#: An edge that emits NEITHER (an older edge, or one that crashed before the
#: print) is treated conservatively as "cannot confirm applied" ONLY when it is
#: a codegraph edge whose scope depends on the prefix — see
#: ``_edge_post_state_confirms_apply``. Non-codegraph edges keep the pre-v0.2.74
#: "rc=0 == success" contract (they have no unresolvable-scope failure mode).
_EDGE_SENTINEL_NOOP_NO_PREFIX = "EDGE_NOOP_NO_PREFIX=1"
_EDGE_SENTINEL_APPLIED = "EDGE_APPLIED=1"

#: Header directive grammar (``# @key: value`` or ``-- @key: value``). The
#: runner reads ``@destructive`` + ``@classification`` and cross-checks them
#: against ``is_derived`` (SPEC §1 rule 4 — fail-closed packaging guard).
_HEADER_RE = re.compile(
    r"^\s*(?:#|--)\s*@(?P<key>[a-zA-Z_]+)\s*:\s*(?P<value>\S+)", re.MULTILINE
)

#: SQLite-target directive: ``-- @db: launcher`` | ``-- @db: hub`` (default
#: launcher). SPEC §1 rule 3.
_DB_TARGET_RE = re.compile(
    r"^\s*--\s*@db\s*:\s*(?P<db>launcher|hub)", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationEdge:
    """One discovered migration edge file for a given artifact_type."""

    artifact_type: str
    from_version: int
    to_version: int
    path: Path
    ext: str  # "sql" | "sh" | "ps1" | "py"


@dataclass(frozen=True)
class EdgeResult:
    """Outcome of applying ONE edge (HIGH-2, v0.2.74).

    ``ok`` — the edge exited cleanly (rc=0 / .sql committed). ``stdout`` —
    the captured stdout of a subprocess edge (empty for .sql edges), used by
    the HIGH-2 sentinel check to distinguish a real apply from a
    "nothing to patch because scope was empty" rc=0 no-op that must NOT
    advance the recorded version.
    """

    ok: bool
    stdout: str = ""

    def __bool__(self) -> bool:  # back-compat: callers that just test truthiness
        return self.ok


def _coerce_edge_result(value: object) -> EdgeResult:
    """Normalize an ``_apply_edge`` return to an ``EdgeResult``.

    Back-compat: pre-v0.2.74 test spies (and any external monkeypatch) return
    a bare ``bool`` from ``_apply_edge``. Treat a bool as ``EdgeResult(ok=bool,
    stdout="")`` so the HIGH-2 sentinel check degrades gracefully (no stdout →
    no sentinel → the codegraph-specific "confirm applied" path applies its
    conservative default; non-codegraph edges keep the rc=0==success contract).
    """
    if isinstance(value, EdgeResult):
        return value
    return EdgeResult(ok=bool(value), stdout="")


@dataclass
class MigrationRunReport:
    """Outcome of a ``run_schema_migrations`` pass.

    Each list holds ``(artifact_type, artifact_name, detail)`` tuples (detail
    is a short human string) except where noted. The shim translates these
    into ``DeferralEntry`` rows; tests assert on the lists + counters.
    """

    #: Artifacts NEVER_MATERIALIZED → registered at canonical this run.
    registered: list[tuple[str, str, str]] = field(default_factory=list)
    #: Artifacts whose registry write FAILED (DB locked/unwritable) — C4: NOT
    #: counted as registered, so a broken DB doesn't falsely report success.
    register_failed: list[tuple[str, str, str]] = field(default_factory=list)
    #: B-2: Rust-owned register-only advances REFUSED because the real
    #: ``MAX(_schema_migrations.version)`` in launcher.db is BEHIND the Python
    #: canonical constant (the launcher binary is older than the code expects).
    #: Stamping the registry here would phantom-claim a schema the DB doesn't
    #: have — conservative default: do nothing, surface "update launcher".
    register_refused_db_behind: list[tuple[str, str, str]] = field(
        default_factory=list
    )
    #: Artifacts already UP_TO_DATE (and not stale) → no action.
    up_to_date: list[tuple[str, str, str]] = field(default_factory=list)
    #: REFUSE_DOWNGRADE artifacts (stored > canonical) — never mutated.
    refused: list[tuple[str, str, str]] = field(default_factory=list)
    #: Edge/contiguity/classification failures → schema_migration_* deferral.
    errors: list[tuple[str, str, str]] = field(default_factory=list)
    #: Edges actually applied (artifact_type, artifact_name, edge filename).
    applied: list[tuple[str, str, str]] = field(default_factory=list)
    #: DERIVED collections found stale via the live fingerprint.
    live_drift: list[tuple[str, str, str]] = field(default_factory=list)
    #: POLICY STEP 3: stale + no preserving script → regenerate-or-defer.
    #: Each carries the StaleDerived payload dict for the launcher modal.
    pending_regenerate: list[dict] = field(default_factory=list)
    #: ``--check``/dry-run: edges that WOULD run (no mutation occurred).
    planned: list[tuple[str, str, str]] = field(default_factory=list)

    def apply_edge_call_count(self) -> int:
        """Number of edges actually applied this run (NO-OP test asserts 0)."""
        return len(self.applied)


# ---------------------------------------------------------------------------
# Edge discovery + contiguity
# ---------------------------------------------------------------------------


def discover_edges(
    migrations_dir: Path,
    artifact_type: str,
    *,
    platform: str = sys.platform,
) -> list[MigrationEdge]:
    """Discover + sort the migration edges for ``artifact_type``.

    Walks ``migrations_dir / artifact_type`` for files matching
    ``<from>_to_<to>.<ext>``. Returns edges sorted ascending by
    ``from_version``. Malformed filenames are skipped (logged). For an edge
    shipping a cross-OS ``.sh``+``.ps1`` pair, the OS-appropriate sibling is
    selected (``.ps1`` on ``win32``, ``.sh`` otherwise); ``.sql``/``.py`` are
    OS-agnostic and always kept. SPEC §1 rule 3.

    Returns ``[]`` if the directory is absent (the NO-OP case) — never raises.
    """
    type_dir = migrations_dir / artifact_type
    if not type_dir.is_dir():
        return []

    # Bucket candidate files by (from, to) so we can pick the right OS sibling.
    by_edge: dict[tuple[int, int], dict[str, Path]] = {}
    for entry in sorted(type_dir.iterdir()):
        if not entry.is_file():
            continue
        m = _EDGE_RE.match(entry.name)
        if not m:
            logger.warning(
                "discover_edges: skipping malformed migration filename %s "
                "(expected <from>_to_<to>.{sql,sh,ps1,py})",
                entry,
            )
            continue
        frm = int(m.group("from"))
        to = int(m.group("to"))
        ext = m.group("ext")
        by_edge.setdefault((frm, to), {})[ext] = entry

    edges: list[MigrationEdge] = []
    is_win = platform.startswith("win")
    for (frm, to), exts in sorted(by_edge.items()):
        # OS-dispatch for the shell sibling pair; OS-agnostic for sql/py.
        chosen_ext: Optional[str] = None
        if "sql" in exts:
            chosen_ext = "sql"
        elif "py" in exts:
            chosen_ext = "py"
        elif is_win and "ps1" in exts:
            chosen_ext = "ps1"
        elif not is_win and "sh" in exts:
            chosen_ext = "sh"
        elif "ps1" in exts:
            # Only a .ps1 shipped but we're non-Windows (or vice-versa). The
            # multi-OS sibling discipline should prevent this; pick what's
            # there so a single-OS edge still runs rather than vanishing.
            chosen_ext = "ps1"
        elif "sh" in exts:
            chosen_ext = "sh"
        if chosen_ext is None:  # pragma: no cover - defensive
            continue
        edges.append(
            MigrationEdge(
                artifact_type=artifact_type,
                from_version=frm,
                to_version=to,
                path=exts[chosen_ext],
                ext=chosen_ext,
            )
        )
    return edges


def _assert_contiguous(
    edges: Sequence[MigrationEdge], *, start: int, end: int
) -> Optional[str]:
    """Return ``None`` if ``edges`` form a contiguous ascending ladder from
    ``start`` to ``end``; otherwise an error string naming the gap.

    Each edge must satisfy ``to == from + 1`` and the chain must cover
    exactly ``start → start+1 → ... → end`` with no gaps and no overlaps.
    SPEC §1 rule 1 + §2 contiguity assert.
    """
    if not edges:
        return f"no edges shipped for the {start}→{end} ladder"
    expected_from = start
    for edge in edges:
        if edge.to_version != edge.from_version + 1:
            return (
                f"edge {edge.path.name} is not a single-step edge "
                f"(to={edge.to_version} != from+1={edge.from_version + 1})"
            )
        if edge.from_version != expected_from:
            return (
                f"version gap: expected edge starting at v{expected_from} "
                f"but next edge is {edge.path.name} (from v{edge.from_version})"
            )
        expected_from = edge.to_version
    if expected_from != end:
        return (
            f"ladder ends at v{expected_from} but canonical is v{end} "
            f"(missing {expected_from}_to_{expected_from + 1} edge)"
        )
    return None


# ---------------------------------------------------------------------------
# Header parsing + classification cross-check
# ---------------------------------------------------------------------------


def _parse_header(path: Path) -> dict[str, str]:
    """Read the ``@key: value`` directive block from the top of an edge file.

    Reads at most the first 4 KiB (headers live in the first comment block).
    Returns a dict of lowercased keys → values. Soft-fail to ``{}`` on read
    error (the classification cross-check then treats absent directives as a
    packaging issue only when a mismatch is provable).
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        logger.debug("_parse_header: cannot read %s (%s)", path, exc)
        return {}
    return {
        m.group("key").lower(): m.group("value").lower()
        for m in _HEADER_RE.finditer(head)
    }


def _verify_edge_classification(
    edge: MigrationEdge, *, derived: bool, preserving_required: bool
) -> Optional[str]:
    """Cross-check the edge header against ``is_derived``. SPEC §1 rule 4, R2.

    Returns ``None`` if the header is consistent, else an error string. The
    check is FAIL-CLOSED: a declared ``@classification`` or ``@destructive``
    that contradicts the registry classification aborts that artifact.

    - ``@classification`` (if present) must match ``derived`` →
      ``"derived"``/``"user_curated"``.
    - When ``preserving_required`` (the edge is offered as a STEP-2 preserving
      script for a DERIVED collection), ``@destructive: yes`` is rejected — a
      preserving edge must NOT drop.
    """
    header = _parse_header(edge.path)

    declared = header.get("classification")
    if declared is not None:
        expected = "derived" if derived else "user_curated"
        if declared != expected:
            return (
                f"{edge.path.name}: @classification={declared!r} contradicts "
                f"registry classification {expected!r} for artifact_type "
                f"{edge.artifact_type!r} (packaging bug; refusing to run)"
            )

    if preserving_required:
        destructive = header.get("destructive")
        if destructive == "yes":
            return (
                f"{edge.path.name}: offered as a data-preserving edge for a "
                f"DERIVED collection but declares @destructive: yes — a "
                f"preserving edge must not drop (packaging bug)"
            )
    return None


# ---------------------------------------------------------------------------
# Edge application dispatch
# ---------------------------------------------------------------------------


def _resolve_target_db(edge: MigrationEdge, *, launcher_db: Path) -> Path:
    """Resolve the SQLite target for a ``.sql`` edge from its ``-- @db:``
    header (default launcher). The hub.db sits beside launcher.db."""
    head = ""
    try:
        head = edge.path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        pass
    m = _DB_TARGET_RE.search(head)
    target = m.group("db") if m else "launcher"
    if target == "hub":
        return launcher_db.with_name("hub.db")
    return launcher_db


def _apply_edge(
    edge: MigrationEdge,
    *,
    project_root: Path,
    launcher_db: Path,
    weaviate_url: str,
    env: Mapping[str, str],
) -> EdgeResult:
    """Apply ONE migration edge. Returns an :class:`EdgeResult` (``ok`` +
    captured ``stdout``); the runner writes the deferral, not the script.
    SPEC §2.8 (v0.2.74 HIGH-2: now returns stdout for the sentinel check).

    Dispatch by extension:
      - ``.sql`` → open the declared DB; split into statements and run them
        one-at-a-time inside a SINGLE manual transaction (autocommit off via
        ``isolation_level=None`` + explicit BEGIN). A mid-script failure
        ROLLS BACK every prior statement (truly atomic — NOT ``executescript``,
        which issues an implicit COMMIT and would half-apply). → ``ok=False``.
      - ``.sh``/``.ps1`` → ``subprocess.run`` exactly like install.py
        (cwd=project_root, timeout=300, OS-dispatched), passing ``env``, with
        stdout captured for the HIGH-2 sentinel check.
      - ``.py`` → ``subprocess.run([python, script])``.
    """
    if edge.ext == "sql":
        return _apply_sql_edge(edge, launcher_db=launcher_db)
    return _apply_subprocess_edge(
        edge,
        project_root=project_root,
        weaviate_url=weaviate_url,
        env=env,
    )


def _apply_sql_edge(edge: MigrationEdge, *, launcher_db: Path) -> EdgeResult:
    db_path = _resolve_target_db(edge, launcher_db=launcher_db)
    try:
        sql = edge.path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("_apply_sql_edge: cannot read %s (%s)", edge.path, exc)
        return EdgeResult(ok=False)

    statements = _split_sql_statements(sql)
    if not statements:
        # nothing to run (comment-only edge) → trivially atomic. .sql edges
        # have no unresolvable-scope failure mode, so they carry no sentinel.
        return EdgeResult(ok=True)

    # C1 fix: ``conn.executescript`` issues an implicit COMMIT before running,
    # so a mid-script failure half-applies and a manual ROLLBACK errors with
    # "no transaction is active". Run statements ONE AT A TIME inside a single
    # manual transaction with isolation_level=None (no implicit BEGIN), so a
    # failure on statement N leaves statements 1..N-1 ROLLED BACK — truly
    # atomic.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # PRAGMA foreign_keys must be set OUTSIDE a transaction (it's a no-op
        # inside one), so set it before BEGIN.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("COMMIT")
            return EdgeResult(ok=True)
        except sqlite3.Error as exc:
            logger.warning(
                "_apply_sql_edge: %s failed against %s (%s); rolling back",
                edge.path.name,
                db_path,
                exc,
            )
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return EdgeResult(ok=False)
    finally:
        conn.close()


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on top-level ``;``.

    Respects single-/double-quoted string literals (with SQL ``''`` escaping),
    ``--`` line comments, and ``/* */`` block comments so a semicolon inside a
    literal or comment does NOT split a statement. Trailing whitespace-/
    comment-only fragments are dropped. Good enough for migration edges (DDL +
    simple DML); migrations needing triggers/BEGIN...END bodies should ship a
    ``.py`` edge instead (documented in migrations/README.md).
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue
        # Not in any literal/comment.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _is_runnable_sql(stmt):
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if _is_runnable_sql(tail):
        statements.append(tail)
    return statements


def _is_runnable_sql(stmt: str) -> bool:
    """True if ``stmt`` contains an actual SQL keyword (not just comments)."""
    if not stmt:
        return False
    # Strip leading line/block comments to see if anything real remains.
    no_line = re.sub(r"--[^\n]*", "", stmt)
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)
    return bool(no_block.strip())


def _apply_subprocess_edge(
    edge: MigrationEdge,
    *,
    project_root: Path,
    weaviate_url: str,
    env: Mapping[str, str],
) -> EdgeResult:
    if edge.ext in ("sh", "ps1"):
        if edge.ext == "ps1":
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(edge.path),
            ]
        else:
            cmd = ["bash", str(edge.path)]
    elif edge.ext == "py":
        # `.py` edges are run as scripts (a self-contained migration module).
        cmd = [sys.executable, str(edge.path)]
    else:  # pragma: no cover - guarded by discover_edges
        logger.warning("_apply_subprocess_edge: unknown ext %s", edge.ext)
        return EdgeResult(ok=False)

    # Pass WEAVIATE_URL through so Weaviate-side edges target the right host
    # (the two folded scripts read $WEAVIATE_URL). Edge env extends the
    # caller's env; never strip it.
    sub_env = dict(env)
    sub_env.setdefault("WEAVIATE_URL", weaviate_url)
    # HIGH-2 (v0.2.74): capture stdout so the runner can read the edge's
    # machine-readable sentinel (EDGE_APPLIED / EDGE_NOOP_NO_PREFIX). We do NOT
    # capture stderr into the return (it keeps flowing to the parent's stderr
    # for logging via stderr=None), but stdout is captured as text.
    try:
        proc = subprocess.run(  # noqa: S603 — trusted edge scripts under migrations/
            cmd,
            cwd=str(project_root),
            timeout=_EDGE_SUBPROCESS_TIMEOUT,
            env=sub_env,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "_apply_subprocess_edge: %s spawn/timeout failed (%s)",
            edge.path.name,
            exc,
        )
        return EdgeResult(ok=False)
    stdout = proc.stdout or ""
    # Echo the edge's captured stdout to the parent's stdout so the install-log
    # narrative (the "6_to_7: purged N rows" lines) is preserved for the user.
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if proc.returncode != 0:
        logger.warning(
            "_apply_subprocess_edge: %s exited rc=%s (non-zero)",
            edge.path.name,
            proc.returncode,
        )
        return EdgeResult(ok=False, stdout=stdout)
    return EdgeResult(ok=True, stdout=stdout)


def _retry_command(edge: MigrationEdge) -> str:
    """The exact command a user can run to retry a failed edge."""
    if edge.ext == "ps1":
        return f"powershell.exe -File {edge.path}"
    if edge.ext == "sh":
        return f"bash {edge.path}"
    if edge.ext == "py":
        return f"{sys.executable} {edge.path}"
    return (
        "python -m vco_lib.project_init migrate-schema "
        f"--folder . --project-id <id>   # re-runs {edge.path.name}"
    )


# ---------------------------------------------------------------------------
# Artifact-name resolution
# ---------------------------------------------------------------------------

#: Map artifact_type → the env var holding its live Weaviate class name.
_TYPE_ENV_VAR: dict[str, str] = {
    "kg_collection": "KG_COLLECTION",
    "shared_kg_collection": "SHARED_KG_COLLECTION",
    "development_collection": "DEVELOPMENT_COLLECTION",
    "diagrams_collection": "DIAGRAMS_COLLECTION",
}


def _resolve_codegraph_prefix(env: Mapping[str, str]) -> Optional[str]:
    """Resolve the project's codegraph Weaviate class prefix (e.g. ``VCODev``).

    Priority mirrors ``vco_lib.paths.resolve_project_name`` but stays env-only
    (no hub round-trip — the runner is called from install/post-bundle with a
    fully-resolved env): ``CODE_GRAPH_PROJECT`` (already the sanitized prefix,
    used verbatim by the analyzer) → ``PROJECT_NAME``.

    NIT-C (2026-06-16): the PROJECT_NAME *fallback* MUST derive the prefix the
    SAME way the code-graph analyzer does — via
    ``analyze_code_graph._sanitize_collection_prefix`` →
    ``vco_lib.project_naming.canonical_class_prefix`` (underscore-PRESERVING),
    NOT ``sanitize_for_weaviate_class`` (underscore-DROPPING, used for KG/Dev/
    Diagrams). Reusing the analyzer's exact helper (``canonical_class_prefix``
    + its legacy-regex fallback on ``ValueError``) guarantees a future
    codegraph edge probes the class name the analyzer actually wrote, even for
    names containing ``_``/``-``. Returns ``None`` only when neither env is set
    (the runner then skips codegraph). In practice CODE_GRAPH_PROJECT is set
    at install/post-bundle time, so the fallback is the unlikely path.
    """
    cg = (env.get("CODE_GRAPH_PROJECT") or "").strip()
    if cg:
        return cg
    pn = (env.get("PROJECT_NAME") or "").strip()
    if not pn:
        return None
    # Same helper + fallback the analyzer uses for class-prefix derivation.
    from .project_naming import canonical_class_prefix

    try:
        return canonical_class_prefix(pn)
    except ValueError:
        # Legacy fallback identical to analyze_code_graph._sanitize_
        # collection_prefix / codegraph_to_mermaid._sanitize_collection_prefix:
        # replace non-alnum-underscore with underscore, uppercase a leading
        # lowercase letter. Never raises; keeps the runner in lock-step with
        # whatever the analyzer wrote.
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", pn)
        if sanitized and sanitized[0].isalpha() and not sanitized[0].isupper():
            sanitized = sanitized[0].upper() + sanitized[1:]
        return sanitized or None


def codegraph_class_names_for_prefix(prefix: str) -> list[str]:
    """Return the 5 ``<prefix>_Code*`` live class names for a codegraph prefix.

    The single home for the ``<prefix> + _CODEGRAPH_CLASS_SUFFIXES`` join so
    install.py / project_init build the SAME explicit ``artifact_names`` the
    runner would resolve internally. Returns ``[]`` for an empty/None prefix.
    """
    if not prefix:
        return []
    return [f"{prefix}_{suffix}" for suffix in _CODEGRAPH_CLASS_SUFFIXES]


def resolve_codegraph_migration_inputs(
    env: Mapping[str, str],
    *,
    db_path: Optional[Path],
    project_id: Optional[str],
    project_name: Optional[str] = None,
) -> tuple[dict[str, list[str]], dict[str, str], Optional[str]]:
    """A1 (v0.2.74): resolve the codegraph migration inputs ENV-INDEPENDENTLY.

    The PRIMARY bug this closes: ``install.py`` calls ``run_schema_migrations``
    with ``env=os.environ`` — but the install.py process env has NO
    ``CODE_GRAPH_PROJECT`` / ``PROJECT_NAME``. So the runner's internal
    ``_resolve_codegraph_prefix(env)`` returns ``None`` →
    ``_resolve_artifact_names('codegraph_collection')`` returns ``[]`` → the
    codegraph migration loop iterates ZERO times, silently, leaving the
    registry frozen and the collection never migrated.

    This helper resolves the codegraph prefix from the SSOT
    (``launcher.db::project_codegraph_bindings.collection_prefix``, RO-URI) —
    NOT from the caller's env — and returns:

      * ``artifact_names`` — ``{"codegraph_collection": [<prefix>_CodeModule,
        ...]}`` when a prefix resolved, else ``{}`` (nothing to override; the
        runner's own env resolution stays in charge for non-codegraph types).
        Passing this EXPLICITLY makes the runner iterate the codegraph loop
        even though the caller env lacks CODE_GRAPH_PROJECT.
      * ``augmented_env`` — ``dict(env)`` with ``CODE_GRAPH_PROJECT`` set to the
        resolved prefix (when resolved). This is threaded down to each edge
        SUBPROCESS via ``_apply_subprocess_edge``'s ``sub_env = dict(env)`` so
        the edge scripts' OWN ``_resolve_codegraph_prefix()`` resolves the same
        prefix (closing the A1 second-order trap: an edge run without
        CODE_GRAPH_PROJECT prints "nothing to patch", exits 0, and would falsely
        advance the version).
      * ``prefix`` — the resolved prefix (or ``None``), for logging.

    Resolution order for the prefix:
      1. ``env['CODE_GRAPH_PROJECT']`` if the caller already set it (respect it).
      2. ``launcher_db_reader.get_codegraph_prefix(project_id)`` (SSOT).
      3. ``launcher_db_reader.get_codegraph_prefix(project_name)`` fallback.
      4. ``env['PROJECT_NAME']`` → analyzer-style prefix derivation (last resort;
         mirrors ``_resolve_codegraph_prefix``).

    Soft-fail: any launcher.db error yields ``({}, dict(env), None)`` — the
    caller passes the untouched env + no override, and the runner behaves
    exactly as before (no regression; the collection is retried next update).
    """
    base_env = dict(env)

    # 1. Respect an already-set CODE_GRAPH_PROJECT in the caller's env.
    prefix = (base_env.get("CODE_GRAPH_PROJECT") or "").strip() or None

    # 2 + 3. SSOT read from launcher.db (RO-URI, soft-fail).
    if not prefix and db_path is not None:
        try:
            from . import launcher_db_reader as ldr

            if project_id:
                prefix = ldr.get_codegraph_prefix(project_id, db_path=db_path)
            if not prefix and project_name:
                prefix = ldr.get_codegraph_prefix(project_name, db_path=db_path)
        except Exception as exc:  # never block the migration on a DB read
            logger.debug(
                "resolve_codegraph_migration_inputs: launcher.db read "
                "failed (%s); falling back to env",
                exc,
            )

    # 4. Last resort: analyzer-style derivation from PROJECT_NAME in env.
    if not prefix:
        prefix = _resolve_codegraph_prefix(base_env)

    if not prefix:
        # Nothing resolved anywhere — return the untouched env + no override so
        # the runner's own (env-based) resolution stays in charge and the
        # codegraph loop simply no-ops this pass (retried next update).
        return ({}, base_env, None)

    base_env["CODE_GRAPH_PROJECT"] = prefix
    artifact_names = {
        "codegraph_collection": codegraph_class_names_for_prefix(prefix)
    }
    return (artifact_names, base_env, prefix)


def _resolve_artifact_names(
    artifact_type: str,
    env: Mapping[str, str],
    artifact_names: Optional[Mapping[str, list[str]]],
) -> list[str]:
    """Resolve the concrete artifact_name(s) to check for ``artifact_type``.

    Priority:
      1. Explicit ``artifact_names[artifact_type]`` (the launcher passes
         resolved class names) — used verbatim.
      2. ``codegraph_collection`` → the project's 5 ``<prefix>_Code*`` live
         class names (CodeModule/Class/Function/API/Interaction). All share
         one recorded version; resolved from the codegraph prefix.
      3. The type's env var (e.g. ``KG_COLLECTION``) for the single-class
         Weaviate collections.
      4. A stable sentinel ``"default"`` for non-collection artifacts
         (vocabularies, row shapes, bundle, node-formats) — one row per
         project keyed on the fixed name (mirrors
         ``_NODE_FORMATS_ARTIFACT_NAME = "default"`` in project_init.py).

    Returns ``[]`` when a Weaviate-class type has no resolvable name (env
    unset) so the runner skips it cleanly rather than registering a bogus row.
    """
    if artifact_names and artifact_type in artifact_names:
        return [n for n in artifact_names[artifact_type] if n]

    if artifact_type == "codegraph_collection":
        prefix = _resolve_codegraph_prefix(env)
        if not prefix:
            return []
        return [f"{prefix}_{suffix}" for suffix in _CODEGRAPH_CLASS_SUFFIXES]

    env_var = _TYPE_ENV_VAR.get(artifact_type)
    if env_var is not None:
        name = (env.get(env_var) or "").strip()
        return [name] if name else []

    # Non-Weaviate-class artifact (vocabularies, shapes, bundle, etc.) — one
    # row per project keyed on the stable sentinel.
    return ["default"]


def _live_probe_for(
    artifact_type: str,
    artifact_name: str,
    weaviate_url: str,
    live_drift_probe: Callable[[str, str], tuple[bool, list[str]]],
    codegraph_probe: Callable[[str, str], tuple[bool, list[str]]],
) -> tuple[bool, list[str]]:
    """Dispatch the staleness probe to the right detector per artifact_type.

    Codegraph classes use ``codegraph_probe`` (an existence/shape probe today,
    a CodeSage fingerprint in Piece 3); all other Weaviate-derived collections
    use ``live_drift_probe`` (``detect_kg_schema_drift`` today). Both are
    injectable + failure-soft so Piece 3 swaps either cleanly. SPEC §2.2.
    """
    if artifact_type == "codegraph_collection":
        return codegraph_probe(weaviate_url, artifact_name)
    return live_drift_probe(weaviate_url, artifact_name)


# ---------------------------------------------------------------------------
# Live-fingerprint adapter (Piece 3 swaps this cleanly)
# ---------------------------------------------------------------------------


def _default_live_drift_probe(
    weaviate_url: str, artifact_name: str
) -> tuple[bool, list[str]]:
    """Default ``live_drift_probe`` → ``live_fingerprint_stale`` (Piece 3).

    The POLICY STEP 1 "is it stale?" test. Re-embed is gated on an ACTUAL
    embedding-invalidating change: a missing CORE named-vector slot, a
    missing ``indexNullState``, OR a slot whose live stored-vector dim
    differs from the catalog. A purely-additive optional-slot gap is NOT
    stale (handled by copy-with-vectors / patch_props, no re-embed). See
    ``vco_lib.project_init.live_fingerprint_stale`` +
    ``vco_lib.weaviate_schema.embedding_schema_fingerprint``. Failure-soft
    (Weaviate down / class absent → not stale), preserving the §5 NO-OP
    guarantee on a containers-down install.
    """
    try:
        from .project_init import live_fingerprint_stale

        return live_fingerprint_stale(weaviate_url, artifact_name)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_default_live_drift_probe: probe failed (%s)", exc)
        return (False, [])


def _default_codegraph_drift_probe(
    weaviate_url: str, artifact_name: str
) -> tuple[bool, list[str]]:
    """Default codegraph staleness probe (existence/shape, failure-soft).

    Piece 3 will supersede this with a CodeSage-vector + class-shape
    fingerprint (codegraph vectors are expensive to regenerate, so the
    preserving / regenerate-or-defer policy applies to codegraph too). Until
    then this is conservative: it never reports stale (always
    ``(False, [])``), which keeps the NO-OP-today guarantee intact for
    codegraph and means a codegraph recreate is only ever driven by a real
    recorded-version bump (an actual migration edge), not by this probe.
    Injectable so Piece 3 swaps it without touching the runner.
    """
    return (False, [])


# ---------------------------------------------------------------------------
# Weaviate existence / row-count probes (A2 + HIGH-2, v0.2.74)
# ---------------------------------------------------------------------------


def _weaviate_class_object_count(
    weaviate_url: str, class_name: str
) -> Optional[int]:
    """Return the object count of ``class_name`` via Weaviate's GraphQL
    Aggregate, or ``None`` when unreachable / malformed / absent-class-error.

    Mirrors ``vco_lib.kg_binding_heal._count_weaviate_class_objects`` (kept a
    thin local copy so the runner has no import cycle into the KG-heal module).
    ``None`` means "unknown" — callers MUST NOT read it as zero (a transient
    network blip must never make a populated collection look empty). ``0`` is a
    genuine empty/absent class. Soft-fails throughout; never raises.
    """
    import json
    import urllib.request

    base = (weaviate_url or "http://localhost:8081").rstrip("/")
    # class_name is a derived Weaviate class name (prefix + fixed suffix) — shape
    # -validate anyway to fail-closed against a future untrusted caller.
    if not class_name or not class_name.replace("_", "").isalnum():
        return None
    query = "{ Aggregate { " f"{class_name} {{ meta {{ count }} }}" " } }"
    try:
        data = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/graphql",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)  # noqa: S310 (localhost)
    except Exception:
        return None
    try:
        if resp.getcode() != 200:
            return None
        payload = json.loads(resp.read())
        agg = payload.get("data", {}).get("Aggregate", {}) or {}
        rows = agg.get(class_name) or []
        if not rows:
            return 0
        count = (rows[0].get("meta") or {}).get("count")
        return count if isinstance(count, int) else None
    except Exception:
        return None


def _codegraph_collection_has_rows(
    weaviate_url: str, artifact_names: Sequence[str]
) -> Optional[bool]:
    """Does ANY of the codegraph classes in ``artifact_names`` hold >=1 row?

    Returns:
        True  — at least one class has a positive object count (the collection
                EXISTS WITH DATA → the A2 ladder-replay must run so a pre-
                registry existing collection gets the schema ladder applied
                rather than being stamped straight to canonical as if born
                fresh).
        False — every probeable class returned a definite 0 (genuinely-empty /
                absent → the born-at-canonical NEVER_MATERIALIZED stamp is
                correct).
        None  — every probe returned "unknown" (Weaviate down / all errored):
                cannot confirm existence. The caller treats ``None``
                conservatively as "do NOT run the ladder" (keeps the §5 NO-OP
                guarantee on a containers-down install; the reconcile / next
                update retries when Weaviate is reachable).

    A definite 0 from at least one class combined with unknowns from the rest
    still returns None (we cannot prove the whole collection is empty), except
    when EVERY probe is a definite 0 → False.
    """
    saw_unknown = False
    saw_zero = False
    for name in artifact_names:
        cnt = _weaviate_class_object_count(weaviate_url, name)
        if cnt is None:
            saw_unknown = True
            continue
        if cnt > 0:
            return True
        saw_zero = True
    if saw_unknown:
        # At least one class is unknown and none had rows → cannot confirm.
        return None
    if saw_zero:
        return False
    return None


def _edge_stdout_says_no_prefix(stdout: str) -> bool:
    """True if the edge printed the ``EDGE_NOOP_NO_PREFIX`` sentinel — i.e. it
    could not resolve a codegraph scope from its env and touched nothing."""
    return _EDGE_SENTINEL_NOOP_NO_PREFIX in (stdout or "")


def _edge_stdout_says_applied(stdout: str) -> bool:
    """True if the edge printed the ``EDGE_APPLIED`` sentinel — it ran its real
    body (any number of rows/props touched, including zero already-migrated)."""
    return _EDGE_SENTINEL_APPLIED in (stdout or "")


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_schema_migrations(
    *,
    db_path: Path,
    project_id: Optional[str],
    migrations_dir: Path,
    deferral_report: object = None,  # DeferralReport-like; runner doesn't write
    weaviate_url: str = "http://localhost:8081",
    env: Mapping[str, str],
    check: bool = False,
    artifact_names: Optional[Mapping[str, list[str]]] = None,
    live_drift_probe: Optional[Callable[[str, str], tuple[bool, list[str]]]] = None,
    codegraph_drift_probe: Optional[
        Callable[[str, str], tuple[bool, list[str]]]
    ] = None,
    now_ms: Optional[int] = None,
    project_root: Optional[Path] = None,
    include_orchestrator_wide: bool = True,
) -> MigrationRunReport:
    """Run version-gated schema migrations for the artifacts of ONE project.

    STRUCTURAL MODEL (per-project, 2026-06-16): projects update SEPARATELY.

    * ``project_id`` is the project whose PER-PROJECT artifacts are migrated
      (its KG / Development / Diagrams / Codegraph collections, its row-shape
      / vocabulary rows). On the ROOT (orchestrator self-)update this is the
      root project's REAL id; on a per-project bundle update it is that
      project's id.
    * Artifacts in :data:`ORCHESTRATOR_WIDE_TYPES` (the shared KG + Layer-5
      launcher/global telemetry shapes) are keyed ``project_id=NULL`` and are
      ONLY migrated when ``include_orchestrator_wide`` is True — i.e. by the
      ROOT update, never by a non-root project's bundle update. This is the
      fix for audit C2 (no NULL-keying of per-project collections → no
      double-registration with the node-formats handler) and C3 (the
      live-drift probe is scoped to the one project being updated).

    The runner itself NEVER writes deferral entries — it returns a
    :class:`MigrationRunReport` the install.py shim / launcher translate into
    ``DeferralEntry`` rows. ``deferral_report`` is accepted for signature
    stability with the shim but is intentionally unused inside the runner.

    Args:
        db_path: launcher.db (the registry lives here).
        project_id: the project whose per-project artifacts are migrated.
        migrations_dir: ``<root>/migrations`` (empty today → no-op).
        weaviate_url: target Weaviate for edge scripts + the live probe.
        env: resolved env (KG_COLLECTION, SHARED_KG_COLLECTION, CODE_GRAPH_
            PROJECT, ...).
        check: dry-run — plan only, NO registry write, NO edge apply.
        artifact_names: explicit class names per type (launcher-resolved).
        live_drift_probe: POLICY STEP 1 staleness test for the single-class
            collections (Piece 3 swaps this).
        codegraph_drift_probe: POLICY STEP 1 staleness test for the 5
            codegraph classes (Piece 3 gives it a CodeSage fingerprint).
        now_ms: override materialized_at (testing).
        project_root: cwd for edge subprocesses (defaults to migrations_dir's
            parent, i.e. the orchestrator clone root).
        include_orchestrator_wide: migrate the ``project_id=NULL`` artifacts
            too (True on the ROOT update; False on a non-root bundle update).

    Returns:
        :class:`MigrationRunReport` summarizing every artifact's outcome.
    """
    report = MigrationRunReport()
    when = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    root = project_root or migrations_dir.parent
    Status = avr.ArtifactVersionStatus
    # A2 (v0.2.74): codegraph_collection's 5 class names share ONE recorded
    # version + ONE edge ladder (each edge patches all applicable classes on a
    # single run). When the NEVER_MATERIALIZED-but-exists ladder-replay fires
    # for the FIRST resolved class name, it runs the ladder for the whole
    # collection — so subsequent class names in the same type must NOT re-run
    # it. This set records artifact_types already handled that way this pass.
    _ladder_replayed_types: set[str] = set()
    # Resolve probe defaults at CALL time (not as bound default-arg values) so
    # a monkeypatch of the module-level default — and Piece 3's swap of
    # _default_live_drift_probe for the richer fingerprint — takes effect
    # without re-importing. Callers can still inject explicit probes.
    if live_drift_probe is None:
        live_drift_probe = _default_live_drift_probe
    if codegraph_drift_probe is None:
        codegraph_drift_probe = _default_codegraph_drift_probe

    for artifact_type in sorted(sv.CANONICAL_VERSIONS):
        # Defensive: an artifact_type in CANONICAL_VERSIONS that somehow lacks
        # a classification entry would KeyError; skip with a WARNING rather
        # than crash (mirrors stale_artifacts_for_project's posture).
        try:
            canonical = sv.canonical_version(artifact_type)
            derived = sv.is_derived(artifact_type)
        except KeyError:
            logger.warning(
                "run_schema_migrations: unknown artifact_type %r; skipping",
                artifact_type,
            )
            continue

        # ---- Per-project vs orchestrator-wide keying (structural fix) ----
        is_wide = artifact_type in ORCHESTRATOR_WIDE_TYPES
        if is_wide and not include_orchestrator_wide:
            # A non-root project's bundle update must not touch the shared KG
            # / launcher-global shapes. Skip entirely.
            continue
        # Orchestrator-wide rows are keyed NULL; per-project rows by project_id.
        effective_pid = None if is_wide else project_id

        for artifact_name in _resolve_artifact_names(
            artifact_type, env, artifact_names
        ):
            status = avr.check_artifact_version(
                db_path,
                project_id=effective_pid,
                artifact_type=artifact_type,
                artifact_name=artifact_name,
            )

            if status == Status.REFUSE_DOWNGRADE:
                # Stored > canonical: launcher.db written by a NEWER
                # orchestrator. Never mutate; surface for the shim's deferral.
                report.refused.append(
                    (
                        artifact_type,
                        artifact_name,
                        f"stored schema is newer than canonical v{canonical}",
                    )
                )
                continue

            if status == Status.NEVER_MATERIALIZED:
                # A2 (v0.2.74): NEVER_MATERIALIZED assumes born-at-canonical.
                # That is TRUE for a genuinely-fresh project but FALSE for a
                # pre-registry EXISTING codegraph collection (installed before
                # V52-AG, so its registry row was never written): stamping it
                # straight to canonical with ZERO edges would PERMANENTLY skip
                # the schema ladder (add-property edges 4→5→6 + the .claude/
                # state purge 6→7 would never reach it). So for a DERIVED
                # collection that has an edge ladder AND actually EXISTS WITH
                # DATA in Weaviate, replay the WHOLE contiguous ladder from the
                # earliest edge (idempotent-from-earliest: 4_to_5/5_to_6 are
                # add-property-if-absent, 6_to_7 is exact-substring delete-only).
                # _apply_edges_preserving registers at canonical after the final
                # edge. Runs ONCE per artifact_type (the 5 codegraph classes
                # share one ladder — see _ladder_replayed_types).
                if (
                    not check
                    and artifact_type in WEAVIATE_DERIVED_TYPES
                    and artifact_type not in _ladder_replayed_types
                ):
                    replay_names = _resolve_artifact_names(
                        artifact_type, env, artifact_names
                    )
                    edges = discover_edges(migrations_dir, artifact_type)
                    _has_rows = _codegraph_collection_has_rows(
                        weaviate_url, replay_names
                    ) if edges else False
                    # v0.2.74 (Fable-review F1): a `None` existence probe means
                    # Weaviate was UNREACHABLE — we cannot distinguish "fresh
                    # project, no data" from "existing collection we couldn't
                    # see". Falling through to the born-at-canonical stamp here
                    # would PERMANENTLY mask an existing collection as migrated
                    # (v7 with zero edges run → the purge never fires, and every
                    # future run short-circuits on UP_TO_DATE). So on None, SKIP
                    # this artifact entirely — leave it unregistered so the next
                    # update (with Weaviate up) retries. Mirrors the A3
                    # reconcile's conservative None handling. Only a DEFINITE
                    # False (all classes answered count 0) may take the
                    # born-at-canonical stamp below.
                    if _has_rows is None:
                        report.errors.append(
                            (
                                artifact_type,
                                artifact_name,
                                "Weaviate unreachable during the NEVER_"
                                "MATERIALIZED existence probe; NOT stamping "
                                "born-at-canonical (would mask an existing "
                                "collection); will retry next update "
                                "[schema_migration_probe_unreachable]",
                            )
                        )
                        continue
                    if _has_rows:
                        _ladder_replayed_types.add(artifact_type)
                        _apply_edges_preserving(
                            artifact_type=artifact_type,
                            artifact_name=artifact_name,
                            edges=edges,
                            stored=edges[0].from_version,
                            canonical=canonical,
                            derived=derived,
                            env=env,
                            weaviate_url=weaviate_url,
                            launcher_db=db_path,
                            project_id=effective_pid,
                            root=root,
                            report=report,
                            check=check,
                            when=when,
                            # register at canonical on the final edge (the row
                            # doesn't exist yet — this IS the materialization).
                            register_on_success=True,
                        )
                        continue
                    # No ladder, OR a DEFINITE empty (every class answered
                    # count 0 — genuinely fresh) → fall through to the
                    # born-at-canonical stamp below. The unreachable/None case
                    # was handled above (skip + retry; never stamp on doubt).

                # Fresh / pre-V52-AG artifact: born at canonical by the seed
                # path. Only RECORD the version so a FUTURE bump is detectable
                # (you can't migrate from v0 of something just created at vN).
                # SPEC §2.1; mirrors project_init.py:9046-9056.
                #
                # C4: only report "registered" when the write actually
                # succeeded. register_artifact_version returns False on a
                # locked/broken DB — counting it as registered would falsely
                # report success every run. On False, record register_failed.
                if check:
                    report.registered.append(
                        (artifact_type, artifact_name, f"would register at v{canonical}")
                    )
                    continue
                ok = avr.register_artifact_version(
                    db_path,
                    project_id=effective_pid,
                    artifact_type=artifact_type,
                    artifact_name=artifact_name,
                    schema_version=canonical,
                    materialized_at=when,
                )
                if ok:
                    report.registered.append(
                        (artifact_type, artifact_name, f"registered at v{canonical}")
                    )
                else:
                    logger.warning(
                        "run_schema_migrations: register_artifact_version "
                        "returned False for %s/%s (DB locked?); not counting "
                        "as registered",
                        artifact_type,
                        artifact_name,
                    )
                    report.register_failed.append(
                        (
                            artifact_type,
                            artifact_name,
                            "registry write failed (DB locked/unwritable); "
                            "will retry next run",
                        )
                    )
                continue

            if status == Status.UP_TO_DATE:
                # LIVE-state safety net (R1). For Weaviate-backed DERIVED
                # collections this enters the BINDING POLICY (steps 1-3).
                if artifact_type in WEAVIATE_DERIVED_TYPES:
                    stale, changed_fields = _live_probe_for(
                        artifact_type, artifact_name, weaviate_url,
                        live_drift_probe, codegraph_drift_probe,
                    )
                    if not stale:
                        # POLICY STEP 1 — not stale → DO NOTHING.
                        report.up_to_date.append(
                            (artifact_type, artifact_name, "live schema current")
                        )
                        continue
                    report.live_drift.append(
                        (
                            artifact_type,
                            artifact_name,
                            ", ".join(changed_fields) or "schema fingerprint changed",
                        )
                    )
                    _resolve_stale_derived(
                        artifact_type=artifact_type,
                        artifact_name=artifact_name,
                        migrations_dir=migrations_dir,
                        canonical=canonical,
                        stored=canonical,  # recorded version is current
                        changed_fields=changed_fields,
                        env=env,
                        weaviate_url=weaviate_url,
                        launcher_db=db_path,
                        project_id=effective_pid,
                        root=root,
                        report=report,
                        check=check,
                        when=when,
                    )
                    continue
                report.up_to_date.append(
                    (artifact_type, artifact_name, "recorded version current")
                )
                continue

            # status is RECREATE_NEEDED or UPGRADE_IN_PLACE_NEEDED →
            # stored < canonical (a real recorded-version bump to migrate).
            stored = _read_stored_version(
                db_path,
                project_id=effective_pid,
                artifact_type=artifact_type,
                artifact_name=artifact_name,
            )
            edges = discover_edges(migrations_dir, artifact_type)

            if artifact_type in WEAVIATE_DERIVED_TYPES:
                # ---- BINDING POLICY for DERIVED collections (SPEC §2.6) ----
                if edges:
                    # POLICY STEP 2 — preserving script exists → run it.
                    _apply_edges_preserving(
                        artifact_type=artifact_type,
                        artifact_name=artifact_name,
                        edges=edges,
                        stored=stored,
                        canonical=canonical,
                        derived=derived,
                        env=env,
                        weaviate_url=weaviate_url,
                        launcher_db=db_path,
                        project_id=effective_pid,
                        root=root,
                        report=report,
                        check=check,
                        when=when,
                    )
                else:
                    # POLICY STEP 3 — schema changed, NO preserving script.
                    # NEVER silently drop; surface regenerate-or-defer.
                    _request_regenerate_or_defer(
                        artifact_type=artifact_type,
                        artifact_name=artifact_name,
                        stored=stored,
                        canonical=canonical,
                        changed_fields=[f"recorded v{stored} < canonical v{canonical}"],
                        report=report,
                        check=check,
                    )
                continue

            # ---- Rust-owned types: REGISTER-ONLY version advance ----
            # The schema change was already applied at launcher startup by
            # migrations.rs::MIGRATIONS; the Python registry only tracks the
            # version floor. No per-edge script exists by design, so just
            # record the new canonical (a clean success, NOT a deferral). If
            # an edge HAS been shipped for one of these (it shouldn't), fall
            # through to the contiguity-checked apply path below so a real
            # ladder still runs rather than being silently swallowed.
            if not edges and artifact_type in RUST_OWNED_TYPES:
                # B-2 GUARD: the register-only advance is safe ONLY when the
                # Rust layer has ALREADY applied the schema at launcher startup.
                # Trust the REAL DB, not the Python constant: read
                # MAX(_schema_migrations.version). If it is BEHIND canonical the
                # launcher binary is older than this code expects — stamping the
                # registry to `canonical` would phantom-claim a schema the tables
                # don't have (any later consumer trusting the registry floor
                # would query a column that doesn't exist). Conservative default
                # ("do nothing rather than guess"): refuse the advance, leave the
                # registry at `stored`, and surface a clear "update launcher"
                # signal. `None` (table missing / unreadable DB) is treated the
                # same as behind — we cannot positively confirm the precondition.
                db_max = _read_launcher_db_max_migration(db_path)
                if db_max is None or db_max < canonical:
                    detail = (
                        f"refused v{stored}→v{canonical}: launcher.db "
                        f"_schema_migrations MAX="
                        f"{'unknown' if db_max is None else db_max} "
                        f"< canonical v{canonical} — launcher binary is older "
                        f"than the code expects; update the launcher, then "
                        f"re-run. NOT stamping a phantom schema version."
                    )
                    report.register_refused_db_behind.append(
                        (artifact_type, artifact_name, detail)
                    )
                    logger.warning(
                        "run_schema_migrations: %s", detail
                    )
                    continue
                if check:
                    report.registered.append(
                        (
                            artifact_type,
                            artifact_name,
                            f"would advance v{stored}→v{canonical} "
                            f"(Rust-owned; schema applied at launcher startup)",
                        )
                    )
                    continue
                ok = avr.register_artifact_version(
                    db_path,
                    project_id=effective_pid,
                    artifact_type=artifact_type,
                    artifact_name=artifact_name,
                    schema_version=canonical,
                    materialized_at=when,
                )
                if ok:
                    report.registered.append(
                        (
                            artifact_type,
                            artifact_name,
                            f"advanced v{stored}→v{canonical} "
                            f"(Rust-owned; schema applied at launcher startup)",
                        )
                    )
                else:
                    logger.warning(
                        "run_schema_migrations: register_artifact_version "
                        "returned False for Rust-owned %s/%s (DB locked?); not "
                        "counting as advanced",
                        artifact_type,
                        artifact_name,
                    )
                    report.register_failed.append(
                        (
                            artifact_type,
                            artifact_name,
                            "registry write failed (DB locked/unwritable); "
                            "will retry next run",
                        )
                    )
                continue

            # ---- user_curated forward-only upgrade (SPEC §2.7) ----
            # v0.2.74 (Fable-review F9): slice the retained ladder to the edges
            # from `stored` forward — the SAME latent contiguity trap as the
            # derived path's B-1: a retained-from-earliest multi-edge ladder
            # (e.g. [1→2, 2→3]) with stored=2 would spuriously gap-error on the
            # 1→2 edge. Forward-only semantics are PRESERVED by the slice: edges
            # BELOW `stored` are never replayed (user-curated edges need not be
            # idempotent), only the owed suffix applies. No such multi-edge
            # user_curated ladder ships today — closed pre-emptively per the
            # zero-deferral rule.
            if edges and stored > edges[0].from_version:
                edges = [e for e in edges if e.from_version >= stored]
            if not edges:
                report.errors.append(
                    (
                        artifact_type,
                        artifact_name,
                        f"stored=v{stored} canonical=v{canonical} but no "
                        f"migrations/{artifact_type}/{stored}_to_... shipped "
                        f"[schema_migration_script_missing]",
                    )
                )
                continue

            gap_err = _assert_contiguous(edges, start=stored, end=canonical)
            if gap_err is not None:
                report.errors.append(
                    (
                        artifact_type,
                        artifact_name,
                        f"{gap_err} [schema_migration_script_missing]",
                    )
                )
                continue

            _apply_user_curated_edges(
                artifact_type=artifact_type,
                artifact_name=artifact_name,
                edges=edges,
                derived=derived,
                canonical=canonical,
                env=env,
                weaviate_url=weaviate_url,
                launcher_db=db_path,
                project_id=effective_pid,
                root=root,
                report=report,
                check=check,
                when=when,
            )

    return report


# ---------------------------------------------------------------------------
# Internal helpers used by run_schema_migrations
# ---------------------------------------------------------------------------


def _read_launcher_db_max_migration(db_path: Path) -> Optional[int]:
    """Return the REAL highest applied launcher.db migration
    (``MAX(_schema_migrations.version)``), or ``None`` if it can't be read.

    This is the ground truth for the ``launcher_db_table_set`` version — the
    schema that the RUNNING launcher binary actually applied at startup. B-2:
    the register-only advance for Rust-owned types must compare the Python
    canonical constant against THIS, never phantom-stamp the registry ahead of
    the real DB.

    Returns ``None`` (rather than 0 or raising) when the table is missing or the
    DB is unreadable — the caller treats ``None`` conservatively (skip the
    advance, surface a clear signal) rather than guessing. Mirrors the Rust
    ``SELECT COALESCE(MAX(version), 0) FROM _schema_migrations`` read
    (``migrations.rs`` — must match).
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:  # pragma: no cover - unreachable in tests
        logger.warning(
            "run_schema_migrations: cannot open launcher.db to read "
            "_schema_migrations MAX (%s); treating as unknown",
            exc,
        )
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM _schema_migrations"
        ).fetchone()
    except sqlite3.Error as exc:
        # Table absent (fresh/foreign DB) or unreadable — conservative unknown.
        logger.warning(
            "run_schema_migrations: cannot read _schema_migrations MAX (%s); "
            "treating as unknown",
            exc,
        )
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _read_stored_version(
    db_path: Path,
    *,
    project_id: Optional[str],
    artifact_type: str,
    artifact_name: str,
) -> int:
    """Read the recorded schema_version for an artifact, or canonical-1 if the
    row vanished between check + read (defensive; the caller only reaches here
    for a < canonical status, so a single edge applies)."""
    rows = avr.list_artifacts_for_project(db_path, project_id=project_id)
    for row in rows:
        if row.artifact_type == artifact_type and row.artifact_name == artifact_name:
            return row.schema_version
    # Should not happen (status was RECREATE/UPGRADE_NEEDED → row existed),
    # but fall back to canonical-1 so the single-edge assertion holds.
    return max(0, sv.canonical_version(artifact_type) - 1)


def _build_stale_payload(
    *,
    artifact_type: str,
    artifact_name: str,
    stored: int,
    canonical: int,
    changed_fields: list[str],
) -> dict:
    """Build the ``StaleDerivedArtifact``-shaped dict the launcher modal
    (Piece 4) consumes. SPEC §6.1. ``has_cross_project_shared_nodes`` is left
    ``None`` here — the GUARD-2 probe runs launcher-side before the modal."""
    return {
        "artifact_type": artifact_type,
        "artifact_name": artifact_name,
        "stored_version": stored,
        "canonical_version": canonical,
        "changed_fields": list(changed_fields),
        "regenerate_est_seconds": None,
        "has_cross_project_shared_nodes": None,
    }


def _request_regenerate_or_defer(
    *,
    artifact_type: str,
    artifact_name: str,
    stored: int,
    canonical: int,
    changed_fields: list[str],
    report: MigrationRunReport,
    check: bool,
) -> None:
    """POLICY STEP 3 — record the regenerate-or-defer need. SPEC §2.9.

    The runner NEVER drops here and NEVER advances the recorded version. It
    records a ``pending_regenerate`` payload (the GUI modal surface, Piece 4)
    and an info-level error tagged ``schema_migration_needs_choice`` (the
    headless deferral surface the shim writes). On ``--check`` it records the
    same payload tagged as a preview (``would_prompt`` detail) — still no
    mutation. The next update re-detects + re-prompts until the user chooses.
    """
    payload = _build_stale_payload(
        artifact_type=artifact_type,
        artifact_name=artifact_name,
        stored=stored,
        canonical=canonical,
        changed_fields=changed_fields,
    )
    report.pending_regenerate.append(payload)
    detail = (
        f"derived collection stale (changed: "
        f"{', '.join(changed_fields) or 'schema'}) and no data-preserving "
        f"migration script exists for the edge "
        f"[schema_migration_needs_choice]"
    )
    if check:
        detail = "would_prompt_regenerate_or_defer: " + detail
    report.errors.append((artifact_type, artifact_name, detail))


def _resolve_stale_derived(
    *,
    artifact_type: str,
    artifact_name: str,
    migrations_dir: Path,
    canonical: int,
    stored: int,
    changed_fields: list[str],
    env: Mapping[str, str],
    weaviate_url: str,
    launcher_db: Path,
    project_id: Optional[str],
    root: Path,
    report: MigrationRunReport,
    check: bool,
    when: int,
) -> None:
    """A DERIVED collection is UP_TO_DATE in the registry but STALE live.

    Resolve via the SAME preserving-vs-recreate decision as a real edge
    (SPEC §2.2). No recorded-version bump occurs — the fingerprint is the
    trigger, not the registry. If a preserving edge exists for the artifact's
    canonical step, run it (STEP 2); else surface regenerate-or-defer
    (STEP 3).
    """
    derived = True  # by construction (WEAVIATE_DERIVED_TYPES are all derived)
    edges = discover_edges(migrations_dir, artifact_type)
    if edges:
        # A live-drift recreate consumes the single newest edge that lands at
        # canonical. Verify contiguity to canonical so a partial ladder is
        # treated as missing rather than half-applied.
        gap_err = _assert_contiguous(
            edges, start=edges[0].from_version, end=canonical
        )
        if gap_err is not None:
            _request_regenerate_or_defer(
                artifact_type=artifact_type,
                artifact_name=artifact_name,
                stored=stored,
                canonical=canonical,
                changed_fields=changed_fields,
                report=report,
                check=check,
            )
            return
        _apply_edges_preserving(
            artifact_type=artifact_type,
            artifact_name=artifact_name,
            edges=edges,
            stored=edges[0].from_version,
            canonical=canonical,
            derived=derived,
            env=env,
            weaviate_url=weaviate_url,
            launcher_db=launcher_db,
            project_id=project_id,
            root=root,
            report=report,
            check=check,
            when=when,
            # live-drift: registry already at canonical → don't re-register.
            register_on_success=False,
        )
    else:
        _request_regenerate_or_defer(
            artifact_type=artifact_type,
            artifact_name=artifact_name,
            stored=stored,
            canonical=canonical,
            changed_fields=changed_fields,
            report=report,
            check=check,
        )


def _apply_edges_preserving(
    *,
    artifact_type: str,
    artifact_name: str,
    edges: list[MigrationEdge],
    stored: int,
    canonical: int,
    derived: bool,
    env: Mapping[str, str],
    weaviate_url: str,
    launcher_db: Path,
    project_id: Optional[str],
    root: Path,
    report: MigrationRunReport,
    check: bool,
    when: int,
    register_on_success: bool = True,
) -> None:
    """POLICY STEP 2 — run data-preserving edge(s) for a DERIVED collection.

    The preserving path is the runner's PRIMARY edge mechanism. The
    classification cross-check (§1.4) asserts the edge is non-destructive
    (``@destructive: yes`` rejected). Honors the §2.3 abort posture and
    registers at canonical after each successful edge (unless invoked from the
    live-drift path, where the registry is already current).
    SPEC §2.5.

    v0.2.74 (B-1 fix): ``edges`` is the FULL retained ladder from earliest (e.g.
    ``[4→5, 5→6, 6→7]``), because the earliest edge files must be kept for the
    A2/reconcile replay-from-earliest paths. But a caller may pass a ``stored``
    ABOVE the earliest edge (e.g. a project already at v5/v6 taking the
    RECREATE_NEEDED path). Applying only the edges from ``stored`` forward is the
    correct minimal walk; asserting contiguity on the WHOLE ladder from ``stored``
    would spuriously fail ("expected edge at v{stored} but next is {earliest}").
    So SLICE the ladder to the edges whose ``from_version >= stored`` and validate
    contiguity on that suffix. When ``stored`` equals the earliest, this is the
    whole ladder (unchanged behaviour for the A2/reconcile callers). A ``stored``
    that lands BETWEEN edge boundaries (no edge starts exactly at ``stored``) is a
    real gap and still errors via the sliced contiguity check.
    """
    if edges and stored > edges[0].from_version:
        edges = [e for e in edges if e.from_version >= stored]
        if not edges:
            # Nothing at or above `stored` in the ladder — either already at
            # canonical (no work) or a gap the contiguity check below reports.
            # An empty suffix with stored < canonical is a genuine missing-edge
            # condition; surface it rather than silently no-op.
            if stored < canonical:
                report.errors.append(
                    (
                        artifact_type,
                        artifact_name,
                        f"no migration edge starts at or after v{stored} "
                        f"(canonical v{canonical}) [schema_migration_script_missing]",
                    )
                )
            return
    gap_err = _assert_contiguous(edges, start=stored, end=canonical)
    if gap_err is not None:
        report.errors.append(
            (
                artifact_type,
                artifact_name,
                f"{gap_err} [schema_migration_script_missing]",
            )
        )
        return

    for edge in edges:
        cls_err = _verify_edge_classification(
            edge, derived=derived, preserving_required=True
        )
        if cls_err is not None:
            report.errors.append(
                (artifact_type, artifact_name, f"{cls_err} [schema_migration_classification]")
            )
            return  # fail-closed: abort this artifact, no mutation
        if check:
            report.planned.append((artifact_type, artifact_name, edge.path.name))
            continue
        result = _coerce_edge_result(
            _apply_edge(
                edge,
                project_root=root,
                launcher_db=launcher_db,
                weaviate_url=weaviate_url,
                env=env,
            )
        )
        if not result.ok:
            # ABORT posture (R3): stop at the failed edge, do NOT advance the
            # recorded version, write a failed deferral, retry next update.
            report.errors.append(
                (
                    artifact_type,
                    artifact_name,
                    f"edge {edge.path.name} failed; retry: "
                    f"{_retry_command(edge)} [schema_migration_failed_{edge.path.stem}]",
                )
            )
            return
        # HIGH-2 (v0.2.74) — defense-in-depth against FALSE-ADVANCE. A codegraph
        # edge that printed EDGE_NOOP_NO_PREFIX exited rc=0 but touched NOTHING
        # because it could not resolve a codegraph scope from ITS env (the A1
        # second-order trap). A rc=0 "nothing to patch" is NOT proof the edge
        # did its job — do NOT advance the recorded version. Surface a deferral
        # so the next update (with the prefix now threaded into the edge env by
        # A1) retries it. Idempotent + soft-failing: the collection is left at
        # `stored`, not falsely stamped to canonical.
        if _edge_stdout_says_no_prefix(result.stdout):
            report.errors.append(
                (
                    artifact_type,
                    artifact_name,
                    f"edge {edge.path.name} exited rc=0 but reported "
                    f"EDGE_NOOP_NO_PREFIX (could not resolve codegraph scope "
                    f"from env → touched nothing); NOT advancing version; retry: "
                    f"{_retry_command(edge)} "
                    f"[schema_migration_failed_{edge.path.stem}]",
                )
            )
            return
        # HIGH-2 / H1 (v0.2.74) — require a POSITIVE proof-of-work sentinel for a
        # CODEGRAPH edge, not just the absence of the negative one. A codegraph
        # edge that exits rc=0 having printed NEITHER EDGE_APPLIED nor
        # EDGE_NOOP_NO_PREFIX cannot be trusted to have done its job (a future
        # edge author who forgets to emit the sentinel, a truncated stdout) — so
        # do NOT advance on that ambiguity. Defer + retry. Scoped to
        # `codegraph_collection` because only the codegraph edges emit these
        # sentinels; KG/dev/diagrams edges never print them, so they keep the
        # pre-v0.2.74 rc=0-means-applied contract (no behaviour change for them).
        if (
            artifact_type == "codegraph_collection"
            and not _edge_stdout_says_applied(result.stdout)
        ):
            report.errors.append(
                (
                    artifact_type,
                    artifact_name,
                    f"edge {edge.path.name} exited rc=0 but printed NO "
                    f"EDGE_APPLIED sentinel (cannot confirm it did real work); "
                    f"NOT advancing version; retry: {_retry_command(edge)} "
                    f"[schema_migration_failed_{edge.path.stem}]",
                )
            )
            return
        report.applied.append((artifact_type, artifact_name, edge.path.name))
        # Register ONLY on the FINAL edge (edge.to_version == canonical). A
        # multi-edge ladder (4→5→6→7) must NOT register the intermediate
        # versions 5/6 — ``register_artifact_version`` asserts the written
        # version equals canonical (it refuses to record a non-canonical
        # version), and a mid-ladder registry write has no independent meaning:
        # the edges are idempotent-from-earliest, so an abort mid-ladder leaves
        # the registry at ``stored`` and the WHOLE ladder replays next update
        # (safe — re-running an applied add-property/delete edge is a no-op).
        if register_on_success and edge.to_version == canonical:
            avr.register_artifact_version(
                db_path=launcher_db,
                project_id=project_id,
                artifact_type=artifact_type,
                artifact_name=artifact_name,
                schema_version=canonical,  # only ever the canonical version
                materialized_at=when,
            )


def _apply_user_curated_edges(
    *,
    artifact_type: str,
    artifact_name: str,
    edges: list[MigrationEdge],
    derived: bool,
    canonical: int,
    env: Mapping[str, str],
    weaviate_url: str,
    launcher_db: Path,
    project_id: Optional[str],
    root: Path,
    report: MigrationRunReport,
    check: bool,
    when: int,
) -> None:
    """SPEC §2.7 forward-only upgrade for user_curated artifacts.

    Ascending edges; first edge's ``from`` == current stored (contiguity
    asserted by the caller). Same §2.3 abort posture as the preserving path.
    """
    for edge in edges:
        cls_err = _verify_edge_classification(
            edge, derived=derived, preserving_required=False
        )
        if cls_err is not None:
            report.errors.append(
                (artifact_type, artifact_name, f"{cls_err} [schema_migration_classification]")
            )
            return
        if check:
            report.planned.append((artifact_type, artifact_name, edge.path.name))
            continue
        result = _coerce_edge_result(
            _apply_edge(
                edge,
                project_root=root,
                launcher_db=launcher_db,
                weaviate_url=weaviate_url,
                env=env,
            )
        )
        if not result.ok:
            report.errors.append(
                (
                    artifact_type,
                    artifact_name,
                    f"edge {edge.path.name} failed; retry: "
                    f"{_retry_command(edge)} [schema_migration_failed_{edge.path.stem}]",
                )
            )
            return
        report.applied.append((artifact_type, artifact_name, edge.path.name))
        # Register ONLY on the FINAL (canonical) edge — see the same rationale
        # in _apply_edges_preserving: register_artifact_version refuses a
        # non-canonical version, and the forward-only ladder replays from
        # `stored` on the next update after a mid-ladder abort.
        if edge.to_version == canonical:
            avr.register_artifact_version(
                db_path=launcher_db,
                project_id=project_id,
                artifact_type=artifact_type,
                artifact_name=artifact_name,
                schema_version=canonical,
                materialized_at=when,
            )


# ---------------------------------------------------------------------------
# Report → DeferralEntry translation (shared by install.py shim + CLI)
# ---------------------------------------------------------------------------


def build_deferral_entries(report: MigrationRunReport) -> list:
    """Translate a :class:`MigrationRunReport` into ``DeferralEntry`` rows.

    SINGLE SOURCE OF TRUTH for the report→deferral mapping, shared by:
      * ``install.py:_run_schema_migration_scripts`` (ROOT update shim), via
        ``_translate_migration_report_to_deferrals`` which adds these to its
        run-scoped ``DeferralReport``; and
      * ``vco_lib.project_init._cmd_migrate_schema`` (per-project CLI surface),
        which reads the project's existing ``UPDATE_DEFERRED.md``, adds these
        entries, and writes it back — so a stale-derived per-project run
        actually PERSISTS the ``schema_regenerate_or_defer_<slug>`` finding for
        a future Claude session (CONCERN-1, 2026-06-16). The runner itself
        never writes; this builder is the only place the mapping lives.

    Maps each non-clean outcome to the EXISTING deferral condition_id family
    (SPEC §6.3 step 2):
      * ``refused``            → ``schema_migration_refuse_downgrade``
      * ``pending_regenerate`` → ``schema_regenerate_or_defer_<slug>`` (one per
        artifact so multiple stale collections don't collide)
      * ``errors``             → the trailing ``[condition_id]`` tag the runner
        embedded (``schema_migration_failed_*`` / ``_script_missing`` /
        ``_classification``); the ``schema_migration_needs_choice`` error rows
        are skipped (already covered by ``pending_regenerate``).

    Returns a list of ``DeferralEntry`` (lazy import so the runner module has
    no hard dependency on deferral_report's import surface).
    """
    from .deferral_report import DeferralEntry

    entries: list = []

    # REFUSE_DOWNGRADE — launcher.db newer than this orchestrator.
    for artifact_type, artifact_name, detail in report.refused:
        entries.append(
            DeferralEntry(
                condition_id="schema_migration_refuse_downgrade",
                title="Schema downgrade refused",
                detected=(
                    f"Artifact `{artifact_type}` (`{artifact_name}`) {detail}. "
                    f"The launcher DB was written by a newer orchestrator."
                ),
                why_deferred=(
                    "Refusing to mangle a newer-than-expected schema. Upgrade "
                    "the orchestrator to the version that wrote this state "
                    "instead of downgrading."
                ),
                command_to_apply=(
                    "# Re-run the orchestrator update to the matching version, "
                    "then:\npython install.py --update"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )

    # POLICY STEP 3 — stale derived + no preserving script → regenerate-or-defer
    # (headless surface). One deferral per artifact so they don't collide.
    for payload in report.pending_regenerate:
        artifact_type = payload.get("artifact_type", "")
        artifact_name = payload.get("artifact_name", "")
        changed = ", ".join(payload.get("changed_fields") or []) or "schema"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", artifact_name).strip("_") or artifact_type
        entries.append(
            DeferralEntry(
                condition_id=f"schema_regenerate_or_defer_{slug}",
                title=f"Derived collection stale: {artifact_name}",
                detected=(
                    f"`{artifact_type}` (`{artifact_name}`) is stale (changed: "
                    f"{changed}) and NO data-preserving migration script exists "
                    f"for the version edge. The runner did NOT drop it."
                ),
                why_deferred=(
                    "Recreating a derived collection drops + re-embeds it from "
                    "disk (`knowledge/**`). That is the LAST resort and needs "
                    "your explicit choice — in the launcher this is the "
                    "'Regenerate now' / 'Defer to Claude' modal; headless it is "
                    "deferred here so nothing destructive happens unattended."
                ),
                command_to_apply=(
                    "# Regenerate now (drop + recreate + re-sync from disk; "
                    "the guarded body still enforces its data-safety guards):\n"
                    f"python -m vco_lib.project_init migrate-schema --folder . "
                    f"--regenerate {artifact_type} --artifact-name {artifact_name}\n"
                    "# Or leave deferred — a future Claude session handles it."
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )

    # Edge failures / missing-script / classification mismatch. The runner
    # tags each detail with a trailing ``[condition_id]``.
    for artifact_type, artifact_name, detail in report.errors:
        m = re.search(r"\[([a-z0-9_]+)\]\s*$", detail)
        condition_id = m.group(1) if m else "schema_migration_failed"
        clean_detail = re.sub(r"\s*\[[a-z0-9_]+\]\s*$", "", detail).strip()
        # The needs_choice case is already covered by pending_regenerate above;
        # skip the duplicate error row so the deferral file stays clean.
        if condition_id == "schema_migration_needs_choice":
            continue
        entries.append(
            DeferralEntry(
                condition_id=condition_id,
                title=f"Schema migration issue: {artifact_type}",
                detected=(
                    f"`{artifact_type}` (`{artifact_name}`): {clean_detail}."
                ),
                why_deferred=(
                    "The migration edge could not be applied (or is missing / "
                    "mis-declared). The recorded schema version was NOT "
                    "advanced; the next `install.py --update` re-attempts it."
                ),
                command_to_apply=(
                    "python -m vco_lib.project_init migrate-schema --folder . "
                    "--check   # inspect, then re-run install.py --update"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )

    return entries
