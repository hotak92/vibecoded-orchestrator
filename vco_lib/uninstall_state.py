# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall step [8]: remove the WHOLE VCO state dir.

Owner ruling 2026-10-16: the uninstaller removes the entire VCO state dir —
``~/.vct`` (``%USERPROFILE%\\.vct``) or ``$VCT_STATE_DIR``, resolved by the one
resolver :func:`vco_lib.paths.vct_root_dir` — RL training data
(``retrieval_rl_data/``) included, confirm-gated like every step, listed by
``--dry-run``, honoured by ``--yes``. Before v0.2.103 only ``launcher.db``
was removed (without its ``-wal`` / ``-shm`` siblings) plus a few named files,
so the hub DB, tokens, logs and caches outlived every uninstall.

Runs after step [3] (:mod:`vco_lib.uninstall_quiesce`) has stopped the hub
and the launcher GUI that hold files here open.

Safety — the step REFUSES (and falls back to the exact-name files the earlier
steps removed: ``launcher.db`` + siblings, the model-gateway state files and
step [7]'s state files) when the dir is not plainly VCO's:

* it is itself a symlink or junction (review R39 H1): walking it would delete
  the TARGET's contents, which the plan never showed — the link and its
  target are named for the user to remove by hand;
* it resolves to a filesystem / drive root, the home directory, or an
  ancestor of either;
* it is, or contains, ``~/.vct-secrets`` (``$VCT_SECRETS_DIR``) or the
  orchestrator clone;
* it is not named ``.vct`` and ``VCT_STATE_DIR`` was not set explicitly — or
  it was set explicitly to a dir with another name that holds none of VCO's
  state files (``launcher.db``, ``hub.pid`` …).

An explicit ``VCT_STATE_DIR`` with another name that DOES hold VCO's files
(review R39 M1) is not removed whole either: only VCO's own top-level entries
go (:data:`VCO_OWNED_NAMES` / :data:`VCO_OWNED_PREFIXES`), the dir itself and
everything else in it stay — ``VCT_STATE_DIR=~/Documents`` must not cost the
user their documents. Entries with a generic name VCO also uses (``logs``,
``data`` …) are listed as left in place: they may be the user's.

No link or junction is followed: the link itself is removed, never its target
(:func:`vco_lib.uninstall_artifacts._is_reparse`). A file that cannot be
removed (in use) is reported and the walk continues.
``backups/kg-reviewer-outside/`` is kept, as step [7] always kept it: it holds
copies of the user's own files.

``~/.vct-secrets`` is a separate directory and is never touched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from vco_lib.uninstall_artifacts import _is_reparse

__all__ = ["StatePlan", "build_plan", "plan_lines", "confirm_prompt", "execute", "unsafe_reason",
           "DB_SIBLING_SUFFIXES"]

#: SQLite companions that must go with a database file.
DB_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")
#: Names only VCO writes at the top of its state dir — the evidence an
#: explicitly-set ``VCT_STATE_DIR`` with a non-default name is VCO's.
VCO_STATE_MARKERS = (
    "launcher.db", "hub.db", "hub.pid", "hub.token", "hub.port", "launcher.toml",
    "services.toml", "model-gateway.token", "model-guidance.json", "retrieval_rl_data",
)
#: Top-level entries only VCO creates in its state dir — removed by name when
#: the dir is an explicit ``VCT_STATE_DIR`` not named ``.vct`` (review R39 M1).
VCO_OWNED_NAMES = frozenset({
    *VCO_STATE_MARKERS, "hub.bind", "launcher-update-state.json", "launcher_errors.jsonl",
    "services-watcher.jsonl", "services.json", "storage.toml", "model-gateway",
    "model-gateway.pid", "model-gateway.port", "model-gateway.last-port", "keyring.pace",
    "install.py.lock", "install-manifest.json", "update.lock.json", "update.log",
    "summary_backend_breaker.json", "summary-cli-isolation-cache.json", "codegraph-parsers.json",
    "license.key", "telemetry.json", "rl_archive", "bundled_manifests", "wait_guard",
    "diagrams.token", "rl_project_identity_cache.json", "orchestrator.json", "projects.json",
})
#: Name prefixes only VCO uses (database siblings and backups, per-project hub
#: tokens, atomic-write temp files of its caches).
VCO_OWNED_PREFIXES = ("launcher.db", "hub.db", "hub.token.", "model-gateway.",
                      "rl_project_identity_cache.json.", "services.toml.")
#: Generic names VCO also writes at the top of its state dir: in a dir not
#: named ``.vct`` they may be the user's own, so they are left and listed.
VCO_GENERIC_NAMES = frozenset({"logs", "metrics", "state", "locks", "cache", "data", "modules",
                               "backups", "projects", "shared", "schemas", "docs"})
RL_DATA_DIRNAME = "retrieval_rl_data"
KEEP_REL = Path("backups") / "kg-reviewer-outside"


@dataclass
class StatePlan:
    root: Path
    #: Why the whole dir is NOT removed (``None`` = it is removed).
    refused: Optional[str] = None
    exists: bool = False
    size_bytes: int = 0
    file_count: int = 0
    rl_bytes: Optional[int] = None  # None = no retrieval_rl_data/
    has_license: bool = False
    keep: list[Path] = field(default_factory=list)
    #: launcher.db (+ siblings) when it lives OUTSIDE ``root``
    #: (``$VCT_LAUNCHER_DB_PATH``), or — when refused — inside it.
    db_files: list[Path] = field(default_factory=list)
    #: The dir is an explicit ``VCT_STATE_DIR`` not named ``.vct``: only
    #: ``entries`` go, the dir and everything else stay (review R39 M1).
    selective: bool = False
    entries: list[Path] = field(default_factory=list)
    #: Generic-named entries left in a selective dir (may be the user's).
    left: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------

def _forms(p: Path) -> list[Path]:
    out = [Path(os.path.abspath(p))]
    try:
        real = Path(os.path.realpath(p))
    except (OSError, ValueError):
        return out
    if real != out[0]:
        out.append(real)
    return out


def _same_or_ancestor(a: Path, b: Path) -> bool:
    """``a`` is ``b`` or one of its ancestors (case-insensitive on Windows)."""
    na, nb = os.path.normcase(str(a)), os.path.normcase(str(b))
    if na == nb:
        return True
    return nb.startswith(na.rstrip("\\/") + os.sep) or nb.startswith(na.rstrip("\\/") + "/")


def unsafe_reason(
    root: Path,
    *,
    home: Path,
    explicit: bool,
    protected: Sequence[Path] = (),
) -> Optional[str]:
    """Why ``root`` must not be touched beyond exact names, or ``None``."""
    if _is_reparse(root):
        try:
            target = os.path.realpath(root)
        except (OSError, ValueError):
            target = "?"
        return (f"{root} is a link or junction (to {target}) — remove the link and, if it "
                "is VCO's, its target by hand")
    homes = _forms(home)
    for form in _forms(root):
        if form.parent == form:
            return f"{form} is a filesystem or drive root"
        for h in homes:
            if _same_or_ancestor(form, h):
                return f"{form} is your home directory or contains it"
        for prot in protected:
            for pf in _forms(prot):
                if _same_or_ancestor(form, pf):
                    return f"{form} is or contains {pf}"
    if root.name != ".vct":
        if not explicit:
            return f"{root} is not named .vct and VCT_STATE_DIR is not set"
        if not any((root / m).exists() or (root / m).is_symlink() for m in VCO_STATE_MARKERS):
            return (f"{root} (VCT_STATE_DIR) holds none of VCO's state files "
                    f"({', '.join(VCO_STATE_MARKERS[:4])} …) — not plainly VCO's")
    return None


# --------------------------------------------------------------------------
# walk — never through a link or junction
# --------------------------------------------------------------------------

def _children(d: Path) -> list[Path]:
    try:
        return sorted(Path(e.path) for e in os.scandir(d))
    except OSError:
        return []


def _measure(d: Path) -> tuple[int, int]:
    size = count = 0
    for child in _children(d):
        if _is_reparse(child):
            count += 1
        elif child.is_dir():
            s, c = _measure(child)
            size += s
            count += c
        else:
            try:
                size += child.lstat().st_size
            except OSError:
                pass
            count += 1
    return size, count


def _unlink_link(p: Path) -> None:
    """Remove the link / junction itself — never its target."""
    try:
        p.unlink()
    except (IsADirectoryError, PermissionError):
        if os.name != "nt":
            raise
        os.rmdir(p)  # a directory junction/symlink: rmdir drops the reparse point


def _remove_tree(d: Path, keep: set[str], failed: list[tuple[Path, str]]) -> int:
    removed = 0
    for child in _children(d):
        if os.path.normcase(str(child)) in keep:
            continue
        try:
            if _is_reparse(child):
                _unlink_link(child)
                removed += 1
            elif child.is_dir():
                removed += _remove_tree(child, keep, failed)
                if not _children(child):
                    child.rmdir()
            else:
                child.unlink()
                removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed.append((child, str(exc)))
    return removed


# --------------------------------------------------------------------------
# plan / lines / execute
# --------------------------------------------------------------------------

def _is_vco_owned(name: str) -> bool:
    return name in VCO_OWNED_NAMES or name.startswith(VCO_OWNED_PREFIXES)


def _measure_paths(paths: Sequence[Path]) -> tuple[int, int]:
    size = count = 0
    for p in paths:
        if not _is_reparse(p) and p.is_dir():
            s, c = _measure(p)
            size, count = size + s, count + c
        else:
            try:
                size += p.lstat().st_size
            except OSError:
                pass
            count += 1
    return size, count


def _db_with_siblings(db: Path) -> list[Path]:
    paths = [db, *(db.with_name(db.name + s) for s in DB_SIBLING_SUFFIXES)]
    return [p for p in paths if p.exists() or p.is_symlink()]


def build_plan(
    *,
    root: Optional[Path] = None,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    clone_root: Optional[Path] = None,
    launcher_db: Optional[Path] = None,
) -> StatePlan:
    """Read-only: what step [8] would remove. Defaults resolve the real
    machine (``vct_root_dir``, ``launcher_db_path``, the secrets resolver)."""
    from vco_lib import paths as _paths
    from vco_lib.paths import secrets_root as _secrets_root

    env = os.environ if env is None else env
    root = _paths.vct_root_dir() if root is None else root
    home = Path.home() if home is None else home
    launcher_db = _paths.launcher_db_path() if launcher_db is None else launcher_db
    explicit = bool((env.get("VCT_STATE_DIR") or "").strip())
    protected = [_secrets_root()] + ([clone_root] if clone_root is not None else [])
    plan = StatePlan(root=root)
    plan.refused = unsafe_reason(root, home=home, explicit=explicit, protected=protected)
    plan.exists = root.is_dir()
    db_outside = not _same_or_ancestor(Path(os.path.abspath(root)), Path(os.path.abspath(launcher_db)))
    if plan.refused is not None or db_outside:
        plan.db_files = _db_with_siblings(launcher_db)
    if plan.refused is None and plan.exists and root.name != ".vct":
        plan.selective = True
        for child in _children(root):
            if _is_vco_owned(child.name):
                plan.entries.append(child)
            elif child.name in VCO_GENERIC_NAMES:
                plan.left.append(child)
        plan.size_bytes, plan.file_count = _measure_paths(plan.entries)
    elif plan.refused is None and plan.exists:
        plan.size_bytes, plan.file_count = _measure(root)
        rl = root / RL_DATA_DIRNAME
        if rl.is_dir() and not _is_reparse(rl):
            plan.rl_bytes = _measure(rl)[0]
        plan.has_license = (root / "license.key").exists()
        kept = root / KEEP_REL
        if kept.is_dir() and _children(kept):
            plan.keep.append(kept)
    return plan


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def plan_lines(plan: StatePlan, *, fallback_files: Sequence[Path] = ()) -> list[str]:
    """The ``[8]`` lines. ``fallback_files`` = the model-gateway state files
    removed by name when the dir is refused."""
    out: list[str] = []
    if plan.refused is not None:
        out.append(f"  [8] [refused] The VCO state dir is NOT removed whole: {plan.refused}")
        names = [*plan.db_files, *(p for p in fallback_files if p.exists())]
        if names:
            out.append("      Only these VCO files are removed by name:")
            out += [f"       - {p}" for p in names]
        out.append("      (step [7] removes the other named VCO state files)")
        return out
    if plan.selective:
        out.append(f"  [8] {plan.root} is not named .vct: remove only VCO's own entries in it "
                   f"({_mb(plan.size_bytes)}, {plan.file_count} files) — the folder and "
                   "everything else in it stay:")
        out += [f"       - {p.name}" for p in plan.entries]
        out += [f"      LEFT IN PLACE: {p}  (a generic name VCO also uses — check it and "
                "delete it by hand if it is VCO's)" for p in plan.left]
    elif plan.exists:
        out.append(f"  [8] Remove the whole VCO state dir: {plan.root}  "
                   f"({_mb(plan.size_bytes)}, {plan.file_count} files)")
        rl = (f"retrieval_rl_data/ = {_mb(plan.rl_bytes)}" if plan.rl_bytes is not None
              else "none present")
        out.append(f"      Includes the RL training data (retrieval_rl_data/; {rl}), "
                   "launcher.db + -wal/-shm, hub DB, tokens, logs, caches")
        if plan.has_license:
            out.append("      Includes license.key — keep a copy if you will reinstall")
        out += [f"      LEFT IN PLACE: {k}  (backups of your own files made by the KG reviewer)"
                for k in plan.keep]
    else:
        out.append(f"  [8] [skip] No VCO state dir at {plan.root}")
    out += [f"      + launcher DB outside it: {p}" for p in plan.db_files]
    return out


def _remove_files(files: Sequence[Path]) -> list[str]:
    audit: list[str] = []
    for p in files:
        try:
            p.unlink()
            audit.append(f"removed {p}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            audit.append(f"WARN: could not remove {p}: {exc} (in use? close the launcher / vct-hub)")
    return audit


def confirm_prompt(plan: StatePlan) -> str:
    """The step-[8] question, worded for what the plan actually removes
    (review R39 L3: the refused case used to ask about the whole dir)."""
    if plan.refused is not None:
        return "Remove the VCO files listed in step [8] by name (the state dir itself stays)?"
    if plan.selective:
        return f"Remove VCO's own entries from {plan.root} (RL training data included; the folder stays)?"
    return f"Remove the VCO state dir {plan.root} (RL training data included)?"


def _remove_entries(entries: Sequence[Path], failed: list[tuple[Path, str]]) -> int:
    removed = 0
    for p in entries:
        try:
            if _is_reparse(p):
                _unlink_link(p)
                removed += 1
            elif p.is_dir():
                removed += _remove_tree(p, set(), failed)
                if not _children(p):
                    p.rmdir()
            else:
                p.unlink()
                removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed.append((p, str(exc)))
    return removed


def _failed_lines(root: Path, failed: list[tuple[Path, str]]) -> list[str]:
    if not failed:
        return []
    out = [f"WARN: {len(failed)} item(s) under {root} could not be removed (in use? "
           "close the VCT Launcher / vct-hub / model gateway, then delete them by hand):"]
    out += [f"WARN:   {p}: {why}" for p, why in failed[:20]]
    if len(failed) > 20:
        out.append(f"WARN:   … and {len(failed) - 20} more")
    return out


def execute(plan: StatePlan) -> list[str]:
    """Remove what :func:`build_plan` planned. Audit lines; never raises.

    A refused plan removes only ``db_files``; the caller runs the other
    exact-name removals (model-gateway state, step [7]'s state files).
    """
    audit = _remove_files(plan.db_files)
    if plan.refused is not None or not plan.exists:
        return audit
    root = plan.root
    if _is_reparse(root):  # became a link since the plan was built: never walk through it
        audit.append(f"WARN: SKIPPED [8]: {root} is now a link or junction — nothing removed through it")
        return audit
    failed: list[tuple[Path, str]] = []
    if plan.selective:
        removed = _remove_entries(plan.entries, failed)
        audit.append(f"removed {removed} VCO entries from {root} (the folder and the rest stay)")
        audit += _failed_lines(root, failed)
        audit += [f"left in place: {p} (generic name — delete it by hand if it is VCO's)"
                  for p in plan.left]
        return audit
    keep = {os.path.normcase(str(k)) for k in plan.keep}
    removed = _remove_tree(root, keep, failed)
    gone = False
    if not keep and not failed:
        try:
            root.rmdir()
            gone = True
        except OSError as exc:
            failed.append((root, str(exc)))
    if gone:
        audit.append(f"removed VCO state dir {root} ({removed} entries)")
    else:
        audit.append(f"removed {removed} entries from VCO state dir {root}")
    audit += _failed_lines(root, failed)
    audit += [f"left in place: {k} (backups of your own files made by the KG reviewer)"
              for k in plan.keep]
    return audit
