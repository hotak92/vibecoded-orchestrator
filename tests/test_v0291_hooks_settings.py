# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 decision #27 — `vco_lib.hooks_settings`, the ONE settings.json
hooks-block writer.

Before this module the launcher's Hooks tab was a full placebo: its
register/toggle/delete wrote `launcher.db::project_hooks`, which nothing
reads, while Claude Code's hook engine reads `.claude/settings.json`
directly. These tests pin the enforcement path that replaced it.

Every destructive / refusing branch is covered on BOTH sides — the act
case (it really edits the file) and the leave-alone case (it refuses and
the file is byte-identical afterwards).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hooks_settings as hs  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

#: A settings.json shaped like a REAL project's: VCO's env + permissions
#: blocks, a group WITH a matcher carrying three sibling hooks, a group
#: WITHOUT a matcher key (7 of the shipped template's groups are like
#: this), a second matcher group under the same event, and a top-level key
#: VCO does not own at all.
def _realistic_settings() -> dict:
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "env": {
            "KG_COLLECTION": "MyProject_KnowledgeGraph",
            "WEAVIATE_URL": "http://localhost:8081",
        },
        "permissions": {"allow": ["Bash(git status)"], "deny": []},
        "userCustomKey": {"nested": [1, 2, 3], "keep": "me"},
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit(*)|Write(*)",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/post-file-edit.sh",
                            "timeout": 30,
                        },
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/post-tool-security.sh",
                            "timeout": 10,
                        },
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/my-own-thing.sh",
                        },
                    ],
                },
                {
                    "matcher": "Write(*.py)",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/py-compile-check.sh",
                            "timeout": 10,
                        }
                    ],
                },
            ],
            "Stop": [
                {
                    # NO `matcher` key — the shape 7 shipped template groups use.
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/cost-tracker.sh",
                            "timeout": 5,
                        }
                    ]
                }
            ],
        },
    }


def _write(path: Path, data: dict, *, indent: int = 2, newline: bool = True) -> str:
    """Write a settings.json in the HOUSE canonical form.

    `ensure_ascii` is deliberately left at Python's default (True), which
    is what `project_init._merge_settings_template_for_bundle` — the
    writer that actually creates and updates this file — emits, and what
    both shipped templates store. The v0.2.91 wave-5 review's MAJOR-2 was
    exactly this axis: the fixture used to pass `ensure_ascii=False`, the
    same convention as the code under test, so the byte-fidelity tests
    were fail-toward-green by construction and could not see that a
    no-op write rewrote three comment lines on every real project. Do not
    "simplify" this back — and note that this helper is a convenience for
    the synthetic-document tests only; the byte-fidelity gate that
    matters (`TestRealShippedTemplateRoundTrip`) derives its fixture by
    RUNNING the house writer over the real shipped templates rather than
    by re-stating any convention here.
    """
    body = json.dumps(data, indent=indent)
    if newline:
        body += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return body


class _TempProject(unittest.TestCase):
    """Base with a throwaway project folder + settings.json."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.settings = self.project / ".claude" / "settings.json"
        self.original_text = _write(self.settings, _realistic_settings())

    def read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def raw(self) -> str:
        return self.settings.read_text(encoding="utf-8")

    def assert_untouched(self) -> None:
        self.assertEqual(
            self.raw(),
            self.original_text,
            "settings.json must be byte-identical after a refusal",
        )


# ═══════════════════════════════════════════════════════════════════════
# list — the read path the GUI renders from
# ═══════════════════════════════════════════════════════════════════════


class ListTests(_TempProject):
    def test_lists_every_innermost_command(self) -> None:
        doc = hs.load_settings(self.settings)
        entries, skipped = hs.list_hooks(doc)
        self.assertEqual(skipped, [])
        commands = [e["command"] for e in entries]
        self.assertEqual(len(commands), 5, commands)
        self.assertIn("bash .claude/hooks/post-file-edit.sh", commands)
        self.assertIn("bash .claude/hooks/cost-tracker.sh", commands)

    def test_group_without_matcher_normalizes_to_empty_string(self) -> None:
        doc = hs.load_settings(self.settings)
        entries, _ = hs.list_hooks(doc)
        stop = [e for e in entries if e["event"] == "Stop"]
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0]["matcher"], "")

    def test_unrepresentable_items_are_reported_not_dropped_silently(self) -> None:
        data = _realistic_settings()
        data["hooks"]["Stop"][0]["hooks"].append({"type": "command"})  # no command
        data["hooks"]["Stop"][0]["hooks"].append("not-an-object")
        _write(self.settings, data)
        doc = hs.load_settings(self.settings)
        entries, skipped = hs.list_hooks(doc)
        self.assertEqual(len(skipped), 2, skipped)
        self.assertNotIn(None, [e["command"] for e in entries])


# ═══════════════════════════════════════════════════════════════════════
# disable — surgical removal + parked entry
# ═══════════════════════════════════════════════════════════════════════


class DisableTests(_TempProject):
    def test_removes_only_the_target_and_preserves_siblings(self) -> None:
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(
            doc,
            "PostToolUse",
            "Edit(*)|Write(*)",
            "bash .claude/hooks/post-tool-security.sh",
        )
        hs.write_settings(doc)

        group = self.read()["hooks"]["PostToolUse"][0]
        commands = [h["command"] for h in group["hooks"]]
        self.assertEqual(
            commands,
            [
                "bash .claude/hooks/post-file-edit.sh",
                "bash .claude/hooks/my-own-thing.sh",
            ],
        )
        self.assertEqual(parked["hook_index"], 1)
        self.assertEqual(parked["item"]["timeout"], 10)
        self.assertFalse(parked["group_removed"])

    def test_every_unrelated_key_survives_verbatim(self) -> None:
        before = self.read()
        doc = hs.load_settings(self.settings)
        hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        hs.write_settings(doc)
        after = self.read()

        for key in ("$schema", "env", "permissions", "userCustomKey"):
            self.assertEqual(after[key], before[key], f"{key} must be untouched")
        self.assertEqual(list(after.keys()), list(before.keys()), "key ORDER preserved")

    def test_emptied_group_and_event_are_removed(self) -> None:
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        hs.write_settings(doc)
        self.assertNotIn("Stop", self.read()["hooks"])
        self.assertTrue(parked["group_removed"])

    def test_last_hook_overall_drops_the_hooks_key_entirely(self) -> None:
        data = _realistic_settings()
        data["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}
        _write(self.settings, data)
        doc = hs.load_settings(self.settings)
        hs.remove_hook(doc, "Stop", "", "x")
        hs.write_settings(doc)
        self.assertNotIn("hooks", self.read())

    def test_missing_entry_refuses_and_leaves_the_file_alone(self) -> None:
        doc = hs.load_settings(self.settings)
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.remove_hook(doc, "PostToolUse", "Edit(*)|Write(*)", "nope")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assert_untouched()

    def test_matcher_must_match_not_just_the_command(self) -> None:
        """The natural key is (event, matcher, command). A right command
        under the WRONG matcher must not be silently removed."""
        doc = hs.load_settings(self.settings)
        with self.assertRaises(hs.HooksSettingsError):
            hs.remove_hook(
                doc, "PostToolUse", "Write(*.py)", "bash .claude/hooks/post-file-edit.sh"
            )
        self.assert_untouched()


# ═══════════════════════════════════════════════════════════════════════
# enable — restore the parked entry
# ═══════════════════════════════════════════════════════════════════════


class EnableTests(_TempProject):
    def test_disable_then_enable_restores_the_original_bytes(self) -> None:
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(
            doc,
            "PostToolUse",
            "Edit(*)|Write(*)",
            "bash .claude/hooks/post-tool-security.sh",
        )
        hs.write_settings(doc)
        self.assertNotEqual(self.raw(), self.original_text)

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        self.assertEqual(
            self.raw(),
            self.original_text,
            "re-enable must restore the file byte-for-byte",
        )

    def test_restores_a_whole_removed_group_without_inventing_a_matcher(self) -> None:
        """The `Stop` group has NO `matcher` key. Removing its only hook
        drops the group; restoring must NOT grow a `matcher: ""`."""
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        hs.write_settings(doc)

        doc2 = hs.load_settings(self.settings)
        hs.insert_hook(doc2, parked)
        hs.write_settings(doc2)
        group = self.read()["hooks"]["Stop"][0]
        self.assertNotIn("matcher", group)
        self.assertEqual(self.raw(), self.original_text)

    def test_enable_is_idempotent_when_the_command_is_already_present(self) -> None:
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        hs.write_settings(doc)
        doc2 = hs.load_settings(self.settings)
        hs.insert_hook(doc2, parked)
        hs.write_settings(doc2)

        # Second restore (double-click, or the user put the line back by
        # hand) must NOT create a duplicate invocation.
        doc3 = hs.load_settings(self.settings)
        self.assertFalse(hs.insert_hook(doc3, parked))
        self.assertEqual(self.raw(), self.original_text)

    def test_restore_clamps_a_stale_position_instead_of_failing(self) -> None:
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(
            doc, "PostToolUse", "Edit(*)|Write(*)", "bash .claude/hooks/my-own-thing.sh"
        )
        # Simulate the user deleting the two siblings while it was parked.
        doc.data["hooks"]["PostToolUse"][0]["hooks"] = []
        hs.write_settings(doc)

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        group = self.read()["hooks"]["PostToolUse"][0]
        self.assertEqual(
            [h["command"] for h in group["hooks"]],
            ["bash .claude/hooks/my-own-thing.sh"],
        )

    # ── Ordinal / structural fidelity of the group-removed restore ──────
    #
    # Each fixture below reproduces a shape that the REAL shipped template
    # has and that `_realistic_settings` happened not to (the whole reason
    # the wave shipped these defects green): a singleton event that is not
    # the LAST key, several groups under one event sharing a matcher, and
    # a group storing `hooks` BEFORE `matcher`. The shapes are cited to
    # their template positions so a future template change can be traced
    # back here.

    def test_restoring_a_singleton_event_puts_the_event_key_back_in_place(
        self,
    ) -> None:
        """Shape: `hooks.PreCompact` — one group, one hook, and NOT the
        last key of the block (template events 2, 3, 6, 7, 9, 11 are all
        like this). Disabling drops the event key; a plain re-add would
        append it last and reorder the whole file."""
        data = _realistic_settings()
        data["hooks"] = {
            "PreCompact": [
                {
                    "matcher": "auto",
                    "hooks": [{"type": "command", "command": "bash a.sh", "timeout": 5}],
                }
            ],
            "PostToolUse": data["hooks"]["PostToolUse"],
            "Stop": data["hooks"]["Stop"],
        }
        original = _write(self.settings, data)

        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "PreCompact", "auto", "bash a.sh")
        hs.write_settings(doc)
        self.assertNotIn("PreCompact", self.read()["hooks"])

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        self.assertEqual(
            list(self.read()["hooks"].keys()), ["PreCompact", "PostToolUse", "Stop"]
        )
        self.assertEqual(self.raw(), original)

    def test_restoring_the_last_hook_overall_puts_the_hooks_key_back_in_place(
        self,
    ) -> None:
        """Same hazard one level up: the shipped template's `hooks` block
        is followed by `_env_comment`, so dropping and re-adding the whole
        block would move it to the end of the document."""
        data = {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},
            "_env_comment": "trailing key, as in the shipped template",
        }
        original = _write(self.settings, data)

        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "Stop", "", "x")
        hs.write_settings(doc)
        self.assertNotIn("hooks", self.read())

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        self.assertEqual(list(self.read().keys()), ["$schema", "hooks", "_env_comment"])
        self.assertEqual(self.raw(), original)

    def test_restore_recreates_its_group_instead_of_merging_into_a_sibling(
        self,
    ) -> None:
        """Shape: three `PreToolUse` groups share the matcher `Bash` in the
        shipped template. When the disable EMPTIES a group, the survivors
        shift into its index — so both the index probe and the
        first-group-with-this-matcher fallback would drop the item into a
        DIFFERENT group, changing the file's structure while the entry
        still "works"."""
        data = _realistic_settings()
        data["hooks"]["PreToolUse"] = [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash one.sh"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash two.sh"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash three.sh"}]},
        ]
        original = _write(self.settings, data)

        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "PreToolUse", "Bash", "bash one.sh")
        hs.write_settings(doc)
        self.assertTrue(parked["group_removed"])
        self.assertEqual(len(self.read()["hooks"]["PreToolUse"]), 2)

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        groups = self.read()["hooks"]["PreToolUse"]
        self.assertEqual(
            [[h["command"] for h in g["hooks"]] for g in groups],
            [["bash one.sh"], ["bash two.sh"], ["bash three.sh"]],
            "the entry returns to its OWN group, not a same-matcher sibling",
        )
        self.assertEqual(self.raw(), original)

    def test_recreated_group_preserves_its_original_key_order(self) -> None:
        """Shape: the shipped template's `PostToolUse[9]` stores `hooks`
        BEFORE `matcher`. Rebuilding the group from its other keys and
        then assigning `hooks` would silently swap them."""
        data = _realistic_settings()
        data["hooks"]["ConfigChange"] = [
            {
                "hooks": [{"type": "command", "command": "bash cfg.sh"}],
                "matcher": "*",
            }
        ]
        original = _write(self.settings, data)
        self.assertLess(original.index('"hooks"', original.index('"ConfigChange"')),
                        original.index('"matcher": "*"'))

        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "ConfigChange", "*", "bash cfg.sh")
        hs.write_settings(doc)
        doc2 = hs.load_settings(self.settings)
        hs.insert_hook(doc2, parked)
        hs.write_settings(doc2)
        self.assertEqual(
            list(self.read()["hooks"]["ConfigChange"][0].keys()), ["hooks", "matcher"]
        )
        self.assertEqual(self.raw(), original)

    def test_restore_is_a_no_op_when_a_sibling_group_already_has_the_entry(
        self,
    ) -> None:
        """Idempotency is event-wide, and it runs BEFORE any structural
        edit: a user who put the line back by hand in another group with
        the same matcher gets no duplicate invocation and no rewrite."""
        data = _realistic_settings()
        data["hooks"]["PreToolUse"] = [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash one.sh"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash two.sh"}]},
        ]
        _write(self.settings, data)

        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "PreToolUse", "Bash", "bash one.sh")
        hs.write_settings(doc)
        # The user restores it by hand — into the OTHER group.
        doc_hand = hs.load_settings(self.settings)
        doc_hand.data["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": "bash one.sh"}
        )
        hs.write_settings(doc_hand)
        hand_text = self.raw()

        doc2 = hs.load_settings(self.settings)
        self.assertFalse(hs.insert_hook(doc2, parked))
        hs.write_settings(doc2)
        self.assertEqual(self.raw(), hand_text, "no duplicate, no rewrite")
        commands = [
            h["command"]
            for g in self.read()["hooks"]["PreToolUse"]
            for h in g["hooks"]
        ]
        self.assertEqual(commands.count("bash one.sh"), 1)

    def test_a_parked_entry_without_the_ordinals_still_restores(self) -> None:
        """Backward compatibility for entries parked by an earlier build:
        the ordinal keys are optional, and their absence falls back to the
        old append behaviour rather than refusing to restore."""
        doc = hs.load_settings(self.settings)
        parked = hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        hs.write_settings(doc)
        legacy = {
            k: v
            for k, v in parked.items()
            if k not in ("group_key_index", "event_index", "hooks_key_index")
        }

        doc2 = hs.load_settings(self.settings)
        self.assertTrue(hs.insert_hook(doc2, legacy))
        hs.write_settings(doc2)
        self.assertEqual(
            [h["command"] for h in self.read()["hooks"]["Stop"][0]["hooks"]],
            ["bash .claude/hooks/cost-tracker.sh"],
        )

    def test_rejects_a_parked_entry_of_an_unknown_schema(self) -> None:
        doc = hs.load_settings(self.settings)
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.insert_hook(doc, {"schema": 99, "event": "Stop", "item": {}})
        self.assertEqual(ctx.exception.code, "parked_entry_invalid")
        self.assert_untouched()

    def test_rejects_a_parked_entry_missing_its_item(self) -> None:
        doc = hs.load_settings(self.settings)
        with self.assertRaises(hs.HooksSettingsError):
            hs.insert_hook(doc, {"schema": 1, "event": "Stop"})
        self.assert_untouched()


# ═══════════════════════════════════════════════════════════════════════
# register / unregister
# ═══════════════════════════════════════════════════════════════════════


class RegisterTests(_TempProject):
    def test_joins_an_existing_matcher_group(self) -> None:
        doc = hs.load_settings(self.settings)
        self.assertTrue(
            hs.register_hook(
                doc, "PostToolUse", "Edit(*)|Write(*)", "bash .claude/hooks/new.sh", 7
            )
        )
        hs.write_settings(doc)
        groups = self.read()["hooks"]["PostToolUse"]
        self.assertEqual(len(groups), 2, "must NOT create a parallel group")
        self.assertEqual(groups[0]["hooks"][-1]["command"], "bash .claude/hooks/new.sh")
        self.assertEqual(groups[0]["hooks"][-1]["timeout"], 7)
        self.assertEqual(groups[0]["hooks"][-1]["type"], "command")

    def test_creates_a_new_group_for_a_new_matcher(self) -> None:
        doc = hs.load_settings(self.settings)
        hs.register_hook(doc, "PostToolUse", "Bash(*)", "bash .claude/hooks/b.sh")
        hs.write_settings(doc)
        groups = self.read()["hooks"]["PostToolUse"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[-1]["matcher"], "Bash(*)")

    def test_empty_matcher_creates_a_group_with_no_matcher_key(self) -> None:
        doc = hs.load_settings(self.settings)
        hs.register_hook(doc, "SessionEnd", "", "bash .claude/hooks/e.sh")
        hs.write_settings(doc)
        group = self.read()["hooks"]["SessionEnd"][0]
        self.assertNotIn("matcher", group)

    def test_creates_a_brand_new_event(self) -> None:
        doc = hs.load_settings(self.settings)
        hs.register_hook(doc, "PreCompact", "auto", "bash .claude/hooks/pc.sh")
        hs.write_settings(doc)
        self.assertIn("PreCompact", self.read()["hooks"])

    def test_registering_a_duplicate_changes_nothing(self) -> None:
        doc = hs.load_settings(self.settings)
        self.assertFalse(
            hs.register_hook(
                doc,
                "PostToolUse",
                "Edit(*)|Write(*)",
                "bash .claude/hooks/post-file-edit.sh",
            )
        )
        self.assert_untouched()

    def test_rejects_empty_event_and_command_and_bad_timeout(self) -> None:
        doc = hs.load_settings(self.settings)
        for kwargs, code in (
            (dict(event="  ", matcher="", command="x"), "invalid_event"),
            (dict(event="Stop", matcher="", command="   "), "invalid_command"),
            (
                dict(event="Stop", matcher="", command="x", timeout_seconds=0),
                "invalid_timeout",
            ),
            (
                dict(event="Stop", matcher="", command="x", timeout_seconds=-3),
                "invalid_timeout",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(hs.HooksSettingsError) as ctx:
                    hs.register_hook(doc, **kwargs)
                self.assertEqual(ctx.exception.code, code)
        self.assert_untouched()


class UnregisterTests(_TempProject):
    def test_unregister_never_deletes_the_script_file(self) -> None:
        script = self.project / ".claude" / "hooks" / "my-own-thing.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "vco_lib.hooks_settings",
                "unregister",
                "--project-folder",
                str(self.project),
                "--event",
                "PostToolUse",
                "--matcher",
                "Edit(*)|Write(*)",
                "--command",
                "bash .claude/hooks/my-own-thing.sh",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["script_file_deleted"])
        self.assertTrue(script.is_file(), "the hook SCRIPT must survive unregister")
        commands = [
            h["command"] for h in self.read()["hooks"]["PostToolUse"][0]["hooks"]
        ]
        self.assertNotIn("bash .claude/hooks/my-own-thing.sh", commands)


# ═══════════════════════════════════════════════════════════════════════
# Refusals — every one leaves the file byte-identical
# ═══════════════════════════════════════════════════════════════════════


class RefusalTests(_TempProject):
    def test_unparseable_settings_json_refuses_without_clobbering(self) -> None:
        broken = '{ "hooks": { "Stop": [ }  <- trailing garbage'
        self.settings.write_text(broken, encoding="utf-8")
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.load_settings(self.settings)
        self.assertEqual(ctx.exception.code, "unparseable")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), broken)

    def test_non_object_document_refuses(self) -> None:
        self.settings.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.load_settings(self.settings)
        self.assertEqual(ctx.exception.code, "not_an_object")

    def test_malformed_hooks_block_shapes_refuse(self) -> None:
        for hooks_value in (
            "a string",
            {"Stop": "not-an-array"},
            {"Stop": ["not-an-object"]},
            {"Stop": [{"hooks": "not-an-array"}]},
        ):
            with self.subTest(hooks=hooks_value):
                self.settings.write_text(
                    json.dumps({"hooks": hooks_value}), encoding="utf-8"
                )
                with self.assertRaises(hs.HooksSettingsError) as ctx:
                    hs.load_settings(self.settings)
                self.assertIn(
                    ctx.exception.code,
                    ("hooks_block_malformed",),
                    ctx.exception.message,
                )

    def test_missing_settings_json_refuses_rather_than_creating_one(self) -> None:
        self.settings.unlink()
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.load_settings(self.settings)
        self.assertEqual(ctx.exception.code, "missing")
        self.assertFalse(self.settings.exists(), "must not conjure a settings.json")

    @unittest.skipIf(os.name == "nt", "symlink creation needs privilege on Windows")
    def test_symlinked_settings_json_refuses_to_write_through(self) -> None:
        real = self.project / "elsewhere.json"
        real_text = json.dumps(_realistic_settings(), indent=2) + "\n"
        real.write_text(real_text, encoding="utf-8")
        self.settings.unlink()
        self.settings.symlink_to(real)

        doc = hs.load_settings(self.settings)
        hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.write_settings(doc)
        self.assertEqual(ctx.exception.code, "symlink_blocked")
        self.assertEqual(
            real.read_text(encoding="utf-8"),
            real_text,
            "the symlink TARGET must be untouched",
        )

    @unittest.skipIf(os.name == "nt", "symlink creation needs privilege on Windows")
    def test_symlinked_claude_dir_refuses_to_write_through(self) -> None:
        real_dir = self.project / "shared-claude"
        real_dir.mkdir()
        real_settings = real_dir / "settings.json"
        real_text = json.dumps(_realistic_settings(), indent=2) + "\n"
        real_settings.write_text(real_text, encoding="utf-8")
        import shutil

        shutil.rmtree(self.project / ".claude")
        (self.project / ".claude").symlink_to(real_dir, target_is_directory=True)

        doc = hs.load_settings(self.settings)
        hs.remove_hook(doc, "Stop", "", "bash .claude/hooks/cost-tracker.sh")
        with self.assertRaises(hs.HooksSettingsError) as ctx:
            hs.write_settings(doc)
        self.assertEqual(ctx.exception.code, "symlink_blocked")
        self.assertEqual(real_settings.read_text(encoding="utf-8"), real_text)


# ═══════════════════════════════════════════════════════════════════════
# Formatting fidelity
# ═══════════════════════════════════════════════════════════════════════


class FormattingTests(_TempProject):
    def test_no_op_round_trip_on_a_canonical_file_is_byte_identical(self) -> None:
        doc = hs.load_settings(self.settings)
        hs.write_settings(doc)
        self.assert_untouched()

    def test_four_space_indent_is_preserved(self) -> None:
        original = _write(self.settings, _realistic_settings(), indent=4)
        self.assertEqual(hs.detect_indent(original), 4)
        doc = hs.load_settings(self.settings)
        hs.write_settings(doc)
        self.assertEqual(self.raw(), original)

    def test_absent_trailing_newline_is_preserved(self) -> None:
        original = _write(self.settings, _realistic_settings(), newline=False)
        doc = hs.load_settings(self.settings)
        hs.write_settings(doc)
        self.assertEqual(self.raw(), original)
        self.assertFalse(self.raw().endswith("\n"))

    def test_minified_file_normalizes_to_the_house_indent_once(self) -> None:
        minified = json.dumps(_realistic_settings())
        self.settings.write_text(minified, encoding="utf-8")
        doc = hs.load_settings(self.settings)
        hs.register_hook(doc, "SessionEnd", "", "bash .claude/hooks/e.sh")
        hs.write_settings(doc)
        first = self.raw()
        self.assertIn("\n  ", first, "normalized to the 2-space house form")
        # Second pass changes nothing further — normalization happens once.
        doc2 = hs.load_settings(self.settings)
        hs.write_settings(doc2)
        self.assertEqual(self.raw(), first)

    def test_non_ascii_content_survives_the_round_trip(self) -> None:
        """Semantics always survive; a canonical file is byte-stable."""
        data = _realistic_settings()
        data["userCustomKey"]["note"] = "café — ünïcode ✓"
        original = _write(self.settings, data)
        self.assertIn(
            "\\u2014", original, "the house form stores non-ASCII as escapes"
        )
        doc = hs.load_settings(self.settings)
        hs.write_settings(doc)
        self.assertEqual(self.raw(), original, "already canonical → byte-identical")
        self.assertEqual(self.read()["userCustomKey"]["note"], "café — ünïcode ✓")

    def test_literal_utf8_file_normalizes_to_the_house_escapes_once(self) -> None:
        """A hand-edited file holding literal UTF-8 is normalised to the
        house escape form exactly once — the same "normalise once" policy
        the indent sniffer applies to a minified file, never a rewrite on
        every subsequent toggle."""
        data = _realistic_settings()
        data["userCustomKey"]["note"] = "café — ünïcode ✓"
        hand_edited = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.settings.write_text(hand_edited, encoding="utf-8")

        doc = hs.load_settings(self.settings)
        hs.write_settings(doc)
        first = self.raw()
        self.assertNotEqual(first, hand_edited, "normalised to the house form")
        self.assertIn("\\u2014", first)
        self.assertEqual(
            json.loads(first)["userCustomKey"]["note"],
            "café — ünïcode ✓",
            "normalisation is cosmetic — the value is unchanged",
        )
        # Second pass changes nothing further.
        doc2 = hs.load_settings(self.settings)
        hs.write_settings(doc2)
        self.assertEqual(self.raw(), first)


# ═══════════════════════════════════════════════════════════════════════
# Byte fidelity against the REAL shipped templates
#
# v0.2.91 wave-5 review MAJOR-2. The claim under test is the one the
# module docstring and the CHANGELOG make — "a round-trip on an already-
# canonical file is byte-identical", "key order is preserved … untouched"
# — and it is a claim about REAL projects, so it is tested against real
# project bytes.
#
# Fixture provenance (the point of this class): the fixture is NOT
# hand-written and NOT re-serialised here. It is produced by RUNNING the
# authoritative producer — `project_init._merge_settings_template_for_
# bundle`, the function that actually creates `.claude/settings.json` on
# every install and bundle update — over the two real shipped templates.
# A fixture written by the code under test's own convention is exactly
# what let MAJOR-2 ship green (see `knowledge/concepts/
# source-text-gates-fail-toward-green-2026-08-27.md`), so the fixture
# here comes from the other side of the seam, and
# `test_the_fixture_really_exercises_the_escape_axis` proves the axis is
# live rather than normalised away.
# ═══════════════════════════════════════════════════════════════════════

SHIPPED_TEMPLATES = (
    "settings.json.linux.template",
    "settings.json.windows.template",
)


def _house_writer_output(template_name: str, target: Path) -> str:
    """Create `target` the way a real install does, and return its bytes.

    Deliberately routed through the real bundle-merge writer rather than
    a local `json.dumps` — this test's whole value is that its expected
    bytes come from the producer, not from a restatement of the
    producer's convention.
    """
    from vco_lib.project_init import _merge_settings_template_for_bundle

    template = REPO_ROOT / "templates" / template_name
    target.parent.mkdir(parents=True, exist_ok=True)
    status, _redirect = _merge_settings_template_for_bundle(
        template, target, dry_run=False
    )
    assert status == "created", f"unexpected writer status {status!r}"
    return target.read_text(encoding="utf-8")


class TestRealShippedTemplateRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.settings = self.project / ".claude" / "settings.json"

    def test_the_fixture_really_exercises_the_escape_axis(self) -> None:
        """Meta-check: the gate below is only meaningful while the real
        templates still contain non-ASCII stored as escapes. If they ever
        stop doing so — or if someone regenerates them with
        `ensure_ascii=False` — the byte-identity assertions would pass
        vacuously on this axis, so fail loudly here instead."""
        for name in SHIPPED_TEMPLATES:
            with self.subTest(template=name):
                canonical = _house_writer_output(name, self.settings)
                self.settings.unlink()
                self.assertIn(
                    "\\u2014",
                    canonical,
                    "the shipped template must still store non-ASCII as escapes",
                )
                self.assertTrue(
                    canonical.isascii(),
                    "the house canonical form emits no raw non-ASCII bytes",
                )
                wrong = (
                    json.dumps(json.loads(canonical), indent=2, ensure_ascii=False)
                    + "\n"
                )
                self.assertNotEqual(
                    wrong,
                    canonical,
                    "the ensure_ascii axis must be observable in this fixture",
                )

    def test_the_shipped_template_is_the_house_writers_own_output(self) -> None:
        """Anchors "do not touch the templates": each one is byte-for-byte
        what the bundle writer emits for its own parsed content (modulo
        the trailing newline the writer appends), which is why it can
        serve as the fixture of record."""
        for name in SHIPPED_TEMPLATES:
            with self.subTest(template=name):
                template_text = (REPO_ROOT / "templates" / name).read_text(
                    encoding="utf-8"
                )
                canonical = _house_writer_output(name, self.settings)
                self.settings.unlink()
                self.assertEqual(canonical, template_text.rstrip("\n") + "\n")

    def test_render_matches_the_house_writers_convention(self) -> None:
        """`SettingsDoc.render` must agree with the producer byte-for-byte
        for the same document — the parity pin that makes
        `CANONICAL_ENSURE_ASCII` a derived fact rather than a second
        opinion. Fails on any future drift on EITHER side."""
        for name in SHIPPED_TEMPLATES:
            with self.subTest(template=name):
                canonical = _house_writer_output(name, self.settings)
                doc = hs.load_settings(self.settings)
                self.settings.unlink()
                self.assertEqual(doc.render(), canonical)

    def test_no_op_load_and_write_is_byte_identical(self) -> None:
        for name in SHIPPED_TEMPLATES:
            with self.subTest(template=name):
                canonical = _house_writer_output(name, self.settings)
                hs.write_settings(hs.load_settings(self.settings))
                self.assertEqual(self.settings.read_text(encoding="utf-8"), canonical)
                self.settings.unlink()

    def test_every_entry_disable_then_enable_is_byte_identical(self) -> None:
        for name in SHIPPED_TEMPLATES:
            canonical = _house_writer_output(name, self.settings)
            entries, skipped = hs.list_hooks(hs.load_settings(self.settings))
            self.assertEqual(skipped, [], f"{name}: every entry must be representable")
            self.assertGreaterEqual(
                len(entries), 40, f"{name}: the template shrank unexpectedly"
            )
            for entry in entries:
                with self.subTest(template=name, command=entry["command"]):
                    self.settings.write_text(canonical, encoding="utf-8")
                    doc = hs.load_settings(self.settings)
                    parked = hs.remove_hook(
                        doc, entry["event"], entry["matcher"], entry["command"]
                    )
                    hs.write_settings(doc)
                    self.assertNotEqual(
                        self.settings.read_text(encoding="utf-8"),
                        canonical,
                        "disable must really remove the entry",
                    )
                    doc2 = hs.load_settings(self.settings)
                    self.assertTrue(hs.insert_hook(doc2, parked))
                    hs.write_settings(doc2)
                    self.assertEqual(
                        self.settings.read_text(encoding="utf-8"),
                        canonical,
                        "re-enable must restore the file byte-for-byte",
                    )
            self.settings.unlink()

    def test_the_parked_json_the_caller_stores_survives_a_string_round_trip(
        self,
    ) -> None:
        """The Rust caller stores `parked_json` as an opaque string and
        hands it back verbatim. Restoring from the SERIALISED form (not
        the in-memory dict) must be just as exact, for every entry — the
        wire form is `_cmd_disable`'s `json.dumps(parked,
        ensure_ascii=False)`, which is deliberately NOT the file's
        convention (it never reaches the file)."""
        canonical = _house_writer_output(SHIPPED_TEMPLATES[0], self.settings)
        entries, _ = hs.list_hooks(hs.load_settings(self.settings))
        for entry in entries:
            with self.subTest(command=entry["command"]):
                self.settings.write_text(canonical, encoding="utf-8")
                doc = hs.load_settings(self.settings)
                parked = hs.remove_hook(
                    doc, entry["event"], entry["matcher"], entry["command"]
                )
                hs.write_settings(doc)
                doc2 = hs.load_settings(self.settings)
                wire = json.dumps(parked, ensure_ascii=False)
                hs.insert_hook(doc2, json.loads(wire))
                hs.write_settings(doc2)
                self.assertEqual(self.settings.read_text(encoding="utf-8"), canonical)


# ═══════════════════════════════════════════════════════════════════════
# Starter-script seeding
# ═══════════════════════════════════════════════════════════════════════


class StarterScriptTests(_TempProject):
    def test_creates_a_runnable_bash_starter(self) -> None:
        result = hs.create_starter_script(
            self.project, "bash .claude/hooks/brand-new.sh", "PostToolUse"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["created"])
        script = self.project / ".claude" / "hooks" / "brand-new.sh"
        body = script.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("#!/usr/bin/env bash"))
        self.assertIn("PostToolUse", body)

    def test_creates_a_powershell_starter_for_a_ps1_command(self) -> None:
        hs.create_starter_script(
            self.project,
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            ".claude/hooks/brand-new.ps1",
            "Stop",
        )
        script = self.project / ".claude" / "hooks" / "brand-new.ps1"
        body = script.read_text(encoding="utf-8")
        self.assertIn("$ErrorActionPreference", body)
        self.assertNotIn("#!/usr/bin/env bash", body)

    def test_never_clobbers_an_existing_script(self) -> None:
        script = self.project / ".claude" / "hooks" / "mine.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("MY PRECIOUS CONTENT\n", encoding="utf-8")
        result = hs.create_starter_script(
            self.project, "bash .claude/hooks/mine.sh", "Stop"
        )
        assert result is not None
        self.assertFalse(result["created"])
        self.assertEqual(script.read_text(encoding="utf-8"), "MY PRECIOUS CONTENT\n")

    def test_no_starter_for_a_command_that_invokes_no_script(self) -> None:
        self.assertIsNone(
            hs.create_starter_script(self.project, "echo hello", "Stop")
        )

    def test_no_starter_for_an_absolute_or_traversing_path(self) -> None:
        for command in ("bash /etc/evil.sh", "bash ../../outside.sh"):
            with self.subTest(command=command):
                self.assertIsNone(
                    hs.create_starter_script(self.project, command, "Stop")
                )

    def test_argument_position_script_is_not_seeded(self) -> None:
        """`bash wrapper.sh --target .claude/hooks/x.sh` invokes wrapper.sh;
        the second path is an ARGUMENT. Seeding it would be wrong."""
        result = hs.create_starter_script(
            self.project, "bash wrapper.sh --target .claude/hooks/x.sh", "Stop"
        )
        assert result is not None
        self.assertEqual(Path(result["path"]).name, "wrapper.sh")
        self.assertFalse((self.project / ".claude" / "hooks" / "x.sh").exists())


# ═══════════════════════════════════════════════════════════════════════
# CLI contract — stdout is machine-readable, exit codes are stable
# ═══════════════════════════════════════════════════════════════════════


class CliTests(_TempProject):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "vco_lib.hooks_settings", *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )

    def test_list_emits_exactly_one_json_object(self) -> None:
        r = self._run("list", "--project-folder", str(self.project))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["hooks"]), 5)
        self.assertNotIn("item", payload["hooks"][0], "raw item is not in the wire shape")

    def test_disable_then_enable_via_cli_restores_the_file(self) -> None:
        r = self._run(
            "disable",
            "--project-folder",
            str(self.project),
            "--event",
            "Stop",
            "--matcher",
            "",
            "--command",
            "bash .claude/hooks/cost-tracker.sh",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        parked = json.loads(r.stdout)["parked"]
        self.assertNotIn("Stop", self.read()["hooks"])

        r2 = self._run(
            "enable",
            "--project-folder",
            str(self.project),
            "--entry-json",
            json.dumps(parked),
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertTrue(json.loads(r2.stdout)["changed"])
        self.assertEqual(self.raw(), self.original_text)

    def test_disable_emits_a_prebaked_parked_json_that_preserves_key_order(self) -> None:
        """Regression: the Rust caller stores `parked_json` verbatim.

        It must NOT rebuild the string from the `parked` object, because
        `serde_json::Value` is a BTreeMap without the `preserve_order`
        feature — a round trip there sorts the inner item's keys
        (`type, command, timeout` -> `command, timeout, type`) and the file
        restored on re-enable stops matching the original byte-for-byte.
        Handing the caller a ready-made string is what removes that
        opportunity, so the field has to exist and has to be ordered.
        """
        r = self._run(
            "disable",
            "--project-folder",
            str(self.project),
            "--event",
            "Stop",
            "--matcher",
            "",
            "--command",
            "bash .claude/hooks/cost-tracker.sh",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        parked_json = payload["parked_json"]
        self.assertIsInstance(parked_json, str)
        self.assertEqual(json.loads(parked_json), payload["parked"])
        item = parked_json[parked_json.index('"item"') :]
        self.assertLess(
            item.index('"type"'),
            item.index('"command"'),
            "the inner hook item's original key order must survive",
        )

    def test_register_with_create_starter_writes_both(self) -> None:
        r = self._run(
            "register",
            "--project-folder",
            str(self.project),
            "--event",
            "SessionEnd",
            "--matcher",
            "",
            "--command",
            "bash .claude/hooks/fresh.sh",
            "--timeout-seconds",
            "12",
            "--create-starter",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["changed"])
        self.assertTrue(payload["starter"]["created"])
        self.assertTrue((self.project / ".claude" / "hooks" / "fresh.sh").is_file())
        self.assertEqual(
            self.read()["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 12
        )

    def test_refusal_exits_nonzero_with_a_stable_code_and_no_write(self) -> None:
        r = self._run(
            "disable",
            "--project-folder",
            str(self.project),
            "--event",
            "Stop",
            "--matcher",
            "",
            "--command",
            "does-not-exist",
        )
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "not_found")
        self.assert_untouched()

    def test_unparseable_file_refusal_over_the_cli(self) -> None:
        self.settings.write_text("{ broken", encoding="utf-8")
        r = self._run("list", "--project-folder", str(self.project))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["code"], "unparseable")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ broken")

    def test_no_target_arguments_exits_two(self) -> None:
        r = self._run("list")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["code"], "no_target")


# ═══════════════════════════════════════════════════════════════════════
# Tokenizer parity with project_init's hook identity (the extraction)
# ═══════════════════════════════════════════════════════════════════════


class TokenizerParityTests(unittest.TestCase):
    """`project_init._vco_hook_script_identity` now delegates to
    `hooks_settings.invoked_script_tokens`. These cases pin the shapes the
    bundle-merge supersede logic depends on — a regression here silently
    rewrites or drops a user's own hook at the next bundle update."""

    def setUp(self) -> None:
        from vco_lib import project_init

        self.identity = project_init._vco_hook_script_identity

    def test_invoked_shapes_resolve(self) -> None:
        for command, expected in (
            (".claude/hooks/x.sh", "x.sh"),
            ("bash .claude/hooks/x.sh", "x.sh"),
            ("bash '.claude/hooks/pre-tool-use.sh'", "pre-tool-use.sh"),
            (
                '[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/pre-tool-use.sh',
                "pre-tool-use.sh",
            ),
            (
                "powershell -NoProfile -ExecutionPolicy Bypass -File "
                ".claude\\hooks\\x.ps1",
                "x.ps1",
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.identity(command), expected)

    def test_non_invoked_shapes_return_none(self) -> None:
        for command in (
            "cat .claude/hooks/x.sh | grep foo",
            "bash my-wrapper.sh --target .claude/hooks/x.sh",
            "echo hello",
            "",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.identity(command))

    def test_constants_are_the_shared_ones(self) -> None:
        from vco_lib import project_init

        self.assertIs(project_init._HOOK_INTERPRETER_TOKENS, hs.INTERPRETER_TOKENS)
        self.assertIs(project_init._HOOK_SCRIPT_FLAG_TOKENS, hs.SCRIPT_FLAG_TOKENS)
        self.assertIs(
            project_init._HOOK_CMD_SEPARATOR_TOKENS, hs.CMD_SEPARATOR_TOKENS
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
