# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Standalone HTTP server exposing RetrievalRL for training from external clients.

Primary consumer: the weaviate-kg MCP server, which calls this server to:
  1. Score the candidates of each KG search with the project's model (from
     the vectors the client sends) and receive the reranked order + scores —
     or, while the project's model does not pass its own held-out check, the
     client's own order back (``serving: "search_order"``).
  2. Submit each answer's citation event (frozen vectors + answer cosines +
     literal citations) for the project's online learning.

Per-project online learning (v0.2.106, owner ruling 2026-11-12; opt-in on the
client side): ``online_learning.ProjectLearner`` — the offline objective in
bounded rounds, a held-out window of the project's newest queries with a
purge gap, and a gate that serves the model only while it beats plain cosine
(NDCG@10 >= +2% AND cited-node MRR@10 not below cosine's).

Endpoints:
  POST /cache_nodes      — candidates + serve-time features → the project's reranked order
                           (or its search order while its model is not served)
  POST /rl_update        — citation events → the project's online learning
  GET  /project_status   — the routed project's online learning + serving gate
  GET  /health           — liveness check
  POST /rotate_weights   — hot-swap loaded RLModel to a new .pt without restart       (v0.1.1)
  POST /reset            — fork (or fork + specialize) project model from global       (v0.1.1),
                           or re-seed it from the bundled checkpoint (strategy "bundled")
  POST /finetune         — async offline training job; returns job_id                  (v0.1.1)
  GET  /finetune_status  — poll job_id state (running / done / failed)                 (v0.1.1)
  POST /global/retrain   — async global retrain (online/offline) across N projects     (v0.1.1)

Job-status wire format (`/finetune_status`):
  Launcher (see vibecoded-orchestrator launcher/src-tauri/src/commands/rl_service.rs
  `run_finetune_then_rotate`) treats ``state == "done"`` as success and
  ``state in {"failed", "error"}`` as failure. We emit ``"done"`` as the
  canonical terminal-success state for that reason. ``"running"`` during
  execution, ``"failed"`` with a ``reason`` field on error.

Usage (from ``claude_mcp_servers/`` in the orchestrator checkout; the
container runs the same module as ``python -m rl_reranker.rl_server``):
  python -m rl_reranker.rl_server                    # default port 11438
  python -m rl_reranker.rl_server --port 11439

Env / CLI:
  --port INT           TCP port (default 11438)
  --project-root PATH  project root containing state/ (default: cwd)
  --log-path PATH      JSONL event log path (``RL_LOG_PATH``; default:
                       ``rl_client.rl_logger.default_rl_log_path()``)
  --reranker-model STR Ollama model for cross-encoder reranking (optional)
  --weights-dir PATH   bundled-weights root (``RL_WEIGHTS_DIR``; ``/app/weights`` in
                       the image). Layout + manifest: ``bundled_weights.py``. With
                       no checkpoint shipped for the active profile the server
                       runs fresh weights and reports ``trained: false``.

Extra v0.1.1 env vars:
  RL_GLOBAL_WEIGHTS_DIR  Directory holding the shared global weights .pt files.
                        Default: /data/global. Used by /reset and /global/retrain.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

try:
    from aiohttp import web
except ImportError:
    print("aiohttp required: pip install aiohttp", file=sys.stderr)
    sys.exit(1)

from collections import OrderedDict

from vco_lib.atomic import atomic_copy_file
from vco_lib.log_setup import configure_logging
from vco_lib.rl_weights_ownership import classify as classify_weights_owner

from .bundled_weights import BundledWeights, resolve_bundled_weights
from .online_learning import DEFAULT_CONFIG, OnlineConfig, ProjectLearner, compute_round
from .retrieval_rl import RetrievalRL
from rl_client.rl_logger import RLDataLogger, default_rl_log_path
from .rl_model import N_ENTITY_TYPES, RLModel, linked_slots_masked
from rl_client.rerank_wire import RERANK_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 11438

# v0.2.10 — per-project routing constants.
#
# The container is installed once per machine (install.scope=global in the
# manifest) and serves all projects from one process. Clients identify which
# project a request belongs to via the ``X-VCT-Project-ID`` HTTP header. The
# container loads a base model at startup; per-project fine-tuned heads are
# loaded lazily on first request and cached in an LRU keyed by
# (project_id, embedding_source). When the header is missing OR no head
# exists on disk for the requested project, requests fall back to the base
# model with an audit-log entry — backward compatibility with pre-routing
# clients (e.g. v0.2.9 launchers) is preserved.
_HEADER_PROJECT_ID = "X-VCT-Project-ID"
_BASE_PROJECT_SENTINEL = "_base"
_DEFAULT_PROJECT_HEAD_LRU_SIZE = 10


# Mapping from ACTIVE_EMBEDDING (the env var read by weaviate_mcp/server.py)
# to the (model_tag, embedding_dim) pair that drives RLModel selection.
#
# Each embedding source gets its own RLModel weights file (rl_model_<tag>.pt)
# to avoid catastrophic interference: a model trained on qwen3 1024-dim
# vectors does not generalise to openai 1536-dim vectors, and even same-dim
# vectors from different models occupy different latent spaces. Cross-source
# data must be re-embedded (see scripts/reembed_rl_data.py) before training
# the target source's network.
#
# Legacy default tag "1024" is kept ONLY for backward compatibility with the
# pre-2026-05-05 single-network setup (an existing rl_model_1024.pt that
# was trained on a mix of arctic + qwen3 embeddings — preserved per the
# never-delete-RL-data rule, but not selected by the new derivation path).
# Override RL_MODEL_TAG / RL_EMB_DIM in env to force a specific tag.
# NOTE (2026-05-19): codesage REMOVED from the active table. The KG RL
# reranker handles TEXT embeddings only (qwen3 / arctic / openai).
# Code-graph reranking will live in a separate future paid module
# (vct-code-reranker) with its own global model + per-project forks —
# see ../.claude/context/plans/codegraph-reranker-future.md.
# Adding a new text embedder later: extend this table. Don't add code
# embedders here.
#
# v0.2.0 (2026-05-22): the v2 transformer-tail architecture refused loading
# of pre-v2 checkpoints via RLModel._load_pt (arch_version field mismatch).
# v0.2.7 (2026-05-24) added embedding_source + weights_version checkpoint
# metadata, making the in-pickle dict authoritative for source / dim / arch.
# v0.2.8 (2026-05-31): filename pattern aligned with launcher R3 convention.
#
#   Before v0.2.8: filename = rl_model_{source}_{dim}_{arch}.pt
#     e.g. rl_model_arctic_1024_v2.pt
#     Container parsed dim+arch out of the filename.
#   v0.2.8+: filename = rl_model_{source}_{weights_version}.pt
#     e.g. rl_model_arctic_0.2.7.pt (launcher writes this exact shape to
#     <vct_root>/data/<module>/<project_slug>/state/ per R3).
#     Dim + arch live INSIDE the .pt pickle (R4 cross-source guard already
#     reads them from there). Filename is a label; truth is in the dict.
#
# Tag values map source → dim only. arch is read from the checkpoint dict
# on load, or defaults to "v2" for fresh init.
_EMBEDDING_SOURCE_TO_DIM: dict[str, int] = {
    "qwen3":    1024,
    "arctic":   1024,
    "openai":   1536,
    # Legacy alias: pre-2026-04-14 default. Distinct tag prevents co-mingling
    # of old vs new training data.
    "legacy":   1024,
}

# Sentinel used in the filename when no weights_version is known (e.g. a
# fresh-init local fork that has not been rotated to any released global
# checkpoint yet). The container writes rl_model_<source>_local.pt for these.
_LOCAL_FORK_VERSION_SENTINEL = "local"


def _build_weights_filename(source: str, weights_version: str = "") -> str:
    """Return the v0.2.8 weights filename for a given (source, version) pair.

    Format: ``rl_model_<source>_<version>.pt`` matching the launcher's R3
    download convention. When ``weights_version`` is empty (fresh-init local
    fork pre-release-sync), uses the ``"local"`` sentinel.
    """
    version = weights_version.strip() or _LOCAL_FORK_VERSION_SENTINEL
    return f"rl_model_{source}_{version}.pt"


def _model_base_version(model: Any) -> str:
    """The release a model's training started from — the same rule
    ``choose_local_checkpoint`` judges a checkpoint by
    (``base_weights_version``, else ``weights_version``; "" = none)."""
    return str(getattr(model, "base_weights_version", "") or getattr(model, "weights_version", "")
               or "")


def _glob_weights_files(state_dir: "Path", source: str) -> "list[Path]":
    """Return all ``.pt`` files in ``state_dir`` matching the given source.

    Used by container startup to find the most-recent checkpoint for the
    active embedding source. Filters by filename prefix only; the actual
    cross-source guard runs inside ``RLModel._try_load``.
    """
    # Both the v0.2.8 shape (rl_model_<source>_<version>.pt) and legacy
    # shapes (rl_model_<source>_<dim>_<arch>.pt) match the same prefix.
    # _try_load is the safety: it refuses any .pt whose embedding_source
    # field disagrees with the container's pinned source.
    return sorted(state_dir.glob(f"rl_model_{source}_*.pt"))


def _global_retrain_seed(state_dir: "Path", project_id: str, source: str) -> "Path | None":
    """The checkpoint a first-ever ``/global/retrain`` seeds the global model
    from, or ``None`` (then the global model starts from a fresh init).

    The requesting project's own checkpoints (``projects/<project_id>/``)
    come first, the shared base dir second; within a dir the NEWEST file
    (mtime, then name) of the writers' naming (``_glob_weights_files``) wins.
    A ``*_finetuned.pt`` sibling is not a live model (it waits for an explicit
    ``/rotate_weights``) and is never a seed.
    """
    dirs = [state_dir]
    if project_id and project_id != _BASE_PROJECT_SENTINEL:
        dirs.insert(0, _per_project_state_dir(state_dir, project_id))
    for d in dirs:
        cands = [p for p in _glob_weights_files(d, source)
                 if p.is_file() and not p.stem.endswith("_finetuned")]
        if cands:
            return max(cands, key=lambda p: (p.stat().st_mtime_ns, p.name))
    return None


def _semver_key(version: str) -> "tuple[int, int, int] | None":
    """``(major, minor, patch)`` of a release version, ``None`` otherwise."""
    parts = (version or "").strip().split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2].split("-")[0])
    except ValueError:
        return None


def choose_local_checkpoint(
    candidates: "list[Path]",
    bundled_version: str,
    *,
    exclude: "tuple[Path, ...]" = (),
    read_meta: Any = None,
) -> "Path | None":
    """The local checkpoint to serve INSTEAD of the bundled one, if any.

    Rule (v0.2.106): the bundled ``current`` wins over a local checkpoint that
    has no training history (a fresh initialisation, e.g. the
    ``rl_model_<source>_local.pt`` a server writes when nothing shipped) or
    whose base is an OLDER release. A local checkpoint is kept only when

    * its base release is NEWER than the bundled one (a user rotated to a
      later release), or
    * its base IS the bundled release and it carries its own training on top
      (``local_train_steps > 0``).

    The base is ``base_weights_version`` (falling back to ``weights_version``
    for checkpoints older than that field). Unreadable checkpoints and
    non-release bases ("", "local") never win. Among several keepers the
    newest base, then the most steps, then the latest mtime wins. ``exclude``
    names files not to consider (the seeded copy of the bundled checkpoint
    itself). Returns ``None`` when the bundled checkpoint should be served.

    Ownership comes from the one shared table (``vco_lib/rl_weights_ownership``,
    also read by the launcher's "Download default weights"): a MANAGED file
    (the launcher's symlink or marked copy of a downloaded release) is judged
    by its release version only — training recorded on it does not count, as
    the launcher replaces such a file on its next download; local training
    counts for a USER-owned file only.
    """
    from .rl_model import read_checkpoint_meta

    reader = read_meta or read_checkpoint_meta
    bundled_key = _semver_key(bundled_version)
    if bundled_key is None:
        return None
    excluded = {Path(p).resolve() for p in exclude}
    best: "tuple[tuple, Path] | None" = None
    for cand in candidates:
        if Path(cand).resolve() in excluded:
            continue
        meta = reader(cand)
        if not meta:
            continue
        base_key = _semver_key(meta.get("base_weights_version") or meta.get("weights_version") or "")
        if base_key is None:
            continue
        steps = int(meta.get("local_train_steps") or 0)
        if classify_weights_owner(Path(cand)) == "managed":
            steps = 0
        if base_key < bundled_key or (base_key == bundled_key and steps <= 0):
            continue
        try:
            mtime = Path(cand).stat().st_mtime
        except OSError:
            mtime = 0.0
        rank = (base_key, steps, mtime)
        if best is None or rank > best[0]:
            best = (rank, Path(cand))
    return best[1] if best else None


def _resolve_model_tag_and_dim() -> tuple[str, int]:
    """
    Derive (model_tag, emb_dim) from environment.

    The model_tag is the SOURCE STRING (e.g. ``"arctic"``) only. v0.2.8
    refactor: dim + arch_version no longer live in the tag — they're read
    from the .pt's pickled dict on load.

    Resolution order (first wins):
      1. Both RL_MODEL_TAG + RL_EMB_DIM set explicitly → use them as-is.
         (Used by retraining flows where re-embedded data has a known target.)
      2. ACTIVE_EMBEDDING set to a known source → look up in
         _EMBEDDING_SOURCE_TO_DIM table.
      3. ACTIVE_EMBEDDING unset → fall back to ``legacy`` 1024 default
         (matches pre-2026-05-05 behaviour for unmigrated callers).
      4. ACTIVE_EMBEDDING set to an unknown source → fail loudly. The
         caller passed something we cannot map to a network, and silently
         falling back would corrupt training.
    """
    explicit_tag = os.environ.get("RL_MODEL_TAG")
    explicit_dim = os.environ.get("RL_EMB_DIM")
    if explicit_tag and explicit_dim:
        return explicit_tag, int(explicit_dim)

    active = os.environ.get("ACTIVE_EMBEDDING", "").strip()
    if not active:
        return "legacy", _EMBEDDING_SOURCE_TO_DIM["legacy"]

    if active in _EMBEDDING_SOURCE_TO_DIM:
        return active, _EMBEDDING_SOURCE_TO_DIM[active]

    # Dynamic-embedder fallback (v0.2.0). Users can plug in any Ollama embedder
    # (nomic-embed-text, bge-m3, mxbai-embed-large, …) and get a per-source RL
    # network without code changes — the v2 architecture's enc1 accepts any
    # emb_dim ≥ 2.
    #
    # Gated behind RL_ALLOW_UNKNOWN_EMBEDDER so typos in known sources (e.g.
    # `qwen` instead of `qwen3`) still fail loudly. When opting in, the caller
    # MUST also set RL_EMB_DIM so we know the network's input width without
    # making an extra HTTP probe to the embedding service.
    if os.environ.get("RL_ALLOW_UNKNOWN_EMBEDDER", "").strip().lower() in ("1", "true", "yes"):
        if not explicit_dim:
            raise ValueError(
                f"RL_ALLOW_UNKNOWN_EMBEDDER=1 requires RL_EMB_DIM to be set. "
                f"ACTIVE_EMBEDDING={active!r} is unknown and we cannot infer "
                f"its embedding dimension automatically."
            )
        tag = _sanitize_embedder_name(active)
        logger.info(
            "RLServer: dynamic-embedder fallback engaged. active=%r → tag=%r dim=%s. "
            "Initialising a fresh per-source RL network; collect online "
            "feedback before retraining via offline_trainer.",
            active, tag, explicit_dim,
        )
        return tag, int(explicit_dim)

    raise ValueError(
        f"Unknown ACTIVE_EMBEDDING={active!r}. Known sources: "
        f"{sorted(_EMBEDDING_SOURCE_TO_DIM)}. Set RL_MODEL_TAG + "
        f"RL_EMB_DIM explicitly to bypass this check, or set "
        f"RL_ALLOW_UNKNOWN_EMBEDDER=1 + RL_EMB_DIM to opt into the "
        f"dynamic-embedder fallback."
    )


_SANITIZE_RE = None  # lazy-compiled on first use


def _sanitize_embedder_name(name: str) -> str:
    """Normalize an Ollama embedder identifier to a filename-safe tag fragment.

    Examples:
      "nomic-embed-text:latest" → "nomic_embed_text"
      "BAAI/bge-m3"             → "baai_bge_m3"
      "mxbai-embed-large"       → "mxbai_embed_large"
    """
    import re
    global _SANITIZE_RE
    if _SANITIZE_RE is None:
        _SANITIZE_RE = re.compile(r"[^a-z0-9]+")
    cleaned = _SANITIZE_RE.sub("_", name.strip().lower()).strip("_")
    return cleaned or "unknown"


def _resolve_arch_version() -> str:
    """Return the RLModel architecture version to instantiate.

    Default is "v2" (the transformer-tail rewrite landed 2026-05-22).
    Set RL_ARCH_VERSION=v1 to roll back to the v0.1.x snapshot in
    rl_model_v1.py — testing/comparison only, not a supported production
    path. Any value other than "v1" / "v2" is a hard error so typos don't
    silently load the wrong network.
    """
    v = os.environ.get("RL_ARCH_VERSION", "v2").strip().lower()
    if v not in ("v1", "v2"):
        raise ValueError(
            f"Unknown RL_ARCH_VERSION={v!r}. Allowed: 'v1' (legacy snapshot) "
            f"or 'v2' (default, transformer-tail)."
        )
    return v


def _resolve_device() -> str:
    """Return the device string to pass to RLModel.

    Reads ``RL_DEVICE`` env (default ``"auto"``). Accepts ``auto`` / ``cpu`` /
    ``cuda`` / ``cuda:N``. Validation happens inside ``RLModel._resolve_device``;
    invalid values silently fall back to CPU with a warning rather than crash
    the container at startup.

    The launcher's container-variant dispatch (cpu/cuda/rocm Dockerfile
    variants — see ``manifest.runtime.gpu_image_variants``) selects WHICH
    torch wheel is in the container. ``RL_DEVICE=auto`` then picks GPU when
    the variant ships CUDA/ROCm AND ``--device <gpu>`` was passed to
    ``podman run``. CPU containers + ``auto`` cleanly select CPU.
    """
    return os.environ.get("RL_DEVICE", "auto").strip() or "auto"


# ---------------------------------------------------------------------------
# vct-hub client (v0.2.6, module-DB persistence)
# ---------------------------------------------------------------------------
#
# Thin HTTP client wrapping the launcher's vct-hub REST endpoints for module-
# owned tables. The container reads/writes its own rl_state / rl_weights_state
# rows through these calls instead of touching SQLite directly (containers in
# podman-rootless can't reach the launcher's SQLite file safely; hub is the
# authoritative + auth-bearing surface).
#
# Endpoints (launcher v0.2.31+):
#   GET    /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
#                                                                       [?fields=col,col]
#   POST   /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}
#   PATCH  /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
#   DELETE /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
#   GET    /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}
#   POST   /api/v1/modules/{module_id}/token/refresh
#
# Auth: bearer token via VCT_MODULE_TOKEN env (injected by launcher on
# `podman run -e`). Container refreshes the token when within 5 min of expiry.
#
# Failure mode: all hub calls soft-fail with a logged warning. The container's
# in-memory state is the truth; the launcher's dashboard is allowed to show
# stale "last refreshed Xs ago" data when the hub is unreachable. We never
# raise out of a hub call — the goal is "rerank requests keep flowing even
# if the launcher GUI is closed and the hub process exited."

_HUB_BASE_URL = os.environ.get("VCT_HUB_BASE_URL", "http://127.0.0.1:7700")
_HUB_MODULE_ID = "vct-rl-reranker"
_HUB_REFRESH_LEAD_SECONDS = 300  # refresh token when within 5 min of expiry
_HUB_REQUEST_TIMEOUT_SECONDS = 5.0


class _HubClient:
    """Async client for vct-hub's module-DB endpoints + token refresh.

    Instantiate once per process. Re-uses an aiohttp ClientSession when
    available; falls back to per-call sessions in tests where the event loop
    is short-lived.
    """

    def __init__(
        self,
        *,
        base_url: str = _HUB_BASE_URL,
        module_id: str = _HUB_MODULE_ID,
        token_env: str = "VCT_MODULE_TOKEN",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._module_id = module_id
        self._token_env = token_env
        self._token: str = os.environ.get(token_env, "")
        self._token_expires_at: float = 0.0  # 0 = unknown / never refreshed
        self._session: "aiohttp.ClientSession | None" = None

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None or self._session.closed:
            import aiohttp as _aiohttp
            self._session = _aiohttp.ClientSession(
                timeout=_aiohttp.ClientTimeout(total=_HUB_REQUEST_TIMEOUT_SECONDS),
            )
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _maybe_refresh_token(self) -> None:
        """Refresh the bearer token if it's within REFRESH_LEAD of expiry.

        Soft-fails: a failed refresh leaves the existing (possibly-expired)
        token in place; the next hub call will hit 401 and the caller will
        see the warning. This is intentional — we don't want token refresh
        to block hot-path rerank operations.
        """
        if self._token_expires_at == 0.0:
            return  # unknown expiry; assume launcher rotates on container restart
        now = time.time()
        if now < self._token_expires_at - _HUB_REFRESH_LEAD_SECONDS:
            return  # not yet within lead window
        try:
            session = await self._ensure_session()
            url = f"{self._base}/api/v1/modules/{self._module_id}/token/refresh"
            async with session.post(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    logger.warning(
                        "_HubClient: token refresh failed (status=%d); using stale token",
                        resp.status,
                    )
                    return
                body = await resp.json()
                new_token = body.get("token")
                new_expiry = body.get("expires_at")
                if isinstance(new_token, str) and new_token:
                    self._token = new_token
                if isinstance(new_expiry, (int, float)):
                    self._token_expires_at = float(new_expiry)
        except Exception as exc:
            logger.warning("_HubClient: token refresh raised %s; using stale token", exc)

    def _table_url(self, project_id: str, table: str, key: "str | None" = None) -> str:
        base = (
            f"{self._base}/api/v1/modules/{self._module_id}"
            f"/db/projects/{project_id}/rows/{table}"
        )
        return f"{base}/{key}" if key else base

    # ------------------------------------------------------------------
    # Public REST verbs — all soft-fail
    # ------------------------------------------------------------------

    async def get_row(
        self,
        project_id: str,
        table: str,
        key: str,
        *,
        fields: "list[str] | None" = None,
    ) -> "dict | None":
        """Fetch a single row. Returns None on 404 / network error."""
        await self._maybe_refresh_token()
        try:
            session = await self._ensure_session()
            url = self._table_url(project_id, table, key)
            params = {"fields": ",".join(fields)} if fields else None
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning(
                        "_HubClient.get_row(%s/%s): status=%d", table, key, resp.status,
                    )
                    return None
                return await resp.json()
        except Exception as exc:
            logger.debug("_HubClient.get_row(%s/%s) soft-failed: %s", table, key, exc)
            return None

    async def list_rows(self, project_id: str, table: str) -> "list[dict]":
        """List all rows in a table for a project. Returns [] on failure."""
        await self._maybe_refresh_token()
        try:
            session = await self._ensure_session()
            url = self._table_url(project_id, table)
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    logger.warning("_HubClient.list_rows(%s): status=%d", table, resp.status)
                    return []
                body = await resp.json()
                rows = body.get("rows") if isinstance(body, dict) else body
                return list(rows) if isinstance(rows, list) else []
        except Exception as exc:
            logger.debug("_HubClient.list_rows(%s) soft-failed: %s", table, exc)
            return []

    async def upsert_row(
        self,
        project_id: str,
        table: str,
        key: str,
        fields: "dict[str, Any]",
    ) -> bool:
        """Create or update a row. Returns True on 2xx, False otherwise.

        Implementation: POST {key, fields}; the hub treats this as upsert.
        The launcher's hub implementation may choose PUT or POST semantics;
        we follow the spec which mandates POST as the upsert verb.
        """
        await self._maybe_refresh_token()
        try:
            session = await self._ensure_session()
            url = self._table_url(project_id, table)
            payload = {"key": key, "fields": fields}
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                if 200 <= resp.status < 300:
                    return True
                logger.warning(
                    "_HubClient.upsert_row(%s/%s): status=%d", table, key, resp.status,
                )
                return False
        except Exception as exc:
            logger.debug("_HubClient.upsert_row(%s/%s) soft-failed: %s", table, key, exc)
            return False

    async def patch_row(
        self,
        project_id: str,
        table: str,
        key: str,
        fields: "dict[str, Any]",
    ) -> bool:
        """Partial update of an existing row. Returns True on 2xx."""
        await self._maybe_refresh_token()
        try:
            session = await self._ensure_session()
            url = self._table_url(project_id, table, key)
            async with session.patch(url, headers=self._headers(), json=fields) as resp:
                if 200 <= resp.status < 300:
                    return True
                logger.warning(
                    "_HubClient.patch_row(%s/%s): status=%d", table, key, resp.status,
                )
                return False
        except Exception as exc:
            logger.debug("_HubClient.patch_row(%s/%s) soft-failed: %s", table, key, exc)
            return False

    async def delete_row(self, project_id: str, table: str, key: str) -> bool:
        """Delete a row. Returns True on 2xx or 404 (already gone)."""
        await self._maybe_refresh_token()
        try:
            session = await self._ensure_session()
            url = self._table_url(project_id, table, key)
            async with session.delete(url, headers=self._headers()) as resp:
                if 200 <= resp.status < 300 or resp.status == 404:
                    return True
                logger.warning(
                    "_HubClient.delete_row(%s/%s): status=%d", table, key, resp.status,
                )
                return False
        except Exception as exc:
            logger.debug("_HubClient.delete_row(%s/%s) soft-failed: %s", table, key, exc)
            return False

    async def list_rl_events(
        self,
        project_id: str,
        *,
        event_type: "str | None" = None,
        since_ms: "int | None" = None,
        until_ms: "int | None" = None,
        page_limit: int = 10_000,
        max_events: "int | None" = None,
    ) -> "list[dict]":
        """List this module's RL telemetry events for one project.

        Reads the module-scoped route
        ``GET /api/v1/modules/{module_id}/projects/{project_id}/rl/events``
        (added hub-side under ``require_module_scope`` — the SAME module
        token this client already authenticates with for the /db routes).
        We deliberately do NOT use the hub-wide ``/api/v1/rl/events`` route:
        that is gated by ``hub.token``, which containers do not (and should
        not) possess — different trust boundary.

        Pagination: the hub caps a single response at 10,000 rows. We page
        forward by advancing ``since_ms`` to one past the last returned
        ``ts_ms`` until a short page (or ``max_events``) is reached. Rows are
        returned in ascending ``ts_ms`` (the hub's GET order); duplicate
        ``ts_ms`` at a page boundary are de-duplicated by event ``id``.

        Soft-fail: returns whatever was collected so far on any error (the
        offline trainer treats a short/empty corpus as "nothing to train on"
        rather than crashing). A 401/403 here means the module token is
        missing/expired or the project is out of scope — logged, not raised.

        Args:
            project_id: The project whose events to fetch. Pinned into the
                URL; the hub's scope gate 403s if the token's scope differs.
            event_type: Optional server-side filter (e.g. ``"retrieval"``).
                The offline trainer needs both ``retrieval`` and ``citation``,
                so it leaves this None and filters client-side.
            since_ms / until_ms: Inclusive time-window bounds (unix epoch ms).
            page_limit: Per-request row cap (hub clamps to 10,000).
            max_events: Stop after collecting this many rows (None = all).

        Returns:
            List of raw hub row dicts (each carries ``payload_json`` +
            denormalized columns), ascending by ``ts_ms``.
        """
        await self._maybe_refresh_token()
        collected: list[dict] = []
        seen_ids: set = set()
        cursor = since_ms
        # Hub clamps to 10k; never request more per page than that.
        per_page = max(1, min(int(page_limit), 10_000))
        try:
            session = await self._ensure_session()
            base = (
                f"{self._base}/api/v1/modules/{self._module_id}"
                f"/projects/{project_id}/rl/events"
            )
            while True:
                params: "dict[str, Any]" = {"limit": per_page}
                if event_type:
                    params["event_type"] = event_type
                if cursor is not None:
                    params["since_ms"] = cursor
                if until_ms is not None:
                    params["until_ms"] = until_ms
                async with session.get(
                    base, headers=self._headers(), params=params
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "_HubClient.list_rl_events(%s): status=%d (returning %d "
                            "collected so far)",
                            project_id, resp.status, len(collected),
                        )
                        return collected
                    body = await resp.json()
                rows = body.get("rows") if isinstance(body, dict) else body
                if not isinstance(rows, list) or not rows:
                    break
                fresh = 0
                max_ts = cursor if cursor is not None else None
                page_max_ts: "int | None" = None
                for row in rows:
                    rid = row.get("id")
                    if rid is not None and rid in seen_ids:
                        continue
                    if rid is not None:
                        seen_ids.add(rid)
                    collected.append(row)
                    fresh += 1
                    ts = row.get("ts_ms")
                    if isinstance(ts, int):
                        if max_ts is None or ts > max_ts:
                            max_ts = ts
                        if page_max_ts is None or ts > page_max_ts:
                            page_max_ts = ts
                    if max_events is not None and len(collected) >= max_events:
                        return collected[:max_events]
                # Termination: a page that yielded NO fresh rows means we've
                # drained the window (the server returned only rows we've
                # already seen, or nothing new past the cursor). We deliberately
                # do NOT compare len(rows) against the requested limit — the
                # server may cap a page BELOW what we asked (the real hub clamps
                # to 10k; a proxy could cap lower), and a short-but-nonempty page
                # does not imply the end. seen_ids makes re-fetching safe.
                if fresh == 0:
                    break
                # Advance the cursor past this page's newest ts. If the page's
                # rows all shared the cursor's ts (can't advance), bump by 1ms so
                # we move forward; seen_ids dedups any boundary overlap.
                if page_max_ts is None:
                    # No usable timestamps on this page — can't page by ts_ms.
                    # Avoid an infinite loop.
                    break
                if cursor is not None and page_max_ts <= cursor:
                    cursor = cursor + 1
                else:
                    cursor = page_max_ts + 1
            return collected
        except Exception as exc:
            logger.debug(
                "_HubClient.list_rl_events(%s) soft-failed: %s (returning %d collected)",
                project_id, exc, len(collected),
            )
            return collected

    async def close(self) -> None:
        """Close the underlying aiohttp session. Idempotent."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None


# ---------------------------------------------------------------------------
# Job tracking (v0.1.1)
# ---------------------------------------------------------------------------

# Roots permitted as path prefixes when validating request-supplied paths.
# Both /rotate_weights and /finetune accept paths only INSIDE these roots
# so a hostile request can't make the container open arbitrary files
# (e.g. /etc/passwd). The list is intentionally narrow — anything the
# legitimate caller (the host launcher) writes lives under /data/state,
# /data/logs, or /data/global inside the container bind mount.
_SAFE_PATH_ROOTS: tuple[str, ...] = ("/data/state/", "/data/logs/", "/data/global/")


@dataclass
class _ProjectHead:
    """Per-project model head + bookkeeping.

    Held in an LRU keyed by (project_id, embedding_source). One head per
    (project, source) pair; a project on a different embedding_source than
    the container's pinned base source is REJECTED (see
    ``_embedding_mismatch_response``) so we never mix vector spaces in
    one cache.

    ``rl_model`` is the loaded ``RLModel`` instance for this project.
    ``state_path`` is the path to the .pt currently loaded into rl_model
    (per-project base directory: ``/data/state/projects/<project_id>/``).
    ``data_logger`` writes to a per-project JSONL log under ``/data/logs/``.
    ``flags_cache`` holds the cached (timestamp, flags-dict) for the 3
    per-project boolean settings (rl_use_global, etc.).
    ``is_base_fallback`` is True when this head was created because no
    per-project weights existed on disk for the requested project_id and
    we fell back to a fresh copy of the base model. Used by audit logging
    so the operator can see how often per-project specialization is
    actually engaged vs falling back.
    """
    project_id: str
    embedding_source: str
    rl_model: "RLModel"
    state_path: Path
    data_logger: "RLDataLogger"
    flags_cache: "tuple[float, dict[str, bool]] | None" = None
    is_base_fallback: bool = False
    # Last finetune artifact path for this project (separate from
    # state_path because /finetune writes a *_finetuned.pt sibling that
    # must be explicitly /rotate_weights'd into). Empty until the first
    # successful finetune for this project.
    last_finetuned_path: "Path | None" = None
    # v0.2.106 — the project's online learning + serving gate
    # (``online_learning.ProjectLearner``). None for the ``_base`` sentinel
    # (no project identity = no per-project model) and for a head whose
    # model failed to load (it serves the shared base in-process, which must
    # never be trained by one project). A head without a learner always
    # answers with the client's search order.
    learner: "ProjectLearner | None" = None
    # The project's own state dir (``projects/<project_id>/``): where its
    # checkpoint and learner state are saved.
    project_dir: "Path | None" = None


def _per_project_state_dir(base_state_dir: Path, project_id: str) -> Path:
    """Return the per-project state subdirectory.

    Layout: ``<base_state_dir>/projects/<project_id>/``. The base directory
    (``/data/state/``) keeps the legacy single-project layout for the
    ``_base`` sentinel; per-project heads live under the ``projects/`` subdir
    so the two layouts don't collide and a future migration can move
    legacy users in-place.

    POSIX-only inside the container (host-side bind-mount handling is the
    launcher's responsibility, see Stream A).
    """
    if project_id == _BASE_PROJECT_SENTINEL:
        return base_state_dir
    return base_state_dir / "projects" / project_id


def _per_project_log_path(log_root: Path, project_id: str) -> Path:
    """Return the per-project JSONL events log path.

    Format: ``<log_root>/rl_events_<project_id>.jsonl`` matching the manifest's
    ``runtime.log_path_template``. For ``_base`` (no header), uses the legacy
    unscoped filename to stay compatible with pre-v0.2.10 single-project
    installs.
    """
    if project_id == _BASE_PROJECT_SENTINEL:
        return log_root / "rl_events.jsonl"
    return log_root / f"rl_events_{project_id}.jsonl"


@dataclass
class JobState:
    """In-process state for a background training job.

    Returned by ``GET /finetune_status``. The launcher polls this every
    5 seconds; the wire shape (state/percent/message/reason/...) matches
    what ``run_finetune_then_rotate`` in the launcher's rl_service.rs
    parses. ``state == "done"`` is the success terminal; ``state in
    {"failed", "error"}`` is the failure terminal.
    """

    job_id: str
    kind: str           # "finetune" | "specialize" | "global_online" | "global_offline"
    state: str          # "running" | "done" | "failed"
    percent: int = 0    # 0..100 — best-effort progress estimate
    message: str = ""
    started_at: str = ""
    result_weights_path: "str | None" = None
    error: "str | None" = None


def _is_path_under(candidate: Path, roots: tuple[str, ...]) -> bool:
    """Return True iff ``candidate`` lies under one of the allowed roots.

    Resolves symlinks and ``..`` so a request can't sneak out via
    ``/data/state/../../etc/passwd``. Roots are matched as literal
    string prefixes after resolution; trailing slash on each root keeps
    ``/data/state-x`` from passing the ``/data/state`` check.
    """
    try:
        resolved = str(candidate.resolve(strict=False))
    except Exception:
        return False
    # Path.resolve() strips the trailing slash; re-append for prefix match.
    if not resolved.endswith("/"):
        resolved_check = resolved + "/"
    else:
        resolved_check = resolved
    return any(resolved.startswith(r) or resolved_check.startswith(r) for r in roots)


def _safe_weights_path(p: str) -> "Path | None":
    """Validate a request-supplied weights path. Returns the Path on
    success or None when the path is empty / not under a safe root.

    We accept ``/data/state/`` and ``/data/global/`` because both
    /rotate_weights and /reset legitimately reference files in those
    locations.
    """
    if not p or not isinstance(p, str):
        return None
    candidate = Path(p)
    # Reject relative paths outright — caller MUST give an absolute
    # container-side path (the launcher already does this; see
    # container_weights_path() in rl_service.rs).
    if not candidate.is_absolute():
        return None
    allowed = ("/data/state/", "/data/global/")
    if not _is_path_under(candidate, allowed):
        return None
    return candidate


# NOTE (v0.2.11): `_safe_events_path` + `_filter_events_by_days` were removed
# when training went DB-only. Training no longer accepts a request-supplied
# JSONL path or filters a JSONL file by date — events come from launcher.db via
# the hub (filtered by project_id + since_ms in the query). The per-project
# JSONL LOG-WRITE path (`_per_project_log_path` + RLDataLogger) is unrelated and
# stays: that's telemetry capture, not training input.


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


class _seeded_init:
    """Context: seed torch's RNGs (CPU and CUDA) for a fresh network
    initialisation, restoring the previous RNG state afterwards. ``enabled``
    False = no-op (a checkpoint is being loaded; nothing random happens)."""

    def __init__(self, seed: int, *, enabled: bool) -> None:
        self._seed = int(seed)
        self._enabled = enabled
        self._fork: Any = None

    def __enter__(self) -> None:
        if not self._enabled:
            return
        import torch

        devices = list(range(torch.cuda.device_count())) if torch.cuda.is_available() else []
        self._fork = torch.random.fork_rng(devices=devices)
        self._fork.__enter__()
        torch.manual_seed(self._seed)

    def __exit__(self, *exc: Any) -> None:
        if self._fork is not None:
            self._fork.__exit__(*exc)
            self._fork = None


class RLServer:
    """Thin aiohttp wrapper around RetrievalRL for cross-process access."""

    def __init__(
        self,
        project_root: Path,
        log_path: str,
        reranker_model: str | None = None,
        port: int = 0,
        bind_host: str = "127.0.0.1",
        weights_root: "Path | None" = None,
    ) -> None:
        # v0.2.9 C8: ``reranker_model`` is ACCEPTED for back-compat but
        # IGNORED. Cross-encoder citation detection was deleted from the
        # container; Pro-tier cross-encoder wiring is deferred to v0.2.10+
        # on the MCP side. Logged at WARNING so operators notice when their
        # config has a stale --reranker-model that no longer does anything.
        if reranker_model:
            logger.warning(
                "RLServer: reranker_model=%r is ignored in v0.2.9+; the "
                "cross-encoder path was retired (see SELF-HANDOFF-rl-"
                "citation-unified-v3-2026-06-04.md). Remove --reranker-model "
                "from your launch config.",
                reranker_model,
            )
        self._project_root = project_root
        self._port = port
        # Bind address — defaults to loopback for safety. Containerized
        # deployments (Dockerfile) set RL_BIND_HOST=0.0.0.0 or pass
        # --bind 0.0.0.0 so the host port mapping (-p X:X) works.
        self._bind_host = bind_host

        # Resolve which RLModel network to use for THIS process. Each
        # embedding source has its own weights file (see _resolve_model_tag_and_dim).
        # arch_version selects between v0.2.0 (default) and the v0.1.x legacy
        # snapshot — see _resolve_arch_version + rl_model_v1.py.
        rl_model_tag, rl_emb_dim = _resolve_model_tag_and_dim()
        rl_arch_version = _resolve_arch_version()
        rl_device = _resolve_device()
        active_emb_source = os.environ.get("ACTIVE_EMBEDDING", "legacy") or "legacy"
        # Data logger — writes retrieval + citation events to the shared JSONL.
        # We pass embedding_source/embedding_dim so v2 events are tagged at
        # log time (see rl_logger schema_version=2). Legacy events without
        # these fields are converted by scripts/migrate_rl_log_v1_to_v2.py.
        data_logger = RLDataLogger(
            log_path=Path(log_path),
            project=project_root.name,
            embedding_source=active_emb_source,
            embedding_dim=rl_emb_dim,
        )

        # State path resolution. ProjectManifest from MAO is not available here;
        # use a simple `<project_root>/state/` convention. Caller can override
        # by passing a project_root with a different state layout.
        #
        # v0.2.8 filename pattern: rl_model_<source>_<weights_version>.pt
        # (the launcher's R3 download path writes exactly this shape). On
        # startup we glob for all matching files and pick the most recent
        # (sorted by filename — semver-ish ordering works because the
        # version segment is monotonic per-source). The _try_load cross-source
        # guard inside RLModel will refuse any .pt whose embedding_source
        # field disagrees with the container's pinned source.
        state_dir = project_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        # The checkpoint the image ships for this profile (None = no trained
        # model ships -> fresh weights, reported as not trained). A present but
        # inconsistent manifest raises here: a broken image must not start
        # looking like an untrained one.
        self._bundled_weights: "BundledWeights | None" = resolve_bundled_weights(
            weights_root, rl_model_tag,
        )
        _seed_bundled_weights(self._bundled_weights, state_dir)
        candidate_files = _glob_weights_files(state_dir, rl_model_tag)
        if self._bundled_weights is not None:
            # A trained model ships for this profile: serve it unless a local
            # checkpoint has earned its place (choose_local_checkpoint — an
            # untrained local fork or an older base never shadows it).
            seeded = state_dir / _build_weights_filename(
                rl_model_tag, self._bundled_weights.version,
            )
            state_path = choose_local_checkpoint(
                candidate_files, self._bundled_weights.version, exclude=(seeded,),
            ) or seeded
        elif candidate_files:
            # Nothing ships: pick the lexically-greatest filename (the
            # pre-v0.2.106 rule; "local" sorts after "0.x.y").
            state_path = candidate_files[-1]
        else:
            # No checkpoint on disk yet — use the local-fork sentinel.
            # RLModel.load_or_create will fresh-init the network and save
            # it under this name (treats as "local fork, no release sync").
            state_path = state_dir / _build_weights_filename(rl_model_tag, "")
        rl_model = RLModel.load_or_create(
            state_path=state_path,
            emb_dim=rl_emb_dim,
            arch_version=rl_arch_version,
            device=rl_device,
            embedding_source=active_emb_source,  # v0.2.7 — enables R4 cross-source check
        )
        logger.info(
            "RLServer: loaded RLModel from %s (tag=%s, emb_dim=%d, source=%s, arch=%s, device=%s)",
            state_path, rl_model_tag, rl_emb_dim, active_emb_source,
            rl_arch_version, rl_model.device,
        )

        # v0.2.106: no ollama_url here any more — the server scores
        # client-sent vectors (rl_client/rerank_wire.py, protocol v2) and
        # embeds nothing, so it has no Ollama to reach.
        self._rl = RetrievalRL(
            project_root=project_root,
            rl_model=rl_model,
            data_logger=data_logger,
        )
        # Per-source (tag, dim, arch) — stashed for /rotate_weights bookkeeping
        # and /reset's global-weights file resolution.
        self._rl_model_tag = rl_model_tag
        self._rl_emb_dim = rl_emb_dim
        self._rl_arch_version = rl_arch_version
        self._rl_device_spec = rl_device           # original spec ("auto" / "cuda" / "cpu")
        self._rl_device_actual = rl_model.device   # resolved ("cuda:0" / "cpu")
        self._active_emb_source = active_emb_source
        self._state_dir = state_dir
        # v0.2.8 — pinned path of the active checkpoint. /rotate_weights
        # updates this; /reset replaces it. Source of truth for
        # _push_rl_weights_state and reset/retrain handlers.
        self._state_path = state_path
        # v0.2.8 — last finetune artifact path (separate from the active
        # rotate path because /finetune writes a *_finetuned.pt sibling
        # that the caller must explicitly /rotate_weights into). Empty
        # until the first successful finetune. Surfaced to the launcher
        # dashboard via rl_weights_state.last_finetuned_path so the GUI
        # "Rotate to most recent finetune" button (v0.2.44+) knows what
        # to point at.
        self._last_finetuned_path: "Path | None" = None

        # In-process job registry for /finetune + /global/retrain. Keyed by
        # job_id (UUID). Lost on container restart by design — the launcher
        # treats a 404 on status-poll as "job vanished; fall through to
        # rotate-only" (see run_finetune_then_rotate).
        self._jobs: dict[str, JobState] = {}
        self._jobs_lock = asyncio.Lock()

        # Global weights directory (used by /reset + /global/retrain).
        self._global_weights_dir = Path(
            os.environ.get("RL_GLOBAL_WEIGHTS_DIR", "/data/global")
        )

        # v0.2.6 — vct-hub client for module-DB writes (rl_state + rl_weights_state).
        # v0.2.11 — the client is now built UNCONDITIONALLY. A GLOBAL-scope container
        # legitimately has NO single VCT_PROJECT_ID (it serves all projects, routing
        # per-request by the X-VCT-Project-ID header), yet it STILL needs a hub client
        # for the DB-only training READ path (offline_trainer reads rl_events via the
        # module-scoped hub route, project resolved per-request). Previously the client
        # was None whenever VCT_PROJECT_ID was unset — which is ALWAYS true for a global
        # container — so training was dead-on-arrival. Decoupling fixes that.
        #
        # The client soft-fails on every call when it can't authenticate/reach the hub
        # (no VCT_MODULE_TOKEN, hub down, pre-injection launcher), so building it
        # unconditionally is safe: rerank is unaffected, hub writes/reads just no-op.
        # The per-PROJECT hub WRITES (rl_state / rl_weights_state) still gate on a
        # resolved project_id at their call sites (they need a concrete project key);
        # only the client OBJECT is now always present.
        self._project_id = os.environ.get("VCT_PROJECT_ID", "")
        # The legacy single-project fallback of header-less requests. Kept
        # apart from ``_project_id``, which ``_swap_in_head`` rewrites to the
        # routed project on every request (v0.2.106 fix: a header-less request
        # used to inherit the previous request's project).
        self._env_project_id = self._project_id
        self._hub_client: "_HubClient | None" = _HubClient()
        if not self._project_id:
            logger.info(
                "RLServer: VCT_PROJECT_ID unset (expected for a global-scope "
                "container). Hub client built for per-request-scoped reads/writes; "
                "per-project hub WRITES gate on the routed project_id. Rerank "
                "functionality unaffected.",
            )

        self._app = web.Application()
        self._app.router.add_get("/health", self._health)
        self._app.router.add_post("/cache_nodes", self._cache_nodes)
        self._app.router.add_post("/rl_update", self._rl_update)
        # v0.1.1 — model-management endpoints
        self._app.router.add_post("/rotate_weights", self._rotate_weights)
        self._app.router.add_post("/reset", self._reset)
        self._app.router.add_post("/finetune", self._finetune)
        self._app.router.add_get("/finetune_status", self._finetune_status)
        self._app.router.add_post("/global/retrain", self._global_retrain)
        # v0.2.2 — D1' state introspection (dashboard widget feed)
        self._app.router.add_get("/state_summary", self._state_summary)
        # v0.2.106 — the routed project's online learning + serving gate
        self._app.router.add_get("/project_status", self._project_status)

        # v0.2.106 — per-project online learning (online_learning.py). One
        # config for every project; tests and the replay pass their own.
        self._online_cfg: OnlineConfig = DEFAULT_CONFIG
        # Rounds running off the event loop (references kept so they are not
        # garbage-collected mid-flight; ``wait_for_rounds`` awaits them).
        self._round_tasks: "set[asyncio.Task]" = set()
        # rl_online_state pushes made when a project head loads (same
        # reference-keeping; ``wait_for_rounds`` awaits them too).
        self._push_tasks: "set[asyncio.Task]" = set()

        # v0.2.10 — per-project routing infrastructure.
        #
        # Single container, many projects: each request carries an
        # ``X-VCT-Project-ID`` header. The container loads the base model
        # at startup (above) and lazy-loads per-project heads on first
        # request via the LRU below. The base context is held as a
        # _ProjectHead under the sentinel key ("_base", source) so the
        # routing code paths can treat the no-header case uniformly.
        #
        # ``_routing_lock`` serializes the swap of
        # ``self._rl._rl_model`` / ``self._state_path`` /
        # ``self._project_id`` / ``self._rl._data_logger`` so concurrent
        # requests for different projects don't race on the shared
        # RetrievalRL instance. Reranking is ~10-50 ms per call so
        # serialization doesn't materially affect interactive UX — see
        # v0.2.49 plan, single-container architecture rationale.
        head_lru_size = int(os.environ.get(
            "RL_PROJECT_HEAD_LRU_SIZE",
            str(_DEFAULT_PROJECT_HEAD_LRU_SIZE),
        ))
        self._project_head_lru_size = max(1, head_lru_size)
        self._project_heads: "OrderedDict[tuple[str, str], _ProjectHead]" = OrderedDict()
        self._routing_lock = asyncio.Lock()
        # Audit log of project_id resolution events for ops introspection
        # ("no_project_id_using_base", "no_project_head_using_base",
        # "loaded_project_head"). Emitted at INFO so they show up in the
        # container's stdout/stderr without needing a separate sink.
        # Counters also exposed via /health for the launcher dashboard.
        self._routing_counters: dict[str, int] = {
            "requests_with_header": 0,
            "requests_without_header": 0,
            "head_loads": 0,
            "head_load_failures": 0,
            "base_fallback_no_weights": 0,
            # v0.2.106: a project's own checkpoint was set aside because the
            # bundled weights supersede it (choose_local_checkpoint).
            "project_head_superseded_by_bundled": 0,
        }

        # Seed the LRU with the base context built in __init__ above.
        # When the container has VCT_PROJECT_ID set (legacy single-project
        # deployments), we also pre-create a head for that project_id
        # pointing at the SAME model + state_path + data_logger as the
        # base. Pre-v0.2.10 clients (no header) still hit the base.
        base_head = _ProjectHead(
            project_id=_BASE_PROJECT_SENTINEL,
            embedding_source=self._active_emb_source,
            rl_model=self._rl._rl_model,
            state_path=self._state_path,
            data_logger=data_logger,
        )
        self._project_heads[(_BASE_PROJECT_SENTINEL, self._active_emb_source)] = base_head
        # Log root (parent dir of the configured --log-path). Per-project
        # logs live in this same directory under the per-project filename
        # pattern (see _per_project_log_path). Stashed for /finetune /reset /
        # /global/retrain handlers that derive events paths.
        self._log_root = Path(log_path).parent

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # v0.2.10 — per-project routing helpers
    # ------------------------------------------------------------------

    def _resolve_request_project_id(self, request: "web.Request") -> str:
        """Return the project_id for an incoming request.

        Resolution order:
          1. ``X-VCT-Project-ID`` header (case-insensitive per HTTP spec)
          2. ``VCT_PROJECT_ID`` env var (legacy single-project install)
          3. ``_BASE_PROJECT_SENTINEL`` ('_base') — request will use the
             base model without per-project personalization.

        Side effects: bumps ``_routing_counters`` based on which branch
        was taken. Emits an INFO log on the no-header fallback so ops
        can spot pre-routing clients.
        """
        header_val = request.headers.get(_HEADER_PROJECT_ID, "").strip()
        if header_val:
            self._routing_counters["requests_with_header"] += 1
            return header_val
        self._routing_counters["requests_without_header"] += 1
        # Legacy single-project env fallback. When VCT_PROJECT_ID is set
        # (the pre-v0.2.10 deployment shape), treat it as if every request
        # carried that header. This means an unmodified v0.2.9 container
        # behaves identically post-update so long as the launcher keeps
        # setting VCT_PROJECT_ID.
        if self._env_project_id:
            return self._env_project_id
        # Truly no project context: the launcher hasn't shipped a header
        # yet AND VCT_PROJECT_ID is empty. Audit-log the fallback so the
        # operator notices their clients aren't sending the header.
        logger.info(
            "routing: no_project_id_using_base (path=%s); request will use "
            "base model without per-project personalization",
            request.rel_url.path if request.rel_url is not None else "<unknown>",
        )
        return _BASE_PROJECT_SENTINEL

    async def _get_or_load_project_head(self, project_id: str) -> _ProjectHead:
        """Return the project head for ``project_id``, loading on first use.

        Lookup is keyed by (project_id, active_embedding_source). The
        container is pinned to one embedding source for its lifetime (see
        _embedding_mismatch_response); we don't try to serve mixed sources
        from one process.

        LRU eviction policy: ``move_to_end`` on hit; ``popitem(last=False)``
        on miss when at capacity. The base sentinel head is pinned: it
        never evicts (re-inserted to the front when touched). This keeps
        no-header / legacy traffic from hammering the disk under load.

        Load policy: looks for the latest ``rl_model_<source>_*.pt`` file
        in the per-project state dir. When present, loads it. When absent,
        falls back to a fresh copy of the BASE model's checkpoint (via
        ``RLModel.load_or_create`` against the base ``state_path``) and
        flags the head as ``is_base_fallback=True`` so callers can see the
        request is being served without per-project specialization.
        """
        source = self._active_emb_source
        key = (project_id, source)
        existing = self._project_heads.get(key)
        if existing is not None:
            # Base sentinel always re-reads from the live
            # ``self._rl._rl_model`` so tests that mutate it post-__init__
            # (recorder/RLModel-fake pattern) observe their swap. For
            # non-base heads, return the cached entry verbatim.
            if project_id == _BASE_PROJECT_SENTINEL:
                existing.rl_model = self._rl._rl_model
                existing.state_path = self._state_path
                existing.data_logger = self._rl._data_logger
            # Touch — preserve recently-used ordering.
            self._project_heads.move_to_end(key)
            return existing

        # Base sentinel should already have been seeded in __init__, but be
        # defensive — if some path evicted it we can recreate from
        # ``self._rl._rl_model``.
        if project_id == _BASE_PROJECT_SENTINEL:
            base_head = _ProjectHead(
                project_id=_BASE_PROJECT_SENTINEL,
                embedding_source=source,
                rl_model=self._rl._rl_model,
                state_path=self._state_path,
                data_logger=self._rl._data_logger,
            )
            self._project_heads[key] = base_head
            self._project_heads.move_to_end(key)
            return base_head

        # Lazy-load per-project head. Look for an existing checkpoint under
        # the per-project state dir; absent → base-fallback head with a
        # freshly-cloned base model (load_or_create against the BASE
        # state_path is correct here: it returns a NEW RLModel instance
        # initialised from the same .pt, so subsequent online updates on
        # this head do NOT contaminate the base or other projects' heads).
        project_state_dir = _per_project_state_dir(self._state_dir, project_id)
        project_state_dir.mkdir(parents=True, exist_ok=True)
        candidate_files = _glob_weights_files(project_state_dir, self._rl_model_tag)
        if candidate_files and self._bundled_weights is not None:
            # Same precedence as the base model: a project checkpoint is used
            # only when it has earned it over the shipped one.
            chosen = choose_local_checkpoint(candidate_files, self._bundled_weights.version)
            if chosen is not None:
                candidate_files = [chosen]
            else:
                candidate_files = []
                self._routing_counters["project_head_superseded_by_bundled"] += 1
        if candidate_files:
            target_path = candidate_files[-1]
            is_fallback = False
        else:
            # No per-project weights yet → fall back to the base model.
            # We load_or_create FROM the base state_path; load_or_create
            # builds a fresh RLModel instance reading the same .pt bytes,
            # so online updates against this head won't affect base.
            target_path = self._state_path
            is_fallback = True
            self._routing_counters["base_fallback_no_weights"] += 1
            logger.info(
                "routing: no_project_head_using_base project_id=%s source=%s "
                "(no rl_model_%s_*.pt found in %s); request will use a clone "
                "of base weights",
                project_id, source, self._rl_model_tag, project_state_dir,
            )

        learner: "ProjectLearner | None" = None
        try:
            # A fresh initialisation (no checkpoint at all) is seeded, so a
            # project's online learning is deterministic from its first event.
            with _seeded_init(self._online_cfg.seed, enabled=not target_path.exists()):
                new_model = RLModel.load_or_create(
                    state_path=target_path,
                    emb_dim=self._rl_emb_dim,
                    arch_version=self._rl_arch_version,
                    device=self._rl_device_spec,
                    embedding_source=source,
                )
            if is_fallback:
                # Started from the base checkpoint: train and save under the
                # project's own dir, never over the base file.
                target_path = project_state_dir / _build_weights_filename(
                    self._rl_model_tag, _model_base_version(new_model),
                )
                new_model.retarget(target_path)
            learner = ProjectLearner.load(
                project_state_dir, profile=self._rl_model_tag, emb_dim=self._rl_emb_dim,
                model=new_model, cfg=self._online_cfg,
            )
        except Exception as exc:
            # Catastrophic load failure: log + fall back to the in-process
            # base model directly. We never raise out of routing — the
            # request must still succeed (with the client's search order:
            # no learner, so nothing trains the shared base model).
            self._routing_counters["head_load_failures"] += 1
            logger.warning(
                "routing: head_load_failed project_id=%s source=%s path=%s "
                "err=%s; falling back to shared base model in-process",
                project_id, source, target_path, exc,
            )
            new_model = self._rl._rl_model
            target_path = self._state_path
            is_fallback = True
            learner = None

        per_project_log = _per_project_log_path(self._log_root, project_id)
        per_project_logger = RLDataLogger(
            log_path=per_project_log,
            project=project_id,
            embedding_source=source,
            embedding_dim=self._rl_emb_dim,
        )

        head = _ProjectHead(
            project_id=project_id,
            embedding_source=source,
            rl_model=new_model,
            state_path=target_path,
            data_logger=per_project_logger,
            is_base_fallback=is_fallback,
            learner=learner,
            project_dir=project_state_dir,
        )

        # LRU insert with eviction. The base sentinel head is pinned and
        # never evicted — find a non-base candidate to evict if needed. A
        # head whose online round is still running is not evicted (its
        # result would land on a head nobody routes to any more); an evicted
        # head's learner is saved first, so its queued events survive.
        self._project_heads[key] = head
        self._routing_counters["head_loads"] += 1
        while len(self._project_heads) > self._project_head_lru_size:
            for evict_key in list(self._project_heads.keys()):
                cand = self._project_heads[evict_key]
                busy = cand.learner is not None and cand.learner.round_in_flight
                if evict_key[0] != _BASE_PROJECT_SENTINEL and evict_key != key and not busy:
                    evicted = self._project_heads.pop(evict_key)
                    self._save_learner(evicted)
                    logger.info(
                        "routing: evicted project head project_id=%s source=%s "
                        "(LRU size=%d)",
                        evicted.project_id, evicted.embedding_source,
                        self._project_head_lru_size,
                    )
                    break
            else:
                # Only base sentinel remains; nothing else evictable.
                break

        logger.info(
            "routing: loaded_project_head project_id=%s source=%s path=%s "
            "fallback=%s",
            project_id, source, target_path, is_fallback,
        )
        if learner is not None:
            # The launcher's RL panel reads the rl_online_state row, not this
            # process: re-publish what THIS load found (a recreated container,
            # a learner state discarded for another checkpoint, a state dir
            # that is gone), so the row never keeps claiming a model is served
            # that this container does not hold. Off the request path.
            task = asyncio.create_task(self._push_rl_online_state(head))
            self._push_tasks.add(task)
            task.add_done_callback(self._push_tasks.discard)
        return head

    async def _route_request(self, request: "web.Request") -> "tuple[str, _ProjectHead]":
        """Resolve the project_id + project head for an incoming request.

        Returns (project_id, head). The caller MUST hold ``_routing_lock``
        before swapping in the head's fields (rl_model / state_path /
        data_logger / project_id) to avoid concurrent-request races.
        """
        project_id = self._resolve_request_project_id(request)
        head = await self._get_or_load_project_head(project_id)
        return project_id, head

    def _swap_in_head(self, head: _ProjectHead) -> None:
        """Swap the project head into the shared ``self._rl`` instance.

        Mutates ``self._rl._rl_model``, ``self._rl._data_logger``,
        ``self._state_path``, and ``self._project_id`` so the rest of the
        handler code (which still reads these fields) sees the per-project
        context. Caller MUST hold ``_routing_lock`` for the duration of
        the handler's logical operation — concurrent invocations of
        different heads would race here.
        """
        self._rl._rl_model = head.rl_model
        self._rl._data_logger = head.data_logger
        self._state_path = head.state_path
        # Use the routed project_id for hub writes. When the head is the
        # base sentinel and the env-supplied VCT_PROJECT_ID is set, prefer
        # the env value (preserves legacy single-project hub behavior).
        if head.project_id == _BASE_PROJECT_SENTINEL:
            self._project_id = os.environ.get("VCT_PROJECT_ID", "")
        else:
            self._project_id = head.project_id
        # Per-project flags cache: restore from the head's cached value so
        # _fetch_project_flags hits the right project's TTL window.
        if head.flags_cache is not None:
            self._project_flags_cached_at, self._project_flags_cache = head.flags_cache
        else:
            # Invalidate the previous project's cache so we don't serve
            # stale flags. Setting timestamp to 0 forces a refresh on the
            # next _fetch_project_flags call.
            self._project_flags_cache = None
            self._project_flags_cached_at = 0.0

    def _capture_head_state(self, head: _ProjectHead) -> None:
        """Persist any post-handler state back to the head record.

        Mirrors back the items mutated by handlers (state_path on
        rotate/reset, flags cache, last_finetuned_path) so the LRU stays
        the source of truth for the next request that lands on this
        project.
        """
        live = self._rl._rl_model
        if head.learner is not None and live is not None and live is not head.rl_model:
            # v0.2.106: every model swap of a project (/rotate_weights,
            # /reset, the rl_use_global switch) lands here. The held-out
            # window may have been trained on by the new model, so the gate
            # starts over and the project is served its search order until
            # the new model passes on fresh queries.
            head.learner.on_model_replaced(live, "rotate / reset / global model")
        head.state_path = self._state_path
        head.rl_model = live
        if self._project_flags_cache is not None:
            head.flags_cache = (self._project_flags_cached_at, self._project_flags_cache)
        if self._last_finetuned_path is not None:
            head.last_finetuned_path = self._last_finetuned_path

    def _embedding_mismatch_response(
        self, request_active: "str | None"
    ) -> "web.Response | None":
        """Return a 409 response when the request's active_embedding doesn't
        match this container's pinned source. Returns None when there's no
        mismatch (either fields match, or the request omitted the field).

        Omitted field is permissive — callers from older clients pre-dating
        this cross-check won't have it. The launcher's spawn-per-project
        contract is the real guarantee; this check is belt-and-braces for
        Scenario B (one Python client serving multiple projects, talking to
        the wrong RL container). Once the MCP client sends the per-request
        active_embedding kwarg on RLClient, we can tighten the omitted-field
        case too.
        """
        if not request_active:
            return None
        if request_active == self._active_emb_source:
            return None
        return web.json_response(
            {
                "ok": False,
                "error": "active_embedding_mismatch",
                "container_active_embedding": self._active_emb_source,
                "request_active_embedding": request_active,
                "hint": (
                    "This RL container is pinned to a single embedding source "
                    "for the lifetime of the process. Spawn a separate "
                    "container per (project, active_embedding) pair, or "
                    "restart this one with ACTIVE_EMBEDDING set to the "
                    "requested value."
                ),
            },
            status=409,
        )

    async def _health(self, request: web.Request) -> web.Response:
        # v0.2.10 — surface routing counters + LRU occupancy so the launcher's
        # dashboard widget can see how the per-project routing is being used.
        # Reading without the lock is fine; counters are int-incremented and
        # the LRU dict size is a Python int read, both atomic at the GIL level.
        trained = self._base_model_is_trained()
        bundled = self._bundled_weights
        return web.json_response({
            "ok": True,
            # "not trained" = the base model is not a released, trained
            # checkpoint (fresh weights). Ranking still runs; whether its
            # scores are USED is the client's scoring lock, not this server.
            "model": "RLModel loaded" if trained else "not trained",
            "trained": trained,
            "weights": {
                "profile": self._rl_model_tag,
                "loaded_version": self._loaded_weights_version() or None,
                "bundled_version": bundled.version if bundled else None,
            },
            "active_embedding": self._active_emb_source,
            # RL-10 negotiation fields (rl_client.client.RLClient.negotiate):
            # the /cache_nodes wire contract this server speaks, and the
            # embedding space its model expects.
            "protocol_version": RERANK_PROTOCOL_VERSION,
            "embedding_dim": self._rl_emb_dim,
            "embedding_space": self._active_emb_source,
            # The client leaves the linked slots out of its features when
            # they are masked for this profile (this server masks anyway).
            "linked_slots_masked": linked_slots_masked(self._rl_model_tag),
            # v0.2.106 (owner 2026-11-12): every project's model is gated by
            # this server on that project's own held-out queries; a project
            # whose model does not pass gets its own search order back
            # (``serving: "search_order"``). So the client may call this
            # server even while ``trained`` is false.
            "per_project_gate": True,
            "online_learning": self._online_summary(),
            "arch_version": self._rl_arch_version,
            "model_tag": self._rl_model_tag,
            "emb_dim": self._rl_emb_dim,
            "device_spec": self._rl_device_spec,
            "device": self._rl_device_actual,
            "routing": {
                "lru_size": len(self._project_heads),
                "lru_capacity": self._project_head_lru_size,
                "counters": dict(self._routing_counters),
            },
        })

    def _loaded_weights_version(self) -> str:
        """``weights_version`` recorded in the BASE model's checkpoint."""
        base = self._project_heads.get((_BASE_PROJECT_SENTINEL, self._active_emb_source))
        model = base.rl_model if base is not None else self._rl._rl_model
        return str(getattr(model, "weights_version", "") or "")

    def _base_model_is_trained(self) -> bool:
        """True when the base model came from a released checkpoint.

        A released checkpoint carries a semver ``weights_version`` (set by the
        trainer at save time). A fresh initialisation carries ``""`` and a
        local fork that was never synced to a release carries the
        ``"local"`` sentinel; neither is a trained model.
        """
        version = self._loaded_weights_version()
        return bool(version) and version != _LOCAL_FORK_VERSION_SENTINEL

    async def _state_summary(self, request: web.Request) -> web.Response:
        """Read-only introspection of the live model's D1' state.

        Feeds the launcher's dashboard widget — exposes how many dynamic
        types the user has accumulated, plus on-disk layout sanity flags
        (marker + sidecar dir present). Layout-unaware callers (e.g.
        pre-v0.2.2 launchers) can ignore the layout fields safely.

        Soft-fail: if the model isn't loaded yet (shouldn't happen post
        __init__ but defensive), reports zero counts rather than erroring.
        Layout fields go to null in that case so the launcher can
        distinguish "no model" from "model with no dynamic types".
        """
        model = self._rl._rl_model
        if model is None:
            return web.json_response({
                "ok": True,
                "builtin_types": N_ENTITY_TYPES,
                "dynamic_types": 0,
                "marker_present": None,
                "sidecar_dir_present": None,
                "model_loaded": False,
            })

        dynamic_count = sum(
            1 for idx in model._type_registry.values()
            if idx >= N_ENTITY_TYPES
        )
        return web.json_response({
            "ok": True,
            "builtin_types": N_ENTITY_TYPES,
            "dynamic_types": dynamic_count,
            "marker_present": model._layout_marker_path.exists(),
            "sidecar_dir_present": model._sidecar_dir.is_dir(),
            "model_loaded": True,
        })

    # ------------------------------------------------------------------
    # vct-hub writers (v0.2.6, module-DB persistence)
    #
    # All hub writes are best-effort. Failure to write to hub never aborts
    # a rerank request, never corrupts in-memory model state, never raises
    # out of the calling handler. The dashboard widget tolerates stale data.
    # ------------------------------------------------------------------

    async def _push_rl_state(self) -> None:
        """UPSERT the current `rl_state` row via vct-hub.

        Called on container startup (post-RLModel.load_or_create) and after
        any operation that may have mutated the model's structural state:
        /rotate_weights swap, /reset model-swap, dynamic-type registration
        (via the wrapping save() path).

        No-op when VCT_PROJECT_ID is unset OR hub client is None.
        Schema mirrors /state_summary's response: same fields, same keys.
        """
        if self._hub_client is None or not self._project_id:
            return
        model = self._rl._rl_model
        if model is None:
            return
        dynamic_count = sum(
            1 for idx in model._type_registry.values()
            if idx >= N_ENTITY_TYPES
        )
        fields = {
            "refreshed_at": int(time.time()),
            "builtin_types": N_ENTITY_TYPES,
            "dynamic_types": dynamic_count,
            "marker_present": int(model._layout_marker_path.exists()),
            "sidecar_dir_present": int(model._sidecar_dir.is_dir()),
            "model_loaded": 1,
            "arch_version": self._rl_arch_version,
            "emb_dim": self._rl_emb_dim,
            "active_embedding": self._active_emb_source,
        }
        await self._hub_client.upsert_row(
            self._project_id, "rl_state", self._project_id, fields,
        )

    async def _push_rl_weights_state(
        self,
        *,
        last_rotated: bool = False,
        last_finetuned: bool = False,
    ) -> None:
        """UPSERT the current `rl_weights_state` row via vct-hub.

        Composite key is (project_id, embedding_source); we pass the
        compound key as f"{project_id}:{embedding_source}" in the URL path.

        `last_rotated=True` stamps last_rotated_at to now.
        `last_finetuned=True` stamps last_finetuned_at to now.
        Both flags can be passed in the same call (rare; e.g. fork_and_specialize
        ends with both a rotation AND a finetune).

        No-op when VCT_PROJECT_ID is unset OR hub client is None.
        """
        if self._hub_client is None or not self._project_id:
            return
        now = int(time.time())
        # Always reflect the currently-active checkpoint. v0.2.8: tracked
        # on self._state_path; updated by /rotate_weights and /reset.
        state_pt = self._state_path
        # v0.2.7 — pull weights_version + embedding_source from the in-memory
        # model (populated by _try_load from the checkpoint's metadata, or
        # empty string for fresh-init / legacy checkpoints).
        live_model = self._rl._rl_model
        ckpt_weights_version = live_model.weights_version if live_model is not None else ""
        ckpt_embedding_source = live_model.embedding_source if live_model is not None else ""
        # v0.2.8 — read the 3 per-project flags from hub config so the
        # dashboard's "is_using_global" / "online_training_disabled"
        # mirrors stay fresh without requiring an extra hub roundtrip
        # from the launcher's reading side.
        flags = await self._fetch_project_flags()
        fields: dict[str, Any] = {
            # local_version is the user-displayed weights label. Prefer the
            # checkpoint's semver (e.g. "0.2.6"); fall back to arch_version
            # only when the checkpoint is pre-v0.2.7 (no weights_version baked).
            "local_version": ckpt_weights_version or self._rl_arch_version,
            "local_arch_version": self._rl_arch_version,
            "local_path": str(state_pt) if state_pt.exists() else "",
            # v0.2.7 additions (R5 + R4). Both default to empty when unknown.
            "weights_version": ckpt_weights_version,
            "embedding_source": ckpt_embedding_source or self._active_emb_source,
            # v0.2.8 additions: per-project lifecycle mirrors + finetune path.
            "is_using_global": 1 if flags.get("rl_use_global") else 0,
            "online_training_disabled": 1 if flags.get("rl_online_training_disabled") else 0,
            "last_finetuned_path": (
                str(self._last_finetuned_path)
                if self._last_finetuned_path is not None
                else ""
            ),
        }
        if last_rotated:
            fields["last_rotated_at"] = now
        if last_finetuned:
            fields["last_finetuned_at"] = now
        # Composite key for (project_id, embedding_source) per the table's
        # PRIMARY KEY. URL-encoded with ':' separator; the hub side parses
        # it back into the two-column PK on UPSERT.
        composite_key = f"{self._project_id}:{self._active_emb_source}"
        await self._hub_client.upsert_row(
            self._project_id, "rl_weights_state", composite_key, fields,
        )

    async def _push_rl_global_weights_state(
        self,
        *,
        last_rotated: bool = False,
    ) -> None:
        """UPSERT the current `rl_global_weights_state` row via vct-hub.

        Distinct from rl_weights_state (per-project): this table tracks
        which global checkpoint is loaded RIGHT NOW per embedding_source
        for projects with rl_use_global=true. Key = embedding_source.

        Called from /rotate_weights when the rotated-in file lives under
        /data/global/ (i.e. the rotation targeted the global slot rather
        than a per-project local checkpoint).

        Soft-fails like _push_rl_weights_state.
        """
        if self._hub_client is None or not self._project_id:
            return
        now = int(time.time())
        live_model = self._rl._rl_model
        ckpt_weights_version = live_model.weights_version if live_model is not None else ""
        fields: dict[str, Any] = {
            "local_version": ckpt_weights_version or self._rl_arch_version,
            "local_arch_version": self._rl_arch_version,
            "local_path": str(self._state_path) if self._state_path.exists() else "",
        }
        if last_rotated:
            fields["last_rotated_at"] = now
            # First-time write also stamps last_loaded_at; subsequent
            # rotations to the same source only bump last_rotated_at.
            fields["last_loaded_at"] = now
        # Key is just embedding_source (this table is project-agnostic).
        # The hub's module-DB router uses project_id for routing/auth
        # but the WHERE clause is built from the row's primary key.
        await self._hub_client.upsert_row(
            self._project_id,
            "rl_global_weights_state",
            self._active_emb_source,
            fields,
        )

    # ------------------------------------------------------------------
    # v0.2.8 — per-project flags from vct-hub config endpoint
    # ------------------------------------------------------------------

    # Cache TTL for the 3 flags. 30s is short enough that GUI toggles
    # feel responsive (worst-case 30s lag between user-flip and
    # container honoring) but long enough to avoid hammering the hub
    # on every /cache_nodes call. The container queries the hub on
    # every cache_nodes/rl_update by default; this cache reduces that
    # to ~2 hub calls/min under heavy reranking load.
    _PROJECT_FLAGS_CACHE_TTL_S: float = 30.0

    async def _fetch_project_flags(self) -> dict[str, bool]:
        """Return the 3 per-project RL flags from vct-hub.

        Fields returned (all default False on missing/error):
          - rl_use_global
          - rl_online_training_disabled
          - rl_global_training_source_flag

        Cached for _PROJECT_FLAGS_CACHE_TTL_S seconds. Soft-fails when
        the hub is unreachable (returns the cached value if still in
        TTL, or all-False on the first uncached call).
        """
        cached = getattr(self, "_project_flags_cache", None)
        cached_at = getattr(self, "_project_flags_cached_at", 0.0)
        now = time.time()
        if cached is not None and now - cached_at < self._PROJECT_FLAGS_CACHE_TTL_S:
            return cached

        # No project_id → no hub call possible. Return all-False; cache
        # for full TTL so we don't busy-loop on misconfigured containers.
        if self._hub_client is None or not self._project_id:
            empty = {"rl_use_global": False, "rl_online_training_disabled": False,
                     "rl_global_training_source_flag": False}
            self._project_flags_cache = empty
            self._project_flags_cached_at = now
            return empty

        # v0.2.11: read the 3 flags via the MODULE-SCOPED config route
        # `GET /api/v1/modules/{module_id}/projects/{pid}/config`, authorized by
        # the container's module-identity token (VCT_MODULE_TOKEN) — symmetric
        # with the rl/events route. The launcher-public `/projects/{pid}/config`
        # route is gated by `hub.token` (which the container correctly does NOT
        # hold), so the container must use the module-scoped mirror. The hub
        # serves the SAME flag values there (the existing project_config handler
        # mounted under the module-scoped path). Response shape is unchanged:
        # the 3 flags as top-level booleans, or nested under `rl_settings`.
        flags: dict[str, bool] = {
            "rl_use_global": False,
            "rl_online_training_disabled": False,
            "rl_global_training_source_flag": False,
        }
        try:
            session = await self._hub_client._ensure_session()
            url = (
                f"{self._hub_client._base}/api/v1/modules/{self._hub_client._module_id}"
                f"/projects/{self._project_id}/config"
            )
            await self._hub_client._maybe_refresh_token()
            async with session.get(url, headers=self._hub_client._headers()) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    # Tolerate either nested {"rl_settings": {...}} or flat shape.
                    settings = body.get("rl_settings") if isinstance(body, dict) else None
                    src = settings if isinstance(settings, dict) else body
                    if isinstance(src, dict):
                        for k in flags:
                            v = src.get(k)
                            if isinstance(v, bool):
                                flags[k] = v
                else:
                    logger.warning(
                        "_fetch_project_flags: module-scoped config route returned "
                        "status=%d (project=%s); per-project toggles will use "
                        "all-False defaults until resolved", resp.status, self._project_id,
                    )
        except Exception as exc:
            logger.debug("_fetch_project_flags soft-failed: %s", exc)

        self._project_flags_cache = flags
        self._project_flags_cached_at = now
        return flags

    async def _ensure_global_model_loaded(self) -> None:
        """Swap the in-memory RLModel to the latest global checkpoint
        for this source, if not already loaded.

        Called from /cache_nodes when the project has rl_use_global=true.
        Soft-fails: on any error the existing model stays in place and
        rerank continues with project-local weights (correctness-degrades-
        gracefully, since the local model is still trained on this
        project's events).

        Idempotent: a second call when the global is already loaded is
        a cheap path-compare with no I/O.
        """
        # Already pointing at a global slot? No work.
        if str(self._state_path).startswith("/data/global/"):
            return

        candidates = _glob_weights_files(self._global_weights_dir, self._rl_model_tag)
        if not candidates:
            logger.debug(
                "_ensure_global_model_loaded: no global checkpoint for source=%r in %s",
                self._rl_model_tag, self._global_weights_dir,
            )
            return
        target = candidates[-1]

        try:
            new_model = RLModel.load_or_create(
                state_path=target,
                emb_dim=self._rl_emb_dim,
                arch_version=self._rl_arch_version,
                device=self._rl_device_spec,
                embedding_source=self._active_emb_source,
            )
        except Exception as exc:
            logger.warning(
                "_ensure_global_model_loaded: load failed for %s: %s "
                "(staying on local model)", target, exc,
            )
            return

        self._rl._rl_model = new_model
        self._state_path = target
        logger.info(
            "_ensure_global_model_loaded: swapped to global %s for project %s",
            target, self._project_id,
        )
        # Bump dashboard state — both the per-project mirror (so
        # is_using_global flips correctly) and the global slot.
        await self._push_rl_state()
        await self._push_rl_weights_state(last_rotated=True)
        await self._push_rl_global_weights_state(last_rotated=True)

    async def _cache_nodes(self, request: web.Request) -> web.Response:
        """Score one search's candidates with the loaded model.

        Body JSON (wire protocol v2, ``rl_client.rerank_wire``):
          task_id:          str
          nodes:            list[dict] — slim candidates (``title``,
                            ``node_type``), in search order
          limit:            int — how many to return in ``top_k``
          features:         dict — the serve-time feature block (query vector,
                            per-node vector / linked vectors / types), aligned
                            with ``nodes``
          active_embedding: str — optional; a mismatch with this container's
                            pinned source is refused (409)
          query, session_id: accepted and unused — the container writes no
                            retrieval events (the MCP logs them, with the same
                            vectors, to ``rl_events``)

        Response JSON (200):
          ok, task_id, scored (true),
          order:   request-node indices, best first (a permutation),
          scores:  model score per request node (null = node had no vector),
          top_k:   ``nodes`` in ``order``, truncated to ``limit``,
          weights_version, linked_slots_masked

        400 malformed body; 409 embedding mismatch; 422 features missing or
        malformed; 500 unexpected error (logged here, not echoed).
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any undecodable body is a 400
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("task_id"), str):
            return web.json_response(
                {"ok": False, "error": "task_id (string) is required"}, status=400,
            )
        task_id: str = body["task_id"]
        nodes = body.get("nodes")
        if not isinstance(nodes, list):
            return web.json_response(
                {"ok": False, "error": "nodes must be a list"}, status=400,
            )
        try:
            limit = int(body.get("limit", len(nodes)))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "limit must be an integer"}, status=400,
            )

        # Cross-check the request's active_embedding against this container's
        # pinned source: a mismatch means the client is talking to the wrong
        # container; refuse loudly rather than score with the wrong network.
        mismatch = self._embedding_mismatch_response(body.get("active_embedding"))
        if mismatch is not None:
            return mismatch

        features = body.get("features")
        if features is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "features_required",
                    "hint": (
                        "The container does not embed anything; send the "
                        "serve-time features (rl_client.rerank_wire, protocol "
                        f"v{RERANK_PROTOCOL_VERSION})."
                    ),
                },
                status=422,
            )

        try:
            # v0.2.10 per-project routing: X-VCT-Project-ID header (falling
            # back to env / base sentinel) selects the model head. The lock
            # keeps concurrent requests for different projects from racing
            # on the shared RetrievalRL instance.
            _project_id, head = await self._route_request(request)
            async with self._routing_lock:
                self._swap_in_head(head)
                # v0.2.8: rl_use_global=true scores with the latest global
                # checkpoint for this source (soft-fails to the local model).
                flags = await self._fetch_project_flags()
                if flags.get("rl_use_global"):
                    await self._ensure_global_model_loaded()
                self._capture_head_state(head)
                weights_version = str(
                    getattr(self._rl._rl_model, "weights_version", "") or ""
                )
                # v0.2.106 per-project gate: the model reranks only for a
                # project whose model passes the owner's bar on that
                # project's own held-out queries; every other request gets
                # the client's own order back, unchanged.
                learner = head.learner
                if learner is None or not learner.serving:
                    why = (
                        "no project id on the request (per-project models only)"
                        if head.project_id == _BASE_PROJECT_SENTINEL
                        else "the project's model could not be loaded"
                        if learner is None
                        else learner.reason
                    )
                    return web.json_response(
                        self._search_order_answer(task_id, nodes, limit, why, weights_version)
                    )
                try:
                    result = self._rl.rerank(
                        features, len(nodes), profile=self._rl_model_tag,
                    )
                except ValueError as exc:
                    return web.json_response(
                        {"ok": False, "error": "invalid_features", "detail": str(exc)},
                        status=422,
                    )
        except Exception as exc:  # noqa: BLE001 — never crash the handler
            logger.exception("cache_nodes error: %s", exc)
            return web.json_response({"ok": False, "error": "internal_error"}, status=500)

        top_k = [nodes[i] for i in result.order][: max(0, limit)]
        return web.json_response({
            "ok": True,
            "task_id": task_id,
            "scored": True,
            "serving": "model",
            "reason": learner.reason,
            "order": result.order,
            "scores": result.scores,
            "top_k": top_k,
            "weights_version": weights_version or None,
            "linked_slots_masked": result.linked_slots_masked,
        })

    def _search_order_answer(
        self, task_id: str, nodes: list, limit: int, why: str, weights_version: str,
    ) -> dict[str, Any]:
        """The ``/cache_nodes`` answer for a project whose model is not served:
        the client's own order, unchanged (``scored: false``)."""
        n = len(nodes)
        return {
            "ok": True,
            "task_id": task_id,
            "scored": False,
            "serving": "search_order",
            "reason": why,
            "order": list(range(n)),
            "scores": [None] * n,
            "top_k": list(nodes[: max(0, limit)]),
            "weights_version": weights_version or None,
            "linked_slots_masked": linked_slots_masked(self._rl_model_tag),
        }

    async def _rl_update(self, request: web.Request) -> web.Response:
        """v0.2.9 (C8) pre-packed payload contract.

        Body JSON:
          task_ids:          list[str] — task ids referenced by this batch.
          active_embedding:  str — pinned embedder; mismatch -> reject.
          tasks:             {task_id: {nodes_packed, query_emb, cosine_sims,
                                        literal_cited, cross_encoder_cited,
                                        targets?}}
          task_type:         str (optional, default "mcp_interactive").

        ``tasks[id]`` carries the frozen retrieval-time vectors + the citation
        inputs (cosine_sims, literal_cited, cross_encoder_cited). v0.2.106:
        each task enters the routed project's online learner
        (``online_learning.ProjectLearner.ingest``: unified target formula,
        held-out window, purge gap); every ``round_every`` events a training
        round + gate evaluation runs off the event loop (``_finish_round``).
        Nothing trains for a request without a project id. Container does NO
        embedding / chunking / data-logger writes (MCP is sole citation
        writer).

        Training is skipped (the events still feed the held-out check) when
        the project set ``rl_online_training_disabled`` or ``rl_use_global``
        (the shared model is read-only for the project).

        Pre-C8 callers sent ``agent_output: str`` -- those callers are
        deprecated. If ``tasks`` is absent and ``agent_output`` is present,
        we log a deprecation warning and return 200 with a "deprecated"
        marker (no training step). MCP-side v0.2.47 always sends the new
        shape; the legacy branch is a one-release deprecation window.

        Fire-and-forget from caller's perspective.
        """
        try:
            body: dict[str, Any] = await request.json()
            task_ids: list[str] = body.get("task_ids", [])
            tasks: dict[str, Any] = body.get("tasks") or {}
            task_type: str = body.get("task_type") or "mcp_interactive"

            # Same cross-check as /cache_nodes -- refuse training updates from
            # a client whose pinned embedder doesn't match ours, otherwise we
            # contaminate this container's network with off-source signal.
            req_active = body.get("active_embedding")
            mismatch = self._embedding_mismatch_response(req_active)
            if mismatch is not None:
                return mismatch

            # Legacy shape -- deprecated, returned 200 with marker. The
            # v0.2.10+ release will reject this with 400.
            if not tasks and body.get("agent_output"):
                logger.warning(
                    "rl_update: legacy agent_output shape received "
                    "(task_ids=%d); ignored. MCP-side caller must upgrade "
                    "to the v0.2.9 pre-packed-payload contract.",
                    len(task_ids),
                )
                return web.json_response(
                    {"ok": True, "skipped": "legacy_shape_deprecated"}
                )

            if not task_ids or not tasks:
                return web.json_response(
                    {"ok": True, "skipped": "no task_ids or tasks"}
                )

            if not isinstance(tasks, dict) or not isinstance(task_ids, list):
                return web.json_response(
                    {"ok": False, "error": "tasks must be an object and task_ids a list"},
                    status=400,
                )

            # v0.2.10 routing: the events land on the routed project's head;
            # v0.2.106: in that project's online learner (one per project,
            # never the shared base).
            project_id, head = await self._route_request(request)
            learner = head.learner
            if learner is None:
                return web.json_response({
                    "ok": True, "scheduled": 0,
                    "skipped": (
                        "no_project_id" if head.project_id == _BASE_PROJECT_SENTINEL
                        else "project_model_unavailable"
                    ),
                })
            job = None
            async with self._routing_lock:
                self._swap_in_head(head)

                flags = await self._fetch_project_flags()
                skip_train = bool(flags.get("rl_online_training_disabled")) or bool(
                    flags.get("rl_use_global"))
                if skip_train:
                    logger.info(
                        "rl_update: online training off for project_id=%s "
                        "(online_training_disabled / use_global); %d events feed "
                        "the held-out check only", project_id, len(task_ids),
                    )
                self._capture_head_state(head)

                now_ms = int(time.time() * 1000)
                scheduled = 0
                for task_id in task_ids:
                    payload = tasks.get(task_id)
                    if not isinstance(payload, dict):
                        continue
                    if learner.ingest(payload, ts_ms=now_ms, train=not skip_train):
                        scheduled += 1
                if learner.round_due():
                    job = learner.prepare_round(head.rl_model, now_ms=now_ms)
            if job is not None:
                task = asyncio.create_task(self._finish_round(head, job))
                self._round_tasks.add(task)
                task.add_done_callback(self._round_tasks.discard)

            resp: dict[str, Any] = {
                "ok": True, "scheduled": scheduled, "task_type": task_type,
                "round_started": job is not None,
                "serving": learner.serving,
            }
            if scheduled < len(task_ids):
                resp["skipped_events"] = len(task_ids) - scheduled
                if learner.last_skip_reason:
                    resp["skip_reason"] = learner.last_skip_reason
            if skip_train:
                resp["online_training_disabled"] = True
            return web.json_response(resp)

        except Exception as exc:
            logger.exception("rl_update error: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # v0.2.106 — per-project online learning: rounds, persistence, status
    # ------------------------------------------------------------------

    async def _finish_round(self, head: _ProjectHead, job: Any) -> None:
        """Run one prepared round off the event loop, then apply it.

        The training + evaluation work on a private copy of the network
        (``online_learning.compute_round``), so serving is never blocked by
        a round; the result is loaded into the live model under the routing
        lock, unless the model was replaced meanwhile. Never raises.
        """
        learner = head.learner
        if learner is None:
            return
        try:
            result = await asyncio.to_thread(compute_round, job)
        except Exception as exc:  # noqa: BLE001 — a failed round never breaks serving
            logger.exception("online round failed for project_id=%s: %s", head.project_id, exc)
            async with self._routing_lock:
                learner.abandon_round()
            return
        async with self._routing_lock:
            applied = learner.apply_round(head.rl_model, result)
            if applied:
                self._save_project_head(head, model_trained=result.steps > 0)
        if applied:
            logger.info(
                "online round project_id=%s: %d steps (%d queries) in %.2fs; serving=%s (%s)",
                head.project_id, result.steps, result.trained_queries, result.runtime_s,
                learner.serving, learner.reason,
            )
            await self._push_rl_online_state(head)

    async def wait_for_rounds(self) -> None:
        """Await every round in flight, and every pending state push (tests,
        and a clean shutdown)."""
        while self._round_tasks or self._push_tasks:
            await asyncio.gather(*list(self._round_tasks), *list(self._push_tasks),
                                 return_exceptions=True)

    def _project_checkpoint_path(self, head: _ProjectHead) -> Path:
        """Where the project's trained model is saved: its current file when
        that lives in the project's own dir, else the v0.2.8 name there
        (``rl_model_<source>_<base version or "local">.pt``)."""
        assert head.project_dir is not None
        current = Path(str(head.rl_model._pt_path))
        try:
            inside = current.resolve().parent == head.project_dir.resolve()
        except OSError:
            inside = False
        if inside:
            return current
        return head.project_dir / _build_weights_filename(
            self._rl_model_tag, _model_base_version(head.rl_model),
        )

    def _save_project_head(self, head: _ProjectHead, *, model_trained: bool) -> None:
        """Persist a project after a round (caller holds the routing lock).

        A trained model is saved under the project's own dir (a model that
        was rotated in from elsewhere is retargeted there first, so online
        training never writes a global or shipped file); the learner state
        is saved beside it. Soft-fails with a log line.
        """
        learner = head.learner
        if learner is None or head.project_dir is None:
            return
        try:
            if model_trained:
                target = self._project_checkpoint_path(head)
                if Path(str(head.rl_model._pt_path)) != target:
                    head.rl_model.retarget(target)
                head.rl_model.save()
                head.state_path = target
                if self._rl._rl_model is head.rl_model:
                    self._state_path = target
                from .online_learning import model_fingerprint
                learner.model_fingerprint = model_fingerprint(head.rl_model)
            learner.save(head.project_dir)
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning("online learning: saving project_id=%s failed: %s",
                           head.project_id, exc)

    def _save_learner(self, head: _ProjectHead) -> None:
        """Save a head's learner state alone (eviction / shutdown)."""
        if head.learner is None or head.project_dir is None:
            return
        try:
            head.learner.save(head.project_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("online learning: saving learner of project_id=%s failed: %s",
                           head.project_id, exc)

    def _online_summary(self) -> dict[str, Any]:
        """Loaded projects and how many are served their model (for /health)."""
        learners = [h.learner for h in self._project_heads.values() if h.learner is not None]
        return {
            "projects_loaded": len(learners),
            "projects_serving_model": sum(1 for lr in learners if lr.serving),
            "bar": {"min_ndcg_gain": self._online_cfg.min_ndcg_gain,
                    "cited_mrr_not_below_cosine": True,
                    "judged_on": "paired-bootstrap lower bounds",
                    "confidence": self._online_cfg.confidence,
                    "holdout_queries": self._online_cfg.holdout_queries},
        }

    async def _project_status(self, request: web.Request) -> web.Response:
        """The routed project's online learning + serving gate (read-only).

        200 ``{ok, project_id, serving, reason, online: {...}}``; ``online``
        is ``null`` for a request without a project id (nothing is learned
        or served for it) or when the project's model could not be loaded.
        """
        try:
            project_id, head = await self._route_request(request)
            async with self._routing_lock:
                learner = head.learner
                status = learner.status() if learner is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.exception("project_status error: %s", exc)
            return web.json_response({"ok": False, "error": "internal_error"}, status=500)
        if status is None:
            why = ("no project id on the request (per-project models only)"
                   if head.project_id == _BASE_PROJECT_SENTINEL
                   else "the project's model could not be loaded")
            return web.json_response({"ok": True, "project_id": None
                                      if head.project_id == _BASE_PROJECT_SENTINEL
                                      else project_id, "serving": False,
                                      "reason": why, "online": None})
        return web.json_response({
            "ok": True, "project_id": project_id, "serving": status["serving"],
            "reason": status["reason"], "online": status,
            "weights": {
                "local_train_steps": head.rl_model.local_train_steps,
                "base_weights_version": head.rl_model.base_weights_version or None,
                "file": Path(str(head.rl_model._pt_path)).name,
            },
        })

    async def _push_rl_online_state(self, head: _ProjectHead) -> None:
        """UPSERT the project's ``rl_online_state`` row via vct-hub (the
        launcher's RL panel reads it). Best-effort, like the other writers."""
        learner = head.learner
        if (self._hub_client is None or learner is None
                or head.project_id == _BASE_PROJECT_SENTINEL):
            return
        st = learner.status()
        ev = st.get("last_eval") or {}
        c = st["counters"]
        fields: dict[str, Any] = {
            "embedding_source": self._active_emb_source,
            "refreshed_at": int(time.time()),
            "serving": 1 if st["serving"] else 0,
            "reason": st["reason"],
            "training_enabled": 0 if st["training"] != "on" else 1,
            "events_seen": c["events_seen"],
            "queries_trained": c["queries_trained"],
            "train_steps": c["train_steps"],
            "rounds": c["rounds"],
            "holdout_queries": st["holdout_queries"],
            "holdout_target": st["holdout_target"],
            "serving_turned_off": c["serving_turned_off"],
            "last_eval_at": int((ev.get("at_ms") or 0) // 1000),
            "ndcg_gain": ev.get("ndcg_gain"),
            "ndcg": ev.get("ndcg"),
            "cos_ndcg": ev.get("cos_ndcg"),
            "mrr": ev.get("mrr"),
            "cos_mrr": ev.get("cos_mrr"),
            "cited_queries": ev.get("n_cited"),
            # migration 0008: the bounds the serving decision used (owner ruling
            # 2026-11-12; None = undefined, a failing bound)
            "ndcg_gain_lb": ev.get("ndcg_gain_lb"),
            "mrr_diff_lb": ev.get("mrr_diff_lb"),
            "gate_confidence": ev.get("confidence"),
        }
        try:
            # Single-column key (the hub resolves ONE primary-key column):
            # one row per project, for the profile this server is pinned to.
            await self._hub_client.upsert_row(
                head.project_id, "rl_online_state", head.project_id, fields,
            )
        except Exception as exc:  # noqa: BLE001 — the dashboard tolerates stale rows
            logger.debug("rl_online_state push failed: %s", exc)

    # ------------------------------------------------------------------
    # v0.1.1 HTTP handlers — model management
    # ------------------------------------------------------------------

    async def _rotate_weights(self, request: web.Request) -> web.Response:
        """Hot-swap the in-memory RLModel to a different ``.pt`` checkpoint.

        Body JSON:
          weights_path: str  — absolute container-side path under /data/state/
                                or /data/global/, ending in .pt

        Returns:
          200 {"ok": True, "loaded": "<path>", "emb_dim": <int>}
          400 when path is missing / outside the safe roots / not a file
          500 when torch fails to load the checkpoint

        D1' rotation invariant (v0.2.2+):
          This endpoint replaces only the NETWORK .pt at the given path.
          The sidecar directory ``<weights_path.parent>/type_embs/`` and
          the marker file ``<weights_path.parent>/.types_layout_v2`` are
          NOT touched by this handler — they belong to the user's local
          dynamic-type state, which must persist across a weights rotation.
          ``RLModel.load_or_create`` resolves sidecars from the same
          directory as the .pt, so the swap below picks up the user's
          existing dynamic types automatically. Builtin rows 0..8 inside
          ``type_embs.weight`` are intentionally REPLACED by the new
          shipped checkpoint (that's the whole point of a rotation —
          let us refine builtin embeddings over releases).

          The launcher's rotation flow writes the new global checkpoint
          to the same state-dir path it then calls this endpoint with,
          which is an atomic single-file copy — the sibling sidecar dir
          is left untouched on disk.
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"invalid JSON: {exc}"}, status=400
            )

        raw_path = body.get("weights_path", "")
        safe = _safe_weights_path(raw_path)
        if safe is None:
            return web.json_response(
                {"ok": False, "error": (
                    "weights_path must be an absolute path under /data/state/ "
                    "or /data/global/"
                )},
                status=400,
            )
        if not safe.exists() or not safe.is_file():
            return web.json_response(
                {"ok": False, "error": f"weights file not found: {safe}"},
                status=400,
            )

        # Reload via RLModel.load_or_create against the requested path.
        # We use the model's existing emb_dim — if the checkpoint was
        # trained on a different embedding source, load_or_create logs a
        # mismatch warning and reinitialises fresh weights. The 200 below
        # still fires (we successfully replaced the in-memory model);
        # callers who care about exact provenance should track which
        # weights file they uploaded.
        try:
            new_model = RLModel.load_or_create(
                state_path=safe,
                emb_dim=self._rl_emb_dim,
                arch_version=self._rl_arch_version,
                device=self._rl_device_spec,
                embedding_source=self._active_emb_source,  # R4 cross-source guard
            )
        except Exception as exc:
            logger.exception("rotate_weights: load failed for %s", safe)
            return web.json_response(
                {"ok": False, "error": f"load failed: {exc}"}, status=500
            )

        # v0.2.10 — route swap to the per-project head so we mutate
        # the right model + state_path. Lock held for atomicity vs
        # concurrent /cache_nodes or /rl_update on the same head.
        project_id, head = await self._route_request(request)
        async with self._routing_lock:
            self._swap_in_head(head)
            # Atomic swap on the RetrievalRL instance. The old RLModel goes
            # out of scope and is GC'd; we don't .save() it because the
            # caller is replacing it deliberately.
            self._rl._rl_model = new_model
            # v0.2.8 — track the new active path so _push_rl_weights_state
            # reports the rotated-in file, not the original startup file.
            self._state_path = safe
            logger.info(
                "rotate_weights: swapped in %s (emb_dim=%d, project_id=%s)",
                safe, self._rl_emb_dim, project_id,
            )
            # v0.2.6 — refresh hub state cache + bump last_rotated_at.
            # v0.2.8 — also bump rl_global_weights_state when rotating to
            # a global slot (any .pt under /data/global/).
            await self._push_rl_state()
            await self._push_rl_weights_state(last_rotated=True)
            if str(safe).startswith("/data/global/"):
                await self._push_rl_global_weights_state(last_rotated=True)
            self._capture_head_state(head)
        return web.json_response(
            {"ok": True, "loaded": str(safe), "emb_dim": self._rl_emb_dim}
        )

    async def _reset(self, request: web.Request) -> web.Response:
        """Reset the project model from the global or the bundled checkpoint.

        Body JSON:
          strategy: "fork" | "fork_and_specialize" | "bundled"
          days:     int   — events lookback for "fork_and_specialize" (default 30)

        Behaviour:
          bundled:             copy the checkpoint the image ships for the
                                active profile → project state, reload model,
                                return (the "use the shipped default weights"
                                action; 404 when no trained model ships).
          fork:                copy global → project state, reload model, return.
          fork_and_specialize: do fork; then kick off background offline training
                                on the project's events log → returns a job_id
                                that the launcher polls via /finetune_status.

        Returns:
          200 {"ok": True, "strategy": "fork", "weights_path": "<path>", "job_id": null}
          200 {"ok": True, "strategy": "fork_and_specialize", "weights_path": "...", "job_id": "<uuid>"}
          400 on bad/missing strategy
          404 when the global (or, for "bundled", the shipped) weights file is
              not present
          500 on copy failure
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"invalid JSON: {exc}"}, status=400
            )

        strategy = body.get("strategy", "")
        if strategy not in ("fork", "fork_and_specialize", "bundled"):
            return web.json_response(
                {
                    "ok": False,
                    "error": "strategy must be 'fork', 'fork_and_specialize' or 'bundled'",
                },
                status=400,
            )
        try:
            days = int(body.get("days", 30))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "days must be an integer"}, status=400
            )
        if days <= 0 or days > 3650:
            return web.json_response(
                {"ok": False, "error": "days must be 1..3650"}, status=400
            )

        # Resolve the source checkpoint. "bundled" = the one the image ships
        # for this profile; otherwise the most-recent global checkpoint
        # matching this source (versioned filenames, sorted glob).
        if strategy == "bundled":
            if self._bundled_weights is None:
                return web.json_response(
                    {
                        "ok": False,
                        "error": (
                            f"not trained: no bundled weights ship for "
                            f"profile {self._rl_model_tag!r}"
                        ),
                    },
                    status=404,
                )
            global_candidates = [self._bundled_weights.path]
        else:
            global_candidates = _glob_weights_files(
                self._global_weights_dir, self._rl_model_tag
            )
        if not global_candidates:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        f"no global weights for source={self._rl_model_tag!r} "
                        f"in {self._global_weights_dir}. Set RL_GLOBAL_WEIGHTS_DIR "
                        f"or place a rl_model_{self._rl_model_tag}_<version>.pt "
                        f"file before /reset."
                    ),
                },
                status=404,
            )
        global_path = global_candidates[-1]  # latest by sorted glob

        # v0.2.10 — route to the per-project head so the fork lands in the
        # right per-project state subdir. We resolve project_id BEFORE the
        # copy so the file system path is project-correct from the start.
        project_id, head = await self._route_request(request)
        project_state_dir = _per_project_state_dir(self._state_dir, project_id)
        project_state_dir.mkdir(parents=True, exist_ok=True)
        # Project-side fork keeps the same filename to preserve provenance.
        project_path = project_state_dir / global_path.name

        # Atomic, metadata-preserving copy through vco_lib.atomic (the one
        # home of the tmp+rename write; the image COPIES it).
        # Same-filesystem rename is atomic on POSIX; copy2 preserves
        # mode + mtime which makes weight provenance trivially auditable
        # (`stat` on the file gives the global-side timestamp).
        try:
            project_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(global_path, project_path)
        except Exception as exc:
            logger.exception("reset: copy %s -> %s failed", global_path, project_path)
            return web.json_response(
                {"ok": False, "error": f"copy failed: {exc}"}, status=500
            )

        # Reload the in-process model so subsequent /cache_nodes calls
        # use the fresh fork. We don't bother holding the old reference;
        # GC will reap it. Failures here are surfaced as 500 because
        # the file was copied but we couldn't load it — caller should
        # know the on-disk state is now ahead of the in-memory state.
        try:
            new_model = RLModel.load_or_create(
                state_path=project_path,
                emb_dim=self._rl_emb_dim,
                arch_version=self._rl_arch_version,
                device=self._rl_device_spec,
                embedding_source=self._active_emb_source,  # R4 cross-source guard
            )
        except Exception as exc:
            logger.exception("reset: reload failed after copy")
            return web.json_response(
                {"ok": False, "error": f"reload failed: {exc}"}, status=500
            )

        # v0.2.10 — swap the new model into the routed head atomically.
        async with self._routing_lock:
            self._swap_in_head(head)
            self._rl._rl_model = new_model
            # v0.2.8 — track the new active path.
            self._state_path = project_path

            if strategy in ("fork", "bundled"):
                logger.info(
                    "reset: %s → %s (no specialize, project_id=%s)",
                    "seeded bundled" if strategy == "bundled" else "forked global",
                    project_path, project_id,
                )
                # v0.2.6 — refresh hub state cache + bump last_rotated_at.
                await self._push_rl_state()
                await self._push_rl_weights_state(last_rotated=True)
                self._capture_head_state(head)
                return web.json_response(
                    {
                        "ok": True,
                        "strategy": strategy,
                        "weights_path": str(project_path),
                        "job_id": None,
                    }
                )

            # fork_and_specialize: kick off an async fine-tune job for this
            # project. v0.2.11 — DB-only: the job reads this project's events
            # from launcher.db via the hub (keyed by project_id), no JSONL.
            job_id = uuid.uuid4().hex
            job = JobState(
                job_id=job_id,
                kind="specialize",
                state="running",
                percent=0,
                message="Starting specialize…",
                started_at=_now_iso(),
            )
            async with self._jobs_lock:
                self._jobs[job_id] = job

            asyncio.create_task(
                self._run_finetune_job(
                    job_id=job_id,
                    project_id=project_id,
                    base_weights_path=project_path,
                    days=days,
                    kind="specialize",
                )
            )
            logger.info(
                "reset: fork+specialize scheduled job=%s base=%s days=%d "
                "project_id=%s (DB-only)",
                job_id, project_path, days, project_id,
            )
            # v0.2.6 — fork-side rotation already happened above; push it now.
            # last_finetuned_at gets stamped later when the bg job lands at "done".
            await self._push_rl_state()
            await self._push_rl_weights_state(last_rotated=True)
            self._capture_head_state(head)
        return web.json_response(
            {
                "ok": True,
                "strategy": "fork_and_specialize",
                "weights_path": str(project_path),
                "job_id": job_id,
            }
        )

    async def _finetune(self, request: web.Request) -> web.Response:
        """Start an async offline fine-tune job.

        v0.2.11 — DB-only. The project is resolved from the
        ``X-VCT-Project-ID`` header (same routing as every other request);
        events come from launcher.db via the hub. ``events_path`` in the body
        is IGNORED (back-compat — older launchers still send it); only
        ``base_weights_path`` + ``days`` are honoured.

        Body JSON:
          base_weights_path: str — absolute path under /data/state/ or /data/global/
          days:              int — lookback in days (default 30)
          events_path:       str — IGNORED (DB-only; retired in v0.2.11)

        Returns 202 with ``{"job_id": "<uuid>"}``. Caller polls via
        ``GET /finetune_status?job_id=<uuid>``.
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"invalid JSON: {exc}"}, status=400
            )

        base_raw = body.get("base_weights_path", "")
        base_path = _safe_weights_path(base_raw)

        if base_path is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "base_weights_path must be absolute and under "
                        "/data/state/ or /data/global/"
                    ),
                },
                status=400,
            )
        if not base_path.exists():
            return web.json_response(
                {"ok": False, "error": f"base_weights_path not found: {base_path}"},
                status=400,
            )

        try:
            days = int(body.get("days", 30))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "days must be an integer"}, status=400
            )
        if days <= 0 or days > 3650:
            return web.json_response(
                {"ok": False, "error": "days must be 1..3650"}, status=400
            )

        # v0.2.10 — resolve the routed project_id (X-VCT-Project-ID header) so
        # both the hub event read AND the post-job hub-state write target the
        # requesting project. v0.2.11 — this project_id is now ALSO the training
        # data key (DB-only). A '_base'/no-header finetune has no real project
        # whose DB events we could train on → reject with a clear 400.
        project_id, head = await self._route_request(request)
        if project_id == _BASE_PROJECT_SENTINEL:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "finetune requires a project (send the X-VCT-Project-ID "
                        "header). The base model can't be fine-tuned: training "
                        "data is keyed by project_id in launcher.db."
                    ),
                },
                status=400,
            )
        async with self._routing_lock:
            self._swap_in_head(head)
            job_id = uuid.uuid4().hex
            job = JobState(
                job_id=job_id,
                kind="finetune",
                state="running",
                percent=0,
                message="Starting fine-tune…",
                started_at=_now_iso(),
            )
            async with self._jobs_lock:
                self._jobs[job_id] = job

            asyncio.create_task(
                self._run_finetune_job(
                    job_id=job_id,
                    project_id=project_id,
                    base_weights_path=base_path,
                    days=days,
                    kind="finetune",
                )
            )
            logger.info(
                "finetune: scheduled job=%s base=%s days=%d project_id=%s (DB-only)",
                job_id, base_path, days, project_id,
            )
            self._capture_head_state(head)
        return web.json_response({"job_id": job_id}, status=202)

    async def _finetune_status(self, request: web.Request) -> web.Response:
        """Return the in-process state of a previously-scheduled job.

        Query string:
          job_id=<uuid>

        Wire format (matches launcher's ``run_finetune_then_rotate``):
          200 {"state": "running", "percent": 42, "message": "..."}
          200 {"state": "done",    "percent": 100, "message": "...",
                "result_weights_path": "..."}
          200 {"state": "failed",  "reason": "..."}
          404 {"error": "unknown job_id"}
        """
        job_id = request.query.get("job_id", "")
        if not job_id:
            return web.json_response({"error": "missing job_id"}, status=400)

        async with self._jobs_lock:
            job = self._jobs.get(job_id)

        if job is None:
            return web.json_response({"error": "unknown job_id"}, status=404)

        if job.state == "failed":
            return web.json_response(
                {"state": "failed", "reason": job.error or job.message or "unknown error"}
            )
        payload: dict[str, Any] = {
            "state": job.state,
            "percent": job.percent,
            "message": job.message,
        }
        if job.state == "done" and job.result_weights_path:
            payload["result_weights_path"] = job.result_weights_path
        return web.json_response(payload)

    async def _global_retrain(self, request: web.Request) -> web.Response:
        """Train the global RLModel from multiple projects' event logs.

        Body JSON (v0.2.11 — DB-only):
          mode:         "online" | "offline"
          project_ids:  list[str]  — REQUIRED; the projects whose launcher.db
                                     events are aggregated for the global model
          days:         int        — lookback (default 30)
          events_paths: list[str]  — IGNORED (DB-only; retired in v0.2.11)

        online:  replay each project's events through current global weights
        offline: train on the union of all projects' events

        Either way, the result is written back to the global weights path
        (under RL_GLOBAL_WEIGHTS_DIR). Returns 202 + job_id; poll via
        /finetune_status.
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"invalid JSON: {exc}"}, status=400
            )

        mode = body.get("mode", "")
        if mode not in ("online", "offline"):
            return web.json_response(
                {"ok": False, "error": "mode must be 'online' or 'offline'"},
                status=400,
            )
        project_ids = body.get("project_ids") or []
        if not isinstance(project_ids, list):
            return web.json_response(
                {"ok": False, "error": "project_ids must be an array"},
                status=400,
            )
        # v0.2.11 — project_ids is now the DATA DRIVER (events come from the DB
        # keyed by project_id), not just an audit log. Must be non-empty, and
        # no '_base' sentinel (the base model has no project events to train on).
        project_ids = [str(p) for p in project_ids if p and str(p) != _BASE_PROJECT_SENTINEL]
        if not project_ids:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "project_ids must be a non-empty array of real project "
                        "ids (DB-only; '_base' and empty are rejected)."
                    ),
                },
                status=400,
            )

        # v0.2.7 — accept either ``days`` (back-compat) OR ``earliest_date``
        # (ISO date string YYYY-MM-DD or empty/null for "no cutoff").
        # earliest_date takes precedence when both are present.
        # Empty / null earliest_date means "no cutoff — use ALL history".
        earliest_date_raw = body.get("earliest_date")
        if earliest_date_raw not in (None, ""):
            # Parse ISO date. We don't enforce timezone (treat as UTC).
            try:
                from datetime import datetime as _dt
                # Accept both "2026-05-24" and "2026-05-24T00:00:00".
                if "T" in str(earliest_date_raw):
                    parsed = _dt.fromisoformat(str(earliest_date_raw).replace("Z", "+00:00"))
                else:
                    parsed = _dt.fromisoformat(str(earliest_date_raw) + "T00:00:00+00:00")
                # Convert to days-since-now for compatibility with _run_finetune_job's
                # existing days-based filter. Negative/future dates → 0 (use ALL).
                now = _dt.now(tz=parsed.tzinfo) if parsed.tzinfo else _dt.now()
                delta_seconds = (now - parsed).total_seconds()
                if delta_seconds < 0:
                    # Future date → no events match anyway. Caller mistake; train
                    # on nothing rather than 400-erroring (the trainer's
                    # "no events matched" path already handles this gracefully).
                    days = 0
                else:
                    days = max(1, int(delta_seconds / 86400) + 1)
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"ok": False, "error": f"earliest_date must be ISO YYYY-MM-DD; got {earliest_date_raw!r} ({exc})"},
                    status=400,
                )
        else:
            try:
                days = int(body.get("days", 30))
            except (TypeError, ValueError):
                return web.json_response(
                    {"ok": False, "error": "days must be an integer"}, status=400
                )

        # Global weights path is per-source. /global/retrain reads from
        # and writes to the SAME file; concurrent retrain runs would
        # clobber each other but the launcher serializes them and we
        # don't attempt locking inside the container.
        #
        # v0.2.8: prefer the most-recent existing global checkpoint for
        # this source (versioned filename per launcher R3); fall back to
        # the local-fork sentinel name when no global has shipped yet
        # (first-ever retrain — _run_global_retrain_job will seed from
        # the project's weights).
        global_candidates = _glob_weights_files(
            self._global_weights_dir, self._rl_model_tag
        )
        if global_candidates:
            global_path = global_candidates[-1]
        else:
            global_path = self._global_weights_dir / _build_weights_filename(
                self._rl_model_tag, ""
            )

        # v0.2.10 — resolve routed project_id for hub-write context. The
        # global retrain writes to /data/global/, which is project-agnostic,
        # but the kicking-off project's hub row should still be updated
        # post-completion. We DON'T swap the model (global retrain operates
        # on the global slot, NOT on a per-project head).
        request_project_id, _request_head = await self._route_request(request)
        async with self._routing_lock:
            # Set self._project_id so the bg job's _push_rl_weights_state
            # post-done points at the requesting project. The model itself
            # isn't swapped — global retrain mutates /data/global/ directly.
            if request_project_id == _BASE_PROJECT_SENTINEL:
                self._project_id = os.environ.get("VCT_PROJECT_ID", "")
            else:
                self._project_id = request_project_id

            job_id = uuid.uuid4().hex
            kind = "global_online" if mode == "online" else "global_offline"
            job = JobState(
                job_id=job_id,
                kind=kind,
                state="running",
                percent=0,
                message=f"Starting global {mode} retrain ({len(project_ids)} projects)…",
                started_at=_now_iso(),
            )
            async with self._jobs_lock:
                self._jobs[job_id] = job

            asyncio.create_task(
                self._run_global_retrain_job(
                    job_id=job_id,
                    mode=mode,
                    global_path=global_path,
                    days=days,
                    project_ids=project_ids,
                    seed_project_id=request_project_id,
                )
            )
            logger.info(
                "global/retrain: scheduled job=%s mode=%s projects=%d days=%d "
                "requested_by_project=%s (DB-only)",
                job_id, mode, len(project_ids), days, request_project_id,
            )
        return web.json_response({"job_id": job_id}, status=202)

    # ------------------------------------------------------------------
    # Background job implementations
    # ------------------------------------------------------------------

    async def _update_job(self, job_id: str, **fields: Any) -> None:
        """Patch a job-state record under the registry lock.

        v0.2.6 — when state transitions to "done", push last_finetuned_at to
        the hub's rl_weights_state row. Soft-fails; never blocks the job state
        machine.
        """
        terminal_done = fields.get("state") == "done"
        async with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
        if terminal_done:
            try:
                await self._push_rl_weights_state(last_finetuned=True)
            except Exception as exc:
                logger.debug("_update_job: post-done hub write soft-failed: %s", exc)

    async def _run_finetune_job(
        self,
        job_id: str,
        project_id: str,
        base_weights_path: Path,
        days: int,
        kind: str,
    ) -> None:
        """Background offline training job for /finetune and /reset's
        fork_and_specialize path.

        v0.2.11 — DB-only. Events come from launcher.db via the module-scoped
        hub route (``self._hub_client.list_rl_events``), filtered to
        ``project_id`` over the last ``days``; the per-embedding-source filter
        is applied by offline_trainer.train. No JSONL file, no tmp staging.

        Pipeline:
          1. Fetch this project's events from the hub (since_ms = now − days).
          2. Seed the result file from base_weights_path (resume-from).
          3. Call offline_trainer.train (hub_client + project_id) → it loads,
             pairs, turns the pairs into per-query samples (the unified
             targets) and fits them with the ONE objective
             (``objective.batch_loss`` via ``online_learning.fit``, v0.2.106),
             keeping the best epoch on a newest-queries validation split, and
             writes back to result_path.
          4. Result lands at ``<state>/projects/<project_id>/<base stem>_finetuned.pt``
             — a deliberate /rotate_weights is required before it goes live.
        """
        try:
            from .offline_trainer import train as _train

            if self._hub_client is None:
                raise RuntimeError(
                    "finetune requires the hub (launcher.db is the source of "
                    "truth); no _HubClient available on this server"
                )

            # v0.2.106: the result lives in THIS project's own dir
            # (``projects/<project_id>/``, beside its online checkpoint), so
            # two projects' jobs can never write the same file and a project's
            # job never writes the shared base dir. Named after the base it
            # resumes from; the caller must explicitly /rotate_weights to make
            # it live. (Before: ``<state>/<active stem>_finetuned.pt``, read
            # from whichever head was swapped in when the job got to run.)
            project_dir = _per_project_state_dir(self._state_dir, project_id)
            project_dir.mkdir(parents=True, exist_ok=True)
            stem = base_weights_path.stem.removesuffix("_finetuned")
            result_path = project_dir / f"{stem}_finetuned.pt"
            # Seed the result file FROM the base so train() resumes from the
            # right checkpoint (it reads state_path via RLModel.load_or_create,
            # then writes back to the same path).
            shutil.copy2(base_weights_path, result_path)

            since_ms = int(time.time() * 1000) - days * 86_400_000

            await self._update_job(
                job_id, percent=15,
                message=f"Training from DB (project={project_id}, last {days}d)…",
            )

            # offline_trainer.train is synchronous (CPU-bound) — run it in an
            # executor so we don't block the event loop. It reads events from
            # the hub itself (DB-only), applies the per-embedding-source filter,
            # and re-AdamW's internally (correct for resume-from-a-checkpoint).
            # A no-pairs corpus is a benign no-op inside train() (logs + returns);
            # result_path stays equal to the base (= unchanged weights).
            await self._update_job(job_id, percent=30, message="Running training…")
            result = await asyncio.to_thread(
                _train,
                model_state_path=result_path,
                hub_client=self._hub_client,
                project_id=project_id,
                epochs=3,
                filter_embedding_source=self._active_emb_source,
                profile=self._rl_model_tag,
                emb_dim=self._rl_emb_dim,
                arch_version=self._rl_arch_version,
                device=self._rl_device_spec,
                since_ms=since_ms,
            )

            # v0.2.11 — distinguish TRAINED from NO-DATA. A hub that returns
            # zero events (empty corpus, OR a 401/403 auth failure that
            # soft-failed list_rl_events → []) must NOT report a false
            # "Fine-tune complete": that would tell the launcher/user the model
            # improved when it didn't (audit CONCERN-1). train() returns
            # {trained: bool, ...}; we surface "no events" as a distinct
            # terminal message and DON'T stamp last_finetuned_at on a no-op.
            trained = bool(result.get("trained")) if isinstance(result, dict) else False
            if not trained:
                reason = result.get("reason", "no_data") if isinstance(result, dict) else "no_data"
                n_events = result.get("n_events", 0) if isinstance(result, dict) else 0
                await self._update_job(
                    job_id,
                    state="done",
                    percent=100,
                    message=(
                        f"No training performed: {reason} "
                        f"({n_events} events from the hub for project={project_id} "
                        f"in the last {days}d). Weights unchanged."
                    ),
                    result_weights_path=str(base_weights_path),
                )
                logger.info(
                    "finetune job=%s no-op: reason=%s n_events=%s (kind=%s)",
                    job_id, reason, n_events, kind,
                )
                return

            # v0.2.8 — stash the artifact path so _push_rl_weights_state
            # (fired by _update_job on terminal=done) writes it to the hub.
            self._last_finetuned_path = result_path
            await self._update_job(
                job_id,
                state="done",
                percent=100,
                message=(
                    f"Fine-tune complete ({result.get('n_pairs', '?')} pairs, "
                    f"{result.get('n_queries', '?')} queries, "
                    f"{result.get('epochs', '?')} epochs)"
                ),
                result_weights_path=str(result_path),
            )
            logger.info(
                "finetune job=%s done: result=%s pairs=%s (kind=%s)",
                job_id, result_path, result.get("n_pairs"), kind,
            )

        except Exception as exc:
            logger.exception("finetune job=%s failed", job_id)
            await self._update_job(
                job_id,
                state="failed",
                message=f"Fine-tune failed: {exc}",
                error=str(exc),
            )

    async def _run_global_retrain_job(
        self,
        job_id: str,
        mode: str,
        global_path: Path,
        days: int,
        project_ids: list[str],
        seed_project_id: str = "",
    ) -> None:
        """Background global-retrain job (v0.2.11 — DB-only).

        ``seed_project_id``: the requesting project; its newest checkpoint
        seeds a first-ever global model (``_global_retrain_seed``).

        Trains ONE global model on the union of several projects' events,
        read from launcher.db via the hub (keyed by project_id). No JSONL.

        ``mode == "offline"``: aggregate all projects' paired events, then
                                three shuffled epochs over the union,
                                against global_path as resume-from.
        ``mode == "online"``:  per project, one pass over its pairs in time
                                order, one project at a time, saved back to
                                global_path. Faster; useful for recent,
                                small-volume events.

        Both fit the ONE training objective (v0.2.106:
        ``offline_trainer.queries_from_pairs`` -> ``online_learning.fit`` ->
        ``objective.batch_loss``; the per-node BCE replay ``train_epoch`` it
        used before is gone).
        """
        try:
            from .offline_trainer import (
                pair_events as _pair_events,
                queries_from_pairs as _queries_from_pairs,
                load_training_events as _load_training_events,
            )
            from .online_learning import VectorStore as _VectorStore, fit as _fit

            if self._hub_client is None:
                raise RuntimeError(
                    "global/retrain requires the hub (launcher.db is the "
                    "source of truth); no _HubClient available"
                )

            global_path.parent.mkdir(parents=True, exist_ok=True)

            if not global_path.exists():
                # First-ever global retrain: seed from the requesting
                # project's weights (else the base model's) so the model
                # resumes from a good checkpoint rather than random init.
                project_seed = _global_retrain_seed(
                    self._state_dir, seed_project_id, self._rl_model_tag,
                )
                if project_seed is not None:
                    shutil.copy2(project_seed, global_path)
                    logger.info(
                        "global/retrain: seeded global from project weights %s",
                        project_seed,
                    )

            since_ms = int(time.time() * 1000) - days * 86_400_000

            await self._update_job(
                job_id,
                percent=10,
                message=f"Reading {len(project_ids)} project(s) from DB…",
            )

            def _fetch_pairs(pid: str) -> "list[tuple[dict, dict]]":
                """Fetch one project's events from the hub → paired."""
                evs = _load_training_events(
                    self._hub_client,
                    pid,
                    filter_embedding_source=self._active_emb_source,
                    since_ms=since_ms,
                )
                return _pair_events(evs)

            # Load the global model once (both modes resume-from / write-to it).
            await self._update_job(job_id, percent=15, message="Loading global model…")
            gm = RLModel.load_or_create(
                state_path=global_path,
                emb_dim=self._rl_emb_dim,
                arch_version=self._rl_arch_version,
                device=self._rl_device_spec,
                embedding_source=self._active_emb_source,  # R4 cross-source guard
            )

            total_pairs = 0
            cfg = self._online_cfg

            def _train(pairs: "list[tuple[dict, dict]]", *, epochs: int, shuffle: bool) -> int:
                store = _VectorStore(self._rl_emb_dim)
                queries = _queries_from_pairs(pairs, store, profile=self._rl_model_tag)
                return _fit(gm, queries, store, profile=self._rl_model_tag, epochs=epochs,
                            batch_queries=cfg.batch_queries, lr=cfg.lr, seed=cfg.seed,
                            shuffle=shuffle, loss=cfg.loss)

            if mode == "offline":
                # Aggregate every project's pairs, then run a few epochs over
                # the union, same target math as the per-project path.
                all_pairs: "list[tuple[dict, dict]]" = []
                for idx, pid in enumerate(project_ids):
                    await self._update_job(
                        job_id,
                        percent=20 + int(20 * idx / max(1, len(project_ids))),
                        message=f"Reading project {idx+1}/{len(project_ids)}…",
                    )
                    all_pairs.extend(await asyncio.to_thread(_fetch_pairs, pid))
                total_pairs = len(all_pairs)
                await self._update_job(
                    job_id, percent=40,
                    message=f"Training on {total_pairs} pairs ({len(project_ids)} projects)…",
                )
                # 3 epochs over the aggregated corpus.
                await asyncio.to_thread(lambda: _train(all_pairs, epochs=3, shuffle=True))
            else:  # mode == "online"
                for idx, pid in enumerate(project_ids):
                    await self._update_job(
                        job_id,
                        percent=20 + int(70 * idx / max(1, len(project_ids))),
                        message=f"Replaying project {idx+1}/{len(project_ids)}…",
                    )
                    pairs = await asyncio.to_thread(_fetch_pairs, pid)
                    total_pairs += len(pairs)
                    pairs.sort(key=lambda pr: int(pr[0].get("ts_ms") or 0))
                    await asyncio.to_thread(
                        lambda p=pairs: _train(p, epochs=1, shuffle=False))

            await asyncio.to_thread(gm.save)
            logger.info(
                "global/retrain %s: trained %d pairs across %d projects → %s",
                mode, total_pairs, len(project_ids), global_path,
            )

            await self._update_job(
                job_id,
                state="done",
                percent=100,
                message=f"Global {mode} retrain complete ({len(project_ids)} projects, {total_pairs} pairs)",
                result_weights_path=str(global_path),
            )
            logger.info(
                "global/retrain job=%s done: %s (mode=%s, project_ids=%s)",
                job_id, global_path, mode, project_ids,
            )

        except Exception as exc:
            logger.exception("global/retrain job=%s failed", job_id)
            await self._update_job(
                job_id,
                state="failed",
                message=f"Global retrain failed: {exc}",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._port)
        await site.start()
        logger.info("RLServer listening on http://%s:%d", self._bind_host, self._port)

        # v0.2.6 — push initial rl_state + rl_weights_state to vct-hub so the
        # launcher's dashboard widget reflects "model loaded" immediately after
        # the container binds its port. Soft-fails when hub is unreachable.
        await self._push_rl_state()
        await self._push_rl_weights_state(last_rotated=True)

        # Graceful shutdown on SIGINT / SIGTERM
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        logger.info("RLServer shutting down…")
        # v0.2.106: let running online rounds land, then save every
        # project's learner so its queued events survive the restart.
        await self.wait_for_rounds()
        async with self._routing_lock:
            for h in list(self._project_heads.values()):
                self._save_learner(h)
        if self._hub_client is not None:
            await self._hub_client.close()
        await runner.cleanup()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Standalone HTTP server for RetrievalRL (MCP training path)"
    )
    p.add_argument("--port", type=int, default=int(os.getenv("RL_PORT", _DEFAULT_PORT)))
    p.add_argument(
        "--bind",
        dest="bind_host",
        default=os.getenv("RL_BIND_HOST", "127.0.0.1"),
        help="Bind address (default 127.0.0.1; container deployments use 0.0.0.0)",
    )
    p.add_argument("--project-root", type=Path, default=Path(os.getenv("RL_PROJECT_ROOT", ".")))
    # No baked-in default path: an unset --log-path / RL_LOG_PATH resolves in
    # main() through rl_client.rl_logger.default_rl_log_path() (the one home,
    # under the VCO state dir). The image always sets RL_LOG_PATH, so it never
    # needs vco_lib.paths, which it does not carry.
    p.add_argument("--log-path", default=os.getenv("RL_LOG_PATH") or None)
    p.add_argument("--reranker-model", default=os.getenv("RL_RERANKER_MODEL", None))
    p.add_argument(
        "--weights-dir",
        default=os.getenv("RL_WEIGHTS_DIR", None),
        help="Bundled-weights root (manifest.json + <profile>/<version>/...; "
             "see bundled_weights.py). The checkpoint shipped for the active "
             "profile seeds state/ on first start; none shipped = not trained.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # vco_lib.log_setup is the one home of root-logger setup (VCO_LOG_LEVEL
    # honoured; -v makes DEBUG the default). The image COPIES it.
    configure_logging(
        logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    project_root = args.project_root.resolve()
    logger.info(
        "RLServer starting — project_root=%s bind=%s port=%d",
        project_root, args.bind_host, args.port,
    )

    # The bundled-weights root is resolved inside RLServer (it needs the
    # active profile); the checkpoint shipped for that profile seeds state/
    # on first start and never overwrites an existing checkpoint.
    server = RLServer(
        project_root=project_root,
        log_path=str(args.log_path or default_rl_log_path()),
        reranker_model=args.reranker_model,
        port=args.port,
        bind_host=args.bind_host,
        weights_root=Path(args.weights_dir) if args.weights_dir else None,
    )
    asyncio.run(server.run())


def _seed_bundled_weights(bundled: "BundledWeights | None", state_dir: Path) -> bool:
    """Copy the shipped checkpoint into ``state_dir`` on first start.

    Never overwrites: a checkpoint already present under the same name (an
    earlier seed, or a user's own rotation to that version) is kept. Returns
    True when a file was copied. ``bundled is None`` (no trained model ships
    for the active profile) seeds nothing: the server then runs fresh
    weights and reports itself as not trained.
    """
    if bundled is None:
        logger.info("Weights seed: no bundled checkpoint for this profile (not trained)")
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    dest = state_dir / _build_weights_filename(bundled.profile, bundled.version)
    if dest.exists():
        logger.info("Weights seed: %s already present, skipping", dest.name)
        return False
    atomic_copy_file(bundled.path, dest)
    logger.info("Weights seed: %s -> %s", bundled.path, dest)
    return True


if __name__ == "__main__":
    main()
