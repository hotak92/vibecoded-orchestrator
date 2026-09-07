# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-D — the version SSOT (``vco_lib.vco_version``) and its wiring.

The defect this suite pins shut: pre-v0.2.92, ``.vco-manifest.json``'s
``vco_version`` held a git short SHA on every clone install while every
semver-shaped consumer compared it against release constants — a type
mismatch that silently disarmed the chunker-boundary gate for every real
user (WP-A skip-safety map row 2). A SHA is a commit, not a version; the
SSOT returns them as separate fields and the manifest-reader maps legacy
SHA-only manifests to ``(version=None, commit=<sha>)`` so the substitution
can never happen again at read time.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import vco_version  # noqa: E402


def _write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f"[project]\nname = \"vibecoded-orchestrator\"\nversion = \"{version}\"\n",
        encoding="utf-8",
    )


class TestResolve(unittest.TestCase):
    def test_real_checkout_resolves_semver_and_commit(self) -> None:
        """The SSOT on THIS checkout: pyproject semver + a real short SHA.

        This is the load-bearing assertion — the old resolver returned a
        SHA where a version belongs; the SSOT must return the pyproject
        semver as the version and the SHA only as the commit.
        """
        resolved = vco_version.resolve(REPO_ROOT)
        self.assertIsNotNone(resolved.semver)
        self.assertRegex(resolved.semver, r"^\d+\.\d+\.\d+$")
        self.assertIsNotNone(resolved.commit)
        self.assertRegex(resolved.commit or "", r"^[0-9a-f]{7,40}$")
        self.assertEqual(resolved.source, "pyproject+git")
        # The semver equals pyproject's [project] version — the file
        # bump-version.sh names as the head of the release pin set.
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
            declared = tomllib.load(fh)["project"]["version"]
        self.assertEqual(resolved.semver, declared.lstrip("v"))

    def test_pyproject_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_pyproject(root, "9.9.99")
            resolved = vco_version.resolve(root)
            self.assertEqual(resolved.semver, "9.9.99")
            self.assertIsNone(resolved.commit)
            self.assertEqual(resolved.source, "pyproject")

    def test_v_prefix_is_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_pyproject(root, "v1.2.3")
            self.assertEqual(vco_version.resolve(root).semver, "1.2.3")

    def test_no_pyproject_no_git_is_unknown_not_a_guess(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resolved = vco_version.resolve(Path(td))
            self.assertIsNone(resolved.semver)
            self.assertIsNone(resolved.commit)
            self.assertEqual(resolved.source, "unknown")

    def test_non_semver_version_is_rejected_not_coerced(self) -> None:
        """A pyproject version that is not X.Y.Z is UNKNOWN, not a string a
        downstream semver parser will choke on silently."""
        for bad in ("0.2", "1.2.3rc1", "next", ""):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _write_pyproject(root, bad)
                self.assertIsNone(
                    vco_version.resolve(root).semver,
                    f"{bad!r} must not resolve as a version",
                )

    def test_never_raises_on_garbage_root(self) -> None:
        resolved = vco_version.resolve(Path("/nonexistent-v0292-wpd"))
        self.assertIsNone(resolved.semver)
        self.assertEqual(resolved.source, "unknown")


class TestRecordedManifestVersion(unittest.TestCase):
    """The ONE manifest-version reader, tolerant of every era."""

    def test_current_shape_semver_and_commit(self) -> None:
        version, commit = vco_version.recorded_manifest_version(
            {"vco_version": "0.2.92", "vco_commit": "c81f4fde"}
        )
        self.assertEqual(version, "0.2.92")
        self.assertEqual(commit, "c81f4fde")

    def test_legacy_sha_only_maps_to_commit_not_version(self) -> None:
        """The pre-v0.2.92 shape: ``vco_version`` holds a SHA. It must come
        back as the COMMIT with version=None — comparing it as a version is
        the original defect."""
        version, commit = vco_version.recorded_manifest_version(
            {"vco_version": "5155a553"}
        )
        self.assertIsNone(version)
        self.assertEqual(commit, "5155a553")

    def test_semver_without_commit_still_a_version(self) -> None:
        version, commit = vco_version.recorded_manifest_version(
            {"vco_version": "0.2.88"}
        )
        self.assertEqual(version, "0.2.88")
        self.assertIsNone(commit)

    def test_full_sha_is_also_a_commit(self) -> None:
        version, commit = vco_version.recorded_manifest_version(
            {"vco_version": "c81f4fde" * 4}
        )
        self.assertIsNone(version)
        self.assertEqual(commit, "c81f4fde" * 4)

    def test_v_prefixed_semver_normalised(self) -> None:
        version, _ = vco_version.recorded_manifest_version(
            {"vco_version": "v0.2.90", "vco_commit": "abc1234"}
        )
        self.assertEqual(version, "0.2.90")

    def test_garbage_is_dropped_not_passed_through(self) -> None:
        for manifest in (
            {},
            {"vco_version": ""},
            {"vco_version": "some-branch-name"},
            {"vco_version": None},
            {"vco_version": 42},
            "not-a-dict",
        ):
            self.assertEqual(
                vco_version.recorded_manifest_version(manifest),  # type: ignore[arg-type]
                (None, None),
                f"{manifest!r} must read as unknown",
            )


class TestProjectInitEnvelopeCarriesBoth(unittest.TestCase):
    """The bundle-engine envelope + manifest carry semver AND commit."""

    def setUp(self) -> None:
        import shutil

        from tests.test_install_bundle import _make_fake_orchestrator

        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wpd-ver-"))
        self.orch = self.tmp / "orch"
        self.proj = self.tmp / "proj"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        # Deterministic, fixture-local semver — NOT the real repo's, so the
        # assertion proves the value came from the fixture's pyproject.
        _write_pyproject(self.orch, "9.9.99")
        self._shutil = shutil

    def tearDown(self) -> None:
        self._shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_envelope_and_manifest_carry_semver_and_commit(self) -> None:
        from vco_lib import project_init

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["vco_version"], "9.9.99")
        self.assertIn("vco_commit", result)  # additive key, always present
        manifest = __import__("json").loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["vco_version"], "9.9.99")
        self.assertIn("vco_commit", manifest)

    def test_manifest_commit_records_git_sha_when_root_is_a_repo(self) -> None:
        import subprocess

        from vco_lib import project_init

        subprocess.run(
            ["git", "init", "-q", str(self.orch)],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.orch), "add", "."],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.orch), "-c", "user.email=t@t", "-c",
             "user.name=t", "commit", "-qm", "fixture"],
            check=False, capture_output=True,
        )
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["vco_version"], "9.9.99")
        self.assertIsNotNone(result["vco_commit"])
        self.assertRegex(result["vco_commit"] or "", r"^[0-9a-f]{7,40}$")

    def test_bootstrap_resolver_uses_the_ssot(self) -> None:
        """install.py's bootstrap resolver delegates to the SSOT — the
        envelope's vco_version is a semver, not 'unknown' + a SHA."""
        import install  # type: ignore  # noqa: E402  (repo-root module)

        version, sha = install._bootstrap_resolve_vco_version(self.orch)
        self.assertEqual(version, "9.9.99")
        self.assertIsNone(sha)  # fixture has no git

        version2, sha2 = install._bootstrap_resolve_vco_version(
            REPO_ROOT
        )
        self.assertRegex(version2, r"^\d+\.\d+\.\d+$")
        self.assertIsNotNone(sha2)

    def test_no_version_spawns_left_in_bootstrap(self) -> None:
        """The inline git spawn is gone from install.py's resolver — the
        R28 re-audit's recipe 3 — pinning the migration against regression.
        Checked on the function BODY (docstrings may name the retired spawn
        for the historical record)."""
        import ast
        import inspect

        import install  # type: ignore

        source = inspect.getsource(install._bootstrap_resolve_vco_version)
        tree = ast.parse(source)
        fn = tree.body[0]
        assert isinstance(fn, ast.FunctionDef)
        stmts = fn.body
        if (
            stmts
            and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)
        ):
            stmts = stmts[1:]  # drop the docstring
        body_src = "\n".join(ast.unparse(stmt) for stmt in stmts)
        self.assertNotIn("subprocess", body_src)
        self.assertNotIn("rev-parse", body_src)
        self.assertIn("vco_version", body_src)


if __name__ == "__main__":
    unittest.main()
