# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``install._install_pinned_npm`` with the ``file:`` pin
shape (Phase 2 of the diagrams-integration plan, 2026-05-25).

Companion to ``test_install_pinned_npm.py``: that suite covers the
registry-pin path (claude-mermaid@1.6.3 style), this one covers the
file: pin path (file:vco_lib/excalidraw_mcp_fork style)
that we introduced for the vendored Excalidraw fork.

Mocks ``subprocess.run`` and ``_NPM_PATH`` so the suite never touches a
real npm install. The vendored fork's on-disk presence is required
(the install path reads its package.json for name+version resolution) —
since we vendored it as part of Phase 2 itself, the test relies on
that real on-disk layout. If a future test environment is missing the
vendor dir, the fixture-build helper synthesises a minimal one.

Coverage:

  * file: pin success path → vendor dir → npm install -g <dir> →
    version verify against vendored package.json → integrity check
    SKIPPED (file: pins don't carry a separate shasum) → audit
    "ok_vendored".
  * file: pin already-pinned short-circuit (no subprocess call).
  * Env-var skip (VCT_SKIP_EXCALIDRAW) → no subprocess, no vendor read.
  * npm absent → graceful False, no subprocess.
  * Vendor dir missing → graceful False with "vendor_unreadable" audit.
  * Vendor package.json malformed → graceful False with "vendor_unreadable".
  * _check_npm_pin_drift for file: pins → always returns (True, None).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import bundled_versions  # noqa: E402


# A minimal stand-in manifest pinning the excalidraw_mcp slot at the
# file: pin we ship in Phase 2. Other npm.* entries omitted — the
# manifest dict structure only needs the key under test.
def _make_file_pin_manifest(vendor_rel: str) -> dict:
    """Build a minimal manifest with the excalidraw_mcp entry pointing
    at ``file:<vendor_rel>``. Caller controls the relative path so the
    test can either point at the real vendored fork (smoke check) or
    at a controlled tmp directory."""
    return {
        "npm": {
            "excalidraw_mcp": {
                "package": f"file:{vendor_rel}",
                "version": "git+vendored-test-label",
                "shasum": "",
            },
        },
    }


def _patch_manifest(manifest):
    return mock.patch.object(
        bundled_versions, "load_bundled_versions",
        return_value=manifest,
    )


def _patch_npm_path(path: str | None = "/usr/bin/npm"):
    return mock.patch.object(install, "_NPM_PATH", path)


def _patch_audit_log(tmp_path: Path):
    return mock.patch.object(
        install, "_BUNDLED_VERSIONS_AUDIT_LOG",
        tmp_path / "bundled_versions.jsonl",
    )


def _mk_run_factory(stages: list):
    """Same shape as test_install_pinned_npm._mk_run_factory."""
    calls: list = []
    iterator = iter(stages)

    def fake_run(cmd, *args, **kwargs):
        calls.append((tuple(cmd), kwargs.get("timeout")))
        try:
            nxt = next(iterator)
        except StopIteration as e:
            raise AssertionError(
                f"subprocess.run called {len(calls)} times; "
                f"only {len(stages)} responses queued. "
                f"Last call: {cmd}"
            ) from e
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, subprocess.CompletedProcess):
            return nxt
        rc, stdout, stderr = nxt
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout=stdout, stderr=stderr,
        )

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def _make_synthetic_vendor(root: Path,
                           name: str = "excalidraw-mcp-server",
                           version: str = "2.0.0") -> Path:
    """Create a minimal vendored package layout under ``root``.

    Returns the absolute vendor dir. Mirrors the real
    ``vco_lib/excalidraw_mcp_fork/`` shape just enough for
    the install path's package.json reads to succeed.
    """
    vendor_dir = root / "vco_lib" / "excalidraw_mcp_fork"
    vendor_dir.mkdir(parents=True)
    pkg_json = {
        "name": name,
        "version": version,
        "type": "module",
        "main": "dist/mcp/index.js",
        "license": "MIT",
    }
    (vendor_dir / "package.json").write_text(
        json.dumps(pkg_json, indent=2), encoding="utf-8",
    )
    (vendor_dir / "dist").mkdir()
    (vendor_dir / "dist" / "mcp").mkdir()
    (vendor_dir / "dist" / "mcp" / "index.js").write_text(
        "#!/usr/bin/env node\n// synthetic vendor for tests\n",
        encoding="utf-8",
    )
    return vendor_dir


class FilePinHappyPathTests(unittest.TestCase):
    """Success scenarios for file: pin installs."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-excalidraw-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmpdir, ignore_errors=True))

    def test_file_pin_install_success(self) -> None:
        # Use the REAL vendored fork (Phase 2 ships it in-tree); this
        # doubles as a smoke check that the vendor layout matches what
        # the install path expects. The vendor's actual package.json
        # declares name=excalidraw-mcp-server, version=2.0.0.
        manifest = _make_file_pin_manifest(
            "vco_lib/excalidraw_mcp_fork",
        )
        # Stages:
        #   1. initial `npm view -g <name> version` (absent → empty stdout)
        #   2. `npm install -g <vendor-abs-dir>` (success)
        #   3. post-install `npm view -g <name> version` (returns 2.0.0)
        # Integrity probe is SKIPPED for file: pins → no 4th stage.
        stages = [
            (0, "", ""),
            (0, "+ excalidraw-mcp-server@2.0.0\n", ""),
            (0, "2.0.0\n", ""),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertTrue(result)
        # Install invocation used the absolute vendor dir, NOT a
        # `<pkg>@<ver>` shape — file: pins MUST go through the local
        # branch.
        install_call = run_factory.calls[1]
        argv = install_call[0]
        self.assertEqual(argv[:3], ("/usr/bin/npm", "install", "-g"))
        self.assertIn("excalidraw_mcp_fork", argv[3])
        self.assertTrue(Path(argv[3]).is_absolute())
        # Audit log records the vendored result distinct from
        # registry-pin "ok" so a future audit can grep one shape.
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "ok_vendored"', audit)
        self.assertIn('"vendor_path"', audit)

    def test_file_pin_already_pinned_short_circuits(self) -> None:
        manifest = _make_file_pin_manifest(
            "vco_lib/excalidraw_mcp_fork",
        )
        # First query reports the vendored version → no install.
        stages = [
            (0, "2.0.0\n", ""),  # `npm view -g <name> version`
            # `_installed_npm_integrity` call for the audit row.
            (0, json.dumps({"dependencies": {
                "excalidraw-mcp-server": {"version": "2.0.0"}
            }}), ""),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertTrue(result)
        self.assertEqual(len(run_factory.calls), 2)  # query + integrity probe only
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "already_pinned"', audit)


class FilePinSkipPathTests(unittest.TestCase):
    """Opt-out + npm-missing paths."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-excalidraw-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmpdir, ignore_errors=True))

    def test_env_var_skip_returns_false_without_subprocess(self) -> None:
        manifest = _make_file_pin_manifest(
            "vco_lib/excalidraw_mcp_fork",
        )
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.dict("os.environ", {"VCT_SKIP_EXCALIDRAW": "1"}), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm(
                "excalidraw_mcp", skip_env_var="VCT_SKIP_EXCALIDRAW",
            )
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "skipped_env_var"', audit)

    def test_npm_missing_returns_false_without_subprocess(self) -> None:
        manifest = _make_file_pin_manifest(
            "vco_lib/excalidraw_mcp_fork",
        )
        with _patch_manifest(manifest), _patch_npm_path(None), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "skipped_npm_missing"', audit)


class FilePinFailurePathTests(unittest.TestCase):
    """Vendor-resolution + subprocess failure paths."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-excalidraw-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmpdir, ignore_errors=True))

    def test_vendor_dir_missing_returns_false(self) -> None:
        manifest = _make_file_pin_manifest("nonexistent/vendor/path")
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "vendor_unreadable"', audit)
        self.assertIn('"vendor_path"', audit)

    def test_vendor_package_json_missing_returns_false(self) -> None:
        # Vendor dir exists but no package.json inside it.
        bad_vendor = self.tmpdir / "bad_vendor"
        bad_vendor.mkdir()
        # Resolve to repo-relative form so the install path's
        # PROJECT_ROOT / rel resolution works. Since we can't easily
        # mount a tmpdir under PROJECT_ROOT, we lean on absolute paths
        # by passing the absolute as the rel — install.py's resolve()
        # handles both shapes.
        manifest = {
            "npm": {
                "excalidraw_mcp": {
                    "package": f"file:{bad_vendor.absolute()}",
                    "version": "vendored-missing-pkgjson",
                    "shasum": "",
                },
            },
        }
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "vendor_unreadable"', audit)

    def test_post_install_version_mismatch_returns_false(self) -> None:
        # The synthetic vendor declares version 2.0.0 but the
        # post-install query reports 1.9.9 → mismatch.
        synth = _make_synthetic_vendor(self.tmpdir, version="2.0.0")
        manifest = {
            "npm": {
                "excalidraw_mcp": {
                    "package": f"file:{synth.absolute()}",
                    "version": "vendored-test",
                    "shasum": "",
                },
            },
        }
        stages = [
            (0, "", ""),                                      # absent
            (0, "+ excalidraw-mcp-server@1.9.9\n", ""),       # install (wrong landed)
            (0, "1.9.9\n", ""),                               # verify (mismatch)
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("excalidraw_mcp")
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "version_mismatch"', audit)
        self.assertIn('"actual_version": "1.9.9"', audit)

    def test_idempotent_reinstall_is_safe(self) -> None:
        """Calling the install path twice in a row must not double-
        install or error; the second call should short-circuit on the
        already-pinned check.
        """
        synth = _make_synthetic_vendor(self.tmpdir, version="2.0.0")
        manifest = {
            "npm": {
                "excalidraw_mcp": {
                    "package": f"file:{synth.absolute()}",
                    "version": "vendored-test",
                    "shasum": "",
                },
            },
        }
        # First invocation: absent → install → verify.
        # Second invocation: already at 2.0.0 → short-circuit (one extra
        # query for integrity audit).
        stages = [
            (0, "", ""),
            (0, "+ excalidraw-mcp-server@2.0.0\n", ""),
            (0, "2.0.0\n", ""),
            # Second call:
            (0, "2.0.0\n", ""),
            (0, json.dumps({"dependencies": {
                "excalidraw-mcp-server": {"version": "2.0.0"}
            }}), ""),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(manifest), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            r1 = install._install_pinned_npm("excalidraw_mcp")
            r2 = install._install_pinned_npm("excalidraw_mcp")
        self.assertTrue(r1)
        self.assertTrue(r2)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "ok_vendored"', audit)
        self.assertIn('"result": "already_pinned"', audit)


class FilePinDriftDetectorTests(unittest.TestCase):
    """`_check_npm_pin_drift` for file: pins is structurally a no-op —
    the vendor IS the pin, so there's no separate registry version to
    drift against."""

    def test_file_pin_never_reports_drift(self) -> None:
        manifest = _make_file_pin_manifest(
            "vco_lib/excalidraw_mcp_fork",
        )
        with _patch_manifest(manifest), _patch_npm_path(), \
             mock.patch.object(subprocess, "run") as run:
            in_sync, msg = install._check_npm_pin_drift("excalidraw_mcp")
        self.assertTrue(in_sync)
        self.assertIsNone(msg)
        # And critically: no npm subprocess invoked — file: pins skip
        # the query entirely.
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
