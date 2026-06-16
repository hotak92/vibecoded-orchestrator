#!/usr/bin/env python3
"""
Generate description, summary, and per-chunk summaries for KG nodes.

Uses Ollama (free) or Haiku API. Stores formats in a sidecar JSON file
(knowledge/.node_formats.json) — NOT in the .md frontmatter. This keeps
source files clean and avoids bloating full-node retrievals with redundant
summaries.

Sidecar entry shape (matches the on-edit hook generate-kg-summary.py):
    {
      "title": "...",
      "description": "<6-line bullet list>",
      "summary": "<≤100-line summary>",
      "generated_at": "ISO timestamp",
      "content_hash": "<8-hex>",            # for dedup against re-runs
      "chunk_summaries": {"1": "...", ...}, # only if total_chunks > 1
      "total_chunks": N                      # only if multi-chunk
    }

For multi-chunk nodes, chunks are fetched from Weaviate (which produced them
during sync) and each gets a 1-sentence summary surfaced by auto-tier
retrieval (`three_chunks` / `full` tiers in hybrid_search).

Usage:
    python generate_node_formats.py knowledge/tools/leanctx.md   # single node
    python generate_node_formats.py --all                          # all nodes
    python generate_node_formats.py --all --dry-run               # preview only
    python generate_node_formats.py --all --force                 # regenerate existing
"""

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11435")

# Use 9B+ models only (quality matters for summaries)
MODEL_CANDIDATES = ["qwen3.5:9b", "granite4:7b"]

# Knowledge base root (two levels up from this script, overridable via --knowledge-dir)
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
FORMATS_FILE = KNOWLEDGE_DIR / ".node_formats.json"


def get_available_models() -> list[str]:
    """Return list of model names available in Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except requests.RequestException as e:
        print(f"ERROR: Cannot reach Ollama at {OLLAMA_URL}: {e}", file=sys.stderr)
        print("Make sure Ollama is running: podman-compose up -d ollama", file=sys.stderr)
        sys.exit(1)


def pick_model(candidates: list[str], available: list[str]) -> str:
    """Pick the first candidate model that is available."""
    for model in candidates:
        if model in available:
            return model
    # Fallback: any non-embedding model
    non_embed = [m for m in available if "embed" not in m.lower() and "jina" not in m.lower()]
    if non_embed:
        return non_embed[0]
    print("ERROR: No usable text generation model found in Ollama.", file=sys.stderr)
    sys.exit(1)


def call_ollama(model: str, prompt: str, num_predict: int) -> str:
    """Call Ollama generate API and return response text.

    Uses think=False to disable chain-of-thought for models that support it
    (qwen3.x series). Without this, thinking tokens consume the budget and
    the response field is empty.
    """
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.3, "num_predict": num_predict},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.Timeout:
        raise RuntimeError(f"Ollama request timed out (model={model})")
    except requests.RequestException as e:
        raise RuntimeError(f"Ollama request failed: {e}")


def call_haiku(prompt: str, max_tokens: int) -> str:
    """Call Claude Haiku via the Anthropic API.

    Requires ANTHROPIC_API_KEY environment variable.
    Use this on machines without a GPU where Ollama is too slow.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Required for --provider haiku. "
            "Set it: export ANTHROPIC_API_KEY=sk-ant-..."
        )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except requests.Timeout:
        raise RuntimeError("Haiku API request timed out")
    except requests.RequestException as e:
        raise RuntimeError(f"Haiku API request failed: {e}")


# Active provider — set by main() based on --provider flag
_provider = "ollama"
_ollama_model = ""


def call_llm(prompt: str, max_tokens: int) -> str:
    """Route to the active provider (ollama or haiku)."""
    if _provider == "haiku":
        return call_haiku(prompt, max_tokens)
    return call_ollama(_ollama_model, prompt, max_tokens)


def generate_description(content: str, title: str, model: str) -> str:
    """Generate a 6-line description.

    Post-processes output to keep exactly 6 non-empty lines regardless of
    how many the model produces.
    """
    prompt = (
        f"Write a brief description of this knowledge node as bullet points. "
        f"Cover: what it is, key features (3-4 points), use case, and why it matters. "
        f"Output only short single-sentence lines, no headers or blank lines.\n\n"
        f"Title: {title}\n\nContent:\n{content[:3000]}"
    )
    raw = call_llm(prompt, max_tokens=300)
    # Trim to exactly 6 non-empty lines (model may produce more or fewer)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return "\n".join(lines[:6])


def generate_summary(content: str, title: str, model: str) -> str:
    """Generate a ≤100-line summary.

    Post-processes to enforce the 100-line hard cap.
    """
    prompt = (
        f"Summarize this knowledge node concisely. "
        f"Preserve all important facts, decisions, and relationships. "
        f"Omit verbose code examples and repetitive details. "
        f"Output only the summary text.\n\n"
        f"Title: {title}\n\nContent:\n{content[:8000]}"
    )
    raw = call_llm(prompt, max_tokens=2000)
    lines = raw.splitlines()
    if len(lines) > 100:
        lines = lines[:100]
    return "\n".join(lines)


def generate_chunk_summary(title: str, chunk_num: int, total: int, chunk_content: str) -> str:
    """Generate a 1-sentence summary of a single chunk (matches generate-kg-summary.py)."""
    prompt = (
        f"Write a 1-sentence summary of this section (chunk {chunk_num}/{total}) of "
        f"the knowledge node \"{title}\". Be specific about what THIS chunk covers.\n\n"
        f"Content:\n{chunk_content[:2000]}"
    )
    return call_llm(prompt, max_tokens=200).strip()


def get_chunks_from_weaviate(title: str) -> list[tuple[int, str]]:
    """Fetch chunks for a node by title from Weaviate.

    Returns sorted list of (chunk_number, content) tuples. Empty list if
    the node is single-chunk or Weaviate is unreachable.
    """
    try:
        # Add the project package to the path so we can import weaviate.
        sys.path.insert(0, str(PROJECT_ROOT / "claude_mcp_servers"))
        import weaviate
        from weaviate.classes.query import Filter

        # v0.2.21 Step 18: resolve KG collection via the launcher's
        # vct-hub; fall back to env (ClaudeKnowledgeGraph default kept
        # for pre-v0.2.21 callers).
        try:
            from vco_lib.project_config import resolve as _vco_resolve  # type: ignore[import-not-found]
            _cfg = _vco_resolve(PROJECT_ROOT)
            kg_collection = _cfg.kg_collection or os.getenv(
                "KG_COLLECTION", "ClaudeKnowledgeGraph"
            )
        except Exception:
            kg_collection = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
        client = weaviate.connect_to_local(host="localhost", port=8081, grpc_port=50052)
        try:
            coll = client.collections.get(kg_collection)
            resp = coll.query.fetch_objects(
                filters=Filter.by_property("title").equal(title),
                limit=20,
            )
            chunks: list[tuple[int, str]] = []
            for obj in resp.objects:
                props = obj.properties or {}
                cn = props.get("chunk_number")
                content = props.get("content", "")
                if cn is not None and content:
                    chunks.append((int(cn), content))
            chunks.sort(key=lambda c: c[0])
            return chunks
        finally:
            client.close()
    except Exception as e:
        # Weaviate unreachable, schema mismatch, or no python client. Treat as
        # single-chunk — chunk_summaries will be skipped for this run.
        print(f"  WARN: could not fetch chunks for {title!r}: {e}", file=sys.stderr)
        return []


def content_hash(text: str) -> str:
    """Short stable hash for dedup against re-runs (matches generate-kg-summary.py)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_frontmatter(file_path: Path) -> Optional[tuple[str, str, str]]:
    """
    Parse YAML frontmatter from a markdown file.

    Returns (raw_frontmatter, body, title) or None if no frontmatter found.
    """
    text = file_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not match:
        return None
    frontmatter = match.group(1)
    body = match.group(2)

    # Extract title from frontmatter
    title_match = re.search(r"^title:\s*(.+)$", frontmatter, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else file_path.stem

    return frontmatter, body, title


def load_formats_db() -> dict:
    """Load the sidecar JSON formats database."""
    if FORMATS_FILE.exists():
        import json
        return json.loads(FORMATS_FILE.read_text(encoding="utf-8"))
    return {}


def save_formats_db(db: dict) -> None:
    """Save the sidecar JSON formats database."""
    import json
    FORMATS_FILE.write_text(
        json.dumps(db, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def has_formats(rel_path: str, db: dict, c_hash: str | None = None) -> bool:
    """Return True if the entry is complete and (optionally) content-hash matches.

    Complete = has description + summary. If `c_hash` is provided, also
    requires the stored content_hash to match (so edits trigger regen).
    """
    entry = db.get(rel_path, {})
    if not (entry.get("description") and entry.get("summary")):
        return False
    if c_hash is not None and entry.get("content_hash") != c_hash:
        return False
    return True


def store_formats(
    rel_path: str,
    title: str,
    description: str,
    summary: str,
    db: dict,
    c_hash: str | None = None,
    chunk_summaries: dict | None = None,
    total_chunks: int | None = None,
) -> None:
    """Store generated formats in the sidecar DB (not in the .md file).

    For multi-chunk nodes, pass chunk_summaries + total_chunks so the
    auto-tier retrieval can surface per-chunk summaries.
    """
    from datetime import datetime, timezone
    entry: dict = {
        "title": title,
        "description": description,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if c_hash is not None:
        entry["content_hash"] = c_hash
    if chunk_summaries:
        entry["chunk_summaries"] = chunk_summaries
    # Always record total_chunks (2026-06-15) — previously written ONLY for
    # multi-chunk nodes, so a single-chunk node had `total_chunks` ABSENT,
    # indistinguishable from "never computed". Consumers (auto-tier retrieval,
    # the "does tier:full cover the whole node?" check) need to know the chunk
    # count for every node. Single-chunk nodes get total_chunks=1; multi-chunk
    # nodes keep their real count. Falls back to len(chunk_summaries) when an
    # explicit count wasn't threaded, else 1 (the node embeds as one chunk).
    if total_chunks is not None:
        entry["total_chunks"] = total_chunks
    elif chunk_summaries:
        entry["total_chunks"] = len(chunk_summaries)
    else:
        entry["total_chunks"] = 1
    db[rel_path] = entry


def find_all_nodes() -> list[Path]:
    """Return all .md files under the knowledge directory."""
    return sorted(KNOWLEDGE_DIR.rglob("*.md"))


def process_node(
    file_path: Path,
    model: str,
    dry_run: bool,
    force: bool,
    db: dict,
) -> str:
    """
    Process a single knowledge node. Returns status string.

    Status values: "skipped", "generated", "dry_run", "error:<msg>"
    """
    result = parse_frontmatter(file_path)
    if result is None:
        return "error:no_frontmatter"

    frontmatter, body, title = result
    rel_path = str(file_path.relative_to(PROJECT_ROOT)) if file_path.is_relative_to(PROJECT_ROOT) else str(file_path)

    full_content = body.strip()
    c_hash = content_hash(full_content)

    # Skip if entry is complete AND content hash matches (no edits since last gen)
    if not force and has_formats(rel_path, db, c_hash):
        return "skipped"

    if dry_run:
        return "dry_run"

    try:
        description = generate_description(full_content, title, model)
        summary = generate_summary(full_content, title, model)
    except RuntimeError as e:
        return f"error:{e}"

    # Multi-chunk handling: fetch chunks from Weaviate; if N>1, generate
    # per-chunk summaries so auto-tier retrieval can surface them.
    chunk_summaries: dict | None = None
    total_chunks: int | None = None
    chunks = get_chunks_from_weaviate(title)
    if len(chunks) > 1:
        total_chunks = len(chunks)
        chunk_summaries = {}
        for cn, chunk_content in chunks:
            try:
                cs = generate_chunk_summary(title, cn, total_chunks, chunk_content)
                chunk_summaries[str(cn)] = cs
            except RuntimeError as e:
                # Don't fail the whole node if one chunk summary fails — just skip it.
                print(f"  WARN: chunk {cn} summary failed for {title!r}: {e}", file=sys.stderr)

    store_formats(
        rel_path,
        title,
        description,
        summary,
        db,
        c_hash=c_hash,
        chunk_summaries=chunk_summaries,
        total_chunks=total_chunks,
    )
    return "generated"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate description and summary formats for KG nodes."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Single .md file to process (relative to project root or absolute)",
    )
    parser.add_argument("--all", action="store_true", help="Process all nodes in knowledge/")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without writing files"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate formats even if they already exist",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "haiku"],
        default="ollama",
        help="LLM provider: ollama (free, needs GPU) or haiku (paid, no GPU needed)",
    )
    parser.add_argument(
        "--knowledge-dir",
        metavar="PATH",
        default=None,
        help=(
            "Override the knowledge directory (default: <project_root>/knowledge). "
            "Use an absolute path or one relative to cwd. "
            "Example: --knowledge-dir /path/to/your-project/knowledge"
        ),
    )
    args = parser.parse_args()

    if not args.file and not args.all:
        parser.print_help()
        sys.exit(1)

    # Apply --knowledge-dir override before any path operations
    global KNOWLEDGE_DIR, FORMATS_FILE, PROJECT_ROOT
    if args.knowledge_dir is not None:
        override = Path(args.knowledge_dir).resolve()
        if not override.exists():
            print(f"ERROR: --knowledge-dir path does not exist: {override}", file=sys.stderr)
            sys.exit(1)
        KNOWLEDGE_DIR = override
        FORMATS_FILE = KNOWLEDGE_DIR / ".node_formats.json"
        PROJECT_ROOT = KNOWLEDGE_DIR.parent

    # Set up provider
    global _provider, _ollama_model
    _provider = args.provider

    if args.provider == "haiku":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set. Required for --provider haiku.", file=sys.stderr)
            sys.exit(1)
        model = "claude-haiku-4-5"
        print(f"Using provider: Haiku (API)")
    else:
        available = get_available_models()
        model = pick_model(MODEL_CANDIDATES, available)
        _ollama_model = model
        print(f"Using provider: Ollama ({model})")

    # Load sidecar formats database
    db = load_formats_db()

    # Collect files to process
    if args.all:
        files = find_all_nodes()
    else:
        path = Path(args.file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            print(f"ERROR: File not found: {path}", file=sys.stderr)
            sys.exit(1)
        files = [path]

    total = len(files)
    skipped = generated = errors = dry_count = 0
    session_start = time.time()

    for i, file_path in enumerate(files, 1):
        node_start = time.time()
        rel = file_path.relative_to(PROJECT_ROOT) if file_path.is_relative_to(PROJECT_ROOT) else file_path

        status = process_node(file_path, model, args.dry_run, args.force, db)
        elapsed = time.time() - node_start

        if status == "skipped":
            skipped += 1
            print(f"[{i}/{total}] SKIP  {rel} (already has formats)")
        elif status == "dry_run":
            dry_count += 1
            print(f"[{i}/{total}] DRY   {rel}")
        elif status == "generated":
            generated += 1
            print(f"[{i}/{total}] OK    {rel}  ({elapsed:.1f}s)")
        else:
            errors += 1
            msg = status.replace("error:", "")
            print(f"[{i}/{total}] ERROR {rel}: {msg}")

    # Save the formats database
    if generated > 0 and not args.dry_run:
        save_formats_db(db)
        print(f"Saved formats to {FORMATS_FILE}")

    total_elapsed = time.time() - session_start
    print(
        f"\nDone in {total_elapsed:.1f}s — "
        f"generated: {generated}, skipped: {skipped}, "
        f"dry-run: {dry_count}, errors: {errors}"
    )


if __name__ == "__main__":
    main()
