# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The KG query / format layer — light enough for a hook (v0.2.102 W6-M).

Moved VERBATIM out of ``server.py`` (and five accessors out of
``embeddings.py``), following the ``embeddings.py`` / ``rl_enrichment.py``
pure-move pattern: the per-project config resolution the KG read path keys on
(hub → env → default, the resolution context, the collection constants, the
shared-KG read/write gates), the fan-out across self + shared + granted peers,
the per-node cross-project access filter, the score tiers and the tier
renderer with its sidecar / chunk-assembly helpers, ``_format_obj`` and the
adjacent-chunk enrichment, the one-per-node collapse, and the query-embed
accessors. Nothing here imports weaviate-client or the MCP SDK, so the hook
KG producer (``claude_mcp_servers/scripts/rl_kg_search.py``) runs the SAME
code without paying ~0.7 s for ``weaviate_mcp.server`` on every prompt.

Two consumers, one definition:

* ``server.py`` imports every name below and REBINDS each moved function into
  its own namespace (:func:`bind_into` — same code object, ``server``'s
  globals). Inside the MCP the moved code therefore resolves ``Filter``,
  ``logger``, ``KG_COLLECTION``, ``_fetch_node_chunks`` … on ``server``
  exactly as it did before the move, so every ``patch("…server.X")`` in the
  suite is observed by the moved code unchanged. A server (re)import reloads
  this module first, so a reload with a changed environment re-resolves the
  config as it always did.
* The hook producer reads this module through ``kg_host.host()`` when no
  server module is loaded. Here ``Filter`` and :func:`get_weaviate_client` are
  the plain-HTTP read adapter (``kg_http``), and the module doubles as the
  LIGHT HOST for the satellites that read ``server.<name>``: names it does not
  define are served from ``rl_enrichment`` / ``rl_state`` and, as the last
  resort, from the real server module (:func:`__getattr__`) — slower, never
  missing.
"""

import asyncio
import json
import logging
import os
import re
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from .kg_http import Filter, HttpWeaviateClient  # noqa: F401 — Filter: the light host's binding (read by the moved code)
from .kg_http import WeaviateUnreachable as WeaviateUnreachable  # re-exported by server (one class for both clients)

logger = logging.getLogger(__name__)


def _canonical_sanitize_for_weaviate_class(name: str) -> str:
    """``vco_lib.project_init.sanitize_for_weaviate_class``, imported on first
    use: ``project_init`` costs ~0.14 s and only the env-fallback peer path
    (``VCT_KG_ACCESS_LIST``) needs it. ``server`` binds the eager import under
    this same name, so the MCP path is unchanged; a broken install still fails
    loudly (the ImportError propagates)."""
    from vco_lib.project_init import sanitize_for_weaviate_class

    return sanitize_for_weaviate_class(name)


def _reraise_vco_lib_import(exc: "ImportError", what: str) -> NoReturn:
    """Enrich a vco_lib ImportError with remediation, then re-raise LOUD.

    ``vco_lib`` is shipped; a failed import is a broken install. No
    fallback — startup must fail visibly rather than degrade to a
    silent inline copy that masks the breakage.
    """
    _note = (
        f"weaviate-kg: required vco_lib member '{what}' is not importable — "
        f"this is a BROKEN orchestrator install (vco_lib is editable-installed "
        f"into every healthy install). Run `python install.py --update` from "
        f"the orchestrator root to repair it."
    )
    if hasattr(exc, "add_note"):
        exc.add_note(_note)  # type: ignore[attr-defined]  # Python 3.11+
        raise exc
    raise ImportError(f"{exc} — {_note}") from exc


# v0.2.18: EmbeddingService (vco_lib) — centralised dispatcher for embed
# calls and slot resolution. The per-helper adapters in ``embeddings.py``
# call it; a failed import = broken install → loud-fail (was: silent
# HAS_EMBEDDING_SERVICE=False + inline legacy bodies).
_vco_lib_parent = Path(__file__).resolve().parent.parent.parent
if str(_vco_lib_parent) not in sys.path:
    sys.path.insert(0, str(_vco_lib_parent))
try:
    from vco_lib.embedding_service import (
        EmbeddingService,
        # NoEmbeddingBackendError is re-exported for embeddings.py's dynamic
        # ``server.NoEmbeddingBackendError`` access (ruff can't see it).
        NoEmbeddingBackendError,  # noqa: F401  # pyright: ignore[reportUnusedImport] — re-export (comment above)
    )
except ImportError as _embed_import_err:
    _reraise_vco_lib_import(_embed_import_err, "embedding_service.EmbeddingService")


HAS_EMBEDDING_SERVICE = True


# v0.2.94: the user's HOME (re-exported by ``server``). The boundary in
# `_resolution_context()` now reads it inside ``vco_lib.project_root``
# (v0.2.102 W6-W), from the same resolver. Resolved through the ONE resolver (`VCT_USER_HOME_OVERRIDE` -> `Path.home()`)
# and never reconstructed inline — an inline `Path.home()` is exactly the shape
# that let the doctor read the developer's real global config from the test
# suite (v0.2.92 W-CLAUDE), because nothing could steer it.
try:
    from vco_lib.paths import user_home as _user_home  # noqa: F401  # pyright: ignore[reportUnusedImport] — re-exported by server
except ImportError as _user_home_import_err:
    _reraise_vco_lib_import(_user_home_import_err, "paths.user_home")


# ─── v0.2.21 Step 18: per-project config resolver ───────────────────────
#
# Module-level config constants used to be read directly from os.getenv
# at import time. v0.2.21 routes them through the launcher's vct-hub via
# vco_lib.project_config.resolve(), with env-var fallback when the hub
# is unreachable (launcher not running, project not registered, stale
# token after a launcher restart). The resolved values populate the
# module-level KG_COLLECTION / SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION
# / ACTIVE_EMBEDDING / CODE_GRAPH_PROJECT constants below.
#
# The MCP is long-running (hours to days). We resolve ONCE at import
# time — the hub's TTL semantics keep callers fresh-enough (cf. parent
# plan §8.3). A SIGHUP triggers process exit + relaunch (sighup_handler
# above), so a launcher GUI edit propagates within the next request.
# SHIPPED — a failed IMPORT of vco_lib.project_config = broken install →
# loud-fail (was: silent _HAS_PROJECT_CONFIG=False). This is DISTINCT from
# the RUNTIME hub-unreachable degrade: `_try_resolve_project_config()`
# below still returns None (→ env fallback) when the live hub is down /
# the project isn't registered — a legitimate, documented degrade that is
# NOT a broken install. Only the import itself loud-fails here.
_vco_lib_parent_for_pc = Path(__file__).resolve().parent.parent.parent
if str(_vco_lib_parent_for_pc) not in sys.path:
    sys.path.insert(0, str(_vco_lib_parent_for_pc))
try:
    from vco_lib.project_config import resolve as _resolve_project_config  # type: ignore[import-not-found]
    # NOTE: the former ``ResolverError as _ResolverError`` import was dead code
    # (only ever assigned in the deleted except-fallback, never referenced) —
    # removed with the fallback per the v0.2.81 cleanup directive.
except ImportError as _pc_import_err:
    _reraise_vco_lib_import(_pc_import_err, "project_config.resolve")
_HAS_PROJECT_CONFIG = True


_resolved_project_config = None  # cached resolve() result (or None if unreachable)


# ─── v0.2.92: WHOSE project is this module resolving for? ───────────────
#
# This module has two consumers with very different amounts of context:
#
#   1. The MCP SERVER process. Claude Code spawns it with
#      ``CLAUDE_PROJECT_DIR`` pointing at the workspace, so "which project"
#      is answered authoritatively by the harness.
#   2. A LIBRARY IMPORT from a project CLI. ``templates/scripts/
#      search_knowledge.py`` imports the tier helpers from here; a plain
#      shell has no ``CLAUDE_PROJECT_DIR`` at all.
#
# Pre-v0.2.92 case 2 fell back to ``Path(__file__).parent.parent.parent`` —
# the ORCHESTRATOR's install root — and then resolved the ORCHESTRATOR's
# config from the hub, labelled it ``src=hub``, and let it OUTRANK the
# project's own correct ``KG_COLLECTION`` env var. A user running
# ``kg-search`` inside their project saw the orchestrator's collections
# announced as if the hub had endorsed them for their project. The
# collection constants (`KG_COLLECTION`, `SHARED_KG_COLLECTION`,
# `CODE_GRAPH_PROJECT`, the access lists) were all silently orchestrator-
# scoped for every library consumer — a landmine for any future CLI that
# reads them.
#
# The fix is structural, not documentary: name the CONTEXT the resolution
# is keyed on, and let a merely-GUESSED context lose to an explicit env
# var. There is no longer any code path that answers "the orchestrator"
# to a question about "here" while claiming authority for the answer.
_CTX_WORKSPACE = "workspace"  # CLAUDE_PROJECT_DIR — the harness told us
_CTX_CWD = "cwd"              # a bundled project found by walking up from CWD
_CTX_MODULE = "module"        # last resort: where server.py happens to live

#: The orchestrator install root (this module's own ``parent.parent.parent``).
#: Only ever used as the ``_CTX_MODULE`` guess.
_MODULE_OWN_ROOT = Path(__file__).resolve().parent.parent.parent


# The marker test and the home boundary live in ONE home since v0.2.102 W6-W
# (``vco_lib.project_root``), shared with every ``.claude/state`` writer that
# used to fall back to a bare cwd. The historical names stay as thin
# delegates so ``server`` keeps re-exporting (and ``bind_into``-rebinding)
# them unchanged. ``.claude/settings.json`` or ``.claude/env`` marks a project
# (a bare ``.claude/`` does not); the walk stops at, and never accepts,
# either home this process could have.
def _looks_like_vco_project(path: Path) -> bool:
    """Delegate: :func:`vco_lib.project_root.looks_like_vco_project`."""
    from vco_lib.project_root import looks_like_vco_project

    return looks_like_vco_project(path)


def _home_boundaries() -> "frozenset[Path]":
    """Delegate: :func:`vco_lib.project_root.home_boundaries`."""
    from vco_lib.project_root import home_boundaries

    return home_boundaries()


def _resolution_context() -> "tuple[Path, str]":
    """Return ``(project_root, context_kind)`` for hub resolution.

    Priority:
      1. ``CLAUDE_PROJECT_DIR`` when it names an existing directory —
         authoritative, the harness told us which workspace we serve.
      2. The nearest ancestor of the CWD that looks like a bundled VCO
         project — authoritative enough: the caller is standing in it.
         This is the case a project CLI hits (no ``CLAUDE_PROJECT_DIR``).
      3. ``_MODULE_OWN_ROOT`` — a GUESS. Correct only when the process
         happens to be about the orchestrator itself.

    The rung-2 walk STOPS at the user's home and never accepts it (v0.2.94).
    EVERY Claude Code user has ``~/.claude/settings.json`` — the harness's
    GLOBAL config file, not a project marker — so a walk that reaches home
    matches it and hands back the home directory as "the project". Everything
    keyed on the project root then points at ``$HOME``: the KG-collection
    fallback, ``KG_BASE_DIR``-relative writes, doctor paths. A project lives
    UNDER home, never at or above it, so home is both the rejection and the
    stopping point; a cwd outside home (``/opt/...``, ``/tmp/...``) never meets
    the boundary and walks as before. "Home" here means EITHER home this
    process could have — see :func:`_home_boundaries`.

    NOT cached: ``CLAUDE_PROJECT_DIR`` is patched between calls by the
    NEW-6 regression tests, and the cost is a handful of ``is_file()``
    stats. The expensive part (the hub round-trip) is memoised by
    :func:`_try_resolve_project_config`.
    """
    workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if workspace:
        try:
            candidate = Path(workspace)
            if candidate.is_dir():
                return candidate.resolve(), _CTX_WORKSPACE
        except OSError:
            pass
    from vco_lib.project_root import find_vco_project_root

    found = find_vco_project_root()  # home-bounded walk, one home
    if found is not None:
        return found, _CTX_CWD
    return _MODULE_OWN_ROOT, _CTX_MODULE


def _hub_context_is_authoritative() -> bool:
    """True when the hub was asked about a project we actually identified.

    False for :data:`_CTX_MODULE` — there the "project" is just wherever
    ``server.py`` was installed, so anything the hub says about it is an
    answer to a question nobody asked.
    """
    return _resolution_context()[1] != _CTX_MODULE


def _env_speaks(env_name: str, empty_means_unset: bool = False) -> bool:
    """True when ``env_name`` carries an explicit value for this process."""
    raw = os.environ.get(env_name)
    if raw is None:
        return False
    if empty_means_unset and not raw.strip():
        return False
    return True


def _hub_value_wins(*env_names: str, empty_means_unset: bool = False) -> bool:
    """Should a hub-resolved value take precedence over env here?

    THE one home for the precedence rule (v0.2.92). Hub-first stays the
    rule whenever we know which project we are. When the context is only a
    guess, an explicit env var — written into the project's own
    ``.claude/settings.json`` by the launcher — wins, because it describes
    the caller's project while the hub answer describes the orchestrator's.
    The hub answer is still used when env is silent: a guess beats a
    bundled default.
    """
    if _hub_context_is_authoritative():
        return True
    return not any(
        _env_speaks(name, empty_means_unset=empty_means_unset) for name in env_names
    )


#: ``(env_name, hub_value, env_value)`` triples recorded at module load when
#: the hub and an env var disagree. Surfaced once by
#: :func:`_log_collection_resolution` — a silent override of a correct env
#: var is exactly how the v0.2.92 report's issue 2 stayed invisible.
_HUB_ENV_CONFLICTS: "list[tuple[str, str, str]]" = []


def _note_hub_env_conflict(env_name: str, hub_value: str, env_value: str) -> None:
    """Record a hub-vs-env disagreement for the startup log."""
    entry = (env_name, hub_value, env_value)
    if entry not in _HUB_ENV_CONFLICTS:
        _HUB_ENV_CONFLICTS.append(entry)


def _try_resolve_project_config():
    """Best-effort: resolve the project config via vct-hub once.

    Returns the ProjectConfig dataclass or None. On None, every consumer
    falls back to its existing os.getenv() default. The resolver client
    emits its own rate-limited warning on the fall-through path
    (Step 17); this MCP doesn't need to log anything extra.

    v0.2.47 RL-6c follow-up: when ``VCT_DISABLE_HUB_RESOLVER=1`` is set in
    the environment, this short-circuits to None so test fixtures get
    pure env-fallback behavior. Without this guard, tests that
    monkey-patch ``KG_COLLECTION`` / ``SHARED_KG_COLLECTION`` /
    ``DIAGRAMS_COLLECTION`` env vars and reload the module had their
    injection silently overridden by whatever the live vct-hub on the
    dev machine reported. The env var is set once per test session via
    ``tests/conftest.py``'s autouse fixture. Production runs leave it
    unset, preserving the hub-first resolution semantics.
    (Historical note: pre-v0.2.102 this also covered DIAGRAMS_COLLECTION,
    removed with the diagrams integrations.)
    """
    global _resolved_project_config
    if _resolved_project_config is not None:
        return _resolved_project_config
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        return None
    # v0.2.81: the import-time guard now loud-fails on a broken vco_lib, so
    # `_resolve_project_config` is always a real callable here — the old
    # `if not _HAS_PROJECT_CONFIG` early-return is dead and was removed.
    try:
        # NEW-6 (2026-05-28): prefer CLAUDE_PROJECT_DIR over Path(__file__)
        # so MCP subprocesses launched against different workspaces resolve
        # to different projects. Pre-fix: every project's MCP resolved to
        # whichever workspace server.py physically lives in (the global
        # ~/.claude.json registration's path), causing telemetry mislabeling
        # and wrong KG collection routing for any non-default project.
        #
        # v0.2.92: the ladder (and, crucially, WHETHER the answer counts as
        # authoritative) now lives in `_resolution_context()`. A library
        # import from a project CLI lands on the CWD's project instead of
        # silently resolving the orchestrator's config.
        _project_root, _ = _resolution_context()
        _resolved_project_config = _resolve_project_config(_project_root)
        return _resolved_project_config
    except Exception:
        # Hub unreachable, project not registered, etc. — silent fall-
        # through to env. The resolver client already logged the cause
        # at its rate-limited warning level.
        return None


def _config_field_with_source(
    field_name: str,
    env_name: str,
    default: str,
    empty_means_unset: bool = False,
    hub_empty_is_meaningful: bool = False,
) -> "tuple[str, str]":
    """Resolve one config field and say WHERE the value came from.

    THE one home for the hub/env/default precedence AND for the source
    label that the startup log + error messages quote. Pre-v0.2.92 the
    value and its label were computed by two separate functions
    (``_config_field`` / ``_resolve_source_for``) that re-derived the same
    decision from different inputs — so the label could disagree with the
    value it described. It did: a library import got the orchestrator's
    collection names stamped ``src=hub``.

    Returns ``(value, source)`` where source is one of:
      ``"hub"``                      hub-resolved, context identified
      ``"hub(unverified-context)"``  hub-resolved from the module-path
                                     GUESS, used only because env is silent
      ``"env"``                      taken from ``env_name``
      ``"default"``                  bundled fallback
      ``"default(empty-env-coerced)"`` env was blank and coerced

    Args:
        field_name: Attribute name on the ProjectConfig dataclass.
        env_name: Env-var name to read on the env-fallback path.
        default: Value to return if neither hub nor env resolves.
        empty_means_unset: When True, an explicit empty-string env value
            (e.g. ``KG_COLLECTION=""``) is treated the same as "unset" and
            falls through to the default. When False (legacy), an empty
            env value is returned literally — used for keys where empty
            carries semantic meaning (e.g. DEVELOPMENT_COLLECTION's empty
            default disables docs-fanout in hybrid_search).
        hub_empty_is_meaningful: When True, an empty hub value is a real
            answer ("this project is explicitly unbound") rather than "no
            hub value" — the SHARED_KG_COLLECTION semantic.

    v0.2.27: ``empty_means_unset=True`` added for KG_COLLECTION-shape
    fields where an empty literal would propagate to Weaviate queries
    and cause schema-fail with a confusing error. The bug surfaced when
    ``.vscode/settings.json claude-code.env`` (an inert surface on
    Linux per PR-27) wrote ``KG_COLLECTION=""`` and the MCP picked it up.
    """
    cfg = _try_resolve_project_config()
    hub_value = ""
    hub_answered = False
    if cfg is not None:
        try:
            raw_hub = getattr(cfg, field_name, "")
            hub_value = str(raw_hub) if raw_hub else ""
            hub_answered = bool(hub_value) or hub_empty_is_meaningful
        except Exception:
            hub_value, hub_answered = "", False

    env_raw = os.environ.get(env_name)
    env_coerced = False
    if (
        empty_means_unset
        and isinstance(env_raw, str)
        and not env_raw.strip()
    ):
        env_raw = None
        env_coerced = True

    # Surface a disagreement rather than silently preferring one side.
    if hub_answered and env_raw is not None and env_raw != hub_value:
        _note_hub_env_conflict(env_name, hub_value, env_raw)

    if hub_answered and _hub_value_wins(
        env_name, empty_means_unset=empty_means_unset
    ):
        # The label carries the context, not just the origin: a hub answer
        # keyed on the module-path GUESS is a different claim from one keyed
        # on a project the harness (or the CWD) identified, and conflating
        # them is what let `src=hub` read as an endorsement it was not.
        return hub_value, (
            "hub" if _hub_context_is_authoritative() else "hub(unverified-context)"
        )
    if env_raw is not None:
        return env_raw, "env"
    if env_coerced:
        return default, "default(empty-env-coerced)"
    return default, "default"


def _config_field(
    field_name: str,
    env_name: str,
    default: str,
    empty_means_unset: bool = False,
) -> str:
    """Value-only wrapper over :func:`_config_field_with_source`."""
    return _config_field_with_source(
        field_name, env_name, default, empty_means_unset=empty_means_unset
    )[0]


# ── MIRROR (category C) — must match `vco_lib/weaviate_helpers.py::
#    weaviate_url_default`, which is the SHARED HOME for this resolution.
#
# Why a mirror and not a call: this MCP deliberately does NOT import vco_lib.
# It boots on half-installed environments where vco_lib is not importable,
# and adding a boot-critical import here would trade a real failure mode for
# a cosmetic convergence (same reasoning as `get_weaviate_client`, recorded
# in knowledge/concepts/weaviate-helper-convergence-mock-surface-pattern.md).
#
# The precedence below is therefore copied, and must stay byte-equivalent in
# BEHAVIOUR to the shared home:
#   1. WEAVIATE_URL  — a full URL the user stated (scheme+host+port); wins
#                      even when WEAVIATE_PORT disagrees.
#   2. WEAVIATE_PORT — a port for the canonical localhost host. Honouring it
#                      here is what lets a user who moved Weaviate off 8081
#                      still get search results; before v0.2.96 this line
#                      ignored the knob and every hybrid_search on a
#                      relocated instance silently addressed 8081.
#   3. 8081          — the canonical default.
# Empty / whitespace-only values are treated as UNSET at both levels.
_WEAVIATE_URL_ENV = (os.getenv("WEAVIATE_URL") or "").strip()
_WEAVIATE_PORT_ENV = (os.getenv("WEAVIATE_PORT") or "").strip()
WEAVIATE_URL = _WEAVIATE_URL_ENV or f"http://localhost:{_WEAVIATE_PORT_ENV or 8081}"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")


EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
EMBEDDING_SOURCE = os.getenv("EMBEDDING_SOURCE", "ollama")
# Dual-embedding support: when enabled, objects are stored with named vectors
# ("ollama_embed", "openai_embed") instead of a single flat vector.
# Enabled by default for fresh installs. Existing collections need migration
# first — see migrate_embeddings tool. Set to "false" to use legacy single-vector mode.
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"
# Active embedding for search queries:
#   KG: "qwen3" (default), "ollama" (legacy arctic), "openai"
#   Code: "codesage" (default), "ollama" (legacy jina), "openai"
# v0.2.21 Step 18: resolved via vct-hub with env-fallback (see _config_field
# above). Hub failure degrades silently to os.getenv.
ACTIVE_EMBEDDING = _config_field("active_embedding", "ACTIVE_EMBEDDING", "qwen3")


# Node formats sidecar — pre-generated descriptions/summaries for progressive disclosure.
# Loaded lazily on first use. Maps file_path → {title, description, summary, generated_at}.
KG_BASE_DIR = os.getenv("KG_BASE_DIR", "")
_node_formats_cache: dict | None = None
# Per-collection sidecar cache. Keys are collection names; values are the
# parsed sidecar dicts (or {} if no sidecar was found at the resolved path).
# This lets shared-KG results pull descriptions/summaries from the bundled
# vibecoded-orchestrator/knowledge/.node_formats.json while project results
# still use their own per-project sidecar.
_node_formats_by_collection: dict[str, dict] = {}


def _canonicalize_sidecar(data: object) -> None:
    """Re-key a loaded sidecar to canonical POSIX keys, in memory.

    v0.2.103 B1: lookups use the canonical POSIX ``file_path`` Weaviate
    stores on every OS, but a sidecar written on Windows before the fix
    keyed entries with ``\\`` — every lookup missed. The writer rewrites
    such a sidecar on its next run; until then the reader re-keys what it
    loaded (the file on disk is never touched from here). One home for the
    rule: ``vco_lib.node_formats_keys``.
    """
    if not isinstance(data, dict):
        return
    try:
        from vco_lib.node_formats_keys import canonicalize_keys
    except ImportError as exc:
        _reraise_vco_lib_import(exc, "node_formats_keys.canonicalize_keys")
    canonicalize_keys(data)


def _sidecar_lookup_key(file_path: str) -> str:
    """The canonical sidecar key for a result's ``file_path``."""
    try:
        from vco_lib.paths import to_posix_rel
    except ImportError as exc:
        _reraise_vco_lib_import(exc, "paths.to_posix_rel")
    return to_posix_rel(file_path)


def _load_node_formats() -> dict:
    """Load the project-KG .node_formats.json sidecar. Cached after first load.

    Looks under KG_BASE_DIR/knowledge/ first, then the cwd-relative path. This
    is the legacy entry point used everywhere that doesn't carry a collection
    hint. For collection-aware lookups, prefer ``_load_node_formats_for_collection``.
    """
    global _node_formats_cache
    if _node_formats_cache is not None:
        return _node_formats_cache

    # Try KG_BASE_DIR/knowledge/.node_formats.json, then cwd-relative
    candidates = []
    if KG_BASE_DIR:
        candidates.append(os.path.join(KG_BASE_DIR, "knowledge", ".node_formats.json"))
    candidates.append(os.path.join(os.getcwd(), "knowledge", ".node_formats.json"))

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.loads(fh.read())
                _canonicalize_sidecar(loaded)
                _node_formats_cache = loaded
                logger.info(f"Loaded node formats from {path} ({len(loaded)} entries)")
                return loaded
            except Exception as e:
                logger.warning(f"Failed to load node formats from {path}: {e}")

    _node_formats_cache = {}
    return _node_formats_cache


def _load_node_formats_for_collection(collection_name: str) -> dict:
    """Load the .node_formats.json sidecar appropriate for a given collection.

    Resolution rules:
      - Project KG (KG_COLLECTION) → uses the project sidecar (same as
        ``_load_node_formats``: KG_BASE_DIR/knowledge/.node_formats.json or
        cwd/knowledge/.node_formats.json).
      - Shared KG (SHARED_KG_COLLECTION) → uses the SHARED sidecar bundled
        with the orchestrator install. Resolution order:
          1. $SHARED_KG_NODE_FORMATS env override (absolute path, for tests).
          2. _SERVER_INFERRED_BASE/knowledge/.node_formats.json — the
             orchestrator's own knowledge/ directory shipped with this server.
      - Anything else (DEVELOPMENT_COLLECTION, code-graph collections, etc.)
        → empty dict; sidecar tiers don't apply.

    Caches per-collection to avoid re-reading on every result. Returns {} on
    miss / parse error so callers can safely treat the result as a dict.
    """
    if collection_name in _node_formats_by_collection:
        return _node_formats_by_collection[collection_name]

    candidates: list[str] = []
    if collection_name == KG_COLLECTION:
        # Same resolution as _load_node_formats — keep them in lockstep.
        if KG_BASE_DIR:
            candidates.append(os.path.join(KG_BASE_DIR, "knowledge", ".node_formats.json"))
        candidates.append(os.path.join(os.getcwd(), "knowledge", ".node_formats.json"))
    elif SHARED_KG_COLLECTION and collection_name == SHARED_KG_COLLECTION:
        env_override = os.getenv("SHARED_KG_NODE_FORMATS", "")
        if env_override:
            candidates.append(env_override)
        candidates.append(str(_SERVER_INFERRED_BASE / "knowledge" / ".node_formats.json"))
    # else: no sidecar resolution for unknown collections — fall through.

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.loads(fh.read())
                _canonicalize_sidecar(data)
                _node_formats_by_collection[collection_name] = data
                logger.info(
                    f"Loaded node formats for {collection_name} from {path} ({len(data)} entries)"
                )
                return data
            except Exception as e:
                logger.warning(f"Failed to load node formats from {path}: {e}")

    _node_formats_by_collection[collection_name] = {}
    return {}


def _get_node_format(file_path: str, level: str, collection_name: str | None = None) -> str | None:
    """Get a pre-generated format for a node.

    Args:
        file_path: Relative path (e.g., 'knowledge/tools/leanctx.md')
        level: 'description', 'summary', or 'chunk_summaries'
        collection_name: Optional collection the result came from. When given,
            the matching per-collection sidecar is consulted (project vs shared).
            When omitted, falls back to the legacy single-sidecar lookup so
            existing callers keep working.

    Returns:
        The formatted text (or dict for 'chunk_summaries'), or None if not available.
    """
    file_path = _sidecar_lookup_key(file_path)
    if collection_name:
        db = _load_node_formats_for_collection(collection_name)
        entry = db.get(file_path, {})
        val = entry.get(level) if isinstance(entry, dict) else None
        if val is not None:
            return val
        # If the collection-aware lookup misses but the legacy sidecar has it,
        # fall through. This covers the case where a result came from a
        # collection without sidecar support but file_path happens to map into
        # the project sidecar (rare but cheap to handle).
    db = _load_node_formats()
    entry = db.get(file_path, {})
    return entry.get(level) if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Score-driven retrieval verbosity tiers
# ---------------------------------------------------------------------------
# Thresholds calibrated 2026-04-10 on 18 relevant + 20 irrelevant queries
# (canonical source: previously inline in claude_mcp_servers/scripts/rl_kg_search.py).
#
# Tier semantics (RL-reranked or 1-distance score, range 0..1, higher=better):
#   < 0.42   → discard (noise from unrelated topics)
#   0.42..0.55 → "summary"      (LLM description from sidecar, or 200-char content)
#   0.55..0.65 → "single_chunk" (matched chunk, up to ~2000 chars)
#   0.65..0.75 → "three_chunks" (matched + neighbours, 3 chunks centred on hit)
#   >= 0.75  → "full"           (whole node, capped at 7 nearest chunks)
#
# Tunable at runtime via env-var overrides (kept as module constants so tests and
# rl_kg_search can override without monkey-patching imports).
def _safe_float(env_name: str, default: str) -> float:
    """Parse a float from an env var, tolerating a user typo.

    D-12 (v0.2.73): KG_TIER_*/CODE_TIER_* are user-documented tunables
    (CLAUDE.md). Previously `float(os.getenv(...))` ran at MODULE SCOPE, so
    a locale-comma or typo (e.g. KG_TIER_MIN=0,42) raised ValueError during
    import → the ENTIRE weaviate-kg MCP failed to start and every KG +
    codegraph tool disappeared behind a startup traceback the hook contract
    mostly hides. A tunable knob must not be a kill switch: on a bad value
    we log a WARNING and fall back to the calibrated default.
    """
    from vco_lib.env_values import env_float  # noqa: PLC0415 — the one env-float reader

    val = env_float(env_name, None)
    if val is not None:
        return val
    raw = os.getenv(env_name)
    if raw is not None and raw.strip() != "":
        # This reader's extra: a set-but-unparseable tier knob is SAID.
        logger.warning(
            "%s=%r is not a valid float; using default %s. "
            "(Tier knobs must be a plain decimal like 0.42 — a comma or "
            "stray character here would otherwise crash the whole MCP.)",
            env_name, raw, default,
        )
    return float(default)


_TIER_THRESHOLDS: dict[str, float] = {
    "min":          _safe_float("KG_TIER_MIN",          "0.42"),
    "single_chunk": _safe_float("KG_TIER_SINGLE_CHUNK", "0.55"),
    "three_chunks": _safe_float("KG_TIER_THREE_CHUNKS", "0.65"),
    "full":         _safe_float("KG_TIER_FULL",         "0.75"),
}


# Per-tier chunk window (how many chunks to assemble from a chunked node)
_TIER_CHUNK_WINDOW: dict[str, int] = {
    "single_chunk": 1,
    "three_chunks": 3,
    "full":         7,
}


def _get_result_verbosity_by_score(
    score: float,
    thresholds: dict[str, float] | None = None,
) -> str:
    """Return one of: 'discard' | 'summary' | 'single_chunk' | 'three_chunks' | 'full'.

    Score is normalised 0..1, higher=better.

    ``thresholds`` (v0.2.72 P4) selects the tier gate. Default ``None`` →
    ``_TIER_THRESHOLDS`` (the KG gate, min=0.42) — so KG callers that pass no
    ``thresholds`` get byte-identical v0.2.71 behaviour. The CODE path passes
    ``_CODE_TIER_THRESHOLDS`` (min=0.22) so good code matches aren't discarded.
    """
    t = thresholds if thresholds is not None else _TIER_THRESHOLDS
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s < t["min"]:
        return "discard"
    if s < t["single_chunk"]:
        return "summary"
    if s < t["three_chunks"]:
        return "single_chunk"
    if s < t["full"]:
        return "three_chunks"
    return "full"


def _chunk_summaries_header(
    file_path: str,
    shown_chunk_nums: list[int] | None = None,
    collection_name: str | None = None,
) -> str:
    """Build a small header listing per-chunk summaries from the sidecar.

    Used when assembling partial multi-chunk content (single_chunk / three_chunks
    tiers) so the agent gets a one-line orientation of every chunk in the source
    node, even those not included in the assembled body.

    Args:
        file_path: Relative path (e.g. 'knowledge/concepts/foo.md')
        shown_chunk_nums: Chunk numbers actually being assembled below the header.
            When provided, the header marks shown chunks with ▶ and unshown with ·
            so the agent can request additional chunks deliberately.

    Returns:
        Header string ending in two newlines, or "" if no chunk_summaries exist.
    """
    if not file_path:
        return ""
    chunk_summaries = _get_node_format(file_path, "chunk_summaries", collection_name)
    return _render_chunk_map(chunk_summaries, shown_chunk_nums)


def _render_chunk_map(chunk_summaries, shown_chunk_nums: list[int] | None) -> str:
    """Render a chunk_summaries dict as the ▶/· chunk-map header text.

    Shared by the KG header (``_chunk_summaries_header``) and the code header
    (``_code_chunk_summaries_header``) — one concern, one home. Returns ""
    when ``chunk_summaries`` is not a non-empty dict.
    """
    if not isinstance(chunk_summaries, dict) or not chunk_summaries:
        return ""

    shown = set(shown_chunk_nums or [])
    lines = ["[Chunk map:"]
    # Keys are stringified ints — sort numerically when possible.
    def _key(k: str) -> int:
        try:
            return int(k)
        except (TypeError, ValueError):
            return 0
    for k in sorted(chunk_summaries.keys(), key=_key):
        try:
            n = int(k)
        except (TypeError, ValueError):
            n = 0
        marker = "▶" if n in shown else "·"
        snippet = (chunk_summaries[k] or "").strip().splitlines()
        first_line = snippet[0] if snippet else ""
        if len(first_line) > 140:
            first_line = first_line[:137] + "…"
        lines.append(f"  {marker} {k}: {first_line}")
    lines.append("]")
    return "\n".join(lines) + "\n\n"


def _node_chunk_key(result: dict) -> "tuple[str, str, object]":
    """The identity of ONE chunk row for result de-duplication.

    W7 (v0.2.92 wiring audit): keyed on ``file_path`` FIRST — a title is not
    a node identity (measured live: 2 titles per collection map to two
    file_paths each), so a ``(title, chunk_number)`` key silently DROPS the
    colliding node's chunk at the same chunk_number, whichever arrived
    second. ``title`` is kept in the tuple so a row with no stored
    ``file_path`` (legacy; none measured live) still de-duplicates exactly
    as it did before.
    """
    return (
        result.get("file_path", "") or "",
        result.get("title", "") or "",
        result.get("chunk_number"),
    )


def _fetch_node_chunks(
    coll, title: str, hit_chunk: int, total: int, max_chunks: int,
    file_path: str = "",
):
    """Fetch up to ``max_chunks`` content chunks centred on ``hit_chunk``.

    Returns a list of (chunk_num, content) tuples sorted by chunk_num. Empty list
    on failure (collection unavailable, no chunks, etc.).

    W7 (v0.2.92 wiring audit): the filter keys on ``title AND file_path`` —
    title ALONE is NOT a node identity (measured live: 2 colliding titles in
    each of VCODev_KnowledgeGraph / VCODev_Development, each mapping to two
    file_paths), and a title-only fetch interleaved two nodes' chunks in the
    ``three_chunks`` / ``full`` window. ``file_path`` is the canonical node
    key everywhere else in the write path (kg-sync scopes deletes, the
    embed-skip, and the plan comparison by it) and measured 100% populated
    on real rows (764/764 KG, 338/338 Development). HONEST DEGRADATION: a
    result with NO file_path falls back to the title-only filter — the
    pre-fix behaviour, no worse — and any sibling chunks the tighter filter
    cannot see simply shrink the window, which the caller reports as a
    PARTIAL view (no ``coverage: complete`` hint) rather than silently
    mis-assembling another node's chunks into this one.

    v0.2.101 pull-in ③: ``title`` and ``file_path`` are word-TOKENIZED, so the
    two ``Equal`` clauses only NARROW — title "Knowledge Graph" at
    ``knowledge-graph.md`` token-matches "Orchestrator Knowledge Graph" at
    ``orchestrator-knowledge-graph.md``, and that node's chunks were assembled
    into this window. Rows are now confirmed in Python
    (``vco_lib.weaviate_exact_match``): exact ``file_path``, same title token
    sequence. The row bound is unchanged (``(total or max_chunks) + 1``) but
    now counts CONFIRMED rows, paging past any sibling flood.
    """
    try:
        import vco_lib.weaviate_exact_match as _wem

        chunk_filter = Filter.by_property("title").equal(title)
        if file_path:
            chunk_filter = chunk_filter & Filter.by_property("file_path").equal(
                file_path
            )
        objs = _wem.fetch_matching_rows(
            coll,
            chunk_filter,
            _wem.exact_row_predicate(
                paths={"file_path": file_path}, same_tokens={"title": title},
            ),
            max_matches=(total or max_chunks) + 1,
        )
        chunk_list: list[tuple[int, str]] = []
        for obj in objs:
            cn = int(_whole_number(obj.properties.get("chunk_num", 0)) or 0)
            chunk_list.append((cn, str(obj.properties.get("content", "") or "")))
        chunk_list.sort(key=lambda x: x[0])
        if max_chunks >= len(chunk_list):
            return chunk_list
        # Centre window on hit_chunk
        hit_idx = next((i for i, (cn, _) in enumerate(chunk_list) if cn == hit_chunk), 0)
        half = max_chunks // 2
        start = max(0, min(hit_idx - half, len(chunk_list) - max_chunks))
        return chunk_list[start:start + max_chunks]
    except Exception as exc:
        logger.debug("Chunk fetch failed for '%s': %s", title, exc)
        return []


def _label_single_chunk_coverage(base: dict, result: dict) -> None:
    """Coverage label for a SINGLE-chunk node whose body is ``base["content"]``.

    v0.2.102 W4-G (a dogfood finding): ``_format_obj`` truncates the display
    ``content`` to 300 chars, and the single-chunk fallbacks below returned
    that preview labelled ``coverage: complete`` + "No further Read … needed"
    — a false promise, so agents worked from a preview. Now: complete only
    when the body is the whole node; a truncated preview is labelled
    ``partial`` with a hint naming the file to Read.
    """
    if result.get("content_truncated"):
        fp = result.get("file_path", "") or ""
        base["coverage"] = "partial"
        base["retrieval_hint"] = (
            "Preview only (the first 300 characters of the node). Read "
            + (fp if fp else "the source file")
            + " for the full content."
        )
        return
    base["coverage"] = "complete"
    base["retrieval_hint"] = (
        "Full node provided (single-chunk node). No further Read of the "
        "source file is needed."
    )


def _format_result_by_tier(
    result: dict,
    tier: str,
    sidecar_db: dict | None = None,
    coll=None,
) -> dict | None:
    """Format a single search result at the requested verbosity tier.

    Args:
        result: Result dict from _format_obj or merged hybrid_search (must include
            at minimum: title, node_type, file_path, content; optional: tags,
            score, chunk_number, total_chunks).
        tier: One of 'discard' | 'titles' | 'summary' | 'single_chunk' |
            'three_chunks' | 'full' | 'descriptions' (legacy alias for 'summary').
        sidecar_db: Optional pre-loaded sidecar dict. When omitted, the cached
            sidecar is used via _get_node_format. Pass an explicit dict in tests.
        coll: Optional Weaviate collection handle. Required for multi-chunk
            assembly (single_chunk / three_chunks / full tiers). When omitted,
            those tiers fall back to the 300-char content snippet.

    Returns:
        Formatted result dict, or None when tier == 'discard'.
    """
    if tier == "discard":
        return None

    fp = result.get("file_path", "") or ""
    title = result.get("title", "")
    node_type = result.get("node_type", "")
    tags = result.get("tags", []) or []
    score = result.get("score")
    content = result.get("content", "") or ""
    total_chunks = result.get("total_chunks") or 1
    hit_chunk = result.get("chunk_number") or 1
    # Source collection — set by _format_obj when known. Used to pick the
    # right sidecar (project vs shared) for descriptions/summaries/chunk maps.
    source_collection = result.get("source_collection") or result.get("collection")

    # Helper to read sidecar respecting an injected db override (used by tests).
    def _sc(level: str):
        if sidecar_db is not None:
            entry = (
                sidecar_db.get(_sidecar_lookup_key(fp), {})
                if isinstance(sidecar_db, dict) else {}
            )
            return entry.get(level) if isinstance(entry, dict) else None
        return _get_node_format(fp, level, source_collection)

    base = {
        "title": title,
        "node_type": node_type,
        "file_path": fp,
        "tags": tags,
    }
    if score is not None:
        base["score"] = score
    # tier echoed back so callers (and tests) can see what was chosen.
    base["tier"] = tier

    if tier == "titles":
        return base

    if tier in ("summary", "descriptions"):
        # Decision: prefer description (longer, ~6 lines), fall back to summary
        # (1-2 lines), then to truncated content. This fixes BUG-SIDECAR-DESC-FALLBACK
        # — the old hybrid_search loop skipped 'summary' entirely.
        desc = _sc("description")
        summary = _sc("summary")
        if desc:
            base["description"] = desc
        elif summary:
            base["summary"] = summary
        else:
            base["content"] = content[:200] if len(content) > 200 else content
        return base

    # single_chunk / three_chunks / full — multi-chunk assembly when possible
    window = _TIER_CHUNK_WINDOW.get(tier, 1)
    chunks = (
        _fetch_node_chunks(
            coll, title, hit_chunk, total_chunks, window, file_path=fp
        )
        if coll is not None
        else []
    )
    if chunks and total_chunks and total_chunks > 1:
        shown_nums = [cn for cn, _ in chunks]
        # Whole-node summary header when partial view (all-tiers below 'full' for multi-chunk).
        node_summary = _sc("summary")
        header_parts: list[str] = []
        is_partial = len(chunks) < (total_chunks or len(chunks))
        if is_partial and node_summary:
            header_parts.append(f"[Node summary: {node_summary}]")
        # Chunk map header for single_chunk/three_chunks (orient agent across full node).
        if tier in ("single_chunk", "three_chunks"):
            chunk_map = _chunk_summaries_header(
                fp, shown_chunk_nums=shown_nums, collection_name=source_collection
            )
            if chunk_map:
                # _chunk_summaries_header already trails with \n\n; strip for join below.
                header_parts.append(chunk_map.rstrip())
        body = "\n".join(c for _, c in chunks)
        prefix = ("\n\n".join(header_parts) + "\n\n") if header_parts else ""
        base["content"] = f"{prefix}{body}"
        base["chunks_shown"] = len(chunks)
        base["chunks_total"] = total_chunks
        # Coverage hint (2026-06-15): tell the caller explicitly when the
        # returned chunks already cover the ENTIRE node, so it doesn't waste a
        # Read on the source file. Only emitted when coverage is 100% — when
        # is_partial, the absence of the hint signals "more exists on disk".
        #
        # W7 (v0.2.92 wiring audit): ALSO requires the node key. Without a
        # `file_path` the assembler falls back to the title-only filter, which
        # under a duplicate title can return the right COUNT of the wrong rows
        # — "complete" would then vouch for a window that silently mixes two
        # nodes. Such a result degrades to an UNCLAIMED (partial) view, which
        # is the honest answer and tells the caller to Read the file.
        if not is_partial and fp:
            base["coverage"] = "complete"
            base["retrieval_hint"] = (
                "Full node provided (all chunks). No further Read of the source "
                "file is needed."
            )
        return base

    # v0.2.102 W4-G: a SINGLE-chunk node whose row was fetched → return the
    # row's FULL body (the result's `content` is `_format_obj`'s 300-char
    # display preview). "complete" keeps the W7 rule: it needs the node key
    # (`file_path`) and exactly one confirmed row.
    if chunks and not (total_chunks and total_chunks > 1):
        base["content"] = "\n".join(c for _, c in chunks)
        if fp and len(chunks) == 1:
            _label_single_chunk_coverage(base, {"file_path": fp})
        else:
            # No node key, or more than one row matched: the body may mix two
            # nodes (the W7 rule), so it is not certified — say so explicitly,
            # like the single-chunk preview path, instead of leaving no label.
            base["coverage"] = "partial"
            base["retrieval_hint"] = (
                "Could not confirm this is the whole node (no file key, or "
                "more than one matching row). Read "
                + (fp if fp else "the source file")
                + " for the full content."
            )
        return base

    # Single-chunk node (or coll unavailable) — return content as-is.
    if tier == "single_chunk":
        # For multi-chunk nodes where we couldn't fetch, prepend node summary if available.
        node_summary = _sc("summary") if (total_chunks and total_chunks > 1) else None
        if node_summary:
            base["content"] = f"[Node summary: {node_summary}]\n\n{content}"
        else:
            base["content"] = content
            # Single-chunk node at single_chunk tier → complete unless the
            # content is a truncated preview (then partial + Read hint).
            if not (total_chunks and total_chunks > 1):
                _label_single_chunk_coverage(base, result)
        return base

    # three_chunks / full fallback when chunk fetch failed: return full snippet.
    base["content"] = content
    # Coverage hint (2026-06-15): a single-chunk node (total_chunks <= 1) is
    # returned in its entirety here — this is the common `full`-tier case for
    # the many KG nodes that embed as one chunk (chunks are capped by the
    # R39 policy ceiling at 8 192 tokens / ~32KB, well inside large-context
    # embedders like qwen3's 10 240 num_ctx). Tell the caller so it doesn't
    # redundantly Read the source file. For a MULTI-chunk node that reached this
    # fallback because chunk-fetch FAILED, coverage is NOT complete (content is
    # the single matched chunk's snippet) → no hint, so the caller knows to Read.
    # v0.2.102 W4-G: unless that content is `_format_obj`'s truncated
    # preview — then it is labelled partial with a hint naming the file.
    if not (total_chunks and total_chunks > 1):
        _label_single_chunk_coverage(base, result)
    return base


# v0.2.21 Step 18: resolved via vct-hub (with env-fallback). Hub failure
# degrades to os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph") which
# preserves pre-v0.2.21 behaviour.
#
# v0.2.27: ``empty_means_unset=True`` — an explicit empty string in the
# env (e.g. from a stale ``.vscode/settings.json claude-code.env`` block)
# falls through to the default rather than being used literally. An empty
# collection name would propagate to Weaviate queries and cause
# schema-fail with a confusing error message — see the "every configured
# collection schema-failed" bug from 2026-05-22.
KG_COLLECTION, _KG_COLLECTION_SOURCE = _config_field_with_source(
    "kg_collection",
    "KG_COLLECTION",
    "ClaudeKnowledgeGraph",
    empty_means_unset=True,
)
# Cross-project shared collection. Defaults to
# "VibeCodedOrchestrator_KnowledgeGraph" (since v0.2.23 B1; was
# "VibecodedOrchestrator_KnowledgeGraph" v0.2.12–v0.2.22, itself renamed
# from "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E — see
# `vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME` for the pre-v0.2.12
# legacy alias and `_LEGACY_SHARED_KG_NAME_LOWERCASE_C` for the v0.2.12–
# v0.2.22 alias). The bundled cross-project KG is seeded at install time
# from vibecoded-orchestrator/knowledge/ — which, as of v0.2.81, is itself
# materialized ONCE from templates/knowledge/ by install.py Step 4d
# (root ≡ shared collection). Non-root projects no longer carry per-project
# copies of the curated set; they read it here via the shared-read fan-out
# (_kg_search_collections), opt-out via SHARED_KG_READ_DISABLED.
#
# Asymmetric access (2026-05-01): SHARED_KG_COLLECTION is ALWAYS exposed to
# read paths when set. There is no per-project read opt-out — every project
# always reads the shared KG. The per-project gate below restricts WRITES
# only.
_SHARED_KG_DEFAULT = "VibeCodedOrchestrator_KnowledgeGraph"
# v0.2.21 Step 18: resolved via vct-hub when reachable; env-fallback
# otherwise. When the hub is reachable, an explicit empty value means
# "no shared KG binding for this project" and we honour it AS-IS — the
# asymmetric-access design (module docstring) says a project that's
# been explicitly unbound stays unbound. The default only applies on
# env-fallback (hub unreachable), matching pre-v0.2.21 behaviour.
#
# v0.2.92: routed through the shared resolver so the "explicit empty = this
# project is unbound" semantic (``hub_empty_is_meaningful``) and the
# authoritative-context gate live in ONE place. The gate matters most here:
# under a merely-guessed context, an "unbound" answer about the ORCHESTRATOR
# would have blanked the calling project's shared KG.
SHARED_KG_COLLECTION, _SHARED_KG_COLLECTION_SOURCE = _config_field_with_source(
    "shared_kg_collection",
    "SHARED_KG_COLLECTION",
    _SHARED_KG_DEFAULT,
    hub_empty_is_meaningful=True,
)

# Per-project WRITE gate. When true, store_knowledge_node refuses writes
# whose resolved target is SHARED_KG_COLLECTION (scope='shared' or explicit
# match). Reads are unaffected. Default false (writes allowed by default).
#
# Legacy alias: SHARED_KG_OPT_OUT used to gate BOTH reads and writes. We
# now honour it as a write-only fallback when SHARED_KG_WRITE_DISABLED is
# unset, so existing per-project env files keep restricting writes after
# the rename. Removal targeted for ~3 releases (2026-08).
def _resolve_shared_kg_write_disabled() -> bool:
    """Resolve the write-disabled flag from env, honouring the legacy alias.

    Precedence:
      1. SHARED_KG_WRITE_DISABLED (canonical key) — wins if set, even when
         set to a falsy spelling like "false" / "0" / "" so users can
         explicitly RE-ENABLE writes on a project that previously had the
         legacy SHARED_KG_OPT_OUT=true.
      2. SHARED_KG_OPT_OUT (legacy alias) — read only when the canonical
         key is unset (i.e. literally absent from the environment).
      3. False otherwise (default: writes allowed).

    Returns:
        True if shared-KG writes are disabled, False otherwise.
    """
    canonical = os.environ.get("SHARED_KG_WRITE_DISABLED")
    if canonical is not None:
        return canonical.strip().lower() in ("1", "true", "yes")
    legacy = os.environ.get("SHARED_KG_OPT_OUT")
    if legacy is not None:
        return legacy.strip().lower() in ("1", "true", "yes")
    return False


SHARED_KG_WRITE_DISABLED = _resolve_shared_kg_write_disabled()
# Back-compat module attribute. Existing tests and callers may still read
# `SHARED_KG_OPT_OUT` from this module — keep the symbol pointing at the
# resolved write-disabled value so its observable semantics match the new
# write gate (legacy callers that relied on this to gate reads will, by
# design, now read the shared KG anyway).
SHARED_KG_OPT_OUT = SHARED_KG_WRITE_DISABLED


# Per-project READ gate (v0.2.46 — Decision B). Symmetric mirror of the
# WRITE gate above. When true, ``_kg_collections_to_search`` drops
# ``SHARED_KG_COLLECTION`` from the fan-out list — hybrid_search /
# semantic_graph_search stop fanning out to the shared KG for this
# project. The project's own primary KG + any peers granted via the
# access matrix remain searchable.
#
# No legacy alias: ``SHARED_KG_READ_DISABLED`` is canonical from v0.2.46
# onward. Pre-v0.2.46 the read path was unconditional (the asymmetric
# model), so there's no historical key to honour. ``SHARED_KG_OPT_OUT``
# is a WRITE-only alias (see comment above) — it does NOT gate reads.
#
# Default false (reads allowed). Asymmetric-by-default semantic: fresh
# projects READ the shared KG. Users who want strict isolation flip both
# SHARED_KG_READ_DISABLED and SHARED_KG_WRITE_DISABLED to true.
def _resolve_shared_kg_read_disabled() -> bool:
    """Resolve the read-disabled flag from the environment.

    Precedence:
      1. ``SHARED_KG_READ_DISABLED`` (canonical key) — wins if set.
      2. False otherwise (default: reads allowed).

    Returns:
        True if shared-KG reads are disabled for this project,
        False otherwise.
    """
    canonical = os.environ.get("SHARED_KG_READ_DISABLED")
    if canonical is not None:
        return canonical.strip().lower() in ("1", "true", "yes")
    return False


SHARED_KG_READ_DISABLED = _resolve_shared_kg_read_disabled()
# Project-specific documentation collection (e.g. ProjectName_development).
# When set, hybrid_search also searches this collection automatically.
# Auto-pairing convention: the launcher should set `KG_COLLECTION=Foo` AND
# `DEVELOPMENT_COLLECTION=Foo_development` together. We do NOT auto-derive
# here — `vco_lib.config_projection` and `_ensure_collections` (install.py)
# are the canonical writers; the server just reads. semantic_graph_search
# uses KG_COLLECTION only — docs have no WikiLinks so graph traversal can't
# find useful neighbors there.
# v0.2.21 Step 18: resolved via vct-hub (with env-fallback to "").
DEVELOPMENT_COLLECTION, _DEVELOPMENT_COLLECTION_SOURCE = _config_field_with_source(
    "development_collection", "DEVELOPMENT_COLLECTION", ""
)


# ─── Multi-source access matrix (P1-D, 2026-05-08) ─────────────────────────
#
# The launcher's GUI access matrix is propagated into env vars that the MCP
# server (and bundled hooks) read at process start to fan-out KG / code-graph
# searches across peer projects. Without these vars, the matrix was a
# launcher-internal feature with no runtime effect.
#
# Format (set by `vco_lib.config_projection`, in `.claude/env`
# AND `.claude/settings.json env` — PR-27 / v0.2.12 removed the historical
# third surface `.vscode/settings.json claude-code.env` after sentinel
# testing on `/proc/<mcp_pid>/environ` proved it does NOT propagate to
# MCP subprocesses on Linux. Users editing the VS Code key for KG/code-
# graph routing report "settings didn't take" — they need to use the
# canonical `.claude/settings.json env` channel instead, or the launcher
# GUI which writes both files):
#
#   VCT_KG_ACCESS_LIST=PeerA,PeerB,PeerC
#       Comma-separated peer project NAMES (already sanitized to the
#       collection-prefix shape). Empty / unset = no peers granted (the
#       default).
#   VCT_CODE_GRAPH_ACCESS_LIST=PeerA,PeerB
#       Same shape, for the code-graph access matrix.
#
# Each peer maps to one KG collection (`<Peer>_KnowledgeGraph`) for KG and
# 5 collections (`<Peer>_CodeFunction`, `<Peer>_CodeClass`, ...) for code.
# Resolution is done via `_sanitize_collection_prefix` which is idempotent
# for already-sanitized inputs.

def _parse_csv_env(name: str) -> list[str]:
    """Parse a comma-separated env var, dropping empties and stripping
    whitespace. Returns [] when the var is unset or empty."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _kg_peer_collections() -> list[str]:
    """Return the list of peer-project KG collection names this process
    should search.

    v0.2.21 Step 18 (caller migration): prefer the launcher's
    hub-resolved ``kg_access_list`` (already-canonical collection names,
    no sanitization needed) over the legacy ``VCT_KG_ACCESS_LIST`` CSV
    env var. Falls back to the env CSV when the hub is unreachable
    (parent plan §8.11 — VCT_KG_ACCESS_LIST is the env-fallback path).

    The hub returns the FULL access list (self + shared + peers, all
    canonical Weaviate class names); we filter to peers only so the
    caller of ``_kg_collections_to_search`` can prepend self/shared in
    their canonical positions. Pre-v0.2.21 env-fallback path keeps the
    sanitize-then-suffix logic for the legacy CSV format.
    """
    # Hub-first path: kg_access_list is canonical class names.
    # v0.2.92: only when the hub was asked about a project we identified —
    # under the module-path guess an explicit VCT_KG_ACCESS_LIST (written into
    # the caller's own .claude/settings.json) describes the caller's peers,
    # while the hub answer describes the orchestrator's.
    _cfg_for_peers = _try_resolve_project_config()
    if _cfg_for_peers is not None and _hub_value_wins("VCT_KG_ACCESS_LIST"):
        out: list[str] = []
        seen: set[str] = set()
        for coll in _cfg_for_peers.kg_access_list:
            # Skip self / shared — caller of _kg_collections_to_search
            # prepends those in their canonical order.
            if coll == KG_COLLECTION or coll == SHARED_KG_COLLECTION:
                continue
            if coll in seen or not coll:
                continue
            seen.add(coll)
            out.append(coll)
        return out

    # Env-fallback path (pre-v0.2.21 / hub unreachable).
    peers = _parse_csv_env("VCT_KG_ACCESS_LIST")
    out = []
    seen = set()
    for p in peers:
        # `_sanitize_collection_prefix` is idempotent for already-sanitized
        # input; this lets the launcher pass either form (it always passes
        # the sanitized form, but we tolerate both for forward-compat with
        # tooling that builds its own VCT_KG_ACCESS_LIST).
        prefix = _sanitize_collection_prefix(p)
        if not prefix:
            continue
        coll = f"{prefix}_KnowledgeGraph"
        if coll in seen:
            continue
        seen.add(coll)
        out.append(coll)
    return out


def _kg_collections_to_search(include_dev: bool = False) -> list[str]:
    """Return the union of KG collections this process should fan-out
    across: self + shared (when configured + distinct) + every peer in
    `VCT_KG_ACCESS_LIST`. Order: self first, shared second, peers after.
    Caller may pass `include_dev=True` to also include
    `DEVELOPMENT_COLLECTION` (only `hybrid_search` does — graph traversal
    skips dev docs, see existing comment at the call site).

    v0.2.102: the ``include_diagrams`` leg (per-project
    ``DIAGRAMS_COLLECTION`` + diagram-access peers, Phase 1.5.C) was
    removed with the diagrams integrations.

    Defensive filtering (v0.2.27): empty / whitespace-only collection
    names are dropped. KG_COLLECTION should never be empty (the resolver
    coerces empty env to the bundled default), but the access matrix is
    user-controlled and could carry an empty entry; without this filter
    that empty propagates to Weaviate as an unresolvable class name and
    schema-fails.
    """
    out: list[str] = []
    if KG_COLLECTION and KG_COLLECTION.strip():
        out.append(KG_COLLECTION)
    # v0.2.46 Decision B — SHARED_KG_READ_DISABLED is the symmetric mirror
    # of SHARED_KG_WRITE_DISABLED. When true the shared collection drops
    # out of the fan-out so hybrid_search / semantic_graph_search stop
    # searching it for this project. Same single-line gate covers both
    # callers (``hybrid_search`` + ``semantic_graph_search``) because both
    # route through this helper.
    if (
        SHARED_KG_COLLECTION
        and SHARED_KG_COLLECTION.strip()
        and SHARED_KG_COLLECTION != KG_COLLECTION
        and not SHARED_KG_READ_DISABLED
    ):
        out.append(SHARED_KG_COLLECTION)
    for coll in _kg_peer_collections():
        if not coll or not coll.strip():
            continue
        if coll == KG_COLLECTION or coll == SHARED_KG_COLLECTION:
            continue
        if coll not in out:
            out.append(coll)
    if (
        include_dev
        and DEVELOPMENT_COLLECTION
        and DEVELOPMENT_COLLECTION.strip()
        and DEVELOPMENT_COLLECTION not in out
    ):
        out.append(DEVELOPMENT_COLLECTION)
    return out


# ─── v0.2.102 S2: per-node cross_project_access enforcement (read side) ───
#
# The verdict, the row filter and the requester-id rungs live in ONE home,
# `vco_lib/kg_node_access.py`, shared with the kg-search / kg-info CLIs and
# (through the wrappers below) the hook router's KG leg — so the launcher's
# "Enforced on cross-project reads" notice holds on EVERY KG read surface,
# not only this MCP's fan-out. These wrappers only bind the module's own
# config seam (`_try_resolve_project_config`) as the requester source.
import vco_lib.kg_node_access as _kg_node_access  # noqa: E402

_cross_project_access_allows = _kg_node_access.allows


def _own_project_id_for_access() -> str:
    """This process's launcher project id, or ``""`` when unresolvable.

    Hub-resolved ``ProjectConfig.project_id`` first, then the
    launcher-projected ``$VCT_PROJECT_ID`` (``kg_node_access.
    requester_from_config``); ``""`` when neither is known, and
    ``_cross_project_access_allows`` then denies restricted rows.
    """
    return _kg_node_access.requester_from_config(_try_resolve_project_config())


def _filter_cross_project_access(objects: list, *, is_self: bool) -> list:
    """Apply the per-node access filter to ONE fan-out collection's rows.

    ``is_self`` → returned unchanged (the self collection is never filtered;
    each call site decides what "self" means — KG: ``coll_name ==
    KG_COLLECTION``; code-graph: the row's prefix equals the effective
    project's own prefix, with the bare cross-tenant path treated as self by
    explicit request). The requester id is resolved lazily — only when a
    row's verdict depends on it.
    """
    return _kg_node_access.filter_objects(
        objects, is_self=is_self, requester=_own_project_id_for_access,
    )


# Fallback base directory: the orchestrator project root inferred from this server's location.
# server.py lives at  <project>/claude_mcp_servers/weaviate_mcp/server.py
# so parent.parent.parent = <project>/
_SERVER_INFERRED_BASE: Path = Path(__file__).resolve().parent.parent.parent


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as a Weaviate collection prefix.

    **Canonical rule** (cross-language, locked 2026-05-25 by cr-b2):
    delegates to ``vco_lib.project_init.sanitize_for_weaviate_class`` —
    the documented source-of-truth per ``derive_project_collection_names``'s
    docstring. Replaces the pre-cr-b2 underscore-replace implementation
    which diverged from the canonical Python rule (and from the
    indexer's writer-side naming) for any project name containing
    non-alphanumeric characters (spaces, hyphens, dots). The
    divergence silently broke cross-project diagrams visibility on
    first invocation — the indexer wrote under one class, this MCP
    searched a different one, the hub's ``diagrams_access_list``
    pointed at a third.

    Rule (must match ``sanitize_for_weaviate_class``):
      1. Split on any non-alphanumeric run (``[^A-Za-z0-9]+``).
      2. PascalCase each surviving part (uppercase first char,
         preserve rest).
      3. Concatenate (NO joiner — no underscore between parts).
      4. If nothing survives OR result starts with a non-letter,
         fall back to ``"vct"`` (lowercase — Weaviate uppercases on
         POST regardless).

    The Rust mirror is
    ``launcher/src-tauri/vct-hub/src/config_api.rs::sanitize_diagrams_class_prefix``;
    cross-language parity is pinned by
    ``tests/test_diagrams_class_name_parity.py`` /
    ``launcher/src-tauri/tests/diagrams_class_name_parity.rs``
    consuming the shared JSON fixture at
    ``tests/fixtures/diagrams_class_name_parity.json``.

    v0.2.81: the import-time ``_HAS_CANONICAL_SANITIZER`` fallback (a
    silent inline re-implementation on a broken vco_lib) is GONE — a
    failed ``vco_lib.project_init`` import now loud-fails at module load
    (see the guard above). ``sanitize_for_weaviate_class`` never raises
    (returns ``"vct"`` on garbage input), so no runtime safety net is
    needed here.
    """
    return _canonical_sanitize_for_weaviate_class(name)


_cached_embed_service: "EmbeddingService | None" = None
_embed_service_construction_failed_at: float = 0.0  # epoch seconds
#: v0.2.102 W6-B2: why the last construction failed (quoted by the loud
#: outage error) and when the cached service last re-read its code record.
_embed_service_construction_error: str = ""
_embed_record_checked_at: float = 0.0
_EMBED_SERVICE_RETRY_WINDOW = 10.0  # don't re-probe failed construction more than once per 10s


def serialize_datetime(value):
    """Convert datetime objects to ISO format strings for JSON serialization"""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


_CHUNK_HEADER_RE = re.compile(r'^\[chunk (\d+)/(\d+)\]\n\n', re.MULTILINE)


def _parse_chunk_header(content: str) -> tuple[int, int] | None:
    """
    Parse '[chunk N/total]' prefix from stored content.

    Returns (chunk_number_1indexed, total_chunks) or None if not a chunk.
    """
    m = _CHUNK_HEADER_RE.match(content)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _whole_number(value):
    """A whole-number float (``2.0``) as ``int``; anything else unchanged.

    ``chunk_num`` / ``total_chunks`` are Weaviate ``number`` properties, and
    both clients return a ``number`` as ``float`` (the weaviate-client path and
    kg_http's ``_convert``), so a chunk count rendered as "2/2.0 chunks" and
    reached int-only consumers as a float. Non-whole values (never written by
    the chunker) and non-floats pass through untouched.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _format_obj(obj, collection_name: str, distance: float | None = None) -> dict:
    """
    Format a Weaviate object into the standard result dict.

    Parses '[chunk N/total]' prefix from content (if present) and exposes
    chunk_number, total_chunks, and source_id as first-class fields so callers
    can do reliable dedup and neighbour fetching without re-parsing the prefix.
    """
    content = obj.properties.get("content", "")
    dist = distance if distance is not None else (
        obj.metadata.distance if obj.metadata else None
    )
    title = obj.properties.get("title", "Untitled")

    # Parse chunk metadata — prefer schema properties, fall back to content prefix.
    # F-G (v0.2.70): the ``or`` chains used to fall through to None when the
    # stored ``chunk_num``/``total_chunks`` were 0 (legacy single-chunk nodes
    # stored with 0 rather than 1). A None here propagates into the citation /
    # n_emb / sibling-fetch paths and can make a single-chunk node behave as if
    # it has no chunk identity. Maintainer ruling: a single-chunk node must be
    # NON-BLOCKING — absent/0/1 are all a VALID single-chunk node, never "skip".
    # So we read the property explicitly (preserving an explicit 0/None) and
    # normalise a missing/0 chunk to "chunk 1 of 1" at the END.
    parsed = _parse_chunk_header(content)
    _raw_chunk_num = _whole_number(obj.properties.get("chunk_num"))
    _raw_total = _whole_number(obj.properties.get("total_chunks"))
    chunk_number = (
        _raw_chunk_num if _raw_chunk_num not in (None, 0)
        else (parsed[0] if parsed else None)
    )
    total_chunks = (
        _raw_total if _raw_total not in (None, 0)
        else (parsed[1] if parsed else None)
    )
    # Normalise a single-chunk node (no chunk header + no usable chunk props) to
    # the canonical "chunk 1 of 1" so downstream (sibling refetch, citation
    # n_emb, adjacent-chunk fetch) treats it as a first-class node rather than
    # an identity-less one. Multi-chunk nodes (header parsed OR props >1) keep
    # their real numbers untouched.
    if chunk_number is None and total_chunks is None:
        chunk_number = 1
        total_chunks = 1
    source_id = obj.properties.get("source_node_id") or title

    # v0.2.102: the Phase 1.5.C ``result_kind`` discriminator
    # ("knowledge" | "diagram") was removed with the diagrams fan-out —
    # its only routing target (describe_excalidraw / DiagramsTab) is gone.

    # v0.2.70 Stream D-1: the Development docs collection (<PROJECT>_Development)
    # deliberately has NO `node_type` property (an explicit 2026-05-19 decision —
    # do NOT add the property; that reverses the decision and trips
    # test_v52_ag_schema_versions). So a docs result would default to "unknown"
    # and render as `type:unknown` in injected hook blocks. Formatter-only fix:
    # default node_type to "doc" ONLY for Development-collection results. True KG
    # nodes that legitimately lack a type keep "unknown" (a KG-appropriate
    # default), never silently "doc". Gated on the module-level
    # DEVELOPMENT_COLLECTION (the docs class for this project).
    _default_node_type = (
        "doc"
        if (collection_name and DEVELOPMENT_COLLECTION and collection_name == DEVELOPMENT_COLLECTION)
        else "unknown"
    )
    # v0.2.70 over-collapse fix: fingerprint the FULL body BEFORE truncation.
    # The `content` field below is truncated to `content[:300] + "..."` for
    # display, but the content-identity dedup (`_collapse_to_one_per_node`'s
    # second pass + the hook path's combine_kg_results) must key on the REAL
    # body — otherwise two legitimately-distinct same-title nodes sharing their
    # first 300 chars but differing in the tail would collapse to one, silently
    # dropping a node from Claude's context. The shared dedup helper prefers
    # this precomputed sha over re-hashing the truncated display field.
    # `content_sha` mirrors the seen-store sha1(body)[:12] convention via the
    # one Python home (rl_client.content_dedup.content_sha).
    try:
        from claude_mcp_servers.rl_client.content_dedup import (
            content_sha as _content_sha,
        )
        _full_content_sha = _content_sha((content or "").strip())
    except Exception:  # noqa: BLE001 — never break formatting on a dedup import
        _full_content_sha = ""
    return {
        "title": title,
        "node_type": obj.properties.get("node_type") or _default_node_type,
        "content": content[:300] + "..." if len(content) > 300 else content,
        # v0.2.102 W4-G: says the display `content` above is a PREVIEW, so the
        # tier renderer never labels it the whole node (`coverage: complete`)
        # — it fetches the full row or says "preview" and points at the file.
        "content_truncated": len(content) > 300,
        # Full-body fingerprint (untruncated) for content-identity dedup. See
        # the comment above + content_dedup.content_identity_key's truncation
        # guard. Empty string when content is empty (helper then falls back to
        # identity keying so a content-less node is never collapsed/dropped).
        "content_sha": _full_content_sha,
        "tags": obj.properties.get("tags", []),
        "file_path": obj.properties.get("file_path", ""),
        "created_at": serialize_datetime(obj.properties.get("created_at", "")),
        "updated_at": serialize_datetime(obj.properties.get("updated_at", "")),
        "distance": dist,
        "collection": collection_name,
        # Chunk metadata (None for un-chunked nodes)
        "source_id": source_id,
        "chunk_number": chunk_number,
        "total_chunks": total_chunks,
        # v0.2.47 RL-6b-2: surface KG wikilink titles so the RL enrichment
        # helper can fetch linked-node embeddings. Code collections + diagrams
        # don't have a `links` property → defaults to empty list. The KG
        # schema's `links: text[]` is populated by sync_knowledge_graph from
        # the markdown frontmatter / wikilink parse.
        "links": obj.properties.get("links", []),
    }


def _fetch_adjacent_chunks(coll, title: str, hit_num: int, total: int,
                           collection_name: str,
                           file_path: str = "") -> list[dict]:
    """
    Fetch the chunk immediately before (hit_num-1) and after (hit_num+1) for
    the same source node identified by *title*.

    W7 (v0.2.92 wiring audit): keyed on ``file_path AND chunk_num`` — the
    canonical per-node key — instead of ``source_node_id == title``. The old
    filter only ever matched MCP-written rows (whose source_node_id WAS the
    title) and, under duplicate titles (measured: 2 live collisions per
    collection), matched BOTH nodes' chunks at the same chunk_num and picked
    an arbitrary one via ``limit=1``. The file_path key matches every
    writer's rows (kg-sync always wrote a per-write uuid4 source_node_id, so
    the old Strategy 1 returned nothing for them).

    Falls back to the title filter + content-prefix parsing for objects that
    lack a file_path (legacy rows; none measured live).

    Returns formatted result dicts with distance=None (exact neighbour fetch).

    v0.2.101 pull-in ③: "exact" was not true — ``file_path`` and ``title`` are
    word-TOKENIZED, so ``file_path Equal knowledge/concepts/knowledge-graph.md
    AND chunk_num Equal 2`` also matched chunk 2 of
    ``orchestrator-knowledge-graph.md``, and ``limit=1`` could return THAT row
    as this node's neighbour. Both strategies now confirm rows in Python
    (``vco_lib.weaviate_exact_match``): Strategy 1 keeps only the exact
    ``file_path``; the Strategy 2 title fallback keeps only the same title
    token sequence and, when this result has a path, rejects rows stamped
    with a DIFFERENT path (another node) while still serving path-less legacy
    rows (its reason to exist).
    """
    import vco_lib.weaviate_exact_match as _wem

    target_nums = set()
    if hit_num > 1:
        target_nums.add(hit_num - 1)
    if hit_num < total:
        target_nums.add(hit_num + 1)
    if not target_nums:
        return []

    neighbours = []

    # Strategy 1: Property-based filter (fast, exact) for new-format objects
    if file_path:
        try:
            for target_num in target_nums:
                prop_filter = (
                    Filter.by_property("file_path").equal(file_path)
                    & Filter.by_property("chunk_num").equal(target_num)
                )
                obj = _wem.fetch_first_matching_row(
                    coll, prop_filter,
                    _wem.exact_row_predicate(paths={"file_path": file_path}),
                )
                if obj is not None:
                    neighbours.append(_format_obj(obj, collection_name, distance=None))
        except Exception:
            # file_path / chunk_num properties may not exist on this collection;
            # fall through to content-prefix fallback below.
            pass

    # If property-based fetch found all targets, return early
    found_nums = {nb.get("chunk_number") for nb in neighbours}
    remaining = target_nums - found_nums
    if not remaining:
        return neighbours

    # Strategy 2: Fallback -- title filter + content-prefix parsing (old objects)
    try:
        all_objs = _wem.fetch_matching_rows(
            coll,
            Filter.by_property("title").equal(title),
            _wem.exact_row_predicate(
                paths={"file_path": file_path},
                same_tokens={"title": title},
                missing_path_ok=True,
            ),
            max_matches=total or None,
        )
        for obj in all_objs:
            content = obj.properties.get("content", "")
            parsed = _parse_chunk_header(content)
            if parsed and parsed[0] in remaining:
                neighbours.append(_format_obj(obj, collection_name, distance=None))
    except Exception as exc:
        logger.debug("Adjacent chunk fetch failed for '%s': %s", title, exc)

    return neighbours


def _enrich_with_adjacent_chunks(coll, results: list[dict], collection_name: str) -> list[dict]:
    """
    For each chunked result in *results*, fetch adjacent chunks (N-1, N+1)
    and merge them into the list, de-duplicated by the per-NODE chunk
    identity ``_node_chunk_key`` (file_path + title + chunk_number — W7:
    a title alone is not a node identity).

    KG-2 (v0.2.73): neighbours are only RENDERED at the `three_chunks` and
    `full` tiers — the `summary` / `single_chunk` tiers show just the matched
    chunk. Fetching neighbours for a result that will land below the
    `three_chunks` tier is a wasted Weaviate round-trip whose rows the tier
    budget then discards. So we only fetch neighbours for results whose score
    is at least the `single_chunk` threshold (one tier below three_chunks —
    a conservative margin that still fetches for a result an RL rerank might
    bump UP into a neighbour-rendering tier, but skips the clearly-summary
    band). Results with no score (score unavailable) keep the old behaviour
    (fetch) so we never regress a legitimately-needed neighbour.

    Args:
        coll: Weaviate collection handle
        results: List of formatted result dicts (from _format_obj)
        collection_name: Collection name for formatting

    Returns:
        Combined list: original results + neighbour chunks, deduplicated.
    """
    seen: set = set()
    combined: list[dict] = []

    for r in results:
        key = _node_chunk_key(r)
        if key not in seen:
            seen.add(key)
            combined.append(r)

    # KG-2: the gate below which neighbour-fetch is pure waste. Use the KG
    # gate's single_chunk threshold as the conservative floor (three_chunks
    # is the first tier that renders neighbours; single_chunk gives a
    # one-tier rerank margin).
    _neighbour_floor = _TIER_THRESHOLDS["single_chunk"]

    def _score_of(r: dict) -> float | None:
        # score_cosine (1.0 - distance) is what results carry at enrich time
        # (before combined_score / _rerank are computed downstream); the
        # others cover the reranked / code-path shapes if this helper is
        # ever reused post-rerank.
        for k in ("combined_score", "_rerank", "score_cosine", "score", "_s"):
            v = r.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    # Fetch neighbours for chunked hits that could actually render them.
    for r in list(combined):
        cn = r.get("chunk_number")
        tc = r.get("total_chunks")
        if cn is None or tc is None:
            continue
        sc = _score_of(r)
        if sc is not None and sc < _neighbour_floor:
            # Will render at summary/single_chunk → neighbours discarded; skip.
            continue
        neighbours = _fetch_adjacent_chunks(
            coll, r["title"], cn, tc, collection_name,
            file_path=r.get("file_path") or "",
        )
        for nb in neighbours:
            nb_key = _node_chunk_key(nb)
            if nb_key not in seen:
                seen.add(nb_key)
                combined.append(nb)

    return combined


def _collapse_to_one_per_node(
    results: list[dict],
    score_field: str = "combined_score",
    *,
    key_fields: tuple[str, ...] = ("file_path", "title"),
    chunk_field: str = "chunk_number",
    dedup_kind: str = "kg",
) -> list[dict]:
    """
    Collapse multi-chunk matches of the same node into a single entry, keeping
    the highest-scoring chunk's full record and recording how many chunks of
    that node matched the query.

    Generalized (v0.2.72 P4) so BOTH the KG path (default args → identical
    v0.2.71 behaviour) and the CODE path share one collapse:
      * KG (default): key_fields=("file_path","title"), chunk_field=
        "chunk_number", dedup_kind="kg" — keys on (file_path, title) exactly
        as before; the content-identity second pass runs with kind="kg".
      * CODE: pass key_fields=("file_path","full_name"), chunk_field=
        "chunk_num", dedup_kind="code" — code chunks share a full_name; the
        content-identity pass runs with kind="code" (code_identity_key +
        code body fields).

    KG-SAFETY: every added parameter is keyword-only with a default equal to
    the v0.2.71 hard-coded value, so the four KG hot-path callers
    (server.py hybrid_search / semantic_graph_search at 5975/6041/6714/6809)
    pass no new kwargs and get byte-identical output. The KG parity test
    (tests/test_collapse_tier_generalized.py) asserts this.

    Why: the per-collection dedup keys on (title, chunk_number) — correct for
    merging semantic+keyword hits on the SAME chunk. But two different chunks
    of the same node both survive into the candidate list. To the agent this
    looks like duplicates; to the RL server (which keys EVERYTHING on title),
    the second chunk's signal silently overwrites the first's in
    retrieval_rl.py — corrupting both online training and offline logs.

    Collapsing here, upstream of _rl_cache_and_rerank, gives:
      - Clean user-visible top-K (no apparent duplicates)
      - Clean RL candidate pool (one entry per node title)
      - Clean offline training log (one record per node)
      - New `chunks_matched` field — useful learning signal: a node matched
        on N chunks is plausibly a stronger semantic match than one matched
        on a single chunk, even at the same combined_score.

    Key: (file_path or '', title) — file_path disambiguates rare cross-collection
    title collisions (e.g. same node title in shared and project KG).

    Returns: collapsed list, sorted by score_field desc. Each kept entry
    carries the WINNING chunk's score, content, and metadata, plus:
      - chunks_matched: int — number of distinct chunks of this node in input
      - best_chunk_number: int|None — the surviving chunk's number (None for
        unchunked nodes)
    """
    by_node: dict[tuple, dict] = {}
    counts: dict[tuple, int] = {}
    for r in results:
        # Key on the caller-chosen fields. Default ("file_path","title") is the
        # v0.2.71 KG key; code passes ("file_path","full_name").
        key = tuple((r.get(f) or "") for f in key_fields)
        # F1 (pre-gate audit, SEV-1): an ALL-EMPTY key is un-collapsible —
        # key such rows by object identity (mirrors code_ranking.py's
        # `("__id__", id(c))` guard in run_code_retrieval_pipeline's _dedup_key)
        # so rows whose props carry none of the key fields NEVER bucket
        # together into one survivor. Protects the KG path too (a row with
        # neither file_path nor title stays distinct).
        if not any(key):
            key = ("__id__", id(r))
        counts[key] = counts.get(key, 0) + 1
        existing = by_node.get(key)
        if existing is None or r.get(score_field, 0.0) > existing.get(score_field, 0.0):
            by_node[key] = r
    collapsed = []
    for key, r in by_node.items():
        r = dict(r)  # don't mutate caller's dict
        r["chunks_matched"] = counts[key]
        # best_chunk_number reads the caller's chunk field (KG "chunk_number",
        # code "chunk_num"). Kept under the SAME output key so downstream
        # consumers (RL, formatters) read it uniformly.
        r["best_chunk_number"] = r.get(chunk_field)
        collapsed.append(r)
    collapsed.sort(key=lambda x: x.get(score_field, 0.0), reverse=True)

    # v0.2.70 concern-2: a SECOND collapse on CONTENT IDENTITY. The (file_path,
    # title) key above keeps two rows that are the SAME node living in two
    # collections — the canonical case is one node present in BOTH the project
    # KG and the shared KG (same title + same body, different file_path). To the
    # agent those are one identical block injected twice. The shared helper
    # collapses entries that share BOTH a name AND a content fingerprint; its
    # over-collapse guard keeps two genuinely-distinct-title nodes that merely
    # share a body separate. Score-sorted above, so the first survivor per
    # content-identity is the highest-scoring representative.
    try:
        from claude_mcp_servers.rl_client.content_dedup import (
            dedup_by_content_identity,
        )
        # dedup_kind defaults to "kg" (unchanged); code passes "code" so the
        # content-identity pass uses code_identity_key + code body fields.
        collapsed = dedup_by_content_identity(collapsed, kind=dedup_kind)
    except Exception:  # noqa: BLE001 — never break retrieval on a dedup import
        pass
    return collapsed


# ─── EmbeddingService accessor ──────────────────────────────────────────
#
# v0.2.18: Lazy + cached EmbeddingService accessor.
#
# Why lazy: the MCP server is long-running (one process per Claude Code
# session). Constructing at import time would probe backends before the
# Weaviate/Ollama containers have settled, producing a stale
# NoEmbeddingBackendError that survives until the next session restart.
#
# Why cached: the service owns an HTTP connection pool; one instance
# amortises TLS+keep-alive across every embed call this MCP makes for
# the rest of the session.
#
# Why not module-level singleton: per the v0.2.18 locked design
# decision, EmbeddingService is per-project — but this MCP server IS
# pinned to one project for its lifetime (KG_COLLECTION env is set per-
# project by the launcher), so "per-MCP-instance" satisfies the
# per-project constraint.
#
# Concurrency: the cached service is initialised under the asyncio
# event loop's natural serialisation. Two near-simultaneous tool calls
# may both try to construct the service; the second's `for_project()`
# is a few-ms re-probe of already-running backends, and the cache write
# is idempotent. No lock needed.
#
# M-1 coupling: the cache globals ``_cached_embed_service`` /
# ``_embed_service_construction_failed_at`` / ``_EMBED_SERVICE_RETRY_WINDOW``
# live on ``server`` (tests poke ``server._cached_embed_service``), so this
# accessor reads/writes them via the ``server`` module object.


#: How often a cached EmbeddingService re-reads the code graph's recorded model.
_RECORD_RECHECK_SECS = 30.0


def _code_record_changed(svc, prefix: str) -> bool:
    """True when the launcher DB now records a different code model for
    ``prefix`` than the one ``svc`` resolved at construction. Soft-fails to
    False (a DB hiccup must not churn the service)."""
    try:
        from vco_lib.code_embed_model import CodeModelResolution, read_code_model_record

        res = getattr(svc, "code_model_resolution", None)
        if not prefix or not isinstance(res, CodeModelResolution):
            return False
        rec = read_code_model_record(prefix)
        before = res.recorded.model if res.recorded else None
        return before != (rec.model if rec else None)
    except Exception:  # noqa: BLE001
        return False


def _get_embedding_service():
    """Return a cached EmbeddingService instance for this MCP session.

    Returns None when:
      * vco_lib isn't importable (HAS_EMBEDDING_SERVICE=False), OR
      * Construction raised NoEmbeddingBackendError recently (within the
        10-second retry window — avoids hammering already-down services).

    On None, the WRITE/backfill helpers below fall through to their inline
    HTTP bodies, each of which embeds with its OWN named model into that
    model's slot (a transport fallback, never a model substitution). The
    SEARCH paths never do: they raise the loud
    ``vco_lib.embed_outage.EmbeddingBackendUnavailable`` (owner ruling
    2026-10-10 — a query embedded by another model searches noise). The
    construction error is kept on ``server._embed_service_construction_error``
    so that loud text can quote it.
    """
    from .kg_host import host as _kg_host
    server = _kg_host(__package__)
    if not server.HAS_EMBEDDING_SERVICE:
        return None
    import time as _time

    if server._cached_embed_service is not None:
        # v0.2.102 W6-B2: a long-lived MCP must not keep querying the PRE-flip
        # space after a fill completes (or a re-walk records another model) —
        # re-read the cheap DB record at most every _RECORD_RECHECK_SECS and
        # rebuild the service when it changed.
        now = _time.monotonic()
        if now - getattr(server, "_embed_record_checked_at", 0.0) < _RECORD_RECHECK_SECS:
            return server._cached_embed_service
        server._embed_record_checked_at = now
        if not _code_record_changed(server._cached_embed_service,
                                    getattr(server, "CODE_GRAPH_PROJECT", None) or ""):
            return server._cached_embed_service
        server.logger.info("weaviate-kg: the code graph's recorded model changed — "
                           "rebuilding the embedding service")
        server._cached_embed_service = None
    if (_time.monotonic() - server._embed_service_construction_failed_at) < server._EMBED_SERVICE_RETRY_WINDOW:
        return None  # in the retry window — don't probe again yet

    try:
        # v0.2.102 W6-B2: the code leg embeds in the space THIS project's
        # code graph is recorded as indexed in (vco_lib.code_embed_model).
        server._cached_embed_service = server.EmbeddingService.for_project(
            codegraph_prefix=getattr(server, "CODE_GRAPH_PROJECT", None) or None)
        return server._cached_embed_service
    except server.NoEmbeddingBackendError as e:
        server.logger.warning(
            "EmbeddingService construction failed (NoEmbeddingBackendError): %s. "
            "Falling back to legacy inline helpers for the next %.0fs.",
            e,
            server._EMBED_SERVICE_RETRY_WINDOW,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        server._embed_service_construction_error = str(e)
        return None
    except Exception as e:
        server.logger.warning(
            "EmbeddingService construction failed (%s) — falling back to "
            "legacy inline helpers.",
            e,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        server._embed_service_construction_error = str(e)
        return None


async def _get_search_vector(
    text: str, scheme: str = "kg", task: str | None = None
) -> tuple[list[float] | None, str]:
    """Get embedding for search, returns (vector, target_vector_name).

    v0.2.18: routes through EmbeddingService which resolves BOTH the
    embedding backend AND the named-vector slot from env in one place.
    Pre-v0.2.18 this branched on ACTIVE_EMBEDDING here, which duplicated
    the slot-resolution logic already living in the Wave-A
    EmbeddingService TEXT_SLOT_MAP / CODE_SLOT_MAP.

    v0.2.101: this is a SEARCH path, so it embeds through the QUERY-side
    methods (``embed_text_query`` / ``embed_code_query``) which apply the
    active model's query-prefix — documents still go through ``embed_text`` /
    ``embed_code`` unprefixed. ``task`` selects the per-use instruction
    sentence (see ``chunking.QUERY_TASKS``); ``None`` uses the scheme default.

    No legacy fallback (owner ruling 2026-10-10): when the service is
    unavailable or its backend stays down past the transient retry, this
    raises ``vco_lib.embed_outage.EmbeddingBackendUnavailable`` carrying the
    loud text (backend, container, watchdog state, remedy) — the pre-v0.2.102
    ACTIVE_EMBEDDING branch embedded with whatever the env named (a code
    query with CodeSage on a jina-indexed graph) and searched noise.

    Args:
        text: Text to embed.
        scheme: 'kg' or 'code' — determines text vs code backend.
        task: QUERY_TASKS key (KG search / hook injection / code-nl / code-sim).
    """
    from vco_lib.embed_outage import embed_query_or_raise

    from .kg_host import host as _kg_host
    server = _kg_host(__package__)
    svc = server._get_embedding_service()
    vec = await asyncio.to_thread(
        embed_query_or_raise, scheme, svc, text, task,
        construction_error=getattr(server, "_embed_service_construction_error", ""),
    )
    assert svc is not None  # embed_query_or_raise raised for a missing service
    return vec, (svc.code_vector_slot if scheme == "code" else svc.text_vector_slot)


async def _kg_search_vector(
    text: str, collection: str, task: str | None = None, *, steady=None,
) -> tuple[list[float] | None, str]:
    """The query vector + slot for searching ONE KG collection (v0.2.102 W6-I).

    The collection is searched with the model it is RECORDED as complete in
    (``vco_lib.kg_embed_model``). In the steady state that is the service's
    own model and this is exactly ``steady`` — the CALLER's own
    :func:`_get_search_vector` binding (so a seam patched on the caller's
    module is honoured; default: ``server``'s). While a newly selected model's slot
    fills, the query is embedded with the recorded model instead — through
    the same loud query-embed entry, never another model."""
    from vco_lib.kg_embed_model import recorded_query_model, sibling_text_service

    from .kg_host import host as _kg_host
    server = _kg_host(__package__)
    model = await asyncio.to_thread(recorded_query_model, collection) if collection else ""
    if model:
        from vco_lib.embed_outage import embed_query_or_raise

        svc = server._get_embedding_service()
        qs = sibling_text_service(svc, model) if svc is not None else None
        vec = await asyncio.to_thread(
            embed_query_or_raise, "kg", qs, text, task,
            construction_error=getattr(server, "_embed_service_construction_error", ""),
        )
        assert qs is not None  # embed_query_or_raise raised for a missing service
        return vec, qs.text_vector_slot
    fn = steady or server._get_search_vector
    if task is None:
        return await fn(text)
    return await fn(text, task=task)


# ─── Light host (v0.2.102 W6-M) ───────────────────────────────────────────
#
# Everything below serves the process where NO server module is loaded (the
# hook KG producer — see ``kg_host.host``). ``server`` never imports these
# names: it keeps its own weaviate-client ``get_weaviate_client`` / ``Filter``
# and its hub-resolved ``CODE_GRAPH_PROJECT``.

#: The hook KG producer has no code-graph context: its EmbeddingService is
#: constructed exactly as the KG CLIs construct theirs
#: (``EmbeddingService.for_project()`` — no ``codegraph_prefix``), which is what
#: ``_get_embedding_service`` builds when this is ``None``.
CODE_GRAPH_PROJECT = None

_http_weaviate_client = None


def _weaviate_host_port(url: str) -> "tuple[str, int]":
    """Host + HTTP port of ``WEAVIATE_URL`` — the ONE parse both clients use
    (``server.get_weaviate_client`` for weaviate-client, the light host's
    :func:`get_weaviate_client` for plain HTTP)."""
    http_host = url.replace("http://", "").replace("https://", "").split(":")[0]
    http_port = int(url.split(":")[-1]) if ":" in url else 8081
    return http_host, http_port


def get_weaviate_client():
    """Light-host twin of ``server.get_weaviate_client``: the same
    ``WEAVIATE_URL`` resolution, served over plain HTTP (``kg_http``). Raises
    ``WeaviateUnreachable`` when Weaviate does not answer its readiness probe;
    a failed client is never cached, so the next call retries."""
    global _http_weaviate_client
    if _http_weaviate_client is None:
        http_host, http_port = _weaviate_host_port(WEAVIATE_URL)
        _http_weaviate_client = HttpWeaviateClient(f"http://{http_host}:{http_port}")
    return _http_weaviate_client


def bind_into(namespace: dict, names) -> None:
    """Rebind each named function of this module into ``namespace``.

    The twin shares the function's code object, defaults and closure; only
    its globals become ``namespace``. ``server`` calls this for every moved
    function so the moved code reads server's globals — the exact resolution
    it had before the move (patches on ``server`` are seen, ``global``
    statements write ``server``'s state, ``Filter`` is weaviate-client's).
    """
    mine = globals()
    for name in names:
        fn = mine[name]
        twin = types.FunctionType(fn.__code__, namespace, fn.__name__,
                                  fn.__defaults__, fn.__closure__)
        twin.__kwdefaults__ = dict(fn.__kwdefaults__) if fn.__kwdefaults__ else None
        twin.__dict__.update(fn.__dict__)
        twin.__doc__ = fn.__doc__
        twin.__qualname__ = fn.__qualname__
        twin.__annotations__ = dict(fn.__annotations__)
        twin.__module__ = namespace.get("__name__", fn.__module__)
        namespace[name] = twin


def __getattr__(name: str):
    """Light-host fallback for ``server.<name>`` reads (PEP 562).

    Served in order from ``rl_enrichment`` (its functions — the names
    ``server`` re-exports from it), ``rl_state`` (the RL state + tuning
    constants ``server`` re-exports by reference / value) and, last, the real
    server module: a name this host does not carry costs the server import,
    it never goes missing.
    """
    if name.startswith("__"):
        raise AttributeError(name)
    from . import rl_enrichment as _rle
    from . import rl_state as _rls

    fn = vars(_rle).get(name)
    if isinstance(fn, types.FunctionType) and fn.__module__ == _rle.__name__:
        return fn
    if name in vars(_rls) and not isinstance(vars(_rls)[name], types.ModuleType):
        return vars(_rls)[name]
    logger.debug("kg_query_core: %r is not served by the light host — "
                 "loading the server module", name)
    import importlib

    return getattr(importlib.import_module(f"{__package__}.server"), name)
