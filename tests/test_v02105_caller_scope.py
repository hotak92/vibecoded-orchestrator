# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 #512C — caller lists are resolved inside the callee's own repo root.

The field report: a project whose code graph ALSO holds an extra code folder's
mirror rows listed the mirror's functions as callers. The injected context for
``LinearAccelModel::combine`` named a WinForms tool's methods, and callers were
matched on the bare leaf name across the whole collection.

Both caller surfaces (the MCP's ``query_code_structure('callers', …)`` and the
CLI ``structure callers`` — interactive AND ``--hook-format``, which is what
the injection hooks run) must:

1. list only callers whose ``project_source`` is the callee's own root,
2. list a QUALIFIED match before a bare-name-only one,
3. leave a legacy row (no ``project_source`` anywhere) exactly as it was.

The fakes below model the Weaviate behaviours that matter here: word-tokenized
``Equal`` on TEXT, ``contains_any`` on the ``call_names`` array, AND-ed
filters, and a cross-reference resolved into ``.objects``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"

for _p in (str(SCRIPTS_DIR), str(REPO_ROOT / "claude_mcp_servers"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _tokens(value) -> set:
    return {t for t in re.split(r"[^A-Za-z0-9_]+", str(value or "")) if t}


class _Row:
    def __init__(self, uuid: str, **props):
        self.uuid = uuid
        self.properties = props
        self.references: object = None


class _Ref:
    """``_CrossReference`` stand-in: the resolved targets live on ``.objects``."""

    def __init__(self, objects):
        self.objects = objects


class _Filter:
    _prop: str = ""

    def __init__(self):
        self.parts = []

    @classmethod
    def by_property(cls, prop):
        f = cls()
        f._prop = prop
        return f

    def contains_any(self, terms):
        self.parts.append((self._prop, "contains_any", tuple(terms)))
        return self

    def equal(self, value):
        self.parts.append((self._prop, "equal", value))
        return self

    def __and__(self, other):
        f = _Filter()
        f.parts = list(self.parts) + list(other.parts)
        return f


def _row_matches(flt, props) -> bool:
    if flt is None:
        return True
    for prop, op, value in flt.parts:
        actual = props.get(prop)
        if op == "contains_any":
            if isinstance(actual, list):
                if not (set(actual) & set(value)):
                    return False
            elif not any(str(t) in _tokens(actual) for t in value):
                return False
        else:  # word-tokenized Equal, like Weaviate's TEXT default
            if not _tokens(value) <= _tokens(actual):
                return False
    return True


class _Query:
    def __init__(self, coll):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=100, return_references=None,
                      return_properties=None, **_kw):
        if (return_properties is not None
                and "project_source" in return_properties
                and self._coll.legacy_schema):
            raise Exception(
                "no such prop: project_source (legacy schema variant)")
        # fix-round M5: record the caller-scan fetches so tests can assert the
        # over-fetch is paid ONLY when the page proves the collection mixed.
        self._coll.fetch_log.append((filters, limit))
        hits = [r for r in self._coll.rows if _row_matches(filters, r.properties)]
        out = []
        for row in hits[:limit]:
            copy = _Row(row.uuid, **dict(row.properties))
            link = getattr(return_references, "link_on", "")
            if link == "calls":
                targets = self._coll.edges.get(row.uuid) or []
                copy.references = (
                    {"calls": _Ref([_Row(t, full_name=t) for t in targets])}
                    if targets else {}
                )
            out.append(copy)
        return SimpleNamespace(objects=out)


class _Coll:
    def __init__(self, rows, edges=None, legacy_schema=False):
        self.rows = rows
        self.edges = edges or {}
        self.legacy_schema = legacy_schema
        self.fetch_log = []
        self.query = _Query(self)


class _Client:
    def __init__(self, coll):
        self.collections = SimpleNamespace(get=lambda _name: coll)

    def close(self):
        pass


# ── fixtures ────────────────────────────────────────────────────────────────
# Two source roots: the project's own ("/repo/proj") and an extra code folder
# ("/repo/mirror"). BOTH define a function named `combine` (distinct qualified
# names, like the field report's model method vs the WinForms tool).
_PROJ_ROOT = "/repo/proj"
_MIRROR_ROOT = "/repo/mirror"


def _two_root_rows():
    return [
        _Row("t-proj", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
        _Row("t-mirror", full_name="mirror.form.Form1.combine", name="combine",
             project_source=_MIRROR_ROOT, file_path="mirror/form.py",
             start_line=5, language="python", call_names=[]),
        # a caller in the project's own root
        _Row("c-proj", full_name="pkg.model.Model.run", name="run",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=30, language="python", call_names=["combine"]),
        # the MIRROR's caller of ITS OWN same-named function
        _Row("c-mirror", full_name="mirror.form.Form1.convert", name="convert",
             project_source=_MIRROR_ROOT, file_path="mirror/form.py",
             start_line=40, language="python", call_names=["combine"]),
    ]


def _mcp_callers_payload(monkeypatch, coll, target: str) -> dict:
    srv = pytest.importorskip("claude_mcp_servers.weaviate_mcp.server")
    client = _Client(coll)
    with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
         mock.patch.object(srv, "Filter", _Filter), \
         mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
        raw = srv.query_code_structure("callers", target, project="")
    payload = json.loads(raw)
    assert payload.get("success"), payload
    return payload


def _mcp_callers(monkeypatch, coll, target: str) -> list:
    return [r["full_name"] for r in _mcp_callers_payload(
        monkeypatch, coll, target)["results"]]


def _caller_fetch_limits(coll) -> list:
    """The limits of the CALLER-scan fetches (the callee identity fetch
    filters on ``full_name``, not ``call_names``)."""
    out = []
    for flt, limit in coll.fetch_log:
        props = {p for p, _op, _v in getattr(flt, "parts", [])}
        if "call_names" in props:
            out.append(limit)
    return out


def _cli_hook_row(monkeypatch, capsys, coll, target: str) -> str:
    qcg = pytest.importorskip("query_code_graph")
    monkeypatch.setattr(qcg, "Filter", _Filter)
    q = qcg.CodeGraphQuery(project=None)
    q.client = _Client(coll)
    q.query_structure("callers", target, hook_format=True,
                      indexed_revision=False, source_file=None,
                      exclude_file=None)
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith("CODE: ")]
    assert len(rows) == 1, rows
    return rows[0]


# ── 1. the root rule ────────────────────────────────────────────────────────


def test_only_same_root_callers_are_listed(monkeypatch) -> None:
    """THE ACT. Pre-fix BOTH callers were listed: `call_names` matching is
    name-only and every row of the collection was a candidate."""
    coll = _Coll(_two_root_rows())
    assert _mcp_callers(monkeypatch, coll, "pkg.model.Model.combine") == [
        "pkg.model.Model.run"
    ]


def test_the_mirror_callee_keeps_its_own_caller(monkeypatch) -> None:
    """LEAVE-ALONE counterpart: asking for the MIRROR's function lists the
    mirror's caller — the rule is per callee root, not "project root only"."""
    coll = _Coll(_two_root_rows())
    assert _mcp_callers(
        monkeypatch, coll, "mirror.form.Form1.combine") == [
        "mirror.form.Form1.convert"
    ]


def test_hook_format_injection_drops_the_mirror_caller(monkeypatch, capsys) -> None:
    """The INJECTION surface (what the model actually sees)."""
    coll = _Coll(_two_root_rows())
    row = _cli_hook_row(monkeypatch, capsys, coll, "pkg.model.Model.combine")
    assert "pkg.model.Model.run@pkg/model.py:30" in row
    assert "mirror.form.Form1.convert" not in row


# ── 2. qualified name beats a bare-name match ───────────────────────────────


def _qualified_rows():
    return [
        _Row("t1", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
        # BARE-name match, listed FIRST in the store (pre-fix order wins)
        _Row("c-bare", full_name="pkg.util.Thing.go", name="go",
             project_source=_PROJ_ROOT, file_path="pkg/util.py",
             start_line=3, language="python", call_names=["combine"]),
        # QUALIFIED match: same class/module scope as the callee
        _Row("c-qualified", full_name="pkg.model.Model.run", name="run",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=30, language="python", call_names=["combine"]),
    ]


def test_qualified_match_is_listed_before_a_bare_name_match(monkeypatch) -> None:
    """Pre-fix the store order won: `pkg.util.Thing.go` came first."""
    names = _mcp_callers(monkeypatch, _Coll(_qualified_rows()),
                         "pkg.model.Model.combine")
    assert names == ["pkg.model.Model.run", "pkg.util.Thing.go"]


def test_hook_format_lists_the_qualified_match_first(monkeypatch, capsys) -> None:
    """The ≤5-item hook cap must keep the REAL caller, not the stranger."""
    row = _cli_hook_row(monkeypatch, capsys, _Coll(_qualified_rows()),
                        "pkg.model.Model.combine")
    assert row.index("pkg.model.Model.run@") < row.index("pkg.util.Thing.go@")


def test_a_resolved_call_edge_is_a_qualified_match(monkeypatch, capsys) -> None:
    """An edge-confirmed caller (uuid-precise) also outranks a name-only one,
    even from another module."""
    rows = [
        _Row("t1", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
        _Row("c-name-only", full_name="pkg.util.Thing.go", name="go",
             project_source=_PROJ_ROOT, file_path="pkg/util.py",
             start_line=30, language="python", call_names=["combine"]),
        _Row("c-edge", full_name="pkg.other.Worker.step", name="step",
             project_source=_PROJ_ROOT, file_path="pkg/other.py",
             start_line=50, language="python", call_names=["combine"]),
    ]
    row = _cli_hook_row(monkeypatch, capsys,
                        _Coll(rows, edges={"c-edge": ["t1"]}),
                        "pkg.model.Model.combine")
    assert row.index("pkg.other.Worker.step@") < row.index(
        "pkg.util.Thing.go@")


# ── 3. legacy rows are left alone ───────────────────────────────────────────


def _legacy_rows():
    return [
        _Row("lt", full_name="legacy.thing.target", name="target",
             file_path="thing.py", start_line=1, language="python",
             call_names=[]),
        _Row("lc", full_name="legacy.thing.caller", name="caller",
             file_path="thing.py", start_line=9, language="python",
             call_names=["target"]),
    ]


def test_legacy_rows_without_project_source_are_still_listed(monkeypatch) -> None:
    """A project that never stamped `project_source` (pre-v0.2.47 rows, no
    extra code folders) keeps the exact pre-v0.2.105 answer."""
    assert _mcp_callers(monkeypatch, _Coll(_legacy_rows()),
                        "legacy.thing.target") == ["legacy.thing.caller"]


def test_legacy_schema_without_the_property_is_left_alone(monkeypatch) -> None:
    """A collection that ERRORS on `return_properties=['project_source']`
    must not break `callers`: the read falls back and filters nothing."""
    coll = _Coll(_legacy_rows(), legacy_schema=True)
    assert _mcp_callers(monkeypatch, coll, "legacy.thing.target") == [
        "legacy.thing.caller"
    ]


def test_cli_legacy_rows_are_still_listed(monkeypatch, capsys) -> None:
    row = _cli_hook_row(monkeypatch, capsys, _Coll(_legacy_rows()),
                        "legacy.thing.target")
    assert "legacy.thing.caller@thing.py:9" in row


# ── 4. the WRITE side: call edges resolve inside the caller's root ──────────

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_v02105_caller_analyzer", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _resolver(analyzer_mod):
    """An analyzer with the two-root function cache the scan would build."""
    a = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    a.function_cache = {
        "pkg.Model.combine": "t-proj",
        "mirror.Form1.combine": "t-mirror",
    }
    a._xref_source = {"t-proj": _PROJ_ROOT, "t-mirror": _MIRROR_ROOT}
    index = analyzer_mod._build_short_name_index(a.function_cache)
    return a, index


def test_a_bare_call_name_resolves_inside_the_callers_own_root(analyzer_mod) -> None:
    """Pre-fix the mirror caller's `combine()` resolved to `candidates[0]`
    (whichever same-named function the cache happened to list first) — the
    wrong tree's function — so the stored edge (and `n_callers`) was wrong."""
    a, index = _resolver(analyzer_mod)
    assert a._resolve_call_target(
        "combine", "mirror.other.Worker.step", index, _MIRROR_ROOT,
    ) == "t-mirror"
    assert a._resolve_call_target(
        "combine", "pkg.other.Worker.step", index, _PROJ_ROOT,
    ) == "t-proj"


def test_the_same_module_preference_still_wins_inside_the_root(analyzer_mod) -> None:
    a, index = _resolver(analyzer_mod)
    assert a._resolve_call_target(
        "combine", "mirror.Form1.convert", index, _MIRROR_ROOT,
    ) == "t-mirror"


def test_unknown_caller_root_keeps_the_pre_fix_answer(analyzer_mod) -> None:
    """LEAVE-ALONE: no stamp anywhere (legacy rows) → no narrowing."""
    a, index = _resolver(analyzer_mod)
    assert a._resolve_call_target("combine", "x.y", index) == "t-proj"


def test_a_root_with_no_candidate_keeps_the_edge(analyzer_mod) -> None:
    """LEAVE-ALONE: narrowing must never LOSE a real edge — a caller root no
    candidate carries falls back to the full candidate list."""
    a, index = _resolver(analyzer_mod)
    assert a._resolve_call_target(
        "combine", "x.y", index, "/repo/third",
    ) == "t-proj"


# ── 5. fix round: mixed graphs keep legacy callers (M4); the over-fetch is
# paid only by mixed graphs and the answer keeps its true cap (M5) ──────────


def _mixed_graph_rows():
    """A MIXED graph: the callee's file was re-walked (stamped), its caller
    lives in an untouched legacy file (no stamp), and the mirror's caller of
    its own same-named function is present too."""
    return [
        _Row("t-proj", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
        # a caller in a legacy UNSTAMPED file (pre-v0.2.47 row, never re-walked)
        _Row("c-legacy", full_name="legacy.thing.caller", name="caller",
             file_path="legacy/thing.py", start_line=9, language="python",
             call_names=["combine"]),
        # the MIRROR's caller of ITS OWN same-named function — still dropped
        _Row("c-mirror", full_name="mirror.form.Form1.convert", name="convert",
             project_source=_MIRROR_ROOT, file_path="mirror/form.py",
             start_line=40, language="python", call_names=["combine"]),
    ]


def test_mixed_graph_keeps_unstamped_legacy_callers(monkeypatch) -> None:
    """ACT (fix M4): a stamped callee whose real caller row predates source
    stamping must not lose it — an absent stamp is not evidence of another
    root. Pre-fix the exact-``in``-roots check dropped the unstamped caller."""
    names = _mcp_callers(monkeypatch, _Coll(_mixed_graph_rows()),
                         "pkg.model.Model.combine")
    assert "legacy.thing.caller" in names
    # LEAVE-ALONE does not extend to STAMPED foreign rows (the original bug)
    assert "mirror.form.Form1.convert" not in names


def test_cli_hook_mixed_graph_keeps_unstamped_legacy_caller(
        monkeypatch, capsys) -> None:
    """The same rule on the INJECTION surface."""
    row = _cli_hook_row(monkeypatch, capsys, _Coll(_mixed_graph_rows()),
                        "pkg.model.Model.combine")
    assert "legacy.thing.caller@legacy/thing.py:9" in row
    assert "mirror.form.Form1.convert" not in row


def test_path_spelling_differences_do_not_drop_the_caller(monkeypatch) -> None:
    """ACT (fix M4): a caller stamped with a different SPELLING of the same
    root (``/repo/./proj/`` — a ``.``/trailing-slash form) is the same root.
    Pre-fix the exact stored-string compare dropped it."""
    rows = [
        _Row("t1", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
        _Row("c1", full_name="pkg.model.Model.run", name="run",
             project_source="/repo/./proj/", file_path="pkg/model.py",
             start_line=30, language="python", call_names=["combine"]),
        _Row("c2", full_name="mirror.form.Form1.convert", name="convert",
             project_source=_MIRROR_ROOT, file_path="mirror/form.py",
             start_line=40, language="python", call_names=["combine"]),
    ]
    assert _mcp_callers(monkeypatch, _Coll(rows),
                        "pkg.model.Model.combine") == ["pkg.model.Model.run"]


def test_windows_drive_letter_case_is_one_root(monkeypatch) -> None:
    """Windows case-fold: ``C:/Projects/App`` and ``c:/projects/app`` are one
    root; a genuinely different drive is still another codebase."""
    srv = pytest.importorskip("claude_mcp_servers.weaviate_mcp.server")
    monkeypatch.setattr(srv, "_CASEFOLD_PROJECT_SOURCES", True)
    rows = [
        _Row("wt", full_name="app.model.Model.combine", name="combine",
             project_source="C:/Projects/App", file_path="app/model.py",
             start_line=10, language="python", call_names=[]),
        _Row("wc", full_name="app.model.Model.run", name="run",
             project_source="c:/projects/app", file_path="app/model.py",
             start_line=30, language="python", call_names=["combine"]),
        _Row("wo", full_name="tool.form.Form1.convert", name="convert",
             project_source="D:/Other/tool", file_path="tool/form.py",
             start_line=40, language="python", call_names=["combine"]),
    ]
    assert _mcp_callers(monkeypatch, _Coll(rows),
                        "app.model.Model.combine") == ["app.model.Model.run"]


def _single_root_many_callers(n: int = 60) -> list:
    rows = [
        _Row("t1", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
    ]
    for i in range(n):
        rows.append(_Row(
            f"c{i}", full_name=f"pkg.callers.fn{i}", name=f"fn{i}",
            project_source=_PROJ_ROOT, file_path="pkg/callers.py",
            start_line=i, language="python", call_names=["combine"]))
    return rows


def _mixed_graph_capped_rows(mirror: int = 40, own: int = 30) -> list:
    """A mixed graph whose base-limit caller page fills with MIRROR rows
    before the project's own callers (the mirror rows are listed first, as
    store order would have it)."""
    rows = [
        _Row("t1", full_name="pkg.model.Model.combine", name="combine",
             project_source=_PROJ_ROOT, file_path="pkg/model.py",
             start_line=10, language="python", call_names=[]),
    ]
    for i in range(mirror):
        rows.append(_Row(
            f"m{i}", full_name=f"mirror.form.Form1.convert{i}",
            name=f"convert{i}", project_source=_MIRROR_ROOT,
            file_path="mirror/form.py", start_line=i, language="python",
            call_names=["combine"]))
    for i in range(own):
        rows.append(_Row(
            f"p{i}", full_name=f"pkg.model.Model.run{i}", name=f"run{i}",
            project_source=_PROJ_ROOT, file_path="pkg/model.py",
            start_line=100 + i, language="python", call_names=["combine"]))
    return rows


def test_single_root_graph_keeps_the_base_fetch_and_answer_cap(
        monkeypatch) -> None:
    """M5 ACT-side: a graph with ONE root never pays the ×10 over-fetch (its
    caller page cannot contain out-of-scope rows) and the reported limit is
    the TRUE answer cap, not a scan width. Pre-fix the fetch was 500 rows
    and the payload reported ``limit: 500``."""
    coll = _Coll(_single_root_many_callers())
    payload = _mcp_callers_payload(monkeypatch, coll,
                                   "pkg.model.Model.combine")
    assert _caller_fetch_limits(coll) == [50]
    assert len(payload["results"]) == 50
    assert payload["limit"] == 50
    assert payload["truncated"] is True


def test_mixed_graph_escalates_then_recaps_the_answer(monkeypatch) -> None:
    """M5: the over-fetch IS owed when the page proves the collection mixed
    (full page + out-of-scope rows), the ANSWER is re-capped at 50 after
    filtering, and the payload reports the true cap."""
    coll = _Coll(_mixed_graph_capped_rows())
    payload = _mcp_callers_payload(monkeypatch, coll,
                                   "pkg.model.Model.combine")
    assert _caller_fetch_limits(coll) == [50, 500]
    assert len(payload["results"]) == 30      # only the project's callers
    assert payload["limit"] == 50             # the TRUE answer cap
    assert payload["truncated"] is False


def test_cli_hook_single_root_never_escalates(monkeypatch, capsys) -> None:
    """The latency-budgeted injection path pays the base fetch only."""
    coll = _Coll(_single_root_many_callers())
    _cli_hook_row(monkeypatch, capsys, coll, "pkg.model.Model.combine")
    assert _caller_fetch_limits(coll) == [50]


def test_cli_hook_escalates_only_when_the_page_proves_mixed(
        monkeypatch, capsys) -> None:
    coll = _Coll(_mixed_graph_capped_rows())
    row = _cli_hook_row(monkeypatch, capsys, coll,
                        "pkg.model.Model.combine")
    assert _caller_fetch_limits(coll) == [50, 500]
    assert "mirror.form.Form1.convert" not in row