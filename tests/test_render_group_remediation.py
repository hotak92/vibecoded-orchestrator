# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.53 L-P0-8: render / video group remediation hint.

When the launcher's `webkit_preflight` runs and the user is missing
`render` (and/or `video`) group membership, access to /dev/dri/renderD128
raises EACCES; the preflight silently skips GPU-accelerated WebKit. The
user wonders why the launcher chrome is slow but has no clear path to
the fix.

install.py now prints a clear remediation hint during the GPU detection
step when:
  - A GPU is detected (NVIDIA / AMD / Apple Metal), AND
  - The current user is NOT in BOTH `render` and `video` groups.

The hint surfaces the `sudo usermod -aG <missing> $USER` command and
the `newgrp` follow-up, so users have a complete fix in one place.

These tests exercise the membership probe + printer matrix.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install as install_mod  # noqa: E402


class UserInGroupTests(unittest.TestCase):
    """Coverage matrix for `_user_in_group`:
      - non-Linux OS → False (Windows + macOS have different perms models)
      - Linux, user IS in group → True
      - Linux, user NOT in group → False
      - Group doesn't exist (KeyError) → False
      - USER env unset, falls back to pwd.getpwuid(getuid())
      - All probes fail → False (safer default = "needs hint")
    """

    def test_non_linux_returns_false(self) -> None:
        with mock.patch.object(install_mod.platform, "system", return_value="Darwin"):
            self.assertFalse(install_mod._user_in_group("render"))
        with mock.patch.object(install_mod.platform, "system", return_value="Windows"):
            self.assertFalse(install_mod._user_in_group("render"))

    def test_user_in_group_returns_true(self) -> None:
        fake_group = mock.Mock(gr_mem=["alice", "bob"])
        fake_grp = mock.Mock(getgrnam=mock.Mock(return_value=fake_group))
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.dict(install_mod.os.environ, {"USER": "alice"}, clear=False), \
                mock.patch.dict(sys.modules, {"grp": fake_grp}):
            self.assertTrue(install_mod._user_in_group("render"))

    def test_user_not_in_group_returns_false(self) -> None:
        fake_group = mock.Mock(gr_mem=["root"])
        fake_grp = mock.Mock(getgrnam=mock.Mock(return_value=fake_group))
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.dict(install_mod.os.environ, {"USER": "alice"}, clear=False), \
                mock.patch.dict(sys.modules, {"grp": fake_grp}):
            self.assertFalse(install_mod._user_in_group("render"))

    def test_unknown_group_returns_false(self) -> None:
        fake_grp = mock.Mock(getgrnam=mock.Mock(side_effect=KeyError("no such group")))
        with mock.patch.object(install_mod.platform, "system", return_value="Linux"), \
                mock.patch.dict(install_mod.os.environ, {"USER": "alice"}, clear=False), \
                mock.patch.dict(sys.modules, {"grp": fake_grp}):
            self.assertFalse(install_mod._user_in_group("nonexistent"))


class RenderVideoGroupHintTests(unittest.TestCase):
    """The printer:
      - is idempotent (called twice = printed once)
      - does NOT print when user is already in BOTH groups
      - lists exactly the missing group(s) in the `usermod -aG` command
      - tells the user the `newgrp` follow-up
    """

    def setUp(self) -> None:
        install_mod._RENDER_GROUP_HINT_PRINTED = False

    def test_silent_when_in_both_groups(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(install_mod, "_user_in_group", return_value=True):
            install_mod._print_render_video_group_hint()
        self.assertEqual(buf.getvalue(), "",
                         "no output expected when user already in both groups")

    def test_lists_only_missing_groups(self) -> None:
        """If user is in `video` but not `render`, the usermod command
        should mention ONLY `render`, not `render,video`."""
        def in_group(name: str) -> bool:
            return name == "video"  # in video, not render

        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(install_mod, "_user_in_group", side_effect=in_group):
            install_mod._print_render_video_group_hint()
        out = buf.getvalue()
        self.assertIn("usermod -aG render $USER", out)
        # Critical: must NOT add `video` to the usermod cmd if user is
        # already in it (would be a no-op but suggests the user does
        # something they don't need to).
        self.assertNotIn("usermod -aG render,video", out)
        self.assertNotIn("usermod -aG video", out)

    def test_lists_both_groups_when_missing(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(install_mod, "_user_in_group", return_value=False):
            install_mod._print_render_video_group_hint()
        out = buf.getvalue()
        self.assertIn("usermod -aG render,video $USER", out)
        # Both groups mentioned in the diagnosis line too.
        self.assertIn("render", out)
        self.assertIn("video", out)

    def test_mentions_newgrp_followup(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(install_mod, "_user_in_group", return_value=False):
            install_mod._print_render_video_group_hint()
        out = buf.getvalue()
        self.assertIn("newgrp", out)

    def test_mentions_webkit_preflight_link(self) -> None:
        """The hint must mention the WebKit / GPU link so users see the
        connection between group membership and launcher acceleration."""
        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(install_mod, "_user_in_group", return_value=False):
            install_mod._print_render_video_group_hint()
        out = buf.getvalue()
        self.assertIn("webkit_preflight", out)
        self.assertIn("/dev/dri/renderD128", out)

    def test_idempotent_when_called_twice(self) -> None:
        buf1 = io.StringIO()
        with redirect_stdout(buf1), \
                mock.patch.object(install_mod, "_user_in_group", return_value=False):
            install_mod._print_render_video_group_hint()
        self.assertGreater(len(buf1.getvalue()), 0)

        buf2 = io.StringIO()
        with redirect_stdout(buf2), \
                mock.patch.object(install_mod, "_user_in_group", return_value=False):
            install_mod._print_render_video_group_hint()
        self.assertEqual(buf2.getvalue(), "",
                         "second call should not print (idempotent)")


if __name__ == "__main__":
    unittest.main()
