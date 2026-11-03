# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Keep the logins of the gateway's extra Claude accounts alive (v0.2.105).

An account used only through the gateway would expire within hours: the
gateway reads a credentials file and never refreshes it, and Claude Code
refreshes only the login of a session it runs (behind its own lock and
compare-and-swap, which a second implementation must not race). So the
gateway asks Claude Code to do it (owner ruling 2026-11-03): shortly before an
account's login expires it starts ONE native ``claude`` process with that
account's ``CLAUDE_CONFIG_DIR`` and lets it refresh its own file. VCO never
reads, passes or stores the token value.

Which invocation, and why it spends no model call
-------------------------------------------------
STATUS: the design below is read from Claude Code 2.1.312's shipped bundle
and exercised against a stand-in CLI in the tests; it has NOT been run
against a live second login yet (feasibility probe P4 needs one). That is why
success is judged on the file, never assumed — a release that stops
refreshing this way shows up as ``not_renewed`` WARNINGs and the loud
fallback, never as a silent "renewed".

* ``claude auth status`` does NOT refresh: in Claude Code 2.1.312 its handler
  reports ``loggedIn`` from token PRESENCE and makes no refresh call (read in
  the shipped bundle, 2026-11-03). It cannot keep a login alive.
* Claude Code refreshes an OAuth login before it sends a request, and only
  when the token expires within five minutes (its ``now + 300000 >=
  expiresAt`` check). So the keeper runs ``claude -p`` inside that window —
  :data:`REFRESH_LEAD_S` before expiry, or after it — with ``ANTHROPIC_BASE_URL``
  pointing at a loopback port this process has BOUND BUT NOT OPENED for
  listening: the refresh goes to Claude's own sign-in service, then the model
  request is refused by the operating system on the spot. No model call is
  made, nothing on this machine can receive the request (the port is held),
  and ``CLAUDE_CODE_MAX_RETRIES=0`` stops the client retrying it.
* ``--safe-mode`` keeps hooks, MCP servers and plugins of that config dir from
  starting; ``--no-session-persistence`` writes no transcript.

Success is judged on the FILE, never on the process: the attempt counts as
renewed only when the account's credentials file states a later expiry
afterwards. Anything else is logged as a WARNING and, once the login really
lapses, requests for that account take the loud fallback
(:mod:`model_router.accounts`).

Bounded and rate-limited: one child at a time, a hard timeout, at most one
attempt per account per :data:`MIN_INTERVAL_S`, doubling to
:data:`MAX_BACKOFF_S` after failures. ``primary`` — the login the owner's own
native sessions keep fresh — is never touched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence

from vco_lib.gateway_accounts import PRIMARY_ID

from .accounts import AccountService

logger = logging.getLogger(__name__)

#: Start a refresh this many seconds before expiry. Below Claude Code's own
#: five-minute refresh window, so the child really refreshes.
REFRESH_LEAD_S = 240
#: How often the keeper looks.
TICK_S = 60.0
#: At most one attempt per account in this many seconds…
MIN_INTERVAL_S = 600.0
#: …doubling after each failure up to this.
MAX_BACKOFF_S = 3600.0
#: Hard bound on one child.
CHILD_TIMEOUT_S = 90.0
CLAUDE_BIN_ENV = "VCT_MODEL_GATEWAY_CLAUDE_BIN"

#: Env names the child must NOT inherit: any of them would make Claude Code
#: use another credential than the account's own login, or send the request
#: somewhere else.
_SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CONFIG_DIR",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
)

OUTCOME_RENEWED = "renewed"
OUTCOME_NOT_RENEWED = "not_renewed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NO_CLAUDE = "claude_not_found"
OUTCOME_SPAWN_FAILED = "spawn_failed"


@dataclass(frozen=True)
class RefreshAttempt:
    at: float
    outcome: str
    detail: str
    expires_in_s_after: Optional[int]


#: ``(argv, env, cwd, timeout) -> returncode`` (``None`` = killed on timeout).
Runner = Callable[[Sequence[str], Mapping[str, str], Path, float], Awaitable[Optional[int]]]


def resolve_claude_bin() -> Optional[str]:
    """``$VCT_MODEL_GATEWAY_CLAUDE_BIN`` when set (a file, else ``None``),
    else the one resolver for a detached process (:mod:`vco_lib.claude_cli`:
    PATH, then Claude Code's own install locations)."""
    from vco_lib.claude_cli import resolve_claude_cli

    custom = (os.environ.get(CLAUDE_BIN_ENV) or "").strip()
    return resolve_claude_cli(override=custom or None)


def refresh_command(claude_bin: str) -> list[str]:
    return [claude_bin, "-p", "--safe-mode", "--no-session-persistence",
            "--max-turns", "1", "ok"]


def refresh_env(base: Mapping[str, str], config_dir: str, dead_port: int) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in _SCRUBBED_ENV}
    env.update({
        "CLAUDE_CONFIG_DIR": config_dir,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{dead_port}",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    })
    return env


def spawn_kwargs(*, os_name: Optional[str] = None) -> dict:
    """How the child is started: its own session on POSIX (so a timeout can
    kill everything it started), and on Windows a console with NO window —
    the gateway is a console-less daemon, so a plain console child would get
    a visible window every time the keeper runs (the one windowless home,
    :mod:`vco_lib.windowless_spawn`)."""
    from vco_lib.windowless_spawn import detached_popen_kwargs

    return detached_popen_kwargs(os_name=os_name)


def kill_child_tree(proc) -> None:
    """Kill the child AND whatever it started: its whole process group on
    POSIX (it leads its own session), the child itself on Windows."""
    from vco_lib.hook_supervisor import signal_group

    signal_group(proc, int(getattr(signal, "SIGKILL", signal.SIGTERM)))


async def default_runner(argv: Sequence[str], env: Mapping[str, str], cwd: Path,
                         timeout_s: float) -> Optional[int]:
    """Spawn, wait with a hard timeout, kill our own child (and its process
    group) on timeout. The child's output is discarded: it is never needed
    and never logged."""
    proc = await asyncio.create_subprocess_exec(
        *argv, env=dict(env), cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **spawn_kwargs(),
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout_s)
    except asyncio.TimeoutError:
        kill_child_tree(proc)
        await proc.wait()
        return None


class _HeldPort:
    """A loopback port bound by this process and never listened on: a
    connection to it is refused, and no other process can take it while
    held."""

    def __enter__(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        return int(self._sock.getsockname()[1])

    def __exit__(self, *exc: object) -> None:
        self._sock.close()


class AccountRefresher:
    def __init__(
        self,
        service: AccountService,
        *,
        work_dir: Path,
        runner: Runner = default_runner,
        clock: Callable[[], float] = time.time,
        claude_bin: Optional[Callable[[], Optional[str]]] = None,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._service = service
        self._work_dir = work_dir
        self._runner = runner
        self._clock = clock
        self._claude_bin = claude_bin or resolve_claude_bin
        self._base_env = base_env
        self._last: dict[str, RefreshAttempt] = {}
        self._next_allowed: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    # ── decisions ────────────────────────────────────────────────────────
    def due(self, account_id: str, now: float) -> bool:
        """A non-primary account whose login exists and expires within
        :data:`REFRESH_LEAD_S` (or has expired), and no attempt is pending
        a back-off."""
        if account_id == PRIMARY_ID:
            return False
        reader = self._service.reader(account_id)
        if reader is None:
            return False
        state = reader.read()
        if state.state not in ("present", "expired") or not state.expires_at_ms:
            return False
        if state.expires_at_ms / 1000 - now > REFRESH_LEAD_S:
            return False
        return now >= self._next_allowed.get(account_id, 0.0)

    async def tick(self) -> list[RefreshAttempt]:
        """One pass over every registered account. Never raises."""
        done: list[RefreshAttempt] = []
        async with self._lock:
            # Keeps every login's cached state current — on macOS the Keychain
            # answer /health shows (it never runs `security` itself).
            try:
                self._service.primary_reader.read()
            except Exception:  # noqa: BLE001 — a warm-up never stops a tick
                pass
            reg = self._service.registry()
            for acct in reg.accounts:
                try:
                    if self.due(acct.id, self._clock()):
                        done.append(await self._refresh(acct.id, acct.config_dir))
                except Exception as exc:  # noqa: BLE001 — soft-fail, logged
                    logger.warning("model-gateway: login keeper failed for account %s: %s",
                                   acct.id, exc)
        return done

    async def _refresh(self, account_id: str, config_dir: str) -> RefreshAttempt:
        reader = self._service.reader(account_id)
        assert reader is not None  # noqa: S101 — `due` resolved it
        before = reader.read().expires_at_ms
        now = self._clock()
        claude_bin = self._claude_bin()
        if claude_bin is None:
            attempt = RefreshAttempt(now, OUTCOME_NO_CLAUDE,
                                     f"no `claude` executable found (set {CLAUDE_BIN_ENV})", None)
        else:
            self._work_dir.mkdir(parents=True, exist_ok=True)
            base = self._base_env if self._base_env is not None else os.environ
            try:
                with _HeldPort() as port:
                    code = await self._runner(
                        refresh_command(claude_bin),
                        refresh_env(base, config_dir, port),
                        self._work_dir,
                        CHILD_TIMEOUT_S,
                    )
            except OSError as exc:
                attempt = RefreshAttempt(now, OUTCOME_SPAWN_FAILED, str(exc), None)
            else:
                after_state = reader.read(fresh=True)  # the Keychain too
                after = after_state.expires_at_ms
                expires_in = int(after / 1000 - self._clock()) if after else None
                if after and after > (before or 0):
                    outcome, detail = OUTCOME_RENEWED, "the credentials file states a later expiry"
                elif code is None:
                    outcome, detail = OUTCOME_TIMEOUT, f"killed after {CHILD_TIMEOUT_S:.0f}s"
                else:
                    outcome, detail = (OUTCOME_NOT_RENEWED,
                                       f"claude exited {code}; the credentials file was not renewed")
                attempt = RefreshAttempt(now, outcome, detail, expires_in)
        self._record(account_id, attempt, config_dir)
        return attempt

    def _record(self, account_id: str, attempt: RefreshAttempt, config_dir: str) -> None:
        self._last[account_id] = attempt
        if attempt.outcome == OUTCOME_RENEWED:
            self._backoff.pop(account_id, None)
            self._next_allowed[account_id] = attempt.at + MIN_INTERVAL_S
            logger.info("model-gateway: renewed the login of Claude account %s "
                        "(expires in %ss)", account_id, attempt.expires_in_s_after)
            return
        backoff = min(MAX_BACKOFF_S, self._backoff.get(account_id, MIN_INTERVAL_S / 2) * 2)
        self._backoff[account_id] = backoff
        self._next_allowed[account_id] = attempt.at + backoff
        logger.warning(
            "model-gateway: could not renew the login of Claude account %s (%s: %s); "
            "next try in %.0f min. If it lapses, requests for it are served by the "
            "fallback account. Log it in again with: CLAUDE_CONFIG_DIR=%s claude auth login",
            account_id, attempt.outcome, attempt.detail, backoff / 60, config_dir,
        )

    # ── lifecycle / health ───────────────────────────────────────────────
    def status(self) -> dict:
        return {
            acct: {**asdict(att), "next_try_at": self._next_allowed.get(acct)}
            for acct, att in self._last.items()
        }

    async def run_forever(self, interval_s: float = TICK_S) -> None:
        """Tick forever. An unexpected raise is logged and the loop goes on:
        a keeper that died silently would let every extra login lapse."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — logged, the loop survives
                logger.warning("model-gateway: login keeper pass failed (%s: %s); "
                               "retrying in %.0fs", type(exc).__name__, exc, interval_s)
            await asyncio.sleep(interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = [
    "AccountRefresher",
    "CHILD_TIMEOUT_S",
    "MIN_INTERVAL_S",
    "REFRESH_LEAD_S",
    "RefreshAttempt",
    "refresh_command",
    "refresh_env",
    "resolve_claude_bin",
]
