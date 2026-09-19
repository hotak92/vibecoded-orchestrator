# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The contract of `vco_lib.settings_merge` — the settings.json merge
ALGORITHM, extracted from `project_init` in v0.2.95.

The extraction was forced by the line-count ratchet on `project_init.py`
(`tests/test_v0292_wp4_delivery_and_promises.py`), and the BEHAVIOUR it moved
is already covered where it always was: `tests/test_install_bundle.py`
(supersede / append / idempotence, 20+ cases), `tests/test_v0295_
hook_retirements.py` (the scrub), `tests/test_v0291_hooks_settings.py` and
`tests/test_install_hooks.py` (through the bundle engine). Those files still
call the functions through `project_init`, which is the point of the last
test here.

What THIS file adds is the module's own contract — the four things a caller
may rely on that no behaviour test states outright:

  1. USER-WINS is total. The template may add a key or merge deeper; it may
     never replace a value the user set. A bundle update writes into a file
     the user edits, so this is the property that makes the write safe.
  2. The inputs are not mutated. The caller (`install_project_bundle`)
     compares `merged == existing` to decide whether to write at all — an
     in-place merge would make that comparison always true and the update a
     silent no-op.
  3. `retired_removed` survives the RECURSION, not just the top level. The
     source says a threading hole "would be invisible until the day it
     isn't"; this is the test that makes it visible today.
  4. The dependency runs one way: `project_init` -> `settings_merge`, never
     back. That direction is the whole justification for the split, and it is
     the kind of thing a later convenience import silently reverses.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init, settings_merge  # noqa: E402

#: A registration `vco_lib.hook_retirements` declares dead (cost telemetry,
#: removed in v0.2.95). Same literal as `tests/test_v0295_hook_retirements_
#: cli.py` uses — if the table ever drops it, both files go red together,
#: which is the correct coupling.
RETIRED = ("Stop", "bash .claude/hooks/cost-tracker.sh")

#: A shipped hook that is very much alive, in its CURRENT form and in a stale
#: pre-v0.2.70 backslash form with the same script identity.
LIVE_CMD = "bash .claude/hooks/notify-stop.sh"
LIVE_CMD_STALE = "bash .claude\\hooks\\notify-stop.sh"


def _block(event: str, command: str) -> dict:
    return {event: [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}


# ---------------------------------------------------------------------------
# 1. user-wins
# ---------------------------------------------------------------------------


def test_a_key_the_user_does_not_have_is_added_from_the_template():
    merged = settings_merge.smart_merge_settings({"a": 1}, {"b": 2})
    assert merged == {"a": 1, "b": 2}


def test_a_scalar_the_user_set_is_never_replaced_by_the_template():
    """The property that makes writing into a user-edited file safe."""
    merged = settings_merge.smart_merge_settings(
        {"model": "opus", "env": {"KG_COLLECTION": "Mine"}},
        {"model": "sonnet", "env": {"KG_COLLECTION": "Default"}},
    )
    assert merged["model"] == "opus"
    assert merged["env"]["KG_COLLECTION"] == "Mine"


def test_a_dict_on_both_sides_merges_deeper_and_the_user_still_wins_at_the_leaf():
    merged = settings_merge.smart_merge_settings(
        {"env": {"A": "user"}},
        {"env": {"A": "template", "B": "template"}},
    )
    assert merged["env"] == {"A": "user", "B": "template"}


def test_a_user_scalar_facing_a_template_dict_is_left_alone():
    """Type mismatch is not a licence to overwrite — 'user wins' has no
    exception for 'the user's value is the wrong shape'."""
    merged = settings_merge.smart_merge_settings({"hooks": "off"}, {"hooks": {"Stop": []}})
    assert merged["hooks"] == "off"


# ---------------------------------------------------------------------------
# 2. no mutation
# ---------------------------------------------------------------------------


def test_neither_input_is_mutated_by_the_settings_merge():
    user = {"env": {"A": "user"}, "hooks": _block(*RETIRED)}
    template = {"env": {"B": "t"}, "hooks": _block("Stop", LIVE_CMD)}
    user_before, template_before = repr(user), repr(template)
    settings_merge.smart_merge_settings(user, template)
    assert repr(user) == user_before, "the caller compares merged == existing"
    assert repr(template) == template_before


def test_neither_input_is_mutated_by_the_hooks_merge():
    user = _block("Stop", LIVE_CMD_STALE)
    template = _block("Stop", LIVE_CMD)
    user_before, template_before = repr(user), repr(template)
    settings_merge.merge_hooks_block(user, template)
    assert repr(user) == user_before
    assert repr(template) == template_before


# ---------------------------------------------------------------------------
# 3. the hooks special-case, and the accumulator that rides through it
# ---------------------------------------------------------------------------


def test_the_hooks_key_routes_to_the_hooks_merge_not_the_generic_recursion():
    """Proof by a supersede: the generic path cannot produce this answer.

    A hooks block is `{event: [entry, ...]}` — the values are LISTS, and the
    generic recursion only descends into dicts, so under it the user's stale
    command would stand. Seeing it rewritten to the template's current form is
    the special case firing.
    """
    merged = settings_merge.smart_merge_settings(
        {"hooks": _block("Stop", LIVE_CMD_STALE)},
        {"hooks": _block("Stop", LIVE_CMD)},
    )
    commands = [
        h["command"]
        for entry in merged["hooks"]["Stop"]
        for h in entry["hooks"]
    ]
    assert commands == [LIVE_CMD], "the stale form must be superseded, not stacked"


def test_a_retired_registration_is_reported_through_the_top_level_hooks_block():
    event, command = RETIRED
    removed: list = []
    merged = settings_merge.smart_merge_settings(
        {"hooks": _block(event, command)}, {"hooks": {}}, retired_removed=removed,
    )
    assert merged["hooks"] == {}, "the dead registration is gone"
    assert [r["command"] for r in removed] == [command]
    assert removed[0]["event"] == event
    assert removed[0]["retirement"], "a removal must carry its table entry"


def test_the_accumulator_survives_the_recursion_not_only_the_top_level():
    """The threading hole the source calls "invisible until the day it isn't".

    The hooks block is top-level in today's settings.json, so a caller that
    threaded `retired_removed` into the hooks branch and forgot the recursive
    branch would pass every other test in the suite. Nest it one level and the
    hole opens: the removal still happens (the scrub is unconditional) but the
    caller is never told, so no audit row is written for a registration VCO
    just deleted from the user's file.
    """
    event, command = RETIRED
    removed: list = []
    merged = settings_merge.smart_merge_settings(
        {"nested": {"hooks": _block(event, command)}},
        {"nested": {"hooks": {}}},
        retired_removed=removed,
    )
    assert merged["nested"]["hooks"] == {}
    assert [r["command"] for r in removed] == [command], (
        "a removal under a nested hooks block must still reach the caller"
    )


def test_a_user_command_vco_cannot_identify_is_preserved_byte_for_byte():
    """The conservative posture, stated as this module's own contract: when in
    doubt, leave it. A wrong replace destroys a user's hook."""
    mine = "bash ./scripts/my-own-hook.sh --target .claude/hooks/notify-stop.sh"
    removed: list = []
    merged = settings_merge.merge_hooks_block(
        _block("Stop", mine), _block("Stop", LIVE_CMD), retired_removed=removed,
    )
    commands = [h["command"] for entry in merged["Stop"] for h in entry["hooks"]]
    assert mine in commands
    assert removed == []


# ---------------------------------------------------------------------------
# 4. the split holds: one implementation, one direction
# ---------------------------------------------------------------------------


def test_project_inits_private_names_are_aliases_not_a_second_copy():
    """`project_init._smart_merge_for_bundle` / `._merge_hooks_for_bundle` are
    the names ~30 existing call-sites use. They must be THE objects from this
    module — identity, not two authors agreeing — or the extraction has merely
    added a copy to keep in step."""
    assert project_init._smart_merge_for_bundle is settings_merge.smart_merge_settings
    assert project_init._merge_hooks_for_bundle is settings_merge.merge_hooks_block


def test_the_module_does_not_import_project_init():
    """One direction only. `settings_merge` is the pure decision `project_init`
    delegates to; an import back would re-couple them and (being a cycle)
    would be discovered as an ImportError in the field rather than here.

    Asked in a CHILD interpreter because this test process has already
    imported `project_init` — in-process `sys.modules` cannot answer it.

    BOTH pins are load-bearing and neither is redundant: `child_env()` beats
    a stale `vco_lib` on the child's `PYTHONPATH` / `$VCT_ORCHESTRATOR_ROOT`,
    and `cwd` beats `sys.path[0]`, which for `python -c` is the CURRENT
    DIRECTORY and outranks both. Measured while writing this test: run from a
    worktree of the dogfood fork, the child imported THAT tree's `vco_lib`
    and reported a module this one has as missing.
    """
    code = (
        "import sys\n"
        "import vco_lib.settings_merge\n"
        "print('vco_lib.project_init' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "vco_lib.settings_merge must not pull in vco_lib.project_init"
    )
