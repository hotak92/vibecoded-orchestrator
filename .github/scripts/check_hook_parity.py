#!/usr/bin/env python3
"""Hook OS-parity gate.

Enforces that every .sh hook has a .ps1 sibling (and vice versa) in the
project's hooks directory AND the templates/scripts/ directory, and that
both files are modified together in any PR that touches one of them.

Opt-out: a magic comment ``# OS-EXEMPT-PARITY: <reason>`` in the first 5
lines of the hook file (reason must contain at least one non-whitespace
character).

Behaviour:
- Each scan root is checked INDEPENDENTLY. If no .ps1 file exists in a
  given root on the base ref, existence parity for THAT root is
  **warn-only** (annotation, exit 0). Once any .ps1 lands on base in that
  root, the rule turns blocking for it.
- Modification parity is always blocking.
- Files under ``_lib/`` or ``lib/`` subdirectories of any scan root are
  excluded (those are sourced helpers, not hooks).

v0.2.54 Track E (Theme 6): the parity gate now covers
``templates/scripts/`` in addition to ``templates/hooks/``. Pre-Track-E
this was a coverage gap (per the
``feedback_multi_os_sibling_check_at_pr_time`` lesson): a script could
land with a `.sh` but no `.ps1` sibling, leaving native-Windows users
(no WSL) silently bypassing whatever the script gated. The shell-side
WRITE-path access check (`vct_access_check.sh`) was the trigger case.

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

# Scan roots, relative to repo root. Each is checked INDEPENDENTLY: a
# missing .ps1 in one root doesn't gate the others, and the warn-only
# bootstrap state is tracked per root. Pre-Track-E this was a single
# `HOOKS_DIRS` tuple — `templates/scripts/` was not covered. Excluded
# paths are matched as components of the relative path (so "_lib" matches
# `templates/hooks/_lib/foo.sh`).
#
# Order matters for the legacy single-root fallback: if neither
# templates/hooks nor templates/scripts exists, we fall back to
# .claude/hooks. The list-of-tuples shape lets us scan multiple roots.
SCAN_ROOTS = ("templates/hooks", "templates/scripts", ".claude/hooks")
# Legacy alias kept for any out-of-tree consumers that imported HOOKS_DIRS.
HOOKS_DIRS = SCAN_ROOTS
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
    """Return the first hooks directory that exists in the repo, else None.

    Legacy single-root accessor kept for backwards compatibility; the v0.2.54
    Track E multi-root scan uses `find_scan_roots` instead.
    """
    for candidate in SCAN_ROOTS:
        path = repo_root / candidate
        if path.is_dir():
            return path
    return None


def find_scan_roots(repo_root: Path) -> list[Path]:
    """Return every scan root that exists, in SCAN_ROOTS order.

    Track E (v0.2.54) — multi-root scan. Pre-Track-E only the first
    matching root was returned (find_hooks_dir, single-root semantics);
    the parity gate ran against templates/hooks and silently ignored
    templates/scripts. This function returns ALL roots that exist so the
    main loop checks parity in each independently.
    """
    out: list[Path] = []
    for candidate in SCAN_ROOTS:
        path = repo_root / candidate
        if path.is_dir():
            out.append(path)
    return out


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


def _has_extensionless_bash_sibling(ps1_path: Path) -> bool:
    """True iff `<ps1_path>` has an extensionless bash sibling next to it.

    v0.2.54 Track E: templates/scripts/ uses a different convention from
    templates/hooks/. Hooks ship as `name.sh` + `name.ps1` siblings. Scripts
    ship as `name` (extensionless bash wrapper, e.g. `kg-sync`) + `name.ps1`.
    Both are valid parity shapes; this function lets the gate recognize the
    latter without flagging it as a missing-.sh-sibling violation.

    The check is: does a file at the same stem WITHOUT any suffix exist AND
    start with a `#!/bin/bash` or `#!/usr/bin/env bash` shebang? The
    shebang check ensures we don't accept a random unrelated file as a
    bash sibling (e.g. a JSON config that happens to share the stem).
    """
    if ps1_path.suffix != ".ps1":
        return False
    bare = ps1_path.with_suffix("")
    if not bare.is_file():
        return False
    try:
        with bare.open("rb") as f:
            first_line = f.readline(256).decode("utf-8", errors="replace").rstrip()
    except OSError:
        return False
    return first_line.startswith("#!") and "bash" in first_line


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


def _check_one_root(
    repo_root: Path,
    scan_root: Path,
    base_ref: str | None,
    changed: set[str],
    failures: list[str],
    warnings: list[str],
) -> bool:
    """Run existence + modification parity for ONE scan root.

    Returns True iff this root's existence rule is currently blocking
    (i.e. has at least one .ps1 on base). Caller aggregates results.
    """
    scan_root_rel = scan_root.relative_to(repo_root).as_posix()

    # Gate the existence-parity rule on whether base already has any .ps1
    # IN THIS ROOT. Each root is independent — a root that has not yet
    # graduated to "has at least one .ps1" stays warn-only even if other
    # roots are already blocking.
    if base_ref:
        ps1_on_base = base_has_ps1(repo_root, scan_root_rel, f"origin/{base_ref}")
    else:
        ps1_on_base = bool(list_hook_files(scan_root, ".ps1"))

    sh_files = list_hook_files(scan_root, ".sh")
    ps1_files = list_hook_files(scan_root, ".ps1")

    # --- Existence parity ---
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

    for ps1 in ps1_files:
        sh_sibling = ps1.with_suffix(".sh")
        if sh_sibling.exists():
            continue
        # v0.2.54 Track E: also accept an extensionless bash wrapper as
        # a valid sibling. Used by templates/scripts/ (e.g. `kg-sync` +
        # `kg-sync.ps1`). See _has_extensionless_bash_sibling for the
        # contract.
        if _has_extensionless_bash_sibling(ps1):
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
    for changed_path in changed:
        if not (
            changed_path.startswith(scan_root_rel + "/")
            or changed_path == scan_root_rel
        ):
            continue
        p = Path(changed_path)
        if p.suffix not in (".sh", ".ps1"):
            continue
        rel_to_root = Path(changed_path).relative_to(scan_root_rel)
        if is_excluded(rel_to_root):
            continue

        sibling_suffix = ".ps1" if p.suffix == ".sh" else ".sh"
        sibling = p.with_suffix(sibling_suffix).as_posix()
        sibling_path_abs = repo_root / sibling
        source_path_abs = repo_root / changed_path
        if source_path_abs.exists() and has_magic_comment(source_path_abs):
            continue
        if sibling_path_abs.exists() and has_magic_comment(sibling_path_abs):
            continue

        if not sibling_path_abs.exists():
            continue

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

    return ps1_on_base


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0

    repo_root = Path(
        run_git(["rev-parse", "--show-toplevel"], cwd=Path.cwd())
    ).resolve()

    scan_roots = find_scan_roots(repo_root)
    if not scan_roots:
        annotate(
            "error",
            f"No scan roots found (looked for: {', '.join(SCAN_ROOTS)})",
        )
        return 2

    base_ref = os.environ.get("GITHUB_BASE_REF") or None

    try:
        changed = get_changed_files(repo_root, base_ref)
    except RuntimeError as e:
        annotate("error", f"Failed to compute changed files: {e}")
        return 2

    failures: list[str] = []
    warnings: list[str] = []

    print(
        f"Scanning {len(scan_roots)} root(s) for OS parity: "
        + ", ".join(r.relative_to(repo_root).as_posix() for r in scan_roots)
    )

    any_blocking = False
    for scan_root in scan_roots:
        is_blocking = _check_one_root(
            repo_root, scan_root, base_ref, changed, failures, warnings
        )
        any_blocking = any_blocking or is_blocking

    # Summary
    if not any_blocking and warnings:
        annotate(
            "warning",
            f"OS parity gate active but no .ps1 hooks/scripts exist yet on base "
            f"({len(warnings)} .sh file(s) without siblings) — this rule "
            f"starts blocking once the first .ps1 lands per scan root.",
        )

    if failures:
        print(f"\nHook OS-parity gate FAILED ({len(failures)} violation(s)).")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("Hook OS-parity gate passed.")
    if warnings and not any_blocking:
        print(
            f"({len(warnings)} .sh file(s) without .ps1 siblings — "
            "warn-only until first .ps1 lands per scan root.)"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        annotate("error", f"check_hook_parity.py crashed: {e}")
        sys.exit(2)
