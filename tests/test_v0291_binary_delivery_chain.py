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

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "launcher" / "src-tauri" / "src"
INSTALLER_RS = SRC / "commands" / "installer.rs"
SELF_UPDATE_RS = SRC / "commands" / "self_update.rs"
LIB_RS = SRC / "lib.rs"
FRESHNESS_RS = SRC / "services" / "binary_freshness.rs"
SERVICES_MOD_RS = SRC / "services" / "mod.rs"
# v0.2.91 wave-2: the at-rest swap delegates its lock-write + detached spawn
# here, so the no-relaunch invariant is now checked across this seam.
UPDATE_HANDOFF_RS = SRC / "commands" / "update_handoff.rs"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


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
        """WI-7 (a) at the RC-1 site specifically."""
        self.assertIn("update_binary_clobber_averted", read(INSTALLER_RS))


class Wi2AlreadyUpToDateHealsTests(unittest.TestCase):
    """WI-2 — the early-return branches must still reconcile the binary."""

    def _branch_bodies(self, src: str) -> list[str]:
        out = []
        for m in re.finditer(r'if pull_output\.contains\("Already up to date"\)', src):
            out.append(src[m.start() : m.start() + 3000])
        return out

    def test_every_already_up_to_date_branch_reconciles_before_returning(self) -> None:
        src = read(INSTALLER_RS)
        bodies = self._branch_bodies(src)
        self.assertGreaterEqual(
            len(bodies), 2, "expected the update_orchestrator + merge siblings"
        )
        for i, body in enumerate(bodies):
            self.assertIn(
                "reconcile_dist_at_rest(",
                body,
                f'"Already up to date" branch #{i} returns without reconciling the '
                "dist binary — that is the RC-2 dead end",
            )
            idx_reconcile = body.index("reconcile_dist_at_rest(")
            idx_return = body.index("return Ok(")
            self.assertLess(
                idx_reconcile,
                idx_return,
                f"branch #{i} must reconcile BEFORE returning success",
            )


class Wi4SurfaceBParityTests(unittest.TestCase):
    """WI-4 — the launcher self-update surface gets the same machinery."""

    def test_clean_tree_guard_excludes_generated_release_controlled_paths(self) -> None:
        src = read(SELF_UPDATE_RS)
        start = src.index("fn first_blocking_change(")
        body = src[start : start + 2000]
        self.assertIn("is_generated_release_controlled(", body)
        self.assertIn("build_generated_release_controlled_globset(", body)

    def test_surface_b_does_a_pre_pull_rename_and_reverts_it(self) -> None:
        src = read(SELF_UPDATE_RS)
        self.assertIn("binary_freshness::pre_pull_rename_running_binary(", src)
        # Every failure return after the pull must revert first.
        self.assertGreaterEqual(
            src.count("revert_rename(pre_pull_renamed.as_deref())"),
            3,
            "each post-rename failure path must revert the rename",
        )

    def test_surface_b_renames_before_the_generated_file_reconcile(self) -> None:
        """Ordering parity with `update_orchestrator`. The reconcile's
        `git checkout HEAD -- launcher/dist/**` cannot rewrite a mapped running
        `.exe`; renaming ourselves aside first is what makes the take-upstream
        reconcile actually able to resolve the dist-divergence class it exists
        for."""
        src = read(SELF_UPDATE_RS)
        start = src.index("pub async fn apply_launcher_update")
        body = src[start : src.index("\n#[command]", start)]
        idx_rename = body.index("pre_pull_rename_running_binary(")
        idx_f1 = body.index("auto_restore_byte_identical_tracked_mods(")
        idx_reconcile = body.index("resolve_generated_files_to_upstream(")
        self.assertLess(idx_rename, idx_f1)
        self.assertLess(idx_rename, idx_reconcile)

    def test_both_surfaces_use_the_shared_handoff_tail(self) -> None:
        for path in (INSTALLER_RS, SELF_UPDATE_RS):
            self.assertIn(
                "stage_and_handoff_after_update(",
                read(path),
                f"{path.name} must route through the shared finalize tail",
            )

    def test_surface_b_exits_for_the_handoff_only_after_its_bookkeeping(self) -> None:
        """Load-bearing ordering: unlike the installer surface, this flow never
        runs install.py, so the desktop-shortcut / install-manifest /
        hardware-redetect updates below are the ONLY place the new version gets
        recorded. Exiting straight out of the staging call would skip all
        three."""
        src = read(SELF_UPDATE_RS)
        start = src.index("async fn finish_apply_after_pull")
        body = src[start : src.index("\n#[command]", start)]
        idx_stage = body.index("stage_and_handoff_after_update(")
        idx_manifest = body.index("refresh_install_manifest(")
        idx_exit = body.index("if handoff.handoff_active {")
        self.assertLess(idx_stage, idx_manifest)
        self.assertLess(
            idx_manifest,
            idx_exit,
            "the handoff exit must come AFTER the manifest/shortcut bookkeeping",
        )

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

    def test_minor1_self_update_checks_the_revert_outcome(self) -> None:
        """MINOR-1: Surface B discarded the `RevertOutcome`, so an averted
        clobber there produced no deferral, no audit row and no trace — while
        the installer surface recorded both."""
        src = read(SELF_UPDATE_RS)
        start = src.index("let revert_rename = |backup: Option<&std::path::Path>|")
        body = src[start : start + 1600]
        self.assertNotIn(
            "let _ = crate::services::binary_freshness::revert_pre_pull_rename(",
            body,
            "the revert outcome must not be discarded",
        )
        self.assertIn("revert_and_record(", body)
        self.assertIn("RevertOutcome::ClobberAverted", body)
        self.assertIn('"update_binary_clobber_averted"', body)


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

    def test_falling_back_to_the_real_display_says_so_on_stderr(self) -> None:
        idx = self.src.index("XVFB_RUN=")
        block = self.src[idx : idx + 1800]
        self.assertIn(
            "REAL display",
            block,
            "a fallback onto the operator's display must be announced, not "
            "silent — that silence IS the window-flash incident",
        )
        self.assertIn(">&2", block, "the note belongs on stderr, not stdout")
        # The no-display case still HARD-FAILS rather than warning.
        self.assertIn("exit 3", block)

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
