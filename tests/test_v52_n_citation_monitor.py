# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-N — citation monitor: tool-agnostic extractor + compaction sentinel.

Covers the V52-N rewrite of the RL citation answer-monitor in
``claude_mcp_servers/weaviate_mcp/server.py``:

  * ``_rl_extract_answer_window`` is now TOOL-AGNOSTIC -- every
    ``tool_use`` block contributes (name + JSON input), not just
    Write/Edit. Tool OUTPUTS still excluded.
  * Human turns are NO LONGER a stop condition -- accumulation continues
    across follow-ups.
  * The accumulator threshold is expressed in TOKENS (25 000) aligned
    with the citation gate.
  * The PreCompact hook drops ``.claude/state/rl_monitors_force_flush.flag``;
    the monitor polls for it and fires with whatever's accumulated.
  * The monitor deletes the sentinel after firing so subsequent
    compactions re-arm.
  * 60-min safety-valve timeout (raised from 10 min).

Test surface:
  1. ``_rl_extract_answer_window`` collects text + thinking + tool_use input
     from ALL tools (not just Write/Edit).
  2. ``_rl_extract_answer_window`` does NOT stop on a human turn -- assistant
     blocks after the human still get accumulated.
  3. The 25k-token threshold (via 100k-char approximation) fires complete=True.
  4. ``_rl_check_force_flush`` reads the sentinel; ``_rl_clear_force_flush``
     deletes it.
  5. Sentinel path resolution honours ``CLAUDE_PROJECT_DIR`` and falls
     back to ``_SERVER_INFERRED_BASE``.
  6. Constants: ``_RL_MONITOR_TIMEOUT == 3600.0`` (V52-N safety valve),
     ``_RL_MONITOR_ANSWER_THRESHOLD_TOKENS == 25_000``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the server module under test. We do NOT use importorskip because
# server.py is part of the orchestrator's core MCP and ships with the
# branch — the V52-N edits live in it directly.
from claude_mcp_servers.weaviate_mcp import server as srv


# ----------------------------------------------------------------------
# Helpers — synthesize Claude transcript JSONL
# ----------------------------------------------------------------------


def _kg_search_block(query: str = "test query") -> dict:
    """Build a tool_use block that mimics an MCP kg-search call."""
    return {
        "type": "tool_use",
        "name": "hybrid_search",
        "input": {"query": query, "limit": 5},
    }


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _thinking_block(text: str) -> dict:
    return {"type": "thinking", "thinking": text}


def _tool_use_block(name: str, **input_kwargs) -> dict:
    return {"type": "tool_use", "name": name, "input": dict(input_kwargs)}


def _assistant_msg(*blocks) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _user_human_msg(text: str = "follow up") -> dict:
    """A real human turn (no toolUseResult field)."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _user_tool_result_msg(content: str = "result") -> dict:
    """A tool-result message (the user-type carrier of tool output).
    Has toolUseResult set so the legacy code path treated it as not-a-
    stop-signal; V52-N skips all user msgs regardless."""
    return {
        "type": "user",
        "toolUseResult": {"stdout": content},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": content}],
        },
    }


# ----------------------------------------------------------------------
# 1. Tool-agnostic extractor
# ----------------------------------------------------------------------


class TestExtractAnswerWindow:
    """V52-N tool-agnostic accumulation: every tool_use's name + input
    contributes; outputs do not."""

    def test_collects_text_blocks(self):
        messages = [
            _assistant_msg(_kg_search_block(), _text_block("first answer chunk")),
            _assistant_msg(_text_block(" second chunk")),
        ]
        text, complete = srv._rl_extract_answer_window(messages, 0, 0)
        assert "first answer chunk" in text
        assert "second chunk" in text
        assert complete is False  # below threshold

    def test_collects_thinking_blocks(self):
        messages = [
            _assistant_msg(_kg_search_block(), _thinking_block("internal scratch")),
            _assistant_msg(_text_block("final answer")),
        ]
        text, _ = srv._rl_extract_answer_window(messages, 0, 0)
        assert "internal scratch" in text
        assert "final answer" in text

    def test_collects_every_tool_use_input_not_just_write_edit(self):
        """V52-N change: any tool_use (Bash, Grep, Read, custom) contributes."""
        messages = [
            _assistant_msg(_kg_search_block()),
            _assistant_msg(_tool_use_block("Bash", command="ls -la /tmp")),
            _assistant_msg(_tool_use_block("Grep", pattern="def foo", path="src/")),
            _assistant_msg(_tool_use_block("Read", file_path="/x/y.py")),
            _assistant_msg(_tool_use_block("CustomTool", payload={"key": "val"})),
        ]
        text, _ = srv._rl_extract_answer_window(messages, 0, 0)
        # Tool names are emitted with their inputs (user said: "if tool name
        # ends up in the output it's ok").
        assert "Bash" in text
        assert "ls -la /tmp" in text
        assert "Grep" in text
        assert "def foo" in text
        assert "Read" in text
        assert "/x/y.py" in text
        assert "CustomTool" in text
        assert "key" in text

    def test_excludes_tool_use_result_messages(self):
        """tool_result + toolUseResult-carrying user messages are skipped
        even though they're user-type. Tool OUTPUTS would drown out the
        signal."""
        messages = [
            _assistant_msg(_kg_search_block(), _text_block("answer prefix ")),
            _user_tool_result_msg("MASSIVE OUTPUT DUMP THAT SHOULD NOT APPEAR"),
            _assistant_msg(_text_block("answer continues")),
        ]
        text, _ = srv._rl_extract_answer_window(messages, 0, 0)
        assert "MASSIVE OUTPUT DUMP" not in text
        assert "answer prefix" in text
        assert "answer continues" in text

    def test_does_NOT_stop_on_human_turn(self):
        """V52-N: subsequent assistant blocks after a human follow-up
        count as part of the same answer accumulation. Pre-V52-N the
        monitor would stop here on a short window and the citation gate
        would silently reject it."""
        messages = [
            _assistant_msg(_kg_search_block(), _text_block("part 1 of answer ")),
            _user_human_msg("yes, continue"),
            _assistant_msg(_text_block("part 2 after follow-up ")),
            _user_human_msg("more please"),
            _assistant_msg(_text_block("part 3 after second follow-up")),
        ]
        text, complete = srv._rl_extract_answer_window(messages, 0, 0)
        assert "part 1 of answer" in text
        assert "part 2 after follow-up" in text
        assert "part 3 after second follow-up" in text
        # Human turn text itself should NOT be in there.
        assert "yes, continue" not in text
        assert "more please" not in text
        # All parts together are still below threshold → complete=False.
        assert complete is False

    def test_fires_complete_when_threshold_reached(self):
        """When accumulated chars reach _RL_MONITOR_ANSWER_THRESHOLD_TOKENS*4,
        the extractor returns complete=True with a truncated window."""
        threshold_chars = srv._RL_MONITOR_ANSWER_THRESHOLD_TOKENS * 4
        # Build a big text block so a single assistant message crosses
        # the threshold.
        big_text = "x" * (threshold_chars + 5_000)
        messages = [
            _assistant_msg(_kg_search_block(), _text_block(big_text)),
        ]
        text, complete = srv._rl_extract_answer_window(messages, 0, 0)
        assert complete is True
        # Truncated to threshold.
        assert len(text) <= threshold_chars

    def test_truncates_per_tool_use_input_at_limit(self):
        """A single huge tool_use input (e.g. paste of a 200K-char file)
        does not single-handedly explode the answer window. Pre-V52-N
        the limit was per Write/Edit content; V52-N applies it to every
        tool_use's serialized input."""
        per_call_limit = srv._RL_TOOL_CONTENT_LIMIT
        # 50K of payload in ONE tool_use call.
        huge_payload = "y" * (per_call_limit + 30_000)
        messages = [
            _assistant_msg(_kg_search_block()),
            _assistant_msg(_tool_use_block("Bash", command=huge_payload)),
        ]
        text, _ = srv._rl_extract_answer_window(messages, 0, 0)
        # The single tool-use contribution is bounded by the per-call
        # limit, not the threshold.  We can't measure the exact length
        # because the JSON wrapping adds bytes, but it's strictly less
        # than the per-call limit by construction.
        assert len(text) <= per_call_limit + 200  # tiny prefix slack
        # And the start of the payload should appear.
        assert "y" * 100 in text


# ----------------------------------------------------------------------
# 2. Sentinel file operations
# ----------------------------------------------------------------------


class TestSentinelFile:
    """V52-N sentinel write-by-hook + read-by-monitor + clear-after-fire."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp(prefix="v52n_sentinel_")
        self._saved_env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self._tmpdir

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        if self._saved_env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._saved_env

    def _sentinel(self) -> Path:
        p = Path(self._tmpdir) / srv._RL_MONITOR_FORCE_FLUSH_SENTINEL
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_sentinel_path_honours_claude_project_dir(self):
        """The resolved sentinel path is rooted at CLAUDE_PROJECT_DIR."""
        resolved = srv._rl_force_flush_sentinel_path()
        assert str(resolved).startswith(self._tmpdir)
        assert resolved.name == "rl_monitors_force_flush.flag"

    def test_check_returns_false_when_no_sentinel(self):
        # Ensure absent.
        s = self._sentinel()
        if s.exists():
            s.unlink()
        assert srv._rl_check_force_flush() is False

    def test_check_returns_true_when_sentinel_present(self):
        self._sentinel().write_text("1234567890")
        assert srv._rl_check_force_flush() is True

    def test_clear_removes_sentinel(self):
        self._sentinel().write_text("present")
        assert srv._rl_check_force_flush() is True
        srv._rl_clear_force_flush()
        assert srv._rl_check_force_flush() is False

    def test_clear_is_soft_fail_when_missing(self):
        """Clearing a missing sentinel is a no-op, not an exception.
        Race with the hook: monitor sees presence on poll N, clears,
        but by N+1 something else has touched the file path -- should
        not crash the monitor."""
        # Should not raise.
        srv._rl_clear_force_flush()
        srv._rl_clear_force_flush()
        srv._rl_clear_force_flush()

    def test_check_is_soft_fail_on_filesystem_error(self):
        """Filesystem probe raising returns False, not exception."""
        with patch.object(srv, "_rl_force_flush_sentinel_path",
                          side_effect=OSError("disk full")):
            assert srv._rl_check_force_flush() is False


# ----------------------------------------------------------------------
# 3. Constants — load-bearing for citation gate alignment
# ----------------------------------------------------------------------


class TestConstants:
    """The V52-N constant block is load-bearing: if it drifts, the
    pre-V52-N silent-drop reappears (monitor fires before gate accepts)."""

    def test_threshold_tokens_matches_citation_gate(self):
        """The whole point of V52-N: monitor + gate use the same number."""
        assert srv._RL_MONITOR_ANSWER_THRESHOLD_TOKENS == 25_000
        assert srv._RL_MIN_ANSWER_TOKENS_FOR_CITATION == 25_000

    def test_legacy_char_threshold_is_4x_tokens(self):
        """Back-compat alias kept = 4× token count (qwen3 BPE average)."""
        assert srv._RL_MONITOR_ANSWER_THRESHOLD == (
            srv._RL_MONITOR_ANSWER_THRESHOLD_TOKENS * 4
        )

    def test_timeout_is_60_minutes(self):
        """Safety-valve only (raised from 10 min).  Compaction sentinel
        + threshold are the real stops."""
        assert srv._RL_MONITOR_TIMEOUT == 3600.0

    def test_sentinel_path_constant_is_relative_to_state_dir(self):
        """Project-local .claude/state/ matches existing convention."""
        assert srv._RL_MONITOR_FORCE_FLUSH_SENTINEL == \
            ".claude/state/rl_monitors_force_flush.flag"


# ----------------------------------------------------------------------
# 4. End-to-end-ish: synthesize a 30k-token-ish transcript, run extract
# ----------------------------------------------------------------------


def test_synthesized_30k_token_transcript_fires_complete():
    """Build a transcript whose accumulated text exceeds the 25k-token
    (~100k-char) threshold and verify the extractor returns
    complete=True. This mimics the realistic "Claude produced a long
    technical answer with code samples" scenario."""
    # 30k tokens at 4 chars/token = 120K chars. Split across 10 text
    # blocks to mimic streaming.
    chunk = "lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 250  # ~14K chars
    messages = [_assistant_msg(_kg_search_block())]
    for i in range(10):
        messages.append(_assistant_msg(_text_block(f"part {i}: {chunk}")))
    text, complete = srv._rl_extract_answer_window(messages, 0, 0)
    assert complete is True
    threshold_chars = srv._RL_MONITOR_ANSWER_THRESHOLD_TOKENS * 4
    assert len(text) == threshold_chars  # exactly truncated to threshold


def test_synthesized_transcript_with_human_followups_still_accumulates():
    """Spread the same 30k tokens across blocks with human follow-ups
    between them. V52-N: still fires."""
    chunk = "lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 250
    messages = [_assistant_msg(_kg_search_block())]
    for i in range(10):
        messages.append(_assistant_msg(_text_block(f"part {i}: {chunk}")))
        messages.append(_user_human_msg(f"continue {i}"))
    text, complete = srv._rl_extract_answer_window(messages, 0, 0)
    assert complete is True
    # Confirm we got past the early parts.
    assert "part 0" in text


# ----------------------------------------------------------------------
# 5. Sentinel-file-triggered flush (force-fire on sub-threshold answer)
# ----------------------------------------------------------------------


def test_sentinel_file_forces_monitor_to_fire_with_partial_answer(tmp_path):
    """When the sentinel exists, the monitor treats whatever was
    extracted (even sub-threshold) as complete=True. This is the
    PreCompact branch."""
    saved_env = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    try:
        # Drop the sentinel.
        sentinel = tmp_path / srv._RL_MONITOR_FORCE_FLUSH_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("now")

        # Sanity: the monitor's check sees it.
        assert srv._rl_check_force_flush() is True

        # Sub-threshold answer.
        messages = [
            _assistant_msg(_kg_search_block(), _text_block("tiny answer")),
        ]
        text, complete = srv._rl_extract_answer_window(messages, 0, 0)
        # The extractor itself doesn't know about the sentinel — it
        # returns complete=False here (below threshold). The monitor's
        # outer loop is what flips this to True when the sentinel is
        # present.
        assert "tiny answer" in text
        assert complete is False

        # That logic is in _rl_answer_monitor's polling loop.  Cleanup
        # contract:
        srv._rl_clear_force_flush()
        assert srv._rl_check_force_flush() is False
    finally:
        if saved_env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved_env


# ----------------------------------------------------------------------
# 6. Pre-Compact hook drops the sentinel — static test on hook source
# ----------------------------------------------------------------------


def test_pre_compact_hook_sh_drops_sentinel():
    """The PreCompact hook .sh sibling must drop the sentinel file at
    the right path. Static-content check: assert the hook source
    contains the sentinel path string."""
    repo_root = Path(__file__).resolve().parent.parent
    hook = repo_root / "templates" / "hooks" / "pre-compact-save.sh"
    if not hook.exists():
        pytest.skip("pre-compact-save.sh not present in this checkout")
    src = hook.read_text()
    assert "rl_monitors_force_flush.flag" in src, (
        "PreCompact .sh hook does not drop the V52-N sentinel; "
        "in-flight citation monitors will lose their accumulated answer "
        "on every compaction."
    )


def test_pre_compact_hook_ps1_drops_sentinel():
    """Cross-OS parity: the .ps1 sibling must do the same. Audit lesson
    from v0.2.49: every templates/scripts AND templates/hooks .sh
    needs a .ps1 sibling at PR time, not audit time."""
    repo_root = Path(__file__).resolve().parent.parent
    hook = repo_root / "templates" / "hooks" / "pre-compact-save.ps1"
    if not hook.exists():
        pytest.skip("pre-compact-save.ps1 not present in this checkout")
    src = hook.read_text()
    assert "rl_monitors_force_flush.flag" in src, (
        "PreCompact .ps1 hook does not drop the V52-N sentinel; "
        "native-Windows users would silently bypass the flush mechanism."
    )
