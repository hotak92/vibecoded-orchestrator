# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105: a code-graph walk must never end with exit 0, zero modules and
nothing on stderr when source files exist (Windows field report: 146 source
files, empty log, zero modules).

Pinned here, each both ways:

* the walk judges only the path BELOW its root — a project living under a
  folder named like an ignored directory (``build``, ``env``, ``out``,
  ``worktrees``) keeps its files; an ignored directory INSIDE the project is
  still skipped (``vco_lib.codegraph_source_filter``);
* ``--incremental`` walks a never-indexed root in full and gates an indexed
  one (``vco_lib.codegraph_walk_diagnosis.root_incremental_gate``);
* a whole-tree walk that dispatched nothing prints WHY and exits 3 when
  source files exist but every one was excluded, 0 when there is nothing to
  index or the incremental gate saw no change (``empty_walk_verdict``) —
  end to end through the analyzer's ``main()``;
* the walk-claim refusal names the pid holding the claim.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import codegraph_source_filter as sf
from vco_lib import codegraph_walk_diagnosis as wd

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def acg():
    spec = importlib.util.spec_from_file_location("acg_v02105_silent_walk", ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str, text: str = "def f():\n    return 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _bare(acg):
    inst = acg.CodeGraphAnalyzer.__new__(acg.CodeGraphAnalyzer)
    inst.index_dot_claude = False
    return inst


# ── the walk judges only the path below its root ─────────────────────────────


@pytest.mark.parametrize("ancestor", ["build", "env", "out", "worktrees", "bin", ".wt"])
def test_a_project_under_an_ignored_folder_name_keeps_its_files(acg, tmp_path, ancestor):
    """Pre-fix: every file of ``<tmp>/<ancestor>/proj`` was excluded because
    the ABSOLUTE path carried the ignored segment — the silent zero-module
    walk."""
    proj = tmp_path / ancestor / "proj"
    _write(proj, "pkg/a.py")
    assert [p.name for p in _bare(acg)._find_python_files(proj)] == ["a.py"]


def test_an_ignored_folder_inside_the_project_is_still_skipped(acg, tmp_path):
    proj = tmp_path / "proj"
    _write(proj, "src/a.py")
    _write(proj, "build/generated.py")
    _write(proj, ".claude/state/tool_backups/copy.py")
    assert [p.name for p in _bare(acg)._find_python_files(proj)] == ["a.py"]
    reasons = {p.name: why for p, why in sf.classify_finder_files(
        proj, ("*.py",), None, frozenset({"build"}))}
    assert reasons == {"a.py": None, "generated.py": "directory 'build'",
                       "copy.py": "directory '.claude/state'"}


# ── verdicts ─────────────────────────────────────────────────────────────────


def test_every_file_excluded_is_loud_and_non_zero():
    census = wd.census_from_pairs("/p", [("/p/node_modules/a.js", "directory 'node_modules'"),
                                         ("/p/node_modules/b.js", "directory 'node_modules'"),
                                         ("/p/dist/c.js", "directory 'dist'")])
    v = wd.empty_walk_verdict([census])
    assert v.exit_code == wd.EXIT_NOTHING_INDEXED == 3
    text = "\n".join(v.lines)
    assert "every one of the 3 source file(s)" in text
    assert "directory 'node_modules' (2), directory 'dist' (1)" in text


def test_no_supported_file_is_loud_but_zero():
    v = wd.empty_walk_verdict([wd.census_from_pairs("/docs", [])], extensions_hint="*.py")
    assert v.exit_code == 0
    assert "no source file with a supported extension under /docs" in v.lines[0]
    assert "*.py" in v.lines[0]


def test_incremental_no_change_on_an_indexed_root_is_loud_but_zero():
    census = wd.census_from_pairs("/p", [("/p/a.py", None)])
    v = wd.empty_walk_verdict([census], incremental=True, unchanged_files=1,
                              diff_range="HEAD~1..HEAD")
    assert v.exit_code == 0
    assert "incremental gate saw no change" in v.lines[0] and "HEAD~1..HEAD" in v.lines[0]


def test_walkable_files_with_nothing_dispatched_is_non_zero():
    census = wd.census_from_pairs("/p", [("/p/a.py", None)])
    assert wd.empty_walk_verdict([census]).exit_code == 3


def test_a_walk_that_dispatched_files_reports_nothing(capsys):
    assert wd.report_empty_walk({"files_analyzed": 3}, incremental=False,
                                since_commit=None, language=None) == 0
    assert capsys.readouterr().err == ""


# ── the incremental gate ─────────────────────────────────────────────────────


class _Coll:
    def __init__(self, objects, raises=False):
        self._objects, self._raises, self.calls = objects, raises, []
        self.query = self

    def fetch_objects(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise RuntimeError("weaviate down")
        return types.SimpleNamespace(objects=self._objects)


def test_never_indexed_root_is_walked_in_full(capsys):
    assert wd.root_incremental_gate(_Coll([]), "/p") is False
    assert "has no indexed rows yet" in capsys.readouterr().err


def test_indexed_root_keeps_the_gate(capsys):
    assert wd.root_incremental_gate(_Coll([object()]), "/p") is True
    assert capsys.readouterr().err == ""


def test_unknown_keeps_the_gate(capsys):
    assert wd.root_incremental_gate(_Coll([], raises=True), "/p") is True
    assert "could not tell" in capsys.readouterr().err
    assert wd.root_incremental_gate(None, "/p") is True


def test_an_extra_root_is_judged_by_its_own_stamp():
    coll = _Coll([])
    assert wd.root_incremental_gate(coll, "/x", extra_stamp="/x") is False
    assert "filters" in coll.calls[0]


def _walk_stub(acg, module_rows):
    inst = acg.CodeGraphAnalyzer.__new__(acg.CodeGraphAnalyzer)
    inst.project_name = "Acme"
    inst.client = None
    inst._prune_failures = 0
    inst._git_deleted_report = acg._PruneReport()
    inst.index_dot_claude = False
    inst._progress_emitter = None
    inst._get_stale_file_set = lambda: None
    inst._prune_git_deleted = lambda root, since: None
    inst._track_visited = False
    inst.modules_collection = _Coll(module_rows)
    analyzed: list = []
    inst._analyze_python_file = lambda f, root: analyzed.append(f.name) or {"modules": 1}
    # every file "unchanged" in the diff range
    inst._filter_changed_files = lambda root, files, since_commit=None: []
    return inst, analyzed


def test_incremental_walk_of_a_never_indexed_project_indexes_every_file(acg, tmp_path):
    """Pre-fix: the diff gate dropped a.py and b.py as unchanged on a graph
    that held nothing — zero modules, exit 0."""
    proj = tmp_path / "proj"
    _write(proj, "a.py")
    _write(proj, "b.py")
    inst, analyzed = _walk_stub(acg, module_rows=[])
    stats = inst.analyze_repository(proj, language="python", incremental=True)
    assert sorted(analyzed) == ["a.py", "b.py"]
    assert stats["modules"] == 2 and "walk_censuses" not in stats


def test_incremental_walk_of_an_indexed_project_still_gates(acg, tmp_path, capsys):
    proj = tmp_path / "proj"
    _write(proj, "a.py")
    inst, analyzed = _walk_stub(acg, module_rows=[object()])
    stats = inst.analyze_repository(proj, language="python", incremental=True)
    assert analyzed == []
    assert stats["incremental_unchanged_files"] == 1
    rc = wd.report_empty_walk(stats, incremental=True, since_commit=None, language="python")
    assert rc == 0
    assert "incremental gate saw no change" in capsys.readouterr().err


# ── end to end through main() ────────────────────────────────────────────────


_RUNNER = textwrap.dedent(r"""
    import sys, importlib.util
    spec = importlib.util.spec_from_file_location("acg", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Coll:
        def __init__(self, name):
            self.name = name
            self.data = self
        def replace(self, uuid, **kw): return None
        def update(self, uuid, **kw): return None
        def iterator(self, *a, **kw): return iter([])
        def delete_by_id(self, uuid): return None
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
            def add_property(p): return None
        config = _config()

    def _create(self, force=False, *, repo_path=None):
        for attr, base in (("modules_collection", "CodeModule"), ("classes_collection", "CodeClass"),
                           ("functions_collection", "CodeFunction"), ("apis_collection", "CodeAPI"),
                           ("interactions_collection", "CodeInteraction")):
            setattr(self, attr, _Coll("Fake_" + base))
    mod.CodeGraphAnalyzer.create_collections = _create
    mod.CodeGraphAnalyzer._get_existing_module = lambda self, p, h: None
    def _connect(self):
        self.client = object()
        return True
    mod.CodeGraphAnalyzer.connect = _connect
    mod.CodeGraphAnalyzer.close = lambda self: None
    mod.CodeGraphAnalyzer.create_cross_references = (
        lambda self, changed_files=None: {"calls": 0, "extends": 0, "imports": 0})
    mod.generate_embedding = lambda text: None
    mod.embed_module = lambda summary: None
    mod.embed_function = lambda sig, body, language="python": None
    mod.embed_class = lambda sig, body, methods=None, language="python": None

    class _Svc:
        code_vector_slot = "codesage_embed"; code_model_id = "codesage-large-v2"; code_dim = 2048
        text_vector_slot = "qwen3_embed"; text_model_id = "qwen3-embedding:0.6b"; text_dim = 1024
        def code_backend_ready(self): return True
        def text_backend_ready(self): return True
        def close(self): return None
    mod.EmbeddingService.for_project = classmethod(lambda cls, *a, **kw: _Svc())
    sys.argv = ["analyze_code_graph.py", sys.argv[2], "--project", "FakeProject",
                "--language", "python"]
    sys.exit(mod.main())
""")


def _run_main(tmp_path: Path, repo: Path) -> subprocess.CompletedProcess:
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(runner), str(ANALYZER), str(repo)],
        capture_output=True, text=True, timeout=120,
        env=child_env(VCT_JOERN_AVAILABLE="0", WEAVIATE_URL="http://127.0.0.1:9"),
        cwd=str(tmp_path),
    )


def test_main_exits_3_and_names_the_rule_when_every_file_is_excluded(tmp_path):
    repo = tmp_path / "proj"
    _write(repo, "venv/lib/a.py")
    _write(repo, "node_modules/pkg/b.py")
    proc = _run_main(tmp_path, repo)
    assert proc.returncode == 3, (proc.stdout[-1500:], proc.stderr[-1500:])
    assert "every one of the 2 source file(s)" in proc.stderr
    assert "directory 'node_modules' (1)" in proc.stderr and "directory 'venv' (1)" in proc.stderr


def test_main_indexes_a_project_that_lives_under_a_build_folder(tmp_path):
    repo = tmp_path / "build" / "proj"
    _write(repo, "a.py")
    proc = _run_main(tmp_path, repo)
    assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])
    assert "Files analyzed: 1" in proc.stdout


def test_main_with_nothing_to_index_says_so_and_exits_0(tmp_path):
    repo = tmp_path / "docs"
    _write(repo, "README.md", "# hi\n")
    proc = _run_main(tmp_path, repo)
    assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])
    assert "no source file with a supported extension for --language python" in proc.stderr


# ── the claim refusal names its holder ───────────────────────────────────────


def test_the_claim_refusal_names_the_holder_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    from vco_lib import codegraph_walk as cw

    with cw.walk_claim("Demo"):
        msg = cw.already_running_message("Demo")
    import os

    assert f"held by pid {os.getpid()} since " in msg
    cw.walk_claim_holder_path("Demo").write_text("garbage", encoding="utf-8")
    assert "held by" not in cw.already_running_message("Demo")
