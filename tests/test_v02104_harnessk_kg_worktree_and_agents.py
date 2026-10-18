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
    assert "absolute path" in res["path_note"]
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


# ---------------------------------------------------------------------------
# Review round fixes (B-M1 matcher migration, B-M2/L2 path containment, L4 note)
# ---------------------------------------------------------------------------

OLD_MATCHER = "ScheduleWakeup|CronCreate|Monitor"
NEW_MATCHER = "ScheduleWakeup|CronCreate"


def _wait_guard_matchers(doc: dict) -> list:
    return [g.get("matcher") for g in doc["hooks"]["PreToolUse"]
            if any("wait-guard" in h.get("command", "") for h in g["hooks"])]


@pytest.mark.parametrize("flavour", ["linux", "windows"])
def test_installed_old_self_wake_group_is_migrated_not_duplicated(flavour):
    import copy
    import json

    from vco_lib import settings_merge

    template = json.loads((REPO_ROOT / "templates" /
                           f"settings.json.{flavour}.template").read_text(encoding="utf-8"))
    installed = copy.deepcopy(template)  # the 5b896ee0 shape: old matcher
    for g in installed["hooks"]["PreToolUse"]:
        if g.get("matcher") == NEW_MATCHER:
            g["matcher"] = OLD_MATCHER
    assert OLD_MATCHER in _wait_guard_matchers(installed)

    merged = settings_merge.smart_merge_settings(installed, template)
    got = _wait_guard_matchers(merged)
    assert OLD_MATCHER not in got
    assert got.count(NEW_MATCHER) == 1
    assert sorted(got, key=str) == sorted(_wait_guard_matchers(template), key=str)
    # fixed point
    assert settings_merge.smart_merge_settings(copy.deepcopy(merged), template) == merged


def test_user_group_with_own_hook_keeps_its_matcher():
    import copy
    import json

    from vco_lib import settings_merge

    template = json.loads((REPO_ROOT / "templates" /
                           "settings.json.linux.template").read_text(encoding="utf-8"))
    installed = copy.deepcopy(template)
    for g in installed["hooks"]["PreToolUse"]:
        if g.get("matcher") == NEW_MATCHER:
            g["matcher"] = OLD_MATCHER
            g["hooks"].append({"type": "command", "command": "bash ~/mine.sh"})
    merged = settings_merge.smart_merge_settings(installed, template)
    assert OLD_MATCHER in [g.get("matcher") for g in merged["hooks"]["PreToolUse"]]


def test_spoofed_git_file_with_nonexistent_gitdir_is_rejected(repo_and_worktree, tmp_path):
    main, _wt = repo_and_worktree
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / ".git").write_text(f"gitdir: {main}/.git/worktrees/does-not-exist\n")
    assert _server()._linked_worktree_root(evil / "k" / "n.md", main) is None


def test_gitdir_without_reciprocal_pointer_is_rejected(repo_and_worktree, tmp_path):
    main, _wt = repo_and_worktree
    real = next((main / ".git" / "worktrees").iterdir())
    evil = tmp_path / "evil2"
    evil.mkdir()
    (evil / ".git").write_text(f"gitdir: {real}\n")  # exists, but points back at lane
    assert _server()._linked_worktree_root(evil / "k" / "n.md", main) is None


def test_pruned_worktree_stale_marker_is_rejected(repo_and_worktree):
    import shutil

    main, wt = repo_and_worktree
    gitdir = next((main / ".git" / "worktrees").iterdir())
    moved = main.parent / "pruned-gitdir"
    shutil.move(str(gitdir), str(moved))  # what `git worktree prune` leaves
    assert _server()._linked_worktree_root(wt / "k" / "n.md", main) is None


def test_dotdot_cannot_escape_the_worktree_while_claiming_it(
        monkeypatch, repo_and_worktree):
    main, wt = repo_and_worktree
    _patch_server_for_store(monkeypatch, main, _FakeCollection())
    sneaky = f"{wt}/knowledge/../../main/knowledge/x.md"
    res = _store(_server(), file_path=sneaky)
    assert ".." not in res["absolute_path"]
    assert res["absolute_path"] == str(main / "knowledge" / "x.md")
    assert res["file_path"] == "knowledge/x.md"
    assert "worktree" not in res.get("path_note", "")


def test_relative_path_note_is_short(monkeypatch, tmp_path):
    _patch_server_for_store(monkeypatch, tmp_path, _FakeCollection())
    res = _store(_server())
    assert len(res["path_note"]) < 160
