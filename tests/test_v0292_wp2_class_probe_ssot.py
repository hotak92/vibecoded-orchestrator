# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-2 (W18) — ONE tri-state probe for "does this exist?".

The rule these tests exist to enforce, stated once:

    A check that cannot distinguish "I could not determine this" from
    "this is fine" is not a check.

Three shipped helpers answered "which Weaviate classes exist?" by returning
``[]`` when the server could not be reached, and one sqlite resolver answered
"what prefix is this project bound to?" with ``None`` for both "no binding" and
"could not read the DB". So an unreachable backend and an empty one produced
byte-identical answers, and every consumer downstream inherited the lie. One of
those consumers uses the answer to decide which on-disk directories a user may
delete.

What is pinned here, in the order the file is written:

  1. **The mechanism.** ``ProbeResult`` refuses the three accidental
     collapses (``if p``, ``for x in p``, ``len(p)``). A caller that has not
     decided what "unknown" means for its decision fails loudly at the first
     use rather than silently reading unknown as absent. Tested for BOTH the
     unknown and the known states — the guard must not be a special case of
     the failure path.
  2. **The probe semantics**, per input shape: transport failure, non-200,
     unparsable body, ``classes``-less payload, genuinely empty schema,
     populated schema.
  3. **Every migrated call site**, act AND leave-alone: what it does when the
     read succeeds, and what it does when the read could not happen.
  4. **The straggler proof** — the fourth copy is gone and no class-listing
     function in either module returns a bare ``[]`` from an except arm.
  5. **The vocabulary pin** — ``PROBE_UNKNOWN`` is the same string as the
     doctor's ``STATUS_UNKNOWN``, so the two Python tri-states cannot drift on
     the word that carries the meaning.
  6. **Tri-OS shape** — every decision in this package is OS-invariant, proven
     rather than asserted.

Hermetic: no live Weaviate, no live launcher.db. The Weaviate side is driven
through the probe's injectable ``request`` seam (or a patched module-level
``http_request``); the sqlite side uses a real on-disk DB built from the
SHIPPED migration SQL via ``tests.common.launcher_db_fixture`` — the ONE
applier, rather than this file's own copy of the glob-and-executescript loop.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_codegraph_binding,
    add_project,
    create_empty_launcher_db,
)
from vco_lib import config_projection as cp  # noqa: E402
from vco_lib import weaviate_helpers as wh  # noqa: E402
from vco_lib import weaviate_schema as ws  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════


def _schema_response(*class_names: str) -> tuple[int, bytes]:
    import json

    body = {"classes": [{"class": n} for n in class_names]}
    return (200, json.dumps(body).encode("utf-8"))


class _Responder:
    """A ``http_request``-shaped callable returning (or raising) ``result``.

    Records every call so a test can assert the URL shape the probe built.
    """

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, method, url, *, body=None, timeout=30.0):
        self.calls.append((method, url, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _responder(result) -> _Responder:
    return _Responder(result)


class _ExplodingCursor:
    def __init__(self, exc):
        self._exc = exc

    def execute(self, *_a, **_kw):
        raise self._exc

    def fetchone(self):  # pragma: no cover — never reached
        raise AssertionError("execute() should have raised first")


class _ExplodingConn:
    """Minimal ``sqlite3.Connection`` stand-in whose queries always fail.

    Models the corrupt/locked database — the case that is genuinely UNKNOWN
    and that a "no such table" check must NOT absorb.
    """

    def __init__(self, exc):
        self._exc = exc

    def cursor(self):
        return _ExplodingCursor(self._exc)


def _broken_conn(exc: sqlite3.Error) -> sqlite3.Connection:
    """``_ExplodingConn`` typed as the Connection the resolvers declare."""
    return cast(sqlite3.Connection, _ExplodingConn(exc))


def _build_launcher_db(
    db_path: Path,
    *,
    codegraph_prefix: str | None = "BoundPrefix",
    break_prefix_column: bool = False,
) -> Path:
    """A real launcher.db built by applying the SHIPPED migration SQL.

    Deliberately NOT a hand-rolled ``CREATE TABLE`` copy — and no longer a
    private copy of the applier either: ``create_empty_launcher_db`` is the
    one place that walks the migration files, so this fixture cannot drift
    from the schema the launcher ships OR from the other tests' idea of it.

    ``break_prefix_column`` renames ``collection_prefix`` away, which makes the
    resolver's SELECT fail with ``no such column`` — a real sqlite error that
    is NOT a missing table, i.e. the UNKNOWN case.
    """
    create_empty_launcher_db(db_path)
    add_project(
        db_path, project_id="p1", name="My Proj",
        folder_path="/tmp/wp2-p1", host="base", slug="my-proj",
        created_at=1, updated_at=1,
    )
    if codegraph_prefix is not None:
        # NOT routed through add_project's `codegraph_prefix=`: that helper
        # skips a falsy prefix, and the blank-prefix cases below need the
        # row to EXIST while naming "".
        add_codegraph_binding(db_path, "p1", codegraph_prefix, updated_at=1)
    if break_prefix_column:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "ALTER TABLE project_codegraph_bindings "
                "RENAME COLUMN collection_prefix TO legacy_prefix"
            )
            conn.commit()
        finally:
            conn.close()
    return db_path


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE MECHANISM — the collapse guards
# ═══════════════════════════════════════════════════════════════════════════


class TriStateCannotBeCollapsedTests(unittest.TestCase):
    """A caller that ignores the unknown state must FAIL, not guess.

    Would have caught: nothing today, because nothing today has a type at all.
    Guards the future — these are the four expressions that turned every one of
    the twelve "formally-correct answer to the wrong question" sites into a
    silent wrong answer, and each is now a TypeError at the first use.
    """

    def setUp(self):
        self.unknown = wh.ProbeResult.unknown("weaviate down", what="q")
        self.absent = wh.ProbeResult.absent(what="q", value=[])
        self.present = wh.ProbeResult.present(["A"], what="q")

    def test_truthiness_is_fatal_for_every_state(self):
        for probe in (self.unknown, self.absent, self.present):
            with self.subTest(state=probe.state):
                with self.assertRaises(TypeError):
                    bool(probe)
                with self.assertRaises(TypeError):
                    if probe:  # noqa: SIM103 — the point is that it raises
                        pass
                with self.assertRaises(TypeError):
                    if not probe:
                        pass

    def test_iteration_is_fatal_for_every_state(self):
        for probe in (self.unknown, self.absent, self.present):
            with self.subTest(state=probe.state):
                with self.assertRaises(TypeError):
                    list(probe)
                with self.assertRaises(TypeError):
                    for _ in probe:  # noqa: B007
                        pass

    def test_len_is_fatal_for_every_state(self):
        for probe in (self.unknown, self.absent, self.present):
            with self.subTest(state=probe.state):
                with self.assertRaises(TypeError):
                    len(probe)

    def test_or_default_idiom_is_fatal(self):
        # `listing or []` is the single most common accidental collapse.
        with self.assertRaises(TypeError):
            _ = self.unknown or []

    def test_guard_messages_name_the_alternative(self):
        # A guard that only says "no" teaches nothing; each must name the
        # accessor to use instead, or the next editor deletes the guard.
        for expr in (lambda: bool(self.unknown),
                     lambda: list(self.unknown),
                     lambda: len(self.unknown)):
            with self.assertRaises(TypeError) as ctx:
                expr()
            msg = str(ctx.exception)
            self.assertTrue(
                ".require()" in msg or ".is_present()" in msg,
                f"guard message must name the accessor to use: {msg!r}",
            )

    def test_state_vocabulary_is_closed(self):
        with self.assertRaises(ValueError):
            wh.ProbeResult("healthy", "q")

    def test_require_raises_only_for_unknown(self):
        with self.assertRaises(wh.ProbeUnavailable):
            self.unknown.require()
        self.assertEqual(self.absent.require(), [])
        self.assertEqual(self.present.require(), ["A"])

    def test_unavailable_carries_question_and_reason(self):
        with self.assertRaises(wh.ProbeUnavailable) as ctx:
            self.unknown.require()
        self.assertEqual(ctx.exception.what, "q")
        self.assertEqual(ctx.exception.reason, "weaviate down")
        self.assertIn("weaviate down", str(ctx.exception))

    def test_optional_view_distinguishes_unknown_from_empty(self):
        # THE convention: None = could not check, [] = read and empty.
        self.assertIsNone(self.unknown.or_none())
        self.assertEqual(self.absent.or_none(), [])
        self.assertEqual(self.present.or_none(), ["A"])

    def test_predicates_never_agree_across_absent_and_unknown(self):
        self.assertTrue(self.absent.is_absent())
        self.assertFalse(self.unknown.is_absent())
        self.assertTrue(self.unknown.is_unknown())
        self.assertFalse(self.absent.is_unknown())
        self.assertTrue(self.absent.is_known())
        self.assertFalse(self.unknown.is_known())


# ═══════════════════════════════════════════════════════════════════════════
# 2. THE PROBE — one GET /v1/schema, six input shapes
# ═══════════════════════════════════════════════════════════════════════════


class ProbeClassListingTests(unittest.TestCase):
    """THE test of this package.

    ``test_transport_failure_is_unknown_not_absent`` is the one that matters:
    it is the exact shape that shipped for years as ``except Exception: return
    []`` and that let an unreachable Weaviate be read as an empty one.
    """

    def test_transport_failure_is_unknown_not_absent(self):
        probe = wh.probe_class_listing(
            "http://localhost:8081",
            request=_responder(ConnectionRefusedError("connection refused")),
        )
        self.assertTrue(probe.is_unknown())
        self.assertFalse(probe.is_absent())
        self.assertIsNone(probe.or_none())
        with self.assertRaises(wh.ProbeUnavailable):
            probe.require()
        self.assertIn("ConnectionRefusedError", probe.reason)

    def test_non_200_is_unknown(self):
        probe = wh.probe_class_listing(
            "http://localhost:8081", request=_responder((503, b"unavailable")),
        )
        self.assertTrue(probe.is_unknown())
        self.assertIn("503", probe.reason)

    def test_unparsable_body_is_unknown(self):
        probe = wh.probe_class_listing(
            "http://localhost:8081", request=_responder((200, b"<html>nope")),
        )
        self.assertTrue(probe.is_unknown())
        self.assertIn("unparsable", probe.reason)

    def test_payload_without_classes_array_is_unknown(self):
        # A live Weaviate always answers {"classes": [...]}; a payload without
        # one means we did not understand the response, so refusing beats
        # claiming the server is empty.
        for body in (b"{}", b'{"classes": null}', b'{"classes": {}}', b"[]"):
            with self.subTest(body=body):
                probe = wh.probe_class_listing(
                    "http://x", request=_responder((200, body)),
                )
                self.assertTrue(probe.is_unknown())
                self.assertFalse(probe.is_absent())

    def test_genuinely_empty_schema_is_absent_not_unknown(self):
        # The leave-alone half: an empty server is a real, usable answer.
        probe = wh.probe_class_listing(
            "http://x", request=_responder(_schema_response()),
        )
        self.assertTrue(probe.is_absent())
        self.assertFalse(probe.is_unknown())
        self.assertEqual(probe.require(), [])
        self.assertEqual(probe.or_none(), [])

    def test_populated_schema_is_present_with_names(self):
        probe = wh.probe_class_listing(
            "http://x", request=_responder(_schema_response("B_Code", "A_KG")),
        )
        self.assertTrue(probe.is_present())
        self.assertEqual(probe.require(), ["B_Code", "A_KG"])

    def test_malformed_class_entries_are_dropped(self):
        import json

        payload = json.dumps({
            "classes": [
                {"class": "Good"}, {"class": ""}, {"class": None},
                {"noclass": 1}, "not-a-dict",
            ]
        }).encode("utf-8")
        probe = wh.probe_class_listing(
            "http://x", request=_responder((200, payload)),
        )
        self.assertEqual(probe.require(), ["Good"])

    def test_default_request_is_resolved_at_call_time(self):
        # So mock.patch on the module attribute reaches the probe — the seam
        # weaviate_schema's tests rely on.
        with mock.patch.object(
            wh, "http_request", return_value=_schema_response("X"),
        ):
            self.assertEqual(
                wh.probe_class_listing("http://x").require(), ["X"],
            )

    def test_never_raises_on_any_input(self):
        for result in (
            ConnectionRefusedError("x"), TimeoutError("x"), OSError("x"),
            ValueError("x"), (500, b""), (200, b"\xff\xfe"),
        ):
            with self.subTest(result=repr(result)):
                probe = wh.probe_class_listing(
                    "http://x", request=_responder(result),
                )
                self.assertTrue(probe.is_unknown())


class ProbeUrlShapeTests(unittest.TestCase):
    """Tri-OS: the probe builds ONE URL shape on every platform.

    The package has no OS branches at all (see
    :class:`NoOsDependentDecisionsTests`); the only place an OS difference
    could leak in is path joining, so the URL is asserted invariant under the
    three platform identities rather than merely assumed to be.
    """

    def _url_for(self, base):
        req = _responder(_schema_response())
        wh.probe_class_listing(base, request=req)
        return req.calls[0][1]

    def test_url_is_forward_slashed_on_every_platform(self):
        for platform, osname in (
            ("linux", "posix"), ("darwin", "posix"), ("win32", "nt"),
        ):
            with self.subTest(platform=platform):
                with mock.patch.object(sys, "platform", platform), \
                        mock.patch("os.name", osname):
                    url = self._url_for("http://localhost:8081")
                self.assertEqual(url, "http://localhost:8081/v1/schema")
                self.assertNotIn("\\", url)

    def test_trailing_slash_is_normalised(self):
        self.assertEqual(
            self._url_for("http://localhost:8081/"),
            "http://localhost:8081/v1/schema",
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3a. MIGRATED CALL SITES — vco_lib/weaviate_schema.py
# ═══════════════════════════════════════════════════════════════════════════


class WeaviateSchemaListingTests(unittest.TestCase):
    """``_list_all_classes`` + its two callers, act AND leave-alone.

    Would have caught (all three RED against the pre-fix source): a Weaviate
    that could not be reached made ``enumerate_kg_collections`` /
    ``enumerate_code_collections`` return ``[]``, so
    ``migrate_collections_to_v0218_schema`` produced an empty, successful-
    looking report and ``format_reports_table`` printed "(no collections
    matched)" for a server it never talked to.
    """

    def _down(self):
        return mock.patch.object(
            wh, "http_request", side_effect=ConnectionRefusedError("down"),
        )

    def _serving(self, *names):
        return mock.patch.object(
            wh, "http_request", return_value=_schema_response(*names),
        )

    # ── act: the read succeeds ──────────────────────────────────────────

    def test_list_all_classes_returns_sorted_names(self):
        with self._serving("Z_KnowledgeGraph", "A_KnowledgeGraph"):
            self.assertEqual(
                ws._list_all_classes(weaviate_url="http://x"),
                ["A_KnowledgeGraph", "Z_KnowledgeGraph"],
            )

    def test_empty_server_still_yields_empty_lists(self):
        # Leave-alone: "nothing to migrate" is a legitimate answer and must
        # survive the change untouched.
        with self._serving():
            self.assertEqual(ws._list_all_classes(weaviate_url="http://x"), [])
            self.assertEqual(
                ws.enumerate_kg_collections(weaviate_url="http://x"), [],
            )
            self.assertEqual(
                ws.enumerate_code_collections(weaviate_url="http://x"), [],
            )

    def test_enumerators_filter_normally_when_the_read_succeeds(self):
        with self._serving(
            "Proj_KnowledgeGraph", "Proj_CodeFunction", "Random_Other",
        ):
            self.assertEqual(
                ws.enumerate_kg_collections(weaviate_url="http://x"),
                ["Proj_KnowledgeGraph"],
            )
            self.assertEqual(
                ws.enumerate_code_collections(weaviate_url="http://x"),
                ["Proj_CodeFunction"],
            )

    # ── leave-alone: the read could not happen ──────────────────────────

    def test_list_all_classes_raises_instead_of_returning_empty(self):
        with self._down():
            with self.assertRaises(wh.ProbeUnavailable):
                ws._list_all_classes(weaviate_url="http://x")

    def test_enumerate_kg_all_projects_refuses_when_unreachable(self):
        with self._down():
            with self.assertRaises(wh.ProbeUnavailable):
                ws.enumerate_kg_collections(weaviate_url="http://x")

    def test_enumerate_kg_per_project_refuses_when_unreachable(self):
        # The worst pre-fix reading: "none of THIS project's collections
        # exist" for a project whose collections were all there.
        with self._down():
            with self.assertRaises(wh.ProbeUnavailable):
                ws.enumerate_kg_collections(
                    project_name="MyProject", weaviate_url="http://x",
                )

    def test_enumerate_code_refuses_when_unreachable(self):
        with self._down():
            for kwargs in ({}, {"project_name": "MyProject"}):
                with self.subTest(**kwargs):
                    with self.assertRaises(wh.ProbeUnavailable):
                        ws.enumerate_code_collections(
                            weaviate_url="http://x", **kwargs,
                        )

    def test_migration_touches_nothing_when_the_schema_is_unknown(self):
        """The destructive-adjacent branch: refuse, and prove zero writes.

        ``migrate_collection_to_target`` can REBUILD a collection (drop +
        recreate through staging). Pre-fix, an unreachable server produced an
        empty collection list and a clean report; now it raises before the
        first call, and the spy proves no collection was visited.
        """
        with self._down(), mock.patch.object(
            ws, "migrate_collection_to_target",
        ) as spy:
            with self.assertRaises(wh.ProbeUnavailable):
                ws.migrate_collections_to_v0218_schema(
                    weaviate_url="http://x",
                )
        spy.assert_not_called()

    def test_migration_runs_normally_when_the_schema_is_readable(self):
        with self._serving("Proj_KnowledgeGraph"), mock.patch.object(
            ws, "migrate_collection_to_target",
            return_value=ws.MigrationReport(collection="Proj_KnowledgeGraph"),
        ) as spy:
            reports = ws.migrate_collections_to_v0218_schema(
                weaviate_url="http://x",
            )
        self.assertEqual(len(reports), 1)
        spy.assert_called_once()

    def test_exception_is_the_one_home_not_a_local_copy(self):
        self.assertIs(ws.ProbeUnavailable, wh.ProbeUnavailable)


# ═══════════════════════════════════════════════════════════════════════════
# 3b. MIGRATED CALL SITE — vco_lib/config_projection.py
# ═══════════════════════════════════════════════════════════════════════════


class CodegraphBindingPrefixProbeTests(unittest.TestCase):
    """The sqlite half of the same conflation.

    ``None`` used to mean "no binding row" AND "the launcher DB blew up", and
    both fell through to a NAME-DERIVED ``CODE_GRAPH_PROJECT`` that gets
    PERSISTED into ``.claude/settings.json`` + ``.claude/env``. A wrong
    code-graph prefix makes every CLI and hook query a class that does not
    exist and return nothing, silently.
    """

    def _conn(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ── the read succeeds (present / absent) ────────────────────────────
    # This class tests the SENSOR, so "act vs leave-alone" belongs to its
    # consumer (ProjectEnvProjectionRefusalTests below), not here.

    def test_bound_prefix_is_present(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db = _build_launcher_db(Path(td) / "launcher.db")
            conn = self._conn(db)
            try:
                probe = cp.probe_codegraph_binding_prefix(conn, "p1")
            finally:
                conn.close()
        self.assertTrue(probe.is_present())
        self.assertEqual(probe.require(), "BoundPrefix")

    def test_no_row_is_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db = _build_launcher_db(
                Path(td) / "launcher.db", codegraph_prefix=None,
            )
            conn = self._conn(db)
            try:
                probe = cp.probe_codegraph_binding_prefix(conn, "p1")
            finally:
                conn.close()
        self.assertTrue(probe.is_absent())
        self.assertIsNone(probe.require())

    def test_blank_prefix_is_absent(self):
        import tempfile

        for prefix in ("", "   ", "\t\n"):
            with self.subTest(prefix=repr(prefix)):
                with tempfile.TemporaryDirectory() as td:
                    db = _build_launcher_db(
                        Path(td) / "launcher.db", codegraph_prefix=prefix,
                    )
                    conn = self._conn(db)
                    try:
                        probe = cp.probe_codegraph_binding_prefix(conn, "p1")
                    finally:
                        conn.close()
                self.assertTrue(probe.is_absent())

    def test_missing_table_is_absent_not_unknown(self):
        # A pre-migration launcher.db has never had the table, so "no binding
        # names a prefix" is TRUE. This is the one sqlite error that is
        # genuine evidence, and it must keep its soft-fail.
        conn = _broken_conn(
            sqlite3.OperationalError(
                "no such table: project_codegraph_bindings"
            )
        )
        probe = cp.probe_codegraph_binding_prefix(conn, "p1")
        self.assertTrue(probe.is_absent())
        self.assertFalse(probe.is_unknown())

    # ── the read could not happen (unknown) ─────────────────────────────

    def test_corrupt_db_is_unknown_not_absent(self):
        conn = _broken_conn(
            sqlite3.DatabaseError("database disk image is malformed")
        )
        probe = cp.probe_codegraph_binding_prefix(conn, "p1")
        self.assertTrue(probe.is_unknown())
        self.assertFalse(probe.is_absent())
        self.assertIn("malformed", probe.reason)

    def test_locked_db_is_unknown(self):
        conn = _broken_conn(sqlite3.OperationalError("database is locked"))
        probe = cp.probe_codegraph_binding_prefix(conn, "p1")
        self.assertTrue(probe.is_unknown())

    def test_missing_column_is_unknown(self):
        # The table exists but not in the shape we can read: we cannot prove
        # absence, so we must not claim it.
        conn = _broken_conn(
            sqlite3.OperationalError("no such column: collection_prefix")
        )
        probe = cp.probe_codegraph_binding_prefix(conn, "p1")
        self.assertTrue(probe.is_unknown())

    def test_scalar_view_raises_only_for_unknown(self):
        self.assertIsNone(
            cp._fetch_codegraph_binding_prefix(
                _broken_conn(sqlite3.OperationalError("no such table: x")),
                "p1",
            )
        )
        with self.assertRaises(wh.ProbeUnavailable):
            cp._fetch_codegraph_binding_prefix(
                _broken_conn(sqlite3.DatabaseError("malformed")), "p1",
            )

    def test_missing_table_helper_has_one_home(self):
        self.assertTrue(
            cp._is_missing_table_error(
                sqlite3.OperationalError("no such table: diagram_access")
            )
        )
        self.assertFalse(
            cp._is_missing_table_error(
                sqlite3.DatabaseError("database disk image is malformed")
            )
        )


class ProjectEnvProjectionRefusalTests(unittest.TestCase):
    """The guard that consumes the probe: does it act, and does it refuse?

    ``project_env_from_db`` WRITES its answer to disk (via
    ``apply_project_env``). Persisting a guess is worse than persisting
    nothing, so the unknown state has to stop the projection.
    """

    def test_bound_prefix_is_projected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db = _build_launcher_db(Path(td) / "launcher.db")
            bundle = cp.project_env_from_db("p1", db_path=db)
        self.assertEqual(
            bundle["canonical_env"]["CODE_GRAPH_PROJECT"], "BoundPrefix",
        )

    def test_absent_binding_still_falls_back_to_the_derived_prefix(self):
        # LEAVE-ALONE. The name-derived placeholder is CORRECT before the
        # first analysis has bound anything; this half must not regress.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db = _build_launcher_db(
                Path(td) / "launcher.db", codegraph_prefix=None,
            )
            bundle = cp.project_env_from_db("p1", db_path=db)
        self.assertEqual(
            bundle["canonical_env"]["CODE_GRAPH_PROJECT"], "MyProj",
        )

    def test_unreadable_binding_refuses_instead_of_guessing(self):
        # ACT (the refusal). RED against the pre-fix source, which returned
        # CODE_GRAPH_PROJECT='MyProj' — a name-derived guess written over a
        # binding that may well have named something else.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db = _build_launcher_db(
                Path(td) / "launcher.db", break_prefix_column=True,
            )
            with self.assertRaises(cp.DbUnreachable) as ctx:
                cp.project_env_from_db("p1", db_path=db)
        msg = str(ctx.exception)
        self.assertIn("project_codegraph_bindings", msg)
        self.assertIn("Refusing", msg)

    def test_refusal_uses_the_modules_declared_failure_vocabulary(self):
        # `DbUnreachable` is what env_template (exit 3) and install.py already
        # catch — refusing through a NEW exception type would be an unhandled
        # crash for both.
        self.assertTrue(issubclass(cp.DbUnreachable, cp.ConfigProjectionError))


# ═══════════════════════════════════════════════════════════════════════════
# 4. STRAGGLER PROOF — no fourth copy, no bare [] on a failure arm
# ═══════════════════════════════════════════════════════════════════════════


_CLASS_LISTING_FUNCTIONS = frozenset({
    "list_classes",          # the deleted fourth copy
    "_list_all_classes",
    "enumerate_kg_collections",
    "enumerate_code_collections",
    "probe_class_listing",
})


def _except_arms_returning_empty_container(module_path: Path) -> list[str]:
    """Names of class-listing functions that return ``[]``/``{}``/``None``
    from inside an ``except`` handler."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in _CLASS_LISTING_FUNCTIONS:
            continue
        for handler in [
            h for h in ast.walk(node) if isinstance(h, ast.ExceptHandler)
        ]:
            for stmt in ast.walk(handler):
                if not isinstance(stmt, ast.Return) or stmt.value is None:
                    continue
                value = stmt.value
                empty_list = isinstance(value, ast.List) and not value.elts
                empty_dict = isinstance(value, ast.Dict) and not value.keys
                none_const = (
                    isinstance(value, ast.Constant) and value.value is None
                )
                if empty_list or empty_dict or none_const:
                    offenders.append(f"{module_path.name}::{node.name}")
    return offenders


class NoStragglerCopiesTests(unittest.TestCase):
    def test_the_fourth_copy_is_gone(self):
        # `weaviate_helpers.list_classes` was the fourth implementation of the
        # class-listing question and the only one with NO caller — a []-on-
        # failure helper kept for a user who never arrived. Removal counts as
        # concretizing a promise; a re-added copy fails here.
        self.assertFalse(
            hasattr(wh, "list_classes"),
            "weaviate_helpers.list_classes is back — route the caller through "
            "probe_class_listing instead of re-adding the []-on-failure copy",
        )

    def test_no_class_listing_function_swallows_a_failure_into_empty(self):
        for module in (wh, ws):
            source = inspect.getsourcefile(module)
            self.assertIsNotNone(source, f"no source file for {module!r}")
            path = Path(str(source))
            with self.subTest(module=path.name):
                self.assertEqual(_except_arms_returning_empty_container(path), [])

    def test_the_ast_guard_can_actually_fail(self):
        # A guard nobody has seen fail is a guard nobody can trust: run the
        # same AST check over the PRE-FIX shape and require a hit.
        import tempfile
        import textwrap

        pre_fix = textwrap.dedent(
            """
            def _list_all_classes(*, weaviate_url=None):
                try:
                    status, body = _http_request("GET", "/v1/schema")
                    if status != 200:
                        return []
                    return sorted(x for x in body)
                except Exception:
                    return []
            """
        )
        with tempfile.TemporaryDirectory() as td:
            probe_file = Path(td) / "weaviate_schema.py"
            probe_file.write_text(pre_fix, encoding="utf-8")
            self.assertEqual(
                _except_arms_returning_empty_container(probe_file),
                ["weaviate_schema.py::_list_all_classes"],
            )

    def test_only_one_module_owns_the_probe(self):
        self.assertIs(
            sys.modules[wh.probe_class_listing.__module__],
            wh,
            "probe_class_listing must stay in vco_lib.weaviate_helpers",
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. VOCABULARY PIN + 6. TRI-OS SHAPE
# ═══════════════════════════════════════════════════════════════════════════


class VocabularyPinTests(unittest.TestCase):
    def test_unknown_is_the_same_word_the_doctor_uses(self):
        # Python now has two tri-states in two domains (doctor: health;
        # this: existence). They share the ONE state that carries the meaning,
        # so a future consolidation has nothing to reconcile — and neither
        # side can rename it without this failing.
        from vco_lib import doctor

        self.assertEqual(wh.PROBE_UNKNOWN, doctor.STATUS_UNKNOWN)

    def test_states_are_exactly_three(self):
        self.assertEqual(
            wh.PROBE_STATES,
            (wh.PROBE_PRESENT, wh.PROBE_ABSENT, wh.PROBE_UNKNOWN),
        )


class NoOsDependentDecisionsTests(unittest.TestCase):
    """Tri-OS row for WP-2, stated as evidence rather than as a claim.

    This package decides nothing from the platform: it does one HTTP GET and
    one sqlite SELECT. Rather than assert that in a comment, assert it against
    the source — if a future edit introduces a platform branch here, this test
    makes it a deliberate act.
    """

    _OS_MARKERS = ("sys.platform", "os.name", "platform.system", "os.sep",
                   "ntpath", "posixpath")

    def _source_of(self, *objs) -> str:
        return "\n".join(inspect.getsource(o) for o in objs)

    def test_the_probe_has_no_platform_branch(self):
        src = self._source_of(
            wh.probe_class_listing, wh.ProbeResult,
            ws._list_all_classes, ws.enumerate_kg_collections,
            ws.enumerate_code_collections,
            cp.probe_codegraph_binding_prefix,
            cp._fetch_codegraph_binding_prefix,
            cp._is_missing_table_error,
        )
        for marker in self._OS_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, src)

    def test_behaviour_is_identical_under_all_three_platform_identities(self):
        results = {}
        for platform in ("linux", "darwin", "win32"):
            with mock.patch.object(sys, "platform", platform):
                unreachable = wh.probe_class_listing(
                    "http://x", request=_responder(OSError("down")),
                )
                empty = wh.probe_class_listing(
                    "http://x", request=_responder(_schema_response()),
                )
                populated = wh.probe_class_listing(
                    "http://x", request=_responder(_schema_response("A")),
                )
                results[platform] = (
                    unreachable.state, empty.state, populated.state,
                    empty.require(), populated.require(),
                )
        self.assertEqual(len(set(map(repr, results.values()))), 1, results)
        self.assertEqual(
            results["win32"][:3],
            (wh.PROBE_UNKNOWN, wh.PROBE_ABSENT, wh.PROBE_PRESENT),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
