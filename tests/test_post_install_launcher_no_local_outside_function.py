# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for M-P0-5 (v0.2.53).

`scripts/post-install-launcher.sh:851,853,862` used `local` outside any
function. Under `set -uo pipefail` (line 50 of that script), `local`
outside a function:
1. prints "local: can only be used in a function" to stderr,
2. does NOT assign the variable,
3. returns exit 1,
4. the next reference to the unassigned variable triggers
   "unbound variable" → script exits 127 silently.

The trigger is the build-path's npm→pnpm install branch — reached on
every macOS user given the M-P0-4 + M-P0-2 cascade. The fix removes
the bare `local` keywords.

This test scans the script for `local` declarations not enclosed in a
`<name>() { ... }` (or `function <name>() { ... }`) block.

We use a hand-rolled function-tracker rather than a real bash parser
because the bash AST tools are not portable. The tracker:
- counts `{` / `}` depth INSIDE function definitions only,
- treats a function as opening at the `name() {` pattern and closing
  when its brace depth returns to 0,
- enforces that any line starting with `local ` is at function-depth > 0.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "scripts" / "post-install-launcher.sh"


FUNC_OPEN_RE = re.compile(r"^\s*(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*\{?\s*$")
LOCAL_RE = re.compile(r"^\s*local\b")


def _scan_for_local_outside_function(src: str) -> list[tuple[int, str]]:
    """Return list of (line_number, line_text) for offending `local` uses.

    Heuristic:
    - Track curly-brace depth in the WHOLE script (excluding string
      bodies and HEREDOCs — see exclusions below).
    - When a function opens (matched by FUNC_OPEN_RE), remember the
      brace depth at which it started; everything inside it (until
      depth returns to that level) counts as "inside function".
    - A `local` declaration outside any active function is offending.
    """
    offending: list[tuple[int, str]] = []
    brace_depth = 0
    func_stack: list[int] = []  # entry brace_depth of each open function
    in_heredoc: str | None = None  # delimiter, if any
    in_pyheredoc = False  # python embedded HEREDOC

    lines = src.splitlines()
    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip()

        # HEREDOC tracking — skip body lines, they aren't bash.
        if in_heredoc is not None:
            if line.strip() == in_heredoc:
                in_heredoc = None
            continue

        # Detect simple HEREDOC openers: `<<'TAG'` or `<<TAG` (no
        # whitespace around quotes).
        m = re.search(r"<<[-]?'?([A-Za-z_][A-Za-z0-9_]*)'?\s*[)]?\s*$", line)
        if m:
            in_heredoc = m.group(1)
            # Fall through — opening line itself can still have other
            # syntax we care about, but typically the opener is a
            # single `python3 - <<'PY'`-style line.
            # We still keep brace tracking by continuing the line.

        # Skip pure comments.
        if line.lstrip().startswith("#"):
            continue

        # Function opener: `name() {` — sometimes the `{` is on the
        # same line, sometimes the next.
        if FUNC_OPEN_RE.match(line):
            # Add to func_stack at the depth that will be after the `{`.
            # If the `{` is on this line, depth increments first; if
            # not, the next `{` will increment it. We pessimistically
            # mark at current depth + 1 when there's a `{` on the line.
            opens_on_line = line.count("{") - line.count("}")
            # function opens at the depth AT the `{` (depth+1 here).
            entry_depth = brace_depth + max(opens_on_line, 1)
            func_stack.append(entry_depth)

        # Check `local` BEFORE updating brace depth — `local` is
        # judged by the depth at its line.
        if LOCAL_RE.match(line) and not func_stack:
            offending.append((i, line.strip()))

        # Update brace depth (very rough — counts `{` and `}` outside
        # strings, which post-install-launcher.sh respects well enough
        # for this heuristic).
        for ch in line:
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                # Check function stack: did we close a function?
                while func_stack and brace_depth < func_stack[-1]:
                    func_stack.pop()

    return offending


def test_no_local_outside_function_in_post_install_launcher():
    src = TARGET.read_text(encoding="utf-8")
    offenders = _scan_for_local_outside_function(src)
    assert not offenders, (
        "scripts/post-install-launcher.sh has `local` declarations "
        "outside any function — these abort under `set -u` (M-P0-5):\n"
        + "\n".join(f"  line {n}: {t}" for n, t in offenders)
    )


def test_npm_prefix_assignment_does_not_use_local():
    """Direct positive-controlled check on the M-P0-5 fix site."""
    src = TARGET.read_text(encoding="utf-8")
    # The fix block should contain plain `npm_prefix=""` (no `local`).
    # Search a window around the original offending lines.
    assert re.search(
        r"^\s*npm_prefix=\"\"\s*$", src, re.MULTILINE
    ), "Expected plain `npm_prefix=\"\"` after M-P0-5 fix"
    assert re.search(
        r"^\s*probe_dirs=\(\)\s*$", src, re.MULTILINE
    ), "Expected plain `probe_dirs=()` after M-P0-5 fix"
    # Ensure no `local` precedes EITHER npm_prefix or probe_dirs.
    # We do NOT check for `local cand` here because there IS a
    # legitimate `local cand` inside the `_ensure_path_for_tool`
    # function (line ~218) — the structural scanner above proves no
    # `local` appears outside a function.
    bad = re.search(
        r"^\s*local\s+npm_prefix\b|^\s*local\s+probe_dirs\b",
        src,
        re.MULTILINE,
    )
    assert not bad, (
        "Found re-introduced `local npm_prefix`/`local probe_dirs` at the "
        "M-P0-5 site: " + (bad.group(0) if bad else "")
    )


def test_set_u_simulation_block_runs():
    """Smoke: run the FIXED block under `set -uo pipefail` to confirm it
    doesn't error on the first variable reference.

    This re-creates the exact pattern from the script — empty
    `npm_prefix`, then `probe_dirs+=` extensions, then a loop.
    """
    import subprocess
    snippet = """
        set -uo pipefail
        npm_prefix=""
        npm_prefix="${npm_prefix:-}"
        probe_dirs=()
        if [ -n "$npm_prefix" ]; then
            probe_dirs+=("$npm_prefix/bin")
        fi
        probe_dirs+=(
            "$HOME/.local/share/npm/bin"
            "$HOME/.npm-global/bin"
        )
        for cand in "${probe_dirs[@]}"; do
            echo "would probe: $cand"
        done
    """
    out = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0, (
        f"Fixed block failed under set -uo pipefail: rc={out.returncode}, "
        f"stderr={out.stderr!r}"
    )
    assert "would probe" in out.stdout
