# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression + unit tests for the v0.2.16 W1 analyzer-integrity fixes.

Covers:
  - bug 0.1 / replace() idempotency
  - bug 0.7 / path-aware UUIDs (and cross-OS normalization)
  - bug 0.8 / NameError on class_uuid (line 3464 area, post-renumber)
  - bug 0.2 / non-zero exit codes when inserts fail
  - addendum D / worktree-skip + ignore_dirs refactor
  - addendum H / --prune-stale tracking + deletion

Most tests are pure-Python unit tests against the analyzer module's
helpers and don't require a running Weaviate. The two tests that
genuinely need Weaviate (insert-error exit, prune-stale e2e) are
gated by ``requires_weaviate`` and skip cleanly when the service is
unreachable.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
import types
from pathlib import Path
from typing import Any, List

import pytest


# ---------------------------------------------------------------------------
# Import the analyzer module from templates/scripts/ without it landing on
# sys.path as `analyze_code_graph` for downstream tests. We load it under a
# private name to avoid clashes with the rendered .claude/scripts copy that
# other tests may pull in.
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    """Load the analyzer script as a module without touching sys.path
    side-effects. Returns the imported module."""
    spec = importlib.util.spec_from_file_location(
        "_w1_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.skip(f"Cannot load analyzer module from {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        # The script calls sys.exit() on weaviate-client import failure.
        # When that happens, the env isn't set up for these tests anyway.
        pytest.skip("weaviate-client unavailable — analyzer cannot be loaded")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# Test 1 — _dedup_insert idempotency (bug 0.1)
# ---------------------------------------------------------------------------


class _FakeCollectionData:
    """Stand-in for ``collection.data`` — records every replace() call so the
    test can assert idempotency without touching Weaviate."""

    def __init__(self) -> None:
        self.replace_calls: List[Any] = []
        self.insert_calls: List[Any] = []

    def replace(self, uuid: str, **kwargs: Any) -> None:
        # Mirrors weaviate-client v4 contract: replace returns None.
        self.replace_calls.append({"uuid": uuid, **kwargs})
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        # Old behaviour: 422 on duplicate. The pre-v0.2.16 _dedup_insert
        # used this. Kept to detect regressions.
        if any(c.get("uuid") == uuid for c in self.insert_calls):
            raise RuntimeError(f"id '{uuid}' already exists")
        self.insert_calls.append({"uuid": uuid, **kwargs})
        return uuid

    def delete_by_id(self, uuid: str) -> None:
        # No-op — used by the prune-stale path; signature only matters for tests.
        pass


class _FakeCollection:
    """Minimal stand-in for a Weaviate v4 collection object."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.data = _FakeCollectionData()


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "TestProject"):
    """Construct a real CodeGraphAnalyzer without connecting to Weaviate."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    # Replicate __init__ minimally (we skip Weaviate connection entirely).
    inst.project_name = project
    inst.client = None
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    # v0.2.72 (P5): __init__ now sets index_dot_claude; the walkers read it
    # directly, so the __new__-based fixture must set it. True here — this
    # file's walk test pins the ORIGINAL v0.2.16 intent: `.claude/worktrees/`
    # is skipped EVEN when `.claude/` itself is indexed (orchestrator-root
    # mode). The user-project False default is covered by the P5 ignore-set
    # tests (test_codegraph_ignore_set.py).
    inst.index_dot_claude = True
    return inst


def test_dedup_insert_idempotent(analyzer_mod):
    """Bug 0.1 regression: calling _dedup_insert twice with the same identity
    must NOT raise (replace() is idempotent). Same UUID must be returned."""
    analyzer = _make_analyzer(analyzer_mod)
    fake_coll = _FakeCollection("Test_CodeFunction")

    insert_params = {"properties": {"name": "foo", "full_name": "mod.foo"}}

    # Call 1 — fresh insert.
    uuid_a = analyzer._dedup_insert(
        fake_coll, insert_params, "mod.foo", file_path_rel="src/mod.py"
    )
    # Call 2 — same identity, should be an idempotent upsert (no exception).
    uuid_b = analyzer._dedup_insert(
        fake_coll, insert_params, "mod.foo", file_path_rel="src/mod.py"
    )

    assert uuid_a == uuid_b, "Same identity must yield the same deterministic UUID"
    assert len(fake_coll.data.replace_calls) == 2, (
        "replace() must be called both times (idempotent upsert)"
    )
    assert len(fake_coll.data.insert_calls) == 0, (
        "insert() must NEVER be called by _dedup_insert in v0.2.16 — replace() is the new contract"
    )


# ---------------------------------------------------------------------------
# Test 2 — UUID identity-key includes file_path_rel (bug 0.7)
# ---------------------------------------------------------------------------


def test_uuid_includes_file_path(analyzer_mod):
    """Bug 0.7 regression: two genuinely-different files with the same
    module-stem + symbol name must produce different UUIDs.

    Pre-v0.2.16 the UUID key was ``project::full_name`` — these two collided.
    """
    uuid_a = analyzer_mod._deterministic_uuid(
        "MyProject", "claude_mcp_servers/server.py", "server.handler"
    )
    uuid_b = analyzer_mod._deterministic_uuid(
        "MyProject", "docs/research/probes/server.py", "server.handler"
    )
    assert uuid_a != uuid_b, (
        "Same symbol in different files must produce different UUIDs in v0.2.16"
    )

    # And the same (project, path, name) triple must still be stable across calls.
    uuid_a_again = analyzer_mod._deterministic_uuid(
        "MyProject", "claude_mcp_servers/server.py", "server.handler"
    )
    assert uuid_a == uuid_a_again, "Deterministic UUID must be stable across calls"


# ---------------------------------------------------------------------------
# Test 3 — UUID normalises Windows-backslash paths (cross-OS contract)
# ---------------------------------------------------------------------------


def test_uuid_normalizes_windows_path(analyzer_mod):
    """Cross-OS contract: a caller passing a POSIX path and a caller passing
    the same path with backslashes (then ``.as_posix()``-normalised) must
    produce the SAME UUID. Asserts that the call sites correctly normalise
    via ``Path(...).as_posix()`` before threading into ``_deterministic_uuid``.
    """
    # POSIX form
    posix_path = "src/sub/module.py"
    # Windows form — what a Path object would have if constructed on Windows
    # from the same relative path. PureWindowsPath ensures the conversion
    # logic is exercised regardless of host OS.
    from pathlib import PureWindowsPath
    windows_path = PureWindowsPath("src", "sub", "module.py")
    normalized_from_windows = windows_path.as_posix()

    assert normalized_from_windows == posix_path, (
        "PureWindowsPath('src','sub','module.py').as_posix() must yield 'src/sub/module.py'"
    )

    uuid_posix = analyzer_mod._deterministic_uuid(
        "MyProject", posix_path, "module.Foo"
    )
    uuid_normalised = analyzer_mod._deterministic_uuid(
        "MyProject", normalized_from_windows, "module.Foo"
    )
    assert uuid_posix == uuid_normalised, (
        "Cross-OS UUID stability broken — caller must POSIX-normalise paths "
        "before passing to _deterministic_uuid (use .as_posix())"
    )

    # Sanity: if the caller forgets to normalise and passes a backslash
    # string, the UUID WILL differ (this asserts the contract is real, not
    # a coincidence of helpers normalising under the hood).
    uuid_unnormalised = analyzer_mod._deterministic_uuid(
        "MyProject", "src\\sub\\module.py", "module.Foo"
    )
    assert uuid_unnormalised != uuid_posix, (
        "If this passes, the helper is normalising internally — which would "
        "mask caller bugs. Keep the contract: callers MUST .as_posix()."
    )


# ---------------------------------------------------------------------------
# Test 4 — _find_python_files skips .claude/worktrees/ (addendum D)
# ---------------------------------------------------------------------------


def test_walk_skips_worktrees(analyzer_mod, tmp_path: Path):
    """Addendum D regression: ``_find_python_files`` must skip every file
    under any ``worktrees/`` directory (typically ``.claude/worktrees/agent-*``).

    Setup:
        repo/
          src/main.py                                          ← MUST appear
          .claude/worktrees/agent-abc/src/main.py             ← MUST NOT appear
          .claude/worktrees/agent-xyz/some/other/file.py      ← MUST NOT appear
          .claude/scripts/some_real_script.py                  ← MUST appear
    """
    repo = tmp_path / "repo"
    src_main = repo / "src" / "main.py"
    src_main.parent.mkdir(parents=True)
    src_main.write_text("def hello(): pass\n")

    wt_main = repo / ".claude" / "worktrees" / "agent-abc" / "src" / "main.py"
    wt_main.parent.mkdir(parents=True)
    wt_main.write_text("def hello(): pass\n")

    wt_other = repo / ".claude" / "worktrees" / "agent-xyz" / "some" / "other" / "file.py"
    wt_other.parent.mkdir(parents=True)
    wt_other.write_text("x = 1\n")

    legit_hook = repo / ".claude" / "scripts" / "some_real_script.py"
    legit_hook.parent.mkdir(parents=True)
    legit_hook.write_text("# real hook\n")

    analyzer = _make_analyzer(analyzer_mod)
    found = analyzer._find_python_files(repo)
    found_paths = sorted(str(p.relative_to(repo)) for p in found)

    assert "src/main.py" in found_paths or "src" + os.sep + "main.py" in found_paths
    assert any("scripts" in p and "some_real_script.py" in p for p in found_paths), (
        ".claude/scripts/ files must STILL be analyzed (only .claude/worktrees/ is skipped)"
    )

    # Critical: no path containing 'worktrees' must appear.
    for p in found_paths:
        assert "worktrees" not in p, (
            f"File from a git worktree clone leaked through: {p}. "
            "addendum D fix is missing — _COMMON_IGNORE_DIRS must contain 'worktrees'."
        )


def test_common_ignore_dirs_contains_worktrees(analyzer_mod):
    """Belt-and-braces: assert the constant itself contains 'worktrees'."""
    assert "worktrees" in analyzer_mod._COMMON_IGNORE_DIRS


def test_ignore_dirs_for_language_inherits_common(analyzer_mod):
    """Language-specific extras must MERGE with the common set, not replace it."""
    go_dirs = analyzer_mod._ignore_dirs_for("go")
    # Common entries
    assert ".git" in go_dirs
    assert "node_modules" in go_dirs
    assert "worktrees" in go_dirs
    # Go-specific
    assert "vendor" in go_dirs

    rust_dirs = analyzer_mod._ignore_dirs_for("rust")
    assert "target" in rust_dirs
    assert "worktrees" in rust_dirs

    # Unknown language gets the common set only — must not error.
    unknown = analyzer_mod._ignore_dirs_for("nonexistentlang")
    assert "worktrees" in unknown


# ---------------------------------------------------------------------------
# Test 5 — Class extraction no longer raises NameError on class_uuid (bug 0.8)
# ---------------------------------------------------------------------------


def test_class_extraction_no_nameerror(analyzer_mod, tmp_path: Path):
    """Bug 0.8 regression. The pre-v0.2.16 code on line 3312/3314 had:

        self._dedup_insert(...)                               # return discarded
        self.class_cache[...] = class_uuid                    # NameError: never bound

    With the fix (capture return value), parsing a file with a class must
    no longer raise NameError. We invoke _extract_class with a fake
    classes_collection that records replace() calls.
    """
    import ast

    py_source = textwrap.dedent("""\
        class Foo:
            def bar(self):
                return 42
    """)
    src_file = tmp_path / "modfoo.py"
    src_file.write_text(py_source)

    analyzer = _make_analyzer(analyzer_mod)
    # Wire up fake collections so the _dedup_insert calls don't hit Weaviate.
    analyzer.classes_collection = _FakeCollection("Test_CodeClass")
    analyzer.functions_collection = _FakeCollection("Test_CodeFunction")
    # _extract_class also writes to class_cache after the insert. That used
    # to NameError; now it should succeed.
    analyzer.class_cache = {}
    analyzer.function_cache = {}

    tree = ast.parse(py_source)
    class_node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))

    # Should not raise.
    analyzer._extract_class(
        class_node,
        module_uuid="fake-module-uuid",
        file_path=src_file,
        repo_root=tmp_path,
        source_lines=py_source.splitlines(),
    )

    # Class cache must be populated with the new UUID (proves class_uuid
    # was bound and the cache assignment succeeded).
    assert "modfoo.Foo" in analyzer.class_cache, (
        "class_cache must include the new class after _extract_class — "
        "bug 0.8 fix (capture return value) appears to have regressed"
    )
    assert analyzer.class_cache["modfoo.Foo"], "class_uuid must be a non-empty string"


# ---------------------------------------------------------------------------
# Test 6 — Insert-error exit code (bug 0.2) — Weaviate-gated end-to-end
# ---------------------------------------------------------------------------


def _weaviate_reachable() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:8081/v1/.well-known/ready", timeout=2)
        return True
    except Exception:
        return False


def test_main_exits_4_on_insert_errors(analyzer_mod, tmp_path: Path, monkeypatch):
    """Bug 0.2 regression: when SOME per-file analyses succeed but at least
    one trips a ``_DedupInsertError``, the script must exit with code 4
    (not 0). Pre-v0.2.16 the script always exited 0 even when most files
    failed to write — the launcher rendered green-toast success and
    users had no idea their code graph was empty.

    To exercise the partial-failure path, the runner creates two files
    and patches ``collection.data.replace`` to fail only on the second one.
    File 1 succeeds → files_analyzed == 1, exit-3 NO_FILES_INDEXED is
    skipped. File 2 fails → insert_errors == 1, exit-4 fires.
    """
    import subprocess
    import textwrap as _tw

    repo = tmp_path / "fakerepo"
    repo.mkdir()
    (repo / "good.py").write_text("def good_fn():\n    return 1\n")
    (repo / "bad.py").write_text("def bad_fn():\n    return 2\n")

    runner = tmp_path / "runner.py"
    runner.write_text(_tw.dedent(f"""\
        import sys, importlib.util
        spec = importlib.util.spec_from_file_location("acg", r"{_ANALYZER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Build a per-collection fake whose replace() raises only for the
        # 'bad.py' module insert. Identifying via insert_params content.
        class _Coll:
            def __init__(self, name):
                self.name = name
                self.data = self
            def replace(self, uuid, **kwargs):
                props = kwargs.get("properties", {{}})
                path = props.get("path", "") or props.get("full_name", "") or ""
                if "bad" in path:
                    raise RuntimeError("simulated weaviate 422 on bad.py")
                return None
            def update(self, uuid, **kwargs):
                return None
            def insert(self, uuid, **kwargs):
                raise AssertionError("v0.2.16 must NOT call insert() — replace() only")
            def iterator(self, *a, **kw):
                return iter([])
            def delete_by_id(self, uuid):
                return None
            class _query:
                @staticmethod
                def fetch_objects(*a, **kw):
                    class _R:
                        objects = []
                    return _R()
            query = _query()
            class _config:
                @staticmethod
                def get():
                    class _C:
                        properties = []
                    return _C()
                @staticmethod
                def add_property(p):
                    return None
            config = _config()

        def _fake_create_collections(self, force=False):
            self.modules_collection = _Coll("Fake_CodeModule")
            self.classes_collection = _Coll("Fake_CodeClass")
            self.functions_collection = _Coll("Fake_CodeFunction")
            self.apis_collection = _Coll("Fake_CodeAPI")
            self.interactions_collection = _Coll("Fake_CodeInteraction")
        mod.CodeGraphAnalyzer.create_collections = _fake_create_collections

        def _fake_existing_module(self, path, file_hash):
            return None
        mod.CodeGraphAnalyzer._get_existing_module = _fake_existing_module

        def _fake_connect(self):
            self.client = object()
            return True
        mod.CodeGraphAnalyzer.connect = _fake_connect

        def _fake_close(self):
            return None
        mod.CodeGraphAnalyzer.close = _fake_close

        def _fake_create_cross_refs(self, changed_files=None):
            return {{"calls": 0, "extends": 0, "imports": 0}}
        mod.CodeGraphAnalyzer.create_cross_references = _fake_create_cross_refs

        # Embeddings: bypass network calls.
        mod.generate_embedding = lambda text: None
        mod.embed_module = lambda summary: None
        mod.embed_function = lambda sig, body, language="python": None
        mod.embed_class = lambda sig, body, methods=None, language="python": None

        # v0.2.18 (Wave B Commit 5): main() now constructs an
        # EmbeddingService BEFORE the analyzer runs and bails with exit 0
        # if no backend is reachable. On CI (no Ollama / CodeEmbed
        # running) this gate fires first, short-circuiting the insert-
        # error path this test is supposed to exercise. Mock both
        # ``for_project`` and ``code_backend_ready`` so main() proceeds
        # to the analyzer + reaches the insert-call simulation.
        class _FakeSvc:
            code_vector_slot = "codesage_embed"
            code_model_id = "codesage-large-v2"
            code_dim = 2048
            text_vector_slot = "qwen3_embed"
            text_model_id = "qwen3-embedding:0.6b"
            text_dim = 1024
            def code_backend_ready(self):
                return True
            def text_backend_ready(self):
                return True
            def close(self):
                return None
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return None
        mod.EmbeddingService.for_project = classmethod(lambda cls, *a, **kw: _FakeSvc())

        sys.argv = ["analyze_code_graph.py", r"{repo}",
                    "--project", "FakeProject"]
        sys.exit(mod.main())
    """))

    proc = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "VCT_JOERN_AVAILABLE": "0"},
    )

    assert proc.returncode == 4, (
        f"Expected exit code 4 on partial insert errors, got {proc.returncode}. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "insert errors" in proc.stdout.lower() or "insert errors" in proc.stderr.lower(), (
        f"Expected operator-facing 'insert errors' message. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 — --prune-stale removes UUIDs the run didn't visit (addendum H)
# ---------------------------------------------------------------------------


def test_prune_stale_removes_unvisited(analyzer_mod):
    """Addendum H regression: when --prune-stale is on and the run visits
    UUID A but not UUID B (which exists in the collection), the prune pass
    must delete B and leave A alone.

    We exercise this against a fake collection — no Weaviate needed.
    """
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    # Pre-existing collection state: two objects, A and B.
    fake = _FakeCollection("Test_CodeFunction")
    deleted_uuids: List[str] = []

    class _Obj:
        def __init__(self, uid: str, project: str) -> None:
            self.uuid = uid
            self.properties = {"project": project}

    pre_existing = [
        _Obj("uuid-a-visited", "TestProject"),
        _Obj("uuid-b-stale", "TestProject"),
        _Obj("uuid-c-otherproj", "OtherProject"),  # must NOT be deleted
    ]

    def _iterator(return_properties=None):
        return iter(pre_existing)

    def _delete_by_id(uuid: str) -> None:
        deleted_uuids.append(uuid)

    fake.iterator = _iterator  # type: ignore[attr-defined]
    fake.data.delete_by_id = _delete_by_id  # type: ignore[method-assign]

    # Visit only UUID A.
    analyzer.visited_uuids = {(fake.name, "uuid-a-visited")}

    # v0.2.73 (C-11): _prune_collection now returns (pruned, failures).
    pruned, _failures = analyzer._prune_collection(fake, visited_uuids={"uuid-a-visited"})

    assert pruned == 1, "Exactly one stale object should be pruned"
    assert deleted_uuids == ["uuid-b-stale"], (
        f"Wrong UUIDs deleted: {deleted_uuids}. "
        "UUID A was visited (must survive); UUID C belongs to another "
        "project (must survive)."
    )
