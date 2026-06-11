# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for detect-workflow-needs + generate-workflow (CLI, end-to-end)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DETECT = REPO_ROOT / "templates" / "scripts" / "detect_workflow_needs.py"
GENERATE = REPO_ROOT / "templates" / "scripts" / "generate_workflow.py"


def _run(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=60,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# detect-workflow-needs
# ---------------------------------------------------------------------------


def test_detect_empty_project_no_recs(project):
    res = _run(DETECT, "--json", cwd=project)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["recommendations"] == []


def test_detect_signals_map_to_recipes(project):
    (project / "package.json").write_text("{}", encoding="utf-8")
    (project / "CHANGELOG.md").write_text("# changelog", encoding="utf-8")
    src = project / "src"
    src.mkdir()
    for i in range(12):
        (src / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    (project / ".git").mkdir()  # signal only; no real repo needed
    res = _run(DETECT, "--json", cwd=project)
    payload = json.loads(res.stdout)
    names = {r["name"] for r in payload["recommendations"]}
    assert {"dependency-update-check", "code-review-loop", "release-prep"} <= names
    assert payload["signals"]["languages"].get("python", 0) >= 12


def test_detect_excludes_existing_workflows(project):
    (project / "package.json").write_text("{}", encoding="utf-8")
    wf = project / ".claude" / "workflows"
    wf.mkdir(parents=True)
    (wf / "dependency-update-check.mjs").write_text("export const meta = {}", encoding="utf-8")
    res = _run(DETECT, "--json", cwd=project)
    names = {r["name"] for r in json.loads(res.stdout)["recommendations"]}
    assert "dependency-update-check" not in names


def test_detect_skips_vendored_dirs(project):
    nm = project / "node_modules" / "dep"
    nm.mkdir(parents=True)
    for i in range(50):
        (nm / f"v{i}.js").write_text("x", encoding="utf-8")
    res = _run(DETECT, "--json", cwd=project)
    payload = json.loads(res.stdout)
    assert payload["signals"]["code_files"] == 0


# ---------------------------------------------------------------------------
# generate-workflow
# ---------------------------------------------------------------------------

STOCK = ["dependency-update-check", "code-review-loop", "release-prep", "weekly-housekeeping"]


def test_generate_list_templates(project):
    res = _run(GENERATE, "--list-templates", cwd=project)
    assert res.returncode == 0
    assert set(res.stdout.split()) == set(STOCK)


@pytest.mark.parametrize("name", STOCK)
def test_generate_stock_templates_render_clean(project, name):
    res = _run(GENERATE, name, cwd=project)
    assert res.returncode == 0, res.stderr
    target = project / ".claude" / "workflows" / f"{name}.mjs"
    content = target.read_text(encoding="utf-8")
    # meta contract: pure-literal meta with the routing fields.
    assert content.startswith("export const meta = {")
    assert f"name: '{name}'" in content
    assert "keywords: [" in content
    # No leftover Python-format artifacts.
    assert "{{" not in content and "}}" not in content
    # Balanced braces (cheap structural sanity for the JS).
    assert content.count("{") == content.count("}")
    # Template-literal interpolations survived .format() escaping.
    if name != "release-prep":
        assert re.search(r"\$\{[A-Za-z_]", content) or "${" not in content


def test_generate_generic_scaffold_and_force(project):
    res = _run(GENERATE, "my-custom-flow", "--description", "Custom flow", cwd=project)
    assert res.returncode == 0, res.stderr
    target = project / ".claude" / "workflows" / "my-custom-flow.mjs"
    content = target.read_text(encoding="utf-8")
    assert "EDIT ME" in content
    assert "name: 'my-custom-flow'" in content
    assert "Custom flow" in content

    res2 = _run(GENERATE, "my-custom-flow", cwd=project)
    assert res2.returncode == 2
    assert "already exists" in res2.stderr

    res3 = _run(GENERATE, "my-custom-flow", "--force", cwd=project)
    assert res3.returncode == 0


def test_generate_rejects_bad_names(project):
    for bad in ("Bad_Name", "UPPER", "a b", "-leading", ""):
        res = _run(GENERATE, bad, cwd=project) if bad else _run(GENERATE, cwd=project)
        # 1 = our validator; 2 = argparse itself (e.g. "-leading" looks like a flag).
        # Either way the name is rejected and nothing is written.
        assert res.returncode in (1, 2), f"{bad!r} should be rejected"
        assert not list((project / ".claude").rglob("*.mjs"))


def test_generated_keywords_parse_with_matcher(project):
    """The keyword-suggest matcher must be able to read meta.keywords from
    generated workflows (integration with the workflow-suggestion hook)."""
    matcher_path = REPO_ROOT / "templates" / "scripts" / "agent-skill-keyword-match.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("kw_m", matcher_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if not hasattr(m, "collect_workflows"):
        pytest.skip("matcher workflow extension not yet ported to templates/scripts")
    _run(GENERATE, "release-prep", cwd=project)
    out = m.collect_workflows(project)
    assert out and out[0][0] == "release-prep" and len(out[0][1]) >= 3
