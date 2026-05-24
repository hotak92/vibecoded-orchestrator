# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for `install._install_pinned_npm` and
`install._check_npm_pin_drift` (Phase 0, diagrams-integration plan
2026-05-24).

Mocks `subprocess.run` and the module-level `_NPM_PATH` so the test
suite never touches the system's real npm. Covers:

  * success path (install → version match → integrity match)
  * already-pinned short-circuit
  * env-var skip (opt-out)
  * npm absent → graceful False, no subprocess call
  * subprocess timeout / OSError
  * non-zero exit
  * post-install version mismatch
  * integrity mismatch
  * integrity-not-exposed (older npm) → True with WARN audit
  * drift detector for `_check_npm_pin_drift`
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
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# A minimal stand-in manifest pinned to test values. We avoid relying
# on the real `bundled_mcp_versions.toml` values so a future bump
# doesn't accidentally break these tests.
_FAKE_MANIFEST = {
    "npm": {
        "mermaid_mcp": {
            "package": "claude-mermaid",
            "version": "1.6.3",
            "shasum": "a5f1050ef7af6dc2595f5507366006489fef2879",
        },
        "missing_package": {
            "package": "@vco-test/never-published",
            "version": "9.9.9",
            "shasum": "ffffffffffffffffffffffffffffffffffffffff",
        },
    },
}


def _patch_manifest():
    """Decorator-friendly context manager: patches
    `bundled_versions.load_bundled_versions` to return the fake manifest
    above, so tests don't depend on the real .toml's pinned values."""
    return mock.patch.object(
        bundled_versions, "load_bundled_versions",
        return_value=_FAKE_MANIFEST,
    )


def _patch_npm_path(path: str | None = "/usr/bin/npm"):
    """Patch the module-level `_NPM_PATH` cache."""
    return mock.patch.object(install, "_NPM_PATH", path)


def _patch_audit_log(tmp_path: Path):
    """Redirect the audit log to a tempdir so test runs don't pollute
    the user's `~/.claude/metrics/bundled_versions.jsonl`."""
    return mock.patch.object(
        install, "_BUNDLED_VERSIONS_AUDIT_LOG",
        tmp_path / "bundled_versions.jsonl",
    )


def _mk_run_factory(stages: list):
    """Build a `subprocess.run` side_effect that returns canned
    CompletedProcess objects in order; raises if the test calls more
    times than expected.

    Each entry in `stages` may be:
      - a `subprocess.CompletedProcess` (returned directly);
      - an Exception subclass instance (raised);
      - a tuple `(returncode, stdout, stderr)` (built into a
        CompletedProcess on the fly).
    """
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


class InstallPinnedNpmHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-bundled-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir,
                                                            ignore_errors=True))

    def test_install_succeeds_and_returns_true(self) -> None:
        # Stage sequence: not installed (query) → install → version verify
        # → integrity verify (matches pin).
        ls_json = json.dumps({
            "dependencies": {
                "claude-mermaid": {
                    "version": "1.6.3",
                    "_shasum": "a5f1050ef7af6dc2595f5507366006489fef2879",
                }
            }
        })
        stages = [
            (0, "", ""),                  # initial `npm view -g` (absent) → empty stdout
            (0, "+ claude-mermaid@1.6.3\n", ""),  # `npm install -g pkg@ver`
            (0, "1.6.3\n", ""),           # post-install `npm view -g version`
            (0, ls_json, ""),             # `npm ls -g --json --depth=0 pkg`
        ]
        run_factory = _mk_run_factory(stages)

        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")

        self.assertTrue(result)
        # Audit log line written.
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "ok"', audit)
        self.assertIn("claude-mermaid", audit)

    def test_already_pinned_short_circuits(self) -> None:
        # First subprocess call returns the pinned version → no install.
        ls_json = json.dumps({
            "dependencies": {
                "claude-mermaid": {
                    "version": "1.6.3",
                    "_shasum": "a5f1050ef7af6dc2595f5507366006489fef2879",
                }
            }
        })
        stages = [
            (0, "1.6.3\n", ""),  # `npm view -g version` (already at pin)
            (0, ls_json, ""),    # `npm ls -g --json` for audit-only integrity probe
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertTrue(result)
        # Both queries consumed; no install call.
        self.assertEqual(len(run_factory.calls), 2)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "already_pinned"', audit)


class InstallPinnedNpmSkipPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-bundled-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir,
                                                            ignore_errors=True))

    def test_env_var_skip_returns_false_without_subprocess(self) -> None:
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.dict("os.environ", {"VCT_SKIP_MERMAID": "1"}), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm(
                "mermaid_mcp", skip_env_var="VCT_SKIP_MERMAID",
            )
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "skipped_env_var"', audit)

    def test_npm_missing_returns_false_without_subprocess(self) -> None:
        with _patch_manifest(), _patch_npm_path(None), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run") as run:
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertFalse(result)
        run.assert_not_called()
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "skipped_npm_missing"', audit)


class InstallPinnedNpmFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vco-test-bundled-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir,
                                                            ignore_errors=True))

    def test_timeout_during_install_returns_false(self) -> None:
        stages = [
            (0, "", ""),  # initial version probe (absent)
            subprocess.TimeoutExpired(cmd="npm install", timeout=300),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp", timeout=300)
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "install_timeout"', audit)

    def test_oserror_during_install_returns_false(self) -> None:
        stages = [
            (0, "", ""),  # initial version probe
            OSError("disk full"),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "install_oserror"', audit)

    def test_non_zero_exit_returns_false(self) -> None:
        stages = [
            (0, "", ""),                      # initial probe
            (1, "", "E404 not found\n"),      # install fails
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "install_nonzero"', audit)

    def test_post_install_version_mismatch_returns_false(self) -> None:
        # npm install reports success but the resolver landed a
        # different version (peer-dep conflict or workspace override).
        stages = [
            (0, "", ""),                          # initial probe (absent)
            (0, "+ claude-mermaid@1.6.2\n", ""),  # install (wrong version landed)
            (0, "1.6.2\n", ""),                   # post-install verify
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "version_mismatch"', audit)
        self.assertIn('"actual_version": "1.6.2"', audit)

    def test_integrity_mismatch_returns_false(self) -> None:
        bad_ls_json = json.dumps({
            "dependencies": {
                "claude-mermaid": {
                    "version": "1.6.3",
                    "_shasum": "deadbeef" + "0" * 32,
                }
            }
        })
        stages = [
            (0, "", ""),                            # initial probe
            (0, "+ claude-mermaid@1.6.3\n", ""),    # install
            (0, "1.6.3\n", ""),                     # version verify (OK)
            (0, bad_ls_json, ""),                   # integrity (mismatch)
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertFalse(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "integrity_mismatch"', audit)

    def test_integrity_not_exposed_returns_true_with_warn(self) -> None:
        """Older npm clients don't expose `_shasum`. We still return
        True because the version pin is verified — the WARN audit
        entry records the skipped strict check."""
        ls_json_no_shasum = json.dumps({
            "dependencies": {
                "claude-mermaid": {"version": "1.6.3"}
                # No _shasum / _integrity field.
            }
        })
        stages = [
            (0, "", ""),
            (0, "+ claude-mermaid@1.6.3\n", ""),
            (0, "1.6.3\n", ""),
            (0, ls_json_no_shasum, ""),
        ]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             _patch_audit_log(self.tmpdir), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            result = install._install_pinned_npm("mermaid_mcp")
        self.assertTrue(result)
        audit = (self.tmpdir / "bundled_versions.jsonl").read_text()
        self.assertIn('"result": "integrity_check_skipped"', audit)


class CheckNpmPinDriftTests(unittest.TestCase):
    def test_no_drift_when_npm_absent(self) -> None:
        with _patch_manifest(), _patch_npm_path(None):
            in_sync, msg = install._check_npm_pin_drift("mermaid_mcp")
        self.assertTrue(in_sync)
        self.assertIsNone(msg)

    def test_no_drift_when_package_not_installed(self) -> None:
        # `npm view -g <pkg> version` returns exit-1 when the package
        # isn't installed.
        stages = [(1, "", "npm ERR! not installed\n")]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            in_sync, msg = install._check_npm_pin_drift("mermaid_mcp")
        self.assertTrue(in_sync)
        self.assertIsNone(msg)

    def test_no_drift_when_installed_matches_pin(self) -> None:
        stages = [(0, "1.6.3\n", "")]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            in_sync, msg = install._check_npm_pin_drift("mermaid_mcp")
        self.assertTrue(in_sync)
        self.assertIsNone(msg)

    def test_drift_detected_when_versions_differ(self) -> None:
        stages = [(0, "1.6.2\n", "")]
        run_factory = _mk_run_factory(stages)
        with _patch_manifest(), _patch_npm_path(), \
             mock.patch.object(subprocess, "run", side_effect=run_factory):
            in_sync, msg = install._check_npm_pin_drift("mermaid_mcp")
        self.assertFalse(in_sync)
        self.assertIsNotNone(msg)
        # Drift message must name the package, both versions, and the
        # remediation command.
        self.assertIn("1.6.2", msg)
        self.assertIn("1.6.3", msg)
        self.assertIn("claude-mermaid", msg)
        self.assertIn("--force-pin-reset", msg)

    def test_drift_records_deferral_entry(self) -> None:
        """`_record_npm_pin_drift_deferral` writes a `bundle_pin_drift_<key>`
        entry into the DeferralReport with the correct shape."""
        report = DeferralReport()
        install._record_npm_pin_drift_deferral(
            "mermaid_mcp",
            "installed claude-mermaid version 1.6.2 differs from pin 1.6.3",
            report,
        )
        entries = list(report.entries)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.condition_id, "bundle_pin_drift_mermaid_mcp")
        self.assertIn("mermaid_mcp", entry.title)
        self.assertIn("--force-pin-reset", entry.command_to_apply)
        self.assertEqual(entry.severity, "warning")


class ManifestSpecResolutionTests(unittest.TestCase):
    def test_unknown_key_raises_keyerror(self) -> None:
        with _patch_manifest():
            with self.assertRaises(KeyError) as cm:
                install._resolve_pinned_package("not_a_real_key")
        self.assertIn("not_a_real_key", str(cm.exception))

    def test_missing_required_field_raises_keyerror(self) -> None:
        broken_manifest = {
            "npm": {
                "broken": {"package": "x", "version": "1.0.0"},
                # missing shasum
            },
        }
        with mock.patch.object(bundled_versions, "load_bundled_versions",
                               return_value=broken_manifest):
            with self.assertRaises(KeyError) as cm:
                install._resolve_pinned_package("broken")
        self.assertIn("shasum", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
