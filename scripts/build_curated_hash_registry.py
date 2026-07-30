#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Build the curated-knowledge provenance registry (v0.2.89 §7.2).

Release-time tool. Walks the git history of the curated bundled KG set and
emits ``templates/knowledge/.curated_hashes.json``::

    {
      "schema_version": 1,
      "generated_at": "<ISO-8601 UTC>",
      "files": {"concepts/foo.md": ["<sig1>", "<sig2>", ...], ...}
    }

Each sig is the STORAGE-LAYER content signature
(:func:`vco_lib.knowledge_residue.content_signature_excluding_updated` —
full sha256, tolerant ONLY of the machine-written ``updated:`` frontmatter
line). The registry is consumed by
``vco_lib.knowledge_residue.cleanup_bundled_knowledge_residue`` to prove a
user project's ``knowledge/`` file is a bundled, unmodified residue copy
before deleting it.

History coverage (§7.2):

* ``templates/knowledge/**`` — every committed blob version since the
  V52-C move.
* Legacy pre-V52-C ``knowledge/**`` — the curated set lived in-tree there;
  its blob versions are harvested TOO, but ONLY for rel paths that also
  appear (at any point in history, or in the current worktree) under
  ``templates/knowledge/**``. This scoping keeps orchestrator-root-project
  nodes (post-V52-C ``knowledge/**`` commits — never shipped per-project)
  OUT of the registry: without it, a user's manually-copied root node
  could be deleted as "residue". Curated files deleted BEFORE V52-C (rel
  path never present under templates/knowledge) are a conservative miss —
  their residue copies stay on disk.
* The CURRENT WORKTREE state of ``templates/knowledge/**/*.md`` is also
  included, so "edit node → regenerate registry → commit together" keeps
  the freshness invariant test green pre-commit.

If the published history is shallow/squashed the registry simply covers
fewer historical versions — a conservative miss (files stay on disk); the
tool prints the history depth so the release runner can see the coverage.

Usage::

    python scripts/build_curated_hash_registry.py            # build + write
    python scripts/build_curated_hash_registry.py --check    # verify fresh
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.atomic import atomic_write_text  # noqa: E402
from vco_lib.knowledge_residue import (  # noqa: E402
    REGISTRY_BASENAME,
    content_signature_excluding_updated,
)

_TEMPLATES_PREFIX = "templates/knowledge/"
_LEGACY_PREFIX = "knowledge/"


def _git(repo_root: Path, *args: str) -> str:
    """Run a git command; loud-fail (this is a release tool, not a client
    soft-fail path)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode})"
        )
    return proc.stdout


def _history_blob_index(repo_root: Path) -> dict[str, dict[str, set]]:
    """Map ``{"templates": {rel: {blob_sha,...}}, "legacy": {...}}`` across
    every commit (``--all``) that touched either knowledge path."""
    commits = _git(
        repo_root, "rev-list", "--all", "--", "templates/knowledge", "knowledge",
    ).split()
    index: dict[str, dict[str, set]] = {"templates": {}, "legacy": {}}
    for commit in commits:
        listing = _git(
            repo_root, "ls-tree", "-r", commit,
            "--", "templates/knowledge", "knowledge",
        )
        for line in listing.splitlines():
            # <mode> <type> <sha>\t<path>
            try:
                meta, path = line.split("\t", 1)
                _mode, obj_type, sha = meta.split()
            except ValueError:
                continue
            if obj_type != "blob" or not path.endswith(".md"):
                continue
            path = path.replace("\\", "/")
            if path.startswith(_TEMPLATES_PREFIX):
                rel = path[len(_TEMPLATES_PREFIX):]
                index["templates"].setdefault(rel, set()).add(sha)
            elif path.startswith(_LEGACY_PREFIX):
                rel = path[len(_LEGACY_PREFIX):]
                index["legacy"].setdefault(rel, set()).add(sha)
    return index


def build_registry(repo_root: Path) -> tuple[dict, dict]:
    """Return ``(registry_payload, stats)``."""
    index = _history_blob_index(repo_root)

    # The curated rel-path universe: everything ever under
    # templates/knowledge/ plus the current worktree files.
    worktree_root = repo_root / "templates" / "knowledge"
    worktree_files = sorted(
        f for f in worktree_root.rglob("*.md") if f.is_file()
    ) if worktree_root.is_dir() else []
    universe = set(index["templates"])
    universe.update(
        str(f.relative_to(worktree_root)).replace("\\", "/")
        for f in worktree_files
    )

    files: dict[str, set] = {rel: set() for rel in sorted(universe)}
    blob_cache: dict[str, str] = {}
    blobs_hashed = 0

    def _sig_for_blob(sha: str) -> str:
        nonlocal blobs_hashed
        if sha not in blob_cache:
            raw = _git_bytes(repo_root, "cat-file", "blob", sha)
            blob_cache[sha] = content_signature_excluding_updated(
                raw.decode("utf-8", errors="replace")
            )
            blobs_hashed += 1
        return blob_cache[sha]

    legacy_versions = 0
    for rel in sorted(universe):
        for sha in sorted(index["templates"].get(rel, ())):
            files[rel].add(_sig_for_blob(sha))
        # Legacy leg — scoped to the templates universe (see module doc).
        for sha in sorted(index["legacy"].get(rel, ())):
            files[rel].add(_sig_for_blob(sha))
            legacy_versions += 1

    # Current worktree state (uncommitted edits regenerate cleanly).
    for f in worktree_files:
        rel = str(f.relative_to(worktree_root)).replace("\\", "/")
        files[rel].add(content_signature_excluding_updated(
            f.read_text(encoding="utf-8", errors="replace")
        ))

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "files": {rel: sorted(sigs) for rel, sigs in sorted(files.items())},
    }

    total_commits = len(_git(repo_root, "rev-list", "--all").split())
    knowledge_commits = len(_git(
        repo_root, "rev-list", "--all",
        "--", "templates/knowledge", "knowledge",
    ).split())
    shallow = _git(
        repo_root, "rev-parse", "--is-shallow-repository",
    ).strip() == "true"
    stats = {
        "files_covered": len(payload["files"]),
        "total_signatures": sum(len(v) for v in payload["files"].values()),
        "unique_blobs_hashed": blobs_hashed,
        "legacy_blob_versions": legacy_versions,
        "legacy_rel_paths_in_universe": len(
            set(index["legacy"]) & universe
        ),
        "legacy_rel_paths_excluded": len(
            set(index["legacy"]) - universe
        ),
        "knowledge_commits_scanned": knowledge_commits,
        "repo_total_commits": total_commits,
        "shallow_repository": shallow,
    }
    return payload, stats


def _render(payload: dict) -> str:
    return json.dumps(payload, indent=1, sort_keys=True) + "\n"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT),
        help="Orchestrator repo root (default: this script's repo)",
    )
    parser.add_argument(
        "--output", default=None,
        help=f"Output path (default: templates/knowledge/{REGISTRY_BASENAME})",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Regenerate in memory and diff against the committed registry "
             "(exit 1 on drift); ignores generated_at",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    out_path = (
        Path(args.output)
        if args.output
        else repo_root / "templates" / "knowledge" / REGISTRY_BASENAME
    )

    payload, stats = build_registry(repo_root)

    print("curated-hash registry stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["shallow_repository"]:
        print(
            "  WARNING: shallow repository — the registry covers fewer "
            "historical versions (conservative: unmatched residue stays "
            "on disk)."
        )

    if args.check:
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"CHECK FAILED: cannot read {out_path}: {exc}",
                  file=sys.stderr)
            return 1
        if existing.get("files") != payload["files"] or (
            existing.get("schema_version") != payload["schema_version"]
        ):
            print(
                f"CHECK FAILED: {out_path} is stale — regenerate with "
                f"`python scripts/build_curated_hash_registry.py`",
                file=sys.stderr,
            )
            return 1
        print(f"check OK: {out_path} is fresh")
        return 0

    atomic_write_text(out_path, _render(payload))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
