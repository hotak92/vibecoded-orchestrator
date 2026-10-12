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
import re
from pathlib import PurePosixPath
from typing import Iterable, List, Mapping, Optional, Tuple

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


#: Instruction files the coding agents load as standing prompts, matched by
#: exact file name wherever they sit (Claude Code also loads nested
#: ``CLAUDE.md`` files; Codex reads ``AGENTS.md``; Gemini CLI ``GEMINI.md``).
_INSTRUCTION_FILE_NAMES = frozenset({"CLAUDE.md", "CLAUDE.local.md",
                                     "AGENTS.md", "GEMINI.md"})

#: The Claude Code settings files whose ``hooks`` may carry a ``prompt`` /
#: ``agent`` hook (the hook's prompt text lives inside the JSON).
_SETTINGS_FILE_NAMES = frozenset({"settings.json", "settings.local.json"})

#: A ``prompt`` / ``agent`` hook entry in settings JSON text.
_HOOK_PROMPT_RE = re.compile(r'"type"\s*:\s*"(?:prompt|agent)"')


def _after(dirs: List[str], first: str, second: str) -> int:
    """Index just past the first ``first/second`` pair in ``dirs``, or -1."""
    for i in range(len(dirs) - 1):
        if dirs[i] == first and dirs[i + 1] == second:
            return i + 2
    return -1


def _touches_hook_prompt(tool_input: Optional[Mapping]) -> bool:
    """True when the Edit/Write text -- the new ``content``, or either side
    of an ``old_string`` -> ``new_string`` edit -- holds a ``prompt`` /
    ``agent`` hook entry. Only what the call itself carries is read (no
    file I/O): an edit elsewhere in a settings file that happens to hold a
    prompt hook is not a prompt edit."""
    if not tool_input:
        return False
    for key in ("content", "new_string", "old_string"):
        value = tool_input.get(key)
        if isinstance(value, str) and _HOOK_PROMPT_RE.search(value):
            return True
    return False


def prompt_kind(file_path: str, project_root: str = "",
                tool_input: Optional[Mapping] = None) -> Optional[str]:
    """What kind of prompt ``file_path`` is, or None when it is not one.

    * ``"agent definition"`` -- ``.claude/agents/**/*.md``;
    * ``"slash command"`` -- ``.claude/commands/**/*.md``;
    * ``"skill"`` -- any ``SKILL.md``, and a skill's supporting text files
      (``.claude/skills/<name>/**`` with a prompt-template extension);
    * ``"output style"`` -- ``.claude/output-styles/**/*.md``;
    * ``"instruction file"`` -- ``CLAUDE.md`` / ``CLAUDE.local.md`` /
      ``AGENTS.md`` / ``GEMINI.md`` anywhere,
      ``.github/copilot-instructions.md``,
      ``.github/instructions/**/*.instructions.md``,
      ``.github/chatmodes/**/*.chatmode.md``, ``.cursor/rules/**/*.mdc``;
    * ``"prompt template"`` -- ``*.prompt`` / ``*.prompt.md`` /
      ``*.prompt.txt``, and template files under a ``prompts/`` directory;
    * ``"hook prompt"`` -- ``.claude/settings.json`` /
      ``settings.local.json`` when the Edit/Write itself adds, changes or
      removes a ``"type": "prompt"`` / ``"agent"`` hook (``tool_input``).
      Hook SCRIPTS that assemble a prompt in shell are not detected: no
      cheap, precise test tells them from any other script.

    ``README.md`` is never a prompt.
    """
    parts = _rel_parts(file_path, project_root)
    if not parts:
        return None
    name = parts[-1]
    lower = name.lower()
    dirs = [d.lower() for d in parts[:-1]]
    ext = os.path.splitext(lower)[1]
    if name == "SKILL.md":
        return "skill"
    if name in _INSTRUCTION_FILE_NAMES:
        return "instruction file"
    if lower == "readme.md":
        return None
    if lower.endswith((".prompt", ".prompt.md", ".prompt.txt")):
        return "prompt template"
    if lower in _SETTINGS_FILE_NAMES and dirs[-1:] == [".claude"]:
        return "hook prompt" if _touches_hook_prompt(tool_input) else None
    if dirs[:1] == [".github"]:
        if len(dirs) == 1 and lower == "copilot-instructions.md":
            return "instruction file"
        if dirs[1:2] == ["instructions"] and lower.endswith(".instructions.md"):
            return "instruction file"
        if dirs[1:2] == ["chatmodes"] and lower.endswith(".chatmode.md"):
            return "instruction file"
    if ext == ".mdc" and _after(dirs, ".cursor", "rules") >= 0:
        return "instruction file"
    if ext == ".md":
        if _after(dirs, ".claude", "agents") >= 0:
            return "agent definition"
        if _after(dirs, ".claude", "commands") >= 0:
            return "slash command"
        if _after(dirs, ".claude", "output-styles") >= 0:
            return "output style"
    skill_at = _after(dirs, ".claude", "skills")
    if skill_at >= 0 and len(dirs) > skill_at and ext in _PROMPT_DIR_EXTS:
        return "skill"
    if "prompts" in dirs and ext in _PROMPT_DIR_EXTS:
        return "prompt template"
    return None


def is_prompt_shaped(file_path: str, project_root: str = "",
                     tool_input: Optional[Mapping] = None) -> bool:
    """True when :func:`prompt_kind` names a kind."""
    return prompt_kind(file_path, project_root, tool_input) is not None


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


def nudge_block(file_path: str, project_root: str,
                tool_input: Optional[Mapping] = None) -> str:
    """The ``NOTE:`` block for one prompt-shaped file, or ``""``.

    The header's first field names the file, so the seen-store key is per
    file; the caller passes the block through the session seen-store.
    ``tool_input`` (the Edit/Write input) is what tells a hook-prompt edit
    of ``.claude/settings.json`` from any other settings edit.
    """
    if not file_path:
        return ""
    kind = prompt_kind(file_path, project_root, tool_input)
    if not kind:
        return ""
    agent = enabled_prompt_agent(project_root)
    if not agent:
        return ""
    rel = "/".join(_rel_parts(file_path, project_root))
    lines = [
        f"{NOTE_PREFIX}prompt-shaped file {rel} | agent={agent}",
        f"  This edit changes a prompt ({kind})."
        f" Consider delegating the change to the `{agent}` agent, or applying"
        f" its guidance, before editing it.",
    ]
    if os.path.isfile(os.path.join(project_root, PROMPT_SPECIALIZATION_DOC)):
        lines.append(f"  Reference: {PROMPT_SPECIALIZATION_DOC}")
    return "\n".join(lines) + "\n"
