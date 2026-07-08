# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""User-secret STRIP contract (Phase 0.E emit arm retired v0.2.75 P3).

Phase 0.E (2026-05-25) shipped an emit-capable user-secret writer;
v0.2.73 abolished value-emission in production (the Rust writer
projects an always-empty emit set), and v0.2.75 P3 DELETED the Python
emit arm entirely. The sole surviving contract is: VCO never writes
secret values into the project tree.

This file tests:

  1. :func:`user_secret_known_keys_from_db` — the STRIP set resolver
     reads the union of three buckets (per-project, shared, global)
     from ``secret_active_state``, dedups across buckets, sorts.

  2. :func:`apply_user_secrets` — the STRIP-ONLY surface writer:
     * A non-empty ``user_secret_pairs`` is a HARD
       ``ConfigProjectionError`` (retired-emit backstop) — on the
       direct entry point AND the combined ``apply_project_env`` path.
     * STRIP every known key from the JSON env sub-blocks; rebuild the
       ``.claude/env`` managed block WITHOUT a user-secret section
       (legacy sections from pre-v0.2.73 launchers removed).
     * Preserve canonical env keys, user-added-by-hand keys, and
       sibling blocks (hooks, permissions, editor config).
     * Re-runs are idempotent — byte-identical output.

  3. Cross-OS atomicity — no .tmp leaks; tempfile lands in target
     directory so the rename is on one filesystem.

  4. CLI verbs ``apply-user-secrets`` (strip-only; the retired
     ``--pairs-json`` flag is rejected by the live argparse parser)
     and ``user-secret-known-keys`` — happy paths + error envelopes
     (project_not_found exits 2, db_unreachable exits 3).

  5. The grep-gate: no caller anywhere in the tree references the
     retired ``--pairs-json`` flag (i.e. nothing can pass a non-empty
     emit set to the CLI).

The CANONICAL writer's behaviour is regression-tested in
``tests/test_config_projection.py``; the tree-wide never-writes-values
invariant lives in ``tests/test_config_projection_byte_identical.py``
and ``projects_v2.rs``.

Run: pytest tests/test_config_projection_user_secrets.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vco_lib.config_projection import (
    CLAUDE_ENV_MANAGED_BEGIN,
    CLAUDE_ENV_MANAGED_END,
    ConfigProjectionError,
    DbUnreachable,
    UserSecretBundle,
    apply_project_env,
    apply_user_secrets,
    user_secret_known_keys_from_db,
)


# ─── DB fixture (secret_active_state schema) ────────────────────────────


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
    """Build a minimal launcher.db with the secret_active_state schema.

    Mirrors the launcher migrations 007 + 009 schema enough for the
    Phase 0.E resolver to read.

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
        create_secret_table: if False, omit the secret_active_state
            table entirely (test soft-fail on pre-migration DBs).
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            slug TEXT NOT NULL
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            PRIMARY KEY (project_id, role)
        );
        """
    )
    if create_secret_table:
        # Mirror migration 007 (post-009 shape). The Python resolver
        # only reads (scope, project_id, module_id, key), so the
        # requester_project_id column isn't required for these
        # tests — but we include it to match production schema.
        cur.executescript(
            """
            CREATE TABLE secret_active_state (
                scope TEXT NOT NULL,
                project_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                key TEXT NOT NULL,
                requester_project_id TEXT NOT NULL DEFAULT '*',
                active INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, project_id, module_id, key, requester_project_id)
            );
            """
        )
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
        (project_id, project_name, project_folder, project_slug),
    )
    if create_secret_table:
        for key in per_project_keys or []:
            cur.execute(
                "INSERT INTO secret_active_state "
                "(scope, project_id, module_id, key, active, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 0)",
                ("per_project", project_id, "user", key),
            )
        for key in shared_keys or []:
            cur.execute(
                "INSERT INTO secret_active_state "
                "(scope, project_id, module_id, key, active, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 0)",
                ("shared", "_user_shared_", "user", key),
            )
        for key in global_keys or []:
            cur.execute(
                "INSERT INTO secret_active_state "
                "(scope, project_id, module_id, key, active, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 0)",
                ("global", "_global_", "user", key),
            )
        for scope, pid, key in inactive_keys or []:
            cur.execute(
                "INSERT INTO secret_active_state "
                "(scope, project_id, module_id, key, active, updated_at) "
                "VALUES (?, ?, ?, ?, 0, 0)",
                (scope, pid, "user", key),
            )
    conn.commit()
    conn.close()


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
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO secret_active_state "
        "(scope, project_id, module_id, key, active, updated_at) "
        "VALUES ('per_project', 'OTHER_PROJ', 'user', 'OTHER_KEY', 1, 0)"
    )
    conn.commit()
    conn.close()

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


# ─── apply_user_secrets — fresh creation (lifecycle 1) ──────────────────


def _make_secret_bundle(
    project_root: Path,
    pairs: list[tuple[str, str]],
    known: list[str],
    project_id: str = "test-id",
) -> UserSecretBundle:
    """Helper: build a UserSecretBundle for the writer tests."""
    return {
        "user_secret_pairs": pairs,
        "user_secret_known_keys": known,
        "project_id": project_id,
        "project_root": project_root,
    }


def test_apply_us_rejects_nonempty_pairs(tmp_path: Path) -> None:
    """v0.2.75 P3: the emit arm is DELETED — a non-empty emit set is a
    hard error naming the retired contract, and NOTHING is written."""
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_abc123")],
        known=["GITHUB_TOKEN"],
    )
    with pytest.raises(ConfigProjectionError, match="retired"):
        apply_user_secrets(bundle, surfaces=["claude_settings_json"])
    assert not (tmp_path / ".claude" / "settings.json").exists()
    # The value must appear NOWHERE under the tree.
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert "ghp_abc123" not in p.read_text(encoding="utf-8")


def test_apply_project_env_combined_rejects_nonempty_pairs(tmp_path: Path) -> None:
    """The combined apply path enforces the same retired-emit backstop."""
    bundle: dict = {
        "canonical_env": {"KG_COLLECTION": "TestKG"},
        "project_id": "test-id",
        "project_root": tmp_path,
    }
    secret_bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("ACTIVE_TOKEN", "active_val")],
        known=["ACTIVE_TOKEN"],
    )
    with pytest.raises(ConfigProjectionError, match="retired"):
        apply_project_env(
            bundle, surfaces=["claude_settings_json"],
            user_secret_bundle=secret_bundle,
        )
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_apply_us_strip_only_fresh_files_carry_no_user_secret_section(
    tmp_path: Path,
) -> None:
    """A strip-only apply against a project with NO prior files creates
    the surface skeletons with EMPTY env content — never a user-secret
    section, never a value."""
    bundle = _make_secret_bundle(tmp_path, pairs=[], known=["GITHUB_TOKEN"])
    report = apply_user_secrets(
        bundle, surfaces=["claude_settings_json", "claude_env"],
    )

    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["env"] == {}

    text = (tmp_path / ".claude" / "env").read_text()
    assert CLAUDE_ENV_MANAGED_BEGIN in text
    assert CLAUDE_ENV_MANAGED_END in text
    assert "# user secrets" not in text

    assert report["claude_settings_json"]["emitted"] == []
    assert report["claude_env"]["emitted"] == []


# ─── apply_user_secrets — STRIP of stale values ─────────────────────────


def test_apply_us_strips_stale_value_from_settings_json(tmp_path: Path) -> None:
    """A stale value written by a pre-fix launcher leaves settings.json;
    canonical keys, user-added-by-hand keys, and sibling blocks
    survive verbatim."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "GITHUB_TOKEN": "ghp_stale_value",        # stripped
            "PAUSED_KEY": "stale_paused_value",       # stripped
            "OPENAI_API_BASE": "user-added-by-hand",  # preserved
            "KG_COLLECTION": "PreservedCanonical",    # preserved
        },
        "hooks": {"PreToolUse": []},  # sibling block — must survive
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    report = apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    raw = settings_path.read_text()
    data = json.loads(raw)
    assert "GITHUB_TOKEN" not in data["env"]
    assert "PAUSED_KEY" not in data["env"]
    assert "ghp_stale_value" not in raw
    # User-added-by-hand key preserved (NOT in known-keys).
    assert data["env"]["OPENAI_API_BASE"] == "user-added-by-hand"
    # Canonical key + sibling block preserved.
    assert data["env"]["KG_COLLECTION"] == "PreservedCanonical"
    assert data["hooks"] == {"PreToolUse": []}

    assert report["claude_settings_json"]["stripped"] == [
        "GITHUB_TOKEN", "PAUSED_KEY",
    ]
    assert report["claude_settings_json"]["emitted"] == []


def test_apply_us_strips_legacy_user_secret_section_from_claude_env(
    tmp_path: Path,
) -> None:
    """A legacy user-secret section (pre-v0.2.73 launcher) is removed by
    the BEGIN/END rebuild; canonical exports and lines outside the
    markers survive verbatim."""
    env_path = tmp_path / ".claude" / "env"
    env_path.parent.mkdir()
    env_path.write_text(
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        '# header\n'
        'export KG_COLLECTION="PreservedCanonical"\n'
        '\n'
        '# user secrets (per-project; managed via launcher GUI Secrets panel)\n'
        'export GITHUB_TOKEN="ghp_OLD"\n'
        'export PAUSED_KEY="stale"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
        "# user trailer\n"
    )

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])

    text = env_path.read_text()
    # Every legacy secret line gone — values included.
    assert "GITHUB_TOKEN" not in text
    assert "PAUSED_KEY" not in text
    assert "ghp_OLD" not in text
    assert "# user secrets" not in text
    # Canonical export preserved across the rebuild.
    assert 'export KG_COLLECTION="PreservedCanonical"' in text
    # User trailer (outside markers) preserved.
    assert "# user trailer" in text


def test_apply_us_full_deletion_empty_pairs_strips_all_known(
    tmp_path: Path,
) -> None:
    """All-deletion case: empty pairs + non-empty known → every known
    key is stripped from the JSON env block.

    This is the "user unregistered all their secrets" flow — the
    resolver returns empty pairs, but the strip set still carries
    the keys until ``forget_user_secret_state_for_project`` runs.
    """
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "KEY_A": "valA",
            "KEY_B": "valB",
            "USER_ADDED": "preserved",
        },
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["KEY_A", "KEY_B"],
    )
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert "KEY_A" not in data["env"]
    assert "KEY_B" not in data["env"]
    # User-added-by-hand preserved.
    assert data["env"]["USER_ADDED"] == "preserved"


# ─── apply_user_secrets — invariants and idempotence ────────────────────


def test_apply_us_preserves_user_added_by_hand_keys(tmp_path: Path) -> None:
    """A KEY that's NOT in known-keys is left untouched.

    User-added-by-hand keys (the user edited settings.json directly,
    bypassing set_secret_v2) are NEVER in the strip set by construction
    of the Rust resolver — only keys that came through set_secret_v2
    land in secret_active_state. The writer must not assume "every
    user-shaped key in the env block is a strip candidate".
    """
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "BY_HAND_KEY": "preserved",
            "ANOTHER_BY_HAND": "also preserved",
        },
    }, indent=2))

    # Empty bundle: no pairs, no known-keys.
    bundle = _make_secret_bundle(tmp_path, pairs=[], known=[])
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert data["env"]["BY_HAND_KEY"] == "preserved"
    assert data["env"]["ANOTHER_BY_HAND"] == "also preserved"


def test_apply_us_idempotent_settings_json(tmp_path: Path) -> None:
    """Two strip-only apply_user_secrets calls with the same bundle
    produce byte-identical settings.json output."""
    # Seed canonical content + stale strippables first.
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {"KG_COLLECTION": "TestKG", "TOKEN_A": "stale"},
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["TOKEN_A", "TOKEN_B"],
    )
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])
    first = settings_path.read_bytes()
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])
    second = settings_path.read_bytes()
    assert first == second
    assert b"TOKEN_A" not in first


def test_apply_us_idempotent_claude_env(tmp_path: Path) -> None:
    """Two strip-only calls produce byte-identical .claude/env."""
    env_path = tmp_path / ".claude" / "env"
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["TOKEN_A", "TOKEN_B"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])
    first = env_path.read_bytes()
    apply_user_secrets(bundle, surfaces=["claude_env"])
    second = env_path.read_bytes()
    assert first == second


def test_apply_us_atomic_no_tempfile_leak(tmp_path: Path) -> None:
    """After a successful apply, no .tmp files remain in the target dir."""
    bundle = _make_secret_bundle(
        tmp_path, pairs=[], known=["KEY"],
    )
    apply_user_secrets(
        bundle, surfaces=["claude_settings_json", "claude_env"],
    )
    stragglers = (
        list((tmp_path / ".claude").glob("*.tmp"))
        + list((tmp_path / ".claude").glob("*.tmp*"))
    )
    assert not stragglers, f"tempfile leak: {stragglers}"


def test_apply_us_unknown_surface_raises(tmp_path: Path) -> None:
    bundle = _make_secret_bundle(tmp_path, pairs=[], known=[])
    with pytest.raises(ConfigProjectionError, match="unknown surface"):
        apply_user_secrets(bundle, surfaces=["bogus"])


def test_apply_us_vscode_surface_opt_in(tmp_path: Path) -> None:
    """The .vscode/settings.json surface is opt-in via the ``surfaces``
    arg — and the strip applies there too."""
    vscode = tmp_path / ".vscode" / "settings.json"
    vscode.parent.mkdir()
    vscode.write_text(json.dumps({
        "claude-code.env": {"KEY": "stale", "BY_HAND": "kept"},
    }))

    bundle = _make_secret_bundle(tmp_path, pairs=[], known=["KEY"])
    # Default surfaces: vscode NOT included — file untouched.
    apply_user_secrets(bundle)
    assert json.loads(vscode.read_text())["claude-code.env"]["KEY"] == "stale"

    # Explicit opt-in strips.
    apply_user_secrets(bundle, surfaces=["vscode_settings_json"])
    data = json.loads(vscode.read_text())
    assert "KEY" not in data["claude-code.env"]
    assert data["claude-code.env"]["BY_HAND"] == "kept"


# ─── Combined apply_project_env(user_secret_bundle=...) ─────────────────


def test_apply_project_env_with_user_secret_bundle_combined_strip(
    tmp_path: Path,
) -> None:
    """The ``user_secret_bundle`` kwarg on apply_project_env applies the
    canonical write + the user-secret STRIP in ONE pass per surface
    (atomic per file). No values are ever emitted."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "PAUSED_KEY": "should-be-stripped",
            "OPENAI_API_BASE": "user-canonical-override-preserved",
        },
    }, indent=2))

    bundle: dict = {
        "canonical_env": {
            "KG_COLLECTION": "TestKG",
            "PROJECT_NAME": "Test",
        },
        "project_id": "test-id",
        "project_root": tmp_path,
    }
    secret_bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["ACTIVE_TOKEN", "PAUSED_KEY"],
    )

    apply_project_env(
        bundle, surfaces=["claude_settings_json"],
        user_secret_bundle=secret_bundle,
    )

    data = json.loads(settings_path.read_text())
    # Canonical key landed.
    assert data["env"]["KG_COLLECTION"] == "TestKG"
    # Every known user-secret key stripped / never written.
    assert "PAUSED_KEY" not in data["env"]
    assert "ACTIVE_TOKEN" not in data["env"]
    # User-canonical override preserved.
    assert data["env"]["OPENAI_API_BASE"] == "user-canonical-override-preserved"


def test_apply_project_env_user_bundle_writes_no_user_secret_section(
    tmp_path: Path,
) -> None:
    """Combined write to .claude/env carries canonical exports ONLY —
    no user-secret section (the emit arm is retired)."""
    bundle: dict = {
        "canonical_env": {"KG_COLLECTION": "TestKG"},
        "project_id": "test-id",
        "project_root": tmp_path,
    }
    secret_bundle = _make_secret_bundle(
        tmp_path,
        pairs=[],
        known=["GITHUB_TOKEN"],
    )
    apply_project_env(
        bundle, surfaces=["claude_env"],
        user_secret_bundle=secret_bundle,
    )

    text = (tmp_path / ".claude" / "env").read_text()
    assert 'export KG_COLLECTION="TestKG"' in text
    assert "# user secrets" not in text
    assert "GITHUB_TOKEN" not in text
    assert CLAUDE_ENV_MANAGED_END in text


# ─── CLI verb tests ─────────────────────────────────────────────────────


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m vco_lib.config_projection`` and capture output."""
    cmd = [sys.executable, "-m", "vco_lib.config_projection", *args]
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    repo_root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
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


def test_cli_apply_user_secrets_happy_path_strips_known(tmp_path: Path) -> None:
    """``apply-user-secrets`` STRIPS every known key from the surfaces.

    Full happy-path round-trip: build a DB with known-keys, pre-seed
    the surfaces with stale values, invoke the CLI, verify the strip.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "myproj"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
        per_project_keys=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    settings_path = proj / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {"GITHUB_TOKEN": "ghp_stale", "KEEP_ME": "user-by-hand"},
    }))

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--surfaces", "claude_settings_json,claude_env",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True

    raw = settings_path.read_text()
    settings = json.loads(raw)
    assert "GITHUB_TOKEN" not in settings["env"]
    assert "PAUSED_KEY" not in settings["env"]
    assert "ghp_stale" not in raw
    assert settings["env"]["KEEP_ME"] == "user-by-hand"

    env_text = (proj / ".claude" / "env").read_text()
    assert "GITHUB_TOKEN" not in env_text
    assert "# user secrets" not in env_text


def test_cli_apply_user_secrets_rejects_retired_pairs_json_flag(
    tmp_path: Path,
) -> None:
    """The grep-gate's live-CLI twin: the retired ``--pairs-json`` flag
    is REJECTED by the real argparse parser (a caller that still passes
    it fails loudly rather than silently emitting values). Live-binary
    regression per the argv-shape-tests-miss-parser-rejections lesson.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
    )
    pairs_json = tmp_path / "pairs.json"
    pairs_json.write_text(json.dumps([["KEY", "value"]]))

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--pairs-json", str(pairs_json),
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
    # Nothing written.
    assert not (proj / ".claude" / "settings.json").exists()


def test_cli_apply_user_secrets_project_not_found_exits_2(tmp_path: Path) -> None:
    """A non-existent project_id exits 2 with a JSON error envelope."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="real-proj", project_folder=str(proj),
    )

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "ghost-proj",
        "--db-path", str(db),
    )
    assert result.returncode == 2
    err = json.loads(result.stderr)
    assert err["error"] == "project_not_found"


def test_cli_apply_user_secrets_db_missing_exits_3(tmp_path: Path) -> None:
    """A missing launcher DB exits 3 with a JSON error envelope."""
    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "any",
        "--db-path", str(tmp_path / "no.db"),
    )
    assert result.returncode == 3
    err = json.loads(result.stderr)
    assert err["error"] == "db_unreachable"


def test_cli_apply_user_secrets_purges_all_known(tmp_path: Path) -> None:
    """The default invocation IS the purge workflow: every known key is
    stripped from the surfaces (there is no other mode)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "myproj"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
        per_project_keys=["DROP_THIS"],
    )
    settings_path = proj / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {"DROP_THIS": "stale"},
    }))

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--surfaces", "claude_settings_json",
    )
    assert result.returncode == 0, result.stderr

    data = json.loads(settings_path.read_text())
    assert "DROP_THIS" not in data["env"]


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
        f"unexpected apply-user-secrets callers: {verb_offenders} — the verb "
        "is strip-only and currently has no production spawner; a new caller "
        "must be reviewed against the never-writes-values invariant"
    )
