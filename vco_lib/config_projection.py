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

  * The Rust env writer (the dominant writer until Phase 0.B Part 2;
    retired in v0.2.97 — this module is now the only one).
  * Rust ``ensure_project_env_template`` (the ``.env`` template — sibling
    surface, NOT in scope for this contract; see Out of scope below).
  * Python ``install.py`` backfill helpers (removed v0.2.92 — superseded by the config-projection single writer) (``_backfill_kg_collection_env_in_project``
    and friends) that scribble missing canonical keys when ``install-bundle
    --update`` runs against an older project.
  * Per-grant-change Tauri commands that called the Rust env writer
    directly (e.g. ``kg_set_collection_access_mode``).

Each path had its own opinion about what to write and how to merge. Bug-4
of the install-flow architectural overhaul (PR-145, 2026-05-06) was
specifically a wholesale-replace of the ``env`` sub-block in
``.claude/settings.json`` that silently dropped user-added keys. The
fix was a deep-merge in the Rust env writer, but other writers
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
     (the ``vco_lib.project_init`` subcommands ``derive`` and
     ``migrate-collections``); a fourth CLI verb does not add
     infrastructure.

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
    python -m vco_lib.config_projection from-db --project-id <uuid>   # always JSON

Out of scope
~~~~~~~~~~~~

* The ``<project_root>/.env`` file. It has different rules (a
  VCO-managed block inside a human-edited file, commented placeholders)
  and a different audience (CLI users who edit it by hand), so it has
  its own contract: :mod:`vco_lib.env_template` (Phase 0.D) — since
  v0.2.97 that file's only writer, reached from the launcher through
  ``python -m vco_lib.env_template apply`` and from ``install.py``
  through :mod:`vco_lib.install_env`.
* Adding new canonical env keys. This module ROUTES existing keys
  through one contract; widening the canonical key set is a separate
  governance step that must update both the Rust ``CANONICAL_INSTALL_ENV_KEYS``
  const and :func:`list_canonical_keys` here.
* User-bucket secret VALUES (keychain-resident). VCO never writes one
  into a project. The DB-side resolver
  :func:`user_secret_known_keys_from_db` lists every KEY ever observed
  across the three buckets (per_project, shared, global); every
  :func:`apply_project_env` removes an in-tree value of one of them
  ONLY when it equals the launcher's stored value (value evidence —
  see "User-secret values in the tree" below).

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
produce output BYTE-IDENTICAL to what the (since-retired, v0.2.97) Rust
env writer produced for the same input. This is the
regression-proof acceptance criterion of Phase 0.B and is tested by
``tests/test_config_projection_byte_identical.py`` (parity guard).
Divergences caught by the parity test must be fixed by changing the
Python side (Rust is the source of truth for byte layout until the
follow-up PR that flips production callers).

User-secret values in the tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Phase 0.E (2026-05-25) shipped an ``apply-user-secrets`` verb that could
write user-secret ``(KEY, VALUE)`` pairs into the env surfaces; v0.2.73
abolished value-emission everywhere and v0.2.75 P3 cut the verb down to a
strip of every launcher-known key NAME. v0.2.97 retired that verb too — it
had no caller, and removing a value by NAME destroys a key the user typed.
It is SUPERSEDED by the evidence-gated scrub inside every
:func:`apply_project_env` (and the launcher's unregister, via
``strip-proven-secret-values``): a value is removed only when it equals the
launcher's stored value. The surviving contract is

  **VCO never writes secret values into the project tree** — pinned by
  ``tests/test_config_projection_byte_identical.py``, the grep-gate in
  ``tests/test_config_projection_user_secrets.py`` and the Rust
  ``secrets_cmd`` refresh tests.

  * Secret VALUES stay Rust/keychain-owned and are resolved at need
    through the hub / file-store / project-.env chain
    (``docs/VCT_SECRETS_PRIMITIVE.md``) — never projected into any file
    under the project folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NotRequired, Optional, TypedDict

from vco_lib import jsonc_edit, settings_refusal
from vco_lib import service_endpoints as _service_endpoints
from vco_lib.atomic import atomic_rewrite_text
# v0.2.92 W18 — the tri-state probe result. Imported from `weaviate_helpers`
# because that module is `vco_lib`'s dependency-free leaf (stdlib only), which
# is what makes it a safe home for a type every other module needs; the type
# itself is transport-agnostic and is used HERE for a pure sqlite read.
from vco_lib.weaviate_helpers import ProbeResult
from vco_lib.launcher_db_reader import (
    ACTIVE_EMBEDDING_SETTING_KEY,
    ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
    ACTIVE_EMBEDDING_SOURCE_USER,
    APP_STATE_KEY_ACTIVE_EMBEDDING,
    APP_STATE_KEY_DEFAULT_TEXT_EMBED,
    ORCHESTRATOR_CORE_MODULE_ID,
    profile_for_text_model,
    sqlite_ro_uri as _sqlite_ro_uri,
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
    # The Rust env writer never emitted DIAGRAMS_COLLECTION (it was
    # retired in v0.2.97; only this Python contract writes the key).
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
    # v0.2.71 T-B-flags — per-project dual-write + dual-log toggles.
    # Both sourced from ``module_settings`` (launcher.db as truth), default
    # OFF. The Rust ``CANONICAL_INSTALL_ENV_KEYS`` const does NOT include
    # these keys (same deliberate Python-canonical-writer split as
    # ``DIAGRAMS_COLLECTION`` above) — the Python ``apply`` CLI is the
    # canonical writer per the Option-A interop strategy at the top of this
    # module.
    #
    #   * ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` — consumed by
    #     ``vco_lib/embedding_service.py::_resolve_write_all_slots``
    #     (reads the env verbatim; its read path is unchanged). Before
    #     T-B-flags this was an env-only toggle the DB was unaware of; now
    #     the DB is the truth that POPULATES it.
    #   * ``DUAL_RL_LOG_ENABLED`` — consumed by
    #     ``claude_mcp_servers/weaviate_mcp/server.py::
    #     _resolve_dual_rl_log_enabled`` (T-C). Projecting it here CLOSES
    #     T-C's ``TODO(T-B-flags)`` — T-C reads the env, this writes it.
    "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
    "DUAL_RL_LOG_ENABLED",
    # v0.2.88 (DEFECT 5): the third dual-write flag joins the managed set so a
    # user's hand-set env value is reconciled to the DB truth on every update
    # (the projection wins), like its two siblings. Pre-fix it was env-only and
    # "survived" updates by being UNKNOWN here — luck, not design.
    "DUAL_EMBEDDING_ARCTIC_SECONDARY",
    "PROJECT_NAME",
    "CODE_GRAPH_PROJECT",
    "ACTIVE_EMBEDDING",
    # v0.2.72 T-FLOOR (P1) — code-graph two-stage floor overrides, sourced from
    # machine-global ``app_state`` (codegraph.retrieval_floor /
    # codegraph.post_rerank_floor, written by T-GUI-DB). Consumed by
    # ``claude_mcp_servers/weaviate_mcp/code_ranking.py::resolve_retrieval_floor``
    # / ``resolve_post_rerank_floor`` (canonical value + empty-string coercion +
    # per-slot default). Same deliberate Python-canonical-writer split as
    # DIAGRAMS_COLLECTION / DUAL_* above: the Rust ``CANONICAL_INSTALL_ENV_KEYS``
    # const does NOT (yet) include these; the Python ``apply`` CLI is the
    # canonical writer. Conditionally emitted — omitted when no app_state row.
    "VCO_CODE_GRAPH_RETRIEVAL_FLOOR",
    "VCO_CODE_GRAPH_POST_RERANK_FLOOR",
    # v0.2.73 Concern-A/C: GLOBAL leg of the two-level RL telemetry gate.
    # Conditionally emitted — omitted when no app_state row (absent global env →
    # resolver treats it as "not globally disabled"; the per-project .claude/env
    # flag still applies). Same Python-canonical-writer split as the floors above.
    "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
    "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
    # NOTE — ``VCO_LOG_LEVEL`` (v0.2.91 WP-L / decision #21) is deliberately
    # NOT a canonical key. It is the one projected value that must NOT beat
    # an operator's own environment, so it cannot ride this list:
    #
    #   * canonical keys are written into ``.claude/settings.json`` ``env``,
    #     a plain string→string map that STRUCTURALLY overwrites inherited
    #     environment for MCP subprocesses. There is no conditional syntax
    #     there, so projecting the level would make the GUI preference
    #     clobber the shell export of the person debugging that very MCP —
    #     an inversion of decision #21's ``env > app_state > INFO``;
    #   * canonical keys are written into ``.claude/env`` as hard
    #     ``export K="v"`` assignments, which clobber at ``source`` time.
    #
    # So the level takes the ``shell_defaulted_env`` path instead: emitted
    # ONLY into ``.claude/env``, and in the DEFAULTED form
    # ``export VCO_LOG_LEVEL="${VCO_LOG_LEVEL:-<stored>}"`` so an operator's
    # export still wins for hooks and their Python children. See
    # :data:`SHELL_DEFAULTED_ENV_KEYS`.
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
    # v0.2.97: the code-embed URL under the name its clients actually read.
    # (``CODE_EMBED_URL`` above is the alias the ONE client resolver —
    # ``vco_lib.code_embed_image.service_base_url`` — accepts after
    # ``CODE_EMBED_SERVICE_URL``, lane Y.) Python-only, like the
    # DIAGRAMS_COLLECTION / DUAL_* keys: the Rust CANONICAL_INSTALL_ENV_KEYS
    # does not list it, and the subset parity test allows that direction.
    "CODE_EMBED_SERVICE_URL",
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
    # A-8 (v0.2.73): KG_BASE_DIR — the project's folder path. Previously
    # Rust-only (emitted "always" by the legacy Rust env writer's
    # SecretsPanel path, retired v0.2.97) but ABSENT from this Python canonical set. Because
    # the Python ``apply`` rebuilds the managed block from scratch and drops
    # keys not in this set, KG_BASE_DIR would appear after a secrets toggle
    # and VANISH on the next Python apply (create/rename/refresh) — a
    # flapping config surface. Adding it here makes the Python contract emit
    # it on every apply, matching the Rust ``CANONICAL_INSTALL_ENV_KEYS``
    # (last entry) so the two lists agree and the parity test
    # (test_canonical_env_key_parity_v0273.py) passes. Value = folder path;
    # always emitted (never None).
    "KG_BASE_DIR",
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
# The ONE definition (v0.2.97 review R6 retired the Rust copies with the
# Rust splice): the writer below and the unregister's strip
# (:mod:`vco_lib.unregister_env`) both use these. The in-place replace on the
# next call depends on substring match.

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

    ``shell_defaulted_env`` (v0.2.91 WP-L, optional) carries the keys in
    :data:`SHELL_DEFAULTED_ENV_KEYS` — values that reach ONLY ``.claude/env``,
    and there in the ``export K="${K:-v}"`` form so the operator's own
    environment still wins. They are NOT canonical keys: they never touch the
    JSON surfaces, and their absence is not a signal to strip anything (the
    shell block is rebuilt wholesale, so a removed key simply stops being
    emitted). Absent or empty ⇒ nothing extra is written.

    ``user_secret_known_keys`` (v0.2.97, optional) — every KEY the launcher
    knows as a user secret for this project (per-project + shared + global
    buckets, :func:`_fetch_user_secret_known_keys`). Pre-v0.2.73 launchers
    wrote the VALUES of exactly these keys into the JSON env blocks; every
    :func:`apply_project_env` removes them again (the secrets stay in the
    keychain), which is what makes the ``user_secret_values_retained_in_tree``
    deferral's "the next refresh removes them" true.
    """

    canonical_env: dict[str, str]
    project_id: str
    project_root: Path
    shell_defaulted_env: NotRequired[dict[str, str]]
    user_secret_known_keys: NotRequired[list[str]]


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

# WP-Q item 3 (G6): the RL reranker paid-module id, keyed in ``module_ports``
# for the per-project RL serving port projection (RL_SERVER_PORT / RL_SERVER_URL).
_RL_RERANKER_MODULE_ID = "vct-rl-reranker"


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
    # v0.2.96 L-11: built by `launcher_db_reader.sqlite_ro_uri`, the ONE
    # home — a hand-interpolated path containing ?/#/% truncates at the
    # first ? and the open fails shut.
    conn = sqlite3.connect(
        _sqlite_ro_uri(db_path), uri=True, timeout=5.0
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


# ─── v0.2.72 T-FLOOR (P1): code-graph two-stage floor app_state keys ─────────
#
# Machine-global code-graph floor values, written by T-GUI-DB (the launcher's
# Codegraph settings surface) into ``app_state``. This module only PROJECTS
# them to the per-project env surfaces (.claude/settings.json + .claude/env) so
# the CLI + MCP + hooks all read the same VCO_CODE_GRAPH_* override the user
# picked in the GUI. The floor DEFAULTS + resolution live in
# ``claude_mcp_servers/weaviate_mcp/code_ranking.py`` — this projection only
# emits a value when the app_state row is present (soft-fail: absent → key
# OMITTED → the shared resolver falls back to its per-slot default).
#
# The env-key NAMES mirror ``code_ranking.py`` (_ENV_RETRIEVAL_FLOOR /
# _ENV_POST_RERANK_FLOOR) exactly — changing one requires changing the other.
APP_STATE_KEY_CODEGRAPH_RETRIEVAL_FLOOR = "codegraph.retrieval_floor"
APP_STATE_KEY_CODEGRAPH_POST_RERANK_FLOOR = "codegraph.post_rerank_floor"

# v0.2.73 Concern-A/C: machine-GLOBAL RL telemetry opt-outs. The GUI writes
# these ``app_state`` keys via the generic ``app_state_set_bool`` Tauri command
# ("true"/"false" strings); this projection emits them into every project's
# ``.claude/settings.json`` env as the GLOBAL leg of a two-level gate. The RL
# resolvers (``telemetry_writer._local_logging_disabled`` /
# ``_online_training_disabled``) OR the global env with the per-project
# ``.claude/env`` flag, so a GLOBAL disable overrides ALL projects while a
# global-enabled state still lets a single project opt out locally.
# ``app_state_key_triggers_env_reprojection`` (Rust) must list these keys so a
# GUI write refreshes every project's env.
#   app_state ``rl.local_logging_disabled_global``  → env RL_LOCAL_LOGGING_DISABLED_GLOBAL
#   app_state ``rl.online_training_disabled_global`` → env RL_ONLINE_TRAINING_DISABLED_GLOBAL
APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL = "rl.local_logging_disabled_global"
APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL = "rl.online_training_disabled_global"
_ENV_RL_LOCAL_LOGGING_DISABLED_GLOBAL = "RL_LOCAL_LOGGING_DISABLED_GLOBAL"
_ENV_RL_ONLINE_TRAINING_DISABLED_GLOBAL = "RL_ONLINE_TRAINING_DISABLED_GLOBAL"

# ─── v0.2.91 WP-L: dual-flag host-wide defaults + the log⟹write clamp ───
#
# MUST MATCH, byte-for-byte, the Rust home of this cascade:
# ``launcher/src-tauri/vct-launcher-core/src/db/settings.rs`` (the
# ``DualFlag`` addressing table, ``Db::resolve_dual_flags``, and
# ``Db::set_dual_flag_global_default``). The hub ``/config`` resolver
# (``vct-hub/src/config_api.rs``) calls the Rust function directly; THIS is
# the one mirror, because the projection runs in a separate Python process
# with no interpreter on the Rust side of the hot path (tier C of the A>B>C
# sharing rule). The lock is ``tests/test_dual_flags_cascade_parity_v0291.py``
# — it parses the Rust addressing table + truth table and drives this
# resolver through the identical cases.
#
# Precedence per flag:
#   1. explicit per-project ``module_settings`` row — wins IN BOTH
#      DIRECTIONS (an explicit ``false`` beats a host-wide ``true``);
#   2. host-wide ``app_state`` default (absent row ⇒ False);
#   3. False.
# Then the CROSS-TIER clamp on the RESOLVED values:
#   ``resolved_log = resolved_log and resolved_write``
# (down only — an inherited log=True must never promote write to True).
APP_STATE_KEY_DUAL_WRITE_DEFAULT = "embedding.dual_write_default"
APP_STATE_KEY_DUAL_RL_LOG_DEFAULT = "embedding.dual_rl_log_default"
APP_STATE_KEY_DUAL_ARCTIC_DEFAULT = "embedding.dual_arctic_default"

_DUAL_SETTING_KEY_WRITE_ALL_SLOTS = "dual_embedding_write_all_slots"
_DUAL_SETTING_KEY_RL_LOG = "dual_rl_log_enabled"
_DUAL_SETTING_KEY_ARCTIC_SECONDARY = "dual_embedding_arctic_secondary"

# v0.2.91 WP-L / plan decision #21 — machine-GLOBAL diagnostic log level.
# app_state ``logging.level`` → ``.claude/env`` ``VCO_LOG_LEVEL``.
#
# MUST MATCH ``vct_launcher_core::logging::LOG_LEVEL_APP_STATE_KEY`` /
# ``LOG_LEVEL_ENV``. Deliberately NOT the legacy ``logging_level`` key, which
# was removed from the Preferences page this same release for having no
# consumer — reusing that name would resurrect stale values written while it
# was a no-op and let them govern behaviour nobody opted into.
#
# Three consumer populations, and they do NOT resolve the level the same way:
#
#   * the launcher and the hub PROCESSES read ``launcher.db`` directly and
#     resolve ``VCO_LOG_LEVEL`` (real environment) > app_state > INFO. They
#     never source ``.claude/env``, so their tier 1 is the real environment
#     and the documented order holds exactly.
#   * project-side HOOKS and their Python children source ``.claude/env``.
#     They get the DEFAULTED form below, so an operator who exports
#     ``VCO_LOG_LEVEL=debug`` in their shell keeps it — the stored preference
#     only fills in when the environment is silent.
#   * MCP SERVERS get their environment from ``.claude/settings.json`` ``env``,
#     which structurally overwrites inherited values with no conditional
#     syntax available. Projecting the level there would invert tier 1 for
#     precisely the processes an operator raises verbosity to debug, so
#     v0.2.91 does NOT project it to that surface at all: MCP diagnostics
#     follow the operator's environment, or the INFO default. (Recorded
#     extension point, deliberately not built this cycle: the MCP already
#     queries the hub's ``/config`` at import, so a hub-config tier could
#     carry the preference there without the clobber. Add it only if field
#     demand appears.)
APP_STATE_KEY_LOGGING_LEVEL = "logging.level"
_ENV_LOGGING_LEVEL = "VCO_LOG_LEVEL"

#: Keys emitted ONLY into ``.claude/env``, and in the DEFAULTED shell form
#: ``export K="${K:-value}"`` rather than a hard assignment.
#:
#: This is the escape hatch for a projected value that must yield to the
#: operator's own environment. A canonical key does the opposite — it is the
#: authority, and a hand-set value on disk is reconciled away on the next
#: projection (that is the contract
#: ``test_arctic_secondary_hand_set_env_file_flips_to_db_off`` pins, and it is
#: correct for per-project CONFIG). A debug knob is the other kind of value:
#: the person who exported it is the authority for that shell.
#:
#: Keep this set tiny. If a key is per-project configuration, it belongs in
#: :data:`_CANONICAL_KEYS`; only operator-override knobs belong here.
SHELL_DEFAULTED_ENV_KEYS: tuple[str, ...] = (_ENV_LOGGING_LEVEL,)

# v0.2.72 R1 (F5 residual): explicit GUI override for the machine-global
# shared-KG class name, written by the launcher's SharedKgPicker via
# ``set_shared_kg_collection_name`` (project_identity.rs).
#
# MUST MATCH — the SHARED_KG_COLLECTION resolution is implemented three
# times and the precedence must stay identical in all three (drift here
# is the R1 resolver-disagreement bug):
#   * ``launcher/src-tauri/src/commands/project_env_settings.rs``
#     (``APP_STATE_KEY_SHARED_KG_NAME`` + ``populate()`` Priority 1)
#   * ``launcher/src-tauri/vct-hub/src/config_api.rs`` (hub resolver,
#     app_state override read before the binding-role resolution)
#   * THIS file (``project_env_from_db``)
# Precedence in every implementation: non-empty
# ``app_state[shared_kg.collection_name]`` wins; otherwise each surface's
# pre-existing binding-derivation chain applies.
APP_STATE_KEY_SHARED_KG_NAME = "shared_kg.collection_name"


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


def _global_dual_flag(conn: sqlite3.Connection, app_state_key: str) -> bool:
    """Host-wide default for one dual flag, from ``app_state``.

    Mirror of ``Db::dual_flag_install_default``. The Rust
    ``app_state_set_bool`` writer stores the literal strings ``"true"`` /
    ``"false"``, and ``app_state_get_bool`` reads back
    ``matches!(v, "true" | "1")`` — replicate that exactly (NOT a generic
    truthy parse) so a value like ``"yes"`` is False on both sides.
    Absent row ⇒ ``False``. Soft-fail on any DB error.
    """
    raw = _fetch_app_state_str(conn, app_state_key)
    return raw in ("true", "1")


def _fetch_dual_flag_explicit(
    conn: sqlite3.Connection,
    project_id: str,
    module_id: str,
    setting_key: str,
) -> Optional[bool]:
    """Explicit per-project dual-flag row, or ``None`` when inheriting.

    Mirror of ``Db::dual_flag_explicit``. A malformed (non-bool) value is
    treated as ABSENT rather than fail-open-to-true: these flags are opt-in
    cost multipliers, so a corrupt row must not silently turn on extra
    embedding calls.
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
        value = json.loads(row["setting_value"])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, bool) else None


def _resolve_dual_flags_cascade(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[bool, bool, bool]:
    """Resolve the three dual flags — the ONE cascade, mirrored.

    Returns ``(write_all_slots, rl_log, arctic_secondary)`` as EFFECTIVE
    values, i.e. after the log⟹write clamp.

    LOCKED order (must match
    ``vct-launcher-core/src/db/settings.rs::Db::resolve_dual_flags`` — and
    therefore the hub ``config_api.rs`` resolver, which calls it — byte-for-
    byte; cross-surface lockstep prevents the Defect-D class of
    GUI-write-vs-hub-read disagreement):

      1. explicit per-project ``module_settings`` row wins in BOTH
         directions (an explicit ``False`` beats a host-wide ``True``);
      2. else the host-wide ``app_state`` default;
      3. else ``False``.

    Then the CROSS-TIER clamp: ``rl_log = rl_log and write_all_slots``. It
    only ever clamps DOWN — an inherited ``rl_log = True`` must never promote
    ``write_all_slots``, because that would let a host-wide default overrule
    an explicit per-project opt-out, which step 1 forbids.

    Plan §F #25: NOTHING here consults ``module_installs`` or any
    module-enabled flag. ``dual_rl_log_enabled`` is addressed under module_id
    ``vct-rl-reranker``, but the paid module gates RERANKING, never
    COLLECTION — a project without the module resolves identically.
    """

    def resolve(module_id: str, setting_key: str, app_state_key: str) -> bool:
        explicit = _fetch_dual_flag_explicit(conn, project_id, module_id, setting_key)
        if explicit is not None:
            return explicit
        return _global_dual_flag(conn, app_state_key)

    write_all_slots = resolve(
        ORCHESTRATOR_CORE_MODULE_ID,
        _DUAL_SETTING_KEY_WRITE_ALL_SLOTS,
        APP_STATE_KEY_DUAL_WRITE_DEFAULT,
    )
    rl_log = resolve(
        _RL_RERANKER_MODULE_ID,
        _DUAL_SETTING_KEY_RL_LOG,
        APP_STATE_KEY_DUAL_RL_LOG_DEFAULT,
    )
    arctic_secondary = resolve(
        ORCHESTRATOR_CORE_MODULE_ID,
        _DUAL_SETTING_KEY_ARCTIC_SECONDARY,
        APP_STATE_KEY_DUAL_ARCTIC_DEFAULT,
    )
    return (write_all_slots, rl_log and write_all_slots, arctic_secondary)


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


def _is_missing_table_error(exc: sqlite3.Error) -> bool:
    """True when ``exc`` says a table this DB was queried for does not exist.

    ONE home for the ``"no such table" in str(exc).lower()`` test that three
    resolvers in this module used to inline (``_fetch_diagram_access_list``,
    ``_fetch_user_secret_known_keys``, and now
    :func:`probe_codegraph_binding_prefix`). Extracted rather than copied a
    third time.

    Why the distinction is load-bearing: a pre-migration launcher.db that has
    never had the table is a TRUE, structural "there are no rows" — soft-fail
    to the empty answer. Every OTHER ``sqlite3.Error`` (corrupt image, locked
    DB, disk I/O error) means we could not read a table that may well hold
    rows, which is a different fact and must never be reported as emptiness.

    (``vco_lib/kg_binding_heal.py`` inlines the same test at 10 further sites;
    that module is outside this package's file set — reported to the merge
    lane rather than edited here.)
    """
    return "no such table" in str(exc).lower()


#: The question :func:`probe_codegraph_binding_prefix` answers.
_WHAT_CODEGRAPH_BINDING = (
    "the project's project_codegraph_bindings.collection_prefix"
)


def probe_codegraph_binding_prefix(
    conn: sqlite3.Connection, project_id: str
) -> "ProbeResult[Optional[str]]":
    """Tri-state read of ``project_codegraph_bindings.collection_prefix``.

    v0.2.92 W18 — the ONE home for this question. Returns:

        * ``present(prefix)`` — a binding row names a non-empty prefix.
        * ``absent(None)``    — the read SUCCEEDED and no binding names a
          prefix: the table does not exist yet (pre-migration DB), there is no
          row for this project, the column is not a string, or the prefix is
          empty/whitespace. All four are true statements about a readable DB.
        * ``unknown(reason)`` — the DB could NOT be read (corrupt image,
          locked, I/O error). **Not** absence.

    The distinction matters because the ABSENT answer legitimately routes the
    caller to a NAME-DERIVED placeholder prefix (correct: no analysis has run,
    so any placeholder is fine and it matches what the hub derives). Applying
    that same fallback to a read we could not perform is how a projection
    silently overwrites a good, binding-derived ``CODE_GRAPH_PROJECT`` with a
    guess — and a wrong code-graph prefix makes every CLI and hook query a
    collection that does not exist, silently returning nothing.

    v0.2.72 R2 (F5 residual): the CODE_GRAPH_PROJECT env value must derive
    from the SAME source the hub resolver uses, or the CLI/hooks (env
    fallback) and the MCP (hub-first) query DIFFERENT code-graph
    collections after a prefix rebind.

    MUST MATCH the hub's derivation in
    ``launcher/src-tauri/vct-hub/src/config_api.rs`` (search
    ``code_graph_collection_prefix``): binding row's ``collection_prefix``
    first, derived fallback only when no binding row exists. (The two
    no-row fallbacks differ historically — hub: slug-sanitized; this
    projection: name-sanitized via :func:`_sanitize_kg_collection` — and
    both fire only before the first code-graph analysis has written a
    binding row, when any placeholder prefix is acceptable.)

    Mirrors the hub's ``get_project_codegraph_binding`` read: no
    ``enabled`` filter — a disabled binding still names the prefix the
    analyzer last wrote to.

    Never raises.
    """
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT collection_prefix FROM project_codegraph_bindings "
            "WHERE project_id = ?",
            (project_id,),
        )
        row = cur.fetchone()
    except sqlite3.Error as exc:
        if _is_missing_table_error(exc):
            # Pre-migration / partial launcher.db: the table has never
            # existed, so no binding can name a prefix. A true absence.
            return ProbeResult.absent(what=_WHAT_CODEGRAPH_BINDING)
        return ProbeResult.unknown(
            f"{type(exc).__name__}: {exc}", what=_WHAT_CODEGRAPH_BINDING,
        )
    if row is None:
        return ProbeResult.absent(what=_WHAT_CODEGRAPH_BINDING)
    raw = row["collection_prefix"]
    if not isinstance(raw, str):
        return ProbeResult.absent(what=_WHAT_CODEGRAPH_BINDING)
    stripped = raw.strip()
    if not stripped:
        return ProbeResult.absent(what=_WHAT_CODEGRAPH_BINDING)
    return ProbeResult.present(stripped, what=_WHAT_CODEGRAPH_BINDING)


def _fetch_codegraph_binding_prefix(
    conn: sqlite3.Connection, project_id: str
) -> Optional[str]:
    """Scalar view of :func:`probe_codegraph_binding_prefix`.

    ``str`` when a binding names a prefix, ``None`` when the read SUCCEEDED
    and none does.

    Raises:
        vco_lib.weaviate_helpers.ProbeUnavailable: the read did not succeed.
            v0.2.92 W18 — this used to return ``None`` on any ``sqlite3.Error``
            too, so "no binding" and "could not read the DB" arrived at the
            caller as the same value and both fell through to a name-derived
            guess.

    Kept as a separate function for ``vco_lib.project_identity.resolve_snapshot``
    (outside this package's file set), which imports it by this name. NOTE for
    whoever owns that module: it wraps this call in ``except Exception:
    code_prefix = None``, so the raise is swallowed there and the conflation
    survives at THAT site — the migration recipe is in the WP-2 report.
    """
    return probe_codegraph_binding_prefix(conn, project_id).require()


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
        # v0.2.96 L-11: the shared RO-URI builder (see `_connect_ro`).
        conn = sqlite3.connect(
            _sqlite_ro_uri(db_path), uri=True, timeout=5.0
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

    Join ``codegraph_access`` to ``projects`` on the grantor side, pull
    the grantor's display ``name``. Excludes self (the project always
    has access to its own codegraph; the env var carries PEERS only).

    Why ``p.name`` and NOT ``p.slug`` (GAP-CG-1 fix, 2026-07-14): the
    analyzer writes each project's Code* classes under
    ``canonical_class_prefix(NAME)_Code*`` and stamps the ``project``
    property = NAME (``analyze_code_graph.py`` — the ``--project
    <project.name>`` arg). The MCP/CLI consumer sanitises each list
    entry through the SAME underscore-preserving canonical rule to
    rebuild the peer's class prefix AND uses the raw entry as the
    ``project``-property filter value (``kg_access.py`` /
    ``weaviate_mcp/server.py``). A slug (lowercased/hyphenated) diverges
    from the name on BOTH derived values (``"Client Alpha"`` → analyzer
    prefix ``ClientAlpha`` + filter ``Client Alpha``; slug
    ``client-alpha`` → reader prefix ``Client_alpha`` + filter
    ``client-alpha``) → the peer's collections aren't found and the
    filter never matches → silent zero peer results. Emitting the NAME
    makes writer/reader prefix + filter agree for ANY project name.

    This converges on the sibling ``_fetch_diagram_access_list`` in this
    module, which already does ``SELECT p.name`` for the identical
    writer/reader-parity reason (fixed in ``70381fa9`` for diagrams five
    days after this resolver was born slug-based in ``8170b0bf`` — an
    unreconciled omission, now closed).

    Blank names are filtered (defensive against malformed DB rows;
    mirrors the diagrams resolver's ``if r["name"]`` guard).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT p.name FROM codegraph_access ca "
        "JOIN projects p ON p.id = ca.grantor_project_id "
        "WHERE ca.grantee_project_id = ? AND ca.access_level = 'read'",
        (project_id,),
    )
    return sorted({str(r["name"]) for r in cur.fetchall() if r["name"]})


def _fetch_diagram_access_list(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Resolve the ``VCT_DIAGRAMS_ACCESS_LIST`` value.

    Joins ``diagram_access`` to ``projects`` on the grantor side, pulling
    the grantor's display ``name``. The MCP consumer
    (``weaviate_mcp/server.py::_diagrams_peer_collections``) sanitises
    each name through ``_sanitize_collection_prefix`` and appends
    ``_Diagrams`` to derive the canonical Weaviate class name.

    Why ``p.name`` and not ``p.slug``: the diagrams collection-prefix
    derivation is keyed on the project NAME (sanitised) — same rule the
    launcher's ``project_diagrams`` indexer uses when it writes
    ``<SanitizedName>_Diagrams`` rows into Weaviate. Switching to slugs
    would create a prefix mismatch between writer and reader.
    ``VCT_CODE_GRAPH_ACCESS_LIST`` carries NAMES for the same reason
    (converged 2026-07-14, GAP-CG-1) — the two access-list resolvers
    now agree on the identity form.

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
        # v0.2.92 W18: routed through the ONE home. Behaviour unchanged —
        # missing table is a true "no grants"; every other error still raises.
        if _is_missing_table_error(exc):
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
        # v0.2.92 W18: routed through the ONE home. Behaviour unchanged.
        if _is_missing_table_error(exc):
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
    shared, global) for the given project — the names whose in-tree values
    every :func:`apply_project_env` checks for value evidence
    (:func:`classify_json_env_secrets`), also printed by the
    ``user-secret-known-keys`` verb.

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


def _sanitize_collection_prefix(slug: str) -> str:
    """Slug → class-prefix sanitiser, underscore-PRESERVING.

    v0.2.84 D1 (P2): the Python mirror of the hub's
    ``sanitize_collection_prefix`` (now
    ``vct_launcher_core::collection_naming::sanitize_collection_prefix``,
    config_api.rs pre-move). Used for the dev / diagrams NON-canonical
    fallback — the custom-rename case where the primary KG binding exists
    but doesn't end ``_KnowledgeGraph`` — so this projection converges on
    the hub's byte-exact fallback (previously it name-derived via
    :func:`_sanitize_kg_collection`, drifting from the hub's slug-derived
    value; that drift is the P2 finding).

    DISTINCT from :func:`_sanitize_kg_collection` (underscore-DROPPING,
    ``"Vct"`` fallback — drives the KG BASENAME) and from
    ``vco_lib.project_naming.canonical_class_prefix`` (the codegraph SSOT).
    Do NOT collapse the three: they are three different rules with three
    different fallbacks. This one mirrors the hub's fallback verbatim:

      1. Replace non-alphanumeric ASCII chars with ``_``.
      2. Trim leading/trailing ``_``, then capitalize the first character.
      3. If empty after trimming, return ``"Project"``.

    Parity with the Rust home is pinned by
    ``tests/test_v0284_dev_collection_one_rule.py`` (and byte-matches the
    hub's ``config_development_collection_falls_back_to_slug_for_non_
    canonical_primary`` test: slug ``"weirdproject"`` → ``"Weirdproject"``).
    """
    out_chars: list[str] = []
    for ch in slug:
        if ch.isascii() and ch.isalnum():
            out_chars.append(ch)
        else:
            out_chars.append("_")
    trimmed = "".join(out_chars).strip("_")
    if not trimmed:
        return "Project"
    return trimmed[0].upper() + trimmed[1:]


def _derive_dev_diagrams_from_kg(
    kg_collection: str, slug: str
) -> tuple[str, str]:
    """v0.2.84 D1 (P2) — the ONE python dev/diagrams derivation site.

    Given a RESOLVED (binding-first) primary KG collection name + the
    project slug, return ``(development_collection, diagrams_collection)``
    by the one rule (== hub v0.2.46 Decision C +
    ``vct_launcher_core::collection_naming::derive_sibling_collection``):

      * suffix-swap when ``kg_collection`` ends ``_KnowledgeGraph``
        (basename + ``_Development`` / ``_Diagrams``);
      * else — the custom-rename case (primary binding doesn't end
        ``_KnowledgeGraph``) — ``_sanitize_collection_prefix(slug)`` +
        the suffix (the SLUG-based, underscore-PRESERVING sanitizer,
        byte-matching the hub).

    Pure string logic (no DB / no probe). Extracted so
    :func:`project_env_from_db` and the read-only
    :func:`resolve_project_collection_names` share ONE derivation home —
    the structural parity pins key on this being the single site.
    """
    if kg_collection.endswith("_KnowledgeGraph"):
        basename = kg_collection[: -len("_KnowledgeGraph")]
        dev = basename + "_Development"
        diagrams = basename + "_Diagrams"
    else:
        prefix = _sanitize_collection_prefix(slug)
        dev = f"{prefix}_Development"
        diagrams = f"{prefix}_Diagrams"
    return dev, diagrams


# ─── Exceptions ─────────────────────────────────────────────────────────


class ConfigProjectionError(Exception):
    """Base for every error raised by this module."""


class ProjectNotFound(ConfigProjectionError):
    """No row in ``projects`` matches the supplied project_id."""


class DbUnreachable(ConfigProjectionError):
    """Could not open the launcher DB (missing, perms, corrupt)."""


class SettingsWriteRefused(ConfigProjectionError):
    """A settings file exists but could not be edited safely, so it was left
    byte-identical (v0.2.97, :mod:`vco_lib.settings_refusal`).

    ``refusals`` names each file and why. Raised after every OTHER requested
    surface was written, so one broken file never blocks the rest.
    """

    def __init__(self, refusals: Iterable["settings_refusal.Refusal"]) -> None:
        self.refusals = list(refusals)
        super().__init__(
            "; ".join(r.sentence() for r in self.refusals)
            + ". VCO never overwrites a settings file it cannot safely edit — "
            "everything else in it would be lost. Repair the file (or move it "
            "aside so a fresh one is created) and re-run; the project's "
            "UPDATE_DEFERRED.md has the details."
        )


# ─── orchestrator-root fallback ─────────────────────────────────────────


def _orchestrator_root_from_module() -> Path | None:
    """Resolve the orchestrator clone from THIS module's own location, or
    ``None`` when it cannot be CONFIRMED.

    ``vco_lib`` ships inside the orchestrator clone and ``install.py``
    installs it editable (``pip install -e .``), so the interpreter running
    this projection is loading a module file that sits inside the clone —
    which is exactly the fact a caller that could not resolve the root is
    missing.

    Confirmation is positive and two-part: an ancestor directory must carry
    BOTH the ``vct-module.json`` manifest (the orchestrator-clone marker
    ``project_init._find_orchestrator_root_from_module`` and
    ``vct_launcher_core::orchestrator_manifest`` already walk for) and a
    ``vco_lib/`` directory (so a manifest belonging to some OTHER VCT module
    cannot be mistaken for the clone). Nothing is guessed: a non-editable copy
    of ``vco_lib`` in a venv's ``site-packages`` has no manifest above it and
    yields ``None`` — the conservative answer, which leaves the three
    portability keys omitted exactly as before this fallback existed.

    Unlike ``project_init._find_orchestrator_root_from_module``, this returns
    ``None`` rather than a best-effort parent directory: this value is WRITTEN
    into every project's ``.claude/env`` as an absolute pointer, and a wrong
    pointer is worse than an absent one (the venv ladder validates the env
    tier and would reject it, but the launcher, the hooks and the user reading
    the file would not).

    Never raises.
    """
    try:
        here = Path(__file__).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for parent in here.parents:
        try:
            if (parent / "vct-module.json").is_file() and (parent / "vco_lib").is_dir():
                return parent
        except OSError:
            continue
    return None


# ─── project_env_from_db ────────────────────────────────────────────────


def project_env_from_db(
    project_id: str,
    *,
    db_path: Path | None = None,
    weaviate_url_override: str | None = None,
    ollama_url_override: str | None = None,
    active_embedding_override: str | None = None,
    shared_kg_default: str | None = None,
    weaviate_port_default: int | None = None,
    ollama_port_default: int | None = None,
    code_embed_port_default: int | None = None,
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

    PURE FUNCTION over its inputs: same DB state in → same dict out. No
    filesystem writes, no caching, no environment read: the service
    endpoints come from the same DB's ``service_endpoints`` rows (row →
    compiled default — see :mod:`vco_lib.service_endpoints`); neither the
    projected transport (``WEAVIATE_URL`` …) nor the retired statements
    (``VCT_WEAVIATE_URL``, ``vct-config.toml``) are read. Test fixtures pin
    values through the keyword arguments.

    Args:
        project_id: The project's UUID (the ``projects.id`` column).
        db_path: Optional override of the launcher DB location. Defaults
            to :func:`_resolve_launcher_db_path`. Tests should pass an
            explicit path to avoid touching the real ``~/.vct/launcher.db``.
        weaviate_url_override: Pin the WEAVIATE_URL value. Unpinned, it is
            this machine's Weaviate as
            :func:`vco_lib.service_endpoints.machine_weaviate_url` resolves
            it — the ``service_endpoints`` row, else ``localhost:8081`` — the
            same answer the hub's ``/config`` serves. The launcher pins it
            with its own Rust resolution of the same row.
        ollama_url_override: Same shape, for OLLAMA_URL.
        active_embedding_override: Pin ACTIVE_EMBEDDING; otherwise read
            from ``module_settings`` (orchestrator-core /
            active_embedding), defaulting to "qwen3".
        shared_kg_default: Fallback SHARED_KG_COLLECTION when the
            project's ``shared`` KG binding row is absent.

            **v0.2.72 R1**: a non-empty
            ``app_state[shared_kg.collection_name]`` override (the GUI
            SharedKgPicker) is consulted FIRST and wins over both the
            binding row and this fallback — matching the Rust
            ``populate()`` Priority-1 semantics.

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
        weaviate_port_default: Replaces the compiled default port when
            this machine has NO ``weaviate`` row (``None`` = 8081); a row
            always wins. ``WEAVIATE_PORT`` is the row's port (or this value
            when the URL is pinned).
        ollama_port_default: Same, for OLLAMA_URL / OLLAMA_PORT
            (``None`` = 11435).
        code_embed_port_default: Same, for CODE_EMBED_URL /
            CODE_EMBED_SERVICE_URL / CODE_EMBED_PORT (``None`` = 11440).
        orchestrator_root: The orchestrator clone to emit
            ``VCT_ORCHESTRATOR_ROOT`` / ``VCT_INFRASTRUCTURE_DIR`` /
            ``VCT_INSTALL_ROOT`` for. When ``None`` (default, and what the
            launcher passes whenever its own root resolver fails), the root is
            resolved from THIS module's location via
            :func:`_orchestrator_root_from_module` — see the comment at that
            emit site for why an apply that omits them REMOVES them. The three
            keys are omitted only when that confirmation also fails (a
            non-editable ``vco_lib`` copy with no manifest above it).

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
        # v0.2.72 R1 (F5 residual): the explicit GUI override
        # `app_state[shared_kg.collection_name]` (SharedKgPicker →
        # `set_shared_kg_collection_name`) is Priority 1 — it wins over
        # BOTH the project's own `role='shared'` binding row and the
        # orchestrator-root derivation, exactly like the Rust
        # `populate()` (project_env_settings.rs) and the hub resolver
        # (config_api.rs). See the MUST-MATCH note on
        # `APP_STATE_KEY_SHARED_KG_NAME`. Empty/absent → fall through to
        # the binding derivation below (`_fetch_app_state_str` already
        # coerces empty/whitespace to None).
        shared_kg_override = _fetch_app_state_str(
            conn, APP_STATE_KEY_SHARED_KG_NAME
        )
        if shared_kg_override:
            shared_kg = shared_kg_override
        else:
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
        # v0.2.84 D1 (P2) — DEVELOPMENT_COLLECTION (and DIAGRAMS_COLLECTION)
        # derive from the RESOLVED primary KG by the ONE rule
        # (`_derive_dev_diagrams_from_kg`, == hub v0.2.46 Decision C +
        # `vct_launcher_core::collection_naming`). The dead
        # `kg_bindings.get("archive", ...)` priority is REMOVED: no
        # installer / launcher / migration ever writes a `role='archive'`
        # binding, so that lookup ALWAYS name-derived from the display name
        # (`{sanitized}_Development`) and LIVE-REVERTED the hub/launcher's
        # correct binding-paired value on every install-bundle / update —
        # the exact P2 drift (the 340-row `VCODev_Development` docs store
        # stranded when the display name resolved to
        # `VibeCodedOrchestrator_Development`). The derivation suffix-swaps
        # off `kg_collection`, which is ITSELF binding-first (:1558 above),
        # so a `primary` override carries through; the non-`_KnowledgeGraph`
        # fallback (custom-rename primary) uses the SLUG-based sanitizer to
        # byte-match the hub. Both dev + diagrams share the one derivation.
        dev_collection, diagrams_collection = _derive_dev_diagrams_from_kg(
            kg_collection, proj.slug
        )

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
        # v0.2.71 T-B-flags / v0.2.88 (DEFECT 5) — the three dual toggles.
        #
        # v0.2.91 WP-L (plan decision #22): they gained an INSTALL-WIDE
        # default tier, so the old per-row ``_fetch_module_setting_bool(...,
        # default=False)`` shape is no longer correct — it collapses "no
        # per-project row" into False and would make this projection disagree
        # with both the GUI and the hub the moment a host-wide default is on.
        # ONE cascade, mirrored from the Rust home; see
        # ``_resolve_dual_flags_cascade``'s docstring for the locked order,
        # the cross-tier clamp, and the parity test that pins them.
        (
            dual_embedding_write_all_slots,
            dual_rl_log_enabled,
            dual_embedding_arctic_secondary,
        ) = _resolve_dual_flags_cascade(conn, project_id)
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
            # and fixes the auto-seeded qwen3 case (a backfill-stamped qwen3
            # with no provenance). GUARD on the derive: an unmapped/absent hardware
            # pick → stay qwen3 (never stamp a guessed profile → wrong slot).
            active_embedding = _resolve_active_embedding_cascade(conn, project_id)

        # v0.2.72 T-FLOOR (P1): machine-global code-graph two-stage floor
        # overrides. T-GUI-DB writes app_state[codegraph.retrieval_floor] /
        # app_state[codegraph.post_rerank_floor] when the user sets a non-
        # default floor in the launcher's Codegraph settings; we project them
        # to VCO_CODE_GRAPH_RETRIEVAL_FLOOR / VCO_CODE_GRAPH_POST_RERANK_FLOOR
        # so the CLI + MCP + hooks all read the same value the GUI picked. Soft-
        # fail: an absent row → None → the key is OMITTED by `_set` below → the
        # shared resolver in weaviate_mcp/code_ranking.py falls back to its per-
        # slot default. (Read helper is a hook for the DB value; when T-GUI-DB
        # has not yet written the row, the projection simply emits nothing and
        # the shipped per-slot defaults apply.)
        codegraph_retrieval_floor = _fetch_app_state_str(
            conn, APP_STATE_KEY_CODEGRAPH_RETRIEVAL_FLOOR
        )
        codegraph_post_rerank_floor = _fetch_app_state_str(
            conn, APP_STATE_KEY_CODEGRAPH_POST_RERANK_FLOOR
        )

        # v0.2.73 Concern-A/C: machine-GLOBAL RL telemetry opt-outs. The GUI's
        # global Preferences toggles write these app_state keys ("true"/"false");
        # we project the GLOBAL leg into every project's env so the RL resolvers
        # can OR it with the per-project ``.claude/env`` flag (global disable
        # overrides all projects). Soft-fail: an absent row → None → the key is
        # OMITTED by ``_set`` below, and the resolver treats a missing global env
        # as "not globally disabled" (the per-project flag still applies).
        rl_local_logging_disabled_global = _fetch_app_state_str(
            conn, APP_STATE_KEY_RL_LOCAL_LOGGING_DISABLED_GLOBAL
        )
        rl_online_training_disabled_global = _fetch_app_state_str(
            conn, APP_STATE_KEY_RL_ONLINE_TRAINING_DISABLED_GLOBAL
        )

        # v0.2.91 WP-L (decision #21): machine-GLOBAL diagnostic log level.
        # Absent row → None → the key is OMITTED by ``_set`` below and the
        # consumers apply their INFO default. Unknown values are NOT coerced
        # here — the consumer owns that fallback (same split as the codegraph
        # floors), and the GUI only ever writes error|warn|info|debug.
        logging_level = _fetch_app_state_str(conn, APP_STATE_KEY_LOGGING_LEVEL)

        # v0.2.72 R2 (F5 residual): CODE_GRAPH_PROJECT derives
        # hub-consistently — the project's codegraph binding prefix
        # (`project_codegraph_bindings.collection_prefix`) first, the
        # name-derived prefix only when no binding row exists. Before
        # this fix the projection ALWAYS emitted the name-derived prefix,
        # so a rebound prefix left the CLI/hooks (env fallback) querying a
        # different set of collections than the MCP (hub-first) — and the
        # rebind never moved the watcher's hashed CODE_GRAPH_PROJECT key,
        # so no guarded MCP reload fired. MUST-MATCH note lives on
        # `_fetch_codegraph_binding_prefix`.
        #
        # v0.2.76 (seams-lens #1): the NO-BINDING fallback now uses the
        # underscore-PRESERVING `canonical_class_prefix` (SSOT:
        # vco_lib.codegraph_naming) — NOT the underscore-DROPPING
        # `_sanitize_kg_collection` (`sanitized`) it used before. The analyzer
        # + binding-seed name code-graph classes with canonical_class_prefix, so
        # for a never-yet-analyzed underscore-containing name (e.g. `My_Project`)
        # the old fallback emitted `CODE_GRAPH_PROJECT=MyProject` while the first
        # analysis would bind `My_Project_Code*` — split-brain until a rebind.
        # canonical falls back to `sanitized` only for names it rejects
        # (leading-digit / all-symbol), where any placeholder prefix is fine
        # (no binding exists yet). Matches the Rust `resolve_code_graph_project`
        # + the standalone `_apply_standalone_env` writer.
        #
        # v0.2.92 W18 — the three-way branch. The name-derived fallback below
        # is CORRECT for a project whose binding row does not exist yet, and
        # WRONG for a project whose binding row we simply failed to read: this
        # function's output is written to `.claude/settings.json` env and
        # `.claude/env`, so guessing here persists a CODE_GRAPH_PROJECT that
        # can name a collection nothing ever wrote to. Refuse instead — the
        # module's declared vocabulary for "the launcher DB could not be read"
        # is `DbUnreachable`, and its callers already surface it
        # (`env_template` exits 3 with `db_unreachable`; install.py records
        # the action and leaves the existing env surfaces untouched).
        _cg_binding = probe_codegraph_binding_prefix(conn, project_id)
        if _cg_binding.is_unknown():
            raise DbUnreachable(
                "could not read "
                "project_codegraph_bindings.collection_prefix for project "
                f"{project_id}: {_cg_binding.reason}. Refusing to project a "
                "name-derived CODE_GRAPH_PROJECT over a binding that may "
                "exist — re-run once the launcher DB is readable."
            )
        _cg_binding_prefix = _cg_binding.require()
        if _cg_binding_prefix:
            code_graph_project = _cg_binding_prefix
        else:
            try:
                from vco_lib.codegraph_naming import canonical_class_prefix
                code_graph_project = canonical_class_prefix(proj.name)
            except ValueError:
                code_graph_project = sanitized
        # v0.2.97: the keys whose pre-v0.2.73 in-tree VALUES every apply
        # removes (see ProjectEnvBundle.user_secret_known_keys).
        user_secret_known_keys = _fetch_user_secret_known_keys(conn, project_id)
        # v0.2.97: this machine's `service_endpoints` rows, read while the
        # connection is open — the SAME launcher.db the rest of the bundle
        # comes from. Rendered below through the ONE rule
        # (vco_lib/service_endpoints.py, mirrored by the hub): row → compiled
        # default, no other leg.
        endpoint_rows = _service_endpoints.read_rows(conn)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    # Service endpoints. A caller-pinned value wins; otherwise the machine's
    # row — the answer `service_endpoints.rs` gives the hub's `/config` and
    # the launcher's `populate`, so the three agree. A `*_port_default`
    # replaces the compiled default for an ABSENT row only (the launcher
    # passes the ports it resolved from the same rows, so the two cannot
    # differ).
    _service_endpoints.warn_absent(endpoint_rows)
    weaviate_row = endpoint_rows.get("weaviate")
    ollama_row = endpoint_rows.get("ollama")
    code_embed_row = endpoint_rows.get("code_embed")
    if weaviate_url_override:
        weaviate_url = weaviate_url_override
        weaviate_port = (
            weaviate_port_default
            if weaviate_port_default is not None
            else _service_endpoints.weaviate_port_for_url(weaviate_url)
        )
    else:
        weaviate_url = _service_endpoints.render_url(
            "weaviate", weaviate_row, default_port=weaviate_port_default,
        )
        weaviate_port = _service_endpoints.render_port(
            "weaviate", weaviate_row, default_port=weaviate_port_default,
        )
    ollama_port = _service_endpoints.render_port(
        "ollama", ollama_row, default_port=ollama_port_default,
    )
    code_embed_port = _service_endpoints.render_port(
        "code_embed", code_embed_row, default_port=code_embed_port_default,
    )
    ollama_url = ollama_url_override or _service_endpoints.render_url(
        "ollama", ollama_row, default_port=ollama_port_default,
    )
    code_embed_url = _service_endpoints.render_url(
        "code_embed", code_embed_row, default_port=code_embed_port_default,
    )

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
    # v0.2.71 T-B-flags — dual-write + dual-log toggles. Boolean →
    # "true"/"false" (lowercase). The consumers parse truthily:
    # ``embedding_service.py::_resolve_write_all_slots`` matches
    # {"1","true","yes","on"}; T-C's ``_resolve_dual_rl_log_enabled`` is the
    # same shape. Emitting "false" explicitly (rather than omitting) makes
    # the DB-driven OFF state visible on disk, which the survives-update
    # regression test asserts.
    _set(
        "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
        "true" if dual_embedding_write_all_slots else "false",
    )
    _set("DUAL_RL_LOG_ENABLED", "true" if dual_rl_log_enabled else "false")
    # v0.2.88 (DEFECT 5): the DB is now the truth that POPULATES the arctic-
    # secondary env (pre-fix it was env-only and survived updates only by being
    # unknown to the projection). Consumer
    # (embedding_service.py::_resolve_dual_embedding_arctic_secondary) parses
    # {"1","true","yes","on"} truthily; emit lowercase to match.
    _set(
        "DUAL_EMBEDDING_ARCTIC_SECONDARY",
        "true" if dual_embedding_arctic_secondary else "false",
    )
    _set("PROJECT_NAME", proj.name)
    # A-8 (v0.2.73): KG_BASE_DIR mirrors the project folder so
    # .claude/settings.json::env and .claude/env agree with the value
    # ``build_kg_sync_env`` already passes to kg-sync subprocesses. Matches
    # the Rust ``KG_BASE_DIR`` arm (``folder.display().to_string()``).
    # Always emitted (never None) so the Python apply no longer strips it
    # after a secrets-toggle write left it on disk.
    _set("KG_BASE_DIR", str(proj.folder_path))
    # v0.2.72 R2: binding-prefix-first (hub-consistent); see the
    # resolution comment above where `code_graph_project` is computed.
    _set("CODE_GRAPH_PROJECT", code_graph_project)
    _set("ACTIVE_EMBEDDING", active_embedding)
    # v0.2.72 T-FLOOR (P1): code-graph two-stage floor overrides (machine-global,
    # from app_state via T-GUI-DB). Emitted only when a value is present — an
    # absent app_state row → OMITTED → shared per-slot default applies. The
    # empty-string / unparseable coercion lives in the CONSUMER
    # (weaviate_mcp/code_ranking.py::resolve_*_floor), so here we simply project
    # the raw string (already stripped by _fetch_app_state_str).
    _set("VCO_CODE_GRAPH_RETRIEVAL_FLOOR", codegraph_retrieval_floor)
    _set("VCO_CODE_GRAPH_POST_RERANK_FLOOR", codegraph_post_rerank_floor)
    # v0.2.73 Concern-A/C: GLOBAL leg of the two-level RL telemetry gate. Emitted
    # only when the app_state row is present (absent → OMITTED → resolver treats
    # the missing global env as "not globally disabled"). Raw string projected
    # verbatim; the resolver parses truthily ({"true","1","yes","on"}).
    _set(_ENV_RL_LOCAL_LOGGING_DISABLED_GLOBAL, rl_local_logging_disabled_global)
    _set(_ENV_RL_ONLINE_TRAINING_DISABLED_GLOBAL, rl_online_training_disabled_global)
    # (v0.2.91 WP-L: VCO_LOG_LEVEL is deliberately NOT `_set` here — it is
    # not a canonical key. It leaves via `shell_defaulted_env` at the bottom
    # of this function, reaching `.claude/env` only, in the defaulted form.
    # See SHELL_DEFAULTED_ENV_KEYS.)
    _set("WEAVIATE_URL", weaviate_url)
    _set("WEAVIATE_PORT", str(weaviate_port))
    _set("OLLAMA_URL", ollama_url)
    _set("OLLAMA_PORT", str(ollama_port))
    _set("CODE_EMBED_URL", code_embed_url)
    _set("CODE_EMBED_PORT", str(code_embed_port))
    # v0.2.97: the name every code-embed CLIENT reads
    # (weaviate_mcp/embeddings.py, query_code_graph.py, vco_lib.code_embed_image)
    # — before, only `~/.claude.json`'s weaviate-kg entry carried it, so the
    # projected CODE_EMBED_URL reached no consumer. Same value, by construction.
    _set("CODE_EMBED_SERVICE_URL", code_embed_url)

    # NOTE (WP-Q item 3 / G6): RL_SERVER_PORT / RL_SERVER_URL are DELIBERATELY
    # NOT projected here. Per the H.1 design contract (see
    # vct-hub/src/config_api.rs::ProjectConfig.rl_server_port, V52-AA v0.2.52),
    # the per-project RL serving port is a HUB-RESOLVED value, not a
    # settings.json/.claude/env key: the MCP's rl_enrichment._get_rl_client
    # reads it live from ProjectConfig.rl_server_port (sourced from the
    # module_ports SoT). Projecting it into the global env allowlist would force
    # the wrong precedence for a per-project value. The G6 reconcile therefore
    # fixes the module_ports ROW (record reality); the hub then serves the
    # correct port automatically — no env projection needed.

    # v0.2.95: when the CALLER could not resolve the orchestrator root, resolve
    # it from THIS MODULE's own location before giving up.
    #
    # The three portability keys were emitted only when a caller handed them
    # down, and the launcher's `ProjectEnvSettings::populate` hands down
    # `None` whenever `resolve_orchestrator_root` fails (a PATH-installed
    # launcher binary far from the clone — the case `install.py::
    # _seed_launcher_install_path` documents). Because an apply REBUILDS the
    # managed block from scratch and drops keys absent from the bundle, such a
    # run does not merely skip the keys — it REMOVES the ones a previous
    # bundle update wrote. Field evidence (field report 2026-09-14): a `.claude/env`
    # whose header advertises the portability keys and carries none of them,
    # which is what made a correctly-installed orchestrator undiscoverable —
    # the venv ladder's DURABLE tier is exactly this file-backed key.
    #
    # `vco_lib` ships INSIDE the orchestrator clone and `install.py` installs
    # it editable, so the module file's own path names the clone. Confirmed
    # positively (the `vct-module.json` manifest + a sibling `vco_lib/`), never
    # guessed: a non-editable copy of vco_lib in some venv's site-packages has
    # no manifest above it, so it resolves to None and the keys stay omitted —
    # today's behaviour, unchanged, rather than a wrong absolute path written
    # into every project.
    if orchestrator_root is None:
        orchestrator_root = _orchestrator_root_from_module()

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

    # GITHUB_TOKEN is never resolved here, by design: since v0.2.73 VCO
    # writes no secret value into a project (consumers resolve the PAT at
    # need through the hub — the "Secrets" section of
    # templates/ORCHESTRATOR-CLAUDE.md.template). It stays in the
    # canonical set so every apply REMOVES a GITHUB_TOKEN a pre-v0.2.73
    # writer left in `.claude/settings.json` env (signal-to-remove).

    # v0.2.91 WP-L (decision #21): the diagnostic log level rides the
    # shell-defaulted channel, NOT `canonical_env` — see
    # SHELL_DEFAULTED_ENV_KEYS for why an operator-override knob must not be
    # written as an authoritative assignment. Absent app_state row ⇒ omitted
    # entirely ⇒ consumers apply their INFO default.
    shell_defaulted: dict[str, str] = {}
    if logging_level:
        shell_defaulted[_ENV_LOGGING_LEVEL] = logging_level

    return {
        "canonical_env": env,
        "project_id": project_id,
        "project_root": Path(proj.folder_path),
        "shell_defaulted_env": shell_defaulted,
        "user_secret_known_keys": user_secret_known_keys,
    }


class ProjectCollectionNames(TypedDict):
    """Binding-first KG / development / diagrams collection names for one
    project — the read-only seam for WP-4's D3 (bootstrap/migrate).

    Same ONE rule as :func:`project_env_from_db` (they share the
    :func:`_derive_dev_diagrams_from_kg` derivation), but WITHOUT the full
    env bundle: callers that only need the three collection names (fresh
    create / migrate dispatch) read this instead of building a whole
    bundle.
    """

    kg_collection: str
    development_collection: str
    diagrams_collection: str


def resolve_project_collection_names(
    project_id: str,
    *,
    db_path: Path | None = None,
) -> ProjectCollectionNames:
    """Resolve a project's KG / development / diagrams collection names
    binding-first from launcher.db (v0.2.84 D1 — the read-only seam WP-4's
    D3 bootstrap/migrate call).

    Resolution (identical to :func:`project_env_from_db`, via the shared
    :func:`_derive_dev_diagrams_from_kg`):

      * KG = ``project_kg_bindings(role='primary').collection_name`` when a
        row exists; else ``_sanitize_kg_collection(name)_KnowledgeGraph``
        (name-derived last resort — a project with NO primary binding yet,
        e.g. fresh create before the binding is seeded).
      * dev / diagrams = suffix-swap off the resolved KG, slug-fallback for
        a non-``_KnowledgeGraph`` primary (custom-rename).

    Read-only: opens launcher.db read-only, never writes. Use this to
    resolve names BEFORE creating collections so bootstrap/migrate honor an
    existing binding instead of re-deriving from the display name (the R3
    re-creator fix).

    Args:
        project_id: The launcher project id (or slug — resolved the same
            way :func:`project_env_from_db` resolves it).
        db_path: Optional launcher.db override (tests pin this).

    Returns:
        :class:`ProjectCollectionNames`.

    Raises:
        ProjectNotFound: no project row for ``project_id``.
        DbUnreachable: launcher.db missing / unopenable.
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
        sanitized = _sanitize_kg_collection(proj.name)
        kg_collection = kg_bindings.get(
            "primary", f"{sanitized}_KnowledgeGraph"
        )
        dev_collection, diagrams_collection = _derive_dev_diagrams_from_kg(
            kg_collection, proj.slug
        )
        return ProjectCollectionNames(
            kg_collection=kg_collection,
            development_collection=dev_collection,
            diagrams_collection=diagrams_collection,
        )
    finally:
        conn.close()


def resolve_collection_names_for_folder(
    folder: Path,
    *,
    db_path: Path | None = None,
) -> ProjectCollectionNames:
    """Folder-shaped read-only seam for WP-4's D3 (v0.2.84 D1/D3).

    The D3 bootstrap/migrate call-sites are folder+name shaped (the Rust
    launcher passes ``--project-folder`` to bootstrap-collections /
    migrate-collections). This maps a project FOLDER to its registered
    ``project_id`` — canonicalizing BOTH sides with :meth:`Path.resolve`
    first, so a symlinked / trailing-slash / relative folder still matches
    the stored ``folder_path`` — then delegates to
    :func:`resolve_project_collection_names` (NO second rule copy). The
    folder→id canonicalization lives HERE (next to the other read helpers),
    not in the ``project_init`` mega-file, because the matching pitfalls
    (symlinks, trailing slashes) must have ONE home.

    Args:
        folder: The project folder to resolve.
        db_path: Optional launcher.db override (tests pin this).

    Returns:
        :class:`ProjectCollectionNames` for the matched project.

    Raises:
        ProjectNotFound: no registered project's folder_path canonicalizes
            to ``folder`` (e.g. a standalone CLI bootstrap on a folder the
            launcher never saw). Callers should treat this — like
            :class:`DbUnreachable` — as the signal to fall back to the
            name-derived last resort (D1's no-binding path), NOT as fatal.
        DbUnreachable: launcher.db missing / unopenable.
    """
    # list_registered_projects raises DbUnreachable on a missing DB — let
    # it propagate (same no-launcher posture the caller falls back on).
    projects = list_registered_projects(db_path=db_path)

    def _canon(p: Path) -> Path:
        # resolve() collapses symlinks + `..` + trailing slashes to a
        # canonical absolute path. strict=False so a folder that doesn't
        # exist on disk (rare — a stale row) still normalizes rather than
        # raising; the comparison then simply won't match.
        try:
            return p.resolve()
        except OSError:
            return p.absolute()

    target = _canon(folder)
    for proj in projects:
        stored = str(proj.get("folder_path", ""))
        if not stored:
            continue
        if _canon(Path(stored)) == target:
            return resolve_project_collection_names(
                str(proj["id"]), db_path=db_path
            )
    raise ProjectNotFound(
        f"no registered project folder matches {folder} "
        f"(canonicalized {target})"
    )


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
#: The JSON surfaces: ``surface -> (path relative to the project, env key)``.
_JSON_SURFACE_FILES: dict[str, tuple[str, str]] = {
    _SURFACE_CLAUDE_SETTINGS: (".claude/settings.json", "env"),
    _SURFACE_VSCODE_SETTINGS: (".vscode/settings.json", "claude-code.env"),
}


# v0.2.84 D2 (P2) — env-repoint audit.
#
# Once D1 lands, the next `apply_project_env` on an existing install
# naturally OVERWRITES a stale name-derived DEVELOPMENT_COLLECTION /
# KG_COLLECTION with the binding-paired name — that IS the migration (no
# data movement, ever; it's a pointer fix). D2 makes that self-repair
# VISIBLE: when the writer changes an on-disk value of one of these two
# keys, we append a `dev_collection_env_repointed` auto-resolution row
# (v0.2.83 JSONL home) so a dogfooder can see the convergence in
# `.claude/logs/auto-resolutions.jsonl`. NO deferral entry is ever created
# under this id — it is a record of a self-resolved condition, not a
# pending one. Guard: no row when old == new (no noise on the steady state).
_REPOINT_AUDITED_KEYS: tuple[str, ...] = ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
_REPOINT_CONDITION_ID = "dev_collection_env_repointed"


def _read_surface_canonical_value(
    path: Path, key: str, *, env_key: str
) -> Optional[str]:
    """Read the current on-disk value of one canonical env KEY from a
    JSON env surface (settings.json / vscode settings.json).

    Returns the string value under ``root[env_key][key]`` when present and
    a string; ``None`` when the file is missing / malformed / the key is
    absent. Soft-fail throughout (audit reads never break a write) — which
    v0.2.97 made true: a non-UTF-8 file used to raise here, BEFORE any
    surface was written. JSONC is read too (the same reader the writer uses).
    """
    loaded = jsonc_edit.load_object(path) if path.exists() else None
    if loaded is None:
        return None
    block = loaded[0].get(env_key)
    if not isinstance(block, dict):
        return None
    val = block.get(key)
    return val if isinstance(val, str) else None


def _read_managed_env_canonical_value(path: Path, key: str) -> Optional[str]:
    """Read the current on-disk value of one canonical env KEY from the
    ``.claude/env`` managed block (``export KEY="value"``).

    Returns the unescaped string value when a matching export line exists
    inside the BEGIN/END managed block; ``None`` otherwise. Soft-fail:
    missing file / no marker / no matching line → ``None``.

    The parse is the ONE managed-block reader,
    :func:`vco_lib.envfile.env_value` with the block markers (v0.2.97 — this
    was a second parser that disagreed with it on ``\\"``); this function
    only binds the file and the markers.
    """
    from vco_lib.envfile import env_value

    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError: a non-UTF-8 file (v0.2.97)
        return None
    return env_value(
        text, key, begin_marker=CLAUDE_ENV_MANAGED_BEGIN, end_marker=CLAUDE_ENV_MANAGED_END,
    )


def _emit_repoint_audit_rows(
    project_root: Path,
    old_values: Mapping[str, Optional[str]],
    new_env: Mapping[str, str],
) -> None:
    """Emit a `dev_collection_env_repointed` audit row per audited key whose
    on-disk value the writer CHANGED (v0.2.84 D2).

    ``old_values`` maps each audited KEY to the representative pre-write
    on-disk value (or ``None`` when the key was absent). A row is emitted
    only when the key is present in ``new_env`` AND the old value was a
    non-empty string that DIFFERS from the new value — a genuine repoint of
    an existing value (a first-time write, where old is ``None`` / empty,
    is NOT a repoint and stays silent). Best-effort: import + write are
    soft; a failure never breaks the surface writes that already happened.
    """
    changed: list[tuple[str, str, str]] = []
    for key in _REPOINT_AUDITED_KEYS:
        new_val = new_env.get(key)
        old_val = old_values.get(key)
        if new_val is None:
            continue
        if not old_val:
            # Absent or empty on disk → first write, not a repoint.
            continue
        if old_val != new_val:
            changed.append((key, old_val, new_val))
    if not changed:
        return
    try:
        from vco_lib.deferral_emit import record_auto_resolution
    except Exception:  # noqa: BLE001 — the audit trail is best-effort
        return
    for key, old_val, new_val in changed:
        try:
            record_auto_resolution(
                project_root,
                _REPOINT_CONDITION_ID,
                action="repointed",
                detail=f"{key}: {old_val} → {new_val}",
            )
        except Exception:  # noqa: BLE001 — never break the write path
            pass


def apply_project_env(
    bundle: ProjectEnvBundle,
    *,
    surfaces: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    """Project ``bundle`` to the requested env surfaces.

    Args:
        bundle: As returned by :func:`project_env_from_db`.
        surfaces: Sequence of surface names. Defaults to
            ``("claude_settings_json", "claude_env")`` — matching the
            former production Rust writer (retired v0.2.97) after
            PR-27 (v0.2.12, 2026-05-16) removed the historical
            ``.vscode/settings.json`` write. Pass
            ``("claude_settings_json", "claude_env", "vscode_settings_json")``
            to also write the VS Code workspace surface (opt-in;
            useful for the diagrams flow where the VS Code extension's
            claude-code.env block is the only path to the embedded
            editor).

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
        SettingsWriteRefused: a JSON surface exists but could not be
            edited safely (v0.2.97). It is left byte-identical, the
            refusal is recorded in the project's deferral ledger, and
            every other requested surface is still written first.
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
    # v0.2.97 review R2 F18: GITHUB_TOKEN is canonical but no bundle ever
    # carries it, so leaving it in this set made every apply delete it BY
    # NAME — a token the user typed included. It is removed only on value
    # evidence, with the user secrets below.
    canonical_keys = list_canonical_keys() - _LEGACY_SECRET_ENV_KEYS

    # v0.2.84 D2 (P2): snapshot the on-disk KG_COLLECTION /
    # DEVELOPMENT_COLLECTION values BEFORE writing so we can emit a repoint
    # audit row when the writer changes one. Read from the surfaces we're
    # about to write, in production-surface priority (settings.json →
    # .claude/env → vscode); the first non-None wins as the representative
    # old value (all production surfaces normally carry the same value).
    # Best-effort: soft-fail reads never affect the write.
    old_repoint_values: dict[str, Optional[str]] = {
        key: None for key in _REPOINT_AUDITED_KEYS
    }
    for key in _REPOINT_AUDITED_KEYS:
        if _SURFACE_CLAUDE_SETTINGS in surfaces_seq:
            old_repoint_values[key] = _read_surface_canonical_value(
                project_root / ".claude" / "settings.json", key, env_key="env"
            )
        if old_repoint_values[key] is None and _SURFACE_CLAUDE_ENV in surfaces_seq:
            old_repoint_values[key] = _read_managed_env_canonical_value(
                project_root / ".claude" / "env", key
            )
        if old_repoint_values[key] is None and _SURFACE_VSCODE_SETTINGS in surfaces_seq:
            old_repoint_values[key] = _read_surface_canonical_value(
                project_root / ".vscode" / "settings.json",
                key,
                env_key="claude-code.env",
            )

    # v0.2.75 P3: the user-secret EMIT arm is retired; the writer's
    # `user_secret_pairs` parameter is always fed an empty set.
    us_pairs: list[tuple[str, str]] = []
    # v0.2.97 (review R2 F18): every apply removes from the JSON env blocks
    # the values VCO can PROVE it wrote — the in-file value equals the one the
    # launcher stores for that key (:func:`classify_json_env_secrets`). A name
    # match alone proves nothing and removes nothing; such a key is the
    # user's, reported by ``user_owned_secret_value_in_tree``. The secrets
    # stay in the keychain; only proven copies in committable files go.
    known_secret_keys = list(bundle.get("user_secret_known_keys") or [])
    verdicts = classify_json_env_secrets(project_root, known_keys=known_secret_keys)
    residue_before = retained_launcher_value_names(project_root, verdicts=verdicts)
    us_strip_keys: list[str] = residue_before.get(".claude/settings.json", [])
    vscode_strip_keys: list[str] = residue_before.get(".vscode/settings.json", [])

    report: dict[str, list[str]] = {}
    refusals: list[settings_refusal.Refusal] = []
    project_id = bundle.get("project_id")

    if _SURFACE_CLAUDE_SETTINGS in surfaces_seq:
        path = project_root / ".claude" / "settings.json"
        keys = _write_json_surface(
            project_root, _SURFACE_CLAUDE_SETTINGS, refusals, project_id,
            lambda: _write_json_env_block(
                path, env, canonical_keys, env_key="env",
                user_secret_pairs=us_pairs,
                user_secret_strip_keys=us_strip_keys,
            ),
        )
        if keys is not None:
            report[_SURFACE_CLAUDE_SETTINGS] = keys

    if _SURFACE_CLAUDE_ENV in surfaces_seq:
        path = project_root / ".claude" / "env"
        keys = _write_shell_env_managed_block(
            path,
            env,
            user_secret_pairs=us_pairs,
            # v0.2.91 WP-L: shell-only, operator-yielding keys. This is the
            # ONLY surface that gets them — see SHELL_DEFAULTED_ENV_KEYS.
            defaulted_env=bundle.get("shell_defaulted_env"),
        )
        report[_SURFACE_CLAUDE_ENV] = keys

    if _SURFACE_VSCODE_SETTINGS in surfaces_seq:
        vscode_path = project_root / ".vscode" / "settings.json"
        keys = _write_json_surface(
            project_root, _SURFACE_VSCODE_SETTINGS, refusals, project_id,
            lambda: _write_json_env_block(
                vscode_path, env, canonical_keys, env_key="claude-code.env",
                user_secret_pairs=us_pairs,
                user_secret_strip_keys=vscode_strip_keys,
            ),
        )
        if keys is not None:
            report[_SURFACE_VSCODE_SETTINGS] = keys

    # v0.2.97: `.vscode/settings.json` is not a default surface, but a
    # pre-PR-27 (v0.2.12) launcher wrote its `claude-code.env` block too. When
    # a value VCO can prove it wrote is still there, strip exactly those keys
    # through the same editor (never creates the file; refuses — and records —
    # one it cannot edit).
    if _SURFACE_VSCODE_SETTINGS not in surfaces_seq and vscode_strip_keys:
        try:
            strip_env_keys(project_root, _SURFACE_VSCODE_SETTINGS, vscode_strip_keys)
        except SettingsWriteRefused as exc:
            refusals.extend(exc.refusals)

    # v0.2.84 D2 (P2): emit a `dev_collection_env_repointed` audit row for
    # each audited key whose existing on-disk value the write just changed
    # (old != new, old non-empty). Runs AFTER the surface writes so the
    # audit reflects a completed repoint. Best-effort — never raises.
    _emit_repoint_audit_rows(project_root, old_repoint_values, env)
    _record_secret_value_scrubs(project_root, residue_before, known_secret_keys)

    if refusals:
        raise SettingsWriteRefused(refusals)
    return report


# ─── Pre-v0.2.73 in-tree user-secret VALUES ─────────────────────────────
#
# ``user_secret_values_retained_in_tree`` (a bundle-update deferral owned by
# ``vco_lib.project_init``) and every :func:`apply_project_env` share ONE rule
# (v0.2.97, review R2 F18 — owner rule "never destroy data without positive
# evidence"): an env value in a JSON settings file is VCO's to remove ONLY when
# VCO can PROVE it wrote it — the in-file value EQUALS the value the launcher
# stores for that key, read through the sanctioned resolver (the hub, with its
# per-project active gate) and compared in constant time. A NAME match proves
# nothing: the user can type a key the launcher also knows. Anything else —
# a different value, a paused or unknown key (the hub refuses it: no evidence),
# a resolver that could not answer — is left byte-for-byte and reported by
# ``user_owned_secret_value_in_tree`` (:mod:`vco_lib.user_owned_secrets`).
# The ``.claude/env`` managed block is different: it is VCO's own region
# (between its markers), rebuilt wholesale on every apply.

#: The shared PAT reached every project's env surfaces under this name; its
#: stored slot is ``github_pat``. Canonical, but NEVER removed by name (see
#: :func:`apply_project_env`): only with the same value evidence as the rest.
_LEGACY_SECRET_ENV_KEYS: frozenset[str] = frozenset({"GITHUB_TOKEN"})
_STORED_SLOT_FOR_ENV_KEY: dict[str, str] = {"GITHUB_TOKEN": "github_pat"}

_RETAINED_SECRET_CID = "user_secret_values_retained_in_tree"

#: The JSON env surfaces: ``(file, env-block key)``.
_JSON_SECRET_SURFACES: tuple[tuple[str, str], ...] = (
    (".claude/settings.json", "env"),
    (".vscode/settings.json", "claude-code.env"),
)

#: Evidence verdicts, per in-file key (or, in a ``.env``-style file, per line).
EVIDENCE_PROVEN = "proven"              # the value equals the launcher's stored value
EVIDENCE_NOT_VCO = "not_vco"            # the launcher holds a different value, or none
EVIDENCE_PAUSED = "paused"              # the hub refused: not active for this project
EVIDENCE_UNKNOWN = "unknown"            # the resolver could not answer (hub down, ...)
EVIDENCE_NEVER_STORED = "never_stored"  # a secret-shaped name the launcher never stored

#: The ONE wording of each verdict, used in every report (the Python deferral
#: entries and — handed over the bridge — the launcher's unregister result).
EVIDENCE_REASONS: dict[str, str] = {
    EVIDENCE_PROVEN: "it equals the value the launcher stores (VCO wrote it)",
    EVIDENCE_NOT_VCO: "it is not the value the launcher stores for it",
    EVIDENCE_PAUSED: (
        "VCO could not check it — the launcher's copy is paused (or not "
        "granted) for this project"
    ),
    EVIDENCE_UNKNOWN: "VCO could not check it — the launcher could not be asked",
    EVIDENCE_NEVER_STORED: "the launcher never stored a secret with this name",
}

#: ``export KEY="value"`` inside the ``.claude/env`` managed block — the shape
#: the projection writer emits. Only ``bool(value)`` is ever used.
_MANAGED_EXPORT_RE = re.compile(
    r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"', re.MULTILINE,
)


def _stored_secret_value(env_key: str, project_root: Path) -> tuple[str, Optional[str]]:
    """What the launcher stores for ``env_key``, through the sanctioned resolver.

    ``("ok", value)``; ``("paused", None)`` when the hub refused the key as not
    active for this project (a paused secret is NOT read around the permission
    matrix — no evidence either way); ``("absent", None)`` when the launcher
    holds no value for it; ``("unknown", None)`` on any resolver failure. Hub
    tier ONLY (``allow_file_fallback=False``): the pre-v0.2.73 writer copied the
    keychain value, so the keychain is the only store whose value is evidence.
    The value never leaves this process and is never logged — the resolver's
    errors name keys, not values.
    """
    from vco_lib import agent_secrets

    slot = _STORED_SLOT_FOR_ENV_KEY.get(env_key, env_key)
    try:
        return "ok", agent_secrets.get(slot, project=str(project_root), allow_file_fallback=False)
    except agent_secrets.AccessDenied:
        return "paused", None
    except agent_secrets.SecretNotFound:
        return "absent", None
    except Exception:  # noqa: BLE001 — any resolver failure is "no evidence"
        return "unknown", None


def _json_env_block(path: Path, env_key: str) -> dict:
    loaded = jsonc_edit.load_object(path) if path.is_file() else None
    block = loaded[0].get(env_key) if loaded is not None else None
    return block if isinstance(block, dict) else {}


def classify_json_env_secrets(
    folder: Path, *, known_keys: Optional[Iterable[str]] = None,
) -> dict[str, dict[str, str]]:
    """``{file: {KEY: verdict}}`` for every candidate in the JSON env blocks.

    A candidate is a key with a non-empty string value that is either a
    launcher-known user secret / ``GITHUB_TOKEN`` (verdict by value evidence,
    :func:`_stored_secret_value`, compared with :func:`hmac.compare_digest`) or
    merely secret-SHAPED (:func:`vco_lib.secrets_audit.is_secret_shaped_env_key`
    — ``never_stored``). VCO's other canonical keys are never candidates.
    Values are compared and dropped; only verdicts leave this function.
    """
    from vco_lib.secrets_audit import is_secret_shaped_env_key

    folder = Path(folder)
    vco_names = _vco_secret_names(folder, known_keys)
    other_canonical = list_canonical_keys() - _LEGACY_SECRET_ENV_KEYS
    lookups: dict[str, tuple[str, Optional[str]]] = {}
    verdicts: dict[str, dict[str, str]] = {}
    for rel, env_key in _JSON_SECRET_SURFACES:
        for name, value in _json_env_block(folder / rel, env_key).items():
            if name in other_canonical or not isinstance(value, str) or not value:
                continue
            if name in vco_names:
                verdict = _value_verdict(name, value, folder, lookups)
            elif is_secret_shaped_env_key(name):
                verdict = EVIDENCE_NEVER_STORED
            else:
                continue
            verdicts.setdefault(rel, {})[name] = verdict
    return verdicts


def _vco_secret_names(folder: Path, known_keys: Optional[Iterable[str]]) -> set[str]:
    known = known_user_secret_keys_for_folder(folder) if known_keys is None else list(known_keys)
    return set(known) | _LEGACY_SECRET_ENV_KEYS


def _value_verdict(
    name: str, value: str, folder: Path, lookups: dict[str, tuple[str, Optional[str]]],
) -> str:
    """The evidence verdict for one in-file ``value`` of a launcher-known key:
    ``proven`` only when it equals the stored value (constant-time compare).
    ``lookups`` caches one resolver answer per key for the caller's pass; the
    stored value never leaves the caller's process."""
    import hmac

    if name not in lookups:
        lookups[name] = _stored_secret_value(name, folder)
    status, stored = lookups[name]
    if status == "ok" and stored and hmac.compare_digest(
        value.encode("utf-8"), stored.encode("utf-8"),
    ):
        return EVIDENCE_PROVEN
    return {
        "paused": EVIDENCE_PAUSED, "unknown": EVIDENCE_UNKNOWN,
    }.get(status, EVIDENCE_NOT_VCO)


def _strip_proven_env_lines(
    path: Path, vco_names: set[str], folder: Path,
    lookups: dict[str, tuple[str, Optional[str]]], *, managed_block: bool,
) -> tuple[list[str], dict[str, str], Optional[str]]:
    """Drop from one ``.env``-style file every line whose OWN value is proven;
    ``(proven names, {name: verdict of a line left}, error or None)`` — with an
    error, the proven names are the ones that were NOT removed.

    Per OCCURRENCE (review R3 F23): a file may carry a key twice — a line the
    user wrote above one VCO wrote — and only the line whose value equals the
    stored one goes. Lines are read with the ONE line grammar,
    :func:`vco_lib.envfile.parse_env_line`; comments, blank lines and — when
    ``managed_block`` (``.claude/env``) — every line of VCO's managed block
    (located by the ONE extractor, :func:`vco_lib.envfile.extract_managed_block`;
    VCO's region, handled whole by the unregister) are kept. Every other byte,
    CRLF included, is kept, and so is the file's MODE (review R4 F29).

    A failed REWRITE is not raised (review R4 F25): the file is untouched (the
    write is atomic), and the error comes back with the proven names so the
    caller can report exactly what was NOT removed and move on to the next
    surface.
    """
    from vco_lib.envfile import extract_managed_block, parse_env_line

    try:
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, ValueError):
        return [], {}, None
    skip_from, skip_to = len(text), len(text)
    block = (
        extract_managed_block(text, CLAUDE_ENV_MANAGED_BEGIN, CLAUDE_ENV_MANAGED_END)
        if managed_block else None
    )
    if block is not None:
        skip_from = text.find(block)
        skip_to = skip_from + len(block) + len(CLAUDE_ENV_MANAGED_END)
    kept: list[str] = []
    removed: list[str] = []
    left: dict[str, str] = {}
    offset = 0
    for line in text.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        pair = None if skip_from <= start < skip_to else parse_env_line(line)
        if pair is not None and pair[0] in vco_names and pair[1]:
            verdict = _value_verdict(pair[0], pair[1], folder, lookups)
            if verdict == EVIDENCE_PROVEN:
                removed.append(pair[0])
                continue
            left[pair[0]] = verdict
        kept.append(line)
    if removed:
        try:
            _atomic_write_text(path, "".join(kept))
        except OSError as exc:
            return sorted(set(removed)), left, f"could not rewrite {path}: {exc.strerror or exc}"
    return sorted(set(removed)), left, None


def strip_proven_secret_values(
    folder: Path, *, known_keys: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Remove every secret VALUE VCO can PROVE it wrote from the four env
    files a pre-v0.2.73 launcher wrote; report the rest. The unregister's
    evidence step (v0.2.97): a confirmed "unregister" consents to removing
    what VCO wrote, not a same-named key the user typed.

    Candidates are the launcher-known user secrets (every bucket) and
    ``GITHUB_TOKEN``. ``.env`` / ``.claude/env`` (outside the managed block)
    are edited per LINE — only a line whose own value equals the stored one
    goes; the JSON env blocks per key, through :func:`strip_env_keys` (JSONC
    in place, refusal recorded). Names the launcher never stored are not
    touched or listed — they were never VCO's.

    Returns ``{"removed": {file: [KEY]}, "left": {file: {KEY: verdict}},
    "not_removed": {file: [KEY]}, "errors": [message]}`` — names and verdicts
    only, never a value. ``not_removed`` lists PROVEN names whose removal
    failed (a rewrite error, a refused JSONC edit); each surface is attempted
    regardless of an earlier one's failure (review R4 F25).
    """
    folder = Path(folder)
    vco_names = _vco_secret_names(folder, known_keys)
    lookups: dict[str, tuple[str, Optional[str]]] = {}
    removed: dict[str, list[str]] = {}
    left: dict[str, dict[str, str]] = {}
    not_removed: dict[str, list[str]] = {}
    errors: list[str] = []
    for rel in (".env", ".claude/env"):
        gone, kept, error = _strip_proven_env_lines(
            folder / rel, vco_names, folder, lookups, managed_block=rel == ".claude/env",
        )
        if gone and error:
            errors.append(error)
            not_removed[rel] = gone
        elif gone:
            removed[rel] = gone
        if kept:
            left[rel] = kept
    for rel, env_key in _JSON_SECRET_SURFACES:
        proven: list[str] = []
        for name, value in _json_env_block(folder / rel, env_key).items():
            if name in vco_names and isinstance(value, str) and value:
                verdict = _value_verdict(name, value, folder, lookups)
                if verdict == EVIDENCE_PROVEN:
                    proven.append(name)
                else:
                    left.setdefault(rel, {})[name] = verdict
        if proven:
            surface = next(s for s, (r, _k) in _JSON_SURFACE_FILES.items() if r == rel)
            try:
                gone = strip_env_keys(folder, surface, proven)
            except (SettingsWriteRefused, OSError) as exc:
                errors.append(f"{rel}: {exc}")
                not_removed[rel] = sorted(proven)
                gone = []
            if gone:
                removed[rel] = gone
    return {"removed": removed, "left": left, "not_removed": not_removed, "errors": errors}


def _managed_block_secret_exports(path: Path) -> list[str]:
    """Secret-shaped exports with a value inside ``.claude/env``'s managed block
    (VCO's own region). The block is located by the ONE extractor,
    :func:`vco_lib.envfile.extract_managed_block` — the END that closes the
    block is the first one AFTER its BEGIN (review R2 F21)."""
    from vco_lib.envfile import extract_managed_block
    from vco_lib.secrets_audit import is_secret_shaped_env_key

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    block = extract_managed_block(text, CLAUDE_ENV_MANAGED_BEGIN, CLAUDE_ENV_MANAGED_END)
    if block is None:
        return []
    return sorted({
        key for key, value in _MANAGED_EXPORT_RE.findall(block)
        if value and is_secret_shaped_env_key(key)
    })


def retained_secret_keys_in(path: Path, known_keys: Iterable[str] = ()) -> list[str]:
    """NAMES of values in one env surface that VCO can prove it wrote.

    ``.claude/env``: secret-shaped exports inside VCO's managed block. A JSON
    settings file: keys whose value EQUALS the launcher's stored value
    (:func:`classify_json_env_secrets`). Never reads a value out. Soft: ``[]``
    for a missing or unreadable file.
    """
    if not path.is_file():
        return []
    if path.name == "env":
        return _managed_block_secret_exports(path)
    folder = path.parent.parent
    rel = path.relative_to(folder).as_posix()
    verdicts = classify_json_env_secrets(folder, known_keys=known_keys).get(rel, {})
    return sorted(k for k, v in verdicts.items() if v == EVIDENCE_PROVEN)


def known_user_secret_keys_for_folder(folder: Path) -> list[str]:
    """The launcher-known user-secret keys of the project registered at
    ``folder``; ``[]`` when there is no launcher DB or the folder is not a
    registered project (there is then no refresh to run either)."""
    from vco_lib.module_gated_delivery import resolve_project_id_for_folder

    db_path = _resolve_launcher_db_path()
    if not db_path.is_file():
        return []
    project_id = resolve_project_id_for_folder(Path(folder), db_path=db_path)
    if not project_id:
        return []
    try:
        return user_secret_known_keys_from_db(project_id, db_path=db_path)
    except (DbUnreachable, sqlite3.Error):
        return []


def retained_launcher_value_names(
    folder: Path,
    *,
    known_keys: Optional[Iterable[str]] = None,
    verdicts: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> dict[str, list[str]]:
    """``{surface: [key NAMES]}`` of values VCO can prove it wrote that are
    still in the project's env surfaces; empty when there are none.

    The ONE detection behind ``user_secret_values_retained_in_tree``: its
    emitter and bundle reconciler call this, and :func:`apply_project_env`
    removes exactly this set, so the entry never promises a removal the
    refresh does not make. ``verdicts`` reuses a classification already made.
    """
    folder = Path(folder)
    if verdicts is None:
        verdicts = classify_json_env_secrets(folder, known_keys=known_keys)
    found: dict[str, list[str]] = {}
    managed = _managed_block_secret_exports(folder / ".claude" / "env")
    if managed:
        found[".claude/env"] = managed
    for rel, _env_key in _JSON_SECRET_SURFACES:
        names = sorted(k for k, v in verdicts.get(rel, {}).items() if v == EVIDENCE_PROVEN)
        if names:
            found[rel] = names
    return found


def retained_user_secret_state(folder: Path) -> Optional[bool]:
    """The reconciler's tri-state: ``True`` — a provably VCO-written value
    remains; ``None`` — none proven, but a launcher-known key's value could not
    be checked (keep the entry: no evidence it is over); ``False`` — clean."""
    verdicts = classify_json_env_secrets(Path(folder))
    if retained_launcher_value_names(folder, verdicts=verdicts):
        return True
    if any(
        v in (EVIDENCE_UNKNOWN, EVIDENCE_PAUSED) for per in verdicts.values() for v in per.values()
    ):
        return None
    return False


def _record_secret_value_scrubs(
    project_root: Path, before: Mapping[str, list[str]], known_keys: list[str],
) -> None:
    """Trail each value this apply removed (key NAME only, never the value)
    and clear ``user_secret_values_retained_in_tree`` once nothing remains.
    Best-effort: observability never fails a write that already landed."""
    try:
        from vco_lib.deferral_emit import record_auto_resolution, resolve_conditions
        from vco_lib.deferral_report import DeferralReport

        after = retained_launcher_value_names(project_root, known_keys=known_keys)
        for rel, names in before.items():
            for name in sorted(set(names) - set(after.get(rel, []))):
                record_auto_resolution(
                    project_root, _RETAINED_SECRET_CID, "scrubbed_user_secret_value",
                    f"removed the in-tree value of {name} from {rel} (it equalled the "
                    "value the launcher stores); the secret itself stays in the keychain",
                )
        if not after and DeferralReport.read(project_root).has_condition(_RETAINED_SECRET_CID):
            resolve_conditions(project_root, [_RETAINED_SECRET_CID])
    except Exception:  # noqa: BLE001 — trail + ledger are observability
        pass


# ─── Update-time migration: re-project ALL registered projects ──────────
#
# GAP-CG-1 (2026-07-14) changed the FORM of the value emitted into
# ``VCT_CODE_GRAPH_ACCESS_LIST`` (grantor slug → grantor NAME). Existing
# installs carry the stale slug form on disk until the next re-projection.
# ``apply_project_env`` already OVERWRITES canonical keys with the
# DB-resolved value, so a single re-projection pass per project migrates
# the value automatically — no bespoke slug→name rewriter needed. This
# helper is the ALL-PROJECTS driver the orchestrator update flow calls so
# every project's on-disk env is brought to the name-based form in one
# pass, with a per-project deferral on failure (never a silent half-
# migration). It REUSES the existing projection machinery
# (``list_registered_projects`` + ``project_env_from_db`` +
# ``apply_project_env``) — it is NOT a second projector.


class ProjectReprojectOutcome(TypedDict):
    """Per-project result of :func:`reproject_all_registered_projects`."""

    project_id: str
    project_name: str
    status: str  # "migrated" | "failed" | "refused" | "skipped"
    detail: str
    keys_written: list[str]


def reproject_all_registered_projects(
    *,
    db_path: Path | None = None,
    surfaces: Iterable[str] | None = None,
    deferral_report: Any | None = None,
    log_event: Any | None = None,
) -> list[ProjectReprojectOutcome]:
    """Re-project every registered project's canonical env from launcher.db.

    The single ALL-PROJECTS driver for the orchestrator update flow.
    After GAP-CG-1 flipped the ``VCT_CODE_GRAPH_ACCESS_LIST`` producer
    from grantor-slug to grantor-name, this pass migrates every project's
    on-disk env (``.claude/settings.json`` + ``.claude/env``) to the new
    name-based form by delegating to the canonical projection machinery
    — ``project_env_from_db`` (which now emits names) +
    ``apply_project_env`` (which OVERWRITES the canonical key). No
    per-slug rewriting: correctness follows from re-running the producer.

    Failure discipline (USER decision 2026-07-14): on ANY per-project
    failure, emit a deferral entry naming the affected project via the
    canonical deferral system (``deferral_report`` argument →
    ``deferral_report.safe_emit_entry``) rather than half-migrating
    silently. A single project's failure never aborts the sweep — the
    remaining projects still migrate, and each failure gets its own
    deferral row + ``"failed"`` outcome.

    v0.2.97 — a settings file the writer would not touch is NOT that
    failure: :class:`SettingsWriteRefused` means the projection left an
    unparseable / uneditable ``settings.json`` byte-identical and has ALREADY
    recorded the accurate condition, ``settings_write_refused_<surface>``, in
    THAT project's ledger. Such a project gets a ``"refused"`` outcome and no
    ``codegraph_access_list_reprojection_failed`` entry, whose wording (a
    code-graph migration failure, fixed by re-running ``reproject-all``)
    would send the user after the wrong cause.

    Cleanup semantics: the migration IS the cleanup. Re-projecting a
    project OVERWRITES the stale slug-form ``VCT_CODE_GRAPH_ACCESS_LIST``
    with the name-form value (and DROPS the key entirely when the project
    has no code-graph grants — ``project_env_from_db`` omits empty keys
    and ``apply_project_env`` treats omission as signal-to-remove). So no
    separate dead-slug-data deletion step is needed: the stale value only
    lives in the env surfaces this pass rewrites, and it is gone the
    moment migration succeeds for that project. Projects whose migration
    FAILED keep their old (working-or-not) env untouched — the deferral
    tells the user to re-run ``python -m vco_lib.config_projection
    reproject-all`` once the underlying cause (unreachable project folder,
    permission error) is fixed.

    Args:
        db_path: Optional launcher.db override (tests pin this).
        surfaces: Optional surface list forwarded to ``apply_project_env``
            (defaults to the canonical settings.json + .claude/env pair).
        deferral_report: Optional ``DeferralReport`` accumulator. When
            provided, per-project failures emit a ``codegraph_access_list_
            reprojection_failed`` deferral entry. ``None`` → no deferral
            (the ``"failed"`` outcome still records the cause in-memory).
        log_event: Optional ``(step, phase, detail)`` logger forwarded to
            ``safe_emit_entry`` for its own soft-fail path.

    Returns:
        One :class:`ProjectReprojectOutcome` per registered project, in
        the DB's deterministic ``ORDER BY name`` order.

    Raises:
        DbUnreachable: when the launcher DB itself is missing/unopenable
            (a whole-sweep precondition failure — distinct from a single
            project's failure, which is caught + deferred). The caller's
            update flow should treat this as "launcher never booted" and
            skip the migration (matching ``_backfill_code_graph_project_
            env``'s ``db_unreachable`` no-op posture).
    """
    # list_registered_projects raises DbUnreachable on a missing DB — let
    # it propagate (whole-sweep precondition, not a per-project failure).
    projects = list_registered_projects(db_path=db_path)

    # Deferral emitter is imported lazily + soft: a missing deferral_report
    # module must not break the migration (the sweep still runs, failures
    # still record their cause in the returned outcomes).
    _safe_emit_entry = None
    if deferral_report is not None:
        try:
            from vco_lib.deferral_report import safe_emit_entry as _safe_emit_entry
        except Exception:  # noqa: BLE001 — deferral is best-effort telemetry
            _safe_emit_entry = None

    outcomes: list[ProjectReprojectOutcome] = []
    for proj in projects:
        pid = str(proj.get("id", ""))
        pname = str(proj.get("name", "")) or pid
        try:
            bundle = project_env_from_db(pid, db_path=db_path)
            report = apply_project_env(bundle, surfaces=surfaces)
            keys_written: set[str] = set()
            for _surface_keys in report.values():
                keys_written.update(_surface_keys)
            outcomes.append(
                ProjectReprojectOutcome(
                    project_id=pid,
                    project_name=pname,
                    status="migrated",
                    detail="re-projected canonical env (name-based access list)",
                    keys_written=sorted(keys_written),
                )
            )
        except SettingsWriteRefused as exc:
            outcomes.append(
                ProjectReprojectOutcome(
                    project_id=pid,
                    project_name=pname,
                    status="refused",
                    detail=str(exc),
                    keys_written=[],
                )
            )
        except Exception as exc:  # noqa: BLE001 — one project's failure never aborts the sweep
            detail = f"{type(exc).__name__}: {exc}"
            outcomes.append(
                ProjectReprojectOutcome(
                    project_id=pid,
                    project_name=pname,
                    status="failed",
                    detail=detail,
                    keys_written=[],
                )
            )
            if _safe_emit_entry is not None:
                _safe_emit_entry(
                    deferral_report,
                    condition_id="codegraph_access_list_reprojection_failed",
                    title=(
                        f"code-graph access-list migration failed for "
                        f"project {pname!r}"
                    ),
                    detected=(
                        f"re-projecting {pname!r} (id={pid}) to migrate its "
                        f"VCT_CODE_GRAPH_ACCESS_LIST from grantor-slug to "
                        f"grantor-name form failed: {detail}"
                    ),
                    why_deferred=(
                        "The orchestrator update flipped the code-graph "
                        "access-list identity form (slug → name, GAP-CG-1). "
                        "This project could not be re-projected, so its "
                        "VCT_CODE_GRAPH_ACCESS_LIST may still carry stale "
                        "slug-form entries — cross-project code-graph reads "
                        "from it will silently return zero peer rows until "
                        "re-projection succeeds."
                    ),
                    command_to_apply=(
                        "python -m vco_lib.config_projection reproject-all"
                    ),
                    severity="warning",
                    log_event=log_event,
                    log_step="codegraph-access-list-migration",
                )
    return outcomes


def _write_json_env_block(
    path: Path,
    canonical_env: Mapping[str, str],
    canonical_keys: Iterable[str],
    *,
    env_key: str,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
    user_secret_strip_keys: Iterable[str] | None = None,
) -> list[str]:
    """Write the canonical env into a JSON file's ``<env_key>`` sub-block.

    Deep-merge contract (inherited from the Rust writer's deep-merge,
    retired v0.2.97):

      * Read the existing JSON (if present). Treat missing file as
        ``{}``. A JSONC file (comments / trailing commas — VS Code's own
        format for ``.vscode/settings.json``) is READ and later edited in
        place, comments kept (v0.2.97, :mod:`vco_lib.jsonc_edit`). A file
        that exists but is not JSONC either, is not UTF-8, cannot be read,
        or has a non-object root is NEVER written:
        :class:`SettingsWriteRefused` is raised and the file stays
        byte-identical (v0.2.97, :mod:`vco_lib.settings_refusal`). Before
        v0.2.97 each of those cases was treated as ``{}`` and rewritten as
        only the env block — every other setting in it destroyed.
      * Locate the ``env_key`` sub-block. If missing or not an object,
        create a fresh object.
      * For each canonical key in ``canonical_keys``:
          - if present in ``canonical_env``: set ``env[key] = value``
            (string).
          - if absent from ``canonical_env``: delete ``env[key]`` if
            present (signal-to-remove semantics; supports "the launcher
            decided this project no longer has any peer KG access").
      * User-secret keys (v0.2.97, review R4 F26):
          - ``user_secret_strip_keys`` are the names whose in-file value
            the CALLER PROVED equals the launcher's stored value
            (:func:`classify_json_env_secrets`) — a paused, unknown or
            different value is never in it. They are removed before the
            canonical keys are applied.
          - ``user_secret_pairs`` is always empty since v0.2.73: VCO never
            writes a secret value. The parameter survives only so the
            call shape stays stable; a non-empty list would be inserted
            after the canonical keys.
      * Non-canonical, non-user-secret keys (user-added by hand
        directly in the JSON) are PRESERVED untouched.
      * Write the result back with 2-space indent, no trailing newline,
        ``ensure_ascii=False`` (matching Rust's
        ``serde_json::to_string_pretty`` byte layout) — for a strict-JSON
        original. A JSONC original is edited member by member instead and
        verified by re-parsing; an edit that cannot be verified writes
        NOTHING and raises :class:`SettingsWriteRefused`.

    Returns the sorted list of canonical keys whose value was set
    (those that were deleted are not listed — the audit consumer wants
    to see what's NOW exported, not what was previously there). User-
    secret keys are NOT in the returned list (audit reporting for
    user secrets is the auto-resolution trail; see
    :func:`_record_secret_value_scrubs`).

    Atomic write: writes to a tempfile in the same directory as ``path``,
    then ``os.replace``-s into place. This is atomic on POSIX (rename
    on the same filesystem) and on Windows 10+ (NTFS rename is
    transactional for same-volume).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Read-merge-write.
    existing_root, jsonc_text = _read_json_env_root(path)

    env_block_raw = existing_root.get(env_key)
    if not isinstance(env_block_raw, dict):
        env_block: dict[str, Any] = {}
    else:
        env_block = dict(env_block_raw)  # defensive copy

    # Remove the user-secret keys whose value the caller PROVED VCO wrote
    # (equal to the launcher's stored value; never a paused / unknown /
    # different one), before the canonical keys are applied.
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

    # `user_secret_pairs` is always empty since v0.2.73 (VCO never writes a
    # secret value); kept only for the call shape.
    if user_secret_pairs:
        for k, v in user_secret_pairs:
            env_block[k] = v

    existing_root[env_key] = env_block

    # Atomic write. ``ensure_ascii=False`` matches Rust's serde_json
    # which passes Unicode through verbatim. 2-space indent matches
    # ``serde_json::to_string_pretty``. No trailing newline matches
    # Rust's ``std::fs::write(&path, pretty)`` where ``pretty`` has
    # no trailing newline (verified by reading projects_v2.rs L1896-1898).
    _atomic_write_text(path, _serialise_json_env_root(path, existing_root, jsonc_text))

    written_keys.sort()
    return written_keys


def _read_json_env_root(path: Path) -> tuple[dict[str, Any], Optional[str]]:
    """``(root, jsonc_text)`` for a JSON env surface — the ONE reader both
    JSON writers use.

    ``jsonc_text`` is the original text when the file is JSONC (valid only
    with comments / trailing commas): the writer must then EDIT that text
    (:func:`_serialise_json_env_root`), never re-serialise it. A missing
    file is ``{}`` (the writer creates it). A file that exists but cannot be
    read as an object raises :class:`SettingsWriteRefused` — it is never
    treated as ``{}``, because writing ``{}`` plus the env block back is
    exactly how every other setting in it used to be destroyed.
    """
    loaded = settings_refusal.load_for_edit(path)
    if loaded is None:
        return {}, None
    if isinstance(loaded, settings_refusal.Refusal):
        raise SettingsWriteRefused([loaded])
    parsed, raw = loaded
    return parsed, (None if jsonc_edit.is_strict_json(raw) else raw)


def _serialise_json_env_root(
    path: Path, root: Mapping[str, Any], jsonc_text: Optional[str],
) -> str:
    """The bytes to write.

    Strict JSON (or a fresh file): the Rust-parity layout. JSONC: the
    original text edited member by member and verified
    (:func:`vco_lib.jsonc_edit.rewrite_preserving`); an edit that cannot be
    verified raises :class:`SettingsWriteRefused` and the file is left
    exactly as it was.
    """
    if jsonc_text is None:
        return json.dumps(root, indent=2, ensure_ascii=False)
    try:
        return jsonc_edit.rewrite_preserving(jsonc_text, root)
    except jsonc_edit.JsoncEditRefused as exc:
        raise SettingsWriteRefused([settings_refusal.Refusal(
            path, settings_refusal.KIND_EDIT_REFUSED,
            "it has comments or trailing commas (JSONC) and this change could "
            f"not be made in place without risking other content ({exc.message}); "
            "edit it by hand, or remove the comments",
        )]) from exc


def _write_json_surface(
    project_root: Path,
    surface: str,
    refusals: list["settings_refusal.Refusal"],
    project_id: Optional[str],
    write: Any,
) -> Any:
    """Run one JSON surface's ``write()``; a refusal is recorded, not raised.

    The caller raises :class:`SettingsWriteRefused` once every requested
    surface has had its turn, so one unreadable file never blocks the
    others. A refusal is left in the project's deferral ledger
    (:func:`vco_lib.settings_refusal.record`); a successful write clears a
    refusal recorded for the same surface earlier. Returns ``write()``'s
    result, or ``None`` when the surface was refused.
    """
    try:
        result = write()
    except SettingsWriteRefused as exc:
        for refusal in exc.refusals:
            settings_refusal.record(project_root, surface, refusal, project_id=project_id)
        refusals.extend(exc.refusals)
        return None
    settings_refusal.clear_recorded(project_root, surface)
    return result


def write_env_block(
    project_folder: Path,
    surface: str,
    values: Mapping[str, str],
    owned_keys: Iterable[str],
) -> list[str]:
    """Set ``values`` in one JSON surface's env block; remove every other
    ``owned_keys`` member; leave all other keys (and, for JSONC, bytes) alone.

    The ONE implementation of a surgical env-block edit, for writers that own
    a key set outside the canonical projection — the launcher's module-
    deprecation keys and the orchestrator root's MCP-setting keys
    (``module_deprecation.rs`` / ``dashboard.rs`` call it through the
    ``write-env-block`` CLI instead of carrying a second, Rust, copy). Same
    read-merge-write, JSONC handling and refusal as :func:`apply_project_env`.
    Its removal-only twin is :func:`strip_env_keys`.

    Keys new to the block are appended in ``values`` order, then
    ``owned_keys`` order — never in set-iteration order, which would vary
    with the interpreter's hash seed from one run to the next.

    Raises:
        ConfigProjectionError: an unknown surface, a value that is not a
            string, or a key in ``values`` that ``owned_keys`` does not own.
        SettingsWriteRefused: the file exists but cannot be edited safely;
            it was left byte-identical and the refusal recorded.
    """
    if surface not in _JSON_SURFACE_FILES:
        raise ConfigProjectionError(
            f"unknown JSON surface {surface!r}; valid: {sorted(_JSON_SURFACE_FILES)}"
        )
    owned = set(owned_keys)
    stray = sorted(set(values) - owned)
    if stray:
        raise ConfigProjectionError(f"keys not in owned_keys: {stray}")
    if not all(isinstance(v, str) for v in values.values()):
        raise ConfigProjectionError("every value must be a string")
    rel, env_key = _JSON_SURFACE_FILES[surface]
    ordered = list(dict.fromkeys([*values, *owned_keys]))
    refusals: list[settings_refusal.Refusal] = []
    written = _write_json_surface(
        project_folder, surface, refusals, None,
        lambda: _write_json_env_block(
            project_folder / rel, values, ordered, env_key=env_key,
        ),
    )
    if refusals:
        raise SettingsWriteRefused(refusals)
    return written


def strip_env_keys(
    project_folder: Path, surface: str, keys: Iterable[str],
) -> list[str]:
    """Remove ``keys`` from one JSON surface's env block; nothing else.

    The removal-only twin of :func:`write_env_block`: every ``apply`` uses
    it for the proven secret values in ``.vscode/settings.json``, and the
    unregister's value-checked twin is :func:`strip_env_keys_holding`.
    Unlike a write it never CREATES anything: a missing file, a missing env
    block, or an env block holding none of ``keys`` is left alone and costs
    no write. An env block the strip empties is removed, so no ``"env": {}``
    is left behind. JSONC is edited in place; a file that cannot be edited
    safely is refused and recorded exactly as a write would be.

    Returns the sorted keys actually removed.

    Raises:
        ConfigProjectionError: an unknown surface.
        SettingsWriteRefused: as :func:`write_env_block`.
    """
    wanted = set(keys)
    return _strip_json_env(project_folder, surface, lambda key, _value: key in wanted)[0]


def strip_env_keys_holding(
    project_folder: Path, surface: str, expected: Mapping[str, str], keys: Iterable[str],
) -> tuple[list[str], list[str]]:
    """The evidence-rule strip (v0.2.97 review R6): of ``keys``, remove from
    one JSON surface's env block only those whose value EQUALS ``expected``
    (what VCO projects for this project); a key holding anything else is the
    user's and stays. Returns ``(removed, left)`` — sorted key names; ``left``
    are the ``keys`` present with a different value (or none expected).
    Same file discipline and refusals as :func:`strip_env_keys`.
    """
    wanted = set(keys)
    return _strip_json_env(
        project_folder, surface,
        lambda key, value: key in wanted and key in expected and value == expected[key],
        report=wanted,
    )


def _strip_json_env(
    project_folder: Path,
    surface: str,
    remove: Callable[[str, Any], bool],
    *,
    report: "set[str] | None" = None,
) -> tuple[list[str], list[str]]:
    """Remove from one JSON surface's env block every entry ``remove(key,
    value)`` accepts — the ONE removal core of :func:`strip_env_keys` and
    :func:`strip_env_keys_holding`. Returns ``(removed, left)``: ``left`` =
    the ``report`` keys present but not removed. Never creates a file; drops
    an env block it empties; a file it cannot edit safely is refused and
    recorded (:class:`SettingsWriteRefused`).
    """
    if surface not in _JSON_SURFACE_FILES:
        raise ConfigProjectionError(
            f"unknown JSON surface {surface!r}; valid: {sorted(_JSON_SURFACE_FILES)}"
        )
    rel, env_key = _JSON_SURFACE_FILES[surface]
    path = project_folder / rel
    if not path.exists():
        return [], []

    def _strip() -> tuple[list[str], list[str]]:
        root, jsonc_text = _read_json_env_root(path)
        block = root.get(env_key)
        if not isinstance(block, dict):
            return [], []
        removed = sorted(k for k, v in block.items() if remove(k, v))
        left = sorted(k for k in block if k in (report or ()) and k not in removed)
        if not removed:
            return [], left
        remaining = {k: v for k, v in block.items() if k not in removed}
        if remaining:
            root[env_key] = remaining
        else:
            del root[env_key]
        _atomic_write_text(path, _serialise_json_env_root(path, root, jsonc_text))
        return removed, left

    refusals: list[settings_refusal.Refusal] = []
    outcome = _write_json_surface(project_folder, surface, refusals, None, _strip)
    if refusals:
        raise SettingsWriteRefused(refusals)
    return outcome


def _write_shell_env_managed_block(
    path: Path,
    canonical_env: Mapping[str, str],
    *,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
    defaulted_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Write the canonical env between bracket markers in ``.claude/env``.

    Behaviour (the splice is :func:`_merge_managed_block`):

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
    the retired Rust block builder's output (v0.2.97).

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

    managed = _build_managed_block(
        canonical_env,
        user_secret_pairs=user_secret_pairs,
        defaulted_env=defaulted_env,
    )
    new_text = _merge_managed_block(prior, managed)
    _atomic_write_text(path, new_text)

    return sorted(list(canonical_env.keys()) + list((defaulted_env or {}).keys()))


def _build_managed_block(
    canonical_env: Mapping[str, str],
    *,
    user_secret_pairs: Iterable[tuple[str, str]] | None = None,
    defaulted_env: Mapping[str, str] | None = None,
) -> str:
    """Render the managed block for ``.claude/env``.

    Format (byte-identical to the retired Rust block builder's output):

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
    to the retired Rust block builder's output.
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
    # v0.2.91 WP-L — shell-DEFAULTED keys (currently just VCO_LOG_LEVEL).
    #
    # `export K="${K:-v}"` instead of `export K="v"`: the projected value
    # fills in only when the environment is silent, so an operator who
    # exported the variable in their shell keeps it after sourcing this file.
    # That is the difference between per-project CONFIG (where the projection
    # is the authority and reconciles a hand-set value away) and an
    # operator-override DEBUG knob (where the person who exported it is).
    #
    # This section was the one deliberate divergence from the retired Rust
    # block builder: the Rust writer had no defaulted form, and did not need one — the Python `apply` CLI is
    # the canonical writer for these keys (the Option-A interop strategy at
    # the top of this module), exactly as it already is for DUAL_* / the
    # code-graph floors / the RL globals.
    #
    # Emitted AFTER the canonical exports so the shape is stable, and before
    # the user-secret section so secrets stay last (their block is the one a
    # reader scans for).
    for key, value in (defaulted_env or {}).items():
        escaped = value.replace('"', '\\"')
        out.append(f'export {key}="${{{key}:-{escaped}}}"')
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

    The one splice (its Rust mirror retired in v0.2.97 review R6 with its
    only caller, the unregister strip — now :mod:`vco_lib.unregister_env`):

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

    Thin delegate to :func:`vco_lib.atomic.atomic_rewrite_text` (v0.2.54
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

    v0.2.97 (review R4 F29): an EXISTING file keeps its permission bits —
    ``mkstemp`` creates the replacement 0600, so without this a group-readable
    ``.env`` / settings file came out unreadable to its group. A new file is
    created as before. One home: :func:`vco_lib.atomic.atomic_rewrite_text`.
    """
    atomic_rewrite_text(path, content)


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
            weaviate_url_override=args.weaviate_url,
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
    except SettingsWriteRefused as exc:
        print(json.dumps(_refused_payload(exc)), file=sys.stderr)
        return 4
    except ConfigProjectionError as exc:
        print(json.dumps({"error": "apply_failed", "message": str(exc)}),
              file=sys.stderr)
        return 4

    print(json.dumps({"ok": True, "report": report, "project_id": args.project_id,
                      "project_root": str(bundle["project_root"])}))
    return 0


def _refused_payload(exc: SettingsWriteRefused) -> dict[str, Any]:
    """The machine form of a refusal, shared by every CLI verb that writes."""
    return {
        "error": "settings_write_refused",
        "message": str(exc),
        "refused": [r.as_json() for r in exc.refusals],
    }


def _cli_write_env_block(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection write-env-block``.

    stdin: ``{"set": {KEY: "value", ...}, "owned_keys": [KEY, ...]}``. stdout
    carries exactly ONE JSON object on every path (``ok`` true/false), which
    is what the launcher parses. Exit codes: 0 written, 2 bad request,
    4 refused or failed (a refused file is left byte-identical).
    """
    try:
        request = json.loads(sys.stdin.read() or "{}")
        values = request.get("set", {}) if isinstance(request, dict) else None
        owned = request.get("owned_keys", []) if isinstance(request, dict) else None
        if not isinstance(values, dict) or not isinstance(owned, list):
            raise ConfigProjectionError(
                'stdin must be {"set": {...}, "owned_keys": [...]}'
            )
        written = write_env_block(
            Path(args.project_folder), args.surface, values, owned,
        )
    except SettingsWriteRefused as exc:
        print(json.dumps({"ok": False, **_refused_payload(exc)}))
        return 4
    except (ConfigProjectionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "bad_request", "message": str(exc)}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "write_failed", "message": str(exc)}))
        return 4
    print(json.dumps({"ok": True, "surface": args.surface, "written": written}))
    return 0


def _cli_strip_proven_secret_values(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection strip-proven-secret-values``.

    stdout: ONE JSON object — ``{"ok": true, "removed": {file: [KEY]},
    "left": {file: {KEY: verdict}}, "reasons": {verdict: sentence},
    "errors": [message]}`` — key names, verdicts and the ONE wording of each
    verdict; never a value. The launcher's unregister acts on it
    (:func:`strip_proven_secret_values`). Exit 0; 4 with ``{"ok": false, ...}``
    on an unexpected failure.
    """
    try:
        result = strip_proven_secret_values(Path(args.project_folder))
    except Exception as exc:  # noqa: BLE001 — the caller must see a failure, loudly
        print(json.dumps({"ok": False, "error": "strip_failed",
                          "message": f"{type(exc).__name__}: {exc}"}))
        return 4
    print(json.dumps({"ok": True, **result, "reasons": EVIDENCE_REASONS}))
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


def _cli_user_secret_known_keys(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection user-secret-known-keys
    --project-id <id>``.

    Print every user-bucket KEY the launcher has observed across the three
    buckets — the names whose in-tree values the env refresh checks for
    value evidence (:func:`classify_json_env_secrets`). Names only.
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


def _cli_reproject_all(args: argparse.Namespace) -> int:
    """``python -m vco_lib.config_projection reproject-all``.

    Update-time migration entry point: re-project EVERY registered
    project's canonical env so a stale slug-form
    ``VCT_CODE_GRAPH_ACCESS_LIST`` is replaced by the name-form value
    (GAP-CG-1). Prints a JSON summary of per-project outcomes. Failures
    are recorded per-project (and, in a full install run, deferred via
    the caller's DeferralReport) — this CLI reports them but exits 0
    unless the launcher DB itself is unreachable (exit 3), matching the
    "launcher never booted → no-op" posture of the update flow.
    """
    surfaces = (
        [s.strip() for s in args.surfaces.split(",") if s.strip()]
        if args.surfaces
        else None
    )
    try:
        outcomes = reproject_all_registered_projects(
            db_path=Path(args.db_path) if args.db_path else None,
            surfaces=surfaces,
        )
    except DbUnreachable as exc:
        print(
            json.dumps({"error": "db_unreachable", "message": str(exc)}),
            file=sys.stderr,
        )
        return 3

    migrated = [o for o in outcomes if o["status"] == "migrated"]
    failed = [o for o in outcomes if o["status"] == "failed"]
    refused = [o for o in outcomes if o["status"] == "refused"]
    summary = {
        "total": len(outcomes),
        "migrated": len(migrated),
        "failed": len(failed),
        "refused": len(refused),
        "outcomes": outcomes,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
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
    # v0.2.97 (lane W): unset = this machine's value (vco_lib.service_endpoints);
    # the launcher passes its own Rust resolution.
    p_apply.add_argument("--weaviate-url", default=None)
    p_apply.add_argument("--weaviate-port", type=int, default=None)
    p_apply.add_argument("--ollama-port", type=int, default=None)
    p_apply.add_argument("--code-embed-port", type=int, default=None)
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

    # v0.2.97: the `apply-user-secrets` verb (a strip of every launcher-known
    # key NAME) was retired — SUPERSEDED by the evidence-gated scrub every
    # `apply` performs and by `strip-proven-secret-values` for the unregister.
    p_us_known = sub.add_parser(
        "user-secret-known-keys",
        help="print every user-bucket KEY observed in the DB (the names the "
             "refresh checks for value evidence)",
    )
    p_us_known.add_argument("--project-id", required=True)
    p_us_known.add_argument("--db-path", default=None)
    p_us_known.add_argument("--json", action="store_true")
    p_us_known.set_defaults(handler=_cli_user_secret_known_keys)

    # GAP-CG-1 (2026-07-14) update-time migration: re-project every
    # registered project so the code-graph access list migrates from
    # grantor-slug to grantor-name form. Invoked by the orchestrator
    # update flow (install.py --update / launcher update path).
    p_reproj = sub.add_parser(
        "reproject-all",
        help="re-project EVERY registered project's canonical env "
             "(migrates VCT_CODE_GRAPH_ACCESS_LIST slug→name form)",
    )
    p_reproj.add_argument(
        "--db-path", default=None,
        help="override launcher DB path (defaults to ~/.vct/launcher.db)",
    )
    p_reproj.add_argument(
        "--surfaces", default=None,
        help="comma-separated subset of "
             "claude_settings_json,claude_env,vscode_settings_json "
             "(default: claude_settings_json,claude_env)",
    )
    p_reproj.set_defaults(handler=_cli_reproject_all)

    # v0.2.97: the ONE surgical env-block editor for launcher writers that
    # own a key set outside the canonical projection (module deprecation).
    p_block = sub.add_parser(
        "write-env-block",
        help="set/strip owned keys in one JSON surface's env block "
             "(request JSON on stdin; refuses a file it cannot safely edit)",
    )
    p_block.add_argument("--project-folder", required=True)
    p_block.add_argument(
        "--surface", default=_SURFACE_CLAUDE_SETTINGS,
        choices=sorted(_JSON_SURFACE_FILES),
    )
    p_block.set_defaults(handler=_cli_write_env_block)

    # v0.2.97 review R6: the `strip-env-keys` verb (a by-name strip, whose
    # only caller was the launcher's unregister) is retired — SUPERSEDED by
    # `python -m vco_lib.unregister_env strip-routing`, which removes a key
    # only where its value is VCO's.

    # v0.2.97: the unregister's evidence step (names + verdicts only).
    p_proven = sub.add_parser(
        "strip-proven-secret-values",
        help="remove the secret values VCO provably wrote from a project's env "
             "files; report the rest (names and verdicts only; never a value)",
    )
    p_proven.add_argument("--project-folder", required=True)
    p_proven.set_defaults(handler=_cli_strip_proven_secret_values)

    return p


def build_apply_argv(
    python: str,
    project_id: str,
    *,
    db_path: Path | str | None = None,
    orchestrator_root: Path | str | None = None,
) -> list[str]:
    """The ``apply`` verb's argv, built next to the parser that must accept it.

    The ONE Python-side builder (v0.2.97): the project mover and the
    collection rename each hand-built this argv with a ``--folder`` flag the
    verb has never had, so argparse exited 2 and every post-move / post-rename
    env re-projection failed. The project root is not an input: ``apply``
    re-derives it from the launcher DB row, which is the point of calling it
    after a flip. The Rust twin is ``build_config_projection_apply_args`` in
    ``launcher/src-tauri/src/commands/projects_v2.rs``; both are parsed by
    this module's real parser in ``tests/test_v0297_vco_lib_argv_contract.py``.
    """
    argv = [python, "-m", "vco_lib.config_projection", "apply",
            "--project-id", project_id]
    if db_path is not None:
        argv += ["--db-path", str(db_path)]
    if orchestrator_root is not None:
        argv += ["--orchestrator-root", str(orchestrator_root)]
    return argv



def run_update_reprojection_step(
    *,
    print_fn: Any = print,
    log_event: Any | None = None,
    deferral_report: Any | None = None,
) -> None:
    """install.py ``--update`` wiring for :func:`reproject_all_registered_projects`.

    Extracted from install.py main() (ratchet: flow logic lives in vco_lib,
    the monolith gets a thin call). Behavior:

    - :class:`DbUnreachable` -> quiet no-op — fresh install pre-launcher-boot,
      the same posture as the orchestrator-root backfill's ``db_unreachable``
      arm (the first project add creates launcher.db; later ``--update`` runs
      re-project then).
    - ``deferral_report`` MUST be threaded from the caller for per-project
      failures to land in UPDATE_DEFERRED.md: the sweep's ``safe_emit_entry``
      is None-guarded, so omitting it silently drops the entries while the
      summary below still says "N deferred" — the exact masking false-signal
      the 2026-07-14 final review flagged (B2). install.py passes its live
      ``_deferral_report``.
    - Never raises past DbUnreachable handling: the update must not die on a
      summary/logging problem (the sweep itself did the real work).
    """
    try:
        outcomes = reproject_all_registered_projects(
            log_event=log_event, deferral_report=deferral_report
        )
    except DbUnreachable:
        return
    migrated = sum(1 for o in outcomes if o["status"] == "migrated")
    failed = sum(1 for o in outcomes if o["status"] == "failed")
    refused = sum(1 for o in outcomes if o["status"] == "refused")
    if outcomes:
        msg = f"  Access-list re-projection: {migrated} project(s) re-projected"
        if failed:
            msg += f", {failed} deferred (see UPDATE_DEFERRED.md)"
        if refused:
            msg += (f", {refused} with a settings file left untouched (see that "
                    "project's own UPDATE_DEFERRED.md)")
        try:
            print_fn(msg)
        except Exception:
            pass
    if log_event is not None:
        try:
            log_event(
                "9/10", "info",
                "reproject_all_registered_projects "
                f"migrated={migrated} failed={failed} refused={refused}",
            )
        except Exception:
            pass


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
    "ProjectReprojectOutcome",
    "SettingsWriteRefused",
    "apply_project_env",
    "build_apply_argv",
    "known_user_secret_keys_for_folder",
    "list_canonical_keys",
    "list_registered_projects",
    "project_env_from_db",
    "reproject_all_registered_projects",
    "resolve_project_folder",
    "classify_json_env_secrets",
    "strip_proven_secret_values",
    "retained_secret_keys_in",
    "retained_user_secret_state",
    "retained_launcher_value_names",
    "strip_env_keys",
    "user_secret_known_keys_from_db",
    "write_env_block",
]
