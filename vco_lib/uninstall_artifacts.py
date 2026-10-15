# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall step [7]: what the install places OUTSIDE the clone.

``install.py --uninstall`` (``_run_uninstall``) promises it "removes ONLY
orchestrator-managed paths". Until v0.2.103 it removed the launcher DB, the
boot services and the model-gateway state, but nothing else VCO writes outside
the clone (review V02103-FINAL2 M-1):

* the prebuilt launcher ``first-install.bat`` downloads WITHOUT a prompt into
  ``%LOCALAPPDATA%\\vct-launcher\\`` (``vct-launcher.exe``, ``vct-hub.exe``,
  ``vct-updater.exe``) and the ``VCT Launcher.lnk`` shortcuts it writes on the
  Desktop and in the Start Menu (``first-install.bat`` /
  ``scripts/post-install-launcher.ps1``);
* the macOS ``VCT Launcher.app`` ``scripts/post-install-launcher.sh`` copies
  into ``/Applications`` (or ``~/Applications``) and the ``~/Applications``
  link it refreshes; the flat binary it stages in
  ``~/.local/share/vct-launcher/`` (Linux, and the macOS no-bundle fallback);
* the ``vct-launcher.desktop`` entries (menu + ``~/Desktop``) written by that
  script and by the launcher's ``desktop_shortcut.rs``;
* state files this cycle added under the VCO state root:
  ``state/engine-start.lock`` (``container_engine_start``),
  ``state/codegraph-walk-claims/*.lock`` (``codegraph_walk``),
  ``model-guidance.json`` (``model_selection``) and the engine starter's log
  ``logs/engine-start.log`` (bounded in place, no rotated siblings).

Ownership is decided per path, never by a directory walk: an exact name VCO
writes AND, where the name alone could be a user's, a VCO signature in the
content (a ``.lnk`` naming ``vct-launcher``, a ``.desktop`` entry with
``Name=VCT Launcher``, an ``.app`` whose ``CFBundleIdentifier`` is the
launcher's). A directory is removed only when it is empty after its named
files are gone, so a file someone else put there survives.

``backups/kg-reviewer-outside/`` is NEVER removed: it holds copies of the
user's own bytes (the KG reviewer backs up before it reverts). The plan lists
it as left in place so the user decides.

A running ``vct-hub`` is stopped first (its own ``--stop``) because Windows
cannot delete a running ``.exe``; a file that still cannot be removed (the
launcher GUI is open) is reported with what to do, and the step continues.
Never raises.
"""
from __future__ import annotations

import os
import plistlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

__all__ = [
    "LAUNCHER_BUNDLE_ID", "WINDOWS_LAUNCHER_EXES", "Plan", "Removal",
    "build_plan", "plan_lines", "execute",
]

#: ``identifier`` in ``launcher/src-tauri/tauri.conf.json`` (must match).
LAUNCHER_BUNDLE_ID = "com.vibecodedtools.launcher"
#: ``productName`` in ``tauri.conf.json`` -> the bundle / shortcut name.
LAUNCHER_PRODUCT_NAME = "VCT Launcher"
#: What ``scripts/lib/download-launcher-asset.ps1`` lands in -DestDir.
WINDOWS_LAUNCHER_EXES = ("vct-launcher.exe", "vct-hub.exe", "vct-updater.exe")
#: ``first-install.bat`` ``DEST_DIR=%LOCALAPPDATA%\\vct-launcher``.
WINDOWS_LAUNCHER_DIRNAME = "vct-launcher"
LNK_NAME = f"{LAUNCHER_PRODUCT_NAME}.lnk"
DESKTOP_ENTRY_NAME = "vct-launcher.desktop"
APP_BUNDLE_NAME = f"{LAUNCHER_PRODUCT_NAME}.app"
#: The engine starter's own outcome log (``container_engine_start``).
ENGINE_START_LOG = "engine-start.log"

_LNK_SIGNATURES = (b"vct-launcher", "vct-launcher".encode("utf-16-le"))


@dataclass(frozen=True)
class Removal:
    path: Path
    #: "file" (unlink), "link" (unlink the symlink only), "tree" (rmtree a
    #: verified VCO .app bundle).
    kind: str
    label: str


@dataclass
class Plan:
    remove: list[Removal] = field(default_factory=list)
    #: Directories removed only when empty after the named files are gone.
    prune_if_empty: list[Path] = field(default_factory=list)
    #: (path, reason) — VCO wrote it but it holds the user's bytes.
    keep: list[tuple[Path, str]] = field(default_factory=list)
    #: A ``vct-hub`` binary to ``--stop`` before removal (Windows locks).
    hub_binaries: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------
# ownership predicates
# --------------------------------------------------------------------------

def _is_vco_lnk(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        data = path.read_bytes()[: 64 * 1024].lower()
    except OSError:
        return False
    return any(sig in data for sig in _LNK_SIGNATURES)


def _is_vco_desktop_entry(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")[: 64 * 1024]
    except OSError:
        return False
    lines = {ln.strip() for ln in text.splitlines()}
    return "[Desktop Entry]" in lines and f"Name={LAUNCHER_PRODUCT_NAME}" in lines


def _bundle_id(app: Path) -> Optional[str]:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _is_vco_app(app: Path) -> bool:
    return _bundle_id(app) == LAUNCHER_BUNDLE_ID


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def _windows_desktop_dirs(env: Mapping[str, str]) -> list[Path]:
    """``%USERPROFILE%\\Desktop`` (first-install.bat) and the shell's known
    Desktop folder (post-install-launcher.ps1 — OneDrive may redirect it)."""
    dirs: list[Path] = []
    profile = env.get("USERPROFILE")
    if profile:
        dirs.append(Path(profile) / "Desktop")
    known = _known_windows_desktop() if os.name == "nt" else None
    if known is not None and known not in dirs:
        dirs.append(known)
    return dirs


def _known_windows_desktop() -> Optional[Path]:  # pragma: no cover - Windows
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSIDL_DESKTOPDIRECTORY = 0x10, SHGFP_TYPE_CURRENT = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:  # type: ignore[attr-defined]
            return Path(buf.value)
    except Exception:  # noqa: BLE001 — best effort; USERPROFILE covers .bat
        return None
    return None


def _add_file(plan: Plan, path: Path, label: str) -> None:
    if path.is_file() or path.is_symlink():
        plan.remove.append(Removal(path, "file", label))


def build_plan(
    *,
    os_name: str,
    home: Path,
    vct_root: Path,
    env: Optional[Mapping[str, str]] = None,
    system_applications: Path = Path("/Applications"),
) -> Plan:
    """Every VCO-written path outside the clone that exists on this machine.

    Pure apart from reads; ``os_name`` is ``platform.system()``.
    """
    env = os.environ if env is None else env
    plan = Plan()

    # --- VCO state files added this cycle (every OS) ----------------------
    state = vct_root / "state"
    _add_file(plan, state / "engine-start.lock", "container-engine start lock")
    claims = state / "codegraph-walk-claims"
    if claims.is_dir() and not claims.is_symlink():
        for lock in sorted(claims.glob("*.lock")):
            _add_file(plan, lock, "code-graph walk claim")
        plan.prune_if_empty.append(claims)
    plan.prune_if_empty.append(state)
    _add_file(plan, vct_root / "model-guidance.json", "model-guidance record")
    logs = vct_root / "logs"
    _add_file(plan, logs / ENGINE_START_LOG, "container-engine start log")
    outside = vct_root / "backups" / "kg-reviewer-outside"
    if outside.exists():
        plan.keep.append((outside, "backups of your own files made by the KG reviewer"))

    # --- launcher binaries + shortcuts ------------------------------------
    if os_name == "Windows":
        local = env.get("LOCALAPPDATA")
        if local:
            dest = Path(local) / WINDOWS_LAUNCHER_DIRNAME
            if dest.is_dir() and not dest.is_symlink():
                for exe in WINDOWS_LAUNCHER_EXES:
                    _add_file(plan, dest / exe, "prebuilt launcher binary")
                if (dest / "vct-hub.exe").is_file():
                    plan.hub_binaries.append(dest / "vct-hub.exe")
                plan.prune_if_empty.append(dest)
        lnk_dirs = list(_windows_desktop_dirs(env))
        appdata = env.get("APPDATA")
        if appdata:
            lnk_dirs.append(
                Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            )
        for d in lnk_dirs:
            lnk = d / LNK_NAME
            if _is_vco_lnk(lnk):
                plan.remove.append(Removal(lnk, "file", "launcher shortcut"))
        return plan

    # Linux, and the macOS no-.app fallback, stage the flat binary here.
    flat_dir = home / ".local" / "share" / "vct-launcher"
    if flat_dir.is_dir() and not flat_dir.is_symlink():
        _add_file(plan, flat_dir / "vct-launcher", "prebuilt launcher binary")
        plan.prune_if_empty.append(flat_dir)

    if os_name == "Linux":
        xdg = env.get("XDG_DATA_HOME")
        apps = Path(xdg) / "applications" if xdg else home / ".local" / "share" / "applications"
        for entry in (apps / DESKTOP_ENTRY_NAME, home / "Desktop" / DESKTOP_ENTRY_NAME):
            if _is_vco_desktop_entry(entry):
                plan.remove.append(Removal(entry, "file", "launcher desktop entry"))
    elif os_name == "Darwin":
        for apps_dir in (system_applications, home / "Applications"):
            app = apps_dir / APP_BUNDLE_NAME
            if app.is_symlink():
                if _is_vco_app(app):  # follows the link to read the plist
                    plan.remove.append(Removal(app, "link", "launcher Applications link"))
            elif app.is_dir() and _is_vco_app(app):
                plan.remove.append(Removal(app, "tree", "launcher app bundle"))
    return plan


def plan_lines(plan: Plan) -> list[str]:
    """The ``[7]`` lines of the uninstall plan (``--dry-run`` prints them)."""
    if not plan.remove and not plan.keep:
        return ["  [7] [skip] No launcher binaries, shortcuts or VCO state files outside the clone"]
    out = ["  [7] Remove launcher binaries, shortcuts and VCO state files outside the clone:"]
    out += [f"       - {r.path}  ({r.label})" for r in plan.remove]
    if plan.hub_binaries:
        out.append("       (a running vct-hub is stopped first: `vct-hub --stop`)")
    out += [f"       LEFT IN PLACE: {p}  ({why})" for p, why in plan.keep]
    return out


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------

def _default_hub_running() -> bool:
    try:
        from vco_lib import hub_ensure

        return hub_ensure.is_running()
    except Exception:  # noqa: BLE001 — treat as not running; removal reports locks
        return False


def _default_runner(cmd: Sequence[str], timeout: int = 15) -> Optional[int]:
    from vco_lib.boot_service import run_quiet

    return run_quiet(cmd, timeout=timeout)


def execute(
    plan: Plan,
    *,
    dry_run: bool = False,
    extra_hub_binaries: Sequence[Optional[Path]] = (),
    hub_running: Optional[Callable[[], bool]] = None,
    runner: Optional[Callable[..., Optional[int]]] = None,
) -> list[str]:
    """Remove the planned paths. Returns audit lines; never raises."""
    audit: list[str] = []
    if dry_run:
        return [f"would remove {r.path}" for r in plan.remove] + [
            f"left in place: {p} ({why})" for p, why in plan.keep
        ]

    hubs = [p for p in [*plan.hub_binaries, *extra_hub_binaries] if p is not None]
    if hubs and (hub_running or _default_hub_running)():
        run = runner or _default_runner
        for hub in hubs:
            if Path(hub).is_file():
                rc = run([str(hub), "--stop"], timeout=30)
                audit.append(
                    f"stopped vct-hub via `{hub} --stop`" if rc == 0
                    else f"WARN: `{hub} --stop` "
                    + ("could not be spawned / timed out" if rc is None else f"exited {rc}")
                )
                break

    for item in plan.remove:
        try:
            if item.kind == "tree":
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
            audit.append(f"removed {item.path}")
        except FileNotFoundError:
            audit.append(f"no {item.path} (already gone)")
        except OSError as exc:
            audit.append(
                f"WARN: could not remove {item.path}: {exc} — if the launcher or "
                "vct-hub is running from it, close the launcher, run "
                "`vct-hub --stop`, then remove it by hand or re-run the uninstall"
            )

    for d in plan.prune_if_empty:
        try:
            if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
                d.rmdir()
                audit.append(f"removed empty {d}")
        except OSError:
            pass  # not empty / in use: left in place, nothing to report

    for p, why in plan.keep:
        audit.append(f"left in place: {p} ({why})")
    return audit
