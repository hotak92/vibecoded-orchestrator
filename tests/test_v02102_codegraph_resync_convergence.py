# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W1-C — code-graph resync convergence.

S16-3  The minified heuristic judged ONE line: a hand-written 3,500-line Rust
       file with one 2,007-character (2,021-byte) SQL line was skipped as "minified", and the
       skip left the file's earlier rows behind forever (never re-embedded,
       never pruned → the resync census reported them owed on every walk).
       Now: a shape rule (median line length, or the share of characters in
       long lines) AND a policy skip prunes the skipped file's rows.
P299-B5 The hash-skip decision ignored the vector: a row whose content hash and
       revision matched was skipped even when the active slot held no vector.
       Now the one fingerprint read also fetches the active slot's vector and a
       positively-empty slot re-embeds.
S16-4  CodeInteraction had no ``project`` property, so the identity sweep
       skipped it on every run (and every other ``project``-scoped reader
       failed on it). Now: schema spec + 7_to_8 edge + analyzer create/ensure,
       backfill, ``project`` stamped on writes, a property-aware sweep.
S16-5  search_code_graph reported ``schema_missing: <prefix>_CodeInteraction``
       for a class that exists — the self ``project`` filter hit the missing
       property. Now the class has the property, and the MCP's degraded note
       tells a missing PROPERTY (``property_missing``) from a missing class.

No live Weaviate anywhere: in-memory fakes; the suite pins WEAVIATE_URL at the
unroutable sentinel.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sqlite3
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import codegraph_guards as guards  # noqa: E402
from vco_lib import codegraph_schema as cgs  # noqa: E402
from vco_lib import codegraph_vector_copy as cvc  # noqa: E402
from vco_lib.codegraph_lang._shared import _is_minified_content  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_EDGE_7_TO_8 = REPO_ROOT / "migrations" / "codegraph_collection" / "7_to_8.py"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(autouse=True)
def _private_audit_ledger(tmp_path, monkeypatch):
    """Each test gets its own one-time-audit ledger (never the machine's)."""
    from vco_lib import codegraph_one_time_audits as audits

    monkeypatch.setattr(audits, "_ledger_path", lambda: tmp_path / "audits.json")


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load("_v02102_w1c_acg", _ANALYZER_PATH)


# ══════════════════════════ S16-3 (a): the shape rule ═══════════════════════


def _rust_source_with_one_long_literal(n_lines: int = 300, literal_len: int = 2100) -> str:
    """The S16-3 field shape: ordinary hand-written code (the launcher's
    db/migrations.rs) with ONE SQL string literal longer than 2,000 chars."""
    body = [f"    let value_{i} = compute(input, {i}); // step {i}" for i in range(n_lines)]
    body[n_lines // 2] = '    const SQL: &str = "' + ("x" * literal_len) + '";'
    return "fn main() {\n" + "\n".join(body) + "\n}\n"


def test_one_long_literal_in_normal_source_is_not_minified():
    """(i) RED on the pre-.102 rule (any line >= 2000 chars → minified)."""
    src = _rust_source_with_one_long_literal()
    assert max(len(line) for line in src.split("\n")) >= 2100
    assert _is_minified_content(src) is False


def test_readable_generated_file_with_a_long_descriptor_line_is_kept():
    """A descriptor_pb2.py-shaped file (3,370 lines, median 49, two lines over
    20k chars, ~12% of its chars in long lines) is readable code — keep it."""
    lines = [f"_globals['_FIELD_{i}']._serialized_start = {i * 7}" for i in range(3370)]
    lines[10] = "DESCRIPTOR = b'" + "\\x0a" * 5500 + "'"
    lines[20] = "_SERIALIZED = b'" + "\\x12" * 5500 + "'"
    assert _is_minified_content("\n".join(lines)) is False


def test_single_line_bundle_is_minified():
    """(ii) a bundler's one-line output (cytoscape.min.js / vendor.js shape)."""
    blob = "!function(e,t){" + "var a=e[t]||{};a.x=function(n){return n*2};" * 400 + "}(window,'m');"
    assert _is_minified_content(blob) is True


def test_bundle_with_thousands_of_short_lines_around_giant_ones_is_minified():
    """(ii) the mermaid.min.js shape: 3,637 lines, median 24, only 3.9% of the
    LINES long — but 95.8% of the characters live in them. A fraction-of-lines
    rule calls this readable; the character-share rule does not."""
    short = ["/*! license */ var a = 1;"] * 3500
    giant = ["e.exports=function(t){" + "t.a=t.b+t.c;" * 700 + "};"] * 140
    lines: List[str] = []
    for i, s in enumerate(short):
        lines.append(s)
        if i % 25 == 0 and giant:
            lines.append(giant.pop())
    content = "\n".join(lines)
    long_lines = sum(1 for ln in content.split("\n") if len(ln) >= 2000)
    assert long_lines / len(content.split("\n")) < 0.05
    assert _is_minified_content(content) is True


def test_wide_median_file_is_minified():
    """(ii) the react-dom.production.min.js shape: no 1000+ line, median ~505."""
    content = "\n".join(("a=" + "b+c;" * 125)[:505] for _ in range(268))
    assert _is_minified_content(content) is True


def test_mid_median_minified_build_without_long_lines_is_minified():
    """The react-dom-test-utils.production.min.js shape: 41 lines, median 326,
    not one line >= 1000 chars — only the median rule can catch it."""
    content = "\n".join(("t=" + "e.a(n);" * 50)[:326] for _ in range(41))
    assert max(len(x) for x in content.split("\n")) < 1000
    assert _is_minified_content(content) is True


def test_thresholds_are_module_level_constants():
    from vco_lib.codegraph_lang import _shared

    assert _shared._MINIFIED_LONG_LINE_LEN == 1000
    assert _shared._MINIFIED_LONG_SHARE == 0.7
    assert _shared._MINIFIED_MEDIAN_LINE_LEN == 250
    assert _shared._MINIFIED_MIN_CONTENT_LEN == 4000
    assert not hasattr(_shared, "_MINIFIED_MAX_LINE_LEN"), (
        "the one-line rule is retired — a stale constant would read as live policy"
    )


# ═════════════ S16-3 (b): a policy-skipped file leaves no owed rows ═════════


class _Obj:
    def __init__(self, uuid, props):
        self.uuid = uuid
        self.properties = props


class _PruneData:
    def __init__(self):
        self.deleted: list = []

    def delete_by_id(self, uuid):
        self.deleted.append(uuid)


class _PruneColl:
    def __init__(self, name, anchor, rows):
        self.name = name
        self._rows = [_Obj(u, p) for u, p in rows]
        self.data = _PruneData()
        props = [types.SimpleNamespace(name=n) for n in (anchor, "project", "project_source")]
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(properties=props))

    def iterator(self, return_properties=None):
        return iter(self._rows)


_ANCHORS = {
    "modules_collection": "path",
    "functions_collection": "file_path",
    "classes_collection": "file_path",
    "apis_collection": "file_path",
    "interactions_collection": "file_path",
}


def _row(uuid, anchor, path, source, project="Acme"):
    return (uuid, {anchor: path, "project": project, "project_source": source})


def _bare_analyzer(analyzer_mod, rows):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "Acme"
    inst.client = None
    inst._prune_failures = 0
    inst._git_deleted_report = analyzer_mod._PruneReport()
    for attr, anchor in _ANCHORS.items():
        setattr(inst, attr, _PruneColl(f"Acme_{attr}", anchor, rows.get(attr, [])))
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False
    inst._track_visited = False
    inst.visited_uuids = set()
    inst._get_existing_module = lambda path, file_hash: "existing-module-uuid"
    return inst


def _deleted(inst) -> set:
    out: set = set()
    for attr in _ANCHORS:
        out |= set(getattr(inst, attr).data.deleted)
    return out


_MINIFIED_JS = "!function(){" + "var q=window.x||{};q.f=function(n){return n};" * 300 + "}();\n"
_NORMAL_JS = "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"


def test_only_file_mode_prunes_the_skipped_file(analyzer_mod, tmp_path):
    """The per-edit hook's single-file walk reaches the same end-of-walk flush."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "vendor.js").write_text(_MINIFIED_JS)
    repo = repo.resolve()
    rows = {a: [_row(f"{a}-vendor", anc, "src/vendor.js", repo.as_posix())]
            for a, anc in _ANCHORS.items()}
    inst = _bare_analyzer(analyzer_mod, rows)
    inst.analyze_repository(repo, only_file=repo / "src" / "vendor.js")
    assert _deleted(inst) == {f"{a}-vendor" for a in _ANCHORS}


def test_minified_file_under_an_extra_root_is_pruned_only_there(analyzer_mod, tmp_path):
    """Act arm for a non-primary source root: the extra root's skipped file loses
    its rows; the primary's same-path (normal, unchanged) file keeps its own."""
    primary, extra = tmp_path / "primary", tmp_path / "extra"
    for root, body in ((primary, _NORMAL_JS), (extra, _MINIFIED_JS)):
        (root / "src").mkdir(parents=True)
        (root / "src" / "vendor.js").write_text(body)
    primary, extra = primary.resolve(), extra.resolve()
    rows = {"functions_collection": [
        _row("primary-vendor", "file_path", "src/vendor.js", primary.as_posix()),
        _row("extra-vendor", "file_path", "src/vendor.js", extra.as_posix()),
        _row("legacy-unstamped", "file_path", "src/vendor.js", ""),
    ]}
    inst = _bare_analyzer(analyzer_mod, rows)
    inst.analyze_repository(primary, extra_paths=[extra])
    deleted = _deleted(inst)
    assert "extra-vendor" in deleted
    assert "primary-vendor" not in deleted
    assert "legacy-unstamped" not in deleted, "an empty source is the PRIMARY's, never an extra root's"


def _walk_repo_with_a_minified_file(analyzer_mod, tmp_path, *, extra_rows=()):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "vendor.js").write_text(
        "!function(){" + "var q=window.x||{};q.f=function(n){return n};" * 300 + "}();\n"
    )
    (repo / "src" / "app.js").write_text(
        "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"
    )
    repo = repo.resolve()
    src = repo.as_posix()
    rows: Dict[str, list] = {}
    for attr, anchor in _ANCHORS.items():
        rows[attr] = [
            _row(f"{attr}-vendor", anchor, "src/vendor.js", src),
            _row(f"{attr}-app", anchor, "src/app.js", src),
        ]
    for attr, row in extra_rows:
        rows[attr].append(row)
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "Acme"
    inst.client = None
    inst._prune_failures = 0
    inst._git_deleted_report = analyzer_mod._PruneReport()
    for attr, anchor in _ANCHORS.items():
        setattr(inst, attr, _PruneColl(f"Acme_{attr}", anchor, rows[attr]))
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False
    inst._track_visited = False
    inst.visited_uuids = set()
    # app.js is unchanged since the last walk: the per-file gate skips it, so
    # it is walked-and-kept (the leave-alone arm), never extracted here.
    inst._get_existing_module = lambda path, file_hash: "existing-module-uuid"
    stats = inst.analyze_repository(repo)
    deleted = set()
    for attr in _ANCHORS:
        deleted |= set(getattr(inst, attr).data.deleted)
    return inst, deleted, stats, src


def test_minified_skip_prunes_the_skipped_files_rows(analyzer_mod, tmp_path):
    """(iii) RED before .102: the skip printed and returned, leaving five owed
    rows the walk could neither re-embed nor prune."""
    inst, deleted, _stats, _src = _walk_repo_with_a_minified_file(analyzer_mod, tmp_path)
    assert {f"{a}-vendor" for a in _ANCHORS} <= deleted
    assert inst._prune_failures == 0


def test_non_skipped_files_and_other_roots_keep_their_rows(analyzer_mod, tmp_path):
    """(iv) leave-alone: the unchanged (not skipped) file keeps its rows; the
    SAME relative path under another source root or another project survives;
    a legacy primary row with no provenance is the primary's and is pruned."""
    other_root = (tmp_path / "elsewhere").as_posix()
    extra = (
        ("functions_collection", _row("other-root", "file_path", "src/vendor.js", other_root)),
        ("functions_collection", _row("other-project", "file_path", "src/vendor.js",
                                      "", project="Someone")),
        ("functions_collection", _row("legacy-primary", "file_path", "src/vendor.js", "")),
    )
    _inst, deleted, _stats, _src = _walk_repo_with_a_minified_file(
        analyzer_mod, tmp_path, extra_rows=extra)
    assert not any(u.endswith("-app") for u in deleted), deleted
    assert "other-root" not in deleted and "other-project" not in deleted
    assert "legacy-primary" in deleted


def test_prune_helper_counts_failures_and_never_guesses_a_source():
    from vco_lib.codegraph_lang._shared import prune_policy_skipped_files

    calls: list = []

    def _deleter(coll, path_prop, match, **kw):
        calls.append((coll, kw))
        if coll == "boom":
            raise RuntimeError("scan failed")
        return 1, 0

    deleted, failures = prune_policy_skipped_files(
        [("ok", "path"), ("boom", "file_path")],
        {"/repo": {"a.js"}, "": {"orphan.js"}},
        project="Acme", primary_sources={"/repo"}, deleter=_deleter,
    )
    assert (deleted, failures) == (1, 1)
    # The empty-source key pruned nothing (cannot attribute rows → no guess).
    assert all(kw["project"] == "Acme" for _c, kw in calls) and len(calls) == 2


# ══════════════════ P299-B5: hash-skip requires the vector ══════════════════


class _FpObj:
    def __init__(self, properties, vector):
        self.properties = properties
        self.vector = vector


class _FpQuery:
    def __init__(self, store):
        self.store = store
        self.calls: list = []

    def fetch_object_by_id(self, uuid, return_properties=None, **kw):
        self.calls.append(kw)
        row = self.store.get(str(uuid))
        if row is None:
            return None
        vec = row["vector"] if "include_vector" in kw else None
        return _FpObj(row["props"], vec)


class _FpData:
    def __init__(self):
        self.replace_calls: list = []
        self.update_calls: list = []

    def replace(self, uuid, **kwargs):
        self.replace_calls.append({"uuid": str(uuid), **kwargs})

    def insert(self, uuid, **kwargs):
        self.replace_calls.append({"uuid": str(uuid), **kwargs})
        return str(uuid)

    def update(self, uuid, properties):
        self.update_calls.append({"uuid": str(uuid), "properties": properties})


class _FpColl:
    """A named-vector class that DECLARES the active code slot."""

    def __init__(self, name, store, slots=("codesage_embed",)):
        self.name = name
        self.query = _FpQuery(store)
        self.data = _FpData()
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[], vector_config={s: object() for s in slots}))


def _fresh_analyzer(analyzer_mod, project="TestProject"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.module_cache, inst.class_cache, inst.function_cache = {}, {}, {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    return inst


class _Embedder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"codesage_embed": [0.1, 0.2, 0.3]}


_BODY = "def foo():\n    return 1\n"


def _func_params(embedder):
    return {
        "properties": {"name": "foo", "full_name": "mod.foo", "function_body": _BODY,
                       "signature": "foo()", "type_uses": []},
        "_deferred_embed": embedder,
    }


def _stored_row(analyzer_mod, vector):
    props = {"name": "foo", "full_name": "mod.foo", "function_body": _BODY,
             "signature": "foo()", "type_uses": [], "chunk_num": 0, "total_chunks": 1}
    return {
        "props": {
            "content_hash": analyzer_mod._content_hash_for_object("T_CodeFunction", props),
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
            "total_chunks": 1,
        },
        "vector": vector,
    }


@pytest.mark.parametrize("vector", [{}, {"codesage_embed": []}, {"other_slot": [0.5]}])
def test_hash_match_without_active_vector_is_reembedded(analyzer_mod, vector):
    """RED before .102: hash + revision match → SKIP, vector never checked."""
    inst = _fresh_analyzer(analyzer_mod)
    uid = analyzer_mod._deterministic_uuid(inst.project_name, "src/mod.py", "mod.foo")
    coll = _FpColl("T_CodeFunction", {uid: _stored_row(analyzer_mod, vector)})
    emb = _Embedder()

    inst._dedup_insert(coll, _func_params(emb), "mod.foo", file_path_rel="src/mod.py")

    assert emb.calls == 1, "a row with no vector in the active slot must re-embed"
    assert len(coll.data.replace_calls) == 1, "…and the fresh vector must be written"
    assert coll.data.replace_calls[0].get("vector")
    assert coll.data.update_calls == [], "never a revision-only STAMP over a missing vector"


def test_hash_match_with_vector_is_skipped(analyzer_mod):
    """Leave-alone: hash + revision + a populated active slot → SKIP."""
    inst = _fresh_analyzer(analyzer_mod)
    uid = analyzer_mod._deterministic_uuid(inst.project_name, "src/mod.py", "mod.foo")
    coll = _FpColl("T_CodeFunction",
                   {uid: _stored_row(analyzer_mod, {"codesage_embed": [0.1, 0.2]})})
    emb = _Embedder()

    inst._dedup_insert(coll, _func_params(emb), "mod.foo", file_path_rel="src/mod.py")

    assert emb.calls == 0 and coll.data.replace_calls == []
    assert all(c.get("include_vector") == ["codesage_embed"] for c in coll.query.calls)


def test_undeclared_slot_never_vetoes_the_skip(analyzer_mod):
    """A class that does not declare the active slot cannot gain it by a
    re-embed — no veto, and the read stays the pre-.102 one (no vector asked)."""
    inst = _fresh_analyzer(analyzer_mod)
    uid = analyzer_mod._deterministic_uuid(inst.project_name, "src/mod.py", "mod.foo")
    coll = _FpColl("T_CodeFunction", {uid: _stored_row(analyzer_mod, {})},
                   slots=("qwen3_embed",))
    emb = _Embedder()

    inst._dedup_insert(coll, _func_params(emb), "mod.foo", file_path_rel="src/mod.py")

    assert emb.calls == 0
    assert all("include_vector" not in c for c in coll.query.calls)


def test_write_path_does_not_skip_over_a_missing_vector(analyzer_mod):
    """The write-side skip (`_write_one_object`, eager-vector call sites) must
    agree with the precheck: an eagerly-embedded object whose stored twin has
    no vector is WRITTEN (before .102 its own inline read skipped it)."""
    inst = _fresh_analyzer(analyzer_mod)
    uid = analyzer_mod._deterministic_uuid(inst.project_name, "src/mod.py", "mod.foo")
    coll = _FpColl("T_CodeFunction", {uid: _stored_row(analyzer_mod, {})})
    params = _func_params(None)
    params.pop("_deferred_embed")
    params["vector"] = {"codesage_embed": [0.4, 0.5]}

    inst._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1


def test_guards_default_read_is_unchanged_and_slot_read_reports_presence():
    q = _FpQuery({"u": {"props": {"content_hash": "h", "embed_revision": 9}, "vector": {}}})
    coll = types.SimpleNamespace(query=q)

    fp = guards.read_object_fingerprint(coll, "u", "embed_revision")
    assert fp is not None
    assert q.calls[-1] == {} and "vector_present" not in fp

    fp = guards.read_object_fingerprint(coll, "u", "embed_revision", vector_slot="s")
    assert fp is not None
    assert q.calls[-1] == {"include_vector": ["s"]} and fp["vector_present"] is False


@pytest.mark.parametrize("field,expected", [
    ({"s": [0.1]}, True), ({"s": []}, False), ({}, False), ({"s": None}, False),
    (None, None), ([0.1, 0.2], None),
])
def test_slot_vector_present(field, expected):
    assert guards.slot_vector_present(field, "s") is expected


# ═════════════ S16-4: CodeInteraction gets `project` + the sweep ═════════════


def test_spec_table_declares_project_on_code_interaction():
    specs = {p for (p, _t, _d) in cgs.CODEGRAPH_PROPERTY_SPECS["CodeInteraction"]}
    assert "project" in specs
    assert ("project", "TEXT") in {(p, t) for (p, t, _d) in
                                   cgs.CODEGRAPH_PROPERTY_SPECS["CodeInteraction"]}


class _SchemaColl:
    """A live-ish class: schema props mutate on add_property; rows mutate on update."""

    def __init__(self, name, props, rows=(), iter_raises_without_project=True,
                 add_raises=False):
        self.name = name
        self.props = set(props)
        self.rows = [_Obj(u, dict(p)) for u, p in rows]
        self.iter_calls: list = []
        self.added: list = []
        self.updates: list = []
        self._raise = iter_raises_without_project
        outer = self

        def _add(prop):
            if add_raises:
                raise RuntimeError("422 add_property refused")
            outer.added.append(prop.name)
            outer.props.add(prop.name)

        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[types.SimpleNamespace(name=n) for n in sorted(outer.props)]),
            add_property=_add,
        )

        def _update(uuid, properties):
            outer.updates.append((uuid, properties))
            for r in outer.rows:
                if r.uuid == uuid:
                    r.properties.update(properties)

        self.data = types.SimpleNamespace(update=_update)

    def iterator(self, return_properties=None):
        self.iter_calls.append(list(return_properties or []))
        missing = [p for p in (return_properties or []) if p not in self.props]
        if missing and self._raise:
            raise RuntimeError(f"no such prop with name '{missing[0]}' found in class")
        return iter(self.rows)


def _five_class_client(prefix="Acme", *, ix_add_raises=False, ix_rows=None):
    colls = {
        f"{prefix}_{b}": _SchemaColl(f"{prefix}_{b}", {"project", "file_path"},
                                     rows=[("r-" + b, {"project": prefix})])
        for b in ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI")
    }
    colls[f"{prefix}_CodeInteraction"] = _SchemaColl(
        f"{prefix}_CodeInteraction", {"source_project", "file_path"},
        rows=ix_rows if ix_rows is not None else [("ix-1", {}), ("ix-2", {"project": None})],
        add_raises=ix_add_raises,
    )
    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda n: n in colls, get=lambda n: colls[n]))
    return client, colls


def test_sweep_ensures_project_on_code_interaction_and_enumerates_it():
    """RED before .102: the `project` iterate raised on CodeInteraction and the
    sweep logged + `continue`d — the class was never ensured nor enumerated."""
    client, colls = _five_class_client()
    ix = colls["Acme_CodeInteraction"]

    summaries = cvc.sweep_stale_identities("Acme", "Acme", client=client, dry_run=False)

    assert summaries == []  # single-identity after the backfill
    assert ix.added == ["project"]
    assert {r.uuid: r.properties.get("project") for r in ix.rows} == {"ix-1": "Acme", "ix-2": "Acme"}
    assert ["project"] in ix.iter_calls[-1:], "CodeInteraction must be enumerated after the ensure"


def test_sweep_finds_a_stale_identity_on_an_ensured_code_interaction():
    client, colls = _five_class_client(ix_rows=[("ix-1", {})])
    found = cvc._distinct_stale_identities(client, "Acme", "Acme", ensure=True)
    assert found == []
    colls["Acme_CodeInteraction"].rows.append(_Obj("ix-old", {"project": "OldName"}))
    assert cvc._distinct_stale_identities(client, "Acme", "Acme", ensure=True) == ["OldName"]


def test_read_only_sweep_never_writes_and_warns(caplog):
    """Leave-alone arm: a dry-run sweep does not ensure or backfill; the class
    lacking `project` is skipped with a WARNING naming it."""
    client, colls = _five_class_client()
    ix = colls["Acme_CodeInteraction"]
    with caplog.at_level("WARNING"):
        cvc.sweep_stale_identities("Acme", "Acme", client=client, dry_run=True)
    assert ix.added == [] and ix.updates == []
    assert ix.iter_calls == [], "a class known to lack the property is not iterated"
    assert "Acme_CodeInteraction has no 'project' property" in caplog.text


def test_sweep_warns_when_the_ensure_fails(caplog):
    client, colls = _five_class_client(ix_add_raises=True)
    with caplog.at_level("WARNING"):
        cvc.sweep_stale_identities("Acme", "Acme", client=client, dry_run=False)
    assert colls["Acme_CodeInteraction"].updates == []
    assert "cannot ensure 'project' on Acme_CodeInteraction" in caplog.text
    assert "Acme_CodeInteraction has no 'project' property" in caplog.text


def test_backfill_touches_only_rows_lacking_the_value_and_resumes():
    coll = _SchemaColl("X", {"project"}, rows=[
        ("a", {}), ("b", {"project": "Kept"}), ("c", {"project": ""}),
    ])
    calls = {"n": 0}
    real_update = coll.data.update

    def _flaky(uuid, properties):
        calls["n"] += 1
        if uuid == "c" and calls["n"] == 2:
            raise RuntimeError("transient")
        real_update(uuid, properties)

    coll.data = types.SimpleNamespace(update=_flaky)
    assert cgs.backfill_missing_property(coll, "project", "Acme") == (1, 1)
    assert cgs.backfill_missing_property(coll, "project", "Acme") == (1, 0)  # resumed
    assert {r.uuid: r.properties.get("project") for r in coll.rows} == {
        "a": "Acme", "b": "Kept", "c": "Acme"}
    assert cgs.backfill_missing_property(coll, "project", "Acme") == (0, 0)  # idempotent


def test_backfill_leaves_a_class_without_the_property_alone():
    coll = _SchemaColl("X", {"source_project"}, rows=[("a", {})])
    assert cgs.backfill_missing_property(coll, "project", "Acme") == (0, 0)
    assert coll.updates == []


def test_analyzer_ensure_adds_and_backfills_once(analyzer_mod):
    ix = _SchemaColl("Acme_CodeInteraction", {"source_project"}, rows=[("ix-1", {})])
    stub = types.SimpleNamespace(interactions_collection=ix, project_name="Acme")
    analyzer_mod.CodeGraphAnalyzer._ensure_interaction_project_property(stub)
    assert ix.added == ["project"] and ix.rows[0].properties["project"] == "Acme"
    # Present already → no add, no rescan (the edge owns completing a partial backfill).
    ix.rows.append(_Obj("ix-2", {}))
    analyzer_mod.CodeGraphAnalyzer._ensure_interaction_project_property(stub)
    assert ix.added == ["project"] and "project" not in ix.rows[1].properties


class _CreateClient:
    def __init__(self):
        self.collections = types.SimpleNamespace(
            exists=lambda _n: False,
            get=lambda n: _SchemaColl(n, set()),
        )


def test_analyzer_creates_code_interaction_with_project_on_a_zero_interaction_project(
    analyzer_mod,
):
    """S16-5 red-proof (analyzer side): the five classes always exist once a
    project is analyzed — CodeInteraction included even with zero interactions —
    and it is born with `project`."""
    inst = analyzer_mod.CodeGraphAnalyzer("Acme")
    inst.client = _CreateClient()
    created: Dict[str, list] = {}

    def _record(name, **kw):
        created[name] = [p.name for p in kw.get("properties", [])]
        return True

    inst._create_class_with_retry = _record
    inst.create_collections()
    assert set(created) == {f"Acme_{b}" for b in (
        "CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction")}
    for name, props in created.items():
        assert "project" in props, f"{name} must be born with `project`"


def test_interaction_rows_are_stamped_with_project(analyzer_mod):
    inst = _fresh_analyzer(analyzer_mod, project="Acme")
    seen: list = []
    inst.store_entity = lambda entity: seen.append(entity)
    inst._store_interactions(
        [{"interaction_type": "http", "protocol": "GET", "endpoint": "/x", "source": "f",
          "confidence": "high"}],
        language="Python", module_uuid="m", file_path_rel="a.py")
    assert seen[0].extras["project"] == "Acme"
    assert seen[0].extras["source_project"] == "Acme"


def test_project_is_outside_the_interaction_content_hash(analyzer_mod):
    """Stamping `project` on writes must not re-hash (and so re-write) a single
    already-stored interaction row."""
    base = {"interaction_type": "http", "protocol": "GET", "endpoint": "/x",
            "raw_target": "", "direction": "outbound", "description": "d"}
    h = analyzer_mod._content_hash_for_object
    assert h("A_CodeInteraction", base) == h("A_CodeInteraction", {**base, "project": "A"})


# ───────────────────────────── the 7_to_8 edge ─────────────────────────────


@pytest.fixture()
def edge(monkeypatch):
    mod = _load("_v02102_edge_7_to_8", _EDGE_7_TO_8)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "Acme")
    return mod


def _run_edge(edge_mod, monkeypatch, client):
    monkeypatch.setattr(edge_mod, "_connect", lambda: types.SimpleNamespace(
        collections=client.collections, close=lambda: None))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = edge_mod.main()
    return rc, out.getvalue()


def test_edge_adds_and_backfills_then_reports_applied(edge, monkeypatch):
    client, colls = _five_class_client(ix_rows=[("ix-1", {}), ("ix-2", {"project": "Kept"})])
    rc, out = _run_edge(edge, monkeypatch, client)
    ix = colls["Acme_CodeInteraction"]
    assert rc == 0 and "EDGE_APPLIED=1" in out
    assert ix.added == ["project"]
    assert {r.uuid: r.properties["project"] for r in ix.rows} == {"ix-1": "Acme", "ix-2": "Kept"}
    for base in ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI"):
        assert colls[f"Acme_{base}"].added == [] and colls[f"Acme_{base}"].updates == []
    # Idempotent re-run: nothing added, nothing rewritten.
    ix.updates.clear()
    rc, out = _run_edge(edge, monkeypatch, client)
    assert rc == 0 and ix.updates == [] and ix.added == ["project"]


def test_edge_skips_an_absent_class_without_creating_it(edge, monkeypatch):
    client, colls = _five_class_client()
    del colls["Acme_CodeInteraction"]
    created: list = []
    client.collections.create = lambda *a, **k: created.append((a, k))
    rc, out = _run_edge(edge, monkeypatch, client)
    assert rc == 0 and "EDGE_APPLIED=1" in out
    assert "does not exist — skipped" in out
    assert created == []


def test_edge_backfill_failure_blocks_the_version_advance(edge, monkeypatch):
    client, colls = _five_class_client(ix_rows=[("ix-1", {})])

    def _fail(uuid, properties):
        raise RuntimeError("500")

    colls["Acme_CodeInteraction"].data = types.SimpleNamespace(update=_fail)
    rc, out = _run_edge(edge, monkeypatch, client)
    assert rc == 1 and "EDGE_APPLIED=1" not in out


def test_edge_without_scope_prints_the_noop_sentinel(edge, monkeypatch):
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = edge.main()
    assert rc == 0 and "EDGE_NOOP_NO_PREFIX=1" in out.getvalue()
    assert "EDGE_APPLIED=1" not in out.getvalue()


def _launcher_db(tmp_path: Path) -> Path:
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE _schema_migrations (version INTEGER PRIMARY KEY,"
                 " description TEXT NOT NULL, applied_at INTEGER NOT NULL)")
    mig = REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db" / "migrations"
    for f in sorted(mig.glob("[0-9][0-9][0-9]_*.sql")):
        conn.executescript(f.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO projects (id, name, folder_path, host, slug, created_at,"
                 " updated_at, rl_port) VALUES ('p1','Acme','/tmp/p1','base','acme',1,1,NULL)")
    conn.execute("INSERT OR REPLACE INTO project_codegraph_bindings (project_id,"
                 " collection_prefix, enabled, config_json, updated_at)"
                 " VALUES ('p1','Acme',1,'{}',1)")
    conn.commit()
    conn.close()
    return db


def test_runner_auto_applies_7_to_8_as_an_additive_edge_with_no_consent_deferral(
    tmp_path, monkeypatch,
):
    """Coordinator condition (1): on update the runner walks 7→8 on its own
    (derived + `@destructive: no` → the preserving path), advances to v8, and
    writes NO deferral — no `schema_migration_required`, no consent path."""
    from vco_lib import artifact_version_registry as avr
    from vco_lib import schema_migration_runner as smr
    from vco_lib import schema_versions as sv

    assert sv.canonical_version("codegraph_collection") == 8
    db = _launcher_db(tmp_path)
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(db, project_id="p1", artifact_type="codegraph_collection",
                                      artifact_name=f"Acme_{suffix}", schema_version=8,
                                      materialized_at=1)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE artifact_schema_versions SET schema_version = 7"
                 " WHERE artifact_type = 'codegraph_collection'")
    conn.commit()
    conn.close()
    applied: list = []

    def _spy(edge_obj, *, project_root, launcher_db, weaviate_url, env):
        applied.append(edge_obj.path.name)
        return smr.EdgeResult(ok=True, stdout=smr._EDGE_SENTINEL_APPLIED)

    monkeypatch.setattr(smr, "_apply_edge", _spy)
    names, env, prefix = smr.resolve_codegraph_migration_inputs({}, db_path=db, project_id="p1")
    assert prefix == "Acme"
    report = smr.run_schema_migrations(
        db_path=db, project_id="p1", migrations_dir=REPO_ROOT / "migrations", env=env,
        artifact_names=names, weaviate_url="http://127.0.0.1:9",
        include_orchestrator_wide=False, now_ms=1)

    assert applied and set(applied) == {"7_to_8.py"}
    assert not report.errors and not report.refused
    assert smr.build_deferral_entries(report) == []
    conn = sqlite3.connect(str(db))
    versions = conn.execute("SELECT DISTINCT schema_version FROM artifact_schema_versions"
                            " WHERE artifact_type='codegraph_collection'").fetchall()
    conn.close()
    assert versions == [(8,)]


def test_edge_header_is_derived_and_non_destructive():
    from vco_lib import schema_migration_runner as smr

    edges = smr.discover_edges(REPO_ROOT / "migrations", "codegraph_collection")
    e78 = [e for e in edges if e.path.name == "7_to_8.py"]
    assert e78 and (e78[0].from_version, e78[0].to_version) == (7, 8)
    assert smr._verify_edge_classification(e78[0], derived=True, preserving_required=True) is None
    assert smr._assert_contiguous(edges, start=edges[0].from_version, end=8) is None


def test_adding_a_property_does_not_change_the_embedding_fingerprint():
    """The launcher's schema-drift gate keys re-embed/rebuild on the named-vector
    catalog only; an added property is not in it."""
    from vco_lib import weaviate_schema

    fp = weaviate_schema.embedding_schema_fingerprint("Acme_CodeInteraction")
    assert fp == weaviate_schema.embedding_schema_fingerprint("Other_CodeInteraction")


# ═════════ S16-5: the MCP tells a missing PROPERTY from a missing class ═════════


def _mcp():
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp"))
    import weaviate_mcp.server as srv  # noqa: PLC0415

    return srv


class _NoSuchProp(Exception):
    def __str__(self) -> str:
        return ("explorer: get class: vector search: object vector search at index "
                "acme_codeinteraction: no such prop with name 'project' found in class "
                "'Acme_CodeInteraction' in the schema. Check your schema files")


class _ClassNotFound(Exception):
    def __str__(self) -> str:
        return "could not find class MissingPeer_CodeFunction"


def test_missing_property_name_parser():
    srv = _mcp()
    assert srv._missing_property_name(str(_NoSuchProp())) == "project"
    assert srv._missing_property_name("no such property valid_until") == "valid_until"
    assert srv._missing_property_name(str(_ClassNotFound())) is None


def test_schema_degraded_splits_class_and_property():
    srv = _mcp()
    out = srv._schema_degraded(["A_X", "B_Y"], {"B_Y": "project"})
    assert out == {"schema_missing": ["A_X"], "property_missing": {"B_Y": "project"}}
    assert srv._schema_degraded(["B_Y"], {"B_Y": "project"}) == {
        "property_missing": {"B_Y": "project"}}


def test_search_code_graph_reports_property_missing_not_schema_missing(monkeypatch):
    """RED before .102: a class that EXISTS but lacks `project` was filed under
    `schema_missing` (reads as "class absent"). A truly absent peer class still
    lands in `schema_missing`."""
    srv = _mcp()
    self_prefix = srv._code_sanitize_collection_prefix("Acme")

    class _Meta:
        distance = 0.1
        score = 0.9

    class _Row:
        def __init__(self):
            self.properties = {"full_name": "m.f", "name": "f", "file_path": "x.py",
                               "signature": "def f()", "chunk_num": 0, "total_chunks": 1}
            self.metadata = _Meta()
            self.vector = {"codesage_embed": [0.1, 0.2, 0.3]}

    def _per(name):
        def _go(**_kw):
            if name == f"{self_prefix}_CodeInteraction":
                raise _NoSuchProp()
            if not name.startswith(self_prefix + "_"):
                raise _ClassNotFound()
            return types.SimpleNamespace(objects=[_Row()])
        return _go

    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        get=lambda n: types.SimpleNamespace(query=types.SimpleNamespace(near_vector=_per(n)))))
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", "Acme", raising=False)

    async def _embed(_q):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(srv, "get_code_query_embedding", _embed)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda *_a, **_k: client)
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False, raising=False)
    monkeypatch.setattr(srv, "_parse_csv_env", lambda name: (
        ["MissingPeer"] if name == "VCT_CODE_GRAPH_ACCESS_LIST" else []))
    monkeypatch.setattr(srv, "run_code_retrieval_pipeline",
                        lambda rows, *_a, **k: rows[: k.get("limit", 8)], raising=False)

    data = json.loads(asyncio.run(srv.search_code_graph(
        query="auth", scope="all", limit=5, project="Acme")))

    failed = data.get("degraded", {}).get("failed_collections", {})
    assert failed.get("property_missing") == {f"{self_prefix}_CodeInteraction": "project"}, data
    assert all("MissingPeer" in c for c in failed.get("schema_missing", [])), failed
    assert f"{self_prefix}_CodeInteraction" not in failed.get("schema_missing", [])


class _AggColl(_SchemaColl):
    def __init__(self, name, props, stale=0):
        super().__init__(name, props)
        outer = self

        def _over_all(filters=None, total_count=True):
            if "project" not in outer.props:
                raise RuntimeError("no such prop with name 'project' found in class")
            return types.SimpleNamespace(total_count=stale)

        self.aggregate = types.SimpleNamespace(over_all=_over_all)


def _probe_client(ix_props):
    colls = {f"Acme_{b}": _AggColl(f"Acme_{b}", {"project"})
             for b in ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI")}
    colls["Acme_CodeInteraction"] = _AggColl("Acme_CodeInteraction", ix_props)
    return types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda n: n in colls, get=lambda n: colls[n]))


def test_identity_probe_forces_the_sweep_when_a_class_lacks_project():
    """RED before the fix round: the project-less class counted as merely
    undeterminable, the other four returned 0 → probe 0 → the sweep (and its
    ensure) never ran from its own background trigger."""
    from vco_lib import codegraph_resync as cr

    assert cr._probe_stale_identity_count(_probe_client({"source_project"}), "Acme", "Acme") is None
    # Leave-alone: every class carries `project`, zero stale → converged (0).
    assert cr._probe_stale_identity_count(_probe_client({"project"}), "Acme", "Acme") == 0


def _wire_hybrid(monkeypatch, srv, side):
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None)

    async def _vec(_q):
        return ([0.1, 0.2, 0.3], "qwen3_embed")

    monkeypatch.setattr(srv, "_get_search_vector", _vec)
    monkeypatch.setattr(srv, "_kg_collections_to_search", lambda *_a, **_k: ["A_KG", "B_KG", "C_KG"])

    async def _single(coll_name, *_a, **_k):
        return side(coll_name)

    monkeypatch.setattr(srv, "_hybrid_search_single_collection", _single)

    async def _rerank(_task, _q, nodes, _limit, **_k):
        return nodes

    monkeypatch.setattr(srv, "_rl_cache_and_rerank", _rerank)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_rl_enrich_nodes_with_linked_embs", lambda *a, **k: None)

    async def _dual(*_a, **_k):
        return None

    monkeypatch.setattr(srv, "_resolve_dual_rl_log_inputs", _dual)


def test_hybrid_search_reports_property_missing(monkeypatch):
    srv = _mcp()

    def _side(coll):
        if coll == "A_KG":
            return {("Node A", 0): {"combined_score": 0.9, "title": "Node A",
                                    "collection": "A_KG", "content": "x"}}
        if coll == "B_KG":
            raise RuntimeError("no such prop with name 'valid_until' found in class 'B_KG'")
        raise RuntimeError("could not find class C_KG")

    _wire_hybrid(monkeypatch, srv, _side)
    out = asyncio.run(srv._hybrid_search_body("q", 5, "", [], cast(int, None), "auto", False))
    failed = json.loads(out)["degraded"]["failed_collections"]
    assert failed["property_missing"] == {"B_KG": "valid_until"}
    assert failed["schema_missing"] == ["C_KG"]


def test_semantic_graph_search_reports_property_missing(monkeypatch):
    srv = _mcp()

    def _hit():
        return types.SimpleNamespace(
            uuid="u1", properties={"title": "P", "content": "c", "file_path": "knowledge/p.md"},
            metadata=types.SimpleNamespace(distance=0.1, score=0.9), vector={})

    class _Q:
        def __init__(self, name):
            self.name = name

        def _go(self, **_kw):
            if self.name == "B_KG":
                raise RuntimeError("no such prop with name 'valid_until' found in class 'B_KG'")
            return types.SimpleNamespace(objects=[_hit()])

        near_vector = near_text = _go

        def fetch_objects(self, **_kw):
            return types.SimpleNamespace(objects=[])

    colls = {n: types.SimpleNamespace(name=n, query=_Q(n)) for n in (srv.KG_COLLECTION, "B_KG")}
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "weaviate", raising=False)
    monkeypatch.setattr(srv, "_kg_collections_to_search",
                        lambda **_k: [srv.KG_COLLECTION, "B_KG"])
    monkeypatch.setattr(srv, "_stale_filter_for", lambda *_a, **_k: None)
    client = types.SimpleNamespace(collections=types.SimpleNamespace(get=lambda n: colls[n]))
    raw = asyncio.run(srv._semantic_graph_search_body(
        client, "primary", limit=3, depth=1, detail="titles", include_stale=True))
    failed = json.loads(raw)["degraded"]["failed_collections"]
    assert failed == {"property_missing": {"B_KG": "valid_until"}}



# ═══════════════════ Round 3 — Opus review fixes (B1, S3–S5, nits) ═══════════════════


def test_as_extra_path_walk_never_prunes_the_primarys_unstamped_rows(analyzer_mod, tmp_path):
    """B1 red-proof (the review's repro, inverted): an `--as-extra-path` walk's
    minified skip must NOT delete a legacy (unstamped) row — under `strict` an
    empty source is most likely the PRIMARY project's. The extra root's own
    stamped row is still pruned (act arm)."""
    extra = tmp_path / "extra"
    (extra / "src").mkdir(parents=True)
    (extra / "src" / "vendor.js").write_text(_MINIFIED_JS)
    extra = extra.resolve()
    rows = {"functions_collection": [
        _row("legacy-unstamped-primary", "file_path", "src/vendor.js", ""),
        _row("extra-stamped", "file_path", "src/vendor.js", extra.as_posix()),
    ]}
    inst = _bare_analyzer(analyzer_mod, rows)
    inst.analyze_repository(extra, as_extra_path=True)
    deleted = _deleted(inst)
    assert "legacy-unstamped-primary" not in deleted
    assert "extra-stamped" in deleted


def test_prune_never_touches_a_sibling_token_path(analyzer_mod, tmp_path):
    """Exact-match guard: `lib/src/vendor.js` shares every token with the skipped
    `src/vendor.js` (a word-tokenized filter matches both) — only the exact
    path's rows go."""
    inst, deleted, _stats, src = _walk_repo_with_a_minified_file(
        analyzer_mod, tmp_path,
        extra_rows=(("functions_collection",
                     _row("sibling-token", "file_path", "lib/src/vendor.js", "")),
                    ("functions_collection",
                     ("sibling-token-stamped", {"file_path": "lib/src/vendor.js",
                                                "project": "Acme",
                                                "project_source": "/repo-placeholder"})),
                    ),
    )
    assert "sibling-token" not in deleted and "sibling-token-stamped" not in deleted
    assert "functions_collection-vendor" in deleted


def test_policy_skip_record_is_reset_at_walk_start(analyzer_mod, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.js").write_text(_NORMAL_JS)
    inst = _bare_analyzer(analyzer_mod, {})
    inst._policy_skipped = {"/stale/source": {"leftover.js"}}
    seen: list = []
    inst._prune_policy_skipped_files = lambda: seen.append(dict(inst._policy_skipped))
    inst.analyze_repository(repo.resolve())
    assert seen == [{}], "a previous run's record must never reach this walk's prune"


# ── S5: ignore-set rows purgeable at ANY revision + the one-time purge ──


def test_current_revision_ignored_row_is_purgeable():
    """RED before round 3: rule 3 (converged → not_owed) ran before the
    ignore-set rule, so a denylisted row at the current revision was immortal."""
    from vco_lib.codegraph_row_classify import classify_row

    row = {"file_path": "vendor/lib/x.js", "embed_revision": 1}
    assert classify_row(row, None, current_revision=1) == "purgeable"
    # Leave-alone: a converged non-ignored row stays not_owed; an extra root's
    # ignored row is still that root's (tenant rule first).
    assert classify_row({"file_path": "src/x.js", "embed_revision": 1}, None,
                        current_revision=1) == "not_owed"
    assert classify_row({**row, "project_source": "/other"}, None, current_revision=1,
                        primary_sources={"/repo"}) == "not_owed"


class _PurgeColl:
    def __init__(self, name, rows, anchor="file_path"):
        self.name = name
        self.rows = [_Obj(u, p) for u, p in rows]
        self.deleted: list = []
        self.fail: set = set()
        outer = self

        def _del(uuid):
            if uuid in outer.fail:
                raise RuntimeError("500 boom")
            outer.deleted.append(uuid)

        self.data = types.SimpleNamespace(delete_by_id=_del)
        props = [types.SimpleNamespace(name=n) for n in (anchor, "project", "project_source")]
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(properties=props))

    def iterator(self, return_properties=None, **_kw):
        return iter(self.rows)


def _purge_client(colls):
    return types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda n: n in colls, get=lambda n: colls[n]))


def test_ignored_path_purge_hits_only_this_projects_ignored_paths():
    from vco_lib import codegraph_resync as cr

    fn = _PurgeColl("Acme_CodeFunction", [
        ("ignored", {"file_path": "vendor/x.js"}),
        ("sibling-token", {"file_path": "lib/my_vendor_tools/x.js"}),
        ("token-superset", {"file_path": "src/vendor.js"}),
        ("dot-claude", {"file_path": ".claude/scripts/tool.py"}),
        ("normal", {"file_path": "src/app.py"}),
    ])
    other = _PurgeColl("Other_CodeFunction", [("other-ignored", {"file_path": "vendor/x.js"})])
    client = _purge_client({"Acme_CodeFunction": fn, "Other_CodeFunction": other})

    rep = cr.prune_ignored_rows_report("Acme", client=client, index_dot_claude=True)
    assert fn.deleted == ["ignored"]
    assert other.deleted == [], "another project's prefix is never opened"
    assert rep == {"counts": {"Acme_CodeFunction": 1}, "failures": 0, "complete": True}

    # index_dot_claude=False (a user project): `.claude/` is walk-excluded too.
    rep = cr.prune_ignored_rows_report("Acme", client=client, index_dot_claude=False)
    assert fn.deleted == ["ignored", "ignored", "dot-claude"]  # fake keeps rows


def test_ignored_path_purge_reports_failures_and_records_only_clean_passes(monkeypatch):
    from vco_lib import codegraph_one_time_audits as audits
    from vco_lib import codegraph_resync as cr

    fn = _PurgeColl("Acme_CodeFunction", [("ignored", {"file_path": "node_modules/a.js"})])
    fn.fail.add("ignored")
    client = _purge_client({"Acme_CodeFunction": fn})
    monkeypatch.setattr(cr, "_build_client", lambda *a, **k: client)
    monkeypatch.setattr(cr, "configure_logging", lambda *a, **k: None)
    key = cr.ignore_set_key(False)

    cr._main(["--prune-ignored", "--project", "Acme"])
    assert audits.audit_owed(audits.AUDIT_IGNORED_PATHS, "Acme", key), "a failed pass is not done"

    fn.fail.clear()
    cr._main(["--prune-ignored", "--project", "Acme"])  # resumed
    assert fn.deleted == ["ignored"]
    assert not audits.audit_owed(audits.AUDIT_IGNORED_PATHS, "Acme", key)
    assert audits.audit_owed(audits.AUDIT_IGNORED_PATHS, "Acme", cr.ignore_set_key(True)), (
        "flipping index_dot_claude changes the key → owed again"
    )


# ── S4: one-time vectorless-row audit through the M-2 demote ──


class _DemoteColl:
    def __init__(self, name, rows, slots=("codesage_embed",)):
        self.name = name
        self.rows = [types.SimpleNamespace(uuid=u, properties=dict(p), vector=v)
                     for u, p, v in rows]
        self.updates: list = []
        self.fail: set = set()
        self.iter_kwargs: list = []
        outer = self

        def _update(uuid, properties):
            if uuid in outer.fail:
                raise RuntimeError("500")
            outer.updates.append(uuid)
            for r in outer.rows:
                if r.uuid == uuid:
                    r.properties.update(properties)

        self.data = types.SimpleNamespace(update=_update)
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[], vector_config={s: object() for s in slots}))

    def iterator(self, return_properties=None, include_vector=None):
        self.iter_kwargs.append(include_vector)
        return iter(self.rows)


def _demote_world():
    fn = _DemoteColl("Acme_CodeFunction", [
        ("no-vector", {"embed_revision": 1}, {}),
        ("empty-slot", {"embed_revision": 1}, {"codesage_embed": []}),
        ("has-vector", {"embed_revision": 1}, {"codesage_embed": [0.1]}),
        ("other-slot-only", {"embed_revision": 1}, {"qwen3_embed": [0.2]}),
        ("already-stale", {"embed_revision": 0}, {}),
        ("unjudgeable", {"embed_revision": 1}, None),
    ])
    mod = _DemoteColl("Acme_CodeModule", [("mod-no-vec", {"embed_revision": 1}, {})],
                      slots=("qwen3_embed",))  # undeclared active slot → skipped
    colls = {"Acme_CodeFunction": fn, "Acme_CodeModule": mod}
    return _purge_client(colls), fn, mod


def test_vectorless_audit_demotes_only_current_rows_missing_the_active_slot():
    from vco_lib import codegraph_resync as cr

    client, fn, mod = _demote_world()
    res = cr._maybe_run_vectorless_audit(
        "Acme", slot_resolver=lambda: "codesage_embed", client=client)
    assert res == {"demoted": 3, "failed": 0}
    assert sorted(fn.updates) == ["empty-slot", "no-vector", "other-slot-only"]
    assert fn.iter_kwargs == [["codesage_embed"]]
    assert mod.updates == [], "a class without the slot cannot gain it by re-embedding"
    assert {r.uuid: r.properties["embed_revision"] for r in fn.rows}["has-vector"] == 1


def test_vectorless_audit_is_one_time_per_key_and_resumable():
    from vco_lib import codegraph_one_time_audits as audits
    from vco_lib import codegraph_resync as cr

    client, fn, _mod = _demote_world()
    fn.fail.add("no-vector")
    first = cr._maybe_run_vectorless_audit(
        "Acme", slot_resolver=lambda: "codesage_embed", client=client)
    assert first == {"demoted": 2, "failed": 1}
    assert audits.recorded_key(audits.AUDIT_VECTORLESS, "Acme") is None, "partial ≠ done"
    assert cr.vectorless_audit_owed("Acme", 1)

    fn.fail.clear()
    fn.updates.clear()
    second = cr._maybe_run_vectorless_audit(
        "Acme", slot_resolver=lambda: "codesage_embed", client=client)
    assert fn.updates == ["no-vector"], "the re-run touches only what is left"
    assert second == {"demoted": 1, "failed": 0}
    assert audits.recorded_key(audits.AUDIT_VECTORLESS, "Acme") == "codesage_embed@1"
    assert not cr.vectorless_audit_owed("Acme", 1)

    fn.updates.clear()
    assert cr._maybe_run_vectorless_audit(
        "Acme", slot_resolver=lambda: "codesage_embed", client=client) is None
    assert fn.updates == [], "recorded under this key → never re-scans"
    # A slot switch changes the key → owed again.
    assert cr._maybe_run_vectorless_audit(
        "Acme", slot_resolver=lambda: "jina_embed", client=client) is not None


def test_vectorless_audit_skips_when_the_slot_is_unresolvable():
    from vco_lib import codegraph_resync as cr

    client, fn, _mod = _demote_world()
    assert cr._maybe_run_vectorless_audit("Acme", slot_resolver=lambda: None,
                                          client=client) is None
    assert fn.updates == []


def test_spawn_gate_launches_for_an_owed_audit_and_leaves_audited_projects_alone(
    monkeypatch, tmp_path,
):
    from vco_lib import codegraph_one_time_audits as audits
    from vco_lib import codegraph_resync as cr

    (tmp_path / ".claude" / "scripts").mkdir(parents=True)
    (tmp_path / ".claude" / "scripts" / "analyze_code_graph.py").write_text(
        "CODEGRAPH_EMBED_REVISION: int = 1\n")
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 0})
    monkeypatch.setattr(cr, "count_cleanup_owed_rows", lambda *a, **k: 0)
    spawned: list = []

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    def _popen(argv, **_k):
        spawned.append(argv)
        return _Proc()

    monkeypatch.setattr(cr.subprocess, "Popen", _popen)
    monkeypatch.setattr(cr._python_exe, "preflight", lambda *a, **k: (True, ""), raising=False)

    res = cr.spawn_background_resync(tmp_path, "Acme", python_exe=sys.executable,
                                     index_dot_claude=False)
    assert res.status == "launched", res
    audits.record_audit(audits.AUDIT_VECTORLESS, "Acme", "codesage_embed@1")
    audits.record_audit(audits.AUDIT_IGNORED_PATHS, "Acme", cr.ignore_set_key(False))
    spawned.clear()
    res = cr.spawn_background_resync(tmp_path, "Acme", python_exe=sys.executable,
                                     index_dot_claude=False)
    assert res.status == "not_owed" and spawned == []


# ── identity probe: CodeInteraction never keeps it positive ──


def test_identity_probe_ignores_stale_identities_on_code_interaction():
    """The engine LEAVES CodeInteraction rows (identity unreconstructable), so a
    stale identity there must not keep the probe positive (a futile sweep on
    every update)."""
    from vco_lib import codegraph_resync as cr

    client = _probe_client({"project"})
    client.collections.get("Acme_CodeInteraction").aggregate = types.SimpleNamespace(
        over_all=lambda **_k: types.SimpleNamespace(total_count=9))
    assert cr._probe_stale_identity_count(client, "Acme", "Acme") == 0


def test_unmigratable_bases_match_the_identity_engine():
    from vco_lib import codegraph_resync as cr

    samples = {
        "CodeModule": {"path": "a.py"}, "CodeClass": {"full_name": "a.C"},
        "CodeFunction": {"full_name": "a.f"}, "CodeAPI": {"endpoint": "/x", "method": "GET"},
        "CodeInteraction": {"endpoint": "/x", "raw_target": "http://h/x", "file_path": "a.py"},
    }
    for base, props in samples.items():
        unmigratable = cvc._identity_key_for_row(base, props) is None
        assert unmigratable == (base in cr._IDENTITY_UNMIGRATABLE_BASES), base


# ── S3: telemetry tells a missing property from a missing class ──


def test_schema_failure_mode_vocabulary():
    srv = _mcp()
    f = srv._schema_failure_mode
    assert f("partial", [], {}) is None
    assert f("partial", ["A"], {"A": "p"}) == "partial_fan_out_property_missing"
    assert f("partial", ["A", "B"], {"A": "p"}) == "partial_fan_out_schema_missing"
    assert f("all", ["A"], {"A": "p"}) == "all_collections_property_missing"
    assert f("all", ["A"], {}) == "all_collections_schema_missing"
    assert f("all", ["A"], {"A": "p"}, [("B", "x")]) == "all_collections_failed"


def test_hybrid_search_telemetry_records_property_missing(monkeypatch):
    """RED before round 3: the RL log filed the property failure as
    `partial_fan_out_schema_missing`."""
    srv = _mcp()
    modes: list = []

    def _side(coll):
        if coll == "A_KG":
            return {("Node A", 0): {"combined_score": 0.9, "title": "Node A",
                                    "collection": "A_KG", "content": "x"}}
        raise RuntimeError(f"no such prop with name 'valid_until' found in class '{coll}'")

    _wire_hybrid(monkeypatch, srv, _side)

    async def _rerank(_task, _q, nodes, _limit, **k):
        modes.append(k.get("failure_mode"))
        return nodes

    monkeypatch.setattr(srv, "_rl_cache_and_rerank", _rerank)
    asyncio.run(srv._hybrid_search_body("q", 5, "", [], cast(int, None), "auto", False))
    assert modes == ["partial_fan_out_property_missing"]


# ═══════════════════ Round 4 — audit reachability + bounded drivers ═══════════════════


def _converged_world(monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr

    (tmp_path / ".claude" / "scripts").mkdir(parents=True)
    (tmp_path / ".claude" / "scripts" / "analyze_code_graph.py").write_text(
        "CODEGRAPH_EMBED_REVISION: int = 1\n")
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 0})
    monkeypatch.setattr(cr, "count_cleanup_owed_rows", lambda *a, **k: 0)
    monkeypatch.setattr(cr._python_exe, "preflight", lambda *a, **k: (True, ""), raising=False)
    spawned: list = []

    class _Proc:
        pid = 777

        def poll(self):
            return None

    def _popen(argv, **_k):
        spawned.append(argv)
        return _Proc()

    monkeypatch.setattr(cr.subprocess, "Popen", _popen)
    return cr, spawned


def test_bundle_update_spawns_the_driver_for_a_converged_project_owing_audits(
    monkeypatch, tmp_path,
):
    """RED before round 4: a converged, extractor-current project never reached
    the driver on update, so its one-time audits never ran."""
    cr, spawned = _converged_world(monkeypatch, tmp_path)
    res = cr.spawn_one_time_audits_if_owed(tmp_path, "Acme", index_dot_claude=False,
                                           python_exe=sys.executable)
    assert res is not None and res.status == "launched", res
    assert any("--run-resync" in a for a in spawned)


def test_bundle_update_leaves_an_audited_project_alone(monkeypatch, tmp_path):
    from vco_lib import codegraph_one_time_audits as audits

    cr, spawned = _converged_world(monkeypatch, tmp_path)
    audits.record_audit(audits.AUDIT_VECTORLESS, "Acme", "codesage_embed@1")
    audits.record_audit(audits.AUDIT_IGNORED_PATHS, "Acme", cr.ignore_set_key(False))
    probes: list = []
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: probes.append(1) or {})
    assert cr.spawn_one_time_audits_if_owed(tmp_path, "Acme", index_dot_claude=False,
                                            python_exe=sys.executable) is None
    assert spawned == [] and probes == [], "nothing owed → no spawn, no probe"


def test_project_bundle_update_routes_through_the_audit_spawn(monkeypatch, tmp_path):
    """Wiring: the per-project bundle update calls the audit spawn when no
    extractor re-index launched — and not when one did (it runs the audits)."""
    from vco_lib import codegraph_extractor_generation as ceg
    from vco_lib import codegraph_resync as cr
    from vco_lib import project_init as pi

    calls: list = []
    monkeypatch.setattr(pi, "_resolve_codegraph_project_name", lambda _f: "Acme")
    monkeypatch.setattr(cr, "bundle_update_audit_step",
                        lambda *a, **k: calls.append((a, k)) or None)
    for status, expect in (("skipped", 1), ("launched", 0)):
        calls.clear()
        monkeypatch.setattr(ceg, "ensure_extractor_generation",
                            lambda *a, _s=status, **k: ceg.ReindexResult(
                                status=_s, reason="r", generation="g", message="m"))
        result: dict = {"warnings": []}
        pi._trigger_extractor_generation_reindex(
            tmp_path, prev_version="0.2.101", running_version="0.2.102",
            is_root_target=False, result=result, log=lambda *a, **k: None)
        assert len(calls) == expect, status
    calls.clear()
    monkeypatch.setattr(ceg, "ensure_extractor_generation",
                        lambda *a, **k: ceg.ReindexResult(status="skipped", reason="r",
                                                          generation="g", message="m"))
    pi._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.101", running_version="0.2.102",
        is_root_target=True, result={"warnings": []}, log=lambda *a, **k: None)
    assert calls[0][1]["index_dot_claude"] is True


# ── bounded detached drivers: N slots, queue never drop, released on exit ──


_SLOT_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from vco_lib.atomic import machine_slot
with machine_slot(Path(sys.argv[2]), slots=int(sys.argv[3]), wait_s=0, poll_s=0.05) as s:
    print(f"slot={s}", flush=True)
    if sys.argv[4] == "crash":
        import os; os._exit(3)
    time.sleep(float(sys.argv[4]))
"""


def _holder(tmp_path, slot_dir, slots, mode):
    import subprocess

    script = tmp_path / "holder.py"
    script.write_text(_SLOT_HOLDER)
    proc = subprocess.Popen([sys.executable, str(script), str(REPO_ROOT), str(slot_dir),
                             str(slots), mode], stdout=subprocess.PIPE, text=True,
                            env=child_env())
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    return proc, line


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots are POSIX")
def test_n_plus_one_driver_waits_for_a_slot_then_runs(tmp_path):
    import time

    from vco_lib.atomic import machine_slot

    slot_dir = tmp_path / "slots"
    a, la = _holder(tmp_path, slot_dir, 2, "1.5")
    b, lb = _holder(tmp_path, slot_dir, 2, "1.5")
    assert {la, lb} == {"slot=0", "slot=1"}
    waits: list = []
    t0 = time.monotonic()
    with machine_slot(slot_dir, slots=2, wait_s=60, poll_s=0.05,
                      on_wait=lambda w: waits.append(w)) as got:
        elapsed = time.monotonic() - t0
    assert got in (0, 1), "the third driver ran on a freed slot (queued, not dropped)"
    assert waits, "it announced the wait"
    assert elapsed >= 0.5
    a.wait(10)
    b.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots are POSIX")
def test_slot_is_released_when_the_holder_crashes(tmp_path):
    from vco_lib.atomic import machine_slot

    slot_dir = tmp_path / "slots"
    proc, line = _holder(tmp_path, slot_dir, 1, "crash")
    assert line == "slot=0"
    assert proc.wait(10) == 3
    with machine_slot(slot_dir, slots=1, wait_s=0, poll_s=0.05) as got:
        assert got == 0, "a crashed holder never keeps its slot"


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based slots are POSIX")
def test_bound_expiry_runs_anyway_never_drops(tmp_path):
    from vco_lib.atomic import machine_slot

    slot_dir = tmp_path / "slots"
    proc, _ = _holder(tmp_path, slot_dir, 1, "3")
    ran = []
    with machine_slot(slot_dir, slots=1, wait_s=0.2, poll_s=0.05) as got:
        ran.append(got)
    assert ran == [None], "past the bound the work still runs (slot None)"
    proc.wait(10)


def test_driver_entry_takes_a_slot_and_honours_the_env(monkeypatch, tmp_path):
    from vco_lib import atomic
    from vco_lib import codegraph_resync as cr

    seen: dict = {}
    real = atomic.machine_slot

    def _spy(slot_dir, **kw):
        seen.update(kw, dir=slot_dir)
        return real(tmp_path / "s", slots=kw["slots"], wait_s=0)

    monkeypatch.setattr(atomic, "machine_slot", _spy)
    monkeypatch.setenv("VCO_RESYNC_DRIVER_SLOTS", "3")
    monkeypatch.setenv("VCO_RESYNC_DRIVER_WAIT_S", "42")
    monkeypatch.setattr(cr, "_run_resync_and_verify_slotted", lambda *a, **k: 17)
    assert cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py") == 17
    assert seen["slots"] == 3 and seen["wait_s"] == 42.0
    monkeypatch.setenv("VCO_RESYNC_DRIVER_SLOTS", "junk")
    cr.run_resync_and_verify("Acme", tmp_path, tmp_path / "a.py")
    assert seen["slots"] == 2, "an unparseable cap falls back to the default"


# ── W1-C R5: per-project single-flight — one resync driver per project ──────


_CLAIM_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from vco_lib.atomic import exclusive_file_lock
with exclusive_file_lock(Path(sys.argv[2]), timeout_s=None):
    print("held", flush=True)
    if sys.argv[3] == "crash":
        import os; os._exit(3)
    time.sleep(float(sys.argv[3]))
"""


def _claim_holder(tmp_path, lock_path, mode):
    import subprocess

    script = tmp_path / "claim_holder.py"
    script.write_text(_CLAIM_HOLDER)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(REPO_ROOT), str(lock_path), mode],
        stdout=subprocess.PIPE, text=True, env=child_env(),
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    return proc, line


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based claims are POSIX")
def test_second_driver_for_the_same_project_skips(monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr

    ran: list = []
    monkeypatch.setattr(cr, "_run_resync_and_verify_slotted",
                        lambda *a, **k: ran.append(a) or 17)
    proc, line = _claim_holder(
        tmp_path, cr._driver_claim_lock_path("Acme Projects"), "30")
    assert line == "held"
    assert cr.run_resync_and_verify(
        "Acme Projects", tmp_path, tmp_path / "a.py") == 0
    assert ran == [], "a same-project second driver must skip, not run"
    proc.kill()
    proc.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based claims are POSIX")
def test_a_driver_for_a_different_project_is_not_blocked(monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr

    ran: list = []
    monkeypatch.setattr(cr, "_run_resync_and_verify_slotted",
                        lambda *a, **k: ran.append(a) or 17)
    proc, line = _claim_holder(
        tmp_path, cr._driver_claim_lock_path("Acme Projects"), "30")
    assert line == "held"
    assert cr.run_resync_and_verify(
        "Beta Corp", tmp_path, tmp_path / "a.py") == 17
    assert ran, "another project's claim must not block this driver"
    proc.kill()
    proc.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="flock-based claims are POSIX")
def test_the_claim_is_freed_when_the_holder_dies(monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr

    ran: list = []
    monkeypatch.setattr(cr, "_run_resync_and_verify_slotted",
                        lambda *a, **k: ran.append(a) or 17)
    proc, line = _claim_holder(
        tmp_path, cr._driver_claim_lock_path("Acme Projects"), "crash")
    assert line == "held"
    assert proc.wait(10) == 3
    assert cr.run_resync_and_verify(
        "Acme Projects", tmp_path, tmp_path / "a.py") == 17
    assert ran, "a crashed holder never keeps its project claim"


def test_bundle_audit_step_logs_and_emits_the_deferral(monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr
    from vco_lib import deferral_emit

    emitted: list = []
    logs: list = []
    monkeypatch.setattr(deferral_emit, "emit", lambda folder, entry: emitted.append(entry))
    sentinel = object()
    monkeypatch.setattr(cr, "spawn_one_time_audits_if_owed",
                        lambda *a, **k: cr.ResyncTriggerResult(
                            status="deferred", message="svc down", deferral=sentinel))
    res = cr.bundle_update_audit_step(tmp_path, "Acme", index_dot_claude=False,
                                      log=lambda *a, **k: logs.append(a))
    assert res is not None and res.status == "deferred"
    assert emitted == [sentinel] and logs[0][1] == "warn"
    monkeypatch.setattr(cr, "spawn_one_time_audits_if_owed", lambda *a, **k: None)
    logs.clear()
    assert cr.bundle_update_audit_step(tmp_path, "Acme", index_dot_claude=False,
                                       log=lambda *a, **k: logs.append(a)) is None
    assert logs == [], "nothing owed → silent"

