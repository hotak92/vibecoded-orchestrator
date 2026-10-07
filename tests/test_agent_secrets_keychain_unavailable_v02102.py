# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102: resolver classifies the hub's 503 ``keychain_unavailable``.

Upstream contract (`vct-hub/src/modules_api.rs`): when a keychain operation
timed out or the hub's keychain worker is stuck, a ``?key=`` miss answers
``503 {"error": {"code": "keychain_unavailable"}}`` with a ``Retry-After``
header — never ``404 key_not_active`` (the pre-v0.2.102 symptom: callers were
told a key was "not active" while the keychain was merely busy).

``agent_secrets`` maps it to :class:`KeychainUnavailable`, a subclass of
:class:`KeychainLocked` (existing ``except KeychainLocked`` callers keep
working, and ``get()`` still falls through to the file store and ``.env``),
whose message says "retry", not "unlock".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import agent_secrets  # noqa: E402
from vco_lib.agent_secrets import (  # noqa: E402
    AccessDenied,
    KeychainLocked,
    KeychainUnavailable,
    get,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


def _stub_hub_http(monkeypatch, status_code, body, headers=None):
    monkeypatch.setattr(agent_secrets, "_resolve_project_id", lambda arg: "pid-x")
    monkeypatch.setattr(
        agent_secrets,
        "_get_with_401_retry",
        lambda url_builder, params=None, project_id=None: _FakeResponse(
            status_code, body, headers
        ),
    )


def _empty_store(monkeypatch, tmp_path):
    root = tmp_path / "empty-store"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    return root


def test_keychain_unavailable_taxonomy():
    assert issubclass(KeychainUnavailable, KeychainLocked)
    assert not issubclass(KeychainUnavailable, AccessDenied)
    assert "KeychainUnavailable" in agent_secrets.__all__


def test_hub_get_maps_503_keychain_unavailable_with_retry_after(monkeypatch):
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_unavailable", "message": "timed out"}},
        {"Retry-After": "2"},
    )
    with pytest.raises(KeychainUnavailable) as ei:
        agent_secrets._hub_get("github_pat", None)
    msg = str(ei.value).lower()
    assert "temporarily unavailable" in msg
    assert "retry in 2 seconds" in msg
    assert "unlock" not in msg, "a busy keychain has nothing to unlock"


def test_hub_get_keychain_unavailable_without_header(monkeypatch):
    _stub_hub_http(
        monkeypatch, 503, {"error": {"code": "keychain_unavailable"}}
    )
    with pytest.raises(KeychainUnavailable):
        agent_secrets._hub_get("github_pat", None)


def test_keychain_error_is_not_classified_unavailable(monkeypatch):
    _stub_hub_http(monkeypatch, 503, {"error": {"code": "keychain_error"}})
    with pytest.raises(KeychainLocked) as ei:
        agent_secrets._hub_get("github_pat", None)
    assert not isinstance(ei.value, KeychainUnavailable)


def test_get_falls_through_to_file_store_on_unavailable(monkeypatch, tmp_path):
    root = _empty_store(monkeypatch, tmp_path)
    (root / "shared" / "SOME_KEY").write_text("file-store-value")
    _stub_hub_http(monkeypatch, 503, {"error": {"code": "keychain_unavailable"}})
    assert get("SOME_KEY") == "file-store-value"


def test_get_all_miss_surfaces_unavailable_not_access_denied(monkeypatch, tmp_path):
    _empty_store(monkeypatch, tmp_path)
    _stub_hub_http(monkeypatch, 503, {"error": {"code": "keychain_unavailable"}})
    monkeypatch.setattr(agent_secrets, "_project_dotenv_get", lambda k, p: None)
    with pytest.raises(KeychainUnavailable) as ei:
        get("MISSING_KEY")
    msg = str(ei.value).lower()
    assert "temporarily unavailable" in msg
    assert "retry" in msg
