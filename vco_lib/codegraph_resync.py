# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Revision-gated code-graph resync trigger (v0.2.72 P7).

Background
----------
P3 (model-aware chunking, ``templates/scripts/analyze_code_graph.py``) changed
how over-budget Function/Class entities are embedded: instead of truncating the
tail, they are now split into N chunks (``chunk_num``/``total_chunks``). That
invalidates the existing single-object embeddings of the ~7-9% of entities that
were over budget — their on-disk body text is unchanged, so the analyzer's
per-object content-hash tombstone-skip would skip them forever and they'd never
gain their chunks.

The analyzer stamps a per-object ``embed_revision`` property equal to
``CODEGRAPH_EMBED_REVISION``. On the next analyze it FORCES a re-embed of any
row whose stored ``embed_revision`` differs (or is NULL) — bypassing the
content-hash skip for exactly the stale rows, leaving everything already at the
current revision untouched.

This module is the *trigger* side: it kicks off a BACKGROUND, per-project,
resumable re-analyze so the revision-gated resync actually runs after an
``install.py --update`` — WITHOUT blocking the update. The heavy lifting (the
gate itself) lives in the analyzer; this module only decides *whether* and *how*
to launch it, and degrades gracefully when the code-embed service is down.

Design invariants (project rules)
---------------------------------
* **No global/process timeout.** A slow machine must be able to finish. The
  analyzer's per-embed-request guard (``VCT_EMBED_REQUEST_TIMEOUT_SECS``) is the
  correct granularity for catching a wedged embedder; we never impose a
  wall-clock deadline on the whole resync.
* **Background + non-blocking.** We ``Popen`` the analyzer detached and return
  immediately. The caller (``install.py --update``) does not wait on it.
* **Degrade, don't fail.** If the code-embed service (:11440) is unreachable,
  we DO NOT launch (a re-embed would fail per-object) — instead we return a
  ``deferred`` status and hand the caller a :class:`DeferralEntry` so the user
  is told to re-run once the service is up. The update itself still succeeds.
* **Resumable + idempotent.** Because the gate is per-object, an interrupted
  resync continues on the next run; re-running after completion is a cheap
  no-op (every row already at the current revision → all skip). v0.2.73:
  "the next run" is now guaranteed to exist — the R-6 owed-probe re-triggers
  on every ``--update`` while stale rows remain, and the R-7 driver verifies
  convergence post-walk, recording a ONE-TIME ``UPDATE_DEFERRED.md`` entry
  when rows stay stale (pre-fix, a walk that died mid-run left no record
  anywhere — runtime-proven on 2026-07-02).
* **Surface, never silence.** Soft-fail always leaves a signal: children log
  to ``<vct_root_dir>/logs/`` (R-5), unverified convergence defers (R-7),
  and the pending ledger entry survives foreign writers' rebuilds (A-2) —
  it clears ONLY on a positive zero-stale probe.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # real type for annotations; runtime import is the guarded one below
    from vco_lib.deferral_report import DeferralEntry as _DeferralEntryT

try:  # DeferralEntry is optional at import time (unit tests may not need it)
    from vco_lib.deferral_report import DeferralEntry
except Exception:  # noqa: BLE001 — keep the module importable in isolation
    DeferralEntry = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

DEFAULT_CODE_EMBED_PORT = 11440
_CONDITION_ID = "codegraph_embed_resync_pending"

# ── R-6 (v0.2.73): owed-work probe ───────────────────────────────────────────
#
# Fallback embedding revision when the analyzer source cannot be parsed.
# MUST MATCH templates/scripts/analyze_code_graph.py::CODEGRAPH_EMBED_REVISION
# (the primary source — `_resolve_embed_revision` regex-parses it from the
# resolved analyzer file so the two can't silently drift on a normal install;
# this constant only covers a missing/unreadable analyzer).
_FALLBACK_EMBED_REVISION = 1

_EMBED_REVISION_RE = re.compile(
    r"^CODEGRAPH_EMBED_REVISION\s*:\s*int\s*=\s*(\d+)", re.MULTILINE
)

# Collections the owed-probe (and the post-walk verifier) count. MUST MATCH
# the analyzer's `_build_stale_file_set` probe scope (the file-anchored
# collections a re-walk can actually converge). CodeAPI/CodeInteraction carry
# no file_path and are deliberately excluded — counting rows the walk cannot
# reach would make the owed state permanently un-clearable (C-3 interlock
# warning).
_RESYNC_PROBE_BASES: tuple = ("CodeModule", "CodeClass", "CodeFunction")


def _resolve_embed_revision(analyzer_path: Optional[Path]) -> int:
    """Parse ``CODEGRAPH_EMBED_REVISION`` out of the analyzer source.

    The constant lives in the analyzer script (a template, not an importable
    module — importing it would execute heavy top-level code). A regex parse
    of the resolved file keeps this module in lock-step with whatever
    revision the spawned walk will actually stamp. Falls back to
    ``_FALLBACK_EMBED_REVISION`` when the file is missing/unreadable or the
    anchor line changed shape (conservative: a wrong-but-stale revision makes
    the probe report MORE stale rows, never fewer → never a wrong skip).
    """
    if analyzer_path is None:
        return _FALLBACK_EMBED_REVISION
    try:
        text = Path(analyzer_path).read_text(encoding="utf-8", errors="replace")
        m = _EMBED_REVISION_RE.search(text)
        if m:
            return int(m.group(1))
    except Exception as exc:  # noqa: BLE001 — parse failure → fallback
        logger.warning("codegraph resync: cannot parse embed revision: %s", exc)
    return _FALLBACK_EMBED_REVISION

# ── F9 (pre-gate audit): one-time prune of already-indexed ignore-set rows ──
#
# The P5 walker/dispatch exclusions stop NEW `.wt/` + vendor-bundle rows, but
# rows indexed BEFORE those exclusions shipped still pollute live collections
# (live-confirmed: worktree copies of tests injecting as retrieval context).
# The prune below deletes rows whose stored path falls in the CURRENT ignore
# set. This is DERIVED, regenerable data (the analyzer re-creates any row that
# genuinely belongs), so auto-applying is safe per the v0.2.60 regenerated-data
# precedent — counts are logged, everything soft-fails.

_CODEGRAPH_BASES: tuple = (
    "CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction",
)

# Path-part ignore set. MUST MATCH templates/scripts/analyze_code_graph.py::
# _COMMON_IGNORE_DIRS (+ `vendor`, which the analyzer applies via the js/ts/
# go/ruby language extras — pruning it unconditionally here matches the
# single-file dispatch's conservative `.wt`/`vendor` gate). `.claude` is added
# only when the caller says index_dot_claude=False for the project (the
# orchestrator root indexes .claude/ as first-party source — never prune it
# there).
_PRUNE_IGNORE_PARTS: frozenset = frozenset({
    '.git', '.svn', '.hg',
    '.venv', 'venv', 'env', '.env', 'virtualenv', '.tox', 'site-packages',
    '__pycache__', '.pytest_cache',
    'build', 'dist', 'out',
    'node_modules',
    'worktrees', '.wt',
    '.svelte-kit', '.next', '.nuxt', '.cache', '.parcel-cache', '.turbo',
    '.angular',
    'vendor',
})

# Filename skip suffixes. MUST MATCH the union of analyze_code_graph.py::
# _JS_SKIP_SUFFIXES + _TS_SKIP_SUFFIXES (build output / config / type stubs).
_PRUNE_SKIP_SUFFIXES: tuple = (
    '.min.js', '.bundle.js', '.chunk.js', '.config.js', '.config.mjs',
    '.d.ts', '.bundle.ts', '.chunk.ts', '.config.ts', '.config.mts',
)

# Per-base property carrying the source path. MUST MATCH the analyzer's
# storage shape (CodeModule keys on `path`; the rest on `file_path`).
_PRUNE_PATH_PROP: dict = {
    "CodeModule": "path",
}


def _collection_prefix(project_name: str) -> Optional[str]:
    """Project name → Weaviate class prefix, via the ENDORSED vco_lib wrapper
    (``codegraph_to_mermaid._sanitize_collection_prefix`` → SSOT
    ``project_naming.canonical_class_prefix``). No 5th sanitizer copy here —
    tests/test_canonical_class_prefix_parity.py guards against that.

    Returns ``None`` when the wrapper is unimportable or the name is unusable
    — the caller then does NOTHING (conservative default: never guess a
    prefix and delete from the wrong collections).
    """
    try:
        from vco_lib.codegraph_to_mermaid import (
            _sanitize_collection_prefix as _sanitize,
        )
    except Exception as exc:  # noqa: BLE001 — script-mode / partial install
        logger.warning("codegraph prune: prefix resolver unavailable: %s", exc)
        return None
    try:
        prefix = _sanitize(project_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("codegraph prune: cannot derive prefix: %s", exc)
        return None
    return prefix or None


def _build_client(weaviate_url: Optional[str] = None,
                  grpc_port: Optional[int] = None):
    """Build a Weaviate client from url/env/defaults; ``None`` on failure.

    One home for the connection recipe (extracted from ``prune_ignored_rows``
    when ``count_stale_rows`` became the second caller). The caller owns
    closing the returned client.
    """
    try:
        import weaviate  # local import — soft-fail when not installed

        url = weaviate_url or os.environ.get("WEAVIATE_URL") or "http://localhost:8081"
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?", url)
        host = m.group(1) if m else "localhost"
        http_port = int(m.group(2)) if (m and m.group(2)) else 8081
        gport = int(grpc_port or os.environ.get("GRPC_PORT") or 50052)
        return weaviate.connect_to_custom(
            http_host=host, http_port=http_port, http_secure=False,
            grpc_host=host, grpc_port=gport, grpc_secure=False,
        )
    except Exception as exc:  # noqa: BLE001 — no Weaviate → caller degrades
        logger.warning("codegraph resync: Weaviate unavailable: %s", exc)
        return None


def _path_is_ignored(file_path: str, *, index_dot_claude: bool = True) -> bool:
    """True when a stored row path falls in the CURRENT ignore set.

    Path-PART match (not substring) for directories — `my_vendor_tools/x.py`
    is NOT pruned; `vendor/x.py` is. Suffix match for build-output filenames.
    """
    if not file_path:
        return False
    norm = str(file_path).replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    ignore = _PRUNE_IGNORE_PARTS
    if not index_dot_claude:
        ignore = ignore | frozenset({'.claude'})
    if any(p in ignore for p in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith('vite.config'):
        return True
    return any(name.endswith(s) for s in _PRUNE_SKIP_SUFFIXES)


def prune_ignored_rows(
    project_name: str,
    *,
    client=None,
    index_dot_claude: bool = True,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> dict:
    """Delete code-graph rows whose stored path is in the CURRENT ignore set.

    Scoped STRICTLY to the project's own collections (``<Prefix>_CodeFunction``
    etc. — the per-project prefix is the tenant boundary). Returns a
    ``{collection_name: deleted_count}`` dict; every per-collection failure is
    logged and skipped (soft-fail — a prune failure must never fail the caller).

    ``client`` may be injected (tests); otherwise a connection is built from
    ``weaviate_url`` / ``$WEAVIATE_URL`` / localhost:8081 and closed on exit.
    """
    counts: dict = {}
    if not project_name:
        return counts

    own_client = False
    if client is None:
        client = _build_client(weaviate_url, grpc_port)
        if client is None:
            return counts
        own_client = True

    try:
        try:
            from weaviate.classes.query import Filter
        except Exception as exc:  # noqa: BLE001
            logger.warning("codegraph prune: weaviate Filter unavailable: %s", exc)
            return counts

        prefix = _collection_prefix(project_name)
        if prefix is None:
            # Conservative default: no positive prefix confirmation → do
            # nothing rather than guess a delete target.
            return counts
        for base in _CODEGRAPH_BASES:
            coll_name = f"{prefix}_{base}"
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    continue
                coll = client.collections.get(coll_name)
                path_prop = _PRUNE_PATH_PROP.get(base, "file_path")
                to_delete: list = []
                for obj in coll.iterator(return_properties=[path_prop]):
                    p = getattr(obj, "properties", None) or {}
                    fp = p.get(path_prop) or ""
                    if _path_is_ignored(fp, index_dot_claude=index_dot_claude):
                        to_delete.append(obj.uuid)
                deleted = 0
                for i in range(0, len(to_delete), 100):
                    batch = to_delete[i:i + 100]
                    coll.data.delete_many(
                        where=Filter.by_id().contains_any(batch)
                    )
                    deleted += len(batch)
                if deleted:
                    logger.info(
                        "codegraph prune: %s — deleted %d ignore-set row(s)",
                        coll_name, deleted,
                    )
                counts[coll_name] = deleted
            except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
                logger.warning("codegraph prune: %s failed: %s", coll_name, exc)
        return counts
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _count_stale_in_collection(coll, current_revision: int) -> Optional[int]:
    """Count rows NOT at ``current_revision`` in one collection.

    Two tiers:
      1. Filtered aggregate ``embed_revision != rev OR embed_revision IS
         NULL`` — cheap. The IsNull leg is load-bearing: Weaviate comparisons
         ignore NULLs and pre-migration rows are exactly the NULL ones (a
         ``min()``/``not_equal``-only probe reports "converged" over a
         half-migrated collection — C-3 warning).
      2. Full scan returning only ``embed_revision``, classified client-side
         (NULL-safe) — covers collections created without
         ``index_null_state=True`` where the IsNull filter errors.

    Returns ``None`` when neither tier could run (undeterminable — the caller
    must NOT treat that as zero).
    """
    try:
        from weaviate.classes.query import Filter

        flt = (
            Filter.by_property("embed_revision").not_equal(int(current_revision))
            | Filter.by_property("embed_revision").is_none(True)
        )
        agg = coll.aggregate.over_all(filters=flt, total_count=True)
        total = getattr(agg, "total_count", None)
        if total is not None:
            return int(total)
    except Exception:  # noqa: BLE001 — fall to the NULL-safe scan
        pass
    try:
        stale = 0
        for obj in coll.iterator(return_properties=["embed_revision"]):
            rev = (getattr(obj, "properties", None) or {}).get("embed_revision")
            try:
                if rev is None or int(rev) != int(current_revision):
                    stale += 1
            except (TypeError, ValueError):
                stale += 1
        return stale
    except Exception as exc:  # noqa: BLE001 — undeterminable
        logger.warning(
            "codegraph resync: stale count failed on %s: %s",
            getattr(coll, "name", "?"), exc,
        )
        return None


def count_stale_rows(
    project_name: str,
    *,
    current_revision: Optional[int] = None,
    analyzer_path: Optional[Path] = None,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> Optional[dict]:
    """R-6 (v0.2.73): per-collection count of rows owed a re-embed.

    Probes ``<Prefix>_{CodeModule,CodeClass,CodeFunction}`` (the collections
    a re-walk can actually converge — MUST MATCH the analyzer's
    ``_build_stale_file_set`` scope) for rows whose ``embed_revision`` is
    NULL or differs from the current revision.

    Returns ``{collection_name: stale_count}`` (absent collections count 0),
    or ``None`` when the answer is undeterminable (Weaviate down, prefix
    unresolvable, a collection unprobeable). Callers gate SKIPPING on a
    positive all-zero result only — ``None`` means "cannot positively
    confirm", never "converged".
    """
    if not project_name:
        return None
    if current_revision is None:
        current_revision = _resolve_embed_revision(analyzer_path)
    prefix = _collection_prefix(project_name)
    if prefix is None:
        return None

    own_client = False
    if client is None:
        client = _build_client(weaviate_url, grpc_port)
        if client is None:
            return None
        own_client = True

    try:
        counts: dict = {}
        for base in _RESYNC_PROBE_BASES:
            coll_name = f"{prefix}_{base}"
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    counts[coll_name] = 0
                    continue
                coll = client.collections.get(coll_name)
            except Exception as exc:  # noqa: BLE001 — undeterminable
                logger.warning(
                    "codegraph resync: cannot open %s: %s", coll_name, exc
                )
                return None
            n = _count_stale_in_collection(coll, current_revision)
            if n is None:
                return None
            counts[coll_name] = n
        return counts
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# ── v0.2.73 (M1/M3): metadata backfill for existing rows ─────────────────────
#
# `is_test` (M1) and `doc` (M3) are stamped at analyze time — but the per-file
# skip means rows in unchanged files never self-heal via analysis, and a
# revision bump would be the wrong tool (forces embed COMPUTE for render-only
# props). The right shape is a Weaviate-side `data.update` pass: PATCH
# semantics — no vector touch, no tombstone, no embed.

# Chunked bodies carry a leading `[chunk N/total]\n\n` header. MUST MATCH
# templates/scripts/analyze_code_graph.py::_CHUNK_HEADER_RE (and
# server._parse_chunk_header).
_CHUNK_HEADER_RE = re.compile(r"^\[chunk \d+/\d+\]\n\n")


def _resolve_metadata_helpers():
    """Import the shared `is_test_path` + `_extract_docstring` helpers.

    Both live in `claude_mcp_servers/weaviate_mcp/` (single homes:
    code_ranking / code_truncation). Script-mode children may not have that
    package dir on sys.path — add it from the repo layout before retrying.
    Returns ``(is_test_path | None, extract_docstring | None)``; a missing
    helper soft-degrades its half of the backfill (logged by the caller).
    """
    pkg_dir = Path(__file__).resolve().parent.parent / "claude_mcp_servers"
    if pkg_dir.is_dir() and str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))
    itp = None
    ext = None
    try:
        from weaviate_mcp.code_ranking import is_test_path as itp  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001 — ships with AG-5's consumer half
        itp = None
    try:
        from weaviate_mcp.code_truncation import _extract_docstring as ext  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001
        ext = None
    return itp, ext


# Per-base config for the backfill: (path property, body property or None).
# CodeModule gets is_test only (its `module_summary` is not a doc source).
_BACKFILL_BASES: dict = {
    "CodeFunction": ("file_path", "function_body"),
    "CodeClass": ("file_path", "class_body"),
    "CodeModule": ("path", None),
}


def backfill_codegraph_metadata(
    project_name: str,
    *,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> dict:
    """One-shot `data.update` backfill of `is_test` (+`doc` for F/C rows).

    Idempotent + resumable by construction (already-populated rows are
    skipped); NO global timeout; per-collection AND per-row soft-fail.
    Gated by a cheap probe: a collection with no NULL-`is_test` row is
    skipped entirely (steady-state cost ≈ 3 point queries). Returns
    ``{collection_name: rows_updated}``.

    doc rules (M3): only when the stored doc is empty AND the row is the
    canonical chunk (chunk_num 0/None — the docstring lives at the entity
    head); the `[chunk N/total]` header is stripped before extraction; an
    empty extract → no write (docstring-less function, re-probed next run
    at the cost of one iterator row). is_test rules (M1): only when stored
    is NULL and a path is present (fail-safe: no path → leave NULL, the
    query-time derive treats it as not-a-test).
    """
    counts: dict = {}
    if not project_name:
        return counts
    prefix = _collection_prefix(project_name)
    if prefix is None:
        return counts

    is_test_fn, extract_fn = _resolve_metadata_helpers()
    if is_test_fn is None:
        logger.warning(
            "codegraph backfill: is_test_path unavailable — is_test half skipped"
        )
    if extract_fn is None:
        logger.warning(
            "codegraph backfill: _extract_docstring unavailable — doc half skipped"
        )
    if is_test_fn is None and extract_fn is None:
        return counts

    own_client = False
    if client is None:
        client = _build_client(weaviate_url, grpc_port)
        if client is None:
            return counts
        own_client = True

    try:
        try:
            from weaviate.classes.query import Filter
        except Exception as exc:  # noqa: BLE001
            logger.warning("codegraph backfill: Filter unavailable: %s", exc)
            return counts

        for base, (path_prop, body_prop) in _BACKFILL_BASES.items():
            coll_name = f"{prefix}_{base}"
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    continue
                coll = client.collections.get(coll_name)

                # Cheap gate: fully-populated collections skip the scan.
                # Probe failure (e.g. IsNull unindexed) → scan anyway
                # (fail-open toward doing the work).
                try:
                    probe = coll.query.fetch_objects(
                        filters=Filter.by_property("is_test").is_none(True),
                        limit=1,
                    )
                    if not getattr(probe, "objects", None):
                        counts[coll_name] = 0
                        continue
                except Exception:  # noqa: BLE001
                    pass

                return_props = [path_prop, "is_test"]
                if body_prop and extract_fn is not None:
                    return_props += [body_prop, "doc", "language", "chunk_num"]

                updated = 0
                for obj in coll.iterator(return_properties=return_props):
                    p = getattr(obj, "properties", None) or {}
                    new_props: dict = {}

                    if is_test_fn is not None and p.get("is_test") is None:
                        path_val = p.get(path_prop) or ""
                        if path_val:
                            try:
                                new_props["is_test"] = bool(is_test_fn(path_val))
                            except Exception:  # noqa: BLE001
                                pass

                    if (
                        body_prop
                        and extract_fn is not None
                        and not p.get("doc")
                        and p.get("chunk_num") in (0, None)
                    ):
                        body = p.get(body_prop) or ""
                        if body:
                            try:
                                doc = extract_fn(
                                    _CHUNK_HEADER_RE.sub("", body, count=1),
                                    str(p.get("language") or "python"),
                                )
                                if doc:
                                    new_props["doc"] = doc[:2000]
                            except Exception:  # noqa: BLE001
                                pass

                    if not new_props:
                        continue
                    try:
                        coll.data.update(uuid=obj.uuid, properties=new_props)
                        updated += 1
                    except Exception as exc:  # noqa: BLE001 — per-row soft-fail
                        logger.warning(
                            "codegraph backfill: update failed on %s/%s: %s",
                            coll_name, obj.uuid, exc,
                        )
                counts[coll_name] = updated
                if updated:
                    logger.info(
                        "codegraph backfill: %s — %d row(s) updated",
                        coll_name, updated,
                    )
            except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
                logger.warning("codegraph backfill: %s failed: %s", coll_name, exc)
        return counts
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class ResyncTriggerResult:
    """Outcome of a resync-trigger attempt. Never raises; the caller inspects
    ``status`` to decide whether to record a deferral.

    ``status`` is one of:
      * ``"launched"``   — a background analyze was spawned (``pid`` is set).
      * ``"deferred"``   — the code-embed service was down; ``deferral`` carries
                            a :class:`DeferralEntry` (when the type is available)
                            for the caller to record. Nothing was spawned.
      * ``"skipped"``    — a precondition wasn't met (analyzer/python missing,
                            no project name). Soft no-op; ``message`` explains.
      * ``"not_owed"``   — R-6 (v0.2.73): the owed-probe POSITIVELY confirmed
                            zero stale rows — no work owed, nothing spawned.
                            The caller may resolve a pending
                            ``codegraph_embed_resync_pending`` deferral.
    """

    status: str
    message: str = ""
    pid: Optional[int] = None
    deferral: Optional[object] = None


def code_embed_service_healthy(
    code_embed_url: Optional[str] = None,
    *,
    timeout: float = 2.0,
) -> bool:
    """Return True iff the code-embedding service answers ``/health`` < 400.

    Resolution order for the base URL: explicit arg → ``CODE_EMBED_SERVICE_URL``
    env → ``http://localhost:<CODE_EMBED_PORT|11440>``. Never raises — any
    failure (connection refused, timeout, DNS) returns False so the caller
    degrades to the deferral path rather than crashing the update.
    """
    base = (
        code_embed_url
        or os.environ.get("CODE_EMBED_SERVICE_URL")
        or f"http://localhost:{os.environ.get('CODE_EMBED_PORT', DEFAULT_CODE_EMBED_PORT)}"
    )
    base = base.rstrip("/")
    health = base if base.endswith("/health") else f"{base}/health"
    try:
        resp = urllib.request.urlopen(health, timeout=timeout)
        return resp.status < 400
    except Exception:  # noqa: BLE001 — unreachable service → not healthy
        return False


def _resync_log_path(project_name: str) -> Optional[Path]:
    """R-5 (RT-2): per-spawn log file for the detached resync children.

    ``<vct_root_dir>/logs/resync-<project>-<ts>.log``. Pre-fix the children's
    stdout/stderr went to DEVNULL — a walk that died at 40% left NO record
    anywhere ("soft-fail into a void"). Returns ``None`` when the path cannot
    be prepared (caller then degrades to DEVNULL rather than blocking the
    spawn — but logs that degradation).
    """
    try:
        from vco_lib.paths import vct_root_dir

        logs_dir = vct_root_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", project_name or "project")
        ts = time.strftime("%Y%m%d-%H%M%S")
        return logs_dir / f"resync-{safe}-{ts}.log"
    except Exception as exc:  # noqa: BLE001 — logging must not block the spawn
        logger.warning("codegraph resync: cannot prepare log path: %s", exc)
        return None


def _resolve_analyzer(repo_root: Path) -> Optional[Path]:
    """Locate ``analyze_code_graph.py``. Prefers the shipped project copy under
    ``.claude/scripts/`` (what user projects run), falls back to the source
    template. Returns None when neither exists (soft-skip)."""
    candidates = [
        repo_root / ".claude" / "scripts" / "analyze_code_graph.py",
        repo_root / "templates" / "scripts" / "analyze_code_graph.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def build_resync_deferral(
    project_name: str,
    command_to_apply: str,
) -> Optional["_DeferralEntryT"]:
    """Construct the ``codegraph_embed_resync_pending`` :class:`DeferralEntry`.

    Returns None when ``DeferralEntry`` is unavailable (isolated import) so the
    caller can still branch on a falsy value without a hard dependency.
    """
    if DeferralEntry is None:
        return None
    return DeferralEntry(
        condition_id=_CONDITION_ID,
        title="Code-graph re-embed pending (chunking revision changed)",
        detected=(
            "The code-embedding service (:{port}) was unreachable during "
            "--update, so the revision-gated code-graph resync for project "
            "'{proj}' could not run. About 7-9% of functions/classes were "
            "embedded under the pre-chunking scheme and need re-embedding so "
            "their over-budget tails become searchable.".format(
                port=DEFAULT_CODE_EMBED_PORT, proj=project_name
            )
        ),
        why_deferred=(
            "A per-object re-embed needs the code-embedding service running; "
            "launching the analyze now would fail every embed. The resync is "
            "resumable — re-running it once the service is up re-embeds only "
            "the stale rows (revision mismatch) and skips everything already "
            "current, so it is safe and cheap to defer."
        ),
        command_to_apply=command_to_apply,
        severity="warning",
        kg_node_refs=[],
    )


def build_unconverged_deferral(
    project_name: str,
    counts: Optional[dict],
    command_to_apply: str,
) -> Optional["_DeferralEntryT"]:
    """R-7 (v0.2.73): the ONE-TIME "resync did not converge" ledger entry.

    Written by the post-walk verifier when stale rows remain (or convergence
    could not be verified). Per the deferred-over-autoconverge design ruling
    this is a one-time surface, NOT a per-update reconciler loop: install.py
    treats the condition id as FOREIGN (preserved verbatim, A-2) and resolves
    it ONLY on a positive zero-stale probe (R-6). Fields are single-line —
    the Markdown round-trip truncates multi-line field values (A-3).
    """
    if DeferralEntry is None:
        return None
    if counts is not None:
        owed = ", ".join(f"{k}: {v}" for k, v in counts.items() if v) or "unknown"
        detected = (
            f"A background code-graph resync for project '{project_name}' "
            f"finished but stale rows remain ({owed}). Retrieval still works "
            "via the previously stored vectors; the stale rows are just not "
            "re-embedded under the current revision yet."
        )
    else:
        detected = (
            f"A background code-graph resync for project '{project_name}' "
            "finished but convergence could not be verified (the stale-row "
            "probe was unavailable)."
        )
    return DeferralEntry(
        condition_id=_CONDITION_ID,
        title="Code-graph resync did not fully converge",
        detected=detected,
        why_deferred=(
            "Background resyncs are best-effort and never force-applied. "
            "Re-running the command below re-embeds only the remaining stale "
            "rows (cheap, resumable). This entry is written once and clears "
            "automatically when a later update's probe confirms zero stale rows."
        ),
        command_to_apply=command_to_apply,
        severity="warning",
        kg_node_refs=[],
    )


def _resolve_persisted_resync_deferral(repo_root: Path) -> None:
    """Remove a persisted resync ledger entry (converged). Read-merge-write
    like every non-install.py deferral writer; soft-fail with a log line."""
    try:
        from vco_lib.deferral_report import DeferralReport

        folder = Path(repo_root)
        report = DeferralReport.read(folder)
        if report.has_condition(_CONDITION_ID):
            report.mark_resolved(_CONDITION_ID)
            report.write(folder)
            logger.info("resync driver: resolved persisted resync deferral")
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("resync driver: could not resolve deferral: %s", exc)


def _record_unconverged_deferral(
    repo_root: Path,
    project_name: str,
    counts: Optional[dict],
    resume_cmd: str,
) -> None:
    """Persist the one-time unconverged entry. Read-merge-write; soft-fail."""
    try:
        entry = build_unconverged_deferral(project_name, counts, resume_cmd)
        if entry is None:
            return
        from vco_lib.deferral_report import DeferralReport

        folder = Path(repo_root)
        report = DeferralReport.read(folder)
        report.add_entry(entry)
        report.write(folder)
        print(
            "[resync-driver] one-time deferral recorded in "
            f"{folder / '.claude' / 'context' / 'UPDATE_DEFERRED.md'}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — surfacing must not crash the child
        logger.warning("resync driver: could not record deferral: %s", exc)


def _register_spawn_with_hub(
    project_name: str, pid: int, repo_root: "Path | str | None" = None
) -> None:
    """R-4 (Python half): best-effort registration of the detached resync
    driver in the launcher's ``code_graph_builds`` tracker via vct-hub, so
    the GUI top progress shows the walk and the boot orphan-sweep can
    death-detect it (pid-aliveness).

    Wire contract (Rust half ships together under R-4 —
    ``modules_api.rs::register_codegraph_build``):

        POST http://127.0.0.1:<port>/api/v1/projects/<project>/codegraph-builds
        Authorization: Bearer <vct_root_dir>/hub.token
        {"status": "running", "pid": <pid>, "source": "install_resync",
         "repo_root": "<abs repo root>"}

    ``repo_root`` is the PRIMARY resolver on the hub side: ``project_name`` is
    the codegraph project name (Weaviate class prefix), which is neither the
    launcher project id nor its slug, so the ``<project>`` path segment can't
    be resolved from id/slug alone. The hub matches ``repo_root`` against the
    launcher's indexed ``folder_path`` (canonical match), falling back to
    id/slug. (Pre-gate correctness audit C-3.)

    Hub down / endpoint absent (404 on older hubs) / token missing → soft
    no-op logged at debug. Registration is observability, never a gate on the
    spawn.
    """
    try:
        import json as _json
        import urllib.parse
        import urllib.request

        from vco_lib.paths import vct_root_dir

        root = vct_root_dir()
        port_raw = os.environ.get("VCT_HUB_PORT") or ""
        if not port_raw:
            try:
                port_raw = (root / "hub.port").read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                port_raw = ""
        port = int(port_raw) if port_raw else 7700
        token = os.environ.get("VCT_HUB_TOKEN") or ""
        if not token:
            token = (root / "hub.token").read_text(encoding="utf-8").strip()
        # repo_root is the PRIMARY resolver on the hub side: project_name here
        # is the codegraph project name (Weaviate class prefix), which is
        # neither the launcher project id nor its slug — the hub can't resolve
        # it from the path segment alone. The repo root path IS indexed by the
        # launcher, so we send it in the body for a reliable match. (C-3.)
        _payload = {"status": "running", "pid": int(pid), "source": "install_resync"}
        if repo_root:
            _payload["repo_root"] = str(repo_root)
        body = _json.dumps(_payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/projects/"
            f"{urllib.parse.quote(project_name, safe='')}/codegraph-builds",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=3.0)
        logger.info(
            "codegraph resync: registered spawn with hub (HTTP %s)",
            getattr(resp, "status", "?"),
        )
    except Exception as exc:  # noqa: BLE001 — observability, never a gate
        logger.debug("codegraph resync: hub registration skipped: %s", exc)


def run_resync_and_verify(
    project_name: str,
    repo_root: Path,
    analyzer_path: Path,
    *,
    prune_stale: bool = False,
) -> int:
    """R-7 driver — runs INSIDE the detached child spawned by
    :func:`spawn_background_resync`.

    1. Runs the analyzer as a blocking subprocess (NO timeout — project
       rule; the analyzer self-guards per embed request). ``--prune-stale``
       is forwarded only when the spawn confirmed it safe (no extra paths).
    2. Re-probes the stale-row counts post-walk.
    3. Positive zero → resolves any persisted resync ledger entry.
       Stale rows remain / probe unavailable → records the ONE-TIME
       unconverged deferral (soft-fail WITH a signal — the RT-5 walk died
       with none).

    Always returns 0: the deferral (not the exit code) carries the signal;
    nothing waits on this process.
    """
    argv = [
        sys.executable, str(analyzer_path), str(repo_root),
        "--project", project_name,
    ]
    if prune_stale:
        argv.append("--prune-stale")
    print(f"[resync-driver] running: {' '.join(argv)}", flush=True)
    try:
        rc = subprocess.run(argv, cwd=str(repo_root)).returncode  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        print(f"[resync-driver] analyzer failed to start: {exc}", flush=True)
        rc = -1

    try:
        counts = count_stale_rows(project_name, analyzer_path=Path(analyzer_path))
    except Exception as exc:  # noqa: BLE001
        print(f"[resync-driver] post-walk probe raised: {exc}", flush=True)
        counts = None

    resume_cmd = " ".join(shlex.quote(p) for p in argv)
    if counts is not None and sum(counts.values()) == 0:
        # The probe is authoritative for the ledger's "owed" semantic —
        # converged even when the analyzer exit code was non-zero (rc is
        # still logged above for the record).
        print(f"[resync-driver] converged: 0 stale rows (analyzer exit {rc})",
              flush=True)
        _resolve_persisted_resync_deferral(repo_root)
        return 0
    stale_desc = (
        str(sum(counts.values())) if counts is not None else "unverifiable"
    )
    print(
        f"[resync-driver] NOT converged (analyzer exit {rc}, "
        f"stale rows: {stale_desc})",
        flush=True,
    )
    _record_unconverged_deferral(repo_root, project_name, counts, resume_cmd)
    return 0


def spawn_background_resync(
    repo_root: Path,
    project_name: str,
    *,
    python_exe: Optional[str] = None,
    code_embed_url: Optional[str] = None,
    check_service: bool = True,
    check_owed: bool = True,
    index_dot_claude: bool = True,
) -> ResyncTriggerResult:
    """Launch a BACKGROUND, revision-gated full re-analyze of ``repo_root``.

    A full (no ``--incremental``, no ``--only-file``) analyze is intentional:
    the revision gate inside the analyzer makes it LIGHT — only rows whose
    stored ``embed_revision`` differs from the current one re-embed; the 90%+
    already-current rows hash-skip. This is the host-agnostic, revision-based
    resync (it re-embeds the root project too, not just non-root projects).

    Non-blocking: the analyzer is spawned detached via ``Popen`` and this
    function returns immediately with ``status="launched"``. NO global timeout
    is imposed — the analyzer self-guards per embed request.

    Degrade path: when ``check_service`` and the code-embed service is down, we
    do NOT spawn (a re-embed would fail). We return ``status="deferred"`` with a
    :class:`DeferralEntry` for the caller to record; the update still succeeds.

    Never raises: precondition failures (missing analyzer/python, empty project
    name, spawn error) return a ``skipped``/``deferred`` result with a message.
    """
    if not project_name:
        return ResyncTriggerResult(
            status="skipped", message="no project name — cannot target collections"
        )

    analyzer = _resolve_analyzer(repo_root)
    if analyzer is None:
        return ResyncTriggerResult(
            status="skipped",
            message=f"analyze_code_graph.py not found under {repo_root}",
        )

    py = python_exe or sys.executable
    if not py:
        return ResyncTriggerResult(
            status="skipped", message="no python interpreter resolved"
        )

    # R-5 rider (A-1): shlex-quote every part — the command lands verbatim in
    # UPDATE_DEFERRED.md and must survive paths containing spaces.
    resume_cmd = " ".join(
        shlex.quote(part)
        for part in (str(py), str(analyzer), str(repo_root), "--project", project_name)
    )

    # R-6 (v0.2.73): gate on OWED WORK, not on "--update happened". Pre-fix,
    # every update spawned a full background walk regardless of whether any
    # row needed a re-embed. Skip ONLY on a POSITIVE zero — an undeterminable
    # probe (Weaviate down, prefix unresolvable) proceeds like before
    # (conservative default: never skip on uncertainty).
    if check_owed:
        try:
            stale_counts = count_stale_rows(project_name, analyzer_path=analyzer)
        except Exception as exc:  # noqa: BLE001 — probe must never block
            logger.warning("codegraph resync: owed-probe raised: %s", exc)
            stale_counts = None
        if stale_counts is not None and sum(stale_counts.values()) == 0:
            return ResyncTriggerResult(
                status="not_owed",
                message=(
                    f"no resync owed for {project_name} — all rows at the "
                    "current embed revision"
                ),
            )
        if stale_counts:
            owed = {k: v for k, v in stale_counts.items() if v}
            logger.info("codegraph resync: stale rows owed: %s", owed)

    if check_service and not code_embed_service_healthy(code_embed_url):
        deferral = build_resync_deferral(project_name, resume_cmd)
        return ResyncTriggerResult(
            status="deferred",
            message=(
                f"code-embed service (:{DEFAULT_CODE_EMBED_PORT}) unreachable — "
                "resync deferred (see UPDATE_DEFERRED.md)"
            ),
            deferral=deferral,
        )

    # Build the DRIVER argv (R-7): the detached child is this module in
    # --run-resync mode. It runs the analyzer as a blocking subprocess (full
    # walk; the revision gate keeps it light), then re-probes convergence and
    # records the one-time deferral when stale rows remain — the RT-5 walk
    # died silently precisely because nothing verified it. We do NOT pass
    # --force-recreate (that would DROP + rebuild the schema, losing all
    # rows) — the resync is purely additive re-embed of stale rows.
    #
    # We DO NOT forward --prune-stale here. CRITICAL (pre-gate correctness
    # audit C-1): the analyzer only adds a file's UUIDs to `visited_uuids`
    # when it actually WALKS the file. A revision-gated resync hash-skips
    # every already-converged file (the whole point — 90%+ of files skip),
    # so those files' rows are "unvisited" and --prune-stale would DELETE
    # them — destroying the majority of an already-converged code graph
    # (GPU-hours of vectors) while the post-walk stale-count verifier sees 0
    # stale rows (deleted rows aren't stale) and falsely reports "converged".
    # Prune is only ever safe from a FULL walk that visits every current
    # file; CG-4 orphan cleanup is handled by the F9 ignore-prune child and
    # the GUI-triggered full rebuild, NOT by this selective re-embed resync.
    argv = [
        py, str(Path(__file__).resolve()), "--run-resync",
        "--project", project_name,
        "--repo-root", str(repo_root),
        "--analyzer", str(analyzer),
    ]

    # Detached background spawn. stdout/stderr go to a per-spawn log file
    # under <vct_root_dir>/logs/ (R-5 / RT-2 — pre-fix they went to DEVNULL,
    # so a walk that died mid-run left no record anywhere). The children
    # inherit the fd; the parent closes its handle right after spawning so
    # nothing keeps the parent's pipes open. Log-file preparation failure
    # degrades to DEVNULL (spawn must never block on logging).
    # start_new_session detaches the process group (POSIX); on Windows the
    # default is fine (no controlling terminal to inherit).
    log_path = _resync_log_path(project_name)
    log_handle = None
    if log_path is not None:
        try:
            log_handle = open(log_path, "ab")
            log_handle.write(
                f"# codegraph resync for {project_name} — spawned "
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode()
            )
            log_handle.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("codegraph resync: cannot open log file: %s", exc)
            log_handle = None
    child_out = log_handle if log_handle is not None else subprocess.DEVNULL
    popen_kwargs = {
        "cwd": str(repo_root),
        "stdout": child_out,
        "stderr": child_out,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    # F9: spawn the ignore-set prune as a SECOND detached child (this module
    # run as a script — see the __main__ handler). Background, soft-fail:
    # a prune spawn failure never blocks the resync itself. Rows it deletes
    # are regenerable derived data; the concurrent analyzer never re-writes
    # them (its walkers skip the same ignore set).
    try:
        prune_argv = [
            py, str(Path(__file__).resolve()),
            "--prune-ignored", "--project", project_name,
        ]
        if index_dot_claude:
            prune_argv.append("--index-dot-claude")
        subprocess.Popen(prune_argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — prune is best-effort
        logger.warning("codegraph prune spawn failed: %s", exc)

    # v0.2.73 (M1/M3): spawn the metadata backfill as a THIRD detached child
    # (prune precedent above). data.update-only — no vectors, no embeds;
    # idempotent; a spawn failure never blocks the resync itself.
    try:
        backfill_argv = [
            py, str(Path(__file__).resolve()),
            "--backfill-metadata", "--project", project_name,
        ]
        subprocess.Popen(backfill_argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — backfill is best-effort
        logger.warning("codegraph metadata-backfill spawn failed: %s", exc)

    # v0.2.73 (M2): spawn the code-summary generator as a FOURTH detached
    # child (prune/backfill precedent above). LLM-budgeted via its own
    # --max-per-run default (env VCO_CODE_SUMMARY_MAX_PER_RUN); writes only
    # .claude/.code_formats.json; a spawn failure never blocks the resync.
    try:
        summary_script = analyzer.parent / "generate-code-summary.py"
        if summary_script.is_file():
            summary_argv = [
                py, str(summary_script),
                "--project", project_name,
                "--project-root", str(repo_root),
            ]
            subprocess.Popen(summary_argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — summary rider is best-effort
        logger.warning("code-summary spawn failed: %s", exc)

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — spawn failure must not crash update
        # Treat a spawn failure like the service-down case: defer with a
        # re-run command so the user can complete the resync later.
        deferral = build_resync_deferral(project_name, resume_cmd)
        return ResyncTriggerResult(
            status="deferred",
            message=f"background analyze spawn failed: {exc}",
            deferral=deferral,
        )
    finally:
        # The children inherited the fd; the parent's handle is done.
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass

    # R-4 (Python half): best-effort GUI/death-detection registration of the
    # driver pid via vct-hub. Soft no-op when the hub or the endpoint isn't
    # available — never gates the spawn.
    try:
        _register_spawn_with_hub(project_name, proc.pid, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        logger.debug("codegraph resync: hub registration raised: %s", exc)

    log_note = f" (log: {log_path})" if log_handle is not None else ""
    return ResyncTriggerResult(
        status="launched",
        message=(
            f"background code-graph resync launched for {project_name}{log_note}"
        ),
        pid=proc.pid,
    )


def _main(argv: Optional[list] = None) -> int:
    """Script entrypoint for the detached children spawned from
    :func:`spawn_background_resync`:

      * ``--prune-ignored`` — F9 ignore-set prune child.
      * ``--run-resync`` — R-7 resync DRIVER: runs the analyzer (blocking,
        no timeout), then verifies convergence and maintains the one-time
        ``codegraph_embed_resync_pending`` deferral.
    """
    import argparse

    # Script mode puts THIS file's directory (vco_lib/) on sys.path, not the
    # repo root — make `vco_lib.*` importable for the prefix resolver.
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    parser = argparse.ArgumentParser(description="codegraph resync helpers")
    parser.add_argument("--prune-ignored", action="store_true",
                        help="delete rows whose path is in the ignore set")
    parser.add_argument("--run-resync", action="store_true",
                        help="run the analyzer + verify convergence (R-7 driver)")
    parser.add_argument("--backfill-metadata", action="store_true",
                        help="data.update backfill of is_test/doc (M1/M3)")
    parser.add_argument("--project", required=True, help="project name")
    parser.add_argument("--index-dot-claude", action="store_true",
                        help="the project indexes .claude/ — do NOT prune it")
    parser.add_argument("--repo-root", help="repository root (--run-resync)")
    parser.add_argument("--analyzer", help="analyzer script path (--run-resync)")
    parser.add_argument("--prune-stale", action="store_true",
                        help="forward --prune-stale to the analyzer "
                             "(--run-resync; spawn passes it only when safe)")
    args = parser.parse_args(argv)

    if args.prune_ignored:
        logging.basicConfig(level=logging.INFO)
        counts = prune_ignored_rows(
            args.project, index_dot_claude=args.index_dot_claude,
        )
        total = sum(counts.values()) if counts else 0
        logger.info("codegraph prune complete: %d row(s) deleted (%s)",
                    total, counts)
    elif args.run_resync:
        logging.basicConfig(level=logging.INFO)
        if not args.repo_root or not args.analyzer:
            print("[resync-driver] --run-resync needs --repo-root + --analyzer",
                  file=sys.stderr)
            return 2
        return run_resync_and_verify(
            args.project,
            Path(args.repo_root),
            Path(args.analyzer),
            prune_stale=args.prune_stale,
        )
    elif args.backfill_metadata:
        logging.basicConfig(level=logging.INFO)
        counts = backfill_codegraph_metadata(args.project)
        total = sum(counts.values()) if counts else 0
        logger.info("codegraph metadata backfill complete: %d row(s) updated (%s)",
                    total, counts)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
