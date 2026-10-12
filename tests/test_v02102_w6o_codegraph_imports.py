# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-O: language-aware import resolution + CodeAPI ``is_test``.

Findings closed (architecture/KG feasibility review, 2026-10-12, §0):

* F-5 — 17 % of stored Python ``imports`` edges pointed at ``.rs``/``.sh``
  files: the stem lookup ignored language (``logging.py`` → ``logging.rs``).
* F-6 — Rust and TypeScript names (``crate::a::b``, ``./x``, ``$lib/x``) almost
  never resolved; shell ``import_names`` were comment words.
* F-7 — CodeAPI had no test flag; legacy CodeAPI rows kept ``file_path = null``.

Hand-written fixtures per language; every assertion here fails on the
pre-W6-O resolver / extractors (see the lane report for the red run).
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
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from vco_lib import codegraph_guards as guards  # noqa: E402
from vco_lib.codegraph_imports import (  # noqa: E402
    build_import_index,
    expand_rust_use_tree,
    filter_compatible_import_rows,
    import_edge_compatible,
    resolve_import_targets,
)


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────────────── F-5: cross-language stems ──────────────────────

_MIXED = [
    "launcher/src-tauri/src/logging.rs",      # first in index order on purpose
    "scripts/lib/logging.sh",
    "claude_mcp_servers/model_router/logging.py",
    "claude_mcp_servers/model_router/usage.py",
]


def test_python_import_never_resolves_to_a_rust_or_shell_stem() -> None:
    index = build_import_index(_MIXED)
    got = resolve_import_targets("claude_mcp_servers/model_router/usage.py", "logging", index)
    assert got == ["claude_mcp_servers/model_router/logging.py"]


def test_python_import_with_only_foreign_stems_is_unresolved() -> None:
    index = build_import_index(["src/logging.rs", "lib/logging.sh", "app.py"])
    assert resolve_import_targets("app.py", "logging", index) == []


def test_python_rule_otherwise_unchanged() -> None:
    index = build_import_index(["pkg/b.py", "other/c.py"])
    assert resolve_import_targets("pkg/a.py", "pkg.b", index) == ["pkg/b.py"]
    assert resolve_import_targets("pkg/a.py", "deep.ns.c", index) == ["other/c.py"]


def test_compatibility_families() -> None:
    assert import_edge_compatible("python", "python")
    assert not import_edge_compatible("python", "rust")
    assert not import_edge_compatible("python", "shell")
    assert import_edge_compatible("typescript", "svelte")
    assert import_edge_compatible("svelte", "javascript")
    assert not import_edge_compatible("rust", "typescript")
    # unknown side → kept (a reader that cannot prove an edge wrong keeps it)
    assert import_edge_compatible("", "rust")


def test_read_filter_hides_stored_cross_language_edges() -> None:
    rows = [types.SimpleNamespace(properties={"path": "src/logging.rs", "language": "rust"}),
            types.SimpleNamespace(properties={"path": "pkg/log.py", "language": "python"}),
            types.SimpleNamespace(properties={"path": "lib/x.sh"})]  # language from path
    kept = filter_compatible_import_rows({"path": "pkg/usage.py", "language": "python"}, rows)
    assert [r.properties["path"] for r in kept] == ["pkg/log.py"]


# ───────────────────────────────── Rust ─────────────────────────────────────

_RUST = [
    "launcher/src-tauri/src/main.rs",
    "launcher/src-tauri/src/commands/mod.rs",
    "launcher/src-tauri/src/commands/single_flight.rs",
    "launcher/src-tauri/src/commands/gateway.rs",
    "launcher/src-tauri/src/db.rs",
    "launcher/src-tauri/src/db/models.rs",
    "launcher/src-tauri/vct-launcher-core/src/lib.rs",
    "launcher/src-tauri/vct-launcher-core/src/paths.rs",
    "launcher/src-tauri/vct-launcher-core/src/db/mod.rs",
    "launcher/src-tauri/vct-launcher-core/src/db/projects.rs",
]
_GW = "launcher/src-tauri/src/commands/gateway.rs"


@pytest.mark.parametrize("name,expected", [
    ("crate::db::models::ProjectRow", ["launcher/src-tauri/src/db/models.rs"]),
    ("crate::db::Db", ["launcher/src-tauri/src/db.rs"]),
    ("crate::commands::single_flight::{self, SingleFlightGuard, OP_X}",
     ["launcher/src-tauri/src/commands/single_flight.rs"]),
    ("super::single_flight::Guard", ["launcher/src-tauri/src/commands/single_flight.rs"]),
    ("super::super::db::models", ["launcher/src-tauri/src/db/models.rs"]),
    ("self::inner::X", []),  # gateway.rs declares no child module `inner`
    ("vct_launcher_core::db::projects::{list, Row as R}",
     ["launcher/src-tauri/vct-launcher-core/src/db/projects.rs"]),
    ("vct_launcher_core::{paths, db::projects}",
     ["launcher/src-tauri/vct-launcher-core/src/paths.rs",
      "launcher/src-tauri/vct-launcher-core/src/db/projects.rs"]),
    ("std::collections::HashMap", []),
    ("serde::{Deserialize, Serialize}", []),
])
def test_rust_use_trees(name, expected) -> None:
    assert resolve_import_targets(_GW, name, build_import_index(_RUST)) == expected


def test_rust_mod_declarations() -> None:
    index = build_import_index(_RUST)
    assert resolve_import_targets("launcher/src-tauri/src/main.rs", "mod commands", index) == [
        "launcher/src-tauri/src/commands/mod.rs"]
    assert resolve_import_targets("launcher/src-tauri/src/db.rs", "mod models", index) == [
        "launcher/src-tauri/src/db/models.rs"]
    assert resolve_import_targets("launcher/src-tauri/src/commands/mod.rs", "mod gateway", index) == [
        _GW]


def test_rust_use_tree_expansion() -> None:
    assert expand_rust_use_tree("a::{b, c::{d, self}, e as f, g::*}") == [
        ["a", "b"], ["a", "c", "d"], ["a", "c"], ["a", "e"], ["a", "g"]]
    assert expand_rust_use_tree("::std::io") == [["std", "io"]]


def test_rust_never_resolves_to_a_python_module_of_the_same_stem() -> None:
    index = build_import_index(["src/main.rs", "src/logging.py"])
    assert resolve_import_targets("src/main.rs", "mod logging", index) == []
    assert resolve_import_targets("src/main.rs", "crate::logging", index) == ["src/main.rs"][:0]


# ─────────────────────────── TypeScript / JS / Svelte ───────────────────────

_TS = [
    "launcher/src/lib/tauri.ts",
    "launcher/src/lib/stores/toast.ts",
    "launcher/src/lib/stores/index.ts",
    "launcher/src/lib/components/Banner.svelte",
    "launcher/src/lib/state.svelte.ts",
    "launcher/src/routes/+page.svelte",
    "launcher/src/lib/util.js",
    "web/src/lib/db.ts",
    "web/app/api/route.ts",
    "launcher/src/lib/tauri.py",  # a Python twin must never win
]


@pytest.mark.parametrize("importer,spec,expected", [
    ("launcher/src/routes/+page.svelte", "$lib/tauri", "launcher/src/lib/tauri.ts"),
    ("launcher/src/routes/+page.svelte", "$lib/stores", "launcher/src/lib/stores/index.ts"),
    ("launcher/src/routes/+page.svelte", "$lib/components/Banner.svelte",
     "launcher/src/lib/components/Banner.svelte"),
    ("launcher/src/lib/stores/toast.ts", "../tauri", "launcher/src/lib/tauri.ts"),
    ("launcher/src/lib/stores/toast.ts", "../tauri.js", "launcher/src/lib/tauri.ts"),
    ("launcher/src/lib/stores/toast.ts", "./index", "launcher/src/lib/stores/index.ts"),
    ("launcher/src/lib/stores/toast.ts", "../util.js", "launcher/src/lib/util.js"),
    ("launcher/src/lib/stores/toast.ts", "../state.svelte", "launcher/src/lib/state.svelte.ts"),
    ("web/app/api/route.ts", "@/lib/db", "web/src/lib/db.ts"),
])
def test_ts_specifiers(importer, spec, expected) -> None:
    assert resolve_import_targets(importer, spec, build_import_index(_TS)) == [expected]


@pytest.mark.parametrize("spec", ["svelte", "@tauri-apps/api/core", "$app/navigation", "../../../../x"])
def test_ts_bare_or_escaping_specifiers_do_not_resolve(spec) -> None:
    assert resolve_import_targets("launcher/src/lib/stores/toast.ts", spec, build_import_index(_TS)) == []


# ───────────────────────────────── shell ────────────────────────────────────

_SH = [
    "templates/hooks/_lib/common.sh",
    "templates/hooks/stop-drain.sh",
    "scripts/lib/common.sh",
    "scripts/release.sh",
    "templates/hooks/common.py",
]


@pytest.mark.parametrize("importer,arg,expected", [
    ("templates/hooks/stop-drain.sh", '"$HOOK_DIR/_lib/common.sh"', "templates/hooks/_lib/common.sh"),
    ("templates/hooks/stop-drain.sh", '"$(dirname "$0")/_lib/common.sh"', "templates/hooks/_lib/common.sh"),
    ("templates/hooks/stop-drain.sh", "./_lib/common.sh", "templates/hooks/_lib/common.sh"),
    ("scripts/release.sh", '"${BASH_SOURCE%/*}/lib/common.sh"', "scripts/lib/common.sh"),
    ("scripts/release.sh", "scripts/lib/common.sh", "scripts/lib/common.sh"),
])
def test_shell_source_paths(importer, arg, expected) -> None:
    assert resolve_import_targets(importer, arg, build_import_index(_SH)) == [expected]


@pytest.mark.parametrize("arg", ["Replaces", "If", "$f", "~/.bashrc", "/etc/profile"])
def test_shell_words_and_dynamic_only_args_do_not_resolve(arg) -> None:
    assert resolve_import_targets("templates/hooks/stop-drain.sh", arg, build_import_index(_SH)) == []


def test_powershell_dot_source_and_module() -> None:
    index = build_import_index(["tools/lib/Common.ps1", "tools/Mod.psm1", "tools/run.ps1"])
    assert resolve_import_targets("tools/run.ps1", r"$PSScriptRoot\lib\Common.ps1", index) == [
        "tools/lib/Common.ps1"]
    assert resolve_import_targets("tools/run.ps1", "Mod", index) == ["tools/Mod.psm1"]


# ─────────────────────────── extraction (F-6 shell/Rust) ────────────────────

_SHELL_SRC = '''#!/usr/bin/env bash
# Replaces the old drain. That is why. Instead of polling, If set:
# . not_a_command.sh
source "$DIR/_lib/common.sh"
. ./helpers.sh
echo "source not_me.sh"
cat <<EOF
. Instead
source heredoc.sh
EOF
if true; then . "${BASH_SOURCE%/*}/x.sh"; fi
foo() { source inner.sh; }
'''
_SHELL_WANT = ['"$DIR/_lib/common.sh"', "./helpers.sh", '"${BASH_SOURCE%/*}/x.sh"', "inner.sh"]


def test_shell_tree_sitter_imports_are_only_real_source_commands() -> None:
    from vco_lib.codegraph_defs import extract_definitions

    defs = extract_definitions("shell", _SHELL_SRC)
    if defs is None:
        pytest.skip("bash grammar unavailable")
    assert defs.imports == _SHELL_WANT


def test_shell_regex_fallback_skips_comments_and_heredocs() -> None:
    from vco_lib.codegraph_lang.shell import regex_shell_sources

    assert regex_shell_sources(_SHELL_SRC) == _SHELL_WANT


_RUST_SRC = '''//! use crate::not_this;
use crate::a::{b,
    c::D};
mod x;
pub(crate) mod y;
mod inline { fn f() {} }
#[cfg(test)]
mod tests { use super::*; }
#[cfg(test)]
mod t2;
fn f() { use std::io; }
'''


def test_rust_tree_sitter_imports_include_mod_decls_and_skip_test_code() -> None:
    from vco_lib.codegraph_defs import extract_definitions

    defs = extract_definitions("rust", _RUST_SRC)
    if defs is None:
        pytest.skip("rust grammar unavailable")
    assert defs.imports == ["crate::a::{b, c::D}", "mod x", "mod y", "std::io"]


def test_rust_regex_fallback_reads_multiline_use_and_mod_decls() -> None:
    from vco_lib.codegraph_lang._shared import blank_block_comments_preserving_lines
    from vco_lib.codegraph_lang.rust import regex_rust_imports
    import re

    clean = blank_block_comments_preserving_lines(re.sub(r"//.*$", "", _RUST_SRC, flags=re.MULTILINE))
    got = regex_rust_imports(clean)
    assert "crate::a::{b, c::D}" in got and "mod x" in got and "mod y" in got
    assert not any("not_this" in g for g in got)


# ─────────────────────────── analyzer integration ───────────────────────────

_IDEMPOTENCE = REPO_ROOT / "tests" / "test_v0292_analyzer_reference_idempotence.py"


@pytest.fixture(scope="module")
def harness():
    pytest.importorskip("weaviate.collections.classes.internal")
    return _load("_w6o_idem_harness", _IDEMPOTENCE)


@pytest.fixture(scope="module")
def analyzer_mod():
    return _load("_w6o_acg", REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py")


def test_cross_reference_pass_links_same_language_only(harness, analyzer_mod) -> None:
    rows = {
        "m-rs": harness._StoredRow("m-rs", {"path": "src/logging.rs", "project_source": ""}),
        "m-usage": harness._StoredRow("m-usage", {
            "path": "pkg/usage.py", "project_source": "", "import_names": ["logging", "pkg.log"]}),
        "m-log": harness._StoredRow("m-log", {"path": "pkg/log.py", "project_source": ""}),
        "m-main": harness._StoredRow("m-main", {
            "path": "src/main.rs", "project_source": "",
            "import_names": ["mod logging", "crate::logging::init"]}),
        "m-ts": harness._StoredRow("m-ts", {
            "path": "web/src/app.ts", "project_source": "", "import_names": ["./util", "logging"]}),
        "m-util": harness._StoredRow("m-util", {"path": "web/src/util.ts", "project_source": ""}),
    }
    mods = harness._FakeCollection("P_CodeModule", rows)
    classes = harness._FakeCollection("P_CodeClass", {})
    funcs = harness._FakeCollection("P_CodeFunction", {})
    harness._analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()
    assert harness._beacons(mods, "m-usage", "imports") == ["m-log"]  # NOT logging.rs
    assert harness._beacons(mods, "m-main", "imports") == ["m-rs"]
    assert harness._beacons(mods, "m-ts", "imports") == ["m-util"]


# ─────────────────────────────── F-7: CodeAPI ───────────────────────────────


def test_codeapi_spec_has_is_test_and_edge_8_to_9_owns_it() -> None:
    from vco_lib.codegraph_schema import CODEGRAPH_PROPERTY_SPECS
    from vco_lib import schema_versions as sv

    assert ("is_test", "BOOL") in {(p, t) for p, t, _ in CODEGRAPH_PROPERTY_SPECS["CodeAPI"]}
    edge = _load("_w6o_edge_8_to_9", REPO_ROOT / "migrations" / "codegraph_collection" / "8_to_9.py")
    assert edge._V9_PROPS == ("is_test",)
    assert sv.canonical_version("codegraph_collection") == 9


class _Coll:
    def __init__(self, name: str, declared=("file_path", "is_test")) -> None:
        self.name = name
        self.updates: list = []
        self.data = types.SimpleNamespace(
            update=lambda uuid, properties: self.updates.append((uuid, dict(properties))))
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in declared]))


@pytest.mark.parametrize("file_path,expected", [
    ("tests/fixtures/codegraph_truth/proto/catalog.proto", True),
    ("claude_mcp_servers/code_embedding_service/server.py", False),
])
def test_analyzer_stamps_is_test_on_codeapi_rows(analyzer_mod, file_path, expected) -> None:
    """The analyzer's write choke point classifies CodeAPI rows with the same
    path rule as functions (and leaves CodeInteraction alone)."""
    captured: dict = {}

    class _Probe(Exception):
        pass

    def _stop(self, *args, **kwargs):
        params = next(a for a in (*args, *kwargs.values())
                      if isinstance(a, dict) and "properties" in a)
        captured.update(params["properties"])
        raise _Probe

    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst._current_source = ""
    inst._current_language = "proto"
    inst._embed_skip_fingerprint = None
    props = {"endpoint": "/x", "method": "GET", "api_description": "d", "parameters": [],
             "returns": "", "project": "P", "language": "proto"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(analyzer_mod.CodeGraphAnalyzer, "_write_one_object", _stop, raising=True)
        with pytest.raises(_Probe):
            inst._dedup_insert(_Coll("P_CodeAPI"), {"properties": props},
                               file_path_rel=file_path, identity_key="GET /x")
    assert captured["is_test"] is expected
    assert captured["file_path"] == file_path


def test_anchor_heal_patch_fills_only_missing_values() -> None:
    assert guards.anchor_heal_props_for(_Coll("P_CodeAPI")) == ("file_path", "is_test")
    assert guards.anchor_heal_props_for(_Coll("P_CodeInteraction")) == ("file_path",)
    assert guards.anchor_heal_props_for(_Coll("P_CodeFunction")) == ()
    props = {"file_path": "tests/fixtures/x.proto", "is_test": True}
    assert guards.anchor_heal_patch({"file_path": None, "is_test": None}, props) == props
    assert guards.anchor_heal_patch({"file_path": "a", "is_test": False}, props) == {}
    assert guards.anchor_heal_patch(None, props) == {}


def test_heal_skipped_row_patches_a_legacy_null_anchor_row() -> None:
    coll = _Coll("P_CodeAPI")
    props = {"file_path": "tests/fixtures/routes.js", "is_test": True, "endpoint": "/x"}
    assert guards.heal_skipped_row(coll, "u1", props, {"file_path": None, "is_test": None})
    assert coll.updates == [("u1", {"file_path": "tests/fixtures/routes.js", "is_test": True})]
    # leave-alone: an anchored row is never rewritten
    coll2 = _Coll("P_CodeAPI")
    assert not guards.heal_skipped_row(coll2, "u2", props, {"file_path": "x.js", "is_test": False})
    assert coll2.updates == []


@pytest.mark.parametrize("stored_anchor,want_patch", [
    ({"file_path": None, "is_test": None},
     [("u1", {"file_path": "tests/fixtures/routes.js", "is_test": True})]),
    ({"file_path": "tests/fixtures/routes.js", "is_test": True}, []),  # leave alone
])
def test_analyzer_skip_path_heals_a_hash_matched_legacy_row(analyzer_mod, stored_anchor,
                                                             want_patch) -> None:
    """The WIRING: `_write_one_object` on a content-identical CodeAPI row skips
    the replace (no re-embed) yet PATCHes the hash-excluded anchor it lacks."""
    props = {"endpoint": "/items/list", "method": "GET", "api_description": "GET /items/list",
             "parameters": [], "returns": "", "file_path": "tests/fixtures/routes.js",
             "is_test": True}
    content_hash = analyzer_mod._content_hash_for_object("P_CodeAPI", props)
    stored = {"content_hash": content_hash, "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
              **stored_anchor}
    coll = _Coll("P_CodeAPI")
    coll.query = types.SimpleNamespace(
        fetch_object_by_id=lambda uuid, return_properties, **kw: types.SimpleNamespace(
            properties={k: stored.get(k) for k in return_properties}, vector=None))
    coll.data.replace = coll.data.insert = lambda **kw: pytest.fail("content-identical row rewritten")
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst._embed_skip_fingerprint = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(analyzer_mod, "DUAL_EMBEDDING_ENABLED", False)
        assert inst._write_one_object(coll, "u1", {"properties": dict(props)}, "GET /items/list") == "u1"
    assert coll.updates == want_patch


def test_heal_props_only_for_declared_properties() -> None:
    """Review LOW-5: a class without `is_test` (8_to_9 deferred, ensure
    soft-failed) must not get `is_test` in the fingerprint read — that read
    would fail and the row would re-embed. One schema read per class."""
    reads: list = []
    coll = _Coll("P_CodeAPI", declared=("file_path",))
    real_get = coll.config.get
    coll.config.get = lambda: reads.append(1) or real_get()
    cache: dict = {}
    assert guards.anchor_heal_props_for(coll, cache) == ("file_path",)
    assert guards.anchor_heal_props_for(coll, cache) == ("file_path",)
    assert reads == [1]
    full = _Coll("P_CodeAPI")
    assert guards.anchor_heal_props_for(full, {}) == ("file_path", "is_test")
    broken = _Coll("P_CodeAPI")
    broken.config.get = lambda: (_ for _ in ()).throw(RuntimeError("schema down"))
    assert guards.anchor_heal_props_for(broken, {}) == ()
    # a class with nothing to heal never reads its schema
    fn = _Coll("P_CodeFunction")
    fn.config.get = lambda: pytest.fail("schema read for a class with no heal props")
    assert guards.anchor_heal_props_for(fn, {}) == ()


def test_analyzer_skip_path_asks_only_for_declared_heal_props(analyzer_mod) -> None:
    """The WIRING of LOW-5: with `is_test` undeclared, the analyzer's
    fingerprint point-read does not request it (a request for an undeclared
    property fails the read and forces a re-embed)."""
    props = {"endpoint": "/items/list", "method": "GET", "api_description": "GET /items/list",
             "parameters": [], "returns": "", "file_path": "tests/fixtures/routes.js",
             "is_test": True}
    content_hash = analyzer_mod._content_hash_for_object("P_CodeAPI", props)
    stored = {"content_hash": content_hash, "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
              "file_path": None}
    asked: list = []
    coll = _Coll("P_CodeAPI", declared=("file_path",))

    def _fetch(uuid, return_properties, **kw):
        asked.append(list(return_properties))
        if "is_test" in return_properties:
            raise RuntimeError("no such prop: is_test")
        return types.SimpleNamespace(properties={k: stored.get(k) for k in return_properties},
                                     vector=None)

    coll.query = types.SimpleNamespace(fetch_object_by_id=_fetch)
    coll.data.replace = coll.data.insert = lambda **kw: pytest.fail("re-embedded")
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst._embed_skip_fingerprint = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(analyzer_mod, "DUAL_EMBEDDING_ENABLED", False)
        assert inst._write_one_object(coll, "u1", {"properties": dict(props)}, "GET /items/list") == "u1"
    assert all("is_test" not in a for a in asked), asked
    assert coll.updates == [("u1", {"file_path": "tests/fixtures/routes.js"})]


def test_revision_stamp_path_also_heals_through_the_stash(analyzer_mod) -> None:
    """A content-identical legacy row at a stale-but-compatible revision: the
    D1 path PATCHes embed_revision (no embed), and the skip path — which runs
    AFTER the choke point stamped file_path/is_test into props — PATCHes the
    missing anchor. Never a replace, never an embed."""
    props = {"endpoint": "/x", "method": "GET", "api_description": "d", "parameters": [],
             "returns": ""}
    content_hash = analyzer_mod._content_hash_for_object("P_CodeAPI", props)
    det = analyzer_mod._deterministic_uuid("P", "tests/fixtures/r.js", "GET /x", project_source="")
    stored = {"content_hash": content_hash, "embed_revision": 1, "file_path": None, "is_test": None}
    coll = _Coll("P_CodeAPI")
    coll.query = types.SimpleNamespace(
        fetch_object_by_id=lambda uuid, return_properties, **kw: types.SimpleNamespace(
            properties={k: stored.get(k) for k in return_properties}, vector=None))
    coll.data.replace = coll.data.insert = lambda **kw: pytest.fail("rewritten")
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name, inst._current_source, inst._current_language = "P", "", "javascript"
    inst._embed_skip_fingerprint = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(analyzer_mod, "DUAL_EMBEDDING_ENABLED", False)
        mp.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 2)
        mp.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 1)
        inst._dedup_insert(coll, {"properties": dict(props),
                                  "_deferred_embed": lambda: pytest.fail("embedded")},
                           "GET /x", file_path_rel="tests/fixtures/r.js")
    assert coll.updates == [(det, {"embed_revision": 2}),
                            (det, {"file_path": "tests/fixtures/r.js", "is_test": True})]


def test_fingerprint_read_carries_heal_props_only_when_asked() -> None:
    seen: list = []

    def fetch(uuid, return_properties, **_kw):
        seen.append(list(return_properties))
        return types.SimpleNamespace(properties={"content_hash": "h", "embed_revision": 1,
                                                 "file_path": None, "is_test": None})

    coll = types.SimpleNamespace(name="P_CodeAPI",
                                 query=types.SimpleNamespace(fetch_object_by_id=fetch))
    fp = guards.read_object_fingerprint(coll, "u", "embed_revision",
                                        heal_props=("file_path", "is_test"))
    assert fp is not None and fp["heal"] == {"file_path": None, "is_test": None}
    fp2 = guards.read_object_fingerprint(coll, "u", "embed_revision")
    assert fp2 is not None and "heal" not in fp2
    assert seen == [["content_hash", "embed_revision", "file_path", "is_test"],
                    ["content_hash", "embed_revision"]]


class _ApiColl:
    def __init__(self, declared):
        self.name = "P_CodeAPI"
        self.updates: list = []
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(objects=[object()]))
        self.data = types.SimpleNamespace(
            update=lambda uuid, properties: self.updates.append((uuid, dict(properties))))
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in declared]))

    def iterator(self, return_properties=None, return_references=None):
        ref = types.SimpleNamespace(objects=[types.SimpleNamespace(
            properties={"file_path": "tests/fixtures/repo/src/Inventory.cs"})])
        obj = types.SimpleNamespace(uuid="a0", properties={"file_path": None})
        obj.references = {"handler": ref}
        yield obj


@pytest.mark.parametrize("declared,want", [
    (("file_path", "is_test"),
     {"file_path": "tests/fixtures/repo/src/Inventory.cs", "is_test": True}),
    # is_test not declared yet (edge 8_to_9 not applied): anchor only — an
    # undeclared property must never cost the row its anchor.
    (("file_path",), {"file_path": "tests/fixtures/repo/src/Inventory.cs"}),
])
def test_anchor_backfill_derives_codeapi_is_test_in_the_same_patch(declared, want) -> None:
    from vco_lib import codegraph_resync
    from weaviate_mcp.code_ranking import is_test_path

    api = _ApiColl(declared)
    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda n: n == "P_CodeAPI", get=lambda n: api), close=lambda: None)
    done, left = codegraph_resync._backfill_anchor_file_paths(
        client, "P", counts={}, is_test_fn=is_test_path)
    assert (done, left) == (1, 0)
    assert api.updates == [("a0", want)]


def test_metadata_backfill_covers_codeapi() -> None:
    from vco_lib import codegraph_resync

    assert codegraph_resync._BACKFILL_BASES["CodeAPI"] == ("file_path", None)
