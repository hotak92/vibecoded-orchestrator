# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-B2 (owner decision 5) — every Preferences key has a BEHAVIOURAL
reader test: set the value, observe the effect, change it, observe the change.

The census next door (``test_v02102_pref_keys_have_consumers.py``) proves a
real read SITE exists; this module proves the read DOES something. Every
``APP_STATE_KEY_*`` the Preferences page writes (``loaders.ts``) must appear in
:data:`READERS` — a key without a value-mutation test fails here.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db, set_app_state

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADERS_TS = REPO_ROOT / "launcher" / "src" / "lib" / "preferences" / "loaders.ts"
INSTALLER_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"

#: key → where its value-mutation reader test lives (this module unless named).
READERS: dict[str, str] = {
    "rl.local_logging_disabled_global": "test_rl_global_flags_project_their_values",
    "rl.online_training_disabled_global": "test_rl_global_flags_project_their_values",
    "kg_summary_openai_consent": "test_summary_consent_value_drives_the_gate",
    "kg_summary_openai_model": "test_summary_openai_model_value_is_the_model",
    "kg_summary_backend_override": "test_summary_backend_override_value_is_the_pinned_tier",
    "kg_summary_ollama_model": "test_summary_ollama_model_value_is_the_model",
    "code_embed_backend_override": "tests/test_v02102_code_embed_preferences_readers.py::TestTarget::test_pins",
    "code_embed_openai_model": "tests/test_v02102_code_embed_preferences_readers.py::TestTarget::test_ollama_and_openai_pins_use_their_model_rows",
    "code_embed_ollama_model": "tests/test_v02102_code_embed_preferences_readers.py::TestTarget::test_ollama_and_openai_pins_use_their_model_rows",
    "openai_embeddings_consent": "test_embeddings_consent_value_drives_the_gate",
    "embedding.active_profile": "test_active_profile_value_drives_the_text_model",
    "launcher.hardware_snapshot": "rust:hardware_snapshot_key_value_drives_the_read_gpu_mode",
    # v0.2.104 API-1: background ``claude -p`` jobs' subscription -> API-key
    # retry; the row ON makes a quota refusal retry with the key, OFF does not.
    "model_gateway.api_key_fallback": "tests/test_v02104_claude_cli_api_key_fallback.py::TestSummaryCliFallback::test_the_launcher_row_drives_the_retry",
}


def _pref_keys() -> set[str]:
    keys = set(re.findall(r"export const APP_STATE_KEY_\w+ = '([^']+)'",
                          LOADERS_TS.read_text(encoding="utf-8")))
    assert keys, "no APP_STATE_KEY_* parsed from loaders.ts"
    return keys


def test_every_preferences_key_has_a_behavioural_reader_test():
    missing = sorted(_pref_keys() - set(READERS))
    assert not missing, f"Preferences keys without a value-mutation reader test: {missing}"
    here = Path(__file__).read_text(encoding="utf-8")
    for key, where in READERS.items():
        if where.startswith("rust:"):
            assert f"fn {where[5:]}(" in INSTALLER_RS.read_text(encoding="utf-8"), key
        elif "::" in where:
            path, *names = where.split("::")
            assert f"def {names[-1]}(" in (REPO_ROOT / path).read_text(encoding="utf-8"), key
        else:
            assert f"def {where}(" in here, key


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = make_launcher_db(tmp_path, projects=[{
        "project_id": "x", "name": "X", "folder_path": str(tmp_path / "p")}])
    (tmp_path / "p").mkdir()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(path))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))  # summary_backends reads <state>/launcher.db
    for var in ("ACTIVE_EMBEDDING", "EMBEDDING_MODEL", "KG_SUMMARY_OPENAI_MODEL",
                "KG_SUMMARY_BACKEND", "KG_SUMMARY_OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return path


def _summary_backends():
    name = "summary_backends_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "templates" / "scripts" / "summary_backends.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_rl_global_flags_project_their_values(db):
    from vco_lib.config_projection import project_env_from_db

    for value in ("true", "false"):
        set_app_state(db, "rl.local_logging_disabled_global", value)
        set_app_state(db, "rl.online_training_disabled_global", value)
        env = project_env_from_db("x", db_path=db)["canonical_env"]
        assert env["RL_LOCAL_LOGGING_DISABLED_GLOBAL"] == value
        assert env["RL_ONLINE_TRAINING_DISABLED_GLOBAL"] == value


def test_summary_consent_value_drives_the_gate(db):
    sb = _summary_backends()
    set_app_state(db, "kg_summary_openai_consent", "true")
    assert sb.openai_consent_granted()
    set_app_state(db, "kg_summary_openai_consent", "false")
    assert not sb.openai_consent_granted()


def test_summary_openai_model_value_is_the_model(db):
    sb = _summary_backends()
    for model in ("gpt-4o", "gpt-4.1-mini"):
        set_app_state(db, "kg_summary_openai_model", model)
        assert sb._openai_model() == model


def test_summary_backend_override_value_is_the_pinned_tier(db):
    sb = _summary_backends()
    for tier in ("cli", "zai", "qwen", "ollama"):
        set_app_state(db, "kg_summary_backend_override", tier)
        assert sb._forced_backend(("KG_SUMMARY_BACKEND",)) == tier


def test_summary_ollama_model_value_is_the_model(db):
    from vco_lib import summary_model

    for model in ("gemma4:e4b", "qwen3.5:9b"):
        set_app_state(db, "kg_summary_ollama_model", model)
        assert summary_model._preferences_model() == model


def test_embeddings_consent_value_drives_the_gate(db):
    from vco_lib.openai_consent import embeddings_consent_granted

    set_app_state(db, "openai_embeddings_consent", "true")
    assert embeddings_consent_granted()
    set_app_state(db, "openai_embeddings_consent", "false")
    assert not embeddings_consent_granted()


def test_active_profile_value_drives_the_text_model(db):
    from vco_lib.embedding_service import resolve_active_text_model_id

    set_app_state(db, "embedding.active_profile", "qwen3")
    qwen = resolve_active_text_model_id()
    set_app_state(db, "embedding.active_profile", "arctic")
    arctic = resolve_active_text_model_id()
    assert "qwen3" in qwen and "arctic" in arctic
