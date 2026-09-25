# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — a RENDERED file's ``.from-upstream-`` sidecar is not outstanding work.

The orchestrator-root ``CLAUDE.md`` is materialized by install.py from
``templates/ORCHESTRATOR-CLAUDE.md.template``; upstream's tracked copy is the
short placeholder saying so. The launcher's pre-pull 3-way merge used to
conflict on it every release and park ``CLAUDE.md.from-upstream-<sha>`` —
upstream's placeholder, which adopting would have replaced the rendered file
with. Nothing was ever there to adopt, yet the sidecars accumulated (the
maintainer's install had two, one per release) and each kept the
``orchestrator_user_modified_preserved`` entry alive.

Three halves, one name rule (``rendered_root_files.is_rendered_sidecar_path``):

* the launcher writes no such sidecar any more (pinned in
  ``git_user_editable_merge.rs``);
* install.py's re-render (``rendered_root_files.render_all``) reaps the ones
  older launchers parked — ONLY when git provably still holds their bytes —
  and records each removal in ``.claude/logs/auto-resolutions.jsonl``. This is
  what makes the FIRST 0.2.97 update clean up, since that pull is performed by
  a 0.2.96 launcher;
* the clear probe never lets such a sidecar keep the entry alive, a stale
  entry naming only such sidecars clears, and a GENUINE sidecar keeps the
  entry exactly as before.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_probes as dp  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib.deferral_report import DeferralEntry  # noqa: E402

CID = "orchestrator_user_modified_preserved"


def _entry(*paths: str) -> DeferralEntry:
    """Shaped like `git_user_editable_merge.rs::build_deferral_text`."""
    bullets = "\n".join(
        f"  - `{p.rsplit('.from-upstream-', 1)[0]}` — conflict; local preserved, "
        f"upstream saved as `{p}` (base=16714e0 theirs=89a5530)"
        for p in paths
    )
    return DeferralEntry(
        condition_id=CID,
        title=f"{len(paths)} orchestrator-root file preserved/merged during update",
        detected=f"VCO ran a per-path 3-way merge before `git pull`:\n{bullets}",
        why_deferred="Default-to-safety.",
        command_to_apply="\n".join(f"#   rm {p}      # POSIX" for p in paths),
        severity="info",
    )


class RenderedSidecarClassifierTests(unittest.TestCase):
    def test_classifies_from_the_shared_rendered_table(self):
        self.assertTrue(dp.is_rendered_file_sidecar("CLAUDE.md.from-upstream-89a5530"))
        # Case/separator folding follows rendered_root_files.is_rendered_path.
        self.assertTrue(dp.is_rendered_file_sidecar("claude.md.from-upstream-f1f5488"))
        self.assertFalse(dp.is_rendered_file_sidecar("README.md.from-upstream-89a5530"))
        self.assertFalse(
            dp.is_rendered_file_sidecar("knowledge/concepts/CLAUDE.md.from-upstream-89a5530")
        )
        self.assertFalse(dp.is_rendered_file_sidecar("docs/A.md.from-upstream-5a9ae53"))
        self.assertFalse(dp.is_rendered_file_sidecar("docs/CLAUDE.md.from-upstream-89a5530"))
        self.assertFalse(dp.is_rendered_file_sidecar("CLAUDE.md"))
        # The reap's exact name rule: 4..40 hex digits, nothing else.
        self.assertFalse(dp.is_rendered_file_sidecar("CLAUDE.md.from-upstream-notes"))
        self.assertFalse(dp.is_rendered_file_sidecar("CLAUDE.md.from-upstream-abc"))
        self.assertFalse(dp.is_rendered_file_sidecar("CLAUDE.md.from-upstream-" + "a" * 41))

    def test_dismiss_key_names_only_adoptable_sidecars(self):
        entry = _entry(
            "CLAUDE.md.from-upstream-89a5530", "docs/A.md.from-upstream-89a5530"
        )
        self.assertEqual(
            dp.dismiss_fields_for_sidecars(entry),
            {"preserved_sidecars": ["docs/A.md.from-upstream-89a5530"]},
        )


class RenderedSidecarProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel: str) -> None:
        p = self.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# placeholder\n", encoding="utf-8")

    def _probe(self, entry):
        return dp.orchestrator_sidecars_still_present(
            dp.ProbeContext(folder=self.folder, entry=entry)
        )

    def test_the_field_case_clears(self):
        """ACT: the maintainer's install — the entry names
        `CLAUDE.md.from-upstream-89a5530`, and it plus an older
        `CLAUDE.md.from-upstream-f1f5488` are still on disk. RED before
        v0.2.97: True forever, since neither sidecar was ever adoptable."""
        self._touch("CLAUDE.md.from-upstream-89a5530")
        self._touch("CLAUDE.md.from-upstream-f1f5488")
        entry = _entry("CLAUDE.md.from-upstream-89a5530")
        self.assertIs(self._probe(entry), False)
        # And through the registry dispatch install.py's re-probe pass uses.
        self.assertIs(dp.evaluate(self.folder, entry), False)

    def test_a_list_less_entry_ignores_a_rendered_sidecar(self):
        self._touch("CLAUDE.md.from-upstream-f1f5488")
        self.assertIs(self._probe(_entry()), False)

    def test_a_genuine_sidecar_elsewhere_still_keeps_the_entry(self):
        """LEAVE-ALONE: the rendered sidecar is not work, but a real one is —
        the sweep still finds it and the entry stays."""
        self._touch("CLAUDE.md.from-upstream-89a5530")
        self._touch("knowledge/concepts/x.md.from-upstream-4c44eb8")
        self.assertIs(self._probe(_entry("CLAUDE.md.from-upstream-89a5530")), True)

    def test_a_named_genuine_sidecar_still_keeps_the_entry(self):
        self._touch("CLAUDE.md.from-upstream-89a5530")
        self._touch("docs/A.md.from-upstream-89a5530")
        entry = _entry(
            "CLAUDE.md.from-upstream-89a5530", "docs/A.md.from-upstream-89a5530"
        )
        self.assertIs(self._probe(entry), True)

    def test_a_same_named_file_below_the_root_is_not_rendered(self):
        """Only the ROOT CLAUDE.md is rendered (the table is root-relative); a
        `docs/CLAUDE.md` is an ordinary user-editable doc whose sidecar counts."""
        self._touch("docs/CLAUDE.md.from-upstream-89a5530")
        self.assertIs(self._probe(_entry("CLAUDE.md.from-upstream-89a5530")), True)



# ---------------------------------------------------------------------------
# The reap: install.py's re-render retires stale rendered-file sidecars.
# ---------------------------------------------------------------------------

PLACEHOLDER = "# Placeholder\ninstall.py materializes this file from the template.\n"
TEMPLATE = "Rendered for {{ORCHESTRATOR_ROOT}}\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class RenderAllReapsStaleSidecarsTests(unittest.TestCase):
    """render_all removes a rendered file's stale sidecars — only provable ones."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_text(
            TEMPLATE, encoding="utf-8"
        )
        (self.root / "CLAUDE.md").write_text(PLACEHOLDER, encoding="utf-8")
        _git(self.root, "init", "-q")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
        self.placeholder_oid = _git(self.root, "rev-parse", "HEAD:CLAUDE.md")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, body: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def _trail(self) -> list[dict]:
        path = self.root / ".claude" / "logs" / "auto-resolutions.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_act_reaps_every_provable_sidecar_and_records_each(self):
        """ACT: the maintainer's two sidecars (one per release), both holding
        upstream's placeholder blob. RED before v0.2.97: both survive forever."""
        self._write("CLAUDE.md.from-upstream-89a5530", PLACEHOLDER)
        self._write("claude.md.from-upstream-F1F5488", PLACEHOLDER)

        (outcome,) = rrf.render_all(self.root)

        self.assertEqual(outcome.status, "full_rewrite")
        self.assertEqual(
            sorted(outcome.reaped_sidecars),
            ["CLAUDE.md.from-upstream-89a5530", "claude.md.from-upstream-F1F5488"],
        )
        self.assertIn("removed 2 un-adoptable upstream sidecar(s)", outcome.detail)
        self.assertFalse((self.root / "CLAUDE.md.from-upstream-89a5530").exists())
        self.assertFalse((self.root / "claude.md.from-upstream-F1F5488").exists())
        rows = self._trail()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["condition_id"], "orchestrator_user_modified_preserved")
            self.assertEqual(row["action"], "removed un-adoptable rendered-file sidecar")
            self.assertIn(f"git cat-file -p {self.placeholder_oid}", row["detail"])
        # The record names a blob that really restores the removed bytes.
        self.assertEqual(
            _git(self.root, "cat-file", "-p", self.placeholder_oid) + "\n", PLACEHOLDER
        )

    def test_leave_alone_everything_not_provably_a_stale_rendered_sidecar(self):
        kept = [
            self._write("CLAUDE.md.from-upstream-deadbee", "hand-edited, unique bytes\n"),
            self._write("CLAUDE.md.from-upstream-notes", PLACEHOLDER),  # not a sha
            self._write("docs/CLAUDE.md.from-upstream-89a5530", PLACEHOLDER),  # not rendered
            self._write("README.md.from-upstream-89a5530", PLACEHOLDER),  # ordinary file
            self._write("knowledge/concepts/x.md.from-upstream-89a5530", PLACEHOLDER),
        ]
        target = self._write("elsewhere.txt", PLACEHOLDER)
        link = self.root / "CLAUDE.md.from-upstream-abcdef1"
        try:
            os.symlink(target, link)
            kept.append(link)
        except (OSError, NotImplementedError):
            pass  # no symlink privilege (Windows) — the other cases still run

        (outcome,) = rrf.render_all(self.root)

        self.assertEqual(outcome.reaped_sidecars, ())
        self.assertNotIn("sidecar", outcome.detail)
        for path in kept:
            self.assertTrue(path.is_symlink() or path.exists(), path)
        self.assertEqual(self._trail(), [])

    def test_leave_alone_when_the_render_did_not_succeed(self):
        (self.root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").unlink()
        sidecar = self._write("CLAUDE.md.from-upstream-89a5530", PLACEHOLDER)
        (outcome,) = rrf.render_all(self.root)
        self.assertEqual(outcome.status, "template_missing")
        self.assertTrue(sidecar.exists())

    def test_leave_alone_outside_a_git_work_tree(self):
        shutil.rmtree(self.root / ".git")
        sidecar = self._write("CLAUDE.md.from-upstream-89a5530", PLACEHOLDER)
        (outcome,) = rrf.render_all(self.root)
        self.assertEqual(outcome.reaped_sidecars, ())
        self.assertTrue(sidecar.exists(), "no git ⇒ no evidence ⇒ no removal")


class CleanMergeThenRerenderTests(unittest.TestCase):
    """Is a CLEAN launcher 3-way merge of a rendered file harmful?

    `git_user_editable_merge.rs::a0_clean_merge_of_a_rendered_file_keeps_user_text_and_auto_block`
    produces exactly MERGED below: upstream changed a placeholder line the user
    kept verbatim, the user's own note and the AUTO block are intact. This is
    install.py's next step on those bytes.
    """

    MERGED = (
        "# base\nLine A2\nLine B\n\nMy own note.\n\n"
        "<!-- BEGIN: AUTO (rendered) -->\nAUTO v1 body\n<!-- END: AUTO -->\n"
    )

    def test_rerender_replaces_only_the_auto_block(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "templates").mkdir()
            (root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_text(
                "<!-- BEGIN: AUTO (rendered) -->\nAUTO v2 body\n<!-- END: AUTO -->\n",
                encoding="utf-8",
            )
            (root / "CLAUDE.md").write_text(self.MERGED, encoding="utf-8")
            (entry,) = rrf.entries()
            outcome = rrf.render_entry(root, entry)
            self.assertEqual(outcome.status, "auto_block_updated")
            self.assertEqual(
                (root / "CLAUDE.md").read_text(encoding="utf-8"),
                "# base\nLine A2\nLine B\n\nMy own note.\n\n"
                "<!-- BEGIN: AUTO (rendered) -->\nAUTO v2 body\n<!-- END: AUTO -->\n",
            )


if __name__ == "__main__":
    unittest.main()
