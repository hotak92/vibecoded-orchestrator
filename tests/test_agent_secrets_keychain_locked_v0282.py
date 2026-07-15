# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.82 WP-4b: resolver classifies the hub's new keychain-503 states.

Upstream contract (WP-4a, `vct-hub/src/modules_api.rs`): the `/env` route
returns

  * ``503 {"error": {"code": "keychain_locked", ...}}`` when the OS keychain
    is LOCKED (both full-env and ``?key=`` forms, before any Entry is built),
  * ``503 {"error": {"code": "keychain_error", ...}}`` for a per-key non-lock
    keychain failure on a ``?key=`` lookup,
  * ``404 {"error": {"code": "key_not_active"}}`` ONLY for a genuinely
    not-declared / paused key.

WP-4b maps the two 503 states onto the NEW :class:`KeychainLocked` exception
(distinct from :class:`AccessDenied` / ``key_not_active``). The file store
(tier 2) and project ``.env`` (tier 3) are still consulted — they are
INDEPENDENT sanctioned stores, so a locked keychain must not strand a key
that lives there. On an all-tier miss the surfaced :class:`KeychainLocked`
message names BOTH the lock state and the file-store miss.

Two test layers:

* **HTTP-dispatch layer** — stub ``_get_with_401_retry`` to return a fake
  503 response so the REAL ``_hub_get`` status dispatch runs. This is the
  fail-without/pass-with proof: on base, ``_hub_get`` has no 503 branch, so
  a 503 falls to the catch-all ``HubUnreachable`` (asserted by
  ``test_base_shape_503_would_be_hub_unreachable_without_branch``, which
  checks the source has the branch this file relies on).
* **Classification-consumption layer** — monkeypatch ``_hub_get`` to raise
  ``KeychainLocked`` directly and assert ``get()``'s fall-through +
  final-message behaviour (mirrors the existing 403/AccessDenied tests).
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
    HubUnreachable,
    KeychainLocked,
    ResolverError,
    get,
)


# ─── Fake HTTP response for the real _hub_get dispatch ──────────────────


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` — status + JSON body."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _stub_hub_http(monkeypatch, status_code: int, body: dict):
    """Make the REAL ``_hub_get`` run its status dispatch against a canned
    HTTP response, with project-id resolution short-circuited."""
    monkeypatch.setattr(agent_secrets, "_resolve_project_id", lambda arg: "pid-x")
    monkeypatch.setattr(
        agent_secrets,
        "_get_with_401_retry",
        lambda url_builder, params=None, project_id=None: _FakeResponse(
            status_code, body
        ),
    )


def _empty_store(monkeypatch, tmp_path):
    root = tmp_path / "empty-store"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    return root


# ─── Taxonomy ───────────────────────────────────────────────────────────


def test_keychain_locked_is_resolver_error_not_hub_unreachable():
    assert issubclass(KeychainLocked, ResolverError)
    # A locked keychain is NOT unreachability: the hub answered. Callers that
    # catch HubUnreachable for env-fallback must NOT swallow this state.
    assert not issubclass(KeychainLocked, HubUnreachable)


def test_keychain_locked_is_distinct_from_access_denied():
    # key_not_active (authorization) vs keychain_locked (unavailability) are
    # different conditions; neither subclasses the other.
    assert not issubclass(KeychainLocked, AccessDenied)
    assert not issubclass(AccessDenied, KeychainLocked)


def test_keychain_locked_in_public_api():
    assert "KeychainLocked" in agent_secrets.__all__


# ─── HTTP-dispatch layer: real _hub_get classifies the 503 states ───────


def test_hub_get_maps_503_keychain_locked(monkeypatch):
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_locked", "message": "keychain is locked"}},
    )
    with pytest.raises(KeychainLocked) as ei:
        agent_secrets._hub_get("github_pat", None)
    msg = str(ei.value).lower()
    assert "locked" in msg
    assert "keychain" in msg


def test_hub_get_maps_503_keychain_error(monkeypatch):
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_error", "message": "per-key read failed"}},
    )
    with pytest.raises(KeychainLocked) as ei:
        agent_secrets._hub_get("github_pat", None)
    # keychain_error is still a KeychainLocked (unavailability), but the
    # message must NOT claim the whole store is "locked" — it names the
    # per-key read failure honestly.
    msg = str(ei.value).lower()
    assert "unreadable" in msg or "per-key" in msg


def test_hub_get_503_is_not_access_denied(monkeypatch):
    """A locked keychain must NEVER be classified as key_not_active — that
    would tell the caller the key is unauthorized when it may just be
    unreadable."""
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_locked", "message": "locked"}},
    )
    with pytest.raises(KeychainLocked):
        agent_secrets._hub_get("github_pat", None)
    # And it is NOT an AccessDenied.
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_locked", "message": "locked"}},
    )
    try:
        agent_secrets._hub_get("github_pat", None)
    except AccessDenied:  # pragma: no cover — would be a regression
        pytest.fail("503 keychain_locked wrongly classified as AccessDenied")
    except KeychainLocked:
        pass


def test_hub_get_other_503_stays_hub_unreachable(monkeypatch):
    """Only keychain_locked / keychain_error map to KeychainLocked. Any OTHER
    503 (e.g. a future service_misconfigured) keeps the historical
    HubUnreachable classification via the tail."""
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "service_misconfigured", "message": "no KG binding"}},
    )
    with pytest.raises(HubUnreachable):
        agent_secrets._hub_get("github_pat", None)
    # Crucially NOT a KeychainLocked.
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "service_misconfigured", "message": "x"}},
    )
    with pytest.raises(HubUnreachable):
        agent_secrets._hub_get("github_pat", None)


def test_hub_get_404_key_not_active_still_access_denied(monkeypatch):
    """Regression guard: the new 503 branch must not perturb the 404
    key_not_active → AccessDenied mapping."""
    _stub_hub_http(
        monkeypatch,
        404,
        {"error": {"code": "key_not_active", "message": "paused"}},
    )
    with pytest.raises(AccessDenied):
        agent_secrets._hub_get("gated_key", None)


def test_base_shape_503_would_be_hub_unreachable_without_branch():
    """Fail-without/pass-with proof, source-level: the fix ADDS a
    `status_code == 503` branch that keys on keychain_locked/keychain_error.
    On base there is NO such branch, so a 503 falls to the catch-all
    `HubUnreachable(... status ...)`. This asserts the branch this file's
    behavioural tests depend on actually exists (guards against a silent
    revert that would send the 503 back to HubUnreachable)."""
    src = (REPO_ROOT / "vco_lib" / "agent_secrets.py").read_text(encoding="utf-8")
    assert "resp.status_code == 503" in src, (
        "agent_secrets must have a 503 branch (WP-4b); without it the 503 "
        "falls to the catch-all HubUnreachable (base behaviour)"
    )
    assert 'code in ("keychain_locked", "keychain_error")' in src
    assert "raise KeychainLocked(" in src


# ─── Consumption layer: get() fall-through + honest all-miss message ────


def test_locked_keychain_still_falls_back_to_file_store(tmp_path, monkeypatch):
    """The file store is an INDEPENDENT sanctioned store — a locked keychain
    must not strand a file-store key. This is honest, not a downgrade."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "github_pat").write_text("file-copy-value")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    def fake_hub_get(key, project):
        raise KeychainLocked(f"hub could not read the OS keychain for {key!r}")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    # File-store hit → value returned + NO exception raised.
    assert get("github_pat", project="anything") == "file-copy-value"


def test_locked_keychain_all_miss_raises_keychain_locked_with_honest_message(
    tmp_path, monkeypatch
):
    """File store AND .env miss → the surfaced error is the distinct
    KeychainLocked (NOT a generic SecretNotFound), and its message names
    BOTH the lock state and the file-store miss."""
    _empty_store(monkeypatch, tmp_path)

    def fake_hub_get(key, project):
        raise KeychainLocked(f"hub could not read the OS keychain for {key!r}")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(KeychainLocked) as ei:
        get("github_pat", project="anything")
    msg = str(ei.value).lower()
    # Honest message names the state + the file-store miss + remediation.
    assert "locked" in msg
    assert "file store" in msg
    assert "unlock" in msg or "launcher" in msg


def test_locked_keychain_message_never_leaks_a_value(tmp_path, monkeypatch):
    """A seeded file value that DOESN'T match the requested key must never
    appear in the KeychainLocked message (errors name keys + tiers, never
    values)."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "OTHER_KEY").write_text("secret-value-should-not-leak")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    def fake_hub_get(key, project):
        raise KeychainLocked("hub could not read the OS keychain")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(KeychainLocked) as ei:
        get("github_pat", project="anything")
    assert "secret-value-should-not-leak" not in str(ei.value)


def test_locked_keychain_propagates_when_fallback_disabled(tmp_path, monkeypatch):
    """With fallback disabled the KeychainLocked surfaces directly (distinct
    type, not HubUnreachable) so the diagnostic stays honest."""
    _empty_store(monkeypatch, tmp_path)

    def fake_hub_get(key, project):
        raise KeychainLocked("hub could not read the OS keychain")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(KeychainLocked):
        get("github_pat", project="anything", allow_file_fallback=False)


def test_locked_keychain_end_to_end_via_http_stub_falls_back(tmp_path, monkeypatch):
    """End-to-end through the REAL _hub_get (HTTP-stubbed 503) → get() falls
    to the file store. Proves the 503-classification + fall-through wiring
    hold together, not just the isolated _hub_get unit."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "github_pat").write_text("e2e-file-copy")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    _stub_hub_http(
        monkeypatch,
        503,
        {"error": {"code": "keychain_locked", "message": "locked"}},
    )
    assert get("github_pat", project="anything") == "e2e-file-copy"
