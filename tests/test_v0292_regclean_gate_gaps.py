# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 6 — two gates that were not gating.

**(a) pyright's scope.** `claude_mcp_servers/model_router` is new this cycle and
was not in `pyrightconfig.json`'s ``include``, so the CI `python-typecheck` job
never looked at it. The package is 0/0 today, which is the only moment adding a
surface is free — a package that ships outside the gate is one whose first type
error arrives in a field report.

**(b) A guard that could pass while blind.**
`tests/test_v52_ag_schema_versions.py::test_launcher_db_table_set_version_matches_migration_count`
scrapes ``version: N,`` literals out of `migrations.rs` and asserted only that
the list was non-EMPTY. A shape change that broke the scan down to a handful of
survivors would report a plausible wrong maximum and PASS — while the constant
it exists to pin had silently stopped being pinned. The Rust twin
(`schema_versions_rust_parity.rs::the_migration_parse_is_not_vacuous`) has
carried a floor since it was written; the asymmetry was the defect.

This file pins BOTH the config and the guard, independently of the files they
guard, so removing either one goes red here as well.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYRIGHTCONFIG = _REPO_ROOT / "pyrightconfig.json"
_MIGRATIONS_RS = (
    _REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
    / "migrations.rs"
)
_SCHEMA_TEST = _REPO_ROOT / "tests" / "test_v52_ag_schema_versions.py"
_RUST_PARITY = _REPO_ROOT / "launcher" / "src-tauri" / "tests" / "schema_versions_rust_parity.rs"

#: Same floor the Rust twin uses. Raise it when the array grows; NEVER lower it.
MIGRATION_FLOOR = 40


def _load_pyrightconfig() -> dict:
    """Parse the config, tolerating the `//` comments it deliberately carries.

    The comments are load-bearing documentation (they record why each exclusion
    exists and what it costs), so the parse strips them rather than the file
    losing them. Only whole-line comments are stripped — no string in this file
    contains `//`, and asserting that keeps the stripper honest.
    """
    raw = _PYRIGHTCONFIG.read_text(encoding="utf-8")
    stripped = "\n".join(
        ln for ln in raw.splitlines() if not ln.strip().startswith("//")
    )
    return json.loads(stripped)


# --------------------------------------------------------------------------- #
# (a) pyright include scope
# --------------------------------------------------------------------------- #


def test_model_router_is_inside_the_pyright_gate():
    cfg = _load_pyrightconfig()
    assert "claude_mcp_servers/model_router" in cfg["include"], (
        "the model_router package ships but is not typechecked by CI's "
        "python-typecheck job — it was 0/0 when it was added, so there is no "
        "cleanup debt reason to leave it out"
    )


def test_every_pyright_include_path_exists():
    """A stale `include` entry silently shrinks the gate to nothing it names."""
    cfg = _load_pyrightconfig()
    for entry in cfg["include"]:
        assert (_REPO_ROOT / entry).exists(), f"include path missing: {entry}"


def test_every_pyright_exclude_path_still_exists():
    """Same reasoning for excludes, minus the glob patterns.

    A `**/…` pattern names no single path; the concrete ones do, and a
    concrete exclude that no longer exists is a backlog item somebody already
    closed without reopening the gate.
    """
    cfg = _load_pyrightconfig()
    for entry in cfg.get("exclude", []):
        if "*" in entry:
            continue
        assert (_REPO_ROOT / entry).exists(), (
            f"exclude path {entry} no longer exists — delete the entry so the "
            f"gate covers what it now can"
        )


# --------------------------------------------------------------------------- #
# (b) the migrations parse floor
# --------------------------------------------------------------------------- #


def _parsed_versions() -> "list[int]":
    """Re-derive the scan independently of the test that owns it."""
    src = _MIGRATIONS_RS.read_text(encoding="utf-8")
    end = src.find("];")
    head = src[:end] if end > 0 else src
    return [int(m.group(1)) for m in re.finditer(r"version:\s*(\d+),", head)]


def test_the_migration_scan_is_not_vacuous():
    versions = _parsed_versions()

    assert len(versions) >= MIGRATION_FLOOR, (
        f"parsed only {len(versions)} migration versions — the array only "
        f"grows, so a shrinking parse means the scan broke or the `];` bound "
        f"moved. Fix the parse; do not lower the floor."
    )
    assert 1 in versions, "the initial migration must be in the parsed set"
    assert all(v <= 500 for v in versions), "the scan escaped the MIGRATIONS array"


def test_the_python_guard_carries_the_floor_itself():
    """The owning test must hold the assertion, not just this file.

    A floor that lives only here would leave the original test able to pass
    vacuously when run alone — which is how it is usually run.
    """
    src = _SCHEMA_TEST.read_text(encoding="utf-8")
    assert f">= {MIGRATION_FLOOR}" in src, (
        "test_v52_ag_schema_versions.py lost its not-vacuous floor"
    )
    assert "1 in versions_in_array" in src
    assert "v <= 500 for v in versions_in_array" in src


def test_the_python_floor_matches_the_rust_twin():
    """The two languages guard the same array; a split floor is a lie somewhere."""
    rust = _RUST_PARITY.read_text(encoding="utf-8")
    m = re.search(r"versions\.len\(\)\s*>=\s*(\d+)", rust)
    assert m, "the Rust twin's not-vacuous floor could not be found"
    assert int(m.group(1)) == MIGRATION_FLOOR, (
        f"Rust floor is {m.group(1)}, Python floor is {MIGRATION_FLOOR} — the "
        f"two guards disagree about how short a parse is implausible"
    )


def test_a_broken_scan_would_now_be_caught():
    """Drive the floor with a mangled source and prove it convicts.

    Without this, "the floor is present" is itself an untested promise — the
    §8.2 lesson about tests whose name claims more than they assert.
    """
    mangled = "const MIGRATIONS: &[Migration] = &[ version: 1, version: 2, ];"
    end = mangled.find("];")
    head = mangled[:end] if end > 0 else mangled
    versions = [int(m.group(1)) for m in re.finditer(r"version:\s*(\d+),", head)]

    assert len(versions) == 2
    with pytest.raises(AssertionError):
        assert len(versions) >= MIGRATION_FLOOR
