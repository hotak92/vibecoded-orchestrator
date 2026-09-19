# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 R1 — a file the install RENDERS must never reach a merge conflict.

The owner's ruling (2026-09-10): "those conflicts should not arise at all for
3rd party users on files that are expected to change like CLAUDE.md", and on
the remedy "ok to always be deferred in case of conflict". Evidence: every
0.2.93-to-0.2.94 updater stopped at the divergence modal on ``CLAUDE.md``,
because the install RENDERS that tracked path (so it is divergent by design)
and upstream had changed the tracked stub.

What is pinned here:

* **the table** — ``vco_lib/rendered_root_files.toml`` parses, declares
  ``CLAUDE.md``, and is the ONE home both languages read (the Rust loader
  embeds the same file, with the same format version and no second copy of the
  path list or the state-file location);
* **the renderer** — ``install._materialize_orchestrator_self_claude_md``
  ITERATES that table (so "the rendered set" is enumerable from the renderer,
  not from a hand list beside it), replaces only the AUTO block, and preserves
  everything the user wrote outside it;
* **the git-level mechanics, DRIVEN against real repositories** — the sequence
  the launcher performs before the pull (hold the working-tree bytes, take
  upstream's blob, pathspec-commit it, put the bytes back) makes the very pull
  that used to conflict exit 0 with no conflict state, and leaves the user's
  rendered copy on disk. The Rust unit tests drive the actual function; THIS
  drives the same sequence with git itself, so the claim "after this, the pull
  cannot conflict" is verified independently of the implementation language;
* **the hand-off + deferral** — install.py re-renders from the NEW template and
  writes exactly one ``rendered_file_upstream_changed`` row (class
  ``informational_record``), then consumes the state file so the row drains;
* **leave-alone** — a genuinely user-edited NON-rendered file still blocks the
  pull (that is the modal this change must NOT widen away).
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.rust_source import read_rust_code  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

MERGE_RS = REPO_ROOT / "launcher/src-tauri/src/commands/git_user_editable_merge.rs"
SELF_UPDATE_RS = REPO_ROOT / "launcher/src-tauri/src/commands/self_update.rs"
INSTALLER_RS = REPO_ROOT / "launcher/src-tauri/src/commands/installer.rs"
# v0.2.95 phase 2: the pull sequence BOTH update surfaces run, and therefore
# the one place the A0 pre-merge (and with it the rendered reconcile) is wired.
UPDATE_PIPELINE_RS = REPO_ROOT / "launcher/src-tauri/src/commands/update_pipeline.rs"

TEMPLATE_V1 = (
    "<!-- BEGIN: AUTO (rendered) -->\n"
    "AUTO body v1 — orchestrator root is {{ORCHESTRATOR_ROOT}}\n"
    "<!-- END: AUTO -->\n"
)
TEMPLATE_V2 = (
    "<!-- BEGIN: AUTO (rendered) -->\n"
    "AUTO body v2 — NEW template text, root {{ORCHESTRATOR_ROOT}}\n"
    "<!-- END: AUTO -->\n"
)

USER_PREFIX = "# My own rules\n\nKeep this line — it exists nowhere but here.\n\n"
USER_SUFFIX = "\n\n## My tail\n\nAlso only here.\n"

STUB_V1 = "# CLAUDE.md (tracked stub)\n\nRendered at install time.\n"
STUB_V2 = "# CLAUDE.md (tracked stub)\n\nRendered at install time. Reminder block removed.\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    out = _git(cwd, *args)
    assert out.returncode == 0, f"git {args} failed in {cwd}: {out.stderr}\n{out.stdout}"
    return out


class TestRenderedRootFilesTable(unittest.TestCase):
    """The table is the single home both languages read."""

    def test_table_declares_claude_md_and_a_gitignored_state_file(self) -> None:
        entries = rrf.entries()
        self.assertTrue(entries, "the table must declare at least one rendered path")
        claude = next((e for e in entries if e.path == "CLAUDE.md"), None)
        self.assertIsNotNone(claude, f"CLAUDE.md must be declared; got {entries}")
        assert claude is not None  # narrowing for type checkers
        self.assertEqual(claude.template, "templates/ORCHESTRATOR-CLAUDE.md.template")
        self.assertTrue((REPO_ROOT / claude.template).is_file(), "the template must exist")
        self.assertIn("ORCHESTRATOR_ROOT", claude.substitutions)
        self.assertTrue(
            rrf.state_file_rel_path().startswith(".claude/state/"),
            "the hand-off state file must live under the gitignored .claude/state/ — "
            "writing it must never dirty the tree the pull is about to merge",
        )

    def test_matching_folds_case_and_separators(self) -> None:
        self.assertTrue(rrf.is_rendered_path("CLAUDE.md"))
        self.assertTrue(rrf.is_rendered_path("claude.md"))  # HFS+/NTFS case folding
        self.assertFalse(rrf.is_rendered_path("README.md"))
        self.assertFalse(rrf.is_rendered_path("knowledge/concepts/foo.md"))

    def test_malformed_tables_fail_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            rrf._parse("format_version = 99\nstate_file='x'\n[[rendered]]\npath='a'\n"
                       "template='t'\nbegin_marker='b'\nend_marker='e'\n", source="t")
        with self.assertRaises(RuntimeError):
            rrf._parse("format_version = 1\nstate_file=''\n", source="t")
        dup = (
            "format_version = 1\nstate_file='.claude/state/x.json'\n"
            "[[rendered]]\npath='A.md'\ntemplate='t'\nbegin_marker='b'\nend_marker='e'\n"
            "[[rendered]]\npath='a.md'\ntemplate='t'\nbegin_marker='b'\nend_marker='e'\n"
        )
        with self.assertRaises(RuntimeError):
            rrf._parse(dup, source="t")


class TestCrossLanguageLockstep(unittest.TestCase):
    """Tier-(B) shared config: two parsers, ONE table, no second copy.

    Every scan below reads Rust through ``tests.common.rust_source`` — the ONE
    comment-blanking home — rather than raw ``read_text``. v0.2.95 ship-gate
    MINOR-7: these pins were comment-BLIND, so a name appearing only in a
    comment satisfied them, and one of them carried a third private stripper
    (``line.split("//", 1)[0]``, which cuts inside a ``"…//…"`` string literal).
    String literals are kept verbatim by that home, which matters here: the
    ``include_str!`` path and the ``const`` line being pinned ARE string/code
    text, not prose.
    """

    def test_rust_embeds_this_exact_table_at_the_same_version(self) -> None:
        src = read_rust_code(MERGE_RS)
        self.assertIn(
            'include_str!("../../../../vco_lib/rendered_root_files.toml")',
            src,
            "the Rust loader must embed THIS table (4 levels up = repo root)",
        )
        include_target = (MERGE_RS.parent / "../../../../vco_lib/rendered_root_files.toml").resolve()
        self.assertEqual(include_target, rrf.TABLE_PATH.resolve())
        self.assertIn(
            f"const RENDERED_TABLE_FORMAT_VERSION: u32 = {rrf.SUPPORTED_FORMAT_VERSION};",
            src,
            "both loaders must accept the same format version",
        )

    def test_rust_keeps_no_second_copy_of_the_path_list_or_state_path(self) -> None:
        src = read_rust_code(MERGE_RS)
        state = rrf.state_file_rel_path()
        self.assertNotIn(
            f'"{state}"',
            src,
            "the state-file path must come from the table, never a literal in Rust",
        )
        # The rendered path set is read from the table; the only literal
        # "CLAUDE.md" occurrences allowed in the reconcile's own region are in
        # comments and tests, never a classification list.
        reconcile_start = src.index("pub(crate) async fn resolve_rendered_files_keep_local_at")
        reconcile_end = src.index("fn write_rendered_reconcile_state")
        # `src` is already comment-free (read through the one home), so the
        # third private stripper that used to stand here — `line.split("//",
        # 1)[0]`, which also cuts inside a `"…//…"` string literal — is gone.
        body = src[reconcile_start:reconcile_end]
        self.assertNotIn(
            "CLAUDE.md",
            body,
            "the reconcile must classify from the table, not from a hard-coded path",
        )

    def test_both_update_surfaces_reach_the_same_reconcile(self) -> None:
        """Both update surfaces must reach the rendered reconcile.

        v0.2.95 phase 2 — the property is unchanged; the WAY the launcher
        self-update surface satisfies it is not. It used to have no A0 step and
        so called `resolve_rendered_files_keep_local` DIRECTLY; it now pulls
        through the shared `update_pipeline`, which runs the A0 pre-merge for
        both surfaces. So there is ONE entry point where there were two, and
        asserting the old direct call would now report that consolidation as a
        regression.

        What is asserted instead is the property itself, per surface:
        `pre_merge_user_editable` runs the reconcile, and BOTH surfaces reach
        `pre_merge_user_editable` — the launcher one via the pipeline's A0 step,
        which is the single call the pipeline makes on behalf of both.

        The behavioural half of this lives in
        `update_pipeline::tests::pull_sequence_reaches_the_rendered_reconcile_
        through_the_a0_step`: it drives the real pull sequence over a clone in
        the state every install is in and asserts the OBSERVABLE consequence
        (the pull lands, the rendered bytes survive, HEAD carries upstream's
        blob). That is the guard that cannot be satisfied by a name in a
        comment; this one pins the wiring either surface could quietly lose.
        """
        merge_src = read_rust_code(MERGE_RS)
        pipeline_src = read_rust_code(UPDATE_PIPELINE_RS)
        self_update_src = read_rust_code(SELF_UPDATE_RS)

        # The A0 pre-merge runs the rendered reconcile FIRST.
        self.assertIn(
            "resolve_rendered_files_keep_local_at(",
            merge_src.split("pub(crate) async fn pre_merge_user_editable")[1],
            "the A0 pre-merge must run the rendered reconcile first",
        )
        # The shared pipeline runs the A0 pre-merge …
        self.assertIn(
            "run_pre_merge_user_editable(",
            pipeline_src,
            "the shared update pipeline must run the A0 pre-merge",
        )
        # … and BOTH surfaces pull through that pipeline, which is how they
        # reach it. `installer::update_orchestrator` and
        # `self_update::apply_launcher_update` each call it exactly once.
        for name, src in (
            ("installer.rs", read_rust_code(INSTALLER_RS)),
            ("self_update.rs", self_update_src),
        ):
            self.assertIn(
                "prepare_and_pull_orchestrator_repo(",
                src,
                f"{name}'s update surface must pull through the shared pipeline — "
                "that is how it reaches the rendered reconcile",
            )
        # And the launcher surface must not keep a second, divergent entry
        # point into the same class.
        self.assertNotIn(
            "git_user_editable_merge::resolve_rendered_files_keep_local(",
            self_update_src,
            "one entry point: the direct call was folded into the pipeline's A0 step",
        )


class TestDeferralRegistryEntry(unittest.TestCase):
    def test_condition_is_declared_as_an_informational_record_install_owns(self) -> None:
        registry = tomllib.loads(
            (REPO_ROOT / "vco_lib" / "deferral_conditions.toml").read_text(encoding="utf-8")
        )
        entry = registry["conditions"]["rendered_file_upstream_changed"]
        self.assertEqual(entry["class"], "informational_record")
        self.assertEqual(entry["owner"], "install.py")
        self.assertEqual(entry["clear_probe"], "owned-drop-when-absent")
        from vco_lib import deferral_registry

        self.assertIn(
            "rendered_file_upstream_changed",
            deferral_registry.install_owned_ids(),
            "install.py must OWN it, or its row would never drain",
        )


class _RendererCase(unittest.TestCase):
    """Shared scaffolding for the renderer/deferral tests."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="vco-r1-")
        self.root = Path(self._tmp.name)
        (self.root / "templates").mkdir(parents=True)
        self.template = self.root / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
        self.template.write_text(TEMPLATE_V1, encoding="utf-8")
        self.install = importlib.import_module("install")
        self.addCleanup(self._tmp.cleanup)

    def render(self, report: DeferralReport | None = None) -> str:
        with mock.patch.object(
            self.install, "_log_install_event", lambda *a, **k: None
        ), contextlib.redirect_stdout(io.StringIO()):
            self.install._materialize_orchestrator_self_claude_md(
                self.root, deferral_report=report
            )
        target = self.root / "CLAUDE.md"
        return target.read_text(encoding="utf-8") if target.is_file() else ""

    def rendered_body(self, auto: str) -> str:
        """A previously-rendered file: user text, the AUTO block, user text."""
        return (
            USER_PREFIX
            + "<!-- BEGIN: AUTO (rendered) -->\n"
            + auto
            + "<!-- END: AUTO -->"
            + USER_SUFFIX
        )

    def write_state_file(self, files: list[str], base: str, theirs: str, branch: str) -> Path:
        state = self.root / rrf.state_file_rel_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reconciled_at": "2026-09-16T10:00:00Z",
                    "branch": branch,
                    "base": base,
                    "theirs": theirs,
                    "files": [
                        {"path": f, "template": "templates/ORCHESTRATOR-CLAUDE.md.template"}
                        for f in files
                    ],
                }
            ),
            encoding="utf-8",
        )
        return state


class TestRendererIsTableDriven(_RendererCase):
    def test_fresh_install_creates_the_file_from_the_template(self) -> None:
        body = self.render()
        self.assertIn("AUTO body v1", body)
        self.assertIn(str(self.root), body, "{{ORCHESTRATOR_ROOT}} must be substituted")
        self.assertNotIn("{{", body, "no placeholder may survive into the written file")

    def test_update_replaces_only_the_auto_block(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            self.rendered_body("AUTO body v1 — orchestrator root is /old/root\n"),
            encoding="utf-8",
        )
        self.template.write_text(TEMPLATE_V2, encoding="utf-8")
        body = self.render()
        self.assertIn("AUTO body v2", body, "the NEW template must land")
        self.assertNotIn("AUTO body v1", body)
        self.assertIn("Keep this line", body, "user text ABOVE the markers must survive")
        self.assertIn("Also only here", body, "user text BELOW the markers must survive")

    def test_a_template_the_renderer_cannot_substitute_writes_nothing(self) -> None:
        # A table entry naming an unknown placeholder must fail that entry
        # loudly rather than write a file with a literal {{NAME}} in it.
        bogus = rrf.RenderedRootFile(
            path="CLAUDE.md",
            template="templates/ORCHESTRATOR-CLAUDE.md.template",
            begin_marker="<!-- BEGIN: AUTO",
            end_marker="<!-- END: AUTO -->",
            substitutions=("NO_SUCH_PLACEHOLDER",),
        )
        outcome = rrf.render_entry(self.root, bogus)
        self.assertEqual(outcome.status, "unknown_substitution")
        self.assertIn("NO_SUCH_PLACEHOLDER", outcome.detail)
        self.assertFalse((self.root / "CLAUDE.md").exists(), "nothing may be written")


class TestDeferralEmission(_RendererCase):
    def test_one_informational_row_names_the_file_and_the_upstream_range(self) -> None:
        state = self.write_state_file(["CLAUDE.md"], "aaaaaaaaaaaa1", "bbbbbbbbbbbb2", "main")
        report = DeferralReport()
        self.render(report)
        rows = [e for e in report.entries if e.condition_id == "rendered_file_upstream_changed"]
        self.assertEqual(len(rows), 1, f"expected exactly one row, got {report.entries}")
        row = rows[0]
        self.assertEqual(row.severity, "info")
        self.assertIn("CLAUDE.md", row.detected)
        self.assertIn("aaaaaaaaaaaa", row.detected)
        self.assertIn("bbbbbbbbbbbb", row.detected)
        self.assertIn("main", row.detected)
        self.assertFalse(
            state.exists(),
            "the state file is consumed, so the next --update does not re-detect the "
            "condition and the owned row drains",
        )

    def test_no_reconcile_means_no_row(self) -> None:
        report = DeferralReport()
        self.render(report)
        self.assertEqual(
            [e for e in report.entries if e.condition_id == "rendered_file_upstream_changed"],
            [],
        )

    def test_without_a_report_the_state_file_is_kept_for_the_next_run(self) -> None:
        state = self.write_state_file(["CLAUDE.md"], "a" * 12, "b" * 12, "main")
        self.render(None)
        self.assertTrue(
            state.exists(),
            "a caller that cannot record the row must not discard the evidence",
        )


class TestDrivenGitSequence(unittest.TestCase):
    """The load-bearing claim, driven against real repositories.

    Builds an upstream + a clone, renders CLAUDE.md the way an install does
    (uncommitted, tracked, divergent by design), changes the tracked stub
    upstream — twice, so the clone is several releases behind (R26: the trigger
    is STATE, not "since the last release") — then performs the launcher's
    pre-pull sequence with git itself and asserts the pull that used to stop at
    the divergence modal now lands cleanly with the user's content intact.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="vco-r1-git-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.clone = root / "clone"
        _git_ok(root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.seed.mkdir()
        _git_ok(self.seed, "init", "--initial-branch=main")
        _git_ok(self.seed, "config", "user.email", "test@example.invalid")
        _git_ok(self.seed, "config", "user.name", "Test")
        (self.seed / "templates").mkdir()
        (self.seed / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_text(
            TEMPLATE_V1, encoding="utf-8"
        )
        (self.seed / "CLAUDE.md").write_text(STUB_V1, encoding="utf-8")
        (self.seed / "README.md").write_text("# readme\nline A\nline B\n", encoding="utf-8")
        _git_ok(self.seed, "add", ".")
        _git_ok(self.seed, "commit", "-m", "seed")
        _git_ok(self.seed, "remote", "add", "origin", str(self.remote))
        _git_ok(self.seed, "push", "origin", "main")
        _git_ok(root, "clone", str(self.remote), str(self.clone))
        _git_ok(self.clone, "config", "user.email", "test@example.invalid")
        _git_ok(self.clone, "config", "user.name", "Test")
        _git_ok(self.clone, "remote", "add", "vco_upstream", str(self.remote))
        _git_ok(self.clone, "fetch", "vco_upstream")
        self.install = importlib.import_module("install")

    # -- helpers ---------------------------------------------------------

    def _upstream_commit(self, rel: str, body: str) -> None:
        (self.seed / rel).write_text(body, encoding="utf-8")
        _git_ok(self.seed, "add", rel)
        _git_ok(self.seed, "commit", "-m", f"upstream: {rel}")
        _git_ok(self.seed, "push", "origin", "main")
        _git_ok(self.clone, "fetch", "vco_upstream")

    def _render_install(self, report: DeferralReport | None = None) -> str:
        with mock.patch.object(
            self.install, "_log_install_event", lambda *a, **k: None
        ), contextlib.redirect_stdout(io.StringIO()):
            self.install._materialize_orchestrator_self_claude_md(
                self.clone, deferral_report=report
            )
        return (self.clone / "CLAUDE.md").read_text(encoding="utf-8")

    def _reconcile(self, path: str) -> tuple[str, str]:
        """The launcher's pre-pull sequence, performed with git itself."""
        base = _git_ok(self.clone, "merge-base", "HEAD", "vco_upstream/main").stdout.strip()
        theirs = _git_ok(self.clone, "rev-parse", "vco_upstream/main").stdout.strip()
        held = (self.clone / path).read_bytes()
        _git_ok(self.clone, "checkout", theirs, "--", path)
        _git_ok(
            self.clone,
            "-c", "user.name=VCO Orchestrator",
            "-c", "user.email=orchestrator@vibecoded.tools",
            "commit", "--no-verify",
            "-m", "vco: take upstream's tracked copy of rendered file(s)",
            "--", path,
        )
        (self.clone / path).write_bytes(held)
        state = self.clone / rrf.state_file_rel_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reconciled_at": "2026-09-16T10:00:00Z",
                    "branch": "main",
                    "base": base,
                    "theirs": theirs,
                    "files": [{"path": path, "template": "templates/ORCHESTRATOR-CLAUDE.md.template"}],
                }
            ),
            encoding="utf-8",
        )
        return base, theirs

    def _unmerged(self) -> list[str]:
        out = _git_ok(self.clone, "status", "--porcelain")
        return [
            line
            for line in out.stdout.splitlines()
            if line[:2] in {"UU", "AA", "DU", "UD", "AU", "UA", "DD"}
        ]

    # -- the tests -------------------------------------------------------

    def test_rendered_file_survives_the_pull_and_is_re_rendered_with_one_row(self) -> None:
        # 1. the install renders CLAUDE.md over the tracked stub (uncommitted).
        first = self._render_install()
        (self.clone / "CLAUDE.md").write_text(
            USER_PREFIX + first.rstrip("\n") + USER_SUFFIX, encoding="utf-8"
        )
        rendered_local = (self.clone / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("AUTO body v1", rendered_local)

        # 2. upstream changes the tracked stub TWICE and ships a new template
        #    (the clone is now several releases behind).
        self._upstream_commit("CLAUDE.md", STUB_V1 + "interim release\n")
        self._upstream_commit("templates/ORCHESTRATOR-CLAUDE.md.template", TEMPLATE_V2)
        self._upstream_commit("CLAUDE.md", STUB_V2)

        # Sanity: WITHOUT the reconcile this is precisely the modal case.
        refuse = _git(self.clone, "pull", "--ff-only", "vco_upstream", "main")
        self.assertNotEqual(refuse.returncode, 0, "the unreconciled pull must refuse")
        self.assertIn("would be overwritten by merge", refuse.stderr + refuse.stdout)

        # 3. the launcher's pre-pull reconcile.
        base, theirs = self._reconcile("CLAUDE.md")
        self.assertEqual(
            (self.clone / "CLAUDE.md").read_text(encoding="utf-8"),
            rendered_local,
            "the reconcile must leave the rendered working-tree copy byte-identical",
        )
        self.assertEqual(
            _git_ok(self.clone, "show", "HEAD:CLAUDE.md").stdout,
            STUB_V2,
            "and HEAD must now carry upstream's tracked copy",
        )

        # 4. the pull the update runs next (the RealMerge arm).
        pull = _git(
            self.clone,
            "pull", "--no-rebase", "--no-edit", "--autostash", "vco_upstream", "main",
        )
        self.assertEqual(
            pull.returncode, 0, f"the pull must exit 0: {pull.stdout}\n{pull.stderr}"
        )
        self.assertEqual(self._unmerged(), [], "no conflict state may be left behind")
        self.assertFalse((self.clone / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(
            (self.clone / "CLAUDE.md").read_text(encoding="utf-8"),
            rendered_local,
            "the merge must not have replaced the user's rendered copy",
        )

        # 5. install.py re-renders from the NEW template and records the row.
        report = DeferralReport()
        final = self._render_install(report)
        self.assertIn("AUTO body v2", final, "the NEW template's AUTO block must land")
        self.assertNotIn("AUTO body v1", final)
        self.assertIn("Keep this line", final, "user text above the markers survives")
        self.assertIn("Also only here", final, "user text below the markers survives")

        rows = [e for e in report.entries if e.condition_id == "rendered_file_upstream_changed"]
        self.assertEqual(len(rows), 1, "exactly one informational row")
        self.assertIn("CLAUDE.md", rows[0].detected)
        self.assertIn(base[:12], rows[0].detected)
        self.assertIn(theirs[:12], rows[0].detected)
        self.assertFalse((self.clone / rrf.state_file_rel_path()).exists())

    def test_a_non_rendered_user_edit_still_blocks_the_pull(self) -> None:
        # Leave-alone: the mechanism is scoped to RENDERED paths. An ordinary
        # user-edited file that upstream also changed still refuses the pull —
        # that is the divergence the modal exists for, and the A0 3-way /
        # sidecar path (pinned in the Rust tests) owns it.
        (self.clone / "README.md").write_text("# readme\nLOCAL line A\nline B\n", encoding="utf-8")
        self._upstream_commit("README.md", "# readme\nUPSTREAM line A\nline B\n")
        refuse = _git(self.clone, "pull", "--ff-only", "vco_upstream", "main")
        self.assertNotEqual(refuse.returncode, 0)
        self.assertIn("would be overwritten by merge", refuse.stderr + refuse.stdout)
        self.assertFalse(
            rrf.is_rendered_path("README.md"),
            "README.md must not be in the rendered table — it is user-authored, "
            "not derived from a template",
        )


if __name__ == "__main__":
    unittest.main()
