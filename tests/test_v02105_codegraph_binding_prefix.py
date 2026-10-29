# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the code-graph collection prefix comes from the BINDING, never
the launcher slug or the registered name.

The bug: the orchestrator root is registered with slug/name
``orchestrator-root`` and bound to the prefix ``VibeCodedOrchestrator``. The
owed-work retry passed the slug to ``codegraph_resync --project``, which
sanitized it to ``Orchestrator_root`` and walked the root into a zombie
``Orchestrator_root_Code*`` set, so ``codegraph_embed_resync_pending`` never
converged; the summary backfill passed the registered name to
``generate-code-summary``. Every test here uses a root whose slug differs from
its binding prefix and asserts each caller targets the binding prefix.

Red-proofs (recorded in the lane report): pointing each caller back at the
slug/name source turns its test red.

No live service: launcher.db is the real-schema fixture in tmp, Weaviate is a
fake listing, the hub resolver is disabled by conftest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import codegraph_project_prefix as cpp

SLUG = "orchestrator-root"
PREFIX = "VibeCodedOrchestrator"
ZOMBIE = "Orchestrator_root"


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """A root folder registered as ``orchestrator-root`` and bound to
    ``VibeCodedOrchestrator``, with no settings env (so only the binding row
    can answer — the case the retry hit)."""
    folder = tmp_path / "VCO_dev"
    (folder / ".claude" / "scripts").mkdir(parents=True)
    (folder / ".claude" / "scripts" / "analyze_code_graph.py").write_text("# stub\n")
    db = make_launcher_db(tmp_path, projects=[{
        "project_id": "root-id", "name": SLUG, "slug": SLUG,
        "folder_path": folder, "codegraph_prefix": PREFIX,
    }])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return folder


# ── the resolver ──────────────────────────────────────────────────────────


def test_binding_row_beats_slug_and_name(root):
    res = cpp.resolve(root)
    assert (res.prefix, res.source) == (PREFIX, cpp.SOURCE_BINDING)
    assert res.authoritative


def test_hub_prefix_is_tier_one(root, monkeypatch):
    cfg = SimpleNamespace(code_graph_collection_prefix="HubPrefix",
                          code_graph_project=SLUG)
    monkeypatch.setattr("vco_lib.project_config.resolve", lambda _f: cfg)
    assert cpp.resolve(root) == cpp.PrefixResolution("HubPrefix", cpp.SOURCE_HUB)


def test_hub_slug_field_is_never_used(root, monkeypatch):
    """A hub that answers with an EMPTY prefix falls through to the binding,
    never to its ``code_graph_project`` slug."""
    cfg = SimpleNamespace(code_graph_collection_prefix="", code_graph_project=SLUG)
    monkeypatch.setattr("vco_lib.project_config.resolve", lambda _f: cfg)
    assert cpp.resolve_codegraph_prefix(root) == PREFIX


def test_settings_pin_is_tier_two(root):
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"CODE_GRAPH_PROJECT": "Pinned"}}))
    assert cpp.resolve(root) == cpp.PrefixResolution("Pinned", cpp.SOURCE_SETTINGS)


def test_binding_beats_project_name(root):
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"PROJECT_NAME": "orchestrator root"}}))
    assert cpp.resolve_codegraph_prefix(root) == PREFIX


def test_a_name_is_used_only_when_no_binding_exists(tmp_path, monkeypatch):
    folder = tmp_path / "plain"
    folder.mkdir()
    db = make_launcher_db(tmp_path, projects=[{
        "project_id": "p", "name": "Plain Name", "folder_path": folder,
    }])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    res = cpp.resolve(folder)
    assert (res.prefix, res.source) == ("Plain Name", cpp.SOURCE_DERIVED)
    assert cpp.resolve_codegraph_prefix(folder, authoritative_only=True) == ""


def test_an_unreadable_registry_never_falls_back_to_a_name(tmp_path, monkeypatch):
    db = tmp_path / "launcher.db"
    db.write_bytes(b"not a database")
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"PROJECT_NAME": "Guess"}}))
    assert cpp.resolve_codegraph_prefix(folder) == ""


# ── caller 1: the owed-work retries ───────────────────────────────────────


def _ctx(folder: Path, cid: str):
    from vco_lib import deferral_retry as dr

    seen: list = []
    ctx = dr.RetryContext(folder=folder, condition_id=cid,
                          runner=lambda argv, _cwd: seen.append(list(argv)) or 0,
                          python="python")
    return dr, ctx, seen


def _project_arg(argv: list) -> str:
    return argv[argv.index("--project") + 1]


def test_resync_retry_targets_the_binding_prefix(root):
    dr, ctx, seen = _ctx(root, "codegraph_embed_resync_pending")
    dr.retry_codegraph_resync(ctx)
    assert len(seen) == 1
    assert _project_arg(seen[0]) == PREFIX


def test_code_graph_walk_retry_targets_the_binding_prefix(root):
    dr, ctx, seen = _ctx(root, "code_graph_walk_skipped")
    dr.retry_code_graph_walk(ctx)
    assert len(seen) == 1
    assert _project_arg(seen[0]) == PREFIX


# ── caller 2: the summary backfill ────────────────────────────────────────


def test_summary_backfill_prunes_against_the_binding_prefix(root):
    from vco_lib import summary_health as sh

    (root / ".claude" / ".code_formats.json").write_text(
        json.dumps({"a.py::m.f": {"one_liner": "f", "backend": "ollama"}}))
    calls: list = []

    def spawn(argv, *, cwd, env):
        calls.append(list(argv))
        return 0

    res = sh.backfill(root, sh.PendingSet(code_stale=["a.py::m.f"]), spawn=spawn)
    assert res.code_leg_skipped is False
    code = [c for c in calls if c[1].endswith("generate-code-summary.py")]
    assert len(code) == 1 and _project_arg(code[0]) == PREFIX


# ── caller 3: the install / bundle-update spawns ──────────────────────────


def test_bundle_reindex_resolves_the_binding_prefix(root):
    from vco_lib import project_init

    assert project_init._resolve_codegraph_project_name(root) == PREFIX


def test_install_update_resync_spawn_targets_the_binding_prefix(root):
    """Executes the REAL install.py shim + its prefix helper."""
    from vco_lib import codegraph_resync as cr
    from vco_lib.deferral_report import DeferralReport

    src = (Path(__file__).resolve().parents[1] / "install.py").read_text(encoding="utf-8")
    bodies = []
    for name in ("def _trigger_codegraph_embed_resync(", "def _root_codegraph_prefix("):
        start = src.index(name)
        bodies.append(src[start:src.index("\ndef ", start + 10)])
    ns = {"PROJECT_ROOT": root, "DeferralReport": DeferralReport,
          "_log_install_event": lambda *a: None,
          "_derive_orchestrator_project_name": lambda: "InstallDefault"}
    exec(compile("\n".join(bodies), "install.py", "exec"), ns)  # noqa: S102
    seen = {}

    def _spawn(_root, project_name, **_kw):
        seen["project"] = project_name
        return cr.ResyncTriggerResult(status="skipped", message="stub")

    with mock.patch.object(cr, "spawn_background_resync", _spawn), \
            mock.patch.object(cr, "report_trigger_result", lambda *a, **k: None):
        ns["_trigger_codegraph_embed_resync"](DeferralReport())
    assert seen["project"] == PREFIX
    # Leave-alone side: an unbound root keeps the install default.
    assert ns["_root_codegraph_prefix"]() == PREFIX
    ns["PROJECT_ROOT"] = root.parent / "unbound"
    assert ns["_root_codegraph_prefix"]() == "InstallDefault"


# ── caller 4: the move's printed rebuild command ──────────────────────────


def test_move_rebuild_command_names_the_binding_prefix(root, tmp_path):
    from vco_lib import project_move as pm

    dst = tmp_path / "moved"
    (dst / ".claude" / "context").mkdir(parents=True)
    plan = pm.MovePlan(project_id="root-id", project_name=SLUG, project_slug=SLUG,
                       src=str(root), dst=str(dst), src_exists=True, dst_exists=True,
                       dst_empty=False, dst_has_manifest=True)
    pm._emit_move_deferrals(plan, dst, stale_file_hits=(), stale_db_hits=(),
                            harness={}, codegraph_enqueued=True)
    ledger = (dst / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
    assert f"--project '{PREFIX}'" in ledger
    assert f"--project '{SLUG}'" not in ledger


# ── third-party auto-heal ─────────────────────────────────────────────────


def _resync_entry(project: str):
    from vco_lib.codegraph_resync import build_unconverged_deferral

    entry = build_unconverged_deferral(
        project, {f"{ZOMBIE}_CodeFunction": 3},
        f"python -m vco_lib.codegraph_resync --run-resync --project {project} "
        "--repo-root /x --analyzer /x/a.py")
    assert entry is not None
    return entry


def _ledger(folder: Path):
    from vco_lib.deferral_report import DeferralReport

    return {e.condition_id: e for e in DeferralReport.read(folder).entries}


def test_heal_retargets_a_slug_resync_entry(root):
    from vco_lib.deferral_emit import emit

    emit(root, _resync_entry(SLUG))
    assert cpp.heal_resync_entry(root, PREFIX) is True
    entry = _ledger(root)["codegraph_embed_resync_pending"]
    assert f"--project {PREFIX}" in entry.command_to_apply
    assert f"--project {SLUG}" not in entry.command_to_apply
    assert "Corrected by a later update" in entry.detected


def test_heal_leaves_a_correct_resync_entry_alone(root):
    from vco_lib.deferral_emit import emit

    emit(root, _resync_entry(PREFIX))
    before = (root / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
    assert cpp.heal_resync_entry(root, PREFIX) is False
    assert (root / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text() == before


def _heal(root, *, classes, keep=None, counts=None):
    counts = counts or {}

    def lister():
        if isinstance(classes, Exception):
            raise classes
        return list(classes)

    return cpp.heal_slug_prefix_divergence(
        root, weaviate_url="http://127.0.0.1:9",
        keep_set=(keep if keep is not None else {cpp._norm(PREFIX)}, True),
        list_classes=lister, count_objects=lambda c: counts.get(c))


def test_heal_reports_zombie_classes_with_counts_and_a_consented_drop(root):
    out = _heal(root, classes=[f"{PREFIX}_CodeFunction", f"{ZOMBIE}_CodeFunction",
                               f"{ZOMBIE}_CodeClass", "Other_CodeFunction"],
                counts={f"{ZOMBIE}_CodeFunction": 55000, f"{ZOMBIE}_CodeClass": 7})
    assert out["emitted"] is True
    assert [z["class_name"] for z in out["zombies"]] == [
        f"{ZOMBIE}_CodeClass", f"{ZOMBIE}_CodeFunction"]
    entry = _ledger(root)[cpp.ZOMBIE_CONDITION_ID]
    assert "55000 rows" in entry.detected and PREFIX in entry.detected
    assert (f"drop-orphan-code-collections --weaviate-url http://127.0.0.1:9 "
            f"--prefix {ZOMBIE} --confirm") in entry.command_to_apply
    assert "For your Claude assistant" in entry.command_to_apply


def test_heal_never_reports_a_prefix_some_project_is_bound_to(root):
    out = _heal(root, classes=[f"{ZOMBIE}_CodeFunction"],
                keep={cpp._norm(PREFIX), cpp._norm(ZOMBIE)})
    assert out["zombies"] == [] and cpp.ZOMBIE_CONDITION_ID not in _ledger(root)


def test_heal_clears_the_entry_once_the_classes_are_gone(root):
    _heal(root, classes=[f"{ZOMBIE}_CodeModule"])
    assert cpp.ZOMBIE_CONDITION_ID in _ledger(root)
    out = _heal(root, classes=[f"{PREFIX}_CodeModule"])
    assert out["resolved"] is True
    assert cpp.ZOMBIE_CONDITION_ID not in _ledger(root)


def test_an_unreadable_schema_leaves_the_entry_as_is(root):
    _heal(root, classes=[f"{ZOMBIE}_CodeModule"])
    out = _heal(root, classes=RuntimeError("weaviate down"))
    assert "unreadable" in out["skipped"]
    assert cpp.ZOMBIE_CONDITION_ID in _ledger(root)


def test_heal_does_nothing_when_slug_matches_the_binding(tmp_path, monkeypatch):
    folder = tmp_path / "acme"
    folder.mkdir()
    db = make_launcher_db(tmp_path, projects=[{
        "project_id": "a", "name": "Acme", "slug": "acme",
        "folder_path": folder, "codegraph_prefix": "Acme"}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    lister = mock.Mock(return_value=["Acme_CodeFunction"])
    out = cpp.heal_slug_prefix_divergence(
        folder, keep_set=({"acme"}, True), list_classes=lister,
        count_objects=lambda _c: 1)
    assert out["skipped"] and not lister.called


def test_a_real_bundle_update_runs_the_heal(root, monkeypatch):
    """END-TO-END wiring: the bundle update — the path the launcher runs for
    every project and the root — reaches the heal for its folder."""
    from vco_lib import codegraph_extractor_generation as ceg
    from vco_lib import project_init

    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(ceg, "ensure_extractor_generation",
                        lambda *_a, **_k: ceg.ReindexResult(status="skipped", reason="x",
                                                            message="stub"))
    project_init.install_project_bundle(root, orchestrator_root=repo_root,
                                        update_mode=False, dry_run=False)
    seen = []
    monkeypatch.setattr(cpp, "heal_slug_prefix_divergence",
                        lambda folder, **_k: seen.append(Path(folder)) or {})
    project_init.install_project_bundle(root, orchestrator_root=repo_root,
                                        update_mode=True, dry_run=False)
    assert seen == [root]


# ── the consented drop narrows to the named prefix ────────────────────────


def test_drop_orphan_prefix_narrows_and_never_widens(monkeypatch):
    from vco_lib import project_init

    monkeypatch.setattr(project_init, "_codegraph_keep_set_normalised",
                        lambda: ({PREFIX.lower()}, True))
    monkeypatch.setattr(project_init, "_revalidated_orphan_live_classes",
                        lambda _u: [f"{ZOMBIE}_CodeFunction", "Stale_CodeModule"])
    dropped: list = []
    monkeypatch.setattr(project_init, "_delete_class",
                        lambda cls, weaviate_url=None: dropped.append(cls))
    args = argparse.Namespace(confirm=True, weaviate_url="http://127.0.0.1:9",
                              json=True, prefix=[ZOMBIE])
    assert project_init._cmd_drop_orphan_code_collections(args) == 0
    assert dropped == [f"{ZOMBIE}_CodeFunction"]
    dropped.clear()
    args.prefix = None  # leave-alone side: no filter keeps today's behaviour
    project_init._cmd_drop_orphan_code_collections(args)
    assert dropped == [f"{ZOMBIE}_CodeFunction", "Stale_CodeModule"]
