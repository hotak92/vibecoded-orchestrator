# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Phase 0.B Part 2 (2026-05-25) — Rust-side subprocess invocation
contract for the canonical-env Python writer.

The Rust production callers (`create_project_v2`, `rename_project_v2`,
`set_shared_kg_write_disabled`, `refresh_project_env_with_db`) all
delegate to a private helper `apply_project_env_via_python` in
`launcher/src-tauri/src/commands/projects_v2.rs` that spawns:

    <python> -m vco_lib.config_projection apply --project-id <id>

This test pins the BEHAVIORAL contract of that subprocess from the
Python side:

  * The CLI's `apply` verb exists and accepts `--project-id`.
  * Invoking it as a real subprocess (with a real launcher.db fixture
    on disk) produces byte-identical surface output to a direct
    in-process call to `apply_project_env(project_env_from_db(...))`.
  * The CLI's exit codes match the documented contract (0 = success,
    2 = project not found, 3 = db unreachable, 4 = apply failed).
  * Stderr carries a JSON-shaped diagnostic on error (so the Rust side
    can surface a meaningful warning toast to the user).
  * The CLI respects `$VCT_STATE_DIR` for DB resolution (the canonical
    env-var channel for launcher state isolation).

We do NOT need to invoke cargo-built Rust here — the Rust side's
contract surface is purely "spawn `python -m vco_lib.config_projection
apply --project-id <id>` with these env vars and parse exit code +
stderr". As long as the Python CLI satisfies the contract, the Rust
caller works. Re-pinning the Rust spawn argv shape lives in
`launcher/src-tauri/src/commands/projects_v2.rs::tests` (where it can
be exercised against the real `apply_project_env_via_python` Rust
function).

Run: pytest tests/test_config_projection_subprocess.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def launcher_db_with_project(tmp_path: Path) -> tuple[Path, str, Path]:
    """Build a minimal launcher.db with one project row and KG bindings.

    Returns:
        (db_path, project_id, project_folder)
    """
    project_folder = tmp_path / "MyProject"
    project_folder.mkdir()

    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            slug TEXT
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT,
            role TEXT,
            collection_name TEXT
        );
        CREATE TABLE kg_collection_access (
            project_id TEXT,
            collection_name TEXT,
            access_level TEXT
        );
        CREATE TABLE codegraph_access (
            grantee_project_id TEXT,
            grantor_project_id TEXT,
            access_level TEXT
        );
        CREATE TABLE module_settings (
            project_id TEXT,
            module_id TEXT,
            setting_key TEXT,
            setting_value TEXT
        );
        """
    )
    project_id = "proj-subproc-1"
    conn.execute(
        "INSERT INTO projects VALUES (?, ?, ?, ?)",
        (project_id, "MyProject", str(project_folder), "myproject"),
    )
    conn.execute(
        "INSERT INTO project_kg_bindings VALUES (?, ?, ?)",
        (project_id, "primary", "MyProject_KnowledgeGraph"),
    )
    conn.execute(
        "INSERT INTO project_kg_bindings VALUES (?, ?, ?)",
        (project_id, "archive", "MyProject_Development"),
    )
    conn.execute(
        "INSERT INTO project_kg_bindings VALUES (?, ?, ?)",
        (project_id, "shared", "VibeCodedOrchestrator_KnowledgeGraph"),
    )
    conn.commit()
    conn.close()
    return db_path, project_id, project_folder


# ─── Helpers ────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_apply_cli(
    project_id: str,
    db_path: Path,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Spawn `python -m vco_lib.config_projection apply --project-id <id>`
    with the same env-var shape the Rust caller uses.

    Mirrors `apply_project_env_via_python`'s spawn shape:
      * `python -m vco_lib.config_projection apply --project-id <id>`
      * Minimal env: PATH + VCT_STATE_DIR (+ HOME on POSIX)
      * cwd = project_folder when supplied
      * 30 s timeout
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VCT_STATE_DIR": str(db_path.parent),
        # PYTHONPATH so the test environment can find vco_lib without
        # needing it pip-installed. The Rust path relies on the
        # interpreter's venv having vco_lib reachable via the
        # orchestrator clone's directory structure.
        "PYTHONPATH": str(REPO_ROOT),
    }
    # Pass HOME so the launcher DB fallback (~/.vct/launcher.db) doesn't
    # resolve to a system path the test can't write to.
    if sys.platform != "win32" and "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vco_lib.config_projection",
            "apply",
            "--project-id",
            project_id,
        ],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ─── Tests ──────────────────────────────────────────────────────────────


def test_subprocess_apply_writes_canonical_env(
    launcher_db_with_project: tuple[Path, str, Path],
) -> None:
    """Happy path: CLI spawns successfully, exits 0, writes both surfaces.

    Pins the Rust-side contract: `subprocess.run` of the CLI with the
    standard env shape lands canonical env on disk at the same paths
    the in-process Python writer would.
    """
    db_path, project_id, folder = launcher_db_with_project
    result = _run_apply_cli(project_id, db_path, cwd=folder)
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}; stdout={result.stdout!r}; "
        f"stderr={result.stderr!r}"
    )

    # Surfaces created.
    claude_dir = folder / ".claude"
    assert (claude_dir / "settings.json").is_file()
    assert (claude_dir / "env").is_file()

    # Canonical env content is present in both surfaces.
    settings = json.loads((claude_dir / "settings.json").read_text())
    env_block = settings.get("env", {})
    assert env_block.get("KG_COLLECTION") == "MyProject_KnowledgeGraph"
    assert env_block.get("DEVELOPMENT_COLLECTION") == "MyProject_Development"
    assert env_block.get("PROJECT_NAME") == "MyProject"
    assert env_block.get("CODE_GRAPH_PROJECT") == "MyProject"

    shell_env_text = (claude_dir / "env").read_text()
    assert "# vco-managed-begin" in shell_env_text
    assert "# vco-managed-end" in shell_env_text
    assert 'export KG_COLLECTION="MyProject_KnowledgeGraph"' in shell_env_text


def test_subprocess_apply_exit_code_project_not_found(
    launcher_db_with_project: tuple[Path, str, Path],
) -> None:
    """Pins the contract: project not in DB → exit 2 + JSON stderr.

    The Rust caller surfaces stderr to the warning toast, so the
    diagnostic must be a single readable line.
    """
    db_path, _project_id, folder = launcher_db_with_project
    result = _run_apply_cli("nonexistent-project-id", db_path, cwd=folder)
    assert result.returncode == 2
    # stderr is JSON-shaped per the CLI contract.
    diag = json.loads(result.stderr.strip())
    assert diag["error"] == "project_not_found"
    assert "nonexistent-project-id" in diag["message"]


def test_subprocess_apply_exit_code_db_unreachable(tmp_path: Path) -> None:
    """Pins the contract: DB doesn't exist → exit 3 + JSON stderr.

    Simulates the Rust caller running before the launcher has booted
    (or with a corrupt VCT_STATE_DIR pointing at a non-existent path).
    """
    # Point VCT_STATE_DIR at a directory with no launcher.db.
    empty_dir = tmp_path / "empty_state"
    empty_dir.mkdir()
    folder = tmp_path / "MyProject"
    folder.mkdir()

    env = {
        "PATH": os.environ.get("PATH", ""),
        "VCT_STATE_DIR": str(empty_dir),
        "PYTHONPATH": str(REPO_ROOT),
    }
    if sys.platform != "win32" and "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    result = subprocess.run(
        [sys.executable, "-m", "vco_lib.config_projection", "apply",
         "--project-id", "any-id"],
        env=env, cwd=str(folder), capture_output=True, text=True, timeout=30.0,
    )
    assert result.returncode == 3
    diag = json.loads(result.stderr.strip())
    assert diag["error"] == "db_unreachable"


def test_subprocess_apply_respects_vct_state_dir(
    launcher_db_with_project: tuple[Path, str, Path],
) -> None:
    """Pins the env-var contract: VCT_STATE_DIR is the canonical channel
    the Rust caller uses to point Python at the launcher DB.

    This test verifies that WITHOUT VCT_STATE_DIR (and without a real
    `~/.vct/launcher.db`), the CLI fails with db_unreachable — proving
    the env-var IS being read and DOES change behaviour. The Rust
    caller's `cmd.env("VCT_STATE_DIR", ...)` line is the only thing
    keeping production from blowing up on missing-DB.
    """
    db_path, project_id, folder = launcher_db_with_project

    # First: WITH VCT_STATE_DIR pointing at our fixture DB — succeeds.
    ok = _run_apply_cli(project_id, db_path, cwd=folder)
    assert ok.returncode == 0

    # Second: WITHOUT VCT_STATE_DIR, also point HOME away from any real
    # ~/.vct/launcher.db so the fallback can't find a DB. The CLI must
    # fail with db_unreachable.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "HOME": str(folder),  # not a real HOME; ~/.vct/launcher.db won't exist
    }
    result = subprocess.run(
        [sys.executable, "-m", "vco_lib.config_projection", "apply",
         "--project-id", project_id],
        env=env, cwd=str(folder), capture_output=True, text=True, timeout=30.0,
    )
    assert result.returncode == 3, (
        f"Expected db_unreachable without VCT_STATE_DIR; got "
        f"returncode={result.returncode}; stderr={result.stderr!r}"
    )


def test_subprocess_apply_matches_in_process_semantically(
    launcher_db_with_project: tuple[Path, str, Path],
) -> None:
    """The CLI subprocess + a direct in-process call to
    ``apply_project_env(project_env_from_db(...))`` MUST produce
    semantically-identical output (same JSON env block content, same
    shell exports). The Rust → Python subprocess hop is transparent
    iff this invariant holds.

    NOTE on byte-vs-semantic parity: the JSON env block's KEY ORDER
    is non-deterministic across runs because the contract's
    `_write_json_env_block` iterates `list_canonical_keys()` (a set).
    For Phase 0.B Part 2 this is acceptable: every consumer is a JSON
    parser, not a byte comparator. A future tightening could sort keys
    or pin insertion order; tracked as a hygiene follow-up.
    """
    from vco_lib.config_projection import (
        apply_project_env,
        project_env_from_db,
    )

    db_path, project_id, folder = launcher_db_with_project

    # Path A: subprocess.
    sub_result = _run_apply_cli(project_id, db_path, cwd=folder)
    assert sub_result.returncode == 0

    settings_subproc = json.loads(
        (folder / ".claude" / "settings.json").read_text()
    )
    env_subproc_text = (folder / ".claude" / "env").read_text()

    # Wipe + path B: in-process.
    (folder / ".claude" / "settings.json").unlink()
    (folder / ".claude" / "env").unlink()
    bundle = project_env_from_db(project_id, db_path=db_path)
    apply_project_env(bundle)
    settings_inproc = json.loads(
        (folder / ".claude" / "settings.json").read_text()
    )
    env_inproc_text = (folder / ".claude" / "env").read_text()

    # JSON parity: same canonical env keys + values, regardless of key
    # order in the serialized form.
    assert settings_subproc == settings_inproc, (
        "subprocess and in-process produced different "
        ".claude/settings.json contents (parsed)"
    )

    # Shell-env parity: same exports, possibly in different line order
    # (the canonical key set iteration order is non-deterministic).
    def _exports(text: str) -> set[str]:
        return {
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("export ")
        }

    assert _exports(env_subproc_text) == _exports(env_inproc_text), (
        "subprocess and in-process emitted different .claude/env "
        "exports"
    )
    # Both files MUST carry the managed-block markers regardless of
    # the inner ordering.
    for block in (env_subproc_text, env_inproc_text):
        assert "# vco-managed-begin" in block
        assert "# vco-managed-end" in block


def test_subprocess_apply_uses_minimal_env_no_leak(
    launcher_db_with_project: tuple[Path, str, Path],
) -> None:
    """Pins the Rust-side env_clear() discipline: the subprocess must
    NOT inherit launcher-process env vars that would corrupt resolution.

    Specifically: if the Rust caller leaks its own KG_COLLECTION into
    the subprocess env, the Python contract MIGHT override the DB-
    resolved value (env-as-config). The contract today reads ONLY from
    launcher.db (no env-var consults for canonical values), so the
    leak is harmless TODAY, but the env_clear() discipline guards
    against future env-var consults sneaking in. This test pins the
    discipline by passing a hostile KG_COLLECTION env var and asserting
    the on-disk value comes from the DB, not the env var.
    """
    db_path, project_id, folder = launcher_db_with_project

    # Hostile: pass a KG_COLLECTION env var that DIFFERS from the DB row.
    result = _run_apply_cli(
        project_id, db_path, cwd=folder,
        extra_env={"KG_COLLECTION": "HOSTILE_KG_FROM_ENV"},
    )
    assert result.returncode == 0

    settings = json.loads((folder / ".claude" / "settings.json").read_text())
    env_block = settings.get("env", {})
    assert env_block.get("KG_COLLECTION") == "MyProject_KnowledgeGraph", (
        f"DB-resolved KG_COLLECTION was overridden by env-var leakage: "
        f"got {env_block.get('KG_COLLECTION')!r}, expected "
        f"'MyProject_KnowledgeGraph'"
    )
