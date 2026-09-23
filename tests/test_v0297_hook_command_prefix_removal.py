# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the settings-level ``[ -n "$VCT_DISABLE_HOOKS" ] || `` prefix is gone.

Owner ruling: "cleanup dead/replaced code and make sure everything migrated
to new implementation". The prefix was a REDUNDANT second copy of the
``VCT_DISABLE_HOOKS`` opt-out: the real mechanism is the in-script guard
(every ``templates/hooks/*.{sh,ps1}`` exits 0 on the variable, enforced by
``tests/test_hooks_disable_guard.py``). Claude Code shows the full command
string as the hook's label, so users saw shell noise like
``PostCompact [[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/post-compact.sh]
completed successfully`` on every fire.

What these tests pin:

  1. TEMPLATE — the shipped linux template carries no settings-level guard
     prefix. (The windows template never did; the v0.2.73 D-4 parity test
     already pins that side.)
  2. MIGRATION — a bundle update REWRITES an existing VCO-shipped prefixed
     entry to the new form IN PLACE: same position, same matcher, same
     timeout/async, no duplicate (prefixed + unprefixed both present would
     be a double-firing defect). The mechanism is the v0.2.70
     supersede-not-stack pass in ``vco_lib.settings_merge.merge_hooks_block``
     — the guard prefix is transparent to
     ``vco_hook_script_identity`` because ``||`` is a command separator in
     the tokenizer — so no NEW merge code was needed for this change.
  3. LEAVE-ALONE — a user's OWN hook command, even one that itself embeds
     ``VCT_DISABLE_HOOKS``, is preserved byte-for-byte.
  4. RE-ENABLE — a parked (launcher-disabled) prefixed entry restores as it
     was parked (the prefixed command still functions — the in-script guard
     makes the prefix harmless) and the NEXT bundle update heals it to the
     new form.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINUX_TEMPLATE = REPO_ROOT / "templates" / "settings.json.linux.template"

from vco_lib import settings_merge  # noqa: E402
from vco_lib.hooks_settings import (  # noqa: E402
    SettingsDoc,
    insert_hook,
    remove_hook,
)
from vco_lib.hook_retirements import vco_hook_script_identity  # noqa: E402

#: The retired settings-level guard, verbatim (JSON-decoded form).
OLD_GUARD = '[ -n "$VCT_DISABLE_HOOKS" ] || '


def _load_template() -> dict:
    return json.loads(LINUX_TEMPLATE.read_text(encoding="utf-8"))


def _prefixed(template: dict) -> dict:
    """Re-create a pre-v0.2.97 install's hooks block: every VCO script
    command carries the old settings-level guard prefix."""
    out: dict = {}
    for event, groups in template["hooks"].items():
        out[event] = []
        for group in groups:
            new_group = dict(group)
            new_group["hooks"] = [
                {**h, "command": OLD_GUARD + h["command"]}
                if isinstance(h, dict) and isinstance(h.get("command"), str)
                else h
                for h in group.get("hooks", [])
            ]
            out[event].append(new_group)
    return out


def _commands_by_event(hooks: dict) -> dict[str, list[str]]:
    return {
        event: [h["command"] for g in groups for h in g.get("hooks", [])
                if isinstance(h, dict) and isinstance(h.get("command"), str)]
        for event, groups in hooks.items()
    }


class TemplateHasNoPrefixTests(unittest.TestCase):
    """The shipped linux template must not carry the settings-level guard."""

    def test_no_linux_command_carries_the_guard_prefix(self) -> None:
        data = _load_template()
        offenders = [
            cmd
            for cmds in _commands_by_event(data["hooks"]).values()
            for cmd in cmds
            if "VCT_DISABLE_HOOKS" in cmd
        ]
        self.assertEqual(
            offenders,
            [],
            "The settings-level '[ -n \"$VCT_DISABLE_HOOKS\" ] || ' prefix was "
            "removed in v0.2.97: the in-script guard (tests/test_hooks_disable_"
            "guard.py) is the one mechanism. Offending command(s): "
            f"{offenders!r}",
        )

    def test_every_linux_command_still_resolves_a_hook_identity(self) -> None:
        """Post-strip sanity: every shipped command still INVOKE a hook
        script the merge can recognise (a mangled strip would break both the
        supersede pass and the launcher's Hooks tab)."""
        data = _load_template()
        for event, cmds in _commands_by_event(data["hooks"]).items():
            for cmd in cmds:
                self.assertIsNotNone(
                    vco_hook_script_identity(cmd),
                    f"{event}: command no longer invokes a .claude/hooks/ "
                    f"script after the prefix strip: {cmd!r}",
                )


class PrefixedInstallMigrationTests(unittest.TestCase):
    """A pre-v0.2.97 settings.json, merged with the current template."""

    def setUp(self) -> None:
        self.template = _load_template()
        # The "existing install": the current template's hooks block with
        # every command prefixed (the pre-v0.2.97 shipped shape), plus a
        # user's own hook and a user top-level key the merge must preserve.
        self.user = {
            "model": "opus",
            "hooks": _prefixed(self.template),
        }
        self.user["hooks"]["Stop"][0]["hooks"].append(
            {
                "type": "command",
                "command": '[ -n "$VCT_DISABLE_HOOKS" ] || bash ./my-scripts/own-teardown.sh',
                "timeout": 4,
            }
        )
        self.merged = settings_merge.smart_merge_settings(
            copy.deepcopy(self.user), self.template
        )

    def test_every_prefixed_vco_entry_is_rewritten_to_the_new_form(self) -> None:
        """The user-visible outcome: after ONE bundle update, a pre-v0.2.97
        install's settings.json carries no settings-level guard on any
        VCO-shipped command. (The user's OWN hooks may keep theirs.)"""
        for event, m_cmds in _commands_by_event(self.merged["hooks"]).items():
            for cmd in m_cmds:
                if vco_hook_script_identity(cmd) is None:
                    continue  # the user's own hook — checked separately
                self.assertNotIn(
                    "VCT_DISABLE_HOOKS", cmd,
                    f"{event}: a VCO-shipped command still carries the "
                    f"settings-level guard after the merge: {cmd!r}",
                )

    def test_merged_vco_commands_equal_the_template_commands(self) -> None:
        """And the healed commands are EXACTLY the template's — not a
        hand-rolled strip: whatever the template ships is what runs."""
        for event, t_cmds in _commands_by_event(self.template["hooks"]).items():
            m_cmds = _commands_by_event(self.merged["hooks"]).get(event, [])
            for cmd in t_cmds:
                self.assertIn(
                    cmd, m_cmds,
                    f"{event}: the shipped command {cmd!r} must be present "
                    "after the merge",
                )

    def test_rewrite_happens_in_place_preserving_shape_and_position(self) -> None:
        """Position, matcher, timeout and async survive the rewrite: the
        supersede pass replaces the command STRING inside the existing
        item, never the item's place in the array."""
        self.assertEqual(
            [e for e in self.merged["hooks"]],
            [e for e in self.user["hooks"]],
            "event order changed",
        )
        for event in self.user["hooks"]:
            self.assertEqual(
                len(self.merged["hooks"][event]),
                len(self.user["hooks"][event]),
                f"{event}: group count changed",
            )
            for u_group, m_group in zip(
                self.user["hooks"][event], self.merged["hooks"][event]
            ):
                self.assertEqual(
                    u_group.get("matcher"), m_group.get("matcher"),
                    f"{event}: matcher changed",
                )
                self.assertEqual(
                    len(u_group["hooks"]), len(m_group["hooks"]),
                    f"{event}: inner-hook count changed (a rewrite must not "
                    "add or drop entries)",
                )
                for u_hook, m_hook in zip(u_group["hooks"], m_group["hooks"]):
                    self.assertEqual(u_hook.get("timeout"), m_hook.get("timeout"))
                    self.assertEqual(u_hook.get("async"), m_hook.get("async"))

    def test_no_duplicate_invocation_per_hook(self) -> None:
        """prefixed + unprefixed both present would double-fire the hook.

        Keyed by (event, group): the template deliberately registers some
        scripts under SEVERAL matchers (pre-diagram-path-validation.sh on
        ``Write|Edit`` AND on the diagram MCP tools; kg-update-nudge.sh on
        several events) — same identity in DIFFERENT groups is by design,
        twice in the SAME group is the defect."""
        for event, groups in self.merged["hooks"].items():
            for group in groups:
                cmds = [
                    h["command"] for h in group.get("hooks", [])
                    if isinstance(h, dict) and isinstance(h.get("command"), str)
                ]
                idents = [
                    i for i in (vco_hook_script_identity(c) for c in cmds)
                    if i is not None
                ]
                self.assertEqual(
                    len(idents), len(set(idents)),
                    f"{event}/{group.get('matcher')!r}: a hook identity is "
                    f"registered more than once in the same group: {idents!r}",
                )

    def test_user_hook_and_user_keys_are_untouched(self) -> None:
        self.assertEqual(self.merged["model"], "opus")
        self.assertIn(
            '[ -n "$VCT_DISABLE_HOOKS" ] || bash ./my-scripts/own-teardown.sh',
            _commands_by_event(self.merged["hooks"])["Stop"],
            "the user's OWN hook (script outside .claude/hooks/) must be "
            "preserved byte-for-byte even though it embeds VCT_DISABLE_HOOKS",
        )

    def test_second_merge_changes_nothing(self) -> None:
        """Idempotence: the healed file is a fixed point of the merge."""
        again = settings_merge.smart_merge_settings(
            copy.deepcopy(self.merged), self.template
        )
        self.assertEqual(again, self.merged)


class ReenabledParkedEntryTests(unittest.TestCase):
    """The launcher disable/enable cycle across the prefix removal."""

    def _doc_from(self, hooks: dict) -> SettingsDoc:
        return SettingsDoc(
            path=Path("/tmp/test-settings.json"),
            data={"hooks": copy.deepcopy(hooks)},
            indent=2,
            trailing_newline=True,
        )

    def test_reenable_restores_parked_bytes_and_next_update_heals(self) -> None:
        template = _load_template()
        old_form = OLD_GUARD + "bash .claude/hooks/notify-stop.sh"
        new_form = "bash .claude/hooks/notify-stop.sh"

        # 1. A pre-v0.2.97 install disables the hook from the launcher: the
        #    entry is REMOVED from settings.json and parked in launcher.db
        #    (carrying the old prefixed bytes).
        doc = self._doc_from({"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": old_form, "timeout": 10},
        ]}]})
        parked = remove_hook(doc, "Stop", "", old_form)
        self.assertEqual(parked["item"]["command"], old_form)

        # While the hook is disabled it is absent from settings.json (the
        # removal cascade deleted the emptied `hooks` key entirely — the
        # documented disable shape), and the PARKED bytes in launcher.db are
        # untouched by this change: nothing in the prefix removal can reach
        # them, and the entry below proves the restore still works.
        self.assertNotIn("hooks", doc.data)

        # 2. The user re-enables: the parked bytes go back verbatim. The
        #    prefixed command still FUNCTIONS (the in-script guard makes the
        #    prefix redundant, not harmful) — this is the documented
        #    behaviour, not a defect.
        self.assertTrue(insert_hook(doc, parked))
        self.assertEqual(
            doc.data["hooks"]["Stop"][0]["hooks"][0]["command"], old_form
        )

        # 3. The next bundle update supersedes the restored prefixed form
        #    to the current one — exactly one entry, in place.
        healed = settings_merge.merge_hooks_block(
            copy.deepcopy(doc.data["hooks"]), template["hooks"]
        )
        cmds = _commands_by_event(healed)["Stop"]
        self.assertEqual(cmds.count(new_form), 1)
        self.assertEqual(cmds.count(old_form), 0)
        # timeout preserved through both hops.
        item = [h for g in healed["Stop"] for h in g["hooks"]
                if h.get("command") == new_form][0]
        self.assertEqual(item.get("timeout"), 10)


if __name__ == "__main__":
    unittest.main()
