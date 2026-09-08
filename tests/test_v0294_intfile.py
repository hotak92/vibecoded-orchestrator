# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco_lib.intfile.read_int_line`` — the ONE small-state-file reader.

Seven private readers answered the same question before v0.2.94 and had
drifted in HOW they read it (whole file vs first line, strip-then-split vs
split-then-strip). Each call site keeps its own SENTINEL and its own bounds —
those are policy — and none keeps its own parser.

======================================================  ========  ==========
call site                                               sentinel  bounds
======================================================  ========  ==========
``model_router.config._read_port_file``                 None      1..65535
``model_router.__main__._recorded_pid``                 ``-1``    >= 1
``vco_lib.deferral_retry._read_pidfile``                None      >= 1
``vco_lib.hub_ensure.hub_pid``                          None      >= 1
``vco_lib.access_resolver._hub_port``                   None      1..65535
``vco_lib.project_config`` (``parse_int_line`` only)    None      1..65535
``vco_lib.codegraph_resync`` — lane D, lands at merge
======================================================  ========  ==========

``project_config`` shares the PARSER rather than the reader on purpose: it
emits a different warning for an unreadable ``hub.port`` than for one full of
nonsense, and both are a cross-language contract with the ``.sh``/``.ps1``
siblings. Sharing the reader would have collapsed the two into one.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vco_lib.intfile import read_int_line


class ReadIntLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-intfile-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "value"

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_a_plain_number_reads_back(self) -> None:
        self.assertEqual(read_int_line(self.write("4242\n")), 4242)

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertEqual(read_int_line(self.write("  4242  \n")), 4242)

    def test_only_the_first_line_is_read(self) -> None:
        """The reading ``hub_status.rs::probe`` already used, now shared.

        A file with an unexpected trailing line answers with the number
        somebody wrote rather than falling through to a default that, on the
        machine this was written for, is somebody else's service.
        """
        self.assertEqual(read_int_line(self.write("11441\nstray\n")), 11441)

    def test_every_unreadable_shape_is_the_sentinel(self) -> None:
        for text in ("", "   ", "not-a-port", "\n\n", "11441 11442"):
            with self.subTest(text=text):
                self.assertIsNone(read_int_line(self.write(text)))

    def test_a_missing_file_is_the_sentinel(self) -> None:
        self.assertIsNone(read_int_line(self.root / "nope"))

    def test_a_directory_is_the_sentinel_not_an_exception(self) -> None:
        self.assertIsNone(read_int_line(self.root))

    def test_non_utf8_bytes_do_not_raise(self) -> None:
        self.path.write_bytes(b"\xff\xfe4242\n")
        self.assertIsNone(read_int_line(self.path))

    def test_a_custom_sentinel_is_returned_instead_of_none(self) -> None:
        """The gateway's pid claim must tell "says nothing usable" from
        "the file is mine now", so its sentinel is a value."""
        self.assertEqual(read_int_line(self.write("junk"), sentinel=-1), -1)
        self.assertEqual(read_int_line(self.root / "nope", sentinel=-1), -1)

    def test_bounds_are_part_of_the_reading(self) -> None:
        """Out of range and unparseable are the same kind of "not evidence",
        so the check lives here rather than at four call sites."""
        self.assertIsNone(read_int_line(self.write("0"), minimum=1))
        self.assertIsNone(read_int_line(self.write("-5"), minimum=1))
        self.assertIsNone(read_int_line(self.write("70000"), maximum=65535))
        self.assertEqual(
            read_int_line(self.write("65535"), minimum=1, maximum=65535), 65535,
        )
        self.assertEqual(
            read_int_line(self.write("1"), minimum=1, maximum=65535), 1,
        )

    def test_a_negative_value_survives_when_no_minimum_is_given(self) -> None:
        """The bounds are opt-in: a caller that wants them says so."""
        self.assertEqual(read_int_line(self.write("-5")), -5)


class CallSiteSemanticsTests(unittest.TestCase):
    """Each of the four keeps the answers it had, through the shared reader."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-intfile-cs-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _file(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_port_reader_keeps_its_range_and_none(self) -> None:
        from model_router.config import _read_port_file

        self.assertEqual(_read_port_file(self._file("p", "11460\n")), 11460)
        for junk in ("0", "70000", "not-a-port", ""):
            with self.subTest(junk=junk):
                self.assertIsNone(_read_port_file(self._file("p", junk)))

    def test_the_gateway_pid_reader_keeps_its_value_sentinel(self) -> None:
        from model_router.__main__ import UNREADABLE_PID, _recorded_pid

        self.assertEqual(_recorded_pid(self._file("g", "4242\n")), 4242)
        for junk in ("0", "junk", ""):
            with self.subTest(junk=junk):
                self.assertEqual(
                    _recorded_pid(self._file("g", junk)), UNREADABLE_PID,
                )
        self.assertEqual(_recorded_pid(self.root / "missing"), UNREADABLE_PID)

    def test_the_deferral_pid_reader_keeps_none(self) -> None:
        from vco_lib.deferral_retry import _read_pidfile

        self.assertEqual(_read_pidfile(self._file("d", "4242\n")), 4242)
        self.assertIsNone(_read_pidfile(self._file("d", "junk")))
        self.assertIsNone(_read_pidfile(self.root / "missing"))

    def test_the_hub_pid_reader_keeps_none_and_rejects_zero(self) -> None:
        from vco_lib import hub_ensure

        with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(self.root)}):
            self._file("hub.pid", "4242\n")
            self.assertEqual(hub_ensure.hub_pid(), 4242)
            self._file("hub.pid", "0\n")
            self.assertIsNone(hub_ensure.hub_pid(), "0 is not a startable owner")
            self._file("hub.pid", "junk\n")
            self.assertIsNone(hub_ensure.hub_pid())

    def test_the_hub_port_readers_use_the_shared_one(self) -> None:
        """Three files parsed ``hub.port`` privately, whole-file.

        ``access_resolver`` shares the READER; ``project_config`` shares the
        PARSER only, because it must classify "unreadable" and "nonsense"
        into two different warnings that the .sh/.ps1 siblings also emit.
        """
        from vco_lib import access_resolver

        with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(self.root)}):
            os.environ.pop("VCT_HUB_PORT", None)
            self._file("hub.port", "7801\n")
            self.assertEqual(access_resolver._hub_port(), 7801)
            self._file("hub.port", "7801\nstray line\n")
            self.assertEqual(
                access_resolver._hub_port(), 7801,
                "a trailing line must read the number, not the default",
            )
            for junk in ("", "junk", "0", "70000"):
                with self.subTest(junk=junk):
                    self._file("hub.port", junk)
                    self.assertEqual(access_resolver._hub_port(), 7700)

    def test_the_project_config_parser_keeps_both_warnings(self) -> None:
        """Sharing the parse must not retire either warning."""
        from vco_lib.intfile import parse_int_line

        self.assertEqual(parse_int_line("7801\nstray\n", minimum=1), 7801)
        self.assertIsNone(parse_int_line("junk", minimum=1))
        source = (
            Path(__file__).resolve().parent.parent / "vco_lib/project_config.py"
        ).read_text(encoding="utf-8")
        for warning in ("hub_port_invalid", "hub_port_unreadable"):
            with self.subTest(warning=warning):
                self.assertIn(warning, source)

    def test_no_call_site_keeps_a_private_parser(self) -> None:
        """The point of the extraction, asserted rather than assumed."""
        repo = Path(__file__).resolve().parent.parent
        for rel in (
            "claude_mcp_servers/model_router/config.py",
            "claude_mcp_servers/model_router/__main__.py",
            "vco_lib/deferral_retry.py",
            "vco_lib/hub_ensure.py",
            "vco_lib/access_resolver.py",
            "vco_lib/project_config.py",
        ):
            with self.subTest(rel=rel):
                source = (repo / rel).read_text(encoding="utf-8")
                self.assertTrue(
                    "read_int_line" in source or "parse_int_line" in source,
                )
                # Short messages: the default would print the whole file.
                self.assertNotIn(
                    "raw.splitlines()[0]", source, f"{rel} kept a private parser",
                )
                self.assertNotIn(
                    'read_text(encoding="utf-8").strip())', source,
                    f"{rel} still parses a state file inline",
                )

    def test_the_codegraph_resync_reader_lands_with_lane_d(self) -> None:
        """The third ``hub.port`` parser is another lane's file.

        Asserted WHEN MIGRATED, skipped with the reason until then — never
        vacuously green: the skip names what is still private, so the day the
        import appears this becomes a hard check with no edit here.
        """
        repo = Path(__file__).resolve().parent.parent
        source = (repo / "vco_lib/codegraph_resync.py").read_text(encoding="utf-8")
        if "read_int_line" not in source and "parse_int_line" not in source:
            self.assertIn(
                '(root / "hub.port").read_text', source,
                "the private parser is neither migrated nor where it was — "
                "this check has gone blind",
            )
            self.skipTest(
                "vco_lib/codegraph_resync.py still parses hub.port privately; "
                "it is lane D's file and lands at merge",
            )
        self.assertNotIn('(root / "hub.port").read_text', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
