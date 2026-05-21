# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 C10 — KG-summary OpenAI consent gate.

The `templates/scripts/generate-kg-summary.py` script reads the
`kg_summary_openai_consent` app_state row from the launcher SQLite DB
before allowing the OpenAI tier to be selected. This test exercises
the gate in isolation:

  - consent=false AND KG_SUMMARY_BACKEND=openai → script logs a clear
    message and selects 'skip' (NOT 'openai').
  - consent=true → openai is selectable (assuming key is present).
  - --force-api flag bypasses the gate entirely.
  - Missing launcher.db / missing app_state table / missing row are
    all treated as consent=false (the safe default).

We import the script directly via importlib because it lives under
`templates/scripts/` (not on the default sys.path) — and we exercise
its module-level helpers in isolation, not by running it as a
subprocess. That keeps the test fast and lets us inject env / DB
state surgically.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


# Resolve the script path relative to the repo root. Each test loads
# the module fresh (via importlib) so module-level state (env reads,
# _BACKEND_CACHE) doesn't bleed across cases.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"


def _load_script_module(name: str = "kg_summary_under_test") -> Any:
    """Import generate-kg-summary.py as a module under a unique name.

    Unique name → no module-level state leak across tests. We delete
    the cached entry before re-importing to be defensive.
    """
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_app_state_db(
    db_path: Path,
    *,
    consent: "bool | None" = None,
    model: "str | None" = None,
) -> None:
    """Create launcher.db with the canonical app_state schema and seed it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_state ("
            "key TEXT PRIMARY KEY, "
            "value TEXT, "
            "updated_at INTEGER NOT NULL DEFAULT 0)"
        )
        if consent is not None:
            conn.execute(
                "INSERT OR REPLACE INTO app_state(key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("kg_summary_openai_consent", "true" if consent else "false", 0),
            )
        if model is not None:
            conn.execute(
                "INSERT OR REPLACE INTO app_state(key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("kg_summary_openai_model", model, 0),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """VCT_STATE_DIR pointing at an empty tmp dir.

    The script reads launcher.db relative to VCT_STATE_DIR (or
    ~/.vct/launcher.db otherwise). Pinning VCT_STATE_DIR for the test
    isolates each case + lets us seed app_state surgically.
    """
    state_dir = tmp_path / "vct-state"
    state_dir.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    # Clean any forced backend env var that might be picked up.
    monkeypatch.delenv("KG_SUMMARY_BACKEND", raising=False)
    return state_dir


# ────────────────────────────────────────────────────────────────────
# Consent gate — DB row variants
# ────────────────────────────────────────────────────────────────────

class TestConsentRowReading:
    def test_missing_db_treated_as_no_consent(
        self, isolated_state: Path,
    ) -> None:
        """No launcher.db at all → consent=False."""
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_missing_row_treated_as_no_consent(
        self, isolated_state: Path,
    ) -> None:
        """DB exists, table exists, row absent → consent=False."""
        _seed_app_state_db(isolated_state / "launcher.db")
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_row_false_returns_false(self, isolated_state: Path) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=False)
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_row_true_returns_true(self, isolated_state: Path) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=True)
        mod = _load_script_module()
        assert mod.openai_consent_granted() is True

    def test_truthy_string_variants(self, isolated_state: Path) -> None:
        """The script accepts {true, 1, yes} (case-insensitive) as true."""
        for raw in ["true", "TRUE", "True", "1", "yes", "YES"]:
            db_path = isolated_state / "launcher.db"
            db_path.unlink(missing_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE app_state ("
                    "key TEXT PRIMARY KEY, value TEXT, "
                    "updated_at INTEGER NOT NULL DEFAULT 0)"
                )
                conn.execute(
                    "INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, 0)",
                    ("kg_summary_openai_consent", raw),
                )
                conn.commit()
            finally:
                conn.close()
            mod = _load_script_module(name=f"kgu_{raw}")
            assert mod.openai_consent_granted() is True, (
                f"expected {raw!r} → True"
            )

    def test_non_truthy_string_variants(self, isolated_state: Path) -> None:
        """Anything else (including empty / random) is False."""
        for raw in ["false", "0", "no", "", "maybe", "off"]:
            db_path = isolated_state / "launcher.db"
            db_path.unlink(missing_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE app_state ("
                    "key TEXT PRIMARY KEY, value TEXT, "
                    "updated_at INTEGER NOT NULL DEFAULT 0)"
                )
                conn.execute(
                    "INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, 0)",
                    ("kg_summary_openai_consent", raw),
                )
                conn.commit()
            finally:
                conn.close()
            mod = _load_script_module(name=f"kgu_neg_{raw or 'empty'}")
            assert mod.openai_consent_granted() is False, (
                f"expected {raw!r} → False"
            )


# ────────────────────────────────────────────────────────────────────
# select_backend — gate behaviour
# ────────────────────────────────────────────────────────────────────

class TestSelectBackendConsentGate:
    def test_forced_openai_without_consent_falls_through_to_skip(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KG_SUMMARY_BACKEND=openai + no consent + no --force-api → skip.

        The script must NOT honour the env-var forcing of OpenAI when
        consent has not been granted; that's the whole point of the
        gate. The user gets a clear log message and the script
        exits 0 (no error) — leaving the KG node un-summarised but
        otherwise intact.
        """
        # No DB seeding → consent is False (the safe default).
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        # Defense: ensure no force flag is leaked.
        mod._FORCE_API = False
        chosen = mod.select_backend()
        assert chosen == "skip", (
            f"expected forced openai + no consent → 'skip', got {chosen!r}"
        )

    def test_forced_openai_with_consent_picks_openai(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=True)
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        mod._FORCE_API = False
        chosen = mod.select_backend()
        assert chosen == "openai"

    def test_force_api_flag_bypasses_consent_gate(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--force-api operator override: even without consent, pick openai
        (when forced via env).
        """
        # No consent in DB.
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        mod._FORCE_API = True
        chosen = mod.select_backend()
        assert chosen == "openai", (
            f"--force-api should bypass consent gate; got {chosen!r}"
        )


# ────────────────────────────────────────────────────────────────────
# Model resolution
# ────────────────────────────────────────────────────────────────────

class TestOpenAIModelResolution:
    def test_default_model_when_unset(self, isolated_state: Path) -> None:
        """No env, no app_state row → default 'gpt-4o-mini'."""
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4o-mini"

    def test_app_state_row_overrides_default(self, isolated_state: Path) -> None:
        _seed_app_state_db(
            isolated_state / "launcher.db",
            model="gpt-4o",
        )
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4o"

    def test_env_var_overrides_app_state(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KG_SUMMARY_OPENAI_MODEL beats both stored value and default."""
        _seed_app_state_db(
            isolated_state / "launcher.db",
            model="gpt-4o",
        )
        monkeypatch.setenv("KG_SUMMARY_OPENAI_MODEL", "gpt-4.1-mini")
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4.1-mini"
