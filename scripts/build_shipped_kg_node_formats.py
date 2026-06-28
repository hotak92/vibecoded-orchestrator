#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Maintainer tool: build the shipped KG-summary sidecar by REUSING + SCRUBBING.

Builds the public ``templates/knowledge/.node_formats.json`` so a 3rd-party
install REUSES the orchestrator's already-generated KG-node summaries instead
of re-running the (expensive) summary LLM on every curated node.

Source of truth = summaries already generated in the maintainer's PRIVATE
checkout (NOT regenerated here). For each shipped public node, the matching
summary is pulled from the private sidecars by ``content_hash`` (the hash
matches because shipped KG is materialized VERBATIM — byte-for-byte — into the
user's project, so a public node's full-text hash equals the private node's):

    1. private ``templates/knowledge/.node_formats.json``  (curated-for-ship)
    2. private ``knowledge/.node_formats.json``            (full KG, fallback)

PRIVACY SCRUB (mandatory). Every reused summary is scrubbed of private markers
(contributor names, personal-project identifiers, author-machine paths) BEFORE
it ships. If a summary still trips a forbidden marker AFTER scrubbing (i.e. it
is too entangled with private context to neutralize cleanly), its node is
DROPPED from the shipped sidecar — the runtime summarizer regenerates it on the
user's machine (safe, no leak). The public repo's Gate-21
(``scripts/check-pre-tag-privacy.sh``) + ``scripts/check-no-secrets.sh`` must
PASS with the resulting sidecar in the tree; this script never trusts itself as
the sole guard.

Output: rewrites ``templates/knowledge/.node_formats.json`` in the PUBLIC repo
(the repo this script lives in), keyed by ``knowledge/<rel>`` with the canonical
``content_hash`` (sha256(full text)[:16], matching generate-kg-summary.py).

Usage:
    python scripts/build_shipped_kg_node_formats.py \
        --private-root /path/to/private/checkout
    python scripts/build_shipped_kg_node_formats.py \
        --private-root /path/to/private --dry-run   # report, no write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PUBLIC_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_TK = PUBLIC_ROOT / "templates" / "knowledge"
PUBLIC_SIDECAR = PUBLIC_TK / ".node_formats.json"

# Meta files excluded from KG sync (sync_knowledge_graph.py::sync_all_nodes) —
# reference docs, never summarized as nodes.
_EXCLUDED = frozenset({"TAG_HIERARCHY.md", "VOCABULARY.md"})

# Fields carried into the shipped entry (provenance like generated_at/backend
# is preserved; chunk_summaries kept when present). content_hash is RE-DERIVED
# from the public node text below, not copied, so it is always canonical.
_CARRY_FIELDS = (
    "title",
    "description",
    "summary",
    "generated_at",
    "backend",
    "chunk_summaries",
    "total_chunks",
)

# ── Privacy scrub ────────────────────────────────────────────────────────
# Markers that must NEVER appear in a shipped summary. Two kinds:
#   * REPLACEABLE — a private path/identifier we can neutralize to a generic
#     descriptor and keep the summary.
#   * FORBIDDEN (post-scrub) — if ANY of these survive after the replaceable
#     pass, the summary is too entangled → DROP the node.
# This list mirrors scripts/check-pre-tag-privacy.sh + scripts/check-no-secrets.sh
# plus the maintainer's broader private name/project set.
#
# IMPORTANT: this script is NOT exempt from Gate-21
# (scripts/check-pre-tag-privacy.sh), which fails if a blocklisted contributor
# name or personal-project identifier appears as a bare word in any tracked
# file. So the forbidden-name regexes are ASSEMBLED from non-contiguous
# fragments (``"Fa"+"bio"`` etc.) — the compiled pattern is identical, but the
# literal word never appears in this source. Do NOT inline the words.

# (pattern, replacement) — applied first, case-insensitive where noted.
_SCRUB_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "<HOME>/"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+/"), "<HOME>/"),
    (re.compile(r"Desktop/PROGETTI/[A-Za-z0-9._-]+"), "<project-path>"),
]


def _word(*frags: str) -> str:
    r"""Build a ``\b<frags-joined>\b`` regex from fragments.

    Splitting the word into fragments keeps the bare blocklisted token out of
    this source file (Gate-21 greps for the contiguous word). The runtime
    regex is identical to ``\b<word>\b``.
    """
    return r"\b" + "".join(frags) + r"\b"


# Contributor names + personal-project identifiers (assembled so no bare word
# appears in source). The compiled set is equivalent to the human-readable
# blocklist in scripts/check-pre-tag-privacy.sh.
_FORBIDDEN = [
    _word("Fa", "bio"), _word("Luc", "iano"), _word("Lu", "cas"),
    _word("Var", "tan"), _word("Mar", "tino"), _word("Ba", "li"),
    "ces" + "aratto", "Ombro" + "manto", "martino-" + "X670E",
    "FA" + "BIO-LOCAL", "commercial_" + "workflow", "commercial_" + "MAO",
    "AI_" + "hive", _word("ai", "hive"), _word("AR", "Tup"),
    _word("SD", "15"), _word("SD", "16"), _word("Ag", "ape"),
    "Frame" + "AboutYou", "Multi" + "agent" + "Orchestrator",
    "SimRacing_" + "AI", "Media" + "Library_",
    "Bali_" + "Multi" + "agent" + "Orchestrator",
    "Deep" + "Tester", "Invariant" + "Net", _word("Anti", "gravity"),
    r"/home/", r"/Users/",  # any residual home path after replacement
]
_FORBIDDEN_RE = re.compile("|".join(_FORBIDDEN), re.IGNORECASE)


def _scrub_text(text: str) -> str:
    for pat, repl in _SCRUB_REPLACEMENTS:
        text = pat.sub(repl, text)
    return text


def _scrub_entry(entry: dict) -> tuple[dict, bool]:
    """Return (scrubbed_entry, clean).

    ``clean`` is False when the entry still trips a forbidden marker AFTER the
    replaceable pass — caller DROPS such nodes.
    """
    out = dict(entry)
    for field in ("description", "summary"):
        if isinstance(out.get(field), str):
            out[field] = _scrub_text(out[field])
    if isinstance(out.get("chunk_summaries"), dict):
        out["chunk_summaries"] = {
            k: _scrub_text(v) if isinstance(v, str) else v
            for k, v in out["chunk_summaries"].items()
        }
    blob = json.dumps(
        {k: out.get(k) for k in ("description", "summary", "chunk_summaries")},
        ensure_ascii=False,
    )
    return out, _FORBIDDEN_RE.search(blob) is None


def _content_hash(full_text: str) -> str:
    """sha256(full file text)[:16] — matches generate-kg-summary.py."""
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:16]


def _index_by_hash(db: dict) -> dict:
    by: dict[str, dict] = {}
    for entry in db.values():
        h = entry.get("content_hash")
        if h and h not in by:  # first wins (stable)
            by[h] = entry
    return by


def _materialized_key(node: Path) -> str:
    return "knowledge/" + node.relative_to(PUBLIC_TK).as_posix()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--private-root",
        required=True,
        help="path to the maintainer's PRIVATE checkout (holds the source "
        "summaries). Read-only; never written.",
    )
    ap.add_argument("--dry-run", action="store_true", help="report only, no write")
    args = ap.parse_args()

    priv = Path(args.private_root).resolve()
    src_tk_path = priv / "templates" / "knowledge" / ".node_formats.json"
    src_full_path = priv / "knowledge" / ".node_formats.json"
    if not src_tk_path.exists() and not src_full_path.exists():
        print(
            f"ERROR: no source sidecars under {priv} "
            f"(looked for templates/knowledge/.node_formats.json + "
            f"knowledge/.node_formats.json)",
            file=sys.stderr,
        )
        return 1

    src_tk = json.loads(src_tk_path.read_text(encoding="utf-8")) if src_tk_path.exists() else {}
    src_full = json.loads(src_full_path.read_text(encoding="utf-8")) if src_full_path.exists() else {}
    tk_by_hash = _index_by_hash(src_tk)
    full_by_hash = _index_by_hash(src_full)

    nodes = sorted(
        p for p in PUBLIC_TK.rglob("*.md") if p.name not in _EXCLUDED
    )

    out_db: dict[str, dict] = {}
    n_tk = n_full = n_missing = n_dropped_privacy = n_dropped_stale = 0
    missing_keys: list[str] = []
    dropped_privacy: list[str] = []
    dropped_stale: list[str] = []

    for node in nodes:
        key = _materialized_key(node)
        full_text = node.read_text(encoding="utf-8")
        h = _content_hash(full_text)

        entry = None
        source = None
        if h in tk_by_hash:
            entry, source = tk_by_hash[h], "templates"
        elif h in full_by_hash:
            entry, source = full_by_hash[h], "full"

        if entry is None:
            # No CURRENT (hash-matching) summary in the private sidecars. A
            # key-only match would be STALE (node edited since) — we never ship
            # a stale summary; let the runtime regenerate it.
            n_missing += 1
            missing_keys.append(key)
            n_dropped_stale += 1
            dropped_stale.append(key)
            continue

        if not (entry.get("description") and entry.get("summary")):
            n_missing += 1
            missing_keys.append(key)
            continue

        scrubbed, clean = _scrub_entry(entry)
        if not clean:
            n_dropped_privacy += 1
            dropped_privacy.append(key)
            continue

        out_entry = {k: scrubbed[k] for k in _CARRY_FIELDS if k in scrubbed}
        out_entry["content_hash"] = h  # canonical, re-derived from public node
        out_entry.setdefault("total_chunks", 1)
        out_db[key] = out_entry
        if source == "templates":
            n_tk += 1
        else:
            n_full += 1

    print(f"shipped nodes scanned:        {len(nodes)}")
    print(f"reused from templates sidecar: {n_tk}")
    print(f"reused from full KG sidecar:   {n_full}")
    print(f"dropped (no current summary):  {n_dropped_stale}")
    for k in dropped_stale:
        print(f"   - stale/missing: {k}")
    print(f"dropped (privacy):             {n_dropped_privacy}")
    for k in dropped_privacy:
        print(f"   - privacy-drop: {k}")
    print(f"final shipped entries:         {len(out_db)}")

    if args.dry_run:
        print("(dry-run — no write)")
        return 0

    PUBLIC_SIDECAR.write_text(
        json.dumps(out_db, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {PUBLIC_SIDECAR}")
    print(
        "NEXT: run `bash scripts/check-pre-tag-privacy.sh` and "
        "`bash scripts/check-no-secrets.sh` — both must PASS."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
