# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco_lib.python_exe`` — the ladder, the preflight, and the loud failure.

The behaviour under test is the fix for the 2026-09-09 field defect: eight
projects were told "code-graph re-index started in the background" while every
detached child died on ``ModuleNotFoundError: No module named 'vco_lib'``,
because the interpreter came from a bare PATH probe rather than from the
orchestrator venv. The rules this file pins:

  1. the ladder's ORDER is behaviour (``$VCT_VENV`` beats the install root);
  2. ``sys.executable`` is a rung, not a default — it is taken only when it
     PASSES a preflight;
  3. a resolver that cannot answer RAISES, naming every candidate it tried. A
     silent ``"python3"`` is the defect, not the fallback.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import python_exe as px  # noqa: E402

_LADDER_ENV = (px.VENV_ENV_VAR, *px.INSTALL_ROOT_ENV_VARS)


class _LadderBase(unittest.TestCase):
    """A tmp tree plus FULL control of the ladder's env inputs.

    The env has to be scrubbed rather than merely set: this suite runs from a
    real orchestrator checkout, whose own `$VCT_INSTALL_ROOT` would otherwise
    answer for the fixtures and make every assertion vacuous.
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved = {k: os.environ.get(k) for k in _LADDER_ENV}
        for k in _LADDER_ENV:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)
        px.clear_preflight_cache()
        self.addCleanup(px.clear_preflight_cache)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def make_clone(self, name: str = "clone", *, venv: str = ".venv",
                   interpreter: str = "bin/python") -> tuple[Path, Path]:
        """A directory `looks_like_orchestrator_root` accepts, with a fake venv."""
        clone = self.root / name
        (clone / "vco_lib").mkdir(parents=True, exist_ok=True)
        (clone / ".claude").mkdir(parents=True, exist_ok=True)
        py = clone / venv / interpreter
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IEXEC)
        return clone, py


class LadderOrder(_LadderBase):
    def test_install_root_venv_is_found(self):
        clone, py = self.make_clone()
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_vco_lib_python(), py)

    def test_the_legacy_layout_still_resolves(self):
        clone, py = self.make_clone(venv="claude_mcp_servers/.venv")
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_vco_lib_python(), py)

    def test_the_modern_layout_wins_over_the_legacy_one(self):
        clone, modern = self.make_clone()
        legacy = clone / "claude_mcp_servers" / ".venv" / "bin" / "python"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_vco_lib_python(), modern,
                         "`.venv` is probed before the pre-v0.2.74 location")

    def test_bin_python3_is_probed_when_bin_python_is_absent(self):
        clone, py = self.make_clone(interpreter="bin/python3")
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_vco_lib_python(), py)

    def test_vct_venv_overrides_the_install_root(self):
        clone, root_py = self.make_clone()
        override = self.root / "elsewhere"
        override_py = override / "bin" / "python"
        override_py.parent.mkdir(parents=True)
        override_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        os.environ["VCT_VENV"] = str(override)
        self.assertEqual(px.resolve_vco_lib_python(), override_py)
        self.assertNotEqual(px.resolve_vco_lib_python(), root_py)

    def test_vct_venv_may_name_the_interpreter_itself(self):
        binary = self.root / "some-python"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.environ["VCT_VENV"] = str(binary)
        self.assertEqual(px.resolve_vco_lib_python(), binary)

    def test_windows_layout_resolves_from_a_posix_runner(self):
        """R6/9: no `os_name` seam — the Windows name is simply in the list.

        Both sides probe `bin/python`, `bin/python3` AND `Scripts/python.exe` on
        every OS, so a Windows-shaped venv resolves here with no injection, and
        an MSYS/Git-Bash venv (`bin/`, on Windows) resolves there.
        """
        clone = self.root / "winclone"
        (clone / "vco_lib").mkdir(parents=True)
        (clone / ".claude").mkdir(parents=True)
        py = clone / ".venv" / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_vco_lib_python(), py)

    def test_a_stale_install_root_env_does_not_win(self):
        """A moved clone leaves `$VCT_INSTALL_ROOT` pointing at nothing.

        The env value must be VALIDATED (`looks_like_orchestrator_root`) rather
        than trusted — the same class of bug as the 2026-09-05 kg-sync wrapper
        still naming a previous orchestrator location.

        HERMETIC: the package-relative fallback and the preflight are both
        pinned, so this measures the LADDER, not whether the machine running the
        tests happens to have a venv.
        """
        dead = self.root / "moved-away"
        os.environ["VCT_INSTALL_ROOT"] = str(dead)
        # No clone below the dead path, and no `sys.executable` rescue.
        with mock.patch.object(px, "resolve_install_root", return_value=None), \
                mock.patch.object(px, "preflight", return_value=(False, "nope")):
            with self.assertRaises(px.PythonExeUnresolved) as ctx:
                px.resolve_vco_lib_python()
        self.assertNotIn(
            str(dead), str(ctx.exception),
            "a stale env value must never be offered as a candidate",
        )

        # And when the fallback DOES resolve a clone, that clone is what wins —
        # never the stale env path.
        clone, py = self.make_clone("real-clone")
        with mock.patch.object(px, "resolve_install_root", return_value=clone):
            self.assertEqual(px.resolve_vco_lib_python(), py)


class SysExecutableIsARungNotADefault(_LadderBase):
    def test_taken_when_it_passes_the_preflight(self):
        os.environ["VCT_INSTALL_ROOT"] = str(self.root / "nothing-here")
        with mock.patch.object(px, "resolve_install_root", return_value=None), \
                mock.patch.object(px, "preflight", return_value=(True, "")):
            self.assertEqual(px.resolve_vco_lib_python(), Path(sys.executable))

    def test_refused_when_it_fails_the_preflight(self):
        with mock.patch.object(px, "resolve_install_root", return_value=None), \
                mock.patch.object(
                    px, "preflight",
                    return_value=(False, "/usr/bin/python3 cannot import weaviate"),
                ):
            with self.assertRaises(px.PythonExeUnresolved) as ctx:
                px.resolve_vco_lib_python()
        msg = str(ctx.exception)
        self.assertIn("cannot import weaviate", msg,
                      "the failure must name WHY the interpreter was rejected")
        self.assertIn("install.py --update", msg,
                      "and must tell the user what to do about it")
        self.assertNotIn(
            "python3'", msg.split("Tried:")[0],
            "the headline must not read as if a fallback were taken",
        )

    def test_the_failure_names_every_candidate_it_tried(self):
        os.environ["VCT_VENV"] = str(self.root / "no-venv-here")
        clone = self.root / "clone"
        (clone / "vco_lib").mkdir(parents=True)
        (clone / ".claude").mkdir(parents=True)
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        with mock.patch.object(px, "preflight", return_value=(False, "nope")):
            with self.assertRaises(px.PythonExeUnresolved) as ctx:
                px.resolve_vco_lib_python()
        exc = ctx.exception
        tiers = {c.tier for c in exc.candidates}
        self.assertIn(px.TIER_VCT_VENV, tiers)
        self.assertIn(f"{px.TIER_INSTALL_ROOT}:.venv", tiers)
        self.assertIn(f"{px.TIER_INSTALL_ROOT}:claude_mcp_servers/.venv", tiers)
        self.assertIn(px.TIER_SYS_EXECUTABLE, tiers)
        self.assertIn(str(clone / ".venv"), str(exc))

    def test_or_none_logs_instead_of_raising(self):
        with mock.patch.object(px, "resolve_install_root", return_value=None), \
                mock.patch.object(px, "preflight", return_value=(False, "nope")):
            self.assertIsNone(px.resolve_vco_lib_python_or_none())

    def test_resolve_or_current_never_returns_a_bare_name_when_a_venv_exists(self):
        clone, py = self.make_clone()
        os.environ["VCT_INSTALL_ROOT"] = str(clone)
        self.assertEqual(px.resolve_or_current(), str(py))

    def test_resolve_or_current_degrades_to_the_running_interpreter(self):
        """The migration shim's contract: strictly better than `sys.executable`,
        never worse. Call-sites whose failure mode is already handled (a
        non-zero exit the caller reports) use this rather than the raising
        resolver, so a recovery path cannot be made WORSE by the fix."""
        with mock.patch.object(px, "resolve_install_root", return_value=None), \
                mock.patch.object(px, "preflight", return_value=(False, "nope")):
            self.assertEqual(px.resolve_or_current(), sys.executable)


class Preflight(_LadderBase):
    """The probe's DECISION logic — hermetic (the subprocess is injected).

    Measuring the runner's own interpreter would measure the machine, not the
    tree: on a box without `weaviate` these would fail for a reason that has
    nothing to do with the code under test. The ONE test that runs a real
    interpreter is the integration red-proof at the end, which skips with a
    reason when `python -m venv` is unavailable.
    """

    @staticmethod
    def _reports(missing: "list[str] | None" = None, *, rc: int = 0, stderr: str = ""):
        """A runner that answers with the probe's own stdout contract."""

        def _runner(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv, rc, stdout=json.dumps(missing or []) + "\n", stderr=stderr,
            )

        return _runner

    def test_an_interpreter_that_imports_everything_passes(self):
        ok, detail = px.preflight(
            "/x/python", runner=self._reports([]), use_cache=False,
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_a_real_venv_without_the_editable_install_is_rejected_by_name(self):
        """RED-PROOF, with a REAL interpreter — no stub, no mock.

        `python -m venv` gives an interpreter that is perfectly healthy and
        cannot import `vco_lib` or `weaviate`: exactly the shape of the
        `/usr/bin/python3` the launcher's bundle path was handing to every
        detached child on 2026-09-09. Under the OLD code this interpreter would
        have been spawned and the caller told "launched".
        """
        venv_root = self.root / "bare-venv"
        try:
            # DELIBERATELY UNPINNED — no `child_env()` here (allowlisted in
            # tests/test_v0292_fixround_child_env_lint.py). `child_env()` puts
            # the checkout on the child's path, and an interpreter that CANNOT
            # import `vco_lib` is exactly the fixture being built.
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_root)],
                capture_output=True, timeout=180, check=True,
            )
        except Exception as exc:  # pragma: no cover — venv unavailable in CI image
            self.skipTest(f"could not build a bare venv: {exc}")
        bare = next(
            (p for p in px.venv_interpreters(venv_root) if p.is_file()), None
        )
        self.assertIsNotNone(bare, "the fixture venv has no interpreter")

        ok, detail = px.preflight(bare, use_cache=False)
        self.assertFalse(
            ok, "a venv without our editable install must NOT pass the preflight",
        )
        # Which module is named depends on the inherited env — deliberately.
        # The probe does NOT scrub `PYTHONPATH`, because the child it gates
        # inherits it too: under a `PYTHONPATH` that really does make `vco_lib`
        # importable, `vco_lib` legitimately passes and `weaviate` is what
        # catches this interpreter. Either way it is REJECTED, which is the
        # property under test.
        self.assertTrue(
            any(m in detail for m in px.DEFAULT_REQUIRED_MODULES),
            f"the rejection must name the missing module; got {detail!r}",
        )
        self.assertIn(str(bare), detail,
                      "the rejection must name the interpreter, not just the module")

        # And with no PYTHONPATH help — the state a detached child in a USER
        # project actually runs in — the message is the field one verbatim.
        saved = os.environ.pop("PYTHONPATH", None)
        try:
            ok2, detail2 = px.preflight(bare, use_cache=False)
        finally:
            if saved is not None:
                os.environ["PYTHONPATH"] = saved
        self.assertFalse(ok2)
        self.assertIn("vco_lib", detail2)
        self.assertIn(
            "No module named 'vco_lib'", detail2,
            "this is the exact line the 8 field resync logs contained",
        )

    def test_a_python_that_reports_a_missing_module_is_rejected_by_name(self):
        """The same verdict from the probe's stdout contract alone (fast path)."""
        fake = self.root / "fakepy" / "bin" / "python3"
        fake.parent.mkdir(parents=True)
        fake.write_text(
            "#!/bin/sh\n"
            "cat <<'EOF'\n"
            "[\"weaviate: ModuleNotFoundError: No module named 'weaviate'\"]\n"
            "EOF\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        ok, detail = px.preflight(fake, use_cache=False)
        self.assertFalse(ok)
        self.assertIn("weaviate", detail)
        self.assertIn(str(fake), detail,
                      "the rejection must name the interpreter, not just the module")

    def test_a_nonexistent_interpreter_is_rejected_not_raised(self):
        ok, detail = px.preflight(self.root / "not-a-python", use_cache=False)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_a_probe_that_explodes_is_still_only_a_verdict(self):
        with mock.patch.object(px.subprocess, "run", side_effect=RuntimeError("boom")):
            ok, detail = px.preflight("/anything", use_cache=False)
        self.assertFalse(ok)
        self.assertIn("boom", detail)

    def test_the_verdict_is_cached_per_interpreter(self):
        calls = {"n": 0}

        def _runner(*a, **k):
            calls["n"] += 1
            return subprocess.CompletedProcess(a[0], 0, stdout="[]\n", stderr="")

        px.clear_preflight_cache()
        for _ in range(3):
            self.assertEqual(px.preflight("/x/python", runner=_runner), (True, ""))
        self.assertEqual(calls["n"], 1, "an 8-project update must pay for ONE probe")
        px.preflight("/y/python", runner=_runner)
        self.assertEqual(calls["n"], 2, "a DIFFERENT interpreter is probed again")

    def test_the_memo_is_invalidated_by_the_env_that_decides_the_answer(self):
        """R6/8: a cached verdict must not outlive the env that produced it.

        `PYTHONPATH` and `VIRTUAL_ENV` both change what an interpreter can
        import, and both legitimately change mid-process (a caller pinning a
        checkout; a venv being created). Keying on them means a step that FIXES
        an interpreter is not overruled by a stale "no".
        """
        calls = {"n": 0}

        def _runner(*a, **k):
            calls["n"] += 1
            missing = [] if os.environ.get("PYTHONPATH") else ["vco_lib: ModuleNotFoundError"]
            return subprocess.CompletedProcess(
                a[0], 0, stdout=json.dumps(missing) + "\n", stderr="",
            )

        px.clear_preflight_cache()
        saved = os.environ.pop("PYTHONPATH", None)
        try:
            self.assertFalse(px.preflight("/x/python", runner=_runner)[0])
            os.environ["PYTHONPATH"] = str(self.root)
            ok, _ = px.preflight("/x/python", runner=_runner)
        finally:
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved
        self.assertTrue(ok, "the stale negative verdict must not have been reused")
        self.assertEqual(calls["n"], 2, "the changed env must force a re-probe")

    def test_clear_preflight_cache_covers_the_same_interpreter_changing(self):
        """The case the env key CANNOT cover: packages change under a fixed env
        (a `pip install` into the resolved venv from inside this process).

        No caller needs it today — the only in-process preflight runs AFTER the
        install — but the escape hatch has to exist and has to say so, for the
        future caller that inverts that order."""
        calls = {"n": 0}
        answers = iter([["weaviate: ModuleNotFoundError"], []])

        def _runner(*a, **k):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                a[0], 0, stdout=json.dumps(next(answers)) + "\n", stderr="",
            )

        px.clear_preflight_cache()
        self.assertFalse(px.preflight("/x/python", runner=_runner)[0])
        self.assertFalse(px.preflight("/x/python", runner=_runner)[0])  # memo
        px.clear_preflight_cache()
        self.assertTrue(px.preflight("/x/python", runner=_runner)[0])
        self.assertEqual(calls["n"], 2)
        doc = px.clear_preflight_cache.__doc__ or ""
        self.assertIn(
            "none does today", doc,
            "the docstring must state the CURRENT truth about who needs this — "
            "an obligation on a caller that has none is a promise nothing keeps",
        )

    def test_the_probe_runs_from_a_neutral_cwd(self):
        """The illusion that hid the field defect.

        `project_init` imported `vco_lib` fine — from its cwd. Its detached
        grandchild, cwd = the user project, could not. A probe run from the
        orchestrator clone would inherit the same illusion and bless a system
        python, so the probe must not run there.
        """
        seen = {}

        def _runner(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="[]\n", stderr="")

        px.preflight("/x/python", runner=_runner, use_cache=False)
        cwd = Path(seen["cwd"]).resolve()
        self.assertNotEqual(cwd, REPO_ROOT)
        self.assertFalse(
            str(cwd).startswith(str(REPO_ROOT)),
            f"the preflight must not run from inside the checkout (cwd={cwd})",
        )

    def test_the_probe_asks_about_both_modules(self):
        seen = {}

        def _runner(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="[]\n", stderr="")

        px.preflight("/x/python", runner=_runner, use_cache=False)
        self.assertEqual(seen["argv"][-2:], ["vco_lib", "weaviate"])
        self.assertEqual(px.DEFAULT_REQUIRED_MODULES, ("vco_lib", "weaviate"))


if __name__ == "__main__":
    unittest.main()
