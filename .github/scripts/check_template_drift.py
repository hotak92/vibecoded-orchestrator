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

Behaviour
---------
- Walks both dirs, compares pairs by name (extension matters).
- Asymmetric extras: blocking unless listed in EXPECTED_ONESIDED.
- Content drift: blocking unless the pair is in EXPECTED_ASYMMETRIC.
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

# Pairs allowed to differ in content. Format: relative path under EITHER
# canonical or mirror (we match by suffix). The most common cause is the
# PR-2/PR-143 rewiring: templates/scripts/*.py resolve
# claude_mcp_servers/ via $VCT_ORCHESTRATOR_ROOT, .claude/scripts/*.py
# resolve via in-tree _SCRIPT_DIR.parent.parent. The rest of each file is
# typically identical; only the resolution block at the top diverges.
#
# The 12 hooks below are also asymmetric: .claude/hooks/*.sh has accumulated
# audit-driven enhancements (auth-mode detection, pruned-context-summary,
# etc.) that templates/hooks/*.sh hasn't picked up yet. Resolving that
# drift requires careful per-file review + matching .ps1 updates (the
# Hook OS-Parity gate enforces .sh ↔ .ps1 lockstep). Tracked in
# follow-up #6 of .claude/CONTEXT_STATE.md ("Bidirectional .claude/hooks/
# ↔ templates/hooks/ per-file sync"). Until that lands, this allowlist
# pins the current known-good state so NEW drift gets caught.
EXPECTED_ASYMMETRIC = {
    # PR-2 / PR-143 rewiring: env-var lookup vs in-tree resolution.
    "scripts/analyze_code_graph.py",
    "scripts/detect_duplicates.py",
    "scripts/generate-kg-summary.py",
    "scripts/maintain_knowledge_graph.py",
    "scripts/process_documents.py",
    "scripts/search_knowledge.py",
    "scripts/sync_knowledge_graph.py",
    # Hook drift accumulated from .claude/-side audit enhancements;
    # resolving requires .sh+.ps1 lockstep updates. See follow-up #6.
    "hooks/compact-context-reinject.sh",
    "hooks/cost-tracker.sh",
    "hooks/ensure-code-embed-service.sh",
    "hooks/notify-stop.sh",
    "hooks/post-compact.sh",
    "hooks/post-file-edit.sh",
    "hooks/post-git-commit-kg-sync.sh",
    "hooks/post-tool-security.sh",
    "hooks/pre-compact-save.sh",
    "hooks/pre-edit-context-inject.sh",
    "hooks/session-start-kg-loader.sh",
    "hooks/stop-failure-notify.sh",
}


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
