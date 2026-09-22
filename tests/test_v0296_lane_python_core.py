# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 ship-gate fix lane (python core) — permanent pins.

One file per concern, in the order the lane's brief lists them:

* **F-B1** — the ``bundle_user_modified_preserved`` deferral names the cause
  that actually emits it (the adoption backup failing to write), and does not
  describe the "default-to-safety" policy that v0.2.84 retired. Since v0.2.84
  the backup-failure fallback is the ONLY path into this entry on an update,
  so the old text was wrong for 100 % of its emissions and its headline
  remedy (``--update --force``) overwrites with NO backup on the machine
  whose backup writes just failed.
* **D-5** — preserve bookkeeping has ONE home
  (``vco_lib.bundle_preserve.record_preserve``), reached from BOTH sites in
  ``install_project_bundle``. The mutation pin proves the migration: break the
  shared function and every call-site goes red.
* **L-11** — the read-only SQLite URI is percent-quoted, so a launcher.db path
  containing ``?`` / ``#`` / ``%`` opens instead of silently failing shut.
* **L-12** — ``vco_lib.module_gated_delivery``'s own soft-fail legs are pinned
  directly (the existing suite pins them only through the composite gate).
* **F-N5** — the two stale docstrings/comments in ``project_init`` state what
  the code does.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import bundle_ext, make_fake_orchestrator  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


class _BundleFixture(unittest.TestCase):
    """A non-root project with the bundle installed, ready for an update."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0296-lane-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch)
        self.ext = bundle_ext()
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

    def tearDown(self):
        import shutil
        for p in self.tmp.rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo(self) -> Path:
        return self.proj / ".claude" / "hooks" / f"foo.{self.ext}"

    def _bump(self, body: str) -> None:
        (self.orch / "templates" / "hooks" / f"foo.{self.ext}").write_text(
            body, encoding="utf-8",
        )

    def _update_with_failing_backup(self, error: Exception):
        """Run an update in which every adoption backup raises *error*."""
        self._foo().write_text("MY LOCAL EDIT\n", encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")
        with mock.patch.object(
            project_init, "_backup_bytes_for_adoption", side_effect=error,
        ):
            return project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )

    def _entry_text(self) -> str:
        """The rendered ledger entry for `bundle_user_modified_preserved`."""
        report = DeferralReport.read(self.proj)
        self.assertTrue(
            report.has_condition("bundle_user_modified_preserved"),
            "the backup-failure fallback must emit the deferral",
        )
        path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return path.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# F-B1 — the entry names the cause that emits it
# ═══════════════════════════════════════════════════════════════════════════


class UserModifiedDeferralNamesTheBackupFailure(_BundleFixture):
    """The text a user is told to read must name the actionable cause.

    RED against the pre-fix emitter on every assertion here: it said the
    files "were preserved (not overwritten)" by "Default-to-safety", never
    mentioned the backup, and led with `--update --force`.
    """

    def test_detected_names_the_failed_backup_write(self):
        self._update_with_failing_backup(OSError("No space left on device"))
        text = self._entry_text()
        self.assertIn("BACKUP WRITE FAILED", text)
        self.assertIn("No space left on device", text)

    def test_detected_does_not_claim_a_default_to_safety_policy(self):
        """The retired policy must not be described as the reason.

        `_file_action` classifies a divergent CODE-surface file as `adopt` in
        update mode and returns `preserve` only for `knowledge/**` (which
        NEW-1 routes to the SILENT list). So no emission of this entry is
        caused by "we default to preserving divergent files".
        """
        self._update_with_failing_backup(OSError("Permission denied"))
        text = self._entry_text()
        self.assertNotIn("Default-to-safety", text)
        self.assertNotIn("were preserved (not overwritten)", text)

    def test_force_is_the_last_resort_not_the_headline_remedy(self):
        """`--update --force` overwrites with NO backup. Offering it first,
        on the machine whose backup writes just failed, is the defect."""
        self._update_with_failing_backup(OSError("No space left on device"))
        text = self._entry_text()
        fix_backup = text.index("RECOMMENDED — fix the backup destination")
        force = text.index("--update --force")
        self.assertLess(
            fix_backup, force,
            "the recoverable remedy must precede the destructive one",
        )
        self.assertIn("LAST RESORT", text)
        self.assertIn("DESTROYS your local edits", text)

    def test_symlink_redirect_backup_failure_is_named_too(self):
        """The third real cause (a symlink under `.claude/backups/`) reaches
        the same entry and its message is carried verbatim."""
        self._update_with_failing_backup(
            OSError("adoption backup for x was redirected to a .vco-new sibling")
        )
        text = self._entry_text()
        self.assertIn("redirected to a .vco-new sibling", text)

    def test_no_failure_detail_still_states_the_divergence(self):
        """A caller that collected no `(path, error)` pairs must still get a
        usable entry — it states the divergence and stops, rather than
        inventing a cause it cannot prove."""
        from vco_lib.bundle_preserve import emit_user_modified_deferral

        emit_user_modified_deferral(
            self.proj, [".claude/hooks/foo.sh"], self.orch, None,
        )
        text = self._entry_text()
        self.assertIn(".claude/hooks/foo.sh", text)
        self.assertNotIn("The backup write failed with:", text)
        self.assertNotIn("Default-to-safety", text)


# ═══════════════════════════════════════════════════════════════════════════
# D-5 — ONE home for preserve bookkeeping, BOTH call-sites migrated
# ═══════════════════════════════════════════════════════════════════════════


class PreserveBookkeepingHasOneHome(_BundleFixture):
    """The migration pin. A partially-migrated extraction leaves the suite
    green while defeating the whole point, so the test MUTATES the shared
    function and requires each call-site to go red through it."""

    def test_adopt_backup_failure_site_routes_through_the_shared_home(self):
        sentinel: list[str] = []

        def _spy(op, shipped_hash, **kw):
            sentinel.append(op.dest_rel)
            kw["user_modified_paths"].append(op.dest_rel)

        with mock.patch("vco_lib.bundle_preserve.record_preserve", _spy):
            self._update_with_failing_backup(OSError("disk full"))
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{self.ext}"), sentinel,
            "the adopt-backup-failure fallback must call the shared "
            "`record_preserve`, not an inline twin",
        )

    def test_classified_preserve_site_routes_through_the_shared_home(self):
        """The `elif action == 'preserve'` branch — reached by a divergent
        user-owned `knowledge/**` node, the only dest `_file_action` still
        classifies `preserve` in update mode.

        `make_fake_orchestrator` ships no knowledge tree, so the shipped node
        is added here the same way `test_v0285_install_parity` does (the
        depth-1 allowlisted file is the only knowledge a NON-root project
        receives).
        """
        kdir = self.orch / "templates" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / "TAG_HIERARCHY.md").write_text("# v1\n", encoding="utf-8")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        node = self.proj / "knowledge" / "TAG_HIERARCHY.md"
        self.assertTrue(node.is_file(), "the fixture node must install")
        node.write_text("MY OWN KG EDIT\n", encoding="utf-8")
        (kdir / "TAG_HIERARCHY.md").write_text("# v2\n", encoding="utf-8")

        sentinel: list[str] = []

        def _spy(op, shipped_hash, **kw):
            sentinel.append(op.dest_rel)
            kw["knowledge_preserved_paths"].append(op.dest_rel)

        with mock.patch("vco_lib.bundle_preserve.record_preserve", _spy):
            project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        self.assertTrue(
            any(p.endswith("TAG_HIERARCHY.md") for p in sentinel),
            "the classified-preserve branch must call the shared "
            f"`record_preserve`; saw {sentinel}",
        )

    def test_the_rule_itself_lives_in_one_place(self):
        """Knowledge routing, manifest carry-forward and the `preserved` row
        are ONE function — changing the rule cannot land at one site only."""
        import inspect

        from vco_lib import bundle_preserve

        src = inspect.getsource(project_init.install_project_bundle)
        self.assertEqual(
            src.count('"reason": _preserve_reason'), 0,
            "an inline preserve-bookkeeping twin is back in "
            "install_project_bundle — call `_record_preserve` instead",
        )
        self.assertEqual(src.count("_record_preserve("), 2)
        self.assertIn(
            '"knowledge-preserve"',
            inspect.getsource(bundle_preserve.record_preserve),
        )


# ═══════════════════════════════════════════════════════════════════════════
# L-11 — the read-only SQLite URI is percent-quoted
# ═══════════════════════════════════════════════════════════════════════════


class SqliteReadOnlyUriIsQuoted(unittest.TestCase):
    """A launcher.db under a path containing `?`, `#` or `%` must still open.

    SQLite parses everything after the first `?` in a `file:` URI as the
    query string, so an unquoted path truncates there. The ship-gate review
    called the consequence conservative ("the open fails, the gate never
    fires"). MEASURED, it is not conservative at all — see
    :meth:`test_plain_interpolation_opens_the_wrong_file_read_write`.
    """

    HOSTILE = "weird?dir#frag%20name"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vct-v0296-uri-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / self.HOSTILE
        self.dir.mkdir()
        self.db = self.dir / "launcher.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE t (a TEXT)")
        con.execute("INSERT INTO t VALUES ('ok')")
        con.commit()
        con.close()

    def test_plain_interpolation_opens_the_wrong_file_read_write(self):
        """The shape the helper replaces, measured — the red half of the proof.

        The path truncates at its own `?`, which ALSO swallows the
        `?mode=ro` that was supposed to follow. So the connection is not
        read-only and the file is not the launcher DB: SQLite's read-write
        default CREATES the truncated path as an empty database. Every
        subsequent read then reports the tables absent, which each caller's
        soft-fail reads as "not registered" — a wrong answer, not a missing
        one, delivered over a write-capable handle on a file VCO never meant
        to touch.
        """
        truncated = Path(str(self.db).split("?", 1)[0])
        self.assertFalse(truncated.exists())
        conn = sqlite3.connect(
            f"file:{self.db}?mode=ro", uri=True, timeout=2.0,
        )
        try:
            attached = list(conn.execute("PRAGMA database_list"))[0][2]
            self.assertEqual(Path(attached), truncated)
            # mode=ro was LOST — the handle writes.
            conn.execute("CREATE TABLE proof_the_uri_is_writable (x TEXT)")
            conn.commit()
            # And the real DB's content is nowhere in sight.
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT a FROM t")
        finally:
            conn.close()
        self.assertTrue(
            truncated.exists(),
            "SQLite created the truncated path as a new database",
        )
        truncated.unlink()

    def test_the_shared_helper_opens_it(self):
        from vco_lib.launcher_db_reader import sqlite_ro_uri

        conn = sqlite3.connect(sqlite_ro_uri(self.db), uri=True, timeout=2.0)
        try:
            self.assertEqual(conn.execute("SELECT a FROM t").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_the_uri_is_still_read_only(self):
        from vco_lib.launcher_db_reader import sqlite_ro_uri

        conn = sqlite3.connect(sqlite_ro_uri(self.db), uri=True, timeout=2.0)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO t VALUES ('nope')")
        finally:
            conn.close()

    def test_extra_params_are_appended_after_mode_ro(self):
        from vco_lib.launcher_db_reader import sqlite_ro_uri

        uri = sqlite_ro_uri(self.db, immutable=True)
        self.assertIn("mode=ro", uri)
        self.assertIn("immutable=1", uri)
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.close()

    def test_every_launcher_db_open_uses_the_helper(self):
        """The un-migrated-call-site check. A hand-built `file:{...}?mode=ro`
        anywhere in the shipped Python is a call-site the fix does not reach —
        which is exactly how a "shared helper" stays green while half the
        product keeps the bug."""
        import re

        pattern = re.compile(r'f"file:\{')
        offenders: list[str] = []
        roots = [
            REPO_ROOT / "vco_lib",
            REPO_ROOT / "templates" / "scripts",
            REPO_ROOT / "install.py",
        ]
        for root in roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for f in files:
                if f.name == "launcher_db_reader.py":
                    continue  # the home itself
                for i, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if pattern.search(line):
                        offenders.append(
                            f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}"
                        )
        self.assertEqual(
            offenders, [],
            "hand-built sqlite `file:` URIs found — route them through "
            "`vco_lib.launcher_db_reader.sqlite_ro_uri`:\n"
            + "\n".join(offenders),
        )


# ═══════════════════════════════════════════════════════════════════════════
# L-12 — module_gated_delivery's own soft-fail legs, pinned directly
# ═══════════════════════════════════════════════════════════════════════════


class ResolveProjectIdForFolderSoftFails(unittest.TestCase):
    """`resolve_project_id_for_folder` is the folder→UUID half of the
    delivery gate AND of two env projections. The composite-gate tests pin
    the gate's verdict; these pin THIS function, so a future caller that
    reads its `None` differently still has the contract in front of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vct-v0296-mgd-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "project"
        self.folder.mkdir()

    def _resolve(self, db: Path):
        from vco_lib.module_gated_delivery import resolve_project_id_for_folder
        return resolve_project_id_for_folder(self.folder, db_path=db)

    def test_absent_db_is_none(self):
        self.assertIsNone(self._resolve(self.root / "nope" / "launcher.db"))

    def test_corrupt_db_is_none(self):
        db = self.root / "launcher.db"
        db.write_bytes(b"definitely not sqlite" * 8)
        self.assertIsNone(self._resolve(db))

    @unittest.skipIf(os.name == "nt", "POSIX chmod semantics")
    def test_unreadable_db_is_none(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses POSIX permission bits")
        # Real-schema DB via the ONE fixture home, then made unreadable — a
        # hand-rolled `CREATE TABLE projects` here would be a schema copy
        # that drifts from the launcher's (pinned by
        # test_launcher_db_fixture_schema.py).
        from tests.common.launcher_db_fixture import make_launcher_db

        db = make_launcher_db(self.root, projects=[])
        db.chmod(0o000)
        self.addCleanup(db.chmod, 0o600)
        self.assertIsNone(self._resolve(db))

    def test_missing_projects_table_is_none(self):
        db = self.root / "launcher.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE unrelated (x TEXT)")
        con.commit()
        con.close()
        self.assertIsNone(self._resolve(db))

    def test_registered_folder_resolves_and_a_sibling_does_not(self):
        """The `not_registered` surface: a row for SOMEONE ELSE's folder must
        resolve to None, never to that other project's id."""
        from tests.common.launcher_db_fixture import make_launcher_db

        other = self.root / "other-project"
        other.mkdir()
        db = make_launcher_db(self.root, projects=[
            {"project_id": "11111111-2222-3333-4444-555555555555",
             "name": "Mine", "folder_path": self.folder},
            {"project_id": "99999999-8888-7777-6666-555555555555",
             "name": "Theirs", "folder_path": other},
        ])
        self.assertEqual(
            self._resolve(db), "11111111-2222-3333-4444-555555555555",
        )
        from vco_lib.module_gated_delivery import resolve_project_id_for_folder
        self.assertIsNone(
            resolve_project_id_for_folder(self.root / "unregistered", db_path=db),
        )

    def test_unresolvable_folder_is_none_not_an_exception(self):
        """A folder whose `.resolve()` raises (a symlink loop, a path too
        long) must soft-fail like every other miss."""
        from vco_lib.module_gated_delivery import resolve_project_id_for_folder
        from tests.common.launcher_db_fixture import make_launcher_db

        db = make_launcher_db(self.root, projects=[])
        bad = mock.MagicMock(spec=Path)
        bad.resolve.side_effect = OSError("ELOOP")
        self.assertIsNone(resolve_project_id_for_folder(bad, db_path=db))


# ═══════════════════════════════════════════════════════════════════════════
# ONE reader for "the class names in a /v1/schema payload"
# ═══════════════════════════════════════════════════════════════════════════


class SchemaClassNamesHasOneHome(unittest.TestCase):
    """Found while fixing A4: the comprehension had SIX hand-written copies,
    three of which yielded ``str | None`` — which is where three of
    ``install.py``'s type errors came from when it joined the pyright gate.
    A ``None`` in that set flows into ``sorted()`` and into class-name
    comparisons; the copies had each rediscovered the narrowing trick, and
    half had got it wrong."""

    def test_non_conforming_entries_are_dropped(self):
        from vco_lib.weaviate_helpers import schema_class_names

        self.assertEqual(
            schema_class_names({"classes": [
                {"class": "Alpha_KnowledgeGraph"},
                {"class": ""},          # empty name is not a name
                {"class": None},        # missing value
                {"noclass": "x"},       # no key at all
                "not-a-dict",
                {"class": 7},           # not a string
                {"class": "Beta_Code"},
            ]}),
            {"Alpha_KnowledgeGraph", "Beta_Code"},
        )

    def test_unreadable_payloads_are_an_empty_set_not_a_crash(self):
        from vco_lib.weaviate_helpers import schema_class_names

        for payload in (None, [], "x", {}, {"classes": None}, {"classes": "x"}):
            self.assertEqual(schema_class_names(payload), set())

    def test_the_result_is_genuinely_set_of_str(self):
        """The property the copies lost: `sorted()` must not meet a None."""
        from vco_lib.weaviate_helpers import schema_class_names

        got = schema_class_names({"classes": [{"class": "B"}, {"class": None},
                                              {"class": "A"}]})
        self.assertEqual(sorted(got), ["A", "B"])
        self.assertTrue(all(isinstance(n, str) for n in got))

    def test_no_surviving_hand_written_copies(self):
        """The un-migrated-call-site scan, keyed on the payload access the
        copies all share.

        Two shapes are deliberately NOT offenders: `doctor.py`'s
        superficially similar comprehension reads `finding.detail`, not a
        schema response (so it does not match the needle at all), and the ONE
        site that genuinely needs the class ENTRIES rather than their names
        carries an inline `# schema-entries (not names)` marker. A waiver
        that must be written on the line, next to the code, is the repo's
        existing form for "this is the other question" — and it is visible to
        the next reader, which a silently-narrowed needle would not be.
        """
        needle = 'schema.get("classes"'
        waiver = "schema-entries (not names)"
        offenders: list[str] = []
        roots = [
            REPO_ROOT / "vco_lib",
            REPO_ROOT / "templates" / "scripts",
            REPO_ROOT / "install.py",
        ]
        for root in roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for f in files:
                if f.name == "weaviate_helpers.py":
                    continue  # the home itself
                for i, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if (
                        needle in line
                        and waiver not in line
                        and not line.lstrip().startswith("#")
                    ):
                        offenders.append(
                            f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}"
                        )
        self.assertEqual(
            offenders, [],
            "hand-written schema class-name readers found — call "
            "`vco_lib.weaviate_helpers.schema_class_names`:\n"
            + "\n".join(offenders),
        )

    def test_the_waiver_is_not_a_blanket_escape(self):
        """The marker must be RARE and it must be on a line that genuinely
        reads entries. One site earns it today; a second should be argued
        for, not slipped in."""
        marker = "schema-entries (not names)"
        hits: list[str] = []
        for root in (REPO_ROOT / "vco_lib", REPO_ROOT / "templates" / "scripts",
                     REPO_ROOT / "install.py"):
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for f in files:
                for i, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if marker in line and not line.lstrip().startswith("#"):
                        hits.append(f"{f.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(len(hits), 1, f"expected exactly one waiver; got {hits}")


# ═══════════════════════════════════════════════════════════════════════════
# D-4 — ONE folder→project-id resolver, every call-site migrated
# ═══════════════════════════════════════════════════════════════════════════


class FolderToProjectIdHasOneHome(unittest.TestCase):
    """`module_gated_delivery.resolve_project_id_for_folder` was extracted
    THIS cycle (WP-10) and its own header calls itself "ONE home for the
    pattern" — while two more inline copies stayed (and a third was ADDED in
    the same cycle by WP-2's `_shared_kg_from_launcher_db`).

    The mutation pin: break the shared resolver and every call-site must go
    red through it. A "shared helper" half the product does not call is the
    specific failure this repo has been bitten by.
    """

    PID = "cafe0000-1111-2222-3333-444444444444"
    SHARED = "MachineCanonical_Shared_KG"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vct-v0296-d4-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self._saved = {
            k: os.environ.get(k)
            for k in ("VCT_LAUNCHER_DB_PATH", "VCT_STATE_DIR")
        }
        self.addCleanup(self._restore)

        from tests.common.launcher_db_fixture import add_project, make_launcher_db
        self.db = make_launcher_db(self.root / "db")
        add_project(
            self.db,
            project_id=self.PID,
            name="D4 Proj",
            folder_path=str(self.folder),
            host="base",
            kg_primary="MyProject_KnowledgeGraph",
            kg_shared=self.SHARED,
        )
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(self.db)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_kg_binding_read_routes_through_the_shared_resolver(self):
        from vco_lib.kg_binding_read import _read_kg_binding_override

        # Unmutated: the real resolver finds the registered folder.
        self.assertEqual(
            _read_kg_binding_override(self.folder)["shared_kg_collection"],
            self.SHARED,
        )
        # Mutated: the call-site must go red through the shared home.
        with mock.patch(
            "vco_lib.module_gated_delivery.resolve_project_id_for_folder",
            return_value=None,
        ):
            self.assertIsNone(
                _read_kg_binding_override(self.folder)["shared_kg_collection"],
                "kg_binding_read still carries its own folder→id loop",
            )

    def test_the_sync_scripts_db_tier_routes_through_the_shared_resolver(self):
        """`templates/scripts/sync_knowledge_graph.py` ships to user projects
        and CAN import `vco_lib` (it already does), so the A-tier fix applies:
        one implementation called across the boundary, not a mirror."""
        import importlib.util
        import uuid as _uuid

        script = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        name = f"_d4_sync_{_uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, script)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        self.addCleanup(sys.modules.pop, name, None)
        os.environ["KG_BASE_DIR"] = str(self.folder)
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        self.addCleanup(os.environ.pop, "KG_BASE_DIR", None)
        self.addCleanup(os.environ.pop, "VCT_DISABLE_HUB_RESOLVER", None)
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as exc:  # pragma: no cover
            self.skipTest(f"sync script has runtime deps not installed ({exc})")

        self.assertEqual(
            mod._shared_kg_from_launcher_db(self.folder), self.SHARED,
        )
        with mock.patch(
            "vco_lib.module_gated_delivery.resolve_project_id_for_folder",
            return_value=None,
        ):
            # With the by-folder tier dead, the ladder falls to the
            # orchestrator-root row — absent in this fixture → "".
            self.assertEqual(
                mod._shared_kg_from_launcher_db(self.folder), "",
                "the sync script still carries its own folder-match loop",
            )

    def test_the_wp10_call_sites_are_still_wired(self):
        """The two call-sites WP-10 itself migrated, re-proved through the
        same mutation so this pin covers ALL of them, not just the new ones."""
        from vco_lib.module_gated_delivery import module_gateway_agents_active
        from vco_lib.project_init import _read_codegraph_binding_override

        with mock.patch(
            "vco_lib.module_gated_delivery.resolve_project_id_for_folder",
            return_value=None,
        ):
            self.assertFalse(module_gateway_agents_active(self.folder))
            self.assertIsNone(
                _read_codegraph_binding_override(
                    self.folder)["collection_prefix"],
            )

    def test_installs_resolver_routes_through_the_shared_home(self):
        """`install.py::_resolve_project_id_by_folder` was the FIFTH copy and
        the one whose comparator drifted furthest (plain `==` on resolved
        paths, so case-different Windows rows missed). It keeps its name —
        four call-sites and three tests use it — over the shared body."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_d4_install_py", REPO_ROOT / "install.py",
        )
        install_py = importlib.util.module_from_spec(spec)
        sys.modules["_d4_install_py"] = install_py
        self.addCleanup(sys.modules.pop, "_d4_install_py", None)
        spec.loader.exec_module(install_py)

        self.assertEqual(
            install_py._resolve_project_id_by_folder(self.folder), self.PID,
        )
        with mock.patch(
            "vco_lib.module_gated_delivery.resolve_project_id_for_folder",
            return_value=None,
        ):
            self.assertIsNone(
                install_py._resolve_project_id_by_folder(self.folder),
                "install.py still carries its own folder→id loop",
            )

    def test_no_surviving_inline_folder_match_loops(self):
        """The un-migrated-call-site scan: the resolver's SQL shape must not
        reappear anywhere outside its one home."""
        needle = "SELECT id, folder_path FROM projects"
        offenders: list[str] = []
        roots = [
            REPO_ROOT / "vco_lib",
            REPO_ROOT / "templates" / "scripts",
            REPO_ROOT / "install.py",
        ]
        for root in roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for f in files:
                if f.name == "module_gated_delivery.py":
                    continue  # the home itself
                for i, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if needle in line:
                        offenders.append(f"{f.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(
            offenders, [],
            "inline folder→project-id loops found — call "
            "`module_gated_delivery.resolve_project_id_for_folder`:\n"
            + "\n".join(offenders),
        )


# ═══════════════════════════════════════════════════════════════════════════
# D-1 / D-2 — the two Python→Rust line grammars, LOCKED
#
# Both landed this cycle as C-tier mirrors with no lock at all: no "must
# match" comment on either side and no parity test. The Rust tests for the
# recheck line HAND-WRITE the line they parse, so producer and parser could
# drift with every test staying green — and the failure mode of each drift is
# SILENT (progress ticks stop reaching the modal, which looks exactly like a
# stalled update; recheck counts degrade to a raw stdout tail).
#
# These tests extract the real values from the Rust source and compare them.
# They are deliberately NOT marker scans: "`MUST MATCH` in src" is satisfied
# by a name in a comment and can observe no drift at all.
# ═══════════════════════════════════════════════════════════════════════════


class VcoEventLineGrammarParity(unittest.TestCase):
    """`[VCO-EVENT] <step> <phase> <detail>` — Python producer ↔ Rust consumer."""

    RUST = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
            / "update_pipeline.rs")

    def setUp(self):
        self.src = self.RUST.read_text(encoding="utf-8")

    def test_rust_strips_exactly_the_python_prefix(self):
        import re as _re

        from vco_lib.progress_event import EVENT_PREFIX

        m = _re.search(r'line\.strip_prefix\("([^"]*)"\)', self.src)
        self.assertIsNotNone(
            m, "could not locate the [VCO-EVENT] strip_prefix in the consumer",
        )
        self.assertEqual(
            m.group(1), EVENT_PREFIX,
            "the Rust consumer strips a prefix the Python producer does not "
            "emit — every progress tick would be dropped, which on the GUI "
            "looks identical to a stalled update",
        )

    def test_rust_splits_into_exactly_the_python_field_count(self):
        import re as _re

        from vco_lib.progress_event import EVENT_FIELD_COUNT

        m = _re.search(r"rest\.splitn\((\d+), ' '\)", self.src)
        self.assertIsNotNone(m, "could not locate the field split")
        self.assertEqual(int(m.group(1)), EVENT_FIELD_COUNT)

    def test_a_real_emitted_line_parses_under_the_rust_rules(self):
        """End-to-end over the ACTUAL producer output: format a line, then
        apply the consumer's own two operations to it."""
        from vco_lib.progress_event import EVENT_FIELD_COUNT, EVENT_PREFIX, format_event

        line = format_event("7c/10", "ok", "syncing knowledge: 42/100 nodes")
        self.assertTrue(line.endswith("\n"))
        body = line.rstrip("\n")
        self.assertTrue(body.startswith(EVENT_PREFIX))
        rest = body[len(EVENT_PREFIX):]
        parts = rest.split(" ", EVENT_FIELD_COUNT - 1)
        self.assertEqual(len(parts), EVENT_FIELD_COUNT)
        self.assertEqual(parts[0], "7c/10")
        self.assertEqual(parts[1], "ok")
        self.assertEqual(parts[2], "syncing knowledge: 42/100 nodes")

    def test_detail_newlines_cannot_split_one_event_into_two(self):
        from vco_lib.progress_event import format_event

        line = format_event("kg-sync", "ok", "line one\nline two\r\nthree")
        self.assertEqual(line.count("\n"), 1, "only the terminator may remain")
        self.assertNotIn("\r", line)

    def test_the_relay_filter_is_built_from_the_same_prefix(self):
        """`child_process` forwards only these lines. A hand-copied prefix
        there is a silent drop the moment the grammar moves."""
        from vco_lib.child_process import _EVENT_LINE_RE
        from vco_lib.progress_event import format_event

        self.assertTrue(
            _EVENT_LINE_RE.match(format_event("7c/10", "start", "x")),
        )
        self.assertIsNone(_EVENT_LINE_RE.match("[VCO-OTHER] 7c/10 start x"))

    def test_both_producers_route_through_the_one_home(self):
        """The un-migrated-call-site check: no surviving hand-BUILT
        `[VCO-EVENT]` line in the shipped Python.

        The scan looks for the marker inside a STRING LITERAL (``"[VCO-EVENT]``
        or ``'[VCO-EVENT]``) — prose that merely names the grammar in a
        comment or docstring is documentation, and several places should
        legitimately keep naming it.
        """
        import re as _re

        literal = _re.compile(r"""["'](?:\[VCO-EVENT\])""")
        offenders: list[str] = []
        roots = [
            REPO_ROOT / "vco_lib",
            REPO_ROOT / "templates" / "scripts",
            REPO_ROOT / "install.py",
        ]
        for root in roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for f in files:
                if f.name == "progress_event.py":
                    continue  # the home itself
                for i, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if literal.search(line):
                        offenders.append(
                            f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}"
                        )
        self.assertEqual(
            offenders, [],
            "hand-built [VCO-EVENT] lines found — emit through "
            "`vco_lib.progress_event`:\n" + "\n".join(offenders),
        )


class SummaryRecheckLineGrammarParity(unittest.TestCase):
    """`[summary-health] recheck: …` — Python producer ↔ Rust parser."""

    RUST = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
            / "kg_summary.rs")

    def setUp(self):
        self.src = self.RUST.read_text(encoding="utf-8")

    def _rust_const(self, name: str) -> str:
        import re as _re

        m = _re.search(rf'const {name}: &str = "([^"]*)";', self.src)
        self.assertIsNotNone(m, f"could not locate the Rust const {name}")
        return m.group(1)

    def test_rust_prefix_equals_the_python_prefix(self):
        from vco_lib.summary_health import RECHECK_LINE_PREFIX

        self.assertEqual(
            self._rust_const("RECHECK_LINE_PREFIX"), RECHECK_LINE_PREFIX,
            "the launcher looks for a prefix the CLI does not print — the "
            "recheck's counts would silently degrade to a raw stdout tail",
        )

    def test_the_skipped_marker_the_rust_tests_for_is_in_the_python_suffix(self):
        from vco_lib.summary_health import RECHECK_CODE_LEG_SKIPPED

        self.assertIn(
            "code leg SKIPPED", self.src,
            "the Rust parser's code-leg probe is gone",
        )
        self.assertIn("code leg SKIPPED", RECHECK_CODE_LEG_SKIPPED)
        # The parser drops everything from `"; code leg"` before splitting
        # the counts — so the Python suffix must start with exactly that.
        self.assertIn('rest.split("; code leg")', self.src)
        self.assertTrue(RECHECK_CODE_LEG_SKIPPED.startswith("; code leg"))

    def _parse_like_rust(self, line: str):
        """Re-implement `parse_recheck_summary_line`'s SPLITTING rules over a
        real produced line. Not a second implementation of the producer — it
        is the consumer's own algorithm, which is the thing under test."""
        from vco_lib.summary_health import RECHECK_LINE_PREFIX

        self.assertIn(RECHECK_LINE_PREFIX, line)
        rest = line.split(RECHECK_LINE_PREFIX, 1)[1].strip()
        code_leg_skipped = "code leg SKIPPED" in rest
        counts_part = rest.split("; code leg")[0].strip()
        pendings, runs = counts_part.split(";")
        def leading(seg: str) -> int:
            digits = ""
            for ch in seg.strip():
                if not ch.isdigit():
                    break
                digits += ch
            self.assertTrue(digits, f"no leading integer in {seg!r}")
            return int(digits)
        pb, pa = (leading(x) for x in pendings.split(",", 1))
        sp, fa = (leading(x) for x in runs.split(",", 1))
        return pb, pa, sp, fa, code_leg_skipped

    def test_a_real_produced_line_parses_under_the_rust_rules(self):
        from vco_lib.summary_health import RecheckResult, format_recheck_summary_line

        line = format_recheck_summary_line(RecheckResult(
            pending_before=7, pending_after=2,
            spawned=5, failures=1, code_leg_skipped=False,
        ))
        self.assertEqual(self._parse_like_rust(line), (7, 2, 5, 1, False))

    def test_the_code_leg_suffix_does_not_disturb_the_counts(self):
        from vco_lib.summary_health import RecheckResult, format_recheck_summary_line

        line = format_recheck_summary_line(RecheckResult(
            pending_before=0, pending_after=0,
            spawned=0, failures=0, code_leg_skipped=True,
        ))
        self.assertEqual(self._parse_like_rust(line), (0, 0, 0, 0, True))

    def test_the_cli_prints_through_the_one_home(self):
        """`main()` must not re-inline the format it just extracted."""
        import inspect

        from vco_lib import summary_health

        src = inspect.getsource(summary_health.main)
        self.assertIn("format_recheck_summary_line(result)", src)
        self.assertNotIn("pending \"\n", src)
        self.assertNotIn("generator run(s)", src)


# ═══════════════════════════════════════════════════════════════════════════
# F-N5 — the two stale docstrings
# ═══════════════════════════════════════════════════════════════════════════


class DocstringsStateWhatTheCodeDoes(unittest.TestCase):
    def test_resolve_active_modules_does_not_claim_row_less_default_on(self):
        """The docstring claimed a module with no row is active. It is not —
        only `_DEFAULT_ACTIVE_MODULES` is, and the WP-10 delivery gate depends
        on that. A reader who "fixed the code to match" would ship
        `claude-gw/*` agent ids onto every stock install."""
        doc = project_init.resolve_active_modules.__doc__ or ""
        self.assertNotIn("STUB BEHAVIOUR", doc)
        self.assertNotIn("Phase 1.1's DB migration lands", doc)
        self.assertNotIn("when no row exists for that", doc)
        self.assertIn("_DEFAULT_ACTIVE_MODULES", doc)

    def test_row_less_module_really_is_inactive(self):
        """The behaviour the docstring now describes, on the wire — so the
        docstring cannot drift back without a red test."""
        from tests.common.launcher_db_fixture import insert_rows, make_launcher_db

        tmp = tempfile.TemporaryDirectory(prefix="vct-v0296-mods-")
        self.addCleanup(tmp.cleanup)
        pid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        db = make_launcher_db(Path(tmp.name), projects=[
            {"project_id": pid, "name": "P", "folder_path": Path(tmp.name)},
        ])
        insert_rows(db, "project_modules", [
            {"project_id": pid, "module_name": "something_else", "enabled": 1},
        ])
        active = project_init.resolve_active_modules(pid, db_path=db)
        self.assertNotIn("model_gateway", active)
        self.assertIn("diagrams", active)   # the default-on set
        self.assertIn("something_else", active)

    def test_user_modified_emitter_does_not_call_dismiss_a_placeholder(self):
        """`dismiss-deferral` exists and the entry prints it. The docstring
        used to call it a "PR 5+ command — placeholder in the message for
        now"."""
        from vco_lib import bundle_preserve
        doc = bundle_preserve.emit_user_modified_deferral.__doc__ or ""
        self.assertNotIn("placeholder", doc.lower())
        self.assertIn("dismiss-deferral", doc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
