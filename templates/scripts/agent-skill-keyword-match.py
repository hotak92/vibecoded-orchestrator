#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Keyword-based agent/skill suggester for the UserPromptSubmit hook.

Reads a user prompt from stdin, walks the project's `.claude/agents/*.md`
and `.claude/skills/*/SKILL.md` files, parses their YAML frontmatter for a
`keywords:` list, and prints a short sentence-cased suggestion when any
keyword matches the prompt (case-sensitive whole-word).

The suggestion shape is a bullet list with one line per match, optionally
followed by a short `short_desc:` hint after an em-dash separator:

    You might want to use this agent:
    - foo — short scope hint from foo's frontmatter

    You might want to use these skills:
    - baz — short scope hint
    - qux — another short scope hint

Singular/plural agreement is exact ("this agent" vs "these agents"). Agent
matches and skill matches are emitted as separate bullet groups, with a
blank line between groups when both are present. When nothing matches the
script is silent (empty stdout). It always exits 0.

An item without a `short_desc:` field renders as just its name (no em-dash,
no hint) — the hook degrades gracefully on older catalogs.

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

import argparse
import os
import re
import sys
import tempfile
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
_SHORT_DESC_LINE = re.compile(r"^short_desc\s*:\s*(.*?)\s*$")


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


def parse_short_desc(frontmatter: str) -> str:
    """Return the top-level `short_desc:` value, or "" if absent."""
    for line in frontmatter.splitlines():
        if line and line[0] not in (" ", "\t"):
            m = _SHORT_DESC_LINE.match(line)
            if m:
                value = _strip_quotes(m.group(1))
                return value
    return ""


# ---------------------------------------------------------------------------
# Keyword matching (case-insensitive, whole-word)
# ---------------------------------------------------------------------------


# A word character per the matching contract: ASCII letter, digit, or
# underscore. This is the same alphabet Python's \w defines in ASCII mode.
_WORDCHAR = re.compile(r"[A-Za-z0-9_]")


def matches_prompt(keyword: str, prompt: str) -> bool:
    """Return True if `keyword` appears in `prompt` with whole-word boundaries.

    Rules:
    - **Case-insensitive** (v0.2.29): `UI` matches `UI`, `ui`, and `Ui`.
      Pre-v0.2.29 this was case-sensitive, but user prompts come in
      arbitrary casing — case-sensitive matching crippled most realistic
      matches (e.g. `keywords: [UI design]` not firing on "make me a nice
      ui for this"). Lower-casing both sides recovers the common case.
    - Whole-word: the character immediately before the match (if any) and
      the character immediately after (if any) MUST NOT be `[A-Za-z0-9_]`.
      So `UI` matches `the UI is broken` but NOT `GUIDE`, `UIComponent`,
      `myUI`, `UIs`, etc.
    - Multi-word keywords match as literal substrings with the same
      boundary rule at each end.
    """
    if not keyword:
        return False
    # Case-insensitive: lowercase both sides. We still scan with .find()
    # for O(n) performance vs regex compilation per keyword.
    klow = keyword.lower()
    plow = prompt.lower()
    klen = len(klow)
    start = 0
    while True:
        idx = plow.find(klow, start)
        if idx < 0:
            return False
        # Boundary check uses the ORIGINAL `prompt` so non-ASCII chars
        # are still treated as non-word-chars by `_WORDCHAR` (ASCII-only
        # regex). The lowercase comparison only changed the match search.
        left_ok = idx == 0 or not _WORDCHAR.match(prompt[idx - 1])
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


# Filenames to skip when walking the agents/skills directories. v0.2.29:
# defensive against users who drop a README.md alongside the .md agents
# (matches the v0.2.22 fix that filtered README from the launcher's
# populate path). Compared case-insensitively against the bare stem.
_SKIP_STEMS = {"readme"}


def collect_agents(root: Path) -> list[tuple[str, list[str], str]]:
    """Return [(display_name, keywords, short_desc), ...] for every agent file with keywords."""
    agents_dir = root / ".claude" / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[tuple[str, list[str], str]] = []
    try:
        candidates = sorted(
            p for p in agents_dir.glob("*.md")
            if p.is_file() and p.stem.lower() not in _SKIP_STEMS
        )
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
        short_desc = parse_short_desc(fm)
        out.append((display, keywords, short_desc))
    return out


def collect_skills(root: Path) -> list[tuple[str, list[str], str]]:
    """Return [(display_name, keywords, short_desc), ...] for every SKILL.md with keywords."""
    skills_dir = root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[tuple[str, list[str], str]] = []
    try:
        # v0.2.29: skip a README-named subdirectory (defensive — matches
        # the v0.2.22 launcher-populate convention). A skill is a directory
        # containing SKILL.md, so a `skills/README.md` flat file is already
        # filtered by the `is_dir()` check below; the case to guard against
        # is `skills/readme/SKILL.md` (unusual but possible).
        subdirs = sorted(
            p for p in skills_dir.iterdir()
            if p.is_dir() and p.name.lower() not in _SKIP_STEMS
        )
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
        short_desc = parse_short_desc(fm)
        out.append((display, keywords, short_desc))
    return out


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _bullet(name: str, short_desc: str) -> str:
    """Format one bullet line. `- name — hint` if hint present, else `- name`."""
    if short_desc:
        return f"- {name} — {short_desc}"
    return f"- {name}"


def format_suggestion(
    matched_agents: list[tuple[str, str]],
    matched_skills: list[tuple[str, str]],
) -> str:
    """Format the bullet-list suggestion message.

    Inputs are lists of (display_name, short_desc) tuples. Singular/plural
    agreement on the header line ("this agent" vs "these agents"). Each
    match renders on its own line as `- name — short_desc` (or `- name`
    if no short_desc). A blank line separates the agents and skills groups
    when both are present. Returns "" when both lists are empty.
    """
    blocks: list[str] = []
    if matched_agents:
        header = "this agent" if len(matched_agents) == 1 else "these agents"
        lines = [f"You might want to use {header}:"]
        for name, sd in matched_agents:
            lines.append(_bullet(name, sd))
        blocks.append("\n".join(lines))
    if matched_skills:
        header = "this skill" if len(matched_skills) == 1 else "these skills"
        lines = [f"You might want to use {header}:"]
        for name, sd in matched_skills:
            lines.append(_bullet(name, sd))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Per-session dedup (v0.2.29)
# ---------------------------------------------------------------------------
#
# Without dedup, every user prompt during a session that contains a
# previously-matched keyword (or a keyword for an already-suggested item)
# re-emits the same suggestion line. This wastes context tokens and
# trains the user to ignore the additionalContext block. v0.2.29 adds
# per-session dedup keyed on (agent|skill, display_name): once an item
# has been suggested in this session, it won't be suggested again until
# compaction resets the state.
#
# State location: `<project_root>/.claude/state/keyword_suggest_<session_id>.txt`
# — same pattern as `pre-edit-context-inject.sh`'s `seen_kg_titles_*.txt`.
# Project-local, gitignored (.claude/state/ is in the orchestrator's
# .gitignore), survives reboot, bounded by the GC pass below. One line
# per already-suggested item, prefixed with `a:` (agent) or `s:` (skill)
# to avoid namespace collisions between an agent and a skill that share
# a display name.
#
# Compaction reset: the PostCompact hook (`post-compact.sh`) deletes the
# file so a new "what's available" surface fires after the user starts
# a fresh logical task. Without `--session-id`, dedup is disabled and
# the matcher emits every match every time (back-compat for direct
# invocations e.g. from tests).


def _dedup_dir() -> Path:
    """Resolve the project-local `.claude/state/` directory.

    Resolution chain mirrors the rest of this script (env-first, cwd
    fallback). The directory is created lazily by `_persist_seen`.

    TEST OVERRIDE: when `$VCT_KEYWORD_DEDUP_DIR` is set (any non-empty
    value), it overrides the resolved path entirely. This lets tests
    point dedup state at a tmp_path without having to fake
    `CLAUDE_PROJECT_DIR` (which controls many other things).
    """
    override = os.environ.get("VCT_KEYWORD_DEDUP_DIR", "").strip()
    if override:
        return Path(override)
    return _project_root() / ".claude" / "state"


def _dedup_file(session_id: str) -> Path | None:
    sid = session_id.strip()
    if not sid:
        return None
    # Defensive: reject anything that isn't UUID-ish / safe slug to
    # prevent path traversal. The session_id from Claude Code is always
    # a UUID, so this allow-list is conservative.
    if not re.match(r"^[A-Za-z0-9._-]+$", sid):
        return None
    return _dedup_dir() / f"keyword_suggest_{sid}.txt"


def _load_seen(session_id: str) -> set[str]:
    """Return the set of already-suggested entries for this session.

    Each entry is a string of the form `a:<name>` or `s:<name>`. Returns
    an empty set on any error path (file missing, unreadable, etc.).
    """
    f = _dedup_file(session_id)
    if f is None or not f.is_file():
        return set()
    try:
        return {line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def _persist_seen(session_id: str, additions: Iterable[str]) -> None:
    """Append the given entries to the per-session dedup file.

    Best-effort. Any failure (permission denied, mkdir failure) is
    silently swallowed — the hook MUST never raise. Worst case: a
    suggestion fires twice in the same session.
    """
    additions_list = [a for a in additions if a]
    if not additions_list:
        return
    f = _dedup_file(session_id)
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            for entry in additions_list:
                fh.write(entry + "\n")
    except OSError:
        return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--session-id", default="", help="Per-session dedup key")
    # v0.2.33 (SubagentStart): when the consuming subagent doesn't have
    # the `Agent`/`Task` tool, suggesting AGENTS to it is pointless —
    # it has no way to spawn one. The bash/ps1 wrapper checks the
    # subagent's tool list and passes --skills-only when Agent/Task
    # is absent. Default off → back-compat for the existing
    # UserPromptSubmit wrapper, which always wants both groups.
    p.add_argument("--skills-only", action="store_true",
                   help="Suppress agent suggestions; emit only skills.")
    # Tolerate unknown args silently — the hook contract is "never block".
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        prompt = sys.stdin.read()
    except Exception:
        return 0
    if not prompt or not prompt.strip():
        return 0
    root = _project_root()
    try:
        agents = collect_agents(root) if not args.skills_only else []
        skills = collect_skills(root)
    except Exception:
        return 0

    matched_agents: list[tuple[str, str]] = []
    for name, keywords, short_desc in agents:
        if any_match(keywords, prompt):
            matched_agents.append((name, short_desc))
    matched_skills: list[tuple[str, str]] = []
    for name, keywords, short_desc in skills:
        if any_match(keywords, prompt):
            matched_skills.append((name, short_desc))

    # Filter out already-suggested entries (per-session dedup). Key is
    # the display NAME — PR #259's short_desc may change between turns
    # (e.g. user re-edits the agent's frontmatter), but identity is the
    # name. When no session_id is supplied, `seen` is empty → no
    # filtering happens.
    seen = _load_seen(args.session_id)
    matched_agents = [(n, sd) for (n, sd) in matched_agents if f"a:{n}" not in seen]
    matched_skills = [(n, sd) for (n, sd) in matched_skills if f"s:{n}" not in seen]

    message = format_suggestion(matched_agents, matched_skills)
    if message:
        sys.stdout.write(message + "\n")
        # Persist BEFORE returning — if the user CTRL-Cs the next prompt,
        # we still want the dedup state on disk so a future prompt in
        # the same session doesn't re-suggest these.
        _persist_seen(
            args.session_id,
            [f"a:{n}" for (n, _sd) in matched_agents]
            + [f"s:{n}" for (n, _sd) in matched_skills],
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Hook contract: never block a prompt.
        sys.exit(0)
