# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Retired hook REGISTRATIONS are removed by the bundle engine (v0.2.95).

THE DEFECT. Retiring a hook has two halves: the SCRIPT stops shipping (the
manifest reconcile deletes it from the project) and the REGISTRATION stops
firing. Only the first was ever done. ``_merge_hooks_for_bundle`` recognises a
VCO hook by its presence in the CURRENT template, so the moment a hook stops
being shipped its stale registration stops being recognised as VCO's and is
preserved byte-for-byte as "the user's own" — forever, on every update.

Field evidence (field report 2026-09-14): two ``sync_knowledge_graph.py``
registrations VCO wrote in 2026-04/05 were still firing on every ``Edit`` a
year later, failing with ``ModuleNotFoundError: No module named 'weaviate_mcp'``
behind a ``|| true`` that hid it completely. A third retiree arrives with
v0.2.95 (cost telemetry): its script is deleted by the update, and without this
mechanism every session would then end by invoking a file that is not there.

WHAT THESE TESTS PIN.

1. The table matches what VCO ACTUALLY SHIPPED — the command strings are
   quoted here verbatim from this repository's git history (plus the
   guard-less variant observed on a live install that predates the public
   repo). A retirement that matches nothing is a comment, not a mechanism.
2. The ACT: a matching registration is removed, its group pruned when empty,
   and the removal is RECORDED.
3. The LEAVE-ALONE (the half that matters more): a user command that merely
   CONTAINS a retired path, a user hook invoking a different script, and the
   same command under a DIFFERENT event are all preserved byte-for-byte.
4. Delivery (ruling R17): the removal happens through the ONE bundle engine on
   ``--update`` against an already-damaged install, and writes an audit row.
5. Idempotency + state-keying (ruling R26): a second run removes nothing and
   changes nothing, and the match never depends on which release the project
   came from.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hook_retirements as hr  # noqa: E402
from vco_lib import project_init  # noqa: E402

# ── The command strings VCO shipped, verbatim ──────────────────────────────
#
# Provenance (each recoverable with `git log -S`):
#   SYNC_SH_GUARDED   templates/settings.json.template @ 47214144 (2026-04-25)
#   SYNC_SH_BARE      a live install predating the public repo (the field
#                     report 2026-09-14, line 22) — same command, no guard
#   SYNC_INLINE_PY    templates/settings.json.linux.template @ 4813f5bd
#   SYNC_PS1          templates/settings.json.windows.template @ da830e45
SYNC_SH_GUARDED = (
    '[ -n "$VCT_DISABLE_HOOKS" ] || python .claude/scripts/sync_knowledge_graph.py '
    '"$CLAUDE_TOOL_ARG_FILE_PATH" 2>&1 || true'
)
SYNC_SH_BARE = (
    'python .claude/scripts/sync_knowledge_graph.py '
    '"$CLAUDE_TOOL_ARG_FILE_PATH" 2>&1 || true'
)
SYNC_INLINE_PY = (
    '[ -n "$VCT_DISABLE_HOOKS" ] || python3 -c "import json,sys,subprocess,os; '
    "d=json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}; "
    "fp=d.get('tool_input',{}).get('file_path',''); "
    "subprocess.run(['python','.claude/scripts/sync_knowledge_graph.py',fp]) "
    'if fp else None" 2>&1 || true'
)
SYNC_PS1 = (
    'powershell -NoProfile -Command "try { python '
    ".claude/scripts/sync_knowledge_graph.py "
    '$env:CLAUDE_TOOL_ARG_FILE_PATH } catch { }"'
)
COST_SH = '[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/cost-tracker.sh'
COST_PS1 = "powershell -NoProfile -ExecutionPolicy Bypass -File .claude/hooks/cost-tracker.ps1"


def _cmds(hooks: dict, event: str) -> list:
    out = []
    for entry in hooks.get(event, []):
        for h in entry.get("hooks", []):
            if h.get("command"):
                out.append(h["command"])
    return out


class TableIsHonestTests(unittest.TestCase):
    """The declared table matches reality on both ends."""

    def test_every_shipped_shape_matches_its_retirement(self):
        cases = [
            ("PostToolUse", SYNC_SH_GUARDED, None),
            ("PostToolUse", SYNC_SH_BARE, None),
            ("PostToolUse", SYNC_INLINE_PY, None),
            ("PostToolUse", SYNC_PS1, None),
            ("Stop", COST_SH, "cost-tracker.sh"),
            ("Stop", COST_PS1, "cost-tracker.ps1"),
        ]
        for event, cmd, identity in cases:
            with self.subTest(cmd=cmd[:48]):
                self.assertIsNotNone(
                    hr.match_retired_registration(event, cmd, hook_identity=identity),
                    "a shipped registration that the table does not match is a "
                    "comment, not a mechanism",
                )

    def test_command_targets_are_stored_normalised(self):
        """A target that is not already in normalised form can never match."""
        for entry in hr.RETIRED_REGISTRATIONS:
            if entry.kind != hr.KIND_COMMAND:
                continue
            with self.subTest(target=entry.target[:48]):
                self.assertEqual(hr.normalize_command(entry.target), entry.target)

    def test_every_entry_names_its_release_and_replacement(self):
        for entry in hr.RETIRED_REGISTRATIONS:
            with self.subTest(target=entry.target[:48]):
                self.assertTrue(entry.retired_in.startswith("v0."), entry.retired_in)
                self.assertTrue(entry.reason.strip())
                # `audit_replacement` is what the audit row prints; it is never
                # empty prose even when the capability itself went away.
                self.assertTrue(entry.audit_replacement.strip())

    def test_identity_is_required_not_optional(self):
        """A caller that forgets the identity must not silently lose half the
        table — so the parameter is keyword-only with NO default."""
        with self.assertRaises(TypeError):
            hr.match_retired_registration("Stop", COST_SH)  # type: ignore[call-arg]


class LeaveAloneTests(unittest.TestCase):
    """The conservative half. Each of these MUST survive untouched."""

    def test_retired_path_inside_a_larger_user_command_is_kept(self):
        user_cmd = (
            "bash my-wrapper.sh --sync-with .claude/scripts/sync_knowledge_graph.py "
            "&& echo done"
        )
        self.assertIsNone(
            hr.match_retired_registration(
                "PostToolUse", user_cmd,
                hook_identity=project_init._vco_hook_script_identity(user_cmd),
            )
        )

    def test_user_hook_referencing_the_retired_script_as_an_argument_is_kept(self):
        user_cmd = "bash my-wrapper.sh --target .claude/hooks/cost-tracker.sh"
        # The anchored walk resolves the INVOKED script (my-wrapper.sh), so the
        # identity is not the retired basename and nothing matches.
        self.assertIsNone(
            hr.match_retired_registration(
                "Stop", user_cmd,
                hook_identity=project_init._vco_hook_script_identity(user_cmd),
            )
        )

    def test_a_users_own_sync_invocation_with_their_own_interpreter_is_kept(self):
        """Same script, a DIFFERENT (working) interpreter — that command syncs
        successfully and is the user's. Whole-command equality is what keeps it."""
        user_cmd = (
            '/opt/vco/.venv/bin/python .claude/scripts/sync_knowledge_graph.py "$F"'
        )
        self.assertIsNone(
            hr.match_retired_registration(
                "PostToolUse", user_cmd,
                hook_identity=project_init._vco_hook_script_identity(user_cmd),
            )
        )

    def test_right_command_wrong_event_is_kept(self):
        self.assertIsNone(
            hr.match_retired_registration("PreToolUse", SYNC_SH_BARE, hook_identity=None)
        )
        self.assertIsNone(
            hr.match_retired_registration(
                "PostToolUse", COST_SH, hook_identity="cost-tracker.sh",
            )
        )

    def test_merge_preserves_a_users_own_stop_hook(self):
        user_hooks = {
            "Stop": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "bash .claude/hooks/my-own.sh"},
                ]},
            ],
        }
        merged = project_init._merge_hooks_for_bundle(user_hooks, {})
        self.assertEqual(_cmds(merged, "Stop"), ["bash .claude/hooks/my-own.sh"])


class MergeRemovalTests(unittest.TestCase):
    """The ACT, at the merge seam."""

    def test_retired_inline_sync_is_removed_and_recorded(self):
        user_hooks = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": SYNC_SH_BARE},
                    {"type": "command", "command": "bash .claude/hooks/post-file-edit.sh"},
                ]},
            ],
        }
        removed: list = []
        merged = project_init._merge_hooks_for_bundle(
            user_hooks, {}, retired_removed=removed,
        )
        self.assertEqual(
            _cmds(merged, "PostToolUse"), ["bash .claude/hooks/post-file-edit.sh"],
        )
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["event"], "PostToolUse")
        self.assertEqual(removed[0]["command"], SYNC_SH_BARE)
        self.assertEqual(removed[0]["retirement"].retired_in, "v0.2.73")

    def test_group_left_empty_is_pruned_and_the_event_key_dropped(self):
        user_hooks = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": SYNC_INLINE_PY},
                ]},
            ],
        }
        merged = project_init._merge_hooks_for_bundle(user_hooks, {})
        self.assertNotIn(
            "PostToolUse", merged,
            "an event whose every group emptied must lose its key, not linger "
            "as an empty array",
        )

    def test_template_rewires_the_event_it_emptied(self):
        """Dropping the event key must not lose the CURRENT wiring: the merge
        re-creates the event from the template in the same pass."""
        user_hooks = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": SYNC_INLINE_PY},
                ]},
            ],
        }
        template = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": "bash .claude/hooks/post-file-edit.sh"},
                ]},
            ],
        }
        merged = project_init._merge_hooks_for_bundle(user_hooks, template)
        self.assertEqual(
            _cmds(merged, "PostToolUse"), ["bash .claude/hooks/post-file-edit.sh"],
        )

    def test_cost_tracker_is_matched_through_any_invocation_form(self):
        for cmd in (
            COST_SH,
            "bash .claude/hooks/cost-tracker.sh",
            'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/cost-tracker.sh"',
            r"bash .claude\hooks\cost-tracker.sh",
        ):
            with self.subTest(cmd=cmd):
                user_hooks = {
                    "Stop": [
                        {"matcher": "", "hooks": [
                            {"type": "command", "command": cmd},
                            {"type": "command", "command": "bash .claude/hooks/notify-stop.sh"},
                        ]},
                    ],
                }
                merged = project_init._merge_hooks_for_bundle(user_hooks, {})
                self.assertEqual(
                    _cmds(merged, "Stop"), ["bash .claude/hooks/notify-stop.sh"],
                )

    def test_second_run_removes_nothing(self):
        user_hooks = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": SYNC_SH_GUARDED},
                    {"type": "command", "command": "bash .claude/hooks/post-file-edit.sh"},
                ]},
            ],
        }
        once = project_init._merge_hooks_for_bundle(user_hooks, {})
        removed: list = []
        twice = project_init._merge_hooks_for_bundle(once, {}, retired_removed=removed)
        self.assertEqual(twice, once)
        self.assertEqual(removed, [])

    def test_malformed_blocks_are_left_exactly_as_found(self):
        user_hooks = {
            "PostToolUse": "not-a-list",
            "Stop": [{"matcher": "", "hooks": "not-a-list"}],
        }
        merged = project_init._merge_hooks_for_bundle(dict(user_hooks), {})
        self.assertEqual(merged, user_hooks)


class BundleEngineDeliveryTests(unittest.TestCase):
    """Ruling R17: it reaches users through the ONE engine, on `--update`,
    against an install that is ALREADY damaged."""

    def setUp(self):
        from tests.test_install_bundle import _make_fake_orchestrator

        self.tmp = Path(tempfile.mkdtemp(prefix="vct-retire-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _settings(self) -> dict:
        return json.loads(
            (self.proj / ".claude" / "settings.json").read_text(encoding="utf-8")
        )

    def test_update_removes_the_registration_and_writes_an_audit_row(self):
        # 1. a normal install.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # 2. damage it the way a real 2026-05-era project is damaged.
        settings = self._settings()
        settings.setdefault("hooks", {})["PostToolUse"] = [
            {"matcher": "Edit|Write", "hooks": [
                {"type": "command", "command": SYNC_SH_BARE},
                {"type": "command", "command": "bash .claude/hooks/post-file-edit.sh"},
            ]},
        ]
        (self.proj / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8",
        )

        # 3. the ordinary bundle update.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertEqual(result["errors"], [])

        cmds = _cmds(self._settings()["hooks"], "PostToolUse")
        self.assertNotIn(SYNC_SH_BARE, cmds)
        self.assertIn("bash .claude/hooks/post-file-edit.sh", cmds)

        # 4. the removal is on the record, naming the replacement.
        self.assertIn("retired_hook_registrations", result)
        trail = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(trail.is_file(), "no auto-resolution trail was written")
        rows = [
            json.loads(line)
            for line in trail.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        mine = [r for r in rows if r.get("action") == "removed_retired_hook_registration"]
        self.assertEqual(len(mine), 1, rows)
        self.assertIn("sync_knowledge_graph.py", mine[0]["detail"])
        self.assertIn("post-file-edit.sh", mine[0]["detail"])

    def test_a_clean_project_gets_no_row_and_no_rewrite(self):
        """The leave-alone case at the DELIVERY level: an undamaged project's
        update must not report a removal it did not make."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertNotIn("retired_hook_registrations", result)
        trail = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        if trail.is_file():
            rows = [
                json.loads(line)
                for line in trail.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertFalse(
                [r for r in rows if r.get("action") == "removed_retired_hook_registration"]
            )


if __name__ == "__main__":
    unittest.main()
