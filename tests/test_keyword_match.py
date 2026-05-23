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


def test_parse_short_desc(matcher):
    assert matcher.parse_short_desc("short_desc: hand-crafted hint\n") == "hand-crafted hint"
    assert matcher.parse_short_desc('short_desc: "quoted hint"\n') == "quoted hint"
    assert matcher.parse_short_desc("name: foo\ndescription: bar\n") == ""


def test_parse_short_desc_ignored_when_nested(matcher):
    # Same column-0 enforcement as parse_keywords.
    fm = "metadata:\n  short_desc: nested\n"
    assert matcher.parse_short_desc(fm) == ""


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


def test_format_single_agent_with_hint(matcher):
    out = matcher.format_suggestion([("foo", "scope hint")], [])
    assert out == "You might want to use this agent:\n- foo — scope hint"


def test_format_single_agent_without_hint(matcher):
    # Empty short_desc → no em-dash, just the name.
    out = matcher.format_suggestion([("foo", "")], [])
    assert out == "You might want to use this agent:\n- foo"


def test_format_two_agents_mixed_hints(matcher):
    out = matcher.format_suggestion(
        [("foo", "first hint"), ("bar", "")], []
    )
    assert out == (
        "You might want to use these agents:\n"
        "- foo — first hint\n"
        "- bar"
    )


def test_format_three_agents(matcher):
    out = matcher.format_suggestion(
        [("a", "A hint"), ("b", "B hint"), ("c", "C hint")], []
    )
    assert out == (
        "You might want to use these agents:\n"
        "- a — A hint\n"
        "- b — B hint\n"
        "- c — C hint"
    )


def test_format_single_skill(matcher):
    out = matcher.format_suggestion([], [("foo", "skill hint")])
    assert out == "You might want to use this skill:\n- foo — skill hint"


def test_format_two_skills(matcher):
    out = matcher.format_suggestion(
        [], [("foo", "foo hint"), ("bar", "bar hint")]
    )
    assert out == (
        "You might want to use these skills:\n"
        "- foo — foo hint\n"
        "- bar — bar hint"
    )


def test_format_combined_one_each(matcher):
    out = matcher.format_suggestion(
        [("agentA", "agent hint")], [("skillB", "skill hint")]
    )
    # Two blocks separated by blank line.
    assert out == (
        "You might want to use this agent:\n"
        "- agentA — agent hint\n"
        "\n"
        "You might want to use this skill:\n"
        "- skillB — skill hint"
    )


def test_format_combined_plural(matcher):
    out = matcher.format_suggestion(
        [("a1", "a1 hint"), ("a2", "a2 hint")],
        [("s1", "s1 hint"), ("s2", "s2 hint")],
    )
    blocks = out.split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("You might want to use these agents:")
    assert "- a1 — a1 hint" in blocks[0]
    assert "- a2 — a2 hint" in blocks[0]
    assert blocks[1].startswith("You might want to use these skills:")
    assert "- s1 — s1 hint" in blocks[1]
    assert "- s2 — s2 hint" in blocks[1]


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

    # Tuples are now (name, keywords, short_desc).
    agent_names = {n for n, _, _ in agents}
    skill_names = {n for n, _, _ in skills}
    assert agent_names == {"kubernetes-agent", "accessibility-checker"}
    assert skill_names == {"tdd-skill"}
    # No short_desc in the test fixtures → empty string fallback.
    for _, _, sd in agents:
        assert sd == ""
    for _, _, sd in skills:
        assert sd == ""


def test_collect_propagates_short_desc(tmp_path, matcher):
    _write_agent(
        tmp_path,
        "k",
        "k-agent",
        "short_desc: review k8s manifests for safety\nkeywords: [Kubernetes]\n",
    )
    agents = matcher.collect_agents(tmp_path)
    assert len(agents) == 1
    name, _, sd = agents[0]
    assert name == "k-agent"
    assert sd == "review k8s manifests for safety"


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
    names = {n for n, _, _ in agents}
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


def test_cli_one_agent_match_no_short_desc(tmp_path):
    # No short_desc → bullet renders as just `- name`.
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes, Helm]\n")
    result = _run_matcher_subprocess("review my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == (
        "You might want to use this agent:\n- kubernetes-agent"
    )


def test_cli_one_agent_match_with_short_desc(tmp_path):
    _write_agent(
        tmp_path,
        "k",
        "kubernetes-agent",
        "short_desc: review k8s manifests for safety\nkeywords: [Kubernetes, Helm]\n",
    )
    result = _run_matcher_subprocess("review my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == (
        "You might want to use this agent:\n"
        "- kubernetes-agent — review k8s manifests for safety"
    )


def test_cli_two_agents_match(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    _write_agent(tmp_path, "h", "h-agent", "keywords: [Helm]\n")
    result = _run_matcher_subprocess("review my Kubernetes and Helm setup", tmp_path)
    assert result.returncode == 0
    text = result.stdout.strip()
    # Plural header + one bullet per agent (sorted order from glob).
    assert text.startswith("You might want to use these agents:")
    assert "- k-agent" in text
    assert "- h-agent" in text


def test_cli_combined_agent_and_skill(tmp_path):
    _write_agent(tmp_path, "k", "k-agent", "keywords: [Kubernetes]\n")
    _write_skill(tmp_path, "tdd", "tdd-skill", "keywords: [TDD]\n")
    result = _run_matcher_subprocess("apply TDD to my Kubernetes manifest", tmp_path)
    assert result.returncode == 0
    text = result.stdout.strip()
    # Two blocks separated by a blank line.
    blocks = text.split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("You might want to use this agent:")
    assert "- k-agent" in blocks[0]
    assert blocks[1].startswith("You might want to use this skill:")
    assert "- tdd-skill" in blocks[1]


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
