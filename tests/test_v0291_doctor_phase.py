# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D — the doctor phase.

The failure class this closes: VCO probed its environment once, at install
time, and never re-verified those assumptions against what it had actually
registered. ``missing_prereqs`` was computed and dropped on the floor; a
missing npx produced a reassurance that was FALSE; the launcher badge was
structurally blind to the whole class. Every test here pins a piece of that
inversion.

Hermeticity is the design constraint, not a nicety: the whole engine runs off
:class:`DoctorResolvers`, so a complete fake machine is three lambdas. No
network, no ``~/.claude.json``, no subprocess, no live service.
"""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor  # noqa: E402


class _PinRow:
    def __init__(self, key, pinned, installed, status):
        self.key, self.pinned, self.installed, self.status = (
            key, pinned, installed, status,
        )


def _resolvers(*, npx=None, servers=None, report=None, pins=()):
    """A whole fake machine."""
    npx_payload = npx if npx is not None else {
        "schema_version": 1, "npx_present": True, "npx_path": "/b/npx",
        "npm_present": True, "commands": {"npx": "/b/npx", "npm": "/b/npm"},
    }

    def _npx(names):
        payload = dict(npx_payload)
        commands = dict(payload.get("commands") or {})
        for n in names:
            commands.setdefault(n, None)
        payload["commands"] = commands
        return payload

    return doctor.DoctorResolvers(
        npx_probe=_npx,
        mcp_entries=lambda: dict(servers or {}),
        deferral_report=lambda folder: report,
        # Never shell out to npm from a unit test (hermeticity): the default
        # resolver would run `npm list -g` and make the verdict depend on the
        # developer's global node_modules.
        pin_rows=lambda: list(pins),
    )


class _FakeEntry:
    def __init__(self, cid):
        self.condition_id = cid
        self.title = cid
        self.detected = ""
        self.command_to_apply = ""
        self.severity = "warning"


class _FakeReport:
    def __init__(self, cids):
        self.entries = [_FakeEntry(c) for c in cids]


class NpxProbeTests(unittest.TestCase):
    def test_missing_npx_is_a_problem_with_the_registry_cid(self):
        res = _resolvers(
            npx={"npx_present": False, "npx_path": "", "npm_present": True,
                 "commands": {"npx": None, "npm": "/b/npm"}},
            servers={"playwright": {"command": "npx", "args": ["-y", "@x/y"]}},
        )
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        npx = next(f for f in report.findings if f.probe == "npx_resolvable")
        self.assertEqual(npx.status, doctor.STATUS_PROBLEM)
        self.assertEqual(npx.condition_id, doctor.CID_NPX_MISSING)
        self.assertEqual(npx.fix, doctor.FIX_DEFER, "installing Node is never automatic")
        self.assertIn("npm", npx.summary, "the npm-present branch must be named")
        self.assertFalse(report.ok)

    def test_present_npx_is_ok_and_emits_nothing(self):
        res = _resolvers(
            servers={"playwright": {"command": "npx", "args": ["-y", "@x/y"]}},
        )
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        self.assertEqual(
            next(f for f in report.findings if f.probe == "npx_resolvable").status,
            doctor.STATUS_OK,
        )
        self.assertEqual(doctor.deferral_entries_for(report), [])

    def test_unspawnable_entries_are_named(self):
        res = _resolvers(
            npx={"npx_present": False, "npx_path": "", "npm_present": False,
                 "commands": {"npx": None, "npm": None}},
            servers={
                "playwright": {"command": "npx", "args": ["-y", "@x/y"]},
                "mermaid": {"command": "npx", "args": ["-y", "m"]},
                "weaviate-kg": {"command": "/opt/vco/.venv/bin/python"},
            },
        )
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "mcp_commands_spawnable")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn("playwright", f.summary)
        self.assertIn("mermaid", f.summary)
        self.assertNotIn(
            "weaviate-kg", f.summary,
            "a path-shaped command is not a PATH-resolution question",
        )
        self.assertIn("Node.js", f.command)

    def test_no_bare_commands_is_unknown_not_ok(self):
        """Positive evidence only: an empty ~/.claude.json means we learned
        nothing, and "nothing to check" must not read as "all good"."""
        res = _resolvers(servers={})
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "mcp_commands_spawnable")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)

    def test_unknown_findings_do_not_fail_the_report(self):
        res = _resolvers(servers={})
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        self.assertTrue(report.unknowns)
        self.assertTrue(report.ok, "'could not check' is not 'broken'")


class CommandClassificationTests(unittest.TestCase):
    def test_command_is_path_shapes(self):
        # Must agree with maintenance.rs::command_is_path (same question, two
        # surfaces) — the Rust half has the mirroring test.
        for cmd in ("/usr/bin/python", "C:\\py\\python.exe",
                    "\\\\srv\\share\\python.exe", "./rel/python", "dir/python"):
            self.assertTrue(doctor.command_is_path(cmd), cmd)
        for cmd in ("npx", "node", "python", ""):
            self.assertFalse(doctor.command_is_path(cmd), cmd)

    def test_bare_command_names_dedupe_and_sort(self):
        servers = {
            "a": {"command": "npx"}, "b": {"command": "npx"},
            "c": {"command": "node"}, "d": {"command": "/abs/python"},
            "e": {}, "f": "not-a-dict",
        }
        self.assertEqual(doctor.bare_command_names(servers), ["node", "npx"])


class LedgerProbeTests(unittest.TestCase):
    def test_splits_actionable_from_records(self):
        res = _resolvers(report=_FakeReport([
            "kg_access_phantom_repaired",     # informational_record
            "mcp_registration_failed",        # action_required
        ]))
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "deferral_ledger")
        self.assertEqual(f.detail["actionable"], ["mcp_registration_failed"])
        self.assertEqual(f.detail["informational"], ["kg_access_phantom_repaired"])

    def test_owed_retryable_work_is_auto_fix(self):
        res = _resolvers(report=_FakeReport(["kg_sync_no_embedding_backend"]))
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "owed_retryable_work")
        self.assertEqual(f.fix, doctor.FIX_AUTO)
        self.assertEqual(f.detail["condition_ids"], ["kg_sync_no_embedding_backend"])

    def test_no_retryable_work_no_finding(self):
        res = _resolvers(report=_FakeReport(["mcp_registration_failed"]))
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        self.assertFalse(
            [f for f in report.findings if f.probe == "owed_retryable_work"],
        )

    def test_empty_ledger_is_ok(self):
        res = _resolvers(report=_FakeReport([]))
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "deferral_ledger")
        self.assertEqual(f.status, doctor.STATUS_OK)

    def test_a_capped_condition_is_not_promised_as_auto_fixable(self):
        """v0.2.91 wave-3 (NIT): the finding says "VCO can retry this
        itself". Once the attempt cap is spent the dispatcher SKIPS the
        condition, so listing it is a promise the next dispatch will not
        keep — and it is exactly the case where the user needs to know the
        entry is now ordinary manual work."""
        import json
        import tempfile

        from vco_lib import deferral_retry

        cid = "kg_sync_no_embedding_backend"
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            path = deferral_retry.attempts_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(
                    json.dumps({"condition_id": cid,
                                "status": deferral_retry.STARTED}) + "\n"
                    for _ in range(deferral_retry.MAX_ATTEMPTS)
                ),
                encoding="utf-8",
            )
            res = _resolvers(report=_FakeReport([cid]))
            report = doctor.run_doctor(folder, resolvers=res)
        self.assertFalse(
            [f for f in report.findings if f.probe == "owed_retryable_work"],
            "a cap-exhausted condition must not be promised as auto-fixable",
        )
        # …and it is still surfaced by the ledger finding itself.
        f = next(f for f in report.findings if f.probe == "deferral_ledger")
        self.assertIn(cid, f.detail["actionable"] + f.detail["informational"])

    def test_an_uncapped_condition_is_still_promised(self):
        """LEAVE-ALONE half — one attempt short of the cap still lists."""
        import json
        import tempfile

        from vco_lib import deferral_retry

        cid = "kg_sync_no_embedding_backend"
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            path = deferral_retry.attempts_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(
                    json.dumps({"condition_id": cid,
                                "status": deferral_retry.STARTED}) + "\n"
                    for _ in range(deferral_retry.MAX_ATTEMPTS - 1)
                ),
                encoding="utf-8",
            )
            res = _resolvers(report=_FakeReport([cid]))
            report = doctor.run_doctor(folder, resolvers=res)
        f = next(f for f in report.findings if f.probe == "owed_retryable_work")
        self.assertEqual(f.detail["condition_ids"], [cid])


class PrereqProbeTests(unittest.TestCase):
    """The consumer report 6 §B.1 says was missing."""

    def test_blocking_prereq_becomes_a_finding(self):
        res = _resolvers()
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=res,
            context={"bootstrap_envelope": {"missing_prereqs": [
                {"name": "node", "severity": "blocking",
                 "install_hint": "install node"},
                {"name": "lean_ctx", "severity": "optional"},
            ]}},
        )
        f = next(f for f in report.findings if f.probe == "prereqs")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn("node", f.summary)
        self.assertIn("install node", f.command)

    def test_optional_prereqs_alone_are_ok(self):
        report = doctor.run_doctor(
            Path("/tmp/x"), resolvers=_resolvers(),
            context={"bootstrap_envelope": {"missing_prereqs": [
                {"name": "lean_ctx", "severity": "optional"},
            ]}},
        )
        f = next(f for f in report.findings if f.probe == "prereqs")
        self.assertEqual(f.status, doctor.STATUS_OK)

    def test_absent_envelope_is_unknown(self):
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=_resolvers())
        f = next(f for f in report.findings if f.probe == "prereqs")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)


class NpmPinProbeTests(unittest.TestCase):
    def test_drift_is_a_problem(self):
        res = _resolvers(pins=[_PinRow("mermaid_mcp", "1.6.3", "1.4.2", "drift")])
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "npm_pins")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertIn("1.4.2", f.summary)
        self.assertEqual(f.command, "vco verify-pins --fix")

    def test_missing_is_not_a_problem(self):
        """A bundled npm package can be legitimately absent (opt-out env var,
        `file:` pin, default-disabled diagram MCP). Calling that a problem
        would make the doctor cry wolf on a healthy machine."""
        res = _resolvers(pins=[
            _PinRow("mermaid_mcp", "1.6.3", "1.6.3", "match"),
            _PinRow("mermaid_lib", "11.15.0", None, "missing"),
        ])
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "npm_pins")
        self.assertEqual(f.status, doctor.STATUS_OK)
        self.assertEqual(f.detail["missing"], ["mermaid_lib"])

    def test_no_npm_is_unknown(self):
        res = doctor.DoctorResolvers(
            npx_probe=lambda names: {"npx_present": True, "npx_path": "/b/npx",
                                     "npm_present": True, "commands": {}},
            mcp_entries=dict,
            deferral_report=lambda f: None,
            pin_rows=lambda: None,
        )
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=res)
        f = next(f for f in report.findings if f.probe == "npm_pins")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)

    def test_real_manifest_has_no_phantom_pin_rows(self):
        """RED on c67ef888: `cmd_verify_pins` fed `_collect_pin_rows` the WHOLE
        bundled-versions document, so the top-level `npm` / `chromium` TABLE
        NAMES became pin rows with `<missing-package-for-…>` placeholders,
        classified as drift. `vco verify-pins` could therefore never exit 0 on
        a real install, and `--fix` would have called the installer with those
        non-keys. Every existing test stubbed the FLAT shape production does
        not produce, so the mocks all agreed with each other and none agreed
        with reality."""
        from vco_lib.cli import verify

        section = verify._npm_pin_section(verify._load_bundled_versions())
        self.assertIn("mermaid_mcp", section)
        self.assertNotIn("chromium", section)
        rows = verify._collect_pin_rows(section, npm_path=None)
        for row in rows:
            self.assertNotIn("<missing-package-for-", row.package)
            self.assertEqual(row.status, "npm-not-available")

    def test_verify_pins_can_exit_zero_on_the_real_manifest(self):
        """The same defect stated BEHAVIOURALLY, so it red-proofs without
        naming the new helper: with every real pin installed at its pinned
        version, ``vco verify-pins`` must exit 0.

        RED on c67ef888 — it returned EXIT_DRIFT no matter what, because the
        two phantom rows (`npm`, `chromium`) were always classified as drift.
        """
        import argparse as _argparse

        from vco_lib.bundled_versions import load_bundled_versions
        from vco_lib.cli import verify

        wanted = {
            spec["package"]: spec["version"]
            for spec in load_bundled_versions()["npm"].values()
        }
        with mock.patch.object(
            verify, "_which", lambda tool: "/usr/bin/npm" if tool == "npm" else None,
        ), mock.patch.object(
            verify, "_npm_view_version",
            side_effect=lambda package, npm_path: wanted.get(package),
        ):
            code = verify.cmd_verify_pins(
                _argparse.Namespace(json=True, fix=False),
            )
        self.assertEqual(code, verify.EXIT_OK)

    def test_flat_fixture_shape_still_accepted(self):
        """The stubbed shape every existing verify-pins test uses must keep
        working — the fix widens the accepted input, it does not narrow it."""
        from vco_lib.cli import verify

        flat = {"mermaid_mcp": {"package": "claude-mermaid", "version": "1.6.3"}}
        self.assertEqual(verify._npm_pin_section(flat), flat)


class LauncherFreshnessProbeTests(unittest.TestCase):
    def test_without_extras_the_probe_declines(self):
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=_resolvers())
        f = next(f for f in report.findings if f.probe == "launcher_binary_fresh")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)

    def test_stale_binary_reports_but_never_auto_fixes(self):
        """Decision #4: anything touching a RUNNING binary is surface-only."""
        with mock.patch("vco_lib.deferral_probes.run_probe", return_value=True):
            report = doctor.run_doctor(
                Path("/tmp/x"), resolvers=_resolvers(),
                context={"launcher_probe_extras": {"dist_rel_dir": "d",
                                                   "launcher_binary_name": "b",
                                                   "source_version": "1.0"}},
            )
        f = next(f for f in report.findings if f.probe == "launcher_binary_fresh")
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertEqual(f.fix, doctor.FIX_DEFER)
        self.assertEqual(f.condition_id, "launcher_binary_stale")

    def test_fresh_binary_is_ok(self):
        with mock.patch("vco_lib.deferral_probes.run_probe", return_value=False):
            report = doctor.run_doctor(
                Path("/tmp/x"), resolvers=_resolvers(),
                context={"launcher_probe_extras": {"dist_rel_dir": "d",
                                                   "launcher_binary_name": "b",
                                                   "source_version": "1.0"}},
            )
        f = next(f for f in report.findings if f.probe == "launcher_binary_fresh")
        self.assertEqual(f.status, doctor.STATUS_OK)


class EmissionTests(unittest.TestCase):
    def _problem_report(self):
        res = _resolvers(
            npx={"npx_present": False, "npx_path": "", "npm_present": False,
                 "commands": {"npx": None, "npm": None}},
            servers={"playwright": {"command": "npx"}},
        )
        return doctor.run_doctor(Path("/tmp/x"), resolvers=res)

    def test_one_entry_per_condition_not_per_finding(self):
        """Two findings carry CID_NPX_MISSING (the npx probe and the
        per-entry one); the ledger must receive ONE entry."""
        entries = doctor.deferral_entries_for(self._problem_report())
        self.assertEqual([e.condition_id for e in entries],
                         [doctor.CID_NPX_MISSING])

    def test_never_re_emits_another_owners_condition(self):
        """`launcher_binary_stale` has an owner (WP-A) with its own emit
        latch and clear probe. The doctor REPORTS it; re-emitting would fork
        its lifecycle."""
        with mock.patch("vco_lib.deferral_probes.run_probe", return_value=True):
            report = doctor.run_doctor(
                Path("/tmp/x"), resolvers=_resolvers(),
                context={"launcher_probe_extras": {"dist_rel_dir": "d",
                                                   "launcher_binary_name": "b",
                                                   "source_version": "1.0"}},
            )
        self.assertEqual(doctor.deferral_entries_for(report), [])

    def test_sink_receives_entries_instead_of_the_locked_writer(self):
        """install.py's in-flight run report is the sink, so the run's single
        authoritative write carries the entry — never a second writer behind
        finalize()'s back."""
        class _Sink:
            def __init__(self):
                self.added = []

            def add_entry(self, e):
                self.added.append(e)

        sink = _Sink()
        cids = doctor.emit_findings(Path("/tmp/x"), self._problem_report(), sink=sink)
        self.assertEqual(cids, [doctor.CID_NPX_MISSING])
        self.assertEqual([e.condition_id for e in sink.added], cids)

    def test_no_sink_writes_through_the_locked_emitter(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            doctor.emit_findings(folder, self._problem_report())
            md = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(md.is_file())
            self.assertIn(doctor.CID_NPX_MISSING, md.read_text(encoding="utf-8"))


class ReportRenderingTests(unittest.TestCase):
    def test_json_payload_is_stable_and_complete(self):
        report = doctor.run_doctor(Path("/tmp/x"), resolvers=_resolvers())
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["schema_version"], doctor.SCHEMA_VERSION)
        self.assertEqual(payload["scope"], doctor.SCOPE_FULL)
        self.assertEqual(len(payload["findings"]), len(report.findings))
        for f in payload["findings"]:
            self.assertIn(f["status"], (doctor.STATUS_OK, doctor.STATUS_PROBLEM,
                                        doctor.STATUS_UNKNOWN))

    def test_problems_render_first(self):
        res = _resolvers(
            npx={"npx_present": False, "npx_path": "", "npm_present": False,
                 "commands": {"npx": None, "npm": None}},
            servers={"playwright": {"command": "npx"}},
        )
        lines = doctor.run_doctor(Path("/tmp/x"), resolvers=res).render_lines()
        self.assertTrue(lines[0].strip().startswith("[ !]"))


class ScopeTests(unittest.TestCase):
    def test_boot_scope_skips_the_subprocess_probes(self):
        boot = {pid for pid, (_fn, scopes) in doctor.PROBES.items()
                if doctor.SCOPE_BOOT in scopes}
        self.assertNotIn("npm_pins", boot, "npm ls is not a boot-path cost")
        self.assertNotIn("prereqs", boot, "needs install.py's envelope")
        self.assertIn("mcp_commands_spawnable", boot)

    def test_boot_scope_leaves_binary_freshness_to_the_launcher(self):
        """At boot the launcher runs its own Rust freshness probe, which reads
        the RUNNING process's compiled-in version — the input the Python leg
        structurally cannot see. Two implementations answering one question
        from different evidence would eventually contradict each other."""
        boot = {pid for pid, (_fn, scopes) in doctor.PROBES.items()
                if doctor.SCOPE_BOOT in scopes}
        self.assertNotIn("launcher_binary_fresh", boot)
        full = {pid for pid, (_fn, scopes) in doctor.PROBES.items()
                if doctor.SCOPE_FULL in scopes}
        self.assertIn("launcher_binary_fresh", full)

    def test_boot_report_only_runs_boot_probes(self):
        report = doctor.run_doctor(
            Path("/tmp/x"), scope=doctor.SCOPE_BOOT, resolvers=_resolvers(),
        )
        self.assertFalse([f for f in report.findings if f.probe == "npm_pins"])


class ProbeIsolationTests(unittest.TestCase):
    def test_a_raising_probe_degrades_to_unknown(self):
        """The doctor is layered ON TOP of a run that already succeeded — it
        must never be able to fail it."""
        def _boom(folder, res, ctx):
            raise RuntimeError("probe exploded")

        with mock.patch.dict(doctor.PROBES,
                             {"deferral_ledger": (_boom, (doctor.SCOPE_FULL,))}):
            report = doctor.run_doctor(Path("/tmp/x"), resolvers=_resolvers())
        f = next(f for f in report.findings if f.probe == "deferral_ledger")
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertIn("probe exploded", f.summary)


class CliContractTests(unittest.TestCase):
    def _args(self, **over):
        base = dict(folder=Path("/tmp/x"), json=False, scope=doctor.SCOPE_FULL,
                    auto_fix=True, emit=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_exit_code_reflects_problems_only(self):
        clean = doctor.DoctorReport(folder=Path("/tmp/x"), scope="full", findings=[
            doctor.Finding(probe="p", status=doctor.STATUS_UNKNOWN, summary=""),
        ])
        broken = doctor.DoctorReport(folder=Path("/tmp/x"), scope="full", findings=[
            doctor.Finding(probe="p", status=doctor.STATUS_PROBLEM, summary=""),
        ])
        with mock.patch.object(doctor, "run_doctor", return_value=clean), \
             mock.patch.object(doctor, "emit_findings", return_value=[]):
            self.assertEqual(doctor.run_from_args(self._args(json=True)), 0)
        with mock.patch.object(doctor, "run_doctor", return_value=broken), \
             mock.patch.object(doctor, "emit_findings", return_value=[]):
            self.assertEqual(doctor.run_from_args(self._args(json=True)), 1)

    def test_no_emit_also_disarms_the_retry(self):
        """--no-emit means "tell me, change nothing". A retry is a BIGGER side
        effect than the ledger write, so the quieter flag must not leave it
        armed."""
        seen = {}

        def _fake_run_and_report(folder, **kw):
            seen.update(kw)
            return doctor.DoctorReport(folder=folder, scope="full")

        with mock.patch.object(doctor, "run_and_report",
                               side_effect=_fake_run_and_report):
            doctor.run_from_args(self._args(emit=False))
        self.assertFalse(seen["auto_fix"])
        self.assertFalse(seen["emit"])

    def test_vco_cli_registers_the_subcommand(self):
        from vco_lib.cli import __main__ as cli_main

        parser = cli_main._build_parser()
        args = parser.parse_args(["doctor", "--json"])
        self.assertTrue(args.json)
        self.assertTrue(callable(args.func))


class InstallPyWiringTests(unittest.TestCase):
    """The install/update invocation point — source-scanned.

    A behavioural test would have to run a full install; the wiring facts
    (phase called from main(), re-probe still update-only, doctor sinks into
    the run report, and the phase sits BEFORE the single final write) are
    exactly what a regression would break.
    """

    def setUp(self):
        self.src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_main_calls_the_phase(self):
        self.assertIn(
            "_post_install_probe_phase(_deferral_report, _deferral_folder, args=args)",
            self.src,
        )

    def test_phase_keeps_reprobe_update_only_and_doctor_unconditional(self):
        body = self.src[self.src.index("def _post_install_probe_phase"):]
        body = body[: body.index("def _run_doctor_phase")]
        self.assertIn('if getattr(args, "update", False):', body)
        self.assertIn("_apply_deferred_entries(report, folder, args=args)", body)
        # The doctor call must NOT be inside the update guard.
        doctor_off = body.index("_run_doctor_phase(report, folder, args=args)")
        guard_off = body.index('if getattr(args, "update", False):')
        self.assertGreater(doctor_off, guard_off)
        # …and it must sit at the function's own indent level, not nested
        # under the guard. v0.2.91 wave-3 (NIT): the previous assertion
        # compared a 4-character slice against an 8-character prefix, so it
        # was VACUOUSLY true and could never have caught the regression it
        # names. Compare the call line's actual indent instead.
        call_line = next(
            ln for ln in body.splitlines()
            if "_run_doctor_phase(report, folder, args=args)" in ln
        )
        indent = len(call_line) - len(call_line.lstrip(" "))
        self.assertEqual(
            indent, 4,
            "the doctor call must run on fresh installs too — an indent "
            f"deeper than 4 means it was nested under a guard: {call_line!r}",
        )

    def test_doctor_emits_into_the_run_report_not_a_second_writer(self):
        body = self.src[self.src.index("def _run_doctor_phase"):]
        body = body[: body.index("# ---", 10)]
        self.assertIn("sink=report", body)

    def test_install_time_doctor_never_dispatches_a_retry(self):
        """LEAVE-ALONE leg of the retry trigger.

        A retry resolves its condition through the locked emitter while this
        run's `finalize()` is still pending — a resolve landing between that
        read and its write would be resurrected by our own final write. It
        would also repeat the seed step 7c just ran, and block the install on
        work measured in minutes. The session-start check and `vco doctor`
        dispatch instead.
        """
        body = self.src[self.src.index("def _run_doctor_phase"):]
        body = body[: body.index("# ---", 10)]
        self.assertIn("auto_fix=False", body)

    def test_phase_runs_before_the_single_authoritative_write(self):
        call = self.src.index("_post_install_probe_phase(_deferral_report")
        finalize = self.src.index("_final = _deferral_flow.finalize()")
        self.assertLess(
            call, finalize,
            "doctor entries must ride the run's own final write",
        )


if __name__ == "__main__":
    unittest.main()
