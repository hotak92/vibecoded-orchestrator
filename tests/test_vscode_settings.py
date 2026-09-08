# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-12 — the VS Code global-settings writer.

This module writes a USER-OWNED file outside the project, so the tests are
weighted towards the leave-alone half: every act test has a counterpart
asserting that the things this writer must not touch were not touched, and
several assert on the file's SHA-256 rather than on its parsed form (a
"refused" path must be byte-identical, not merely equivalent).

Tri-OS: the per-OS path shapes and the permission decision are unit-tested
for Windows, macOS AND Linux from any host, because ``candidate_paths``
takes the platform as an argument. What CANNOT be tested off-platform — that
Windows' ``icacls`` actually applies the ACL, and that a real VS Code reads
the keys back — is named in the skip reasons rather than left invisible.

The real user's VS Code settings are never in scope here: every test drives
``home=``/``env=`` at a tmp_path, and the one test that resolves the default
home asserts on the SHAPE of the returned path without touching it.
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from vco_lib import vscode_settings as vs

POSIX_ONLY = (
    "POSIX mode bits do not exist on Windows; the Windows arm of this rule is "
    "test_windows_permission_failure_rolls_back, which drives the same "
    "decision through a raising restrict_to_owner on every OS"
)

TOKEN = "wp12-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def env_block(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))[vs.ENV_BLOCK_KEY]


@pytest.fixture()
def settings_file(tmp_path: Path) -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True)
    path = user / "settings.json"
    path.write_text(
        json.dumps(
            {
                "editor.fontSize": 13,
                "workbench.colorTheme": "Default Dark+",
            },
            indent=4,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# R15 — the rule this package must not get wrong
# ---------------------------------------------------------------------------


def test_point_panel_never_writes_tier_slots(settings_file: Path):
    """No tier slot and no subagent slot, ever. The name dispatched must answer.

    Six keys, asserted as a set rather than one-by-one: a future edit that
    adds a seventh slot key to ``SLOT_OVERRIDE_KEYS`` is covered without
    anyone remembering to extend this test.
    """
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert result["ok"] and result["status"] == "written"

    block = env_block(settings_file)
    for key in vs.SLOT_OVERRIDE_KEYS:
        assert key not in block, f"{key} must never be written by VCO"
    assert set(result["keys_written"]) == set(vs.ROUTING_KEYS)


def test_point_panel_writes_exactly_the_four_routing_keys(settings_file: Path):
    vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    block = env_block(settings_file)
    assert set(block) == set(vs.ROUTING_KEYS)
    assert block["ANTHROPIC_BASE_URL"] == BASE_URL
    assert block["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert block["ANTHROPIC_API_KEY"] == ""
    assert block["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    # ANTHROPIC_MODEL is NOT among them: no model was picked.
    assert vs.MODEL_KEY not in block


def test_model_written_only_when_explicitly_picked(settings_file: Path):
    vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert vs.MODEL_KEY not in env_block(settings_file)

    vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-opus-5",
    )
    # claude-opus-5 is 1M-windowed in the shipped context table, so the
    # written id carries the client's [1m] hint (R41 decoration; see
    # tests/test_v0292_vscode_settings_1m_decoration.py).
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-opus-5[1m]"


def test_a_vendor_model_is_never_written_as_the_default(settings_file: Path):
    """USER RULING 2026-09-08, the incident in one assertion.

    ANTHROPIC_MODEL is the value a RESTARTED panel resumes on. With a vendor
    id there, 204 turns of a release cycle ran on GLM while the picker still
    said Fable. So the key is refused — and the rest of the write proceeds,
    because the user asked to be pointed at the gateway and that part is
    fine.
    """
    result = vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-gw/glm-5.3",
    )
    block = env_block(settings_file)
    assert vs.MODEL_KEY not in block, "a vendor id must never become the Default"
    assert block["ANTHROPIC_BASE_URL"] == BASE_URL, "the rest of the write proceeds"
    assert result["ok"] and result["status"] == "written"
    assert vs.MODEL_KEY not in result["keys_written"]
    reason = result["refusal_reason"]
    assert reason and "claude-gw/glm-5.3" in reason, "the refusal names the id"
    assert vs.MODEL_KEY in reason and "Default" in reason, "and the rule"
    assert reason in result["message"], "a silent refusal is the same defect again"


def test_a_namespaced_claude_id_is_not_first_party_either(settings_file: Path):
    """`claude-gw/claude-opus-5` resolves ONLY through the gateway.

    The tail is a real Claude id, but the namespace is not: a panel that came
    back stock would fall back to a name nothing answers. Rejecting on the
    substring alone would have let this through.
    """
    result = vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-gw/claude-opus-5",
    )
    assert vs.MODEL_KEY not in env_block(settings_file)
    assert result["refusal_reason"]


def test_first_party_rule_covers_the_1m_suffix_and_the_namespace():
    for ok in ("claude-opus-5", "claude-opus-5[1m]", "Claude-Fable-5-1"):
        assert vs.is_first_party_model_id(ok) is True, ok
    for no in (
        "claude-gw/glm-5.3",
        "claude-gw/glm-5.3[1m]",
        "claude-gw/claude-opus-5",
        "glm-5.3",
        "glm-5.3[1m]",
        "gpt-x",
        "",
        "   ",
        None,
        42,
    ):
        assert vs.is_first_party_model_id(no) is False, no


def test_first_party_rule_is_a_prefix_test_and_case_folds_first():
    """Review R1-5. A substring test passed BOTH of these.

    `glm-5.3-claude` is a vendor id that happens to contain the word, and
    `Claude-GW/glm-5.3` is the gateway namespace in different case — the
    exact value the namespace check exists to catch.
    """
    for vendor_id in ("glm-5.3-claude", "Claude-GW/glm-5.3", "CLAUDE-GW/glm-5.3[1m]",
                      "my-claude-proxy", "anthropic-ish"):
        assert vs.is_first_party_model_id(vendor_id) is False, vendor_id
    assert vs.FIRST_PARTY_ID_PREFIX == "claude-"


def test_default_model_offered_by_the_gui_is_first_party():
    """The pre-selected Default was `claude-gw/glm-5.3` until 2026-09-08 and
    is exactly what put the machine on GLM overnight."""
    assert vs.is_first_party_model_id(vs.DEFAULT_GATEWAY_MODEL)
    assert not vs.DEFAULT_GATEWAY_MODEL.startswith(vs.GATEWAY_ID_PREFIX)
    assert "glm" not in vs.DEFAULT_GATEWAY_MODEL.lower()


def test_default_model_exists_in_the_shipped_context_seed():
    """The offered default names a model the gateway actually knows about.

    Read from the seed FILE rather than imported, so this stays a
    cross-artefact check: if someone renames the id in the seed, the GUI's
    default silently stops matching a real row and this reds.
    """
    seed = (
        Path(__file__).resolve().parent.parent
        / "claude_mcp_servers"
        / "model_router"
        / "chat_model_context.seed.json"
    )
    models = json.loads(seed.read_text(encoding="utf-8"))["models"]
    bare = vs.DEFAULT_GATEWAY_MODEL.split("/", 1)[-1]
    assert bare in models, f"{bare} is not a row in {seed.name}"
    assert models[bare]["vendor"] == vs.FIRST_PARTY_VENDOR, (
        "the offered Default must be a row the seed itself calls first-party"
    )


# ---------------------------------------------------------------------------
# Merge, never replace
# ---------------------------------------------------------------------------


def test_rerun_preserves_the_users_model_choice(settings_file: Path):
    """The defect the field prototype had: re-sync reset ANTHROPIC_MODEL."""
    vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-opus-5",
    )
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    # Preserved AND window-correct: the re-run keeps the [1m] decoration
    # idempotently instead of letting the value drift back to plain.
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-opus-5[1m]"
    assert vs.MODEL_KEY in result["keys_preserved"]


def test_the_result_names_the_base_url_it_wrote(settings_file: Path):
    """The reporting half of R1-1: a caller's done message must be able to
    name the endpoint actually written, not the one it assumed."""
    other = "http://127.0.0.1:11437"
    result = vs.point_at_gateway(settings_file, base_url=other, token=TOKEN)
    assert result["base_url"] == other
    assert env_block(settings_file)["ANTHROPIC_BASE_URL"] == other
    # Present on a refusal too — that is when knowing the target matters most.
    path = settings_file.parent / "broken.json"
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")
    refused = vs.point_at_gateway(path, base_url=other, token=TOKEN)
    assert refused["ok"] is False and refused["base_url"] == other


def test_a_vendor_default_already_in_the_file_is_preserved_not_deleted(
    settings_file: Path,
):
    """LEAVE-ALONE half of the rule: VCO refuses to WRITE one; it does not
    reach into the user's file and remove one they already have. It is
    reported (`panel_mode`) and cleared on a click (`clear_default_model`)."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY] = {vs.MODEL_KEY: "claude-gw/glm-5.3[1m]"}
    settings_file.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")

    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-gw/glm-5.3[1m]"
    assert vs.MODEL_KEY in result["keys_preserved"]
    assert result["refusal_reason"] is None, "nothing was refused; nothing was asked"


def test_clear_default_removes_only_the_model_key(settings_file: Path):
    vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-opus-5",
    )
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "claude-gw/glm-5.3-flash"
    settings_file.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")

    result = vs.clear_default_model(settings_file)
    assert result["ok"] and result["status"] == "written"
    assert result["keys_removed"] == [vs.MODEL_KEY]
    assert result["cleared_value"] == "claude-opus-5[1m]"
    block = env_block(settings_file)
    assert vs.MODEL_KEY not in block
    assert set(vs.ROUTING_KEYS) <= set(block), "routing keys are not its business"
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-gw/glm-5.3-flash", (
        "a slot override is the user's per-tier choice, not the restart fallback"
    )
    assert json.loads(settings_file.read_text(encoding="utf-8"))[vs.LOGIN_PROMPT_KEY] is True


def test_clear_default_is_a_no_op_when_there_is_none(settings_file: Path):
    vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    before = settings_file.read_bytes()
    result = vs.clear_default_model(settings_file)
    assert result["ok"] and result["status"] == "unchanged"
    assert result["keys_removed"] == []
    assert settings_file.read_bytes() == before


def test_clear_default_refuses_jsonc_byte_identical(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")
    before = path.read_bytes()
    result = vs.clear_default_model(path)
    assert result["ok"] is False and result["reason"] == "not_strict_json"
    assert vs.MODEL_KEY in result["message"]
    assert path.read_bytes() == before


def test_unrelated_env_keys_are_carried_forward_and_reported(settings_file: Path):
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY] = {
        "HTTPS_PROXY": "http://corp-proxy:3128",
        "MY_TEAM_FLAG": "yes",
    }
    settings_file.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")

    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    block = env_block(settings_file)
    assert block["HTTPS_PROXY"] == "http://corp-proxy:3128"
    assert block["MY_TEAM_FLAG"] == "yes"
    assert result["keys_preserved"] == ["HTTPS_PROXY", "MY_TEAM_FLAG"]


def test_unrelated_top_level_settings_are_untouched(settings_file: Path):
    vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings["editor.fontSize"] == 13
    assert settings["workbench.colorTheme"] == "Default Dark+"


def test_key_order_and_indent_are_preserved(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n  "z.last": 1,\n  "a.first": 2\n}\n', encoding="utf-8",
    )
    vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    text = path.read_text(encoding="utf-8")
    keys = list(json.loads(text))
    assert keys[:2] == ["z.last", "a.first"], "existing key order must survive"
    assert '\n  "z.last"' in text, "two-space indent must survive"


def test_crlf_file_stays_crlf(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{\r\n    "editor.fontSize": 13\r\n}\r\n')
    vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


# ---------------------------------------------------------------------------
# Slot overrides already in the file: preserved by default, removable on ask
# ---------------------------------------------------------------------------


def _with_prototype_slots(path: Path) -> None:
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY] = {
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-gw/glm-5.3-flash",
        "KEEP_ME": "1",
    }
    path.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")


def test_existing_slot_overrides_are_preserved_and_reported(settings_file: Path):
    """LEAVE-ALONE: they are the user's keys, so they survive — loudly.

    Surviving does not mean frozen mid-defect: glm-5.3-flash is 1M-windowed
    in the shipped context table, so both slot values come out in their
    ``[1m]`` context-window form (same model, right window — R41)."""
    _with_prototype_slots(settings_file)
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    block = env_block(settings_file)
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-gw/glm-5.3-flash[1m]"
    assert result["slot_overrides_preserved"] == [
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ]
    assert result["keys_removed"] == []


def test_slot_overrides_removed_only_when_asked(settings_file: Path):
    """ACT: the explicit, user-initiated removal."""
    _with_prototype_slots(settings_file)
    result = vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, remove_slot_overrides=True,
    )
    block = env_block(settings_file)
    for key in vs.SLOT_OVERRIDE_KEYS:
        assert key not in block
    assert result["keys_removed"] == [
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ]
    assert result["slot_overrides_preserved"] == []
    assert block["KEEP_ME"] == "1", "an unrelated key is not collateral damage"


# ---------------------------------------------------------------------------
# JSONC: refuse, byte-identically
# ---------------------------------------------------------------------------


JSONC_WITH_COMMENT = """{
    // The team's shared font size. Do not change without asking.
    "editor.fontSize": 13,
    "workbench.colorTheme": "Default Dark+"
}
"""

JSONC_TRAILING_COMMA = """{
    "editor.fontSize": 13,
    "workbench.colorTheme": "Default Dark+",
}
"""


@pytest.mark.parametrize(
    "body,hint",
    [
        (JSONC_WITH_COMMENT, "comments"),
        (JSONC_TRAILING_COMMA, "trailing comma"),
    ],
)
def test_jsonc_is_refused_and_the_file_is_byte_identical(
    tmp_path: Path, body: str, hint: str,
):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    before = sha(path)

    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert result["reason"] == "not_strict_json"
    assert hint in result["message"]
    assert sha(path) == before, "a refused write must not touch a single byte"


def test_refusal_hands_back_a_pasteable_block(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")

    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    block = result["paste_block"]
    # A printed instruction is shipped code: it must actually parse when the
    # user does what it says (paste between the braces of their object).
    parsed = json.loads("{" + block + "}")
    assert set(parsed) == {vs.ENV_BLOCK_KEY, vs.LOGIN_PROMPT_KEY}
    assert parsed[vs.LOGIN_PROMPT_KEY] is True
    assert set(parsed[vs.ENV_BLOCK_KEY]) == set(vs.ROUTING_KEYS)
    assert TOKEN not in block, "the paste block must not carry the real token"


def test_reset_refusal_names_the_two_keys_to_delete_by_hand(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")
    before = sha(path)

    result = vs.reset_native(path)
    assert result["ok"] is False
    assert vs.ENV_BLOCK_KEY in result["message"]
    assert vs.LOGIN_PROMPT_KEY in result["message"]
    assert sha(path) == before


def test_non_object_top_level_is_refused(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    before = sha(path)
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["reason"] == "not_an_object"
    assert sha(path) == before


def test_env_block_of_the_wrong_type_is_refused(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({vs.ENV_BLOCK_KEY: "not-an-object"}, indent=4), encoding="utf-8",
    )
    before = sha(path)
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["reason"] == "env_block_not_an_object"
    assert sha(path) == before


def test_missing_settings_dir_is_refused_not_created(tmp_path: Path):
    path = tmp_path / "NotInstalled" / "User" / "settings.json"
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["reason"] == "no_settings_dir"
    assert not path.parent.exists(), "must not fabricate a config tree"


def test_missing_file_in_an_existing_user_dir_is_created(tmp_path: Path):
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True)
    path = user / "settings.json"
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["ok"] and result["status"] == "written"
    assert result["backup_path"] is None, "nothing existed to back up"
    assert env_block(path)["ANTHROPIC_BASE_URL"] == BASE_URL


# ---------------------------------------------------------------------------
# reset_native — exactly two keys
# ---------------------------------------------------------------------------


def test_reset_removes_exactly_the_two_managed_keys(settings_file: Path):
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY] = {"ANTHROPIC_BASE_URL": BASE_URL}
    settings[vs.LOGIN_PROMPT_KEY] = True
    settings["some.other.extension"] = {"a": 1}
    settings_file.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")

    result = vs.reset_native(settings_file)
    assert result["ok"] and result["status"] == "written"
    assert set(result["keys_removed"]) == set(vs.MANAGED_SETTINGS_KEYS)

    after = json.loads(settings_file.read_text(encoding="utf-8"))
    assert vs.ENV_BLOCK_KEY not in after
    assert vs.LOGIN_PROMPT_KEY not in after
    # LEAVE-ALONE half: everything else survived verbatim.
    assert after["editor.fontSize"] == 13
    assert after["some.other.extension"] == {"a": 1}


def test_reset_removes_login_prompt_even_when_env_block_is_absent(tmp_path: Path):
    """The stranding case: env block already gone, suppression left behind."""
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({vs.LOGIN_PROMPT_KEY: True}, indent=4), encoding="utf-8")

    result = vs.reset_native(path)
    assert result["keys_removed"] == [vs.LOGIN_PROMPT_KEY]
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_reset_on_a_clean_file_changes_nothing(settings_file: Path):
    before = sha(settings_file)
    result = vs.reset_native(settings_file)
    assert result["ok"] and result["status"] == "unchanged"
    assert sha(settings_file) == before


def test_reset_on_a_missing_file_is_a_no_op(tmp_path: Path):
    result = vs.reset_native(tmp_path / "nope" / "settings.json")
    assert result["ok"] and result["status"] == "unchanged"


def test_backup_names_never_collide_within_the_same_second(settings_file: Path):
    """The backup a user wants back is the FIRST one, so it must survive.

    Two writes inside one second produce the same timestamp; without the
    uniquifier the second `atomic_copy_file` would overwrite the first
    backup — destroying the pre-VCO state and keeping only the intermediate.
    """
    first = vs._backup_path(settings_file, now=1_800_000_000.0)
    first.write_text("pretend this is the original", encoding="utf-8")
    second = vs._backup_path(settings_file, now=1_800_000_000.0)
    assert second != first
    assert first.read_text(encoding="utf-8") == "pretend this is the original"


def test_two_content_changing_writes_leave_two_backups(settings_file: Path):
    original = settings_file.read_text(encoding="utf-8")
    vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    vs.reset_native(settings_file)

    backups = sorted(settings_file.parent.glob("settings.json.bak-*"))
    assert len(backups) == 2, (
        "each content-changing write keeps its own backup; a collision would "
        "silently drop the pre-VCO state"
    )
    assert any(b.read_text(encoding="utf-8") == original for b in backups), (
        "the ORIGINAL file must still be recoverable from one of the backups"
    )


def test_point_twice_is_idempotent_and_makes_one_backup(settings_file: Path):
    first = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    after_first = sha(settings_file)
    second = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert first["status"] == "written"
    assert second["status"] == "unchanged"
    assert second["backup_path"] is None
    assert sha(settings_file) == after_first
    backups = list(settings_file.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1, "an unchanged run must not spawn another backup"


# ---------------------------------------------------------------------------
# Already-damaged: a panel pointed at a dead / uninstalled gateway
# ---------------------------------------------------------------------------


def test_reset_if_gateway_resets_a_panel_pointed_at_a_DEAD_gateway(tmp_path: Path):
    """ACT. Nothing here is running — the guard is a URL comparison, by design.

    This is the uninstall interaction: the gateway's port file is gone, the
    daemon is not listening, and the panel must still be recoverable.
    """
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                vs.ENV_BLOCK_KEY: {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:11436",
                    "ANTHROPIC_AUTH_TOKEN": TOKEN,
                },
                vs.LOGIN_PROMPT_KEY: True,
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    result = vs.reset_native_if_vco_gateway(path, ports=(vs.DEFAULT_GATEWAY_PORT,))
    assert result["ok"] and result["status"] == "written"
    assert set(result["keys_removed"]) == set(vs.MANAGED_SETTINGS_KEYS)


def test_reset_if_gateway_leaves_a_foreign_base_url_alone(tmp_path: Path):
    """LEAVE-ALONE. Somebody else's endpoint is not ours to reset."""
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                vs.ENV_BLOCK_KEY: {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
                vs.LOGIN_PROMPT_KEY: True,
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    before = sha(path)
    result = vs.reset_native_if_vco_gateway(path, ports=(vs.DEFAULT_GATEWAY_PORT,))
    assert result["status"] == "left_alone"
    assert sha(path) == before


def test_reset_if_gateway_leaves_a_different_local_port_alone(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {vs.ENV_BLOCK_KEY: {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}},
            indent=4,
        ),
        encoding="utf-8",
    )
    before = sha(path)
    result = vs.reset_native_if_vco_gateway(path, ports=(11436,))
    assert result["status"] == "left_alone"
    assert sha(path) == before


def test_prototype_written_file_is_overwritten_by_point_but_slots_reported(
    settings_file: Path,
):
    """Already-damaged, the second shape: the machine-local prototype's file."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings[vs.ENV_BLOCK_KEY] = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
        "ANTHROPIC_AUTH_TOKEN": "old-prototype-token",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash",
    }
    settings[vs.LOGIN_PROMPT_KEY] = True
    settings_file.write_text(json.dumps(settings, indent=4) + "\n", encoding="utf-8")

    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    block = env_block(settings_file)
    assert block["ANTHROPIC_BASE_URL"] == BASE_URL
    assert block["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert result["slot_overrides_preserved"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]


# ---------------------------------------------------------------------------
# is_vco_gateway_base_url — the guard the uninstall path leans on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:11436", True),
        ("http://127.0.0.1:11436/", True),
        ("http://localhost:11436", True),
        ("http://[::1]:11436", True),
        ("http://127.0.0.5:11436", True),
        ("http://127.0.0.1:8787", False),
        ("http://192.168.1.10:11436", False),
        ("https://api.z.ai/api/anthropic", False),
        ("http://127.0.0.1", False),  # default port 80, not ours
        ("", False),
        (None, False),
        ("not a url", False),
        ("ftp://127.0.0.1:11436", False),
    ],
)
def test_is_vco_gateway_base_url(url, expected):
    assert vs.is_vco_gateway_base_url(url, ports=(11436,)) is expected


def test_resolve_gateway_ports_falls_back_to_the_default_only_without_evidence(
    monkeypatch, tmp_path,
):
    """Review R2-5. The old form asserted the default was ALWAYS in the
    answer, which contradicted R1-2 and passed only because conftest
    redirects VCT_STATE_DIR to a scratch dir with no port files. Now it
    pins the leave-alone half explicitly: no pin, no port file, no
    last-port record -> the documented default, and nothing else."""
    monkeypatch.delenv(vs.PORT_ENV, raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    assert vs.resolve_gateway_ports() == (vs.DEFAULT_GATEWAY_PORT,)


# ---------------------------------------------------------------------------
# Target discovery — tri-OS, from any host
# ---------------------------------------------------------------------------


def test_linux_paths_for_every_variant(tmp_path: Path):
    paths = {
        t.app_id + ":" + t.flavour: t.path
        for t in vs.candidate_paths(platform_key="linux", home=tmp_path, env={})
    }
    assert paths["code:native"] == str(
        tmp_path / ".config" / "Code" / "User" / "settings.json"
    )
    assert paths["code-insiders:native"] == str(
        tmp_path / ".config" / "Code - Insiders" / "User" / "settings.json"
    )
    assert paths["vscodium:native"] == str(
        tmp_path / ".config" / "VSCodium" / "User" / "settings.json"
    )
    assert paths["cursor:native"] == str(
        tmp_path / ".config" / "Cursor" / "User" / "settings.json"
    )
    assert paths["code:flatpak"] == str(
        tmp_path
        / ".var"
        / "app"
        / "com.visualstudio.code"
        / "config"
        / "Code"
        / "User"
        / "settings.json"
    )


def test_linux_honours_xdg_config_home(tmp_path: Path):
    xdg = tmp_path / "elsewhere"
    targets = vs.candidate_paths(
        platform_key="linux", home=tmp_path, env={"XDG_CONFIG_HOME": str(xdg)},
    )
    assert str(xdg / "Code" / "User" / "settings.json") in [t.path for t in targets]


def test_macos_paths(tmp_path: Path):
    targets = vs.candidate_paths(platform_key="darwin", home=tmp_path, env={})
    paths = [t.path for t in targets]
    assert (
        str(
            tmp_path
            / "Library"
            / "Application Support"
            / "Code"
            / "User"
            / "settings.json"
        )
        in paths
    )
    assert (
        str(
            tmp_path
            / "Library"
            / "Application Support"
            / "Code - Insiders"
            / "User"
            / "settings.json"
        )
        in paths
    )
    # macOS has no Flatpak; only the four native entries exist.
    assert len(targets) == len(vs.VARIANTS)


def test_windows_paths_use_appdata(tmp_path: Path):
    appdata = tmp_path / "AppData" / "Roaming"
    targets = vs.candidate_paths(
        platform_key="win32", home=tmp_path, env={"APPDATA": str(appdata)},
    )
    paths = [t.path for t in targets]
    assert str(appdata / "Code" / "User" / "settings.json") in paths
    assert str(appdata / "Cursor" / "User" / "settings.json") in paths
    assert len(targets) == len(vs.VARIANTS)


def test_windows_falls_back_to_home_appdata_when_env_is_missing(tmp_path: Path):
    targets = vs.candidate_paths(platform_key="win32", home=tmp_path, env={})
    assert str(
        tmp_path / "AppData" / "Roaming" / "Code" / "User" / "settings.json"
    ) in [t.path for t in targets]


def test_detect_targets_offers_only_existing_files(tmp_path: Path):
    real = tmp_path / ".config" / "VSCodium" / "User"
    real.mkdir(parents=True)
    (real / "settings.json").write_text("{}", encoding="utf-8")

    found = vs.detect_targets(platform_key="linux", home=tmp_path, env={})
    assert [t.app_id for t in found] == ["vscodium"]


def test_env_override_adds_a_target(tmp_path: Path):
    custom = tmp_path / "portable" / "settings.json"
    custom.parent.mkdir(parents=True)
    custom.write_text("{}", encoding="utf-8")

    found = vs.detect_targets(
        platform_key="linux",
        home=tmp_path,
        env={vs.ENV_TARGET_OVERRIDE: str(custom)},
    )
    assert [t.path for t in found] == [str(custom)]
    assert found[0].flavour == "override"


def test_env_override_does_not_duplicate_a_detected_variant(tmp_path: Path):
    real = tmp_path / ".config" / "Code" / "User"
    real.mkdir(parents=True)
    settings = real / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    found = vs.detect_targets(
        platform_key="linux",
        home=tmp_path,
        env={vs.ENV_TARGET_OVERRIDE: str(settings)},
    )
    assert len(found) == 1
    assert found[0].app_id == "code"


def test_detect_targets_with_no_arguments_resolves_and_never_raises():
    """The production call shape (no arguments) must not blow up anywhere.

    Deliberately asserts only the type: on a headless CI box the list is
    empty, on a developer machine it is not, and asserting anything about
    the maintainer's own editor configuration would make this test a
    property of one machine. `detect_targets` only stats paths — no test
    can assert "did not write" more strongly than the implementation being
    read-only, which is what the reviewer checks.
    """
    assert isinstance(vs.detect_targets(), list)


# ---------------------------------------------------------------------------
# Permissions — the token lands in a file this module locks down, or not at all
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason=POSIX_ONLY)
def test_written_file_and_backup_are_owner_only(settings_file: Path):
    settings_file.chmod(0o644)
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    backup = Path(result["backup_path"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600, (
        "the backup can hold the OLD token; it must be locked down too"
    )
    assert result["permissions"] == "owner_only"


def test_permission_failure_rolls_the_write_back(settings_file: Path, monkeypatch):
    """The Windows-ACL failure arm, driven on every OS.

    ``restrict_to_owner`` raising is exactly what a Windows box with a broken
    ``icacls`` produces. The contract is that the token does NOT stay on disk
    in a file we could not lock down — so the file goes back to its previous
    bytes and the caller gets a refusal.
    """
    before = sha(settings_file)
    original = vs.restrict_to_owner

    def fail_on_settings(path):
        if path.name == "settings.json":
            raise vs.PermissionHardeningError("icacls exited 5")
        return original(path)

    monkeypatch.setattr(vs, "restrict_to_owner", fail_on_settings)
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)

    assert result["ok"] is False
    assert result["reason"] == "not_lockable"
    assert sha(settings_file) == before, "the write must be rolled back"
    assert TOKEN not in settings_file.read_text(encoding="utf-8")


def test_permission_failure_on_a_new_file_removes_it(tmp_path: Path, monkeypatch):
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True)
    path = user / "settings.json"

    monkeypatch.setattr(
        vs,
        "restrict_to_owner",
        lambda p: (_ for _ in ()).throw(vs.PermissionHardeningError("no ACL")),
    )
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["ok"] is False
    assert not path.exists(), "a file we could not lock down must not survive"


def test_backup_permission_failure_aborts_before_the_write(
    settings_file: Path, monkeypatch,
):
    before = sha(settings_file)

    def fail_on_backup(path):
        if vs._BACKUP_STEM in path.name:
            raise vs.PermissionHardeningError("no ACL")
        return None

    monkeypatch.setattr(vs, "restrict_to_owner", fail_on_backup)
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    assert result["reason"] == "backup_not_lockable"
    assert sha(settings_file) == before
    assert not list(settings_file.parent.glob("settings.json.bak-*"))


def test_permissions_probe_is_a_tristate(settings_file: Path, monkeypatch):
    monkeypatch.setattr(
        vs, "owner_only_state", lambda p: (_ for _ in ()).throw(OSError("boom")),
    )
    assert vs._probe_permissions(settings_file) == "unknown"


# ---------------------------------------------------------------------------
# inspect_target
# ---------------------------------------------------------------------------


def test_inspect_reports_a_pointed_panel(settings_file: Path):
    vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-opus-5",
    )
    info = vs.inspect_target(settings_file, ports=(11436,))
    assert info["exists"] and info["parseable"] is True
    assert info["points_at_vco_gateway"] is True
    assert info["base_url"] == BASE_URL
    assert info["model"] == "claude-opus-5[1m]"
    assert info["discovery_enabled"] is True
    assert info["disable_login_prompt"] is True
    assert info["slot_overrides"] == []
    assert set(info["managed_keys_present"]) == set(vs.MANAGED_SETTINGS_KEYS)


def test_inspect_reports_unparseable_without_guessing(tmp_path: Path):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")
    info = vs.inspect_target(path)
    assert info["parseable"] is False
    assert info["points_at_vco_gateway"] is None, "unknown is not False"
    assert info["refusal_reason"] == "not_strict_json"


def test_inspect_of_a_missing_file(tmp_path: Path):
    info = vs.inspect_target(tmp_path / "settings.json")
    assert info["exists"] is False
    assert info["permissions"] == "unknown"


# ---------------------------------------------------------------------------
# CLI — stdout is a machine contract
# ---------------------------------------------------------------------------


def test_cli_inspect_emits_only_json(settings_file: Path, capsys):
    rc = vs.main(["inspect", "--path", str(settings_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out)["path"] == str(settings_file)


def test_cli_point_takes_the_token_from_env_never_argv(settings_file: Path, capsys, monkeypatch):
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    rc = vs.main(
        ["point", "--path", str(settings_file), "--base-url", BASE_URL],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert env_block(settings_file)["ANTHROPIC_AUTH_TOKEN"] == TOKEN


def test_cli_point_exits_nonzero_on_refusal(tmp_path: Path, capsys, monkeypatch):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC_WITH_COMMENT, encoding="utf-8")
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    rc = vs.main(["point", "--path", str(path), "--base-url", BASE_URL])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "not_strict_json"


def test_cli_point_with_a_vendor_model_exits_nonzero_with_the_reason(
    settings_file: Path, capsys, monkeypatch,
):
    """A script that asked for a Default and got none must not read exit 0.

    The routing keys ARE written (that is what `point` was asked to do); the
    non-zero exit and the payload's `refusal_reason` are how the caller
    learns the Default was declined.
    """
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    rc = vs.main(
        [
            "point",
            "--path",
            str(settings_file),
            "--base-url",
            BASE_URL,
            "--model",
            "claude-gw/glm-5.3",
        ],
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["status"] == "written"
    assert "claude-gw/glm-5.3" in payload["refusal_reason"]
    block = env_block(settings_file)
    assert vs.MODEL_KEY not in block
    assert block["ANTHROPIC_BASE_URL"] == BASE_URL


def test_cli_point_with_a_first_party_model_exits_zero(
    settings_file: Path, capsys, monkeypatch,
):
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    rc = vs.main(
        [
            "point",
            "--path",
            str(settings_file),
            "--base-url",
            BASE_URL,
            "--model",
            "claude-opus-5",
        ],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] is None
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-opus-5[1m]"


def test_cli_clear_default_emits_only_json(settings_file: Path, capsys, monkeypatch):
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    vs.main(["point", "--path", str(settings_file), "--base-url", BASE_URL,
             "--model", "claude-opus-5"])
    capsys.readouterr()
    rc = vs.main(["clear-default", "--path", str(settings_file)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "clear_default_model"
    assert payload["keys_removed"] == [vs.MODEL_KEY]
    assert vs.MODEL_KEY not in env_block(settings_file)


# ---------------------------------------------------------------------------
# probe_gateway — the tri-state the GUI's Start action keys on
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    """Stands in for ``http.client.HTTPConnection``. Records the target so
    the probe cannot quietly start asking a different host."""

    seen: list[tuple[str, int, str]] = []

    def __init__(self, host, port, timeout=None, *, answer=None, raises=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._answer = answer
        self._raises = raises

    def request(self, method, path):
        _FakeConnection.seen.append((self.host, self.port, path))
        if self._raises is not None:
            raise self._raises

    def getresponse(self):
        return self._answer

    def close(self):
        pass


def _fake_conn(monkeypatch, *, answer=None, raises=None):
    import functools
    import http.client

    _FakeConnection.seen = []
    monkeypatch.setattr(
        http.client,
        "HTTPConnection",
        functools.partial(_FakeConnection, answer=answer, raises=raises),
    )


def test_probe_gateway_running_only_for_our_service(monkeypatch):
    _fake_conn(
        monkeypatch,
        answer=_FakeResponse(
            b'{"ok": true, "service": "vct-model-gateway", "version": "0.2.94"}'
        ),
    )
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_RUNNING
    assert _FakeConnection.seen == [("127.0.0.1", 11436, "/health")], (
        "the probe asks loopback directly — never a proxy, never a hostname"
    )


def test_probe_gateway_foreign_answer_is_unreachable_not_running(monkeypatch):
    """The 2026-09-08 machine: a legacy scorer container owned port 11436.

    Calling that "running" would point the panel at somebody else's service
    under our name — and would suppress the port-collision fallback that
    picks a free port instead.
    """
    _fake_conn(monkeypatch, answer=_FakeResponse(b'{"service": "vco-model-router"}'))
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_UNREACHABLE


def test_probe_gateway_refused_is_stopped(monkeypatch):
    _fake_conn(monkeypatch, raises=ConnectionRefusedError(111, "Connection refused"))
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_STOPPED


def test_probe_gateway_timeout_is_unreachable_never_stopped(monkeypatch):
    import socket

    _fake_conn(monkeypatch, raises=socket.timeout("timed out"))
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_UNREACHABLE


def test_probe_gateway_http_error_is_unreachable(monkeypatch):
    _fake_conn(monkeypatch, answer=_FakeResponse(b"nope", status=503))
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_UNREACHABLE


def test_probe_gateway_unparseable_body_is_unreachable(monkeypatch):
    _fake_conn(monkeypatch, answer=_FakeResponse(b"<html>hi"))
    assert vs.probe_gateway(ports=(11436,)) == vs.GATEWAY_UNREACHABLE


def _state(monkeypatch, tmp_path, **files):
    """A scratch ``<vct_root>`` holding the named port files."""
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    monkeypatch.delenv(vs.PORT_ENV, raising=False)
    for name, value in files.items():
        basename = {
            "port": vs.PORT_BASENAME,
            "last": vs.LAST_PORT_BASENAME,
        }[name]
        (root / basename).write_text(f"{value}\n", encoding="utf-8")
    return root


def test_is_vco_gateway_base_url_defaults_to_the_resolved_port_not_the_shipped_one(
    monkeypatch, tmp_path,
):
    """Review R3-6: the default argument must not restore the pre-R1-2 rule.

    A caller that omits `ports` used to be told "11436 is ours" whatever the
    machine actually resolved — including the uninstall-time reset, which
    would then skip our own panel on 11437 and volunteer to reset somebody
    else's on 11436.
    """
    _state(monkeypatch, tmp_path, last=11437)
    assert vs.is_vco_gateway_base_url("http://127.0.0.1:11437") is True
    assert vs.is_vco_gateway_base_url("http://127.0.0.1:11436") is False
    # An explicit list still wins — a caller with a better answer keeps it.
    assert vs.is_vco_gateway_base_url("http://127.0.0.1:11436", ports=(11436,)) is True
    # ...including an EMPTY one, which says "no port here is ours".
    assert vs.is_vco_gateway_base_url("http://127.0.0.1:11437", ports=()) is False


def test_resolve_gateway_ports_answers_with_the_resolved_port_alone(
    monkeypatch, tmp_path,
):
    """Review R1-2: the shipped default is not permanently 'ours'.

    Returning both the resolved port AND 11436 is what let a panel pointed
    at a legacy container on 11436 read as a healthy VCO gateway while the
    real one ran on 11437.
    """
    _state(monkeypatch, tmp_path, port=11437)
    assert vs.resolve_gateway_ports() == (11437,)
    assert vs.DEFAULT_GATEWAY_PORT not in vs.resolve_gateway_ports()


def test_the_last_started_port_survives_the_daemons_clean_exit(monkeypatch, tmp_path):
    """Review R2-2, the whole point of the last-port record.

    The daemon UNLINKS its port file when it exits cleanly. Without this
    record, a gateway that had moved to 11437 (because something else held
    11436) is forgotten the moment it stops: the panel it wrote reads as an
    unmanaged prototype endpoint, the uninstall reset walks past it, and a
    Services "point" writes our host token into a base URL naming whatever
    owns the default port.
    """
    _state(monkeypatch, tmp_path, last=11437)
    assert vs.resolve_gateway_ports() == (11437,)


def test_the_live_port_file_beats_the_last_port_record(monkeypatch, tmp_path):
    """Order matters: a RUNNING gateway's own file is better evidence than
    the launcher's memory of the last one it started."""
    _state(monkeypatch, tmp_path, port=11440, last=11437)
    assert vs.resolve_gateway_ports() == (11440,)


def test_the_env_pin_beats_every_file(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, port=11440, last=11437)
    monkeypatch.setenv(vs.PORT_ENV, "11999")
    assert vs.resolve_gateway_ports() == (11999,)


def test_a_corrupt_or_out_of_range_port_file_is_ignored_not_trusted(
    monkeypatch, tmp_path,
):
    root = _state(monkeypatch, tmp_path)
    for bad in ("not-a-port", "", "0", "70000", "-1"):
        (root / vs.PORT_BASENAME).write_text(bad, encoding="utf-8")
        (root / vs.LAST_PORT_BASENAME).write_text(bad, encoding="utf-8")
        assert vs.resolve_gateway_ports() == (vs.DEFAULT_GATEWAY_PORT,), bad


def test_resolve_gateway_ports_falls_back_only_when_nothing_resolved(
    monkeypatch, tmp_path,
):
    """LEAVE-ALONE half: an uninstalled gateway (no importable package) must
    still recognise the documented port, or the uninstall-time reset would
    walk past the panel it is meant to clean up."""
    import builtins

    _state(monkeypatch, tmp_path)
    real_import = builtins.__import__

    def no_model_router(name, *a, **k):
        if name.startswith("model_router"):
            raise ImportError("simulated: gateway package uninstalled")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_model_router)
    assert vs.resolve_gateway_ports() == (vs.DEFAULT_GATEWAY_PORT,)


def test_panel_endpoint_port_is_the_panels_own_loopback_port_or_nothing():
    """Review R2-3: the panel's endpoint is a different question from the
    gateway's port, and a remote endpoint has no local port at all."""
    assert vs.panel_endpoint_port("http://127.0.0.1:11436") == 11436
    assert vs.panel_endpoint_port("http://localhost:8787") == 8787
    for no_port in (
        None,
        "",
        "https://api.z.ai/api/anthropic",
        "http://127.0.0.1/no-port",
        "not a url",
    ):
        assert vs.panel_endpoint_port(no_port) is None, no_port


def test_probe_gateway_states_are_the_three_the_gui_switches_on():
    assert vs.GATEWAY_STATES == ("running", "stopped", "unreachable")


def test_cli_reset_if_gateway_leaves_foreign_alone(tmp_path: Path, capsys):
    path = tmp_path / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({vs.ENV_BLOCK_KEY: {"ANTHROPIC_BASE_URL": "https://api.z.ai"}}),
        encoding="utf-8",
    )
    rc = vs.main(["reset-if-gateway", "--path", str(path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "left_alone"


def test_resolve_host_token_prefers_env(monkeypatch):
    monkeypatch.setenv(vs.ENV_TOKEN, "from-env")
    assert vs.resolve_host_token() == "from-env"


def test_resolve_host_token_refuses_actionably_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv(vs.ENV_TOKEN, raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    with pytest.raises(vs.SettingsRefused) as exc:
        vs.resolve_host_token()
    assert exc.value.reason == "no_host_token"
    assert "Start the model gateway" in exc.value.message
