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

# Hygiene (v0.2.75): keep the Popen handles of deliberately-DETACHED children
# alive for the parent's lifetime. The children (resync driver, prune,
# metadata backfill, code-summary rider) are meant to outlive install.py —
# nobody waits on them — but dropping the handle lets CPython's Popen.__del__
# fire on GC and print "ResourceWarning: subprocess NNN is still running"
# (+ tracemalloc advice) into the user's update output. Live-observed 4x on
# the 2026-07-07 dogfood update. Holding the reference suppresses the
# destructor for the process lifetime; the OS reparents the children on
# parent exit as designed. Do NOT replace this with warning suppression —
# the warning is correct for genuinely-forgotten children.
_DETACHED_CHILDREN: list = []

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

# Per-base source-path property. MUST MATCH the analyzer's storage shape
# (CodeModule keys on `path`; Function/Class on `file_path`) — the same map the
# analyzer's `_build_stale_file_set` probe uses. Used by the R3 reachability
# filter to read each stale row's stored path.
_PROBE_PATH_PROP: dict = {
    "CodeModule": "path",
    "CodeClass": "file_path",
    "CodeFunction": "file_path",
}


# v0.2.75 (P1b-1): the reachability rule, the ignore-set and the whole
# owed/not-owed/purgeable decision now live in ONE shared home —
# ``vco_lib.codegraph_row_classify`` — consumed here by import and mirrored
# byte-identically inside the analyzer template (which cannot import vco_lib
# at user sites); ``tests/test_codegraph_row_classify_parity.py`` locks the
# mirror. The pre-v0.2.75 module-local names below are kept as aliases for
# existing importers (tests, downstream scripts).
from vco_lib.codegraph_row_classify import (  # noqa: E402 — grouped with the notes above
    CODEGRAPH_IGNORE_PARTS,
    CODEGRAPH_SKIP_SUFFIXES,
    TRANSIENT_STATE_MARKER as _SHARED_TRANSIENT_STATE_MARKER,
    classify_row,
    is_deleted_primary_row,
    path_is_ignored,
    path_reachable_on_disk,
)

# v0.2.82 (WP-2 task 3): the resync's "owed work" staleness + the convergence
# report's embed-vs-stamp split delegate to the ONE pure guards module, so the
# resync count can never drift from what the analyzer's re-walk actually does.
# ``codegraph_row_classify.classify_row`` (imported above) is a DIFFERENT,
# reachability-aware classifier — the guards helpers are imported under
# namespaced aliases to keep the two unmistakably separate.
from vco_lib.codegraph_guards import (  # noqa: E402 — grouped with the notes above
    classify_stale_kind as _guards_classify_stale_kind,
    is_row_revision_stale as _guards_is_row_revision_stale,
)

#: Backwards-compat alias (pre-v0.2.75 module-local name).
_path_reachable_on_disk = path_reachable_on_disk


def _make_reachability_filter(repo_root: Optional[Path]):
    """Return a memoized ``(rel_path: str) -> bool`` reachability predicate, or
    ``None`` when ``repo_root`` is not provided (R3 disabled → count all stale).

    The on-disk result for each distinct path is cached for the whole probe run
    (the Function collection repeats one ``file_path`` across every function in
    a file — one syscall per unique path, not per row). This is the "on-disk
    file set cached once per run" the R3 bound calls for; membership is resolved
    lazily via per-path existence checks so we never pay a full-tree walk.
    """
    if repo_root is None:
        return None
    cache: dict = {}

    def _reachable(rel_path: str) -> bool:
        key = rel_path or ""
        hit = cache.get(key)
        if hit is None:
            hit = _path_reachable_on_disk(key, repo_root)
            cache[key] = hit
        return hit

    return _reachable


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

# Path-part ignore set + filename skip suffixes: v0.2.75 (P1b-2) — now the
# ONE derived set shared with the analyzer's walk table
# (`_ALL_IGNORE_PARTS`) via `vco_lib.codegraph_row_classify` (value-parity
# locked by tests/test_codegraph_row_classify_parity.py). This adds the
# previously missing language-extras (`target`/`coverage`/`obj`/`bin`/
# `.gradle`/`.vs`/`.bundle`) so a stale row under e.g. `target/` no longer
# survives the prune while being unreachable by any walk (an immortal
# convergence loop pre-v0.2.75). `.claude` is added only when the caller
# says index_dot_claude=False for the project (the orchestrator root indexes
# .claude/ as first-party source — never prune it there). The pre-v0.2.75
# module-local names are kept as aliases for existing importers.
_PRUNE_IGNORE_PARTS: frozenset = CODEGRAPH_IGNORE_PARTS
_PRUNE_SKIP_SUFFIXES: tuple = CODEGRAPH_SKIP_SUFFIXES

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


# v0.2.75 (P1b): the ignored-path predicate moved to the shared classifier
# module (one home). Alias keeps the pre-v0.2.75 module-local name working
# for existing importers.
_path_is_ignored = path_is_ignored


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


#: Exact substring identifying orchestrator transient-scratch rows — the
#: value now lives in ``vco_lib.codegraph_row_classify.TRANSIENT_STATE_MARKER``
#: (MUST match ``migrations/codegraph_collection/6_to_7.py::_TRANSIENT_MARKER``).
#: Alias keeps the pre-v0.2.75 module-local name for existing importers.
_TRANSIENT_STATE_MARKER = _SHARED_TRANSIENT_STATE_MARKER


def _count_stale_in_collection(
    coll,
    current_revision: int,
    *,
    path_prop: str = "file_path",
    reachable_fn=None,
    primary_sources: "Optional[set]" = None,
    index_dot_claude: bool = True,
) -> Optional[int]:
    """Count rows a re-walk is genuinely OWED in one collection.

    v0.2.75 (P1b-1): the per-row decision routes through the ONE shared
    classifier (``vco_lib.codegraph_row_classify.classify_row`` — the same
    function the analyzer's orphan-clear mirrors), so the probe and the
    purge can never disagree again about which rows count. A row is counted
    iff it classifies ``"owed"``: stale AND reachable AND non-ignored AND
    primary-source. Everything the classifier calls ``"purgeable"`` —
    deleted-file orphans (R3, v0.2.74), transient ``.claude/state/`` scratch
    (F2/F4), pathless rows, ignore-set rows — is NOT owed: the analyzer's
    orphan-clear deletes those on its next walk, and counting them here kept
    the owed state permanently non-zero → every ``--update`` re-triggered a
    whole-repo resync (the "not converged" loop; pathless + ignored-path
    were the two IMMORTAL classes pre-v0.2.75). Extra-path rows
    (``project_source`` outside ``primary_sources``) classify ``"not_owed"``
    and converge on their own root's walk.

    Bounded cost: the cheap filtered aggregate is ALWAYS the first gate. When
    it returns 0, we return 0 IMMEDIATELY — no per-row scan (the converged
    steady-state cost is one aggregate). The slow per-row classify scan is
    paid ONLY when the aggregate says "stale > 0" (there is genuine work to
    classify). Without a ``reachable_fn`` the aggregate count is returned
    as-is (pre-R3 behaviour: all stale rows, orphans included — conservative
    over-count, never a wrong "converged").

    Tiers:
      1. Filtered aggregate ``embed_revision != rev OR embed_revision IS
         NULL`` — cheap. The IsNull leg is load-bearing: Weaviate comparisons
         ignore NULLs and pre-migration rows are exactly the NULL ones (a
         ``min()``/``not_equal``-only probe reports "converged" over a
         half-migrated collection — C-3 warning).
      2. Full scan classified client-side (NULL-safe) — covers collections
         created without ``index_null_state=True`` where the IsNull filter
         errors, AND is where the shared classifier is applied.

    Returns ``None`` when neither tier could run (undeterminable — the caller
    must NOT treat that as zero).
    """
    agg_total: Optional[int] = None
    try:
        from weaviate.classes.query import Filter

        flt = (
            Filter.by_property("embed_revision").not_equal(int(current_revision))
            | Filter.by_property("embed_revision").is_none(True)
        )
        agg = coll.aggregate.over_all(filters=flt, total_count=True)
        total = getattr(agg, "total_count", None)
        if total is not None:
            agg_total = int(total)
    except Exception:  # noqa: BLE001 — fall to the NULL-safe scan
        agg_total = None

    if agg_total is not None:
        # Aggregate is authoritative for "is there ANY stale row?". Zero → done,
        # NO per-row scan (the R3 bound: cheap converged steady state).
        if agg_total == 0:
            return 0
        if reachable_fn is None:
            # Pre-R3: return the aggregate (all stale rows, orphans included).
            return agg_total
        # R3: aggregate > 0 → pay the per-row scan to count only reachable
        # stale rows (fall through to the scan below).

    # Full NULL-safe scan. Reads embed_revision (+ the path property when R3
    # reachability filtering is on, + project_source when source-scoping is on
    # AND the class actually has it — requesting an absent property 500s the
    # iterator on schema variants, mirroring the 6_to_7 config-probe defense).
    read_props = ["embed_revision"]
    if reachable_fn is not None and path_prop not in read_props:
        read_props.append(path_prop)
    want_source = primary_sources is not None
    if want_source:
        try:
            cfg = coll.config.get()
            present = {p.name for p in cfg.properties}
            if "project_source" in present:
                read_props.append("project_source")
            else:
                want_source = False  # class predates the property → no scoping
        except Exception:  # noqa: BLE001 — probe best-effort; skip the scoping
            want_source = False
    try:
        stale = 0
        for obj in coll.iterator(return_properties=read_props):
            props = getattr(obj, "properties", None) or {}
            if reachable_fn is None:
                # Pre-R3 tier: no root/predicate → revision-only counting
                # (classify_row would fail open toward "owed" for every
                # path-bearing row anyway). v0.2.82 (WP-2 task 3): the inline
                # NULL/int-mismatch check is now the ONE guards helper, so the
                # resync count and the analyzer's re-walk agree by construction.
                if _guards_is_row_revision_stale(
                    props.get("embed_revision"), int(current_revision)
                ):
                    stale += 1
                continue
            # v0.2.75 (P1b-1): ONE shared decision — count iff "owed".
            verdict = classify_row(
                props,
                None,  # reachability comes via the memoized reachable_fn
                path_prop=path_prop,
                current_revision=int(current_revision),
                index_dot_claude=index_dot_claude,
                primary_sources=primary_sources if want_source else None,
                reachable_fn=reachable_fn,
            )
            if verdict == "owed":
                stale += 1
        return stale
    except Exception as exc:  # noqa: BLE001 — undeterminable
        logger.warning(
            "codegraph resync: stale count failed on %s: %s",
            getattr(coll, "name", "?"), exc,
        )
        # If the aggregate DID give a positive count but the R3 scan failed, we
        # cannot safely subtract orphans — return None (undeterminable) rather
        # than a possibly-wrong number. The caller treats None conservatively
        # (never a wrong "converged").
        return None


def count_stale_rows(
    project_name: str,
    *,
    current_revision: Optional[int] = None,
    analyzer_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
    index_dot_claude: bool = True,
) -> Optional[dict]:
    """R-6 (v0.2.73): per-collection count of rows owed a re-embed.

    Probes ``<Prefix>_{CodeModule,CodeClass,CodeFunction}`` (the collections
    a re-walk can actually converge — MUST MATCH the analyzer's
    ``_build_stale_file_set`` scope) for rows whose ``embed_revision`` is
    NULL or differs from the current revision.

    R3 (v0.2.74): when ``repo_root`` is provided, count only REACHABLE stale
    rows — a stale row whose stored path was deleted from disk (an orphan) is
    excluded, because nothing re-walks a deleted file so it can never converge.
    Counting orphans kept the owed state non-zero forever → every ``--update``
    re-triggered a whole-repo resync (the "not converged" loop). The analyzer's
    D1 orphan-clear deletes those rows; this filter stops counting them so the
    owed-probe reaches 0. Bounded: per-collection the cheap aggregate gates the
    scan (aggregate 0 → return 0 with NO per-row scan). ``repo_root`` absent →
    pre-R3 behaviour (count all stale, orphans included — conservative: never a
    wrong "converged", just an occasional un-clearable owed loop on a shrunk
    repo, which is the pre-fix status quo).

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

    # R3: build the memoized on-disk reachability predicate ONCE for the whole
    # probe run (shared across the 3 collections; one syscall per unique path).
    # None when repo_root is not supplied → R3 disabled, count all stale.
    reachable_fn = _make_reachability_filter(repo_root)

    # v0.2.74 (Fable-review F2): the primary root's POSIX forms (raw +
    # resolved) — rows stamped with a DIFFERENT non-empty project_source are
    # extra-path rows a primary walk can never re-stamp → excluded from the
    # owed count (mirrors the analyzer orphan-clear's B1 scoping). None when
    # repo_root is absent (scoping off, pre-fix behaviour).
    primary_sources: Optional[set] = None
    if repo_root is not None:
        primary_sources = set()
        try:
            primary_sources.add(Path(repo_root).as_posix())
        except Exception:  # noqa: BLE001
            pass
        try:
            primary_sources.add(Path(repo_root).resolve().as_posix())
        except Exception:  # noqa: BLE001
            pass
        if not primary_sources:
            primary_sources = None  # unresolvable → scoping off (conservative)

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
            n = _count_stale_in_collection(
                coll,
                current_revision,
                path_prop=_PROBE_PATH_PROP.get(base, "file_path"),
                reachable_fn=reachable_fn,
                primary_sources=primary_sources,
                # v0.2.75 (P1b-1): the classifier's ignore-set gate needs the
                # per-project `.claude` decision so a user project's stale
                # `.claude/**` rows (walk-excluded there) are not counted
                # owed forever, while the orchestrator root (which indexes
                # `.claude/` as first-party source) keeps counting them.
                index_dot_claude=index_dot_claude,
            )
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


def classify_stale_kinds(
    project_name: str,
    *,
    current_revision: Optional[int] = None,
    floor_revision: int = 1,
    analyzer_path: Optional[Path] = None,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> Optional[dict]:
    """v0.2.82 (WP-2 task 3): split the owed rows into ``embed_owed`` vs
    ``stamp_owed`` for the driver's convergence REPORT (reporting only).

    The owed-gate semantics are UNCHANGED: a ``stamp_owed`` row still counts as
    owed work (the pass that stamps it is cheap). This just lets the driver say
    WHY the remaining work is owed — a metadata-only revision bump produces
    ``stamp_owed`` rows (cheap ``data.update``, no re-embed), whereas a genuine
    embedding-space change produces ``embed_owed`` rows. Uses the SAME guards
    classifier (:func:`codegraph_guards.classify_stale_kind`) the analyzer's
    per-row decision uses, so the report can never disagree with the walk.

    Returns ``{"embed_owed": N, "stamp_owed": M}`` (across the three
    file-anchored probe collections), or ``None`` when undeterminable (Weaviate
    down / prefix unresolvable). ``floor_revision`` defaults to 1 (the current
    ``_EMBED_SPACE_COMPATIBLE_FROM_REVISION``); callers that resolve a bumped
    floor pass it explicitly.
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

    split = {"embed_owed": 0, "stamp_owed": 0}
    try:
        for base in _RESYNC_PROBE_BASES:
            coll_name = f"{prefix}_{base}"
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    continue
                coll = client.collections.get(coll_name)
            except Exception as exc:  # noqa: BLE001 — undeterminable
                logger.warning(
                    "codegraph stale-kind: cannot open %s: %s", coll_name, exc
                )
                return None
            try:
                for obj in coll.iterator(return_properties=["embed_revision"]):
                    props = getattr(obj, "properties", None) or {}
                    kind = _guards_classify_stale_kind(
                        props.get("embed_revision"),
                        current_revision=int(current_revision),
                        floor_revision=int(floor_revision),
                    )
                    if kind in split:
                        split[kind] += 1
            except Exception as exc:  # noqa: BLE001 — undeterminable
                logger.warning(
                    "codegraph stale-kind: scan failed on %s: %s", coll_name, exc
                )
                return None
        return split
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def count_cleanup_owed_rows(
    project_name: str,
    *,
    repo_root: Path,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> Optional[int]:
    """A1 (v0.2.76 / CG-4): count PRIMARY-source rows whose file is gone from
    disk — REGARDLESS of revision.

    Why this exists (the CG-4 pure-deletion inertness gap): a ``git rm`` of a
    file whose rows are already at the CURRENT ``embed_revision`` leaves those
    rows embed-converged. ``count_stale_rows`` (which counts only ``"owed"``
    rows) correctly returns 0 for them — a re-walk cannot embed-converge a
    deleted file. So when such a deletion is the ONLY change, the resync gate
    sees a positive-zero stale count and returns ``not_owed``, and the
    analyzer (whose CG-4 whole-repo sweep, ``_clear_deleted_primary_rows``,
    WOULD purge those rows) is never spawned. The deleted file's rows keep
    surfacing in ``search_code_graph`` until the next real analyze — the same
    immortal-ish shape v0.2.75 P1b killed elsewhere.

    This probe closes the gap on the GATE side (classifier semantics stay
    intact — ``classify_row`` still calls a converged deleted row
    ``"not_owed"``): it counts exactly the rows the sweep would delete via the
    ONE shared predicate :func:`is_deleted_primary_row`. When it returns > 0
    the gate proceeds to spawn (the whole-repo walk runs the sweep). Scoped to
    the three file-anchored collections (``_RESYNC_PROBE_BASES``), primary
    source only (extra-path rows converge on their own root's walk — B1),
    reachability memoized once per probe run.

    Returns the count (``>= 0``), or ``None`` when undeterminable (Weaviate
    down, prefix unresolvable, a collection unprobeable) — the caller treats
    ``None`` conservatively (never a wrong "nothing to clean up").
    """
    if not project_name or repo_root is None:
        return None
    prefix = _collection_prefix(project_name)
    if prefix is None:
        return None

    reachable_fn = _make_reachability_filter(repo_root)
    # Primary-source scoping — the raw + resolved POSIX forms of the root
    # (mirrors count_stale_rows / the analyzer sweep's B1 scoping). Empty →
    # scoping off (conservative: judge every path-bearing row's reachability).
    primary_sources: Optional[set] = set()
    try:
        primary_sources.add(Path(repo_root).as_posix())
    except Exception:  # noqa: BLE001
        pass
    try:
        primary_sources.add(Path(repo_root).resolve().as_posix())
    except Exception:  # noqa: BLE001
        pass
    if not primary_sources:
        primary_sources = None

    own_client = False
    if client is None:
        client = _build_client(weaviate_url, grpc_port)
        if client is None:
            return None
        own_client = True

    try:
        total = 0
        for base in _RESYNC_PROBE_BASES:
            coll_name = f"{prefix}_{base}"
            path_prop = _PROBE_PATH_PROP.get(base, "file_path")
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    continue
                coll = client.collections.get(coll_name)
            except Exception as exc:  # noqa: BLE001 — undeterminable
                logger.warning(
                    "codegraph cleanup-owed: cannot open %s: %s", coll_name, exc
                )
                return None
            # Read the path + project_source (when the class carries it — an
            # absent property 500s the iterator on schema variants, same
            # defensive probe count_stale_rows / the analyzer sweep use).
            read_props = [path_prop]
            want_source = primary_sources is not None
            if want_source:
                try:
                    present = {p.name for p in coll.config.get().properties}
                    if "project_source" in present:
                        read_props.append("project_source")
                    else:
                        want_source = False
                except Exception:  # noqa: BLE001 — probe best-effort
                    want_source = False
            try:
                for obj in coll.iterator(return_properties=read_props):
                    props = getattr(obj, "properties", None) or {}
                    if is_deleted_primary_row(
                        props,
                        repo_root,
                        path_prop=path_prop,
                        primary_sources=primary_sources if want_source else None,
                        reachable_fn=reachable_fn,
                    ):
                        total += 1
            except Exception as exc:  # noqa: BLE001 — undeterminable
                logger.warning(
                    "codegraph cleanup-owed: scan failed on %s: %s",
                    coll_name, exc,
                )
                # An incomplete scan must never authorise a "nothing to clean"
                # conclusion — return None (conservative).
                return None
        return total
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


# ── v0.2.82 (Task 4, user scope-add — overrides plan D3): file_path backfill ──
# for legacy anchor-less CodeAPI / CodeInteraction rows.
#
# WHY (rejecting D3's lazy convergence): pre-v0.2.82 CodeAPI/CodeInteraction
# rows carried NO `file_path`, so the prune anchor-resolution could not reap the
# orphaned rows of a deleted file. Plan D3 accepted "they converge when their
# file next changes". The user rejected that: updating users must get old rows
# migrated to the anchored format automatically. This backfill resolves each
# anchor-less row's `file_path` from its stored REFERENCE to a file-anchored
# collection (which DOES carry file_path/path) and PATCHes it in.
#
# Reference map (VERIFIED against the schema blocks, analyze_code_graph.py
# ~2062-2115 as merged):
#   CodeAPI         → `handler`         → CodeFunction (has `file_path`)
#   CodeInteraction → `source_function` → CodeFunction (has `file_path`)
#                     `source_module`   → CodeModule   (has `path`) — fallback
#
# PATCH-only + content-hash-EXCLUDED: `file_path` is in the analyzer's
# `_CONTENT_HASH_EXCLUDE`, so writing it NEVER perturbs a matched row's content
# hash → ZERO re-writes / re-embeds (a call-count test pins this). Rows whose
# reference is absent/unresolvable are LEFT + counted (conservative: never guess
# an anchor). This is not "legacy support" — it is a metadata migration the
# WP-3 post-build backfill rider runs automatically on every non-root update.
_ANCHOR_BACKFILL_REFS: dict = {
    # base → ordered list of (reference_property, target_path_property). The
    # first reference that resolves to a non-empty path wins.
    "CodeAPI": [("handler", "file_path")],
    "CodeInteraction": [
        ("source_function", "file_path"),
        ("source_module", "path"),
    ],
}


def _backfill_anchor_file_paths(
    client,
    prefix: str,
    *,
    counts: dict,
) -> tuple:
    """v0.2.82 (Task 4): PATCH `file_path` onto anchor-less CodeAPI/Interaction
    rows by resolving their stored reference to a file-anchored collection.

    Mutates ``counts`` in place (``{collection_name: rows_updated}``) and returns
    ``(backfilled, unresolvable)`` totals for the machine-readable CLI line.

    Idempotent (rows that already carry a non-empty file_path are skipped by the
    cheap NULL-probe gate); per-collection + per-row soft-fail; NO global
    timeout. A reference read uses ``QueryReference`` with an inline
    ``return_properties=[<target path prop>]`` so the referenced row's path
    arrives with the API/Interaction row in ONE iterator pass. Unresolvable rows
    (no reference, dangling target, target lacks a path) are LEFT + counted.
    """
    backfilled = 0
    unresolvable = 0
    try:
        from weaviate.classes.query import Filter, QueryReference
    except Exception as exc:  # noqa: BLE001
        logger.warning("codegraph anchor-backfill: weaviate query API unavailable: %s", exc)
        return backfilled, unresolvable

    for base, ref_specs in _ANCHOR_BACKFILL_REFS.items():
        coll_name = f"{prefix}_{base}"
        try:
            if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                continue
            coll = client.collections.get(coll_name)

            # Cheap gate: a collection with no missing-file_path row is skipped
            # entirely (steady-state cost ≈ one point query per collection).
            # `file_path` may not even be a schema property on a pre-rider-a
            # class — probe failure → scan anyway (fail-open toward the work).
            try:
                probe = coll.query.fetch_objects(
                    filters=Filter.by_property("file_path").is_none(True),
                    limit=1,
                )
                if not getattr(probe, "objects", None):
                    counts[coll_name] = 0
                    continue
            except Exception:  # noqa: BLE001 — unindexed / absent prop → scan
                pass

            # One combined reference query per spec: fetch the row + its target's
            # path inline. Build the QueryReference list once.
            query_refs = [
                QueryReference(link_on=ref_prop, return_properties=[target_prop])
                for ref_prop, target_prop in ref_specs
            ]

            updated = 0
            left = 0
            for obj in coll.iterator(
                return_properties=["file_path"],
                return_references=query_refs,
            ):
                props = getattr(obj, "properties", None) or {}
                if props.get("file_path"):
                    continue  # already anchored (idempotent)
                resolved_path = _resolve_anchor_from_refs(obj, ref_specs)
                if not resolved_path:
                    left += 1
                    continue
                try:
                    coll.data.update(
                        uuid=obj.uuid, properties={"file_path": resolved_path},
                    )
                    updated += 1
                except Exception as exc:  # noqa: BLE001 — per-row soft-fail
                    logger.warning(
                        "codegraph anchor-backfill: update failed on %s/%s: %s",
                        coll_name, obj.uuid, exc,
                    )
                    left += 1
            counts[coll_name] = updated
            backfilled += updated
            unresolvable += left
            if updated or left:
                logger.info(
                    "codegraph anchor-backfill: %s — %d anchored, %d unresolvable",
                    coll_name, updated, left,
                )
        except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
            logger.warning("codegraph anchor-backfill: %s failed: %s", coll_name, exc)
    return backfilled, unresolvable


def _resolve_anchor_from_refs(obj, ref_specs) -> str:
    """Resolve the first non-empty target path from a row's references.

    ``obj.references`` is a dict ``{ref_prop: [ref_obj, ...]}`` where each
    ``ref_obj.properties`` carries the inline-fetched target path. Returns the
    first non-empty path in ``ref_specs`` order, or ``""`` when none resolve
    (dangling reference, missing target, target lacks a path). Never raises.
    """
    refs = getattr(obj, "references", None) or {}
    if not isinstance(refs, dict):
        return ""
    for ref_prop, target_prop in ref_specs:
        try:
            targets = refs.get(ref_prop)
            objects = getattr(targets, "objects", None) if targets is not None else None
            if objects is None and isinstance(targets, list):
                objects = targets
            for target in objects or []:
                tprops = getattr(target, "properties", None) or {}
                path = tprops.get(target_prop)
                if path:
                    return str(path)
        except Exception:  # noqa: BLE001 — a bad ref shape → try the next spec
            continue
    return ""


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
    # NOTE (v0.2.82 Task 4): the anchor `file_path` backfill for CodeAPI/
    # CodeInteraction is INDEPENDENT of the is_test/doc helpers, so we do NOT
    # early-return when both are None — the client is still opened and the
    # anchor pass still runs. Only when BOTH the metadata helpers are absent do
    # we skip the metadata loop (via the per-half `is not None` guards below).

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

        _run_metadata_half = is_test_fn is not None or extract_fn is not None
        for base, (path_prop, body_prop) in (
            _BACKFILL_BASES.items() if _run_metadata_half else []
        ):
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

        # v0.2.82 (Task 4): anchor `file_path` onto legacy CodeAPI/Interaction
        # rows from their stored reference to a file-anchored collection. Runs
        # unconditionally (independent of the is_test/doc helpers). The two
        # totals are surfaced under reserved summary keys the CLI prints as a
        # machine-readable line.
        anchored, unresolvable = _backfill_anchor_file_paths(
            client, prefix, counts=counts,
        )
        counts["_file_path_backfilled"] = anchored
        counts["_file_path_unresolvable"] = unresolvable
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
    like every non-install.py deferral writer; soft-fail with a log line.

    v0.2.83 (WP-B1): routed through the ONE emitter home
    (vco_lib.deferral_emit) — locked read-modify-write, foreign entries
    preserved. ``resolve_conditions`` returns how many of the given IDs were
    present (and dropped), so the "resolved" log line still fires only when the
    entry actually existed."""
    try:
        from vco_lib.deferral_emit import resolve_conditions

        folder = Path(repo_root)
        removed = resolve_conditions(folder, (_CONDITION_ID,), log=logger)
        if removed:
            logger.info("resync driver: resolved persisted resync deferral")
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("resync driver: could not resolve deferral: %s", exc)


def _record_unconverged_deferral(
    repo_root: Path,
    project_name: str,
    counts: Optional[dict],
    resume_cmd: str,
) -> None:
    """Persist the one-time unconverged entry. Read-merge-write; soft-fail.

    v0.2.83 (WP-B1): routed through the ONE emitter home
    (vco_lib.deferral_emit) — locked read-modify-write, foreign entries
    preserved."""
    try:
        entry = build_unconverged_deferral(project_name, counts, resume_cmd)
        if entry is None:
            return
        from vco_lib.deferral_emit import emit

        folder = Path(repo_root)
        emit(folder, entry, log=logger)
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
    index_dot_claude: bool = True,
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
    # v0.2.84 (D4/P1): pre-analyze identity sweep. Mirrors the Rust pre-build
    # rationale (codegraph.rs::migrate_stale_identities_for_build): migrate any
    # stale ``project`` identity onto the canonical prefix BEFORE the full walk
    # so a ``--prune-stale`` pass cannot reap old-identity rows and so we never
    # keep two writers. Probe-first + soft-fail inside the helper — a no-op when
    # the prefix is already single-identity (the common converged case pays one
    # cheap aggregate). Never gates the analyzer.
    try:
        identity_sweep_if_stale(Path(repo_root), project_name)
    except Exception as exc:  # noqa: BLE001 — sweep must never block the walk
        print(f"[resync-driver] identity sweep raised (soft-fail): {exc}",
              flush=True)

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
        counts = count_stale_rows(
            project_name,
            analyzer_path=Path(analyzer_path),
            repo_root=Path(repo_root),  # R3: exclude orphan (deleted-file) rows
            index_dot_claude=index_dot_claude,  # P1b-1: same gate as the spawn
        )
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
    # v0.2.82 (WP-2 task 3): report WHY the remaining rows are owed —
    # embed_owed (a re-walk re-embeds) vs stamp_owed (a cheap metadata patch).
    # Reporting ONLY: the owed-gate semantics are unchanged (stamp-owed still
    # counts as owed above). Soft-fail — a report-split failure never changes
    # the driver's exit or the ledger entry.
    split_desc = ""
    if counts is not None and sum(counts.values()) > 0:
        try:
            split = classify_stale_kinds(
                project_name, analyzer_path=Path(analyzer_path),
            )
            if split is not None:
                split_desc = (
                    f", embed_owed={split.get('embed_owed', 0)} "
                    f"stamp_owed={split.get('stamp_owed', 0)}"
                )
        except Exception as exc:  # noqa: BLE001 — reporting must not crash the child
            logger.debug("resync driver: stale-kind split failed: %s", exc)
    print(
        f"[resync-driver] NOT converged (analyzer exit {rc}, "
        f"stale rows: {stale_desc}{split_desc})",
        flush=True,
    )
    _record_unconverged_deferral(repo_root, project_name, counts, resume_cmd)
    return 0


def _probe_stale_identity_count(
    client, prefix: str, canonical: str,
) -> Optional[int]:
    """Cheap gate for :func:`identity_sweep_if_stale`: total count of rows whose
    ``project`` ≠ ``canonical`` across the 5 ``<prefix>_<base>`` classes, via a
    filtered aggregate (the cheapest read — no per-row scan). ``None`` when the
    probe is undeterminable on EVERY class (Weaviate down / prefix unresolvable
    / aggregate shape unsupported) so the caller never treats "can't tell" as
    "converged". ``0`` means every class positively reported zero stale rows.

    Soft-fail per class: an aggregate error on one class does not poison the
    others (it contributes ``undeterminable``, not ``0``).
    """
    try:
        from weaviate.classes.query import Filter
    except Exception as exc:  # noqa: BLE001 — no client lib → undeterminable
        logger.warning("identity sweep probe: Filter unavailable: %s", exc)
        return None
    any_determinable = False
    total = 0
    for base in _CODEGRAPH_BASES:
        coll_name = f"{prefix}_{base}"
        try:
            if (
                hasattr(client.collections, "exists")
                and not client.collections.exists(coll_name)
            ):
                # Absent class = zero stale rows there (positively determinable).
                any_determinable = True
                continue
            coll = client.collections.get(coll_name)
            flt = Filter.by_property("project").not_equal(canonical)
            agg = coll.aggregate.over_all(filters=flt, total_count=True)
            count = getattr(agg, "total_count", None)
            if count is not None:
                any_determinable = True
                total += int(count)
        except Exception as exc:  # noqa: BLE001 — one class undeterminable
            logger.debug(
                "identity sweep probe: aggregate %s failed: %s", coll_name, exc
            )
            continue
    return total if any_determinable else None


def identity_sweep_if_stale(
    repo_root: Path, project_name: str,
) -> int:
    """v0.2.84 (D4/P1): migrate any STALE code-graph ``project`` identity for
    ``project_name`` onto its canonical prefix, if (and only if) stale rows
    exist. Returns the number of rows moved+deduped (0 when nothing was stale).

    Owned by the root's install.py --update flow (via the thin shim
    ``install._trigger_codegraph_identity_sweep``) AND run pre-analyze inside
    :func:`run_resync_and_verify`. Distinct from the R-6 embed-resync gate:
    identity-stale rows can be embed-revision-CURRENT (a renamed project's rows
    carry the old ``project`` value but a fresh ``embed_revision``), so the
    owed-probe would report "not owed" while the dual identity persists — this
    is exactly why the sweep must run UNCONDITIONALLY, not behind that gate.

    Mechanism (all in :mod:`vco_lib.codegraph_vector_copy`, the ONE identity
    engine): resolve the canonical prefix → cheap filtered-aggregate probe
    (``project != canonical``) → only on a POSITIVE-nonzero probe run the real
    :func:`~vco_lib.codegraph_vector_copy.sweep_stale_identities`. Soft-fail
    throughout — a missing helper / Weaviate-down / prefix-unresolvable
    condition logs and returns 0 (never crashes the caller; never guesses).

    On a real migration (moved+deduped > 0) it records an auto-resolution audit
    row (``codegraph_identity_migrated``) so the healing leaves a visible trail
    — a loud log line + a JSONL entry under ``<repo_root>/.claude/logs``.
    """
    if not project_name:
        return 0
    try:
        from vco_lib.codegraph_vector_copy import sweep_stale_identities
    except Exception as exc:  # noqa: BLE001 — missing helper must not wedge caller
        logger.warning("identity sweep: engine unavailable: %s", exc)
        return 0

    prefix = _collection_prefix(project_name)
    if not prefix:
        # Prefix unresolvable → do NOTHING (never guess a prefix — the
        # conservative default the whole module already uses).
        return 0
    # For code-graph the analyzer stamps ``project == <collection prefix>`` (the
    # canonical identity), so the sweep's ``canonical`` IS the prefix here.
    canonical = prefix

    client = _build_client()
    if client is None:
        return 0
    moved_deduped = 0
    try:
        probe = _probe_stale_identity_count(client, prefix, canonical)
        if probe == 0:
            # Positively single-identity — cheap converged path, no scan/migrate.
            logger.info(
                "identity sweep: %s already single-identity (%r) — nothing owed",
                prefix, canonical,
            )
            return 0
        # probe is None (undeterminable) OR > 0: fall through to the engine.
        # The engine's own enumerate scan is the authoritative discovery; on an
        # undeterminable probe we proceed (conservative: never skip on
        # uncertainty), on a positive probe we know there is work.
        summaries = sweep_stale_identities(
            prefix, canonical, client=client, dry_run=False,
        )
        moved = sum(s.moved for s in summaries)
        deduped = sum(s.deduped for s in summaries)
        left = sum(s.left for s in summaries)
        failures = sum(s.failures for s in summaries)
        moved_deduped = moved + deduped
        if summaries:
            logger.info(
                "identity sweep: %s — %d identit%s migrated onto %r "
                "(moved=%d deduped=%d left=%d failures=%d)",
                prefix, len(summaries), "y" if len(summaries) == 1 else "ies",
                canonical, moved, deduped, left, failures,
            )
        if moved_deduped > 0:
            # B-F9 (no silent mutations): a real identity migration is a healing
            # action on the user's data — record the audit trail.
            try:
                from vco_lib.deferral_emit import record_auto_resolution
                record_auto_resolution(
                    Path(repo_root),
                    "codegraph_identity_migrated",
                    action="migrated stale code-graph identity",
                    detail=(
                        f"prefix={prefix} → canonical={canonical}: "
                        f"moved={moved} deduped={deduped} left={left} "
                        f"failures={failures}"
                    ),
                    log=logger,
                )
            except Exception as exc:  # noqa: BLE001 — audit is best-effort
                logger.warning(
                    "identity sweep: could not record auto-resolution: %s", exc
                )
    except Exception as exc:  # noqa: BLE001 — sweep must never crash the caller
        logger.warning("identity sweep: raised (soft-fail): %s", exc)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return moved_deduped


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
            stale_counts = count_stale_rows(
                project_name,
                analyzer_path=analyzer,
                repo_root=repo_root,  # R3: exclude orphan (deleted-file) rows
                index_dot_claude=index_dot_claude,  # P1b-1 classifier gate
            )
        except Exception as exc:  # noqa: BLE001 — probe must never block
            logger.warning("codegraph resync: owed-probe raised: %s", exc)
            stale_counts = None
        if stale_counts is not None and sum(stale_counts.values()) == 0:
            # A1 (v0.2.76 / CG-4): a positive-zero embed-stale count does NOT
            # mean "nothing owed". A file deleted from disk while its rows were
            # at the current revision leaves them embed-converged (classify_row
            # → not_owed, correctly), so the stale count is 0 — but those rows
            # still serve stale search results until the analyzer's whole-repo
            # CG-4 sweep purges them, and the gate would never spawn the
            # analyzer for a pure deletion. Before short-circuiting not_owed,
            # probe for such deleted-primary rows; ANY positive count means the
            # sweep is owed → fall through to spawn (the whole-repo walk runs
            # it). Undeterminable (None) is conservative: proceed like the
            # pre-fix path (never skip on uncertainty). repo_root is required
            # for the reachability test — without it we cannot judge deletion,
            # so we keep the original not_owed (the reachability-gated stale
            # count already ran the same way).
            cleanup_owed = None
            if repo_root is not None:
                try:
                    cleanup_owed = count_cleanup_owed_rows(
                        project_name, repo_root=repo_root
                    )
                except Exception as exc:  # noqa: BLE001 — probe must never block
                    logger.warning(
                        "codegraph resync: cleanup-owed probe raised: %s", exc
                    )
                    cleanup_owed = None
            if not (cleanup_owed and cleanup_owed > 0):
                return ResyncTriggerResult(
                    status="not_owed",
                    message=(
                        f"no resync owed for {project_name} — all rows at the "
                        "current embed revision"
                    ),
                )
            logger.info(
                "codegraph resync: %d deleted-primary row(s) owed a CG-4 "
                "sweep (embed-stale count is 0) — spawning", cleanup_owed
            )
        elif stale_counts:
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
    # P1b-1 (v0.2.75): forward the `.claude` decision so the driver's
    # post-walk verify probe classifies with the SAME gate as this spawn's
    # owed-probe (otherwise spawn says "owed" / verify says "converged" —
    # or vice versa — for `.claude/**` rows on user projects).
    if index_dot_claude:
        argv.append("--index-dot-claude")

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
        # Handle kept in _DETACHED_CHILDREN: suppresses the Popen destructor's
        # ResourceWarning — this child deliberately outlives the caller.
        _DETACHED_CHILDREN.append(
            subprocess.Popen(prune_argv, **popen_kwargs)  # noqa: S603 — argv is ours
        )
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
        # Handle kept alive — see _DETACHED_CHILDREN.
        _DETACHED_CHILDREN.append(
            subprocess.Popen(backfill_argv, **popen_kwargs)  # noqa: S603 — argv is ours
        )
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
            # Handle kept alive — see _DETACHED_CHILDREN.
            _DETACHED_CHILDREN.append(
                subprocess.Popen(summary_argv, **popen_kwargs)  # noqa: S603 — argv is ours
            )
    except Exception as exc:  # noqa: BLE001 — summary rider is best-effort
        logger.warning("code-summary spawn failed: %s", exc)

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 — argv is ours
        # Handle kept alive — see _DETACHED_CHILDREN (the local `proc` goes
        # out of scope when this function returns; only the pid is returned).
        _DETACHED_CHILDREN.append(proc)
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


def log_vectorless_degrade(
    insert_params: "dict", failure: "Exception | None", log=logger
) -> None:
    """v0.2.77 5c task 4: emit ONE audit WARNING for a code-graph object that
    degraded to vectorless (embed_revision=0) after the bounded 503 backoff
    still yielded no vector. Extracted so the analyzer's
    ``_run_deferred_embed_into`` stays a few lines (P2f ratchet).

    Best-effort: never raises (logging must not wedge a write). ``insert_params``
    is the object's insert kwargs (``properties`` holds the identity fields);
    ``failure`` is the embed exception, or None when the embedder simply
    returned no vector.
    """
    try:
        props = insert_params.get("properties") if isinstance(insert_params, dict) else None
        ident = "?"
        if isinstance(props, dict):
            ident = str(
                props.get("full_name") or props.get("name")
                or props.get("path") or props.get("file_path") or "?"
            )
        reason = (
            f"embed failed after 503 backoff ({failure})"
            if failure is not None else "embedder returned no vector"
        )
        log.warning(
            "code-graph object degraded to VECTORLESS (embed_revision=0): "
            "%s — %s; will re-embed on next walk", ident, reason,
        )
    except Exception:  # noqa: BLE001 — logging must never break the write
        pass


def union_stale_into_changed(
    source_root: "Path",
    all_files: "list[Path]",
    changed_files: "list[Path]",
    stale_set: "frozenset | set | None",
) -> "tuple[list[Path], int]":
    """v0.2.77 5c task 5 (5c-v): add stale-revision files back into an
    incremental changed-set so ``--incremental`` can HEAL vectorless rows.

    Pure helper (no ``self``, no I/O) extracted from the analyzer so the
    monolith stays flat (P2f ratchet) and the union logic is independently
    testable. The analyzer's ``_union_stale_into_changed`` is a thin shim that
    resolves the per-run stale set and delegates here.

    THE GAP: ``_filter_changed_files`` keeps only git-diff-changed files, so a
    file whose content is UNCHANGED but which owns a stale/vectorless
    (``embed_revision`` NULL or 0) code-graph row never re-walks under
    ``--incremental`` — it never reaches ``_get_existing_module`` (where the R-1
    stale-file probe fires), so before this only a FULL walk could re-embed the
    89 vectorless rows the 5c incident wrote.

    THE FIX: any file in ``all_files`` whose path RELATIVE TO ``source_root``
    (POSIX) is in ``stale_set`` is UNIONed into ``changed_files`` even when
    git-diff didn't flag it. Rows are stored with ``path``/``file_path`` =
    ``file.relative_to(source_root).as_posix()`` (per-root relativisation), so
    testing membership with the SAME relativisation matches only rows belonging
    to THIS root (primary rows vs extra-path rows key off their own root).

    FAIL-OPEN: a ``None`` / empty ``stale_set`` → return ``(changed_files, 0)``
    unchanged (today's behaviour exactly). Never raises: a per-file relativise
    error is skipped.

    Returns ``(unioned_files, added_count)``. Order: original changed order
    preserved, newly-added stale files appended in find-order (deterministic).
    """
    if not stale_set:
        return changed_files, 0
    already = set(changed_files)
    result = list(changed_files)
    added = 0
    for f in all_files:
        if f in already:
            continue
        try:
            rel = f.relative_to(source_root).as_posix()
        except Exception:  # noqa: BLE001 — odd root: cannot key, skip
            continue
        if rel in stale_set:
            result.append(f)
            already.add(f)
            added += 1
    return result, added


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
            index_dot_claude=args.index_dot_claude,
        )
    elif args.backfill_metadata:
        logging.basicConfig(level=logging.INFO)
        counts = backfill_codegraph_metadata(args.project)
        # The reserved `_file_path_*` summary keys are totals, not per-collection
        # row counts — exclude them from the updated-rows sum.
        anchored = int(counts.pop("_file_path_backfilled", 0)) if counts else 0
        unresolvable = int(counts.pop("_file_path_unresolvable", 0)) if counts else 0
        total = sum(counts.values()) if counts else 0
        logger.info("codegraph metadata backfill complete: %d row(s) updated (%s)",
                    total, counts)
        # v0.2.82 (Task 4): machine-readable anchor-backfill summary line WP-3's
        # rider (and the user) can parse from the CLI output.
        print(
            f"file_path_backfilled={anchored} unresolvable={unresolvable}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
