"""v0.2.105: the launcher's account rows (``gateway_accounts.login_state``) must ask
the macOS Keychain for the SAME item the gateway reads: the user's own login was
made without ``CLAUDE_CONFIG_DIR`` (un-suffixed item), an added account's with
its config dir (suffixed item). Without that, a Mac shows the personal account as
"not logged in" whenever the gateway is down."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import gateway_accounts  # noqa: E402


@pytest.fixture
def keychain_calls(monkeypatch):
    from model_router import auth
    calls: list = []
    monkeypatch.setattr(auth._kc, "is_macos", lambda platform=None: True)

    def fake_read_login(config_dir, **_kw):
        calls.append(config_dir)
        return auth._kc.KeychainLogin(status=auth._kc.NOT_FOUND, service="x")

    monkeypatch.setattr(auth._kc, "read_login", fake_read_login)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return calls


def test_the_primary_row_asks_for_the_unsuffixed_item(tmp_path, keychain_calls):
    gateway_accounts.login_state(tmp_path / ".claude", primary=True)
    assert keychain_calls == [None]


def test_an_added_account_asks_for_its_config_dir_item(tmp_path, keychain_calls):
    team = tmp_path / ".claude-team"
    gateway_accounts.login_state(team)
    assert keychain_calls == [str(team)]


def test_the_primary_row_honours_a_claude_config_dir_the_user_set(tmp_path, monkeypatch,
                                                                   keychain_calls):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "own"))
    gateway_accounts.login_state(tmp_path / ".claude", primary=True)
    assert keychain_calls == [str(tmp_path / "own")]
