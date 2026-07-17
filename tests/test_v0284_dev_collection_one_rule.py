# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 WP-2 (P2, D1) — one collection-naming rule: python realization
+ cross-home structural pins.

The Rust side of the ONE rule
(``vct_launcher_core::collection_naming::resolve_project_collections`` +
``populate()``) is pinned by in-crate ``#[cfg(test)]`` tests in
``collection_naming.rs`` and by the hub's ``config_api`` Decision C tests.
THIS module covers:

  * the FAIL-WITHOUT-FIX PIN (no-name-derivation-when-binding-resolves)
    for the PYTHON surface (``config_projection.project_env_from_db``),
  * the python slug-fallback convergence onto the hub (dev + diagrams),
  * the kg_sync consumer parity (the binding-paired dev name feeds the
    kg-sync env),
  * NON-ROOT first-class parity (amendment A3 — a project folder distinct
    from the orchestrator root behaves identically), and
  * the STRUCTURAL pins that the three homes DELEGATE to the ONE rule and
    that ``config_projection`` has exactly one ``_Development`` derivation
    site.
"""

from __future__ import annotations

from pathlib import Path

from vco_lib.config_projection import (
    _REPOINT_AUDITED_KEYS,
    ProjectNotFound,
    _sanitize_collection_prefix,
    project_env_from_db,
    resolve_collection_names_for_folder,
    resolve_project_collection_names,
)

# Reuse the schema-faithful launcher.db fixture builder from the main
# config_projection suite (one home for the DDL — do not re-hand-roll it).
from tests.test_config_projection import _make_launcher_db

# Repo root = two levels up from this file (tests/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_API_RS = (
    _REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "config_api.rs"
)
_POPULATE_RS = (
    _REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "src"
    / "commands"
    / "project_env_settings.rs"
)
_CONFIG_PROJECTION_PY = _REPO_ROOT / "vco_lib" / "config_projection.py"
_COLLECTION_NAMING_RS = (
    _REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "collection_naming.rs"
)


# ─── FAIL-WITHOUT-FIX PIN: no name-derivation when a binding resolves ───


def test_python_dev_suffix_swaps_from_binding_not_display_name(tmp_path: Path) -> None:
    """PIN (P2): binding ``VCODev_KnowledgeGraph`` + display name
    "VibeCoded Orchestrator" (which name-derives to
    ``VibeCodedOrchestrator_*``) ⇒ DEVELOPMENT_COLLECTION is
    ``VCODev_Development`` (suffix-swap off the BINDING), never
    ``VibeCodedOrchestrator_Development``.

    Fails on the pre-fix tree where the dev line keyed off the dead
    ``role='archive'`` binding and otherwise name-derived from the display
    name — the exact P2 drift that stranded the real docs store.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "vco"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="root",
        project_name="VibeCoded Orchestrator",
        project_folder=str(proj),
        project_slug="vibecoded-orchestrator",
        kg_bindings={"primary": "VCODev_KnowledgeGraph"},
    )
    env = project_env_from_db("root", db_path=db)["canonical_env"]
    assert env["KG_COLLECTION"] == "VCODev_KnowledgeGraph"
    assert env["DEVELOPMENT_COLLECTION"] == "VCODev_Development", (
        "dev must suffix-swap off the resolved binding, NOT name-derive "
        "from the display name (P2 regression was "
        "VibeCodedOrchestrator_Development)"
    )
    assert env["DIAGRAMS_COLLECTION"] == "VCODev_Diagrams"


def test_python_dev_slug_fallback_for_non_canonical_primary(tmp_path: Path) -> None:
    """Non-``_KnowledgeGraph`` primary (custom-rename) ⇒ dev/diagrams fall
    back to ``_sanitize_collection_prefix(slug)_<Suffix>`` — byte-matching
    the hub's ``config_development_collection_falls_back_to_slug_for_non_
    canonical_primary`` (slug "weirdproject" → "Weirdproject_Development").
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "weird"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="p-weird",
        project_name="Weird Project",
        project_folder=str(proj),
        project_slug="weirdproject",
        kg_bindings={"primary": "WeirdName_Custom"},
    )
    env = project_env_from_db("p-weird", db_path=db)["canonical_env"]
    assert env["KG_COLLECTION"] == "WeirdName_Custom"
    assert env["DEVELOPMENT_COLLECTION"] == "Weirdproject_Development"
    assert env["DIAGRAMS_COLLECTION"] == "Weirdproject_Diagrams"


def test_python_dev_canonical_suffix_swap(tmp_path: Path) -> None:
    """Canonical primary (ends ``_KnowledgeGraph``) ⇒ dev/diagrams
    suffix-swap off the basename (the common, byte-unchanged path)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "acme"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="p-acme",
        project_name="Acme",
        project_folder=str(proj),
        project_slug="acme",
        kg_bindings={"primary": "Acme_KnowledgeGraph"},
    )
    env = project_env_from_db("p-acme", db_path=db)["canonical_env"]
    assert env["DEVELOPMENT_COLLECTION"] == "Acme_Development"
    assert env["DIAGRAMS_COLLECTION"] == "Acme_Diagrams"


# ─── NON-ROOT first-class parity (amendment A3) ───


def test_non_root_project_uses_same_one_rule(tmp_path: Path) -> None:
    """Amendment A3: a NON-root project (its own folder, its own binding,
    NOT the orchestrator root) resolves dev/diagrams by the SAME one rule.

    The rule is pure binding+string logic — there is no root-vs-non-root
    branch — so a non-root fixture must behave identically. We assert both
    the canonical suffix-swap and the slug-fallback for a non-root project.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "client-app"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="client-001",
        project_name="Client App",
        project_folder=str(proj),
        project_slug="client-app",
        kg_bindings={"primary": "ClientApp_KnowledgeGraph"},
    )
    env = project_env_from_db("client-001", db_path=db)["canonical_env"]
    assert env["KG_COLLECTION"] == "ClientApp_KnowledgeGraph"
    assert env["DEVELOPMENT_COLLECTION"] == "ClientApp_Development"
    assert env["DIAGRAMS_COLLECTION"] == "ClientApp_Diagrams"

    # And the non-canonical fallback for a non-root, custom-rename project.
    db2 = tmp_path / "launcher2.db"
    proj2 = tmp_path / "client-b"
    proj2.mkdir()
    _make_launcher_db(
        db2,
        project_id="client-002",
        project_name="Client B",
        project_folder=str(proj2),
        project_slug="client-b-portal",
        kg_bindings={"primary": "RenamedKG"},
    )
    env2 = project_env_from_db("client-002", db_path=db2)["canonical_env"]
    assert env2["DEVELOPMENT_COLLECTION"] == "Client_b_portal_Development"


# ─── kg_sync consumer parity ───


def test_kg_sync_consumer_gets_binding_paired_dev(tmp_path: Path) -> None:
    """The dev collection a kg-sync-style consumer would read from the
    projected env is the BINDING-PAIRED name (P2's whole point: the docs
    store `<KG>_Development` is what gets ensured/synced, not a stranded
    name-derived shell).

    Consumer-parity proxy: `project_env_from_db`'s DEVELOPMENT_COLLECTION
    is exactly `<primary-basename>_Development`. `sync_knowledge_graph.
    ensure_dev_collection_exists` consumes this CONFIGURED value verbatim
    (creation-from-config only — pinned by the R3 verify-only note), so a
    binding-paired projection means kg-sync ensures the right collection.
    """
    db = tmp_path / "launcher.db"
    proj = tmp_path / "vcodev"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="vcodev",
        project_name="Some Display Name That Would Name-Derive Differently",
        project_folder=str(proj),
        project_slug="vcodev",
        kg_bindings={"primary": "VCODev_KnowledgeGraph"},
    )
    env = project_env_from_db("vcodev", db_path=db)["canonical_env"]
    # The value kg-sync would receive in its child env.
    assert env["DEVELOPMENT_COLLECTION"] == "VCODev_Development"
    # It is paired with the KG (same basename) — never name-derived from
    # the (very different) display name.
    assert env["KG_COLLECTION"] == "VCODev_KnowledgeGraph"
    assert env["DEVELOPMENT_COLLECTION"].removesuffix("_Development") == (
        env["KG_COLLECTION"].removesuffix("_KnowledgeGraph")
    )


# ─── WP-4 D3 seam: read-only binding-first collection-name resolver ───


def test_resolve_project_collection_names_binding_first(tmp_path: Path) -> None:
    """The read-only WP-4 seam `resolve_project_collection_names` returns
    binding-first KG/dev/diagrams by the SAME one rule (shared derivation
    helper) — WITHOUT building a full env bundle. Covers the canonical
    suffix-swap AND the name-derived last resort (no binding)."""
    db = tmp_path / "launcher.db"
    proj = tmp_path / "p"
    proj.mkdir()
    _make_launcher_db(
        db,
        project_id="p1",
        project_name="VibeCoded Orchestrator",
        project_folder=str(proj),
        project_slug="vibecoded-orchestrator",
        kg_bindings={"primary": "VCODev_KnowledgeGraph"},
    )
    names = resolve_project_collection_names("p1", db_path=db)
    assert names["kg_collection"] == "VCODev_KnowledgeGraph"
    assert names["development_collection"] == "VCODev_Development"
    assert names["diagrams_collection"] == "VCODev_Diagrams"

    # No-binding last resort: a project row with NO primary binding →
    # name-derived (matches config_projection + populate).
    db2 = tmp_path / "launcher2.db"
    proj2 = tmp_path / "p2"
    proj2.mkdir()
    _make_launcher_db(
        db2,
        project_id="p2",
        project_name="Fresh Create",
        project_folder=str(proj2),
        project_slug="fresh-create",
        kg_bindings={},  # no bindings — fresh create
    )
    names2 = resolve_project_collection_names("p2", db_path=db2)
    assert names2["kg_collection"] == "FreshCreate_KnowledgeGraph"
    assert names2["development_collection"] == "FreshCreate_Development"


def test_resolve_collection_names_for_folder_matches_canonicalized(
    tmp_path: Path,
) -> None:
    """Folder-shaped seam: a symlinked / trailing-slash folder still matches
    the registered folder_path (both sides canonicalized) and returns the
    binding-first names."""
    db = tmp_path / "launcher.db"
    real = tmp_path / "realproj"
    real.mkdir()
    _make_launcher_db(
        db,
        project_id="p1",
        project_name="Client App",
        project_folder=str(real),
        project_slug="client-app",
        kg_bindings={"primary": "ClientApp_KnowledgeGraph"},
    )
    # Match via a SYMLINK to the registered folder.
    link = tmp_path / "linkproj"
    link.symlink_to(real)
    names = resolve_collection_names_for_folder(link, db_path=db)
    assert names["kg_collection"] == "ClientApp_KnowledgeGraph"
    assert names["development_collection"] == "ClientApp_Development"

    # Match via a trailing-slash / non-normalized path to the same folder.
    weird = Path(str(real) + "/./")
    names_b = resolve_collection_names_for_folder(weird, db_path=db)
    assert names_b["development_collection"] == "ClientApp_Development"


def test_resolve_collection_names_for_folder_unregistered_raises(
    tmp_path: Path,
) -> None:
    """Leave-alone: an UNREGISTERED folder (standalone CLI bootstrap on a
    folder the launcher never saw) raises ProjectNotFound — the caller's
    signal to fall back to name-derivation (D1 last resort), never fatal."""
    db = tmp_path / "launcher.db"
    real = tmp_path / "registered"
    real.mkdir()
    _make_launcher_db(
        db,
        project_id="p1",
        project_name="Registered",
        project_folder=str(real),
        project_slug="registered",
        kg_bindings={"primary": "Registered_KnowledgeGraph"},
    )
    unregistered = tmp_path / "never-seen"
    unregistered.mkdir()
    try:
        resolve_collection_names_for_folder(unregistered, db_path=db)
        raise AssertionError("expected ProjectNotFound for unregistered folder")
    except ProjectNotFound:
        pass


# ─── D2 audit scope: only KG_COLLECTION / DEVELOPMENT_COLLECTION ───


def test_repoint_audit_scope_is_kg_and_dev_only() -> None:
    """The repoint audit must fire ONLY for KG_COLLECTION and
    DEVELOPMENT_COLLECTION — never for port / url / other canonical keys
    (no noise). Pin the audited-key set at the source."""
    assert set(_REPOINT_AUDITED_KEYS) == {
        "KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
    }, (
        "the repoint audit must be scoped to exactly the two collection "
        "keys — a port/url change must NOT emit a repoint row"
    )


# ─── Python ↔ hub sanitizer byte-parity (planner note #3 / amendment A1) ──


def test_python_sanitizer_byte_matches_hub_pinned_cases() -> None:
    """The python `_sanitize_collection_prefix` must byte-match the Rust
    `collection_naming::sanitize_collection_prefix` (the ONE promoted home)
    on the hub's pinned cases + degenerate inputs — the cross-language
    drift point now that the sanitizer was rehomed into vct-launcher-core.
    """
    cases = {
        "myproject": "Myproject",
        "my-project": "My_project",
        "my project": "My_project",
        "MyProject": "MyProject",
        "weirdproject": "Weirdproject",
        "": "Project",
        "---": "Project",
        "!!!": "Project",
        "123": "123",
    }
    for slug, expected in cases.items():
        assert _sanitize_collection_prefix(slug) == expected, (
            f"python sanitizer drifted from the hub on {slug!r}"
        )


# ─── STRUCTURAL PINS: delegation + single derivation site ───


def test_hub_delegates_to_collection_naming() -> None:
    """The hub's config_api must call the shared rule
    (`collection_naming::derive_sibling_collection`) and must NOT re-inline
    its own suffix-swap for dev/diagrams."""
    src = _CONFIG_API_RS.read_text(encoding="utf-8")
    assert "collection_naming::derive_sibling_collection" in src, (
        "hub config_api must delegate dev/diagrams to the ONE rule "
        "(collection_naming::derive_sibling_collection)"
    )
    # The old inline suffix-swap for the DEV candidate must be gone (the
    # diagrams-access CSV block still legitimately references _Diagrams, so
    # we pin the DEV-candidate inline form specifically).
    assert 'format!("{}_Development", basename)' not in src, (
        "hub must not re-inline the dev suffix-swap — it delegates now"
    )


def test_populate_delegates_to_collection_naming() -> None:
    """The launcher's populate() must call
    `collection_naming::resolve_project_collections` for own_kg/own_dev
    (no more `sanitize_kg_collection(project_name)` derivation of the dev
    name)."""
    src = _POPULATE_RS.read_text(encoding="utf-8")
    assert "collection_naming::resolve_project_collections" in src, (
        "populate() must delegate KG/dev to the ONE rule"
    )
    # The old name-derived own_dev line must be gone.
    assert 'let own_dev = format!("{}_Development", kg_basename);' not in src, (
        "populate() must not name-derive own_dev anymore — it delegates"
    )


def test_config_projection_single_development_derivation_site() -> None:
    """`config_projection.py` must have EXACTLY ONE `_Development`
    derivation site (the one rule). The dead archive-role priority is
    gone; the only place the literal `_Development"` appears in a
    string-BUILD context is the single suffix-swap/fallback block.
    """
    src = _CONFIG_PROJECTION_PY.read_text(encoding="utf-8")
    # v0.2.84 D1 consolidated the dev/diagrams derivation into ONE pure
    # helper `_derive_dev_diagrams_from_kg`. The dev NAME is BUILT there in
    # exactly two forms:
    #   * `+ "_Development"`                       (canonical suffix-swap)
    #   * `_sanitize_collection_prefix(slug)`-prefixed f-string (fallback)
    # A `("_KnowledgeGraph", "_Development")` tuple elsewhere is a
    # strip-SUFFIX loop for access-peer prefixes, NOT a derivation site —
    # excluded by targeting the two build shapes specifically.
    suffix_swap_sites = src.count('+ "_Development"')
    fallback_sites = src.count('dev = f"{prefix}_Development"')
    assert suffix_swap_sites == 1, (
        f"expected exactly 1 `+ \"_Development\"` suffix-swap site, "
        f"found {suffix_swap_sites}"
    )
    assert fallback_sites == 1, (
        f"expected exactly 1 slug-fallback `_Development` site, "
        f"found {fallback_sites}"
    )
    # And the whole derivation must live in the ONE shared helper.
    assert "def _derive_dev_diagrams_from_kg(" in src, (
        "the dev/diagrams derivation must live in the single "
        "_derive_dev_diagrams_from_kg helper"
    )
    # The dead archive-role priority must be gone from the ACTIVE code:
    # `dev_collection` must never be assigned from a kg_bindings lookup.
    # (A prose reference to the removed `kg_bindings.get("archive", ...)`
    # in an explanatory comment is fine — we pin the assignment form, not
    # the substring, so the comment doesn't false-trip the guard.)
    import re as _re

    dev_from_bindings = _re.search(
        r"dev_collection\s*=\s*kg_bindings\.get", src
    )
    assert dev_from_bindings is None, (
        "the dead role='archive' → DEVELOPMENT_COLLECTION priority must be "
        "removed — dev_collection must derive from the resolved KG, never "
        "from a kg_bindings lookup (v0.2.84 D1)"
    )


def test_sanitizer_is_single_home_in_core() -> None:
    """Amendment A1 / one-home: `sanitize_collection_prefix` lives in
    vct-launcher-core::collection_naming, and the hub re-exports it rather
    than re-defining a file-local `fn`."""
    core_src = _COLLECTION_NAMING_RS.read_text(encoding="utf-8")
    assert "pub fn sanitize_collection_prefix" in core_src, (
        "the promoted sanitizer must be defined in core::collection_naming"
    )
    hub_src = _CONFIG_API_RS.read_text(encoding="utf-8")
    assert (
        "use vct_launcher_core::collection_naming::sanitize_collection_prefix"
        in hub_src
    ), "hub must re-export the core sanitizer, not re-define it"
    # No file-local `fn sanitize_collection_prefix` re-definition survives.
    assert "fn sanitize_collection_prefix(slug: &str) -> String {" not in hub_src, (
        "hub must not keep a file-local sanitize_collection_prefix fn"
    )
