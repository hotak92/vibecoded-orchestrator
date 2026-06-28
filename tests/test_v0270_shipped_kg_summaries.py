# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Part 1: pre-shipped KG summaries — lifecycle + no-subs-token guards.

The orchestrator ships LLM-generated summaries for its curated KG nodes in
``templates/knowledge/.node_formats.json`` so a 3rd-party install REUSES them
instead of re-running the (expensive) summary LLM on every node. For that reuse
to actually fire, two invariants must hold — and this module locks them in:

1. NO-SUBS-TOKEN guard (the key correctness invariant).
   ``templates/knowledge/`` is materialized into ``<project>/knowledge/``
   VERBATIM — byte-for-byte, ``transform=None`` (project_init.py). So unlike
   agents/skills, knowledge nodes do NOT pass through ``_apply_subs``. A
   shipped node containing an install-time substitution token
   (``{{ORCHESTRATOR_ROOT}}`` etc.) would (a) leak the un-concretized literal
   into the user's project, AND (b) break the shipped-summary hash match (the
   token text would differ between intent and materialized form only if it
   WERE substituted — since it isn't, the real risk is the leak + the reader
   confusion). We forbid the 5 real subs tokens in shipped knowledge .md.
   The tokens are READ FROM ``_agent_subs`` so the test can't drift from the
   source set. Doc-literal tokens that are NOT in the subs set (``{{ text }}``,
   ``${{ github.actor }}``, ``{{project_id}}``) are allowed.

2. SHIPPED-SUMMARY hash match (the reuse invariant).
   Every entry in the shipped ``.node_formats.json`` must (a) be keyed by the
   ``knowledge/<rel>`` path an install produces, (b) carry a ``content_hash``
   that equals the runtime summarizer's hash over the CURRENT node text, and
   (c) have non-empty description + summary. Because the install materializes
   the node verbatim, that shipped hash equals the install-time hash, so
   ``generate-kg-summary.py``'s "unchanged (hash match), skipping" branch fires
   → no LLM call. A stale entry (hash != current node) would be silently
   regenerated, defeating the pre-ship; this test catches that at CI time.

Pure repo-introspection tests — no network / Ollama / Weaviate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_KNOWLEDGE = REPO_ROOT / "templates" / "knowledge"
SIDECAR = TEMPLATES_KNOWLEDGE / ".node_formats.json"
RUNTIME_SUMMARIZER = REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"

# Meta files excluded from KG sync (sync_knowledge_graph.py::sync_all_nodes).
# They are reference docs, never embedded/summarized as nodes, so the shipped
# sidecar legitimately omits them.
_EXCLUDED_BASENAMES = frozenset({"TAG_HIERARCHY.md", "VOCABULARY.md"})


def _subs_tokens() -> set[str]:
    """The 5 real install-time substitution tokens, read from the source."""
    from vco_lib.project_init import _agent_subs

    return set(_agent_subs(Path("/orch"), Path("/proj")).keys())


def _runtime_content_hash(full_text: str) -> str:
    """Mirror templates/scripts/generate-kg-summary.py::content_hash.

    sha256(FULL file text)[:16]. We re-implement the one-liner here rather than
    importing the script (importing it pulls in urllib/requests setup that is
    irrelevant to a pure hash check), but assert below that it MATCHES the
    script's own function so the two can't drift.
    """
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:16]


def _shipped_nodes() -> list[Path]:
    return sorted(
        p
        for p in TEMPLATES_KNOWLEDGE.rglob("*.md")
        if p.name not in _EXCLUDED_BASENAMES
    )


def _materialized_key(node: Path) -> str:
    return "knowledge/" + node.relative_to(TEMPLATES_KNOWLEDGE).as_posix()


class NoSubsTokenInShippedKnowledgeTest(unittest.TestCase):
    """Part 1.3: shipped knowledge .md must contain NO install-time subs token."""

    def test_no_subs_token_in_any_shipped_node(self):
        tokens = _subs_tokens()
        self.assertEqual(
            len(tokens), 5,
            f"expected exactly 5 subs tokens, _agent_subs gave {sorted(tokens)}",
        )

        offenders: list[str] = []
        for node in _shipped_nodes():
            text = node.read_text(encoding="utf-8")
            for tok in tokens:
                if tok in text:
                    offenders.append(f"{_materialized_key(node)} contains {tok}")
        self.assertEqual(
            offenders, [],
            "shipped knowledge is materialized VERBATIM (no _apply_subs), so a "
            "substitution token would leak un-concretized and break the shipped "
            "summary hash match. Offenders:\n  " + "\n  ".join(offenders),
        )

    def test_doc_literal_braces_are_allowed(self):
        """Sanity: tokens NOT in the subs set must NOT be flagged.

        Guards against a future over-broad implementation that bans ALL
        ``{{...}}`` and breaks legitimate doc-literal examples.
        """
        tokens = _subs_tokens()
        for benign in ("{{ text }}", "${{ github.actor }}", "{{project_id}}"):
            self.assertNotIn(
                benign, tokens,
                f"{benign} must NOT be in the subs set (it's a doc literal)",
            )


class ShippedSummaryHashConventionTest(unittest.TestCase):
    """The hash convention we ship under MUST equal the runtime summarizer's."""

    def test_local_hash_matches_runtime_summarizer(self):
        spec = importlib.util.spec_from_file_location(
            "_rt_summarizer_hashcheck", RUNTIME_SUMMARIZER
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_rt_summarizer_hashcheck"] = mod
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"summarizer deps missing ({exc})")
        sample = "---\ntitle: X\n---\nhello world\n"
        self.assertEqual(
            _runtime_content_hash(sample), mod.content_hash(sample),
            "the test's hash convention drifted from generate-kg-summary.py",
        )


class ShippedSidecarLifecycleTest(unittest.TestCase):
    """Part 1.2: the shipped sidecar must be CURRENT so reuse actually fires."""

    @classmethod
    def setUpClass(cls):
        if not SIDECAR.exists():
            raise unittest.SkipTest(
                "templates/knowledge/.node_formats.json not present"
            )
        cls.db = json.loads(SIDECAR.read_text(encoding="utf-8"))

    def test_sidecar_keys_use_materialized_path(self):
        """Every entry key must be a 'knowledge/<rel>' path (the install key),
        and resolve to an actual shipped node."""
        bad = [k for k in self.db if not k.startswith("knowledge/")]
        self.assertEqual(bad, [], f"non-materialized keys: {bad}")

        valid = {_materialized_key(n) for n in _shipped_nodes()}
        orphans = [k for k in self.db if k not in valid]
        self.assertEqual(
            orphans, [],
            f"sidecar entries with no matching shipped node (stale; run "
            f"scripts/preship_kg_node_formats.py --prune): {orphans}",
        )

    def test_every_entry_hash_matches_current_node(self):
        """The reuse invariant: stored content_hash == hash of the CURRENT node.

        A mismatch means the node was edited after the summary was generated,
        so the runtime summarizer would NOT skip — it would regenerate,
        defeating the pre-ship. Regenerate with
        ``scripts/preship_kg_node_formats.py --force``.
        """
        node_by_key = {_materialized_key(n): n for n in _shipped_nodes()}
        mismatches: list[str] = []
        incomplete: list[str] = []
        for key, entry in self.db.items():
            node = node_by_key.get(key)
            if node is None:
                continue  # orphan — covered by the prior test
            full_text = node.read_text(encoding="utf-8")
            if _runtime_content_hash(full_text) != entry.get("content_hash"):
                mismatches.append(key)
            if not (entry.get("description") and entry.get("summary")):
                incomplete.append(key)
        self.assertEqual(
            mismatches, [],
            "shipped summaries are STALE vs current node text (reuse won't "
            "fire). Run scripts/preship_kg_node_formats.py --force. "
            f"Stale: {mismatches}",
        )
        self.assertEqual(
            incomplete, [],
            f"entries missing description/summary: {incomplete}",
        )


class ConsumeShippedSidecarLifecycleTest(unittest.TestCase):
    """Part 1.2: the runtime summarizer must REUSE (skip) a hash-matching
    shipped entry instead of regenerating — the whole point of the pre-ship.

    Runs ``generate-kg-summary.py`` as a subprocess against a temp project
    seeded with a hash-matching sidecar entry, and asserts it exits 0 with the
    'unchanged (hash match), skipping' message and NEVER selects a backend.
    KG_SUMMARY_BACKEND=skip is set as a tripwire: if the hash-skip branch did
    NOT fire, the script would fall through to backend selection and log the
    'no backend' / 'skip' path instead — a different, detectable message.
    """

    def test_runtime_summarizer_reuses_shipped_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node = root / "knowledge" / "concepts" / "x.md"
            node.parent.mkdir(parents=True)
            node.write_text(
                "---\ntitle: X Node\ntype: concept\n---\nbody text here\n",
                encoding="utf-8",
            )
            full = node.read_text(encoding="utf-8")
            c_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
            (root / "knowledge" / ".node_formats.json").write_text(
                json.dumps(
                    {
                        "knowledge/concepts/x.md": {
                            "title": "X Node",
                            "description": "d",
                            "summary": "s",
                            "content_hash": c_hash,
                            "total_chunks": 1,
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "KG_PROJECT_ROOT": str(root),
                "KG_SUMMARY_BACKEND": "skip",  # tripwire (see docstring)
                "VCT_DISABLE_HUB_RESOLVER": "1",
            }
            try:
                r = subprocess.run(
                    [sys.executable, str(RUNTIME_SUMMARIZER), str(node)],
                    env=env, capture_output=True, text=True, timeout=60,
                )
            except FileNotFoundError as exc:
                raise unittest.SkipTest(f"cannot run summarizer ({exc})")
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            self.assertIn(
                "hash match", r.stdout,
                "runtime summarizer did NOT take the shipped-entry skip path — "
                f"it would regenerate, defeating the pre-ship. stdout={r.stdout!r}",
            )


if __name__ == "__main__":
    unittest.main()
