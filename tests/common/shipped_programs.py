# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The programs VCO ships that can land on a user's PATH, and the command trees
of the two CLIs whose verbs shipped text names (v0.2.97).

ONE home for both, read by every test that needs to tell a program name from
something else: ``test_v0297_cli_program_names`` (no two programs share a
name; every ``vco <verb>`` / ``vct-cli <verb>`` is real) and
``test_v0297_r6_manifest_references`` (``vct-cli module list`` is a command,
not a module id).

* Python ``vco`` — the argparse parser ``vco_lib.cli.__main__._build_parser``
  builds;
* Rust ``vct-cli`` — ``launcher/tools/vct-cli/cli_verbs.json``, which the
  crate's unit test ``verb_table_matches_the_parser`` proves equal to clap's
  tree.

A program is every Cargo binary target, every ``pyproject.toml`` script and
every executable under ``tools/``; ``.sh`` / ``.ps1`` / ``.cmd`` siblings of
one tool are one program.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the orchestrator venv is 3.11+
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[2]
VERB_TABLE = REPO / "launcher" / "tools" / "vct-cli" / "cli_verbs.json"

SKIP_PARTS = {".git", "node_modules", "target", ".venv", "venv", "__pycache__", "dist"}
TOOL_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat")


# ─── the two command trees ──────────────────────────────────────────────


def python_vco_tree() -> dict[str, set[str]]:
    """``vco``'s verbs → their subverbs, from the real argparse parser."""
    from vco_lib.cli.__main__ import _build_parser

    def children(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return dict(action.choices)
        return {}

    return {
        verb: set(children(sub))
        for verb, sub in children(_build_parser()).items()
    }


def rust_cli_tree() -> tuple[str, dict[str, set[str]]]:
    data = json.loads(VERB_TABLE.read_text(encoding="utf-8"))
    return data["program"], {verb: set(subs) for verb, subs in data["verbs"].items()}


# ─── the programs that can land on PATH ─────────────────────────────────


def _walk(root: Path, name: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
        if name in filenames:
            yield Path(dirpath) / name


def cargo_programs(repo: Path = REPO) -> list[tuple[str, str]]:
    """``(command name, origin)`` for every Cargo binary target under ``repo``.

    Explicit ``[[bin]]`` targets, plus Cargo's auto-discovered ones: the
    package-named binary for an unclaimed ``src/main.rs`` and one per
    unclaimed ``src/bin/*.rs``.
    """
    out: list[tuple[str, str]] = []
    for manifest in _walk(repo, "Cargo.toml"):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        rel = manifest.relative_to(repo).as_posix()
        bins = data.get("bin", [])
        claimed = {str(Path(b["path"])) for b in bins if "path" in b}
        for b in bins:
            out.append((b["name"], f"{rel} [[bin]]"))
        package = data.get("package")
        if not package or package.get("autobins", True) is False:
            continue
        crate = manifest.parent
        if (crate / "src" / "main.rs").is_file() and str(Path("src/main.rs")) not in claimed:
            out.append((package["name"], f"{rel} src/main.rs"))
        bin_dir = crate / "src" / "bin"
        if bin_dir.is_dir():
            for rs in sorted(bin_dir.glob("*.rs")):
                if str(rs.relative_to(crate)) not in claimed:
                    out.append((rs.stem, f"{rel} src/bin/{rs.name}"))
    return out


def python_programs(repo: Path = REPO) -> list[tuple[str, str]]:
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    out = []
    for table in ("scripts", "gui-scripts"):
        for name in project.get(table, {}):
            out.append((name, f"pyproject.toml [project.{table}]"))
    return out


def tool_programs(repo: Path = REPO) -> list[tuple[str, str]]:
    """Executables under ``tools/`` (``lib/`` and ``tests/`` are not CLIs)."""
    out = []
    tools = repo / "tools"
    if not tools.is_dir():
        return out
    for path in sorted(tools.rglob("*")):
        parts = path.relative_to(tools).parts
        if not path.is_file() or {"lib", "tests"}.intersection(parts):
            continue
        if path.suffix in (".cmd", ".bat", ".ps1"):
            is_cli = True
        else:
            try:
                is_cli = path.read_bytes()[:2] == b"#!"
            except OSError:
                continue
        if not is_cli:
            continue
        name = path.name
        for suffix in TOOL_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        # siblings of one tool share an origin, so they are one program
        origin = (path.parent / name).relative_to(repo).as_posix()
        out.append((name, f"{origin}{{,{','.join(TOOL_SUFFIXES)}}}"))
    return out


def command_name_collisions(programs: list[tuple[str, str]]) -> dict[str, set[str]]:
    by_name: dict[str, set[str]] = {}
    for name, origin in programs:
        by_name.setdefault(name.lower(), set()).add(origin)
    return {name: origins for name, origins in by_name.items() if len(origins) > 1}


def all_programs() -> list[tuple[str, str]]:
    return cargo_programs() + python_programs() + tool_programs()


def program_names() -> set[str]:
    return {name for name, _ in all_programs()}


def command_trees() -> dict[str, dict[str, set[str]]]:
    """Program name → its verbs → their subverbs, for the programs whose
    parser a test can read."""
    program, rust = rust_cli_tree()
    return {"vco": python_vco_tree(), program: rust}
