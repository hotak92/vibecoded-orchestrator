# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — every reader/editor of a project's settings goes through the ONE
JSONC reader (:mod:`vco_lib.jsonc_edit`) and the ONE env-line parser
(:mod:`vco_lib.envfile`).

Claude Code accepts comments and trailing commas in ``.claude/settings.json``;
VS Code's ``.vscode/settings.json`` is JSONC by definition. Each reader below
used a strict ``json.loads`` and so read such a file as "no settings":

* ``knowledge_residue`` — the ``SHARED_KG_READ_DISABLED`` gate of the bundled-
  knowledge residue cleanup (an opted-out project looked opted in, so a bundle
  update could delete its on-disk curated copies — its only curated access);
* ``project_init`` — the on-disk ``KG_COLLECTION`` pin, and the legacy
  ``BASH_ENV`` strip (which now EDITS a JSONC file in place);
* ``hooks_settings`` — the Hooks tab refused a JSONC file as "not valid JSON";
  it now edits it in place, or refuses VISIBLY (ledger entry) when the edit
  cannot be verified;
* ``install.py`` — the PROJECT_NAME readers (``.vscode/settings.json`` as
  JSONC, and ``.claude/env`` through the shared parser, which also reads the
  ``export KEY="…"`` form VCO's own managed block writes).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hooks_settings as hs  # noqa: E402
from vco_lib import kg_sync_drift, knowledge_residue, project_init  # noqa: E402
from vco_lib.config_projection import _build_managed_block  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

#: The hooks editor's OWN refusal surface (v0.2.97 review F19) — and the env
#: projection's, which the hooks editor must never clear.
REFUSED_CID = "settings_write_refused_hooks_claude_settings_json"
PROJECTION_CID = "settings_write_refused_claude_settings_json"


def _settings(folder: Path, data: dict, *, jsonc: bool) -> Path:
    path = folder / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2)
    if jsonc:
        text = "// my own note\n" + text[:-1].rstrip() + ",\n}\n"
        with pytest.raises(ValueError):
            json.loads(text)
    path.write_text(text, encoding="utf-8")
    return path


# ── 1. knowledge_residue: the SHARED_KG_READ_DISABLED gate ─────────────────


@pytest.mark.parametrize("jsonc", [False, True])
def test_the_shared_read_gate_honours_settings_json(tmp_path, jsonc):
    """ACT (RED before for jsonc=True: the gate read as "not disabled")."""
    _settings(tmp_path, {"env": {"SHARED_KG_READ_DISABLED": "true"}}, jsonc=jsonc)
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is True


@pytest.mark.parametrize("value", ["false", "0", ""])
def test_the_shared_read_gate_stays_open_otherwise(tmp_path, value):
    """LEAVE-ALONE: a false/empty value, in JSONC, keeps shared reads on."""
    _settings(tmp_path, {"env": {"SHARED_KG_READ_DISABLED": value}}, jsonc=True)
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is False


def test_the_shared_read_gate_falls_back_to_the_managed_block(tmp_path):
    """An unreadable settings.json falls through to ``.claude/env``, read with
    the shared parser — including the ``export KEY="…"`` form the projection
    writes."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_bytes(b"{ broken")
    (tmp_path / ".claude" / "env").write_text(
        _build_managed_block({"SHARED_KG_READ_DISABLED": "true"}), encoding="utf-8")
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is True
    assert knowledge_residue.project_env_value(tmp_path, "SHARED_KG_READ_DISABLED") == "true"
    (tmp_path / ".claude" / "env").unlink()
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is False


def test_the_kg_collection_hint_reads_jsonc(tmp_path):
    _settings(tmp_path, {"env": {"KG_COLLECTION": "P_KnowledgeGraph"}}, jsonc=True)
    assert kg_sync_drift._local_kg_collection_hint(tmp_path) == "P_KnowledgeGraph"


# ── 2. project_init: the KG pin and the legacy BASH_ENV strip ─────────────


def test_the_on_disk_kg_pin_is_read_from_a_jsonc_settings_file(tmp_path):
    _settings(tmp_path, {"env": {"KG_COLLECTION": "VCODev_KnowledgeGraph"}}, jsonc=True)
    out = project_init._resolve_bundle_collection_names_binding_first(
        "Some Name", tmp_path, db_path=tmp_path / "missing" / "launcher.db")
    assert out["kg_collection"] == "VCODev_KnowledgeGraph"


def test_the_bash_env_strip_edits_a_jsonc_file_in_place(tmp_path):
    path = _settings(tmp_path, {"env": {
        "BASH_ENV": ".claude/scripts/leanctx-bash-env.sh", "KEEP": "1"}}, jsonc=True)
    out = project_init._cleanup_legacy_bash_env_in_project(tmp_path)
    assert out["action"] == "removed", out
    text = path.read_text(encoding="utf-8")
    assert "// my own note" in text and "BASH_ENV" not in text
    assert "KEEP" in text


def test_the_bash_env_strip_leaves_other_values_and_broken_files_alone(tmp_path):
    path = _settings(tmp_path, {"env": {"BASH_ENV": "/opt/mine.sh"}}, jsonc=True)
    before = path.read_bytes()
    assert project_init._cleanup_legacy_bash_env_in_project(tmp_path)["action"] == "left-alone"
    assert path.read_bytes() == before
    path.write_bytes(b"{ broken")
    assert project_init._cleanup_legacy_bash_env_in_project(tmp_path)["action"] == "unparseable"
    assert path.read_bytes() == b"{ broken"


BASH_ENV_CID = "legacy_bash_env_cleanup_pending"


def _root_cleanup(monkeypatch, root: Path) -> str:
    """install.py's own (root) legacy cleanup, pointed at ``root``."""
    import install

    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    install._cleanup_legacy_bash_env_shim(argparse.Namespace())
    ledger = root / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return ledger.read_text(encoding="utf-8") if ledger.exists() else ""


def test_the_root_bash_env_strip_edits_a_jsonc_file_in_place(tmp_path, monkeypatch):
    """R4 F32(h) ACT. RED before: install.py's root copy `json.loads`-ed the
    file, printed a skip, and left the fork-bomb pointer in a JSONC file."""
    path = _settings(tmp_path, {"env": {
        "BASH_ENV": "${CLAUDE_PROJECT_DIR}/.claude/scripts/leanctx-bash-env.sh",
        "KEEP": "1"}}, jsonc=True)
    _root_cleanup(monkeypatch, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "BASH_ENV" not in text and "// my own note" in text and "KEEP" in text
    assert not DeferralReport.read(tmp_path).has_condition(BASH_ENV_CID)


def test_the_root_bash_env_strip_records_a_file_it_cannot_read(tmp_path, monkeypatch):
    """R4 F32(h) REFUSE + RECORD. RED before: a print nobody reads, no entry."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{ "env": { "BASH_ENV": "leanctx-bash-env.sh" ')
    before = path.read_bytes()
    ledger = _root_cleanup(monkeypatch, tmp_path)
    assert path.read_bytes() == before
    assert DeferralReport.read(tmp_path).has_condition(BASH_ENV_CID)
    assert "could not be read" in ledger


def test_an_unverifiable_jsonc_edit_is_refused_and_recorded(tmp_path, monkeypatch):
    """A JSONC edit `dumps_preserving` cannot verify writes NOTHING and says
    so truthfully (not the "read-only file system" write-failure text)."""
    path = _settings(tmp_path, {"env": {"BASH_ENV": "leanctx-bash-env.sh"}}, jsonc=True)
    before = path.read_bytes()
    monkeypatch.setattr(project_init._jsonc_edit, "dumps_preserving", lambda *a, **k: None)
    out = project_init._cleanup_legacy_bash_env_in_project(tmp_path)
    assert out["action"] == "edit-refused", out
    assert path.read_bytes() == before
    rendered = _root_cleanup(monkeypatch, tmp_path)
    assert BASH_ENV_CID in rendered and "read-only" not in rendered
    assert "by hand" in rendered


# ── 3. hooks_settings: the Hooks tab edits JSONC in place ──────────────────


NOTIFY = "bash .claude/hooks/notify-stop.sh"


def test_the_hooks_editor_edits_a_jsonc_settings_file_in_place(tmp_path):
    """ACT. RED before: load_settings raised `unparseable` ("not valid JSON")."""
    path = _settings(tmp_path, {"env": {"A": "1"}, "hooks": {}}, jsonc=True)
    doc = hs.load_settings(path)
    assert hs.register_hook(doc, "Stop", "", NOTIFY) is True
    hs.write_settings(doc)
    text = path.read_text(encoding="utf-8")
    assert "// my own note" in text
    doc2 = hs.load_settings(path)
    assert doc2.data["hooks"]["Stop"][0]["hooks"][0]["command"] == NOTIFY
    parked = hs.remove_hook(doc2, "Stop", "", NOTIFY)
    hs.write_settings(doc2)
    assert parked and "// my own note" in path.read_text(encoding="utf-8")
    assert not DeferralReport.read(tmp_path).has_condition(REFUSED_CID)


def test_a_strict_json_file_is_still_written_in_the_house_form(tmp_path):
    """LEAVE-ALONE: strict JSON is re-serialised exactly as before."""
    path = _settings(tmp_path, {"hooks": {}}, jsonc=False)
    doc = hs.load_settings(path)
    assert doc.jsonc_text is None
    hs.register_hook(doc, "Stop", "", NOTIFY)
    hs.write_settings(doc)
    assert path.read_text(encoding="utf-8") == doc.render()
    json.loads(path.read_text(encoding="utf-8"))


def test_an_unverifiable_jsonc_edit_is_refused_visibly_and_cleared_after(tmp_path):
    """A comment INSIDE the value the edit replaces cannot be kept: refuse,
    write nothing, leave a ledger entry; the next successful write clears it."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n  "hooks": {\n    "Stop": [\n      // keep me\n'
        '      {"hooks": [{"type": "command", "command": "echo mine"}]}\n'
        '    ]\n  }\n}\n', encoding="utf-8")
    before = path.read_bytes()
    doc = hs.load_settings(path)
    hs.register_hook(doc, "Stop", "", NOTIFY)
    with pytest.raises(hs.HooksSettingsError) as ctx:
        hs.write_settings(doc)
    assert ctx.value.code == "jsonc_edit_refused"
    assert path.read_bytes() == before
    assert DeferralReport.read(tmp_path).has_condition(REFUSED_CID)

    path.write_text(before.decode().replace("      // keep me\n", ""), encoding="utf-8")
    doc = hs.load_settings(path)
    hs.register_hook(doc, "Stop", "", NOTIFY)
    hs.write_settings(doc)
    assert not DeferralReport.read(tmp_path).has_condition(REFUSED_CID)


def _projection_refused_file(folder: Path) -> Path:
    """A JSONC settings.json the env projection refuses to edit (a duplicated
    `env` key — `jsonc_edit` will not pick which one to change) while its
    `hooks` member edits fine."""
    path = folder / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{\n  // mine\n  "env": {"A": "1"},\n  "env": {"A": "1"},\n'
                    '  "hooks": {}\n}\n', encoding="utf-8")
    return path


def _apply(folder: Path) -> None:
    from vco_lib import config_projection as cp

    cp.apply_project_env({"canonical_env": {"KG_COLLECTION": "P_KnowledgeGraph"},
                          "project_id": "", "project_root": folder})


def test_a_hooks_write_never_clears_the_projections_refusal(tmp_path):
    """F19. RED before: both writers shared one surface, so this successful
    hooks write cleared the projection's still-true refusal."""
    from vco_lib.config_projection import SettingsWriteRefused

    path = _projection_refused_file(tmp_path)
    with pytest.raises(SettingsWriteRefused):
        _apply(tmp_path)
    assert DeferralReport.read(tmp_path).has_condition(PROJECTION_CID)

    doc = hs.load_settings(path)
    hs.register_hook(doc, "Stop", "", NOTIFY)
    hs.write_settings(doc)  # succeeds: `hooks` is not the duplicated member
    assert DeferralReport.read(tmp_path).has_condition(PROJECTION_CID), \
        "the projection's refusal is still true and must stay recorded"


def test_each_writer_clears_only_its_own_refusal(tmp_path):
    """Both refusals recorded; each writer's success clears ITS entry only."""
    from vco_lib import settings_refusal
    from vco_lib.config_projection import SettingsWriteRefused

    path = _projection_refused_file(tmp_path)
    with pytest.raises(SettingsWriteRefused):
        _apply(tmp_path)
    hs._record_jsonc_refusal(path, hs.jsonc_edit.JsoncEditRefused("x", "a test refusal"))
    report = DeferralReport.read(tmp_path)
    assert report.has_condition(PROJECTION_CID) and report.has_condition(REFUSED_CID)

    # The file is repaired; the projection's success clears its own entry only.
    path.write_text('{\n  // mine\n  "env": {"A": "1"},\n  "hooks": {}\n}\n', encoding="utf-8")
    _apply(tmp_path)
    report = DeferralReport.read(tmp_path)
    assert not report.has_condition(PROJECTION_CID)
    assert report.has_condition(REFUSED_CID), "the projection must not clear the hooks entry"

    # The hooks editor's success clears its own.
    doc = hs.load_settings(path)
    hs.register_hook(doc, "Stop", "", NOTIFY)
    hs.write_settings(doc)
    assert not DeferralReport.read(tmp_path).has_condition(REFUSED_CID)
    assert settings_refusal.condition_id(hs._SETTINGS_SURFACE) == REFUSED_CID


@pytest.mark.parametrize("raw,code", [
    (b"{ broken", "unparseable"),
    (b"\xff\xfe{}", "unreadable"),
])
def test_an_unreadable_settings_file_is_refused_untouched(tmp_path, raw, code):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(hs.HooksSettingsError) as ctx:
        hs.load_settings(path)
    assert ctx.value.code == code
    if code == "unparseable":
        assert "not valid JSON or JSONC" in ctx.value.message
    assert path.read_bytes() == raw


# ── 4. install.py: the PROJECT_NAME readers ──────────────────────────────


@pytest.fixture(scope="module")
def install_mod():
    import install

    return install


def test_the_vscode_project_name_is_read_from_jsonc(tmp_path, install_mod):
    """VS Code's own format is JSONC (RED before: None)."""
    vs = tmp_path / ".vscode" / "settings.json"
    vs.parent.mkdir()
    vs.write_text('{\n  // editor\n  "claude-code.env": {"PROJECT_NAME": "Mine",},\n}\n',
                  encoding="utf-8")
    assert install_mod._read_project_name_from_vscode_settings(tmp_path) == "Mine"
    vs.write_bytes(b"{ broken")
    assert install_mod._read_project_name_from_vscode_settings(tmp_path) is None


def test_the_envfile_project_name_reads_the_managed_block_form(tmp_path, install_mod):
    """``export PROJECT_NAME="…"`` — the form the managed block writes (RED
    before: the key parsed as ``export PROJECT_NAME`` and never matched)."""
    env = tmp_path / "env"
    env.write_text(_build_managed_block({"PROJECT_NAME": "My Project"}), encoding="utf-8")
    assert install_mod._read_project_name_from_envfile(env) == "My Project"
    env.write_text('# PROJECT_NAME="placeholder"\nPROJECT_NAME=\n', encoding="utf-8")
    assert install_mod._read_project_name_from_envfile(env) is None
    assert install_mod._read_project_name_from_envfile(tmp_path / "absent") is None


# ── 7. the launcher's JSONC-aware read verb (read-env) ────────────────────


def test_read_env_reports_each_json_surface(tmp_path, capsys):
    """The Python half of `vco_lib_bridge::read_settings_env_blocks`: JSONC
    read, a missing file, an unreadable file — and the one-object stdout
    contract of the CLI."""
    from vco_lib import env_projection_check as epc

    _settings(tmp_path, {"env": {"KG_COLLECTION": "P_KnowledgeGraph"}}, jsonc=True)
    vs = tmp_path / ".vscode" / "settings.json"
    vs.parent.mkdir()
    vs.write_bytes(b"{ broken")
    got = epc.read_json_env_blocks(tmp_path)
    assert got["claude_settings_json"]["status"] == "ok"
    assert got["claude_settings_json"]["env"] == {"KG_COLLECTION": "P_KnowledgeGraph"}
    assert got["vscode_settings_json"]["status"] == "unreadable"
    assert got["vscode_settings_json"]["env"] is None and got["vscode_settings_json"]["error"]
    vs.unlink()
    assert epc.read_json_env_blocks(tmp_path)["vscode_settings_json"]["status"] == "missing"

    assert epc.main(["read-env", "--project-folder", str(tmp_path),
                     "--project-folder", str(tmp_path / "other")]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is True
    assert set(reply["folders"]) == {str(tmp_path), str(tmp_path / "other")}
    assert reply["folders"][str(tmp_path / "other")]["claude_settings_json"]["status"] == "missing"
