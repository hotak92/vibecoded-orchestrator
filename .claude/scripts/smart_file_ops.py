#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Context-Efficient File Operations

Helpers for Claude to minimize context usage:
- Targeted reads (specific sections)
- Change summaries (not full content)
- Existence checks (before full reads)
"""

import sys
from pathlib import Path
from typing import Optional, Tuple


def check_file_exists(file_path: str) -> bool:
    """Check if file exists (0 tokens in context)"""
    return Path(file_path).exists()


def get_file_line_count(file_path: str) -> int:
    """Get line count without loading content"""
    try:
        with open(file_path) as f:
            return sum(1 for _ in f)
    except:
        return 0


def get_file_section(file_path: str, start_line: int, num_lines: int) -> Optional[str]:
    """
    Read specific section of file

    Returns only the requested lines, minimizing context usage
    """
    try:
        with open(file_path) as f:
            lines = f.readlines()
            section = lines[start_line:start_line + num_lines]
            return ''.join(section)
    except:
        return None


def find_in_file(file_path: str, pattern: str) -> Optional[Tuple[int, str]]:
    """
    Find first occurrence of pattern

    Returns (line_number, line_content) or None
    Minimal context: just the matching line
    """
    try:
        with open(file_path) as f:
            for i, line in enumerate(f, 1):
                if pattern in line:
                    return (i, line.strip())
        return None
    except:
        return None


def get_file_summary(file_path: str) -> dict:
    """
    Get file metadata without content

    Returns brief summary (minimal tokens)
    """
    try:
        path = Path(file_path)
        stat = path.stat()

        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "lines": get_file_line_count(file_path),
            "modified": stat.st_mtime
        }
    except:
        return {"exists": False}


def verify_write(file_path: str, expected_pattern: str) -> bool:
    """
    Verify write succeeded by checking for expected pattern

    Returns True/False without loading full content
    """
    result = find_in_file(file_path, expected_pattern)
    return result is not None


if __name__ == "__main__":
    # CLI interface
    if len(sys.argv) < 3:
        print("Usage:")
        print("  smart_file_ops.py check <file>")
        print("  smart_file_ops.py summary <file>")
        print("  smart_file_ops.py find <file> <pattern>")
        print("  smart_file_ops.py section <file> <start> <lines>")
        sys.exit(1)

    command = sys.argv[1]
    file_path = sys.argv[2]

    if command == "check":
        exists = check_file_exists(file_path)
        print(f"{'EXISTS' if exists else 'NOT_FOUND'}")

    elif command == "summary":
        summary = get_file_summary(file_path)
        print(f"Exists: {summary.get('exists', False)}")
        if summary.get('exists'):
            print(f"Lines: {summary.get('lines', 0)}")
            print(f"Size: {summary.get('size_bytes', 0)} bytes")

    elif command == "find":
        if len(sys.argv) < 4:
            print("Usage: smart_file_ops.py find <file> <pattern>")
            sys.exit(1)
        pattern = sys.argv[3]
        result = find_in_file(file_path, pattern)
        if result:
            line_num, line_content = result
            print(f"Found at line {line_num}: {line_content}")
        else:
            print("NOT_FOUND")

    elif command == "section":
        if len(sys.argv) < 5:
            print("Usage: smart_file_ops.py section <file> <start> <lines>")
            sys.exit(1)
        start = int(sys.argv[3])
        num_lines = int(sys.argv[4])
        section = get_file_section(file_path, start, num_lines)
        if section:
            print(section)
        else:
            print("ERROR: Could not read section")
