# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""User-secret KEY contract (the value-writing arms are retired).

VCO never writes a secret value into the project tree. Phase 0.E
(2026-05-25) shipped ``apply-user-secrets``; v0.2.75 P3 cut it down to a
strip of every launcher-known key NAME, and v0.2.97 retired it: removing a
value by NAME destroys a key the user typed. It is SUPERSEDED by the
evidence-gated scrub inside every ``apply_project_env`` (and the launcher's
unregister, via ``strip-proven-secret-values``) — a value is removed only when
it equals the launcher's stored value; that behaviour is tested in
``tests/test_v0297_user_secret_scrub.py``.

This file tests:

  1. :func:`user_secret_known_keys_from_db` — the resolver of the names the
     refresh checks: the union of three buckets (per-project, shared,
     global) from ``secret_active_state``, deduped, sorted.
  2. The refresh's ``.claude/env`` block carries no user-secret section.
  3. The ``user-secret-known-keys`` CLI verb (names only).
  4. The grep-gate: the retired ``--pairs-json`` flag and the retired
     ``apply-user-secrets`` verb stay retired — no parser registration,
     no caller anywhere in the tree.

Run: pytest tests/test_config_projection_user_secrets.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import (
    connect,
    insert_rows,
    make_launcher_db,
)
from vco_lib.config_projection import (
    CLAUDE_ENV_MANAGED_END,
    DbUnreachable,
    apply_project_env,
    user_secret_known_keys_from_db,
)


# ─── DB fixture (secret_active_state schema) ────────────────────────────


def _secret_row(
    scope: str, project_id: str, key: str, *, active: int,
) -> dict[str, object]:
    """One ``secret_active_state`` row in the shape migration 009 backfills:
    shared/global rows carry the ``'*'`` any-requester sentinel, per_project
    rows carry the owning project id. (The pre-merge hand-rolled DDL gave the
    column ``DEFAULT '*'`` and let every row take the sentinel — a shape the
    launcher never writes for a per-project secret. The resolver under test
    reads only (scope, project_id, module_id, key), so the assertions are
    unaffected; the seed is now simply legal.)"""
    return {
        "scope": scope,
        "project_id": project_id,
        "module_id": "user",
        "key": key,
        "requester_project_id": "*" if scope in ("shared", "global") else project_id,
        "active": active,
        "updated_at": 0,
    }


def _make_launcher_db_with_secrets(
    db_path: Path,
    *,
    project_id: str = "proj-1",
    project_name: str = "Demo",
    project_folder: str = "/tmp/demo",
    project_slug: str = "demo",
    per_project_keys: list[str] | None = None,
    shared_keys: list[str] | None = None,
    global_keys: list[str] | None = None,
    inactive_keys: list[tuple[str, str, str]] | None = None,
    create_secret_table: bool = True,
) -> None:
    """Build a launcher.db (REAL schema) seeded for the secrets resolver.

    v0.2.92 §3.4: the schema is the shipped migration set applied verbatim
    by ``tests.common.launcher_db_fixture`` — migrations 007 + 009 own
    ``secret_active_state``, so this file no longer restates (and no longer
    has to keep in sync with) its column list.

    Args:
        per_project_keys: KEY names to insert at (scope='per_project',
            project_id=<project_id>, module_id='user'). Active=1.
        shared_keys: KEY names at (scope='shared', project_id=
            '_user_shared_', module_id='user'). Active=1.
        global_keys: KEY names at (scope='global', project_id=
            '_global_', module_id='user'). Active=1.
        inactive_keys: list of (scope, project_id, key) rows that
            should be inserted with active=0 — to verify the
            resolver INCLUDES them in the strip set regardless of
            active flag (mirroring the Rust ``list_*_user_secret_keys``
            family, which always returns every observed key).
        create_secret_table: if False, DROP secret_active_state after the
            migrations have run, modelling a pre-migration-007 launcher.db
            (test soft-fail on such DBs).
    """
    make_launcher_db(
        db_path,
        projects=[{
            "project_id": project_id,
            "name": project_name,
            "folder_path": project_folder,
            "slug": project_slug,
        }],
    )
    if not create_secret_table:
        # DELIBERATE degraded shape: a launcher.db that pre-dates migration
        # 007 has no secret_active_state at all. Built by dropping the real
        # table rather than by hand-rolling a partial schema, so every OTHER
        # table stays exactly what production has.
        conn = connect(db_path)
        try:
            conn.execute("DROP TABLE secret_active_state")
            conn.commit()
        finally:
            conn.close()
        return
    rows: list[dict[str, object]] = [
        _secret_row("per_project", project_id, key, active=1)
        for key in per_project_keys or []
    ]
    rows.extend(
        _secret_row("shared", "_user_shared_", key, active=1)
        for key in shared_keys or []
    )
    rows.extend(
        _secret_row("global", "_global_", key, active=1)
        for key in global_keys or []
    )
    rows.extend(
        _secret_row(scope, pid, key, active=0)
        for scope, pid, key in inactive_keys or []
    )
    if rows:
        insert_rows(db_path, "secret_active_state", rows)


# ─── user_secret_known_keys_from_db tests ───────────────────────────────


def test_known_keys_empty_when_table_absent(tmp_path: Path) -> None:
    """Pre-migration-007 DB (no secret_active_state) → empty list.

    Soft-fail discipline: env-file writes must never block on a
    metadata-read hiccup. A launcher.db that pre-dates the secret
    schema is not a fatal error — it just means no user secrets
    have been registered yet.
    """
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(db, create_secret_table=False)
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == []


def test_known_keys_empty_when_no_rows(tmp_path: Path) -> None:
    """secret_active_state exists but has no user-bucket rows → empty list."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(db)
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == []


def test_known_keys_per_project_bucket_only(tmp_path: Path) -> None:
    """A KEY registered at per_project scope appears in the strip set."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db, per_project_keys=["MY_PROJECT_TOKEN"],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == ["MY_PROJECT_TOKEN"]


def test_known_keys_shared_bucket_only(tmp_path: Path) -> None:
    """A KEY at shared scope (project_id='_user_shared_') is visible."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db, shared_keys=["SHARED_API_KEY"],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == ["SHARED_API_KEY"]


def test_known_keys_global_bucket_only(tmp_path: Path) -> None:
    """A KEY at global scope (project_id='_global_') is visible."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db, global_keys=["MACHINE_TOKEN"],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == ["MACHINE_TOKEN"]


def test_known_keys_union_across_three_buckets(tmp_path: Path) -> None:
    """All three buckets contribute to the strip set; sorted + deduped."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db,
        per_project_keys=["PER_PROJ_KEY", "ZZ_LAST_KEY"],
        shared_keys=["SHARED_KEY", "AA_FIRST_KEY"],
        global_keys=["GLOBAL_KEY"],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    # All five keys, alphabetically sorted.
    assert keys == [
        "AA_FIRST_KEY", "GLOBAL_KEY", "PER_PROJ_KEY",
        "SHARED_KEY", "ZZ_LAST_KEY",
    ]


def test_known_keys_dedupes_across_buckets(tmp_path: Path) -> None:
    """The same KEY in multiple buckets appears once in the strip set.

    The Rust resolver's bucket-precedence rule (per-project wins on
    VALUE collision) doesn't apply to the strip set — we only need
    the union of KEY names. A KEY appearing in two buckets is
    de-duplicated for the env writer's strip pass.
    """
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db,
        per_project_keys=["SHARED_NAME"],
        shared_keys=["SHARED_NAME"],
        global_keys=["SHARED_NAME"],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == ["SHARED_NAME"]


def test_known_keys_inactive_rows_still_in_strip_set(tmp_path: Path) -> None:
    """An active=0 row STILL appears in the strip set.

    This mirrors Rust's ``list_user_secret_keys_for_project`` which
    selects regardless of ``active``. The whole point of the strip
    set is to remove keys that are paused (active=0) — if we filtered
    on active=1 here, paused secrets would never leave the surfaces.
    """
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db,
        per_project_keys=["ACTIVE_KEY"],
        inactive_keys=[("per_project", "proj-1", "PAUSED_KEY")],
    )
    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert "PAUSED_KEY" in keys
    assert "ACTIVE_KEY" in keys


def test_known_keys_filters_other_projects_per_project_bucket(tmp_path: Path) -> None:
    """Per-project bucket is filtered by project_id; other projects'
    keys do NOT leak.

    Cross-project isolation is critical: a KEY registered for project
    A must not appear in project B's strip set (otherwise the writer
    would helpfully delete project B's same-named user-added key on
    the next refresh).
    """
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(db, per_project_keys=["MY_KEY"])
    # Inject a row for a DIFFERENT project at per_project scope.
    insert_rows(
        db, "secret_active_state",
        [_secret_row("per_project", "OTHER_PROJ", "OTHER_KEY", active=1)],
    )

    keys = user_secret_known_keys_from_db("proj-1", db_path=db)
    assert keys == ["MY_KEY"]
    assert "OTHER_KEY" not in keys


def test_known_keys_db_missing_raises(tmp_path: Path) -> None:
    """Missing launcher DB → DbUnreachable (distinct from empty list).

    Lets callers distinguish "no launcher installed" from "launcher
    installed, no secrets yet" — useful for the Rust subprocess
    bridge's error reporting.
    """
    with pytest.raises(DbUnreachable):
        user_secret_known_keys_from_db("any", db_path=tmp_path / "no.db")


# ─── the refresh's .claude/env carries no user-secret section ───────────


def test_the_refresh_writes_no_user_secret_section_into_claude_env(tmp_path: Path) -> None:
    """The ``.claude/env`` managed block the refresh writes carries canonical
    exports ONLY — never a user-secret section, never a secret key."""
    apply_project_env(
        {"canonical_env": {"KG_COLLECTION": "TestKG"}, "project_id": "test-id",
         "project_root": tmp_path, "user_secret_known_keys": ["GITHUB_TOKEN", "X_TOKEN"]},
        surfaces=["claude_env"],
    )
    text = (tmp_path / ".claude" / "env").read_text()
    assert 'export KG_COLLECTION="TestKG"' in text
    assert "# user secrets" not in text
    assert "GITHUB_TOKEN" not in text and "X_TOKEN" not in text
    assert CLAUDE_ENV_MANAGED_END in text


# ─── CLI verb tests ─────────────────────────────────────────────────────


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m vco_lib.config_projection`` and capture output."""
    cmd = [sys.executable, "-m", "vco_lib.config_projection", *args]
    # child_env() puts the repo root FIRST on PYTHONPATH so the child imports
    # the CHECKOUT's vco_lib, never a stale site-packages copy (§3.16).
    env = child_env(**(env_extra or {}))
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_cli_user_secret_known_keys_json(tmp_path: Path) -> None:
    """``user-secret-known-keys --json`` prints the strip set."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(
        db, per_project_keys=["A_KEY"], shared_keys=["B_KEY"],
    )
    result = _run_cli(
        "user-secret-known-keys",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    keys = json.loads(result.stdout)
    assert keys == ["A_KEY", "B_KEY"]


def test_cli_user_secret_known_keys_plain(tmp_path: Path) -> None:
    """``user-secret-known-keys`` (no --json) prints one key per line."""
    db = tmp_path / "launcher.db"
    _make_launcher_db_with_secrets(db, per_project_keys=["MY_TOKEN"])
    result = _run_cli(
        "user-secret-known-keys",
        "--project-id", "proj-1",
        "--db-path", str(db),
    )
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert "MY_TOKEN" in lines


# ─── Grep-gate: the retired emit contract stays retired ─────────────────


def test_grep_gate_no_pairs_json_callers_tree_wide() -> None:
    """No file in the tree references the retired ``--pairs-json`` flag
    (i.e. no caller can pass a non-empty emit set to the CLI), and no
    file outside the config-projection module + its tests invokes the
    ``apply-user-secrets`` verb at all.

    Allowlist: THIS test file (documents the retirement), CHANGELOG
    (history), and knowledge/docs archives.
    """
    repo_root = Path(__file__).resolve().parent.parent
    flag_offenders: list[str] = []
    verb_offenders: list[str] = []
    allow_flag = {
        "tests/test_config_projection_user_secrets.py",
        "CHANGELOG.md",
        # The module itself documents the retirement in prose (docstrings
        # / comments). The capability check below asserts the PARSER
        # cannot re-register the flag.
        "vco_lib/config_projection.py",
    }
    allow_verb = allow_flag | {"vco_lib/config_projection.py"}

    # Capability check: the argparse registration form of the flag must
    # never return to the module (prose mentions are fine).
    module_text = (repo_root / "vco_lib" / "config_projection.py").read_text(
        encoding="utf-8"
    )
    assert '"--pairs-json"' not in module_text, (
        "the --pairs-json argparse registration was deleted in v0.2.75 and "
        "must not be re-added — the emit contract is retired"
    )
    skip_dirs = {
        ".git", "target", "node_modules", ".venv", "dist", "build",
        ".claude", "knowledge", "docs",
    }
    exts = {
        ".py", ".rs", ".sh", ".ps1", ".ts", ".js", ".svelte", ".toml",
        ".yml", ".yaml", ".json",
    }
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        if any(part in skip_dirs for part in path.relative_to(repo_root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ("pairs-json" in text or "pairs_json" in text) and rel not in allow_flag:
            flag_offenders.append(rel)
        if "apply-user-secrets" in text and rel not in allow_verb:
            verb_offenders.append(rel)
    assert not flag_offenders, (
        f"retired --pairs-json emit flag referenced by: {flag_offenders} — "
        "the value-emitting arm was deleted in v0.2.75; no caller may pass "
        "a non-empty emit set"
    )
    assert not verb_offenders, (
        f"the retired apply-user-secrets verb is referenced by: {verb_offenders} "
        "— it was removed in v0.2.97 (superseded by the evidence-gated refresh "
        "scrub); nothing may call it"
    )
    assert '"apply-user-secrets"' not in module_text, (
        "the apply-user-secrets parser registration was retired in v0.2.97 "
        "and must not return — a strip by NAME destroys a key the user typed"
    )
