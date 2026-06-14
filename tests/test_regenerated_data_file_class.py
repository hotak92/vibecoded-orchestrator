"""v0.2.57 — regenerated-data bundle file class + schema-version gate.

Covers the fix for the spurious `bundle_user_modified_preserved` warning on
`knowledge/.node_formats.json` (a per-project REGENERATED KG-summary cache,
not a user customization). Two surfaces:

  A. `_file_action` returns `keep-regenerated` (NOT `preserve`) for a
     `regenerated_data` op whose on-disk copy diverged → no warning, keep
     local.
  B. `check-node-formats-schema` subcommand drives the DB-backed schema gate
     (artifact_type `kg_node_formats`): register on first materialize,
     UP_TO_DATE on re-run, regenerate (or info-defer) on a schema bump.
     Includes the NULL-project_id upsert-dedup regression for
     `register_artifact_version`.

Also asserts the LIVING-DOCS class (CLAUDE.md / CONTEXT_STATE.md / MEMORY.md)
is handled by the separate `.reference.md` materializer and never enters the
`preserve` / user-modified path (no double-warning).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init as pi  # noqa: E402
from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402


def _mk_artifact_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE artifact_schema_versions(
            project_id TEXT, artifact_type TEXT NOT NULL, artifact_name TEXT NOT NULL,
            schema_version INTEGER NOT NULL, materialized_at INTEGER NOT NULL,
            PRIMARY KEY(project_id, artifact_type, artifact_name))"""
    )
    con.commit()
    con.close()


class FileActionKeepRegeneratedTests(unittest.TestCase):
    """Class A: the regenerated_data flag flips preserve → keep-regenerated."""

    def _op(self, *, regenerated_data: bool, source_abs: Path) -> "pi._BundleFileOp":
        return pi._BundleFileOp(
            dest_rel="knowledge/.node_formats.json",
            source_abs=source_abs,
            source_rel="templates/knowledge/.node_formats.json",
            transform=None,
            always_overwrite=False,
            regenerated_data=regenerated_data,
        )

    def test_regenerated_data_diverged_returns_keep_regenerated(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "proj"
            (folder / "knowledge").mkdir(parents=True)
            target = folder / "knowledge" / ".node_formats.json"
            target.write_text('{"my/node.md": {"title": "local"}}')
            seed = Path(d) / "seed.json"
            seed.write_text('{"seed/stub.md": {"title": "seed"}}')  # diverges
            action, _ = pi._file_action(
                self._op(regenerated_data=True, source_abs=seed),
                target, update_mode=True, manifest={"files": {}}, project_root=folder,
            )
        self.assertEqual(action, "keep-regenerated")

    def test_same_divergence_unflagged_still_preserves(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "proj"
            (folder / "knowledge").mkdir(parents=True)
            target = folder / "knowledge" / ".node_formats.json"
            target.write_text('{"my/node.md": {"title": "local"}}')
            seed = Path(d) / "seed.json"
            seed.write_text('{"seed/stub.md": {"title": "seed"}}')
            action, _ = pi._file_action(
                self._op(regenerated_data=False, source_abs=seed),
                target, update_mode=True, manifest={"files": {}}, project_root=folder,
            )
        self.assertEqual(action, "preserve",
                         "without the flag the old preserve+warn behavior must remain")

    def test_node_formats_is_flagged_regenerated_in_enumeration(self):
        self.assertTrue(pi._is_regenerated_data_file("knowledge/.node_formats.json"))
        self.assertFalse(pi._is_regenerated_data_file("knowledge/concepts/foo.md"))


class NullProjectIdUpsertTests(unittest.TestCase):
    """register_artifact_version must DEDUPE on a NULL project_id (SQLite
    treats NULL as distinct in a PK, so plain INSERT OR REPLACE would append
    a duplicate that could shadow the fresh row on read)."""

    def test_null_project_id_upsert_replaces_not_appends(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "launcher.db"
            _mk_artifact_db(db)
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO artifact_schema_versions VALUES(NULL,'kg_node_formats','proj',0,1)"
            )
            con.commit()
            con.close()
            canonical = sv.canonical_version("kg_node_formats")
            ok = avr.register_artifact_version(
                db, project_id=None, artifact_type="kg_node_formats",
                artifact_name="proj", schema_version=canonical, materialized_at=999,
            )
            self.assertTrue(ok)
            con = sqlite3.connect(db)
            rows = con.execute(
                "SELECT schema_version FROM artifact_schema_versions "
                "WHERE artifact_type='kg_node_formats'"
            ).fetchall()
            con.close()
        self.assertEqual(rows, [(canonical,)],
                         "NULL-project_id upsert must leave exactly ONE row at canonical")


class CheckNodeFormatsSchemaSubcommandTests(unittest.TestCase):
    """Class B: the check-node-formats-schema subcommand DB-gate."""

    def _run(self, folder: Path, db: Path, now_ms: int = 1000):
        r = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init", "check-node-formats-schema",
             "--folder", str(folder), "--db", str(db), "--now-ms", str(now_ms)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        return r, json.loads(r.stdout)

    def _seed_project(self, d: str) -> tuple[Path, Path]:
        folder = Path(d) / "proj"
        (folder / "knowledge" / "concepts").mkdir(parents=True)
        (folder / "knowledge" / ".node_formats.json").write_text(
            '{"concepts/n.md": {"title": "keep-me"}}'
        )
        (folder / "knowledge" / "concepts" / "n.md").write_text("# n\nbody\n")
        db = Path(d) / "launcher.db"
        _mk_artifact_db(db)
        return folder, db

    def test_first_materialize_registers_at_canonical(self):
        with tempfile.TemporaryDirectory() as d:
            folder, db = self._seed_project(d)
            r, out = self._run(folder, db)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(out["status"], "NEVER_MATERIALIZED")
            self.assertEqual(out["action"], "registered")
            con = sqlite3.connect(db)
            row = con.execute(
                "SELECT schema_version FROM artifact_schema_versions "
                "WHERE artifact_type='kg_node_formats'"
            ).fetchone()
            con.close()
        self.assertEqual(row[0], sv.canonical_version("kg_node_formats"))

    def test_second_run_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as d:
            folder, db = self._seed_project(d)
            self._run(folder, db, now_ms=1000)
            _, out2 = self._run(folder, db, now_ms=2000)
        self.assertEqual(out2["status"], "UP_TO_DATE")

    def test_stale_without_generator_defers_and_preserves_cache(self):
        with tempfile.TemporaryDirectory() as d:
            folder, db = self._seed_project(d)
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO artifact_schema_versions "
                "VALUES(NULL,'kg_node_formats','default',0,1)"
            )
            con.commit()
            con.close()
            _, out = self._run(folder, db, now_ms=3000)
            self.assertEqual(out["status"], "RECREATE_NEEDED")
            self.assertEqual(out["action"], "deferred")
            self.assertTrue(out["deferral_written"])
            # cache UNTOUCHED (no data loss)
            cache = (folder / "knowledge" / ".node_formats.json").read_text()
            self.assertIn("keep-me", cache)
            deferred = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            self.assertIn("regenerated_data_schema_migration_pending", deferred)

    def test_stale_with_working_generator_regenerates_and_reregisters(self):
        with tempfile.TemporaryDirectory() as d:
            folder, db = self._seed_project(d)
            (folder / ".claude" / "scripts").mkdir(parents=True)
            # A WORKING generator MUST actually rewrite the cache — exit-0
            # alone is NOT regeneration (review B1: the real generator exits
            # 0 without writing when no backend is available). This fake
            # appends a fresh entry so the cache content CHANGES, which is
            # what `_regenerate_node_formats` now requires to report success.
            (folder / ".claude" / "scripts" / "generate-kg-summary.py").write_text(
                "import json, sys, time\n"
                "from pathlib import Path\n"
                "f = Path('knowledge/.node_formats.json')\n"
                "d = json.loads(f.read_text()) if f.exists() else {}\n"
                "d[sys.argv[1]] = {'title': 'regenerated', 'at': time.time()}\n"
                "f.write_text(json.dumps(d))\n"
                "sys.exit(0)\n"
            )
            # Use the artifact's STABLE name (review N2): a fixed 'default'.
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO artifact_schema_versions "
                "VALUES(NULL,'kg_node_formats','default',0,1)"
            )
            con.commit()
            con.close()
            _, out = self._run(folder, db, now_ms=4000)
            self.assertEqual(out["action"], "regenerated", out)
            self.assertTrue(out["regenerated"])
            con = sqlite3.connect(db)
            rows = con.execute(
                "SELECT schema_version FROM artifact_schema_versions "
                "WHERE artifact_type='kg_node_formats'"
            ).fetchall()
            con.close()
        self.assertEqual(rows, [(sv.canonical_version("kg_node_formats"),)],
                         "must re-register at canonical with exactly one row")

    def test_stale_with_noop_generator_defers_not_false_success(self):
        """Review B1: a generator that exits 0 WITHOUT writing (the real
        no-backend / `select_backend()=='skip'` case during a headless
        update) must NOT be treated as a successful regeneration. The cache
        is unchanged → defer, leave the registry at the OLD version, write
        the info deferral. Registering canonical here would silently mask
        the migration."""
        with tempfile.TemporaryDirectory() as d:
            folder, db = self._seed_project(d)
            (folder / ".claude" / "scripts").mkdir(parents=True)
            # exit-0-but-writes-nothing — exactly the no-backend behavior.
            (folder / ".claude" / "scripts" / "generate-kg-summary.py").write_text(
                "import sys; sys.exit(0)\n"
            )
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO artifact_schema_versions "
                "VALUES(NULL,'kg_node_formats','default',0,1)"
            )
            con.commit()
            con.close()
            _, out = self._run(folder, db, now_ms=5000)
            self.assertEqual(out["action"], "deferred", out)
            self.assertTrue(out["deferral_written"])
            # cache untouched
            self.assertIn("keep-me", (folder / "knowledge" / ".node_formats.json").read_text())
            # registry STILL at the old version (NOT falsely advanced)
            con = sqlite3.connect(db)
            ver = con.execute(
                "SELECT schema_version FROM artifact_schema_versions "
                "WHERE artifact_type='kg_node_formats'"
            ).fetchone()[0]
            con.close()
        self.assertEqual(ver, 0,
                         "no-backend exit-0 must NOT advance the registry version")


class LivingDocsNoDoubleWarnTests(unittest.TestCase):
    """The living-docs class (CLAUDE.md / CONTEXT_STATE.md / MEMORY.md) must
    NOT be in the bundle ops list — they're handled by the separate
    `.reference.md` materializer, so they can never produce a
    `bundle_user_modified_preserved` (preserve) warning."""

    def test_living_docs_not_in_bundle_ops(self):
        ops = pi._enumerate_bundle_files(REPO_ROOT)
        dests = {op.dest_rel for op in ops}
        for living in ("CLAUDE.md", ".claude/CONTEXT_STATE.md", ".claude/MEMORY.md"):
            self.assertNotIn(
                living, dests,
                f"{living} must NOT be a bundle op (it's a living doc handled by "
                f"the .reference.md materializer — being in the ops list would route "
                f"it through _file_action → preserve → bundle_user_modified_preserved, "
                f"i.e. a double-warning on top of template_review_pending).",
            )


if __name__ == "__main__":
    unittest.main()
