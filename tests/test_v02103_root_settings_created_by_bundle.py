# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 F4 (Windows field run): a fresh install flagged VCO's OWN freshly
created ``.claude/settings.json`` as user-owned.

Step 5b (the delegated ``install-bundle`` run) creates the root's
settings.json from the template — without the ``_vco_managed_keys`` sentinel —
and step 9 (``install.py::_configure_claude_settings``) then took the
"legacy / external" branch: VCO defaults to ``settings.json.vco-new`` plus a
``claude_settings_user_modified_preserved`` deferral, on every fresh install
since v0.2.85 moved the bundle ahead of step 9.

Fix: the bundle's own envelope (``settings_action == "created"``) is recorded
by ``vco_lib.self_install`` and step 9 recognises a file created in THIS run:
defaults are filled in (existing values win), the sentinel is stamped, no
deferral, no ``.vco-new``. A sentinel-less file the bundle did NOT create this
run keeps the user-owned path.

RED-PROOF: make ``root_settings_created_this_run`` return False → the
real-path test fails (deferral + .vco-new reappear).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: E402
from vco_lib import self_install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

_spec = importlib.util.spec_from_file_location("install_py_v02103_f4", REPO_ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v02103_f4"] = install_py
_spec.loader.exec_module(install_py)

_EMBED = {"text_model": "snowflake-arctic-embed2:latest", "active_embedding": "arctic",
          "code_backend": "ollama"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "orch"
    r.mkdir()
    make_fake_orchestrator(r)
    monkeypatch.setattr(install_py, "PROJECT_ROOT", r)
    monkeypatch.setattr(self_install, "_ROOT_SETTINGS_ACTION", {})
    yield r
    shutil.rmtree(r, ignore_errors=True)


def _ids(report: DeferralReport) -> list[str]:
    return [e.condition_id for e in report.entries]


def test_fresh_root_bundle_then_step9_no_deferral_no_vco_new(root):
    """The real sequence: install-bundle (subprocess, as step 5b runs it)
    creates settings.json, then step 9 runs — no deferral, no .vco-new, the
    sentinel is stamped, the template's hooks survive and install's env and
    permissions defaults are filled in."""
    settings = root / ".claude" / "settings.json"
    assert not settings.exists()
    result = self_install.run_root_bundle_install(root, update_mode=False)
    assert result.get("settings_action") == "created", result.get("warnings")
    created = json.loads(settings.read_text(encoding="utf-8"))
    assert "_vco_managed_keys" not in created  # the shape that tripped step 9

    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)

    assert "claude_settings_user_modified_preserved" not in _ids(report)
    assert not (root / ".claude" / "settings.json.vco-new").exists()
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["_vco_managed_keys"] == list(install_py._VCO_SETTINGS_MANAGED_KEYS)
    assert after.get("hooks") == created.get("hooks")
    assert after["env"]["ACTIVE_EMBEDDING"] == "arctic"
    defaults = install_py._build_vco_settings_defaults(_EMBED)
    for rule in defaults["permissions"]["allow"]:
        assert rule in after["permissions"]["allow"]
    for rule in (created.get("permissions") or {}).get("allow", []):
        assert rule in after["permissions"]["allow"]

    # A second install pass now takes the managed-block path — still quiet.
    report2 = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report2)
    assert "claude_settings_user_modified_preserved" not in _ids(report2)
    assert not (root / ".claude" / "settings.json.vco-new").exists()


def test_a_file_the_bundle_did_not_create_this_run_stays_user_owned(root):
    """Leave-alone case: a sentinel-less settings.json that existed BEFORE the
    bundle ran (the bundle merges it, settings_action != created) keeps the
    legacy/user-owned path — the run signal, not the file's shape, decides."""
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(make *)"]}}), encoding="utf-8")
    result = self_install.run_root_bundle_install(root, update_mode=False)
    assert result.get("settings_action") != "created"
    assert not self_install.root_settings_created_this_run(root)

    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)
    assert "claude_settings_user_modified_preserved" in _ids(report)
    assert (root / ".claude" / "settings.json.vco-new").exists()


def test_adopt_is_fill_only_existing_values_win():
    """Env already projected into the file (the canonical writer ran between
    5b and 9) is never overwritten by install's defaults; missing keys are
    filled; permissions.allow is the ordered union; the sentinel is stamped."""
    existing = {"hooks": {"Stop": []}, "env": {"KG_COLLECTION": "Acme_KnowledgeGraph"},
                "permissions": {"allow": ["Bash(a)"], "deny": ["Bash(rm *)"]}}
    defaults = {"_vco_managed_keys": ["env", "hooks"],
                "env": {"KG_COLLECTION": "KnowledgeGraph", "ACTIVE_EMBEDDING": "arctic"},
                "permissions": {"allow": ["Bash(git *)", "Bash(a)"]}}
    out = self_install.adopt_bundle_created_settings(existing, defaults, "_vco_managed_keys")
    assert out["env"] == {"KG_COLLECTION": "Acme_KnowledgeGraph", "ACTIVE_EMBEDDING": "arctic"}
    assert out["permissions"] == {"allow": ["Bash(a)", "Bash(git *)"], "deny": ["Bash(rm *)"]}
    assert out["hooks"] == {"Stop": []}
    assert out["_vco_managed_keys"] == ["env", "hooks"]
    assert existing["env"] == {"KG_COLLECTION": "Acme_KnowledgeGraph"}  # pure


def test_only_created_counts_and_the_signal_is_per_root(tmp_path, monkeypatch):
    monkeypatch.setattr(self_install, "_ROOT_SETTINGS_ACTION", {})
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for action, expected in (("created", True), ("merged", False), ("would-create", False),
                             ("unchanged", False), ("", False)):
        self_install._record_root_settings_action(a, {"settings_action": action})
        assert self_install.root_settings_created_this_run(a) is expected, action
    self_install._record_root_settings_action(a, {"settings_action": "created"})
    assert self_install.root_settings_created_this_run(b) is False
