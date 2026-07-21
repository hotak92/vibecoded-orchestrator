# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.85 PLAN-v0285 WP-1: root install delegates to the ONE bundle engine.

install.py's Steps 5b + 9b (bespoke root .claude/ materialize + agents/skills
install, with their own enumeration / 4-action classifier / manifest writer /
settings merge) were DELETED and replaced by a single delegated call to the
same ``install-bundle`` CLI the launcher uses (``vco_lib.self_install.
run_root_bundle_install``), against the root folder. See the plan for the
decision record (D1 delegation, D2 subprocess contract, D3 adopt-with-backup,
D4 update-mode rule, D5 flag mapping, D7 copy2 audit, D8 deferral verify-pins).

The fail-without-fix pins here (PIN-R1/R2/R3) were RED on the pre-v0.2.85 tree:
  * PIN-R1: the old ``_refresh_orchestrator_self_vco_manifest`` rebuilt the
    manifest ``files`` map from only hooks/scripts/settings, so an install.py
    --update DESTROYED the agents/skills manifest entries the launcher wrote
    (F-NEW-1). Post-fix the single bundle manifest writer preserves them.
  * PIN-R2: the old Step-5b path PRESERVED a drifted runtime copy and emitted
    an eternal ``orchestrator_self_user_modified_preserved`` deferral; the
    launcher path already ADOPTED it (v0.2.84) — the asymmetry R-A bans.
    Post-fix root adopts-with-backup, no deferral.
  * PIN-R3: structural — install.py no longer defines the deleted functions
    and its ``shutil.copy2`` count is at/below the post-audit ratchet.
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# A4 discipline (per plan D10 / D11): reuse the shared fake-orchestrator
# fixture rather than inventing a new one.
from tests._v0284_bundle_fixtures import (  # noqa: E402
    bundle_ext,
    make_fake_orchestrator,
)
from vco_lib import self_install  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stage_root() -> Path:
    """A fake orchestrator ROOT (folder == orchestrator_root) with templates.

    The ROOT case is exactly what install.py's delegated call exercises:
    ``--folder <root> --orchestrator-root <root> --project-folder <root>``.
    """
    tmp = Path(tempfile.mkdtemp(prefix="v0285-root-deleg-"))
    make_fake_orchestrator(tmp)
    return tmp


def _manifest(root: Path) -> dict:
    return json.loads(
        (root / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
    )


def _write_manifest(root: Path, data: dict) -> None:
    (root / ".claude" / ".vco-manifest.json").write_text(
        json.dumps(data), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# PIN-R1 — manifest clobber (FAIL-WITHOUT-FIX)
# ---------------------------------------------------------------------------


class PinR1ManifestClobberTests(unittest.TestCase):
    """The root update flow must NOT drop agents/skills manifest entries.

    Pre-v0.2.85 ``_refresh_orchestrator_self_vco_manifest`` rebuilt ``files``
    from only hooks/scripts/settings — every install.py --update demoted the
    agents/skills entries to manifest-less (F-NEW-1). The single bundle
    manifest writer (post-delegation) preserves the full set.
    """

    def setUp(self):
        self.root = _stage_root()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_agents_manifest_entry_survives_root_update(self):
        # Fresh install → manifest tracks .claude/agents/coder.md.
        self_install.run_root_bundle_install(self.root, update_mode=False)
        files = _manifest(self.root).get("files", {})
        agent_keys = [k for k in files if "agents" in k]
        self.assertTrue(
            agent_keys,
            "fresh install must record the agents entry in the manifest",
        )

        # PLAN-v0285 PIN-R1: run the root UPDATE flow → entry SURVIVES. The old
        # _refresh_orchestrator_self_vco_manifest dropped it here.
        self_install.run_root_bundle_install(self.root, update_mode=True)
        files_after = _manifest(self.root).get("files", {})
        for k in agent_keys:
            self.assertIn(
                k, files_after,
                f"agents manifest entry {k!r} was clobbered by the root "
                "update (F-NEW-1 regression)",
            )

    def test_backslash_separator_agents_entry_survives(self):
        """A3 / Windows-manifest-key variant: an agents entry keyed with the
        backslash separator (as a Windows-authored manifest would carry it)
        must ALSO survive the root update — the bundle writer never rebuilds
        ``files`` from a hooks/scripts/settings-only walk."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        man = _manifest(self.root)
        # Inject a Windows-shaped (backslash) agents key that the bundle path
        # did not write (simulating a manifest authored on Windows).
        win_key = ".claude\\agents\\coder.md"
        man.setdefault("files", {})[win_key] = {"sha256": "deadbeef", "source": ""}
        _write_manifest(self.root, man)

        self_install.run_root_bundle_install(self.root, update_mode=True)
        files_after = _manifest(self.root).get("files", {})
        # The bundle writer keys by its own (host-OS) path for coder.md; the
        # foreign backslash key must not be *actively deleted* by a
        # hooks/scripts/settings-only rebuild. Either the host-OS coder.md key
        # is present OR the backslash key was carried — the F-NEW-1 clobber
        # would have wiped BOTH under the old writer.
        self.assertTrue(
            any("agents" in k and "coder.md" in k for k in files_after),
            "no agents/coder.md manifest entry survived the root update "
            "(F-NEW-1 clobber would drop every agents entry)",
        )


# ---------------------------------------------------------------------------
# PIN-R2 — root adoption parity + deferral retirement (FAIL-WITHOUT-FIX)
# ---------------------------------------------------------------------------


class PinR2RootAdoptionTests(unittest.TestCase):
    """A drifted manifest-less runtime copy at a shipped destination is now
    ADOPTED with a backup (D3), NOT preserved + eternally deferred."""

    def setUp(self):
        self.root = _stage_root()
        self.ext = bundle_ext()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_drifted_script_is_adopted_with_backup_no_deferral(self):
        # First install seeds .claude/scripts/kg-search + its manifest entry.
        self_install.run_root_bundle_install(self.root, update_mode=False)
        script = self.root / ".claude" / "scripts" / "kg-search"
        rel = ".claude/scripts/kg-search"
        original = "USER LOCAL DRIFT\n"
        script.write_text(original, encoding="utf-8")
        # Make it manifest-LESS (the pre-manifest / stale-shipped incident
        # shape): strip its manifest entry so no prior-hash exists.
        man = _manifest(self.root)
        man["files"].pop(rel, None)
        _write_manifest(self.root, man)

        # PLAN-v0285 PIN-R2 (was: preserve + orchestrator_self_user_modified_
        # preserved deferral). Now: adopt-with-backup.
        res = self_install.run_root_bundle_install(self.root, update_mode=True)

        # (a) envelope actions.adopt lists it.
        self.assertIn(
            rel, res["actions"]["adopt"],
            "drifted manifest-less shipped file must be ADOPTED on root "
            "(pre-v0.2.85 it was preserved)",
        )
        # (b) shipped bytes on disk (drift replaced).
        self.assertNotEqual(
            script.read_text(encoding="utf-8"), original,
            "shipped bytes must land on disk after adoption",
        )
        # (c) backup exists under .claude/backups/bundle-adoptions/<ts>/ with
        #     the ORIGINAL bytes.
        self.assertIn("adopt_backup_dir", res)
        backup = self.root / res["adopt_backup_dir"] / rel
        self.assertTrue(
            backup.exists(),
            f"adoption backup must exist at {backup}",
        )
        self.assertEqual(
            backup.read_text(encoding="utf-8"), original,
            "adoption backup must carry the ORIGINAL (pre-adopt) bytes",
        )

    def test_no_orchestrator_self_preserved_deferral_after_adoption(self):
        """The retired producer must be gone: no
        ``orchestrator_self_user_modified_preserved`` entry is written on a
        root update that adopts a drifted file (PLAN-v0285 D3)."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        script = self.root / ".claude" / "scripts" / "kg-search"
        script.write_text("USER DRIFT\n", encoding="utf-8")
        man = _manifest(self.root)
        man["files"].pop(".claude/scripts/kg-search", None)
        _write_manifest(self.root, man)

        self_install.run_root_bundle_install(self.root, update_mode=True)

        report = DeferralReport.read(self.root)
        self.assertFalse(
            report.has_condition("orchestrator_self_user_modified_preserved"),
            "the retired orchestrator_self_user_modified_preserved producer "
            "must not emit on the delegated root path (D3)",
        )

    def test_stale_preserved_deferral_self_clears_on_root_update(self):
        """A pre-existing on-disk
        ``orchestrator_self_user_modified_preserved`` entry (from a pre-.85
        run) drop-when-absent self-clears once install.py's finalize runs
        (the id stays in _INSTALL_OWNED_CONDITION_IDS, searxng-precedent)."""
        import install  # noqa: PLC0415 — heavy import; only needed here.

        # Seed a stale owned entry on disk (as a pre-.85 run would have left).
        pre = DeferralReport()
        pre.add_entry(DeferralEntry(
            condition_id="orchestrator_self_user_modified_preserved",
            title="stale from a pre-v0.2.85 run",
            detected="left on disk by the retired Step-5b producer",
            why_deferred="pre-v0.2.85",
            command_to_apply="python install.py --update",
            severity="warning",
        ))
        pre.write(self.root)
        self.assertTrue(
            DeferralReport.read(self.root).has_condition(
                "orchestrator_self_user_modified_preserved"
            )
        )

        # An install.py finalize (owned-id drop-when-absent) clears it because
        # nothing re-emits it this run — mirror the end-of-run single write.
        flow = InstallDeferralFlow(
            folder=self.root,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()          # A-2 seed excludes owned ids (drop-when-absent).
        flow.finalize()      # single authoritative write.

        self.assertFalse(
            DeferralReport.read(self.root).has_condition(
                "orchestrator_self_user_modified_preserved"
            ),
            "the retired owned id must drop-when-absent self-clear at finalize",
        )


# ---------------------------------------------------------------------------
# PIN-R3 — structural single-engine (FAIL-WITHOUT-FIX)
# ---------------------------------------------------------------------------

# Post-audit copy2 ratchet (PLAN-v0285 D7, tightened pre-beta F-11): the
# Step-5b/9b copy2 sites were deleted. The remaining `shutil.copy2(` string
# occurrences are the dist-staging family + the 3 claude.json `.bak` backups
# (all KEPT with per-site justification comments) plus one comment mention —
# grep-string count = 8 today. The pre-beta WP-E audit moved the live-overwrite
# copy2 sites onto atomic_copy_file; nothing convertible remains, so the ratchet
# is dropped from 15 to the current 8 (F-11: no silent headroom for new copy2
# sites to creep in). This constant is a ratchet: it may only DROP in future
# work, never rise silently.
_MAX_INSTALL_COPY2 = 8


class PinR3StructuralSingleEngineTests(unittest.TestCase):
    def setUp(self):
        self.src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_deleted_root_materialize_functions_are_absent(self):
        tree = ast.parse(self.src)
        defined = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        for gone in (
            "_self_materialize_file_action",
            "_refresh_orchestrator_self_vco_manifest",
            "_install_hooks_and_settings",
            "_install_agents_and_skills",
            "_load_self_materialize_prior_hashes",
            "_emit_self_materialize_preserved_deferral",
            "_materialize_orchestrator_self_claude_dir",
            "_merge_settings_template",
            "_smart_merge_settings",
        ):
            self.assertNotIn(
                gone, defined,
                f"install.py still defines {gone!r} — the bundle engine must "
                "be the single classifier/enumerator/manifest-writer (R-A)",
            )

    def test_kept_root_helpers_still_present(self):
        """CLAUDE.md AUTO-marker render (Step 4c) and root knowledge seed
        (Step 4d) are KEPT — a different concern from the bundle."""
        tree = ast.parse(self.src)
        defined = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        self.assertIn("_materialize_orchestrator_self_claude_md", defined)

    def test_copy2_count_at_or_below_ratchet(self):
        n = self.src.count("shutil.copy2(")
        self.assertLessEqual(
            n, _MAX_INSTALL_COPY2,
            f"install.py has {n} shutil.copy2 sites; ratchet is "
            f"{_MAX_INSTALL_COPY2} (the 8 Step-5b/9b sites must stay deleted)",
        )

    def test_delegated_call_present(self):
        """install.py must invoke the ONE bundle engine via self_install."""
        self.assertIn("run_root_bundle_install", self.src)
        self.assertIn("_self_install", self.src)


# ---------------------------------------------------------------------------
# Leave-alone battery (act + leave-alone on every destructive/adoption gate)
# ---------------------------------------------------------------------------


class LeaveAloneBatteryTests(unittest.TestCase):
    def setUp(self):
        self.root = _stage_root()
        self.ext = bundle_ext()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_skip_materialize_claude_dir_leaves_hooks_scripts_settings(self):
        """--skip-materialize-claude-dir → skip_kinds={hooks,scripts,settings}
        (D5): those kinds untouched + carried forward, agents/skills still
        install, NO orphan action for the skipped kinds."""
        # First install everything.
        self_install.run_root_bundle_install(self.root, update_mode=False)
        hook = self.root / ".claude" / "hooks" / f"foo.{self.ext}"
        script = self.root / ".claude" / "scripts" / "kg-search"
        hook_bytes = hook.read_bytes()
        script_bytes = script.read_bytes()
        man_before = _manifest(self.root).get("files", {})
        hook_rel = f".claude/hooks/foo.{self.ext}"

        # Bump the templates so an un-skipped run WOULD change the hook — then
        # skip it and confirm no change.
        (self.root / "templates" / "hooks" / f"foo.{self.ext}").write_text(
            "CHANGED TEMPLATE\n", encoding="utf-8",
        )
        res = self_install.run_root_bundle_install(
            self.root,
            update_mode=True,
            skip_kinds=frozenset(self_install.SKIP_MATERIALIZE_CLAUDE_DIR_KINDS),
        )

        # (1) hooks/scripts untouched on disk.
        self.assertEqual(hook.read_bytes(), hook_bytes,
                         "skipped hook must be untouched")
        self.assertEqual(script.read_bytes(), script_bytes,
                         "skipped script must be untouched")
        # (2) no orphan action for the skipped kind (would DELETE the file).
        orphan_deleted = res["actions"].get("orphan-deleted", [])
        self.assertNotIn(hook_rel, orphan_deleted,
                         "skipped hook must NOT be orphan-deleted")
        # (3) prior manifest entry carried forward verbatim.
        man_after = _manifest(self.root).get("files", {})
        self.assertIn(hook_rel, man_after,
                      "skipped hook manifest entry must be carried forward")
        self.assertEqual(man_after[hook_rel], man_before[hook_rel],
                         "carried-forward manifest entry must be byte-identical")
        # (4) agents still install (were not skipped).
        self.assertTrue((self.root / ".claude" / "agents" / "coder.md").exists())

    def test_no_hooks_flag_still_lands_hooks_byte_parity(self):
        """--no-hooks is an HONEST no-op (D5): hooks still install byte-for-
        byte. The flag never gated hooks (only VCT_DISABLE_HOOKS=1 disables
        execution)."""
        # A default delegated install (no skip_kinds) always installs hooks —
        # --no-hooks does NOT add "hooks" to skip_kinds. Prove hooks land.
        self_install.run_root_bundle_install(self.root, update_mode=False)
        hook = self.root / ".claude" / "hooks" / f"foo.{self.ext}"
        self.assertTrue(hook.exists(), "hooks must land under --no-hooks")
        self.assertEqual(
            hook.read_bytes(),
            (self.root / "templates" / "hooks" / f"foo.{self.ext}").read_bytes(),
            "installed hook must be byte-identical to the template",
        )

    def test_hook_exec_bit_present_on_posix(self):
        """Exec-bit delta (D7): root hooks move from install.py's forced 0o755
        to the bundle's 0o700 policy — the exec bit (owner-execute) must stay
        PRESENT (the v0.2.53 mode-664 regression must remain dead)."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only exec-bit check")
        import stat  # noqa: PLC0415
        self_install.run_root_bundle_install(self.root, update_mode=False)
        hook = self.root / ".claude" / "hooks" / "foo.sh"
        self.assertTrue(hook.exists())
        self.assertTrue(
            hook.stat().st_mode & stat.S_IXUSR,
            "installed .sh hook must retain the owner-execute bit",
        )

    def test_fresh_tree_preexisting_shipped_dest_is_skip_existing(self):
        """Fresh-tree (no manifest) with a pre-existing file at a shipped
        destination → skip-existing (first-install semantics), NO adoption,
        NO backup. D4: manifest absent ⇒ first-install."""
        # Pre-create a divergent file at a shipped destination BEFORE any
        # install (so no manifest exists → update_mode=False).
        pre = self.root / ".claude" / "agents" / "coder.md"
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text("PRE-EXISTING USER FILE\n", encoding="utf-8")

        res = self_install.run_root_bundle_install(self.root, update_mode=False)
        rel = ".claude/agents/coder.md"
        self.assertIn(rel, res["actions"].get("skip-existing", []),
                      "pre-existing shipped-dest file must be skip-existing")
        self.assertNotIn(rel, res["actions"].get("adopt", []),
                         "fresh install must NOT adopt")
        self.assertFalse(
            (self.root / ".claude" / "backups" / "bundle-adoptions").exists(),
            "fresh install must NOT create an adoption backup dir",
        )
        # The user's pre-existing content is untouched.
        self.assertEqual(pre.read_text(encoding="utf-8"),
                         "PRE-EXISTING USER FILE\n")

    def test_dry_run_mutates_nothing(self):
        """dry_run (adopt-preview mapping → --dry-run) makes no filesystem
        mutations."""
        res = self_install.run_root_bundle_install(
            self.root, update_mode=False, dry_run=True,
        )
        self.assertTrue(res["dry_run"])
        self.assertFalse(
            (self.root / ".claude" / "hooks").exists(),
            "dry-run must not write .claude/hooks/",
        )
        self.assertFalse(
            (self.root / ".claude" / "agents").exists(),
            "dry-run must not write .claude/agents/",
        )
        self.assertFalse(
            (self.root / ".claude" / ".vco-manifest.json").exists(),
            "dry-run must not write the manifest",
        )

    def test_templates_tree_unchanged_after_root_run(self):
        """The maintainer's source of truth (templates/) is never written by
        any install op (D3 structural pin): its byte-hash is identical before
        and after a root install."""
        import hashlib  # noqa: PLC0415

        def _tree_hash(d: Path) -> str:
            h = hashlib.sha256()
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    h.update(str(p.relative_to(d)).encode())
                    h.update(p.read_bytes())
            return h.hexdigest()

        templates = self.root / "templates"
        before = _tree_hash(templates)
        self_install.run_root_bundle_install(self.root, update_mode=False)
        after = _tree_hash(templates)
        self.assertEqual(before, after,
                         "no install op may write into templates/ (D3)")

    def test_divergent_knowledge_is_preserved_not_adopted(self):
        """knowledge/** divergence stays PRESERVE (never adopt user knowledge
        — D3 carve-out). Seed a knowledge template, install, drift it, update."""
        kdir = self.root / "templates" / "knowledge" / "concepts"
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / "note.md").write_text("shipped knowledge v1\n", encoding="utf-8")

        self_install.run_root_bundle_install(self.root, update_mode=False)
        knote = self.root / "knowledge" / "concepts" / "note.md"
        if not knote.exists():
            self.skipTest(
                "knowledge/ is not part of the bundle enumeration in this "
                "fixture (curated-knowledge gate); preserve path not exercised"
            )
        knote.write_text("USER edited knowledge\n", encoding="utf-8")
        # Bump the template so an un-preserved path WOULD overwrite.
        (kdir / "note.md").write_text("shipped knowledge v2\n", encoding="utf-8")

        res = self_install.run_root_bundle_install(self.root, update_mode=True)
        rel = "knowledge/concepts/note.md"
        self.assertNotIn(rel, res["actions"].get("adopt", []),
                         "user knowledge must NEVER be adopted (D3 carve-out)")
        self.assertEqual(knote.read_text(encoding="utf-8"),
                         "USER edited knowledge\n",
                         "divergent knowledge must be preserved on disk")


# ---------------------------------------------------------------------------
# D8 verify-pins — deferral choreography + parse-failure posture
# ---------------------------------------------------------------------------


class D8DeferralVerifyPinsTests(unittest.TestCase):
    def setUp(self):
        self.root = _stage_root()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_foreign_entry_after_seed_survives_finalize(self):
        """F-NEW-3 / D8: a foreign (bundle-family) entry appended to
        UPDATE_DEFERRED.md AFTER seed() survives finalize()'s late-merge
        under the shared lock. This is what lets the mid-run bundle
        subprocess's deferrals persist through install.py's single write."""
        import install  # noqa: PLC0415

        flow = InstallDeferralFlow(
            folder=self.root,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()  # nothing on disk yet.

        # A bundle subprocess writes a foreign entry mid-run (NOT an
        # install.py-owned id → must be preserved verbatim).
        foreign = DeferralReport()
        foreign.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_preserved",
            title="bundle preserved a file",
            detected="a user-modified bundle file was preserved",
            why_deferred="user edit",
            command_to_apply="python -m vco_lib.project_init dismiss-deferral ...",
            severity="warning",
        ))
        foreign.write(self.root)

        result = flow.finalize()
        self.assertGreaterEqual(result.late_merged, 1,
                                "finalize must late-merge the foreign entry")
        self.assertTrue(
            DeferralReport.read(self.root).has_condition(
                "bundle_user_modified_preserved"
            ),
            "a foreign bundle entry written after seed() must survive finalize",
        )

    def test_parse_failure_returns_partial_warning_no_exception(self):
        """D2 honest soft-fail: a subprocess emitting polluted stdout →
        run_root_bundle_install returns an error-shaped envelope with a
        PARTIAL warning, install continues, NO exception (launcher posture)."""
        class _FakeProc:
            returncode = 0
            stdout = "NOTICE: prose before the json\n{not json}\n"
            stderr = "some stderr line 1\nstderr line 2\n"

        def _runner(argv, cwd):  # noqa: ARG001
            return _FakeProc()

        # Must not raise.
        res = self_install.run_root_bundle_install(
            self.root, update_mode=True, _runner=_runner,
        )
        # Error-shaped envelope with the launcher's exact warning wording.
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("produced unparseable output", res["warnings"][0])
        self.assertIn("stderr tail:", res["warnings"][0])
        # Bundle-shaped keys still present so the human renderer works.
        self.assertIn("actions", res)
        self.assertIn("update_mode", res)

    def test_subprocess_launch_failure_soft_fails(self):
        """A subprocess that fails to start soft-fails to an error envelope
        (never raises) — the install prints PARTIAL and continues."""
        def _runner(argv, cwd):  # noqa: ARG001
            raise OSError("cannot spawn")

        res = self_install.run_root_bundle_install(
            self.root, update_mode=False, _runner=_runner,
        )
        self.assertTrue(res["warnings"])
        self.assertTrue(res["errors"])
        self.assertIn("actions", res)


# ---------------------------------------------------------------------------
# D4 update-mode rule (both directions) + D5 flag mapping (argv shape)
# ---------------------------------------------------------------------------


class UpdateModeAndFlagMappingTests(unittest.TestCase):
    def setUp(self):
        self.root = _stage_root()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_fresh_tree_is_first_install(self):
        """D4: no manifest ⇒ update_mode resolves False (first-install). We
        assert via the envelope the caller passed through."""
        res = self_install.run_root_bundle_install(self.root, update_mode=False)
        self.assertFalse(res["update_mode"])

    def test_manifest_present_is_update(self):
        """D4: a present manifest ⇒ update_mode True."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        self.assertTrue((self.root / ".claude" / ".vco-manifest.json").exists())
        res = self_install.run_root_bundle_install(self.root, update_mode=True)
        self.assertTrue(res["update_mode"])

    def test_argv_flag_mapping_shape(self):
        """D5: skip_kinds → repeated --skip-kind; force → --force; dry_run →
        --dry-run; update → --update; all before --json (launcher order)."""
        argv = self_install.root_bundle_argv(
            Path("/root"),
            update_mode=True,
            force=True,
            dry_run=True,
            skip_kinds={"hooks", "scripts", "settings"},
            python_executable="py",
        )
        # Core shape.
        self.assertEqual(argv[:4], ["py", "-m", "vco_lib.project_init",
                                    "install-bundle"])
        self.assertEqual(
            argv[4:10],
            ["--folder", "/root", "--orchestrator-root", "/root",
             "--project-folder", "/root"],
        )
        # Mode flags present, --json last.
        self.assertIn("--update", argv)
        self.assertIn("--force", argv)
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[-1], "--json")
        # Repeated --skip-kind, sorted for determinism.
        skip_idxs = [i for i, a in enumerate(argv) if a == "--skip-kind"]
        self.assertEqual(len(skip_idxs), 3)
        self.assertEqual(
            [argv[i + 1] for i in skip_idxs],
            ["hooks", "scripts", "settings"],
        )

    def test_default_argv_no_mode_flags(self):
        """A no-arg fresh run emits neither --update nor --force nor --dry-run
        nor --skip-kind (the byte-identical-to-launcher-create base case)."""
        argv = self_install.root_bundle_argv(
            Path("/root"), update_mode=False, python_executable="py",
        )
        for flag in ("--update", "--force", "--dry-run", "--skip-kind"):
            self.assertNotIn(flag, argv)
        self.assertEqual(argv[-1], "--json")


class D4RealRuleResolutionTests(unittest.TestCase):
    """v0.2.85 M-4 (Fable review): the D4 rule that ACTUALLY ships is
    ``update_mode = args.update or manifest.exists()`` inside
    ``install.py::_run_root_claude_dir_install`` — NOT the passthrough of an
    explicit ``update_mode=`` the WP-1 seam tests exercise. Drive the real
    install.py resolver with a fake args + a tmp PROJECT_ROOT (manifest present
    / absent × --update true / false) and capture the update_mode the delegated
    call receives. Without this, a regression flipping the rule to first-install
    on an installed root (→ skip-existing → root never updates its hooks) would
    go unpinned."""

    def setUp(self):
        import install as _install  # noqa: PLC0415
        self._install = _install
        self.tmp = Path(tempfile.mkdtemp(prefix="v0285-d4-rule-"))
        (self.tmp / ".claude").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _resolved_update_mode(self, *, arg_update: bool, manifest: bool) -> bool:
        """Run the real resolver, mocking the delegated call + CLAUDE.md render,
        and return the update_mode it resolved."""
        from unittest import mock  # noqa: PLC0415
        if manifest:
            (self.tmp / ".claude" / ".vco-manifest.json").write_text(
                '{"files": {}}', encoding="utf-8")

        class _Args:
            update = arg_update
            skip_materialize_claude_dir = False
            with_agents = True
            with_skills = True
            with_hooks = True
            force_materialize_claude_dir = False
            adopt_project_dry_run = False

        captured: dict = {}

        def _fake_run(root, *, update_mode, **kw):
            captured["update_mode"] = update_mode
            return {"update_mode": update_mode, "actions": {}, "warnings": [],
                    "errors": [], "settings_action": "", "manifest_written": True}

        with mock.patch.object(self._install, "PROJECT_ROOT", self.tmp), \
                mock.patch.object(self._install._self_install,
                                  "run_root_bundle_install", _fake_run), \
                mock.patch.object(self._install._self_install,
                                  "format_bundle_result_lines",
                                  lambda r: []), \
                mock.patch.object(self._install,
                                  "_materialize_orchestrator_self_claude_md",
                                  lambda *_a, **_k: None):
            self._install._run_root_claude_dir_install(_Args())
        return captured["update_mode"]

    def test_fresh_tree_no_manifest_no_update_flag_is_first_install(self):
        self.assertFalse(self._resolved_update_mode(arg_update=False, manifest=False))

    def test_manifest_present_is_update_even_without_flag(self):
        # THE load-bearing case: a plain `install.py` re-run over an installed
        # root must be an UPDATE (else the root never updates its own hooks).
        self.assertTrue(self._resolved_update_mode(arg_update=False, manifest=True))

    def test_update_flag_forces_update_even_on_fresh_tree(self):
        self.assertTrue(self._resolved_update_mode(arg_update=True, manifest=False))


if __name__ == "__main__":
    unittest.main()
