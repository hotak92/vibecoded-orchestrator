# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The version-keyed chat-model context table.

Three properties, each with a concrete cost behind it.

**EXACT keys only.** ``lookup("glm-5.1-flash-x")`` must NOT resolve to
``glm-5.1``. Within the shipped vendor, one minor version has a 1M window and
the previous one has 200K, so a partial match would tell the client a model has
five times the context it does. This is the deliberate OPPOSITE of
``weaviate_mcp.chunking._num_ctx_for_model``, which partial-matches on purpose
because Ollama tags vary by quantisation while the architectural limit does
not — two questions with the same words and opposite lookup rules. The pair of
tests documents the asymmetry.

**Every row cites an official source.** A window without a citation is a guess,
and guessing is precisely what a version-keyed table exists to prevent, so an
uncited row is dropped from the advertising decision rather than trusted.

**Degradation is never silent.** Absent export -> seed. Malformed export ->
seed plus a warning naming the path. Unknown schema -> seed plus a different
warning. The source is reported in ``/health`` either way.
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from model_router import context_table as ct

#: The ten vendor rows the shipped seed must carry.
EXPECTED_VENDOR_SEED_IDS = {
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5",
    "glm-5-turbo", "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air",
}

#: The first-party rows. They exist for ONE reader —
#: ``vco_lib.vscode_settings.decorate_1m`` — so a slot/default naming a 1M
#: Claude model carries the client's own ``[1m]`` hint (a plain id is
#: assumed 200K; field symptom: "0% context remaining" right after
#: compaction). The gateway's ``/v1/models`` never reads them: first-party
#: ids are published verbatim (pinned below).
EXPECTED_CLAUDE_SEED_IDS = {
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
}

#: The seed, and nothing else.
EXPECTED_SEED_IDS = EXPECTED_VENDOR_SEED_IDS | EXPECTED_CLAUDE_SEED_IDS

#: Exactly the models whose official page states a 1M window.
EXPECTED_1M_IDS = {"glm-5.3", "glm-5.3-flash", "glm-5.2"} | EXPECTED_CLAUDE_SEED_IDS

#: Official documentation host per vendor id. A citation anywhere else is
#: not a vendor page.
OFFICIAL_DOC_PREFIX = {
    "zai": "https://docs.z.ai/",
    "anthropic": "https://docs.anthropic.com",
}


class SeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seed = ct.load_seed()

    def test_seed_ships_beside_the_module(self) -> None:
        """It must travel inside the wheel, not be resolved from a checkout."""
        self.assertTrue(ct.SEED_PATH.is_file(), ct.SEED_PATH)
        self.assertEqual(ct.SEED_PATH.parent.name, "model_router")

    def test_seed_has_exactly_the_documented_rows(self) -> None:
        self.assertEqual(set(self.seed.rows), EXPECTED_SEED_IDS)

    def test_every_seed_row_cites_an_official_vendor_page(self) -> None:
        """A row without a citation is a test failure, not a data choice."""
        for model_id, row in self.seed.rows.items():
            with self.subTest(model=model_id):
                prefix = OFFICIAL_DOC_PREFIX.get(row.vendor)
                self.assertIsNotNone(prefix, f"{model_id}: unknown vendor {row.vendor!r}")
                self.assertTrue(
                    row.source.startswith(prefix or "\0"),
                    f"{model_id} cites {row.source!r}, which is not an official "
                    f"{row.vendor} documentation URL",
                )

    def test_no_seed_row_was_dropped_for_lack_of_a_citation(self) -> None:
        self.assertEqual(self.seed.uncited, ())

    def test_the_model_with_no_page_of_its_own_says_so(self) -> None:
        """Its page 404s; the citation points at the card that carries the spec
        and the note records that honestly rather than implying a page."""
        row = self.seed.rows["glm-4.5-air"]
        self.assertEqual(row.source, "https://docs.z.ai/guides/llm/glm-4.5")
        self.assertIn("404", row.source_note)

    def test_one_m_flags_match_the_official_windows(self) -> None:
        flagged = {mid for mid, row in self.seed.rows.items() if row.window_1m}
        self.assertEqual(flagged, EXPECTED_1M_IDS)

    def test_window_1m_agrees_with_context_window(self) -> None:
        for model_id, row in self.seed.rows.items():
            with self.subTest(model=model_id):
                self.assertEqual(
                    row.window_1m, row.context_window >= 1_000_000,
                    f"{model_id}: window_1m={row.window_1m} but "
                    f"context_window={row.context_window}",
                )

    def test_adjacent_versions_differ_which_is_why_keys_are_exact(self) -> None:
        """The concrete case the exact-key rule exists for."""
        self.assertEqual(self.seed.rows["glm-5.2"].context_window, 1_000_000)
        self.assertEqual(self.seed.rows["glm-5.1"].context_window, 200_000)

    def test_claude_rows_are_first_party_and_1m(self) -> None:
        """The Claude 5 rows exist so ``vco_lib.vscode_settings.decorate_1m``
        can append the client's own ``[1m]`` hint to a slot/default naming
        one of them. Every such row is vendor ``anthropic`` and 1M; every
        other ``claude``-named id is deliberately absent (the client knows
        the older families' windows natively, and a row could only disagree
        with it)."""
        from model_router.vendors import ANTHROPIC_FAMILY

        claude_rows = {mid for mid in self.seed.rows if "claude" in mid.lower()}
        self.assertEqual(claude_rows, EXPECTED_CLAUDE_SEED_IDS)
        for model_id in claude_rows:
            row = self.seed.rows[model_id]
            with self.subTest(model=model_id):
                self.assertEqual(row.vendor, ANTHROPIC_FAMILY.family_id)
                self.assertTrue(row.window_1m)
                self.assertEqual(row.context_window, 1_000_000)

    def test_claude_rows_never_reach_the_gateway_catalog(self) -> None:
        """The gateway publishes first-party ids VERBATIM; the ``[1m]``
        decision is applied to VENDOR entries only. A Claude row in the seed
        must therefore not change what ``/v1/models`` advertises. Driven
        through the real ``CatalogService.union`` with the real seed's
        ``advertise_1m``, so a refactor that routes first-party entries
        through the table reds here rather than in the field."""
        import asyncio

        from model_router import catalog as cat
        from model_router.vendors import ANTHROPIC_FAMILY, VENDORS

        seed = self.seed
        assert seed.advertise_1m("claude-opus-5"), "precondition: the seed row is 1M"

        async def fetch(url, _headers):
            if ANTHROPIC_FAMILY.upstream in url:
                return {"data": [{"id": "claude-opus-5", "display_name": "Opus 5"}]}
            return {"data": [{"id": "glm-5.3", "display_name": "GLM-5.3"}]}

        async def vendor_key(_vendor):
            return "key"

        service = cat.CatalogService(
            vendors=dict(VENDORS),
            anthropic=ANTHROPIC_FAMILY,
            fetch_json=fetch,
            oauth_token=lambda: "tok",
            vendor_key=vendor_key,
            live_ttl_s=3600,
            static_ttl_s=60,
            clock=lambda: 100.0,
        )
        entries, _ = asyncio.run(service.union(advertise_1m=seed.advertise_1m))
        ids = {e.id for e in entries}
        self.assertIn("claude-opus-5", ids, "first-party id published verbatim")
        self.assertNotIn("claude-opus-5[1m]", ids, "the seed row must not decorate it")
        self.assertIn("claude-gw/glm-5.3[1m]", ids, "the vendor row still does")

    def test_every_row_names_a_vendor_that_exists(self) -> None:
        from model_router.vendors import ANTHROPIC_FAMILY, VENDORS

        known = set(VENDORS) | {ANTHROPIC_FAMILY.family_id}
        for model_id, row in self.seed.rows.items():
            with self.subTest(model=model_id):
                self.assertIn(row.vendor, known)


class ExactLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seed = ct.load_seed()

    def test_exact_id_resolves(self) -> None:
        self.assertIsNotNone(self.seed.lookup("glm-5.1"))

    def test_a_longer_id_does_not_partial_match(self) -> None:
        self.assertIsNone(self.seed.lookup("glm-5.1-flash-x"))
        self.assertFalse(self.seed.advertise_1m("glm-5.2-preview"))

    def test_a_family_prefix_does_not_match(self) -> None:
        self.assertIsNone(self.seed.lookup("glm"))
        self.assertIsNone(self.seed.lookup("glm-6"))
        # Positive control: "glm-5" resolves because it IS a row, not because
        # a prefix rule found it.
        self.assertIsNotNone(self.seed.lookup("glm-5"))

    def test_case_is_significant(self) -> None:
        self.assertIsNone(self.seed.lookup("GLM-5.3"))


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wp9-ctx-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.export = self.dir / "chat_model_context.json"

    def _write(self, payload: object) -> None:
        self.export.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def test_absent_export_falls_back_to_the_seed(self) -> None:
        table = ct.ContextTableLoader(self.export).current()
        self.assertEqual(table.source, ct.SOURCE_SEED_NO_EXPORT)
        self.assertEqual(set(table.rows), EXPECTED_SEED_IDS)

    def test_export_wins_when_present(self) -> None:
        self._write(
            {
                "schema_version": 1,
                "generated_at": "2026-09-02T00:00:00Z",
                "source": "launcher.db",
                "models": {
                    "acme-xl": {
                        "vendor": "acme",
                        "context_window": 1_000_000,
                        "max_output": 64_000,
                        "window_1m": True,
                        "source": "https://docs.acme.example/xl",
                    }
                },
            }
        )
        table = ct.ContextTableLoader(self.export).current()
        self.assertEqual(table.source, ct.SOURCE_EXPORT)
        self.assertEqual(set(table.rows), {"acme-xl"})
        self.assertTrue(table.advertise_1m("acme-xl"))

    def test_malformed_export_warns_and_uses_the_seed(self) -> None:
        self._write("{not json at all")
        loader = ct.ContextTableLoader(self.export)
        with self.assertLogs(ct.logger, level=logging.WARNING) as captured:
            table = loader.current()
        self.assertEqual(table.source, ct.SOURCE_SEED_MALFORMED)
        self.assertEqual(set(table.rows), EXPECTED_SEED_IDS)
        self.assertIn(str(self.export), "\n".join(captured.output))

    def test_unknown_schema_warns_distinctly_and_uses_the_seed(self) -> None:
        self._write({"schema_version": 99, "models": {}})
        loader = ct.ContextTableLoader(self.export)
        with self.assertLogs(ct.logger, level=logging.WARNING) as captured:
            table = loader.current()
        self.assertEqual(table.source, ct.SOURCE_SEED_UNSUPPORTED)
        self.assertIn("unsupported schema", "\n".join(captured.output))

    def test_uncited_export_row_is_dropped_and_named(self) -> None:
        self._write(
            {
                "schema_version": 1,
                "models": {
                    "cited": {
                        "vendor": "acme", "context_window": 1_000_000,
                        "max_output": 1, "window_1m": True,
                        "source": "https://docs.acme.example/cited",
                    },
                    "guessed": {
                        "vendor": "acme", "context_window": 1_000_000,
                        "max_output": 1, "window_1m": True, "source": "",
                    },
                },
            }
        )
        loader = ct.ContextTableLoader(self.export)
        with self.assertLogs(ct.logger, level=logging.WARNING) as captured:
            table = loader.current()
        self.assertEqual(set(table.rows), {"cited"})
        self.assertEqual(table.uncited, ("guessed",))
        self.assertFalse(table.advertise_1m("guessed"))
        self.assertIn("guessed", "\n".join(captured.output))

    def test_reload_on_mtime_change(self) -> None:
        self._write({"schema_version": 1, "models": {}})
        loader = ct.ContextTableLoader(self.export)
        self.assertEqual(set(loader.current().rows), set())
        self._write(
            {
                "schema_version": 1,
                "models": {
                    "acme-xl": {
                        "vendor": "acme", "context_window": 10, "max_output": 1,
                        "window_1m": False,
                        "source": "https://docs.acme.example/xl",
                    }
                },
            }
        )
        # Force a distinguishable stamp even on a coarse-resolution clock.
        import os

        os.utime(self.export, (1_000_000, 1_000_000))
        self.assertEqual(set(loader.current().rows), {"acme-xl"})

    def test_export_removal_returns_to_the_seed(self) -> None:
        self._write({"schema_version": 1, "models": {}})
        loader = ct.ContextTableLoader(self.export)
        self.assertEqual(loader.current().source, ct.SOURCE_EXPORT)
        self.export.unlink()
        table = loader.current()
        self.assertEqual(table.source, ct.SOURCE_SEED_NO_EXPORT)
        self.assertEqual(set(table.rows), EXPECTED_SEED_IDS)


class SeparateFromEmbeddingLimitsTests(unittest.TestCase):
    """The boundary against ``MODEL_TOKEN_LIMITS`` is real, not a comment."""

    def test_this_table_does_not_import_the_embedding_limits(self) -> None:
        """Prose may reference the other table (and does, to mark the
        boundary); CODE must not reach for it."""
        import ast

        tree = ast.parse(Path(ct.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            self.assertFalse(
                name.startswith(("weaviate_mcp", "vco_lib.embedding")),
                f"context_table.py imports {name!r}; the chat-model table and "
                "the embedding num_ctx table are deliberately separate",
            )

    def test_the_two_lookup_rules_are_opposite(self) -> None:
        """One partial-matches by design; this one must not."""
        try:
            from weaviate_mcp.chunking import _num_ctx_for_model
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"weaviate_mcp not importable here: {exc}")
        # The embedding side resolves a quantisation-tagged variant...
        self.assertIsNotNone(_num_ctx_for_model("qwen3-embedding:0.6b-q8_0"))
        # ...while the chat side refuses anything but an exact id.
        self.assertIsNone(ct.load_seed().lookup("glm-5.3-preview"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
