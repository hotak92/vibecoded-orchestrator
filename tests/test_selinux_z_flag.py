# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.53 L-P0-3: SELinux enforcing detection + bind-mount `:Z` hint.

The recurring footgun on Fedora / RHEL / CentOS Stream / Rocky / Alma is
that Podman starts containers cleanly, but every bind-mount volume gives
"Permission denied" inside the container because SELinux's default
label policy blocks the container's cross-context access. The fix is
either (a) the `:Z` suffix on the bind-mount, (b) `chcon -Rt
container_file_t <host_path>`, or (c) temporarily `setenforce 0`.

install.py now:
  1. Detects SELinux enforcement via `getenforce` (canonical) or
     `/sys/fs/selinux/enforce` sysfs fallback.
  2. When enforcing AND we're going to set up containers, prints a
     clear remediation hint listing all three options.
  3. Is a no-op on non-Linux OSes and on Linux with SELinux
     permissive/disabled.

These tests exercise the helper unit-style + verify the printer
integration is wired into `_detect_system`.
"""
from __future__ import annotations

import io
import platform
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install as install_mod  # noqa: E402


class DetectSelinuxEnforcingTests(unittest.TestCase):
    """Coverage matrix for `_detect_selinux_enforcing`:
      - non-Linux OS → False (no probe attempted)
      - getenforce present, returns "Enforcing" → True
      - getenforce present, returns "Permissive" → False
      - getenforce present, returns "Disabled" → False
      - getenforce missing, sysfs has "1" → True
      - getenforce missing, sysfs has "0" → False
      - getenforce missing, sysfs absent → False (assume non-SELinux)
      - getenforce throws OSError → fall through to sysfs
      - getenforce + sysfs both raise → False (soft-fail)
    """

    def test_non_linux_returns_false(self) -> None:
        with mock.patch.object(install_mod.platform, "system", return_value="Darwin"):
            self.assertFalse(install_mod._detect_selinux_enforcing())
        with mock.patch.object(install_mod.platform, "system", return_value="Windows"):
            self.assertFalse(install_mod._detect_selinux_enforcing())

    def test_getenforce_enforcing_returns_true(self) -> None:
        mock_result = mock.Mock(returncode=0, stdout="Enforcing\n")
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value="/usr/sbin/getenforce"), \
                mock.patch.object(install_mod.subprocess, "run", return_value=mock_result):
            self.assertTrue(install_mod._detect_selinux_enforcing())

    def test_getenforce_permissive_returns_false(self) -> None:
        mock_result = mock.Mock(returncode=0, stdout="Permissive\n")
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value="/usr/sbin/getenforce"), \
                mock.patch.object(install_mod.subprocess, "run", return_value=mock_result):
            self.assertFalse(install_mod._detect_selinux_enforcing())

    def test_getenforce_disabled_returns_false(self) -> None:
        mock_result = mock.Mock(returncode=0, stdout="Disabled\n")
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value="/usr/sbin/getenforce"), \
                mock.patch.object(install_mod.subprocess, "run", return_value=mock_result):
            self.assertFalse(install_mod._detect_selinux_enforcing())

    def test_getenforce_case_insensitive(self) -> None:
        """The probe must accept `Enforcing` regardless of trailing
        whitespace / case (defensive against future getenforce
        formatting changes)."""
        mock_result = mock.Mock(returncode=0, stdout="ENFORCING\n")
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value="/usr/sbin/getenforce"), \
                mock.patch.object(install_mod.subprocess, "run", return_value=mock_result):
            self.assertTrue(install_mod._detect_selinux_enforcing())

    def test_sysfs_fallback_when_getenforce_missing(self) -> None:
        """When `getenforce` is not on PATH (minimal container), fall
        back to reading /sys/fs/selinux/enforce."""
        m = mock.mock_open(read_data="1")
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value=None), \
                mock.patch("install.Path") as mock_path:
            instance = mock.Mock()
            instance.is_file.return_value = True
            instance.read_text.return_value = "1"
            mock_path.return_value = instance
            self.assertTrue(install_mod._detect_selinux_enforcing())

    def test_sysfs_zero_returns_false(self) -> None:
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value=None), \
                mock.patch("install.Path") as mock_path:
            instance = mock.Mock()
            instance.is_file.return_value = True
            instance.read_text.return_value = "0"
            mock_path.return_value = instance
            self.assertFalse(install_mod._detect_selinux_enforcing())

    def test_sysfs_absent_returns_false(self) -> None:
        """No /sys/fs/selinux/enforce → SELinux not compiled into kernel
        → return False."""
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value=None), \
                mock.patch("install.Path") as mock_path:
            instance = mock.Mock()
            instance.is_file.return_value = False
            mock_path.return_value = instance
            self.assertFalse(install_mod._detect_selinux_enforcing())

    def test_getenforce_oserror_falls_through_to_sysfs(self) -> None:
        """If getenforce binary exists but raises (PATH lookup error,
        timeout) fall through to the sysfs probe."""
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value="/usr/sbin/getenforce"), \
                mock.patch.object(install_mod.subprocess, "run", side_effect=OSError("bad PATH")), \
                mock.patch("install.Path") as mock_path:
            instance = mock.Mock()
            instance.is_file.return_value = True
            instance.read_text.return_value = "1"
            mock_path.return_value = instance
            self.assertTrue(install_mod._detect_selinux_enforcing())

    def test_all_probes_fail_returns_false(self) -> None:
        """Soft-fail throughout: any unexpected error → False (caller
        treats this as 'no SELinux fix needed', the safer default)."""
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.object(install_mod.shutil, "which", return_value=None), \
                mock.patch("install.Path", side_effect=OSError):
            self.assertFalse(install_mod._detect_selinux_enforcing())


class SelinuxBindMountHintPrinterTests(unittest.TestCase):
    """The printer is idempotent (called twice = printed once) and
    surfaces all three remediation options."""

    def setUp(self) -> None:
        # Reset the module-level dedup flag for each test so we control
        # the printing behaviour independently.
        install_mod._SELINUX_HINT_PRINTED = False

    def test_prints_all_three_remediation_options(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            install_mod._print_selinux_bind_mount_hint()
        out = buf.getvalue()
        # Option (a): named-volume layout (no migration).
        self.assertIn("(a)", out)
        self.assertIn("NAMED-volume", out)
        # Option (b): :Z suffix + chcon command (both must be present).
        self.assertIn("(b)", out)
        self.assertIn(":Z", out)
        self.assertIn("chcon", out)
        self.assertIn("container_file_t", out)
        # Option (c): temporary setenforce 0.
        self.assertIn("(c)", out)
        self.assertIn("setenforce 0", out)

    def test_idempotent_when_called_twice(self) -> None:
        """Second call must produce no output (dedup flag)."""
        buf1 = io.StringIO()
        with redirect_stdout(buf1):
            install_mod._print_selinux_bind_mount_hint()
        self.assertGreater(len(buf1.getvalue()), 0)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            install_mod._print_selinux_bind_mount_hint()
        self.assertEqual(buf2.getvalue(), "",
                         "second call should not print (idempotent)")

    def test_hint_mentions_compose_override_path(self) -> None:
        """The hint must reference the canonical override file path so
        users know WHICH compose to edit."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            install_mod._print_selinux_bind_mount_hint()
        out = buf.getvalue()
        self.assertIn("docker-compose.override.yml", out)


if __name__ == "__main__":
    unittest.main()
