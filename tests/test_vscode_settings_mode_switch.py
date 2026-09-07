# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The Multimodel <-> Remote Control switch in ``vco_lib.vscode_settings``.

Remote Control is endpoint-gated (Claude Code >= 2.1.196 refuses it whenever
``ANTHROPIC_BASE_URL`` is not api.anthropic.com), and the extension's env
block is machine-scoped, so the user has one mode at a time and a switch to
flip. These tests pin the four things the switch must get right:

1. The ``remote-control`` leg drops ONLY what cannot resolve natively — the
   routing keys, the login-prompt key, and model/slot values that name a
   gateway-only model — and keeps every ``claude-*`` slot the user set.
2. What it drops is STASHED, minus the token: the stash names the routing
   keys but never carries the credential (asserted on the stash's bytes).
3. The ``multimodel`` leg puts the stashed choices back, decorated, and
   clears the stash. Both legs are idempotent, and a second click leaves the
   stash alone.
4. A file the writer cannot parse is refused with the same JSONC message the
   other actions use, byte-for-byte untouched.

Every test drives its own ``stash=`` path under ``tmp_path`` (the default
resolves under ``VCT_STATE_DIR``, which conftest already redirects); the
real user's VS Code settings and launcher state are never in scope.
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from vco_lib import vscode_settings as vs

TOKEN = "mode-switch-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"
GLM = "claude-gw/glm-5.3"
GLM_1M = "claude-gw/glm-5.3[1m]"
FLASH_1M = "claude-gw/glm-5.3-flash[1m]"
CLAUDE_45 = "claude-opus-4-5"

POSIX_ONLY = "POSIX mode bits do not exist on Windows"

JSONC = '{\n    // a note\n    "editor.fontSize": 13\n}\n'


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(tmp_path: Path, payload: dict) -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True, exist_ok=True)
    path = user / "settings.json"
    path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    return path


def _pointed(tmp_path: Path, extra_env: dict | None = None, model: str | None = GLM_1M) -> Path:
    """A file the ``point`` action would have produced, plus ``extra_env``."""
    env: dict = {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    if model:
        env[vs.MODEL_KEY] = model
    env.update(extra_env or {})
    return _settings(
        tmp_path,
        {
            "editor.fontSize": 13,
            vs.ENV_BLOCK_KEY: env,
            vs.LOGIN_PROMPT_KEY: True,
        },
    )


def _doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _block(path: Path) -> dict:
    return _doc(path).get(vs.ENV_BLOCK_KEY, {})


@pytest.fixture()
def stash(tmp_path: Path) -> Path:
    return tmp_path / "state" / "model-gateway" / "vscode-mode-stash.json"


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """The context-table loader is cached at module level; the seed rows the
    decoration and the vendor test read must come from THIS process's env."""
    vs._CONTEXT_TABLE_LOADER = None
    yield
    vs._CONTEXT_TABLE_LOADER = None


class _Row:
    def __init__(self, vendor: str) -> None:
        self.vendor = vendor


class _Table:
    """Stub with the two methods the writer uses."""

    def __init__(self, rows: dict[str, str], one_m: set[str] = frozenset()) -> None:
        self._rows = rows
        self._one_m = set(one_m)

    def lookup(self, model_id: str):
        v = self._rows.get(model_id)
        return _Row(v) if v else None

    def advertise_1m(self, model_id: str) -> bool:
        return model_id in self._one_m


# ---------------------------------------------------------------------------
# Constants pinned to their sources
# ---------------------------------------------------------------------------


def test_first_party_vendor_matches_the_gateway_registry():
    from model_router.vendors import ANTHROPIC_FAMILY

    assert vs.FIRST_PARTY_VENDOR == ANTHROPIC_FAMILY.family_id


def test_stash_lives_in_the_gateways_state_subdir():
    from model_router import config as gw

    assert vs.STASH_SUBDIR == gw._STATE_SUBDIR
    assert vs.stash_path().parent == gw.export_path().parent


# ---------------------------------------------------------------------------
# The pure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("claude-gw/glm-5.3", True),
        ("claude-gw/glm-5.3[1m]", True),
        ("claude-gw/claude-opus-5", True),  # namespaced: only the gateway resolves it
        ("glm-5.3", True),  # bare vendor id, table says zai
        ("glm-5.3[1m]", True),
        ("claude-opus-5", False),  # table says anthropic
        ("claude-opus-4-5", False),  # unknown to the table: MAY resolve, keep it
        ("gpt-x", False),
        ("", False),
        ("   ", False),
        (None, False),
        (42, False),
    ],
)
def test_is_gateway_only_model(value, expected):
    table = _Table({"glm-5.3": "zai", "claude-opus-5": "anthropic"})
    assert vs.is_gateway_only_model(value, table) is expected


def test_classifier_without_a_table_knows_only_the_namespace():
    assert vs.is_gateway_only_model("claude-gw/glm-5.3", None) is True
    assert vs.is_gateway_only_model("glm-5.3", None) is False


def test_classifier_never_raises_on_a_broken_table():
    class Broken:
        def lookup(self, _):
            raise RuntimeError("boom")

    assert vs.is_gateway_only_model("glm-5.3", Broken()) is False


# ---------------------------------------------------------------------------
# remote-control leg
# ---------------------------------------------------------------------------


def test_remote_control_keeps_claude_slots_and_stashes_gateway_ones(
    tmp_path: Path, stash: Path,
):
    path = _pointed(
        tmp_path,
        {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
            "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
            "MY_OWN_KEY": "kept",
        },
    )
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] and out["status"] == "written", out

    doc = _doc(path)
    assert vs.LOGIN_PROMPT_KEY not in doc
    block = doc[vs.ENV_BLOCK_KEY]
    for key in vs.ROUTING_KEYS:
        assert key not in block
    assert vs.MODEL_KEY not in block, "a claude-gw/ default cannot resolve natively"
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in block
    assert block["CLAUDE_CODE_SUBAGENT_MODEL"] == CLAUDE_45, "the user's Claude slot survives"
    assert block["MY_OWN_KEY"] == "kept"
    assert doc["editor.fontSize"] == 13

    assert set(out["keys_removed"]) == set(vs.ROUTING_KEYS) | {vs.LOGIN_PROMPT_KEY}
    assert out["values_stashed"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL", vs.MODEL_KEY]
    assert out["slot_overrides_preserved"] == ["CLAUDE_CODE_SUBAGENT_MODEL"]
    assert out["restart_required"] is True
    assert out["stash_present"] is True and out["stash_path"] == str(stash)


def test_stash_names_the_routing_keys_but_never_carries_the_token(
    tmp_path: Path, stash: Path,
):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)

    raw = stash.read_bytes()
    assert TOKEN.encode() not in raw, "the host token must never reach the stash"
    doc = json.loads(raw)
    assert doc["schema_version"] == 1
    assert doc["settings_path"] == str(path)
    assert set(doc["routing_keys"]) == set(vs.ROUTING_KEYS), "names, all four"
    assert set(doc["routing_values"]) == {
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    }, "values only for the non-secret keys"
    assert doc["routing_values"]["ANTHROPIC_BASE_URL"] == BASE_URL
    assert doc["login_prompt_removed"] is True
    assert doc["values"] == {
        vs.MODEL_KEY: GLM_1M,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
    }


@pytest.mark.skipif(sys.platform == "win32", reason=POSIX_ONLY)
def test_stash_is_owner_only(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path)
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert stat.S_IMODE(stash.stat().st_mode) == 0o600


def test_remote_control_drops_the_env_block_it_emptied(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path, model=None)
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["status"] == "written"
    doc = _doc(path)
    assert vs.ENV_BLOCK_KEY not in doc, "an empty husk is not a setting"
    assert vs.LOGIN_PROMPT_KEY not in doc
    assert doc == {"editor.fontSize": 13}


def test_remote_control_is_idempotent_and_leaves_the_stash_alone(
    tmp_path: Path, stash: Path,
):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    first = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert first["status"] == "written"
    file_sha, stash_sha = _sha(path), _sha(stash)

    second = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert second["ok"] and second["status"] == "unchanged"
    assert _sha(path) == file_sha
    assert _sha(stash) == stash_sha, "a second click must not clobber the first click's stash"
    assert second["stash_present"] is True


def test_remote_control_on_a_stock_file_is_a_no_op_and_writes_no_stash(
    tmp_path: Path, stash: Path,
):
    path = _settings(tmp_path, {"editor.fontSize": 13})
    before = _sha(path)
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] and out["status"] == "unchanged"
    assert _sha(path) == before
    assert not stash.exists()


def test_remote_control_on_a_missing_file_is_a_no_op(tmp_path: Path, stash: Path):
    out = vs.set_mode(tmp_path / "nope" / "settings.json", vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] and out["status"] == "unchanged"
    assert not stash.exists()


def test_remote_control_heals_a_plain_1m_claude_slot(tmp_path: Path, stash: Path):
    """The stock client assumes 200K for a plain ``claude-opus-5``; the
    ``[1m]`` hint is its own convention, so a preserved 1M Claude slot
    carries it. Reads the shipped seed's Claude 5 rows."""
    path = _pointed(tmp_path, {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5"})
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["status"] == "written"
    assert _block(path)["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-opus-5[1m]"
    assert out["values_healed"] == ["CLAUDE_CODE_SUBAGENT_MODEL"]
    assert "CLAUDE_CODE_SUBAGENT_MODEL" in out["message"]


def test_remote_control_refuses_jsonc_byte_identical(tmp_path: Path, stash: Path):
    path = tmp_path / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC, encoding="utf-8")
    before = _sha(path)
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] is False and out["status"] == "refused"
    assert out["reason"] == "not_strict_json"
    assert out["message"].startswith(vs.describe_json_failure(JSONC, "")[:40])
    assert "JSONC" in out["message"]
    assert vs.ENV_BLOCK_KEY in out["message"] and vs.LOGIN_PROMPT_KEY in out["message"]
    assert _sha(path) == before
    assert not stash.exists()


def test_refused_settings_write_puts_the_previous_stash_back(
    tmp_path: Path, stash: Path, monkeypatch,
):
    """Stash-first ordering: when the settings write is rolled back, the
    stash must return to what it was, or the two disagree forever."""
    from model_router.fileperms import PermissionHardeningError

    stash.parent.mkdir(parents=True)
    stash.write_text('{"schema_version": 1, "settings_path": "x", "values": {}}\n')
    previous = stash.read_bytes()
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    before = _sha(path)

    real = vs.restrict_to_owner

    def failing(p: Path) -> None:
        if Path(p) == path:
            raise PermissionHardeningError("simulated")
        real(p)

    monkeypatch.setattr(vs, "restrict_to_owner", failing)
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] is False and out["reason"] == "not_lockable"
    assert _sha(path) == before
    assert stash.read_bytes() == previous


def test_refused_settings_write_removes_a_stash_it_created(
    tmp_path: Path, stash: Path, monkeypatch,
):
    from model_router.fileperms import PermissionHardeningError

    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    real = vs.restrict_to_owner

    def failing(p: Path) -> None:
        if Path(p) == path:
            raise PermissionHardeningError("simulated")
        real(p)

    monkeypatch.setattr(vs, "restrict_to_owner", failing)
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] is False
    assert not stash.exists()


# ---------------------------------------------------------------------------
# multimodel leg
# ---------------------------------------------------------------------------


def test_round_trip_restores_the_exact_gateway_choices(tmp_path: Path, stash: Path):
    path = _pointed(
        tmp_path,
        {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
            "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
            "MY_OWN_KEY": "kept",
        },
    )
    original = _block(path)
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in _block(path)

    out = vs.set_mode(
        path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash,
    )
    assert out["ok"] and out["status"] == "written", out
    assert out["mode"] == vs.MODE_MULTIMODEL
    assert out["keys_restored"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL", vs.MODEL_KEY]
    assert "Restored" in out["message"]
    assert _block(path) == original, "the round trip is exact"
    assert _doc(path)[vs.LOGIN_PROMPT_KEY] is True
    assert not stash.exists(), "restored choices are no longer stashed"
    assert out["stash_present"] is False


def test_restored_plain_values_are_decorated(tmp_path: Path, stash: Path):
    """A stash made by a pre-seed VCO could hold a plain ``claude-gw/glm-5.3``;
    the restore passes through the same [1m] loop as every other value."""
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash"}, model=GLM)
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert json.loads(stash.read_text())["values"][vs.MODEL_KEY] == GLM

    out = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["status"] == "written"
    block = _block(path)
    assert block[vs.MODEL_KEY] == GLM_1M
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == FLASH_1M
    assert set(out["values_healed"]) == {vs.MODEL_KEY, "ANTHROPIC_DEFAULT_HAIKU_MODEL"}


def test_multimodel_without_a_stash_is_a_plain_point(tmp_path: Path, stash: Path):
    path = _settings(tmp_path, {"editor.fontSize": 13})
    out = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"] and out["status"] == "written"
    assert out["keys_restored"] == []
    assert out["stash_skipped_reason"] is None
    block = _block(path)
    assert set(vs.ROUTING_KEYS) <= set(block)
    assert block["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert vs.MODEL_KEY not in block, "no stash, no model: the user picks in /model"
    assert not any(k in block for k in vs.SLOT_OVERRIDE_KEYS)


def test_multimodel_is_idempotent(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    sha = _sha(path)
    again = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert again["ok"] and again["status"] == "unchanged"
    assert _sha(path) == sha
    assert not stash.exists()


def test_full_round_trip_twice_ends_where_it_started(tmp_path: Path, stash: Path):
    path = _pointed(
        tmp_path,
        {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M, "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45},
    )
    start = _doc(path)
    for _ in range(2):
        vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
        vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert _doc(path) == start
    assert not stash.exists()


def test_stash_for_another_editor_is_left_alone(tmp_path: Path, stash: Path):
    """One stash path, two editors: Cursor's choices must not land in VS
    Code's file."""
    cursor = _pointed(tmp_path / "cursor", {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    vs.set_mode(cursor, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert stash.exists()

    code = _settings(tmp_path / "code", {"editor.fontSize": 13})
    out = vs.set_mode(code, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"] and out["status"] == "written"
    assert out["keys_restored"] == []
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in _block(code)
    assert out["stash_skipped_reason"] and str(cursor) in out["stash_skipped_reason"]
    assert stash.exists(), "the stash still belongs to the other file"
    assert out["stash_present"] is True


def test_a_tampered_stash_cannot_inject_routing_or_unknown_keys(
    tmp_path: Path, stash: Path,
):
    path = _settings(tmp_path, {"editor.fontSize": 13})
    stash.parent.mkdir(parents=True)
    stash.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "settings_path": str(path),
                "values": {
                    "ANTHROPIC_AUTH_TOKEN": "attacker-token",
                    "ANTHROPIC_BASE_URL": "https://evil.example",
                    "SOME_OTHER_KEY": "x",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
                    "ANTHROPIC_SMALL_FAST_MODEL": "",
                },
            }
        )
    )
    out = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"]
    block = _block(path)
    assert block["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert block["ANTHROPIC_BASE_URL"] == BASE_URL
    assert "SOME_OTHER_KEY" not in block
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in block, "an empty value is not restored"
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == FLASH_1M
    assert out["keys_restored"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]


def test_an_unreadable_stash_is_reported_and_kept(tmp_path: Path, stash: Path):
    path = _settings(tmp_path, {"editor.fontSize": 13})
    stash.parent.mkdir(parents=True)
    stash.write_text("{not json", encoding="utf-8")
    out = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"] and out["status"] == "written"
    assert "not valid JSON" in out["stash_skipped_reason"]
    assert stash.exists(), "never delete what we could not read"


def test_multimodel_refusal_keeps_the_stash(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    path.write_text(JSONC, encoding="utf-8")
    out = vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"] is False and out["reason"] == "not_strict_json"
    assert stash.exists()
    assert out["stash_present"] is True


def test_explicit_model_beats_a_restored_one(tmp_path: Path, stash: Path):
    """``point_at_gateway`` contract: the caller's explicit choice wins."""
    path = _settings(tmp_path, {"editor.fontSize": 13})
    out = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, model=GLM_1M,
        restore_env={vs.MODEL_KEY: FLASH_1M},
    )
    assert _block(path)[vs.MODEL_KEY] == GLM_1M
    assert vs.MODEL_KEY in out["keys_written"]
    assert vs.MODEL_KEY not in out["keys_restored"]
    assert vs.MODEL_KEY not in out["keys_preserved"]


def test_set_mode_rejects_an_unknown_mode(tmp_path: Path):
    with pytest.raises(ValueError):
        vs.set_mode(tmp_path / "s.json", "turbo")
    with pytest.raises(ValueError):
        vs.set_mode(tmp_path / "s.json", vs.MODE_MULTIMODEL)  # no base_url/token


# ---------------------------------------------------------------------------
# --get
# ---------------------------------------------------------------------------


def test_panel_mode_multimodel(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    out = vs.panel_mode(path, ports=(11436,), stash=stash)
    assert out["mode"] == vs.MODE_MULTIMODEL
    assert out["base_url"] == BASE_URL
    assert out["model"] == GLM_1M
    assert out["slot_overrides"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
    assert out["stash_present"] is False
    assert "Remote Control is unavailable" in out["detail"]


def test_panel_mode_remote_control_after_the_switch(tmp_path: Path, stash: Path):
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    out = vs.panel_mode(path, ports=(11436,), stash=stash)
    assert out["mode"] == vs.MODE_REMOTE_CONTROL
    assert out["base_url"] is None
    assert out["stash_present"] is True
    assert "stashed" in out["detail"]


def test_panel_mode_stock_file_and_missing_file(tmp_path: Path, stash: Path):
    path = _settings(tmp_path, {"editor.fontSize": 13})
    assert vs.panel_mode(path, stash=stash)["mode"] == vs.MODE_REMOTE_CONTROL
    missing = vs.panel_mode(tmp_path / "nope.json", stash=stash)
    assert missing["mode"] == vs.MODE_REMOTE_CONTROL
    assert "No settings file" in missing["detail"]


def test_panel_mode_unmanaged_for_a_foreign_endpoint(tmp_path: Path, stash: Path):
    path = _settings(
        tmp_path,
        {vs.ENV_BLOCK_KEY: {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}},
    )
    out = vs.panel_mode(path, ports=(11436,), stash=stash)
    assert out["mode"] == vs.MODE_UNMANAGED
    assert "leaves it alone" in out["detail"]


def test_panel_mode_unmanaged_for_a_different_local_port(tmp_path: Path, stash: Path):
    path = _settings(
        tmp_path, {vs.ENV_BLOCK_KEY: {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}},
    )
    assert vs.panel_mode(path, ports=(11436,), stash=stash)["mode"] == vs.MODE_UNMANAGED


def test_panel_mode_unparseable_guesses_nothing(tmp_path: Path, stash: Path):
    path = tmp_path / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC, encoding="utf-8")
    out = vs.panel_mode(path, stash=stash)
    assert out["mode"] == vs.MODE_UNPARSEABLE
    assert "JSONC" in out["detail"]
    assert out["base_url"] is None


# ---------------------------------------------------------------------------
# CLI — stdout is the launcher's machine contract
# ---------------------------------------------------------------------------


def test_cli_mode_get_emits_only_json(tmp_path: Path, capsys):
    path = _pointed(tmp_path)
    rc = vs.main(["mode", "--get", "--path", str(path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == vs.MODE_MULTIMODEL and out["path"] == str(path)


def test_cli_mode_set_remote_control(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    rc = vs.main(["mode", "--set", "remote-control", "--path", str(path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["mode"] == vs.MODE_REMOTE_CONTROL
    stash = tmp_path / "state" / vs.STASH_SUBDIR / vs.STASH_BASENAME
    assert stash.is_file(), "the default stash path is under the state root"
    assert TOKEN.encode() not in stash.read_bytes()


def test_cli_mode_set_multimodel_takes_the_token_from_env_never_argv(
    tmp_path: Path, capsys, monkeypatch,
):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(vs.ENV_TOKEN, TOKEN)
    path = _pointed(tmp_path, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M})
    assert vs.main(["mode", "--set", "remote-control", "--path", str(path)]) == 0
    capsys.readouterr()
    rc = vs.main(
        ["mode", "--set", "multimodel", "--path", str(path), "--base-url", BASE_URL],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["mode"] == vs.MODE_MULTIMODEL
    assert payload["keys_restored"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL", vs.MODEL_KEY]
    assert _block(path)["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert not (tmp_path / "state" / vs.STASH_SUBDIR / vs.STASH_BASENAME).exists()


def test_cli_mode_set_multimodel_refuses_actionably_without_a_token(
    tmp_path: Path, capsys, monkeypatch,
):
    monkeypatch.delenv(vs.ENV_TOKEN, raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    path = _settings(tmp_path, {"editor.fontSize": 13})
    before = _sha(path)
    rc = vs.main(["mode", "--set", "multimodel", "--path", str(path)])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "no_host_token"
    assert "Start the model gateway" in payload["message"]
    assert _sha(path) == before


def test_cli_mode_set_exits_nonzero_on_refusal(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(JSONC, encoding="utf-8")
    rc = vs.main(["mode", "--set", "remote-control", "--path", str(path)])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "not_strict_json"


def test_cli_mode_needs_get_or_set(tmp_path: Path):
    with pytest.raises(SystemExit):
        vs.main(["mode", "--path", str(tmp_path / "s.json")])
    with pytest.raises(SystemExit):
        vs.main(["mode", "--set", "turbo", "--path", str(tmp_path / "s.json")])


def test_second_remote_control_pass_merges_into_the_first_stash(
    tmp_path: Path, stash: Path,
):
    """Review R1 finding 6: already on stock, the user hand-types a gateway-only
    id into a slot, and remote-control runs again (CLI has no "already there"
    guard). The stash must GAIN that slot, not lose the first pass's model."""
    path = _pointed(tmp_path)
    first = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert first["status"] == "written"
    first_doc = json.loads(stash.read_text(encoding="utf-8"))
    assert vs.MODEL_KEY in first_doc["values"]
    # Hand-typed gateway id while on stock.
    doc = _doc(path)
    doc.setdefault(vs.ENV_BLOCK_KEY, {})["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = FLASH_1M
    path.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    second = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert second["status"] == "written"
    merged = json.loads(stash.read_text(encoding="utf-8"))
    assert merged["values"][vs.MODEL_KEY] == first_doc["values"][vs.MODEL_KEY]
    assert merged["values"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == FLASH_1M
    assert set(merged["routing_keys"]) >= set(first_doc["routing_keys"])
    assert merged["login_prompt_removed"] is True
    # A stash for ANOTHER settings file is never merged into.
    other = _pointed(tmp_path / "other")
    vs.set_mode(other, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert json.loads(stash.read_text(encoding="utf-8"))["settings_path"] == str(other)
