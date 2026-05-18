# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.18 Commit 10 — install.py writes preset defaults to the
launcher's `app_state` SQLite table so the GUI dropdowns pre-populate
for brand-new projects.

Covers:
  * `_preset_to_default_models` — preset/active_embedding dispatch:
      - gpu  → qwen3-embedding:0.6b + codesage-large-v2
      - cpu  → qwen3-embedding:0.6b + jina v2 code (per EMBEDDING_CONFIGS)
      - openai + opt-in → openai-text-embedding-3-small (text & code,
        prefixed form matching Wave A's openai_cmd.rs constants)
      - openai + NOT opt-in → falls back to the config's local IDs
      - low_resource ("arctic") → snowflake-arctic + jina v2 code
  * `_discover_app_state_db_path` — resolves to
        $VCT_STATE_DIR/launcher.db
        or ~/.vct/launcher.db when VCT_STATE_DIR is unset/empty
  * `_write_preset_defaults_to_app_state`:
      - skips silently when launcher.db missing (fresh first-install)
      - skips silently when app_state table missing (defense-in-depth)
      - writes both rows when DB exists + table exists + rows absent
      - does NOT overwrite existing rows (idempotency / user-preserves)
      - emits `preset_defaults` events to install.jsonl with the right
        phase (`ok` | `skip` | `warn`)

The tests use `sqlite3` directly + `tempfile.TemporaryDirectory` rather
than `:memory:` so the path-discovery + on-disk-existence checks are
exercised realistically.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# install.py lives at the repo root; tests/ is a sibling. Mirror the
# pattern in test_install_choices_replay.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""


def _create_db_with_app_state(db_path: Path) -> None:
    """Create launcher.db with the v0.2.18 app_state schema applied.
    Mirrors what the launcher's `migrations::apply` would have done on
    first boot."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_APP_STATE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _create_db_without_app_state(db_path: Path) -> None:
    """Create launcher.db with a different schema but NO app_state
    table — exercises the soft-fail path for `no such table` errors."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dummy (
                id INTEGER PRIMARY KEY
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _read_app_state(db_path: Path) -> dict[str, str]:
    """Return the current app_state contents as a {key: value} dict."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_state")
        return {k: v for k, v in cur.fetchall()}
    finally:
        conn.close()


class _LogFixture:
    """Redirect install.PROJECT_ROOT to a tempdir with state/logs/ so
    `_log_install_event` writes go somewhere we can inspect.

    Adapted from test_install_choices_replay.py — identical contract."""

    def __init__(self):
        self._tmp = None
        self._orig_root = None
        self.path = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "state" / "logs").mkdir(parents=True)
        self._orig_root = install.PROJECT_ROOT
        install.PROJECT_ROOT = root
        install._PENDING_EVENTS.clear()
        self.path = root / "state" / "logs" / "install.jsonl"
        return self

    def __exit__(self, *_):
        install.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def read_events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def events_for_step(self, step: str) -> list[dict]:
        return [e for e in self.read_events() if e.get("step") == step]


class _DbFixture:
    """Tempdir + a `VCT_STATE_DIR` env override so
    `_discover_app_state_db_path` resolves into the tempdir.

    The launcher's path resolver honours `VCT_STATE_DIR`; pointing it at
    a per-test temp directory keeps the test hermetic AND exercises the
    same code path a real dev launcher would use.
    """

    def __init__(self):
        self._tmp = None
        self.root = None
        self.db_path = None
        self._env_patch = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "launcher.db"
        self._env_patch = mock.patch.dict(
            "os.environ", {"VCT_STATE_DIR": str(self.root)}
        )
        self._env_patch.start()
        return self

    def __exit__(self, *_):
        self._env_patch.stop()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# `_preset_to_default_models` — pure-function mapping tests
# ---------------------------------------------------------------------------


class PresetToDefaultModelsTests(unittest.TestCase):
    """Mapping is pure: input dict + bool → (text, code) tuple. No I/O."""

    def test_gpu_preset_maps_to_qwen3_and_codesage(self):
        config = dict(install.EMBEDDING_CONFIGS["gpu"])
        text, code = install._preset_to_default_models(
            config, openai_set_as_default=False,
        )
        self.assertEqual(text, "qwen3-embedding:0.6b")
        self.assertEqual(code, "codesage-large-v2")

    def test_cpu_preset_maps_to_qwen3_text_and_jina_code(self):
        # CPU preset's `code_model` is jina v2 base-code in
        # EMBEDDING_CONFIGS (not qwen3 — install.py ships an Ollama
        # code-capable model rather than reusing the text model).
        config = dict(install.EMBEDDING_CONFIGS["cpu"])
        text, code = install._preset_to_default_models(
            config, openai_set_as_default=False,
        )
        self.assertEqual(text, "qwen3-embedding:0.6b")
        self.assertEqual(code, "unclemusclez/jina-embeddings-v2-base-code:latest")

    def test_openai_preset_with_opt_in_maps_to_prefixed_openai_id(self):
        # When the user explicitly opts in (`--openai-key` was passed,
        # which sets `openai_key` in the config dict), Wave A's storage
        # convention uses the `openai-` prefix.
        config = dict(install.EMBEDDING_CONFIGS["openai"])
        config["openai_key"] = "sk-test-fake-key"
        text, code = install._preset_to_default_models(
            config, openai_set_as_default=True,
        )
        self.assertEqual(text, "openai-text-embedding-3-small")
        self.assertEqual(code, "openai-text-embedding-3-small")

    def test_openai_preset_without_opt_in_falls_back_to_config_ids(self):
        # If somehow the OpenAI preset is active but the user did NOT
        # opt-in to OpenAI defaults, write the raw IDs from the config
        # (text-embedding-3-small, no `openai-` prefix). Practical case:
        # someone wired up the OpenAI preset by hand and wants to
        # populate explicit IDs only.
        config = dict(install.EMBEDDING_CONFIGS["openai"])
        text, code = install._preset_to_default_models(
            config, openai_set_as_default=False,
        )
        self.assertEqual(text, "text-embedding-3-small")
        self.assertEqual(code, "text-embedding-3-small")

    def test_low_resource_preset_maps_to_arctic_text_and_jina_code(self):
        # "low_resource" mode sets active_embedding="arctic" → snowflake
        # text + jina code (per EMBEDDING_CONFIGS).
        config = dict(install.EMBEDDING_CONFIGS["low_resource"])
        text, code = install._preset_to_default_models(
            config, openai_set_as_default=False,
        )
        self.assertEqual(text, "snowflake-arctic-embed2:latest")
        self.assertEqual(code, "unclemusclez/jina-embeddings-v2-base-code:latest")

    def test_unknown_preset_falls_back_to_qwen3_defaults(self):
        # Defense-in-depth: a config dict without text_model/code_model
        # falls back to qwen3 (the universal CPU baseline). Should
        # never hit in practice since EMBEDDING_CONFIGS always populates
        # both keys, but the helper must not crash.
        text, code = install._preset_to_default_models(
            {"active_embedding": "unknown"}, openai_set_as_default=False,
        )
        self.assertEqual(text, "qwen3-embedding:0.6b")
        self.assertEqual(code, "qwen3-embedding:0.6b")


# ---------------------------------------------------------------------------
# `_discover_app_state_db_path` — path resolution tests
# ---------------------------------------------------------------------------


class DiscoverAppStateDbPathTests(unittest.TestCase):

    def test_honours_vct_state_dir_env_override(self):
        with mock.patch.dict(
            "os.environ", {"VCT_STATE_DIR": "/tmp/vct-test-override"}
        ):
            path = install._discover_app_state_db_path()
        self.assertEqual(path, Path("/tmp/vct-test-override") / "launcher.db")

    def test_empty_vct_state_dir_falls_back_to_home_default(self):
        with mock.patch.dict("os.environ", {"VCT_STATE_DIR": ""}):
            path = install._discover_app_state_db_path()
        # Should resolve under home/.vct/, not into an empty-string path.
        self.assertTrue(
            str(path).endswith("/.vct/launcher.db")
            or str(path).endswith("\\.vct\\launcher.db"),  # Windows
            f"expected ~/.vct/launcher.db fallback, got {path}",
        )

    def test_no_env_var_resolves_under_home(self):
        # Remove the env var entirely. Path must include "launcher.db"
        # and live under home, regardless of OS.
        env_without = {
            k: v for k, v in __import__("os").environ.items()
            if k != "VCT_STATE_DIR"
        }
        with mock.patch.dict("os.environ", env_without, clear=True):
            path = install._discover_app_state_db_path()
        self.assertEqual(path.name, "launcher.db")
        # Parent directory is "<home>/.vct".
        self.assertEqual(path.parent.name, ".vct")


# ---------------------------------------------------------------------------
# `_write_preset_defaults_to_app_state` — integration with sqlite
# ---------------------------------------------------------------------------


class WritePresetDefaultsTests(unittest.TestCase):

    def test_soft_fails_when_launcher_db_missing(self):
        # Fresh-install scenario: launcher has never run, so launcher.db
        # does not exist on disk. The helper should log a `skip` event
        # and return without raising.
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            self.assertFalse(db_fix.db_path.exists())
            install._write_preset_defaults_to_app_state(
                db_fix.root,
                dict(install.EMBEDDING_CONFIGS["gpu"]),
                openai_set_as_default=False,
            )
            # File still does not exist — we did NOT create it.
            self.assertFalse(db_fix.db_path.exists())
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "skip")
            self.assertIn("launcher.db not found", events[0]["detail"])

    def test_soft_fails_when_app_state_table_missing(self):
        # File exists but no app_state table → "no such table" sqlite
        # error. Helper must catch + skip.
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            _create_db_without_app_state(db_fix.db_path)
            install._write_preset_defaults_to_app_state(
                db_fix.root,
                dict(install.EMBEDDING_CONFIGS["gpu"]),
                openai_set_as_default=False,
            )
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "skip")
            self.assertIn("app_state table not present", events[0]["detail"])

    def test_writes_both_rows_when_db_exists_and_rows_absent(self):
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            _create_db_with_app_state(db_fix.db_path)
            install._write_preset_defaults_to_app_state(
                db_fix.root,
                dict(install.EMBEDDING_CONFIGS["gpu"]),
                openai_set_as_default=False,
            )
            state = _read_app_state(db_fix.db_path)
            self.assertEqual(
                state.get("default_text_embedding"),
                "qwen3-embedding:0.6b",
            )
            self.assertEqual(
                state.get("default_code_embedding"),
                "codesage-large-v2",
            )
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "ok")
            data = events[0].get("data", {})
            self.assertTrue(data.get("text_inserted"))
            self.assertTrue(data.get("code_inserted"))

    def test_does_not_overwrite_existing_rows(self):
        # User had a prior launcher session and picked their own
        # defaults. We must NOT clobber those selections.
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            _create_db_with_app_state(db_fix.db_path)
            # Pre-populate with user's manual choices.
            conn = sqlite3.connect(str(db_fix.db_path))
            try:
                conn.execute(
                    "INSERT INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    ("default_text_embedding", "user-picked-text-model", 1),
                )
                conn.execute(
                    "INSERT INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    ("default_code_embedding", "user-picked-code-model", 1),
                )
                conn.commit()
            finally:
                conn.close()

            install._write_preset_defaults_to_app_state(
                db_fix.root,
                dict(install.EMBEDDING_CONFIGS["gpu"]),
                openai_set_as_default=False,
            )

            # User selections preserved.
            state = _read_app_state(db_fix.db_path)
            self.assertEqual(
                state.get("default_text_embedding"), "user-picked-text-model",
            )
            self.assertEqual(
                state.get("default_code_embedding"), "user-picked-code-model",
            )
            # Event still logs ok, but `*_inserted` flags are False
            # (rowcount 0 for both INSERTs).
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "ok")
            data = events[0].get("data", {})
            self.assertFalse(data.get("text_inserted"))
            self.assertFalse(data.get("code_inserted"))

    def test_writes_openai_prefixed_id_when_set_as_default(self):
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            _create_db_with_app_state(db_fix.db_path)
            config = dict(install.EMBEDDING_CONFIGS["openai"])
            config["openai_key"] = "sk-fake"
            install._write_preset_defaults_to_app_state(
                db_fix.root,
                config,
                openai_set_as_default=True,
            )
            state = _read_app_state(db_fix.db_path)
            self.assertEqual(
                state.get("default_text_embedding"),
                "openai-text-embedding-3-small",
            )
            self.assertEqual(
                state.get("default_code_embedding"),
                "openai-text-embedding-3-small",
            )
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "ok")

    def test_partial_existing_row_preserves_user_selection_and_fills_other(self):
        # User had previously set only `default_text_embedding`; the
        # `default_code_embedding` row is absent. Helper must preserve
        # the text row AND populate the missing code row.
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            _create_db_with_app_state(db_fix.db_path)
            conn = sqlite3.connect(str(db_fix.db_path))
            try:
                conn.execute(
                    "INSERT INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    ("default_text_embedding", "user-text-model", 1),
                )
                conn.commit()
            finally:
                conn.close()

            install._write_preset_defaults_to_app_state(
                db_fix.root,
                dict(install.EMBEDDING_CONFIGS["gpu"]),
                openai_set_as_default=False,
            )

            state = _read_app_state(db_fix.db_path)
            # User's text choice survives.
            self.assertEqual(
                state.get("default_text_embedding"), "user-text-model",
            )
            # Code row is filled from the preset (was absent).
            self.assertEqual(
                state.get("default_code_embedding"), "codesage-large-v2",
            )
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase"], "ok")
            data = events[0].get("data", {})
            self.assertFalse(data.get("text_inserted"))
            self.assertTrue(data.get("code_inserted"))

    def test_never_raises_on_arbitrary_sqlite_error(self):
        # Pass an embed_config with no `text_model` / `code_model`
        # keys — helper falls back to qwen3 defaults; not a crash case
        # but proves the defense-in-depth path. Then point the helper
        # at a path that exists but isn't a sqlite DB at all.
        with _LogFixture() as log_fix, _DbFixture() as db_fix:
            # Write garbage bytes to launcher.db.
            db_fix.db_path.write_bytes(b"not a sqlite file" * 100)
            # Must not raise.
            install._write_preset_defaults_to_app_state(
                db_fix.root,
                {"active_embedding": "gpu",
                 "text_model": "qwen3-embedding:0.6b",
                 "code_model": "codesage-large-v2"},
                openai_set_as_default=False,
            )
            # Either a `warn` (sqlite error during connect/query) or a
            # `skip` (corruption looks like "file is not a database").
            events = log_fix.events_for_step("preset_defaults")
            self.assertEqual(len(events), 1)
            self.assertIn(events[0]["phase"], ("warn", "skip"))


if __name__ == "__main__":
    unittest.main()
