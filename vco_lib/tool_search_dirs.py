# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where a tool lives when THIS process's PATH does not say (v0.2.97 R9 H1/H5).

"Is podman installed?" used to mean ``shutil.which("podman")`` — i.e. "is it on
the PATH of whoever is asking". A boot unit, a GUI-launched launcher and the hub
inherit a short PATH, so a rootless Docker in ``~/bin`` or a Homebrew podman in
``/opt/homebrew/bin`` read as NOT INSTALLED there — and "the recorded runtime is
not installed" is the one shape on which the runtime-record reconcile may drive
the OTHER runtime, whose volumes do not hold the data.

The list of usual install locations is ONE committed table,
``vco_lib/tool_search_dirs.toml`` (tier B: the Rust launcher/hub PATH augment
parses the same file; ``tests/fixtures/tool_search_dirs_cases.json`` pins the
expansion rule on both sides). This module is its Python reader:

* :func:`candidate_dirs` — the table's directories for an OS, expanded;
* :func:`which` — PATH first, then those directories (so a tool on PATH is
  always the one found, exactly as before);
* :func:`run` — :func:`subprocess.run` that can SPAWN such a tool: a bare
  program name found only in the table is run by its absolute path, with its
  directory appended to the child's PATH (a compose provider next to it);
* :func:`reachable_path` — the PATH a shell needs so the runtime tools found
  only in the table resolve by name (the boot wrappers and the session hooks
  apply it; see ``vco_lib.containers`` ``--shell``);
* ``python -m vco_lib.tool_search_dirs search-path`` — prints that PATH.

Stdlib only (``tomllib``): it runs from install.py's pre-venv phase through
``vco_lib.containers`` / ``vco_lib.runtime_reconcile``. A missing or malformed
table is a broken install and raises — never a silent empty list.
"""
from __future__ import annotations

import argparse
import functools
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ENV_OVERRIDE",
    "RUNTIME_TOOLS",
    "TABLE_PATH",
    "candidate_dirs",
    "expand_entry",
    "find_in_dirs",
    "os_key",
    "path_separator",
    "reachable_path",
    "run",
    "which",
    "main",
]

TABLE_PATH = Path(__file__).with_name("tool_search_dirs.toml")
SUPPORTED_FORMAT_VERSION = 1

#: Set (even to ``""``) → REPLACES the table's list for this OS. Must match
#: ``runtime.rs::TOOL_SEARCH_DIRS_ENV``.
ENV_OVERRIDE = "VCT_TOOL_SEARCH_DIRS"

#: The tools a shell must be able to reach by name to drive VCO's stack:
#: the two runtimes and their standalone compose front-ends.
RUNTIME_TOOLS: tuple[str, ...] = ("podman", "docker", "podman-compose", "docker-compose")

_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(.*)$", re.DOTALL)


def os_key(system: Optional[str] = None) -> str:
    """``linux`` / ``macos`` / ``windows`` (the table's keys), ``other`` else."""
    s = (system if system is not None else platform.system()).strip().lower()
    return {"linux": "linux", "darwin": "macos", "macos": "macos",
            "windows": "windows"}.get(s, "other")


def path_separator(os_name: str) -> str:
    return ";" if os_name == "windows" else ":"


@functools.lru_cache(maxsize=1)
def _table() -> dict[str, tuple[str, ...]]:
    data = tomllib.loads(TABLE_PATH.read_text(encoding="utf-8"))
    version = data.get("format_version")
    if version != SUPPORTED_FORMAT_VERSION:
        raise ValueError(
            f"{TABLE_PATH} has format_version {version!r}; this reader supports "
            f"{SUPPORTED_FORMAT_VERSION}"
        )
    dirs = data.get("dirs")
    if not isinstance(dirs, dict):
        raise ValueError(f"{TABLE_PATH} has no [dirs] table")
    out: dict[str, tuple[str, ...]] = {}
    for key, value in dirs.items():
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{TABLE_PATH}: [dirs].{key} must be a list of strings")
        out[key] = tuple(value)
    return out


def expand_entry(entry: str, *, home: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """One table entry → a directory, or ``None`` when it cannot be expanded
    (``~`` without a home, ``${NAME}`` unset or empty). MUST MATCH
    ``runtime.rs::expand_search_dir``."""
    if entry == "~" or entry.startswith("~/"):
        if not home:
            return None
        return home.rstrip("/\\") + entry[1:]
    m = _VAR_RE.match(entry)
    if m:
        value = env.get(m.group(1)) or ""
        if not value:
            return None
        return value + m.group(2)
    return entry


def candidate_dirs(
    *,
    os_name: Optional[str] = None,
    home: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The expanded directories for ``os_name`` (default: this OS), in table
    order, duplicates dropped. ``home`` defaults to ``env["HOME"]``; ``env``
    to :data:`os.environ`. :data:`ENV_OVERRIDE` replaces the table's list."""
    source: Mapping[str, str] = os.environ if env is None else env
    name = os_name or os_key()
    if home is None:
        home = source.get("HOME") or None
    if ENV_OVERRIDE in source:
        raw = [p for p in source[ENV_OVERRIDE].split(path_separator(name)) if p.strip()]
    else:
        raw = list(_table().get(name, ()))
    out: list[str] = []
    for entry in raw:
        expanded = expand_entry(entry.strip(), home=home, env=source)
        if expanded and expanded not in out:
            out.append(expanded)
    return out


def _path_of(env: Optional[Mapping[str, str]]) -> str:
    source: Mapping[str, str] = os.environ if env is None else env
    return source.get("PATH", "")


def _executable_names(name: str) -> list[str]:
    if os.name != "nt" or Path(name).suffix:
        return [name]
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    return [name + e.lower() for e in exts] + [name]


def find_in_dirs(name: str, *, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``name`` in the first table directory (:func:`candidate_dirs`) that
    holds it as an executable file — PATH is NOT consulted. Absolute path or
    ``None``."""
    for d in candidate_dirs(env=env):
        for candidate in _executable_names(name):
            p = os.path.join(d, candidate)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def which(name: str, *, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``name`` on PATH (``env``'s, default the process's) — else in the
    first table directory that holds it. The absolute path, or ``None``.

    PATH first, so a tool the caller's PATH reaches is always the one found,
    exactly as ``shutil.which`` answered before; the table only turns "not
    on this PATH" into "found where it is usually installed"."""
    on_path = shutil.which(name) if env is None else shutil.which(name, path=_path_of(env))
    return on_path or find_in_dirs(name, env=env)


def reachable_path(
    tools: Sequence[str] = RUNTIME_TOOLS, *, env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The PATH (``env``'s, default the process's) with the directory of each
    of ``tools`` that is found ONLY through the table APPENDED — so it resolves
    by name — or ``None`` when every tool is either on PATH already or nowhere.

    Appended, not prepended: whatever the PATH already reaches keeps winning;
    the only change is that a tool it could not reach at all now resolves."""
    current = _path_of(env)
    sep = os.pathsep
    parts = [p for p in current.split(sep) if p] if current else []
    added: list[str] = []
    for tool in tools:
        if shutil.which(tool, path=current):
            continue
        hit = find_in_dirs(tool, env=env)
        if hit:
            d = str(Path(hit).parent)
            if d not in parts and d not in added:
                added.append(d)
    if not added:
        return None
    return sep.join(parts + added)


def _is_bare(program: str) -> bool:
    return not (os.sep in program or (os.altsep and os.altsep in program))


def run(argv: Sequence[str], **kw: Any) -> "subprocess.CompletedProcess[Any]":
    """:func:`subprocess.run`, able to spawn a tool found only in the table:
    a bare ``argv[0]`` that is not on PATH but is found by :func:`which` runs
    by its absolute path, and (unless the caller passed ``env``) its directory
    is appended to the child's PATH. Everything else is unchanged."""
    args = list(argv)
    if args and isinstance(args[0], str) and _is_bare(args[0]) and not shutil.which(args[0]):
        hit = find_in_dirs(args[0])
        if hit:
            args[0] = hit
            if "env" not in kw:
                child = dict(os.environ)
                child["PATH"] = os.pathsep.join(
                    p for p in (child.get("PATH", ""), str(Path(hit).parent)) if p)
                kw["env"] = child
    return subprocess.run(args, **kw)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vco_lib.tool_search_dirs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "search-path",
        help="Print PATH with the directory of every container-runtime tool found "
             "only in the usual install locations appended (PATH unchanged when none).",
    )
    sub.add_parser("dirs", help="Print this OS's search directories, one per line.")
    a = p.parse_args(argv)
    if a.cmd == "dirs":
        for d in candidate_dirs():
            print(d)
        return 0
    print(reachable_path() or os.environ.get("PATH", ""))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return _cli(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
