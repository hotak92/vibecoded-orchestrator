# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W6-B — the launcher's Code Graph Embeddings keys are READ.

The launcher Preferences page → "Code Graph Embeddings" has written three
``app_state`` keys since v0.2.23 that no Python read (the W6-R audit's F1 —
a whole placebo section):

* ``code_embed_backend_override`` — the backend pin
  ("" | "auto" | "codesage" | "qwen3" | "jina" | "openai" | "ollama").
* ``code_embed_openai_model`` — the OpenAI model when the pin is ``openai``.
* ``code_embed_ollama_model`` — the Ollama model when the pin is ``ollama``.

Since v0.2.102 W6-B the reader is
:func:`vco_lib.embedding_service.resolve_code_model_id`, the ONE home
``EmbeddingService.for_project`` (the constructor the code-graph analyzer,
the install seed and the MCP server all use) resolves the code model
through. Precedence: explicit env (``CODE_EMBED_MODEL``, then an explicitly
exported ``CODE_EMBED_BACKEND``) → the Preferences pin → the install
default. The ``openai`` pin passes through the SAME consent gate the
KG-summary OpenAI tier uses (``kg_summary_openai_consent``) and is refused
loudly — never silently re-routed to a free backend.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]

from vco_lib.embedding_service import (  # noqa: E402
    DEFAULT_CODE_MODEL,
    DEFAULT_TEXT_MODEL,
    CodeEmbedConsentRefusal,
    EmbeddingService,
    NoEmbeddingBackendError,
    resolve_code_model_id,
)

_FALLBACK_JINA_MODEL = "unclemusclez/jina-embeddings-v2-base-code:latest"


def _pin_db(tmp_path: Path, rows: dict[str, str]) -> None:
    """A launcher.db whose app_state holds exactly ``rows``."""
    conn = sqlite3.connect(tmp_path / "launcher.db")
    conn.execute("CREATE TABLE IF NOT EXISTS app_state "
                 "(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    for key, value in rows.items():
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?, 0)", (key, value))
    conn.commit()
    conn.close()


class ResolveCodeModelIdTests(unittest.TestCase):
    """Unit leg: the precedence table, with dict seams for env + app_state."""

    def resolve(self, env=None, state=None):
        return resolve_code_model_id(env or {}, app_state=(state or {}).get)

    def test_keys_match_the_launcher(self):
        loaders = (REPO_ROOT / "launcher" / "src" / "lib" / "preferences"
                   / "loaders.ts").read_text(encoding="utf-8")
        from vco_lib import embedding_service as es

        for const in (
            "APP_STATE_KEY_CODE_EMBED_OVERRIDE",
            "APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL",
            "APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL",
        ):
            key = getattr(es, const)
            assert f"'{key}'" in loaders, f"{key} missing from loaders.ts"

    def test_no_env_no_pin_is_the_install_default(self):
        assert self.resolve() == (DEFAULT_CODE_MODEL, "default")

    def test_auto_and_empty_pin_are_no_pin(self):
        for empty in ("", "auto"):
            assert self.resolve(
                state={"code_embed_backend_override": empty}
            ) == (DEFAULT_CODE_MODEL, "default"), empty

    def test_pin_codesage(self):
        assert self.resolve(
            state={"code_embed_backend_override": "codesage"}
        ) == (DEFAULT_CODE_MODEL, "preferences")

    def test_pin_qwen3(self):
        assert self.resolve(
            state={"code_embed_backend_override": "qwen3"}
        ) == (DEFAULT_TEXT_MODEL, "preferences")

    def test_pin_jina(self):
        assert self.resolve(
            state={"code_embed_backend_override": "jina"}
        ) == (_FALLBACK_JINA_MODEL, "preferences")

    def test_pin_ollama_uses_the_model_row(self):
        assert self.resolve(state={
            "code_embed_backend_override": "ollama",
            "code_embed_ollama_model": "my-code-model:latest",
        }) == ("my-code-model:latest", "preferences")

    def test_pin_ollama_without_model_falls_to_default(self):
        assert self.resolve(
            state={"code_embed_backend_override": "ollama"}
        ) == (DEFAULT_CODE_MODEL, "default")

    def test_pin_openai_with_consent_uses_the_model_row(self):
        assert self.resolve(state={
            "code_embed_backend_override": "openai",
            "code_embed_openai_model": "text-embedding-3-large",
            "kg_summary_openai_consent": "true",
        }) == ("text-embedding-3-large", "preferences")

    def test_pin_openai_defaults_the_model(self):
        assert self.resolve(state={
            "code_embed_backend_override": "openai",
            "kg_summary_openai_consent": "true",
        }) == ("text-embedding-3-small", "preferences")

    def test_pin_openai_without_consent_is_refused_loudly(self):
        with self.assertRaises(CodeEmbedConsentRefusal) as ctx:
            self.resolve(state={
                "code_embed_backend_override": "openai",
                "code_embed_openai_model": "text-embedding-3-large",
                # no kg_summary_openai_consent row
            })
        msg = str(ctx.exception)
        # The refusal names the REAL source (the Preferences pin), never an
        # env var the operator did not set — same contract as the
        # KG-summary gate's message (W5-D).
        assert "code_embed_backend_override" in msg, msg
        assert "kg_summary_openai_consent" in msg, msg
        assert "CODE_EMBED_BACKEND" not in msg, msg
        assert ctx.exception.model_id == "text-embedding-3-large"

    def test_pin_openai_consent_truthiness_matches_the_kg_summary_gate(self):
        # Only true/1/yes (case-insensitive) grant consent — anything else
        # (including garbage) refuses, like summary_backends.
        for raw in ("true", "1", "YES"):
            assert self.resolve(state={
                "code_embed_backend_override": "openai",
                "kg_summary_openai_consent": raw,
            }) == ("text-embedding-3-small", "preferences"), raw
        for raw in ("false", "garbage", ""):
            with self.assertRaises(CodeEmbedConsentRefusal):
                self.resolve(state={
                    "code_embed_backend_override": "openai",
                    "kg_summary_openai_consent": raw,
                })

    def test_unknown_pin_is_ignored_toward_the_default(self):
        assert self.resolve(
            state={"code_embed_backend_override": "future-thing"}
        ) == (DEFAULT_CODE_MODEL, "default")

    def test_env_model_wins_over_the_pin(self):
        assert self.resolve(
            env={"CODE_EMBED_MODEL": "env-model"},
            state={"code_embed_backend_override": "qwen3"},
        ) == ("env-model", "env")

    def test_env_backend_ollama_wins_over_the_pin(self):
        assert self.resolve(
            env={"CODE_EMBED_BACKEND": "ollama"},
            state={"code_embed_backend_override": "codesage"},
        ) == (DEFAULT_TEXT_MODEL, "env")

    def test_env_backend_service_blocks_the_pin_even_though_same_model(self):
        # An explicitly exported CODE_EMBED_BACKEND is an operator decision:
        # it must outrank the Preferences pin (env always wins) even when the
        # derived model is the same string the pin would produce.
        assert self.resolve(
            env={"CODE_EMBED_BACKEND": "service"},
            state={"code_embed_backend_override": "qwen3"},
        ) == (DEFAULT_CODE_MODEL, "env")

    def test_env_model_wins_over_env_backend(self):
        assert self.resolve(env={
            "CODE_EMBED_BACKEND": "ollama", "CODE_EMBED_MODEL": "explicit",
        }) == ("explicit", "env")


class LauncherDbReadTests(unittest.TestCase):
    """The production read leg: launcher.db app_state, not a dict seam."""

    def test_pin_is_read_from_launcher_db(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            _pin_db(tmp_path, {"code_embed_backend_override": "qwen3"})
            with patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}):
                model, source = resolve_code_model_id(env={})
            assert (model, source) == (DEFAULT_TEXT_MODEL, "preferences")


class ForProjectWiringTests(unittest.TestCase):
    """The resolver's answer is what the indexer's constructor uses."""

    def _patched_adapters(self, *, ollama_ready=True, code_ready=True):
        from vco_lib.embedding_providers import (
            CodeEmbedAdapter,
            OllamaAdapter,
            OpenAIAdapter,
        )
        from vco_lib.embedding_providers.openai import ValidationResult

        ollama_mock = MagicMock(spec=OllamaAdapter)
        ollama_mock.is_reachable.return_value = ollama_ready
        code_mock = MagicMock(spec=CodeEmbedAdapter)
        code_mock.is_reachable.return_value = code_ready
        openai_mock = MagicMock(spec=OpenAIAdapter)
        openai_mock.validate.return_value = ValidationResult(
            valid=True, reason=None
        )
        return (
            patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_mock),
            patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_mock),
            patch("vco_lib.embedding_service.OpenAIAdapter", return_value=openai_mock),
        )

    def test_for_project_resolves_the_pin(self):
        """F1 red-proof target: a qwen3 pin changes the model/slot the
        indexer's service holds — pre-W6-B for_project ignored launcher.db
        for the code model entirely."""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            _pin_db(tmp_path, {"code_embed_backend_override": "qwen3"})
            p1, p2, p3 = self._patched_adapters()
            with p1, p2, p3, patch.dict(os.environ, {
                "VCT_STATE_DIR": str(tmp_path),
                "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
            }):
                svc = EmbeddingService.for_project(project_root=tmp_path)
                try:
                    self.assertEqual(svc.code_model_id, DEFAULT_TEXT_MODEL)
                    self.assertEqual(svc.code_vector_slot, "qwen3_embed")
                finally:
                    svc.close()

    def test_for_project_openai_pin_without_consent_keeps_text_alive(self):
        """The refusal must not break the KG/text path: for_project still
        constructs, and every CODE embed raises the refusal."""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            _pin_db(tmp_path, {
                "code_embed_backend_override": "openai",
                "code_embed_openai_model": "text-embedding-3-small",
            })
            p1, p2, p3 = self._patched_adapters()
            with p1, p2, p3, patch.dict(os.environ, {
                "VCT_STATE_DIR": str(tmp_path),
                "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
            }):
                svc = EmbeddingService.for_project(project_root=tmp_path)
                try:
                    self.assertEqual(svc.code_vector_slot, "openai_code_embed")
                    with self.assertRaises(NoEmbeddingBackendError) as ctx:
                        svc.embed_code("def f(): pass")
                    self.assertIn("kg_summary_openai_consent", str(ctx.exception))
                    with self.assertRaises(NoEmbeddingBackendError):
                        svc.embed_code_batch(["def f(): pass"])
                finally:
                    svc.close()


class ConsentRefusalBlocksEmbedLegsTests(unittest.TestCase):
    """The permanent refusal raises on every code-embed entry point — no
    cost-bearing OpenAI call, no silent re-route (unit leg, injected
    adapters, no for_project needed)."""

    def _service(self):
        from vco_lib.embedding_providers import (
            CodeEmbedAdapter,
            OllamaAdapter,
            OpenAIAdapter,
        )

        ollama_mock = MagicMock(spec=OllamaAdapter)
        code_mock = MagicMock(spec=CodeEmbedAdapter)
        # The CodeEmbed service being UP must NOT silence the consent gate.
        code_mock.is_reachable.return_value = True
        openai_mock = MagicMock(spec=OpenAIAdapter)
        svc = EmbeddingService(
            project_root=None,
            ollama_url="http://localhost:11435",
            code_embed_url="http://localhost:11440",
            text_model_id="qwen3-embedding:0.6b",
            code_model_id="text-embedding-3-small",
            openai_api_key="sk-test",
            ollama_adapter=ollama_mock,
            code_adapter=code_mock,
            openai_adapter=openai_mock,
        )
        svc._code_backend_error = CodeEmbedConsentRefusal(
            "consent not granted", "text-embedding-3-small"
        )
        svc._code_ready = False
        return svc, openai_mock

    def test_every_code_embed_leg_raises_and_openai_is_never_called(self):
        svc, openai_mock = self._service()
        with self.assertRaises(NoEmbeddingBackendError):
            svc.embed_code("def f(): pass")
        with self.assertRaises(NoEmbeddingBackendError):
            svc.embed_code_batch(["def f(): pass"])
        with self.assertRaises(NoEmbeddingBackendError):
            svc.embed_code_query("search")
        openai_mock.embed.assert_not_called()
        openai_mock.embed_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
