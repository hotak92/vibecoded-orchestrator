# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The sanctioned per-project scratch directory (v0.2.104).

Agents and lanes used to put their disposable files (downloads, extracted
archives, test harnesses, copies of a file before an edit) under ``/tmp``.
Hundreds of MB piled up there, because the permission layer refuses an
``rm -rf`` of it — in a lane and in the main session alike — and scratch
placed inside a worktree broke every test that walks the repo. This module is
the ONE home of the replacement:

    <project>/.claude/state/scratch/<session-or-lane-id>/

* **Created on demand, private.** The root and each lane directory are 0700 on
  POSIX; on native Windows they get the same owner-only ACL the lean-ctx tee
  directory gets — this module RUNS the one home of that idiom (the
  ``S5-ACL-BEGIN``/``S5-ACL-END`` block of
  ``templates/hooks/_lib/lean-ctx-tee.ps1``) rather than carrying a second copy.
  A directory that cannot be made private is refused, never handed out.
* **Cleaned by VCO.** :func:`sweep` removes lane directories whose NEWEST entry
  is older than the TTL (``VCO_SCRATCH_TTL_HOURS``, default 168 h = 7 days,
  ``0`` = keep forever — the same knob shape as the tee's
  ``VCO_LEAN_CTX_TEE_TTL_HOURS``, read from the environment or
  ``<project>/.claude/env``). It runs once per session from the SessionStart
  hook ``session-start-kg-loader`` — never on the per-tool-call path.
* **Never leaves its root.** The sweep refuses a scratch root that is (or sits
  under) a symlink, removes a symlinked lane entry as a LINK (its target is
  never touched), and deletes trees with :func:`shutil.rmtree`, which unlinks
  inner symlinks instead of descending into them.
* **Invisible to git and the code graph.** ``.claude/state/`` is written to
  ``.git/info/exclude`` at bundle add/update (:mod:`vco_lib.git_exclude`) and
  is transient to the code graph
  (:data:`vco_lib.codegraph_row_classify.TRANSIENT_STATE_MARKER`).
* **Reported.** ``vco doctor`` shows the lane count, total size and the oldest
  lane's age (:func:`probe_scratch`).

CLI (``python -m vco_lib.scratch_dir``; the shipped wrapper is
``.claude/scripts/vco-scratch`` / ``vco-scratch.ps1``)::

    path   [--project DIR] [--name LANE]   create + print the scratch dir
    sweep  [--project DIR] [--ttl-hours N] [--json]
    report [--project DIR] [--json]

Exit codes: 0 ok · 1 refused (unsafe root, cannot make private, no project)
· 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

#: Scratch root, relative to the project root.
SCRATCH_REL: tuple[str, ...] = (".claude", "state", "scratch")

#: TTL knob (hours; ``0`` = keep forever; a non-integer falls back to the
#: default). Read from the environment first, then ``<project>/.claude/env``.
TTL_ENV = "VCO_SCRATCH_TTL_HOURS"
DEFAULT_TTL_HOURS = 168

#: Session-id env vars, in order: the harness's own, then VCO's legacy name.
SESSION_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")

#: Fallback lane name when neither ``--name`` nor a session id is known.
DEFAULT_LANE = "default"

#: A ``--name`` must be ONE safe path component: no separator, no leading dot
#: (so ``.`` / ``..`` are impossible), at most 128 characters.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

#: The one home of the Windows owner-only ACL idiom (see module docstring).
ACL_LIB_REL: tuple[str, ...] = ("templates", "hooks", "_lib", "lean-ctx-tee.ps1")
ACL_BEGIN = "# S5-ACL-BEGIN"
ACL_END = "# S5-ACL-END"

_HOUR = 3600.0


class ScratchError(RuntimeError):
    """The scratch directory cannot be handed out or swept safely."""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def scratch_root(project: "str | Path") -> Path:
    """``<project>/.claude/state/scratch`` (not created)."""
    return Path(project).joinpath(*SCRATCH_REL)


def resolve_ttl_hours(project: "str | Path | None" = None,
                      env: Optional[Mapping[str, str]] = None) -> int:
    """The scratch TTL in hours: env var, else ``<project>/.claude/env``,
    else :data:`DEFAULT_TTL_HOURS`. An unparseable or negative value falls
    back to the default (never to "keep forever" and never to "delete now")."""
    environ = os.environ if env is None else env
    raw: Optional[str] = environ.get(TTL_ENV)
    if raw is None and project is not None:
        raw = _claude_env_value(Path(project), TTL_ENV)
    return _parse_ttl(raw)


def _parse_ttl(raw: Optional[str]) -> int:
    text = (raw or "").strip()
    if not text.isdigit():
        return DEFAULT_TTL_HOURS
    return int(text)


def _claude_env_value(project: Path, key: str) -> Optional[str]:
    from vco_lib.envfile import parse_env_lines  # noqa: PLC0415

    try:
        text = (project / ".claude" / "env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for k, v in parse_env_lines(text):
        if k == key:
            return v
    return None


def lane_name(name: Optional[str] = None,
              env: Optional[Mapping[str, str]] = None) -> str:
    """The lane directory name: ``name`` when given (validated — a bad one is
    a usage error, never silently rewritten), else the sanitised session id,
    else :data:`DEFAULT_LANE`."""
    if name is not None:
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid scratch name {name!r}: use letters, digits, '.', '_' "
                "or '-' (no path separator, no leading dot, at most 128 chars)"
            )
        return name
    from vco_lib.inject_intent import sanitize_session_id  # noqa: PLC0415

    environ = os.environ if env is None else env
    for var in SESSION_ENV_VARS:
        sid = sanitize_session_id((environ.get(var) or "").strip())
        if sid:
            return sid
    return DEFAULT_LANE


def resolve_project(project: "str | Path | None") -> Path:
    """``--project`` when given, else the one project-root rule
    (:func:`vco_lib.project_root.resolve_project_root`)."""
    if project:
        return Path(project)
    from vco_lib.project_root import resolve_project_root  # noqa: PLC0415

    found = resolve_project_root()
    if found is None:
        raise ScratchError(
            "no VCO project found (CLAUDE_PROJECT_DIR unset and no marked "
            "project above the current directory) - pass --project"
        )
    return found


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------


def _symlinked_component(project: Path, root: Path) -> Optional[Path]:
    """The first path component from ``<project>/.claude`` down to ``root``
    that is a symlink (or a Windows junction), else ``None``. A redirected
    component would put the scratch tree — and every deletion the sweep makes
    — somewhere other than the project."""
    current = project
    for part in root.relative_to(project).parts:
        current = current / part
        if current.is_symlink() or _is_junction(current):
            return current
    return None


def _is_junction(path: Path) -> bool:
    probe: Optional[Callable[[], bool]] = getattr(path, "is_junction", None)
    try:
        return bool(probe()) if probe is not None else False
    except OSError:
        return False


def _owner_only_acl_script(lib_text: str) -> str:
    from vco_lib.envfile import extract_managed_block  # noqa: PLC0415

    block = extract_managed_block(lib_text, ACL_BEGIN, ACL_END)
    if not block or "function Set-OwnerOnlyAcl" not in block:
        raise ScratchError(
            "owner-only ACL block not found in lean-ctx-tee.ps1 (broken install)"
        )
    return (
        block
        + "\nif (Set-OwnerOnlyAcl $env:VCO_SCRATCH_ACL_PATH -Directory) "
        "{ exit 0 } else { exit 1 }\n"
    )


def _default_acl_runner(path: Path) -> bool:
    """Apply the owner-only ACL to ``path`` by running the S5 block."""
    lib = Path(__file__).resolve().parents[1].joinpath(*ACL_LIB_REL)
    try:
        script = _owner_only_acl_script(lib.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ScratchError(f"cannot read the owner-only ACL lib {lib}: {exc}") from exc
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return False
    env = dict(os.environ, VCO_SCRATCH_ACL_PATH=str(path))
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, env=env, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


#: Test seam: the Windows ACL applier (``path -> made private?``).
ACL_RUNNER: Callable[[Path], bool] = _default_acl_runner


def _is_windows() -> bool:
    return os.name == "nt"


def _make_private_dir(path: Path) -> None:
    """Create ``path`` (one level) if missing and make it owner-only."""
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ScratchError(f"cannot create {path}: {exc}") from exc
    if path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path} is not a real directory - refusing it")
    if _is_windows():
        if not ACL_RUNNER(path):
            raise ScratchError(
                f"cannot make {path} owner-only (ACL) - refusing to hand out a "
                "scratch directory other accounts could read"
            )
        return
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ScratchError(f"cannot chmod 0700 {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scratch_path(project: "str | Path", name: Optional[str] = None,
                 env: Optional[Mapping[str, str]] = None) -> Path:
    """Create (if needed) and return ``<project>/.claude/state/scratch/<lane>``.

    Raises :class:`ScratchError` when the root is redirected by a symlink or a
    directory cannot be made private, and :class:`ValueError` for a bad name.
    """
    project_path = Path(project)
    lane = lane_name(name, env)
    root = scratch_root(project_path)
    state = root.parent
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScratchError(f"cannot create {state}: {exc}") from exc
    bad = _symlinked_component(project_path, root)
    if bad is not None:
        raise ScratchError(f"{bad} is a symlink - refusing to place scratch through it")
    _make_private_dir(root)
    target = root / lane
    _make_private_dir(target)
    return target


def _newest_mtime(lane_dir: str) -> float:
    """Newest mtime anywhere in a lane tree, by ``lstat`` (links never followed)."""
    newest = os.lstat(lane_dir).st_mtime
    for dirpath, dirnames, filenames in os.walk(lane_dir, followlinks=False):
        for entry in dirnames + filenames:
            try:
                mtime = os.lstat(os.path.join(dirpath, entry)).st_mtime
            except OSError:
                continue
            newest = max(newest, mtime)
    return newest


def _tree_bytes(lane_dir: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(lane_dir, followlinks=False):
        for entry in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, entry))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


@dataclass
class LaneInfo:
    name: str
    kind: str  # "dir" | "file" | "symlink"
    newest_mtime: float
    size_bytes: int


def _scan(root: Path) -> list[LaneInfo]:
    """One entry per direct child of a safe scratch root."""
    lanes: list[LaneInfo] = []
    with os.scandir(root) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or _is_junction(Path(entry.path)):
                lanes.append(LaneInfo(entry.name, "symlink", st.st_mtime, 0))
            elif stat.S_ISDIR(st.st_mode):
                lanes.append(LaneInfo(entry.name, "dir", _newest_mtime(entry.path),
                                      _tree_bytes(entry.path)))
            else:
                size = st.st_size if stat.S_ISREG(st.st_mode) else 0
                lanes.append(LaneInfo(entry.name, "file", st.st_mtime, size))
    return lanes


def _safe_root(project: Path) -> Optional[Path]:
    """The scratch root when it exists and is safe to walk, ``None`` when it
    does not exist; raises :class:`ScratchError` when it is redirected."""
    root = scratch_root(project)
    if not os.path.lexists(root):
        return None
    bad = _symlinked_component(project, root)
    if bad is not None:
        raise ScratchError(f"{bad} is a symlink - the scratch sweep never follows it")
    if not root.is_dir():
        raise ScratchError(f"{root} is not a directory")
    return root


@dataclass
class SweepResult:
    root: str
    ttl_hours: int
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"root": self.root, "ttl_hours": self.ttl_hours,
                "removed": self.removed, "kept": self.kept, "errors": self.errors}


def _remove_lane(root: Path, lane: LaneInfo) -> None:
    path = root / lane.name
    if lane.kind == "dir":
        # Re-check right before deleting: the entry must still be a real
        # directory directly under the root (no swap to a symlink since the
        # scan). shutil.rmtree unlinks inner symlinks, never descends them.
        if path.is_symlink() or _is_junction(path) or not path.is_dir():
            raise OSError(f"{path} changed type since the scan - left alone")
        shutil.rmtree(path)
    elif lane.kind == "symlink" and _is_junction(path):
        os.rmdir(path)  # removes the junction itself, never its target
    else:
        os.unlink(path)  # a file, or the symlink itself (target untouched)


def sweep(project: "str | Path", ttl_hours: Optional[int] = None,
          now: Optional[float] = None) -> SweepResult:
    """Remove scratch lanes whose newest entry is older than the TTL.

    Touches nothing outside ``<project>/.claude/state/scratch``: a redirected
    root raises :class:`ScratchError` before anything is deleted, and a
    symlinked lane entry is removed as a link only. Per-lane failures are
    collected in :attr:`SweepResult.errors`, never raised.
    """
    project_path = Path(project)
    ttl = resolve_ttl_hours(project_path) if ttl_hours is None else ttl_hours
    result = SweepResult(root=str(scratch_root(project_path)), ttl_hours=ttl)
    root = _safe_root(project_path)
    if root is None:
        return result
    lanes = _scan(root)
    if ttl <= 0:
        result.kept = sorted(lane.name for lane in lanes)
        return result
    cutoff = (time.time() if now is None else now) - ttl * _HOUR
    for lane in sorted(lanes, key=lambda item: item.name):
        if lane.newest_mtime >= cutoff:
            result.kept.append(lane.name)
            continue
        try:
            _remove_lane(root, lane)
            result.removed.append(lane.name)
        except OSError as exc:
            result.errors.append(f"{lane.name}: {exc}")
    return result


def report(project: "str | Path", now: Optional[float] = None) -> dict:
    """Count, total size and oldest lane age of the scratch root."""
    project_path = Path(project)
    root = _safe_root(project_path)
    ttl = resolve_ttl_hours(project_path)
    lanes = _scan(root) if root is not None else []
    current = time.time() if now is None else now
    oldest = min(lanes, key=lambda item: item.newest_mtime) if lanes else None
    return {
        "root": str(scratch_root(project_path)),
        "exists": root is not None,
        "count": len(lanes),
        "total_bytes": sum(lane.size_bytes for lane in lanes),
        "oldest_name": oldest.name if oldest else None,
        "oldest_age_hours": (
            round(max(0.0, current - oldest.newest_mtime) / _HOUR, 1) if oldest else None
        ),
        "ttl_hours": ttl,
    }


def _fmt_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 ** 3:
        return f"{n / (1024 * 1024):.1f} MiB"
    return f"{n / 1024 ** 3:.2f} GiB"


def summarize(rep: dict) -> str:
    """One human line for :func:`report`'s dict."""
    ttl = rep["ttl_hours"]
    ttl_text = "kept forever (TTL 0)" if ttl == 0 else f"cleaned after {ttl} h"
    if not rep["count"]:
        return f"scratch dir empty ({rep['root']}; {ttl_text})"
    return (
        f"scratch dir: {rep['count']} entr{'y' if rep['count'] == 1 else 'ies'}, "
        f"{_fmt_bytes(rep['total_bytes'])}, oldest {rep['oldest_name']} "
        f"{rep['oldest_age_hours']} h old ({rep['root']}; {ttl_text})"
    )


def probe_scratch(folder: Path, res: object, ctx: dict) -> list:
    """``vco doctor`` probe: reported, never graded (an informational ``ok``),
    or ``unknown`` when the root is redirected / unreadable."""
    from vco_lib.doctor import STATUS_OK, STATUS_UNKNOWN, Finding  # noqa: PLC0415

    try:
        rep = report(folder)
    except (ScratchError, OSError) as exc:
        return [Finding(probe="scratch_dir", status=STATUS_UNKNOWN,
                        summary=f"scratch dir not measured: {exc}",
                        detail={"root": str(scratch_root(folder))})]
    return [Finding(probe="scratch_dir", status=STATUS_OK,
                    summary=summarize(rep), detail=rep)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.scratch_dir",
        description="The per-project scratch directory VCO creates and cleans.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_path = sub.add_parser("path", help="create and print the scratch dir")
    p_path.add_argument("--project", default=None)
    p_path.add_argument("--name", default=None,
                        help="lane name (default: the session id)")
    p_sweep = sub.add_parser("sweep", help="remove lanes older than the TTL")
    p_sweep.add_argument("--project", default=None)
    p_sweep.add_argument("--ttl-hours", type=int, default=None)
    p_sweep.add_argument("--json", action="store_true")
    p_rep = sub.add_parser("report", help="count, size and oldest lane")
    p_rep.add_argument("--project", default=None)
    p_rep.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        project = resolve_project(args.project)
        if args.cmd == "path":
            print(scratch_path(project, args.name))
            return 0
        if args.cmd == "sweep":
            if args.ttl_hours is not None and args.ttl_hours < 0:
                print("scratch_dir: --ttl-hours must be >= 0", file=sys.stderr)
                return 2
            result = sweep(project, args.ttl_hours)
            if args.json:
                print(json.dumps(result.to_dict()))
            elif result.removed or result.errors:
                print(f"scratch sweep: removed {len(result.removed)}, "
                      f"kept {len(result.kept)}, errors {len(result.errors)}")
            for err in result.errors:
                print(f"scratch_dir: {err}", file=sys.stderr)
            return 0
        rep = report(project)
        print(json.dumps(rep) if args.json else summarize(rep))
        return 0
    except ValueError as exc:
        print(f"scratch_dir: {exc}", file=sys.stderr)
        return 2
    except (ScratchError, OSError) as exc:
        print(f"scratch_dir: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the wrappers
    sys.exit(main())
