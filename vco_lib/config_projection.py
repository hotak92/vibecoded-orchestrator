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
* User-bucket secrets (``user_secret_pairs`` / ``user_secret_known_keys``
  in ``ProjectEnvSettings``). Those have a separate active-flag /
  paused-state lifecycle managed by ``commands::project_env_settings::
  resolve_user_secret_state`` in Rust; routing them through this
  contract requires plumbing that active-flag gate into Python first.
  The contract handles canonical (launcher-owned) keys only.

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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, TypedDict

from vco_lib.paths import vct_root_dir


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
    "SHARED_KG_COLLECTION",
    "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT",
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
    "VCT_KG_ACCESS_LIST",
    "VCT_CODE_GRAPH_ACCESS_LIST",
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

    Mirrors Rust's ``crate::paths::vct_root_dir().join("launcher.db")``
    via :func:`vco_lib.paths.vct_root_dir`.

    The file may not exist (fresh install where the launcher has never
    been started). Callers must handle that.
    """
    return vct_root_dir() / "launcher.db"


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


# ─── Sanitization (mirrors Rust ``sanitize_kg_collection``) ─────────────
#
# Same rule as ``vco_lib.project_init.sanitize_for_weaviate_class``:
# split on any non-alphanumeric run, PascalCase each chunk, concatenate,
# fall back to "Vct" if nothing survives or starts with a digit.

_SAFE_CLASS_RE = re.compile(r"[^A-Za-z0-9]+")


def _sanitize_kg_collection(project_name: str) -> str:
    """Mirror of Rust's ``sanitize_kg_collection``.

    See ``vco_lib.project_init.sanitize_for_weaviate_class`` for the
    canonical implementation; we re-implement here to avoid an import
    cycle (project_init is the heavier module; this one is meant to be
    importable by tests in isolation).
    """
    base = project_name or ""
    parts = [p for p in _SAFE_CLASS_RE.split(base) if p]
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or pascal[:1].isdigit():
        return "Vct"
    return pascal


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
    shared_kg_default: str = "VibeCodedOrchestrator_KnowledgeGraph",
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
        shared_kg_default: Fallback SHARED_KG_COLLECTION if the
            ``shared`` KG binding row is absent. Matches the Rust
            ``DEFAULT_SHARED_KG_COLLECTION`` constant
            (``"VibeCodedOrchestrator_KnowledgeGraph"``).
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
        shared_kg = kg_bindings.get("shared", shared_kg_default)
        dev_collection = kg_bindings.get(
            "archive", f"{sanitized}_Development"
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

        # Module settings — orchestrator-core scope.
        shared_kg_write_disabled = _fetch_module_setting_bool(
            conn, project_id, "orchestrator-core",
            "shared_kg_write_disabled", default=False,
        )
        if active_embedding_override is not None:
            active_embedding = active_embedding_override
        else:
            active_embedding = _fetch_module_setting_str(
                conn, project_id, "orchestrator-core",
                "active_embedding", default="qwen3",
            )
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
    _set("SHARED_KG_COLLECTION", shared_kg)
    # Boolean → "true"/"false" (lowercase, matching Rust's
    # `shared_kg_write_disabled_str()` -> bool::to_string()).
    _set("SHARED_KG_WRITE_DISABLED", "true" if shared_kg_write_disabled else "false")
    # Legacy alias — same value, kept for ~3 releases (target 2026-08).
    _set("SHARED_KG_OPT_OUT", "true" if shared_kg_write_disabled else "false")
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

    if kg_access:
        _set("VCT_KG_ACCESS_LIST", ",".join(kg_access))
    if code_graph_access:
        _set("VCT_CODE_GRAPH_ACCESS_LIST", ",".join(code_graph_access))

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

    report: dict[str, list[str]] = {}

    if _SURFACE_CLAUDE_SETTINGS in surfaces_seq:
        path = project_root / ".claude" / "settings.json"
        keys = _write_json_env_block(
            path, env, canonical_keys, env_key="env",
        )
        report[_SURFACE_CLAUDE_SETTINGS] = keys

    if _SURFACE_CLAUDE_ENV in surfaces_seq:
        path = project_root / ".claude" / "env"
        keys = _write_shell_env_managed_block(path, env)
        report[_SURFACE_CLAUDE_ENV] = keys

    if _SURFACE_VSCODE_SETTINGS in surfaces_seq:
        path = project_root / ".vscode" / "settings.json"
        keys = _write_json_env_block(
            path, env, canonical_keys, env_key="claude-code.env",
        )
        report[_SURFACE_VSCODE_SETTINGS] = keys

    return report


# ─── Surface writers ────────────────────────────────────────────────────


def _write_json_env_block(
    path: Path,
    canonical_env: Mapping[str, str],
    canonical_keys: set[str],
    *,
    env_key: str,
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
      * Non-canonical keys (user-added) are PRESERVED untouched.
      * Write the result back with 2-space indent, no trailing newline,
        ``ensure_ascii=False`` (matching Rust's
        ``serde_json::to_string_pretty`` byte layout).

    Returns the sorted list of canonical keys whose value was set
    (those that were deleted are not listed — the audit consumer wants
    to see what's NOW exported, not what was previously there).

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

    # Apply canonical updates.
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

    Atomic write: same tempfile + ``os.replace`` discipline as the JSON
    surfaces.

    Returns the sorted list of canonical keys written. Keys absent from
    ``canonical_env`` (the bundle decided to omit them) are simply not
    rendered.
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

    managed = _build_managed_block(canonical_env)
    new_text = _merge_managed_block(prior, managed)
    _atomic_write_text(path, new_text)

    return sorted(canonical_env.keys())


def _build_managed_block(canonical_env: Mapping[str, str]) -> str:
    """Render the managed block for ``.claude/env``.

    Format (byte-identical to Rust's
    ``build_claude_env_managed_block_with_user_secrets``, modulo the
    user-secrets section which is out-of-scope here):

      ``# vco-managed-begin\\n``
      ``<header comments — 7 lines>\\n``
      ``export KEY1="value1"\\n``
      ...
      ``# vco-managed-end\\n``

    Embedded double-quotes in values are backslash-escaped (rare on
    POSIX; legitimate on Windows + git-bash paths). The Rust writer
    only escapes ``"`` — we mirror that.

    Note: in production, the user-secret pairs from the Rust resolver
    land BETWEEN the canonical and END marker. This Python writer does
    NOT emit them (user secrets stay Rust-owned for now); the END
    marker comes directly after the canonical block. The next Rust
    invocation will re-add the user-secret block by replacing
    BEGIN→END wholesale. Until a project's grant matrix changes
    OR config_projection grows a user-secret bridge, this hybrid is
    consistent: whichever writer ran last owns the block content.
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

    Uses a tempfile in the SAME directory as ``path`` (so the rename
    stays on one filesystem — cross-filesystem rename fails with EXDEV
    on Linux), then ``os.replace``-s into place. ``os.replace`` is
    cross-OS atomic (POSIX rename / Windows MoveFileExW with
    REPLACE_EXISTING).

    UTF-8 encoded, LF line endings (matches Rust's ``std::fs::write``
    which writes the byte sequence verbatim — Rust never CRLF-converts
    unless the caller asked for it).

    The tempfile is fsync'd to disk before rename. The parent directory
    is NOT fsync'd; on most filesystems this means a crash immediately
    after the rename could leave the rename un-persisted (the file
    would either be the old or the new content — never a partial-write).
    That's the acceptable trade for not paying directory-fsync cost on
    every write.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile with delete=False so we can rename it; the
    # caller's responsibility to clean up if rename fails.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can fail on some pseudo-filesystems (procfs,
                # tmpfs in containers); don't fail the write over it.
                pass
        os.replace(str(tmp_path), str(path))
    except Exception:
        # Best-effort cleanup of the tempfile on any failure. Don't
        # mask the original exception.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


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
    "apply_project_env",
    "list_canonical_keys",
    "project_env_from_db",
]
