# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Is an installed file at a VCO-shipped destination VCO's OWN artifact?

Two predicates, one question, extracted from ``vco_lib.project_init`` in
v0.2.92 (the delivery audit's line-count ratchet on that module is what forced
the extraction, and it was right to: this is a self-contained concern with no
dependency on the installer's mutable state).

* :func:`installed_matches_template_history` — v0.2.31. Do the installed bytes
  hash-match ANY historical version of the shipped template? Then VCO wrote
  them itself, in an earlier release.
* :func:`stale_shipped_artifact_reason` — v0.2.92. The classification rule the
  bundle installer applies to a PRE-EXISTING file on FIRST install: is it
  provably a stale VCO artifact (adopt it, with a backup) or is it the user's
  own work (leave it alone)?

Callers: ``vco_lib.project_init._file_action`` only, at three decision points.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from vco_lib.hashing import sha256_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vco_lib.project_init import _BundleFileOp

__all__ = [
    "installed_matches_template_history",
    "stale_shipped_artifact_reason",
]


def installed_matches_template_history(
    template_source: Path,
    installed_hash: str,
    orchestrator_root: Path,
    *,
    max_commits: int = 50,
) -> bool:
    """v0.2.31 heal: did this file's installed sha match ANY historical
    version of the template under `templates/`? If yes, the file was
    shipped by VCO at some point — the user hasn't edited it, it's just
    stale. Safe to overwrite.

    Bounded git-log walk on the template path. Looks at `git log -p`
    for the path, hashes each historical blob's content, and compares.

    Returns False (= preserve as user-modified) on any error path:
      - orchestrator_root isn't a git repo (tarball install)
      - git isn't on PATH
      - template path not under orchestrator_root
      - git log returns no history (new file not yet committed)

    `max_commits` caps the walk depth (~6 months at typical release
    cadence for this repo). Adjust upward if false-preserves happen.

    Note: this helper covers ``project_init._file_action``'s "no prior_hash in
    manifest but file exists on disk" case introduced by adding new
    files to the bundle without retro-actively updating manifests on
    existing installs. The discipline for genuinely user-modified
    files (= file content never matched any shipped version) is
    unchanged — those still take the preserve path.

    v0.2.92 (WP-D, recipe from the R28 re-audit): the two inline
    ``subprocess.run(["git", …])`` spawns now run through
    :mod:`vco_lib.git_meta` — the text-mode ``git log`` via
    :func:`git_meta.run_git`, the BINARY ``git show <sha>:<path>`` via
    :func:`git_meta.run_git_binary`. The split is load-bearing: the blob
    bytes are sha-256-hashed raw, and the text runner's
    ``errors="replace"`` decode would silently mangle non-UTF-8 content
    before the hash — an adopt decision made against corrupted bytes.
    """
    from vco_lib import git_meta as _git_meta

    if not orchestrator_root.is_dir():
        return False
    git_dir = orchestrator_root / ".git"
    if not git_dir.exists():
        # Tarball install or non-git source tree. Can't walk history.
        return False
    try:
        rel = template_source.resolve().relative_to(orchestrator_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    rel_str = str(rel).replace("\\", "/")
    # `git log --format=%H` over the path → list of commits touching it.
    # We then `git show <sha>:<path>` for each and sha-256 the bytes.
    rc, out, _err = _git_meta.run_git(
        orchestrator_root,
        ["log", f"-{max_commits}", "--pretty=format:%H", "--", rel_str],
        timeout=5,
    )
    if rc != 0:
        return False
    commits = [c.strip() for c in out.splitlines() if c.strip()]
    if not commits:
        # File never had a commit touching it under this path. Could be
        # legitimately new (uncommitted) or moved/renamed; fall through
        # to default-preserve.
        return False
    for sha in commits:
        b_rc, blob, _b_err = _git_meta.run_git_binary(
            orchestrator_root,
            ["show", f"{sha}:{rel_str}"],
            timeout=2,
        )
        if b_rc != 0:
            continue
        if sha256_bytes(blob) == installed_hash:
            return True
    return False


def stale_shipped_artifact_reason(
    op: "_BundleFileOp",
    target_path: Path,
    source_bytes: bytes,
    installed_hash: str,
    orchestrator_root: Optional[Path],
) -> Optional[str]:
    """Is this pre-existing file provably a STALE VCO-SHAPED ARTIFACT rather
    than user work? Returns a short reason, or ``None`` when undecidable.

    v0.2.92 (field bug, 2026-09-05). A project added with **safe add** whose
    `.claude/scripts/` held pre-VCO wrappers got every one of them classified
    `skip-existing` → `bundle_skipped_existing_files`, whose declared
    disposition is `informational_record` ("nothing is pending; do not surface
    it as a problem"). The wrappers pointed at ANOTHER project's venv and
    ANOTHER project's collection default, so the project's KG build failed
    outright (`ModuleNotFoundError: No module named 'weaviate'`, 329/329 nodes
    failed) while the ledger said nothing was wrong.

    "Differs from the shipped bytes" is NOT evidence of user authorship. This
    predicate names the two cases where the opposite is PROVABLE, and only
    those:

    1. ``old-shipped-version`` — the installed bytes hash-match some
       historical shipped version of this very template
       (:func:`installed_matches_template_history`). VCO wrote those bytes
       itself, in an earlier release. Adopting them back cannot destroy user
       work because there is none.
    2. ``missing-resilience-marker`` — the SHIPPED template carries the
       ``$VCT_INSTALL_ROOT`` interpreter-discovery ladder and the installed
       copy does not. Such a copy predates RT-4 (2026-06-27) or predates VCO
       entirely; it cannot reach THIS install's venv, and the pre-VCO
       generation of these wrappers additionally defaults ``KG_COLLECTION``
       to a foreign collection name — so running one writes another project's
       knowledge graph. It is broken-for-this-install by construction, and
       that is a property of the file, not a judgement about its author.

    Ordering is deliberate: rule 2 is a substring scan over bytes already in
    memory; rule 1 spawns bounded `git log`/`git show`. Cheap test first.
    Measured on the field project (21 divergent files, 24 commits deep on the
    slowest template): under a second for the whole classification pass.

    LIMITS, stated rather than credited away. Rule 1 needs the orchestrator to
    be a git CHECKOUT — on a tarball install `installed_matches_template_history`
    returns False and only rule 2 is available. Rule 2 is a substring scan, so
    a file whose ladder was replaced by a comment naming it would pass as
    healthy. Both errors point the same safe way: the predicate can FAIL TO
    NOTICE a stale file (which leaves today's preserve behaviour), it cannot
    condemn a healthy one.

    Everything else returns ``None`` and keeps today's preserve/skip
    behaviour — a hand-written hook full of project-specific logic is user
    work and stays untouched. ``knowledge/**`` never reaches here (the caller
    carves it out first; adopting a KG node would destroy user knowledge).
    """
    from vco_lib import wrapper_health as _wh

    # Rule 2 — broken-for-this-install by the shipped file's own invariant.
    # `path_is_resilient` is conservative on a read error (⇒ "not resilient"),
    # which at worst routes the file to `adopt`; the adopt branch reads the
    # bytes for its backup and falls back to `preserve` if that read fails, so
    # an unreadable file is never overwritten uncaptured.
    if _wh.bytes_are_resilient(source_bytes) and not _wh.path_is_resilient(target_path):
        return "missing-resilience-marker"

    # Rule 1 — provably a version VCO itself shipped, just an older one.
    if orchestrator_root is not None and installed_matches_template_history(
        op.source_abs, installed_hash, orchestrator_root
    ):
        return "old-shipped-version"

    return None
