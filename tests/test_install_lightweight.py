# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the lightweight re-install path in install.py.

Covers Deliverable 3 from launch-blocker spec 2026-04-28:
  * `_rewrite_paths_in_file` replaces literal occurrences only.
  * `_lightweight_rewrite_paths` rewrites .env + .claude/settings.json.
  * `_venv_triage` returns the right action for each branch:
      - missing .venv → "create"
      - Python version mismatch → "recreate"
      - requirements.txt drift → "upgrade"
      - all match → "skip"
  * Lightweight install does NOT pull models / seed Weaviate (the
    runtime test only asserts the function exits ok and produces the
    expected log events).
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


class _ProjectRootFixture:
    """Swap install.PROJECT_ROOT to a tempdir + ensure state/logs/."""

    def __init__(self):
        self._tmp = None
        self._orig_root = None
        self.root: Path = Path()

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "state" / "logs").mkdir(parents=True)
        self._orig_root = install.PROJECT_ROOT
        install.PROJECT_ROOT = self.root
        install._PENDING_EVENTS.clear()
        return self

    def __exit__(self, *_):
        install.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()


class TestRewritePathsInFile(unittest.TestCase):
    def test_replaces_literal_occurrence(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.txt"
            p.write_text("path=/old/foo\nother=/old/foo/bar\n", encoding="utf-8")
            self.assertTrue(install._rewrite_paths_in_file(p, "/old/foo", "/new/bar"))
            text = p.read_text(encoding="utf-8")
            self.assertIn("/new/bar", text)
            self.assertNotIn("/old/foo", text)

    def test_no_change_when_pattern_absent(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.txt"
            p.write_text("nothing here\n", encoding="utf-8")
            self.assertFalse(install._rewrite_paths_in_file(p, "/missing", "/x"))

    def test_missing_file_returns_false(self):
        self.assertFalse(install._rewrite_paths_in_file(
            Path("/nonexistent/foo"), "/a", "/b"))

    def test_empty_old_str_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.txt"
            p.write_text("data\n", encoding="utf-8")
            self.assertFalse(install._rewrite_paths_in_file(p, "", "/b"))


class TestLightweightRewritePaths(unittest.TestCase):
    def test_rewrites_env_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text(
                "RL_PROJECT_ROOT=/old/install\nKG_BASE_DIR=/old/install/knowledge\n",
                encoding="utf-8",
            )
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text(
                '{"env": {"BASH_ENV": "/old/install/.claude/scripts/x.sh"}}\n',
                encoding="utf-8",
            )
            report = install._lightweight_rewrite_paths(root, "/old/install")
            self.assertTrue(report[".env"])
            self.assertTrue(report[".claude/settings.json"])
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertNotIn("/old/install", env_text)
            self.assertIn(str(root), env_text)

    def test_noop_when_old_eq_new(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text("KEY=val\n", encoding="utf-8")
            report = install._lightweight_rewrite_paths(root, str(root))
            self.assertFalse(any(report.values()))

    def test_noop_when_old_path_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text("KEY=val\n", encoding="utf-8")
            report = install._lightweight_rewrite_paths(root, "")
            self.assertFalse(any(report.values()))


class TestVenvTriage(unittest.TestCase):
    def test_missing_venv_says_create(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            triage = install._venv_triage(root)
            self.assertEqual(triage["action"], "create")
            self.assertIn("missing", triage["reason"])

    def test_recreate_when_python_version_mismatch(self):
        # Make a fake .venv whose python prints a wrong version.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".venv" / "bin").mkdir(parents=True)
            fake_py = root / ".venv" / "bin" / "python"
            # Fake python: prints a version that won't match runtime.
            fake_py.write_text(
                "#!/usr/bin/env bash\necho '2.7'\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            triage = install._venv_triage(root)
            self.assertEqual(triage["action"], "recreate")
            self.assertIn("Python version mismatch", triage["reason"])

    def test_upgrade_when_requirements_drift(self):
        # Match Python version → reach the drift check. No previous
        # state-hashes snapshot → drift returns all True → action upgrade.
        with _ProjectRootFixture() as fx:
            (fx.root / ".venv" / "bin").mkdir(parents=True)
            fake_py = fx.root / ".venv" / "bin" / "python"
            fake_py.write_text(
                f"#!/usr/bin/env bash\necho '{sys.version_info.major}."
                f"{sys.version_info.minor}'\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            (fx.root / "requirements.txt").write_text("foo==1.0\n",
                                                       encoding="utf-8")
            triage = install._venv_triage(fx.root)
            self.assertEqual(triage["action"], "upgrade")

    def test_skip_when_no_drift(self):
        # Snapshot first, no changes → skip.
        with _ProjectRootFixture() as fx:
            (fx.root / ".venv" / "bin").mkdir(parents=True)
            fake_py = fx.root / ".venv" / "bin" / "python"
            fake_py.write_text(
                f"#!/usr/bin/env bash\necho '{sys.version_info.major}."
                f"{sys.version_info.minor}'\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            (fx.root / "requirements.txt").write_text("foo==1.0\n",
                                                       encoding="utf-8")
            install._record_state_hashes(fx.root)
            triage = install._venv_triage(fx.root)
            self.assertEqual(triage["action"], "skip")


class TestRunLightweight(unittest.TestCase):
    def test_run_lightweight_emits_log_events(self):
        # End-to-end smoke. We exercise the function with a "create"
        # venv triage path replaced by a mock that records the call —
        # we don't actually want pytest to spawn a Python venv.
        with _ProjectRootFixture() as fx:
            # Fake an existing healthy .venv that matches Python.
            (fx.root / ".venv" / "bin").mkdir(parents=True)
            fake_py = fx.root / ".venv" / "bin" / "python"
            fake_py.write_text(
                f"#!/usr/bin/env bash\necho '{sys.version_info.major}."
                f"{sys.version_info.minor}'\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            # Create a state-hashes snapshot so triage returns "skip"
            # (no pip install runs).
            (fx.root / "requirements.txt").write_text("foo==1.0\n",
                                                       encoding="utf-8")
            install._record_state_hashes(fx.root)

            # Build a fake args namespace.
            import argparse
            args = argparse.Namespace(
                lightweight=True,
                lightweight_old_path=None,
                no_containers=True,
                dev=False,
            )
            rc = install._run_lightweight(args)
            self.assertEqual(rc, 0)

            # Lightweight events were logged.
            log = (fx.root / "state" / "logs" / "install.jsonl").read_text(
                encoding="utf-8")
            events = [json.loads(l) for l in log.splitlines() if l]
            steps = {e["step"] for e in events}
            self.assertIn("lightweight", steps)
            # Triage logged "skip" (means no pip install was triggered).
            triage_evts = [e for e in events
                           if e.get("step") == "lightweight"
                           and "venv triage" in e.get("detail", "")]
            self.assertTrue(any("skip" in e["detail"] for e in triage_evts),
                            f"expected 'skip' triage; got {triage_evts}")
            # Final state-hashes snapshot lands too (so future runs
            # have a fresh baseline).
            self.assertIn("state-hashes", steps)

    def test_run_lightweight_rewrites_paths(self):
        with _ProjectRootFixture() as fx:
            old_path = "/old/install"
            (fx.root / ".env").write_text(
                f"RL_PROJECT_ROOT={old_path}\n", encoding="utf-8")
            # Healthy venv.
            (fx.root / ".venv" / "bin").mkdir(parents=True)
            fake_py = fx.root / ".venv" / "bin" / "python"
            fake_py.write_text(
                f"#!/usr/bin/env bash\necho '{sys.version_info.major}."
                f"{sys.version_info.minor}'\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            (fx.root / "requirements.txt").write_text("foo==1.0\n",
                                                       encoding="utf-8")
            install._record_state_hashes(fx.root)

            import argparse
            args = argparse.Namespace(
                lightweight=True,
                lightweight_old_path=old_path,
                no_containers=True,
                dev=False,
            )
            rc = install._run_lightweight(args)
            self.assertEqual(rc, 0)
            env_text = (fx.root / ".env").read_text(encoding="utf-8")
            self.assertNotIn(old_path, env_text)
            self.assertIn(str(fx.root), env_text)


if __name__ == "__main__":
    unittest.main()
