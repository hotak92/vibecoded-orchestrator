#!/usr/bin/env python3
"""
Generate LLM summaries for a KG node and store in .node_formats.json.

Called by PostToolUse hook on knowledge/**/*.md edits.

Three-tier model selection (in order):
  1. `claude` CLI on PATH      → best quality, requires CLI install (Max sub or API key)
  2. Ollama (local, FREE)      → http://localhost:11435, no extra dep beyond what
                                  the orchestrator already requires for embeddings
  3. ANTHROPIC_API_KEY direct  → opt-in fallback, cost warning logged
  4. Silent skip               → friendly log line, exits 0

Env overrides:
  KG_SUMMARY_BACKEND        → force "cli" | "ollama" | "api" | "skip" (auto-detect default)
  KG_SUMMARY_OLLAMA_MODEL   → Ollama model tag (default: qwen3.5:9b for 16GB+ VRAM,
                                                          gemma4:e4b for low-VRAM/CPU)
  KG_SUMMARY_OLLAMA_URL     → Ollama base URL (default: http://localhost:11435)
  KG_SUMMARY_TIMEOUT        → per-call timeout seconds (default: 180)

For multi-chunk nodes: generates both a whole-node summary and per-chunk summaries.
For single-chunk nodes: generates description + summary only.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OLLAMA_DEFAULT_MODEL = os.getenv("KG_SUMMARY_OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_URL = os.getenv("KG_SUMMARY_OLLAMA_URL", "http://localhost:11435").rstrip("/")
TIMEOUT = int(os.getenv("KG_SUMMARY_TIMEOUT", "180"))

# Project root (where knowledge/ + .claude/ live): defaults to the script's
# parent.parent.parent (Claude orchestrator), but overridable via
# KG_PROJECT_ROOT env var so this script can summarize KG nodes in *other*
# repos (e.g. per-project VCO-installed projects).
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_PROJECT = Path(os.getenv("KG_PROJECT_ROOT", str(_DEFAULT_ROOT))).resolve()
FORMATS_PATH = CLAUDE_PROJECT / "knowledge" / ".node_formats.json"
KNOWLEDGE_DIR = CLAUDE_PROJECT / "knowledge"
LOG_PATH = CLAUDE_PROJECT / ".claude" / "logs" / "kg-summary-generator.log"


def _resolve_orchestrator_root() -> Path:
    """Find the orchestrator clone root (which contains claude_mcp_servers/).

    PR-2 portability contract: per-project installs of this script need to
    locate the orchestrator's Python deps. Resolution chain:
      1. VCT_ORCHESTRATOR_ROOT env var (set by the launcher when invoking us
         from create_project_v2 / retry_kg_summary).
      2. CLAUDE_PROJECT itself if it contains claude_mcp_servers/ (i.e. the
         script is running INSIDE the orchestrator clone).
      3. Script's parent.parent.parent — historical fallback for the
         orchestrator's own .claude/scripts/.
    Returns the resolved path; the import call site verifies the
    claude_mcp_servers subdir actually exists before using it.
    """
    env = os.getenv("VCT_ORCHESTRATOR_ROOT", "").strip()
    if env:
        candidate = Path(env).resolve()
        if (candidate / "claude_mcp_servers").is_dir():
            return candidate
    if (CLAUDE_PROJECT / "claude_mcp_servers").is_dir():
        return CLAUDE_PROJECT
    return _DEFAULT_ROOT


ORCHESTRATOR_ROOT = _resolve_orchestrator_root()

SYSTEM_PROMPT = (
    "You are a technical documentation summarizer. "
    "Write concise, specific, factual summaries. No filler words, no preamble. "
    "Start directly with the content."
)


def log(msg: str) -> None:
    print(msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


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
    """Read a KG node file. Returns (title, full_text, body)."""
    text = file_path.read_text(encoding="utf-8")
    title = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            body = parts[2].strip()
            for line in fm.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("'\"")
    if not title:
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    return title, text, body


# ──────────────────────────────────────────────────────────────────────
# Backend: Claude CLI
# ──────────────────────────────────────────────────────────────────────
def cli_available() -> bool:
    return shutil.which("claude") is not None


def call_cli(prompt: str) -> str:
    import subprocess

    # Resolve the absolute path so subprocess honors PATHEXT on Windows
    # (where `claude` may ship as `claude.cmd` / `claude.bat` via npm).
    # cli_available() already returned True via shutil.which, but Python's
    # subprocess.run won't apply PATHEXT to bare names on Windows.
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise RuntimeError("claude CLI not found on PATH at call time")

    full_prompt = SYSTEM_PROMPT + "\n\n" + prompt
    result = subprocess.run(
        [claude_path, "-p", full_prompt, "--model", "haiku", "--max-turns", "1"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
    return result.stdout.strip()


# ──────────────────────────────────────────────────────────────────────
# Backend: Ollama
# ──────────────────────────────────────────────────────────────────────
# Per-model generation params for short technical summarization. Override
# via env: KG_SUMMARY_OLLAMA_OPTIONS='{"temperature":0.5,"num_ctx":16000}'
#
# num_ctx 24576 (24k) comfortably fits ~3 × 8k chunks of input + system + output.
# Our prompts are ~1.5k tokens (4000-char body truncation) + 350 num_predict.
#
# qwen3.5:* defaults to thinking-mode and emits <think>...</think> blocks
# unless suppressed. Ollama exposes a `think: false` toggle on /api/generate
# (added in 0.5+). We pass it AND post-strip any leaked think blocks defensively.
OLLAMA_MODEL_DEFAULTS: dict[str, dict] = {
    "qwen3.5": {
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_ctx": 32768,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "qwen3": {  # fallback for plain qwen3 tags
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_ctx": 32768,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "gemma4": {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
        "num_ctx": 32768,
        "num_predict": 1024,
    },
    "gemma3": {  # fallback for plain gemma3 tags
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
        "num_ctx": 32768,
        "num_predict": 1024,
    },
}


def _ollama_options_for(model: str) -> dict:
    user_override = os.getenv("KG_SUMMARY_OLLAMA_OPTIONS")
    if user_override:
        try:
            return json.loads(user_override)
        except json.JSONDecodeError:
            pass
    family = model.split(":", 1)[0].lower()
    return OLLAMA_MODEL_DEFAULTS.get(family, {
        "temperature": 0.4,
        "top_p": 0.9,
        "num_ctx": 32768,
        "num_predict": 1024,
    })


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (qwen3 family)."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def call_ollama(prompt: str, model: str = OLLAMA_DEFAULT_MODEL) -> str:
    options = _ollama_options_for(model)
    body = {
        "model": model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": options,
    }
    family = model.split(":", 1)[0].lower()
    if family.startswith("qwen3"):
        body["think"] = False  # Ollama 0.5+ recognizes this; older versions ignore
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _strip_think_blocks(data.get("response", "").strip())


# ──────────────────────────────────────────────────────────────────────
# Backend: Anthropic API (direct)
# ──────────────────────────────────────────────────────────────────────
def api_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def call_api(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    payload = json.dumps(
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 350,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


# ──────────────────────────────────────────────────────────────────────
# Tier dispatch
# ──────────────────────────────────────────────────────────────────────
_BACKEND_CACHE: dict[str, str] = {}


def select_backend() -> str:
    """Pick the best backend on first call, cache for subsequent prompts."""
    if "choice" in _BACKEND_CACHE:
        return _BACKEND_CACHE["choice"]

    forced = os.getenv("KG_SUMMARY_BACKEND", "").lower().strip()
    if forced in {"cli", "ollama", "api", "skip"}:
        _BACKEND_CACHE["choice"] = forced
        log(f"  KG-summary backend: {forced} (forced via env)")
        return forced

    if cli_available():
        _BACKEND_CACHE["choice"] = "cli"
        log("  KG-summary backend: cli (claude on PATH)")
        return "cli"
    if ollama_available():
        _BACKEND_CACHE["choice"] = "ollama"
        log(f"  KG-summary backend: ollama ({OLLAMA_DEFAULT_MODEL})")
        return "ollama"
    if api_available():
        _BACKEND_CACHE["choice"] = "api"
        log("  KG-summary backend: api (ANTHROPIC_API_KEY) — costs apply")
        return "api"

    _BACKEND_CACHE["choice"] = "skip"
    log(
        "  KG-summary: no backend available (no claude CLI, no Ollama at "
        f"{OLLAMA_URL}, no ANTHROPIC_API_KEY). Skipping."
    )
    return "skip"


def call_llm(prompt: str) -> str:
    backend = select_backend()
    if backend == "cli":
        return call_cli(prompt)
    if backend == "ollama":
        return call_ollama(prompt)
    if backend == "api":
        return call_api(prompt)
    raise RuntimeError("no backend available")


# ──────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────
def generate_description(title: str, body: str) -> str:
    body_trunc = body[:3000] + ("..." if len(body) > 3000 else "")
    prompt = f"""Summarize this knowledge node in exactly 3-4 sentences.
Sentence 1: What it is (definition/purpose).
Sentence 2-3: Key technical details or implementation specifics.
Sentence 4: Why it matters / when to use it.

Title: {title}

Content:
{body_trunc}"""
    return call_llm(prompt)


def generate_summary(title: str, body: str) -> str:
    body_trunc = body[:4000] + ("..." if len(body) > 4000 else "")
    prompt = f"""Write a 1-2 sentence summary of this knowledge node. Be maximally specific and technical. Include the single most important fact.

Title: {title}

Content:
{body_trunc}"""
    return call_llm(prompt)


def generate_chunk_summary(title: str, chunk_num: int, total: int, chunk_content: str) -> str:
    prompt = f"""Write a 1-sentence summary of this section (chunk {chunk_num}/{total}) of the knowledge node "{title}". Be specific about what THIS chunk covers.

Content:
{chunk_content[:2000]}"""
    return call_llm(prompt)


def get_chunks_from_weaviate(title: str) -> list[tuple[int, str]]:
    try:
        # ORCHESTRATOR_ROOT honors VCT_ORCHESTRATOR_ROOT (PR-2 portability)
        # so per-project installs find claude_mcp_servers/ in the orchestrator
        # clone, not in the project being summarized.
        sys.path.insert(0, str(ORCHESTRATOR_ROOT / "claude_mcp_servers"))
        import weaviate
        from weaviate.classes.query import Filter
        from urllib.parse import urlparse

        kg_collection = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
        # Honor WEAVIATE_URL when the launcher (or env) sets it to a non-default
        # endpoint; otherwise default to localhost:8081 to match historical behavior.
        weaviate_url = urlparse(os.getenv("WEAVIATE_URL", "http://localhost:8081"))
        weaviate_host = weaviate_url.hostname or "localhost"
        weaviate_port = weaviate_url.port or 8081
        weaviate_grpc = int(os.getenv("GRPC_PORT", "50052"))
        client = weaviate.connect_to_local(host=weaviate_host, port=weaviate_port, grpc_port=weaviate_grpc)
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

    try:
        rel_path = str(file_path.relative_to(CLAUDE_PROJECT))
    except ValueError:
        rel_path = str(file_path)

    title, full_text, body = read_node(file_path)
    if not title:
        # Pre-write hook should have blocked this, but guard anyway.
        log(f"  No title in {rel_path} (pre-write hook should have caught this), skipping")
        sys.exit(0)

    c_hash = content_hash(full_text)
    formats = load_formats()
    existing = formats.get(rel_path, {})
    if not args.force and existing.get("content_hash") == c_hash:
        print(f"  {title}: unchanged (hash match), skipping")
        sys.exit(0)

    if select_backend() == "skip":
        sys.exit(0)

    log(f"  Generating summaries for: {title}")

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
        "backend": _BACKEND_CACHE.get("choice", "?"),
    }

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
    log(f"  Saved {rel_path} via {entry['backend']}")


if __name__ == "__main__":
    main()
