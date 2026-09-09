# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The PATH-BEARING REGISTRY — what carries a project's folder path, and what
each carrier's move POLICY is.

WHY THIS FILE EXISTS
--------------------
A project's folder path is written into more places than any one editor keeps
in their head. When a registered project was moved by hand (2026-08-29 field
run), the ``projects.folder_path`` flip was done correctly and the project was
STILL broken: ``project_agents.file_path`` (44 rows) and
``project_skills.file_path`` (53 rows) still pointed at the old root, and
nothing noticed. A grep-and-hope rewrite is not a fix for that class of bug —
the next column to grow a path would be missed the same way.

So the rewrite is REGISTRY-DRIVEN, in two tables:

``PATH_BEARING_ENV_KEYS``
    Env keys whose VALUES embed the project folder path. Enforced by
    :func:`unregistered_path_bearing_env_keys` against a real rendered
    projection, so a new key that embeds the folder fails a test at the PR
    that adds it — not at the next field move.

``PATH_BEARING_DB_COLUMNS``
    EVERY ``TEXT`` column in ``launcher.db``, each with a declared move
    POLICY. Enforced by :func:`unclassified_columns` against the schema the
    migrations actually produce, so a new column cannot ship unclassified.

THE POLICIES
------------
``flip``
    The column IS the project root. Exactly one: ``projects.folder_path``.
    Written by the sanctioned DB writer, once, atomically.

``re-derive`` / ``targeted-update``
    A value VCO owns and can recompute from the NEW root through the SAME
    path oracle that produced it. NEVER a string ``REPLACE`` — a text
    substitution on a path is how you turn ``/a/proj`` into ``/b/projects``
    when the old root is a prefix of another string in the value.

``user-owned-flag``
    A path the USER chose (an extra code-graph root, a secret file). It may
    deliberately point at the old location — a cloned repo the user still
    wants indexed. VCO must NOT rewrite it; it emits a per-path deferral and
    leaves the row byte-identical.

``historical``
    Telemetry / log / audit text that was TRUE AT EVENT TIME. Rewriting it
    would falsify a record. The sweep reports these as ``historical
    (expected)`` — never as a warning.

``sweep-only``
    Could carry the root, is not VCO's to silently rewrite, and has no
    fixer. Surfaced with the command that DOES re-derive it (e.g. the
    diagram index rebuild), never auto-edited.

``not-path-bearing``
    Cannot carry a project root — ids, slugs, enums, model names, hashes.
    A sweep HIT here means this registry is wrong; that is the loud signal
    ``project_move_stale_db_path`` exists to raise.

Note the deliberate asymmetry with "is a path": ``module_installs.install_path``
IS an absolute path, but ``vct_launcher_core::manifest::validate_install_dir``
refuses any value outside ``~/.vct/modules/`` — so it cannot carry a PROJECT
root and is classified ``not-path-bearing`` with that reason recorded.

WHO READS THIS
--------------
* :mod:`vco_lib.project_move` — the sweep (read-only, over ALL columns) and
  the per-policy disposition of each hit.
* ``vct-launcher-core``'s ``Db::repoint_project_paths`` — the fixer for the
  ``targeted-update`` columns. It does not PARSE this file (a Rust TOML read
  per move would buy nothing); instead
  ``tests/test_v0292_wp17_path_bearing_registry.py`` source-scans the Rust
  writer and fails if a ``targeted-update`` column has no fixer there. One
  home for the CLASSIFICATION, one home for the FIX, a test locking them.

Pure + dependency-free: no I/O, no DB, no vco_lib imports. Import it from
anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "POLICY_FLIP",
    "POLICY_RE_DERIVE",
    "POLICY_TARGETED_UPDATE",
    "POLICY_USER_OWNED_FLAG",
    "POLICY_HISTORICAL",
    "POLICY_SWEEP_ONLY",
    "POLICY_NOT_PATH_BEARING",
    "POLICIES",
    "ACTIONABLE_POLICIES",
    "ColumnPolicy",
    "PATH_BEARING_ENV_KEYS",
    "PROJECTION_OWNED_SCRUB_KEYS",
    "PATH_BEARING_DB_COLUMNS",
    "policy_for",
    "columns_with_policy",
    "unclassified_columns",
    "unregistered_path_bearing_env_keys",
]


# ───────────────────────────────────────────────────────────────────────────
# Policies
# ───────────────────────────────────────────────────────────────────────────

POLICY_FLIP = "flip"
POLICY_RE_DERIVE = "re-derive"
POLICY_TARGETED_UPDATE = "targeted-update"
POLICY_USER_OWNED_FLAG = "user-owned-flag"
POLICY_HISTORICAL = "historical"
POLICY_SWEEP_ONLY = "sweep-only"
POLICY_NOT_PATH_BEARING = "not-path-bearing"

#: Every legal policy value. A row with anything else is a registry error.
POLICIES: frozenset[str] = frozenset(
    {
        POLICY_FLIP,
        POLICY_RE_DERIVE,
        POLICY_TARGETED_UPDATE,
        POLICY_USER_OWNED_FLAG,
        POLICY_HISTORICAL,
        POLICY_SWEEP_ONLY,
        POLICY_NOT_PATH_BEARING,
    }
)

#: Policies whose sweep hit means WORK IS OWED (a deferral, or a fixer that
#: did not do its job). ``historical`` hits are expected and silent-by-design;
#: ``flip`` is the column the move just wrote.
ACTIONABLE_POLICIES: frozenset[str] = frozenset(
    {
        POLICY_RE_DERIVE,
        POLICY_TARGETED_UPDATE,
        POLICY_USER_OWNED_FLAG,
        POLICY_SWEEP_ONLY,
        POLICY_NOT_PATH_BEARING,
    }
)


# ───────────────────────────────────────────────────────────────────────────
# Table 1 — env keys whose VALUES embed the project folder path
# ───────────────────────────────────────────────────────────────────────────

#: Keys the canonical env projection emits whose value CONTAINS the project
#: folder path. After a move these must be re-projected from the NEW row —
#: which ``vco_lib.config_projection.apply_project_env`` does by construction,
#: because it derives every value from DB state rather than editing text.
#:
#: The registry's job is not to drive that rewrite (the projection needs no
#: list); it is to make a NEW folder-embedding key impossible to add silently.
#: :func:`unregistered_path_bearing_env_keys` renders a projection for a
#: fixture project and reports any emitted value containing the folder path
#: whose key is not here.
#: Membership is "this key's VALUE can contain a project folder path",
#: independent of WHO writes it — the gate's question is whether a
#: folder-embedding value is accounted for, not which writer emitted it.
PATH_BEARING_ENV_KEYS: frozenset[str] = frozenset(
    {
        # THE projected one: `config_projection._CANONICAL_KEYS` includes
        # KG_BASE_DIR, resolved as `str(proj.folder_path)`. It is the only
        # canonical key whose value is the folder today — which is exactly
        # why a drift gate is needed rather than a comment.
        "KG_BASE_DIR",
        # NOT projected. Set by the launcher and by the kg-sync wrappers
        # (v0.2.89 BUG 3's non-leaking channel), never exported by a Claude
        # session — that non-leaking property is what it is FOR.
        "KG_SYNC_PROJECT_ROOT",
        # NOT projected. Set by the Claude Code harness for the session's own
        # project. Registered because a spawned child that inherits the
        # OPERATOR's value would resolve the operator's tree, which is the
        # B-as-run #3 failure in another costume.
        "CLAUDE_PROJECT_DIR",
    }
)

#: Keys a spawned child MUST NOT inherit from the operator's shell (B-as-run
#: #3: a foreign ``KG_COLLECTION`` from the operator's own project leaked into
#: the move's first sync). Scrubbed from ``os.environ`` before the TARGET
#: project's resolved config is overlaid.
#:
#: This is the PROJECTION-OWNED set: every key the canonical projection writes
#: for a project, so any of them present in the ambient env belongs to SOME
#: project — possibly not this one. It is deliberately a superset of
#: :data:`PATH_BEARING_ENV_KEYS`.
#:
#: One home caveat: the canonical list also exists Rust-side as
#: ``CANONICAL_INSTALL_ENV_KEYS`` (``projects_v2.rs``). This tuple is derived
#: from ``vco_lib.config_projection`` at run time when that module exposes the
#: names (see :func:`vco_lib.project_move.projection_owned_keys`); the literals
#: here are the FLOOR used when it does not, never a second source of truth.
PROJECTION_OWNED_SCRUB_KEYS: frozenset[str] = frozenset(
    {
        "KG_COLLECTION",
        "SHARED_KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
        "DIAGRAMS_COLLECTION",
        "PROJECT_NAME",
        "CODE_GRAPH_PROJECT",
        "KG_BASE_DIR",
        "KG_SYNC_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "VCT_PROJECT_ID",
        "SHARED_KG_READ_DISABLED",
        "SHARED_KG_WRITE_DISABLED",
        "VCT_KG_ACCESS_LIST",
        "VCT_CODE_GRAPH_ACCESS_LIST",
    }
)


# ───────────────────────────────────────────────────────────────────────────
# Table 2 — every launcher.db TEXT column, classified
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColumnPolicy:
    """One classified ``launcher.db`` TEXT column."""

    table: str
    column: str
    policy: str
    note: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


def _plain(table: str, *columns: str, note: str = "") -> tuple[ColumnPolicy, ...]:
    """Shorthand for a run of ``not-path-bearing`` columns on one table."""
    return tuple(
        ColumnPolicy(table, c, POLICY_NOT_PATH_BEARING, note) for c in columns
    )


#: Every TEXT column in ``launcher.db``, with its move policy.
#:
#: Ordered by table name to match ``PRAGMA table_info`` walk order, which is
#: what the coverage test compares against.
PATH_BEARING_DB_COLUMNS: tuple[ColumnPolicy, ...] = (
    # ── app_state ────────────────────────────────────────────────────────
    *_plain("app_state", "key"),
    ColumnPolicy(
        "app_state",
        "value",
        POLICY_SWEEP_ONLY,
        "Free-form machine-global values (launcher.install_path, embedding "
        "profile, …). Orchestrator-scoped, not project-scoped — but the "
        "column is untyped, so a project root landing here is possible and "
        "must be surfaced rather than assumed away.",
    ),
    # ── artifact_schema_versions ─────────────────────────────────────────
    *_plain("artifact_schema_versions", "project_id", "artifact_type", "artifact_name"),
    # ── audit_log ────────────────────────────────────────────────────────
    *_plain("audit_log", "operation", "project_id", "module_id", "actor"),
    ColumnPolicy(
        "audit_log",
        "detail",
        POLICY_HISTORICAL,
        "The audit trail. `project_create`'s detail and this move's own "
        "`project_path_change` detail BOTH name paths that were true at "
        "event time. Rewriting an audit row is falsifying it.",
    ),
    # ── chat_model_context (migration 043) ───────────────────────────────
    *_plain(
        "chat_model_context",
        "model_id",
        "vendor",
        "source",
        "source_note",
        "updated_at",
        note="Orchestrator-wide model metadata; no project scope at all.",
    ),
    # ── chat_model_context_tombstone (migration 045, v0.2.94) ─────────────
    *_plain(
        "chat_model_context_tombstone",
        "model_id",
        "deleted_at",
        note="A row the user deleted from the orchestrator-wide model table, "
        "with the event time; no project scope, no path.",
    ),
    # ── code_graph_builds ────────────────────────────────────────────────
    *_plain("code_graph_builds", "project_id", "status", "languages"),
    ColumnPolicy(
        "code_graph_builds",
        "error_message",
        POLICY_HISTORICAL,
        "A failure message from a build that ran against the OLD root.",
    ),
    ColumnPolicy(
        "code_graph_builds",
        "log_tail",
        POLICY_HISTORICAL,
        "Captured analyzer output; full of old-root paths BY CONSTRUCTION. "
        "Confirmed carrying old-root text by the field sweep — correctly left.",
    ),
    # ── codegraph_access ─────────────────────────────────────────────────
    *_plain(
        "codegraph_access", "grantor_project_id", "grantee_project_id", "access_level"
    ),
    # ── deprecation_events ───────────────────────────────────────────────
    *_plain("deprecation_events", "project_id", "module_id", "eol_date", "migration_url"),
    ColumnPolicy(
        "deprecation_events",
        "message",
        POLICY_HISTORICAL,
        "Upstream-authored notice text captured at event time.",
    ),
    # ── diagram_access ───────────────────────────────────────────────────
    *_plain(
        "diagram_access", "grantor_project_id", "grantee_project_id", "access_level"
    ),
    # ── diagram_index_retry ──────────────────────────────────────────────
    *_plain("diagram_index_retry", "project_id"),
    ColumnPolicy(
        "diagram_index_retry",
        "file_path",
        POLICY_SWEEP_ONLY,
        "A queued retry for a diagram that failed to index. Re-derived by "
        "`vco rebuild-diagram-index`; the move surfaces that command rather "
        "than rewriting a queue VCO does not own the semantics of.",
    ),
    ColumnPolicy(
        "diagram_index_retry",
        "error",
        POLICY_HISTORICAL,
        "The indexing error as it was raised.",
    ),
    # ── diagram_snapshots ────────────────────────────────────────────────
    *_plain("diagram_snapshots", "content_hash", "trigger", "label"),
    # ── kg_collection_access ─────────────────────────────────────────────
    *_plain("kg_collection_access", "project_id", "collection_name", "access_level"),
    # ── kg_summaries ─────────────────────────────────────────────────────
    *_plain("kg_summaries", "project_id", "status", "backend"),
    ColumnPolicy("kg_summaries", "error_message", POLICY_HISTORICAL),
    ColumnPolicy(
        "kg_summaries",
        "log_tail",
        POLICY_HISTORICAL,
        "Field-confirmed carrier of old-root text; correctly left.",
    ),
    # ── kg_syncs ─────────────────────────────────────────────────────────
    *_plain("kg_syncs", "project_id", "status"),
    ColumnPolicy("kg_syncs", "error_message", POLICY_HISTORICAL),
    ColumnPolicy(
        "kg_syncs",
        "log_tail",
        POLICY_HISTORICAL,
        "Field-confirmed carrier of old-root text; correctly left.",
    ),
    # ── license_key_validations ──────────────────────────────────────────
    *_plain("license_key_validations", "module_id", "tier"),
    ColumnPolicy("license_key_validations", "error_message", POLICY_HISTORICAL),
    # ── license_keys ─────────────────────────────────────────────────────
    *_plain(
        "license_keys", "module_id", "key_prefix", "keychain_username", "tier"
    ),
    ColumnPolicy("license_keys", "last_validation_error", POLICY_HISTORICAL),
    # ── module_access_tokens ─────────────────────────────────────────────
    *_plain("module_access_tokens", "module_id", "project_id", "token_secret"),
    # ── module_db_migrations ─────────────────────────────────────────────
    *_plain(
        "module_db_migrations", "module_id", "filename", "sha256", "namespace",
        note="`filename` is a migration basename inside the module package.",
    ),
    # ── module_deprecation_seen ──────────────────────────────────────────
    *_plain("module_deprecation_seen", "project_id", "module_id"),
    # ── module_installs ──────────────────────────────────────────────────
    *_plain(
        "module_installs",
        "id",
        "project_id",
        "module_id",
        "module_version",
        "status",
        "container_name",
        "kg_collections",
    ),
    ColumnPolicy(
        "module_installs",
        "install_path",
        POLICY_NOT_PATH_BEARING,
        "IS an absolute path, but `validate_install_dir` "
        "(vct-launcher-core/src/manifest.rs) refuses any value outside "
        "`~/.vct/modules/`. It therefore cannot carry a PROJECT root — a "
        "sweep hit here is a registry/validator contradiction worth raising.",
    ),
    ColumnPolicy("module_installs", "last_error", POLICY_HISTORICAL),
    # ── module_mcp_tool_defaults ─────────────────────────────────────────
    *_plain(
        "module_mcp_tool_defaults", "mcp_name", "tool_name", "description", "module_id"
    ),
    # ── module_ports ─────────────────────────────────────────────────────
    *_plain("module_ports", "project_id", "module_id"),
    # ── module_settings ──────────────────────────────────────────────────
    *_plain("module_settings", "project_id", "module_id", "setting_key"),
    ColumnPolicy(
        "module_settings",
        "setting_value",
        POLICY_USER_OWNED_FLAG,
        "Arbitrary user-authored module configuration. A module setting that "
        "names a path under the old root is the USER's choice — surfaced "
        "per-row, never rewritten.",
    ),
    # ── project_agents ───────────────────────────────────────────────────
    *_plain(
        "project_agents",
        "project_id",
        "agent_name",
        "source",
        "source_module",
        "model",
        "config_json",
    ),
    ColumnPolicy(
        "project_agents",
        "file_path",
        POLICY_TARGETED_UPDATE,
        "ABSOLUTE path to the agent's .md. Recomputed from the new root via "
        "`resolve_kind_paths` — the SAME oracle `set_enabled_with_fs_move` "
        "uses — for enabled AND disabled rows. Disabled rows are the reason "
        "this is targeted-update rather than a populate re-derive: populate "
        "scans only the enabled dirs (project_state_populate.rs), so it can "
        "NEVER reach an `enabled=0` row's path. 44 rows of this column were "
        "the field move's most visible breakage.",
    ),
    # ── project_codegraph_bindings ───────────────────────────────────────
    *_plain(
        "project_codegraph_bindings",
        "project_id",
        "collection_prefix",
        "embedding_model",
        "last_analyzed_commit",
        "config_json",
        note="`collection_prefix` is project IDENTITY — a move must never "
        "re-derive it from the new folder basename (that is exactly the "
        "defect this feature exists to avoid reproducing).",
    ),
    # ── project_codegraph_extra_paths ────────────────────────────────────
    *_plain("project_codegraph_extra_paths", "project_id", "label", "last_indexed_commit"),
    ColumnPolicy(
        "project_codegraph_extra_paths",
        "path",
        POLICY_USER_OWNED_FLAG,
        "A path the USER designated for their project's code graph — often "
        "a clone that is NOT moving. Rewriting it would silently re-point "
        "the user's index at a tree they did not choose. Per-path deferral; "
        "row byte-identical.",
    ),
    # ── project_diagrams ─────────────────────────────────────────────────
    *_plain(
        "project_diagrams",
        "project_id",
        "diagram_name",
        "diagram_type",
        "inferred_title",
        "diagram_kind",
        "chat_id",
        "linked_session_summary",
        "config_json",
    ),
    ColumnPolicy(
        "project_diagrams",
        "file_path",
        POLICY_SWEEP_ONLY,
        "MAY be relative or absolute (`diagrams_cmd.rs::resolve_diagram_path` "
        "branches on `is_absolute`). Relative rows follow the move for free; "
        "absolute rows are re-derived by `vco rebuild-diagram-index`, which "
        "the deferral names.",
    ),
    ColumnPolicy(
        "project_diagrams",
        "category_path",
        POLICY_SWEEP_ONLY,
        "A logical category, not a filesystem path — but free text, so a "
        "hit is surfaced with the same rebuild command.",
    ),
    ColumnPolicy(
        "project_diagrams",
        "content_text",
        POLICY_USER_OWNED_FLAG,
        "The diagram's own authored content. User data — never edited.",
    ),
    # ── project_hooks ────────────────────────────────────────────────────
    *_plain(
        "project_hooks",
        "project_id",
        "event",
        "matcher",
        "source",
        "source_module",
        "config_json",
    ),
    ColumnPolicy(
        "project_hooks",
        "command",
        POLICY_SWEEP_ONLY,
        "Hook commands are `$CLAUDE_PROJECT_DIR`-relative by convention, so "
        "they follow a move for free — the reason ProjectHook has no "
        "file_path column at all. A hook whose command hard-codes an "
        "absolute root is a user edit: surfaced, not rewritten.",
    ),
    ColumnPolicy(
        "project_hooks",
        "disabled_entry_json",
        POLICY_SWEEP_ONLY,
        "The verbatim settings.json entry VCO parked when the hook was "
        "disabled (migration 042). Rewriting it would change what "
        "re-enabling restores; it must round-trip byte-exact.",
    ),
    # ── project_kg_bindings ──────────────────────────────────────────────
    *_plain(
        "project_kg_bindings",
        "project_id",
        "role",
        "collection_name",
        "embedding_model",
        "weaviate_url",
        "config_json",
        note="`collection_name` is project IDENTITY — never re-derived from "
        "the new folder basename.",
    ),
    ColumnPolicy(
        "project_kg_bindings",
        "kg_dir_path",
        POLICY_TARGETED_UPDATE,
        "NULL unless the GUI/hub set it, and `populate_kg_bindings` skips "
        "rows that already exist — so nothing else re-derives it. Rebased by "
        "`db::bindings_writer::repoint_kg_dir_path`, called from inside the "
        "move's commit transaction so it commits or rolls back WITH the flip. "
        "The fixer lives in bindings_writer.rs because the binding tables "
        "have a SINGLE-WRITER gate (tests/test_kg_binding_single_writer_rust"
        ".py) allowing their SQL only in "
        "db/{bindings_writer,project_state,access,migrations}.rs — v0.2.92 W3 "
        "classified this `sweep-only` rather than evade that gate, and W14 "
        "closed it in the sanctioned home. The rebase is component-wise (so "
        "`/old/proj` never captures `/old/project`) and case-insensitive on "
        "Windows/macOS only; a value NOT under the old root is left exactly "
        "as it is, because it is either already correct or a deliberate user "
        "pointer outside the project.",
    ),
    # ── project_mcp_servers ──────────────────────────────────────────────
    *_plain(
        "project_mcp_servers",
        "project_id",
        "mcp_name",
        "source",
        "source_module",
        "config_json",
    ),
    ColumnPolicy(
        "project_mcp_servers",
        "source_file",
        POLICY_SWEEP_ONLY,
        "Where the MCP registration was read from — may be the project's own "
        "`.mcp.json` (absolute). Re-derived by the launcher's own MCP scan; "
        "surfaced here rather than hand-edited.",
    ),
    ColumnPolicy(
        "project_mcp_servers",
        "command",
        POLICY_SWEEP_ONLY,
        "A third-party MCP's launch command may embed an absolute path the "
        "USER wrote. Never rewritten (v0.2.83 third-party-MCP preservation).",
    ),
    # ── project_mcp_tool_grants ──────────────────────────────────────────
    *_plain("project_mcp_tool_grants", "project_id", "mcp_name", "tool_name"),
    # ── project_modules ──────────────────────────────────────────────────
    *_plain("project_modules", "project_id", "module_name"),
    # ── project_moves (migration 044) ────────────────────────────────────
    *_plain("project_moves", "id", "project_id", "status"),
    ColumnPolicy(
        "project_moves",
        "src",
        POLICY_HISTORICAL,
        "Where the project lived BEFORE this move. It is the record; a sweep "
        "for the old path is SUPPOSED to find it here, and rewriting it would "
        "erase the only durable answer to 'where did this project come from?'. "
        "This row is also what the retention deferral points at.",
    ),
    ColumnPolicy(
        "project_moves",
        "dst",
        POLICY_HISTORICAL,
        "Where this move sent the project. Historical for the same reason as "
        "`src`: a LATER move's sweep will find this one's `dst` as an old "
        "path, and that is correct.",
    ),
    ColumnPolicy(
        "project_moves",
        "error",
        POLICY_HISTORICAL,
        "Why a move failed, as it failed.",
    ),
    # ── project_permissions ──────────────────────────────────────────────
    *_plain("project_permissions", "project_id", "subject", "kind", "config_json"),
    ColumnPolicy(
        "project_permissions",
        "value",
        POLICY_USER_OWNED_FLAG,
        "A permission rule the user granted, e.g. `Read(/abs/path/**)`. "
        "Rewriting a permission is widening or narrowing a security "
        "decision the user made. Surfaced only.",
    ),
    # ── project_secret_refs ──────────────────────────────────────────────
    *_plain(
        "project_secret_refs",
        "project_id",
        "secret_key",
        "resolution",
        "env_name",
        "source_module",
        "required_for",
        "description",
    ),
    ColumnPolicy(
        "project_secret_refs",
        "file_path",
        POLICY_USER_OWNED_FLAG,
        "Where a secret RESOLVES FROM — a user-configured location that is "
        "frequently outside the project tree on purpose. Never rewritten.",
    ),
    # ── project_setups ───────────────────────────────────────────────────
    *_plain("project_setups", "project_id", "status", "phase"),
    ColumnPolicy("project_setups", "warnings", POLICY_HISTORICAL),
    ColumnPolicy("project_setups", "error_message", POLICY_HISTORICAL),
    ColumnPolicy(
        "project_setups",
        "log_tail",
        POLICY_HISTORICAL,
        "Setup output captured against whatever root was live then.",
    ),
    # ── project_skills ───────────────────────────────────────────────────
    *_plain(
        "project_skills",
        "project_id",
        "skill_name",
        "source",
        "source_module",
        "model",
        "config_json",
    ),
    ColumnPolicy(
        "project_skills",
        "file_path",
        POLICY_TARGETED_UPDATE,
        "The skill DIRECTORY's absolute path. Same oracle, same "
        "disabled-row reasoning as project_agents.file_path. 53 rows of "
        "this column survived the field move pointing at the old root.",
    ),
    # ── projects ─────────────────────────────────────────────────────────
    *_plain("projects", "id", "name", "host", "slug"),
    ColumnPolicy(
        "projects",
        "folder_path",
        POLICY_FLIP,
        "THE column. UNIQUE-constrained, which is what makes a collision "
        "with an already-registered path a race-proof refusal rather than a "
        "check-then-act.",
    ),
    # ── rl_events ────────────────────────────────────────────────────────
    *_plain(
        "rl_events",
        "event_type",
        "project_id",
        "project_name",
        "task_id",
        "task_type",
        "embedding_source",
        "embedding_model",
        "quarantine_reason",
    ),
    ColumnPolicy(
        "rl_events",
        "payload_json",
        POLICY_HISTORICAL,
        "Retrieval telemetry. Its paths are the observation. Field sweep "
        "confirmed old-root text here — correctly left; rewriting it would "
        "corrupt the training corpus.",
    ),
    # ── secret_active_state ──────────────────────────────────────────────
    *_plain(
        "secret_active_state",
        "scope",
        "project_id",
        "module_id",
        "key",
        "requester_project_id",
    ),
    # ── secret_grants ────────────────────────────────────────────────────
    *_plain(
        "secret_grants",
        "scope",
        "owner_project_id",
        "module_id",
        "key",
        "grantee_project_id",
        "granted_by_actor",
    ),
    ColumnPolicy(
        "secret_grants",
        "note",
        POLICY_HISTORICAL,
        "A free-text justification written at grant time.",
    ),
    # ── tier_cache ───────────────────────────────────────────────────────
    *_plain("tier_cache", "orchestrator_tier", "module_licenses"),
    ColumnPolicy("tier_cache", "last_error", POLICY_HISTORICAL),
)


#: Tables the sweep does not walk: SQLite's own bookkeeping.
INTERNAL_TABLES: frozenset[str] = frozenset({"sqlite_sequence", "_schema_migrations"})


_BY_QUALIFIED: dict[str, ColumnPolicy] = {
    c.qualified: c for c in PATH_BEARING_DB_COLUMNS
}


def policy_for(table: str, column: str) -> str | None:
    """The declared policy for ``table.column``, or ``None`` if unclassified.

    ``None`` is the signal that the registry is incomplete — callers raise it
    loudly (``project_move_stale_db_path``) rather than guessing a policy.
    """
    entry = _BY_QUALIFIED.get(f"{table}.{column}")
    return entry.policy if entry is not None else None


def columns_with_policy(*policies: str) -> tuple[ColumnPolicy, ...]:
    """Every registered column whose policy is one of ``policies``."""
    wanted = frozenset(policies)
    return tuple(c for c in PATH_BEARING_DB_COLUMNS if c.policy in wanted)


def unclassified_columns(
    schema: dict[str, list[str]],
) -> tuple[str, ...]:
    """Qualified names present in ``schema`` but absent from the registry.

    ``schema`` maps ``table -> [text column names]`` — what
    ``PRAGMA table_info`` reports after every migration has been applied.
    :data:`INTERNAL_TABLES` are ignored.

    The coverage test calls this. A non-empty result means a migration added a
    TEXT column and nobody decided whether a move must rewrite it.
    """
    missing: list[str] = []
    for table, columns in schema.items():
        if table in INTERNAL_TABLES:
            continue
        for column in columns:
            if f"{table}.{column}" not in _BY_QUALIFIED:
                missing.append(f"{table}.{column}")
    return tuple(sorted(missing))


def stale_registry_entries(
    schema: dict[str, list[str]],
) -> tuple[str, ...]:
    """Registry rows naming a ``table.column`` the schema no longer has.

    The reverse direction of :func:`unclassified_columns`: a dropped column
    leaves a rule that can never fire, and a rule that can never fire is a
    promise (R16 category 4). Also catches a typo'd column name at the PR that
    introduces it — otherwise the sweep would silently skip that column
    forever.
    """
    live = {
        f"{table}.{column}"
        for table, columns in schema.items()
        if table not in INTERNAL_TABLES
        for column in columns
    }
    return tuple(sorted(q for q in _BY_QUALIFIED if q not in live))


def unregistered_path_bearing_env_keys(
    projected: dict[str, str], folder_path: str
) -> tuple[str, ...]:
    """Keys in ``projected`` whose VALUE embeds ``folder_path`` but which are
    not declared in :data:`PATH_BEARING_ENV_KEYS`.

    The drift gate for table 1. ``projected`` is a rendered env projection for
    a fixture project; ``folder_path`` is that fixture's folder.

    Comparison is separator-normalised (``\\`` → ``/``) and case-folded, so a
    Windows-shaped projection is checked with the same rule as a POSIX one.
    """
    needle = folder_path.replace("\\", "/").casefold()
    if not needle:
        return ()
    out: list[str] = []
    for key, value in projected.items():
        if key in PATH_BEARING_ENV_KEYS:
            continue
        if needle in str(value).replace("\\", "/").casefold():
            out.append(key)
    return tuple(sorted(out))
