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

# ──────────────────────────────────────────────────────────────────────
# Response validity — THE SAME predicate the runtime generator uses
# ──────────────────────────────────────────────────────────────────────
# v0.2.92 WP-Q2. This script and `templates/scripts/generate-kg-summary.py`
# write the SAME file (`knowledge/.node_formats.json`). WP-Q taught the
# runtime generator to reject a model NON-ANSWER and to treat a stored one
# as not-satisfied, so poisoned rows heal on the next ordinary run. This
# script reaches the same file on the orchestrator-root layout via
# `sync_knowledge_graph._regen_node_formats_after_full_sync()` — so without
# the same gate, one path heals a row and the other re-poisons it.
#
# ONE implementation, not a mirror (project rule A > B > C): the predicate
# is imported from the shared ladder rather than copied. That is reachable
# HERE — unlike in an installed project's `.claude/scripts/` — because this
# script only ever runs from an orchestrator CLONE: its own PROJECT_ROOT is
# `<clone>/`, `_regen_node_formats_after_full_sync` requires
# `<clone>/claude_mcp_servers/scripts/generate_node_formats.py` to exist
# before it will spawn this at all (falling back to the runtime generator
# otherwise), and `docs/CONFIGURATION.md` documents the manual `--all`
# backfill from the clone. `templates/scripts/` is therefore always a
# sibling.
#
# The import is HARD on purpose. A soft `except ImportError: is_non_answer
# = lambda _t: False` would restore the exact re-poisoning bug this closes,
# silently — the failure mode VCO's no-silent-fallback rule exists for.
_LADDER_CANDIDATES = (
    PROJECT_ROOT / "templates" / "scripts",   # orchestrator clone (canonical)
    PROJECT_ROOT / ".claude" / "scripts",     # materialized bundle beside it
)
for _cand in _LADDER_CANDIDATES:
    if (_cand / "summary_backends.py").is_file():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
else:  # no break — nothing to import from; say so, don't degrade quietly
    raise ImportError(
        "generate_node_formats.py cannot find the shared summary ladder "
        "(summary_backends.py). Looked in: "
        + ", ".join(str(p) for p in _LADDER_CANDIDATES)
        + ". This script runs from an orchestrator clone, where "
        "templates/scripts/ is a sibling — a missing ladder means a broken "
        "checkout, not a supported layout."
    )
# pyright cannot follow the sys.path insertion above (the ladder lives in a
# sibling directory, resolved at runtime); the guaranteed-sibling argument is
# in the block above, and the ImportError branch is the runtime check.
import summary_backends as _sb  # noqa: E402  # pyright: ignore[reportMissingImports]

#: Re-exported so callers/tests can reach the predicate through THIS
#: module's namespace, and so the identity (not a copy) is assertable.
is_non_answer = _sb.is_non_answer


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


def get_chunks_from_weaviate(title: str, file_path: str = "") -> list[tuple[int, str]]:
    """Fetch this node's stored chunks from Weaviate, sorted by chunk number.

    Returns a sorted list of ``(chunk_number, content)``. Empty list if the
    node is single-chunk or Weaviate is unreachable.

    Two v0.2.92 W7 corrections, both of which made this return ``[]``
    or the WRONG rows:

    * the stored property is ``chunk_num`` (that is the schema name every
      writer uses); this read ``chunk_number``, which is only the name the
      MCP's *result formatter* gives it, so the ``cn is not None`` guard
      below rejected EVERY row and the per-chunk summaries were never
      generated for any node. ``chunk_number`` is still accepted as a
      fallback in case a caller passes formatter-shaped objects.
    * the filter keyed on ``title`` alone. A title is not a node identity —
      measured live, two titles per collection map to two file_paths each —
      so a colliding node's chunks were blended into this node's summaries.
      An empty ``file_path`` degrades to the pre-fix title-only filter.
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
            chunk_filter = Filter.by_property("title").equal(title)
            if file_path:
                chunk_filter = chunk_filter & Filter.by_property(
                    "file_path"
                ).equal(file_path)
            resp = coll.query.fetch_objects(filters=chunk_filter, limit=20)
            chunks: list[tuple[int, str]] = []
            for obj in resp.objects:
                props = obj.properties or {}
                cn = props.get("chunk_num", props.get("chunk_number"))
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
    """Canonical short stable hash for dedup against re-runs.

    MUST be called with the FULL file text (frontmatter + body, unstripped) so
    the stored `content_hash` equals the CANONICAL scheme used by BOTH
    `generate-kg-summary.py` (runtime) and
    `scripts/build_shipped_kg_node_formats.py` (the shipped-sidecar builder):
    `sha256(full_file_text)[:16]`. The builder looks up private sidecar entries
    by this exact key, so a mismatch makes freshly generated summaries INVISIBLE
    to the ship path (they get reported stale/missing and the user regenerates
    locally — a quiet degradation). See the 2026-07-20 sidecar-hash-mismatch KG
    node; before that fix this generator hashed `body.strip()` (frontmatter
    stripped) instead of the full text, diverging from canonical.

    STORAGE-LAYER hash: answers "is this sidecar already current so I can skip
    re-generating it" — deliberately sha256[:16], distinct from the RETRIEVAL
    content-identity hash (rl_client/content_dedup.content_sha, sha1[:12], which
    mirrors the seen-store). The two layers answer different questions ("skip a
    re-write" vs "drop a duplicate reaching Claude"); they are intentionally NOT
    converged. See the v0.2.70 dedup triage.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _legacy_content_hash(body: str) -> str:
    """The PRE-2026-07-20 storage hash scheme: sha256(body.strip())[:16].

    Kept for exactly one purpose — detecting sidecar entries keyed under the old
    (frontmatter-stripped body) scheme so `rekey_legacy_entries` can silently
    migrate them to the canonical full-text key WITHOUT regenerating the summary.
    Do NOT use this for storing new entries; `content_hash(full_file_text)` is
    canonical.
    """
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def rekey_legacy_entries(db: dict) -> int:
    """Silently re-key sidecar entries from the legacy hash scheme to canonical.

    For each entry whose stored `content_hash` does NOT match the canonical
    full-text hash of its node file, but DOES match the legacy `body.strip()`
    hash of that same file, overwrite the stored hash with the canonical value —
    WITHOUT regenerating the (still-valid) summary. This preserves skip-unchanged
    correctness across the transition: a node that was summarized under the old
    scheme and is byte-unchanged will now be recognized as current, not
    regenerated.

    Entries that match neither scheme (genuinely stale, or already canonical) are
    left untouched — the normal skip/regenerate logic handles them.

    F-13 (intentional): a node whose FRONTMATTER-only changed since its last
    summarize still matches the legacy body-hash (the legacy scheme hashed the
    body alone), so it is re-keyed to canonical-of-current-file WITHOUT
    regenerating. This is deliberate, not a missed-regeneration bug: the summary
    describes the BODY, which is unchanged, and the old body-only scheme would
    never have regenerated on a frontmatter-only edit either.

    Returns the number of entries re-keyed (for logging).
    """
    rekeyed = 0
    for rel_path, entry in db.items():
        stored = entry.get("content_hash")
        if not stored:
            continue
        # Resolve the node file for this entry. Keys are stored relative to the
        # project root (e.g. "knowledge/concepts/foo.md"); the KNOWLEDGE_DIR
        # override maps <root>/knowledge → KNOWLEDGE_DIR, so strip a leading
        # "knowledge/" and resolve under KNOWLEDGE_DIR.
        #
        # F-4: normalize `\`→`/` first — on Windows the DB keys come from
        # `str(Path.relative_to(...))` and use `\`, which would never match the
        # `knowledge/` prefix (the same v0.2.81/v0.2.85 manifest-separator
        # lesson). Without this, Windows + --knowledge-dir override falls to the
        # PROJECT_ROOT branch, the node file isn't found, the entry is skipped,
        # and legacy entries silently REGENERATE (LLM cost) instead of re-keying.
        rel = rel_path.replace("\\", "/")
        node_prefix = KNOWLEDGE_DIR.name + "/"
        if rel.startswith(node_prefix):
            node_file = KNOWLEDGE_DIR / rel[len(node_prefix):]
        else:
            node_file = PROJECT_ROOT / rel
        if not node_file.is_file():
            continue
        try:
            full_text = node_file.read_text(encoding="utf-8")
        except OSError:
            continue
        canonical = content_hash(full_text)
        if stored == canonical:
            continue  # already canonical
        parsed = parse_frontmatter(node_file)
        body = parsed[1] if parsed else full_text
        if stored == _legacy_content_hash(body):
            entry["content_hash"] = canonical
            rekeyed += 1
    return rekeyed


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
    """Save the sidecar JSON formats database ATOMICALLY.

    F-7: `.node_formats.json` holds EVERY node's LLM summary; a plain
    truncate-write here means a kill mid-save corrupts the whole DB
    (regenerable, but at full LLM-regeneration cost). Write to a tempfile in
    the SAME directory (so os.replace stays on one filesystem), fsync, then
    os.replace into place — either the old DB survives intact or the fully
    written new one does, never a truncated file. Self-contained (no vco_lib
    import) because this script runs from the MCP venv, which is not
    guaranteed to have vco_lib on its path; the primitive is trivial. This
    body is kept BYTE-IDENTICAL between the public and private copies of the
    script.
    """
    import json
    # v0.2.92 (duplication-merge): this was a verbatim copy of
    # `vco_lib.atomic.atomic_write_text`. This script only ever runs from an
    # orchestrator CLONE (PROJECT_ROOT is asserted above), so `vco_lib` is a
    # sibling package and the import is HARD — a broken checkout raises.
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from vco_lib.atomic import atomic_write_text  # noqa: PLC0415

    payload = json.dumps(db, indent=2, ensure_ascii=False)
    atomic_write_text(FORMATS_FILE, payload)


def has_formats(rel_path: str, db: dict, c_hash: str | None = None) -> bool:
    """Return True if the entry is USABLE and (optionally) content-hash matches.

    Usable = description and summary are both present AND are actually
    summaries. v0.2.92 WP-Q2: the old test was truthiness, so a stored
    ``"Ready. What do you need summarized?"`` satisfied it — and because
    the content hash of the unchanged source file also matched, the row was
    frozen forever. WP-Q root-caused those strings to a Windows argv defect
    and taught the runtime generator to reject them; this is the SAME gate
    on the second writer of the same file, so a row healed by one path is
    not reported as satisfied (and thus left poisoned) by the other.

    Chunk summaries are ENRICHMENT, and the asymmetry is deliberate: a
    STORED non-answer chunk invalidates the entry, a MISSING one does not.
    Treating absent-as-poisoned would re-run the two whole-node LLM calls on
    every pass for any node whose Weaviate chunk fetch keeps failing —
    churn bought for a retrieval-tier nicety. Mirrors
    ``generate-kg-summary.stored_entry_is_usable``.

    If `c_hash` is provided, the stored content_hash must also match (so
    edits trigger regen).
    """
    entry = db.get(rel_path, {})
    if not isinstance(entry, dict):
        return False
    if is_non_answer(entry.get("description")):
        return False
    if is_non_answer(entry.get("summary")):
        return False
    chunk_summaries = entry.get("chunk_summaries")
    if isinstance(chunk_summaries, dict) and chunk_summaries:
        if any(is_non_answer(v) for v in chunk_summaries.values()):
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

    # Summarize the body (frontmatter is metadata, not prose to summarize) but
    # HASH the full file text — canonical scheme, shared with the runtime
    # generate-kg-summary.py and the shipped-sidecar builder. Hashing the
    # stripped body here would key entries under a scheme the builder can't
    # look up (2026-07-20 sidecar-hash-mismatch fix).
    full_content = body.strip()
    c_hash = content_hash(file_path.read_text(encoding="utf-8"))

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

    # v0.2.92 WP-Q2 — the WRITE half of the same gate. `call_llm` here is
    # this script's own Ollama/Haiku router, not the shared ladder, so it
    # does not raise on a non-answer the way `summary_backends.call_llm`
    # does. Without this check a fresh refusal ("I cannot summarize...", an
    # empty generation) would be stored, and the content-hash gate would
    # then freeze it — re-poisoning a row the runtime generator may have
    # just healed. Reported as an error (never cached): the run's tally
    # shows it, the next run retries, and no bad row is written.
    for field, value in (("description", description), ("summary", summary)):
        if is_non_answer(value):
            return (
                f"error:{field} came back a non-answer "
                f"({(value or '').strip()[:80]!r}) — not cached"
            )

    # Multi-chunk handling: fetch chunks from Weaviate; if N>1, generate
    # per-chunk summaries so auto-tier retrieval can surface them.
    chunk_summaries: dict | None = None
    total_chunks: int | None = None
    chunks = get_chunks_from_weaviate(title, rel_path)
    if len(chunks) > 1:
        total_chunks = len(chunks)
        chunk_summaries = {}
        for cn, chunk_content in chunks:
            try:
                cs = generate_chunk_summary(title, cn, total_chunks, chunk_content)
            except RuntimeError as e:
                # Don't fail the whole node if one chunk summary fails — just skip it.
                print(f"  WARN: chunk {cn} summary failed for {title!r}: {e}", file=sys.stderr)
                continue
            if is_non_answer(cs):
                # DROP, don't store: absent is recoverable, poisoned is not
                # (has_formats treats a stored non-answer chunk as invalid,
                # a missing one as fine). Same asymmetry, both halves.
                print(f"  WARN: chunk {cn} summary for {title!r} was a "
                      f"non-answer — dropped, not cached", file=sys.stderr)
                continue
            chunk_summaries[str(cn)] = cs

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
        print("Using provider: Haiku (API)")
    else:
        available = get_available_models()
        model = pick_model(MODEL_CANDIDATES, available)
        _ollama_model = model
        print(f"Using provider: Ollama ({model})")

    # Load sidecar formats database
    db = load_formats_db()

    # Silently migrate any entries still keyed under the legacy body.strip()
    # hash scheme to the canonical full-text key (no summary regeneration). This
    # preserves skip-unchanged correctness across the 2026-07-20 hash-scheme
    # transition — a byte-unchanged node summarized under the old scheme is now
    # recognized as current instead of being needlessly regenerated.
    n_rekeyed = rekey_legacy_entries(db)
    if n_rekeyed:
        print(f"Re-keyed {n_rekeyed} legacy sidecar entr"
              f"{'y' if n_rekeyed == 1 else 'ies'} to canonical full-text hash.")

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

    # Save the formats database when anything changed: new summaries generated,
    # OR legacy entries re-keyed to canonical (the re-key must persist even if no
    # node needed regeneration, else the migration is lost on the next run).
    if (generated > 0 or n_rekeyed > 0) and not args.dry_run:
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
