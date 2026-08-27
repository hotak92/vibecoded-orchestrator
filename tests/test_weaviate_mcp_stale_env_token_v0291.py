# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D item 4 — stale-env hub-token fallback in the weaviate MCP.

`_fetch_writable_collections_for_project` probes the hub's BULK access
route to enrich `store_knowledge_node`'s deny-branch with the list of
collections the project may actually write. It is a never-raises
enrichment helper: any failure returns `[]` and the caller falls back to
a generic remediation hint.

THE SEAM: the MCP server is LONG-LIVED. A shell that exported a
now-rotated `VCT_HUB_TOKEN` before spawning it poisoned every probe for
the whole process lifetime, so the deny-branch permanently showed the
generic hint instead of the real collection list.

Pinned here — and note what is NOT changed: the `return []` degradation
and the never-raise contract are byte-identical, because the fallback
only ever REPLACES a provable refusal with a successful answer.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))


ENV_TOKEN = "stale-env-token-v0291-not-a-real-secret"
DISK_TOKEN = "fresh-disk-token-v0291-not-a-real-secret"
COLLECTIONS = ["Synthetic_KnowledgeGraph", "Synthetic_Development"]


def _load_server():
    try:
        from weaviate_mcp import server as srv
    except Exception as exc:  # pragma: no cover — dependency-gated
        pytest.skip(f"weaviate_mcp.server unavailable: {exc}")
    return srv


@pytest.fixture
def srv():
    mod = _load_server()
    mod._test_reset_stale_env_state()
    yield mod
    mod._test_reset_stale_env_state()


@pytest.fixture
def stale_env_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh token on disk, STALE token exported — the field shape."""
    state = tmp_path / "vct"
    state.mkdir()
    (state / "hub.token").write_text(DISK_TOKEN, encoding="utf-8")
    (state / "hub.port").write_text("7700", encoding="utf-8")
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.setenv("VCT_HUB_TOKEN", ENV_TOKEN)
    monkeypatch.delenv("VCT_HUB_TOKEN_STRICT", raising=False)
    monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    return state


def _hub_accepting(expected: str, seen: list):
    """urlopen side-effect: 401 for any bearer but ``expected``."""

    def _side_effect(req, timeout=None):  # noqa: ANN001 — mock signature
        bearer = (req.get_header("Authorization") or "").replace("Bearer ", "", 1)
        seen.append(bearer)
        if bearer != expected:
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:7700", code=401, msg="Unauthorized",
                hdrs=None, fp=None,
            )
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps({"collections": COLLECTIONS}).encode()
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        return ctx

    return _side_effect


def test_stale_pin_is_retried_and_the_real_list_is_returned(srv, stale_env_pin):
    """RED-PROOF: pre-v0.2.91 the 401 propagated to the never-raise
    handler and the deny-branch got `[]` (the generic hint) forever."""
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_hub_accepting(DISK_TOKEN, seen)):
        result = srv._fetch_writable_collections_for_project("p1")

    assert result == COLLECTIONS
    assert seen == [ENV_TOKEN, DISK_TOKEN]
    assert srv._IGNORE_ENV_HUB_TOKEN is True


def test_latch_stops_re_presenting_the_dead_pin(srv, stale_env_pin):
    """The long-lived half: later probes go straight to the on-disk token."""
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_hub_accepting(DISK_TOKEN, seen)):
        srv._fetch_writable_collections_for_project("p1")
        seen.clear()
        assert srv._fetch_writable_collections_for_project("p1") == COLLECTIONS

    assert seen == [DISK_TOKEN]


def test_strict_pin_keeps_the_empty_list_degradation(
    srv, stale_env_pin, monkeypatch: pytest.MonkeyPatch
):
    """LEAVE-ALONE: the guard keeps the pin authoritative, so the helper
    degrades exactly as before — `[]`, no raise, one request."""
    monkeypatch.setenv("VCT_HUB_TOKEN_STRICT", "1")
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_hub_accepting(DISK_TOKEN, seen)):
        assert srv._fetch_writable_collections_for_project("p1") == []
    assert seen == [ENV_TOKEN]
    assert srv._IGNORE_ENV_HUB_TOKEN is False


def test_retry_that_is_also_refused_keeps_the_empty_list(srv, stale_env_pin):
    """LEAVE-ALONE: both tokens refused → `[]`, no raise, no latch."""
    seen: list = []
    with patch("urllib.request.urlopen",
               side_effect=_hub_accepting("a-third-token-nobody-has", seen)):
        assert srv._fetch_writable_collections_for_project("p1") == []
    assert seen == [ENV_TOKEN, DISK_TOKEN]
    assert srv._IGNORE_ENV_HUB_TOKEN is False


def test_retry_that_5xxs_does_not_latch_the_pin_off(srv, stale_env_pin):
    """v0.2.91 wave-3 (MINOR-1). RED pre-fix: any non-401/403 retry answer
    was adopted, so a 503 following the 401 latched the env pin off for
    this LONG-LIVED server and logged "stale VCT_HUB_TOKEN…" — on an answer
    that proves nothing about the credential. One hiccup, and every later
    probe in the process presents a token the hub may never have accepted."""
    seen: list = []

    def _side_effect(req, timeout=None):  # noqa: ANN001 — mock signature
        seen.append((req.get_header("Authorization") or "").replace("Bearer ", "", 1))
        code = 401 if len(seen) == 1 else 503
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:7700", code=code, msg="x", hdrs=None, fp=None,
        )

    with patch("urllib.request.urlopen", side_effect=_side_effect):
        assert srv._fetch_writable_collections_for_project("p1") == []
    assert seen == [ENV_TOKEN, DISK_TOKEN]
    assert srv._IGNORE_ENV_HUB_TOKEN is False


def test_identical_tokens_make_exactly_one_request(
    srv, stale_env_pin, monkeypatch: pytest.MonkeyPatch
):
    """LEAVE-ALONE: the pin is not stale — the happy path is untouched."""
    monkeypatch.setenv("VCT_HUB_TOKEN", DISK_TOKEN)
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_hub_accepting(DISK_TOKEN, seen)):
        assert srv._fetch_writable_collections_for_project("p1") == COLLECTIONS
    assert seen == [DISK_TOKEN]


def test_never_raises_on_a_transport_failure(srv, stale_env_pin):
    """LEAVE-ALONE: the never-raise contract — a connection error still
    yields `[]` rather than escaping to the deny-branch caller."""
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError(ConnectionRefusedError("down"))):
        assert srv._fetch_writable_collections_for_project("p1") == []


def test_no_token_at_all_still_returns_empty(
    srv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """LEAVE-ALONE: no env pin and no on-disk token → `[]` without a
    request (the pre-existing 'can't query' short-circuit)."""
    state = tmp_path / "vct"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_hub_accepting(DISK_TOKEN, seen)):
        assert srv._fetch_writable_collections_for_project("p1") == []
    assert seen == []
