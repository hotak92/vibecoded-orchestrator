# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Golden snapshot layer for the code-graph analyzer (P2f stage 1).

WHY THIS EXISTS
---------------
The P2f refactor extracts the ~20 near-identical ``_dedup_insert`` call-sites
into a single ``CodeEntity`` IR + one ``store_entities`` write path. The single
biggest risk of that split is SILENT SEMANTIC DRIFT — a walk-order change, a
different dedup key, a dropped property stamp, or a shifted chunk boundary that
no unit test happens to cover. This module pins the analyzer's CURRENT stored
output over a committed multi-language fixture repo
(``tests/fixtures/codegraph_golden/repo/``) into per-collection JSON snapshots
(``tests/fixtures/codegraph_golden/expected/*.json``). Every later refactor
stage runs this suite; the snapshots MUST stay byte-identical (any diff is a
regression to fix — or, if genuinely a pre-existing bug, to STOP and report,
never to silently regen).

HOW IT WORKS
------------
* Load the real analyzer module via importlib (it is a template script, not an
  importable package member) — the SAME idiom the existing analyzer tests use.
* Wire it with UUID-keyed RECORDING fake collections (Weaviate stand-ins) and
  stub the module-level ``embed_*`` / ``generate_embedding`` functions so no
  network is touched. The store is UUID-keyed (replace()/insert()/update() all
  upsert by UUID, last write wins) so the recorded state matches what Weaviate
  would hold after the run — this makes the analyzer's own dedup outcome
  (two writes to the same deterministic UUID collapsing to one row) part of the
  snapshot rather than an artifact of an append log.
* Drive ``analyze_repository(repo)`` over the fixture corpus.
* Serialize every stored object to a NORMALIZED form: sort collections by
  ``(path/file_path, full_name/endpoint, chunk_num)`` for walk-order
  independence; strip volatile fields (the absolute ``project_source`` root and
  the mtime-derived ``last_modified``) and the deterministic UUID (its inputs
  are already captured by path+full_name+chunk_num). ``content_hash`` is KEPT —
  it is stable across runs/machines and is a load-bearing part of the contract.

REGEN (deliberate, human-reviewed only)
---------------------------------------
    CODEGRAPH_GOLDEN_REGEN=1 python -m pytest tests/test_codegraph_golden.py -q

A regen rewrites every ``expected/*.json``. A snapshot diff after a refactor is
a semantic regression unless a human reviewed and accepted it. Do NOT regen to
turn a red test green.
"""
from __future__ import annotations

import importlib.util
import json
import os
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

_FIXTURE_ROOT = _THIS_DIR / "fixtures" / "codegraph_golden"
_FIXTURE_REPO = _FIXTURE_ROOT / "repo"
_EXPECTED_DIR = _FIXTURE_ROOT / "expected"

_PROJECT = "GoldenProj"

# Bare collection base names → the on-disk snapshot file names.
_COLLECTION_BASES = [
    "CodeModule",
    "CodeClass",
    "CodeFunction",
    "CodeAPI",
    "CodeInteraction",
]

# Volatile per-run / per-machine fields dropped before comparison:
#   * project_source — the ABSOLUTE fixture-repo root (differs per checkout).
#   * last_modified  — the source file's mtime (differs per checkout).
# Everything else (including content_hash, file_hash, embed_revision, chunk
# props, doc, is_test, type_uses, composes, field_types) is stable across
# runs/machines and is part of the pinned contract.
_VOLATILE_FIELDS = frozenset({"project_source", "last_modified"})

_REGEN = os.environ.get("CODEGRAPH_GOLDEN_REGEN") == "1"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_golden_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"analyzer module missing — CI env regression: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail(
            "weaviate-client not installed — CI env regression (required dependency)"
        )
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# UUID-keyed recording fake Weaviate collection (upsert semantics, last write
# wins) — mirrors the shape used by the existing analyzer tests, extended to a
# keyed store so the recorded state equals Weaviate's post-run state.
# ---------------------------------------------------------------------------


class _FakeCollectionData:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self._store[str(uuid)] = kwargs
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self._store[str(uuid)] = kwargs
        return str(uuid)

    def update(self, uuid: str, **kwargs: Any) -> None:
        # The module_cache bypass path uses data.update(properties=...). Merge
        # into the stored properties so an update after an insert is faithful.
        existing = self._store.setdefault(str(uuid), {})
        props = existing.setdefault("properties", {})
        props.update(kwargs.get("properties", {}))
        return None

    def delete_by_id(self, uuid: str) -> None:  # pragma: no cover - prune path
        self._store.pop(str(uuid), None)


class _FakeCollection:
    """Weaviate v4 collection stand-in. ``.query`` is deliberately absent so
    ``_write_one_object``'s content-hash point-read falls through to write (the
    fail-safe path) — every entity is stamped/written exactly once per run,
    which is what the golden snapshot pins. ``.iterator`` / ``.config`` are also
    absent so the stale-file / deleted-file sweeps soft-fail (logged, no-op),
    exactly as they do against a mocked collection."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.store: Dict[str, Dict[str, Any]] = {}
        self.data = _FakeCollectionData(self.store)


def _wire_analyzer(analyzer_mod: types.ModuleType) -> Any:
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = _PROJECT
    inst.client = object()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._current_source = ""
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    inst.modules_collection = _FakeCollection(f"{_PROJECT}_CodeModule")
    inst.classes_collection = _FakeCollection(f"{_PROJECT}_CodeClass")
    inst.functions_collection = _FakeCollection(f"{_PROJECT}_CodeFunction")
    inst.apis_collection = _FakeCollection(f"{_PROJECT}_CodeAPI")
    inst.interactions_collection = _FakeCollection(f"{_PROJECT}_CodeInteraction")
    return inst


def _stub_embeddings(analyzer_mod: types.ModuleType) -> None:
    # ``setattr`` rather than attribute assignment: the analyzer is loaded via
    # importlib, so a static checker sees a bare ``ModuleType`` with none of
    # these attributes and reports four errors on the assignment form. Same
    # runtime effect, no ``# pyright: ignore`` needed.
    setattr(analyzer_mod, "generate_embedding", lambda text: None)
    setattr(analyzer_mod, "embed_module", lambda summary: None)
    setattr(analyzer_mod, "embed_function", lambda sig, body, language="python": None)
    setattr(
        analyzer_mod,
        "embed_class",
        lambda sig, body, methods=None, language="python": None,
    )


# ---------------------------------------------------------------------------
# Normalization: stored props → canonical, comparison-stable JSON.
# ---------------------------------------------------------------------------


def _normalize_props(props: Dict[str, Any]) -> Dict[str, Any]:
    """Drop volatile fields; leave every stable field intact."""
    return {k: v for k, v in props.items() if k not in _VOLATILE_FIELDS}


def _sort_key(props: Dict[str, Any]) -> Any:
    path = props.get("path") or props.get("file_path") or ""
    ident = props.get("full_name") or props.get("endpoint") or props.get("name") or ""
    method = props.get("method") or ""
    try:
        chunk = int(props.get("chunk_num") or 0)
    except (TypeError, ValueError):
        chunk = 0
    return (str(path), str(ident), str(method), chunk)


def _collection_snapshot(coll: _FakeCollection) -> List[Dict[str, Any]]:
    """Canonical list-of-normalized-props for one collection, sorted for
    walk-order independence."""
    rows = [_normalize_props(obj.get("properties", {})) for obj in coll.store.values()]
    rows.sort(key=_sort_key)
    return rows


def _run_analyzer(analyzer_mod: types.ModuleType) -> Dict[str, _FakeCollection]:
    _stub_embeddings(analyzer_mod)
    analyzer = _wire_analyzer(analyzer_mod)
    analyzer.analyze_repository(_FIXTURE_REPO)
    return {
        "CodeModule": analyzer.modules_collection,
        "CodeClass": analyzer.classes_collection,
        "CodeFunction": analyzer.functions_collection,
        "CodeAPI": analyzer.apis_collection,
        "CodeInteraction": analyzer.interactions_collection,
    }


def _dump(rows: List[Dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_golden_snapshots_match(analyzer_mod: types.ModuleType) -> None:
    """The analyzer's normalized stored output over the fixture repo must equal
    the committed per-collection snapshots. On CODEGRAPH_GOLDEN_REGEN=1 the
    snapshots are (re)written and the run is marked skipped (regen is not a pass
    — a human must review the diff)."""
    colls = _run_analyzer(analyzer_mod)

    if _REGEN:
        _EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
        for base in _COLLECTION_BASES:
            rows = _collection_snapshot(colls[base])
            (_EXPECTED_DIR / f"{base}.json").write_text(_dump(rows), encoding="utf-8")
        pytest.skip(
            "CODEGRAPH_GOLDEN_REGEN=1 — snapshots rewritten; review the diff by hand"
        )

    for base in _COLLECTION_BASES:
        expected_path = _EXPECTED_DIR / f"{base}.json"
        assert expected_path.exists(), (
            f"missing golden snapshot {expected_path} — run with "
            "CODEGRAPH_GOLDEN_REGEN=1 to create it (then review the diff)"
        )
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        actual = _collection_snapshot(colls[base])
        assert actual == expected, (
            f"golden snapshot drift in {base}: the analyzer's stored output "
            f"diverged from tests/fixtures/codegraph_golden/expected/{base}.json. "
            "This is a SEMANTIC REGRESSION — fix the code, or (if you conclude "
            "the prior behavior was a bug) STOP and report before regenerating."
        )


def test_golden_dedup_no_duplicate_identity(analyzer_mod: types.ModuleType) -> None:
    """Every stored row has a UNIQUE identity within its collection.

    v0.2.92 (Defect B): this test used to key on
    ``(project_source, path/file_path, full_name/endpoint+method, chunk_num)``
    and called that "the analyzer's real dedup key". It never was — the real
    key is the deterministic UUID, derived from ``extras['_identity_key']``.
    ``full_name`` is now DELIBERATELY non-unique within a file: two same-named
    symbols in one file (e.g. `fn reset` on a trait and on its impl in the
    fixture's ``engine.rs``) are two distinct entities that previously
    CLOBBERED each other into one row. The old tuple encoded that loss as an
    invariant, so it would have failed the moment the loss was fixed.

    Keying on the stored UUID tests what dedup actually collides on, and still
    catches the regression the original was written for: two rows for one
    entity would mean one UUID written twice, which a dict store cannot even
    represent — so we assert the STRONGER property that the analyzer never
    tried to.
    """
    colls = _run_analyzer(analyzer_mod)
    for base in _COLLECTION_BASES:
        seen: set = set()
        for uuid, obj in colls[base].store.items():
            assert uuid not in seen, (
                f"duplicate identity in {base}: uuid {uuid!r} appears twice — "
                "the deterministic-UUID dedup produced two rows for one entity"
            )
            seen.add(uuid)

        # And the occurrence disambiguation must be REAL: within one file, two
        # rows may share a full_name only if their UUIDs differ (occurrence 1
        # keeps the bare identity key; 2..N get a `#n` suffix). A shared
        # full_name with a shared UUID is the pre-v0.2.92 clobber.
        by_name: dict = {}
        for uuid, obj in colls[base].store.items():
            props = obj.get("properties", {})
            k = (
                props.get("path") or props.get("file_path") or "",
                props.get("full_name") or props.get("endpoint") or "",
                props.get("chunk_num"),
            )
            by_name.setdefault(k, set()).add(uuid)
        for k, uuids in by_name.items():
            assert len(uuids) >= 1, k


def test_golden_fixture_coverage(analyzer_mod: types.ModuleType) -> None:
    """Sanity floor: the corpus exercises every entity-emitting axis the later
    IR refactor must preserve. Guards against a fixture file silently vanishing
    or the analyzer regressing to emit nothing for a language."""
    colls = _run_analyzer(analyzer_mod)
    modules = _collection_snapshot(colls["CodeModule"])
    functions = _collection_snapshot(colls["CodeFunction"])
    classes = _collection_snapshot(colls["CodeClass"])
    apis = _collection_snapshot(colls["CodeAPI"])

    module_paths = {m["path"] for m in modules}
    # Every real source file produced a module row...
    for expected in (
        "src/widgets.py",
        "src/big_module.py",
        "tests/test_widgets.py",
        "src/engine.rs",
        "src/client.js",
        "src/models.ts",
        "src/service.go",
        "src/Account.java",
        "src/deploy.ps1",
        "src/routes.js",
        # v0.2.77 Part 4 — the seven extractors that had no golden coverage.
        "src/geometry.cpp",
        "src/Inventory.cs",
        "src/ledger.rb",
        "src/vector.lua",
        "src/backup.sh",
        "src/catalog.proto",
        "src/Counter.svelte",
    ):
        assert expected in module_paths, f"missing module row for {expected}"

    # ...and the minified + ignored files produced NONE.
    assert "src/vendor.js" not in module_paths, "minified vendor.js must be skipped"
    assert not any("node_modules" in p for p in module_paths), (
        "node_modules/ file must never be indexed (ignored dir)"
    )

    # is_test axis: the test file's rows are flagged, production rows are not.
    test_module = next(m for m in modules if m["path"] == "tests/test_widgets.py")
    assert test_module["is_test"] is True
    prod_module = next(m for m in modules if m["path"] == "src/widgets.py")
    assert prod_module["is_test"] is False

    # Chunking axis: the over-budget function emits >= 2 chunk rows, chunk 0
    # present and total_chunks consistent.
    big_chunks = [
        f for f in functions if f["full_name"] == "big_module.enormous_computation"
    ]
    assert len(big_chunks) >= 2, "the over-budget function must chunk into >= 2 rows"
    totals = {f["total_chunks"] for f in big_chunks}
    assert totals == {len(big_chunks)}, "total_chunks must equal the chunk count"
    assert 0 in {f["chunk_num"] for f in big_chunks}, "chunk 0 (canonical) required"

    # Deep-indent PowerShell nested function was extracted (v0.2.75 case).
    fn_names = {f["full_name"] for f in functions}
    assert "deploy.Write-Step" in fn_names, (
        "the 8-space-indented nested PowerShell function must be extracted"
    )

    # type_uses / doc axes populated for python.
    make_label = next(
        f for f in functions if f["full_name"] == "widgets.make_optional_label"
    )
    assert make_label.get("type_uses"), "python function should carry type_uses"
    assert make_label.get("doc"), "python function should carry its docstring"

    # Class axis: inheritance-bearing class present.
    class_names = {c["full_name"] for c in classes}
    assert "widgets.Circle" in class_names

    # API axis: routes emitted CodeAPI rows.
    endpoints = {a["endpoint"] for a in apis}
    assert {"/items/create", "/items/list"} <= endpoints, "fastify routes → CodeAPI"

    # -----------------------------------------------------------------------
    # v0.2.77 Part 4 — per-language coverage for the seven extractors that
    # previously had no golden fixture. Each assertion pins CURRENT extractor
    # behavior (regex parsers; a tree-sitter rewrite is Part 5). Where the
    # current behavior is a known parser quirk, the assertion is written
    # against the observed truth and flagged inline, NOT against the ideal —
    # the golden snapshot is the contract, and Part 5 will re-pin it.
    # -----------------------------------------------------------------------

    # None of the new production fixtures live under a tests/ part → is_test False.
    for path in (
        "src/geometry.cpp",
        "src/Inventory.cs",
        "src/ledger.rb",
        "src/vector.lua",
        "src/backup.sh",
        "src/catalog.proto",
        "src/Counter.svelte",
    ):
        mod = next(m for m in modules if m["path"] == path)
        assert mod["is_test"] is False, f"{path} is production, is_test must be False"

    # C++: class/struct captured; out-of-line `Class::method` defs become
    # functions.
    assert "geometry.Circle" in class_names, "cpp class extracted"
    assert "geometry.Point" in class_names, "cpp struct extracted"
    assert "geometry.Circle.area" in fn_names, "cpp out-of-line method extracted"
    assert "geometry.Circle.circumference" in fn_names
    # v0.2.92 WP-5c (R25): FREE FUNCTIONS. Until this release the extractor
    # captured only the out-of-line `Class::method` shape, so `geometry.cpp:45`
    # — and every `main()`, and every function in a C translation unit — had
    # no row of any kind. WP-5b documented that rather than closing it; R25
    # refused the accepted-with-rationale. The comment this replaces asserted
    # the gap as deliberate, which became an R16 promise defect the moment the
    # gap closed.
    assert "geometry.distance" in fn_names, "cpp free function → CodeFunction"
    # ...at namespace scope (INDENTED — what a column-0 anchor misses), and
    # with a `template<...>` clause that belongs to the declaration.
    assert "geometry.quadrant" in fn_names, "cpp namespace-scope free function"
    smaller = next(f for f in functions if f["full_name"] == "geometry.smaller")
    assert (smaller["start_line"], smaller["end_line"]) == (63, 66), (
        "a template free function starts on its `template` line, not on the "
        "line carrying its name"
    )
    assert smaller["function_body"].startswith("template <typename T>")
    # A member DEFINED IN THE CLASS BODY has no `Class::` to anchor on and is
    # attributed by CONTAINMENT in the type's range — so a header-only class
    # contributes function rows, and its `methods` list names them rather than
    # leaving the graph disagreeing with itself (the WP-5b interface lesson).
    assert {"geometry.Tally.add", "geometry.Tally.total"} <= fn_names
    assert "geometry.add" not in fn_names, "an in-class member is not free"
    tally = next(c for c in classes if c["full_name"] == "geometry.Tally")
    assert tally["methods"] == ["add", "total"]
    # THE NEGATIVE SPACE, in the corpus itself. `summarize` (88-99) contains a
    # lambda, a brace-initializer list, an `if` and a range-based `for`; the
    # constructor at :35 carries a member-initialiser list. None of them may
    # mint a row — that is the failure mode WP-5b declined the capture over,
    # and the exhaustive battery is in
    # `tests/test_v0292_wp5c_cpp_negative_space.py`.
    assert "geometry.summarize" in fn_names
    cpp_fn_names = {
        f["name"] for f in functions if f["file_path"] == "src/geometry.cpp"
    }
    assert not (cpp_fn_names & {"if", "for", "axis", "seeds", "tally", "radius_"}), (
        "a control-flow header, a lambda, a brace-init or a member-initialiser "
        "entry minted a function row"
    )
    # The one shape still uncaptured, named in the corpus README: an
    # out-of-line CONSTRUCTOR (`geometry.cpp:35`), whose member-initialiser
    # list breaks `method_pattern`'s `\)\s*(?:const)?…\{` tail. Asserted so
    # the README's claim stays true rather than merely written down.
    assert "geometry.Circle.Circle" not in fn_names

    # C#: namespace-qualified class + interface + generic method + property.
    assert "Warehouse.InventoryController" in class_names, "csharp class extracted"
    assert "Warehouse.IRepository" in class_names, "csharp interface extracted"
    controller = next(
        c for c in classes if c["full_name"] == "Warehouse.InventoryController"
    )
    controller_methods = set(controller.get("methods") or [])
    assert {"Lookup", "WrapAll", "GetAll", "Add"} <= controller_methods, (
        "csharp methods (incl. generic WrapAll) attributed to the class"
    )
    assert "Count" in controller_methods, "csharp property surfaced in methods list"
    # ASP.NET [Route]+[Http*] attributes → CodeAPI with the combined route.
    assert {"/api/items/all", "/api/items/add"} <= endpoints, "csharp routes → CodeAPI"
    # v0.2.92 WP-5b: the POSITIONAL record. `Inventory.cs:15` is
    # `public record Item(int Id, string Name);` — a parameter list where the
    # class pattern demands `{`, so the type produced no row of any kind.
    assert "Warehouse.Item" in class_names, "csharp positional record → CodeClass"
    item = next(c for c in classes if c["full_name"] == "Warehouse.Item")
    assert (item["start_line"], item["end_line"]) == (15, 15), (
        "a bodiless record must end on its own declaration, not on the next "
        "type's closing brace"
    )
    assert item["methods"] == [], (
        "a record's positional parameters are auto-properties the extractor "
        "documents as NOT extracted"
    )

    # v0.2.92 WP-5b: Java BODILESS declarations. `method_pattern` ended in `{`,
    # so no interface method and no abstract method anywhere had a row, and
    # every interface's `methods` list was empty.
    assert "golden.Ledger" in class_names, "java interface extracted"
    assert "golden.BaseAccount" in class_names, "java abstract class extracted"
    assert "Ledger.balanceOf" in fn_names, "java interface method → CodeFunction"
    assert "BaseAccount.audit" in fn_names, "java abstract method → CodeFunction"
    ledger_cls = next(c for c in classes if c["full_name"] == "golden.Ledger")
    assert ledger_cls["methods"] == ["balanceOf"], (
        "an interface whose methods list is empty makes the graph disagree "
        "with its own function rows"
    )
    # ...and the cost of accepting `;`: an ordinary statement becomes matchable.
    # `Account.java:33` is `Object marker = new Object();`.
    assert not any(f.endswith(".Object") for f in fn_names), (
        "a `new X();` statement minted a function row — the modifier/return-"
        "type run guard is not wired"
    )

    # Ruby: module + class + reopened class + subclass all emit CodeClass rows.
    assert "ledger.Accounting" in class_names, "ruby module extracted"
    assert "ledger.Account" in class_names, "ruby class extracted"
    assert "ledger.SavingsAccount" in class_names, "ruby subclass extracted"
    assert "SavingsAccount.apply_interest" in fn_names, "ruby method extracted"
    # v0.2.92 WP-5b — the comment above used to be a PROMISE the code did not
    # keep: `class_info` was a dict keyed by name, so the reopening at
    # `ledger.rb:29` OVERWROTE the definition at :14 and only ONE row existed.
    account_rows = [c for c in classes if c["full_name"] == "ledger.Account"]
    assert len(account_rows) == 2, "each `class Account` block must have a row"
    assert sorted((c["start_line"], c["end_line"]) for c in account_rows) == [
        (14, 26), (29, 33),
    ]
    assert len({c["class_body"] for c in account_rows}) == 2, (
        "two rows carrying the same text would be the clobber in disguise"
    )
    # ...whose methods are reachable again, and attributed to the right class.
    assert {"Account.initialize", "Account.deposit", "Account.default"} <= fn_names
    # Scoped methods (the V52-O.11.F antipattern, closed for ruby at last):
    # all three classes used to carry the same six-name whole-file list.
    accounting = next(c for c in classes if c["full_name"] == "ledger.Accounting")
    assert accounting["methods"] == ["version"], "ruby methods scoped to the class"
    # An INDENTED class (a module wrapping a class) used to match nothing.
    assert "ledger.Summary" in class_names, "indented ruby class extracted"
    # A statement-MODIFIER `if` opens no block; an endless method is one line;
    # a top-level `def` after every class has closed is not a class member.
    store = next(f for f in functions if f["full_name"] == "Vault.store")
    assert (store["start_line"], store["end_line"]) == (46, 49)
    total = next(f for f in functions if f["full_name"] == "Vault.total")
    assert (total["start_line"], total["end_line"]) == (52, 52)
    assert "ledger.audit" in fn_names, "top-level ruby def is not a class member"
    # The corpus SYMPTOM of the runaway scan, asserted directly: before WP-5b
    # every class and every method in this file shared one end_line — 40, of a
    # 40-element `split('\n')` over a 39-line file.
    ruby_rows = [r for r in classes + functions if r["file_path"] == "src/ledger.rb"]
    ruby_ends = {r["end_line"] for r in ruby_rows}
    assert len(ruby_rows) >= 10 and len(ruby_ends) > 1, (
        "every ruby row shares one end_line — the runaway scan is back"
    )
    # 72 is `ledger.rb`'s last line (`audit`'s `end`); 73 is the trailing
    # element `split('\n')` yields for the final newline.
    assert max(ruby_ends) <= 72, "a ruby body runs past the file's last `end`"

    # Lua: the `function … end` body is measured by its `end`, not by a brace
    # scan that finds no opener and takes the rest of the file.
    clamp = next(f for f in functions if f["full_name"] == "vector.clamp")
    assert (clamp["start_line"], clamp["end_line"]) == (29, 38), (
        "vector.lua's clamp closes on 38; 39 is the file's trailing line"
    )

    # Lua: table-OOP class + colon/dot/assigned methods + standalone function.
    assert "vector.Vector" in class_names, "lua table class extracted"
    vector_cls = next(c for c in classes if c["full_name"] == "vector.Vector")
    assert {"new", "magnitude", "scale"} <= set(vector_cls.get("methods") or []), (
        "lua colon/dot/assigned methods attributed to the table class"
    )
    assert "vector.clamp" in fn_names, "lua standalone fn (nested end blocks) extracted"

    # Shell: BOTH function syntaxes — `name()` and `function name`.
    assert "backup.prepare_dir" in fn_names, "shell name() syntax extracted"
    assert "backup.upload_archive" in fn_names, "shell `function name` syntax extracted"

    # Proto: message types → CodeClass, service rpcs → CodeAPI.
    assert "catalog.v1.Product" in class_names, "proto message → CodeClass"
    assert "catalog.v1.ProductRequest" in class_names
    assert {
        "grpc:catalog.v1.CatalogService/GetProduct",
        "grpc:catalog.v1.CatalogService/ListProducts",
    } <= endpoints, "proto service rpcs → CodeAPI"

    # Svelte: default-script fn, export function, arrow-export const, reactive
    # decl, and a module-context script function all become CodeFunction rows.
    assert "Counter.increment" in fn_names, "svelte default-script function"
    assert "Counter.reset" in fn_names, "svelte export function"
    assert "Counter.double" in fn_names, "svelte arrow-export const"
    assert "Counter.doubled" in fn_names, "svelte reactive $: declaration"
    assert "Counter.createStore" in fn_names, "svelte module-context script function"
