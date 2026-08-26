# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Dist-binary repair for the terminal install path (v0.2.91 WP-A / WI-5).

Why this module exists
======================

The launcher's own delivery chain (pre-pull rename, ``<target>.new`` staging,
the stage1 ``vct-updater`` handoff) is compiled INTO the launcher binary. When
that binary is the thing that is broken, every fix shipped for it arrives in a
component that never runs — the bootstrap paradox behind the 2026 field
incident where a Windows install ran a hand-frozen executable for a month while
source updates landed cleanly every time.

``install.py --update`` is the escape hatch that needs no working launcher, and
until v0.2.91 it could not repair a dist binary at all:

* ``_refresh_dist_binary_after_rebuild`` only ever swapped from a freshly
  CARGO-BUILT ``target/release/vct-launcher-temp``. A binary-download user has
  no such artifact, so the helper returned immediately.
* There was no ``git checkout``/restore of ``launcher/dist/**`` anywhere in
  install.py (verified by grep at the time), so a dist tree that diverged from
  HEAD stayed diverged.
* ``_try_invoke_windows_stage1_updater`` documented a launcher-PID "process
  scan" fallback in its docstring that the code never implemented — it skipped
  whenever ``$VCT_LAUNCHER_PID`` was absent, i.e. on exactly the CLI-invoked
  runs the fallback was written for.

This module supplies all three legs, as pure-ish functions install.py calls
through a thin shim. It is deliberately parameterised on ``(dist_rel_dir,
binary_names)`` rather than re-deriving the OS→slot mapping, so it adds no new
cross-language mirror of that table (``install.py::_launcher_binary_relative_path``
stays the single Python source of truth).

Repair strategy
===============

For each dist path git reports as TRACKED-modified:

1. **Restore** — ``git checkout HEAD -- <path>``. This is the whole fix whenever
   the file is not locked (always on POSIX; on Windows whenever no launcher
   holds it open). Cheap, atomic-per-file, and it is the same recovery shape the
   post-install docs already hand users. ``HEAD`` is named explicitly so a
   STAGED-modified binary is restored to the committed bytes rather than to the
   index's diverged copy (see :func:`restore_paths_from_head`).
2. **Stage** — when the restore fails (Windows mandatory lock on a running
   ``.exe``), write HEAD's blob to ``<target>.new`` instead and report that a
   stage1 handoff is needed. The caller spawns ``vct-updater``, which renames
   the sibling into place once the launcher PID exits.

Untracked files under the dist directory (``??``) are NEVER touched: that set
includes the user's own ``.old-<pid>`` backups and any staged ``.new`` sibling,
and ``git checkout`` on an untracked path would either error or, worse, be
widened by a careless caller into a delete. The dirty-set filter is the
destructive gate for this module and both of its legs are unit-tested.

Everything here is best-effort: a repair failure must never abort an install.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "RepairOutcome",
    "dist_dirty_paths",
    "repair_dist_binaries",
    "restore_paths_from_head",
    "run_repair_leg",
    "scan_for_launcher_pid",
    "stage_paths_from_head",
    "staged_sibling",
]

# Seconds. Every git/ps invocation here is local and instant; the timeout only
# exists so a wedged child can never hang an install.
_SUBPROCESS_TIMEOUT = 30

LogFn = Callable[[str, str, str], None]


def _noop_log(_phase: str, _status: str, _message: str) -> None:
    """Default logger: silent. install.py passes a ``_log_install_event`` adapter."""


@dataclass(frozen=True)
class RepairOutcome:
    """What :func:`repair_dist_binaries` did.

    ``handoff_needed`` is the signal the caller acts on: at least one
    ``<target>.new`` is waiting for the stage1 updater to rename it after the
    running launcher exits.
    """

    dirty: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    staged: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def handoff_needed(self) -> bool:
        return bool(self.staged)

    @property
    def changed_anything(self) -> bool:
        return bool(self.restored or self.staged)


def _run_git(
    install_root: Path, args: Sequence[str]
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``install_root``. Never raises on non-zero."""
    return subprocess.run(
        ["git", *args],
        cwd=str(install_root),
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT,
        check=False,
    )


def staged_sibling(target: Path) -> Path:
    """``foo.exe`` -> ``foo.exe.new``.

    MUST match ``vct-updater``'s reader convention and the launcher-side
    ``path_with_new_suffix`` / ``with_new_suffix`` helpers — the writer and the
    reader have to agree on the filename or the swap silently no-ops.
    """
    return target.with_name(target.name + ".new")


def dist_dirty_paths(install_root: Path, dist_rel_dir: str) -> list[str]:
    """Repo-relative paths under ``dist_rel_dir`` that git reports as
    TRACKED-modified (staged or unstaged).

    Untracked (``??``) entries are excluded — see the module docstring: that is
    the destructive gate for the restore leg.

    Returns ``[]`` on any git failure (fail-SAFE: never manufacture a repair
    target out of a git hiccup).
    """
    rel = dist_rel_dir if dist_rel_dir.endswith("/") else dist_rel_dir + "/"
    try:
        proc = _run_git(install_root, ["status", "--porcelain", "--", rel])
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    out: list[str] = []
    text = proc.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if code == "??":
            continue
        # Rename entries render as `R  old -> new`; the live path is the RHS.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        # git quotes paths containing specials; strip the wrapper so the value
        # round-trips into a later `--` pathspec.
        if path.startswith('"') and path.endswith('"') and len(path) >= 2:
            path = path[1:-1]
        if path:
            out.append(path)
    return out


def restore_paths_from_head(
    install_root: Path,
    rel_paths: Iterable[str],
    *,
    log: LogFn = _noop_log,
) -> tuple[list[str], list[str]]:
    """``git checkout HEAD -- <path>`` for each path, one at a time.

    Per-path (not per-directory) so the caller learns exactly WHICH file could
    not be written — the Windows-locked-exe case, which then routes to staging.

    **``HEAD`` is explicit, and load-bearing (v0.2.91 fix-round MINOR-2).**
    ``git checkout -- <path>`` restores from the INDEX, not from HEAD. A dist
    binary in the ``M `` (staged-modified) state — trivially reachable via
    ``git add`` on a locally rebuilt binary, and the exact shape a half-finished
    conflict resolution leaves behind — is then "restored" to the STAGED bytes,
    which are the diverged bytes. The command exits 0, this module reports a
    successful repair, ``git status`` still shows the file as diverged from
    HEAD, and the install stays frozen forever with a green log line over it.
    Naming the tree removes the whole class: the only thing this module may ever
    put on disk is what HEAD says belongs there.

    Returns ``(restored, failed)``.
    """
    restored: list[str] = []
    failed: list[str] = []
    for rel in rel_paths:
        try:
            proc = _run_git(install_root, ["checkout", "HEAD", "--", rel])
        except (OSError, subprocess.SubprocessError) as exc:
            log(
                "dist_repair",
                "warn",
                f"git checkout HEAD -- {rel} could not run: {exc}",
            )
            failed.append(rel)
            continue
        if proc.returncode == 0:
            restored.append(rel)
            log("dist_repair", "ok", f"restored {rel} from HEAD")
        else:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            log(
                "dist_repair",
                "warn",
                f"git checkout HEAD -- {rel} failed ({detail or 'no stderr'}); "
                "will try staging it for the stage1 updater instead",
            )
            failed.append(rel)
    return restored, failed


def stage_paths_from_head(
    install_root: Path,
    rel_paths: Iterable[str],
    *,
    log: LogFn = _noop_log,
) -> tuple[list[str], list[str]]:
    """Write HEAD's blob for each path to ``<target>.new``.

    The bytes are written to a ``.new.tmp`` sibling and renamed onto ``.new``,
    so a killed install can never leave a truncated staged binary for the
    updater to rename over a working one.

    Returns ``(staged, failed)``.
    """
    staged: list[str] = []
    failed: list[str] = []
    for rel in rel_paths:
        try:
            proc = _run_git(install_root, ["show", f"HEAD:{rel}"])
        except (OSError, subprocess.SubprocessError) as exc:
            log("dist_repair", "warn", f"git show HEAD:{rel} could not run: {exc}")
            failed.append(rel)
            continue
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            log("dist_repair", "warn", f"git show HEAD:{rel} failed: {detail}")
            failed.append(rel)
            continue

        target = install_root / rel
        staged_path = staged_sibling(target)
        tmp_path = staged_path.with_name(staged_path.name + ".tmp")
        try:
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(proc.stdout)
            os.replace(tmp_path, staged_path)
        except OSError as exc:
            log("dist_repair", "warn", f"could not stage {staged_path}: {exc}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            failed.append(rel)
            continue
        staged.append(rel)
        log(
            "dist_repair",
            "ok",
            f"staged HEAD's {rel} at {staged_path} ({len(proc.stdout)} bytes) "
            "for the stage1 updater swap",
        )
    return staged, failed


def run_repair_leg(
    install_root: Path,
    *,
    dist_rel_dir: str,
    binary_name: str,
    launcher_driven: bool,
    launcher_pid_env: str = "",
    log: LogFn = _noop_log,
    on_restart_required: Optional[Callable[[int], None]] = None,
    on_swap_locked: Optional[Callable[[str], None]] = None,
    invoke_stage1: Optional[Callable[[Optional[int]], Optional[object]]] = None,
) -> RepairOutcome:
    """Full repair leg, decisions included. install.py's shim only supplies
    the three side-effecting callbacks (its two deferral emitters + the stage1
    updater spawn) so no decision logic lives in the monolith.

    ``launcher_driven`` is ``VCT_AUTO_RESTART_LAUNCHER=1`` — the v0.2.54 C-5
    guard. When the LAUNCHER drove this install.py run it owns both the restart
    and the stage1 handoff: its finalize tail picks up any ``<target>.new`` we
    staged and spawns ``vct-updater`` right before exiting. Spawning here as
    well reproduces the pre-v0.2.54 bug where updater #1's 30 s parent-wait
    deterministically timed out against the launcher's up-to-5-min
    ``WaitForBinaryRefresh``, orphaning ``update.lock.json`` and briefly running
    two updaters. The restart deferral is redundant on that path for the same
    reason.

    Soft-fail: any exception inside the repair pass is logged and swallowed —
    a repair failure must never abort an install.
    """
    try:
        outcome = repair_dist_binaries(install_root, dist_rel_dir, log=log)
    except Exception as exc:  # noqa: BLE001 — best-effort repair
        log("dist_repair", "warn", f"dist repair pass failed: {exc}")
        return RepairOutcome()

    if not outcome.dirty:
        return outcome

    launcher_pid: Optional[int] = None
    if launcher_pid_env.strip():
        try:
            launcher_pid = int(launcher_pid_env.strip())
        except ValueError:
            launcher_pid = None
    if launcher_pid is None and not launcher_driven:
        launcher_pid = scan_for_launcher_pid(binary_name)

    if outcome.restored and launcher_pid is not None and not launcher_driven:
        # The canonical path now holds HEAD's binary but the running process
        # still executes the old image. Surface the honest "restart to pick it
        # up" banner; the launcher never restarts itself.
        if on_restart_required is not None:
            on_restart_required(launcher_pid)

    if outcome.handoff_needed:
        if launcher_driven:
            log(
                "dist_repair",
                "ok",
                "launcher-driven update: staged .new only; the launcher's own "
                "stage1 handoff spawns vct-updater after it exits (C-5: avoids "
                "the double-updater + 30s parent-wait timeout)",
            )
        elif invoke_stage1 is not None and invoke_stage1(launcher_pid) is None:
            if on_swap_locked is not None:
                on_swap_locked(
                    "git checkout could not rewrite "
                    f"{', '.join(outcome.staged)} (file locked); HEAD's bytes are "
                    "staged as `<target>.new` but no stage1 handoff was possible"
                )

    if outcome.failed:
        log(
            "dist_repair",
            "warn",
            "could not restore or stage: " + ", ".join(outcome.failed),
        )
    return outcome


def repair_dist_binaries(
    install_root: Path,
    dist_rel_dir: str,
    *,
    log: LogFn = _noop_log,
) -> RepairOutcome:
    """Bring ``dist_rel_dir`` back in line with HEAD, restore-first.

    This is the permanent no-working-launcher escape hatch: it needs only git
    and a python interpreter, so it repairs an install whose launcher binary is
    frozen, mis-copied, or missing entirely.

    Never touches anything outside ``dist_rel_dir``, and never touches an
    untracked file.
    """
    dirty = dist_dirty_paths(install_root, dist_rel_dir)
    if not dirty:
        return RepairOutcome()

    log(
        "dist_repair",
        "info",
        f"{len(dirty)} dist file(s) diverge from HEAD under {dist_rel_dir}: "
        f"{', '.join(dirty)}",
    )
    restored, could_not_restore = restore_paths_from_head(install_root, dirty, log=log)
    staged, failed = ([], [])
    if could_not_restore:
        staged, failed = stage_paths_from_head(
            install_root, could_not_restore, log=log
        )
    return RepairOutcome(
        dirty=tuple(dirty),
        restored=tuple(restored),
        staged=tuple(staged),
        failed=tuple(failed),
    )


# ---------------------------------------------------------------------------
# Launcher-PID discovery (the docstring-promised "process scan")
# ---------------------------------------------------------------------------


def _pids_from_tasklist(binary_name: str) -> list[int]:
    """Windows: parse ``tasklist /FO CSV /NH`` output for ``binary_name``."""
    try:
        proc = subprocess.run(
            [
                "tasklist",
                "/FI",
                f"IMAGENAME eq {binary_name}",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    pids: list[int] = []
    text = proc.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        # `"vct-launcher.exe","1234","Console","1","50,000 K"`
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        head = parts[0].lstrip('"')
        if head.lower() != binary_name.lower():
            continue
        try:
            pids.append(int(parts[1]))
        except ValueError:
            continue
    return pids


def _pids_from_ps(binary_name: str) -> list[int]:
    """POSIX: ``pgrep -x`` first, falling back to a ``ps`` scan."""
    stem = binary_name[:-4] if binary_name.lower().endswith(".exe") else binary_name
    try:
        proc = subprocess.run(
            ["pgrep", "-x", stem],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
        if proc.returncode == 0:
            return [
                int(t)
                for t in proc.stdout.decode("utf-8", errors="replace").split()
                if t.isdigit()
            ]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,comm="],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    pids: list[int] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, comm = parts
        if comm.strip() == stem and pid_s.isdigit():
            pids.append(int(pid_s))
    return pids


def scan_for_launcher_pid(
    binary_name: str = "vct-launcher.exe",
    *,
    exclude_pid: Optional[int] = None,
) -> Optional[int]:
    """Find a running launcher PID, or ``None``.

    Implements the fallback ``_try_invoke_windows_stage1_updater`` promised in
    its docstring but never had: when install.py is run from a terminal there is
    no ``$VCT_LAUNCHER_PID``, yet a launcher may well be running and holding the
    binary open — which is precisely when the stage1 handoff is required.

    Conservative on ambiguity: when several launchers are running we return the
    LOWEST pid rather than guessing, and the caller's handoff is a no-op if that
    process is not the one holding the lock (the updater simply times out its
    parent-wait and leaves the staged ``.new`` for the next attempt). Returns
    ``None`` on any probe failure — never blocks the install.
    """
    if platform.system().lower().startswith("win"):
        pids = _pids_from_tasklist(binary_name)
    else:
        pids = _pids_from_ps(binary_name)

    own = os.getpid()
    candidates = sorted(
        p for p in pids if p > 0 and p != own and p != (exclude_pid or -1)
    )
    return candidates[0] if candidates else None
