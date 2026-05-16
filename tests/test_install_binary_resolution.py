# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for the v0.2.13 install.py fixes:

* Fix 1 (``_refresh_dist_binary_after_rebuild``): post-cargo-rebuild
  copy of ``target/release/vct-launcher-temp`` into
  ``launcher/dist/<os>-<arch>/vct-launcher``, with conservative gating
  to avoid blindly clobbering the dist artifact.

* Fix 5 (``_register_mcps`` tier-3 retry): when Path A's launcher CLI
  times out or exits non-zero against a tier-1 (potentially stale)
  binary, drive tier-3 explicitly and retry the CLI ONCE with the
  freshly-built binary before falling through to the Python writer.

* Fix 6 (always-touch ``UPDATE_DEFERRED.md`` on ``--update``): when an
  ``--update`` run produces zero actionable deferral entries, write a
  stub file so users have a paper trail confirming the run completed.

Mirrors the mocking conventions of
``tests/test_install_mcp_registration.py``: patches the helpers, drives
``_register_mcps`` end-to-end under ``VCT_USER_HOME_OVERRIDE``, asserts
side effects on disk + the deferral report.

References:
  * ``/tmp/v0213-fix-report-A.md`` (this agent's work log)
  * Brief: post-v0.2.12 launcher-update test surfaced 3 install.py bugs.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


_IS_WINDOWS = platform.system().lower().startswith("win")


def _make_pseudo_install_root(tmp_path: Path) -> Path:
    """Minimal install-root layout with venv-python + claude_mcp_servers/."""
    root = tmp_path / "example_install"
    root.mkdir()
    sub = "Scripts" if _IS_WINDOWS else "bin"
    py_name = "python.exe" if _IS_WINDOWS else "python"
    venv_bin = root / ".venv" / sub
    venv_bin.mkdir(parents=True)
    (venv_bin / py_name).write_text("#!/bin/sh\nexit 0\n")
    if not _IS_WINDOWS:
        (venv_bin / py_name).chmod(0o755)
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
    (root / "claude_mcp_servers" / "search_mcp").mkdir(parents=True)
    if not _IS_WINDOWS:
        wrapper = root / "claude_mcp_servers" / "search_mcp" / "wrapper.sh"
        wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
        wrapper.chmod(0o755)
    return root


def _seed_tauri_conf(install_root: Path, version: str = "0.2.13") -> Path:
    """Drop a tauri.conf.json with a parseable version field."""
    conf_dir = install_root / "launcher" / "src-tauri"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = conf_dir / "tauri.conf.json"
    conf.write_text(
        json.dumps({"version": version, "productName": "VCT Launcher"}),
        encoding="utf-8",
    )
    return conf


def _seed_target_release_temp(install_root: Path, mtime: float | None = None) -> Path:
    """Drop a fake ``target/release/vct-launcher-temp``."""
    rel = install_root / "launcher" / "src-tauri" / "target" / "release"
    rel.mkdir(parents=True, exist_ok=True)
    src = rel / "vct-launcher-temp"
    src.write_bytes(b"#!/bin/sh\necho fresh binary\nexit 0\n")
    if not _IS_WINDOWS:
        src.chmod(0o755)
    if mtime is not None:
        os.utime(src, (mtime, mtime))
    return src


def _seed_dist_binary(install_root: Path, mtime: float | None = None) -> Path:
    """Drop a fake dist binary at ``launcher/dist/<os>-<arch>/vct-launcher[.exe]``."""
    subdir, fname = install._launcher_binary_relative_path()
    dist_dir = install_root / "launcher" / "dist" / subdir
    dist_dir.mkdir(parents=True, exist_ok=True)
    dist = dist_dir / fname
    dist.write_bytes(b"#!/bin/sh\necho stale binary\nexit 0\n")
    if not _IS_WINDOWS:
        dist.chmod(0o755)
    if mtime is not None:
        os.utime(dist, (mtime, mtime))
    return dist


# ─────────────────────────────────────────────────────────────────────────
# Fix 1: _refresh_dist_binary_after_rebuild
# ─────────────────────────────────────────────────────────────────────────


class RefreshDistBinaryTests(unittest.TestCase):
    """Fix 1: post-cargo-rebuild dist-binary refresh."""

    def test_refresh_when_src_newer_and_produced_in_run(self):
        """Source newer than dist AND produced this run → dist gets refreshed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            _seed_dist_binary(root, mtime=old_ts)
            _seed_target_release_temp(root, mtime=new_ts)
            result = install._refresh_dist_binary_after_rebuild(
                root,
                no_swap=False,
                install_start_ts=old_ts + 1,  # source produced after run started
            )
            self.assertIsNotNone(result, "expected dist binary to be refreshed")
            # Content of dist should now match the fresh temp binary.
            subdir, fname = install._launcher_binary_relative_path()
            dist = root / "launcher" / "dist" / subdir / fname
            self.assertEqual(dist.read_bytes(), b"#!/bin/sh\necho fresh binary\nexit 0\n")
            if not _IS_WINDOWS:
                self.assertTrue(os.access(dist, os.X_OK), "dist must remain executable")

    def test_refresh_creates_dist_when_absent(self):
        """No prior dist artifact + fresh src → dist gets created from scratch."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            _seed_target_release_temp(root, mtime=new_ts)
            # No dist seeded — refresh should create it.
            result = install._refresh_dist_binary_after_rebuild(
                root,
                no_swap=False,
                install_start_ts=new_ts - 1,
            )
            self.assertIsNotNone(result)
            self.assertTrue(result.is_file())

    def test_no_swap_flag_short_circuits(self):
        """``no_swap=True`` → helper is a no-op even when src is newer."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            dist = _seed_dist_binary(root, mtime=old_ts)
            stale_content = dist.read_bytes()
            _seed_target_release_temp(root, mtime=new_ts)
            result = install._refresh_dist_binary_after_rebuild(
                root, no_swap=True, install_start_ts=old_ts + 1,
            )
            self.assertIsNone(result)
            # Dist binary untouched.
            self.assertEqual(dist.read_bytes(), stale_content)

    def test_no_swap_when_src_missing(self):
        """No ``target/release/vct-launcher-temp`` → helper returns None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            _seed_dist_binary(root, mtime=time.time())
            # No src seeded.
            result = install._refresh_dist_binary_after_rebuild(
                root, no_swap=False, install_start_ts=time.time() - 100,
            )
            self.assertIsNone(result)

    def test_no_swap_when_src_older_than_dist(self):
        """Conservative: src older than dist → no refresh (dist is the newest)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            old_ts = time.time() - 10000
            dist = _seed_dist_binary(root, mtime=new_ts)
            content_before = dist.read_bytes()
            _seed_target_release_temp(root, mtime=old_ts)
            result = install._refresh_dist_binary_after_rebuild(
                root, no_swap=False, install_start_ts=new_ts - 100,
            )
            self.assertIsNone(result)
            self.assertEqual(dist.read_bytes(), content_before)

    def test_no_swap_when_src_newer_but_not_in_run_and_no_version_drift(self):
        """Conservative: src newer than dist but produced BEFORE this run started
        AND tauri.conf.json mtime not newer than dist → no refresh.

        This guards against picking up weeks-old src/release/vct-launcher-temp
        artifacts that may have nothing to do with the current install.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            # Both src and conf are older than dist's mtime AND the run start.
            now = time.time()
            dist_ts = now - 100
            src_ts = now - 50  # newer than dist, older than run-start
            run_start = now - 10
            dist = _seed_dist_binary(root, mtime=dist_ts)
            conf = _seed_tauri_conf(root)
            os.utime(conf, (dist_ts - 1000, dist_ts - 1000))  # conf MUCH older
            _seed_target_release_temp(root, mtime=src_ts)
            content_before = dist.read_bytes()
            result = install._refresh_dist_binary_after_rebuild(
                root, no_swap=False, install_start_ts=run_start,
            )
            self.assertIsNone(
                result,
                "src newer than dist but not in-run + no version drift → no swap",
            )
            self.assertEqual(dist.read_bytes(), content_before)

    def test_refresh_when_version_stale_even_without_in_run(self):
        """Version drift signal: tauri.conf.json mtime newer than dist mtime
        is sufficient to justify a swap even without ``install_start_ts``
        evidence (covers the "user rebuilt outside install.py before
        --update" scenario).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            now = time.time()
            dist_ts = now - 1000
            src_ts = now - 100  # newer than dist
            conf_ts = now - 50  # newer than dist → version drift detected
            dist = _seed_dist_binary(root, mtime=dist_ts)
            conf = _seed_tauri_conf(root)
            os.utime(conf, (conf_ts, conf_ts))
            _seed_target_release_temp(root, mtime=src_ts)
            # install_start_ts is set AFTER src_ts → "produced_in_run" is False.
            result = install._refresh_dist_binary_after_rebuild(
                root, no_swap=False, install_start_ts=now,
            )
            self.assertIsNotNone(
                result,
                "version drift (conf newer than dist) should trigger swap",
            )

    def test_read_tauri_conf_version(self):
        """``_read_tauri_conf_version`` parses the version field reliably."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            conf = _seed_tauri_conf(root, version="0.2.13")
            self.assertEqual(install._read_tauri_conf_version(root), "0.2.13")
            # Missing file → None.
            conf.unlink()
            self.assertIsNone(install._read_tauri_conf_version(root))

    def test_read_tauri_conf_version_malformed(self):
        """Malformed tauri.conf.json → None (no exception)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            conf_dir = root / "launcher" / "src-tauri"
            conf_dir.mkdir(parents=True)
            (conf_dir / "tauri.conf.json").write_text("{ not valid json", encoding="utf-8")
            self.assertIsNone(install._read_tauri_conf_version(root))


# ─────────────────────────────────────────────────────────────────────────
# Fix 5: _register_mcps tier-3 retry
# ─────────────────────────────────────────────────────────────────────────


class RegisterMcpsTier3RetryTests(unittest.TestCase):
    """Fix 5: when Path A's launcher CLI times out / exits non-zero against
    a tier-1 (potentially stale) binary, drive tier-3 explicitly and
    retry the CLI ONCE before falling through to the Python writer."""

    def _drive_with_subprocess_runs(
        self,
        install_root: Path,
        fake_home: Path,
        first_run_result,
        retry_run_result=None,
        *,
        cargo_fresh_binary: Path | None = None,
        prefer_only_bundled: bool = False,
        no_rebuild_on_stale: bool = False,
    ) -> tuple[DeferralReport, list[list[str]]]:
        """Run ``_register_mcps`` with patched subprocess.run + cargo helper.

        ``first_run_result`` and ``retry_run_result`` may be:
          * a ``subprocess.CompletedProcess``-like mock (use mock.Mock with
            returncode/stdout/stderr), OR
          * an exception INSTANCE to raise (e.g. ``TimeoutExpired(...)``).

        Returns ``(deferral_report, captured_cmds)``.
        """
        report = DeferralReport()
        captured_cmds: list[list[str]] = []

        # Seed a tier-1 binary so _ensure_launcher_binary returns it.
        dist = _seed_dist_binary(install_root)

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            # First invocation uses first_run_result; second uses retry_run_result.
            if len(captured_cmds) == 1:
                if isinstance(first_run_result, BaseException):
                    raise first_run_result
                return first_run_result
            if retry_run_result is None:
                # No expectation for second call — fail loudly.
                raise AssertionError(
                    f"unexpected second subprocess.run call: {cmd}"
                )
            if isinstance(retry_run_result, BaseException):
                raise retry_run_result
            return retry_run_result

        # Patch cargo helper to return either a different fresh binary path
        # OR None (cargo unavailable).
        cargo_return = cargo_fresh_binary

        with mock.patch.dict(os.environ, {"VCT_USER_HOME_OVERRIDE": str(fake_home)}, clear=False), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(install, "_try_cargo_tauri_build", return_value=cargo_return):
            install._register_mcps(
                install_root, report,
                prefer_only_bundled=prefer_only_bundled,
                no_rebuild_on_stale=no_rebuild_on_stale,
            )
        return report, captured_cmds

    def test_tier3_retry_on_timeout_against_stale_binary(self):
        """Tier-1 binary times out → tier-3 retry succeeds → success path."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            # Fresh binary at a DIFFERENT path so we can prove the retry ran.
            fresh_dir = root / "launcher" / "src-tauri" / "target" / "release"
            fresh_dir.mkdir(parents=True, exist_ok=True)
            fresh_binary = fresh_dir / "vct-launcher-temp"
            fresh_binary.write_bytes(b"#!/bin/sh\nexit 0\n")
            if not _IS_WINDOWS:
                fresh_binary.chmod(0o755)

            # First subprocess.run: timeout (simulates stale binary that hangs).
            import subprocess as sp
            first = sp.TimeoutExpired(cmd=["x"], timeout=30)
            # Second subprocess.run: success (returncode=0).
            retry = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            report, cmds = self._drive_with_subprocess_runs(
                root, fake_home,
                first_run_result=first,
                retry_run_result=retry,
                cargo_fresh_binary=fresh_binary,
            )
            self.assertEqual(
                len(cmds), 2,
                "expected exactly 2 subprocess.run calls (initial + tier-3 retry)",
            )
            # Second call invoked the fresh binary, NOT the stale tier-1 one.
            self.assertEqual(cmds[1][0], str(fresh_binary))
            self.assertIn("--register-default-mcps", cmds[1])
            # Path A succeeded on retry → no Python-fallback deferral entry.
            ids = [e.condition_id for e in report.entries]
            self.assertNotIn("mcp_registration_python_fallback", ids)

    def test_tier3_retry_on_nonzero_exit(self):
        """Tier-1 exits non-zero → tier-3 retry → success."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            fresh_dir = root / "launcher" / "src-tauri" / "target" / "release"
            fresh_dir.mkdir(parents=True, exist_ok=True)
            fresh_binary = fresh_dir / "vct-launcher-temp"
            fresh_binary.write_bytes(b"#!/bin/sh\nexit 0\n")
            if not _IS_WINDOWS:
                fresh_binary.chmod(0o755)

            first = mock.Mock(returncode=2, stdout="", stderr="unknown flag\n")
            retry = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            report, cmds = self._drive_with_subprocess_runs(
                root, fake_home,
                first_run_result=first,
                retry_run_result=retry,
                cargo_fresh_binary=fresh_binary,
            )
            self.assertEqual(len(cmds), 2)
            self.assertEqual(cmds[1][0], str(fresh_binary))
            ids = [e.condition_id for e in report.entries]
            self.assertNotIn("mcp_registration_python_fallback", ids)

    def test_no_rebuild_on_stale_flag_blocks_retry(self):
        """``no_rebuild_on_stale=True`` → no tier-3 invocation, fall to Python."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()

            first = mock.Mock(returncode=2, stdout="", stderr="")
            # cargo helper should NEVER be invoked when no_rebuild_on_stale=True;
            # the mock.patch.object on _try_cargo_tauri_build is in place but
            # returns None — if it's called, that's fine but tests should not
            # depend on it. We assert no SECOND subprocess.run happens.
            report, cmds = self._drive_with_subprocess_runs(
                root, fake_home,
                first_run_result=first,
                retry_run_result=None,
                cargo_fresh_binary=None,
                no_rebuild_on_stale=True,
            )
            self.assertEqual(
                len(cmds), 1,
                "no_rebuild_on_stale must prevent the tier-3 retry CLI call",
            )
            # Path A failed → Python fallback ran → deferral entry emitted.
            ids = [e.condition_id for e in report.entries]
            self.assertIn("mcp_registration_python_fallback", ids)

    def test_prefer_only_bundled_blocks_retry(self):
        """``prefer_only_bundled=True`` → no tier-3 invocation."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()

            first = mock.Mock(returncode=2, stdout="", stderr="")
            report, cmds = self._drive_with_subprocess_runs(
                root, fake_home,
                first_run_result=first,
                retry_run_result=None,
                cargo_fresh_binary=None,
                prefer_only_bundled=True,
            )
            self.assertEqual(len(cmds), 1)
            ids = [e.condition_id for e in report.entries]
            self.assertIn("mcp_registration_python_fallback", ids)

    def test_tier3_retry_skipped_when_cargo_unavailable(self):
        """Cargo unavailable (returns None) → no second subprocess call,
        fall through to Python writer."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()

            first = mock.Mock(returncode=2, stdout="", stderr="")
            report, cmds = self._drive_with_subprocess_runs(
                root, fake_home,
                first_run_result=first,
                retry_run_result=None,
                cargo_fresh_binary=None,
            )
            self.assertEqual(
                len(cmds), 1,
                "no fresh binary → no retry; fall through to Python writer",
            )
            ids = [e.condition_id for e in report.entries]
            self.assertIn("mcp_registration_python_fallback", ids)


# ─────────────────────────────────────────────────────────────────────────
# Fix 6: UPDATE_DEFERRED.md stub on --update with zero entries
# ─────────────────────────────────────────────────────────────────────────


class UpdateDeferredStubTests(unittest.TestCase):
    """Fix 6: ``--update`` always touches UPDATE_DEFERRED.md."""

    def test_stub_written_when_no_entries(self):
        """Zero deferrals on --update → stub file written with timestamp."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "project"
            folder.mkdir()
            install._write_update_deferred_stub(folder, mode="update")
            target = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(target.is_file(), "stub file must exist")
            content = target.read_text(encoding="utf-8")
            self.assertIn("# No deferrals from update at ", content)
            self.assertIn("stub: true", content)
            self.assertIn("schema_version: 1", content)

    def test_stub_idempotent(self):
        """Writing the stub twice → no error, file overwritten cleanly."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "project"
            folder.mkdir()
            install._write_update_deferred_stub(folder, mode="update")
            first_content = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
            # Sleep briefly so the second-stamp differs (best-effort, not asserted).
            time.sleep(0.001)
            install._write_update_deferred_stub(folder, mode="update")
            target = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(target.is_file())
            second_content = target.read_text(encoding="utf-8")
            # Either equal (same-second timestamp) or differ only in timestamp.
            self.assertIn("# No deferrals from update at ", second_content)

    def test_stub_creates_parent_dirs(self):
        """``.claude/context/`` may not exist yet — stub creates it."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "fresh_project"
            folder.mkdir()
            # No .claude/ at all.
            install._write_update_deferred_stub(folder, mode="update")
            self.assertTrue((folder / ".claude" / "context" / "UPDATE_DEFERRED.md").is_file())

    def test_real_report_with_entries_does_not_trigger_stub(self):
        """``DeferralReport.write`` returns True when entries exist; the
        caller (install.py main flow) only writes a stub when ``write``
        returns False AND ``args.update``. This asserts the write-True
        invariant the stub-gate depends on.
        """
        from vco_lib.deferral_report import DeferralEntry

        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "project"
            folder.mkdir()
            report = DeferralReport()
            report.add_entry(DeferralEntry(
                condition_id="fix6_test",
                title="Test entry",
                detected="present",
                why_deferred="test",
                command_to_apply="echo ok",
                severity="info",
            ))
            wrote = report.write(folder)
            self.assertTrue(wrote, "non-empty report.write() must return True")
            real = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(real.is_file())
            # The real (non-stub) file does NOT contain the stub marker.
            self.assertNotIn("stub: true", real.read_text(encoding="utf-8"))

    def test_empty_report_write_returns_false(self):
        """``DeferralReport.write`` with no entries returns False → that's
        the signal the stub branch uses on --update."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "project"
            folder.mkdir()
            report = DeferralReport()
            wrote = report.write(folder)
            self.assertFalse(wrote, "empty report.write() must return False")
            # Real path does NOT exist; stub is install.py's job, not DeferralReport's.
            real = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertFalse(real.exists())


if __name__ == "__main__":
    unittest.main()
