# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-B item 4 — the IMMORTAL condition ids finally get a lifecycle.

Four field-observed entries could never be cleared by anything VCO does:

* ``kg_sync_no_embedding_backend`` — emitted when the KG seed found no
  backend; nothing resolved it, even after the user fixed the backend AND a
  full sync succeeded. Its own sibling ``kg_summary_no_backend`` had the paired
  clear one layer away.
* ``orchestrator_user_modified_preserved`` — no owner, no reconcile, no
  resolve site. Doing exactly what its ``command_to_apply`` says (accept or
  delete the upstream sidecars) left the entry untouched. 31 days and counting
  on the maintainer's own install.
* ``kg_access_phantom_repaired`` + record siblings — an entry whose body says
  "No action needed" that nevertheless required a manual action to remove.
* the two v0.2.91 launcher-binary conditions, whose emit is latched once per
  launcher process — so an emit-refresh clear could never fire and the clear
  HAD to be probe-driven.

Every probe below is tested BOTH ways (resolves / leaves alone), because each
one gates the deletion of a user-visible record: the destructive-branch rule.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_probes as dp  # noqa: E402
from vco_lib.deferral_report import DeferralEntry  # noqa: E402

SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


def _sidecar_entry(*paths: str, truncated: int = 0) -> DeferralEntry:
    """An `orchestrator_user_modified_preserved` entry shaped exactly like the
    Rust emitter's (`git_user_editable_merge.rs::build_deferral_text`).

    ``truncated`` appends the emitter's over-CAP trailer bullet
    (``  - ... and N more``, rendered identically by the Rust emitter at
    `git_user_editable_merge.rs` and by `project_init._format_file_list_md`).
    """
    bullets = "\n".join(
        f"  - `{p.rsplit('.from-upstream-', 1)[0]}` — conflict; local "
        f"preserved, upstream saved as `{p}` (base=aaaaaaa theirs=bbbbbbb)"
        for p in paths
    )
    if truncated:
        bullets = f"{bullets}\n  - ... and {truncated} more"
    cmds = "\n".join(
        f"#   mv {p} {p.rsplit('.from-upstream-', 1)[0]}    # POSIX"
        for p in paths
    )
    return DeferralEntry(
        condition_id="orchestrator_user_modified_preserved",
        title=f"{len(paths)} orchestrator-root file(s) preserved during update",
        detected=(
            "During an orchestrator-root update pulling from `vco_upstream/main`, "
            f"{len(paths)} user-editable file(s) had both local and upstream "
            f"changes. VCO ran a per-path 3-way merge before `git pull`:\n{bullets}"
        ),
        why_deferred="Default-to-safety.",
        command_to_apply=cmds,
        severity="info",
    )


class UpstreamSidecarProbeTests(unittest.TestCase):
    """`orchestrator_user_modified_preserved` — sidecar-presence re-probe."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel: str) -> None:
        p = self.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    def test_extractor_reads_every_named_sidecar(self):
        entry = _sidecar_entry(
            "docs/A.md.from-upstream-5a9ae53",
            "knowledge/concepts/b.md.from-upstream-5a9ae53",
        )
        self.assertEqual(
            dp.upstream_sidecar_paths(entry),
            (
                "docs/A.md.from-upstream-5a9ae53",
                "knowledge/concepts/b.md.from-upstream-5a9ae53",
            ),
        )

    def test_resolves_when_every_sidecar_is_gone(self):
        """ACT leg: the user accepted or deleted each parked upstream copy —
        the entry now describes work that has been done."""
        entry = _sidecar_entry("docs/A.md.from-upstream-5a9ae53")
        verdict = dp.orchestrator_sidecars_still_present(
            dp.ProbeContext(folder=self.folder, entry=entry)
        )
        self.assertIs(verdict, False)

    def test_keeps_entry_while_any_sidecar_survives(self):
        """LEAVE-ALONE leg: one remaining sidecar keeps the whole entry, since
        the entry lists all of them in one section."""
        self._touch("docs/B.md.from-upstream-5a9ae53")
        entry = _sidecar_entry(
            "docs/A.md.from-upstream-5a9ae53",
            "docs/B.md.from-upstream-5a9ae53",
        )
        verdict = dp.orchestrator_sidecars_still_present(
            dp.ProbeContext(folder=self.folder, entry=entry)
        )
        self.assertIs(verdict, True)

    # -----------------------------------------------------------------
    # v0.2.91 dogfood fix — the LEGACY / list-less arm.
    #
    # Pre-fix an entry naming no sidecar returned None UNCONDITIONALLY: not
    # resolvable, not re-classifiable, and — worse — the ledger said nothing
    # about it, so an immortal row looked exactly like a live one. The probe
    # now derives the answer from what the entry HAS (its root) via a bounded
    # read-only sweep. Still positive-evidence-only: a sweep that cannot
    # complete returns None, never a resolution.
    # -----------------------------------------------------------------

    @staticmethod
    def _listless_entry() -> DeferralEntry:
        """An auto-merge-only / legacy entry: no sidecar path anywhere in it."""
        return DeferralEntry(
            condition_id="orchestrator_user_modified_preserved",
            title="t", detected="1 file auto-merged (3-way)",
            why_deferred="w", command_to_apply="c", severity="info",
        )

    def test_listless_entry_resolves_when_the_sweep_finds_no_sidecar(self):
        """The sweep COMPLETED and found nothing — positive evidence the
        condition is over, so the entry finally clears instead of living
        forever with the ledger silent about why."""
        self._touch("docs/A.md")
        self.assertIs(
            dp.orchestrator_sidecars_still_present(
                dp.ProbeContext(folder=self.folder, entry=self._listless_entry())
            ),
            False,
        )

    def test_listless_entry_is_kept_when_any_sidecar_is_parked(self):
        """A sidecar left by ANY merge keeps a list-less entry alive — the
        honest reading of "N files were preserved" when the entry cannot say
        which ones."""
        self._touch("knowledge/concepts/b.md.from-upstream-4c44eb8")
        self.assertIs(
            dp.orchestrator_sidecars_still_present(
                dp.ProbeContext(folder=self.folder, entry=self._listless_entry())
            ),
            True,
        )

    def test_listless_entry_is_unknown_when_the_sweep_cannot_complete(self):
        """A walk that could not finish proves nothing about what it did not
        see. It must NEVER read as absence — the whole module's rule."""
        with mock.patch.object(dp.os, "walk", side_effect=OSError("boom")):
            self.assertIsNone(
                dp.orchestrator_sidecars_still_present(
                    dp.ProbeContext(folder=self.folder, entry=self._listless_entry())
                )
            )

    def test_sweep_declines_rather_than_concluding_past_its_bound(self):
        """The entry-count bound is a REFUSAL, not a truncated answer."""
        self._touch("docs/A.md")
        with mock.patch.object(dp, "_SIDECAR_SCAN_MAX_ENTRIES", 0):
            self.assertIsNone(dp.any_upstream_sidecar_on_disk(self.folder))

    def test_sweep_does_not_descend_into_vcs_or_build_trees(self):
        """A sidecar can only be parked next to a user-editable file, never
        inside `.git/` or a build output — so a stray match there must not
        keep the entry alive (and the prune is what keeps the sweep bounded)."""
        self._touch(".git/objects/x.md.from-upstream-deadbee")
        self._touch("node_modules/p/y.md.from-upstream-deadbee")
        self.assertIs(dp.any_upstream_sidecar_on_disk(self.folder), False)

    def test_dismiss_fields_share_the_extractor(self):
        """The dismissal key and the clear probe must agree on what "the
        preserved sidecars" means, or a dismissal could outlive its subject."""
        entry = _sidecar_entry("docs/A.md.from-upstream-5a9ae53")
        self.assertEqual(
            dp.dismiss_fields_for_sidecars(entry),
            {"preserved_sidecars": ["docs/A.md.from-upstream-5a9ae53"]},
        )

    # -----------------------------------------------------------------
    # wave-2 MINOR-2: the bullet list is CAPPED at 100. Over the cap the
    # emitter writes `  - ... and N more` and the tail is never named —
    # so "every named sidecar is gone" stops being evidence that every
    # sidecar is gone. The probe must decline, not resolve.
    # -----------------------------------------------------------------

    def test_truncated_list_is_unknown_even_when_every_named_sidecar_is_gone(self):
        """RED-PROOF: with the trailer present, deleting the 100 NAMED sidecars
        proves nothing about the unnamed tail. Returning False here would
        delete a record still describing real parked files."""
        entry = _sidecar_entry(
            "docs/A.md.from-upstream-5a9ae53",
            "docs/B.md.from-upstream-5a9ae53",
            truncated=7,
        )
        self.assertIsNone(
            dp.orchestrator_sidecars_still_present(
                dp.ProbeContext(folder=self.folder, entry=entry)
            )
        )

    def test_untruncated_list_still_resolves_when_all_are_gone(self):
        """The other side of the same branch: no trailer ⇒ the list is
        COMPLETE, so an empty disk is positive evidence and the entry clears.
        Without this leg the fix could over-correct into a second immortal."""
        entry = _sidecar_entry(
            "docs/A.md.from-upstream-5a9ae53",
            "docs/B.md.from-upstream-5a9ae53",
        )
        self.assertIs(
            dp.orchestrator_sidecars_still_present(
                dp.ProbeContext(folder=self.folder, entry=entry)
            ),
            False,
        )

    def test_truncated_list_with_a_surviving_sidecar_still_keeps(self):
        """A truncated entry whose named sidecars ALSO survive is plainly
        still-applying — `True`, not the weaker `None`."""
        self._touch("docs/A.md.from-upstream-5a9ae53")
        entry = _sidecar_entry("docs/A.md.from-upstream-5a9ae53", truncated=3)
        self.assertIs(
            dp.orchestrator_sidecars_still_present(
                dp.ProbeContext(folder=self.folder, entry=entry)
            ),
            True,
        )

    def test_truncation_marker_shape_matches_both_emitters(self):
        """The marker is a WIRE SHAPE shared with two emitters. A reworded
        trailer on either side would silently re-open the over-clear."""
        rust = (
            REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
            / "git_user_editable_merge.rs"
        ).read_text(encoding="utf-8")
        self.assertIn('"  - ... and {} more"', rust)
        py = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
        self.assertIn('  - ... and {len(paths) - cap} more', py)


class LauncherBinaryProbeTests(unittest.TestCase):
    """The two WP-A conditions Python can meaningfully probe."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.dist_rel = "launcher/dist/linux-x64"
        (self.folder / self.dist_rel).mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.folder / self.dist_rel / "vct-launcher").write_text(
            "binary-v1", encoding="utf-8"
        )
        self._write_meta("0.2.91")
        self._git("add", "-A")
        self._git("commit", "-qm", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=str(self.folder), capture_output=True, check=False,
        )

    def _write_meta(self, version: str):
        (self.folder / self.dist_rel / "vct-launcher.metadata.json").write_text(
            json.dumps({"launcher_version": version}), encoding="utf-8",
        )

    def _ctx(self, entry=None):
        return dp.ProbeContext(
            folder=self.folder,
            entry=entry,
            extras={
                "dist_rel_dir": self.dist_rel,
                "launcher_binary_name": "vct-launcher",
                "source_version": "0.2.91",
            },
        )

    # ── launcher_binary_handoff_skipped_dirty ─────────────────────────────

    def test_handoff_probe_resolves_on_a_clean_tree(self):
        self.assertIs(dp.launcher_dist_still_dirty(self._ctx()), False)

    def test_handoff_probe_keeps_entry_while_dist_is_dirty(self):
        (self.folder / self.dist_rel / "vct-launcher").write_text(
            "binary-v2", encoding="utf-8"
        )
        self.assertIs(dp.launcher_dist_still_dirty(self._ctx()), True)

    def test_handoff_probe_keeps_entry_while_a_new_sibling_waits(self):
        """A clean tree with a staged `.new` still has an unfired handoff —
        the new bytes are on disk and nothing is scheduled to move them."""
        (self.folder / self.dist_rel / "vct-launcher.new").write_text(
            "staged", encoding="utf-8"
        )
        self.assertIs(dp.launcher_dist_still_dirty(self._ctx()), True)

    def test_untracked_files_alone_are_not_dirtiness(self):
        """TRACKED-ONLY, mirroring the v0.2.91 MAJOR-1 `-uno` fix: an
        untracked artefact next to the binary is not divergence from HEAD."""
        (self.folder / self.dist_rel / "notes.txt").write_text(
            "scratch", encoding="utf-8"
        )
        self.assertIs(dp.launcher_dist_still_dirty(self._ctx()), False)

    def test_handoff_probe_declines_without_extras(self):
        """No dist_rel_dir ⇒ the OS→dir mapping is unavailable. Decline rather
        than duplicate the mapping (it has been wrong once already)."""
        self.assertIsNone(
            dp.launcher_dist_still_dirty(
                dp.ProbeContext(folder=self.folder, entry=None, extras={})
            )
        )

    def test_handoff_probe_declines_when_git_is_unusable(self):
        """`dist_dirty_paths` returns [] on ANY git failure — fail-safe for its
        REPAIR caller, but a CLEAR probe must not read that as 'clean'."""
        with TemporaryDirectory() as non_repo:
            ctx = dp.ProbeContext(
                folder=Path(non_repo), entry=None,
                extras={"dist_rel_dir": self.dist_rel},
            )
            self.assertIsNone(dp.launcher_dist_still_dirty(ctx))

    # ── launcher_binary_stale ─────────────────────────────────────────────

    def test_stale_probe_resolves_when_delivery_is_complete_and_no_launcher_runs(self):
        dp_orig = dp._launcher_process_running
        dp._launcher_process_running = lambda _name: False
        try:
            self.assertIs(dp.launcher_binary_stale_still_applies(self._ctx()), False)
        finally:
            dp._launcher_process_running = dp_orig

    def test_stale_probe_keeps_entry_while_a_launcher_is_running(self):
        """Python cannot read the RUNNING process's compiled-in version, so a
        live launcher means "cannot tell" — and cannot-tell keeps the entry."""
        dp_orig = dp._launcher_process_running
        dp._launcher_process_running = lambda _name: True
        try:
            self.assertIs(dp.launcher_binary_stale_still_applies(self._ctx()), True)
        finally:
            dp._launcher_process_running = dp_orig

    def test_stale_probe_keeps_entry_while_dist_is_dirty(self):
        (self.folder / self.dist_rel / "vct-launcher").write_text(
            "hand-copied-old", encoding="utf-8"
        )
        self.assertIs(dp.launcher_binary_stale_still_applies(self._ctx()), True)

    def test_stale_probe_keeps_entry_when_on_disk_lags_source(self):
        self._write_meta("0.2.88")
        self._git("add", "-A")
        self._git("commit", "-qm", "older meta")
        self.assertIs(dp.launcher_binary_stale_still_applies(self._ctx()), True)

    def test_stale_probe_declines_without_a_readable_sidecar(self):
        (self.folder / self.dist_rel / "vct-launcher.metadata.json").unlink()
        self._git("add", "-A")
        self._git("commit", "-qm", "drop meta")
        self.assertIsNone(dp.launcher_binary_stale_still_applies(self._ctx()))

    def test_stale_probe_declines_without_extras(self):
        self.assertIsNone(
            dp.launcher_binary_stale_still_applies(
                dp.ProbeContext(folder=self.folder, entry=None, extras={})
            )
        )

    # ── v0.2.91 WP-D: the process scan is TRI-STATE ───────────────────────

    def test_stale_probe_declines_when_the_process_scan_cannot_run(self):
        """RED before WP-D: `_launcher_process_running` returned a plain bool
        and answered ``False`` — "no launcher is running" — when the scan
        itself could not be performed. The probe then RESOLVED an entry
        describing real outstanding work, on evidence that did not exist.
        Wave-2 accepted the residual because a later boot re-emits; "a later
        boot fixes it" is not a reason to draw an unsupported conclusion."""
        dp_orig = dp._process_scan_available
        dp._process_scan_available = lambda: False
        try:
            self.assertIsNone(dp._launcher_process_running("vct-launcher"))
            self.assertIsNone(
                dp.launcher_binary_stale_still_applies(self._ctx()),
                "scan-failed must keep the entry, never clear it",
            )
        finally:
            dp._process_scan_available = dp_orig

    def test_stale_probe_does_not_resolve_when_the_scanner_raises(self):
        """The same guarantee, expressed WITHOUT naming the new helper — so it
        red-proofs on behaviour, not on an attribute that did not exist yet.

        On c67ef888 the scan's `except` swallowed the failure and returned
        ``False`` ("nothing running"), and the probe RESOLVED. Now it declines.
        """
        with mock.patch(
            "vco_lib.dist_binary_repair.scan_for_launcher_pid",
            side_effect=OSError("process table unavailable"),
        ):
            self.assertIsNot(
                dp.launcher_binary_stale_still_applies(self._ctx()), False,
                "a failed process scan must never clear the entry",
            )

    def test_scan_availability_probes_the_right_tool_per_os(self):
        import platform as _platform

        seen: list[str] = []

        def _which(name):
            seen.append(name)
            return None

        with mock.patch("shutil.which", side_effect=_which):
            dp._process_scan_available()
        if _platform.system().lower().startswith("win"):
            self.assertEqual(seen, ["tasklist"])
        else:
            self.assertEqual(seen, ["pgrep", "ps"])

    def test_import_failure_in_the_scan_is_unknown_not_absence(self):
        dp_orig = dp._process_scan_available
        dp._process_scan_available = lambda: True
        try:
            with mock.patch.dict(
                "sys.modules", {"vco_lib.dist_binary_repair": None},
            ):
                self.assertIsNone(dp._launcher_process_running("vct-launcher"))
        finally:
            dp._process_scan_available = dp_orig

    def test_scan_available_still_distinguishes_running_from_absent(self):
        """The tri-state must not collapse the OTHER way: with a usable
        scanner, both real answers still come through."""
        dp_orig = dp._process_scan_available
        dp._process_scan_available = lambda: True
        try:
            with mock.patch(
                "vco_lib.dist_binary_repair.scan_for_launcher_pid",
                return_value=4242,
            ):
                self.assertIs(dp._launcher_process_running("vct-launcher"), True)
            with mock.patch(
                "vco_lib.dist_binary_repair.scan_for_launcher_pid",
                return_value=None,
            ):
                self.assertIs(dp._launcher_process_running("vct-launcher"), False)
        finally:
            dp._process_scan_available = dp_orig


class ProbeDispatchTests(unittest.TestCase):
    def test_unknown_probe_name_is_unknown_not_resolved(self):
        self.assertIsNone(
            dp.run_probe("no_such_probe", dp.ProbeContext(folder=Path(".")))
        )

    def test_a_raising_probe_never_escapes(self):
        def boom(_ctx):
            raise RuntimeError("probe exploded")

        dp.PROBES["_test_boom"] = boom
        try:
            self.assertIsNone(
                dp.run_probe("_test_boom", dp.ProbeContext(folder=Path(".")))
            )
        finally:
            del dp.PROBES["_test_boom"]


class SyncDeferralPairingTests(unittest.TestCase):
    """`kg_sync_no_embedding_backend` — locked emitter + the paired clear."""

    def setUp(self):
        self.source = SYNC_SCRIPT.read_text(encoding="utf-8")

    def test_emitter_routes_through_the_locked_emitter(self):
        """The LAST shipped raw-triplet writer, and the most dangerous one: it
        runs as a subprocess while install.py's finalize is live."""
        self.assertIn(
            "from vco_lib.deferral_emit import DeferralEntry, emit", self.source
        )
        self.assertIn("emit(install_root, entry)", self.source)
        code = "\n".join(
            ln for ln in self.source.splitlines()
            if not ln.lstrip().startswith("#")
        )
        self.assertNotIn("DeferralReport.read(install_root)", code)
        self.assertNotIn("report.write(install_root)", code)

    def test_clear_is_paired_at_the_narrow_home(self):
        """decision #12: only the END of a fully successful TREE sync clears
        it. "A backend is reachable" is not the same claim as "the seed ran"."""
        self.assertIn("def _clear_sync_deferral_no_backend", self.source)
        self.assertIn(
            "resolve_conditions(install_root, (_SYNC_NO_BACKEND_CID,))", self.source
        )
        # The call site sits under the --all branch, gated on zero failures.
        idx = self.source.index("_clear_sync_deferral_no_backend(PROJECT_ROOT)")
        window = self.source[idx - 400:idx]
        self.assertIn("if total_fail == 0:", window)

    def test_partial_sync_does_not_clear(self):
        """A run with failures must NOT clear — the next clean run will."""
        idx = self.source.index("_clear_sync_deferral_no_backend(PROJECT_ROOT)")
        window = self.source[idx - 400:idx + 200]
        self.assertIn("if total_fail == 0:", window)
        self.assertNotIn("_clear_sync_deferral_no_backend(PROJECT_ROOT)\n            sys.exit(1)", window)

    def test_docs_only_sync_does_not_clear(self):
        """`--all-docs` syncs no KG nodes, so it cannot prove the seed ran."""
        all_docs_idx = self.source.index('elif sys.argv[1] == "--all-docs":')
        tail = self.source[all_docs_idx:all_docs_idx + 400]
        self.assertNotIn("_clear_sync_deferral_no_backend", tail)


if __name__ == "__main__":
    unittest.main()
