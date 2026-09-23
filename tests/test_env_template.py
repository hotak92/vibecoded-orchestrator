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
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import add_kg_binding, make_launcher_db
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
    """Build a launcher.db with the REAL launcher schema, seeded for the
    env-template resolver.

    Same keyword shape as ``tests/test_config_projection.py::_make_launcher_db``
    and still a separate body — but the SCHEMA is no longer restated here. It
    comes from ``tests.common.launcher_db_fixture``, which applies the shipped
    ``launcher/src-tauri/.../migrations/*.sql`` verbatim (v0.2.92 §3.4).

    On "no cross-test-file fixture imports": the point of that rule was that
    this file must not depend on ANOTHER TEST MODULE's private helper —
    importing ``tests/test_config_projection.py`` would drag its collection
    and its fixtures in, and either file's edits could break the other.
    ``tests/common/`` is a shared helper PACKAGE, not a test module: pytest
    never collects it, it defines no tests, and several test files already
    import from it. So this file remains independently runnable — the rule
    is satisfied, not bent.
    """
    projects: list[dict[str, object]] = [{
        "project_id": project_id,
        "name": project_name,
        "folder_path": project_folder,
        "slug": project_slug,
    }]
    projects.extend(
        {"project_id": pid, "name": name, "folder_path": folder, "slug": slug}
        for pid, name, folder, slug in (extra_projects or [])
    )
    make_launcher_db(
        db_path,
        projects=projects,
        module_settings=module_settings or [],
        kg_access=[
            (project_id, coll, level) for coll, level in (kg_access or [])
        ],
        codegraph_access=[
            (grantor, project_id, level)
            for grantor, level in (codegraph_access or [])
        ],
    )
    for role, coll in (kg_bindings or {}).items():
        add_kg_binding(db_path, project_id, role, coll)


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
    """An existing .env WITHOUT the BEGIN marker: the managed block is
    APPENDED at EOF. v0.2.97: a key the USER assigns outside the markers
    is left out of the block (the user's line is the one assignment), and
    the retired append writer's line for a key the block renders is
    migrated into the block — never two assignments of one key."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# vibecoded-orchestrator per-project .env (legacy)\n"
        "KG_COLLECTION=LegacyValue\n"
        "PROJECT_NAME=LegacyName\n"
        "\n"
        "# added by vco 2026-04-28: appended missing canonical keys\n"
        "WEAVIATE_URL=http://localhost:8081\n"
    )

    report = apply_env_template(_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    # User lines preserved byte-for-byte, and they stay the only assignment.
    assert text.startswith(
        "# vibecoded-orchestrator per-project .env (legacy)\n"
        "KG_COLLECTION=LegacyValue\n"
        "PROJECT_NAME=LegacyName\n"
    )
    assert _assignments(text, "KG_COLLECTION") == ["LegacyValue"]
    assert _assignments(text, "PROJECT_NAME") == ["LegacyName"]
    # The legacy section's only line moved into the block (with its header).
    assert "# added by vco 2026-04-28" not in text
    assert _assignments(text, "WEAVIATE_URL") == ["http://localhost:8081"]
    assert ENV_TEMPLATE_BEGIN in text
    assert text.rstrip().endswith(ENV_TEMPLATE_END)
    managed = text[text.find(ENV_TEMPLATE_BEGIN):]
    assert "WEAVIATE_URL=http://localhost:8081" in managed
    assert "KG_COLLECTION" not in managed
    assert report["user_set"] == ["KG_COLLECTION", "PROJECT_NAME"]
    assert report["migrated"] == ["WEAVIATE_URL"]
    assert report["action"] == ["updated"]


def _assignments(text: str, key: str) -> list[str]:
    """Every ACTIVE value of ``key`` in ``text``, in file order."""
    out = []
    for line in text.splitlines():
        body = line.strip()
        if body.startswith("export "):
            body = body[len("export "):].lstrip()
        if body.startswith(f"{key}="):
            out.append(body.split("=", 1)[1])
    return out


# ─── v0.2.97: legacy VCO-authored lines migrate into ONE set ─────────────

# What the retired Rust ``ensure_project_env_template`` appended to an
# existing .env, and what ``install.py --update`` appended (field shapes).
_RUST_APPEND_BLOCK = (
    "\n"
    "# added by vco 2026-05-06: appended missing canonical keys\n"
    "# CODE_EMBED_URL=\n"
    "KG_COLLECTION=Acme_KnowledgeGraph\n"
    "PROJECT_NAME=<project>\n"
    "# ANTHROPIC_API_KEY=\n"
    "# GITHUB_TOKEN=\n"
    "# RL_PROJECT_ROOT=<project_root>\n"
)
_UPDATE_BLOCK = (
    "\n"
    "# --- Added by install.py --update on 2026-05-28 ---\n"
    "# Added by install.py --update on 2026-05-28\n"
    "CODE_EMBED_PORT=11440\n"
    "# Added by install.py --update on 2026-05-28\n"
    "UNKNOWN_FUTURE_KEY=keep\n"
)
# The retired Rust fresh-file template's two managed sections.
_RUST_TEMPLATE = (
    "# vibecoded-orchestrator per-project .env\n"
    "# Edit values to override defaults. Empty / commented lines are\n"
    "# treated as \"use default\". Created by vco 2026-05-06.\n"
    "\n"
    "# === Service URLs (launcher-resolved; edit only if you know what you're doing) ===\n"
    "# WEAVIATE_URL=http://localhost:8081\n"
    "# WEAVIATE_PORT=8081\n"
    "# OLLAMA_URL=http://localhost:11435\n"
    "# OLLAMA_PORT=11435\n"
    "# CODE_EMBED_URL=http://localhost:11440\n"
    "\n"
    "# === Per-project Weaviate collections ===\n"
    "# Resolved by the launcher when the project is registered. Don't\n"
    "# edit unless you know what you're doing.\n"
    "KG_COLLECTION=Acme_KnowledgeGraph\n"
    "SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph\n"
    "DEVELOPMENT_COLLECTION=Acme_Development\n"
    "PROJECT_NAME=Acme\n"
    "ACTIVE_EMBEDDING=qwen3\n"
    "\n"
    "# === LLM API keys (optional) ===\n"
    "# ANTHROPIC_API_KEY=\n"
    "# OPENAI_API_KEY=\n"
)


def _acme_keys() -> dict[str, str]:
    return {
        "PROJECT_NAME": "Acme",
        "CODE_GRAPH_PROJECT": "Acme",
        "KG_COLLECTION": "Acme_KnowledgeGraph",
        "DEVELOPMENT_COLLECTION": "Acme_Development",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        "ACTIVE_EMBEDDING": "arctic",
        "WEAVIATE_URL": "http://localhost:8081",
        "WEAVIATE_PORT": "8081",
        "OLLAMA_URL": "http://localhost:11435",
        "OLLAMA_PORT": "11435",
        "CODE_EMBED_URL": "http://localhost:11440",
        "CODE_EMBED_PORT": "11440",
    }


def _assert_one_set(text: str, keys: dict[str, str]) -> None:
    for key in keys:
        assert len(_assignments(text, key)) == 1, (key, text)


def test_legacy_append_block_migrates_to_one_set(tmp_path: Path) -> None:
    """The Rust append block: its lines for managed keys (and the bogus
    ``PROJECT_NAME=<project>``) move into the block; its placeholders for
    keys the block never carries stay, under their header; user lines are
    byte-identical and keep their own assignment."""
    env_path = tmp_path / ".env"
    user = "MY_TOKEN=abc\nWEAVIATE_URL=http://remote:8081\n"
    env_path.write_text(user + _RUST_APPEND_BLOCK)

    report = apply_env_template(_acme_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    assert text.startswith(user)
    _assert_one_set(text, _acme_keys())
    assert _assignments(text, "WEAVIATE_URL") == ["http://remote:8081"]
    assert _assignments(text, "PROJECT_NAME") == ["Acme"]
    assert "<project>\n" not in text
    # Placeholders the block does not own survive with their header.
    assert (
        "# added by vco 2026-05-06: appended missing canonical keys\n"
        "# ANTHROPIC_API_KEY=\n"
        "# GITHUB_TOKEN=\n"
        "# RL_PROJECT_ROOT=<project_root>\n"
    ) in text
    assert "# CODE_EMBED_URL=\n" not in text
    assert report["migrated"] == ["CODE_EMBED_URL", "KG_COLLECTION", "PROJECT_NAME"]
    assert report["user_set"] == ["WEAVIATE_URL"]


def test_legacy_rust_template_sections_migrate(tmp_path: Path) -> None:
    """A .env born from the retired Rust template: both managed sections
    (header, notes, commented and active lines) are replaced by the block;
    the banner and the optional-keys section stay."""
    env_path = tmp_path / ".env"
    env_path.write_text(_RUST_TEMPLATE)

    apply_env_template(_acme_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    _assert_one_set(text, _acme_keys())
    assert _assignments(text, "ACTIVE_EMBEDDING") == ["arctic"]
    assert "# === Service URLs" not in text
    assert "# === Per-project Weaviate collections ===" not in text
    assert "Resolved by the launcher" not in text
    assert "# WEAVIATE_URL=" not in text
    assert text.startswith("# vibecoded-orchestrator per-project .env\n")
    assert "# === LLM API keys (optional) ===\n# ANTHROPIC_API_KEY=\n" in text
    # No gap left where the sections were.
    assert "\n\n\n" not in text


def test_legacy_update_block_migrates_owned_pairs_only(tmp_path: Path) -> None:
    """``install.py --update``'s annotated pairs: an owned key's pair is
    removed; an unknown key's pair (and so the header) stays."""
    env_path = tmp_path / ".env"
    env_path.write_text("KG_COLLECTION=Acme_KnowledgeGraph\n" + _UPDATE_BLOCK)

    apply_env_template(_acme_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    _assert_one_set(text, _acme_keys())
    assert (
        "# --- Added by install.py --update on 2026-05-28 ---\n"
        "# Added by install.py --update on 2026-05-28\n"
        "UNKNOWN_FUTURE_KEY=keep\n"
    ) in text
    assert _assignments(text, "CODE_EMBED_PORT") == ["11440"]
    assert "# Added by install.py --update on 2026-05-28\nCODE_EMBED_PORT" not in text


def test_user_line_under_legacy_block_is_not_migrated(tmp_path: Path) -> None:
    """A user line glued right under a legacy block (``echo … >> .env``)
    breaks the writer's key order, so it is the user's — kept, and it keeps
    the key out of the block."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# added by vco 2026-05-06: appended missing canonical keys\n"
        "PROJECT_NAME=<project>\n"
        "# GITHUB_TOKEN=\n"
        "KG_COLLECTION=MyCustom_KG\n"
    )

    apply_env_template(_acme_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    assert _assignments(text, "KG_COLLECTION") == ["MyCustom_KG"]
    _assert_one_set(text, _acme_keys())


def test_legacy_file_migration_is_idempotent(tmp_path: Path) -> None:
    """Second apply over a migrated legacy file: byte-identical, no write."""
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n" + _RUST_TEMPLATE + _RUST_APPEND_BLOCK + _UPDATE_BLOCK)
    apply_env_template(_acme_keys(), project_folder=tmp_path)
    first = env_path.read_bytes()
    mtime = env_path.stat().st_mtime_ns

    report = apply_env_template(_acme_keys(), project_folder=tmp_path)

    assert env_path.read_bytes() == first
    assert env_path.stat().st_mtime_ns == mtime
    assert report["action"] == ["unchanged"]
    assert report["migrated"] == []


def test_all_keys_user_set_appends_no_empty_block(tmp_path: Path) -> None:
    """Leave-alone: a file that already assigns every key gets no block and
    no write (the pre-v0.2.97 reconcile's noop, kept)."""
    env_path = tmp_path / ".env"
    body = "".join(f"export {k}={v}\n" for k, v in _acme_keys().items())
    env_path.write_text(body)
    mtime = env_path.stat().st_mtime_ns

    report = apply_env_template(_acme_keys(), project_folder=tmp_path)

    assert env_path.read_text() == body
    assert env_path.stat().st_mtime_ns == mtime
    assert report["env"] == []
    assert report["action"] == ["unchanged"]


def test_block_drops_a_key_the_user_later_sets(tmp_path: Path) -> None:
    """Act: once the user assigns a key outside the block, the block stops
    rendering it — the user's value becomes the only assignment."""
    apply_env_template(_acme_keys(), project_folder=tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("KG_COLLECTION=Override_KG\n" + env_path.read_text())

    report = apply_env_template(_acme_keys(), project_folder=tmp_path)
    text = env_path.read_text()

    assert _assignments(text, "KG_COLLECTION") == ["Override_KG"]
    assert report["user_set"] == ["KG_COLLECTION"]


def test_commented_line_does_not_suppress_the_managed_value(tmp_path: Path) -> None:
    """A ``# KEY=`` line assigns nothing, so the block still renders KEY."""
    env_path = tmp_path / ".env"
    env_path.write_text("# KG_COLLECTION=\n")
    apply_env_template(_acme_keys(), project_folder=tmp_path)
    assert _assignments(env_path.read_text(), "KG_COLLECTION") == ["Acme_KnowledgeGraph"]


def test_unreadable_existing_env_is_an_error_not_a_rewrite(tmp_path: Path) -> None:
    """A .env that exists but cannot be read must never be replaced by a
    fresh block (pre-v0.2.97 treated it as absent). Non-UTF-8 bytes stand
    in for the unreadable file; they must survive byte-for-byte."""
    env_path = tmp_path / ".env"
    raw = b"SECRET=\xff\xfe not utf-8\n"
    env_path.write_bytes(raw)
    with pytest.raises(UnicodeDecodeError):
        apply_env_template(_acme_keys(), project_folder=tmp_path)
    assert env_path.read_bytes() == raw


def test_scaffold_only_on_creation(tmp_path: Path) -> None:
    """The scaffold starts a NEW file; an existing file never gets it."""
    scaffold = "# header\n# OPTIONAL=\n\n"
    apply_env_template(_keys(), project_folder=tmp_path, scaffold=scaffold)
    text = (tmp_path / ".env").read_text()
    assert text.startswith(scaffold + ENV_TEMPLATE_BEGIN)

    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("USER=1\n")
    apply_env_template(_keys(), project_folder=other, scaffold=scaffold)
    assert "# OPTIONAL=" not in (other / ".env").read_text()


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
    # child_env() puts the repo root FIRST on PYTHONPATH so the child imports
    # the CHECKOUT's vco_lib, never a stale site-packages copy (§3.16).
    env = child_env(**(env_extra or {}))
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
