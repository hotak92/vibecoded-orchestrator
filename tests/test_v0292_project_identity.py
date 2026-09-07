# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W8 — identity-derived legacy-collection detection (data-loss guard).

THE BUG (field-confirmed 2026-08-31)
------------------------------------

A registered project whose folder BASENAME differs from its registered
``projects.name`` had its own live, correctly-bound collections classified as
"legacy data under a non-canonical prefix", and the install emitted MIGRATION
AND DROP commands against them: a live multi-thousand-object code graph plus a
live ``*_Development`` class the project reads through ``DEVELOPMENT_COLLECTION``.

Not move-specific — a folder move is only the commonest way to produce the
shape. Any registered project whose basename differs from its name and passes
``_is_similar_prefix`` reproduces it.

Four defects, one root cause (identity derived from the folder basename):

1. detectors sanitize ``folder.name`` instead of reading the registered name /
   the live bindings;
2. the emitted code-graph command was a bare ``_delete_class(...)`` with NO
   run-time re-validation (its KG sibling has one), and its follow-up
   re-analyze used the basename-derived project name;
3. the KG keep-set could not protect ``*_Development`` / ``*_Diagrams`` —
   ``project_kg_bindings`` stores no row for them (they are derived by suffix
   swap), and matching was on the full class name;
4. the code-prefix drift guard recorded the same wrong derivation into
   ``.claude/state/codegraph-prefix-generation.json``.

Both sides of every decision are tested: the ACT case (a genuinely foreign
class is still detected and still droppable — the feature is not neutered) and
the LEAVE-ALONE case (a project's own live data is never proposed, and the
emitted guards refuse).

All fixture names are generic (``ACME_widget`` / ``widget`` / ``ACMEWidget_*``).
No live Weaviate, no ambient launcher.db: the DB is pinned per test via
``VCT_LAUNCHER_DB_PATH``.
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

from tests.common.launcher_db_fixture import (  # noqa: E402
    create_empty_launcher_db,
    seed_launcher_db,
)
from vco_lib import project_identity, project_init  # noqa: E402

URL = "http://localhost:8081"

# ── The field shape, with generic names ────────────────────────────────────
#   registered name : ACME_widget          (launcher.db projects.name)
#   folder basename : widget               (the folder was moved / renamed)
#   KG primary      : ACMEWidget_KnowledgeGraph   (bound; underscore-DROPPING)
#   development     : ACMEWidget_Development      (derived; NO binding row)
#   code prefix     : ACME_widget                 (bound; underscore-PRESERVING)
REG_NAME = "ACME_widget"
FOLDER_BASENAME = "widget"
KG_PRIMARY = "ACMEWidget_KnowledgeGraph"
KG_DEV = "ACMEWidget_Development"
CODE_PREFIX = "ACME_widget"
CODE_FUNCTION = "ACME_widget_CodeFunction"


def _http_mock(schema_classes, counts=None, fail=False):
    counts = counts or {}

    def _side_effect(method, url, *, body=None, timeout=30.0):
        if fail:
            raise urllib.error.URLError("connection refused")
        if method == "GET" and url.endswith("/v1/schema"):
            payload = {"classes": [{"class": c} for c in schema_classes]}
            return (200, json.dumps(payload).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            query = (body or {}).get("query", "")
            for cls in schema_classes:
                if cls in query:
                    return (200, json.dumps({
                        "data": {"Aggregate": {
                            cls: [{"meta": {"count": counts.get(cls, 0)}}]}}
                    }).encode())
            return (200, json.dumps({"data": {"Aggregate": {}}}).encode())
        return (404, b"")

    return _side_effect


class _PinnedDbCase(unittest.TestCase):
    """Base: a temp dir, a project folder named ``widget``, a pinned DB."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-w8-"))
        self.proj = self.tmp / FOLDER_BASENAME
        self.proj.mkdir()
        self.db = self.tmp / "launcher.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def pin(self, db_path: Path | None = None):
        """Context manager pinning ``VCT_LAUNCHER_DB_PATH``."""
        return mock.patch.dict(
            os.environ,
            {"VCT_LAUNCHER_DB_PATH": str(db_path if db_path is not None else self.db)},
        )

    def seed_moved_project(self, **overrides):
        spec = {
            "name": REG_NAME,
            "folder_path": str(self.proj),
            "kg_primary": KG_PRIMARY,
            "codegraph_prefix": CODE_PREFIX,
        }
        spec.update(overrides)
        return seed_launcher_db(self.db, [spec])


# ═══════════════════════════════════════════════════════════════════════════
# R1 — one identity SSOT
# ═══════════════════════════════════════════════════════════════════════════

class IdentityResolutionTests(_PinnedDbCase):

    def test_registered_project_resolves_by_folder_not_basename(self):
        self.seed_moved_project()
        with self.pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertTrue(snap.resolvable)
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertTrue(identity.registered)
        # The REGISTERED name, not the folder basename.
        self.assertEqual(identity.name, REG_NAME)
        self.assertNotEqual(identity.name, FOLDER_BASENAME)
        # Every live bound collection, including the DERIVED siblings that have
        # no row in project_kg_bindings.
        self.assertEqual(identity.kg_primary, KG_PRIMARY)
        self.assertEqual(identity.development, KG_DEV)
        self.assertEqual(identity.diagrams, "ACMEWidget_Diagrams")
        self.assertEqual(identity.codegraph_prefix, CODE_PREFIX)
        self.assertIn(KG_DEV, identity.kg_collections())
        self.assertIn(CODE_FUNCTION, identity.code_collections())

    def test_canonical_prefix_comes_from_binding_not_sanitizer(self):
        # A custom-cased binding the name sanitizer would not reproduce.
        self.seed_moved_project(name="Widget Co", kg_primary="WDGT_KnowledgeGraph")
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
        assert identity is not None
        self.assertEqual(identity.kg_canonical_prefix(), "WDGT")

    def test_unregistered_folder_falls_back_to_basename(self):
        create_empty_launcher_db(self.db)  # readable, zero projects
        with self.pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertTrue(snap.resolvable)
        assert identity is not None
        self.assertFalse(identity.registered)
        self.assertEqual(identity.name, FOLDER_BASENAME)

    def test_explicit_fallback_name_wins_over_basename_when_unregistered(self):
        create_empty_launcher_db(self.db)
        with self.pin():
            identity, _ = project_identity.resolve_identity(
                self.proj, fallback_name="Given Name")
        assert identity is not None
        self.assertEqual(identity.name, "Given Name")

    def test_unreadable_db_yields_no_identity(self):
        # R6: NOT a basename fallback — no identity at all.
        with self.pin(self.tmp / "absent.db"):
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertFalse(snap.resolvable)
        self.assertIsNone(identity)

    def test_folder_match_is_symlink_and_trailing_slash_tolerant(self):
        self.seed_moved_project()
        link = self.tmp / "link-to-widget"
        try:
            link.symlink_to(self.proj, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows
            self.skipTest("symlinks unavailable")
        with self.pin():
            via_link, _ = project_identity.resolve_identity(link)
            via_slash, _ = project_identity.resolve_identity(str(self.proj) + os.sep)
        assert via_link is not None and via_slash is not None
        self.assertTrue(via_link.registered)
        self.assertTrue(via_slash.registered)
        self.assertEqual(via_link.name, REG_NAME)

    def test_canonical_folder_key_matches_config_projection_rule(self):
        # The folder→project matching rule must agree with the one inlined in
        # config_projection.resolve_collection_names_for_folder.
        from vco_lib import config_projection
        self.seed_moved_project()
        with self.pin():
            names = config_projection.resolve_collection_names_for_folder(
                Path(str(self.proj) + os.sep), db_path=self.db)
            identity, _ = project_identity.resolve_identity(
                Path(str(self.proj) + os.sep))
        assert identity is not None
        self.assertEqual(names["kg_collection"], identity.kg_primary)
        self.assertEqual(names["development_collection"], identity.development)
        self.assertEqual(names["diagrams_collection"], identity.diagrams)


# ═══════════════════════════════════════════════════════════════════════════
# R2 / R4 — self-binding exclusion across ALL roles (LEAVE-ALONE side)
# ═══════════════════════════════════════════════════════════════════════════

class SelfBindingExclusionTests(_PinnedDbCase):

    def _detect_kg(self, schema, counts=None):
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(schema, counts)):
                return project_init._detect_legacy_kg_collections(
                    identity.name, URL, identity=identity)  # type: ignore[union-attr]

    def _detect_code(self, schema, counts=None):
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(schema, counts)):
                return project_init._detect_legacy_codegraph_collections(
                    identity.name, URL, identity=identity)  # type: ignore[union-attr]

    def test_own_live_kg_and_development_never_flagged(self):
        self.seed_moved_project()
        got = self._detect_kg(
            [KG_PRIMARY, KG_DEV], {KG_PRIMARY: 2590, KG_DEV: 8})
        self.assertEqual(
            [c["class_name"] for c in got], [],
            "the project's own live KG + Development classes are not legacy")

    def test_own_live_codegraph_classes_never_flagged(self):
        self.seed_moved_project()
        got = self._detect_code([CODE_FUNCTION], {CODE_FUNCTION: 1692})
        self.assertEqual(
            [c["class_name"] for c in got], [],
            "the project's own live code-graph classes are not legacy")

    def test_regression_pre_w8_configuration_flagged_the_live_classes(self):
        """Red-proof: reconstruct the pre-W8 inputs and watch them flag live data.

        The three W8 corrections are independent, so each is disabled here in
        turn against the SAME live schema:

        * KG — keep-set restricted to what ``project_kg_bindings`` literally
          stores (primary/shared collection NAMES, no derived siblings, no
          prefixes) and matched on the full class name, i.e. exactly
          ``launcher_db_reader.kg_binding_keep_set()`` normalised. The live
          ``*_Development`` class is then a drop candidate.
        * code-graph — no keep-set at all (the pre-W8 wrapper passed none) and
          the KG sanitizer's prefix. The live 1692-object code graph is then a
          drop candidate.

        Re-running the SAME detectors through the W8 path returns nothing,
        which is the whole point.
        """
        self.seed_moved_project()
        schema = [KG_PRIMARY, KG_DEV, CODE_FUNCTION]
        counts = {KG_PRIMARY: 2590, KG_DEV: 8, CODE_FUNCTION: 1692}

        pre_w8_kg_keep = {
            project_init._normalise_prefix_for_match(KG_PRIMARY),
        }
        with self.pin(), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(schema, counts)), \
             mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=(pre_w8_kg_keep, True)):
            old_kg = project_init._detect_legacy_kg_collections(
                FOLDER_BASENAME, URL, live_binding="")
        self.assertIn(
            KG_DEV, [c["class_name"] for c in old_kg],
            "pre-W8 the live *_Development class was a drop candidate")

        with self.pin(), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(schema, counts)):
            old_code = project_init._detect_legacy_collections_with_suffixes(
                FOLDER_BASENAME, URL, project_init._CODEGRAPH_SUFFIXES)
        self.assertIn(
            CODE_FUNCTION, [c["class_name"] for c in old_code],
            "pre-W8 the live code graph was a drop candidate")

        # W8 path over the identical schema: nothing.
        self.assertEqual(self._detect_kg(schema, counts), [])
        self.assertEqual(self._detect_code(schema, counts), [])

    def test_another_projects_development_class_is_protected(self):
        # Cross-project: a DIFFERENT project's derived _Development class must
        # not be a drop target for this one either.
        seed_launcher_db(self.db, [
            {"name": REG_NAME, "folder_path": str(self.proj),
             "kg_primary": KG_PRIMARY, "codegraph_prefix": CODE_PREFIX},
            {"name": "ACME_widgetry", "folder_path": str(self.tmp / "other"),
             "kg_primary": "ACMEWidgetry_KnowledgeGraph",
             "codegraph_prefix": "ACME_widgetry"},
        ])
        got = self._detect_kg(
            ["ACMEWidgetry_Development"], {"ACMEWidgetry_Development": 12})
        self.assertEqual([c["class_name"] for c in got], [])

    def test_shared_kg_binding_is_protected(self):
        self.seed_moved_project(kg_shared="Shared_KnowledgeGraph")
        got = self._detect_kg(
            ["Shared_KnowledgeGraph"], {"Shared_KnowledgeGraph": 117})
        self.assertEqual([c["class_name"] for c in got], [])

    # ── ACT side: the feature still works ──────────────────────────────────

    def test_genuinely_foreign_kg_class_is_still_detected(self):
        self.seed_moved_project()
        got = self._detect_kg(
            ["ACMEWidgetOld_KnowledgeGraph"], {"ACMEWidgetOld_KnowledgeGraph": 42})
        self.assertEqual([c["class_name"] for c in got],
                         ["ACMEWidgetOld_KnowledgeGraph"])
        self.assertEqual(got[0]["canonical_name"], KG_PRIMARY)

    def test_genuinely_foreign_codegraph_class_is_still_detected(self):
        self.seed_moved_project()
        got = self._detect_code(
            ["ACME_widget_old_CodeFunction"], {"ACME_widget_old_CodeFunction": 5})
        self.assertEqual([c["class_name"] for c in got],
                         ["ACME_widget_old_CodeFunction"])
        # Canonical uses the code family's own underscore-PRESERVING rule.
        self.assertEqual(got[0]["canonical_name"], CODE_FUNCTION)


# ═══════════════════════════════════════════════════════════════════════════
# R3 — drop-guard parity (run-time re-validation on BOTH families)
# ═══════════════════════════════════════════════════════════════════════════

class DropGuardTests(_PinnedDbCase):

    def test_kg_guard_refuses_live_development_class(self):
        self.seed_moved_project()
        with self.pin():
            # Defect 3: this returned True (ALLOW) before W8.
            self.assertFalse(project_init._legacy_kg_drop_revalidated(KG_DEV))
            self.assertFalse(project_init._legacy_kg_drop_revalidated(KG_PRIMARY))

    def test_kg_guard_allows_genuine_orphan(self):
        self.seed_moved_project()
        with self.pin():
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated("Nobody_KnowledgeGraph"))

    def test_kg_guard_refuses_when_db_unreadable(self):
        with self.pin(self.tmp / "absent.db"):
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated("Nobody_KnowledgeGraph"))

    def test_codegraph_guard_refuses_live_bound_class(self):
        self.seed_moved_project()
        with self.pin():
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated(CODE_FUNCTION))

    def test_codegraph_guard_allows_genuine_orphan(self):
        self.seed_moved_project()
        with self.pin():
            self.assertTrue(
                project_init._legacy_codegraph_drop_revalidated(
                    "Nobody_CodeFunction"))

    def test_codegraph_guard_refuses_when_db_unreadable(self):
        with self.pin(self.tmp / "absent.db"):
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated(
                    "Nobody_CodeFunction"))

    def test_codegraph_guard_refuses_non_codegraph_name(self):
        self.seed_moved_project()
        with self.pin():
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated("NotAClassName"))

    def test_emitted_codegraph_command_embeds_the_guard(self):
        cands = [{"class_name": CODE_FUNCTION, "canonical_name": CODE_FUNCTION,
                  "object_count": 1692, "suffix": "_CodeFunction",
                  "case_only": False}]
        cmd = project_init._format_legacy_codegraph_command(REG_NAME, URL, cands)
        self.assertIn("_legacy_codegraph_drop_revalidated", cmd)
        self.assertIn("REFUSED", cmd)
        # And the re-analyze target is the AUTHORITATIVE name.
        self.assertIn(f"--project '{REG_NAME}'", cmd)
        self.assertNotIn(f"--project '{FOLDER_BASENAME}'", cmd)

    def test_emitted_kg_command_bootstraps_with_a_project_name(self):
        """``bootstrap-collections --name`` takes a PROJECT NAME, not a class.

        The pre-W8 renderer interpolated the candidate's canonical CLASS name,
        which double-derives: ``--name 'ACMEWidget_KnowledgeGraph'`` creates
        ``ACMEWidgetKnowledgeGraph_KnowledgeGraph`` and never prepares the class
        the following ``kg-sync --all`` is told to fill — so step 3's drop of
        the legacy class would destroy the only copy of the data.
        """
        cands = [{"class_name": "ACMEWidgetOld_KnowledgeGraph",
                  "canonical_name": KG_PRIMARY, "object_count": 42,
                  "suffix": "_KnowledgeGraph", "case_only": False}]
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, cands, project_folder=self.proj)
        self.assertIn(f"bootstrap-collections --name '{REG_NAME}'", cmd)
        self.assertNotIn(f"--name '{KG_PRIMARY}'", cmd)
        self.assertIn(f"--project-folder '{self.proj}'", cmd)
        # The double-derived garbage class must never be named anywhere.
        self.assertNotIn("ACMEWidgetKnowledgeGraph", cmd)

    def test_case_rebind_instruction_also_uses_the_project_name(self):
        cands = [{"class_name": "AcmeWidget_KnowledgeGraph",
                  "canonical_name": "ACMEWIDGET_KnowledgeGraph",
                  "object_count": 2590, "suffix": "_KnowledgeGraph",
                  "case_only": True}]
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, cands, project_folder=self.proj)
        self.assertIn("case-REBIND", cmd)
        self.assertNotIn("_delete_class", cmd)
        self.assertIn(f"bootstrap-collections --name '{REG_NAME}'", cmd)

    def test_emitted_codegraph_command_has_no_unguarded_delete(self):
        cands = [{"class_name": CODE_FUNCTION, "canonical_name": CODE_FUNCTION,
                  "object_count": 1692, "suffix": "_CodeFunction",
                  "case_only": False}]
        cmd = project_init._format_legacy_codegraph_command(REG_NAME, URL, cands)
        for line in cmd.splitlines():
            if "_delete_class(" in line:
                self.assertIn("_legacy_codegraph_drop_revalidated", line,
                              f"unguarded delete emitted: {line}")


# ═══════════════════════════════════════════════════════════════════════════
# R5 — prefix-generation file records the AUTHORITATIVE prefix
# ═══════════════════════════════════════════════════════════════════════════

class PrefixGenerationTests(_PinnedDbCase):

    def test_baseline_records_bound_prefix_and_source(self):
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=False, code_prefix=CODE_PREFIX)
        self.assertIsNone(drift)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj), CODE_PREFIX)
        payload = json.loads(
            project_init._codegraph_prefix_gen_path(self.proj).read_text())
        self.assertEqual(payload["source"], "binding")

    def test_poisoned_basename_record_is_corrected_not_reported_as_drift(self):
        # The pre-W8 guard wrote the FOLDER-BASENAME derivation here.
        poison = project_init.derive_project_code_prefix(FOLDER_BASENAME)
        project_init._write_codegraph_prefix_generation(self.proj, poison)
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=True, code_prefix=CODE_PREFIX)
        self.assertIsNone(drift, "a basename-derived record is not a generation change")
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj), CODE_PREFIX)
        md = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if md.exists():
            self.assertNotIn("codegraph_prefix_drift_detected", md.read_text())
        # The correction leaves an audit trail rather than being silent.
        jsonl = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(jsonl.is_file())
        self.assertIn("corrected_basename_derived_prefix_record", jsonl.read_text())

    def test_genuine_drift_still_reported(self):
        project_init._write_codegraph_prefix_generation(self.proj, "Legacy_prefix")
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=True, code_prefix=CODE_PREFIX)
        self.assertEqual(drift, {"old_prefix": "Legacy_prefix",
                                 "new_prefix": CODE_PREFIX})
        md = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
        self.assertIn("codegraph_prefix_drift_detected", md)

    def test_poison_correction_requires_an_authoritative_prefix(self):
        # Without a bound prefix we have nothing trustworthy to correct TO, so
        # the historic behaviour (report drift) is kept.
        poison = project_init.derive_project_code_prefix(FOLDER_BASENAME)
        project_init._write_codegraph_prefix_generation(self.proj, poison)
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=False)
        self.assertIsNotNone(drift)


# ═══════════════════════════════════════════════════════════════════════════
# R6 — conservative on unresolvable state (through the real install flow)
# ═══════════════════════════════════════════════════════════════════════════

def _make_fake_orchestrator(root: Path) -> None:
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
            "{\"hooks\": {}}\n", encoding="utf-8")
    infra = root / "infrastructure"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


class InstallFlowTests(_PinnedDbCase):
    """The end-to-end shape: install_project_bundle on the moved project."""

    def setUp(self):
        super().setUp()
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orchestrator(self.orch)

    def _run(self, schema, counts):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(schema, counts)):
            return project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True)

    def test_moved_project_yields_zero_candidates_and_no_deferral(self):
        self.seed_moved_project()
        with self.pin():
            result = self._run(
                [KG_PRIMARY, KG_DEV, CODE_FUNCTION],
                {KG_PRIMARY: 2590, KG_DEV: 8, CODE_FUNCTION: 1692})
        self.assertEqual(result["legacy_kg_candidates"], [])
        self.assertEqual(result["legacy_codegraph_candidates"], [])
        self.assertEqual(result["authoritative_identity"]["name"], REG_NAME)
        md = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if md.exists():
            text = md.read_text()
            self.assertNotIn("kg_collection_legacy_candidates", text)
            self.assertNotIn("codegraph_collection_legacy_candidates", text)
        # And the recorded prefix generation is the BOUND one.
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj), CODE_PREFIX)

    def test_unreadable_db_emits_nothing_at_all(self):
        with self.pin(self.tmp / "absent.db"):
            result = self._run(
                [KG_PRIMARY, KG_DEV, CODE_FUNCTION],
                {KG_PRIMARY: 2590, KG_DEV: 8, CODE_FUNCTION: 1692})
        self.assertEqual(result["legacy_kg_candidates"], [])
        self.assertEqual(result["legacy_codegraph_candidates"], [])
        self.assertIsNone(result["authoritative_identity"])
        self.assertIsNone(result.get("codegraph_prefix_drift"))
        self.assertIsNone(
            project_init._read_codegraph_prefix_generation(self.proj))

    def test_genuine_orphan_still_deferred_for_a_moved_project(self):
        # ACT side end-to-end: the project is registered under a different
        # basename AND a real orphan exists → the orphan is still reported.
        self.seed_moved_project()
        with self.pin():
            result = self._run(
                [KG_PRIMARY, KG_DEV, "ACMEWidgetOld_KnowledgeGraph"],
                {KG_PRIMARY: 2590, KG_DEV: 8,
                 "ACMEWidgetOld_KnowledgeGraph": 42})
        self.assertEqual(
            [c["class_name"] for c in result["legacy_kg_candidates"]],
            ["ACMEWidgetOld_KnowledgeGraph"])
        md = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
        self.assertIn("kg_collection_legacy_candidates", md)
        # The migration command names the AUTHORITATIVE canonical.
        self.assertIn(KG_PRIMARY, md)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
