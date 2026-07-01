# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""DB-as-source-of-truth contract for per-project canonical env projection.

This module is Phase 0.B of the diagrams-integration plan (2026-05-24,
see ``.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md``).
It codifies the rule that the launcher's SQLite DB is the SINGLE SOURCE
OF TRUTH for every per-project canonical env value, and that this module
is the ONLY legal writer of those values to the three env surfaces.

Why this exists
~~~~~~~~~~~~~~~

Pre-Phase-0, env values reached on-disk surfaces via FOUR independent
paths:

  * Rust ``write_project_env_files`` (the dominant writer; called during
    project create/rename/refresh).
  * Rust ``ensure_project_env_template`` (the ``.env`` template — sibling
    surface, NOT in scope for this contract; see Out of scope below).
  * Python ``install.py`` backfill helpers (``_backfill_kg_collection_env_in_project``
    and friends) that scribble missing canonical keys when ``install-bundle
    --update`` runs against an older project.
  * Per-grant-change Tauri commands that called ``write_project_env_files``
    directly (e.g. ``kg_set_collection_access_mode``).

Each path had its own opinion about what to write and how to merge. Bug-4
of the install-flow architectural overhaul (PR-145, 2026-05-06) was
specifically a wholesale-replace of the ``env`` sub-block in
``.claude/settings.json`` that silently dropped user-added keys. The
fix was a deep-merge in ``write_project_env_files``, but other writers
remained free to regress the same bug by accident — a CI lint had to be
added retroactively.

This module replaces that "many opinions, hope they agree" model with
"one writer, lint-enforced". Every canonical key flows through
:func:`project_env_from_db`; every surface write flows through
:func:`apply_project_env`. New writers anywhere in the codebase that
touch the canonical key set fail the CI lint in
``tests/test_config_projection_single_writer.py``.

Option A vs B (Rust ↔ Python interop)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two interop strategies were considered (see Phase 0.B task brief):

  * Option A — Python is canonical; Rust callers subprocess into the
    ``vco_lib.config_projection apply`` CLI when they need to project
    env to disk.
  * Option B — Port the contract to Rust as a sibling module of
    ``project_env_settings.rs``, with parity tests pinning the two
    implementations together.

This module chooses **Option A**. Reasons:

  1. Single source of truth for byte layout. With Option B, two
     independent implementations of the "deep-merge + bracket-marker
     replacement + atomic write" logic exist; pinning them together
     requires an exhaustive parity test that has to exercise every
     edge case (empty file, malformed JSON, marker without closing
     marker, etc.). Option A has no parity to maintain.
  2. Subprocess cost is negligible for the call sites that need it.
     Project create/rename/refresh fire once per user action (GUI
     click); grant-toggle endpoints fire once per user-driven toggle.
     ~100 ms of Python start-up amortised over a click latency the user
     already accepts.
  3. Python is where ``install.py`` lives. Most of the legacy direct-
     write call sites that the lint rule targets are Python helpers
     in ``install.py``'s backfill section; migrating them to the
     contract is a same-language refactor with no FFI involved.
  4. The launcher already shells out to Python in several places
     (``vco_lib.project_init derive``, ``--migrate-collections``); a
     fourth CLI verb does not add infrastructure.

The trade-off: Rust call sites pay subprocess overhead (one ``python3``
spawn per project mutation). Measured worst-case on a cold Python
interpreter: ~110 ms. Acceptable for user-driven actions; would NOT be
acceptable for hot-path hooks, but no hook ever calls this — hooks
READ env vars; this module only WRITES them.

Public API
~~~~~~~~~~

.. code-block:: python

    from pathlib import Path
    from vco_lib.config_projection import (
        project_env_from_db,
        apply_project_env,
        list_canonical_keys,
    )

    bundle = project_env_from_db("<project-uuid>")
    report = apply_project_env(bundle)
    # report = {"claude_settings_json": ["KG_COLLECTION", ...], ...}

CLI entry point
~~~~~~~~~~~~~~~

For Rust callers that want the write surface in Python::

    python -m vco_lib.config_projection apply --project-id <uuid>
    python -m vco_lib.config_projection list-keys --json
    python -m vco_lib.config_projection from-db --project-id <uuid> --json

Out of scope
~~~~~~~~~~~~

* The ``<project_root>/.env`` template file managed by
  ``ensure_project_env_template`` (Rust) and
  ``_ensure_env_template`` (Python). That file uses different rules
  (append-only, ``# added by vco`` markers, commented placeholders)
  and a different audience (CLI users who edit it by hand). It will
  be migrated through a parallel ``apply_project_env_template``
  contract in a future Phase 0.D when the cross-language ``.env``
  template-key parity test (``env_template_canonical_keys_match_python``)
  is also tightened.
* Adding new canonical env keys. This module ROUTES existing keys
  through one contract; widening the canonical key set is a separate
  governance step that must update both the Rust ``CANONICAL_INSTALL_ENV_KEYS``
  const and :func:`list_canonical_keys` here.
* User-bucket secret VALUES (keychain-resident). The OS keychain is
  Rust-owned; there is no Python keychain bridge yet. Phase 0.E
  (2026-05-25) adds :func:`apply_user_secrets` which ROUTES the
  byte layout through this module while accepting pre-resolved
  (KEY, VALUE) pairs from the Rust caller (which queries the
  keychain). The DB-side resolver
  :func:`user_secret_known_keys_from_db` reads the strip set
  (every KEY ever observed across the three buckets — per_project,
  shared, global) so paused / deleted secrets actually leave the
  surfaces. See the "User-secret writes (Phase 0.E)" section
  below for the contract shape.

Cross-OS rules (non-negotiable)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Atomic writes via ``tempfile.NamedTemporaryFile`` + ``os.replace``.
  ``os.rename`` does NOT replace on Windows; ``os.replace`` does.
* ``pathlib.Path`` for all path construction; no string concatenation.
* No hardcoded ``/tmp`` — temp files land in the target directory so
  the rename is on the same filesystem (rename across filesystems
  fails with ``EXDEV`` on Linux).
* ``os.pathsep`` for PATH-style joins (not applicable here, but the
  pattern is documented for the migration sites).

Deep-merge contract
~~~~~~~~~~~~~~~~~~~

For JSON surfaces (.claude/settings.json, .vscode/settings.json):

  * The TOP-LEVEL object is preserved verbatim except for the env
    sub-block.
  * The env sub-block is treated as a flat string-to-string map.
    Canonical keys (returned by :func:`list_canonical_keys`) are
    OVERWRITTEN with the values from the bundle. Canonical keys whose
    value in the bundle is ``None`` (conditionally-omitted; e.g.
    ``VCT_ORCHESTRATOR_ROOT`` when the launcher runs outside a git
    checkout) are REMOVED from the existing env sub-block. Non-
    canonical keys (user-added) are PRESERVED byte-for-byte.
  * Reading a non-object env sub-block (someone hand-edited it into
    a string) replaces it with a fresh object containing only the
    canonical pairs — same fallback behaviour as the Rust writer.

For the ``.claude/env`` surface:

  * Lines between the ``# vco-managed-begin`` and ``# vco-managed-end``
    bracket markers are REPLACED wholesale on every call. Lines
    outside the markers are preserved verbatim.
  * The marker pattern is byte-identical to the Rust constants
    ``CLAUDE_ENV_MANAGED_BEGIN`` / ``CLAUDE_ENV_MANAGED_END`` —
    drift here would silently break the in-place replace on the next
    call.

Byte-identical output guarantee
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Running ``apply_project_env`` against a freshly-created project must
produce output BYTE-IDENTICAL to what the Rust
``write_project_env_files`` produces for the same input. This is the
regression-proof acceptance criterion of Phase 0.B and is tested by
``tests/test_config_projection_byte_identical.py`` (parity guard).
Divergences caught by the parity test must be fixed by changing the
Python side (Rust is the source of truth for byte layout until the
follow-up PR that flips production callers).

User-secret writes (Phase 0.E, 2026-05-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Phase 0.B explicitly excluded ``user_secret_pairs`` /
``user_secret_known_keys`` because their VALUE side lives in the OS
keychain (Rust-owned; no Python bridge exists). Phase 0.E extends the
contract to cover their WRITE side without bridging the keychain:

  * The Rust caller queries the keychain via the existing
    ``commands::project_env_settings::resolve_user_secret_state``
    code path → produces ``(KEY, VALUE)`` pairs across the three
    buckets (``per_project``, ``shared``, ``global``).
  * The Rust caller serialises those pairs to JSON and invokes
    ``python -m vco_lib.config_projection apply-user-secrets
    --project-id <id> --pairs-json <file>``.
  * Python reads the launcher DB to compute the STRIP set
    (every KEY ever observed in ``secret_active_state`` across
    the three buckets, regardless of active flag) via
    :func:`user_secret_known_keys_from_db`. Pairs in the input AND
    in the known-keys list are EMITTED; known-keys NOT in the
    input are STRIPPED from the JSON env blocks (signal-to-remove).
  * Python writes through the SAME atomic-write pipeline as the
    canonical env, preserving byte-identical output with the Rust
    writer (``build_claude_env_managed_block_with_user_secrets``
    and ``merge_env_object_canonical_with_user_secrets``).

The three lifecycle scenarios this contract supports:

  1. **Fresh secret creation** — KEY appears in both ``user_secret_pairs``
     (input) and the DB strip set (because ``set_secret_v2`` writes the
     active-flag row before invoking the env-refresh hook). EMITTED to
     every surface.
  2. **Secret update (overwrite existing)** — same shape as creation;
     the new VALUE replaces the old in the JSON env objects and the
     ``.claude/env`` managed block is rebuilt from scratch.
  3. **Secret deletion / pause** — KEY is in the DB strip set but
     absent from ``user_secret_pairs`` (because either ``clear_secret_v2``
     removed the keychain entry, or the active flag is 0, or the
     keychain backend is unreachable). The key is REMOVED from the
     JSON env blocks via the explicit strip pass, and is simply
     absent from the rebuilt ``.claude/env`` managed block.

Three reasons NOT to fully bridge the keychain into Python today:

  * The OS keychain APIs (Linux Secret Service, macOS Keychain
    Services, Windows Credential Manager) have well-tested Rust
    bindings via the launcher's ``secrets`` crate; the Python
    side would need a parallel implementation that handles the same
    three backends + the soft-fail discipline (keychain unreachable
    → empty pairs, not a crash).
  * The active-flag gate uses the cross-launcher discovery walker
    in ``db::secret_active::is_secret_active_cross_launcher`` —
    a sibling-walking ``read_is_active_from_db_file`` over every
    other launcher's DB. Re-implementing that defensively in Python
    is doable but doubles the surface area for soft-fail bugs.
  * Two implementations of the same security-sensitive lifecycle
    is a worse outcome than one implementation that owns it.

So the contract is asymmetric on purpose: VALUES stay Rust-owned;
LAYOUT moves to Python (and is shared with the canonical writer's
layout, which was the whole point of Phase 0.B).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, TypedDict

from vco_lib.atomic import atomic_write_text
from vco_lib.launcher_db_reader import (
    ACTIVE_EMBEDDING_SETTING_KEY,
    ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
    ACTIVE_EMBEDDING_SOURCE_USER,
    APP_STATE_KEY_ACTIVE_EMBEDDING,
    APP_STATE_KEY_DEFAULT_TEXT_EMBED,
    ORCHESTRATOR_CORE_MODULE_ID,
    profile_for_text_model,
)



# ─── Canonical key registry ─────────────────────────────────────────────
#
# This list is the Python sibling of the Rust ``CANONICAL_INSTALL_ENV_KEYS``
# constant in ``launcher/src-tauri/src/commands/projects_v2.rs``. Adding
# a key here requires also adding it to the Rust const AND adding a value-
# resolution arm to ``project_env_from_db`` below. The CI lint test
# ``tests/test_config_projection_single_writer.py`` does NOT pin the two
# lists together (that's the parity test's job); the lint test only
# forbids direct writes to ANY of these keys outside this module.
#
# Order matches the Rust const for ease of cross-language diffing. Order
# DOES affect the ``.claude/env`` line ordering (which is human-readable
# only — no semantic difference) but does NOT affect the JSON env block
# (Python dict insertion-ordered → serialised in insertion order, but
# both Rust serde_json and Python json sort by neither, so the order
# only matters for the .claude/env shell file).

_CANONICAL_KEYS: tuple[str, ...] = (
    "KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    # DIAGRAMS_COLLECTION (Phase 1.5 — Diagrams Integration, fix/a1-
    # indexing-pipeline 2026-05-25). Paired with KG_COLLECTION via the
    # canonical sanitized-basename + "_Diagrams" suffix (see the
    # ``derive_project_collection_names`` rule in vco_lib.project_init).
    # Consumed by:
    #   * `vco_lib.diagram_indexer::index_diagram` for the Weaviate
    #     upsert target (Phase 1.5.A indexer hot path).
    #   * `claude_mcp_servers/weaviate_mcp/server.py::DIAGRAMS_COLLECTION`
    #     for hybrid_search fan-out into diagram results (Phase 1.5.C).
    # The Rust ``CANONICAL_INSTALL_ENV_KEYS`` constant
    # (launcher/src-tauri/src/commands/projects_v2.rs L3087) does NOT
    # yet include this key — adding it there is a separate Rust-side PR.
    # Until that lands, the Rust ``write_project_env_files`` path will
    # NOT emit DIAGRAMS_COLLECTION (only this Python contract does).
    # That's deliberate: the Python ``vco_lib.config_projection apply``
    # CLI is the canonical writer per the Option-A interop strategy
    # documented at the top of this module; production callers that
    # need DIAGRAMS_COLLECTION on disk subprocess into the Python CLI.
    # The byte-identical parity test
    # (tests/test_config_projection_byte_identical.py) feeds a bundle
    # that doesn't include DIAGRAMS_COLLECTION, so its assertions are
    # unaffected by this addition.
    "DIAGRAMS_COLLECTION",
    "SHARED_KG_COLLECTION",
    "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT",
    # v0.2.46 Decision B — per-project READ gate for the shared KG.
    # Symmetric mirror of SHARED_KG_WRITE_DISABLED; no legacy alias
    # because the read path was unconditional pre-v0.2.46. Consumed by
    # ``claude_mcp_servers/weaviate_mcp/server.py::
    # _resolve_shared_kg_read_disabled`` to gate hybrid_search /
    # semantic_graph_search fan-out into the shared collection.
    "SHARED_KG_READ_DISABLED",
    "PROJECT_NAME",
    "CODE_GRAPH_PROJECT",
    "ACTIVE_EMBEDDING",
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
    "VCT_ORCHESTRATOR_ROOT",
    "VCT_INFRASTRUCTURE_DIR",
    # v0.2.37 (Gap 6a): legacy alias for VCT_ORCHESTRATOR_ROOT consumed
    # by the templates/scripts/code-graph-analyze venv-fallback (probes
    # ``$VCT_INSTALL_ROOT/.venv``). Pre-v0.2.37 only the launcher
    # exported this key; direct CLI invocations in a fresh OSS install
    # had no way to reach the analyzer venv. Emitted alongside
    # VCT_ORCHESTRATOR_ROOT when orchestrator_root is set; same value.
    "VCT_INSTALL_ROOT",
    # v0.2.49 SB1: the project's launcher.db UUID. Load-bearing for the
    # Phase-8 access-matrix WRITE gate: hooks + the MCP server read this
    # env var to identify the project against the hub's
    # ``GET /api/v1/projects/{id}/access/{collection}`` endpoint. Without
    # it the gate's empty-PID branch fires and writes proceed via the
    # silent-bypass path (the SB1 fix: deferral + dropped_writes.jsonl
    # metric at claude_mcp_servers/weaviate_mcp/server.py and
    # templates/hooks/post-file-edit.{sh,ps1}).
    #
    # Always emitted by ``project_env_from_db`` (canonical DB-resolved
    # value); the standalone path in
    # ``vco_lib.project_init._apply_standalone_env`` omits it (no DB →
    # no UUID → the gate's empty-PID deferral fires at first WRITE and
    # the user is told to re-register the project via the Launcher GUI).
    "VCT_PROJECT_ID",
    "VCT_KG_ACCESS_LIST",
    "VCT_CODE_GRAPH_ACCESS_LIST",
    # Diagrams cross-project visibility (v0.2.34, A7). Previously the MCP
    # piggybacked on ``VCT_KG_ACCESS_LIST`` — wrong granularity: granting
    # KG access leaked diagram visibility, and granting diagram-only
    # access never reached the MCP. This key is sourced from the
    # ``diagram_access`` SQLite table (joined to ``projects.name`` on
    # the grantor side) and consumed by ``weaviate_mcp/server.py::
    # _diagrams_peer_collections`` with no fallback to the KG list.
    # Conditionally emitted: omitted when no peers granted diagram read.
    "VCT_DIAGRAMS_ACCESS_LIST",
    "GITHUB_TOKEN",
)


def list_canonical_keys() -> set[str]:
    """Return the closed set of canonical env keys this module manages.

    The CI lint at ``tests/test_config_projection_single_writer.py``
    consumes this list to detect direct writes elsewhere in the codebase.
    Adding a key here implies adding a value-resolver arm to
    :func:`project_env_from_db` AND mirroring the addition in the Rust
    ``CANONICAL_INSTALL_ENV_KEYS`` const.

    Returns a fresh ``set`` each call so callers can mutate the result
    without affecting other callers.
    """
    return set(_CANONICAL_KEYS)


# ─── Bracket markers for the .claude/env surface ────────────────────────
#
# Must remain byte-identical to the Rust ``CLAUDE_ENV_MANAGED_BEGIN`` /
# ``CLAUDE_ENV_MANAGED_END`` constants in projects_v2.rs. The in-place
# replace on the next call depends on substring match.

CLAUDE_ENV_MANAGED_BEGIN: str = "# vco-managed-begin"
CLAUDE_ENV_MANAGED_END: str = "# vco-managed-end"


# ─── Public dataclasses / types ─────────────────────────────────────────


class ProjectEnvBundle(TypedDict):
    """Complete set of canonical env values for one project.

    ``canonical_env`` is a flat key→value map. Keys are a subset of
    :func:`list_canonical_keys`; keys that the resolver decided to OMIT
    for this project (e.g. ``VCT_ORCHESTRATOR_ROOT`` when the launcher
    runs outside a git checkout, ``VCT_KG_ACCESS_LIST`` when no peers
    granted access) are simply absent from the dict — :func:`apply_project_env`
    treats absent canonical keys as a SIGNAL TO REMOVE the key from the
    existing env surfaces (deep-merge with deletion semantics for
    canonical keys only; non-canonical user keys are never touched).

    All values are strings (the JSON env sub-block is a string→string
    map by Claude Code's contract; ``.claude/env`` is shell-source so
    everything is a string anyway).

    ``project_id`` and ``project_root`` are carried alongside the env
    map so callers don't have to re-query the DB to know where to write.
    """

    canonical_env: dict[str, str]
    project_id: str
    project_root: Path


class UserSecretBundle(TypedDict):
    """Phase 0.E (2026-05-25) — input bundle for :func:`apply_user_secrets`.

    The Rust caller resolves the keychain VALUES (via the existing
    ``commands::project_env_settings::resolve_user_secret_state``)
    and produces this bundle for the Python writer to consume.

    ``user_secret_pairs`` is the ordered list of ``(KEY, VALUE)``
    tuples to EMIT. Order matches the Rust resolver's bucket-precedence
    rule (per-project bucket wins on collision; shared then global
    fill the rest). The list is intentionally a list-of-tuples
    rather than a dict so two pairs with the same KEY but different
    VALUES (a Rust resolver bug) would be visibly malformed at the
    JSON boundary rather than silently de-duplicated by ``json.load``.
    The Python writer treats LAST-write-wins per-key when emitting
    (matching the Rust behaviour: ``env_obj.insert(k, v)`` in
    ``merge_env_object_canonical_with_user_secrets``).

    ``user_secret_known_keys`` is the STRIP set — every KEY that has
    ever had an active-flag row across the three buckets, regardless
    of current active flag. The writer REMOVES any key in this list
    that is NOT in ``user_secret_pairs`` from the JSON env blocks
    (signal-to-remove semantics; the ``.claude/env`` BEGIN/END
    replace handles strip implicitly via wholesale block rebuild).

    ``project_id`` and ``project_root`` are carried alongside so
    callers don't have to re-query the DB to know where to write.

    Invariants enforced by the resolver, not by the dataclass:

      * ``user_secret_known_keys`` is a superset of the keys in
        ``user_secret_pairs`` (the strip set always carries every
        active key — if it didn't, the EMIT for that key would not
        survive a subsequent paused-secret strip pass).
      * No key in ``user_secret_pairs`` is a canonical key (the
        ``set_secret_v2`` GUI path doesn't let users pick canonical
        names; defensive enforcement happens at the call site).

    See also the module-level "User-secret writes (Phase 0.E)" doc
    section.
    """

    user_secret_pairs: list[tuple[str, str]]
    user_secret_known_keys: list[str]
    project_id: str
    project_root: Path


# Sentinel project_id values used in the secret_active_state schema.
# The launcher uses these literals to key shared / global secrets so
# every project's resolver sees the same row. They MUST match the Rust
# constants in launcher/src-tauri/src/secrets.rs (SENTINEL_SHARED /
# SENTINEL_GLOBAL) — a drift here silently dis-routes the env-time
# strip set across reboots.

_USER_SECRET_SCOPE_PER_PROJECT = "per_project"
_USER_SECRET_SCOPE_SHARED = "shared"
_USER_SECRET_SCOPE_GLOBAL = "global"
_USER_SECRET_PROJECT_ID_SHARED = "_user_shared_"
_USER_SECRET_PROJECT_ID_GLOBAL = "_global_"
_USER_SECRET_MODULE_ID = "user"


@dataclass(frozen=True, slots=True)
class _DbProjectRow:
    """Internal: a slim projection of the ``projects`` table row."""

    id: str
    name: str
    folder_path: str
    slug: str


# ─── DB resolver ────────────────────────────────────────────────────────


def _resolve_launcher_db_path() -> Path:
    """Return the path to the launcher's SQLite DB.

    v0.2.54: delegates to :func:`vco_lib.paths.launcher_db_path` so the
    ``$VCT_LAUNCHER_DB_PATH`` override is honoured uniformly across
    every Python-side resolver (previously only ``launcher_db_reader``
    honoured it — split-brain).

    The file may not exist (fresh install where the launcher has never
    been started). Callers must handle that.
    """
    from vco_lib.paths import launcher_db_path
    return launcher_db_path()


def _open_db_read_only(db_path: Path) -> sqlite3.Connection:
    """Open the launcher DB read-only with sane defaults.

    Read-only opens are CRITICAL: the launcher is the only writer of
    its own DB; this module is a CLIENT. Opening read-write here would
    create a WAL file owned by Python's process and disrupt the
    launcher's connection lifecycle.

    Raises:
        sqlite3.OperationalError: if the file doesn't exist or can't be
            opened (e.g. perms). Callers should wrap and re-raise with
            context.
    """
    if not db_path.is_file():
        raise FileNotFoundError(
            f"launcher.db not found at {db_path}; is the launcher running "
            f"and has any project been registered?"
        )
    # SQLite's URI form lets us pass mode=ro reliably across platforms.
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, timeout=5.0
    )
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_project_row(conn: sqlite3.Connection, project_id: str) -> _DbProjectRow:
    """Read the ``projects`` row for a given project_id.

    Raises:
        ProjectNotFound: if no row matches.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, folder_path, slug FROM projects WHERE id = ?",
        (project_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ProjectNotFound(f"no project row with id={project_id!r}")
    return _DbProjectRow(
        id=str(row["id"]),
        name=str(row["name"]),
        folder_path=str(row["folder_path"]),
        slug=str(row["slug"]),
    )


# ─── Public project-row helpers (Phase 0.B Part 2) ──────────────────────
#
# These two functions are the canonical lookup primitives used by the
# diagrams CLIs (``vco rebuild-diagram-index`` and ``vco verify-diagrams
# --all``). They read the ``projects`` table via :func:`_open_db_read_only`
# so the launcher remains the only writer of its own DB.
#
# Resolution semantics:
#   * ``resolve_project_folder`` accepts either a project id (UUID) or a
#     slug; tries id first because that's the canonical handle in the
#     Rust commands, falls back to slug for the URL-addressable
#     ``/p/<slug>/...`` flow.
#   * ``list_registered_projects`` returns the closed set of fields the
#     consumers need (id, name, slug, folder_path), sorted by name so
#     the ``--all`` iteration order is deterministic across runs (useful
#     for CI diffs and progress-bar UX). A ``folder`` alias is included
#     alongside ``folder_path`` for back-compat with the rebuild CLI's
#     consumer that pre-dated the canonical spec.


def resolve_project_folder(
    project_id_or_slug: str,
    *,
    db_path: Path | None = None,
) -> Path:
    """Look up a project by id OR slug; return its absolute folder_path.

    Tries ``projects.id`` first (the canonical handle); if no match,
    falls back to ``projects.slug``. Both columns are unique in the
    launcher schema (id is PRIMARY KEY; slug has a UNIQUE index per
    migration 003), so the lookup is at most two indexed point reads.

    Args:
        project_id_or_slug: Either the UUID stored in ``projects.id``
            or the URL-safe slug stored in ``projects.slug``.
        db_path: Optional override of the launcher DB location. Defaults
            to :func:`_resolve_launcher_db_path`. Tests should pass an
            explicit path to avoid touching the real launcher DB.

    Returns:
        The absolute folder path as a :class:`pathlib.Path`.

    Raises:
        LookupError: when neither id nor slug matches. The diagrams CLIs
            translate this to ``EXIT_ENV_PROBLEM`` (exit code 2).
        DbUnreachable: when the launcher DB is missing or unopenable.
            Distinct from LookupError so callers can distinguish "no
            launcher installed" from "project not registered".
    """
    if db_path is None:
        db_path = _resolve_launcher_db_path()
    try:
        conn = _open_db_read_only(db_path)
    except FileNotFoundError as exc:
        raise DbUnreachable(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise DbUnreachable(
            f"cannot open launcher.db at {db_path}: {exc}"
        ) from exc
    try:
        cur = conn.cursor()
        # Try id first (canonical handle).
        cur.execute(
            "SELECT folder_path FROM projects WHERE id = ?",
            (project_id_or_slug,),
        )
        row = cur.fetchone()
        if row is None:
            # Fall back to slug (URL-addressable handle).
            cur.execute(
                "SELECT folder_path FROM projects WHERE slug = ?",
                (project_id_or_slug,),
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(
                f"no project with id or slug {project_id_or_slug!r}"
            )
        return Path(str(row["folder_path"]))
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def list_registered_projects(
    *, db_path: Path | None = None,
) -> list[Mapping[str, str]]:
    """Return every registered project as ``{id, name, slug, folder_path}``.

    Sorted by ``name`` for deterministic ``--all`` iteration order. Used
    by:

      * ``vco rebuild-diagram-index --all``
      * ``vco verify-diagrams --all``

    Each dict also carries a ``folder`` alias for ``folder_path`` so the
    rebuild CLI's existing consumer (``project.get("folder")``) keeps
    working without modification.

    Args:
        db_path: Optional override of the launcher DB location. Defaults
            to :func:`_resolve_launcher_db_path`. Tests should pass an
            explicit path to avoid touching the real launcher DB.

    Returns:
        List of mappings — empty list when no projects are registered
        (NOT an error). Each mapping has keys: ``id``, ``name``,
        ``slug``, ``folder_path``, and ``folder`` (alias).

    Raises:
        DbUnreachable: when the launcher DB is missing or unopenable.
    """
    if db_path is None:
        db_path = _resolve_launcher_db_path()
    try:
        conn = _open_db_read_only(db_path)
    except FileNotFoundError as exc:
        raise DbUnreachable(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise DbUnreachable(
            f"cannot open launcher.db at {db_path}: {exc}"
        ) from exc
    try:
        cur = conn.cursor()
        # ORDER BY name keeps the iteration order stable across
        # launcher runs; the slug-fallback in the WHERE clause defends
        # against rows where ``name`` was wiped to an empty string by a
        # bad rename (Bug-12 in the launcher's project-rename flow,
        # 2026-02-11).
        cur.execute(
            "SELECT id, name, folder_path, slug FROM projects "
            "ORDER BY name, slug"
        )
        out: list[Mapping[str, str]] = []
        for row in cur.fetchall():
            folder_path = str(row["folder_path"])
            out.append(
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "slug": str(row["slug"]),
                    "folder_path": folder_path,
                    # Back-compat alias for rebuild_diagram_index.py's
                    # consumer that pre-dated the canonical spec.
                    "folder": folder_path,
                }
            )
        return out
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _fetch_module_setting_bool(
    conn: sqlite3.Connection,
    project_id: str,
    module_id: str,
    setting_key: str,
    default: bool = False,
) -> bool:
    """Read a JSON-encoded boolean from the ``module_settings`` table.

    The ``setting_value`` column stores JSON (per the 001_initial.sql
    schema comment). A non-bool JSON value falls through to ``default``
    so a corrupt DB row doesn't crash the resolver.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT setting_value FROM module_settings "
        "WHERE project_id = ? AND module_id = ? AND setting_key = ?",
        (project_id, module_id, setting_key),
    )
    row = cur.fetchone()
    if row is None:
        return default
    try:
        v = json.loads(row["setting_value"])
    except (json.JSONDecodeError, TypeError):
        return default
    return bool(v) if isinstance(v, bool) else default


def _fetch_module_setting_str(
    conn: sqlite3.Connection,
    project_id: str,
    module_id: str,
    setting_key: str,
    default: str = "",
) -> str:
    """Read a JSON-encoded string from the ``module_settings`` table."""
    cur = conn.cursor()
    cur.execute(
        "SELECT setting_value FROM module_settings "
        "WHERE project_id = ? AND module_id = ? AND setting_key = ?",
        (project_id, module_id, setting_key),
    )
    row = cur.fetchone()
    if row is None:
        return default
    try:
        v = json.loads(row["setting_value"])
    except (json.JSONDecodeError, TypeError):
        return default
    return str(v) if isinstance(v, str) else default


def _fetch_module_setting_str_opt(
    conn: sqlite3.Connection,
    project_id: str,
    module_id: str,
    setting_key: str,
) -> Optional[str]:
    """Like :func:`_fetch_module_setting_str` but distinguishes absence.

    Returns ``None`` when the row is missing (or stores a non-string /
    unparseable JSON value), and the decoded string otherwise. Callers
    that must tell "row absent" apart from "row present == some default"
    use this rather than the ``default``-collapsing variant above.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT setting_value FROM module_settings "
        "WHERE project_id = ? AND module_id = ? AND setting_key = ?",
        (project_id, module_id, setting_key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        v = json.loads(row["setting_value"])
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, str) else None


def _fetch_app_state_str(
    conn: sqlite3.Connection,
    key: str,
) -> Optional[str]:
    """Read a raw string value from the launcher's ``app_state`` table.

    The ``app_state.value`` column stores values verbatim (NOT JSON —
    the Rust ``app_state_set`` writes the string directly), so this reads
    the column as-is rather than ``json.loads``-ing it. Returns ``None``
    when the table is absent (fresh install never booted), the key is
    missing, or any SQLite error occurs (soft-fail).
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    raw = row["value"]
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def _global_active_embedding(conn: sqlite3.Connection) -> Optional[str]:
    """Machine-global active-embedding profile from ``app_state``.

    ``app_state[embedding.active_profile]`` → ``app_state[default_text_embedding]``
    mapped via :func:`profile_for_text_model` → ``None``. Mirror of
    ``project_env_settings.rs::global_active_embedding`` (and the hub's
    ``hub_global_active_embedding``).
    """
    explicit = _fetch_app_state_str(conn, APP_STATE_KEY_ACTIVE_EMBEDDING)
    if explicit:
        return explicit
    return profile_for_text_model(
        _fetch_app_state_str(conn, APP_STATE_KEY_DEFAULT_TEXT_EMBED)
    )


def _resolve_active_embedding_cascade(
    conn: sqlite3.Connection,
    project_id: str,
) -> str:
    """Resolve ACTIVE_EMBEDDING for a project — the ONE shared cascade.

    LOCKED order (must match ``project_env_settings.rs::
    resolve_active_embedding_cascade`` + the hub ``config_api.rs``
    resolver byte-for-byte — cross-surface lockstep prevents the
    Defect-D class of GUI-write-vs-hub-read disagreement):

      1. per-project ``module_settings/orchestrator-core/active_embedding``
         WHERE ``active_embedding_source == "user"`` → returned verbatim
         (sticky deliberate pick).
      2. machine-global default (:func:`_global_active_embedding`).
      3. ``"qwen3"`` final fallback.

    An ``"auto"`` marker, a legacy NO-marker per-project row, or an absent
    per-project row all fall to leg 2 (inherit the global default). Every
    read is soft-fail.
    """
    source = _fetch_module_setting_str_opt(
        conn, project_id, ORCHESTRATOR_CORE_MODULE_ID,
        ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
    )
    if source == ACTIVE_EMBEDDING_SOURCE_USER:
        value = _fetch_module_setting_str_opt(
            conn, project_id, ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SETTING_KEY,
        )
        if value:
            return value
        # source=user but the value row is missing/empty — fall through.
    return _global_active_embedding(conn) or "qwen3"


def _fetch_kg_bindings(
    conn: sqlite3.Connection, project_id: str
) -> dict[str, str]:
    """Return ``{role: collection_name}`` for the project's KG bindings.

    Roles are: ``primary`` (own KG), ``shared`` (cross-project shared),
    ``archive`` (development collection). Missing roles are absent from
    the dict.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT role, collection_name FROM project_kg_bindings "
        "WHERE project_id = ?",
        (project_id,),
    )
    return {str(r["role"]): str(r["collection_name"]) for r in cur.fetchall()}


# ─── Shared-KG default resolver (v0.2.40 W40-C) ──────────────────────────


# Last-resort fallback for SHARED_KG_COLLECTION when launcher.db is
# unreachable, the orchestrator-root project row is absent, or its primary
# KG binding is empty. The value matches the Rust
# `LAST_RESORT_SHARED_KG_COLLECTION` const + the
# `vco_lib.project_init._SHARED_KG_NAME`; cross-language drift is pinned
# by ``tests/test_shared_kg_constant_consistency.py``.
#
# In production, this value should essentially never be observed —
# every machine that has run the launcher at least once has an
# `orchestrator-root` project row with a primary KG binding, and the
# Priority-2 resolver below returns that name. The const fires only on
# a totally-fresh-fresh first boot, or in tests with an empty / missing
# launcher.db.
_LAST_RESORT_SHARED_KG_NAME = "VibeCodedOrchestrator_KnowledgeGraph"


def _resolve_shared_kg_default_from_launcher_db(
    db_path: Path | None = None,
) -> str:
    """Return the shared-KG class name from the orchestrator-root binding.

    v0.2.40 W40-C: Python mirror of the Rust
    `resolve_shared_kg_from_orchestrator_root` resolver
    (``launcher/src-tauri/src/commands/project_env_settings.rs``).

    Reads ``project_kg_bindings`` for the project whose slug is
    ``orchestrator-root`` and role is ``primary``, returning that row's
    ``collection_name``. This is the SOURCE OF TRUTH for the shared-KG
    name on every machine that has run the launcher at least once
    (the launcher seeds the orchestrator-root row on first boot via
    ``ensure_orchestrator_root_kg_binding``).

    Soft-fail on EVERY error path — launcher.db missing, file
    unreadable, query fails, row absent, ``collection_name`` empty —
    returns :data:`_LAST_RESORT_SHARED_KG_NAME` (the bundled canonical
    name). Never raises.

    The intent: callers passing ``shared_kg_default=None`` to
    :func:`project_env_from_db` get the DB-driven name when possible,
    and the bundled const when not. The bundled const stays accurate
    for fresh installs but never overrides a real binding.

    Args:
        db_path: Optional override of the launcher DB location. Defaults
            to :func:`_resolve_launcher_db_path`. Tests should pass an
            explicit path.

    Returns:
        A non-empty Weaviate class name string. Never an empty string;
        never raises.
    """
    if db_path is None:
        try:
            db_path = _resolve_launcher_db_path()
        except Exception:
            return _LAST_RESORT_SHARED_KG_NAME

    # Soft-fail: any error path returns the last-resort const.
    try:
        if not db_path.is_file():
            return _LAST_RESORT_SHARED_KG_NAME
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0
        )
        try:
            conn.row_factory = sqlite3.Row
            # Look up the orchestrator-root project by slug.
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM projects WHERE slug = ?",
                ("orchestrator-root",),
            )
            row = cur.fetchone()
            if row is None:
                return _LAST_RESORT_SHARED_KG_NAME
            root_id = str(row["id"])

            # Read its primary KG binding (matches the Rust resolver
            # which reads `role='primary'`, not `role='shared'` — the
            # orchestrator-root's primary KG IS what every other
            # project's shared-KG resolves to).
            cur.execute(
                "SELECT collection_name FROM project_kg_bindings "
                "WHERE project_id = ? AND role = ?",
                (root_id, "primary"),
            )
            brow = cur.fetchone()
            if brow is None:
                return _LAST_RESORT_SHARED_KG_NAME
            name = str(brow["collection_name"]).strip()
            if not name:
                return _LAST_RESORT_SHARED_KG_NAME
            return name
        finally:
            conn.close()
    except Exception:
        # sqlite3.OperationalError, PermissionError, anything — fall back.
        return _LAST_RESORT_SHARED_KG_NAME


def _fetch_kg_access_list(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    own_kg: str,
    own_dev: str,
    shared_kg: str,
) -> list[str]:
    """Resolve the ``VCT_KG_ACCESS_LIST`` value.

    Mirrors Rust's ``resolve_kg_access_peers``: pull rows from
    ``kg_collection_access`` for this project where access_level !=
    'none', strip the project's own collections + the shared collection,
    return the peer-prefixes sorted + deduped.

    The Rust resolver strips the ``_KnowledgeGraph`` / ``_Development``
    suffix to get the peer's PREFIX (project name basename). We mirror
    that exactly so the env-var value is byte-identical.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT collection_name FROM kg_collection_access "
        "WHERE project_id = ? AND access_level != 'none'",
        (project_id,),
    )
    rows = [str(r["collection_name"]) for r in cur.fetchall()]
    skip = {own_kg, own_dev, shared_kg}
    out: set[str] = set()
    for coll in rows:
        if coll in skip or not coll:
            continue
        # Strip suffix to get the peer's prefix. Matches the Rust impl
        # at `project_env_settings.rs::resolve_kg_access_peers`.
        prefix = coll
        for suffix in ("_KnowledgeGraph", "_Development"):
            if prefix.endswith(suffix):
                prefix = prefix[: -len(suffix)]
                break
        if prefix and prefix != own_kg.removesuffix("_KnowledgeGraph"):
            out.add(prefix)
    return sorted(out)


def _fetch_code_graph_access_list(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Resolve the ``VCT_CODE_GRAPH_ACCESS_LIST`` value.

    Mirrors Rust's ``resolve_code_graph_access_peers``: join
    ``codegraph_access`` to ``projects`` on the grantor side, pull the
    grantor's slug. Excludes self (the project always has access to its
    own codegraph; the env var carries PEERS only).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT p.slug FROM codegraph_access ca "
        "JOIN projects p ON p.id = ca.grantor_project_id "
        "WHERE ca.grantee_project_id = ? AND ca.access_level = 'read'",
        (project_id,),
    )
    return sorted({str(r["slug"]) for r in cur.fetchall()})


def _fetch_diagram_access_list(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Resolve the ``VCT_DIAGRAMS_ACCESS_LIST`` value.

    Joins ``diagram_access`` to ``projects`` on the grantor side, pulling
    the grantor's display ``name``. The MCP consumer
    (``weaviate_mcp/server.py::_diagrams_peer_collections``) sanitises
    each name through ``_sanitize_collection_prefix`` and appends
    ``_Diagrams`` to derive the canonical Weaviate class name.

    Why ``p.name`` and not ``p.slug`` (as ``VCT_CODE_GRAPH_ACCESS_LIST``
    uses): the diagrams collection-prefix derivation is currently keyed
    on the project NAME (sanitised) — same rule the launcher's
    ``project_diagrams`` indexer uses when it writes
    ``<SanitizedName>_Diagrams`` rows into Weaviate. Switching to slugs
    would create a prefix mismatch between writer and reader.

    Excludes self by construction — the ``diagram_access`` schema's
    grantor/grantee pair is always cross-project (no self-grants).
    Empty / blank names are filtered (defensive against malformed DB
    rows; mirrors ``_fetch_kg_access_list``'s defensive prefix check).

    SQL uses parameterised ``?`` — no string concat. Same prepared-
    statement discipline as the sibling resolvers in this module.

    Phase 1.5.C (2026-05-24) had the MCP piggyback on
    ``VCT_KG_ACCESS_LIST`` as a temporary simplification. v0.2.34 A7
    splits the two: the MCP now reads ``VCT_DIAGRAMS_ACCESS_LIST``
    exclusively (no KG fallback), and this resolver is the single
    legal writer of that env var.
    """
    cur = conn.cursor()
    # Defensive: pre-migration-022 DBs lack the `diagram_access` table.
    # Phase 0.D's test fixtures + any partial-install scenario where
    # migration 022 hasn't run yet must still resolve env without
    # crashing. Empty list is the right "no grants" fallback semantically.
    try:
        cur.execute(
            "SELECT p.name FROM diagram_access da "
            "JOIN projects p ON p.id = da.grantor_project_id "
            "WHERE da.grantee_project_id = ? AND da.access_level = 'read' "
            "ORDER BY p.name",
            (project_id,),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    # Use a set for dedup, sort for deterministic output (matches the
    # ORDER BY in the SQL but defends against the case where two
    # grantors share a name — sort+dedup keeps the env var stable).
    return sorted({str(r["name"]) for r in cur.fetchall() if r["name"]})


# ─── Phase 0.E user-secret strip-set resolver ───────────────────────────


def _fetch_user_secret_known_keys(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Resolve the union of user-bucket secret KEYS across the three buckets.

    Mirrors the Rust resolver's combined output:

      * per-project bucket: ``(scope='per_project', project_id=<id>,
        module_id='user')`` — see
        ``db::secret_active::list_user_secret_keys_for_project``.
      * shared bucket: ``(scope='shared', project_id='_user_shared_',
        module_id='user')`` — see ``list_shared_user_secret_keys``.
      * global bucket: ``(scope='global', project_id='_global_',
        module_id='user')`` — see ``list_global_user_secret_keys``.

    The union is what drives the STRIP set: any key here that is NOT
    in the input ``user_secret_pairs`` is removed from the JSON env
    blocks on the next write. The Rust resolver dedups across buckets
    (a single KEY in multiple buckets appears once in the known-keys
    list); we mirror that with a ``set`` + sort.

    Order: ASCII-sorted (matches the Rust resolver's per-bucket
    ``ORDER BY key ASC`` discipline, then de-duplicates across
    buckets in the same alphabetical order).

    Soft-fail: a missing ``secret_active_state`` table (the migration
    007/009 boundary — a launcher.db that pre-dates the Phase 0.E
    contract entirely) returns an empty list, matching the Rust
    ``Vec::new()`` soft-fail. Same for the column-shape mismatch
    where the migration ran partially (we don't probe schema; the
    SELECT either succeeds or we treat it as "no keys observed").

    Args:
        conn: an open read-only sqlite3 connection.
        project_id: the project's UUID (drives the per-project bucket
            filter; sentinel rows for shared / global are always
            read for every project).

    Returns:
        ASCII-sorted, de-duplicated list of KEY names. Empty list
        when no rows exist OR the table is absent.

    SQL uses parameterised ``?`` for ``project_id``; the bucket
    discriminators are inlined string literals (no untrusted input).
    """
    cur = conn.cursor()
    # Defensive: pre-migration-007 DBs lack the `secret_active_state`
    # table entirely. Phase 0.D's test fixtures + any partial-install
    # scenario where migration 007 hasn't run yet must still resolve
    # without crashing.
    try:
        cur.execute(
            "SELECT key FROM secret_active_state "
            "WHERE module_id = ? AND ("
            "  (scope = ? AND project_id = ?) OR "
            "  (scope = ? AND project_id = ?) OR "
            "  (scope = ? AND project_id = ?)"
            ")",
            (
                _USER_SECRET_MODULE_ID,
                _USER_SECRET_SCOPE_PER_PROJECT, project_id,
                _USER_SECRET_SCOPE_SHARED, _USER_SECRET_PROJECT_ID_SHARED,
                _USER_SECRET_SCOPE_GLOBAL, _USER_SECRET_PROJECT_ID_GLOBAL,
            ),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    rows = cur.fetchall()
    # Dedup across buckets, sort for deterministic output. The Rust
    # resolver's bucket-precedence rule only matters for VALUE
    # collisions (per-project wins) — for the strip set, any one
    # bucket carrying the key is enough.
    return sorted({str(r["key"]) for r in rows if r["key"]})


def user_secret_known_keys_from_db(
    project_id: str,
    *,
    db_path: Path | None = None,
) -> list[str]:
    """Public API for the user-secret STRIP set lookup.

    Reads the union of user-bucket secret KEYS observed in
    ``secret_active_state`` across the three buckets (per-project,
    shared, global) for the given project. Used by
    :func:`apply_user_secrets` to drive the STRIP pass, and also
    available to Rust subprocess callers that want to inspect the
    set without applying.

    Args:
        project_id: The project's UUID.
        db_path: Optional override of the launcher DB location.
            Defaults to :func:`_resolve_launcher_db_path`. Tests
            should pass an explicit path.

    Returns:
        ASCII-sorted, de-duplicated list of KEY names.

    Raises:
        DbUnreachable: the launcher DB is missing or unopenable.
    """
    if db_path is None:
        db_path = _resolve_launcher_db_path()
    try:
        conn = _open_db_read_only(db_path)
    except FileNotFoundError as exc:
        raise DbUnreachable(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise DbUnreachable(
            f"cannot open launcher.db at {db_path}: {exc}"
        ) from exc
    try:
        return _fetch_user_secret_known_keys(conn, project_id)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ─── Sanitization (delegates to vco_lib.project_init SSOT) ─────────────
#
# NEW-10 / DEDUP-6 (v0.2.53) — consolidated to call the canonical
# underscore-DROPPING sanitizer in ``vco_lib.project_init``. The only
# behavioural delta this wrapper preserves is the "Vct" (capitalized)
# fallback used by ``ProjectEnvSettings.populate()``, vs the lowercase
# "vct" fallback used by the project_init-canonical version. Without
# this wrapper, the fallback-case env-write would change shape from
# ``Vct_KnowledgeGraph`` to ``vct_KnowledgeGraph`` — a real breakage
# for projects whose names sanitize to empty.
#
# The import is lazy (inside the function) to keep the import cycle
# loop closed: project_init.py imports config_projection.py via
# ``_apply_canonical_env_via_config_projection``; we mustn't take the
# import at module load.


def _sanitize_kg_collection(project_name: str) -> str:
    """Sanitize a project name into a Weaviate class prefix.

    DEDUP-6 (v0.2.53) — calls the SSOT
    ``vco_lib.project_init.sanitize_for_weaviate_class`` so the rule
    stays in one place. The only divergence from the SSOT is the
    fallback string: this wrapper returns ``"Vct"`` (capital V) where
    the SSOT returns ``"vct"`` (lowercase), preserving the
    historical contract that ``ProjectEnvSettings``-populated env
    rows use a capitalized fallback.

    See ``vco_lib.project_naming.canonical_class_prefix`` for the
    underscore-PRESERVING canonical sanitizer (used by the code-graph
    analyzer); this function uses the underscore-DROPPING rule because
    it's what install.py-emitted manifests have shipped with since
    v0.2.15.
    """
    # Lazy import — project_init imports config_projection at runtime,
    # so a top-level import here would create a cycle.
    from vco_lib.project_init import sanitize_for_weaviate_class

    sanitized = sanitize_for_weaviate_class(project_name)
    # Preserve the capitalized fallback for this consumer. project_init
    # returns "vct" (lowercase) in its fallback path; the env-write
    # surface here wants "Vct" so the class-name PostgreSQL/Weaviate
    # sees has its conventional initial-capital.
    if sanitized == "vct":
        return "Vct"
    return sanitized


# ─── Exceptions ─────────────────────────────────────────────────────────


class ConfigProjectionError(Exception):
    """Base for every error raised by this module."""


class ProjectNotFound(ConfigProjectionError):
    """No row in ``projects`` matches the supplied project_id."""


class DbUnreachable(ConfigProjectionError):
    """Could not open the launcher DB (missing, perms, corrupt)."""


# ─── project_env_from_db ────────────────────────────────────────────────


def project_env_from_db(
    project_id: str,
    *,
    db_path: Path | None = None,
    weaviate_url_override: str | None = None,
    ollama_url_override: str | None = None,
    active_embedding_override: str | None = None,
    shared_kg_default: str | None = None,
    weaviate_port_default: int = 8081,
    ollama_port_default: int = 11435,
    code_embed_port_default: int = 11440,
    orchestrator_root: Path | None = None,
) -> ProjectEnvBundle:
    """Resolve the complete canonical env bundle for a project.

    Reads (in order):

      1. ``projects`` (id, name, folder_path, slug) — :class:`ProjectNotFound`
         if missing.
      2. ``project_kg_bindings`` rows for the three KG roles (primary,
         shared, archive=development).
      3. ``kg_collection_access`` rows → ``VCT_KG_ACCESS_LIST``.
      4. ``codegraph_access`` rows joined to ``projects`` → ``VCT_CODE_GRAPH_ACCESS_LIST``.
      5. ``module_settings`` rows for the orchestrator-core module:
         ``shared_kg_write_disabled``, ``active_embedding``.

    Then composes the canonical env map. Keys whose resolved value is
    empty / unset / None are OMITTED from the returned dict — callers
    treat omission as the signal to remove the key from existing
    surfaces (see :func:`apply_project_env`).

    PURE FUNCTION: same DB state in → same dict out. No filesystem writes,
    no caching, no environment-variable reads (overrides come in via
    explicit keyword arguments so test fixtures can pin them).

    Args:
        project_id: The project's UUID (the ``projects.id`` column).
        db_path: Optional override of the launcher DB location. Defaults
            to :func:`_resolve_launcher_db_path`. Tests should pass an
            explicit path to avoid touching the real ``~/.vct/launcher.db``.
        weaviate_url_override: Pin the WEAVIATE_URL value (otherwise
            derived from ``http://localhost:<weaviate_port_default>``).
            The Rust resolver pulls this from ``LocalConfig`` /
            ``services.toml``; this Python contract takes the resolved
            value as an explicit arg so the caller can plumb their own
            services-config reader if needed.
        ollama_url_override: Same shape, for OLLAMA_URL.
        active_embedding_override: Pin ACTIVE_EMBEDDING; otherwise read
            from ``module_settings`` (orchestrator-core /
            active_embedding), defaulting to "qwen3".
        shared_kg_default: Fallback SHARED_KG_COLLECTION when the
            project's ``shared`` KG binding row is absent.

            **v0.2.40 W40-C**: when ``None`` (the new default), the
            resolver consults the launcher's
            ``project_kg_bindings(slug='orchestrator-root',
            role='primary').collection_name`` and uses that value as
            the fallback. This makes future shared-KG name flips
            propagate automatically rather than getting silently
            stranded behind a stale const.

            Soft-fail: launcher.db unreachable / orchestrator-root row
            absent / binding empty → falls back to the bundled const
            (``"VibeCodedOrchestrator_KnowledgeGraph"`` — matches the
            Rust ``LAST_RESORT_SHARED_KG_COLLECTION`` constant).

            Explicit string overrides (CLI tests, white-label installs)
            still win and skip the DB-read.
        weaviate_port_default: Port to use when constructing
            ``WEAVIATE_URL`` (and as ``WEAVIATE_PORT``). Default 8081.
        ollama_port_default: Port for OLLAMA_URL / OLLAMA_PORT. Default
            11435.
        code_embed_port_default: Port for CODE_EMBED_URL / CODE_EMBED_PORT.
            Default 11440.
        orchestrator_root: When set, emit ``VCT_ORCHESTRATOR_ROOT`` and
            ``VCT_INFRASTRUCTURE_DIR``. When ``None`` (default), those
            two keys are OMITTED — matching the Rust resolver's
            "launcher running outside a git checkout" semantics.

    Returns:
        A :class:`ProjectEnvBundle` ready to feed into
        :func:`apply_project_env`.

    Raises:
        DbUnreachable: launcher DB missing / unopenable.
        ProjectNotFound: no row in ``projects`` matches.
    """
    if db_path is None:
        db_path = _resolve_launcher_db_path()
    try:
        conn = _open_db_read_only(db_path)
    except FileNotFoundError as exc:
        raise DbUnreachable(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise DbUnreachable(
            f"cannot open launcher.db at {db_path}: {exc}"
        ) from exc

    try:
        proj = _fetch_project_row(conn, project_id)
        kg_bindings = _fetch_kg_bindings(conn, project_id)

        # Resolve the three KG collection names. Primary is REQUIRED —
        # every registered project should have one after the launcher's
        # startup backfill. Fall back to sanitized-name-derived defaults
        # (matching the Rust populate() fallback) so an unbacked project
        # row still produces a usable bundle.
        sanitized = _sanitize_kg_collection(proj.name)
        kg_collection = kg_bindings.get(
            "primary", f"{sanitized}_KnowledgeGraph"
        )
        # v0.2.40 W40-C: when `shared_kg_default` was not provided by
        # the caller (the new default), resolve from launcher.db's
        # orchestrator-root primary binding rather than a stale const.
        # Soft-fail returns the bundled
        # `_LAST_RESORT_SHARED_KG_NAME` const when the DB is
        # unreachable / no orchestrator-root row / binding empty.
        if shared_kg_default is None:
            resolved_default = _resolve_shared_kg_default_from_launcher_db(
                db_path=db_path,
            )
        else:
            resolved_default = shared_kg_default
        shared_kg = kg_bindings.get("shared", resolved_default)
        dev_collection = kg_bindings.get(
            "archive", f"{sanitized}_Development"
        )

        # Phase 1.5 — Diagrams collection. The launcher's DB doesn't (yet)
        # have a kg_bindings role for diagrams; derive from the primary
        # KG collection via the canonical suffix swap so an explicit
        # `primary` override (e.g. a user-renamed `MyKG`) carries through
        # to `MyKG_Diagrams` correctly. Falls back to the sanitized-name
        # default when the primary doesn't end with `_KnowledgeGraph`.
        # Mirrors `vco_lib.project_init.derive_project_collection_names`'s
        # rule (`<sanitized>_Diagrams`) — both code paths must agree on
        # the same canonical name or the indexer would write to one
        # collection while the MCP reads from another.
        if kg_collection.endswith("_KnowledgeGraph"):
            diagrams_collection = (
                kg_collection[: -len("_KnowledgeGraph")] + "_Diagrams"
            )
        else:
            diagrams_collection = f"{sanitized}_Diagrams"

        # Access lists.
        kg_access = _fetch_kg_access_list(
            conn,
            project_id,
            own_kg=kg_collection,
            own_dev=dev_collection,
            shared_kg=shared_kg,
        )
        code_graph_access = _fetch_code_graph_access_list(conn, project_id)
        # v0.2.34 A7: independent diagrams access matrix. Previously the
        # MCP fell back to VCT_KG_ACCESS_LIST, which had the wrong
        # granularity (granting KG leaked diagrams; granting only
        # diagrams was invisible to the MCP). See `_fetch_diagram_access_list`.
        diagram_access = _fetch_diagram_access_list(conn, project_id)

        # Module settings — orchestrator-core scope.
        shared_kg_write_disabled = _fetch_module_setting_bool(
            conn, project_id, "orchestrator-core",
            "shared_kg_write_disabled", default=False,
        )
        # v0.2.46 Decision B — symmetric read gate. Same module_id +
        # default semantics as the write gate above (orchestrator-core
        # scope, default false meaning reads allowed). No legacy alias
        # to honour — pre-v0.2.46 the read path was unconditional.
        shared_kg_read_disabled = _fetch_module_setting_bool(
            conn, project_id, "orchestrator-core",
            "shared_kg_read_disabled", default=False,
        )
        if active_embedding_override is not None:
            active_embedding = active_embedding_override
        else:
            # v0.2.71 T-B-emb: the LOAD-BEARING ACTIVE_EMBEDDING writer.
            # The value here is what lands in .claude/{settings.json,env}
            # (the Rust populate() value does NOT reach those canonical
            # surfaces). Resolve via the ONE cascade — must match
            # project_env_settings.rs::resolve_active_embedding_cascade +
            # the hub config_api.rs resolver EXACTLY (cross-surface lockstep,
            # the Defect-D class):
            #
            #   1. per-project module_settings/orchestrator-core/active_embedding
            #      WHERE active_embedding_source == "user" → verbatim (sticky).
            #   2. machine-global app_state[embedding.active_profile], then the
            #      hardware-pick derive (app_state[default_text_embedding] →
            #      profile). This is the BRIDGE: a non-user project yields to
            #      the global value the launcher/install.py also computed.
            #   3. "qwen3" final fallback.
            #
            # An "auto" marker OR a legacy NO-marker per-project row both fall
            # to leg 2 (inherit global) — the LOCKED v0.2.71 decision that
            # supersedes the brittle pre-v0.2.71 "stored == qwen3" heuristic
            # and fixes the Fabio auto-qwen3 case (a backfill-stamped qwen3 with
            # no provenance). GUARD on the derive: an unmapped/absent hardware
            # pick → stay qwen3 (never stamp a guessed profile → wrong slot).
            active_embedding = _resolve_active_embedding_cascade(conn, project_id)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    # Compose URLs from ports (matches Rust's
    # `format!("http://localhost:{}", weaviate_port)`).
    weaviate_url = (
        weaviate_url_override
        or f"http://localhost:{weaviate_port_default}"
    )
    ollama_url = (
        ollama_url_override
        or f"http://localhost:{ollama_port_default}"
    )
    code_embed_url = f"http://localhost:{code_embed_port_default}"

    # Build the canonical env map. Keys with None/empty value are OMITTED.
    # We use a plain dict because Python's dict is insertion-ordered
    # (PEP 468 / CPython 3.7+) and we want the .claude/env line order to
    # match _CANONICAL_KEYS for human-readability cross-language.
    env: dict[str, str] = {}

    def _set(key: str, value: Optional[str]) -> None:
        """Assign to env iff value is non-None and non-empty.

        Empty-string values are OMITTED to match the Rust semantics for
        conditionally-emitted keys (VCT_KG_ACCESS_LIST etc.). For keys
        like KG_COLLECTION that must always be present, the caller must
        pass a non-empty value.
        """
        if value is None or value == "":
            return
        env[key] = value

    _set("KG_COLLECTION", kg_collection)
    _set("DEVELOPMENT_COLLECTION", dev_collection)
    _set("DIAGRAMS_COLLECTION", diagrams_collection)
    _set("SHARED_KG_COLLECTION", shared_kg)
    # v0.2.49 SB1: emit VCT_PROJECT_ID so hooks + the MCP server can
    # identify this project against the hub's access-matrix endpoint.
    # The Phase-8 WRITE gate at
    # ``claude_mcp_servers/weaviate_mcp/server.py::store_knowledge_node``
    # (and the post-file-edit.{sh,ps1} hooks) read this env var; without
    # it the gate's empty-PID branch fires (silent allow + deferral +
    # dropped_writes.jsonl metric). project_id is non-empty here because
    # the caller (``project_env_from_db``) is keyed by project_id — see
    # ``_apply_standalone_env`` for the DB-less path that intentionally
    # OMITS this key.
    _set("VCT_PROJECT_ID", project_id)
    # Boolean → "true"/"false" (lowercase, matching Rust's
    # `shared_kg_write_disabled_str()` -> bool::to_string()).
    _set("SHARED_KG_WRITE_DISABLED", "true" if shared_kg_write_disabled else "false")
    # Legacy alias — same value, kept for ~3 releases (target 2026-08).
    _set("SHARED_KG_OPT_OUT", "true" if shared_kg_write_disabled else "false")
    # v0.2.46 Decision B — symmetric read gate. No legacy alias because
    # the read path was unconditional pre-v0.2.46.
    _set("SHARED_KG_READ_DISABLED", "true" if shared_kg_read_disabled else "false")
    _set("PROJECT_NAME", proj.name)
    _set("CODE_GRAPH_PROJECT", sanitized)
    _set("ACTIVE_EMBEDDING", active_embedding)
    _set("WEAVIATE_URL", weaviate_url)
    _set("WEAVIATE_PORT", str(weaviate_port_default))
    _set("OLLAMA_URL", ollama_url)
    _set("OLLAMA_PORT", str(ollama_port_default))
    _set("CODE_EMBED_URL", code_embed_url)
    _set("CODE_EMBED_PORT", str(code_embed_port_default))

    if orchestrator_root is not None:
        # Use forward slashes on POSIX, backslashes on Windows — matches
        # what Rust's `Path::display()` produces.
        _set("VCT_ORCHESTRATOR_ROOT", str(orchestrator_root))
        _set(
            "VCT_INFRASTRUCTURE_DIR",
            str(orchestrator_root / "infrastructure"),
        )
        # v0.2.37 (Gap 6a): legacy alias for VCT_ORCHESTRATOR_ROOT —
        # consumed by `templates/scripts/code-graph-analyze` (probes
        # ``$VCT_INSTALL_ROOT/.venv`` before script-relative paths).
        # Same value; the legacy alias kept until the wrappers fully
        # migrate to VCT_ORCHESTRATOR_ROOT.
        _set("VCT_INSTALL_ROOT", str(orchestrator_root))

    if kg_access:
        _set("VCT_KG_ACCESS_LIST", ",".join(kg_access))
    if code_graph_access:
        _set("VCT_CODE_GRAPH_ACCESS_LIST", ",".join(code_graph_access))
    if diagram_access:
        # v0.2.34 A7. CSV of grantor project NAMES (not slugs); the MCP
        # sanitises + appends `_Diagrams`. Conditionally emitted: omitted
        # when no peers granted diagram read — matches the access-list
        # omit semantics used for VCT_KG_ACCESS_LIST and
        # VCT_CODE_GRAPH_ACCESS_LIST (signal-to-remove on apply).
        _set("VCT_DIAGRAMS_ACCESS_LIST", ",".join(diagram_access))

    # GITHUB_TOKEN intentionally NOT resolved here. The Rust resolver
    # pulls it from the OS keychain with active-flag gating; replicating
    # that lifecycle from Python would require a keychain bridge that
    # doesn't exist yet. Production callers that need GITHUB_TOKEN
    # should pass it as a future explicit kwarg; today the keychain
    # path stays Rust-owned and config_projection emits no value for
    # this key (matching the "keychain empty / paused" omit behaviour
    # the Rust resolver already documents).

    return {
        "canonical_env": env,
        "project_id": project_id,
        "project_root": Path(proj.folder_path),
    }


# ─── apply_project_env ──────────────────────────────────────────────────


_SURFACE_CLAUDE_SETTINGS = "claude_settings_json"
_SURFACE_CLAUDE_ENV = "claude_env"
_SURFACE_VSCODE_SETTINGS = "vscode_settings_json"

_DEFAULT_SURFACES: tuple[str, ...] = (
    _SURFACE_CLAUDE_SETTINGS,
    _SURFACE_CLAUDE_ENV,
)
_ALL_SURFACES: tuple[str, ...] = (
    _SURFACE_CLAUDE_SETTINGS,
    _SURFACE_CLAUDE_ENV,
    _SURFACE_VSCODE_SETTINGS,
)


def apply_project_env(
    bundle: ProjectEnvBundle,
    *,
    surfaces: Iterable[str] | None = None,
    user_secret_bundle: UserSecretBundle | None = None,
) -> dict[str, list[str]]:
    """Project ``bundle`` to the requested env surfaces.

    Args:
        bundle: As returned by :func:`project_env_from_db`.
        surfaces: Sequence of surface names. Defaults to
            ``("claude_settings_json", "claude_env")`` — matching the
            production Rust writer ``write_project_env_files`` after
            PR-27 (v0.2.12, 2026-05-16) removed the historical
            ``.vscode/settings.json`` write. Pass
            ``("claude_settings_json", "claude_env", "vscode_settings_json")``
            to also write the VS Code workspace surface (opt-in;
            useful for the diagrams flow where the VS Code extension's
            claude-code.env block is the only path to the embedded
            editor).
        user_secret_bundle: Optional Phase 0.E user-secret payload.
            When provided, user-secret pairs are EMITTED to the same
            surfaces in the same write pass (atomic per surface),
            and the strip set is applied to the JSON env blocks
            before canonical updates. Production callers use
            :func:`apply_user_secrets` instead of threading this
            argument; this parameter exists so a single ``apply``
            CLI invocation can write canonical + secrets in one
            atomic-per-surface pass when both are stale.

    Returns:
        ``{surface_name: [keys_written, ...]}`` — a dict mapping each
        surface that was actually written to the canonical keys that
        landed there. Useful for audit logging and for the
        ``verify-env-projection`` CLI's round-trip test.

    Raises:
        ConfigProjectionError: any surface failed to write atomically.
            Other surfaces may have been written successfully before
            the failure — the function does NOT roll back across
            surfaces (each surface is independently atomic).
    """
    if surfaces is None:
        surfaces_seq: tuple[str, ...] = _DEFAULT_SURFACES
    else:
        surfaces_seq = tuple(surfaces)

    for s in surfaces_seq:
        if s not in _ALL_SURFACES:
            raise ConfigProjectionError(
                f"unknown surface {s!r}; valid: {sorted(_ALL_SURFACES)}"
            )

    project_root = bundle["project_root"]
    env = bundle["canonical_env"]
    canonical_keys = list_canonical_keys()

    # Phase 0.E: precompute the EMIT and STRIP sets from the optional
    # user-secret bundle. STRIP is "known - emit" (paused / deleted
    # keys that need to leave the JSON env blocks); EMIT is the
    # active pairs that need to land in every surface.
    us_pairs: list[tuple[str, str]] = []
    us_strip_keys: list[str] = []
    if user_secret_bundle is not None:
        us_pairs = list(user_secret_bundle["user_secret_pairs"])
        emit_set = {k for k, _ in us_pairs}
        us_strip_keys = [
            k for k in user_secret_bundle["user_secret_known_keys"]
            if k not in emit_set
        ]

    report: dict[str, list[str]] = {}

    if _SURFACE_CLAUDE_SETTINGS in surfaces_seq:
        path = project_root / ".claude" / "settings.json"
        keys = _write_json_env_block(
            path, env, canonical_keys, env_key="env",
            user_secret_pairs=us_pairs,
            user_secret_strip_keys=us_strip_keys,
        )
        report[_SURFACE_CLAUDE_SETTINGS] = keys

    if _SURFACE_CLAUDE_ENV in surfaces_seq:
        path = project_root / ".claude" / "env"
        keys = _write_shell_env_managed_block(
            path, env, user_secret_pairs=us_pairs,
        )
        report[_SURFACE_CLAUDE_ENV] = keys

    if _SURFACE_VSCODE_SETTINGS in surfaces_seq:
        path = project_root / ".vscode" / "settings.json"
        keys = _write_json_env_block(
            path, env, canonical_keys, env_key="claude-code.env",
            user_secret_pairs=us_pairs,
            user_secret_strip_keys=us_strip_keys,
        )
        report[_SURFACE_VSCODE_SETTINGS] = keys

    return report


# ─── Phase 0.E: user-secret writer ──────────────────────────────────────


def apply_user_secrets(
    secret_bundle: UserSecretBundle,
    *,
    surfaces: Iterable[str] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Project ``secret_bundle`` to the requested env surfaces (Phase 0.E).

    This is the user-secret sibling of :func:`apply_project_env`.
    The Rust caller resolves the keychain VALUES (via the existing
    ``commands::project_env_settings::resolve_user_secret_state``),
    serialises them into a :class:`UserSecretBundle`, and invokes
    this function — either in-process (Python tests) or via the CLI
    ``python -m vco_lib.config_projection apply-user-secrets``.

    Behaviour:

      * For each requested JSON surface (settings.json /
        vscode.settings.json):
          - STRIP every key in ``user_secret_known_keys`` that is NOT
            in ``user_secret_pairs`` from the existing ``env`` /
            ``claude-code.env`` sub-block. This is how paused /
            deleted secrets actually leave the surface.
          - EMIT every (KEY, VALUE) in ``user_secret_pairs`` into the
            sub-block. Order in the bundle is preserved insertion-wise.
          - Existing canonical keys and user-added-by-hand keys are
            PRESERVED (the writer does not touch them).
      * For ``.claude/env``:
          - Read the existing file (if any). The user-secret block
            REBUILD is wholesale — there's no in-place key edit on the
            shell surface; the BEGIN/END managed block is rewritten.
          - If the file has a managed block, the user-secret pairs
            land between the canonical exports (preserved) and the
            END marker. If there's no managed block at all, the
            entire managed block (including a placeholder empty
            canonical section) is written.

    The CALLER is responsible for resolving the strip set correctly.
    If they pass an empty ``user_secret_known_keys`` list, no STRIP
    pass runs — useful for test fixtures where the keychain state
    is known to be fresh. Production callers SHOULD pass the result
    of :func:`user_secret_known_keys_from_db` to drive the STRIP
    pass off the launcher DB.

    Why we re-read the canonical env from disk rather than passing it
    in: this function is invoked by the secret-mutation hot path
    (``set_secret_v2`` / ``clear_secret_v2`` → env-refresh hook); the
    canonical env is already on disk from the last
    :func:`apply_project_env` call. Re-reading is cheaper than
    re-resolving from the DB on every secret mutation, and it
    preserves whatever the canonical writer last produced byte-for-
    byte (no risk of the user-secret writer accidentally regenerating
    canonical bytes that drift from the Rust authority).

    Returns a two-level audit report::

        {
          "claude_settings_json": {
            "emitted": ["GITHUB_TOKEN", ...],
            "stripped": ["OLD_PAUSED_KEY", ...],
          },
          ...
        }

    Raises:
        ConfigProjectionError: an unknown surface was passed.

    Cross-OS: same atomic-write discipline as
    :func:`apply_project_env`. The tempfile lands in the target
    directory; ``os.replace`` is invariant across POSIX and
    Windows 10+.
    """
    if surfaces is None:
        surfaces_seq: tuple[str, ...] = _DEFAULT_SURFACES
    else:
        surfaces_seq = tuple(surfaces)

    for s in surfaces_seq:
        if s not in _ALL_SURFACES:
            raise ConfigProjectionError(
                f"unknown surface {s!r}; valid: {sorted(_ALL_SURFACES)}"
            )

    project_root = secret_bundle["project_root"]
    pairs: list[tuple[str, str]] = list(secret_bundle["user_secret_pairs"])
    known: list[str] = list(secret_bundle["user_secret_known_keys"])
    emit_set = {k for k, _ in pairs}
    strip_keys = [k for k in known if k not in emit_set]

    # User-secret keys aren't canonical, so the json writer's
    # signal-to-remove path doesn't run for them — we drive STRIP
    # explicitly via the strip_keys argument.
    report: dict[str, dict[str, list[str]]] = {}

    if _SURFACE_CLAUDE_SETTINGS in surfaces_seq:
        path = project_root / ".claude" / "settings.json"
        _, emitted, stripped = _user_secret_apply_json(
            path, pairs, strip_keys, env_key="env",
        )
        report[_SURFACE_CLAUDE_SETTINGS] = {
            "emitted": emitted, "stripped": stripped,
        }

    if _SURFACE_CLAUDE_ENV in surfaces_seq:
        path = project_root / ".claude" / "env"
        emitted = _user_secret_apply_claude_env(path, pairs)
        report[_SURFACE_CLAUDE_ENV] = {
            "emitted": emitted,
            # .claude/env strip is implicit (BEGIN/END replace rebuilds
            # the entire block); we surface the OBSERVED-removed list
            # as the strip-set keys for parity with the JSON report.
            "stripped": sorted(strip_keys),
        }

    if _SURFACE_VSCODE_SETTINGS in surfaces_seq:
        path = project_root / ".vscode" / "settings.json"
        _, emitted, stripped = _user_secret_apply_json(
            path, pairs, strip_keys, env_key="claude-code.env",
        )
        report[_SURFACE_VSCODE_SETTINGS] = {
            "emitted": emitted, "stripped": stripped,
        }

    return report


def _user_secret_apply_json(
    path: Path,
    pairs: list[tuple[str, str]],
    strip_keys: list[str],
    *,
    env_key: str,
) -> tuple[bool, list[str], list[str]]:
    """Apply user-secret EMIT/STRIP to a JSON env sub-block.

    Surgical: only touches the user-secret keys. Canonical and user-
    added-by-hand keys survive verbatim. The file is created (with
    just the env sub-block) if it doesn't exist, mirroring the
    canonical writer's "fresh file" semantics.

    Returns:
        Tuple of (file_existed, emitted_keys, stripped_keys). The
        file_existed flag is for audit logging — useful to detect
        "user-secret write created the file from scratch" which
        usually means a canonical write hasn't run yet (a Rust-side
        ordering bug worth surfacing).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    file_existed = path.exists()
    existing_root: Any = {}
    if file_existed:
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing_root = parsed
            else:
                existing_root = {}
        except (OSError, json.JSONDecodeError):
            existing_root = {}

    env_block_raw = existing_root.get(env_key)
    if not isinstance(env_block_raw, dict):
        env_block: dict[str, Any] = {}
    else:
        env_block = dict(env_block_raw)

    # STRIP first.
    stripped: list[str] = []
    for k in strip_keys:
        if k in env_block:
            del env_block[k]
            stripped.append(k)

    # EMIT (user-wins on hypothetical collision).
    emitted: list[str] = []
    for k, v in pairs:
        env_block[k] = v
        emitted.append(k)

    existing_root[env_key] = env_block

    serialised = json.dumps(existing_root, indent=2, ensure_ascii=False)
    _atomic_write_text(path, serialised)

    emitted.sort()
    stripped.sort()
    return file_existed, emitted, stripped


def _user_secret_apply_claude_env(
    path: Path,
    pairs: list[tuple[str, str]],
) -> list[str]:
    """Apply user-secret EMIT/STRIP to ``.claude/env``.

    Re-reads the existing managed block, extracts the canonical
    section (everything before the user-secret section header OR
    before END if no header), rebuilds with the new user-secret
    pairs spliced in after canonical and before END. Lines outside
    the BEGIN/END markers are preserved verbatim.

    If the file doesn't exist OR has no managed block: writes a fresh
    managed block with NO canonical content + the user-secret
    section. The next :func:`apply_project_env` call will re-emit
    canonical content; for the interim window the file has the
    correct user-secret state but no canonical state. This matches
    the Rust writer's behaviour when ``set_secret_v2`` runs against
    a project that hasn't had ``write_project_env_files`` invoked
    yet (rare; should only happen on first-secret-before-first-
    refresh).

    STRIP is implicit: the entire managed block is rebuilt; paused /
    deleted secrets simply don't appear in the new pairs list.

    Returns the sorted list of KEYS emitted.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    prior: Optional[str]
    if path.exists():
        try:
            prior = path.read_text(encoding="utf-8")
        except OSError:
            prior = None
    else:
        prior = None

    # Extract the canonical lines from the existing managed block (if
    # any) so we can preserve them across user-secret-only writes.
    # The canonical exports look like `export KEY="value"` — we
    # capture every line between BEGIN and either the user-secret
    # section header OR the END marker.
    canonical_lines: list[str] = []
    if prior is not None:
        begin_idx = prior.find(CLAUDE_ENV_MANAGED_BEGIN)
        if begin_idx != -1:
            end_off = prior[begin_idx:].find(CLAUDE_ENV_MANAGED_END)
            if end_off != -1:
                block_text = prior[begin_idx:begin_idx + end_off]
                # Split into lines; drop the BEGIN line (first one).
                lines = block_text.splitlines()[1:]
                user_secret_header = (
                    "# user secrets (per-project; "
                    "managed via launcher GUI Secrets panel)"
                )
                for line in lines:
                    # Stop at the user-secret section header — everything
                    # after it is the OLD user-secret block we're
                    # rebuilding.
                    if line == user_secret_header:
                        # Drop the preceding blank line if there is one;
                        # the rebuild re-emits it. The canonical exports
                        # are everything before the blank line that
                        # precedes the header.
                        if canonical_lines and canonical_lines[-1] == "":
                            canonical_lines.pop()
                        break
                    canonical_lines.append(line)

    # Build the new managed block. We need to splice user-secret pairs
    # between the canonical content and the END marker.
    out: list[str] = [CLAUDE_ENV_MANAGED_BEGIN]
    out.extend(canonical_lines)
    if pairs:
        out.append("")
        out.append(
            "# user secrets (per-project; managed via launcher GUI Secrets panel)"
        )
        for k, v in pairs:
            escaped = v.replace('"', '\\"')
            out.append(f'export {k}="{escaped}"')
    out.append(CLAUDE_ENV_MANAGED_END)
    managed = "\n".join(out) + "\n"

    new_text = _merge_managed_block(prior, managed)
    _atomic_write_text(path, new_text)

    return sorted(k for k, _ in pairs)


# ─── Surface writers ────────────────────────────────────────────────────


def _write_json_env_block(
    path: Path,
    canonical_env: Mapping[str, str],
    canonical_keys: set[str],
    *,
    env_key: str,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
    user_secret_strip_keys: Iterable[str] | None = None,
) -> list[str]:
    """Write the canonical env into a JSON file's ``<env_key>`` sub-block.

    Deep-merge contract (mirrors Rust's
    ``merge_env_object_canonical_with_user_secrets``):

      * Read the existing JSON (if present). Treat missing file as
        ``{}``. Treat malformed JSON or non-object root as ``{}``
        (matching the Rust fallback at projects_v2.rs lines 1881-1883).
      * Locate the ``env_key`` sub-block. If missing or not an object,
        create a fresh object.
      * For each canonical key in ``canonical_keys``:
          - if present in ``canonical_env``: set ``env[key] = value``
            (string).
          - if absent from ``canonical_env``: delete ``env[key]`` if
            present (signal-to-remove semantics; supports "the launcher
            decided this project no longer has any peer KG access").
      * Phase 0.E user-secret handling (mirrors Rust):
          - STRIP first: ``user_secret_strip_keys`` are removed from
            ``env_block`` BEFORE inserting active pairs. Run before
            canonical so a same-tick toggle (active→inactive across
            two writes) can't rely on residual state. The strip set
            is by construction disjoint from the emit set (the resolver
            computes ``strip = known - emit``), so removing-then-
            inserting is safe.
          - EMIT last: ``user_secret_pairs`` are inserted after the
            canonical keys. A hypothetical KEY collision (which
            ``set_secret_v2`` prevents at the GUI layer) resolves
            user-wins, matching the Rust ordering at
            ``merge_env_object_canonical_with_user_secrets`` step 3.
      * Non-canonical, non-user-secret keys (user-added by hand
        directly in the JSON) are PRESERVED untouched.
      * Write the result back with 2-space indent, no trailing newline,
        ``ensure_ascii=False`` (matching Rust's
        ``serde_json::to_string_pretty`` byte layout).

    Returns the sorted list of canonical keys whose value was set
    (those that were deleted are not listed — the audit consumer wants
    to see what's NOW exported, not what was previously there). User-
    secret keys are NOT in the returned list (audit reporting for
    user secrets uses a separate report channel; see
    :func:`apply_user_secrets`).

    Atomic write: writes to a tempfile in the same directory as ``path``,
    then ``os.replace``-s into place. This is atomic on POSIX (rename
    on the same filesystem) and on Windows 10+ (NTFS rename is
    transactional for same-volume).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Read-merge-write.
    existing_root: Any = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing_root = parsed
            else:
                # Non-object root (someone hand-edited it into an array
                # or string) — reset to empty object, matching Rust's
                # fallback at projects_v2.rs L1881-1883.
                existing_root = {}
        except (OSError, json.JSONDecodeError):
            # Unreadable / malformed — start fresh. Matches Rust which
            # logs a warning + falls back to {}.
            existing_root = {}

    env_block_raw = existing_root.get(env_key)
    if not isinstance(env_block_raw, dict):
        env_block: dict[str, Any] = {}
    else:
        env_block = dict(env_block_raw)  # defensive copy

    # Phase 0.E step 1: STRIP paused / removed user-secret keys.
    # Matches Rust's `merge_env_object_canonical_with_user_secrets`
    # step 1: remove BEFORE canonical / emit so a buggy resolver
    # can't silently drop the active value.
    if user_secret_strip_keys:
        for k in user_secret_strip_keys:
            env_block.pop(k, None)

    # Phase 0.B step 2: canonical updates (overwrite + signal-to-remove).
    written_keys: list[str] = []
    for key in canonical_keys:
        if key in canonical_env:
            env_block[key] = canonical_env[key]
            written_keys.append(key)
        elif key in env_block:
            # Canonical key absent from bundle → remove from surface.
            # This is the "no peers granted" / "launcher outside git
            # checkout" case where the env var should disappear.
            del env_block[key]
        # else: not in bundle, not in surface — nothing to do.

    # Phase 0.E step 3: EMIT active user-secret pairs LAST so they
    # win on a hypothetical KEY collision with a canonical key.
    if user_secret_pairs:
        for k, v in user_secret_pairs:
            env_block[k] = v

    existing_root[env_key] = env_block

    # Atomic write. ``ensure_ascii=False`` matches Rust's serde_json
    # which passes Unicode through verbatim. 2-space indent matches
    # ``serde_json::to_string_pretty``. No trailing newline matches
    # Rust's ``std::fs::write(&path, pretty)`` where ``pretty`` has
    # no trailing newline (verified by reading projects_v2.rs L1896-1898).
    serialised = json.dumps(
        existing_root, indent=2, ensure_ascii=False
    )
    _atomic_write_text(path, serialised)

    written_keys.sort()
    return written_keys


def _write_shell_env_managed_block(
    path: Path,
    canonical_env: Mapping[str, str],
    *,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
) -> list[str]:
    """Write the canonical env between bracket markers in ``.claude/env``.

    Behaviour (mirrors Rust's
    ``merge_claude_env_managed_block`` + ``build_claude_env_managed_block``):

      * If the file doesn't exist: create it with just the managed block.
      * If the file exists and contains :data:`CLAUDE_ENV_MANAGED_BEGIN`:
        find the segment from BEGIN through END (inclusive) and replace
        it. Preserve content outside the markers verbatim.
      * If the file exists but does NOT contain the BEGIN marker (legacy
        wholesale-write or hand-edited): append the managed block at
        EOF, preserving prior content. On the next round-trip the
        BEGIN marker will be present and in-place replace kicks in.

    Order of canonical lines matches the order in ``_CANONICAL_KEYS``
    (insertion order of the bundle's dict, which the caller built from
    ``_CANONICAL_KEYS`` in :func:`project_env_from_db`).

    Phase 0.E (2026-05-25): when ``user_secret_pairs`` is non-empty,
    the user-secret exports land AFTER the canonical block, preceded
    by a blank line + ``# user secrets (per-project; managed via
    launcher GUI Secrets panel)`` section header — byte-identical to
    Rust's ``build_claude_env_managed_block_with_user_secrets``.

    STRIP for ``.claude/env`` is IMPLICIT: the entire BEGIN/END block
    is replaced on every write, so a paused / removed user secret
    simply doesn't appear in the new block — no explicit strip-set
    plumbing needed (contrast with the JSON env writers, where the
    deep-merge is additive).

    Atomic write: same tempfile + ``os.replace`` discipline as the JSON
    surfaces.

    Returns the sorted list of canonical keys written. Keys absent from
    ``canonical_env`` (the bundle decided to omit them) are simply not
    rendered. User-secret keys are NOT in the returned list.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    prior: Optional[str]
    if path.exists():
        try:
            prior = path.read_text(encoding="utf-8")
        except OSError:
            prior = None
    else:
        prior = None

    managed = _build_managed_block(canonical_env, user_secret_pairs=user_secret_pairs)
    new_text = _merge_managed_block(prior, managed)
    _atomic_write_text(path, new_text)

    return sorted(canonical_env.keys())


def _build_managed_block(
    canonical_env: Mapping[str, str],
    *,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
) -> str:
    """Render the managed block for ``.claude/env``.

    Format (byte-identical to Rust's
    ``build_claude_env_managed_block_with_user_secrets``):

      ``# vco-managed-begin\\n``
      ``<header comments — 11 lines>\\n``
      ``export KEY1="value1"\\n``
      ...
      [optional, when user_secret_pairs is non-empty:]
      ``\\n``
      ``# user secrets (per-project; managed via launcher GUI Secrets panel)\\n``
      ``export SECRET_KEY1="secret_value1"\\n``
      ...
      ``# vco-managed-end\\n``

    Embedded double-quotes in values are backslash-escaped (rare on
    POSIX; legitimate on Windows + git-bash paths). The Rust writer
    only escapes ``"`` — we mirror that.

    Phase 0.E (2026-05-25): user-secret exports land BETWEEN the
    canonical block and the END marker, preceded by a blank line +
    section header for diff readability. This block is byte-identical
    to Rust's ``build_claude_env_managed_block_with_user_secrets``.
    Paused / removed secrets are simply absent from this list (the
    BEGIN/END replace strips them implicitly).
    """
    out: list[str] = [CLAUDE_ENV_MANAGED_BEGIN]
    out.append(
        "# Auto-generated by VCT Launcher. Source from your shell rc or use"
    )
    out.append(
        "# tools/claude wrapper (which auto-sources this file before exec'ing"
    )
    out.append(
        "# the real claude binary). Lines OUTSIDE this BEGIN/END block are"
    )
    out.append("# preserved across re-runs — add custom exports there.")
    out.append(
        "# Asymmetric shared-KG access (2026-05-01): reads always-on; this"
    )
    out.append("# gates WRITES only. SHARED_KG_OPT_OUT is the legacy alias kept")
    out.append("# for ~3 releases (target removal: 2026-08).")
    out.append(
        "# Portability keys VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR"
    )
    out.append(
        "# (when present) point at the orchestrator clone + its infrastructure/"
    )
    out.append(
        "# dir; consumed by .claude/hooks/ensure-containers.sh and the"
    )
    out.append(
        "# bundled Python scripts that need the claude_mcp_servers/ package."
    )
    # Iterate canonical_env in its existing (insertion) order — matches
    # the order project_env_from_db emitted, which mirrors _CANONICAL_KEYS.
    for key, value in canonical_env.items():
        escaped = value.replace('"', '\\"')
        out.append(f'export {key}="{escaped}"')
    # Phase 0.E user-secret section. Materialise the iterable first so we
    # can branch on emptiness without consuming a one-shot iterator twice.
    pairs_list: list[tuple[str, str]] = (
        list(user_secret_pairs) if user_secret_pairs else []
    )
    if pairs_list:
        # Blank line separator + section header. Byte-identical to Rust
        # at projects_v2.rs L2429-2430.
        out.append("")
        out.append(
            "# user secrets (per-project; managed via launcher GUI Secrets panel)"
        )
        for k, v in pairs_list:
            escaped = v.replace('"', '\\"')
            out.append(f'export {k}="{escaped}"')
    out.append(CLAUDE_ENV_MANAGED_END)
    # Trailing newline AFTER the final marker — matches Rust which does
    # `out.push('\n')` after CLAUDE_ENV_MANAGED_END.
    return "\n".join(out) + "\n"


def _merge_managed_block(prior: Optional[str], managed: str) -> str:
    """Splice ``managed`` into ``prior`` between the bracket markers.

    Behaviour matches Rust's ``merge_claude_env_managed_block``:

      * ``prior is None``: return ``managed`` as-is.
      * ``prior`` lacks the BEGIN marker: append ``managed`` at EOF
        (ensuring a newline-separator if ``prior`` doesn't end with one).
      * ``prior`` has BEGIN: locate BEGIN, then locate END after it.
        Replace the segment from BEGIN to (END + len(END) + 1 newline)
        with the new managed block. Lines outside the markers are
        preserved byte-for-byte.

      * Edge case: BEGIN present but END missing (truncated managed
        block from a crash) → replace BEGIN-to-EOF with the new block.
        Matches Rust's fallback at projects_v2.rs L2138-2143.
    """
    if prior is None:
        return managed

    begin_idx = prior.find(CLAUDE_ENV_MANAGED_BEGIN)
    if begin_idx == -1:
        # Append managed at EOF with newline separator.
        if not prior.endswith("\n"):
            return prior + "\n" + managed
        return prior + managed

    # Find END after BEGIN.
    end_off = prior[begin_idx:].find(CLAUDE_ENV_MANAGED_END)
    if end_off == -1:
        # Truncated managed block — replace BEGIN→EOF.
        after_end = len(prior)
    else:
        after_end = begin_idx + end_off + len(CLAUDE_ENV_MANAGED_END)
        # Trim one trailing newline after END if present (avoids
        # accumulating blank lines on repeated calls).
        if after_end < len(prior) and prior[after_end] == "\n":
            after_end += 1

    return prior[:begin_idx] + managed + prior[after_end:]


# ─── Atomic write helper ────────────────────────────────────────────────


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Thin delegate to :func:`vco_lib.atomic.atomic_write_text` (v0.2.54
    Track J consolidation — this module, ``env_template``,
    ``deferral_report`` and ``cli/codegraph_diagram`` each carried a
    copy of the mkstemp + fsync + ``os.replace`` recipe). The name is
    kept: four internal call-sites use it and external code mirrors
    the ``env_template`` sibling.

    Byte-parity with the Rust writers is preserved: the shared helper
    opens the tempfile with ``newline=""`` (no translation — write-
    equivalent to the previous ``newline="\\n"``), so ``\\n`` in
    ``content`` lands verbatim as LF, matching Rust's
    ``std::fs::write`` which never CRLF-converts.
    """
    atomic_write_text(path, content)


# ─── CLI entry point ────────────────────────────────────────────────────


def _cli_apply(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection apply --project-id <id>``."""
    try:
        bundle = project_env_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
            orchestrator_root=(
                Path(args.orchestrator_root) if args.orchestrator_root else None
            ),
            weaviate_port_default=args.weaviate_port,
            ollama_port_default=args.ollama_port,
            code_embed_port_default=args.code_embed_port,
        )
    except ProjectNotFound as exc:
        print(json.dumps({"error": "project_not_found", "message": str(exc)}),
              file=sys.stderr)
        return 2
    except DbUnreachable as exc:
        print(json.dumps({"error": "db_unreachable", "message": str(exc)}),
              file=sys.stderr)
        return 3

    surfaces: Iterable[str] | None = None
    if args.surfaces:
        surfaces = tuple(args.surfaces.split(","))
    try:
        report = apply_project_env(bundle, surfaces=surfaces)
    except ConfigProjectionError as exc:
        print(json.dumps({"error": "apply_failed", "message": str(exc)}),
              file=sys.stderr)
        return 4

    print(json.dumps({"ok": True, "report": report, "project_id": args.project_id,
                      "project_root": str(bundle["project_root"])}))
    return 0


def _cli_list_keys(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection list-keys --json``."""
    keys = sorted(list_canonical_keys())
    if args.json:
        print(json.dumps(keys))
    else:
        for k in keys:
            print(k)
    return 0


def _cli_apply_user_secrets(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection apply-user-secrets``.

    Phase 0.E (2026-05-25). Rust callers spawn this verb to write
    user-secret pairs into the env surfaces after a ``set_secret_v2``
    / ``clear_secret_v2`` mutation. The keychain values are resolved
    Rust-side (no Python keychain bridge) and passed in via
    ``--pairs-json``; the strip set is resolved from the launcher DB
    via :func:`user_secret_known_keys_from_db`.

    Exit codes (parallel to ``apply``):
        0 — success
        2 — project_not_found
        3 — db_unreachable
        4 — apply_failed (surface write error)
        5 — pairs_json_invalid (cannot parse the input)
    """
    # 1. Resolve project folder (we need this for the write target).
    try:
        project_folder = resolve_project_folder(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
        )
    except LookupError as exc:
        print(
            json.dumps({"error": "project_not_found", "message": str(exc)}),
            file=sys.stderr,
        )
        return 2
    except DbUnreachable as exc:
        print(
            json.dumps({"error": "db_unreachable", "message": str(exc)}),
            file=sys.stderr,
        )
        return 3

    # 2. Resolve the strip set from the launcher DB.
    try:
        known_keys = user_secret_known_keys_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
        )
    except DbUnreachable as exc:
        print(
            json.dumps({"error": "db_unreachable", "message": str(exc)}),
            file=sys.stderr,
        )
        return 3

    # 3. Parse the input pairs. The Rust caller writes a JSON file
    # (rather than passing on argv) so very long secret values don't
    # hit ARG_MAX on Linux. The file is a list of [KEY, VALUE]
    # arrays so order is preserved (a JSON object would lose order
    # on some parsers).
    pairs: list[tuple[str, str]] = []
    if args.pairs_json:
        try:
            raw = Path(args.pairs_json).read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                json.dumps({
                    "error": "pairs_json_invalid",
                    "message": f"could not read {args.pairs_json}: {exc}",
                }),
                file=sys.stderr,
            )
            return 5
        if not isinstance(parsed, list):
            print(
                json.dumps({
                    "error": "pairs_json_invalid",
                    "message": "pairs_json must be a JSON array of [key, value] pairs",
                }),
                file=sys.stderr,
            )
            return 5
        for entry in parsed:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], str)
            ):
                print(
                    json.dumps({
                        "error": "pairs_json_invalid",
                        "message": "each pair must be a [string, string] array",
                    }),
                    file=sys.stderr,
                )
                return 5
            pairs.append((entry[0], entry[1]))

    secret_bundle: UserSecretBundle = {
        "user_secret_pairs": pairs,
        "user_secret_known_keys": known_keys,
        "project_id": args.project_id,
        "project_root": project_folder,
    }

    surfaces: Iterable[str] | None = None
    if args.surfaces:
        surfaces = tuple(args.surfaces.split(","))

    try:
        report = apply_user_secrets(secret_bundle, surfaces=surfaces)
    except ConfigProjectionError as exc:
        print(
            json.dumps({"error": "apply_failed", "message": str(exc)}),
            file=sys.stderr,
        )
        return 4

    # codeql[py/clear-text-logging-sensitive-data]: false positive —
    # `known_keys` is a list of env-var NAME strings (e.g. "GITHUB_TOKEN"),
    # not their values. It is the "strip set" used to redact secrets from
    # the projection output. No secret values are printed here.
    print(json.dumps({
        "ok": True,
        "report": report,
        "project_id": args.project_id,
        "project_root": str(project_folder),
        "known_keys": known_keys,
    }))
    return 0


def _cli_user_secret_known_keys(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection user-secret-known-keys
    --project-id <id>``.

    Print the STRIP set (every user-bucket KEY observed across the
    three buckets) without applying. Useful for the Rust caller to
    verify the bridge before invoking ``apply-user-secrets`` (e.g.
    in CI parity checks).
    """
    try:
        keys = user_secret_known_keys_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
        )
    except DbUnreachable as exc:
        print(
            json.dumps({"error": "db_unreachable", "message": str(exc)}),
            file=sys.stderr,
        )
        return 3

    if args.json:
        # codeql[py/clear-text-logging-sensitive-data]: false positive —
        # `keys` is a list[str] of env-var NAMES, not values. The
        # `user-secret-known-keys` subcommand's entire purpose is to print
        # these identifiers so the Rust caller can verify the bridge.
        print(json.dumps(keys))
    else:
        for k in keys:
            # codeql[py/clear-text-logging-sensitive-data]: false positive —
            # `k` is a key NAME string (e.g. "GITHUB_TOKEN"), not its value.
            print(k)
    return 0


def _cli_from_db(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection from-db --project-id <id>``.

    Resolve the bundle and print it as JSON without writing anything.
    Useful for the future ``vco verify-env-projection`` round-trip
    test, and for debugging.
    """
    try:
        bundle = project_env_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
            orchestrator_root=(
                Path(args.orchestrator_root) if args.orchestrator_root else None
            ),
        )
    except ProjectNotFound as exc:
        print(json.dumps({"error": "project_not_found", "message": str(exc)}),
              file=sys.stderr)
        return 2
    except DbUnreachable as exc:
        print(json.dumps({"error": "db_unreachable", "message": str(exc)}),
              file=sys.stderr)
        return 3

    out = {
        "project_id": bundle["project_id"],
        "project_root": str(bundle["project_root"]),
        "canonical_env": bundle["canonical_env"],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.config_projection",
        description="DB-as-source-of-truth contract for per-project env projection.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser(
        "apply",
        help="resolve env from launcher DB and write to project's env surfaces",
    )
    p_apply.add_argument("--project-id", required=True)
    p_apply.add_argument(
        "--db-path", default=None,
        help="override launcher DB path (defaults to ~/.vct/launcher.db)",
    )
    p_apply.add_argument(
        "--surfaces", default=None,
        help="comma-separated subset of "
             "claude_settings_json,claude_env,vscode_settings_json "
             "(default: claude_settings_json,claude_env)",
    )
    p_apply.add_argument(
        "--orchestrator-root", default=None,
        help="path to orchestrator clone (emits VCT_ORCHESTRATOR_ROOT etc.)",
    )
    p_apply.add_argument("--weaviate-port", type=int, default=8081)
    p_apply.add_argument("--ollama-port", type=int, default=11435)
    p_apply.add_argument("--code-embed-port", type=int, default=11440)
    p_apply.set_defaults(handler=_cli_apply)

    p_list = sub.add_parser(
        "list-keys",
        help="print the canonical key set (for CI lint and audits)",
    )
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(handler=_cli_list_keys)

    p_from = sub.add_parser(
        "from-db",
        help="resolve and print the env bundle as JSON (no writes)",
    )
    p_from.add_argument("--project-id", required=True)
    p_from.add_argument("--db-path", default=None)
    p_from.add_argument("--orchestrator-root", default=None)
    p_from.set_defaults(handler=_cli_from_db)

    # Phase 0.E (2026-05-25).
    p_us_apply = sub.add_parser(
        "apply-user-secrets",
        help="write user-bucket secret pairs into the env surfaces (Phase 0.E)",
    )
    p_us_apply.add_argument("--project-id", required=True)
    p_us_apply.add_argument(
        "--db-path", default=None,
        help="override launcher DB path (defaults to ~/.vct/launcher.db)",
    )
    p_us_apply.add_argument(
        "--pairs-json", default=None,
        help="path to a JSON file containing a list of [key, value] pairs. "
             "Omit / empty file = treat input as empty (drives STRIP-only "
             "behaviour, useful for purge flows).",
    )
    p_us_apply.add_argument(
        "--surfaces", default=None,
        help="comma-separated subset of "
             "claude_settings_json,claude_env,vscode_settings_json "
             "(default: claude_settings_json,claude_env)",
    )
    p_us_apply.set_defaults(handler=_cli_apply_user_secrets)

    p_us_known = sub.add_parser(
        "user-secret-known-keys",
        help="print the STRIP set (every user-bucket KEY observed in DB)",
    )
    p_us_known.add_argument("--project-id", required=True)
    p_us_known.add_argument("--db-path", default=None)
    p_us_known.add_argument("--json", action="store_true")
    p_us_known.set_defaults(handler=_cli_user_secret_known_keys)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLAUDE_ENV_MANAGED_BEGIN",
    "CLAUDE_ENV_MANAGED_END",
    "ConfigProjectionError",
    "DbUnreachable",
    "ProjectEnvBundle",
    "ProjectNotFound",
    "UserSecretBundle",
    "apply_project_env",
    "apply_user_secrets",
    "list_canonical_keys",
    "list_registered_projects",
    "project_env_from_db",
    "resolve_project_folder",
    "user_secret_known_keys_from_db",
]
