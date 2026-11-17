"""v0.2.107 (CG-2, A-002 / A-095): the ONE whole-class listing.

A listing without a cursor returned 0 rows on a class that held 391,000, and
every sweep read "0 rows" as "empty / converged". ``vco_lib.codegraph_cursor``
pages from the nil uuid and checks a 0-row pass against the class count; these
tests pin the helper and every caller that deletes, clears or concludes on it.

The fake (``tests/common/cursor_fakes.CursorClass``) pages like Weaviate and
is NOT served by the suite's legacy-iterator adapter, so each test below runs
the real paging code and the real zero-row verdict.

Four shapes, per caller:
  * HIDDEN + count positive  -> undeterminable: nothing deleted, nothing
    cleared, never "converged";
  * HIDDEN + count unreadable (None) -> the same;
  * TRULY EMPTY (count 0)    -> reads as empty, the caller proceeds as before;
  * NORMAL                   -> every row visited exactly once, the caller acts.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.common.cursor_fakes import CursorClass, CursorClient, make_uuid, row
from vco_lib import codegraph_cursor as cc
from vco_lib import codegraph_prune as cp
from vco_lib import codegraph_resync as cr
from vco_lib import codegraph_schema as cs
from vco_lib import codegraph_source_purge as csp
from vco_lib import codegraph_vector_copy as cvc
from vco_lib import embed_slot_fill as esf

REPO = Path(__file__).resolve().parent.parent
ANALYZER = REPO / "templates" / "scripts" / "analyze_code_graph.py"

#: The two "cannot be trusted" shapes: (count the helper is told, label).
HIDDEN = [pytest.param(391_000, id="count-positive"), pytest.param(None, id="count-unreadable")]


def _rows(n, props=None):
    return [row(i, dict(props or {"path": f"src/f{i}.py"})) for i in range(1, n + 1)]


def _hidden(name, rows, count, **kw):
    """A class whose listing returns nothing while ``count`` says otherwise."""
    return CursorClass(name, rows, hide_rows=True, count=count, **kw)


# ═══════════════════════════════════════════════════════════════════════════
# The helper
# ═══════════════════════════════════════════════════════════════════════════

def test_normal_class_every_row_exactly_once_across_pages_always_with_a_cursor():
    coll = CursorClass("P_CodeFunction", _rows(250))
    seen = [o.uuid for o in cc.iter_class(coll, "t", return_properties=["path"], page_size=100)]
    assert sorted(seen) == sorted(make_uuid(i) for i in range(1, 251))
    assert len(seen) == len(set(seen)) == 250
    pages = coll.listing_pages()
    assert [p["after"] for p in pages[:2]] == [cc.NIL_UUID, make_uuid(100)]
    assert all(p["after"] is not None for p in pages), "a page was asked without a cursor"
    assert all(p["return_properties"] == ["path"] for p in pages)
    assert cc.undeterminable_classes() == {}


def test_the_defect_shape_a_cursorless_page_is_empty_but_the_cursor_reads_the_rows():
    """The fake reproduces A-002: no cursor -> nothing. The helper never asks
    that way, so it reads the class."""
    coll = CursorClass("P_CodeFunction", _rows(3), cursorless_first_page_empty=True)
    assert coll.query.fetch_objects(limit=3).objects == []          # the old read
    assert len(list(cc.iter_class(coll, "t", return_properties=[]))) == 3


@pytest.mark.parametrize("count", HIDDEN)
def test_zero_rows_on_a_class_that_is_not_confirmed_empty_is_undeterminable(count, caplog):
    coll = _hidden("P_CodeFunction", _rows(5), count)
    with caplog.at_level(logging.WARNING, logger="vco_lib.codegraph_cursor"):
        with pytest.raises(cc.CursorUndeterminable) as err:
            list(cc.iter_class(coll, "the.call.site", return_properties=[]))
    assert err.value.class_name == "P_CodeFunction" and err.value.call_site == "the.call.site"
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1, "exactly one log line per undeterminable listing"
    assert "P_CodeFunction" in lines[0] and "the.call.site" in lines[0]
    assert cc.undeterminable_classes() == {"P_CodeFunction": "the.call.site"}


def test_a_truly_empty_class_reads_as_empty():
    coll = CursorClass("P_CodeFunction", [])
    assert list(cc.iter_class(coll, "t", return_properties=[])) == []
    assert cc.undeterminable_classes() == {}


def test_a_page_failing_mid_way_is_undeterminable_after_the_rows_already_read():
    coll = CursorClass("P_CodeFunction", _rows(250), fail_on_page=2)
    got = []
    with pytest.raises(cc.CursorUndeterminable, match="page request failed after 100 row"):
        for obj in cc.iter_class(coll, "t", return_properties=[], page_size=100):
            got.append(obj.uuid)
    assert len(got) == 100


def test_vector_slots_and_references_reach_every_page_request():
    coll = CursorClass("P_CodeFunction", _rows(2))
    ref = object()
    list(cc.iter_class(coll, "t", return_properties=["a"], include_vector=["codesage_embed"],
                       return_references=[ref], page_size=1))
    for page in coll.listing_pages():
        assert page["include_vector"] == ["codesage_embed"]
        assert page["return_references"] == [ref]
        assert page["limit"] == 1


def test_a_consumer_that_stops_early_gets_no_verdict_and_no_extra_page():
    coll = CursorClass("P_CodeFunction", _rows(250))
    for _obj in cc.iter_class(coll, "t", return_properties=[], page_size=100):
        break
    assert len(coll.listing_pages()) == 1


def _rest(objects, *, status=200, count=None):
    """A REST transport: the objects page, then the Aggregate count reply."""
    import json
    calls = []

    def request(method, url, *, body=None, timeout=10.0):
        calls.append((method, url))
        if method == "GET":
            return status, json.dumps({"objects": objects}).encode()
        reply = ({"errors": [{"message": "unreadable"}]} if count is None
                 else {"data": {"Aggregate": {"P_Kg": [{"meta": {"count": count}}]}}})
        return 200, json.dumps(reply).encode()

    return request, calls


def test_rest_sample_asks_from_the_nil_cursor():
    request, calls = _rest([{"id": "u1"}])
    assert cc.rest_first_page("P_Kg", "t", limit=5, include_vector=True,
                              weaviate_url="http://w", request=request) == [{"id": "u1"}]
    assert calls == [("GET", f"http://w/v1/objects?class=P_Kg&limit=5&after={cc.NIL_UUID}&include=vector")]


@pytest.mark.parametrize("count", HIDDEN)
def test_rest_sample_zero_objects_on_an_unconfirmed_class_is_undeterminable(count):
    request, _ = _rest([], count=count)
    with pytest.raises(cc.CursorUndeterminable):
        cc.rest_first_page("P_Kg", "t", limit=5, weaviate_url="http://w", request=request)


def test_rest_sample_truly_empty_and_failed_request():
    request, _ = _rest([], count=0)
    assert cc.rest_first_page("P_Kg", "t", limit=5, weaviate_url="http://w", request=request) == []
    request, _ = _rest([{"id": "u"}], status=500)
    with pytest.raises(cc.CursorUndeterminable, match="HTTP 500"):
        cc.rest_first_page("P_Kg", "t", limit=5, weaviate_url="http://w", request=request)


@pytest.mark.parametrize("count", HIDDEN)
def test_slot_sample_gives_no_verdict_on_an_undeterminable_class(monkeypatch, count):
    request, _ = _rest([], count=count)
    from vco_lib import weaviate_helpers as wh
    monkeypatch.setattr(wh, "http_request", request)
    assert esf.sample_slot_population("P_Kg", "http://w", 10) == ({}, 0)


# ═══════════════════════════════════════════════════════════════════════════
# The exact-row deleter (every sweep's delete primitive)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("count", HIDDEN)
def test_deleter_undeterminable_deletes_nothing(count):
    coll = _hidden("P_CodeModule", _rows(4), count)
    with pytest.raises(cc.CursorUndeterminable):
        cr.delete_file_rows_exact(coll, "path", lambda raw, props: True)
    assert coll.deleted == []


def test_deleter_empty_class_and_normal_class():
    assert cr.delete_file_rows_exact(
        CursorClass("P_CodeModule", [], properties=["path"]), "path", lambda r, p: True) == (0, 0)
    coll = CursorClass("P_CodeModule", _rows(150))
    hit = {"src/f7.py", "src/f149.py"}
    assert cr.delete_file_rows_exact(coll, "path", lambda raw, props: raw in hit) == (2, 0)
    assert sorted(coll.deleted) == sorted([make_uuid(7), make_uuid(149)])


def test_deleter_narrowed_read_never_lists_the_class():
    """A-095 shape: a completed narrowed read is the candidate set."""
    coll = CursorClass("P_CodeFunction", _rows(3, {"full_name": "m.f"}))
    coll.filtered_reply = lambda filters, limit, kwargs: SimpleNamespace(objects=list(coll.rows.values())[:1])
    rows = list(cr.narrowed_or_all_rows(coll, ["full_name"], object(), "t"))
    assert len(rows) == 1 and coll.listing_pages() == []


# ═══════════════════════════════════════════════════════════════════════════
# --prune-stale (codegraph_prune.prune_collection + the run's status)
# ═══════════════════════════════════════════════════════════════════════════

def _prune(coll, visited=()):
    return cp.prune_collection(coll, set(visited), project_name="P",
                               canonical_lang_id=lambda v: str(v or "").lower())


def _prune_rows():
    return [row(i, {"project": "P", "file_path": f"src/f{i}.py"}) for i in range(1, 6)]


@pytest.mark.parametrize("count", HIDDEN)
def test_prune_undeterminable_deletes_nothing_and_the_run_is_partial(count):
    coll = _hidden("P_CodeFunction", _prune_rows(), count)
    assert _prune(coll) == (0, 0)
    assert coll.deleted == []
    holder = SimpleNamespace()
    stats = cp.prune_failure_stats(holder)
    assert stats["prune_failures"] == 1, "an unlisted class must make the run PARTIAL"
    assert stats["listing_undeterminable"] == {"P_CodeFunction": "codegraph_prune.prune_collection"}
    assert stats["prune_failure_collections"] == {}
    # A printed command is shipped code: no drop command for a class that
    # merely could not be listed.
    lines = cp.format_prune_remedy_lines({}, "P", total_failures=stats["prune_failures"])
    assert not any("curl -X DELETE" in ln for ln in lines), lines
    assert any("P_CodeFunction" in ln and "do NOT drop" in ln for ln in lines)


def test_prune_truly_empty_class_is_a_clean_zero():
    assert _prune(CursorClass("P_CodeFunction", [], properties=["project", "file_path"])) == (0, 0)
    assert cp.prune_failure_stats(SimpleNamespace())["prune_failures"] == 0


def test_prune_normal_class_deletes_exactly_the_unvisited_rows():
    coll = CursorClass("P_CodeFunction", _prune_rows())
    assert _prune(coll, visited={make_uuid(1), make_uuid(2)}) == (3, 0)
    assert sorted(coll.deleted) == [make_uuid(3), make_uuid(4), make_uuid(5)]
    assert cp.prune_failure_stats(SimpleNamespace())["prune_failures"] == 0


def test_prune_page_failing_mid_way_stops_and_is_partial():
    rows = [row(i, {"project": "P", "file_path": f"src/f{i}.py"}) for i in range(1, 251)]
    coll = CursorClass("P_CodeFunction", rows, fail_on_page=2)
    pruned, failures = _prune(coll)
    assert (pruned, failures) == (100, 0)          # rows really read on page 1
    assert cp.prune_failure_stats(SimpleNamespace())["prune_failures"] == 1


def test_remedy_still_names_a_class_whose_deletes_failed():
    """Leave-alone: the delete-failure remedy is unchanged."""
    lines = cp.format_prune_remedy_lines({"P_CodeFunction": 2}, "P", total_failures=2)
    assert any("curl -X DELETE" in ln and "P_CodeFunction" in ln for ln in lines)


def test_reset_prune_ledgers_starts_a_run_clean():
    with pytest.raises(cc.CursorUndeterminable):
        list(cc.iter_class(_hidden("P_X", _rows(1), 9), "t", return_properties=[]))
    holder = SimpleNamespace(_prune_failures=3, _prune_failure_collections={"a": 3})
    cp.reset_prune_ledgers(holder)
    assert cp.prune_failure_stats(holder) == {
        "prune_failures": 0, "prune_failure_collections": {}, "listing_undeterminable": {}}


# ═══════════════════════════════════════════════════════════════════════════
# The resync probes and the driver's convergence branch
# ═══════════════════════════════════════════════════════════════════════════

_REV = 7


def _stale_class(name, rows, **kw):
    """A class whose cheap existence probe says "a stale row exists" (so the
    classified scan runs) - the state after a revision bump."""
    coll = CursorClass(name, rows, **kw)
    coll.filtered_reply = lambda filters, limit, kwargs: SimpleNamespace(objects=[row(999)])
    return coll


def _graph(make):
    return CursorClient([
        make("Acme_CodeModule", [row(1, {"embed_revision": 1, "path": "a.py"})]),
        make("Acme_CodeClass", [row(2, {"embed_revision": 1, "file_path": "a.py"})]),
        make("Acme_CodeFunction", [row(3, {"embed_revision": 1, "file_path": "a.py"}),
                                   row(4, {"embed_revision": _REV, "file_path": "a.py"})]),
    ])


@pytest.mark.parametrize("count", HIDDEN)
def test_stale_count_is_none_never_zero_when_a_class_cannot_be_listed(count):
    client = _graph(lambda n, r: _stale_class(n, r, hide_rows=True, count=count))
    assert cr.count_stale_rows("Acme", current_revision=_REV, client=client) is None


def test_stale_count_normal_and_truly_empty():
    client = _graph(_stale_class)
    assert cr.count_stale_rows("Acme", current_revision=_REV, client=client) == {
        "Acme_CodeModule": 1, "Acme_CodeClass": 1, "Acme_CodeFunction": 1}
    empty = CursorClient([_stale_class(f"Acme_{b}", []) for b in ("CodeModule", "CodeClass", "CodeFunction")])
    assert cr.count_stale_rows("Acme", current_revision=_REV, client=empty) == {
        "Acme_CodeModule": 0, "Acme_CodeClass": 0, "Acme_CodeFunction": 0}


@pytest.mark.parametrize("count", HIDDEN)
def test_orphan_probe_and_its_paired_clear_stop_on_an_unlistable_class(count, tmp_path, monkeypatch):
    client = CursorClient([_hidden("Acme_CodeInteraction", _rows(3, {"project": "Acme"}), count)])
    assert cr.count_orphan_interaction_rows("Acme", repo_root=tmp_path, client=client) is None
    cleared = []
    import vco_lib.deferral_emit as de
    monkeypatch.setattr(de, "resolve_conditions", lambda *a, **k: cleared.append(a) or 1)
    assert cr.clear_orphan_interactions_deferral_if_zero(
        "Acme", repo_root=tmp_path, client=client) is None
    assert cleared == [], "an undeterminable count must never clear the ledger entry"
    out = csp.prune_orphan_interactions("Acme", repo_root=tmp_path, client=client)
    assert out == {"counted": None, "deleted": 0, "failures": 0}
    assert cleared == []


def test_orphan_clear_still_fires_on_a_truly_empty_class(tmp_path, monkeypatch):
    client = CursorClient([CursorClass("Acme_CodeInteraction", [], properties=["file_path", "project"])])
    cleared = []
    import vco_lib.deferral_emit as de
    monkeypatch.setattr(de, "resolve_conditions", lambda *a, **k: cleared.append(a) or 1)
    assert cr.clear_orphan_interactions_deferral_if_zero(
        "Acme", repo_root=tmp_path, client=client) is True
    assert len(cleared) == 1


@pytest.mark.parametrize("count", HIDDEN)
def test_other_probes_return_undeterminable(count, tmp_path):
    client = _graph(lambda n, r: _stale_class(n, r, hide_rows=True, count=count))
    assert cr.classify_stale_kinds("Acme", current_revision=_REV, client=client) is None
    assert cr.count_cleanup_owed_rows("Acme", repo_root=tmp_path, client=client) is None
    assert cr.demote_current_revision_rows("Acme", current_revision=_REV, client=client) is None


@pytest.mark.parametrize("count", HIDDEN)
def test_source_count_scan_arm_is_undeterminable(count):
    coll = _hidden("Acme_CodeModule", _rows(2, {"project_source": "/r"}), count)

    def _boom(filters, limit, kwargs):
        raise RuntimeError("filtered page unavailable")

    coll.filtered_reply = _boom
    assert csp.count_rows_for_project_source(
        "Acme", "/r", client=CursorClient([coll])) is None


def _driver(monkeypatch, tmp_path, client, capsys):
    """Run the resync driver over ``client`` with the walk itself stubbed."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    (tmp_path / "vct").mkdir()
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "code_backend_down_waiting", lambda *a, **k: (None, False))
    monkeypatch.setattr(cr, "_build_client", lambda *a, **k: client)
    monkeypatch.setattr(cr, "_resolve_embed_revision", lambda *a, **k: _REV)
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *a, **k: True)
    monkeypatch.setattr(cr.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0))
    calls = {"resolved": 0, "unconverged": 0, "orphan_resolved": 0, "exposure_cleared": 0}
    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral",
                        lambda *a, **k: calls.__setitem__("resolved", calls["resolved"] + 1))
    monkeypatch.setattr(cr, "_record_unconverged_deferral",
                        lambda *a, **k: calls.__setitem__("unconverged", calls["unconverged"] + 1))
    monkeypatch.setattr(cr, "_resolve_orphan_interactions_deferral",
                        lambda *a, **k: calls.__setitem__("orphan_resolved", calls["orphan_resolved"] + 1))
    from vco_lib import code_embed_exposure as exposure
    monkeypatch.setattr(exposure, "clear_exposure_bump",
                        lambda *a, **k: calls.__setitem__("exposure_cleared", calls["exposure_cleared"] + 1) or True)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    analyzer = scripts / "analyze_code_graph.py"
    analyzer.write_text("# stub analyzer\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")   # the rows' file exists
    capsys.readouterr()
    rc = cr.run_resync_and_verify("Acme", tmp_path, analyzer)
    return rc, calls, capsys.readouterr().out


@pytest.mark.parametrize("count", HIDDEN)
def test_driver_never_reports_converged_over_an_unlistable_class(count, monkeypatch, tmp_path, capsys):
    """REGISTER-CGWRITE remark 3: a zero from a blind scan used to resolve
    ``codegraph_embed_resync_pending`` and clear the exposure-heal marker."""
    client = _graph(lambda n, r: _stale_class(n, r, hide_rows=True, count=count))
    rc, calls, out = _driver(monkeypatch, tmp_path, client, capsys)
    assert rc == 0
    assert "NOT converged" in out and "unverifiable" in out
    assert "converged: 0 stale rows" not in out
    assert calls == {"resolved": 0, "unconverged": 1, "orphan_resolved": 0, "exposure_cleared": 0}


def test_driver_converges_on_truly_empty_classes(monkeypatch, tmp_path, capsys):
    client = CursorClient([_stale_class(f"Acme_{b}", []) for b in ("CodeModule", "CodeClass", "CodeFunction")])
    rc, calls, out = _driver(monkeypatch, tmp_path, client, capsys)
    assert rc == 0 and "converged: 0 stale rows" in out
    assert calls["resolved"] == 1 and calls["exposure_cleared"] == 1 and calls["unconverged"] == 0


def test_driver_reports_the_stale_rows_of_a_normal_class(monkeypatch, tmp_path, capsys):
    rc, calls, out = _driver(monkeypatch, tmp_path, _graph(_stale_class), capsys)
    assert rc == 0 and "NOT converged" in out and "stale rows: 3" in out
    assert calls["resolved"] == 0 and calls["unconverged"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Identity sweep, property backfill, slot scan, copy, migration edge, summaries
# ═══════════════════════════════════════════════════════════════════════════

_BASES = ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction")


@pytest.mark.parametrize("count", HIDDEN)
def test_identity_sweep_never_says_single_identity_over_an_unlistable_class(count, caplog):
    classes = [CursorClass(f"P_{b}", [], properties=["project"]) for b in _BASES]
    classes[2] = _hidden("P_CodeFunction", _rows(3, {"project": "Old Name"}), count,
                         properties=["project"])
    with caplog.at_level(logging.INFO, logger="vco_lib.codegraph_vector_copy"):
        summaries = cvc.sweep_stale_identities("P", "P", client=CursorClient(classes), dry_run=True)
    assert sum(s.failures for s in summaries) == 1, "all-zero success over an unread class"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "already single-identity" not in text
    assert "NOT confirmed single-identity" in text and "P_CodeFunction" in text


def test_identity_sweep_single_identity_on_readable_classes(caplog):
    classes = [CursorClass(f"P_{b}", _rows(2, {"project": "P"}), properties=["project"]) for b in _BASES]
    with caplog.at_level(logging.INFO, logger="vco_lib.codegraph_vector_copy"):
        assert cvc.sweep_stale_identities("P", "P", client=CursorClient(classes), dry_run=True) == []
    assert "already single-identity" in "\n".join(r.getMessage() for r in caplog.records)


def test_identity_discovery_reads_every_page():
    rows = [row(i, {"project": "Old" if i == 180 else "P"}) for i in range(1, 201)]
    classes = [CursorClass("P_CodeModule", rows, properties=["project"])]
    assert cvc._distinct_stale_identities(CursorClient(classes), "P", "P") == ["Old"]


@pytest.mark.parametrize("count", HIDDEN)
def test_property_backfill_counts_an_unlistable_class_as_a_failure(count):
    coll = _hidden("P_CodeInteraction", _rows(2, {"project": None}), count, properties=["project"])
    assert cs.backfill_missing_property(coll, "project", "P") == (0, 1)
    assert coll.updated == []


def test_property_backfill_normal_and_empty():
    coll = CursorClass("P_CodeInteraction", [row(1, {"project": None}), row(2, {"project": "P"})],
                       properties=["project"])
    assert cs.backfill_missing_property(coll, "project", "P") == (1, 0)
    assert coll.updated == [(make_uuid(1), {"project": "P"})]
    assert cs.backfill_missing_property(
        CursorClass("P_CodeInteraction", [], properties=["project"]), "project", "P") == (0, 0)


@pytest.mark.parametrize("count", HIDDEN)
def test_slot_scan_gives_no_verdict_on_an_unlistable_class(count):
    client = CursorClient([_hidden("P_CodeFunction", _rows(2), count)])
    assert esf.scan_slot_counts(["P_CodeFunction"], ["qwen3_embed"], client_factory=lambda: client) is None


def test_slot_scan_counts_and_asks_only_for_the_wanted_declared_slot():
    rows = [row(1, {}, vector={"qwen3_embed": [1.0]}), row(2, {}, vector={"qwen3_embed": []}),
            row(3, {}, vector={})]
    coll = CursorClass("P_CodeFunction", rows)
    coll.config = SimpleNamespace(get=lambda: SimpleNamespace(
        properties=[], vector_config={"qwen3_embed": object(), "codesage_embed": object()}))
    client = CursorClient([coll])
    assert esf.scan_slot_counts(["P_CodeFunction", "P_Absent"], ["qwen3_embed"],
                                client_factory=lambda: client) == ({"qwen3_embed": 1}, 3)
    page = coll.listing_pages()[0]
    assert page["include_vector"] == ["qwen3_embed"] and page["return_properties"] == []


def _copy_world(monkeypatch, src):
    from vco_lib import project_init as pi

    class _Batch:
        failed_objects: list = []

        def __init__(self):
            self.added = []

        def dynamic(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add_object(self, **kw):
            self.added.append(kw)

    dst = SimpleNamespace(name="Dst", batch=_Batch())
    client = SimpleNamespace(
        collections=SimpleNamespace(get=lambda n: src if n == src.name else dst),
        close=lambda: None)
    monkeypatch.setattr(pi, "_connect_v4_client", lambda weaviate_url=None: client)
    monkeypatch.setattr(pi, "_fetch_schema", lambda name, weaviate_url=None: {"vectorConfig": {"a": {}}})
    return pi, dst


@pytest.mark.parametrize("count", HIDDEN)
def test_copy_refuses_a_source_that_lists_nothing(count, monkeypatch):
    src = _hidden("Src", [row(1, {"t": 1}, vector={"a": [0.1]})], count)
    pi, dst = _copy_world(monkeypatch, src)
    with pytest.raises(cc.CursorUndeterminable):
        pi._copy_collection_with_vectors("Src", "Dst")
    assert dst.batch.added == []


def test_copy_copies_every_row_and_an_empty_source_copies_zero(monkeypatch):
    rows = [row(i, {"t": i}, vector={"a": [0.1]}) for i in range(1, 231)]
    pi, dst = _copy_world(monkeypatch, CursorClass("Src", rows))
    assert pi._copy_collection_with_vectors("Src", "Dst") == 230
    assert len({str(a["uuid"]) for a in dst.batch.added}) == 230
    pi, dst = _copy_world(monkeypatch, CursorClass("Src", []))
    assert pi._copy_collection_with_vectors("Src", "Dst") == 0


def _edge_6_to_7():
    path = REPO / "migrations" / "codegraph_collection" / "6_to_7.py"
    spec = importlib.util.spec_from_file_location("_cg2_edge_6_to_7", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _edge_main(monkeypatch, capsys, classes):
    edge = _edge_6_to_7()
    client = CursorClient(classes)
    monkeypatch.setattr(edge, "_resolve_codegraph_prefix", lambda: "P")
    monkeypatch.setattr(edge, "_connect", lambda: client)
    capsys.readouterr()
    rc = edge.main()
    return rc, capsys.readouterr()


@pytest.mark.parametrize("count", HIDDEN)
def test_edge_6_to_7_does_not_advance_over_an_unlistable_class(count, monkeypatch, capsys):
    """REGISTER-DESTRUCTIVE: it found 0 rows, printed EDGE_APPLIED, the version
    advanced and the purge never ran again."""
    coll = _hidden("P_CodeModule", [row(1, {"path": ".claude/state/x.py"})], count, properties=["path"])
    rc, cap = _edge_main(monkeypatch, capsys, [coll])
    assert rc == 1 and "EDGE_APPLIED" not in cap.out
    assert coll.deleted == []


def test_edge_6_to_7_purges_and_advances_on_readable_classes(monkeypatch, capsys):
    coll = CursorClass("P_CodeModule", [row(1, {"path": ".claude/state/x.py"}),
                                        row(2, {"path": ".claude/hooks/y.sh"})], properties=["path"])
    rc, cap = _edge_main(monkeypatch, capsys, [coll, CursorClass("P_CodeFunction", [], properties=["file_path"])])
    assert rc == 0 and "EDGE_APPLIED=1" in cap.out
    assert coll.deleted == [make_uuid(1)]


@pytest.fixture(scope="module")
def code_summary():
    scripts = REPO / "templates" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "_cg2_generate_code_summary", str(scripts / "generate-code-summary.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts))
    return mod


@pytest.mark.parametrize("count", HIDDEN)
def test_code_summary_scan_is_failed_not_empty(count, code_summary):
    """An empty scan read as "no live rows" would GC every sidecar entry."""
    coll = _hidden("P_CodeFunction", _rows(2, {"full_name": "m.f"}), count,
                   properties=["full_name", "chunk_num", "function_body"])
    assert code_summary._iter_canonical_rows(CursorClient([coll]), "P", "CodeFunction") is None


def test_code_summary_scan_reads_rows_and_an_empty_class(code_summary):
    coll = CursorClass("P_CodeFunction", [row(1, {"full_name": "m.f", "chunk_num": 0, "function_body": "x"}),
                                          row(2, {"full_name": "m.f", "chunk_num": 1, "function_body": "y"})],
                       properties=["full_name", "chunk_num", "function_body"])
    got = code_summary._iter_canonical_rows(CursorClient([coll]), "P", "CodeFunction")
    assert [r["full_name"] for r in got] == ["m.f"]
    empty = CursorClass("P_CodeClass", [], properties=["full_name", "chunk_num", "class_body"])
    assert code_summary._iter_canonical_rows(CursorClient([empty]), "P", "CodeClass") == []


# ═══════════════════════════════════════════════════════════════════════════
# The incremental gate and the knowledge-graph listings of the same shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("count", HIDDEN)
def test_incremental_gate_does_not_call_an_unlistable_root_never_indexed(count, capsys):
    from vco_lib import codegraph_walk_diagnosis as wd
    assert wd.root_incremental_gate(_hidden("P_CodeModule", _rows(3), count), "/p") is True
    err = capsys.readouterr().err
    assert "could not tell" in err and "has no indexed rows yet" not in err


def test_incremental_gate_reads_one_row_and_knows_a_truly_empty_root(capsys):
    from vco_lib import codegraph_walk_diagnosis as wd
    coll = CursorClass("P_CodeModule", _rows(300))
    assert wd.root_incremental_gate(coll, "/p") is True
    assert len(coll.listing_pages()) == 1 and coll.listing_pages()[0]["limit"] == 1
    assert wd.root_incremental_gate(CursorClass("P_CodeModule", []), "/p") is False
    assert "has no indexed rows yet" in capsys.readouterr().err


@pytest.mark.parametrize("count", HIDDEN)
def test_kg_secondary_backfill_gives_no_verdict_on_an_unlistable_class(count):
    from vco_lib import kg_secondary_backfill as ksb
    client = CursorClient([_hidden("P_KnowledgeGraph", _rows(2, {"content": "x"}), count)])
    assert ksb.missing_rows("P_KnowledgeGraph", "arctic_embed", client_factory=lambda: client) is None


def test_kg_secondary_backfill_lists_missing_rows_reading_only_the_slot():
    from vco_lib import kg_secondary_backfill as ksb
    rows = [row(1, {"file_path": "a.md", "content": "text"}, vector={"arctic_embed": [1.0]}),
            row(2, {"file_path": "b.md", "content": "text"}, vector={}),
            row(3, {"file_path": "c.md", "content": "  "}, vector={})]
    coll = CursorClass("P_KnowledgeGraph", rows)
    coll.config = SimpleNamespace(get=lambda: SimpleNamespace(
        properties=[], vector_config={"arctic_embed": object(), "qwen3_embed": object()}))
    client = CursorClient([coll])
    assert ksb.missing_rows("P_KnowledgeGraph", "arctic_embed",
                            client_factory=lambda: client) == [("b.md", "text")]
    assert coll.listing_pages()[0]["include_vector"] == ["arctic_embed"]
    empty = CursorClient([CursorClass("P_KnowledgeGraph", [])])
    assert ksb.missing_rows("P_KnowledgeGraph", "arctic_embed", client_factory=lambda: empty) == []


@pytest.mark.parametrize("count", HIDDEN)
def test_kg_dedup_listing_raises_instead_of_reading_an_empty_class(count):
    from vco_lib import kg_dedup
    backend = kg_dedup.WeaviateBackend.__new__(kg_dedup.WeaviateBackend)
    backend._client = CursorClient([_hidden("P_KnowledgeGraph", _rows(2, {"file_path": "a.md"}), count)])
    with pytest.raises(cc.CursorUndeterminable):
        backend.fetch_rows("P_KnowledgeGraph")
    backend._client = CursorClient([CursorClass("P_KnowledgeGraph", _rows(120, {"file_path": "a.md"}))])
    assert len(backend.fetch_rows("P_KnowledgeGraph")) == 120


# ═══════════════════════════════════════════════════════════════════════════
# Real order: one whole-repo walk + the sweeps + --prune-stale, on a fixture
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def acg() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_cg2_analyzer", str(ANALYZER))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _walk(acg, tmp_path, make):
    """A project with a.py on disk and rows for a.py + a deleted b.py; the
    walk re-writes a.py's module row only (its old function is gone)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    root = proj.as_posix()
    rev = acg.CODEGRAPH_EMBED_REVISION

    def props(anchor, rel):
        return {"project": "Acme", anchor: rel, "project_source": root,
                "embed_revision": rev, "language": "python"}

    modules = make("Acme_CodeModule", [row(1, props("path", "a.py")), row(2, props("path", "b.py"))])
    functions = make("Acme_CodeFunction", [row(11, props("file_path", "a.py")),
                                           row(12, props("file_path", "b.py"))])
    inst = acg.CodeGraphAnalyzer.__new__(acg.CodeGraphAnalyzer)
    inst.project_name = "Acme"
    inst.client = None
    inst._git_deleted_report = acg._PruneReport()
    inst.index_dot_claude = False
    inst._progress_emitter = None
    inst._prune_git_deleted = lambda r, since: None
    inst.visited_uuids = set()
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    inst.modules_collection, inst.functions_collection = modules, functions
    inst.classes_collection = inst.apis_collection = inst.interactions_collection = None

    def _analyze(f, source_root):
        rel = f.relative_to(source_root).as_posix()
        inst.visited_uuids.add(("Acme_CodeModule", make_uuid(1)))
        inst._prune_walked_paths.add((root, rel))
        inst._reconcile_walked = {(root, rel): {"Acme_CodeModule": {make_uuid(1)}}}
        return {"modules": 1}

    inst._analyze_python_file = _analyze
    import vco_lib.codegraph_source_scope as scope
    return inst, proj, modules, functions, scope


def test_real_order_walk_and_prune_on_readable_classes(acg, tmp_path, monkeypatch):
    """ACT: b.py's rows (deleted file) and a.py's dropped function all go."""
    inst, proj, modules, functions, scope = _walk(acg, tmp_path, CursorClass)
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    stats = inst.analyze_repository(proj, language="python", prune_stale=True)
    assert sorted(modules.deleted) == [make_uuid(2)]
    assert sorted(functions.deleted) == [make_uuid(11), make_uuid(12)]
    assert stats["prune_failures"] == 0 and stats["listing_undeterminable"] == {}
    for coll in (modules, functions):
        assert all(p["after"] is not None for p in coll.listing_pages())
        assert all(p.get("return_properties") is not None for p in coll.listing_pages())


@pytest.mark.parametrize("count", HIDDEN)
def test_real_order_walk_and_prune_over_unlistable_classes(count, acg, tmp_path, monkeypatch, capsys):
    """LEAVE ALONE: the same walk over classes that list nothing deletes
    nothing and ends PARTIAL, naming the classes - never a clean success."""
    inst, proj, modules, functions, scope = _walk(
        acg, tmp_path, lambda n, r: _hidden(n, r, count))
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    stats = inst.analyze_repository(proj, language="python", prune_stale=True)
    assert modules.deleted == [] and functions.deleted == []
    assert stats["prune_failures"] == 2
    assert set(stats["listing_undeterminable"]) == {"Acme_CodeModule", "Acme_CodeFunction"}
    assert stats["prune_failure_collections"] == {}


def test_real_order_walk_and_prune_on_truly_empty_classes(acg, tmp_path, monkeypatch):
    inst, proj, modules, functions, scope = _walk(
        acg, tmp_path, lambda n, r: CursorClass(n, [], properties=list(r[0].properties)))
    monkeypatch.setattr(scope, "project_has_registered_extras", lambda r: False)
    stats = inst.analyze_repository(proj, language="python", prune_stale=True)
    assert stats["prune_failures"] == 0 and stats["listing_undeterminable"] == {}
    assert modules.deleted == [] and functions.deleted == []


# ═══════════════════════════════════════════════════════════════════════════
# No cursorless listing left in the owned files
# ═══════════════════════════════════════════════════════════════════════════

#: Every file CG-2 migrated: the code-graph, copy and sweep code, plus the
#: knowledge-graph listings of the same mechanical shape.
_OWNED = (
    "vco_lib/codegraph_resync.py", "vco_lib/codegraph_prune.py",
    "vco_lib/codegraph_source_purge.py", "vco_lib/codegraph_vector_copy.py",
    "vco_lib/codegraph_schema.py", "vco_lib/embed_slot_fill.py",
    "vco_lib/weaviate_helpers.py", "vco_lib/weaviate_schema.py",
    "vco_lib/staging_swap.py", "vco_lib/project_init.py",
    "vco_lib/codegraph_deleted_files.py", "vco_lib/codegraph_walk_diagnosis.py",
    # knowledge-graph / migration listings of the same shape, migrated with it
    "vco_lib/collection_rename.py", "vco_lib/kg_dedup.py",
    "vco_lib/kg_secondary_backfill.py", "vco_lib/embedding_enrichment.py",
    "claude_mcp_servers/weaviate_mcp/server.py",
    "claude_mcp_servers/scripts/migrate_to_new_embeddings.py",
    "templates/scripts/analyze_code_graph.py",
    "templates/scripts/generate-code-summary.py",
) + tuple(
    p.relative_to(REPO).as_posix()
    for p in sorted((REPO / "migrations" / "codegraph_collection").glob("*.py"))
)

_ITERATOR_CALL = re.compile(r"\.iterator\(")
_FETCH_CALL = re.compile(r"fetch_objects\(")
_REST_LIST = re.compile(r"/v1/objects\?class=")


def _call_text(src: str, start: int) -> str:
    """The text of the call whose ``(`` follows ``start`` (balanced)."""
    depth, i = 0, src.index("(", start)
    for j in range(i, len(src)):
        depth += {"(": 1, ")": -1}.get(src[j], 0)
        if depth == 0:
            return src[i:j + 1]
    return src[i:]


def _code_lines(src: str) -> str:
    """Source with comment-only lines blanked (positions kept)."""
    return "\n".join("" if ln.lstrip().startswith("#") else ln for ln in src.split("\n"))


def test_no_cursorless_listing_remains_in_the_owned_files():
    offenders = []
    for rel in _OWNED:
        src = _code_lines((REPO / rel).read_text(encoding="utf-8"))
        for m in _ITERATOR_CALL.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            text = src.split("\n")[line - 1]
            if "``" in text or text.lstrip().startswith(('"', "'")):
                continue  # prose in a docstring
            offenders.append(f"{rel}:{line} iterator(")
        for m in _FETCH_CALL.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            if "``" in src.split("\n")[line - 1]:
                continue
            call = _call_text(src, m.start())
            # A read that names a filter is a lookup, not a class listing; the
            # cursor itself names ``after``.
            if "filters" not in call and "after" not in call and "**" not in call:
                offenders.append(f"{rel}:{line} fetch_objects without filters/after")
        for m in _REST_LIST.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{line} REST object list")
    assert offenders == [], "cursorless listing outside vco_lib.codegraph_cursor:\n" + "\n".join(offenders)


def test_the_cursor_module_is_the_only_place_that_pages():
    src = (REPO / "vco_lib" / "codegraph_cursor.py").read_text(encoding="utf-8")
    assert src.count("fetch_objects(limit=limit, after=after") == 1
    assert "after: Any = NIL_UUID" in src
