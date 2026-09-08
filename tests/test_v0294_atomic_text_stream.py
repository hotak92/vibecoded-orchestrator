# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The v0.2.94 additions to ``vco_lib.atomic``: streaming writes, in-place rotation.

Added in v0.2.94 because ``vco fix-transcript`` rewrites session ``.jsonl``
files that reach hundreds of megabytes: ``atomic_write_text`` /
``atomic_write_bytes`` take the whole body as a value, so a streaming caller
either holds a gigabyte in memory or hand-rolls the tmp+rename dance a fifth
time — and the second option is what ``tests/test_v0292_atomic_one_home.py``
exists to forbid.

The contract is the family's: same-directory tempfile, fsync, atomic replace,
tempfile removed on any exception, destination untouched when the block
fails. The optional ``backup=`` is the other half of the same dance and is
pinned here too.

``rotate_tail_lines(in_place=True)`` lands in the same version and for a
related reason: the model gateway's boot log is held OPEN by the init system
that captures it, so the replace-the-path rotation the function already had
would leave every later line in an unlinked inode.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vco_lib.atomic import atomic_text_stream, rotate_tail_lines


class AtomicTextStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-ats-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "file.txt"

    def test_the_body_lands_on_clean_exit(self) -> None:
        with atomic_text_stream(self.target) as handle:
            handle.write("one\n")
            handle.write("two\n")
        self.assertEqual(self.target.read_text(), "one\ntwo\n")

    def test_the_destination_is_untouched_until_the_block_exits(self) -> None:
        self.target.write_text("old\n")
        with atomic_text_stream(self.target) as handle:
            handle.write("new\n")
            self.assertEqual(self.target.read_text(), "old\n")
        self.assertEqual(self.target.read_text(), "new\n")

    def test_an_exception_leaves_the_original_and_no_tempfile(self) -> None:
        self.target.write_text("old\n")

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with atomic_text_stream(self.target) as handle:
                handle.write("half")
                raise Boom
        self.assertEqual(self.target.read_text(), "old\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["file.txt"])

    def test_a_control_flow_exception_also_cleans_up(self) -> None:
        """``BaseException``, not ``Exception``: the abandon path in
        ``transcript_repair.repair_file`` must not leak a tempfile either."""
        self.target.write_text("old\n")
        with self.assertRaises(KeyboardInterrupt):
            with atomic_text_stream(self.target) as handle:
                handle.write("half")
                raise KeyboardInterrupt
        self.assertEqual([p.name for p in self.root.iterdir()], ["file.txt"])

    def test_backup_keeps_the_previous_version(self) -> None:
        self.target.write_text("old\n")
        backup = self.root / "file.txt.bak"
        with atomic_text_stream(self.target, backup=backup) as handle:
            handle.write("new\n")
        self.assertEqual(backup.read_text(), "old\n")
        self.assertEqual(self.target.read_text(), "new\n")

    def test_the_backup_is_a_hard_link_so_the_target_never_vanishes(self) -> None:
        """Two renames leave a window with no file at ``path`` at all.

        The backup is taken as a link BEFORE the single replace, so a crash
        anywhere in the sequence leaves the original reachable under at least
        one name.
        """
        self.target.write_text("old\n")
        backup = self.root / "file.txt.bak"
        original_inode = self.target.stat().st_ino
        with atomic_text_stream(self.target, backup=backup) as handle:
            handle.write("new\n")
            # Mid-block: nothing has moved yet.
            self.assertEqual(self.target.read_text(), "old\n")
        self.assertEqual(backup.stat().st_ino, original_inode)
        self.assertNotEqual(self.target.stat().st_ino, original_inode)
        self.assertEqual(backup.read_text(), "old\n")

    def test_a_failure_at_the_replace_still_leaves_the_target_in_place(self) -> None:
        """The crash window, made observable.

        With the backup taken as a LINK there is one replace, and a failure in
        it leaves ``path`` untouched. With two renames the first one has
        already moved the file away, so the same failure leaves NOTHING at
        ``path`` — the case this ordering exists to remove.
        """
        import os
        from unittest import mock

        self.target.write_text("old\n")
        backup = self.root / "file.txt.bak"
        real_replace = os.replace

        def explode(src, dst, *args, **kwargs):
            if str(dst) == str(self.target):
                raise OSError("simulated crash at the replace")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("os.replace", side_effect=explode):
            with self.assertRaises(OSError):
                with atomic_text_stream(self.target, backup=backup) as handle:
                    handle.write("new\n")

        self.assertTrue(self.target.is_file(), "the original must still be there")
        self.assertEqual(self.target.read_text(), "old\n")

    def test_it_falls_back_to_a_rename_where_links_are_unavailable(self) -> None:
        """FAT, some network mounts, a cross-device path: still back up.

        The errno is the SIGNAL, so the fake carries a real one. A bare
        ``OSError`` (errno ``None``) is not a filesystem saying "I cannot
        link" — it is a test saying "something went wrong", and treating the
        two the same is what let ``EEXIST`` reach the rename below.
        """
        import errno
        import os
        from unittest import mock

        for code in (errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP):
            with self.subTest(errno=errno.errorcode[code]):
                self.target.write_text("old\n")
                backup = self.root / f"file.txt.bak-{code}"
                real_link = os.link
                with mock.patch(
                    "os.link", side_effect=OSError(code, os.strerror(code)),
                ) as fake:
                    with atomic_text_stream(self.target, backup=backup) as handle:
                        handle.write("new\n")
                self.assertTrue(fake.called)
                self.assertIsNotNone(real_link)
                self.assertEqual(backup.read_text(), "old\n")
                self.assertEqual(self.target.read_text(), "new\n")

    def test_an_existing_backup_is_never_clobbered(self) -> None:
        """THE finding: a second repair in the same second must not eat the
        first backup.

        Catching every ``OSError`` around ``os.link`` swallowed ``EEXIST``
        and fell through to ``os.replace(target, backup)``, which overwrote
        the earlier backup with the newer version of the file — destroying
        exactly the copy worth keeping (the one taken BEFORE anything was
        rewritten).
        """
        self.target.write_text("second-version\n")
        backup = self.root / "file.txt.bak"
        backup.write_text("first-backup\n")

        with self.assertRaises(FileExistsError):
            with atomic_text_stream(self.target, backup=backup) as handle:
                handle.write("third-version\n")

        self.assertEqual(backup.read_text(), "first-backup\n")
        self.assertEqual(
            self.target.read_text(), "second-version\n",
            "and the destination is untouched: nothing half-happened",
        )
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["file.txt", "file.txt.bak"],
            "no tempfile left behind on the refusal path",
        )

    def test_an_unexpected_link_error_is_raised_not_renamed_over(self) -> None:
        """Only "cannot link here" errnos fall back; the rest are errors."""
        import errno
        import os
        from unittest import mock

        self.target.write_text("old\n")
        backup = self.root / "file.txt.bak"
        with mock.patch(
            "os.link", side_effect=OSError(errno.EIO, os.strerror(errno.EIO)),
        ):
            with self.assertRaises(OSError) as caught:
                with atomic_text_stream(self.target, backup=backup) as handle:
                    handle.write("new\n")
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(self.target.read_text(), "old\n")
        self.assertFalse(backup.exists())

    def test_the_destination_is_owner_only_unless_a_mode_is_given(self) -> None:
        """``mkstemp`` makes it 0600; ``mode=`` is how a caller keeps its own.

        Documented rather than silently inherited, because the replace does
        not carry the old file's bits across — it swings the NAME to a new
        inode, and that inode's mode is the tempfile's.
        """
        import stat
        import sys

        if sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        self.target.write_text("old\n")
        self.target.chmod(0o644)
        with atomic_text_stream(self.target) as handle:
            handle.write("new\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

        with atomic_text_stream(self.target, mode=0o644) as handle:
            handle.write("newer\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)

    def test_the_backup_keeps_the_originals_mode(self) -> None:
        """It is a hard link, so it shares the inode that carries the bits."""
        import stat
        import sys

        if sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        self.target.write_text("old\n")
        self.target.chmod(0o644)
        backup = self.root / "file.txt.bak"
        with atomic_text_stream(self.target, backup=backup) as handle:
            handle.write("new\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o644)

    def test_backup_is_not_created_for_a_file_that_did_not_exist(self) -> None:
        backup = self.root / "file.txt.bak"
        with atomic_text_stream(self.target, backup=backup) as handle:
            handle.write("new\n")
        self.assertFalse(backup.exists())
        self.assertEqual(self.target.read_text(), "new\n")

    def test_line_endings_are_preserved_verbatim(self) -> None:
        """``newline=""`` by default: a rewriter that translated CRLF would
        corrupt the very lines it is copying through."""
        with atomic_text_stream(self.target) as handle:
            handle.write("a\r\nb\n")
        self.assertEqual(self.target.read_bytes(), b"a\r\nb\n")

    def test_non_utf8_bytes_round_trip_under_surrogateescape(self) -> None:
        raw = b'{"x":"caf\xe9"}\n'
        self.target.write_bytes(raw)
        text = self.target.read_text(encoding="utf-8", errors="surrogateescape")
        with atomic_text_stream(self.target, errors="surrogateescape") as handle:
            handle.write(text)
        self.assertEqual(self.target.read_bytes(), raw)

    def test_a_missing_parent_directory_is_created(self) -> None:
        nested = self.root / "a" / "b" / "file.txt"
        with atomic_text_stream(nested) as handle:
            handle.write("x")
        self.assertEqual(nested.read_text(), "x")


class RotateTailLinesInPlaceTests(unittest.TestCase):
    """Rotating a log a live process holds open in append mode."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-rot-")
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "boot.log"

    def _fill(self, lines: int) -> None:
        self.log.write_text(
            "".join(f"line {i}\n" for i in range(lines)), encoding="utf-8",
        )

    def test_it_keeps_the_tail_and_the_inode(self) -> None:
        """The inode is the whole point: an open descriptor must follow."""
        self._fill(2000)
        inode = self.log.stat().st_ino
        self.assertTrue(
            rotate_tail_lines(
                self.log, max_bytes=1024, keep_lines=10, in_place=True,
            )
        )
        self.assertEqual(self.log.stat().st_ino, inode)
        kept = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[-1], "line 1999")

    def test_a_holder_appending_through_an_open_handle_still_lands(self) -> None:
        """The regression the flag exists for.

        With the replace-based rotation this appended into the OLD, unlinked
        inode: the file on disk never grew again and the lines were lost with
        the last descriptor.
        """
        self._fill(2000)
        with self.log.open("a", encoding="utf-8") as holder:
            rotate_tail_lines(
                self.log, max_bytes=1024, keep_lines=5, in_place=True,
            )
            holder.write("after the rotation\n")
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("after the rotation", text)
        self.assertIn("line 1999", text)

    def test_the_replacing_mode_is_still_the_default(self) -> None:
        """LEAVE-ALONE half: the existing callers write short-lived opens and
        must keep the atomic-replace semantics they were reviewed with."""
        self._fill(2000)
        inode = self.log.stat().st_ino
        self.assertTrue(rotate_tail_lines(self.log, max_bytes=1024, keep_lines=5))
        self.assertNotEqual(self.log.stat().st_ino, inode)

    def test_a_small_file_is_left_alone_in_either_mode(self) -> None:
        self._fill(3)
        before = self.log.read_bytes()
        for in_place in (False, True):
            with self.subTest(in_place=in_place):
                self.assertFalse(
                    rotate_tail_lines(
                        self.log,
                        max_bytes=1_000_000,
                        keep_lines=1,
                        in_place=in_place,
                    )
                )
                self.assertEqual(self.log.read_bytes(), before)

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertFalse(
            rotate_tail_lines(
                Path(self.tmp.name) / "nope.log",
                max_bytes=1,
                keep_lines=1,
                in_place=True,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
