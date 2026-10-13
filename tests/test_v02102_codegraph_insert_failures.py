# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-U (ship-gate finding #1): a persistently failing insert no longer
re-walks on every update.

Before: ``walk_certifies_generation`` refused the stamp on ANY insert error, so a
project with one row Weaviate rejects every time (a 422 on an oversized row)
stayed owed forever and re-spawned a full force-rewalk on every bundle update,
silently. Now the first sighting is held back as ``pending_failing_set`` (the
project stays owed); the SAME set on the next forced walk is accepted and the
stamp written beside ``accepted_failing_set``; the doctor names the files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import codegraph_extractor_generation as ceg
from vco_lib import doctor


class _StatusError(Exception):
    """Shape of weaviate's ``UnexpectedStatusCodeError``."""

    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.status_code = status_code


class _DedupInsertError(RuntimeError):
    """Shape of the analyzer's ``_DedupInsertError`` (duck typed by the module)."""

    def __init__(self, original: BaseException, collection_name: str, uuid: str) -> None:
        super().__init__(f"_dedup_insert failed for {collection_name} (uuid={uuid}): {original}")
        self.original = original
        self.collection_name = collection_name
        self.uuid = uuid


def _err(file_tag: str, *, text: str = "request id 1234 took 3.2s", code: int = 422):
    return _DedupInsertError(_StatusError(text, code), "Acme_CodeFunction",
                             f"00000000-0000-0000-0000-{file_tag:0>12}")


def _rows(*tags: str, text: str = "x"):
    return [ceg.insert_failure_row(f"src/{t}.py", _err(t, text=text)) for t in tags]


def _walk(root: Path, rows, *, errors=None, lines=None):
    out = lines if lines is not None else []
    return ceg.stamp_after_walk(
        root, force_rewalk=True, files_analyzed=40, project="Acme",
        insert_errors=len(rows) if errors is None else errors,
        failing_rows=rows, emit=out.append,
    )


def _stamp(root: Path) -> dict:
    return json.loads(ceg.stamp_path(root).read_text(encoding="utf-8"))


# ── pure helpers ──────────────────────────────────────────────────────────


def test_reason_is_type_and_status_never_the_free_text():
    a = ceg.insert_failure_reason(_err("1", text="request id 1 took 2s"))
    b = ceg.insert_failure_reason(_err("1", text="request id 9 took 7s"))
    assert a == b == "_StatusError:422"
    assert ceg.insert_failure_reason(ValueError("boom")) == "ValueError"


def test_row_carries_file_collection_uuid_and_reason_through_a_chain():
    try:
        try:
            raise _err("7")
        except _DedupInsertError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        row = ceg.insert_failure_row(Path("src") / "m.py", outer)
    assert row == {"file": "src/m.py", "collection": "Acme_CodeFunction",
                   "uuid": "00000000-0000-0000-0000-000000000007",
                   "reason": "_StatusError:422"}


def test_fingerprint_is_order_independent_and_set_sensitive():
    assert ceg.failing_set_fingerprint(_rows("a", "b")) == \
        ceg.failing_set_fingerprint(_rows("b", "a"))
    assert ceg.failing_set_fingerprint(_rows("a")) != ceg.failing_set_fingerprint(_rows("b"))


def test_verdict_rules():
    rows = _rows("a", "b")
    fp = ceg.failing_set_fingerprint(rows)
    gen = ceg.CURRENT_EXTRACTOR_GENERATION
    v = ceg.failing_set_verdict(rows, None, gen)
    assert (v.stable, v.reason) == (False, ceg.FAILING_SET_FIRST_SIGHTING)
    v = ceg.failing_set_verdict(rows, {"fingerprint": fp, "generation": gen}, gen)
    assert (v.stable, v.reason) == (True, ceg.FAILING_SET_STABLE)
    v = ceg.failing_set_verdict(rows, {"fingerprint": fp, "generation": "0.0.1"}, gen)
    assert (v.stable, v.reason) == (False, ceg.FAILING_SET_CHANGED), "other generation"
    v = ceg.failing_set_verdict(rows, {"fingerprint": fp, "generation": gen}, gen,
                                insert_errors=3)
    assert v.reason == ceg.FAILING_SET_UNATTRIBUTED, "more errors than named rows"
    bare = [ceg.insert_failure_row("src/x.py", ValueError("no attribution"))]
    v = ceg.failing_set_verdict(bare, {"fingerprint": ceg.failing_set_fingerprint(bare),
                                       "generation": gen}, gen)
    assert v.reason == ceg.FAILING_SET_UNATTRIBUTED


def test_certify_accepts_insert_errors_only_with_a_stable_set():
    kw = dict(insert_errors=2, files_analyzed=10)
    assert ceg.walk_certifies_generation(True, **kw) is False
    assert ceg.walk_certifies_generation(True, failing_set_stable=True, **kw) is True
    assert ceg.walk_certifies_generation(True, language="py", failing_set_stable=True,
                                         **kw) is False


# ── the stamp state machine ──────────────────────────────────────────────


def test_one_failing_walk_is_not_stamped_and_records_pending(tmp_path):
    ceg.write_stamp(tmp_path, "0.2.92")  # an older certification
    lines: list = []
    assert _walk(tmp_path, _rows("a"), lines=lines) is None
    st = _stamp(tmp_path)
    assert st["generation"] == "0.2.92", "old generation kept → the project stays owed"
    assert ceg.decide(prev_version="0.2.101", running_version="0.2.102",
                      stamp_generation=ceg.read_stamp_generation(tmp_path),
                      graph_exists=True).needs_reindex is True
    pend = st[ceg.PENDING_FAILING_SET_KEY]
    assert pend["count"] == 1 and pend["project"] == "Acme"
    assert pend["rows"][0]["file"] == "src/a.py"
    assert ceg.ACCEPTED_FAILING_SET_KEY not in st
    assert any("held back" in ln for ln in lines)


def test_same_set_twice_is_stamped_logged_and_reported_by_the_doctor(tmp_path):
    first: list = []
    assert _walk(tmp_path, _rows("a", "b"), lines=first) is None
    second: list = []
    # Different free text on the second walk: the reason must still match.
    got = _walk(tmp_path, _rows("b", "a", text="another request id"), lines=second)
    assert got == ceg.CURRENT_EXTRACTOR_GENERATION
    st = _stamp(tmp_path)
    assert st["generation"] == ceg.CURRENT_EXTRACTOR_GENERATION
    assert ceg.PENDING_FAILING_SET_KEY not in st
    acc = st[ceg.ACCEPTED_FAILING_SET_KEY]
    assert acc["count"] == 2 and acc["project"] == "Acme" and acc["first_seen"]
    # resync log: each file + collection + reason, and the verdict
    joined = "\n".join(second)
    for f in ("src/a.py", "src/b.py"):
        assert f"{f} [Acme_CodeFunction] _StatusError:422" in joined
    assert "recorded" in joined and "accepted" in joined
    # stops being owed
    assert ceg.decide(prev_version="0.2.101", running_version="0.2.102",
                      stamp_generation=ceg.read_stamp_generation(tmp_path),
                      graph_exists=True).needs_reindex is False
    # the doctor finding (the shim in vco_lib.doctor, full scope only)
    fn, scopes = doctor.PROBES["codegraph_insert_failures"]
    assert scopes == (doctor.SCOPE_FULL,)
    findings = ceg.doctor_insert_failures(tmp_path, roots=[tmp_path])
    assert len(findings) == 1 and findings[0].is_problem
    cmd = findings[0].command
    assert "src/a.py" in cmd and "src/b.py" in cmd
    assert f".claude/scripts/code-graph-analyze {tmp_path} --project Acme --force-rewalk" in cmd
    # a clean walk rewrites the stamp without the record, and the doctor clears
    assert _walk(tmp_path, []) == ceg.CURRENT_EXTRACTOR_GENERATION
    assert ceg.ACCEPTED_FAILING_SET_KEY not in _stamp(tmp_path)
    assert not ceg.doctor_insert_failures(tmp_path, roots=[tmp_path])[0].is_problem


def test_first_seen_survives_acceptance(tmp_path):
    """The confirming walk must not restart the clock: ``first_seen`` is
    carried over from the pending record."""
    _walk(tmp_path, _rows("a"))
    seen = _stamp(tmp_path)[ceg.PENDING_FAILING_SET_KEY]["first_seen"]
    raw = _stamp(tmp_path)
    raw[ceg.PENDING_FAILING_SET_KEY]["first_seen"] = "2020-01-01T00:00:00Z"
    ceg.stamp_path(tmp_path).write_text(json.dumps(raw), encoding="utf-8")
    _walk(tmp_path, _rows("a"))
    assert seen and _stamp(tmp_path)[ceg.ACCEPTED_FAILING_SET_KEY]["first_seen"] == \
        "2020-01-01T00:00:00Z"


def test_a_different_set_is_not_stamped(tmp_path):
    assert _walk(tmp_path, _rows("a")) is None
    assert _walk(tmp_path, _rows("b")) is None
    st = _stamp(tmp_path)
    assert "generation" not in st
    assert st[ceg.PENDING_FAILING_SET_KEY]["rows"][0]["file"] == "src/b.py"
    # and a set that changes AFTER acceptance voids that acceptance
    assert _walk(tmp_path, _rows("b")) == ceg.CURRENT_EXTRACTOR_GENERATION
    assert _walk(tmp_path, _rows("c")) is None
    st = _stamp(tmp_path)
    assert "generation" not in st and ceg.ACCEPTED_FAILING_SET_KEY not in st


def test_unattributed_errors_are_never_stamped(tmp_path):
    bare = [ceg.insert_failure_row("src/x.py", ValueError("no attribution"))]
    for _ in range(3):
        lines: list = []
        assert _walk(tmp_path, bare, lines=lines) is None
        assert any("could not all be attributed" in ln for ln in lines)
    assert not ceg.stamp_path(tmp_path).exists()
    # a count the rows cannot account for is unattributed too
    for _ in range(3):
        assert _walk(tmp_path, _rows("a"), errors=2) is None
    assert ceg.read_stamp_generation(tmp_path) is None


def test_the_log_is_capped(tmp_path):
    lines: list = []
    tags = [str(i) for i in range(25)]
    _walk(tmp_path, _rows(*tags), lines=lines)
    named = [ln for ln in lines if ln.lstrip().startswith("- src/")]
    assert len(named) == ceg.FAILING_ROWS_LOG_CAP
    assert any("… and 5 more" in ln for ln in lines)


def test_doctor_is_ok_without_an_accepted_set(tmp_path):
    _walk(tmp_path, _rows("a"))  # pending only
    f = ceg.doctor_insert_failures(tmp_path, roots=[tmp_path])
    assert len(f) == 1 and f[0].status == doctor.STATUS_OK


_ANALYZER_PATH = Path(__file__).resolve().parents[1] / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_w6u_analyze_code_graph", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _walk_failing(analyzer_mod, tmp_path, exc_factory):
    """Drive the REAL analyze_repository dispatch loop with one file whose
    walker raises (the harness of test_codegraph_module_row_invalidation_v0273)."""
    (tmp_path / "boom.py").write_text("def f():\n    return 1\n")
    analyzer = analyzer_mod.CodeGraphAnalyzer("TestProj")
    analyzer._invalidate_module_row = lambda f, root: None
    analyzer._find_python_files = lambda root: [tmp_path / "boom.py"]

    def _boom(f, root):
        raise exc_factory()

    analyzer._analyze_python_file = _boom
    return analyzer.analyze_repository(tmp_path, language="python")


def test_dedup_branch_records_an_attributed_row(analyzer_mod, tmp_path):
    stats = _walk_failing(analyzer_mod, tmp_path, lambda: analyzer_mod._DedupInsertError(
        _StatusError("too big", 422), "TestProj_CodeFunction", "uuid-1"))
    assert stats["insert_errors"] == 1
    assert stats["insert_error_rows"] == [{
        "file": "boom.py", "collection": "TestProj_CodeFunction",
        "uuid": "uuid-1", "reason": "_StatusError:422"}]


def test_chained_dedup_branch_records_an_attributed_row(analyzer_mod, tmp_path):
    def _mk():
        try:
            raise analyzer_mod._DedupInsertError(_StatusError("x", 500),
                                                 "TestProj_CodeClass", "uuid-2")
        except Exception as inner:  # noqa: BLE001
            try:
                raise RuntimeError("re-raised") from inner
            except RuntimeError as outer:
                return outer

    stats = _walk_failing(analyzer_mod, tmp_path, _mk)
    assert stats["insert_errors"] == 1
    assert stats["insert_error_rows"][0]["collection"] == "TestProj_CodeClass"
    assert stats["insert_error_rows"][0]["uuid"] == "uuid-2"


def test_a_non_insert_failure_records_no_row(analyzer_mod, tmp_path):
    stats = _walk_failing(analyzer_mod, tmp_path, lambda: RuntimeError("parse"))
    assert stats["insert_errors"] == 0 and not stats.get("insert_error_rows")


def test_main_passes_the_rows_and_project_to_the_stamp():
    """The ``main()`` tail is not drivable without a live Weaviate; pin the one
    call's keyword arguments (red-proved by mutating them away)."""
    import ast

    tree = ast.parse(_ANALYZER_PATH.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "stamp_after_walk"]
    assert len(calls) == 1
    kws = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kws.get("project") == "project_name"
    assert kws.get("failing_rows") == "stats.get('insert_error_rows')"
