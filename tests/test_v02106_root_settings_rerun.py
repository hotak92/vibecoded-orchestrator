# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 (macOS field run): a RERUN after a first install that stopped
between step 5b and step 9 flagged VCO's own ``.claude/settings.json`` as
user-owned.

Run 1: step 5b's bundle CREATED the root's settings.json (no
``_vco_managed_keys`` sentinel), then the install stopped (missing compose
provider) before step 9 could stamp the sentinel. Run 2: step 9 found a
sentinel-less file; the v0.2.103 F4 signal only knew about the CURRENT
process, so the file took the user-owned path — ``settings.json.vco-new`` +
a ``claude_settings_user_modified_preserved`` deferral whose printed remedy
was ``--adopt-project-replace-all``.

Fix: the bundle records "VCO created settings.json" in the manifest
(``vco_lib.settings_provenance``), fingerprinted without ``env`` (the env
writers own it); step 9 adopts a file whose fingerprint still matches. The
user-owned entry now lists the safe manual remedies first and says what
replace-all destroys.

RED-PROOF (base d6db54b5): ``test_rerun_after_a_failed_first_install_adopts_vcos_own_file``
and ``test_vco_env_writes_between_runs_keep_the_file_vcos`` fail there (the
deferral + ``.vco-new`` appear); the entry-text tests fail on the old text
(replace-all was the one runnable command).
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
from vco_lib import self_install, settings_provenance  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.manifest_paths import manifest_path  # noqa: E402

_spec = importlib.util.spec_from_file_location("install_py_v02106_rerun", REPO_ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v02106_rerun"] = install_py
_spec.loader.exec_module(install_py)

_EMBED = {"text_model": "snowflake-arctic-embed2:latest", "active_embedding": "arctic",
          "code_backend": "ollama"}
_CID = "claude_settings_user_modified_preserved"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "orch"
    r.mkdir()
    make_fake_orchestrator(r)
    monkeypatch.setattr(install_py, "PROJECT_ROOT", r)
    monkeypatch.setattr(self_install, "_ROOT_SETTINGS_ACTION", {})
    yield r
    shutil.rmtree(r, ignore_errors=True)


def _settings(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _ids(report: DeferralReport) -> list[str]:
    return [e.condition_id for e in report.entries]


def _run1_aborted_before_step9(root: Path) -> dict:
    """Run 1 through the real functions: the bundle subprocess creates the
    file, then the process ends before step 9 (its in-memory signal is lost)."""
    result = self_install.run_root_bundle_install(root, update_mode=False)
    assert result.get("settings_action") == "created", result.get("warnings")
    self_install._ROOT_SETTINGS_ACTION.clear()  # a new process for run 2
    created = json.loads(_settings(root).read_text(encoding="utf-8"))
    assert "_vco_managed_keys" not in created
    return created


def _run2(root: Path) -> DeferralReport:
    """Run 2: step 5b's bundle again, then step 9."""
    self_install.run_root_bundle_install(root, update_mode=False)
    assert not self_install.root_settings_created_this_run(root)
    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)
    return report


def _write_env(root: Path, **env: str) -> None:
    path = _settings(root)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("env", {}).update(env)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_rerun_after_a_failed_first_install_adopts_vcos_own_file(root):
    created = _run1_aborted_before_step9(root)
    record = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    assert settings_provenance.MANIFEST_KEY in record

    report = _run2(root)

    assert _CID not in _ids(report)
    assert not (root / ".claude" / "settings.json.vco-new").exists()
    after = json.loads(_settings(root).read_text(encoding="utf-8"))
    assert after["_vco_managed_keys"] == list(install_py._VCO_SETTINGS_MANAGED_KEYS)
    assert after.get("hooks") == created.get("hooks")

    # The sentinel now marks it; the next bundle drops the record by itself.
    self_install.run_root_bundle_install(root, update_mode=False)
    record = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    assert settings_provenance.MANIFEST_KEY not in record
    report3 = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report3)
    assert _CID not in _ids(report3)


def test_vco_env_writes_between_runs_keep_the_file_vcos(root):
    """The env projection writes `env` after the bundle created the file (and
    again between run 2's bundle and step 9) — still VCO's file."""
    _run1_aborted_before_step9(root)
    _write_env(root, KG_COLLECTION="Acme_KnowledgeGraph")
    self_install.run_root_bundle_install(root, update_mode=False)
    _write_env(root, PROJECT_NAME="Acme")
    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)

    assert _CID not in _ids(report)
    after = json.loads(_settings(root).read_text(encoding="utf-8"))
    assert "_vco_managed_keys" in after
    assert after["env"]["KG_COLLECTION"] == "Acme_KnowledgeGraph"  # fill-only: kept
    assert after["env"]["PROJECT_NAME"] == "Acme"


def test_a_user_edit_between_runs_keeps_the_user_owned_path(root):
    _run1_aborted_before_step9(root)
    path = _settings(root)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("permissions", {}).setdefault("allow", []).append("Bash(make *)")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    report = _run2(root)

    assert _CID in _ids(report)
    assert (root / ".claude" / "settings.json.vco-new").exists()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert "_vco_managed_keys" not in after
    assert "Bash(make *)" in after["permissions"]["allow"]
    # The edit invalidated the record for good.
    record = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    assert settings_provenance.MANIFEST_KEY not in record


def test_a_user_edit_after_the_bundle_in_the_same_rerun_is_caught(root):
    """The record matches the file the bundle left; an edit made after it
    (before step 9) is still a user edit."""
    _run1_aborted_before_step9(root)
    self_install.run_root_bundle_install(root, update_mode=False)
    path = _settings(root)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["myOwnKey"] = True
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)
    assert _CID in _ids(report)


# --- the record's lifecycle (pure) -------------------------------------------


def test_next_record_lifecycle():
    nr = settings_provenance.next_record
    assert nr(None, settings_action="created", fingerprint_before=None,
              fingerprint_after="b") == {"fingerprint": "b"}
    prior = {"fingerprint": "a"}
    assert nr(prior, settings_action="merged", fingerprint_before="a",
              fingerprint_after="b") == {"fingerprint": "b"}
    assert nr(prior, settings_action="unchanged", fingerprint_before="a",
              fingerprint_after="a") == {"fingerprint": "a"}
    assert nr(prior, settings_action="unchanged", fingerprint_before="x",
              fingerprint_after="x") is None
    assert nr(None, settings_action="merged", fingerprint_before="a",
              fingerprint_after="a") is None
    assert nr(prior, settings_action="created", fingerprint_before=None,
              fingerprint_after=None) is None


def test_fingerprint_ignores_env_and_layout_only():
    fp = settings_provenance.fingerprint
    base = {"hooks": {"Stop": []}, "env": {"A": "1"}}
    assert fp(base) == fp({"env": {"A": "2", "B": "3"}, "hooks": {"Stop": []}})
    assert fp(base) != fp({"hooks": {"Stop": [1]}, "env": {"A": "1"}})


def test_jsonc_or_unreadable_file_is_never_vcos(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text('{ // a comment\n "a": 1 }', encoding="utf-8")
    assert settings_provenance.file_fingerprint(p) is None
    assert settings_provenance.created_by_vco_and_unchanged(tmp_path) is False


# --- the user-owned entry and the .vco-new sibling ---------------------------


def _entry(env_resolved: bool):
    return self_install.user_owned_settings_entry(
        "_vco_managed_keys", ["env", "permissions"], env_resolved=env_resolved)


def test_the_runnable_command_is_the_safe_rerun_not_replace_all():
    entry = _entry(env_resolved=True)
    runnable = [ln for ln in entry.command_to_apply.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert runnable == ["python install.py"]
    assert "--adopt-project-replace-all" in entry.command_to_apply  # named, commented
    # The sentinel line printed is valid JSON a user can paste.
    line = next(ln for ln in entry.command_to_apply.splitlines() if "_vco_managed_keys" in ln)
    assert json.loads("{" + line.lstrip("# ").strip() + "}") == {
        "_vco_managed_keys": ["env", "permissions"]}


def test_the_text_offers_the_safe_path_first_and_says_what_replace_all_destroys():
    why = _entry(env_resolved=True).why_deferred
    assert why.index("_vco_managed_keys") < why.index("--adopt-project-replace-all")
    assert ".vco-new" in why
    tail = why[why.index("--adopt-project-replace-all"):]
    assert "every edit" in tail and "lost" in tail
    assert "env projection" in tail


def test_the_env_note_depends_on_whether_names_resolved():
    unresolved = _entry(env_resolved=False).detected
    assert "Do not copy its `env` block" in unresolved
    assert "KnowledgeGraph" in unresolved
    assert "KnowledgeGraph" not in _entry(env_resolved=True).detected


def test_vco_new_env_uses_the_resolved_names(monkeypatch, tmp_path):
    import vco_lib.config_projection as cp
    import vco_lib.module_gated_delivery as mgd

    monkeypatch.setattr(mgd, "resolve_project_id_for_folder", lambda folder: "pid-1")
    monkeypatch.setattr(cp, "project_env_from_db", lambda pid, **kw: {
        "canonical_env": {"KG_COLLECTION": "Acme_KnowledgeGraph", "UNRELATED": "x"},
        "project_id": pid, "project_root": tmp_path})
    defaults = {"_vco_managed_keys": ["env"],
                "env": {"KG_COLLECTION": "KnowledgeGraph", "ACTIVE_EMBEDDING": "arctic"}}
    out, resolved = self_install.vco_new_settings(tmp_path, defaults)
    assert resolved is True
    assert out["env"] == {"KG_COLLECTION": "Acme_KnowledgeGraph", "ACTIVE_EMBEDDING": "arctic"}
    assert defaults["env"]["KG_COLLECTION"] == "KnowledgeGraph"  # pure

    monkeypatch.setattr(mgd, "resolve_project_id_for_folder", lambda folder: None)
    out, resolved = self_install.vco_new_settings(tmp_path, defaults)
    assert resolved is False and out == defaults


def test_user_owned_path_writes_the_resolved_sibling_and_entry(root, monkeypatch):
    """Through step 9: the sibling carries the resolved names and the entry
    says so (no 'factory defaults' warning)."""
    monkeypatch.setattr(self_install, "vco_new_settings",
                        lambda r, d: ({**d, "env": {**d["env"], "KG_COLLECTION": "Acme_KG"}}, True))
    _settings(root).parent.mkdir(parents=True, exist_ok=True)
    _settings(root).write_text(json.dumps({"permissions": {"allow": ["Bash(make *)"]}}),
                               encoding="utf-8")
    report = DeferralReport()
    install_py._configure_claude_settings(_EMBED, deferral_report=report)
    sibling = json.loads((root / ".claude" / "settings.json.vco-new").read_text(encoding="utf-8"))
    assert sibling["env"]["KG_COLLECTION"] == "Acme_KG"
    (entry,) = [e for e in report.entries if e.condition_id == _CID]
    assert "KnowledgeGraph" not in entry.detected
