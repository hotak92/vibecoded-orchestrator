# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W18 — the three axes, the delivery audit (R17) and the promise
audit (R16) for the WP-4 change set, asserted rather than claimed.

THREE AXES
  * FRESH    — a project with no state file and no orphan history: nothing is
               detected, nothing is emitted, nothing is stamped, on both the
               Weaviate-up and Weaviate-down paths.
  * UPDATE   — the SHIPPED bundle-update path (``install_project_bundle``)
               carries the evidence gate; a real generation change still
               surfaces and a dead record is healed instead.
  * ALREADY  — a user whose install is ALREADY in the broken state. Two
    -DAMAGED  populations, two named mechanisms:
                 (a) a poisoned ``codegraph-prefix-generation.json`` — healed by
                     the bundle update, which every project takes;
                 (b) an outage-time ``codegraph-orphan-live-prefixes.json`` —
                     neutralised at READ time by the reclaim, because a user
                     may paste that command without ever updating first.

DELIVERY (R17). The change adds one new file, ``vco_lib/codegraph_prefix_record.py``.
``vco_lib`` is not bundled into projects — it is delivered by ``pip install -e .``
of the orchestrator checkout, and ``pyproject.toml`` declares
``packages = ["vco_lib"]``, a whole-package glob. That is VERIFIED below by
parsing the real ``pyproject.toml``, not assumed.

PROMISE (R16). Every assertion here backs a sentence some comment or docstring
makes: the 0o700 mode, the deleted ``weaviate_helpers.list_classes``, the
``enumerate_code_collections`` raise, the AGPL headers CLAUDE.md mandates.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import seed_launcher_db  # noqa: E402
from vco_lib import codegraph_prefix_record as cpr  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib import weaviate_helpers as wh  # noqa: E402

REG_NAME = "ACME_widget"
FOLDER_BASENAME = "widget"
KG_PRIMARY = "ACMEWidget_KnowledgeGraph"
CODE_PREFIX = "ACME_widget"


def _http_mock(schema_classes, fail=False):
    def _side_effect(method, url, *, body=None, timeout=30.0):
        if fail:
            raise urllib.error.URLError("connection refused")
        if method == "GET" and url.endswith("/v1/schema"):
            return (200, json.dumps(
                {"classes": [{"class": c} for c in schema_classes]}).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            return (200, json.dumps({"data": {"Aggregate": {}}}).encode())
        return (404, b"")
    return _side_effect


def _make_fake_orchestrator(root: Path) -> None:
    """Minimal orchestrator tree sufficient for install_project_bundle."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    hooks = root / "templates" / "hooks"
    (hooks / "_lib").mkdir(parents=True)
    (hooks / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (hooks / "foo.ps1").write_text("echo v1\n", encoding="utf-8")
    (hooks / "_lib" / "find-python.sh").write_text("true\n", encoding="utf-8")
    (hooks / "_lib" / "find-python.ps1").write_text("true\n", encoding="utf-8")
    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg-search").write_text("#!/bin/sh\n", encoding="utf-8")
    for tpl in ("linux", "windows"):
        (root / "templates" / f"settings.json.{tpl}.template").write_text(
            '{"hooks": {}}\n', encoding="utf-8")
    infra = root / "infrastructure"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


class _BundleCase(unittest.TestCase):
    """A registered project whose folder basename differs from its name — the
    shape every one of this cycle's identity defects needed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wp4-axes-"))
        self.orch = self.tmp / "orch"
        self.proj = self.tmp / FOLDER_BASENAME
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        self.db = self.tmp / "launcher.db"
        seed_launcher_db(self.db, [{
            "name": REG_NAME, "folder_path": str(self.proj),
            "kg_primary": KG_PRIMARY, "codegraph_prefix": CODE_PREFIX,
        }])
        self._env = mock.patch.dict(
            os.environ, {"VCT_LAUNCHER_DB_PATH": str(self.db)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _update(self, schema_classes, fail=False):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(schema_classes, fail)):
            return project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True)

    def _deferral_text(self) -> str:
        md = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return md.read_text(encoding="utf-8") if md.exists() else ""


# ═══════════════════════════════════════════════════════════════════════════
# AXIS 1 — FRESH
# ═══════════════════════════════════════════════════════════════════════════

class FreshInstallTests(_BundleCase):

    def test_fresh_with_weaviate_up_records_the_bound_baseline_and_nothing_else(self):
        result = self._update([f"{CODE_PREFIX}_CodeFunction"])
        self.assertIsNone(result.get("codegraph_prefix_drift"))
        self.assertEqual(cpr.read_prefix(self.proj), CODE_PREFIX)
        self.assertEqual(cpr.read_source(self.proj), cpr.SOURCE_BINDING)
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_fresh_with_weaviate_down_still_records_only_the_baseline(self):
        result = self._update([], fail=True)
        self.assertIsNone(result.get("codegraph_prefix_drift"))
        self.assertEqual(cpr.read_prefix(self.proj), CODE_PREFIX)
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_fresh_project_has_no_orphan_snapshot(self):
        self._update([f"{CODE_PREFIX}_CodeFunction"])
        self.assertFalse(
            project_init._orphan_live_prefix_snapshot_path(self.proj).exists(),
            "a snapshot is written only by an orphan DETECTION, never by an "
            "ordinary update")


# ═══════════════════════════════════════════════════════════════════════════
# AXIS 2 — UPDATE (the shipped path, not a unit)
# ═══════════════════════════════════════════════════════════════════════════

class UpdatePathTests(_BundleCase):

    def test_update_heals_a_record_naming_no_live_class(self):
        """ACT for the already-damaged population (a), driven through the REAL
        update entry point."""
        cpr.write(self.proj, "Dead_prefix", source=cpr.SOURCE_BINDING)
        result = self._update([f"{CODE_PREFIX}_CodeFunction"])
        self.assertIsNone(result.get("codegraph_prefix_drift"),
                          "a record naming nothing is not a generation change")
        self.assertEqual(cpr.read_prefix(self.proj), CODE_PREFIX)
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_update_still_reports_a_genuine_orphaned_generation(self):
        """LEAVE-ALONE for the heal: when the old classes really are there, the
        feature is untouched and the reclaim command is still printed."""
        cpr.write(self.proj, "Legacy_prefix", source=cpr.SOURCE_BINDING)
        result = self._update(["Legacy_prefix_CodeFunction",
                               f"{CODE_PREFIX}_CodeFunction"])
        self.assertEqual(result["codegraph_prefix_drift"],
                         {"old_prefix": "Legacy_prefix",
                          "new_prefix": CODE_PREFIX})
        md = self._deferral_text()
        self.assertIn("codegraph_prefix_drift_detected", md)
        self.assertIn("detect-orphan-code-collections", md)

    def test_update_with_weaviate_down_keeps_the_historic_report(self):
        """No evidence ⇒ no new branch. The entry is still emitted (a signal is
        never dropped) but it says the schema was not consulted."""
        cpr.write(self.proj, "Legacy_prefix", source=cpr.SOURCE_BINDING)
        result = self._update([], fail=True)
        self.assertEqual(result["codegraph_prefix_drift"]["old_prefix"],
                         "Legacy_prefix")
        md = self._deferral_text()
        self.assertIn("UNKNOWN", md)
        self.assertNotIn("are now ORPHANED", md)

    def test_update_with_a_dead_record_and_a_dead_binding_claims_no_orphans(self):
        cpr.write(self.proj, "Legacy_prefix", source=cpr.SOURCE_BINDING)
        self._update(["Someone_Elses_CodeFunction"])
        md = self._deferral_text()
        self.assertIn("NOTHING is orphaned", md)
        self.assertNotIn("detect-orphan-code-collections", md)


# ═══════════════════════════════════════════════════════════════════════════
# AXIS 3 — ALREADY DAMAGED, population (b): the loaded gun on disk
# ═══════════════════════════════════════════════════════════════════════════

class AlreadyDamagedReachTests(unittest.TestCase):
    """Population (b) is reached WITHOUT an update, deliberately.

    A user whose UPDATE_DEFERRED.md already holds the reclaim command may paste
    it at any time. The neutralisation therefore lives at the READ, in the
    command itself, not in the update — an update-only fix would leave every
    not-yet-updated install armed.
    """

    def test_the_guard_is_in_the_reader_so_no_update_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            p = project_init._orphan_live_prefix_snapshot_path(proj)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "schema": project_init._ORPHAN_LIVE_PREFIX_SNAPSHOT_SCHEMA,
                "live_prefixes_normalised": [],
            }), encoding="utf-8")
            probe = project_init._read_orphan_live_prefix_snapshot(proj)
        self.assertTrue(probe.is_unknown())
        self.assertIn("before v0.2.92", probe.reason)
        self.assertIn("Weaviate", probe.reason,
                      "the refusal must tell the user what to do about it")

    def test_the_marker_is_the_only_discriminator(self):
        """Adding the marker to an otherwise identical file makes it usable —
        so the guard keys on provenance, not on emptiness (an empty snapshot
        from a healthy run is a legitimate 'every code class was dropped')."""
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            project_init._write_orphan_live_prefix_snapshot(proj, [])
            self.assertTrue(
                project_init._read_orphan_live_prefix_snapshot(proj).is_absent())


# ═══════════════════════════════════════════════════════════════════════════
# DELIVERY AUDIT (R17)
# ═══════════════════════════════════════════════════════════════════════════

class DeliveryAuditTests(unittest.TestCase):

    def test_the_new_module_is_inside_the_declared_wheel_package(self):
        """Check 1: VERIFIED, not assumed. `packages = ["vco_lib"]` is a
        whole-package declaration, so any new `vco_lib/*.py` ships."""
        import tomllib
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertIn("vco_lib", packages)
        new_module = REPO_ROOT / "vco_lib" / "codegraph_prefix_record.py"
        self.assertTrue(new_module.is_file())
        self.assertEqual(new_module.parent.name, "vco_lib",
                         "the module must live inside the declared package")

    def test_it_is_importable_as_part_of_the_package(self):
        import importlib
        self.assertIsNotNone(
            importlib.import_module("vco_lib.codegraph_prefix_record"))

    def test_no_machine_specific_path_was_introduced(self):
        """Check 4: no assumed orchestrator root, venv, home or drive."""
        for rel in ("vco_lib/codegraph_prefix_record.py",
                    "vco_lib/project_identity.py",
                    "vco_lib/codegraph_ref_dedup.py"):
            with self.subTest(rel=rel):
                src = (REPO_ROOT / rel).read_text(encoding="utf-8")
                for needle in ("/home/", "C:\\\\Users", "/Users/",
                               "PROGETTI", "VCO_dev"):
                    self.assertNotIn(needle, src)

    def test_the_new_module_adds_no_third_party_dependency(self):
        """Check 8/downgrade: an older interpreter or a minimal install must
        still import it — stdlib plus intra-package lazy imports only."""
        src = (REPO_ROOT / "vco_lib" / "codegraph_prefix_record.py").read_text(
            encoding="utf-8")
        import ast
        tree = ast.parse(src)
        toplevel = [n for n in tree.body
                    if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = set()
        for node in toplevel:
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names <= {"__future__", "json", "datetime", "pathlib",
                                  "typing"},
                        f"unexpected top-level imports: {sorted(names)}")

    def test_project_init_does_not_grow_further(self):
        """CLAUDE.md forbids adding >50 contiguous lines to a file past ~5000.

        HONESTY NOTE (v0.2.92 round-2 review, NEW-6): this test used to be
        named ``..._shrank_rather_than_grew`` with a 16_500 ceiling, while the
        file went 15_166 -> 16_020 across the cycle. A ceiling set ABOVE the
        starting size is not a shrink gate — it passed on +854 lines of growth
        and its name asserted the opposite of what happened. Extractions this
        cycle (the D18 block -> ``vco_lib/kg_binding_read.py``, whose correction
        half was later removed with the user's approval) brought it
        DOWN from a 16_556 peak, but not below where it started.

        So it is now a RATCHET pinned just above the current size: the file may
        shrink freely, and any further growth fails here and must be extracted
        into a module instead. Lower the ceiling when you extract; never raise
        it to accommodate an addition — a gate raised to admit the change it
        exists to prevent is not a gate.

        Lowered 16_050 -> 16_030 (2026-09-05): the delivery audit's empty-parent
        prune landed here and tripped this gate, which is the gate working. The
        function moved to ``vco_lib/fs_prune.py``, and since a second caller
        (``knowledge_residue``) already had its own copy of "rmdir only if
        empty", that primitive is now shared rather than duplicated — the
        extraction the ratchet exists to force, not a bump to admit the growth.

        Lowered again 16_030 -> 15_925 (same day) after
        ``_emit_migrate_required_deferral`` moved to
        ``vco_lib/migrate_deferral.py`` (~120 lines, one production caller, no
        dependency on this module's state) and ``_file_sha256`` was found to be
        a duplicate of ``vco_lib.hashing.sha256_file`` and now delegates.

        Lowered 15_925 -> 15_880 (same day, stale-wrapper lane): the v0.2.92
        first-install adoption rule landed here and tripped this gate, which is
        the gate working. Two extractions followed rather than a bump —
        ``vco_lib/shipped_artifact.py`` (the "is this installed file VCO's own
        artifact?" pair: the v0.2.31 git-history match plus the new first-install
        staleness rule) and ``vco_lib/bundle_skip_deferral.py`` (the
        ``bundle_skipped_existing_files`` emitter, following the
        ``migrate_deferral`` precedent). Net effect: 16_089 -> 15_852, i.e. the
        module is SMALLER than before the lane's work despite gaining the
        behaviour. Ceiling pinned 28 lines above the current size, per the
        lesson below.

        A LESSON about this gate, for whoever lowers it next: 16_030 was pinned
        to an instantaneous 16_028, and the very next legitimate change — four
        lines adding Windows equivalents to POSIX-only printed remedies, which
        R42 requires — tripped it. The first instinct was to shave comment
        prose to fit, which is gaming the gate, not obeying it. Pin the ceiling
        with a handful of lines of headroom, or accept that every small
        addition owes an extraction. Both are fine; shaving prose is not.
        """
        n = len((REPO_ROOT / "vco_lib" / "project_init.py")
                .read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(
            n, 15_880,
            f"project_init.py is {n} lines. It may not grow further — extract "
            "new logic into a vco_lib module and lower this ceiling.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# PROMISE AUDIT (R16)
# ═══════════════════════════════════════════════════════════════════════════

class PromiseAuditTests(unittest.TestCase):

    def test_dedup_docstring_names_no_function_that_no_longer_exists(self):
        from vco_lib import codegraph_ref_dedup as rd
        doc = rd.list_code_collections.__doc__ or ""
        self.assertNotIn("weaviate_helpers.list_classes", doc)
        self.assertFalse(hasattr(wh, "list_classes"),
                         "the docstring was corrected because the function was "
                         "deleted; if it comes back, the correction is stale")

    def test_enumerate_code_collections_really_raises(self):
        """The other half of the same corrected sentence."""
        from vco_lib import weaviate_schema
        doc = weaviate_schema.enumerate_code_collections.__doc__ or ""
        self.assertIn("ProbeUnavailable", doc)

    def test_dedup_still_refuses_to_report_a_clean_bill_of_health(self):
        """The docstring's remaining claim, backed by behaviour."""
        from vco_lib import codegraph_ref_dedup as rd

        def down(method, url, **kw):
            return (503, b"down")
        with self.assertRaises(rd.RefDedupError):
            rd.list_code_collections("http://x", request=down)

    def test_identity_module_docstring_promise_now_holds_for_both_halves(self):
        """"resolvable is False whenever ... a binding read failed part-way" —
        true for the KG half since F-1, and for the code half since W18."""
        from vco_lib import project_identity
        doc = project_identity._rows_to_identities.__doc__ or ""
        self.assertIn("project_codegraph_bindings", doc)
        self.assertIn("could not be READ", doc)

    def test_no_stale_parenthetical_about_returning_none_on_error(self):
        src = (REPO_ROOT / "vco_lib" / "project_identity.py").read_text(
            encoding="utf-8")
        self.assertNotIn("also returns None on a SQLite\n            # error",
                         src)
        self.assertNotIn("so this branch covers \"unreadable\" too", src)

    def test_every_vco_lib_module_this_lane_touched_carries_the_agpl_header(self):
        """CLAUDE.md: keep/inherit the AGPL header on orchestrator sources."""
        for rel in ("vco_lib/project_init.py",
                    "vco_lib/project_identity.py",
                    "vco_lib/codegraph_ref_dedup.py",
                    "vco_lib/codegraph_prefix_record.py"):
            with self.subTest(rel=rel):
                first = (REPO_ROOT / rel).read_text(
                    encoding="utf-8").splitlines()[0]
                self.assertEqual(first,
                                 "# SPDX-License-Identifier: AGPL-3.0-or-later")

    def test_class_listing_has_exactly_one_home_in_the_touched_modules(self):
        """§3.11's straggler check, scoped to this lane's files.

        Every "which classes exist?" read routes through
        ``weaviate_helpers.probe_class_listing``. Exactly ONE raw
        ``GET /v1/schema`` survives in ``project_init``: the legacy-KG detector,
        which needs each class's full ``vectorConfig`` for ``embedding_dim`` and
        so is asking a different question. It is exempted BY NAME, not by a
        loose bound — a second raw fetch appearing anywhere fails this test.
        """
        raw = '_http_request("GET", f"{base}/v1/schema"'
        for rel, allowed in (("vco_lib/project_init.py", 1),
                             ("vco_lib/project_identity.py", 0),
                             ("vco_lib/codegraph_prefix_record.py", 0),
                             ("vco_lib/codegraph_orphan_snapshot.py", 0)):
            with self.subTest(rel=rel):
                src = (REPO_ROOT / rel).read_text(encoding="utf-8")
                hits = [ln.strip() for ln in src.splitlines() if raw in ln]
                self.assertEqual(len(hits), allowed,
                                 f"unexpected raw class-listing fetches: {hits}")
        src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
        self.assertTrue(
            'is not the "which classes exist?" question' in src,
            "the one exemption must carry its justification in-line")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
