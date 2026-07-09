# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M-producers: is_test (M1), doc extraction (M3), n_callers (M4),
metadata backfill, and the 5→6 schema edge.

Producer side ONLY (the retrieval consumer — rerank penalty, render lines —
is owned by other tracks). Covers:
  * `is_test_path` truth table (dir PART matching, per-language filename
    patterns, backslash normalization) + parity lock vs the code_ranking
    single-home (skipped until the consumer half lands).
  * `_dedup_insert` choke-point stamps: is_test on Function/Class/Module
    (not API), preset preserved, False is a valid preset; doc populated for
    non-Python rows, Python doc untouched, 2000-char cap.
  * content-hash stability: is_test/n_callers excluded (fallback path).
  * `_ensure_is_test_property` / `_ensure_n_callers_property` additive scope.
  * `create_cross_references` n_callers accumulation: counts, write-only-on-
    change, NULL-stays-NULL, decrement on recompute.
  * `backfill_codegraph_metadata`: update/skip/leave-alone matrix, probe
    short-circuit, per-collection error isolation.
  * `migrations/codegraph_collection/5_to_6.py` structural mirror of 4_to_5.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# The is_test_path parity test imports weaviate_mcp.code_ranking; without this
# the import fails and the parity test SKIPS standalone (it only ran under the
# full-suite conftest path). Adding the shim makes it run in isolation too.
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_module("_v0273_meta_analyzer", _ANALYZER_PATH)


# ─────────────────────────── is_test_path truth table ───────────────────────

_TEST_PATHS = [
    "tests/test_foo.py",
    "src/tests/helper.py",          # dir part anywhere
    "pkg/__tests__/x.js",
    "spec/models/user_spec.rb",
    "testdata/fixture.go",
    "fixtures/sample.json.py",
    "app/test_widget.py",
    "app/widget_test.py",
    "app/conftest.py",
    "web/button.spec.ts",
    "web/button.test.tsx",
    "web/app.spec.mjs",
    "svc/handler_test.go",
    "src/main/FooTest.java",
    "src/main/FooTests.java",
    "src/main/FooIT.java",
    "src/test/java/Anything.java",  # java src/test part-pair
    "Proj.Tests/FooTests.cs",       # csharp *.Tests dir
    "app/FooTest.cs",
    "lib/user_spec.rb",
    "lib/user_test.rb",
    "src/parser_test.rs",
    "cpp/math_test.cpp",
    "cpp/math_tests.cc",
    "cpp/test_math.cpp",
    "lua/thing_spec.lua",
    "sh/deploy_test.sh",
    "sh/deploy.bats",
    "ps/Module.Tests.ps1",
    "win\\tests\\thing.py",         # backslashes normalized
]

_NON_TEST_PATHS = [
    "",
    "src/main.py",
    "my_tests_helper/x.py",          # substring, not a path PART
    "attestation/sign.py",
    "src/latest/foo.py",
    "app/protest.py",
    "java/contest.java",             # case-sensitive CamelCase suffix
    "app/test.py",                   # bare `test.py` matches no pattern
    "src/testing/foo.py",            # `testing` is not in the dir set
    "app/greatest.js",
    "rs/src/lib.rs",                 # in-file #[cfg(test)] NOT path-catchable
    "sh/deploy.sh",
]


def test_is_test_path_truth_table(analyzer_mod):
    fn = analyzer_mod.is_test_path
    for p in _TEST_PATHS:
        assert fn(p) is True, f"expected test: {p}"
    for p in _NON_TEST_PATHS:
        assert fn(p) is False, f"expected NON-test: {p}"


def _extract_inline_is_test_path_fallback():
    """Parse the INLINE `is_test_path` fallback def out of analyze_code_graph.py
    and return it as a standalone callable — WITHOUT letting the module's
    guarded `from weaviate_mcp.code_ranking import is_test_path` succeed.

    Why not just call `analyzer_mod.is_test_path`? On a correctly-installed
    orchestrator that name is the IMPORTED code_ranking symbol (the guarded
    import succeeds), so comparing it to code_ranking.is_test_path is a
    tautology (same object) and the fallback body is never exercised. This
    helper isolates the fallback source so the parity test actually locks the
    two independent bodies together (pre-gate platform audit P1).
    """
    import ast
    import textwrap

    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fallback_src = None
    for node in ast.walk(tree):
        # The fallback def lives inside the `except` of the guarded import;
        # find the FunctionDef named is_test_path that is NOT at module top
        # level (the import binds the name at module level, the def is nested).
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                for stmt in handler.body:
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == "is_test_path":
                        fallback_src = ast.get_source_segment(src, stmt)
    assert fallback_src is not None, (
        "inline is_test_path fallback not found in analyze_code_graph.py — the "
        "guarded-import fallback pattern was removed or renamed"
    )
    ns: dict = {}
    exec(textwrap.dedent(fallback_src), ns)  # noqa: S102 — our own source, test-only
    return ns["is_test_path"]


def test_is_test_path_parity_with_code_ranking(analyzer_mod):
    """Parity lock: the analyzer's INLINE FALLBACK body must behave identically
    to the single-home implementation in code_ranking. We exec the fallback
    source in isolation (see `_extract_inline_is_test_path_fallback`) rather
    than call `analyzer_mod.is_test_path`, which on a normal install is the
    imported code_ranking symbol — comparing it to itself would be a tautology
    (pre-gate platform audit P1)."""
    try:
        from weaviate_mcp.code_ranking import is_test_path as shared
    except Exception:
        # Import-availability gate (bare CI container without the weaviate_mcp
        # deps), NOT a deferred producer/consumer split — code_ranking.is_test_path
        # IS shipped (code_ranking.py). Skip only when the module can't import.
        pytest.skip("weaviate_mcp.code_ranking not importable in this env (deps absent)")
    fallback = _extract_inline_is_test_path_fallback()
    # Sanity: the two must be DIFFERENT function objects (else the test is
    # exercising a single body and the drift-lock is illusory).
    assert fallback is not shared
    for p in _TEST_PATHS + _NON_TEST_PATHS:
        assert shared(p) == fallback(p), p


# ─────────────────── _dedup_insert choke-point stamps ───────────────────────


class _FakeData:
    def __init__(self):
        self.written = []

    def replace(self, uuid, **kwargs):
        self.written.append({"uuid": uuid, **kwargs})

    def insert(self, uuid, **kwargs):
        self.written.append({"uuid": uuid, **kwargs})


class _FakeColl:
    def __init__(self, name):
        self.name = name
        self.data = _FakeData()
        self.query = types.SimpleNamespace(
            fetch_object_by_id=lambda uuid, return_properties=None: None
        )


class _DedupStub:
    """Binds the REAL _dedup_insert path onto a minimal instance."""

    def __init__(self, analyzer_mod):
        self.project_name = "P"
        self._track_visited = False
        self._current_language = ""
        self._current_source = ""
        self.visited_uuids = set()
        cls = analyzer_mod.CodeGraphAnalyzer
        for name in (
            "_dedup_insert", "_write_one_object", "_maybe_chunk_and_write",
            "_stamp_single_chunk_props", "_delete_stale_chunk_rows",
        ):
            setattr(self, name, getattr(cls, name).__get__(self, _DedupStub))


def _func_params(**over):
    props = {
        "name": "f", "full_name": "mod.f", "signature": "def f()",
        "function_body": "def f():\n    return 1\n", "doc": "",
        "language": "python",
    }
    props.update(over)
    return {"properties": props, "vector": [0.1]}


def test_is_test_stamped_on_function_class_module(analyzer_mod):
    stub = _DedupStub(analyzer_mod)
    for base in ("CodeFunction", "CodeClass", "CodeModule"):
        coll = _FakeColl(f"P_{base}")
        params = _func_params()
        stub._dedup_insert(coll, params, "mod.f", file_path_rel="tests/test_x.py")
        assert coll.data.written[0]["properties"]["is_test"] is True, base

    coll = _FakeColl("P_CodeFunction")
    params = _func_params()
    stub._dedup_insert(coll, params, "mod.f", file_path_rel="src/x.py")
    assert coll.data.written[0]["properties"]["is_test"] is False


def test_is_test_not_stamped_on_api_interaction(analyzer_mod):
    stub = _DedupStub(analyzer_mod)
    for base in ("CodeAPI", "CodeInteraction"):
        coll = _FakeColl(f"P_{base}")
        params = {"properties": {"endpoint": "/x"}, "vector": [0.1]}
        stub._dedup_insert(coll, params, "GET /x", file_path_rel="tests/t.py")
        assert "is_test" not in coll.data.written[0]["properties"], base


def test_is_test_preset_false_not_clobbered(analyzer_mod):
    """`False` is a valid preset — the guard is key-presence, not falsiness."""
    stub = _DedupStub(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    params = _func_params(is_test=False)
    stub._dedup_insert(coll, params, "mod.f", file_path_rel="tests/test_x.py")
    assert coll.data.written[0]["properties"]["is_test"] is False


def test_doc_extracted_for_non_python(analyzer_mod):
    stub = _DedupStub(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    body = "public int F()\n/// Adds two numbers.\nreturn a + b;\n"
    params = _func_params(function_body=body, doc="", language="csharp")
    stub._dedup_insert(coll, params, "mod.F", file_path_rel="src/F.cs")
    assert "Adds two numbers" in coll.data.written[0]["properties"]["doc"]


def test_doc_python_row_not_clobbered(analyzer_mod):
    stub = _DedupStub(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    params = _func_params(doc="Existing docstring.")
    stub._dedup_insert(coll, params, "mod.f", file_path_rel="src/f.py")
    assert coll.data.written[0]["properties"]["doc"] == "Existing docstring."


def test_doc_capped_at_2000_chars(analyzer_mod):
    stub = _DedupStub(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    wall = "func f() {\n" + "\n".join("// x" * 40 for _ in range(200))
    params = _func_params(function_body=wall, doc="", language="go")
    stub._dedup_insert(coll, params, "mod.f", file_path_rel="src/f.go")
    assert len(coll.data.written[0]["properties"]["doc"]) <= 2000


def test_ruby_shell_lua_powershell_docstrings():
    # Load THIS repo's copy by path — `import weaviate_mcp` can resolve to a
    # different installed checkout on developer machines.
    ct = _load_module(
        "_v0273_code_truncation",
        REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "code_truncation.py",
    )
    _extract_docstring = ct._extract_docstring

    assert "does things" in _extract_docstring(
        "def go\n# does things\nend\n", "ruby")
    assert "usage: x" in _extract_docstring(
        "go() {\n# usage: x\n}\n", "shell")
    assert "lua doc" in _extract_docstring(
        "function go()\n-- lua doc\nend\n", "lua")
    ps = "function Go {\n<# .SYNOPSIS does #>\n}\n"
    assert ".SYNOPSIS does" in _extract_docstring(ps, "powershell")
    # svelte routes through the js/ts branch.
    assert "svelte doc" in _extract_docstring(
        "function go() {\n// svelte doc\n}\n", "svelte")
    # regression: python branch unchanged.
    assert '"""py doc"""' in _extract_docstring(
        'def f():\n    """py doc"""\n    pass\n', "python")


# ─────────────────── content-hash stability (fallback path) ─────────────────


def test_metadata_props_excluded_from_content_hash(analyzer_mod):
    assert "is_test" in analyzer_mod._CONTENT_HASH_EXCLUDE
    assert "n_callers" in analyzer_mod._CONTENT_HASH_EXCLUDE
    # Unknown-collection fallback hashes all-but-excluded: stamping the two
    # metadata props must not change the digest.
    base = {"some_field": "value"}
    with_meta = dict(base, is_test=True, n_callers=7)
    h1 = analyzer_mod._content_hash_for_object("P_SomethingElse", base)
    h2 = analyzer_mod._content_hash_for_object("P_SomethingElse", with_meta)
    assert h1 == h2


def test_known_collections_hash_unaffected_by_metadata(analyzer_mod):
    props = {
        "full_name": "mod.f", "signature": "def f()",
        "function_body": "def f():\n    return 1\n",
        "type_uses": [], 
    }
    h1 = analyzer_mod._content_hash_for_object("P_CodeFunction", props)
    h2 = analyzer_mod._content_hash_for_object(
        "P_CodeFunction", dict(props, is_test=True, n_callers=3, doc="d")
    )
    assert h1 == h2


# ─────────────────────── ensure helpers (additive scope) ────────────────────


class _EnsureColl:
    def __init__(self, has_props=()):
        self.added = []
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[types.SimpleNamespace(name=n) for n in has_props]
            ),
            add_property=lambda prop: self.added.append(prop.name),
        )


def test_ensure_is_test_scope_and_idempotence(analyzer_mod):
    mods, classes, funcs = _EnsureColl(), _EnsureColl(), _EnsureColl()
    apis, inters = _EnsureColl(), _EnsureColl()
    stub = types.SimpleNamespace(
        modules_collection=mods, classes_collection=classes,
        functions_collection=funcs, apis_collection=apis,
        interactions_collection=inters,
    )
    analyzer_mod.CodeGraphAnalyzer._ensure_is_test_property(stub)
    assert mods.added == ["is_test"]
    assert classes.added == ["is_test"]
    assert funcs.added == ["is_test"]
    assert apis.added == [] and inters.added == [], "API/Interaction excluded"

    have = _EnsureColl(has_props=("is_test",))
    stub2 = types.SimpleNamespace(
        modules_collection=have, classes_collection=have,
        functions_collection=have,
    )
    analyzer_mod.CodeGraphAnalyzer._ensure_is_test_property(stub2)
    assert have.added == [], "already-present prop must be skipped"


def test_ensure_n_callers_function_only(analyzer_mod):
    funcs = _EnsureColl()
    stub = types.SimpleNamespace(functions_collection=funcs)
    analyzer_mod.CodeGraphAnalyzer._ensure_n_callers_property(stub)
    assert funcs.added == ["n_callers"]


# ─────────────────── M4: n_callers accumulation (real pass) ─────────────────


class _XRefFunctions:
    """functions_collection fake for create_cross_references."""

    def __init__(self, bodies, stored_counts=None, stored_call_names=None,
                 file_paths=None):
        self._bodies = bodies
        self._stored = stored_counts or {}
        # v0.2.74 (R1): stored call_names per uuid, for the change-gate test.
        self._stored_calls = stored_call_names or {}
        # v0.2.74 (R4): file_path per uuid + record of which uuids were fetched
        # (so a test can assert unchanged-file functions are NOT re-parsed).
        self._file_paths = file_paths or {}
        self.fetched = []
        self.updates = []
        self.refs = []
        self.query = types.SimpleNamespace(
            fetch_object_by_id=self._fetch
        )
        self.data = types.SimpleNamespace(
            update=lambda uuid, properties: self.updates.append(
                {"uuid": uuid, "properties": properties}
            ),
            reference_add=lambda **kw: self.refs.append(kw),
        )

    def _fetch(self, uuid):
        self.fetched.append(uuid)
        if uuid not in self._bodies:
            return None
        props = {"function_body": self._bodies[uuid], "total_chunks": 1}
        if uuid in self._stored:
            props["n_callers"] = self._stored[uuid]
        if uuid in self._stored_calls:
            props["call_names"] = self._stored_calls[uuid]
        if uuid in self._file_paths:
            props["file_path"] = self._file_paths[uuid]
        return types.SimpleNamespace(uuid=uuid, properties=props)

    def iterator(self, return_properties=None):
        return iter(())


def _xref_analyzer(analyzer_mod, bodies, cache, stored_counts=None,
                   stored_call_names=None, file_paths=None):
    a = analyzer_mod.CodeGraphAnalyzer("P")
    a.client = object()  # truthy — skip the "not connected" bail
    a.functions_collection = _XRefFunctions(
        bodies, stored_counts, stored_call_names, file_paths
    )
    a.classes_collection = types.SimpleNamespace(
        iterator=lambda **kw: iter(()),
        query=types.SimpleNamespace(fetch_objects=lambda **kw: types.SimpleNamespace(objects=[])),
    )
    a.modules_collection = types.SimpleNamespace(iterator=lambda **kw: iter(()))
    a.function_cache = dict(cache)
    a.class_cache = {}
    a.module_cache = {"keep": "the-caches-nonempty"}
    a.module_imports = {}
    # v0.2.74 (R1/R4): the analyzer sources stored n_callers / call_names /
    # file_path from side-dicts the whole-collection cache scan populates.
    # Mirror that here (the caches are injected, so the real scan is stubbed).
    a._xref_stored_ncallers = dict(stored_counts or {})
    a._xref_stored_calls = dict(stored_call_names or {})
    a._xref_file_path = dict(file_paths or {})
    a._populate_caches_from_weaviate = lambda: None  # caches+side-dicts injected
    return a


_CALLER_BODY = "def caller():\n    target()\n"
_TARGET_BODY = "def target():\n    return 1\n"


def test_n_callers_accumulated_for_target(analyzer_mod):
    cache = {"mod.a": "u-a", "mod.b": "u-b", "mod.c": "u-c", "mod.target": "u-t"}
    bodies = {"u-a": _CALLER_BODY, "u-b": _CALLER_BODY, "u-c": _CALLER_BODY,
              "u-t": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache)
    stats = a.create_cross_references()
    ncw = [u for u in a.functions_collection.updates
           if "n_callers" in u["properties"]]
    assert len(ncw) == 1
    assert ncw[0]["uuid"] == "u-t"
    assert ncw[0]["properties"]["n_callers"] == 3
    assert stats["n_callers"] == 1


def test_n_callers_write_only_on_change(analyzer_mod):
    cache = {"mod.a": "u-a", "mod.target": "u-t"}
    bodies = {"u-a": _CALLER_BODY, "u-t": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache, stored_counts={"u-t": 1})
    a.create_cross_references()
    ncw = [u for u in a.functions_collection.updates
           if "n_callers" in u["properties"]]
    assert ncw == [], "stored == new → no write"


def test_n_callers_null_stays_null_for_zero_inbound(analyzer_mod):
    cache = {"mod.leaf": "u-l"}
    bodies = {"u-l": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache)
    a.create_cross_references()
    ncw = [u for u in a.functions_collection.updates
           if "n_callers" in u["properties"]]
    assert ncw == [], "NULL row with zero inbound must stay NULL (no fake 0)"


def test_n_callers_decrements_on_recompute(analyzer_mod):
    """A deleted caller corrects the target's count on the next pass."""
    cache = {"mod.a": "u-a", "mod.target": "u-t"}
    bodies = {"u-a": _CALLER_BODY, "u-t": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache, stored_counts={"u-t": 5})
    a.create_cross_references()
    ncw = [u for u in a.functions_collection.updates
           if "n_callers" in u["properties"]]
    assert len(ncw) == 1 and ncw[0]["properties"]["n_callers"] == 1


# ───────────────────── v0.2.74 (R1) call_names change-gate ──────────────────
# THE headline write-amp fix: create_cross_references must only WRITE
# call_names on a real change (mirroring n_callers). The old code did an
# UNGATED data.update(call_names) for every function every analyze → 23k
# tombstones per run → the 136GB objects-LSM bloat. These tests spy the
# fake collection's `.updates` list for call_names writes.

def _call_names_writes(a):
    return [u for u in a.functions_collection.updates
            if "call_names" in u["properties"]]


def test_call_names_written_when_no_stored(analyzer_mod):
    """First analyze (stored call_names absent/NULL) → WRITE the extracted list."""
    cache = {"mod.caller": "u-c", "mod.target": "u-t"}
    bodies = {"u-c": _CALLER_BODY, "u-t": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache)  # no stored_call_names
    a.create_cross_references()
    cw = _call_names_writes(a)
    # u-c calls target(); u-t (leaf) has no calls → new [] == stored [] → skip.
    assert len(cw) == 1, "only the caller (non-empty new) writes on first pass"
    assert cw[0]["uuid"] == "u-c"
    assert set(cw[0]["properties"]["call_names"]) == {"target"}
    assert a.functions_collection is not None


def test_call_names_skipped_when_unchanged(analyzer_mod):
    """Second analyze, stored == new (order-insensitive) → ZERO call_names writes."""
    cache = {"mod.caller": "u-c", "mod.target": "u-t"}
    bodies = {"u-c": _CALLER_BODY, "u-t": _TARGET_BODY}
    # stored already has the target call, but in a different (irrelevant) order
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names={"u-c": ["target"]})
    a.create_cross_references()
    assert _call_names_writes(a) == [], "unchanged call set → no write (R1 gate)"


def test_call_names_set_compare_ignores_order(analyzer_mod):
    """MED-3: a mere read-back re-ordering must NOT force a spurious write."""
    body = "def caller():\n    a()\n    b()\n"
    cache = {"mod.caller": "u-c", "mod.a": "u-a", "mod.b": "u-b"}
    bodies = {"u-c": body, "u-a": _TARGET_BODY, "u-b": _TARGET_BODY}
    # stored order is [b, a]; extracted order is [a, b] — same SET → skip.
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names={"u-c": ["b", "a"]})
    a.create_cross_references()
    assert _call_names_writes(a) == [], "set-equal (order differs) → no write"


def test_call_names_cleared_on_empty_transition(analyzer_mod):
    """MED-2: a function that HAD calls and now has NONE must WRITE [] to clear
    the stale stored list (else callers-queries stay wrong). This is the case
    the old `if call_names:` guard silently entrenched."""
    cache = {"mod.caller": "u-c"}
    bodies = {"u-c": _TARGET_BODY}  # body has NO calls now
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names={"u-c": ["target"]})  # but stored has one
    a.create_cross_references()
    cw = _call_names_writes(a)
    assert len(cw) == 1, "non-empty→empty is a change → clear write"
    assert cw[0]["uuid"] == "u-c"
    assert cw[0]["properties"]["call_names"] == [], "must write [] to clear"


def test_call_names_changed_set_writes(analyzer_mod):
    """A genuinely-changed call set (added a call) DOES write."""
    body = "def caller():\n    target()\n    extra()\n"
    cache = {"mod.caller": "u-c", "mod.target": "u-t", "mod.extra": "u-e"}
    bodies = {"u-c": body, "u-t": _TARGET_BODY, "u-e": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names={"u-c": ["target"]})  # missing 'extra'
    a.create_cross_references()
    cw = _call_names_writes(a)
    assert len(cw) == 1 and cw[0]["uuid"] == "u-c"
    assert set(cw[0]["properties"]["call_names"]) == {"target", "extra"}


# ─────────────────── v0.2.74 (R4) incremental cross-ref scope ────────────────
# On a scoped (--only-file / --only-files-from) run, create_cross_references
# must re-parse ONLY changed-file function bodies (the READ-amp fix) while
# still rebuilding COMPLETE inbound (n_callers) counts from unchanged callers'
# STORED call_names (MED-1). These tests pass `changed_files=` explicitly.

_CALLER_BODY2 = "def caller2():\n    target()\n"


def test_r4_whole_repo_parses_everything(analyzer_mod):
    """changed_files=None (whole-repo) → every function body is fetched/parsed."""
    cache = {"mod.caller": "u-c", "mod.target": "u-t"}
    bodies = {"u-c": _CALLER_BODY, "u-t": _TARGET_BODY}
    a = _xref_analyzer(analyzer_mod, bodies, cache)
    a.create_cross_references(changed_files=None)
    # both functions fetched (parsed) in whole-repo mode
    assert set(a.functions_collection.fetched) == {"u-c", "u-t"}


def test_r4_unchanged_caller_not_reparsed_but_still_counted(analyzer_mod):
    """MED-1: an UNCHANGED caller's body is NOT re-parsed, yet its stored
    call_names still contribute to the target's n_callers so the count stays
    complete. Here only 'other.py' changed; the two callers live in unchanged
    'a.py'/'b.py' and already have call_names=[target] stored."""
    cache = {"a.caller": "u-a", "b.caller": "u-b",
             "other.fn": "u-o", "mod.target": "u-t"}
    bodies = {"u-a": _CALLER_BODY, "u-b": _CALLER_BODY2,
              "u-o": _TARGET_BODY, "u-t": _TARGET_BODY}
    file_paths = {"u-a": "a.py", "u-b": "b.py",
                  "u-o": "other.py", "u-t": "target.py"}
    stored_calls = {"u-a": ["target"], "u-b": ["target"]}
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names=stored_calls, file_paths=file_paths)
    a.create_cross_references(changed_files={"other.py"})
    # unchanged callers u-a / u-b must NOT be re-parsed (READ-amp fix)…
    assert "u-a" not in a.functions_collection.fetched
    assert "u-b" not in a.functions_collection.fetched
    # …but the target's n_callers must still be the COMPLETE 2 (both stored
    # callers counted from their stored call_names) — MED-1.
    ncw = [u for u in a.functions_collection.updates
           if "n_callers" in u["properties"]]
    assert len(ncw) == 1 and ncw[0]["uuid"] == "u-t"
    assert ncw[0]["properties"]["n_callers"] == 2


def test_r4_changed_file_reparsed_and_written(analyzer_mod):
    """A changed-file function IS re-parsed and its call_names re-written when
    its call set differs from stored."""
    changed_body = "def caller():\n    target()\n    extra()\n"
    cache = {"mod.caller": "u-c", "mod.target": "u-t", "mod.extra": "u-e"}
    bodies = {"u-c": changed_body, "u-t": _TARGET_BODY, "u-e": _TARGET_BODY}
    file_paths = {"u-c": "changed.py", "u-t": "t.py", "u-e": "e.py"}
    stored_calls = {"u-c": ["target"]}  # stored missing 'extra'
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names=stored_calls, file_paths=file_paths)
    a.create_cross_references(changed_files={"changed.py"})
    assert "u-c" in a.functions_collection.fetched, "changed file IS parsed"
    cw = [u for u in a.functions_collection.updates
          if "call_names" in u["properties"]]
    assert len(cw) == 1 and cw[0]["uuid"] == "u-c"
    assert set(cw[0]["properties"]["call_names"]) == {"target", "extra"}


def test_r4_unchanged_caller_never_writes_call_names(analyzer_mod):
    """An unchanged-file caller (skipped body-parse) must never write call_names
    (its stored value is authoritative; R1 gate would skip anyway)."""
    cache = {"a.caller": "u-a", "mod.target": "u-t"}
    bodies = {"u-a": _CALLER_BODY, "u-t": _TARGET_BODY}
    file_paths = {"u-a": "a.py", "u-t": "target.py"}
    stored_calls = {"u-a": ["target"]}
    a = _xref_analyzer(analyzer_mod, bodies, cache,
                       stored_call_names=stored_calls, file_paths=file_paths)
    a.create_cross_references(changed_files={"nothing_matches.py"})
    cw = [u for u in a.functions_collection.updates
          if "call_names" in u["properties"]]
    assert cw == [], "unchanged caller writes no call_names"


# ───────────────────────── backfill (data.update pass) ──────────────────────

from vco_lib import codegraph_resync as cr  # noqa: E402


class _BackfillColl:
    def __init__(self, rows, probe_has_null=True, iter_raises=False):
        self._rows = rows
        self.updates = []
        self._iter_raises = iter_raises
        self.iter_calls = 0
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(
                objects=([object()] if probe_has_null else [])
            )
        )
        self.data = types.SimpleNamespace(
            update=lambda uuid, properties: self.updates.append(
                {"uuid": uuid, "properties": properties}
            )
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        if self._iter_raises:
            raise RuntimeError("scan failed")
        for i, props in enumerate(self._rows):
            yield types.SimpleNamespace(uuid=f"u{i}", properties=props)


def _backfill_client(func_coll, class_coll, mod_coll):
    colls = {
        "Proj_CodeFunction": func_coll,
        "Proj_CodeClass": class_coll,
        "Proj_CodeModule": mod_coll,
    }
    return types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda n: n in colls, get=lambda n: colls[n],
        ),
        close=lambda: None,
    )


def _patch_backfill_helpers(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    monkeypatch.setattr(
        cr, "_resolve_metadata_helpers",
        lambda: (
            lambda p: p.startswith("tests/"),
            lambda body, lang: "EXTRACTED" if "/**" in body else "",
        ),
    )


def test_backfill_update_matrix(monkeypatch):
    _patch_backfill_helpers(monkeypatch)
    func = _BackfillColl([
        # empty doc + NULL is_test + canonical chunk → both stamped
        {"file_path": "tests/a.py", "is_test": None, "doc": "",
         "function_body": "x /** d */", "language": "javascript", "chunk_num": 0},
        # populated doc + populated is_test → leave alone entirely
        {"file_path": "src/b.py", "is_test": False, "doc": "have",
         "function_body": "x /** d */", "language": "javascript", "chunk_num": 0},
        # chunk-1 row: doc skipped, is_test still stamped
        {"file_path": "tests/c.py", "is_test": None, "doc": "",
         "function_body": "[chunk 2/3]\n\nx /** d */", "language": "javascript",
         "chunk_num": 1},
        # docstring-less body → is_test only (no doc write)
        {"file_path": "src/d.py", "is_test": None, "doc": "",
         "function_body": "plain body", "language": "javascript", "chunk_num": 0},
        # NULL path → is_test left NULL (fail-safe), nothing to write
        {"file_path": None, "is_test": None, "doc": "",
         "function_body": "", "language": "javascript", "chunk_num": 0},
    ])
    cls = _BackfillColl([], probe_has_null=False)   # probe short-circuit
    mod = _BackfillColl([
        {"path": "tests/m.py", "is_test": None},
    ])
    client = _backfill_client(func, cls, mod)
    counts = cr.backfill_codegraph_metadata("Proj", client=client)

    assert counts["Proj_CodeFunction"] == 3
    by_uuid = {u["uuid"]: u["properties"] for u in func.updates}
    assert by_uuid["u0"] == {"is_test": True, "doc": "EXTRACTED"}
    assert "u1" not in by_uuid
    assert by_uuid["u2"] == {"is_test": True}          # chunk-1: no doc
    assert by_uuid["u3"] == {"is_test": False}         # doc empty-extract
    assert "u4" not in by_uuid                          # NULL path leave-alone

    assert cls.iter_calls == 0, "all-populated collection must skip the scan"
    assert counts["Proj_CodeClass"] == 0
    assert mod.updates == [{"uuid": "u0", "properties": {"is_test": True}}]


def test_backfill_per_collection_error_isolated(monkeypatch):
    _patch_backfill_helpers(monkeypatch)
    func = _BackfillColl([], iter_raises=True)
    cls = _BackfillColl([
        {"file_path": "tests/k.py", "is_test": None, "doc": "",
         "class_body": "", "language": "python", "chunk_num": 0},
    ])
    mod = _BackfillColl([], probe_has_null=False)
    client = _backfill_client(func, cls, mod)
    counts = cr.backfill_codegraph_metadata("Proj", client=client)
    assert counts.get("Proj_CodeClass") == 1, (
        "one collection's failure must not wedge the others"
    )


def test_backfill_noop_without_helpers(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    monkeypatch.setattr(cr, "_resolve_metadata_helpers", lambda: (None, None))
    assert cr.backfill_codegraph_metadata("Proj", client=object()) == {}


def test_spawn_launches_backfill_child(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")
    spawned = []

    class _P:
        pid = 1

    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (spawned.append(argv), _P())[1],
    )
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    backfill = [a for a in spawned if "--backfill-metadata" in a]
    assert len(backfill) == 1
    assert "--project" in backfill[0] and "MyProj" in backfill[0]


# ─────────────────────────── 5→6 schema edge ────────────────────────────────

_EDGE_PATH = REPO_ROOT / "migrations" / "codegraph_collection" / "5_to_6.py"


def test_edge_header_directives():
    src = _EDGE_PATH.read_text(encoding="utf-8")
    assert "# @idempotent: yes" in src
    assert "# @destructive: no" in src
    assert "# @classification: derived" in src


def test_edge_scope_matches_ensure_helpers():
    """X-1 (v0.2.76): the edge's scope is _V6_PROPS; the class projection now
    comes from the shared home ``specs_subset(_V6_PROPS)`` (the inline
    _FALLBACK_SPECS was removed — the edge imports vco_lib.codegraph_schema
    directly). The full parity lock is tests/test_codegraph_schema_parity.py;
    this pins the M1/M4 class scope locally."""
    from vco_lib.codegraph_schema import specs_subset

    edge = _load_module("_v0273_edge_5_to_6", _EDGE_PATH)
    assert edge._V6_PROPS == ("is_test", "n_callers")
    subset = specs_subset(edge._V6_PROPS)
    is_test_classes = tuple(
        cls for cls, specs in subset.items()
        if any(p == "is_test" for p, _t, _d in specs)
    )
    n_callers_classes = tuple(
        cls for cls, specs in subset.items()
        if any(p == "n_callers" for p, _t, _d in specs)
    )
    assert is_test_classes == ("CodeModule", "CodeClass", "CodeFunction")
    assert n_callers_classes == ("CodeFunction",)


def test_edge_ensure_props_bool_and_int_idempotent():
    """X-1 (v0.2.76): the edge's ``_ensure_props`` (now a DIRECT import of
    vco_lib.codegraph_schema.ensure_codegraph_properties — no inline fallback)
    adds BOOL + INT props and skips already-present ones."""
    edge = _load_module("_v0273_edge_5_to_6b", _EDGE_PATH)
    coll = _EnsureColl()
    client = types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda name: name == "P_CodeFunction",
            get=lambda name: coll,
        )
    )
    results = edge._ensure_props(client, "P")
    assert coll.added == ["is_test", "n_callers"]
    assert results["P_CodeFunction"] == "ensured"
    assert results["P_CodeModule"] == "absent"

    have = _EnsureColl(has_props=("is_test", "n_callers"))
    client2 = types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda name: True,
            get=lambda name: have,
        )
    )
    edge._ensure_props(client2, "P")
    assert have.added == [], "idempotent"


def test_edge_no_env_is_success(monkeypatch):
    edge = _load_module("_v0273_edge_5_to_6c", _EDGE_PATH)
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    assert edge.main() == 0


def test_edge_weaviate_down_defers(monkeypatch):
    edge = _load_module("_v0273_edge_5_to_6d", _EDGE_PATH)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "Proj")

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(edge, "_connect", _boom)
    assert edge.main() == 1, "unreachable Weaviate must NOT advance the version"
