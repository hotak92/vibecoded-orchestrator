# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 decision #26 — the update commands' single-flight guard is WIRED.

SCOPE (widened v0.2.95, deliberately): this file gates the WIRING FACTS of the
orchestrator-update surfaces — facts about which guard/variant each
``#[command]`` reaches for, which no unit test can check because every one of
those commands takes an ``AppHandle`` / ``Window`` / ``State<'_, Db>``. Three
such facts live here now: the single-flight claim (the original), the
``ArtefactSource`` the already-up-to-date branch passes (ship-gate MAJOR-2),
and the launcher.db guard around ``install.py`` (ship-gate MINOR-8). They share
one scanner and one failure mode, which is why they share a file; anything
whose BEHAVIOUR is observable belongs in a Rust test instead, not here.

The guard's *behaviour* is unit-tested in Rust
(``commands/single_flight.rs::tests``: second concurrent claim refused,
sequential re-run allowed, claim released on panic, keys isolated), and since
v0.2.95 the claim's EFFECT on a destructive act is too
(``installer.rs``'s ``the_claim_refuses_the_abort_command_and_the_merge_
survives_the_refusal`` drives the real abort command against a real conflicted
git repo and asserts ``MERGE_HEAD`` survives the refusal).

What neither can reach is the CALL SITE of the other eleven surfaces: they are
``#[command] async fn``s taking ``AppHandle`` / ``Window`` / ``State<'_, Db>``,
none of which a unit test can construct. A guard that is perfect and
unreferenced is exactly the shape this cycle keeps finding (a control writing a
table nobody reads), so the wiring gets its own gate.

This is a SOURCE-TEXT gate, and those fail toward green
(``knowledge/concepts/source-text-gates-fail-toward-green-2026-08-27.md``).
Four of that note's rules are applied here:

1. **Match over CODE only.** The marker is searched in text whose comments
   and string literals have been blanked by the cross-line lexer from
   ``test_v0291_no_bare_prints_in_rust_crates`` — reused, not re-implemented.
   Without that, this very docstring, or the explanatory comment above each
   call site (both of which name ``begin_or_refuse``), would satisfy the
   locator while the actual call was deleted.
2. **A meta-test that proves the naive locator is fooled.** If the code-only
   filter silently stopped filtering, every assertion below would still pass.
   ``test_naive_locator_is_fooled_by_a_comment`` fails when that happens.
3. **Assert the scanner still SEES the constructs it polices.** The
   function-body extractor is checked against a known-present anchor and a
   known-absent one, so a signature rename cannot turn this file into a
   vacuous pass.
4. **CLOSED WORLD (v0.2.95 ship-gate MAJOR-3).** The clone-claim class below no
   longer enumerates the surfaces it checks. It DISCOVERS every ``#[command]``
   in the two files, classifies each by whether its body calls something that
   writes the orchestrator clone, and requires every such command to be
   accounted for — claimed, handed a claim, or exempt with a recorded reason.
   The previous version's docstring promised "every surface that WRITES the
   orchestrator clone takes the one shared claim" while asserting it of four,
   and six unclaimed surfaces sat behind that sentence for a full cycle. An
   enumeration cannot notice what it does not list; this can.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v0291_no_bare_prints_in_rust_crates import (  # noqa: E402
    _ST_CODE,
    _strip_line,
)

PROJECTS_V2 = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)
INSTALLER = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"
)
SINGLE_FLIGHT = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "single_flight.rs"
)
SELF_UPDATE = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "self_update.rs"
)

#: A column-0 Rust item start — the boundary that ends a function body for
#: the purposes of this scan. Independent of any brace counting, so a
#: mis-lexed brace cannot silently extend a body over the whole file.
_TOP_LEVEL_ITEM = re.compile(
    r"^(?:pub\b|fn\b|const\b|static\b|struct\b|enum\b|impl\b|trait\b"
    r"|type\b|mod\b|use\b|async\b|unsafe\b|extern\b|#\[)"
)

#: A `#[command]` fn signature at column 0, capturing the command name.
_COMMAND_FN = re.compile(r"^pub (?:async )?fn ([a-z_0-9]+)")


def code_only_lines(path: Path) -> list[str]:
    """The file's lines with comments and string literals blanked out."""
    state: tuple = (_ST_CODE,)
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped, state = _strip_line(line, state)
        out.append(stripped)
    return out


def function_body(lines: list[str], signature_prefix: str) -> list[str]:
    """Lines from the item declaring `signature_prefix` up to the next
    column-0 item. Returns [] when the signature is not found — callers
    assert non-emptiness so a rename fails loudly instead of vacuously.
    """
    start = None
    for i, line in enumerate(lines):
        if line.startswith(signature_prefix):
            start = i
            break
    if start is None:
        return []
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if _TOP_LEVEL_ITEM.match(line):
            break
        body.append(line)
    return body


def discover_commands(lines: list[str]) -> dict[str, str]:
    """Every ``#[command]`` in `lines`, as ``{command_name: body_text}``.

    Discovery, not enumeration: this is what makes the clone-claim gate
    closed-world. A new `#[command]` appears here the moment it is written.

    Operates on code-only lines, so a `#[command]` mentioned in a doc comment
    is not discovered as one.
    """
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        if line.strip() != "#[command]":
            continue
        # Skip any further attributes / blank lines before the signature.
        j = i + 1
        while j < len(lines) and (
            lines[j].lstrip().startswith("#[") or not lines[j].strip()
        ):
            j += 1
        if j >= len(lines):
            continue
        m = _COMMAND_FN.match(lines[j])
        if not m:
            continue
        body = [lines[j]]
        for line in lines[j + 1 :]:
            if _TOP_LEVEL_ITEM.match(line):
                break
            body.append(line)
        out[m.group(1)] = "\n".join(body)
    return out


#: Calls that WRITE the orchestrator clone — run `install.py --update` against
#: it, `git pull`/`reset --hard` it, rename its binaries aside, stop its hub,
#: abort a merge in it, or delete files from it.
#:
#: These are CALL tokens on purpose. An earlier draft keyed on argument
#: literals (``"--abort"``) and matched nothing: `code_only_lines` blanks
#: string literals, which is the same property that stops this gate passing on
#: prose. Anything that must be visible to the scan has to be code.
CLONE_WRITING_CALLS = (
    "run_install_py_update(",
    "install_py_command(",
    "run_post_pull_install_and_restart(",
    "stop_hub_and_rename_binaries_aside(",
    "prepare_and_pull_orchestrator_repo(",
    "stop_hub_then_hard_reset(",
    "resume_orchestrator_update_with_claim(",
    "update_orchestrator_with_claim(",
    "resolve_conflict_and_resume(",
    "abort_merge_or_rebase_unclaimed(",
    "resolve_collision_files(",
    "finish_apply_after_pull(",
    # Writes `state/install-manifest.json` in an orchestrator root. Included so
    # `update_orchestrator_at` — which file-copies a tree rather than calling
    # any of the helpers above — is DISCOVERED and then explicitly exempted for
    # acting on a different target, instead of being invisible to the scan and
    # exempt by accident. The distinction matters: invisible is how the
    # previous enumeration lost six surfaces.
    "refresh_install_manifest(",
)

#: The ONE entry point. Every surface claims through it rather than through
#: `begin_or_refuse(OP_UPDATE_ORCHESTRATOR_CLONE)`, so the callers cannot
#: disagree about the key.
ENTRY = "single_flight::begin_orchestrator_update_or_refuse"

#: Commands that receive an already-held claim instead of taking one, mapped to
#: the `pub(crate)` helper that takes it on their behalf. Recorded as a pair so
#: the exemption is not a bare "trust me": the helper is asserted to contain the
#: entry point, so a hand-down whose claim disappeared still fails.
HANDS_DOWN = {
    "resolve_untracked_collision_and_retry": (
        "pub(crate) async fn claim_then_resolve_collision_files(",
        INSTALLER,
    ),
}

#: Commands that touch `install.py` or a git tree but NOT the launcher's own
#: orchestrator clone, with the reason each is out of scope. A name may only
#: sit here with an argument; "it seemed unrelated" is how the previous
#: enumeration lost six surfaces.
EXEMPT = {
    "install_orchestrator": (
        "a FRESH install into a user-chosen `config.install_path`, gated by "
        "`validate_source_repo`. Not the launcher's own clone, and it runs "
        "before any clone exists to serialise against."
    ),
    "update_orchestrator_at": (
        "a DIFFERENT target — it file-copies this tree into ANOTHER "
        "orchestrator install. It holds `OP_UPDATE_ORCHESTRATOR_AT`, a "
        "deliberately separate key (see single_flight.rs's do-not-merge "
        "note): guarding one target must not block the other."
    ),
}


class SingleFlightWiring(unittest.TestCase):
    """Both guarded commands claim the flight, with their own key."""

    def test_update_all_projects_claims_the_flight(self) -> None:
        body = function_body(
            code_only_lines(PROJECTS_V2), "pub async fn update_all_projects("
        )
        self.assertTrue(
            body,
            "update_all_projects signature not found — the scan would pass "
            "vacuously; fix the prefix if the signature changed.",
        )
        text = "\n".join(body)
        self.assertIn(
            "single_flight::begin_or_refuse",
            text,
            "update_all_projects performs a real bundle install per project; "
            "it must refuse a second concurrent run (plan §F #26).",
        )
        self.assertIn("OP_UPDATE_ALL_PROJECTS", text)

    def test_update_orchestrator_at_claims_the_flight(self) -> None:
        body = function_body(
            code_only_lines(INSTALLER), "pub async fn update_orchestrator_at("
        )
        self.assertTrue(
            body,
            "update_orchestrator_at signature not found — the scan would pass "
            "vacuously; fix the prefix if the signature changed.",
        )
        text = "\n".join(body)
        self.assertIn(
            "single_flight::begin_or_refuse",
            text,
            "update_orchestrator_at copies a whole orchestrator tree over the "
            "target; MenuBar's loop guard is frontend-only (plan §F #26).",
        )
        self.assertIn("OP_UPDATE_ORCHESTRATOR_AT", text)

    def test_the_two_commands_use_distinct_keys(self) -> None:
        """One shared key would let an orchestrator-clone refresh block a
        project bundle reconcile. The two are deliberately separate
        operations (the do-not-merge boundary) — the guard must not
        re-couple them.
        """
        all_projects = "\n".join(
            function_body(
                code_only_lines(PROJECTS_V2), "pub async fn update_all_projects("
            )
        )
        orchestrator = "\n".join(
            function_body(
                code_only_lines(INSTALLER), "pub async fn update_orchestrator_at("
            )
        )
        self.assertNotIn("OP_UPDATE_ORCHESTRATOR_AT", all_projects)
        self.assertNotIn("OP_UPDATE_ALL_PROJECTS", orchestrator)

    def test_guard_keys_are_defined_and_distinct(self) -> None:
        text = SINGLE_FLIGHT.read_text(encoding="utf-8")
        self.assertIn('OP_UPDATE_ALL_PROJECTS: &str = "update_all_projects"', text)
        self.assertIn(
            'OP_UPDATE_ORCHESTRATOR_AT: &str = "update_orchestrator_at"', text
        )


def classify_clone_writers(
    commands: dict[str, str],
) -> tuple[set[str], set[str]]:
    """Split `commands` into (writes_clone, writes_clone_without_claiming).

    Factored out of the test so the "can this gate fail?" proof below can run
    the REAL classifier over a synthetic file. A proof that runs a copy of the
    logic proves nothing about the logic that ships.
    """
    writers = {
        name
        for name, body in commands.items()
        if any(call in body for call in CLONE_WRITING_CALLS)
    }
    unclaimed = {name for name in writers if ENTRY not in commands[name]}
    return writers, unclaimed


class OrchestratorCloneClaimWiring(unittest.TestCase):
    """v0.2.95 — every surface that WRITES the orchestrator clone takes the
    one shared claim, through the one entry point. CLOSED WORLD: the set is
    discovered from the source, not listed here.

    `OP_UPDATE_ORCHESTRATOR_CLONE` is deliberately ONE key for many commands
    (`single_flight.rs` explains why: they act on the SAME tree, so two keys
    would let a MenuBar click and a Preferences click interleave a `git pull`,
    a `git reset --hard` and an `install.py --update` on it). That invariant is
    only worth anything if each of them actually claims it.

    They all claim through `begin_orchestrator_update_or_refuse`, never
    `begin_or_refuse(OP_UPDATE_ORCHESTRATOR_CLONE)`: one function is what
    stops the callers from disagreeing about the key.
    """

    def _all_commands(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for path in (INSTALLER, SELF_UPDATE):
            found = discover_commands(code_only_lines(path))
            self.assertTrue(
                found,
                f"no #[command] discovered in {path.name} — the discovery "
                "regex has stopped matching and this gate would pass "
                "vacuously.",
            )
            merged.update(found)
        return merged

    def test_every_clone_writing_command_is_accounted_for(self) -> None:
        """THE gate. A new `#[command]` that writes the clone and does not
        claim fails here, by construction, without anyone remembering to add
        it to a list.
        """
        commands = self._all_commands()
        writers, unclaimed = classify_clone_writers(commands)

        self.assertTrue(
            writers,
            "no clone-writing command found at all — CLONE_WRITING_CALLS has "
            "gone stale and this gate is vacuous.",
        )

        unexplained = unclaimed - set(HANDS_DOWN) - set(EXEMPT)
        self.assertEqual(
            unexplained,
            set(),
            "these #[command]s write the orchestrator clone but take no "
            f"single-flight claim: {sorted(unexplained)}. Add "
            f"`{ENTRY}()` at the top of each (or, if one genuinely acts on a "
            "DIFFERENT tree, record it in EXEMPT with the reason). Prior "
            "review §4.8: an unclaimed surface and a claimed one can be "
            "offered to the user at the same moment, and only one of them "
            "refuses.",
        )

    def test_the_hand_down_helpers_really_take_the_claim(self) -> None:
        """A hand-down is only an exemption because someone else claimed.
        Assert that someone still does.
        """
        commands = self._all_commands()
        for command, (helper_prefix, path) in HANDS_DOWN.items():
            self.assertIn(
                command,
                commands,
                f"{command} is recorded as a hand-down receiver but is no "
                "longer a #[command] — drop the stale HANDS_DOWN entry.",
            )
            helper = "\n".join(function_body(code_only_lines(path), helper_prefix))
            self.assertTrue(
                helper,
                f"{helper_prefix!r} not found in {path.name} — the hand-down "
                "claim holder was renamed; this gate would pass vacuously.",
            )
            self.assertIn(
                ENTRY,
                helper,
                f"{command} takes no claim of its own because "
                f"{helper_prefix!r} takes one for it. It no longer does.",
            )

    def test_exempt_entries_still_name_live_commands(self) -> None:
        """An exemption for a command that no longer exists is a rule nobody
        reads, protecting nothing — and it hides the next real one.
        """
        commands = self._all_commands()
        for name in EXEMPT:
            self.assertIn(
                name,
                commands,
                f"EXEMPT names {name!r}, which is no longer a #[command]. "
                "Remove the stale entry.",
            )

    def test_the_known_clone_writers_are_all_present(self) -> None:
        """A floor on the discovered set.

        The closed-world check above fails when a surface APPEARS unclaimed;
        this one fails when a surface DISAPPEARS — a renamed marker, a body
        the boundary regex over-truncates, or a refactor that hides the call
        behind a new helper would otherwise shrink the world silently and
        take the guarantee with it.
        """
        commands = self._all_commands()
        writers, _ = classify_clone_writers(commands)
        expected = {
            # installer.rs
            "update_orchestrator",
            "merge_orchestrator_with_upstream",
            "rebase_orchestrator_onto_upstream",
            "abort_orchestrator_merge_or_rebase",
            "resume_orchestrator_update",
            "keep_local_and_continue_update",
            "accept_upstream_and_continue_update",
            "resolve_untracked_collision_and_retry",
            "resolve_autostash_pop_and_retry",
            "apply_pending_install",
            "apply_hardware_reconfig",
            "update_orchestrator_at",
            "install_orchestrator",
            # self_update.rs
            "apply_launcher_update",
            "force_resync_launcher",
        }
        missing = expected - writers
        self.assertEqual(
            missing,
            set(),
            f"these commands are no longer seen as clone writers: "
            f"{sorted(missing)}. Either the call they were recognised by was "
            "renamed (update CLONE_WRITING_CALLS) or the surface genuinely "
            "stopped writing the clone (update this floor, and say so).",
        )

    def test_the_collision_resolver_claims_before_it_deletes(self) -> None:
        """The claim must be taken in the span that DELETES, not merely by the
        retry it tail-calls.

        Pre-v0.2.95-phase-3 the deletions ran unguarded and only the retry was
        claimed, so a concurrent update meant files removed and no update run.
        """
        text = "\n".join(
            function_body(
                code_only_lines(INSTALLER),
                "pub(crate) async fn claim_then_resolve_collision_files(",
            )
        )
        self.assertTrue(text, "claim_then_resolve_collision_files not found")
        self.assertIn(ENTRY, text)
        self.assertIn(
            "resolve_collision_files(",
            text,
            "the claim and the deletions must live in the SAME function — "
            "that is the whole guarantee.",
        )
        # ...and the claim must come FIRST in that function.
        self.assertLess(
            text.index(ENTRY),
            text.index("    let results ="),
            "the claim must be taken BEFORE any file is removed",
        )

    def test_the_retry_receives_the_claim_rather_than_retaking_it(self) -> None:
        """One claim spans the deletions and the retry they unblock.

        Releasing in between would reopen the window exactly where the tree is
        least consistent (files removed, pull not started) — and re-taking it
        inside the tail call would deadlock against the claim already held.
        """
        text = "\n".join(
            function_body(
                code_only_lines(INSTALLER),
                "pub async fn resolve_untracked_collision_and_retry<",
            )
        )
        self.assertTrue(text, "resolve_untracked_collision_and_retry not found")
        self.assertIn("update_orchestrator_with_claim(app, path, window, flight)", text)
        self.assertNotIn(
            ENTRY,
            text,
            "the command itself must not take a second claim — it receives one "
            "from `claim_then_resolve_collision_files` and hands it on.",
        )

    def test_the_resume_chain_hands_the_claim_down_instead_of_retaking_it(
        self,
    ) -> None:
        """v0.2.95 ship-gate MAJOR-3 — the re-entrancy half.

        `resolve_autostash_pop_and_retry` and the two one-click conflict
        buttons all do destructive work of their own and THEN run the resume
        tail. Each takes the claim at its own top and passes it down; if any
        of them instead let the resume take a fresh claim, that call would be
        refused by the claim its own caller is holding — a working recovery
        path turned into a dead button.

        The Rust side proves the property behaviourally
        (`a_caller_holding_the_claim_can_still_run_the_claim_free_abort`);
        this proves these particular callers are wired into it.
        """
        lines = code_only_lines(INSTALLER)
        for prefix in (
            "pub async fn resolve_autostash_pop_and_retry<",
            "async fn resolve_conflict_and_resume<",
        ):
            text = "\n".join(function_body(lines, prefix))
            self.assertTrue(text, f"{prefix!r} not found in installer.rs")
            self.assertIn(
                "resume_orchestrator_update_with_claim(",
                text,
                f"{prefix!r} must hand its claim to the resume tail, not let "
                "the tail take a second one.",
            )
            self.assertNotIn(
                "resume_orchestrator_update(app",
                text,
                f"{prefix!r} still calls the claim-TAKING resume command; "
                "that call deadlocks against the claim it already holds.",
            )


class ArtefactSourceWiring(unittest.TestCase):
    """v0.2.95 ship-gate MAJOR-2 — the already-up-to-date branch must not tell
    the tail that the source tree moved.

    `owes_manifest_refresh`'s three arms are unit-tested in Rust against real
    manifest bytes; what a unit test cannot reach is which variant
    `apply_launcher_update` hands to the tail, because that command takes an
    `AppHandle`. Passing `SourceOnly` there is precisely the defect: it stamps
    `post_source_only: true` on a tree that did not move, and
    `check_for_updates` turns that into an `install_stale` badge demanding a
    full re-install after a click that changed nothing.
    """

    def test_the_already_up_to_date_branch_passes_unchanged(self) -> None:
        text = "\n".join(
            function_body(
                code_only_lines(SELF_UPDATE), "pub async fn apply_launcher_update<"
            )
        )
        self.assertTrue(text, "apply_launcher_update not found")
        # ARGUMENT position (trailing comma), not the `Unchanged =>` match arm
        # further down the same body — otherwise reverting the early return to
        # `SourceOnly` would still satisfy this, since the arm names both
        # variants. The distinction is the whole assertion.
        self.assertIn(
            "ArtefactSource::Unchanged,",
            text,
            "the already-up-to-date early return must hand the tail "
            "`ArtefactSource::Unchanged`; `SourceOnly` claims an advance that "
            "did not happen (ship-gate MAJOR-2).",
        )
        self.assertNotIn(
            "ArtefactSource::SourceOnly,",
            text,
            "a variant is being passed to the tail as a literal argument, and "
            "the only branch that does that is the already-up-to-date one — "
            "which moved nothing and must not say `SourceOnly`.",
        )

    def test_the_manifest_write_is_gated_on_the_decision_function(self) -> None:
        """The gate must be the named decision, not an inline comparison that
        a fourth variant could silently fall outside of.
        """
        text = "\n".join(
            function_body(
                code_only_lines(SELF_UPDATE), "async fn finish_apply_after_pull<"
            )
        )
        self.assertTrue(text, "finish_apply_after_pull not found")
        self.assertIn("owes_manifest_refresh(artefacts)", text)
        self.assertNotIn(
            "artefacts == ArtefactSource::SourceOnly",
            text,
            "the inline comparison is back; use `owes_manifest_refresh`, whose "
            "match is exhaustive over the variants.",
        )


class InstallPyRunnerDbGuardWiring(unittest.TestCase):
    """v0.2.95 ship-gate MINOR-8 — every caller of the shared
    `install.py --update` runner closes the launcher.db connection around it.

    On Windows the launcher holds SQLite's writer lock exclusively, so
    install.py's `_self_heal_kg_bindings_on_update` rebind times out after 5 s
    and defers `kg_binding_self_heal_db_error` — the half-install loop v0.2.60
    added `DbUpdateClosedGuard` to end. Two of the three call sites had the
    guard and the third did not, and the third is the one every RECOVERY path
    goes through (merge, rebase, resume, one-click conflict resolution,
    autostash-pop resolution): the worst place to reintroduce it.

    Why a source gate: `DbUpdateClosedGuard::new` takes an `AppHandle`, and so
    does every function that calls the runner, so no unit test can construct
    the situation. The asymmetry itself is what went unnoticed for a cycle, so
    it is the asymmetry that gets the gate. Comment-blind, via this file's
    stripper — the explanatory comment at each call site names the guard.
    """

    #: (file, signature prefix) of every function that runs install.py --update
    #: through the shared runner and holds an `AppHandle` to guard with.
    RUNNER_CALLERS = (
        (INSTALLER, "pub(crate) async fn update_orchestrator_with_claim<"),
        (INSTALLER, "async fn run_post_pull_install_and_restart<"),
        (SELF_UPDATE, "pub async fn apply_launcher_update<"),
    )

    def test_every_install_py_runner_call_site_closes_the_db_first(self) -> None:
        for path, prefix in self.RUNNER_CALLERS:
            with self.subTest(fn=prefix):
                text = "\n".join(function_body(code_only_lines(path), prefix))
                self.assertTrue(
                    text,
                    f"{prefix!r} not found in {path.name} — the scan would "
                    "pass vacuously; fix the prefix if the signature changed.",
                )
                self.assertIn(
                    "run_install_py_update(",
                    text,
                    f"{prefix!r} no longer runs install.py through the shared "
                    "runner; if that is deliberate, drop it from "
                    "RUNNER_CALLERS.",
                )
                self.assertIn(
                    "DbUpdateClosedGuard::new(",
                    text,
                    f"{prefix!r} runs `install.py --update` without closing "
                    "the launcher.db connection — on Windows install.py "
                    "cannot then take the SQLite writer lock (v0.2.60).",
                )
                self.assertLess(
                    text.index("DbUpdateClosedGuard::new("),
                    text.index("run_install_py_update("),
                    "the DB must be closed BEFORE install.py starts, not "
                    "after — a guard taken afterwards protects nothing.",
                )


class ScannerSelfCheck(unittest.TestCase):
    """The scan must not be able to pass vacuously."""

    def test_naive_locator_is_fooled_by_a_comment(self) -> None:
        """Proves the code-only filter is load-bearing.

        A raw substring search finds the marker inside a comment and inside a
        string literal; the filtered search does not. If this test starts
        failing because BOTH find it, the stripper has stopped stripping and
        every wiring assertion above has quietly become a prose check.
        """
        fixture = (
            "// this comment mentions single_flight::begin_or_refuse\n"
            'let msg = "single_flight::begin_or_refuse";\n'
            "let unrelated = 1;\n"
        )
        self.assertIn("single_flight::begin_or_refuse", fixture)

        state: tuple = (_ST_CODE,)
        filtered_lines = []
        for line in fixture.splitlines():
            stripped, state = _strip_line(line, state)
            filtered_lines.append(stripped)
        filtered = "\n".join(filtered_lines)
        self.assertNotIn(
            "single_flight::begin_or_refuse",
            filtered,
            "the comment/string stripper is not stripping — the wiring "
            "assertions in this file would pass on prose alone",
        )

    def test_body_extractor_bounds_at_the_next_item(self) -> None:
        """A body must END. Without the column-0 boundary the 'body' would be
        the rest of a 12k-line file, and the marker from ANY later function
        would satisfy the wiring assertions.
        """
        lines = code_only_lines(PROJECTS_V2)
        body = function_body(lines, "pub async fn update_all_projects(")
        self.assertTrue(body)
        self.assertLess(
            len(body),
            len(lines),
            "the extractor ran to EOF — the boundary regex did not match",
        )
        self.assertLess(
            len(body), 600, f"body suspiciously long ({len(body)} lines)"
        )

    def test_missing_signature_returns_empty_not_whole_file(self) -> None:
        lines = code_only_lines(PROJECTS_V2)
        self.assertEqual(
            function_body(lines, "pub async fn this_function_does_not_exist("),
            [],
        )


class ClosedWorldGateCanActuallyFail(unittest.TestCase):
    """v0.2.95 ship-gate MAJOR-3 — proof that the gate above CATCHES a new
    unclaimed surface.

    The previous enumeration could not fail this way: a surface it did not
    name was simply invisible to it, which is how "every surface that WRITES
    the orchestrator clone" came to be asserted of four out of eleven. A gate
    whose failure mode is never exercised is a gate nobody knows the shape of,
    so the classifier that ships is run here over synthetic sources whose
    answers are known by construction.
    """

    #: A file with three commands: one claimed, one that writes the clone with
    #: NO claim (the regression), and one that touches nothing.
    FIXTURE = (
        "#[command]\n"
        "pub async fn claimed_surface<R: Runtime>(\n"
        "    app: AppHandle<R>,\n"
        ") -> Result<(), String> {\n"
        "    let _f = crate::commands::single_flight::"
        "begin_orchestrator_update_or_refuse()?;\n"
        "    run_install_py_update(&repo, py, \"x\", None).await\n"
        "}\n"
        "\n"
        "#[command]\n"
        "pub async fn forgotten_surface<R: Runtime>(\n"
        "    app: AppHandle<R>,\n"
        ") -> Result<(), String> {\n"
        "    run_install_py_update(&repo, py, \"x\", None).await\n"
        "}\n"
        "\n"
        "#[command]\n"
        "pub fn harmless_surface() -> u8 {\n"
        "    7\n"
        "}\n"
    )

    def _commands_from(self, text: str) -> dict[str, str]:
        state: tuple = (_ST_CODE,)
        lines = []
        for line in text.splitlines():
            stripped, state = _strip_line(line, state)
            lines.append(stripped)
        return discover_commands(lines)

    def test_a_new_unclaimed_clone_writer_is_caught(self) -> None:
        commands = self._commands_from(self.FIXTURE)
        self.assertEqual(
            set(commands),
            {"claimed_surface", "forgotten_surface", "harmless_surface"},
            "discovery missed a #[command] in the fixture",
        )
        writers, unclaimed = classify_clone_writers(commands)
        self.assertEqual(writers, {"claimed_surface", "forgotten_surface"})
        self.assertEqual(
            unclaimed,
            {"forgotten_surface"},
            "the classifier must flag a clone-writing command that takes no "
            "claim — this is the exact miss that shipped six unguarded "
            "surfaces",
        )

    def test_a_claim_named_only_in_a_comment_does_not_satisfy_the_gate(
        self,
    ) -> None:
        """The failure mode a source gate falls into on its own: prose that
        looks like wiring. `// takes begin_orchestrator_update_or_refuse` must
        not acquit a command that never calls it.
        """
        commented = self.FIXTURE.replace(
            "pub async fn forgotten_surface<R: Runtime>(\n",
            "pub async fn forgotten_surface<R: Runtime>(\n"
            "    // NOTE: guarded by "
            "single_flight::begin_orchestrator_update_or_refuse\n",
        )
        _, unclaimed = classify_clone_writers(self._commands_from(commented))
        self.assertIn(
            "forgotten_surface",
            unclaimed,
            "a claim mentioned in a COMMENT acquitted the command — the "
            "code-only filter is not being applied by the shipping "
            "classifier",
        )


if __name__ == "__main__":
    unittest.main()
