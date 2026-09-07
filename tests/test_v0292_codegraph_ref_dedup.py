# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — ``vco_lib.codegraph_ref_dedup``: collapsing ALREADY-STORED
duplicate cross-reference beacons.

Every branch that gates a destructive action is tested in BOTH directions —
the act and the leave-alone — because this tool rewrites rows in a stranger's
live database and "did nothing" has to be as reliable as "did the right thing".

Two classes of test double are used, deliberately:

* a STATEFUL fake Weaviate (``_FakeServer``) that stores beacon lists and
  answers the same three REST shapes the real server does, so idempotence,
  partial reads, mid-run mutation and post-write verification are exercised
  end-to-end rather than asserted about;
* the REAL weaviate-client types where the production read path meets them —
  ``_CrossReference`` (whose ``bool()`` is always True and whose ``len()``
  raises) and ``weaviate.util._to_beacons`` (which is why this tool does not
  use ``reference_replace``). A list-only fake is what let the original
  duplicate-beacon defect live in shipped code; a double kinder than reality
  hides the bug it was written to catch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_ref_dedup as rd  # noqa: E402

_internal = pytest.importorskip("weaviate.collections.classes.internal")
_CrossReference = _internal._CrossReference
_weaviate_util = pytest.importorskip("weaviate.util")

U1 = "11111111-1111-5111-8111-111111111111"
U2 = "22222222-2222-5222-8222-222222222222"
U3 = "33333333-3333-5333-8333-333333333333"
ROW = "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa"
ROW2 = "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb"

COLL = "Proj_CodeModule"


def beacon(uid: str, cls: str = COLL) -> str:
    return f"weaviate://localhost/{cls}/{uid}" if cls else \
        f"weaviate://localhost/{uid}"


def ref(uid: str, cls: str = COLL) -> Dict[str, str]:
    """The REST representation of one stored beacon (``href`` included, as the
    live server sends it — a stripped-down double would hide a shape drift)."""
    b = beacon(uid, cls)
    return {"beacon": b, "href": "/v1/objects/" + b.split("localhost/", 1)[1]}


# ===========================================================================
# beacon_target_uuid — the identity every decision hangs on
# ===========================================================================
def test_beacon_uuid_class_qualified_and_class_less_agree() -> None:
    assert rd.beacon_target_uuid(beacon(U1)) == U1
    assert rd.beacon_target_uuid(beacon(U1, "")) == U1


def test_beacon_uuid_canonicalises_case() -> None:
    assert rd.beacon_target_uuid(beacon(U1.upper())) == U1


@pytest.mark.parametrize("bad", [
    None, 123, "", "   ", "not-a-beacon",
    "weaviate://localhost/Coll/not-a-uuid",
    "https://localhost/Coll/" + U1,          # wrong scheme
    "weaviate://localhost",                   # no path segment
])
def test_beacon_uuid_returns_none_for_anything_unparsable(bad: Any) -> None:
    assert rd.beacon_target_uuid(bad) is None


# ===========================================================================
# plan_row — one test per branch, act AND leave-alone
# ===========================================================================
def test_duplicates_collapse_and_preserve_the_distinct_set() -> None:
    value = [ref(U1), ref(U2), ref(U1), ref(U1), ref(U2)]
    plan = rd.plan_row(COLL, ROW, "imports", value)
    assert plan.action == rd.ACTION_COLLAPSE
    assert (plan.before, plan.after, plan.removable) == (5, 2, 3)
    # THE invariant, stated as the test states it to a reader:
    assert set(plan.target_uuids) == {U1, U2}
    # ordered-unique, first-seen order (not sorted, not reversed)
    assert plan.target_uuids == (U1, U2)
    assert plan.beacons == (beacon(U1), beacon(U2))


def test_already_unique_row_is_left_alone_and_carries_no_payload() -> None:
    plan = rd.plan_row(COLL, ROW, "imports", [ref(U1), ref(U2)])
    assert plan.action == rd.ACTION_LEAVE
    assert plan.reason == rd.REASON_ALREADY_UNIQUE
    assert plan.removable == 0
    # A leave plan MUST NOT be writable: no payload to hand to put_references.
    assert plan.beacons == ()


def test_absent_reference_is_left_alone() -> None:
    plan = rd.plan_row(COLL, ROW, "imports", None)
    assert (plan.action, plan.reason) == (rd.ACTION_LEAVE, rd.REASON_NO_BEACONS)
    assert plan.beacons == ()


def test_empty_beacon_list_is_left_alone_and_never_crashes() -> None:
    plan = rd.plan_row(COLL, ROW, "imports", [])
    assert (plan.action, plan.reason) == (rd.ACTION_LEAVE, rd.REASON_NO_BEACONS)
    assert plan.before == plan.after == 0
    assert plan.beacons == ()


def test_non_list_value_is_skipped_not_guessed_at() -> None:
    plan = rd.plan_row(COLL, ROW, "imports", {"beacon": beacon(U1)})
    assert (plan.action, plan.reason) == (rd.ACTION_SKIP,
                                          rd.REASON_UNREADABLE_VALUE)
    assert plan.beacons == ()


def test_one_unreadable_entry_skips_the_WHOLE_row() -> None:
    # Partial readability is not readability: collapsing the readable half
    # would drop the target we could not parse.
    plan = rd.plan_row(COLL, ROW, "imports",
                       [ref(U1), ref(U1), {"beacon": "garbage"}])
    assert (plan.action, plan.reason) == (rd.ACTION_SKIP,
                                          rd.REASON_UNREADABLE_BEACON)
    assert plan.beacons == ()
    assert "garbage" in plan.detail


def test_non_dict_entry_skips_the_row() -> None:
    plan = rd.plan_row(COLL, ROW, "imports", [ref(U1), beacon(U1)])
    assert (plan.action, plan.reason) == (rd.ACTION_SKIP,
                                          rd.REASON_UNREADABLE_BEACON)


def test_mixed_beacon_forms_for_one_target_collapse_keeping_first_form() -> None:
    # The class-less legacy spelling and the class-qualified one name the SAME
    # object; identity is the UUID, representation is whatever came first.
    plan = rd.plan_row(COLL, ROW, "imports",
                       [ref(U1, ""), ref(U1), ref(U1, "")])
    assert plan.action == rd.ACTION_COLLAPSE
    assert plan.target_uuids == (U1,)
    assert plan.beacons == (beacon(U1, ""),)


def test_distinct_set_change_is_skipped_not_written(monkeypatch) -> None:
    """The invariant guard, exercised.

    Unreachable while ``_ordered_unique`` is correct — which is exactly why it
    is worth pinning: the day someone changes the dedup key, this is the line
    that turns silent data loss into a reported skip.
    """
    monkeypatch.setattr(rd, "_ordered_unique",
                        lambda entries: ((U1,), (beacon(U1),)))
    plan = rd.plan_row(COLL, ROW, "imports", [ref(U1), ref(U2), ref(U2)])
    assert (plan.action, plan.reason) == (rd.ACTION_SKIP, rd.REASON_INVARIANT)
    assert plan.beacons == ()


def test_would_empty_is_skipped_not_written(monkeypatch) -> None:
    monkeypatch.setattr(rd, "_ordered_unique", lambda entries: ((), ()))
    plan = rd.plan_row(COLL, ROW, "imports", [ref(U1), ref(U1)])
    assert plan.action == rd.ACTION_SKIP
    assert plan.reason in (rd.REASON_INVARIANT, rd.REASON_WOULD_EMPTY)
    assert plan.beacons == ()


# ===========================================================================
# The REAL _CrossReference: the tool must agree with the production read path
# ===========================================================================
class _Row:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.properties: Dict[str, Any] = {}


class _Holder:
    def __init__(self, references: Any) -> None:
        self.references = references


def test_real_cross_reference_is_the_shape_this_tool_is_reconciled_against() -> None:
    """Pins the premise. If these stop holding, revisit — don't delete."""
    cross = _CrossReference._from([_Row(U1)])
    with pytest.raises(TypeError):
        len(cross)
    with pytest.raises(TypeError):
        iter(cross)
    assert bool(_CrossReference._from([])) is True


def test_tool_survivor_set_equals_what_the_production_read_path_sees() -> None:
    """The bridge test: REST beacons in, ``_CrossReference`` out.

    ``query_code_structure`` answers from the resolved references; this tool
    rewrites the raw beacons. If the two ever disagreed about which distinct
    targets a row has, the repair would silently change a query's answer.
    """
    from vco_lib.codegraph_references import (
        dedup_ref_targets,
        read_cross_reference,
        reference_target_uuids,
    )

    stored = [U1, U2, U1, U1, U2, U2]
    rest_value = [ref(u) for u in stored]
    rows = []
    for u in stored:
        r = _Row(u)
        r.properties = {"path": f"{u}.py"}
        rows.append(r)
    holder = _Holder({"imports": _CrossReference._from(rows)})

    plan = rd.plan_row(COLL, ROW, "imports", rest_value)
    seen_by_reader = reference_target_uuids(holder, "imports")

    # Same duplication is visible from both sides …
    assert len(seen_by_reader) == plan.before == 6
    # … and the same distinct targets survive.
    assert set(seen_by_reader) == set(plan.target_uuids) == {U1, U2}
    # The read side's display collapse and this tool's storage collapse agree
    # on how many rows remain.
    survivors = dedup_ref_targets(read_cross_reference(holder, "imports"),
                                  ("path",))
    assert len(survivors) == plan.after == 2


def test_empty_real_cross_reference_matches_the_no_beacons_branch() -> None:
    from vco_lib.codegraph_references import reference_target_uuids

    holder = _Holder({"imports": _CrossReference._from([])})
    assert reference_target_uuids(holder, "imports") == []
    assert rd.plan_row(COLL, ROW, "imports", []).action == rd.ACTION_LEAVE


def test_reference_replace_would_rewrite_beacons_class_less() -> None:
    """Why this module PUTs raw beacon strings instead of calling
    ``data.reference_replace``. Asserted against the real client helper, so the
    rationale fails loudly if upstream ever changes it."""
    rebuilt = _weaviate_util._to_beacons([U1, U2])
    assert [b["beacon"] for b in rebuilt] == [
        f"weaviate://localhost/{U1}", f"weaviate://localhost/{U2}"]
    # …i.e. NOT what the analyzer stored:
    assert rebuilt[0]["beacon"] != beacon(U1)
    # Ordering is preserved by the client helper (relevant either way).
    assert _weaviate_util._to_beacons([U2, U1])[0]["beacon"].endswith(U2)


# ===========================================================================
# Scope resolution
# ===========================================================================
LIVE = [
    "Proj_CodeModule", "Proj_CodeClass", "Proj_CodeFunction",
    "Vct_coordination_CodeModule", "Vct_coordination_CodeFunction",
    "CodeModule",
]


def test_reference_props_for_prefixed_and_bare_and_foreign() -> None:
    assert rd.reference_props_for("Proj_CodeModule") == ("imports",)
    assert rd.reference_props_for("CodeClass") == ("module", "extends")
    assert rd.reference_props_for("Proj_KnowledgeGraph") == ()
    # A name that merely CONTAINS a base is not a code class.
    assert rd.reference_props_for("CodeModuleRegistry") == ()


def test_prefixes_and_collections_for_prefix() -> None:
    assert rd.prefixes_of(LIVE) == ["", "Proj", "Vct_coordination"]
    assert rd.collections_for_prefix("Proj", LIVE) == [
        "Proj_CodeClass", "Proj_CodeFunction", "Proj_CodeModule"]
    # Prefix matching includes the separator: "Vct" must not claim
    # "Vct_coordination"'s classes.
    assert rd.collections_for_prefix("Vct", LIVE) == []
    assert rd.collections_for_prefix("", LIVE) == ["CodeModule"]


def test_resolve_prefix_verbatim_then_canonical_then_sanitizer() -> None:
    assert rd.resolve_prefix("Proj", LIVE) == "Proj"
    # underscore-PRESERVING rule (what the analyzer uses)
    assert rd.resolve_prefix("vct_coordination", LIVE) == "Vct_coordination"
    # underscore-DROPPING rule, for installs minted by the KG sanitizer
    assert rd.resolve_prefix("proj", ["ProjCodeless_CodeModule",
                                     "Proj_CodeModule"]) == "Proj"


def test_resolve_prefix_refuses_to_guess_and_lists_what_exists() -> None:
    with pytest.raises(rd.RefDedupError) as exc:
        rd.resolve_prefix("Nope", LIVE)
    msg = str(exc.value)
    assert "Nope" in msg and "Vct_coordination" in msg and "Proj" in msg


# ===========================================================================
# A stateful fake Weaviate — the three REST shapes this tool uses
# ===========================================================================
class _FakeServer:
    """Stores real beacon lists and answers list/get/put like the server does.

    ``fail_pages`` / ``fail_gets`` / ``fail_puts`` inject the failures the
    fail-closed branches exist for. ``on_get`` lets a test mutate state between
    the scan read and the pre-write re-read (the concurrency case).
    """

    def __init__(self, rows: Dict[str, Dict[str, Any]],
                 classes: Optional[List[str]] = None) -> None:
        # rows: {collection: {uuid: {prop: [beacon-dict, ...]}}}
        self.rows = rows
        self.classes = classes or sorted(rows)
        self.calls: List[Tuple[str, str]] = []
        self.puts: List[Tuple[str, str, str, List[str]]] = []
        self.fail_pages: set = set()
        self.fail_gets: set = set()
        self.fail_puts: set = set()
        self.on_get: Any = None

    # -- transport ---------------------------------------------------------
    def __call__(self, method: str, url: str, *, body: Any = None,
                 timeout: float = 60.0) -> Tuple[int, bytes]:
        self.calls.append((method, url))
        path = url.split("/v1/", 1)[1]
        if method == "GET" and path == "schema":
            return self._ok({"classes": [{"class": c} for c in self.classes]})
        if method == "GET" and path.startswith("objects?"):
            return self._list(path)
        if method == "GET" and path.startswith("objects/"):
            return self._get(path)
        if method == "PUT" and "/references/" in path:
            return self._put(path, body)
        return (404, b'{"error":"unrouted"}')

    @staticmethod
    def _ok(payload: Any) -> Tuple[int, bytes]:
        return (200, json.dumps(payload).encode())

    def _list(self, path: str) -> Tuple[int, bytes]:
        query = dict(
            kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
        coll = query["class"]
        if coll in self.fail_pages:
            return (500, b'{"error":"boom"}')
        limit = int(query.get("limit", "100"))
        uuids = sorted(self.rows.get(coll, {}))
        after = query.get("after")
        if after:
            uuids = [u for u in uuids if u > after]
        page = uuids[:limit]
        return self._ok({"objects": [
            {"id": u, "class": coll,
             "properties": dict(self.rows[coll][u])} for u in page]})

    def _get(self, path: str) -> Tuple[int, bytes]:
        coll, uid = path[len("objects/"):].split("/", 1)
        if uid in self.fail_gets:
            return (500, b'{"error":"boom"}')
        if self.on_get is not None:
            self.on_get(coll, uid)
        if uid not in self.rows.get(coll, {}):
            return (404, b'{"error":"not found"}')
        return self._ok({"id": uid, "class": coll,
                         "properties": dict(self.rows[coll][uid])})

    def _put(self, path: str, body: Any) -> Tuple[int, bytes]:
        rest, prop = path.rsplit("/references/", 1)
        coll, uid = rest[len("objects/"):].split("/", 1)
        if uid in self.fail_puts:
            return (500, b'{"error":"boom"}')
        assert isinstance(body, list) and body, "empty PUT reached the server"
        beacons = [e["beacon"] for e in body]
        self.puts.append((coll, uid, prop, beacons))
        self.rows[coll][uid][prop] = [
            {"beacon": b, "href": "/x"} for b in beacons]
        return (200, b"{}")


def _server(dup: int = 3) -> _FakeServer:
    rows: Dict[str, Dict[str, Any]] = {COLL: {
        ROW: {"imports": [ref(U1)] * dup + [ref(U2)], "path": "a.py"},
        ROW2: {"imports": [ref(U3)], "path": "b.py"},
    }}
    return _FakeServer(rows)


# ===========================================================================
# put_references — the never-empty chokepoint
# ===========================================================================
def test_put_references_refuses_an_empty_list_before_any_request() -> None:
    srv = _server()
    with pytest.raises(rd.RefDedupError) as exc:
        rd.put_references("http://x", COLL, ROW, "imports", [], request=srv)
    assert "empty" in str(exc.value).lower()
    assert srv.calls == []          # nothing was even attempted
    assert srv.puts == []


def test_put_references_raises_on_non_2xx() -> None:
    srv = _server()
    srv.fail_puts.add(ROW)
    with pytest.raises(rd.RefDedupError):
        rd.put_references("http://x", COLL, ROW, "imports", [beacon(U1)],
                          request=srv)


# ===========================================================================
# list_code_collections — loud, never a silent zero
# ===========================================================================
def test_list_code_collections_filters_to_code_classes() -> None:
    srv = _FakeServer({}, classes=["Proj_CodeModule", "Proj_KnowledgeGraph",
                                   "CodeFunction"])
    assert rd.list_code_collections("http://x", request=srv) == [
        "CodeFunction", "Proj_CodeModule"]


def test_list_code_collections_raises_rather_than_reporting_none() -> None:
    def boom(method: str, url: str, **kw: Any) -> Tuple[int, bytes]:
        return (503, b"down")

    with pytest.raises(rd.RefDedupError):
        rd.list_code_collections("http://x", request=boom)


def test_transport_exception_is_not_swallowed() -> None:
    def boom(method: str, url: str, **kw: Any) -> Tuple[int, bytes]:
        raise OSError("connection refused")

    with pytest.raises(OSError):
        rd.list_code_collections("http://x", request=boom)


# ===========================================================================
# repair_collection — dry-run default, act, leave-alone, skip, fail-closed
# ===========================================================================
def test_dry_run_reports_the_change_and_writes_nothing() -> None:
    srv = _server()
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=False)
    assert srv.puts == []
    assert rep.rows_changed == 1
    assert (rep.beacons_before, rep.beacons_after, rep.beacons_removed) == \
        (4, 2, 2)
    assert rep.changed_rows[0]["applied"] is False
    assert rep.rows_inspected == 2          # the clean row was inspected too


def test_apply_collapses_and_leaves_the_clean_row_untouched() -> None:
    srv = _server()
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert len(srv.puts) == 1
    coll, uid, prop, beacons = srv.puts[0]
    assert (coll, uid, prop) == (COLL, ROW, "imports")
    assert beacons == [beacon(U1), beacon(U2)]
    # the already-unique row was never written
    assert all(p[1] != ROW2 for p in srv.puts)
    assert rep.rows_changed == 1 and rep.writes == 1
    assert rep.beacons_removed == 2
    assert rep.errors == []
    # and the stored row now really holds the collapsed set
    stored = srv.rows[COLL][ROW]["imports"]
    assert [e["beacon"] for e in stored] == [beacon(U1), beacon(U2)]


def test_second_run_is_a_no_op() -> None:
    srv = _server()
    rd.repair_collection("http://x", COLL, request=srv, apply=True)
    before_puts = len(srv.puts)
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert len(srv.puts) == before_puts      # zero further writes
    assert rep.rows_changed == 0
    assert rep.beacons_removed == 0
    assert rep.skipped_total == 0
    assert rep.errors == []


def test_unreadable_row_is_skipped_and_reported_nothing_written() -> None:
    srv = _server()
    srv.rows[COLL]["cccccccc-cccc-5ccc-8ccc-cccccccccccc"] = {
        "imports": [ref(U1), {"beacon": "junk"}, ref(U1)], "path": "c.py"}
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert rep.skipped.get(rd.REASON_UNREADABLE_BEACON) == 1
    assert all(p[1] != "cccccccc-cccc-5ccc-8ccc-cccccccccccc"
               for p in srv.puts)
    assert rep.skipped_rows[0]["reason"] == rd.REASON_UNREADABLE_BEACON


def test_page_read_failure_marks_the_view_incomplete_and_writes_nothing() -> None:
    srv = _server()
    srv.fail_pages.add(COLL)
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert rep.truncated is True
    assert rep.errors and "page read failed" in rep.errors[0]
    assert srv.puts == []
    assert rep.rows_changed == 0


def test_row_collapsed_by_someone_else_between_scan_and_write_is_not_written() -> None:
    srv = _server()

    def collapse_it(coll: str, uid: str) -> None:
        if uid == ROW:
            srv.rows[coll][uid]["imports"] = [ref(U1), ref(U2)]
            srv.on_get = None       # only once, so verification still works

    srv.on_get = collapse_it
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert srv.puts == []
    assert rep.rows_changed == 0
    assert rep.skipped.get(rd.REASON_CHANGED_SINCE_SCAN) == 1


def test_recheck_writes_the_FRESH_target_set_not_the_stale_one() -> None:
    """A concurrent analyzer adding a genuinely new edge must not lose it."""
    srv = _server()

    def add_edge(coll: str, uid: str) -> None:
        if uid == ROW:
            srv.rows[coll][uid]["imports"] = (
                [ref(U1)] * 3 + [ref(U2), ref(U3)])
            srv.on_get = None

    srv.on_get = add_edge
    rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert srv.puts[0][3] == [beacon(U1), beacon(U2), beacon(U3)]


def test_no_recheck_still_never_writes_an_unsafe_payload() -> None:
    srv = _server()
    rd.repair_collection("http://x", COLL, request=srv, apply=True,
                         recheck=False, verify=False)
    assert srv.puts[0][3] == [beacon(U1), beacon(U2)]
    # with recheck off there is exactly one GET per page and no per-row GETs
    assert not any(m == "GET" and "/objects/" + COLL + "/" in u
                   for m, u in srv.calls)


def test_write_failure_is_reported_and_does_not_abort_the_collection() -> None:
    srv = _server()
    srv.rows[COLL]["dddddddd-dddd-5ddd-8ddd-dddddddddddd"] = {
        "imports": [ref(U2), ref(U2)], "path": "d.py"}
    srv.fail_puts.add(ROW)
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert rep.errors and ROW in rep.errors[0]
    # the OTHER duplicated row was still repaired
    assert [p[1] for p in srv.puts] == [
        "dddddddd-dddd-5ddd-8ddd-dddddddddddd"]
    assert rep.rows_changed == 1


def test_post_write_verification_catches_a_changed_target_set() -> None:
    srv = _server()
    real_put = srv._put

    def clobbering_put(path: str, body: Any) -> Tuple[int, bytes]:
        out = real_put(path, body)
        rest, prop = path.rsplit("/references/", 1)
        coll, uid = rest[len("objects/"):].split("/", 1)
        srv.rows[coll][uid][prop] = [ref(U1)]      # U2 vanished
        return out

    srv._put = clobbering_put  # type: ignore[method-assign]
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert rep.errors and "post-write target set differs" in rep.errors[0]
    assert "lost" in rep.errors[0]


def test_verification_off_does_not_invent_an_error() -> None:
    srv = _server()
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True,
                               verify=False)
    assert rep.errors == [] and rep.rows_changed == 1


def test_write_budget_stops_writing_but_keeps_counting() -> None:
    srv = _server()
    for i, u in enumerate("efgh"):
        srv.rows[COLL][f"{u*8}-{u*4}-5{u*3}-8{u*3}-{u*12}"] = {
            "imports": [ref(U3), ref(U3)], "path": f"{u}.py"}
    budget = [2]
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True,
                               write_budget=budget)
    assert len(srv.puts) == 2 and budget[0] == 0
    uncapped = [r for r in rep.changed_rows if r.get("note")]
    assert uncapped, "rows past the cap must still be reported as owed"
    assert all(r["applied"] is False for r in uncapped)


def test_props_filter_narrows_what_is_considered() -> None:
    srv = _FakeServer({"Proj_CodeClass": {ROW: {
        "extends": [ref(U1, "Proj_CodeClass")] * 3,
        "module": [ref(U2, "Proj_CodeModule")] * 2}}})
    rep = rd.repair_collection("http://x", "Proj_CodeClass", request=srv,
                               apply=True, props=["extends"])
    assert [p[2] for p in srv.puts] == ["extends"]
    assert rep.pairs_inspected == 1


def test_non_code_collection_is_a_no_op() -> None:
    srv = _FakeServer({"Proj_KnowledgeGraph": {ROW: {}}})
    rep = rd.repair_collection("http://x", "Proj_KnowledgeGraph", request=srv)
    assert rep.rows_inspected == 0 and srv.calls == []


# ===========================================================================
# run() — scoping
# ===========================================================================
def test_run_requires_an_explicit_scope() -> None:
    srv = _server()
    with pytest.raises(rd.RefDedupError):
        rd.run(request=srv, weaviate_url="http://x")
    with pytest.raises(rd.RefDedupError):
        rd.run(request=srv, weaviate_url="http://x", project="Proj",
               all_projects=True)


def test_run_project_scope_touches_only_that_project() -> None:
    rows = {
        "Proj_CodeModule": {ROW: {"imports": [ref(U1)] * 4}},
        "Other_CodeModule": {ROW2: {
            "imports": [ref(U2, "Other_CodeModule")] * 5}},
    }
    srv = _FakeServer(rows)
    rep = rd.run(request=srv, weaviate_url="http://x", project="Proj",
                 apply=True)
    assert [p[0] for p in srv.puts] == ["Proj_CodeModule"]
    assert [c.collection for c in rep.collections] == ["Proj_CodeModule"]
    assert rep.beacons_removed == 3


def test_run_all_projects_covers_every_prefix() -> None:
    rows = {
        "Proj_CodeModule": {ROW: {"imports": [ref(U1)] * 4}},
        "Other_CodeModule": {ROW2: {
            "imports": [ref(U2, "Other_CodeModule")] * 5}},
    }
    srv = _FakeServer(rows)
    rep = rd.run(request=srv, weaviate_url="http://x", all_projects=True,
                 apply=True)
    assert sorted(p[0] for p in srv.puts) == ["Other_CodeModule",
                                              "Proj_CodeModule"]
    assert rep.beacons_removed == 3 + 4


def test_run_raises_when_the_server_has_no_code_collections() -> None:
    srv = _FakeServer({}, classes=["Proj_KnowledgeGraph"])
    with pytest.raises(rd.RefDedupError):
        rd.run(request=srv, weaviate_url="http://x", all_projects=True)


def test_run_max_writes_caps_the_whole_run_not_each_collection() -> None:
    rows = {
        "Proj_CodeModule": {ROW: {"imports": [ref(U1)] * 4}},
        "Other_CodeModule": {ROW2: {
            "imports": [ref(U2, "Other_CodeModule")] * 5}},
    }
    srv = _FakeServer(rows)
    rep = rd.run(request=srv, weaviate_url="http://x", all_projects=True,
                 apply=True, max_writes=1)
    assert len(srv.puts) == 1
    assert rep.write_cap_reached is True


# ===========================================================================
# CLI
# ===========================================================================
def _run_cli(monkeypatch, srv: _FakeServer, argv: List[str]) -> Tuple[int, str, str]:
    monkeypatch.setattr(rd, "_http_request", srv)
    import io
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = rd.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_cli_defaults_to_dry_run(monkeypatch) -> None:
    srv = _server()
    rc, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj"])
    assert srv.puts == []
    assert rc == 0
    assert "DRY RUN" in out and "Re-run with --apply" in out
    assert "4 -> 2" in out          # per-row before/after
    assert "2 removed" in out


def test_cli_apply_writes_and_warns(monkeypatch) -> None:
    srv = _server()
    rc, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj", "--apply"])
    assert len(srv.puts) == 1 and rc == 0
    assert "APPLY" in out
    assert "Do NOT run a code-graph analysis concurrently" in out


def test_cli_json_keeps_stdout_a_machine_contract(monkeypatch) -> None:
    srv = _server()
    rc, out, err = _run_cli(monkeypatch, srv, ["--project", "Proj", "--json"])
    payload = json.loads(out)       # stdout parses as JSON, nothing else on it
    assert payload["apply"] is False
    assert payload["totals"]["beacons_removed"] == 2
    assert "DRY RUN" in err         # the human report went to stderr
    assert rc == 0


def test_cli_unknown_project_exits_2_and_says_what_exists(monkeypatch) -> None:
    srv = _server()
    rc, _, err = _run_cli(monkeypatch, srv, ["--project", "Nope"])
    assert rc == 2 and "Available prefixes" in err


def test_cli_exit_3_means_work_owed_not_failure(monkeypatch) -> None:
    """Skips and errors are different events and must not share an exit code —
    a clean run with 2 unrepairable rows reporting ``1`` reads as a failure."""
    srv = _server()
    srv.rows[COLL][ROW]["imports"] = [ref(U1), {"beacon": "junk"}]
    rc, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj"])
    assert rc == 3
    assert "SKIPPED" in out


def test_cli_exit_1_is_reserved_for_real_failures(monkeypatch) -> None:
    srv = _server()
    srv.fail_puts.add(ROW)
    rc, _, _ = _run_cli(monkeypatch, srv, ["--project", "Proj", "--apply"])
    assert rc == 1


def test_cli_exit_1_on_an_incomplete_read(monkeypatch) -> None:
    srv = _server()
    srv.fail_pages.add(COLL)
    rc, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj"])
    assert rc == 1
    assert "INCOMPLETE" in out


def test_by_project_rollup_is_ascending_so_the_canary_is_first() -> None:
    rows = {
        "Big_CodeModule": {ROW: {"imports": [ref(U1, "Big_CodeModule")] * 40}},
        "Small_CodeModule": {ROW2: {
            "imports": [ref(U2, "Small_CodeModule")] * 2}},
    }
    srv = _FakeServer(rows)
    rep = rd.run(request=srv, weaviate_url="http://x", all_projects=True)
    order = [p["project"] for p in rep.by_project()]
    assert order == ["Small", "Big"]
    assert rep.by_project()[0]["beacons_removed"] == 1


def test_cli_prints_the_per_project_rollup(monkeypatch) -> None:
    rows = {
        "Big_CodeModule": {ROW: {"imports": [ref(U1, "Big_CodeModule")] * 40}},
        "Small_CodeModule": {ROW2: {
            "imports": [ref(U2, "Small_CodeModule")] * 2}},
    }
    srv = _FakeServer(rows)
    _, out, _ = _run_cli(monkeypatch, srv, ["--all-projects"])
    assert "By project" in out
    assert out.index("  Small") < out.index("  Big")


# ===========================================================================
# Dangling targets — the failure real data produced, which no fake predicted
# ===========================================================================
_REJECT_422 = (
    "reference replace Proj_CodeModule/x.imports: HTTP 422 — "
    '{"error":[{"message":"msg:validate existence code:400 err:validate '
    'reference: no object with id ' + U3 + ' found"}]}'
)


def test_classify_write_rejection_names_the_missing_target() -> None:
    plan = rd.classify_write_rejection(_REJECT_422)
    assert plan is not None
    assert plan.reason == rd.REASON_DANGLING_TARGET
    assert U3 in plan.detail


@pytest.mark.parametrize("msg", [
    "", "HTTP 500 — internal error",
    "HTTP 422 — some other validation problem",
])
def test_unknown_rejections_stay_errors_not_friendly_labels(msg: str) -> None:
    assert rd.classify_write_rejection(msg) is None


def test_dangling_target_row_is_skipped_with_a_remedy_not_an_error() -> None:
    srv = _server()
    srv.fail_puts.add(ROW)

    def rejecting_put(path: str, body: Any) -> Tuple[int, bytes]:
        return (422, json.dumps({"error": [{"message":
                "msg:validate existence code:400 err:validate reference: "
                "no object with id " + U3 + " found"}]}).encode())

    srv.fail_puts.clear()
    srv._put = rejecting_put  # type: ignore[method-assign]
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True)
    assert rep.errors == []                     # not an anonymous error
    assert rep.skipped.get(rd.REASON_DANGLING_TARGET) == 1
    assert U3 in rep.skipped_rows[0]["detail"]
    # the row keeps its duplicates — nothing was written
    assert len(srv.rows[COLL][ROW]["imports"]) == 4
    assert rd.REASON_DANGLING_TARGET in rd.SKIP_REMEDIES


def test_every_named_skip_reason_carries_a_remedy() -> None:
    """A reason with no remedy leaves the user with nowhere to go."""
    reasons = {v for k, v in vars(rd).items()
               if k.startswith("REASON_") and isinstance(v, str)}
    # These two are LEAVE outcomes, not skips: nothing is owed, so no remedy.
    reasons -= {rd.REASON_ALREADY_UNIQUE, rd.REASON_NO_BEACONS,
                rd.REASON_DUPLICATES}
    assert reasons <= set(rd.SKIP_REMEDIES)


def test_errors_are_never_silenced_by_the_row_listing_cap(monkeypatch) -> None:
    """The defect this run exposed: ``--max-rows-listed 0`` hid the error text
    that explained why 46 rows were not repaired."""
    srv = _server()
    srv.fail_puts.add(ROW)
    rc, out, _ = _run_cli(monkeypatch, srv,
                          ["--project", "Proj", "--apply",
                           "--max-rows-listed", "0"])
    assert rc == 1
    assert "HTTP 500" in out and ROW in out


def test_report_separates_collection_wide_beacons_from_touched_rows() -> None:
    """`stored` counts the WHOLE collection, `before/after` only what changes.

    Reading the first as the second is exactly the mistake the original
    single-pair column invited."""
    srv = _server()
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=False)
    assert (rep.beacons_before, rep.beacons_after) == (4, 2)   # changed row
    assert (rep.beacons_seen, rep.beacons_seen_unique) == (5, 3)  # + clean row
    assert rep.beacons_removed == (rep.beacons_seen
                                   - rep.beacons_seen_unique) == 2


def test_apply_output_tells_the_reader_how_to_verify(monkeypatch) -> None:
    srv = _server()
    _, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj", "--apply"])
    assert "re-run WITHOUT --apply" in out


# ===========================================================================
# --check-targets — predicting the rejection instead of discovering it
# ===========================================================================
def test_target_exists_reports_present_missing_and_unknown() -> None:
    srv = _FakeServer({COLL: {U1: {}}})
    cache: Dict[str, Any] = {}
    assert rd.target_exists("http://x", beacon(U1), request=srv,
                            cache=cache) is True
    assert rd.target_exists("http://x", beacon(U3), request=srv,
                            cache=cache) is False
    # class-less beacon: nothing to address, so "cannot tell" — NOT "missing".
    assert rd.target_exists("http://x", beacon(U1, ""), request=srv,
                            cache=cache) is None


def test_target_exists_probe_failure_is_unknown_not_missing() -> None:
    def boom(method: str, url: str, **kw: Any) -> Tuple[int, bytes]:
        raise OSError("refused")

    assert rd.target_exists("http://x", beacon(U1), request=boom,
                            cache={}) is None


def test_target_existence_is_cached_across_rows() -> None:
    srv = _FakeServer({COLL: {U1: {}}})
    cache: Dict[str, Any] = {}
    for _ in range(5):
        rd.target_exists("http://x", beacon(U3), request=srv, cache=cache)
    probes = [u for m, u in srv.calls if m == "GET" and U3 in u]
    assert len(probes) == 1


def test_check_targets_predicts_the_rejection_in_dry_run() -> None:
    srv = _server()
    # U1 exists as a row; U2 does not -> the ROW's collapse would be refused.
    srv.rows[COLL][U1] = {"path": "target.py"}
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=False,
                               check_targets=True)
    assert rep.rows_changed == 0
    assert rep.skipped.get(rd.REASON_DANGLING_TARGET) == 1
    assert U2 in rep.skipped_rows[0]["detail"]


def test_check_targets_skips_the_doomed_write_under_apply() -> None:
    srv = _server()
    srv.rows[COLL][U1] = {"path": "target.py"}
    rd.repair_collection("http://x", COLL, request=srv, apply=True,
                         check_targets=True)
    assert srv.puts == []          # no PUT Weaviate would have refused


def test_check_targets_leaves_healthy_rows_collapsible() -> None:
    srv = _server()
    srv.rows[COLL][U1] = {"path": "t1.py"}
    srv.rows[COLL][U2] = {"path": "t2.py"}
    rep = rd.repair_collection("http://x", COLL, request=srv, apply=True,
                               check_targets=True)
    assert rep.rows_changed == 1 and len(srv.puts) == 1
    assert rep.skipped_total == 0


def test_dry_run_says_it_did_not_check_targets(monkeypatch) -> None:
    srv = _server()
    _, out, _ = _run_cli(monkeypatch, srv, ["--project", "Proj"])
    assert "--check-targets" in out


def test_checked_run_does_not_repeat_the_nudge(monkeypatch) -> None:
    srv = _server()
    srv.rows[COLL][U1] = {"path": "t1.py"}
    srv.rows[COLL][U2] = {"path": "t2.py"}
    _, out, _ = _run_cli(monkeypatch, srv,
                         ["--project", "Proj", "--check-targets"])
    assert "Add --check-targets" not in out


def test_cli_row_listing_cap_is_announced_never_silent(monkeypatch) -> None:
    srv = _server()
    for u in "efghij":
        srv.rows[COLL][f"{u*8}-{u*4}-5{u*3}-8{u*3}-{u*12}"] = {
            "imports": [ref(U3), ref(U3)], "path": f"{u}.py"}
    rc, out, _ = _run_cli(monkeypatch, srv,
                          ["--project", "Proj", "--max-rows-listed", "2"])
    assert "and 5 more changed row(s) not listed" in out
    assert rc == 0


def test_skip_truncation_wording_is_exact_when_nothing_was_shown(
        monkeypatch) -> None:
    """"…and 14 MORE" after showing zero rows is a small lie; say what is
    true. Reporting precision is the whole product here."""
    srv = _server()
    srv.rows[COLL][ROW]["imports"] = [ref(U1), {"beacon": "junk"}]
    _, out, _ = _run_cli(monkeypatch, srv,
                         ["--project", "Proj", "--max-rows-listed", "0"])
    assert "and 1 skipped row(s) not listed" in out
    assert "1 more skipped" not in out
