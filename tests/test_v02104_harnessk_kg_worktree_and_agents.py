# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 harness K1/K3: worktree-lane KG writes and the commit hand-off line.

K1: ``store_knowledge_node`` resolves a relative ``file_path`` against
``KG_BASE_DIR`` (the MAIN checkout); the response says so (``path_note``). An
absolute ``file_path`` inside a linked worktree of that checkout is stored
relative to the worktree. K3: every shipped implementer lane that forbids git
tells the coordinator-commits hand-off.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.test_v0273_kg_write_path import (  # noqa: E402
    _FakeCollection,
    _patch_server_for_store,
    _server,
    _store,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def repo_and_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "a.txt").write_text("x")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "lane"
    _git(main, "worktree", "add", "-q", "-b", "lane", str(wt))
    return main, wt


def test_relative_path_reports_main_checkout(monkeypatch, tmp_path):
    coll = _FakeCollection()
    _patch_server_for_store(monkeypatch, tmp_path, coll)
    res = _store(_server())
    assert res["absolute_path"] == str(tmp_path / "knowledge/concepts/sample_a.md")
    assert "KG_BASE_DIR" in res["path_note"]
    assert "ABSOLUTE" in res["path_note"]
    assert str(tmp_path) in res["path_note"]


def test_absolute_path_in_linked_worktree_is_relative_to_it(
        monkeypatch, repo_and_worktree):
    main, wt = repo_and_worktree
    coll = _FakeCollection()
    _patch_server_for_store(monkeypatch, main, coll)
    target = wt / "knowledge" / "concepts" / "lane_node.md"
    res = _store(_server(), file_path=str(target))
    assert target.exists()
    assert not (main / "knowledge").exists(), "must not land in the main checkout"
    assert res["file_path"] == "knowledge/concepts/lane_node.md"
    assert str(wt) in res["path_note"]


def test_absolute_path_outside_any_worktree_is_stored_as_is(
        monkeypatch, repo_and_worktree, tmp_path):
    main, _wt = repo_and_worktree
    coll = _FakeCollection()
    _patch_server_for_store(monkeypatch, main, coll)
    other = tmp_path / "elsewhere" / "n.md"
    res = _store(_server(), file_path=str(other))
    assert res["file_path"] == str(other)
    assert "path_note" not in res


def test_linked_worktree_root_is_none_for_a_foreign_repo(tmp_path, repo_and_worktree):
    main, _wt = repo_and_worktree
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q")
    (foreign / "b.txt").write_text("x")
    _git(foreign, "add", "b.txt")
    _git(foreign, "commit", "-q", "-m", "i")
    fwt = tmp_path / "foreign-lane"
    _git(foreign, "worktree", "add", "-q", "-b", "fl", str(fwt))
    srv = _server()
    assert srv._linked_worktree_root(fwt / "k" / "n.md", main) is None


def test_tool_docstring_names_the_worktree_rule():
    doc = _server().store_knowledge_node.__doc__ or getattr(
        getattr(_server().store_knowledge_node, "fn", None), "__doc__", "") or ""
    assert "WORKTREE LANES" in doc and "ABSOLUTE" in doc


GATEWAY_AGENTS = REPO_ROOT / "templates" / "agents" / "module-gateway"
HANDOFF = ("The coordinator commits: end your report with the exact list "
           "of files you changed.")


def test_every_git_forbidding_agent_carries_the_commit_handoff():
    forbidding = [p for p in sorted(GATEWAY_AGENTS.glob("*.md"))
                  if "NEVER run any git command" in p.read_text(encoding="utf-8")]
    assert len(forbidding) >= 7
    for p in forbidding:
        assert HANDOFF in p.read_text(encoding="utf-8"), p.name
