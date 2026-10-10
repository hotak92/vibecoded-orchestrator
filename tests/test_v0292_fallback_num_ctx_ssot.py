# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 (m-R5-4 / m-R5-5) — legacy-fallback num_ctx routes through the SSOT.

Two shipped scripts still carry inline (legacy) Ollama embed legs that run
when ``EmbeddingService`` is unavailable. Before this change both hardcoded
their ``num_ctx`` (8 192 in ``migrate_to_new_embeddings.py``, 10 240 in
``search_knowledge.py``) and sent no ``truncate`` — a silent-tail-loss hazard
on the very script that re-embeds a whole corpus when the user switches
models. Both now resolve ``num_ctx`` through
``vco_lib.embedding_providers.ollama._num_ctx_for_model`` (the resolver the
canonical adapter uses, reading ``chunking.MODEL_TOKEN_LIMITS``) and send
``truncate: False`` so an overflow is a loud 400, never a lying 200.

The assertions here are BEHAVIOURAL: the real request body is captured from
a mocked ``requests.post`` and checked per-model (qwen3 → 10 240,
arctic → 4 096). A hardcoded window cannot pass the per-model cases, so
reintroducing a literal fails these tests rather than passing as text.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "claude_mcp_servers") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))


def _load_module(name: str, path: Path):
    """Import a script as a module without running its __main__ guard.

    Same pattern as tests/test_consumer_migrate_v0_2_18.py's helper —
    duplicated (not imported cross-file) per that file's rationale.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {"embedding": [0.1, 0.2, 0.3]}

    def json(self):
        return self._payload


class MigrateScriptNumCtxTests(unittest.TestCase):
    """m-R5-4 — migrate_to_new_embeddings.py legacy Ollama legs."""

    def setUp(self):
        self.mod = _load_module(
            f"_test_migrate_numctx_{id(self)}",
            REPO_ROOT / "claude_mcp_servers" / "scripts"
            / "migrate_to_new_embeddings.py",
        )

    def test_get_text_embedding_fallback_resolves_num_ctx_per_model(self):
        """Default model (qwen3) → 10 240 from the SSOT, not a hard 8 192."""
        env = {"EMBEDDING_MODEL": "qwen3-embedding:0.6b"}
        with mock.patch.object(self.mod, "HAS_EMBEDDING_SERVICE", False), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    self.mod.requests, "post",
                    return_value=_FakeResponse(),
                ) as post:
            vec = self.mod.get_text_embedding("some text")
        self.assertEqual(vec, [0.1, 0.2, 0.3])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["options"]["num_ctx"], 10_240)
        self.assertIs(body["truncate"], False)

    def test_get_text_embedding_fallback_tracks_model_table(self):
        """A model with a DIFFERENT window (arctic 4 096) proves the value
        is resolved per-model — a hardcoded constant cannot pass this."""
        env = {"EMBEDDING_MODEL": "snowflake-arctic-embed2:latest"}
        with mock.patch.object(self.mod, "HAS_EMBEDDING_SERVICE", False), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    self.mod.requests, "post",
                    return_value=_FakeResponse(),
                ) as post:
            self.mod.get_text_embedding("some text")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["options"]["num_ctx"], 4_096)
        self.assertIs(body["truncate"], False)

    def test_fallback_resolver_unknown_model_keeps_resolver_default(self):
        """Unknown model → the resolver's own conservative 8 192."""
        self.assertEqual(self.mod._fallback_num_ctx("never-heard-of:7b"), 8_192)

    def test_fallback_resolver_soft_fails_when_vco_lib_unimportable(self):
        """Half-install (vco_lib not importable) → documented 8 192, no raise."""
        with mock.patch.dict(
            sys.modules, {"vco_lib.embedding_providers.ollama": None},
        ):
            self.assertEqual(
                self.mod._fallback_num_ctx("qwen3-embedding:0.6b"), 8_192,
            )

    def test_main_probe_leg_resolves_num_ctx_per_model(self):
        """The pre-migration reachability probe in ``main()`` (the second
        legacy leg, m-R5-4 line ~535) goes through the same resolver —
        exercised THROUGH ``main()`` so the fix is wired, not just present.
        """
        env = {"EMBEDDING_MODEL": "snowflake-arctic-embed2:latest"}

        class _FakeCollections:
            def list_all(self):
                return []

        class _FakeClient:
            collections = _FakeCollections()

            def close(self):
                pass

        with mock.patch.object(self.mod, "HAS_EMBEDDING_SERVICE", False), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    self.mod, "get_client", return_value=_FakeClient(),
                ), \
                mock.patch.object(
                    self.mod.requests, "post",
                    return_value=_FakeResponse(),
                ) as post, \
                mock.patch.object(
                    self.mod.requests, "get",
                    return_value=_FakeResponse(),
                ), \
                mock.patch.object(
                    sys, "argv", ["migrate_to_new_embeddings.py", "--all"],
                ):
            self.mod.main()
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["options"]["num_ctx"], 4_096)
        self.assertIs(body["truncate"], False)


class SearchKnowledgeNoLegacyLegTests(unittest.TestCase):
    """v0.2.102 W6-B2 (owner ruling 2026-10-10): the search_knowledge.py
    legacy direct-Ollama query leg (m-R5-5's subject) is REMOVED — it embedded
    with ``EMBEDDING_MODEL`` against the legacy ``ollama_embed`` slot, another
    model's space. No service → the loud error, no HTTP call."""

    def setUp(self):
        self.mod = _load_module(
            f"_test_searchkg_numctx_{id(self)}",
            REPO_ROOT / "templates" / "scripts" / "search_knowledge.py",
        )

    def test_no_service_is_loud_and_posts_nothing(self):
        import requests

        with mock.patch.object(
            self.mod, "_get_or_create_embedding_service", return_value=None,
        ), mock.patch.object(requests, "post") as post:
            with self.assertRaises(Exception) as ctx:
                self.mod.get_embedding("some query")
            with self.assertRaises(Exception):
                self.mod._get_target_vector_slot()
        self.assertIn("never queried with a different embedding model", str(ctx.exception))
        post.assert_not_called()
        self.assertFalse(hasattr(self.mod, "_fallback_embed_request_body"))


if __name__ == "__main__":
    unittest.main()
