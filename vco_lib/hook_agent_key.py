# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-C1 (F1/F2/F3/F11) — the ONE per-agent hook-state key.

Harness fact (probe 2026-10-13, Claude Code 2.1.285): hooks fire inside
subagents for every tool call, and those payloads carry ``agent_id`` /
``agent_type`` while ``session_id``, ``prompt_id`` and ``transcript_path``
stay the PARENT's. Every piece of per-session hook state keyed on
``session_id`` alone — the seen-store, the per-turn budget, the code-graph
inject cap, the Build-Anchor reads ledger — was therefore shared by the
main chat and ALL of its subagents: a subagent started with a fresh context
but was suppressed by blocks the parent saw, and the parent was suppressed
by blocks only a subagent saw.

This module is the Python half of the fix; the shell halves are
``templates/hooks/_lib/session-id.sh::vco_hook_agent_key_from_ids`` and its
``.ps1`` sibling ``Get-VcoAgentKeyFromIds`` (parity-pinned by
``tests/test_v02102_w6c1_agent_keyed_hook_state.py``). All three compose
the SAME key:

* main agent (no / empty ``agent_id``)      → ``<sid>``
* subagent (non-empty ``agent_id``)         → ``<sid>__<agent_id>``

Policy decisions baked into the composition (identical in all three legs):

* ``agent_id`` is sanitised with the SAME allow-list charset as the session
  id (``[A-Za-z0-9_-]``); a hostile / odd agent id collapses to the literal
  ``default`` component — its OWN bucket, never the main agent's store and
  never unsanitised bytes in a file name.
* an UNTRUSTWORTHY session id (``""`` / ``"default"``) passes through
  unchanged: the composite would otherwise turn "no trustworthy key" into a
  shared ``default__<agent>`` bucket across sessions, re-creating exactly
  the cross-session bleed the seen-store's inject-blind guard exists to
  prevent. A subagent in an unkeyable session injects blind, like the main
  agent in the same situation.

The caller supplies the session id ALREADY sanitised
(:func:`vco_lib.inject_intent.sanitize_session_id` at every current
call-site); the agent id may be raw.
"""
from __future__ import annotations

import re

#: Separator between the session and agent components. Underscores are
#: inside both ids' allow-list, so the composite stays a single safe
#: filename component. MUST MATCH session-id.sh / session-id.ps1.
AGENT_KEY_SEP = "__"

_AGENT_ID_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def sanitize_agent_id(raw: str) -> str:
    """Sanitise an ``agent_id`` exactly like a session id: allow-list
    ``[A-Za-z0-9_-]``; anything else → ``"default"``; empty stays empty.

    MUST MATCH ``_lib/session-id.sh::vco_hook_sanitize_agent_id`` and its
    ``.ps1`` sibling ``Get-VcoSanitizedAgentId`` (same charset, same
    fallback, same empty policy)."""
    if not raw:
        return ""
    return raw if _AGENT_ID_ALLOWED_RE.match(raw) else "default"


def agent_state_key(session_id: str, agent_id: str) -> str:
    """The per-agent state key: ``<sid>`` for the main agent,
    ``<sid>__<agent_id>`` when ``agent_id`` is non-empty.

    ``session_id`` must already be sanitised (every call-site passes
    :func:`vco_lib.inject_intent.sanitize_session_id`'s output); an
    untrustworthy session (``""`` / ``"default"``) is returned UNCHANGED so
    downstream untrustworthy-id guards (empty store path, inject blind)
    keep firing — see the module docstring. ``agent_id`` may be raw.

    MUST MATCH ``_lib/session-id.sh::vco_hook_agent_key_from_ids`` and
    ``session-id.ps1``'s ``Get-VcoAgentKeyFromIds``."""
    agent = sanitize_agent_id(agent_id or "")
    if not agent or session_id in ("", "default"):
        return session_id
    return f"{session_id}{AGENT_KEY_SEP}{agent}"
