#!/usr/bin/env python3
"""Add `.silent()` to every `Command::new(...)` call in the launcher source.

Rationale: 2026-05-26 audit found 208 of 221 `Command::new` /
`TokioCommand::new` call sites in the launcher were missing the
`CREATE_NO_WINDOW` flag (0x08000000) on Windows. Each missing site flashes
a conhost console window on screen for the duration of the subprocess.
With 11+ subprocesses spawned at launcher boot (detect_system, hub start,
runtime probes, embedding catalog, etc.), the user sees a cascading
"fork bomb" of console windows.

Fix: introduce `CommandExt::silent()` chainable method in
`vct-launcher-core/src/process.rs` and chain `.silent()` after every
`Command::new(...)` call site.

This script does the mass-edit safely:
  - Only modifies `Command::new(...)` lines (not doc comments, not test
    code that uses `assert!(...)` patterns)
  - Detects already-fixed sites (`.silent()` already chained, or
    `creation_flags(0x08000000)` already inline) and skips them
  - Adds `use vct_launcher_core::process::CommandExt as _;` import at top
    of each touched file (or the in-crate `use crate::process::CommandExt
    as _;` for files inside vct-launcher-core itself)
  - Idempotent: re-running is safe (skips already-fixed sites)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# Regex: matches the EXPRESSION value of Command::new(...) on its own line.
# We capture the indent + everything up to AND including `Command::new(...)`,
# so we can append `.silent()` directly after the closing paren.
#
# Examples it should match:
#   let mut cmd = Command::new("git")
#   let out = tokio::process::Command::new(&python)
#   tokio::process::Command::new(binary)
#   let s = TokioCommand::new("git")
#
# What we DON'T want to match (doc-comment lines starting with /// or //):
COMMAND_NEW_RE = re.compile(
    r"^(?P<prefix>[^/]*)"                    # everything before, no `//` line
    r"(?P<cmd>(?:tokio::process::|std::process::)?Command::new\([^)]*\))"
    r"(?P<suffix>.*)$"
)

# Already-fixed detector: skip if line already has `.silent()` OR the next
# 12 lines contain `.creation_flags(0x08000000)` OR `silent_command(`.
FIXED_WINDOW_LINES = 12


def is_already_silent(lines: list[str], idx: int) -> bool:
    """Return True if the Command::new at lines[idx] is already silenced."""
    # Same line: .silent() chained on a one-liner?
    if ".silent()" in lines[idx]:
        return True
    # Next 12 lines: .silent() at end of chain, or creation_flags inline,
    # or silent_command helper used.
    window = "".join(lines[idx + 1 : idx + 1 + FIXED_WINDOW_LINES])
    if ".silent()" in window:
        return True
    if "creation_flags(0x0800_0000)" in window or "creation_flags(0x08000000)" in window:
        return True
    if "silent_command(" in window:
        return True
    return False


def should_skip_line(line: str) -> bool:
    """Skip pure doc/comment lines."""
    stripped = line.lstrip()
    if stripped.startswith("//"):  # covers // and ///
        return True
    return False


def in_test_module(lines: list[str], idx: int) -> bool:
    """True if the line falls inside a `#[cfg(test)] mod tests` block.

    Walk backwards looking for `#[cfg(test)]` followed by `mod` within the
    next few lines. We don't track full block scope (no AST), but the
    heuristic is good enough: if we see the test-mod marker before any
    later top-level item we're inside it.
    """
    for j in range(idx, max(-1, idx - 500), -1):
        if j < 0:
            break
        line = lines[j].lstrip()
        if line.startswith("#[cfg(test)]"):
            # Check the immediately-next non-empty line is `mod ...`
            for k in range(j + 1, min(len(lines), j + 5)):
                if lines[k].lstrip().startswith("mod "):
                    return True
        # Bail out if we hit a function/impl at top level above
        if line.startswith("pub fn ") or line.startswith("fn ") or line.startswith("impl "):
            # Could be inside a function inside a test mod — keep walking
            pass
    return False


def process_file(path: pathlib.Path, dry_run: bool) -> tuple[int, int]:
    """Process one file. Returns (sites_fixed, sites_skipped)."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)

    sites_fixed = 0
    sites_skipped = 0
    out_lines: list[str] = []

    for idx, line in enumerate(lines):
        if should_skip_line(line):
            out_lines.append(line)
            continue
        m = COMMAND_NEW_RE.search(line)
        if not m:
            out_lines.append(line)
            continue
        # We have a Command::new(...) on this line.
        if is_already_silent(lines, idx):
            sites_skipped += 1
            out_lines.append(line)
            continue
        if in_test_module(lines, idx):
            # Tests: leave alone (some test patterns intentionally check
            # process behaviour and shouldn't have side-effects from a
            # console-suppression flag).
            sites_skipped += 1
            out_lines.append(line)
            continue

        # Insert `.silent()` right after the `Command::new(...)` closing paren.
        prefix = m.group("prefix")
        cmd = m.group("cmd")
        suffix = m.group("suffix")
        # Preserve trailing newline.
        trailing_nl = "\n" if line.endswith("\n") else ""
        # Strip the trailing newline from `suffix` before re-appending it.
        suffix_no_nl = suffix.rstrip("\n")
        new_line = f"{prefix}{cmd}.silent(){suffix_no_nl}{trailing_nl}"
        out_lines.append(new_line)
        sites_fixed += 1

    # Track if file uses .silent() anywhere (either we added it now OR a
    # previous run did). If so, ensure the import is present.
    needs_import = (sites_fixed > 0) or any(".silent()" in l for l in out_lines)
    if needs_import and not dry_run:
        # Add the import if it's not already there.
        body = "".join(out_lines)
        # Decide import path: in-crate for vct-launcher-core files, external otherwise.
        path_norm = str(path).replace("\\", "/")
        if "vct-launcher-core" in path_norm:
            import_line = "use crate::process::CommandExt as _;\n"
            wanted_marker = "crate::process::CommandExt"
        elif "vct-hub" in path_norm:
            import_line = "use vct_launcher_core::process::CommandExt as _;\n"
            wanted_marker = "vct_launcher_core::process::CommandExt"
        else:
            import_line = "use vct_launcher_core::process::CommandExt as _;\n"
            wanted_marker = "vct_launcher_core::process::CommandExt"
        if wanted_marker not in body:
            # Insert after the LAST top-level `use ` directive at file top.
            # CAREFUL: skip lines that are continuations of a multi-line
            # `use crate::foo::{ a, b, c };` block. We detect such blocks
            # by tracking open braces.
            insert_idx = 0
            brace_depth = 0
            for i, l in enumerate(out_lines[:200]):
                if l.startswith("use ") and brace_depth == 0:
                    # Single-line use OR start of multi-line use
                    insert_idx = i + 1
                    brace_depth = l.count("{") - l.count("}")
                elif brace_depth > 0:
                    # We're inside a multi-line use {} block — track + advance insertion past the closing brace
                    brace_depth += l.count("{") - l.count("}")
                    if brace_depth == 0:
                        insert_idx = i + 1
            if insert_idx > 0:
                out_lines.insert(insert_idx, import_line)
            else:
                # No `use` found in head; append after the first module
                # doc-comment block.
                out_lines.insert(0, import_line)
            body = "".join(out_lines)
        path.write_text(body, encoding="utf-8")
    return sites_fixed, sites_skipped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--root",
        default=str(pathlib.Path(__file__).resolve().parent.parent / "launcher" / "src-tauri"),
        help="Root directory to scan (default: launcher/src-tauri/)",
    )
    p.add_argument("--dry-run", action="store_true", help="Don't write, just report")
    args = p.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    total_fixed = 0
    total_skipped = 0
    for rs_file in sorted(root.rglob("*.rs")):
        # Skip target/ build artefacts.
        if "target" in rs_file.parts:
            continue
        fixed, skipped = process_file(rs_file, dry_run=args.dry_run)
        if fixed or skipped:
            print(f"{rs_file.relative_to(root)}: fixed={fixed} skipped={skipped}")
        total_fixed += fixed
        total_skipped += skipped

    print()
    print(f"=== TOTAL: fixed={total_fixed} skipped={total_skipped} {'(dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
