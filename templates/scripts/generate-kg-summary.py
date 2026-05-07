#!/usr/bin/env python3
"""
Generate LLM summaries for a KG node and store in .node_formats.json.

Called by PostToolUse hook on knowledge/**/*.md edits.
Uses Claude Haiku via the Anthropic API for high-quality summaries.

For multi-chunk nodes: generates both a whole-node summary and per-chunk summaries.
For single-chunk nodes: generates description + summary only.

Schema in .node_formats.json:
{
  "knowledge/concepts/foo.md": {
    "title": "Foo Pattern",
    "description": "3-4 sentence description of what, how, why.",
    "summary": "1-2 sentence whole-node summary (always present).",
    "chunk_summaries": {           # only for multi-chunk nodes
      "1": "Summary of chunk 1...",
      "2": "Summary of chunk 2...",
    },
    "total_chunks": 3,
    "generated_at": "2026-04-10T...",
    "content_hash": "sha256..."    # skip regeneration if unchanged
  }
}
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Haiku model ID
MODEL = "claude-haiku-4-5-20251001"

CLAUDE_PROJECT = Path(__file__).resolve().parent.parent.parent
FORMATS_PATH = CLAUDE_PROJECT / "knowledge" / ".node_formats.json"
KNOWLEDGE_DIR = CLAUDE_PROJECT / "knowledge"


def load_formats() -> dict:
    if FORMATS_PATH.exists():
        return json.loads(FORMATS_PATH.read_text(encoding="utf-8"))
    return {}


def save_formats(data: dict) -> None:
    FORMATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FORMATS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_node(file_path: Path) -> tuple[str, str, str]:
    """Read a KG node file. Returns (title, frontmatter_yaml, body)."""
    text = file_path.read_text(encoding="utf-8")
    title = ""
    body = text
    # Parse YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            body = parts[2].strip()
            for line in fm.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("'\"")
    if not title:
        # Fallback: first # heading
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    return title, text, body


def call_haiku(prompt: str, max_tokens: int = 300) -> str:
    """Call Claude Haiku via the `claude` CLI (inherits OAuth auth).

    Uses `claude -p` with --model haiku for single-turn prompt execution.
    This avoids needing a raw API key — the CLI handles authentication.
    """
    import subprocess

    full_prompt = (
        "You are a technical documentation summarizer. "
        "Write concise, specific, factual summaries. No filler words, no preamble. "
        "Start directly with the content.\n\n" + prompt
    )

    result = subprocess.run(
        ["claude", "-p", full_prompt, "--model", "haiku", "--max-turns", "1"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
    return result.stdout.strip()


def generate_description(title: str, body: str) -> str:
    """3-4 sentence description: what, key details, why it matters."""
    # Truncate body to ~3000 chars for the prompt
    body_trunc = body[:3000] + ("..." if len(body) > 3000 else "")
    prompt = f"""Summarize this knowledge node in exactly 3-4 sentences.
Sentence 1: What it is (definition/purpose).
Sentence 2-3: Key technical details or implementation specifics.
Sentence 4: Why it matters / when to use it.

Title: {title}

Content:
{body_trunc}"""
    return call_haiku(prompt, max_tokens=250)


def generate_summary(title: str, body: str) -> str:
    """1-2 sentence whole-node summary."""
    body_trunc = body[:4000] + ("..." if len(body) > 4000 else "")
    prompt = f"""Write a 1-2 sentence summary of this knowledge node. Be maximally specific and technical. Include the single most important fact.

Title: {title}

Content:
{body_trunc}"""
    return call_haiku(prompt, max_tokens=120)


def generate_chunk_summary(title: str, chunk_num: int, total: int, chunk_content: str) -> str:
    """1-2 sentence summary of a specific chunk."""
    prompt = f"""Write a 1-sentence summary of this section (chunk {chunk_num}/{total}) of the knowledge node "{title}". Be specific about what THIS chunk covers.

Content:
{chunk_content[:2000]}"""
    return call_haiku(prompt, max_tokens=100)


def get_chunks_from_weaviate(title: str) -> list[tuple[int, str]]:
    """Fetch all chunks for a node from Weaviate, sorted by chunk_num.

    PR-2 portability (2026-05-06): the weaviate Python client is the only
    thing imported here, but historically sys.path was also extended to
    pick up claude_mcp_servers/ from the orchestrator clone. Honor
    $VCT_ORCHESTRATOR_ROOT first, fall back to in-tree resolution.
    """
    try:
        # VCO-REWIRE-BEGIN: orchestrator-root-resolution
        env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
        if env_root and (Path(env_root) / "claude_mcp_servers").is_dir():
            sys.path.insert(0, str(Path(env_root) / "claude_mcp_servers"))
        else:
            sys.path.insert(0, str(CLAUDE_PROJECT / "claude_mcp_servers"))
        # VCO-REWIRE-END: orchestrator-root-resolution
        import weaviate
        from weaviate.classes.query import Filter

        kg_collection = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
        client = weaviate.connect_to_local(host="localhost", port=8081, grpc_port=50052)
        coll = client.collections.get(kg_collection)
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=20,
        )
        chunks = []
        for obj in resp.objects:
            cn = obj.properties.get("chunk_num", 1) or 1
            content = obj.properties.get("content", "")
            chunks.append((cn, content))
        client.close()
        chunks.sort(key=lambda x: x[0])
        return chunks
    except Exception as e:
        print(f"  Warning: couldn't fetch chunks from Weaviate: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(description="Generate KG node summaries")
    parser.add_argument("file", help="Path to knowledge .md file")
    parser.add_argument("--force", action="store_true", help="Regenerate even if content unchanged")
    args = parser.parse_args()

    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    # Compute relative path for the key
    try:
        rel_path = str(file_path.relative_to(CLAUDE_PROJECT))
    except ValueError:
        rel_path = str(file_path)

    title, full_text, body = read_node(file_path)
    if not title:
        print(f"  No title found in {rel_path}, skipping", file=sys.stderr)
        sys.exit(0)

    # Check if content changed
    c_hash = content_hash(full_text)
    formats = load_formats()
    existing = formats.get(rel_path, {})
    if not args.force and existing.get("content_hash") == c_hash:
        print(f"  {title}: unchanged (hash match), skipping")
        sys.exit(0)

    print(f"  Generating summaries for: {title}")

    # Generate description + summary from the full body
    description = generate_description(title, body)
    summary = generate_summary(title, body)
    print(f"  Description: {description[:80]}...")
    print(f"  Summary: {summary[:80]}...")

    entry = {
        "title": title,
        "description": description,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": c_hash,
    }

    # Check for multi-chunk nodes
    chunks = get_chunks_from_weaviate(title)
    total_chunks = len(chunks) if chunks else 1

    if total_chunks > 1:
        print(f"  Multi-chunk node ({total_chunks} chunks), generating per-chunk summaries...")
        chunk_summaries = {}
        for cn, chunk_content in chunks:
            cs = generate_chunk_summary(title, cn, total_chunks, chunk_content)
            chunk_summaries[str(cn)] = cs
            print(f"    Chunk {cn}: {cs[:60]}...")
        entry["chunk_summaries"] = chunk_summaries
        entry["total_chunks"] = total_chunks

    formats[rel_path] = entry
    save_formats(formats)
    print(f"  Saved to {FORMATS_PATH}")


if __name__ == "__main__":
    main()
