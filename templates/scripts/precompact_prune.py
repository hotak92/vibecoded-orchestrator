#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Gentle pre-compact context pruning — generates a compact summary of large tool
results that were in context, so the reinject hook can include it instead of
Claude re-reading everything.

Called by pre-compact-save.sh. Reads hook stdin JSON for transcript_path.
Writes summary to .claude/context/pruned-context-summary.md

NOT aggressive — only summarizes clearly stale, large results. When in doubt,
keeps the full description so Claude knows to re-read if needed.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def main() -> None:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    output_path = Path(project_dir) / ".claude" / "context" / "pruned-context-summary.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Read hook stdin for transcript_path
    transcript_path = None
    try:
        stdin_data = json.loads(sys.stdin.read())
        transcript_path = stdin_data.get("transcript_path")
    except Exception:
        pass

    if not transcript_path or not Path(transcript_path).is_file():
        # No transcript available — write empty summary and exit
        output_path.write_text("")
        return

    # Parse transcript
    messages = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        output_path.write_text("")
        return

    if not messages:
        output_path.write_text("")
        return

    # Analyze tool results — find large, stale ones
    total = len(messages)
    stale_threshold = 10  # messages older than this from the end
    recent_cutoff = max(0, total - 5)  # never touch last 5

    files_read: dict[str, int] = {}  # path -> char count
    searches: list[dict] = []  # {pattern, chars, index}
    commands: list[dict] = []  # {cmd_preview, chars, index}
    pruned_count = 0
    pruned_chars = 0

    for i, msg in enumerate(messages):
        if i >= recent_cutoff:
            break  # don't touch recent messages

        age = total - i  # how many messages ago
        if age < stale_threshold:
            continue  # too recent to prune

        # Look for tool results
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block.get("content", "")
                    if isinstance(result_text, list):
                        result_text = " ".join(
                            b.get("text", "") for b in result_text if isinstance(b, dict)
                        )
                    if not isinstance(result_text, str):
                        continue

                    chars = len(result_text)

                    # Identify tool from preceding assistant message
                    tool_name = block.get("tool_use_id", "")
                    # Try to find the tool name from context
                    # We look at the content for patterns
                    if chars > 500 and "file_path" in str(block):
                        path_match = _extract_path(result_text)
                        if path_match:
                            files_read[path_match] = max(files_read.get(path_match, 0), chars)
                            pruned_count += 1
                            pruned_chars += chars

                    elif chars > 300 and ("matches" in result_text.lower() or "found" in result_text.lower()):
                        searches.append({"preview": result_text[:80], "chars": chars, "index": i})
                        pruned_count += 1
                        pruned_chars += chars

                    elif chars > 1000:
                        commands.append({"preview": result_text[:60], "chars": chars, "index": i})
                        pruned_count += 1
                        pruned_chars += chars

    # Generate summary — keep it SHORT (must save more tokens than it costs)
    if pruned_count == 0:
        output_path.write_text("")
        return

    # Only worth writing if we pruned enough to justify the summary tokens
    est_saved = pruned_chars // 4
    if est_saved < 200:  # not worth a summary for <200 tokens saved
        output_path.write_text("")
        return

    parts = []
    if files_read:
        paths = ", ".join(f"`{p}`" for p in sorted(files_read)[:8])
        parts.append(f"Files read: {paths}")
    if searches:
        parts.append(f"{len(searches)} search results")
    if commands:
        parts.append(f"{len(commands)} command outputs")

    summary = f"*Stale context pruned (~{est_saved} tokens): {'; '.join(parts)}. Re-read if needed.*"
    output_path.write_text(summary)

    # Report to stderr for hook feedback
    est_tokens = pruned_chars // 4
    print(f"Pruned {pruned_count} stale results (~{est_tokens} tokens saved)", file=sys.stderr)


def _extract_path(text: str) -> str | None:
    """Try to extract a file path from tool result text."""
    # Common patterns: first line often has the path
    for line in text.split("\n")[:3]:
        line = line.strip()
        if line.startswith("/") and " " not in line[:80]:
            return line
        if ":" in line and line.split(":")[0].startswith("/"):
            return line.split(":")[0]
    return None


if __name__ == "__main__":
    main()
