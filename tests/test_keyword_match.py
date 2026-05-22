# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for templates/scripts/agent-skill-keyword-match.py.

The matcher is imported by file path (it's a script, not a module shipped
on sys.path); we use importlib.util.spec_from_file_location to load it
into a unique module name so each test sees a clean instance.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCHER_PATH = REPO_ROOT / "templates" / "scripts" / "agent-skill-keyword-match.py"


def _load_matcher():
    spec = importlib.util.spec_from_file_location("kw_match_under_test", MATCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def matcher():
    assert MATCHER_PATH.is_file(), f"matcher not found at {MATCHER_PATH}"
    return _load_matcher()


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def test_parse_keywords_inline_list(matcher):
    fm = textwrap.dedent(
        """\
        name: foo
        keywords: [a, "b c", d]
        """
    )
    assert matcher.parse_keywords(fm) == ["a", "b c", "d"]


def test_parse_keywords_block_list(matcher):
    fm = textwrap.dedent(
        """\
        name: foo
        keywords:
          - a
          - "b c"
          - d
        """
    )
    assert matcher.parse_keywords(fm) == ["a", "b c", "d"]


def test_parse_keywords_block_list_single_quotes(matcher):
    fm = textwrap.dedent(
        """\
        keywords:
          - 'screen reader'
          - WCAG
        """
    )
    assert matcher.parse_keywords(fm) == ["screen reader", "WCAG"]


def test_parse_keywords_inline_no_brackets(matcher):
    # Some YAML writers omit brackets; still a valid flow-style list per spec.
    fm = "keywords: a, b, c\n"
    assert matcher.parse_keywords(fm) == ["a", "b", "c"]


def test_parse_keywords_missing_returns_empty(matcher):
    fm = "name: foo\ndescription: bar\n"
    assert matcher.parse_keywords(fm) == []


def test_parse_keywords_empty_list(matcher):
    fm = "keywords: []\n"
    assert matcher.parse_keywords(fm) == []


def test_parse_keywords_block_form_followed_by_other_key(matcher):
    fm = textwrap.dedent(
        """\
        keywords:
          - alpha
          - beta
        description: should not be a keyword
        """
    )
    assert matcher.parse_keywords(fm) == ["alpha", "beta"]


def test_parse_keywords_ignored_when_nested_under_other_key(matcher):
    # `keywords:` nested under another key (indented) MUST NOT be treated
    # as the top-level keyword list. We only honor column-0 keys.
    fm = textwrap.dedent(
        """\
        metadata:
          keywords: [a, b]
        """
    )
    assert matcher.parse_keywords(fm) == []


def test_parse_name(matcher):
    assert matcher.parse_name("name: foo\n", "fallback") == "foo"
    assert matcher.parse_name('name: "quoted"\n', "fallback") == "quoted"
    assert matcher.parse_name("description: bar\n", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Matching: case sensitivity + whole-word boundary
# ---------------------------------------------------------------------------


def test_match_case_sensitive(matcher):
    assert matcher.matches_prompt("UI", "the UI is broken") is True
    assert matcher.matches_prompt("UI", "the ui is broken") is False
    assert matcher.matches_prompt("UI", "the Ui is broken") is False


def test_match_whole_word_negative(matcher):
    # All of these have `UI` as a substring but bordered by word chars.
    assert matcher.matches_prompt("UI", "GUIDE") is False
    assert matcher.matches_prompt("UI", "UIComponent") is False
    assert matcher.matches_prompt("UI", "myUI") is False
    assert matcher.matches_prompt("UI", "UIs") is False
    assert matcher.matches_prompt("UI", "_UI_") is False
    assert matcher.matches_prompt("UI", "0UI") is False


def test_match_whole_word_positive(matcher):
    # Bordered by non-word characters (or string start/end).
    assert matcher.matches_prompt("UI", "UI") is True
    assert matcher.matches_prompt("UI", " UI ") is True
    assert matcher.matches_prompt("UI", "the UI.") is True
    assert matcher.matches_prompt("UI", "(UI)") is True
    assert matcher.matches_prompt("UI", "UI:") is True
    assert matcher.matches_prompt("UI", "fix-UI-bug") is True  # hyphens are non-word


def test_match_multi_word_keyword(matcher):
    assert matcher.matches_prompt("screen reader", "the screen reader output") is True
    # Hyphenated form ≠ space-separated form.
    assert matcher.matches_prompt("screen reader", "the screen-reader output") is False
    # Multi-word still requires non-word boundary at each end.
    assert matcher.matches_prompt("screen reader", "Xscreen reader") is False
    assert matcher.matches_prompt("screen reader", "screen readerX") is False


def test_match_keyword_at_string_start_and_end(matcher):
    assert matcher.matches_prompt("UI", "UI is broken") is True
    assert matcher.matches_prompt("UI", "broken is the UI") is True


def test_match_keyword_with_digits(matcher):
    # `K8s` is alphanumeric — both ends must not be \w.
    assert matcher.matches_prompt("K8s", "is K8s up") is True
    assert matcher.matches_prompt("K8s", "K8sCluster") is False


def test_match_empty_keyword_returns_false(matcher):
    assert matcher.matches_prompt("", "anything") is False


def test_any_match(matcher):
    assert matcher.any_match(["alpha", "beta"], "I want beta features") is True
    assert matcher.any_match(["alpha", "beta"], "neither here nor there") is False


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def test_format_single_agent(matcher):
    out = matcher.format_suggestion(["foo"], [])
    assert out == "You might want to use this agent: foo"


def test_format_two_agents(matcher):
    out = matcher.format_suggestion(["foo", "bar"], [])
    assert out == "You might want to use these agents: foo, bar"


def test_format_three_agents(matcher):
    out = matcher.format_suggestion(["foo", "bar", "baz"], [])
    assert out == "You might want to use these agents: foo, bar, baz"


def test_format_single_skill(matcher):
    out = matcher.format_suggestion([], ["foo"])
    assert out == "You might want to use this skill: foo"


def test_format_two_skills(matcher):
    out = matcher.format_suggestion([], ["foo", "bar"])
    assert out == "You might want to use these skills: foo, bar"


def test_format_combined_one_each(matcher):
    out = matcher.format_suggestion(["agentA"], ["skillB"])
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == "You might want to use this agent: agentA"
    assert lines[1] == "You might want to use this skill: skillB"


def test_format_combined_plural(matcher):
    out = matcher.format_suggestion(["a1", "a2"], ["s1", "s2"])
    lines = out.splitlines()
    assert lines[0] == "You might want to use these agents: a1, a2"
    assert lines[1] == "You might want to use these skills: s1, s2"


def test_format_empty(matcher):
    assert matcher.format_suggestion([], []) == ""


# ---------------------------------------------------------------------------
# Filesystem walk + end-to-end CLI
# ---------------------------------------------------------------------------


def _write_agent(root: Path, slug: str, name: str, keywords_block: str) -> None:
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\n{keywords_block}---\n# {name}\n"
    (agents_dir / f"{slug}.md").write_text(body, encoding="utf-8")


def _write_skill(root: Path, slug: str, name: str, keywords_block: str) -> None:
    skill_dir = root / ".claude" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\n{keywords_block}---\n# {name}\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_collect_agents_and_skills(tmp_path, matcher):
    _write_agent(tmp_path, "ka", "kubernetes-agent", "keywords: [Kubernetes, Helm]\n")
    _write_agent(tmp_path, "wcag", "accessibility-checker", "keywords: [WCAG, A11y]\n")
    _write_skill(tmp_path, "tdd", "tdd-skill", "keywords: [TDD, red-green]\n")

    agents = matcher.collect_agents(tmp_path)
    skills = matcher.collect_skills(tmp_path)

    agent_names = {n for n, _ in agents}
    skill_names = {n for n, _ in skills}
    assert agent_names == {"kubernetes-agent", "accessibility-checker"}
    assert skill_names == {"tdd-skill"}


def test_missing_agents_dir_is_silent(tmp_path, matcher):
    # No `.claude/agents` exists at all — should return [] not crash.
    assert matcher.collect_agents(tmp_path) == []
    assert matcher.collect_skills(tmp_path) == []


def test_malformed_frontmatter_silently_skipped(tmp_path, matcher):
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    # No closing `---` — malformed.
    (agents_dir / "broken.md").write_text("---\nname: broken\nkeywords: [foo]\n", encoding="utf-8")
    # Good agent alongside.
    _write_agent(tmp_path, "ok", "ok-agent", "keywords: [Bar]\n")

    agents = matcher.collect_agents(tmp_path)
    assert len(agents) == 1
    assert agents[0][0] == "ok-agent"


def test_agent_without_keywords_is_excluded(tmp_path, matcher):
    _write_agent(tmp_path, "no-kw", "no-kw-agent", "")
    _write_agent(tmp_path, "yes-kw", "yes-kw-agent", "keywords: [Bar]\n")
    agents = matcher.collect_agents(tmp_path)
    names = {n for n, _ in agents}
    assert names == {"yes-kw-agent"}


def _run_matcher_subprocess(prompt: str, project_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    return subprocess.run(
        [sys.executable, str(MATCHER_PATH)],
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_cli_no_matches_is_silent(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    result = _run_matcher_subprocess("write me a poem about pasta", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_one_agent_match(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes, Helm]\n")
    result = _run_matcher_subprocess("review my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "You might want to use this agent: kubernetes-agent"


def test_cli_two_agents_match(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    _write_agent(tmp_path, "h", "h-agent", "keywords: [Helm]\n")
    result = _run_matcher_subprocess("review my Kubernetes and Helm setup", tmp_path)
    assert result.returncode == 0
    line = result.stdout.strip()
    # Order follows directory iteration (sorted) — both agents must appear.
    assert line.startswith("You might want to use these agents: ")
    assert "k-agent" in line and "h-agent" in line


def test_cli_combined_agent_and_skill(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    _write_skill(tmp_path, "tdd", "tdd-skill", "keywords: [TDD]\n")
    result = _run_matcher_subprocess("apply TDD to my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2
    assert "k-agent" in lines[0] and "agent" in lines[0]
    assert "tdd-skill" in lines[1] and "skill" in lines[1]


def test_cli_empty_prompt_is_silent(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    result = _run_matcher_subprocess("", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_no_claude_dir_at_all(tmp_path):
    # Project root exists but has no .claude/ at all → should exit 0 silently.
    result = _run_matcher_subprocess("review my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_block_form_frontmatter_end_to_end(tmp_path):
    _write_agent(
        tmp_path,
        "a11y",
        "accessibility-checker",
        "keywords:\n  - WCAG\n  - \"screen reader\"\n  - ARIA\n",
    )
    # Multi-word keyword match.
    result = _run_matcher_subprocess("the screen reader output is wrong", tmp_path)
    assert result.returncode == 0
    assert "accessibility-checker" in result.stdout


def test_cli_case_sensitive_no_match(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    # lowercase "kubernetes" must NOT match the keyword "Kubernetes".
    result = _run_matcher_subprocess("review my kubernetes manifest", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_word_boundary_no_match(tmp_path):
    _write_agent(tmp_path, "ui", "ui-agent", "keywords: [UI]\n")
    # "GUIDE" contains "UI" but bordered by word chars.
    result = _run_matcher_subprocess("write me a GUIDE", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
