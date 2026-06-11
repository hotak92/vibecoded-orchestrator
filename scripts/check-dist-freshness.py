#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""check-dist-freshness.py — assert launcher/dist/ sidecars match live source.

v0.2.54 G-1.5 (Wave 0 follow-up to the v0.2.53 stale-dist foot-gun).

Each bundled binary under ``launcher/dist/<os-arch>/`` ships with a
``<binary>.metadata.json`` sidecar whose ``source_hash`` field captures the
launcher-subtree tree hash the binary was built from (writer:
``scripts/build-bundled-launcher.sh``; readers:
``scripts/post-install-launcher.sh::_bundled_binary_is_fresh`` and the
PowerShell mirror in ``first-install.bat``).

At the v0.2.53 tag, all three OS sidecars carried ``source_hash fd215c7a``
(built at v0.2.52) while the live tree hash was ``449e6cc6``. Every fresh
clone therefore rejected the bundled binary as stale and fell through to
the GitHub-release download — which was ALSO broken on Windows
(zip-vs-exe asset filter, fixed alongside this script) — landing every
Windows first-run in the 15-30 min source-build worst case.

This script is the regression gate for that condition. It:

1. Computes the live launcher-subtree hash exactly as the build script
   and the install-time readers do::

       git ls-tree HEAD launcher/src-tauri/src/ launcher/src/ \\
           launcher/src-tauri/Cargo.toml launcher/src-tauri/Cargo.lock \\
           launcher/package.json | git hash-object --stdin

2. Reads every ``*.metadata.json`` under the dist dir (or ``--dist-dir``
   override, e.g. the ``_dist-artifacts/`` staging tree in the
   commit-dist-binaries release job).

3. Compares each sidecar's ``source_hash`` to the live hash, plus a
   cross-OS consistency check (all sidecars must agree with each other —
   a disagreement means the three OS builds ran from different refs,
   the v0.2.49 mislabeled-binaries failure class).

Modes (``--mode``):
    warn    (default) mismatches print warnings; exit 0. For PR-time /
            tag-time visibility under the CURRENT release flow, where
            in-tree dist binaries are refreshed POST-release by the
            commit-dist-binaries job and are therefore expected to lag
            HEAD whenever launcher source changed since the last release.
    strict  mismatches exit 1. For contexts where freshness is an
            invariant: the commit-dist-binaries job AFTER staging the
            freshly-built artifacts, and (once Track E moves the binary
            refresh to PRE-tag) the pre-release gate.

``--github`` emits ``::warning::`` / ``::error::`` workflow annotations.

Track E integration note (v0.2.54 CI hardening): this script is the
single source of truth for the freshness predicate. release.yml invokes
it twice — Gate 3b (pre-release-gate, ``--mode warn``) and the
commit-dist-binaries post-staging check (``--mode strict``
``--dist-dir _dist-artifacts``). When the release flow is reworked to
refresh binaries before tagging, flip Gate 3b to ``--mode strict`` and
delete this paragraph.

Exit codes:
    0  all sidecars fresh (or warn mode)
    1  strict mode and at least one stale/inconsistent/missing sidecar
    2  environment error (not a git repo, git missing, no sidecars found)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Must stay byte-for-byte in sync with scripts/build-bundled-launcher.sh
# (SOURCE_HASH computation) and the readers in post-install-launcher.sh +
# first-install.bat. tests/test_check_dist_freshness.py pins the parity.
LAUNCHER_SUBTREE_PATHS = [
    "launcher/src-tauri/src/",
    "launcher/src/",
    "launcher/src-tauri/Cargo.toml",
    "launcher/src-tauri/Cargo.lock",
    "launcher/package.json",
]

# OS dirs whose sidecars gate the install path. experimental_macOS is a
# legacy local-maintainer slot and intentionally excluded.
DIST_OS_DIRS = ["linux-x64", "macos-arm64", "windows-x64"]


def compute_live_hash(repo_root: Path) -> str | None:
    """Live launcher-subtree hash at HEAD, or None when not computable."""
    try:
        ls_tree = subprocess.run(
            ["git", "ls-tree", "HEAD", *LAUNCHER_SUBTREE_PATHS],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if ls_tree.returncode != 0:
            return None
        hashed = subprocess.run(
            ["git", "hash-object", "--stdin"],
            cwd=repo_root,
            input=ls_tree.stdout,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if hashed.returncode != 0:
            return None
        return hashed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def collect_sidecars(dist_dir: Path) -> list[Path]:
    """All binary metadata sidecars under the known OS dirs of dist_dir.

    Falls back to a recursive glob when none of the canonical OS dirs
    exist (e.g. the flattened ``_dist-artifacts/<target>/`` staging
    layout in the commit-dist-binaries job).
    """
    sidecars: list[Path] = []
    for os_dir in DIST_OS_DIRS:
        d = dist_dir / os_dir
        if d.is_dir():
            sidecars.extend(sorted(d.glob("*.metadata.json")))
    if not sidecars:
        sidecars = sorted(dist_dir.glob("**/*.metadata.json"))
    return sidecars


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of this script's directory).",
    )
    ap.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Directory holding the per-OS binary dirs "
        "(default: <repo-root>/launcher/dist). Override to validate a "
        "CI artifact staging tree, e.g. _dist-artifacts/.",
    )
    ap.add_argument(
        "--mode",
        choices=["warn", "strict"],
        default="warn",
        help="warn: report mismatches, exit 0. strict: mismatches exit 1.",
    )
    ap.add_argument(
        "--github",
        action="store_true",
        help="Emit ::warning::/::error:: GitHub Actions annotations.",
    )
    args = ap.parse_args(argv)

    repo_root = (
        args.repo_root.resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )
    dist_dir = (
        args.dist_dir.resolve() if args.dist_dir else repo_root / "launcher" / "dist"
    )

    def emit(level: str, msg: str) -> None:
        if args.github:
            print(f"::{level}::{msg}")
        else:
            print(f"[dist-freshness] {level.upper()}: {msg}")

    live_hash = compute_live_hash(repo_root)
    if not live_hash:
        emit(
            "error",
            f"cannot compute live launcher-subtree hash at {repo_root} "
            "(git missing, or not a git checkout with the launcher subtree).",
        )
        return 2

    if not dist_dir.is_dir():
        emit("error", f"dist dir not found: {dist_dir}")
        return 2

    sidecars = collect_sidecars(dist_dir)
    if not sidecars:
        emit("error", f"no *.metadata.json sidecars found under {dist_dir}")
        return 2

    print(f"[dist-freshness] live launcher-subtree hash: {live_hash}")
    stale: list[str] = []
    seen_hashes: dict[str, list[str]] = {}
    for sc in sidecars:
        rel = sc.relative_to(dist_dir) if sc.is_relative_to(dist_dir) else sc
        try:
            meta = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            stale.append(f"{rel}: unreadable metadata ({exc})")
            continue
        sc_hash = meta.get("source_hash") or ""
        if not sc_hash:
            # Lenient like the shell reader: metadata predating the
            # source_hash field can't be verified — report, don't fail.
            print(f"[dist-freshness]   {rel}: no source_hash field (skipped)")
            continue
        seen_hashes.setdefault(sc_hash, []).append(str(rel))
        status = "fresh" if sc_hash == live_hash else "STALE"
        built_at = meta.get("built_at", "?")
        version = meta.get("launcher_version", meta.get("version", "?"))
        print(
            f"[dist-freshness]   {rel}: {status} "
            f"(source_hash={sc_hash[:8]}, built_at={built_at}, version={version})"
        )
        if sc_hash != live_hash:
            stale.append(
                f"{rel}: source_hash {sc_hash[:8]} != live {live_hash[:8]} "
                f"(binary built {built_at} at version {version})"
            )

    if len(seen_hashes) > 1:
        detail = "; ".join(
            f"{h[:8]} -> {', '.join(paths)}" for h, paths in seen_hashes.items()
        )
        stale.append(
            f"cross-OS inconsistency: sidecars disagree on source_hash ({detail}) — "
            "the per-OS binaries were built from different refs."
        )

    if not stale:
        print("[dist-freshness] OK: all bundled-binary sidecars match live source.")
        return 0

    for s in stale:
        emit("error" if args.mode == "strict" else "warning", s)
    summary = (
        f"{len(stale)} stale/inconsistent dist sidecar finding(s). "
        "Fresh clones will reject the bundled binary and fall back to the "
        "GitHub-release download (working) — but the tag ships binaries that "
        "do not match its source. Refresh via "
        "`bash scripts/build-bundled-launcher.sh` per OS, or let the "
        "commit-dist-binaries release job land the rebuilt set."
    )
    if args.mode == "strict":
        emit("error", f"DIST-FRESHNESS GATE FAIL: {summary}")
        return 1
    emit("warning", f"dist-freshness (advisory): {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
