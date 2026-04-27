# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the durable install log + resume-from-log helpers in install.py.

Covers:
  * `_log_install_event` writes valid JSONL when state/logs/ exists, and
    silently no-ops when it doesn't (never raises).
  * `_load_resume_state` parses the log and returns {step: last_phase}
    only for the most-recent install.py session.
  * Stale sessions (>24h old) are ignored.
  * `_should_skip_step` honors --no-resume.
  * Cross-actor events (post-install-launcher.sh, launcher) do not pollute
    the install.py resume map.

These tests don't run the full installer — they isolate the helpers and
drive them directly. The point of resume is to make Step N's per-step
verification redundant when the log says "ok"; the verification still
runs so a stale log can't lose data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


def _iso_offset(seconds: int = 0) -> str:
    """Return an ISO-Z timestamp `seconds` after now (negative = past)."""
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_hours_ago(hours: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class _LogFixture:
    """Context manager: redirects PROJECT_ROOT.state.logs to a tempdir
    and resets it on exit. Tests can write a fixture install.jsonl there
    and call install._log_install_event / install._load_resume_state."""

    def __init__(self):
        self._tmp = None
        self._orig_root = None
        self.path = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "state" / "logs").mkdir(parents=True)
        self._orig_root = install.PROJECT_ROOT
        install.PROJECT_ROOT = root
        # Cross-test isolation: pending-events buffer is module-level, so
        # leftovers from another test leak into ours otherwise.
        self._orig_pending = install._PENDING_EVENTS[:]
        install._PENDING_EVENTS.clear()
        self.path = root / "state" / "logs" / "install.jsonl"
        return self

    def __exit__(self, *exc):
        install.PROJECT_ROOT = self._orig_root
        install._PENDING_EVENTS.clear()
        install._PENDING_EVENTS.extend(self._orig_pending)
        self._tmp.cleanup()
        return False

    def write_lines(self, *records: dict) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


class LogEventTests(unittest.TestCase):

    def test_log_event_writes_valid_jsonl(self):
        with _LogFixture() as fx:
            install._log_install_event("3/10", "ok", "venv created",
                                       data={"path": "/x/.venv"})
            content = fx.path.read_text(encoding="utf-8").strip()
            obj = json.loads(content)
            self.assertEqual(obj["actor"], "install.py")
            self.assertEqual(obj["step"], "3/10")
            self.assertEqual(obj["phase"], "ok")
            self.assertEqual(obj["detail"], "venv created")
            self.assertEqual(obj["data"], {"path": "/x/.venv"})
            self.assertIn("ts", obj)

    def test_log_event_silent_when_dir_missing(self):
        # PROJECT_ROOT pointed at a tempdir without state/logs/. Helper
        # must NOT raise and must NOT create the directory implicitly.
        # The event WILL be buffered in `_PENDING_EVENTS` for later drain
        # — we test that path separately in test_log_event_buffers_pre_step_8.
        with tempfile.TemporaryDirectory() as tmp:
            orig = install.PROJECT_ROOT
            orig_pending = install._PENDING_EVENTS[:]
            install.PROJECT_ROOT = Path(tmp)
            install._PENDING_EVENTS.clear()
            try:
                # Should not raise.
                install._log_install_event("5/10", "ok", "test")
                # Verify nothing was created.
                self.assertFalse((Path(tmp) / "state").exists())
                # Event was buffered (not silently dropped).
                self.assertEqual(len(install._PENDING_EVENTS), 1)
            finally:
                install.PROJECT_ROOT = orig
                install._PENDING_EVENTS.clear()
                install._PENDING_EVENTS.extend(orig_pending)

    def test_log_event_buffers_pre_step_8(self):
        # PROJECT_ROOT exists but state/logs/ does NOT yet (mid-flight,
        # before Step 8). The event must land in _PENDING_EVENTS, not
        # crash, and must NOT be silently dropped: once the dir is
        # created and a real event flows through, the buffer drains in
        # chronological order.
        with tempfile.TemporaryDirectory() as tmp:
            orig = install.PROJECT_ROOT
            install.PROJECT_ROOT = Path(tmp)
            install._PENDING_EVENTS.clear()
            try:
                install._log_install_event("1/10", "ok", "py-version")
                install._log_install_event("2/10", "ok", "system")
                # state/logs/ doesn't exist → events buffered.
                self.assertFalse((Path(tmp) / "state" / "logs"
                                  / "install.jsonl").exists())
                self.assertEqual(len(install._PENDING_EVENTS), 2)

                # Now simulate Step 8 creating the dir + draining.
                (Path(tmp) / "state" / "logs").mkdir(parents=True)
                # _log_install_event drains automatically when path
                # becomes available.
                install._log_install_event("8/10", "ok", "state-dir")

                lines = (Path(tmp) / "state" / "logs"
                         / "install.jsonl").read_text(encoding="utf-8")
                events = [json.loads(line) for line in lines.splitlines() if line]
                # All three events present, in order.
                self.assertEqual(len(events), 3)
                self.assertEqual([e["step"] for e in events],
                                 ["1/10", "2/10", "8/10"])
                self.assertEqual(install._PENDING_EVENTS, [])
            finally:
                install.PROJECT_ROOT = orig
                install._PENDING_EVENTS.clear()

    def test_log_event_handles_unserializable_data(self):
        # `data` field is best-effort — if it can't be JSON-serialized,
        # the helper drops it rather than failing the event.
        with _LogFixture() as fx:
            class _NotJsonable:
                pass
            install._log_install_event("4/10", "ok", "deps",
                                       data={"obj": _NotJsonable()})
            obj = json.loads(fx.path.read_text(encoding="utf-8").strip())
            # Event still landed; data field is just absent.
            self.assertEqual(obj["step"], "4/10")
            self.assertNotIn("data", obj)


class LoadResumeStateTests(unittest.TestCase):

    def test_no_log_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = install.PROJECT_ROOT
            install.PROJECT_ROOT = Path(tmp)
            try:
                self.assertEqual(install._load_resume_state(), {})
            finally:
                install.PROJECT_ROOT = orig

    def test_parses_completed_steps_from_session(self):
        with _LogFixture() as fx:
            now = _iso_offset(0)
            fx.write_lines(
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "ok", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "2/10",
                 "phase": "ok", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "3/10",
                 "phase": "ok", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "4/10",
                 "phase": "ok", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "5/10",
                 "phase": "error", "detail": "compose down"},
            )
            state = install._load_resume_state()
            self.assertEqual(state.get("1/10"), "ok")
            self.assertEqual(state.get("2/10"), "ok")
            self.assertEqual(state.get("3/10"), "ok")
            self.assertEqual(state.get("4/10"), "ok")
            self.assertEqual(state.get("5/10"), "error")

    def test_only_latest_session_counts(self):
        # Two install.py sessions back-to-back. Only the second's events
        # should appear — the first's are stale even within 24h.
        with _LogFixture() as fx:
            old = _iso_hours_ago(1)
            now = _iso_offset(0)
            fx.write_lines(
                # OLD session: 1-3 ok, 4 error
                {"ts": old, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": ""},
                {"ts": old, "actor": "install.py", "step": "1/10",
                 "phase": "ok", "detail": ""},
                {"ts": old, "actor": "install.py", "step": "2/10",
                 "phase": "ok", "detail": ""},
                {"ts": old, "actor": "install.py", "step": "4/10",
                 "phase": "error", "detail": "old failure"},
                # NEW session: re-runs everything fresh, only 1/10 ok.
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": "fresh"},
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "ok", "detail": ""},
            )
            state = install._load_resume_state()
            # Only the new session's ok counts.
            self.assertEqual(state.get("1/10"), "ok")
            # Steps 2-4 from the OLD session must NOT carry over.
            self.assertNotIn("2/10", state)
            self.assertNotIn("4/10", state)

    def test_stale_session_24h_returns_empty(self):
        # Single session, started 25h ago. Treated as stale.
        with _LogFixture() as fx:
            stale = _iso_hours_ago(25)
            fx.write_lines(
                {"ts": stale, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": ""},
                {"ts": stale, "actor": "install.py", "step": "1/10",
                 "phase": "ok", "detail": ""},
                {"ts": stale, "actor": "install.py", "step": "2/10",
                 "phase": "ok", "detail": ""},
            )
            state = install._load_resume_state()
            self.assertEqual(state, {})

    def test_other_actors_ignored(self):
        # post-install-launcher.sh + launcher events do NOT contribute to
        # install.py's resume map. install.py only resumes install.py.
        with _LogFixture() as fx:
            now = _iso_offset(0)
            fx.write_lines(
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": ""},
                {"ts": now, "actor": "install.py", "step": "1/10",
                 "phase": "ok", "detail": ""},
                {"ts": now, "actor": "post-install-launcher.sh",
                 "step": "build/tauri", "phase": "ok", "detail": ""},
                {"ts": now, "actor": "launcher",
                 "step": "first-spawn", "phase": "ok", "detail": ""},
            )
            state = install._load_resume_state()
            self.assertNotIn("build/tauri", state)
            self.assertNotIn("first-spawn", state)
            self.assertEqual(state.get("1/10"), "ok")

    def test_corrupt_lines_dont_break_parsing(self):
        with _LogFixture() as fx:
            now = _iso_offset(0)
            with fx.path.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": now, "actor": "install.py", "step": "1/10",
                    "phase": "start", "detail": "",
                }) + "\n")
                f.write("{not valid json\n")
                f.write("totally garbage\n")
                f.write(json.dumps({
                    "ts": now, "actor": "install.py", "step": "1/10",
                    "phase": "ok", "detail": "",
                }) + "\n")
            state = install._load_resume_state()
            self.assertEqual(state.get("1/10"), "ok")


class ShouldSkipStepTests(unittest.TestCase):

    def test_no_resume_returns_false_even_with_ok(self):
        orig_enabled = install._RESUME_ENABLED
        orig_state = install._RESUME_STATE
        install._RESUME_ENABLED = False
        install._RESUME_STATE = {"3/10": "ok", "4/10": "ok"}
        try:
            self.assertFalse(install._should_skip_step("3/10"))
            self.assertFalse(install._should_skip_step("4/10"))
        finally:
            install._RESUME_ENABLED = orig_enabled
            install._RESUME_STATE = orig_state

    def test_skip_only_for_ok_or_skip(self):
        orig_enabled = install._RESUME_ENABLED
        orig_state = install._RESUME_STATE
        install._RESUME_ENABLED = True
        install._RESUME_STATE = {
            "3/10": "ok",
            "4/10": "skip",
            "5/10": "error",
            "6/10": "warn",
            "7/10": "start",
        }
        try:
            self.assertTrue(install._should_skip_step("3/10"))
            self.assertTrue(install._should_skip_step("4/10"))
            self.assertFalse(install._should_skip_step("5/10"))
            self.assertFalse(install._should_skip_step("6/10"))  # warn is ambiguous
            self.assertFalse(install._should_skip_step("7/10"))  # interrupted
            self.assertFalse(install._should_skip_step("8/10"))  # not in state
        finally:
            install._RESUME_ENABLED = orig_enabled
            install._RESUME_STATE = orig_state


if __name__ == "__main__":
    unittest.main()
