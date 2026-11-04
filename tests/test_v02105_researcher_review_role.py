# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the research lanes also review code (owner ruling 2026-10-16).

Owner, quoted: "ok to keep if good - but may just extend researcher role to
include reviewing code to avoid introducing too many agents (we had the idea
of additional files to further specialize agents/skills - can use that)".

The "additional files" mechanism is ONE home for agents and skills: a doc
under ``templates/specializations/`` (installed to ``.claude/specializations/``)
that a definition names, with its trigger, in one line. The first ROLE doc is
``roles/bounded-review.md``, read by ``deepseek-researcher`` and
``glm-flash-researcher`` for bounded code reviews and fix-delta verifications.

Pins:
* both researcher definitions name the role file and describe the review role
  (and no longer say they are not reviewers);
* every ``.claude/specializations/...`` path a shipped agent / skill / pack
  definition names resolves to a shipped doc (a dangling pointer would send
  the model to a file that never arrives);
* the REAL bundle enumeration ships the role file as a plain
  ``specializations`` copy, outside ``.claude/agents/`` and ``.claude/skills/``
  (the two trees Claude Code scans for definitions), and only definitions —
  frontmatter with a name and a description — land in ``.claude/agents/``;
* end to end: the role file installs, a plain update refreshes an untouched
  copy, and a locally edited copy is ADOPTED with a backup, like any other
  shipped file.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: E402
from vco_lib import project_init  # noqa: E402

TEMPLATES = REPO_ROOT / "templates"
ROLE_REL = "roles/bounded-review.md"
ROLE_DEST = ".claude/specializations/roles/bounded-review.md"
RESEARCHERS = ("deepseek-researcher", "glm-flash-researcher")

_FRONTMATTER = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)
# `.claude/specializations/<path>.md`, with optional `{a,b}` brace groups.
_SPEC_POINTER = re.compile(r"\.claude/specializations/([\w\-/{},]+)\.md")


def _frontmatter(path: Path) -> dict | None:
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if m is None:
        return None
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else None


def _expand_braces(rel: str) -> list[str]:
    m = re.search(r"\{([^}]*)\}", rel)
    if m is None:
        return [rel]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out += _expand_braces(rel[:m.start()] + alt + rel[m.end():])
    return out


def _shipped_definition_files() -> list[Path]:
    return sorted([
        *TEMPLATES.glob("agents/**/*.md"),
        *TEMPLATES.glob("skills/**/*.md"),
        *TEMPLATES.glob("packs/**/*.md"),
    ])


class ResearcherDefinitionTests(unittest.TestCase):
    def test_researchers_name_the_role_file_and_take_the_review_role(self):
        for name in RESEARCHERS:
            path = TEMPLATES / "agents" / "module-gateway" / f"{name}.md"
            body = path.read_text(encoding="utf-8")
            desc = str((_frontmatter(path) or {}).get("description", ""))
            with self.subTest(agent=name):
                self.assertIn(f".claude/specializations/{ROLE_REL}", body)
                self.assertIn("fix-delta verification", desc)
                self.assertIn("review", desc.lower())
                self.assertNotIn("NOT a reviewer", desc)
                self.assertNotIn("Never review a diff", body)
                self.assertNotIn("never reviews a diff", body)

    def test_role_file_carries_checklist_evidence_rules_and_report_shape(self):
        text = (TEMPLATES / "specializations" / ROLE_REL).read_text(
            encoding="utf-8")
        for heading in ("## Checklist", "## Procedure (fix-delta verification)",
                        "## Evidence rules", "## Report shape"):
            self.assertIn(heading, text)
        # A role doc is NOT a definition: no frontmatter, so nothing can
        # mistake it for an agent or a skill even if copied into one's tree.
        self.assertIsNone(_frontmatter(TEMPLATES / "specializations" / ROLE_REL))

    def test_every_specialization_pointer_resolves_to_a_shipped_doc(self):
        shipped = {
            p.relative_to(TEMPLATES / "specializations").as_posix()
            for p in (TEMPLATES / "specializations").rglob("*.md")}
        dangling: list[str] = []
        seen = 0
        for path in _shipped_definition_files():
            for raw in _SPEC_POINTER.findall(path.read_text(encoding="utf-8")):
                for rel in _expand_braces(raw):
                    seen += 1
                    if f"{rel}.md" not in shipped:
                        dangling.append(
                            f"{path.relative_to(REPO_ROOT).as_posix()} -> {rel}.md")
        # Non-vacuity: the researchers' pointer alone is two; the catalogue
        # names many more (code-reviewer, expert-coder, tester, ...).
        self.assertGreater(seen, 10)
        self.assertEqual(dangling, [])

    def test_brace_expansion_control(self):
        self.assertEqual(
            _expand_braces("review-topics/{a,b}"),
            ["review-topics/a", "review-topics/b"])


class RealBundleEnumerationTests(unittest.TestCase):
    """The REAL templates tree through the ONE enumerator the install uses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="vct-role-enum-"))
        cls.ops = project_init._enumerate_bundle_files(REPO_ROOT, cls.tmp)
        cls.by_dest = {op.dest_rel.replace("\\", "/"): op for op in cls.ops}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_role_file_ships_as_a_plain_specialization(self):
        op = self.by_dest.get(ROLE_DEST)
        self.assertIsNotNone(op, f"{ROLE_DEST} is not in the bundle op set")
        assert op is not None
        self.assertEqual(op.source_rel.replace("\\", "/"),
                         f"templates/specializations/{ROLE_REL}")
        self.assertIsNone(op.transform, "role docs are a plain copy")
        self.assertEqual(project_init._bundle_op_kind(ROLE_DEST),
                         "specializations")

    def test_no_specialization_lands_in_a_definition_tree(self):
        for dest, op in self.by_dest.items():
            if op.source_rel.replace("\\", "/").startswith(
                    "templates/specializations/"):
                self.assertTrue(dest.startswith(".claude/specializations/"),
                                dest)

    def test_only_definitions_land_in_the_agents_dir(self):
        # Claude Code loads every `.md` under `.claude/agents/` as an agent:
        # each one the bundle puts there must be a real definition.
        agent_ops = [op for d, op in self.by_dest.items()
                     if d.startswith(".claude/agents/")]
        self.assertTrue(agent_ops)
        for op in agent_ops:
            fm = _frontmatter(op.source_abs)
            with self.subTest(dest=op.dest_rel):
                self.assertIsNotNone(fm, "no frontmatter: not a definition")
                assert fm is not None
                self.assertTrue(fm.get("name") and fm.get("description"))


class RoleFileInstallUpdateAdoptTests(unittest.TestCase):
    """End to end through install_project_bundle with the REAL role bytes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-role-install-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch, with_compose_pair=False)
        self.src = self.orch / "templates" / "specializations" / ROLE_REL
        self.src.parent.mkdir(parents=True)
        shutil.copyfile(TEMPLATES / "specializations" / ROLE_REL, self.src)
        self.dest = self.proj / ROLE_DEST

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self, **kw) -> dict:
        return project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, **kw)

    def _manifest_entry(self) -> dict:
        man = json.loads((self.proj / ".claude" / ".vco-manifest.json")
                         .read_text(encoding="utf-8"))
        return man["files"][ROLE_DEST]

    def test_installs_byte_identical_and_is_recorded(self):
        result = self._install(update_mode=False)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())
        self.assertEqual(
            self._manifest_entry()["source"].replace("\\", "/"),
            f"templates/specializations/{ROLE_REL}")
        # Not a definition tree: nothing role-shaped lands in agents/skills.
        self.assertFalse(list((self.proj / ".claude").glob(
            "agents/**/bounded-review*")))

    def test_untouched_copy_refreshes_on_update(self):
        self._install(update_mode=False)
        new = self.src.read_text(encoding="utf-8") + "\nv2 line\n"
        self.src.write_text(new, encoding="utf-8")
        result = self._install(update_mode=True)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), new)
        self.assertNotIn(ROLE_DEST, [
            a.replace("\\", "/") for a in result["actions"].get("adopt", [])])

    def test_locally_edited_copy_is_adopted_with_a_backup(self):
        self._install(update_mode=False)
        local = "MY LOCAL EDIT\n"
        self.dest.write_text(local, encoding="utf-8")
        new = self.src.read_text(encoding="utf-8") + "\nv2 line\n"
        self.src.write_text(new, encoding="utf-8")
        result = self._install(update_mode=True)
        self.assertIn(ROLE_DEST, [
            a.replace("\\", "/") for a in result["actions"]["adopt"]])
        self.assertEqual(self.dest.read_text(encoding="utf-8"), new)
        backup = self.proj / result["adopt_backup_dir"] / ROLE_DEST
        self.assertEqual(backup.read_text(encoding="utf-8"), local)
        self.assertEqual(self._manifest_entry()["sha256"],
                         hashlib.sha256(new.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
