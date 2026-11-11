# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Export the RL training set from ``launcher.db`` into compact, text-free shards.

Run from the orchestrator root with its venv (Windows: ``.venv\\Scripts\\python.exe``)::

    python -m claude_mcp_servers.rl_reranker.training.export --list-projects
    python -m claude_mcp_servers.rl_reranker.training.export --out <dir> [--project X ...] [--exclude-project Y ...]

What it does, in one pass per selected project:

* opens the launcher DB READ-ONLY (``mode=ro``; it never writes, vacuums or
  deletes) and streams ``rl_events`` row by row — no project is ever loaded
  whole;
* pairs each retrieval with its citation by ``task_id`` (last row wins on a
  duplicate, exactly like the trainer) and applies the trainer's own target
  and soft-label rules (:func:`offline_trainer.citation_targets`);
* keeps only the KG embedding profiles the reranker serves (``qwen3``,
  ``arctic``), drops test/fixture projects, and drops arctic dual-log twins
  written before the project ran v0.2.101 (their query vector lacks the query
  prefix the search used: train/serve skew);
* writes float16 vectors (deduplicated by content) plus integer/float index
  arrays and an ``index.json``.

What it never writes: query text, node titles, node or link text, project
names, folder paths, session ids, task ids, or the raw project ids. Projects
appear only as a salted hash (``--list-projects`` shows the hash next to the
name on YOUR screen; the salt stays on this machine). Every output field is
documented in ``FIELDS.md`` next to this file, which the export also copies
into the output directory — read it before sharing an export.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from ..offline_trainer import citation_targets
from ..rl_model import MAX_LINKED, NODE_TYPES, NULL_TYPE_IDX, UNKNOWN_TYPE_IDX

#: Output format version (``index.json`` → ``format_version``). Bump on any
#: change a reader must know about; ``FIELDS.md`` documents the current one.
EXPORT_FORMAT_VERSION = 1

#: The KG embedding profiles the reranker serves (and the export writes).
EXPORT_PROFILES: tuple[str, ...] = ("qwen3", "arctic")

#: Event types the trainer pairs. Outcome events (pre_bash / bash_outcome /
#: edit_outcome) are not training input (``hub_event_loader``).
_PAIR_EVENT_TYPES = ("retrieval", "citation")

#: Type code for a node / linked node whose type is not one of the builtin
#: :data:`rl_model.NODE_TYPES`. The real name is never exported (type names
#: can be project-specific). The trainer maps this code to the same embedding
#: a serving model gives a type it registers on the fly: the mean of the
#: builtin rows.
TYPE_CODE_OTHER = 9

#: Arctic twins are only trusted from the moment a project ran v0.2.101 (the
#: query-prefix fix for the twin's query vector, rl_enrichment "caller audit
#: Gap 1"). The DB holds no install history, so the evidence is the data
#: itself: the first event of a task type that v0.2.101 introduced. A project
#: that never shows one never ran the fix, and none of its twins is kept.
V0_2_101_TASK_TYPES: tuple[str, ...] = (
    "pre_read_kg_search",
    "pre_write_kg_search",
    "pre_search_kg_search",
    "agent_brief_kg_search",
)
#: v0.2.101's release day (2026-10-06T00:00Z) — no twin before it is trusted,
#: whatever the task types say.
V0_2_101_RELEASE_MS = 1791244800000

#: Timestamps are exported rounded DOWN to this granularity (one hour): enough
#: for a time-ordered hold-out, coarse enough not to log a work pattern.
TS_GRANULARITY_MS = 3_600_000

#: Name of the per-machine salt file under the VCO state dir.
SALT_FILE_NAME = "rl_export_salt"


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only (SQLite URI ``mode=ro``; Windows-safe)."""
    p = Path(db_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"launcher DB not found: {p}")
    return sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)


def load_or_create_salt(salt_path: Path) -> bytes:
    """The machine-local salt for project hashes (created on first use)."""
    if salt_path.is_file():
        data = salt_path.read_text(encoding="utf-8").strip()
        if len(data) >= 32:
            return data.encode("utf-8")
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    salt_path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(salt_path, 0o600)
    except OSError:
        pass
    return value.encode("utf-8")


def hash_project_id(salt: bytes, project_id: str) -> str:
    """Stable, salted, non-reversible id for a project (16 hex chars)."""
    return hashlib.sha256(salt + b"\x00" + project_id.encode("utf-8")).hexdigest()[:16]


def type_code(name: Optional[str], *, empty: int) -> int:
    """Builtin index for a builtin node type, ``empty`` for none, else OTHER."""
    if not name:
        return empty
    try:
        return NODE_TYPES.index(name)
    except ValueError:
        return TYPE_CODE_OTHER


def _title_key(title: str) -> bytes:
    """In-memory join key for a title (the title itself is never stored)."""
    return hashlib.blake2b(title.encode("utf-8"), digest_size=12).digest()


def twin_trust_floor_ms(first_v0_2_101_ts: Optional[int]) -> Optional[int]:
    """Earliest ts from which a project's arctic twins are kept (None = never)."""
    if first_v0_2_101_ts is None:
        return None
    return max(int(first_v0_2_101_ts), V0_2_101_RELEASE_MS)


def is_kg_task_type(task_type: Optional[str]) -> bool:
    """KG retrievals only (code-graph task types are out of scope)."""
    return not (task_type or "").startswith("code_")


# ---------------------------------------------------------------------------
# Project inventory (the --list-projects preview)
# ---------------------------------------------------------------------------

@dataclass
class ProjectInfo:
    project_id: str
    name: str
    hashed_id: str
    fixture: bool
    rows: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    first_v0_2_101_ts: Optional[int] = None

    def matches(self, selector: str) -> bool:
        s = selector.strip()
        return s in (self.project_id, self.hashed_id) or s.casefold() == self.name.casefold()


@dataclass
class Inventory:
    projects: list[ProjectInfo]
    unattributed_rows: Counter  # NULL project_id, by profile


def build_inventory(conn: sqlite3.Connection, salt: bytes) -> Inventory:
    """One light GROUP BY over the indexed columns (no payload is read)."""
    from vco_lib.fixture_class_guard import is_fixture_project_name

    names = {pid: name for pid, name in conn.execute("SELECT id, name FROM projects")}
    by_id: dict[str, ProjectInfo] = {}
    unattributed: Counter = Counter()
    marker_types = set(V0_2_101_TASK_TYPES)
    sql = (
        "SELECT project_id, project_name, event_type, embedding_source, task_type, "
        "count(*), min(ts) FROM rl_events "
        "WHERE event_type IN ('retrieval', 'citation') "
        "GROUP BY project_id, project_name, event_type, embedding_source, task_type"
    )
    for pid, pname, etype, source, task_type, n, min_ts in conn.execute(sql):
        if pid is None:
            if source in EXPORT_PROFILES:
                unattributed[f"{source}:{etype}"] += int(n)
            continue
        info = by_id.get(pid)
        if info is None:
            display = names.get(pid) or pname or pid
            info = ProjectInfo(
                project_id=pid,
                name=str(display),
                hashed_id=hash_project_id(salt, pid),
                fixture=is_fixture_project_name(display) or is_fixture_project_name(pname),
            )
            by_id[pid] = info
        if source in EXPORT_PROFILES and is_kg_task_type(task_type):
            info.rows[source][etype] += int(n)
        if task_type in marker_types and min_ts is not None:
            if info.first_v0_2_101_ts is None or min_ts < info.first_v0_2_101_ts:
                info.first_v0_2_101_ts = int(min_ts)
    projects = sorted(by_id.values(), key=lambda p: (-sum(
        c["retrieval"] for c in p.rows.values()), p.name.casefold()))
    return Inventory(projects=projects, unattributed_rows=unattributed)


def select_projects(
    inventory: Inventory,
    include: list[str],
    exclude: list[str],
) -> tuple[list[ProjectInfo], list[str]]:
    """Apply --project / --exclude-project. Returns (selected, unmatched selectors)."""
    unmatched = [s for s in include + exclude
                 if not any(p.matches(s) for p in inventory.projects)]
    chosen = []
    for p in inventory.projects:
        if p.fixture:
            continue
        if include and not any(p.matches(s) for s in include):
            continue
        if any(p.matches(s) for s in exclude):
            continue
        chosen.append(p)
    return chosen, unmatched


def format_inventory(inventory: Inventory, selected: list[ProjectInfo]) -> str:
    sel = {p.project_id for p in selected}
    lines = [
        "Projects in the launcher DB (retrieval / citation rows per profile; "
        "names are shown here only and never exported):",
        "",
        f"  {'export':<8}{'hashed id':<18}{'qwen3 ret/cit':>17}{'arctic ret/cit':>18}  name",
    ]
    for p in inventory.projects:
        mark = "fixture" if p.fixture else ("yes" if p.project_id in sel else "no")
        q = p.rows.get("qwen3", Counter())
        a = p.rows.get("arctic", Counter())
        lines.append(
            f"  {mark:<8}{p.hashed_id:<18}"
            f"{q['retrieval']:>8}/{q['citation']:<8}"
            f"{a['retrieval']:>9}/{a['citation']:<8}  {p.name}"
        )
    if inventory.unattributed_rows:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(inventory.unattributed_rows.items()))
        lines += ["", f"  Rows with no project id (never exported: test residue or unattributed): {parts}"]
    lines += ["", "Row counts are BEFORE pairing and filtering; the export reports what it kept."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class VectorStore:
    """Append-only float16 vector file, deduplicated by content."""

    def __init__(self, path: Path, dim: int) -> None:
        self.path = path
        self.dim = dim
        self._index: dict[bytes, int] = {}
        self._fh = path.open("wb")
        self.count = 0
        self.lookups = 0

    def add(self, vec: Any) -> int:
        """Index of ``vec`` (a list of floats); -1 when its dim is wrong."""
        arr = np.asarray(vec, dtype=np.float32)
        if arr.ndim != 1 or arr.shape[0] != self.dim or not np.all(np.isfinite(arr)):
            return -1
        raw = arr.astype("<f2").tobytes()
        key = hashlib.blake2b(raw, digest_size=16).digest()
        self.lookups += 1
        idx = self._index.get(key)
        if idx is None:
            idx = self.count
            self._index[key] = idx
            self._fh.write(raw)
            self.count += 1
        return idx

    def finish(self, npy_path: Path) -> None:
        """Turn the raw file into a standard ``.npy`` (shape (count, dim), <f2)."""
        self._fh.close()
        with npy_path.open("wb") as out:
            np.lib.format.write_array_header_1_0(
                out, {"descr": "<f2", "fortran_order": False, "shape": (self.count, self.dim)},
            )
            with self.path.open("rb") as src:
                shutil.copyfileobj(src, out, length=1 << 22)
        self.path.unlink()


class ProfileWriter:
    """Columnar buffers for one profile's queries and samples."""

    def __init__(self, out_dir: Path, profile: str, dim: int) -> None:
        self.dir = out_dir / profile
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.vectors = VectorStore(self.dir / "vectors.f16.tmp", dim)
        # queries
        self.q_vec = array("i")
        self.q_project = array("h")
        self.q_ts = array("q")
        self.q_task_type = array("h")
        self.q_regime = array("h")
        self.q_soft = array("b")
        self.q_weight = array("f")
        self.q_first = array("q")
        self.q_count = array("i")
        # samples
        self.s_query = array("i")
        self.s_node = array("i")
        self.s_linked = array("i")        # MAX_LINKED per sample, -1 = empty
        self.s_linked_type = array("b")   # MAX_LINKED per sample
        self.s_node_type = array("b")
        self.s_target = array("f")
        self.s_cos_cit = array("f")
        self.s_literal = array("b")
        self.s_cited = array("b")
        self.s_cos_qn = array("f")
        self.s_score = array("f")
        self.s_shown_rank = array("h")
        self.s_truncated = array("b")
        self.drops: Counter = Counter()
        self.kept_by_project: Counter = Counter()

    def finish(self) -> dict[str, Any]:
        self.vectors.finish(self.dir / "vectors.npy")
        np.savez_compressed(
            self.dir / "queries.npz",
            vec=np.frombuffer(self.q_vec, dtype=np.int32),
            project=np.frombuffer(self.q_project, dtype=np.int16),
            ts_hour_ms=np.frombuffer(self.q_ts, dtype=np.int64),
            task_type=np.frombuffer(self.q_task_type, dtype=np.int16),
            label_regime=np.frombuffer(self.q_regime, dtype=np.int16),
            soft_label=np.frombuffer(self.q_soft, dtype=np.int8),
            weight=np.frombuffer(self.q_weight, dtype=np.float32),
            first_sample=np.frombuffer(self.q_first, dtype=np.int64),
            n_samples=np.frombuffer(self.q_count, dtype=np.int32),
        )
        n = len(self.s_query)
        np.savez_compressed(
            self.dir / "samples.npz",
            query=np.frombuffer(self.s_query, dtype=np.int32),
            node_vec=np.frombuffer(self.s_node, dtype=np.int32),
            linked_vec=np.frombuffer(self.s_linked, dtype=np.int32).reshape(n, MAX_LINKED),
            linked_type=np.frombuffer(self.s_linked_type, dtype=np.int8).reshape(n, MAX_LINKED),
            node_type=np.frombuffer(self.s_node_type, dtype=np.int8),
            target=np.frombuffer(self.s_target, dtype=np.float32),
            cos_citation=np.frombuffer(self.s_cos_cit, dtype=np.float32),
            literal_cited=np.frombuffer(self.s_literal, dtype=np.int8),
            cited=np.frombuffer(self.s_cited, dtype=np.int8),
            cos_qn=np.frombuffer(self.s_cos_qn, dtype=np.float32),
            score=np.frombuffer(self.s_score, dtype=np.float32),
            shown_rank=np.frombuffer(self.s_shown_rank, dtype=np.int16),
            emb_truncated=np.frombuffer(self.s_truncated, dtype=np.int8),
        )
        return {
            "queries": len(self.q_vec),
            "samples": n,
            "vectors": self.vectors.count,
            "vector_dim": self.vectors.dim,
            "vector_lookups": self.vectors.lookups,
            "dropped": dict(sorted(self.drops.items())),
            "kept_queries_by_project": dict(sorted(self.kept_by_project.items())),
            "bytes": sum(f.stat().st_size for f in self.dir.iterdir() if f.is_file()),
        }


class CodeTable:
    """String → small int table for code-defined strings (task types, regimes)."""

    def __init__(self) -> None:
        self.codes: dict[str, int] = {}

    def code(self, value: str) -> int:
        if value not in self.codes:
            self.codes[value] = len(self.codes)
        return self.codes[value]

    def as_list(self) -> list[str]:
        return [k for k, _ in sorted(self.codes.items(), key=lambda kv: kv[1])]


# ---------------------------------------------------------------------------
# Streaming pair builder
# ---------------------------------------------------------------------------

@dataclass
class _CitationRecord:
    #: title key -> (target, citation cosine or NaN, literal_cited 0/1,
    #: cited -1 unknown / 0 / 1). One dict per citation keeps a whole
    #: project's citations small enough to hold while its retrievals stream.
    entries: dict[bytes, tuple[float, float, int, int]]
    weight: float
    soft: bool
    regime: str


def _latest_retrieval_ids(
    conn: sqlite3.Connection, project_id: str, sources: tuple[str, ...],
) -> dict[str, tuple[str, int]]:
    """``task_id -> (profile, max row id)`` for the project's retrievals."""
    out: dict[str, tuple[str, int]] = {}
    sql = (
        "SELECT task_id, embedding_source, max(id) FROM rl_events "
        "WHERE project_id = ? AND event_type = 'retrieval' AND embedding_source IN ({}) "
        "GROUP BY task_id, embedding_source"
    ).format(",".join("?" * len(sources)))
    for tid, source, max_id in conn.execute(sql, (project_id, *sources)):
        prev = out.get(tid)
        if prev is None or max_id > prev[1]:
            out[tid] = (source, int(max_id))
    return out


def _iter_payloads(
    conn: sqlite3.Connection, project_id: str, event_type: str, sources: tuple[str, ...],
) -> Iterator[tuple[int, int, str, Optional[str], Optional[str], str]]:
    """Rows of one project/event type/profile set, in time order (streamed)."""
    sql = (
        "SELECT id, ts, task_id, task_type, embedding_source, payload_json FROM rl_events "
        "WHERE project_id = ? AND event_type = ? AND embedding_source IN ({}) "
        "ORDER BY ts, id"
    ).format(",".join("?" * len(sources)))
    yield from conn.execute(sql, (project_id, event_type, *sources))


def _citation_record(payload: dict[str, Any]) -> Optional[_CitationRecord]:
    from claude_mcp_servers.rl_client.rl_logger import resolve_label_regime

    decided = citation_targets(payload)
    if decided is None:
        return None
    targets, weight = decided
    cos = payload.get("cosine_sims") or {}
    literal = payload.get("literal_cited") or {}
    cited = payload.get("citations") or {}
    entries: dict[bytes, tuple[float, float, int, int]] = {}
    for title, target in targets.items():
        c = cos.get(title)
        flag = cited.get(title)
        entries[_title_key(title)] = (
            float(target),
            float(c) if isinstance(c, (int, float)) else float("nan"),
            1 if literal.get(title) else 0,
            -1 if flag is None else (1 if flag else 0),
        )
    return _CitationRecord(
        entries=entries,
        weight=float(weight),
        soft=bool(payload.get("soft_label")),
        regime=resolve_label_regime(payload),
    )


def export_project(
    conn: sqlite3.Connection,
    project: ProjectInfo,
    project_code: int,
    writers: dict[str, ProfileWriter],
    task_types: CodeTable,
    regimes: CodeTable,
    log: Any = None,
) -> None:
    """Stream one project's pairs into the profile writers."""
    sources = tuple(writers)
    latest = _latest_retrieval_ids(conn, project.project_id, sources)
    wanted = set(latest)
    twin_floor = twin_trust_floor_ms(project.first_v0_2_101_ts)

    # Phase A: citations (last row wins per task_id), only for retrievals we have.
    citations: dict[str, Optional[_CitationRecord]] = {}
    seen_cit: Counter = Counter()
    for _id, _ts, tid, _tt, _src, payload_json in _iter_payloads(
            conn, project.project_id, "citation", sources):
        if tid not in wanted:
            src = _src if _src in writers else None
            if src is not None:
                writers[src].drops["citation_orphan"] += 1
            continue
        seen_cit[tid] += 1
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            writers[latest[tid][0]].drops["citation_unparseable"] += 1
            continue
        citations[tid] = _citation_record(payload)
    for tid, n in seen_cit.items():
        if n > 1:
            writers[latest[tid][0]].drops["citation_duplicate_superseded"] += n - 1

    # Phase B: retrievals (only the latest row per task_id).
    for row_id, ts, tid, task_type, source, payload_json in _iter_payloads(
            conn, project.project_id, "retrieval", sources):
        if source not in writers:
            continue
        w = writers[source]
        if not is_kg_task_type(task_type):
            w.drops["retrieval_not_kg"] += 1
            continue
        if latest.get(tid, (None, -1))[1] != row_id:
            w.drops["retrieval_duplicate_superseded"] += 1
            continue
        if tid not in citations:
            w.drops["retrieval_no_citation"] += 1
            continue
        cit = citations[tid]
        if cit is None:
            w.drops["retrieval_soft_label_short_window"] += 1
            continue
        if source == "arctic" and (twin_floor is None or ts < twin_floor):
            w.drops["retrieval_arctic_before_prefix_fix"] += 1
            continue
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            w.drops["retrieval_unparseable"] += 1
            continue
        q_raw = payload.get("query_emb")
        if not q_raw:
            w.drops["retrieval_no_query_emb"] += 1
            continue
        q_idx = w.vectors.add(q_raw)
        if q_idx < 0:
            w.drops["retrieval_query_dim_mismatch"] += 1
            continue
        # Node map, last wins per title (as offline_trainer.extract_samples).
        node_map: dict[bytes, dict[str, Any]] = {}
        for n in payload.get("nodes") or []:
            if isinstance(n, dict) and n.get("emb"):
                node_map[_title_key(str(n.get("title", "")))] = n
        first = len(w.s_query)
        q_row = len(w.q_vec)
        for key, (target, cos_cit, literal, cited) in cit.entries.items():
            node = node_map.get(key)
            if node is None:
                w.drops["sample_node_without_vector"] += 1
                continue
            n_idx = w.vectors.add(node["emb"])
            if n_idx < 0:
                w.drops["sample_node_dim_mismatch"] += 1
                continue
            node_type = node.get("node_type") or ""
            linked_idx = [-1] * MAX_LINKED
            linked_type = [NULL_TYPE_IDX] * MAX_LINKED
            l_names = node.get("linked_type_names") or node.get("link_types") or []
            slot = 0
            for li, lv in enumerate((node.get("linked_embs") or [])[:MAX_LINKED]):
                if not lv:
                    continue
                v_idx = w.vectors.add(lv)
                if v_idx < 0:
                    w.drops["linked_dim_mismatch"] += 1
                    continue
                linked_idx[slot] = v_idx
                lname = l_names[li] if li < len(l_names) else ""
                linked_type[slot] = type_code(lname, empty=UNKNOWN_TYPE_IDX)
                slot += 1
            trunc = node.get("emb_truncated")
            rank = node.get("shown_rank")
            w.s_query.append(q_row)
            w.s_node.append(n_idx)
            w.s_linked.extend(linked_idx)
            w.s_linked_type.extend(linked_type)
            w.s_node_type.append(type_code(node_type, empty=0))
            w.s_target.append(float(target))
            w.s_cos_cit.append(cos_cit)
            w.s_literal.append(literal)
            w.s_cited.append(cited)
            cqn = node.get("cos_qn")
            w.s_cos_qn.append(float(cqn) if isinstance(cqn, (int, float)) else float("nan"))
            sc = node.get("score")
            w.s_score.append(float(sc) if isinstance(sc, (int, float)) else float("nan"))
            w.s_shown_rank.append(int(rank) if isinstance(rank, int) and 0 <= rank < 32767 else -1)
            w.s_truncated.append(-1 if trunc is None else (1 if trunc else 0))
        n_samples = len(w.s_query) - first
        if n_samples == 0:
            w.drops["retrieval_no_scorable_nodes"] += 1
            continue
        w.q_vec.append(q_idx)
        w.q_project.append(project_code)
        w.q_ts.append((int(ts) // TS_GRANULARITY_MS) * TS_GRANULARITY_MS)
        w.q_task_type.append(task_types.code(str(task_type or "")))
        w.q_regime.append(regimes.code(cit.regime))
        w.q_soft.append(1 if cit.soft else 0)
        w.q_weight.append(cit.weight)
        w.q_first.append(first)
        w.q_count.append(n_samples)
        w.kept_by_project[project.hashed_id] += 1
    if log is not None:
        log(f"  {project.hashed_id}: " + ", ".join(
            f"{p}={writers[p].kept_by_project[project.hashed_id]}" for p in writers))


def run_export(
    db_path: Path,
    out_dir: Path,
    *,
    salt: bytes,
    include: list[str],
    exclude: list[str],
    profiles: Iterable[str] = EXPORT_PROFILES,
    dim: int = 1024,
    log: Any = None,
) -> dict[str, Any]:
    """Export the selected projects into ``out_dir``. Returns the index dict."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    conn = open_db_readonly(db_path)
    try:
        inventory = build_inventory(conn, salt)
        selected, unmatched = select_projects(inventory, include, exclude)
        if unmatched:
            raise ValueError(f"no project matches: {', '.join(unmatched)}")
        writers = {p: ProfileWriter(out_dir, p, dim) for p in profiles}
        task_types = CodeTable()
        regimes = CodeTable()
        project_hashes: list[str] = []
        for project in selected:
            project_hashes.append(project.hashed_id)
            export_project(
                conn, project, len(project_hashes) - 1, writers,
                task_types, regimes, log=log,
            )
        stats = {p: w.finish() for p, w in writers.items()}
    finally:
        conn.close()
    index = {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - started, 1),
        "ts_granularity_ms": TS_GRANULARITY_MS,
        "max_linked": MAX_LINKED,
        "type_codes": {
            **{name: i for i, name in enumerate(NODE_TYPES)},
            "null_padding": NULL_TYPE_IDX,
            "unknown": UNKNOWN_TYPE_IDX,
            "other": TYPE_CODE_OTHER,
        },
        "task_types": task_types.as_list(),
        "label_regimes": regimes.as_list(),
        "projects": project_hashes,
        "profiles": stats,
    }
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    fields_doc = Path(__file__).with_name("FIELDS.md")
    if fields_doc.is_file():
        shutil.copyfile(fields_doc, out_dir / "FIELDS.md")
    return index


# ---------------------------------------------------------------------------
# Reader (used by the trainer and the eval harness)
# ---------------------------------------------------------------------------

@dataclass
class ProfileShard:
    """One export's data for one profile, loaded for training/evaluation."""

    root: Path
    profile: str
    vectors: np.ndarray            # (V, D) float16, memory-mapped
    queries: dict[str, np.ndarray]
    samples: dict[str, np.ndarray]
    projects: list[str]
    task_types: list[str]


def load_profile_shard(export_dir: Path, profile: str) -> ProfileShard:
    export_dir = Path(export_dir)
    index = json.loads((export_dir / "index.json").read_text(encoding="utf-8"))
    if index.get("format_version") != EXPORT_FORMAT_VERSION:
        raise ValueError(
            f"{export_dir}: export format {index.get('format_version')!r} "
            f"(this reader understands {EXPORT_FORMAT_VERSION})"
        )
    pdir = export_dir / profile
    with np.load(pdir / "queries.npz") as q:
        queries = {k: q[k] for k in q.files}
    with np.load(pdir / "samples.npz") as s:
        samples = {k: s[k] for k in s.files}
    vectors = np.load(pdir / "vectors.npy", mmap_mode="r")
    return ProfileShard(
        root=export_dir, profile=profile, vectors=vectors, queries=queries,
        samples=samples, projects=list(index.get("projects", [])),
        task_types=list(index.get("task_types", [])),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_salt_path() -> Path:
    from vco_lib.paths import vct_root_dir
    return vct_root_dir() / SALT_FILE_NAME


def _default_db_path() -> Path:
    from vco_lib.paths import launcher_db_path
    return launcher_db_path()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m claude_mcp_servers.rl_reranker.training.export",
        description=(
            "Export RL reranker training data from launcher.db (read-only) into "
            "text-free float16 shards. Run --list-projects first; FIELDS.md "
            "documents every output field."
        ),
    )
    p.add_argument("--db", type=Path, default=None,
                   help="launcher DB (default: the VCO state dir's launcher.db)")
    p.add_argument("--list-projects", action="store_true",
                   help="show the projects, their row counts and whether they "
                        "would be exported, then exit (writes nothing)")
    p.add_argument("--project", action="append", default=[],
                   help="export ONLY this project (name, project id or hashed id); repeatable")
    p.add_argument("--exclude-project", action="append", default=[],
                   help="leave this project out (name, project id or hashed id); repeatable")
    p.add_argument("--out", type=Path, default=None,
                   help="output directory (must be new or empty)")
    p.add_argument("--profile", action="append", choices=list(EXPORT_PROFILES), default=None,
                   help="export only this embedding profile (default: both)")
    p.add_argument("--salt-file", type=Path, default=None,
                   help="salt for the project hashes (default: rl_export_salt in "
                        "the VCO state dir; created on first use, never exported)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")
    db_path = args.db or _default_db_path()
    salt = load_or_create_salt(args.salt_file or _default_salt_path())
    try:
        conn = open_db_readonly(db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"export: cannot open the launcher DB read-only: {exc}", file=sys.stderr)
        return 2
    try:
        inventory = build_inventory(conn, salt)
    finally:
        conn.close()
    selected, unmatched = select_projects(inventory, args.project, args.exclude_project)
    if unmatched:
        print(f"export: no project matches: {', '.join(unmatched)}", file=sys.stderr)
        return 2
    print(format_inventory(inventory, selected))
    if args.list_projects:
        return 0
    if args.out is None:
        print("\nexport: --out is required to write an export (or use --list-projects).",
              file=sys.stderr)
        return 2
    if not selected:
        print("export: no project selected; nothing to write.", file=sys.stderr)
        return 2
    print(f"\nExporting {len(selected)} project(s) to {args.out} ...", flush=True)
    try:
        index = run_export(
            db_path, args.out, salt=salt, include=args.project,
            exclude=args.exclude_project, profiles=args.profile or EXPORT_PROFILES,
            log=lambda m: print(m, flush=True),
        )
    except (FileExistsError, ValueError, sqlite3.Error) as exc:
        print(f"export: {exc}", file=sys.stderr)
        return 2
    for prof, st in index["profiles"].items():
        print(f"{prof}: {st['queries']} queries, {st['samples']} samples, "
              f"{st['vectors']} unique vectors, {st['bytes'] / 1e6:.1f} MB")
        for reason, n in st["dropped"].items():
            print(f"    dropped {reason}: {n}")
    print(f"\nDone. Read {Path(args.out) / 'FIELDS.md'} before sharing this directory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
