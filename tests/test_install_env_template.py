# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py's orchestrator-root ``.env`` after v0.2.97 (one writer per concern).

``_write_env_config`` no longer writes the file itself:

  * the canonical keys go into the VCO-managed block through
    :func:`vco_lib.env_template.apply_env_template` (via
    :func:`vco_lib.install_env.write_orchestrator_env`);
  * the install-time-only keys are the text a NEW file starts with
    (:func:`vco_lib.install_env.render_install_env_tail`), same lines and
    format as before;
  * an existing file — re-install, and ``--update``'s
    ``_reconcile_env_keys`` — is refreshed FILL-ONLY: keys are added,
    never changed.

Also pinned: the re-install path no longer plants the placeholder values
the retired ``_ensure_env_template`` wrote (``PROJECT_NAME=<project>``,
``KG_COLLECTION=Project_KnowledgeGraph``).

The writer contract itself is covered by ``tests/test_env_template.py``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402

from vco_lib.env_template import ENV_TEMPLATE_BEGIN, ENV_TEMPLATE_END
from vco_lib.install_env import (
    orchestrator_env_template_keys,
    render_install_env_tail,
)

_ENV = {
    "KG_COLLECTION": "Gamma_KnowledgeGraph",
    "DEVELOPMENT_COLLECTION": "Gamma_Development",
    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
}
_INSTALL_TIME_KEYS = (
    "WEAVIATE_GRPC_PORT", "EMBEDDING_MODEL", "EMBEDDING_DIMS",
    "CODE_EMBED_BACKEND", "CODE_EMBED_MODEL", "CODE_EMBED_DIMS",
    "CODE_EMBED_SERVICE_URL", "EMBEDDING_PROVIDER", "VCT_TELEMETRY",
)


def _assignments(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    ]


def _block(text: str) -> str:
    return text[text.index(ENV_TEMPLATE_BEGIN):text.index(ENV_TEMPLATE_END)]


class _InstallRoot(unittest.TestCase):
    """Runs ``_write_env_config`` against a scratch PROJECT_ROOT."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "state" / "logs").mkdir(parents=True)
        self._orig_root = install.PROJECT_ROOT
        install.PROJECT_ROOT = self.root
        install._PENDING_EVENTS.clear()
        env = {k: v for k, v in install.os.environ.items()
               if k not in ("PROJECT_NAME", "CODE_GRAPH_PROJECT", "ACTIVE_EMBEDDING",
                            "WEAVIATE_PORT", "OLLAMA_PORT", "CODE_EMBED_PORT",
                            "CODE_EMBED_MAX_CONCURRENT")}
        env.update(_ENV)
        # The --openai-key path stores into the secrets store: never a real
        # hub (discard port) — the file store is conftest's tmp redirect.
        env["VCT_HUB_PORT"] = "9"
        env.pop("VCT_HUB_TOKEN", None)
        self._env = mock.patch.dict("os.environ", env, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        install.PROJECT_ROOT = self._orig_root
        self._td.cleanup()

    def write(self, **embed_overrides) -> str:
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        cfg.update(embed_overrides)
        args = mock.Mock()
        args.telemetry = "off"
        args.yes = True
        install._write_env_config(cfg, args)
        return (self.root / ".env").read_text(encoding="utf-8")


class TestFreshInstall(_InstallRoot):

    def test_canonical_keys_are_in_the_managed_block_install_keys_outside(self):
        text = self.write(active_embedding="arctic")
        block = _block(text)
        outside = text.replace(block, "")
        for key in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION", "SHARED_KG_COLLECTION",
                    "SHARED_KG_WRITE_DISABLED", "SHARED_KG_OPT_OUT",
                    "SHARED_KG_READ_DISABLED", "ACTIVE_EMBEDDING", "WEAVIATE_URL",
                    "WEAVIATE_PORT", "OLLAMA_URL", "OLLAMA_PORT"):
            self.assertIn(f"\n{key}=", block, key)
            self.assertEqual(len(_assignments(text, key)), 1, key)
        for key in _INSTALL_TIME_KEYS:
            self.assertEqual(len(_assignments(outside, key)), 1, key)
            self.assertNotIn(f"\n{key}=", block, key)
        self.assertEqual(_assignments(text, "KG_COLLECTION"), ["Gamma_KnowledgeGraph"])
        self.assertEqual(_assignments(text, "ACTIVE_EMBEDDING"), ["arctic"])
        self.assertEqual(_assignments(text, "VCT_TELEMETRY"), ["false"])
        self.assertTrue(text.startswith("# VibeCoded Tools — Orchestrator Configuration\n"))

    def test_unknown_project_name_is_not_invented(self):
        text = self.write()
        self.assertEqual(_assignments(text, "PROJECT_NAME"), [])
        self.assertNotIn("<project>", text)
        self.assertNotIn("Project_KnowledgeGraph", text)

    def test_known_project_name_is_rendered(self):
        install.os.environ["PROJECT_NAME"] = "Orch"
        text = self.write()
        self.assertEqual(_assignments(text, "PROJECT_NAME"), ["Orch"])
        self.assertEqual(_assignments(text, "CODE_GRAPH_PROJECT"), ["Orch"])

    def test_openai_key_path_keeps_its_lines(self):
        text = self.write(openai_key="sk-test-not-real")
        self.assertEqual(_assignments(text, "EMBEDDING_PROVIDER"), ["openai"])
        # v0.2.97: the key itself is stored, never written here.
        self.assertIn("# OpenAI embeddings — the API key is in the launcher keychain /\n", text)
        self.assertEqual(_assignments(text, "OPENAI_API_KEY"), [])
        self.assertNotIn("sk-test-not-real", text)

    def test_tail_matches_the_renderer_byte_for_byte(self):
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        text = self.write()
        expected_tail = render_install_env_tail(
            cfg,
            weaviate_grpc_port=str(install.DEFAULT_WEAVIATE_GRPC_PORT),
            code_embed_port=str(install.DEFAULT_CODE_EMBED_PORT),
            telemetry_enabled=False,
            concurrency_lines=install._code_embed_max_concurrent_env_lines(cfg),
        )
        self.assertTrue(text.startswith(expected_tail + ENV_TEMPLATE_BEGIN))


class TestExistingFile(_InstallRoot):

    def test_rerun_is_byte_identical_and_does_not_write(self):
        first = self.write()
        env = self.root / ".env"
        mtime = env.stat().st_mtime_ns
        second = self.write()
        self.assertEqual(first, second)
        self.assertEqual(env.stat().st_mtime_ns, mtime)

    def test_reinstall_never_changes_a_block_value(self):
        """Leave-alone: a re-install resolving a different collection name
        does not rewrite the value the first install settled."""
        self.write()
        install.os.environ["KG_COLLECTION"] = "Beta_KnowledgeGraph"
        text = self.write()
        self.assertEqual(_assignments(text, "KG_COLLECTION"), ["Gamma_KnowledgeGraph"])

    def test_reinstall_over_a_pre_v0297_file_adds_only_missing_keys(self):
        """Act: a pre-v0.2.97 install wrote every key as a plain line; the
        re-install adds ONLY the keys it lacks, in a block, and changes no
        line of the user's file."""
        legacy = (
            "# VibeCoded Tools — Orchestrator Configuration\n"
            "WEAVIATE_URL=http://localhost:8081\n"
            "WEAVIATE_PORT=8081\n"
            "KG_COLLECTION=Mine_KnowledgeGraph\n"
            "EMBEDDING_MODEL=user-choice\n"
        )
        (self.root / ".env").write_text(legacy, encoding="utf-8")
        text = self.write()
        self.assertTrue(text.startswith(legacy))
        self.assertEqual(_assignments(text, "KG_COLLECTION"), ["Mine_KnowledgeGraph"])
        self.assertEqual(_assignments(text, "EMBEDDING_MODEL"), ["user-choice"])
        block = _block(text)
        self.assertNotIn("\nKG_COLLECTION=", block)
        self.assertIn("\nOLLAMA_URL=", block)
        # The install-time tail is a NEW-file scaffold only.
        self.assertEqual(_assignments(text, "WEAVIATE_GRPC_PORT"), [])


class TestUpdateFoldsLegacyLines(_InstallRoot):
    """``--update`` over an orchestrator ``.env`` carrying every retired
    writer's lines (the field shape): each managed key ends up assigned
    ONCE, a migrated line's value is kept (fill-only), the unsubstituted
    ``PROJECT_NAME=<project>`` is gone, user lines are byte-identical."""

    FIELD = (
        "# VibeCoded Tools — Orchestrator Configuration\n"
        "KG_COLLECTION=Gamma_KnowledgeGraph\n"
        "EMBEDDING_PROVIDER=ollama\n"
        "\n"
        "\n"
        "# added by vco 2026-05-06: appended missing canonical keys\n"
        "# CODE_EMBED_URL=\n"
        "PROJECT_NAME=<project>\n"
        "# ANTHROPIC_API_KEY=\n"
        "# GITHUB_TOKEN=\n"
        "\n"
        "# --- Added by install.py --update on 2026-05-28 ---\n"
        "# Added by install.py --update on 2026-05-28\n"
        "CODE_EMBED_PORT=19999\n"
        "\n"
        "# --- Added by install.py --update on 2026-06-04 ---\n"
        "# Added by install.py --update on 2026-06-04\n"
        "SHARED_KG_READ_DISABLED=true\n"
    )

    def test_one_set_values_kept_placeholder_gone(self):
        env = self.root / ".env"
        env.write_text(self.FIELD, encoding="utf-8")

        result = install._reconcile_env_keys(env)
        text = env.read_text(encoding="utf-8")

        self.assertTrue(text.startswith(
            "# VibeCoded Tools — Orchestrator Configuration\n"
            "KG_COLLECTION=Gamma_KnowledgeGraph\n"
            "EMBEDDING_PROVIDER=ollama\n"
        ))
        self.assertEqual(_assignments(text, "CODE_EMBED_PORT"), ["19999"])
        self.assertEqual(_assignments(text, "SHARED_KG_READ_DISABLED"), ["true"])
        self.assertEqual(_assignments(text, "KG_COLLECTION"), ["Gamma_KnowledgeGraph"])
        self.assertEqual(_assignments(text, "PROJECT_NAME"), [])
        self.assertNotIn("<project>\n", text)
        self.assertNotIn("Added by install.py --update", text)
        self.assertIn("# ANTHROPIC_API_KEY=\n# GITHUB_TOKEN=\n", text)
        self.assertEqual(text.count(ENV_TEMPLATE_BEGIN), 1)
        self.assertEqual(result["action"], "appended")

        # Idempotent: the next --update finds nothing to add or move.
        again = install._reconcile_env_keys(env)
        self.assertEqual(again, {"added": [], "action": "noop"})
        self.assertEqual(env.read_text(encoding="utf-8"), text)


class TestBuilder(unittest.TestCase):

    def test_reads_environ_and_defaults(self):
        keys = orchestrator_env_template_keys(
            {"WEAVIATE_PORT": "9999"},
            default_weaviate_port=8081,
            default_ollama_port=11435,
            default_code_embed_port=11440,
        )
        self.assertEqual(keys["WEAVIATE_URL"], "http://localhost:9999")
        self.assertEqual(keys["KG_COLLECTION"], "KnowledgeGraph")
        self.assertEqual(keys["ACTIVE_EMBEDDING"], "qwen3")
        self.assertNotIn("PROJECT_NAME", keys)


if __name__ == "__main__":
    unittest.main()
