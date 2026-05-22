#!/usr/bin/env python3
"""Hook OS-parity gate.

Enforces that every .sh hook has a .ps1 sibling (and vice versa) in the
project's hooks directory, and that both files are modified together in any
PR that touches one of them.

Opt-out: a magic comment ``# OS-EXEMPT-PARITY: <reason>`` in the first 5
lines of the hook file (reason must contain at least one non-whitespace
character).

Behaviour:
- If no .ps1 file exists in the hooks directory on the base ref, existence
  parity is **warn-only** (annotation, exit 0). Once any .ps1 lands on
  base, the rule turns blocking.
- Modification parity is always blocking.
- Files under ``_lib/`` or ``lib/`` subdirectories of the hooks dir are
  excluded (those are sourced helpers, not hooks).

Usage:
    # In CI (GITHUB_BASE_REF set, repo checked out with fetch-depth: 0):
    python3 .github/scripts/check_hook_parity.py

    # Locally (falls back to last commit's diff vs HEAD~1):
    python3 .github/scripts/check_hook_parity.py

Exit codes:
    0 - pass (or warn-only)
    1 - parity violation
    2 - script error (cannot resolve hooks dir, git failure, etc.)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Hooks directories to scan, relative to repo root. The first one that
# exists is used. Excluded paths are matched as components of the relative
# path (so "_lib" matches `templates/hooks/_lib/foo.sh`).
HOOKS_DIRS = ("templates/hooks", ".claude/hooks")
EXCLUDED_SUBDIRS = ("_lib", "lib")
MAGIC_COMMENT_PREFIX = "# OS-EXEMPT-PARITY:"
MAGIC_COMMENT_LINE_LIMIT = 5


def annotate(level: str, message: str, file: str | None = None) -> None:
    """Emit a GitHub Actions annotation."""
    if file:
        print(f"::{level} file={file},line=1::{message}")
    else:
        print(f"::{level} ::{message}")


def run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stdout (stripped). Raises on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def find_hooks_dir(repo_root: Path) -> Path | None:
    """Return the first hooks directory that exists in the repo, else None."""
    for candidate in HOOKS_DIRS:
        path = repo_root / candidate
        if path.is_dir():
            return path
    return None


def is_excluded(rel_path: Path) -> bool:
    """True if rel_path lies under an excluded subdir of the hooks root."""
    return any(part in EXCLUDED_SUBDIRS for part in rel_path.parts)


def has_magic_comment(file_path: Path) -> bool:
    """True if the file has a valid OS-EXEMPT-PARITY marker in lines 1-5.

    The marker must be ``# OS-EXEMPT-PARITY:`` followed by at least one
    non-whitespace character (the reason).

    v0.2.26 follow-up: opens with ``utf-8-sig`` so a UTF-8 BOM on line 1
    (added to every non-ASCII .ps1 in commit 97eceaf to unblock Windows
    PowerShell 5.1) does not hide the marker. ``utf-8-sig`` strips the
    BOM if present and behaves identically to ``utf-8`` otherwise — safe
    for both .sh (no BOM) and .ps1 (with or without BOM) files.
    """
    try:
        with file_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MAGIC_COMMENT_LINE_LIMIT:
                    break
                stripped = line.strip()
                if stripped.startswith(MAGIC_COMMENT_PREFIX):
                    reason = stripped[len(MAGIC_COMMENT_PREFIX):].strip()
                    if reason:
                        return True
    except OSError:
        return False
    return False


def list_hook_files(hooks_dir: Path, suffix: str) -> list[Path]:
    """Return absolute paths of hook files with the given suffix, excluding
    files under _lib/ or lib/."""
    out = []
    for p in hooks_dir.rglob(f"*{suffix}"):
        if not p.is_file():
            continue
        rel = p.relative_to(hooks_dir)
        if is_excluded(rel):
            continue
        out.append(p)
    return out


def base_has_ps1(repo_root: Path, hooks_dir_rel: str, base_ref: str) -> bool:
    """True if any .ps1 file exists in the hooks dir on the base ref."""
    try:
        out = run_git(
            ["ls-tree", "-r", "--name-only", base_ref, "--", hooks_dir_rel],
            cwd=repo_root,
        )
    except RuntimeError:
        return False
    for line in out.splitlines():
        line = line.strip()
        if not line.endswith(".ps1"):
            continue
        rel = Path(line).relative_to(hooks_dir_rel)
        if is_excluded(rel):
            continue
        return True
    return False


def file_exists_on_base(repo_root: Path, path: str, base_ref: str | None) -> bool:
    """True iff `path` was a tracked file on the base ref.

    Used by modification-parity to distinguish a NEW file (added in this
    PR) from a MODIFIED existing file. New files are paired with their
    pre-existing sibling implicitly — they don't require the sibling to
    also be modified.
    """
    if not base_ref:
        return False
    try:
        # `git cat-file -e` returns 0 if the object exists; non-zero
        # otherwise. Suppress stderr — non-existence is the answer, not
        # a failure to report.
        result = run_git(
            ["cat-file", "-e", f"origin/{base_ref}:{path}"], cwd=repo_root
        )
        return True
    except RuntimeError:
        return False


def get_changed_files(repo_root: Path, base_ref: str | None) -> set[str]:
    """Return the set of repo-relative changed files between base and HEAD.

    If base_ref is None (local dev), uses HEAD~1...HEAD.
    """
    if base_ref:
        # Use three-dot syntax: changes on HEAD since branch point with base.
        diff_range = f"origin/{base_ref}...HEAD"
    else:
        diff_range = "HEAD~1...HEAD"
    out = run_git(["diff", "--name-only", diff_range], cwd=repo_root)
    return {line.strip() for line in out.splitlines() if line.strip()}


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0

    repo_root = Path(
        run_git(["rev-parse", "--show-toplevel"], cwd=Path.cwd())
    ).resolve()

    hooks_dir = find_hooks_dir(repo_root)
    if hooks_dir is None:
        annotate(
            "error",
            f"No hooks directory found (looked for: {', '.join(HOOKS_DIRS)})",
        )
        return 2

    hooks_dir_rel = hooks_dir.relative_to(repo_root).as_posix()

    base_ref = os.environ.get("GITHUB_BASE_REF") or None

    try:
        changed = get_changed_files(repo_root, base_ref)
    except RuntimeError as e:
        annotate("error", f"Failed to compute changed files: {e}")
        return 2

    # Gate the existence-parity rule on whether base already has any .ps1.
    if base_ref:
        ps1_on_base = base_has_ps1(repo_root, hooks_dir_rel, f"origin/{base_ref}")
    else:
        # Local dev: check HEAD itself.
        ps1_on_base = bool(list_hook_files(hooks_dir, ".ps1"))

    sh_files = list_hook_files(hooks_dir, ".sh")
    ps1_files = list_hook_files(hooks_dir, ".ps1")

    failures: list[str] = []
    warnings: list[str] = []

    # --- Existence parity ---
    # Every .sh must have a .ps1 sibling (unless exempt).
    for sh in sh_files:
        ps1_sibling = sh.with_suffix(".ps1")
        if ps1_sibling.exists():
            continue
        if has_magic_comment(sh):
            continue
        rel = sh.relative_to(repo_root).as_posix()
        msg = (
            f"{rel}: missing .ps1 sibling. Add a .ps1 sibling, or add "
            f"a `# OS-EXEMPT-PARITY: <reason>` comment in the first 5 lines."
        )
        if ps1_on_base:
            failures.append(msg)
            annotate("error", msg, file=rel)
        else:
            warnings.append(msg)
            # Will emit a single batched warning below — keep individual file
            # annotations off to avoid noise on the warn-only path.

    # Every .ps1 must have a .sh sibling (unless exempt).
    for ps1 in ps1_files:
        sh_sibling = ps1.with_suffix(".sh")
        if sh_sibling.exists():
            continue
        if has_magic_comment(ps1):
            continue
        rel = ps1.relative_to(repo_root).as_posix()
        msg = (
            f"{rel}: missing .sh sibling. Add a .sh sibling, or add "
            f"a `# OS-EXEMPT-PARITY: <reason>` comment in the first 5 lines."
        )
        # The reverse direction is always blocking — if a .ps1 exists at all,
        # we're past the bootstrap phase.
        failures.append(msg)
        annotate("error", msg, file=rel)

    # --- Modification parity ---
    # If a hook file was modified, its sibling must also be in the diff.
    for changed_path in changed:
        if not (
            changed_path.startswith(hooks_dir_rel + "/")
            or changed_path == hooks_dir_rel
        ):
            continue
        p = Path(changed_path)
        if p.suffix not in (".sh", ".ps1"):
            continue
        rel_to_hooks = Path(changed_path).relative_to(hooks_dir_rel)
        if is_excluded(rel_to_hooks):
            continue

        sibling_suffix = ".ps1" if p.suffix == ".sh" else ".sh"
        sibling = p.with_suffix(sibling_suffix).as_posix()
        # If no sibling exists on disk and the file has a magic exempt
        # comment, modification parity does not apply.
        sibling_path_abs = repo_root / sibling
        source_path_abs = repo_root / changed_path
        # Read magic comment from the source file (if it still exists).
        if source_path_abs.exists() and has_magic_comment(source_path_abs):
            continue
        if sibling_path_abs.exists() and has_magic_comment(sibling_path_abs):
            continue

        if not sibling_path_abs.exists():
            # No sibling on disk. Existence-parity rule above already
            # handled this; do not double-fail here.
            continue

        # If THIS file is brand new on the PR (didn't exist on base) AND
        # its sibling ALREADY existed on base, this is a "new pairing"
        # case — the sibling shouldn't also need to be modified just
        # because we're adding its complement. Common scenario: first
        # batch of .ps1 siblings paired with existing unchanged .sh
        # hooks. Modification parity only fires when the file was
        # already part of a tracked pair on base.
        was_new = not file_exists_on_base(repo_root, changed_path, base_ref)
        sibling_was_on_base = file_exists_on_base(repo_root, sibling, base_ref)
        if was_new and sibling_was_on_base:
            continue

        if sibling not in changed:
            msg = (
                f"{changed_path}: modified without its sibling {sibling}. "
                f"Hooks must be updated together to keep OS parity, or "
                f"mark the file with `# OS-EXEMPT-PARITY: <reason>`."
            )
            failures.append(msg)
            annotate("error", msg, file=changed_path)

    # Summary
    if not ps1_on_base and warnings:
        annotate(
            "warning",
            f"OS parity gate active but no .ps1 hooks exist yet "
            f"({len(warnings)} .sh file(s) without siblings) — this rule "
            f"starts blocking once the first .ps1 lands.",
        )

    if failures:
        print(f"\nHook OS-parity gate FAILED ({len(failures)} violation(s)).")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("Hook OS-parity gate passed.")
    if warnings and not ps1_on_base:
        print(
            f"({len(warnings)} .sh file(s) without .ps1 siblings — "
            "warn-only until first .ps1 lands.)"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        annotate("error", f"check_hook_parity.py crashed: {e}")
        sys.exit(2)
