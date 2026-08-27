# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""KG access matrix gate client (v0.2.49 Phase 8 / item #21+#22).

Python counterpart of ``templates/scripts/vct_access_check.sh``. Used
by the MCP server (`store_knowledge_node`, RL telemetry writers) and
Python-side hooks (`sync_knowledge_graph.py`, etc.) to enforce the
``kg_collection_access`` matrix at write time.

The matrix has been a read-gate only since v0.2.21 — env-routed reads
respect the access list (`VCT_KG_ACCESS_LIST` CSV in `.claude/env`) but
writes flow through `WEAVIATE_URL` blindly. This module closes the
asymmetry by consulting the hub's
``GET /api/v1/projects/{id}/access/{collection}`` endpoint at write
time.

Fail-open contract
~~~~~~~~~~~~~~~~~~

Hub unreachable, auth-failed, 404, malformed response, timeout → return
``"write"`` (the most-permissive level). This is DELIBERATE: a closed-
circuit policy would brick all KG writes during a launcher restart,
unacceptable UX. Every fail-open emission:

1. Logs a WARNING via ``logging`` (rate-limited; one per process per
   reason-key per 5 min).
2. Appends a row to ``$VCT_STATE_DIR/cache/dropped_writes.jsonl`` so
   the dropped-write metric is observable.

Rate-limit scope (cross-client documentation)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module keys its rate-limit on ``reason`` alone (process-scoped:
one WARNING per reason per 5 min per Python process). The consumer
is the long-running MCP server
``claude_mcp_servers/weaviate_mcp/server.py`` — emitting a WARNING
every 5 minutes for the same persistent failure would spam the user's
log indefinitely.

Contrast: ``templates/scripts/vct_access_check.sh`` and
``templates/scripts/vct_access_check.ps1`` (consumed by ephemeral
hook subprocesses) key their rate-limit on ``"$PID:$reason"``. Every
hook invocation is a fresh process, so PID-scoped means each hook
firing emits at least one WARNING — INTENTIONAL because hook callers
are episodic and user-visible; we want the degraded state seen every
time it occurs, not silently suppressed.

The divergence is by design. If you find yourself thinking "should I
align these?" the answer is no — read this paragraph again.

Public API
~~~~~~~~~~

.. code-block:: python

    from vco_lib.access_resolver import check_access_level

    level = check_access_level("p1", "MyProject_KnowledgeGraph")
    # level ∈ {"read", "write", "none"} — fail-open returns "write".

    # Caller gates the write on the literal value:
    if level != "write":
        logger.warning(...)
        return

Mirrors the bash client's contract byte-for-byte except this module:
- Uses Python's :mod:`logging` for the WARNING instead of stderr ``printf``.
- Returns the level string instead of printing it.
- Never raises (the fail-open contract).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger("vco.access_resolver")

# Resolver protocol version — must stay in lock-step with bash sibling.
RESOLVER_PROTOCOL_VERSION = 1

# In-process rate-limit state: maps reason → last-emitted-ts (float seconds).
_WARN_STATE: dict[str, float] = {}
_WARN_WINDOW_SECONDS = 300.0  # 5 min, mirrors bash sibling


def _state_dir() -> Path:
    """Resolve $VCT_STATE_DIR or default to the canonical vct_root_dir.

    Delegates to `vco_lib.paths.vct_root_dir()` for the default. The
    consolidation test at `tests/test_vct_root_dir_consolidation.py`
    requires production code to route through the single source of
    truth in `paths.py` rather than reconstructing the home-relative
    ".vct" path inline. Pre-fix this function had the inline form.
    """
    s = os.environ.get("VCT_STATE_DIR")
    if s:
        return Path(s)
    from vco_lib.paths import vct_root_dir
    return vct_root_dir()


def _hub_port() -> int:
    """Hub port discovery: env > state file > default."""
    p = os.environ.get("VCT_HUB_PORT")
    if p:
        try:
            return int(p)
        except ValueError:
            pass
    port_file = _state_dir() / "hub.port"
    if port_file.is_file():
        try:
            return int(port_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    return 7700


def _hub_token() -> Optional[str]:
    """Hub token discovery: env > state file > None.

    The env pin wins on every FIRST attempt — except once
    :data:`_IGNORE_ENV_HUB_TOKEN` has latched, which happens only after
    the hub PROVABLY refused that pin and the on-disk token worked (see
    :func:`_note_stale_env_token_override`).
    """
    t = os.environ.get("VCT_HUB_TOKEN")
    if t and not _IGNORE_ENV_HUB_TOKEN:
        return t
    return _on_disk_hub_token()


# ─── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ────────────────
#
# MUST MATCH the SSOT `vco_lib/project_config.py::_stale_env_token_fallback`
# and the mirrors in `templates/scripts/vct_access_check.{sh,ps1}` (this
# module's own bash/ps1 siblings), `vct_secrets_resolve.{sh,ps1}`,
# `vct_project_config.{sh,ps1}`, `claude_mcp_servers/wrappers/_base.py`,
# `launcher/tools/vct-cli/src/main.rs` and `tools/vct-secrets/vct`.
# Locked by tests/test_stale_env_token_parity_v0291.py.
#
# WHY it matters MOST here: this module's consumer is the LONG-LIVED
# `claude_mcp_servers/weaviate_mcp/server.py`. A shell that exported a
# now-rotated `VCT_HUB_TOKEN` before spawning it poisons the process for
# its ENTIRE lifetime — every access check 401s and FAILS OPEN to
# "write", so the access matrix silently degrades to permissive until the
# MCP is restarted. That is the longest exposure window of any surface,
# which is why the module-level LATCH below matters (the short-lived
# script siblings need no latch).
#
# THE FAIL-OPEN CONTRACT IS UNCHANGED (deliberate availability choice):
# a genuine auth failure, an unreachable hub, a missing token, or a retry
# that is ALSO refused still fails open to "write" with the SAME reason
# string, the SAME rate-limited WARNING and the SAME dropped-write metric
# row. This only makes the fail-open reached LESS OFTEN.
#
# GLOBAL TOKEN ONLY: `/projects/{id}/access/{collection}` is not a
# per-project-token route (the hub gates only `/env` + `/config` that
# way), so a scoped `hub.token.<id>` would itself 401 here — this mirror
# deliberately has no scoped branch, exactly like its bash/ps1 siblings.

#: HTTP codes that constitute a PROVABLE credential refusal.
_AUTH_REFUSAL_CODES = frozenset({401, 403})

#: The ONE definitive line. Byte-identical to every mirror.
STALE_ENV_TOKEN_MESSAGE = (
    "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — "
    "run `unset VCT_HUB_TOKEN` or open a new shell"
)

#: Latched TRUE once the hub proved this process's env pin dead and the
#: on-disk token worked. Module-level because the consumer is a
#: long-lived process: without it, every later call would re-present the
#: same refused value. Never set when ``VCT_HUB_TOKEN_STRICT=1``.
#:
#: Guarded by :data:`_STALE_ENV_LOCK` — the same locking pattern
#: ``project_config`` uses for its warn-once flag. The consumer is a
#: MULTI-THREADED MCP server, so two concurrent gate checks can land in
#: the latch at once; without the lock the "once" in "one definitive
#: line per process" is not actually once.
_STALE_ENV_LOCK = threading.Lock()
_IGNORE_ENV_HUB_TOKEN = False
#: One definitive log line per process, not per call.
_STALE_ENV_WARNED = False


def _on_disk_hub_token() -> Optional[str]:
    """The on-disk token, IGNORING ``$VCT_HUB_TOKEN``."""
    token_file = _state_dir() / "hub.token"
    if token_file.is_file():
        try:
            return token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _hub_token_strict() -> bool:
    """True when ``VCT_HUB_TOKEN_STRICT=1`` (trimmed) is set.

    MUST MATCH the SSOT and every mirror: compared to the literal ``1``
    after a whitespace trim, so ``"1\\n"`` from a here-doc or a CRLF
    ``.env`` line means the same thing in bash, PowerShell, Rust and here.
    """
    return os.environ.get("VCT_HUB_TOKEN_STRICT", "").strip() == "1"


def _stale_env_token_fallback() -> Optional[str]:
    """The on-disk token to retry with, or ``None`` to leave alone.

    Rules, in order (identical in every mirror):
      1. ``VCT_HUB_TOKEN_STRICT=1``      → None (the pin is authoritative)
      2. ``VCT_HUB_TOKEN`` unset/empty   → None (nothing was pinned)
      3. no readable on-disk token       → None (nothing better to try)
      4. on-disk == env (trimmed)        → None (the pin is not stale)
    """
    if _hub_token_strict():
        return None
    env_tok = (os.environ.get("VCT_HUB_TOKEN") or "").strip()
    if not env_tok:
        return None
    disk_tok = _on_disk_hub_token()
    if not disk_tok or disk_tok == env_tok:
        return None
    return disk_tok


def _note_stale_env_token_override() -> None:
    """Latch the env pin off for this process and log the fix ONCE.

    NOT a fail-open event: no dropped-write metric row and no "hub
    unreachable" phrasing — the gate WORKED, we just had to reach past a
    dead env pin to make it work.
    """
    global _IGNORE_ENV_HUB_TOKEN, _STALE_ENV_WARNED
    with _STALE_ENV_LOCK:
        _IGNORE_ENV_HUB_TOKEN = True
        if _STALE_ENV_WARNED:
            return
        _STALE_ENV_WARNED = True
    logger.warning("%s", STALE_ENV_TOKEN_MESSAGE)


def _test_reset_stale_env_state() -> None:
    """Reset the process-level latch + warn-once flag (tests only)."""
    global _IGNORE_ENV_HUB_TOKEN, _STALE_ENV_WARNED
    with _STALE_ENV_LOCK:
        _IGNORE_ENV_HUB_TOKEN = False
        _STALE_ENV_WARNED = False


# Outcome tags for :func:`_request_access` — one attempt, classified.
_OK = "ok"                          # reached the hub; `response` is set
_REFUSED = "refused"                # 401/403; `code` is set
_HTTP_FAIL = "http_fail"            # definitive non-credential answer
_TRANSPORT_FAIL = "transport_fail"  # never reached the hub


class _Attempt(NamedTuple):
    """One classified access-matrix request.

    ``response`` carries the body for :data:`_OK`; ``reason`` the fail-open
    reason for the two failure tags; ``code`` the HTTP status whenever the
    hub answered at all (:data:`_REFUSED` and :data:`_HTTP_FAIL`).
    Modelled as a NamedTuple rather than a loose ``(tag, payload)`` pair so
    the types are checkable (``vco_lib`` is inside the repo's zero-error
    pyright gate).
    """

    outcome: str
    code: Optional[int] = None
    reason: Optional[str] = None
    response: Optional[tuple[int, bytes]] = None


def _retry_answer_is_definitive(attempt: "_Attempt") -> bool:
    """May a RETRY's outcome be adopted (and the env pin latched off)?

    Only when it PROVES the fallback credential was accepted:

    * ``2xx`` — the hub served the request;
    * ``404`` — "no such project / no access row", which the hub answers
      only AFTER its auth middleware has accepted the bearer, so it is a
      post-auth answer just like a 200.

    Everything else — 5xx, a transport failure, any other status — proves
    nothing about the credential. v0.2.91 wave-3 (MINOR-1): those used to
    be adopted, which meant a hub that 401'd and then hiccuped a 503
    latched the env pin off, warned "stale VCT_HUB_TOKEN", and reported
    ``hub_5xx_503`` instead of the truthful ``hub_auth_401``. Keeping the
    ORIGINAL refusal there is byte-identical to pre-v0.2.91 behaviour.
    """
    if attempt.outcome == _OK:
        return True
    return attempt.outcome == _HTTP_FAIL and attempt.code == 404


def _refusal_reason(code: int) -> str:
    """The fail-open reason for an un-rescued refusal.

    Byte-identical to pre-v0.2.91: 401 kept its dedicated
    ``hub_auth_401`` tag, and 403 (which this route does not currently
    produce — it is not a per-project-token route) kept the catch-all
    ``hub_unexpected_<code>`` shape.
    """
    return "hub_auth_401" if code == 401 else f"hub_unexpected_{code}"


def _request_access(url: str, token: str) -> _Attempt:
    """Issue ONE access-matrix GET with an explicit bearer.

    Extracted (v0.2.91) so the stale-env retry can re-issue the same call
    with a different token. The classification below reproduces the
    pre-v0.2.91 branch-for-branch mapping exactly; only the SHAPE moved
    (early `return _fail_open(...)` → a tagged outcome the caller acts
    on), so every reason string a caller can observe is unchanged.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return _Attempt(_OK, response=(resp.status, resp.read()))
    except urllib.error.HTTPError as e:
        # 4xx + 5xx land here (HTTPError subclasses URLError — this arm
        # MUST stay first, as it was pre-fix).
        if e.code in _AUTH_REFUSAL_CODES:
            return _Attempt(_REFUSED, code=e.code)
        if e.code == 404:
            # Per the bash sibling: 404 = no row → fail-open with metric so
            # the user can investigate why the project isn't registered.
            return _Attempt(_HTTP_FAIL, code=e.code, reason="hub_404_no_row")
        if 500 <= e.code < 600:
            return _Attempt(_HTTP_FAIL, code=e.code, reason=f"hub_5xx_{e.code}")
        return _Attempt(
            _HTTP_FAIL, code=e.code, reason=f"hub_unexpected_{e.code}"
        )
    except urllib.error.URLError as e:
        kind = type(e.reason).__name__ if hasattr(e, "reason") else "unknown"
        return _Attempt(_TRANSPORT_FAIL, reason=f"url_error_{kind}")
    except Exception as e:
        return _Attempt(_TRANSPORT_FAIL, reason=f"unexpected_{type(e).__name__}")


def _maybe_rotate_jsonl(path: Path, max_bytes: int = 1_048_576, keep_lines: int = 100) -> None:
    """v0.2.49 Step F SF8 (L4-S2): log rotation for the dropped-write
    metric + warning JSONL files. Unbounded growth on long-lived MCP
    processes (or hooks accumulating over weeks of dev) degrades the
    bash sibling's linear awk scan and bloats the disk. When the file
    exceeds ``max_bytes``, truncate to the most-recent ``keep_lines``
    rows.

    Best-effort: any I/O error during rotation is silently swallowed
    (the fail-open contract above doesn't get to fail because of log
    bookkeeping).
    """
    try:
        if not path.is_file():
            return
        if path.stat().st_size <= max_bytes:
            return
        # Read tail. For a 1 MiB file with ~150-byte rows that's ~7000
        # lines; reading the full file once is fine. The bash sibling
        # uses a more sophisticated tail-N approach for its awk scan
        # rate-limit hot path.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tail = lines[-keep_lines:]
        tmp = path.with_suffix(path.suffix + ".rot.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(tail)
        # Atomic replace — works on Linux + macOS + Windows.
        os.replace(str(tmp), str(path))
    except Exception:
        # Rotation failure must not break the metric emit's fail-open
        # contract. Worst case the file keeps growing — next call's
        # rotation attempt will retry.
        pass


def _emit_metric(project_id: str, collection: str, reason: str) -> None:
    """Append a dropped-write row to the metric JSONL. Never raises."""
    try:
        cache_dir = _state_dir() / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        jsonl = cache_dir / "dropped_writes.jsonl"
        row = {
            "ts": int(time.time()),
            "project_id": project_id,
            "collection": collection,
            "reason": reason,
            "fail_open": True,
        }
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        # v0.2.49 SF8: rotate post-append if oversized.
        _maybe_rotate_jsonl(jsonl)
    except Exception:
        # Logging failure must not break the fail-open contract.
        pass


def _emit_warning(reason: str) -> None:
    """Rate-limited WARNING — one per reason-key per 5-min window per process.

    Bypass via ``VCO_HOOK_DEBUG=1`` (emits every occurrence).
    """
    now = time.time()
    bypass = os.environ.get("VCO_HOOK_DEBUG") == "1"
    if not bypass:
        last = _WARN_STATE.get(reason)
        if last is not None and (now - last) < _WARN_WINDOW_SECONDS:
            return
    _WARN_STATE[reason] = now
    logger.warning(
        "hub unreachable (%s); failing open to write level (rate-limited)",
        reason,
    )


def _fail_open(project_id: str, collection: str, reason: str) -> str:
    """Emit metric + warning + return 'write'. The fail-open contract."""
    _emit_metric(project_id, collection, reason)
    _emit_warning(reason)
    return "write"


def check_access_level(project_id: str, collection: str) -> str:
    """Return the access level for ``(project_id, collection)``.

    Returns one of ``"read"``, ``"write"``, ``"none"``. Fail-open: any
    network / auth / parse error returns ``"write"`` (most-permissive)
    with a metric emission + rate-limited WARNING log. Never raises.

    The caller gates the write:

    .. code-block:: python

        level = check_access_level(pid, coll)
        if level != "write":
            return  # silent drop or error response per caller's contract
    """
    if not project_id or not collection:
        # No project context → can't check, fail-open without metric noise.
        return "write"

    token = _hub_token()
    if not token:
        return _fail_open(project_id, collection, "no_hub_token")

    port = _hub_port()
    url = f"http://127.0.0.1:{port}/api/v1/projects/{project_id}/access/{collection}"

    attempt = _request_access(url, token)

    # v0.2.91 (WP-D item 4): a PROVABLE credential refusal is the ONLY
    # trigger for the one-shot on-disk-token retry. A strict pin, an
    # absent env token, an identical on-disk token, a retry that is ALSO
    # refused, a retry that cannot complete, or a retry whose answer does
    # not PROVE the credential was accepted (see
    # :func:`_retry_answer_is_definitive`) all fall through to the
    # ORIGINAL refusal — so the fail-open reason string, WARNING and
    # metric row stay byte-identical to pre-v0.2.91.
    if attempt.outcome == _REFUSED:
        fallback = _stale_env_token_fallback()
        if fallback is not None:
            retry = _request_access(url, fallback)
            if _retry_answer_is_definitive(retry):
                _note_stale_env_token_override()
                attempt = retry
        if attempt.outcome == _REFUSED:
            return _fail_open(
                project_id, collection, _refusal_reason(attempt.code or 0)
            )

    if attempt.outcome in (_HTTP_FAIL, _TRANSPORT_FAIL):
        return _fail_open(project_id, collection, attempt.reason or "unknown")

    # `_OK` always carries a response; the `or` keeps the function total
    # for the type checker without adding an unreachable branch (a 0
    # status would fail open through the ordinary `status != 200` arm).
    status, body_bytes = attempt.response or (0, b"")

    if status != 200:
        return _fail_open(project_id, collection, f"hub_status_{status}")

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _fail_open(project_id, collection, "hub_malformed_json")

    level = body.get("level")
    if not isinstance(level, str) or level not in ("read", "write", "none"):
        return _fail_open(project_id, collection, "hub_malformed_level")

    return level


def is_write_allowed(project_id: str, collection: str) -> bool:
    """Convenience: returns True iff caller may write to ``collection``.

    Equivalent to ``check_access_level(project_id, collection) == "write"``,
    but more readable at call sites.
    """
    return check_access_level(project_id, collection) == "write"


__all__ = [
    "check_access_level",
    "is_write_allowed",
    "RESOLVER_PROTOCOL_VERSION",
]
