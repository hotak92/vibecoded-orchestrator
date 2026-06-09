# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the re-install conflict resolution helpers in install.py.

Mirrors the Rust tests in
  launcher/src-tauri/src/commands/installer.rs
(`test_strategy_*`, `test_replace_or_append_block_*`, etc.).

Covers:
  * `_new_sibling_path` — extension splitting (.md, .env, archive.tar.gz).
  * `_replace_or_append_block` — append-when-missing + replace-in-place.
  * `update_merge_notification_block` — file creation + idempotency.
  * `apply_conflict_strategy` — all 4 strategies + report shape.
  * Defense-in-depth: rejects non-orchestrator source.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


def _fake_repo_source(root: Path) -> Path:
    """Build a fake orchestrator source tree (mirrors `fake_repo_source`
    in the Rust tests).

    PR-31 note (v0.2.12): the source tree ships a root ``CLAUDE.md``
    (the orchestrator-self's own development documentation), but
    ``CLAUDE.md`` is NO LONGER in the install whitelist, so
    ``apply_conflict_strategy`` must NOT copy it into the target. The
    file is included here precisely so the tests can assert it stays
    behind — the same way ``README.md`` and ``scripts/foo.sh`` test
    the broader "not in allowlist → not copied" contract.

    V52-C note (v0.2.52): ``knowledge/`` was REMOVED from the install
    whitelist. The source still has a ``knowledge/`` directory (it's a
    legitimate user-state location), but ``apply_conflict_strategy``
    must NOT copy it into the target. Shipped KG nodes are now
    bundle-materialized from ``templates/knowledge/`` via
    ``_enumerate_bundle_files``; the manifest-tracked path preserves
    user customizations on update (V47-A pattern). This fixture
    keeps the ``knowledge/`` dir so the tests can assert it stays
    behind (mirrors the CLAUDE.md negative-assertion pattern).
    """
    p = root / "src"
    p.mkdir(parents=True, exist_ok=True)
    (p / "vct-module.json").write_text("{}")
    (p / ".claude").mkdir()
    (p / ".claude" / "settings.json").write_text("{}")
    # `docs/` IS in the whitelist — fixture for the positive copy test.
    (p / "docs").mkdir()
    (p / "docs" / "note.md").write_text("hello")
    # Files NOT in the allowlist — must NOT be copied. CLAUDE.md is in
    # this group as of PR-31 / v0.2.12. `knowledge/` joined the group
    # in V52-C / v0.2.52 (see the module docstring).
    (p / "CLAUDE.md").write_text("# orchestrator-self CLAUDE.md\n")
    (p / "README.md").write_text("readme")
    (p / "scripts").mkdir()
    (p / "scripts" / "foo.sh").write_text("echo hi")
    (p / "knowledge").mkdir()
    (p / "knowledge" / "note.md").write_text("source-side-should-not-copy")
    return p


def _fake_adopt_target(root: Path) -> Path:
    """Build a fake adopt-target install root with user-edited copies of
    every preserve-list file plus content outside the allowlist."""
    p = root / "tgt"
    p.mkdir(parents=True, exist_ok=True)
    (p / ".claude").mkdir()
    (p / ".claude" / "CONTEXT_STATE.md").write_text(
        "# user CONTEXT_STATE\nsome custom session state\n"
    )
    (p / ".claude" / "PROJECT_REGISTRY.md").write_text(
        "# user registry\nproject-foo\n"
    )
    (p / ".claude" / "settings.json").write_text('{"old":true}')
    (p / "CLAUDE.md").write_text("# user CLAUDE.md\ncustom rules\n")
    (p / ".env").write_text("USER_KEY=secret\n")
    (p / "user_code.py").write_text("print('survive')\n")
    (p / "knowledge").mkdir()
    (p / "knowledge" / "note.md").write_text("OLD\n")
    return p


class NewSiblingPathTests(unittest.TestCase):
    def test_basic_extension(self):
        self.assertEqual(
            install._new_sibling_path(Path("/x/CLAUDE.md")),
            Path("/x/CLAUDE.new.md"),
        )

    def test_dotfile_no_extension(self):
        # .env has no "real" extension — .new is appended at end.
        self.assertEqual(
            install._new_sibling_path(Path("/x/.env")),
            Path("/x/.env.new"),
        )

    def test_no_extension(self):
        self.assertEqual(
            install._new_sibling_path(Path("/x/Makefile")),
            Path("/x/Makefile.new"),
        )

    def test_double_extension_keeps_last(self):
        # split on LAST dot → archive.tar.new.gz
        self.assertEqual(
            install._new_sibling_path(Path("/x/archive.tar.gz")),
            Path("/x/archive.tar.new.gz"),
        )


class ReplaceOrAppendBlockTests(unittest.TestCase):
    def test_appends_when_missing(self):
        existing = "# header\nbody\n"
        block = (
            "<!-- vct-merge-pending -->\nstuff\n<!-- /vct-merge-pending -->\n"
        )
        out = install._replace_or_append_block(existing, block)
        self.assertTrue(out.startswith("# header\nbody\n"))
        self.assertIn("<!-- vct-merge-pending -->", out)

    def test_replaces_in_place(self):
        existing = (
            "# header\n"
            "<!-- vct-merge-pending -->\nOLD STUFF\n<!-- /vct-merge-pending -->\n"
            "# tail\n"
        )
        new_block = (
            "<!-- vct-merge-pending -->\nNEW STUFF\n<!-- /vct-merge-pending -->"
        )
        out = install._replace_or_append_block(existing, new_block)
        self.assertIn("NEW STUFF", out)
        self.assertNotIn("OLD STUFF", out)
        self.assertIn("# header", out)
        self.assertIn("# tail", out)
        # Exactly one block.
        self.assertEqual(out.count(install.MERGE_BLOCK_START), 1)
        self.assertEqual(out.count(install.MERGE_BLOCK_END), 1)


class UpdateMergeNotificationBlockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vct-merge-block-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_creates_file_if_missing(self):
        target = self.dir / ".claude" / "CONTEXT_STATE.md"
        written = install.update_merge_notification_block(target, ["CLAUDE.md"])
        self.assertTrue(written)
        self.assertTrue(target.exists())
        content = target.read_text()
        self.assertIn(install.MERGE_BLOCK_START, content)
        self.assertIn("CLAUDE.md", content)

    def test_appends_to_existing_file(self):
        target = self.dir / "CONTEXT_STATE.md"
        target.write_text("# user content\nimportant stuff\n")
        install.update_merge_notification_block(target, ["CLAUDE.md"])
        content = target.read_text()
        self.assertIn("# user content", content)
        self.assertIn(install.MERGE_BLOCK_START, content)

    def test_idempotent(self):
        # Two consecutive calls must not duplicate the block.
        target = self.dir / "CONTEXT_STATE.md"
        target.write_text("# user content\n")
        install.update_merge_notification_block(target, ["CLAUDE.md"])
        install.update_merge_notification_block(target, ["CLAUDE.md"])
        content = target.read_text()
        self.assertEqual(content.count(install.MERGE_BLOCK_START), 1)
        self.assertEqual(content.count(install.MERGE_BLOCK_END), 1)


class ApplyConflictStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-conflict-"))
        self.source = _fake_repo_source(self.tmp)
        self.target = _fake_adopt_target(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_adopt_as_is_is_noop_on_disk(self):
        pre = (self.target / "CLAUDE.md").read_text()
        report = install.apply_conflict_strategy(
            self.source, self.target, "adopt_as_is", []
        )
        self.assertEqual(report["copied_count"], 0)
        self.assertEqual(report["preserved_count"], 0)
        self.assertEqual(report["new_md_count"], 0)
        self.assertFalse(report["notification_written"])
        self.assertEqual((self.target / "CLAUDE.md").read_text(), pre)
        # User code OUTSIDE allowlist preserved.
        self.assertEqual(
            (self.target / "user_code.py").read_text(), "print('survive')\n"
        )

    def test_overwrite_all_loses_user_edits_in_managed_paths(self):
        report = install.apply_conflict_strategy(
            self.source, self.target, "overwrite_all", []
        )
        self.assertGreater(report["copied_count"], 0)
        self.assertEqual(report["preserved_count"], 0)
        self.assertEqual(report["new_md_count"], 0)

        # settings.json overwritten with upstream {}.
        self.assertEqual(
            (self.target / ".claude" / "settings.json").read_text(), "{}"
        )
        # CLAUDE.md is NOT in the install whitelist as of PR-31 / v0.2.12
        # (see the doc-comment on ORCHESTRATOR_MANAGED_PATHS), so even
        # under overwrite_all the user's CLAUDE.md must stay untouched
        # — the orchestrator-self's root CLAUDE.md is not copied into
        # user projects anymore.
        self.assertEqual(
            (self.target / "CLAUDE.md").read_text(),
            "# user CLAUDE.md\ncustom rules\n",
        )
        # No `.new.md` siblings (overwrite_all never writes them; and
        # CLAUDE.md is no longer managed, so even overwrite_preserve
        # wouldn't write CLAUDE.new.md anymore).
        self.assertFalse((self.target / "CLAUDE.new.md").exists())
        # User code OUTSIDE allowlist preserved.
        self.assertEqual(
            (self.target / "user_code.py").read_text(), "print('survive')\n"
        )

    def test_overwrite_preserve_writes_new_md_siblings_and_notification(self):
        # Inject a managed-path conflict the preserve machinery WILL
        # exercise: put an upstream-shipped .claude/settings.json in
        # the preserve list so the apply path writes a .new.json
        # sibling. (CLAUDE.md is no longer in the whitelist as of
        # PR-31, so it cannot drive this scenario anymore.)
        preserve = list(install.DEFAULT_PRESERVE_LIST) + [".claude/settings.json"]
        report = install.apply_conflict_strategy(
            self.source, self.target, "overwrite_preserve", preserve
        )
        # settings.json is in fake source AND target AND preserve list →
        # .new.json sibling written, user file untouched.
        self.assertTrue((self.target / ".claude" / "settings.new.json").exists())
        self.assertEqual(
            (self.target / ".claude" / "settings.json").read_text(),
            '{"old":true}',
        )
        # CLAUDE.md path: as of PR-31 (v0.2.12), CLAUDE.md is NOT in
        # the install whitelist. apply_conflict_strategy iterates the
        # whitelist, so CLAUDE.md is never visited — no copy, no
        # .new.md sibling, user file stays exactly as written.
        self.assertEqual(
            (self.target / "CLAUDE.md").read_text(),
            "# user CLAUDE.md\ncustom rules\n",
        )
        self.assertFalse((self.target / "CLAUDE.new.md").exists())
        # CONTEXT_STATE.md is in preserve list but fake source doesn't ship
        # one — user file left intact AND notification appended to it.
        ctx_text = (self.target / ".claude" / "CONTEXT_STATE.md").read_text()
        self.assertIn("# user CONTEXT_STATE", ctx_text)
        self.assertIn(install.MERGE_BLOCK_START, ctx_text)
        self.assertIn(install.MERGE_BLOCK_END, ctx_text)
        # The notification block lists the preserved-with-new-sibling
        # files. settings.json is the one driving the .new.json sibling
        # in this test.
        self.assertIn("settings.json", ctx_text)
        # V52-C (v0.2.52): `knowledge/` is OUT of the install whitelist,
        # so apply_conflict_strategy never visits it. The user's
        # `knowledge/note.md` survives every strategy because nothing
        # tries to copy onto it. (Same shape as the CLAUDE.md assertion
        # above — out-of-allowlist files survive unconditionally.)
        # Shipped KG nodes reach the user project via
        # `_enumerate_bundle_files`'s `templates/knowledge/` walk
        # instead, which is the V47-A manifest-tracked path.
        self.assertEqual((self.target / "knowledge" / "note.md").read_text(), "OLD\n")
        self.assertTrue(report["notification_written"])
        self.assertGreaterEqual(report["new_md_count"], 1)
        self.assertEqual(report["preserved_count"], report["new_md_count"])

    def test_overwrite_preserve_notification_block_idempotent(self):
        # Two consecutive runs must not duplicate the block.
        preserve = list(install.DEFAULT_PRESERVE_LIST)
        install.apply_conflict_strategy(
            self.source, self.target, "overwrite_preserve", preserve
        )
        install.apply_conflict_strategy(
            self.source, self.target, "overwrite_preserve", preserve
        )
        ctx = (self.target / ".claude" / "CONTEXT_STATE.md").read_text()
        self.assertEqual(ctx.count(install.MERGE_BLOCK_START), 1)
        self.assertEqual(ctx.count(install.MERGE_BLOCK_END), 1)

    def test_delete_claude_wipes_only_dot_claude(self):
        # Pre-condition: target has user content INSIDE .claude AND outside.
        report = install.apply_conflict_strategy(
            self.source, self.target, "delete_claude_and_reinstall", []
        )
        self.assertGreater(report["copied_count"], 0)
        # .claude wiped + repopulated with upstream.
        self.assertEqual(
            (self.target / ".claude" / "settings.json").read_text(), "{}"
        )
        # User code OUTSIDE allowlist NOT wiped.
        self.assertEqual(
            (self.target / "user_code.py").read_text(), "print('survive')\n"
        )

    def test_delete_claude_handles_missing_dot_claude(self):
        # If .claude doesn't exist (somehow we got dispatched with no
        # adopt-target), DeleteClaude must not error.
        # Build a fresh target with no .claude/.
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "user_code.py").write_text("x")
        report = install.apply_conflict_strategy(
            self.source, empty, "delete_claude_and_reinstall", []
        )
        self.assertGreater(report["copied_count"], 0)

    def test_overwrite_preserve_with_custom_preserve_list(self):
        # Custom preserve list: a single managed path
        # (.claude/settings.json) drives the preserve+new-sibling
        # machinery. We don't use CLAUDE.md here because PR-31
        # (v0.2.12) removed it from the install whitelist —
        # apply_conflict_strategy iterates the whitelist, so an entry
        # not in it never triggers a preserve action.
        report = install.apply_conflict_strategy(
            self.source,
            self.target,
            "overwrite_preserve",
            [".claude/settings.json"],
        )
        self.assertTrue((self.target / ".claude" / "settings.new.json").exists())
        self.assertEqual(
            (self.target / ".claude" / "settings.json").read_text(),
            '{"old":true}',
        )
        # report counts only the one preserved file.
        self.assertEqual(report["preserved_count"], 1)
        # CLAUDE.md untouched on both sides (not in whitelist any more).
        self.assertEqual(
            (self.target / "CLAUDE.md").read_text(),
            "# user CLAUDE.md\ncustom rules\n",
        )
        self.assertFalse((self.target / "CLAUDE.new.md").exists())

    def test_rejects_non_orchestrator_source(self):
        bad_source = self.tmp / "no-vct-module"
        bad_source.mkdir()
        with self.assertRaises(ValueError) as cm:
            install.apply_conflict_strategy(
                bad_source, self.target, "overwrite_all", []
            )
        self.assertIn("not an orchestrator repo", str(cm.exception))

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            install.apply_conflict_strategy(
                self.source, self.target, "no_such_strategy", []
            )


class CliFlagParsingTests(unittest.TestCase):
    def test_normalize_conflict_strategy_kebab_to_snake(self):
        self.assertEqual(
            install._normalize_conflict_strategy("delete-claude"),
            "delete_claude_and_reinstall",
        )
        self.assertEqual(
            install._normalize_conflict_strategy("overwrite-all"), "overwrite_all"
        )
        self.assertEqual(
            install._normalize_conflict_strategy("overwrite-preserve"),
            "overwrite_preserve",
        )
        self.assertEqual(
            install._normalize_conflict_strategy("adopt-as-is"), "adopt_as_is"
        )

    def test_parse_preserve_paths_default(self):
        self.assertEqual(
            install._parse_preserve_paths(None),
            list(install.DEFAULT_PRESERVE_LIST),
        )
        self.assertEqual(
            install._parse_preserve_paths(""),
            list(install.DEFAULT_PRESERVE_LIST),
        )

    def test_parse_preserve_paths_csv(self):
        self.assertEqual(
            install._parse_preserve_paths("a, b , c"),
            ["a", "b", "c"],
        )

    def test_default_preserve_list_matches_documented_set(self):
        # Lockstep check with Rust DEFAULT_PRESERVE_LIST and Svelte
        # DEFAULT_PRESERVE_LIST. Update all three in lockstep when the
        # default changes.
        self.assertEqual(
            list(install.DEFAULT_PRESERVE_LIST),
            [
                "CLAUDE.md",
                ".claude/CONTEXT_STATE.md",
                ".claude/PROJECT_REGISTRY.md",
                ".env",
            ],
        )


if __name__ == "__main__":
    unittest.main()
