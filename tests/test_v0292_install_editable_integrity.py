# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — editable-install integrity: the fix, the repair, and the probe.

THE SHIPPED DEFECT
------------------
install step 4 installs the orchestrator's own distribution EDITABLY
(``pip install -e .``) so every hook, MCP and ``python -m vco_lib.X`` runs the
user's CHECKOUT. The optional ``codegraph-ts`` step then ran::

    pip install <root>[codegraph-ts]        # note: NO -e

That names the SAME distribution, so pip uninstalled the editable install and
replaced it with a real copy of ``vco_lib/`` inside the venv's
``site-packages``. Nothing errored. From then on the install ran a FROZEN
install-time snapshot, and because every later update changes the checkout and
not the copy, the user's fixes silently never took effect. The step is
default-ON (opt-out ``VCT_SKIP_CODEGRAPH_TS=1``), so the blast radius is
essentially every install.

WHAT THIS FILE PINS
-------------------
1. the argv fix (``-e`` present, and BEFORE the extra target);
2. the pure decision table for the residual repair — the ACT and every
   LEAVE-ALONE arm, because the repair deletes files;
3. the orchestration's soft-fail contract (never raises, never removes without
   positive confirmation);
4. the doctor probe's four verdicts;
5. the install.py wiring (the repair is called where step 4 lands, the plan's
   argv is spliced verbatim).

The live end-to-end transition (broken venv -> fixed installer -> editable, no
copy) is proven in ``test_v0292_install_editable_transition.py``, which needs a
real pip and is skip-gated accordingly.
"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor  # noqa: E402
from vco_lib.install_companions import (  # noqa: E402
    ORIGIN_CHECKOUT,
    ORIGIN_FOREIGN,
    ORIGIN_SITE_PACKAGES,
    ORIGIN_UNKNOWN,
    VCO_DIST_INFO_GLOB,
    VCO_DISTRIBUTION_NAME,
    VCO_PACKAGE_NAME,
    classify_vco_lib_origin,
    codegraph_ts_install_plan,
    file_url_to_path,
    parse_direct_url,
    read_vco_dist_shape,
    repair_shadowed_vco_lib,
    resolve_install_venv_python,
    shadow_repair_plan,
)

ROOT = "/opt/vco"
SITE = "/opt/vco/.venv/lib/python3.12/site-packages"


def _editable_gate_kwargs(**overrides):
    """The all-gates-satisfied kwargs for :func:`shadow_repair_plan`.

    Each leave-alone test flips exactly ONE field, so a test that stops failing
    because an unrelated gate changed is impossible.
    """
    kwargs = dict(
        origin_state=ORIGIN_SITE_PACKAGES,
        dist_editable=True,
        dist_url=f"file://{ROOT}",
        install_root=ROOT,
        site_packages=SITE,
        package_dir_exists=True,
        package_dir_is_symlink=False,
        package_init_exists=True,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# 1. The argv fix
# ---------------------------------------------------------------------------


class CodegraphTsArgvTests(unittest.TestCase):
    """RED BEFORE THE FIX: the plan returned ``["<root>[codegraph-ts]"]`` with
    no ``-e``, so every assertion in this class failed."""

    def test_install_argv_is_editable(self):
        should, reason, argv = codegraph_ts_install_plan(
            pyproject_exists=True, skip_env=False, project_root=ROOT,
        )
        self.assertTrue(should)
        self.assertIsNone(reason)
        self.assertEqual(argv, ["-e", f"{ROOT}[codegraph-ts]"])

    def test_dash_e_precedes_the_target(self):
        """``pip install <target> -e`` is not the same command. Order is part
        of the contract, so pin it rather than just membership."""
        _s, _r, argv = codegraph_ts_install_plan(
            pyproject_exists=True, skip_env=False, project_root=ROOT,
        )
        assert argv is not None
        self.assertEqual(argv.index("-e"), 0)
        self.assertTrue(argv[1].endswith("[codegraph-ts]"))

    def test_skip_arms_still_return_no_argv(self):
        """LEAVE-ALONE: the fix must not have turned a skip into an install."""
        for kwargs in (
            dict(pyproject_exists=True, skip_env=True, project_root=ROOT),
            dict(pyproject_exists=False, skip_env=False, project_root=ROOT),
        ):
            should, reason, argv = codegraph_ts_install_plan(**kwargs)
            self.assertFalse(should)
            self.assertIsNotNone(reason)
            self.assertIsNone(argv)


# ---------------------------------------------------------------------------
# 2. direct_url.json parsing (PEP 610)
# ---------------------------------------------------------------------------


class DirectUrlTests(unittest.TestCase):
    def test_editable_directory_install(self):
        """The shape a healthy install carries. Measured verbatim from pip
        26.2.1 + hatchling."""
        self.assertEqual(
            parse_direct_url(
                {"dir_info": {"editable": True}, "url": "file:///opt/vco"}
            ),
            (True, "file:///opt/vco"),
        )

    def test_non_editable_directory_install_is_the_defect_shape(self):
        self.assertEqual(
            parse_direct_url({"dir_info": {}, "url": "file:///opt/vco"}),
            (False, "file:///opt/vco"),
        )

    def test_non_directory_install_is_unknown_not_false(self):
        """A VCS/archive install is not "not editable" — the question does not
        apply. Tri-state, so the repair's gate fails closed instead of acting
        on a False it invented."""
        editable, url = parse_direct_url(
            {"vcs_info": {"vcs": "git"}, "url": "git+https://example/x"}
        )
        self.assertIsNone(editable)
        self.assertEqual(url, "git+https://example/x")

    def test_garbage_is_unknown(self):
        for payload in (None, [], {}, {"dir_info": {}}, "nope", 7):
            self.assertEqual(parse_direct_url(payload), (None, ""))

    def test_file_url_to_path(self):
        self.assertEqual(file_url_to_path("file:///opt/vco"), "/opt/vco")
        self.assertEqual(file_url_to_path("file:///opt/my%20vco"), "/opt/my vco")
        self.assertEqual(file_url_to_path("git+https://example/x"), "")
        self.assertEqual(file_url_to_path(None), "")


# ---------------------------------------------------------------------------
# 3. Origin classification
# ---------------------------------------------------------------------------


class OriginClassificationTests(unittest.TestCase):
    def test_checkout_is_healthy(self):
        state, _detail = classify_vco_lib_origin(
            origin=f"{ROOT}/{VCO_PACKAGE_NAME}/__init__.py",
            install_root=ROOT,
            site_packages=SITE,
        )
        self.assertEqual(state, ORIGIN_CHECKOUT)

    def test_site_packages_copy_is_the_defect(self):
        state, detail = classify_vco_lib_origin(
            origin=f"{SITE}/{VCO_PACKAGE_NAME}/__init__.py",
            install_root=ROOT,
            site_packages=SITE,
        )
        self.assertEqual(state, ORIGIN_SITE_PACKAGES)
        self.assertIn("frozen install-time code", detail)

    def test_third_location_is_foreign_not_ok(self):
        """Another checkout's vco_lib (a moved install, a stale .pth) is a
        problem too — but NOT one we may delete anything for."""
        state, _detail = classify_vco_lib_origin(
            origin="/somewhere/else/vco_lib/__init__.py",
            install_root=ROOT,
            site_packages=SITE,
        )
        self.assertEqual(state, ORIGIN_FOREIGN)

    def test_unmeasured_is_unknown_never_ok(self):
        for origin in ("", None):
            state, _d = classify_vco_lib_origin(
                origin=origin, install_root=ROOT, site_packages=SITE
            )
            self.assertEqual(state, ORIGIN_UNKNOWN)

    def test_unknown_site_packages_does_not_become_a_false_checkout(self):
        state, _d = classify_vco_lib_origin(
            origin=f"{SITE}/{VCO_PACKAGE_NAME}/__init__.py",
            install_root=ROOT,
            site_packages="",
        )
        self.assertEqual(state, ORIGIN_FOREIGN)


# ---------------------------------------------------------------------------
# 4. The repair DECISION — the act and every leave-alone arm
# ---------------------------------------------------------------------------


class ShadowRepairPlanTests(unittest.TestCase):
    def test_act_unowned_copy_over_a_confirmed_editable_install(self):
        """ACT: every gate satisfied -> remove, and name the exact path."""
        should, reason, target = shadow_repair_plan(**_editable_gate_kwargs())
        self.assertTrue(should)
        self.assertEqual(target, f"{SITE}/{VCO_PACKAGE_NAME}")
        self.assertIn(VCO_DISTRIBUTION_NAME, reason)

    def test_leave_alone_healthy_install(self):
        """LEAVE-ALONE: a correct editable install (origin = checkout) must not
        produce a delete under any circumstance."""
        should, reason, target = shadow_repair_plan(
            **_editable_gate_kwargs(origin_state=ORIGIN_CHECKOUT)
        )
        self.assertFalse(should)
        self.assertEqual(target, "")
        self.assertIn("not a measured shadow", reason)

    def test_leave_alone_pip_owned_non_editable_copy(self):
        """LEAVE-ALONE: direct_url says NOT editable -> those files belong to
        pip's RECORD and `pip install -e .` removes them itself. Deleting them
        behind pip's back would corrupt the RECORD."""
        should, reason, target = shadow_repair_plan(
            **_editable_gate_kwargs(dist_editable=False)
        )
        self.assertFalse(should)
        self.assertEqual(target, "")
        self.assertIn("not positively editable", reason)

    def test_leave_alone_when_editability_is_unknown(self):
        should, reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(dist_editable=None)
        )
        self.assertFalse(should)
        self.assertIn("not positively editable", reason)

    def test_leave_alone_when_another_checkout_owns_the_venv(self):
        """LEAVE-ALONE: a venv shared with a different clone. Deleting there
        would break the OTHER install."""
        should, reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(dist_url="file:///opt/some-other-clone")
        )
        self.assertFalse(should)
        self.assertIn("another checkout owns this venv", reason)

    def test_leave_alone_when_the_url_is_not_a_local_directory(self):
        should, _reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(dist_url="git+https://example/x")
        )
        self.assertFalse(should)

    def test_leave_alone_symlink(self):
        """LEAVE-ALONE: never follow a link out of the venv."""
        should, reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(package_dir_is_symlink=True)
        )
        self.assertFalse(should)
        self.assertIn("symlink", reason)

    def test_leave_alone_namespace_portion_without_init(self):
        """LEAVE-ALONE: a leftover with no ``__init__.py`` is only a namespace
        portion and LOSES to the checkout's real package (measured). Deleting
        it would be a destructive change with no benefit."""
        should, reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(package_init_exists=False)
        )
        self.assertFalse(should)
        self.assertIn("namespace portion", reason)

    def test_leave_alone_when_nothing_is_there(self):
        should, _r, _t = shadow_repair_plan(
            **_editable_gate_kwargs(package_dir_exists=False)
        )
        self.assertFalse(should)

    def test_leave_alone_when_site_packages_unknown(self):
        should, reason, _t = shadow_repair_plan(
            **_editable_gate_kwargs(site_packages="")
        )
        self.assertFalse(should)
        self.assertIn("site-packages path unknown", reason)

    def test_every_refusal_explains_itself(self):
        """A silent skip is how this defect survived for months."""
        for override in (
            dict(origin_state=ORIGIN_UNKNOWN),
            dict(dist_editable=False),
            dict(package_dir_is_symlink=True),
            dict(package_init_exists=False),
            dict(package_dir_exists=False),
            dict(site_packages=""),
            dict(dist_url="file:///elsewhere"),
        ):
            should, reason, _t = shadow_repair_plan(**_editable_gate_kwargs(**override))
            self.assertFalse(should, override)
            self.assertTrue(reason.strip(), f"no reason given for {override}")


# ---------------------------------------------------------------------------
# 5. dist-info reading, including the similar-name distribution
# ---------------------------------------------------------------------------


class DistShapeTests(unittest.TestCase):
    def _site(self, tmp: Path) -> Path:
        site = tmp / "site-packages"
        site.mkdir()
        return site

    def test_reads_the_editable_shape(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site = self._site(Path(td))
            di = site / "vibecoded_orchestrator-0.2.92.dist-info"
            di.mkdir()
            (di / "direct_url.json").write_text(
                json.dumps({"dir_info": {"editable": True}, "url": "file:///opt/vco"}),
                encoding="utf-8",
            )
            editable, url, path = read_vco_dist_shape(site)
            self.assertIs(editable, True)
            self.assertEqual(url, "file:///opt/vco")
            self.assertTrue(path.endswith("vibecoded_orchestrator-0.2.92.dist-info"))

    def test_a_similarly_named_distribution_is_not_ours(self):
        """LEAVE-ALONE: ``vibecoded-orchestrator-probe`` normalises to
        ``vibecoded_orchestrator_probe-*``; the glob requires the literal ``-``
        after ``orchestrator`` so it can never match a different project."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site = self._site(Path(td))
            for name in (
                "vibecoded_orchestrator_probe-0.1.dist-info",
                "vibecoded_orchestrator_extras-9.9.dist-info",
                "not_vibecoded_orchestrator-1.0.dist-info",
            ):
                di = site / name
                di.mkdir()
                (di / "direct_url.json").write_text(
                    json.dumps({"dir_info": {}, "url": "file:///other"}),
                    encoding="utf-8",
                )
            self.assertEqual(read_vco_dist_shape(site), (None, "", ""))

    def test_ambiguous_install_fails_closed(self):
        """Two dist-info dirs for our name -> we refuse to reason about it, so
        the repair's editability gate can never be satisfied."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site = self._site(Path(td))
            for v in ("0.2.91", "0.2.92"):
                (site / f"vibecoded_orchestrator-{v}.dist-info").mkdir()
            self.assertEqual(read_vco_dist_shape(site), (None, "", ""))

    def test_missing_dist_info_is_unknown(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_vco_dist_shape(self._site(Path(td))), (None, "", ""))

    def test_glob_constant_matches_the_pyproject_name(self):
        """If the distribution is ever renamed, this catches the glob drifting
        away from it."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'name = "{VCO_DISTRIBUTION_NAME}"', pyproject)
        self.assertEqual(
            VCO_DIST_INFO_GLOB,
            VCO_DISTRIBUTION_NAME.replace("-", "_") + "-*.dist-info",
        )


# ---------------------------------------------------------------------------
# 6. The orchestration — soft-fail + "never remove without confirmation"
# ---------------------------------------------------------------------------


class _Removals:
    """Records deletes instead of performing them."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(str(path))


class RepairOrchestrationTests(unittest.TestCase):
    def _measure(self, origin, purelib=SITE, error=""):
        return lambda _py: {"origin": origin, "purelib": purelib, "error": error}

    def test_healthy_install_removes_nothing(self):
        removals = _Removals()
        result = repair_shadowed_vco_lib(
            ROOT, "/opt/vco/.venv/bin/python",
            measure=self._measure(f"{ROOT}/{VCO_PACKAGE_NAME}/__init__.py"),
            dist_shape=lambda _s: (True, f"file://{ROOT}", "x"),
            remove=removals,
        )
        self.assertEqual(result["action"], "ok")
        self.assertEqual(result["state"], ORIGIN_CHECKOUT)
        self.assertEqual(removals.calls, [])

    def test_unmeasurable_removes_nothing_and_says_why(self):
        removals = _Removals()
        result = repair_shadowed_vco_lib(
            ROOT, "/opt/vco/.venv/bin/python",
            measure=lambda _py: None,
            dist_shape=lambda _s: (True, f"file://{ROOT}", "x"),
            remove=removals,
        )
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["state"], ORIGIN_UNKNOWN)
        self.assertTrue(result["reason"])
        self.assertEqual(removals.calls, [])

    def test_foreign_origin_removes_nothing(self):
        removals = _Removals()
        result = repair_shadowed_vco_lib(
            ROOT, "/opt/vco/.venv/bin/python",
            measure=self._measure("/elsewhere/vco_lib/__init__.py"),
            dist_shape=lambda _s: (True, f"file://{ROOT}", "x"),
            remove=removals,
        )
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["state"], ORIGIN_FOREIGN)
        self.assertEqual(removals.calls, [])

    def test_pip_owned_copy_is_left_for_pip(self):
        """The SHIPPED defect's own shape: non-editable direct_url. pip's
        uninstall during `pip install -e .` owns this; we must not race it."""
        removals = _Removals()
        with mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(Path, "is_file", return_value=True):
            result = repair_shadowed_vco_lib(
                ROOT, "/opt/vco/.venv/bin/python",
                measure=self._measure(f"{SITE}/{VCO_PACKAGE_NAME}/__init__.py"),
                dist_shape=lambda _s: (False, f"file://{ROOT}", "x"),
                remove=removals,
            )
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(removals.calls, [])
        self.assertIn("not positively editable", result["reason"])

    def test_unowned_copy_is_removed_and_re_measured(self):
        """ACT: the residue leg. The result must carry a FRESH measurement, not
        an assumption that the delete worked."""
        removals = _Removals()
        seen = {"n": 0}

        def measure(_py):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"origin": f"{SITE}/{VCO_PACKAGE_NAME}/__init__.py",
                        "purelib": SITE, "error": ""}
            return {"origin": f"{ROOT}/{VCO_PACKAGE_NAME}/__init__.py",
                    "purelib": SITE, "error": ""}

        with mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(Path, "is_file", return_value=True):
            result = repair_shadowed_vco_lib(
                ROOT, "/opt/vco/.venv/bin/python",
                measure=measure,
                dist_shape=lambda _s: (True, f"file://{ROOT}", "x"),
                remove=removals,
            )
        self.assertEqual(result["action"], "removed")
        self.assertEqual(removals.calls, [f"{SITE}/{VCO_PACKAGE_NAME}"])
        self.assertEqual(result["verified_state"], ORIGIN_CHECKOUT)
        self.assertEqual(seen["n"], 2, "must re-measure after removing")

    def test_a_failed_delete_is_reported_not_raised(self):
        def boom(_path):
            raise PermissionError("read-only venv")

        with mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(Path, "is_file", return_value=True):
            result = repair_shadowed_vco_lib(
                ROOT, "/opt/vco/.venv/bin/python",
                measure=self._measure(f"{SITE}/{VCO_PACKAGE_NAME}/__init__.py"),
                dist_shape=lambda _s: (True, f"file://{ROOT}", "x"),
                remove=boom,
            )
        self.assertEqual(result["action"], "failed")
        self.assertIn("PermissionError", result["reason"])


class VenvPythonResolverTests(unittest.TestCase):
    """The helper install.py's ``_resolve_venv_python_for_install`` now
    delegates to — one implementation, no cross-file mirror."""

    def test_prefers_the_modern_layout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in (".venv/bin/python", "claude_mcp_servers/.venv/bin/python"):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("", encoding="utf-8")
            self.assertEqual(
                resolve_install_venv_python(root, os_name="Linux"),
                root / ".venv" / "bin" / "python",
            )

    def test_falls_back_to_the_legacy_layout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "claude_mcp_servers/.venv/bin/python"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
            self.assertEqual(
                resolve_install_venv_python(root, os_name="Linux"), p
            )

    def test_windows_layout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / ".venv/Scripts/python.exe"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
            self.assertEqual(
                resolve_install_venv_python(root, os_name="Windows"), p
            )

    def test_none_when_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(resolve_install_venv_python(Path(td), os_name="Linux"))


# ---------------------------------------------------------------------------
# 7. The doctor probe
# ---------------------------------------------------------------------------


class DoctorProbeTests(unittest.TestCase):
    def _root(self, td: str) -> Path:
        root = Path(td)
        (root / VCO_PACKAGE_NAME).mkdir()
        (root / VCO_PACKAGE_NAME / "__init__.py").write_text("", encoding="utf-8")
        return root

    def _run(self, root: Path, payload):
        res = doctor.DoctorResolvers(vco_lib_origin=lambda _r: payload)
        return doctor.probe_vco_lib_editable(root, res, {})

    def test_checkout_is_silent_ok(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            findings = self._run(
                root,
                {"origin": str(root / VCO_PACKAGE_NAME / "__init__.py"),
                 "purelib": SITE, "error": ""},
            )
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].status, doctor.STATUS_OK)
            self.assertFalse(findings[0].is_problem)

    def test_site_packages_copy_is_loud(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            site = str(root / ".venv/lib/python3.12/site-packages")
            findings = self._run(
                root,
                {"origin": f"{site}/{VCO_PACKAGE_NAME}/__init__.py",
                 "purelib": site, "error": ""},
            )
            self.assertEqual(len(findings), 1)
            f = findings[0]
            self.assertEqual(f.status, doctor.STATUS_PROBLEM)
            self.assertEqual(f.fix, doctor.FIX_DEFER)
            self.assertIn("install.py --update", f.command)
            self.assertIn("import vco_lib", f.command)

    def test_problem_finding_fails_the_report(self):
        """`vco doctor` exits 1 on this. That is the whole point of the probe."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            site = str(root / ".venv/lib/python3.12/site-packages")
            res = doctor.DoctorResolvers(
                npx_probe=lambda _n: {},
                mcp_entries=lambda: {},
                deferral_report=lambda _f: None,
                pin_rows=lambda: None,
                disk_usage=lambda _p: None,
                vco_lib_origin=lambda _r: {
                    "origin": f"{site}/{VCO_PACKAGE_NAME}/__init__.py",
                    "purelib": site, "error": "",
                },
            )
            report = doctor.run_doctor(root, resolvers=res)
            self.assertFalse(report.ok)
            self.assertIn(
                "vco_lib_editable", {f.probe for f in report.problems}
            )

    def test_unmeasurable_is_unknown_not_a_problem(self):
        """"I could not check" is not "broken" — an unknown must not fail an
        install that otherwise succeeded."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            findings = self._run(root, None)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
            self.assertFalse(findings[0].is_problem)

    def test_not_an_orchestrator_root_yields_no_findings(self):
        """LEAVE-ALONE: a user project has no vco_lib/ — the question does not
        apply, and an unknown on every project run would be noise."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            called = {"n": 0}

            def _measure(_r):
                called["n"] += 1
                return None

            res = doctor.DoctorResolvers(vco_lib_origin=_measure)
            self.assertEqual(
                doctor.probe_vco_lib_editable(Path(td), res, {}), []
            )
            self.assertEqual(called["n"], 0, "must not spawn a probe subprocess")

    def test_probe_is_full_scope_only(self):
        """One subprocess is over the boot subset's file-read budget."""
        _fn, scopes = doctor.PROBES["vco_lib_editable"]
        self.assertIn(doctor.SCOPE_FULL, scopes)
        self.assertNotIn(doctor.SCOPE_BOOT, scopes)

    def _problem_findings(self, root: Path):
        site = str(root / ".venv/lib/python3.12/site-packages")
        return self._run(
            root,
            {"origin": f"{site}/{VCO_PACKAGE_NAME}/__init__.py",
             "purelib": site, "error": ""},
        )

    def test_problem_emits_the_registered_condition(self):
        """ACT: the ledger entry. The WP-B gate requires the registry row to
        land in the SAME change as the emit site, so assert both ends."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            findings = self._problem_findings(root)
            self.assertEqual(
                findings[0].condition_id, doctor.CID_VCO_LIB_SHADOWED
            )
            self.assertIn(doctor.CID_VCO_LIB_SHADOWED, doctor.DOCTOR_OWNED_CIDS)

            report = doctor.DoctorReport(folder=root, scope=doctor.SCOPE_FULL)
            report.findings.extend(findings)
            entries = doctor.deferral_entries_for(report)
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                entries[0].condition_id, doctor.CID_VCO_LIB_SHADOWED
            )
            self.assertTrue(entries[0].command_to_apply.strip())
            self.assertEqual(entries[0].severity, "critical")

    def test_the_condition_is_declared_in_the_registry(self):
        """A cid with no declared class/owner/clear mechanism is how the ledger
        silted up. Read the row rather than trusting the emit site."""
        import tomllib

        registry = tomllib.loads(
            (REPO_ROOT / "vco_lib" / "deferral_conditions.toml").read_text(
                encoding="utf-8"
            )
        )
        row = registry["conditions"][doctor.CID_VCO_LIB_SHADOWED]
        self.assertEqual(row["class"], "action_required")
        self.assertEqual(row["owner"], "vco_lib.doctor")
        self.assertEqual(row["clear_probe"], "owned-drop-when-absent")
        self.assertIn("ledger", row["emit_surfaces"])

    def test_a_healthy_install_emits_nothing(self):
        """LEAVE-ALONE: an OK reading must not write a ledger entry."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            findings = self._run(
                root,
                {"origin": str(root / VCO_PACKAGE_NAME / "__init__.py"),
                 "purelib": "/x/site-packages", "error": ""},
            )
            report = doctor.DoctorReport(folder=root, scope=doctor.SCOPE_FULL)
            report.findings.extend(findings)
            self.assertEqual(doctor.deferral_entries_for(report), [])


# ---------------------------------------------------------------------------
# 8. install.py wiring
# ---------------------------------------------------------------------------


class InstallWiringTests(unittest.TestCase):
    """Source-level pins. The behaviour lives in the pure functions above; what
    these guard is that install.py still CALLS them, in the right place — the
    "a later step silently undoes an earlier one" class this defect belongs to
    is a wiring bug, not a logic bug."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def _func(self, name: str) -> str:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                seg = ast.get_source_segment(self.src, node)
                self.assertTrue(seg, f"could not extract {name}")
                return seg or ""
        self.fail(f"{name} not found in install.py")
        return ""

    def test_codegraph_ts_step_splices_the_plan_argv_verbatim(self):
        """The ``-e`` must reach pip. If this step ever rebuilds the argv
        itself, the decision function stops being the single source of truth
        and the defect can come straight back."""
        body = self._func("_install_codegraph_treesitter")
        self.assertIn("*(pip_target or [])", body)
        self.assertIn("codegraph_ts_install_plan", body)
        self.assertNotIn('f"{PROJECT_ROOT}[codegraph-ts]"', body)

    def test_step_four_verifies_its_own_editable_install(self):
        """The step that OWNS the editable install must check its own result —
        and AFTER producing it, not before."""
        body = self._func("_install_requirements")
        call = "_install_companions.repair_and_report_vco_lib("
        self.assertIn(call, body)
        self.assertLess(
            body.index('"-e", "."'), body.index(call),
            "the verify must run AFTER the editable install it verifies",
        )

    def test_repair_reporting_lives_in_vco_lib_not_the_monolith(self):
        """install.py is under a hard line ratchet; the report is a pure
        function of the repair result and has no business next to the pip
        calls. Pins the extraction so it cannot drift back."""
        install_src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertNotIn("def _verify_and_repair_editable_vco_lib", install_src)
        from vco_lib import install_companions  # noqa: PLC0415

        self.assertTrue(hasattr(install_companions, "repair_and_report_vco_lib"))

    #: Decision functions whose returned argv install.py splices verbatim into
    #: a pip invocation. Every one of them is exercised by
    #: ``test_every_delegated_argv_decision_is_editable`` below — that is what
    #: makes a delegated site safe, and it is where the v0.2.92 defect lived.
    PINNED_ARGV_DECISIONS = {"codegraph_ts_install_plan"}

    def _pip_install_argv_lists(self):
        """Every ``[..., "-m", "pip", "install", ...]`` list literal in
        install.py, as (source_segment, enclosing_function_name)."""
        enclosing = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        out = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.List):
                continue
            seg = ast.get_source_segment(self.src, node) or ""
            flat = " ".join(seg.split())
            if '"-m", "pip", "install"' not in flat:
                continue
            out.append((seg, flat, enclosing.get(id(node), "<module>")))
        return out

    def test_no_undelegated_pip_install_can_clobber_the_editable_install(self):
        """The sibling audit, encoded.

        Classifies every pip-install argv in install.py and requires each to be
        one of the shapes we have reasoned about. GREEN before the v0.2.92 fix
        by design — the defective site delegated its argv to a decision
        function, so the source scan alone could never see the missing ``-e``.
        Its job is to stop the NEXT site from being written inline; the
        delegated ones are pinned by the test below, which IS red pre-fix.
        Stating that here so nobody mistakes this for the regression test.
        """
        offenders = []
        for seg, flat, func in self._pip_install_argv_lists():
            if '"-r"' in flat:                       # requirements install
                continue
            if '"--upgrade", "pip"' in flat:          # pip's own upgrade
                continue
            if '"--dry-run"' in flat:                 # wheel-support probe
                continue
            if '"-e"' in flat:                        # editable, explicit
                continue
            body = self._func(func) if func != "<module>" else self.src
            if any(name in body for name in self.PINNED_ARGV_DECISIONS):
                continue                              # delegated + pinned below
            offenders.append(f"{func}: {seg.strip()[:200]}")
        self.assertEqual(
            offenders, [],
            "pip install argv that is neither a requirements/upgrade/probe "
            "call, nor editable, nor delegated to a pinned decision — this is "
            "the shape that clobbers the editable install:\n"
            + "\n\n".join(offenders),
        )

    def test_every_delegated_argv_decision_is_editable(self):
        """RED BEFORE THE FIX. The delegated sites are only safe because the
        decision they delegate to returns an editable argv — so exercise it.

        This is the audit half the source scan structurally cannot do: the
        v0.2.92 defect was a missing ``-e`` INSIDE the decision function, which
        no amount of reading install.py could reveal.
        """
        from vco_lib import install_companions  # noqa: PLC0415

        self.assertTrue(self.PINNED_ARGV_DECISIONS)
        for name in sorted(self.PINNED_ARGV_DECISIONS):
            fn = getattr(install_companions, name)
            should, _reason, argv = fn(
                pyproject_exists=True, skip_env=False, project_root=ROOT,
            )
            self.assertTrue(should, name)
            assert argv is not None
            self.assertIn(
                "-e", argv,
                f"{name} builds a NON-editable pip target ({argv!r}); pip will "
                "replace the editable install of the same distribution with a "
                "frozen copy in site-packages",
            )
            self.assertTrue(
                any(t.startswith(ROOT) for t in argv),
                f"{name} must target the install root",
            )

    def test_delegated_sites_are_all_registered(self):
        """If a new delegated pip site appears, the registry above must grow
        with it — otherwise the editability pin silently stops covering it."""
        delegating = set()
        for _seg, flat, func in self._pip_install_argv_lists():
            if '"-r"' in flat or '"--upgrade", "pip"' in flat or '"--dry-run"' in flat:
                continue
            if '"-e"' in flat or func == "<module>":
                continue
            body = self._func(func)
            for name in self.PINNED_ARGV_DECISIONS:
                if name in body:
                    delegating.add(name)
        self.assertEqual(
            delegating, self.PINNED_ARGV_DECISIONS,
            "PINNED_ARGV_DECISIONS is out of sync with install.py's delegated "
            "pip-install sites",
        )

    def test_venv_python_resolver_delegates_rather_than_mirrors(self):
        body = self._func("_resolve_venv_python_for_install")
        self.assertIn("resolve_install_venv_python", body)
        self.assertNotIn("pyvenv.cfg", body)
        self.assertNotIn('"Scripts"', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
