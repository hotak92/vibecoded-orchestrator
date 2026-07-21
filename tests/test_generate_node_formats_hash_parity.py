# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression: generate_node_formats.py stored hash == builder/runtime canonical.

Root-cause fix for the 2026-07-20 sidecar-hash-scheme mismatch. The LLM summary
generator ``claude_mcp_servers/scripts/generate_node_formats.py`` USED to store a
``content_hash`` computed as ``sha256(body.strip())[:16]`` (frontmatter-stripped
body), while BOTH the shipped-sidecar builder
(``scripts/build_shipped_kg_node_formats.py::_content_hash``) and the runtime
summarizer (``templates/scripts/generate-kg-summary.py::content_hash``) key by the
CANONICAL ``sha256(full_file_text)[:16]``. Because the builder looks up private
sidecar entries by that canonical hash, freshly generated summaries were INVISIBLE
to the ship path (reported stale/missing → users regenerated locally, a quiet
degradation). See the KG node
``shipped-kg-sidecar-hash-scheme-mismatch-2026-07-20``.

This module locks in two things:

1. HASH PARITY — the generator's ``content_hash`` produces the SAME value as the
   builder's ``_content_hash`` and the runtime summarizer's ``content_hash`` for
   the same full file text. If any of the three drifts, this fails.

2. LEGACY RE-KEY — an existing sidecar entry keyed under the old body.strip()
   scheme, whose node is byte-UNCHANGED, is silently re-keyed to the canonical
   full-text hash on load WITHOUT regenerating the summary (skip-unchanged
   correctness preserved across the transition). A genuinely stale entry
   (matches neither scheme) is left for the normal regenerate path.

Pure repo-introspection — no network / Ollama / Weaviate. The generator imports
``requests`` at module import time; if that dependency is absent the import-based
tests SkipTest rather than error.
"""

from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "claude_mcp_servers" / "scripts" / "generate_node_formats.py"
BUILDER = REPO_ROOT / "scripts" / "build_shipped_kg_node_formats.py"
RUNTIME_SUMMARIZER = REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"


def _load_module(name: str, path: Path):
    """Import a script by path, SkipTest'ing on a missing optional dep."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # e.g. requests not installed
        raise unittest.SkipTest(f"{path.name} deps missing ({exc})")
    return mod


def _canonical(full_text: str) -> str:
    """The one canonical scheme every layer must agree on."""
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:16]


class GeneratorHashParityTest(unittest.TestCase):
    """The generator's stored hash must equal builder + runtime canonical."""

    SAMPLE = "---\ntitle: X Node\ntype: concept\n---\n\nbody text here\nline two\n"

    def test_generator_matches_builder(self):
        gnf = _load_module("_gnf_parity", GENERATOR)
        bld = _load_module("_bld_parity", BUILDER)
        self.assertEqual(
            gnf.content_hash(self.SAMPLE),
            bld._content_hash(self.SAMPLE),
            "generate_node_formats.content_hash drifted from the shipped-sidecar "
            "builder's _content_hash — freshly generated summaries become "
            "invisible to the ship path.",
        )

    def test_generator_matches_runtime_summarizer(self):
        gnf = _load_module("_gnf_parity2", GENERATOR)
        rt = _load_module("_rt_parity", RUNTIME_SUMMARIZER)
        self.assertEqual(
            gnf.content_hash(self.SAMPLE),
            rt.content_hash(self.SAMPLE),
            "generate_node_formats.content_hash drifted from the runtime "
            "generate-kg-summary.py content_hash.",
        )

    def test_generator_hash_is_the_canonical_full_text_scheme(self):
        gnf = _load_module("_gnf_parity3", GENERATOR)
        self.assertEqual(
            gnf.content_hash(self.SAMPLE),
            _canonical(self.SAMPLE),
            "generator content_hash must be sha256(full_file_text)[:16].",
        )

    def test_legacy_scheme_differs_from_canonical(self):
        """Sanity: the legacy body-only hash is genuinely a DIFFERENT value, so
        the re-key path below is actually exercising a migration (not a no-op)."""
        gnf = _load_module("_gnf_parity4", GENERATOR)
        # body of SAMPLE as parse_frontmatter returns it (unstripped group 2)
        body = self.SAMPLE.split("---\n", 2)[2]
        self.assertNotEqual(
            gnf._legacy_content_hash(body),
            gnf.content_hash(self.SAMPLE),
            "legacy and canonical hashes must differ for the re-key to matter.",
        )


class LegacyRekeyTest(unittest.TestCase):
    """rekey_legacy_entries migrates old-scheme entries without regenerating."""

    def _write_node(self, td: Path) -> tuple[Path, str, str]:
        """Create a node under <td>/knowledge, return (node, canonical, legacy)."""
        kdir = td / "knowledge"
        node = kdir / "concepts" / "sample.md"
        node.parent.mkdir(parents=True)
        node.write_text(
            "---\ntitle: Sample\ntype: concept\n---\n\nreal body content\n",
            encoding="utf-8",
        )
        gnf = _load_module("_gnf_rekey_setup", GENERATOR)
        full = node.read_text(encoding="utf-8")
        parsed = gnf.parse_frontmatter(node)
        body = parsed[1]  # unstripped body, as the generator's parser returns it
        return node, gnf.content_hash(full), gnf._legacy_content_hash(body)

    def _configure_dirs(self, gnf, td: Path) -> None:
        """Point the module's module-level dir globals at the temp knowledge dir,
        the same way the --knowledge-dir override does in main()."""
        gnf.KNOWLEDGE_DIR = td / "knowledge"
        gnf.FORMATS_FILE = gnf.KNOWLEDGE_DIR / ".node_formats.json"
        gnf.PROJECT_ROOT = td

    def test_legacy_entry_is_rekeyed_without_regen(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            node, canonical, legacy = self._write_node(td)
            gnf = _load_module("_gnf_rekey1", GENERATOR)
            self._configure_dirs(gnf, td)

            db = {
                "knowledge/concepts/sample.md": {
                    "title": "Sample",
                    "description": "d",
                    "summary": "s",
                    "content_hash": legacy,  # OLD scheme
                    "total_chunks": 1,
                }
            }
            self.assertNotEqual(legacy, canonical, "fixture must use legacy hash")

            n = gnf.rekey_legacy_entries(db)

            self.assertEqual(n, 1, "exactly one legacy entry should be re-keyed")
            entry = db["knowledge/concepts/sample.md"]
            self.assertEqual(
                entry["content_hash"], canonical,
                "entry must now carry the canonical full-text hash",
            )
            # summary preserved verbatim — no regeneration
            self.assertEqual(entry["description"], "d")
            self.assertEqual(entry["summary"], "s")

    def test_canonical_entry_is_left_untouched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            node, canonical, legacy = self._write_node(td)
            gnf = _load_module("_gnf_rekey2", GENERATOR)
            self._configure_dirs(gnf, td)

            db = {
                "knowledge/concepts/sample.md": {
                    "title": "Sample",
                    "description": "d",
                    "summary": "s",
                    "content_hash": canonical,  # already canonical
                    "total_chunks": 1,
                }
            }
            n = gnf.rekey_legacy_entries(db)
            self.assertEqual(n, 0, "an already-canonical entry must NOT be re-keyed")
            self.assertEqual(db["knowledge/concepts/sample.md"]["content_hash"], canonical)

    def test_stale_entry_matches_neither_scheme_and_is_not_rekeyed(self):
        """A hash matching neither canonical nor legacy (node edited since) is
        left alone → the normal skip/regenerate logic handles it (it will fail
        has_formats' hash check and regenerate)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            node, canonical, legacy = self._write_node(td)
            gnf = _load_module("_gnf_rekey3", GENERATOR)
            self._configure_dirs(gnf, td)

            db = {
                "knowledge/concepts/sample.md": {
                    "title": "Sample",
                    "description": "d",
                    "summary": "s",
                    "content_hash": "deadbeefdeadbeef",  # neither scheme
                    "total_chunks": 1,
                }
            }
            n = gnf.rekey_legacy_entries(db)
            self.assertEqual(n, 0, "a genuinely stale entry must NOT be re-keyed")
            self.assertEqual(
                db["knowledge/concepts/sample.md"]["content_hash"],
                "deadbeefdeadbeef",
                "stale hash left intact for the regenerate path",
            )


class ProcessNodeCallSiteTest(unittest.TestCase):
    """F-9: pin the CALL SITE, not just the pure function.

    The 2026-07-20 defect lived in ``process_node``, which passed
    ``body.strip()`` into ``content_hash`` (the ``content_hash`` FUNCTION was
    already canonical, so the parity tests above pass even on the unfixed
    code). This test drives ``process_node`` on a node whose frontmatter
    differs from its body — so the legacy body-hash and the canonical
    full-text hash DIVERGE — and asserts that a DB seeded with the CANONICAL
    full-text hash makes ``process_node`` return ``"skipped"``. That skip only
    happens when ``process_node`` computes the canonical hash for its
    ``has_formats`` check. Under the unfixed code (``content_hash(body.strip())``)
    the computed hash would be the LEGACY value, wouldn't match the seeded
    canonical entry, and the node would NOT be skipped (it would fall through
    to generation). No network is touched on the skip path.
    """

    # Frontmatter (title/type) present + a distinct body → legacy(body.strip())
    # and canonical(full) are different 16-hex values.
    NODE_TEXT = (
        "---\n"
        "title: Call Site Node\n"
        "type: concept\n"
        "tags: [a, b]\n"
        "---\n"
        "\n"
        "This is the body prose that differs from the frontmatter.\n"
        "Second line of the body.\n"
    )

    def _prep(self, td: Path):
        gnf = _load_module("_gnf_callsite", GENERATOR)
        kdir = td / "knowledge"
        node = kdir / "concepts" / "callsite.md"
        node.parent.mkdir(parents=True)
        node.write_text(self.NODE_TEXT, encoding="utf-8")
        # Point module globals at the temp knowledge dir (as --knowledge-dir does).
        gnf.KNOWLEDGE_DIR = kdir
        gnf.FORMATS_FILE = kdir / ".node_formats.json"
        gnf.PROJECT_ROOT = td
        return gnf, node

    def test_process_node_uses_canonical_full_text_hash_for_skip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            gnf, node = self._prep(td)

            full = node.read_text(encoding="utf-8")
            canonical = gnf.content_hash(full)
            parsed = gnf.parse_frontmatter(node)
            legacy = gnf._legacy_content_hash(parsed[1])
            self.assertNotEqual(
                canonical, legacy,
                "fixture must make canonical and legacy hashes diverge",
            )

            rel = str(node.relative_to(td))
            db = {
                rel: {
                    "title": "Call Site Node",
                    "description": "d",
                    "summary": "s",
                    "content_hash": canonical,  # seeded under CANONICAL scheme
                    "total_chunks": 1,
                }
            }

            # dry_run False, force False. If process_node computed the canonical
            # hash it matches the seeded entry → "skipped" (never reaches the
            # LLM). If it computed the legacy body.strip() hash (the bug), the
            # entry won't match → it would try to generate.
            status = gnf.process_node(
                node, model="unused", dry_run=False, force=False, db=db
            )
            self.assertEqual(
                status, "skipped",
                "process_node must compute the CANONICAL full-text hash so a "
                "canonically-keyed complete entry is recognized as current; "
                "got a non-skip, meaning the call site used the legacy body hash.",
            )

    def test_process_node_stores_canonical_hash_when_seeded_legacy(self):
        """Complementary pin: a node seeded with the LEGACY hash is NOT skipped,
        and the stored hash the generator WOULD write is the canonical one (we
        assert the computed skip-hash indirectly by confirming a legacy-keyed
        entry is treated as stale, i.e. not skipped)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            gnf, node = self._prep(td)

            parsed = gnf.parse_frontmatter(node)
            legacy = gnf._legacy_content_hash(parsed[1])

            rel = str(node.relative_to(td))
            db = {
                rel: {
                    "title": "Call Site Node",
                    "description": "d",
                    "summary": "s",
                    "content_hash": legacy,  # OLD scheme entry
                    "total_chunks": 1,
                }
            }
            # With a legacy-keyed entry and no re-key applied, process_node's
            # canonical hash will NOT match → not skipped (dry_run avoids the LLM).
            status = gnf.process_node(
                node, model="unused", dry_run=True, force=False, db=db
            )
            self.assertEqual(
                status, "dry_run",
                "a legacy-hash entry must be seen as stale by the canonical "
                "skip check (would regenerate); dry_run confirms the non-skip.",
            )


if __name__ == "__main__":
    unittest.main()
