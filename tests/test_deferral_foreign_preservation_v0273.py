# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-2 (v0.2.73): install.py's deferral write must not clobber FOREIGN entries.

``UPDATE_DEFERRED.md`` has multiple writer families (install.py,
``vco_lib.project_init``, Rust emitters, background resync children).
install.py rebuilds the file from a fresh in-memory ``DeferralReport`` at end
of run — pre-A-2 that silently DELETED any persisted entry whose condition
install.py did not re-detect that run (e.g. a pending codegraph-resync
deferral, a chunker-preset overhaul entry).

Covers:
  * ``condition_is_owned`` — exact IDs + prefix families.
  * ``DeferralReport.merge_from_disk`` — foreign preserved, owned excluded,
    in-memory entry wins over the disk copy, missing/corrupt file → 0 merged.
  * end-to-end: seed → run adds its own entries → write → FOREIGN entry
    survives an update that did not re-detect it (the A-2 acceptance test).
  * install.py's ownership set does NOT claim the resync ledger entry.
"""

from __future__ import annotations

import re
import sys
import unittest
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
    condition_is_owned,
)


def _entry(cid: str, title: str = "T") -> DeferralEntry:
    return DeferralEntry(
        condition_id=cid,
        title=title,
        detected="detected text",
        why_deferred="needs consent",
        command_to_apply="some-command --apply",
        severity="warning",
    )


class TestConditionIsOwned(unittest.TestCase):
    def test_exact_id_match(self):
        self.assertTrue(condition_is_owned("a_cond", {"a_cond", "b_cond"}))
        self.assertFalse(condition_is_owned("c_cond", {"a_cond", "b_cond"}))

    def test_prefix_match(self):
        self.assertTrue(
            condition_is_owned(
                "schema_migration_failed_xyz", set(),
                ("schema_migration_failed_",),
            )
        )
        self.assertFalse(
            condition_is_owned(
                "chunker_preset_overhaul", set(),
                ("schema_migration_failed_",),
            )
        )

    def test_empty_prefix_never_matches_everything(self):
        # A stray "" in the prefix tuple must not claim every condition.
        self.assertFalse(condition_is_owned("anything", set(), ("",)))


class TestMergeFromDisk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _persist(self, *entries: DeferralEntry) -> None:
        rep = DeferralReport()
        for e in entries:
            rep.add_entry(e)
        rep.write(self.folder)

    def test_foreign_entry_merged(self):
        self._persist(_entry("chunker_preset_overhaul"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(self.folder, exclude_ids={"owned_thing"})
        self.assertEqual(merged, 1)
        self.assertTrue(rep.has_condition("chunker_preset_overhaul"))

    def test_owned_entry_excluded(self):
        self._persist(_entry("owned_thing"), _entry("foreign_thing"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(self.folder, exclude_ids={"owned_thing"})
        self.assertEqual(merged, 1)
        self.assertFalse(rep.has_condition("owned_thing"))
        self.assertTrue(rep.has_condition("foreign_thing"))

    def test_owned_prefix_excluded(self):
        self._persist(_entry("dyn_family_abc"), _entry("foreign_thing"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(
            self.folder, exclude_ids=set(), exclude_prefixes=("dyn_family_",)
        )
        self.assertEqual(merged, 1)
        self.assertFalse(rep.has_condition("dyn_family_abc"))

    def test_in_memory_entry_wins_over_disk(self):
        self._persist(_entry("shared_cid", title="stale disk copy"))
        rep = DeferralReport()
        rep.add_entry(_entry("shared_cid", title="fresh in-memory"))
        merged = rep.merge_from_disk(self.folder, exclude_ids=set())
        self.assertEqual(merged, 0)
        titles = [e.title for e in rep.entries if e.condition_id == "shared_cid"]
        self.assertEqual(titles, ["fresh in-memory"])

    def test_missing_file_merges_nothing(self):
        rep = DeferralReport()
        self.assertEqual(rep.merge_from_disk(self.folder, exclude_ids=set()), 0)
        self.assertEqual(len(rep), 0)

    def test_corrupt_file_soft_fails(self):
        target = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\x00\xff not markdown \x00")
        rep = DeferralReport()
        # Must not raise; unparseable content merges nothing.
        self.assertEqual(rep.merge_from_disk(self.folder, exclude_ids=set()), 0)


class TestForeignSurvivesUpdateWrite(unittest.TestCase):
    """The A-2 acceptance test: a FOREIGN entry survives a full
    seed → detect-own-conditions → write cycle that does not re-detect it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_foreign_survives_write_owned_drops(self):
        # Previous state on disk: one foreign entry (another writer family's)
        # + one owned entry whose condition is now resolved (not re-detected).
        prior = DeferralReport()
        prior.add_entry(_entry("codegraph_embed_resync_pending"))
        prior.add_entry(_entry("owned_resolved_thing"))
        prior.write(self.folder)

        # The update run: fresh report, seeded from disk, re-detects only its
        # own NEW condition; the owned_resolved_thing is NOT re-detected.
        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids={"owned_resolved_thing"})
        run.add_entry(_entry("newly_detected_owned"))
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertIn("codegraph_embed_resync_pending", cids,
                      "foreign entry must survive the update write")
        self.assertIn("newly_detected_owned", cids)
        self.assertNotIn("owned_resolved_thing", cids,
                         "owned entries keep drop-when-absent semantics")

    def test_only_foreign_entries_still_written(self):
        prior = DeferralReport()
        prior.add_entry(_entry("foreign_only"))
        prior.write(self.folder)

        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids={"anything_owned"})
        wrote = run.write(self.folder)
        self.assertTrue(wrote, "a foreign-only report must still write the file")
        after = DeferralReport.read(self.folder)
        self.assertTrue(after.has_condition("foreign_only"))

    def test_collision_cids_drop_when_absent(self):
        """v0.2.88 (MAJOR-3): once the two update-flow collision cids are in
        install.py's owned set, a run that does NOT re-detect them (the GUI
        resolved the collision) drops them on the single final write — even
        with a genuine foreign entry present that must survive."""
        prior = DeferralReport()
        prior.add_entry(_entry("untracked_collision_divergent"))
        prior.add_entry(_entry("autostash_pop_conflict"))
        prior.add_entry(_entry("codegraph_embed_resync_pending"))  # foreign
        prior.write(self.folder)

        owned = {"untracked_collision_divergent", "autostash_pop_conflict"}
        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids=owned)
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertNotIn("untracked_collision_divergent", cids,
                         "resolved untracked-collision row must self-clear")
        self.assertNotIn("autostash_pop_conflict", cids,
                         "resolved autostash-pop row must self-clear")
        self.assertIn("codegraph_embed_resync_pending", cids,
                      "a genuine foreign entry must still survive")

    def test_resolve_conditions_settles_collision_rows(self):
        """v0.2.88 (MAJOR-3): the GUI resolver's direct settle path
        (`deferral_emit.resolve_conditions`) removes the two collision rows
        immediately, deleting the file when nothing else remains."""
        from vco_lib.deferral_emit import resolve_conditions  # noqa: PLC0415

        prior = DeferralReport()
        prior.add_entry(_entry("untracked_collision_divergent"))
        prior.add_entry(_entry("autostash_pop_conflict"))
        prior.write(self.folder)

        removed = resolve_conditions(
            self.folder,
            ["untracked_collision_divergent", "autostash_pop_conflict"],
        )
        self.assertEqual(removed, 2, "both present rows must be counted resolved")
        target = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(target.exists(),
                         "settling the only two rows must delete the report file")
        # Resolving absent ids again is a safe no-op (count 0).
        self.assertEqual(
            resolve_conditions(self.folder, ["autostash_pop_conflict"]), 0
        )


class TestInstallOwnershipSet(unittest.TestCase):
    """Guards on install.py's ownership constants.

    v0.2.91 WP-B: the ownership FACT moved out of an install.py literal and
    into `vco_lib/deferral_conditions.toml` (rows whose
    `clear_probe = "owned-drop-when-absent"`). These guards therefore assert
    against the RESOLVED set instead of a source substring — which is strictly
    stronger: the old text scan would have passed on a set that failed to load,
    and would have missed an id owned via a glob family.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        from vco_lib import deferral_registry as _dr  # noqa: PLC0415

        cls.owned = set(_dr.install_owned_ids())
        cls.prefixes = tuple(_dr.install_owned_prefixes())

    def test_resync_ledger_cid_not_owned(self):
        """`codegraph_embed_resync_pending` must NEVER enter the owned set —
        it is an owed-work ledger entry resolved explicitly by the probe.
        Owning it would silently clobber it on every update (the A-2 bug)."""
        self.assertNotIn("codegraph_embed_resync_pending", self.owned)
        self.assertFalse(
            any(
                "codegraph_embed_resync_pending".startswith(p)
                for p in self.prefixes
            ),
            "no owned prefix family may swallow the resync ledger cid",
        )

    def test_update_flow_collision_cids_owned(self):
        """v0.2.88 (MAJOR-3): the two launcher-emitted update-flow collision
        deferrals must be in the owned set so a completed GUI resolution
        drop-when-absent self-clears them (the exact update_resume_required
        lifecycle) instead of nagging every session forever."""
        self.assertIn("untracked_collision_divergent", self.owned)
        self.assertIn("autostash_pop_conflict", self.owned)

    def test_owned_set_and_prefixes_exist(self):
        # The constants keep their names + types; only their SOURCE moved.
        self.assertIn(
            "_INSTALL_OWNED_CONDITION_IDS = _deferral_registry.install_owned_ids()",
            self.source,
        )
        self.assertIn(
            "_INSTALL_OWNED_CONDITION_PREFIXES = "
            "_deferral_registry.install_owned_prefixes()",
            self.source,
        )
        self.assertTrue(self.owned, "the derived owned set must not be empty")
        self.assertTrue(self.prefixes, "owned prefix families must not be empty")
        # P2c-b (v0.2.75): the seed/finalize choreography moved to
        # vco_lib.install_deferral_flow — main() must hold the A-2 seed
        # call site (the flow is constructed with the owned sets above).
        self.assertIn("_deferral_flow.seed(", self.source)

    def test_mid_run_write_removed(self):
        """A-11: exactly ONE write moment remains — P2c-b moved it into
        `InstallDeferralFlow.finalize()` (late-merge + single write), so
        install.py must hold exactly ONE `_deferral_flow.finalize(` call
        site and ZERO direct `_deferral_report.write(` calls. The
        flow-module-side single-write guard lives in
        tests/test_install_deferral_flow_v0275.py. Comment/docstring
        mentions are excluded by matching code lines only."""
        def _code_lines(needle):
            return [
                ln for ln in self.source.splitlines()
                if needle in ln
                and not ln.lstrip().startswith("#")
                and "``" not in ln
            ]

        self.assertEqual(_code_lines("_deferral_report.write("), [],
                         "no direct report write may remain in install.py")
        finalize_lines = _code_lines("_deferral_flow.finalize(")
        self.assertEqual(len(finalize_lines), 1, finalize_lines)
        self.assertIn("_final = _deferral_flow.finalize(", finalize_lines[0])


class TestRustEmitterSeveritiesValid(unittest.TestCase):
    """v0.2.75: every Rust command that shells out to build a DeferralEntry
    must pass a severity in SEVERITY_ORDER — otherwise __post_init__ raises
    ValueError, the helper exits non-zero, and the deferral is SILENTLY never
    written (the error is logged + swallowed). module_updates.rs shipped
    ``severity="medium"`` (not in the set) since v0.2.52, so its
    ``module_update_partial_failure`` deferral never landed. This source-scan
    guards the whole class. MUST MATCH vco_lib/deferral_report.py SEVERITY_ORDER.
    """

    def test_all_rust_literal_severities_are_valid(self) -> None:
        from vco_lib.deferral_report import SEVERITY_ORDER  # noqa: PLC0415
        src = REPO_ROOT / "launcher" / "src-tauri" / "src"
        cmds = src / "commands"
        services = src / "services"
        # v0.2.77 Part 7c task 4 consolidated the Rust deferral emitters onto
        # the shared `services::deferral::emit_deferral_entry(..,
        # &DeferralEntryFields { .., severity: "<lit>" })` writer. The severity
        # literal now appears as a struct FIELD at each call-site, e.g.
        #   severity: "warning",
        # (previously it was `sev_py = py_quote("warning")` or an inline
        # `severity=\"info\"` inside the generated `-c` format string — both
        # shapes are gone). We keep matching the OLD shapes too so this guard
        # still catches any lingering / re-introduced inline emitter.
        pat = re.compile(
            r'(?:severity:\s*"([a-z]+)"'                 # new struct-field shape
            r'|sev(?:erity)?_py\s*=\s*py_quote\("([a-z]+)"\)'  # old py_quote var
            r'|severity=\\"([a-z]+)\\")'                 # old inline -c literal
        )
        found = 0
        for rs in sorted(cmds.glob("*.rs")) + sorted(services.glob("*.rs")):
            text = rs.read_text(encoding="utf-8")
            for m in pat.finditer(text):
                lit = m.group(1) or m.group(2) or m.group(3)
                found += 1
                with self.subTest(file=rs.name, severity=lit):
                    self.assertIn(
                        lit, SEVERITY_ORDER,
                        f"{rs.name} emits severity={lit!r}, not in "
                        f"{SEVERITY_ORDER} — the deferral would never be "
                        f"written (silent ValueError in the shelled helper).",
                    )
        # storage_ux passes a *variable* severity (runtime-checked at its
        # callsite), and chunker routes through a different vco_lib helper —
        # neither carries a literal here, which is fine. We only assert we
        # scanned at least the known literal emitters (codegraph, module_updates,
        # git_user_editable_merge, projects_v2 rename — 4 struct-field sites).
        self.assertGreaterEqual(found, 4, "severity-literal scan found too few "
                                "emitters — the regex likely drifted from the "
                                "Rust source shape (Part 7c moved severities to "
                                "DeferralEntryFields struct fields).")


class TestLightweightPathSeedsBeforeWrite(unittest.TestCase):
    """v0.2.75: the install.py --lightweight branch builds a FRESH
    DeferralReport and must merge_from_disk (foreign-only) BEFORE write(),
    or an empty lightweight run unlinks UPDATE_DEFERRED.{md,json} and destroys
    pending foreign deferrals. Guards the A-2 seed on the lightweight path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_lightweight_merges_before_write(self) -> None:
        # The seed merge must appear between the fresh construction and the
        # write, with the install-owned exclusions.
        lw_construct = self.source.index("_lightweight_deferral = DeferralReport()")
        lw_write = self.source.index("_lightweight_deferral.write(")
        window = self.source[lw_construct:lw_write]
        self.assertIn("_lightweight_deferral.merge_from_disk(", window,
                      "lightweight path must A-2 seed before write()")
        self.assertIn("_INSTALL_OWNED_CONDITION_IDS", window)
        self.assertIn("_INSTALL_OWNED_CONDITION_PREFIXES", window)


if __name__ == "__main__":
    unittest.main()
