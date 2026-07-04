# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the choices-replay + state-hashes drift helpers in install.py.

Covers Deliverable 2 from launch-blocker spec 2026-04-28:
  * `_record_install_choice` writes a properly-shaped event to install.jsonl.
  * `_load_previous_choices` round-trips the choice dict.
  * Stale-session rejection (>24h) per the same rule as `_load_resume_state`.
  * `_compute_state_hashes` MD5s tracked artifacts; missing files → None.
  * `_compute_drift` flags only changed slots; no baseline → all True.
  * `_record_state_hashes` lands a single state-hashes event per call.
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
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_hours_ago(hours: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class _LogFixture:
    """Redirect install.PROJECT_ROOT to a tempdir with state/logs/."""

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
        # Reset module-level pending events buffer.
        install._PENDING_EVENTS.clear()
        self.path = root / "state" / "logs" / "install.jsonl"
        return self

    def __exit__(self, *_):
        install.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def write_events(self, events: list[dict]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")


class TestRecordChoice(unittest.TestCase):
    def test_recorded_choice_appears_in_log_with_step_choices(self):
        with _LogFixture() as fx:
            install._record_install_choice(
                "container_runtime", False, {"reason": "user declined interactive prompt"},
            )
            text = fx.path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 0)
            obj = json.loads(text.splitlines()[-1])
            self.assertEqual(obj["step"], "choices")
            self.assertEqual(obj["phase"], "ok")
            self.assertEqual(obj["detail"], "container_runtime")
            self.assertEqual(obj["data"]["value"], False)
            self.assertEqual(obj["data"]["reason"],
                             "user declined interactive prompt")

    def test_record_choice_handles_complex_value(self):
        with _LogFixture() as fx:
            install._record_install_choice(
                "embedding_mode", "gpu",
                {"qwen3_text": True, "codesage_code": True},
            )
            obj = json.loads(fx.path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(obj["data"]["value"], "gpu")
            self.assertTrue(obj["data"]["qwen3_text"])


class TestLoadPreviousChoices(unittest.TestCase):
    def test_roundtrip_in_same_session(self):
        with _LogFixture() as fx:
            ts = _iso_offset()
            fx.write_events([
                {"ts": ts, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": "checking"},
                {"ts": ts, "actor": "install.py", "step": "choices",
                 "phase": "ok", "detail": "container_runtime",
                 "data": {"value": False, "reason": "user declined"}},
                {"ts": ts, "actor": "install.py", "step": "choices",
                 "phase": "ok", "detail": "embedding_mode",
                 "data": {"value": "gpu", "qwen3_text": True}},
            ])
            choices = install._load_previous_choices()
            self.assertIn("container_runtime", choices)
            self.assertEqual(choices["container_runtime"]["value"], False)
            self.assertEqual(choices["container_runtime"]["reason"], "user declined")
            self.assertIn("embedding_mode", choices)
            self.assertEqual(choices["embedding_mode"]["value"], "gpu")

    def test_stale_session_rejected(self):
        with _LogFixture() as fx:
            old_ts = _iso_hours_ago(48)
            fx.write_events([
                {"ts": old_ts, "actor": "install.py", "step": "1/10",
                 "phase": "start", "detail": "stale session"},
                {"ts": old_ts, "actor": "install.py", "step": "choices",
                 "phase": "ok", "detail": "container_runtime",
                 "data": {"value": True}},
            ])
            choices = install._load_previous_choices()
            self.assertEqual(choices, {},
                             "stale (>24h) session must be rejected")

    def test_only_latest_session_choices(self):
        with _LogFixture() as fx:
            old_ts = _iso_hours_ago(2)  # within 24h
            fresh_ts = _iso_offset()
            fx.write_events([
                # Older session
                {"ts": old_ts, "actor": "install.py", "step": "1/10",
                 "phase": "start"},
                {"ts": old_ts, "actor": "install.py", "step": "choices",
                 "phase": "ok", "detail": "container_runtime",
                 "data": {"value": True}},
                # Newer session — same choice flipped
                {"ts": fresh_ts, "actor": "install.py", "step": "1/10",
                 "phase": "start"},
                {"ts": fresh_ts, "actor": "install.py", "step": "choices",
                 "phase": "ok", "detail": "container_runtime",
                 "data": {"value": False}},
            ])
            choices = install._load_previous_choices()
            self.assertEqual(choices["container_runtime"]["value"], False,
                             "newer session must win")

    def test_no_log_returns_empty(self):
        with _LogFixture():
            self.assertEqual(install._load_previous_choices(), {})

    def test_only_install_py_choices_count(self):
        # Cross-actor events (launcher, post-install-launcher.sh) would
        # not normally write step="choices" — but if some hypothetical
        # future actor did, our parser must pull from the install.py
        # session boundary, so a "1/10 start" from install.py must
        # exist. Without it, no session is found.
        with _LogFixture() as fx:
            ts = _iso_offset()
            fx.write_events([
                {"ts": ts, "actor": "launcher", "step": "choices",
                 "phase": "ok", "detail": "container_runtime",
                 "data": {"value": True}},
            ])
            choices = install._load_previous_choices()
            self.assertEqual(choices, {},
                             "no install.py session marker → no replay")


class TestStateHashes(unittest.TestCase):
    def test_compute_state_hashes_includes_all_slots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Create one of the tracked files.
            (root / "requirements.txt").write_text("foo==1.0\n")
            hashes = install._compute_state_hashes(root)
            self.assertIn("requirements_txt_md5", hashes)
            self.assertIn("cargo_lock_md5", hashes)
            self.assertIn("package_json_md5", hashes)
            self.assertIn("knowledge_md5", hashes)
            self.assertIsNotNone(hashes["requirements_txt_md5"])
            # Missing files → None (not "")
            self.assertIsNone(hashes["cargo_lock_md5"])
            self.assertIsNone(hashes["package_json_md5"])
            self.assertIsNone(hashes["knowledge_md5"])

    def test_compute_state_hashes_changes_when_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("foo==1.0\n")
            h1 = install._compute_state_hashes(root)["requirements_txt_md5"]
            (root / "requirements.txt").write_text("foo==2.0\n")
            h2 = install._compute_state_hashes(root)["requirements_txt_md5"]
            self.assertNotEqual(h1, h2)

    def test_knowledge_md5_changes_on_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "knowledge").mkdir()
            (root / "knowledge" / "a.md").write_text("a")
            h1 = install._compute_state_hashes(root)["knowledge_md5"]
            (root / "knowledge" / "b.md").write_text("b")
            h2 = install._compute_state_hashes(root)["knowledge_md5"]
            self.assertNotEqual(h1, h2)


class TestComputeDrift(unittest.TestCase):
    def test_no_baseline_means_full_drift(self):
        with _LogFixture():
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "requirements.txt").write_text("foo==1.0\n")
                drift = install._compute_drift(root)
                # Every slot should be True (drifted) since there's no
                # previous snapshot to compare against.
                self.assertTrue(all(drift.values()))

    def test_drift_detection_after_snapshot(self):
        # Fixture and artifact root must be the same dir so
        # _record_state_hashes lands in the install.jsonl that
        # _compute_drift's loader (`_install_log_path`) reads from.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state" / "logs").mkdir(parents=True)
            (root / "requirements.txt").write_text("foo==1.0\n")
            orig_root = install.PROJECT_ROOT
            try:
                install.PROJECT_ROOT = root
                install._PENDING_EVENTS.clear()
                install._record_state_hashes(root)

                drift = install._compute_drift(root)
                self.assertFalse(drift["requirements_txt_md5"],
                                 "no change → no drift")
                # Modify a tracked file → drift True for that slot only.
                (root / "requirements.txt").write_text("foo==2.0\n")
                drift = install._compute_drift(root)
                self.assertTrue(drift["requirements_txt_md5"])
            finally:
                install.PROJECT_ROOT = orig_root

    def test_record_state_hashes_writes_one_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state" / "logs").mkdir(parents=True)
            (root / "requirements.txt").write_text("x\n")
            orig_root = install.PROJECT_ROOT
            try:
                install.PROJECT_ROOT = root
                install._PENDING_EVENTS.clear()
                install._record_state_hashes(root)
                log_path = root / "state" / "logs" / "install.jsonl"
                lines = log_path.read_text(encoding="utf-8").splitlines()
                state_events = [
                    json.loads(l) for l in lines
                    if l and json.loads(l).get("step") == "state-hashes"
                ]
                self.assertEqual(len(state_events), 1)
                self.assertEqual(state_events[0]["phase"], "ok")
                self.assertIn("requirements_txt_md5",
                              state_events[0].get("data") or {})
            finally:
                install.PROJECT_ROOT = orig_root


if __name__ == "__main__":
    unittest.main()
