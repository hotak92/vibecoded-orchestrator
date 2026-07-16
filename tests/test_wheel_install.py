# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Wheel-install regression test (B3 ship-blocker — v0.2.34).

Pre-fix failure mode
--------------------
``pyproject.toml`` ships only ``packages = ["vco_lib"]`` in the wheel.
``bundled_mcp_versions.toml`` lived at the repo root and was therefore
silently dropped from the wheel. ``vco_lib/bundled_versions.py`` resolved
the manifest path as ``Path(__file__).parent.parent / "bundled_mcp_versions.toml"``
(i.e. next to ``vco_lib/``, which is the repo root in a source
checkout). That path doesn't exist in a wheel-installed environment, so
``load_bundled_versions()`` raised ``RuntimeError`` — and the wrapper
MCPs (``claude_mcp_servers.wrappers.mermaid_proxy`` /
``excalidraw_proxy``) ``SystemExit(1)`` on the first spawn rather than
register with Claude Code. Users saw "MCP failed to connect" with no
actionable error.

The v0.2.34 fix moved the manifest into ``vco_lib/bundled_mcp_versions.toml``
(now a sibling of the loader) and updated the loader to
``Path(__file__).parent / "bundled_mcp_versions.toml"``. Same move for
the vendored ``excalidraw_mcp_fork/`` tree (used by ``excalidraw_proxy``
to spawn the Node entry point — kept at the same relative-to-repo-root
path so install.py's ``file:`` pin install still works in a source
checkout).

What this test pins
-------------------
* The built wheel CONTAINS ``vco_lib/bundled_mcp_versions.toml``
  (zipfile inspection — no install needed; catches the packaging
  regression directly).
* The built wheel CONTAINS the vendored Excalidraw fork's ``dist/mcp/index.js``
  entry point under ``vco_lib/excalidraw_mcp_fork/`` (same shape).
* A fresh venv with the wheel installed can import
  ``vco_lib.bundled_versions.load_bundled_versions`` and get a non-empty
  dict back — i.e. the runtime-resolved manifest path works post-install.
* A fresh venv with the wheel installed can import
  ``vco_lib.bundled_versions.manifest_path`` and the returned path is a
  REAL FILE inside the venv's site-packages (the wheel-install
  positive control — no source-checkout fallback).

The wrapper-MCP spawn (``python -m claude_mcp_servers.wrappers.mermaid_proxy``)
is NOT exercised here because ``claude_mcp_servers/`` is not in the
wheel by design (the wrappers are launcher-MCP infrastructure, not
library code). The wrapper IS exercised by ``test_mermaid_proxy.py``
and ``test_excalidraw_proxy.py``; the regression that THIS test guards
is the import-time ``load_bundled_versions()`` failure, which is the
proximate cause of the wrapper ``SystemExit(1)``.

Build mechanics
---------------
We build a one-shot wheel into a tempdir via ``python -m build --wheel``
and pip-install it into a tempdir venv. Both wheel-build and venv
creation are conditional-skipped if ``build`` / ``venv`` aren't
available so the test degrades gracefully on minimal environments
(some CI runners don't ship ``pip install build`` by default). The skip
condition is logged so an accidental skip is visible.

Test isolation
--------------
The tempdir venv is deleted in ``tearDownClass`` — never touches the
caller's site-packages. The wheel build runs in ``--no-isolation`` mode
when possible to avoid re-downloading hatchling; falls back to isolated
build if the dev environment lacks hatchling. Timeout is generous
(180 s) because hatchling's first-run setup + pip wheel install can be
slow on cold caches.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Conservative timeout — wheel build + venv create + pip install adds up.
_BUILD_TIMEOUT_S = 180
_INSTALL_TIMEOUT_S = 180
_SUBPROCESS_TIMEOUT_S = 60


def _has_module(name: str) -> bool:
    """True if the current Python can import ``name``. Used to skip
    gracefully when build deps aren't present."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


class WheelPackagingTests(unittest.TestCase):
    """Static (no-install) checks on the built wheel. Always run when
    ``python -m build`` is available; otherwise skip the whole class."""

    wheel_path: Path | None = None
    build_dir: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not _has_module("build"):
            raise unittest.SkipTest(
                "`build` package not installed in test env — skipping "
                "wheel-packaging tests. Install via `pip install build` "
                "to enable.",
            )
        cls.build_dir = Path(tempfile.mkdtemp(prefix="vco-wheel-test-"))
        # Build into the temp dir. --wheel skips the sdist; we only need
        # the wheel for the packaging assertions.
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel",
             "--outdir", str(cls.build_dir),
             str(REPO_ROOT)],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise unittest.SkipTest(
                f"wheel build failed (returncode={result.returncode}). "
                f"stderr tail: {result.stderr.strip()[-500:]}",
            )
        # Find the produced wheel (there should be exactly one).
        wheels = list(cls.build_dir.glob("vibecoded_orchestrator-*.whl"))
        if len(wheels) != 1:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise unittest.SkipTest(
                f"expected exactly one wheel under {cls.build_dir}, "
                f"found {len(wheels)}: {wheels!r}",
            )
        cls.wheel_path = wheels[0]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.build_dir is not None and cls.build_dir.exists():
            shutil.rmtree(cls.build_dir, ignore_errors=True)

    # ─── Static wheel-content checks (no install required) ─────────────

    def test_wheel_includes_bundled_mcp_versions_toml(self) -> None:
        """The B3 regression: ``bundled_mcp_versions.toml`` MUST be a
        member of the built wheel. If this fails, the wrapper MCPs will
        ``SystemExit(1)`` on first spawn post-install. See module
        docstring for the full failure-mode chain."""
        assert self.wheel_path is not None
        with zipfile.ZipFile(self.wheel_path) as z:
            names = z.namelist()
        # The file must live at vco_lib/bundled_mcp_versions.toml inside
        # the wheel (mirror of the source-checkout layout post-v0.2.34).
        self.assertIn(
            "vco_lib/bundled_mcp_versions.toml", names,
            "vco_lib/bundled_mcp_versions.toml MUST be inside the wheel. "
            "Without it, `load_bundled_versions()` raises RuntimeError "
            "at wrapper-MCP startup → SystemExit(1) → 'MCP failed to "
            "connect' in Claude Code. See B3 regression note in this "
            "test's module docstring.",
        )

    def test_wheel_includes_mcp_scan_rules_toml(self) -> None:
        """v0.2.83 WP-B4: ``mcp_scan_rules.toml`` (the cross-language MCP
        scan/registration rule table) MUST be a member of the built wheel.
        Without it, ``vco_lib.mcp_scan_rules.load_mcp_scan_rules()`` raises
        RuntimeError at import of ``install_mcp`` (which reads the table for
        its allowlist / needles / entry-names / deprecated registry) →
        install / MCP-registration path breaks. Same wheel-inclusion
        discipline as ``bundled_mcp_versions.toml``."""
        assert self.wheel_path is not None
        with zipfile.ZipFile(self.wheel_path) as z:
            names = z.namelist()
        self.assertIn(
            "vco_lib/mcp_scan_rules.toml", names,
            "vco_lib/mcp_scan_rules.toml MUST be inside the wheel. Without "
            "it, vco_lib.install_mcp raises RuntimeError at import (it reads "
            "the table for the env allowlist, secret needles, default entry "
            "names, and deprecated registry).",
        )

    def test_wheel_includes_excalidraw_vendored_entrypoint(self) -> None:
        """The Excalidraw wrapper proxy spawns Node on
        ``vco_lib/excalidraw_mcp_fork/dist/mcp/index.js``. That file
        must be in the wheel (vendored tree shipped along with vco_lib
        per the v0.2.34 packaging fix)."""
        assert self.wheel_path is not None
        with zipfile.ZipFile(self.wheel_path) as z:
            names = z.namelist()
        self.assertIn(
            "vco_lib/excalidraw_mcp_fork/dist/mcp/index.js", names,
            "vco_lib/excalidraw_mcp_fork/dist/mcp/index.js MUST be in "
            "the wheel — it's the Node entry point that "
            "`claude_mcp_servers.wrappers.excalidraw_proxy` spawns. "
            "Without it, the Excalidraw MCP can't start post-install.",
        )

    def test_wheel_includes_excalidraw_package_json(self) -> None:
        """``install.py::_install_pinned_npm`` reads
        ``<vendor-dir>/package.json`` for the file: pin path. The
        vendored package.json must travel inside the wheel."""
        assert self.wheel_path is not None
        with zipfile.ZipFile(self.wheel_path) as z:
            names = z.namelist()
        self.assertIn(
            "vco_lib/excalidraw_mcp_fork/package.json", names,
            "vco_lib/excalidraw_mcp_fork/package.json MUST be in the "
            "wheel — install.py reads it to resolve the vendored npm "
            "package name + version for the file: pin install path.",
        )

    def test_wheel_does_NOT_ship_repo_root_bundled_toml_copy(self) -> None:
        """Belt-and-braces: there must NOT be a second copy of the
        manifest at the repo root (pre-v0.2.34 location). A double-include
        via hatch ``force-include`` would be a worse fix — sdist/wheel
        get out-of-sync over time. The move-under-vco_lib choice in
        v0.2.34 was DELIBERATE to avoid this."""
        assert self.wheel_path is not None
        with zipfile.ZipFile(self.wheel_path) as z:
            names = z.namelist()
        # Only the vco_lib-scoped path is allowed.
        toml_entries = [n for n in names if n.endswith("bundled_mcp_versions.toml")]
        self.assertEqual(
            toml_entries, ["vco_lib/bundled_mcp_versions.toml"],
            f"expected exactly one bundled_mcp_versions.toml in the "
            f"wheel at vco_lib/bundled_mcp_versions.toml, found "
            f"{toml_entries!r}",
        )


class WheelInstallRuntimeTests(unittest.TestCase):
    """End-to-end install+import test: pip-install the wheel into a
    throwaway venv, then have THAT venv import vco_lib.bundled_versions
    and prove load_bundled_versions() returns the real manifest.

    This is the closest pytest-friendly proxy for "Claude Code spawns
    the wrapper MCP and it doesn't die" — the wrapper's first action
    inside ``_resolve_upstream_argv`` is ``load_bundled_versions()``,
    so a successful import + call here means the wrapper wouldn't
    SystemExit(1) on the same Python.
    """

    wheel_path: Path | None = None
    build_dir: Path | None = None
    venv_dir: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not _has_module("build"):
            raise unittest.SkipTest(
                "`build` package not installed — skipping wheel-install "
                "runtime tests.",
            )

        # 1) Build the wheel.
        cls.build_dir = Path(tempfile.mkdtemp(prefix="vco-wheel-rt-"))
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel",
             "--outdir", str(cls.build_dir),
             str(REPO_ROOT)],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise unittest.SkipTest(
                f"wheel build failed: {result.stderr.strip()[-500:]}",
            )
        wheels = list(cls.build_dir.glob("vibecoded_orchestrator-*.whl"))
        if not wheels:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise unittest.SkipTest("no wheel produced by build step")
        cls.wheel_path = wheels[0]

        # 2) Create a fresh venv (separate from the test env so the
        # wheel install can't shadow vco_lib already present here).
        cls.venv_dir = Path(tempfile.mkdtemp(prefix="vco-wheel-venv-"))
        venv_result = subprocess.run(
            [sys.executable, "-m", "venv", str(cls.venv_dir)],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
        )
        if venv_result.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            shutil.rmtree(cls.venv_dir, ignore_errors=True)
            raise unittest.SkipTest(
                f"venv creation failed: {venv_result.stderr.strip()[-500:]}",
            )

        # 3) Install the wheel into the venv. No extras (we only need
        # the bare runtime — the wrapper's load_bundled_versions doesn't
        # depend on aiohttp/mcp/etc.).
        venv_python = cls._venv_python_path()
        install_result = subprocess.run(
            [str(venv_python), "-m", "pip", "install",
             "--quiet", "--no-deps", str(cls.wheel_path)],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
        )
        if install_result.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            shutil.rmtree(cls.venv_dir, ignore_errors=True)
            raise unittest.SkipTest(
                f"pip install of wheel failed: "
                f"{install_result.stderr.strip()[-500:]}",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.build_dir is not None and cls.build_dir.exists():
            shutil.rmtree(cls.build_dir, ignore_errors=True)
        if cls.venv_dir is not None and cls.venv_dir.exists():
            shutil.rmtree(cls.venv_dir, ignore_errors=True)

    @classmethod
    def _venv_python_path(cls) -> Path:
        """Return the Python interpreter inside the test venv (cross-OS)."""
        assert cls.venv_dir is not None
        # Linux/macOS: bin/python; Windows: Scripts/python.exe.
        candidates = [
            cls.venv_dir / "bin" / "python",
            cls.venv_dir / "bin" / "python3",
            cls.venv_dir / "Scripts" / "python.exe",
        ]
        for c in candidates:
            if c.exists():
                return c
        raise RuntimeError(
            f"could not find python interpreter under {cls.venv_dir} "
            f"(tried {candidates!r})",
        )

    def _run_in_venv(self, code: str) -> tuple[int, str, str]:
        """Execute ``code`` inside the wheel-installed venv. Returns
        (returncode, stdout, stderr). Used to import + call
        load_bundled_versions WITHOUT the repo-root vco_lib shadowing
        the wheel-installed one (we run the subprocess with cwd=/tmp,
        not REPO_ROOT, so `import vco_lib` resolves to site-packages)."""
        venv_python = self._venv_python_path()
        # cwd=tempfile.gettempdir() avoids accidentally picking up
        # REPO_ROOT/vco_lib via "implicit cwd in sys.path[0]" behaviour.
        # PYTHONPATH explicitly emptied for the same reason.
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONPATH", "PYTHONHOME")}
        return self._run_subprocess(
            [str(venv_python), "-c", code],
            cwd=tempfile.gettempdir(),
            env=env,
        )

    @staticmethod
    def _run_subprocess(argv: list[str], *, cwd: str, env: dict[str, str]) -> tuple[int, str, str]:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        return result.returncode, result.stdout, result.stderr

    # ─── Runtime checks (wheel-installed vco_lib only) ─────────────────

    def test_imports_and_loads_manifest_from_wheel(self) -> None:
        """Pre-fix this raised RuntimeError (manifest path resolved to
        site-packages/bundled_mcp_versions.toml which doesn't exist;
        the file lived at the repo root only). Post-fix the file is a
        sibling of bundled_versions.py inside the wheel, so the
        default-path branch finds it and tomllib parses it cleanly.

        Asserts non-empty parsed dict so a future regression that
        ships an empty .toml (or the .toml at the wrong path inside the
        wheel) also trips this test.
        """
        code = (
            "from vco_lib.bundled_versions import load_bundled_versions; "
            "v = load_bundled_versions(); "
            "assert isinstance(v, dict), 'not a dict'; "
            "assert 'npm' in v, 'no npm section'; "
            "assert v['npm'], 'empty npm section'; "
            "print('OK', len(v['npm']))"
        )
        rc, stdout, stderr = self._run_in_venv(code)
        self.assertEqual(
            rc, 0,
            f"wheel-installed vco_lib.bundled_versions failed at "
            f"runtime (exit={rc}). stderr:\n{stderr}\nstdout:\n{stdout}",
        )
        self.assertIn("OK", stdout, f"unexpected stdout: {stdout!r}")

    def test_manifest_path_resolves_inside_site_packages(self) -> None:
        """The manifest_path() must point at the wheel-installed copy
        (site-packages/vco_lib/bundled_mcp_versions.toml), NOT at any
        repo-root copy that happens to be on disk via accidental
        sys.path entries. This catches the "loader reads file but from
        wrong location" subset of regressions."""
        code = (
            "from vco_lib.bundled_versions import manifest_path; "
            "p = manifest_path(); "
            "assert p.is_file(), f'manifest missing at {p}'; "
            "assert 'site-packages' in str(p), "
            "  f'manifest not in site-packages: {p}'; "
            "print('OK', p)"
        )
        rc, stdout, stderr = self._run_in_venv(code)
        self.assertEqual(
            rc, 0,
            f"wheel-installed manifest_path() failed: exit={rc}, "
            f"stderr:\n{stderr}\nstdout:\n{stdout}",
        )

    def test_resolve_pinned_package_works_post_wheel_install(self) -> None:
        """End-to-end: simulate the FIRST thing the wrapper MCP does
        in ``_resolve_upstream_argv`` — look up the mermaid_mcp pin
        out of the loaded manifest. Pre-fix this failed with a
        SystemExit(1) on RuntimeError; post-fix it should return the
        pinned (package, version) tuple cleanly.
        """
        code = (
            "from vco_lib.bundled_versions import load_bundled_versions; "
            "v = load_bundled_versions(); "
            "spec = v['npm']['mermaid_mcp']; "
            "assert spec['package'], 'no package field'; "
            "assert spec['version'], 'no version field'; "
            "print('OK', spec['package'], spec['version'])"
        )
        rc, stdout, stderr = self._run_in_venv(code)
        self.assertEqual(
            rc, 0,
            f"mermaid_mcp pin resolution failed post-wheel-install: "
            f"exit={rc}, stderr:\n{stderr}\nstdout:\n{stdout}",
        )
        self.assertIn("OK", stdout, f"unexpected stdout: {stdout!r}")


if __name__ == "__main__":
    unittest.main()
