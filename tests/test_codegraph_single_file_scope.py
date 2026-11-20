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
using ``repo_path`` only as the relativization root. (The per-edit hook that
passed ``--only-file`` was retired in v0.2.107 — the code graph now follows
commits — but the analyzer mode stays as a CLI mode for a manual re-analysis
of one file.) The single file flows through the SAME
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
import textwrap
import types
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

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
        repo, only_file=(repo / "alpha.py")
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
        repo, only_files_from=list_file
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
    self-inflicted orphan.

    v0.2.74: the prune now uses the tokenization-SAFE path (iterate + exact
    Python compare + ``delete_by_id``), NOT ``delete_many`` with a
    word-tokenized ``file_path`` filter. The fake seeds the deleted file's row
    AND a sibling row sharing word tokens to prove only the exact orphan row is
    deleted (over-delete guard)."""
    _stub_embeddings(analyzer_mod, monkeypatch)

    class _Obj:
        def __init__(self, uuid: str, path_prop: str, path_value: str) -> None:
            self.uuid = uuid
            self.properties = {
                path_prop: path_value,
                "project": "Bug3Project",
                "project_source": "",
            }

    class _Prop:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Cfg:
        def __init__(self, names) -> None:
            self.properties = [_Prop(n) for n in names]

    class _CfgHolder:
        def __init__(self, names) -> None:
            self._c = _Cfg(names)

        def get(self):
            return self._c

    class _FakeDataWithDelete(_FakeCollectionData):
        def __init__(self) -> None:
            super().__init__()
            self.deleted_ids: List[str] = []

        def delete_by_id(self, uuid: str) -> None:  # safe per-row delete
            self.deleted_ids.append(str(uuid))

    class _FakeCollWithDelete(_FakeCollection):
        def __init__(self, name: str, path_prop: str, rows) -> None:
            self.name = name
            self.data = _FakeDataWithDelete()
            self._rows = rows
            self.config = _CfgHolder((path_prop, "project", "project_source"))

        def iterator(self, return_properties=None):
            return iter(self._rows)

    analyzer = _wire_full_flow_analyzer(analyzer_mod)
    proj = analyzer.project_name
    # Seed each file-anchored collection: one row for the DELETED file (gone.py)
    # + a sibling row that shares word tokens but must survive.
    analyzer.modules_collection = _FakeCollWithDelete(
        f"{proj}_CodeModule", "path",
        [_Obj("mod-gone", "path", "gone.py"),
         _Obj("mod-keep", "path", "gone_helper.py")],
    )
    # v0.2.91 (WP-C): the surviving sibling is anchored to `gone_helper.py` — a
    # file that EXISTS on disk and is NOT in this batch. Anchoring it to
    # `present.py` (a file the batch WALKS) would make it a legitimate
    # entity-orphan for the per-file reconcile (a row for an entity the walk did
    # not emit) and it would be deleted BY DESIGN, which is a different
    # invariant. Keeping it on an unwalked file preserves this test's actual
    # intent — the deleted-file prune must not over-delete a token-sharing
    # sibling — and additionally pins the reconcile's leave-alone side.
    analyzer.functions_collection = _FakeCollWithDelete(
        f"{proj}_CodeFunction", "file_path",
        [_Obj("fn-gone", "file_path", "gone.py"),
         _Obj("fn-keep", "file_path", "gone_helper.py")],
    )
    analyzer.classes_collection = _FakeCollWithDelete(
        f"{proj}_CodeClass", "file_path", [],
    )

    repo = tmp_path
    (repo / "present.py").write_text("def present():\n    return 1\n")
    # `gone_helper.py` EXISTS on disk so it is reachable (not an orphan) — this
    # isolates the assertion to the `_prune_deleted_file_objects` exact-match on
    # `gone.py`, proving the token-sharing sibling survives the prune.
    (repo / "gone_helper.py").write_text("def helper():\n    return 2\n")
    missing = repo / "gone.py"  # never created — the deleted-file target

    list_file = repo / "batch.txt"
    list_file.write_text(f"{repo / 'present.py'}\n{missing}\n")

    stats = analyzer.analyze_repository(
        repo, only_files_from=list_file
    )

    # The present file analyzed; the missing one triggered a prune via the safe
    # delete_by_id path (NOT delete_many), and ONLY the exact orphan row(s).
    # `gone_helper.py` (real file, shares tokens with gone.py) must survive.
    assert stats["files_analyzed"] == 1, "only the present file is analyzed"
    assert "mod-gone" in analyzer.modules_collection.data.deleted_ids, (
        "the exact gone.py module row is deleted"
    )
    assert "mod-keep" not in analyzer.modules_collection.data.deleted_ids, (
        "the token-sharing on-disk sibling gone_helper.py must NOT be deleted"
    )
    assert "fn-gone" in analyzer.functions_collection.data.deleted_ids, (
        "the exact gone.py function row is deleted"
    )
    assert "fn-keep" not in analyzer.functions_collection.data.deleted_ids, (
        "gone_helper.py (real file, unwalked, token-sharing) must NOT be deleted"
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
        repo, only_file=(repo / "alpha.py")
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
    )
    assert stats["files_analyzed"] == 1
    blob = repr(_all_replace_calls(analyzer))
    assert "z.py" not in blob and "def z" not in blob, (
        "extras must NOT be walked when only_file is set"
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


# The hook-level coverage of the per-edit `code-graph-incremental.sh` hook
# (worktree canonicalisation, .claude/state and non-git skips) was retired with
# the hook in v0.2.107 (CG-3): the code graph follows commits of the main
# checkout, never an edit and never a worktree
# (tests/test_v02107_cg3_committed_graph.py).


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
        # Fresh collections so the run is deterministic. `repo_path` is
        # REQUIRED with force=True (v0.2.92 BLOCKER-2): the identity guard
        # checks the family being dropped against that folder's registered
        # bindings, and a force call that names no folder cannot be checked,
        # so it is refused. This throwaway repo is unregistered, so the guard
        # allows the drop.
        analyzer.create_collections(force=True, repo_path=repo)

        first = analyzer.analyze_repository(
            repo, only_file=(repo / "only.py"),
            
        )
        assert first["files_analyzed"] == 1
        assert first["modules"] >= 1 and first["functions"] >= 1, (
            "first run must index the file's objects"
        )

        # Re-run on the UNCHANGED file → per-file hash hit → 0 new objects.
        second = analyzer.analyze_repository(
            repo, only_file=(repo / "only.py"),
            
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
