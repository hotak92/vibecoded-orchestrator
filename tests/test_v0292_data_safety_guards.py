# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the legacy-collection guards must REFUSE what they cannot confirm.

Covers the five data-safety findings of the 2026-09-01 adversarial review of
the W8 change set. Every one of them sits in the mechanism whose entire purpose
is to stop VCO telling a user to delete their own live vector collections, so
both sides of every decision are pinned: the ACT case (a genuine orphan is
still detected and still droppable — the feature is not neutered) and the
LEAVE-ALONE case (live data is never proposed, and the guards refuse).

F-1  The run-time drop guards FAILED OPEN on a corrupt / foreign-schema
     launcher.db. ``_rows_to_identities`` mapped a failed
     ``SELECT … FROM projects`` to ``()`` while ``resolve_snapshot`` still
     reported ``resolvable=True``; SQLite opens lazily, so a non-SQLite byte
     blob read as "genuinely empty machine" and BOTH guards returned ALLOW for
     live class names. The same conflation pre-existed in
     ``launcher_db_reader``'s two keep-set helpers. Fixed in both homes.

F-2  The emit-time veto was launcher.db-ONLY: a project's own
     ``.claude/settings.json`` ``env`` pins — the channel the MCP actually
     resolves from — were never consulted, and with an identity present the
     env ``KG_COLLECTION`` consultation HEAD had was REPLACED rather than
     unioned. Now: a candidate matching this project's live binding from
     EITHER source is never legacy.

F-3  When a registered project's folder match failed (stale
     ``projects.folder_path``; a case-insensitive filesystem), the drift guard
     wrote a basename-derived prefix into
     ``.claude/state/codegraph-prefix-generation.json`` stamped
     ``source="binding"`` and emitted a FALSE drift deferral — the original
     field poisoning, now wearing an authoritative label. A derived prefix may
     no longer overwrite or contradict a record it cannot outrank.

F-4  The KG keep-set was NARROWED vs HEAD: the role-filtered read dropped
     non-derived ``archive``/unknown-role bound rows that HEAD's role-unfiltered
     ``kg_binding_keep_set()`` protected. A keep-set is a protection list.

F-9  The emitted ``bootstrap-collections --name … --project-folder …`` shape is
     pinned against the REAL argparse parser.

F-10 Python↔Rust parity fixture for ``normalise_for_match`` (CLAUDE.md's
     cross-language rule: a class-C mirror REQUIRES a parity test).

All fixture names are generic (``ACME_widget`` / ``widget`` / ``ACMEWidget_*``).
No live Weaviate; the launcher.db is pinned per test via
``VCT_LAUNCHER_DB_PATH`` so nothing depends on the developer's machine.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
    create_corrupt_launcher_db,
    create_empty_launcher_db,
    create_foreign_schema_launcher_db,
    seed_launcher_db,
)
from vco_lib import launcher_db_reader, project_identity, project_init  # noqa: E402

URL = "http://localhost:8081"

REG_NAME = "ACME_widget"
FOLDER_BASENAME = "widget"
KG_PRIMARY = "ACMEWidget_KnowledgeGraph"
KG_DEV = "ACMEWidget_Development"
CODE_PREFIX = "ACME_widget"
CODE_FUNCTION = "ACME_widget_CodeFunction"


def _http_mock(schema_classes, counts=None, fail=False):
    """Mock ``_http_request`` with a fixed Weaviate schema + object counts."""
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


class _Case(unittest.TestCase):
    """A temp tree with a project folder named ``widget`` and a pinned DB."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0292-safety-"))
        self.proj = self.tmp / FOLDER_BASENAME
        self.proj.mkdir()
        self.db = self.tmp / "launcher.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def pin(self, db_path: Path | None = None):
        return mock.patch.dict(
            os.environ,
            {"VCT_LAUNCHER_DB_PATH": str(
                db_path if db_path is not None else self.db)},
        )

    def write_settings_env(self, **env):
        """Write ``<proj>/.claude/settings.json`` with an ``env`` block."""
        target = self.proj / ".claude" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"env": env}, indent=2) + "\n", encoding="utf-8")
        return target

    def seed_registered(self, **overrides):
        spec = {
            "name": REG_NAME,
            "folder_path": str(self.proj),
            "kg_primary": KG_PRIMARY,
            "codegraph_prefix": CODE_PREFIX,
        }
        spec.update(overrides)
        return seed_launcher_db(self.db, [spec])


# ═══════════════════════════════════════════════════════════════════════════
# F-1 — unreadable launcher.db must make the guards REFUSE, not ALLOW
# ═══════════════════════════════════════════════════════════════════════════

class UnreadableDbRefusesDropTests(_Case):
    """The five launcher.db states, each landing on the correct side.

    RED-PROOF (against the pre-fix tree, verified by direct probe):
    ``corrupt`` and ``foreign_schema`` both produced
    ``resolvable=True, projects=()`` and BOTH guards returned ``True`` (ALLOW)
    for ``Live_KnowledgeGraph`` / ``Live_CodeFunction``.
    """

    LIVE_KG = "Live_KnowledgeGraph"
    LIVE_CODE = "Live_CodeFunction"

    def _guards(self):
        return (
            project_init._legacy_kg_drop_revalidated(self.LIVE_KG),
            project_init._legacy_codegraph_drop_revalidated(self.LIVE_CODE),
        )

    def _assert_refuses_for_the_right_reason(self, label: str):
        """Refusal must come from UNRESOLVABILITY, not incidental keep-set hits."""
        snap = project_identity.resolve_snapshot()
        self.assertFalse(
            snap.resolvable,
            f"[{label}] a launcher.db that cannot be READ is not resolvable")
        self.assertEqual(
            launcher_db_reader.kg_binding_keep_set(), ([], False),
            f"[{label}] KG keep-set must report unresolvable, not empty-and-true")
        self.assertEqual(
            launcher_db_reader.codegraph_binding_keep_set(), ([], False),
            f"[{label}] code keep-set must report unresolvable")
        self.assertEqual(
            self._guards(), (False, False),
            f"[{label}] both drop guards must REFUSE")

    def test_non_sqlite_blob_refuses(self):
        create_corrupt_launcher_db(self.db)
        with self.pin():
            self._assert_refuses_for_the_right_reason("corrupt")

    def test_foreign_schema_db_refuses(self):
        create_foreign_schema_launcher_db(self.db)
        with self.pin():
            self._assert_refuses_for_the_right_reason("foreign-schema")

    def test_absent_db_refuses(self):
        with self.pin(self.tmp / "nope.db"):
            self._assert_refuses_for_the_right_reason("absent")

    def test_locked_db_read_error_refuses(self):
        """A busy/locked DB surfaces as a raising read on an opened connection.

        Injected rather than produced with a real ``BEGIN EXCLUSIVE`` so the
        test does not pay ``_open_db_readonly``'s 5-second busy timeout; the
        code path exercised (``conn.execute`` raising ``OperationalError``) is
        identical to what a genuine lock delivers.
        """
        import sqlite3

        class _LockedConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

            def close(self):
                pass

        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               return_value=_LockedConn()):
            self.assertEqual(
                launcher_db_reader.kg_binding_keep_set(), ([], False))
            self.assertEqual(
                launcher_db_reader.codegraph_binding_keep_set(), ([], False))
            self.assertFalse(project_identity.resolve_snapshot().resolvable)
            self.assertEqual(self._guards(), (False, False))

    # ── ACT side: a READ database still permits the feature to work ────────

    def test_readable_empty_db_allows(self):
        """Readable-and-genuinely-empty is a DIFFERENT answer from unreadable."""
        create_empty_launcher_db(self.db)
        with self.pin():
            snap = project_identity.resolve_snapshot()
            self.assertTrue(snap.resolvable)
            self.assertEqual(snap.projects, ())
            self.assertEqual(
                launcher_db_reader.kg_binding_keep_set(), ([], True))
            self.assertEqual(
                launcher_db_reader.codegraph_binding_keep_set(), ([], True))
            self.assertEqual(self._guards(), (True, True))

    def test_readable_db_with_rows_allows_orphan_and_refuses_live(self):
        self.seed_registered()
        with self.pin():
            self.assertTrue(project_identity.resolve_snapshot().resolvable)
            # ACT: a class no project binds is droppable.
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated("Nobody_KnowledgeGraph"))
            self.assertTrue(
                project_init._legacy_codegraph_drop_revalidated(
                    "Nobody_CodeFunction"))
            # LEAVE ALONE: live bound data is not.
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(KG_PRIMARY))
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(KG_DEV))
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated(CODE_FUNCTION))

    def test_partial_binding_table_failure_refuses_whole_snapshot(self):
        """A per-project binding read that RAISES is 'cannot confirm', not 'none'.

        Degrading it to a name-derived primary (the pre-fix behaviour) silently
        drops a custom-bound collection out of the keep-set while the snapshot
        still claims to be resolvable.
        """
        self.seed_registered(kg_primary="Custom_Store")
        with self.pin(), mock.patch(
            "vco_lib.config_projection._fetch_kg_bindings",
            side_effect=RuntimeError("no such table: project_kg_bindings"),
        ):
            self.assertFalse(project_identity.resolve_snapshot().resolvable)
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated("Custom_Store"))


# ═══════════════════════════════════════════════════════════════════════════
# F-2 — the project's OWN settings.json is a veto source
# ═══════════════════════════════════════════════════════════════════════════

#: The hub-registered-but-never-bootstrapped shape: a `projects` row with NO
#: `project_kg_bindings` rows, so the identity resolver INVENTS a name-derived
#: primary while the real, populated class is named only by settings.json.
SETTINGS_KG = "ACMEWidgetTeam_KnowledgeGraph"
SETTINGS_DEV = "ACMEWidgetTeam_Development"
SETTINGS_CODE_PREFIX = "ACME_widget_team"
SETTINGS_CODE_FUNCTION = "ACME_widget_team_CodeFunction"


class SettingsPinnedSelfVetoTests(_Case):

    def _detect_kg(self, schema, counts=None, folder=None):
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(schema, counts)):
                return project_init._detect_legacy_kg_collections(
                    identity.name if identity else REG_NAME, URL,
                    identity=identity,
                    project_folder=self.proj if folder is None else folder,
                )

    def _detect_code(self, schema, counts=None, folder=None):
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(schema, counts)):
                return project_init._detect_legacy_codegraph_collections(
                    identity.name if identity else REG_NAME, URL,
                    identity=identity,
                    project_folder=self.proj if folder is None else folder,
                )

    def test_settings_pinned_kg_class_is_never_flagged(self):
        """The DB knows nothing about the collection; settings.json names it."""
        # Registered, but NO kg_primary binding row → the resolver name-derives
        # `ACMEWidget_KnowledgeGraph`, which is NOT what the project reads.
        self.seed_registered(kg_primary=None)
        self.write_settings_env(
            KG_COLLECTION=SETTINGS_KG, DEVELOPMENT_COLLECTION=SETTINGS_DEV)
        got = self._detect_kg(
            [SETTINGS_KG, SETTINGS_DEV],
            {SETTINGS_KG: 2590, SETTINGS_DEV: 8})
        self.assertEqual(
            [c["class_name"] for c in got], [],
            "a collection this project's settings.json pins is never legacy")

    def test_red_proof_without_the_settings_veto_the_live_class_is_flagged(self):
        """RED-PROOF for the case above: neutralise ONLY the settings veto.

        With `_self_veto_tokens` returning nothing — the pre-F-2 state, where
        detection consulted launcher.db alone — the live 2590-object class the
        MCP is reading right now becomes a migrate+drop candidate.
        """
        self.seed_registered(kg_primary=None)
        self.write_settings_env(
            KG_COLLECTION=SETTINGS_KG, DEVELOPMENT_COLLECTION=SETTINGS_DEV)
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(
                                       [SETTINGS_KG, SETTINGS_DEV],
                                       {SETTINGS_KG: 2590, SETTINGS_DEV: 8})), \
                 mock.patch.object(project_init, "_self_veto_tokens",
                                   return_value=set()):
                pre_fix = project_init._detect_legacy_kg_collections(
                    identity.name, URL, identity=identity,
                    project_folder=self.proj)
        self.assertIn(
            SETTINGS_KG, [c["class_name"] for c in pre_fix],
            "pre-F-2 the settings-pinned live KG class WAS a drop candidate")

    def test_settings_pinned_codegraph_prefix_is_never_flagged(self):
        self.seed_registered(codegraph_prefix=None, kg_primary=None)
        self.write_settings_env(CODE_GRAPH_PROJECT=SETTINGS_CODE_PREFIX)
        got = self._detect_code(
            [SETTINGS_CODE_FUNCTION], {SETTINGS_CODE_FUNCTION: 1692})
        self.assertEqual([c["class_name"] for c in got], [])

    def test_red_proof_settings_pinned_codegraph_prefix(self):
        self.seed_registered(codegraph_prefix=None, kg_primary=None)
        self.write_settings_env(CODE_GRAPH_PROJECT=SETTINGS_CODE_PREFIX)
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(
                                       [SETTINGS_CODE_FUNCTION],
                                       {SETTINGS_CODE_FUNCTION: 1692})), \
                 mock.patch.object(project_init, "_self_veto_tokens",
                                   return_value=set()):
                pre_fix = project_init._detect_legacy_codegraph_collections(
                    identity.name, URL, identity=identity,
                    project_folder=self.proj)
        self.assertIn(
            SETTINGS_CODE_FUNCTION, [c["class_name"] for c in pre_fix],
            "pre-F-2 the settings-pinned live code graph WAS a drop candidate")

    def test_env_kg_collection_is_UNIONED_not_replaced(self):
        """The narrow W8 regression vs HEAD, closed by union.

        With an identity present, `_detect_legacy_kg_collections` took
        `live_binding` from `identity.kg_primary` and stopped consulting the
        process env `KG_COLLECTION` that HEAD used whenever the subprocess
        carried it (a shell that sourced `.claude/env`). Both must protect.
        """
        self.seed_registered()  # binding = ACMEWidget_KnowledgeGraph
        env_pinned = "ACMEWidgetShell_KnowledgeGraph"
        with mock.patch.dict(os.environ, {"KG_COLLECTION": env_pinned}):
            got = self._detect_kg([env_pinned], {env_pinned: 400})
        self.assertEqual([c["class_name"] for c in got], [])

        # RED-PROOF: with the union removed, the env-named live class is flagged.
        with mock.patch.dict(os.environ, {"KG_COLLECTION": env_pinned}), \
             self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=_http_mock(
                                       [env_pinned], {env_pinned: 400})), \
                 mock.patch.object(project_init, "_self_veto_tokens",
                                   return_value=set()):
                pre_fix = project_init._detect_legacy_kg_collections(
                    identity.name, URL, identity=identity,
                    project_folder=self.proj)
        self.assertIn(env_pinned, [c["class_name"] for c in pre_fix])

    # ── ACT side: do NOT over-correct into a guard that never flags ────────

    def test_class_matching_neither_source_is_still_flagged(self):
        self.seed_registered(kg_primary=None)
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        got = self._detect_kg(
            ["ACMEWidgetOld_KnowledgeGraph"],
            {"ACMEWidgetOld_KnowledgeGraph": 42})
        self.assertEqual(
            [c["class_name"] for c in got], ["ACMEWidgetOld_KnowledgeGraph"],
            "a class in NEITHER launcher.db nor settings.json is still legacy")

    def test_codegraph_class_matching_neither_source_is_still_flagged(self):
        self.seed_registered()
        self.write_settings_env(CODE_GRAPH_PROJECT=CODE_PREFIX)
        got = self._detect_code(
            ["ACME_widget_old_CodeFunction"],
            {"ACME_widget_old_CodeFunction": 5})
        self.assertEqual(
            [c["class_name"] for c in got], ["ACME_widget_old_CodeFunction"])


class SettingsAwareRunTimeGuardTests(_Case):
    """The RUN-TIME guards consult settings.json too, when handed the folder."""

    def test_kg_guard_refuses_settings_pinned_class(self):
        # launcher.db knows a DIFFERENT primary; settings.json names the live one.
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        with self.pin():
            # Without the folder → DB-only → allowed (the pre-F-2 answer).
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated(SETTINGS_KG))
            # With the folder → refused.
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(
                    SETTINGS_KG, str(self.proj)))

    def test_kg_guard_refuses_settings_pinned_development_sibling(self):
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        with self.pin():
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(
                    SETTINGS_DEV, str(self.proj)),
                "the derived _Development sibling of a settings pin is live too")

    def test_kg_guard_still_allows_genuine_orphan_with_folder(self):
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        with self.pin():
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated(
                    "Nobody_KnowledgeGraph", str(self.proj)))

    def test_kg_guard_still_allows_a_basename_shaped_orphan(self):
        """Guard against over-correcting into a veto that refuses everything.

        The commonest genuine orphan IS named after the folder basename (that
        is what the pre-v0.2.92 basename derivation minted). The self-veto must
        not sweep it up: the folder is registered, so the veto resolves the
        BINDING names, and a basename-shaped class is in neither source.
        """
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        with self.pin():
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated(
                    "Widget_KnowledgeGraph", str(self.proj)))

    def test_codegraph_guard_refuses_settings_pinned_prefix(self):
        self.seed_registered()
        self.write_settings_env(CODE_GRAPH_PROJECT=SETTINGS_CODE_PREFIX)
        with self.pin():
            self.assertTrue(
                project_init._legacy_codegraph_drop_revalidated(
                    SETTINGS_CODE_FUNCTION))
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated(
                    SETTINGS_CODE_FUNCTION, str(self.proj)))

    def test_codegraph_guard_still_allows_genuine_orphan_with_folder(self):
        self.seed_registered()
        self.write_settings_env(CODE_GRAPH_PROJECT=SETTINGS_CODE_PREFIX)
        with self.pin():
            self.assertTrue(
                project_init._legacy_codegraph_drop_revalidated(
                    "Nobody_CodeFunction", str(self.proj)))

    def test_guard_refuses_when_the_settings_veto_itself_errors(self):
        """A veto source that blows up may never GRANT a drop."""
        self.seed_registered()
        with self.pin(), mock.patch.object(
            project_init, "_self_veto_tokens", side_effect=RuntimeError("boom"),
        ):
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(
                    "Nobody_KnowledgeGraph", str(self.proj)))
            self.assertFalse(
                project_init._legacy_codegraph_drop_revalidated(
                    "Nobody_CodeFunction", str(self.proj)))


class EmittedCommandCarriesTheFolderTests(_Case):
    """The emitted remediation must hand the folder to the run-time guard."""

    KG_CANDS = [{"class_name": "ACMEWidgetOld_KnowledgeGraph",
                 "canonical_name": KG_PRIMARY, "object_count": 42,
                 "suffix": "_KnowledgeGraph", "case_only": False}]
    CODE_CANDS = [{"class_name": "ACME_widget_old_CodeFunction",
                   "canonical_name": CODE_FUNCTION, "object_count": 5,
                   "suffix": "_CodeFunction", "case_only": False}]

    def _payloads(self, cmd: str) -> list[str]:
        """Every ``python -c "<payload>"`` payload in an emitted command."""
        out = []
        for line in cmd.splitlines():
            if not line.startswith("python -c "):
                continue
            parts = shlex.split(line)
            self.assertEqual(parts[:2], ["python", "-c"])
            out.append(parts[2])
        return out

    def test_kg_command_payload_is_valid_python_and_passes_the_folder(self):
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, self.KG_CANDS, project_folder=self.proj)
        payloads = self._payloads(cmd)
        self.assertTrue(payloads, "expected at least one guarded drop payload")
        for payload in payloads:
            compile(payload, "<emitted>", "exec")  # syntax-valid Python
            self.assertIn("_legacy_kg_drop_revalidated(n, f)", payload)
            self.assertIn(str(self.proj), payload)

    def test_codegraph_command_payload_is_valid_python_and_passes_the_folder(self):
        cmd = project_init._format_legacy_codegraph_command(
            REG_NAME, URL, self.CODE_CANDS, project_folder=self.proj)
        payloads = self._payloads(cmd)
        self.assertTrue(payloads)
        for payload in payloads:
            compile(payload, "<emitted>", "exec")
            self.assertIn("_legacy_codegraph_drop_revalidated(n, f)", payload)
            self.assertIn(str(self.proj), payload)

    def test_running_the_emitted_payload_refuses_a_settings_pinned_class(self):
        """END-TO-END: execute the payload VCO tells the user to paste.

        The command is shipped code. Rendering it correctly is not enough —
        what matters is what happens when it RUNS against a machine whose
        launcher.db does not know the collection but whose settings.json does.
        `_delete_class` is patched to record any call; it must never fire.
        """
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        cands = [{"class_name": SETTINGS_KG, "canonical_name": KG_PRIMARY,
                  "object_count": 2590, "suffix": "_KnowledgeGraph",
                  "case_only": False}]
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, cands, project_folder=self.proj)
        payload = self._payloads(cmd)[0]

        deleted: list[str] = []
        printed: list[str] = []
        with self.pin(), \
             mock.patch.object(project_init, "_delete_class",
                               side_effect=lambda n, **kw: deleted.append(n)):
            exec(compile(payload, "<emitted>", "exec"),
                 {"__builtins__": __builtins__, "print": printed.append})

        self.assertEqual(deleted, [],
                         "the emitted command DELETED a settings-pinned live class")
        self.assertTrue(printed and printed[0].startswith("REFUSED"), printed)

    def test_running_the_emitted_payload_still_drops_a_genuine_orphan(self):
        """ACT side of the same end-to-end: the remediation must still work."""
        self.seed_registered()
        self.write_settings_env(KG_COLLECTION=SETTINGS_KG)
        orphan = "ACMEWidgetOld_KnowledgeGraph"
        cands = [{"class_name": orphan, "canonical_name": KG_PRIMARY,
                  "object_count": 42, "suffix": "_KnowledgeGraph",
                  "case_only": False}]
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, cands, project_folder=self.proj)
        payload = self._payloads(cmd)[0]

        deleted: list[str] = []
        with self.pin(), \
             mock.patch.object(project_init, "_delete_class",
                               side_effect=lambda n, **kw: deleted.append(n)):
            exec(compile(payload, "<emitted>", "exec"),
                 {"__builtins__": __builtins__, "print": lambda *a: None})
        self.assertEqual(deleted, [orphan])

    def test_a_quote_bearing_folder_degrades_to_the_db_only_guard(self):
        """A path we cannot embed safely must yield '' — never a broken command.

        Both quote characters are load-bearing in the emitted line (shell
        double quotes around the payload, Python single quotes inside it), so a
        path containing either is dropped rather than emitted. The guard then
        does its launcher.db-only check, which is strictly LESS permissive than
        no guard at all.
        """
        weird = Path("/tmp/it's a \"project\"")
        self.assertEqual(project_init._embeddable_project_folder(weird), "")
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, self.KG_CANDS, project_folder=weird)
        for payload in self._payloads(cmd):
            compile(payload, "<emitted>", "exec")
            self.assertIn("f=''", payload)

    def test_no_unguarded_delete_survives_in_either_family(self):
        for cmd in (
            project_init._format_legacy_kg_command(
                REG_NAME, URL, self.KG_CANDS, project_folder=self.proj),
            project_init._format_legacy_codegraph_command(
                REG_NAME, URL, self.CODE_CANDS, project_folder=self.proj),
        ):
            for line in cmd.splitlines():
                if "_delete_class(" in line:
                    self.assertIn("_revalidated(", line,
                                  f"unguarded delete emitted: {line}")


# ═══════════════════════════════════════════════════════════════════════════
# F-3 — a DERIVED prefix must not overwrite or contradict the record
# ═══════════════════════════════════════════════════════════════════════════

class DerivedPrefixCannotOutrankRecordTests(_Case):

    GOOD_RECORD = "ACME_widget"   # what an authoritative run recorded

    def _record(self, prefix: str, source: str | None):
        path = project_init._codegraph_prefix_gen_path(self.proj)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": project_init._CODEGRAPH_PREFIX_GEN_SCHEMA,
            "collection_prefix": prefix,
        }
        if source is not None:
            payload["source"] = source
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _deferral_text(self) -> str:
        md = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return md.read_text(encoding="utf-8") if md.exists() else ""

    def test_pre_v0292_record_survives_a_derived_run(self):
        """The FIELD shape: stale folder_path → basename identity → no authority.

        RED-PROOF (pre-fix): `detect_codegraph_prefix_drift(..., code_prefix=
        identity.codegraph_prefix)` received the basename derivation, treated it
        as authoritative, emitted a FALSE `codegraph_prefix_drift_detected` and
        overwrote the good record with the basename value stamped
        `source="binding"`.
        """
        self._record(self.GOOD_RECORD, source=None)  # pre-v0.2.92 record
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, FOLDER_BASENAME, emit_deferral=True, code_prefix=None)
        self.assertIsNone(drift, "no authoritative answer ⇒ no drift claim")
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj),
            self.GOOD_RECORD, "the record must be left ALONE, not overwritten")
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_binding_sourced_record_survives_a_derived_run(self):
        self._record(self.GOOD_RECORD, source="binding")
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, FOLDER_BASENAME, emit_deferral=True, code_prefix=None)
        self.assertIsNone(drift)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj),
            self.GOOD_RECORD)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation_source(self.proj),
            "binding", "provenance must not be downgraded by a derived run")

    # ── ACT side: the guard still does its job ────────────────────────────

    def test_two_derived_generations_still_report_drift(self):
        """An unregistered standalone project keeps the feature it needs."""
        self._record("Legacy_prefix", source="derived")
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, FOLDER_BASENAME, emit_deferral=True, code_prefix=None)
        self.assertIsNotNone(drift)
        assert drift is not None
        self.assertEqual(drift["old_prefix"], "Legacy_prefix")
        self.assertIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_authoritative_run_still_reports_a_genuine_drift(self):
        self._record("Legacy_prefix", source="binding")
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=True, code_prefix=CODE_PREFIX)
        self.assertEqual(
            drift, {"old_prefix": "Legacy_prefix", "new_prefix": CODE_PREFIX})
        self.assertIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_authoritative_run_still_corrects_a_poisoned_record(self):
        """W8's poison self-repair is preserved by the F-3 change."""
        poison = project_init.derive_project_code_prefix(FOLDER_BASENAME)
        self._record(poison, source=None)
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, REG_NAME, emit_deferral=True, code_prefix=CODE_PREFIX)
        self.assertIsNone(drift)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj),
            CODE_PREFIX)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation_source(self.proj),
            "binding")

    def test_first_ever_derived_baseline_is_still_recorded_as_derived(self):
        drift = project_init.detect_codegraph_prefix_drift(
            self.proj, FOLDER_BASENAME, emit_deferral=False, code_prefix=None)
        self.assertIsNone(drift)
        self.assertEqual(
            project_init._read_codegraph_prefix_generation_source(self.proj),
            "derived")


class IdentityProvenanceTests(_Case):
    """`ProjectIdentity` must say WHERE each value came from (F-3's mechanism)."""

    def test_bound_project_reports_binding_provenance(self):
        self.seed_registered()
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
        assert identity is not None
        self.assertEqual(identity.kg_primary_source, project_identity.SOURCE_BINDING)
        self.assertEqual(
            identity.codegraph_prefix_source, project_identity.SOURCE_BINDING)
        self.assertEqual(identity.authoritative_codegraph_prefix(), CODE_PREFIX)

    def test_registered_but_unanalyzed_project_reports_derived(self):
        self.seed_registered(kg_primary=None, codegraph_prefix=None)
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
        assert identity is not None
        self.assertTrue(identity.registered)
        self.assertEqual(identity.kg_primary_source, project_identity.SOURCE_DERIVED)
        self.assertEqual(
            identity.codegraph_prefix_source, project_identity.SOURCE_DERIVED)
        self.assertIsNone(
            identity.authoritative_codegraph_prefix(),
            "a name-sanitized prefix must never be offered as authoritative")
        # ...but it IS still populated, because keep-set widening wants it.
        self.assertTrue(identity.codegraph_prefix)

    def test_unregistered_folder_identity_reports_derived(self):
        create_empty_launcher_db(self.db)
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
        assert identity is not None
        self.assertFalse(identity.registered)
        self.assertIsNone(identity.authoritative_codegraph_prefix())


class StaleFolderPathInstallFlowTests(_Case):
    """End-to-end: a registered project whose folder_path no longer matches."""

    def setUp(self):
        super().setUp()
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orchestrator(self.orch)

    def test_stale_folder_path_leaves_the_prefix_record_intact(self):
        # The DB points at the project's OLD location (an out-of-protocol move).
        seed_launcher_db(self.db, [{
            "name": REG_NAME,
            "folder_path": str(self.tmp / "old-location"),
            "kg_primary": KG_PRIMARY,
            "codegraph_prefix": CODE_PREFIX,
        }])
        # A good record written when the binding WAS resolvable.
        project_init._write_codegraph_prefix_generation(
            self.proj, CODE_PREFIX, source="binding")

        with self.pin(), mock.patch.object(
            project_init, "_http_request",
            side_effect=_http_mock([KG_PRIMARY], {KG_PRIMARY: 2590}),
        ):
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True)

        self.assertIsNone(result.get("codegraph_prefix_drift"),
                          "a folder-match failure is not a prefix generation change")
        self.assertEqual(
            project_init._read_codegraph_prefix_generation(self.proj),
            CODE_PREFIX, "the authoritative record must survive untouched")
        self.assertEqual(
            project_init._read_codegraph_prefix_generation_source(self.proj),
            "binding")
        md = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if md.exists():
            self.assertNotIn(
                "codegraph_prefix_drift_detected", md.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# F-4 — the keep-set is role-UNFILTERED (it is a protection list)
# ═══════════════════════════════════════════════════════════════════════════

class RoleUnfilteredKeepSetTests(_Case):

    ARCHIVE_CLASS = "ACMEWidgetArchive_KnowledgeGraph"

    def test_archive_role_bound_class_is_guard_protected(self):
        """RED-PROOF: with the primary/shared-only read, this guard ALLOWED it.

        HEAD's `kg_binding_keep_set()` was role-unfiltered by the explicit
        SEV-2 #2 invariant "any bound KG class is data we must never propose
        dropping"; routing the keep-set through a primary/shared-only read
        silently dropped `archive`-role rows (pre-v0.2.46 installs) from a
        PROTECTION list.
        """
        self.seed_registered(kg_extra_roles={"archive": self.ARCHIVE_CLASS})
        with self.pin():
            snap = project_identity.resolve_snapshot()
            self.assertIn(self.ARCHIVE_CLASS, snap.projects[0].kg_collections())
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated(self.ARCHIVE_CLASS))

    def test_archive_role_bound_class_is_never_flagged_by_detection(self):
        self.seed_registered(kg_extra_roles={"archive": self.ARCHIVE_CLASS})
        with self.pin():
            identity, _ = project_identity.resolve_identity(self.proj)
            with mock.patch.object(
                project_init, "_http_request",
                side_effect=_http_mock([self.ARCHIVE_CLASS],
                                       {self.ARCHIVE_CLASS: 300}),
            ):
                got = project_init._detect_legacy_kg_collections(
                    identity.name, URL, identity=identity,  # type: ignore[union-attr]
                    project_folder=self.proj)
        self.assertEqual([c["class_name"] for c in got], [])

    def test_head_role_unfiltered_reader_and_the_snapshot_agree(self):
        """The snapshot must not enumerate FEWER bound rows than HEAD's reader.

        v0.2.92 duplication-merge: this used to seed a ``future_role`` row as
        well. The REAL schema (migration 002) has
        ``CHECK (role IN ('primary','shared','archive'))``, so such a row cannot
        exist in a launcher-written DB — the hand-rolled fixture had no CHECK
        and let the test assert on a state production cannot reach. The
        role-unfiltered property is exercised on ``archive``, the third legal
        role, which is what the keep-set must not drop.
        """
        self.seed_registered(
            kg_shared="Shared_KnowledgeGraph",
            kg_extra_roles={"archive": self.ARCHIVE_CLASS})
        with self.pin():
            head_names, resolvable = launcher_db_reader.kg_binding_keep_set()
            snap = project_identity.resolve_snapshot()
        self.assertTrue(resolvable)
        tokens = snap.kg_keep_tokens()
        for name in head_names:
            self.assertIn(
                project_init._normalise_prefix_for_match(name), tokens,
                f"{name!r} is bound in launcher.db but absent from the keep-set")

    def test_unbound_class_is_still_droppable(self):
        self.seed_registered(kg_extra_roles={"archive": self.ARCHIVE_CLASS})
        with self.pin():
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated("Nobody_KnowledgeGraph"))


# ═══════════════════════════════════════════════════════════════════════════
# F-12 — --write-env must not hardcode the generic shared-KG literal, and a
#        configured-but-absent collection must FAIL LOUDLY
# ═══════════════════════════════════════════════════════════════════════════

class SharedKgResolutionTests(_Case):

    def test_orchestrator_root_binding_beats_the_generic_literal(self):
        """RED-PROOF: `_apply_standalone_env` hardcoded the literal.

        The literal is VCO's LAST-RESORT default. On an adopt-and-route install
        the root's own collection serves both roles, so the literal names a
        class that does not exist — the state that made one project's shared-KG
        searches silently return nothing for three days.
        """
        # v0.2.92: the fixture applies the REAL schema, which already has
        # `projects.host` — no ALTER needed (the hand-rolled one lacked it).
        seed_launcher_db(self.db, [{
            "project_id": "root", "name": "Orchestrator",
            "folder_path": "/tmp/orch", "slug": "orch",
            "host": "orchestrator_root",
            "kg_primary": "ACMERoot_KnowledgeGraph",
        }])
        with self.pin():
            got = project_init._resolve_shared_kg_name(self.proj)
        self.assertEqual(got, "ACMERoot_KnowledgeGraph")
        self.assertNotEqual(got, project_init._SHARED_KG_NAME)

    def test_existing_on_disk_pin_is_honoured(self):
        create_empty_launcher_db(self.db)
        self.write_settings_env(SHARED_KG_COLLECTION="ACMEShared_KnowledgeGraph")
        with self.pin():
            self.assertEqual(
                project_init._resolve_shared_kg_name(self.proj),
                "ACMEShared_KnowledgeGraph")

    def test_explicit_empty_pin_means_fan_out_disabled_and_is_preserved(self):
        create_empty_launcher_db(self.db)
        self.write_settings_env(SHARED_KG_COLLECTION="")
        with self.pin():
            self.assertEqual(project_init._resolve_shared_kg_name(self.proj), "")

    def test_last_resort_is_unchanged_when_nothing_resolves(self):
        with self.pin(self.tmp / "absent.db"):
            self.assertEqual(
                project_init._resolve_shared_kg_name(self.proj),
                project_init._SHARED_KG_NAME)

    def test_write_env_no_longer_hardcodes_the_literal(self):
        """The literal must not appear as a bare string in the standalone writer."""
        src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(
            encoding="utf-8")
        start = src.index("def _apply_standalone_env(")
        end = src.index("def _backfill_code_graph_project_env_in_project(", start)
        body = src[start:end]
        self.assertNotIn(
            '"VibeCodedOrchestrator_KnowledgeGraph"', body,
            "--write-env must resolve the shared-KG name, not hardcode it")
        self.assertIn("_resolve_shared_kg_name(", body)


class ConfiguredCollectionExistenceProbeTests(_Case):

    def test_absent_configured_collection_warns_loudly(self):
        self.write_settings_env(
            PROJECT_NAME=REG_NAME,
            KG_COLLECTION=KG_PRIMARY,
            SHARED_KG_COLLECTION="Phantom_KnowledgeGraph")
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([KG_PRIMARY])):
            report = project_init.verify_configured_collections_exist(
                self.proj, URL)
        self.assertTrue(report["probed"])
        self.assertEqual(report["absent"], ["Phantom_KnowledgeGraph"])
        self.assertEqual(report["present"], [KG_PRIMARY])
        self.assertTrue(report["warnings"])
        self.assertIn("Phantom_KnowledgeGraph", report["warnings"][0])
        self.assertIn("does NOT exist", report["warnings"][0])

    def test_unreachable_weaviate_reports_unknown_not_absent(self):
        """'Could not check' is not 'absent' — the F-1 conflation, not repeated."""
        self.write_settings_env(KG_COLLECTION=KG_PRIMARY)
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([], fail=True)):
            report = project_init.verify_configured_collections_exist(
                self.proj, URL)
        self.assertFalse(report["probed"])
        self.assertEqual(report["absent"], [])
        self.assertEqual(report["unknown"], [KG_PRIMARY])
        self.assertEqual(report["warnings"], [])

    def test_case_variant_on_disk_satisfies_a_configured_name(self):
        self.write_settings_env(KG_COLLECTION="ACMEWidget_KnowledgeGraph")
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(
                                   ["acmewidget_knowledgegraph"])):
            report = project_init.verify_configured_collections_exist(
                self.proj, URL)
        self.assertEqual(report["absent"], [])
        self.assertEqual(report["present"], ["ACMEWidget_KnowledgeGraph"])

    def test_empty_shared_pin_is_not_probed(self):
        self.write_settings_env(
            KG_COLLECTION=KG_PRIMARY, SHARED_KG_COLLECTION="")
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([KG_PRIMARY])):
            report = project_init.verify_configured_collections_exist(
                self.proj, URL)
        self.assertEqual(report["absent"], [])
        self.assertEqual(report["present"], [KG_PRIMARY])

    def test_probe_is_tri_state(self):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(["A_KnowledgeGraph"])):
            got = project_init.probe_classes_exist(
                ["A_KnowledgeGraph", "B_KnowledgeGraph"], URL)
        self.assertEqual(got, {"A_KnowledgeGraph": True, "B_KnowledgeGraph": False})
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([], fail=True)):
            got = project_init.probe_classes_exist(["A_KnowledgeGraph"], URL)
        self.assertEqual(got, {"A_KnowledgeGraph": None})

    def test_warning_names_a_command_the_parser_accepts(self):
        """The remediation is shipped code — its argv shape must parse."""
        self.write_settings_env(
            PROJECT_NAME=REG_NAME, KG_COLLECTION="Phantom_KnowledgeGraph")
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([])):
            report = project_init.verify_configured_collections_exist(
                self.proj, URL)
        warning = report["warnings"][0]
        cmd = re.search(r"`([^`]+)`", warning)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        _assert_project_init_argv_parses(self, cmd.group(1))


# ═══════════════════════════════════════════════════════════════════════════
# F-9 — the emitted `bootstrap-collections` argv shape is pinned
# ═══════════════════════════════════════════════════════════════════════════

def _assert_project_init_argv_parses(case: unittest.TestCase, command: str):
    """Feed a ``python -m vco_lib.project_init …`` line to the REAL parser."""
    argv = shlex.split(command)
    case.assertEqual(argv[:3], ["python", "-m", "vco_lib.project_init"],
                     f"unexpected invocation shape: {command!r}")
    parser = project_init._build_arg_parser()
    # argparse calls sys.exit on a rejected argv; surface it as a failure.
    try:
        ns, extras = parser.parse_known_args(argv[3:])
    except SystemExit as exc:  # pragma: no cover - only on a real regression
        raise AssertionError(
            f"the emitted command is REJECTED by the parser: {command!r}"
        ) from exc
    case.assertEqual(extras, [], f"unrecognized arguments in {command!r}")
    return ns


class BootstrapCollectionsArgvShapeTests(_Case):
    """F-9: nothing pinned the shape a user is told to paste."""

    def test_emitted_bootstrap_line_parses(self):
        cmd = project_init._bootstrap_collections_cmd(REG_NAME, self.proj)
        ns = _assert_project_init_argv_parses(self, cmd)
        self.assertEqual(ns.subcommand, "bootstrap-collections")
        self.assertEqual(ns.name, REG_NAME)
        self.assertEqual(ns.project_folder, str(self.proj))

    def test_emitted_bootstrap_line_parses_without_a_folder(self):
        ns = _assert_project_init_argv_parses(
            self, project_init._bootstrap_collections_cmd(REG_NAME))
        self.assertEqual(ns.name, REG_NAME)
        self.assertIsNone(ns.project_folder)

    def test_name_with_spaces_and_quotes_survives_the_round_trip(self):
        weird = "ACME widget's prod"
        ns = _assert_project_init_argv_parses(
            self, project_init._bootstrap_collections_cmd(weird, self.proj))
        self.assertEqual(ns.name, weird)

    def test_every_bootstrap_line_in_the_kg_remediation_parses(self):
        cands = [{"class_name": "ACMEWidgetOld_KnowledgeGraph",
                  "canonical_name": KG_PRIMARY, "object_count": 42,
                  "suffix": "_KnowledgeGraph", "case_only": False},
                 {"class_name": "AcmeWidget_KnowledgeGraph",
                  "canonical_name": "ACMEWIDGET_KnowledgeGraph",
                  "object_count": 2590, "suffix": "_KnowledgeGraph",
                  "case_only": True}]
        cmd = project_init._format_legacy_kg_command(
            REG_NAME, URL, cands, project_folder=self.proj)
        lines = [ln for ln in cmd.splitlines()
                 if "bootstrap-collections" in ln and not ln.startswith("#")]
        self.assertTrue(lines, "expected bootstrap-collections in the remediation")
        for line in lines:
            _assert_project_init_argv_parses(self, line)

    def test_a_bogus_flag_would_be_caught(self):
        """Prove the assertion has teeth (the sweep's own C-10 discipline)."""
        with self.assertRaises(AssertionError):
            _assert_project_init_argv_parses(
                self,
                "python -m vco_lib.project_init bootstrap-collections "
                "--name 'X' --definitely-not-a-flag")


# ═══════════════════════════════════════════════════════════════════════════
# F-10 — Python↔Rust parity for `normalise_for_match`
# ═══════════════════════════════════════════════════════════════════════════

_RUST_IDENTITY = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "project_identity.rs"
)

#: The parity corpus. This IS the rule, expressed as data so both languages can
#: assert against the same vectors. Mirrored to
#: ``tests/fixtures/normalise_for_match_parity.json``, consumed on the Rust
#: side by
#: ``launcher/src-tauri/src/commands/project_identity.rs
#: ::normalise_prefix_for_match_matches_shared_fixture`` (v0.2.92 WP-6 --
#: closes the F-10 gap this comment used to describe as still open).
PARITY_CORPUS: tuple[tuple[str, str], ...] = (
    ("VibeCodedOrchestrator", "vibecodedorchestrator"),
    ("VibeCoded Orchestrator", "vibecodedorchestrator"),
    ("vibecoded_orchestrator", "vibecodedorchestrator"),
    ("VibeCoded_Orchestrator", "vibecodedorchestrator"),
    ("Vibecodedorchestrator", "vibecodedorchestrator"),
    ("vibecoded-orchestrator", "vibecodedorchestrator"),
    ("Camel_Case", "camelcase"),
    ("CamelCase", "camelcase"),
    ("MyProject", "myproject"),
    ("", ""),
    ("___", ""),
    ("123abc", "123abc"),
    ("étude", "tude"),
    ("ACME_widget", "acmewidget"),
    ("ACMEWidget_KnowledgeGraph", "acmewidgetknowledgegraph"),
    ("  spaced  out  ", "spacedout"),
    ("Tab\tSep", "tabsep"),
    ("emoji🙂here", "emojihere"),
    ("Ünïcödé_Prôject", "ncdprject"),
    ("MiXeD123_CaSe", "mixed123case"),
)


class NormaliseForMatchParityTests(unittest.TestCase):
    """CLAUDE.md's cross-language rule: a class-C mirror REQUIRES a parity test.

    ``project_identity.normalise_for_match`` and
    ``commands/project_identity.rs::normalise_prefix_for_match`` both carried a
    "must match the other" comment and NEITHER carried a test. Cargo cannot run
    in this lane, so parity is pinned three ways, each of which fails loudly on
    a one-sided edit:

    1. the shared CORPUS is asserted against the Python implementation;
    2. the Rust function BODY is asserted to still be exactly
       ``filter(is_ascii_alphanumeric) → to_lowercase → collect`` — any change
       to it breaks this test and forces the corpus to be re-verified;
    3. the assertions inside the RUST file's own ``#[test]`` blocks for this
       function are EXTRACTED and re-asserted against Python, so vectors the
       Rust side already proves are proved on this side too.
    """

    def test_python_matches_the_corpus(self):
        for raw, expected in PARITY_CORPUS:
            with self.subTest(raw=raw):
                self.assertEqual(
                    project_identity.normalise_for_match(raw), expected)

    def test_project_init_delegates_to_the_one_home(self):
        for raw, expected in PARITY_CORPUS:
            self.assertEqual(
                project_init._normalise_prefix_for_match(raw), expected)

    def test_rust_body_still_implements_the_same_rule(self):
        if not _RUST_IDENTITY.is_file():  # pragma: no cover - source-tree only
            self.skipTest(f"{_RUST_IDENTITY} not present")
        src = _RUST_IDENTITY.read_text(encoding="utf-8")
        m = re.search(
            r"fn normalise_prefix_for_match\(s: &str\) -> String \{(.*?)\n\}",
            src, re.S)
        self.assertIsNotNone(m, "the Rust mirror was renamed or removed")
        assert m is not None
        body = " ".join(m.group(1).split())
        self.assertEqual(
            body,
            "s.chars() .filter(|c| c.is_ascii_alphanumeric()) "
            ".flat_map(|c| c.to_lowercase()) .collect()",
            "the Rust mirror changed — re-verify PARITY_CORPUS on both sides "
            "before updating this expectation (CLAUDE.md class-C mirror rule)")

    def test_rusts_own_test_vectors_hold_in_python(self):
        """Re-assert every literal the Rust unit tests pin, on the Python side."""
        if not _RUST_IDENTITY.is_file():  # pragma: no cover
            self.skipTest(f"{_RUST_IDENTITY} not present")
        src = _RUST_IDENTITY.read_text(encoding="utf-8")
        pairs = re.findall(
            r'assert_eq!\(\s*normalise_prefix_for_match\("((?:[^"\\]|\\.)*)"\)\s*,'
            r'\s*"((?:[^"\\]|\\.)*)"\s*\)',
            src)
        self.assertGreaterEqual(
            len(pairs), 4,
            "expected the Rust unit tests to pin literal vectors")
        for raw, expected in pairs:
            raw_py = raw.encode().decode("unicode_escape")
            expected_py = expected.encode().decode("unicode_escape")
            with self.subTest(raw=raw_py):
                self.assertEqual(
                    project_identity.normalise_for_match(raw_py), expected_py)

        # Equality-between-two-calls assertions (no literal expectation).
        equal_pairs = re.findall(
            r'assert_eq!\(\s*normalise_prefix_for_match\("((?:[^"\\]|\\.)*)"\)\s*,'
            r'\s*normalise_prefix_for_match\("((?:[^"\\]|\\.)*)"\)\s*\)',
            src)
        for a, b in equal_pairs:
            a_py = a.encode().decode("unicode_escape")
            b_py = b.encode().decode("unicode_escape")
            with self.subTest(pair=(a_py, b_py)):
                self.assertEqual(
                    project_identity.normalise_for_match(a_py),
                    project_identity.normalise_for_match(b_py))

        not_equal_pairs = re.findall(
            r'assert_ne!\(\s*normalise_prefix_for_match\("((?:[^"\\]|\\.)*)"\)\s*,'
            r'\s*normalise_prefix_for_match\("((?:[^"\\]|\\.)*)"\)\s*\)',
            src)
        for a, b in not_equal_pairs:
            a_py = a.encode().decode("unicode_escape")
            b_py = b.encode().decode("unicode_escape")
            with self.subTest(pair=(a_py, b_py)):
                self.assertNotEqual(
                    project_identity.normalise_for_match(a_py),
                    project_identity.normalise_for_match(b_py))

    def test_corpus_fixture_file_is_in_sync(self):
        """The JSON fixture a Rust-side parity test can consume must match."""
        fixture = REPO_ROOT / "tests" / "fixtures" / "normalise_for_match_parity.json"
        self.assertTrue(
            fixture.is_file(),
            f"{fixture} is the shared parity corpus — it must exist")
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            [list(pair) for pair in PARITY_CORPUS], data["vectors"],
            "the JSON corpus and PARITY_CORPUS drifted — update both")


def _make_fake_orchestrator(root: Path) -> None:
    """Minimal fake orchestrator tree sufficient for install_project_bundle."""
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
