#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Upload `docs/**/*.md` to the Weaviate Development collection.

Companion to `sync_knowledge_graph.py`. Where that script handles
`knowledge/` files (KG nodes), this one handles `docs/` files (longer-form
project documentation — guides, runbooks, the extended features wiki).

Triggered by:
  - `install.py` Step 7c: bulk-upload `--all` after Weaviate collections
    are created, so a fresh install is searchable out of the box.
  - `.claude/hooks/post-file-edit.sh`: incremental upload on each
    `docs/**.md` edit.

Idempotency: each file gets a deterministic UUID5 derived from
`(file_path, chunk_number)`. Re-running `--all` overwrites existing rows
in place; never duplicates. A pre-write sweep deletes rows for the same
`file_path` that no longer correspond to a current chunk (handles the
file-got-shorter case).

Usage:
  python upload_docs.py <relative-or-absolute path>
  python upload_docs.py --all

Environment (defaults match the orchestrator's compose.yaml):
  WEAVIATE_URL              http://localhost:8081
  GRPC_PORT                 50052
  OLLAMA_URL                http://localhost:11435
  EMBEDDING_MODEL           qwen3-embedding:0.6b
  DEVELOPMENT_COLLECTION    Development         # `KG_COLLECTION` for the KG
  DUAL_EMBEDDING_ENABLED    true                # enable dual qwen3+ollama write

Excludes (never uploaded):
  - docs/license/                        # license source files, not docs
  - docs/features/_drafts/               # multi-agent pipeline scratch
  - docs/features/_*.md                  # multi-agent pipeline artifacts
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Bundled chunking helper from the MCP server. Same one
# `sync_knowledge_graph.py` uses, so chunk sizes line up across collections.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers"))

import weaviate  # type: ignore  # client v4 (already in requirements.txt)
from weaviate.classes.query import Filter  # type: ignore
from weaviate_mcp.chunking import Chunker, TokenCounter  # type: ignore


# Configuration
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
COLLECTION_NAME = os.getenv("DEVELOPMENT_COLLECTION", "Development")
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Conservative working limit — embedding model nominally supports 8k tokens
# but throughput / quality drops past ~2.5k. Same as sync_knowledge_graph.py.
MAX_EMBEDDING_TOKENS = 2500

# UUIDv5 namespace used to derive deterministic chunk UUIDs from file_path.
# Random one-off; fine to be a constant here. Different from the KG namespace
# so a file_path collision (extremely unlikely) wouldn't conflict cross-collection.
UUID_NS_DOCS = uuid.UUID("8afb95ad-fcc3-4d85-8b3c-872d23331fc0")

# Project root — same env vars as the rest of the orchestrator's scripts.
_kg_base_dir = os.getenv("KG_BASE_DIR", "")
PROJECT_ROOT = (
    Path(_kg_base_dir)
    if _kg_base_dir
    else Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
)
DOCS_ROOT = PROJECT_ROOT / "docs"


def _is_excluded(rel_path: Path) -> bool:
    """Skip license sources + multi-agent pipeline artifacts."""
    parts = rel_path.parts
    if "license" in parts:
        return True
    if "features" in parts and "_drafts" in parts:
        return True
    name = rel_path.name
    if rel_path.parent.name == "features" and name.startswith("_") and name.endswith(".md"):
        return True
    return False


def _ollama_embed(text: str) -> list[float]:
    """POST text to Ollama and return the embedding vector.

    Raises on HTTP error or missing 'embedding' key — caller must handle.
    """
    body = json.dumps({
        "model": EMBEDDING_MODEL,
        "prompt": text,
        "options": {"num_ctx": 8192},  # qwen3-embedding requires this
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise RuntimeError(f"Ollama returned no embedding (response keys: {list(data.keys())})")
    return vec


def _connect() -> weaviate.WeaviateClient:
    host_part = WEAVIATE_URL.replace("http://", "").replace("https://", "")
    http_host = host_part.split(":")[0]
    http_port = int(host_part.split(":")[-1]) if ":" in host_part else 8080
    return weaviate.connect_to_custom(
        http_host=http_host,
        http_port=http_port,
        http_secure=False,
        grpc_host=http_host,
        grpc_port=GRPC_PORT,
        grpc_secure=False,
    )


def _collection_uses_named_vectors(client: weaviate.WeaviateClient) -> bool:
    """Detect whether the target collection has named vectors configured.

    Some installs (development, e.g. the Claude orchestrator) configure
    `qwen3_embed` / `ollama_embed` / `openai_embed` named vectors; others
    (fresh installs from this repo) use a single unnamed vector. The
    insert payload differs (dict vs list), so detect at runtime.
    """
    if not client.collections.exists(COLLECTION_NAME):
        return False
    try:
        cfg = client.collections.get(COLLECTION_NAME).config.get()
        vectors = getattr(cfg, "vector_config", None)
        # vectors is None for legacy single-vector configs; dict-like for named
        if vectors is None:
            return False
        # Some weaviate-client versions return a dict; others a Mapping
        try:
            return len(list(vectors.keys())) > 0
        except AttributeError:
            return bool(vectors)
    except Exception:
        return False


def _wrap_vector(vec: list[float], named: bool) -> object:
    """Adapt the vector to whichever shape the collection expects."""
    if not named:
        return vec
    if DUAL_EMBEDDING_ENABLED:
        # qwen3 is the active default; ollama is the legacy slot. Same
        # vector goes into both so RL reranking has both available.
        return {"qwen3_embed": vec, "ollama_embed": vec}
    return {"qwen3_embed": vec}


def _chunk_uuid(file_path: str, chunk_number: int) -> str:
    """Deterministic UUID5 keyed off file_path + chunk index."""
    return str(uuid.uuid5(UUID_NS_DOCS, f"{file_path}::{chunk_number}"))


def _delete_existing_for_path(client: weaviate.WeaviateClient, file_path: str) -> int:
    """Sweep prior rows for a given file_path before writing fresh ones.

    Handles the file-got-shorter case (a 12-chunk doc shrinking to 8
    chunks would leave 4 stale rows behind without this sweep). On
    collections that don't declare `file_path` as a filterable property,
    the sweep is silently skipped — UUID5-keyed inserts still upsert
    by ID, so re-runs of unchanged files remain idempotent; only
    file-shrink-orphans accumulate (rare; cosmetic).

    Returns the number of rows deleted (0 if sweep was skipped).
    """
    try:
        coll = client.collections.get(COLLECTION_NAME)
    except Exception:
        return 0
    try:
        result = coll.data.delete_many(
            where=Filter.by_property("file_path").equal(file_path)
        )
        # weaviate-client returns a `BatchDeleteReturn` with .successful
        return int(getattr(result, "successful", 0) or 0)
    except Exception as e:
        msg = str(e)
        # "no such prop" = legacy schema without file_path indexed → silent skip
        if "no such prop" in msg.lower() or "not found in" in msg.lower():
            return 0
        print(f"  ! delete_existing failed for {file_path}: {e}", flush=True)
        return 0


def _extract_title(path: Path, content: str) -> str:
    """Title = first H1 if present, else filename stem."""
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


def _upload_one(
    client: weaviate.WeaviateClient,
    abs_path: Path,
    rel_path_str: str,
    chunker: Chunker,
    counter: TokenCounter,
    named: bool,
) -> tuple[int, int]:
    """Read, chunk, embed, upsert. Returns (chunks_written, bytes_processed)."""
    content = abs_path.read_text(encoding="utf-8", errors="replace")
    title = _extract_title(abs_path, content)
    n_tokens = counter.count_tokens(content)
    coll = client.collections.get(COLLECTION_NAME)

    # Sweep prior rows so re-runs don't accumulate orphans
    _delete_existing_for_path(client, rel_path_str)

    if n_tokens <= MAX_EMBEDDING_TOKENS:
        chunks_iter: Iterable[tuple[str, int, int]] = [(content, 1, 1)]
    else:
        chunked = chunker.chunk_text(
            content,
            source_id=rel_path_str,
            metadata={"title": title, "file_path": rel_path_str},
        )
        chunks_iter = [
            (c.content, c.chunk_number, c.total_chunks) for c in chunked
        ]

    written = 0
    for chunk_content, chunk_n, total in chunks_iter:
        try:
            vec = _ollama_embed(chunk_content)
        except Exception as e:
            print(f"  ! embedding failed for {rel_path_str} chunk {chunk_n}/{total}: {e}", flush=True)
            continue

        properties = {
            "title": title if total == 1 else f"{title} ({chunk_n}/{total})",
            "content": chunk_content,
            "file_path": rel_path_str,
        }
        chunk_id = _chunk_uuid(rel_path_str, chunk_n)
        wrapped = _wrap_vector(vec, named)

        # Upsert: try insert first; on duplicate-UUID, replace in place.
        # The pre-write sweep above usually prevents duplicates, but on
        # legacy schemas where the sweep was skipped (no file_path
        # property) we fall back to replace so re-runs stay idempotent.
        try:
            coll.data.insert(properties=properties, uuid=chunk_id, vector=wrapped)
            written += 1
            continue
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  ! insert failed for {rel_path_str} chunk {chunk_n}/{total}: {e}", flush=True)
                continue

        try:
            coll.data.replace(uuid=chunk_id, properties=properties, vector=wrapped)
            written += 1
        except Exception as e:
            print(f"  ! replace failed for {rel_path_str} chunk {chunk_n}/{total}: {e}", flush=True)

    return written, len(content)


def _walk_docs() -> list[Path]:
    """All `docs/**/*.md` files, post-exclusion, sorted for stable ordering."""
    if not DOCS_ROOT.is_dir():
        return []
    out: list[Path] = []
    for p in DOCS_ROOT.rglob("*.md"):
        rel = p.relative_to(PROJECT_ROOT)
        if _is_excluded(rel.relative_to("docs") if rel.parts[0] == "docs" else rel):
            continue
        out.append(p)
    out.sort()
    return out


def _resolve_input(arg: str) -> tuple[Path, str] | None:
    """Map a user-supplied path to (absolute_path, project-relative-string)."""
    p = Path(arg)
    if not p.is_absolute():
        p = (PROJECT_ROOT / arg).resolve()
    if not p.is_file():
        print(f"!  not a file: {p}", flush=True)
        return None
    try:
        rel = p.relative_to(PROJECT_ROOT)
    except ValueError:
        print(f"!  outside project root: {p}", flush=True)
        return None
    if _is_excluded(rel.relative_to("docs") if rel.parts[0] == "docs" else rel):
        print(f"!  excluded by policy: {rel}", flush=True)
        return None
    if not str(rel).startswith("docs/"):
        print(f"!  not under docs/: {rel}", flush=True)
        return None
    return p, str(rel)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    arg = sys.argv[1]
    try:
        client = _connect()
    except Exception as e:
        print(f"!  cannot connect to Weaviate at {WEAVIATE_URL}: {e}", flush=True)
        print("   (skip if Weaviate isn't running yet — install.py expects this to be soft-fail)", flush=True)
        return 1

    try:
        if not client.collections.exists(COLLECTION_NAME):
            print(f"!  collection '{COLLECTION_NAME}' does not exist yet — run install.py Step 7b first", flush=True)
            return 1

        named = _collection_uses_named_vectors(client)
        counter = TokenCounter()
        chunker = Chunker(min_tokens=400, max_tokens=MAX_EMBEDDING_TOKENS, target_tokens=1200)

        if arg == "--all":
            files = _walk_docs()
            if not files:
                print(f"   no .md files found under {DOCS_ROOT} (nothing to upload)")
                return 0
            print(f"📚 Uploading {len(files)} doc files to {COLLECTION_NAME} (named_vectors={named})")
            t0 = time.time()
            total_chunks = 0
            for i, p in enumerate(files, 1):
                rel = str(p.relative_to(PROJECT_ROOT))
                print(f"[{i}/{len(files)}] {rel}", flush=True)
                chunks, _ = _upload_one(client, p, rel, chunker, counter, named)
                total_chunks += chunks
            elapsed = time.time() - t0
            print(f"✓ Uploaded {total_chunks} chunks across {len(files)} files in {elapsed:.1f}s")
            return 0

        resolved = _resolve_input(arg)
        if resolved is None:
            return 1
        abs_path, rel = resolved
        print(f"📚 Uploading {rel} (named_vectors={named})")
        chunks, _ = _upload_one(client, abs_path, rel, chunker, counter, named)
        print(f"✓ {chunks} chunks written")
        return 0
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
