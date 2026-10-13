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

* ``agent_id`` is sanitised with the allow-list ``[A-Za-z0-9-]`` — the
  session charset MINUS the underscore (v0.2.102 W6-C2, review R1 C1); a
  hostile / odd agent id (including one carrying ``_``) collapses to the
  literal ``default`` component — its OWN bucket, never the main agent's
  store and never unsanitised bytes in a file name.
* the composition is UNAMBIGUOUS: the agent component never contains
  ``_``, so a composite decodes uniquely (agent = the text after the last
  ``_``, session = the text before the final ``__``). With ``_`` allowed in
  both components, ``("a__b", "c")`` and ``("a", "b__c")`` composed the
  same key. Real Claude Code agent ids are hex (``acb0a3ac69f7894db``), so
  every real key is byte-identical to the W6-C1 encoding. One residual is
  inherent to keeping the main agent's key equal to the bare session id
  (the byte-identical contract): a MAIN key ``<sid>`` can equal a composite
  only if that session id itself contains ``__`` — Claude Code issues UUID
  session ids (hex + single hyphens), and the parity test pins that a
  UUID-shaped id never contains the separator.
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

#: Separator between the session and agent components. It stays inside the
#: session allow-list, so the composite is a single safe filename component
#: (every downstream store re-checks ``[A-Za-z0-9_-]``); it is unambiguous
#: because the AGENT component may not contain ``_`` (see the module
#: docstring). MUST MATCH session-id.sh / session-id.ps1.
AGENT_KEY_SEP = "__"

#: The agent component's allow-list: the session charset WITHOUT ``_`` —
#: what makes :func:`agent_state_key` decodable (review R1 C1).
_AGENT_ID_ALLOWED_RE = re.compile(r"^[A-Za-z0-9-]+$")


def sanitize_agent_id(raw: str) -> str:
    """Sanitise an ``agent_id``: allow-list ``[A-Za-z0-9-]`` (the session
    charset minus ``_``, so the composite key is decodable); anything else
    → ``"default"``; empty stays empty.

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


def split_agent_state_key(key: str) -> tuple[str, str]:
    """Inverse of :func:`agent_state_key` for a composite: ``(sid, agent)``;
    a key with no agent component returns ``(key, "")``. Exists so the
    decodability the encoding promises is executable (and pinned by tests),
    not just asserted in a comment."""
    head, sep, agent = key.rpartition(AGENT_KEY_SEP)
    if not sep or not agent or "_" in agent or not head:
        return key, ""
    return head, agent
