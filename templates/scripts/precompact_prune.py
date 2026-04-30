#!/usr/bin/env python3
"""Post-compact reinjection helper.

Scans the transcript JSONL for the current session, scoped to "since the
last compact" (marker file + JSONL fallback), and produces a short summary
of what the agent has been doing — frequently-edited/written paths, often-
read paths, and bash commands run repeatedly.

The output is consumed by compact-context-reinject.sh, which `cat`s it
into the post-compact context window so the agent doesn't reflexively
re-explore territory it has already covered.

Three buckets:
  1. Edited/written paths   — ≥ EDIT_THRESHOLD successful Edit/Write calls
  2. Read paths             — ≥ READ_THRESHOLD successful Read calls,
                              excluding paths already in bucket 1
                              (Read is implied before Edit/Write)
  3. Bash commands          — ≥ BASH_THRESHOLD identical commands

Path roll-up: counts roll up the directory tree. Each path is reported at
the deepest prefix whose count meets the threshold; shallower prefixes
are also reported when they add at least one leaf not already covered.

Thresholds are env-overridable (PRUNE_EDIT_THRESHOLD, PRUNE_READ_THRESHOLD,
PRUNE_BASH_THRESHOLD).

Last-compact detection: prefer .claude/context/last-compact-marker (epoch
seconds, written by pre-compact-save.sh). If absent, scan the transcript
backwards for the most recent SessionStart matcher=compact event and use
its timestamp. Fall back to whole-transcript if neither works.

Stdin: hook JSON containing {"transcript_path": "/path/to/session.jsonl"}.
Output file: .claude/context/pruned-context-summary.md
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

# Thresholds + caps (env-overridable)
EDIT_THRESHOLD = int(os.environ.get("PRUNE_EDIT_THRESHOLD", "5"))
READ_THRESHOLD = int(os.environ.get("PRUNE_READ_THRESHOLD", "5"))
BASH_THRESHOLD = int(os.environ.get("PRUNE_BASH_THRESHOLD", "4"))
# Caps to keep the reinjected summary short — most recent activity wins
PATH_CAP = int(os.environ.get("PRUNE_PATH_CAP", "20"))
BASH_CAP = int(os.environ.get("PRUNE_BASH_CAP", "20"))

# Tool names we care about
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}
BASH_TOOLS = {"Bash"}


def main() -> None:
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    output_path = project_dir / ".claude" / "context" / "pruned-context-summary.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    transcript_path = _read_transcript_path_from_stdin()
    if not transcript_path or not transcript_path.is_file():
        output_path.write_text("")
        return

    cutoff = _resolve_cutoff(project_dir, transcript_path)
    messages = _load_transcript_since(transcript_path, cutoff)
    if not messages:
        output_path.write_text("")
        return

    edits, reads, bashes, edits_last, reads_last, bashes_last = _collect_tool_uses(messages)

    # Reads imply later Edits, so subtract any path already counted as edited.
    edited_paths = set(edits.keys())
    reads_filtered = Counter({p: c for p, c in reads.items() if p not in edited_paths})
    reads_last_filtered = {p: i for p, i in reads_last.items() if p not in edited_paths}

    edit_report = _cap_by_recency(
        _rollup_paths(edits, EDIT_THRESHOLD), edits_last, PATH_CAP
    )
    read_report = _cap_by_recency(
        _rollup_paths(reads_filtered, READ_THRESHOLD), reads_last_filtered, PATH_CAP
    )
    bash_qualifying = [(cmd, n) for cmd, n in bashes.items() if n >= BASH_THRESHOLD]
    bash_report = _cap_by_recency(bash_qualifying, bashes_last, BASH_CAP)

    if not edit_report and not read_report and not bash_report:
        output_path.write_text("")
        return

    lines = ["## Recent activity (since last compact)", ""]
    if edit_report:
        lines.append("You edited/wrote these paths frequently:")
        for path, n in edit_report:
            lines.append(f"- `{path}` (x{n})")
        lines.append("")
    if read_report:
        lines.append("You read these paths frequently (no edits):")
        for path, n in read_report:
            lines.append(f"- `{path}` (x{n})")
        lines.append("")
    if bash_report:
        lines.append("You frequently ran these bash commands:")
        for cmd, n in bash_report:
            preview = cmd if len(cmd) <= 120 else cmd[:117] + "..."
            lines.append(f"- `{preview}` (x{n})")
        lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n")
    print(
        f"[precompact_prune] {len(edit_report)} edit groups, "
        f"{len(read_report)} read groups, {len(bash_report)} bash groups",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Stdin / transcript loading
# ---------------------------------------------------------------------------

def _read_transcript_path_from_stdin() -> Path | None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return None
    p = data.get("transcript_path")
    return Path(p) if p else None


def _resolve_cutoff(project_dir: Path, transcript_path: Path) -> float:
    """Epoch-seconds cutoff: only messages strictly newer are considered.

    Priority:
      1. .claude/context/last-compact-marker (epoch seconds, plain text)
      2. JSONL scan for most recent compact-related event
      3. 0.0 (whole transcript)
    """
    marker = project_dir / ".claude" / "context" / "last-compact-marker"
    if marker.is_file():
        try:
            return float(marker.read_text().strip())
        except (ValueError, OSError):
            pass

    # Fallback: scan transcript backwards for an event that smells like a
    # post-compact session start. Claude Code emits messages with role/type
    # markers; we look for the most recent one whose payload mentions
    # "compact" in either subtype or a known SessionStart matcher field.
    try:
        last_compact_ts: float | None = None
        with transcript_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _looks_like_compact_event(msg):
                    ts = _parse_ts(msg.get("timestamp"))
                    if ts is not None:
                        last_compact_ts = ts
        if last_compact_ts is not None:
            return last_compact_ts
    except OSError:
        pass

    return 0.0


def _looks_like_compact_event(msg: dict) -> bool:
    if msg.get("type") == "session_start" and "compact" in str(msg).lower():
        return True
    subtype = msg.get("subtype") or msg.get("hookEventName") or ""
    if isinstance(subtype, str) and subtype.lower().startswith("compact"):
        return True
    return False


def _parse_ts(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _load_transcript_since(path: Path, cutoff: float) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(msg.get("timestamp"))
                if cutoff > 0 and ts is not None and ts <= cutoff:
                    continue
                out.append(msg)
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# Tool-use extraction
# ---------------------------------------------------------------------------

def _collect_tool_uses(
    messages: list[dict],
) -> tuple[Counter, Counter, Counter, dict[str, int], dict[str, int], dict[str, int]]:
    """Walk the message stream once, producing three counters + last-seen index maps.

    Returns (edits, reads, bashes, edits_last, reads_last, bashes_last) where
    each `_last` map is `key → message_index_of_last_occurrence`. Used to
    pick the most recent N items when output is capped.
    """
    edits: Counter = Counter()
    reads: Counter = Counter()
    bashes: Counter = Counter()
    edits_last: dict[str, int] = {}
    reads_last: dict[str, int] = {}
    bashes_last: dict[str, int] = {}

    # tool_use_id → (tool_name, tool_input). Built from assistant messages,
    # consumed when matching tool_result blocks.
    tool_use_index: dict[str, tuple[str, dict]] = {}

    for idx, msg in enumerate(messages):
        content = _message_content_blocks(msg)

        # Assistant turn — record tool_use blocks for later matching
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tu_id = block.get("id")
                name = block.get("name")
                tinput = block.get("input") or {}
                if tu_id and name:
                    tool_use_index[tu_id] = (name, tinput)

        # User/tool turn — match results back to tool_use, then record
        # successful invocations
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                continue
            tu_id = block.get("tool_use_id")
            if not tu_id or tu_id not in tool_use_index:
                continue
            name, tinput = tool_use_index[tu_id]

            if name in EDIT_TOOLS:
                path = _input_path(tinput)
                if path:
                    edits[path] += 1
                    edits_last[path] = idx
            elif name in READ_TOOLS:
                path = _input_path(tinput)
                if path:
                    reads[path] += 1
                    reads_last[path] = idx
            elif name in BASH_TOOLS:
                cmd = (tinput.get("command") or "").lstrip()
                if cmd:
                    bashes[cmd] += 1
                    bashes_last[cmd] = idx

    return edits, reads, bashes, edits_last, reads_last, bashes_last


def _message_content_blocks(msg: dict) -> list:
    """Return the list of content blocks for any message shape."""
    inner = msg.get("message")
    if isinstance(inner, dict):
        content = inner.get("content")
    else:
        content = msg.get("content")
    if isinstance(content, list):
        return content
    return []


def _input_path(tinput: dict) -> str | None:
    """Pick the path field used by Read/Edit/Write/MultiEdit/NotebookEdit.

    Normalises to POSIX-style (forward slashes) so the rest of the script
    runs identically on Linux, macOS, and Windows. Windows-style absolute
    paths (`C:\\Users\\foo`) become POSIX-style (`C:/Users/foo`).
    """
    for key in ("file_path", "path", "notebook_path"):
        v = tinput.get(key)
        if isinstance(v, str) and v.strip():
            return _to_posix(v.strip())
    return None


def _to_posix(path: str) -> str:
    """Normalise a path string to POSIX form regardless of host OS.

    Why this exists: the transcript JSONL is portable across machines (a
    repo cloned on Windows then resumed on macOS will have mixed-OS paths).
    We always emit forward-slash paths so `_ancestors()` and roll-up logic
    don't have to fork per-OS.
    """
    if not path:
        return path
    # Windows-style detection: backslash present, or drive letter prefix
    if "\\" in path or (len(path) >= 2 and path[1] == ":"):
        try:
            wp = PureWindowsPath(path)
            return wp.as_posix()
        except (ValueError, OSError):
            return path.replace("\\", "/")
    return path


# ---------------------------------------------------------------------------
# Path roll-up
# ---------------------------------------------------------------------------

def _rollup_paths(counts: Counter, threshold: int) -> list[tuple[str, int]]:
    """Roll up path counts.

    Emission rules:
      * Every leaf with count >= threshold is emitted standalone.
      * Every ancestor prefix whose aggregate count (across ALL descendant
        leaves, including those already emitted standalone) >= threshold
        AND covers at least one leaf not already covered by a deeper
        emitted prefix is emitted with its full aggregate count.

    The "adds a new leaf" rule prevents reporting a chain of nested prefixes
    that all carry the same single high-frequency leaf (`src/`, `src/foo/`,
    `src/foo/bar/` for one file edited many times). The "full aggregate
    count" rule matches the user's spec: `path/` x13 even when `path/to/A`
    x6 is also reported, because the shallower prefix exposes that there's
    additional activity at that level.

    Output sorted by count desc, then path asc.
    """
    if not counts:
        return []

    # 1. Leaves meeting threshold standalone
    qualifying_leaves = {p for p, c in counts.items() if c >= threshold}

    # 2. Per-prefix aggregate over ALL leaves (not just sub-threshold)
    prefix_to_leaves: dict[str, set[str]] = defaultdict(set)
    prefix_aggregate: Counter = Counter()
    for path, n in counts.items():
        for prefix in _ancestors(path):
            prefix_to_leaves[prefix].add(path)
            prefix_aggregate[prefix] += n

    # 3. Walk ancestors deepest-first; emit when they add at least one leaf
    #    not already covered by an emitted (deeper) ancestor or a qualifying
    #    leaf. Already-emitted qualifying leaves count as "covered".
    covered_leaves: set[str] = set(qualifying_leaves)
    emitted_ancestors: list[str] = []
    qualifying_prefixes = sorted(
        (p for p, c in prefix_aggregate.items() if c >= threshold),
        key=lambda p: (-_depth(p), p),
    )
    for prefix in qualifying_prefixes:
        leaves_here = prefix_to_leaves[prefix]
        if leaves_here.issubset(covered_leaves):
            continue
        emitted_ancestors.append(prefix)
        covered_leaves.update(leaves_here)

    out: list[tuple[str, int]] = [(p, counts[p]) for p in qualifying_leaves]
    out.extend((p, prefix_aggregate[p]) for p in emitted_ancestors)

    out.sort(key=lambda kv: (-kv[1], kv[0]))
    return out


def _ancestors(path: str) -> Iterable[str]:
    """Yield every directory prefix of `path`, deepest first.

    Input must already be POSIX-form (forward slashes only). Use `_to_posix`
    upstream if the source can be mixed.

      `/a/b/c.py` → `/a/b/`, `/a/`
      `a/b/c.py`  → `a/b/`, `a/`
      `C:/Users/x/foo.py` → `C:/Users/x/`, `C:/Users/`, `C:/`

    Does not yield the POSIX root (`/`) — too broad to be informative. On
    Windows-derived paths the drive prefix `C:/` IS yielded because it is
    informative (the agent works on a specific drive).
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return
    abs_root = path.startswith("/")
    # Skip the leaf (last part). On absolute POSIX paths skip the root
    # sentinel "/" so we never emit just "/".
    start = 1 if abs_root else 0
    end = len(parts) - 1  # exclude the leaf
    for i in range(end, start, -1):
        if abs_root:
            prefix = "/" + "/".join(parts[1:i]) + "/"
        else:
            prefix = "/".join(parts[:i]) + "/"
        yield prefix


def _depth(path: str) -> int:
    return path.count("/")


def _display_path(path: str) -> str:
    return path


def _is_under_emitted(path: str, emitted: set[str]) -> bool:
    return any(path.startswith(e) for e in emitted)


def _cap_by_recency(
    items: list[tuple[str, int]],
    last_seen: dict[str, int],
    cap: int,
) -> list[tuple[str, int]]:
    """Cap the report to the `cap` most-recent items.

    Recency is measured as the max `last_seen` index of any descendant leaf
    for ancestor prefixes (a path ending in '/'), or the leaf's own
    `last_seen` for leaf entries. Items not in `last_seen` (shouldn't
    happen but be defensive) keep their position.

    The final list preserves the original ordering (count desc, path asc)
    after the recency cull, so the most-active groups still lead.
    """
    if cap <= 0 or len(items) <= cap:
        return items

    def recency(path: str) -> int:
        if path in last_seen:
            return last_seen[path]
        # Ancestor prefix: max recency of any leaf below it
        prefix = path
        return max(
            (i for leaf, i in last_seen.items() if leaf.startswith(prefix)),
            default=-1,
        )

    # Pick the `cap` most-recent items by recency, then restore original order
    ranked = sorted(items, key=lambda kv: -recency(kv[0]))
    keep_keys = {kv[0] for kv in ranked[:cap]}
    return [kv for kv in items if kv[0] in keep_keys]


if __name__ == "__main__":
    main()
