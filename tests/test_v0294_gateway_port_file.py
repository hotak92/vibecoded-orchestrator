# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The port file survives the daemon; the pid file does not.

Two files, two questions, and v0.2.93 answered them with one lifetime:

* ``model-gateway.pid`` answers "is it running?" — a stale one names a
  process that is not us and blocks the next start, so it MUST go on exit;
* ``model-gateway.port`` answers "where did it last run?" — and every reader
  already probes ``/health`` before trusting it.

Deleting the port file on a clean exit made a stopped gateway
indistinguishable from one that had never run. After a SIGTERM (boot-service
stop, reboot) a gateway that had fallen back to a non-default port was
forgotten, every resolver dropped to the compiled-in default, and on a machine
where something ELSE listens there — this one has a legacy container on the
historical port — the client would have talked to the wrong service with no
error anywhere. A stale port file that fails a health probe is a recoverable
miss; a missing one is a confident wrong answer.
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from model_router import __main__ as entry
from model_router import config as gateway_config

from tests.common.ports import free_block as _free_block


class PortFileLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-portfile-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # DISCOVERED, not pinned. These used to be the literals 11599/11600,
        # which asserted that nothing on the machine runs there and that no
        # second copy of this suite is running at the same time — neither is
        # a property of the code under test, and both fail as flakes that
        # look like a broken port file.
        self.ports = _free_block(2)

        env = mock.patch.dict(
            os.environ,
            {
                "VCT_STATE_DIR": str(self.root),
                "VCT_MODEL_GATEWAY_PORT": str(self.ports[0]),
            },
        )
        env.start()
        self.addCleanup(env.stop)

        # _configure_logging attaches a file handler under the temp root;
        # putting it back afterwards keeps the suite's logging clean.
        logger = logging.getLogger("model_router")
        before = list(logger.handlers)
        self.addCleanup(lambda: logger.handlers.__setitem__(slice(None), before))

    def _serve_once(self) -> int:
        """Run ``_serve`` with the event loop stubbed out — start, then exit."""
        with mock.patch.object(web, "run_app", return_value=None):
            return entry._serve(None)

    def test_a_clean_exit_keeps_the_port_and_drops_the_pid(self) -> None:
        self.assertEqual(self._serve_once(), 0)
        port_file = gateway_config.port_path()
        self.assertTrue(port_file.is_file(), "the port file must survive")
        self.assertEqual(
            port_file.read_text(encoding="utf-8").strip(), str(self.ports[0]),
        )
        self.assertFalse(
            gateway_config.pid_path().exists(),
            "the pid file is liveness and must not outlive the process",
        )

    def test_a_stale_port_file_does_not_block_the_next_start(self) -> None:
        """It is data, not a lock: the next run overwrites it."""
        self.assertEqual(self._serve_once(), 0)
        first = gateway_config.port_path().read_text(encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"VCT_MODEL_GATEWAY_PORT": str(self.ports[1])},
        ):
            self.assertEqual(self._serve_once(), 0)
        second = gateway_config.port_path().read_text(encoding="utf-8")
        self.assertEqual(first.strip(), str(self.ports[0]))
        self.assertEqual(second.strip(), str(self.ports[1]))

    def test_the_port_file_survives_a_crash_too(self) -> None:
        """``run_app`` raising is the SIGTERM-shaped path: same outcome."""
        with mock.patch.object(web, "run_app", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                entry._serve(None)
        self.assertTrue(gateway_config.port_path().is_file())
        self.assertFalse(gateway_config.pid_path().exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
