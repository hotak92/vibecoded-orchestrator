#!/usr/bin/env python3
"""Template drift gate.

Enforces that `.claude/hooks/` and `.claude/scripts/` stay byte-identical
to their `templates/hooks/` and `templates/scripts/` counterparts —
EXCEPT for files explicitly marked as intentionally divergent.

Why this exists
---------------
`.claude/` is the orchestrator's own runtime copy; `templates/` is what
`vco_lib.project_init install-bundle` drops into NEW projects when a user
registers one. If unintended drift accumulates, the orchestrator works
(it uses `.claude/`) but every newly-registered project gets stale or
broken copies. We've shipped that bug at least twice (Audit-G 2026-04-30;
codegraph venv 2026-05-07). This gate pins the invariant in CI.

Two kinds of intentional divergence
-----------------------------------
1. **One-sided files** (`EXPECTED_ONESIDED`): exist on one side only,
   intentionally — e.g. helper scripts shipped to user projects but not
   used by the orchestrator itself.

2. **Asymmetric pairs** (`EXPECTED_ASYMMETRIC`): exist on both sides but
   their content intentionally differs. The most common reason is the
   PR-2 / PR-143 "rewiring": `templates/scripts/*.py` resolve
   `claude_mcp_servers/` via `$VCT_ORCHESTRATOR_ROOT` (because in a user
   project, the script lives at `<user-project>/.claude/scripts/` but
   the MCP servers are back in the orchestrator clone), while
   `.claude/scripts/*.py` resolve via in-tree `_PROJECT_HOME =
   _SCRIPT_DIR.parent.parent` (because the orchestrator IS its own
   home).

Adding to either list requires touching this file, which forces a CR
discussion (the goal — drift should be intentional, not accidental).

Hook drift resolution (2026-05-07, follow-up #6)
------------------------------------------------
The 12 hooks formerly on EXPECTED_ASYMMETRIC have been brought into
lockstep between `.claude/hooks/` and `templates/hooks/`. The merged
canonical version preserves both the audit-driven portability scaffolding
(find-python.sh, notify.py wrapper, cross-platform port probes,
last-compact-marker, etc.) and the auth-mode detection that landed in
.claude/-side cost-tracker.sh on 2026-05-01. Each .sh edit was paired
with the matching .ps1 update to satisfy the hook OS-parity gate.

Sentinel-marked rewire blocks (2026-05-07, follow-up #6 phase 3)
----------------------------------------------------------------
The 7 PR-2-rewired scripts retain their orchestrator-root-resolution
asymmetry, but the asymmetric region is now wrapped in matching sentinel
comments on BOTH sides:

    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    ...different on .claude/ vs templates/...
    # VCO-REWIRE-END: orchestrator-root-resolution

Before comparing two such files this gate strips any text between
matching sentinel pairs (inclusive of the sentinel lines themselves)
and compares the rest. If anything OUTSIDE the sentinel block diverges,
the gate fails — so unrelated drift still gets caught.

This means new asymmetric files don't need entries in EXPECTED_ASYMMETRIC
at all; just wrap the divergent region in a sentinel pair on both sides.
EXPECTED_ASYMMETRIC remains as an escape hatch for files that genuinely
can't be sentinel-wrapped (none currently — the 7 scripts are kept on
the list as belt-and-braces while we validate the sentinel approach).

Behaviour
---------
- Walks both dirs, compares pairs by name (extension matters).
- Asymmetric extras: blocking unless listed in EXPECTED_ONESIDED.
- Content drift: blocking unless EITHER (a) sentinel-strip equality holds,
  or (b) the pair is in EXPECTED_ASYMMETRIC.
- Files under `_lib/`, `lib/`, `__pycache__/`: excluded (helpers /
  generated).

Usage:
    python3 .github/scripts/check_template_drift.py

Exit codes:
    0 - pass
    1 - drift detected
    2 - script error
"""

from __future__ import annotations

import filecmp
import sys
from pathlib import Path

# Sentinel markers for "rewire-block" asymmetry. Lines BETWEEN matching
# BEGIN/END pairs (inclusive) are stripped before content comparison so
# both sides can carry a different orchestrator-root-resolution scheme
# without the drift gate flagging it. The trailing tag (after the colon)
# is a free-text label for humans — we match on the prefix only.
SENTINEL_BEGIN = "# VCO-REWIRE-BEGIN:"
SENTINEL_END = "# VCO-REWIRE-END:"

# (canonical, mirror) pairs to compare.
PAIRS = [
    (".claude/hooks", "templates/hooks"),
    (".claude/scripts", "templates/scripts"),
]

EXCLUDED_DIRS = {"_lib", "lib", "__pycache__"}

# Files allowed to live on only one side. Format: "<dir>/<filename>".
# Adding to this list requires explicit reviewer attention. Keep it tight.
EXPECTED_ONESIDED = {
    # Shipped to user projects but not used by the orchestrator itself.
    "templates/scripts/query_code_graph.py",
}

# Pairs allowed to differ in content WITHOUT sentinel-marked rewire blocks.
# Format: relative path under EITHER canonical or mirror (we match by suffix).
#
# Hook drift was resolved 2026-05-07 (follow-up #6 phase 1+2); all 12 hooks
# formerly listed here are now byte-identical between .claude/ and templates/
# on both .sh and .ps1.
#
# The 7 PR-2-rewired scripts (analyze_code_graph.py, detect_duplicates.py,
# generate-kg-summary.py, maintain_knowledge_graph.py, process_documents.py,
# search_knowledge.py, sync_knowledge_graph.py) formerly listed here have
# been migrated to the sentinel-block approach (follow-up #6 phase 3,
# 2026-05-07). Their orchestrator-root-resolution divergence is wrapped in
# matching `# VCO-REWIRE-BEGIN/END: orchestrator-root-resolution` markers
# on both sides; the gate strips those blocks before comparing, so any
# OTHER drift in those files still gets caught.
#
# This set is empty by design as of 2026-05-07. Prefer wrapping new
# divergence in a sentinel pair over adding entries here.
EXPECTED_ASYMMETRIC: set[str] = set()


def annotate(level: str, message: str, file: str | None = None) -> None:
    """Emit a GitHub Actions annotation (or plain stderr locally)."""
    if file:
        print(f"::{level} file={file}::{message}", file=sys.stderr)
    else:
        print(f"::{level}::{message}", file=sys.stderr)


def list_files(root: Path) -> set[str]:
    """Return set of relative file paths under root, excluding helper subdirs."""
    out: set[str] = set()
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        out.add(str(rel))
    return out


def _strip_sentinel_blocks(text: str) -> tuple[str, int]:
    """Drop any `VCO-REWIRE-BEGIN ... VCO-REWIRE-END` blocks (inclusive).

    A block is matched when both BEGIN and END markers are seen in order;
    if a BEGIN appears without a matching END, all remaining lines are
    treated as inside the block (and thus stripped) — that keeps the
    behaviour predictable for malformed files instead of silently skipping
    the strip.

    Returns
    -------
    (stripped_text, blocks_stripped)
        `stripped_text` has the sentinel lines and everything between them
        removed. `blocks_stripped` is the number of complete pairs found
        (useful for diagnostics; an unterminated BEGIN counts as 1).
    """
    out_lines: list[str] = []
    inside = False
    blocks = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not inside:
            if stripped.startswith(SENTINEL_BEGIN):
                inside = True
                continue
            out_lines.append(line)
        else:
            if stripped.startswith(SENTINEL_END):
                inside = False
                blocks += 1
                continue
            # else: line is inside the block, drop it
    if inside:
        # Unterminated BEGIN — count it for diagnostics. Lines after the
        # BEGIN have already been dropped, which is the safe default.
        blocks += 1
    return "".join(out_lines), blocks


def _files_match_after_sentinel_strip(c_path: Path, m_path: Path) -> bool:
    """Compare two files for equality after stripping sentinel blocks.

    Read errors → False (treated as drift, lets the operator see the
    issue rather than silently passing). Files larger than ~5MB are
    declined (sentinel approach is for source files; anything bigger
    is a binary or generated artefact and shouldn't be on the gate's
    radar anyway).
    """
    SIZE_LIMIT = 5 * 1024 * 1024
    try:
        if c_path.stat().st_size > SIZE_LIMIT or m_path.stat().st_size > SIZE_LIMIT:
            return False
        c_text = c_path.read_text(encoding="utf-8", errors="strict")
        m_text = m_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return False

    c_stripped, c_blocks = _strip_sentinel_blocks(c_text)
    m_stripped, m_blocks = _strip_sentinel_blocks(m_text)

    # Both sides must carry sentinels (otherwise the drift is real, not
    # an intentional rewire-block divergence) AND they must agree on the
    # block count. A mismatched count means one side wrapped a block the
    # other didn't, which is itself drift worth reporting.
    if c_blocks == 0 or m_blocks == 0 or c_blocks != m_blocks:
        return False

    return c_stripped == m_stripped


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]

    failures: list[str] = []

    for canonical, mirror in PAIRS:
        c_root = repo_root / canonical
        m_root = repo_root / mirror

        if not c_root.is_dir() or not m_root.is_dir():
            annotate(
                "error",
                f"missing dir: {canonical} ({c_root.is_dir()}) / "
                f"{mirror} ({m_root.is_dir()})",
            )
            return 2

        c_files = list_files(c_root)
        m_files = list_files(m_root)

        for rel in sorted(c_files - m_files):
            key = f"{canonical}/{rel}"
            if key in EXPECTED_ONESIDED:
                continue
            failures.append(f"{key}: present in {canonical} but missing in {mirror}")
            annotate("error", f"missing in {mirror}", file=key)

        for rel in sorted(m_files - c_files):
            key = f"{mirror}/{rel}"
            if key in EXPECTED_ONESIDED:
                continue
            failures.append(f"{key}: present in {mirror} but missing in {canonical}")
            annotate("error", f"missing in {canonical}", file=key)

        # Strip leading "<dirname>/" from canonical (e.g. ".claude/")
        # to compute the EXPECTED_ASYMMETRIC suffix. We use the second
        # path component onwards because both canonical and mirror end
        # in the same shape (hooks/foo.sh, scripts/bar.py).
        canonical_suffix = canonical.split("/", 1)[1] + "/" if "/" in canonical else ""

        for rel in sorted(c_files & m_files):
            c_path = c_root / rel
            m_path = m_root / rel
            if filecmp.cmp(c_path, m_path, shallow=False):
                continue
            # Sentinel-strip pass: if both sides carry matching
            # VCO-REWIRE-BEGIN/END blocks and agree everywhere else,
            # that's an intentional rewire-block asymmetry, not drift.
            if _files_match_after_sentinel_strip(c_path, m_path):
                continue
            asym_key = f"{canonical_suffix}{rel}"
            if asym_key in EXPECTED_ASYMMETRIC:
                continue
            failures.append(
                f"{canonical}/{rel} ↔ {mirror}/{rel}: content differs"
            )
            annotate(
                "error",
                f"content drift vs {mirror}/{rel}",
                file=f"{canonical}/{rel}",
            )

    if failures:
        print("\nTemplate drift detected:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nFix: copy the canonical version to the mirror, OR add to "
            "EXPECTED_ONESIDED in .github/scripts/check_template_drift.py "
            "if the divergence is intentional.",
            file=sys.stderr,
        )
        return 1

    print("OK: .claude/ and templates/ are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
