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
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-gw/glm-5.3",
    )
    # glm-5.3 is 1M-windowed in the shipped context table, so the written
    # id carries the client's [1m] hint (R41 decoration; see
    # tests/test_v0292_vscode_settings_1m_decoration.py).
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-gw/glm-5.3[1m]"


def test_default_model_is_glm_5_3_not_flash():
    assert vs.DEFAULT_GATEWAY_MODEL == "claude-gw/glm-5.3"
    assert "flash" not in vs.DEFAULT_GATEWAY_MODEL
    # Never a pre-5.3 version either.
    for older in ("glm-5.2", "glm-5.1", "glm-5-turbo", "glm-5", "glm-4"):
        assert not vs.DEFAULT_GATEWAY_MODEL.endswith(older)


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
    bare = vs.DEFAULT_GATEWAY_MODEL.split("/", 1)[1]
    assert bare in models, f"{bare} is not a row in {seed.name}"


# ---------------------------------------------------------------------------
# Merge, never replace
# ---------------------------------------------------------------------------


def test_rerun_preserves_the_users_model_choice(settings_file: Path):
    """The defect the field prototype had: re-sync reset ANTHROPIC_MODEL."""
    vs.point_at_gateway(
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-gw/glm-5.3",
    )
    result = vs.point_at_gateway(settings_file, base_url=BASE_URL, token=TOKEN)
    # Preserved AND window-correct: the re-run keeps the [1m] decoration
    # idempotently instead of letting the value drift back to plain.
    assert env_block(settings_file)[vs.MODEL_KEY] == "claude-gw/glm-5.3[1m]"
    assert vs.MODEL_KEY in result["keys_preserved"]


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


def test_resolve_gateway_ports_always_includes_the_documented_default():
    ports = vs.resolve_gateway_ports()
    assert vs.DEFAULT_GATEWAY_PORT in ports


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
        settings_file, base_url=BASE_URL, token=TOKEN, model="claude-gw/glm-5.3",
    )
    info = vs.inspect_target(settings_file, ports=(11436,))
    assert info["exists"] and info["parseable"] is True
    assert info["points_at_vco_gateway"] is True
    assert info["base_url"] == BASE_URL
    assert info["model"] == "claude-gw/glm-5.3[1m]"
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
