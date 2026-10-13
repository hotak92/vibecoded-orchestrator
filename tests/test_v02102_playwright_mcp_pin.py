# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-Z4 — the default ``playwright`` MCP can launch a browser.

Pins, against a described machine (temp npx cache + temp browser registry,
mocked subprocess runner — no network, no npx, no real cache):

* the pin row and the registered argv (``--browser chromium``, exact version);
* the uninstall fingerprint (current + legacy are VCO's, user variants are not);
* ``browser_state``: present / missing / unknown, derived from the PINNED
  package's own ``playwright-core/browsers.json``;
* the install plan: the pinned package's own installer, with the
  ``playwright@<core>`` fallback when ``install-browser`` is absent;
* ``precache``: skip, no-npx, success, failure → ``playwright_browser_missing``
  with the repair command, failure with the right revision already on disk →
  nothing owed;
* the doctor probe + entry + registry clear probe, and the registry row.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vco_lib import doctor, deferral_probes, playwright_mcp as pm
from vco_lib.deferral_report import DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN = pm.Pin(package="@playwright/mcp", version="0.0.83",
             shasum="30bf9c6c0d80835517fd2e9b022559ea05cb1428", browser="chromium")


def _npx_pkg(cache: Path, *, version: str = "0.0.83", core: str = "1.64.0-alpha-1",
             revision: str = "1247", install_browser: bool = True,
             hash_dir: str = "0123456789abcdef") -> Path:
    """Lay out ``<cache>/_npx/<hash>/node_modules/{@playwright/mcp,playwright-core}``."""
    nm = cache / "_npx" / hash_dir / "node_modules"
    pkg = nm / "@playwright" / "mcp"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({
        "name": "@playwright/mcp", "version": version,
        "dependencies": {"playwright": core, "playwright-core": core},
    }), encoding="utf-8")
    (pkg / "cli.js").write_text(
        "if (process.argv.includes('install-browser')) {}\n" if install_browser
        else "program.parse()\n", encoding="utf-8")
    core_dir = nm / "playwright-core"
    core_dir.mkdir(parents=True)
    (core_dir / "browsers.json").write_text(json.dumps({"browsers": [
        {"name": "chromium", "revision": revision},
        {"name": "chromium-headless-shell", "revision": revision},
    ]}), encoding="utf-8")
    return nm


def _install_browser(registry: Path, revision: str = "1247", complete: bool = True) -> Path:
    d = registry / f"chromium-{revision}"
    d.mkdir(parents=True)
    if complete:
        (d / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
    return d


class _Machine(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.cache = self.tmp / "npm-cache"
        self.registry = self.tmp / "ms-playwright"
        self.env = {"npm_config_cache": str(self.cache),
                    "PLAYWRIGHT_BROWSERS_PATH": str(self.registry)}

    def tearDown(self) -> None:
        self._td.cleanup()

    def state(self) -> pm.BrowserState:
        return pm.browser_state(PIN, environ=self.env)


class PinAndRegistrationTests(unittest.TestCase):
    def test_shipped_pin_is_exact_and_chromium(self) -> None:
        pin = pm.load_pin()
        self.assertEqual(pin.package, "@playwright/mcp")
        self.assertEqual(pin.browser, "chromium")
        self.assertRegex(pin.version, r"^\d+\.\d+\.\d+")
        self.assertRegex(pin.shasum, r"^[0-9a-f]{40}$")

    def test_pin_is_kept_out_of_the_npm_install_g_set(self) -> None:
        # `vco verify-pins` asks `npm ls -g` about [npm.*]; the MCP is an npx
        # launch, so it must live in [npx.*] only.
        data = tomllib.loads((REPO_ROOT / "vco_lib" / "bundled_mcp_versions.toml")
                             .read_text(encoding="utf-8"))
        self.assertIn("playwright_mcp", data["npx"])
        self.assertNotIn("playwright_mcp", data.get("npm", {}))
        self.assertEqual(set(data["npx"]["playwright_mcp"]),
                         {"package", "version", "shasum", "browser"})

    def test_missing_pin_is_loud(self) -> None:
        with self.assertRaises(KeyError):
            pm.load_pin({"npm": {}})
        with self.assertRaises(KeyError):
            pm.load_pin({"npx": {"playwright_mcp": {"package": "@playwright/mcp"}}})

    def test_registration_args_shape(self) -> None:
        self.assertEqual(pm.registration_args(PIN),
                         ["-y", "@playwright/mcp@0.0.83", "--browser", "chromium"])
        self.assertEqual(pm.registration_entry(PIN), {
            "type": "stdio", "command": "npx",
            "args": ["-y", "@playwright/mcp@0.0.83", "--browser", "chromium"],
            "env": {},
        })

    def test_vco_fingerprint(self) -> None:
        current = pm.registration_entry(PIN)
        legacy = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        self.assertTrue(pm.is_vco_shaped_entry(current, PIN))
        self.assertTrue(pm.is_vco_shaped_entry(legacy, PIN))
        for args in (["@playwright/mcp"], ["-y", "@playwright/mcp@1.2.3"],
                     ["-y", "@playwright/mcp@0.0.83"],
                     ["-y", "@playwright/mcp@0.0.83", "--browser", "firefox"]):
            with self.subTest(args=args):
                self.assertFalse(pm.is_vco_shaped_entry({"command": "npx", "args": args}, PIN))
        self.assertFalse(pm.is_vco_shaped_entry(
            {"command": "/opt/node/bin/npx", "args": current["args"]}, PIN))

    def test_uninstall_fingerprint_uses_the_one_predicate(self) -> None:
        from vco_lib import install_mcp
        self.assertTrue(install_mcp._is_vco_shaped_playwright_entry(pm.registration_entry()))
        self.assertTrue(install_mcp._is_vco_shaped_playwright_entry(
            {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}))


class LocationTests(unittest.TestCase):
    def test_npm_cache_dir(self) -> None:
        home = Path("/h")
        self.assertEqual(pm.npm_cache_dir({}, home, "linux"), home / ".npm")
        self.assertEqual(pm.npm_cache_dir({"npm_config_cache": "/c"}, home, "linux"), Path("/c"))
        self.assertEqual(pm.npm_cache_dir({"LOCALAPPDATA": "/L"}, home, "win32"),
                         Path("/L") / "npm-cache")

    def test_browsers_registry_dir(self) -> None:
        home = Path("/h")
        self.assertEqual(pm.browsers_registry_dir({}, home, "linux"),
                         home / ".cache" / "ms-playwright")
        self.assertEqual(pm.browsers_registry_dir({"XDG_CACHE_HOME": "/x"}, home, "linux"),
                         Path("/x") / "ms-playwright")
        self.assertEqual(pm.browsers_registry_dir({}, home, "darwin"),
                         home / "Library" / "Caches" / "ms-playwright")
        self.assertEqual(pm.browsers_registry_dir({"LOCALAPPDATA": "/L"}, home, "win32"),
                         Path("/L") / "ms-playwright")
        self.assertEqual(pm.browsers_registry_dir({"PLAYWRIGHT_BROWSERS_PATH": "/p"}, home),
                         Path("/p"))
        self.assertIsNone(pm.browsers_registry_dir({"PLAYWRIGHT_BROWSERS_PATH": "0"}, home))
        self.assertEqual(
            pm.browsers_registry_dir({"PLAYWRIGHT_BROWSERS_PATH": "0"}, home,
                                     node_modules=Path("/nm")),
            Path("/nm") / "playwright-core" / ".local-browsers")


class BrowserStateTests(_Machine):
    def test_unknown_when_pinned_package_not_cached(self) -> None:
        _npx_pkg(self.cache, version="0.0.68")  # a DIFFERENT version only
        st = self.state()
        self.assertEqual(st.state, pm.STATE_UNKNOWN)
        self.assertEqual(st.repair_argv,
                         ["npx", "-y", "@playwright/mcp@0.0.83", "install-browser", "chromium"])

    def test_missing_when_revision_known_and_absent(self) -> None:
        _npx_pkg(self.cache, revision="1247")
        _install_browser(self.registry, "1243")  # an OLDER revision is not enough
        _install_browser(self.registry, "1247", complete=False)  # half a download is not either
        st = self.state()
        self.assertEqual(st.state, pm.STATE_MISSING)
        self.assertEqual(st.revisions, ("1247",))
        self.assertEqual(st.path, self.registry / "chromium-1247")

    def test_present_when_the_pinned_revision_is_complete(self) -> None:
        _npx_pkg(self.cache, revision="1247")
        _install_browser(self.registry, "1247")
        st = self.state()
        self.assertEqual(st.state, pm.STATE_PRESENT)
        self.assertEqual(st.path, self.registry / "chromium-1247")

    def test_fallback_install_argv_when_install_browser_absent(self) -> None:
        nm = _npx_pkg(self.cache, core="1.70.0", install_browser=False)
        cached = pm.cached_package(PIN, npm_cache=self.cache)
        self.assertEqual(cached.node_modules, nm)
        self.assertEqual(pm.browser_install_argv(PIN, cached),
                         ["npx", "-y", "playwright@1.70.0", "install", "chromium"])


class PrecacheTests(_Machine):
    def _runner(self, *, fetch_rc=0, install_rc=0, on_install=None, fetch_exc=None):
        calls: list[list[str]] = []

        def run(argv, **kw):
            calls.append(list(argv))
            self.assertEqual(kw.get("on_failure"), "return")
            self.assertGreaterEqual(kw["timeout"], 900)  # long per-chunk guards
            if "--version" in argv:
                if fetch_exc:
                    raise fetch_exc
                if fetch_rc == 0 and not (self.cache / "_npx").exists():
                    _npx_pkg(self.cache)
                return SimpleNamespace(returncode=fetch_rc)
            if install_rc == 0 and on_install:
                on_install()
            return SimpleNamespace(returncode=install_rc)
        return run, calls

    def _precache(self, runner, report, env=None):
        events: list = []
        outcome = pm.precache(
            npx_path="/usr/bin/npx", runner=runner,
            log_event=lambda *a, **k: events.append(a), report=report,
            environ=env if env is not None else self.env, out=lambda _l: None, pin=PIN,
            state_fn=lambda p: pm.browser_state(p, environ=self.env),
        )
        return outcome, events

    def test_skip_env(self) -> None:
        run, calls = self._runner()
        report = DeferralReport()
        outcome, _ = self._precache(run, report, env={**self.env, "VCT_SKIP_PLAYWRIGHT": "1"})
        self.assertEqual(outcome, "skipped")
        self.assertEqual(calls, [])
        self.assertEqual(report.entries, [])

    def test_no_npx(self) -> None:
        report = DeferralReport()
        outcome = pm.precache(npx_path=None, runner=lambda *a, **k: None,
                              log_event=lambda *a, **k: None, report=report,
                              environ=self.env, out=lambda _l: None, pin=PIN)
        self.assertEqual(outcome, "no-npx")
        self.assertEqual(report.entries, [])

    def test_success_installs_through_the_pinned_package(self) -> None:
        run, calls = self._runner(on_install=lambda: _install_browser(self.registry))
        report = DeferralReport()
        outcome, _ = self._precache(run, report)
        self.assertEqual(outcome, "ok")
        self.assertEqual(calls, [
            ["/usr/bin/npx", "-y", "@playwright/mcp@0.0.83", "--version"],
            ["/usr/bin/npx", "-y", "@playwright/mcp@0.0.83", "install-browser", "chromium"],
        ])
        self.assertEqual(report.entries, [])

    def test_failed_install_defers_with_the_repair_command(self) -> None:
        run, _ = self._runner(install_rc=1)
        report = DeferralReport()
        outcome, _ = self._precache(run, report)
        self.assertEqual(outcome, "failed")
        [entry] = report.entries
        self.assertEqual(entry.condition_id, "playwright_browser_missing")
        self.assertIn("npx -y @playwright/mcp@0.0.83 install-browser chromium",
                      entry.command_to_apply)
        self.assertIn("chromium-1247", entry.detected)

    def test_exit_zero_without_the_browser_still_defers(self) -> None:
        run, _ = self._runner(install_rc=0)  # "succeeded" but left nothing
        report = DeferralReport()
        outcome, _ = self._precache(run, report)
        self.assertEqual(outcome, "failed")
        self.assertEqual([e.condition_id for e in report.entries],
                         ["playwright_browser_missing"])

    def test_failure_with_the_revision_already_present_owes_nothing(self) -> None:
        _npx_pkg(self.cache)
        _install_browser(self.registry)
        run, _ = self._runner(install_rc=1)
        report = DeferralReport()
        outcome, _ = self._precache(run, report)
        self.assertEqual(outcome, "ok")
        self.assertEqual(report.entries, [])

    def test_fetch_timeout_defers_and_does_not_raise(self) -> None:
        run, calls = self._runner(fetch_exc=subprocess.TimeoutExpired("npx", 900))
        report = DeferralReport()
        outcome, _ = self._precache(run, report)
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual([e.condition_id for e in report.entries],
                         ["playwright_browser_missing"])


class InstallShimTests(unittest.TestCase):
    def test_shim_threads_the_run_report_and_runner(self) -> None:
        import install
        report = DeferralReport()
        with mock.patch.object(install._playwright_mcp, "precache") as pre, \
                mock.patch.object(install, "_find_npx", return_value="/x/npx"):
            install._install_playwright_browsers(report)
        kw = pre.call_args.kwargs
        self.assertIs(kw["report"], report)
        self.assertIs(kw["runner"], install._run_logged_subprocess)
        self.assertEqual(kw["npx_path"], "/x/npx")

    def test_shim_soft_fails(self) -> None:
        import install
        with mock.patch.object(install._playwright_mcp, "precache", side_effect=RuntimeError("boom")):
            install._install_playwright_browsers(DeferralReport())  # must not raise


class DoctorProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "vco_lib").mkdir()
        (self.root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, entry, state=None, environ=None):
        res = doctor.DoctorResolvers(
            mcp_entries=lambda: ({} if entry is None else {"playwright": entry}),
            playwright_browser_state=lambda: state,
            environ=(lambda: environ or {}),
        )
        return pm.probe_browser(self.root, res, {})

    def _state(self, which: str) -> pm.BrowserState:
        pin = pm.load_pin()
        return pm.BrowserState(which, pin, f"reason-{which}", revisions=("1247",),
                               repair_argv=pm.browser_install_argv(pin, None))

    def test_no_entry_or_user_entry_means_nothing_to_check(self) -> None:
        self.assertEqual(self._run(None), [])
        self.assertEqual(self._run({"command": "npx", "args": ["-y", "@acme/pw"]}), [])

    def test_not_an_install_root(self) -> None:
        (self.root / "vco_lib" / "__init__.py").unlink()
        self.assertEqual(self._run(pm.registration_entry(), self._state("missing")), [])

    def test_legacy_entry_is_unknown_and_names_the_update(self) -> None:
        [f] = self._run({"command": "npx", "args": ["-y", "@playwright/mcp@latest"]})
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertEqual(f.command, "python install.py --update")

    def test_missing_is_a_deferred_problem_with_the_entry(self) -> None:
        [f] = self._run(pm.registration_entry(), self._state("missing"))
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertEqual(f.condition_id, "playwright_browser_missing")
        report = doctor.DoctorReport(folder=self.root, scope=doctor.SCOPE_FULL, findings=[f])
        [entry] = doctor.deferral_entries_for(report)
        self.assertEqual(entry.condition_id, "playwright_browser_missing")
        self.assertIn("install-browser chromium", entry.command_to_apply)
        self.assertEqual(entry.disposition, "action_required")

    def test_present_is_ok_and_self_resolving(self) -> None:
        [f] = self._run(pm.registration_entry(), self._state("present"))
        self.assertEqual(f.status, doctor.STATUS_OK)
        report = doctor.DoctorReport(folder=self.root, scope=doctor.SCOPE_FULL, findings=[f])
        self.assertEqual(doctor.healthy_condition_ids(report), ["playwright_browser_missing"])
        self.assertEqual(doctor.deferral_entries_for(report), [])

    def test_unknown_is_never_a_problem(self) -> None:
        [f] = self._run(pm.registration_entry(), self._state("unknown"))
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertEqual(f.condition_id, "")

    def test_skip_env_is_reported_not_deferred(self) -> None:
        [f] = self._run(pm.registration_entry(), self._state("missing"),
                        environ={"VCT_SKIP_PLAYWRIGHT": "1"})
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)
        self.assertEqual(f.condition_id, "")

    def test_probe_is_registered_full_scope(self) -> None:
        fn, scopes = doctor.PROBES["playwright_browser"]
        self.assertIs(fn, doctor.probe_playwright_browser)
        self.assertEqual(scopes, (doctor.SCOPE_FULL,))
        self.assertIn("playwright_browser_missing", doctor.DOCTOR_OWNED_CIDS)
        self.assertIn("playwright_browser_missing", doctor.DOCTOR_SELF_RESOLVING_CIDS)


class ClearProbeAndRegistryTests(unittest.TestCase):
    def test_registry_row(self) -> None:
        data = tomllib.loads((REPO_ROOT / "vco_lib" / "deferral_conditions.toml")
                             .read_text(encoding="utf-8"))
        row = data["conditions"]["playwright_browser_missing"]
        self.assertEqual(row["class"], "action_required")
        self.assertEqual(row["clear_probe"], "probe:py:playwright_browser_still_missing")
        self.assertIs(deferral_probes.PROBES["playwright_browser_still_missing"],
                      deferral_probes.playwright_browser_still_missing)

    def test_clear_probe_tristate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = deferral_probes.ProbeContext(folder=root)
            self.assertIsNone(deferral_probes.playwright_browser_still_missing(ctx))
            (root / "vco_lib").mkdir()
            (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
            pin = pm.load_pin()
            for which, want in (("present", False), ("missing", True), ("unknown", None)):
                with self.subTest(which=which), mock.patch.object(
                        pm, "browser_state",
                        return_value=pm.BrowserState(which, pin, "r")):
                    self.assertIs(deferral_probes.playwright_browser_still_missing(ctx), want)


if __name__ == "__main__":
    unittest.main()
