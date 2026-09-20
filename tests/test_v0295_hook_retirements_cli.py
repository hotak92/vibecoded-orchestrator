# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 F7 — `python -m vco_lib.hook_retirements match --json`.

The launcher's Hooks tab holds PARKED entries: the settings.json bytes of
hooks the user disabled, kept in `launcher.db` so a re-enable restores them
exactly. The bundle scrub that retires dead registrations walks settings.json
— where, by definition, a parked entry is NOT. So a hook disabled BEFORE its
retirement keeps a row labelled "Disabled (restorable)" for a script the same
update deleted.

The REFUSAL half of that is already closed (`hooks_settings.insert_hook`
raises `hook_retired`, so every restore path refuses through one decision).
This CLI is the EAGER half: the launcher asks, once per tab load, which of its
parked rows are dead, and releases those bytes before the user clicks
anything.

What is pinned here is the CONTRACT the Rust caller depends on — the shape of
the answer, the exit codes, and (twice over) the conservatism of the matcher:
a near-miss here deletes a user's own hook, so "left alone" is tested as
deliberately as "pruned".

Hermetic: `_match_pairs` is pure, and the one subprocess leg runs
`sys.executable` against this checkout — pinned there by `child_env()`, not
merely by `cwd` — with piped stdin. Nothing touches a project, a DB, or a
backend.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.common.child_env import child_env
from vco_lib import hook_retirements as hr

REPO = Path(__file__).resolve().parents[1]

#: A registration the table declares dead by SCRIPT identity (cost telemetry,
#: removed in v0.2.95 — the file stops shipping, so the invocation is a
#: dangling reference on every session end).
RETIRED_SCRIPT = ("Stop", "bash .claude/hooks/cost-tracker.sh")

#: A registration the table declares dead by WHOLE-COMMAND equality (one of
#: the two inline `sync_knowledge_graph.py` forms VCO itself shipped).
RETIRED_COMMAND = (
    "PostToolUse",
    'python .claude/scripts/sync_knowledge_graph.py '
    '"$CLAUDE_TOOL_ARG_FILE_PATH" 2>&1 || true',
)

#: A shipped hook that is very much alive.
LIVE = ("Stop", "bash .claude/hooks/notify-stop.sh")


def _run_cli(stdin_body: str) -> tuple[int, str, str]:
    """The CLI as the launcher invokes it: `-m`, cwd = the clone root.

    ``env=child_env()`` pins the checkout's import roots (and
    ``$VCT_ORCHESTRATOR_ROOT``) for the child. Without it the child answers
    from whichever ``vco_lib`` its own ``sys.path`` finds first — on this
    repo's dev box a non-editable site-packages copy, or the dogfood fork
    named by the developer's ``$VCT_ORCHESTRATOR_ROOT`` — and this leg would
    be asserting the retirement TABLE of a tree that is not this one. That is
    the quiet failure: the table would still parse, the shape would still
    match, and only the retirement CONTENT would be wrong.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.hook_retirements", "match", "--json"],
        input=stdin_body,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=child_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


class MatchVerdictTests(unittest.TestCase):
    """The decision, exercised in-process (no interpreter start per case)."""

    def test_a_retired_hook_script_is_reported_retired_with_its_release(self) -> None:
        event, command = RETIRED_SCRIPT
        [row] = hr._match_pairs([{"event": event, "command": command}])
        self.assertTrue(row["retired"])
        self.assertEqual(row["event"], event)
        self.assertEqual(row["command"], command)
        self.assertEqual(row["retired_in"], "v0.2.95")
        self.assertTrue(row["reason"], "a removal must say why")
        self.assertEqual(
            row["replacement"],
            "nothing (the capability was removed)",
            "a retirement with no successor must say so in words, not with an "
            "empty string the caller has to interpret",
        )

    def test_a_retired_inline_command_matches_on_whole_command_equality(self) -> None:
        event, command = RETIRED_COMMAND
        [row] = hr._match_pairs([{"event": event, "command": command}])
        self.assertTrue(row["retired"])
        self.assertIn("post-file-edit", row["replacement"])

    def test_a_live_hook_is_left_alone(self) -> None:
        """The leave-alone half. A parked row for THIS hook is the user's only
        copy of it; dropping it would destroy user state."""
        event, command = LIVE
        [row] = hr._match_pairs([{"event": event, "command": command}])
        self.assertFalse(row["retired"])
        self.assertEqual(row["retired_in"], "")
        self.assertEqual(row["replacement"], "")
        self.assertEqual(row["reason"], "")

    def test_a_retired_path_at_an_ARGUMENT_position_is_not_a_match(self) -> None:
        """The anchored-walk guarantee, restated at this seam: a user's own
        wrapper that NAMES a retired hook is not a retired registration."""
        [row] = hr._match_pairs([{
            "event": "Stop",
            "command": "bash my-wrapper.sh --target .claude/hooks/cost-tracker.sh",
        }])
        self.assertFalse(
            row["retired"],
            "the user's wrapper merely mentions the path — deleting it would "
            "destroy a hook VCO never wrote",
        )

    def test_the_event_scopes_the_match(self) -> None:
        """A retired `Stop` command is not retired under `PreToolUse`."""
        _, command = RETIRED_SCRIPT
        [row] = hr._match_pairs([{"event": "PreToolUse", "command": command}])
        self.assertFalse(row["retired"])

    def test_every_pair_gets_a_row_in_input_order(self) -> None:
        """The caller zips the answer against what it sent; a filtered answer
        would make it re-derive identity on its side."""
        pairs = [
            {"event": LIVE[0], "command": LIVE[1]},
            {"event": RETIRED_SCRIPT[0], "command": RETIRED_SCRIPT[1]},
            {"event": LIVE[0], "command": LIVE[1]},
        ]
        rows = hr._match_pairs(pairs)
        self.assertEqual([r["retired"] for r in rows], [False, True, False])
        self.assertEqual(
            [(r["event"], r["command"]) for r in rows],
            [(p["event"], p["command"]) for p in pairs],
        )

    def test_empty_pairs_is_a_valid_request(self) -> None:
        self.assertEqual(hr._match_pairs([]), [])

    def test_a_malformed_batch_raises_rather_than_answering_not_retired(self) -> None:
        """"Nothing is retired" and "I could not tell" must not be the same
        answer: the first leaves dead rows parked forever while looking like a
        success."""
        for bad in ("not-a-list", {"event": "Stop"}, [["Stop", "cmd"]], [{"event": 1}]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hr._match_pairs(bad)  # type: ignore[arg-type]


class CliContractTests(unittest.TestCase):
    """Exit codes + stdout shape, through a real interpreter."""

    def test_a_batch_answers_ok_with_one_row_per_pair(self) -> None:
        body = json.dumps({"pairs": [
            {"event": RETIRED_SCRIPT[0], "command": RETIRED_SCRIPT[1]},
            {"event": LIVE[0], "command": LIVE[1]},
        ]})
        code, out, err = _run_cli(body)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIs(payload["ok"], True)
        self.assertEqual([m["retired"] for m in payload["matches"]], [True, False])

    def test_empty_pairs_answers_ok_with_no_matches(self) -> None:
        code, out, err = _run_cli(json.dumps({"pairs": []}))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"ok": True, "matches": []})

    def test_malformed_stdin_refuses_with_a_nonzero_exit(self) -> None:
        for body in ("", "not json at all", "[]", '{"pairs": 3}', '{"pairs":[7]}'):
            code, out, _ = _run_cli(body)
            self.assertNotEqual(code, 0, f"{body!r} must not exit 0")
            payload = json.loads(out)
            self.assertIs(payload["ok"], False, body)
            self.assertEqual(payload["code"], "bad_request", body)
            self.assertTrue(payload["error"], "a refusal must say what is wrong")

    def test_stdout_carries_exactly_one_json_object_on_every_path(self) -> None:
        """The house machine contract: a caller parses stdout whole."""
        for body in (json.dumps({"pairs": []}), "not json"):
            _, out, _ = _run_cli(body)
            self.assertEqual(
                len([ln for ln in out.splitlines() if ln.strip()]),
                1,
                f"stdout for {body!r} is not a single JSON line: {out!r}",
            )

    def test_the_verdicts_are_the_same_in_process_and_through_the_cli(self) -> None:
        """One matcher, two entry points — the CLI must not be a second
        opinion about what a retired registration is."""
        pairs = [
            {"event": RETIRED_SCRIPT[0], "command": RETIRED_SCRIPT[1]},
            {"event": RETIRED_COMMAND[0], "command": RETIRED_COMMAND[1]},
            {"event": LIVE[0], "command": LIVE[1]},
        ]
        _, out, _ = _run_cli(json.dumps({"pairs": pairs}))
        self.assertEqual(json.loads(out)["matches"], hr._match_pairs(pairs))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
