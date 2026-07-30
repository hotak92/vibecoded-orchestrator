#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Release tool: build + verify the pre-shipped KG-embedding sidecars (§8.2).

Generates ``templates/knowledge/.node_embeddings.<slot>.json`` — one file per
named-vector slot — holding pre-computed embedding vectors for every bundled
curated KG node, so a 3rd-party install INGESTS them at seed time instead of
re-embedding all ~115 nodes locally (the arctic-on-CPU install-hang class).
The ingest plumbing shipped in v0.2.70 (``sync_knowledge_graph.py``,
``_shipped_vector_for`` / ``_shipped_chunk_vector``); v0.2.89 ships the DATA
for the two canonical local slots (``qwen3_embed`` + ``arctic2_embed``).

HASH SCHEME — the one non-negotiable detail
-------------------------------------------
The sidecar's ``nodes`` map is keyed by the node's STORAGE-LAYER content
signature: ``_content_signature_excluding_updated`` from
``templates/scripts/sync_knowledge_graph.py`` — the FULL 64-hex sha256 of
(frontmatter minus the ``updated:`` line + body). This is deliberately
DISTINCT from the summary sidecar's key (``.node_formats.json`` uses
``sha256(full text)[:16]``). Shipping data keyed by the 16-hex summary hash
would be a silent 100 % ingest miss (the 2026-07-20 hash-scheme-mismatch
incident class), so this tool IMPORTS the real function from the sync script
(never a re-implementation) and a parity test
(``tests/test_v0289_shipped_kg_embeddings.py``) locks generator-key ==
sync-lookup-key.

Chunk decision — replicated from ``sync_node``
----------------------------------------------
Per slot, the chunk layout mirrors what an install ACTIVE on that slot's
model would produce: ``TokenCounter.count_tokens(full file text) <=
chunking_preset_for_model(model_id).max`` → one chunk embedding the FULL
file text; otherwise ``Chunker.for_model(model_id).chunk_text(...)`` and one
vector per ``chunk.content`` (``chunk_num`` 1-indexed, matching
``_shipped_chunk_vector``). Note the layouts legitimately DIFFER per slot
(qwen3's preset max is 13 500 tokens; arctic2's is 3 200 — a handful of
larger nodes are multi-chunk in the arctic sidecar only). The ingest-side
chunk-count guard turns any residual mismatch into a conservative
compute-locally fallback, never a bad ingest.

Backend — LOUD-FAIL by design
-----------------------------
This is a RELEASE tool, not a client path: the per-slot embedding backend is
REQUIRED. The service is constructed directly with the explicit
``--model`` (never ``EmbeddingService.for_project()``, whose env/launcher.db
resolution could silently substitute a different model), the model→slot
mapping is cross-checked against ``TEXT_SLOT_MAP``, and an unreachable
backend / missing model / wrong-dim vector aborts with a non-zero exit.

Context-overflow fallback (token-dense nodes, small-num_ctx models)
-------------------------------------------------------------------
The chunk gate counts tokens with the same approximation the sync path uses
(~4 chars/token); token-DENSE content (LaTeX-heavy nodes) can pass the gate
yet exceed a small model's real context window (arctic2 num_ctx 4 096) —
modern Ollama then rejects the embed with HTTP 400 "input length exceeds the
context length". The production compute path hits the identical wall (the
active-slot embed fails the node; the WP-O secondary fan-out's
4-chars/token ``_bounded_for_model`` budget passes such texts and fails
too), while OLDER Ollama builds silently truncate and embed the leading
window. For those nodes this tool ships what the truncating builds compute:
the full text is tried FIRST (full fidelity for every node that fits); on a
context-overflow 400 the LEADING sub-window is embedded instead, with a
char budget tightened stepwise from the model's num_ctx (ratios 4→3→2→1
chars/token — ratio 1 always fits since BPE tokens are ≥1 char). Every
bounded node is printed loudly and summarized so a release run can't ship
bounded vectors invisibly (an unexpected entry here — e.g. any qwen3 node —
signals an Ollama runner-state problem: re-run against a fresh runner).

Output schema (v0.2.70 ``schema_version`` 1 — unchanged)
--------------------------------------------------------
``{"schema_version": 1, "slot", "model_id", "dim", "nodes": {<sig>:
{"total_chunks": N, "chunks": [{"chunk_num": 1..N, "vector": [...]}]}}}``
Floats are rounded to 7 significant decimals (cosine impact ≪ 1e-6); the
file is written atomically via ``vco_lib.atomic.atomic_write_text``.

Usage
-----
    # Regenerate BOTH canonical slot files (needs local Ollama serving
    # qwen3-embedding:0.6b AND snowflake-arctic-embed2), then self-verify:
    python scripts/build_shipped_kg_embeddings.py

    # Regenerate one slot (model defaults to the slot's canonical model):
    python scripts/build_shipped_kg_embeddings.py \
        --slot qwen3_embed --model qwen3-embedding:0.6b
    python scripts/build_shipped_kg_embeddings.py \
        --slot arctic2_embed --model snowflake-arctic-embed2:latest

    # PRE-TAG GATE (no backend needed): assert every current template
    # node's signature is covered in BOTH slot files with the right chunk
    # counts. Non-zero exit on any gap → wire this into the pre-tag battery:
    python scripts/build_shipped_kg_embeddings.py --verify

Env: ``OLLAMA_URL`` overrides the backend URL (default
``http://localhost:11435``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_KNOWLEDGE = REPO_ROOT / "templates" / "knowledge"
SYNC_SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

# vco_lib lives at the repo root; weaviate_mcp (chunking presets) under
# claude_mcp_servers/. Both are editable-installed on a healthy install, but
# a release checkout may run this tool from a bare venv — pin the repo's own
# copies so generator and sync script resolve the SAME source either way.
for _p in (REPO_ROOT, REPO_ROOT / "claude_mcp_servers"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Mirror of sync_knowledge_graph.py::sync_all_nodes EXCLUDED_FILES — the two
# reference docs are never synced/embedded as nodes, so they ship no vectors.
EXCLUDED_BASENAMES = frozenset({"TAG_HIERARCHY.md", "VOCABULARY.md"})

#: Canonical slot → model for the shipped sidecars. The arctic model id
#: matches ``vco_lib.embedding_service.ARCTIC_SECONDARY_MODEL`` (the model
#: the dual-write secondary fan-out uses), so shipped arctic vectors live in
#: the same embedding space the compute path would populate.
DEFAULT_SLOT_MODELS: Dict[str, str] = {
    "qwen3_embed": "qwen3-embedding:0.6b",
    "arctic2_embed": "snowflake-arctic-embed2:latest",
}

_SIG_HEX_LEN = 64  # full sha256 hexdigest — NOT the 16-hex summary hash

_sync_mod = None


def _sync_module():
    """Load templates/scripts/sync_knowledge_graph.py by path (cached).

    The signature function and the chunking classes are taken FROM the sync
    script so the generator can never drift from the ingest side. Loud-fail:
    a broken import means a broken checkout/venv — surface it, don't degrade
    (per the vco_lib loud-fail rule).
    """
    global _sync_mod
    if _sync_mod is None:
        # Keep the module import hermetic: short-circuit the vct-hub
        # collection resolver (we never touch Weaviate from this tool).
        os.environ.setdefault("VCT_DISABLE_HUB_RESOLVER", "1")
        spec = importlib.util.spec_from_file_location(
            "_shipped_embed_sync_kg", SYNC_SCRIPT_PATH
        )
        if spec is None or spec.loader is None:  # pragma: no cover (defensive)
            raise RuntimeError(f"cannot load sync script at {SYNC_SCRIPT_PATH}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_shipped_embed_sync_kg"] = mod
        spec.loader.exec_module(mod)
        _sync_mod = mod
    return _sync_mod


def content_signature(text: str) -> str:
    """The sidecar lookup key — DELEGATES to the sync script's function.

    ``_content_signature_excluding_updated``: full 64-hex sha256 of
    (frontmatter minus ``updated:`` line + body). Never re-implement this
    here — the parity test pins this delegation.
    """
    return _sync_module()._content_signature_excluding_updated(text)


def chunk_plan(content: str, model_id: str) -> List[Tuple[int, str]]:
    """Replicate ``sync_node``'s chunk decision for *content* under *model_id*.

    Returns ``[(chunk_num, chunk_text), ...]`` with ``chunk_num`` 1-indexed:

      * single-chunk (token count <= the model preset's max): ONE entry whose
        text is the FULL file content — exactly what the single-object path
        embeds (``_build_vector_arg(server, content)``);
      * multi-chunk: ``Chunker.for_model(model_id).chunk_text(content)`` and
        one entry per ``chunk.content`` (the stripped chunk text the
        multi-chunk insert loop embeds), ``chunk_num = chunk.chunk_number+1``.

    Mirrors ``_max_chunk_tokens_for`` (preset max via
    ``chunking_preset_for_model``) + ``_chunker_for`` (``Chunker.for_model``).
    """
    mod = _sync_module()
    from weaviate_mcp.chunking import chunking_preset_for_model

    token_count = mod.TokenCounter.count_tokens(content)
    _min_t, max_t, _tgt_t = chunking_preset_for_model(model_id)
    if token_count <= max_t:
        return [(1, content)]

    chunker = mod.Chunker.for_model(model_id)
    chunks = chunker.chunk_text(
        text=content,
        source_id="shipped-embeddings-plan",
        metadata={},
    )
    return [(c.chunk_number + 1, c.content) for c in chunks]


def iter_shipped_nodes() -> List[Path]:
    """Every current INGESTABLE template node, sorted.

    Mirrors the consumer side exactly:

      * ``sync_all_nodes`` EXCLUDED_FILES (TAG_HIERARCHY.md / VOCABULARY.md
        are reference docs, never embedded);
      * ``sync_node``'s archived skip — nodes whose frontmatter ``status``
        is archived/deprecated/superseded (or whose path holds an archive
        segment) are NEVER embedded by sync, so shipping vectors for them
        would be dead data no install can ingest. The predicate is the
        IMPORTED ``_is_archived_node`` (fed the parsed frontmatter, same as
        sync_node's defence-in-depth second check) — never a local copy.
    """
    mod = _sync_module()
    out: List[Path] = []
    for p in sorted(TEMPLATES_KNOWLEDGE.rglob("*.md")):
        if p.name in EXCLUDED_BASENAMES:
            continue
        frontmatter, _body = mod.parse_frontmatter(p.read_text(encoding="utf-8"))
        if mod._is_archived_node(p, frontmatter=frontmatter or {})[0]:
            continue
        out.append(p)
    return out


def sidecar_path(slot: str) -> Path:
    return TEMPLATES_KNOWLEDGE / f".node_embeddings.{slot}.json"


def _round7(x: float) -> float:
    """Round to 7 significant decimals (≈10 B/number in JSON, §8.3)."""
    v = float(f"{float(x):.7g}")
    if not math.isfinite(v):
        raise ValueError(f"non-finite vector component: {x!r}")
    return v


def _is_ctx_overflow(exc: Exception) -> bool:
    """True for Ollama's context-overflow rejection (HTTP 400 body match)."""
    return "exceeds the context length" in str(exc)


def _embed_with_ctx_fallback(
    embed_fn: Callable[[str], List[float]],
    chunk_text: str,
    model_id: str,
    label: str,
) -> Tuple[List[float], bool]:
    """Embed *chunk_text*; on a context-overflow 400, embed a leading sub-window.

    Returns ``(vector, bounded)``. The full text is ALWAYS tried first (full
    fidelity whenever the model fits it — the compute path's behaviour). On
    overflow, the char budget is tightened from ``num_ctx × 4`` (the shared
    ``_bounded_for_model`` heuristic) down through ratios 3, 2, 1 until the
    backend accepts — ratio 1 (``num_ctx`` chars) always fits because BPE
    tokens consume ≥1 character. This mirrors what silently-truncating
    Ollama builds compute for the same node; see the module docstring.
    Non-overflow errors propagate untouched (loud-fail).
    """
    try:
        return embed_fn(chunk_text), False
    except Exception as exc:  # noqa: BLE001 — inspect, re-raise non-overflow
        if not _is_ctx_overflow(exc):
            raise
    from vco_lib.embedding_service import _num_ctx_for_secondary

    num_ctx = _num_ctx_for_secondary(model_id) or 4096
    last_exc: Optional[Exception] = None
    for ratio in (4, 3, 2, 1):
        budget = num_ctx * ratio
        if budget >= len(chunk_text):
            continue  # window wouldn't shrink the input — same failure again
        try:
            vec = embed_fn(chunk_text[:budget])
            print(
                f"     ⚠️  {label}: full text exceeds {model_id} context "
                f"(num_ctx {num_ctx}); shipped LEADING {budget}-char window "
                f"(ratio {ratio} chars/token)",
                flush=True,
            )
            return vec, True
        except Exception as exc:  # noqa: BLE001 — inspect, re-raise non-overflow
            if not _is_ctx_overflow(exc):
                raise
            last_exc = exc
    raise RuntimeError(
        f"{label}: cannot fit within {model_id}'s context even at "
        f"{num_ctx} chars ({last_exc})"
    )


def build_slot_data(
    slot: str,
    model_id: str,
    embed_fn: Callable[[str], List[float]],
    dim: int,
    *,
    progress: bool = True,
) -> dict:
    """Build the sidecar dict for *slot* by embedding every current node.

    ``embed_fn`` is injectable for tests; the real path is
    ``EmbeddingService.embed_text``. Every vector is validated (length ==
    *dim*, finite floats) BEFORE rounding — a wrong-dim or NaN vector aborts
    the build (release tool: loud-fail, never ship bad data).
    """
    nodes: Dict[str, dict] = {}
    files = iter_shipped_nodes()
    n_chunks_total = 0
    bounded: List[str] = []
    for i, node in enumerate(files, 1):
        rel = node.relative_to(TEMPLATES_KNOWLEDGE).as_posix()
        text = node.read_text(encoding="utf-8")
        sig = content_signature(text)
        if sig in nodes:
            # Two byte-equivalent nodes (mod `updated:` line) share one
            # signature — one entry serves both at ingest time.
            if progress:
                print(f"  [{i}/{len(files)}] {rel} — duplicate signature, reusing entry")
            continue
        plan = chunk_plan(text, model_id)
        chunks_out: List[dict] = []
        for chunk_num, chunk_text in plan:
            label = f"{rel} chunk {chunk_num}"
            vec, was_bounded = _embed_with_ctx_fallback(
                embed_fn, chunk_text, model_id, label
            )
            if was_bounded:
                bounded.append(label)
            if not isinstance(vec, list) or len(vec) != dim:
                raise RuntimeError(
                    f"{label}: backend returned "
                    f"{len(vec) if isinstance(vec, list) else type(vec).__name__} "
                    f"values, expected dim={dim} — refusing to ship"
                )
            chunks_out.append(
                {"chunk_num": chunk_num, "vector": [_round7(x) for x in vec]}
            )
        nodes[sig] = {"total_chunks": len(chunks_out), "chunks": chunks_out}
        n_chunks_total += len(chunks_out)
        if progress:
            print(f"  [{i}/{len(files)}] {rel} ({len(plan)} chunk(s))", flush=True)

    if progress:
        print(f"  → {len(nodes)} node entries, {n_chunks_total} chunk vectors")
        if bounded:
            print(
                f"  ⚠️  {len(bounded)} chunk(s) shipped LEADING-WINDOW vectors "
                f"(context overflow — inspect the list; any unexpected entry "
                f"means an Ollama runner-state problem, re-run):"
            )
            for label in bounded:
                print(f"       - {label}")
    return {
        "schema_version": 1,
        "slot": slot,
        "model_id": model_id,
        "dim": dim,
        "nodes": nodes,
    }


def render_sidecar_json(data: dict) -> str:
    """Compact, deterministic JSON (float noise is the whole file — no indent)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n"


def _build_embedding_service(slot: str, model_id: str):
    """Construct the per-slot EmbeddingService with LOUD preflight checks.

    Direct construction (NOT ``for_project()``): the model must be exactly
    the one requested — env / launcher.db resolution substituting a
    different model here would ship vectors in the wrong embedding space.
    """
    from vco_lib.embedding_service import (
        DEFAULT_CODE_EMBED_URL,
        DEFAULT_OLLAMA_URL,
        DEFAULT_TEXT_MODEL,
        EmbeddingService,
    )

    ollama_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip() or DEFAULT_OLLAMA_URL
    svc = EmbeddingService(
        project_root=REPO_ROOT,
        ollama_url=ollama_url,
        code_embed_url=DEFAULT_CODE_EMBED_URL,
        text_model_id=model_id,
        code_model_id=DEFAULT_TEXT_MODEL,  # unused — text path only
        openai_api_key="",  # local slots only; never touch OpenAI here
    )
    if svc.text_vector_slot != slot:
        raise SystemExit(
            f"❌ --model {model_id!r} maps to slot {svc.text_vector_slot!r}, "
            f"not {slot!r} (TEXT_SLOT_MAP) — refusing to build a cross-model sidecar"
        )
    if not svc.text_backend_ready():
        raise SystemExit(
            f"❌ embedding backend unreachable at {ollama_url} for model "
            f"{model_id!r}. This is a RELEASE tool: the backend is REQUIRED "
            f"(start Ollama and `ollama pull {model_id}`)."
        )
    # Preflight embed: proves the MODEL is actually present (reachability
    # alone doesn't) and that the returned dim matches the slot map.
    try:
        probe = svc.embed_text("shipped-kg-embeddings preflight probe")
    except Exception as exc:
        raise SystemExit(
            f"❌ preflight embed failed for model {model_id!r} at {ollama_url}: "
            f"{exc}\n   (is the model pulled? `ollama pull {model_id}`)"
        )
    if len(probe) != svc.text_dim:
        raise SystemExit(
            f"❌ model {model_id!r} returned {len(probe)}-dim vectors but the "
            f"slot map says {svc.text_dim} — slot/dim drift, refusing to ship"
        )
    return svc


def generate_slot(slot: str, model_id: str) -> Path:
    """Embed every current node for *slot* and atomically write its sidecar."""
    from vco_lib.atomic import atomic_write_text

    print(f"🔨 building {sidecar_path(slot).name} (model={model_id}) …", flush=True)
    svc = _build_embedding_service(slot, model_id)
    try:
        data = build_slot_data(slot, model_id, svc.embed_text, svc.text_dim)
    finally:
        try:
            svc.close()
        except Exception:
            pass
    path = sidecar_path(slot)
    atomic_write_text(path, render_sidecar_json(data))
    # atomic_write_text's mkstemp leaves 0600; shipped template data is
    # world-readable like its siblings (git normalizes to 644 anyway).
    try:
        path.chmod(0o644)
    except OSError:
        pass
    size = path.stat().st_size
    print(f"✅ wrote {path} ({size:,} bytes, {len(data['nodes'])} nodes)")
    return path


def verify_slots(slot_models: Optional[Dict[str, str]] = None) -> List[str]:
    """Assert every current template node is covered in every slot file.

    Returns a list of problem strings (empty == green). Backend-free — this
    is the pre-tag gate leg (``--verify``). Checks per slot file:

      * present + parseable + schema_version 1 + slot field == filename slot;
      * ``model_id`` maps to this slot via TEXT_SLOT_MAP and ``dim`` matches;
      * EVERY current node's signature has an entry whose chunk count equals
        the chunk plan the ingest side would compute (the
        goes-red-on-unregenerated-edit invariant);
      * chunk_nums are exactly 1..N, every vector has ``dim`` finite floats;
      * no stale orphan entries (signatures matching no current node).
    """
    from vco_lib.embedding_service import _resolve_text_slot

    problems: List[str] = []
    for slot, default_model in (slot_models or DEFAULT_SLOT_MODELS).items():
        path = sidecar_path(slot)
        name = path.name
        if not path.is_file():
            problems.append(
                f"{name}: MISSING — regenerate with "
                f"`python scripts/build_shipped_kg_embeddings.py`"
            )
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: unreadable/unparseable ({exc})")
            continue
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            problems.append(f"{name}: schema_version != 1")
            continue
        if data.get("slot") != slot:
            problems.append(
                f"{name}: slot field {data.get('slot')!r} != {slot!r} "
                f"(the file-level slot guard would reject this at ingest)"
            )
        model_id = data.get("model_id") or default_model
        map_slot, map_dim = _resolve_text_slot(model_id)
        if map_slot != slot:
            problems.append(
                f"{name}: model_id {model_id!r} maps to slot {map_slot!r}, "
                f"not {slot!r} — cross-model data"
            )
            continue
        if data.get("dim") != map_dim:
            problems.append(
                f"{name}: dim {data.get('dim')!r} != {map_dim} for {model_id!r}"
            )
        nodes = data.get("nodes")
        if not isinstance(nodes, dict) or not nodes:
            problems.append(f"{name}: nodes map missing/empty")
            continue

        current_sigs: set = set()
        for node in iter_shipped_nodes():
            rel = node.relative_to(TEMPLATES_KNOWLEDGE).as_posix()
            text = node.read_text(encoding="utf-8")
            sig = content_signature(text)
            current_sigs.add(sig)
            entry = nodes.get(sig)
            if not isinstance(entry, dict):
                problems.append(
                    f"{name}: no entry for {rel} (sig {sig[:12]}…) — node "
                    f"edited without regenerating? Run "
                    f"`python scripts/build_shipped_kg_embeddings.py`"
                )
                continue
            expected = chunk_plan(text, model_id)
            chunks = entry.get("chunks")
            if not isinstance(chunks, list) or len(chunks) != len(expected):
                problems.append(
                    f"{name}: {rel} ships "
                    f"{len(chunks) if isinstance(chunks, list) else 'malformed'} "
                    f"chunk(s), ingest expects {len(expected)} — regenerate"
                )
                continue
            if entry.get("total_chunks") != len(chunks):
                problems.append(f"{name}: {rel} total_chunks != len(chunks)")
            nums = sorted(
                c.get("chunk_num") for c in chunks if isinstance(c, dict)
            )
            if nums != list(range(1, len(chunks) + 1)):
                problems.append(f"{name}: {rel} chunk_nums {nums} != 1..{len(chunks)}")
                continue
            for c in chunks:
                vec = c.get("vector")
                if (
                    not isinstance(vec, list)
                    or len(vec) != map_dim
                    or not all(
                        isinstance(x, (int, float)) and math.isfinite(x) for x in vec
                    )
                ):
                    problems.append(
                        f"{name}: {rel} chunk {c.get('chunk_num')} vector "
                        f"malformed (len/dim/finite check failed)"
                    )
                    break

        orphans = set(nodes) - current_sigs
        if orphans:
            problems.append(
                f"{name}: {len(orphans)} stale orphan entr"
                f"{'y' if len(orphans) == 1 else 'ies'} (signature matches no "
                f"current node) — regenerate to prune"
            )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build/verify the shipped KG-embedding sidecars "
        "(templates/knowledge/.node_embeddings.<slot>.json)."
    )
    ap.add_argument(
        "--slot",
        choices=sorted(DEFAULT_SLOT_MODELS),
        help="generate ONE slot (default: generate all canonical slots)",
    )
    ap.add_argument(
        "--model",
        help="embedding model id for --slot (default: the slot's canonical model)",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="verify-only (no backend needed): every current node covered in "
        "every canonical slot file — the pre-tag gate leg",
    )
    args = ap.parse_args(argv)

    if args.verify:
        if args.slot or args.model:
            ap.error("--verify takes no --slot/--model (it checks all canonical slots)")
        problems = verify_slots()
        if problems:
            print("❌ shipped-embeddings verification FAILED:", file=sys.stderr)
            for p in problems:
                print(f"   - {p}", file=sys.stderr)
            return 2
        n_slots = len(DEFAULT_SLOT_MODELS)
        n_nodes = len(iter_shipped_nodes())
        print(f"✅ shipped embeddings verified: {n_nodes} nodes covered in "
              f"{n_slots} slot sidecars")
        return 0

    if args.model and not args.slot:
        ap.error("--model requires --slot")

    if args.slot:
        targets = {args.slot: args.model or DEFAULT_SLOT_MODELS[args.slot]}
    else:
        targets = dict(DEFAULT_SLOT_MODELS)

    for slot, model_id in targets.items():
        generate_slot(slot, model_id)

    # Self-check: a just-generated set must verify green (full-slot runs
    # only — a single-slot regen may legitimately leave the OTHER file stale
    # until its own regen, which --verify at pre-tag will still catch).
    if not args.slot:
        problems = verify_slots()
        if problems:
            print("❌ post-generation verification FAILED:", file=sys.stderr)
            for p in problems:
                print(f"   - {p}", file=sys.stderr)
            return 2
        print("✅ post-generation verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
