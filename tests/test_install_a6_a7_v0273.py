# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-6 + A-7 (v0.2.73): update-gate deadline refresh + atomic binary swap.

A-6: install.py re-extends the update-in-progress lockfile deadline at major
phase transitions (``_refresh_update_lockfile_deadline``). Without it the
fixed 15-min deadline silently evaporates mid-update on slow hardware and an
MCP can respawn against a mid-swap binary (the V52-AI fork-bomb).

A-7: the POSIX launcher-binary swap now copies to a temp file in the same dir,
chmods, then ``os.replace`` — atomic. A kill/disk-full mid-copy no longer
leaves a truncated binary at the canonical dist path for ``_register_mcps`` to
register.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

_IS_WINDOWS = platform.system().lower().startswith("win")


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


@unittest.skipIf(_IS_WINDOWS, "A-7 atomic path is POSIX-only; Windows uses rename-fallback")
class TestAtomicBinarySwap(unittest.TestCase):
    def test_swap_goes_through_temp_then_os_replace(self):
        """The POSIX swap must copy to a ``.tmp-<pid>`` sibling and
        ``os.replace`` onto the final path — never a bare in-place copy2."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            dist = _seed_dist_binary(root, mtime=old_ts)
            _seed_target_release_temp(root, mtime=new_ts)

            seen_replace = {"tmp_used": False}
            real_replace = os.replace

            def spy_replace(a, b, *args, **kw):
                # The source must be a temp sibling in the dist dir.
                if ".tmp-" in str(a) and Path(b) == dist:
                    seen_replace["tmp_used"] = True
                return real_replace(a, b, *args, **kw)

            report = DeferralReport()
            with mock.patch.object(install, "_query_launcher_version", return_value=None):
                with mock.patch.object(install.os, "replace", side_effect=spy_replace):
                    install._refresh_dist_binary_after_rebuild(
                        root,
                        no_swap=False,
                        install_start_ts=old_ts + 1,
                        deferral_report=report,
                    )
            self.assertTrue(
                seen_replace["tmp_used"],
                "swap must os.replace a temp sibling onto the dist path (atomic)",
            )
            # Final binary is the fresh one.
            self.assertIn(b"fresh binary", dist.read_bytes())

    def test_mid_copy_failure_leaves_original_dist_intact(self):
        """If the copy to the temp file fails, the original dist binary must
        be untouched (no truncated canonical binary)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            old_ts = time.time() - 10000
            new_ts = time.time()
            dist = _seed_dist_binary(root, mtime=old_ts)
            original_bytes = dist.read_bytes()
            _seed_target_release_temp(root, mtime=new_ts)

            def boom_copy2(src, dst, *a, **kw):
                # Fail only when writing the temp sibling (the swap copy).
                raise OSError("disk full")

            report = DeferralReport()
            with mock.patch.object(install, "_query_launcher_version", return_value=None):
                with mock.patch.object(install.shutil, "copy2", side_effect=boom_copy2):
                    install._refresh_dist_binary_after_rebuild(
                        root,
                        no_swap=False,
                        install_start_ts=old_ts + 1,
                        deferral_report=report,
                    )
            # Original dist binary is byte-identical (no truncation).
            self.assertEqual(dist.read_bytes(), original_bytes)
            # No orphan temp file left behind.
            dist_dir = dist.parent
            leftovers = [p for p in dist_dir.iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftovers, [], f"orphan temp files: {leftovers}")


# A-6 deadline-refresh coverage MOVED (P2c-a, v0.2.75): the
# `_refresh_update_lockfile_deadline` helper was extracted to
# `vco_lib.install_update_gate.InstallUpdateGate`, and the P3a rider
# deliberately CHANGED the absent-lockfile behaviour during --update
# (re-create instead of no-op). The full matrix — extend-on-present,
# re-create-on-absent-mid-update, no-op-on-fresh-install, soft-fail,
# atexit-once, and the main() call-site structural guards — lives in
# tests/test_install_update_gate_v0275.py.


if __name__ == "__main__":
    unittest.main()
