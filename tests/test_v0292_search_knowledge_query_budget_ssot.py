# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 (WP-E) — search_knowledge.py's query-token cap becomes SSOT-derived.

Before this change ``MAX_QUERY_TOKENS`` was a flat constant (2500),
independent of which embedding model was actually active — a qwen3 install
(large num_ctx) and a jina/CPU-tier install (small num_ctx) got the exact
same cap even though their real budgets differ by up to 5x. ``_max_query_tokens()``
now resolves the cap from the shared query-budget SSOT
(``claude_mcp_servers.rl_client.query_chunking.model_max_tokens``, which
itself reads ``chunking.py``'s ``MODEL_TOKEN_LIMITS`` — the same SSOT
``code_truncation.py`` and ``query_code_graph.py`` use) and falls back to
the flat constant only when the SSOT resolver is unavailable or returns
nothing usable.

Two things are covered:
  * ``_max_query_tokens()`` in isolation — SSOT-resolved vs. fallback path.
  * The truncation block inside ``search_knowledge()`` — proves the
    over-cap branch reuses the shared ``chunk_query()`` machinery (its
    first chunk) rather than a raw ``text[:n*4]`` character slice, via a
    fully-mocked Weaviate client/embedding so no live service is needed.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "claude_mcp_servers") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))


def _load_module(name: str, path: Path):
    """Import a script as a module without running its __main__ guard.

    Same pattern as tests/test_consumer_migrate_v0_2_18.py's helper —
    duplicated here (rather than imported cross-file) because pytest test
    modules are not meant to be import targets of one another.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class SearchKnowledgeQueryBudgetSSOTTests(unittest.TestCase):
    def _import_module(self):
        script_path = REPO_ROOT / "templates" / "scripts" / "search_knowledge.py"
        mod_name = f"_test_search_kg_budget_{id(self)}_{sys._getframe(1).f_code.co_name}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        return _load_module(mod_name, script_path)

    # -----------------------------------------------------------------
    # _max_query_tokens() — SSOT resolution + fallback.
    # -----------------------------------------------------------------

    def test_resolves_cap_from_ssot_when_available(self):
        mod = self._import_module()
        with patch(
            "claude_mcp_servers.rl_client.query_chunking.model_max_tokens",
            return_value=9500,
        ):
            self.assertEqual(mod._max_query_tokens(), 9500)

    def test_falls_back_to_flat_constant_when_ssot_import_fails(self):
        mod = self._import_module()
        # Simulate a partial install: the SSOT module cannot be imported.
        with patch.dict(sys.modules, {"claude_mcp_servers.rl_client.query_chunking": None}):
            self.assertEqual(mod._max_query_tokens(), mod.MAX_QUERY_TOKENS)

    def test_falls_back_to_flat_constant_when_ssot_returns_none(self):
        mod = self._import_module()
        # An unresolvable model name (SSOT genuinely doesn't know it) must
        # not silently produce a cap of 0/None — falls back to the flat
        # constant, same as the import-failure path.
        with patch(
            "claude_mcp_servers.rl_client.query_chunking.model_max_tokens",
            return_value=None,
        ):
            self.assertEqual(mod._max_query_tokens(), mod.MAX_QUERY_TOKENS)

    def test_falls_back_to_flat_constant_when_ssot_returns_zero(self):
        mod = self._import_module()
        # 0 is falsy in Python — the `if resolved:` guard must treat it the
        # same as None, not return a cap of 0 (which would truncate every
        # non-empty query to nothing).
        with patch(
            "claude_mcp_servers.rl_client.query_chunking.model_max_tokens",
            return_value=0,
        ):
            self.assertEqual(mod._max_query_tokens(), mod.MAX_QUERY_TOKENS)

    def test_cap_is_int_even_when_ssot_returns_float(self):
        mod = self._import_module()
        with patch(
            "claude_mcp_servers.rl_client.query_chunking.model_max_tokens",
            return_value=1234.7,
        ):
            cap = mod._max_query_tokens()
            self.assertEqual(cap, 1234)
            self.assertIsInstance(cap, int)

    def test_cap_reflects_the_active_model_not_a_hardcoded_name(self):
        # Proves _max_query_tokens() actually threads _ACTIVE_EMBEDDING_MODEL
        # through to the SSOT resolver rather than calling it with a fixed
        # literal — changing the active model changes which model the SSOT
        # is asked about.
        mod = self._import_module()
        seen: list[str] = []

        def _spy(model_name):
            seen.append(model_name)
            return 4321

        mod._ACTIVE_EMBEDDING_MODEL = "some/other-model"
        with patch(
            "claude_mcp_servers.rl_client.query_chunking.model_max_tokens",
            side_effect=_spy,
        ):
            mod._max_query_tokens()
        self.assertEqual(seen, ["some/other-model"])

    # -----------------------------------------------------------------
    # search_knowledge()'s truncation block — reuses chunk_query(), not a
    # raw character slice. Fully mocked Weaviate client + embedding so no
    # live service is required.
    # -----------------------------------------------------------------

    def _fake_client(self):
        """A Weaviate client stub whose one collection returns zero hits.

        Zero hits keeps every downstream branch (dedup, RL rerank, tier
        formatting) a no-op — the test only cares what happened to the
        QUERY TEXT before near_vector() was ever called.
        """
        fake_response = SimpleNamespace(objects=[])
        fake_collection = SimpleNamespace(
            query=SimpleNamespace(near_vector=lambda **kw: fake_response)
        )
        return SimpleNamespace(
            collections=SimpleNamespace(get=lambda name: fake_collection),
            close=lambda: None,
        )

    def test_oversized_query_truncation_reuses_shared_chunker(self):
        mod = self._import_module()
        long_query = "word " * 500  # ~2500 chars => ~625 tokens by count_tokens

        embedded_queries: list[str] = []

        def _fake_get_embedding(text):
            embedded_queries.append(text)
            return [0.0] * 8

        stub_first_chunk = "STUB_CHUNK_FROM_SHARED_CHUNKER"

        with patch.object(mod, "_max_query_tokens", return_value=10), \
             patch.object(mod, "get_weaviate_client", return_value=self._fake_client()), \
             patch.object(mod, "get_embedding", _fake_get_embedding), \
             patch(
                 "claude_mcp_servers.rl_client.query_chunking.chunk_query",
                 return_value=[stub_first_chunk, "second chunk unused"],
             ):
            mod.search_knowledge(long_query, collections=["FakeCollection"])

        # The embedded (post-truncation) query text is the shared chunker's
        # FIRST CHUNK verbatim — not a `text[:max_query_tokens*4]` slice of
        # the original (which would not equal the stub value at all).
        self.assertEqual(embedded_queries, [stub_first_chunk])

    def test_oversized_query_falls_back_to_char_slice_when_chunker_unavailable(self):
        mod = self._import_module()
        long_query = "word " * 500

        embedded_queries: list[str] = []

        def _fake_get_embedding(text):
            embedded_queries.append(text)
            return [0.0] * 8

        with patch.object(mod, "_max_query_tokens", return_value=10), \
             patch.object(mod, "get_weaviate_client", return_value=self._fake_client()), \
             patch.object(mod, "get_embedding", _fake_get_embedding), \
             patch.dict(sys.modules, {"claude_mcp_servers.rl_client.query_chunking": None}):
            mod.search_knowledge(long_query, collections=["FakeCollection"])

        # Last-resort fallback: a plain char slice sized off the SSOT cap
        # (max_query_tokens * 4 chars), never the untruncated original.
        self.assertEqual(embedded_queries, [long_query[:40]])
        self.assertLess(len(embedded_queries[0]), len(long_query))

    def test_short_query_is_never_truncated(self):
        mod = self._import_module()
        short_query = "short query"

        embedded_queries: list[str] = []

        def _fake_get_embedding(text):
            embedded_queries.append(text)
            return [0.0] * 8

        with patch.object(mod, "_max_query_tokens", return_value=9500), \
             patch.object(mod, "get_weaviate_client", return_value=self._fake_client()), \
             patch.object(mod, "get_embedding", _fake_get_embedding):
            mod.search_knowledge(short_query, collections=["FakeCollection"])

        self.assertEqual(embedded_queries, [short_query])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
