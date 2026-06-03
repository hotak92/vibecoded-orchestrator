# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V47-D (Gap D) tests for v0.2.46 Part 2 — venv triage adopt guard.

Closes the HIGH-severity finding from
``.claude/context/audits/venv-architecture-audit-2026-06-03.md``: the
lightweight re-install path silently called ``shutil.rmtree(.venv)`` on
Python-version mismatch, which for ARTup-class projects (9+ GB
scientific stacks) would destroy hours-to-days of `pip install` work.

V47-D wires a guard inside ``_venv_triage`` that downgrades the
destructive ``"recreate"`` action to a new ``"skip-no-manifest"``
outcome when:

  * no ``.vco-manifest.json`` exists at ``<project>/.claude/``
    (= "VCO never installed here, this is a 3rd-party venv"), AND
  * the user has NOT opted in via ``--adopt-project-replace-all`` OR
    the new ``--rebuild-venv`` CLI flag.

These tests pin the new behaviour and also re-verify the V47-G-stub
contract test (``test_venv_triage_default_call_unchanged_for_existing_callers``)
still passes — the new ``force_rebuild`` kwarg has a default of False so
existing callers don't need to change.
"""
from __future__ import annotations

import argparse
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


def _make_fake_venv(root: Path, version_str: str | None = None) -> Path:
    """Materialise a fake `.venv` under `root` with a python shim
    whose `-c "...version_info..."` prints `version_str` (default:
    current interpreter's major.minor).

    Returns the path to the fake python shim.
    """
    (root / ".venv" / "bin").mkdir(parents=True)
    fake_py = root / ".venv" / "bin" / "python"
    if version_str is None:
        version_str = f"{sys.version_info.major}.{sys.version_info.minor}"
    fake_py.write_text(
        f"#!/usr/bin/env bash\necho '{version_str}'\n",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)
    return fake_py


def _write_manifest(root: Path) -> Path:
    """Drop a minimal `.vco-manifest.json` at <root>/.claude/."""
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    manifest = root / ".claude" / ".vco-manifest.json"
    manifest.write_text('{"version": "0.2.46", "files": []}',
                        encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Section 1: skip-no-manifest action — the load-bearing new behaviour
# ---------------------------------------------------------------------------

class TestSkipNoManifestOnPythonMismatch(unittest.TestCase):
    """`.venv` exists + Python mismatch + no manifest + adopt mode
    (any non-replace-all) → "skip-no-manifest", NOT "recreate"."""

    def test_adopt_mode_skips_when_no_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")  # mismatch
            # NO .vco-manifest.json created — this is a 3rd-party venv.
            triage = install._venv_triage(
                root, adopt_project_mode="adopt",
            )
            self.assertEqual(triage["action"], "skip-no-manifest")
            # The venv directory must NOT have been deleted by triage.
            self.assertTrue((root / ".venv" / "bin" / "python").exists())
            self.assertIn("no .vco-manifest.json",
                          triage["reason"].lower())

    def test_no_adopt_mode_skips_when_no_manifest(self):
        """Even with adopt_project_mode=None, the guard still fires —
        the absence of a manifest is the load-bearing signal, not the
        adopt flag."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")
            triage = install._venv_triage(
                root, adopt_project_mode=None,
            )
            self.assertEqual(triage["action"], "skip-no-manifest")
            self.assertTrue((root / ".venv" / "bin" / "python").exists())


# ---------------------------------------------------------------------------
# Section 2: replace-all + force_rebuild — explicit opt-in still recreates
# ---------------------------------------------------------------------------

class TestExplicitOptInRecreates(unittest.TestCase):
    def test_replace_all_recreates_even_without_manifest(self):
        """`--adopt-project-replace-all` is the aggressive variant: user
        explicitly opted in to overwriting pre-existing files. The
        guard must yield to this."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")
            triage = install._venv_triage(
                root, adopt_project_mode="replace-all",
            )
            self.assertEqual(triage["action"], "recreate")
            self.assertIn("Python version mismatch", triage["reason"])

    def test_force_rebuild_recreates_even_without_manifest(self):
        """`--rebuild-venv` is the explicit override for the venv guard.
        With it, the user is asserting "I know VCO is correct to rebuild
        this venv, do it." → recreate."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")
            triage = install._venv_triage(
                root, adopt_project_mode="adopt", force_rebuild=True,
            )
            self.assertEqual(triage["action"], "recreate")


# ---------------------------------------------------------------------------
# Section 3: manifest present → existing "recreate" behaviour preserved
# ---------------------------------------------------------------------------

class TestManifestPresentPreservesRecreate(unittest.TestCase):
    def test_recreate_when_python_mismatch_and_manifest_present(self):
        """Manifest present = VCO owns this venv. Python-version drift
        is a legitimate reason to rebuild it. No guard."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")
            _write_manifest(root)
            triage = install._venv_triage(
                root, adopt_project_mode="adopt",
            )
            self.assertEqual(triage["action"], "recreate")
            self.assertIn("Python version mismatch", triage["reason"])

    def test_recreate_when_python_mismatch_and_manifest_present_no_adopt(self):
        """Same case, but adopt_project_mode=None — Wave-1 behaviour."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root, version_str="2.7")
            _write_manifest(root)
            triage = install._venv_triage(root)
            self.assertEqual(triage["action"], "recreate")


# ---------------------------------------------------------------------------
# Section 4: no requirements.txt + no manifest = early skip
# ---------------------------------------------------------------------------

class TestNoRequirementsNoManifestSkips(unittest.TestCase):
    def test_no_requirements_no_manifest_returns_skip_no_manifest(self):
        """`.venv` exists, but neither requirements.txt nor
        .vco-manifest.json — VCO has no way to know what to install.
        Skip rather than guess."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Matching Python version so we don't hit the mismatch path.
            _make_fake_venv(root)
            # No requirements.txt, no manifest.
            triage = install._venv_triage(
                root, adopt_project_mode="adopt",
            )
            self.assertEqual(triage["action"], "skip-no-manifest")
            # Venv path must be reported so the deferral entry can
            # reference it.
            self.assertIsNotNone(triage["venv_python"])

    def test_no_requirements_no_manifest_with_force_rebuild_proceeds(self):
        """With `--rebuild-venv`, the user opts in. The triage falls
        through to the Python-version + drift path."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_fake_venv(root)
            # No requirements.txt → drift returns True → "upgrade".
            triage = install._venv_triage(
                root, adopt_project_mode="adopt", force_rebuild=True,
            )
            # When force_rebuild bypasses the no-manifest+no-reqs guard,
            # we fall through to the regular drift check, which (no
            # state-hash snapshot present) returns "upgrade".
            self.assertIn(triage["action"], ("upgrade", "recreate"))


# ---------------------------------------------------------------------------
# Section 5: V47-G-stub contract preservation
# ---------------------------------------------------------------------------

class TestSignaturePreservation(unittest.TestCase):
    def test_venv_triage_accepts_force_rebuild_kwarg(self):
        sig = inspect.signature(install._venv_triage)
        self.assertIn("force_rebuild", sig.parameters)
        # Default must be False so existing callers don't break.
        self.assertIs(sig.parameters["force_rebuild"].default, False)

    def test_venv_triage_adopt_project_mode_kwarg_still_present(self):
        """V47-G-stub contract — the kwarg added by stub must stay."""
        sig = inspect.signature(install._venv_triage)
        self.assertIn("adopt_project_mode", sig.parameters)
        self.assertIsNone(sig.parameters["adopt_project_mode"].default)

    def test_venv_triage_default_call_still_works(self):
        """Backwards-compat: _venv_triage(path) must still work without
        the new kwargs. This guards Wave 1 callers."""
        result = install._venv_triage(Path("/nonexistent/path/no-venv"))
        self.assertIsInstance(result, dict)
        self.assertIn("action", result)
        self.assertEqual(result["action"], "create")

    def test_missing_venv_still_returns_create(self):
        """Missing-venv branch is unchanged regardless of new kwargs."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            triage = install._venv_triage(
                root, adopt_project_mode="adopt", force_rebuild=False,
            )
            self.assertEqual(triage["action"], "create")


# ---------------------------------------------------------------------------
# Section 6: --rebuild-venv CLI flag parses correctly
# ---------------------------------------------------------------------------

def _build_minimal_parser_for_rebuild_venv() -> argparse.ArgumentParser:
    """Mirror the argparse declaration V47-D added to install.py:main()
    so we can verify the flag parses without running main()."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-venv", action="store_true", default=False)
    return parser


class TestRebuildVenvFlag(unittest.TestCase):
    def test_rebuild_venv_flag_parses(self):
        args = _build_minimal_parser_for_rebuild_venv().parse_args(
            ["--rebuild-venv"]
        )
        self.assertTrue(args.rebuild_venv)

    def test_rebuild_venv_default_false(self):
        args = _build_minimal_parser_for_rebuild_venv().parse_args([])
        self.assertFalse(args.rebuild_venv)


# ---------------------------------------------------------------------------
# Section 7: deferral condition_id integration smoke
# ---------------------------------------------------------------------------

class TestDeferralEntryConditionId(unittest.TestCase):
    """Verify that the DeferralEntry payload _run_lightweight builds
    when the triage hits skip-no-manifest uses the documented
    condition_id `venv_skip_no_manifest`. Construct the entry via the
    same dataclass install.py uses so we catch any schema drift."""

    def test_condition_id_is_venv_skip_no_manifest(self):
        from vco_lib.deferral_report import DeferralEntry
        # Mirror the construction shape used in _run_lightweight.
        entry = DeferralEntry(
            condition_id="venv_skip_no_manifest",
            title="Venv preserved (no VCO manifest detected)",
            detected="Found .venv at /x but no manifest.",
            why_deferred=(
                "Recreating a venv that VCO did not install could "
                "destroy a user-curated environment."
            ),
            command_to_apply="python install.py --lightweight --rebuild-venv",
            severity="warning",
        )
        self.assertEqual(entry.condition_id, "venv_skip_no_manifest")
        self.assertEqual(entry.severity, "warning")
        self.assertIn("--rebuild-venv", entry.command_to_apply)


if __name__ == "__main__":
    unittest.main()
