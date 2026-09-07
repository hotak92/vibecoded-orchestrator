# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-7 — the ``vct`` you RUN, not the one you have.

THE FAILURE THIS CLOSES
-----------------------
``vct`` was documented for years as "copy it from ``tools/vct-secrets/``", and
nothing in ``install.py`` refreshes that copy — by design: ``~/.vct-secrets/``
is a directory install.py states in three places it must never touch. So a
user's PATH ``vct`` can be arbitrarily old while they read current docs, and
every write-time guard added since their copy was taken is simply absent from
the bytes that run.

v0.2.85 added the ``VCT_GUARDS`` capability stamp and a
``_doctor_check_deployed_copy`` leg — but that leg lives in the CHECKOUT's copy,
so it only runs for someone who already knew to invoke the checkout's copy,
which is exactly what a person in this state does not do. **The stale copy
cannot detect itself.** The detector has to live somewhere the user's ordinary
tooling reaches: ``vco doctor``.

Verified on the machine this was written on: ``~/.local/bin/vct`` →
``~/.vct-secrets/vct``, dated 24 April, with **no ``VCT_GUARDS`` line at all**
— the "predates the stamp entirely" arm, in the field.

FOUR OUTCOMES
-------------
not-applicable (no finding) · ``ok`` · ``problem`` · ``unknown``. Absence of a
``vct`` is NOT staleness and must never be conflated with it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor  # noqa: E402

CHECKOUT_CLI = REPO_ROOT / "tools" / "vct-secrets" / "vct"

CURRENT = """#!/usr/bin/env bash
VCT_SECRETS_DIR="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
VCT_GUARDS="value-shape name-shape shared-scope symlink-self"
"""

PARTIAL = """#!/usr/bin/env bash
VCT_SECRETS_DIR="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
VCT_GUARDS="value-shape name-shape"
"""

ANCIENT = """#!/usr/bin/env bash
# vct — VCT Secrets Primitive CLI (Phase 1, Bash)
VCT_SECRETS_DIR="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
VERSION="0.1.0-phase1"
"""

UNRELATED = """#!/usr/bin/env python3
print("some other tool that happens to be called vct")
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "root"
        (self.root / "tools" / "vct-secrets").mkdir(parents=True)
        self.checkout_cli = self.root / "tools" / "vct-secrets" / "vct"
        self.checkout_cli.write_text(CURRENT, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _probe(self, resolved):
        res = doctor.DoctorResolvers(path_command=lambda name: resolved)
        return doctor.probe_stale_vct_deploy(self.root, res, {})

    def _deployed(self, body: str, name: str = "vct") -> str:
        path = self.base / "deployed" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return str(path)


class StaleDeployTests(Fixture):
    # ── act ─────────────────────────────────────────────────────────────

    def test_a_copy_predating_the_stamp_names_every_missing_guard(self):
        """The field shape on the machine this was written on."""
        findings = self._probe(self._deployed(ANCIENT))
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn("predates the guard capability stamp", f.summary)
        for guard in ("value-shape", "name-shape", "shared-scope", "symlink-self"):
            self.assertIn(guard, f.summary)
        self.assertEqual(f.fix, doctor.FIX_DEFER)

    def test_a_partially_current_copy_names_only_what_is_missing(self):
        f = self._probe(self._deployed(PARTIAL))[0]
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertEqual(f.detail["missing"], ["shared-scope", "symlink-self"])
        self.assertNotIn("value-shape", f.summary)

    # ── leave alone ─────────────────────────────────────────────────────

    def test_a_symlink_to_the_checkout_is_ok(self):
        """The deployment that CANNOT go stale — the one the README now
        recommends. Resolution is measured through realpath, so the symlink
        and its target compare equal."""
        link = self.base / "bin" / "vct"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(self.checkout_cli)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlinks unavailable (unprivileged Windows)")
        f = self._probe(str(link))[0]
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertIn("resolves to this checkout", f.summary)

    def test_a_separate_but_current_copy_is_ok(self):
        f = self._probe(self._deployed(CURRENT))[0]
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertIn("carries every guard", f.summary)

    def test_no_vct_on_path_raises_nothing_at_all(self):
        """Absence is not staleness. A health report that grades a tool the
        user never deployed is noise, and noise is what makes reports
        unread."""
        self.assertEqual(self._probe(None), [])
        self.assertEqual(self._probe(""), [])

    def test_a_folder_without_the_checkout_cli_is_not_applicable(self):
        with TemporaryDirectory() as td:
            res = doctor.DoctorResolvers(path_command=lambda n: "/usr/bin/vct")
            self.assertEqual(
                doctor.probe_stale_vct_deploy(Path(td), res, {}), [])

    # ── unknown ─────────────────────────────────────────────────────────

    def test_an_unrelated_program_named_vct_is_not_graded(self):
        f = self._probe(self._deployed(UNRELATED))[0]
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertIn("does not look like VCO's", f.summary)

    def test_an_unreadable_deployed_copy_is_unknown(self):
        path = self._deployed(CURRENT)
        with mock.patch.object(Path, "read_text", side_effect=OSError("EACCES")):
            findings = self._probe(path)
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)

    def test_a_reference_without_a_stamp_yields_unknown_not_a_verdict(self):
        """If the CHECKOUT has no stamp there is nothing to compare against —
        reporting a deployed copy as stale on no evidence would be an
        accusation, not a measurement."""
        self.checkout_cli.write_text(ANCIENT, encoding="utf-8")
        f = self._probe(self._deployed(ANCIENT))[0]
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertIn("nothing to compare", f.summary)

    def test_an_unreadable_reference_is_unknown(self):
        real = Path.read_text

        def _flaky(self_path, *a, **kw):
            if self_path == self.checkout_cli:
                raise OSError("EIO")
            return real(self_path, *a, **kw)

        with mock.patch.object(Path, "read_text", _flaky):
            findings = self._probe(self._deployed(CURRENT))
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("could not read the reference", findings[0].summary)


class NeverTouchesTheStoreTests(Fixture):
    """Detection only. ``~/.vct-secrets/`` is never written, ever."""

    def test_the_probe_writes_nothing_anywhere(self):
        deployed = self._deployed(ANCIENT)
        before = self._snapshot()
        self._probe(deployed)
        self.assertEqual(before, self._snapshot())

    def test_the_remediation_contains_no_destructive_or_automatic_repair(self):
        f = self._probe(self._deployed(ANCIENT))[0]
        self.assertNotIn("rm ", f.command)
        self.assertNotIn("mv ", f.command)
        # Every action line is commented — the user runs it, not VCO.
        for line in f.command.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)

    def test_the_remediation_matches_what_the_cli_itself_advises(self):
        """Two surfaces, one piece of advice. `vct doctor` prints `ln -sfn`
        and `cp -a` for this exact condition; the doctor must not invent a
        third way to fix it."""
        f = self._probe(self._deployed(ANCIENT))[0]
        cli = CHECKOUT_CLI.read_text(encoding="utf-8")
        self.assertIn("ln -sfn", f.command)
        self.assertIn("ln -sfn", cli)
        self.assertIn("cp -a", f.command)
        self.assertIn("cp -a", cli)

    def _snapshot(self):
        return sorted(
            (str(p.relative_to(self.base)), p.stat().st_mtime_ns, p.stat().st_size)
            for p in self.base.rglob("*") if p.is_file()
        )


class GuardStampParsingTests(unittest.TestCase):
    def test_quoted_space_separated_tokens(self):
        self.assertEqual(
            doctor.parse_vct_guards('VCT_GUARDS="a b c"\n'), ["a", "b", "c"])

    def test_single_quotes_and_bare_values(self):
        self.assertEqual(doctor.parse_vct_guards("VCT_GUARDS='a b'"), ["a", "b"])
        self.assertEqual(doctor.parse_vct_guards("VCT_GUARDS=solo"), ["solo"])

    def test_absent_stamp_is_none_not_empty(self):
        """`None` (never stamped) and `[]` (stamped, no guards) are different
        facts and the probe branches on the difference."""
        self.assertIsNone(doctor.parse_vct_guards("#!/bin/bash\necho hi\n"))
        self.assertEqual(doctor.parse_vct_guards('VCT_GUARDS=""'), [])

    def test_a_commented_out_stamp_is_not_a_stamp(self):
        self.assertIsNone(doctor.parse_vct_guards('# VCT_GUARDS="a b"\n'))

    def test_the_real_checkout_cli_parses(self):
        """R16: the reference this probe reads is the shipped file, and the
        format it assumes is the format that file actually has."""
        guards = doctor.parse_vct_guards(CHECKOUT_CLI.read_text(encoding="utf-8"))
        self.assertIsNotNone(guards)
        assert guards is not None
        self.assertIn("value-shape", guards)
        self.assertIn("symlink-self", guards)

    def test_the_stamp_prefix_matches_the_shipped_cli(self):
        cli = CHECKOUT_CLI.read_text(encoding="utf-8")
        self.assertTrue(
            any(line.startswith(doctor.VCT_GUARDS_PREFIX)
                for line in cli.splitlines()))
        self.assertEqual(doctor.VCT_CLI_REL, ("tools", "vct-secrets", "vct"))


class CrossPlatformTests(Fixture):
    """R14 — the OS-dependent legs, unit-tested from any runner."""

    def test_resolution_goes_through_the_injectable_which(self):
        """`shutil.which` is the ONE resolver, and it is seam-injected — so
        the Windows PATHEXT rules (an extensionless bash script is NOT found)
        produce the correct not-applicable answer rather than a wrong one."""
        seen = []

        def _which(name):
            seen.append(name)
            return None

        res = doctor.DoctorResolvers(path_command=_which)
        self.assertEqual(doctor.probe_stale_vct_deploy(self.root, res, {}), [])
        self.assertEqual(seen, ["vct"])

    def test_default_resolver_is_shutil_which(self):
        with mock.patch("shutil.which", return_value="/x/vct") as which:
            doctor.DoctorResolvers().resolve_path_command("vct")
        which.assert_called_once_with("vct")

    def test_case_folding_only_applies_where_the_os_folds(self):
        import ntpath
        import posixpath
        ident = lambda p: p  # noqa: E731
        self.assertTrue(doctor.same_location(
            r"C:\Tools\vct", "C:/tools/VCT",
            normcase=ntpath.normcase, realpath=ident))
        self.assertFalse(doctor.same_location(
            "/tools/vct", "/tools/VCT",
            normcase=posixpath.normcase, realpath=ident))


class ReadmeParityTests(unittest.TestCase):
    def test_the_symlink_instruction_now_has_a_detector_behind_it(self):
        """R16 §8.2 (WP-7): the docs recommend `ln -sfn`; that recommendation
        had no code enforcing or detecting the alternative. The probe IS the
        code — this pins that both exist."""
        candidates = [
            REPO_ROOT / "tools" / "vct-secrets" / "README.md",
            *(REPO_ROOT / "docs").rglob("*.md"),
            REPO_ROOT / "README.md",
        ]
        hits = [
            p for p in candidates
            if p.is_file()
            and "ln -sfn" in p.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertTrue(hits, "no doc recommends the symlink deployment any more")
        self.assertIn("stale_vct_deploy", doctor.PROBES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
