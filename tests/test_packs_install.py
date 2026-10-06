# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 catalogue plan §3/§9.2 — opt-in PACK plumbing end to end.

Hermetic: a fixture orchestrator (the shared ``make_fake_orchestrator`` plus a
``templates/packs/`` tree + a ``templates/specializations/`` doc) and a fixture
project. No Weaviate, no network, no real-repo template churn. Covers:

* ``--pack`` first install: members land, ``manifest.packs`` records, result
  reports ``packs_installed``;
* a plain ``--update`` (no ``--pack``) keeps a recorded pack's members current
  (the launcher's existing Update-bundle button, zero launcher changes — §3.4);
* ``--remove-pack``: an unmodified member is deleted outright; a user-modified
  member is BACKED UP (bytes == pre-edit) then deleted; a backup-write failure
  leaves the member in place + warns (never a silent delete — §3.5);
* a disabled-side member (``{agents,skills}.disabled/``) is removed under the
  same rule (§3.5);
* ``--skip-kind skills`` skips a pack's skill ops but not its agent ops (§9.2);
* an unknown pack name is refused by the LIVE parser (not an argv-shape guess —
  the memory rule);
* a pre-v0.2.101 manifest (no ``packs`` key) round-trips with ``packs == {}``;
* a pack member that USED to be a default file orphan-processes on retirement
  and re-delivers through the ordinary path on ``--pack`` (§2.4/§9.3);
* a broken/missing table is LOUD (engine ``errors[]``) and NEVER orphan-deletes
  a recorded pack's members (data safety);
* ``python -m vco_lib.packs status --json`` matches the ONE committed cross-lane
  contract fixture (success AND refusal) — ``tests/fixtures/
  packs_status_contract.json``, the same file the Rust parser tests against;
* the manifest-writer fix: an engine update preserves the additive
  ``dismissals`` memory (found while threading ``packs`` through the same
  fresh-payload write).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402
from vco_lib import packs as packs_mod  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "packs_status_contract.json"

# A packs.toml for the fixture orchestrator: one pack with an agent + a skill
# (with a companion file, to prove companions ship), one skills-only pack.
_FIXTURE_TABLE = """\
[pack.alpha-pack]
description = "Alpha advisors"
members = [
  "alpha-pack/agents/alpha-agent.md",
  "alpha-pack/skills/alpha-skill/SKILL.md",
]

[pack.beta-pack]
description = "Beta skills"
members = [
  "beta-pack/skills/beta-skill/SKILL.md",
]
"""

_ALPHA_AGENT = "---\nname: alpha-agent\ndescription: alpha\n---\n# Alpha\nRoot {{ORCHESTRATOR_ROOT}}\n"
_ALPHA_SKILL = "---\nname: alpha-skill\ndescription: alpha skill\n---\n# Alpha skill\n"
_ALPHA_COMPANION = "alpha companion reference\n"
_BETA_SKILL = "---\nname: beta-skill\ndescription: beta skill\n---\n# Beta skill\n"


def _add_packs_and_specs(root: Path) -> None:
    """Extend a fake orchestrator with a packs tree + a specializations doc."""
    packs = root / "templates" / "packs"
    (packs / "alpha-pack" / "agents").mkdir(parents=True)
    (packs / "alpha-pack" / "agents" / "alpha-agent.md").write_text(
        _ALPHA_AGENT, encoding="utf-8")
    (packs / "alpha-pack" / "skills" / "alpha-skill" / "references").mkdir(parents=True)
    (packs / "alpha-pack" / "skills" / "alpha-skill" / "SKILL.md").write_text(
        _ALPHA_SKILL, encoding="utf-8")
    (packs / "alpha-pack" / "skills" / "alpha-skill" / "references" / "guide.md"
     ).write_text(_ALPHA_COMPANION, encoding="utf-8")
    (packs / "beta-pack" / "skills" / "beta-skill").mkdir(parents=True)
    (packs / "beta-pack" / "skills" / "beta-skill" / "SKILL.md").write_text(
        _BETA_SKILL, encoding="utf-8")
    (packs / "packs.toml").write_text(_FIXTURE_TABLE, encoding="utf-8")
    specs = root / "templates" / "specializations"
    (specs / "fields").mkdir(parents=True)
    (specs / "fields" / "backend.md").write_text(
        "# Backend\nRead before backend work.\n", encoding="utf-8")


class _PackCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-l1-packs-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch, with_compose_pair=False)
        _add_packs_and_specs(self.orch)
        assert self.proj.resolve() != self.orch.resolve()

    def tearDown(self) -> None:
        for p in self.tmp.rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _install(self, **kw) -> dict:
        return project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, **kw)

    def _manifest(self) -> dict:
        return json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8"))

    def _run_status_cli(self, *, expect_rc: int = 0) -> dict:
        proc = self._spawn([sys.executable, "-m", "vco_lib.packs", "status",
                            "--folder", str(self.proj),
                            "--orchestrator-root", str(self.orch), "--json"])
        self.assertEqual(proc.returncode, expect_rc,
                         f"stdout:{proc.stdout}\nstderr:{proc.stderr[-500:]}")
        return json.loads(proc.stdout)

    def _spawn(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = child_env()
        env["WEAVIATE_URL"] = "http://127.0.0.1:9"
        env["OLLAMA_URL"] = "http://127.0.0.1:9"
        env["VCT_DISABLE_HUB_RESOLVER"] = "1"
        return subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=300, cwd=str(self.orch))

    @staticmethod
    def _assert_matches_contract(reply: dict, *, kind: str) -> None:
        contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
        if kind == "refusal":
            exemplar = contract["refusal_reply"]
            assert reply["ok"] is False
            for key in ("error", "message"):
                assert key in reply and isinstance(reply[key], type(exemplar[key]))
            return
        exemplar = contract["status_reply"]
        assert reply["ok"] is True
        assert isinstance(reply["packs"], list)
        for row in reply["packs"]:
            assert isinstance(row["name"], str)
            assert isinstance(row["description"], str)
            assert isinstance(row["members"], list)
            assert all(isinstance(m, str) for m in row["members"])
            assert isinstance(row["installed"], bool)
        # The exemplar's own rows satisfy the same shape (contract self-check).
        for row in exemplar["packs"]:
            assert isinstance(row["name"], str)


class PackInstallTests(_PackCase):
    def test_first_install_with_pack_lands_members_and_records(self):
        result = self._install(update_mode=False, packs={"alpha-pack"})
        self.assertEqual(result["errors"], [], result)
        # Agent + skill + companion all land at their ordinary destinations.
        self.assertTrue((self.proj / ".claude/agents/alpha-agent.md").is_file())
        self.assertTrue((self.proj / ".claude/skills/alpha-skill/SKILL.md").is_file())
        self.assertTrue((self.proj / ".claude/skills/alpha-skill/references/guide.md"
                         ).is_file())
        # The other pack is NOT installed.
        self.assertFalse((self.proj / ".claude/skills/beta-skill").exists())
        # Result reports the install; manifest records it.
        self.assertEqual(result["packs_installed"], ["alpha-pack"])
        man = self._manifest()
        self.assertIn("alpha-pack", man["packs"])
        self.assertTrue(man["packs"]["alpha-pack"]["installed_at"].endswith("Z"))
        # Member entries carry their pack source (the --remove-pack key).
        src = man["files"][".claude/agents/alpha-agent.md"]["source"]
        self.assertEqual(src.replace("\\", "/"),
                         "templates/packs/alpha-pack/agents/alpha-agent.md")
        # The agent body got the ordinary substitution (placeholder resolved to
        # the orchestrator root's absolute path — basename "orchestrator").
        body = (self.proj / ".claude/agents/alpha-agent.md").read_text(encoding="utf-8")
        self.assertNotIn("{{ORCHESTRATOR_ROOT}}", body)
        self.assertIn("orchestrator", body)

    def test_specializations_kind_lands_as_plain_copy(self):
        result = self._install(update_mode=False)
        self.assertEqual(result["errors"], [], result)
        doc = self.proj / ".claude/specializations/fields/backend.md"
        self.assertTrue(doc.is_file())
        # PLAIN copy — no substitution (the doc has no placeholders, and the
        # kind is classified so --skip-kind / orphan / leftover all inherit).
        self.assertEqual(doc.read_text(encoding="utf-8"),
                         "# Backend\nRead before backend work.\n")
        self.assertEqual(project_init._bundle_op_kind(
            ".claude/specializations/fields/backend.md"), "specializations")

    def test_plain_update_keeps_recorded_pack_current(self):
        # §3.4: install the pack, then change a member's template bytes and run
        # a PLAIN --update (no --pack) — the recorded pack's member refreshes.
        self._install(update_mode=False, packs={"alpha-pack"})
        (self.orch / "templates/packs/alpha-pack/skills/alpha-skill/SKILL.md"
         ).write_text(_ALPHA_SKILL + "v2 content\n", encoding="utf-8")

        result = self._install(update_mode=True)  # no packs arg
        self.assertEqual(result["errors"], [], result)
        installed = self.proj / ".claude/skills/alpha-skill/SKILL.md"
        self.assertIn("v2 content", installed.read_text(encoding="utf-8"))
        # Still recorded after the update.
        self.assertIn("alpha-pack", self._manifest()["packs"])

    def test_manifest_v2_without_packs_roundtrips_empty(self):
        # A pre-v0.2.101 manifest has no `packs` key; the reader defaults it.
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / ".vco-manifest.json").write_text(
            json.dumps({"schema_version": 2, "files": {}, "preserved_files": {}}),
            encoding="utf-8")
        man = project_init._read_manifest(self.proj)
        self.assertEqual(man["packs"], {})
        # And a missing manifest reads back with packs == {} too.
        empty = project_init._read_manifest(self.tmp / "nonexistent")
        self.assertEqual(empty["packs"], {})


class PackRemoveTests(_PackCase):
    def _installed_alpha(self) -> Path:
        return self.proj / ".claude/skills/alpha-skill/SKILL.md"

    def test_remove_unmodified_member_is_deleted_outright(self):
        self._install(update_mode=False, packs={"alpha-pack"})
        result = self._install(update_mode=True, remove_packs={"alpha-pack"})
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["packs_removed"], ["alpha-pack"])
        self.assertFalse(self._installed_alpha().exists())
        self.assertFalse((self.proj / ".claude/agents/alpha-agent.md").exists())
        # Unmodified → NO backup (nothing of the user's to preserve).
        backups = list((self.proj / ".claude/backups/bundle-adoptions").rglob("*")) \
            if (self.proj / ".claude/backups/bundle-adoptions").exists() else []
        self.assertFalse([b for b in backups if b.is_file()], backups)
        # Pack record + member entries leave the manifest.
        man = self._manifest()
        self.assertNotIn("alpha-pack", man["packs"])
        self.assertNotIn(".claude/skills/alpha-skill/SKILL.md", man["files"])
        # One informational ledger row.
        self.assertIn("pack_removed",
                      {e.condition_id for e in DeferralReport.read(self.proj).entries})

    def test_remove_modified_member_is_backed_up_then_deleted(self):
        self._install(update_mode=False, packs={"alpha-pack"})
        edited = self._installed_alpha()
        pre_edit = edited.read_text(encoding="utf-8") + "\n# MY NOTES\n"
        edited.write_text(pre_edit, encoding="utf-8")

        result = self._install(update_mode=True, remove_packs={"alpha-pack"})
        self.assertEqual(result["packs_removed"], ["alpha-pack"])
        self.assertFalse(edited.exists(), "modified member is deleted after backup")
        # The backup exists AND carries the pre-delete (edited) bytes.
        backups = [b for b in
                   (self.proj / ".claude/backups/bundle-adoptions").rglob("SKILL.md")
                   if "alpha-skill" in str(b)]
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), pre_edit)
        self.assertTrue(any("backed up" in n for n in result.get("notes", [])))

    def test_backup_write_failure_leaves_member_in_place_and_warns(self):
        # §9.2 red-proof: the no-backup deletion path must not exist. Mutate the
        # ONE backup writer to raise → the member is LEFT IN PLACE (never
        # removed without its promised copy) + a warning is recorded.
        self._install(update_mode=False, packs={"alpha-pack"})
        edited = self._installed_alpha()
        edited.write_text(edited.read_text(encoding="utf-8") + "\n# MINE\n",
                          encoding="utf-8")
        with mock.patch.object(project_init, "_backup_bytes_for_adoption",
                               side_effect=OSError("backup dir read-only")):
            result = self._install(update_mode=True, remove_packs={"alpha-pack"})
        self.assertTrue(edited.exists(), "backup failed → member must survive")
        self.assertTrue(any("could not remove" in w for w in result["warnings"]),
                        result["warnings"])
        # The pack record is kept so a later --remove-pack retries.
        self.assertIn("alpha-pack", self._manifest()["packs"])
        self.assertNotIn("packs_removed", result)

    def test_remove_disabled_side_member_is_backed_up_and_removed(self):
        # §3.5: a member the launcher's toggle moved to skills.disabled/ is
        # removed under the same backup rule (disable ≠ ownership).
        self._install(update_mode=False, packs={"alpha-pack"})
        enabled_dir = self.proj / ".claude/skills/alpha-skill"
        disabled_dir = self.proj / ".claude/skills.disabled/alpha-skill"
        disabled_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(enabled_dir), str(disabled_dir))
        edited = disabled_dir / "SKILL.md"
        pre = edited.read_text(encoding="utf-8") + "\n# DISABLED EDIT\n"
        edited.write_text(pre, encoding="utf-8")

        result = self._install(update_mode=True, remove_packs={"alpha-pack"})
        self.assertFalse(disabled_dir.exists(),
                         "disabled-side member removed")
        backups = [b for b in
                   (self.proj / ".claude/backups/bundle-adoptions").rglob("SKILL.md")
                   if "alpha-skill" in str(b)]
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), pre)
        self.assertEqual(result["packs_removed"], ["alpha-pack"])


class PackSkipKindTests(_PackCase):
    def test_skip_kind_skills_skips_pack_skill_but_not_pack_agent(self):
        # A pack's members classify as ordinary agents/skills, so --skip-kind
        # covers them: skipping `skills` drops the pack skill, keeps the agent.
        result = self._install(update_mode=False, packs={"alpha-pack"},
                               skip_kinds=frozenset({"skills"}))
        self.assertEqual(result["errors"], [], result)
        self.assertFalse((self.proj / ".claude/skills/alpha-skill").exists())
        self.assertTrue((self.proj / ".claude/agents/alpha-agent.md").is_file())
        self.assertEqual(result["skip_kinds"], ["skills"])


class PackRetirementTransitionTests(_PackCase):
    def test_former_default_member_orphans_then_reinstalls_via_pack(self):
        # §2.4/§9.3: a file that WAS a default agent, now moved into a pack.
        # Step 1: ship it as a default and install.
        free = self.orch / "templates/agents/free/alpha-agent.md"
        free.write_text(_ALPHA_AGENT, encoding="utf-8")
        # Remove it from the pack table so it is a pure default for step 1.
        (self.orch / "templates/packs/packs.toml").write_text(
            _FIXTURE_TABLE.replace(
                '  "alpha-pack/agents/alpha-agent.md",\n', ""),
            encoding="utf-8")
        first = self._install(update_mode=False)
        self.assertEqual(first["errors"], [], first)
        installed = self.proj / ".claude/agents/alpha-agent.md"
        self.assertTrue(installed.is_file())

        # Step 2: upstream RETIRES the default copy (moves it into the pack).
        free.unlink()
        (self.orch / "templates/packs/packs.toml").write_text(
            _FIXTURE_TABLE, encoding="utf-8")
        # A plain update orphan-removes the unmodified former-default copy.
        upd = self._install(update_mode=True)
        self.assertIn(str(Path(".claude/agents/alpha-agent.md")),
                      upd["actions"]["orphan-deleted"])
        self.assertFalse(installed.exists())

        # Step 3: opting into the pack re-delivers through the ordinary path.
        re_add = self._install(update_mode=True, packs={"alpha-pack"})
        self.assertEqual(re_add["packs_installed"], ["alpha-pack"])
        self.assertTrue(installed.is_file())
        self.assertIn("alpha-pack", self._manifest()["packs"])


class BrokenTableSafetyTests(_PackCase):
    def _break_table(self) -> None:
        (self.orch / "templates/packs/packs.toml").write_text(
            "this is not = valid = toml\n", encoding="utf-8")

    def test_broken_table_is_loud_and_never_orphan_deletes_recorded_members(self):
        self._install(update_mode=False, packs={"alpha-pack"})
        installed = self.proj / ".claude/skills/alpha-skill/SKILL.md"
        self.assertTrue(installed.is_file())
        self._break_table()

        # A plain update with a broken table: LOUD error, and the recorded
        # pack's members are carried forward — NOT orphan-deleted.
        result = self._install(update_mode=True)
        self.assertTrue(result["errors"], "a broken table must surface as an error")
        self.assertTrue(any("packs.toml" in (e.get("path", "") + e.get("error", ""))
                            for e in result["errors"]), result["errors"])
        self.assertTrue(installed.is_file(),
                        "a broken table must never orphan-delete installed members")
        self.assertIn("alpha-pack", self._manifest()["packs"])

    def test_status_refusal_shape_matches_contract(self):
        self._break_table()
        reply = self._run_status_cli(expect_rc=1)
        self._assert_matches_contract(reply, kind="refusal")


class LiveParserRejectionTests(_PackCase):
    def test_unknown_pack_name_is_refused_by_the_live_parser(self):
        # Memory rule: drive the LIVE CLI parser, not an argv-shape guess.
        proc = self._cli("--pack", "definitely-not-a-pack")
        self.assertEqual(proc.returncode, 2, proc.stderr[-500:])
        self.assertIn("invalid choice", proc.stderr)
        self.assertIn("definitely-not-a-pack", proc.stderr)

    def test_known_pack_name_passes_the_live_parser_and_installs(self):
        proc = self._cli("--pack", "alpha-pack", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        result = json.loads(proc.stdout)
        self.assertEqual(result["packs_installed"], ["alpha-pack"])
        self.assertTrue((self.proj / ".claude/agents/alpha-agent.md").is_file())

    def _cli(self, *extra: str) -> subprocess.CompletedProcess:
        return self._spawn(
            [sys.executable, "-m", "vco_lib.project_init", "install-bundle",
             "--folder", str(self.proj), "--orchestrator-root", str(self.orch),
             *extra])


class StatusContractTests(_PackCase):
    """The binding cross-lane contract: my ``status --json`` output validates
    against the SAME committed fixture the Rust parser (packs_cmd.rs) tests
    against — a field rename on either side fails here, not at runtime."""

    def test_status_success_shape_and_installed_flag(self):
        reply = self._run_status_cli()
        self._assert_matches_contract(reply, kind="success")
        names = {r["name"] for r in reply["packs"]}
        self.assertEqual(names, {"alpha-pack", "beta-pack"})
        alpha = next(r for r in reply["packs"] if r["name"] == "alpha-pack")
        self.assertFalse(alpha["installed"])
        # members = agent stem + skill dir name (the contract's member rule).
        self.assertEqual(alpha["members"], ["alpha-agent", "alpha-skill"])

        # After installing alpha-pack, `installed` flips true.
        self._install(update_mode=False, packs={"alpha-pack"})
        reply2 = self._run_status_cli()
        alpha2 = next(r for r in reply2["packs"] if r["name"] == "alpha-pack")
        self.assertTrue(alpha2["installed"])
        beta2 = next(r for r in reply2["packs"] if r["name"] == "beta-pack")
        self.assertFalse(beta2["installed"])


class RealTableContractTests(unittest.TestCase):
    """The REAL repo table must produce the member lists the committed fixture
    hard-codes for two packs — the binding that keeps L1's table and L3's Rust
    fixture from drifting (both test against the one file)."""

    def test_real_table_matches_committed_fixture_members(self):
        contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
        table = packs_mod.load_packs(REPO_ROOT)
        for row in contract["status_reply"]["packs"]:
            name = row["name"]
            if "members" not in row:
                continue  # the fixture's minimal row (name only) is allowed
            self.assertIn(name, table, "fixture names a pack absent from the table")
            self.assertEqual(
                packs_mod.member_names(table[name]), row["members"],
                f"pack {name!r} members drifted from the committed contract")
            self.assertEqual(table[name].description, row.get("description",
                             table[name].description))


class ManifestDismissalCarryTests(_PackCase):
    """The manifest-writer bug found while threading `packs`: the engine builds
    a FRESH payload each run, so any additive key it does not carry forward is
    silently dropped. `dismissals` was being wiped by every bundle update —
    breaking the v0.2.91 B-F7 dismissal-memory promise (a dismissed nudge
    re-fired). `packs` and `dismissals` are the same additive-key family."""

    def test_engine_update_preserves_dismissals(self):
        from vco_lib import deferral_dismissal as dd
        self._install(update_mode=False)
        dd.record_dismissal(self.proj, "dual_ollama_detected",
                            {"alt_port": "11434", "canon_port": "11435"})
        self.assertIn("dismissals", self._manifest())
        # A bundle update must not cost the user their dismissal memory.
        self._install(update_mode=True)
        man = self._manifest()
        self.assertIn("dual_ollama_detected", man.get("dismissals", {}),
                      "the engine write dropped the dismissals memory")


class PureHelperTests(unittest.TestCase):
    def test_effective_packs_union_minus_removals(self):
        self.assertEqual(
            packs_mod.effective_packs(["a", "b"], ["c"], ["b"]),
            ["a", "c"])
        self.assertEqual(packs_mod.effective_packs([], [], []), [])
        self.assertEqual(packs_mod.effective_packs(["a"], [], ["a"]), [])

    def test_root_from_argv_honours_explicit_root(self):
        got = packs_mod.root_from_argv(
            ["install-bundle", "--orchestrator-root", str(REPO_ROOT), "--pack", "x"])
        self.assertEqual(got, REPO_ROOT.resolve())
        got2 = packs_mod.root_from_argv(
            ["install-bundle", f"--orchestrator-root={REPO_ROOT}"])
        self.assertEqual(got2, REPO_ROOT.resolve())


if __name__ == "__main__":
    unittest.main()
