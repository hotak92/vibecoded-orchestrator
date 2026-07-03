#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Generate LLM summaries for CODE entities → `.claude/.code_formats.json`.

v0.2.73 M2 — the code analogue of ``generate-kg-summary.py``: the code
`summary` render tier had only ``signature + doc`` or a raw body snippet;
this sidecar gives it a real LLM ``one_liner`` + ``summary`` (+ per-chunk
summaries for multi-chunk entities). Consumed by the MCP/CLI renderer
(`server.py::_get_code_format`, T-RENDER).

Sidecar contract (FROZEN v1 — plan §3 D1; the consumer codes against this):
  * Path: ``<project-root>/.claude/.code_formats.json``.
  * Key: ``f"{file_path}::{full_name}"`` (composite — bare full_name
    collides across files; F-KEY).
  * Entry: ``{full_name, file_path, collection, one_liner, summary,
    generated_at, content_hash, backend}`` + optional ``total_chunks`` /
    ``chunk_summaries`` (multi-chunk entities only). ``collection`` is the
    BASE class ("CodeFunction" / "CodeClass"). ``content_hash`` is the ROW's
    stored ``content_hash`` property — staleness checks need no file reads:
    an entry is stale iff its hash != the row's current one.

Content source is WEAVIATE (not source files): canonical rows
(``chunk_num`` 0/NULL) of ``<Prefix>_CodeFunction`` + ``<Prefix>_CodeClass``.

Triggers (D3):
  1. Background rider on the resync module (``spawn_background_resync``
     spawns this as a detached child after an update/rebuild — exactly when
     rows change).
  2. Manual CLI: ``.claude/scripts/generate-code-summary.py --project X
     [--max-per-run N] [--force]``.
  NOT a per-edit hook (code edits are orders of magnitude more frequent than
  KG edits; a PostToolUse trigger would burn the local LLM continuously).

Cost gating (D3):
  * ``--max-per-run`` (default 150; env ``VCO_CODE_SUMMARY_MAX_PER_RUN``,
    empty-string coercion) caps LLM-visited entities per run. Resumable by
    construction: each run processes the next stale/missing entries and
    writes the sidecar incrementally (atomic tmp+rename).
  * Priority: ``n_callers`` DESC (hub entities first — M4 synergy), then
    ``total_chunks`` DESC, then name. Missing ``n_callers`` → 0.
  * Triviality skip: bodies < ~200 chars get ``one_liner`` only.
  * NO global timeout (locked rule) — per-call timeout only
    (``KG_SUMMARY_TIMEOUT``, via summary_backends).

Backends: the shared 4-tier ladder (``summary_backends.py`` — claude CLI →
Ollama → OpenAI-with-consent → Anthropic API → skip). No backend → logs +
exits 0 (sidecar untouched; the renderer keeps today's behaviour).
``CODE_SUMMARY_BACKEND`` forces the backend, falling back to
``KG_SUMMARY_BACKEND``.

Staleness + GC (D4): stale entries (hash mismatch) regenerate (count toward
the cap); entries whose key matches no live canonical row are pruned at the
end of a run; renames regenerate under the new key (old key GC'd).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# The shared backend ladder lives beside this script (templates/scripts/ in
# the orchestrator clone; .claude/scripts/ in installed projects).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import summary_backends as _sb  # noqa: E402

# ── Project root / paths (mirror generate-kg-summary.py's resolution) ────────
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_PROJECT = Path(os.getenv("KG_PROJECT_ROOT", str(_DEFAULT_ROOT))).resolve()
FORMATS_RELPATH = Path(".claude") / ".code_formats.json"
LOG_PATH = CLAUDE_PROJECT / ".claude" / "logs" / "code-summary-generator.log"

# The backend-force env alias: CODE_SUMMARY_BACKEND first, falling back to
# the shared KG_SUMMARY_BACKEND knob (plan §3 D2).
_ENV_KEYS = ("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND")
_LABEL = "code-summary"

# Per-run cap default + env override (empty-string coercion discipline).
DEFAULT_MAX_PER_RUN = 150
_ENV_MAX_PER_RUN = "VCO_CODE_SUMMARY_MAX_PER_RUN"

# Bodies shorter than this get a one_liner only (the signature already says
# it all) — halves call volume on typical codebases (plan §3 D3).
TRIVIAL_BODY_CHARS = 200

# Collections summarised. CodeModule is excluded at v1: module rows embed a
# generated module_summary already; Function/Class are where the summary tier
# renders raw body snippets today.
BASE_COLLECTIONS = ("CodeFunction", "CodeClass")
_BODY_FIELD = {"CodeFunction": "function_body", "CodeClass": "class_body"}


def log(msg: str) -> None:
    print(msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


_sb.set_logger(log)

_FORCE_API = False


def select_backend() -> str:
    return _sb.select_backend(
        force_api=_FORCE_API, env_keys=_ENV_KEYS, label=_LABEL
    )


def call_llm(prompt: str) -> str:
    return _sb.call_llm(prompt, force_api=_FORCE_API, env_keys=_ENV_KEYS, label=_LABEL)


# ──────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested without Weaviate)
# ──────────────────────────────────────────────────────────────────────
def entry_key(file_path: str, full_name: str) -> str:
    """FROZEN v1 composite key: ``file_path::full_name`` (F-KEY)."""
    return f"{file_path}::{full_name}"


def resolve_max_per_run(cli_value: "int | None", env: "dict | None" = None) -> int:
    """CLI flag → env override → default. Empty-string / unparseable env
    values fall through (v0.2.27 coercion discipline)."""
    if cli_value is not None:
        return cli_value
    raw = (env if env is not None else os.environ).get(_ENV_MAX_PER_RUN)
    if raw is not None:
        stripped = str(raw).strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                pass
    return DEFAULT_MAX_PER_RUN


def is_trivial(body: str) -> bool:
    return len(body or "") < TRIVIAL_BODY_CHARS


def needs_generation(existing_entry: "dict | None", row_content_hash: str,
                     force: bool) -> bool:
    """Missing entry, hash drift, or --force → regenerate.

    No stored row hash (pre-v0.2.61 rows) → generate only when the entry is
    missing (we cannot cheaply detect staleness without a hash; never churn).
    """
    if force:
        return True
    if existing_entry is None:
        return True
    if not row_content_hash:
        return False
    return existing_entry.get("content_hash") != row_content_hash


def priority_key(row: dict) -> tuple:
    """Sort key: n_callers DESC, total_chunks DESC, then name ASC (D3)."""
    n_callers = row.get("n_callers") or 0
    total_chunks = row.get("total_chunks") or 1
    try:
        n_callers = int(n_callers)
    except (TypeError, ValueError):
        n_callers = 0
    try:
        total_chunks = int(total_chunks)
    except (TypeError, ValueError):
        total_chunks = 1
    return (-n_callers, -total_chunks, str(row.get("full_name") or ""))


def plan_work(rows: list, formats: dict, *, force: bool) -> list:
    """Order the stale/missing subset of *rows* by priority.

    ``rows`` are canonical-row dicts carrying at least ``full_name`` /
    ``file_path`` / ``content_hash`` (+ optional ``n_callers`` /
    ``total_chunks``). Returns the worklist (the caller applies the cap).
    """
    work = []
    for row in rows:
        fp = row.get("file_path") or ""
        fn = row.get("full_name") or ""
        if not fn or not fp:
            continue  # un-keyable row — never guess
        key = entry_key(fp, fn)
        if needs_generation(formats.get(key), str(row.get("content_hash") or ""),
                            force):
            work.append(row)
    work.sort(key=priority_key)
    return work


def gc_dead_keys(formats: dict, live_keys: set,
                 collections: tuple = BASE_COLLECTIONS) -> int:
    """Prune entries (for the scanned collections) whose key matches no live
    row. Returns the number removed. Entries of OTHER collections are left
    alone (future collections must not be GC'd by an older generator)."""
    dead = [
        k for k, v in formats.items()
        if isinstance(v, dict)
        and v.get("collection") in collections
        and k not in live_keys
    ]
    for k in dead:
        del formats[k]
    return len(dead)


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomic write: vco_lib.atomic if importable, else tmp+rename inline."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from vco_lib.atomic import atomic_write_text  # type: ignore

        atomic_write_text(path, payload)
        return
    except Exception:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def load_formats(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            log(f"  code-summary: unreadable sidecar {path} ({exc}) — "
                "starting fresh (regenerable derived data)")
    return {}


# ──────────────────────────────────────────────────────────────────────
# Prompts (mirror generate-kg-summary.py's shapes, code-flavoured)
# ──────────────────────────────────────────────────────────────────────
def generate_one_liner(kind: str, full_name: str, signature: str, doc: str,
                       body: str) -> str:
    prompt = f"""Write a 1-sentence summary of what this {kind} does. Be maximally specific — name the concrete behaviour, not the category.

Name: {full_name}
Signature: {signature}
Doc: {doc[:500]}

Code:
{body[:2500]}"""
    return call_llm(prompt)


def generate_summary(kind: str, full_name: str, signature: str, doc: str,
                     body: str) -> str:
    prompt = f"""Summarize this {kind} in 2-4 sentences: its role, its key behaviour (inputs/outputs, side effects, error handling), and when it is used. Be technical and specific.

Name: {full_name}
Signature: {signature}
Doc: {doc[:800]}

Code:
{body[:4000]}"""
    return call_llm(prompt)


def generate_chunk_summary(full_name: str, chunk_num: int, total: int,
                           chunk_body: str) -> str:
    prompt = f"""Write a 1-sentence summary of this section (chunk {chunk_num}/{total}) of the code entity "{full_name}". Be specific about what THIS chunk covers.

Code:
{chunk_body[:2000]}"""
    return call_llm(prompt)


def build_entry(row: dict, collection: str, *, one_liner: str, summary: str,
                backend: str, chunk_summaries: "dict | None" = None) -> dict:
    """Assemble one FROZEN-v1 sidecar entry (plan §3 D1)."""
    entry = {
        "full_name": row.get("full_name") or "",
        "file_path": row.get("file_path") or "",
        "collection": collection,
        "one_liner": one_liner,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": str(row.get("content_hash") or ""),
        "backend": backend,
    }
    total_chunks = row.get("total_chunks") or 1
    try:
        total_chunks = int(total_chunks)
    except (TypeError, ValueError):
        total_chunks = 1
    if total_chunks > 1:
        entry["total_chunks"] = total_chunks
        if chunk_summaries:
            entry["chunk_summaries"] = chunk_summaries
    return entry


# ──────────────────────────────────────────────────────────────────────
# Weaviate access (lazy imports — module import stays dependency-light)
# ──────────────────────────────────────────────────────────────────────
def _collection_prefix(project_name: str) -> "str | None":
    """Project name → Weaviate class prefix via the ENDORSED SSOT wrapper
    (``codegraph_to_mermaid._sanitize_collection_prefix`` →
    ``project_naming.canonical_class_prefix``). No new sanitizer copy —
    mirrors ``vco_lib.codegraph_resync._collection_prefix``. ``None`` when
    unresolvable (the caller then does NOTHING — never guess a prefix)."""
    env_root = os.getenv("VCT_ORCHESTRATOR_ROOT", "").strip()
    for root in (env_root, str(_DEFAULT_ROOT)):
        if root and (Path(root) / "vco_lib").is_dir() and root not in sys.path:
            sys.path.insert(0, root)
    try:
        from vco_lib.codegraph_to_mermaid import (  # type: ignore
            _sanitize_collection_prefix as _sanitize,
        )
    except Exception as exc:  # noqa: BLE001 — partial install
        log(f"  code-summary: prefix resolver unavailable: {exc}")
        return None
    try:
        return _sanitize(project_name) or None
    except Exception as exc:  # noqa: BLE001
        log(f"  code-summary: cannot derive prefix: {exc}")
        return None


def _connect_weaviate():
    """Weaviate client from env (WEAVIATE_URL / GRPC_PORT); None on failure."""
    try:
        import weaviate
        from urllib.parse import urlparse

        url = urlparse(os.getenv("WEAVIATE_URL", "http://localhost:8081"))
        return weaviate.connect_to_local(
            host=url.hostname or "localhost",
            port=url.port or 8081,
            grpc_port=int(os.getenv("GRPC_PORT", "50052")),
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail, exit 0 at call site
        log(f"  code-summary: Weaviate unreachable: {exc}")
        return None


_ROW_PROPS = [
    "full_name", "file_path", "signature", "doc", "language",
    "content_hash", "total_chunks", "n_callers", "chunk_num",
]


def _iter_canonical_rows(client, prefix: str, base: str) -> list:
    """All canonical rows (chunk_num 0/NULL) of ``<prefix>_<base>`` as dicts."""
    name = f"{prefix}_{base}"
    try:
        if hasattr(client.collections, "exists") and not client.collections.exists(name):
            return []
        coll = client.collections.get(name)
    except Exception as exc:  # noqa: BLE001
        log(f"  code-summary: cannot open {name}: {exc}")
        return []
    body_field = _BODY_FIELD[base]
    rows = []
    try:
        for obj in coll.iterator(return_properties=_ROW_PROPS + [body_field]):
            props = dict(getattr(obj, "properties", None) or {})
            chunk_num = props.get("chunk_num")
            if chunk_num not in (None, 0):
                continue  # sibling chunk rows fetched on demand
            props["_body"] = str(props.get(body_field) or "")
            rows.append(props)
    except Exception as exc:  # noqa: BLE001 — partial scan is worse than none
        log(f"  code-summary: scan of {name} failed: {exc}")
        return []
    return rows


def _fetch_chunk_bodies(client, prefix: str, base: str, full_name: str) -> list:
    """``[(chunk_num, body), ...]`` for a multi-chunk entity, sorted."""
    try:
        from weaviate.classes.query import Filter

        coll = client.collections.get(f"{prefix}_{base}")
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(full_name),
            limit=50,
        )
        body_field = _BODY_FIELD[base]
        chunks = []
        for obj in resp.objects:
            props = obj.properties or {}
            cn = props.get("chunk_num")
            chunks.append((int(cn) if cn is not None else 0,
                           str(props.get(body_field) or "")))
        chunks.sort(key=lambda x: x[0])
        return chunks
    except Exception as exc:  # noqa: BLE001 — chunk summaries are optional
        log(f"  code-summary: chunk fetch for {full_name} failed: {exc}")
        return []


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
def run(project: str, *, project_root: Path, max_per_run: int,
        force: bool) -> int:
    formats_path = project_root / FORMATS_RELPATH
    prefix = _collection_prefix(project)
    if prefix is None:
        return 0  # logged; conservative no-op

    if select_backend() == "skip":
        return 0  # ladder tier 5 — sidecar stays as-is, renderer unaffected

    client = _connect_weaviate()
    if client is None:
        return 0

    try:
        formats = load_formats(formats_path)
        all_rows: dict[str, list] = {}
        live_keys: set = set()
        for base in BASE_COLLECTIONS:
            rows = _iter_canonical_rows(client, prefix, base)
            all_rows[base] = rows
            for r in rows:
                fp, fn = r.get("file_path") or "", r.get("full_name") or ""
                if fp and fn:
                    live_keys.add(entry_key(fp, fn))

        generated = 0
        failures = 0
        for base in BASE_COLLECTIONS:
            kind = "function" if base == "CodeFunction" else "class"
            for row in plan_work(all_rows[base], formats, force=force):
                if generated >= max_per_run:
                    break
                full_name = str(row.get("full_name") or "")
                body = row.get("_body") or ""
                signature = str(row.get("signature") or "")
                doc = str(row.get("doc") or "")
                try:
                    one_liner = generate_one_liner(
                        kind, full_name, signature, doc, body)
                    summary = "" if is_trivial(body) else generate_summary(
                        kind, full_name, signature, doc, body)
                    chunk_summaries = None
                    total_chunks = int(row.get("total_chunks") or 1)
                    if total_chunks > 1:
                        chunk_summaries = {}
                        for cn, chunk_body in _fetch_chunk_bodies(
                                client, prefix, base, full_name):
                            chunk_summaries[str(cn)] = generate_chunk_summary(
                                full_name, cn, total_chunks, chunk_body)
                except Exception as exc:  # noqa: BLE001 — per-entity isolation
                    failures += 1
                    log(f"  code-summary: {full_name} failed: {exc}")
                    continue
                key = entry_key(str(row.get("file_path") or ""), full_name)
                formats[key] = build_entry(
                    row, base,
                    one_liner=one_liner, summary=summary,
                    backend=_sb._BACKEND_CACHE.get("choice", "?"),
                    chunk_summaries=chunk_summaries,
                )
                generated += 1
                # Incremental persistence: a killed run keeps its progress.
                if generated % 10 == 0:
                    atomic_write_json(formats_path, formats)

        # D4: GC entries whose key matches no live canonical row (bounded to
        # the collections scanned this run — full scan == full GC).
        removed = gc_dead_keys(formats, live_keys)
        if generated or removed or force:
            atomic_write_json(formats_path, formats)
        log(f"  code-summary: {generated} generated, {removed} pruned, "
            f"{failures} failed (cap {max_per_run}) → {formats_path}")
        return 0
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    global _FORCE_API
    parser = argparse.ArgumentParser(
        description="Generate LLM summaries for code entities (.code_formats.json)"
    )
    parser.add_argument("--project", required=True,
                        help="Project name (Weaviate collection prefix source)")
    parser.add_argument("--project-root", default=None,
                        help="Project root holding .claude/ (default: "
                             "KG_PROJECT_ROOT env, else the script's install root)")
    parser.add_argument("--max-per-run", type=int, default=None,
                        help=f"Cap LLM-visited entities per run (default "
                             f"{DEFAULT_MAX_PER_RUN}; env {_ENV_MAX_PER_RUN})")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate every entry (also full GC)")
    parser.add_argument("--force-api", action="store_true",
                        help="Bypass the kg_summary_openai_consent gate "
                             "(operator override)")
    args = parser.parse_args()

    if args.force_api:
        _FORCE_API = True
    project_root = (
        Path(args.project_root).resolve() if args.project_root else CLAUDE_PROJECT
    )
    return run(
        args.project,
        project_root=project_root,
        max_per_run=resolve_max_per_run(args.max_per_run),
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
