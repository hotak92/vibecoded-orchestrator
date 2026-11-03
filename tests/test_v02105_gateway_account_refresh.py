# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the gateway keeps its extra Claude logins alive.

Shortly before an account's login expires the keeper runs ONE native
``claude -p`` with that account's ``CLAUDE_CONFIG_DIR`` and a base URL that
refuses connections, so Claude Code refreshes its own credentials file and no
model call is made. Success is judged on the file. Bounded, rate-limited,
logged, soft-fail.

A test double of the ``claude`` executable stands in for the real CLI: like
the shipped 2.1.312 bundle, it renews the login on a print-mode run and does
NOT on ``auth status``. The live check is in the owner's steps.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Mapping, Optional, Sequence

from model_router import account_refresh as ar
from model_router.accounts import AccountService
from model_router.auth import OAuthReader

TOKEN = "refresh-test-token-synthetic"
NEW_TOKEN = "refresh-test-token-renewed-synthetic"


def write_login(config_dir: Path, token: Optional[str], expires_in_s: float) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    section: dict = {"expiresAt": int((time.time() + expires_in_s) * 1000)}
    if token is not None:
        section["accessToken"] = token
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": section}))


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="acct-refresh-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.primary_dir = self.root / "primary"
        self.work_dir = self.root / "work"
        write_login(self.primary_dir, TOKEN, 60)  # near expiry, but primary
        registry = self.root / "accounts.json"
        registry.write_text(json.dumps({"schema": 1, "default": "primary", "accounts": [
            {"id": "work", "label": "Work", "config_dir": str(self.work_dir)}]}))
        self.service = AccountService(
            OAuthReader(self.primary_dir / ".credentials.json"), registry,
        )
        self.calls: list[tuple[list[str], dict, Path]] = []

    def keeper(self, runner=None, claude_bin: Optional[str] = "/fake/claude",
               base_env: Optional[Mapping[str, str]] = None) -> ar.AccountRefresher:
        return ar.AccountRefresher(
            self.service,
            work_dir=self.root / "keeper",
            runner=runner or self.renewing_runner,
            claude_bin=lambda: claude_bin,
            base_env=base_env if base_env is not None else {
                "PATH": "/usr/bin", "ANTHROPIC_AUTH_TOKEN": "host-token-synthetic",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:11460", "HOME": str(self.root),
            },
        )

    async def renewing_runner(self, argv: Sequence[str], env: Mapping[str, str],
                              cwd: Path, timeout: float) -> Optional[int]:
        self.calls.append((list(argv), dict(env), cwd))
        write_login(Path(env["CLAUDE_CONFIG_DIR"]), NEW_TOKEN, 8 * 3600)
        return 1  # the model request is refused, so claude exits non-zero

    async def idle_runner(self, argv, env, cwd, timeout) -> Optional[int]:
        self.calls.append((list(argv), dict(env), cwd))
        return 1


class SchedulingTests(_Base):
    async def test_far_from_expiry_is_left_alone(self) -> None:
        write_login(self.work_dir, TOKEN, 3600)
        self.assertEqual(await self.keeper().tick(), [])
        self.assertEqual(self.calls, [])

    async def test_inside_the_lead_window_it_renews(self) -> None:
        write_login(self.work_dir, TOKEN, ar.REFRESH_LEAD_S - 30)
        with self.assertLogs("model_router", level="INFO") as logs:
            done = await self.keeper().tick()
        self.assertEqual([a.outcome for a in done], [ar.OUTCOME_RENEWED])
        self.assertEqual(len(self.calls), 1, "exactly one child")
        self.assertTrue(any("renewed the login of Claude account work" in m for m in logs.output))
        self.assertTrue(self.service.reader("work").read().present)

    async def test_an_expired_login_is_renewed_too(self) -> None:
        write_login(self.work_dir, TOKEN, -600)
        done = await self.keeper().tick()
        self.assertEqual([a.outcome for a in done], [ar.OUTCOME_RENEWED])

    async def test_primary_is_never_touched(self) -> None:
        write_login(self.work_dir, TOKEN, 3600)
        keeper = self.keeper()
        await keeper.tick()
        self.assertEqual(self.calls, [], "primary expires in 60 s and is still not touched")
        self.assertFalse(keeper.due("primary", time.time()))
        self.assertTrue(keeper.due("work", time.time() + 3600), "leave-alone arm")

    async def test_a_logged_out_account_is_not_attempted(self) -> None:
        self.work_dir.mkdir()
        self.assertEqual(await self.keeper().tick(), [])
        self.assertEqual(self.calls, [])

    async def test_attempts_are_rate_limited_and_back_off(self) -> None:
        write_login(self.work_dir, TOKEN, 60)
        real_now = time.time()
        offset = [0.0]
        keeper = self.keeper(runner=self.idle_runner)
        keeper._clock = lambda: real_now + offset[0]  # noqa: SLF001 — injected clock
        with self.assertLogs("model_router", level="WARNING") as logs:
            first = await keeper.tick()
        self.assertEqual([a.outcome for a in first], [ar.OUTCOME_NOT_RENEWED])
        self.assertTrue(any("could not renew the login of Claude account work" in m
                            and "claude auth login" in m for m in logs.output))
        self.assertEqual(await keeper.tick(), [], "no second attempt inside the interval")
        offset[0] += ar.MIN_INTERVAL_S + 1
        self.assertEqual(len(await keeper.tick()), 1, "allowed again after the back-off")
        offset[0] += ar.MIN_INTERVAL_S + 1
        self.assertEqual(await keeper.tick(), [], "the back-off doubled after a second failure")
        self.assertEqual(len(self.calls), 2)

    async def test_status_names_outcome_never_a_token(self) -> None:
        write_login(self.work_dir, TOKEN, 30)
        keeper = self.keeper()
        await keeper.tick()
        status = keeper.status()
        self.assertEqual(status["work"]["outcome"], ar.OUTCOME_RENEWED)
        text = json.dumps(status)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(NEW_TOKEN, text)
        self.assertEqual(self.service.health(status)["accounts"][1]["refresh"]["outcome"],
                         ar.OUTCOME_RENEWED)

    async def test_no_claude_executable_is_reported(self) -> None:
        write_login(self.work_dir, TOKEN, 30)
        done = await self.keeper(claude_bin=None).tick()
        self.assertEqual([a.outcome for a in done], [ar.OUTCOME_NO_CLAUDE])
        self.assertEqual(self.calls, [])

    async def test_a_hung_child_is_a_timeout(self) -> None:
        write_login(self.work_dir, TOKEN, 30)

        async def hung(argv, env, cwd, timeout):
            return None

        done = await self.keeper(runner=hung).tick()
        self.assertEqual([a.outcome for a in done], [ar.OUTCOME_TIMEOUT])

    async def test_a_runner_crash_never_escapes_the_tick(self) -> None:
        write_login(self.work_dir, TOKEN, 30)

        async def boom(argv, env, cwd, timeout):
            raise RuntimeError("synthetic")

        self.assertEqual(await self.keeper(runner=boom).tick(), [])


class ChildShapeTests(_Base):
    async def test_the_child_uses_the_accounts_own_login_and_a_refusing_port(self) -> None:
        write_login(self.work_dir, TOKEN, 30)
        seen: dict = {}

        async def probe(argv, env, cwd, timeout):
            url = env["ANTHROPIC_BASE_URL"]
            port = int(url.rsplit(":", 1)[1])
            sock = socket.socket()
            try:
                sock.connect(("127.0.0.1", port))
                seen["refused"] = False
            except ConnectionRefusedError:
                seen["refused"] = True
            finally:
                sock.close()
            seen.update(argv=list(argv), env=dict(env), timeout=timeout)
            return 1

        await self.keeper(runner=probe).tick()
        self.assertTrue(seen["refused"], "the model request can reach nothing")
        env = seen["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.work_dir))
        self.assertTrue(env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertNotEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:11460")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env, "the gateway host token never reaches it")
        self.assertEqual(env["CLAUDE_CODE_MAX_RETRIES"], "0")
        self.assertEqual(seen["argv"][1:], ["-p", "--safe-mode", "--no-session-persistence",
                                            "--max-turns", "1", "ok"])
        self.assertLessEqual(seen["timeout"], ar.CHILD_TIMEOUT_S)


_DOUBLE = textwrap.dedent('''\
    import json, os, sys, time
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    path = os.path.join(cfg, ".credentials.json")
    args = sys.argv[1:]
    # Like Claude Code 2.1.312: `auth status` reports presence and refreshes
    # nothing; a print-mode run refreshes a login that expires within 5 min
    # before it sends its request.
    if args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": os.path.exists(path)}))
        sys.exit(0)
    if "-p" in args:
        with open(path) as fh:
            doc = json.load(fh)
        oauth = doc["claudeAiOauth"]
        if time.time() * 1000 + 300000 >= oauth["expiresAt"]:
            oauth["accessToken"] = "double-renewed-token-synthetic"
            oauth["expiresAt"] = int(time.time() * 1000) + 8 * 3600 * 1000
            with open(path, "w") as fh:
                json.dump(doc, fh)
        sys.exit(1)  # the request to the refusing port fails
    sys.exit(2)
''')


async def _python_runner(argv, env, cwd, timeout):
    """The real runner, with the double run by this interpreter (portable:
    no shebang, no exec bit, the same on every OS)."""
    return await ar.default_runner([sys.executable, *argv], env, cwd, timeout)


class TestDoubleTests(_Base):
    """Which command rewrites the credentials file, against a double that
    behaves like the shipped CLI read on 2026-11-03."""

    def make_double(self) -> str:
        path = self.root / "claude_double.py"
        path.write_text(_DOUBLE)
        return str(path)

    async def test_the_keepers_command_renews_through_a_real_child(self) -> None:
        write_login(self.work_dir, TOKEN, 60)
        before = (self.work_dir / ".credentials.json").stat().st_mtime_ns
        keeper = self.keeper(runner=_python_runner, claude_bin=self.make_double(),
                             base_env=dict(os.environ))
        done = await keeper.tick()
        self.assertEqual([a.outcome for a in done], [ar.OUTCOME_RENEWED])
        self.assertNotEqual((self.work_dir / ".credentials.json").stat().st_mtime_ns, before)

    async def test_auth_status_would_not_have_renewed(self) -> None:
        write_login(self.work_dir, TOKEN, 60)
        double = self.make_double()
        before = (self.work_dir / ".credentials.json").read_bytes()
        code = await _python_runner(
            [double, "auth", "status"], {**os.environ, "CLAUDE_CONFIG_DIR": str(self.work_dir)},
            self.root, 30,
        )
        self.assertEqual(code, 0)
        self.assertEqual((self.work_dir / ".credentials.json").read_bytes(), before)

    async def test_the_default_runner_kills_a_hung_child(self) -> None:
        hang = self.root / "hang.py"
        hang.write_text("import time\ntime.sleep(30)\n")
        started = time.monotonic()
        code = await _python_runner([str(hang)], dict(os.environ), self.root, 0.5)
        self.assertIsNone(code)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DaemonWiringTests(unittest.IsolatedAsyncioTestCase):
    """The DAEMON attaches and starts the keeper; ``create_app`` alone does not."""

    async def test_only_the_daemon_starts_the_keeper(self) -> None:
        from aiohttp.test_utils import TestServer

        from model_router.__main__ import _start_account_refresh_at_startup
        from model_router.config import GatewayConfig
        from model_router.server import APP_KEY, create_app

        with tempfile.TemporaryDirectory(prefix="acct-wiring-") as tmp:
            config = GatewayConfig(host="127.0.0.1", port=0, token="t",
                                   credentials_file=Path(tmp) / ".credentials.json",
                                   accounts_file=Path(tmp) / "accounts.json")
            plain = create_app(config)
            async with TestServer(plain):
                self.assertIsNone(plain[APP_KEY].account_refresher)
            daemon = create_app(config)
            _start_account_refresh_at_startup(daemon)
            async with TestServer(daemon):
                keeper = daemon[APP_KEY].account_refresher
                self.assertIsInstance(keeper, ar.AccountRefresher)
                self.assertIsNotNone(keeper._task)  # noqa: SLF001
            self.assertTrue(keeper._task.done(), "stopped on cleanup")  # noqa: SLF001
