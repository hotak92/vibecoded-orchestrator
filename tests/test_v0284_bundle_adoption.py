# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 PLAN-v0284 D7 (P5 / ruling R2): shipped-file adoption ACT tests.

Per R2 ("we don't expect users to edit any VCO codefile"), an --update run now
ADOPTS divergent bundle files at VCO-shipped destinations by default — backing
up the CURRENT bytes to a timestamped `.claude/backups/bundle-adoptions/<ts>/`
tree, writing the shipped bytes, recording the manifest entry, and emitting a
ONE-TIME notice (stdout + auto-resolutions.jsonl + result keys) — NOT an eternal
`bundle_user_modified_preserved` deferral.

Two adoption cases (R2 treats them identically):
  * manifest-hash-mismatch (KNOWN user-modified) — adoption act #2.
  * manifest-LESS-no-history-match (pre-manifest / pre-history-rewrite stale
    shipped file) — the FAIL-WITHOUT-FIX pin (the incident's 11 frozen files).
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import bundle_ext, make_fake_orchestrator  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


class BundleAdoptionActTests(unittest.TestCase):
    """Adoption ACT tests on a NON-ROOT project (folder ≠ orchestrator_root —
    A3: non-root projects are first-class)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-adopt-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch)
        # A3 pin: this fixture is NON-ROOT (proj is not the orchestrator root).
        assert self.proj.resolve() != self.orch.resolve()
        self.ext = bundle_ext()
        # Seed first install → manifest tracks foo.<ext>.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo(self) -> Path:
        return self.proj / ".claude" / "hooks" / f"foo.{self.ext}"

    def _bump(self, body: str) -> None:
        (self.orch / "templates" / "hooks" / f"foo.{self.ext}").write_text(
            body, encoding="utf-8",
        )

    def _manifest(self) -> dict:
        return json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )

    # ---- adoption act #2: KNOWN user-modified (manifest-hash-mismatch) ----

    def test_known_user_modified_is_adopted(self):
        """R2: a KNOWN user-modified file (manifest-hash-mismatch) is ADOPTED
        with a backup + no deferral (same treatment as the manifest-less case)."""
        old = "MY LOCAL EDIT\n"
        self._foo().write_text(old, encoding="utf-8")
        new = "#!/bin/sh\necho v2\n"
        self._bump(new)

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["adopt"])
        # Shipped bytes on disk.
        self.assertEqual(self._foo().read_text(encoding="utf-8"), new)
        # Backup holds prior bytes at the documented path.
        backup = self.proj / result["adopt_backup_dir"] / ".claude" / "hooks" / f"foo.{self.ext}"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), old)
        # Manifest advanced.
        self.assertEqual(
            self._manifest()["files"][rel]["sha256"],
            hashlib.sha256(new.encode("utf-8")).hexdigest(),
        )
        # No eternal deferral.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_preserved"))
        # JSONL notice present.
        jsonl = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        rows = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(any(r["action"] == "adopted_shipped_file" for r in rows))

    # ---- FAIL-WITHOUT-FIX PIN: manifest-LESS stale shipped file ----

    def test_manifest_less_stale_file_is_adopted(self):
        """PIN (P5): a file at a shipped destination whose manifest entry is
        MISSING and whose bytes match NO template git history (the incident's 11
        frozen files) is ADOPTED — shipped bytes on disk, backup captured, JSONL
        notice, manifest now tracks it, NO `bundle_user_modified_preserved`,
        and a pre-existing stale entry clears.

        Pre-fix this fell to `preserve` forever (frozen + nagged).
        """
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        # Simulate a pre-manifest install: drop foo's manifest entry AND put
        # stale-but-different bytes on disk (matching no history).
        manifest = self._manifest()
        del manifest["files"][rel]
        (self.proj / ".claude" / ".vco-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )
        stale = "STALE OLD SHIPPED VERSION\n"
        self._foo().write_text(stale, encoding="utf-8")
        new = "#!/bin/sh\necho v_new\n"
        self._bump(new)

        # Seed a stale pre-existing preserve deferral to prove self-clear.
        from vco_lib.deferral_report import DeferralEntry
        seed = DeferralReport.read(self.proj)
        seed.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_preserved",
            title="stale", detected="stale", why_deferred="stale",
            command_to_apply="noop", severity="info",
        ))
        seed.write(self.proj)

        # `orchestrator_root` is a git-less tree here, so the v0.2.31 history
        # heal can't match → the file falls to the terminal adopt path.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn(rel, result["actions"]["adopt"])
        # Shipped bytes on disk.
        self.assertEqual(self._foo().read_text(encoding="utf-8"), new)
        # Backup holds the stale bytes at the documented path.
        backup = self.proj / result["adopt_backup_dir"] / ".claude" / "hooks" / f"foo.{self.ext}"
        self.assertTrue(backup.exists(), f"backup missing at {backup}")
        self.assertEqual(backup.read_text(encoding="utf-8"), stale)
        # Manifest now tracks the file (adopted → recorded).
        self.assertIn(rel, self._manifest()["files"])
        # NO preserve deferral; the seeded stale one self-cleared.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_preserved"))
        # JSONL notice line present, naming the file + backup.
        jsonl = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        rows = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(
            any(r["action"] == "adopted_shipped_file" and f"foo.{self.ext}" in r["detail"]
                for r in rows),
            f"expected adopted_shipped_file row; got {rows}",
        )

    # ---- backups tree is invisible to manifest + orphan machinery ----

    def test_backup_tree_not_tracked_in_manifest_and_not_orphaned(self):
        """The backup tree must never enter the manifest ownership set nor be
        picked up by the orphan scan (it is not in `_enumerate_bundle_files`)."""
        self._foo().write_text("EDIT\n", encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        backup_dir_rel = result["adopt_backup_dir"]
        manifest = self._manifest()
        # No manifest key references the backups subtree.
        for key in list(manifest["files"].keys()) + list(manifest.get("preserved_files", {}).keys()):
            self.assertNotIn("bundle-adoptions", key.replace("\\", "/"))
        # A SECOND update run must not orphan-delete the backup dir.
        backup_file = self.proj / backup_dir_rel / ".claude" / "hooks" / f"foo.{self.ext}"
        self.assertTrue(backup_file.exists())
        self._bump("#!/bin/sh\necho v3\n")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertTrue(
            backup_file.exists(),
            "the first run's backup must survive a later update (not orphaned)",
        )

    def test_adopt_is_idempotent_second_run_is_noop(self):
        """After adoption the on-disk file matches the shipped bytes, so a
        re-run with the same orchestrator classifies it `noop` (no new backup)."""
        self._foo().write_text("EDIT\n", encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        result2 = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result2["actions"]["noop"])
        self.assertNotIn("adopt_backup_dir", result2,
                         "a noop run must not create an adoption backup dir")

    def test_no_adoption_leaves_no_backup_dir_key(self):
        """A run with zero divergent files creates no backup dir and emits no
        `adopt_backup_dir` key."""
        self._bump("#!/bin/sh\necho v2\n")  # bump, but user hasn't touched → overwrite
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["overwrite"])
        self.assertEqual(result["actions"]["adopt"], [])
        self.assertNotIn("adopt_backup_dir", result)
        # No backups tree created at all.
        self.assertFalse(
            (self.proj / ".claude" / "backups" / "bundle-adoptions").exists()
        )

    # ---- A4 container-runtime coverage: compose files adopt without
    #      splitting the launcher-owned C-RT-5 override mirror ----

    def test_docker_and_podman_compose_files_adopt_together(self):
        """A3/A4: BOTH shipped compose flavours (docker + podman) at a shipped
        destination adopt when user-edited — identical treatment, no runtime
        asymmetry."""
        docker_rel = str(Path("infrastructure") / "docker-compose.yml")
        podman_rel = str(Path("infrastructure") / "podman-compose.gpu.yml")
        (self.proj / docker_rel).write_text("services: {user: docker}\n", encoding="utf-8")
        (self.proj / podman_rel).write_text("services: {user: podman}\n", encoding="utf-8")
        # Orchestrator bumps both.
        (self.orch / "infrastructure" / "docker-compose.yml").write_text(
            "services: {v2: docker}\n", encoding="utf-8")
        (self.orch / "infrastructure" / "podman-compose.gpu.yml").write_text(
            "services: {v2: podman}\n", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn(docker_rel, result["actions"]["adopt"])
        self.assertIn(podman_rel, result["actions"]["adopt"])
        # Both refreshed to shipped bytes.
        self.assertEqual(
            (self.proj / docker_rel).read_text(encoding="utf-8"), "services: {v2: docker}\n")
        self.assertEqual(
            (self.proj / podman_rel).read_text(encoding="utf-8"), "services: {v2: podman}\n")
        # Both prior copies backed up under the SAME run dir.
        backup_root = self.proj / result["adopt_backup_dir"]
        self.assertTrue((backup_root / docker_rel).exists())
        self.assertTrue((backup_root / podman_rel).exists())

    def test_crt5_override_mirror_never_in_op_set_so_never_adopted(self):
        """A4 PIN: the launcher-owned dual-name C-RT-5 compose mirror
        (`docker-compose.override.yml` / `podman-compose.override.yml`) is NOT
        in `_enumerate_bundle_files`, so adoption can never split the pair by
        adopting/backing-up one side. Prove it: neither override name appears in
        the enumerated op set, and a user-authored override survives an update
        untouched (never adopted, never backed up)."""
        ops = project_init._enumerate_bundle_files(self.orch, project_root=self.proj)
        op_rels = {op.dest_rel.replace("\\", "/") for op in ops}
        for name in ("docker-compose.override.yml", "podman-compose.override.yml"):
            self.assertNotIn(f"infrastructure/{name}", op_rels,
                             f"{name} must not be in the bundle op set (launcher-owned mirror)")
        # A user-authored override pair survives an update untouched.
        infra = self.proj / "infrastructure"
        infra.mkdir(parents=True, exist_ok=True)
        (infra / "docker-compose.override.yml").write_text("# user docker override\n", encoding="utf-8")
        (infra / "podman-compose.override.yml").write_text("# user podman override\n", encoding="utf-8")
        self._foo().write_text("EDIT\n", encoding="utf-8")  # a real adoption alongside
        self._bump("#!/bin/sh\necho v2\n")
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        # Override pair untouched.
        self.assertEqual(
            (infra / "docker-compose.override.yml").read_text(encoding="utf-8"),
            "# user docker override\n")
        self.assertEqual(
            (infra / "podman-compose.override.yml").read_text(encoding="utf-8"),
            "# user podman override\n")
        # Neither override landed in adopt[] nor in the backup tree.
        self.assertNotIn(str(Path("infrastructure") / "docker-compose.override.yml"),
                         result["actions"]["adopt"])
        for p in (self.proj / ".claude" / "backups").rglob("*"):
            self.assertNotIn("override.yml", p.name)


class RootTargetAdoptionParityTests(unittest.TestCase):
    """A3: adoption/backup/manifest behaviour must be IDENTICAL for a ROOT
    bundle target (project folder ≡ orchestrator_root). This exercises the
    is_root_target branch of the loop to prove the adoption path is not
    accidentally gated on non-root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-adopt-root-"))
        # ROOT: the project folder IS the orchestrator root.
        self.root = self.tmp / "orchestrator"
        self.root.mkdir()
        make_fake_orchestrator(self.root)
        self.ext = bundle_ext()
        project_init.install_project_bundle(
            self.root, orchestrator_root=self.root, update_mode=False,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_root_target_adopts_with_backup_identically(self):
        """A ROOT-target update adopts a divergent bundle file with a backup +
        no deferral — same as the non-root case."""
        foo = self.root / ".claude" / "hooks" / f"foo.{self.ext}"
        foo.write_text("ROOT LOCAL EDIT\n", encoding="utf-8")
        (self.root / "templates" / "hooks" / f"foo.{self.ext}").write_text(
            "#!/bin/sh\necho v2\n", encoding="utf-8")
        result = project_init.install_project_bundle(
            self.root, orchestrator_root=self.root, update_mode=True,
        )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["adopt"])
        self.assertEqual(foo.read_text(encoding="utf-8"), "#!/bin/sh\necho v2\n")
        backup = self.root / result["adopt_backup_dir"] / ".claude" / "hooks" / f"foo.{self.ext}"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "ROOT LOCAL EDIT\n")
        report = DeferralReport.read(self.root)
        self.assertFalse(report.has_condition("bundle_user_modified_preserved"))


if __name__ == "__main__":
    unittest.main()
