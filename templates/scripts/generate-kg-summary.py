#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Generate LLM summaries for a KG node and store in .node_formats.json.

The ONE writer of `knowledge/.node_formats.json` (v0.2.103 merge). Called in
two modes:

* per-node — PostToolUse hook on knowledge/**/*.md edits, and the launcher's
  per-file walk (`launcher/src-tauri/src/commands/kg_summary.rs`):
  `generate-kg-summary.py <file> [--force] [--force-api] [--dry-run]`
* bulk — the manual `--all` backfill, the post-sync refresh
  (`sync_knowledge_graph._regen_node_formats_after_full_sync`) and the
  schema-migration regeneration (`vco_lib/project_init.py::
  _regenerate_node_formats`):
  `generate-kg-summary.py --all [--knowledge-dir DIR] [--force] [--dry-run]`

The bulk mode was merged in from the retired Ollama-only second writer of
the same sidecar (v0.2.103, owner decision 2026-10-14 — it loaded a second
generation model on the GPU beside the code-embedding re-index and OOMed in
the field); every LLM call in BOTH modes goes through the shared summary
ladder below.

Backend ladder (in order; one home: templates/scripts/summary_backends.py):
  1. `claude` CLI on PATH      → best quality, requires CLI install (Max sub or API key)
                                 v0.2.23 C10: gated by a smoke-test, not just --version,
                                 so an installed-but-unauthenticated CLI doesn't get picked.
  2. z.ai GLM                  → only when the vendor key resolves through the gateway
                                 registry (subscription — no consent gate).
  3. QwenCloud                 → same rule as z.ai (subscription — no consent gate).
  4. Ollama (local, FREE)      → http://localhost:11435; skipped when the model is not
                                 pulled (never pulled at runtime); deferred while a
                                 code-graph walk runs or free VRAM is below the floor
                                 sized from the model (not applied when already loaded).
  5. OpenAI API (opt-in)       → the ONLY consent-gated tier: `kg_summary_openai_consent`
                                 app_state key (default false; launcher Preferences → KG
                                 Summaries). Bypass via `--force-api`. Costs apply.
  6. ANTHROPIC_API_KEY direct  → legacy opt-in fallback. Cost warning logged.
  7. Silent skip               → friendly log line, exits 0
A model refusal never descends to a lower tier (owner ruling 2026-10-18):
the node is left without a summary, flagged as refused in the sidecar, and
the NEXT ordinary sync tries it again — the flag is a record, not a skip.

Env overrides:
  KG_SUMMARY_BACKEND        → force "cli" | "zai" | "qwen" | "ollama" | "openai" | "api" | "skip"
                              (auto-detect default; "api" = Anthropic, "openai" = OpenAI).
                              Unset: the launcher Preferences backend pin
  KG_SUMMARY_OLLAMA_MODEL   → Ollama model tag. Unset: the machine's generation
                              model (vco_lib.summary_model — Preferences pick,
                              else the model install recorded and pulled, else
                              the hardware tier: the strongest model whose
                              free-VRAM floor the card meets — qwen3.5:9b,
                              else qwen3.5:4b; gemma4:e4b on a capable CPU
                              with no GPU; qwen3.5:0.8b floor)
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

v0.2.92 WP-Q: two behaviours arrive from that shared module.
  * A tier that stops serving (usage limit, auth, sustained capacity) is
    DEMOTED and the next tier answers, instead of every remaining node
    spawning another doomed `claude -p`. The latch is cross-process
    because this script runs ONCE PER NODE.
  * A model NON-ANSWER ("Ready. What do you need summarized?", a refusal,
    an empty reply) is never written to the sidecar AS A SUMMARY, and an
    EXISTING entry holding one is regenerated even though its content hash
    still matches — a row that never held a valid summary is not
    "satisfied" by it. The hash gate itself is unchanged and still skips
    healthy unchanged nodes. v0.2.104 (owner ruling 2026-10-18): a
    REFUSAL-shaped non-answer additionally leaves a `refused` flag entry in
    the sidecar (no summary text — the flag is a record, and never a usable
    entry, so the next ordinary sync retries the node).
"""

import argparse
import hashlib
import json
import os
import sys
import time
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
# v0.2.92 WP-Q additions — same "keep every name reachable" contract.
is_non_answer = _sb.is_non_answer
BackendUnavailable = _sb.BackendUnavailable
NonAnswerResponse = _sb.NonAnswerResponse
ContentRefusal = _sb.ContentRefusal


def __getattr__(name: str):
    # v0.2.103: `OLLAMA_DEFAULT_MODEL` stays reachable through THIS
    # namespace (the "keep every name reachable" contract above) but is
    # resolved LAZILY on first access — summary_backends resolves the
    # machine's generation model (and, when vco_lib is missing from the
    # interpreter, prints its stderr line) only when the Ollama tier is
    # actually consulted, not on every per-node import.
    if name == "OLLAMA_DEFAULT_MODEL":
        return getattr(_sb, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    """Save the sidecar JSON ATOMICALLY.

    Ported from the retired bulk generator (v0.2.103 merge):
    `.node_formats.json` holds EVERY node's LLM summary, so a plain
    truncate-write killed mid-save corrupts the whole DB (regenerable, but
    at full LLM-regeneration cost). The atomic primitive is the ONE shared
    home (``vco_lib.atomic.atomic_write_text``) — the same call the retired
    script made, and legal here because this script already hard-imports
    ``vco_lib.kg_node_status`` for the archived predicate (the repo rule:
    a failed ``import vco_lib`` is a broken install, never a fallback).
    """
    from vco_lib.atomic import atomic_write_text

    FORMATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(FORMATS_PATH, payload)


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


#: Stable substring of the per-node line printed when a node is ARCHIVED.
#: MUST MATCH ``launcher/src-tauri/src/commands/kg_summary.rs::
#: ARCHIVED_MARKER`` — the launcher counts a node carrying it as skipped
#: (``nodes_unchanged``), never failed or succeeded.
ARCHIVED_SKIP_MARKER = "archived, skipping"


def _archived_reason(file_path: Path, full_text: str) -> str:
    """The shared predicate's reason when *file_path* is an ARCHIVED node,
    else ``""``.

    ONE predicate, no second copy (owner decision 2026-10-14: archived
    nodes skip the Weaviate sync AND the summarization):
    ``vco_lib.kg_node_status.is_archived_content`` — the same rule the
    sync's skip/delete, the seed's change check and the drift scan use.
    The import is HARD by the repo rule (a failed ``import vco_lib`` is a
    broken install, never a fallback case): quietly degrading to "not
    archived" would keep summarizing — and keep sidecar entries alive for —
    nodes the sync deliberately removed from Weaviate.
    """
    from vco_lib.kg_node_status import is_archived_content

    archived, reason = is_archived_content(file_path, full_text)
    return reason if archived else ""


def _drop_sidecar_entry(rel_path: str, formats: dict) -> bool:
    """Remove *rel_path*'s entry from the sidecar dict. True when dropped.

    The sidecar must not keep SERVING summaries for a node the sync removed
    from Weaviate: retrieval renders whatever the sidecar holds, so a stale
    archived entry would keep a summary alive for a node that no longer
    exists in the graph.
    """
    if rel_path in formats:
        del formats[rel_path]
        return True
    return False


def _sidecar_key(file_path: Path) -> str:
    """The canonical sidecar key for *file_path*: project-relative POSIX.

    v0.2.103 B1: ``str(Path.relative_to(...))`` keyed entries with ``\\``
    on Windows, while every reader looks them up by the canonical POSIX
    ``file_path`` Weaviate stores — so every summary lookup missed there.
    One home for the key shape: ``vco_lib.node_formats_keys.sidecar_key``.
    """
    from vco_lib.node_formats_keys import sidecar_key

    return sidecar_key(file_path, CLAUDE_PROJECT)


def _node_file_for_key(rel_path: str) -> Path:
    """The node file a sidecar key names (either separator accepted).

    A ``knowledge/``-prefixed key resolves under KNOWLEDGE_DIR, so a
    ``--knowledge-dir`` override is honoured; anything else is
    project-relative.
    """
    from vco_lib.paths import to_posix_rel

    rel = to_posix_rel(rel_path)
    node_prefix = KNOWLEDGE_DIR.name + "/"
    if rel.startswith(node_prefix):
        return KNOWLEDGE_DIR / rel[len(node_prefix):]
    return CLAUDE_PROJECT / rel


def _current_hash_for_key(rel_path: str) -> "str | None":
    """The node's current canonical content hash, or ``None`` if unreadable."""
    node_file = _node_file_for_key(rel_path)
    try:
        return content_hash(node_file.read_text(encoding="utf-8"))
    except OSError:
        return None


def rekey_separator_entries(db: dict) -> int:
    """Re-key ``\\``-separated sidecar keys to canonical POSIX, in place.

    v0.2.103 B1 one-time migration of sidecars written on Windows before
    the fix. Idempotent and lossless (``vco_lib.node_formats_keys.
    canonicalize_keys``): when both forms of a key exist, the entry that
    still holds a summary wins, then the one matching the node's current
    content, then the newer ``generated_at``. Returns the keys rewritten.
    """
    from vco_lib.node_formats_keys import canonicalize_keys

    return canonicalize_keys(db, current_hash=_current_hash_for_key)


# ──────────────────────────────────────────────────────────────────────
# Legacy sidecar re-key (ported from the retired bulk generator, v0.2.103)
# ──────────────────────────────────────────────────────────────────────
def _legacy_content_hash(body: str) -> str:
    """The PRE-2026-07-20 storage hash scheme: sha256(body.strip())[:16].

    Kept for exactly one purpose — detecting sidecar entries keyed under the
    old (frontmatter-stripped body) scheme so :func:`rekey_legacy_entries`
    can silently migrate them to the canonical full-text key WITHOUT
    regenerating the summary. Do NOT use this for storing new entries;
    ``content_hash(full_file_text)`` is canonical.
    """
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def rekey_legacy_entries(db: dict) -> int:
    """Silently re-key sidecar entries from the legacy hash scheme to canonical.

    For each entry whose stored ``content_hash`` does NOT match the canonical
    full-text hash of its node file, but DOES match the legacy ``body.strip()``
    hash of that same file, overwrite the stored hash with the canonical value —
    WITHOUT regenerating the (still-valid) summary. This preserves
    skip-unchanged correctness across the 2026-07-20 hash-scheme transition: a
    node that was summarized under the old scheme (by the retired Ollama-only
    bulk generator) and is byte-unchanged is recognized as
    current, not regenerated.

    Entries that match neither scheme (genuinely stale, or already canonical)
    are left untouched — the normal skip/regenerate logic handles them.

    Returns the number of entries re-keyed (for logging).
    """
    rekeyed = 0
    for rel_path, entry in db.items():
        stored = entry.get("content_hash") if isinstance(entry, dict) else None
        if not stored:
            continue
        node_file = _node_file_for_key(rel_path)
        if not node_file.is_file():
            continue
        try:
            full_text = node_file.read_text(encoding="utf-8")
        except OSError:
            continue
        canonical = content_hash(full_text)
        if stored == canonical:
            continue  # already canonical
        _title, _full, body = read_node(node_file)
        if stored == _legacy_content_hash(body):
            entry["content_hash"] = canonical
            rekeyed += 1
    return rekeyed


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


def get_chunks_from_weaviate(title: str, file_path: str = "") -> list[tuple[int, str]]:
    """Fetch this node's stored chunks, sorted by chunk_num.

    W7 (v0.2.92 wiring audit): filtered by title AND ``file_path``. A title
    is not a node identity — measured live, two titles per collection map to
    two different file_paths each — and a title-only filter silently blended
    BOTH nodes' chunks into one node's ``chunk_summaries`` sidecar, which is
    what the retrieval chunk-map header renders. ``file_path`` is the same
    relative path this script keys the sidecar by, so the two agree by
    construction. An empty ``file_path`` degrades to the pre-fix title-only
    filter (no worse than before).
    """
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
        # ── MIRROR (category C) — must match `vco_lib/weaviate_helpers.py::
        #    weaviate_url_default`, the SHARED HOME for this resolution.
        #    Precedence: WEAVIATE_URL > WEAVIATE_PORT > the canonical port;
        #    an empty/whitespace-only value at either level is UNSET, not a
        #    literal. (Before v0.2.96 this honoured only WEAVIATE_URL, so an
        #    install relocated via WEAVIATE_PORT summarised against whatever
        #    answered on 8081.)
        #
        # Why a mirror and not a call: this file treats `vco_lib` as OPTIONAL
        # everywhere — the only use is the `try/except Exception` import a few
        # lines above, and the sys.path insert here adds `claude_mcp_servers`,
        # NOT the vco_lib parent. A hard call would turn a soft dependency into
        # a hard one inside the summary path.
        #
        # `tests/test_v0296_weaviate_url_port_precedence.py::
        # TestShippedMirrorParity` EXECUTES these lines against the shared
        # home on every case, so the two cannot drift.
        _weaviate_url_env = (os.getenv("WEAVIATE_URL") or "").strip()
        _weaviate_port_env = (os.getenv("WEAVIATE_PORT") or "").strip()
        WEAVIATE_URL = _weaviate_url_env or f"http://localhost:{_weaviate_port_env or 8081}"
        weaviate_url = urlparse(WEAVIATE_URL)
        weaviate_host = weaviate_url.hostname or "localhost"
        weaviate_port = weaviate_url.port or 8081
        weaviate_grpc = int(os.getenv("GRPC_PORT", "50052"))
        # v0.2.102 W4-E: no PyPI version lookup on connect (one home:
        # weaviate_helpers.disable_weaviate_client_pypi_check; guarded).
        try:
            from vco_lib.weaviate_helpers import disable_weaviate_client_pypi_check
            disable_weaviate_client_pypi_check()
        except Exception:
            pass
        client = weaviate.connect_to_local(host=weaviate_host, port=weaviate_port, grpc_port=weaviate_grpc)
        coll = client.collections.get(kg_collection)
        chunk_filter = Filter.by_property("title").equal(title)
        if file_path:
            chunk_filter = chunk_filter & Filter.by_property("file_path").equal(
                file_path
            )
        resp = coll.query.fetch_objects(filters=chunk_filter, limit=20)
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


def stored_entry_is_usable(entry: dict) -> bool:
    """True when a cached sidecar entry actually holds summaries.

    v0.2.92 WP-Q: the content-hash gate is correct and stays (it is why an
    unchanged node costs 0 s instead of ~34 s). What it must NOT do is
    treat a row as satisfied by a value that was never a summary — a field
    scan found 43% of one project's entries holding a model non-answer,
    frozen forever because the source file's hash had not changed. Adding
    the validity precondition means those rows heal on the next ordinary
    run, with no user action and no guessing which ones are bad.

    Both `description` and `summary` are required here (unlike the code
    sidecar, where an empty `summary` is legitimate for a trivial body).
    """
    if not isinstance(entry, dict):
        return False
    if _sb.is_non_answer(entry.get("description")):
        return False
    if _sb.is_non_answer(entry.get("summary")):
        return False
    # Chunk summaries are optional ENRICHMENT: a stored one that is a
    # non-answer invalidates the entry, but a MISSING one does not. Absent
    # is not poisoned, and treating it as poisoned would re-run the two
    # whole-node calls on every sync for any node whose chunk fetch keeps
    # failing (Weaviate down, chunk row absent) — churn in exchange for a
    # retrieval-tier nicety. The whole-node description + summary, which
    # are the load-bearing fields, are still validated above.
    chunk_summaries = entry.get("chunk_summaries")
    if isinstance(chunk_summaries, dict) and chunk_summaries:
        if any(_sb.is_non_answer(v) for v in chunk_summaries.values()):
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser (exposed so caller-wiring tests can run the REAL
    parser against the argv each caller spawns — argv-shape-only tests are
    not enough)."""
    parser = argparse.ArgumentParser(description="Generate KG node summaries")
    parser.add_argument(
        "file", nargs="?", help="Path to knowledge .md file (single-node mode)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Process every node under knowledge/ (bulk backfill)",
    )
    parser.add_argument("--force", action="store_true", help="Regenerate even if content unchanged")
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Show what would be done and write nothing — no summary, no "
            "sidecar re-key, no archived-entry drop (both modes)"
        ),
    )
    parser.add_argument(
        "--knowledge-dir", metavar="PATH", default=None,
        help=(
            "Override the knowledge directory (default: <project>/knowledge). "
            "Use an absolute path or one relative to cwd. The sidecar is "
            "written inside it and the project root becomes its parent. "
            "Example: --knowledge-dir /path/to/your-project/knowledge"
        ),
    )
    parser.add_argument(
        "--force-api", action="store_true",
        help=(
            "Bypass the kg_summary_openai_consent gate (operator override). "
            "Use when scripting from CI or when the launcher Preferences "
            "GUI is not available."
        ),
    )
    return parser


def _record_refusal(formats: dict, rel_path: str, title: str, c_hash: str,
                    exc: "Exception") -> None:
    """Flag the node as REFUSED in the sidecar (owner ruling 2026-10-18).

    The entry deliberately carries NO ``description``/``summary``: the
    refusal text is never cached, and ``stored_entry_is_usable`` rejects the
    entry, so the content-hash gate can NOT skip the node — the NEXT
    ordinary sync tries it again. The flag is a visible record (which node,
    when, which backend, the reply's shape), not a skip marker. The stored
    ``refusal`` string is the ladder's own message, which reports the
    reply's SHAPE (char count), never its text.
    """
    formats[rel_path] = {
        "title": title,
        "refused": True,
        "refused_at": datetime.now(timezone.utc).isoformat(),
        "refusal": str(exc),
        "content_hash": c_hash,
        "backend": _BACKEND_CACHE.get("choice", "?"),
    }


def _process_node_bulk(
    file_path: Path, *, force: bool, dry_run: bool, formats: dict
) -> str:
    """Process one node in ``--all`` mode. Returns a status string.

    Status values: ``"skipped"``, ``"generated"``, ``"dry_run"``,
    ``"archived"``, ``"archived_dropped"``, ``"refused:<msg>"``,
    ``"nonanswer:<msg>"``, ``"error:<msg>"`` (the first four keep the
    retired bulk generator's contract, so the progress/tally lines callers
    parse are unchanged). Every LLM call goes through the shared ladder.

    A REFUSAL (the model declined the content) raises
    :class:`summary_backends.ContentRefusal` there and is reported as a
    ``refused:`` status here: the node is left without a summary, flagged in
    the sidecar (``_record_refusal``), and retried by the next ordinary sync
    — the flag is never a usable entry, so the hash gate cannot freeze it
    (owner ruling 2026-10-18). It is NOT an error for the exit code: an
    error would arm the auto-retry/deferral machinery over content this
    backend may never answer, and the ruling asks only for the next ORDINARY
    sync to retry. A non-refusal non-answer keeps the old contract:
    ``nonanswer:`` status, nothing cached.
    """
    title, full_text, body = read_node(file_path)
    if not title:
        return "error:no title (neither frontmatter title nor H1) — skipped"
    rel_path = _sidecar_key(file_path)

    # Archived nodes skip the summarization entirely (owner decision
    # 2026-10-14 — the same shared predicate the Weaviate sync skips and
    # deletes rows by), and any existing sidecar entry for them is DROPPED:
    # the sidecar must not keep serving summaries for a node the graph no
    # longer holds.
    archived = _archived_reason(file_path, full_text)
    if archived:
        dropped = _drop_sidecar_entry(rel_path, formats)
        return "archived_dropped" if dropped else "archived"

    c_hash = content_hash(full_text)
    existing = formats.get(rel_path, {})
    if (
        not force
        and existing.get("content_hash") == c_hash
        and stored_entry_is_usable(existing)
    ):
        return "skipped"
    if dry_run:
        return "dry_run"

    try:
        description = generate_description(title, body)
        summary = generate_summary(title, body)
    except _sb.ContentRefusal as exc:
        # Owner ruling 2026-10-18: a refusal does NOT descend tiers and is
        # NOT an error — the node is flagged refused in the sidecar (a
        # record, never a cached answer) and the next ordinary sync retries
        # it, since the flagged entry is never a usable one.
        _record_refusal(formats, rel_path, title, c_hash, exc)
        return f"refused:{exc}"
    except _sb.NonAnswerResponse as exc:
        # A non-refusal non-answer: never cached (the hash gate must not
        # freeze it) and NOT an error for the exit code — with nothing
        # cached the next ordinary sync retries the node anyway.
        return f"nonanswer:{exc}"
    except RuntimeError as exc:
        return f"error:{exc}"

    entry: dict = {
        "title": title,
        "description": description,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": c_hash,
        "backend": _BACKEND_CACHE.get("choice", "?"),
    }

    chunks = get_chunks_from_weaviate(title, rel_path)
    total_chunks = len(chunks) if chunks else 1
    if total_chunks > 1:
        chunk_summaries = {}
        for cn, chunk_content in chunks:
            try:
                cs = generate_chunk_summary(title, cn, total_chunks, chunk_content)
            except RuntimeError as exc:
                # Includes NonAnswerResponse. Omitting the chunk keeps the
                # (valid) whole-node description + summary; caching a
                # non-answer here would make the entry unusable AND
                # hash-frozen. The omission is regenerated next run.
                log(f"    Chunk {cn}: not stored — {exc}")
                continue
            chunk_summaries[str(cn)] = cs
        if chunk_summaries:
            entry["chunk_summaries"] = chunk_summaries
        entry["total_chunks"] = total_chunks

    formats[rel_path] = entry
    return "generated"


#: Bulk exit code: NO summary backend was selectable this run (the ladder
#: returned ``skip`` — VRAM/code-graph-walk guard, or no CLI and no keys).
#: "Owed, retry later": nothing was generated, so the nodes stay owed for a
#: run when a backend IS available. Distinct from 1 (per-node errors) so the
#: bulk callers can record it WITHOUT arming the auto-retry that re-runs a
#: full ``--all`` sync (review v0.2.103 M-2). ``2`` is argparse's; pinned
#: across the process boundary by
#: ``tests/test_v02103_nodeformats_merge.py``.
BULK_EXIT_NO_BACKEND = 3


def _run_bulk(args: argparse.Namespace) -> int:
    """The ``--all`` pass over every node under KNOWLEDGE_DIR.

    Returns the process exit code:

    * ``0`` — the pass COMPLETED. Nodes may have been skipped (unchanged,
      archived), refused (``refused:`` — flagged in the sidecar, retried by
      the next ordinary sync; owner ruling 2026-10-18) or unanswered
      (``nonanswer:`` — nothing cached): those ARE success; the tally line
      names the counts so nothing is invisible.
    * ``1`` — a node errored for a non-refusal reason (a tier's transport
      failed, a parse broke). Potentially transient → the bulk callers
      (kg-sync's post-sync refresh, the schema-migration regeneration)
      treat it as owed work and record a deferral. Incremental persistence
      (below) keeps a re-run cheap: only the failed nodes regenerate.
    * :data:`BULK_EXIT_NO_BACKEND` — no summary backend was selectable at
      all. "Owed, retry later" — kg-sync records the environmental
      ``kg_node_formats_backend_unavailable`` condition for it, NOT the
      auto-retryable refresh-failed one (a deferral must not raise a
      never-clearing retry that re-runs a full sync).

    The sidecar is saved ATOMICALLY after EACH generated node (and each
    dropped archived entry) — never only at the end: a bulk run over a
    fresh install can spend hours on the ladder's CLI tier, and an
    all-or-nothing save meant a kill mid-run discarded every node already
    paid for (review v0.2.103 H-3). There is deliberately NO whole-run
    wall-clock cap: per-LLM-call timeouts
      (``summary_backends.TIMEOUT``, default 180 s) already bound a hung
      backend, and a cap would cut valid long work (owner rule: shipped
      timeouts are very long by design).
    """
    files = sorted(KNOWLEDGE_DIR.rglob("*.md")) if KNOWLEDGE_DIR.is_dir() else []
    if not files:
        print(f"No knowledge .md files under {KNOWLEDGE_DIR} — nothing to do.")
        return 0

    formats = load_formats()
    # Silently migrate entries still keyed under the legacy body.strip()
    # hash scheme to the canonical full-text key (no regeneration). The
    # re-key must persist even if no node needs regeneration this run.
    n_sep = rekey_separator_entries(formats)
    if n_sep and not args.dry_run:
        save_formats(formats)
        print(
            f"Re-keyed {n_sep} sidecar "
            f"entr{'y' if n_sep == 1 else 'ies'} to canonical POSIX paths."
        )
    n_rekeyed = rekey_legacy_entries(formats)
    if n_rekeyed and not args.dry_run:
        save_formats(formats)
        print(
            f"Re-keyed {n_rekeyed} legacy sidecar "
            f"entr{'y' if n_rekeyed == 1 else 'ies'} to the canonical "
            f"full-text hash."
        )

    if select_backend() == "skip":
        print(
            "No summary backend available (deferred or unconfigured) — "
            "nodes stay owed for a run when one is; nothing written.",
            file=sys.stderr,
        )
        return BULK_EXIT_NO_BACKEND

    total = len(files)
    skipped = generated = errors = dry_count = archived_count = 0
    nonanswers = refused = 0
    started = time.time()
    for i, file_path in enumerate(files, 1):
        node_start = time.time()
        rel = _sidecar_key(file_path)
        status = _process_node_bulk(
            file_path, force=args.force, dry_run=args.dry_run, formats=formats
        )
        elapsed = time.time() - node_start
        if status == "skipped":
            skipped += 1
            print(f"[{i}/{total}] SKIP  {rel} (already has formats)")
        elif status in ("archived", "archived_dropped"):
            archived_count += 1
            # The marker substring is load-bearing (ARCHIVED_SKIP_MARKER —
            # the launcher's ARCHIVED_MARKER matches it).
            print(f"[{i}/{total}] SKIP  {rel} (archived, skipping)")
        elif status == "dry_run":
            dry_count += 1
            print(f"[{i}/{total}] DRY   {rel}")
        elif status == "generated":
            generated += 1
            print(f"[{i}/{total}] OK    {rel}  ({elapsed:.1f}s)")
        elif status.startswith("refused:"):
            # One plain line naming the node (owner ruling 2026-10-18).
            refused += 1
            print(
                f"[{i}/{total}] REFUSED {rel}: "
                f"{status.removeprefix('refused:')} "
                f"(flagged; the next sync retries it)"
            )
        elif status.startswith("nonanswer:"):
            nonanswers += 1
            print(
                f"[{i}/{total}] NON-ANSWER {rel}: "
                f"{status.removeprefix('nonanswer:')} (not cached; the next "
                f"sync retries it)"
            )
        else:
            errors += 1
            print(f"[{i}/{total}] ERROR {rel}: {status.removeprefix('error:')}")
        # Incremental persistence (H-3): every node that CHANGED the sidecar
        # is on disk before the next one starts. Atomic (shared home), so a
        # kill mid-run leaves a valid sidecar holding everything finished so
        # far. Dry-run writes nothing; unchanged runs rewrite nothing.
        if not args.dry_run and (
            status in ("generated", "archived_dropped")
            or status.startswith("refused:")
        ):
            save_formats(formats)

    print(
        f"\nDone in {time.time() - started:.1f}s — "
        f"generated: {generated}, skipped: {skipped}, "
        f"refused: {refused}, "
        f"non-answer: {nonanswers}, "
        f"dry-run: {dry_count}, errors: {errors}, archived: {archived_count}"
    )
    if not args.dry_run and (generated or archived_count or refused):
        print(f"Sidecar saved incrementally to {FORMATS_PATH}")
    return 1 if errors else 0


def main():
    global _FORCE_API, CLAUDE_PROJECT, KNOWLEDGE_DIR, FORMATS_PATH, LOG_PATH, ORCHESTRATOR_ROOT
    parser = build_parser()
    args = parser.parse_args()

    if not args.file and not args.all:
        parser.print_help()
        sys.exit(1)

    if args.force_api:
        # Module-level flag picked up by select_backend(). The CLI flag
        # ONLY affects this single run; it doesn't write to app_state.
        _FORCE_API = True

    # Apply the --knowledge-dir override before any path operations (same
    # semantics the retired bulk generator had): the sidecar lands inside
    # the overridden knowledge/ and the project root (the rel_path base and
    # the chunk-fetch resolver's project) becomes its parent.
    if args.knowledge_dir is not None:
        override = Path(args.knowledge_dir).resolve()
        if not override.exists():
            print(f"--knowledge-dir path does not exist: {override}", file=sys.stderr)
            sys.exit(1)
        KNOWLEDGE_DIR = override
        FORMATS_PATH = KNOWLEDGE_DIR / ".node_formats.json"
        CLAUDE_PROJECT = KNOWLEDGE_DIR.parent
        LOG_PATH = CLAUDE_PROJECT / ".claude" / "logs" / "kg-summary-generator.log"
        ORCHESTRATOR_ROOT = _resolve_orchestrator_root()

    if args.all:
        sys.exit(_run_bulk(args))

    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    rel_path = _sidecar_key(file_path)

    title, full_text, body = read_node(file_path)
    if not title:
        # Pre-write hook should have blocked this, but guard anyway.
        log(f"  No title in {rel_path} (pre-write hook should have caught this), skipping")
        sys.exit(0)

    c_hash = content_hash(full_text)
    formats = load_formats()
    # One-time separator re-key (v0.2.103 B1): a sidecar written on Windows
    # before the fix holds `\` keys the lookup below (and every retrieval
    # reader) would miss. Persisted at once so the hash-skip path also
    # leaves a canonical sidecar behind — except under --dry-run, which
    # writes nothing in either mode (R20 L6: this save used to ignore it).
    if rekey_separator_entries(formats) and not args.dry_run:
        save_formats(formats)

    # Archived nodes skip the summarization entirely (owner decision
    # 2026-10-14 — the ONE shared predicate the Weaviate sync skips and
    # deletes rows by), and any existing sidecar entry for them is DROPPED:
    # the sidecar must not keep serving summaries for a node the graph no
    # longer holds. The line carries ARCHIVED_SKIP_MARKER so the launcher
    # counts this node as skipped, never failed or succeeded.
    archived = _archived_reason(file_path, full_text)
    if archived:
        print(f"  {title}: {ARCHIVED_SKIP_MARKER} ({archived})")
        if _drop_sidecar_entry(rel_path, formats):
            if args.dry_run:
                print(f"  {title}: dry-run — would drop the stale sidecar entry")
            else:
                save_formats(formats)
                log(f"  {title}: dropped the stale sidecar entry for the archived node")
        sys.exit(0)

    existing = formats.get(rel_path, {})
    if not args.force and existing.get("content_hash") == c_hash:
        if stored_entry_is_usable(existing):
            print(f"  {title}: unchanged (hash match), skipping")
            sys.exit(0)
        # Wording deliberately avoids the phrase the launcher and
        # tests/test_v0270_shipped_kg_summaries.py match on ("hash match"):
        # this is the REGENERATE path, and a reader (or a grep) must not
        # mistake it for the skip path. A REFUSED flag (owner ruling
        # 2026-10-18) is never usable, so it lands here: the next ordinary
        # sync retries the node.
        if existing.get("refused"):
            log(f"  {title}: flagged refused on an earlier run — retrying now")
        else:
            log(f"  {title}: cached entry holds a non-answer, not a summary — "
                f"regenerating it now")

    if args.dry_run:
        # Before backend selection: a dry run neither probes nor calls a model.
        print(f"  {title}: dry-run — would generate summaries (nothing written)")
        sys.exit(0)

    if select_backend() == "skip":
        sys.exit(0)

    log(f"  Generating summaries for: {title}")

    try:
        description = generate_description(title, body)
        summary = generate_summary(title, body)
    except _sb.ContentRefusal as exc:
        # Owner ruling 2026-10-18: the model REFUSED the content. Do not
        # descend tiers, do not cache anything as a summary — flag the node
        # refused in the sidecar (a record, not a usable entry, so the next
        # ordinary sync retries it), log one plain line naming the node, and
        # exit 0: a recorded refusal is not a run failure.
        log(f"  {title}: refused by the model ({exc}) — flagged; "
            f"the next sync retries it")
        _record_refusal(formats, rel_path, title, c_hash, exc)
        save_formats(formats)
        sys.exit(0)
    except _sb.NonAnswerResponse as exc:
        # Writing this would poison the sidecar and the hash gate would
        # then freeze it. Fail loudly instead: the launcher records the
        # node as failed and the user can see why.
        log(f"  {title}: {exc}")
        sys.exit(1)
    except RuntimeError as exc:
        log(f"  {title}: summary generation failed — {exc}")
        sys.exit(1)
    print(f"  Description: {description[:80]}...")
    print(f"  Summary: {summary[:80]}...")

    entry: dict = {          # mixed value types (str + int + dict)
        "title": title,
        "description": description,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": c_hash,
        "backend": _BACKEND_CACHE.get("choice", "?"),
    }

    chunks = get_chunks_from_weaviate(title, rel_path)
    total_chunks = len(chunks) if chunks else 1

    if total_chunks > 1:
        print(f"  Multi-chunk node ({total_chunks} chunks), generating per-chunk summaries...")
        chunk_summaries = {}
        for cn, chunk_content in chunks:
            try:
                cs = generate_chunk_summary(title, cn, total_chunks, chunk_content)
            except RuntimeError as exc:
                # Includes NonAnswerResponse. Omitting the chunk keeps the
                # (valid) whole-node description + summary; caching a
                # non-answer here would make the entry unusable AND
                # hash-frozen. The omission is regenerated next run.
                log(f"    Chunk {cn}: not stored — {exc}")
                continue
            chunk_summaries[str(cn)] = cs
            print(f"    Chunk {cn}: {cs[:60]}...")
        if chunk_summaries:
            entry["chunk_summaries"] = chunk_summaries
        entry["total_chunks"] = total_chunks

    formats[rel_path] = entry
    save_formats(formats)
    log(f"  Saved {rel_path} via {entry['backend']}")


if __name__ == "__main__":
    main()
