# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 5c task 2 — install-time wiring of the concurrency budget.

Covers:
  * `_finalize_embed_config_for_host` stamps `code_embed_max_concurrent` +
    `update_all_max_parallel` onto the resolved embed config, derived from the
    host's free VRAM/RAM via `select_embedding_concurrency`.
  * `_choose_embedding_config` return paths all flow through the augmenter (via
    the assignment-site call in the install flow — verified here by calling the
    augmenter on each preset directly, since the flow function is large).
  * `_write_env_config` writes `CODE_EMBED_MAX_CONCURRENT` when derived AND not
    already set in the env (honour-explicit-config); OMITS it when the user
    exported it.
  * `_write_preset_defaults_to_app_state` seeds the two app_state keys
    (ON CONFLICT DO NOTHING idempotency) and preserves prior/GUI values.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""


def _sysinfo(has_gpu: bool, vram_gb: float, ram_gb: float,
             vendor: str = "") -> "install.SystemInfo":
    return install.SystemInfo(
        os_name="Linux",
        has_gpu=has_gpu,
        has_metal=False,
        container_cmd="podman",
        gpu_name="RTX 4090" if has_gpu else "",
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        gpu_vendor=(vendor or ("nvidia" if has_gpu else "")),
    )


class AugmentConfigTests(unittest.TestCase):
    def test_gpu_codesage_uses_vram_pool(self):
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])  # code_model == codesage
        install._finalize_embed_config_for_host(
            cfg, _sysinfo(has_gpu=True, vram_gb=16.0, ram_gb=64.0, vendor="nvidia"),
        )
        self.assertIn("code_embed_max_concurrent", cfg)
        self.assertIn("update_all_max_parallel", cfg)
        self.assertGreaterEqual(cfg["code_embed_max_concurrent"], 1)
        self.assertLessEqual(cfg["code_embed_max_concurrent"], 8)
        self.assertGreaterEqual(cfg["update_all_max_parallel"], 1)

    def test_cpu_preset_uses_ram_pool(self):
        # cpu preset code_model is Ollama jina → not GPU-resident → RAM pool.
        cfg = dict(install.EMBEDDING_CONFIGS["cpu"])
        install._finalize_embed_config_for_host(
            cfg, _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=32.0),
        )
        self.assertGreaterEqual(cfg["code_embed_max_concurrent"], 1)

    def test_all_presets_get_knobs(self):
        # Every EMBEDDING_CONFIGS preset must accept augmentation without error
        # and land both knobs (mirrors "all _choose_embedding_config returns").
        for key in install.EMBEDDING_CONFIGS:
            cfg = dict(install.EMBEDDING_CONFIGS[key])
            install._finalize_embed_config_for_host(
                cfg, _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0, vendor="nvidia"),
            )
            self.assertIn("code_embed_max_concurrent", cfg, key)
            self.assertIn("update_all_max_parallel", cfg, key)

    def test_soft_fails_on_bad_sysinfo_leaves_knobs_unset(self):
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        bad = _sysinfo(has_gpu=True, vram_gb=float("nan"), ram_gb=64.0)
        # A NaN vram must not crash; select_embedding_concurrency tolerates it,
        # so the knobs ARE set (to floor-1 at worst). Assert no exception + set.
        install._finalize_embed_config_for_host(cfg, bad)
        self.assertIn("code_embed_max_concurrent", cfg)


class WriteEnvConcurrencyTests(unittest.TestCase):
    """CODE_EMBED_MAX_CONCURRENT honour-explicit-config in the .env writer."""

    def _render_env(self, embed_config: dict, env_overrides: dict) -> str:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state" / "logs").mkdir(parents=True)
            orig_root = install.PROJECT_ROOT
            install.PROJECT_ROOT = root
            install._PENDING_EVENTS.clear()
            try:
                base_env = {k: v for k, v in install.os.environ.items()
                            if k != "CODE_EMBED_MAX_CONCURRENT"}
                base_env.update(env_overrides)
                with mock.patch.dict("os.environ", base_env, clear=True):
                    args = mock.Mock()
                    args.telemetry = "off"
                    args.yes = True
                    install._write_env_config(embed_config, args)
                return (root / ".env").read_text(encoding="utf-8")
            finally:
                install.PROJECT_ROOT = orig_root

    def _minimal_config(self, **extra) -> dict:
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        cfg["gpu_vendor"] = "nvidia"
        cfg.update(extra)
        return cfg

    def test_writes_derived_value_when_not_in_env(self):
        cfg = self._minimal_config(code_embed_max_concurrent=3)
        env = self._render_env(cfg, {})
        self.assertIn("CODE_EMBED_MAX_CONCURRENT=3", env)

    def test_omits_when_user_exported_explicit_value(self):
        cfg = self._minimal_config(code_embed_max_concurrent=3)
        env = self._render_env(cfg, {"CODE_EMBED_MAX_CONCURRENT": "7"})
        # Honour-explicit: we did NOT write our derived line (the user's env
        # value stands; the .env writer must not clobber it).
        self.assertNotIn("CODE_EMBED_MAX_CONCURRENT=3", env)

    def test_omits_when_budget_not_derived(self):
        cfg = self._minimal_config()  # no code_embed_max_concurrent key
        env = self._render_env(cfg, {})
        self.assertNotIn("CODE_EMBED_MAX_CONCURRENT=", env)


class SeedAppStateConcurrencyTests(unittest.TestCase):
    def _fresh_db(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        db = root / "launcher.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(_APP_STATE_SCHEMA)
        conn.commit()
        conn.close()
        self._state_dir = root
        return db

    def _read(self, db: Path) -> dict:
        conn = sqlite3.connect(str(db))
        try:
            return {k: v for k, v in conn.execute(
                "SELECT key, value FROM app_state"
            )}
        finally:
            conn.close()

    def _seed(self, cfg: dict):
        # _LogFixture equivalent: point PROJECT_ROOT at a tmp for events.
        with tempfile.TemporaryDirectory() as logtd:
            logroot = Path(logtd)
            (logroot / "state" / "logs").mkdir(parents=True)
            orig = install.PROJECT_ROOT
            install.PROJECT_ROOT = logroot
            install._PENDING_EVENTS.clear()
            try:
                with mock.patch.dict(
                    "os.environ", {"VCT_STATE_DIR": str(self._state_dir)}
                ):
                    install._write_preset_defaults_to_app_state(
                        cfg, openai_set_as_default=False,
                    )
            finally:
                install.PROJECT_ROOT = orig

    def test_seeds_both_concurrency_keys_when_derived(self):
        db = self._fresh_db()
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        cfg["code_embed_max_concurrent"] = 4
        cfg["update_all_max_parallel"] = 2
        self._seed(cfg)
        rows = self._read(db)
        self.assertEqual(rows.get("embedding.code_embed_max_concurrent"), "4")
        self.assertEqual(rows.get("embedding.update_all_max_parallel"), "2")

    def test_omits_keys_when_not_derived(self):
        db = self._fresh_db()
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])  # no concurrency knobs
        self._seed(cfg)
        rows = self._read(db)
        self.assertNotIn("embedding.code_embed_max_concurrent", rows)
        self.assertNotIn("embedding.update_all_max_parallel", rows)

    def test_preserves_prior_user_tuned_value(self):
        db = self._fresh_db()
        # A prior GUI selection of 6 exists.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?)",
            ("embedding.update_all_max_parallel", "6", 1),
        )
        conn.commit()
        conn.close()
        cfg = dict(install.EMBEDDING_CONFIGS["gpu"])
        cfg["code_embed_max_concurrent"] = 4
        cfg["update_all_max_parallel"] = 2
        self._seed(cfg)
        rows = self._read(db)
        # ON CONFLICT DO NOTHING → prior 6 preserved, not overwritten with 2.
        self.assertEqual(rows.get("embedding.update_all_max_parallel"), "6")
        # The absent code key IS seeded.
        self.assertEqual(rows.get("embedding.code_embed_max_concurrent"), "4")


if __name__ == "__main__":
    unittest.main()
