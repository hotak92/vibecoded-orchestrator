# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the source-repo validation gate in install.py.

The wizard previously let users pick an arbitrary empty folder as
install_path, and install.py / install_orchestrator would copy the
ORCHESTRATOR_MANAGED_PATHS subset in — producing a half-install
with no launcher/ and no first-install.sh that the end user couldn't
run. The 2026-04-29 lockdown adds `validate_source_repo()` that
refuses to install into anything that doesn't have BOTH install.py
and first-install.sh side by side.

These tests exercise the helper directly — no full installer side
effects are triggered.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


class ValidateSourceRepoTests(unittest.TestCase):
    def test_rejects_empty_dir(self):
        # An empty tmp dir is the canonical "user picked an empty
        # folder" case. validate_source_repo must SystemExit with a
        # message that names install.py and first-install.sh so the
        # user knows what's expected.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                install.validate_source_repo(Path(td))
            msg = str(cm.exception)
            self.assertIn("install.py", msg)
            self.assertIn("first-install.sh", msg)

    def test_rejects_install_py_only(self):
        # install.py present but first-install.sh missing — still a
        # non-source target, must be refused.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "install.py").write_text("# install\n")
            with self.assertRaises(SystemExit):
                install.validate_source_repo(Path(td))

    def test_rejects_first_install_sh_only(self):
        # first-install.sh present but install.py missing.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "first-install.sh").write_text("#!/usr/bin/env bash\n")
            with self.assertRaises(SystemExit):
                install.validate_source_repo(Path(td))

    def test_accepts_source_repo(self):
        # Both markers present — must pass.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "install.py").write_text("# install\n")
            (Path(td) / "first-install.sh").write_text("#!/usr/bin/env bash\n")
            # Should not raise.
            install.validate_source_repo(Path(td))

    def test_real_project_root_passes(self):
        # The real PROJECT_ROOT (the source checkout these tests live
        # in) must always pass — otherwise CI itself would fail. This
        # also guards against a future refactor that would make
        # install.py refuse to run from its own clone.
        install.validate_source_repo(install.PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
