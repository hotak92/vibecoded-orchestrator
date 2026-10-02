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

Callers: ``vco_lib.project_init._file_action`` (three decision points) and,
since v0.2.100 (WP-15), ``vco_lib.bundle_leftovers`` — the pass that removes a
file OUTSIDE the manifest only when it is provably a VCO artefact an earlier
release shipped. Both reach the same core, :func:`match_shipped_history`, so
"is this VCO's own artefact?" has one answer, not two.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from vco_lib.hashing import sha256_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vco_lib.project_init import _BundleFileOp

__all__ = [
    "ShippedMatch",
    "installed_matches_template_history",
    "match_shipped_history",
    "release_containing",
    "stale_shipped_artifact_reason",
]


@dataclass(frozen=True)
class ShippedMatch:
    """Which historical blob the installed bytes equal: the commit and the
    template path (relative to the orchestrator root, POSIX) it was read at."""

    commit: str
    template_rel: str


def match_shipped_history(
    orchestrator_root: Path,
    template_rels: Sequence[str],
    installed_hash: str,
    *,
    render: Optional[Callable[[bytes], bytes]] = None,
    all_refs: bool = False,
    oldest: bool = False,
    max_commits: int = 50,
) -> Optional[ShippedMatch]:
    """THE predicate: do ``installed_hash``'s bytes equal a version VCO itself
    shipped at any of ``template_rels`` (POSIX paths relative to
    ``orchestrator_root``, which may no longer exist in the working tree)?

    Walks the last ``max_commits`` commits touching any of the paths (``--all``
    refs when ``all_refs``), ``git show``s each blob raw and compares its
    sha-256 — and, when ``render`` is given, the sha-256 of the RENDERED blob,
    because a placeholder-substituted file (an agent, a skill page) was
    written rendered, never raw. A ``render`` that raises is treated as "no
    rendered form" for that blob.

    ``oldest=True`` walks the window oldest-first, so the match names the
    commit that FIRST shipped those bytes (the leftover report's "shipped by"
    release) rather than a later commit that merely moved them.

    ``None`` on every uncertain path — not a git checkout (tarball install),
    git absent, no history, nothing matched — so a caller can only ever act on
    a POSITIVE match. That is the safety property both callers rely on.
    """
    from vco_lib import git_meta as _git_meta

    if not template_rels or not orchestrator_root.is_dir():
        return None
    if not (orchestrator_root / ".git").exists():
        return None
    rels = [str(r).replace("\\", "/") for r in template_rels]
    log_args = ["log"] + (["--all"] if all_refs else []) + [
        f"-{max_commits}", "--pretty=format:%H", "--", *rels]
    rc, out, _err = _git_meta.run_git(orchestrator_root, log_args, timeout=5)
    if rc != 0:
        return None
    commits = [c.strip() for c in out.splitlines() if c.strip()]
    for sha in (reversed(commits) if oldest else commits):
        for rel in rels:
            b_rc, blob, _b_err = _git_meta.run_git_binary(
                orchestrator_root, ["show", f"{sha}:{rel}"], timeout=2)
            if b_rc != 0:
                continue
            if sha256_bytes(blob) == installed_hash:
                return ShippedMatch(sha, rel)
            if render is not None:
                try:
                    rendered = render(blob)
                except Exception:  # noqa: BLE001 — no rendered form for this blob
                    continue
                if sha256_bytes(rendered) == installed_hash:
                    return ShippedMatch(sha, rel)
    return None


def release_containing(orchestrator_root: Path, commit: str) -> str:
    """The first release tag containing ``commit`` (the release that SHIPPED
    it), else the short sha. Read-only, bounded; never raises."""
    from vco_lib import git_meta as _git_meta

    rc, out, _err = _git_meta.run_git(
        orchestrator_root,
        ["tag", "--contains", commit, "--sort=version:refname", "--list", "v*"],
        timeout=5,
    )
    if rc == 0:
        for line in out.splitlines():
            if line.strip():
                return line.strip()
    return commit[:10]


def installed_matches_template_history(
    template_source: Path,
    installed_hash: str,
    orchestrator_root: Path,
    *,
    also_paths: Sequence[str] = (),
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

    v0.2.100 (WP-15): a thin bool over :func:`match_shipped_history`, the one
    core the leftover pass shares. ``also_paths`` adds further template paths
    (POSIX, relative to the orchestrator root) whose history counts too — an
    ``_archive/`` location or a path since deleted.
    """
    if not orchestrator_root.is_dir():
        return False
    try:
        rel = template_source.resolve().relative_to(orchestrator_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    rels = [str(rel).replace("\\", "/"), *also_paths]
    return match_shipped_history(
        orchestrator_root, rels, installed_hash, max_commits=max_commits,
    ) is not None


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
