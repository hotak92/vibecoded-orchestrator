#!/usr/bin/env python3
"""Hook shell-discipline gate.

Enforces: if a `.sh` hook uses `set -e` (or `set -eu`) anywhere in its
preamble, it MUST also include `pipefail` — either via a single
`set -euo pipefail` line, or via separate `set -e` + `set -o pipefail`
calls.

Why: `set -e` alone is misleading. When a pipeline like `cmd1 | cmd2`
is executed, only the LAST command's exit status counts; `cmd1`
silently failing inside the pipe goes unnoticed despite `set -e`.
`set -o pipefail` makes the pipeline exit on the FIRST non-zero status,
which is what the author of `set -e` almost always meant.

The audit (`.claude/context/vco-pre-fork-audit-2026-05-07.md` item #12)
verified all current hooks already follow this rule. This script keeps
them honest — a hook author adding `set -e` without `pipefail` will
fail this gate.

Hooks that don't use `set -e` at all are fine (deliberate choice — some
hooks tolerate per-command failure). The gate only triggers on the
mismatch.

Usage:
  python3 .github/scripts/check_hook_set_directives.py

Exit codes:
  0 — all hooks pass (or no `set -e` users found)
  1 — at least one hook has `set -e` without `pipefail`
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_DIRS = [
    REPO_ROOT / ".claude" / "hooks",
    REPO_ROOT / "templates" / "hooks",
]
EXCLUDED_SUBDIRS = ("_lib", "lib")


def hook_files() -> list[Path]:
    files: list[Path] = []
    for d in HOOK_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.sh"):
            if any(part in EXCLUDED_SUBDIRS for part in f.relative_to(REPO_ROOT).parts):
                continue
            files.append(f)
    return sorted(files)


def has_set_e(content: str) -> bool:
    """Detect `set -e` in any form: -e, -eu, -euo, -eo, etc."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("set "):
            continue
        rest = stripped[4:].strip()
        if rest.startswith("-o "):
            continue  # `set -o foo` form is checked separately
        if rest.startswith("+"):
            continue  # `set +e` (unset)
        first_token = rest.split()[0] if rest else ""
        if first_token.startswith("-") and "e" in first_token[1:]:
            return True
    return False


def has_pipefail(content: str) -> bool:
    """Any form of pipefail enabling: `set -o pipefail`, `set -euo pipefail`, etc."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("set "):
            continue
        if "pipefail" in stripped:
            return True
    return False


def main() -> int:
    violations: list[tuple[Path, str]] = []
    for f in hook_files():
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"::warning file={f}::could not read: {exc}")
            continue
        if has_set_e(content) and not has_pipefail(content):
            violations.append(
                (
                    f,
                    "uses `set -e` (or `-eu`, `-eo`) but is missing "
                    "`set -o pipefail`. A pipeline failure in the first "
                    "command would be silently swallowed. Use "
                    "`set -euo pipefail` (or add a separate "
                    "`set -o pipefail` line).",
                )
            )

    if not violations:
        print(f"hook set-discipline gate passed ({len(hook_files())} files checked)")
        return 0

    print(f"::error::hook set-discipline gate FAILED ({len(violations)} violation(s))")
    for f, msg in violations:
        rel = f.relative_to(REPO_ROOT)
        print(f"::error file={rel},line=1::{rel}: {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
