# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.40 W40-A — cross-prefix KG binding self-heal.

The helper under test is ``install.py::_self_heal_kg_bindings_on_update``.
v0.2.23 B1 introduced a case-insensitive self-heal for binding rows that
differ only in casing from a live Weaviate class. v0.2.40 extends it
with a SECOND pass: when a binding row's ``collection_name`` is GENUINELY
MISSING from Weaviate (and has no case-sibling), probe for
``*_KnowledgeGraph`` / ``*_Development`` classes with non-zero row count
and auto-adopt the populated class when exactly one candidate matches.

Test coverage:

  * T1 — fresh install (no Weaviate collections) → second-pass no-op.
  * T2 — canonical-prefix user (collection exists at advertised name) →
    no change (first pass handles exact-match; second pass short-circuits).
  * T3 — VCO_dev shape: shared binding advertises
    ``VibeCodedOrchestrator_KnowledgeGraph`` (missing in Weaviate) while
    ``VCODev_KnowledgeGraph`` is populated → second pass adopts to
    ``VCODev_KnowledgeGraph`` and tags ``manual_override=v0.2.40-prefix-adopt``.
  * T4 — two populated candidates → ``multi_candidate_prefix_adopt``
    deferral, no auto-pick, binding row unchanged.
  * T5 — idempotency: running the function twice after adoption is a
    no-op on the second call.
  * T6 — ``_Development`` sibling: a binding row with ``_Development``
    suffix follows the same second-pass logic.
  * T7 — Weaviate aggregate transient error for a candidate: the row
    is left un-aligned (never adopted with unknown count).
  * T8 — unknown suffix on advertised name (not ``_KnowledgeGraph`` /
    ``_Development``) → no adoption, no deferral.

The stub Weaviate adds POST ``/v1/graphql`` support to the schema-stub
pattern used by ``test_install_self_heal_kg_bindings.py``: it responds
to the GraphQL Aggregate query the helper issues with a fixed row count
per class. The schema-listing GET handler is preserved so the existing
case-rebind path still works through the same fixture.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ─── Stub Weaviate HTTP server (schema + aggregate) ──────────────────────


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Stub Weaviate handler.

    Supports:
      * ``GET /v1/schema`` → returns ``self.__class__.schema``.
      * ``POST /v1/graphql`` → parses the Aggregate query, returns a
        ``{"data":{"Aggregate":{<class>: [{"meta":{"count": N}}]}}}``
        body using ``self.__class__.counts`` (default 0 per class).
      * ``POST /v1/graphql`` with ``self.__class__.aggregate_fail_for``
        set to a class name → returns 500 to simulate transient failure
        for that one class.
    """

    schema: dict = {"classes": []}
    counts: dict = {}
    aggregate_fail_for: str = ""

    # Regex to extract the class name out of the Aggregate query body.
    # Match `{ Aggregate { Foo { meta { count } } } }` and capture `Foo`.
    _AGG_RE = re.compile(r"Aggregate\s*\{\s*([A-Za-z0-9_]+)\s*\{")

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/schema":
            body = json.dumps(self.__class__.schema).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/graphql":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        query = payload.get("query") or ""
        m = self._AGG_RE.search(query)
        class_name = m.group(1) if m else ""
        if (
            class_name
            and class_name == self.__class__.aggregate_fail_for
        ):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "simulated transient failure"}')
            return
        count = int(self.__class__.counts.get(class_name, 0))
        body = json.dumps(
            {"data": {"Aggregate": {class_name: [{"meta": {"count": count}}]}}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        # Silence per-request stderr noise.
        pass


def _start_stub_weaviate(
    classes: list[str],
    counts: dict | None = None,
    aggregate_fail_for: str = "",
) -> tuple[http.server.HTTPServer, int]:
    """Start a stub Weaviate exposing the given class list + counts."""
    _StubHandler.schema = {
        "classes": [{"class": name} for name in classes]
    }
    _StubHandler.counts = dict(counts or {})
    _StubHandler.aggregate_fail_for = aggregate_fail_for
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Sanity-poll: confirm the listener is up before the test sends.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, port


# ─── launcher.db helpers ──────────────────────────────────────────────────


_PROJECT_KG_BINDINGS_DDL = """
CREATE TABLE project_kg_bindings (
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER,
    kg_dir_path TEXT,
    weaviate_url TEXT,
    config_json TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (project_id, role)
)
"""


def _build_launcher_db(
    db_path: Path,
    rows: list[tuple[str, str, str, str]],
) -> None:
    """Create launcher.db with project_kg_bindings seeded with rows.

    Each row is ``(project_id, role, collection_name, config_json)``.
    Use ``"{}"`` when no prior config_json is set.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_PROJECT_KG_BINDINGS_DDL)
        now = int(time.time() * 1000)
        for project_id, role, collection_name, config_json in rows:
            conn.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name, embedding_model, "
                "embedding_dim, kg_dir_path, weaviate_url, config_json, "
                "updated_at) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
                (project_id, role, collection_name, config_json, now),
            )
        conn.commit()
    finally:
        conn.close()


def _read_bindings(
    db_path: Path,
) -> list[tuple[str, str, str, str]]:
    """Return ``(project_id, role, collection_name, config_json)`` rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT project_id, role, collection_name, config_json "
            "FROM project_kg_bindings ORDER BY project_id, role"
        )
        return list(cur.fetchall())
    finally:
        conn.close()


# ─── Tests ───────────────────────────────────────────────────────────────


class CrossPrefixSelfHealTests(unittest.TestCase):
    """v0.2.40 W40-A — second-pass cross-prefix adoption."""

    def setUp(self):
        self._tmp = (
            Path(__file__).resolve().parent
            / f"_tmp_cross_prefix_{os.getpid()}_{id(self)}"
        )
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._db_path = self._tmp / "launcher.db"
        self._env_patch = mock.patch.dict(
            os.environ, {"VCT_STATE_DIR": str(self._tmp)}, clear=False
        )
        self._env_patch.start()
        self._server = None
        self._port = None

    def tearDown(self):
        self._env_patch.stop()
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k in ("WEAVIATE_URL", "WEAVIATE_PORT"):
            os.environ.pop(k, None)

    def _set_weaviate_url(self, port: int) -> None:
        os.environ["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"

    # ── T1 ───────────────────────────────────────────────────────────
    def test_t1_fresh_install_no_weaviate_classes_second_pass_noop(self):
        """No classes in Weaviate at all → second pass finds zero
        candidates per binding → no adoption, no deferral."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "primary", "MyOrch_KnowledgeGraph", "{}"),
                ("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph", "{}"),
            ],
        )
        # Empty schema → no candidates.
        self._server, self._port = _start_stub_weaviate(classes=[])
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Bindings unchanged.
        bindings = _read_bindings(self._db_path)
        coll_names = {(pid, role): coll for (pid, role, coll, _cfg) in bindings}
        self.assertEqual(
            coll_names[("p1", "primary")], "MyOrch_KnowledgeGraph"
        )
        self.assertEqual(
            coll_names[("p1", "shared")],
            "VibeCodedOrchestrator_KnowledgeGraph",
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("multi_candidate_prefix_adopt", ids)

    # ── T2 ───────────────────────────────────────────────────────────
    def test_t2_canonical_prefix_user_no_op(self):
        """Binding row's advertised collection exists exactly in
        Weaviate → exact-match path skips, second pass never fires."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "shared", "VibeCodedTools_KnowledgeGraph", "{}"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedTools_KnowledgeGraph"],
            counts={"VibeCodedTools_KnowledgeGraph": 500},
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            [(pid, role, coll) for (pid, role, coll, _cfg) in bindings],
            [("p1", "shared", "VibeCodedTools_KnowledgeGraph")],
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("multi_candidate_prefix_adopt", ids)

    # ── T3 (key) ─────────────────────────────────────────────────────
    def test_t3_vco_dev_state_adopts_populated_prefix(self):
        """VCO_dev's broken state:
            primary binding = VCODev_KnowledgeGraph (manual_override:v0.2.29-cleanup)
            shared  binding = VibeCodedOrchestrator_KnowledgeGraph
                              (manual_override:v0.2.28-recovery, MISSING in Weaviate)
            Weaviate holds  : VCODev_KnowledgeGraph (1033 objects)
        Expected: shared binding rebound to VCODev_KnowledgeGraph,
        config_json tagged with v0.2.40-prefix-adopt.
        """
        _build_launcher_db(
            self._db_path,
            rows=[
                (
                    "orchestrator-root", "primary",
                    "VCODev_KnowledgeGraph",
                    json.dumps({"manual_override": "v0.2.29-cleanup"}),
                ),
                (
                    "orchestrator-root", "shared",
                    "VibeCodedOrchestrator_KnowledgeGraph",
                    json.dumps({"manual_override": "v0.2.28-recovery"}),
                ),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"],
            counts={"VCODev_KnowledgeGraph": 1033},
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Shared binding now points at VCODev_KnowledgeGraph (the
        # populated class); primary binding unchanged.
        bindings = _read_bindings(self._db_path)
        by_role = {
            (pid, role): (coll, cfg)
            for (pid, role, coll, cfg) in bindings
        }
        primary_coll, primary_cfg = by_role[("orchestrator-root", "primary")]
        shared_coll, shared_cfg = by_role[("orchestrator-root", "shared")]

        self.assertEqual(primary_coll, "VCODev_KnowledgeGraph",
                         "primary row must not be touched")
        primary_cfg_parsed = json.loads(primary_cfg)
        self.assertEqual(
            primary_cfg_parsed.get("manual_override"),
            "v0.2.29-cleanup",
            "primary's manual_override sentinel must be preserved",
        )

        self.assertEqual(
            shared_coll, "VCODev_KnowledgeGraph",
            "shared row should be adopted to the populated class",
        )
        shared_cfg_parsed = json.loads(shared_cfg)
        self.assertEqual(
            shared_cfg_parsed.get("manual_override"),
            "v0.2.40-prefix-adopt",
            "shared row's manual_override should be updated to v0.2.40 tag",
        )

        # Deferral entry mentions the adoption.
        entries = report.entries
        ids = [e.condition_id for e in entries]
        self.assertIn("kg_binding_self_healed", ids)
        healed = next(e for e in entries
                      if e.condition_id == "kg_binding_self_healed")
        self.assertIn("VCODev_KnowledgeGraph", healed.detected)
        self.assertIn("VibeCodedOrchestrator_KnowledgeGraph", healed.detected)
        self.assertIn("1033", healed.detected)  # row count surfaced
        self.assertNotIn("multi_candidate_prefix_adopt", ids)

    # ── T4 (key) ─────────────────────────────────────────────────────
    def test_t4_multi_candidate_emits_deferral_no_auto_pick(self):
        """Two populated candidates with the same suffix → deferral
        entry, binding row unchanged."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "shared",
                 "VibeCodedOrchestrator_KnowledgeGraph", "{}"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=[
                "VCODev_KnowledgeGraph",
                "OtherProj_KnowledgeGraph",
            ],
            counts={
                "VCODev_KnowledgeGraph": 1033,
                "OtherProj_KnowledgeGraph": 442,
            },
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding row unchanged.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            [(pid, role, coll) for (pid, role, coll, _cfg) in bindings],
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )

        # Deferral entry emitted for ambiguity.
        entries = report.entries
        ids = [e.condition_id for e in entries]
        self.assertIn("multi_candidate_prefix_adopt", ids)
        multi = next(e for e in entries
                     if e.condition_id == "multi_candidate_prefix_adopt")
        # Both candidates and their row counts must be listed.
        self.assertIn("VCODev_KnowledgeGraph", multi.detected)
        self.assertIn("OtherProj_KnowledgeGraph", multi.detected)
        self.assertIn("1033", multi.detected)
        self.assertIn("442", multi.detected)
        # Sorted by row count descending — VCODev listed first.
        self.assertLess(
            multi.detected.index("VCODev_KnowledgeGraph"),
            multi.detected.index("OtherProj_KnowledgeGraph"),
        )
        # command_to_apply contains SQL for both alternatives, marked
        # with the v0.2.40 sentinel.
        self.assertIn("UPDATE project_kg_bindings", multi.command_to_apply)
        self.assertIn("v0.2.40-prefix-adopt", multi.command_to_apply)
        # Severity is warning (not info) — user must intervene.
        self.assertEqual(multi.severity, "warning")
        # The kg_binding_self_healed entry must NOT be emitted in
        # multi-only mode (there were no actual heals to report).
        self.assertNotIn("kg_binding_self_healed", ids)

    # ── T5 (key) ─────────────────────────────────────────────────────
    def test_t5_idempotency_second_run_is_noop(self):
        """After adoption, a second run finds the new collection
        already exists (exact match) and short-circuits — no further
        adoption, no new deferral."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("orchestrator-root", "shared",
                 "VibeCodedOrchestrator_KnowledgeGraph",
                 json.dumps({"manual_override": "v0.2.28-recovery"})),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"],
            counts={"VCODev_KnowledgeGraph": 1033},
        )
        self._set_weaviate_url(self._port)

        # First run: adopts.
        report1 = DeferralReport()
        install._self_heal_kg_bindings_on_update(report1)
        bindings_after = _read_bindings(self._db_path)
        self.assertEqual(
            bindings_after[0][2], "VCODev_KnowledgeGraph",
            "first run must adopt the populated class",
        )
        ids1 = [e.condition_id for e in report1.entries]
        self.assertIn("kg_binding_self_healed", ids1)

        # Second run: collection_name now equals a live class — no-op.
        report2 = DeferralReport()
        install._self_heal_kg_bindings_on_update(report2)
        bindings_after_2 = _read_bindings(self._db_path)
        self.assertEqual(
            bindings_after_2[0][2], "VCODev_KnowledgeGraph",
            "second run must not change the collection_name",
        )
        # Config still tagged with v0.2.40 sentinel from the first run.
        cfg = json.loads(bindings_after_2[0][3])
        self.assertEqual(
            cfg.get("manual_override"), "v0.2.40-prefix-adopt",
        )
        ids2 = [e.condition_id for e in report2.entries]
        self.assertNotIn(
            "kg_binding_self_healed", ids2,
            "second run must NOT emit a heal-completed entry",
        )
        self.assertNotIn("multi_candidate_prefix_adopt", ids2)

    # ── T6 ───────────────────────────────────────────────────────────
    def test_t6_development_suffix_adoption(self):
        """The same logic applies to ``_Development`` collections."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "dev",
                 "VibeCodedOrchestrator_Development", "{}"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_Development"],
            counts={"VCODev_Development": 419},
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = _read_bindings(self._db_path)
        self.assertEqual(bindings[0][2], "VCODev_Development")
        cfg = json.loads(bindings[0][3])
        self.assertEqual(
            cfg.get("manual_override"), "v0.2.40-prefix-adopt"
        )
        entries = report.entries
        healed = next(e for e in entries
                      if e.condition_id == "kg_binding_self_healed")
        self.assertIn("VCODev_Development", healed.detected)
        self.assertIn("419", healed.detected)

    # ── T7 ───────────────────────────────────────────────────────────
    def test_t7_transient_aggregate_failure_skips_candidate(self):
        """If the Aggregate query for a candidate returns 500, the
        candidate is treated as unknown count and skipped — never
        adopted blindly. With ONE candidate that fails, no adoption
        happens; the row stays put.
        """
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "shared",
                 "VibeCodedOrchestrator_KnowledgeGraph", "{}"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"],
            counts={"VCODev_KnowledgeGraph": 1033},
            aggregate_fail_for="VCODev_KnowledgeGraph",
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding row unchanged — never adopt blindly on unknown count.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings[0][2], "VibeCodedOrchestrator_KnowledgeGraph",
            "must not adopt when Weaviate Aggregate fails",
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("multi_candidate_prefix_adopt", ids)

    # ── T8 ───────────────────────────────────────────────────────────
    def test_t8_unknown_suffix_no_adoption(self):
        """Binding rows whose advertised name doesn't end in
        ``_KnowledgeGraph`` / ``_Development`` are left alone — the
        helper doesn't second-guess arbitrary user-set conventions.
        """
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "shared", "MysteryClassNoSuffix", "{}"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"],
            counts={"VCODev_KnowledgeGraph": 1033},
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = _read_bindings(self._db_path)
        self.assertEqual(bindings[0][2], "MysteryClassNoSuffix")
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("multi_candidate_prefix_adopt", ids)

    # ── T9 ───────────────────────────────────────────────────────────
    def test_t9_zero_populated_candidates_is_noop(self):
        """If candidate classes exist with the matching suffix but
        all have row_count == 0, no adoption happens (an empty class
        carries no signal that it's the right destination)."""
        _build_launcher_db(
            self._db_path,
            rows=[
                ("p1", "shared",
                 "VibeCodedOrchestrator_KnowledgeGraph", "{}"),
            ],
        )
        # VCODev_KnowledgeGraph exists but is EMPTY.
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"],
            counts={"VCODev_KnowledgeGraph": 0},
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings[0][2], "VibeCodedOrchestrator_KnowledgeGraph"
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("multi_candidate_prefix_adopt", ids)


# ─── Direct helper tests for _prefix_adopt_kg_bindings_pass ─────────────


class PrefixAdoptHelperTests(unittest.TestCase):
    """Direct tests on the extracted ``_prefix_adopt_kg_bindings_pass``
    helper, with a synthetic Weaviate URL routed at the stub. These
    pin the contract independently of the surrounding self-heal
    orchestration so a refactor of the outer function can't silently
    revert the contract.
    """

    def setUp(self):
        self._server, self._port = _start_stub_weaviate(
            classes=["Foo_KnowledgeGraph", "Bar_KnowledgeGraph"],
            counts={"Foo_KnowledgeGraph": 10, "Bar_KnowledgeGraph": 20},
        )
        self._weaviate_url = f"http://127.0.0.1:{self._port}"
        self._conn = sqlite3.connect(":memory:")
        self._cur = self._conn.cursor()
        self._cur.execute(_PROJECT_KG_BINDINGS_DDL)

    def tearDown(self):
        self._conn.close()
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def _insert(self, project_id, role, collection_name, config_json="{}"):
        self._cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, embedding_model, "
            "embedding_dim, kg_dir_path, weaviate_url, config_json, "
            "updated_at) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            (project_id, role, collection_name, config_json,
             int(time.time() * 1000)),
        )

    def test_helper_returns_adopts_and_multi_candidates_keys(self):
        """Contract: the helper always returns a dict with both
        ``adopts`` and ``multi_candidates`` keys (lists, possibly empty).
        """
        existing = {"Foo_KnowledgeGraph", "Bar_KnowledgeGraph"}
        result = install._prefix_adopt_kg_bindings_pass(
            self._cur,
            existing_classes=existing,
            existing_by_lower={n.lower(): n for n in existing},
            weaviate_url=self._weaviate_url,
        )
        self.assertIn("adopts", result)
        self.assertIn("multi_candidates", result)
        self.assertIsInstance(result["adopts"], list)
        self.assertIsInstance(result["multi_candidates"], list)

    def test_helper_preserves_other_config_json_keys(self):
        """When adopting, the helper must NOT clobber unrelated keys
        in ``config_json`` — only set/overwrite ``manual_override``."""
        self._insert(
            "p1", "shared", "Missing_KnowledgeGraph",
            config_json=json.dumps({
                "owner": "user-7",
                "audit_trail": ["created v0.2.10"],
            }),
        )
        # Stub has only Foo_KnowledgeGraph at row count 10 here — but
        # we need EXACTLY one candidate. Use a different stub config:
        self._server.shutdown()
        self._server, self._port = _start_stub_weaviate(
            classes=["Solo_KnowledgeGraph"],
            counts={"Solo_KnowledgeGraph": 7},
        )
        self._weaviate_url = f"http://127.0.0.1:{self._port}"

        existing = {"Solo_KnowledgeGraph"}
        install._prefix_adopt_kg_bindings_pass(
            self._cur,
            existing_classes=existing,
            existing_by_lower={n.lower(): n for n in existing},
            weaviate_url=self._weaviate_url,
        )

        self._cur.execute(
            "SELECT collection_name, config_json "
            "FROM project_kg_bindings WHERE project_id = ? AND role = ?",
            ("p1", "shared"),
        )
        coll, cfg_str = self._cur.fetchone()
        self.assertEqual(coll, "Solo_KnowledgeGraph")
        cfg = json.loads(cfg_str)
        # Pre-existing keys preserved.
        self.assertEqual(cfg.get("owner"), "user-7")
        self.assertEqual(cfg.get("audit_trail"), ["created v0.2.10"])
        # v0.2.40 sentinel applied.
        self.assertEqual(
            cfg.get("manual_override"), "v0.2.40-prefix-adopt"
        )


if __name__ == "__main__":
    unittest.main()
