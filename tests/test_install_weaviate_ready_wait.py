# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.89 FIX 1: bounded Weaviate-readiness wait.

Windows CPU-only field report (v0.2.72→v0.2.88): the PC slept
mid-re-embed; on wake the WSL2 port-forward died so Weaviate :8081 returned
HTTP 000 from Windows. install.py then hung INDEFINITELY — the unbounded
re-embed subprocess blocked on a Weaviate that never answered. The fix gates
the re-embed on ``vco_lib.install_weaviate.wait_for_weaviate_ready`` (install.py
keeps a thin ``_wait_for_weaviate_ready`` wrapper), which polls with a BOUNDED
deadline and returns False (soft-fail) instead of blocking forever.

Invariants:
  * When the readiness probe NEVER becomes ready, the wait returns False within
    the deadline and does NOT hang (bounded by wall-clock — verified with a
    tiny deadline so the test is fast).
  * When the probe returns 200 promptly, the wait returns True quickly (no
    regression to the happy path).
  * The install.py thin wrapper delegates to the vco_lib helper.
"""
from __future__ import annotations

import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib import install_weaviate  # noqa: E402


class WaitForWeaviateReadyTest(unittest.TestCase):
    """Directly exercises the extracted vco_lib bounded-poll loop.

    The loop does a function-local ``import time`` / ``import urllib.request``,
    which bind the real module objects — so patching ``urllib.request.urlopen``
    and ``time.sleep`` at the module level reaches them.
    """

    def test_soft_fails_within_deadline_when_never_ready(self) -> None:
        """Probe always fails (connection refused / HTTP 000) → returns False
        within the deadline; must NOT hang."""
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ), mock.patch("time.sleep", return_value=None):
            start = time.monotonic()
            result = install_weaviate.wait_for_weaviate_ready(
                "http://localhost:8081", 0.3
            )
            elapsed = time.monotonic() - start

        self.assertFalse(result, "unready probe must soft-fail to False")
        # Bounded: even with sleep() stubbed, the monotonic-deadline loop exits
        # once the (real) wall clock passes the 0.3s deadline. Generous ceiling
        # to avoid flakiness on slow CI, but nowhere near "hangs forever".
        self.assertLess(elapsed, 10.0, "wait must be bounded, not hang")

    def test_succeeds_promptly_when_ready(self) -> None:
        """Probe returns HTTP 200 immediately → returns True quickly."""
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.getcode.return_value = 200

        with mock.patch.object(
            urllib.request, "urlopen", return_value=fake_resp
        ) as m_urlopen, mock.patch("time.sleep", return_value=None) as m_sleep:
            result = install_weaviate.wait_for_weaviate_ready(
                "http://localhost:8081", 30.0
            )

        self.assertTrue(result, "ready probe must return True")
        # Happy path: exactly one probe, no sleeps (returned on first success).
        self.assertEqual(m_urlopen.call_count, 1)
        self.assertEqual(m_sleep.call_count, 0)

    def test_becomes_ready_after_a_few_failures(self) -> None:
        """First two probes fail, third returns 200 → returns True without
        hanging; does not give up early."""
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.getcode.return_value = 200
        side_effects = [
            urllib.error.URLError("not up yet"),
            OSError("still refused"),
            fake_resp,
        ]
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=side_effects
        ), mock.patch("time.sleep", return_value=None):
            result = install_weaviate.wait_for_weaviate_ready(
                "http://localhost:8081", 30.0
            )
        self.assertTrue(result)


class InstallWrapperTest(unittest.TestCase):
    """The install.py thin wrapper resolves env defaults + delegates."""

    def test_wrapper_delegates_and_soft_fails(self) -> None:
        with mock.patch.object(
            install._install_weaviate,
            "wait_for_weaviate_ready",
            return_value=False,
        ) as m_delegate:
            result = install._wait_for_weaviate_ready(
                weaviate_url="http://localhost:8081", deadline_seconds=1.0
            )
        self.assertFalse(result)
        m_delegate.assert_called_once()
        # First positional arg is the URL; second is the deadline.
        args, kwargs = m_delegate.call_args
        self.assertEqual(args[0], "http://localhost:8081")
        self.assertEqual(args[1], 1.0)

    def test_wrapper_env_override_deadline(self) -> None:
        """WEAVIATE_READY_TIMEOUT env overrides the default deadline in the
        wrapper's resolution (verified via the delegated deadline arg)."""
        with mock.patch.dict(
            install.os.environ, {"WEAVIATE_READY_TIMEOUT": "7"}, clear=False
        ), mock.patch.object(
            install._install_weaviate,
            "wait_for_weaviate_ready",
            return_value=True,
        ) as m_delegate:
            result = install._wait_for_weaviate_ready(
                weaviate_url="http://localhost:8081"
            )
        self.assertTrue(result)
        _args, _kwargs = m_delegate.call_args
        self.assertEqual(_args[1], 7.0)


class SlowEmbedSeparationInvariantTest(unittest.TestCase):
    """v0.2.89 review test-gap: pin the v0.2.69 FIX 3 separation invariant.

    The bounded readiness gate exists PRECISELY so the re-embed subprocess can
    stay UNTIMED (a legitimate slow CPU re-embed must never be killed by a
    wall-clock kwarg — the regression v0.2.69 removed). A future edit that
    quietly re-adds ``timeout=`` to the seed's ``subprocess.run`` would
    reintroduce it; this pin goes red instead.
    """

    def test_seed_subprocess_has_no_timeout_kwarg(self) -> None:
        import inspect

        src = inspect.getsource(install._seed_weaviate_impl)
        # Guard against the pin going vacuous if the subprocess call is
        # refactored away entirely.
        self.assertIn(
            "subprocess.run(", src,
            "_seed_weaviate_impl no longer calls subprocess.run directly — "
            "relocate this pin to wherever the re-embed subprocess moved",
        )
        self.assertNotIn(
            "timeout=", src,
            "the re-embed subprocess must stay UNTIMED (v0.2.69 FIX 3): the "
            "bounded pre-spawn readiness gate handles the dead-endpoint case; "
            "a wall-clock timeout here would kill legitimate slow CPU re-embeds",
        )


class MalformedUrlFailFastTest(unittest.TestCase):
    """v0.2.89 review MINOR-6 leg: a malformed WEAVIATE_URL is deterministic —
    the wait must fail FAST instead of burning the whole deadline re-probing a
    URL that can never become ready."""

    def test_malformed_url_fails_fast(self) -> None:
        start = time.monotonic()
        result = install_weaviate.wait_for_weaviate_ready(
            "not-a-url", deadline_seconds=30.0
        )
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertLess(
            elapsed, 5.0,
            "a malformed URL (ValueError from the probe) must return "
            "immediately, not poll until the 30s deadline",
        )


if __name__ == "__main__":
    unittest.main()
