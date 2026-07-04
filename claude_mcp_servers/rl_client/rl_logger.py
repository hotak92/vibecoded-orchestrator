# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is VENDORED into paid-modules/vct-rl-reranker/rl_logger.py
# Both copies MUST stay byte-identical. The byte-identity test in
# tests/test_vendored_file_sync.py enforces this on every CI run.
# To re-sync after an edit: ./scripts/sync-vendored-files.sh
"""
Global RL data logger for retrieval training data collection.

Logs retrieval events (query + nodes presented with scores) and citation
feedback (which nodes were actually cited by agents) to a single JSONL file.

Default path: ~/.claude/retrieval_rl_data/rl_events.jsonl

This file accumulates data from ALL projects on the machine, enabling:
  1. Offline RL batch training / replay-buffer replay.
  2. Cross-project node quality signals.
  3. Quality verification for synthetic data generation.

Event schema
------------
retrieval event::

    {
        "event":      "retrieval",
        "ts":         "2026-02-26T12:00:00+00:00",   # ISO-8601 UTC
        "project":    "MyProject",
        "task_id":    "abc123",                       # links to citation event
        "session_id": "sess_xyz",                     # optional
        "task_type":  "implementation",
        "query":      "...",                          # truncated to 2000 chars
        "query_emb":  [0.1234, ...],                  # optional: 1024-dim query embedding
        "nodes": [
            {
                "title": "Foo",
                "score": 0.812,
                "tier":  "top_k",
                "emb":   [0.1234, ...]               # optional: node embedding (1024-dim)
            },
            {"title": "Bar", "score": 0.341, "tier": "extra_reference"}
        ]
    }

citation event::

    {
        "event":       "citation",
        "ts":          "2026-02-26T12:01:30+00:00",
        "project":     "MyProject",
        "task_id":     "abc123",
        "task_type":   "implementation",
        "citations":   {"Foo": true, "Bar": false},   # None = inconclusive
        "cosine_sims": {"Foo": 0.812, "Bar": 0.341}  # optional: cos(node, agent_output)
    }

The same ``task_id`` links a retrieval event with its citation event.
Stored embeddings are frozen at scoring time (Weaviate node content may change
over time, so we record the vector actually used during training).
``cosine_sims`` in citation events lets the offline trainer reconstruct the
same analog advantage rewards that were used for online learning, without
access to the original agent output text.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rotation defaults: 10 000 events ≈ 50-80 MB with embeddings.
_DEFAULT_MAX_EVENTS = 10_000
_DEFAULT_MAX_ARCHIVES = 5


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _round_emb(emb: "list[float]") -> "list[float]":
    """Round embedding to 4 decimal places to reduce JSONL file size."""
    return [round(float(v), 4) for v in emb]


def serialize_node_record(
    n: "dict[str, Any]",
    *,
    include_links: bool = False,
    include_shown_rank: bool = False,
    include_chunks_matched: bool = False,
    include_best_chunk_number: bool = False,
    include_code_path_fields: bool = False,
    round_scalars: bool = False,
) -> "dict[str, Any]":
    """Serialize ONE candidate node dict into a per-node telemetry record.

    Single home for the per-node record shape shared by
    ``RLTelemetryWriter._build_retrieval_payload`` (the consented-upload
    queue payload) and ``RLTelemetryWriter._build_v3_retrieval_event`` (the
    launcher.db v3 event). Prior to this extraction those two loops inlined a
    near-identical copy — a semi-duplication that had already drifted (the v3
    builder's docstring claimed field parity with the queue builder while
    actually carrying ``links`` + ``best_chunk_number`` + the RL-2 code-path
    fields the queue builder omits). Consolidating here makes the field set
    and its insertion ORDER a single source of truth so the two callers cannot
    drift again.

    NOTE the legacy ``RLDataLogger.log_retrieval`` JSONL loop is deliberately
    NOT routed here: it emits ``links`` AFTER the ``cos_*`` block (vs BEFORE
    here) and rounds ``score`` + ``cos_*`` — a genuinely different serialization
    with a different byte order. Folding it in would change its on-disk bytes,
    so it keeps its own loop (documented at that call-site). The ``round_scalars``
    flag below encodes that precision difference for parity/testing even though
    no live caller currently sets it.

    **Byte/insertion-order contract**: JSON preserves dict insertion order, so
    the on-wire/on-disk record is order-sensitive. Field insertion order here
    is FIXED:
    ``title, score, tier, emb, linked_embs, linked_type_names,
    node_type, [links], [cos_qn, cos_ql, cos_nl], [shown_rank],
    [chunks_matched], [best_chunk_number], [collection, file_path,
    rerank_score, boost_delta, boost_signals]``. Bracketed groups are gated by
    the keyword flags.

    **v0.2.73 n_emb payload-dedup**: the node vector is written ONCE, under
    ``emb`` (the field the offline trainer reads). Pre-dedup this record emitted
    the SAME 1024-dim vector twice — as ``emb`` AND ``n_emb`` (the KG enrichment
    mirrors the vector into both keys on the candidate dict), and NO offline
    consumer reads a stored ``n_emb`` (the offline trainer's
    ``extract_samples`` / ``train_epoch`` read ``node.get("emb")`` only; the
    ``n_emb`` reads in ``retrieval_rl.py`` are the ONLINE /rl_update RPC path,
    which consumes the in-process ctx dict, never this serialized record). So
    ``n_emb`` was ~50% dead write weight on KG events. A node that carries only
    ``n_emb`` (code retrievals) is promoted to ``emb`` here so it becomes
    trainable rather than silently skipped.

    Each optional field is emitted ONLY when truthy/non-None in ``n`` (same
    ``if n.get(...)`` / ``is not None`` guards the original loops used), so a
    sparse node dict yields the same sparse record.

    Args:
        n: One candidate node dict (as produced by the search-pipeline
           reducer ``_build_log_nodes``).
        include_links: emit ``links`` (first 10, str-coerced). v3 event +
           legacy JSONL include it; the queue payload historically omits it.
        include_shown_rank: emit ``shown_rank`` (int). RL-1 — post-rerank
           shown order. Both hub-side builders include it; the legacy JSONL
           path predates it and omits it.
        include_chunks_matched: emit ``chunks_matched`` (int). RL-7.
        include_best_chunk_number: emit ``best_chunk_number`` (int). RL-7 —
           only the v3 event carries it.
        include_code_path_fields: emit the RL-2 code-retrieval fields
           (``collection``, ``file_path``, ``rerank_score``, ``boost_delta``,
           ``boost_signals``). Only the v3 event carries these.
        round_scalars: round ``score`` and the ``cos_*`` features to 4 places.
           The legacy JSONL writer rounds them (file-size); the hub-side
           builders keep full precision (launcher.db stores JSON verbatim and
           the offline trainer wants the unrounded scalar). Embeddings are
           ALWAYS rounded via ``_round_emb`` regardless of this flag — that has
           been true in every caller since v3.
    """
    rec: "dict[str, Any]" = {
        "title": str(n.get("title", "")),
        "score": (
            round(float(n.get("score", 0.0)), 4)
            if round_scalars
            else float(n.get("score", 0.0))
        ),
        "tier": str(n.get("tier", "top_k")),
    }
    # v0.2.73 (n_emb payload-dedup): the node vector is serialized ONCE, under
    # `emb` — the field the offline trainer actually reads
    # (paid-modules/vct-rl-reranker/offline_trainer.py `extract_samples`/
    # `train_epoch` both read `node.get("emb")`; NO offline consumer reads a
    # stored `n_emb`). The KG enrichment site mirrors the SAME vector into both
    # `emb` and `n_emb` on the candidate dict, so pre-dedup every KG retrieval
    # event carried the identical 1024-dim vector TWICE (~50% of the per-node
    # embedding bytes were dead weight). The in-process citation cache + online
    # /rl_update RPC still read `n_emb` off the ctx dict (see
    # search_pipeline `_build_log_nodes` / rl_enrichment) — that path is
    # UNCHANGED; only this WRITTEN-telemetry serialization is deduped. Code
    # retrievals attach the vector as `n_emb`-only; promoting it to `emb` here
    # makes those events trainable (they were silently skipped before, since
    # the trainer never read `n_emb`).
    _node_vec = n.get("emb") or n.get("n_emb")
    if _node_vec:
        rec["emb"] = _round_emb(_node_vec)
    # v3+: MAX_LINKED packed linked-slot embeddings.
    if n.get("linked_embs"):
        rec["linked_embs"] = [_round_emb(e) for e in n["linked_embs"] if e]
    if n.get("linked_type_names"):
        rec["linked_type_names"] = [str(t) for t in n["linked_type_names"]]
    if n.get("node_type"):
        rec["node_type"] = str(n["node_type"])
    if include_links and n.get("links"):
        rec["links"] = [str(lnk) for lnk in n["links"][:10]]
    for field in ("cos_qn", "cos_ql", "cos_nl"):
        val = n.get(field)
        if val is not None:
            rec[field] = round(float(val), 4) if round_scalars else float(val)
    if include_shown_rank and n.get("shown_rank") is not None:
        rec["shown_rank"] = int(n["shown_rank"])
    if include_chunks_matched and n.get("chunks_matched") is not None:
        rec["chunks_matched"] = int(n["chunks_matched"])
    if include_best_chunk_number and n.get("best_chunk_number") is not None:
        rec["best_chunk_number"] = int(n["best_chunk_number"])
    if include_code_path_fields:
        # RL-2 code-path node fields (present only on code retrievals).
        if n.get("collection"):
            rec["collection"] = str(n["collection"])
        if n.get("file_path"):
            rec["file_path"] = str(n["file_path"])
        if n.get("rerank_score") is not None:
            rec["rerank_score"] = float(n["rerank_score"])
        if n.get("boost_delta") is not None:
            rec["boost_delta"] = float(n["boost_delta"])
        if n.get("boost_signals"):
            # code_ranking stamps signals as a dict; keep the shape verbatim
            # (payload_json is stored as-is).
            _sig = n["boost_signals"]
            rec["boost_signals"] = (
                dict(_sig) if isinstance(_sig, dict) else list(_sig)
            )
    return rec


class RLDataLogger:
    """
    Append-only JSONL data logger for RL retrieval training.

    Thread/process safety: each ``_append`` call opens and closes the file with
    a single ``write`` — safe for single-process asyncio use.  For multi-process
    use, JSONL line-level appends are atomic on Linux ext4/xfs (< 4KB write).

    Args:
        log_path: Path to the JSONL log file.
                  Defaults to ``~/.claude/retrieval_rl_data/rl_events.jsonl``.
        project:  Project name tag written to every event.
    """

    DEFAULT_DIR: Path = Path.home() / ".claude" / "retrieval_rl_data"
    DEFAULT_PATH: Path = DEFAULT_DIR / "rl_events.jsonl"

    # Schema versioning. v1 was the pre-2026-05-05 format with no embedding
    # source metadata — events recorded query_emb / per-node emb as raw float
    # lists with no annotation of which model produced them. v2 adds
    # embedding_source + embedding_dim + embedding_model + schema_version on
    # every retrieval event, and schema_version on every citation event.
    # Legacy events must be converted by scripts/migrate_rl_log_v1_to_v2.py
    # before offline_trainer will accept them — there is no runtime fallback.
    #
    # v3 (v0.2.47, 2026-06-04) aligns MCP-side and paid-module-side schemas.
    # Citation events gain per-node `literal_cited` (title appears in ANSWER
    # text via word-boundary regex) and `cross_encoder_cited` (Pro-tier
    # cross-encoder verdict; absent on free / v0.2.9 deferral). Retrieval
    # events gain per-node `n_emb` (best-chunk vector), `linked_embs`
    # (MAX_LINKED packed: extra_chunks_of_same_node + actual_linked_nodes),
    # and `linked_type_names`. These let online + offline training share
    # the SAME `_rl_model.update(...)` inputs byte-identically (per the
    # unified-target formula in vco_lib.rl_training_targets). Pre-v3
    # readers ignore unknown fields; v3 readers default missing fields to
    # all-False / empty (lossless vs pre-v3 behavior).
    SCHEMA_VERSION: int = 3

    def __init__(
        self,
        log_path: "Path | None" = None,
        project: str = "",
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_archives: int = _DEFAULT_MAX_ARCHIVES,
        embedding_source: str = "",
        embedding_dim: int = 0,
        embedding_model: str = "",
    ) -> None:
        self._path = Path(log_path) if log_path else self.DEFAULT_PATH
        self._project = project
        self._max_events = max_events
        self._max_archives = max_archives
        # Embedding metadata stamped on every retrieval event so the offline
        # trainer can filter to events matching the target network's source.
        # See claude_mcp_servers/rl_server/rl_server.py::_resolve_model_tag_and_dim
        # for the canonical (source -> dim) mapping. Empty values are allowed
        # for tests / standalone usage but offline_trainer will reject events
        # missing embedding_source.
        self._embedding_source = embedding_source
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._write_count = 0
        # Check rotation every 100 writes to avoid stat() on every append
        self._rotation_check_interval = 100
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("RLDataLogger: cannot create log directory: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def log_path(self) -> Path:
        """Resolved path to the JSONL log file."""
        return self._path

    def log_retrieval(
        self,
        task_id: str,
        task_type: str,
        query: str,
        nodes: "list[dict[str, Any]]",
        session_id: str = "",
        query_emb: "list[float] | None" = None,
        failure_mode: "str | None" = None,
        failed_collections: "list[str] | None" = None,
    ) -> None:
        """
        Log a retrieval event: query + nodes presented to the agent.

        Args:
            task_id:    Unique task identifier.  Links to the citation event
                        emitted after the task completes.
            task_type:  Agent task category (e.g. "implementation", "research").
            query:      Task description / search query text.
            nodes:      Sequence of ``{title, score, tier}`` dicts.
                        *tier* should be ``"top_k"`` or ``"extra_reference"``.
                        May optionally include ``emb: list[float]`` (node embedding)
                        to enable standalone offline training without re-embedding.
            session_id: Optional orchestrator session ID for run-level grouping.
            query_emb:  Optional query embedding vector (1024-dim,
                        snowflake-arctic-embed2).  Frozen at scoring time so the
                        offline trainer uses the same vector even if the query
                        text has been truncated or the model updated.
            failure_mode: Optional tag identifying a degraded-mode retrieval
                        (e.g. ``"all_collections_schema_missing"``,
                        ``"weaviate_unreachable"``). When set, the offline
                        trainer filters the event out of training-pair
                        construction but still uses it as a query-distribution
                        / failure-rate signal. None = normal successful
                        retrieval. v0.2.24 (RL-defect-2026-05-22).
            failed_collections: Optional list of collection names that
                        failed in the fan-out. Useful for diagnosing
                        per-machine config drift (e.g. a hardcoded shared-KG
                        default that doesn't exist on the user's Weaviate).
        """
        # NOTE — deliberately NOT routed through `serialize_node_record`.
        # This is the LEGACY JSONL writer (test-only since the v0.2.47 hub
        # cutover). It is a genuinely different serialization concern from the
        # two live hub builders in telemetry_writer.py, NOT a copy that drifted:
        #   * different scalar precision — it rounds `score` + `cos_*` for
        #     file-size (the hub builders keep full precision for the trainer);
        #   * different FIELD ORDER — here `links` is emitted AFTER the `cos_*`
        #     block, whereas the hub builders emit `links` BEFORE it. JSON
        #     preserves insertion order, so folding this loop into the shared
        #     helper (which uses the hub order) would change this writer's
        #     on-disk bytes for any node carrying both `links` and `cos_*`.
        # Preserving this loop's FIELD ORDER + scalar rounding keeps the legacy
        # JSONL corpus byte-stable. v0.2.73 (n_emb payload-dedup): the node
        # vector is serialized ONCE, under `emb` — the field the offline trainer
        # reads. Pre-dedup a KG node carried the SAME vector under both `emb`
        # and `n_emb` (the enrichment site mirrors them), doubling the per-node
        # embedding bytes for a field (`n_emb`) no offline consumer reads. Mirror
        # of the dedup in `serialize_node_record` above; the only difference here
        # is this loop's distinct field order / scalar rounding.
        node_records = []
        for n in nodes:
            rec: dict[str, Any] = {
                "title": str(n.get("title", "")),
                "score": round(float(n.get("score", 0.0)), 4),
                "tier": str(n.get("tier", "top_k")),
            }
            _node_vec = n.get("emb") or n.get("n_emb")
            if _node_vec:
                rec["emb"] = _round_emb(_node_vec)
            # v3+: per-node packed linked-slot embeddings (MAX_LINKED total,
            # extra_chunks_of_this_node first then actual_linked_nodes).
            # Stored already in the order _rl_model.update() consumes; offline
            # replay does NO re-packing.
            if n.get("linked_embs"):
                rec["linked_embs"] = [
                    _round_emb(e) for e in n["linked_embs"] if e
                ]
            if n.get("linked_type_names"):
                rec["linked_type_names"] = [
                    str(t) for t in n["linked_type_names"]
                ]
            if n.get("node_type"):
                rec["node_type"] = str(n["node_type"])
            # Log cosine features when available (used by offline trainer;
            # cos_ql=0.5 is valid for nodes with no links, but should NOT be
            # logged here if it's a fallback for "value not computed")
            for field in ("cos_qn", "cos_ql", "cos_nl"):
                val = n.get(field)
                if val is not None:
                    rec[field] = round(float(val), 4)
            if n.get("links"):
                rec["links"] = [str(lnk) for lnk in n["links"][:10]]
            node_records.append(rec)

        record: dict[str, Any] = {
            "event": "retrieval",
            "schema_version": self.SCHEMA_VERSION,
            "ts": _now(),
            "project": self._project,
            "task_id": task_id,
            "session_id": session_id,
            "task_type": task_type,
            "query": query[:2000],
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "nodes": node_records,
        }
        if query_emb is not None:
            record["query_emb"] = _round_emb(query_emb)
        if failure_mode:
            record["failure_mode"] = str(failure_mode)
        if failed_collections:
            # Cap at 32 entries to avoid pathological payloads.
            record["failed_collections"] = [
                str(c) for c in list(failed_collections)[:32]
            ]

        self._append(record)

    def log_citations(
        self,
        task_id: str,
        task_type: str,
        citations: "dict[str, bool | None]",
        cosine_sims: "dict[str, float] | None" = None,
        literal_cited: "dict[str, bool] | None" = None,
        cross_encoder_cited: "dict[str, bool] | None" = None,
        answer_text: "str | None" = None,
    ) -> None:
        """
        Log citation feedback: which nodes were actually used by the agent.

        Args:
            task_id:             Task identifier (matches the retrieval event).
            task_type:           Agent task category.
            citations:           Mapping of node title → cited flag.
                                 ``True`` = cited, ``False`` = not cited,
                                 ``None`` = inconclusive (cross-encoder call failed).
            cosine_sims:         Optional mapping of node title → RAW cosine
                                 similarity (cos(node_emb, agent_output_emb)).
                                 RAW values, NO bonuses pre-applied — offline
                                 trainer reapplies them via
                                 ``vco_lib.rl_training_targets.compute_unified_targets``.
            literal_cited:       v3+. Per-node bool — True iff the node's
                                 title/slug/wikilink/file_path appears as a
                                 word-boundary match in the ANSWER text. Used
                                 as a boost-flag input to the unified target
                                 formula. Absent for pre-v3 events; readers
                                 default missing entries to False.
            cross_encoder_cited: v3+. Per-node bool — Pro-tier cross-encoder
                                 verdict. Absent in v0.2.9 (cross-encoder
                                 wiring on MCP side is deferred); readers
                                 default missing entries to False.
            answer_text:         v3+ optional. The full agent answer text.
                                 v0.2.9 leaves this None (privacy/size) —
                                 the field reserves DB space so future
                                 versions can opt in to logging answers
                                 for offline multi-model training without
                                 a schema bump. None ⇒ field omitted.
        """
        # v0.2.40 F3: stamp the embedding triple
        # (embedding_source, embedding_dim, embedding_model) on every
        # citation event so it is self-contained. The offline RL
        # training pipeline pairs retrieval events with citation
        # events via shared embedding-triple keys; if the retrieval
        # event is dropped by the reader's embedding-triple filter
        # (historically the JSONL training_loader, retired v0.2.73
        # RL-8; today the DB-only offline_trainer path), the citation
        # event would otherwise orphan silently with no anchor.
        # Mirrors the retrieval-event shape.
        record: dict[str, Any] = {
            "event": "citation",
            "schema_version": self.SCHEMA_VERSION,
            "ts": _now(),
            "project": self._project,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "citations": {
                title: (bool(cited) if cited is not None else None)
                for title, cited in citations.items()
            },
        }
        if cosine_sims:
            record["cosine_sims"] = {
                t: round(float(v), 4) for t, v in cosine_sims.items()
            }
        if literal_cited:
            record["literal_cited"] = {
                t: bool(v) for t, v in literal_cited.items()
            }
        if cross_encoder_cited:
            record["cross_encoder_cited"] = {
                t: bool(v) for t, v in cross_encoder_cited.items()
            }
        if answer_text is not None:
            record["answer_text"] = str(answer_text)

        self._append(record)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._write_count += 1
            if self._write_count % self._rotation_check_interval == 0:
                self._rotate_if_needed()
        except Exception as exc:
            logger.debug("RLDataLogger: failed to append event: %s", exc)

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def _rotate_if_needed(self) -> None:
        """Rotate log when it exceeds max_events lines.

        Archives old file with timestamp suffix, prunes oldest archives
        beyond max_archives. Counting lines is cheap for JSONL (one line
        per event). Runs every _rotation_check_interval writes.
        """
        try:
            if not self._path.exists():
                return
            line_count = self._count_lines()
            if line_count <= self._max_events:
                return

            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
            archive = self._path.with_suffix(f".{ts}.jsonl")
            self._path.rename(archive)
            logger.info(
                "RLDataLogger: rotated %s (%d events) -> %s",
                self._path.name, line_count, archive.name,
            )
            self._prune_archives()
        except Exception as exc:
            logger.warning("RLDataLogger: rotation failed: %s", exc)

    def _count_lines(self) -> int:
        """Count lines in the current log file efficiently."""
        count = 0
        try:
            with self._path.open("rb") as f:
                for _ in f:
                    count += 1
        except OSError:
            pass
        return count

    def _prune_archives(self) -> None:
        """Remove oldest archives beyond max_archives."""
        parent = self._path.parent
        stem = self._path.stem  # "rl_events"
        archives = sorted(parent.glob(f"{stem}.*.jsonl"))
        excess = len(archives) - self._max_archives
        if excess <= 0:
            return
        for old in archives[:excess]:
            try:
                old.unlink()
                logger.debug("RLDataLogger: pruned old archive %s", old.name)
            except OSError:
                pass
