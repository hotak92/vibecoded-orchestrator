# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.66 (Bug 3): per-edit code-graph analysis is scoped to the EDITED file.

Background — the bug this fix targets
-------------------------------------
The PostToolUse code-graph hook (`code-graph-incremental.sh`) used to invoke
the analyzer as ``<repo> --incremental``. ``--incremental`` runs
``git diff --name-only HEAD~1 HEAD`` and re-analyzes EVERY file in the last
commit. In an active cycle that is dozens of files (measured: 36), so:

  1. WRITE AMPLIFICATION — every single keystroke-level edit re-parsed +
     re-hash-queried all HEAD~1..HEAD files across 5 collections, the source
     of the observed multi-hundred-MiB/s disk-write peaks.
  2. CORRECTNESS — the file the user just edited is UNCOMMITTED (working
     tree), so it is NOT in HEAD~1..HEAD at all. The per-edit sync re-churned
     the PREVIOUS commit's files and NEVER indexed the actual edit.

The fix
-------
``analyze_repository(only_file=<path>)`` analyzes exactly the one edited file,
using ``repo_path`` only as the relativization root. The hook now passes
``--only-file "$EDITED_FILE"``. The single file flows through the SAME
per-file (``_get_existing_module``) and per-object (``_dedup_insert``
content-hash) skip paths, so an unchanged or trivially-edited file writes
~0 objects.

What these tests pin
--------------------
  * ``_dispatch_name_for_file`` routes extensions to the right dispatch name
    (and skips ``.d.ts`` / ``.min.js`` / unknown extensions). [covered in
    test_codegraph_language_scoped_prune.py — not duplicated here]
  * ``_single_file_dispatch`` selects exactly the one file, and refuses
    (clean no-op) for out-of-repo / nonexistent / unknown-extension /
    language-mismatch inputs. (pure unit — no Weaviate)
  * Running ``analyze_repository(only_file=A)`` over a 2-file repo writes
    ONLY A's objects; B's module/class/function are never touched. (fake
    collections — no Weaviate)
  * An unchanged file (per-file hash hit) writes 0 objects through the
    single-file path. (fake collections — no Weaviate)
  * End-to-end against a LIVE Weaviate (skipped when absent): a re-run on an
    unchanged file writes 0 new objects.

The harness mirrors test_analyze_code_graph_v0_2_16.py / test_code_graph_
content_hash_skip.py: load the analyzer via importlib, construct the analyzer
with ``__new__`` (no Weaviate connect), wire fake collections + stub the
module-level embed_* functions. The live test is gated behind a reachability
probe so CI without Weaviate skips it rather than hard-failing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import types
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_bug3_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(
            f"Analyzer module file missing from repo — CI env regression: {_ANALYZER_PATH}"
        )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail(
            "weaviate-client package not installed — CI env regression (required dependency missing)"
        )
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# Fakes — record replace()/insert() so we can assert per-file blast radius.
# ---------------------------------------------------------------------------


class _FakeCollectionData:
    def __init__(self) -> None:
        self.replace_calls: List[Dict[str, Any]] = []
        self.insert_calls: List[Dict[str, Any]] = []

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self.replace_calls.append({"uuid": str(uuid), **kwargs})
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self.insert_calls.append({"uuid": str(uuid), **kwargs})
        return str(uuid)

    def delete_by_id(self, uuid: str) -> None:  # pragma: no cover - prune path
        pass


class _FakeCollection:
    """Weaviate v4 collection stand-in. ``.query`` deliberately absent so
    ``_dedup_insert``'s content-hash point-read falls through to write
    (the per-object skip's fail-safe path), keeping these tests focused on
    file-scope rather than the hash-skip already pinned elsewhere."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.data = _FakeCollectionData()


def _wire_full_flow_analyzer(
    analyzer_mod: types.ModuleType, project: str = "Bug3Project"
):
    """Construct an analyzer ready for a direct ``analyze_repository`` call
    against fake collections (no Weaviate, no embedding network calls)."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
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
    inst.modules_collection = _FakeCollection(f"{project}_CodeModule")
    inst.classes_collection = _FakeCollection(f"{project}_CodeClass")
    inst.functions_collection = _FakeCollection(f"{project}_CodeFunction")
    inst.apis_collection = _FakeCollection(f"{project}_CodeAPI")
    inst.interactions_collection = _FakeCollection(f"{project}_CodeInteraction")
    return inst


def _stub_embeddings(analyzer_mod: types.ModuleType, monkeypatch) -> None:
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: None)
    monkeypatch.setattr(analyzer_mod, "embed_module", lambda summary: None)
    monkeypatch.setattr(
        analyzer_mod, "embed_function",
        lambda sig, body, language="python": None,
    )
    monkeypatch.setattr(
        analyzer_mod, "embed_class",
        lambda sig, body, methods=None, language="python": None,
    )


def _all_replace_calls(analyzer) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for coll in (
        analyzer.modules_collection,
        analyzer.classes_collection,
        analyzer.functions_collection,
        analyzer.apis_collection,
        analyzer.interactions_collection,
    ):
        calls.extend(coll.data.replace_calls)
        calls.extend(coll.data.insert_calls)
    return calls


# ---------------------------------------------------------------------------
# _single_file_dispatch — pure-unit file-scope selection (no Weaviate).
# ---------------------------------------------------------------------------


def _lang_dispatch(analyzer):
    """The same (name, find_fn, analyze_fn) tuples analyze_repository builds.
    We only need the names + analyze_fns for the selector contract."""
    return [
        ('python',     analyzer._find_python_files, analyzer._analyze_python_file),
        ('lua',        analyzer._find_lua_files,    analyzer._analyze_lua_file),
        ('cpp',        analyzer._find_cpp_files,    analyzer._analyze_cpp_file),
        ('javascript', analyzer._find_js_files,     analyzer._analyze_js_file),
        ('typescript', analyzer._find_ts_files,     analyzer._analyze_js_file),
        ('go',         analyzer._find_go_files,     analyzer._analyze_go_file),
        ('rust',       analyzer._find_rust_files,   analyzer._analyze_rust_file),
        ('java',       analyzer._find_java_files,   analyzer._analyze_java_file),
        ('ruby',       analyzer._find_ruby_files,   analyzer._analyze_ruby_file),
        ('shell',      analyzer._find_shell_files,  analyzer._analyze_shell_file),
        ('csharp',     analyzer._find_csharp_files, analyzer._analyze_csharp_file),
        ('proto',      analyzer._find_proto_files,  analyzer._analyze_proto_file),
        ('svelte',     analyzer._find_svelte_files,     analyzer._analyze_svelte_file),
        ('powershell', analyzer._find_powershell_files, analyzer._analyze_powershell_file),
    ]


def test_single_file_dispatch_selects_only_the_one_file(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """The selector returns exactly one dispatch entry containing only the
    edited file, routed to the python analyze_fn, relativized to repo_path."""
    repo = tmp_path
    edited = repo / "src" / "a.py"
    edited.parent.mkdir(parents=True)
    edited.write_text("def a():\n    return 1\n")
    # A SECOND file exists in the repo — it must NOT appear in the dispatch.
    (repo / "src" / "b.py").write_text("def b():\n    return 2\n")

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    out = analyzer._single_file_dispatch(
        edited, repo, _lang_dispatch(analyzer), None
    )

    assert len(out) == 1, "Single-file mode must yield exactly one dispatch entry"
    lang_name, analyze_fn, files, source_root = out[0]
    assert lang_name == "python"
    assert files == [edited.resolve()], "Only the edited file may be selected"
    assert source_root == repo, "repo_path stays the relativization root"
    assert analyze_fn == analyzer._analyze_python_file


def test_single_file_dispatch_rejects_file_outside_repo(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """A file not under repo_path is a clean no-op — a wrong relativization
    base would mis-key the row and create a duplicate/zombie object."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere" / "x.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("def x():\n    return 0\n")

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    out = analyzer._single_file_dispatch(
        outside, repo, _lang_dispatch(analyzer), None
    )
    assert out == [], "Out-of-repo file must be a no-op (mis-keyed row guard)"


def test_single_file_dispatch_nonexistent_file_is_noop(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """A file deleted between the edit and the debounced run is a no-op."""
    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    missing = tmp_path / "gone.py"
    out = analyzer._single_file_dispatch(
        missing, tmp_path, _lang_dispatch(analyzer), None
    )
    assert out == []


def test_single_file_dispatch_unknown_extension_is_noop(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """A non-code extension (the directory walk would never index it) is a
    no-op rather than an error."""
    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    doc = tmp_path / "README.md"
    doc.write_text("# hi\n")
    out = analyzer._single_file_dispatch(
        doc, tmp_path, _lang_dispatch(analyzer), None
    )
    assert out == []


def test_single_file_dispatch_skips_dts_and_minjs(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """``.d.ts`` and ``.min.js`` are excluded by the directory walk's
    name-based skips; single-file mode must agree (no-op)."""
    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    dts = tmp_path / "types.d.ts"
    dts.write_text("export {};\n")
    minjs = tmp_path / "bundle.min.js"
    minjs.write_text("var a=1;\n")
    assert analyzer._single_file_dispatch(
        dts, tmp_path, _lang_dispatch(analyzer), None
    ) == []
    assert analyzer._single_file_dispatch(
        minjs, tmp_path, _lang_dispatch(analyzer), None
    ) == []


def test_single_file_dispatch_respects_language_filter(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """When an explicit --language is supplied and the file's language does
    not match, the selector is a no-op."""
    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    py = tmp_path / "a.py"
    py.write_text("def a():\n    return 1\n")
    # File is python; filter asks for rust → no-op.
    assert analyzer._single_file_dispatch(
        py, tmp_path, _lang_dispatch(analyzer), "rust"
    ) == []
    # Matching filter → selected.
    out = analyzer._single_file_dispatch(
        py, tmp_path, _lang_dispatch(analyzer), "python"
    )
    assert len(out) == 1 and out[0][0] == "python"


# ---------------------------------------------------------------------------
# Full-flow file scope: analyze_repository(only_file=A) writes ONLY A.
# ---------------------------------------------------------------------------


def test_analyze_only_file_indexes_only_that_file(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """Running ``analyze_repository(only_file=A)`` over a repo containing A
    and B must write A's objects and NEVER touch B's. This is the per-edit
    blast-radius contract: 1 file, not all HEAD~1..HEAD files."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    repo = tmp_path
    (repo / "alpha.py").write_text(
        textwrap.dedent(
            """\
            def alpha_func():
                return "alpha"


            class AlphaClass:
                def method(self):
                    return 1
            """
        )
    )
    (repo / "beta.py").write_text(
        textwrap.dedent(
            """\
            def beta_func():
                return "beta"


            class BetaClass:
                def method(self):
                    return 2
            """
        )
    )

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    stats = analyzer.analyze_repository(
        repo, only_file=(repo / "alpha.py"), extract_cfg=False, extract_pdg=False
    )

    # alpha.py produced objects; beta.py is untouched.
    assert stats["files_analyzed"] == 1, (
        f"Exactly one file should be analyzed, got {stats['files_analyzed']}"
    )
    assert stats["modules"] >= 1
    assert stats["functions"] >= 1

    # No written object may reference beta.py — scan every collection's
    # writes for the beta module path / symbol names.
    written = _all_replace_calls(analyzer)
    blob = repr(written)
    assert "beta.py" not in blob, (
        "beta.py must NOT be indexed in a single-file run on alpha.py "
        "(blast radius must be 1 file, not the whole repo)"
    )
    assert "beta_func" not in blob and "BetaClass" not in blob, (
        "beta.py's symbols must NOT be written in a single-file run on alpha.py"
    )
    # Positive control: alpha's content IS written.
    assert "alpha.py" in blob, "alpha.py (the edited file) must be indexed"


# ---------------------------------------------------------------------------
# v0.2.73 (FIX-B): batched multi-file mode (--only-files-from).
# ---------------------------------------------------------------------------


def test_analyze_only_files_from_batches_all_listed_files(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """``analyze_repository(only_files_from=<list>)`` analyzes EVERY listed
    file in ONE pass (the end-of-turn batch) — not one process per file — and
    leaves unlisted files untouched."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    repo = tmp_path
    (repo / "alpha.py").write_text("def alpha_func():\n    return 1\n")
    (repo / "beta.py").write_text("def beta_func():\n    return 2\n")
    # gamma.py exists but is NOT in the list → must be untouched.
    (repo / "gamma.py").write_text("def gamma_func():\n    return 3\n")

    list_file = repo / "batch.txt"
    list_file.write_text(f"{repo / 'alpha.py'}\n{repo / 'beta.py'}\n")

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    stats = analyzer.analyze_repository(
        repo, only_files_from=list_file, extract_cfg=False, extract_pdg=False
    )

    assert stats["files_analyzed"] == 2, (
        f"Both listed files analyzed in one pass, got {stats['files_analyzed']}"
    )
    blob = repr(_all_replace_calls(analyzer))
    assert "alpha.py" in blob and "beta.py" in blob, "both listed files indexed"
    assert "gamma.py" not in blob and "gamma_func" not in blob, (
        "an unlisted file must NOT be indexed by the batch"
    )


def test_analyze_only_files_from_prunes_deleted_path(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """A path in the batch that vanished from disk (edited-then-deleted in the
    turn) is PRUNED (its objects deleted), not skipped/errored — no
    self-inflicted orphan."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    class _FakeDataWithDelete(_FakeCollectionData):
        def __init__(self) -> None:
            super().__init__()
            self.delete_many_calls: List[Any] = []

        def delete_many(self, where: Any = None) -> None:
            self.delete_many_calls.append(where)

    class _FakeCollWithDelete(_FakeCollection):
        def __init__(self, name: str) -> None:
            self.name = name
            self.data = _FakeDataWithDelete()

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    # Swap in delete-capable fakes for the file-anchored collections.
    analyzer.modules_collection = _FakeCollWithDelete(f"{analyzer.project_name}_CodeModule")
    analyzer.functions_collection = _FakeCollWithDelete(f"{analyzer.project_name}_CodeFunction")
    analyzer.classes_collection = _FakeCollWithDelete(f"{analyzer.project_name}_CodeClass")

    repo = tmp_path
    (repo / "present.py").write_text("def present():\n    return 1\n")
    missing = repo / "gone.py"  # never created

    list_file = repo / "batch.txt"
    list_file.write_text(f"{repo / 'present.py'}\n{missing}\n")

    stats = analyzer.analyze_repository(
        repo, only_files_from=list_file, extract_cfg=False, extract_pdg=False
    )

    # The present file analyzed; the missing one triggered a prune (delete_many)
    # rather than an error.
    assert stats["files_analyzed"] == 1, "only the present file is analyzed"
    total_deletes = (
        len(analyzer.modules_collection.data.delete_many_calls)
        + len(analyzer.functions_collection.data.delete_many_calls)
        + len(analyzer.classes_collection.data.delete_many_calls)
    )
    assert total_deletes >= 1, (
        "the deleted file must trigger a prune (delete_many), not a silent skip"
    )


def test_analyze_unchanged_file_writes_zero_objects(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """The per-file hash short-circuit (``_get_existing_module``) makes an
    unchanged file a no-op: 0 modules/classes/functions, 0 writes. This is
    the 'just the hashes' win — re-running the per-edit sync on a file whose
    content didn't change must not re-churn its objects."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    repo = tmp_path
    (repo / "alpha.py").write_text(
        "def alpha_func():\n    return 1\n"
    )

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    # Simulate 'already indexed, hash matches' for EVERY file: the real
    # per-file fast path returns the stored UUID, so analyze_*_file returns
    # all-zero stats before any insert. This exercises the single-file path's
    # routing through that skip (the skip logic itself is pinned in
    # test_code_graph_content_hash_skip.py + test_analyze_code_graph_v0_2_16).
    monkeypatch.setattr(
        analyzer_mod.CodeGraphAnalyzer,
        "_get_existing_module",
        lambda self, path, file_hash: "already-indexed-uuid",
    )

    stats = analyzer.analyze_repository(
        repo, only_file=(repo / "alpha.py"), extract_cfg=False, extract_pdg=False
    )

    assert stats["files_analyzed"] == 1, "the file is still 'analyzed' (walked)"
    assert stats["modules"] == 0, "unchanged file must write 0 modules"
    assert stats["classes"] == 0, "unchanged file must write 0 classes"
    assert stats["functions"] == 0, "unchanged file must write 0 functions"
    assert _all_replace_calls(analyzer) == [], (
        "unchanged file must write 0 objects to ANY collection (hash-skip)"
    )


# ---------------------------------------------------------------------------
# Mutual-exclusion: --only-file cannot combine with whole-tree flags.
# ---------------------------------------------------------------------------


def test_analyze_only_file_with_extras_is_ignored_at_repo_level(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """At the ``analyze_repository`` level, ``only_file`` takes precedence:
    extras are never walked in single-file mode (the CLI surface rejects the
    combination outright; this guards the library contract too)."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "z.py").write_text("def z():\n    return 9\n")

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    stats = analyzer.analyze_repository(
        repo,
        only_file=(repo / "a.py"),
        extra_paths=[extra],  # must be ignored in single-file mode
        extract_cfg=False,
        extract_pdg=False,
    )
    assert stats["files_analyzed"] == 1
    blob = repr(_all_replace_calls(analyzer))
    assert "z.py" not in blob and "def z" not in blob, (
        "extras must NOT be walked when only_file is set"
    )


# ---------------------------------------------------------------------------
# v0.2.66 (Bug 3, part b): canonical_source dedups worktree edits onto the
# main-checkout object (the orphan-object / disk-write-peak root cause).
# ---------------------------------------------------------------------------


def _written_module_props(analyzer) -> List[Dict[str, Any]]:
    """All CodeModule object properties written this run."""
    out: List[Dict[str, Any]] = []
    for call in analyzer.modules_collection.data.replace_calls:
        props = call.get("properties")
        if isinstance(props, dict):
            out.append(props)
    return out


def test_canonical_source_stamps_main_root_not_worktree(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """A worktree edit (repo_path = worktree root) with ``canonical_source``
    set to the MAIN repo root must stamp ``project_source`` = main root on the
    written objects — NOT the worktree root. This is what collapses the
    per-worktree duplicate onto the one canonical object."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    worktree = tmp_path / "wt-feature-x"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "mod.py").write_text("def f():\n    return 1\n")
    main_root = (tmp_path / "main-repo").as_posix()

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    analyzer.analyze_repository(
        worktree,
        only_file=(worktree / "src" / "mod.py"),
        canonical_source=main_root,
        extract_cfg=False,
        extract_pdg=False,
    )

    mods = _written_module_props(analyzer)
    assert mods, "the edited module must be written"
    for props in mods:
        assert props.get("project_source") == main_root, (
            "project_source must be the canonical MAIN root, not the worktree "
            f"root — got {props.get('project_source')!r}"
        )
        assert worktree.as_posix() not in (props.get("project_source") or ""), (
            "the worktree root must NOT leak into project_source (that's the "
            "duplicate-object key we're eliminating)"
        )
        # The file_path stays the in-repo relative path (identical between a
        # worktree and its main checkout — that's why only project_source had
        # to be canonicalized).
        assert props.get("path") == "src/mod.py"


def test_worktree_and_main_edit_converge_on_same_uuid(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """The dedup proof: editing ``src/mod.py`` from a WORKTREE (with
    canonical_source = main root) and editing the SAME file from the MAIN
    checkout must write the SAME deterministic module UUID — i.e. ONE
    canonical object, not two. Pre-fix the worktree run keyed under the
    worktree's absolute root → a distinct UUID → a duplicate that never got
    pruned (the orphan accumulation behind the disk-write peaks)."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    main_root_dir = tmp_path / "main-repo"
    main_root_dir.mkdir()
    (main_root_dir / "src").mkdir()
    (main_root_dir / "src" / "mod.py").write_text("def f():\n    return 1\n")
    main_root = main_root_dir.as_posix()

    worktree = tmp_path / "wt-feature-x"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "mod.py").write_text("def f():\n    return 1\n")

    # Run 1: edit from the MAIN checkout (canonical_source == its own root,
    # exactly what the hook passes for a main-checkout edit).
    a_main = _wire_full_flow_analyzer(analyzer_mod)
    a_main.analyze_repository(
        main_root_dir,
        only_file=(main_root_dir / "src" / "mod.py"),
        canonical_source=main_root,
        extract_cfg=False,
        extract_pdg=False,
    )
    main_uuids = {c["uuid"] for c in a_main.modules_collection.data.replace_calls}

    # Run 2: edit the SAME logical file from a WORKTREE, canonical_source =
    # main root. Must produce the SAME module UUID → dedup.
    a_wt = _wire_full_flow_analyzer(analyzer_mod)
    a_wt.analyze_repository(
        worktree,
        only_file=(worktree / "src" / "mod.py"),
        canonical_source=main_root,
        extract_cfg=False,
        extract_pdg=False,
    )
    wt_uuids = {c["uuid"] for c in a_wt.modules_collection.data.replace_calls}

    assert main_uuids and wt_uuids, "both runs must write the module"
    assert main_uuids == wt_uuids, (
        "worktree edit and main-checkout edit of the same file must converge "
        "on the SAME canonical module UUID (one object, not a per-worktree "
        f"duplicate) — main={main_uuids} worktree={wt_uuids}"
    )


def test_divergent_project_breaks_convergence_documents_concern1(
    analyzer_mod: types.ModuleType, tmp_path: Path, monkeypatch
) -> None:
    """CONCERN-1 at the analyzer level: the UUID seed includes ``project``
    (analyze_code_graph.py:_deterministic_uuid). Even with an identical
    ``canonical_source`` and relative path, a DIVERGENT ``project`` yields a
    DIFFERENT UUID. This is WHY the per-worktree basename fallback breaks dedup
    and WHY the hook re-resolves project against the canonical main root. The
    analyzer faithfully uses whatever project it's given — the canonicalization
    has to happen in the hook, which this test documents by showing the analyzer
    diverges when project diverges."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    canon = (tmp_path / "main-repo").as_posix()

    def _run_with_project(project_name: str, root_subdir: str):
        root = tmp_path / root_subdir
        (root / "src").mkdir(parents=True)
        (root / "src" / "mod.py").write_text("def f():\n    return 1\n")
        a = _wire_full_flow_analyzer(analyzer_mod, project=project_name)
        a.analyze_repository(
            root,
            only_file=(root / "src" / "mod.py"),
            canonical_source=canon,  # SAME canonical source for both
            extract_cfg=False,
            extract_pdg=False,
        )
        return {c["uuid"] for c in a.modules_collection.data.replace_calls}

    # Same project + same canonical_source + same rel path → SAME UUID.
    same_a = _run_with_project("CanonicalProject", "ra")
    same_b = _run_with_project("CanonicalProject", "rb")
    assert same_a == same_b, "identical project must converge"

    # DIVERGENT project (the per-worktree basename leak) → DIFFERENT UUID,
    # despite identical canonical_source + rel path. This is the gap the hook
    # fix closes by canonicalizing project too.
    diverged = _run_with_project("ephemeral-track-zzz", "rc")
    assert diverged != same_a, (
        "a divergent project must produce a DIFFERENT UUID — proving project "
        "is part of the seed and must be canonicalized (hook-side) for dedup"
    )


def test_single_file_dispatch_skips_claude_state(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """v0.2.66 (Bug 3, part c): a file under ``.claude/state/`` is transient
    scratch (tool_backups snapshots) and must NEVER be indexed — even via a
    direct analyzer invocation (the hook also guards it)."""
    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    state_file = tmp_path / ".claude" / "state" / "tool_backups" / "snap.py"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("x = 1\n")
    out = analyzer._single_file_dispatch(
        state_file, tmp_path, _lang_dispatch(analyzer), None
    )
    assert out == [], ".claude/state/ files must be a no-op in single-file mode"


# ---------------------------------------------------------------------------
# Hook-level live coverage (Bug 3 parts b + c) — drive the TEMPLATE hook in a
# real git repo + worktree via an argv-recording stub analyzer. Skipped when
# git or bash is unavailable.
# ---------------------------------------------------------------------------


_HOOK_SH = _REPO_ROOT / "templates" / "hooks" / "code-graph-incremental.sh"

_ARGV_STUB = """\
import json, os, sys
with open(os.environ["VCT_ARGV_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
"""


def _git(args: List[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _run_hook(edited_file: Path, repo_path: Path, project: str,
              stub: Path, argv_log: Path) -> str:
    """Invoke the template hook (which backgrounds the analyzer stub) and
    return the recorded argv JSONL (possibly empty when the hook skipped).

    The hook runs the analyzer in a backgrounded subshell (`( ... ) &`). We
    wrap the call in `bash -c '... ; wait'` so the recorded argv has landed
    by the time the outer process returns — deterministic, no sleep-polling.
    """
    env = {
        **os.environ,
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": sys.executable,
        "VCT_ARGV_LOG": str(argv_log),
    }
    # `wait` blocks on the hook's backgrounded analyzer child before returning.
    # When `project` is empty we pass only 2 positional args so the hook
    # resolves PROJECT_NAME itself (exercising the basename-fallback path that
    # CONCERN-1 targets); otherwise pass $3 explicitly.
    if project:
        script = f'bash {json.dumps(str(_HOOK_SH))} "$1" "$2" "$3"; wait'
        argv = ["_", str(edited_file), str(repo_path), project]
    else:
        script = f'bash {json.dumps(str(_HOOK_SH))} "$1" "$2"; wait'
        argv = ["_", str(edited_file), str(repo_path)]
    subprocess.run(
        ["bash", "-c", script, *argv],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return argv_log.read_text() if argv_log.exists() else ""


def _analyzer_arg(argv: List[str], flag: str) -> Optional[str]:
    """Return the value following ``flag`` in an analyzer argv, or None."""
    return argv[argv.index(flag) + 1] if flag in argv else None


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_canonicalizes_worktree_edit_to_main_root(tmp_path: Path) -> None:
    """A code edit inside a git LINKED WORKTREE must invoke the analyzer with
    the worktree root as repo_path (on-disk relativization) AND the MAIN repo
    root via --canonical-source — proving the worktree-dedup wiring."""
    main = tmp_path / "main"
    main.mkdir()
    _git(["init", "-q"], main)
    (main / "a.py").write_text("x = 1\n")
    _git(["add", "a.py"], main)
    _git(["commit", "-q", "-m", "init"], main)

    worktree = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(worktree)], main)
    edited = worktree / "src" / "mod.py"
    edited.parent.mkdir(parents=True)
    edited.write_text("def f():\n    return 1\n")

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)
    argv_log = tmp_path / "argv.jsonl"
    argv_log.write_text("")

    log = _run_hook(edited, worktree, "TestProj", stub, argv_log)
    assert log.strip(), "analyzer should have been invoked for a worktree edit"
    row = json.loads(log.strip().splitlines()[0])
    argv = row["argv"]
    # repo_path positional = the worktree root (on-disk relativization root).
    assert argv[0] == str(worktree)
    # --only-file = the actual edited file.
    assert "--only-file" in argv
    assert argv[argv.index("--only-file") + 1] == str(edited)
    # --canonical-source = the MAIN repo root (dedup key), NOT the worktree.
    assert "--canonical-source" in argv
    canon = argv[argv.index("--canonical-source") + 1]
    assert canon == str(main), (
        f"--canonical-source must be the MAIN repo root {main}, got {canon}"
    )
    assert canon != str(worktree)


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_main_checkout_edit_is_canonical_noop(tmp_path: Path) -> None:
    """A normal (non-worktree) main-checkout edit must pass repo_path ==
    --canonical-source (both the main root) — the canonicalization is a no-op
    for the common case."""
    main = tmp_path / "main"
    main.mkdir()
    _git(["init", "-q"], main)
    (main / "seed.py").write_text("x = 1\n")
    _git(["add", "seed.py"], main)
    _git(["commit", "-q", "-m", "init"], main)
    edited = main / "mod.py"
    edited.write_text("def f():\n    return 1\n")

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)
    argv_log = tmp_path / "argv.jsonl"
    argv_log.write_text("")

    log = _run_hook(edited, main, "TestProj", stub, argv_log)
    assert log.strip(), "analyzer should have been invoked"
    argv = json.loads(log.strip().splitlines()[0])["argv"]
    canon = argv[argv.index("--canonical-source") + 1]
    assert argv[0] == str(main)
    assert canon == str(main), "main-checkout edit: canonical-source == repo root"


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_skips_claude_state_path(tmp_path: Path) -> None:
    """An edit under .claude/state/ must NOT invoke the analyzer at all."""
    main = tmp_path / "main"
    main.mkdir()
    _git(["init", "-q"], main)
    (main / "seed.py").write_text("x = 1\n")
    _git(["add", "seed.py"], main)
    _git(["commit", "-q", "-m", "init"], main)
    edited = main / ".claude" / "state" / "tool_backups" / "snap.py"
    edited.parent.mkdir(parents=True)
    edited.write_text("y = 2\n")

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)
    argv_log = tmp_path / "argv.jsonl"
    argv_log.write_text("")

    log = _run_hook(edited, main, "TestProj", stub, argv_log)
    assert log.strip() == "", (
        ".claude/state/ edit must be a no-op (analyzer not invoked)"
    )


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_skips_non_git_scratch_path(tmp_path: Path) -> None:
    """A code file with NO resolvable git main root (transient scratch) must
    be a conservative no-op — never indexed under a throwaway root."""
    scratch = tmp_path / "scratch_no_git"
    scratch.mkdir()
    edited = scratch / "thing.py"
    edited.write_text("z = 3\n")

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)
    argv_log = tmp_path / "argv.jsonl"
    argv_log.write_text("")

    log = _run_hook(edited, scratch, "TestProj", stub, argv_log)
    assert log.strip() == "", (
        "a non-git scratch path must be a no-op (no canonical root → skip)"
    )


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_worktree_project_name_converges_via_basename_fallback(
    tmp_path: Path,
) -> None:
    """CONCERN-1 regression: the object UUID is keyed on `project` too, and
    `project` is resolved PER-ROOT with a basename fallback. With NO registered
    launcher project (the ephemeral `isolation: worktree` case), a main-checkout
    edit resolves project = basename(main root) while a worktree edit — pre-fix —
    resolved project = basename(WORKTREE root), which differs → distinct UUID →
    duplicates persist. After the fix, the hook re-resolves project against the
    CANONICAL MAIN root for a worktree edit, so BOTH produce the same project.

    This test drives the hook with NO `$3` (forcing the basename-fallback path)
    and a worktree whose basename DELIBERATELY differs from the main root's.
    It FAILS on the pre-fix hook (worktree basename leaks into --project) and
    PASSES after the project_name canonicalization. There is no registered
    vct_project_config resolver in these temp repos, so the basename fallback
    is the one actually exercised (exactly the ephemeral-worktree scenario)."""
    # Main repo basename and worktree basename are intentionally DIFFERENT.
    main = tmp_path / "proj-main-root"
    main.mkdir()
    _git(["init", "-q"], main)
    (main / "src").mkdir()
    (main / "src" / "mod.py").write_text("def f():\n    return 1\n")
    _git(["add", "."], main)
    _git(["commit", "-q", "-m", "init"], main)

    worktree = tmp_path / "ephemeral-track-zzz"  # basename != main basename
    _git(["worktree", "add", "-q", str(worktree)], main)
    wt_edited = worktree / "src" / "mod.py"
    wt_edited.write_text("def f():\n    return 2\n")  # edited in the worktree

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)

    # Run 1: main-checkout edit, no $3 → project = basename(main root).
    log_main_path = tmp_path / "argv_main.jsonl"
    log_main_path.write_text("")
    log_main = _run_hook(
        main / "src" / "mod.py", main, "", stub, log_main_path
    )
    assert log_main.strip(), "main-checkout edit should invoke the analyzer"
    main_argv = json.loads(log_main.strip().splitlines()[0])["argv"]
    main_project = _analyzer_arg(main_argv, "--project")

    # Run 2: worktree edit, no $3 → project would naively be the worktree
    # basename; after the fix it re-resolves to basename(canonical main root).
    log_wt_path = tmp_path / "argv_wt.jsonl"
    log_wt_path.write_text("")
    log_wt = _run_hook(wt_edited, worktree, "", stub, log_wt_path)
    assert log_wt.strip(), "worktree edit should invoke the analyzer"
    wt_argv = json.loads(log_wt.strip().splitlines()[0])["argv"]
    wt_project = _analyzer_arg(wt_argv, "--project")

    # The worktree basename must NOT leak into --project (the bug).
    assert wt_project != worktree.name, (
        "CONCERN-1 regression: worktree edit used the WORKTREE basename "
        f"({worktree.name!r}) as --project — duplicates would accumulate"
    )
    # Both edits of the SAME logical file must resolve the SAME project.
    assert wt_project == main_project, (
        "worktree edit and main-checkout edit must resolve the SAME --project "
        f"(got worktree={wt_project!r} main={main_project!r}) so their object "
        "UUIDs converge"
    )
    # And both must share the canonical-source (the main root) too — the full
    # UUID-determining triple (project + canonical-source + rel path) matches.
    assert (
        _analyzer_arg(wt_argv, "--canonical-source")
        == _analyzer_arg(main_argv, "--canonical-source")
    ), "worktree and main edits must share the canonical-source root"


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash unavailable — hook-level live coverage skipped",
)
def test_hook_extras_keep_parent_project_name(tmp_path: Path) -> None:
    """The CONCERN-1 fix must NOT touch the extras case: an extra-path edit
    keeps the PARENT project's name (extras index into the parent's
    collections). We approximate the extras path by asserting the
    re-resolution is gated on the non-extras branch — exercised indirectly:
    a normal main-checkout edit with an explicit $3 must pass that $3 through
    unchanged (the re-resolution only fires when canonical root != on-disk
    root, which is false for a main checkout)."""
    main = tmp_path / "parent"
    main.mkdir()
    _git(["init", "-q"], main)
    (main / "a.py").write_text("x = 1\n")
    _git(["add", "a.py"], main)
    _git(["commit", "-q", "-m", "init"], main)
    edited = main / "mod.py"
    edited.write_text("def f():\n    return 1\n")

    stub = tmp_path / "stub.py"
    stub.write_text(_ARGV_STUB)
    argv_log = tmp_path / "argv.jsonl"
    argv_log.write_text("")

    # Explicit $3 = a parent-project prefix that differs from basename(main).
    log = _run_hook(edited, main, "ParentPrefix", stub, argv_log)
    argv = json.loads(log.strip().splitlines()[0])["argv"]
    assert _analyzer_arg(argv, "--project") == "ParentPrefix", (
        "a main-checkout edit must pass the caller-supplied $3 through "
        "unchanged (re-resolution only fires for worktree edits)"
    )


# ---------------------------------------------------------------------------
# Live end-to-end (skipped when Weaviate is not reachable).
# ---------------------------------------------------------------------------


def _weaviate_reachable() -> bool:
    try:
        urllib.request.urlopen(
            "http://localhost:8081/v1/.well-known/ready", timeout=2
        )
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _weaviate_reachable(),
    reason="Weaviate not reachable on localhost:8081 — live single-file e2e skipped",
)
def test_live_unchanged_file_writes_zero_objects_on_rerun(
    analyzer_mod: types.ModuleType, tmp_path: Path
) -> None:
    """End-to-end against a live Weaviate: analyze one file, then re-run on
    the SAME unchanged file — the second run must write 0 new objects
    (per-file hash hit). Uses a throwaway project prefix so it can't collide
    with a real project's collections.

    Skipped automatically when no embedding backend is reachable (the
    analyzer's own soft-fail gate); a degraded code graph would make the
    assertion meaningless."""
    project = "Bug3LiveScopeTest"
    repo = tmp_path
    (repo / "only.py").write_text(
        "def only_func():\n    return 42\n"
    )

    # Build a real analyzer + embedding service via the public connect path.
    try:
        svc = analyzer_mod.EmbeddingService.for_project(repo)
    except Exception as exc:  # NoEmbeddingBackendError or import gap
        pytest.skip(f"embedding service unavailable: {exc}")
    if not svc.code_backend_ready():
        svc.close()
        pytest.skip("code embedding backend not reachable — skipping live e2e")
    analyzer_mod._set_embedding_service(svc)

    analyzer = analyzer_mod.CodeGraphAnalyzer(project, named_vectors=True)
    if not analyzer.connect():
        svc.close()
        pytest.skip("could not connect to Weaviate — skipping live e2e")
    try:
        # Fresh collections so the run is deterministic.
        analyzer.create_collections(force=True)

        first = analyzer.analyze_repository(
            repo, only_file=(repo / "only.py"),
            extract_cfg=False, extract_pdg=False,
        )
        assert first["files_analyzed"] == 1
        assert first["modules"] >= 1 and first["functions"] >= 1, (
            "first run must index the file's objects"
        )

        # Re-run on the UNCHANGED file → per-file hash hit → 0 new objects.
        second = analyzer.analyze_repository(
            repo, only_file=(repo / "only.py"),
            extract_cfg=False, extract_pdg=False,
        )
        assert second["modules"] == 0, "unchanged re-run must write 0 modules"
        assert second["functions"] == 0, "unchanged re-run must write 0 functions"
        assert second["classes"] == 0, "unchanged re-run must write 0 classes"
    finally:
        # Clean up the throwaway collections so the live instance isn't
        # polluted by the test's project prefix.
        try:
            for coll in (
                analyzer.coll_module, analyzer.coll_class, analyzer.coll_function,
                analyzer.coll_api, analyzer.coll_interaction,
            ):
                try:
                    analyzer.client.collections.delete(coll)
                except Exception:
                    pass
        except Exception:
            pass
        analyzer.close()
        svc.close()
