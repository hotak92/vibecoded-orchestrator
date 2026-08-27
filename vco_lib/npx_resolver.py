# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for Node-CLI resolution (``npx`` / ``npm``) + command resolvability.

v0.2.91 WP-D. Before this module the npx-resolution ladder lived ONLY inside
``install.py`` (``_find_npx``, v0.2.51 Bug E), where its answer reached exactly
one caller — the Playwright pre-cache — and its NEGATIVE answer produced a
stdout notice claiming the MCP "will lazy-install when first invoked". That
claim is false: the registered entry is ``{"command": "npx", ...}``, so with no
npx on PATH the MCP cannot spawn at all, and there is nothing to lazy-install
INTO. The field consequence was months of silently-dead npx MCPs on a machine
whose npx was missing (fnm shipped it; only ``node``/``npm`` were symlinked).

Four consumers now share this one home:

1. ``install.py`` — the Playwright pre-cache (thin wrappers ``_find_npx`` /
   ``_find_npm`` delegate here).
2. ``vco_lib.doctor`` — the npx probe that emits the honest deferral.
3. The launcher's registration-health badge — Rust shells out to
   ``python -m vco_lib.npx_resolver --json`` (the A-leg of the A>B>C rule:
   ONE implementation called cross-language, never a Rust re-implementation of
   the 4-step ladder that would drift the moment a fifth step is added).
   Doctor-time only, never a hot path.
4. The mermaid / excalidraw wrapper proxies — :func:`package_run_argv` builds
   the upstream spawn argv with the ``npm exec`` fallback.

Cross-OS
--------
* POSIX: ``os.access(path, os.X_OK)`` confirms executability.
* Windows: there is no executable bit — ``is_file()`` is the relevant check,
  and each candidate is probed with its ``.cmd`` / ``.ps1`` variants.

Nothing here raises: symlink loops, permission errors and hostile paths all
fall through to the next candidate or to ``None``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

#: Output contract version for the ``--json`` CLI (the Rust probe pins it).
SCHEMA_VERSION = 1


def _is_windows() -> bool:
    return sys.platform == "win32"


def _candidate_names(stem: str) -> tuple[str, ...]:
    """``stem`` plus the Windows shim suffixes, in probe order."""
    if _is_windows():
        return (stem, f"{stem}.cmd", f"{stem}.ps1")
    return (stem,)


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and (_is_windows() or os.access(path, os.X_OK))
    except OSError:
        return False


def find_npx() -> Optional[str]:
    """Locate npx with fnm/nvm-style fallback (verbatim v0.2.51 Bug E ladder).

    Returns the absolute path to the npx binary, or ``None`` if truly absent.

    Strategy (each candidate gets the .cmd / .ps1 Windows variants too):
      1. ``shutil.which("npx")`` — the common case (npx is on PATH).
      2. ``dirname(which("npm"))/npx`` — sibling of the symlink target
         (covers the case where the user symlinked `npm` to ~/.local/bin/
         but the same dir also has `npx` as a sibling — common on apt /
         brew where both ship together).
      3. ``dirname(realpath(which("npm")))/npx`` — sibling of the REAL npm.
         For fnm/nvm this lands in ``lib/node_modules/npm/bin/`` because npm
         itself is a symlink to ``npm-cli.js`` in that dir. The npx shim
         there is sometimes a broken-without-context cli (it imports node
         modules relatively); we probe it but it's not always runnable on
         its own.
      4. Climb up from the realpath: if real npm lives at
         ``<root>/lib/node_modules/npm/bin/npm-cli.js``, probe
         ``<root>/bin/npx`` — the canonical fnm/nvm shim that wraps node +
         npx-cli.js with the right argv. This is the path that works at
         runtime on real fnm/nvm setups.

    Reported 2026-06-09 from a user's machine: ``~/.local/bin/npm`` was
    symlinked to ``~/.fnm/node-versions/v20.20.1/installation/bin/npm``; the
    corresponding npx sits in the same dir but not on PATH, so
    ``shutil.which("npx")`` failed and both the Playwright pre-cache and the
    bundled-npm pinning skipped silently on a working Node install.
    """
    direct = shutil.which("npx")
    if direct:
        return direct
    npm = shutil.which("npm")
    if not npm:
        return None

    # Build candidate base directories. Order matters: prefer the canonical
    # fnm/nvm shim (case 4) over the broken-on-its-own
    # ``lib/node_modules/npm/bin/npx`` (case 3) because the former actually runs.
    npm_path = Path(npm)
    candidate_dirs: list[Path] = [npm_path.parent]  # case 2 (apt / brew layout)

    try:
        real_npm: Optional[Path] = npm_path.resolve()
    except OSError:
        real_npm = None
    if real_npm is not None:
        # Case 4: canonical fnm/nvm <root>/bin — derive from the real npm path
        # by climbing out of lib/node_modules/npm/bin.
        parts = real_npm.parts
        try:
            idx = parts.index("node_modules")
            # <root>/lib/node_modules/npm/bin/npm-cli.js → need parts[idx-1]=="lib".
            if idx >= 2 and parts[idx - 1] == "lib":
                candidate_dirs.append(Path(*parts[: idx - 1]) / "bin")
        except ValueError:
            pass
        # Case 3: sibling of the real npm-cli.js. Lower priority because this
        # shim sometimes doesn't run standalone, but it's the right answer for
        # distros shipping `npm` as a bare executable (not the *-cli.js shape).
        candidate_dirs.append(real_npm.parent)

    seen: set[Path] = set()
    for d in candidate_dirs:
        for name in _candidate_names("npx"):
            cand = d / name
            if cand in seen:
                continue
            seen.add(cand)
            if _usable(cand):
                return str(cand)
    return None


def find_npm() -> Optional[str]:
    """Locate npm. ``None`` when absent.

    No fallback heuristic: if ``npm`` is not on PATH there is nothing to
    dirname-realpath off (the npx ladder is derived FROM npm's location).
    The caller decides whether to surface an install hint or skip.
    """
    return shutil.which("npm") or None


def resolve_command(name: str) -> Optional[str]:
    """Resolve a bare command name to an absolute path, or ``None``.

    ``npx`` gets the full :func:`find_npx` ladder; every other name gets
    ``shutil.which`` (which already handles ``.cmd``/``.exe`` resolution on
    Windows via PATHEXT). This is the function the registration-health badge
    asks about for a bare-name MCP ``command``: an entry Claude Code cannot
    resolve on PATH is an MCP that can never spawn.
    """
    if not name:
        return None
    if name == "npx":
        return find_npx()
    if name == "npm":
        return find_npm()
    return shutil.which(name) or None


def package_run_argv(package: str, version: str = "") -> Optional[list[str]]:
    """Argv that runs an npm package one-shot, or ``None`` when Node is absent.

    Preference order:

    1. ``[<npx>, "-y", "<package>@<version>"]`` — the shape VCO registers and
       the one every wrapper used before v0.2.91.
    2. ``[<npm>, "exec", "--yes", "--", "<package>@<version>"]`` — the
       equivalent invocation when npx is missing but npm is present. Same
       precedent as ``scripts/build-bundled-launcher.sh`` (which falls back
       through pnpm → npx → package-manager exec for exactly this reason).
    3. ``None`` — neither resolvable; the caller must fail loudly.

    ``--`` before the spec is load-bearing: without it ``npm exec`` treats a
    later ``--flag`` as its own.
    """
    spec = f"{package}@{version}" if version else package
    npx = find_npx()
    if npx:
        return [npx, "-y", spec]
    npm = find_npm()
    if npm:
        return [npm, "exec", "--yes", "--", spec]
    return None


def probe(commands: Optional[Iterable[str]] = None) -> dict:
    """Machine-readable resolvability snapshot.

    Args:
        commands: extra bare command names to resolve (e.g. every bare
            ``command`` found in ``~/.claude.json``). ``npx``/``npm`` are
            always included.

    Returns a dict with :data:`SCHEMA_VERSION`, the npx/npm verdicts, and a
    ``commands`` map of ``name -> absolute path or None``.
    """
    names: list[str] = ["npx", "npm"]
    for c in commands or ():
        if c and c not in names:
            names.append(c)
    resolved = {name: resolve_command(name) for name in names}
    return {
        "schema_version": SCHEMA_VERSION,
        "npx_present": resolved["npx"] is not None,
        "npx_path": resolved["npx"] or "",
        "npm_present": resolved["npm"] is not None,
        "npm_path": resolved["npm"] or "",
        "commands": resolved,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.npx_resolver [--json] [--command NAME]...``

    Exit code is 0 whenever the probe RAN — resolvability is reported in the
    payload, never in the exit status. A non-zero exit would be
    indistinguishable from "the interpreter could not start", and the Rust
    caller must be able to tell "npx is missing" from "I could not ask".
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.npx_resolver",
        description=(
            "Resolve npx / npm (with the fnm-nvm ladder) and report whether "
            "bare MCP commands are spawnable. Used by the launcher's "
            "registration-health badge and by vco doctor."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit the machine-readable payload (default: human lines)",
    )
    parser.add_argument(
        "--command", dest="commands", action="append", default=[],
        metavar="NAME", help="extra bare command name to resolve (repeatable)",
    )
    args = parser.parse_args(argv)
    payload = probe(args.commands)
    if args.json:
        print(json.dumps(payload))
        return 0
    for name, path in payload["commands"].items():
        print(f"{name}: {path or 'NOT RESOLVABLE'}")
    return 0


__all__ = [
    "SCHEMA_VERSION",
    "find_npm",
    "find_npx",
    "main",
    "package_run_argv",
    "probe",
    "resolve_command",
]


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
