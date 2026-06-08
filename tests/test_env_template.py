# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``vco_lib.env_template`` (Phase 0.D contract).

Covers:

  1. :func:`project_env_template_from_db` — pure resolver, subset filter
     of the Phase 0.B canonical map.
  2. :func:`apply_env_template` — managed-block writer, marker-bracketed
     block replace, user-content preservation, atomic write discipline.
  3. CLI entry points — ``apply``, ``list-keys``, ``from-db`` happy paths
     plus error envelopes.
  4. Subset invariant — every ``.env`` template key must also be a
     Phase 0.B canonical key (re-asserted at runtime).

The byte-identical regression guard lives in
``tests/test_env_template_byte_identical.py`` (separate file so this
one stays small and fast).

Run: pytest tests/test_env_template.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib.config_projection import (
    DbUnreachable,
    ProjectNotFound,
    list_canonical_keys,
)
from vco_lib.env_template import (
    ENV_TEMPLATE_BEGIN,
    ENV_TEMPLATE_END,
    apply_env_template,
    list_canonical_env_template_keys,
    project_env_template_from_db,
)


# ─── DB fixture (mirrors tests/test_config_projection.py) ───────────────


def _make_launcher_db(
    db_path: Path,
    *,
    project_id: str = "proj-001",
    project_name: str = "Demo Project",
    project_folder: str = "/tmp/demo",
    project_slug: str = "demo-project",
    extra_projects: list[tuple[str, str, str, str]] | None = None,
    kg_bindings: dict[str, str] | None = None,
    kg_access: list[tuple[str, str]] | None = None,
    codegraph_access: list[tuple[str, str]] | None = None,
    module_settings: list[tuple[str, str, str, str]] | None = None,
) -> None:
    """Build a minimal launcher.db with the schema the resolver reads.

    Identical to ``tests/test_config_projection.py::_make_launcher_db``;
    intentionally duplicated rather than imported so the two test files
    stay independently runnable (no cross-test-file fixture imports).
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
            embedding_model TEXT,
            PRIMARY KEY (project_id, role)
        );
        CREATE TABLE kg_collection_access (
            project_id TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            access_level TEXT NOT NULL,
            -- v0.2.49 Step F SF6 (L3-SF1): align test-only DDL with the
            -- production schema. Migration 029 added these audit columns;
            -- this fixture hand-rolls its own DDL and omitted them.
            -- DEFAULT 0 matches migration 029's backfill of legacy rows.
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, collection_name)
        );
        CREATE TABLE codegraph_access (
            grantor_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grantor_project_id, grantee_project_id)
        );
        CREATE TABLE module_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            module_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            UNIQUE(project_id, module_id, setting_key)
        );
        """
    )
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
        (project_id, project_name, project_folder, project_slug),
    )
    for row in extra_projects or []:
        cur.execute(
            "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
            row,
        )
    for role, coll in (kg_bindings or {}).items():
        cur.execute(
            "INSERT INTO project_kg_bindings (project_id, role, collection_name) "
            "VALUES (?, ?, ?)",
            (project_id, role, coll),
        )
    for coll, level in kg_access or []:
        cur.execute(
            "INSERT INTO kg_collection_access (project_id, collection_name, access_level) "
            "VALUES (?, ?, ?)",
            (project_id, coll, level),
        )
    for grantor, level in codegraph_access or []:
        cur.execute(
            "INSERT INTO codegraph_access (grantor_project_id, grantee_project_id, "
            "access_level, granted_at) VALUES (?, ?, ?, ?)",
            (grantor, project_id, level, 0),
        )
    for pid, mid, key, value in module_settings or []:
        cur.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value) "
            "VALUES (?, ?, ?, ?)",
            (pid, mid, key, value),
        )
    conn.commit()
    conn.close()


# ─── Subset invariant ───────────────────────────────────────────────────


def test_template_keys_are_subset_of_phase_0b_canonical() -> None:
    """The .env template key set is a STRICT subset of the Phase 0.B
    canonical key set — runtime-asserted at import; this test confirms
    the public surface agrees."""
    template_keys = list_canonical_env_template_keys()
    full_keys = list_canonical_keys()
    assert template_keys.issubset(full_keys), (
        f"env_template keys not in config_projection canonical: "
        f"{sorted(template_keys - full_keys)}"
    )
    # And the subset is non-trivially smaller — Phase 0.D's value-add
    # is the curated INCLUDE list, not a copy of every key.
    assert len(template_keys) < len(full_keys), (
        "env_template should EXCLUDE access-list / orchestrator-root / "
        "secret keys per the docstring rationale; the subset must be "
        "strictly smaller than the full canonical set."
    )


def test_template_keys_excludes_known_runtime_concerns() -> None:
    """Sanity guard: keys that change per-session or live in the keychain
    must NOT be in the .env template subset."""
    keys = list_canonical_env_template_keys()
    excluded_by_design = {
        "VCT_KG_ACCESS_LIST",           # per-session grant snapshot
        "VCT_CODE_GRAPH_ACCESS_LIST",   # per-session grant snapshot
        "VCT_ORCHESTRATOR_ROOT",        # launcher-install-local path
        "VCT_INFRASTRUCTURE_DIR",       # launcher-install-local path
        "VCT_INSTALL_ROOT",             # launcher-install-local path (v0.2.37 alias)
        "GITHUB_TOKEN",                 # secret; keychain-owned
    }
    leaked = keys & excluded_by_design
    assert not leaked, (
        f"Keys leaked into .env template that shouldn't be there: "
        f"{sorted(leaked)}. See vco_lib/env_template.py module docstring "
        f"for EXCLUDE rationale per key."
    )


def test_template_keys_includes_identity_and_services() -> None:
    """Sanity guard: the INCLUDE side of the subset rationale."""
    keys = list_canonical_env_template_keys()
    must_include = {
        "PROJECT_NAME",
        "KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
        "SHARED_KG_COLLECTION",
        "SHARED_KG_WRITE_DISABLED",
        "WEAVIATE_URL",
        "OLLAMA_URL",
        "ACTIVE_EMBEDDING",
    }
    missing = must_include - keys
    assert not missing, f"Missing from .env template subset: {sorted(missing)}"


def test_list_keys_returns_fresh_set() -> None:
    """Each call returns a fresh set so mutation doesn't leak."""
    a = list_canonical_env_template_keys()
    a.add("FAKE_KEY")
    b = list_canonical_env_template_keys()
    assert "FAKE_KEY" not in b


# ─── project_env_template_from_db tests ─────────────────────────────────


def test_from_db_happy_returns_subset(tmp_path: Path) -> None:
    """The resolver returns a dict containing ONLY template-subset keys
    with their resolved values."""
    db = tmp_path / "launcher.db"
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    _make_launcher_db(
        db,
        project_id="p1",
        project_name="My App",
        project_folder=str(project_folder),
    )

    keys = project_env_template_from_db("p1", db_path=db)

    # All returned keys are in the subset.
    template_subset = list_canonical_env_template_keys()
    assert set(keys.keys()).issubset(template_subset)

    # Identity + collection keys resolved.
    assert keys["PROJECT_NAME"] == "My App"
    assert keys["CODE_GRAPH_PROJECT"] == "MyApp"
    assert keys["KG_COLLECTION"] == "MyApp_KnowledgeGraph"
    assert keys["DEVELOPMENT_COLLECTION"] == "MyApp_Development"
    assert keys["SHARED_KG_COLLECTION"] == "VibeCodedOrchestrator_KnowledgeGraph"
    assert keys["SHARED_KG_WRITE_DISABLED"] == "false"
    assert keys["SHARED_KG_OPT_OUT"] == "false"
    assert keys["ACTIVE_EMBEDDING"] == "qwen3"
    assert keys["WEAVIATE_URL"] == "http://localhost:8081"
    assert keys["OLLAMA_URL"] == "http://localhost:11435"


def test_from_db_excludes_access_lists_even_when_resolver_populates_them(
    tmp_path: Path,
) -> None:
    """When the Phase 0.B resolver returns ``VCT_KG_ACCESS_LIST`` /
    ``VCT_CODE_GRAPH_ACCESS_LIST``, the template resolver STRIPS them."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="grantee",
        project_name="G",
        project_folder=str(proj),
        project_slug="grantee",
        extra_projects=[
            ("a", "Alpha", "/tmp/a", "alpha"),
        ],
        kg_bindings={
            "primary": "G_KnowledgeGraph",
            "shared": "VibeCodedOrchestrator_KnowledgeGraph",
            "archive": "G_Development",
        },
        kg_access=[
            ("Foo_KnowledgeGraph", "read"),  # peer — would normally land in env
        ],
        codegraph_access=[
            ("a", "read"),  # peer slug — would normally land in env
        ],
    )

    keys = project_env_template_from_db("grantee", db_path=db)
    assert "VCT_KG_ACCESS_LIST" not in keys
    assert "VCT_CODE_GRAPH_ACCESS_LIST" not in keys


def test_from_db_excludes_orchestrator_root_even_when_passed(
    tmp_path: Path,
) -> None:
    """``orchestrator_root`` is forwarded for CLI symmetry but the
    resulting VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR keys are
    stripped from the template subset."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    orch = tmp_path / "vco-clone"
    orch.mkdir()
    _make_launcher_db(
        db, project_id="x", project_name="X", project_folder=str(proj)
    )
    keys = project_env_template_from_db("x", db_path=db, orchestrator_root=orch)
    assert "VCT_ORCHESTRATOR_ROOT" not in keys
    assert "VCT_INFRASTRUCTURE_DIR" not in keys


def test_from_db_preserves_subset_ordering(tmp_path: Path) -> None:
    """The returned dict iterates in the documented canonical order
    (identity → KG → flags → embedding → services). Insertion order is
    what makes the managed-block render deterministic."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="x", project_name="X", project_folder=str(proj))
    keys = project_env_template_from_db("x", db_path=db)
    order = list(keys.keys())
    # PROJECT_NAME comes before KG_COLLECTION (identity before KG).
    assert order.index("PROJECT_NAME") < order.index("KG_COLLECTION")
    # WEAVIATE_URL comes after the flag block.
    assert order.index("SHARED_KG_WRITE_DISABLED") < order.index("WEAVIATE_URL")


def test_from_db_project_not_found(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="real", project_folder=str(proj))
    with pytest.raises(ProjectNotFound):
        project_env_template_from_db("ghost", db_path=db)


def test_from_db_missing_db_file(tmp_path: Path) -> None:
    with pytest.raises(DbUnreachable):
        project_env_template_from_db("x", db_path=tmp_path / "no-such.db")


# ─── apply_env_template tests ───────────────────────────────────────────


def _keys() -> dict[str, str]:
    """Build a representative template map for writer tests."""
    return {
        "PROJECT_NAME": "TestProj",
        "KG_COLLECTION": "TestKG",
        "DEVELOPMENT_COLLECTION": "TestDev",
        "WEAVIATE_URL": "http://localhost:8081",
        "OLLAMA_URL": "http://localhost:11435",
    }


def test_apply_creates_env_fresh(tmp_path: Path) -> None:
    """No existing .env → fresh file with just the managed block."""
    report = apply_env_template(_keys(), project_folder=tmp_path)
    env_path = tmp_path / ".env"
    assert env_path.exists()
    text = env_path.read_text()
    assert text.startswith(ENV_TEMPLATE_BEGIN + "\n")
    assert text.endswith(ENV_TEMPLATE_END + "\n")
    assert "KG_COLLECTION=TestKG" in text
    assert "PROJECT_NAME=TestProj" in text
    # Audit report.
    assert "env" in report
    assert "KG_COLLECTION" in report["env"]
    assert report["env"] == sorted(report["env"])


def test_apply_idempotent_twice_byte_identical(tmp_path: Path) -> None:
    """Two applies in a row produce byte-identical output."""
    apply_env_template(_keys(), project_folder=tmp_path)
    first = (tmp_path / ".env").read_bytes()
    apply_env_template(_keys(), project_folder=tmp_path)
    second = (tmp_path / ".env").read_bytes()
    assert first == second


def test_apply_preserves_user_lines_outside_markers(tmp_path: Path) -> None:
    """Lines outside the BEGIN/END markers are preserved byte-for-byte."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# user header — keep this\n"
        "MY_USER_OVERRIDE=custom-value\n"
        f"{ENV_TEMPLATE_BEGIN}\n"
        "# stale managed content\n"
        "KG_COLLECTION=OldStale\n"
        f"{ENV_TEMPLATE_END}\n"
        "# user trailer — also keep this\n"
        "ANOTHER_USER_KEY=hello\n"
    )

    apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    # User content above markers preserved verbatim.
    assert text.startswith("# user header — keep this\n")
    assert "MY_USER_OVERRIDE=custom-value" in text
    # Managed block replaced wholesale.
    assert "KG_COLLECTION=TestKG" in text
    assert "OldStale" not in text
    # User trailer preserved.
    assert "# user trailer — also keep this" in text
    assert "ANOTHER_USER_KEY=hello" in text


def test_apply_replaces_marker_block_wholesale(tmp_path: Path) -> None:
    """Adding extra junk inside the managed block: it gets blown away
    on apply. (That's the contract — users must edit outside markers.)"""
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{ENV_TEMPLATE_BEGIN}\n"
        "# I added this comment manually — it WILL be removed\n"
        "USER_ATTEMPTED_KEY=will-vanish\n"
        f"{ENV_TEMPLATE_END}\n"
    )

    apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()
    assert "USER_ATTEMPTED_KEY" not in text
    assert "I added this comment manually" not in text


def test_apply_handles_missing_end_marker(tmp_path: Path) -> None:
    """A truncated managed block (BEGIN present, END missing — e.g. from
    a crashed half-write) is replaced wholesale on the next apply."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# preserved header\n"
        f"{ENV_TEMPLATE_BEGIN}\n"
        "KG_COLLECTION=CrashedHalfWrite\n"
        # No END marker, no trailer.
    )

    apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()
    assert "# preserved header" in text
    assert "KG_COLLECTION=TestKG" in text
    assert "CrashedHalfWrite" not in text
    # END marker present now (recovery completed).
    assert ENV_TEMPLATE_END in text


def test_apply_appends_managed_block_to_legacy_env(tmp_path: Path) -> None:
    """An existing .env WITHOUT the BEGIN marker (legacy append-only
    format from _ensure_env_template / ensure_project_env_template):
    the managed block is APPENDED at EOF. User content fully preserved.
    The next apply will then in-place replace because BEGIN is present."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# vibecoded-orchestrator per-project .env (legacy)\n"
        "KG_COLLECTION=LegacyValue\n"
        "PROJECT_NAME=LegacyName\n"
        "# added by vco 2026-04-28: appended missing canonical keys\n"
        "WEAVIATE_URL=http://localhost:8081\n"
    )

    apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    # All legacy lines preserved.
    assert "KG_COLLECTION=LegacyValue" in text
    assert "PROJECT_NAME=LegacyName" in text
    assert "# added by vco 2026-04-28" in text
    # Managed block now present at EOF.
    assert ENV_TEMPLATE_BEGIN in text
    assert text.rstrip().endswith(ENV_TEMPLATE_END)
    # Managed block carries the launcher's resolved KG_COLLECTION.
    # (User's LegacyValue line earlier in the file still wins under
    # shell-source last-wins semantics — but the managed block IS
    # rendered correctly.)
    managed = text[text.find(ENV_TEMPLATE_BEGIN) :]
    assert "KG_COLLECTION=TestKG" in managed


def test_apply_legacy_then_idempotent(tmp_path: Path) -> None:
    """After the first apply against a legacy file, the second apply
    in-place replaces (no double-block accumulation)."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# legacy file\n"
        "KG_COLLECTION=LegacyValue\n"
    )

    apply_env_template(_keys(), project_folder=tmp_path)
    first = env_path.read_text()

    apply_env_template(_keys(), project_folder=tmp_path)
    second = env_path.read_text()

    assert first == second, "second apply must not double-render"
    # Exactly one BEGIN marker (not two).
    assert second.count(ENV_TEMPLATE_BEGIN) == 1
    assert second.count(ENV_TEMPLATE_END) == 1


def test_apply_appends_trailing_newline_to_file_without_one(
    tmp_path: Path,
) -> None:
    """A legacy .env that doesn't end with a newline gets a separator
    newline before the appended managed block — no glueing."""
    env_path = tmp_path / ".env"
    # Note: explicit no trailing newline.
    env_path.write_bytes(b"KG_COLLECTION=LegacyValue")

    apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()
    # The legacy line is on its own line, not concatenated with the marker.
    assert "KG_COLLECTION=LegacyValue\n" in text
    assert (
        "KG_COLLECTION=LegacyValue" + ENV_TEMPLATE_BEGIN not in text
    ), "managed block must not glue onto last legacy line"


def test_apply_atomic_no_tempfile_leak(tmp_path: Path) -> None:
    """After a successful apply, no .tmp files remain in the project folder."""
    apply_env_template(_keys(), project_folder=tmp_path)
    stragglers = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob("*.tmp*"))
    assert not stragglers, f"tempfile leak: {stragglers}"


def test_apply_uses_lf_line_endings(tmp_path: Path) -> None:
    """Written content uses LF, even though Python's text mode normalises
    on output (the writer explicitly sets newline='\\n')."""
    apply_env_template(_keys(), project_folder=tmp_path)
    raw = (tmp_path / ".env").read_bytes()
    assert b"\r\n" not in raw, "CRLF line endings detected in .env"
    assert b"\n" in raw  # sanity: there ARE newlines


def test_apply_with_empty_keys_renders_marker_pair_only(tmp_path: Path) -> None:
    """An empty key map still emits the markers — the boundary IS the
    semantic, not the content."""
    report = apply_env_template({}, project_folder=tmp_path)
    text = (tmp_path / ".env").read_text()
    assert ENV_TEMPLATE_BEGIN in text
    assert ENV_TEMPLATE_END in text
    assert report["env"] == []
    # No KEY=VALUE lines between markers.
    begin = text.find(ENV_TEMPLATE_BEGIN)
    end = text.find(ENV_TEMPLATE_END)
    between = text[begin + len(ENV_TEMPLATE_BEGIN) : end].strip()
    assert between == ""


def test_apply_renders_forensic_comment_above_each_key(tmp_path: Path) -> None:
    """Each KEY=VALUE line is preceded by a `# added by vco — KEY=VALUE`
    comment for forensic value (user can audit where the value came from)."""
    apply_env_template({"KG_COLLECTION": "TestKG"}, project_folder=tmp_path)
    text = (tmp_path / ".env").read_text()
    assert "# added by vco — KG_COLLECTION=TestKG" in text
    assert "KG_COLLECTION=TestKG" in text


def test_apply_creates_parent_dirs(tmp_path: Path) -> None:
    """project_folder is mkdir'd if missing — supports the "the launcher
    created the DB row but not the folder yet" race window."""
    new_folder = tmp_path / "freshly-created-project"
    assert not new_folder.exists()
    apply_env_template(_keys(), project_folder=new_folder)
    assert (new_folder / ".env").is_file()


# ─── CLI tests ──────────────────────────────────────────────────────────


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m vco_lib.env_template`` and capture output."""
    cmd = [sys.executable, "-m", "vco_lib.env_template", *args]
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    repo_root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_cli_list_keys_json() -> None:
    result = _run_cli("list-keys", "--json")
    assert result.returncode == 0, result.stderr
    keys = json.loads(result.stdout)
    assert "KG_COLLECTION" in keys
    assert "PROJECT_NAME" in keys
    # Sorted output for deterministic auditing.
    assert keys == sorted(keys)
    # The CLI returns the SUBSET, not the full Phase 0.B set.
    assert "VCT_KG_ACCESS_LIST" not in keys
    assert "GITHUB_TOKEN" not in keys


def test_cli_list_keys_plain() -> None:
    result = _run_cli("list-keys")
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert "KG_COLLECTION" in lines


def test_cli_from_db_happy(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="x", project_name="X", project_folder=str(proj))
    result = _run_cli("from-db", "--project-id", "x", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["project_id"] == "x"
    template = out["canonical_env_template"]
    assert template["KG_COLLECTION"] == "X_KnowledgeGraph"
    # Subset enforced.
    assert "VCT_KG_ACCESS_LIST" not in template


def test_cli_from_db_project_not_found_exits_2(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="real", project_folder=str(proj))
    result = _run_cli(
        "from-db", "--project-id", "ghost", "--db-path", str(db)
    )
    assert result.returncode == 2
    err = json.loads(result.stderr)
    assert err["error"] == "project_not_found"


def test_cli_from_db_missing_db_exits_3(tmp_path: Path) -> None:
    result = _run_cli(
        "from-db", "--project-id", "x", "--db-path", str(tmp_path / "no.db")
    )
    assert result.returncode == 3
    err = json.loads(result.stderr)
    assert err["error"] == "db_unreachable"


def test_cli_apply_writes_env(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="x", project_name="X", project_folder=str(proj))
    result = _run_cli(
        "apply",
        "--project-id", "x",
        "--project-folder", str(proj),
        "--db-path", str(db),
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert "env" in out["report"]
    # File actually written.
    env_path = proj / ".env"
    assert env_path.exists()
    text = env_path.read_text()
    assert "KG_COLLECTION=X_KnowledgeGraph" in text
    assert ENV_TEMPLATE_BEGIN in text


def test_cli_apply_project_not_found_exits_2(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(db, project_id="real", project_folder=str(proj))
    result = _run_cli(
        "apply",
        "--project-id", "ghost",
        "--project-folder", str(proj),
        "--db-path", str(db),
    )
    assert result.returncode == 2
    err = json.loads(result.stderr)
    assert err["error"] == "project_not_found"
    # And no .env was written.
    assert not (proj / ".env").exists()


def test_cli_apply_missing_db_exits_3(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    result = _run_cli(
        "apply",
        "--project-id", "x",
        "--project-folder", str(proj),
        "--db-path", str(tmp_path / "no.db"),
    )
    assert result.returncode == 3
    err = json.loads(result.stderr)
    assert err["error"] == "db_unreachable"
