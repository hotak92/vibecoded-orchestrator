# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Phase 0.E (2026-05-25) — user-secret writes through the Python contract.

Phase 0.B (2026-05-24) explicitly excluded ``user_secret_pairs`` /
``user_secret_known_keys`` from ``vco_lib.config_projection`` because
their VALUE side lives in the OS keychain (Rust-owned, no Python
bridge). Phase 0.E extends the contract to cover their WRITE side
without bridging the keychain: the Rust caller resolves keychain
values and passes them in via :class:`UserSecretBundle`; Python owns
the byte layout (settings.json deep-merge, .claude/env BEGIN/END
managed block, .vscode/settings.json deep-merge).

This file tests:

  1. :func:`user_secret_known_keys_from_db` — the STRIP set resolver
     reads the union of three buckets (per-project, shared, global)
     from ``secret_active_state``, dedups across buckets, sorts.

  2. :func:`apply_user_secrets` — the surface writer:
     * EMIT pairs into JSON env sub-blocks (settings.json,
       .vscode/settings.json) and the ``.claude/env`` managed block.
     * STRIP pairs that are in the known-keys list but NOT in the
       active pairs — paused / deleted secrets leave the surfaces.
     * Preserve canonical env keys, user-added-by-hand keys, and
       sibling blocks (hooks, permissions, editor config).
     * Re-runs are idempotent — byte-identical output for the same
       input.

  3. Three lifecycle scenarios (per the v0.2.34 spec):
     * Fresh secret creation — KEY in pairs AND in known-keys.
     * Secret update (overwrite existing) — same key, new value.
     * Secret deletion / pause — KEY in known-keys but NOT in pairs.

  4. Cross-OS atomicity — no .tmp leaks; tempfile lands in target
     directory so the rename is on one filesystem.

  5. CLI verbs ``apply-user-secrets`` and ``user-secret-known-keys``
     — happy paths + error envelopes (project_not_found exits 2,
     db_unreachable exits 3, pairs_json_invalid exits 5).

The CANONICAL writer's behaviour is regression-tested in
``tests/test_config_projection.py``; this file only covers the new
user-secret surface. Combined-pass writes (canonical + user secrets
in one :func:`apply_project_env` call via the new
``user_secret_bundle`` kwarg) are also tested here because the
combination only emerged with Phase 0.E.

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


def test_apply_us_fresh_creation_writes_to_settings_json(tmp_path: Path) -> None:
    """Lifecycle 1: fresh secret creation lands in .claude/settings.json.

    No prior file exists. apply_user_secrets creates settings.json
    with the user-secret pair in the env sub-block.
    """
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_abc123")],
        known=["GITHUB_TOKEN"],
    )
    report = apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert data["env"]["GITHUB_TOKEN"] == "ghp_abc123"

    assert report["claude_settings_json"]["emitted"] == ["GITHUB_TOKEN"]
    assert report["claude_settings_json"]["stripped"] == []


def test_apply_us_fresh_creation_writes_to_claude_env(tmp_path: Path) -> None:
    """Lifecycle 1: fresh secret lands in .claude/env managed block.

    With NO existing canonical content, the writer emits a managed
    block containing only the user-secret section. This is the
    "set_secret_v2 ran before write_project_env_files ever did"
    case — rare but handled.
    """
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_abc123")],
        known=["GITHUB_TOKEN"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])

    env_path = tmp_path / ".claude" / "env"
    text = env_path.read_text()
    assert CLAUDE_ENV_MANAGED_BEGIN in text
    assert CLAUDE_ENV_MANAGED_END in text
    assert 'export GITHUB_TOKEN="ghp_abc123"' in text
    # Section header is present.
    assert "# user secrets (per-project; managed via launcher GUI Secrets panel)" in text


def test_apply_us_three_buckets_emit_in_order(tmp_path: Path) -> None:
    """Lifecycle 1, three buckets: all three KEYS land in the surface.

    The Rust resolver returns pairs ordered per-project → shared →
    global (collision-wins-per-project). We mirror that ordering
    on the writer side. ``apply_user_secrets`` doesn't re-sort
    (the resolver decides order; the writer respects it).
    """
    pairs = [
        ("PER_PROJ_KEY", "pp_val"),
        ("SHARED_KEY", "shared_val"),
        ("GLOBAL_KEY", "global_val"),
    ]
    bundle = _make_secret_bundle(
        tmp_path, pairs=pairs,
        known=["PER_PROJ_KEY", "SHARED_KEY", "GLOBAL_KEY"],
    )
    apply_user_secrets(
        bundle, surfaces=["claude_settings_json", "claude_env"],
    )

    settings = json.loads(
        (tmp_path / ".claude" / "settings.json").read_text()
    )
    env_block = settings["env"]
    assert env_block["PER_PROJ_KEY"] == "pp_val"
    assert env_block["SHARED_KEY"] == "shared_val"
    assert env_block["GLOBAL_KEY"] == "global_val"

    env_text = (tmp_path / ".claude" / "env").read_text()
    # All three exports present in the .claude/env managed block.
    assert 'export PER_PROJ_KEY="pp_val"' in env_text
    assert 'export SHARED_KEY="shared_val"' in env_text
    assert 'export GLOBAL_KEY="global_val"' in env_text


# ─── apply_user_secrets — secret update (lifecycle 2) ───────────────────


def test_apply_us_overwrite_existing_value_in_settings_json(tmp_path: Path) -> None:
    """Lifecycle 2: secret update overwrites the existing JSON value."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "GITHUB_TOKEN": "ghp_OLD_VALUE",
            "KG_COLLECTION": "PreservedCanonical",
        },
        "hooks": {"PreToolUse": []},  # sibling block — must survive
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_NEW_VALUE")],
        known=["GITHUB_TOKEN"],
    )
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert data["env"]["GITHUB_TOKEN"] == "ghp_NEW_VALUE"
    assert "ghp_OLD_VALUE" not in settings_path.read_text()
    # Canonical key preserved (not touched by user-secret writer).
    assert data["env"]["KG_COLLECTION"] == "PreservedCanonical"
    # Sibling block preserved.
    assert data["hooks"] == {"PreToolUse": []}


def test_apply_us_overwrite_existing_value_in_claude_env(tmp_path: Path) -> None:
    """Lifecycle 2: secret update overwrites in .claude/env managed block.

    The whole BEGIN/END managed block is rewritten. The OLD value
    must not survive anywhere in the file (no stale copy in a
    duplicate export line).
    """
    env_path = tmp_path / ".claude" / "env"
    env_path.parent.mkdir()
    # Seed a managed block with old user-secret value.
    env_path.write_text(
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        '# header\n'
        'export KG_COLLECTION="PreservedCanonical"\n'
        '\n'
        '# user secrets (per-project; managed via launcher GUI Secrets panel)\n'
        'export GITHUB_TOKEN="ghp_OLD"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
        "# user trailer\n"
    )

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_NEW")],
        known=["GITHUB_TOKEN"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])

    text = env_path.read_text()
    assert 'export GITHUB_TOKEN="ghp_NEW"' in text
    # OLD value must be gone — including any cosmetic mention.
    assert "ghp_OLD" not in text
    # Canonical export preserved across the rebuild.
    assert 'export KG_COLLECTION="PreservedCanonical"' in text
    # User trailer (outside markers) preserved.
    assert "# user trailer" in text


# ─── apply_user_secrets — secret deletion / pause (lifecycle 3) ─────────


def test_apply_us_strip_paused_key_from_settings_json(tmp_path: Path) -> None:
    """Lifecycle 3: a KEY in known-keys but NOT in pairs is STRIPPED.

    This is the "user paused / deleted the secret in the SecretsPanel"
    case. The launcher DB still carries the active-flag row (so we
    know the KEY exists), but the resolver's EMIT list omits it
    (active=0 or keychain returned None). The writer must remove
    it from the JSON env block.
    """
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "GITHUB_TOKEN": "ghp_active_value",   # still active
            "PAUSED_KEY": "stale_paused_value",   # should be stripped
            "OPENAI_API_BASE": "user-added-by-hand",  # preserved
            "KG_COLLECTION": "PreservedCanonical",
        },
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_active_value")],
        # PAUSED_KEY is in known-keys (still tracked) but NOT in pairs
        # (not actively emitted) → must be stripped.
        known=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    report = apply_user_secrets(bundle, surfaces=["claude_settings_json"])

    data = json.loads(settings_path.read_text())
    assert data["env"]["GITHUB_TOKEN"] == "ghp_active_value"
    # Paused key removed from the env block.
    assert "PAUSED_KEY" not in data["env"]
    # User-added-by-hand key preserved (NOT in known-keys).
    assert data["env"]["OPENAI_API_BASE"] == "user-added-by-hand"
    # Canonical key preserved.
    assert data["env"]["KG_COLLECTION"] == "PreservedCanonical"

    assert "PAUSED_KEY" in report["claude_settings_json"]["stripped"]
    assert "GITHUB_TOKEN" in report["claude_settings_json"]["emitted"]


def test_apply_us_strip_paused_key_from_claude_env(tmp_path: Path) -> None:
    """Lifecycle 3: strip is implicit in .claude/env via BEGIN/END rebuild."""
    env_path = tmp_path / ".claude" / "env"
    env_path.parent.mkdir()
    env_path.write_text(
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        '# header\n'
        'export KG_COLLECTION="PreservedCanonical"\n'
        '\n'
        '# user secrets (per-project; managed via launcher GUI Secrets panel)\n'
        'export GITHUB_TOKEN="ghp_active"\n'
        'export PAUSED_KEY="stale"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
    )

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_active")],
        known=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])

    text = env_path.read_text()
    assert 'export GITHUB_TOKEN="ghp_active"' in text
    # Paused key absent from rebuilt managed block.
    assert "PAUSED_KEY" not in text
    # Canonical preserved.
    assert 'export KG_COLLECTION="PreservedCanonical"' in text


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
    """Two apply_user_secrets calls with the same bundle produce
    byte-identical settings.json output."""
    # Seed canonical content first (settings.json must exist for the
    # idempotence comparison to be meaningful).
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {"KG_COLLECTION": "TestKG"},
    }, indent=2))

    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("TOKEN_A", "val_a"), ("TOKEN_B", "val_b")],
        known=["TOKEN_A", "TOKEN_B"],
    )
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])
    first = settings_path.read_bytes()
    apply_user_secrets(bundle, surfaces=["claude_settings_json"])
    second = settings_path.read_bytes()
    assert first == second


def test_apply_us_idempotent_claude_env(tmp_path: Path) -> None:
    """Two apply_user_secrets calls produce byte-identical .claude/env."""
    env_path = tmp_path / ".claude" / "env"
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("TOKEN_A", "val_a"), ("TOKEN_B", "val_b")],
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
        tmp_path, pairs=[("KEY", "val")], known=["KEY"],
    )
    apply_user_secrets(
        bundle, surfaces=["claude_settings_json", "claude_env"],
    )
    stragglers = (
        list((tmp_path / ".claude").glob("*.tmp"))
        + list((tmp_path / ".claude").glob("*.tmp*"))
    )
    assert not stragglers, f"tempfile leak: {stragglers}"


def test_apply_us_escapes_double_quotes_in_claude_env(tmp_path: Path) -> None:
    """Values with embedded double quotes are backslash-escaped in
    .claude/env (matches the Rust writer)."""
    bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("WEIRD_KEY", 'value "with" quotes')],
        known=["WEIRD_KEY"],
    )
    apply_user_secrets(bundle, surfaces=["claude_env"])
    text = (tmp_path / ".claude" / "env").read_text()
    assert r'export WEIRD_KEY="value \"with\" quotes"' in text


def test_apply_us_unknown_surface_raises(tmp_path: Path) -> None:
    bundle = _make_secret_bundle(tmp_path, pairs=[], known=[])
    with pytest.raises(ConfigProjectionError, match="unknown surface"):
        apply_user_secrets(bundle, surfaces=["bogus"])


def test_apply_us_vscode_surface_opt_in(tmp_path: Path) -> None:
    """The .vscode/settings.json surface is opt-in via the ``surfaces`` arg."""
    bundle = _make_secret_bundle(
        tmp_path, pairs=[("KEY", "val")], known=["KEY"],
    )
    # Default surfaces: vscode NOT included.
    apply_user_secrets(bundle)
    assert not (tmp_path / ".vscode" / "settings.json").exists()

    # Explicit opt-in.
    apply_user_secrets(bundle, surfaces=["vscode_settings_json"])
    vscode = tmp_path / ".vscode" / "settings.json"
    assert vscode.exists()
    data = json.loads(vscode.read_text())
    assert data["claude-code.env"]["KEY"] == "val"


# ─── Combined apply_project_env(user_secret_bundle=...) ─────────────────


def test_apply_project_env_with_user_secret_bundle_combined(
    tmp_path: Path,
) -> None:
    """The new ``user_secret_bundle`` kwarg on apply_project_env writes
    canonical + secrets in ONE pass per surface (atomic per file)."""
    # Pre-populate with a canonical user override + a paused user-secret
    # key so we can verify both the canonical deep-merge AND the
    # user-secret EMIT/STRIP run in the same pass.
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
        pairs=[("ACTIVE_TOKEN", "active_val")],
        known=["ACTIVE_TOKEN", "PAUSED_KEY"],
    )

    apply_project_env(
        bundle, surfaces=["claude_settings_json"],
        user_secret_bundle=secret_bundle,
    )

    data = json.loads(settings_path.read_text())
    # Canonical key landed.
    assert data["env"]["KG_COLLECTION"] == "TestKG"
    # Active secret landed.
    assert data["env"]["ACTIVE_TOKEN"] == "active_val"
    # Paused secret stripped.
    assert "PAUSED_KEY" not in data["env"]
    # User-canonical override preserved.
    assert data["env"]["OPENAI_API_BASE"] == "user-canonical-override-preserved"


def test_apply_project_env_user_bundle_emit_after_canonical_in_claude_env(
    tmp_path: Path,
) -> None:
    """Combined write to .claude/env emits user secrets AFTER canonical
    in the managed block (byte order matches Rust)."""
    bundle: dict = {
        "canonical_env": {"KG_COLLECTION": "TestKG"},
        "project_id": "test-id",
        "project_root": tmp_path,
    }
    secret_bundle = _make_secret_bundle(
        tmp_path,
        pairs=[("GITHUB_TOKEN", "ghp_x")],
        known=["GITHUB_TOKEN"],
    )
    apply_project_env(
        bundle, surfaces=["claude_env"],
        user_secret_bundle=secret_bundle,
    )

    text = (tmp_path / ".claude" / "env").read_text()
    kg_idx = text.find('export KG_COLLECTION="TestKG"')
    secret_header_idx = text.find(
        "# user secrets (per-project; managed via launcher GUI Secrets panel)"
    )
    token_idx = text.find('export GITHUB_TOKEN="ghp_x"')
    end_idx = text.find(CLAUDE_ENV_MANAGED_END)
    assert kg_idx > 0
    assert secret_header_idx > kg_idx, (
        "user-secret section header must come AFTER the canonical exports"
    )
    assert token_idx > secret_header_idx
    assert end_idx > token_idx


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


def test_cli_apply_user_secrets_happy_path(tmp_path: Path) -> None:
    """``apply-user-secrets`` writes user pairs into the project's surfaces.

    Full happy-path round-trip: build a DB with known-keys, write a
    pairs JSON file, invoke the CLI, verify the surfaces.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "myproj"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
        per_project_keys=["GITHUB_TOKEN", "PAUSED_KEY"],
    )
    pairs_json = tmp_path / "pairs.json"
    pairs_json.write_text(json.dumps([["GITHUB_TOKEN", "ghp_real"]]))

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--pairs-json", str(pairs_json),
        "--surfaces", "claude_settings_json,claude_env",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True

    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert settings["env"]["GITHUB_TOKEN"] == "ghp_real"
    # PAUSED_KEY is in known but not in pairs → stripped (or never written).
    assert "PAUSED_KEY" not in settings["env"]

    env_text = (proj / ".claude" / "env").read_text()
    assert 'export GITHUB_TOKEN="ghp_real"' in env_text
    assert "PAUSED_KEY" not in env_text


def test_cli_apply_user_secrets_empty_pairs_strips_known(tmp_path: Path) -> None:
    """A pairs-json with [] (empty list) triggers STRIP-only behaviour.

    Useful for "user unregistered every secret" flows where the
    keychain is empty but the strip set still has rows.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "myproj"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
        per_project_keys=["DROP_ME"],
    )
    # Pre-populate the surface with the key so we can verify the strip.
    settings_path = proj / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {"DROP_ME": "stale", "KEEP_ME": "user-by-hand"},
    }))

    pairs_json = tmp_path / "pairs.json"
    pairs_json.write_text("[]")

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--pairs-json", str(pairs_json),
        "--surfaces", "claude_settings_json",
    )
    assert result.returncode == 0, result.stderr

    data = json.loads(settings_path.read_text())
    assert "DROP_ME" not in data["env"]
    assert data["env"]["KEEP_ME"] == "user-by-hand"


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


def test_cli_apply_user_secrets_invalid_pairs_json_exits_5(tmp_path: Path) -> None:
    """A malformed pairs-json exits 5 with a JSON error envelope."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
    )
    # Write a JSON object instead of an array — invalid pair shape.
    pairs_json = tmp_path / "pairs.json"
    pairs_json.write_text('{"not": "a list"}')

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--pairs-json", str(pairs_json),
    )
    assert result.returncode == 5
    err = json.loads(result.stderr)
    assert err["error"] == "pairs_json_invalid"


def test_cli_apply_user_secrets_invalid_pair_shape_exits_5(tmp_path: Path) -> None:
    """Pairs that aren't [string, string] arrays fail with exit 5."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db_with_secrets(
        db, project_id="proj-1", project_folder=str(proj),
    )
    pairs_json = tmp_path / "pairs.json"
    # Bad shape: each entry must be [string, string].
    pairs_json.write_text(json.dumps([["KEY", 123]]))

    result = _run_cli(
        "apply-user-secrets",
        "--project-id", "proj-1",
        "--db-path", str(db),
        "--pairs-json", str(pairs_json),
    )
    assert result.returncode == 5


def test_cli_apply_user_secrets_no_pairs_json_treats_as_empty(
    tmp_path: Path,
) -> None:
    """Omitting --pairs-json runs the STRIP-only path (empty pairs).

    This is the "purge every user secret" workflow — useful if the
    Rust caller wants to clear all secrets in one shot without
    enumerating them.
    """
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
