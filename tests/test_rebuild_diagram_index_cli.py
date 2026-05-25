# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco rebuild-diagram-index`` (Phase 1.5.C acceptance).

Coverage (per plan §1.5.8 + the agent brief):

* Empty project → reports 0 / 0 / 0.
* 3 valid diagrams → reports 3 indexed.
* Pre-existing sidecars + matching content → re-run is idempotent
  (0 Weaviate writes, 0 sidecar writes; ``skipped == total``).
* Mutate one file → re-run reports 1 reindexed, 2 skipped.
* Delete one file → orphan flagged; with ``--prune`` orphan removed.
* ``--all`` iterates registered projects.
* ``--dry-run`` writes nothing.

The CLI talks to two upstream APIs:

* ``vco_lib.diagram_indexer.index_diagram`` — Phase 1.5.A. We use the
  shipped STUB (real, file-backed) so the idempotency assertion is
  end-to-end real (sidecar files actually exist and are byte-compared).
* ``vco_lib.config_projection.{resolve_project_folder,
  list_registered_projects}`` — Phase 0.B. Not on disk yet; the CLI
  imports them via lazy wrappers (``_resolve_project_folder`` and
  ``_list_registered_projects`` on the rebuild module). Tests monkey-
  patch the wrappers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.cli import rebuild_diagram_index as rdi  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_folder(tmp_path: Path) -> Path:
    """A bare project folder with ``.claude/diagrams/`` ready to populate."""
    diagrams = tmp_path / ".claude" / "diagrams"
    diagrams.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def stubbed_resolver(monkeypatch, project_folder: Path):
    """Map a fixed project_id → the fixture folder. Returns the id."""
    def _resolve(pid: str) -> Path:
        if pid == "demo-project":
            return project_folder
        raise LookupError(f"unknown project: {pid}")
    monkeypatch.setattr(rdi, "_resolve_project_folder", _resolve)
    return "demo-project"


def _args(
    project_id: str | None = None,
    *,
    json_mode: bool = False,
    prune: bool = False,
    all_: bool = False,
    dry_run: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        project_id=project_id,
        json=json_mode,
        prune=prune,
        all=all_,
        dry_run=dry_run,
    )


def _write_mermaid(path: Path, body: str = "flowchart TD\n  A --> B\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _read_payload_from_stdout(capsys) -> dict:
    raw = capsys.readouterr().out.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests — base walks
# ---------------------------------------------------------------------------


def test_empty_project_reports_zero(stubbed_resolver, capsys):
    code = rdi.cmd_rebuild_diagram_index(
        _args(stubbed_resolver, json_mode=True)
    )
    assert code == rdi.EXIT_OK
    payload = _read_payload_from_stdout(capsys)
    assert payload["total"] == 0
    assert payload["indexed"] == 0
    assert payload["skipped"] == 0
    assert payload["failed"] == 0
    assert payload["weaviate_writes"] == 0
    assert payload["orphans"] == []
    assert payload["overall"] == "ok"


def test_three_diagrams_all_indexed(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "auth" / "login.mmd")
    _write_mermaid(base / "gui" / "auth" / "register.mmd")
    _write_mermaid(base / "architecture" / "data-flow.mmd")

    code = rdi.cmd_rebuild_diagram_index(
        _args(stubbed_resolver, json_mode=True)
    )
    assert code == rdi.EXIT_OK
    payload = _read_payload_from_stdout(capsys)
    assert payload["total"] == 3
    assert payload["indexed"] == 3
    assert payload["skipped"] == 0
    assert payload["failed"] == 0
    # STUB never writes to Weaviate.
    assert payload["weaviate_writes"] == 0
    # Sidecars exist.
    for name in ("login.mmd", "register.mmd"):
        side = base / "gui" / "auth" / f"{name}.meta.json"
        assert side.exists()


# ---------------------------------------------------------------------------
# Tests — idempotency
# ---------------------------------------------------------------------------


def test_idempotent_rerun_writes_nothing(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "login.mmd")
    _write_mermaid(base / "gui" / "register.mmd")

    # First run — populates sidecars.
    rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    capsys.readouterr()  # discard

    # Snapshot sidecar contents + mtime.
    sidecars = list(base.rglob("*.meta.json"))
    snapshots = {
        s: (s.read_text(encoding="utf-8"), s.stat().st_mtime_ns)
        for s in sidecars
    }
    assert len(snapshots) == 2

    # Second run — should be a no-op.
    code = rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert payload["total"] == 2
    assert payload["indexed"] == 0
    assert payload["skipped"] == 2
    assert payload["weaviate_writes"] == 0

    # Sidecars unchanged byte-for-byte AND mtime preserved (no rewrite).
    for s, (text, mtime) in snapshots.items():
        assert s.read_text(encoding="utf-8") == text
        assert s.stat().st_mtime_ns == mtime


def test_mutated_file_triggers_single_reindex(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "a.mmd", "flowchart TD\n  A --> B\n")
    _write_mermaid(base / "gui" / "b.mmd", "flowchart TD\n  X --> Y\n")
    _write_mermaid(base / "gui" / "c.mmd", "flowchart TD\n  P --> Q\n")

    rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    capsys.readouterr()

    # Mutate ONE file.
    _write_mermaid(base / "gui" / "b.mmd", "flowchart TD\n  X --> Z\n")

    code = rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert payload["total"] == 3
    assert payload["indexed"] == 1
    assert payload["skipped"] == 2


# ---------------------------------------------------------------------------
# Tests — orphan handling
# ---------------------------------------------------------------------------


def test_deleted_file_flagged_as_orphan(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "deleted.mmd")
    _write_mermaid(base / "gui" / "kept.mmd")

    rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    capsys.readouterr()

    # Delete one diagram (sidecar remains).
    (base / "gui" / "deleted.mmd").unlink()

    code = rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert payload["total"] == 1
    assert len(payload["orphans"]) == 1
    assert payload["orphans"][0].endswith("deleted.mmd.meta.json")
    assert payload["orphans_pruned"] == 0
    # Orphan sidecar still on disk (no prune).
    assert (base / "gui" / "deleted.mmd.meta.json").exists()


def test_prune_removes_orphan_sidecar(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "deleted.mmd")
    _write_mermaid(base / "gui" / "kept.mmd")

    rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    capsys.readouterr()
    (base / "gui" / "deleted.mmd").unlink()

    code = rdi.cmd_rebuild_diagram_index(
        _args(stubbed_resolver, json_mode=True, prune=True)
    )
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert len(payload["orphans"]) == 1
    assert payload["orphans_pruned"] == 1
    # Sidecar gone now.
    assert not (base / "gui" / "deleted.mmd.meta.json").exists()
    # Kept diagram's sidecar still here.
    assert (base / "gui" / "kept.mmd.meta.json").exists()


# ---------------------------------------------------------------------------
# Tests — --all + --dry-run
# ---------------------------------------------------------------------------


def test_all_iterates_projects(monkeypatch, tmp_path, capsys):
    """``--all`` flows through the real config_projection helpers when a
    seeded launcher DB is in place.

    This exercises the Phase 0.B Part 2 path end-to-end: the test stubs
    the launcher DB (not the helper), so ``list_registered_projects``
    and ``resolve_project_folder`` run for real. Regression guard for
    code-review B1: prior to Part 2, ``--all`` died with
    ``RuntimeError("Phase 0.B Part 2 not merged")``.
    """
    # Two projects, each with one diagram.
    p1_folder = tmp_path / "p1"
    p2_folder = tmp_path / "p2"
    _write_mermaid(p1_folder / ".claude" / "diagrams" / "gui" / "a.mmd")
    _write_mermaid(p2_folder / ".claude" / "diagrams" / "arch" / "b.mmd")

    # Seed a real launcher DB with the schema config_projection reads.
    import sqlite3
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "folder_path TEXT NOT NULL, slug TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?,?,?,?)",
        ("p1", "AlphaProj", str(p1_folder), "p1"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?,?,?,?)",
        ("p2", "BravoProj", str(p2_folder), "p2"),
    )
    conn.commit()
    conn.close()

    # Pin the launcher-DB resolver inside config_projection so both
    # helpers transparently read our tmp DB.
    from vco_lib import config_projection as cp
    monkeypatch.setattr(cp, "_resolve_launcher_db_path", lambda: db)

    code = rdi.cmd_rebuild_diagram_index(_args(all_=True, json_mode=True))
    assert code == rdi.EXIT_OK
    payload = _read_payload_from_stdout(capsys)
    assert payload["exit_code"] == rdi.EXIT_OK
    assert len(payload["projects"]) == 2
    titles = {p["project_id"] for p in payload["projects"]}
    assert titles == {"p1", "p2"}
    for p in payload["projects"]:
        assert p["total"] == 1
        assert p["indexed"] == 1


def test_all_iterates_projects_deterministic_order(monkeypatch, tmp_path, capsys):
    """``--all`` iterates projects in name-sorted order — deterministic
    across runs so CI diffs and progress UX stay stable."""
    folders = {}
    for slug in ("zulu", "alpha", "mike"):
        f = tmp_path / slug
        # Diagrams require a category subdirectory (see the
        # diagram_paths flat-folder rejection rule).
        _write_mermaid(f / ".claude" / "diagrams" / "gui" / "x.mmd")
        folders[slug] = f

    import sqlite3
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "folder_path TEXT NOT NULL, slug TEXT NOT NULL)"
    )
    # Insert in NON-alphabetical order; helper must sort by name.
    for slug, name in (("zulu", "Zulu"), ("alpha", "Alpha"), ("mike", "Mike")):
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, slug) VALUES (?,?,?,?)",
            (slug, name, str(folders[slug]), slug),
        )
    conn.commit()
    conn.close()

    from vco_lib import config_projection as cp
    monkeypatch.setattr(cp, "_resolve_launcher_db_path", lambda: db)

    code = rdi.cmd_rebuild_diagram_index(_args(all_=True, json_mode=True))
    assert code == rdi.EXIT_OK
    payload = _read_payload_from_stdout(capsys)
    order = [p["project_id"] for p in payload["projects"]]
    assert order == ["alpha", "mike", "zulu"]


def test_dry_run_writes_nothing(stubbed_resolver, project_folder, capsys):
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "a.mmd")
    _write_mermaid(base / "gui" / "b.mmd")

    code = rdi.cmd_rebuild_diagram_index(
        _args(stubbed_resolver, json_mode=True, dry_run=True)
    )
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert payload["total"] == 2
    # Both reported as "indexed" because no sidecars exist yet (dry-run
    # would write 2).
    assert payload["indexed"] == 2
    assert payload["skipped"] == 0
    assert payload["dry_run"] is True
    # No sidecars on disk.
    assert list(base.rglob("*.meta.json")) == []


def test_dry_run_after_real_run_is_clean(stubbed_resolver, project_folder, capsys):
    """After a real run, dry-run reports skipped (idempotent)."""
    base = project_folder / ".claude" / "diagrams"
    _write_mermaid(base / "gui" / "a.mmd")

    rdi.cmd_rebuild_diagram_index(_args(stubbed_resolver, json_mode=True))
    capsys.readouterr()

    code = rdi.cmd_rebuild_diagram_index(
        _args(stubbed_resolver, json_mode=True, dry_run=True)
    )
    payload = _read_payload_from_stdout(capsys)
    assert code == rdi.EXIT_OK
    assert payload["indexed"] == 0
    assert payload["skipped"] == 1


# ---------------------------------------------------------------------------
# Tests — usage errors
# ---------------------------------------------------------------------------


def test_missing_project_id_exits_two(monkeypatch, capsys):
    code = rdi.cmd_rebuild_diagram_index(_args(json_mode=True))
    assert code == rdi.EXIT_ENV_PROBLEM
    payload = _read_payload_from_stdout(capsys)
    assert payload["overall"] == "usage_error"


def test_all_with_positional_is_usage_error(monkeypatch, capsys):
    code = rdi.cmd_rebuild_diagram_index(
        _args("demo", json_mode=True, all_=True)
    )
    assert code == rdi.EXIT_ENV_PROBLEM
    payload = _read_payload_from_stdout(capsys)
    assert payload["overall"] == "usage_error"


def test_project_not_found_exits_two(monkeypatch, capsys):
    def _resolve(pid: str) -> Path:
        raise LookupError(f"no such project: {pid}")
    monkeypatch.setattr(rdi, "_resolve_project_folder", _resolve)

    code = rdi.cmd_rebuild_diagram_index(
        _args("ghost-project", json_mode=True)
    )
    assert code == rdi.EXIT_ENV_PROBLEM
    payload = _read_payload_from_stdout(capsys)
    assert payload["overall"] == "project_not_found"
