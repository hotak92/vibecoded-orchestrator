# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — no VCO writer overwrites a settings file it cannot read.

The env writers (``config_projection`` and, through ``write-env-block``, the
launcher's module-deprecation keys) used to read an unparseable
``.claude/settings.json`` / ``.vscode/settings.json`` as ``{}`` and write that
back with only their block in it — every other setting destroyed. Each test
here is an ACT case (the write happens as intended) or a LEAVE-ALONE case
(the file is byte-identical afterwards and the refusal is visible: raised,
recorded in the project's deferral ledger, and cleared once it is over).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import config_projection as cp
from vco_lib import deferral_probes, deferral_registry, jsonc_edit, settings_refusal
from vco_lib.deferral_report import DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_CID = "settings_write_refused_claude_settings_json"
VSCODE_CID = "settings_write_refused_vscode_settings_json"
DEPRECATION_KEYS = [
    "VCT_RL_MODULE_DEPRECATED",
    "VCT_RL_MODULE_DEPRECATION_MESSAGE",
    "VCT_RL_MODULE_DEPRECATION_DATE",
    "VCT_RL_MODULE_DEPRECATION_URL",
]

#: One broken shape per way a file can fail to be an editable object.
UNPARSEABLE = {
    "syntax": b'{"hooks": {"PreToolUse": []},, "permissions": {"allow": ["Bash"]}}',
    "unterminated_jsonc": b'{\n  // mine\n  "hooks": {\n',
    "array_root": b'["not", "an", "object"]',
    "string_root": b'"just a string"',
    "not_utf8": b'{"name": "\xff\xfe caf\xe9"}',
}


def _bundle(root: Path) -> dict:
    return {
        "canonical_env": {"KG_COLLECTION": "TestKG", "PROJECT_NAME": "Test"},
        "project_id": "pid-refusal",
        "project_root": root,
    }


def _settings(root: Path, data: bytes, rel: str = ".claude/settings.json") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _entry(root: Path, cid: str = CLAUDE_CID):
    return DeferralReport.read(root).entry_for(cid)


def _probe(root: Path, entry) -> object:
    ctx = deferral_probes.ProbeContext(folder=root, entry=entry)
    return deferral_probes.PROBES["settings_write_refusal_still_applies"](ctx)


# ── LEAVE ALONE: every unparseable shape, byte-identical + visible ──────────


@pytest.mark.parametrize("shape", sorted(UNPARSEABLE))
def test_unparseable_settings_is_byte_identical_and_the_refusal_is_visible(tmp_path, shape):
    original = UNPARSEABLE[shape]
    path = _settings(tmp_path, original)

    with pytest.raises(cp.SettingsWriteRefused) as info:
        cp.apply_project_env(_bundle(tmp_path))

    assert path.read_bytes() == original, "the user's file must not change by one byte"
    assert str(path) in str(info.value) and "NOT updated" in str(info.value)
    entry = _entry(tmp_path)
    assert entry is not None, "the refusal must reach the project's deferral ledger"
    assert "`.claude/settings.json`" in entry.detected
    assert entry.dismiss_fields["kind"] == "unparseable"
    assert entry.dismiss_fields["path"] == ".claude/settings.json"
    assert "apply --project-id pid-refusal" in entry.command_to_apply
    assert entry.resolved_disposition == "action_required"


def test_the_other_surfaces_are_still_written_when_one_is_refused(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(_bundle(tmp_path))
    assert path.read_bytes() == UNPARSEABLE["syntax"]
    env_text = (tmp_path / ".claude" / "env").read_text(encoding="utf-8")
    assert 'export KG_COLLECTION="TestKG"' in env_text


def test_vscode_refusal_has_its_own_entry_and_never_touches_the_claude_one(tmp_path):
    vscode = _settings(tmp_path, b"{ broken", ".vscode/settings.json")
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(
            _bundle(tmp_path), surfaces=["claude_settings_json", "vscode_settings_json"],
        )
    assert vscode.read_bytes() == b"{ broken"
    assert _entry(tmp_path, VSCODE_CID) is not None
    assert _entry(tmp_path, CLAUDE_CID) is None, "the claude surface was written fine"
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text())["env"]["KG_COLLECTION"] == "TestKG"


def test_a_refused_jsonc_edit_is_raised_and_recorded(tmp_path):
    raw = b'{\n  // mine\n  "env": {"KG_COLLECTION": "a", "KG_COLLECTION": "b"},\n}\n'
    path = _settings(tmp_path, raw)
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(_bundle(tmp_path))
    assert path.read_bytes() == raw
    entry = _entry(tmp_path)
    assert entry.dismiss_fields["kind"] == "jsonc_edit_refused"
    assert _probe(tmp_path, entry) is True, "same bytes → the same edit is refused again"
    path.write_bytes(raw.replace(b"// mine", b"// mine, edited"))
    assert _probe(tmp_path, entry) is None, "changed bytes: only the next write can tell"


# ── ACT: JSONC in place, strict JSON layout, missing file created ───────────


def test_jsonc_settings_is_edited_in_place_with_comments_kept(tmp_path):
    raw = '{\n    // hand-kept\n    "hooks": {"Stop": []}, /* keep */\n    "env": {"MINE": "1",},\n}\n'
    path = _settings(tmp_path, raw.encode())
    cp.apply_project_env(_bundle(tmp_path))
    text = path.read_text(encoding="utf-8")
    assert "// hand-kept" in text and "/* keep */" in text
    data = jsonc_edit.loads(text)
    assert data["hooks"] == {"Stop": []}
    assert data["env"] == {"MINE": "1", "KG_COLLECTION": "TestKG", "PROJECT_NAME": "Test"}
    assert _entry(tmp_path) is None


def test_strict_json_keeps_the_layout(tmp_path):
    """Re-serialised with the Rust-parity layout: 2-space indent, no
    trailing newline (the env keys' order is the canonical set's)."""
    path = _settings(tmp_path, json.dumps({"hooks": {}}).encode())
    cp.apply_project_env(_bundle(tmp_path))
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data == {"hooks": {}, "env": {"KG_COLLECTION": "TestKG", "PROJECT_NAME": "Test"}}
    assert text == json.dumps(data, indent=2, ensure_ascii=False)


def test_a_missing_settings_file_is_created(tmp_path):
    cp.apply_project_env(_bundle(tmp_path))
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data == {"env": {"KG_COLLECTION": "TestKG", "PROJECT_NAME": "Test"}}
    assert DeferralReport.read(tmp_path).entries == []


# ── lifecycle: probe + paired clear ─────────────────────────────────────────


def test_the_probe_keeps_a_still_broken_file_and_clears_a_repaired_or_removed_one(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(_bundle(tmp_path))
    entry = _entry(tmp_path)
    assert _probe(tmp_path, entry) is True
    path.write_text('{"hooks": {}}', encoding="utf-8")
    assert _probe(tmp_path, entry) is False
    path.unlink()
    assert _probe(tmp_path, entry) is False, "a missing file is simply created next time"


def test_the_probe_does_not_guess_without_the_recorded_path(tmp_path):
    class Bare:
        dismiss_fields: dict = {}
    assert _probe(tmp_path, Bare()) is None


def test_the_next_successful_write_clears_the_entry(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(_bundle(tmp_path))
    assert _entry(tmp_path) is not None
    path.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    cp.apply_project_env(_bundle(tmp_path))
    assert _entry(tmp_path) is None
    assert json.loads(path.read_text())["hooks"] == {"Stop": []}


def test_the_family_is_registered_with_the_probe():
    for cid in (CLAUDE_CID, VSCODE_CID):
        spec = deferral_registry.condition(cid)
        assert spec is not None and spec.pattern == "settings_write_refused_*"
        assert spec.condition_class == "action_required"
        assert spec.clear_probe == "probe:py:settings_write_refusal_still_applies"
        assert spec.dismiss_key == ("path", "kind", "sha256")
    assert "settings_write_refusal_still_applies" in deferral_probes.PROBES


# ── write-env-block: the ONE surgical editor the launcher calls ─────────────


def test_write_env_block_sets_and_strips_only_owned_keys(tmp_path):
    path = _settings(tmp_path, json.dumps({
        "hooks": {"Stop": []},
        "env": {"USER_KEY": "keep", "VCT_RL_MODULE_DEPRECATION_URL": "stale"},
    }).encode())
    written = cp.write_env_block(
        tmp_path, "claude_settings_json",
        {"VCT_RL_MODULE_DEPRECATED": "1", "VCT_RL_MODULE_DEPRECATION_MESSAGE": "m"},
        DEPRECATION_KEYS,
    )
    assert written == ["VCT_RL_MODULE_DEPRECATED", "VCT_RL_MODULE_DEPRECATION_MESSAGE"]
    data = json.loads(path.read_text())
    assert data["hooks"] == {"Stop": []}
    assert data["env"] == {
        "USER_KEY": "keep", "VCT_RL_MODULE_DEPRECATED": "1",
        "VCT_RL_MODULE_DEPRECATION_MESSAGE": "m",
    }


def test_write_env_block_refuses_an_unparseable_file(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    with pytest.raises(cp.SettingsWriteRefused):
        cp.write_env_block(tmp_path, "claude_settings_json", {}, DEPRECATION_KEYS)
    assert path.read_bytes() == UNPARSEABLE["syntax"]
    entry = _entry(tmp_path)
    assert entry is not None and "--project-id" not in entry.command_to_apply


def test_write_env_block_rejects_a_key_it_does_not_own(tmp_path):
    with pytest.raises(cp.ConfigProjectionError):
        cp.write_env_block(tmp_path, "claude_settings_json", {"OTHER": "x"}, DEPRECATION_KEYS)
    assert not (tmp_path / ".claude").exists()


def _cli(tmp_path: Path, request: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.config_projection", "write-env-block",
         "--project-folder", str(tmp_path)],
        input=json.dumps(request), capture_output=True, text=True, cwd=REPO_ROOT,
        env=child_env(), check=False, timeout=60,
    )


def test_the_cli_refusal_is_one_json_object_on_stdout_and_exit_4(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["array_root"])
    proc = _cli(tmp_path, {"set": {"VCT_RL_MODULE_DEPRECATED": "1"}, "owned_keys": DEPRECATION_KEYS})
    assert proc.returncode == 4, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is False and out["error"] == "settings_write_refused"
    assert out["refused"][0]["path"] == str(path)
    assert path.read_bytes() == UNPARSEABLE["array_root"]


def test_the_cli_writes_and_reports_the_keys(tmp_path):
    proc = _cli(tmp_path, {"set": {"VCT_RL_MODULE_DEPRECATED": "1"}, "owned_keys": DEPRECATION_KEYS})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "ok": True, "surface": "claude_settings_json", "written": ["VCT_RL_MODULE_DEPRECATED"],
    }
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text()) == {
        "env": {"VCT_RL_MODULE_DEPRECATED": "1"},
    }


def test_the_cli_rejects_a_malformed_request(tmp_path):
    proc = _cli(tmp_path, {"set": ["not", "a", "map"]})
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"] == "bad_request"


def test_write_env_block_appends_new_keys_in_a_stable_order(tmp_path):
    """New keys land in ``values`` order, not set-iteration order (which
    varies with the hash seed between runs and would reorder the file)."""
    keys = [f"K{i:02d}" for i in range(20)]
    cp.write_env_block(tmp_path, "claude_settings_json", {k: "v" for k in keys}, keys)
    env = json.loads((tmp_path / ".claude" / "settings.json").read_text())["env"]
    assert list(env) == keys


# ── strip-env-keys: the removal-only twin (v0.2.97 review F5) ───────────────


def test_strip_env_keys_removes_only_the_named_keys_and_drops_an_emptied_block(tmp_path):
    path = _settings(tmp_path, json.dumps({
        "hooks": {"Stop": []}, "env": {"KG_COLLECTION": "x", "USER_KEY": "keep"},
    }).encode())
    assert cp.strip_env_keys(tmp_path, "claude_settings_json", ["KG_COLLECTION", "ABSENT"]) == [
        "KG_COLLECTION",
    ]
    assert json.loads(path.read_text()) == {"hooks": {"Stop": []}, "env": {"USER_KEY": "keep"}}
    assert cp.strip_env_keys(tmp_path, "claude_settings_json", ["USER_KEY"]) == ["USER_KEY"]
    assert json.loads(path.read_text()) == {"hooks": {"Stop": []}}, "no empty env block left behind"


def test_strip_env_keys_edits_jsonc_in_place(tmp_path):
    raw = (
        "{\n    // team settings\n    \"editor.formatOnSave\": true,\n"
        "    \"claude-code.env\": {\n        \"KG_COLLECTION\": \"x\",\n"
        "        \"USER_KEY\": \"mine\", // keep\n    },\n}\n"
    )
    path = _settings(tmp_path, raw.encode(), ".vscode/settings.json")
    assert cp.strip_env_keys(tmp_path, "vscode_settings_json", ["KG_COLLECTION"]) == ["KG_COLLECTION"]
    text = path.read_text(encoding="utf-8")
    assert "// team settings" in text and "// keep" in text
    assert jsonc_edit.loads(text)["claude-code.env"] == {"USER_KEY": "mine"}


def test_strip_env_keys_never_creates_and_never_rewrites_for_nothing(tmp_path):
    assert cp.strip_env_keys(tmp_path, "claude_settings_json", ["K"]) == []
    assert not (tmp_path / ".claude").exists(), "a strip never creates a file"
    original = b'{"hooks":{},"env":{"OTHER":"1"}}'
    path = _settings(tmp_path, original)
    assert cp.strip_env_keys(tmp_path, "claude_settings_json", ["K"]) == []
    assert path.read_bytes() == original, "nothing to strip → not rewritten (layout kept)"


def test_strip_env_keys_refuses_and_records_an_unreadable_file(tmp_path):
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    with pytest.raises(cp.SettingsWriteRefused):
        cp.strip_env_keys(tmp_path, "claude_settings_json", ["KG_COLLECTION"])
    assert path.read_bytes() == UNPARSEABLE["syntax"]
    assert _entry(tmp_path) is not None


def test_the_unregister_strip_refuses_an_unreadable_file_and_says_so(tmp_path):
    """The unregister's JSON strip (`unregister_env strip-routing`, which
    superseded the by-name `strip-env-keys` verb in review R6) refuses a file
    it cannot edit exactly like every writer: byte-identical, recorded in the
    ledger, and named in the reply's `errors` (the other surfaces still run)."""
    def run(request: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "vco_lib.unregister_env", "strip-routing",
             "--project-folder", str(tmp_path)],
            input=json.dumps(request), capture_output=True, text=True, cwd=REPO_ROOT,
            env=child_env(), check=False, timeout=60,
        )

    assert run({"keys": "A"}).returncode == 2
    path = _settings(tmp_path, UNPARSEABLE["array_root"])
    proc = run({"keys": ["KG_COLLECTION"]})
    assert proc.returncode == 0, proc.stderr
    reply = json.loads(proc.stdout)
    assert any("left untouched" in e and "NOT updated" in e for e in reply["errors"]), reply
    assert path.read_bytes() == UNPARSEABLE["array_root"]
    assert _entry(tmp_path) is not None


def test_the_apply_cli_exits_4_with_the_refusal_on_stderr(tmp_path, monkeypatch, capsys):
    """The launcher's ``apply_project_env_via_python`` puts a non-zero exit's
    stderr into its warnings — that is the GUI-visible half of the refusal."""
    path = _settings(tmp_path, UNPARSEABLE["syntax"])
    monkeypatch.setattr(cp, "project_env_from_db", lambda *a, **k: _bundle(tmp_path))
    rc = cp.main(["apply", "--project-id", "pid-refusal"])
    assert rc == 4
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "settings_write_refused" and str(path) in err["message"]
    assert path.read_bytes() == UNPARSEABLE["syntax"]


# ── the bundle merge: a non-object root is refused like bad JSON ────────────


def test_bundle_settings_merge_leaves_a_non_object_root_alone(tmp_path):
    from vco_lib import project_init

    template = tmp_path / "template.json"
    template.write_text(json.dumps({"hooks": {}, "permissions": {"allow": []}}))
    target = _settings(tmp_path, UNPARSEABLE["array_root"])
    status, _ = project_init._merge_settings_template_for_bundle(template, target, dry_run=False)
    assert status == "unchanged (user file unparseable)"
    assert target.read_bytes() == UNPARSEABLE["array_root"]


def test_bundle_settings_merge_still_merges_a_strict_object(tmp_path):
    from vco_lib import project_init

    template = tmp_path / "template.json"
    template.write_text(json.dumps({"permissions": {"allow": []}}))
    target = _settings(tmp_path, json.dumps({"mine": 1}).encode())
    status, _ = project_init._merge_settings_template_for_bundle(template, target, dry_run=False)
    assert status == "merged"
    assert json.loads(target.read_text()) == {"mine": 1, "permissions": {"allow": []}}


def test_refusal_reason_names_where_a_strict_parser_stops(tmp_path):
    path = _settings(tmp_path, b'{\n  "a": 1,,\n}')
    refusal = settings_refusal.load_for_edit(path)
    assert isinstance(refusal, settings_refusal.Refusal)
    assert "line 2" in refusal.reason
