# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the per-project `.env` canonical-template helpers in install.py.

Covers Deliverable 1 from launch-blocker spec 2026-04-28:
  * `_ensure_env_template` creates a fresh `.env` from the canonical
    template when none exists, with all expected keys.
  * On a pre-existing `.env`, missing canonical keys are appended
    commented-out, tagged with the `# added by vco YYYY-MM-DD` marker.
  * User-set values are preserved verbatim — never overwritten.
  * Idempotent: running `_ensure_env_template` twice in a row produces
    no diff after the first invocation.
  * Marker-detection: a key already declared (commented OR active) is
    treated as present and not re-appended.
  * Trailing newline: append-merge doesn't glue header onto last line.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# Canonical keys we expect every install to carry. Mirrors the Rust
# `env_canonical_keys()` list — the integration test
# `env_template_canonical_keys_match_python` enforces parity. If you
# change this list, also update commands/projects_v2.rs.
CANONICAL_KEYS = {
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "KG_COLLECTION",
    "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "PROJECT_NAME",
    "CONVERSATION_COLLECTION",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "RL_SERVER_URL",
    "RL_SERVER_PORT",
    "RL_PROJECT_ROOT",
    "VCT_TELEMETRY",
}


class TestEnvTemplateFreshCreate(unittest.TestCase):
    """Branch: `.env` does not exist → create from template."""

    def test_creates_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            self.assertFalse(env.exists())
            report = install._ensure_env_template(env, project_name="Agape")
            self.assertEqual(report["action"], "created")
            self.assertTrue(env.exists())

    def test_created_file_has_all_canonical_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="Agape")
            present = install._parse_existing_env_keys(env)
            for key in CANONICAL_KEYS:
                self.assertIn(key, present, f"missing {key} in fresh template")

    def test_created_file_uses_project_name_in_collection_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="Agape")
            text = env.read_text(encoding="utf-8")
            self.assertIn("KG_COLLECTION=Agape_KnowledgeGraph", text)
            self.assertIn("DEVELOPMENT_COLLECTION=Agape_Development", text)
            self.assertIn("PROJECT_NAME=Agape", text)

    def test_created_file_keeps_optional_keys_commented(self):
        # Sensitive / optional keys must NOT be active by default.
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            self.assertIn("# ANTHROPIC_API_KEY=", text)
            self.assertIn("# OPENAI_API_KEY=", text)
            self.assertIn("# GITHUB_TOKEN=", text)
            self.assertIn("# VCT_TELEMETRY=off", text)
            # And the un-commented form must NOT appear.
            self.assertNotIn("\nANTHROPIC_API_KEY=", text)
            self.assertNotIn("\nGITHUB_TOKEN=", text)


class TestEnvTemplateAppendMerge(unittest.TestCase):
    """Branch: `.env` exists → append missing canonical keys only."""

    def test_user_value_preserved_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("OPENAI_API_KEY=sk-user-set-this-themselves\n",
                           encoding="utf-8")
            report = install._ensure_env_template(env, project_name="X")
            self.assertEqual(report["action"], "appended")
            text = env.read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=sk-user-set-this-themselves", text)
            # User-line must still be FIRST in the file (not duplicated
            # nor reordered).
            first_line = text.splitlines()[0]
            self.assertEqual(first_line, "OPENAI_API_KEY=sk-user-set-this-themselves")

    def test_missing_keys_appended_with_marker(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("OPENAI_API_KEY=sk-x\n", encoding="utf-8")
            report = install._ensure_env_template(env, project_name="X")
            self.assertEqual(report["action"], "appended")
            text = env.read_text(encoding="utf-8")
            self.assertIn(install.ENV_VCO_MARKER, text)
            # Marker line must include today's date in YYYY-MM-DD form.
            marker_lines = [l for l in text.splitlines()
                            if install.ENV_VCO_MARKER in l]
            self.assertTrue(any(":" in l for l in marker_lines),
                            f"marker line should include date: {marker_lines}")

    def test_already_commented_key_is_not_reappended(self):
        # If the user has `# ANTHROPIC_API_KEY=` in their file (the
        # canonical commented form), it should be recognised as
        # present — appending would create a duplicate.
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "# my custom comment\n"
                "# ANTHROPIC_API_KEY=\n"
                "GITHUB_TOKEN=ghp_user_set\n",
                encoding="utf-8",
            )
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            # ANTHROPIC_API_KEY appears exactly once.
            count = text.count("ANTHROPIC_API_KEY")
            self.assertEqual(count, 1, f"expected 1 occurrence, got {count}\n{text}")

    def test_idempotent_double_run_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="X")
            text_after_first = env.read_text(encoding="utf-8")
            report = install._ensure_env_template(env, project_name="X")
            text_after_second = env.read_text(encoding="utf-8")
            self.assertEqual(report["action"], "noop")
            self.assertEqual(text_after_first, text_after_second)

    def test_no_trailing_newline_is_handled(self):
        # File without trailing newline: header must not glue onto
        # the user's last line.
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("FOO=bar", encoding="utf-8")  # no \n
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            self.assertIn("FOO=bar\n", text)
            # Marker line must be on its own line, not appended to FOO=bar.
            for line in text.splitlines():
                if install.ENV_VCO_MARKER in line:
                    self.assertFalse(line.startswith("FOO=bar"),
                                     f"header glued to user line: {line!r}")

    def test_partial_canonical_keys_only_append_missing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "KG_COLLECTION=MyCustom_KG\n"
                "PROJECT_NAME=Custom\n",
                encoding="utf-8",
            )
            report = install._ensure_env_template(env, project_name="X")
            self.assertEqual(report["action"], "appended")
            self.assertNotIn("KG_COLLECTION", report["added_keys"])
            self.assertNotIn("PROJECT_NAME", report["added_keys"])
            # Other canonical keys SHOULD appear in the appended set.
            self.assertIn("ANTHROPIC_API_KEY", report["added_keys"])

    def test_user_value_for_kg_collection_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("KG_COLLECTION=MyCustom_KG\n", encoding="utf-8")
            install._ensure_env_template(env, project_name="Agape")
            text = env.read_text(encoding="utf-8")
            self.assertIn("KG_COLLECTION=MyCustom_KG", text)
            self.assertNotIn("KG_COLLECTION=Agape_KnowledgeGraph", text)


class TestEnvTemplateNoop(unittest.TestCase):
    """Branch: `.env` exists with all keys → noop."""

    def test_complete_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="X")  # create
            mtime_before = env.stat().st_mtime
            # Re-run a hair later — but mtime resolution may be 1s, so
            # also assert via report dict.
            report = install._ensure_env_template(env, project_name="X")
            self.assertEqual(report["action"], "noop")
            self.assertEqual(report["added_keys"], [])

    def test_parse_keys_handles_blank_and_comment_only_lines(self):
        text = "\n\n# pure prose comment\n\n# Another: with a colon\nFOO=bar\n"
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(text, encoding="utf-8")
            keys = install._parse_existing_env_keys(env)
            self.assertEqual(keys, {"FOO"})


if __name__ == "__main__":
    unittest.main()
