# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the gateway reads a Claude login from the macOS Keychain.

On a Mac Claude Code keeps its OAuth login in the login Keychain (the
credentials file is only its fallback), so a file-only reader saw no login at
all. The reader now falls back, on macOS only, to the item Claude Code writes
(``Claude Code-credentials`` / ``…-<sha256(config dir)[:8]>``, account
``$USER``) through ``security find-generic-password … -w``.

A stand-in ``security`` on PATH (this machine has no ``/usr/bin/security``)
records its argv and answers found / not found / locked from a canned JSON.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import time

import pytest

from model_router import account_refresh as ar
from model_router import macos_keychain as kc
from model_router.accounts import AccountService
from model_router.auth import OAuthReader

TOKEN = "keychain-oauth-token-synthetic"
RENEWED = "keychain-oauth-token-renewed-synthetic"

_FAKE = """#!{python}
import json, os, sys
d = os.environ["FAKE_SECURITY_DIR"]
with open(os.path.join(d, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
mode = open(os.path.join(d, "mode")).read().strip()
if mode == "not_found":
    sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\\n")
    sys.exit(44)
if mode == "locked":
    sys.stderr.write("security: SecKeychainItemCopyContent: User interaction is not allowed.\\n")
    sys.exit(36)
service = sys.argv[sys.argv.index("-s") + 1]
items = json.load(open(os.path.join(d, "items.json")))
if service not in items:
    sys.stderr.write("security: The specified item could not be found in the keychain.\\n")
    sys.exit(44)
sys.stdout.write(items[service] + "\\n")
"""


@pytest.fixture
def fake_security(tmp_path, monkeypatch):
    if os.path.isfile(kc.SECURITY_BIN):
        pytest.skip("a real /usr/bin/security is present (macOS): the stand-in would be bypassed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "security"
    exe.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "fake-security"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SECURITY_DIR", str(state))
    monkeypatch.setenv("USER", "alice")

    class Fake:
        def mode(self, value: str) -> None:
            (state / "mode").write_text(value)

        def items(self, mapping: dict) -> None:
            (state / "items.json").write_text(json.dumps(mapping))

        def calls(self) -> list:
            path = state / "argv.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    fake = Fake()
    fake.mode("found")
    fake.items({})
    return fake


def login_json(token: str, expires_in_s: float = 3600) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token,
                                         "expiresAt": int((time.time() + expires_in_s) * 1000)}})


def hashed(config_dir: str) -> str:
    return "Claude Code-credentials-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]


# ── naming (Claude Code 2.1.312's own rule) ──────────────────────────────


def test_service_and_account_naming():
    assert kc.service_name(None) == "Claude Code-credentials"
    assert kc.service_name("") == "Claude Code-credentials"
    assert kc.service_name("/Users/a/.claude-work") == hashed("/Users/a/.claude-work")
    # NFC: a decomposed é names the same item as the composed one.
    assert kc.service_name("/Users/ré") == kc.service_name("/Users/ré")
    assert kc.account_name({"USER": "alice"}) == "alice"
    assert kc.account_name({"USER": "a b"}) == "claude-code-user"


# ── the reader on macOS ──────────────────────────────────────────────────


def test_the_users_own_login_is_read_from_the_unsuffixed_item(tmp_path, fake_security, caplog):
    fake_security.items({"Claude Code-credentials": login_json(TOKEN)})
    reader = OAuthReader(tmp_path / ".claude" / ".credentials.json",
                         keychain_config_dir=None, platform="darwin")
    with caplog.at_level("DEBUG"):
        state = reader.read()
    assert state.present and state.token == TOKEN and state.state == "present"
    argv = fake_security.calls()[-1]
    assert argv == ["find-generic-password", "-a", "alice", "-w", "-s", "Claude Code-credentials"]
    assert TOKEN not in caplog.text, "a token value is never logged"
    assert "Claude Code-credentials" in caplog.text


def test_an_added_account_reads_the_item_named_after_its_config_dir(tmp_path, fake_security):
    cfg = tmp_path / "cfg-work"
    fake_security.items({hashed(str(cfg)): login_json(TOKEN)})
    reader = OAuthReader(cfg / ".credentials.json", platform="darwin")  # derived dir
    assert reader.read().token == TOKEN
    assert fake_security.calls()[-1][-1] == hashed(str(cfg))


def test_a_file_login_is_used_without_asking_the_keychain(tmp_path, fake_security):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(login_json("file-token-synthetic"))
    reader = OAuthReader(cfg / ".credentials.json", platform="darwin")
    assert reader.read().token == "file-token-synthetic"
    assert fake_security.calls() == []


def test_a_missing_item_is_no_login(tmp_path, fake_security):
    fake_security.mode("not_found")
    state = OAuthReader(tmp_path / "c" / ".credentials.json", platform="darwin").read()
    assert not state.present and state.state == "absent"


def test_a_locked_keychain_is_no_login_and_says_so(tmp_path, fake_security):
    fake_security.mode("locked")
    state = OAuthReader(tmp_path / "c" / ".credentials.json", platform="darwin").read()
    assert not state.present and state.state == "unreadable"
    assert "Keychain" in (state.problem or "")
    assert TOKEN not in (state.problem or "")


def test_an_expired_item_is_expired(tmp_path, fake_security):
    cfg = tmp_path / "c"
    fake_security.items({hashed(str(cfg)): login_json(TOKEN, expires_in_s=-60)})
    state = OAuthReader(cfg / ".credentials.json", platform="darwin").read()
    assert state.state == "expired" and state.token is None


def test_health_reads_never_run_security_and_answers_are_cached(tmp_path, fake_security):
    cfg = tmp_path / "c"
    fake_security.items({hashed(str(cfg)): login_json(TOKEN)})
    reader = OAuthReader(cfg / ".credentials.json", platform="darwin")
    assert not reader.read(cached_only=True).present  # nothing cached yet
    assert fake_security.calls() == [], "/health never runs a subprocess"
    reader.read()
    reader.read()
    assert reader.read(cached_only=True).token == TOKEN
    assert len(fake_security.calls()) == 1, "one Keychain read per cache window"
    reader.read(fresh=True)
    assert len(fake_security.calls()) == 2


def test_off_macos_the_keychain_is_never_asked(tmp_path, fake_security):
    cfg = tmp_path / "c"
    fake_security.items({hashed(str(cfg)): login_json(TOKEN)})
    state = OAuthReader(cfg / ".credentials.json", platform="linux").read()
    assert not state.present
    assert fake_security.calls() == []
    # ...and the default follows the real platform (this suite runs off macOS).
    if sys.platform != "darwin":
        assert not OAuthReader(cfg / ".credentials.json").read().present
        assert fake_security.calls() == []


# ── the login keeper detects a renewal written to the Keychain ───────────


def test_the_keeper_sees_a_keychain_renewal(tmp_path, fake_security):
    primary = tmp_path / "primary"
    cfg = tmp_path / "cfg-work"
    registry = tmp_path / "accounts.json"
    registry.write_text(json.dumps({"schema": 1, "default": "primary", "accounts": [
        {"id": "work", "label": "Work", "config_dir": str(cfg)}]}))
    fake_security.items({hashed(str(cfg)): login_json(TOKEN, expires_in_s=60)})
    service = AccountService(
        OAuthReader(primary / ".credentials.json", keychain_config_dir=None, platform="darwin"),
        registry, reader_factory=lambda p: OAuthReader(p, platform="darwin"))

    async def runner(argv, env, cwd, timeout):
        # What `claude -p` does on a Mac: Claude Code rewrites its Keychain item.
        assert env["CLAUDE_CONFIG_DIR"] == str(cfg)
        fake_security.items({hashed(str(cfg)): login_json(RENEWED, expires_in_s=8 * 3600)})
        return 0

    keeper = ar.AccountRefresher(service, work_dir=tmp_path / "w", runner=runner,
                                 claude_bin=lambda: "/bin/true", base_env={})
    done = asyncio.run(keeper.tick())
    assert [a.outcome for a in done] == [ar.OUTCOME_RENEWED]
    assert service.reader("work").read(cached_only=True).token == RENEWED
