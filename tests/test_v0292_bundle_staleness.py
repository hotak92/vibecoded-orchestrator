# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-D — bundle-staleness census: verdicts, honesty, ledger pair,
self-check, and the state-keyed chunker-revision gate.

The census answers ONE question per registered project — "would a bundle
update change files here?" — by running the ONE bundle engine in dry-run
(R27: no second verdict implementation). Verdicts are state-keyed on file
hashes (R26): a project whose manifest is three releases old classifies
exactly like one release old. Anything not positively proven is ``unknown``
with a reason; "unknown" never renders as "current" — the conflation that
kept a launcher saying "up to date" for five weeks.

Hermetic: launcher.db is a fixture sqlite file pointed at by
``VCT_LAUNCHER_DB_PATH`` (the reader's sanctioned env override); every
folder is tmp_path. No real registry, Weaviate collection, or user state is
touched. The subprocess legs pin the REAL CLIs (``install-bundle``,
``python -m vco_lib.bundle_staleness``) so argv contracts cannot drift from
what the tests construct in-process.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402
from vco_lib import bundle_staleness, chunker_revision, doctor, project_init  # noqa: E402

FIXTURE_SEMVER = "9.9.99"


def _write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f"[project]\nname = \"fixture\"\nversion = \"{version}\"\n",
        encoding="utf-8",
    )


def _make_registry_db(db_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """launcher.db (REAL schema) seeded with ``projects`` rows.

    ``rows`` are ``(id, name, folder_path, host)`` — the four columns the
    census reads. Everything else (``slug``, timestamps,
    ``folder_missing_at_last_boot``) comes from the shipped migrations.
    """
    make_launcher_db(db_path, projects=[
        {"project_id": pid, "name": name, "folder_path": folder, "host": host,
         "created_at": 0, "updated_at": 0}
        for pid, name, folder, host in rows
    ])


def _tree_digest(root: Path) -> str:
    """Byte digest of every file under root (path → sha256), excluding
    nothing — the census must leave the WHOLE project tree identical."""
    manifest = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest.append(
                (
                    str(path.relative_to(root)),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return hashlib.sha256(repr(manifest).encode()).hexdigest()


class _CensusFixture(unittest.TestCase):
    """orchestrator fixture + three projects (current / stale / no-manifest)
    + a gone-folder row, registered in a fixture launcher.db."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wpd-census-"))
        self.orch = self.tmp / "orchestrator"
        self.orch.mkdir()
        _make_fake_orchestrator(self.orch)
        _write_pyproject(self.orch, FIXTURE_SEMVER)
        # `looks_like_orchestrator_root` (the ONE definition, in vco_lib.paths)
        # requires vco_lib/ + .claude/ — the doctor probe gates on it.
        (self.orch / "vco_lib").mkdir()
        (self.orch / ".claude").mkdir()

        self.p_current = self.tmp / "proj-current"
        self.p_stale = self.tmp / "proj-stale"
        self.p_nomanifest = self.tmp / "proj-nomanifest"
        self.p_gone = self.tmp / "proj-gone"
        for folder in (self.p_current, self.p_stale, self.p_nomanifest):
            folder.mkdir()
        # gone folder deliberately NOT created

        # stale: installed against v1 templates, then the orchestrator
        # shipped a newer foo.sh — the "bundle from three releases back"
        # shape (the manifest records v1's shipped hash).
        project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=False,
        )
        (self.orch / "templates" / "hooks" / "foo.sh").write_text(
            "#!/bin/sh\necho v2 -- shipped three releases later\n",
            encoding="utf-8",
        )
        # current: installed AFTER the change — nothing an update would do.
        project_init.install_project_bundle(
            self.p_current, orchestrator_root=self.orch, update_mode=False,
        )
        # nomanifest: folder exists, never installed

        self.db = self.tmp / "launcher.db"
        _make_registry_db(self.db, [
            ("id-current", "Current Project", str(self.p_current), "base"),
            ("id-stale", "Stale Project", str(self.p_stale), "base"),
            ("id-nomanifest", "NoManifest Project", str(self.p_nomanifest), "base"),
            ("id-gone", "Gone Project", str(self.p_gone), "base"),
        ])
        self._old_db_env = os.environ.get("VCT_LAUNCHER_DB_PATH")
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.db)

    def tearDown(self) -> None:
        if self._old_db_env is None:
            os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        else:
            os.environ["VCT_LAUNCHER_DB_PATH"] = self._old_db_env
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _row(self, payload: dict, needle: str) -> dict:
        for row in payload["projects"]:
            if row["id"] == needle:
                return row
        raise AssertionError(f"no row {needle!r} in census")


class TestCensusVerdicts(_CensusFixture):
    def test_one_current_one_stale_two_unknown(self) -> None:
        """The plan's acceptance shape: current / stale / unknown counted
        honestly, with the stale one LISTING the changed files."""
        payload = bundle_staleness.run_census(
            self.orch, refresh_ledger=False
        )
        self.assertEqual(payload["registry"], "launcher.db")
        self.assertEqual(
            payload["summary"],
            {"current": 1, "stale": 1, "unknown": 2},
        )
        self.assertEqual(payload["running"]["version"], FIXTURE_SEMVER)
        self.assertEqual(payload["schema"], 1)

        stale = self._row(payload, "id-stale")
        self.assertEqual(stale["verdict"], "stale")
        self.assertEqual(stale["reason"], "files_changed")
        self.assertIn(".claude/hooks/foo.sh", stale["changed_files"])

        current = self._row(payload, "id-current")
        self.assertEqual(current["verdict"], "current")
        self.assertEqual(current["reason"], "noop")

        self.assertEqual(
            self._row(payload, "id-nomanifest")["reason"], "manifest_missing"
        )
        self.assertEqual(
            self._row(payload, "id-gone")["reason"], "folder_missing"
        )

    def test_recorded_version_is_display_only_state_keyed_verdict(self) -> None:
        """R26 pin: a manifest 'three releases back' whose recorded version
        is a legacy SHA still classifies by FILE HASHES, and the SHA reads
        as a commit, never a version."""
        payload = bundle_staleness.run_census(
            self.orch, refresh_ledger=False
        )
        stale = self._row(payload, "id-stale")
        # The fixture manifest was written by THIS build → semver recorded.
        self.assertEqual(stale["recorded"]["version"], FIXTURE_SEMVER)
        # Now hand-write a legacy SHA-shaped manifest into a fresh project
        # and confirm the reader maps it to (None, <sha>).
        p_legacy = self.tmp / "proj-legacy"
        p_legacy.mkdir()
        (p_legacy / ".claude").mkdir()
        (p_legacy / ".claude" / ".vco-manifest.json").write_text(
            json.dumps({
                "schema_version": 1,
                "vco_version": "5155a553",
                "files": {},
            }),
            encoding="utf-8",
        )
        self.db.unlink()
        _make_registry_db(self.db, [
            ("id-legacy", "Legacy", str(p_legacy), "base"),
        ])
        payload = bundle_staleness.run_census(self.orch, refresh_ledger=False)
        row = self._row(payload, "id-legacy")
        self.assertIsNone(row["recorded"]["version"])
        self.assertEqual(row["recorded"]["commit"], "5155a553")

    def test_unparseable_manifest_is_unknown(self) -> None:
        (self.p_nomanifest / ".claude").mkdir(exist_ok=True)
        (self.p_nomanifest / ".claude" / ".vco-manifest.json").write_text(
            "definitely not json {", encoding="utf-8"
        )
        payload = bundle_staleness.run_census(self.orch, refresh_ledger=False)
        self.assertEqual(
            self._row(payload, "id-nomanifest")["reason"],
            "manifest_unparseable",
        )

    def test_text_summary_always_prints_all_three_counts(self) -> None:
        """Honesty rule: the human summary line carries current/stale/unknown
        together — a count that can silently disappear is a count that can
        silently become '0 stale'."""
        payload = bundle_staleness.run_census(
            self.orch, refresh_ledger=False
        )
        text = bundle_staleness.render_text(payload)
        self.assertIn("1 current, 1 stale, 2 unknown", text)
        self.assertIn("Stale Project", text)
        self.assertIn(".claude/hooks/foo.sh", text)
        self.assertIn("remedy", text)

    def test_registry_unavailable_reports_unknown_never_zero_stale(self) -> None:
        """Fresh root install before first launcher boot: no registry. The
        census must SAY so — a vacuous '0 stale' is the original lie."""
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "no-such.db")
        payload = bundle_staleness.run_census(
            self.orch, refresh_ledger=False
        )
        self.assertEqual(payload["registry"], "unavailable")
        self.assertEqual(payload["projects"], [])
        text = bundle_staleness.render_text(payload)
        self.assertIn("registry unavailable", text)
        self.assertIn("could be determined", text)
        self.assertNotIn("0 current, 0 stale, 0 unknown\nremedy", text)

    def test_dry_run_census_has_zero_side_effects(self) -> None:
        """The census never mutates a project: byte-digest the WHOLE tree
        (including .claude/state and the manifest) before/after."""
        digests = {
            str(folder): _tree_digest(folder)
            for folder in (self.p_current, self.p_stale, self.p_nomanifest)
        }
        # Pre-seed a stale chunker revision so the gate WOULD have things
        # to do if it mis-ran under dry-run.
        state_dir = self.p_stale / ".claude" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "chunker-revision.json").write_text(
            json.dumps({"revision": "v0.0.0-fixture"}), encoding="utf-8"
        )
        digests[str(self.p_stale)] = _tree_digest(self.p_stale)
        before_root_ledger = (self.orch / ".claude" / "context").exists()

        bundle_staleness.run_census(self.orch, refresh_ledger=False)

        for folder, digest in digests.items():
            self.assertEqual(
                _tree_digest(Path(folder)), digest,
                f"census mutated {folder}",
            )
        # No deferral ledger appeared at the root (refresh off) and none in
        # any project (dry-run emits nothing).
        self.assertFalse(before_root_ledger)
        for folder in (self.p_current, self.p_stale, self.p_nomanifest):
            self.assertFalse(
                (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").exists(),
                f"dry-run census emitted a deferral into {folder}",
            )

    def test_census_cost_stays_under_budget(self) -> None:
        """The plan's measurement gate: < 1s per project (fixture is tiny;
        the real-machine measurement — 7 projects, 0.135 s/project — is in
        the WP-D report). Regression guard against accidental per-file
        subprocess spawns in the classification path."""
        t0 = time.perf_counter()
        bundle_staleness.run_census(self.orch, refresh_ledger=False)
        elapsed = time.perf_counter() - t0
        self.assertLess(
            elapsed, 4 * 1.0,
            f"census took {elapsed:.2f}s for 4 fixture projects",
        )


class TestLedgerPair(_CensusFixture):
    def _root_ledger_text(self) -> str:
        path = self.orch / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_full_census_emits_then_resolves_the_entry(self) -> None:
        """The PAIR (paired-resolution): stale > 0 → one entry naming the
        count, the project and the remedy; stale == 0 → entry gone."""
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        text = self._root_ledger_text()
        self.assertIn("project_bundles_stale", text)
        self.assertIn("Stale Project", text)
        self.assertIn("1 of 4", text)
        self.assertIn("install-bundle", text)  # the exact CLI remedy

        # Heal: update the stale project through the real engine, then
        # flip + re-census → the entry is resolved (file removed when the
        # report holds no other entries). The registry is narrowed to the
        # two DETERMINABLE projects first: resolution requires a census
        # that positively accounted for the whole population (m2 — zero
        # stale AND zero unknown), and the fixture's other two rows (a
        # deleted folder, a never-installed folder) can never be determined.
        project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=True,
        )
        self.db.unlink()
        _make_registry_db(self.db, [
            ("id-current", "Current Project", str(self.p_current), "base"),
            ("id-stale", "Stale Project", str(self.p_stale), "base"),
        ])
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertNotIn(
            "project_bundles_stale", self._root_ledger_text(),
            "a census with stale == 0 must resolve the entry",
        )

    def test_in_engine_update_flips_row_and_clears_ledger(self) -> None:
        """R27 surface (b): a REAL project bundle update proves itself
        current (self-check) and flips the census state WITHOUT a full
        re-census — update_all_projects costs N flips, not N² dry-runs."""
        # Determinable population only: the flip's ledger refresh may clear
        # the entry only when the census it updates has zero stale AND zero
        # unknown (m2), so the fixture's undeterminable rows are dropped.
        self.db.unlink()
        _make_registry_db(self.db, [
            ("id-current", "Current Project", str(self.p_current), "base"),
            ("id-stale", "Stale Project", str(self.p_stale), "base"),
        ])
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        state = bundle_staleness.read_census_state(self.orch)
        self.assertIsNotNone(state)
        self.assertEqual(
            self._row_from_state(state, "id-stale")["verdict"], "stale"
        )

        result = project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertEqual(result["errors"], [])
        state = bundle_staleness.read_census_state(self.orch)
        self.assertEqual(
            self._row_from_state(state, "id-stale")["verdict"], "current",
            "the engine must flip the just-updated project in the census state",
        )
        self.assertEqual(
            state["summary"]["stale"], 0,
            "summary must be recomputed from the flip",
        )
        self.assertNotIn(
            "project_bundles_stale", self._root_ledger_text(),
            "the flip's ledger refresh must resolve the now-stale-0 entry",
        )

    def test_failed_self_check_records_unknown_not_current(self) -> None:
        """BLOCKER-1: ``self_check`` REPORTS (it never raises), so a flip
        that ignores its warnings turns a demonstrably-failed install into
        a ``current`` census row + a resolved ledger entry — the "said it
        updated, didn't" defect this whole feature exists to end.

        Seed a failure the way a Windows AV/OneDrive lock presents: the
        shipped destination cannot be written (a directory stands where the
        file goes), so the engine preserves, the self-check finds the file
        still differing, and the project must NOT read as current.
        """
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertIn("project_bundles_stale", self._root_ledger_text())

        blocked = self.p_stale / ".claude" / "hooks" / "foo.sh"
        blocked.unlink()
        blocked.mkdir()  # the write cannot land here

        result = project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertTrue(
            any("self-check" in w for w in result["warnings"]),
            f"fixture must produce a failing self-check, got "
            f"{result['warnings']}",
        )

        row = self._row_from_state(
            bundle_staleness.read_census_state(self.orch), "id-stale"
        )
        self.assertEqual(
            row["verdict"], "unknown",
            "a failed self-check must never flip the row to current",
        )
        self.assertEqual(row["reason"], "self_check_failed")
        entry = self._root_ledger_text()
        self.assertIn(
            "project_bundles_stale", entry,
            "an install that could not prove itself must not resolve the "
            "root ledger entry",
        )
        # The entry NAMES the project and the reason, so the failure is
        # actionable rather than a silent 'still there' badge.
        self.assertIn("Stale Project (self_check_failed)", entry)
        self.assertIn("the last bundle update did NOT land", entry)

    def test_unknown_only_census_keeps_the_entry_but_restates_it(self) -> None:
        """m2 arm A: 'I could not determine these projects' is not 'every
        project is current' — the entry must NOT resolve. And because it
        must not resolve, it must stop claiming what is no longer true: the
        re-rendered entry names the undetermined projects and their reasons,
        drops the stale-bundle claim, and carries the remedy that actually
        applies (fix/remove the registry row, not 'Update all bundles').
        """
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertIn("stale bundle", self._root_ledger_text())

        # Heal the ONE genuinely stale project; the two undeterminable ones
        # (deleted folder, never installed) remain.
        project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=True,
        )
        payload = bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertEqual(payload["summary"], {"current": 2, "stale": 0,
                                              "unknown": 2})

        text = self._root_ledger_text()
        self.assertIn(
            "project_bundles_stale", text,
            "an undetermined population must never resolve the entry",
        )
        # It no longer LIES about what it is...
        self.assertNotIn("are on a stale bundle", text)
        self.assertNotIn(bundle_staleness.REMEDY_GUI, text)
        self.assertIn("2 project(s) VCO could not determine", text)
        # ...and it names each project WITH its reason, so the user knows
        # which row to fix or remove (item 3: the reason reaches the text).
        self.assertIn("could NOT be determined", text)
        self.assertIn("Gone Project (folder_missing)", text)
        self.assertIn("NoManifest Project (manifest_missing)", text)
        # The remedy is about the undetermined rows, not a bundle update.
        self.assertIn("resolve each named cause", text)
        self.assertIn("remove the project from the launcher", text)
        self.assertIn("python -m vco_lib.bundle_staleness", text)

    def test_fully_determined_nothing_stale_census_resolves(self) -> None:
        """m2 arm B: the entry IS clearable — a census that accounts for
        every registered project and finds nothing stale removes it. This is
        what keeps arm A's never-resolve rule from being a permanent badge.
        """
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertIn("project_bundles_stale", self._root_ledger_text())

        project_init.install_project_bundle(
            self.p_stale, orchestrator_root=self.orch, update_mode=True,
        )
        # The user's action for the two undetermined rows: give the
        # never-installed folder a bundle, and drop the deleted project's
        # registry row. Both are exactly what the entry's remedy names.
        project_init.install_project_bundle(
            self.p_nomanifest, orchestrator_root=self.orch, update_mode=False,
        )
        self.db.unlink()
        _make_registry_db(self.db, [
            ("id-current", "Current Project", str(self.p_current), "base"),
            ("id-stale", "Stale Project", str(self.p_stale), "base"),
            ("id-nomanifest", "NoManifest Project",
             str(self.p_nomanifest), "base"),
        ])
        payload = bundle_staleness.run_census(self.orch, refresh_ledger=True)
        self.assertEqual(payload["summary"], {"current": 3, "stale": 0,
                                              "unknown": 0})
        self.assertNotIn(
            "project_bundles_stale", self._root_ledger_text(),
            "a fully-determined, nothing-stale census must clear the entry",
        )

    def test_flip_without_census_state_invents_nothing(self) -> None:
        self.assertFalse(
            bundle_staleness.record_project_now_current(self.orch, self.p_stale),
            "no census state on disk → no flip, no invented verdicts",
        )

    def _row_from_state(self, state: dict, needle: str) -> dict:
        for row in state["projects"]:
            if row["id"] == needle:
                return row
        raise AssertionError(needle)


class TestSelfCheck(unittest.TestCase):
    """The post-install proof that the bundle actually landed."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wpd-selfcheck-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "proj"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        _write_pyproject(self.orch, FIXTURE_SEMVER)

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _install(self) -> dict:
        return project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )

    def test_clean_install_self_check_is_silent(self) -> None:
        result = self._install()
        self.assertEqual(result["errors"], [])
        warnings = [w for w in result["warnings"] if "self-check" in w]
        self.assertEqual(warnings, [])

    def test_tampered_manifest_surfaces_a_warning(self) -> None:
        self._install()
        manifest_path = self.proj / ".claude" / ".vco-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["vco_version"] = "0.0.1-stale"  # lie about what shipped
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        warnings = bundle_staleness.self_check(
            self.proj, self.orch,
            update_mode=True, manifest_written=True,
        )
        self.assertTrue(
            any("vco_version" in w for w in warnings),
            f"manifest/run mismatch must warn, got {warnings}",
        )

    def test_deleted_shipped_file_surfaces_a_warning(self) -> None:
        self._install()
        target = self.proj / ".claude" / "hooks" / "foo.sh"
        target.unlink()
        warnings = bundle_staleness.self_check(
            self.proj, self.orch,
            update_mode=True, manifest_written=True,
        )
        self.assertTrue(
            any("still differ" in w for w in warnings),
            f"a file an update would re-create must warn, got {warnings}",
        )
        self.assertTrue(any("foo.sh" in w for w in warnings))

    def test_first_install_mode_does_not_false_alarm_on_preexisting(self) -> None:
        """Fresh-install self-check runs in fresh-install mode: a
        pre-existing divergent file (skip-existing) is NOT a mismatch."""
        (self.proj / ".claude" / "hooks").mkdir(parents=True)
        (self.proj / ".claude" / "hooks" / "foo.sh").write_text(
            "# user's own hook, predates VCO\n", encoding="utf-8"
        )
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        warnings = [w for w in result["warnings"] if "self-check" in w]
        self.assertEqual(warnings, [])

    def test_skip_kinds_run_is_not_flagged_for_skipped_files(self) -> None:
        """A run that deliberately skipped a kind (install.py's legacy
        --skip-materialize-claude-dir delegation) must not self-warn about
        the files it chose not to ship."""
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
            skip_kinds=frozenset({"hooks"}),
        )
        warnings = [w for w in result["warnings"] if "self-check" in w]
        self.assertEqual(warnings, [])


class TestChunkerRevisionGate(unittest.TestCase):
    """The state-keyed per-project chunker-resync gate (R26/R27).

    Supersedes the inert ``_crosses_chunker_boundary`` call site — the
    manifest's ``vco_version`` was a SHA, the comparator wanted semver, and
    the gate never fired for any real user. The replacement keys on the
    stored last-seen ``_CHUNKER_REVISION`` sentinel, so it fires for a user
    who skipped ANY number of releases.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wpd-chunker-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "proj"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        _write_pyproject(self.orch, FIXTURE_SEMVER)

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _state_path(self) -> Path:
        return self.proj / ".claude" / "state" / "chunker-revision.json"

    def _stored_revision(self) -> str | None:
        path = self._state_path()
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["revision"]

    def _store_revision(self, revision: str) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"revision": revision}), encoding="utf-8"
        )

    def _ledger_text(self) -> str:
        path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_first_observation_records_and_stays_silent(self) -> None:
        """A fresh install has nothing to re-chunk: the gate ARMS (records
        the current sentinel) without nagging."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        stored = self._stored_revision()
        self.assertIsNotNone(stored)
        self.assertEqual(stored, project_init.current_chunker_revision())
        self.assertNotIn("chunker_preset_overhaul_pending", self._ledger_text())

    def test_preexisting_project_with_no_sentinel_emits_the_resync_e2e(self) -> None:
        """The ACT arm of BLOCKER-A, end-to-end through the real engine.

        A project installed BEFORE v0.2.92 has a bundle manifest but no
        chunker sentinel — its KG was chunked under the old presets and owes
        a re-chunk. Round-3 shipped this arm with unit + string coverage
        only; round-4 (m2) asked for the e2e, because the signal it depends
        on (the manifest existing BEFORE this run writes its own) is an
        ordering property that only the real call site can exercise.

        Leave-alone twin: ``test_first_observation_records_and_stays_silent``
        — same call, no prior manifest, must stay silent.
        """
        manifest = self.proj / ".claude" / ".vco-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"vco_version": "deadbeef", "files": {}}),
            encoding="utf-8",
        )
        self.assertIsNone(self._stored_revision(), "fixture must start unstamped")

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )

        self.assertIn(
            "chunker_preset_overhaul_pending",
            self._ledger_text(),
            "a pre-v0.2.92 project (manifest present, sentinel absent) owes a "
            "re-chunk and must be told so",
        )
        self.assertEqual(
            self._stored_revision(),
            project_init.current_chunker_revision(),
            "the gate stamps after emitting, so the nag does not repeat",
        )

    def test_revision_change_emits_resync_deferral(self) -> None:
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        current = project_init.current_chunker_revision()
        self._store_revision("v0.0.0-many-releases-back")
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn("chunker_preset_overhaul_pending", self._ledger_text())
        self.assertTrue(
            any("chunker revision" in w.lower() for w in result["warnings"]),
            f"the envelope must surface the resync, got {result['warnings']}",
        )
        # Re-stamped: a THIRD run with no further change is silent.
        self._prune_ledger()
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertNotIn(
            "chunker_preset_overhaul_pending", self._ledger_text(),
            "same revision twice must not re-emit",
        )
        self.assertEqual(self._stored_revision(), current)

    def test_multi_revision_jump_fires_identically(self) -> None:
        """R26 pin: the gate is keyed on OBSERVED STATE. One sentinel step
        back and MANY sentinel steps back behave the same — there is no
        adjacency assumption left anywhere."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        current = project_init.current_chunker_revision()
        # "One step back": fabricate the immediately-previous sentinel.
        one_back = "v0.2.87" if current.startswith("v0.2.88") else "v-old-1"
        self._store_revision(one_back)
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn("chunker_preset_overhaul_pending", self._ledger_text())
        self._prune_ledger()
        # "Many steps back": the same behaviour.
        self._store_revision("v0.1.0-ancient")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn("chunker_preset_overhaul_pending", self._ledger_text())

    def test_dry_run_neither_emits_nor_stamps(self) -> None:
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self._store_revision("v0.0.0-old")
        self._prune_ledger()
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
            dry_run=True,
        )
        self.assertEqual(self._stored_revision(), "v0.0.0-old")
        self.assertNotIn("chunker_preset_overhaul_pending", self._ledger_text())

    def test_superseded_semver_gate_is_pinned_inert_on_sha_inputs(self) -> None:
        """The retired call site's arithmetic, pinned BOTH ways so the
        supersession is documented in code, not folklore: the helper still
        answers semver questions correctly, and still returns False on the
        SHA inputs real manifests carried — the structural inertness that
        made the per-project flow blind for its entire life (v0.2.47→91).
        """
        # Correct on semver inputs (kept for the shared-rule binding):
        self.assertTrue(
            project_init._crosses_chunker_boundary("0.2.45", "0.2.92")
        )
        # Structurally inert on every real pre-v0.2.92 manifest:
        self.assertFalse(
            project_init._crosses_chunker_boundary("5155a553", "0.2.92")
        )

    def _prune_ledger(self) -> None:
        path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if path.exists():
            path.unlink()


class TestReclonedProjectFallback(unittest.TestCase):
    """MAJOR-R5-4 — the re-cloned / manifest-deleted population.

    A project whose ``.claude/.vco-manifest.json`` was deleted (or that
    was re-cloned without it) has neither manifest nor chunker sentinel,
    so the round-3 BLOCKER-A signal ("prior manifest ⇒ owes a resync")
    classifies it FRESH while its Weaviate collection still holds objects
    chunked under the old presets — and the old KNOWN_ISSUES remedy
    (``kg-sync --all``) re-chunked nothing for it (the sync arms its plan
    comparison only while the ledger carries the crossing entry such a
    project never receives). The gate's fresh arm now consults one more
    witness: the project's REGISTERED bound KG class.

    Every e2e here drives the REAL bundle engine
    (``install_project_bundle`` → ``chunker_revision.gate``) with only the
    two external boundaries — identity resolution (launcher.db) and the
    Weaviate object count — faked.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wpd-r54-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "proj"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        _write_pyproject(self.orch, FIXTURE_SEMVER)

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- fixture helpers (same shape as TestChunkerRevisionGate) -----------

    def _state_path(self) -> Path:
        return self.proj / ".claude" / "state" / "chunker-revision.json"

    def _stored_revision(self) -> str | None:
        path = self._state_path()
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["revision"]

    def _ledger_text(self) -> str:
        path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _bundle_with_probe(
        self, *, count, kg_class: str = "Recloned_KnowledgeGraph"
    ):
        """Run the REAL bundle engine with the R5-4 evidence probe faked:
        ``_registered_kg_class`` resolves to ``kg_class`` and the default
        counter returns ``count`` (int) or ``None`` (Weaviate unreachable).
        Returns the envelope dict."""
        from unittest import mock

        with mock.patch.object(
            chunker_revision, "_registered_kg_class", return_value=kg_class
        ), mock.patch.object(
            chunker_revision, "_default_kg_counter", return_value=count
        ):
            return project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )

    # -- the three evidence outcomes --------------------------------------

    def test_reclone_nonempty_kg_emits_resync_e2e(self) -> None:
        """ACT: no manifest, no sentinel, REGISTERED KG class holds 7
        objects → the project's KG predates this install; the resync
        deferral fires (arming the printed ``kg-sync --all`` remedy — the
        emitted entry IS the arming signal) and the sentinel stamps after
        emitting so the nag does not repeat."""
        self.assertIsNone(self._stored_revision(), "fixture starts unstamped")
        self.assertFalse(
            (self.proj / ".claude" / ".vco-manifest.json").exists(),
            "fixture models a manifest-deleted / re-cloned project",
        )
        self._bundle_with_probe(count=7)
        self.assertIn(
            "chunker_preset_overhaul_pending",
            self._ledger_text(),
            "a re-cloned project whose KG already holds objects owes a "
            "re-chunk and must be told so",
        )
        self.assertEqual(
            self._stored_revision(),
            project_init.current_chunker_revision(),
            "the gate stamps after emitting, so the nag does not repeat",
        )

    def test_fresh_registered_project_empty_kg_stays_silent_e2e(self) -> None:
        """LEAVE-ALONE twin: same no-manifest/no-sentinel state, but the
        registered class is EMPTY (0 objects — a genuinely fresh add whose
        KG has not been seeded yet; a not-yet-created class also counts 0)
        → silent first-observation. This twin must stay GREEN under the
        act-arm mutation."""
        self._bundle_with_probe(count=0)
        self.assertNotIn(
            "chunker_preset_overhaul_pending", self._ledger_text(),
            "a fresh add owes nothing and must not be nagged",
        )
        self.assertEqual(
            self._stored_revision(),
            project_init.current_chunker_revision(),
            "fresh observation still stamps silently",
        )

    def test_unreachable_weaviate_is_no_verdict_and_no_stamp_e2e(self) -> None:
        """count UNKNOWN (Weaviate unreachable) → no stamp (a stamp would
        permanently misclassify the project as fresh from one transient
        outage), an honest no-verdict outcome in the envelope, and a NEXT
        run — with Weaviate back — that decides for real."""
        result = self._bundle_with_probe(count=None)
        self.assertIsNone(
            self._stored_revision(), "unknown must leave the sentinel unstamped"
        )
        self.assertNotIn(
            "chunker_preset_overhaul_pending", self._ledger_text()
        )
        self.assertTrue(
            any("kg-count-unknown" in w for w in result["warnings"]),
            f"the envelope must carry the honest no-verdict outcome, got "
            f"{result['warnings']}",
        )
        # Second run: the counter now answers — the question left open by
        # the outage is decided, not frozen.
        self._bundle_with_probe(count=3)
        self.assertIn(
            "chunker_preset_overhaul_pending", self._ledger_text()
        )
        self.assertEqual(
            self._stored_revision(),
            project_init.current_chunker_revision(),
        )

    def test_unregistered_folder_is_never_probed(self) -> None:
        """The identity is used ONLY when registered: an unresolvable
        registry (no launcher.db — the CLI-only / never-added case) keeps
        the pre-R5-4 fresh semantics AND never asks Weaviate. A
        basename-derived probe here would be the v0.2.92 W8 bug one level
        up: counting SOMEONE ELSE'S collection."""
        from unittest import mock

        spy = mock.Mock(return_value=99)
        with mock.patch.dict(
            os.environ,
            {"VCT_LAUNCHER_DB_PATH": str(self.tmp / "no" / "launcher.db")},
        ), mock.patch.object(chunker_revision, "_default_kg_counter", spy):
            project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        spy.assert_not_called()
        self.assertNotIn(
            "chunker_preset_overhaul_pending", self._ledger_text()
        )
        self.assertEqual(
            self._stored_revision(),
            project_init.current_chunker_revision(),
        )

    def test_gate_injected_counter_seam_decides_all_three_states(self) -> None:
        """The keyword-only ``count_kg_objects`` seam: the three R5-4
        decisions with the counter INJECTED (the default untouched) —
        act, leave-alone, unknown — including the no-stamp guarantee on
        the unknown arm."""
        from unittest import mock

        with mock.patch.object(
            chunker_revision,
            "_registered_kg_class",
            return_value="Recloned_KnowledgeGraph",
        ):
            act = self.tmp / "act"
            zero = self.tmp / "zero"
            unknown = self.tmp / "unknown"
            for p in (act, zero, unknown):
                p.mkdir()
            out_act = chunker_revision.gate(
                act, count_kg_objects=lambda cls: 3
            )
            out_zero = chunker_revision.gate(
                zero, count_kg_objects=lambda cls: 0
            )
            out_unknown = chunker_revision.gate(
                unknown, count_kg_objects=lambda cls: None
            )
        self.assertEqual(out_act, "resync-emitted")
        self.assertEqual(out_zero, "first-observation")
        self.assertEqual(out_unknown, "error:kg-count-unknown")
        for p in (act, zero):
            self.assertEqual(
                json.loads(
                    (p / ".claude" / "state" / "chunker-revision.json")
                    .read_text(encoding="utf-8")
                )["revision"],
                project_init.current_chunker_revision(),
            )
        self.assertFalse(
            (unknown / ".claude" / "state" / "chunker-revision.json").exists(),
            "unknown must not stamp",
        )


class TestDoctorProbe(_CensusFixture):
    def test_probe_registered_full_scope_only(self) -> None:
        self.assertIn("bundle_staleness", doctor.PROBES)
        _fn, scopes = doctor.PROBES["bundle_staleness"]
        self.assertEqual(scopes, (doctor.SCOPE_FULL,))

    def test_probe_reports_problem_summary_plus_per_project(self) -> None:
        findings = doctor.probe_bundle_staleness(
            self.orch, doctor.DoctorResolvers(), {}
        )
        probe_ids = {f.probe for f in findings}
        self.assertEqual(probe_ids, {"bundle_staleness"})
        summary = [
            f for f in findings if f.condition_id == doctor.CID_BUNDLES_STALE
        ]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].status, doctor.STATUS_PROBLEM)
        self.assertEqual(summary[0].fix, doctor.FIX_DEFER)
        self.assertIn("install-bundle", summary[0].command)
        self.assertEqual(summary[0].detail["stale"], 1)
        per_stale = [
            f for f in findings
            if f.status == doctor.STATUS_PROBLEM and not f.condition_id
        ]
        self.assertEqual(len(per_stale), 1)
        self.assertIn("Stale Project", per_stale[0].summary)
        unknowns = [
            f for f in findings if f.status == doctor.STATUS_UNKNOWN
        ]
        self.assertEqual(len(unknowns), 2)

    def test_probe_registry_unavailable_is_one_unknown(self) -> None:
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.tmp / "no-such.db")
        findings = doctor.probe_bundle_staleness(
            self.orch, doctor.DoctorResolvers(), {}
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("registry unavailable", findings[0].summary)

    def test_probe_declines_on_non_orchestrator_folder(self) -> None:
        self.assertEqual(
            doctor.probe_bundle_staleness(
                self.p_current, doctor.DoctorResolvers(), {}
            ),
            [],
        )


class TestSubprocessCLIs(_CensusFixture):
    """Drive the REAL CLIs — argv contracts are shipped code."""

    def _env(self) -> dict:
        # child_env pins the repo root FIRST on PYTHONPATH so these
        # `-m vco_lib.…` children import the checkout, never a stale
        # site-packages copy (§3.16).
        return child_env(VCT_LAUNCHER_DB_PATH=str(self.db))

    def test_install_bundle_cli_then_census_current(self) -> None:
        """The plan's end-to-end: a stale project, updated through the REAL
        `install-bundle` CLI, re-censuses as current and the root ledger
        entry is gone (paired clear via the in-engine flip)."""
        # Establish the stale census + ledger entry first.
        bundle_staleness.run_census(self.orch, refresh_ledger=True)
        ledger = self.orch / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(ledger.exists())

        proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.project_init", "install-bundle",
                "--folder", str(self.p_stale),
                "--orchestrator-root", str(self.orch),
                "--update", "--json",
            ],
            capture_output=True, text=True, timeout=120, env=self._env(),
            cwd=str(self.tmp),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"install-bundle failed: {proc.stderr[-2000:]}",
        )
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["errors"], [])
        self.assertEqual(envelope["vco_version"], FIXTURE_SEMVER)
        self.assertIn("vco_commit", envelope)

        # In-engine flip already cleared the root ledger entry.
        state = bundle_staleness.read_census_state(self.orch)
        self.assertEqual(
            next(
                r for r in state["projects"] if r["id"] == "id-stale"
            )["verdict"],
            "current",
        )

        census = bundle_staleness.run_census(
            self.orch, refresh_ledger=False
        )
        self.assertEqual(census["summary"]["stale"], 0)
        self.assertEqual(
            census["summary"]["current"], 2,
            "current + just-updated stale project",
        )

    def test_census_cli_json_contract(self) -> None:
        """§5.1 — the payload WP-D2's GUI command consumes."""
        proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.bundle_staleness",
                "--json", "--orchestrator-root", str(self.orch),
            ],
            capture_output=True, text=True, timeout=120, env=self._env(),
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        for key in (
            "schema", "running", "registry", "orchestrator_root",
            "projects", "summary", "remedy",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["registry"], "launcher.db")
        self.assertIn("gui", payload["remedy"])
        self.assertIn("--folder", payload["remedy"]["cli"])
        for row in payload["projects"]:
            for key in (
                "id", "name", "folder", "verdict", "reason", "recorded",
                "counts", "changed_files", "user_modified",
            ):
                self.assertIn(key, row)
            self.assertIn(
                row["verdict"], ("current", "stale", "unknown")
            )
            self.assertIn(
                row["reason"],
                (
                    "noop", "files_changed", "manifest_missing",
                    "manifest_unparseable", "folder_missing", "engine_error",
                    "registry_unavailable",
                ),
            )

    def test_json_census_is_read_only_unless_opted_in(self) -> None:
        """m1: `--json` is a QUERY. Answering it must not emit/resolve a
        root deferral or rewrite the census state — a read that mutates
        root state cannot be trusted by a GUI poll, a script, or a human
        who runs it twice. `--refresh-ledger` is the explicit opt-in.
        """
        base = [
            sys.executable, "-m", "vco_lib.bundle_staleness",
            "--json", "--orchestrator-root", str(self.orch),
        ]
        # Byte-digest the WHOLE orchestrator root (state file, ledger, and
        # everything else) plus the registry DB and its sqlite sidecars: a
        # read that mutates root state cannot be used by a GUI poll.
        root_before = _tree_digest(self.orch)
        db_before = sorted(p.name for p in self.db.parent.glob("launcher.db*"))
        proc = subprocess.run(
            base, capture_output=True, text=True, timeout=120,
            env=self._env(), cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        # It really censused (so "no side effects" is not "did nothing").
        self.assertEqual(json.loads(proc.stdout)["summary"]["stale"], 1)
        self.assertFalse(
            bundle_staleness.census_state_path(self.orch).exists(),
            "a --json census must not write the population state file",
        )
        self.assertFalse(
            (self.orch / ".claude" / "context" / "UPDATE_DEFERRED.md").exists(),
            "a --json census must not touch the root ledger",
        )
        self.assertEqual(
            _tree_digest(self.orch), root_before,
            "a --json census must not change ANY byte under the root",
        )
        self.assertEqual(
            sorted(p.name for p in self.db.parent.glob("launcher.db*")),
            db_before,
            "the registry read must not create sqlite -wal/-shm sidecars",
        )

        proc = subprocess.run(
            base + ["--refresh-ledger"], capture_output=True, text=True,
            timeout=120, env=self._env(), cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertTrue(bundle_staleness.census_state_path(self.orch).exists())
        self.assertIn(
            "project_bundles_stale",
            (self.orch / ".claude" / "context" / "UPDATE_DEFERRED.md")
            .read_text(encoding="utf-8"),
        )

    def test_census_cli_exit_2_without_root(self) -> None:
        proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.bundle_staleness",
                "--json", "--orchestrator-root",
                str(self.tmp / "no-such-root"),
            ],
            capture_output=True, text=True, timeout=60, env=self._env(),
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 2)

    def test_project_filter_reports_one_project_and_writes_nothing(self) -> None:
        """`--project` is REPORT-ONLY: a one-project summary must never be
        mistaken for the population's, so no census state and no ledger."""
        proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.bundle_staleness",
                "--json", "--orchestrator-root", str(self.orch),
                "--project", "id-stale",
            ],
            capture_output=True, text=True, timeout=60, env=self._env(),
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        payload = json.loads(proc.stdout)
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(payload["projects"][0]["verdict"], "stale")
        self.assertFalse(
            bundle_staleness.census_state_path(self.orch).exists(),
            "a filtered census must not write the population state file",
        )
        self.assertFalse(
            (self.orch / ".claude" / "context" / "UPDATE_DEFERRED.md").exists(),
            "a filtered census must not touch the root ledger",
        )


if __name__ == "__main__":
    unittest.main()
