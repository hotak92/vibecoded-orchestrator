# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the LIVE transition: an already-damaged venv is repaired.

``test_v0292_install_editable_integrity.py`` pins the decisions. This file
pins what ``pip`` actually DOES, because the whole defect came from assuming
pip's behaviour instead of measuring it.

It builds a throwaway venv plus a synthetic source tree that mirrors the real
packaging shape (hatchling backend, ``packages = ["vco_lib"]``, an optional
``codegraph-ts`` extra) and uses the REAL distribution name
``vibecoded-orchestrator``. Using the real name is deliberate: it is what makes
this test prove that ``VCO_DIST_INFO_GLOB`` / ``VCO_PACKAGE_NAME`` match the
directory names pip really writes. It is safe because everything happens inside
a temporary venv that is deleted afterwards — the caller's environment is never
touched.

Three transitions, in the order a real user lives them:

1. **The damage.** The pre-v0.2.92 argv (``pip install <root>[extra]``, no
   ``-e``) is replayed verbatim and asserted to produce the exact broken shape
   observed in the field: ``direct_url.json`` without ``"editable": true``,
   ``RECORD`` rows for ``vco_lib/`` files, a real ``vco_lib/`` directory in
   ``site-packages``, and ``import vco_lib`` from a NEUTRAL cwd landing there.
   This is the RED half — it is the bug, reproduced.

2. **The repair, leg 1 (pip's own).** The fixed argv — taken from
   ``codegraph_ts_install_plan``, not hand-written — is run against that broken
   venv. End state: editable, no ``vco_lib/`` directory left behind, and the
   neutral-cwd import back on the checkout. This is the answer for every user
   who is ALREADY damaged: their next update repairs them.

3. **The repair, leg 2 (the residue sweep).** A copy whose dist-info was lost
   is UNOWNED — pip cannot uninstall files it has no RECORD for, so leg 1 leaves
   it in place and it keeps shadowing (hatchling's editable install is a plain
   path-entry ``.pth``, and ``site-packages`` precedes it on ``sys.path``).
   ``repair_shadowed_vco_lib`` is asserted to remove exactly that, and to
   re-measure rather than assume.

Skip-gating follows ``test_wheel_install.py``: venv creation and the first pip
call are attempted, and any failure (no network for the build backend, no
``ensurepip``, offline CI) raises a LOUD ``SkipTest`` naming the reason, so an
accidental skip is visible rather than silent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.install_companions import (  # noqa: E402
    ORIGIN_CHECKOUT,
    ORIGIN_SITE_PACKAGES,
    VCO_DIST_INFO_GLOB,
    VCO_PACKAGE_NAME,
    classify_vco_lib_origin,
    codegraph_ts_install_plan,
    measure_vco_lib_origin,
    read_vco_dist_shape,
    repair_shadowed_vco_lib,
)

_VENV_TIMEOUT_S = 120
_PIP_TIMEOUT_S = 300

# The robustness flags install.py splices into every pip install
# (``install.py::_pip_install_flags``). Replayed here so the argv under test is
# the one users actually get.
_PIP_FLAGS = ["--timeout", "60", "--retries", "5", "--prefer-binary"]

_PYPROJECT = '''[build-system]
requires = ["hatchling >= 1.18"]
build-backend = "hatchling.build"

[project]
name = "vibecoded-orchestrator"
version = "0.2.92"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
"codegraph-ts" = []

[tool.hatch.build.targets.wheel]
packages = ["vco_lib"]
'''


class EditableTransitionTests(unittest.TestCase):
    """Live pip behaviour. One venv for the whole class — the transitions are
    sequential by nature (you cannot repair a venv you have not broken)."""

    tmp: Path
    repo: Path
    venv: Path
    venv_python: Path
    site: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="vco-editable-transition-"))
        cls.repo = cls.tmp / "checkout"
        (cls.repo / VCO_PACKAGE_NAME).mkdir(parents=True)
        (cls.repo / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (cls.repo / VCO_PACKAGE_NAME / "__init__.py").write_text(
            'MARKER = "checkout"\n', encoding="utf-8"
        )

        cls.venv = cls.tmp / "venv"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(cls.venv)],
                capture_output=True, text=True, timeout=_VENV_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            cls._bail(f"could not create a venv: {exc}")
        if result.returncode != 0:
            cls._bail(
                "venv creation failed (no ensurepip?): "
                f"{result.stderr.strip()[-300:]}"
            )

        sub = "Scripts" if sys.platform.startswith("win") else "bin"
        exe = "python.exe" if sys.platform.startswith("win") else "python"
        cls.venv_python = cls.venv / sub / exe
        if not cls.venv_python.exists():
            cls._bail(f"venv has no interpreter at {cls.venv_python}")

        # First pip call doubles as the network/build-backend gate: hatchling is
        # fetched here (or served from pip's cache) and nothing later can work
        # without it.
        first = cls._pip(["install", *_PIP_FLAGS, "-e", str(cls.repo)])
        if first.returncode != 0:
            cls._bail(
                "the initial editable install failed — no network for the "
                "build backend, or pip is unusable. stderr tail: "
                f"{first.stderr.strip()[-400:]}"
            )
        cls.site = cls._purelib()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    @classmethod
    def _bail(cls, reason: str):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)
        raise unittest.SkipTest(f"live pip transition test skipped: {reason}")

    @classmethod
    def _pip(cls, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(cls.venv_python), "-m", "pip", *argv],
            capture_output=True, text=True, timeout=_PIP_TIMEOUT_S,
            cwd=str(cls.repo),
        )

    @classmethod
    def _purelib(cls) -> Path:
        payload = measure_vco_lib_origin(cls.venv_python) or {}
        purelib = payload.get("purelib") or ""
        if not purelib:
            cls._bail("could not read the venv's purelib")
        return Path(purelib)

    # -- helpers ----------------------------------------------------------

    def _origin_state(self) -> tuple[str, str]:
        payload = measure_vco_lib_origin(self.venv_python) or {}
        origin = payload.get("origin") or ""
        state, _detail = classify_vco_lib_origin(
            origin=origin,
            install_root=str(self.repo),
            site_packages=payload.get("purelib") or str(self.site),
        )
        return state, origin

    def _dist_info(self) -> Path | None:
        matches = sorted(self.site.glob(VCO_DIST_INFO_GLOB))
        return matches[0] if len(matches) == 1 else None

    def _record_package_rows(self) -> list[str]:
        di = self._dist_info()
        if di is None:
            return []
        record = di / "RECORD"
        if not record.is_file():
            return []
        return [
            line for line in record.read_text(encoding="utf-8").splitlines()
            if line.startswith(f"{VCO_PACKAGE_NAME}/")
        ]

    def _break_it(self) -> None:
        """Replay the PRE-v0.2.92 argv, verbatim.

        Kept as a literal rather than derived from ``codegraph_ts_install_plan``
        on purpose: the point of this test is that the OLD shape breaks and the
        NEW one does not, so the old shape must not follow the function when it
        changes again.
        """
        result = self._pip(
            ["install", *_PIP_FLAGS, f"{self.repo}[codegraph-ts]"]
        )
        self.assertEqual(
            result.returncode, 0,
            f"could not reproduce the broken state: {result.stderr[-400:]}",
        )

    def _fixed_argv(self) -> list[str]:
        should, _reason, argv = codegraph_ts_install_plan(
            pyproject_exists=True, skip_env=False, project_root=str(self.repo),
        )
        self.assertTrue(should)
        assert argv is not None
        return ["install", *_PIP_FLAGS, *argv]

    # -- 1. the damage ----------------------------------------------------

    def test_1_pre_fix_argv_reproduces_the_field_defect(self):
        """RED HALF: this is the bug, measured rather than described."""
        self._break_it()

        editable, url, _path = read_vco_dist_shape(self.site)
        self.assertIs(
            editable, False,
            "expected a NON-editable direct_url — that is the defect's "
            "signature (an editable install carries dir_info.editable=true)",
        )
        self.assertTrue(url.startswith("file:"))

        self.assertTrue(
            self._record_package_rows(),
            "expected RECORD to list copied vco_lib/ files",
        )
        self.assertTrue(
            (self.site / VCO_PACKAGE_NAME / "__init__.py").is_file(),
            "expected a real vco_lib/ package copied into site-packages",
        )
        self.assertFalse(
            list(self.site.glob("_editable_impl_vibecoded_orchestrator*.pth")),
            "the editable .pth should have been removed by the reinstall",
        )

        state, origin = self._origin_state()
        self.assertEqual(
            state, ORIGIN_SITE_PACKAGES,
            f"import vco_lib from a neutral cwd should hit the copy; got {origin}",
        )

    # -- 2. the repair, leg 1 ---------------------------------------------

    def test_2_fixed_argv_repairs_an_already_damaged_venv(self):
        """The answer for users who are ALREADY broken: their next update fixes
        them, because pip's own uninstall step removes the copy it owns."""
        self._break_it()
        result = self._pip(self._fixed_argv())
        self.assertEqual(result.returncode, 0, result.stderr[-400:])

        editable, url, _path = read_vco_dist_shape(self.site)
        self.assertIs(editable, True, "end state must be an EDITABLE install")
        self.assertTrue(url.endswith(str(self.repo)))

        self.assertEqual(
            self._record_package_rows(), [],
            "an editable install must not own copied vco_lib/ files",
        )
        self.assertFalse(
            (self.site / VCO_PACKAGE_NAME).exists(),
            "the copied package directory must be gone from site-packages",
        )

        state, origin = self._origin_state()
        self.assertEqual(
            state, ORIGIN_CHECKOUT,
            f"import vco_lib should resolve to the checkout; got {origin}",
        )

    def test_3_fixed_argv_on_a_healthy_venv_keeps_it_healthy(self):
        """LEAVE-ALONE: running the fixed step twice must be idempotent."""
        self._pip(self._fixed_argv())
        result = self._pip(self._fixed_argv())
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        editable, _url, _p = read_vco_dist_shape(self.site)
        self.assertIs(editable, True)
        self.assertEqual(self._origin_state()[0], ORIGIN_CHECKOUT)
        self.assertFalse((self.site / VCO_PACKAGE_NAME).exists())

    # -- 3. the repair, leg 2 ---------------------------------------------

    def test_4_unowned_copy_survives_pip_and_is_swept_by_the_repair(self):
        """The residue leg pip structurally cannot reach.

        Losing the dist-info is what makes a copy UNOWNED: pip has no RECORD
        for those files, so its uninstall step cannot see them. The copy then
        outlives every future editable install and keeps shadowing the
        checkout — which is precisely the condition CLAUDE.md documents as
        needing a manual PYTHONPATH workaround. This asserts install.py's sweep
        makes that manual step unnecessary.
        """
        self._break_it()
        dist_info = self._dist_info()
        self.assertIsNotNone(dist_info)
        assert dist_info is not None
        shutil.rmtree(dist_info)

        result = self._pip(self._fixed_argv())
        self.assertEqual(result.returncode, 0, result.stderr[-400:])

        # pip reports success and the install IS editable...
        editable, _url, _p = read_vco_dist_shape(self.site)
        self.assertIs(editable, True)
        # ...yet the unowned copy still wins. This is the finding that makes
        # the sweep necessary rather than belt-and-braces.
        state, origin = self._origin_state()
        self.assertEqual(
            state, ORIGIN_SITE_PACKAGES,
            "an unowned copy must still shadow after a plain editable "
            f"reinstall (got {origin})",
        )

        outcome = repair_shadowed_vco_lib(self.repo, self.venv_python)
        self.assertEqual(outcome["action"], "removed", outcome)
        self.assertEqual(
            Path(outcome["removed_path"]), self.site / VCO_PACKAGE_NAME
        )
        self.assertEqual(
            outcome["verified_state"], ORIGIN_CHECKOUT,
            "the repair must RE-MEASURE, not assume the delete worked",
        )
        self.assertFalse((self.site / VCO_PACKAGE_NAME).exists())
        self.assertEqual(self._origin_state()[0], ORIGIN_CHECKOUT)

    def test_5_repair_is_a_noop_on_a_healthy_venv(self):
        """LEAVE-ALONE, live: nothing is deleted when nothing is wrong."""
        self._pip(self._fixed_argv())
        before = sorted(p.name for p in self.site.iterdir())
        outcome = repair_shadowed_vco_lib(self.repo, self.venv_python)
        self.assertEqual(outcome["action"], "ok", outcome)
        self.assertEqual(outcome["state"], ORIGIN_CHECKOUT)
        self.assertEqual(sorted(p.name for p in self.site.iterdir()), before)

    def test_6_direct_url_payload_matches_the_documented_shapes(self):
        """The verbatim JSON both docstrings quote. If pip ever changes it,
        this is where we find out — not in the field."""
        self._break_it()
        di = self._dist_info()
        assert di is not None
        broken = json.loads((di / "direct_url.json").read_text(encoding="utf-8"))
        self.assertEqual(broken.get("dir_info"), {})

        self._pip(self._fixed_argv())
        di = self._dist_info()
        assert di is not None
        fixed = json.loads((di / "direct_url.json").read_text(encoding="utf-8"))
        self.assertEqual(fixed.get("dir_info"), {"editable": True})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
