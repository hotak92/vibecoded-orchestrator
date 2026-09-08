# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE port chain: the daemon resolves and falls back exactly like the launcher.

The 2026-09-08 machine is the case. A legacy container owned the documented
default port; the launcher coped by starting the gateway on the first port of
its fallback range and remembering it, but the DAEMON's own chain was env ->
port file -> default and its start was a single bind attempt. So a
login-registered gateway could never come up on that machine, and if it had,
the two sides would have disagreed about where it was.

(The range has since moved to 11460-11468, clear of every port this codebase
already reserves — 11439 legacy RL, 11440 code-embedding, 11442 and 11443 for
the two RL supervisors — so the digits in a field report from that day name
the old range, not this one.)

Three things are pinned here:

* **the resolution order** — env, then the running daemon's port file, then
  the last-port record, then the default. Each step is evidence; the default
  is the answer only when there is none;
* **the fallback, and its limit** — a RESOLVED port moves off a busy port, an
  EXPLICIT one never does. Both halves, because a fallback that always fires
  is as wrong as one that never does: silently moving off a pinned port sends
  every configured client at nothing;
* **the cross-language literals** — the basename and the range digits, read
  out of the Rust source, so the two implementations of one chain cannot
  drift.
"""
from __future__ import annotations

import errno
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from model_router import __main__ as entry
from model_router import config as gateway_config

from tests.common.env import EnvIsolationMixin
from tests.common.ports import free_block as _free_block
from tests.common.ports import free_port as _free_port

REPO = Path(__file__).resolve().parent.parent
RUST = REPO / "launcher" / "src-tauri" / "src" / "commands" / "model_gateway.rs"


class _StateCase(EnvIsolationMixin, unittest.TestCase):
    """A scratch state root with every gateway env key cleared.

    ``set_env`` comes from :class:`tests.common.env.EnvIsolationMixin`: three
    suites had grown the same helper with the same paragraph explaining the
    same leak, which is one paragraph too many.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-portchain-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for key in (
            "VCT_MODEL_GATEWAY_PORT",
            "VCT_MODEL_GATEWAY_HOST",
            "VCT_MODEL_GATEWAY_CREDENTIALS",
            "VCT_MODEL_GATEWAY_CONTEXT_TABLE",
        ):
            self.set_env(key, None)
        self.set_env("VCT_STATE_DIR", str(self.root))


class ResolutionChainTests(_StateCase):
    """Reading order only — nothing here binds anything.

    The port numbers below are arbitrary EVIDENCE values written into files
    under a scratch state root; they are not fallback-range members and mean
    nothing beyond "a number that is not the default".
    """

    def test_the_default_answers_when_there_is_no_evidence(self) -> None:
        self.assertEqual(gateway_config.resolve_port(), gateway_config.DEFAULT_PORT)

    def test_the_last_port_record_beats_the_default(self) -> None:
        """The step that keeps a STOPPED gateway findable."""
        gateway_config.last_port_path().write_text("11441\n", encoding="utf-8")
        self.assertEqual(gateway_config.resolve_port(), 11441)

    def test_the_running_port_file_beats_the_record(self) -> None:
        gateway_config.last_port_path().write_text("11441\n", encoding="utf-8")
        gateway_config.port_path().write_text("11438\n", encoding="utf-8")
        self.assertEqual(gateway_config.resolve_port(), 11438)

    def test_the_env_beats_everything(self) -> None:
        gateway_config.last_port_path().write_text("11441\n", encoding="utf-8")
        gateway_config.port_path().write_text("11438\n", encoding="utf-8")
        self.set_env("VCT_MODEL_GATEWAY_PORT", "11500")
        self.assertEqual(gateway_config.resolve_port(), 11500)

    def test_a_corrupt_record_falls_through_rather_than_erroring(self) -> None:
        for junk in ("", "   ", "not-a-port", "0", "70000", "11441 11442"):
            with self.subTest(junk=junk):
                gateway_config.last_port_path().write_text(junk, encoding="utf-8")
                self.assertEqual(
                    gateway_config.resolve_port(), gateway_config.DEFAULT_PORT,
                )

    def test_a_corrupt_port_file_falls_through_to_the_record(self) -> None:
        """Not to the default: the next EVIDENCE, then the default."""
        gateway_config.port_path().write_text("not-a-port\n", encoding="utf-8")
        gateway_config.last_port_path().write_text("11441\n", encoding="utf-8")
        self.assertEqual(gateway_config.resolve_port(), 11441)

    def test_the_record_sits_beside_the_port_file(self) -> None:
        self.assertEqual(
            gateway_config.last_port_path().parent,
            gateway_config.port_path().parent,
        )
        self.assertEqual(
            gateway_config.last_port_path().name,
            gateway_config.LAST_PORT_BASENAME,
        )

    def test_explicit_port_is_only_the_env(self) -> None:
        self.assertIsNone(gateway_config.explicit_port())
        gateway_config.port_path().write_text("11438\n", encoding="utf-8")
        self.assertIsNone(
            gateway_config.explicit_port(),
            "a port FILE is evidence, not a pin — only the env pins",
        )
        self.set_env("VCT_MODEL_GATEWAY_PORT", "11500")
        self.assertEqual(gateway_config.explicit_port(), 11500)

    def test_a_nonsense_env_value_is_not_a_pin(self) -> None:
        for junk in ("banana", "0", "70000", "-1", ""):
            with self.subTest(junk=junk):
                self.set_env("VCT_MODEL_GATEWAY_PORT", junk)
                self.assertIsNone(gateway_config.explicit_port())


#: The two port helpers this file used to define live in
#: ``tests/common/ports.py`` — five files had grown a copy of the same three
#: lines. The shipped fallback range is still not usable here: those ports
#: are where a real gateway lands on a developer machine, and a test that
#: binds a documented port fights whatever owns it. So a block is DISCOVERED
#: and then INJECTED — the code under test iterates whatever the constant
#: holds, which is the property that makes it testable without pinning the
#: machine's real ports.


class FallbackTests(_StateCase):
    """Driven against REAL bound sockets — the thing being tested is a bind."""

    def setUp(self) -> None:
        super().setUp()
        self.block = _free_block(4)
        patch = mock.patch.object(gateway_config, "FALLBACK_PORT_RANGE", self.block)
        patch.start()
        self.addCleanup(patch.stop)

    def _hold(self, port: int) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", port))
        held.listen(8)
        self.addCleanup(held.close)

    def _bound(self, port: int, explicit: bool = False) -> int:
        """Bind through the fallback and register every socket for cleanup."""
        socks, chosen = entry._bind_with_fallback("127.0.0.1", port, explicit)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertTrue(socks, "a successful bind produces at least one socket")
        return chosen

    def test_a_free_port_is_used_unchanged(self) -> None:
        """The leave-alone half: never 'helpfully' moved."""
        self.assertEqual(self._bound(self.block[0]), self.block[0])

    def test_a_busy_resolved_port_moves_to_the_next_free_one(self) -> None:
        self._hold(self.block[0])
        self._hold(self.block[1])
        self.assertEqual(
            self._bound(self.block[0]), self.block[2], "the range is tried in order",
        )

    def test_an_explicit_port_never_falls_back(self) -> None:
        self._hold(self.block[0])
        with self.assertRaises(OSError) as caught:
            entry._bind_with_fallback("127.0.0.1", self.block[0], True)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_every_port_taken_raises_rather_than_scanning_on(self) -> None:
        for port in self.block:
            self._hold(port)
        with self.assertRaises(OSError) as caught:
            entry._bind_with_fallback("127.0.0.1", self.block[0], False)
        self.assertIn("every fallback", str(caught.exception))


class _FakeSocket:
    """Records what was asked of it; binds nothing.

    The platform question is "which option is SET", and that is not
    observable through a real socket — a Linux box cannot bind like a Mac.
    So the socket is faked and the assertion is about the CALL, which is the
    only part of the decision that differs per platform.
    """

    def __init__(self, family: int, socktype: int, proto: int = 0) -> None:
        self.family = family
        self.socktype = socktype
        self.proto = proto
        self.options: list[tuple[int, int, int]] = []
        self.bound: object = None
        self.backlog = 0

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: object) -> None:
        self.bound = address

    def listen(self, backlog: int) -> None:
        self.backlog = backlog

    def close(self) -> None:
        pass


def _ipv6_loopback_available() -> bool:
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(("::1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class BindSocketTests(unittest.TestCase):
    """What ``_bind_socket`` decides before it binds, and what it binds.

    Three separate wrongs lived here, none of them visible to a green Linux
    suite:

    * the family was pinned to ``AF_INET``, so ``VCT_MODEL_GATEWAY_HOST=::1``
      — a loopback address the config layer ACCEPTS — could not bind at all;
    * then only ``getaddrinfo``'s FIRST result was bound, which is a guess
      whenever the host is a name: ``localhost`` may resolve ``::1`` first
      while every consumer of the port file dials ``127.0.0.1``;
    * and ``SO_REUSEADDR`` was set on Linux only, which makes a macOS restart
      inside the ~30 s ``TIME_WAIT`` window fail — silently drifting a
      resolved port into the fallback range, and turning a launcher-pinned
      ``--port`` into an exit 1 for half a minute after every stop.
    """

    def _bind_fake(self, os_name: str, platform: str) -> "list[_FakeSocket]":
        """Bind through a FAKE socket so the OPTION CALLS are observable.

        A real socket cannot answer "which flag would this set on macOS?"
        from a Linux box, and that question is the whole platform decision.
        BOTH ``os.name`` and ``sys.platform`` are patched so an
        implementation that keys on either is measured honestly — the
        pre-R3 one keyed on ``sys.platform`` and would otherwise pass here
        for the wrong reason, on a Linux host, for every OS.

        The liveness probe is stubbed out because it would otherwise talk to
        the fake and read a no-op ``connect`` as a live listener.
        """
        made: list[_FakeSocket] = []

        def factory(family: int, socktype: int, proto: int = 0) -> _FakeSocket:
            sock = _FakeSocket(family, socktype, proto)
            made.append(sock)
            return sock

        with mock.patch.object(entry.socket, "socket", factory):
            with mock.patch.object(entry.os, "name", os_name):
                with mock.patch.object(entry.sys, "platform", platform):
                    with mock.patch.object(
                        entry, "_listener_accepts", return_value=False,
                    ):
                        entry._bind_socket("127.0.0.1", 11555)
        return made

    def test_so_reuseaddr_is_set_on_every_posix(self) -> None:
        """ACT half, and the R3 correction: macOS needs it as much as Linux.

        It is what lets a restart reclaim a port whose previous CONNECTIONS
        are in TIME_WAIT. Leaving it off there did not buy a stricter
        liveness check — that job belongs to the connect probe — it only
        broke restarts: a resolved port drifted into the fallback range and a
        launcher-pinned ``--port`` made ``serve`` exit 1 for ~30 s.
        """
        for os_name, platform in (
            ("posix", "linux"), ("posix", "darwin"), ("posix", "freebsd13"),
        ):
            with self.subTest(os_name=os_name, platform=platform):
                sock = self._bind_fake(os_name, platform)[0]
                self.assertIn(
                    (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), sock.options,
                )

    def test_windows_sets_no_socket_option_at_all(self) -> None:
        """LEAVE-ALONE half, and both flags are wrong there for one reason.

        ``SO_REUSEADDR`` on Windows means STEAL a port another process is
        listening on. ``SO_EXCLUSIVEADDRUSE`` looks like the guard against
        that and re-introduces the TIME_WAIT refusal instead: Microsoft
        documents that such a socket "cannot necessarily be reused
        immediately after socket closure". The launcher's probe sets no
        option, so it would call the port free, pin it with ``--port``, and
        the exclusive bind would answer WSAEADDRINUSE — exit 1 for the whole
        wait window. A default bind is what a restart needs.
        """
        sock = self._bind_fake("nt", "win32")[0]
        self.assertEqual(
            sock.options, [],
            "no socket option belongs on the Windows bind: REUSEADDR steals, "
            "EXCLUSIVEADDRUSE blocks the restart the POSIX flag exists for",
        )

    def test_the_backlog_and_address_still_reach_the_socket(self) -> None:
        """Positive control: the fake is not silently no-oping the bind."""
        sock = self._bind_fake("posix", "linux")[0]
        self.assertEqual(sock.bound, ("127.0.0.1", 11555))
        self.assertEqual(sock.backlog, 128)
        self.assertEqual(sock.family, socket.AF_INET)

    # ── the two halves of "is this port free?" ───────────────────────────
    def test_a_specific_live_listener_makes_the_port_taken(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        port = held.getsockname()[1]
        with self.assertRaises(OSError) as caught:
            entry._bind_socket("127.0.0.1", port)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_a_WILDCARD_live_listener_makes_the_port_taken(self) -> None:
        """The case a bind cannot answer once SO_REUSEADDR is set: on
        macOS/BSD binding 127.0.0.1:P SUCCEEDS while 0.0.0.0:P listens."""
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(held.close)
        held.bind(("0.0.0.0", 0))
        held.listen(8)
        port = held.getsockname()[1]
        self.assertTrue(
            entry._listener_accepts(socket.AF_INET, ("127.0.0.1", port)),
            "a wildcard listener accepts a loopback connection",
        )
        with self.assertRaises(OSError) as caught:
            entry._bind_socket("127.0.0.1", port)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertIn(
            "already accepts connections", str(caught.exception),
            "the CONNECT probe must be what refuses: Linux's bind would "
            "refuse this too, macOS's (with SO_REUSEADDR) would not",
        )

    def test_a_port_in_time_wait_is_still_free(self) -> None:
        """THE macOS regression, reproduced on any POSIX.

        A server that closes an accepted connection FIRST leaves that
        connection — and therefore its local port — in TIME_WAIT for tens of
        seconds. Without ``SO_REUSEADDR`` the next bind fails, which is
        exactly what "restart the gateway" does.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        served, _peer = listener.accept()
        served.close()   # ACTIVE close: this side goes to TIME_WAIT
        client.close()
        listener.close()

        socks = entry._bind_socket("127.0.0.1", port)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual(socks[0].getsockname()[1], port)

    # ── every address, not the first ─────────────────────────────────────
    def test_an_ipv4_loopback_host_binds_for_real(self) -> None:
        socks = entry._bind_socket("127.0.0.1", 0)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual([s.family for s in socks], [socket.AF_INET])
        self.assertEqual(socks[0].getsockname()[0], "127.0.0.1")

    def test_an_ipv6_loopback_host_binds_for_real(self) -> None:
        """``VCT_MODEL_GATEWAY_HOST=::1`` is a loopback address the config
        layer accepts; pinning AF_INET made it unbindable."""
        if not _ipv6_loopback_available():
            self.skipTest("no IPv6 loopback on this machine")
        socks = entry._bind_socket("::1", 0)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual([s.family for s in socks], [socket.AF_INET6])
        self.assertEqual(socks[0].getsockname()[0], "::1")

    def test_a_name_that_resolves_to_both_families_binds_both(self) -> None:
        """``localhost`` is a NAME. Binding only its first address serves an
        address half the machine's clients do not dial."""
        port = _free_port()
        families = {
            info[0] for info in socket.getaddrinfo(
                "localhost", port, type=socket.SOCK_STREAM,
            )
        }
        if len(families) < 2:
            self.skipTest("localhost resolves to one family on this machine")
        socks = entry._bind_socket("localhost", port)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual({s.family for s in socks}, families)
        for sock in socks:
            self.assertEqual(sock.getsockname()[1], port)

    def test_one_taken_address_makes_the_whole_port_taken(self) -> None:
        """All-or-nothing: a gateway reachable on half its addresses is
        worse than one that moved to the next port."""
        port = _free_port()
        infos = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
        families = {info[0] for info in infos}
        if len(families) < 2:
            self.skipTest("localhost resolves to one family on this machine")
        family, socktype, proto, _canon, sockaddr = infos[-1]
        held = socket.socket(family, socktype, proto)
        self.addCleanup(held.close)
        held.bind(sockaddr)
        held.listen(8)
        with self.assertRaises(OSError) as caught:
            entry._bind_socket("localhost", port)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertFalse(
            entry._listener_accepts(infos[0][0], infos[0][4]),
            "the address that WAS free is left free, not half-bound",
        )

    def _dual_infos(self, port: int) -> list:
        """A ``getaddrinfo`` answer with IPv6 FIRST, as a name resolves.

        Machine-independent: ``localhost`` resolves to one family on many
        hosts (this one included), so the two tests above skip there and the
        property they exist for would go unchecked. The addresses are real —
        only the RESOLUTION is synthetic — so the binds below are real binds.
        """
        if not _ipv6_loopback_available():
            self.skipTest("no IPv6 loopback on this machine")
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    def test_both_families_are_bound_when_a_name_gives_both(self) -> None:
        """Binding ``infos[0]`` alone would serve ``::1`` while every reader
        of the port file dials ``127.0.0.1``."""
        port = _free_port()
        infos = self._dual_infos(port)
        with mock.patch.object(entry.socket, "getaddrinfo", return_value=infos):
            socks = entry._bind_socket("localhost", port)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual(
            [s.family for s in socks], [socket.AF_INET6, socket.AF_INET],
        )
        self.assertTrue(
            entry._listener_accepts(socket.AF_INET, ("127.0.0.1", port)),
            "the IPv4 address a client actually dials must be listening",
        )

    def test_one_taken_family_closes_the_other_and_frees_the_port(self) -> None:
        """All-or-nothing, without depending on how this host spells
        ``localhost``. Half a gateway is worse than one that moved."""
        port = _free_port()
        infos = self._dual_infos(port)
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", port))
        held.listen(8)

        with mock.patch.object(entry.socket, "getaddrinfo", return_value=infos):
            with self.assertRaises(OSError) as caught:
                entry._bind_socket("localhost", port)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertFalse(
            entry._listener_accepts(socket.AF_INET6, ("::1", port, 0, 0)),
            "the address that DID bind must be closed again, not left half-up",
        )

    def test_a_host_that_resolves_to_nothing_raises_rather_than_binds(
        self,
    ) -> None:
        with mock.patch.object(entry.socket, "getaddrinfo", return_value=[]):
            with self.assertRaises(OSError) as caught:
                entry._bind_socket("127.0.0.1", 11555)
        self.assertEqual(caught.exception.errno, errno.EADDRNOTAVAIL)

    def test_duplicate_resolutions_bind_once(self) -> None:
        """``getaddrinfo`` repeats an address across protocols; binding it
        twice would fail against ourselves and report the port as taken."""
        port = _free_port()
        one = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        with mock.patch.object(
            entry.socket, "getaddrinfo", return_value=[one, one],
        ):
            socks = entry._bind_socket("127.0.0.1", port)
        for sock in socks:
            self.addCleanup(sock.close)
        self.assertEqual(len(socks), 1)


class BindWritesBothFilesTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_env("VCT_MODEL_GATEWAY_HOST", "127.0.0.1")
        import logging

        logger = logging.getLogger("model_router")
        before = list(logger.handlers)
        self.addCleanup(lambda: logger.handlers.__setitem__(slice(None), before))

    def test_the_daemon_records_the_port_it_actually_bound(self) -> None:
        """The whole point: a fallback that nobody can find is no fallback.

        The resolved port is held by something else, so the daemon lands
        somewhere in the range — and BOTH files must name where it landed, not
        where it wanted to go.
        """
        block = _free_block(3)
        patch = mock.patch.object(gateway_config, "FALLBACK_PORT_RANGE", block)
        patch.start()
        self.addCleanup(patch.stop)

        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", block[0]))
        held.listen(8)
        self.addCleanup(held.close)
        gateway_config.port_path().write_text(f"{block[0]}\n", encoding="utf-8")

        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 0)

        bound = gateway_config.port_path().read_text(encoding="utf-8").strip()
        self.assertNotEqual(int(bound), block[0])
        self.assertIn(int(bound), block)
        self.assertEqual(
            gateway_config.last_port_path().read_text(encoding="utf-8").strip(),
            bound,
            "the record and the port file must agree",
        )

    def test_the_record_survives_the_daemon(self) -> None:
        """On a DISCOVERED port, like its sibling above.

        Run against the shipped default this asserted on machine state: the
        default port is a real one, and on the machine this suite was written
        for a legacy container owns it — so the daemon fell back, or failed,
        depending on who else was up. The record's lifetime has nothing to do
        with which port it names.
        """
        block = _free_block(3)
        patch = mock.patch.object(gateway_config, "FALLBACK_PORT_RANGE", block)
        patch.start()
        self.addCleanup(patch.stop)
        gateway_config.port_path().write_text(f"{block[0]}\n", encoding="utf-8")

        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 0)
        self.assertTrue(gateway_config.last_port_path().is_file())
        self.assertEqual(
            gateway_config.last_port_path().read_text(encoding="utf-8").strip(),
            str(block[0]),
        )
        self.assertFalse(gateway_config.pid_path().exists())

    def test_a_pinned_busy_port_fails_loudly_and_writes_nothing(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        self.addCleanup(held.close)
        self.set_env("VCT_MODEL_GATEWAY_PORT", str(held.getsockname()[1]))

        with mock.patch.object(web, "run_app", return_value=None):
            self.assertEqual(entry._serve(None), 1)
        self.assertFalse(gateway_config.port_path().exists())
        self.assertFalse(gateway_config.last_port_path().exists())
        self.assertFalse(
            gateway_config.pid_path().exists(),
            "a failed start must not leave a pid file blocking the next one",
        )


class ReservedPortTests(unittest.TestCase):
    """The fallback range must not overlap a port this codebase already owns.

    A fallback that lands on a reserved port either loses the race to that
    service (the gateway moves on, harmless) or WINS it and takes the port
    away before the service starts — which reproduces, one layer along,
    exactly the collision the fallback exists to escape. The two ports below
    are the ones VCO itself hands out, read from their own homes where that
    is importable without dragging in a service dependency.
    """

    #: Ports VCO hands to its OWN services, with the file each is declared
    #: in. Only the code-embed one is importable here — the others live in
    #: a module that drags in the Weaviate client, or in Rust — so they are
    #: named with their home, and the Rust two are additionally read out of
    #: that source below so a move there fails HERE rather than in the field.
    RESERVED = {
        11439: "RL_SERVER_URL — claude_mcp_servers/weaviate_mcp/server.py",
        11442: "ORCHESTRATOR_ROOT_RL_PORT — vct-hub/src/module_supervisor.rs",
        11443: "GLOBAL_RL_PORT — vct-launcher-core/services/container_runtime.rs",
    }

    def _reserved_ports(self) -> dict[int, str]:
        from vco_lib.code_embed_image import DEFAULT_PORT as CODE_EMBED_PORT

        ports = dict(self.RESERVED)
        ports[CODE_EMBED_PORT] = "vco_lib.code_embed_image.DEFAULT_PORT"
        return ports

    def test_the_range_avoids_the_ports_vco_already_reserves(self) -> None:
        reserved = self._reserved_ports()
        overlap = sorted(set(reserved) & set(gateway_config.FALLBACK_PORT_RANGE))
        self.assertEqual(
            overlap, [],
            "the fallback range would bind "
            + ", ".join(f"{p} ({reserved[p]})" for p in overlap),
        )

    def test_the_rust_rl_port_constants_are_where_this_test_thinks(self) -> None:
        """Positive control: the two Rust ports above are read, not assumed.

        A constant that moved would otherwise leave this suite asserting that
        the range avoids a port nobody uses any more, while the range quietly
        overlapped the port it moved TO.
        """
        sources = {
            11442: (
                REPO / "launcher" / "src-tauri" / "vct-hub" / "src"
                / "module_supervisor.rs",
                "ORCHESTRATOR_ROOT_RL_PORT",
            ),
            11443: (
                REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src"
                / "services" / "container_runtime.rs",
                "GLOBAL_RL_PORT",
            ),
        }
        for port, (path, name) in sources.items():
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), f"{path} is missing")
                match = re.search(
                    rf"pub const {name}:\s*u16\s*=\s*(\d+)",
                    path.read_text(encoding="utf-8"),
                )
                assert match is not None, f"{name} not found in {path}"
                self.assertEqual(int(match.group(1)), port)

    def test_the_default_port_is_not_inside_its_own_fallback_range(self) -> None:
        """Otherwise one candidate is the port that just failed to bind."""
        self.assertNotIn(
            gateway_config.DEFAULT_PORT, gateway_config.FALLBACK_PORT_RANGE,
        )


class RustParityTests(unittest.TestCase):
    """The literals both implementations of the chain depend on.

    Positive control first: this file must exist and must still declare the
    basename this suite has always pinned, so a rename or a moved file fails
    here rather than making every later assertion vacuous.

    The two new constants are asserted WHEN DECLARED. They are authored in the
    sibling launcher lane and arrive in this tree at merge; the moment they do,
    these become hard equality checks with no edit here. Until then the
    control above is what keeps the test honest — it cannot pass on a missing
    or wrong file.
    """

    def setUp(self) -> None:
        self.assertTrue(RUST.is_file(), f"{RUST} is missing")
        self.src = RUST.read_text(encoding="utf-8")

    def test_the_port_file_basename_still_matches(self) -> None:
        match = re.search(r'const PORT_BASENAME:\s*&str\s*=\s*"([^"]+)"', self.src)
        assert match is not None, "PORT_BASENAME not found — parity scan is blind"
        self.assertEqual(match.group(1), gateway_config._PORT_BASENAME)

    def test_the_last_port_basename_matches_when_declared(self) -> None:
        match = re.search(
            r'const LAST_PORT_BASENAME:\s*&str\s*=\s*"([^"]+)"', self.src,
        )
        if match is None:
            self.skipTest(
                "LAST_PORT_BASENAME is not in the Rust source yet (it lands "
                "with the launcher lane); the Python side declares "
                f"{gateway_config.LAST_PORT_BASENAME!r} and this assertion "
                "becomes strict on merge",
            )
        self.assertEqual(match.group(1), gateway_config.LAST_PORT_BASENAME)

    def test_the_fallback_range_digits_match_when_declared(self) -> None:
        match = re.search(r"FALLBACK_PORT_RANGE[^=]*=\s*(\d+)\.\.=(\d+)", self.src)
        if match is None:
            self.skipTest(
                "FALLBACK_PORT_RANGE is not in the Rust source yet; the Python "
                f"side declares {gateway_config.FALLBACK_PORT_RANGE.start}-"
                f"{gateway_config.FALLBACK_PORT_RANGE.stop - 1} and this "
                "assertion becomes strict on merge",
            )
        start, end = int(match.group(1)), int(match.group(2))
        self.assertEqual(start, gateway_config.FALLBACK_PORT_RANGE.start)
        self.assertEqual(
            end, gateway_config.FALLBACK_PORT_RANGE.stop - 1,
            "Rust's range is INCLUSIVE; Python's range() stops one past it",
        )

    def test_the_default_port_still_matches(self) -> None:
        match = re.search(r"DEFAULT_GATEWAY_PORT:\s*u16\s*=\s*(\d+)", self.src)
        assert match is not None, "DEFAULT_GATEWAY_PORT not found"
        self.assertEqual(int(match.group(1)), gateway_config.DEFAULT_PORT)

    def test_the_service_name_matches_the_rust_side(self) -> None:
        """The string that tells THIS daemon from any other listener.

        The Python home is :data:`model_router.config.SERVICE_NAME` (the
        health handler writes it, the startup probe reads it). Rust compares
        against the same word when it reads ``/health``, so the two spellings
        are one contract.

        A HARD assert either way, never a skip: once the switch lane extracts
        the Rust literal into ``const GATEWAY_SERVICE`` this compares the
        constant; until then it compares the literal the Rust file already
        contains. Both forms fail if Python's name ever changes alone.
        """
        match = re.search(
            r'const GATEWAY_SERVICE:\s*&str\s*=\s*"([^"]+)"', self.src,
        )
        if match is not None:
            self.assertEqual(match.group(1), gateway_config.SERVICE_NAME)
            return
        self.assertIn(
            f'"{gateway_config.SERVICE_NAME}"', self.src,
            "the Rust side must name the same service string — as a literal "
            "now, as GATEWAY_SERVICE once the switch lane extracts it",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
