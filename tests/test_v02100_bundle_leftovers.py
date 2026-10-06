# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-15 — bundle delivery completeness.

Real git fixture of a templates tree (the leftover rule needs HISTORY):

* an agent an earlier release shipped and a later one moved to ``_archive/`` →
  removed, backed up, reported with the release that shipped it;
* a user file with a VCO-like name (edited bytes, or a name VCO never shipped)
  → untouched and unreported;
* compose copies (owner Q3) → an unmodified copy removed, an edited copy backed
  up then removed, both reported; at the ORCHESTRATOR ROOT the same manifest
  entries are dropped and the live files are never touched;
* a script in a subdirectory of ``templates/scripts/`` ships to the same
  subpath (project AND root, one engine), ``__pycache__`` excluded;
* a tarball install (no git) → no-op with one log line;
* Windows ``\\`` manifest keys are normalised;
* F-W2-08(a) knowledge-only divergence is an informational count, never a
  warning or a pointer at a ledger entry that does not exist;
* F-W2-08(b) every real run appends to ``.claude/logs/bundle-install.log``;
* the reconcile clears the one-shot records on the next quiet update;
* "is this VCO's artefact?" has ONE home — ``_file_action`` and the leftover
  pass reach the same ``shipped_artifact.match_shipped_history``.
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

from vco_lib import bundle_leftovers, project_init, shipped_artifact  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.hashing import sha256_bytes  # noqa: E402

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

OLD_AGENT = b"---\nname: old-helper\n---\n# Old helper\nPlain body, no placeholders.\n"
OLD_SKILL = b"---\nname: old-skill\n---\n# Old skill\nPlain body, no placeholders.\n"
OLD_SPEC = b"# Old field\nA plain specialisation doc, no placeholders.\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout


def _make_templates(root: Path) -> None:
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    t = root / "templates"
    (t / "hooks" / "_lib").mkdir(parents=True)
    (t / "hooks" / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (t / "hooks" / "_lib" / "x.sh").write_text("# lib\n", encoding="utf-8")
    (t / "scripts" / "sub").mkdir(parents=True)
    (t / "scripts" / "kg-search").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (t / "scripts" / "sub" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (t / "scripts" / "__pycache__").mkdir()
    (t / "scripts" / "__pycache__" / "junk.py").write_text("junk\n", encoding="utf-8")
    (t / "agents" / "free").mkdir(parents=True)
    # Neutral fixture name — no shipped-catalogue meaning (retired in v0.2.101).
    (t / "agents" / "free" / "example-agent.md").write_text(
        "# Example agent\nAt {{ORCHESTRATOR_ROOT}}\n", encoding="utf-8")
    (t / "agents" / "free" / "old-helper.md").write_bytes(OLD_AGENT)
    # v0.2.101 §9.3: a prior-shipped SKILL directory and a SPECIALIZATIONS doc
    # (the new plain-copy kind) — both retired in a later commit and proved by
    # git-history byte-match in the leftover pass. No placeholders, so the
    # installed bytes equal the retired template blob exactly.
    (t / "skills" / "old-skill").mkdir(parents=True)
    (t / "skills" / "old-skill" / "SKILL.md").write_bytes(OLD_SKILL)
    (t / "specializations" / "fields").mkdir(parents=True)
    (t / "specializations" / "fields" / "old-field.md").write_bytes(OLD_SPEC)
    settings = json.dumps({"permissions": {"allow": ["Bash"]}, "hooks": {}})
    (t / "settings.json.linux.template").write_text(settings, encoding="utf-8")
    (t / "settings.json.windows.template").write_text(settings, encoding="utf-8")
    (root / "infrastructure").mkdir()
    (root / "infrastructure" / "docker-compose.yml").write_text("services: {}\n",
                                                                 encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v02100-leftovers-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_templates(self.orch)
        _git(self.orch, "init", "-q")
        _git(self.orch, "add", "-A")
        _git(self.orch, "commit", "-q", "-m", "v1")
        _git(self.orch, "tag", "v0.0.1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archive_old_helper(self) -> None:
        (self.orch / "templates" / "agents" / "_archive").mkdir()
        _git(self.orch, "mv", "templates/agents/free/old-helper.md",
             "templates/agents/_archive/old-helper.md")
        _git(self.orch, "commit", "-q", "-m", "archive old-helper")

    def _git_rm(self, *rels: str) -> None:
        """Retire template paths (git history keeps the shipped blob, so the
        leftover pass can still byte-match a hand-restored copy)."""
        _git(self.orch, "rm", "-q", *rels)
        _git(self.orch, "commit", "-q", "-m", f"retire {rels}")

    def _drop_manifest_entries(self, *rels: str) -> None:
        mpath = self.proj / ".claude/.vco-manifest.json"
        manifest = json.loads(mpath.read_text())
        for rel in rels:
            manifest["files"].pop(rel, None)
        mpath.write_text(json.dumps(manifest))

    def _install(self, folder=None, **kw):
        return project_init.install_project_bundle(
            folder or self.proj, orchestrator_root=self.orch, **kw)

    def _ledger(self, folder=None) -> dict:
        return {e.condition_id: e for e in DeferralReport.read(folder or self.proj).entries}


class LeftoverPolicyTests(_Base):
    def test_archived_agent_outside_manifest_is_backed_up_and_removed(self):
        self._install(update_mode=False)
        # A pre-manifest copy: VCO's bytes on disk, no manifest entry.
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        manifest["files"].pop(".claude/agents/old-helper.md")
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        self._archive_old_helper()

        result = self._install(update_mode=True)

        target = self.proj / ".claude/agents/old-helper.md"
        self.assertFalse(target.exists())
        self.assertEqual(result["leftovers_removed"], [".claude/agents/old-helper.md"])
        backups = list((self.proj / ".claude/backups/bundle-adoptions").rglob("old-helper.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), OLD_AGENT)
        row = self._ledger()["bundle_leftover_removed"]
        self.assertIn(".claude/agents/old-helper.md", row.detected)
        self.assertIn("v0.0.1", row.detected)
        self.assertIn("backup:", row.detected)
        self.assertTrue(any("retired VCO file" in n for n in result.get("notes", [])))

    def test_retired_hook_lib_file_is_removed_from_an_installed_project(self):
        """v0.2.101 wave-3 (injection redesign SF-2): four `_lib` helpers
        (codegraph-query, command-noise-strip, query-cache ×2 flavours) were
        DELETED from templates/hooks/_lib/ — installed projects carry copies
        under `.claude/hooks/_lib/`, and nothing sources them any more. This
        pins the delivery chain for that shape on the fixture stand-in
        (`_lib/x.sh`, shipped by _make_templates): a manifest-tracked _lib
        file whose template path was retired must NOT survive the next
        bundle update as an inert orphan.

        The retirement pins in test_codegraph_hook_gates_v0270.py only prove
        the TEMPLATES absence; this row proves the installed-project side.
        """
        self._install(update_mode=False)
        lib = self.proj / ".claude" / "hooks" / "_lib" / "x.sh"
        self.assertTrue(lib.is_file(), "fixture precondition: the lib shipped")
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        tracked = ".claude/hooks/_lib/x.sh" in manifest["files"]

        self._git_rm("templates/hooks/_lib/x.sh")
        result = self._install(update_mode=True)

        self.assertFalse(lib.exists(), (
            "a retired _lib file survived the update as an inert orphan "
            f"(manifest-tracked before update: {tracked}); result keys: "
            f"{ {k: v for k, v in result.items() if 'leftover' in k or 'orphan' in k} }"
        ))

    def test_hand_restored_retired_hook_lib_file_is_leftover_removed(self):
        """The pre-manifest / hand-restored shape for a `_lib` file: on disk
        with VCO's exact retired bytes, NOT in the manifest → the leftover
        pass byte-matches it against git history, backs it up and removes it
        (same contract the agents/skills rows pin, proving the hooks kind
        walks the `_lib/` subdirectory)."""
        self._install(update_mode=False)
        lib = self.proj / ".claude" / "hooks" / "_lib" / "x.sh"
        shipped_bytes = lib.read_bytes()
        self._drop_manifest_entries(".claude/hooks/_lib/x.sh")
        self._git_rm("templates/hooks/_lib/x.sh")

        result = self._install(update_mode=True)

        self.assertFalse(lib.exists())
        self.assertIn(".claude/hooks/_lib/x.sh", result["leftovers_removed"])
        backups = list((self.proj / ".claude/backups/bundle-adoptions").rglob("x.sh"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), shipped_bytes)

    def test_user_files_with_vco_like_names_are_untouched_and_unreported(self):
        self._install(update_mode=False)
        self._archive_old_helper()
        edited = self.proj / ".claude/agents/old-helper.md"
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        manifest["files"].pop(".claude/agents/old-helper.md")
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        edited.write_bytes(OLD_AGENT + b"my own line\n")
        never = self.proj / ".claude/agents/glm-flash-reviewer.md"
        never.write_text("hand-written\n", encoding="utf-8")
        mine = self.proj / ".claude/scripts/my-tool.py"
        mine.write_text("print('mine')\n", encoding="utf-8")

        result = self._install(update_mode=True)

        for p in (edited, never, mine):
            self.assertTrue(p.exists(), p)
        self.assertNotIn("leftovers_removed", result)
        self.assertNotIn("bundle_leftover_removed", self._ledger())

    def test_tarball_install_is_a_noop_with_one_log_line(self):
        self._install(update_mode=False)
        shutil.rmtree(self.orch / ".git")
        stray = self.proj / ".claude/hooks/old.sh"
        stray.write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
        logs = []
        out = bundle_leftovers.remove_vco_leftovers(
            self.proj, self.orch, known_rels=(), backup_ts=lambda: "T",
            log=logs.append)
        self.assertEqual(out.removed, [])
        self.assertTrue(stray.exists())
        self.assertEqual(len(logs), 1)
        self.assertIn("no git history", logs[0])

    def test_quiet_update_clears_the_one_shot_record(self):
        self._install(update_mode=False)
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        manifest["files"].pop(".claude/agents/old-helper.md")
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        self._archive_old_helper()
        self._install(update_mode=True)
        self.assertIn("bundle_leftover_removed", self._ledger())
        self._install(update_mode=True)
        self.assertNotIn("bundle_leftover_removed", self._ledger())


class RetiredSkillAndSpecializationLeftoverTests(_Base):
    """v0.2.101 §9.3: a prior-shipped SKILL directory and a retired
    SPECIALIZATIONS doc (the new plain-copy kind, `_KIND_SOURCES`) follow the
    SAME leftover rule as a retired agent — an outside-the-manifest copy is
    backed up + removed on a git-history byte-match, a user-modified copy is
    untouched and unreported."""

    SKILL_REL = ".claude/skills/old-skill/SKILL.md"
    SPEC_REL = ".claude/specializations/fields/old-field.md"

    def test_retired_skill_and_spec_outside_manifest_removed_with_backup(self):
        self._install(update_mode=False)
        skill = self.proj / self.SKILL_REL
        spec = self.proj / self.SPEC_REL
        self.assertTrue(skill.is_file() and spec.is_file())
        # Pre-manifest / hand-restored shape: on disk, NOT in the manifest.
        self._drop_manifest_entries(self.SKILL_REL, self.SPEC_REL)
        self._git_rm("templates/skills/old-skill/SKILL.md",
                     "templates/specializations/fields/old-field.md")

        result = self._install(update_mode=True)

        self.assertFalse(skill.exists())
        self.assertFalse(spec.exists())
        self.assertEqual(set(result["leftovers_removed"]),
                         {self.SKILL_REL, self.SPEC_REL})
        backups = {b.name for b in
                   (self.proj / ".claude/backups/bundle-adoptions").rglob("*")
                   if b.is_file()}
        self.assertEqual(backups, {"SKILL.md", "old-field.md"})
        row = self._ledger()["bundle_leftover_removed"]
        self.assertIn("old-skill/SKILL.md", row.detected)
        self.assertIn("old-field.md", row.detected)
        # The emptied skill directory is pruned, not left behind.
        self.assertFalse((self.proj / ".claude/skills/old-skill").exists())

    def test_user_modified_retired_spec_is_untouched_and_unreported(self):
        self._install(update_mode=False)
        spec = self.proj / self.SPEC_REL
        self._drop_manifest_entries(self.SPEC_REL)
        spec.write_bytes(OLD_SPEC + b"my own notes\n")
        self._git_rm("templates/specializations/fields/old-field.md")

        result = self._install(update_mode=True)

        self.assertTrue(spec.exists())
        self.assertEqual(spec.read_bytes(), OLD_SPEC + b"my own notes\n")
        self.assertNotIn("leftovers_removed", result)
        self.assertNotIn("bundle_leftover_removed", self._ledger())


class ComposeCopyTests(_Base):
    def _seed_copies(self, folder: Path, sep: str = "/") -> tuple:
        shipped = (self.orch / "infrastructure/docker-compose.yml").read_bytes()
        infra = folder / "infrastructure"
        infra.mkdir(exist_ok=True)
        (infra / "docker-compose.yml").write_bytes(shipped)
        (infra / "podman-compose.gpu.yml").write_text("services: {edited: 1}\n",
                                                      encoding="utf-8")
        mpath = folder / ".claude/.vco-manifest.json"
        manifest = json.loads(mpath.read_text())
        for name in ("docker-compose.yml", "podman-compose.gpu.yml"):
            manifest["files"][f"infrastructure{sep}{name}"] = {
                "sha256": sha256_bytes(shipped), "source": f"infrastructure/{name}"}
        mpath.write_text(json.dumps(manifest))
        return infra / "docker-compose.yml", infra / "podman-compose.gpu.yml"

    def test_project_copies_removed_edited_one_backed_up_both_reported(self):
        self._install(update_mode=False)
        self.assertFalse((self.proj / "infrastructure").exists(),
                         "compose files must no longer ship into a project")
        plain, edited = self._seed_copies(self.proj)

        result = self._install(update_mode=True)

        self.assertFalse(plain.exists())
        self.assertFalse(edited.exists())
        self.assertEqual(sorted(result["compose_copies_removed"]),
                         ["infrastructure/docker-compose.yml",
                          "infrastructure/podman-compose.gpu.yml"])
        backups = self.proj / ".claude/backups/bundle-adoptions"
        self.assertEqual([p.name for p in backups.rglob("*.yml")], ["podman-compose.gpu.yml"])
        self.assertIn("/infrastructure/", next(backups.rglob("*.yml")).as_posix())
        row = self._ledger()["bundle_compose_copies_removed"]
        self.assertIn("infrastructure/docker-compose.yml", row.detected)
        self.assertIn("infrastructure/podman-compose.gpu.yml", row.detected)
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        self.assertFalse([k for k in manifest["files"] if k.startswith("infrastructure")])

    def test_windows_backslash_manifest_keys_are_normalised(self):
        self.assertTrue(bundle_leftovers.is_compose_copy("infrastructure\\docker-compose.yml"))
        self._install(update_mode=False)
        # A `.claude\agents\...` key is KNOWN to the leftover pass: never a candidate.
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        entry = manifest["files"].pop(".claude/agents/old-helper.md")
        manifest["files"][".claude\\agents\\old-helper.md"] = entry
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        self._archive_old_helper()
        out = bundle_leftovers.remove_vco_leftovers(
            self.proj, self.orch, known_rels=set(manifest["files"]),
            backup_ts=lambda: "T")
        self.assertEqual(out.removed, [])
        self.assertTrue((self.proj / ".claude/agents/old-helper.md").exists())

    def test_root_target_drops_entries_and_never_touches_live_compose(self):
        self._install(folder=self.orch, update_mode=False)
        live, _ = (self.orch / "infrastructure/docker-compose.yml",
                   self.orch / "infrastructure/podman-compose.gpu.yml")
        before = live.read_bytes()
        mpath = self.orch / ".claude/.vco-manifest.json"
        manifest = json.loads(mpath.read_text())
        manifest["files"]["infrastructure/docker-compose.yml"] = {
            "sha256": sha256_bytes(before), "source": "infrastructure/docker-compose.yml"}
        mpath.write_text(json.dumps(manifest))

        result = self._install(folder=self.orch, update_mode=True)

        self.assertEqual(live.read_bytes(), before)
        self.assertNotIn("compose_copies_removed", result)
        manifest = json.loads(mpath.read_text())
        self.assertNotIn("infrastructure/docker-compose.yml", manifest["files"])


class ScriptSubdirectoryTests(_Base):
    def test_subdirectory_script_ships_to_project_and_root_pycache_never(self):
        for folder in (self.proj, self.orch):
            result = self._install(folder=folder, update_mode=False)
            shipped = folder / ".claude/scripts/sub/helper.py"
            self.assertTrue(shipped.is_file(), folder)
            self.assertEqual(shipped.read_text(encoding="utf-8"), "X = 1\n")
            self.assertFalse((folder / ".claude/scripts/__pycache__").exists())
            self.assertIn(str(Path(".claude/scripts/sub/helper.py")),
                          result["actions"]["create"])


class KnowledgeKeptAndRunLogTests(_Base):
    def test_knowledge_only_divergence_is_information_not_a_warning(self):
        (self.orch / "templates/knowledge").mkdir(parents=True)
        (self.orch / "templates/knowledge/TAG_HIERARCHY.md").write_text("# tags v1\n",
                                                                         encoding="utf-8")
        self._install(update_mode=False)
        (self.proj / "knowledge/TAG_HIERARCHY.md").write_text("# my tags\n", encoding="utf-8")
        (self.orch / "templates/knowledge/TAG_HIERARCHY.md").write_text("# tags v2\n",
                                                                         encoding="utf-8")

        result = self._install(update_mode=True)

        self.assertEqual(result["knowledge_kept"], [str(Path("knowledge/TAG_HIERARCHY.md"))])
        self.assertTrue(any("knowledge file(s) kept" in n for n in result["notes"]))
        joined = " ".join(result["warnings"])
        self.assertNotIn("UPDATE_DEFERRED", joined)
        self.assertNotIn("--force", joined)
        self.assertNotIn("bundle_user_modified_preserved", self._ledger())
        lines = project_init.format_bundle_result_lines(result)
        self.assertTrue(any(ln.startswith("  NOTE ") and "knowledge" in ln for ln in lines))
        self.assertFalse(any(ln.startswith("  WARNING ") and "knowledge" in ln for ln in lines))

    def test_every_real_run_appends_to_the_project_bundle_log(self):
        self._install(update_mode=False)
        log = self.proj / ".claude/logs/bundle-install.log"
        self.assertEqual(log.read_text(encoding="utf-8").count("== "), 1)
        self._install(update_mode=True)
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text.count("== "), 2)
        self.assertIn("bundle update", text)
        self._install(update_mode=True, dry_run=True)
        self.assertEqual(log.read_text(encoding="utf-8").count("== "), 2)


class OnePredicateTests(_Base):
    def test_file_action_and_leftover_pass_share_the_predicate(self):
        self._install(update_mode=False)
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        manifest["files"].pop(".claude/agents/old-helper.md")
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        self._archive_old_helper()
        real = shipped_artifact.match_shipped_history
        calls = []

        def spy(root, rels, installed_hash, **kw):
            calls.append(tuple(rels))
            return real(root, rels, installed_hash, **kw)

        # `_file_action` reaches it through installed_matches_template_history:
        # edit a shipped hook with no manifest entry so the history heal runs.
        manifest = json.loads((self.proj / ".claude/.vco-manifest.json").read_text())
        manifest["files"].pop(".claude/hooks/foo.sh")
        (self.proj / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
        (self.orch / "templates/hooks/foo.sh").write_text("#!/bin/sh\necho v2\n",
                                                          encoding="utf-8")
        with mock.patch.object(shipped_artifact, "match_shipped_history", spy):
            self._install(update_mode=True)
        self.assertIn(("templates/hooks/foo.sh",), calls)
        self.assertIn(("templates/agents/free/old-helper.md",
                       "templates/agents/_archive/old-helper.md"), calls)


class LedgerMergeReadTests(unittest.TestCase):
    """F-W2-08(c): an entry a standalone Rust writer added to the Markdown is
    READ even when the JSON sidecar exists, so the next Python write keeps it."""

    def test_markdown_only_rust_entry_survives_a_python_write(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            rep = DeferralReport()
            rep.add_entry(DeferralEntry(
                condition_id="python_one", title="p", detected="d",
                why_deferred="w", command_to_apply="echo", severity="info"))
            self.assertTrue(rep.write(folder))
            md = folder / ".claude/context/UPDATE_DEFERRED.md"
            md.write_text(md.read_text(encoding="utf-8") + (
                "\n## update_resume_required (warning)\n\n**Title**: resume\n\n"
                "**Detected**: d\n\n**Why deferred**: w\n\n**To apply**:\n```bash\n"
                "python install.py --update\n```\n\n---\n"), encoding="utf-8")
            ids = {e.condition_id for e in DeferralReport.read(folder).entries}
            self.assertEqual(ids, {"python_one", "update_resume_required"})
            again = DeferralReport.read(folder)
            again.write(folder)
            ids = {e.condition_id for e in DeferralReport.read(folder).entries}
            self.assertEqual(ids, {"python_one", "update_resume_required"})


if __name__ == "__main__":
    unittest.main()
