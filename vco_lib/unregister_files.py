# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
""""Remove project": take out of the project folder what is VCO's, and only
that (v0.2.106) — the ONE home of the unregister's file purge.

The rule is the env strip's evidence rule, applied to files and settings: a
thing goes only when VCO can PROVE it is VCO's; everything else stays and is
named in the reply.

* **Shipped files** — the bundle manifest (``.claude/.vco-manifest.json``)
  records the hash of every file VCO wrote. A file whose bytes still equal
  that hash is untouched since VCO wrote it: it goes (its launcher-disabled
  copy under ``.claude/{agents,skills}.disabled/`` too). A file whose bytes
  differ was changed by the user: it stays (``kept_modified``). A file VCO
  never recorded — a user's own hook, script, agent or skill — is never
  looked at. ``knowledge/**`` is always the user's data and is never touched.
  The manifest goes last, and only when every proven file went (so a retry can
  still prove the rest). Without a manifest nothing can be proven, and the
  reply says so. Before v0.2.106 the unregister deleted ``.claude/hooks/`` and
  ``.claude/scripts/`` whole, user files included.
* **``.claude/settings.json``** — the hook registrations VCO shipped go
  (:func:`vco_lib.settings_merge.strip_shipped_hooks`: exact spelling against
  the shipped templates, prior shapes and retired scripts); a user's command,
  in a VCO group or not, stays, and so does the registration of a hook script
  that stayed because the user changed it. ``permissions.allow`` / ``deny``:
  the merge only ever ADDS a whole list the user did not have, so a list that
  holds every entry of the shipped one is VCO's list — the shipped entries go
  and the user's additions stay; any other list is the user's and is reported.
  The retired allow patterns go (the update's merge drops them too). The
  template's own commentary keys go when they hold the template's text. Keys a
  user may set to the same value (``$schema``, ``idleCompaction``,
  ``skillOverrides``) stay and are named. An emptied ``hooks`` /
  ``permissions`` / ``env`` goes; a file left as ``{}`` is deleted.
  ``.claude/settings.local.json`` is deleted only when nothing but an empty
  ``env`` is left in it (the env strip ran first).
* **VCO's runtime files** — every path :data:`vco_lib.git_exclude.VCO_RUNTIME_EXCLUDES`
  names as VCO's alone (``.claude/state/``, ``.claude/logs/``, the deferral
  ledger, the compaction markers, the code-format sidecar …), the
  ``.claude/context/templates/*.reference.md`` sidecars, the safe-add
  ``.env.vco.reference``, and VCO's blocks in ``.git/info/exclude``. Kept and
  named: ``.claude/backups/`` (it holds the user's own earlier file versions)
  and ``.claude/worktrees/`` (an agent's worktree can hold uncommitted work).
  ``.claude/env`` is deleted only when the env strip left it empty.
* **Never** the orchestrator clone itself: its ``.claude/`` is the live
  install, so the purge refuses there and says so.

CLI: ``python -m vco_lib.unregister_files purge --project-folder F
[--orchestrator-root R]`` → ONE JSON object on stdout: ``{"ok": true,
"removed": [rel], "kept_modified": [rel], "kept": ["<rel> — <reason>"],
"settings_removed": [str], "errors": [str]}``. Names only, never a value.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Optional

__all__ = ["purge_project_files", "main"]

#: Template keys whose VALUE is VCO's own prose — no user writes that text.
VCO_AUTHORED_SETTINGS_KEYS: tuple[str, ...] = ("_template_origin", "_comment", "_env_comment")
#: Template keys a user can just as well set to the same value: never removed.
SHARED_VALUE_SETTINGS_KEYS: tuple[str, ...] = ("$schema", "idleCompaction", "skillOverrides")
_SETTINGS_REL = ".claude/settings.json"
_SETTINGS_LOCAL_REL = ".claude/settings.local.json"
_CLAUDE_ENV_REL = ".claude/env"
#: Runtime namespaces that may hold the USER's bytes: kept, named.
_KEPT_RUNTIME: dict[str, str] = {
    ".claude/backups": "VCO's update backups hold your own earlier versions of files",
    ".claude/worktrees": "agent worktrees can hold work that is not committed yet",
}


class _Run:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.removed: list[str] = []
        self.kept_modified: list[str] = []
        self.kept: list[str] = []
        self.settings_removed: list[str] = []
        self.errors: list[str] = []

    def rel(self, path: Path) -> str:
        return path.relative_to(self.folder).as_posix()

    def delete(self, path: Path) -> bool:
        """Remove a file, a link or a directory tree (a link is never
        followed). Records the outcome."""
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                return False
        except OSError as exc:
            self.errors.append(f"could not remove {self.rel(path)}: {exc}")
            return False
        self.removed.append(self.rel(path))
        return True


def _inside(folder: Path, rel: str) -> Optional[Path]:
    """``folder / rel`` when ``rel`` is a plain relative path that stays
    inside ``folder``; ``None`` for an absolute or ``..`` path."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    return folder.joinpath(*pure.parts)


def _manifest_hashes(manifest: dict) -> dict[str, str]:
    """``{posix rel: shipped sha256}`` from both manifest sections; the
    ``files`` record wins over a ``preserved_files`` one."""
    from vco_lib.paths import to_posix_rel

    out: dict[str, str] = {}
    for section, key in (("preserved_files", "shipped_sha256"), ("files", "sha256")):
        for rel, entry in (manifest.get(section) or {}).items():
            sha = entry.get(key) if isinstance(entry, dict) else None
            if isinstance(sha, str) and sha:
                out[to_posix_rel(rel)] = sha
    return out


def _purge_shipped_files(run: _Run) -> bool:
    """Delete the manifest-tracked files still byte-identical to what VCO
    wrote. Returns True when the manifest may go (it existed and every proven
    file was removed)."""
    from vco_lib.bundle_kinds import disabled_counterpart
    from vco_lib.hashing import sha256_file
    from vco_lib.manifest_paths import MANIFEST_REL_POSIX, manifest_path
    from vco_lib.project_init import _read_manifest

    if not manifest_path(run.folder).is_file():
        if any((run.folder / ".claude" / d).is_dir() for d in ("hooks", "scripts")):
            run.kept.append(
                f"{MANIFEST_REL_POSIX} — missing, so VCO cannot tell its hooks, scripts, "
                "agents and skills from yours; they were left in place")
        return False
    failed = False
    for rel, sha in sorted(_manifest_hashes(_read_manifest(run.folder)).items()):
        if rel == "knowledge" or rel.startswith("knowledge/"):
            continue  # always the user's data
        for cand in filter(None, (rel, disabled_counterpart(rel))):
            path = _inside(run.folder, cand)
            if path is None or not (path.is_file() or path.is_symlink()):
                continue
            try:
                same = not path.is_symlink() and sha256_file(path) == sha
            except OSError as exc:
                run.errors.append(f"could not read {cand}: {exc}")
                failed = True
                continue
            if not same:
                run.kept_modified.append(cand)
                continue
            if run.delete(path):
                _drop_bytecode(path)
            else:
                failed = True
    return not failed


def _drop_bytecode(py: Path) -> None:
    """The ``__pycache__/<stem>.*.pyc`` of a removed VCO ``.py`` — generated
    from that file alone."""
    if py.suffix != ".py":
        return
    cache = py.parent / "__pycache__"
    for pyc in cache.glob(f"{py.stem}.*.pyc") if cache.is_dir() else ():
        try:
            pyc.unlink()
        except OSError:
            pass
    from vco_lib.fs_prune import rmdir_if_empty
    if cache.is_dir():
        rmdir_if_empty(cache)


def _templates(root: Optional[Path]) -> Optional[list[dict]]:
    """Both OS settings templates (a project may have been installed from
    either), or ``None`` when neither can be read."""
    if root is None:
        return None
    out = []
    for name in ("settings.json.linux.template", "settings.json.windows.template"):
        try:
            data = json.loads((root / "templates" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out or None


def _strip_permissions(data: dict, templates: list[dict], run: _Run) -> None:
    from vco_lib.mcp_scan_rules import retired_settings_allow_patterns

    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return
    new = dict(perms)
    for key in ("allow", "deny"):
        lst = perms.get(key)
        if not isinstance(lst, list):
            continue
        shipped: list = []
        for t in templates:
            t_list = (t.get("permissions") or {}).get(key) or []
            if t_list and all(e in lst for e in t_list):
                shipped = t_list
                break
        retired = set(retired_settings_allow_patterns()) if key == "allow" else set()
        gone = set(shipped) | retired
        kept = [e for e in lst if e not in gone]
        if len(kept) != len(lst):
            run.settings_removed.append(
                f"permissions.{key}: {len(lst) - len(kept)} entr{'y' if len(lst) - len(kept) == 1 else 'ies'} VCO added")
        if not shipped and kept:
            run.kept.append(
                f"{_SETTINGS_REL} permissions.{key} — it does not hold VCO's whole shipped "
                "list, so VCO cannot tell its entries from yours; left as it is")
        if kept:
            new[key] = kept
        else:
            new.pop(key, None)
    if new:
        data["permissions"] = new
    else:
        data.pop("permissions", None)


def _strip_settings(run: _Run, templates: Optional[list[dict]]) -> None:
    from vco_lib import jsonc_edit
    from vco_lib.atomic import atomic_rewrite_text
    from vco_lib.hook_retirements import vco_hook_script_identity
    from vco_lib.settings_merge import strip_shipped_hooks

    path = run.folder / _SETTINGS_REL
    if not path.is_file():
        return
    if templates is None:
        run.kept.append(f"{_SETTINGS_REL} — VCO's settings templates could not be read, so "
                        "no hook registration or setting was removed")
        return
    loaded = jsonc_edit.load_object(path)
    if loaded is None:
        run.errors.append(f"{_SETTINGS_REL} left untouched: it is not a JSON object VCO can read")
        return
    original, raw = loaded
    data = dict(original)

    def keep(cmd: Any) -> bool:
        name = vco_hook_script_identity(cmd) if isinstance(cmd, str) else None
        return name is not None and (run.folder / ".claude" / "hooks" / name).exists()

    if "hooks" in data:
        new_hooks, gone = strip_shipped_hooks(
            data["hooks"], [t.get("hooks") or {} for t in templates], keep=keep)
        if gone:
            run.settings_removed.append(f"hooks: {len(gone)} registration(s) VCO shipped")
        if isinstance(new_hooks, dict) and not new_hooks:
            data.pop("hooks")
        else:
            data["hooks"] = new_hooks
    _strip_permissions(data, templates, run)
    for key in VCO_AUTHORED_SETTINGS_KEYS:
        if key in data and any(data[key] == t.get(key) for t in templates if key in t):
            data.pop(key)
            run.settings_removed.append(key)
    for key in SHARED_VALUE_SETTINGS_KEYS:
        if key in data and any(data[key] == t.get(key) for t in templates if key in t):
            run.kept.append(f"{_SETTINGS_REL} {key} — VCO sets this value too, but so can you, "
                            "so it was left")
    if data.get("env") == {}:
        data.pop("env")
    if data == original:
        return
    if not data:
        run.delete(path)
        return
    text = jsonc_edit.dumps_preserving(raw, data, indent=2)
    if text is None:
        run.errors.append(f"{_SETTINGS_REL} left untouched: its comments could not be kept "
                          "while removing VCO's entries")
        run.settings_removed.clear()
        return
    try:
        atomic_rewrite_text(path, text)
    except OSError as exc:
        run.errors.append(f"could not rewrite {_SETTINGS_REL}: {exc}")
        run.settings_removed.clear()


def _delete_if_empty_settings_local(run: _Run) -> None:
    from vco_lib import jsonc_edit

    path = run.folder / _SETTINGS_LOCAL_REL
    if not path.is_file():
        return
    loaded = jsonc_edit.load_object(path)
    if loaded is not None and all(k == "env" and v == {} for k, v in loaded[0].items()):
        run.delete(path)


def _purge_runtime(run: _Run) -> None:
    from vco_lib.git_exclude import SAFE_ADD_SIDECAR_SUFFIX, VCO_RUNTIME_EXCLUDES
    from vco_lib.manifest_paths import MANIFEST_REL_POSIX
    from vco_lib.project_init import _template_reference_sidecars

    for pattern in VCO_RUNTIME_EXCLUDES:
        rel = pattern.strip("/")
        if rel == MANIFEST_REL_POSIX or rel.endswith(".disabled") or rel == _SETTINGS_LOCAL_REL:
            continue  # the manifest goes last; disabled copies by hash; local settings above
        path = run.folder / rel
        if not (path.exists() or path.is_symlink()):
            continue
        if rel in _KEPT_RUNTIME:
            run.kept.append(f"{rel}/ — {_KEPT_RUNTIME[rel]}")
        elif rel == _CLAUDE_ENV_REL:
            try:
                empty = path.is_file() and not path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                empty = False
            if empty:
                run.delete(path)
        else:
            run.delete(path)
    for ref in _template_reference_sidecars().values():
        path = run.folder / ref
        if path.is_file():
            run.delete(path)
    sidecar = run.folder / (".env" + SAFE_ADD_SIDECAR_SUFFIX)
    if sidecar.is_file():
        run.delete(sidecar)


def _strip_git_exclude(run: _Run) -> None:
    from vco_lib.git_exclude import strip_vco_blocks

    outcome = strip_vco_blocks(run.folder)
    action = outcome.get("action", "")
    if action == "stripped":
        run.settings_removed.append(f".git/info/exclude: {len(outcome['removed'])} line(s) VCO wrote")
    elif action.startswith(("read_failed", "write_failed")):
        run.errors.append(f".git/info/exclude left untouched ({action})")


def _prune(run: _Run) -> None:
    from vco_lib.fs_prune import prune_now_empty_parents

    for rel in list(run.removed):
        prune_now_empty_parents(run.folder, (run.folder / rel).parent)


def default_orchestrator_root() -> Path:
    """The checkout this ``vco_lib`` is imported from (an editable install)."""
    return Path(__file__).resolve().parents[1]


def purge_project_files(folder: Path, *, orchestrator_root: Optional[Path] = None) -> dict:
    """Run the purge on ``folder``; see the module docstring. Never raises
    for a per-file failure — each is in ``errors`` and the rest still runs."""
    from vco_lib.paths import looks_like_orchestrator_root
    from vco_lib.project_init import _is_root_bundle_target

    folder = Path(folder)
    run = _Run(folder)
    root = orchestrator_root if orchestrator_root is not None else default_orchestrator_root()
    if not folder.is_dir():
        return _reply(run)
    if looks_like_orchestrator_root(folder) or _is_root_bundle_target(root, folder):
        run.errors.append(
            "this folder is a VCO orchestrator clone: its .claude/ is the live install, so no "
            "file was removed from it")
        return _reply(run)
    manifest_may_go = _purge_shipped_files(run)
    _strip_settings(run, _templates(root))
    _delete_if_empty_settings_local(run)
    _purge_runtime(run)
    _strip_git_exclude(run)
    from vco_lib.manifest_paths import manifest_path
    if manifest_may_go:
        run.delete(manifest_path(folder))
    _prune(run)
    return _reply(run)


def _reply(run: _Run) -> dict:
    return {"removed": run.removed, "kept_modified": run.kept_modified, "kept": run.kept,
            "settings_removed": run.settings_removed, "errors": run.errors}


def _cli_purge(args: argparse.Namespace) -> int:
    result = purge_project_files(
        Path(args.project_folder),
        orchestrator_root=Path(args.orchestrator_root) if args.orchestrator_root else None)
    print(json.dumps({"ok": True, **result}))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.unregister_files")
    sub = parser.add_subparsers(dest="cmd", required=True)
    purge = sub.add_parser("purge", help="remove VCO's own files and settings from a project "
                                         "folder (names only on stdout)")
    purge.add_argument("--project-folder", required=True)
    purge.add_argument("--orchestrator-root", default=None)
    purge.set_defaults(handler=_cli_purge)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
