# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-A1/A2 — `vco_lib/inject_intent.py` pure intent core.

RED on the base tree because the module is absent (import error at collection).

Covers (PLAN-V02101 §WP-F row 1 + §3 WP-A):
  * READ / EDIT / SEARCH / MECHANICAL classification table, including the
    injection survey's wrong-trigger corpus (heredoc write, `cargo clippy`,
    `git status`, `pub(crate)` grep pattern, `sed -n` dump, `git show
    rev:path`, `cd` prefix).
  * `edit_enclosing_symbols` (Python + Rust regex table).
  * `file_pub_symbols` (top-level defs; Read-tool line-number prefixes
    stripped; <= 5).
  * `agent_task_section` (`Task:` line of the shipped handoff format;
    preamble skip; 400-char cap; empty prompt -> "").
  * The PORTED gates (`pattern_gate`, `extract_symbol`) against the legacy
    corpora. v0.2.101 Wave 2 retired the shell copies from
    `templates/hooks/_lib/codegraph-query.{sh,ps1}` WITH their last legacy
    callers, so `vco_lib/inject_intent.py` is now the ONLY implementation
    (the interim shell-parity battery was retired with its shell side; the
    extractor corpus lives on in test_p1e_codegraph_extract_symbol.py).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from vco_lib.inject_intent import (  # noqa: E402
    INTENT_EDIT,
    INTENT_MECHANICAL,
    INTENT_READ,
    INTENT_SEARCH,
    agent_task_section,
    classify_bash,
    clean_identifier,
    edit_enclosing_symbols,
    extract_symbol,
    file_pub_symbols,
    pattern_gate,
)


# --- A1: classification table ----------------------------------------------


def _intent(cmd: str, cwd: str = "") -> str:
    return classify_bash(cmd, cwd).intent


class TestClassifyMechanical:
    """MECHANICAL: no query, no injection, no RL retrieval event."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "cargo clippy",  # survey wrong-trigger corpus
            "cargo build --workspace",
            "git status",
            "git commit -m 'x'",
            "ls -la",
            "cd /tmp",
            "npm install",
            "pip install -e .",
            "pytest tests/",
            "python script.py",
            "podman ps",
            "echo hello",
            "mkdir -p build && cmake ..",
            "grep 'pub(crate) fn' src/lib.rs",  # regex-fragment pattern: gate passes
            # the legacy `name(` shape but no CLEAN identifier is recoverable,
            # so the new classifier must NOT treat it as a SEARCH query
            # (survey: it returned an unrelated symbol at 0.526).
            "grep TODO notes.txt",  # bare all-caps word — never an identifier
            "grep hello world.txt",  # bare lowercase word
        ],
    )
    def test_mechanical(self, cmd: str) -> None:
        assert _intent(cmd) == INTENT_MECHANICAL


class TestClassifyRead:
    @pytest.mark.parametrize(
        "cmd",
        [
            "cat src/main.rs",
            "head -40 vco_lib/foo.py",
            "tail -n 20 app.log",
            "sed -n '1,40p' notes.txt",  # survey "sed dump" — READ, weak surface
            "less README.md",
            "diff a.py b.py",
            "git diff",
            "git diff HEAD~1 -- templates/hooks/pre-tool-use.sh",
            "git log --oneline -5",
            "git show HEAD",
        ],
    )
    def test_read(self, cmd: str) -> None:
        assert _intent(cmd) == INTENT_READ

    def test_read_targets_named_paths(self) -> None:
        bi = classify_bash("git diff HEAD~1 -- templates/hooks/pre-tool-use.sh")
        assert any("pre-tool-use.sh" in t for t in bi.targets)

    def test_cat_pipeline_is_read(self) -> None:
        assert _intent("cat foo.py | head -20") == INTENT_READ

    def test_git_show_rev_path_keeps_revision(self) -> None:
        """`git show <rev>:<path>` keeps the rev for the revision-pin check
        (the router stays silent when the code-graph stamp != rev)."""
        bi = classify_bash("git show abc1234:vco_lib/inject_intent.py")
        assert bi.intent == INTENT_READ
        assert ("abc1234", "vco_lib/inject_intent.py") in bi.rev_paths
        assert any("inject_intent.py" in t for t in bi.targets)

    def test_git_show_rev_path_in_pipeline(self) -> None:
        bi = classify_bash("git show HEAD:src/main.rs | head -5")
        assert bi.intent == INTENT_READ
        assert ("HEAD", "src/main.rs") in bi.rev_paths

    def test_cd_prefix_sees_through_to_real_verb(self) -> None:
        """Survey wrong-trigger corpus: `cd <dir> && <real command>` — the
        classifier must reach past the `cd` prefix."""
        bi = classify_bash("cd /repo && cat src/lib.rs")
        assert bi.intent == INTENT_READ
        assert any("lib.rs" in t for t in bi.targets)

    def test_env_assignment_prefix_sees_through(self) -> None:
        bi = classify_bash("GIT_PAGER=cat git diff")
        assert bi.intent == INTENT_READ


class TestClassifyEdit:
    def test_sed_in_place(self, tmp_path: Path) -> None:
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        bi = classify_bash(f"sed -i 's/foo/bar/' {f}", str(tmp_path))
        assert bi.intent == INTENT_EDIT
        assert any(str(f) in t or "app.py" in t for t in bi.targets)

    def test_redirect_write(self, tmp_path: Path) -> None:
        bi = classify_bash("echo hello > out.txt", str(tmp_path))
        assert bi.intent == INTENT_EDIT
        assert any("out.txt" in t for t in bi.targets)

    def test_append_redirect(self, tmp_path: Path) -> None:
        bi = classify_bash("echo hello >> out.txt", str(tmp_path))
        assert bi.intent == INTENT_EDIT

    def test_heredoc_write_is_edit_not_read(self, tmp_path: Path) -> None:
        """Survey wrong-trigger corpus: `cat > f <<EOF` is a WRITE, not the
        READ its `cat` verb suggests."""
        cmd = "cat > notes.md <<'EOF'\nsome body text\nEOF"
        bi = classify_bash(cmd, str(tmp_path))
        assert bi.intent == INTENT_EDIT
        assert any("notes.md" in t for t in bi.targets)

    def test_heredoc_snippet_for_knowledge_target(self, tmp_path: Path) -> None:
        (tmp_path / "knowledge").mkdir()
        cmd = "cat > knowledge/node.md <<'EOF'\nTitle of the node body\nEOF"
        bi = classify_bash(cmd, str(tmp_path))
        assert bi.intent == INTENT_EDIT
        assert "Title of the node body" in bi.write_snippet

    def test_heredoc_snippet_withheld_for_code_target(self, tmp_path: Path) -> None:
        cmd = "cat > app.py <<'EOF'\nprint('x')\nEOF"
        bi = classify_bash(cmd, str(tmp_path))
        assert bi.intent == INTENT_EDIT
        assert bi.write_snippet == ""

    def test_tee_write(self, tmp_path: Path) -> None:
        bi = classify_bash("echo x | tee out.txt", str(tmp_path))
        assert bi.intent == INTENT_EDIT

    def test_edit_outranks_read_in_chain(self, tmp_path: Path) -> None:
        bi = classify_bash("cat app.py && sed -i 's/a/b/' app.py", str(tmp_path))
        assert bi.intent == INTENT_EDIT

    def test_dev_null_sink_is_not_edit(self) -> None:
        """>/dev/null and 2>&1 are stripped before write-shape detection —
        `cargo test 2>&1 | tail -5` must stay READ/MECHANICAL, never EDIT."""
        assert _intent("cargo test 2>&1 | tail -5") != INTENT_EDIT
        assert _intent("grep foo bar.py > /dev/null") != INTENT_EDIT


class TestClassifySearch:
    def test_grep_identifier(self) -> None:
        bi = classify_bash("grep -rn 'vco_query_cache_put' templates/")
        assert bi.intent == INTENT_SEARCH
        assert "vco_query_cache_put" in bi.symbols

    def test_rg_camelcase(self) -> None:
        bi = classify_bash("rg 'VcoQueryCache' src")
        assert bi.intent == INTENT_SEARCH
        assert "VcoQueryCache" in bi.symbols

    def test_git_grep(self) -> None:
        bi = classify_bash("git grep vco_seen_add")
        assert bi.intent == INTENT_SEARCH
        assert "vco_seen_add" in bi.symbols

    def test_grep_dash_e_pattern(self) -> None:
        bi = classify_bash("grep -rn -e extract_write_targets vco_lib/")
        assert bi.intent == INTENT_SEARCH
        assert "extract_write_targets" in bi.symbols

    def test_grep_call_shape(self) -> None:
        bi = classify_bash("grep 'classify_bash(' -r vco_lib/")
        assert bi.intent == INTENT_SEARCH
        assert "classify_bash" in bi.symbols

    def test_classifier_never_uses_command_text_as_query(self) -> None:
        """The classifier NEVER produces a whole-command query (WP-A1)."""
        bi = classify_bash("grep -rn 'foo_bar' templates/hooks | head -5")
        for sym in bi.symbols:
            assert len(sym) < 200
            assert "grep" not in sym


# --- GLM review round 2 (V02101-INJECTION-CORE-REVIEW-2026-10-05) ------------


class TestRelativeRevPins:
    """SF-2: `HEAD~N` / `HEAD^` are the commonest relative revs — the pin
    must survive them (before the fix the token polluted `targets` and
    rev_paths stayed empty)."""

    def test_tilde_rev(self) -> None:
        bi = classify_bash("git show HEAD~2:src/main.rs")
        assert bi.intent == INTENT_READ
        assert ("HEAD~2", "src/main.rs") in bi.rev_paths

    def test_caret_rev(self) -> None:
        bi = classify_bash("git show HEAD^:vco_lib/foo.py")
        assert ("HEAD^", "vco_lib/foo.py") in bi.rev_paths

    def test_compound_relative_rev(self) -> None:
        bi = classify_bash("git show v1.2^2:a/b.rs | head -5")
        assert ("v1.2^2", "a/b.rs") in bi.rev_paths

    def test_no_polluted_target(self) -> None:
        bi = classify_bash("git show HEAD~2:src/main.rs")
        assert all("HEAD~2:" not in t for t in bi.targets)
        assert any(t == "src/main.rs" for t in bi.targets)


class TestKeywordPatterns:
    """SF-3: `grep 'def authenticate'` is THE canonical exact-lookup shape —
    the identifier sits right behind the keyword."""

    @pytest.mark.parametrize(
        ("cmd", "want"),
        [
            ("grep -rn 'def authenticate' src/", "authenticate"),
            ("grep 'class Foo' -r .", "Foo"),
            ("rg 'fn extract_write' src", "extract_write"),
            ("grep -n 'function doThing' app.js", "doThing"),
            ("git grep 'func main'", "main"),
        ],
    )
    def test_keyword_then_identifier(self, cmd: str, want: str) -> None:
        bi = classify_bash(cmd)
        assert bi.intent == INTENT_SEARCH
        assert want in bi.symbols

    def test_keyword_with_no_identifier_stays_mechanical(self) -> None:
        assert _intent("grep -rn 'def ' src/") == INTENT_MECHANICAL


class TestXargsTransparency:
    """SF-4: a search/read behind `xargs` or `command` must not go silent
    when Wave 2 retires the legacy whole-command regex gate."""

    def test_find_pipe_xargs_grep(self) -> None:
        bi = classify_bash("find . -name '*.py' | xargs grep -l classify_bash")
        assert bi.intent == INTENT_SEARCH
        assert "classify_bash" in bi.symbols

    def test_xargs_with_own_flags(self) -> None:
        bi = classify_bash("echo x | xargs -n 1 grep vco_seen_add")
        assert bi.intent == INTENT_SEARCH
        assert "vco_seen_add" in bi.symbols

    def test_xargs_sticky_flag(self) -> None:
        bi = classify_bash("find . -name '*.rs' | xargs -I{} grep -H foo_bar {}")
        assert bi.intent == INTENT_SEARCH
        assert "foo_bar" in bi.symbols

    def test_command_wrapper_read(self) -> None:
        bi = classify_bash("command cat foo.py")
        assert bi.intent == INTENT_READ

    def test_command_dash_v_is_not_search(self) -> None:
        assert _intent("command -v grep") != INTENT_SEARCH


class TestKeywordTightening:
    """Round-3 nit-2: the word-wise keyword retry must not rescue junk —
    a single-char or all-underscore "identifier" is never a lookup key."""

    def test_single_underscore_not_rescued(self) -> None:
        assert _intent("grep -rn 'def _' src/") == INTENT_MECHANICAL

    def test_single_char_not_rescued(self) -> None:
        assert _intent("grep 'class A' notes") == INTENT_MECHANICAL

    def test_real_two_char_identifier_still_rescued(self) -> None:
        bi = classify_bash("grep -rn 'def ab' src/")
        assert bi.intent == INTENT_SEARCH
        assert "ab" in bi.symbols


class TestDiffRevPins:
    """Round-3 nit-3: `git diff <rev>:<path>` is a pinned read too — the
    rev-path token must become a pin, never a polluted target."""

    def test_git_diff_revpath_pins(self) -> None:
        bi = classify_bash("git diff HEAD~2:src/main.rs")
        assert ("HEAD~2", "src/main.rs") in bi.rev_paths
        assert all("HEAD~2:" not in t for t in bi.targets)

    def test_git_show_still_pins(self) -> None:
        bi = classify_bash("git show v2:f.py")
        assert ("v2", "f.py") in bi.rev_paths

    def test_plain_git_log_no_pin(self) -> None:
        bi = classify_bash("git log --oneline a.b.c")
        assert bi.rev_paths == ()
        assert all(":" not in t for t in bi.targets)


class TestIntentShapeNarrowing:
    """Nits 6+7: the INTENT edit-shape is narrower than the SYNC write-shape
    (compiler --output flags and targetless heredocs are not edits)."""

    @pytest.mark.parametrize(
        "cmd",
        ["cargo build --output x", "gcc --output-dir build", "make --output foo"],
    )
    def test_compiler_output_flag_not_edit(self, cmd: str) -> None:
        assert _intent(cmd) != INTENT_EDIT

    def test_heredoc_pipe_only_not_edit(self) -> None:
        assert _intent("cat <<EOF\nhello\nEOF") != INTENT_EDIT

    def test_tee_heredoc_still_edit(self, tmp_path: Path) -> None:
        bi = classify_bash("tee notes.md <<EOF\nbody\nEOF", str(tmp_path))
        assert bi.intent == INTENT_EDIT

    def test_redirect_still_edit(self, tmp_path: Path) -> None:
        assert classify_bash("echo x > out.txt", str(tmp_path)).intent == INTENT_EDIT


# --- A1: ports of the shell gates (one home; parity-pinned below) -----------


class TestPatternGatePort:
    @pytest.mark.parametrize(
        "p",
        ["vco_query_cache_put", "VcoQueryCache", "classify_bash(", "def authenticate",
         "class Foo", "fn extract", "self_update.force_resync_launcher"],
    )
    def test_positives(self, p: str) -> None:
        assert pattern_gate(p) is True

    @pytest.mark.parametrize("p", ["TODO", "hello", "foo.bar baz", ""])
    def test_negatives(self, p: str) -> None:
        # "foo.bar baz" as a WHOLE pattern: the legacy gate tests the full
        # pattern text; a bare dotted token must not fire.
        assert pattern_gate(p) is False


class TestExtractSymbolPort:
    def test_extracts_first_symbol(self) -> None:
        assert extract_symbol("grep -rn vco_query_cache_put templates/") == "vco_query_cache_put"

    def test_skips_env_assignment(self) -> None:
        assert extract_symbol("LEAN_CTX_OFF=1 run_thing") == "run_thing"

    def test_no_whole_text_fallback(self) -> None:
        assert extract_symbol("git diff abc123..HEAD") == ""

    def test_skips_noncode_paths(self) -> None:
        assert extract_symbol("tail -f /var/log/app.log") == ""

    def test_accepts_source_paths(self) -> None:
        assert extract_symbol("cat src/main.rs") == "src/main.rs"

    def test_cap_200(self) -> None:
        long = "a_" + "b" * 300
        assert len(extract_symbol(long)) <= 200


class TestCleanIdentifier:
    @pytest.mark.parametrize(
        ("tok", "want"),
        [
            ("foo_bar", "foo_bar"),
            ("FooBar", "FooBar"),
            ("classify_bash(", "classify_bash"),
            ("self_update.force_resync_launcher", "self_update.force_resync_launcher"),
            ("Foo::bar", "Foo::bar"),
            ("pub(crate)", ""),
            ("a|b", ""),
            ("foo bar", ""),
            ("", ""),
        ],
    )
    def test_clean(self, tok: str, want: str) -> None:
        assert clean_identifier(tok) == want


# --- Shell-gate retirement (v0.2.101 Wave 2) --------------------------------
# The shell/Python parity battery that lived here was retired WITH its shell
# side: Wave 2 deleted codegraph_bash_gate / codegraph_pattern_gate /
# codegraph_extract_symbol from _lib/codegraph-query.{sh,ps1} together with
# their last legacy callers (the pre-bash/pre-edit rewires + the pre-tool-use
# branch removal). vco_lib/inject_intent.py is now the ONLY implementation;
# the extractor corpus lives on in tests/test_p1e_codegraph_extract_symbol.py
# (retargeted to the Python one-home) and the gate corpora above.


# --- A2: symbol extraction ---------------------------------------------------


_PY_SRC = '''\
import os


class Outer:
    def method_a(self):
        return 1

    def method_b(self):
        x = 2
        return x


def top_level():
    def inner_helper():
        return "inner"
    return inner_helper()
'''

_RS_SRC = '''\
pub struct Widget {
    pub name: String,
}

impl Widget {
    pub fn new(name: String) -> Self {
        Self { name }
    }

    fn rename(&mut self, n: String) {
        self.name = n;
    }
}

fn free_function() -> u32 {
    42
}
'''


class TestEditEnclosingSymbols:
    def test_python_innermost_first(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(_PY_SRC)
        names = edit_enclosing_symbols(str(f), 'return "inner"')
        assert names[0] == "inner_helper"
        assert "top_level" in names

    def test_python_method_and_class(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(_PY_SRC)
        names = edit_enclosing_symbols(str(f), "x = 2")
        assert names[0] == "method_b"
        assert "Outer" in names

    def test_rust_impl_method(self, tmp_path: Path) -> None:
        f = tmp_path / "w.rs"
        f.write_text(_RS_SRC)
        names = edit_enclosing_symbols(str(f), "self.name = n;")
        assert "rename" in names
        assert any(n in ("Widget",) for n in names)

    def test_rust_free_fn(self, tmp_path: Path) -> None:
        f = tmp_path / "w.rs"
        f.write_text(_RS_SRC)
        names = edit_enclosing_symbols(str(f), "42")
        assert "free_function" in names

    def test_match_not_found(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(_PY_SRC)
        assert edit_enclosing_symbols(str(f), "no such text anywhere") == []

    def test_missing_file(self, tmp_path: Path) -> None:
        assert edit_enclosing_symbols(str(tmp_path / "absent.py"), "x") == []

    def test_bounded_to_three(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(_PY_SRC)
        assert len(edit_enclosing_symbols(str(f), "x = 2")) <= 3


class TestFilePubSymbols:
    def test_python_top_level(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(_PY_SRC)
        syms = file_pub_symbols(str(f))
        assert "Outer" in syms
        assert "top_level" in syms
        # nested defs are NOT top-level
        assert "inner_helper" not in syms
        # methods are not pub symbols of the module surface
        assert "method_a" not in syms

    def test_rust_top_level(self, tmp_path: Path) -> None:
        f = tmp_path / "w.rs"
        f.write_text(_RS_SRC)
        syms = file_pub_symbols(str(f))
        assert "Widget" in syms
        assert "free_function" in syms

    def test_content_preferred_over_disk(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def stale():\n    pass\n")
        syms = file_pub_symbols(str(f), content="def fresh():\n    pass\n")
        assert syms == ["fresh"]

    def test_read_tool_line_prefixes_stripped(self, tmp_path: Path) -> None:
        """PostToolUse Read delivers cat -n shaped content — the prefixes
        must not defeat the top-level (zero-indent) test."""
        f = tmp_path / "mod.py"
        numbered = "     1\tdef alpha():\n     2\t    pass\n     3\tclass Beta:\n     4\t    pass\n"
        syms = file_pub_symbols(str(f), content=numbered)
        assert "alpha" in syms
        assert "Beta" in syms

    def test_cap_five(self, tmp_path: Path) -> None:
        f = tmp_path / "many.py"
        f.write_text("".join(f"def f{i}():\n    pass\n" for i in range(12)))
        assert len(file_pub_symbols(str(f))) <= 5

    def test_missing_file_empty(self, tmp_path: Path) -> None:
        assert file_pub_symbols(str(tmp_path / "absent.py")) == []


# --- A2/C4: agent brief TASK section -----------------------------------------


class TestAgentTaskSection:
    HANDOFF = (
        "@implementer (Model)\n"
        "Task: Implement the widget parser per PLAN-X §3\n"
        "Context: /repo/src/widget.rs, keep it pure\n"
        "Success Criteria: tests green\n"
    )

    def test_task_line_extracted(self) -> None:
        out = agent_task_section(self.HANDOFF)
        assert "Implement the widget parser" in out
        assert "Context:" not in out

    def test_first_action_preamble_skipped(self) -> None:
        prompt = (
            "Operate at medium effort.\n"
            "FIRST ACTION: read the plan file.\n"
            "Implement the cache fix and run the tests.\n"
        )
        out = agent_task_section(prompt)
        assert "Implement the cache fix" in out
        assert "medium effort" not in out

    def test_plain_prompt_first_sentence(self) -> None:
        out = agent_task_section("Fix the parser bug. Then run tests.")
        assert out == "Fix the parser bug."

    def test_cap_400(self) -> None:
        out = agent_task_section("Task: " + "x " * 500)
        assert len(out) <= 400

    @pytest.mark.parametrize("p", ["", "   ", "\n\n"])
    def test_empty_prompt(self, p: str) -> None:
        assert agent_task_section(p) == ""


# --- CLI entry (used by _lib/inject-budget delegation + Wave 2 wrappers) -----


class TestModuleCli:
    def test_classify_cli_json(self) -> None:
        from tests.common.child_env import child_env

        r = subprocess.run(
            [sys.executable, "-m", "vco_lib.inject_intent", "classify"],
            input="grep -rn vco_seen_add templates/",
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
            env=child_env(),
        )
        assert r.returncode == 0, r.stderr
        import json
        d = json.loads(r.stdout)
        assert d["intent"] == "SEARCH"
        assert "vco_seen_add" in d["symbols"]

    def test_task_type_cli(self) -> None:
        from tests.common.child_env import child_env

        r = subprocess.run(
            [sys.executable, "-m", "vco_lib.inject_intent", "task-type", "read_code"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
            env=child_env(),
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "pre_read_kg_search"
