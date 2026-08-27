# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project config resolver — Python client for the launcher's vct-hub.

This module is the Python counterpart of ``templates/scripts/vct_project_config.sh``
/ ``.ps1``. It talks to ``GET /api/v1/projects/{id}/config`` on the
launcher's local hub and returns a strongly-typed :class:`ProjectConfig`
dataclass with every field the orchestrator's hooks, MCPs and CLI
scripts need to operate: KG collection name, codegraph project slug,
embedding selections, access matrices, service URLs, etc.

This is the v0.2.21 fix for the "env-var thread" problem documented in
``docs/HOOK_TOKEN_AUDIT_2026-05-20.md`` (and the parent plan
``.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md``):
previously every consumer reached for ``os.environ["KG_COLLECTION"]``
and friends, which silently desynced from the launcher's GUI source-of-
truth whenever the user toggled access matrix or rebound a project.
Now every consumer routes through this resolver; the hub is the single
authoritative source.

Public API
~~~~~~~~~~

.. code-block:: python

    from pathlib import Path
    from vco_lib.project_config import resolve, ProjectConfig

    cfg: ProjectConfig = resolve(Path.cwd())
    print(cfg.kg_collection)           # str
    print(cfg.kg_access_list)          # tuple[str, ...]
    print(cfg.embedding_models.text)   # str

    # Single-field fast path (uses the hub's ?key= filter):
    from vco_lib.project_config import resolve_field
    kg = resolve_field(Path.cwd(), "kg_collection")  # str | None

Exceptions
~~~~~~~~~~

This module raises a small hierarchy rooted at :class:`ResolverError`.
Callers are expected to catch :class:`HubUnreachable` and decide whether
to fall back to env vars; env-fallback is intentionally NOT inside this
module so callers can decide per-context whether a partial config is
acceptable. Step 18's migrated callers wrap their resolve() call in a
try/except and degrade gracefully.

Discovery chain (mirrors the bash / ps1 clients)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* port:  ``$VCT_HUB_PORT`` → ``<vct_root_dir>/hub.port`` → ``7700``
* token: ``$VCT_HUB_TOKEN`` → ``<vct_root_dir>/hub.token`` → fail

Stale-env-token fallback (v0.2.91)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The env pin above wins on the FIRST attempt, always. But the hub
regenerates ``hub.token`` on every start, so a shell that exported
``VCT_HUB_TOKEN`` before an update holds a value the hub will refuse.
On a PROVABLE refusal (401/403) — and only then — the resolver retries
ONCE with the on-disk token when the env value provably differs from it,
prints one stderr line naming the fix, and otherwise leaves today's
error path untouched. ``VCT_HUB_TOKEN_STRICT=1`` disables that fallback
entirely (tests/harnesses that pin a bad token expecting the 401 path).
See :func:`_stale_env_token_fallback` — the ONE decision function, whose
sh/ps1/Rust mirrors must match it.

Both are cached in-process for 5 s after the first read so a hot-path
caller doing many resolves in one process pays the disk-read once. The
cache TTL was chosen short enough that a launcher restart (which rotates
the token) is observed by the next call within at most 5 s, matching
the parent plan's ≤30 s freshness guarantee.

Caching
~~~~~~~

* Hub discovery (port + token): **5 s in-process TTL**.
* Resolved :class:`ProjectConfig`: **no caching** — every call round-
  trips the hub. The hub is the source of truth; caching here would
  desync from the GUI within a single Edit. Hub round-trips on
  localhost are sub-millisecond.
* HTTP session: **singleton per process**, with one connection pooled.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests
import requests.adapters

from vco_lib.paths import vct_root_dir


# ─── Resolver protocol version (v0.2.22 Item #2) ──────────────────────
#
# Forward-compat anchor. The hub's response carries a `schema_version`
# field; this constant pins the version this client knows how to
# interpret. When the client sees a response with a HIGHER value, it
# emits a one-line stderr warning so the user has a diagnostic for
# "I upgraded the launcher but my hooks behave oddly" — the client
# still parses the body (additive fields default client-side), and
# raises only when the value is non-numeric / malformed.
#
# MUST stay in lock-step with `RESOLVER_PROTOCOL_VERSION` in
# `launcher/src-tauri/vct-hub/src/config_api.rs`. When bumping there,
# bump here in the same commit.
RESOLVER_PROTOCOL_VERSION: int = 1


# ─── Constants ──────────────────────────────────────────────────────────

#: Default hub port. Mirrors ``DEFAULT_PORT`` in
#: ``launcher/src-tauri/vct-hub/src/server.rs``.
DEFAULT_HUB_PORT: int = 7700

#: TTL (seconds) for the in-process hub-discovery cache. Short enough
#: that a launcher restart is observed quickly, long enough to amortise
#: disk reads when a hot caller resolves many times in one process.
HUB_DISCOVERY_TTL_SECONDS: float = 5.0

#: Connect/read timeouts for the singleton requests session. Localhost
#: should respond in single-digit ms; 2 s connect / 5 s read is plenty
#: while still failing fast when the launcher isn't running.
_CONNECT_TIMEOUT_SECONDS: float = 2.0
_READ_TIMEOUT_SECONDS: float = 5.0


# ─── Exception hierarchy ────────────────────────────────────────────────


class ResolverError(Exception):
    """Base class for every error raised by :func:`resolve`."""


# v0.2.76 (R5): honest post-update hint appended to the user-facing
# unreachable/401 messages. Requiring an editor/launcher restart after an
# orchestrator update is EXPECTED behaviour (user ruling 2026-07-10) — there is
# no auto-retry/auto-heal; the hint just tells the user what to do. MUST MATCH
# the string appended in `vct_secrets_resolve.sh`/`.ps1` and
# `vct_project_config.sh`/`.ps1`.
_POST_UPDATE_HINT = (
    " If VCO was just updated, restart the launcher and reload the editor "
    "window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
)


class HubUnreachable(ResolverError):
    """The launcher hub couldn't be contacted.

    Covers: missing ``hub.token`` file (launcher not running), connection
    refused, request timeout, 401 unauthorized (stale token after a
    launcher restart), and unexpected non-2xx/4xx/5xx statuses.
    Callers typically degrade to env-var fallback on this exception.
    """


class Unauthorized(ResolverError):
    """The hub responded 401.

    A subclass of nothing-but-:class:`ResolverError` so callers can
    distinguish "definitely-stale-token" from generic unreachability if
    they care; most callers won't and can catch :class:`HubUnreachable`
    via the alias path used by :func:`resolve`'s mapping.
    """


class ServiceMisconfigured(ResolverError):
    """The hub responded 503 ``service_misconfigured``.

    The launcher knows about the project but its primary KG binding is
    missing (backfill hasn't run / failed). The user can fix this in
    the launcher GUI. Callers should emit a loud warning rather than
    silently fall back.
    """


class ProjectNotFound(ResolverError):
    """The hub responded 404 ``project_not_found``.

    No row in ``projects`` matches the supplied id / slug / path.
    Either the project was never registered with the launcher, or it
    was registered then deleted.
    """


class FieldNotFound(ResolverError):
    """The hub responded 404 ``field_not_found`` to a ``?key=`` query.

    Either the field name is mis-spelled, or the field is absent from
    the assembled config (e.g. ``development_collection`` when no
    archive binding exists — the resolver emits an empty string for
    those, so this exception is more often a typo than a real absence).
    """


class Forbidden(ResolverError):
    """The hub responded 403 on a per-project ``/env`` / ``/config`` route.

    v0.2.77 flip. A HARD scoped-credential boundary refusal — the hub
    ACCEPTED the request and knows the project, but refused the bearer:
    either the coarse global ``hub.token`` on a per-project route with the
    compat window closed (``VCT_HUB_LEGACY_GLOBAL_ENV`` unset/deny), or a
    per-project token minted for a DIFFERENT project.

    Deliberately NOT a :class:`HubUnreachable` subclass: unreachability is
    a transient condition callers degrade to env-fallback on, whereas a 403
    is a persistent misconfiguration that env-fallback would MASK. Callers
    that catch :class:`HubUnreachable` for the fallback path will therefore
    let this propagate (or handle it explicitly) rather than silently
    resolving stale env values. The scoped ``hub.token.<id>`` is already
    preferred by the resolver's token picker; a 403 means that file was
    absent/unreadable (so we rode the global token) or the wrong project's
    token was presented. Fix: ensure the scoped token file exists, or set
    ``VCT_HUB_LEGACY_GLOBAL_ENV=1`` on the hub to reopen the compat window.
    """


# ─── Public dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtraCodegraphPath:
    """v0.2.47: a single read-only path contributing to a project's codegraph.

    Mirrors the Rust ``CodeGraphExtraPath`` struct in
    ``launcher/src-tauri/vct-hub/src/config_api.rs``. Frozen dataclass so
    callers can use it as a dict key / pass it across thread boundaries
    without defensive copies.

    Pre-v0.2.47 hubs don't ship this field — the parser back-fills the
    list with an empty tuple in that case, so old hubs paired with v0.2.47+
    clients don't crash.
    """

    path: str
    enabled: bool
    #: Pre-v0.2.47 unconfigured / never-analyzed paths set this to ``None``.
    #: Non-git extras also stay ``None`` (no SHA to track).
    last_indexed_commit: Optional[str] = None


@dataclass(frozen=True, slots=True)
class EmbeddingModels:
    """Per-project embedding selections (text + code)."""

    text: str
    code: str


@dataclass(frozen=True, slots=True)
class RetrievalTuning:
    """Global retrieval tuning thresholds.

    v0.2.22 Item #13 (2026-05-20). Sourced from the launcher GUI's
    Preferences → Retrieval tuning panel which writes
    ``<vct_root_dir>/retrieval-tuning.toml``; the hub re-reads the
    file on every ``/config`` response. Five env-tunable knobs:

    * ``code_graph_score_floor`` — pre-edit codegraph injection cutoff
    * ``kg_tier_min`` — KG result discard threshold (below = noise)
    * ``kg_tier_single_chunk`` — render single matched chunk above this
    * ``kg_tier_three_chunks`` — render matched + 2 neighbours above
    * ``kg_tier_full`` — render whole node above this

    Defaults pinned in ``score-driven-retrieval-tiers.md``:
    0.35 / 0.42 / 0.55 / 0.65 / 0.75.
    """

    code_graph_score_floor: float
    kg_tier_min: float
    kg_tier_single_chunk: float
    kg_tier_three_chunks: float
    kg_tier_full: float


#: Single source of truth for the calibrated RetrievalTuning defaults
#: on the Python side. MUST stay in lockstep with the Rust constants in
#: ``launcher/src-tauri/vct-hub/src/retrieval_tuning_io.rs`` and the
#: launcher's ``commands::retrieval_tuning`` module. Drift either way
#: is caught by ``tests/test_retrieval_tuning_roundtrip.py`` (drift
#: guard). Pinned in ``knowledge/concepts/score-driven-retrieval-tiers.md``.
_RETRIEVAL_TUNING_DEFAULTS: tuple[float, float, float, float, float] = (
    0.35,  # code_graph_score_floor
    0.42,  # kg_tier_min
    0.55,  # kg_tier_single_chunk
    0.65,  # kg_tier_three_chunks
    0.75,  # kg_tier_full
)


def _default_retrieval_tuning() -> RetrievalTuning:
    """Factory for the calibrated default RetrievalTuning.

    Used by (1) the ProjectConfig dataclass default, (2) the
    ``_from_hub_body`` parser when the hub omits ``retrieval_tuning``
    entirely (pre-v0.2.22 hubs paired with new clients), (3) any caller
    that builds a ``ProjectConfig`` by hand and doesn't care about
    overriding the thresholds.

    Returns a freshly-constructed value each call so callers can safely
    treat it as owned; the dataclass is ``frozen=True`` so sharing one
    instance would also be safe, but the factory keeps the contract
    explicit.
    """
    return RetrievalTuning(*_RETRIEVAL_TUNING_DEFAULTS)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Resolved per-project config.

    Mirrors the hub's ``ProjectConfigResponse`` struct in
    ``launcher/src-tauri/vct-hub/src/config_api.rs``. Every field is
    non-nullable; collection fields that the hub returns as empty
    strings (``shared_kg_collection``, ``development_collection``)
    remain empty strings here — callers can use ``if cfg.foo:`` to
    distinguish.
    """

    project_id: str
    project_path: str
    project_slug: str
    project_display_name: str
    code_graph_project: str
    code_graph_collection_prefix: str
    kg_collection: str
    shared_kg_collection: str
    development_collection: str
    active_embedding: str
    embedding_models: EmbeddingModels
    kg_access_list: tuple[str, ...]
    codegraph_access_list: tuple[str, ...]
    weaviate_url: str
    ollama_url: str
    grpc_port: int
    shared_kg_write_disabled: bool
    #: Absolute path to Claude Code's per-workspace session-jsonl
    #: directory (``~/.claude/projects/<slug>/``). New in v0.2.31. Used
    #: by the RL citation-monitor in the weaviate MCP to find Claude's
    #: session transcripts for the active workspace. The hub computes
    #: this via :func:`claude_session_dir_for` so the slug rule lives
    #: in exactly one place rather than drifting between inline copies.
    #: Pre-v0.2.31 hubs paired with v0.2.31+ clients omit the field;
    #: the parser back-fills with an empty string in that case so old
    #: hubs don't crash new clients (the MCP fallback path covers the
    #: empty-string sentinel by recomputing locally).
    claude_session_dir: str = ""
    #: Global retrieval tuning thresholds (v0.2.22 Item #13). Absent in
    #: pre-v0.2.22 hub responses; defaults to calibrated values when
    #: missing so old hubs paired with new clients don't crash. The
    #: default is also surfaced via ``default_factory`` here so direct
    #: callers (test fixtures, future helpers) can omit the field
    #: entirely — the factory mirrors the same constants used by the
    #: parser-side fallback at :func:`_from_hub_body`.
    retrieval_tuning: RetrievalTuning = field(
        default_factory=_default_retrieval_tuning,
    )
    #: Resolver protocol version reported by the hub (v0.2.22 Item #2).
    #: Pinned at 1 since v0.2.21 — see :data:`RESOLVER_PROTOCOL_VERSION`.
    #: Pre-v0.2.22 hubs omit the field; in that case the client back-
    #: fills with `RESOLVER_PROTOCOL_VERSION` so callers see a stable
    #: type. When the hub reports a value HIGHER than the client knows
    #: about, the client emits a one-line stderr warning and still
    #: returns the parsed body (additive fields default client-side).
    schema_version: int = RESOLVER_PROTOCOL_VERSION
    #: Peer-project diagrams collection names (already-canonical
    #: Weaviate class names, e.g. ``("Foo_Diagrams",)``). v0.2.34 A7
    #: split from the KG list — pre-v0.2.34 the MCP piggybacked on
    #: ``kg_access_list`` which had the wrong granularity. Empty tuple
    #: when no peers granted diagram read. Pre-v0.2.34 hubs omit the
    #: field; the parser back-fills with ``()`` so old hubs paired
    #: with v0.2.34+ clients don't crash — the MCP env-fallback path
    #: covers the empty-tuple sentinel by reading
    #: ``VCT_DIAGRAMS_ACCESS_LIST``. Placed after the other defaulted
    #: fields to keep frozen-dataclass init signature backward-compat.
    diagrams_access_list: tuple[str, ...] = ()
    #: Per-project Weaviate diagrams collection name (Phase 1.5 —
    #: fix/a1-indexing-pipeline 2026-05-25). The hub derives this from
    #: the primary KG collection via the `_KnowledgeGraph` → `_Diagrams`
    #: suffix swap. Pre-fix hubs paired with post-fix clients omit the
    #: field; the parser back-fills with the empty string. The MCP
    #: server's ``_config_field("diagrams_collection", ...,
    #: empty_means_unset=True)`` treats empty as unset and falls
    #: through to the env-var path (``DIAGRAMS_COLLECTION``), which
    #: preserves backward compatibility while letting newer hubs serve
    #: the canonical name directly. Additive (declared last so callers
    #: that construct ``ProjectConfig`` positionally don't break).
    diagrams_collection: str = ""
    #: v0.2.40 R2 — RL Reranker per-project flags. Until v0.2.40 the
    #: three GUI checkboxes (``set_rl_use_global`` /
    #: ``set_rl_online_training_disabled`` /
    #: ``set_rl_global_training_source_flag`` in
    #: ``launcher/src-tauri/src/commands/rl_settings.rs``) wrote into
    #: ``module_settings`` but had no readback path; the RL container
    #: never saw them. v0.2.40+ hubs expose them through the resolver
    #: so the container can fetch its project's flag state on every
    #: config refresh.
    #:
    #: Semantics (mirror the Rust ``ProjectConfigResponse``):
    #:
    #: * ``rl_use_global`` — read-only global mode (events DO NOT
    #:   update the local model when true).
    #: * ``rl_online_training_disabled`` — freezes the local model
    #:   AND marks new events as log-only.
    #: * ``rl_global_training_source_flag`` — opts this project's
    #:   data into the global model's retraining corpus.
    #:
    #: Pre-v0.2.40 hubs paired with v0.2.40+ clients omit the fields;
    #: the parser back-fills with ``False`` so old hubs don't crash
    #: new clients. Default ``False`` also matches the GUI's pre-
    #: unchecked checkbox state and the setter helper's
    #: ``unwrap_or(false)`` contract. Declared last to keep the
    #: frozen-dataclass init signature backward-compat.
    rl_use_global: bool = False
    rl_online_training_disabled: bool = False
    rl_global_training_source_flag: bool = False
    #: v0.2.46 Decision B — per-project READ gate for the shared KG.
    #: Symmetric mirror of ``shared_kg_write_disabled``. When ``True``,
    #: the MCP's ``_kg_collections_to_search`` drops
    #: ``SHARED_KG_COLLECTION`` from the hybrid_search /
    #: semantic_graph_search fan-out so this project stops searching
    #: the shared corpus. Read was unconditional pre-v0.2.46 (asymmetric
    #: access model); v0.2.46 lets users opt OUT explicitly while
    #: keeping the default ON.
    #:
    #: Pre-v0.2.46 hubs paired with v0.2.46+ clients omit the field;
    #: the parser back-fills with ``False`` so old hubs don't crash new
    #: clients (the env-fallback path in the MCP then resolves from
    #: ``SHARED_KG_READ_DISABLED`` directly). Default ``False`` also
    #: matches the GUI's pre-unchecked checkbox state. Declared last to
    #: keep the frozen-dataclass init signature backward-compat.
    shared_kg_read_disabled: bool = False
    #: v0.2.47 — read-only paths that contribute to this project's
    #: codegraph (sibling clones, vendored references, etc.). Hooks
    #: consult this list to decide whether an edit under an out-of-
    #: project path should re-trigger analyze for THIS project. Each
    #: entry mirrors the Rust ``CodeGraphExtraPath`` shape; the analyzer
    #: walks every enabled extra in the same pass as the primary repo
    #: and stamps ``project_source`` on the resulting rows.
    #:
    #: Pre-v0.2.47 hubs paired with v0.2.47+ clients omit this field;
    #: the parser back-fills with ``()`` so old hubs don't crash new
    #: clients — extras simply aren't visible to the hook chain until
    #: the hub binary is updated. Empty tuple is also the natural
    #: default for projects that haven't configured any extras yet.
    code_graph_extra_paths: tuple[ExtraCodegraphPath, ...] = ()
    #: v0.2.49 Stream B — per-project enable toggle for the RL Reranker
    #: (a global-scope module). Source: hub reads
    #: ``module_settings(project_id, "vct-rl-reranker", "enabled_for_project")``
    #: and exposes the resolved bool here. Default ``True`` when no row
    #: exists (fail-open: a corrupted setting never silently disables
    #: a module the user expects to work).
    #:
    #: Consumer: ``claude_mcp_servers/weaviate_mcp/server.py::
    #: _rl_cache_and_rerank`` reads this and short-circuits the rerank
    #: request when ``False`` — search returns base cosine order
    #: instead. The server-side telemetry path
    #: (``/data/logs/rl_events_<slug>.jsonl``) is untouched: that file
    #: is written by the RL container itself, not by the MCP, so
    #: disabling the client gate cannot drop training events.
    #:
    #: Pre-v0.2.49 hubs paired with v0.2.49+ clients omit the field;
    #: the parser back-fills with ``True`` so old hubs don't crash new
    #: clients. Declared last to keep frozen-dataclass init signature
    #: backward-compat.
    rl_reranker_enabled_for_project: bool = True
    #: V52-AA (v0.2.52) — per-project RL Reranker container port. Source:
    #: hub reads ``module_ports(project_id, "vct-rl-reranker", port)``
    #: (canonical SoT since migration 017 / v0.2.26) and exposes the
    #: allocated port here. ``None`` when no row exists (RL not installed
    #: for this project, OR allocator hasn't run yet).
    #:
    #: Closes the V52-AA env-propagation gap. Pre-V52-AA the only channel
    #: for the MCP subprocess to learn the container port was the
    #: ``RL_SERVER_PORT`` env var — which the launcher never wrote to
    #: ``.claude/settings.json env`` or ``.claude/env`` (deliberate: see
    #: the H.1 design contract in
    #: ``launcher/src-tauri/src/mcp_registration.rs``). Now the MCP's
    #: ``_get_rl_client`` consults this field when env is unset and
    #: builds the client with the resolved port.
    #:
    #: Pre-V52-AA hubs paired with V52-AA+ clients omit the field; the
    #: parser back-fills with ``None`` so old hubs don't crash new
    #: clients. New hubs paired with old clients have the field
    #: silently ignored.
    rl_server_port: Optional[int] = None


# ─── Internal: hub discovery ────────────────────────────────────────────


@dataclass(slots=True)
class _Discovery:
    port: int
    token: str
    expires_at: float


_discovery_lock = threading.Lock()
_discovery_cache: _Discovery | None = None

#: Rate-limit guard for corrupt-input discovery warnings: emit at most one
#: stderr line per (process, kind) so a hot caller doing many resolves in a
#: process with a persistently corrupt hub.port doesn't spam stderr. Reset
#: by ``_test_clear_cache`` for deterministic tests.
_discovery_warned_lock = threading.Lock()
_discovery_warned_kinds: set[str] = set()


def _warn_discovery(kind: str, detail: str) -> None:
    """Emit ONE best-effort stderr warning for a corrupt discovery input.

    F-8 corrupt-input contract — MUST MATCH the bash sibling
    ``vct_project_config.sh`` (``_emit_warning "hub_port_invalid" | …``)
    and the ps1 sibling ``vct_project_config.ps1`` (``Emit-Warning``):
    a corrupt/unreadable ``hub.port`` (or a non-integer ``VCT_HUB_PORT``)
    must NOT raise — it warns and the caller falls through to the default
    port. Idempotent per ``kind`` within one process (mirrors
    :func:`_maybe_warn_schema_version`'s per-value dedup). Stderr write is
    best-effort — never raises.
    """
    with _discovery_warned_lock:
        if kind in _discovery_warned_kinds:
            return
        _discovery_warned_kinds.add(kind)
    try:
        sys.stderr.write(f"[vco_lib.project_config] {kind}: {detail}\n")
        sys.stderr.flush()
    except OSError:
        # stderr unavailable (closed in a daemon context, etc.) — the
        # diagnostic is best-effort; swallow.
        pass


def _discover_hub() -> tuple[int, str]:
    """Resolve ``(port, token)`` for the local launcher hub.

    Resolution order:
      1. ``$VCT_HUB_PORT`` / ``$VCT_HUB_TOKEN`` env vars (tests / dev).
      2. ``<vct_root_dir>/hub.port`` + ``hub.token`` (written by the
         launcher on startup; mode 0o600 on Unix).
      3. Port falls back to :data:`DEFAULT_HUB_PORT`. The token has no
         default — missing file raises :class:`HubUnreachable`.

    The ``$VCT_HUB_TOKEN`` pin wins here unconditionally — that is the
    contract tests and dev harnesses rely on. It is only ever set aside
    AFTER the hub PROVABLY refuses it (401/403), by the bounded one-shot
    retry in :func:`_get_with_401_retry`; set ``VCT_HUB_TOKEN_STRICT=1``
    (:data:`HUB_TOKEN_STRICT_ENV`) to disable even that, so a harness
    pinning a deliberately-wrong token still observes the refusal.

    Results are cached in-process for :data:`HUB_DISCOVERY_TTL_SECONDS`.
    """
    global _discovery_cache
    now = time.monotonic()
    with _discovery_lock:
        cached = _discovery_cache
        if cached is not None and cached.expires_at > now:
            return cached.port, cached.token

        # Port: env > file > default.
        #
        # F-8 corrupt-input contract — MUST MATCH the bash sibling
        # `vct_project_config.sh::hub_port` and the ps1 sibling
        # `vct_project_config.ps1::Get-HubPort`: a non-integer
        # `VCT_HUB_PORT`, a non-integer `hub.port` file, or an unreadable
        # `hub.port` (perm-denied) must NOT raise — the port has a sane
        # default (7700), so we emit ONE stderr warning and fall through to
        # it. Only a truly ABSENT file (FileNotFoundError) is the silent
        # default path (that is the normal env-only / dev case). This makes
        # all three resolvers behave identically on corrupt port input:
        # warn + default, never crash, never a garbage/partial resolution.
        port_env = os.environ.get("VCT_HUB_PORT", "").strip()
        if port_env:
            try:
                port = int(port_env)
            except ValueError:
                _warn_discovery(
                    "hub_port_invalid",
                    "VCT_HUB_PORT is not a positive integer; "
                    "using default 7700",
                )
                port = DEFAULT_HUB_PORT
        else:
            port_file = vct_root_dir() / "hub.port"
            try:
                raw = port_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                port = DEFAULT_HUB_PORT
            except OSError:
                _warn_discovery(
                    "hub_port_unreadable",
                    "hub.port is not readable; using default 7700",
                )
                port = DEFAULT_HUB_PORT
            else:
                try:
                    port = int(raw) if raw else DEFAULT_HUB_PORT
                except ValueError:
                    _warn_discovery(
                        "hub_port_invalid",
                        "hub.port contains non-integer content; "
                        "using default 7700",
                    )
                    port = DEFAULT_HUB_PORT

        # Token: env > file > fail.
        #
        # F-8 corrupt-input contract — MUST MATCH the bash sibling
        # `vct_project_config.sh::hub_token` and the ps1 sibling
        # `vct_project_config.ps1::Get-HubToken`: the token has NO sane
        # default, so an absent/empty/unreadable `hub.token` is not
        # "corrupt content to warn-and-default" but "no token" → the hub is
        # genuinely unreachable. We raise :class:`HubUnreachable` (the
        # caller's env-fallback path), and — for the UNREADABLE case only —
        # route through `_warn_discovery` first so the stderr shape matches
        # the sh/ps1 `hub_token_unreadable` warning before the raise. The
        # read failure NEVER crashes with a raw OSError traceback.
        token_env = os.environ.get("VCT_HUB_TOKEN", "").strip()
        if token_env:
            token = token_env
        else:
            token_file = vct_root_dir() / "hub.token"
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise HubUnreachable(
                    f"hub.token missing at {token_file}; is the launcher "
                    f"running?{_POST_UPDATE_HINT}"
                ) from exc
            except OSError as exc:
                _warn_discovery(
                    "hub_token_unreadable",
                    "hub.token is not readable; treating as no token",
                )
                raise HubUnreachable(
                    f"cannot read hub.token at {token_file}: {exc}"
                ) from exc
            if not token:
                raise HubUnreachable(
                    f"hub.token at {token_file} is empty"
                )

        _discovery_cache = _Discovery(
            port=port,
            token=token,
            expires_at=now + HUB_DISCOVERY_TTL_SECONDS,
        )
        return port, token


def _test_clear_cache() -> None:
    """Reset the hub-discovery cache + schema-warning dedup set.

    For test use only — tests that exercise the schema_version warning
    path need a fresh dedup state to confirm the warning DOES fire when
    expected. Resetting the discovery cache and the warning set in the
    same helper keeps test setup symmetric.
    """
    global _discovery_cache, _stale_env_warned
    with _discovery_lock:
        _discovery_cache = None
    with _schema_warned_lock:
        _schema_warned_versions.clear()
    with _discovery_warned_lock:
        _discovery_warned_kinds.clear()
    with _stale_env_warned_lock:
        _stale_env_warned = False


def _invalidate_discovery_cache() -> None:
    """Drop the cached (port, token) so the next ``_discover_hub`` call
    re-reads ``hub.port`` + ``hub.token`` from disk.

    v0.2.21 mid-session-25-review (Reviewer A MEDIUM finding): the hub
    rotates its auth token on every restart (`auth.rs::generate_token`
    runs in `server.rs::start_hub_server`). After a Step 12 update
    choreography hub-stop → hub-start sequence, the in-process 5-second
    discovery cache holds the OLD token for up to 5 seconds. Calls
    during that window 401, get mapped to ``HubUnreachable``, and
    callers degrade to env vars even though the hub is up and the new
    token is sitting on disk.

    The retry-wrapper below catches a 401, invalidates the cache, and
    re-discovers + re-issues the request once. Subsequent 401s map to
    HubUnreachable unchanged.
    """
    global _discovery_cache
    with _discovery_lock:
        _discovery_cache = None


# ─── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ───────────────
#
# THE decision function lives here (Python SSOT). The three mirrors —
# `templates/scripts/vct_secrets_resolve.sh` / `.ps1`,
# `templates/scripts/vct_project_config.sh` / `.ps1`,
# `claude_mcp_servers/wrappers/_base.py`,
# `launcher/tools/vct-cli/src/main.rs`, `tools/vct-secrets/vct` — MUST
# MATCH the four rules below verbatim (A>B>C: a per-call subprocess into
# Python is not reachable from a bash `curl` retry path, so these stay
# thin mirrors locked by the F-8 parity tests).

#: Env var that DISABLES the stale-env-token fallback. A test/harness
#: that pins a deliberately-wrong ``VCT_HUB_TOKEN`` and asserts the 401
#: path sets this to ``1`` so the fallback never de-hermeticizes it.
#: Documented next to the env pin in :func:`_discover_hub` (and in
#: `launcher/tools/vct-cli/src/main.rs::resolve_token`).
HUB_TOKEN_STRICT_ENV: str = "VCT_HUB_TOKEN_STRICT"

#: The ONE definitive line printed (once per process) after a stale env
#: token has been overridden by the on-disk token. Byte-identical in
#: every mirror — the parity test asserts this exact sentence appears in
#: each of them.
STALE_ENV_TOKEN_MESSAGE: str = (
    "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — "
    "run `unset VCT_HUB_TOKEN` or open a new shell"
)

_stale_env_warned_lock = threading.Lock()
_stale_env_warned = False


def _hub_token_strict() -> bool:
    """True when the hermeticity guard ``VCT_HUB_TOKEN_STRICT=1`` is set.

    MUST MATCH the mirrors: the value is compared to the literal ``1``
    (no truthy-string parsing) so the guard is unambiguous in bash,
    PowerShell, and Rust alike.
    """
    return os.environ.get(HUB_TOKEN_STRICT_ENV, "").strip() == "1"


def _on_disk_hub_token(project_id: str | None = None) -> str:
    """Read the on-disk bearer token, IGNORING ``$VCT_HUB_TOKEN``.

    Preserves each call-site's scoped-vs-global semantics: when
    ``project_id`` is given (a per-project ``/env`` / ``/config`` route)
    the scoped ``hub.token.<project_id>`` wins, exactly like
    :func:`_project_token`; otherwise the global ``hub.token`` is used.
    Returns ``""`` when nothing readable exists (never raises).
    """
    root = vct_root_dir()
    if project_id:
        scoped = root / f"hub.token.{project_id}"
        try:
            raw = scoped.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            raw = ""
        if raw:
            return raw
    try:
        return (root / "hub.token").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def _stale_env_token_fallback(project_id: str | None = None) -> str | None:
    """Decide whether a PROVABLY-refused request may be retried once
    with the on-disk token, and return that token when it may.

    The four rules (mirrors must match):

    1. ``VCT_HUB_TOKEN_STRICT=1`` → never (``None``). The pin is
       authoritative; the caller keeps today's 401/403 path.
    2. ``VCT_HUB_TOKEN`` unset/empty → never. Nothing was pinned, so the
       first attempt already used the on-disk token.
    3. No readable on-disk token → never (nothing better to try).
    4. On-disk token EQUALS the env token → never (the env pin is not
       stale; the refusal has another cause).

    Otherwise return the on-disk token — the canonical always-fresh SSOT
    (regenerated on every hub start, mode 0o600). Callers use it for ONE
    bounded retry; nothing loops, respawns, or restarts.

    SCOPE DECISION (v0.2.91, coordinator ruling — decided, not missed):
    the original WP-D item 4 sketch ALSO proposed upgrading the resolvers'
    hedged failure messages ("a pre-update session *may* hold a stale
    VCT_HUB_TOKEN") to a definitive second sentence. That half is
    deliberately NOT shipped. With this fallback in place the field case
    SUCCEEDS and prints :data:`STALE_ENV_TOKEN_MESSAGE`, so the hedge is
    only ever reached where a definitive claim would be FALSE or useless:
    (a) ``VCT_HUB_TOKEN_STRICT=1`` — a harness pinned the token on
    purpose; (b) the on-disk token was refused too — staleness is then not
    the cause; (c) no readable on-disk token — staleness is unprovable.
    A second parity-locked sentence across eight mirrors and ~15 message
    sites would add complexity serving no reachable user.

    KNOWN IMPRECISION, ACCEPTED (v0.2.91 wave-3 NIT — documented, not
    missed). On a PER-PROJECT route (``/env``, ``/config``) there is one
    corner where the word "stale" in :data:`STALE_ENV_TOKEN_MESSAGE` is
    not literally true: the env pin holds a CURRENT global ``hub.token``
    while the route requires the scoped ``hub.token.<id>``. The hub 403s
    the global token, rule 4 sees env != scoped-on-disk, the retry with
    the scoped token succeeds, and the user is told their pin is "stale"
    when it was merely scope-mismatched.

    Not fixed, deliberately: (a) the REMEDY the sentence gives — unset
    ``VCT_HUB_TOKEN`` so the resolver picks the right token per route — is
    exactly right in that corner too, so the user is not misdirected; and
    (b) a corner-specific sentence is a SECOND parity-locked message
    across all 14 mirrors, which is the same cost the B-half decision
    above already declined for a larger benefit. One sentence, byte-locked
    everywhere, beats two that can drift.
    """
    if _hub_token_strict():
        return None
    env_tok = os.environ.get("VCT_HUB_TOKEN", "").strip()
    if not env_tok:
        return None
    disk_tok = _on_disk_hub_token(project_id)
    if not disk_tok or disk_tok == env_tok:
        return None
    return disk_tok


def _retry_answer_is_definitive(status_code: int) -> bool:
    """May a stale-env RETRY's response be adopted (and warned about)?

    Only when it PROVES the fallback credential was accepted:

    * ``2xx`` — the hub served the request;
    * ``404`` — ``project_not_found`` / ``field_not_found``, which the hub
      answers only AFTER its auth middleware accepted the bearer, so it is
      a post-auth answer exactly like a 200 (and the caller maps it to a
      precise ``ProjectNotFound`` / ``FieldNotFound``, far better than the
      401 it would otherwise see).

    Everything else proves nothing about the credential and keeps the
    ORIGINAL refusal. That deliberately includes ``400`` and ``503``,
    which the hub also only emits post-auth: adopting them would trade a
    truthful "401 unauthorized" for a marginally more specific message on
    a path that requires BOTH a stale pin AND a hub error, while widening
    the set of statuses that can latch the env pin off. The conservative
    branch is byte-identical to pre-v0.2.91 there.

    v0.2.91 wave-3 (MINOR-1): before this, ANY non-401/403 retry answer
    was adopted — so a 5xx after a 401 latched the pin off, printed the
    definitive line, and surfaced ``hub returned 503`` in place of the
    truthful 401.
    """
    return 200 <= status_code < 300 or status_code == 404


def _warn_stale_env_token() -> None:
    """Emit :data:`STALE_ENV_TOKEN_MESSAGE` once per process (best-effort).

    Once per process, not once per call: a hook that resolves many keys
    with a stale export must not flood stderr, but the user must see the
    fix at least once. Reset by :func:`_test_clear_cache`.
    """
    global _stale_env_warned
    with _stale_env_warned_lock:
        if _stale_env_warned:
            return
        _stale_env_warned = True
    try:
        sys.stderr.write(
            f"[vco_lib.project_config] {STALE_ENV_TOKEN_MESSAGE}\n"
        )
        sys.stderr.flush()
    except OSError:
        # stderr unavailable (closed in a daemon context) — the
        # diagnostic is best-effort; swallow.
        pass


def _project_token(project_id: str, global_token: str) -> str:
    """Resolve the bearer token to present for a PER-PROJECT route.

    v0.2.76 Part 4 — PER-PROJECT TOKEN PREFERENCE (MUST MATCH the bash
    sibling ``vct_project_config.sh::hub_token`` / ``vct_secrets_resolve.sh``
    and the ps1 ``Get-HubToken``): prefer the scoped
    ``<vct_root_dir>/hub.token.<project_id>`` when a readable file exists;
    otherwise fall back to ``global_token`` (the discovery-resolved
    ``hub.token``) for the one-release compat window.

    ``VCT_HUB_TOKEN`` (env) already won inside :func:`_discover_hub`, so
    ``global_token`` carries it when set — a pinned env token therefore
    still overrides any per-project file, matching the sh/ps1 rule where
    the env branch returns first. We only look at the per-project file
    when no env override is in play (i.e. when ``VCT_HUB_TOKEN`` is unset
    — checked here so a test/dev harness that pins the env keeps a single
    token across every route, exactly like the shell resolvers).

    Best-effort: an unreadable / absent per-project file falls through to
    ``global_token``; a read error never raises here (the caller's 401
    path handles a wrong token).
    """
    if os.environ.get("VCT_HUB_TOKEN", "").strip():
        # Env pin wins over the per-project file (parity with sh/ps1).
        return global_token
    proj_file = vct_root_dir() / f"hub.token.{project_id}"
    try:
        raw = proj_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return global_token
    return raw or global_token


def _get_with_401_retry(
    url_builder: Callable[[int, str], str],
    *,
    params: dict | None = None,
    project_id: str | None = None,
) -> requests.Response:
    """GET a hub URL with one-shot 401 retry-with-cache-invalidation.

    ``url_builder`` is a closure that takes ``(port, token)`` and returns
    the full URL. The closure pattern is needed because the URL itself
    embeds the port (e.g. ``http://127.0.0.1:7700/api/v1/...``); a token
    rotation that also moved the port would invalidate the cached URL.
    The closure is invoked twice on retry — once with the (possibly-
    stale) cached discovery, once with the freshly re-read discovery.

    v0.2.76 Part 4 — when ``project_id`` is set (the caller is hitting a
    per-project route: ``/config`` or ``/env``), the Authorization header
    carries the PER-PROJECT token (``hub.token.<id>``) in preference to
    the global ``hub.token`` — see :func:`_project_token`. The
    ``by-path`` lookup passes ``project_id=None`` (it is not a
    per-project route) so it keeps using the global token. The
    ``url_builder`` still receives the discovery token as its second arg
    (no current builder embeds it in the URL — only the port matters); we
    override only the header.

    Returns the underlying ``requests.Response`` — the caller handles
    status-code dispatch. Wraps ``requests.RequestException`` into
    ``HubUnreachable`` as the existing call sites do.

    Subsequent 401s (after the retry) are returned as-is so the caller
    can map them to ``HubUnreachable`` with the appropriate error
    message context.

    v0.2.91 (WP-D item 4) — STALE-ENV-TOKEN FALLBACK. The re-discovery
    above used to re-prefer the SAME stale ``$VCT_HUB_TOKEN`` (env wins
    inside :func:`_discover_hub`), so the retry was a no-op in exactly
    the scenario the field hits: a pre-update shell exported the token,
    the hub rotated it on restart, every resolve 401s until the shell is
    replaced. Now, when :func:`_stale_env_token_fallback` proves the env
    pin differs from the on-disk token, the retry presents the ON-DISK
    token instead and — on success — prints
    :data:`STALE_ENV_TOKEN_MESSAGE` once. Request count is unchanged
    (still at most two attempts on a 401). A 403 (the global-token-on-
    ``/env`` refusal shape) gets ONE such attempt too, but only when a
    fallback candidate exists.

    The retry's answer is ADOPTED only when it proves the fallback
    credential was accepted — see :func:`_retry_answer_is_definitive`.
    Anything else (another refusal, a 5xx, a transport failure, any other
    status) returns the ORIGINAL refusal response, so the caller's error
    text is byte-identical to pre-v0.2.91 on every one of those paths.
    ``VCT_HUB_TOKEN_STRICT=1`` disables all of it.
    """
    port, token = _discover_hub()
    url = url_builder(port, token)
    bearer = _project_token(project_id, token) if project_id else token
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        resp = _http_session().get(
            url,
            params=params,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException as exc:
        raise HubUnreachable(f"hub GET failed: {exc}") from exc

    if resp.status_code == 403:
        # Never retried before v0.2.91 (a 403 is an authorization
        # decision, not a rotation artefact) — and it still is not,
        # UNLESS the env pin is provably stale, in which case the
        # scoped/global on-disk token is the correct credential to
        # present. One bounded attempt; failure returns the original.
        fallback = _stale_env_token_fallback(project_id)
        if fallback is None:
            return resp
        try:
            resp_fb = _http_session().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {fallback}"},
                timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException:
            # The fallback attempt could not complete — keep today's
            # error path exactly (the original 403 response).
            return resp
        if not _retry_answer_is_definitive(resp_fb.status_code):
            return resp
        _warn_stale_env_token()
        return resp_fb

    if resp.status_code != 401:
        return resp

    # 401 → likely a stale token from the in-process 5s discovery cache
    # after a hub restart. Invalidate + re-discover + retry once. If
    # the second attempt also 401s, the caller maps it to HubUnreachable.
    # The per-project token is re-resolved too (its file may have rotated
    # on the same restart, or vanished → we fall back to the freshly-read
    # global token, the compat path).
    resp_401 = resp
    _invalidate_discovery_cache()
    port, token = _discover_hub()
    url = url_builder(port, token)
    bearer = _project_token(project_id, token) if project_id else token
    # v0.2.91: when the env pin is provably stale, the ONE retry we were
    # already making presents the on-disk token instead of re-presenting
    # the value the hub just refused.
    fallback = _stale_env_token_fallback(project_id)
    if fallback is not None:
        bearer = fallback
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        resp = _http_session().get(
            url,
            params=params,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException as exc:
        raise HubUnreachable(f"hub GET failed on retry: {exc}") from exc
    if fallback is None:
        # No credential swap happened — this is the pre-v0.2.91 cache
        # invalidation retry, whose response is returned as it always was.
        return resp
    if not _retry_answer_is_definitive(resp.status_code):
        # The swap did not prove itself. Return the ORIGINAL 401 rather
        # than an answer produced by a credential we cannot vouch for, and
        # do NOT print the definitive line.
        return resp_401
    _warn_stale_env_token()
    return resp


# ─── Internal: HTTP session ─────────────────────────────────────────────


_session_lock = threading.Lock()
_session: requests.Session | None = None

# Warn-once gate for the schema-version-too-new diagnostic. Threaded
# callers (hooks, MCP servers) should see exactly ONE warning per
# process when paired with a newer hub, not one per resolve() call.
# Reset by `_test_clear_cache` for deterministic tests.
_schema_warned_lock = threading.Lock()
_schema_warned_versions: set[int] = set()


def _maybe_warn_schema_version(hub_version: int) -> None:
    """Emit one-line stderr warning if hub schema_version is higher
    than what this client knows about (RESOLVER_PROTOCOL_VERSION).

    Idempotent per (process, hub_version) pair: we de-dup on the
    integer value so a hub bounce between two versions in the same
    process emits one warning per distinct version, not one per
    request. Stderr write is best-effort — never raises.
    """
    if hub_version <= RESOLVER_PROTOCOL_VERSION:
        return
    with _schema_warned_lock:
        if hub_version in _schema_warned_versions:
            return
        _schema_warned_versions.add(hub_version)
    try:
        sys.stderr.write(
            f"[vco_lib.project_config] hub reports schema_version="
            f"{hub_version} but this client only knows version "
            f"{RESOLVER_PROTOCOL_VERSION}; some fields may be unrecognised. "
            f"Update vco_lib (pip install --upgrade) to silence this warning.\n"
        )
        sys.stderr.flush()
    except OSError:
        # stderr unavailable (closed in a daemon context, etc.) —
        # silently swallow. The diagnostic is best-effort.
        pass


def _http_session() -> requests.Session:
    """Return the process-singleton :class:`requests.Session`.

    Constructed lazily on first call. One connection pooled per scheme;
    keepalive across calls keeps per-resolve latency tight. The hub
    URL is process-invariant (the launcher doesn't move ports mid-run),
    so a singleton is safe.
    """
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                sess = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=1, pool_maxsize=2
                )
                sess.mount("http://", adapter)
                _session = sess
    return _session


def _test_reset_session() -> None:
    """Drop the singleton session. For test use only."""
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = None


# ─── Internal: project-id resolution ────────────────────────────────────


def _looks_like_path(value: str) -> bool:
    """Heuristic: distinguish a project_id (UUID) from a folder path.

    Mirrors the bash client's ``looks_like_path`` helper. Cheap to be
    wrong here: a misclassified path will 404 the UUID lookup; a
    misclassified id (rare — UUIDs don't contain '/') will 404 the
    by-path lookup. Either way the caller gets :class:`ProjectNotFound`
    and degrades sensibly.
    """
    if value.startswith(("/", "./", "../")):
        return True
    if "/" in value or "\\" in value:
        return True
    # Windows drive prefix: "C:..." etc.
    if len(value) >= 2 and value[1] == ":":
        return True
    return False


def _resolve_project_id(project_arg: str) -> str:
    """Resolve the project's id for the resolver argument.

    If ``project_arg`` looks like a UUID/id, return it verbatim. If it
    looks like a path, GET ``/api/v1/projects/by-path?path=<arg>`` and
    return the resulting ``id``. Discovery (port + token) is performed
    inside :func:`_get_with_401_retry` so a hub restart between calls
    doesn't strand a stale token in the in-process cache.
    """
    if not _looks_like_path(project_arg):
        return project_arg

    resp = _get_with_401_retry(
        lambda port, _token: f"http://127.0.0.1:{port}/api/v1/projects/by-path",
        params={"path": project_arg},
    )

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            raise HubUnreachable(
                f"by-path returned 200 but body is not JSON: {resp.text!r}"
            ) from exc
        pid = data.get("id") if isinstance(data, dict) else None
        if not pid or not isinstance(pid, str):
            raise HubUnreachable(
                f"by-path returned 200 but no .id field: {resp.text!r}"
            )
        return pid
    if resp.status_code == 401:
        raise HubUnreachable(
            "hub returned 401 unauthorized; launcher may have restarted "
            f"(token rotated).{_POST_UPDATE_HINT}"
        )
    if resp.status_code == 403:
        # by-path is NOT a per-project-token route (the flip only gates
        # /env + /config), so a 403 here is anomalous. Still raise the
        # typed Forbidden rather than mislabeling it HubUnreachable — the
        # latter would trigger a spurious env-fallback and hide the cause.
        raise Forbidden(
            f"hub returned 403 forbidden on by-path lookup for "
            f"{project_arg!r}: {resp.text!r}"
        )
    if resp.status_code == 404:
        raise ProjectNotFound(f"no project registered at path: {project_arg}")
    if resp.status_code == 400:
        raise ProjectNotFound(f"hub rejected by-path query: {resp.text}")
    raise HubUnreachable(
        f"hub returned status {resp.status_code} for by-path lookup; body={resp.text!r}"
    )


# ─── Internal: response → dataclass ─────────────────────────────────────


def _coerce_optional_port(raw: Any) -> Optional[int]:
    """Coerce a JSON value into an Optional[int] port, defensively.

    V52-AA (v0.2.52). The hub's ``rl_server_port`` field serialises
    ``Option<u16>`` — ``null`` for absent, a number for allocated. We
    accept the JSON-typed integer directly and also coerce a numeric
    string (defense-in-depth; the hub doesn't emit strings here today
    but a future schema change shouldn't crash old clients). Any other
    shape (negative, zero, oversize, malformed) is treated as
    ``None`` — caller falls through to env-resolution / disabled mode.
    """
    if raw is None:
        return None
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def _parse_extra_codegraph_paths(
    raw: Any,
) -> tuple[ExtraCodegraphPath, ...]:
    """Decode the hub's ``code_graph_extra_paths`` array into a tuple.

    v0.2.47 (extras). Defensive: a non-list value or per-row decode
    failure becomes an empty tuple / dropped row rather than an
    exception. The resolver's outer ``KeyError, TypeError, ValueError``
    catch in :func:`_from_hub_body` would otherwise translate one bad
    row into a wholesale ``HubUnreachable`` — which is the wrong
    response for "the hub returned 17 valid extras and 1 garbage one".

    Each row mirrors the Rust ``CodeGraphExtraPath`` struct:
    ``{"path": str, "enabled": bool, "last_indexed_commit": str | null}``.
    """
    if not isinstance(raw, list):
        return ()
    out: list[ExtraCodegraphPath] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        path = row.get("path")
        if not isinstance(path, str) or not path:
            # Path is the only truly required column. Skip the row.
            continue
        # `enabled` defaults to True when absent so a pre-v0.2.47 hub
        # that ships only `{path}` rows is treated as "all enabled".
        enabled_raw = row.get("enabled", True)
        enabled = bool(enabled_raw) if not isinstance(enabled_raw, bool) else enabled_raw
        commit_raw = row.get("last_indexed_commit")
        commit: Optional[str]
        if commit_raw is None:
            commit = None
        elif isinstance(commit_raw, str) and commit_raw:
            commit = commit_raw
        else:
            commit = None
        out.append(ExtraCodegraphPath(
            path=path,
            enabled=enabled,
            last_indexed_commit=commit,
        ))
    return tuple(out)


def _from_hub_body(body: dict[str, Any]) -> ProjectConfig:
    """Convert the hub's full-config JSON envelope into ProjectConfig.

    Raises :class:`HubUnreachable` if the envelope is missing required
    fields (a defensive check against a future hub version that drops
    a field — we'd rather fail loudly than silently degrade).
    """
    try:
        em = body["embedding_models"]
        # schema_version (v0.2.22 Item #2). Optional — pre-v0.2.22
        # hubs don't emit it, in which case we back-fill with
        # RESOLVER_PROTOCOL_VERSION (the value this client knows
        # about) so callers see a stable int. Non-numeric values are
        # treated as malformed and bubble up via the outer except
        # below. A NEWER version than the client supports triggers
        # the warn-once stderr line — we still parse the body
        # (additive fields default client-side; the warning is the
        # only user-visible signal that something is off).
        schema_version_raw = body.get("schema_version", RESOLVER_PROTOCOL_VERSION)
        schema_version = int(schema_version_raw)
        _maybe_warn_schema_version(schema_version)
        # retrieval_tuning is OPTIONAL — pre-v0.2.22 hubs don't emit
        # it, in which case we synthesize the calibrated defaults so
        # old hubs paired with new clients don't crash. Per-field
        # defaults are sourced from `_RETRIEVAL_TUNING_DEFAULTS` (single
        # source of truth — see module-level constant above; mirrors the
        # Rust constants in vct-hub/src/retrieval_tuning_io.rs).
        rt_raw = body.get("retrieval_tuning") if isinstance(body, dict) else None
        if isinstance(rt_raw, dict):
            d = _RETRIEVAL_TUNING_DEFAULTS
            retrieval_tuning = RetrievalTuning(
                code_graph_score_floor=float(rt_raw.get(
                    "code_graph_score_floor", d[0],
                )),
                kg_tier_min=float(rt_raw.get("kg_tier_min", d[1])),
                kg_tier_single_chunk=float(rt_raw.get(
                    "kg_tier_single_chunk", d[2],
                )),
                kg_tier_three_chunks=float(rt_raw.get(
                    "kg_tier_three_chunks", d[3],
                )),
                kg_tier_full=float(rt_raw.get("kg_tier_full", d[4])),
            )
        else:
            retrieval_tuning = _default_retrieval_tuning()
        return ProjectConfig(
            project_id=str(body["project_id"]),
            project_path=str(body["project_path"]),
            project_slug=str(body["project_slug"]),
            project_display_name=str(body["project_display_name"]),
            code_graph_project=str(body["code_graph_project"]),
            code_graph_collection_prefix=str(
                body["code_graph_collection_prefix"]
            ),
            kg_collection=str(body["kg_collection"]),
            shared_kg_collection=str(body.get("shared_kg_collection", "")),
            development_collection=str(body.get("development_collection", "")),
            # fix/a1-indexing-pipeline (2026-05-25) — additive field.
            # Pre-fix hubs omit it; empty-string sentinel signals "compute
            # locally / fall through to env var" to MCP consumers.
            diagrams_collection=str(body.get("diagrams_collection", "")),
            active_embedding=str(body["active_embedding"]),
            embedding_models=EmbeddingModels(
                text=str(em["text"]),
                code=str(em["code"]),
            ),
            kg_access_list=tuple(str(s) for s in body["kg_access_list"]),
            codegraph_access_list=tuple(
                str(s) for s in body["codegraph_access_list"]
            ),
            # v0.2.34 A7 additive field — pre-v0.2.34 hubs omit it.
            # Empty-tuple sentinel signals "no hub-resolved peers";
            # the MCP env-fallback path reads VCT_DIAGRAMS_ACCESS_LIST
            # in that case.
            diagrams_access_list=tuple(
                str(s) for s in body.get("diagrams_access_list", [])
            ),
            weaviate_url=str(body["weaviate_url"]),
            ollama_url=str(body["ollama_url"]),
            grpc_port=int(body["grpc_port"]),
            shared_kg_write_disabled=bool(body["shared_kg_write_disabled"]),
            # v0.2.31 additive field — pre-v0.2.31 hubs omit it. Empty
            # string sentinel keeps the dataclass shape stable and
            # signals "compute locally" to MCP consumers.
            claude_session_dir=str(body.get("claude_session_dir", "")),
            retrieval_tuning=retrieval_tuning,
            schema_version=schema_version,
            # v0.2.40 R2 additive RL Reranker flags — pre-v0.2.40 hubs
            # omit these; default `False` for back-compat and matches
            # the Rust handler's `unwrap_or(false)` contract on absent
            # rows. Each `.get(..., False)` is independent — old hubs
            # paired with new clients see all three as False; new hubs
            # paired with old clients have the fields silently ignored.
            rl_use_global=bool(body.get("rl_use_global", False)),
            rl_online_training_disabled=bool(
                body.get("rl_online_training_disabled", False)
            ),
            rl_global_training_source_flag=bool(
                body.get("rl_global_training_source_flag", False)
            ),
            # v0.2.46 Decision B additive field — pre-v0.2.46 hubs omit
            # it; default ``False`` keeps the symmetric default-off
            # behaviour and matches the Rust handler's
            # ``unwrap_or(false)`` contract on absent rows.
            shared_kg_read_disabled=bool(
                body.get("shared_kg_read_disabled", False)
            ),
            # v0.2.47 additive field — pre-v0.2.47 hubs omit it; empty
            # tuple is the natural default. Each entry is one
            # ExtraCodegraphPath; malformed entries are silently
            # dropped (defense-in-depth so a single bad row doesn't
            # wedge the whole resolve()). The hub already filters
            # ``enabled`` to surface user intent — clients see the
            # full list and decide per-call whether to filter further.
            code_graph_extra_paths=_parse_extra_codegraph_paths(
                body.get("code_graph_extra_paths", []),
            ),
            # v0.2.49 Stream B additive field — pre-v0.2.49 hubs omit it;
            # default ``True`` matches the hub-side fail-open default
            # (`Db::module_is_enabled_for_project` returns true on absent
            # row) and the launcher GUI's "no opinion = enabled" UX. Old
            # hubs paired with new clients see the default; new hubs
            # paired with old clients have the field silently ignored.
            rl_reranker_enabled_for_project=bool(
                body.get("rl_reranker_enabled_for_project", True)
            ),
            # V52-AA (v0.2.52) additive field — pre-V52-AA hubs omit it;
            # default ``None`` matches the Rust handler's
            # ``get_project_rl_port`` returning ``Option<u16>`` (None when
            # no row exists). Coerce to int when the JSON value is a
            # finite number; treat anything else (None, missing key,
            # malformed string) as ``None`` so the MCP falls through to
            # env-resolution / disabled mode.
            rl_server_port=_coerce_optional_port(body.get("rl_server_port")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HubUnreachable(
            f"hub response missing/malformed field: {exc}; body={body!r}"
        ) from exc


# ─── Public: resolve() ──────────────────────────────────────────────────


def resolve(project_root: Path | str) -> ProjectConfig:
    """Resolve the full per-project config via the launcher hub.

    :param project_root: The project's folder path (will be normalised
        via :func:`Path.resolve`) **or** the project's UUID id. The
        client auto-detects which based on a cheap path-shape heuristic.

    :raises HubUnreachable: hub not running, conn refused, 401, network
        error, malformed response.
    :raises ProjectNotFound: the project isn't registered with the
        launcher.
    :raises ServiceMisconfigured: the project is registered but its
        primary KG binding is missing (user fixes in launcher GUI).
    :raises ResolverError: any other resolver-side failure.

    Notes on env-fallback: this function deliberately does NOT fall
    back to env vars on failure. Callers decide whether a partial-
    config-from-env is acceptable for their context; the resolver
    raises so the caller's try/except can choose. See the parent plan
    ``.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md``
    §"Resolver clients" for the migration discipline.

    **v0.2.46 KG-AUTO-HEAL-E test-stability gate**: when the env var
    ``VCT_DISABLE_HUB_RESOLVER`` is set to a truthy value, this function
    raises :class:`HubUnreachable` immediately — without making any HTTP
    probe. Mirrors the gate at ``weaviate_mcp/server.py::
    _try_resolve_project_config`` (added in v0.2.47 RL-6c) so tests
    that monkey-patch ``KG_COLLECTION`` / ``SHARED_KG_COLLECTION`` env
    vars produce the SAME result regardless of whether a launcher hub
    is running on the dev machine. The MCP-side gate alone wasn't
    enough — the CLI-side callers (templates/scripts/get_node_info.py
    etc.) also need the same escape hatch. Without it, the hub returns
    the project's REAL bindings + env overrides are silently ignored.

    Production runs leave the env var unset → hub-first resolution
    semantics preserved exactly.
    """
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        raise HubUnreachable(
            "VCT_DISABLE_HUB_RESOLVER set; resolver short-circuited "
            "(test gate or explicit opt-out)"
        )

    if isinstance(project_root, str):
        arg = project_root
    else:
        arg = str(project_root)

    # Normalise paths (only paths — leave UUID-shaped strings alone).
    if _looks_like_path(arg):
        try:
            arg = str(Path(arg).resolve())
        except OSError:
            # Path doesn't exist on disk — let the hub answer with 404.
            pass

    pid = _resolve_project_id(arg)

    resp = _get_with_401_retry(
        lambda port, _token: f"http://127.0.0.1:{port}/api/v1/projects/{pid}/config",
        # Per-project route → prefer the scoped hub.token.<id> (Part 4).
        project_id=pid,
    )

    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError as exc:
            raise HubUnreachable(
                f"config returned 200 but body is not JSON: {resp.text!r}"
            ) from exc
        if not isinstance(body, dict):
            raise HubUnreachable(
                f"config returned 200 but body is not an object: {body!r}"
            )
        return _from_hub_body(body)

    # Pull out the error.code envelope (may be absent on 5xx infra errors).
    err_code: str | None = None
    err_msg: str = resp.text
    try:
        err_body = resp.json()
        if isinstance(err_body, dict):
            err_obj = err_body.get("error")
            if isinstance(err_obj, dict):
                err_code = err_obj.get("code")
                err_msg = str(err_obj.get("message", err_msg))
    except ValueError:
        pass

    if resp.status_code == 401:
        raise HubUnreachable(
            f"hub returned 401 unauthorized: {err_msg}"
        )
    if resp.status_code == 403:
        # Scoped-credential boundary refusal (v0.2.77 flip). HARD refusal,
        # NOT transient — Forbidden is not a HubUnreachable subclass, so a
        # caller catching HubUnreachable for env-fallback will not mask it.
        raise Forbidden(
            f"hub returned 403 forbidden for project {pid}: {err_msg}. "
            f"Present the scoped hub.token.{pid}, or set "
            f"VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub to reopen the "
            f"one-release compat window."
        )
    if resp.status_code == 404:
        if err_code == "field_not_found":
            raise FieldNotFound(err_msg)
        raise ProjectNotFound(f"project {pid} not registered: {err_msg}")
    if resp.status_code == 503:
        raise ServiceMisconfigured(err_msg)
    if resp.status_code == 400:
        raise ResolverError(f"hub rejected request: {err_msg}")
    if 500 <= resp.status_code < 600:
        raise HubUnreachable(
            f"hub returned {resp.status_code}: {err_msg}"
        )
    raise HubUnreachable(
        f"hub returned unexpected status {resp.status_code}: {err_msg}"
    )


# ─── Public: resolve_field() — single-field fast path ───────────────────


def resolve_field(
    project_root: Path | str, key: str
) -> str | int | bool | list[Any] | dict[str, Any]:
    """Resolve a single config field via the hub's ``?key=`` filter.

    Saves wire bytes vs the full :func:`resolve` round-trip for hot-
    path callers that only need one field (e.g.
    ``code-graph-query --project ...`` needs ``code_graph_project``).

    :raises HubUnreachable: hub not running / unreachable.
    :raises ProjectNotFound: project not registered.
    :raises ServiceMisconfigured: primary KG binding missing.
    :raises FieldNotFound: ``key`` is not a known config field.
    :raises ResolverError: malformed request (e.g. empty ``key``).
    """
    if not key or not key.strip():
        raise ResolverError("resolve_field: key must be a non-empty string")
    key = key.strip()

    if isinstance(project_root, str):
        arg = project_root
    else:
        arg = str(project_root)
    if _looks_like_path(arg):
        try:
            arg = str(Path(arg).resolve())
        except OSError:
            pass

    pid = _resolve_project_id(arg)

    resp = _get_with_401_retry(
        lambda port, _token: f"http://127.0.0.1:{port}/api/v1/projects/{pid}/config",
        params={"key": key},
        # Per-project route → prefer the scoped hub.token.<id> (Part 4).
        project_id=pid,
    )

    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError as exc:
            raise HubUnreachable(
                f"single-field response is not JSON: {resp.text!r}"
            ) from exc
        if not isinstance(body, dict) or key not in body:
            raise HubUnreachable(
                f"single-field response missing {key!r}: {body!r}"
            )
        return body[key]

    err_code: str | None = None
    err_msg: str = resp.text
    try:
        err_body = resp.json()
        if isinstance(err_body, dict):
            err_obj = err_body.get("error")
            if isinstance(err_obj, dict):
                err_code = err_obj.get("code")
                err_msg = str(err_obj.get("message", err_msg))
    except ValueError:
        pass

    if resp.status_code == 401:
        raise HubUnreachable(f"hub returned 401 unauthorized: {err_msg}")
    if resp.status_code == 403:
        # Scoped-credential boundary refusal (v0.2.77 flip). See resolve()
        # for the full rationale — Forbidden is intentionally NOT a
        # HubUnreachable subclass so env-fallback callers don't mask it.
        raise Forbidden(
            f"hub returned 403 forbidden for project {pid}: {err_msg}. "
            f"Present the scoped hub.token.{pid}, or set "
            f"VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub to reopen the "
            f"one-release compat window."
        )
    if resp.status_code == 404:
        if err_code == "field_not_found":
            raise FieldNotFound(err_msg)
        raise ProjectNotFound(f"project {pid} not registered: {err_msg}")
    if resp.status_code == 503:
        raise ServiceMisconfigured(err_msg)
    if resp.status_code == 400:
        raise ResolverError(f"hub rejected request: {err_msg}")
    if 500 <= resp.status_code < 600:
        raise HubUnreachable(f"hub returned {resp.status_code}: {err_msg}")
    raise HubUnreachable(
        f"hub returned unexpected status {resp.status_code}: {err_msg}"
    )


# ─── Public: claude_session_dir_for() — slug rule (v0.2.31) ─────────────


def claude_session_dir_for(workspace_path: Path) -> Path:
    """Compute Claude Code's session-jsonl directory for a workspace.

    Claude Code stores per-workspace session transcripts under
    ``~/.claude/projects/<slug>/``, where ``<slug>`` is derived from the
    workspace's absolute path by replacing certain characters with ``-``.

    Verified rule (against ``~/.claude/projects/`` listings on Linux as
    of 2026-05-23, against Claude Code 2.1.143):

    * ``/`` → ``-``  (path separator)
    * ``_`` → ``-``  (e.g. ``VCO_dev`` → ``VCO-dev``)
    * ``.`` → ``-``  (e.g. ``.claude/worktrees`` → ``-claude-worktrees``)

    This function is THE source of truth for the slug rule on the
    Python side: MCPs and other consumers should call this helper (or
    route through ``vct-hub`` which calls it internally) rather than
    re-implementing the rule inline. Inline copies have drifted in the
    past — most recently the RL citation-monitor at
    ``claude_mcp_servers/weaviate_mcp/server.py`` only handled ``/`` →
    ``-``, causing zero-citation telemetry on workspaces whose absolute
    paths contained underscores. This
    helper exists so the rule lives in exactly one place.

    The spec requires AT MINIMUM ``/`` + ``_`` → ``-`` to fix the
    observed v0.2.31 bug. We also include ``.`` because empirical
    inspection of ``~/.claude/projects/`` shows Claude Code applies
    that substitution too (worktree paths under ``.claude/`` would
    otherwise look at ``-home-…-.claude-…`` which doesn't exist on
    disk — Claude writes them under ``…--claude-…``).

    Open questions for future verification (worth confirming when
    Anthropic ever documents this contract):

    * Space (`` `` → ``-``?) — almost certainly yes; user workspaces
      with spaces would otherwise be broken.
    * Unicode — likely passed through verbatim or NFKC-normalised,
      not currently exercised by any test fixture.

    :param workspace_path: Absolute path to the workspace root. Not
        canonicalised here — callers that want symlink resolution
        should pass ``workspace_path.resolve()``.
    :return: ``~/.claude/projects/<slug>/`` as a :class:`Path`. The
        returned path may not exist on disk (e.g. fresh workspace
        that hasn't been opened in Claude Code yet); callers that
        care must check ``.exists()`` themselves.
    """
    slug = str(workspace_path).replace("/", "-").replace("_", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / slug


__all__ = [
    "DEFAULT_HUB_PORT",
    "EmbeddingModels",
    "ExtraCodegraphPath",
    "FieldNotFound",
    "Forbidden",
    "HUB_DISCOVERY_TTL_SECONDS",
    "HubUnreachable",
    "ProjectConfig",
    "ProjectNotFound",
    "RESOLVER_PROTOCOL_VERSION",
    "ResolverError",
    "RetrievalTuning",
    "ServiceMisconfigured",
    "Unauthorized",
    "claude_session_dir_for",
    "resolve",
    "resolve_field",
]
