# SPDX-License-Identifier: AGPL-3.0-or-later
# v0.2.95 lane F1 — `hub_restart_failed_after_abort` never cleared.
#
# The dogfood defect (PLAN-v0295 §"FOUND IN THE 0.2.94 DOGFOOD", F1): the
# entry's ONLY resolver was a hand-written branch inside install.py's
# `_apply_deferred_entries`, and the re-probe phase that runs it sat BEFORE
# the hub-restart step of the same update — so the branch's "is the hub up
# now?" question could never be answered Yes in the run that restarted the
# hub. Standalone `vco doctor` never touched foreign cids at all. Result: a
# row that survived hub recovery + two updates + a doctor pass, forever.
#
# The fix, pinned here end to end:
#
# * a STATE-keyed registry probe (`hub_back_after_restart_failure`) that
#   re-derives "is the hub answering /api/v1/health on the resolved port"
#   every time it runs (R26: no "this release fixed it" clears);
# * the probe declared in `deferral_conditions.toml`, so the generic
#   probe-first dispatch settles it wherever a re-probe pass runs;
# * the install.py pre-restart hand-written branch REMOVED (arch review §7:
#   collapse the duplicate deciders — with a declared probe it was unreachable
#   dead code whose comments promised a clear it could never deliver);
# * install.py's re-probe phase moved AFTER `_deploy_and_start_vct_hub`, and
#   the doctor's reconcile pass (`vco_lib.doctor.reconcile_probe_cleared`,
#   which runs LAST — the doctor is the reconciler's OBSERVE step) now clears
#   the row on the standalone surface too.
#
# Hermeticity: every hub in these tests is a local `http.server` on an
# ephemeral port, pinned through `VCT_HUB_PORT` (the ONE resolution chain,
# `vco_lib.access_resolver._hub_port`); nothing starts or probes the real
# hub, and no install/update is executed against the machine.

import ast
import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

CID = "hub_restart_failed_after_abort"
PROBE_NAME = "hub_back_after_restart_failure"


def _load_install():
    """Import install.py without running main() (it guards on __main__)."""
    spec = importlib.util.spec_from_file_location(
        "install_for_v0295_f1_tests", REPO_ROOT / "install.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(cid: str):
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=cid, title=f"t {cid}", detected="d",
        why_deferred="w", command_to_apply="c", severity="warning",
    )


def _persist(folder: Path, *entries):
    from vco_lib.deferral_report import DeferralReport

    report = DeferralReport.read(folder)
    for e in entries:
        report.add_entry(e)
    report.write(folder)


def _ledger_cids(folder: Path):
    from vco_lib.deferral_report import DeferralReport

    return [e.condition_id for e in DeferralReport.read(folder).entries]


def _audit_rows(folder: Path):
    target = folder / ".claude" / "logs" / "auto-resolutions.jsonl"
    if not target.is_file():
        return []
    return [json.loads(ln) for ln in target.read_text(encoding="utf-8").splitlines() if ln]


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers every GET the way the hub answers `/api/v1/health`."""

    def do_GET(self):  # noqa: N802 — http.server API
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — base API name
        """Silence — test output is for assertions, not request logs."""
        pass


class _HubUp:
    """Context manager: a local hub answering /health on an ephemeral port."""

    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._patcher = mock.patch.dict(
            os.environ, {"VCT_HUB_PORT": str(self.port)}
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        return False


def _closed_port() -> int:
    """A port that is provably not listening (bind, read port, close)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProbeTriStateTests(unittest.TestCase):
    """The probe is STATE-keyed: it asks the machine now, every time."""

    def test_hub_answering_health_means_condition_over(self):
        from vco_lib import deferral_probes

        with _HubUp():
            verdict = deferral_probes.run_probe(
                PROBE_NAME,
                deferral_probes.ProbeContext(folder=Path("/tmp/x"), entry=None, extras={}),
            )
        self.assertIs(verdict, False)  # False = provably over ⇒ clear

    def test_nothing_answering_means_condition_still_applies(self):
        from vco_lib import deferral_probes

        port = _closed_port()
        with mock.patch.dict(os.environ, {"VCT_HUB_PORT": str(port)}):
            verdict = deferral_probes.run_probe(
                PROBE_NAME,
                deferral_probes.ProbeContext(folder=Path("/tmp/x"), entry=None, extras={}),
            )
        self.assertIs(verdict, True)  # True = still applies ⇒ keep

    def test_port_resolution_failure_is_not_a_verdict(self):
        """Could-not-look must degrade to None (keep), never a clear."""
        from vco_lib import access_resolver, deferral_probes

        def _boom():
            raise RuntimeError("no state dir in this universe")

        with mock.patch.object(access_resolver, "_hub_port", _boom):
            verdict = deferral_probes.run_probe(
                PROBE_NAME,
                deferral_probes.ProbeContext(folder=Path("/tmp/x"), entry=None, extras={}),
            )
        self.assertIsNone(verdict)


class RegistryDeclarationTests(unittest.TestCase):
    """The lifecycle is DECLARED, not hand-written: toml row + PROBES entry."""

    def test_condition_declares_the_state_keyed_probe(self):
        toml = (REPO_ROOT / "vco_lib" / "deferral_conditions.toml").read_text(
            encoding="utf-8")
        block = toml[toml.index(f"[conditions.{CID}]"):]
        block = block[: block.index("[conditions.", 10)]
        self.assertIn('class = "action_required"', block)
        self.assertIn(f'clear_probe = "probe:py:{PROBE_NAME}"', block)

    def test_probe_is_registered(self):
        from vco_lib import deferral_probes

        self.assertIn(PROBE_NAME, deferral_probes.PROBES)

    def test_the_old_pre_restart_resolver_branch_is_gone(self):
        """The install.py elif could never fire (probe-first dispatch makes it
        dead code) and its clear condition ANDed a sidecar version the probe
        deliberately dropped — collapse, per the arch review."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertNotIn(f'elif cid == "{CID}":', src)


class ReconcileTests(unittest.TestCase):
    """`doctor.reconcile_probe_cleared` — act / leave-alone / no-op."""

    def test_row_with_hub_up_is_cleared_with_an_audit_row(self):
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            with _HubUp():
                lines: list = []
                cleared = doctor.reconcile_probe_cleared(folder, log=lines.append)
            self.assertEqual(cleared, [CID])
            self.assertNotIn(CID, _ledger_cids(folder))
            rows = _audit_rows(folder)
            self.assertTrue(any(
                r.get("condition_id") == CID
                and r.get("action") == "resolved_by_registry_probe"
                and PROBE_NAME in str(r.get("detail", ""))
                for r in rows
            ))
            self.assertTrue(any(CID in ln for ln in lines))

    def test_row_with_hub_down_is_kept(self):
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            port = _closed_port()
            with mock.patch.dict(os.environ, {"VCT_HUB_PORT": str(port)}):
                cleared = doctor.reconcile_probe_cleared(folder, log=lambda _l: None)
            self.assertEqual(cleared, [])
            self.assertIn(CID, _ledger_cids(folder))
            self.assertEqual(_audit_rows(folder), [])

    def test_no_row_writes_nothing(self):
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with _HubUp():
                cleared = doctor.reconcile_probe_cleared(folder, log=lambda _l: None)
            self.assertEqual(cleared, [])
            self.assertFalse((folder / ".claude").exists())


def _hermetic_resolvers(folder: Path):
    """A full fake machine — no npm, no git, no Weaviate, no hub, no disk."""
    from vco_lib import doctor

    def _npx(names):
        return {
            "schema_version": 1, "npx_present": True, "npx_path": "/b/npx",
            "npm_present": True,
            "commands": {n: "/b/npx" for n in names},
        }

    from vco_lib.deferral_report import DeferralReport

    return doctor.DoctorResolvers(
        npx_probe=_npx,
        mcp_entries=lambda: {},
        deferral_report=lambda f: DeferralReport.read(Path(f)),
        pin_rows=lambda: [],
        disk_usage=lambda p: None,
        vco_lib_origin=lambda root: None,
        source_facts=lambda f, ask_remote: doctor.SourceFacts(),
        path_command=lambda name: None,
        kg_binding_evidence=lambda: None,
        code_embed_state=lambda root: None,
        gateway_state=None,
    )


class _Sink:
    """install.py's in-flight run report stand-in (the `sink=` seam)."""

    def __init__(self):
        self.entries = []

    def add_entry(self, entry):
        self.entries.append(entry)


class RunAndReportReconcileTests(unittest.TestCase):
    """The standalone doctor clears the row; the in-run (sink) doctor does not."""

    def test_standalone_pass_clears_the_row_before_printing(self):
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            with _HubUp():
                report = doctor.run_and_report(
                    folder,
                    resolvers=_hermetic_resolvers(folder),
                    printer=lambda _l: None,
                    auto_fix=False,
                )
            self.assertNotIn(CID, _ledger_cids(folder))
            self.assertTrue(any(
                r.get("condition_id") == CID
                and r.get("action") == "resolved_by_registry_probe"
                for r in _audit_rows(folder)
            ))
            # The SAME pass's ledger probe must describe the reconciled
            # ledger, not an entry it cleared moments earlier.
            ledger = next(
                f for f in report.findings if f.probe == "deferral_ledger")
            self.assertEqual(ledger.status, doctor.STATUS_OK)

    def test_no_emit_leaves_the_ledger_alone(self):
        """"--no-emit means tell me, change nothing" — the reconcile is a
        write, so it stays armed off."""
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            with _HubUp():
                doctor.run_and_report(
                    folder,
                    resolvers=_hermetic_resolvers(folder),
                    printer=lambda _l: None,
                    auto_fix=False,
                    emit=False,
                )
            self.assertIn(CID, _ledger_cids(folder))
            self.assertEqual(_audit_rows(folder), [])

    def test_sink_present_does_not_reconcile(self):
        """In-run the install's own re-probe pass owns the clears (one writer
        per run); the doctor's sink path must not race finalize()."""
        from vco_lib import doctor

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            sink = _Sink()
            with _HubUp():
                doctor.run_and_report(
                    folder, sink=sink,
                    resolvers=_hermetic_resolvers(folder),
                    printer=lambda _l: None,
                    auto_fix=False,
                )
            self.assertIn(CID, _ledger_cids(folder))
            self.assertEqual(_audit_rows(folder), [])


class InstallOrderingTests(unittest.TestCase):
    """The phase must run AFTER the hub restart — comments cannot satisfy
    this; it compares AST Call nodes inside main() itself."""

    def test_reprobe_phase_runs_after_the_hub_restart(self):
        tree = ast.parse(
            (REPO_ROOT / "install.py").read_text(encoding="utf-8"))
        main_fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        lines: dict = {}
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                lines.setdefault(node.func.id, []).append(node.lineno)
        self.assertIn("_post_install_probe_phase", lines,
                      "main() must still call the re-probe/doctor phase")
        self.assertIn("_deploy_and_start_vct_hub", lines,
                      "main() must still start the hub")
        self.assertGreater(
            min(lines["_post_install_probe_phase"]),
            max(lines["_deploy_and_start_vct_hub"]),
            "the re-probe pass that can clear hub_restart_failed_after_abort "
            "must run AFTER the hub restart step of the same run — before it, "
            "the F1 branch could never see the hub it had just restarted",
        )


class InstallDispatchTests(unittest.TestCase):
    """`_apply_deferred_entries` settles the cid through the registry probe
    (probe-first dispatch), with the REAL probe against a local hub."""

    @classmethod
    def setUpClass(cls):
        cls.install = _load_install()

    def test_registry_probe_resolves_the_entry(self):
        from vco_lib.deferral_report import DeferralReport

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _persist(folder, _entry(CID))
            run_report = DeferralReport()
            with _HubUp():
                self.install._apply_deferred_entries(run_report, folder)
            # Resolved on the run report (the final write carries this to
            # disk) + the B-F9 audit row names the probe.
            self.assertFalse(run_report.has_condition(CID))
            self.assertTrue(any(
                r.get("condition_id") == CID
                and r.get("action") == "resolved_by_registry_probe"
                for r in _audit_rows(folder)
            ))


if __name__ == "__main__":
    unittest.main()
