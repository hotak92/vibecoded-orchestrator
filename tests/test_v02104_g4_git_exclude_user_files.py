# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 G4: `.git/info/exclude` must never hide the user's own `.claude/` files.

Up to v0.2.103 every bundle add/update wrote one blanket `/.claude/` line, so
`git add .claude/CONTEXT_STATE.md` (and every new plan, handoff or
`settings.json`) failed with "paths are ignored". These tests drive a REAL
bundle run into a REAL `git init` repo and ask git itself (`git check-ignore`,
`git add`) what is hidden — the pattern list alone proves nothing about how
git reads it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import git_exclude, project_init  # noqa: E402
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402

#: Files a user versions under `.claude/` — none may be ignored.
USER_FILES = (
    ".claude/CONTEXT_STATE.md",
    ".claude/CLAUDE.md",
    ".claude/settings.json",
    ".claude/context/plans/PLAN-feature.md",
    ".claude/context/handoffs/HANDOFF-today.md",
    ".claude/agents/my-own-agent.md",  # beside VCO's shipped agents
    ".claude/hooks/my-own-hook.sh",  # beside VCO's shipped hooks
    ".claude/skills/my-own-skill/SKILL.md",  # beside VCO's shipped skills
)

#: VCO-managed paths — every one must be ignored.
VCO_FILES = (
    ".claude/.vco-manifest.json",
    ".claude/state/lean-ctx-tee/x.log",
    ".claude/logs/run.log",
    ".claude/backups/bundle-adoptions/ts/a.sh",
    ".claude/worktrees/agent-1/file.txt",
    ".claude/agents.disabled/parked.md",
    ".claude/context/UPDATE_DEFERRED.md",
    ".claude/hooks/foo.sh",  # shipped by the fake orchestrator
    ".claude/hooks/_lib/find-python.sh",
    ".claude/scripts/kg-search",
    ".claude/skills/architect/SKILL.md",
)


def _git_env() -> dict:
    env = dict(os.environ)
    # Keep the machine's global/system git config (a core.excludesFile, say)
    # from deciding what this test sees.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_git_env(), check=False,
    )


def _ignored(repo: Path, rel: str) -> bool:
    res = _git(repo, "check-ignore", "-q", "--no-index", rel)
    if res.returncode not in (0, 1):
        raise AssertionError(f"git check-ignore failed: {res.stderr}")
    return res.returncode == 0


@unittest.skipUnless(shutil.which("git"), "git is required")
class G4BundleExcludeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-g4-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        res = _git(self.proj, "init", "-q")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.exclude = self.proj / ".git" / "info" / "exclude"

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _bundle(self, *, update_mode: bool, safe_add: bool = False) -> dict:
        return project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=update_mode,
            safe_add=safe_add,
            log_event=lambda *a, **k: None,
        )

    def _touch(self, rels) -> None:
        for rel in rels:
            p = self.proj / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("x\n", encoding="utf-8")

    def _assert_split(self) -> None:
        self._touch(USER_FILES + VCO_FILES)
        hidden_user = [r for r in USER_FILES if _ignored(self.proj, r)]
        self.assertEqual(hidden_user, [], "the user's own .claude/ files are hidden")
        visible_vco = [r for r in VCO_FILES if not _ignored(self.proj, r)]
        self.assertEqual(visible_vco, [], "VCO-managed paths are not excluded")

    def test_fresh_add_hides_vco_paths_only(self):
        self._bundle(update_mode=False)
        self.assertNotIn("/.claude/", self.exclude.read_text(encoding="utf-8").splitlines())
        self._assert_split()

    def test_safe_add_hides_vco_paths_only(self):
        self._bundle(update_mode=False, safe_add=True)
        self._assert_split()

    def test_git_add_of_context_state_succeeds_without_force(self):
        self._bundle(update_mode=False)
        self._touch((".claude/CONTEXT_STATE.md", ".claude/context/plans/p.md"))
        res = _git(self.proj, "add", ".claude/CONTEXT_STATE.md", ".claude/context/plans/p.md")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_update_migrates_the_legacy_glob_and_keeps_user_lines(self):
        self._bundle(update_mode=False)
        # Rewrite the exclude file as v0.2.103 left it: the user's own lines,
        # then VCO's block carrying the blanket glob + the root manifest line.
        legacy = (
            "# git ls-files --others --exclude-from=.git/info/exclude\n"
            "*.swp\n"
            "/my-scratch/\n"
            "\n"
            "# VCO safe-add (v0.2.63): keep orchestrator-created files out of your commits.\n"
            "# This is .git/info/exclude (LOCAL-only) — not the tracked .gitignore.\n"
            "/.claude/\n"
            "/.vco-manifest.json\n"
            "/CLAUDE.md\n"
        )
        self.exclude.write_text(legacy, encoding="utf-8")
        self.assertTrue(_ignored(self.proj, ".claude/CONTEXT_STATE.md"))  # the bug

        result = self._bundle(update_mode=True)
        g1 = result["g1_git_exclude"]
        self.assertEqual(sorted(g1["removed"]), ["/.claude/", "/.vco-manifest.json"])
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("/.claude/", lines)
        self.assertNotIn("/.vco-manifest.json", lines)
        for kept in ("*.swp", "/my-scratch/", "/CLAUDE.md"):
            self.assertIn(kept, lines)
        self._assert_split()

        # Idempotent: a second update changes nothing.
        before = self.exclude.read_bytes()
        again = self._bundle(update_mode=True)
        self.assertEqual(again["g1_git_exclude"]["action"], "noop")
        self.assertEqual(self.exclude.read_bytes(), before)

    def test_a_users_own_claude_glob_is_left_alone(self):
        self._bundle(update_mode=False)
        body = self.exclude.read_text(encoding="utf-8")
        self.exclude.write_text("/.claude/\n" + body, encoding="utf-8")
        result = self._bundle(update_mode=True)
        self.assertEqual(result["g1_git_exclude"]["removed"], [])
        self.assertEqual(self.exclude.read_text(encoding="utf-8").splitlines()[0], "/.claude/")

    def test_a_claude_line_the_user_appends_after_vcos_block_is_kept(self):
        """M2: `echo '/.claude/' >> .git/info/exclude` lands right after VCO's
        block; the next update must keep it (the user chose to hide .claude/)."""
        self._bundle(update_mode=False)
        with self.exclude.open("a", encoding="utf-8") as fh:
            fh.write("/.claude/\n")
        self.assertTrue(_ignored(self.proj, ".claude/CONTEXT_STATE.md"))
        result = self._bundle(update_mode=True)
        self.assertEqual(result["g1_git_exclude"]["removed"], [])
        self.assertEqual(self.exclude.read_text(encoding="utf-8").splitlines()[-1], "/.claude/")
        self._touch((".claude/CONTEXT_STATE.md",))
        self.assertTrue(_ignored(self.proj, ".claude/CONTEXT_STATE.md"))

    def test_a_users_claude_line_after_a_pre_v02104_block_is_kept(self):
        """M2, upgrade path: VCO's old glob goes, the user's appended copy stays."""
        self._bundle(update_mode=False)
        self.exclude.write_text(
            "\n# VCO safe-add (v0.2.63): keep orchestrator-created files out of your commits.\n"
            "# This is .git/info/exclude (LOCAL-only) — not the tracked .gitignore.\n"
            "/.claude/\n/CLAUDE.md\n/.claude/\n/my-notes/\n",
            encoding="utf-8",
        )
        result = self._bundle(update_mode=True)
        self.assertEqual(result["g1_git_exclude"]["removed"], ["/.claude/"])
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/.claude/"), 1)
        self.assertLess(lines.index("# VCO end"), lines.index("/.claude/"))
        self.assertIn("/my-notes/", lines)

    def test_a_retired_shipped_file_takes_its_exclude_line_with_it(self):
        """L4: a script VCO stops shipping is deleted by the update; its line
        must go too, or the user's own file at that path is hidden."""
        self._bundle(update_mode=False)
        self.assertIn("/.claude/scripts/notify.py",
                      self.exclude.read_text(encoding="utf-8").splitlines())
        (self.orch / "templates" / "scripts" / "notify.py").unlink()
        result = self._bundle(update_mode=True)
        self.assertIn(".claude/scripts/notify.py", result["actions"]["orphan-deleted"])
        self.assertEqual(result["g1_git_exclude"]["removed"], ["/.claude/scripts/notify.py"])
        self.assertNotIn("/.claude/scripts/notify.py",
                         self.exclude.read_text(encoding="utf-8").splitlines())
        self._touch((".claude/scripts/notify.py",))
        self.assertFalse(_ignored(self.proj, ".claude/scripts/notify.py"))
        self.assertTrue(_ignored(self.proj, ".claude/scripts/kg-search"))

    # -- R2 L1/L2/L3: a legacy block's end is inferred conservatively --------

    _LEGACY_HEADER = (
        "# VCO safe-add (v0.2.63): keep orchestrator-created files out of your commits.\n"
        "# This is .git/info/exclude (LOCAL-only) — not the tracked .gitignore.\n"
    )

    def test_vcos_legacy_glob_goes_even_when_the_user_duplicated_it_above(self):
        """L1: the user adds `/.claude/` at the TOP with an editor while VCO's
        old block below still carries its own. VCO's copy must be migrated, so
        removing the user's line later really makes `.claude/` addable."""
        self._bundle(update_mode=False)
        self.exclude.write_text(
            "/.claude/\n/.vco-manifest.json\n\n" + self._LEGACY_HEADER
            + "/.vco-manifest.json\n/.claude/\n/CLAUDE.md\n",
            encoding="utf-8",
        )
        result = self._bundle(update_mode=True)
        self.assertEqual(
            sorted(result["g1_git_exclude"]["removed"]), ["/.claude/", "/.vco-manifest.json"],
        )
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], ["/.claude/", "/.vco-manifest.json"])  # the user's
        self.assertEqual(lines.count("/.claude/"), 1)
        self.assertLess(lines.index("/CLAUDE.md"), lines.index("# VCO end"))
        # The user now removes their own line: nothing of VCO's hides .claude/.
        self.exclude.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        self._assert_split()

    def test_a_users_glob_after_a_block_ending_with_the_sidecar_is_kept(self):
        """L2: an old block with no glob (VCO wrote the sidecar line last)
        must not absorb the `/.claude/` the user appended after it."""
        self._bundle(update_mode=False)
        self.exclude.write_text(
            self._LEGACY_HEADER + "/.env.vco.reference\n/.claude/\n", encoding="utf-8",
        )
        result = self._bundle(update_mode=True)
        self.assertEqual(result["g1_git_exclude"]["removed"], [])
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/.claude/"), 1)
        self.assertLess(lines.index("# VCO end"), lines.index("/.claude/"))
        self._touch((".claude/CONTEXT_STATE.md",))
        self.assertTrue(_ignored(self.proj, ".claude/CONTEXT_STATE.md"))

    def test_a_users_claude_path_after_a_legacy_block_survives_retirement(self):
        """L3: no pre-v0.2.104 release wrote a `/.claude/<path>` line, so one
        after an old block is the user's — retiring a shipped file of the same
        name must not delete the user's hand-written exclude."""
        self._bundle(update_mode=False)
        self.exclude.write_text(
            self._LEGACY_HEADER + "/.claude/\n/CLAUDE.md\n/.claude/scripts/notify.py\n",
            encoding="utf-8",
        )
        (self.orch / "templates" / "scripts" / "notify.py").unlink()
        result = self._bundle(update_mode=True)
        self.assertIn(".claude/scripts/notify.py", result["actions"]["orphan-deleted"])
        self.assertEqual(result["g1_git_exclude"]["removed"], ["/.claude/"])
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        self.assertLess(lines.index("# VCO end"), lines.index("/.claude/scripts/notify.py"))
        self._touch((".claude/scripts/notify.py",))
        self.assertTrue(_ignored(self.proj, ".claude/scripts/notify.py"))

    def test_user_lines_appended_after_a_legacy_block_stay_outside_it(self):
        """L3: a directory pattern is never a legacy VCO entry, so it and every
        line after it stay outside the closed block for good."""
        self._bundle(update_mode=False)
        self.exclude.write_text(
            self._LEGACY_HEADER + "/.claude/\n/.vco-manifest.json\n/my-secrets/\n/notes.txt\n",
            encoding="utf-8",
        )
        result = self._bundle(update_mode=True)
        self.assertEqual(
            sorted(result["g1_git_exclude"]["removed"]), ["/.claude/", "/.vco-manifest.json"],
        )
        lines = self.exclude.read_text(encoding="utf-8").splitlines()
        end = lines.index("# VCO end")
        self.assertLess(end, lines.index("/my-secrets/"))
        self.assertLess(end, lines.index("/notes.txt"))
        self._touch(("my-secrets/k.txt", "notes.txt"))
        self.assertTrue(_ignored(self.proj, "my-secrets/k.txt"))
        self.assertTrue(_ignored(self.proj, "notes.txt"))
        self._assert_split()


class G4PatternUnitTests(unittest.TestCase):
    def test_user_owned_paths_are_never_patterns(self):
        for rel in (
            ".claude/CONTEXT_STATE.md", ".claude/settings.json", ".claude/CLAUDE.md",
            ".claude/context/plans/a.md", ".claude/context/handoffs/b.md",
        ):
            self.assertIsNone(git_exclude.exclude_pattern_for(rel), rel)

    def test_shipped_paths_get_their_own_entry(self):
        cases = {
            ".claude/hooks/foo.sh": "/.claude/hooks/foo.sh",
            r".claude\agents\x.md": "/.claude/agents/x.md",
            ".claude/hooks/_lib/a.sh": "/.claude/hooks/_lib/",
            ".claude/skills/architect/SKILL.md": "/.claude/skills/architect/",
            ".claude/specializations/x/y.md": "/.claude/specializations/x/y.md",
            ".claude/.vco-manifest.json": "/.claude/.vco-manifest.json",
            ".claude/state/x": "/.claude/state/",
            "CLAUDE.md": "/CLAUDE.md",
        }
        for rel, want in cases.items():
            self.assertEqual(git_exclude.exclude_pattern_for(rel), want, rel)

    def test_no_blanket_claude_glob_is_ever_produced(self):
        entries = git_exclude.safe_add_exclude_entries(
            {"actions": {"create": [".claude/hooks/a.sh", ".claude/CONTEXT_STATE.md"]}},
            Path("/nonexistent-folder-for-this-test"),
        )
        self.assertNotIn("/.claude/", entries)
        self.assertIn("/.claude/hooks/a.sh", entries)
        for runtime in git_exclude.VCO_RUNTIME_EXCLUDES:
            self.assertIn(runtime, entries)

    def test_unchanged_shipped_files_are_listed_on_update(self):
        """The migration drops `/.claude/` on the first update; files the run
        left untouched (`noop`) must get their own entry in that same run."""
        entries = git_exclude.safe_add_exclude_entries(
            {"actions": {"noop": [".claude/scripts/s.py"], "skip-existing": ["x.md"]}},
            Path("/nonexistent-folder-for-this-test"),
        )
        self.assertIn("/.claude/scripts/s.py", entries)
        self.assertNotIn("/x.md", entries, "a pre-existing user file is never hidden")

    def test_a_legacy_line_is_never_written(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".git" / "info").mkdir(parents=True)
            res = git_exclude.append_git_info_exclude(
                folder, ("/.claude/", "/.vco-manifest.json", "/.claude/state/"),
            )
            self.assertEqual(res["added"], ["/.claude/state/"])

    def test_deferral_summary_names_a_dozen_and_counts_the_rest(self):
        self.assertEqual(git_exclude.summarize_entries([]), "(none)")
        entries = [f"/e{i}" for i in range(15)]
        text = git_exclude.summarize_entries(entries)
        self.assertTrue(text.endswith("/e11 and 3 more"), text)

    def test_migration_only_touches_vco_blocks(self):
        text = "*.swp\n\n# VCO project move: x\n/.claude/\n/a\n\n/.claude/\n/.vco-manifest.json\n"
        new, removed = git_exclude._rewrite_vco_blocks(text)
        self.assertEqual(removed, ["/.claude/"])
        self.assertEqual(
            new,
            "*.swp\n\n# VCO project move: x\n/a\n# VCO end\n\n/.claude/\n/.vco-manifest.json\n",
        )

    def test_a_claude_line_after_vcos_newer_entries_is_the_users(self):
        """M2 (review reproduction) + R2 L3: no pre-v0.2.104 release wrote a
        per-path `/.claude/<file>` entry, so that line and everything after it
        (the `/.claude/` included) are the user's."""
        text = (
            "# VCO safe-add (v0.2.63): x\n# This is .git/info/exclude\n"
            "/.claude/hooks/a.sh\n/.claude/\n/my-own-private-notes/\n"
        )
        new, removed = git_exclude._rewrite_vco_blocks(text)
        self.assertEqual(removed, [])
        self.assertIn("/.claude/\n/my-own-private-notes/\n", new)
        self.assertLess(new.index("# VCO end"), new.index("/.claude/\n"))

    def test_a_repeated_line_is_never_vcos(self):
        """VCO de-duplicated against its own earlier lines: a repeat of one
        (same block or an earlier VCO block) ends the legacy block."""
        text = "# VCO safe-add: x\n/.claude/\n/CLAUDE.md\n/.claude/\n"
        new, removed = git_exclude._rewrite_vco_blocks(text)
        self.assertEqual(removed, ["/.claude/"])
        self.assertEqual(new, "# VCO safe-add: x\n/CLAUDE.md\n# VCO end\n/.claude/\n")

    def test_new_blocks_end_with_the_end_marker_and_are_stable(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".git" / "info").mkdir(parents=True)
            git_exclude.append_git_info_exclude(folder, ("/a", "/b"))
            body = (folder / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertTrue(body.endswith("/a\n/b\n# VCO end\n"), body)
            again = git_exclude.append_git_info_exclude(folder, ("/a", "/b"))
            self.assertEqual(again["action"], "noop")

    def test_an_ended_block_keeps_a_claude_line_the_user_put_inside_it(self):
        text = "# VCO safe-add: x\n/.claude/\n/a\n# VCO end\n"
        new, removed = git_exclude._rewrite_vco_blocks(text)
        self.assertEqual((new, removed), (text, []))

    def test_retired_patterns_leave_vco_blocks_only(self):
        text = (
            "/.claude/hooks/old.sh\n\n# VCO safe-add: x\n/.claude/hooks/old.sh\n"
            "/.claude/hooks/kept.sh\n# VCO end\n"
        )
        new, removed = git_exclude._rewrite_vco_blocks(text, ["/.claude/hooks/old.sh"])
        self.assertEqual(removed, ["/.claude/hooks/old.sh"])
        self.assertEqual(
            new,
            "/.claude/hooks/old.sh\n\n# VCO safe-add: x\n/.claude/hooks/kept.sh\n# VCO end\n",
        )

    def test_retired_entries_skip_patterns_still_shipped(self):
        envelope = {"actions": {
            "orphan-deleted": [".claude/hooks/old.sh", ".claude/skills/arch/gone.txt"],
            "orphan-retired": [".claude/agents/mine-now.md"],
        }}
        out = git_exclude.retired_exclude_entries(envelope, ["/.claude/skills/arch/"])
        self.assertEqual(out, ["/.claude/hooks/old.sh", "/.claude/agents/mine-now.md"])

    def test_specializations_are_excluded_per_shipped_file(self):
        """L6: a user's own review-kind doc beside the shipped ones stays addable."""
        self.assertEqual(
            git_exclude.exclude_pattern_for(".claude/specializations/review-kinds/code.md"),
            "/.claude/specializations/review-kinds/code.md",
        )


@unittest.skipUnless(shutil.which("git"), "git is required")
class G4ProjectMoveTests(unittest.TestCase):
    """A project move copies the user's own material to the new folder; the
    exclude it writes there must not hide that material (same rule)."""

    def test_move_excludes_vco_files_but_not_the_users_copied_material(self):
        import json as _json

        from vco_lib import project_move as pm
        from tests.test_v0292_wp17_project_move import make_manifest, write

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, dst = root / "src", root / "dst"
            src.mkdir()
            dst.mkdir()
            write(src / "knowledge" / "my-node.md", "mine\n")
            write(src / "CONTEXT_STATE.md", "root state\n")
            write(src / ".claude" / "context" / "plans" / "p.md", "plan\n")
            write(src / ".claude" / "state" / "s.json", "{}\n")
            make_manifest(src, files={}, preserved={})
            self.assertEqual(_git(dst, "init", "-q").returncode, 0)
            project = {"id": "p1", "name": "P", "slug": "p", "folder_path": str(src)}
            plan = pm.plan_move(
                project=project, dst=str(dst), registered=[project], into_existing=True,
            )
            envelope = {"actions": {"create": [".claude/hooks/a.sh"]}}

            def runner(argv, env):
                return subprocess.CompletedProcess(argv, 0, _json.dumps(envelope), "")

            result = pm.execute_pre_flip(plan, move_id="m1", runner=runner)
            self.assertEqual(result.git_exclude.get("action"), "appended")
            lines = (dst / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
            self.assertIn("/.claude/hooks/a.sh", lines)
            self.assertIn("/.claude/state/", lines)
            for leaked in ("/.claude/", "/knowledge/my-node.md", "/CONTEXT_STATE.md",
                           "/.claude/context/plans/p.md"):
                self.assertNotIn(leaked, lines)
            for rel in ("knowledge/my-node.md", "CONTEXT_STATE.md", ".claude/context/plans/p.md"):
                self.assertFalse(_ignored(dst, rel), rel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
