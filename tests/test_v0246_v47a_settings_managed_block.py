"""V47-A (Gap A) tests for `.claude/settings.json` managed-block merge.

Pins the behavior matrix documented in install.py::_configure_claude_settings:

* File missing → write VCO defaults (including `_vco_managed_keys`).
* File parses + has `_vco_managed_keys` → managed-block merge (refresh
  listed keys, preserve every other top-level key).
* File parses + no `_vco_managed_keys` → legacy: write VCO defaults as
  `settings.json.vco-new` sibling + emit deferral, leave original alone.
* File parse fails → emit deferral, leave file alone.
* `adopt_project_mode == "replace-all"` → unconditionally overwrite.
* `adopt_project_mode in (None, "adopt")` → use safe matrix above.

Tests intentionally use isolated tmp_path roots so the worktree's own
`.claude/settings.json` is never modified.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# Load install.py as a module without triggering argparse-at-top-level side
# effects (no main() call). Pattern matches V47-G-stub contract test.
_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47a", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47a"] = install_py
_spec.loader.exec_module(install_py)


# Import DeferralReport for the deferral-side assertions.
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def embed_config() -> dict:
    """Minimal embed_config dict satisfying _build_vco_settings_defaults."""
    return {
        "text_model": "qwen3-embedding:0.6b",
        "active_embedding": "qwen3",
        "code_backend": "ollama",
    }


@pytest.fixture
def isolated_project_root(tmp_path: Path, monkeypatch):
    """Re-point install_py.PROJECT_ROOT at a fresh tmp dir for each test."""
    monkeypatch.setattr(install_py, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _settings_file(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _vco_new_sibling(root: Path) -> Path:
    return root / ".claude" / "settings.json.vco-new"


# ---------------------------------------------------------------------------
# Section 1: schema constants + defaults builder
# ---------------------------------------------------------------------------

def test_managed_keys_sentinel_constant_is_underscored():
    """The sentinel key is `_vco_managed_keys` (leading underscore signals
    metadata, matches existing `_template_origin` / `_comment` convention)."""
    assert install_py._VCO_MANAGED_KEYS_SENTINEL == "_vco_managed_keys"


def test_managed_keys_default_is_env_and_hooks():
    """Default VCO-managed keys are `env` and `hooks` per design spec."""
    assert install_py._VCO_SETTINGS_MANAGED_KEYS == ("env", "hooks")


def test_build_defaults_includes_sentinel_key(embed_config):
    defaults = install_py._build_vco_settings_defaults(embed_config)
    assert "_vco_managed_keys" in defaults
    assert defaults["_vco_managed_keys"] == ["env", "hooks"]


def test_build_defaults_includes_env_block(embed_config):
    defaults = install_py._build_vco_settings_defaults(embed_config)
    assert "env" in defaults
    assert defaults["env"]["EMBEDDING_MODEL"] == "qwen3-embedding:0.6b"
    assert defaults["env"]["KG_COLLECTION"] == "KnowledgeGraph"


def test_build_defaults_includes_permissions(embed_config):
    """Permissions stay as the existing fresh-install block."""
    defaults = install_py._build_vco_settings_defaults(embed_config)
    assert "permissions" in defaults
    assert "Bash(git *)" in defaults["permissions"]["allow"]


# ---------------------------------------------------------------------------
# Section 2: file-missing path → fresh write
# ---------------------------------------------------------------------------

def test_fresh_install_writes_settings_with_sentinel(
    isolated_project_root, embed_config,
):
    """Case 1: no existing settings.json → VCO defaults written including
    the `_vco_managed_keys` sentinel."""
    install_py._configure_claude_settings(embed_config)

    settings_path = _settings_file(isolated_project_root)
    assert settings_path.is_file()

    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["_vco_managed_keys"] == ["env", "hooks"]
    assert "env" in payload
    assert payload["env"]["EMBEDDING_MODEL"] == "qwen3-embedding:0.6b"


def test_fresh_install_does_not_emit_deferral(
    isolated_project_root, embed_config,
):
    """Case 1: fresh write is not a deferral situation."""
    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, deferral_report=report,
    )
    assert len(report.entries) == 0


# ---------------------------------------------------------------------------
# Section 3: existing-with-sentinel path → managed-block merge
# ---------------------------------------------------------------------------

def test_existing_with_sentinel_refreshes_managed_keys(
    isolated_project_root, embed_config,
):
    """Case 4: managed-block-aware file → refresh `env` and `hooks` from
    defaults."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "_vco_managed_keys": ["env", "hooks"],
        # Stale env values that VCO should refresh.
        "env": {
            "WEAVIATE_URL": "http://localhost:9999",
            "EMBEDDING_MODEL": "OLD_MODEL",
        },
        # User extensions that VCO must preserve.
        "permissions": {"allow": ["Bash(custom *)"]},
        "customUserKey": {"foo": "bar"},
    }, indent=2) + "\n", encoding="utf-8")

    install_py._configure_claude_settings(embed_config)

    refreshed = json.loads(settings_path.read_text(encoding="utf-8"))
    # Managed key got refreshed
    assert refreshed["env"]["EMBEDDING_MODEL"] == "qwen3-embedding:0.6b"
    # Non-managed user keys preserved verbatim
    assert refreshed["permissions"]["allow"] == ["Bash(custom *)"]
    assert refreshed["customUserKey"] == {"foo": "bar"}
    # Sentinel still present
    assert refreshed["_vco_managed_keys"] == ["env", "hooks"]


def test_existing_with_sentinel_does_not_emit_deferral(
    isolated_project_root, embed_config,
):
    """Case 4: managed-block merge is auto-resolvable; no deferral."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "_vco_managed_keys": ["env", "hooks"],
        "env": {"WEAVIATE_URL": "http://localhost:9999"},
        "permissions": {"allow": ["Bash(custom *)"]},
    }) + "\n", encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, deferral_report=report,
    )
    assert len(report.entries) == 0


def test_existing_with_sentinel_no_vco_new_sibling_created(
    isolated_project_root, embed_config,
):
    """Case 4: only in legacy case do we write `.vco-new`. Managed-block
    merge edits the file in place."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "_vco_managed_keys": ["env", "hooks"],
        "env": {},
    }) + "\n", encoding="utf-8")

    install_py._configure_claude_settings(embed_config)

    assert not _vco_new_sibling(isolated_project_root).exists()


# ---------------------------------------------------------------------------
# Section 4: existing-without-sentinel path → legacy / vco-new sidecar
# ---------------------------------------------------------------------------

def test_legacy_existing_writes_vco_new_sibling(
    isolated_project_root, embed_config,
):
    """Case 5: file exists without `_vco_managed_keys` → VCO defaults
    land at `settings.json.vco-new`, original preserved."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "permissions": {"allow": ["Bash(custom *)"]},
        "env": {"KG_COLLECTION": "LegacyCollection"},
    }
    settings_path.write_text(json.dumps(legacy, indent=2) + "\n",
                             encoding="utf-8")
    legacy_text = settings_path.read_text(encoding="utf-8")

    install_py._configure_claude_settings(embed_config)

    # Original untouched
    assert settings_path.read_text(encoding="utf-8") == legacy_text

    # Sidecar written with VCO defaults
    new_path = _vco_new_sibling(isolated_project_root)
    assert new_path.is_file()
    new_payload = json.loads(new_path.read_text(encoding="utf-8"))
    assert new_payload["_vco_managed_keys"] == ["env", "hooks"]


def test_legacy_existing_emits_deferral(
    isolated_project_root, embed_config,
):
    """Case 5: legacy detection emits
    `claude_settings_user_modified_preserved`."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["Bash(custom *)"]},
    }) + "\n", encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, deferral_report=report,
    )
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.condition_id == "claude_settings_user_modified_preserved"
    assert entry.severity == "warning"
    assert "--adopt-project-replace-all" in entry.command_to_apply


# ---------------------------------------------------------------------------
# Section 5: parse-failure path
# ---------------------------------------------------------------------------

def test_unparseable_file_left_alone(
    isolated_project_root, embed_config,
):
    """Case 3: garbage JSON → file untouched, deferral emitted."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    garbage = "{ this is not valid JSON ]]]"
    settings_path.write_text(garbage, encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, deferral_report=report,
    )

    # File untouched
    assert settings_path.read_text(encoding="utf-8") == garbage
    # Deferral emitted
    assert len(report.entries) == 1
    assert report.entries[0].condition_id == "claude_settings_unparseable"


def test_non_object_json_left_alone(
    isolated_project_root, embed_config,
):
    """Case 3b: file parses but isn't a JSON object (e.g., array) → same
    as parse failure."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("[1, 2, 3]\n", encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, deferral_report=report,
    )

    # File untouched
    assert settings_path.read_text(encoding="utf-8") == "[1, 2, 3]\n"
    assert len(report.entries) == 1
    assert report.entries[0].condition_id == "claude_settings_unparseable"


# ---------------------------------------------------------------------------
# Section 6: adopt_project_mode dispatch
# ---------------------------------------------------------------------------

def test_replace_all_overwrites_managed_file(
    isolated_project_root, embed_config,
):
    """`adopt_project_mode='replace-all'` overwrites even a managed-aware
    file (advanced opt-in path)."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "_vco_managed_keys": ["env", "hooks"],
        "env": {},
        "userExtension": "kept-on-adopt-not-on-replace-all",
    }) + "\n", encoding="utf-8")

    install_py._configure_claude_settings(
        embed_config, adopt_project_mode="replace-all",
    )

    fresh = json.loads(settings_path.read_text(encoding="utf-8"))
    # userExtension dropped (replace-all overwrites outright)
    assert "userExtension" not in fresh
    # VCO defaults present
    assert fresh["_vco_managed_keys"] == ["env", "hooks"]


def test_replace_all_overwrites_legacy_file_no_sidecar(
    isolated_project_root, embed_config,
):
    """`replace-all` on a legacy (no-sentinel) file overwrites in place;
    no `.vco-new` sidecar needed since the user explicitly opted in."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["legacy"]},
    }) + "\n", encoding="utf-8")

    install_py._configure_claude_settings(
        embed_config, adopt_project_mode="replace-all",
    )

    refreshed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "_vco_managed_keys" in refreshed
    # No sidecar — direct overwrite path on replace-all.
    assert not _vco_new_sibling(isolated_project_root).exists()


def test_adopt_mode_none_uses_safe_matrix(
    isolated_project_root, embed_config,
):
    """`adopt_project_mode=None` uses the safe matrix (= legacy file →
    sidecar, not overwrite)."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"permissions": {"allow": ["legacy"]}}
    legacy_text = json.dumps(legacy, indent=2) + "\n"
    settings_path.write_text(legacy_text, encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, adopt_project_mode=None, deferral_report=report,
    )

    # Original preserved, sidecar written
    assert settings_path.read_text(encoding="utf-8") == legacy_text
    assert _vco_new_sibling(isolated_project_root).is_file()
    assert len(report.entries) == 1


def test_adopt_mode_adopt_uses_safe_matrix(
    isolated_project_root, embed_config,
):
    """`adopt_project_mode='adopt'` behaves the same as None for V47-A's
    legacy-detection branch."""
    settings_path = _settings_file(isolated_project_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"permissions": {"allow": ["legacy"]}}
    settings_path.write_text(json.dumps(legacy, indent=2) + "\n",
                             encoding="utf-8")

    report = DeferralReport()
    install_py._configure_claude_settings(
        embed_config, adopt_project_mode="adopt", deferral_report=report,
    )

    # Sidecar written, deferral emitted (same as None)
    assert _vco_new_sibling(isolated_project_root).is_file()
    assert len(report.entries) == 1
    assert report.entries[0].condition_id == "claude_settings_user_modified_preserved"


# ---------------------------------------------------------------------------
# Section 7: pure-function merge helper
# ---------------------------------------------------------------------------

def test_merge_helper_preserves_user_keys():
    existing = {
        "_vco_managed_keys": ["env", "hooks"],
        "env": {"OLD": "value"},
        "permissions": {"allow": ["userBash"]},
    }
    defaults = {
        "_vco_managed_keys": ["env", "hooks"],
        "env": {"NEW": "value"},
        "hooks": {"PreToolUse": []},
        "permissions": {"allow": ["DEFAULT"]},
    }
    merged = install_py._merge_vco_settings_managed_block(existing, defaults)
    # env refreshed
    assert merged["env"] == {"NEW": "value"}
    # hooks added (was missing in existing, listed as managed)
    assert merged["hooks"] == {"PreToolUse": []}
    # permissions NOT in managed list → user's value wins
    assert merged["permissions"]["allow"] == ["userBash"]


def test_merge_helper_extends_sentinel_additively():
    """If defaults' managed-keys list grows in a future VCO version, the
    merged sentinel surfaces the union (additive only)."""
    existing = {
        "_vco_managed_keys": ["env"],
        "env": {},
    }
    defaults = {
        "_vco_managed_keys": ["env", "hooks", "mcpServers"],
        "env": {"new": "value"},
        "hooks": {},
        "mcpServers": {},
    }
    merged = install_py._merge_vco_settings_managed_block(existing, defaults)
    # Sentinel surfaces union, ordered: existing first, then new
    assert merged["_vco_managed_keys"] == ["env", "hooks", "mcpServers"]


def test_merge_helper_handles_invalid_sentinel_gracefully():
    """If `_vco_managed_keys` is the wrong type (e.g., a string), the
    helper should not crash."""
    existing = {
        "_vco_managed_keys": "not a list",  # bad
        "env": {"keep": "me"},
        "permissions": {"allow": []},
    }
    defaults = {
        "_vco_managed_keys": ["env", "hooks"],
        "env": {"replace": "me"},
        "hooks": {},
    }
    # Should not raise.
    merged = install_py._merge_vco_settings_managed_block(existing, defaults)
    # Defensive path: no keys treated as managed → existing kept verbatim
    # except the sentinel gets refreshed.
    assert merged["env"] == {"keep": "me"}
    assert merged["permissions"] == {"allow": []}
