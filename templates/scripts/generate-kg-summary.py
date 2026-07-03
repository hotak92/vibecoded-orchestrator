#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Generate LLM summaries for a KG node and store in .node_formats.json.

Called by PostToolUse hook on knowledge/**/*.md edits.

Four-tier model selection (in order):
  1. `claude` CLI on PATH      → best quality, requires CLI install (Max sub or API key)
                                 v0.2.23 C10: gated by a smoke-test, not just --version,
                                 so an installed-but-unauthenticated CLI doesn't get picked.
  2. Ollama (local, FREE)      → http://localhost:11435, no extra dep beyond what
                                 the orchestrator already requires for embeddings
  3. OpenAI API (opt-in)       → gated by `kg_summary_openai_consent` app_state key
                                 (default false). Set via launcher Preferences → KG
                                 Summaries. Bypass via `--force-api`. Costs apply.
  4. ANTHROPIC_API_KEY direct  → legacy opt-in fallback. Cost warning logged.
  5. Silent skip               → friendly log line, exits 0

Env overrides:
  KG_SUMMARY_BACKEND        → force "cli" | "ollama" | "api" | "openai" | "skip"
                              (auto-detect default; "api" = Anthropic, "openai" = OpenAI)
  KG_SUMMARY_OLLAMA_MODEL   → Ollama model tag (default: qwen3.5:9b for 16GB+ VRAM,
                                                          gemma4:e4b for low-VRAM/CPU)
  KG_SUMMARY_OLLAMA_URL     → Ollama base URL (default: http://localhost:11435)
  KG_SUMMARY_OPENAI_MODEL   → OpenAI model name (default: gpt-4o-mini — cheapest
                              summary-capable model as of 2026-05-21)
  KG_SUMMARY_TIMEOUT        → per-call timeout seconds (default: 180)

Flags:
  --force-api               → bypass the openai-consent gate (operator override; use
                              when scripting from CI / a one-shot terminal where the
                              launcher app_state is not yet seeded).

For multi-chunk nodes: generates both a whole-node summary and per-chunk summaries.
For single-chunk nodes: generates description + summary only.

v0.2.73 M2: the 4-tier backend ladder was EXTRACTED verbatim into the sibling
module ``summary_backends.py`` (one home — the code-summary generator is the
second caller). This script is now a thin caller; behaviour is unchanged.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# The shared backend ladder lives beside this script (templates/scripts/ in
# the orchestrator clone; .claude/scripts/ in installed projects — the bundle
# ships *.py side-by-side, vco_lib/bundle_globs.py::script_patterns).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import summary_backends as _sb  # noqa: E402

# Re-exports — callers + tests historically accessed the ladder through THIS
# module's namespace (e.g. tests/test_kg_summary_openai_consent.py sets
# mod._FORCE_API and calls mod.select_backend()); keep every name reachable.
ANTHROPIC_MODEL = _sb.ANTHROPIC_MODEL
OLLAMA_DEFAULT_MODEL = _sb.OLLAMA_DEFAULT_MODEL
OLLAMA_URL = _sb.OLLAMA_URL
TIMEOUT = _sb.TIMEOUT
OPENAI_DEFAULT_MODEL = _sb.OPENAI_DEFAULT_MODEL
OPENAI_API_URL = _sb.OPENAI_API_URL
APP_STATE_KEY_OPENAI_CONSENT = _sb.APP_STATE_KEY_OPENAI_CONSENT
APP_STATE_KEY_OPENAI_MODEL = _sb.APP_STATE_KEY_OPENAI_MODEL
OLLAMA_MODEL_DEFAULTS = _sb.OLLAMA_MODEL_DEFAULTS
SYSTEM_PROMPT = _sb.SYSTEM_PROMPT

cli_available = _sb.cli_available
call_cli = _sb.call_cli
_ollama_options_for = _sb._ollama_options_for
_strip_think_blocks = _sb._strip_think_blocks
ollama_available = _sb.ollama_available
call_ollama = _sb.call_ollama
api_available = _sb.api_available
call_api = _sb.call_api
openai_available = _sb.openai_available
_read_app_state_value = _sb._read_app_state_value
openai_consent_granted = _sb.openai_consent_granted
_openai_model = _sb._openai_model
call_openai = _sb.call_openai

# SAME dict object as the shared module's cache (main() reads
# _BACKEND_CACHE["choice"] for the sidecar entry's `backend` field).
# Cleared on (re)import so a freshly-loaded script module starts with no
# cached backend choice — exactly the pre-extraction semantics, where the
# cache was a module-level dict of THIS module (the consent tests re-import
# this script per case and rely on that isolation).
_BACKEND_CACHE = _sb._BACKEND_CACHE
_sb.reset_backend_cache()

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


def log(msg: str) -> None:
    print(msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


# Route the shared ladder's log lines through this script's file-backed log
# (pre-extraction, the ladder called this module's log() directly).
_sb.set_logger(log)


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
# Tier dispatch — thin wrappers over the shared ladder (summary_backends)
# ──────────────────────────────────────────────────────────────────────
# Operator override: when --force-api is passed on the CLI we bypass
# the consent gate for OpenAI. Module-level state (read at call time by
# the wrappers below) because select_backend is called from multiple
# entry points (generate_description / generate_summary /
# generate_chunk_summary) per run; threading it through every call
# would be noisy. Tests set `mod._FORCE_API = True` directly.
_FORCE_API = False


def select_backend() -> str:
    """Pick the best backend on first call, cache for subsequent prompts.

    Thin wrapper: the selection order + consent gate live in
    ``summary_backends.select_backend`` (v0.2.73 M2 extraction — behaviour
    identical). This wrapper only feeds in this script's ``_FORCE_API``.
    """
    return _sb.select_backend(force_api=_FORCE_API)


def call_llm(prompt: str) -> str:
    return _sb.call_llm(prompt, force_api=_FORCE_API)


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

        # v0.2.21 Step 18: resolve KG collection via the launcher's vct-hub.
        # Falls back to env when the hub is unreachable (matches the
        # pre-v0.2.21 path). CLAUDE_PROJECT is the project being summarised.
        try:
            from vco_lib.project_config import resolve as _vco_resolve  # type: ignore[import-not-found]
            _cfg = _vco_resolve(CLAUDE_PROJECT)
            kg_collection = _cfg.kg_collection or os.getenv(
                "KG_COLLECTION", "KnowledgeGraph"
            )
        except Exception:
            kg_collection = os.getenv("KG_COLLECTION", "KnowledgeGraph")
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
    parser.add_argument(
        "--force-api", action="store_true",
        help=(
            "Bypass the kg_summary_openai_consent gate (operator override). "
            "Use when scripting from CI or when the launcher Preferences "
            "GUI is not available."
        ),
    )
    args = parser.parse_args()

    if args.force_api:
        # Module-level flag picked up by select_backend(). The CLI flag
        # ONLY affects this single run; it doesn't write to app_state.
        global _FORCE_API
        _FORCE_API = True

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
