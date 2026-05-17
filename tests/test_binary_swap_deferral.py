# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.15 (Agent D, candidate 0.1) launcher
auto-restart-after-binary-swap UX.

Coverage:
  * ``_refresh_dist_binary_after_rebuild`` emits a
    ``launcher_restart_required`` deferral entry on successful swap.
  * The deferral entry carries the launcher PID from VCT_LAUNCHER_PID
    when set; absent env var → no PID hint but entry still written.
  * The entry carries a version derived from ``--version`` query (mocked)
    OR falls back to ``_read_install_version`` when the binary can't
    self-report.
  * No deferral when no swap happens (src missing, src older than dist,
    --no-binary-swap set, no version drift).
  * On Windows ERROR_SHARING_VIOLATION, rename-fallback produces a
    ``launcher_restart_required`` (success) entry; both overwrite AND
    rename failing produces ``launcher_binary_swap_failed_locked``
    (failure) entry. (Mocked — we don't actually need Windows to test
    the Python-side fallback logic.)
  * Soft-fail: passing ``deferral_report=None`` is a no-op (the original
    behaviour for callers that don't care about the deferral side
    effect).

Mirrors the conventions of ``tests/test_install_binary_resolution.py``:
patches the helpers, drives ``_refresh_dist_binary_after_rebuild`` directly,
asserts on disk + deferral report state.
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Fixture helpers (cloned from test_install_binary_resolution.py — kept
# inline to avoid cross-test imports that drift over time).
# ---------------------------------------------------------------------------

def _seed_target_release_temp(install_root: Path, mtime: float | None = None) -> Path:
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


def _seed_vct_module_json(install_root: Path, version: str = "0.2.15") -> Path:
    import json
    target = install_root / "vct-module.json"
    target.write_text(json.dumps({"name": "vco", "version": version}))
    return target


# ---------------------------------------------------------------------------
# Successful swap → launcher_restart_required deferral
# ---------------------------------------------------------------------------

class LauncherRestartDeferralEmission(unittest.TestCase):
    """Successful swap path emits the deferral entry."""

    def test_emits_launcher_restart_required_on_successful_swap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            _seed_dist_binary(root, mtime=old_ts)
            _seed_target_release_temp(root, mtime=new_ts)
            _seed_vct_module_json(root, version="0.2.15")

            report = DeferralReport()
            # Block the subprocess call to the binary --version — we don't
            # actually run a shell script that responds correctly to it.
            # The fallback path uses _read_install_version (= 0.2.15).
            with mock.patch.object(install, "_query_launcher_version", return_value=None):
                result = install._refresh_dist_binary_after_rebuild(
                    root,
                    no_swap=False,
                    install_start_ts=old_ts + 1,
                    deferral_report=report,
                )

            self.assertIsNotNone(result, "swap should have happened")
            self.assertTrue(
                report.has_condition("launcher_restart_required"),
                "expected launcher_restart_required entry after successful swap",
            )
            entry = next(
                e for e in report.entries
                if e.condition_id == "launcher_restart_required"
            )
            self.assertIn("0.2.15", entry.title)
            self.assertEqual(entry.severity, "info")
            self.assertIn("swapped into", entry.detected)

    def test_entry_includes_pid_when_env_var_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            _seed_target_release_temp(root, mtime=new_ts)

            report = DeferralReport()
            with mock.patch.dict(os.environ, {"VCT_LAUNCHER_PID": "12345"}):
                with mock.patch.object(install, "_query_launcher_version", return_value=None):
                    install._refresh_dist_binary_after_rebuild(
                        root,
                        no_swap=False,
                        install_start_ts=new_ts - 1,
                        deferral_report=report,
                    )
            entry = next(
                (e for e in report.entries if e.condition_id == "launcher_restart_required"),
                None,
            )
            self.assertIsNotNone(entry)
            self.assertIn("12345", entry.detected,
                          "PID should appear in the deferral message when env var set")

    def test_entry_omits_pid_when_env_var_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            _seed_target_release_temp(root, mtime=new_ts)

            report = DeferralReport()
            # Explicitly clear the env var (test isolation).
            env_no_pid = {k: v for k, v in os.environ.items() if k != "VCT_LAUNCHER_PID"}
            with mock.patch.dict(os.environ, env_no_pid, clear=True):
                with mock.patch.object(install, "_query_launcher_version", return_value=None):
                    install._refresh_dist_binary_after_rebuild(
                        root,
                        no_swap=False,
                        install_start_ts=new_ts - 1,
                        deferral_report=report,
                    )
            entry = next(
                (e for e in report.entries if e.condition_id == "launcher_restart_required"),
                None,
            )
            self.assertIsNotNone(entry)
            self.assertNotIn("PID:", entry.detected,
                             "no PID hint when env var absent")

    def test_entry_uses_query_version_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            _seed_target_release_temp(root, mtime=new_ts)
            _seed_vct_module_json(root, version="0.2.99")  # fallback (should NOT win)

            report = DeferralReport()
            # Binary self-reports a different version — that wins over the
            # fallback.
            with mock.patch.object(install, "_query_launcher_version", return_value="0.2.15"):
                install._refresh_dist_binary_after_rebuild(
                    root,
                    no_swap=False,
                    install_start_ts=new_ts - 1,
                    deferral_report=report,
                )
            entry = next(
                e for e in report.entries
                if e.condition_id == "launcher_restart_required"
            )
            self.assertIn("0.2.15", entry.title)
            self.assertNotIn("0.2.99", entry.title)

    def test_deferral_report_none_is_noop(self):
        """Passing None doesn't crash — preserves the original callsite
        signature for callers that don't care about the deferral."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            new_ts = time.time()
            _seed_target_release_temp(root, mtime=new_ts)

            with mock.patch.object(install, "_query_launcher_version", return_value=None):
                # Should not raise.
                result = install._refresh_dist_binary_after_rebuild(
                    root,
                    no_swap=False,
                    install_start_ts=new_ts - 1,
                    deferral_report=None,
                )
            self.assertIsNotNone(result, "swap should have happened")


# ---------------------------------------------------------------------------
# No-swap paths → no deferral
# ---------------------------------------------------------------------------

class NoDeferralWhenNoSwap(unittest.TestCase):
    """Confirm we don't write spurious deferral entries on skip paths."""

    def test_no_deferral_when_no_swap_flag_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            _seed_dist_binary(root, mtime=old_ts)
            _seed_target_release_temp(root, mtime=new_ts)
            report = DeferralReport()
            install._refresh_dist_binary_after_rebuild(
                root, no_swap=True,
                install_start_ts=old_ts + 1,
                deferral_report=report,
            )
            self.assertFalse(
                report.has_condition("launcher_restart_required"),
                "no deferral should be emitted when --no-binary-swap is set",
            )

    def test_no_deferral_when_src_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            _seed_dist_binary(root, mtime=time.time())
            report = DeferralReport()
            install._refresh_dist_binary_after_rebuild(
                root, no_swap=False,
                install_start_ts=time.time() - 100,
                deferral_report=report,
            )
            self.assertFalse(report.has_condition("launcher_restart_required"))

    def test_no_deferral_when_src_older_than_dist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            now = time.time()
            _seed_dist_binary(root, mtime=now)
            _seed_target_release_temp(root, mtime=now - 1000)
            report = DeferralReport()
            install._refresh_dist_binary_after_rebuild(
                root, no_swap=False,
                install_start_ts=now - 500,
                deferral_report=report,
            )
            self.assertFalse(report.has_condition("launcher_restart_required"))


# ---------------------------------------------------------------------------
# Windows-specific paths (mocked — no actual Windows host required)
# ---------------------------------------------------------------------------

class WindowsSwapFallbackPaths(unittest.TestCase):
    """Simulate ERROR_SHARING_VIOLATION + rename-fallback success/failure."""

    def _make_winerror32(self) -> OSError:
        err = OSError(13, "Permission denied")
        # Set winerror to 32 (ERROR_SHARING_VIOLATION) so the
        # _is_windows_sharing_violation helper triggers the fallback path.
        err.winerror = 32  # type: ignore[attr-defined]
        return err

    def _seed_at_windows_path(self, root: Path, src_mtime: float, dist_mtime: float) -> tuple[Path, Path]:
        """Seed src + dist at the Windows-canonical subdir paths.

        Mocking ``platform.system`` to return Windows changes the result of
        ``_launcher_binary_relative_path`` to ``("windows-x64", "vct-launcher.exe")``.
        But the test fixture helpers (``_seed_dist_binary`` /
        ``_seed_target_release_temp``) compute the subdir BEFORE the mock
        is in scope — so they'd seed at ``linux-x64/vct-launcher`` and the
        production code would look at ``windows-x64/vct-launcher.exe`` and
        find nothing. This helper materializes the path manually using the
        Windows tuple, side-stepping the helper's pre-mock resolution.
        """
        # Dist at windows-x64/vct-launcher.exe.
        dist_dir = root / "launcher" / "dist" / "windows-x64"
        dist_dir.mkdir(parents=True, exist_ok=True)
        dist = dist_dir / "vct-launcher.exe"
        dist.write_bytes(b"stale-windows-binary")
        if not _IS_WINDOWS:
            dist.chmod(0o755)
        os.utime(dist, (dist_mtime, dist_mtime))
        # Src at target/release/vct-launcher-temp (same path on every OS).
        rel = root / "launcher" / "src-tauri" / "target" / "release"
        rel.mkdir(parents=True, exist_ok=True)
        src = rel / "vct-launcher-temp"
        src.write_bytes(b"fresh-windows-binary")
        if not _IS_WINDOWS:
            src.chmod(0o755)
        os.utime(src, (src_mtime, src_mtime))
        return src, dist

    def test_emits_swap_failed_locked_when_rename_also_fails(self):
        """Both overwrite AND rename hit SHARING_VIOLATION → red banner deferral."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            src, dist = self._seed_at_windows_path(root, new_ts, old_ts)
            report = DeferralReport()

            err = self._make_winerror32()
            with mock.patch.object(install.platform, "system", return_value="Windows"):
                with mock.patch.object(install, "_is_windows_sharing_violation", return_value=True):
                    with mock.patch.object(install.shutil, "copy2", side_effect=err):
                        with mock.patch.object(Path, "rename", side_effect=err):
                            result = install._refresh_dist_binary_after_rebuild(
                                root,
                                no_swap=False,
                                install_start_ts=old_ts + 1,
                                deferral_report=report,
                            )

            self.assertIsNone(result, "swap should have failed")
            self.assertTrue(
                report.has_condition("launcher_binary_swap_failed_locked"),
                f"expected launcher_binary_swap_failed_locked deferral, "
                f"got: {[e.condition_id for e in report.entries]}",
            )
            self.assertFalse(
                report.has_condition("launcher_restart_required"),
                "must NOT emit success deferral when swap failed",
            )

    def test_emits_restart_required_when_rename_fallback_succeeds(self):
        """Overwrite fails with SHARING_VIOLATION but rename succeeds → success path."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            src, dist = self._seed_at_windows_path(root, new_ts, old_ts)
            _seed_vct_module_json(root, version="0.2.15")

            report = DeferralReport()

            # First copy2 fails (SHARING_VIOLATION). After Path.rename moves
            # the file aside, the second copy2 must succeed (real impl).
            call_counter = {"count": 0}
            original_copy2 = install.shutil.copy2
            err = self._make_winerror32()

            def selective_copy2(src, dst, *a, **kw):
                call_counter["count"] += 1
                if call_counter["count"] == 1:
                    raise err
                return original_copy2(src, dst, *a, **kw)

            with mock.patch.object(install.platform, "system", return_value="Windows"):
                with mock.patch.object(install, "_is_windows_sharing_violation", return_value=True):
                    with mock.patch.object(install.shutil, "copy2", side_effect=selective_copy2):
                        with mock.patch.object(install, "_query_launcher_version", return_value=None):
                            result = install._refresh_dist_binary_after_rebuild(
                                root,
                                no_swap=False,
                                install_start_ts=old_ts + 1,
                                deferral_report=report,
                            )

            self.assertIsNotNone(result, "rename-fallback should have succeeded")
            self.assertTrue(
                report.has_condition("launcher_restart_required"),
                "rename-fallback success path should still emit the restart entry",
            )
            self.assertFalse(
                report.has_condition("launcher_binary_swap_failed_locked"),
                "must NOT emit failure deferral when rename succeeded",
            )
            # The old binary should have been renamed to ".old-<version>".
            backup_glob = list((root / "launcher" / "dist" / "windows-x64").glob("vct-launcher.exe.old-*"))
            self.assertEqual(
                len(backup_glob), 1,
                f"expected one .old-<version> sibling, found: {backup_glob}",
            )

    def test_is_windows_sharing_violation_helper(self):
        """The helper recognises winerror=32 only on Windows hosts."""
        err = OSError(13, "Permission denied")
        err.winerror = 32  # type: ignore[attr-defined]

        with mock.patch.object(install.platform, "system", return_value="Windows"):
            self.assertTrue(install._is_windows_sharing_violation(err))

        with mock.patch.object(install.platform, "system", return_value="Linux"):
            self.assertFalse(install._is_windows_sharing_violation(err))

        # Plain OSError (no winerror attr) → False on Windows.
        plain = OSError(2, "no such file")
        with mock.patch.object(install.platform, "system", return_value="Windows"):
            self.assertFalse(install._is_windows_sharing_violation(plain))


# ---------------------------------------------------------------------------
# _query_launcher_version helper
# ---------------------------------------------------------------------------

class QueryLauncherVersion(unittest.TestCase):
    """The version-query helper handles every failure mode gracefully."""

    def test_returns_none_when_binary_missing(self):
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "nonexistent"
            self.assertIsNone(install._query_launcher_version(bogus))

    def test_returns_none_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "fake"
            binary.write_text("#!/bin/sh\necho err >&2\nexit 1\n")
            if not _IS_WINDOWS:
                binary.chmod(0o755)
            self.assertIsNone(install._query_launcher_version(binary))

    def test_parses_semver_token_from_output(self):
        """When the binary prints `vct-launcher 0.2.15`, return `0.2.15`."""
        with mock.patch.object(install.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="vct-launcher 0.2.15\n",
                stderr="",
            )
            with tempfile.TemporaryDirectory() as td:
                # is_file() check needs an actual file.
                binary = Path(td) / "fake"
                binary.write_text("#!/bin/sh\nexit 0\n")
                if not _IS_WINDOWS:
                    binary.chmod(0o755)
                self.assertEqual(install._query_launcher_version(binary), "0.2.15")

    def test_handles_v_prefix_and_trailing_punctuation(self):
        with mock.patch.object(install.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="v0.2.15, built 2026-05-17\n",
                stderr="",
            )
            with tempfile.TemporaryDirectory() as td:
                binary = Path(td) / "fake"
                binary.write_text("#!/bin/sh\nexit 0\n")
                if not _IS_WINDOWS:
                    binary.chmod(0o755)
                self.assertEqual(install._query_launcher_version(binary), "0.2.15")


if __name__ == "__main__":
    unittest.main()
