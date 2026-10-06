# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-B2 — `query_code_graph.py structure ... --hook-format`.

RED on the base tree: the flags do not exist and the formatter helpers are
absent.

Covers (PLAN-V02101 §WP-F row 7 + §3 WP-B2):
  * `CODE: <full_name> | <kind> | def <file>:<line> | callers: [...]` row
    shape — symbol + file:line ONLY, never bodies (orchestrator answer 5);
  * <= 5 rows, callers <= 5 each (with an honest truncation marker);
  * `--indexed-revision`: every hook-format block carries the code graph's
    revision stamp line (`CODE-REV:`) so the router can stay SILENT when the
    ref being read (`git show <rev>`) does not match (wave-4 caveat (a));
  * same-language identity: an exact-symbol match must be same-language as
    the source file, cross-language ONLY on an exact full-name equality
    (kills the WP-03b wrong-language hit class);
  * leaf-name target fallback (Edit surfaces extract `bar`, not
    `mod::Foo::bar`);
  * parser accepts the new flags on the structure subcommand AND main()
    threads them into query_structure (behavioural wiring, not a source
    scan);
  * the extension→language map is ONE home (vco_lib.inject_intent), equal
    to the analyzer's dispatch table.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"


@pytest.fixture(scope="module")
def qcg() -> types.ModuleType:
    for p in (str(SCRIPTS_DIR), str(REPO_ROOT / "claude_mcp_servers"), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("query_code_graph")


def _ent(full_name: str, lang: str = "python", file: str = "mod.py",
         line: int = 10, callers: list | None = None) -> dict:
    return {
        "full_name": full_name, "kind": "function", "language": lang,
        "file": file, "line": line, "callers": callers or [],
    }


def _caller(full_name: str, file: str = "other.py", line: int = 3) -> dict:
    return {"full_name": full_name, "file": file, "line": line}


class TestFormatterShape:
    def test_row_shape(self, qcg: types.ModuleType) -> None:
        out = qcg.format_structure_hook_block(
            "callers", "mod.fn",
            [_ent("mod.fn", callers=[_caller("app.run"), _caller("cli.main", "c.py", 7)])],
        )
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert lines == [
            "CODE: mod.fn | function | def mod.py:10 | "
            "callers: app.run@other.py:3, cli.main@c.py:7 | src=mod.py"
        ]

    def test_no_bodies_ever(self, qcg: types.ModuleType) -> None:
        ent = _ent("mod.fn", callers=[_caller("app.run")])
        ent["body"] = "SECRET BODY TEXT"
        out = qcg.format_structure_hook_block("callers", "mod.fn", [ent])
        assert "SECRET BODY TEXT" not in out

    def test_max_five_rows(self, qcg: types.ModuleType) -> None:
        ents = [_ent(f"mod.fn{i}") for i in range(9)]
        out = qcg.format_structure_hook_block("callers", "mod.fn", ents)
        rows = [ln for ln in out.splitlines() if ln.startswith("CODE:")]
        assert len(rows) == 5

    def test_max_five_callers_with_marker(self, qcg: types.ModuleType) -> None:
        callers = [_caller(f"c{i}.fn", f"f{i}.py", i) for i in range(8)]
        out = qcg.format_structure_hook_block(
            "callers", "mod.fn", [_ent("mod.fn", callers=callers)])
        row = [ln for ln in out.splitlines() if ln.startswith("CODE:")][0]
        listed = row.split("callers: ", 1)[1].split(" | ")[0]
        assert len(listed.split(", ")) == 5
        assert "+3" in row  # honest truncation signal

    def test_revision_stamp_line(self, qcg: types.ModuleType) -> None:
        out = qcg.format_structure_hook_block(
            "callers", "mod.fn", [_ent("mod.fn")], revision_stamp="abc123")
        assert out.splitlines()[0] == "CODE-REV: abc123"

    def test_no_stamp_without_revision(self, qcg: types.ModuleType) -> None:
        out = qcg.format_structure_hook_block("callers", "mod.fn", [_ent("mod.fn")])
        assert "CODE-REV" not in out

    def test_empty_entities_emit_no_rows(self, qcg: types.ModuleType) -> None:
        out = qcg.format_structure_hook_block("callers", "mod.fn", [])
        assert not [ln for ln in out.splitlines() if ln.startswith("CODE:")]


class TestLanguageIdentity:
    def test_same_language_passes(self, qcg: types.ModuleType) -> None:
        kept = qcg.filter_same_language(
            [_ent("mod.fn", lang="python")], "mod.fn", "src/mod.py")
        assert len(kept) == 1

    def test_wrong_language_dropped(self, qcg: types.ModuleType) -> None:
        """WP-03b class: a Rust symbol hit for a Python-file query is noise
        (the name does NOT exactly equal the target, so the cross-language
        exception does not apply)."""
        kept = qcg.filter_same_language(
            [_ent("other_mod.fn", lang="rust", file="mod.rs")], "mod.fn", "src/mod.py")
        assert kept == []

    def test_cross_language_allowed_on_exact_full_name(self, qcg: types.ModuleType) -> None:
        kept = qcg.filter_same_language(
            [_ent("vco_lib.foo.Bar", lang="rust")], "vco_lib.foo.Bar", "src/mod.py")
        assert len(kept) == 1

    def test_leaf_query_matches_only_same_language(self, qcg: types.ModuleType) -> None:
        kept = qcg.filter_same_language(
            [_ent("a.fn", lang="python"), _ent("b.fn", lang="go")], "fn", "x.py")
        assert [e["full_name"] for e in kept] == ["a.fn"]

    def test_no_source_file_disables_filter(self, qcg: types.ModuleType) -> None:
        ents = [_ent("a.fn", lang="python"), _ent("b.fn", lang="go")]
        assert qcg.filter_same_language(ents, "fn", None) == ents
        assert qcg.filter_same_language(ents, "fn", "README.md") == ents


class TestSameSourceFile:
    def test_relative_vs_absolute(self, qcg: types.ModuleType) -> None:
        assert qcg._same_source_file("src/mod.py", "/repo/src/mod.py") is True
        assert qcg._same_source_file("/repo/src/mod.py", "src/mod.py") is True

    def test_different_files(self, qcg: types.ModuleType) -> None:
        assert qcg._same_source_file("src/mod.py", "src/other.py") is False
        assert qcg._same_source_file("mod.py", "notmod.py") is False

    def test_empty(self, qcg: types.ModuleType) -> None:
        assert qcg._same_source_file("", "src/mod.py") is False
        assert qcg._same_source_file("src/mod.py", "") is False


class TestIndexedRevision:
    def test_git_repo_head(self, tmp_path: Path, qcg: types.ModuleType) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path, check=True,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              capture_output=True, text=True, check=True).stdout.strip()
        assert qcg.compute_indexed_revision(str(tmp_path)) == head

    def test_non_git_unknown(self, tmp_path: Path, qcg: types.ModuleType) -> None:
        assert qcg.compute_indexed_revision(str(tmp_path)) == "unknown"

    def test_stamp_memoized_per_root(self, qcg: types.ModuleType,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        """nit-2: the Read surface pays the stamp on every CG leg (≤5
        structure calls per run) — one `git rev-parse` per process is enough;
        the rest is served from the per-root memo."""
        calls: list = []
        real_run = qcg.subprocess.run

        def _counting(*a, **k):
            calls.append(a)
            return real_run(*a, **k)

        monkeypatch.setattr(qcg.subprocess, "run", _counting)
        qcg.compute_indexed_revision("/nonexistent-memo-root-xyz")
        qcg.compute_indexed_revision("/nonexistent-memo-root-xyz")
        assert len(calls) <= 1, (
            f"compute_indexed_revision re-shelled git {len(calls)}× for the "
            "same root within one process")


class TestParserAndWiring:
    def test_parser_accepts_new_flags(self, qcg: types.ModuleType) -> None:
        parser = qcg.build_parser()
        args = parser.parse_args(
            ["structure", "callers", "mod.fn", "--hook-format",
             "--indexed-revision", "--source-file", "src/mod.py",
             "--exclude-file", "src/mod.py"])
        assert args.command == "structure"
        assert args.hook_format is True
        assert args.indexed_revision is True
        assert args.source_file == "src/mod.py"
        assert args.exclude_file == "src/mod.py"

    def test_parser_defaults_off(self, qcg: types.ModuleType) -> None:
        parser = qcg.build_parser()
        args = parser.parse_args(["structure", "callers", "mod.fn"])
        assert args.hook_format is False
        assert args.indexed_revision is False
        assert args.source_file is None
        assert args.exclude_file is None

    def test_main_threads_flags_into_query_structure(
            self, qcg: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Behavioural wiring pin: main() must pass the three new values
        through to query_structure (mutate the call and this goes red)."""
        recorded: dict = {}

        class FakeQuerier:
            def __init__(self, project=None):
                pass

            def connect(self):
                return True

            def query_structure(self, query_type, target, **kw):
                recorded["query_type"] = query_type
                recorded["target"] = target
                recorded.update(kw)

            def close(self):
                pass

        monkeypatch.setattr(qcg, "CodeGraphQuery", FakeQuerier)
        rc = qcg.main([
            "structure", "callers", "mod.fn", "--hook-format",
            "--indexed-revision", "--source-file", "src/mod.py",
            "--exclude-file", "src/mod.py", "--project", "P"])
        assert rc == 0
        assert recorded["query_type"] == "callers"
        assert recorded["target"] == "mod.fn"
        assert recorded["hook_format"] is True
        assert recorded["indexed_revision"] is True
        assert recorded["source_file"] == "src/mod.py"
        assert recorded["exclude_file"] == "src/mod.py"

    def test_main_signature_accepts_argv(self, qcg: types.ModuleType) -> None:
        import inspect
        sig = inspect.signature(qcg.main)
        assert list(sig.parameters) == ["argv"] or "argv" in sig.parameters


class TestExtLangOneHome:
    def test_inject_intent_map_matches_analyzer(self) -> None:
        """ONE home: inject_intent.language_for_path's table must equal the
        analyzer's dispatch table (which decides the `language` property the
        identity check compares against)."""
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.inject_intent import EXT_TO_LANG

        spec = importlib.util.spec_from_file_location(
            "_acg_lang_parity", str(SCRIPTS_DIR / "analyze_code_graph.py"))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pytest.skip("weaviate-client unavailable — analyzer cannot load")
        for ext, lang in mod._EXT_TO_DISPATCH_NAME.items():
            assert EXT_TO_LANG.get(ext) == lang, f"ext {ext} diverges"

    def test_language_for_path(self) -> None:
        from vco_lib.inject_intent import language_for_path
        assert language_for_path("src/mod.py") == "python"
        assert language_for_path("a/b/w.rs") == "rust"
        assert language_for_path("x.PY") == "python"
        assert language_for_path("README.md") == ""
        assert language_for_path("noext") == ""
