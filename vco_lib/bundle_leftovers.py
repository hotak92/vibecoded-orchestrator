# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What a bundle update does with files it no longer ships — the ONE home of
the leftovers policy (v0.2.100, WP-15: L5-F06, owner Q3).

Two rules, both applied by :func:`vco_lib.project_init.install_project_bundle`
after its orphan pass, both recorded as ``informational_record`` rows (a
completed action — nothing is pending):

* :func:`remove_vco_leftovers` — a file under a managed ``.claude/`` kind
  (hooks, scripts, agents, skills) that is in NEITHER the bundle manifest NOR
  this run's enumeration is removed **only** when its bytes equal a version
  VCO itself shipped at a template path that no longer ships (deleted since,
  or moved to ``templates/agents/_archive/``). The question "is this VCO's own
  artefact?" is answered by :func:`vco_lib.shipped_artifact.match_shipped_history`
  — the same core ``_file_action`` uses — so there is one answer. A match is
  backed up to ``.claude/backups/bundle-adoptions/<ts>/`` and removed, and the
  row names the file, its backup and the release that shipped it. No match →
  the file is the user's: untouched and unreported. A path whose template
  still exists in the tree (a gated-off gateway agent, a module-delivered
  ``mao`` agent) is never a candidate — "not delivered this run" is a
  decision, not a retirement. ``knowledge/**``, ``.claude/state``,
  ``.claude/context`` and the ``*.disabled`` locations are never walked. A
  tarball install (no git history) cannot prove anything, so the pass is a
  no-op with one log line.

* :func:`retire_disabled_orphan` — v0.2.101 (catalogue plan §2.3): the
  orphan pass's disabled-side arm. A retired agent/skill whose copy the
  launcher's toggle moved to ``.claude/{agents,skills}.disabled/`` is backed
  up (when edited) and removed there too — the disable choice does not make a
  VCO file the user's, and the walk above never descends into ``.disabled``.

* :func:`retire_compose_copies` — owner Q3: project bundles no longer ship
  copies of the orchestrator's compose files (the hooks resolve the compose
  directory from the orchestrator root). Every manifest-tracked
  ``infrastructure/*`` copy in a NON-root project is removed; a copy whose
  bytes differ from what VCO shipped is backed up first. Both are reported.
  At the orchestrator root those paths ARE the live compose files, so the
  caller only drops their manifest entries there and never calls this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

__all__ = [
    "COMPOSE_BUCKET",
    "LeftoverOutcome",
    "is_compose_copy",
    "remove_vco_leftovers",
    "retire_compose_copies",
    "retire_disabled_orphan",
    "run_leftover_policy",
]

#: The destination bucket the bundle used to fill with compose copies.
COMPOSE_BUCKET = "infrastructure/"

#: Managed `.claude/<kind>/` directories → the template directory (or, for
#: agents, directories) their files came from. Agents list every directory a
#: bundle agent has ever shipped from; ``mao/`` is absent on purpose (those
#: are module-delivered, not bundle-delivered). ``specializations`` joined in
#: v0.2.101 (catalogue plan §5 — plain-doc kind, recursive like skills).
#: Pack members (``templates/packs/**``) install to the agents/skills kinds
#: and are manifest-tracked, so they orphan-process, never leftover-process.
_KIND_SOURCES: dict = {
    "hooks": ("templates/hooks",),
    "scripts": ("templates/scripts",),
    "skills": ("templates/skills",),
    "specializations": ("templates/specializations",),
    "agents": ("templates/agents/free", "templates/agents/module-gateway",
               "templates/agents/_archive", "templates/agents"),
}
_ARCHIVE_MARK = "/_archive/"
_SKIP_DIR_NAMES = frozenset({"__pycache__"})


@dataclass
class LeftoverOutcome:
    """What one pass did: ``(dest_rel, backup_rel | None, detail)`` rows."""

    removed: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def is_compose_copy(dest_rel: str) -> bool:
    """True for a manifest key in the retired compose-copy bucket
    (separator-normalised: Windows manifest keys carry ``\\``)."""
    return dest_rel.replace("\\", "/").startswith(COMPOSE_BUCKET)


def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def _candidates(folder: Path, known: set, skip_kinds: frozenset) -> Iterable[tuple]:
    """``(kind, dest_rel)`` for every file under the managed kinds that the
    run does not know. Symlinks are never followed or touched."""
    claude = folder / ".claude"
    for kind in _KIND_SOURCES:
        if kind in skip_kinds:
            continue
        base = claude / kind
        if not base.is_dir() or base.is_symlink():
            continue
        for path in sorted(base.rglob("*")):
            rel_parts = path.relative_to(folder).parts
            if any(p in _SKIP_DIR_NAMES for p in rel_parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if any(folder.joinpath(*rel_parts[:i]).is_symlink()
                   for i in range(2, len(rel_parts))):
                continue
            dest_rel = "/".join(rel_parts)
            if dest_rel in known:
                continue
            if kind == "agents" and (len(rel_parts) != 3 or path.suffix != ".md"):
                continue  # only `.claude/agents/<name>.md` was ever shipped
            yield kind, dest_rel


def _historical_template_paths(root: Path) -> Optional[set]:
    """Every path git has ever recorded under the managed template dirs, or
    ``None`` when history is unreadable."""
    from vco_lib import git_meta as _git_meta

    rc, out, _err = _git_meta.run_git(
        root,
        ["log", "--all", "--format=", "--name-only", "--",
         "templates/hooks", "templates/scripts", "templates/agents", "templates/skills",
         # v0.2.101: the specialisations docs kind. `templates/packs` is NOT a
         # pathspec: a pack member's template path embeds the pack name, which
         # a dest_rel cannot derive, so `_retired_sources` could never consult
         # it. Pack members are manifest-tracked (they retire through the
         # orphan pass); an unmanifested hand-restored copy stays untouched —
         # treated as the user's file, the safe outcome.
         "templates/specializations"],
        timeout=30,
    )
    if rc != 0:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def _retired_sources(root: Path, kind: str, dest_rel: str, history: set) -> list:
    """Historical template paths for ``dest_rel`` that no longer ship: deleted
    from the tree, or kept only under ``_archive/``."""
    tail = PurePosixPath(dest_rel).parts[2:]  # drop `.claude/<kind>`
    out = []
    for src_dir in _KIND_SOURCES[kind]:
        rel = "/".join((src_dir, *tail))
        if rel not in history:
            continue
        if _ARCHIVE_MARK in f"/{rel}" or not (root / rel).exists():
            out.append(rel)
    return out


def _md_render(root: Path, folder: Path, dest_rel: str) -> Optional[Callable[[bytes], bytes]]:
    """The render a shipped Markdown page went through, side-effect free (no
    findings sink), so a historical blob can be compared with its install."""
    if not dest_rel.endswith(".md"):
        return None
    from vco_lib import materialize as _mz

    transform = _mz.Transform(dest_rel, _mz.BUNDLE_MARKDOWN_SPEC,
                              _mz.MaterializeContext(root, folder))
    return lambda raw: transform.render(raw)[0]


def _backup_and_remove(folder: Path, dest_rel: str, ts: str, *, backup: bool) -> Optional[str]:
    """Back ``dest_rel`` up (when asked) THEN unlink it; prune now-empty
    parents. Raises on any failure BEFORE the unlink — never removes bytes
    without the captured copy the caller promised."""
    from vco_lib.fs_prune import prune_now_empty_parents
    from vco_lib.project_init import _backup_bytes_for_adoption  # the patch point

    target = folder / dest_rel
    backup_rel = None
    if backup:
        backup_rel = _backup_bytes_for_adoption(folder, dest_rel, ts, target.read_bytes())
    target.unlink()
    prune_now_empty_parents(folder, target.parent)
    return backup_rel


def _emit(folder: Path, cid: str, title: str, rows: list, why: str) -> None:
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralEntry

    lines = "\n".join(
        f"- `{rel}` — {detail}" + (f"; backup: `{bk}`" if bk else "")
        for rel, bk, detail in rows
    )
    _de.emit(folder, DeferralEntry(
        condition_id=cid,
        title=title,
        detected=f"{len(rows)} file(s):\n{lines}",
        why_deferred=why,
        command_to_apply=(
            "# Nothing to do. To bring one back, copy its backup to its old path:\n"
            "#   cp <backup> <file>"
        ),
        severity="info",
    ))


def remove_vco_leftovers(
    folder: Path,
    orchestrator_root: Path,
    *,
    known_rels: Iterable[str],
    skip_kinds: frozenset = frozenset(),
    backup_ts: Callable[[], str],
    dry_run: bool = False,
    log: Callable[[str], None] = lambda _m: None,
) -> LeftoverOutcome:
    """Remove (with a backup) every managed-kind file outside the manifest
    that is provably an artefact an earlier VCO release shipped and later
    retired. See the module docstring for the rule and its limits."""
    from vco_lib.hashing import sha256_file
    from vco_lib.shipped_artifact import match_shipped_history, release_containing

    outcome = LeftoverOutcome()
    folder = Path(folder)
    root = Path(orchestrator_root)
    known = {_posix(r) for r in known_rels}
    candidates = list(_candidates(folder, known, skip_kinds))
    if not candidates:
        return outcome
    if not (root / ".git").exists():
        log(f"leftover check skipped: {root} has no git history (tarball install) — "
            f"{len(candidates)} file(s) outside the manifest left untouched")
        return outcome
    history = _historical_template_paths(root)
    if history is None:
        log("leftover check skipped: git history unreadable — files left untouched")
        return outcome
    for kind, dest_rel in candidates:
        sources = _retired_sources(root, kind, dest_rel, history)
        if not sources:
            continue
        try:
            installed = sha256_file(folder / dest_rel)
        except OSError:
            continue
        match = match_shipped_history(root, sources, installed, all_refs=True, oldest=True,
                                      render=_md_render(root, folder, dest_rel))
        if match is None:
            continue  # the user's file — untouched, unreported
        detail = (f"shipped by VCO {release_containing(root, match.commit)} "
                  f"as `{match.template_rel}`, no longer shipped")
        if dry_run:
            outcome.removed.append((dest_rel, None, detail))
            continue
        try:
            backup_rel = _backup_and_remove(folder, dest_rel, backup_ts(), backup=True)
        except Exception as exc:  # noqa: BLE001 — reported; file left in place
            outcome.errors.append((dest_rel, f"{type(exc).__name__}: {exc}"))
            continue
        outcome.removed.append((dest_rel, backup_rel, detail))
    if outcome.removed and not dry_run:
        _emit(folder, "bundle_leftover_removed",
              "Retired VCO files removed (backed up)", outcome.removed,
              "An earlier VCO release shipped these files and a later one retired "
              "them; they were outside the bundle manifest, so no update removed "
              "them. Their bytes match what VCO shipped, so they were not yours. "
              "Each was backed up before removal. Files that match nothing VCO "
              "ever shipped are treated as yours and were not touched.")
    return outcome


def retire_disabled_orphan(
    folder: Path,
    prior_rel: str,
    prior_entry: Optional[dict],
    *,
    backup_ts: Callable[[], str],
    dry_run: bool = False,
) -> list:
    """v0.2.101 catalogue plan §2.3 — the disabled side of a retired orphan.

    A manifest-tracked agent/skill that upstream retired may sit at its
    DISABLED location (the launcher GUI's enable/disable toggle MOVES files
    between ``.claude/{agents,skills}/`` and ``.claude/{agents,skills}.disabled/``).
    The enabled side is already gone — that is why the orphan loop's case (a)
    runs — and the leftover pass never walks ``.disabled`` locations, so
    without this step the copy would linger forever (the gap §2.3 names). The
    user's choice was DISABLE and the file was VCO's: backup-if-modified +
    remove, the ``--remove-pack`` rule. Unmodified (manifest hash match) →
    removed outright; edited (or untracked) → backed up under
    ``.claude/backups/bundle-adoptions/<ts>/`` first; a backup failure leaves
    the file in place (the ``_backup_and_remove`` contract). Symlinks are
    never touched. Returns ``(dest_rel, backup_rel | None, detail)`` rows —
    empty when there is no disabled-side copy (the ordinary case (a)).
    """
    from vco_lib.bundle_kinds import classify_bundle_op_kind, disabled_counterpart
    from vco_lib.hashing import sha256_file

    if classify_bundle_op_kind(prior_rel) is None:
        return []
    dis_rel = disabled_counterpart(prior_rel)
    if dis_rel is None:
        return []
    target = Path(folder) / dis_rel
    if target.is_symlink() or not target.is_file():
        return []
    prior_hash = (prior_entry or {}).get("sha256", "")
    try:
        modified = prior_hash == "" or sha256_file(target) != prior_hash
    except OSError:
        return []  # unreadable → default to safety: leave it alone
    detail = ("retired VCO copy on the disabled side — your edits backed up, "
              "then removed" if modified else
              "retired VCO copy on the disabled side — removed")
    if dry_run:
        return [(dis_rel, None, detail)]
    try:
        backup_rel = _backup_and_remove(Path(folder), dis_rel, backup_ts(),
                                        backup=modified)
    except Exception:  # noqa: BLE001 — never remove without the promised copy
        return []
    return [(dis_rel, backup_rel, detail)]


def retire_compose_copies(
    folder: Path,
    prior_entries: dict,
    *,
    backup_ts: Callable[[], str],
    dry_run: bool = False,
) -> LeftoverOutcome:
    """Owner Q3: remove the project's manifest-tracked compose copies — an
    unmodified copy (manifest hash match) outright, a modified one after a
    backup under ``.claude/backups/bundle-adoptions/<ts>/infrastructure/``."""
    from vco_lib.hashing import sha256_file

    outcome = LeftoverOutcome()
    for dest_rel, entry in sorted(prior_entries.items()):
        target = folder / dest_rel
        if target.is_symlink() or not target.is_file():
            continue
        try:
            modified = sha256_file(target) != (entry or {}).get("sha256", "")
        except OSError as exc:
            outcome.errors.append((dest_rel, f"{type(exc).__name__}: {exc}"))
            continue
        detail = ("your edited copy — backed up, then removed" if modified
                  else "unmodified VCO copy — removed")
        if dry_run:
            outcome.removed.append((_posix(dest_rel), None, detail))
            continue
        try:
            backup_rel = _backup_and_remove(folder, dest_rel, backup_ts(), backup=modified)
        except Exception as exc:  # noqa: BLE001 — reported; file left in place
            outcome.errors.append((dest_rel, f"{type(exc).__name__}: {exc}"))
            continue
        outcome.removed.append((_posix(dest_rel), backup_rel, detail))
    if outcome.removed and not dry_run:
        _emit(folder, "bundle_compose_copies_removed",
              "Compose-file copies removed from this project", outcome.removed,
              "Project bundles no longer carry copies of the orchestrator's "
              "compose files: the service hooks read the compose directory from "
              "the orchestrator root, so a copy here could only drift from the "
              "real one. Unmodified copies were removed; edited copies were "
              "backed up first.")
    return outcome


def run_leftover_policy(
    folder: Path,
    orchestrator_root: Path,
    result: dict,
    new_files: dict,
    *,
    compose_prior: dict,
    known_rels: Iterable[str],
    skip_kinds: frozenset,
    update_mode: bool,
    dry_run: bool,
    backup_ts: Callable[[], str],
    log: Callable[[str], None],
) -> dict:
    """The bundle engine's single call: both rules, their envelope keys
    (``compose_copies_removed`` / ``leftovers_removed``, present only when
    non-empty), one informational ``notes`` line each, a warning per file
    that could not be removed. Returns ``{cid: removed-this-run}`` for the
    bundle reconcile (a record clears on the next update that removed
    nothing). The leftover pass runs on UPDATES only — a first install (safe
    add included) never removes anything it did not itself write."""
    compose = retire_compose_copies(folder, compose_prior, backup_ts=backup_ts,
                                    dry_run=dry_run)
    leftovers = (
        remove_vco_leftovers(folder, orchestrator_root, known_rels=known_rels,
                             skip_kinds=skip_kinds, backup_ts=backup_ts,
                             dry_run=dry_run, log=log)
        if update_mode else LeftoverOutcome()
    )
    for rel, _err in compose.errors:
        new_files[rel] = compose_prior[rel]  # still ours; the next run retries
    verb = "would be removed (dry run)" if dry_run else "removed"
    for outcome, key, label in (
        (compose, "compose_copies_removed", "compose-file copies"),
        (leftovers, "leftovers_removed", "retired VCO file(s)"),
    ):
        if outcome.removed:
            result[key] = [rel for rel, _bk, _d in outcome.removed]
            backed = sum(1 for _r, bk, _d in outcome.removed if bk)
            result.setdefault("notes", []).append(
                f"{len(outcome.removed)} {label} {verb}"
                + (f"; {backed} backed up under .claude/backups/bundle-adoptions/"
                   if backed else ""))
            log(f"{label} {verb}: {', '.join(result[key])}")
        for rel, err in outcome.errors:
            result["warnings"].append(f"could not remove {rel} ({err}); left in place")
    return {
        "bundle_compose_copies_removed": bool(compose.removed) and not dry_run,
        "bundle_leftover_removed": bool(leftovers.removed) and not dry_run,
    }
