# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Before-edit nudge toward the prompt-authoring agent (v0.2.102 W6-N F-8).

Owner ruling 2026-10-12: the post-file-edit hook's "Run /workflow-optimizer"
line came AFTER the edit (whatever it would have done was already done) and
named a skill that never shipped. The useful moment is BEFORE an agent edits a
prompt-shaped file, and the useful pointer is an agent the project actually
has. This module is the one home for both decisions; the pre-edit / pre-write
context router (``hook_context_router.py``) renders the result as a ``NOTE:``
block, which the shared seen-store dedupes per session per file exactly like
the KG/CODE blocks.

Pure stdlib, no I/O beyond ``os.path`` existence checks.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Iterable, List, Optional, Tuple

#: The prompt-authoring agents to suggest, in preference order; the first one
#: ENABLED in the project wins (an agent the user switched off lives in
#: ``.claude/agents.disabled/`` and is never suggested). ``prompt-engineer`` is
#: the agent the owner named; v0.2.101's catalogue right-sizing retired it from
#: the shipped set, and its job (writing agent definitions, skills and prompts)
#: is ``agent-author``'s — kept here so the nudge stays useful on a current
#: install while still honouring a project that kept ``prompt-engineer``.
PROMPT_AGENT_CANDIDATES: Tuple[str, ...] = ("prompt-engineer", "agent-author")

#: Reference doc the retired prompt-engineer's depth moved into; mentioned only
#: when the project has it.
PROMPT_SPECIALIZATION_DOC = ".claude/specializations/fields/prompt-engineering.md"

#: Extensions a file under a ``prompts/`` directory may carry to count as a
#: prompt template.
_PROMPT_DIR_EXTS = frozenset({".md", ".txt", ".j2", ".jinja", ".jinja2",
                              ".tmpl", ".prompt", ".mustache"})

#: Header prefix of the block (the seen-store filters in the router,
#: ``_lib/seen-store.sh`` and ``_lib/seen-store.ps1`` all parse it).
NOTE_PREFIX = "NOTE: "


def _rel_parts(file_path: str, project_root: str) -> Tuple[str, ...]:
    path = file_path.replace("\\", "/")
    root = (project_root or "").replace("\\", "/").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root) + 1:]
    return tuple(p for p in PurePosixPath(path).parts if p not in ("/", ""))


def is_prompt_shaped(file_path: str, project_root: str = "") -> bool:
    """True for agent definitions, skills and prompt templates.

    * ``.claude/agents/**/*.md`` and ``.claude/commands/**/*.md`` (slash
      commands are prompt templates);
    * any ``SKILL.md`` (the skill file name is the convention, wherever it
      lives — ``.claude/skills/**`` and shipped skill sources alike);
    * agent definition sources: a ``.md`` under an ``agents/`` directory
      that sits under a ``templates/`` directory (``README.md`` excluded);
    * ``*.prompt`` / ``*.prompt.md`` / ``*.prompt.txt`` files, and template
      files under a ``prompts/`` directory.
    """
    parts = _rel_parts(file_path, project_root)
    if not parts:
        return False
    name = parts[-1]
    lower = name.lower()
    dirs = [d.lower() for d in parts[:-1]]
    if name == "SKILL.md":
        return True
    if lower.endswith((".prompt", ".prompt.md", ".prompt.txt")):
        return True
    ext = os.path.splitext(lower)[1]
    if "prompts" in dirs and ext in _PROMPT_DIR_EXTS:
        return True
    if ext != ".md" or lower == "readme.md":
        return False
    for i, d in enumerate(dirs):
        nxt = dirs[i + 1] if i + 1 < len(dirs) else ""
        if d == ".claude" and nxt in ("agents", "commands"):
            return True
        if d == "agents" and "templates" in dirs[:i]:
            return True
    return False


def _agent_dirs(project_root: str) -> Iterable[str]:
    base = os.path.join(project_root, ".claude", "agents")
    yield base
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return
    for entry in entries:
        sub = os.path.join(base, entry)
        if not entry.startswith(("_", ".")) and os.path.isdir(sub):
            yield sub


def enabled_prompt_agent(project_root: str,
                         candidates: Tuple[str, ...] = PROMPT_AGENT_CANDIDATES
                         ) -> Optional[str]:
    """The first candidate whose definition is ENABLED in the project
    (``.claude/agents/<name>.md`` or one non-archive subdirectory down), else
    None. A disabled agent is moved to ``.claude/agents.disabled/`` by the
    launcher and is therefore never found here."""
    if not project_root:
        return None
    dirs: List[str] = list(_agent_dirs(project_root))
    for name in candidates:
        for d in dirs:
            if os.path.isfile(os.path.join(d, f"{name}.md")):
                return name
    return None


def nudge_block(file_path: str, project_root: str) -> str:
    """The ``NOTE:`` block for one prompt-shaped file, or ``""``.

    The header's first field names the file, so the seen-store key is per
    file; the caller passes the block through the session seen-store.
    """
    if not file_path or not is_prompt_shaped(file_path, project_root):
        return ""
    agent = enabled_prompt_agent(project_root)
    if not agent:
        return ""
    rel = "/".join(_rel_parts(file_path, project_root))
    lines = [
        f"{NOTE_PREFIX}prompt-shaped file {rel} | agent={agent}",
        f"  This file is a prompt (agent definition, skill or prompt template)."
        f" Consider delegating the change to the `{agent}` agent, or applying"
        f" its guidance, before editing it.",
    ]
    if os.path.isfile(os.path.join(project_root, PROMPT_SPECIALIZATION_DOC)):
        lines.append(f"  Reference: {PROMPT_SPECIALIZATION_DOC}")
    return "\n".join(lines) + "\n"
