"""FS-disable contract tests for vco_lib.project_init.install_project_bundle.

Wave 2, Subagent D (2026-05-22). Plan:
.claude/context/plans/agent-skill-keyword-suggest-and-fs-disable.md

THE KEY INVARIANT (don't-resurrect rule):

When the launcher GUI disables an agent or skill, it moves the file out
of Claude's discovery glob:
  .claude/agents/<name>.md      ->  .claude/agents.disabled/<name>.md
  .claude/skills/<name>/        ->  .claude/skills.disabled/<name>/

The next `install-bundle --update` (or first-install) MUST NOT recreate
the enabled-side file. Doing so would silently undo the user's disable
choice. The launcher would then show the agent as enabled even though
the user explicitly disabled it.

Implementation: `_file_action` now classifies these ops as "skip-disabled".
A pure no-op (no FS write, no manifest update, no preservation entry).

Tests below directly exercise `install_project_bundle`'s lower-level
copy classification rather than the full launcher orchestration path,
because the don't-resurrect rule is enforced inside `_file_action`
regardless of how the call is dispatched.

Also covers:
  - `_agent_or_skill_already_present`: pure path-presence helper
    (mirror of Rust `resolve_kind_paths`-driven check in
    `launcher/src-tauri/src/commands/project_state_populate.rs`).
  - `_classify_bundle_op_kind`: dest_rel -> (kind, name) classifier.
  - Fresh-install behaviour unchanged.
  - Update-mode behaviour unchanged for non-disabled files.

These tests run fully offline using tempfile.TemporaryDirectory; no
network, no Weaviate, no launcher DB.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# Fake orchestrator fixture (slim — only the bits we need)
# ---------------------------------------------------------------------------


def _make_fake_orchestrator(root: Path) -> None:
    """Build a minimal orchestrator tree carrying ONE agent and ONE skill.

    Smaller than tests/test_install_bundle.py's `_make_fake_orchestrator`
    because these tests only need to exercise the agent/skill path; the
    bundle's hooks / scripts / settings are uninvolved in FS-disable.

    Layout:
      <root>/vct-module.json                     — repo-root marker
      <root>/templates/agents/free/foo.md
      <root>/templates/skills/tdd/SKILL.md
      <root>/templates/settings.json.{linux,windows}.template — required
        by _enumerate_bundle_files / smart-merge path.
    """
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")

    agents = root / "templates" / "agents" / "free"
    agents.mkdir(parents=True)
    (agents / "foo.md").write_text(
        "---\nname: foo\nmodel: sonnet\n---\n# foo agent body\n",
        encoding="utf-8",
    )

    skills = root / "templates" / "skills"
    tdd = skills / "tdd"
    tdd.mkdir(parents=True)
    (tdd / "SKILL.md").write_text(
        "---\nname: tdd\nmodel: sonnet\n---\n# tdd skill body\n",
        encoding="utf-8",
    )

    settings = {"hooks": {}}
    (root / "templates" / "settings.json.linux.template").write_text(
        json.dumps(settings), encoding="utf-8",
    )
    (root / "templates" / "settings.json.windows.template").write_text(
        json.dumps(settings), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Pure helper tests (no install)
# ---------------------------------------------------------------------------


class AgentSkillAlreadyPresentTests(unittest.TestCase):
    """`_agent_or_skill_already_present`: returns True when the entry
    exists at EITHER the enabled or disabled location.

    Mirror of `resolve_kind_paths`-driven check in the Rust populate
    (`launcher/src-tauri/src/commands/project_state_populate.rs`).
    Path math is platform-agnostic (pathlib.Path), no string concat.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-preserve-disabled-")
        self.project = Path(self._tmp.name)
        (self.project / ".claude" / "agents").mkdir(parents=True)
        (self.project / ".claude" / "agents.disabled").mkdir(parents=True)
        (self.project / ".claude" / "skills").mkdir(parents=True)
        (self.project / ".claude" / "skills.disabled").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_false_when_neither_location_has_agent(self) -> None:
        self.assertFalse(
            project_init._agent_or_skill_already_present(
                self.project, "foo", "agent"
            )
        )

    def test_returns_true_when_enabled_agent_present(self) -> None:
        (self.project / ".claude" / "agents" / "foo.md").write_text("x")
        self.assertTrue(
            project_init._agent_or_skill_already_present(
                self.project, "foo", "agent"
            )
        )

    def test_returns_true_when_disabled_agent_present(self) -> None:
        (self.project / ".claude" / "agents.disabled" / "foo.md").write_text("x")
        self.assertTrue(
            project_init._agent_or_skill_already_present(
                self.project, "foo", "agent"
            ),
            "Disabled-only agent must be treated as 'already present'",
        )

    def test_returns_true_when_both_locations_have_agent(self) -> None:
        (self.project / ".claude" / "agents" / "foo.md").write_text("x")
        (self.project / ".claude" / "agents.disabled" / "foo.md").write_text("x")
        self.assertTrue(
            project_init._agent_or_skill_already_present(
                self.project, "foo", "agent"
            )
        )

    def test_returns_false_when_neither_location_has_skill(self) -> None:
        self.assertFalse(
            project_init._agent_or_skill_already_present(
                self.project, "tdd", "skill"
            )
        )

    def test_returns_true_when_enabled_skill_dir_present(self) -> None:
        (self.project / ".claude" / "skills" / "tdd").mkdir()
        self.assertTrue(
            project_init._agent_or_skill_already_present(
                self.project, "tdd", "skill"
            )
        )

    def test_returns_true_when_disabled_skill_dir_present(self) -> None:
        (self.project / ".claude" / "skills.disabled" / "tdd").mkdir()
        self.assertTrue(
            project_init._agent_or_skill_already_present(
                self.project, "tdd", "skill"
            ),
            "Disabled-only skill must be treated as 'already present'",
        )

    def test_unknown_kind_returns_false(self) -> None:
        # Defensive: bogus `kind` arg returns False rather than raising.
        # The Python helper is called from `_classify_bundle_op_kind`
        # output and should be tolerant of unknown values for future
        # extensibility (hooks aren't FS-disabled today, but might be).
        self.assertFalse(
            project_init._agent_or_skill_already_present(
                self.project, "x", "hook"
            )
        )


class ClassifyBundleOpKindTests(unittest.TestCase):
    """`_classify_bundle_op_kind`: dest_rel -> (kind, name) classifier.

    Cross-platform: must handle both `/` and `\\` separators because
    `_BundleFileOp.dest_rel` is built via `str(Path(...))` whose
    separator depends on the host OS.
    """

    def test_agent_md_returns_agent_kind(self) -> None:
        # POSIX separator
        self.assertEqual(
            project_init._classify_bundle_op_kind(".claude/agents/foo.md"),
            ("agent", "foo"),
        )
        # Windows separator — must classify identically
        self.assertEqual(
            project_init._classify_bundle_op_kind(".claude\\agents\\foo.md"),
            ("agent", "foo"),
        )

    def test_skill_dir_returns_skill_kind(self) -> None:
        # Top-level SKILL.md
        self.assertEqual(
            project_init._classify_bundle_op_kind(
                ".claude/skills/tdd/SKILL.md"
            ),
            ("skill", "tdd"),
        )
        # Companion file inside the skill dir — same skill name returned
        self.assertEqual(
            project_init._classify_bundle_op_kind(
                ".claude/skills/tdd/extra.txt"
            ),
            ("skill", "tdd"),
        )
        # Windows separator
        self.assertEqual(
            project_init._classify_bundle_op_kind(
                ".claude\\skills\\tdd\\SKILL.md"
            ),
            ("skill", "tdd"),
        )

    def test_non_agent_skill_paths_return_none(self) -> None:
        # Hooks, scripts, settings, infra — not FS-disable subjects.
        self.assertIsNone(
            project_init._classify_bundle_op_kind(".claude/hooks/post-edit.sh")
        )
        self.assertIsNone(
            project_init._classify_bundle_op_kind(".claude/scripts/kg-search")
        )
        self.assertIsNone(
            project_init._classify_bundle_op_kind(".claude/settings.json")
        )
        self.assertIsNone(
            project_init._classify_bundle_op_kind(
                "infrastructure/docker-compose.yml"
            )
        )

    def test_agents_subfolder_md_does_not_classify(self) -> None:
        # `.claude/agents/<subdir>/<name>.md` — not the canonical layout.
        # Default to None rather than misclassifying.
        self.assertIsNone(
            project_init._classify_bundle_op_kind(
                ".claude/agents/legacy/foo.md"
            )
        )


# ---------------------------------------------------------------------------
# install_project_bundle integration: the don't-resurrect invariant
# ---------------------------------------------------------------------------


class InstallBundlePreservesDisabledTests(unittest.TestCase):
    """End-to-end: install_project_bundle MUST NOT recreate an
    enabled-side file when the disabled-side companion exists.

    Scenario sequence (mirrors the user flow):
      1. Fresh install -> agent .md lands in `.claude/agents/foo.md`.
      2. Launcher GUI disables the agent -> file is moved to
         `.claude/agents.disabled/foo.md`.
      3. User re-runs install-bundle (either via launcher's "Update
         bundle" button or a manual `python -m vco_lib.project_init
         install-bundle --update`).
      4. INVARIANT: the file does NOT reappear at `.claude/agents/foo.md`.
         The `.disabled/` copy is unchanged. The install reports the
         skip via `actions["skip-disabled"]`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-bundle-disabled-")
        root = Path(self._tmp.name)
        self.orch = root / "orchestrator"
        self.proj = root / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent_dest(self, name: str) -> Path:
        return self.proj / ".claude" / "agents" / f"{name}.md"

    def _agent_disabled_dest(self, name: str) -> Path:
        return self.proj / ".claude" / "agents.disabled" / f"{name}.md"

    def _skill_dest(self, name: str) -> Path:
        return self.proj / ".claude" / "skills" / name

    def _skill_disabled_dest(self, name: str) -> Path:
        return self.proj / ".claude" / "skills.disabled" / name

    # ---- Agents ----

    def test_fresh_install_lands_agent_in_enabled_location(self) -> None:
        """Baseline: nothing fancy, file goes to `.claude/agents/foo.md`."""
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        self.assertTrue(self._agent_dest("foo").exists())
        self.assertIn(
            str(Path(".claude") / "agents" / "foo.md"),
            result["actions"]["create"],
        )
        self.assertEqual(result["actions"]["skip-disabled"], [])

    def test_disabled_agent_is_not_resurrected_on_first_install(self) -> None:
        """First-install (NOT --update) into a folder where the user has
        ALREADY pre-disabled the agent (only the .disabled/ copy exists,
        e.g. because the project was bootstrapped from a clone of another
        project's `.claude/`). The bundle must respect this.
        """
        # Pre-populate the disabled location with a fingerprint we can
        # later check for non-mutation.
        self._agent_disabled_dest("foo").parent.mkdir(parents=True)
        self._agent_disabled_dest("foo").write_text(
            "USER_DISABLED_SENTINEL\n", encoding="utf-8",
        )

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

        self.assertEqual(result["errors"], [])
        # The enabled-side file did NOT appear.
        self.assertFalse(
            self._agent_dest("foo").exists(),
            "install-bundle must NOT resurrect a user-disabled agent",
        )
        # The .disabled/ copy is byte-identical to what we wrote.
        self.assertEqual(
            self._agent_disabled_dest("foo").read_text(encoding="utf-8"),
            "USER_DISABLED_SENTINEL\n",
            "install-bundle must NOT touch the .disabled/ copy",
        )
        # The action was classified as skip-disabled.
        self.assertIn(
            str(Path(".claude") / "agents" / "foo.md"),
            result["actions"]["skip-disabled"],
            f"expected skip-disabled action, got: {result['actions']}",
        )

    def test_disabled_agent_is_not_resurrected_on_update(self) -> None:
        """Full sequence: install -> disable (move file) -> --update.
        The --update pass must NOT recreate the enabled-side file.
        THIS IS THE KEY INVARIANT (per Subagent D plan).
        """
        # 1. First install. Lands at `.claude/agents/foo.md`.
        first = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(first["errors"], [])
        self.assertTrue(self._agent_dest("foo").exists())

        # 2. Simulate the launcher GUI's disable action: move the
        #    file out of agents/ into agents.disabled/.
        self._agent_disabled_dest("foo").parent.mkdir(parents=True)
        self._agent_dest("foo").rename(self._agent_disabled_dest("foo"))
        self.assertFalse(self._agent_dest("foo").exists())
        self.assertTrue(self._agent_disabled_dest("foo").exists())

        # Capture the disabled-side bytes so we can confirm
        # non-mutation after the --update.
        before_bytes = self._agent_disabled_dest("foo").read_bytes()

        # 3. Re-run install-bundle in update mode.
        second = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )

        self.assertEqual(second["errors"], [])
        # 4. THE INVARIANT.
        self.assertFalse(
            self._agent_dest("foo").exists(),
            "install-bundle --update resurrected a disabled agent — "
            "this is the bug Subagent D's contract prevents",
        )
        self.assertEqual(
            self._agent_disabled_dest("foo").read_bytes(),
            before_bytes,
            "the .disabled/ copy was modified by install-bundle --update",
        )
        self.assertIn(
            str(Path(".claude") / "agents" / "foo.md"),
            second["actions"]["skip-disabled"],
        )
        # Sanity: we did NOT also record this as a create / overwrite.
        self.assertNotIn(
            str(Path(".claude") / "agents" / "foo.md"),
            second["actions"]["create"],
        )
        self.assertNotIn(
            str(Path(".claude") / "agents" / "foo.md"),
            second["actions"]["overwrite"],
        )

    # ---- Skills ----

    def test_fresh_install_lands_skill_in_enabled_location(self) -> None:
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        self.assertTrue((self._skill_dest("tdd") / "SKILL.md").exists())
        self.assertEqual(result["actions"]["skip-disabled"], [])

    def test_disabled_skill_is_not_resurrected_on_update(self) -> None:
        """Same don't-resurrect invariant for skills (which are whole
        directories, not single files).
        """
        # 1. Fresh install of tdd skill.
        first = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(first["errors"], [])
        self.assertTrue((self._skill_dest("tdd") / "SKILL.md").exists())

        # 2. Simulate launcher disable: move whole skill dir.
        self._skill_disabled_dest("tdd").parent.mkdir(parents=True)
        self._skill_dest("tdd").rename(self._skill_disabled_dest("tdd"))
        self.assertFalse(self._skill_dest("tdd").exists())
        self.assertTrue(
            (self._skill_disabled_dest("tdd") / "SKILL.md").exists()
        )

        before_bytes = (
            self._skill_disabled_dest("tdd") / "SKILL.md"
        ).read_bytes()

        # 3. install-bundle --update.
        second = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )

        self.assertEqual(second["errors"], [])
        # 4. INVARIANT: skill dir not recreated.
        self.assertFalse(
            self._skill_dest("tdd").exists(),
            "install-bundle --update resurrected a disabled skill",
        )
        self.assertEqual(
            (self._skill_disabled_dest("tdd") / "SKILL.md").read_bytes(),
            before_bytes,
            "the .disabled/ skill SKILL.md was modified by --update",
        )
        # The skill's SKILL.md op was classified as skip-disabled.
        skill_md_rel = str(Path(".claude") / "skills" / "tdd" / "SKILL.md")
        self.assertIn(
            skill_md_rel,
            second["actions"]["skip-disabled"],
            f"expected skip-disabled for {skill_md_rel}, "
            f"got actions: {second['actions']}",
        )

    def test_disabled_skill_does_not_pollute_other_action_buckets(
        self,
    ) -> None:
        """A disabled skill's SKILL.md should NOT appear in any of
        create / overwrite / preserve / skip-existing — only in
        skip-disabled.
        """
        self._skill_disabled_dest("tdd").mkdir(parents=True)
        (self._skill_disabled_dest("tdd") / "SKILL.md").write_text(
            "USER_DISABLED\n", encoding="utf-8",
        )

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

        self.assertEqual(result["errors"], [])
        skill_md_rel = str(Path(".claude") / "skills" / "tdd" / "SKILL.md")
        # Pin: ONLY skip-disabled.
        for bucket in ("create", "overwrite", "preserve", "skip-existing",
                       "always-overwrite", "noop"):
            self.assertNotIn(
                skill_md_rel,
                result["actions"][bucket],
                f"disabled skill leaked into actions[{bucket!r}]",
            )
        self.assertIn(skill_md_rel, result["actions"]["skip-disabled"])

    # ---- Cross-cutting ----

    def test_actions_dict_contains_skip_disabled_key_even_when_empty(
        self,
    ) -> None:
        """Schema-stability: `actions['skip-disabled']` is always present
        (as an empty list when nothing was skipped). Callers iterating
        the full action map shouldn't crash with KeyError.
        """
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertIn("skip-disabled", result["actions"])
        self.assertIsInstance(result["actions"]["skip-disabled"], list)

    def test_disabled_agent_does_not_appear_in_manifest(self) -> None:
        """When the bundle skips a disabled agent, it does NOT claim
        ownership in `.vco-manifest.json`. (The enabled-side file
        isn't ours — we didn't write it.) This matters for orphan
        detection: a future run that no longer ships this agent
        shouldn't trigger the orphan-preserved deferral for a path
        that VCO never installed in the first place.
        """
        # Pre-disable foo.
        self._agent_disabled_dest("foo").parent.mkdir(parents=True)
        self._agent_disabled_dest("foo").write_text("x", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [])

        # Manifest written, but agent foo's enabled path NOT in it.
        manifest_path = self.proj / ".claude" / ".vco-manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files", {})
        self.assertNotIn(
            str(Path(".claude") / "agents" / "foo.md"),
            files,
            "skip-disabled must NOT record a manifest entry "
            "(the enabled file doesn't exist; we own nothing here)",
        )


if __name__ == "__main__":
    unittest.main()
