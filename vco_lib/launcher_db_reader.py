# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read-only launcher.db helper for install.py (v0.2.44).

Discovers ``~/.vct/launcher.db`` (env override: ``VCT_LAUNCHER_DB_PATH``).
Soft-fails on any error (returns ``None`` or empty). Never raises.

Used by ``install.py`` to resolve canonical KG collection names from
``project_kg_bindings`` (the source-of-truth) instead of from env vars
(a projection). This closes the architectural-debt phase 1 of the
v0.2.43 V0243-0 recurring bug — env-derived collection names drift
from the launcher DB silently, the DB binding is authoritative.

Surface
~~~~~~~

* :func:`get_orchestrator_root_project_id` — lookup the
  ``host='orchestrator_root'`` row in ``projects``.
* :func:`get_kg_binding` — lookup ``collection_name`` from
  ``project_kg_bindings`` for a given ``(project_id, role)`` pair where
  ``role`` is ``"primary"`` or ``"shared"``.
* :func:`get_orchestrator_root_bindings` — convenience: return both
  primary + shared bindings for orchestrator-root in one call.

Soft-fail semantics
~~~~~~~~~~~~~~~~~~~

All public functions return ``None`` (or ``(None, None)`` for the tuple
helper) when the DB is unavailable, malformed, the row is missing, or
SQLite raises. They never propagate exceptions. This matches the rest
of ``install.py``'s "Weaviate-down / launcher-down must not block the
install" discipline (see CHANGELOG v0.2.41 + v0.2.42 hardening notes).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple


def _discover_db_path() -> Optional[Path]:
    """Return ``launcher.db`` path or ``None`` if not found.

    Resolution priority:
      1. ``VCT_LAUNCHER_DB_PATH`` env override (must point at an existing file).
      2. :func:`vco_lib.paths.launcher_db_path` — honours ``VCT_STATE_DIR``
         env override and falls back to ``~/.vct/launcher.db``.

    Returns ``None`` when neither yields an existing regular file.

    v0.2.44 V44-E: delegates the default-path branch to
    :func:`vco_lib.paths.launcher_db_path` so the single source-of-truth
    helper in ``vco_lib.paths`` owns cross-OS path resolution + the
    ``VCT_STATE_DIR`` override (the previous inline form bypassed the
    env override silently). Satisfies
    ``tests/test_vct_root_dir_consolidation::test_no_inline_reconstructions_outside_paths_module``.
    """
    override = os.environ.get("VCT_LAUNCHER_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_file() else None
    try:
        from vco_lib.paths import launcher_db_path
        default = launcher_db_path()
        return default if default.is_file() else None
    except Exception:
        return None


def _open_db_readonly() -> Optional[sqlite3.Connection]:
    """Open ``launcher.db`` in read-only mode, or ``None`` on any failure.

    Uses SQLite's ``file:...?mode=ro`` URI form so concurrent launcher
    writes are never blocked by us. ``timeout=5.0`` guards against a
    locked-DB hang on Windows. Row factory is set to :class:`sqlite3.Row`
    so callers can use column-name access.
    """
    p = _discover_db_path()
    if p is None:
        return None
    try:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def get_orchestrator_root_project_id() -> Optional[str]:
    """Return ``project_id`` of the orchestrator-root project.

    Looks up the row in ``projects`` whose ``host`` column equals
    ``'orchestrator_root'``. Returns ``None`` when the DB is unavailable,
    the table/column is missing, or no such row exists.
    """
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE host = 'orchestrator_root' LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_kg_binding(project_id: str, role: str) -> Optional[str]:
    """Return ``collection_name`` from ``project_kg_bindings`` for ``(project_id, role)``.

    :param project_id: the ``projects.id`` value to look up.
    :param role: must be ``"primary"`` or ``"shared"`` — any other value
        returns ``None`` without touching the DB (defensive: prevents
        a typo from silently matching some other role string).

    Returns ``None`` when DB unavailable / row missing / SQLite errors.
    """
    if role not in ("primary", "shared"):
        return None
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT collection_name FROM project_kg_bindings "
            "WHERE project_id = ? AND role = ? LIMIT 1",
            (project_id, role),
        ).fetchone()
        return row["collection_name"] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_orchestrator_root_bindings() -> Tuple[Optional[str], Optional[str]]:
    """Return ``(primary_collection_name, shared_collection_name)`` for orchestrator-root.

    Convenience wrapper that combines :func:`get_orchestrator_root_project_id`
    with two :func:`get_kg_binding` calls. Either or both elements of the
    returned tuple can be ``None`` (DB unavailable, project row missing,
    or a binding row missing).
    """
    pid = get_orchestrator_root_project_id()
    if pid is None:
        return (None, None)
    return (get_kg_binding(pid, "primary"), get_kg_binding(pid, "shared"))


# ────────────────────────────────────────────────────────────────────────────
# v0.2.52 V52-AJ: app_state key readers
#
# Used by EmbeddingService.for_project() + install.py's subprocess env-thread
# layer to resolve the active embedding profile when no env var is set.
#
# The launcher writes ``embedding.active_profile`` from the Identity tab's
# embedding selector + on install.py's preset choice. Mirror constant:
#   launcher/src-tauri/src/commands/project_env_settings.rs
#     ::APP_STATE_KEY_ACTIVE_EMBEDDING (canonical string: "embedding.active_profile")
#
# This file is read-only — writes happen exclusively from the Rust launcher.
# ────────────────────────────────────────────────────────────────────────────

#: Canonical app_state key for the active embedding profile.
#: MUST stay in sync with the Rust constant cited above.
APP_STATE_KEY_ACTIVE_EMBEDDING = "embedding.active_profile"

#: Canonical app_state key for the hardware-selected default TEXT model id.
#: Written by install.py's preset seeding + the launcher Identity-tab
#: chooser (``set_text_embedding_and_profile``). Mirror constant:
#:   launcher/src-tauri/src/commands/openai_cmd.rs
#:     ::APP_STATE_DEFAULT_TEXT_EMBED (canonical string: "default_text_embedding")
APP_STATE_KEY_DEFAULT_TEXT_EMBED = "default_text_embedding"

# ─── v0.2.71 T-B-emb: per-project active-embedding marker ────────────────────
#
# The per-project ACTIVE_EMBEDDING profile + its provenance live in
# ``module_settings`` under module_id ``orchestrator-core``. Mirror constants:
#   launcher/src-tauri/src/commands/project_env_settings.rs
#     ::ORCHESTRATOR_CORE_MODULE_ID / ::ACTIVE_EMBEDDING_SETTING_KEY /
#     ::ACTIVE_EMBEDDING_SOURCE_SETTING_KEY /
#     ::ACTIVE_EMBEDDING_SOURCE_USER / ::ACTIVE_EMBEDDING_SOURCE_AUTO
# The hub config_api.rs resolver + config_projection.py use the same cascade:
# a source=="user" per-project row is sticky; "auto" / legacy-no-marker / absent
# rows inherit the machine-global ``app_state[embedding.active_profile]``.

#: module_settings module_id that owns the per-project active-embedding rows.
ORCHESTRATOR_CORE_MODULE_ID = "orchestrator-core"
#: setting_key for the per-project ACTIVE_EMBEDDING profile value.
ACTIVE_EMBEDDING_SETTING_KEY = "active_embedding"
#: setting_key for the provenance marker companion row.
ACTIVE_EMBEDDING_SOURCE_SETTING_KEY = "active_embedding_source"
#: marker value: deliberate Settings-tab user pick (sticky across updates).
ACTIVE_EMBEDDING_SOURCE_USER = "user"
#: marker value: startup-backfill auto-seed (inherits the global default).
ACTIVE_EMBEDDING_SOURCE_AUTO = "auto"

#: TEXT model id → ACTIVE_EMBEDDING named-vector slot ("profile").
#:
#: MUST match (and is the importable shared home for what was previously
#: re-listed in) ``install.py::_TEXT_MODEL_ACTIVE_EMBEDDING`` and the Rust
#: mirror ``project_env_settings.rs::active_profile_for_model``. A model
#: id with no entry here maps to ``None`` (see ``profile_for_text_model``)
#: — conservative: never stamp a guessed profile, because the wrong slot
#: indexes the KG against the wrong vector (the 2026-04-30 vector-audit
#: bug class). Drift between these three re-introduces v0.2.68 Defect D.
_TEXT_MODEL_ACTIVE_EMBEDDING = {
    "qwen3-embedding:0.6b": "qwen3",
    "snowflake-arctic-embed2:latest": "arctic",
    "openai-text-embedding-3-small": "openai",
    "text-embedding-3-small": "openai",
}


def profile_for_text_model(model_id: str | None) -> Optional[str]:
    """Map a TEXT embedding model id to its ACTIVE_EMBEDDING profile.

    Returns ``None`` for an unknown / empty / ``None`` model id — callers
    MUST treat ``None`` as "leave the active-embedding value at its prior
    fallback (qwen3)" rather than stamping a guessed profile.

    Mirror of the Rust ``active_profile_for_model`` (lockstep via the
    must-match comment on ``_TEXT_MODEL_ACTIVE_EMBEDDING`` above).
    """
    if not model_id:
        return None
    return _TEXT_MODEL_ACTIVE_EMBEDDING.get(model_id.strip())


def read_app_state_default_text_embedding() -> Optional[str]:
    """Return the hardware-selected default TEXT model id, or ``None``.

    Reads ``app_state[default_text_embedding]`` (raw string, not JSON —
    the launcher's ``app_state_set`` stores values verbatim). This is the
    hardware pick (e.g. ``snowflake-arctic-embed2:latest`` on an arctic
    host) that callers map to a profile via :func:`profile_for_text_model`
    when the canonical ``embedding.active_profile`` key is unset.

    Soft-fail: ``None`` when launcher.db is absent / key unset / empty.
    """
    raw = read_app_state_value(APP_STATE_KEY_DEFAULT_TEXT_EMBED)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def read_app_state_value(key: str) -> Optional[str]:
    """Read a single ``app_state`` key from launcher.db (read-only).

    Returns the stored string value, or ``None`` when the DB is unavailable,
    the ``app_state`` table is absent (fresh install, never booted), the
    key is missing, or any SQLite error occurs.

    Soft-fail discipline: never raises. Callers treat ``None`` as
    "unknown / use fallback".

    :param key: the ``app_state.key`` to look up.
    """
    if not key:
        return None
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ? LIMIT 1", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_app_state_active_embedding() -> Optional[str]:
    """Return the active embedding profile from launcher.db, or ``None``.

    Reads ``app_state[embedding.active_profile]``. The value, when set,
    is one of:

      * ``"qwen3"`` — local Ollama qwen3-embedding:0.6b (default)
      * ``"arctic"`` — local Ollama snowflake-arctic-embed2:latest
      * ``"openai"`` — OpenAI text-embedding-3-small
      * ``"codesage"`` — CodeSage-Large-v2 (code embeddings; rarely the
        text profile, but accepted for completeness)

    Returned verbatim (no normalisation here; callers normalise via
    ``.strip().lower()`` if needed). Returns ``None`` when launcher.db
    is absent (free-tier install with no launcher), the key is unset
    (fresh install before any embedding choice was made), or the value
    is an empty string after stripping.

    This is the bridge between the launcher's stored embedding choice
    (written by the Identity-tab embedding selector + install.py's preset
    seeding) and any subprocess (``install.py``'s ``sync_knowledge_graph.py``
    spawn, the MCP server's ``EmbeddingService.for_project()``) that
    needs to know which embedding to use when no env override is set.
    """
    raw = read_app_state_value(APP_STATE_KEY_ACTIVE_EMBEDDING)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None
