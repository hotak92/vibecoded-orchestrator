# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install.py's ``_ensure_env_template`` POST-Phase-0.D.

PHASE 0.D MIGRATION (2026-05-24): ``_ensure_env_template`` used to
implement append-only "# added by vco YYYY-MM-DD" semantics in-line.
After Phase 0.D it delegates to
:func:`vco_lib.env_template.apply_env_template` — the single-writer
contract for the per-project ``.env`` surface.

What this test file pins after the migration:
  * ``_ensure_env_template`` creates a ``.env`` with the new BEGIN/END
    managed block when none exists.
  * On a pre-existing ``.env`` (with or without the BEGIN marker), the
    managed block is in-place-replaced / appended atomically.
  * Idempotent: running twice produces byte-identical output.
  * User content OUTSIDE the BEGIN/END markers is preserved verbatim.
  * The legacy "user value never overwritten" semantic no longer holds
    for canonical keys INSIDE the managed block (they are now launcher-
    resolved); user overrides must live OUTSIDE the markers (where they
    win under shell-source last-wins rules).

The deep coverage of the writer contract lives in
``tests/test_env_template.py`` and
``tests/test_env_template_byte_identical.py``; this file only covers
the install.py adapter behaviour.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402

from vco_lib.env_template import (
    ENV_TEMPLATE_BEGIN,
    ENV_TEMPLATE_END,
    list_canonical_env_template_keys,
)


# Canonical Phase 0.D template keys that MUST appear in a fresh .env
# written by _ensure_env_template. Strict subset of the install.py
# legacy "all canonical" set; the EXCLUDE-by-design set (API keys, RL
# section, telemetry) no longer lives inside the managed block.
PHASE_0D_KEYS = list_canonical_env_template_keys()


class TestEnsureEnvTemplateFreshCreate(unittest.TestCase):
    """Branch: ``.env`` does not exist → create with managed block."""

    def test_creates_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            self.assertFalse(env.exists())
            report = install._ensure_env_template(env, project_name="Acme")
            self.assertEqual(report["action"], "created")
            self.assertTrue(env.exists())

    def test_created_file_has_phase_0d_managed_block(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="Acme")
            text = env.read_text(encoding="utf-8")
            # The Phase 0.D BEGIN/END markers must appear.
            self.assertIn(ENV_TEMPLATE_BEGIN, text)
            self.assertIn(ENV_TEMPLATE_END, text)
            # Every Phase 0.D canonical key appears as KEY=VALUE inside
            # the managed block.
            begin_idx = text.find(ENV_TEMPLATE_BEGIN)
            end_idx = text.find(ENV_TEMPLATE_END)
            block = text[begin_idx:end_idx]
            for key in PHASE_0D_KEYS:
                self.assertIn(
                    f"{key}=", block, f"missing canonical key {key} in managed block"
                )

    def test_created_file_uses_project_name_in_collection_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="Acme")
            text = env.read_text(encoding="utf-8")
            self.assertIn("KG_COLLECTION=Acme_KnowledgeGraph", text)
            self.assertIn("DEVELOPMENT_COLLECTION=Acme_Development", text)
            self.assertIn("PROJECT_NAME=Acme", text)

    def test_optional_keys_NOT_in_managed_block(self):
        """API keys / RL section / telemetry are EXCLUDED from the Phase
        0.D managed-block subset by design (see env_template module
        docstring). They may still be written elsewhere in the .env
        (install.py's _write_env_config writes them OUTSIDE the managed
        block) but `_ensure_env_template` no longer puts them inside."""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            begin_idx = text.find(ENV_TEMPLATE_BEGIN)
            end_idx = text.find(ENV_TEMPLATE_END)
            block = text[begin_idx:end_idx]
            for excluded in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "GITHUB_TOKEN",
                "RL_SERVER_URL",
                "VCT_TELEMETRY",
            ):
                self.assertNotIn(
                    f"{excluded}=",
                    block,
                    f"{excluded} leaked into the managed block (Phase 0.D subset violation)",
                )


class TestEnsureEnvTemplateExistingFile(unittest.TestCase):
    """Branch: ``.env`` exists → managed-block in-place replace / append."""

    def test_user_content_outside_markers_preserved(self):
        """The new contract preserves OUTSIDE-marker content verbatim.
        User-set values for canonical keys outside the markers stay put
        and (under shell-source last-wins semantics) take effect."""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "# my custom header\n"
                "OPENAI_API_KEY=sk-user-set-this-themselves\n"
                "KG_COLLECTION=MyCustomOverride_KG\n",
                encoding="utf-8",
            )
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            # User lines preserved.
            self.assertIn("OPENAI_API_KEY=sk-user-set-this-themselves", text)
            self.assertIn("KG_COLLECTION=MyCustomOverride_KG", text)
            # Custom header preserved.
            self.assertIn("# my custom header", text)
            # Phase 0.D managed block appended.
            self.assertIn(ENV_TEMPLATE_BEGIN, text)

    def test_idempotent_double_run_byte_identical(self):
        """Two runs against the same starting state produce identical
        bytes (modulo the first run creating the managed block)."""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            install._ensure_env_template(env, project_name="X")
            text_after_first = env.read_text(encoding="utf-8")
            install._ensure_env_template(env, project_name="X")
            text_after_second = env.read_text(encoding="utf-8")
            self.assertEqual(text_after_first, text_after_second)

    def test_legacy_env_with_added_by_vco_lines_preserved(self):
        """Legacy ``.env`` files written by pre-Phase-0.D vco carry
        ``# added by vco YYYY-MM-DD`` lines but no BEGIN/END marker.
        Phase 0.D appends a fresh managed block at EOF; legacy lines
        stay in place (they sit outside the markers)."""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "# vibecoded-orchestrator per-project .env (legacy format)\n"
                "OPENAI_API_KEY=sk-x\n"
                "\n"
                "# added by vco 2026-04-28: appended missing canonical keys\n"
                "# GITHUB_TOKEN=\n",
                encoding="utf-8",
            )
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            # Legacy header / user lines / # added by vco annotation preserved.
            self.assertIn("# vibecoded-orchestrator per-project .env (legacy format)", text)
            self.assertIn("OPENAI_API_KEY=sk-x", text)
            self.assertIn("# added by vco 2026-04-28", text)
            # New Phase 0.D managed block appended at EOF.
            self.assertIn(ENV_TEMPLATE_BEGIN, text)

    def test_no_trailing_newline_is_handled(self):
        """A .env without trailing newline gets a separator before the
        appended managed block — no glueing onto the user's last line."""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_bytes(b"FOO=bar")  # no \n
            install._ensure_env_template(env, project_name="X")
            text = env.read_text(encoding="utf-8")
            self.assertIn("FOO=bar\n", text)
            # The BEGIN marker is on its own line, not glued.
            self.assertNotIn(f"FOO=bar{ENV_TEMPLATE_BEGIN}", text)

    def test_managed_block_re_renders_canonical_values(self):
        """Values INSIDE the managed block reflect the latest install.py-
        resolved canonical state on every run. Pre-Phase-0.D the
        function preserved any active KEY=VALUE the user had; post-
        Phase-0.D the managed-block canonical values ALWAYS win because
        the block is wholesale-replaced. (User overrides must live
        outside the markers.)"""
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            # First run as project "Acme".
            install._ensure_env_template(env, project_name="Acme")
            text1 = env.read_text(encoding="utf-8")
            self.assertIn("PROJECT_NAME=Acme", text1)
            # Second run with a different project_name — the managed
            # block is rewritten.
            install._ensure_env_template(env, project_name="Beta")
            text2 = env.read_text(encoding="utf-8")
            # Inside the managed block, PROJECT_NAME reflects the new value.
            begin_idx = text2.find(ENV_TEMPLATE_BEGIN)
            end_idx = text2.find(ENV_TEMPLATE_END)
            block = text2[begin_idx:end_idx]
            self.assertIn("PROJECT_NAME=Beta", block)
            self.assertNotIn("PROJECT_NAME=Acme", block)


class TestEnsureEnvTemplateReportShape(unittest.TestCase):
    """The report dict shape is preserved across the Phase 0.D migration
    so existing callers (install.py Step 9, future Rust subprocess
    callers reading stdout) keep working."""

    def test_report_has_required_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            report = install._ensure_env_template(env, project_name="X")
            self.assertIn("action", report)
            self.assertIn("added_keys", report)
            self.assertIn("env_path", report)
            self.assertEqual(report["env_path"], str(env))

    def test_action_is_created_or_appended(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            r1 = install._ensure_env_template(env, project_name="X")
            self.assertIn(r1["action"], ("created", "appended"))
            r2 = install._ensure_env_template(env, project_name="X")
            # Second run on an existing file always returns "appended"
            # (the action label after migration reflects whether the
            # file pre-existed, not whether content changed).
            self.assertEqual(r2["action"], "appended")


if __name__ == "__main__":
    unittest.main()
