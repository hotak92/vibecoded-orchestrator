# SPDX-License-Identifier: AGPL-3.0-or-later
# v0.2.95 lane F4 — standalone `vco doctor` said "not evaluated — the caller
# supplied no envelope" for `launcher_binary_fresh` and `prereqs`, which a
# user reads as ABSENCE of a problem (PLAN-v0295 §"FOUND IN THE 0.2.94
# DOGFOOD", F4). The rule this pins: a probe that cannot run must read as an
# unanswered question that names what would answer it — never OK, never
# absence.
#
# The fix, pinned here end to end:
#
# * the standalone CLI OBTAINS the facts itself through the SAME producers
#   install.py's own doctor phase calls — `_bootstrap_build_envelope` /
#   `_launcher_binary_relative_path` + `_read_launcher_version` — loaded by
#   PATH via importlib (direct function reuse, NOT a shell-out: install.py
#   already calls the producer in-process, and each envelope sub-probe has
#   its own internal timeout);
# * only when THAT fails do the probes print tri-state `unknown` with the
#   exact runnable command (`doctor.BOOTSTRAP_COMMAND`), in the summary text
#   (render_lines prints `command` only for problems, so the summary is where
#   a human reads it) AND in the machine-readable `command` field;
# * a printed command is shipped code: the last test class proves the command
#   is a real install.py CLI by invoking its dispatch with the same argv and
#   parsing the JSON it prints.
#
# Hermeticity: the producer in these tests is a FAKE install.py written into
# a tmp folder (plus one bounded read-only invocation of the real
# `install.py --bootstrap --json` argv shape against the checkout, which is
# detection-only and touches no state dir, hub, or container).

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_INSTALL_PY = '''\
"""Fake install root producer — the two facts the doctor asks for."""


def _bootstrap_build_envelope(root):
    return {"schema_version": 1, "missing_prereqs": []}


def _launcher_binary_relative_path():
    return ("test-arch", "vct-launcher-test")


def _read_launcher_version(root):
    return "9.9.9"
'''

#: The command the probes print. A printed command is shipped code — asserted
#: real by BootstrapCommandIsRealTests below, not merely formatted here.
EXPECTED_COMMAND = "python install.py --bootstrap --json"


def _make_fake_install_root(folder: Path) -> Path:
    (folder / "install.py").write_text(FAKE_INSTALL_PY, encoding="utf-8")
    dist = folder / "launcher" / "dist" / "test-arch"
    dist.mkdir(parents=True)
    (dist / "vct-launcher-test.metadata.json").write_text(
        json.dumps({"launcher_version": "9.9.9"}), encoding="utf-8")
    return folder


def _hermetic_resolvers():
    from vco_lib import doctor

    def _npx(names):
        return {
            "schema_version": 1, "npx_present": True, "npx_path": "/b/npx",
            "npm_present": True,
            "commands": {n: "/b/npx" for n in names},
        }

    return doctor.DoctorResolvers(
        npx_probe=_npx,
        mcp_entries=lambda: {},
        deferral_report=lambda folder: None,
        pin_rows=lambda: [],
        disk_usage=lambda p: None,
        vco_lib_origin=lambda root: None,
        source_facts=lambda f, ask_remote: doctor.SourceFacts(),
        path_command=lambda name: None,
        kg_binding_evidence=lambda: None,
        code_embed_state=lambda root: None,
    )


def _finding(report, probe):
    return next(f for f in report.findings if f.probe == probe)


class StandaloneObtainsTests(unittest.TestCase):
    """Fake producer reachable ⇒ the probes are EVALUATED, not skipped."""

    def test_probes_consume_the_obtained_facts(self):
        """The launcher probe's inputs (git-clean dist + matching sidecar +
        no launcher process named like the fake binary) describe a healthy
        delivery; the prereqs probe consumes the envelope directly. Both must
        be EVALUATED — the prereqs probe to OK, and the launcher probe to
        something OTHER than "not evaluated" (its verdict may still be
        `unknown` on a machine whose process table cannot be scanned — that is
        the honest tri-state, and it is not the F4 defect)."""
        import tempfile

        from vco_lib import doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_fake_install_root(Path(tmp))
            # A clean git work tree, so the launcher probe's dirtiness leg
            # resolves instead of declining.
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            subprocess.run(
                ["git", "-C", str(root), "add", "-A"], check=True,
                capture_output=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t",
                 "-c", "user.name=t", "commit", "-q", "-m", "t"],
                check=True, capture_output=True)
            ctx: dict = {}
            doctor.supply_missing_install_context(root, ctx)
            self.assertIsInstance(ctx.get("bootstrap_envelope"), dict)
            self.assertIsInstance(ctx.get("launcher_probe_extras"), dict)
            report = doctor.run_doctor(
                root, resolvers=_hermetic_resolvers(), context=ctx)
            prereqs = _finding(report, "prereqs")
            self.assertEqual(prereqs.status, doctor.STATUS_OK)
            launcher = _finding(report, "launcher_binary_fresh")
            self.assertNotIn("not evaluated", launcher.summary)
            self.assertNotIn(EXPECTED_COMMAND, launcher.summary)

    def test_missing_keys_are_never_overwritten(self):
        """The install.py path injects both facts; the standalone obtain must
        not replace them (and must not even LOOK for a producer)."""
        from vco_lib import doctor

        injected = {
            "bootstrap_envelope": {"schema_version": 1, "missing_prereqs": []},
            "launcher_probe_extras": {"dist_rel_dir": "launcher/dist/x"},
        }
        with mock.patch.object(
            doctor, "_load_install_module",
            side_effect=AssertionError("producer must not be loaded"),
        ):
            ctx = doctor.supply_missing_install_context(Path("/nonexistent"), dict(injected))
        self.assertEqual(ctx, injected)

    def test_run_doctor_itself_never_loads_a_producer(self):
        """The ENGINE stays caller-supplied (hermeticity for direct callers);
        obtaining facts is the CLI preflight's job, not the probe engine's."""
        from vco_lib import doctor

        with tempfile_dir() as tmp:
            with mock.patch.object(
                doctor, "_load_install_module",
                side_effect=AssertionError("run_doctor must stay pure"),
            ):
                report = doctor.run_doctor(
                    Path(tmp), resolvers=_hermetic_resolvers())
        self.assertIsNotNone(report)


@contextlib.contextmanager
def tempfile_dir():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


class ProducerFailingTests(unittest.TestCase):
    """No producer reachable ⇒ tri-state unknown naming the exact command."""

    def test_unknown_names_the_command_in_summary_and_field(self):
        import tempfile

        from vco_lib import doctor

        with tempfile.TemporaryDirectory() as tmp:
            # A folder with NO install.py: the producer cannot be obtained.
            ctx: dict = {}
            doctor.supply_missing_install_context(Path(tmp), ctx)
            self.assertNotIn("bootstrap_envelope", ctx)
            self.assertNotIn("launcher_probe_extras", ctx)
            report = doctor.run_doctor(
                Path(tmp), resolvers=_hermetic_resolvers(), context=ctx)
            for probe in ("prereqs", "launcher_binary_fresh"):
                f = _finding(report, probe)
                self.assertEqual(
                    f.status, doctor.STATUS_UNKNOWN,
                    f"{probe} must never read as OK or absent",
                )
                # The summary is what a human reads (render_lines prints
                # `command` only for problems, so an unknown's command MUST
                # be in the text); the field is what a machine reads.
                self.assertIn(EXPECTED_COMMAND, f.summary)
                self.assertEqual(f.command, doctor.BOOTSTRAP_COMMAND)

    def test_broken_producer_is_cached_as_unavailable(self):
        """A producer that raises at import time (e.g. install.py's
        Python-version sentinel) is facts-unavailable, not a dead doctor —
        and the failed load is CACHED, so the file is executed once per
        process, not once per probe that wants its facts."""
        import tempfile

        from vco_lib import doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The module counts its own executions, then refuses to load.
            (root / "install.py").write_text(
                "import pathlib\n"
                "p = pathlib.Path(__file__).with_name('loads.txt')\n"
                "n = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n + 1))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            ctx: dict = {}
            doctor.supply_missing_install_context(root, ctx)
            doctor.supply_missing_install_context(root, ctx)
            self.assertEqual(ctx, {})
            self.assertEqual(
                (root / "loads.txt").read_text(encoding="utf-8"), "1",
                "a failed producer load must be cached, not re-executed",
            )


class StandaloneRunAndReportTests(unittest.TestCase):
    """The standalone CLI path (run_and_report with no sink) prefills the
    context the probes then consume — the in-run path (sink) is untouched."""

    def test_standalone_prefills_and_the_probes_are_evaluated(self):
        import tempfile

        from vco_lib import doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_fake_install_root(Path(tmp))
            seen: dict = {}
            real_supply = doctor.supply_missing_install_context

            def _spy(folder, ctx):
                seen["folder"] = Path(folder)
                return real_supply(Path(folder), ctx)

            with mock.patch.object(doctor, "supply_missing_install_context",
                                   side_effect=_spy):
                report = doctor.run_and_report(
                    root, resolvers=_hermetic_resolvers(),
                    printer=lambda _l: None, auto_fix=False,
                )
            self.assertEqual(seen.get("folder"), root)
            # The probes consumed the obtained facts: prereqs evaluated to OK
            # (an unobtainable envelope would have been the unknown + command).
            self.assertEqual(
                _finding(report, "prereqs").status, doctor.STATUS_OK)

    def test_sink_path_does_not_obtain(self):
        """install.py injects its own envelope mid-run; the doctor's sink path
        must not load a producer on top of that (one home, one writer)."""
        import tempfile

        from vco_lib import doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_fake_install_root(Path(tmp))

            class _Sink:
                def __init__(self):
                    self.entries = []

                def add_entry(self, entry):
                    self.entries.append(entry)

            with mock.patch.object(
                doctor, "_load_install_module",
                side_effect=AssertionError("sink path must not load a producer"),
            ):
                doctor.run_and_report(
                    root, sink=_Sink(), resolvers=_hermetic_resolvers(),
                    printer=lambda _l: None, auto_fix=False,
                )


class BootstrapCommandIsRealTests(unittest.TestCase):
    """`doctor.BOOTSTRAP_COMMAND` is a printed command, and a printed command
    is shipped code: prove the argv answers with the JSON envelope the
    probes consume, by driving the REAL install.py's bootstrap dispatch."""

    def test_the_command_is_a_real_cli_printing_the_envelope(self):
        spec = importlib.util.spec_from_file_location(
            "install_for_v0295_f4_tests", REPO_ROOT / "install.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = module._run_bootstrap(["--bootstrap", "--json"])
        self.assertEqual(rc, 0)
        envelope = json.loads(out.getvalue())
        self.assertIsInstance(envelope, dict)
        self.assertIn("missing_prereqs", envelope)

    def test_the_printed_string_matches_the_module_constant(self):
        from vco_lib import doctor

        self.assertEqual(doctor.BOOTSTRAP_COMMAND, EXPECTED_COMMAND)


if __name__ == "__main__":
    unittest.main()
