# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — `VCT_AGENT_ID` / `VCT_BRIEF_TOOL_USE_ID` do not leak between tests.

The context router writes both straight into ``os.environ`` on every run
(``hook_context_router`` — one run per process in production), exactly like
``VCT_TRANSCRIPT_PATH``. A test that drives ``router.main`` in-process leaves
them set; every later ``citation_pending.stage_pending`` of the suite then
tags its pending file with that test's agent id / brief tool-use id instead
of the real caller's. ``tests/conftest.py::_drop_agent_run_env`` clears both
before and after every test; the second test below fails without it.
"""
from __future__ import annotations

import os


def test_a_prior_test_leaves_the_agent_env_set():
    """Stands in for any router-driving test: the router's env step writes
    ``VCT_AGENT_ID`` (and the agent surface's ``VCT_BRIEF_TOOL_USE_ID``)
    into ``os.environ`` directly — no monkeypatch, nothing restored."""
    os.environ["VCT_AGENT_ID"] = "agent-leak-probe-1"
    os.environ["VCT_BRIEF_TOOL_USE_ID"] = "toolu_leakprobe01"
    assert os.environ.get("VCT_AGENT_ID") == "agent-leak-probe-1"


def test_the_next_test_sees_no_agent_env():
    """The autouse guard must have cleared both keys before this test ran —
    the same contract ``_drop_staging_transcript_env`` pins for
    ``VCT_TRANSCRIPT_PATH``."""
    assert "VCT_AGENT_ID" not in os.environ, (
        "VCT_AGENT_ID leaked from a prior test: conftest's _drop_agent_run_env "
        "guard is missing or not autouse"
    )
    assert "VCT_BRIEF_TOOL_USE_ID" not in os.environ, (
        "VCT_BRIEF_TOOL_USE_ID leaked from a prior test: conftest's "
        "_drop_agent_run_env guard is missing or not autouse"
    )
