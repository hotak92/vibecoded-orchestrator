# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Recognise the launcher CLI (``launcher/tools/vct-cli``) under a FORMER name.

The launcher's Rust command-line tool was ``vct`` before v0.1.0 (now the bash
secrets tool, ``tools/vct-secrets/vct``), ``vco`` from v0.1.0 to v0.2.96 (now
the orchestrator's Python CLI, ``vco doctor`` …) and is ``vct-cli`` since
v0.2.97. A copy still on PATH under an old name hides — or is hidden by — the
real program of that name, and a user told "run ``vco doctor``" gets
"unrecognized subcommand".

This module is the ONE home of the identity rule and of what to do about a
match. Two callers:

* ``launcher/tools/vct-cli/install.sh`` (through ``retire-old-names.sh`` →
  ``python -m vco_lib.launcher_cli_identity retire --bin-dir <dir>``) removes
  the copy an earlier run of that script installed;
* the doctor probe ``former_launcher_cli_on_path`` REPORTS copies anywhere on
  PATH as ``former_launcher_cli_on_path`` and never deletes anything; the
  registry clear probe re-runs the same reading.

The rule is positive evidence only: a file is this CLI under ``<name>`` when
``--version`` prints ``<name> <digit>…`` AND ``-h`` carries the tagline every
release printed. ``-h``, not ``--help``: clap prints the one-line ``about``
for ``-h`` and the ``long_about`` (which lacks the tagline) for ``--help``.
The Python ``vco`` fails the first test (its ``--version`` is an argparse
error), the secrets ``vct`` the second.

Standard library only: ``install.sh`` may run this with a bare ``python3``.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

#: Names this CLI shipped under, newest first.
FORMER_NAMES: tuple = ("vco", "vct")
#: The name it ships under now.
CURRENT_NAME = "vct-cli"
#: Printed by ``-h`` in every release ("VCT Launcher CLI — …" before v0.1.0,
#: "vibecoded-orchestrator CLI — …" after).
TAGLINE = "CLI — power-user / CI escape hatch."
#: The first words of the Python ``vco``'s ``-h`` description
#: (``vco_lib.cli.__main__._build_parser``; pinned by a test). Recognising it
#: keeps ``retire`` from telling the user to delete the right program.
PYTHON_VCO_DESCRIPTION = "VibeCoded Orchestrator CLI."
#: A hung binary must not hang an install or a doctor pass.
RUN_TIMEOUT_SECONDS = 10

Runner = Callable[[Sequence[str]], str]


def _run(argv: Sequence[str]) -> str:
    """stdout of ``argv``, or ``""`` when it could not run. Never raises."""
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, errors="replace",
            stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return proc.stdout or ""


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _command_name(path: Path) -> str:
    """``vco`` for ``…/vco`` and, on Windows, for ``…\\vco.exe``."""
    if _is_windows():
        exts = {e.lower() for e in os.environ.get("PATHEXT", ".EXE").split(";") if e}
        if path.suffix.lower() in exts:
            return path.stem
    return path.name


def is_launcher_cli_named(path: "str | Path", name: str, *, run: Runner = _run) -> bool:
    """True when ``path`` is an executable that identifies itself as this CLI
    under ``name``. A symlink is judged by what it runs."""
    p = Path(path)
    if not p.is_file() or not os.access(p, os.X_OK):
        return False
    first = (run([str(p), "--version"]).splitlines() or [""])[0].strip()
    if not re.match(rf"{re.escape(name)} \d", first):
        return False
    return TAGLINE in run([str(p), "-h"])


def is_python_vco(path: "str | Path", *, run: Runner = _run) -> bool:
    return PYTHON_VCO_DESCRIPTION in run([str(path), "-h"])


@dataclass(frozen=True)
class FormerCopy:
    path: str
    name: str


def _candidates(directory: Path, name: str) -> List[Path]:
    if not _is_windows():
        return [directory / name]
    exts = [e for e in os.environ.get("PATHEXT", ".EXE").split(";") if e]
    return [directory / name] + [directory / f"{name}{e.lower()}" for e in exts]


def find_on_path(path_env: Optional[str] = None, *, run: Runner = _run) -> List[FormerCopy]:
    """Every copy of this CLI under a former name reachable through PATH —
    not only the first, because a later one is still a stale program."""
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    found: List[FormerCopy] = []
    seen: set = set()
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        for name in FORMER_NAMES:
            for cand in _candidates(Path(entry), name):
                key = os.path.normcase(os.path.abspath(cand))
                if key in seen:
                    continue
                seen.add(key)
                if is_launcher_cli_named(cand, name, run=run):
                    found.append(FormerCopy(str(cand), name))
    return found


# ─── the printed remedy (rendered AND read back here, so they cannot drift) ──

def _delete_verb() -> str:
    return "del" if _is_windows() else "rm"


def _quote(path: str) -> str:
    from vco_lib import remedy_shell

    return remedy_shell.quote(path)


def remedy(copies: Sequence[FormerCopy]) -> str:
    """One delete command per copy, one per line. Never chained."""
    return "\n".join(f"{_delete_verb()} {_quote(c.path)}" for c in copies)


_REMEDY_LINE = re.compile(r"^(?:rm|del) (.+)$")


def paths_in_remedy(text: str) -> List[str]:
    """The paths a :func:`remedy` text names (the clear probe's input)."""
    out: List[str] = []
    for line in (text or "").splitlines():
        m = _REMEDY_LINE.match(line.strip())
        if not m:
            continue
        arg = m.group(1)
        if arg.startswith('"') and arg.endswith('"') and len(arg) >= 2:
            out.append(arg[1:-1].replace('""', '"'))
            continue
        try:
            parts = shlex.split(arg)
        except ValueError:
            continue
        if len(parts) == 1:
            out.append(parts[0])
    return out


def still_present(recorded: Sequence[str], path_env: Optional[str] = None,
                  *, run: Runner = _run) -> bool:
    """True while any recorded copy still is this CLI, or any copy is on PATH.

    Recorded paths are checked by path, so a caller whose PATH differs from
    the user's shell (a launcher, an install run) cannot clear an entry for a
    copy that is still there."""
    for p in recorded:
        name = _command_name(Path(p))
        if name in FORMER_NAMES and is_launcher_cli_named(p, name, run=run):
            return True
    return bool(find_on_path(path_env, run=run))


# ─── install.sh: retire what an earlier install.sh put in <bin_dir> ──────

def retire(bin_dir: "str | Path", path_env: Optional[str] = None, *,
           home: Optional[Path] = None, run: Runner = _run,
           out: Callable[[str], None] = print) -> None:
    """Remove this CLI's copies under former names from ``bin_dir`` (where
    ``install.sh`` installs), then report copies elsewhere on PATH.

    Removes only a REGULAR file that identifies itself; a symlink, a Python
    ``vco``, a secrets ``vct`` and anything unidentified are left alone."""
    bin_path = Path(bin_dir)
    home = Path.home() if home is None else home
    handled: set = set()
    for name in FORMER_NAMES:
        old = bin_path / name
        handled.add(os.path.normcase(os.path.abspath(old)))
        regular = old.is_file() and not old.is_symlink()
        if regular and is_launcher_cli_named(old, name, run=run):
            old.unlink()
            out(f"[vct-cli] Removed {old}: it was this CLI under its old name `{name}`.")
            secrets = home / ".vct-secrets" / "vct"
            if name == "vct" and secrets.exists() and not old.exists():
                out(f"[vct-cli] Restoring symlink: {old} -> {secrets}")
                old.symlink_to(secrets)
        elif name == "vco" and regular and not is_python_vco(old, run=run):
            out(f"[vct-cli] Left {old} in place: it does not identify itself as this CLI.")
            out(f"          If it is an old copy of the launcher CLI, delete it:  rm {_quote(str(old))}")
            out(f"          The launcher CLI is `{CURRENT_NAME}` now; `vco` is the orchestrator's Python CLI.")
        elif old.is_symlink() and is_launcher_cli_named(old, name, run=run):
            out(f"[vct-cli] NOTE: {old} is a symlink to an old copy of this CLI (named `{name}`).")
            out(f"          Remove the link:  rm {_quote(str(old))}")
    for copy in find_on_path(path_env, run=run):
        if os.path.normcase(os.path.abspath(copy.path)) in handled:
            continue
        out(f"[vct-cli] NOTE: {copy.path} is an old copy of this CLI (named `{copy.name}`).")
        out(f"          It hides or shadows the real `{copy.name}`. Remove it:  rm {_quote(copy.path)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.launcher_cli_identity",
        description="Find or retire copies of the launcher CLI under its former names.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("retire", help="What launcher/tools/vct-cli/install.sh runs.")
    r.add_argument("--bin-dir", required=True)
    sub.add_parser("scan", help="Print each former-name copy on PATH (read-only).")
    args = parser.parse_args(argv)
    if args.command == "retire":
        retire(args.bin_dir)
        return 0
    for copy in find_on_path():
        print(f"{copy.name}\t{copy.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
