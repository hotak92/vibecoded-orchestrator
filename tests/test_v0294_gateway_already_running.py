# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""An already-running gateway is SUCCESS, not a failure — and the restart loop.

Field incident, 2026-09-08, this machine. A gateway was started by hand (pid
1147171, port 11437). The user-session unit then ran ``vct-model-gateway
serve`` at login; ``_acquire_single_instance`` saw the live pid, printed "stop
it first, or delete that file" and exited 1. The unit carries
``Restart=on-failure`` with ``RestartSec=10s``, so systemd restarted it every
ten seconds — 1442 times over four hours, appending 294 KB to the boot log —
and never tripped the default start limit, which needs five starts inside ten
SECONDS. The unit's own comment claimed that limit would "give up rather than
spin"; it could not, at that RestartSec.

Three defects, one incident, one test file:

* **exit code** — "already running" is the desired end state of a boot-time
  start. It exits 0 now, so ``Restart=on-failure`` is satisfied. What it must
  NOT do is exit 0 for any live pid: the number may have been reused, so the
  claim is checked against ``/health`` and the service name;
* **the printed advice** — "delete that file" was printed for a HEALTHY
  daemon. A printed command is shipped code and must never act on a condition
  that was not confirmed;
* **the unit's own limits** — asserted in ``tests/test_model_gateway_boot.py``
  beside the rest of the unit's shape.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from unittest import mock

from aiohttp import web

from model_router import __main__ as entry
from model_router import config as gateway_config

from tests.common.ports import free_block as _free_block


class _HealthHandler(BaseHTTPRequestHandler):
    """A stand-in that answers /health with whatever the test set."""

    payload: object = {"ok": True, "service": "vct-model-gateway"}
    status: int = 200

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        body = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence: this server's access log is not the test's output."""


class HealthProbeTests(unittest.TestCase):
    """``_gateway_answers`` against a REAL listener, because the point is
    that a listener alone proves nothing."""

    def _serve(self, payload: object, status: int = 200) -> int:
        _HealthHandler.payload = payload
        _HealthHandler.status = status
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_our_own_health_document_is_recognised(self) -> None:
        port = self._serve({"ok": True, "service": "vct-model-gateway"})
        self.assertTrue(entry._gateway_answers("127.0.0.1", port, 2.0))

    def test_another_service_on_the_port_is_not_us(self) -> None:
        """THE case the port chain exists for: a legacy container answering
        happily on the port the gateway would like."""
        port = self._serve({"ok": True, "service": "some-other-daemon"})
        self.assertFalse(entry._gateway_answers("127.0.0.1", port, 2.0))

    def test_a_non_json_answer_is_not_us(self) -> None:
        port = self._serve("<html>hello</html>")
        self.assertFalse(entry._gateway_answers("127.0.0.1", port, 2.0))

    def test_a_non_200_answer_is_not_us(self) -> None:
        port = self._serve({"service": "vct-model-gateway"}, status=503)
        self.assertFalse(entry._gateway_answers("127.0.0.1", port, 2.0))

    def test_nothing_listening_is_not_us(self) -> None:
        port = _free_block(1)[0]
        self.assertFalse(entry._gateway_answers("127.0.0.1", port, 1.0))

    def test_an_ipv6_host_is_bracketed_in_the_url(self) -> None:
        """``http://::1:7/health`` is not a URL; the literal needs brackets."""
        self.assertEqual(entry._health_url("::1", 7), "http://[::1]:7/health")
        self.assertEqual(
            entry._health_url("127.0.0.1", 7), "http://127.0.0.1:7/health",
        )

    def test_the_probe_ignores_an_http_proxy(self) -> None:
        """A loopback probe routed through a proxy answers about the PROXY."""
        port = self._serve({"ok": True, "service": "vct-model-gateway"})
        with mock.patch.dict(
            os.environ,
            {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"},
        ):
            self.assertTrue(entry._gateway_answers("127.0.0.1", port, 2.0))


class _ServeCase(unittest.TestCase):
    """``_serve`` with a scratch state root and no real event loop."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-already-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"VCT_STATE_DIR": str(self.root), "VCT_MODEL_GATEWAY_HOST": "127.0.0.1"},
        )
        env.start()
        self.addCleanup(env.stop)
        for key in ("VCT_MODEL_GATEWAY_PORT", "VCT_MODEL_GATEWAY_CREDENTIALS"):
            os.environ.pop(key, None)

        logger = logging.getLogger("model_router")
        before = list(logger.handlers)
        self.addCleanup(lambda: logger.handlers.__setitem__(slice(None), before))

        # The takeover path waits for a gateway that will never answer; the
        # deadline is a constant so a test can spend nothing on it.
        deadline = mock.patch.object(entry, "_HEALTH_PROBE_DEADLINE_S", 0.0)
        deadline.start()
        self.addCleanup(deadline.stop)

        alive = mock.patch("vco_lib.deferral_probes.pid_is_alive", lambda pid: True)
        alive.start()
        self.addCleanup(alive.stop)

        self.block = _free_block(3)
        fallback = mock.patch.object(
            gateway_config, "FALLBACK_PORT_RANGE", self.block,
        )
        fallback.start()
        self.addCleanup(fallback.stop)
        gateway_config.port_path().write_text(
            f"{self.block[0]}\n", encoding="utf-8",
        )

    def _run(self, *, answers: bool) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch.object(entry, "_gateway_answers", return_value=answers):
            with mock.patch.object(web, "run_app", return_value=None):
                with redirect_stdout(buffer):
                    code = entry._serve(None)
        return code, buffer.getvalue()


class AlreadyRunningTests(_ServeCase):
    def test_a_live_healthy_gateway_is_exit_zero_and_no_bind(self) -> None:
        """The fix. ``Restart=on-failure`` must see success, and the port
        this process would have taken must be left alone."""
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("a bind was attempted for a running gateway")

        with mock.patch.object(entry, "_bind_with_fallback", refuse):
            code, out = self._run(answers=True)

        self.assertEqual(code, 0)
        self.assertIn("already running", out)
        self.assertIn("424242", out)
        self.assertEqual(
            gateway_config.pid_path().read_text(encoding="utf-8").strip(),
            "424242",
            "the running instance's claim must survive our start",
        )

    def test_the_exit_zero_path_prints_no_destructive_advice(self) -> None:
        """"Delete that file" was printed for a HEALTHY daemon."""
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")
        code, out = self._run(answers=True)
        self.assertEqual(code, 0)
        self.assertNotIn("delete", out.lower())
        self.assertNotIn("Stop it first", out)

    def test_a_live_pid_that_answers_nothing_is_taken_over(self) -> None:
        """A reused pid, or a start that died: bind anyway."""
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")
        with self.assertLogs("model_router", level="WARNING") as captured:
            code, out = self._run(answers=False)
        self.assertEqual(code, 0)
        self.assertIn("no gateway answered", "\n".join(captured.output))
        self.assertNotIn("already running", out)
        self.assertTrue(gateway_config.port_path().is_file())
        self.assertFalse(
            gateway_config.pid_path().exists(),
            "we claimed the file, so our own exit releases it",
        )

    def test_a_dead_pid_is_still_taken_over_without_probing(self) -> None:
        """LEAVE-ALONE half: the pre-existing stale-file path is unchanged,
        and costs no health probe."""
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")
        with mock.patch("vco_lib.deferral_probes.pid_is_alive", lambda pid: False):
            with mock.patch.object(entry, "_gateway_answers") as probe:
                with mock.patch.object(web, "run_app", return_value=None):
                    self.assertEqual(entry._serve(None), 0)
        probe.assert_not_called()
        self.assertTrue(gateway_config.port_path().is_file())

    def test_no_pid_file_at_all_never_probes(self) -> None:
        """The ordinary start pays nothing for any of this."""
        with mock.patch.object(entry, "_gateway_answers") as probe:
            with mock.patch.object(web, "run_app", return_value=None):
                self.assertEqual(entry._serve(None), 0)
        probe.assert_not_called()


class ExclusiveClaimTests(_ServeCase):
    """One winner. The pid file and the port file must name one instance.

    Read-then-write let two starts both see no live holder, both write their
    own pid and both bind: the pid file then named one daemon while the port
    file named the other, and every reader that trusts the pair was reading
    two different processes.
    """

    def test_a_stale_file_is_unlinked_and_reclaimed(self) -> None:
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")
        with mock.patch("vco_lib.deferral_probes.pid_is_alive", lambda pid: False):
            with mock.patch.object(web, "run_app") as run:
                self.assertEqual(entry._serve(None), 0)
                # DURING the run the file is ours, which is what "reclaimed"
                # means; the release only happens on the way out.
                run.assert_called_once()
        self.assertFalse(gateway_config.pid_path().exists())
        self.assertTrue(gateway_config.port_path().is_file())

    def test_a_garbage_file_is_reclaimed_without_probing(self) -> None:
        gateway_config.pid_path().write_text("not-a-pid\n", encoding="utf-8")
        with mock.patch.object(entry, "_gateway_answers") as probe:
            with mock.patch.object(web, "run_app", return_value=None):
                self.assertEqual(entry._serve(None), 0)
        probe.assert_not_called()
        self.assertTrue(gateway_config.port_path().is_file())

    def test_losing_the_reclaim_exits_zero_without_binding(self) -> None:
        """A contender created the file in the instant between our unlink and
        our create: that start is coming up, so this one steps aside — and
        exits 0, or `Restart=on-failure` turns a lost race into the spin this
        whole path exists to end."""
        gateway_config.pid_path().write_text("424242\n", encoding="utf-8")
        real_claim = entry._acquire_single_instance
        calls: list[int] = []

        def claim(path: Path):
            calls.append(1)
            if len(calls) == 1:
                return real_claim(path)
            # Simulate the contender winning the re-create.
            path.write_text("999999\n", encoding="utf-8")
            return 999999

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("a bind was attempted after losing the claim")

        with mock.patch("vco_lib.deferral_probes.pid_is_alive", lambda pid: False):
            with mock.patch.object(entry, "_acquire_single_instance", claim):
                with mock.patch.object(entry, "_bind_with_fallback", refuse):
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        code = entry._serve(None)

        self.assertEqual(code, 0)
        self.assertIn("another start claimed", buffer.getvalue())
        self.assertEqual(
            gateway_config.pid_path().read_text(encoding="utf-8").strip(),
            "999999",
            "the contender's claim is left alone",
        )

    def test_the_pid_file_is_owner_only_after_an_exclusive_claim(self) -> None:
        import stat
        import sys as _sys

        if _sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        self.assertIsNone(entry._acquire_single_instance(gateway_config.pid_path()))
        self.addCleanup(gateway_config.pid_path().unlink)
        self.assertEqual(
            stat.S_IMODE(gateway_config.pid_path().stat().st_mode), 0o600,
        )


class ServeCleanupTests(_ServeCase):
    """The bind is the point of no return: everything after it must be
    reached by the same cleanup.

    ``create_app`` and the two port-file writes sat BEFORE the ``try``, so a
    failure in either — a read-only state dir, an invalid vendor registry —
    left the function by an exception with the listeners still open and the
    pid file still naming this process. The next start then found a live pid
    with no gateway behind it: recoverable now (it takes the file over), but
    one probe and one warning that need not exist.
    """

    def test_a_failing_port_file_write_still_releases_everything(self) -> None:
        closed: list[bool] = []
        real_bind = entry._bind_with_fallback

        def watched(host: str, port: int, explicit: bool):
            socks, chosen = real_bind(host, port, explicit)

            class _Watch:
                def __init__(self, sock):
                    self._sock = sock

                def __getattr__(self, name):
                    return getattr(self._sock, name)

                def close(self):
                    closed.append(True)
                    self._sock.close()

            return [_Watch(s) for s in socks], chosen

        def explode(path: Path, text: str, *, strict: bool) -> None:
            raise OSError("state directory is read-only")

        with mock.patch.object(entry, "_bind_with_fallback", watched):
            with mock.patch.object(entry, "_write_owner_only", explode):
                with mock.patch.object(web, "run_app") as run:
                    with self.assertRaises(OSError):
                        entry._serve(None)
                    run.assert_not_called()

        self.assertTrue(closed, "the listening sockets must be closed")
        self.assertFalse(
            gateway_config.pid_path().exists(),
            "a pid file naming a process that is leaving blocks the next start",
        )

    def test_a_failing_app_build_also_releases_everything(self) -> None:
        """The write helper is armed to fail LOUDLY if it runs first: the
        port file must be written after the app builds, never before."""
        with mock.patch.object(
            entry, "_write_owner_only", side_effect=AssertionError("too early"),
        ):
            with mock.patch(
                "model_router.server.create_app",
                side_effect=RuntimeError("bad vendor registry"),
            ):
                with self.assertRaises(RuntimeError):
                    entry._serve(None)
        self.assertFalse(
            gateway_config.pid_path().exists(),
            "a pid file naming a process that is leaving blocks the next start",
        )


class AccessLogTests(_ServeCase):
    """ONE line per request, in ONE shape — including aiohttp's own.

    ``server.py``'s logging policy promises a single access line whose first
    field is what the CLIENT asked for. ``run_app`` defaults to attaching
    aiohttp's access logger at INFO, which propagates into the same file
    handler and writes a second, differently-shaped line for every request:
    not extra detail, just a log nobody can grep.
    """

    def setUp(self) -> None:
        super().setUp()
        # The DAEMON configures the root logger at INFO; pytest leaves it at
        # WARNING, which would suppress aiohttp's access line for the wrong
        # reason and make the end-to-end test below pass vacuously.
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level

        def restore() -> None:
            # CLOSE the FileHandler `_configure_logging` attached, rather
            # than only dropping the reference: an open handle on a file
            # inside the scratch state root outlives the test otherwise, and
            # on Windows an open handle blocks the TemporaryDirectory
            # cleanup outright.
            for handler in root.handlers:
                if handler not in saved_handlers:
                    handler.close()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)
        root.handlers.clear()
        root.setLevel(logging.INFO)

    def test_run_app_is_asked_for_no_access_log(self) -> None:
        with mock.patch.object(web, "run_app") as run:
            self.assertEqual(entry._serve(None), 0)
        run.assert_called_once()
        self.assertIn("access_log", run.call_args.kwargs)
        self.assertIsNone(run.call_args.kwargs["access_log"])

    def test_a_real_request_writes_exactly_one_line_to_the_log_file(
        self,
    ) -> None:
        """End to end against a real aiohttp server on the bound socket.

        BOTH halves, because ``access_log=None`` has two ways to be wrong:
        it can fail to suppress aiohttp's line, and it can suppress OURS.
        So the run makes three requests:

        * ``/health`` — the gateway deliberately logs nothing for it, so
          any line naming it came from aiohttp;
        * an AUTHORISED ``/v1/messages`` naming a model no family claims —
          refused by the routing layer with ONE ``route=refused`` line;
        * a BARE ``/v1/messages`` with no token — refused at the door, and
          logged in the same shape (``reason=unauthorised``) now that
          "one line per request" includes the request that never got in.
        """
        import asyncio
        import urllib.error
        import urllib.request

        from aiohttp.log import access_logger

        log_file = gateway_config.log_path()
        served: dict = {}

        def serve(app, **kwargs):
            served["kwargs"] = kwargs

            async def run() -> None:
                # DEFAULTS to aiohttp's own access logger exactly as
                # ``run_app`` does, so dropping the kwarg from ``_serve``
                # makes the second line reappear and this test go red.
                runner = web.AppRunner(
                    app, access_log=kwargs.get("access_log", access_logger),
                )
                await runner.setup()
                site = web.SockSite(runner, kwargs["sock"][0])
                await site.start()
                port = kwargs["sock"][0].getsockname()[1]

                def call(
                    path: str,
                    body: bytes | None = None,
                    *,
                    authorised: bool = True,
                ) -> None:
                    headers = {"content-type": "application/json"}
                    if body is not None and authorised:
                        # The daemon's OWN host token, generated into the
                        # scratch state root by this very ``_serve`` call.
                        # Synthetic, local, and never printed.
                        headers["authorization"] = "Bearer " + (
                            gateway_config.token_path()
                            .read_text(encoding="utf-8").strip()
                        )
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=body, headers=headers,
                    )
                    try:
                        urllib.request.urlopen(request, timeout=5).read()
                    except urllib.error.HTTPError:
                        pass  # a refusal is a SERVED request, which is the point

                await asyncio.to_thread(call, "/health")
                await asyncio.to_thread(
                    call, "/v1/messages", b'{"model":"no-such-family/x"}',
                )
                await asyncio.to_thread(
                    lambda: call(
                        "/v1/messages", b'{"model":"claude-opus-5"}',
                        authorised=False,
                    )
                )
                await runner.cleanup()

            asyncio.run(run())

        with mock.patch.object(web, "run_app", serve):
            self.assertEqual(entry._serve(None), 0)

        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIsNone(
            served["kwargs"].get("access_log", access_logger),
            "run_app must be told there is no access logger",
        )
        written = log_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            [line for line in written if "/health" in line], [],
            "the gateway logs no line for /health, and aiohttp's access "
            "logger must not add one",
        )
        ours = [line for line in written if "model-gateway: requested=" in line]
        self.assertEqual(
            len(ours), 2,
            f"ONE line for the routed refusal and ONE for the 401: {ours}",
        )
        refused = [line for line in ours if "reason=unauthorised" in line]
        self.assertEqual(
            len(refused), 1,
            f"exactly one 'refused … reason=unauthorised' line: {ours}",
        )
        self.assertIn("route=refused", refused[0])
        self.assertIn("status=401", refused[0])
        self.assertIn("method=POST", refused[0])
        self.assertIn("path='/v1/messages'", refused[0])
        self.assertNotIn(
            "/v1/messages HTTP/1.1", "\n".join(written),
            "that shape is aiohttp's own access format, not ours",
        )


class StderrLogLevelTests(unittest.TestCase):
    """The boot log must hold what the daemon could NOT record, not a copy.

    ``StandardError=append:<name>.boot.log`` means this process's stderr IS
    the boot log. With the stderr handler at INFO every access line landed
    there as well as in the daemon's own log, so the boot log grew for the
    life of a HEALTHY daemon — not only across failed starts — and nothing
    rotates it between starts.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-stderr-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level

        def restore() -> None:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)
        root.handlers.clear()

    def _run(self, log_file: Path) -> tuple[str, Optional[str]]:
        buffer = io.StringIO()
        with mock.patch.object(sys, "stderr", buffer):
            warning = entry._configure_logging(log_file)
            logging.getLogger("model_router").info("an access line")
            logging.getLogger("model_router").warning("a real problem")
        for handler in logging.getLogger().handlers:
            handler.flush()
        return buffer.getvalue(), warning

    def test_info_reaches_the_file_and_not_stderr(self) -> None:
        log_file = self.root / "logs" / "model-gateway.log"
        stderr, warning = self._run(log_file)
        self.assertIsNone(warning)
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("an access line", text)
        self.assertIn("a real problem", text)
        self.assertNotIn("an access line", stderr)
        self.assertIn(
            "a real problem", stderr,
            "a WARNING still reaches the init system's capture file",
        )

    def test_stderr_keeps_info_when_the_file_handler_could_not_open(
        self,
    ) -> None:
        """LEAVE-ALONE half: then stderr is the ONLY place the records exist,
        and silencing it would lose them."""
        blocker = self.root / "logs"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory\n", encoding="utf-8")
        stderr, warning = self._run(blocker / "model-gateway.log")
        assert warning is not None
        self.assertIn("logging to stderr only", warning)
        self.assertIn("an access line", stderr)
        self.assertIn("a real problem", stderr)


class BootLogRotationTests(_ServeCase):
    """The 294 KB. Nothing else rotates this file.

    The init system opens ``<state>/logs/model-gateway.boot.log`` once and
    appends forever; a daemon in a restart loop writes the same refusal into
    it every RestartSec until somebody notices. Trimming it at start is the
    only place with both the path and a reason to look.
    """

    def _boot_log(self) -> Path:
        from vco_lib.boot_service import boot_log_file

        return boot_log_file(gateway_config.log_path())

    def test_an_oversized_boot_log_is_trimmed_at_start(self) -> None:
        path = self._boot_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(f"refusal {i}\n" for i in range(40_000)), encoding="utf-8",
        )
        oversized = path.stat().st_size
        self.assertGreater(oversized, entry._BOOT_LOG_MAX_BYTES)

        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 0)

        self.assertLess(path.stat().st_size, oversized)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), entry._BOOT_LOG_KEEP_LINES)
        self.assertEqual(
            lines[-1], "refusal 39999", "the TAIL is what a diagnosis needs",
        )

    def test_a_small_boot_log_is_left_byte_identical(self) -> None:
        path = self._boot_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("one refusal\n", encoding="utf-8")
        before = path.read_bytes()
        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_no_boot_log_at_all_is_not_created(self) -> None:
        """A CLI start has no init-system capture file; do not invent one."""
        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 0)
        self.assertFalse(self._boot_log().exists())


class ProbeCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-candidates-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"VCT_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for key in ("VCT_MODEL_GATEWAY_PORT",):
            os.environ.pop(key, None)

    def test_every_source_is_a_candidate_best_first(self) -> None:
        gateway_config.port_path().write_text("21001\n", encoding="utf-8")
        gateway_config.last_port_path().write_text("21002\n", encoding="utf-8")
        self.assertEqual(
            gateway_config.port_candidates(),
            [21001, 21002, gateway_config.DEFAULT_PORT],
        )

    def test_agreement_collapses_to_one_probe(self) -> None:
        gateway_config.port_path().write_text("21001\n", encoding="utf-8")
        gateway_config.last_port_path().write_text("21001\n", encoding="utf-8")
        self.assertEqual(
            gateway_config.port_candidates(), [21001, gateway_config.DEFAULT_PORT],
        )

    def test_resolve_port_is_the_head_of_the_candidates(self) -> None:
        """One order, not two: the resolver cannot drift from the prober."""
        gateway_config.last_port_path().write_text("21003\n", encoding="utf-8")
        self.assertEqual(
            gateway_config.resolve_port(), gateway_config.port_candidates()[0],
        )

    def test_every_candidate_is_tried_on_each_round(self) -> None:
        """The daemon is on ONE of them and the sources can disagree."""
        seen: list[int] = []

        def answers(host: str, port: int, timeout: float) -> bool:
            seen.append(port)
            return port == 21002

        with mock.patch.object(entry, "_gateway_answers", answers):
            found = entry._probe_running_gateway(
                "127.0.0.1", [21001, 21002, 21003], deadline_s=0.0,
            )
        self.assertEqual(found, 21002)
        self.assertEqual(seen, [21001, 21002])

    def test_the_probe_gives_up_at_its_deadline(self) -> None:
        with mock.patch.object(entry, "_gateway_answers", return_value=False):
            self.assertIsNone(
                entry._probe_running_gateway("127.0.0.1", [21001], deadline_s=0.0),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
