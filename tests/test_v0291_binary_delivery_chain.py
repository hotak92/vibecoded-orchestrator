# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-A — structural pins on the Windows binary delivery chain.

The 2026 field incident: a Windows install ran a hand-frozen v0.2.88
``vct-launcher.exe`` for a month while source updates landed cleanly every
time. Two root causes made that reachable and then permanent:

* **RC-1** — the update abort tail restored the pre-pull backup over a
  canonical path the pull had already filled with NEW bytes.
* **RC-2** — NO code path re-examined the on-disk binary outside the tail of a
  SUCCESSFUL pull: "Already up to date" early-returned before staging, the
  self-update check compared git SHAs only, boot recovery read lock files only,
  and the self-update surface hard-blocked on its clean-tree guard.

Each fix is unit-tested in Rust (``services::binary_freshness``'s reactor-free
``#[cfg(test)] mod tests``). What Rust unit tests CANNOT reach are the
CALL-SITES: ``update_orchestrator`` is a Tauri command with a ``Window``
parameter, and the boot/exit hooks live inside ``tauri::Builder``. Those are
pinned here by source scan — the same discipline as
``test_v0290_no_bare_tokio_spawn_in_sync_fns.py``.

RED-PROOF: every assertion below fails against ``bd8f6836``. Verified by
running this file's checks against ``git show bd8f6836:<path>`` extracts; the
evidence is recorded in the WP-A implementation report.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.common.rust_source import read_rust_code

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "launcher" / "src-tauri" / "src"
INSTALLER_RS = SRC / "commands" / "installer.rs"
SELF_UPDATE_RS = SRC / "commands" / "self_update.rs"
# v0.2.95 phase 2: the pull sequence BOTH update surfaces run. Several
# invariants below used to live in `installer.rs` alone and now span the two.
UPDATE_PIPELINE_RS = SRC / "commands" / "update_pipeline.rs"
LIB_RS = SRC / "lib.rs"
FRESHNESS_RS = SRC / "services" / "binary_freshness.rs"
SERVICES_MOD_RS = SRC / "services" / "mod.rs"
# v0.2.91 wave-2: the at-rest swap delegates its lock-write + detached spawn
# here, so the no-relaunch invariant is now checked across this seam.
UPDATE_HANDOFF_RS = SRC / "commands" / "update_handoff.rs"
# v0.2.100 WP-03b: the ONE update pipeline. `update_run.rs` is its driver +
# live side effects, `update_failure.rs` its one error rendering, and
# `restart.rs` its one relaunch. The per-surface commands the claims below
# were first pinned on (`update_orchestrator`, `apply_launcher_update`,
# `force_resync_launcher`, `merge_orchestrator_with_upstream`, …) are gone.
UPDATE_RUN_RS = SRC / "commands" / "update_run.rs"
UPDATE_FAILURE_RS = SRC / "commands" / "update_failure.rs"
RESTART_RS = SRC / "commands" / "restart.rs"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


#: `read`, with Rust comments gone and string literals verbatim.
#:
#: This project's rule is "never guard wiring with a source scan — a name in a
#: comment satisfies it", and this file is full of necessary source scans (the
#: call-sites it pins are Tauri commands and `tauri::Builder` hooks that no
#: Rust unit test can reach). Stripping comments removes the specific way a
#: scan lies: a helper named only in a comment explaining why it was REMOVED
#: would otherwise keep the assertion green.
#:
#: v0.2.95 ship-gate MINOR-7 — this was a PRIVATE line-comment cutter with its
#: own quote tracker, written here while `tests/common/rust_source.py` already
#: answered the same question for six other lints. Two homes for one concern,
#: and the private one was the weaker: it handled neither block comments nor
#: `r#"…"#` raw strings, and carried no cross-line lexer state. It now names
#: the shared home, which is also the home the module's own docstring instructs
#: callers to extend ("do not add a fourth copy").
read_code = read_rust_code


def item_body(src: str, signature: str, indent: str = "") -> str:
    """The text of the item starting at `signature`, up to its closing brace
    at `indent` (``""`` for a top-level fn, four spaces for an impl method).

    Raises ``ValueError`` when the signature is absent, so a rename fails
    loudly instead of turning an assertion vacuous.
    """
    start = src.index(signature)
    return src[start : src.index("\n" + indent + "}\n", start)]


#: The production `UpdateOps` impl — the LIVE side effects of the one update
#: pipeline (the trait declaration above it names the same methods).
_LIVE_OPS_IMPL = "impl<'w, R: Runtime> UpdateOps for LiveOps<'w, R> {"


def live_op(src: str, signature: str) -> str:
    """Body of one method of the production `UpdateOps` impl."""
    return item_body(src[src.index(_LIVE_OPS_IMPL) :], signature, "    ")


class SharedHomeTests(unittest.TestCase):
    """WP-A's shared-component call: ONE home for the delivery chain."""

    def test_binary_freshness_module_exists_and_is_registered(self) -> None:
        self.assertTrue(FRESHNESS_RS.is_file(), f"missing {FRESHNESS_RS}")
        self.assertIn("pub mod binary_freshness;", read(SERVICES_MOD_RS))

    def test_installer_no_longer_defines_its_own_copies(self) -> None:
        """The relocated helpers must have exactly one definition.

        A second definition in installer.rs would be the drift hazard the
        extraction exists to remove.
        """
        src = read(INSTALLER_RS)
        for sym in (
            "fn revert_pre_pull_rename",
            "fn pre_pull_rename_running_binary",
            "fn stage_locked_binaries_for_handoff",
            "fn path_with_new_suffix",
        ):
            self.assertNotIn(
                sym,
                src,
                f"{sym} must live only in services/binary_freshness.rs",
            )

    def test_freshness_module_defines_the_pure_decision_fns(self) -> None:
        src = read(FRESHNESS_RS)
        for sym in (
            "fn decide_revert(",
            "fn decide_binary_freshness(",
            "fn canonical_path_for_backup(",
            "fn stage_dirty_binaries(",
            "fn stage_and_handoff_after_update(",
            "fn reconcile_dist_at_rest(",
        ):
            self.assertIn(sym, src, f"{sym} missing from the shared module")

    def test_mechanism_tests_are_not_gated_behind_a_tokio_reactor(self) -> None:
        """v0.2.90 lesson: ``#[tokio::test]`` supplies a reactor that masks
        'no reactor running' panics, so the DECISION tests must be plain
        ``#[test]``. (The few async tests here drive real git subprocesses,
        which is a different concern.)"""
        src = read(FRESHNESS_RS)
        for name in (
            "differing_canonical_is_never_clobbered",
            "identical_or_absent_canonical_still_reverts",
            "unknown_state_prefers_keeping_the_canonical_file",
            "on_disk_newer_and_dirty_is_stale_for_both_reasons",
            "matching_versions_and_clean_dist_are_fresh",
            "revert_keeps_freshly_pulled_bytes_and_parks_the_backup",
        ):
            idx = src.index(f"fn {name}(")
            preceding = src[max(0, idx - 200) : idx]
            self.assertNotIn(
                "#[tokio::test]",
                preceding,
                f"{name} is a mechanism test and must not run under a reactor",
            )


class Wi3NonClobberingRevertTests(unittest.TestCase):
    """WI-3 — the abort tail must never restore old bytes over newer ones."""

    def test_revert_routes_through_the_pure_decision(self) -> None:
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) fn revert_pre_pull_rename(")
        body = src[start : start + 2600]
        self.assertIn("compare_backup_to_canonical(", body)
        self.assertIn("decide_revert(", body)
        self.assertIn("RevertDecision::KeepCanonicalParkBackup", body)

    def test_abort_tail_reports_and_records_an_averted_clobber(self) -> None:
        """The tail must both REPORT the averted clobber to its caller and
        RECORD it durably.

        v0.2.91 wave-2 (WP-F carry-over iv): the record is no longer emitted
        inline here — the tail calls the shared `revert_and_record`, the same
        helper the launcher self-update surface uses. The invariant is
        unchanged, so this follows it across the seam instead of asserting on
        the inlined shape (which would have made a one-home consolidation look
        like a regression, and a genuine drop of the record look fine as long
        as the old literal survived somewhere in the window).
        """
        src = read(INSTALLER_RS)
        start = src.index("fn abort_update_restore_binaries_and_hub(")
        body = src[start : start + 1800]
        self.assertIn("RevertOutcome::ClobberAverted", body)
        self.assertIn(
            "binary_freshness::revert_and_record(",
            body,
            "the abort tail must revert through the shared revert+record helper",
        )
        # …and that helper must actually emit the record.
        freshness = read(FRESHNESS_RS)
        rec_start = freshness.index("pub(crate) fn revert_and_record_with(")
        rec_body = freshness[rec_start : rec_start + 1400]
        self.assertIn("RevertOutcome::ClobberAverted", rec_body)
        self.assertIn(
            "emit(",
            rec_body,
            "revert_and_record must emit the clobber-averted record",
        )
        wrapper = freshness.index("pub(crate) fn revert_and_record(")
        self.assertIn(
            "emit_clobber_averted_condition(",
            freshness[wrapper : wrapper + 600],
            "the production wrapper must supply the real emitter",
        )

    def test_pop_conflict_site_audits_the_averted_clobber(self) -> None:
        """WI-7 (a) at the RC-1 site specifically.

        RETARGETED v0.2.100 (WP-03b): the RC-1 site — the exit-0 pull whose
        autostash POP conflicted after the merge landed — lives in the ONE
        pipeline (`update_pipeline.rs`), no longer in installer.rs. The row is
        not written there (the Db handle is the caller's): the site reads the
        abort tail's outcome and carries it OUT on the error, and
        `update_failure::from_pipeline_error` turns it into the
        `update_binary_clobber_averted` row (behaviour-tested in
        `update_failure::tests::pipeline_pop_conflict_maps_to_autostash_pop_
        with_the_after_success_audit_row`). Pinned here: the site does not
        drop the outcome on the floor.
        """
        pipeline = read_code(UPDATE_PIPELINE_RS)
        self.assertIn("let record_clobber = restore.clobber_averted;", pipeline)
        pop_at = pipeline.index("return Err(UpdatePipelineError::AutostashPopConflict {")
        self.assertIn(
            "record_binary_clobber_averted: record_clobber,",
            pipeline[pop_at : pop_at + 400],
            "the autostash-pop exit must carry the abort tail's clobber outcome",
        )


class Wi2AlreadyUpToDateHealsTests(unittest.TestCase):
    """WI-2 — the early-return branches must still reconcile the binary.

    The property: EVERY "Already up to date" early return reconciles the
    at-rest dist binary before it returns success. Miss one and that install
    has no path back to a fresh binary — every later update says "Already up
    to date" and changes nothing, forever (RC-2).

    **The property follows the code across homes; the count does not move.**
    v0.2.95 phase 2 extracted the `update_orchestrator` branch into
    `commands/update_pipeline.rs`, leaving one branch in `installer.rs`. A scan
    of `installer.rs` alone then reported "1 not greater than or equal to 2"
    and read as a REGRESSION, when both branches were intact and both still
    reconciled. Scanning the union keeps the assertion honest about what it
    asserts: two branches exist, wherever they live, and each reconciles.

    The pipeline's branch ALSO has a behavioural test now —
    `update_pipeline::tests::already_up_to_date_still_probes_the_dist_binary_
    for_staleness` drives it over a temp repo with a stale dist sidecar and
    asserts the staleness comes back. That is the stronger guard, and it is
    available because `reconcile_and_pull` is reachable from a unit test. The
    sibling branch is not: it sits inside `merge_orchestrator_with_upstream`, a
    Tauri command needing an `AppHandle` + `Window` + a live clone, which is
    exactly the call-site class this file exists to cover.
    """

    #: Every file that may hold an "Already up to date" early return of the
    #: PULL sequence. v0.2.100 (WP-03b): the merge surface's sibling branch
    #: went with `merge_orchestrator_with_upstream`; the recovery git ops
    #: (Merge/Rebase/Resume) now report `already_up_to_date` to
    #: `update_run::finish_recovery_git_op`, which heals — pinned separately
    #: in `test_the_recovery_up_to_date_leg_reconciles_before_returning`.
    HOMES = (UPDATE_PIPELINE_RS,)

    def _branch_bodies(self) -> list[tuple[str, str]]:
        """`(home, body)` for every branch, across all homes."""
        out: list[tuple[str, str]] = []
        for path in self.HOMES:
            src = read(path)
            for m in re.finditer(
                r'if pull_output\.contains\("Already up to date"\)', src
            ):
                out.append((path.name, src[m.start() : m.start() + 3000]))
        return out

    def test_every_already_up_to_date_branch_reconciles_before_returning(self) -> None:
        bodies = self._branch_bodies()
        self.assertGreaterEqual(
            len(bodies),
            1,
            "expected the pull sequence's branch in "
            f"{[p.name for p in self.HOMES]}; a DROP here is the real regression "
            "this guards (a branch deleted, not a branch relocated)",
        )
        for home, body in bodies:
            self.assertIn(
                "reconcile_dist_at_rest(",
                body,
                f'"Already up to date" branch in {home} returns without reconciling '
                "the dist binary — that is the RC-2 dead end",
            )
            idx_reconcile = body.index("reconcile_dist_at_rest(")
            idx_return = min(
                i for i in (body.find("return Ok("), body.find("return Err(")) if i >= 0
            )
            self.assertLess(
                idx_reconcile,
                idx_return,
                f"the branch in {home} must reconcile BEFORE returning",
            )

    def test_the_recovery_up_to_date_leg_reconciles_before_returning(self) -> None:
        """v0.2.100 (WP-03b): the second "Already up to date" home.

        Merge/Rebase/Resume run through `update_run::recovery_git_op`; an
        up-to-date result is mapped by `finish_recovery_git_op`, which must
        restore the binaries AND reconcile the dist binary at rest before it
        returns — the same RC-2 heal the pull sequence performs.
        """
        src = read_code(UPDATE_RUN_RS)
        body = item_body(src, "async fn finish_recovery_git_op<")
        arm = body[body.index("Ok(done) if done.already_up_to_date =>") :]
        arm = arm[: arm.index("Ok(done) =>")]
        self.assertIn("abort_update_restore_binaries_and_hub(", arm)
        self.assertIn("reconcile_dist_at_rest(", arm)
        self.assertLess(
            arm.index("abort_update_restore_binaries_and_hub("),
            arm.index("reconcile_dist_at_rest("),
            "restore the binaries first, then reconcile the at-rest state",
        )
        self.assertIn("dist_binary_stale: heal.is_stale()", arm)


class Wi4SurfaceBParityTests(unittest.TestCase):
    """WI-4 — the launcher self-update surface gets the same machinery."""

    # RETIRED v0.2.100 (WP-03b): `test_clean_tree_guard_excludes_generated_
    # release_controlled_paths` pinned surface B's own clean-tree guard
    # (`self_update::first_blocking_change`). That guard went with
    # `apply_launcher_update`; superseded by the one pipeline, which has no
    # surface-level clean-tree refusal and reconciles the generated/release-
    # controlled class to upstream inside `reconcile_and_pull`
    # (`resolve_generated_files_to_upstream`, pinned by
    # `test_renames_happen_before_the_pull_sequence_that_reconciles`).

    def test_surface_b_does_a_pre_pull_rename_and_reverts_it(self) -> None:
        """WI-4's property, followed across homes (v0.2.95 phase 2).

        It used to read `self_update.rs` for an inline
        `pre_pull_rename_running_binary(` plus three
        `revert_rename(pre_pull_renamed.as_deref())` call-sites. Surface B now
        pulls through the SHARED pipeline, so both the rename and the revert
        live there — and the property got STRONGER on the way: the pipeline
        renames the hub binary too (`launcher/dist/*/vct-hub*` are tracked
        files, which surface B used to pull straight over), and each failure
        group restores through `abort_update_restore_binaries_and_hub`, which
        reverts non-clobberingly AND brings the hub back up.

        Asserting the old inline shape would now report a consolidation as a
        regression — the exact mistake `test_abort_tail_reports_and_records_an_
        averted_clobber` calls out for its own seam.
        """
        src = read_code(UPDATE_PIPELINE_RS)
        self.assertIn("pre_pull_rename_running_binary(", src)
        self.assertIn(
            "pre_pull_rename_vct_hub_binary(",
            src,
            "the shared pipeline must rename the HUB binary too — the gap that "
            "made surface B pull over a running vct-hub",
        )
        # One restore per failure GROUP reachable after the renames: the
        # non-zero-exit block (which fronts merge-in-progress /
        # untracked-collision / conflict / non-FF / generic), the post-pull
        # unmerged-tree block, the HEAD-did-not-advance guard, and the
        # "Already up to date" no-op return.
        self.assertGreaterEqual(
            src.count("abort_update_restore_binaries_and_hub("),
            4,
            "every post-rename exit must restore the binaries and the hub",
        )
        # …and surface B must not keep a second copy of the machinery.
        self.assertNotIn(
            "pre_pull_rename_running_binary(",
            read_code(SELF_UPDATE_RS),
            "one home: surface B renames through the pipeline, not inline",
        )

    def test_renames_happen_before_the_pull_sequence_that_reconciles(self) -> None:
        """Ordering, load-bearing, pinned at BOTH levels it now spans.

        The reconcile's `git checkout HEAD -- launcher/dist/**` cannot rewrite a
        mapped running `.exe`; renaming aside first is what makes the
        take-upstream reconcile able to resolve the dist-divergence class it
        exists for, and what stops `git pull` aborting on
        ERROR_SHARING_VIOLATION.

        Phase 2 split the steps across two functions — the renames in
        `prepare_and_pull_orchestrator_repo`, F1 and the generated reconcile
        inside the `reconcile_and_pull` it then calls.

        CORRECTED v0.2.95 phase 3. The hub stop and both renames were written
        out FOUR times (pipeline / merge / rebase / resume) and were MISSING
        from `force_resync_launcher`, whose `git reset --hard` writes the same
        tracked binaries; they now have one home,
        `stop_hub_and_rename_binaries_aside`. This test asserted the LITERAL
        LOCATION of the two rename calls, so a legitimate extraction reddened
        it — the recurring shape a source gate must be written against. It now
        asserts the PROPERTY, at both levels:

          * the pipeline prepares the binaries BEFORE the sequence that
            reconciles and pulls, and
          * that preparation is stop-hub → rename hub → rename launcher.

        Either link broken and the ordering guards nothing, so both are pinned.
        """
        # Level 1 (RETARGETED v0.2.100, WP-03b): `prepare_and_pull_
        # orchestrator_repo` is gone; the driver runs phase 5 (hub stop +
        # renames) before phase 6 (the git operation) — behaviour-tested by
        # `update_run::tests::pull_ff_runs_the_thirteen_phases_in_order` and,
        # per kind, `merge_rebase_resume_run_the_one_pipeline`. Pinned here:
        # the live phase-5 op IS the one home, and the pull the live phase-6
        # op reaches is the sequence that reconciles (and renames nothing
        # itself — it receives the renames).
        run_src = read_code(UPDATE_RUN_RS)
        drive = item_body(run_src, "async fn drive_phases<")
        self.assertLess(
            drive.index("ops.stop_hub_and_rename("),
            drive.index("ops.git_op("),
            "the hub stop + renames must precede the git operation",
        )
        live_rename = live_op(run_src, "    fn stop_hub_and_rename(")
        self.assertIn("stop_hub_and_rename_binaries_aside(", live_rename)
        pull = item_body(src := read_code(UPDATE_PIPELINE_RS), "pub(crate) async fn pull_to_upstream(")
        self.assertIn("reconcile_and_pull(", pull)
        self.assertNotIn("stop_hub_and_rename_binaries_aside(", pull)

        # Level 2: the one home really does all three, in order. Without this
        # the assertion above is satisfied by a call to an empty function.
        hstart = src.index("pub(crate) fn stop_hub_and_rename_binaries_aside")
        home = item_body(src[hstart:], "pub(crate) fn stop_hub_and_rename_binaries_aside")
        idx_stop = home.index("ensure_hub_stopped_for_update(")
        idx_hub = home.index("pre_pull_rename_vct_hub_binary(")
        idx_launcher = home.index("pre_pull_rename_running_binary(")
        self.assertLess(
            idx_stop,
            idx_hub,
            "the hub must be STOPPED before its binary is renamed aside",
        )
        self.assertLess(idx_stop, idx_launcher)

        # And inside the sequence, F1 and the generated reconcile are still
        # there to be protected — otherwise the ordering above guards nothing.
        seq = src[src.index("\nasync fn reconcile_and_pull") :]
        self.assertIn("auto_restore_byte_identical_tracked_mods(", seq)
        self.assertIn("resolve_generated_files_to_upstream(", seq)

    def test_the_resync_surface_reaches_the_same_one_home(self) -> None:
        """v0.2.95 phase 3 — the hard-reset rescue had NO hub handling at all,
        and its act is `git reset --hard`, which writes
        `launcher/dist/<arch>/vct-hub{,.exe}` exactly as a pull would.

        RETARGETED v0.2.100 (WP-03b): `force_resync_launcher` is gone; the
        reset is `UpdateKind::ResetHard` of the ONE pipeline. The driver runs
        the hub stop + renames (phase 5) before ANY kind's git operation, so
        the claim now rests on two facts pinned here: the live phase-5 op
        does not skip `ResetHard`, and a failed reset hands the restore to the
        driver (`restored: false` → `abort_restore`, behaviour-tested by
        `update_run::tests` "A git-op failure the pull sequence already
        restored is NOT restored a second time" — both values).
        """
        src = read_code(UPDATE_RUN_RS)
        live_rename = live_op(src, "    fn stop_hub_and_rename(")
        self.assertIn("UpdateKind::ResetHard =>", live_rename)
        self.assertNotIn("return", live_rename, "no kind may skip the hub stop + renames")
        self.assertIn("stop_hub_and_rename_binaries_aside(", live_rename)

        reset = item_body(src, "pub(crate) async fn reset_hard_git_op(")
        self.assertIn("restored: false,", reset)
        self.assertLess(
            reset.index("create_reset_backup("),
            reset.index('"reset", "--hard"'),
            "the local work is saved BEFORE the hard reset",
        )
        drive = item_body(src, "async fn drive_phases<")
        self.assertIn("if !f.restored {", drive)
        self.assertIn("ops.abort_restore(", drive)

    def test_the_one_relaunch_uses_the_shared_handoff_tail(self) -> None:
        """RETARGETED v0.2.100 (WP-03b) from
        `test_both_surfaces_use_the_shared_handoff_tail`: there is one update
        surface and one relaunch (`restart::relaunch`, phase 13). It must
        route through the shared staging + handoff tail, and the pipeline's
        live phase-13 op must reach it.
        """
        relaunch = item_body(read_code(RESTART_RS), "pub(crate) async fn relaunch<")
        self.assertIn("stage_and_handoff_after_update(", relaunch)
        live = live_op(read_code(UPDATE_RUN_RS), "    fn relaunch(")
        self.assertIn("crate::commands::restart::relaunch(", live)

    def test_the_handoff_exit_comes_only_after_the_bookkeeping(self) -> None:
        """Load-bearing ordering: exiting for the Windows handoff must not
        skip the desktop-shortcut / hardware-redetect bookkeeping.

        RETARGETED v0.2.100 (WP-03b) from `test_surface_b_exits_for_the_
        handoff_only_after_its_bookkeeping` (`finish_apply_after_pull`, gone).
        The bookkeeping is phase 12 and the relaunch — the only place the
        handoff exit happens — is phase 13; the ledger order is
        behaviour-tested (`pull_ff_runs_the_thirteen_phases_in_order`). The
        install-manifest half of the old claim is superseded: install.py is
        now the only writer of `state/install-manifest.json`
        (`manifest.rs`, "Bug G … RETIRED v0.2.100").
        """
        src = read_code(UPDATE_RUN_RS)
        drive = item_body(src, "async fn drive_phases<")
        # The advancing path's relaunch is the LAST `relaunch_phase(` call; the
        # earlier one is the already-up-to-date leg, where nothing changed and
        # the ledger records Bookkeeping as skipped ("nothing changed").
        self.assertLess(drive.index("ops.bookkeeping("), drive.rindex("relaunch_phase("))
        self.assertIn("ops.relaunch(", item_body(src, "async fn relaunch_phase<"))
        live = live_op(src, "    fn bookkeeping(")
        self.assertIn("refresh_desktop_shortcut(", live)
        self.assertIn("mark_hardware_redetect_pending_after_update(", live)
        relaunch = item_body(read_code(RESTART_RS), "pub(crate) async fn relaunch<")
        self.assertIn("if handoff.handoff_active {", relaunch)

    def test_update_check_reconciles_at_rest(self) -> None:
        src = read(SELF_UPDATE_RS)
        start = src.index("pub async fn check_for_launcher_update")
        body = src[start : start + 4000]
        self.assertIn(
            "reconcile_dist_at_rest(",
            body,
            "the SHA-only update check is blind to a stale binary (RC-2)",
        )


class Wi1BootReconcileTests(unittest.TestCase):
    """WI-1 — boot-time reconcile, wired without breaking the boot contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = read(LIB_RS)

    def test_boot_spawns_the_reconcile_via_tauri_async_runtime(self) -> None:
        idx = self.src.index("reconcile_dist_at_rest(")
        preceding = self.src[max(0, idx - 1600) : idx]
        self.assertIn(
            "tauri::async_runtime::spawn",
            preceding,
            "boot work must use the lazy global runtime — a bare tokio::spawn "
            "from setup() panics with 'no reactor running' (the v0.2.89 "
            "boot-death class)",
        )

    def test_setup_complete_marker_is_still_the_last_line_of_setup(self) -> None:
        """The boot smoke (scripts/launcher-boot-smoke.sh, pre-ship Gate 2c and
        the Release step) waits on this marker. Anything added after it, or any
        blocking work before it, breaks the gate."""
        marker = 'eprintln!("[vct] setup complete");'
        self.assertIn(marker, self.src)
        after = self.src[self.src.index(marker) + len(marker) :]
        # Only the setup closure's `Ok(())` + closing braces may follow.
        tail = "\n".join(
            line.strip()
            for line in after.splitlines()
            if line.strip() and not line.strip().startswith("//")
        )
        self.assertTrue(
            tail.startswith("Ok(())"),
            f"setup() must end at the marker; found: {tail[:120]!r}",
        )

    def test_boot_reconcile_never_restarts_or_quits(self) -> None:
        """Standing ruling: no auto-restart, no auto-quit."""
        idx = self.src.index("reconcile_dist_at_rest(")
        block = self.src[max(0, idx - 400) : idx + 1200]
        for forbidden in ("app.exit(", "restart_launcher(", "force_quit("):
            self.assertNotIn(
                forbidden,
                block,
                f"the at-rest reconcile must not call {forbidden}",
            )

    def test_exit_hook_performs_the_armed_swap(self) -> None:
        idx = self.src.index("if let tauri::RunEvent::Exit = event {")
        block = self.src[idx : idx + 1400]
        self.assertIn("perform_armed_swap_on_exit()", block)


class NoAutoRestartTests(unittest.TestCase):
    """The at-rest swap must not relaunch — the user asked to quit."""

    def test_at_rest_swap_lock_carries_no_relaunch(self) -> None:
        """Quitting means quitting: the at-rest lock must name no relaunch
        target, or the launcher comes back after the user asked it to go away.

        v0.2.91 wave-2 (WP-F carry-over ii): the lock is written by the shared
        `prepare_update_handoff_impl`, and `relaunch` is the ONE parameter that
        distinguishes the two callers. Followed across the seam: the call site
        must pass `false`, and the impl must map `false` to `relaunch: None`.
        """
        src = read(FRESHNESS_RS)
        start = src.index("fn swap_on_exit_impl(install_root: &Path)")
        body = src[start : src.index("#[cfg(not(target_os = \"windows\"))]", start)]
        self.assertIn(
            "prepare_update_handoff_impl(install_root, false)",
            body,
            "the at-rest swap must ask the shared handoff for a NO-relaunch lock",
        )
        self.assertNotIn("relaunch: Some(", body)

        handoff = read(UPDATE_HANDOFF_RS)
        impl_start = handoff.index("pub(crate) fn prepare_update_handoff_impl(")
        impl_body = handoff[impl_start:]
        lock_at = impl_body.index("let lock = UpdateLock {")
        lock_body = impl_body[lock_at : lock_at + 500]
        self.assertIn("relaunch: if relaunch {", lock_body)
        self.assertIn("None", lock_body)
        # The command wrapper is the UPDATE surface: it relaunches.
        cmd_start = handoff.index("pub async fn prepare_windows_update_handoff(")
        self.assertIn(
            "prepare_update_handoff_impl(&PathBuf::from(&install_root), true)",
            handoff[cmd_start : cmd_start + 900],
            "the update command must still relaunch after its swap",
        )

    def test_at_rest_arming_defers_to_an_in_flight_update_handoff(self) -> None:
        """An update handoff owns the relaunch; our at-rest lock must not
        clobber it or the post-update restart silently disappears."""
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) fn arm_stage1_swap_on_exit(")
        body = src[start : start + 1500]
        self.assertIn("UPDATE_LOCK_FILE", body)
        self.assertIn("lock_path.exists()", body)


class Wi7ObservabilityTests(unittest.TestCase):
    """WI-7 — both silent states now leave a durable record."""

    def test_condition_ids_are_declared_once(self) -> None:
        src = read(FRESHNESS_RS)
        for cid in (
            '"launcher_binary_clobber_averted"',
            '"launcher_binary_handoff_skipped_dirty"',
            '"launcher_binary_stale"',
        ):
            self.assertEqual(
                src.count(cid), 1, f"{cid} must be declared exactly once"
            )

    def test_handoff_skipped_while_dirty_is_recorded(self) -> None:
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) async fn stage_and_handoff_after_update(")
        body = src[start : start + 2600]
        self.assertIn("emit_handoff_skipped_while_dirty(", body)
        self.assertIn("!handoff_result.handoff_active && !staged.is_empty()", body)

    def test_stale_condition_names_all_three_versions(self) -> None:
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) fn emit_binary_stale_condition(")
        body = src[start : start + 3000]
        for token in ("running_version", "on_disk", "dist_dirty"):
            self.assertIn(token, body)

    def test_deferrals_route_through_the_locked_shared_emitter(self) -> None:
        """No raw UPDATE_DEFERRED rewrite from this module — a full-file
        rewrite would drop foreign entries.

        v0.2.91 wave-2: the module also RESOLVES its own records (the WP-B
        registry pairs `launcher_binary_stale` and
        `launcher_binary_handoff_skipped_dirty` with probe-driven clears, and
        the freshness probe is that probe), which needs a cheap read-only
        "is it even recorded?" pre-check so a healthy install does not spend a
        python subprocess on every update-check poll. Reading is fine; writing
        is not. So instead of asserting the filename never appears (a proxy
        that a read trips just as loudly as a rewrite), state the invariant
        itself: the names may appear ONLY inside the one read-only presence
        helper, and that helper may not write.
        """
        src = read(FRESHNESS_RS)
        self.assertIn("crate::services::deferral::emit_deferral_entry(", src)
        self.assertIn("crate::services::deferral::resolve_deferral_conditions(", src)

        helper_start = src.index("fn condition_is_recorded(")
        helper_end = src.index("\n}", helper_start) + 2
        helper = src[helper_start:helper_end]

        # Production code only — `#[cfg(test)]` fixtures legitimately write
        # their own throwaway report files under a tempdir.
        production = src[: src.index("mod tests {")].replace(helper, "")
        for name in ("UPDATE_DEFERRED.md", "UPDATE_DEFERRED.json"):
            self.assertNotIn(
                name,
                production,
                f"{name} may only be named by the read-only presence helper — "
                f"every write goes through the locked shared emitter/resolver",
            )

        for writer in ("fs::write(", "File::create(", "OpenOptions", "remove_file("):
            self.assertNotIn(
                writer,
                helper,
                f"the presence check must stay read-only (found {writer})",
            )


class FixRoundWiringTests(unittest.TestCase):
    """v0.2.91 fix round — the CALL-SITE half of four findings.

    The decisions themselves are unit-tested in Rust (``binary_freshness``'s
    ``#[cfg(test)] mod tests``). What Rust cannot reach on a Linux/macOS host is
    whether the production call sites actually consult them: the staging gate
    only runs on Windows, ``swap_on_exit_impl`` is ``#[cfg(target_os =
    "windows")]``, and the self-update abort closure lives inside a Tauri command
    with an ``AppHandle`` parameter. Those are pinned by source scan — the same
    discipline as ``Wi1BootReconcileTests`` above.
    """

    def test_major1_dist_probe_excludes_untracked_files(self) -> None:
        """MAJOR-1: `??` rows are not divergence.

        The dist directory is where untracked debris lives — the user's own
        `*.old-*` / `*.bak` copies, this module's parked backups, its own `.new`
        staging files. Counting them made a healthy install permanently
        `Stale(DistDirtyVsHead)`, and the deferral's own remediation
        (`git checkout -- launcher/dist/`) cannot delete an untracked file.
        """
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) async fn dist_is_dirty(")
        body = src[start : src.index("\n}", start)]
        self.assertIn(
            '"--untracked-files=no"',
            body,
            "the dist dirty probe must ignore untracked files",
        )

    def test_major1_both_probes_agree_on_untracked(self) -> None:
        """The Rust and Python legs of ONE delivery chain must classify the
        same tree the same way. `dist_dirty_paths` has always skipped `??`."""
        py = (REPO_ROOT / "vco_lib" / "dist_binary_repair.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('if code == "??":', py)
        self.assertEqual(
            read(FRESHNESS_RS).count('"--untracked-files=no"'),
            3,
            "the dist probe + both single-path probes must all exclude untracked",
        )

    def test_major2a_handoff_tail_invalidates_stale_new_siblings(self) -> None:
        """MAJOR-2(a): a `.new` staged earlier in the session carries PRE-pull
        bytes; `prepare_windows_update_handoff` swaps any `.new` it finds with no
        freshness check of its own, so the leftover must be dropped first."""
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) async fn stage_and_handoff_after_update(")
        body = src[start : start + 3000]
        idx_invalidate = body.index("invalidate_stale_new_siblings_for(")
        idx_stage = body.index("stage_locked_binaries_for_handoff(")
        idx_handoff = body.index("prepare_windows_update_handoff(")
        self.assertLess(idx_invalidate, idx_stage)
        self.assertLess(
            idx_invalidate,
            idx_handoff,
            "stale `.new` files must be dropped BEFORE the handoff reads them",
        )

    def test_major2a_exit_swap_invalidates_stale_new_siblings(self) -> None:
        """Same invariant on the at-rest exit path: between ARMING and this exit
        a real update may have landed newer binaries."""
        src = read(FRESHNESS_RS)
        start = src.index("fn swap_on_exit_impl(install_root: &Path)")
        body = src[start : src.index('#[cfg(not(target_os = "windows"))]', start)]
        idx_invalidate = body.index("invalidate_stale_new_siblings_for_blocking(")
        # v0.2.91 wave-2 (carry-over ii): the swap list is now built inside the
        # shared handoff impl, purely from which `<target>.new` siblings exist
        # on disk. That makes the ORDER load-bearing in exactly the same way:
        # a stale sibling still present when the handoff runs is a stale
        # sibling that gets swapped in.
        idx_delegate = body.index("prepare_update_handoff_impl(")
        self.assertLess(
            idx_invalidate,
            idx_delegate,
            "stale siblings must be dropped BEFORE the handoff reads them",
        )
        # And the yield-to-a-real-update check must also precede the handoff,
        # which overwrites the lock unconditionally.
        self.assertLess(
            body.index("lock_path.exists()"),
            idx_delegate,
            "the at-rest swap must yield to an in-flight update handoff",
        )

    def test_major2b_at_rest_reconcile_stands_down_for_a_running_update(self) -> None:
        """MAJOR-2(b): the reconcile is polled, so it can land mid-update. It
        must reuse the EXISTING update-gate lockfile + the stage1 handoff lock
        rather than inventing a new one."""
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) fn update_owns_the_tree_at(")
        body = src[start : start + 1400]
        self.assertIn("skip_if_update_in_progress_at(", body)
        self.assertIn("handoff_lock.exists()", body)
        # And the entry point consults it.
        entry = src.index("pub(crate) async fn reconcile_dist_at_rest(")
        self.assertIn(
            "update_owns_the_tree()",
            src[entry : entry + 400],
            "the at-rest entry point must gate on update ownership",
        )

    def test_major2b_standing_down_is_not_reported_as_fresh(self) -> None:
        """We did not look, so we must not claim a clean bill of health."""
        src = read(FRESHNESS_RS)
        self.assertIn("NotProbed", src)
        start = src.index("pub fn is_stale(&self)")
        body = src[start : start + 260]
        self.assertIn(
            "matches!(self.verdict, FreshnessVerdict::Stale(_))",
            body,
            "only a POSITIVE stale verdict may read as stale",
        )

    def test_minor3_dirty_alone_never_arms_a_destructive_swap(self) -> None:
        """MINOR-3: versions EQUAL + dirty tree is also what a local
        `cargo build` looks like. Staging HEAD's blob and arming a swap there
        destroys the developer's build at their next quit."""
        src = read(FRESHNESS_RS)
        start = src.index("pub(crate) fn decide_at_rest_action(")
        body = src[start : start + 900]
        self.assertIn("StaleReason::DistDirtyVsHead) => AtRestAction::SurfaceOnly", body)
        # The reconcile must route through the decision, not re-derive it.
        rec = src.index("pub(crate) async fn reconcile_dist_at_rest_gated(")
        rec_body = src[rec : rec + 4200]
        self.assertIn("decide_at_rest_action(&verdict)", rec_body)
        self.assertIn("action == AtRestAction::StageAndArm", rec_body)

    def test_minor1_the_one_pipeline_records_the_revert_outcome(self) -> None:
        """MINOR-1: a surface once discarded the `RevertOutcome`, so an averted
        clobber there produced no deferral, no audit row and no trace.

        RETARGETED v0.2.100 (WP-03b) from `test_minor1_self_update_checks_the_
        revert_outcome` (`self_update::render_pipeline_error`, gone). The ONE
        rendering is `update_failure::from_pipeline_error`, which turns both
        clobber-carrying classifications into the audit row (behaviour-tested
        in `update_failure::tests`); pinned here is the half no unit test can
        reach — the live git op PERSISTS those rows through the Db it holds.
        """
        pipeline = read_code(UPDATE_PIPELINE_RS)
        self.assertIn(
            "record_binary_clobber_averted",
            pipeline,
            "the pipeline must REPORT an averted clobber to its caller",
        )
        self.assertIn(
            "restore.clobber_averted",
            pipeline,
            "…and it must read the abort tail's outcome to know, not assume",
        )

        failure = item_body(read_code(UPDATE_FAILURE_RS), "pub(crate) fn from_pipeline_error(")
        self.assertEqual(
            failure.count("if record_binary_clobber_averted {"),
            2,
            "both clobber-carrying classifications (conflict and "
            "autostash-pop-after-success) must record it",
        )
        self.assertIn('"update_binary_clobber_averted"', failure)

        pull = item_body(read_code(UPDATE_RUN_RS), "async fn pull_ff_git_op<")
        at = pull.index("update_failure::from_pipeline_error(err)")
        self.assertIn("audit(rows)", pull[at : at + 200])
        self.assertIn("db.audit(&op", pull)


class Wi6CommentCorrectionTests(unittest.TestCase):
    """WI-6 — the allowlist rationale must stop asserting a false premise."""

    def test_false_regeneration_premise_is_corrected(self) -> None:
        src = read(SRC / "commands" / "git_user_editable_merge.rs")
        # Both comment sites carry an explicit correction block.
        self.assertEqual(
            src.count("CORRECTED v0.2.91 (WI-6)"),
            2,
            "both the allowlist rationale and the pop-probe exclusion rationale "
            "must be corrected",
        )
        # The two falsified premises may still be QUOTED, but only inside a
        # correction — never left standing as the rationale.
        for premise in (
            "A download-user never dirties dist",
            "install.py --update` regenerates / re-deploys these immediately",
        ):
            if premise in src:
                idx = src.index(premise)
                self.assertIn(
                    "CORRECTED v0.2.91 (WI-6)",
                    src[max(0, idx - 2000) : idx],
                    f"the premise {premise!r} must appear only as a quoted, "
                    "corrected claim — not as live rationale",
                )


class BootSmokeDisplayResolutionTests(unittest.TestCase):
    """`scripts/launcher-boot-smoke.sh` must not silently use the real display.

    Standing wave-4 item: xvfb-run was resolved with a bare `command -v`, which
    only checks PATH. On a machine where xvfb is installed but the invoking
    shell's PATH lacks its directory, the script fell straight through to the
    operator's REAL display — a launcher window flashed onto the desktop
    mid-smoke and the run was no longer headless, with nothing said about it.

    RED-PROOF: on the pre-change file the whole block is
    `if command -v xvfb-run …; then RUNNER=(xvfb-run …); elif [ -z "$DISPLAY" ]
    …` — there is no candidate loop and no note, so both assertions below fail.

    v0.2.94: the "announce the fallback" contract is RETIRED. The announced
    fallback still opened a window on a live GNOME/X11 desktop and gnome-shell
    died on a mutter assertion twice on 2026-09-09 (maintainer's machine, no
    xvfb installed). A real display without xvfb-run is now REFUSED (exit 3)
    unless `VCT_BOOT_SMOKE_REAL_DISPLAY=1` opts in; behaviour is DRIVEN in
    `tests/test_v0294_boot_smoke_never_uses_real_display.py`, this class pins
    the source shape.
    """

    def setUp(self) -> None:
        self.src = (REPO_ROOT / "scripts" / "launcher-boot-smoke.sh").read_text(
            encoding="utf-8"
        )

    def test_xvfb_run_is_probed_at_candidate_paths_after_path(self) -> None:
        self.assertIn("command -v xvfb-run", self.src, "PATH stays the first try")
        for candidate in (
            "/usr/bin/xvfb-run",
            "/usr/local/bin/xvfb-run",
            "$HOME/.local/bin/xvfb-run",
        ):
            self.assertIn(
                candidate,
                self.src,
                f"{candidate} is not probed — the same candidate-path pattern "
                "as templates/hooks/lean-ctx-rewrite.sh's lean-ctx probe",
            )

    def test_a_real_display_without_xvfb_is_refused_on_stderr(self) -> None:
        idx = self.src.index('XVFB_RUN=""')
        block = self.src[idx : idx + 3200]
        self.assertIn(
            "REFUSING to open the launcher window",
            block,
            "a real display without xvfb-run must be REFUSED — the announced "
            "fallback killed the desktop session twice (2026-09-09)",
        )
        self.assertIn(">&2", block, "the refusal belongs on stderr, not stdout")
        self.assertIn("VCT_BOOT_SMOKE_REAL_DISPLAY", block, "the opt-in is named")
        self.assertIn("exit 3", block)
        self.assertNotIn(
            "A launcher window WILL appear briefly. Install xvfb to run headless",
            self.src,
            "the retired warn-and-proceed fallback must not come back",
        )

    def test_on_path_behaviour_is_unchanged(self) -> None:
        """When xvfb-run IS on PATH the wrapper chain must be byte-identical to
        the pre-change one — the fix adds a fallback leg, it does not change
        the working path."""
        self.assertIn('XVFB_RUN="xvfb-run"', self.src)
        self.assertIn(
            'RUNNER=("$XVFB_RUN" --auto-servernum "${RUNNER[@]}")',
            self.src,
            "the same `--auto-servernum` wrapper, just via the resolved name",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
