# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (W6-AB): a busy secret store is never a false "no key found".

Incident 2026-10-13 ~04:00 (load average ~15): a gateway request for a qwen
model answered 503 ``no key found for vendor 'qwen' … Put it in the SHARED
scope`` while the key sat in the OS keychain. The resolver detail named
tier 1 as ``Read timed out. (read timeout=5.0)`` — the hub was ALIVE but its
keychain worker was slow (its own per-op wait is 10 s, longer than the
client's 5 s read timeout), and tiers 2 and 3 simply did not hold the key.

Pinned here, against the REAL transport code with only the HTTP session
faked (no hub, no keychain, no network):

* a hub that does not answer in time (read timeout, connection reset after
  connect, a 5xx) is "unavailable", not "absent": once tiers 2+3 also miss,
  :func:`agent_secrets.get` raises the RETRYABLE
  :class:`agent_secrets.SecretStoreUnavailable`, never ``SecretNotFound``,
  and its message never tells the user to add the key;
* a REFUSED connection (no hub running) still falls through at once — one
  attempt, no long wait — and keeps the historical ``SecretNotFound``;
* secret reads use the long :data:`project_config.SECRET_READ_TIMEOUT_SECONDS`
  read timeout (config reads keep the short one), plus at most ONE retry;
* the hub's own ``503 keychain_unavailable`` is retried once after its
  ``Retry-After`` (capped), so a keychain that is merely slow completes;
* ``key_not_active`` / ``key_paused`` keep their exact semantics.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import agent_secrets, project_config  # noqa: E402
from vco_lib.agent_secrets import (  # noqa: E402
    AccessDenied,
    KeychainUnavailable,
    SecretNotFound,
    SecretPaused,
    SecretStoreUnavailable,
)

PID = "11111111-2222-3333-4444-555555555555"
FAKE_VALUE = "synthetic-test-value-not-a-secret"


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class _FakeSession:
    """Scripted ``requests.Session``: each ``get`` pops the next outcome (an
    exception instance is raised, anything else returned) and records the
    timeout it was called with."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self.outcomes:
            raise AssertionError(f"unexpected extra GET {url}")
        nxt = self.outcomes.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """Point discovery at a fake hub and an EMPTY file store / cwd .env."""
    monkeypatch.setenv("VCT_HUB_PORT", "7799")
    monkeypatch.setenv("VCT_HUB_TOKEN", "test-token")
    monkeypatch.setenv("VCT_HUB_TOKEN_STRICT", "1")
    store = tmp_path / "store"
    (store / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(store))
    monkeypatch.chdir(tmp_path)
    project_config._test_clear_cache()
    sleeps: list[float] = []
    monkeypatch.setattr(agent_secrets, "_sleep", sleeps.append)

    def install(outcomes):
        sess = _FakeSession(outcomes)
        monkeypatch.setattr(project_config, "_http_session", lambda: sess)
        return sess

    install.sleeps = sleeps  # type: ignore[attr-defined]
    install.store = store  # type: ignore[attr-defined]
    yield install
    project_config._test_clear_cache()


def _refused() -> requests.ConnectionError:
    try:
        raise ConnectionRefusedError(111, "Connection refused")
    except ConnectionRefusedError as inner:
        try:
            raise requests.ConnectionError(
                "HTTPConnectionPool(host='127.0.0.1', port=7799): Max retries "
                "exceeded (Caused by NewConnectionError: [Errno 111] "
                "Connection refused)"
            ) from inner
        except requests.ConnectionError as exc:
            return exc


def _read_timeout() -> requests.ReadTimeout:
    return requests.ReadTimeout(
        "HTTPConnectionPool(host='127.0.0.1', port=7799): Read timed out. "
        "(read timeout=30.0)"
    )


def _ok() -> _Resp:
    return _Resp(200, {"qwen_key": FAKE_VALUE})


# ── transport classification (project_config) ───────────────────────────


def test_hub_not_responding_is_a_hub_unreachable_subclass():
    assert issubclass(project_config.HubNotResponding, project_config.HubUnreachable)
    assert "HubNotResponding" in project_config.__all__


def test_read_timeout_is_classified_not_responding(hub):
    hub([_read_timeout()])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_connection_reset_after_connect_is_not_responding(hub):
    hub([requests.ConnectionError("('Connection aborted.', RemoteDisconnected())")])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_refused_is_plain_hub_unreachable(hub):
    hub([_refused()])
    with pytest.raises(project_config.HubUnreachable) as ei:
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")
    assert not isinstance(ei.value, project_config.HubNotResponding)


# ── transport classification is STRUCTURAL (v0.2.102 fix round) ─────────
# Refused-vs-not-responding must be decided by exception TYPE and errno,
# never by message text: an unrelated transport error whose text mentions
# "connection refused" (proxy noise, a nested vendor error) was classified
# as a refusal → plain HubUnreachable → the false "launcher hub isn't
# running" remedy on a busy hub.


def _refused_in_args() -> requests.ConnectionError:
    # urllib3's shape: the OS error wrapped inside another exception's args
    # (NewConnectionError lands in MaxRetryError's reason).
    inner = ConnectionRefusedError(111, "Connection refused")
    try:
        raise Exception("Max retries exceeded", inner)
    except Exception as wrapped:
        try:
            raise requests.ConnectionError(wrapped) from wrapped
        except requests.ConnectionError as exc:
            return exc


def _rewrapped_errno_refused() -> requests.ConnectionError:
    # urllib3 2.x's shape: the OSError re-raised as a NEW OSError that keeps
    # only errno (not a ConnectionRefusedError instance).
    inner = OSError(111, "Error connecting to 127.0.0.1:7700")
    try:
        raise requests.ConnectionError("wrapped transport error") from inner
    except requests.ConnectionError as exc:
        return exc


def _text_only_refusal() -> requests.ConnectionError:
    return requests.ConnectionError(
        "upstream vendor error: connection refused by the policy proxy"
    )


def _errno_says_otherwise_text_says_refused() -> requests.ConnectionError:
    inner = OSError(113, "No route to host (proxy noted: connection refused)")
    try:
        raise requests.ConnectionError("transport failed") from inner
    except requests.ConnectionError as exc:
        return exc


def _urlerror_refused() -> requests.ConnectionError:
    import urllib.error

    inner = ConnectionRefusedError(111, "Connection refused")
    return requests.ConnectionError(urllib.error.URLError(inner))


def _urlerror_string_reason() -> requests.ConnectionError:
    import urllib.error

    return requests.ConnectionError(urllib.error.URLError("connection refused"))


def _caused_by_timeout() -> requests.ConnectionError:
    import socket

    inner = socket.timeout("timed out")
    try:
        raise requests.ConnectionError("connection failed") from inner
    except requests.ConnectionError as exc:
        return exc


def test_a_nested_refused_in_args_is_refused(hub):
    hub([_refused_in_args()])
    with pytest.raises(project_config.HubUnreachable) as ei:
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")
    assert not isinstance(ei.value, project_config.HubNotResponding)


def test_an_errno_econnrefused_in_the_chain_is_refused(hub):
    hub([_rewrapped_errno_refused()])
    with pytest.raises(project_config.HubUnreachable) as ei:
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")
    assert not isinstance(ei.value, project_config.HubNotResponding)


def test_text_mentioning_refusal_alone_is_not_a_refusal(hub):
    hub([_text_only_refusal()])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_an_os_error_whose_text_says_refused_but_errno_does_not(hub):
    hub([_errno_says_otherwise_text_says_refused()])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_a_urlerror_with_a_refused_reason_is_refused(hub):
    hub([_urlerror_refused()])
    with pytest.raises(project_config.HubUnreachable) as ei:
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")
    assert not isinstance(ei.value, project_config.HubNotResponding)


def test_a_urlerror_with_a_string_reason_is_not_positively_a_refusal(hub):
    # A STRING reason carries no type and no errno — conservative default is
    # "not responding" (retryable), never the false "hub not running".
    hub([_urlerror_string_reason()])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_a_socket_timeout_in_the_chain_is_not_a_refusal(hub):
    hub([_caused_by_timeout()])
    with pytest.raises(project_config.HubNotResponding):
        project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")


def test_a_real_refused_connection_is_still_classified_refused():
    """The structural walk must survive the REAL requests/urllib3 chain —
    the live check behind 'keep the behaviour': urllib3 wraps the OS error
    in NewConnectionError/MaxRetryError, and the ConnectionRefusedError
    stays reachable through the context chain."""
    import socket as socket_mod

    s = socket_mod.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(requests.RequestException) as ei:
        requests.get(f"http://127.0.0.1:{port}/x", timeout=(1, 2))
    assert project_config._is_connection_refused(ei.value)


def test_config_reads_keep_the_short_read_timeout(hub):
    sess = hub([_Resp(200)])
    project_config._get_with_401_retry(lambda port, _t: f"http://127.0.0.1:{port}/x")
    assert sess.calls[0]["timeout"] == (
        project_config._CONNECT_TIMEOUT_SECONDS,
        project_config._READ_TIMEOUT_SECONDS,
    )


# ── the secrets resolver (agent_secrets) ────────────────────────────────


def test_secret_reads_use_the_long_read_timeout(hub):
    assert project_config.SECRET_READ_TIMEOUT_SECONDS >= 20
    sess = hub([_ok()])
    assert agent_secrets.get("qwen_key", project=PID) == FAKE_VALUE
    assert sess.calls[0]["timeout"] == (
        project_config._CONNECT_TIMEOUT_SECONDS,
        project_config.SECRET_READ_TIMEOUT_SECONDS,
    )


def test_by_path_lookup_for_a_secret_read_uses_the_long_timeout(hub, tmp_path):
    sess = hub([_Resp(200, {"id": PID}), _ok()])
    assert agent_secrets.get("qwen_key", project=str(tmp_path)) == FAKE_VALUE
    assert "by-path" in sess.calls[0]["url"]
    for call in sess.calls:
        assert call["timeout"][1] == project_config.SECRET_READ_TIMEOUT_SECONDS


def test_hub_timeout_then_store_miss_is_unavailable_not_not_found(hub):
    sess = hub([_read_timeout(), _read_timeout()])
    with pytest.raises(SecretStoreUnavailable) as ei:
        agent_secrets.get("qwen_key", project=PID)
    assert not isinstance(ei.value, SecretNotFound)
    msg = str(ei.value)
    assert "retry" in msg.lower()
    assert "vct set" not in msg
    assert "add the key" not in msg.lower()
    assert "Put it in" not in msg
    assert FAKE_VALUE not in msg
    assert len(sess.calls) == 2, "exactly one retry after a timeout"


def test_hub_timeout_retry_completes_a_slow_keychain(hub):
    sess = hub([_read_timeout(), _ok()])
    assert agent_secrets.get("qwen_key", project=PID) == FAKE_VALUE
    assert len(sess.calls) == 2


def test_hub_timeout_still_falls_through_to_the_file_store(hub):
    (hub.store / "shared" / "qwen_key").write_text(FAKE_VALUE + "\n", encoding="utf-8")
    hub([_read_timeout(), _read_timeout()])
    assert agent_secrets.get("qwen_key", project=PID) == FAKE_VALUE


def test_refused_falls_through_at_once_and_stays_not_found(hub):
    sess = hub([_refused()])
    with pytest.raises(SecretNotFound) as ei:
        agent_secrets.get("qwen_key", project=PID)
    assert not isinstance(ei.value, SecretStoreUnavailable)
    assert len(sess.calls) == 1, "a refused connection is never retried"
    assert hub.sleeps == []
    assert "launcher" in str(ei.value).lower()


def test_hub_5xx_then_store_miss_is_unavailable(hub):
    sess = hub([_Resp(500, {"error": {"code": "internal"}}), _Resp(500, {})])
    with pytest.raises(SecretStoreUnavailable):
        agent_secrets.get("qwen_key", project=PID)
    assert len(sess.calls) == 2


def test_keychain_unavailable_is_retried_once_after_retry_after(hub):
    busy = _Resp(503, {"error": {"code": "keychain_unavailable"}}, {"Retry-After": "2"})
    sess = hub([busy, _ok()])
    assert agent_secrets.get("qwen_key", project=PID) == FAKE_VALUE
    assert len(sess.calls) == 2
    assert hub.sleeps == [2.0]


def test_keychain_unavailable_retry_after_is_capped(hub):
    busy = _Resp(503, {"error": {"code": "keychain_unavailable"}}, {"Retry-After": "3600"})
    hub([busy, busy])
    with pytest.raises(KeychainUnavailable) as ei:
        agent_secrets.get("qwen_key", project=PID)
    assert isinstance(ei.value, SecretStoreUnavailable)
    assert hub.sleeps == [agent_secrets.HUB_RETRY_AFTER_CAP_SECONDS]
    assert "vct set" not in str(ei.value)


def test_key_not_active_is_unchanged_and_not_retried(hub):
    sess = hub([_Resp(404, {"error": {"code": "key_not_active"}})])
    with pytest.raises(AccessDenied) as ei:
        agent_secrets.get("qwen_key", project=PID)
    assert not isinstance(ei.value, SecretPaused)
    assert len(sess.calls) == 1


def test_key_paused_is_unchanged_and_not_retried(hub):
    sess = hub([_Resp(404, {"error": {"code": "key_paused"}})])
    with pytest.raises(SecretPaused):
        agent_secrets.get("qwen_key", project=PID)
    assert len(sess.calls) == 1


def test_real_not_found_is_unchanged(hub):
    hub([_Resp(404, {"error": {"code": "something_else"}})])
    with pytest.raises(SecretNotFound) as ei:
        agent_secrets.get("qwen_key", project=PID)
    assert not isinstance(ei.value, SecretStoreUnavailable)


def test_lookup_stored_treats_a_timed_out_hub_as_unknown(hub):
    hub([_read_timeout(), _read_timeout()])
    state, value = agent_secrets.lookup_stored("qwen_key", project=PID)
    assert value is None
    assert state == agent_secrets.STORED_UNKNOWN, (
        "a hub that did not answer may hold the key — a writer must not "
        "plant a second copy"
    )


def test_taxonomy():
    assert issubclass(KeychainUnavailable, SecretStoreUnavailable)
    assert not issubclass(SecretStoreUnavailable, SecretNotFound)
    assert not issubclass(SecretNotFound, SecretStoreUnavailable)
    assert "SecretStoreUnavailable" in agent_secrets.__all__


def test_connection_refused_walk_terminates_on_a_self_referential_reason():
    """R4a AB-1: a URLError whose .reason points back at itself (or an args
    cycle) must terminate as "not refused", never recurse to RecursionError."""
    import urllib.error

    from vco_lib import project_config as pc

    err = urllib.error.URLError("x")
    err.reason = err  # type: ignore[assignment]
    other = OSError("wrapper")
    other.args = (other,)
    assert pc._is_connection_refused(err) is False
    assert pc._is_connection_refused(other) is False
    # Leave-alone: a refusal nested in .reason is still found.
    assert pc._is_connection_refused(urllib.error.URLError(ConnectionRefusedError())) is True
