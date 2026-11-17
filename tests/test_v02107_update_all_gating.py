# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 lane UPDATE-ALL — what a bundle update owes the KG and the code graph.

Audit rows: A-015 (a README under ``knowledge/`` queued ``kg-sync --all`` in
every project), A-016 / CGWRITE-03 (every bundle update queued a full
code-graph pass, beside the Python step's own detached walk — two walks per
project per release).

Python halves pinned here (the launcher halves are Rust unit tests in
``change_detect.rs`` / ``codegraph.rs`` / ``projects_v2.rs`` /
``embed_admission.rs``):

* the envelope STATES whether a file the sync ingests changed
  (``kg_content_changed`` + ``kg_content_changed_paths``) — README / sidecar /
  excluded meta file → no; a real node → yes, and named;
* the bundle step HANDS an owed forced walk to the launcher when the launcher
  runs the build (``VCT_CODEGRAPH_WALK_OWNER=launcher``) instead of spawning a
  second, detached walk — and keeps the driver when audits are owed, or when
  nobody said the launcher walks (install.py, an older launcher);
* an incremental ``--since-commit`` pass removes only the rows of files deleted
  in the range — rows of files it did not visit are kept;
* the literals both languages read are the same.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from vco_lib import bundle_kg_change as bkc
from vco_lib import codegraph_extractor_generation as ceg

_REPO = Path(__file__).resolve().parent.parent
_RUST = _REPO / "launcher" / "src-tauri" / "src" / "commands"


# ── A-015: the envelope's own KG statement ───────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "knowledge/.node_embeddings.README.txt",  # the v0.2.106 field shape
        "knowledge/.node_formats.json",
        "knowledge/TAG_HIERARCHY.md",  # excluded meta file (never synced)
        "knowledge/VOCABULARY.md",
        "knowledge/.cache/x.md",  # a dot dir is a sidecar
        "knowledge",  # the folder itself
        ".claude/settings.json",
        "src/knowledge/a.md",  # not under the synced root
        "docs/diagram.png",
    ],
)
def test_files_the_sync_does_not_ingest_are_not_kg_content(rel):
    assert bkc.is_synced_kg_path(rel) is False


@pytest.mark.parametrize(
    "rel",
    ["knowledge/concepts/x.md", "knowledge/x.md", "docs/guide.md",
     "knowledge\\concepts\\win.md", "./knowledge/a.md"],
)
def test_a_node_the_sync_ingests_is_kg_content(rel):
    assert bkc.is_synced_kg_path(rel) is True


def test_readme_only_update_states_no_kg_change():
    """A-015 leave-alone: the v0.2.106 release shape."""
    result = {"actions": {"overwrite": ["knowledge/.node_embeddings.README.txt"],
                          "noop": ["knowledge/concepts/x.md"]}}
    bkc.record_kg_content_change(result)
    assert result["kg_content_changed"] is False
    assert result["kg_content_changed_paths"] == []


def test_a_changed_or_deleted_node_states_a_change_and_names_it():
    """A-015 act: a created node and a deleted one both owe the sync."""
    result = {"actions": {"create": ["knowledge/b.md"],
                          "orphan-deleted": ["knowledge/a.md"],
                          "preserve": ["knowledge/c.md"]}}
    bkc.record_kg_content_change(result)
    assert result["kg_content_changed"] is True
    assert result["kg_content_changed_paths"] == ["knowledge/a.md", "knowledge/b.md"]


def test_a_garbled_actions_map_states_nothing_rather_than_raising():
    result = {"actions": "garbled"}
    bkc.record_kg_content_change(result)
    assert result["kg_content_changed"] is False


def test_a_real_bundle_run_writes_the_statement(tmp_path, monkeypatch):
    """END-TO-END through the real ``install_project_bundle``: a first install
    creates the four per-project knowledge files (none of them a node) — the
    old launcher path rule called that KG content; the envelope now says no."""
    from tests.test_v0292_reindex_trigger import _identity_snapshot
    from vco_lib import project_init

    monkeypatch.setattr(
        ceg, "ensure_extractor_generation",
        lambda *a, **kw: ceg.ReindexResult(status="skipped", reason=ceg.REASON_NO_GRAPH),
    )
    _identity_snapshot(monkeypatch, resolvable=True, projects=[])
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)

    first = project_init.install_project_bundle(
        folder, orchestrator_root=_REPO, update_mode=False, dry_run=False)
    created_knowledge = [p for p in first["actions"]["create"] if p.startswith("knowledge/")]
    assert created_knowledge, "fixture premise: the bundle ships per-project knowledge files"
    assert first["kg_content_changed"] is False
    assert first["kg_content_changed_paths"] == []

    second = project_init.install_project_bundle(
        folder, orchestrator_root=_REPO, update_mode=True, dry_run=False)
    assert second["kg_content_changed"] is False


# ── CGWRITE-03: one walk per project ─────────────────────────────────────────


class _Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, repo_root, project_name, **kw):
        self.calls.append((repo_root, project_name, kw))
        import types
        return types.SimpleNamespace(status="launched", pid=7, message="", deferral=None)


def _owed(tmp_path, **kw):
    base: dict[str, Any] = dict(
        prev_version="0.2.91", running_version="0.2.107",
        project_name="DemoProj", probe=lambda names, _u: {n: True for n in names})
    base.update(kw)
    return ceg.ensure_extractor_generation(tmp_path, **base)


def test_an_owed_walk_is_handed_to_the_launcher_not_spawned(tmp_path):
    spawn = _Spawn()
    out = _owed(tmp_path, spawn=spawn, walk_owner="launcher",
                audits_owed=lambda *_a: False)
    assert out.status == "handed_off"
    assert spawn.calls == [], "the launcher's build is the one walk"
    assert out.reason_text, "the launcher shows why the forced walk runs"
    assert ceg.read_stamp_generation(tmp_path) is None, "the walk writes the stamp, not this"


def test_without_the_launcher_the_driver_is_spawned_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv(ceg.WALK_OWNER_ENV, raising=False)
    spawn = _Spawn()
    out = _owed(tmp_path, spawn=spawn)
    assert out.status == "launched" and len(spawn.calls) == 1
    assert spawn.calls[0][2]["force_rewalk"] is True


def test_the_env_hand_off_is_what_the_launcher_sets(tmp_path, monkeypatch):
    monkeypatch.setenv(ceg.WALK_OWNER_ENV, ceg.WALK_OWNER_LAUNCHER)
    spawn = _Spawn()
    out = _owed(tmp_path, spawn=spawn, audits_owed=lambda *_a: False)
    assert out.status == "handed_off" and spawn.calls == []


def test_owed_audits_keep_the_driver_which_walks_once_for_both(tmp_path):
    """The one-time audits run only inside the driver: when they are owed the
    driver does the forced walk too and the launcher reads ``launched``."""
    for probe in (lambda *_a: True, lambda *_a: (_ for _ in ()).throw(RuntimeError("x"))):
        spawn = _Spawn()
        out = _owed(tmp_path, spawn=spawn, walk_owner="launcher", audits_owed=probe)
        assert out.status == "launched" and len(spawn.calls) == 1


def test_nothing_owed_hands_nothing_off(tmp_path):
    spawn = _Spawn()
    out = _owed(tmp_path, spawn=spawn, walk_owner="launcher",
                prev_version="0.2.107", audits_owed=lambda *_a: False)
    assert out.status in {"stamped", "skipped"} and spawn.calls == []


def test_the_bundle_step_reports_the_hand_off_and_the_audit_spawn(tmp_path, monkeypatch):
    """The envelope keys the launcher's decision reads."""
    import types

    from vco_lib import codegraph_resync, project_init

    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name", lambda _f: "DemoProj")
    monkeypatch.setattr(
        ceg, "ensure_extractor_generation",
        lambda *a, **kw: ceg.ReindexResult(status="handed_off", reason=ceg.REASON_CROSSES_BUMP,
                                           reason_text="new extractor rung"),
    )
    monkeypatch.setattr(codegraph_resync, "bundle_update_audit_step",
                        lambda *a, **kw: types.SimpleNamespace(status="launched"))
    result: dict[str, Any] = {"warnings": []}
    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.91", running_version="0.2.107",
        is_root_target=False, result=result, log=lambda *a, **kw: None)
    assert result["extractor_reindex"]["status"] == "handed_off"
    assert result["extractor_reindex"]["reason_text"] == "new extractor rung"
    assert result["codegraph_audits"] == {"status": "launched"}
    assert not any("started in the background" in w for w in result["warnings"])

    monkeypatch.setattr(codegraph_resync, "bundle_update_audit_step", lambda *a, **kw: None)
    quiet = {"warnings": []}
    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.91", running_version="0.2.107",
        is_root_target=False, result=quiet, log=lambda *a, **kw: None)
    assert "codegraph_audits" not in quiet, "no audit spawn → no key"


# ── prune semantics under --incremental ──────────────────────────────────────


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_an_incremental_pass_prunes_only_files_deleted_in_its_range(tmp_path):
    """The launcher's HEAD-moved pass is ``--incremental --since-commit
    <recorded>`` WITHOUT ``--prune-stale``. Its only deletion is the git one:
    rows of files deleted in ``<recorded>..HEAD``. Rows of files it did not
    visit (unchanged since the recorded commit) are kept."""
    import importlib.util

    from tests.test_v02100_codegraph_deleted_file_prune import _git, _repo, _row, _stub

    spec = importlib.util.spec_from_file_location(
        "_v02107_acg", str(_REPO / "templates" / "scripts" / "analyze_code_graph.py"))
    assert spec is not None and spec.loader is not None
    acg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acg)  # type: ignore[union-attr]

    root = tmp_path / "repo"
    recorded = _repo(root, {"unchanged.txt": "a\n", "gone.txt": "b\n", "edited.txt": "c\n"})
    _git(root, "rm", "-q", "gone.txt")
    (root / "edited.txt").write_text("c2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "move HEAD")
    src = root.resolve().as_posix()
    inst = _stub(acg, {"functions_collection": [
        _row("unchanged", "file_path", "unchanged.txt", src),
        _row("gone", "file_path", "gone.txt", src),
        _row("edited", "file_path", "edited.txt", src),
    ]})
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False

    inst.analyze_repository(root.resolve(), incremental=True, since_commit=recorded,
                            prune_stale=False)

    assert inst.functions_collection.data.deleted == ["gone"]


# ── the literals both languages read ─────────────────────────────────────────


def _rust(rel: str) -> str:
    return (_RUST / rel).read_text(encoding="utf-8")


def test_the_cross_language_literals_match():
    detect = _rust("projects_v2/change_detect.rs")
    m = re.search(r"BUNDLE_CONTENT_CHANGING_BUCKETS: \[&str; \d+\] =\s*\[([^\]]*)\]", detect)
    assert m, "change_detect.rs bucket list not found"
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == bkc.CONTENT_CHANGING_BUCKETS
    assert f'"{bkc.KG_CONTENT_CHANGED_KEY}"' in detect
    assert f'"{bkc.KG_CONTENT_CHANGED_PATHS_KEY}"' in detect
    pv2 = _rust("projects_v2.rs")
    assert f'CODEGRAPH_WALK_OWNER_ENV: &str = "{ceg.WALK_OWNER_ENV}"' in pv2
    assert f'CODEGRAPH_WALK_OWNER_LAUNCHER: &str = "{ceg.WALK_OWNER_LAUNCHER}"' in pv2
