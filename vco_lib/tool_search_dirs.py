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
expansion AND the order rule on both sides). Each entry carries a
``placement`` (v0.2.97 R10): ``prepend-when-missing`` for the v0.2.53
graphical-launch list (Homebrew, cargo, ~/.local/bin, ... — the order a login
shell builds), ``append`` for the v0.2.97 runtime locations. Both apply only to
a directory the PATH lacks. The looked-up PATH is therefore::

    [prepend entries not on PATH] + PATH + [append entries not on PATH]

— exactly the PATH the Rust augment sets (``runtime.rs::augmented_path``), so a
name resolves to the same binary on every surface. This module is the Python
reader:

* :func:`search_entries` / :func:`candidate_dirs` — the table's entries for an
  OS, expanded (with / without their placement);
* :func:`lookup_entries` — the order rule, pure;
* :func:`which` — ``name`` looked up along that order;
* :func:`run` — :func:`subprocess.run` that spawns what :func:`which` finds: a
  bare program name found in a table directory missing from PATH is run by its
  absolute path, with that directory placed on the child's PATH per its
  placement (a compose provider next to it);
* :func:`reachable_path` — the PATH a shell needs so the runtime tools resolve
  by name to what :func:`which` finds (the boot wrappers and the session hooks
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
    "PLACEMENT_APPEND",
    "PLACEMENT_PREPEND",
    "RUNTIME_TOOLS",
    "TABLE_PATH",
    "candidate_dirs",
    "expand_entry",
    "lookup_entries",
    "os_key",
    "path_separator",
    "reachable_path",
    "run",
    "search_entries",
    "which",
    "main",
]

TABLE_PATH = Path(__file__).with_name("tool_search_dirs.toml")
SUPPORTED_FORMAT_VERSION = 2

#: Set (even to ``""``) → REPLACES the table's list for this OS; its entries
#: are placed :data:`PLACEMENT_APPEND`. Must match
#: ``runtime.rs::TOOL_SEARCH_DIRS_ENV``.
ENV_OVERRIDE = "VCT_TOOL_SEARCH_DIRS"

#: Ahead of the caller's PATH, when missing from it. Must match
#: ``runtime.rs::Placement::PrependWhenMissing``.
PLACEMENT_PREPEND = "prepend-when-missing"
#: After the caller's PATH, when missing from it. Must match
#: ``runtime.rs::Placement::Append``.
PLACEMENT_APPEND = "append"
_PLACEMENTS = (PLACEMENT_PREPEND, PLACEMENT_APPEND)

#: The tools a shell must be able to reach by name to drive VCO's stack:
#: the two runtimes and their standalone compose front-ends.
RUNTIME_TOOLS: tuple[str, ...] = ("podman", "docker", "podman-compose", "docker-compose")

_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(.*)$", re.DOTALL)

Entry = tuple[str, str]  # (dir, placement)


def os_key(system: Optional[str] = None) -> str:
    """``linux`` / ``macos`` / ``windows`` (the table's keys), ``other`` else."""
    s = (system if system is not None else platform.system()).strip().lower()
    return {"linux": "linux", "darwin": "macos", "macos": "macos",
            "windows": "windows"}.get(s, "other")


def path_separator(os_name: str) -> str:
    return ";" if os_name == "windows" else ":"


@functools.lru_cache(maxsize=1)
def _table() -> dict[str, tuple[Entry, ...]]:
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
    out: dict[str, tuple[Entry, ...]] = {}
    for key, value in dirs.items():
        if not isinstance(value, list):
            raise ValueError(f"{TABLE_PATH}: [dirs].{key} must be a list of entries")
        entries: list[Entry] = []
        for item in value:
            if (not isinstance(item, dict) or set(item) != {"dir", "placement"}
                    or not isinstance(item["dir"], str)
                    or item["placement"] not in _PLACEMENTS):
                raise ValueError(
                    f"{TABLE_PATH}: [dirs].{key} entry {item!r} must be "
                    f"{{ dir = \"...\", placement = {' | '.join(map(repr, _PLACEMENTS))} }}"
                )
            entries.append((item["dir"], item["placement"]))
        out[key] = tuple(entries)
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


def search_entries(
    *,
    os_name: Optional[str] = None,
    home: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[Entry]:
    """The expanded ``(dir, placement)`` entries for ``os_name`` (default: this
    OS), in table order, duplicate directories dropped (the first keeps its
    placement). ``home`` defaults to ``env["HOME"]``; ``env`` to
    :data:`os.environ`. :data:`ENV_OVERRIDE` replaces the table's list, every
    entry :data:`PLACEMENT_APPEND`. MUST MATCH
    ``runtime.rs::tool_search_entries_for``."""
    source: Mapping[str, str] = os.environ if env is None else env
    name = os_name or os_key()
    if home is None:
        home = source.get("HOME") or None
    raw: list[Entry]
    if ENV_OVERRIDE in source:
        raw = [(p, PLACEMENT_APPEND)
               for p in source[ENV_OVERRIDE].split(path_separator(name)) if p.strip()]
    else:
        raw = list(_table().get(name, ()))
    out: list[Entry] = []
    seen: set[str] = set()
    for entry, placement in raw:
        expanded = expand_entry(entry.strip(), home=home, env=source)
        if expanded and expanded not in seen:
            seen.add(expanded)
            out.append((expanded, placement))
    return out


def candidate_dirs(
    *,
    os_name: Optional[str] = None,
    home: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The directories of :func:`search_entries`, in table order. MUST MATCH
    ``runtime.rs::tool_search_dirs_for``."""
    return [d for d, _p in search_entries(os_name=os_name, home=home, env=env)]


def lookup_entries(current: Sequence[str], entries: Sequence[Entry]) -> list[str]:
    """THE ORDER RULE, pure: the ``prepend-when-missing`` entries ``current``
    lacks (table order), then ``current`` unchanged, then the ``append``
    entries it lacks (table order). An entry already in ``current`` is never
    moved nor duplicated. MUST MATCH ``runtime.rs::augmented_entries``."""
    present = set(current)
    before: list[str] = []
    after: list[str] = []
    for d, placement in entries:
        if d in present:
            continue
        present.add(d)
        (before if placement == PLACEMENT_PREPEND else after).append(d)
    return before + list(current) + after


def _path_of(env: Optional[Mapping[str, str]]) -> str:
    source: Mapping[str, str] = os.environ if env is None else env
    return source.get("PATH", "")


def _split(path: str) -> list[str]:
    return [p for p in path.split(os.pathsep) if p]


def _first_hit(name: str, dirs: Sequence[str]) -> Optional[tuple[str, str]]:
    """``(directory, executable)`` for the first of ``dirs`` holding ``name``."""
    for d in dirs:
        found = shutil.which(name, path=d)
        if found:
            return d, found
    return None


def _placed(current: str, before: Sequence[str], after: Sequence[str]) -> str:
    """``current`` (kept byte-for-byte) with ``before`` ahead of it and
    ``after`` behind it."""
    return os.pathsep.join([*before, *([current] if current else []), *after])


def which(name: str, *, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``name`` along the order rule for ``env``'s PATH (default the
    process's): a ``prepend-when-missing`` table directory the PATH lacks,
    then the PATH, then an ``append`` directory it lacks. The absolute path,
    or ``None``.

    So a name the PATH resolves keeps resolving there unless a login shell
    would have put a graphical-launch directory (Homebrew, ...) ahead of it —
    the same binary the launcher and the hub run after their augment."""
    hit = _first_hit(name, lookup_entries(_split(_path_of(env)), search_entries(env=env)))
    return hit[1] if hit else None


def reachable_path(
    tools: Sequence[str] = RUNTIME_TOOLS, *, env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The PATH (``env``'s, default the process's) with the table directory of
    each of ``tools`` that :func:`which` finds OUTSIDE it placed per its
    placement — so each resolves by name to what :func:`which` found — or
    ``None`` when every tool is either on PATH already or nowhere.

    The inherited PATH is kept byte-for-byte; only directories it lacks are
    added (``append`` ones after it, ``prepend-when-missing`` ones ahead)."""
    current = _path_of(env)
    parts = _split(current)
    entries = search_entries(env=env)
    order = lookup_entries(parts, entries)
    needed: set[str] = set()
    for tool in tools:
        hit = _first_hit(tool, order)
        if hit and hit[0] not in parts:
            needed.add(hit[0])
    if not needed:
        return None
    placement = dict(entries)
    before = [d for d in order if d in needed and placement[d] == PLACEMENT_PREPEND]
    after = [d for d in order if d in needed and placement[d] != PLACEMENT_PREPEND]
    return _placed(current, before, after)


def _is_bare(program: str) -> bool:
    return not (os.sep in program or (os.altsep and os.altsep in program))


def run(argv: Sequence[str], **kw: Any) -> "subprocess.CompletedProcess[Any]":
    """:func:`subprocess.run` that spawns what :func:`which` finds: a bare
    ``argv[0]`` whose hit is in a table directory missing from PATH runs by
    its absolute path, and (unless the caller passed ``env``) that directory
    is placed on the child's PATH per its placement. Everything else is
    unchanged."""
    args = list(argv)
    if args and isinstance(args[0], str) and _is_bare(args[0]):
        current = _path_of(None)
        parts = _split(current)
        entries = search_entries()
        hit = _first_hit(args[0], lookup_entries(parts, entries))
        if hit and hit[0] not in parts:
            hit_dir, args[0] = hit
            if "env" not in kw:
                first = dict(entries)[hit_dir] == PLACEMENT_PREPEND
                child = dict(os.environ)
                child["PATH"] = _placed(current, [hit_dir] if first else [],
                                        [] if first else [hit_dir])
                kw["env"] = child
    return subprocess.run(args, **kw)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vco_lib.tool_search_dirs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "search-path",
        help="Print PATH with the directory of every container-runtime tool found "
             "outside it placed per the table (PATH unchanged when none).",
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
