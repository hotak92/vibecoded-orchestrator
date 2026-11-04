# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""The env marker for a Claude Code session that keeps NO transcript.

A citation label is computed at turn end by reading the answer the model
wrote in the chat's transcript (``rl_drain_citations``). A headless
``claude -p --no-session-persistence`` run writes no transcript, so a
citation ctx staged for one of its retrievals can never be scored: the Stop
/ SessionEnd drains log it as "transcript not found" and it waits a week
for the abandoned-file collector (1-8 MB each). VCO's post-commit KG
reviewer is such a run, and it runs WITH the project's hooks and MCP
servers, so on a busy project it was most of the ``rl_pending`` backlog.

The process that starts such a session sets :data:`NO_TRANSCRIPT_ENV` in
the child's env; Claude Code passes its env on to the hooks and MCP servers
it spawns, and ``citation_pending.stage_pending`` (the one staging home)
then writes nothing. Only the citation file is skipped: the retrieval's own
RL event is still emitted, so its training log is collected as always.
"""

from __future__ import annotations

import os

#: Set to ``1`` in the env of a Claude Code session that keeps no transcript.
NO_TRANSCRIPT_ENV = "VCO_RL_NO_TRANSCRIPT"


def session_keeps_no_transcript() -> bool:
    """True when this process runs inside a session marked as keeping no
    transcript (:data:`NO_TRANSCRIPT_ENV` is ``1``)."""
    return (os.environ.get(NO_TRANSCRIPT_ENV) or "").strip() == "1"
