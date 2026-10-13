# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-W: ``.claude/state`` writers resolve the project by walking up.

The defect: ``citation_pending._resolve_project_root()`` returned
``CLAUDE_PROJECT_DIR``, else the BARE cwd. The ``kg-search`` CLI runs without
``CLAUDE_PROJECT_DIR`` (Bash tool, user shell); run from a subfolder it staged
``<subfolder>/.claude/state/rl_pending/nosession__kg_cli_*.json``, which the
drain and the TTL sweep (both keyed on the project root) never visit. The same
bare-cwd rung lived in the RL fallback counter writer (+ rl-doctor's reader),
the hook context router's state root, the retrieval-expansion log, and — as
the install root rather than the cwd — the force-flush sentinel.

All of them now share ``vco_lib.project_root``: env -> nearest ancestor with
``.claude/settings.json`` or ``.claude/env`` (never a bare ``.claude/``, never
the user's home) -> ``None`` (writers skip).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_mcp_servers.rl_client import citation_pending as cp
from tests.common.child_env import child_env
from vco_lib import project_root as pr

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "claude_mcp_servers" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A marked project with a deep subfolder; env unset; home elsewhere.

    ``proj/.claude/settings.json`` marks the root. ``proj/a/b`` is the
    subfolder a CLI stands in, mirroring the dogfood orphans under
    ``.claude/context/reviews/``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    proj = tmp_path / "work" / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    return SimpleNamespace(tmp=tmp_path, home=home, proj=proj.resolve(), sub=sub)


def _stray_state_dirs(proj: Path) -> list[Path]:
    """Every ``.claude/state`` below ``proj`` other than the root's own."""
    return [
        p for p in proj.rglob(".claude/state")
        if p.resolve() != (proj / ".claude" / "state").resolve()
    ]


# --- the helper --------------------------------------------------------------


class TestResolver:
    def test_subfolder_walks_up_to_the_marked_root(self, layout, monkeypatch):
        monkeypatch.chdir(layout.sub)
        assert pr.find_vco_project_root() == layout.proj
        assert pr.resolve_project_root() == layout.proj

    def test_explicit_start_is_walked_too(self, layout):
        assert pr.find_vco_project_root(layout.sub) == layout.proj

    def test_env_wins_over_the_walk(self, layout, monkeypatch, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
        monkeypatch.chdir(layout.sub)
        assert pr.resolve_project_root() == other

    def test_blank_env_is_unset(self, layout, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "   ")
        monkeypatch.chdir(layout.sub)
        assert pr.resolve_project_root() == layout.proj

    def test_stray_claude_dir_without_markers_is_not_a_root(self, layout, monkeypatch):
        """The very orphan this bug planted must not become a root."""
        (layout.sub / ".claude" / "state" / "rl_pending").mkdir(parents=True)
        monkeypatch.chdir(layout.sub)
        assert pr.looks_like_vco_project(layout.sub) is False
        assert pr.resolve_project_root() == layout.proj

    def test_claude_env_file_is_a_marker(self, layout, monkeypatch):
        (layout.sub / ".claude").mkdir()
        (layout.sub / ".claude" / "env").write_text("", encoding="utf-8")
        monkeypatch.chdir(layout.sub)
        assert pr.resolve_project_root() == layout.sub.resolve()

    def test_nested_git_repo_is_not_a_boundary(self, layout, monkeypatch):
        """A vendored clone inside the project still resolves to the project."""
        (layout.proj / "a" / ".git").mkdir()
        monkeypatch.chdir(layout.sub)
        assert pr.resolve_project_root() == layout.proj

    def test_no_marked_ancestor_is_none(self, layout, monkeypatch):
        plain = layout.tmp / "plain" / "deep"
        plain.mkdir(parents=True)
        monkeypatch.chdir(plain)
        assert pr.resolve_project_root() is None

    def test_home_is_never_a_root(self, layout, monkeypatch):
        """``~/.claude/settings.json`` is the harness's global config."""
        (layout.home / ".claude").mkdir()
        (layout.home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        scratch = layout.home / "scratch"
        scratch.mkdir()
        monkeypatch.chdir(scratch)
        assert pr.resolve_project_root() is None


# --- citation_pending (the reported defect) ----------------------------------


def _stage(task_id: str = "kg_cli_deadbeef"):
    return cp.stage_pending(
        session_id="", task_id=task_id, seq=None, query="q", ctx={"a": 1},
        source="hook",
    )


class TestCitationPending:
    def test_subfolder_stage_lands_under_the_true_root(self, layout, monkeypatch):
        monkeypatch.chdir(layout.sub)
        path = _stage()
        assert path is not None
        assert path.parent == layout.proj / ".claude" / "state" / "rl_pending"
        assert path.name == "nosession__kg_cli_deadbeef.json"
        assert _stray_state_dirs(layout.proj) == []
        # ...and the owner's sweep/list (keyed on the root) can see it.
        assert cp.list_pending_for_session("", layout.proj) == [path]
        assert cp.list_pending_for_session("") == [path]

    def test_subfolder_sweep_reaches_the_root_queue(self, layout, monkeypatch):
        monkeypatch.chdir(layout.sub)
        path = _stage()
        assert path is not None
        assert cp.sweep_expired(ttl_seconds=0, now_ms=10**15) == 1
        assert not path.exists()

    def test_env_wins(self, layout, monkeypatch, tmp_path):
        other = tmp_path / "ws"
        other.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
        monkeypatch.chdir(layout.sub)
        path = _stage()
        assert path is not None
        assert path.parent == other / ".claude" / "state" / "rl_pending"
        assert not (layout.proj / ".claude" / "state").exists()

    def test_stray_claude_in_subfolder_is_walked_through(self, layout, monkeypatch):
        (layout.sub / ".claude" / "state" / "rl_pending").mkdir(parents=True)
        monkeypatch.chdir(layout.sub)
        path = _stage()
        assert path is not None
        assert path.parent == layout.proj / ".claude" / "state" / "rl_pending"
        assert list((layout.sub / ".claude" / "state" / "rl_pending").iterdir()) == []

    def test_no_root_skips_the_write_and_logs(self, layout, monkeypatch, caplog):
        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        with caplog.at_level("INFO", logger=cp.logger.name):
            assert _stage() is None
            assert cp.pending_dir() is None
            assert cp.list_pending_for_session("") == []
            assert cp.sweep_expired() == 0
        assert not (plain / ".claude").exists()
        assert any("skipping the pending-queue write" in r.message for r in caplog.records)

    def test_explicit_root_is_still_honoured(self, layout, monkeypatch):
        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert cp.pending_dir(layout.proj) == layout.proj / ".claude" / "state" / "rl_pending"

    def test_cli_shaped_child_from_a_subfolder(self, layout):
        """A real child process, as the kg-search CLI runs: no env, subfolder cwd."""
        # child_env with an explicit base: this checkout's import roots, and
        # the root channels (CLAUDE_PROJECT_DIR, KG_BASE_DIR) left as WE built
        # them — absent, which is the condition under test.
        base = {
            k: v for k, v in os.environ.items()
            if k not in ("CLAUDE_PROJECT_DIR", "KG_BASE_DIR")
        }
        env = child_env(base, VCT_USER_HOME_OVERRIDE=str(layout.home))
        assert "CLAUDE_PROJECT_DIR" not in env
        code = (
            "from claude_mcp_servers.rl_client.citation_pending import stage_pending\n"
            "print(stage_pending(session_id='', task_id='kg_cli_c0ffee00', seq=None,"
            " query='q', ctx={}, source='hook'))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=layout.sub, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        expected = layout.proj / ".claude" / "state" / "rl_pending" / "nosession__kg_cli_c0ffee00.json"
        assert out.stdout.strip() == str(expected)
        assert expected.is_file()
        assert _stray_state_dirs(layout.proj) == []


# --- RL fallback counter writer + rl-doctor reader -----------------------------


class TestRlCounter:
    @pytest.fixture(autouse=True)
    def _no_kg_base(self, monkeypatch):
        from claude_mcp_servers.rl_client import search_pipeline as sp

        monkeypatch.setattr(sp, "_kg_server", lambda: SimpleNamespace(KG_BASE_DIR=""))
        self.sp = sp

    def test_subfolder_counter_lands_under_root(self, layout, monkeypatch):
        monkeypatch.chdir(layout.sub)
        self.sp._bump_rl_counter("count", reason="r")
        f = layout.proj / ".claude" / "state" / "rl_fallback_counter.json"
        assert json.loads(f.read_text(encoding="utf-8"))["count"] == 1
        assert _stray_state_dirs(layout.proj) == []

    def test_no_root_writes_nothing(self, layout, monkeypatch):
        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        self.sp._bump_rl_counter("count")
        assert not (plain / ".claude").exists()

    def test_doctor_reads_what_the_writer_wrote(self, layout, monkeypatch):
        from claude_mcp_servers.scripts import rl_doctor

        monkeypatch.chdir(layout.sub)
        assert rl_doctor._resolve_project_root() == str(layout.proj)
        assert rl_doctor._resolve_project_root("/explicit") == "/explicit"


# --- hook context router ------------------------------------------------------


class TestRouter:
    def test_payload_cwd_subfolder_resolves_root(self, layout, monkeypatch):
        import hook_context_router as router

        monkeypatch.chdir(layout.tmp)
        assert router._resolve_project_root({"cwd": str(layout.sub)}) == str(layout.proj)

    def test_env_wins(self, layout, monkeypatch):
        import hook_context_router as router

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/the/workspace")
        assert router._resolve_project_root({"cwd": str(layout.sub)}) == "/the/workspace"

    def test_no_root_is_empty_and_never_cwd_relative(self, layout, monkeypatch):
        import hook_context_router as router

        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert router._resolve_project_root({"cwd": str(plain)}) == ""
        assert router._cache_dir("") is None
        router._budget_gc("")
        router.cache_put("", "k", "blob")
        assert not (plain / ".claude").exists()


# --- MCP: force-flush sentinel + retrieval-expansion log ----------------------


class TestServerSites:
    def test_sentinel_walks_up_from_cwd(self, layout, monkeypatch):
        from claude_mcp_servers.weaviate_mcp import server as srv

        monkeypatch.chdir(layout.sub)
        assert srv._rl_force_flush_sentinel_path() == (
            layout.proj / ".claude" / "state" / "rl_monitors_force_flush.flag"
        )

    def test_sentinel_env_wins(self, layout, monkeypatch, tmp_path):
        from claude_mcp_servers.weaviate_mcp import server as srv

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "ws"))
        monkeypatch.chdir(layout.sub)
        assert srv._rl_force_flush_sentinel_path() == (
            tmp_path / "ws" / ".claude" / "state" / "rl_monitors_force_flush.flag"
        )

    def test_sentinel_without_a_project_keeps_the_install_root(self, layout, monkeypatch):
        from claude_mcp_servers.weaviate_mcp import server as srv

        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert srv._rl_force_flush_sentinel_path() == (
            srv._SERVER_INFERRED_BASE / srv._RL_MONITOR_FORCE_FLUSH_SENTINEL
        )

    def test_expansion_log_walks_up_and_skips_without_root(self, layout, monkeypatch):
        from claude_mcp_servers.weaviate_mcp import server as srv

        monkeypatch.setattr(srv, "KG_BASE_DIR", "")
        monkeypatch.chdir(layout.sub)
        srv._log_detail_choice("q", "full", 1)
        assert list((layout.proj / ".claude" / "logs").glob("*_retrieval_expansion.jsonl"))
        assert not (layout.sub / ".claude").exists()

        plain = layout.tmp / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        srv._log_detail_choice("q", "full", 1)
        assert not (plain / ".claude").exists()
