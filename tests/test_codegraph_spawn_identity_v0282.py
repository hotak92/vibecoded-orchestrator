# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.82 (WP-3 / G3): source-shape pins on the launcher's code-graph identity
SSOT.

The dual-writer duplicate-row bug (a spaced-name project accumulating rows under
BOTH the display name and the sanitized collection prefix) is fixed by feeding
every launcher spawn surface the CANONICAL identity (the codegraph binding
``collection_prefix``, resolved by the ONE Rust helper
``resolve_codegraph_identity``) instead of the raw display name. The behavioural
proof lives in the Rust unit tests (identity matrix, provenance parser,
embedding-change classifier). THIS test is the grep-shape lint that fails at CI
time the moment a NEW ``--project`` feed re-introduces the raw display name in
the two owned files — a lint sees a new spawn surface a runtime test cannot.

Owned files (WP-3 exclusive):
  * launcher/src-tauri/src/commands/codegraph.rs
  * launcher/src-tauri/src/commands/orchestrator_core.rs

Companion Rust source-pin idiom: tests/test_kg_binding_single_writer_rust.py.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_RS_ROOT = REPO_ROOT / "launcher" / "src-tauri"

_CODEGRAPH_RS = _RS_ROOT / "src" / "commands" / "codegraph.rs"
_ORCH_CORE_RS = _RS_ROOT / "src" / "commands" / "orchestrator_core.rs"
# v0.2.82 coordinator follow-up (WP-3 flagged it as out-of-scope): the
# Re-analyze modal surface also spawns the analyzer and must use the SSOT.
_REANALYZE_RS = _RS_ROOT / "src" / "commands" / "codegraph_reanalyze.rs"

_OWNED_FILES = (_CODEGRAPH_RS, _ORCH_CORE_RS, _REANALYZE_RS)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_owned_files_exist() -> None:
    for p in _OWNED_FILES:
        assert p.is_file(), f"WP-3 owned file missing: {p}"


# ── The SSOT helper exists and is defined exactly once ──────────────────────

def test_resolve_codegraph_identity_defined_once() -> None:
    """The identity SSOT helper is defined exactly once, in codegraph.rs."""
    content = _read(_CODEGRAPH_RS)
    defs = re.findall(
        r"\bfn\s+resolve_codegraph_identity\b", content
    )
    assert len(defs) == 1, (
        "resolve_codegraph_identity must be defined exactly once "
        f"(found {len(defs)} definitions in codegraph.rs) — one SSOT, no copies."
    )
    # The pure decision helper it delegates to must also exist.
    assert "fn pick_codegraph_identity" in content, (
        "the pure identity picker pick_codegraph_identity is missing"
    )


# ── No `--project` argument is fed directly from a raw display name ──────────

# `.arg(&project.name)` / `.arg(&project_name)` / a `--project` vec entry
# followed by `project_name.clone()` are the raw-display-name feeds the fix
# eliminates. After WP-3 every `--project` value must be the resolved
# `identity` / `canonical_identity`.
_RAW_NAME_ARG_PATTERNS = (
    # tokio/std Command builder: .arg(&project.name) directly after --project
    re.compile(r"\.arg\(\s*&project\.name\s*\)"),
    re.compile(r"\.arg\(\s*&project_name\s*\)"),
    # vec-style args: a "--project" entry immediately followed by
    # project_name.clone() (the pre-fix run_build_task shape).
    re.compile(r'"--project"\.to_string\(\)\s*,\s*project_name\.clone\(\)'),
)


def test_no_raw_display_name_fed_to_project_flag() -> None:
    """No owned file feeds a raw project display name to ``--project``."""
    violations: list[str] = []
    for path in _OWNED_FILES:
        content = _read(path)
        for pat in _RAW_NAME_ARG_PATTERNS:
            for m in pat.finditer(content):
                lineno = content.count("\n", 0, m.start()) + 1
                snippet = " ".join(m.group(0).split())
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: `{snippet}` — feed "
                    f"the CANONICAL identity (resolve_codegraph_identity / the "
                    f"resolved `identity` / `canonical_identity`) to --project, "
                    f"not the raw display name."
                )
    assert not violations, (
        f"{len(violations)} raw-display-name --project feed(s) found — the "
        "dual-writer identity fix requires every spawn surface to use the "
        "binding-prefix identity:\n" + "\n".join(violations)
    )


# ── Every `--project` feed site routes through the resolved identity ────────

def test_reanalyze_surface_resolves_identity() -> None:
    """The Re-analyze modal (7th surface) resolves the canonical identity
    before spawning — `run_reanalysis_with_stream` must be fed the resolved
    `identity`, never `project.name` raw."""
    content = _read(_REANALYZE_RS)
    assert "resolve_codegraph_identity" in content, (
        "codegraph_reanalyze.rs no longer resolves the canonical identity — "
        "the Re-analyze button would stamp display-name rows again "
        "(dual-writer duplicate UUIDs)."
    )
    assert re.search(
        r"run_reanalysis_with_stream\(\s*&identity\b", content
    ), "run_reanalysis_with_stream must receive the resolved identity"
    assert not re.search(
        r"run_reanalysis_with_stream\(\s*&project\.name\b", content
    ), "raw project.name fed to the reanalyze spawn"


def test_orchestrator_core_surfaces_resolve_identity() -> None:
    """Both orchestrator_core spawn surfaces (reanalyze J + prune-stale L) call
    the SSOT helper before feeding --project."""
    content = _read(_ORCH_CORE_RS)
    calls = re.findall(
        r"crate::commands::codegraph::resolve_codegraph_identity\b", content
    )
    # Two direct analyzer spawn surfaces (J + L) each resolve the identity.
    assert len(calls) >= 2, (
        "orchestrator_core.rs must call resolve_codegraph_identity for BOTH the "
        f"reanalyze (J) and prune-stale (L) surfaces (found {len(calls)} calls)."
    )
    # And the --project value fed must be the resolved `identity`, not a name.
    assert re.search(r'\.arg\(\s*"--project"\s*\)\s*\.arg\(\s*&identity\s*\)', content), (
        "orchestrator_core.rs must feed the resolved `identity` to --project."
    )


def test_run_build_task_uses_canonical_identity_for_project() -> None:
    """run_build_task builds its `--project` arg from `canonical_identity`, and
    resolves that identity via the SSOT helper."""
    content = _read(_CODEGRAPH_RS)
    # The identity is resolved via the SSOT helper inside run_build_task.
    assert "resolve_codegraph_identity(&db, &project_id, &project_name)" in content, (
        "run_build_task must resolve the canonical identity via "
        "resolve_codegraph_identity(&db, &project_id, &project_name)."
    )
    # The args vec feeds canonical_identity (not project_name) to --project.
    assert re.search(
        r'"--project"\.to_string\(\)\s*,\s*canonical_identity\.clone\(\)', content
    ), (
        "run_build_task's args vec must feed canonical_identity.clone() to "
        "--project (not project_name.clone())."
    )


# ── Provenance + backfill + force-recreate wiring shape ─────────────────────

def test_provenance_parse_and_persist_wired() -> None:
    content = _read(_CODEGRAPH_RS)
    assert "fn parse_codegraph_provenance" in content
    assert "fn persist_codegraph_provenance" in content
    # The success arm parses the provenance line.
    assert "parse_codegraph_provenance(&stdout_str)" in content


def test_force_recreate_gated_on_profile_change_only() -> None:
    """--force-recreate is pushed ONLY under the force_recreate_for_profile_change
    gate (task 5a), never unconditionally."""
    content = _read(_CODEGRAPH_RS)
    assert 'args.push("--force-recreate".to_string())' in content, (
        "run_build_task must be able to pass --force-recreate for a profile change"
    )
    # It must sit inside the `if force_recreate_for_profile_change {` block.
    m = re.search(
        r"if force_recreate_for_profile_change\s*\{[^}]*"
        r'args\.push\("--force-recreate"\.to_string\(\)\)',
        content,
        re.DOTALL,
    )
    assert m is not None, (
        "--force-recreate must be gated on force_recreate_for_profile_change, "
        "not pushed unconditionally."
    )


def test_backfill_rider_gated_on_non_root() -> None:
    content = _read(_CODEGRAPH_RS)
    assert "fn spawn_metadata_backfill" in content
    assert "vco_lib.codegraph_resync" in content
    assert "--backfill-metadata" in content
    # Gated on non-root.
    assert "is_non_root" in content and "OrchestratorRoot" in content


def test_boot_resume_reuses_shared_root_skip_helper() -> None:
    """The boot-resume root skip delegates to the SAME pure helper the update
    path uses (no diverging copy)."""
    content = _read(_CODEGRAPH_RS)
    assert (
        "crate::commands::projects_v2::update_should_skip_root_autobuild" in content
    ), (
        "resume_pending_builds must reuse projects_v2::"
        "update_should_skip_root_autobuild — no diverging root-skip copy."
    )
