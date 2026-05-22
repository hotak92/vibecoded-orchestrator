#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Keyword-based agent/skill suggester for the UserPromptSubmit hook.

Reads a user prompt from stdin, walks the project's `.claude/agents/*.md`
and `.claude/skills/*/SKILL.md` files, parses their YAML frontmatter for a
`keywords:` list, and prints a short sentence-cased suggestion when any
keyword matches the prompt (case-sensitive whole-word).

The suggestion shape is:

    You might want to use this agent: foo
    You might want to use these agents: foo, bar
    You might want to use this skill: baz
    You might want to use these skills: baz, qux

Singular/plural agreement is exact. Agent matches and skill matches are
emitted on separate lines (one line per category). When nothing matches
the script is silent (empty stdout). It always exits 0.

Filesystem contract: the hook side knows NOTHING about the launcher DB
or any `.disabled/` mechanism. A file's presence under `.claude/agents/`
or `.claude/skills/<name>/SKILL.md` is the only signal — disabled files
get moved to a SIBLING `.claude/agents.disabled/` / `.claude/skills.disabled/`
directory by the launcher, so they naturally fall outside these globs.

Cross-OS:
- pathlib for all path manipulation (no forward-slash assumptions)
- inline frontmatter parser (no PyYAML dependency)
- Python 3.8+ stdlib only

Project root resolution:
1. `$CLAUDE_PROJECT_DIR` env var (set by Claude Code at hook fire time)
2. `os.getcwd()` fallback
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _read_frontmatter(text: str) -> str | None:
    """Return the body of a top `---`-delimited YAML block, or None.

    Tolerant of: trailing whitespace on the delimiter line, a leading BOM,
    a leading blank line. Returns None if the file doesn't open with `---`
    on the first non-empty line.
    """
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    # Split into lines preserving order, drop leading blank lines.
    lines = text.splitlines()
    # Find the first non-empty line.
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines):
        return None
    if lines[start].strip() != "---":
        return None
    # Find the closing delimiter.
    for end in range(start + 1, len(lines)):
        if lines[end].strip() == "---":
            return "\n".join(lines[start + 1 : end])
    return None


# Matches either `keywords:` (block form, items on following indented lines)
# or `keywords: [...]` (inline list).
_KEYWORDS_LINE = re.compile(r"^keywords\s*:\s*(.*)$")


def _strip_quotes(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _split_inline_list(payload: str) -> list[str]:
    """Split `[a, "b c", d]` into ['a', 'b c', 'd'].

    Honors single/double-quoted entries (so commas inside a quoted entry
    don't split). Empty entries are dropped.
    """
    inner = payload.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ",":
            token = "".join(buf).strip()
            if token:
                items.append(_strip_quotes(token))
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(_strip_quotes(tail))
    return items


def parse_keywords(frontmatter: str) -> list[str]:
    """Extract a `keywords:` list from a YAML frontmatter body.

    Supports BOTH inline form `keywords: [a, "b c", d]` and block form

        keywords:
          - a
          - "b c"
          - d

    Returns [] if no `keywords:` key is found or the list is empty.
    Malformed input → return whatever parsed cleanly (never raise).
    """
    lines = frontmatter.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Don't match `keywords:` if it appears under a nested key — we only
        # accept the keyword list at the top level. A nested key is any line
        # whose first non-space character is at a deeper indent than the
        # top-level keys, which by YAML convention is column 0. So enforce
        # column-0 here.
        if line and line[0] not in (" ", "\t"):
            m = _KEYWORDS_LINE.match(line)
            if m:
                payload = m.group(1).strip()
                if payload:
                    # Inline list form.
                    return _split_inline_list(payload)
                # Block list form: subsequent indented `- item` lines.
                items: list[str] = []
                j = i + 1
                while j < len(lines):
                    next_line = lines[j]
                    if next_line.strip() == "":
                        j += 1
                        continue
                    # Block list items must be indented and start with `-`.
                    stripped = next_line.lstrip()
                    if not stripped.startswith("-"):
                        break
                    # Must be indented (the `-` cannot be at column 0 for a
                    # top-level key's block list).
                    if next_line == stripped:
                        break
                    item = stripped[1:].strip()
                    if item:
                        items.append(_strip_quotes(item))
                    j += 1
                return items
        i += 1
    return []


# ---------------------------------------------------------------------------
# Frontmatter `name:` extraction (display name for the suggestion message)
# ---------------------------------------------------------------------------


_NAME_LINE = re.compile(r"^name\s*:\s*(.*?)\s*$")


def parse_name(frontmatter: str, fallback: str) -> str:
    """Return the top-level `name:` value, or `fallback` if absent."""
    for line in frontmatter.splitlines():
        if line and line[0] not in (" ", "\t"):
            m = _NAME_LINE.match(line)
            if m:
                value = _strip_quotes(m.group(1))
                if value:
                    return value
    return fallback


# ---------------------------------------------------------------------------
# Keyword matching (case-sensitive, whole-word)
# ---------------------------------------------------------------------------


# A word character per the matching contract: ASCII letter, digit, or
# underscore. This is the same alphabet Python's \w defines in ASCII mode.
_WORDCHAR = re.compile(r"[A-Za-z0-9_]")


def matches_prompt(keyword: str, prompt: str) -> bool:
    """Return True if `keyword` appears in `prompt` with whole-word boundaries.

    Rules:
    - Case-sensitive: `UI` matches `UI` but NOT `ui` or `Ui`.
    - Whole-word: the character immediately before the match (if any) and
      the character immediately after (if any) MUST NOT be `[A-Za-z0-9_]`.
      So `UI` matches `the UI is broken` but NOT `GUIDE`, `UIComponent`,
      `myUI`, `UIs`, etc.
    - Multi-word keywords match as literal substrings with the same
      boundary rule at each end.
    """
    if not keyword:
        return False
    klen = len(keyword)
    start = 0
    while True:
        idx = prompt.find(keyword, start)
        if idx < 0:
            return False
        # Left boundary check.
        left_ok = idx == 0 or not _WORDCHAR.match(prompt[idx - 1])
        # Right boundary check.
        end_idx = idx + klen
        right_ok = end_idx == len(prompt) or not _WORDCHAR.match(prompt[end_idx])
        if left_ok and right_ok:
            return True
        start = idx + 1


def any_match(keywords: Iterable[str], prompt: str) -> bool:
    return any(matches_prompt(k, prompt) for k in keywords)


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    return Path(os.getcwd())


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def collect_agents(root: Path) -> list[tuple[str, list[str]]]:
    """Return [(display_name, keywords), ...] for every agent file with keywords."""
    agents_dir = root / ".claude" / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[tuple[str, list[str]]] = []
    try:
        candidates = sorted(p for p in agents_dir.glob("*.md") if p.is_file())
    except OSError:
        return []
    for path in candidates:
        text = _read_file(path)
        if text is None:
            continue
        fm = _read_frontmatter(text)
        if fm is None:
            continue
        try:
            keywords = parse_keywords(fm)
        except Exception:
            continue
        if not keywords:
            continue
        display = parse_name(fm, fallback=path.stem)
        out.append((display, keywords))
    return out


def collect_skills(root: Path) -> list[tuple[str, list[str]]]:
    """Return [(display_name, keywords), ...] for every SKILL.md with keywords."""
    skills_dir = root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[tuple[str, list[str]]] = []
    try:
        subdirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    for sub in subdirs:
        skill_file = sub / "SKILL.md"
        if not skill_file.is_file():
            continue
        text = _read_file(skill_file)
        if text is None:
            continue
        fm = _read_frontmatter(text)
        if fm is None:
            continue
        try:
            keywords = parse_keywords(fm)
        except Exception:
            continue
        if not keywords:
            continue
        display = parse_name(fm, fallback=sub.name)
        out.append((display, keywords))
    return out


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_suggestion(matched_agents: list[str], matched_skills: list[str]) -> str:
    """Format the sentence-cased suggestion message.

    Singular/plural agreement; no cap on match count. Returns "" when
    both lists are empty.
    """
    lines: list[str] = []
    if matched_agents:
        if len(matched_agents) == 1:
            lines.append(f"You might want to use this agent: {matched_agents[0]}")
        else:
            joined = ", ".join(matched_agents)
            lines.append(f"You might want to use these agents: {joined}")
    if matched_skills:
        if len(matched_skills) == 1:
            lines.append(f"You might want to use this skill: {matched_skills[0]}")
        else:
            joined = ", ".join(matched_skills)
            lines.append(f"You might want to use these skills: {joined}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        prompt = sys.stdin.read()
    except Exception:
        return 0
    if not prompt or not prompt.strip():
        return 0
    root = _project_root()
    try:
        agents = collect_agents(root)
        skills = collect_skills(root)
    except Exception:
        return 0

    matched_agents: list[str] = []
    for name, keywords in agents:
        if any_match(keywords, prompt):
            matched_agents.append(name)
    matched_skills: list[str] = []
    for name, keywords in skills:
        if any_match(keywords, prompt):
            matched_skills.append(name)

    message = format_suggestion(matched_agents, matched_skills)
    if message:
        sys.stdout.write(message + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Hook contract: never block a prompt.
        sys.exit(0)
