# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""HTTP client for posting RL events to vct-hub (v0.2.47 RL-5).

Replaces the JSONL append path. Free-tier and Pro use the SAME client;
the hub binary is part of the orchestrator install and runs alongside
the launcher GUI (and survives the GUI being closed).

Soft-fail discipline (locked decision 2026-06-04):
    - Connect-refused, timeout, missing token, non-2xx all return False.
    - Caller does NOT raise; treats False as "event lost" and continues.
    - No retry queue. No JSONL fallback (the JSONL path is dead going
      forward; historical events come over via the one-shot migration
      script in claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py).

Discovery:
    - Hub port: ``$VCT_HUB_PORT`` -> ``<vct_root>/hub.port`` -> 7700.
    - Hub token: ``<vct_root>/hub.token`` (regenerated at every hub startup,
      mode 0o600). Read at every call — no in-process cache (the token
      rotates and we don't want stale-token surprises in a long-lived
      MCP subprocess that survives a launcher restart).
    - vct_root: ``$VCT_STATE_DIR`` -> ``~/.vct``.

Endpoint:
    POST http://127.0.0.1:<port>/api/v1/rl/events
        Authorization: Bearer <token>
        Content-Type: application/json
        Body: see ``RlEventBody`` (matches the hub-side handler).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 7700
_DEFAULT_TIMEOUT_S = 2.0

# WP-R (2026-07-22): belt-and-braces hermeticity chokepoint. This module is the
# SOLE place any RL event/prune reaches the REAL launcher.db (via vct-hub). Any
# unit test that exercises a live code-search / KG-search entry point
# (``search_code_graph``, ``CodeGraphQuery.search_by_concept``, the MCP tools)
# flows through the shared telemetry emitter and, if nothing gates it, ends up
# here — writing FIXTURE junk (dim 3/4 codesage ``code_hook``/``code_search``
# events with titles like ``a.f``/``mod.self_fn``) into the live ``rl_events``
# table. On a real install this accumulated thousands of junk rows plus a fresh
# trickle every test run. The structural fix makes the tests hermetic (see
# ``tests/conftest.py::_disable_rl_hub_writes_in_tests``, which sets
# ``RL_HUB_POST_DISABLED=1`` suite-wide); THIS guard is the second line of
# defence so ANY future test that reaches the poster — before it's added to the
# suite, or via a path the conftest env doesn't cover — still cannot pollute the
# real DB. Two independent sentinels:
#   * ``PYTEST_CURRENT_TEST`` — pytest sets this automatically for the duration
#     of every test; requires NO test-side cooperation, so it catches tests the
#     author never thought about the hub in.
#   * ``RL_HUB_POST_DISABLED`` — explicit opt-out the conftest sets (and any
#     harness/red-proof can set) to assert "nothing must reach the real hub".
# A truthy value at EITHER sentinel makes both posters a soft no-op (return the
# same "event lost" contract the network-failure path returns), so the caller's
# soft-fail flow is unchanged. Never raises.
#
# The ONE escape hatch is ``VCT_HUB_ALLOW_TEST_POST=1``: the file that DIRECTLY
# tests the poster against a mock HTTP hub (tests/test_v0247_hub_writer.py) sets
# it (via the conftest opt-out) so its round-trip assertions actually fire. It
# overrides BOTH suppression legs. Nothing in production ever sets it.
_HUB_POST_DISABLED_ENV = "RL_HUB_POST_DISABLED"
_HUB_ALLOW_TEST_POST_ENV = "VCT_HUB_ALLOW_TEST_POST"


def _in_test_context() -> bool:
    """True iff RL hub writes must be suppressed for hermeticity (WP-R).

    Suppressed when EITHER ``PYTEST_CURRENT_TEST`` (pytest-managed, present for
    the whole test) OR the explicit ``RL_HUB_POST_DISABLED`` opt-out is truthy —
    UNLESS the explicit ``VCT_HUB_ALLOW_TEST_POST`` escape hatch is set (the
    poster's own direct-test file). Reads plain env vars, so it behaves
    identically on every OS. A read that raises (never expected for os.environ,
    but defensive) falls open to "not a test" = writes allowed — the guard must
    never itself break a real production write.
    """
    try:
        if os.environ.get(_HUB_ALLOW_TEST_POST_ENV, "").strip().lower() in (
            "true", "1", "yes", "on",
        ):
            return False
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return True
        return os.environ.get(_HUB_POST_DISABLED_ENV, "").strip().lower() in (
            "true", "1", "yes", "on",
        )
    except Exception:  # noqa: BLE001 — env read must never break a write path
        return False


def _vct_root_dir() -> Path:
    """Resolve <vct_root>: delegates to ``vco_lib.paths.vct_root_dir``.

    v0.2.47 RL-5 follow-up: the original inline reconstruction was
    flagged by ``tests/test_vct_root_dir_consolidation.py`` — only
    ``vco_lib.paths`` is allowed to construct the root path. Delegation
    here preserves the private-callsite shape (callers still go through
    this thin wrapper for unit-test patching convenience) while keeping
    the construction in exactly one place.
    """
    from vco_lib.paths import vct_root_dir as _canonical

    return _canonical()


def _read_hub_port() -> int:
    """Resolve hub port: $VCT_HUB_PORT -> <vct_root>/hub.port -> default 7700.

    Any read error falls through to the default. The hub-startup code
    writes the file on every successful bind so the file presence and
    its content reflect the actual listening port.
    """
    env = os.environ.get("VCT_HUB_PORT")
    if env:
        try:
            return int(env.strip())
        except (ValueError, AttributeError):
            pass
    port_file = _vct_root_dir() / "hub.port"
    try:
        return int(port_file.read_text().strip())
    except (OSError, ValueError):
        return _DEFAULT_PORT


def _read_hub_token() -> str | None:
    """Read the hub bearer token from <vct_root>/hub.token.

    Returns None when the file is missing or unreadable. Caller treats
    None as "hub not running" and skips the POST entirely (soft-fail).

    The token rotates on every hub startup, so callers MUST NOT cache
    the result across requests — a single MCP subprocess can outlive
    multiple hub restarts.
    """
    p = _vct_root_dir() / "hub.token"
    try:
        return p.read_text().strip()
    except OSError:
        return None


def post_rl_event(event: dict[str, Any], timeout: float = _DEFAULT_TIMEOUT_S) -> bool:
    """POST one v3 RL event to the hub's ``/api/v1/rl/events`` route.

    Returns True on HTTP 2xx, False on any error (missing token, connect
    refused, timeout, non-2xx response). Never raises.

    Body schema (matches the hub's ``rl_events_api::PostEventBody``):

        {
            "event_type":       "retrieval" | "citation",
            "schema_version":   int,
            "ts_ms":            int (unix epoch ms; writer-side time),
            "project_id":       str | null,
            "project_name":     str | null,
            "task_id":          str (required),
            "task_type":        str | null,
            "embedding_source": str | null,
            "embedding_dim":    int | null,
            "embedding_model":  str | null,
            "payload_json":     str (the full v3 event JSON, verbatim)
        }

    The hub validates ``event_type`` is one of {retrieval, citation} and
    requires non-empty ``task_id`` + ``payload_json``. Caller is responsible
    for providing those — a 400 from the hub is a writer bug, not a
    soft-fail-recoverable condition.

    Args:
        event: A dict matching the body schema above.
        timeout: Per-request timeout in seconds. Default 2.0. The hub
            INSERT is microsecond-fast; long timeouts here would only
            mask a wedged hub.

    Returns:
        True if the hub returned 2xx; False otherwise (including the
        "hub not running" case where no token file exists).
    """
    # WP-R: hermeticity chokepoint. Under a test context (PYTEST_CURRENT_TEST or
    # the explicit RL_HUB_POST_DISABLED opt-out) suppress the write entirely so a
    # test can never pollute the production rl_events table. Same "event lost"
    # return (False) as the hub-not-running path, so the caller's soft-fail flow
    # is unchanged.
    if _in_test_context():
        logger.debug("rl_events POST suppressed (test context)")
        return False

    token = _read_hub_token()
    if token is None:
        logger.debug("rl_events POST skipped: no hub.token (hub not running?)")
        return False

    port = _read_hub_port()
    url = f"http://127.0.0.1:{port}/api/v1/rl/events"

    try:
        body = json.dumps(event).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # Caller bug: event isn't JSON-serializable. Surface in debug
        # but DON'T raise — we still return False so the caller's flow
        # is uniform with the network-failure case.
        logger.debug("rl_events POST skipped: not JSON-serializable (%s)", exc)
        return False

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # The hub returned a structured 4xx/5xx — the hub IS reachable but
        # REJECTED this event, so the label is genuinely LOST (unlike a
        # hub-not-running URLError, which is the expected off state). Log at
        # WARNING so the loss is visible (R2-11): a 413 means the payload
        # exceeded the 16 MiB axum body limit (raised from 2 MB by WP-Q; the
        # explicit const lives in rl_events_api.rs). The client-side size guard in
        # telemetry_writer._trim_event_to_payload_cap should keep events under
        # it, so a 413 here signals a pathological event worth investigating; a
        # 4xx/5xx otherwise signals a writer bug (e.g. unknown event_type). The
        # error body carries the envelope but reading it here defeats the
        # no-raise contract on closed connections, and the soft-fail caller
        # doesn't need it.
        logger.warning("rl_events POST returned HTTP %s (event dropped)", e.code)
        return False
    except urllib.error.URLError as e:
        # Connect-refused / DNS / TLS / etc. Most common: hub not running.
        logger.debug("rl_events POST URL error: %s", e.reason)
        return False
    except (OSError, TimeoutError) as exc:
        logger.debug("rl_events POST failed: %s", exc)
        return False


def post_rl_prune(
    *,
    cutoff_ms: Optional[int] = None,
    max_rows: Optional[int] = None,
    project_id: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> Optional[dict[str, Any]]:
    """RL-5 (v0.2.73): drive the hub-side ``rl_events`` prune route.

    POSTs a prune request to ``/api/v1/rl/events/prune`` (added hub-side by the
    W2-B track — see the RL-5 patch spec). The hub deletes events older than
    ``cutoff_ms`` and/or keeps only the newest ``max_rows`` rows, then returns
    ``{"ok": true, "deleted": <n>}``.

    Return contract (deliberately three-valued so the retention driver can tell
    "route missing" apart from "route ran, deleted 0"):

      * ``dict``  — the hub's 2xx JSON body (``{"deleted": N, ...}``).
      * ``None``  — the route does not exist on this hub binary (404) OR the
        hub is unreachable / token missing / any transport error. The driver
        treats None as "prune not available; corpus untouched" and moves on.

    Never raises. Retention is best-effort — a wedged hub must never break the
    RL write path or the user-facing search.

    Args:
        cutoff_ms: Delete events with ``ts_ms < cutoff_ms``. None → no age bound.
        max_rows: Keep at most this many most-recent rows. None → no row bound.
        project_id: Scope the prune to one project. None → all projects.
        timeout: Per-request timeout (s).
    """
    # WP-R: hermeticity chokepoint (same as post_rl_event). The retention driver
    # calls this inside ``log_retrieval`` on the write cadence, so a test that
    # triggers a retrieval emit would otherwise reach the real hub's prune route.
    # Under a test context, no-op with the "route unavailable" sentinel (None) so
    # the driver treats it as "corpus untouched" and moves on.
    if _in_test_context():
        logger.debug("rl_events prune suppressed (test context)")
        return None

    token = _read_hub_token()
    if token is None:
        logger.debug("rl_events prune skipped: no hub.token (hub not running?)")
        return None

    port = _read_hub_port()
    url = f"http://127.0.0.1:{port}/api/v1/rl/events/prune"

    body_obj: dict[str, Any] = {}
    if cutoff_ms is not None:
        body_obj["cutoff_ms"] = int(cutoff_ms)
    if max_rows is not None:
        body_obj["max_rows"] = int(max_rows)
    if project_id:
        body_obj["project_id"] = str(project_id)

    try:
        body = json.dumps(body_obj).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.debug("rl_events prune skipped: not JSON-serializable (%s)", exc)
        return None

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                try:
                    return json.loads(resp.read().decode("utf-8"))
                except Exception:  # noqa: BLE001 — 2xx with unparseable body
                    return {"ok": True}
            return None
    except urllib.error.HTTPError as e:
        # 404 == route absent on an older hub binary → treat as "unsupported"
        # (None) so the driver degrades gracefully rather than logging noise.
        if e.code == 404:
            logger.debug("rl_events prune route absent (404); older hub binary")
        else:
            logger.debug("rl_events prune returned HTTP %s", e.code)
        return None
    except urllib.error.URLError as e:
        logger.debug("rl_events prune URL error: %s", e.reason)
        return None
    except (OSError, TimeoutError) as exc:
        logger.debug("rl_events prune failed: %s", exc)
        return None


__all__ = ["post_rl_event", "post_rl_prune"]
