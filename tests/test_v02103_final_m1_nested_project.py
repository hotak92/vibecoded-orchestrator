# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review M-1 — the KG reviewer in a project NESTED in a
parent git repo (the add flow's "use the existing repo" layout).

Before the fix the post-git-commit-kg-sync hook pair resolved the project
root with ``git rev-parse --show-toplevel`` — the PARENT repo — so it looked
for ``<parent>/.claude/scripts/kg_reviewer_backend.py``, printed a false
"broken install ... repair it with the project's bundle update" on every
commit and never reviewed. And even with the right script, the guard read
``git status`` paths (always repo-root-relative) as project-relative, so it
judged nothing correctly.

Now: the project root is the VCO project folder (the ONE hook rule,
``_lib/project-root``), git commands run inside it (git still finds the
enclosing repo), and the guard converts git's path OUTPUT to project-relative
while its ``knowledge/**`` scope stays relative to the project root. Both
hook siblings, act + leave-alone. ``claude`` is always faked.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_v02102_w6n_kg_reviewer_fixes import (  # noqa: E402
    _hook_cmd, _install_chain)
from tests.test_v02103_kg_reviewer_safety import (  # noqa: E402
    _FAKE_REVIEWER, PLAIN_NODE)
from vco_lib import kg_reviewer_guard as guard  # noqa: E402

_REAL_RUN = subprocess.run
PREFIX = "apps/foo/"


def _git(cwd: Path, env: dict, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=cwd, env=env, check=True,
                     capture_output=True, text=True).stdout


def _nested(tmp_path: Path) -> "tuple[Path, Path, dict]":
    """``mono/`` is the repo; ``mono/apps/foo/`` is the VCO project (it owns
    ``.claude/settings.json``). One commit with a node + a doc + a parent
    file; a fake ``claude`` first on PATH."""
    mono = tmp_path / "mono"
    proj = mono / "apps" / "foo"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (proj / "knowledge" / "concepts").mkdir(parents=True)
    (proj / "knowledge" / "concepts" / "cache-layer.md").write_text(
        PLAIN_NODE, encoding="utf-8")
    (proj / "docs.md").write_text("doc\n", encoding="utf-8")
    (mono / "parent.txt").write_text("parent\n", encoding="utf-8")
    (mono / ".gitignore").write_text("apps/foo/.claude/\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE_REVIEWER, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                        "VCO_KG_REVIEW_MAX_TURNS", "VCO_KG_REVIEW_MODEL",
                        "VCT_LAUNCHER_DB_PATH", "CLAUDE_PROJECT_DIR")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "VCT_STATE_DIR": str(tmp_path / "state"),
        "STDERR_CAP_DISABLE": "1",
    })
    _git(mono, env, "init", "-q")
    _git(mono, env, "add", "-A")
    _git(mono, env, "commit", "-q", "-m", "feat: docs")
    return mono, proj, env


def _wait_log(proj: Path, needles: "tuple[str, ...]", secs: float = 90) -> str:
    log = proj / ".claude" / "logs" / "kg-commit-review.log"
    deadline = time.monotonic() + secs
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8-sig") if log.exists() else ""
        if any(n in text for n in needles):
            return text
        time.sleep(0.3)
    return text


def _run_hook(sibling: str, proj: Path, env: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit"},
                          "tool_response": "[main abc] committed"})
    proc = _REAL_RUN(_hook_cmd(sibling), cwd=proj, env=env, input=payload,
                     capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc


# ── the hook siblings, end to end ────────────────────────────────────────


@pytest.mark.parametrize("via_env", [False, True], ids=["walk", "claude-project-dir"])
@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_nested_project_is_reviewed_and_only_the_reviewer_node_is_committed(
        tmp_path, sibling, via_env):
    """ACT. RED before the fix: "broken install ... kg_reviewer_backend.py is
    missing" (the hook looked under the PARENT repo) and no review ran."""
    _hook_cmd(sibling)  # skips when pwsh is absent
    mono, proj, env = _nested(tmp_path)
    _install_chain(proj, env)
    if via_env:
        env["CLAUDE_PROJECT_DIR"] = str(proj)
    # The user's own staged work, in the parent repo AND in the project.
    (mono / "parent.txt").write_text("user staged parent\n", encoding="utf-8")
    _git(mono, env, "add", "parent.txt")
    reviewed = _git(mono, env, "rev-parse", "HEAD").strip()
    proc = _run_hook(sibling, proj, env)
    assert "broken install" not in proc.stderr, proc.stderr
    assert not (mono / ".claude").exists(), "nothing may land in the parent repo root"
    text = _wait_log(proj, ("committed ", "commit skipped", "commit failed"))
    assert "broken install" not in text
    assert "committed " in text, text
    head = _git(mono, env, "rev-parse", "HEAD").strip()
    assert head != reviewed
    shown = _git(mono, env, "show", "--name-only", "--format=%s", head).split("\n")
    assert shown[0].startswith("kg-sync: ") and "feat: docs" in shown[0], shown
    assert [f for f in shown[1:] if f.strip()] == [f"{PREFIX}knowledge/hook-node.md"]
    # LEAVE-ALONE: the reviewer's docs edit (outside knowledge/) is reverted,
    # and the user's staged parent-repo file is neither committed nor unstaged.
    assert (proj / "docs.md").read_text(encoding="utf-8") == "doc\n"
    assert _git(mono, env, "diff", "--cached", "--name-only").split() == ["parent.txt"]
    row = [json.loads(x) for x in (proj / ".claude" / "logs" / "kg-commit-reviews.jsonl")
           .read_text(encoding="utf-8-sig").splitlines() if x.strip()][-1]
    assert "broken_install" not in row


@pytest.mark.parametrize("sibling", [
    pytest.param("sh", marks=pytest.mark.skipif(
        sys.platform == "win32",
        reason="bash .sh leg; Windows runs the .ps1 sibling: this test's [ps1] param")),
    "ps1",
])
def test_nested_project_without_the_chain_names_the_project_path(tmp_path, sibling):
    """LEAVE-ALONE of the loud-fail rule: a genuinely missing script in the
    nested project is still a broken install, named at the PROJECT path."""
    _hook_cmd(sibling)
    mono, proj, env = _nested(tmp_path)
    proc = _run_hook(sibling, proj, env)
    missing = str(proj / ".claude" / "scripts" / "kg_reviewer_backend.py")
    assert "broken install" in proc.stderr
    assert missing.replace("\\", "/") in proc.stderr.replace("\\", "/"), proc.stderr
    assert not (mono / ".claude").exists()


# ── the guard, directly ──────────────────────────────────────────────────


def test_guard_status_paths_are_project_relative_in_a_nested_project(tmp_path):
    mono, proj, env = _nested(tmp_path)
    os.environ.update({k: env[k] for k in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL")})
    (proj / "knowledge" / "concepts" / "cache-layer.md").write_text(
        PLAIN_NODE + "user wip\n", encoding="utf-8")
    (mono / "parent.txt").write_text("user wip parent\n", encoding="utf-8")
    assert guard.repo_prefix(proj) == PREFIX
    assert guard.repo_prefix(mono) == ""
    snap = guard.take_snapshot(proj)
    try:
        # ACT: the user's dirty node is known under its PROJECT-relative path,
        # so a reviewer commit touching it is refused.
        assert "knowledge/concepts/cache-layer.md" in snap.dirty_before
        # LEAVE-ALONE: a parent-repo path is not the project's.
        assert not any(p.endswith("parent.txt") for p in snap.dirty_before)
        assert not any(p.startswith(PREFIX) for p in snap.dirty_before)
        # pre-run bytes of a tracked project file come from HEAD:./<rel>
        assert snap.pre_bytes("docs.md") == (True, b"doc\n")
        block = guard.commit_block_reason(
            snap, ["knowledge/concepts/cache-layer.md"], True)
        assert block and "uncommitted changes" in block
    finally:
        snap.cleanup()


def test_guard_commits_the_project_relative_path_in_a_nested_project(tmp_path):
    mono, proj, env = _nested(tmp_path)
    for k in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME",
              "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        os.environ[k] = env[k]
    snap = guard.take_snapshot(proj)
    lines: list = []
    try:
        node = "knowledge/concepts/new-topic.md"
        (proj / node).write_text("---\ntitle: N\n---\nF.\n", encoding="utf-8")
        sha = guard.commit_reviewer_changes(
            snap, [node], reviewer_bytes={node: (proj / node).read_bytes()},
            short="abc1234", subject="feat: docs", model="sonnet",
            attributed=True, log=lines.append)
    finally:
        snap.cleanup()
    assert sha, lines
    files = _git(mono, env, "show", "--name-only", "--format=", sha).split()
    assert files == [f"{PREFIX}{node}"]
    # the post-commit extras check maps git's output back: no false WARNING
    assert not any("WARNING" in ln for ln in lines), lines


@pytest.fixture(autouse=True)
def _restore_git_env():
    keys = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_prompt_tells_a_nested_reviewer_how_diff_paths_map(tmp_path):
    from vco_lib import kg_reviewer_prompt as prompt_mod
    mono, proj, env = _nested(tmp_path)
    os.environ.update({k: env[k] for k in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL")})
    commit = prompt_mod.collect_commit(proj, "HEAD")
    nested = prompt_mod.build_prompt(commit, proj)
    assert f"the folder `{PREFIX}` of a larger git repository" in nested
    flat = prompt_mod.build_prompt(commit, mono)
    assert "of a larger git repository" not in flat


def test_backend_default_project_root_is_the_vco_project_not_the_parent_repo(
        tmp_path, monkeypatch):
    """The chain's own fallback (no ``--project-root``) uses the same rule."""
    from tests.test_v02103_kg_reviewer_safety import SCRIPTS_DIR, _load
    mod = _load("_m1_kg_reviewer_backend", SCRIPTS_DIR / "kg_reviewer_backend.py")
    mono, proj, env = _nested(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(proj / "knowledge")
    assert mod._project_root(None) == proj.resolve()
    # leave-alone: an explicit --project-root is taken as given
    assert mod._project_root(str(mono)) == mono.resolve()
    # a folder with no VCO marker keeps the pre-v0.2.103 toplevel fallback
    monkeypatch.chdir(mono)
    assert mod._project_root(None) == mono.resolve()
