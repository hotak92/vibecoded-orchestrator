# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 WP-2 (P2, D2) — env-repoint audit rows.

Once the one rule (D1) lands, the next ``apply_project_env`` on an existing
install OVERWRITES a stale name-derived DEVELOPMENT_COLLECTION /
KG_COLLECTION with the binding-paired name — that IS the migration (a
pointer fix, no data movement). D2 makes it VISIBLE: a
``dev_collection_env_repointed`` row is appended to
``.claude/logs/auto-resolutions.jsonl`` when (and only when) the writer
CHANGED an existing on-disk value.

Coverage:
  * FAIL-WITHOUT-FIX PIN: stale ``VibeCodedOrchestrator_Development`` on
    disk + binding ``VCODev_KnowledgeGraph`` ⇒ BOTH surfaces repoint to
    ``VCODev_Development`` AND a parseable JSONL old→new row is appended.
  * Leave-alone: value already binding-paired ⇒ no rewrite, no JSONL row.
  * Non-``_KnowledgeGraph`` custom primary ⇒ slug-fallback (== hub).
  * User rename with the binding updated ⇒ follows the binding.
  * NON-ROOT first-class parity (amendment A3): the repoint pin runs once
    root-shaped and once against a non-root fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from vco_lib.config_projection import (
    apply_project_env,
    project_env_from_db,
)

# Reuse the schema-faithful launcher.db fixture builder (one home).
from tests.test_config_projection import _make_launcher_db

_AUTO_RESOLUTIONS_REL = Path(".claude") / "logs" / "auto-resolutions.jsonl"
_REPOINT_CONDITION_ID = "dev_collection_env_repointed"


# ─── helpers ───


def _write_stale_settings_json(
    project_root: Path, *, kg: str, dev: str, extra: dict[str, str] | None = None
) -> Path:
    """Seed a `.claude/settings.json` carrying a (possibly stale) env block."""
    path = project_root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {"KG_COLLECTION": kg, "DEVELOPMENT_COLLECTION": dev}
    if extra:
        env.update(extra)
    path.write_text(json.dumps({"env": env}, indent=2), encoding="utf-8")
    return path


def _write_stale_claude_env(project_root: Path, *, kg: str, dev: str) -> Path:
    """Seed a `.claude/env` managed block carrying a (possibly stale) env."""
    path = project_root / ".claude" / "env"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# vco-managed-begin\n"
        f'export KG_COLLECTION="{kg}"\n'
        f'export DEVELOPMENT_COLLECTION="{dev}"\n'
        "# vco-managed-end\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def _read_jsonl_rows(project_root: Path) -> list[dict]:
    path = project_root / _AUTO_RESOLUTIONS_REL
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _settings_env(project_root: Path) -> dict[str, str]:
    path = project_root / ".claude" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))["env"]


def _claude_env_value(project_root: Path, key: str) -> str | None:
    path = project_root / ".claude" / "env"
    prefix = f'export {key}="'
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(prefix) and s.endswith('"'):
            return s[len(prefix): -1]
    return None


def _projected_bundle(db: Path, project_id: str) -> dict:
    """Run the real projection to get a binding-paired canonical_env."""
    return project_env_from_db(project_id, db_path=db)


# ─── FAIL-WITHOUT-FIX PIN: repoint both surfaces + JSONL row ───


def _run_repoint_case(
    tmp_path: Path,
    *,
    project_id: str,
    project_name: str,
    project_slug: str,
    primary_binding: str,
    folder_name: str,
) -> Path:
    """Shared body: seed stale surfaces name-derived from the display name,
    project the binding-paired env, apply, return the project folder.
    """
    db = tmp_path / f"{project_id}.db"
    proj = tmp_path / folder_name
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id=project_id,
        project_name=project_name,
        project_folder=str(proj),
        project_slug=project_slug,
        kg_bindings={"primary": primary_binding},
    )
    # Stale on-disk values: name-derived DEV that does NOT match the binding.
    _write_stale_settings_json(
        proj,
        kg=primary_binding,
        dev="VibeCodedOrchestrator_Development",
    )
    _write_stale_claude_env(
        proj,
        kg=primary_binding,
        dev="VibeCodedOrchestrator_Development",
    )
    bundle = _projected_bundle(db, project_id)
    # Sanity: the projection resolved the binding-paired dev name.
    assert bundle["canonical_env"]["DEVELOPMENT_COLLECTION"] == "VCODev_Development"
    apply_project_env(bundle, surfaces=["claude_settings_json", "claude_env"])
    return proj


def test_repoint_root_shaped_rewrites_both_surfaces_and_logs(tmp_path: Path) -> None:
    """PIN (P2, root-shaped): stale VibeCodedOrchestrator_Development +
    binding VCODev_KnowledgeGraph ⇒ BOTH surfaces become
    VCODev_Development AND a parseable JSONL old→new row is appended.

    Fails on the pre-fix tree: without D1 the projection name-derives dev
    from the display name (no repoint needed → the pin can't observe the
    binding-paired value); without D2 no JSONL row is written even when D1
    changes the value.
    """
    proj = _run_repoint_case(
        tmp_path,
        project_id="root",
        project_name="VibeCoded Orchestrator",
        project_slug="vibecoded-orchestrator",
        primary_binding="VCODev_KnowledgeGraph",
        folder_name="vco-root",
    )
    # Both surfaces repointed to the binding-paired dev name.
    assert _settings_env(proj)["DEVELOPMENT_COLLECTION"] == "VCODev_Development"
    assert _claude_env_value(proj, "DEVELOPMENT_COLLECTION") == "VCODev_Development"

    # A parseable JSONL row records the repoint old→new.
    rows = [
        r for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
    ]
    assert rows, "a dev_collection_env_repointed row must be appended"
    dev_rows = [r for r in rows if r["detail"].startswith("DEVELOPMENT_COLLECTION:")]
    assert dev_rows, "the repoint row must name the DEVELOPMENT_COLLECTION change"
    r = dev_rows[0]
    assert r["action"] == "repointed"
    assert (
        r["detail"]
        == "DEVELOPMENT_COLLECTION: VibeCodedOrchestrator_Development → VCODev_Development"
    )
    assert "ts" in r  # ISO-8601 stamp present


def test_repoint_non_root_project_identical_behavior(tmp_path: Path) -> None:
    """Amendment A3: the SAME repoint pin against a NON-root project
    (its own folder + binding, not the orchestrator root) — identical
    rewrite + JSONL behavior. Collection naming has no root branch."""
    proj = _run_repoint_case(
        tmp_path,
        project_id="client-001",
        project_name="Client App With Long Display Name",
        project_slug="client-app",
        # Non-root project can equally carry a renamed primary binding whose
        # basename differs from its display name.
        primary_binding="VCODev_KnowledgeGraph",
        folder_name="client-app",
    )
    assert _settings_env(proj)["DEVELOPMENT_COLLECTION"] == "VCODev_Development"
    assert _claude_env_value(proj, "DEVELOPMENT_COLLECTION") == "VCODev_Development"
    rows = [
        r for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
        and r["detail"].startswith("DEVELOPMENT_COLLECTION:")
    ]
    assert len(rows) == 1, "non-root repoint must emit exactly one dev row"


# ─── Leave-alone: already binding-paired ⇒ no rewrite, no JSONL row ───


def test_no_repoint_when_already_binding_paired(tmp_path: Path) -> None:
    """Steady state: on-disk value already == binding-paired name ⇒ the
    writer changes nothing → NO JSONL row (no noise)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "steady"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="steady",
        project_name="VibeCoded Orchestrator",
        project_folder=str(proj),
        project_slug="vibecoded-orchestrator",
        kg_bindings={"primary": "VCODev_KnowledgeGraph"},
    )
    # Already-correct on-disk values.
    _write_stale_settings_json(
        proj, kg="VCODev_KnowledgeGraph", dev="VCODev_Development"
    )
    _write_stale_claude_env(
        proj, kg="VCODev_KnowledgeGraph", dev="VCODev_Development"
    )
    bundle = _projected_bundle(db, "steady")
    apply_project_env(bundle, surfaces=["claude_settings_json", "claude_env"])

    # Values unchanged, and NO repoint row was written.
    assert _settings_env(proj)["DEVELOPMENT_COLLECTION"] == "VCODev_Development"
    rows = [
        r for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
    ]
    assert rows == [], "no repoint row when the value is already correct"


def test_no_repoint_on_first_write_absent_key(tmp_path: Path) -> None:
    """A first write (no prior on-disk value) is NOT a repoint — no row.

    Absent/empty old value ⇒ the writer is establishing the value, not
    changing an existing pointer; the audit stays silent.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "fresh"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="fresh",
        project_name="Fresh Project",
        project_folder=str(proj),
        project_slug="fresh",
        kg_bindings={"primary": "Fresh_KnowledgeGraph"},
    )
    # No pre-existing settings.json / .claude/env — a first write.
    bundle = _projected_bundle(db, "fresh")
    apply_project_env(bundle, surfaces=["claude_settings_json", "claude_env"])
    assert _settings_env(proj)["DEVELOPMENT_COLLECTION"] == "Fresh_Development"
    rows = [
        r for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
    ]
    assert rows == [], "a first write is not a repoint"


# ─── Non-canonical primary ⇒ slug-fallback (== hub) ───


def test_repoint_to_slug_fallback_for_non_canonical_primary(tmp_path: Path) -> None:
    """A custom-rename primary (no ``_KnowledgeGraph``) repoints the stale
    dev to the SLUG-fallback name, byte-matching the hub."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "weird"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="weird",
        project_name="Weird Project",
        project_folder=str(proj),
        project_slug="weirdproject",
        kg_bindings={"primary": "WeirdName_Custom"},
    )
    _write_stale_settings_json(
        proj, kg="WeirdName_Custom", dev="WeirdProject_Development"
    )
    bundle = _projected_bundle(db, "weird")
    apply_project_env(bundle, surfaces=["claude_settings_json"])
    assert _settings_env(proj)["DEVELOPMENT_COLLECTION"] == "Weirdproject_Development"
    rows = [
        r for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
        and r["detail"].startswith("DEVELOPMENT_COLLECTION:")
    ]
    assert len(rows) == 1
    assert (
        rows[0]["detail"]
        == "DEVELOPMENT_COLLECTION: WeirdProject_Development → Weirdproject_Development"
    )


# ─── User rename with the binding updated ⇒ follows the binding ───


def test_repoint_follows_updated_binding_on_rename(tmp_path: Path) -> None:
    """A user rename that updated the primary binding drives the repoint:
    both KG_COLLECTION and DEVELOPMENT_COLLECTION follow the new binding,
    each emitting its own repoint row.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "renamed"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="renamed",
        project_name="New Name",
        project_folder=str(proj),
        project_slug="new-name",
        # Binding already updated to the renamed KG.
        kg_bindings={"primary": "NewName_KnowledgeGraph"},
    )
    # On disk still carries the OLD name.
    _write_stale_settings_json(
        proj, kg="OldName_KnowledgeGraph", dev="OldName_Development"
    )
    bundle = _projected_bundle(db, "renamed")
    apply_project_env(bundle, surfaces=["claude_settings_json"])
    env = _settings_env(proj)
    assert env["KG_COLLECTION"] == "NewName_KnowledgeGraph"
    assert env["DEVELOPMENT_COLLECTION"] == "NewName_Development"
    details = {
        r["detail"]
        for r in _read_jsonl_rows(proj)
        if r.get("condition_id") == _REPOINT_CONDITION_ID
    }
    assert (
        "KG_COLLECTION: OldName_KnowledgeGraph → NewName_KnowledgeGraph" in details
    )
    assert (
        "DEVELOPMENT_COLLECTION: OldName_Development → NewName_Development"
        in details
    )
