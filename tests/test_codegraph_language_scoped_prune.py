# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression + unit tests for v0.2.18 Plan C — language-scoped prune.

Covers:
  - `language` property is declared on CodeClass / CodeFunction / CodeAPI /
    CodeInteraction in the minimal `code_class_definitions` shape.
  - The analyzer writes the canonical `language` ID on every insert
    (Python, JavaScript exercised; the dispatcher's auto-stamping path
    covers the others uniformly).
  - The prune filter is scoped correctly when `language=` is set:
    rows tagged with that language are pruned; rows tagged with other
    languages are preserved.
  - The legacy (no-language) prune path still walks every row.
  - CodeInteraction's `language` is the SOURCE-SIDE language and a Python
    file calling Go gRPC produces a row with `language="python"`.
  - The `_ensure_language_property` helper adds the property on existing
    v0.2.17 collections without it.
  - v0.2.66 (Bug 3): the analyzer's `_dispatch_name_for_file` maps an
    edited file to its lang_dispatch name, and the per-edit hook passes
    `--only-file "$EDITED_FILE"` instead of the old `--incremental`.

All tests are pure-Python unit tests against the analyzer module's
helpers and don't require a running Weaviate.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, List

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_HOOK_PATH = _REPO_ROOT / "templates" / "hooks" / "code-graph-incremental.sh"


def _load_analyzer_module() -> types.ModuleType:
    """Load the analyzer script as a module without sys.path side-effects."""
    spec = importlib.util.spec_from_file_location(
        "_planc_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module file missing from repo — CI env regression: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client package not installed — CI env regression (required dependency missing)")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# Minimal fakes (mirror those in test_analyze_code_graph_v0_2_16.py).
# ---------------------------------------------------------------------------


class _FakeCollectionData:
    def __init__(self) -> None:
        self.replace_calls: List[dict] = []

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self.replace_calls.append({"uuid": uuid, **kwargs})
        return None

    def delete_by_id(self, uuid: str) -> None:
        pass


class _FakeCollection:
    """Stand-in for a Weaviate v4 collection object."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.data = _FakeCollectionData()


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "TestProject"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._progress_emitter = None
    inst._prune_language = ""
    return inst


# ---------------------------------------------------------------------------
# Test 1 — `language` property declared on the 4 affected code classes.
# ---------------------------------------------------------------------------


def test_language_property_added_to_codeclass_codefunction_codeapi_codeinteraction() -> None:
    """Plan C: code_class_definitions exposes `language` on every code
    class so the smart-dispatch patch_props action picks it up on update."""
    sys.path.insert(0, str(_REPO_ROOT))
    try:
        from vco_lib.project_init import code_class_definitions
    finally:
        # Keep test isolation; the import is otherwise harmless.
        pass

    defs = code_class_definitions(project_prefix="TestProj_")
    assert set(defs.keys()) >= {
        "CodeModule",
        "CodeClass",
        "CodeFunction",
        "CodeAPI",
        "CodeInteraction",
    }, f"Unexpected code-class basenames: {set(defs)}"

    for basename, dfn in defs.items():
        prop_names = {p.get("name") for p in dfn.get("properties", [])}
        assert "language" in prop_names, (
            f"{basename} schema missing `language` property — Plan C "
            f"migrate-collections patch_props won't propagate the field "
            f"to existing v0.2.17 collections. Found props: {prop_names}"
        )


# ---------------------------------------------------------------------------
# Test 2 — analyzer writes canonical language on insert (Python path).
# ---------------------------------------------------------------------------


def test_analyzer_writes_language_on_insert_python(analyzer_mod) -> None:
    """The dispatcher sets `_current_language = lang_name` before the
    per-file loop; `_dedup_insert` then stamps it on every property dict.
    """
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._current_language = "python"

    fake_coll = _FakeCollection("Test_CodeFunction")
    insert_params = {"properties": {"name": "foo", "full_name": "mod.foo"}}
    analyzer._dedup_insert(
        fake_coll, insert_params, "mod.foo", file_path_rel="src/mod.py"
    )

    assert len(fake_coll.data.replace_calls) == 1
    written = fake_coll.data.replace_calls[0]
    props = written["properties"]
    assert props.get("language") == "python", (
        f"Plan C: _dedup_insert must stamp canonical language ID. "
        f"Got language={props.get('language')!r}, expected 'python'."
    )


def test_analyzer_writes_language_on_insert_typescript(analyzer_mod) -> None:
    """Same as the Python case but for TypeScript — both go through the
    same `_dedup_insert` path so this is essentially a smoke test that
    the canonical-ID lookup table covers more than just Python."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._current_language = "typescript"

    fake_coll = _FakeCollection("Test_CodeClass")
    insert_params = {"properties": {"name": "Bar", "full_name": "mod.Bar"}}
    analyzer._dedup_insert(
        fake_coll, insert_params, "mod.Bar", file_path_rel="src/mod.ts"
    )

    written = fake_coll.data.replace_calls[0]
    assert written["properties"].get("language") == "typescript"


def test_analyzer_preserves_explicit_language_on_insert(analyzer_mod) -> None:
    """If a caller pre-set `language` in props, `_dedup_insert` must NOT
    overwrite it with `_current_language`. This guards CodeModule, which
    explicitly threads its language string from _create_or_update_module."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._current_language = "python"

    fake_coll = _FakeCollection("Test_CodeModule")
    insert_params = {
        "properties": {
            "path": "src/x.py",
            "language": "rust",  # explicit override
        },
    }
    analyzer._dedup_insert(
        fake_coll, insert_params, "module::src/x.py", file_path_rel="src/x.py"
    )

    written = fake_coll.data.replace_calls[0]
    assert written["properties"].get("language") == "rust", (
        "Explicit language must override dispatcher-set _current_language; "
        "otherwise CodeModule's display-string-from-caller pattern breaks."
    )


# ---------------------------------------------------------------------------
# Test 3 — _canonical_lang_id normalises display labels to canonical IDs.
# ---------------------------------------------------------------------------


def test_canonical_lang_id_normalises_display_labels(analyzer_mod) -> None:
    """Both display labels and canonical IDs must map to the canonical ID
    so the prune filter recognises legacy mixed-case rows."""
    cases = [
        # (input, expected)
        ("Python", "python"),
        ("python", "python"),
        ("C#", "csharp"),
        ("c#", "csharp"),
        ("csharp", "csharp"),
        ("C++", "cpp"),
        ("c++", "cpp"),
        ("cpp", "cpp"),
        ("JavaScript", "javascript"),
        ("javascript", "javascript"),
        ("js", "javascript"),
        ("TypeScript", "typescript"),
        ("ts", "typescript"),
        ("Go", "go"),
        ("Rust", "rust"),
        ("Lua", "lua"),
        ("Java", "java"),
        ("Ruby", "ruby"),
        ("Shell", "shell"),
        ("Proto", "proto"),
    ]
    for inp, expected in cases:
        got = analyzer_mod._canonical_lang_id(inp)
        assert got == expected, f"_canonical_lang_id({inp!r}) -> {got!r}, expected {expected!r}"

    # Empty / None → empty string (used by prune as "unknown lang, preserve")
    assert analyzer_mod._canonical_lang_id("") == ""
    assert analyzer_mod._canonical_lang_id(None) == ""

    # Unknown strings pass through lowercased (conservative — won't
    # match a known prune scope, so the row survives).
    assert analyzer_mod._canonical_lang_id("Klingon") == "klingon"


# ---------------------------------------------------------------------------
# Test 4 — language-scoped prune filters correctly.
# ---------------------------------------------------------------------------


def test_prune_stale_with_language_filters_correctly(analyzer_mod) -> None:
    """Pre-populate the fake collection with Python + Go rows. Run a
    language-scoped prune for `python` after visiting only some Python
    UUIDs. The unvisited-Python rows must be deleted; the Go rows must
    survive.
    """
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    fake = _FakeCollection("Test_CodeFunction")
    deleted_uuids: List[str] = []

    class _Obj:
        def __init__(self, uid: str, project: str, language: str) -> None:
            self.uuid = uid
            self.properties = {"project": project, "language": language}

    pre_existing = [
        _Obj("py-1-visited", "TestProject", "python"),
        _Obj("py-2-stale", "TestProject", "python"),
        _Obj("py-3-stale", "TestProject", "Python"),  # legacy mixed-case row
        _Obj("go-1", "TestProject", "go"),
        _Obj("go-2", "TestProject", "go"),
    ]

    def _iterator(return_properties=None):
        return iter(pre_existing)

    def _delete_by_id(uuid: str) -> None:
        deleted_uuids.append(uuid)

    fake.iterator = _iterator  # type: ignore[attr-defined]
    fake.data.delete_by_id = _delete_by_id  # type: ignore[method-assign]

    # Visited only one Python row this run.
    pruned = analyzer._prune_collection(
        fake,
        visited_uuids={"py-1-visited"},
        language_scope="python",
    )

    assert pruned == 2, (
        f"Expected exactly 2 unvisited Python rows pruned, got {pruned}. "
        f"Deleted UUIDs: {deleted_uuids}"
    )
    assert set(deleted_uuids) == {"py-2-stale", "py-3-stale"}, (
        f"Wrong UUIDs deleted: {deleted_uuids}. Go rows must survive a "
        "language-scoped prune; legacy mixed-case 'Python' rows MUST be "
        "recognised as matching the canonical 'python' scope."
    )


def test_prune_stale_without_language_global_behavior(analyzer_mod) -> None:
    """When language_scope is empty (legacy global prune), every
    unvisited row in this project is candidate for deletion regardless of
    language. This guards against accidentally narrowing the legacy
    behaviour during the Plan C migration."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    fake = _FakeCollection("Test_CodeFunction")
    deleted_uuids: List[str] = []

    class _Obj:
        def __init__(self, uid: str, project: str, language: str = "") -> None:
            self.uuid = uid
            self.properties = {"project": project, "language": language}

    pre_existing = [
        _Obj("a-visited", "TestProject", "python"),
        _Obj("b-stale-py", "TestProject", "python"),
        _Obj("c-stale-go", "TestProject", "go"),
        _Obj("d-stale-nolang", "TestProject", ""),
    ]

    def _iterator(return_properties=None):
        return iter(pre_existing)

    def _delete_by_id(uuid: str) -> None:
        deleted_uuids.append(uuid)

    fake.iterator = _iterator  # type: ignore[attr-defined]
    fake.data.delete_by_id = _delete_by_id  # type: ignore[method-assign]

    pruned = analyzer._prune_collection(
        fake,
        visited_uuids={"a-visited"},
        language_scope="",  # global prune
    )

    assert pruned == 3, f"Expected 3 prunes (global), got {pruned}"
    assert set(deleted_uuids) == {"b-stale-py", "c-stale-go", "d-stale-nolang"}


def test_prune_scoped_preserves_pre_migration_no_language_rows(analyzer_mod) -> None:
    """v0.2.17 rows have no `language` property. A language-scoped prune
    MUST preserve them (the next full re-analyze will repopulate the
    field). This is the safer of the two policy choices — better to leak
    a few orphan rows than to delete data we can't classify."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    fake = _FakeCollection("Test_CodeFunction")
    deleted_uuids: List[str] = []

    class _Obj:
        def __init__(self, uid: str, props: dict) -> None:
            self.uuid = uid
            self.properties = props

    pre_existing = [
        _Obj("py-stale", {"project": "TestProject", "language": "python"}),
        # Pre-migration: no language property at all.
        _Obj("legacy-1", {"project": "TestProject"}),
        # Pre-migration: language is None (Weaviate returns this for
        # missing-on-row props).
        _Obj("legacy-2", {"project": "TestProject", "language": None}),
    ]

    def _iterator(return_properties=None):
        return iter(pre_existing)

    def _delete_by_id(uuid: str) -> None:
        deleted_uuids.append(uuid)

    fake.iterator = _iterator  # type: ignore[attr-defined]
    fake.data.delete_by_id = _delete_by_id  # type: ignore[method-assign]

    pruned = analyzer._prune_collection(
        fake, visited_uuids=set(), language_scope="python",
    )

    assert pruned == 1, f"Only the python-tagged row should be pruned, got {pruned}"
    assert deleted_uuids == ["py-stale"], (
        "Pre-migration rows (no language property) must survive a "
        "language-scoped prune."
    )


# ---------------------------------------------------------------------------
# Test 5 — CodeInteraction source-side semantics.
# ---------------------------------------------------------------------------


def test_codeinteraction_language_is_source_side(analyzer_mod) -> None:
    """A Python file calling a Go gRPC endpoint creates a CodeInteraction
    row. The `language` property should be `"python"` (the caller's
    language), not the target language. This is the explicit contract:
    interactions are stored from the source's perspective."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._current_language = "python"  # dispatcher set it for the Python pass

    fake_coll = _FakeCollection("Test_CodeInteraction")
    insert_params = {
        "properties": {
            "source_project": "TestProject",
            "interaction_type": "grpc",
            "endpoint": "go-service:UserService.GetUser",
            "protocol": "grpc",
        },
    }
    analyzer._dedup_insert(
        fake_coll,
        insert_params,
        "ix::src/client.py::go-service:UserService.GetUser",
        file_path_rel="src/client.py",
    )

    written = fake_coll.data.replace_calls[0]
    assert written["properties"].get("language") == "python", (
        "CodeInteraction.language must be the SOURCE-SIDE language. A "
        "Python file calling a Go service produces language='python'."
    )


def test_codeinteraction_prune_scoped_by_source_language(analyzer_mod) -> None:
    """A `--language=python` prune deletes only the interactions
    extracted from Python source. Interactions from a previous Go run are
    preserved (they belong to a different `args.language` scope and would
    be cleaned up by their own `--language=go` run)."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    fake = _FakeCollection("Test_CodeInteraction")
    deleted: List[str] = []

    class _Obj:
        def __init__(self, uid: str, props: dict) -> None:
            self.uuid = uid
            self.properties = props

    pre_existing = [
        _Obj("ix-py-1", {"project": "TestProject", "language": "python"}),
        _Obj("ix-py-2-stale", {"project": "TestProject", "language": "python"}),
        _Obj("ix-go-1", {"project": "TestProject", "language": "go"}),
        _Obj("ix-go-2", {"project": "TestProject", "language": "go"}),
    ]

    def _iterator(return_properties=None):
        return iter(pre_existing)

    def _delete_by_id(uuid: str) -> None:
        deleted.append(uuid)

    fake.iterator = _iterator  # type: ignore[attr-defined]
    fake.data.delete_by_id = _delete_by_id  # type: ignore[method-assign]

    # Python pass visited ix-py-1 but not ix-py-2.
    pruned = analyzer._prune_collection(
        fake,
        visited_uuids={"ix-py-1"},
        language_scope="python",
    )

    assert pruned == 1, f"Expected 1 prune (the unvisited python interaction), got {pruned}"
    assert deleted == ["ix-py-2-stale"], (
        "Go interactions must survive a Python-scoped prune. Each language "
        "owns its own subset of CodeInteraction rows."
    )


# ---------------------------------------------------------------------------
# Test 6 — _ensure_language_property is idempotent + adds the property.
# ---------------------------------------------------------------------------


def test_ensure_language_property_adds_to_missing_collections(analyzer_mod) -> None:
    """The analyzer's in-process schema migration helper must add the
    `language` property to CodeClass / CodeFunction / CodeAPI /
    CodeInteraction when missing, and skip silently when present."""

    class _PropStub:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Config:
        def __init__(self, props: List[str]) -> None:
            self.properties = [_PropStub(n) for n in props]

    class _MutableConfig:
        def __init__(self, props: List[str]) -> None:
            self._props = list(props)
            self.added: List[str] = []

        def get(self) -> _Config:
            return _Config(self._props)

        def add_property(self, prop) -> None:
            # weaviate-client v4 Property has .name attribute.
            name = getattr(prop, "name", None) or prop.get("name")
            self.added.append(name)
            self._props.append(name)

    class _Coll:
        def __init__(self, name: str, props: List[str]) -> None:
            self.name = name
            self.config = _MutableConfig(props)

    analyzer = _make_analyzer(analyzer_mod)
    # CodeClass / Function / API missing the prop; Interaction already
    # has it (idempotency check). CodeModule isn't in the list — analyzer
    # owns it via _ensure_import_names_property's neighbour migration.
    analyzer.modules_collection = _Coll("Test_CodeModule", ["language", "path"])
    analyzer.classes_collection = _Coll("Test_CodeClass", ["name", "full_name"])
    analyzer.functions_collection = _Coll("Test_CodeFunction", ["name", "full_name"])
    analyzer.apis_collection = _Coll("Test_CodeAPI", ["endpoint"])
    analyzer.interactions_collection = _Coll("Test_CodeInteraction", ["language"])

    analyzer._ensure_language_property()

    assert analyzer.classes_collection.config.added == ["language"]
    assert analyzer.functions_collection.config.added == ["language"]
    assert analyzer.apis_collection.config.added == ["language"]
    # Already had it → not re-added.
    assert analyzer.interactions_collection.config.added == []


def test_ensure_language_property_idempotent_second_run(analyzer_mod) -> None:
    """Running `_ensure_language_property` twice must add nothing the
    second time. Mirrors the contract of _ensure_import_names_property."""

    class _PropStub:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Config:
        def __init__(self, props: List[str]) -> None:
            self.properties = [_PropStub(n) for n in props]

    class _MutableConfig:
        def __init__(self, props: List[str]) -> None:
            self._props = list(props)
            self.added: List[str] = []

        def get(self) -> _Config:
            return _Config(self._props)

        def add_property(self, prop) -> None:
            name = getattr(prop, "name", None) or prop.get("name")
            self.added.append(name)
            self._props.append(name)

    class _Coll:
        def __init__(self, name: str, props: List[str]) -> None:
            self.name = name
            self.config = _MutableConfig(props)

    analyzer = _make_analyzer(analyzer_mod)
    analyzer.modules_collection = _Coll("Test_CodeModule", ["language"])
    analyzer.classes_collection = _Coll("Test_CodeClass", [])
    analyzer.functions_collection = _Coll("Test_CodeFunction", [])
    analyzer.apis_collection = _Coll("Test_CodeAPI", [])
    analyzer.interactions_collection = _Coll("Test_CodeInteraction", [])

    analyzer._ensure_language_property()
    first_pass_added = (
        analyzer.classes_collection.config.added
        + analyzer.functions_collection.config.added
        + analyzer.apis_collection.config.added
        + analyzer.interactions_collection.config.added
    )
    assert first_pass_added == ["language", "language", "language", "language"]

    analyzer._ensure_language_property()
    second_pass_added = (
        analyzer.classes_collection.config.added[1:]
        + analyzer.functions_collection.config.added[1:]
        + analyzer.apis_collection.config.added[1:]
        + analyzer.interactions_collection.config.added[1:]
    )
    assert second_pass_added == [], (
        "Second pass must be a no-op — the property is already present."
    )


# ---------------------------------------------------------------------------
# Test 7 — migrate-collections schema delta picks up `language` prop.
# ---------------------------------------------------------------------------


def test_existing_v0217_collections_get_language_prop_via_migrate_collections() -> None:
    """The smart-dispatch's `_schema_delta` MUST flag `language` as a
    missing prop on a v0.2.17-shaped code collection. This is the
    defense-in-depth path (the primary path is the analyzer's in-process
    migration); we test it here to lock the contract."""
    sys.path.insert(0, str(_REPO_ROOT))
    from vco_lib.project_init import _schema_delta, code_class_definitions

    # Simulated actual schema (v0.2.17 shape — no `language` prop).
    actual = {
        "class": "TestProj_CodeFunction",
        "vectorConfig": {
            # Minimal: same slot set as target so delta only picks up props.
            "qwen3_embed": {},
            "codesage_embed": {},
            "ollama_code_embed": {},
            "openai_code_embed": {},
            "arctic2_embed": {},
            "openai_text_embed": {},
        },
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "name", "dataType": ["text"]},
            {"name": "full_name", "dataType": ["text"]},
            # No `language` prop here — represents v0.2.17 state.
        ],
    }

    # Target schema from the canonical helper.
    target = code_class_definitions(project_prefix="TestProj_")["CodeFunction"]

    # Plan C: the target's vectorConfig has more slots than `actual`; we
    # only care about the property delta here. Align the slot set to
    # exercise the patch_props branch specifically.
    actual_vc = dict(actual["vectorConfig"])
    target_vc = dict(target["vectorConfig"])
    # Add any missing slots to actual so the delta isn't driven by slots.
    for slot in target_vc:
        actual_vc.setdefault(slot, {})
    actual["vectorConfig"] = actual_vc

    delta = _schema_delta(actual, target)

    missing_prop_names = {p.get("name") for p in delta.missing_props}
    assert "language" in missing_prop_names, (
        f"Plan C: migrate-collections must detect `language` as missing "
        f"on v0.2.17 code collections. Got missing_props={missing_prop_names}. "
        f"This test guards the patch_props defense-in-depth path."
    )


# ---------------------------------------------------------------------------
# Test 8 — extension-to-dispatch-name mapping covers all known languages.
#
# v0.2.66 (Bug 3): the hook no longer carries a LANG `case` block. Language
# detection for the per-edit (single-file) path now lives in the analyzer's
# `_dispatch_name_for_file`, which the hook reaches via `--only-file`. These
# tests assert the analyzer's mapping directly (the real home), and verify
# the hook passes the new single-file invocation rather than `--incremental`.
# ---------------------------------------------------------------------------


# (filename, expected lang_dispatch name) pairs `_dispatch_name_for_file`
# must recognise. The dispatch NAME is the first element of each
# `lang_dispatch` tuple in `analyze_repository`. Note `.c` maps to "cpp"
# (the dispatch name), because `_find_cpp_files` globs `*.c` into the cpp
# dispatch — single-file mode must match the directory walk's routing.
_DISPATCH_NAME_CASES = [
    ("foo.py", "python"),
    ("bar.js", "javascript"),
    ("bar.mjs", "javascript"),
    ("bar.jsx", "javascript"),
    ("bar.ts", "typescript"),
    ("bar.tsx", "typescript"),
    ("svc.go", "go"),
    ("lib.rs", "rust"),
    ("plugin.lua", "lua"),
    ("hot.cpp", "cpp"),
    ("hot.cc", "cpp"),
    ("hot.cxx", "cpp"),
    ("api.h", "cpp"),
    ("api.hpp", "cpp"),
    ("driver.c", "cpp"),   # *.c is globbed into the cpp dispatch
    ("Controller.cs", "csharp"),
    ("Main.java", "java"),
    ("app.rb", "ruby"),
    ("api.proto", "proto"),
    ("install.sh", "shell"),
    ("install.bash", "shell"),
    ("widget.svelte", "svelte"),
    ("hook.ps1", "powershell"),
    ("mod.psm1", "powershell"),
    # Deliberately-skipped extensions mirror the `_find_*_files` skips so
    # single-file mode agrees with the directory walk (no-op = empty).
    ("types.d.ts", ""),
    ("bundle.min.js", ""),
    # Unknown extensions get empty (single-file mode is a no-op).
    ("README.md", ""),
    ("data.json", ""),
]


@pytest.mark.parametrize("filename,expected_name", _DISPATCH_NAME_CASES)
def test_dispatch_name_for_file(
    analyzer_mod: types.ModuleType, filename: str, expected_name: str
) -> None:
    """`_dispatch_name_for_file` maps an edited file to its `lang_dispatch`
    name (or "" for an unknown / deliberately-skipped extension). This is
    the single source of truth the per-edit hook relies on via
    `--only-file`; the parity contract is that the returned value equals a
    `lang_dispatch` first-element in `analyze_repository`.
    """
    assert (
        analyzer_mod._dispatch_name_for_file(Path(filename)) == expected_name
    )


def test_hook_sh_uses_single_file_invocation() -> None:
    """v0.2.66 (Bug 3): the hook must pass `--only-file "$EDITED_FILE"` and
    must NOT use `--incremental` (which re-churned every HEAD~1..HEAD file
    per edit AND missed the actual uncommitted edit). This catches an
    accidental revert to the whole-repo incremental invocation."""
    sh = _HOOK_PATH.read_text(encoding="utf-8")
    import re

    inv = re.search(
        r'\(\s*\n\s*cd "\$REPO_PATH"\s*\n(.*?)\) &',
        sh,
        re.DOTALL,
    )
    assert inv, "could not locate analyzer-invocation subshell in the hook"
    invocation = inv.group(1)
    assert "--only-file" in invocation and '"$EDITED_FILE"' in invocation, (
        "Hook must pass `--only-file \"$EDITED_FILE\"` (single-file scope). "
        "See v0.2.66 Bug 3."
    )
    assert "--incremental" not in invocation, (
        "v0.2.66 regression: `--incremental` must NOT be in the analyzer "
        "invocation — it re-analyzed every HEAD~1..HEAD file per edit and "
        "never indexed the actual (uncommitted) edit. Use `--only-file`."
    )


def test_hook_ps1_uses_single_file_invocation() -> None:
    """Mirror of `test_hook_sh_uses_single_file_invocation` for the .ps1
    sibling — cross-language logic must not drift (the .ps1 path is what
    fires for native-Windows users without WSL)."""
    ps1 = (
        _REPO_ROOT / "templates" / "hooks" / "code-graph-incremental.ps1"
    ).read_text(encoding="utf-8")
    assert "'--only-file'" in ps1 and "$EditedFile" in ps1, (
        "code-graph-incremental.ps1 must pass `--only-file $EditedFile`. "
        "See v0.2.66 Bug 3."
    )
    assert "'--incremental'" not in ps1, (
        "v0.2.66 regression: the .ps1 must NOT pass `--incremental`."
    )


def test_hook_sh_does_not_pass_prune_stale_v52_o7() -> None:
    """V52-O.7 (v0.2.52, 2026-06-09) dropped `--prune-stale --language`
    from the incremental analyzer invocation. Audit a97f0d9 found that
    the language-scoped prune iterated the WHOLE collection and deleted
    every row of that language not visited THIS run — so every Python
    edit destroyed all OTHER Python rows, driving the collection's
    Python coverage to zero.

    The LANG mapping stays in place (load-bearing for v0.2.53's proper
    fix — scope prune to the EDITED FILE only) but the analyzer
    invocation must NOT pass either flag. This regression test prevents
    accidental re-addition before the v0.2.53 follow-up lands.
    """
    sh = _HOOK_PATH.read_text(encoding="utf-8")
    # Extract the analyzer invocation block (between the V52-O.7 comment
    # banner and the closing `) &`). Find any line that's a continuation
    # of the analyzer command and assert --prune-stale / --language
    # aren't present.
    import re
    # Match the actual invocation, not the comment block above it.
    inv = re.search(
        r'\(\s*\n\s*cd "\$REPO_PATH"\s*\n(.*?)\) &',
        sh,
        re.DOTALL,
    )
    assert inv, "could not locate analyzer-invocation subshell in the hook"
    invocation = inv.group(1)
    assert "--prune-stale" not in invocation, (
        "V52-O.7 regression: --prune-stale must NOT be in the analyzer "
        "invocation (it destroys collection-wide rows on every edit). "
        "See v0.2.52 backlog § V52-O.7."
    )
    assert "--language" not in invocation, (
        "V52-O.7 regression: --language must NOT be in the analyzer "
        "invocation (it scopes the destructive --prune-stale to one "
        "language). See v0.2.52 backlog § V52-O.7."
    )


# ---------------------------------------------------------------------------
# Test 9 — argparse no longer warns on --prune-stale + --language combo.
# ---------------------------------------------------------------------------


def test_warning_text_for_prune_plus_language_combo_removed(analyzer_mod) -> None:
    """The pre-Plan-C warning text claimed `--prune-stale + --language=X`
    would delete entries from OTHER languages. With Plan C's scoped
    prune the combo is now CORRECT, not a footgun. The warning string
    must be gone from the source so it doesn't confuse users."""
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    # The exact phrasing of the pre-Plan-C warning.
    forbidden = "would delete code-graph entries from OTHER languages"
    assert forbidden not in src, (
        "Pre-Plan-C warning still in source — remove it. The combo is "
        "now the correct mode."
    )
