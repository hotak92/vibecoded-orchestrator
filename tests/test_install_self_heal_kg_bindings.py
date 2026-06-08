# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 B1 (2026-05-21) — case-mismatch self-heal for launcher.db
``project_kg_bindings`` rows.

The helper under test is ``install.py::_self_heal_kg_bindings_on_update``.
It runs from the ``install.py --update`` flow alongside
``_detect_legacy_shared_kg_class`` and:

  1. Reads ``~/.vct/launcher.db`` (or ``$VCT_STATE_DIR/launcher.db``).
  2. Reads the Weaviate schema (``GET /v1/schema``).
  3. For every ``project_kg_bindings`` row whose ``collection_name``
     differs only in casing from a class actually in Weaviate, UPDATEs
     the row to point at the on-disk casing.
  4. Emits an informational deferral entry summarising every rebind.

Test coverage:

  * Rewrite happens when a case-different sibling exists in Weaviate.
  * No-op when the canonical class already matches (exact equality).
  * No-op when no case-different sibling exists (genuine missing class —
    leave the row alone; orphan-prune sync recreates lazily).
  * Soft-fail when launcher.db is absent (fresh first-install) — no
    crash, no deferral entry beyond the skip log.

The tests stub out Weaviate via a small in-process HTTP server
(``http.server.BaseHTTPRequestHandler``) so the helper's
``urllib.request.urlopen`` call lands in the test's fixture instead of
hitting a real Weaviate. The launcher.db is built via ``sqlite3``
directly because the launcher's own migrations live in Rust and aren't
callable from Python tests — we hand-roll just enough schema for the
helper to see.
"""

from __future__ import annotations

import http.server
import json
import os
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


# ─── Stub Weaviate HTTP server ────────────────────────────────────────────


class _StubSchemaHandler(http.server.BaseHTTPRequestHandler):
    """Returns a fixed schema payload on GET /v1/schema."""

    schema: dict = {"classes": []}

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
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

    def log_message(self, *args, **kwargs):
        # Silence per-request stderr noise.
        pass


def _start_stub_weaviate(classes: list[str]) -> tuple[http.server.HTTPServer, int]:
    """Start a stub Weaviate that serves the given class list on
    ``/v1/schema``. Returns ``(server, port)``.

    Random-port binding (port=0) so parallel test runs don't collide.
    """
    _StubSchemaHandler.schema = {
        "classes": [{"class": name} for name in classes]
    }
    # Bind to localhost ephemeral port — random so concurrent tests
    # don't collide.
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubSchemaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Sanity-poll: open a socket to confirm the listener is up before
    # the test issues its first request.
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


def _build_launcher_db(db_path: Path, rows: list[tuple[str, str, str]]) -> None:
    """Create launcher.db with a project_kg_bindings table seeded with rows.

    Each row is ``(project_id, role, collection_name)``. Other columns
    get sane defaults (None / '{}' / current millis).

    v0.2.49 Bug N: also sets ``journal_mode = WAL`` to mirror the
    launcher's production config (`launcher/src-tauri/vct-launcher-core/
    src/db.rs::init_pragmas`). In WAL mode, RO connections do NOT block
    on writer transactions — which is the load-bearing property the
    RO-first detection path in `_self_heal_kg_bindings_on_update`
    depends on. Without WAL, the default rollback-journal mode causes
    RO connections to block on writer transactions just like RW ones,
    making the Bug N regression test indistinguishable from the
    pre-fix path.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_PROJECT_KG_BINDINGS_DDL)
        now = int(time.time() * 1000)
        for project_id, role, collection_name in rows:
            conn.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name, embedding_model, "
                "embedding_dim, kg_dir_path, weaviate_url, config_json, "
                "updated_at) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, '{}', ?)",
                (project_id, role, collection_name, now),
            )
        conn.commit()
    finally:
        conn.close()


def _read_bindings(db_path: Path) -> list[tuple[str, str, str]]:
    """Read ``(project_id, role, collection_name)`` triples from launcher.db."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT project_id, role, collection_name FROM project_kg_bindings "
            "ORDER BY project_id, role"
        )
        return list(cur.fetchall())
    finally:
        conn.close()


# ─── Tests ───────────────────────────────────────────────────────────────


class SelfHealCaseMismatchTests(unittest.TestCase):
    """Headline contract: rebind binding rows whose collection_name only
    differs in casing from a live class in Weaviate."""

    def setUp(self):
        # Per-test temp dir — VCT_STATE_DIR points at it so launcher.db
        # resolves there. Cleanup in tearDown.
        self._tmp = Path(__file__).resolve().parent / f"_tmp_self_heal_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._db_path = self._tmp / "launcher.db"
        # Patch VCT_STATE_DIR via env so `_discover_app_state_db_path`
        # resolves here instead of `~/.vct`.
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
        # Also pop WEAVIATE_URL/PORT we may have set.
        for k in ("WEAVIATE_URL", "WEAVIATE_PORT"):
            os.environ.pop(k, None)

    def _set_weaviate_url(self, port: int) -> None:
        os.environ["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"

    def test_self_heal_rewrites_binding_when_case_variant_exists(self):
        # Pre-seed launcher.db with a lowercase-c binding row.
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph")],
        )
        # Pre-seed Weaviate with the capital-C canonical class.
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding row was rebound to the on-disk capital-C casing.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
            "expected binding to be rebound to the live class casing",
        )

        # An informational deferral entry was emitted.
        entries = report.entries
        ids = [e.condition_id for e in entries]
        self.assertIn("kg_binding_self_healed", ids)
        healed = next(e for e in entries
                      if e.condition_id == "kg_binding_self_healed")
        self.assertEqual(healed.severity, "info")
        # The detected message must mention the rebind direction so the
        # user has an audit trail.
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph", healed.detected)
        self.assertIn("VibeCodedOrchestrator_KnowledgeGraph", healed.detected)
        self.assertIn("p1", healed.detected)
        self.assertIn("shared", healed.detected)

    def test_self_heal_no_op_when_canonical_exists(self):
        # Pre-seed launcher.db AND Weaviate with the canonical capital-C
        # — exact match, no rebind expected.
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding row unchanged.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
            "exact-match binding must not be rewritten",
        )

        # No deferral entry — there was nothing to heal.
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)

    def test_self_heal_no_op_uses_ro_mode_and_does_not_open_rw(self):
        """v0.2.49 Bug N regression: when no rebind is needed, the helper
        opens launcher.db in RO mode for detection, finds no work, and
        returns cleanly WITHOUT ever opening an RW connection.

        Pre-Bug-N, this helper opened launcher.db in RW mode unconditionally
        with timeout=5.0. On hosts where vct-hub is running (the design —
        hub outlives launcher GUI), every `install.py --update` run hit
        the 5s writer-lock timeout and emitted a
        `kg_binding_self_heal_db_error` deferral, even when there was
        literally nothing to heal. RL chat msg 179 (2026-06-07) reported
        this and recommended Option A: open RO for detection, only reopen
        RW if mismatches found.

        Reproducing the exact production lock state in a hermetic test is
        flaky (depends on whether SQLite's connect-time WAL recovery hits
        the writer lock for that specific filesystem + journal-mode +
        SQLite version combination). Instead, pin the CONTRACT
        deterministically: intercept sqlite3.connect calls and assert
        that for a no-rebind launcher.db, the helper opens RO exactly
        once and RW exactly zero times. Pre-Bug-N would open RW once and
        RO zero times. The behavioral difference is observable without
        depending on lock-timing.
        """
        # Pre-seed: exact-match binding (no rebind needed — the common case).
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        # Intercept sqlite3.connect to count RO vs RW calls. The fix
        # opens RO via `sqlite3.connect("file:<path>?mode=ro", uri=True)`;
        # the legacy RW path is `sqlite3.connect(str_path)`.
        ro_calls: list[str] = []
        rw_calls: list[str] = []
        real_connect = sqlite3.connect

        def _tracking_connect(*args, **kwargs):
            dsn = args[0] if args else kwargs.get("database", "")
            if kwargs.get("uri") and isinstance(dsn, str) and "mode=ro" in dsn:
                ro_calls.append(dsn)
            else:
                rw_calls.append(dsn)
            return real_connect(*args, **kwargs)

        report = DeferralReport()
        # `import sqlite3` is local-to-function inside the helper, so
        # patch sqlite3.connect at the module level — both the helper's
        # local import and any other consumer in this process will see
        # the patched callable.
        with mock.patch("sqlite3.connect", side_effect=_tracking_connect):
            install._self_heal_kg_bindings_on_update(report)

        # Post-Bug-N contract: exactly one RO open, ZERO RW opens.
        self.assertEqual(
            len(ro_calls),
            1,
            f"Bug N: helper should open RO connection ONCE for detection. "
            f"Got {len(ro_calls)} RO calls: {ro_calls}",
        )
        self.assertEqual(
            len(rw_calls),
            0,
            f"Bug N regression: helper should NOT open RW connection when "
            f"detection finds no rebinds needed (would block on the vct-hub "
            f"writer lock in production). Got {len(rw_calls)} RW calls: "
            f"{rw_calls}",
        )

        # And no deferral entry — RO probe found nothing to heal.
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_heal_db_error", ids)
        self.assertNotIn("kg_binding_self_healed", ids)

    def test_self_heal_rebind_path_still_opens_rw(self):
        """v0.2.49 Bug N: when detection DOES find rebinds, the helper
        proceeds to open RW + apply them. Inverse pin: the RO-first
        optimization must not break the rebind-needed path.
        """
        # Pre-seed: lowercase-c binding that needs a rebind.
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        ro_calls: list[str] = []
        rw_calls: list[str] = []
        real_connect = sqlite3.connect

        def _tracking_connect(*args, **kwargs):
            dsn = args[0] if args else kwargs.get("database", "")
            if kwargs.get("uri") and isinstance(dsn, str) and "mode=ro" in dsn:
                ro_calls.append(dsn)
            else:
                rw_calls.append(dsn)
            return real_connect(*args, **kwargs)

        report = DeferralReport()
        # `import sqlite3` is local-to-function inside the helper, so
        # patch sqlite3.connect at the module level — both the helper's
        # local import and any other consumer in this process will see
        # the patched callable.
        with mock.patch("sqlite3.connect", side_effect=_tracking_connect):
            install._self_heal_kg_bindings_on_update(report)

        # RO opened for detection, RW opened to apply.
        self.assertEqual(
            len(ro_calls), 1,
            f"expected 1 RO probe, got {len(ro_calls)} calls: {ro_calls}",
        )
        self.assertEqual(
            len(rw_calls), 1,
            f"expected 1 RW apply, got {len(rw_calls)} calls: {rw_calls}",
        )

        # Rebind actually happened (delegating to the existing case-rebind path).
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
            "Bug N must not break the rebind path: lowercase-c binding "
            "should be rewritten to canonical capital-C.",
        )

    def test_self_heal_no_op_when_no_case_sibling(self):
        # Pre-seed launcher.db with capital-C binding, but Weaviate is
        # empty — true missing-class state. Leave the row alone (the
        # orphan-prune sync will handle lazy recreation).
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(classes=[])
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding row unchanged.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
            "missing-class binding must not be rewritten",
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)

    def test_self_heal_handles_launcher_db_missing(self):
        # launcher.db doesn't exist (fresh first-install, launcher never
        # started). Helper soft-fails to a skip log, no crash, no
        # deferral entry beyond the skip itself.
        self.assertFalse(self._db_path.exists())

        # Weaviate up but irrelevant — the helper short-circuits before
        # reaching the schema call.
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        # The helper must never raise.
        install._self_heal_kg_bindings_on_update(report)

        # No deferral entry — this is a benign skip, not a problem.
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)
        self.assertNotIn("kg_binding_self_heal_db_error", ids)

    def test_self_heal_rebinds_multiple_rows(self):
        # Defensive: when multiple binding rows need rebinding, all of
        # them get fixed in one pass and the deferral entry mentions
        # each.
        _build_launcher_db(
            self._db_path,
            rows=[
                # Two case-mismatched rows for different projects.
                ("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph"),
                ("p2", "shared", "vibecodedorchestrator_knowledgegraph"),
                # Plus one exact match (must not be touched).
                ("p3", "primary", "Acme_KnowledgeGraph"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=[
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Acme_KnowledgeGraph",
            ]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = dict(
            ((pid, role), coll)
            for (pid, role, coll) in _read_bindings(self._db_path)
        )
        self.assertEqual(
            bindings[("p1", "shared")],
            "VibeCodedOrchestrator_KnowledgeGraph",
        )
        self.assertEqual(
            bindings[("p2", "shared")],
            "VibeCodedOrchestrator_KnowledgeGraph",
        )
        self.assertEqual(
            bindings[("p3", "primary")],
            "Acme_KnowledgeGraph",
        )

        healed = next(e for e in report.entries
                      if e.condition_id == "kg_binding_self_healed")
        # Title summarises the count.
        self.assertIn("2", healed.title)
        # Detected message mentions both rebound rows.
        self.assertIn("p1", healed.detected)
        self.assertIn("p2", healed.detected)
        # And does NOT mention the exact-match row.
        self.assertNotIn("p3", healed.detected)

    def test_self_heal_skips_when_weaviate_unreachable(self):
        # launcher.db exists with a case-mismatch row, but Weaviate is
        # unreachable. Helper must not crash — it skips with a log
        # event but does NOT touch launcher.db (we can't build the
        # case-insensitive map without the schema).
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph")],
        )
        # Point WEAVIATE_URL at a port no one is listening on.
        # Pick a random ephemeral port and immediately close the socket
        # so the connect attempt definitely fails (no race).
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        self._set_weaviate_url(dead_port)

        report = DeferralReport()
        # Must not raise.
        install._self_heal_kg_bindings_on_update(report)

        # Binding row unchanged because we couldn't read the schema.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph")],
        )
        # No heal-completed entry (we didn't heal anything).
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)

    # ── v0.2.40 W40-A regression guards ───────────────────────────────
    # These guard the existing case-insensitive path against the
    # second-pass cross-prefix logic added in v0.2.40. Even when the
    # second pass IS available, a row that has a case-different sibling
    # in Weaviate must still go through pass 1 (case-rebind) — the
    # config_json must NOT acquire the `v0.2.40-prefix-adopt` sentinel,
    # because no prefix change happened.

    def test_v0240_case_rebind_does_not_set_prefix_adopt_sentinel(self):
        """A case-only rebind must NOT mark config_json with the v0.2.40
        sentinel — that sentinel is reserved for cross-prefix adoption,
        which signals to env-backfill that the prefix CHANGED. A pure
        case-flip already had `manual_override` semantics (or none) and
        should preserve them.
        """
        # Seed with a config_json carrying a prior sentinel.
        db_path = self._db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(_PROJECT_KG_BINDINGS_DDL)
            now = int(time.time() * 1000)
            conn.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name, embedding_model, "
                "embedding_dim, kg_dir_path, weaviate_url, config_json, "
                "updated_at) VALUES "
                "(?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    "p1", "shared",
                    "VibecodedOrchestrator_KnowledgeGraph",  # lowercase c
                    json.dumps({"manual_override": "v0.2.28-recovery"}),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        # Weaviate holds the canonical capital-C sibling.
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Pass-1 rebound the row.
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT collection_name, config_json "
                "FROM project_kg_bindings WHERE project_id = ? AND role = ?",
                ("p1", "shared"),
            )
            coll, cfg_str = cur.fetchone()
        finally:
            conn.close()
        self.assertEqual(coll, "VibeCodedOrchestrator_KnowledgeGraph")
        cfg = json.loads(cfg_str)
        # The original sentinel is preserved — pass-1 doesn't touch
        # config_json at all (only collection_name + updated_at).
        self.assertEqual(
            cfg.get("manual_override"), "v0.2.28-recovery",
            "case-only rebind must NOT change manual_override sentinel",
        )
        self.assertNotEqual(
            cfg.get("manual_override"), "v0.2.40-prefix-adopt",
            "v0.2.40 sentinel must NOT be applied on a pure case-rebind",
        )

    def test_v0240_pass2_does_not_re_adopt_a_just_case_rebound_row(self):
        """If pass-1 just rebound a row from lowercase-c to capital-C,
        pass-2 must see the row's new value already in
        ``existing_classes`` and short-circuit. No double-modification.
        """
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared",
                   "VibecodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        bindings = _read_bindings(self._db_path)
        # Pass-1 result. Pass-2 must NOT have changed anything further.
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )
        # Only ONE deferral entry (the case-rebind summary). No
        # multi-candidate deferral, no second adoption recorded.
        ids = [e.condition_id for e in report.entries]
        self.assertEqual(
            ids.count("kg_binding_self_healed"), 1,
            "case-rebind must produce exactly one summary entry",
        )
        self.assertNotIn("multi_candidate_prefix_adopt", ids)


# v0.2.49 access-matrix Step A.5: schema mirrors migration 029.
# created_at / updated_at INTEGER NOT NULL DEFAULT 0 (legacy rows
# backfill to 0; v0.2.49+ INSERTs bind both).
_KG_COLLECTION_ACCESS_DDL = """
CREATE TABLE kg_collection_access (
    project_id      TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    access_level    TEXT NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, collection_name)
)
"""


def _build_launcher_db_with_access(
    db_path: Path,
    binding_rows: list[tuple[str, str, str]],
    access_rows: list[tuple[str, str, str]],
) -> None:
    """Create launcher.db with both project_kg_bindings AND
    kg_collection_access tables seeded.

    binding_rows: [(project_id, role, collection_name)]
    access_rows:  [(project_id, collection_name, access_level)]
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_PROJECT_KG_BINDINGS_DDL)
        conn.execute(_KG_COLLECTION_ACCESS_DDL)
        now = int(time.time() * 1000)
        for project_id, role, collection_name in binding_rows:
            conn.execute(
                "INSERT INTO project_kg_bindings "
                "(project_id, role, collection_name, embedding_model, "
                "embedding_dim, kg_dir_path, weaviate_url, config_json, "
                "updated_at) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, '{}', ?)",
                (project_id, role, collection_name, now),
            )
        for project_id, collection_name, access_level in access_rows:
            conn.execute(
                "INSERT INTO kg_collection_access "
                "(project_id, collection_name, access_level) "
                "VALUES (?, ?, ?)",
                (project_id, collection_name, access_level),
            )
        conn.commit()
    finally:
        conn.close()


def _read_access(db_path: Path) -> list[tuple[str, str, str]]:
    """Read (project_id, collection_name, access_level) triples."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT project_id, collection_name, access_level "
            "FROM kg_collection_access ORDER BY project_id, collection_name"
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def _read_access_with_audit(
    db_path: Path,
) -> list[tuple[str, str, str, int, int]]:
    """v0.2.49 access-matrix Step A.5: read full row including audit
    columns. Used by tests that pin the seed-path invariant
    (`created_at == updated_at` on first INSERT)."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT project_id, collection_name, access_level, "
            "       created_at, updated_at "
            "FROM kg_collection_access ORDER BY project_id, collection_name"
        )
        return list(cur.fetchall())
    finally:
        conn.close()


class SelfHealAccessMatrixTests(unittest.TestCase):
    """v0.2.23 review-B HIGH-1 (2026-05-21) — pin the contract that the
    self-heal ALSO rebinds case-mismatched rows in `kg_collection_access`
    (sibling table to `project_kg_bindings`). Without these tests, a
    future regression could revert HIGH-1 silently — the binding side
    keeps healing, the access matrix drifts, and the launcher's Identity
    tab shows ghost rows.
    """

    def setUp(self):
        self._tmp = (
            Path(__file__).resolve().parent
            / f"_tmp_self_heal_acc_{os.getpid()}_{id(self)}"
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

    def test_access_matrix_rebinds_when_case_variant_exists(self):
        """A lowercase-c access row gets rebound to the on-disk casing."""
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[
                ("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph"),
            ],
            access_rows=[
                ("p1", "VibecodedOrchestrator_KnowledgeGraph", "read"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        access = _read_access(self._db_path)
        self.assertEqual(
            access,
            [("p1", "VibeCodedOrchestrator_KnowledgeGraph", "read")],
            "expected access row to be rebound to the live class casing",
        )

        # Deferral mentions BOTH binding and access rebinds.
        healed = next(e for e in report.entries
                      if e.condition_id == "kg_binding_self_healed")
        self.assertIn("kg_collection_access", healed.detected)
        self.assertIn("access row", healed.title)

    def test_access_matrix_no_op_when_canonical_match(self):
        """Capital-C access row + capital-C class — no rebind needed."""
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[
                ("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph"),
            ],
            access_rows=[
                ("p1", "VibeCodedOrchestrator_KnowledgeGraph", "write"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        access = _read_access(self._db_path)
        self.assertEqual(
            access,
            [("p1", "VibeCodedOrchestrator_KnowledgeGraph", "write")],
        )
        ids = [e.condition_id for e in report.entries]
        self.assertNotIn("kg_binding_self_healed", ids)

    def test_access_matrix_collision_keeps_higher_privilege(self):
        """If BOTH a lowercase-c row (`write`) AND a capital-C row (`read`)
        exist for the same project, the higher-privilege row wins at the
        canonical casing. The lower-privilege duplicate is deleted.
        """
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[
                ("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph"),
            ],
            access_rows=[
                ("p1", "VibecodedOrchestrator_KnowledgeGraph", "write"),
                ("p1", "VibeCodedOrchestrator_KnowledgeGraph", "read"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        access = _read_access(self._db_path)
        # Lowercase-c was `write` (rank 2), capital-C was `read` (rank 1).
        # Lowercase-c wins → capital-C deleted, lowercase-c rebound to
        # capital-C with its `write` privilege preserved.
        self.assertEqual(
            access,
            [("p1", "VibeCodedOrchestrator_KnowledgeGraph", "write")],
        )

    def test_access_matrix_collision_keeps_canonical_when_equal_privilege(self):
        """If BOTH rows have equal privilege, the canonical row wins
        (the lowercase-c duplicate is dropped)."""
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[
                ("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph"),
            ],
            access_rows=[
                ("p1", "VibecodedOrchestrator_KnowledgeGraph", "read"),
                ("p1", "VibeCodedOrchestrator_KnowledgeGraph", "read"),
            ],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        access = _read_access(self._db_path)
        self.assertEqual(
            access,
            [("p1", "VibeCodedOrchestrator_KnowledgeGraph", "read")],
            "equal privilege: canonical row kept, lowercase-c dropped",
        )

    def test_access_matrix_parity_insert_matches_canonical_schema(self):
        """v0.2.49 Bug O regression: when the parity self-heal backfills
        missing kg_collection_access rows (for project_kg_bindings rows
        without a matching access entry), the INSERT must match the
        actual schema declared by `migrations/001_initial.sql:63-69`
        — exactly 3 columns: (project_id, collection_name, access_level).

        Pre-Bug-O the INSERT referenced 5 columns (adding `granted_at`
        and `updated_at` that were planned but never landed in any
        migration). Discovered 2026-06-07 via Bug N's empirical post-update validation
        validation: the RW pass would activate on any host with case-
        rebind OR cross-prefix-adopt needs (the common case post-update),
        the parity loop would attempt the INSERT, and SQLite would raise
        `OperationalError: table kg_collection_access has no column named
        granted_at` — every user hit it on every install.py --update.

        Pin: build launcher.db with (a) a case-mismatched binding row
        (triggers RW pass entry from the RO probe) AND (b) NO matching
        kg_collection_access row (triggers the parity-insert). Post-fix:
        the INSERT succeeds, the new row is observable, no exception.
        Pre-fix: this test would fail with
        `OperationalError: ... no column named granted_at`.
        """
        # Binding row needs a case-rebind → RW pass activates.
        # No matching access row → parity-insert fires.
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[
                ("p1", "primary", "vcodev_KnowledgeGraph"),
            ],
            access_rows=[],  # ← empty: triggers parity-insert
        )
        # Weaviate has the canonical-cased class → case-rebind needed.
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        # Pre-Bug-O this would raise OperationalError during the parity
        # INSERT. Post-fix it returns cleanly.
        install._self_heal_kg_bindings_on_update(report)

        # The case-rebind happened (RW pass activated).
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "primary", "VCODev_KnowledgeGraph")],
            "case-rebind path must still work post-Bug-O fix",
        )

        # Two parity INSERTs happened — schema-matching success:
        #   1. (p1, VCODev_KnowledgeGraph, write) for the primary binding
        #   2. (p1, VCODev_Development, write) for the auto-derived
        #      _Development sibling (install.py L14181 backfill)
        # The pre-Bug-O code would have raised OperationalError on the
        # FIRST INSERT and never reached the second.
        access = _read_access(self._db_path)
        self.assertEqual(
            access,
            [
                ("p1", "VCODev_Development", "write"),
                ("p1", "VCODev_KnowledgeGraph", "write"),
            ],
            "Bug O regression: parity-insert must succeed against the "
            "canonical 3-column kg_collection_access schema. Pre-fix the "
            "INSERT raised OperationalError because it referenced "
            "granted_at + updated_at columns that no migration defines.",
        )

    def test_parity_insert_sets_audit_timestamps_equal(self):
        """v0.2.49 access-matrix Step A.5 (seed-path invariant): the
        parity self-heal INSERTs are SEED writes (system-driven, not
        user-driven). Both audit timestamps MUST be set to the SAME
        value on first INSERT so the Rust-side
        `KgAccessRow::is_user_configured` predicate reads FALSE for
        the row.

        This is the load-bearing property that makes the future
        `is_user_configured(row) := row.updated_at != row.created_at`
        predicate work correctly for any v0.2.49+ row. Legacy rows
        (pre-migration-029) have `created_at == updated_at == 0` so
        they also read as not-user-configured; new rows must
        preserve the same equality.

        Regression sentinel: if a future install.py change accidentally
        binds `time.time()*1000` for `updated_at` and `0` for
        `created_at` (or anything that breaks equality), this test
        catches it. The Phase 7 force-upgrade migration depends on
        seed rows reading as NOT user-configured.
        """
        _build_launcher_db_with_access(
            self._db_path,
            binding_rows=[("p1", "primary", "vcodev_KnowledgeGraph")],
            access_rows=[],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VCODev_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        access_full = _read_access_with_audit(self._db_path)
        # Both parity-inserted rows must have created_at == updated_at
        # AND both must be non-zero (a freshly-seeded v0.2.49+ row).
        self.assertEqual(
            len(access_full), 2,
            f"expected 2 parity-inserted rows, got: {access_full}",
        )
        for row in access_full:
            (_proj_id, _coll, _level, created_at, updated_at) = row
            self.assertEqual(
                created_at, updated_at,
                f"seed-path invariant violated for row {row}: "
                f"created_at ({created_at}) != updated_at ({updated_at}). "
                f"Phase 7 force-upgrade migration depends on seed rows "
                f"reading as NOT user-configured.",
            )
            self.assertGreater(
                created_at, 0,
                f"v0.2.49+ seed-path INSERT must bind a non-zero "
                f"timestamp; got row {row}. Zero is the legacy-row "
                f"sentinel — install.py must not write it.",
            )

    def test_access_matrix_absent_table_does_not_block_binding_heal(self):
        """Older launcher.db schemas may not have `kg_collection_access`.
        The binding heal should still complete normally."""
        # Build launcher.db with bindings ONLY (no access table).
        _build_launcher_db(
            self._db_path,
            rows=[("p1", "shared", "VibecodedOrchestrator_KnowledgeGraph")],
        )
        self._server, self._port = _start_stub_weaviate(
            classes=["VibeCodedOrchestrator_KnowledgeGraph"]
        )
        self._set_weaviate_url(self._port)

        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)

        # Binding heal worked.
        bindings = _read_bindings(self._db_path)
        self.assertEqual(
            bindings,
            [("p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph")],
        )
        # Deferral emitted (binding rebind).
        ids = [e.condition_id for e in report.entries]
        self.assertIn("kg_binding_self_healed", ids)


# ─── Helper-level tests (v0.2.24 B3 refactor) ────────────────────────────


class RebindCollectionNamesHelperTests(unittest.TestCase):
    """v0.2.24 B3 (2026-05-22) — direct coverage of the extracted
    ``_rebind_collection_names_to_on_disk_casing`` helper using a
    synthetic table.

    These tests document the helper contract independently of
    ``_self_heal_kg_bindings_on_update`` so a future caller (third
    heal-target table) can rely on the same semantics.
    """

    def setUp(self):
        # In-memory SQLite is enough for the helper — it's
        # cursor-shape-agnostic. No filesystem, no Weaviate stub needed.
        self._conn = sqlite3.connect(":memory:")
        self._cur = self._conn.cursor()

    def tearDown(self):
        self._conn.close()

    def test_helper_rebinds_case_mismatched_rows_in_synthetic_table(self):
        """Direct rebind via ``do_rebind`` when no collisions exist."""
        self._cur.execute(
            "CREATE TABLE synth ("
            "  project_id      TEXT NOT NULL,"
            "  collection_name TEXT NOT NULL,"
            "  PRIMARY KEY (project_id, collection_name)"
            ")"
        )
        self._cur.executemany(
            "INSERT INTO synth (project_id, collection_name) VALUES (?, ?)",
            [
                ("p1", "ExampleClass"),  # exact match — skip
                ("p2", "otherclass"),    # case-mismatch — rebind
                ("p3", "missing"),       # not in Weaviate — skip
            ],
        )
        existing_classes = {"ExampleClass", "OtherClass"}
        existing_by_lower = {n.lower(): n for n in existing_classes}

        def _do_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
            cur.execute(
                "UPDATE synth SET collection_name = ? "
                "WHERE project_id = ? AND collection_name = ?",
                (new_name, project_id, old_name),
            )
            rebinds.append((project_id, old_name, new_name))

        result = install._rebind_collection_names_to_on_disk_casing(
            self._cur,
            table="synth",
            project_id_col="project_id",
            collection_name_col="collection_name",
            existing_classes=existing_classes,
            existing_by_lower=existing_by_lower,
            do_rebind=_do_rebind,
        )

        self.assertEqual(result, [("p2", "otherclass", "OtherClass")])
        self._cur.execute(
            "SELECT project_id, collection_name FROM synth "
            "ORDER BY project_id"
        )
        self.assertEqual(
            list(self._cur.fetchall()),
            [
                ("p1", "ExampleClass"),
                ("p2", "OtherClass"),
                ("p3", "missing"),
            ],
        )

    def test_helper_invokes_conflict_resolver_on_collision(self):
        """When two rows map to the same canonical name after the
        case-rebind, ``resolve_conflict`` is invoked instead of
        ``do_rebind``."""
        self._cur.execute(
            "CREATE TABLE synth ("
            "  project_id      TEXT NOT NULL,"
            "  collection_name TEXT NOT NULL,"
            "  weight          INTEGER NOT NULL,"
            "  PRIMARY KEY (project_id, collection_name)"
            ")"
        )
        # Both rows for p1 will map to canonical "Foo" via lowercase
        # match. The lower-case row has higher weight (2 > 1) so the
        # resolver should keep it.
        self._cur.executemany(
            "INSERT INTO synth VALUES (?, ?, ?)",
            [
                ("p1", "foo", 2),  # case-mismatch, weight 2
                ("p1", "Foo", 1),  # canonical, weight 1 (collision)
            ],
        )
        existing_classes = {"Foo"}
        existing_by_lower = {"foo": "Foo"}

        do_rebind_calls: list = []

        def _do_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
            do_rebind_calls.append((project_id, old_name, new_name))
            rebinds.append(("direct", project_id, old_name, new_name))

        def _resolve_conflict(
            cur, *, project_id, old_name, new_name,
            current_row, conflict_row, rebinds,
        ):
            # current_row = the lowercase-mismatched row
            # conflict_row = the row already at canonical casing
            current_weight = current_row[2]
            conflict_weight = conflict_row[2]
            if current_weight > conflict_weight:
                # Keep lowercase row's data; drop canonical, then rebind.
                cur.execute(
                    "DELETE FROM synth WHERE project_id = ? "
                    "AND collection_name = ?",
                    (project_id, new_name),
                )
                cur.execute(
                    "UPDATE synth SET collection_name = ? "
                    "WHERE project_id = ? AND collection_name = ?",
                    (new_name, project_id, old_name),
                )
                rebinds.append(("resolved-keep-current",
                                project_id, old_name, new_name))
            else:
                cur.execute(
                    "DELETE FROM synth WHERE project_id = ? "
                    "AND collection_name = ?",
                    (project_id, old_name),
                )
                rebinds.append(("resolved-keep-conflict",
                                project_id, old_name, new_name))

        result = install._rebind_collection_names_to_on_disk_casing(
            self._cur,
            table="synth",
            project_id_col="project_id",
            collection_name_col="collection_name",
            existing_classes=existing_classes,
            existing_by_lower=existing_by_lower,
            extra_select_cols=("weight",),
            do_rebind=_do_rebind,
            resolve_conflict=_resolve_conflict,
        )

        # do_rebind must NOT have been called — the conflict was
        # detected and routed to resolve_conflict.
        self.assertEqual(do_rebind_calls, [])
        # The resolver kept the higher-weight row (the lowercase one).
        self.assertEqual(
            result,
            [("resolved-keep-current", "p1", "foo", "Foo")],
        )
        self._cur.execute(
            "SELECT project_id, collection_name, weight FROM synth"
        )
        self.assertEqual(
            list(self._cur.fetchall()),
            [("p1", "Foo", 2)],
        )

    def test_helper_skips_exact_match_and_genuine_missing(self):
        """No rebind when the row already matches OR when no
        case-insensitive sibling exists in ``existing_classes``."""
        # Note: collection_name NOT NOT-NULL here so we can test the
        # helper's defensive "skip empty/NULL row" path. Production
        # tables (project_kg_bindings, kg_collection_access) declare
        # NOT NULL — the helper's guard is purely defensive.
        self._cur.execute(
            "CREATE TABLE synth ("
            "  project_id      TEXT NOT NULL,"
            "  collection_name TEXT"
            ")"
        )
        self._cur.executemany(
            "INSERT INTO synth VALUES (?, ?)",
            [
                ("p1", "Canonical"),     # exact match
                ("p2", "GhostClass"),    # genuinely missing
                ("p3", ""),              # empty — skip
                ("p4", None),            # NULL — skip
            ],
        )
        existing_classes = {"Canonical", "OtherClass"}
        existing_by_lower = {n.lower(): n for n in existing_classes}

        calls: list = []

        def _do_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
            calls.append((project_id, old_name, new_name))
            rebinds.append((project_id, old_name, new_name))

        result = install._rebind_collection_names_to_on_disk_casing(
            self._cur,
            table="synth",
            project_id_col="project_id",
            collection_name_col="collection_name",
            existing_classes=existing_classes,
            existing_by_lower=existing_by_lower,
            do_rebind=_do_rebind,
        )

        self.assertEqual(result, [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
