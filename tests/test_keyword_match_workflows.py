# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the WORKFLOW-suggestion extension of agent-skill-keyword-match.py.

Ported from the dogfooding prototype (v0.2.54 Track D Step 0, 2026-06-11):
the extension now lives in the canonical `templates/scripts/` copy. These
tests exercise the workflow-file keyword parsing (`// keywords:` comment
form + `meta.keywords` array form), the main-loop-only gating, and the
`w:`-prefixed dedup keys.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCHER_PATH = REPO_ROOT / "templates" / "scripts" / "agent-skill-keyword-match.py"


def _load_matcher():
    spec = importlib.util.spec_from_file_location("kw_match_wf_under_test", MATCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def matcher():
    assert MATCHER_PATH.is_file(), f"matcher not found at {MATCHER_PATH}"
    return _load_matcher()


def _write_workflow(root: Path, filename: str, body: str) -> Path:
    wf_dir = root / ".claude" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / filename
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


WORKFLOW_COMMENT_FORM = """\
    // keywords: kg audit, orphaned nodes
    export const meta = {
      name: 'vco-kg-audit',
      description: 'Audit the KG for drift',
      phases: [{ title: 'Scan' }],
    }
    phase('Scan')
    const r = await agent('scan')
    return r
"""

WORKFLOW_META_FORM = """\
    export const meta = {
      name: 'vco-release-audit',
      description: 'Release audit fan-out',
      keywords: ['release audit', "leak scan"],
    }
    const r = await agent('audit')
    return r
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_collect_workflows_comment_form(matcher, tmp_path):
    _write_workflow(tmp_path, "vco-kg-audit.mjs", WORKFLOW_COMMENT_FORM)
    out = matcher.collect_workflows(tmp_path)
    assert out == [("vco-kg-audit", ["kg audit", "orphaned nodes"], "Audit the KG for drift")]


def test_collect_workflows_meta_keywords_form(matcher, tmp_path):
    _write_workflow(tmp_path, "vco-release-audit.mjs", WORKFLOW_META_FORM)
    out = matcher.collect_workflows(tmp_path)
    assert out == [("vco-release-audit", ["release audit", "leak scan"], "Release audit fan-out")]


def test_collect_workflows_js_extension_and_stem_fallback(matcher, tmp_path):
    # No meta name → display falls back to the file stem; .js is collected.
    _write_workflow(
        tmp_path,
        "stem-fallback.js",
        """\
        // keywords: stemkw
        export const meta = {
          description: 'No name field',
        }
        return 1
        """,
    )
    out = matcher.collect_workflows(tmp_path)
    assert out == [("stem-fallback", ["stemkw"], "No name field")]


def test_collect_workflows_no_keywords_is_skipped(matcher, tmp_path):
    _write_workflow(
        tmp_path,
        "silent.mjs",
        """\
        export const meta = { name: 'silent', description: 'no keywords here' }
        return 1
        """,
    )
    assert matcher.collect_workflows(tmp_path) == []


def test_collect_workflows_missing_dir(matcher, tmp_path):
    assert matcher.collect_workflows(tmp_path) == []


def test_meta_block_extraction_balanced_braces(matcher):
    js = "export const meta = { name: 'x', phases: [{ title: 'A' }, { title: 'B' }] }\nrest"
    block = matcher._extract_meta_block(js)
    assert block.startswith("{") and block.endswith("}")
    assert "title: 'B'" in block


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_workflow_group_offer_wording_and_slash(matcher):
    msg = matcher.format_suggestion([], [], [("vco-kg-audit", "Audit the KG")])
    assert "OFFER it to the user" in msg
    assert "- /vco-kg-audit — Audit the KG" in msg
    assert "do not launch it unsolicited" in msg


def test_format_suggestion_back_compat_two_args(matcher):
    # Existing callers pass only (agents, skills) — must not raise.
    msg = matcher.format_suggestion([("a1", "")], [("s1", "")])
    assert "a1" in msg and "s1" in msg


# ---------------------------------------------------------------------------
# End-to-end CLI behavior
# ---------------------------------------------------------------------------


def _run_cli(matcher_path: Path, root: Path, prompt: str, *args: str) -> str:
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["VCT_KEYWORD_DEDUP_DIR"] = str(root / "dedup")
    res = subprocess.run(
        [sys.executable, str(matcher_path), *args],
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert res.returncode == 0
    return res.stdout


def test_cli_workflow_match_end_to_end(tmp_path):
    _write_workflow(tmp_path, "vco-kg-audit.mjs", WORKFLOW_COMMENT_FORM)
    out = _run_cli(MATCHER_PATH, tmp_path, "please run a kg audit on this project")
    assert "/vco-kg-audit" in out


def test_cli_workflow_suppressed_under_skills_only(tmp_path):
    _write_workflow(tmp_path, "vco-kg-audit.mjs", WORKFLOW_COMMENT_FORM)
    out = _run_cli(MATCHER_PATH, tmp_path, "please run a kg audit", "--skills-only")
    assert out.strip() == ""


def test_cli_workflow_dedup_second_prompt_silent(tmp_path):
    _write_workflow(tmp_path, "vco-kg-audit.mjs", WORKFLOW_COMMENT_FORM)
    sid = "11111111-2222-3333-4444-555555555555"
    first = _run_cli(MATCHER_PATH, tmp_path, "run a kg audit", "--session-id", sid)
    assert "/vco-kg-audit" in first
    second = _run_cli(MATCHER_PATH, tmp_path, "another kg audit please", "--session-id", sid)
    assert "/vco-kg-audit" not in second
