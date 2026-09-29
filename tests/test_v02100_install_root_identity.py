# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-02 (AD-2): parity lock for the install-root IDENTITY rule.

The launcher's boot path resolves its orchestrator clone in Rust
(``launcher/src-tauri/vct-launcher-core/src/services/install_root.rs``:
``is_orchestrator_clone`` / ``resolve`` / ``exe_is_inside``) because no venv is
guaranteed at launcher boot, so ``python -m vco_lib`` (rule A) is not
available there. That makes the Rust rule a rule-C mirror, and this file is
its lock: both sides read ``tests/fixtures/install_root_cases.json``.

The rule — MUST match ``install_root.rs::is_orchestrator_clone``:
a directory is an orchestrator clone iff it carries the structural markers
(``vct-module.json``, OR ``install.py`` + ``CLAUDE.md``) AND its
``vct-module.json`` parses to an object whose ``id`` is exactly the string
``"orchestrator"``.

The Python statement of the rule composes only primitives that are already
the Python homes for the facts it reads: the manifest's module id is the key
``vco_lib`` itself uses for the orchestrator manifest (``vct-module.json``
``id``), and ``vco_lib.project_identity.normalise_for_match`` is asserted NOT
to be the comparison (the id match is exact, unlike class-name matching).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "install_root_cases.json"
RUST_SOURCE = (
    REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "services" / "install_root.rs"
)

ORCHESTRATOR_MODULE_ID = "orchestrator"  # must match install_root.rs ORCHESTRATOR_MODULE_ID
MAX_WALK_LEVELS = 8  # must match install_root.rs MAX_WALK_LEVELS


def _looks_like_orchestrator_root(d: Path) -> bool:
    # must match install_root.rs::looks_like_orchestrator_root
    return (d / "vct-module.json").is_file() or (
        (d / "install.py").is_file() and (d / "CLAUDE.md").is_file()
    )


def _manifest_id(d: Path) -> str | None:
    # must match install_root.rs::manifest_id
    try:
        data = json.loads((d / "vct-module.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mid = data.get("id")
    return mid if isinstance(mid, str) else None


def is_orchestrator_clone(d: Path) -> bool:
    # must match install_root.rs::is_orchestrator_clone
    return _looks_like_orchestrator_root(d) and _manifest_id(d) == ORCHESTRATOR_MODULE_ID


def _walk_from_exe(exe: Path) -> Path | None:
    # must match install_root.rs::walk_from_exe (nearest hit, bounded)
    current = exe.parent
    for _ in range(MAX_WALK_LEVELS):
        if is_orchestrator_clone(current):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _rel(base: Path, p: str) -> Path:
    return base.joinpath(*p.split("/"))


def _plant(base: Path, files: dict) -> None:
    for rel, content in files.items():
        target = _rel(base, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if content == "@orchestrator":
            content = '{"id": "orchestrator", "version": "0.0.0"}'
        target.write_text(content, encoding="utf-8")


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


IDENTITY = _load()["identity"]
RESOLVE = _load()["resolve"]


def test_fixture_corpus_covers_the_required_shapes():
    names = " | ".join(r["name"] for r in RESOLVE)
    for shape in (
        "inside the clone",
        "outside the clone",
        "unrelated git tree",
        "missing markers",
        ".app",
        "bounded",
    ):
        assert shape in names, f"resolve corpus lost the {shape!r} row"
    contain = " | ".join(r["name"] for r in _load()["containment"])
    assert "verbatim" in contain and "UNC" in contain


@pytest.mark.parametrize("row", IDENTITY, ids=[r["name"] for r in IDENTITY])
def test_identity_rule_agrees_with_the_shared_table(row, tmp_path):
    _plant(tmp_path, row["files"])
    assert is_orchestrator_clone(_rel(tmp_path, row["dir"])) is row["expect"]


def _resolve(db_cached: Path | None, exe: Path) -> tuple[Path, str] | None:
    # must match install_root.rs::resolve — the cache is held to the SAME
    # identity rule as the walk (W1R-06), never to the structural markers alone.
    if db_cached is not None and is_orchestrator_clone(db_cached):
        return db_cached, "db_cache"
    walked = _walk_from_exe(exe)
    return (walked, "exe_walk") if walked is not None else None


@pytest.mark.parametrize("row", RESOLVE, ids=[r["name"] for r in RESOLVE])
def test_resolve_agrees_with_the_shared_table(row, tmp_path):
    _plant(tmp_path, row["files"])
    exe = _rel(tmp_path, row["exe"])
    cached = _rel(tmp_path, row["db_cached"]) if row["db_cached"] is not None else None
    got = _resolve(cached, exe)
    want = row["expect"]
    if want["kind"] == "not_found":
        assert got is None
    else:
        assert got == (_rel(tmp_path, want["root"]), want["source"])


def test_structural_only_cache_is_not_identity(tmp_path):
    """W1R-06: the rows that plant a structural-only (or foreign-id) cache must
    be exactly the rows where the old structural rule would have accepted a
    non-clone — so the corpus really exercises the gap."""
    hits = 0
    for i, row in enumerate(RESOLVE):
        if row["db_cached"] is None:
            continue
        base = tmp_path / str(i)
        _plant(base, row["files"])
        cached = _rel(base, row["db_cached"])
        if _looks_like_orchestrator_root(cached) and not is_orchestrator_clone(cached):
            hits += 1
            assert row["expect"].get("source") != "db_cache", row["name"]
    assert hits >= 3, "resolve corpus lost its structural-only / foreign-id cache rows"


def test_id_match_is_exact_not_normalised():
    """``normalise_for_match`` (the class-name matcher) would equate
    ``Orchestrator`` with ``orchestrator``; the identity rule must not."""
    from vco_lib.project_identity import normalise_for_match

    row = next(r for r in IDENTITY if "exact" in r["name"])
    assert row["expect"] is False
    mid = json.loads(row["files"]["clone/vct-module.json"])["id"]
    assert normalise_for_match(mid) == normalise_for_match(ORCHESTRATOR_MODULE_ID)


def test_shipped_manifest_carries_the_orchestrator_id():
    """The real clone must satisfy the rule — otherwise every shipped launcher
    would refuse its own clone on the exe walk."""
    assert is_orchestrator_clone(REPO)


def test_rust_constants_match():
    src = RUST_SOURCE.read_text(encoding="utf-8")
    assert f'pub const ORCHESTRATOR_MODULE_ID: &str = "{ORCHESTRATOR_MODULE_ID}";' in src
    assert f"pub const MAX_WALK_LEVELS: usize = {MAX_WALK_LEVELS};" in src
