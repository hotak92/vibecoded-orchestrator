"""v0.2.102 lane W5-D — the launcher's KG-summary Preferences keys are READ.

The launcher Preferences page → KG Summaries has written two app_state keys
since v0.2.23 that no Python read (a declared-but-unread configuration):

* ``kg_summary_ollama_model`` — the local model pick ("Auto-select" = empty).
  Now rung 2 of :mod:`vco_lib.summary_model` (pinned in
  ``tests/test_inference_pull_list_matches_summary_backend.py``).
* ``kg_summary_backend_override`` — the backend pin. Now read by
  ``summary_backends._forced_backend`` after the env keys (pinned here).
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(REPO_ROOT / "templates" / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    for key in ("KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    mod = importlib.import_module("summary_backends")
    mod.reset_backend_cache()
    yield mod
    mod.reset_backend_cache()


def _pin(tmp_path: Path, value: str) -> None:
    conn = sqlite3.connect(tmp_path / "launcher.db")
    conn.execute("CREATE TABLE IF NOT EXISTS app_state "
                 "(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?, 0)",
                 ("kg_summary_backend_override", value))
    conn.commit()
    conn.close()


def test_key_matches_the_launcher(sb):
    loaders = (REPO_ROOT / "launcher" / "src" / "lib" / "preferences" / "loaders.ts"
               ).read_text(encoding="utf-8")
    assert f"'{sb.APP_STATE_KEY_BACKEND_OVERRIDE}'" in loaders


def test_preferences_pin_forces_the_backend(sb, tmp_path):
    _pin(tmp_path, "skip")
    assert sb._forced_backend(("KG_SUMMARY_BACKEND",)) == "skip"
    assert sb.select_backend() == "skip"


def test_auto_select_is_no_pin(sb, tmp_path):
    _pin(tmp_path, "")
    assert sb._forced_backend(("KG_SUMMARY_BACKEND",)) == ""


def test_env_wins_over_the_pin(sb, tmp_path, monkeypatch):
    _pin(tmp_path, "skip")
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "ollama")
    assert sb._forced_backend_with_source(("KG_SUMMARY_BACKEND",)) == ("ollama", "env")


def test_code_generator_alias_order(sb, tmp_path, monkeypatch):
    _pin(tmp_path, "skip")
    keys = ("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND")
    assert sb._forced_backend_with_source(keys) == ("skip", "Preferences")
    monkeypatch.setenv("CODE_SUMMARY_BACKEND", "cli")
    assert sb._forced_backend_with_source(keys) == ("cli", "env")


def test_openai_pin_still_needs_consent(sb, tmp_path):
    _pin(tmp_path, "openai")
    lines: list = []
    sb.set_logger(lines.append)
    try:
        assert sb.select_backend() == "skip", "a pin never bypasses the OpenAI consent gate"
    finally:
        sb.set_logger(print)
    msg = " ".join(lines)
    assert "Preferences backend pin is openai" in msg, msg
    assert "KG_SUMMARY_BACKEND=openai" not in msg, "the skip names the real source"


def test_env_openai_skip_names_the_env_var(sb, monkeypatch):
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
    lines: list = []
    sb.set_logger(lines.append)
    try:
        assert sb.select_backend() == "skip"
    finally:
        sb.set_logger(print)
    assert "KG_SUMMARY_BACKEND=openai" in " ".join(lines)
