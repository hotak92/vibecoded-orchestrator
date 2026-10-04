# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 lane P2 — items 2, 3 and 4.

Item 2 — an ADOPTED service's data knob is projected into
    ``infrastructure/.env``. ``compose_env.service_key_values`` used to gate
    the data knob on ``row.mode == "vco_managed"``, but ``compose`` substitutes
    ``${VCT_*_DATA_SOURCE:-<empty volume>}`` for ANY recreate (install step 5
    and the deferral's printed command). On a real machine Ollama is usually
    ``adopted_container`` with a bind, so the knob was never written and any
    recreate outside install.py re-pointed Ollama at the empty named volume.
    Ports stay gated to ``vco_managed`` (deliberate; pinned below).

Item 3 — archived/superseded KG nodes are skipped by the sync
    (``sync_knowledge_graph._is_archived_node``) but were walked by
    ``install_weaviate._compute_on_disk_content_hashes``, and
    ``content_hash_diff`` counts a missing stored hash as changed — so ~80-97
    archived files were re-walked on every update. ONE predicate
    (``vco_lib.kg_node_status``) now serves both.

Item 4 — the install's whole-tree KG seed (``--all``, 10+ minutes on a CPU)
    is ENQUEUED into the detached retry driver (``deferral_retry``) instead of
    awaited. The install carries the context triple in the spawned child's env;
    ``retry_kg_seed`` stamps it only after the child's own paired clear proves
    the seed ran. Backend down ⇒ still exactly ONE owed row.

All hermetic: fakes for the runtime/compose, a real-schema launcher.db under
``tmp_path``, ``WEAVIATE_URL`` pinned by the suite conftest; no network.

NOTE for the next editor: ``tests/test_install_ci10_seed_diff_gate.py`` still
asserts the PRE-v0.2.101 seed shape for its full-sync tests (a foreground
``run_child_logged`` carrying ``--all``, and install.py advancing the context
triple). Those assertions now belong on the ENQUEUE path — assert
``vco_lib.deferral_retry.spawn_detached`` is called with the context in
``extra_env`` and that install.py does NOT advance the triple; the stamp is
pinned here instead (``test_retry_handler_stamps_only_after_a_proven_clear``).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import compose_env  # noqa: E402
from vco_lib import install_weaviate as _install_weaviate  # noqa: E402
from vco_lib import kg_context_triple  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib.envfile import parse_env_lines  # noqa: E402
import install  # noqa: E402

COMPOSE_FILE = REPO_ROOT / "infrastructure" / "docker-compose.yml"
SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
OLLAMA_DEST = "/root/.ollama"
MODELS_BIND = "/srv/models"


def _keys(infra: Path) -> dict:
    return dict(parse_env_lines((infra / ".env").read_text(encoding="utf-8")))


def _adopted_ollama(mount=None) -> se.EndpointRow:
    return se.EndpointRow(
        service="ollama", mode="adopted_container", port=11435,
        source="live_reconcile", container_name="vco_ollama", data_mount=mount,
    )


def _bind(path=MODELS_BIND) -> dict:
    return {"kind": "bind", "source": str(path), "destination": OLLAMA_DEST}


# ===========================================================================
# Item 2 — a data knob is stated for ANY row that carries an observed mount
# ===========================================================================


def test_adopted_ollama_bind_projects_its_data_knob(tmp_path):
    """The real-machine shape: Ollama adopted, live container binds a host dir."""
    compose_env.write_service_keys(tmp_path, {"ollama": _adopted_ollama(_bind())},
                                    runtime="podman")
    got = _keys(tmp_path)
    assert got["VCT_OLLAMA_DATA_SOURCE"] == MODELS_BIND
    assert got.get("VCT_OLLAMA_VOLUME_NAME", "") == ""


def test_adopted_ollama_volume_projects_the_volume_name(tmp_path):
    """The half-pair semantics survive: a volume states VOLUME_NAME, SOURCE=''."""
    row = _adopted_ollama(
        {"kind": "volume", "source": "legacy_ollama_data", "destination": OLLAMA_DEST})
    compose_env.write_service_keys(tmp_path, {"ollama": row}, runtime="podman")
    got = _keys(tmp_path)
    assert got["VCT_OLLAMA_VOLUME_NAME"] == "legacy_ollama_data"
    assert got.get("VCT_OLLAMA_DATA_SOURCE", "") == ""


def test_adopted_ports_stay_gated_but_the_knob_is_projected(tmp_path):
    """Decision pinned: ports stay vco_managed-only, data knobs do not.

    An adopted service is not composed by VCO, so its PORT substitution is
    irrelevant; its mount is a fact about where the data lives, and a recreate
    by the user's own compose reads the knob. The two halves of this decision
    are asserted TOGETHER so a later edit cannot quietly flip either.
    """
    compose_env.write_service_keys(tmp_path, {"ollama": _adopted_ollama(_bind())},
                                    runtime="podman")
    got = _keys(tmp_path)
    assert "OLLAMA_PORT" not in got
    assert got["VCT_OLLAMA_DATA_SOURCE"] == MODELS_BIND


def test_adopted_row_without_a_mount_states_no_knob_and_keeps_a_user_line(tmp_path):
    """No mount ⇒ no statement, and an outside user line still survives."""
    (tmp_path / ".env").write_text(f"VCT_OLLAMA_DATA_SOURCE={MODELS_BIND}\n", encoding="utf-8")
    compose_env.write_service_keys(tmp_path, {"ollama": _adopted_ollama(None)}, runtime="podman")
    assert _keys(tmp_path)["VCT_OLLAMA_DATA_SOURCE"] == MODELS_BIND


def test_the_printed_recipe_resolves_the_real_bind(tmp_path):
    """The deferral's printed `cd <infra> && <compose>` command reads the .env
    this writer produces — so the render must mount the RECORDED bind, never
    compose's default (empty) named volume."""
    from vco_lib.service_adoption import config_mounts, load_compose_doc

    compose_env.write_service_keys(tmp_path, {"ollama": _adopted_ollama(_bind())},
                                    runtime="podman")
    env = _keys(tmp_path)  # exactly what the printed command would read
    doc = load_compose_doc(COMPOSE_FILE, env)
    assert doc is not None
    mounts = config_mounts(doc["services"]["ollama"], doc.get("volumes") or {})
    m = mounts[OLLAMA_DEST]
    assert m.kind == "bind", (
        "the printed recreate command would re-point Ollama at the empty "
        f"named volume (rendered {m.kind}:{m.source})"
    )
    assert m.source == MODELS_BIND


# ===========================================================================
# Item 3 — ONE archived predicate for the sync AND the seed's change check
# ===========================================================================


def _load_sync_module():
    """Load templates/scripts/sync_knowledge_graph.py without its CLI entry."""
    spec = importlib.util.spec_from_file_location(
        "_v02101_sync_under_test", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_v02101_sync_under_test", mod)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # pragma: no cover - dep missing
        pytest.skip(f"sync script runtime deps not installed ({exc})")
    return mod


def _write_knowledge_tree(root: Path) -> dict:
    """active + status-archived + path-archived; returns {label: path}."""
    (root / "concepts").mkdir(parents=True, exist_ok=True)
    (root / "archive").mkdir(parents=True, exist_ok=True)
    active = root / "concepts" / "live.md"
    active.write_text("---\ntitle: Live\nstatus: active\n---\nbody\n", encoding="utf-8")
    by_status = root / "concepts" / "old.md"
    by_status.write_text("---\ntitle: Old\nstatus: archived\n---\nbody\n", encoding="utf-8")
    by_path = root / "archive" / "deep.md"
    by_path.write_text("---\ntitle: Deep\n---\nbody\n", encoding="utf-8")
    return {"active": active, "by_status": by_status, "by_path": by_path}


def test_archived_nodes_are_not_in_the_on_disk_diff(tmp_path):
    """The walk excludes archived nodes, so they never enter the diff."""
    knowledge = tmp_path / "knowledge"
    files = _write_knowledge_tree(knowledge)

    on_disk = _install_weaviate._compute_on_disk_content_hashes(knowledge)
    assert str(files["active"]) in on_disk
    assert str(files["by_status"]) not in on_disk, (
        "a `status: archived` node must not be walked — the sync skips it, so "
        "it has no stored hash and would be re-listed as changed every update"
    )
    assert str(files["by_path"]) not in on_disk, (
        "a node under knowledge/archive/ must not be walked"
    )

    # End to end through the pure comparison the install uses.
    diff = _install_weaviate.content_hash_diff(on_disk, {}, tmp_path)
    assert str(files["active"]) in diff
    assert str(files["by_status"]) not in diff
    assert str(files["by_path"]) not in diff


def test_shared_predicate_parity(tmp_path):
    """The script, the vco_lib home and the kg_sync_drift mirror agree.

    Two independent implementations exist (the script delegates to the
    vco_lib home; ``kg_sync_drift`` carries its own C-leg mirror because it
    cannot import the script), so this can genuinely go red when one drifts.
    """
    from vco_lib import kg_node_status
    from vco_lib import kg_sync_drift

    sync = _load_sync_module()
    cases = [
        (Path("knowledge/archive/a.md"), None),
        (Path("knowledge/.archive/a.md"), None),
        (Path("knowledge/_archive/a.md"), None),
        (Path("knowledge/concepts/architecture/a.md"), None),   # NOT a match
        (Path("knowledge/concepts/a.md"), {"status": "archived"}),
        (Path("knowledge/concepts/a.md"), {"status": "deprecated"}),
        (Path("knowledge/concepts/a.md"), {"status": "superseded"}),
        (Path("knowledge/concepts/a.md"), {"status": "Archived "}),
        (Path("knowledge/concepts/a.md"), {"status": "active"}),
        (Path("knowledge/concepts/a.md"), {}),
    ]
    for path, frontmatter in cases:
        expected = kg_node_status.is_archived_node(path, frontmatter)[0]
        assert sync._is_archived_node(path, frontmatter)[0] == expected, (path, frontmatter)
        status = (frontmatter or {}).get("status")
        content = f"---\nstatus: {status}\n---\n" if status else "no frontmatter"
        assert kg_sync_drift.is_archived_node(path.parts, content) == expected, (
            f"kg_sync_drift mirror diverged for {path} / {frontmatter}"
        )


# ===========================================================================
# Item 4 — the whole-tree seed is enqueued, not awaited
# ===========================================================================


def _make_args(update: bool = True) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.update = update
    ns.skip_seed = False
    return ns


class _SeedHarness:
    """Drives install._seed_weaviate with fakes; records what it did."""

    def __init__(self, tmp_path: Path, *, spawn_ok: bool = True, report=None):
        self.tmp = tmp_path
        self.spawn_ok = spawn_ok
        self.report = report
        self.spawns: list = []
        self.children: list = []
        self.pruned: list = []
        #: the on-disk cids AS THE SPAWN SAW THEM — the B1 ordering proof
        self.disk_at_spawn: "list[str] | None" = None
        #: optional side effect a fake CHILD performs (e.g. its paired clear)
        self.on_child: "Optional[Callable]" = None
        infra_scripts = tmp_path / ".claude" / "scripts"
        infra_scripts.mkdir(parents=True, exist_ok=True)
        (infra_scripts / "sync_knowledge_graph.py").write_text("# stub\n", encoding="utf-8")
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        py = venv / "python"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        self.db_path = make_launcher_db(
            tmp_path / "launcher.db",
            app_state={
                # A collection RENAME → context change → full sync, no enrichment.
                install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING: "qwen3",
                install._APP_STATE_KEY_LAST_KG_COLLECTION: "OldProject_KnowledgeGraph",
                install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION: "",
            },
        )

    def _spawn(self, folder, *, python="", extra_env=None):
        # v0.2.101 B1: the driver reads the DISK ledger once and exits — record
        # what was on disk at the very moment the spawn happened.
        self.disk_at_spawn = _on_disk_cids(Path(folder))
        self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
        return self.spawn_ok

    def _run_child(self, cmd, **kwargs):
        self.children.append(tuple(str(c) for c in cmd))
        if self.on_child is not None:
            self.on_child(cmd)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    def _prune(self, collection, url):
        self.pruned.append(collection)

    def run(self, *, report=None, spawn_ok: bool = True, update: bool = True):
        self.spawns.clear()
        self.children.clear()
        self.pruned.clear()

        self.spawn_ok = spawn_ok if spawn_ok is not None else self.spawn_ok
        _spawn = self._spawn

        def _fake_subprocess_run(cmd, **kwargs):  # enrichment CLI safety net
            class _R:
                returncode = 0
                stdout = json.dumps({"total": 0, "enriched": 0, "skipped": 0,
                                     "failed": 0, "failures": []})
                stderr = ""

            return _R()

        with mock.patch.object(install, "PROJECT_ROOT", self.tmp), \
                mock.patch.object(install, "_wait_for_weaviate_ready", lambda *a, **k: True), \
                mock.patch.object(install, "_weaviate_awaits_confirmation", lambda *a, **k: False), \
                mock.patch.object(install, "_SERVICE_ENDPOINTS", {}), \
                mock.patch.object(install, "_is_orchestrator_root_install", lambda: False), \
                mock.patch.object(install, "_discover_app_state_db_path",
                                  return_value=self.db_path), \
                mock.patch.object(install, "_prune_stale_kg_rows", self._prune), \
                mock.patch.object(
                    install, "_compute_on_disk_content_hashes",
                    return_value={f"{self.tmp}/knowledge/concepts/a.md": "h"}), \
                mock.patch.object(
                    install, "_batch_query_weaviate_content_hashes",
                    return_value={f"{self.tmp}/knowledge/concepts/a.md": "h"}), \
                mock.patch.object(install, "run_child_logged", side_effect=self._run_child), \
                mock.patch("subprocess.run", side_effect=_fake_subprocess_run), \
                mock.patch("vco_lib.deferral_retry.spawn_detached", side_effect=_spawn):
            install._seed_weaviate(
                _make_args(update=update), deferral_report=report or self.report)


@pytest.fixture()
def seed_env(tmp_path, monkeypatch):
    # The handler writes app_state through `vco_lib.paths.launcher_db_path`,
    # while install.py writes through its (patched) `_discover_app_state_db_path`
    # — point both at the harness's db so the two are comparable.
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("KG_COLLECTION", "TestProject_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "")
    return tmp_path


def test_full_seed_is_enqueued_and_not_awaited(seed_env, capsys):
    """install.py's seed step spawns the driver; it does NOT run the KG seed."""
    report = _report()
    h = _SeedHarness(seed_env)
    h.run(report=report)

    assert len(h.spawns) == 1, "the seed step must hand the work to the driver"
    assert h.spawns[0]["folder"] == seed_env
    kg_children = [c for c in h.children if "sync_knowledge_graph.py" in str(c)]
    assert kg_children == [], (
        "the whole-tree KG seed must not be awaited in the foreground"
    )
    out = capsys.readouterr().out
    assert "background" in out and "deferral-retry-*.log" in out, out


def test_enqueued_seed_carries_the_context_triple_and_leaves_one_owed_row(seed_env):
    """Backend down ⇒ exactly ONE owed row, and the handler gets the triple."""
    from vco_lib import deferral_retry as dr

    report = _report()
    h = _SeedHarness(seed_env)
    h.run(report=report)

    env = h.spawns[0]["env"]
    assert env[dr.SEED_CTX_ENV_ACTIVE_EMBEDDING] == "qwen3"
    assert env[dr.SEED_CTX_ENV_KG_COLLECTION] == "TestProject_KnowledgeGraph"
    assert env[dr.SEED_CTX_ENV_SHARED_KG_COLLECTION] == ""

    owed = [c for c in _on_disk_cids(seed_env) if c == install_cid()]
    assert len(owed) == 1, "the enqueue must not double-emit the owed row"
    assert owed[0] == _install_weaviate.SEED_OWED_WORK_CONDITION_ID


def test_spawn_failure_falls_back_to_the_foreground_seed(seed_env):
    """A driver that cannot launch must never leave the seed silently unwritten."""
    h = _SeedHarness(seed_env)
    h.run(spawn_ok=False)
    assert len(h.spawns) == 1, "the enqueue was attempted (and refused)"
    assert any("sync_knowledge_graph.py" in str(c) for c in h.children), (
        "spawn failure must fall back to running the seed in the foreground"
    )


def test_install_does_not_advance_the_context_triple_on_the_detached_path(seed_env):
    """install.py withholds the triple; the handler stamps it after the seed."""
    h = _SeedHarness(seed_env)
    h.run()
    from vco_lib.launcher_db_writer import read_app_state_key

    assert read_app_state_key(
        h.db_path, "last_installed_kg_collection") == "OldProject_KnowledgeGraph", (
        "the detached path must not advance the context triple in install.py"
    )


def _report():
    from vco_lib.deferral_report import DeferralReport

    return DeferralReport()


def _on_disk_cids(folder: Path) -> "list[str]":
    """The cids in ``folder``'s ON-DISK ledger (v0.2.101 B1: the enqueue writes
    to disk immediately, so this — not the in-memory report — is the record)."""
    from vco_lib.deferral_report import DeferralReport

    return [e.condition_id for e in DeferralReport.read(folder).entries]


def install_cid() -> str:
    return _install_weaviate.SEED_OWED_WORK_CONDITION_ID


# ── the handler's stamping: once, and only after a proven seed ─────────────


def _retry_context(folder: Path):
    from vco_lib import deferral_retry as dr

    (folder / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("# stub\n")
    return dr.RetryContext(
        folder=folder, condition_id="kg_sync_failures_pending",
        backend_probe=lambda *a, **k: True,
        runner=lambda argv, cwd: 0, python="python",
    )


def test_the_context_triple_rule_is_one_home():
    """Gap 6: ONE predicate decides who may record the triple, everywhere.

    ``certified_from_run`` is the whole rule — whole-tree, zero failures, a
    POSITIVELY RESOLVED collection — so install.py, the child, the driver and
    the launcher's Sync cannot hold four opinions about it.
    """
    from vco_lib.kg_context_triple import certified_from_run

    truth = {
        # whole_tree, failures, kg_resolved, orchestrator_root, shared_targeted
        (True, 0, True, True, False): True,     # the ROOT's clean `--all`
        (True, 1, True, True, False): False,    # one failed node: WP-4/SEG-1
        (True, 0, False, True, False): False,   # the literal "KnowledgeGraph"
        (False, 0, True, True, False): False,   # a file-list run cannot speak
        (True, 0, True, False, False): False,   # SF-1: a registered project
        (True, 0, True, True, True): False,     # a shared-targeted pass
        (True, 3, False, True, False): False,
        (False, 1, True, False, True): False,
    }
    for args, expected in truth.items():
        assert certified_from_run(
            whole_tree=args[0], failures=args[1],
            kg_collection_resolved=args[2], orchestrator_root=args[3],
            shared_targeted=args[4],
        ) is expected, args


def test_the_child_records_the_triple_and_writes_the_three_rows(tmp_path, monkeypatch):
    """The child IS the writer now (Gaps 4 and 5): the same helper every
    seeding entry point reaches — install fg, the driver, the session-start
    driver, the launcher's Sync, migrate-collections, `kg-sync --all`."""
    from vco_lib import kg_context_triple as kt

    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    written: dict = {}
    assert kt.record_from_run(
        whole_tree=True, failures=0, kg_collection_resolved=True,
        orchestrator_root=True, shared_targeted=False,
        kg_collection="Proj_KG", shared_kg_collection="Shared_KG",
        write_key=lambda k, v: written.__setitem__(k, v),
    ) is True
    assert written == {
        kt.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING: "arctic",
        kt.APP_STATE_KEY_LAST_KG_COLLECTION: "Proj_KG",
        kt.APP_STATE_KEY_LAST_SHARED_KG_COLLECTION: "Shared_KG",
        kt.APP_STATE_KEY_LAST_KG_SYNC_AT: written[kt.APP_STATE_KEY_LAST_KG_SYNC_AT],
    }
    assert written[kt.APP_STATE_KEY_LAST_KG_SYNC_AT], "the attempt record rides along"

    # …and a refused run writes NOTHING (the gate is inside this call).
    written.clear()
    assert kt.record_from_run(
        whole_tree=True, failures=1, kg_collection_resolved=True,
        orchestrator_root=True, shared_targeted=False,
        kg_collection="Proj_KG", shared_kg_collection="",
        write_key=lambda k, v: written.__setitem__(k, v),
    ) is False
    assert written == {}


def test_the_active_embedding_profile_chain_is_the_one_home(monkeypatch):
    """env → launcher.db app_state → "qwen3", never empty."""
    from vco_lib import kg_context_triple as kt

    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    assert kt.active_embedding_profile({}) == kt.DEFAULT_ACTIVE_EMBEDDING
    assert kt.active_embedding_profile({"ACTIVE_EMBEDDING": "  Arctic "}) == "arctic"


def test_the_retry_handler_does_not_write_the_triple(tmp_path, monkeypatch):
    """Gap 4/5/6: the handler must NOT compete with the child for the write.

    A proven seed leaves the triple to the child that walked the tree, so a
    session-start driver (no carried context) converges exactly like an
    install-spawned one — and there is no second writer to diverge.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib import kg_context_triple as kt
    from vco_lib.launcher_db_writer import read_app_state_key

    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    db = make_launcher_db(tmp_path / "launcher.db", app_state={})
    ctx = _retry_context(tmp_path)

    with mock.patch.object(dr, "condition_cleared", return_value=True):
        assert dr.retry_kg_seed(ctx).status == dr.RETRIED

    assert read_app_state_key(db, kt.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING) is None, (
        "the handler wrote the context triple — the child owns that row now"
    )


def test_seed_context_from_env_is_none_without_a_carried_context(monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.delenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, raising=False)
    assert dr.seed_context_from_env() is None
    assert dr.seed_context_from_env({dr.SEED_CTX_ENV_ACTIVE_EMBEDDING: "qwen3"}) == {
        "active_embedding": "qwen3", "kg_collection": "", "shared_kg_collection": ""}


def test_spawn_detached_overlays_extra_env(tmp_path):
    from vco_lib import deferral_retry as dr

    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]

    with mock.patch("subprocess.Popen", _FakePopen):
        assert dr.spawn_detached(tmp_path, python="python",
                                 extra_env={"VCT_X": "1"}) is True
    assert captured["env"]["VCT_X"] == "1"
    assert captured["argv"][-2:] == ["--folder", str(tmp_path)]

def test_spawn_detached_never_hands_the_child_a_pipe(tmp_path):
    """v0.2.96 WP-1's deadlock cannot come back through the detached path.

    The v0.2.96 incident (`vco_lib/child_process.py` module docstring, commit
    bc80a567): install.py's KG-seed children inherited stdio while install.py's
    own stdout/stderr were launcher-owned PIPES; nothing drained the child's
    ~64 KB pipe buffer, the child blocked mid-write and install.py blocked in
    `subprocess.run` — the user sat at "Seeding" forever. That needs (a) a PIPE
    stdio and (b) a parent that WAITS. `spawn_detached` has neither: the child
    gets the run's LOG FILE (a regular file, never a pipe) and is never waited
    on, so `test_nothing_can_fill` is structural rather than hopeful.
    """
    import subprocess as _sp

    from vco_lib import deferral_retry as dr

    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            captured["argv"] = argv

        def wait(self, *a, **k):  # pragma: no cover - must never be reached
            captured["waited"] = True
            return 0

    with mock.patch("subprocess.Popen", _FakePopen):
        assert dr.spawn_detached(tmp_path, python="python") is True

    assert not captured.get("waited"), "spawn_detached must never wait on the child"
    for stream in ("stdout", "stderr"):
        assert captured[stream] is not _sp.PIPE, (
            f"{stream} must not be a PIPE: a full pipe buffer is the v0.2.96 wedge"
        )
    assert captured["stderr"] is captured["stdout"], "one merged stream (WP-1)"
    assert captured["stdin"] is _sp.DEVNULL
    if os.name == "posix":
        assert captured.get("start_new_session") is True, "must leave the process group"


# ===========================================================================
# Item 4 (follow-up) — the SHARED-collection seed is enqueued too
# ===========================================================================


class _SharedSeedHarness:
    """Drives install._seed_weaviate_shared_kg_only with fakes."""

    def __init__(self, tmp_path: Path, *, shared: str = "Shared_KG",
                 project: str = "Proj_KG", spawn_ok: bool = True):
        self.tmp = tmp_path
        self.shared, self.project, self.spawn_ok = shared, project, spawn_ok
        scripts = tmp_path / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        self.sync_kg = scripts / "sync_knowledge_graph.py"
        self.sync_kg.write_text("# stub\n")
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        self.venv_py = venv / "python"
        self.venv_py.write_text("#!/bin/sh\n")
        self.spawns: list = []
        self.children: list = []
        #: each foreground child's argv AND the env it was handed
        self.child_envs: list = []
        self.report = _report()

    def run(self):
        def _spawn(folder, *, python="", extra_env=None):
            self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
            return self.spawn_ok

        def _child(cmd, **kwargs):
            self.children.append(tuple(str(c) for c in cmd))
            self.child_envs.append(dict(kwargs.get("env") or {}))

            class _R:
                returncode = 0

            return _R()

        with mock.patch.object(install, "PROJECT_ROOT", self.tmp), \
                mock.patch.object(install, "_is_orchestrator_root_install", lambda: False), \
                mock.patch.object(install, "run_child_logged", side_effect=_child), \
                mock.patch("vco_lib.deferral_retry.spawn_detached", side_effect=_spawn):
            return install._seed_weaviate_shared_kg_only(
                args=_make_args(), venv_py=self.venv_py, sync_kg=self.sync_kg,
                weaviate_url="http://127.0.0.1:9", current_shared_kg=self.shared,
                current_kg_collection=self.project, deferral_report=self.report,
            )


@pytest.fixture()
def shared_env(monkeypatch):
    monkeypatch.setenv("KG_COLLECTION", "Proj_KG")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "Shared_KG")
    monkeypatch.delenv("SHARED_KG_WRITE_DISABLED", raising=False)
    monkeypatch.delenv("SHARED_KG_OPT_OUT", raising=False)


def test_shared_seed_is_enqueued_and_not_awaited(tmp_path, shared_env, capsys):
    """The second blocking `--all` of an install leaves via the driver too."""
    from vco_lib import deferral_retry as dr

    h = _SharedSeedHarness(tmp_path)
    assert h.run() == []
    assert len(h.spawns) == 1, "the shared seed must be enqueued"
    assert h.spawns[0]["env"][dr.SEED_CTX_ENV_SHARED_KG_COLLECTION] == "Shared_KG"
    assert [c for c in h.children if "sync_knowledge_graph.py" in str(c)] == [], (
        "the shared seed must not be awaited in the foreground"
    )
    from vco_lib.deferral_report import DeferralReport

    owed = [e for e in DeferralReport.read(tmp_path).entries
            if e.condition_id == _install_weaviate.SHARED_SEED_OWED_CONDITION_ID]
    assert len(owed) == 1, "the shared enqueue must leave exactly one owed row"
    assert "Shared_KG" in owed[0].title
    out = capsys.readouterr().out
    assert "background" in out and "deferral-retry-*.log" in out, out


def test_shared_seed_spawn_failure_falls_back_to_the_foreground(tmp_path, shared_env):
    h = _SharedSeedHarness(tmp_path, spawn_ok=False)
    assert h.run() == []
    assert len(h.spawns) == 1, "the enqueue was attempted (and refused)"
    assert any("sync_knowledge_graph.py" in str(c) for c in h.children), (
        "a failed spawn must fall back to running the shared seed in the foreground"
    )


def test_shared_enqueue_is_skipped_when_shared_equals_project(tmp_path, monkeypatch):
    """The orchestrator-root shape: one class serves both roles, nothing owed."""
    monkeypatch.setenv("KG_COLLECTION", "Same_KG")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "Same_KG")
    monkeypatch.delenv("SHARED_KG_WRITE_DISABLED", raising=False)
    monkeypatch.delenv("SHARED_KG_OPT_OUT", raising=False)
    h = _SharedSeedHarness(tmp_path, shared="", project="Same_KG")
    assert h.run() == []
    assert h.spawns == []
    from vco_lib.deferral_report import DeferralReport

    assert DeferralReport.read(tmp_path).entries == []


# ── the shared handler ─────────────────────────────────────────────────────


def _shared_ctx(folder: Path, *, rc: int, seen: dict | None = None):
    from vco_lib import deferral_retry as dr

    (folder / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("# stub\n")

    def _runner(argv, cwd):
        if seen is not None:
            from vco_lib.kg_context_triple import SHARED_SEED_ENV

            seen["argv"] = [str(a) for a in argv]
            seen["KG_COLLECTION"] = os.environ.get("KG_COLLECTION")
            seen["KG_BASE_DIR"] = os.environ.get("KG_BASE_DIR")
            seen["KG_SYNC_PROJECT_ROOT"] = os.environ.get("KG_SYNC_PROJECT_ROOT")
            seen["shared_marker"] = os.environ.get(SHARED_SEED_ENV)
        return rc

    return dr.RetryContext(folder=folder, condition_id="kg_sync_shared_pending",
                           backend_probe=lambda *a, **k: True,
                           runner=_runner, python="python")


def test_retry_kg_seed_shared_targets_the_shared_collection(tmp_path, monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Shared_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)
    monkeypatch.delenv("KG_BASE_DIR", raising=False)
    seen: dict = {}
    res = dr.retry_kg_seed_shared(_shared_ctx(tmp_path, rc=0, seen=seen))

    assert res.status == dr.RETRIED
    assert seen["KG_COLLECTION"] == "Shared_KG"
    assert seen["KG_BASE_DIR"] == str(tmp_path)
    assert seen["KG_SYNC_PROJECT_ROOT"] == str(tmp_path)
    assert seen["argv"][-1] == "--all"
    assert os.environ.get("KG_COLLECTION") is None, "the env pin must be restored"

    # non-zero exit → FAILED, no clear (the dispatcher re-reads the ledger).
    res = dr.retry_kg_seed_shared(_shared_ctx(tmp_path, rc=1))
    assert res.status == dr.FAILED


def test_retry_kg_seed_shared_skips_without_a_target(tmp_path, monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.delenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, raising=False)
    res = dr.retry_kg_seed_shared(_shared_ctx(tmp_path, rc=0))
    assert res.status == dr.SKIPPED
    assert "SHARED_KG_COLLECTION" in res.detail


def test_shared_collection_for_prefers_the_carried_context(tmp_path, monkeypatch):
    from vco_lib import deferral_retry as dr

    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"SHARED_KG_COLLECTION": "From_Settings"}}), encoding="utf-8")

    monkeypatch.delenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, raising=False)
    assert dr._shared_collection_for(tmp_path) == "From_Settings"
    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Carried")
    assert dr._shared_collection_for(tmp_path) == "Carried"
    assert dr._shared_collection_for(tmp_path / "nowhere") == "Carried"
    monkeypatch.delenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, raising=False)
    assert dr._shared_collection_for(tmp_path / "nowhere") == ""


# ── the script's narrow paired clear ───────────────────────────────────────


def test_the_shared_clear_is_gated_on_a_shared_targeted_run():
    sync = _load_sync_module()
    try:
        setattr(sync, "COLLECTION_NAME", "Proj_KG")
        setattr(sync, "SHARED_COLLECTION_NAME", "Shared_KG")
        assert sync._targets_shared_collection() is False, (
            "a normal project run must not retire the shared seed's row"
        )
        setattr(sync, "COLLECTION_NAME", "Shared_KG")
        assert sync._targets_shared_collection() is True
        setattr(sync, "SHARED_COLLECTION_NAME", "")
        assert sync._targets_shared_collection() is False
    finally:
        setattr(sync, "COLLECTION_NAME", "")
        setattr(sync, "SHARED_COLLECTION_NAME", "")


def test_the_shared_clear_retires_the_owed_row(tmp_path):
    from vco_lib.deferral_emit import DeferralEntry, emit
    from vco_lib.deferral_report import DeferralReport

    sync = _load_sync_module()
    emit(tmp_path, DeferralEntry(
        condition_id=sync._SYNC_SHARED_CID, title="t", detected="d",
        why_deferred="w", command_to_apply="c"))
    assert any(e.condition_id == sync._SYNC_SHARED_CID
               for e in DeferralReport.read(tmp_path).entries)
    sync._clear_shared_seed_deferral(tmp_path)
    assert not any(e.condition_id == sync._SYNC_SHARED_CID
                   for e in DeferralReport.read(tmp_path).entries)


# ── the metadata-repair projection (v0.2.101 item 4 follow-on) ─────────────


def test_metadata_repair_projection_needs_a_carried_collection(tmp_path, monkeypatch):
    """install.py's THIRD precondition, carried into the handler.

    `kg_metadata_repair_certified` documents it: only a run that TARGETED the
    project's CONFIGURED collection may retire the pass — install.py's arm that
    sets `_sync_all` because no `KG_COLLECTION` resolved would otherwise stamp
    a pass over the script's literal `KnowledgeGraph` fallback. install.py saw
    that in its own env; the detached handler sees it in the context the spawn
    carried, so an absent/empty carried collection must refuse the write.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib import kg_metadata_repair_state as state

    state.write_stamp(tmp_path)  # the child DID write its per-project stamp
    monkeypatch.delenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, raising=False)
    monkeypatch.delenv(dr.SEED_CTX_ENV_KG_COLLECTION, raising=False)
    assert dr.stamp_metadata_repair_from_child(tmp_path) is False

    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "")  # carried, but empty
    assert dr.stamp_metadata_repair_from_child(tmp_path) is False


# ===========================================================================
# v0.2.101 B1 — the owed row must reach DISK before the driver is spawned
# ===========================================================================


def test_the_owed_row_is_on_disk_when_the_spawn_happens(seed_env):
    """B1: the driver reads the ledger ONCE and exits — the row must be there.

    The in-memory ``DeferralReport`` only reaches disk at install.py's
    end-of-``main`` finalize, so a row that waited for it was invisible to the
    driver it was written for: the seed did not start until the next session
    and the "continues in the background" message was false.
    """
    h = _SeedHarness(seed_env)
    h.run()
    assert h.disk_at_spawn == [install_cid()], (
        "the owed row must already be in the ON-DISK ledger at spawn time, "
        f"not only in memory; the spawn saw {h.disk_at_spawn!r}"
    )


def test_the_real_driver_runs_the_seed_from_the_on_disk_ledger(seed_env, monkeypatch):
    """B1 end to end: install writes the row → the REAL dispatcher acts on it.

    No `spawn_detached` mock stands in for the driver here: the row install.py
    wrote to disk is dispatched by `vco_lib.deferral_retry.dispatch` itself,
    with a fake child that does what the real one does (writes its per-project
    stamp, then its paired clear). This is the ordering the previous tests
    could not see.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib import kg_metadata_repair_state as state
    from vco_lib.deferral_emit import resolve_conditions
    from vco_lib.launcher_db_writer import read_app_state_key

    h = _SeedHarness(seed_env)
    h.run()
    assert h.disk_at_spawn == [install_cid()]

    ran: list = []

    def _runner(argv, cwd):
        cmd = tuple(str(a) for a in argv)
        ran.append(cmd)
        state.write_stamp(seed_env)                          # the clean `--all`'s stamp
        # …and the child records WHAT it walked against (Gap 4/5/6): this is
        # the real script's own `_record_context_triple`, called here because a
        # session-start-spawned driver carries no context and must still
        # converge.
        kg_context_triple.record_from_run(
            whole_tree=True, failures=0, kg_collection_resolved=True,
            orchestrator_root=True, shared_targeted=False,
            kg_collection=os.environ.get("KG_COLLECTION", "TestProject_KnowledgeGraph"),
            shared_kg_collection="",
        )
        resolve_conditions(seed_env, (install_cid(),))        # the paired clear
        return 0

    with mock.patch.dict(os.environ, h.spawns[0]["env"], clear=False):
        results = dr.dispatch(seed_env, backend_probe=lambda *a, **k: True,
                              runner=_runner, python="python", single_instance=False)

    assert [r.status for r in results] == [dr.RETRIED], results
    assert [c[-1] for c in ran] == ["--all"], ran
    assert _on_disk_cids(seed_env) == [], "the seed's own clear retires the row"
    # …and exactly one driver pass has anything left to do.
    assert dr.owed_condition_ids(seed_env) == []
    # The triple + the repair row + the attempt record all landed once.
    assert read_app_state_key(h.db_path, kg_context_triple.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING) == "qwen3"
    assert read_app_state_key(
        h.db_path, kg_context_triple.APP_STATE_KEY_LAST_KG_COLLECTION) == "TestProject_KnowledgeGraph"
    assert read_app_state_key(
        h.db_path, _install_weaviate.KG_METADATA_REPAIR_STATE_KEY
    ) == _install_weaviate.KG_METADATA_REPAIR_STAMP
    assert read_app_state_key(h.db_path, kg_context_triple.APP_STATE_KEY_LAST_KG_SYNC_AT)


def test_finalize_never_resurrects_a_row_the_seed_already_cleared(seed_env):
    """B1 ordering A: the seed finishes BEFORE install's finalize write.

    Finalize is the run's only write, and it rebuilds the file from memory
    merged with disk. A row the child cleared must not come back.
    """
    from vco_lib.deferral_emit import resolve_conditions
    from vco_lib.install_deferral_flow import InstallDeferralFlow

    flow = InstallDeferralFlow(seed_env, owned_ids=(), owned_prefixes=())
    h = _SeedHarness(seed_env, report=flow.report)
    h.run()
    assert _on_disk_cids(seed_env) == [install_cid()]

    resolve_conditions(seed_env, (install_cid(),))  # the child's paired clear
    flow.finalize()

    assert _on_disk_cids(seed_env) == [], (
        "finalize wrote a stale in-memory row back over the seed's own clear"
    )


def test_finalize_preserves_the_row_when_the_seed_has_not_run_yet(seed_env):
    """B1 ordering B: the seed finishes AFTER install's finalize write."""
    from vco_lib.deferral_emit import resolve_conditions
    from vco_lib.install_deferral_flow import InstallDeferralFlow

    flow = InstallDeferralFlow(seed_env, owned_ids=(), owned_prefixes=())
    h = _SeedHarness(seed_env, report=flow.report)
    h.run()

    flow.finalize()
    assert _on_disk_cids(seed_env) == [install_cid()], (
        "a seed that has not run yet is still OWED — finalize must keep the row"
    )

    resolve_conditions(seed_env, (install_cid(),))  # …then the seed completes
    assert _on_disk_cids(seed_env) == []


def test_a_successful_foreground_fallback_leaves_no_retry_row(seed_env):
    """S3: a refused spawn that then succeeds must not leave a pending row."""
    from vco_lib.deferral_emit import resolve_conditions
    from vco_lib.install_deferral_flow import InstallDeferralFlow

    flow = InstallDeferralFlow(seed_env, owned_ids=(), owned_prefixes=())
    h = _SeedHarness(seed_env, report=flow.report)
    # the real sync script clears its own paired condition on a clean `--all`
    h.on_child = lambda cmd: resolve_conditions(seed_env, (install_cid(),))
    h.run(spawn_ok=False)

    assert len(h.spawns) == 1, "the enqueue was attempted (and refused)"
    assert any("sync_knowledge_graph.py" in str(c) for c in h.children), (
        "the foreground fallback must have run"
    )
    flow.finalize()
    assert _on_disk_cids(seed_env) == [], (
        "the seed completed in the foreground — a 'retry pending' row must not "
        "survive install's finalize"
    )


# ===========================================================================
# v0.2.101 S1 — the driver env is whitelisted (no KG_COLLECTION leak)
# ===========================================================================


def test_the_shared_seeds_kg_collection_never_reaches_the_driver(tmp_path, shared_env):
    """S1: the driver dispatches EVERY owed row, so a leaked target misroutes."""
    from vco_lib import deferral_retry as dr

    h = _SharedSeedHarness(tmp_path)
    h.run()
    assert len(h.spawns) == 1
    env = h.spawns[0]["env"]
    assert env[dr.SEED_CTX_ENV_SHARED_KG_COLLECTION] == "Shared_KG"
    assert "KG_COLLECTION" not in env, (
        "the shared seed's own target must not travel to the DRIVER: its "
        "per-project handler would hand it to the project's child"
    )
    assert "KG_BASE_DIR" not in env
    # …and the positive form of the same rule: NOTHING travels but the context
    # markers and the embedding pair (no ambient dependency on what else the
    # session exports).
    allowed = set(_install_weaviate._DRIVER_ENV_ALLOW) | {
        dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, dr.SEED_CTX_ENV_KG_COLLECTION,
        dr.SEED_CTX_ENV_SHARED_KG_COLLECTION,
    }
    assert set(env) <= allowed, f"unexpected driver-env keys: {set(env) - allowed}"


def test_a_driver_from_the_shared_spawn_cannot_misroute_a_project_seed(
        tmp_path, shared_env, monkeypatch):
    """…and the behaviour that follows: the child sees NO inherited target."""
    from vco_lib import deferral_retry as dr

    h = _SharedSeedHarness(tmp_path)
    h.run()
    seen: dict = {}
    monkeypatch.delenv("KG_COLLECTION", raising=False)
    with mock.patch.dict(os.environ, h.spawns[0]["env"], clear=False):
        # a PER-PROJECT owed row, handled by the driver the shared seed spawned
        res = dr.retry_kg_seed(_shared_ctx(tmp_path, rc=0, seen=seen))

    assert res.status == dr.RETRIED
    assert seen["KG_COLLECTION"] is None, (
        "the shared spawn leaked KG_COLLECTION into a per-project seed's child"
    )
    assert seen["KG_SYNC_PROJECT_ROOT"] == str(tmp_path)


# ===========================================================================
# v0.2.101 S2 — a node archived SINCE its last sync must lose its row
# ===========================================================================


def _archived_removal_world(tmp_path):
    knowledge = tmp_path / "knowledge"
    (knowledge / "concepts").mkdir(parents=True, exist_ok=True)
    (knowledge / "archive").mkdir(parents=True, exist_ok=True)
    active = knowledge / "concepts" / "live.md"
    active.write_text("---\nstatus: active\n---\nx\n", encoding="utf-8")
    by_status = knowledge / "concepts" / "flipped.md"
    by_status.write_text("---\nstatus: archived\n---\nx\n", encoding="utf-8")
    by_path = knowledge / "archive" / "moved.md"
    by_path.write_text("---\ntitle: Moved\n---\nx\n", encoding="utf-8")
    return knowledge, active, by_status, by_path


def test_a_node_archived_since_its_last_sync_is_a_removal(tmp_path):
    """S2: the walk skips it (item 3) and the prune cannot see it (the file
    exists) — so the diff must hand it to the sync's delete-prior-row leg."""
    knowledge, active, by_status, by_path = _archived_removal_world(tmp_path)
    on_disk = _install_weaviate._compute_on_disk_content_hashes(knowledge)
    assert str(by_status) not in on_disk and str(by_path) not in on_disk

    stored = {
        str(active): on_disk[str(active)],
        str(by_status): "hash-from-its-last-sync",
        str(by_path): "hash-from-its-last-sync",
    }
    diff = _install_weaviate.content_hash_diff(
        on_disk, stored, tmp_path, knowledge_root=knowledge)

    assert str(by_status) in diff, "a status-archived node's live row must be removed"
    assert str(by_path) in diff, "a path-archived node's live row must be removed"
    assert str(active) not in diff, "an unchanged active node stays out of the changelist"


def test_an_archived_node_with_no_stored_row_is_not_a_removal(tmp_path):
    """Nothing to delete ⇒ nothing in the changelist (no spurious work)."""
    knowledge, active, by_status, by_path = _archived_removal_world(tmp_path)
    on_disk = _install_weaviate._compute_on_disk_content_hashes(knowledge)
    diff = _install_weaviate.content_hash_diff(
        on_disk, {str(active): on_disk[str(active)]}, tmp_path, knowledge_root=knowledge)
    assert diff == []


# ===========================================================================
# v0.2.101 SF-1 — BOTH enqueued seeds must start in the SAME install
# ===========================================================================


def _both_rows_world(folder: Path) -> "tuple[str, str]":
    """The install's shape: the project row, then (seconds later) the shared row."""
    from vco_lib.deferral_emit import DeferralEntry, emit

    scripts = folder / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "sync_knowledge_graph.py").write_text("# stub\n", encoding="utf-8")
    proj = _install_weaviate.SEED_OWED_WORK_CONDITION_ID
    shared = _install_weaviate.SHARED_SEED_OWED_CONDITION_ID
    emit(folder, DeferralEntry(
        condition_id=proj, title="project seed owed", detected="d",
        why_deferred="w", command_to_apply="c"))
    return proj, shared


def test_a_row_enqueued_while_the_first_pass_runs_is_still_served(seed_env, monkeypatch):
    """SF-1 end to end: ONE driver settles BOTH rows.

    install.py writes the per-project row and spawns a driver, then writes the
    shared row seconds later and spawns a SECOND driver — which the first one's
    pidfile lock blocks. So the first driver must re-read the ledger after its
    pass, or the shared seed waits for the next session while the install prints
    that it continues in the background. Modelled faithfully: the second row is
    written by the first child's own run.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib.deferral_emit import DeferralEntry, emit, resolve_conditions

    proj, shared = _both_rows_world(seed_env)
    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Shared_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)

    served: "list[str]" = []

    def _runner(argv, cwd):
        # the shared handler is the one that sets KG_COLLECTION for its child
        cid = (shared if os.environ.get("KG_COLLECTION") == "Shared_KG" else proj)
        served.append(cid)
        if len(served) == 1:
            # …install.py's step 7d, seconds into the first seed
            emit(seed_env, DeferralEntry(
                condition_id=shared, title="shared seed owed", detected="d",
                why_deferred="w", command_to_apply="c"))
        resolve_conditions(seed_env, (cid,))  # the child's own paired clear
        return 0

    results = dr.dispatch(seed_env, backend_probe=lambda *a, **k: True,
                          runner=_runner, python="python", single_instance=False)

    assert served == [proj, shared], (
        f"one driver must serve BOTH rows; it served {served!r}"
    )
    assert [r.status for r in results] == [dr.RETRIED, dr.RETRIED]
    assert dr.owed_condition_ids(seed_env) == []


def test_the_settle_loop_never_re_attempts_the_same_row(seed_env, monkeypatch):
    """The bound must not eat the attempt cap: one pass per cid, per driver."""
    from vco_lib import deferral_retry as dr
    from vco_lib.deferral_emit import resolve_conditions

    proj, _shared = _both_rows_world(seed_env)
    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)

    calls: "list[str]" = []

    def _runner(argv, cwd):
        calls.append(str(cwd))
        resolve_conditions(seed_env, (proj,))  # clear, so the row stops being owed
        return 0

    dr.dispatch(seed_env, backend_probe=lambda *a, **k: True, runner=_runner,
                python="python", single_instance=False)
    assert len(calls) == 1, "the same row must not be dispatched twice in one driver"
    assert dr.attempt_count(seed_env, proj) == 1, (
        "a second pass must not burn another attempt for a row this driver ran"
    )


def test_the_driver_env_cannot_inherit_a_foreign_kg_target(tmp_path, monkeypatch):
    """N-1: strip the KG targets a caller's shell may have exported."""
    from vco_lib import deferral_retry as dr

    for key, value in (("KG_COLLECTION", "OtherProject_KG"),
                       ("SHARED_KG_COLLECTION", "OtherShared_KG"),
                       ("KG_BASE_DIR", "/elsewhere"),
                       ("KG_SYNC_PROJECT_ROOT", "/elsewhere")):
        monkeypatch.setenv(key, value)

    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            captured["argv"] = argv

    with mock.patch("subprocess.Popen", _FakePopen):
        assert dr.spawn_detached(
            tmp_path, python="python", extra_env={"VCT_KG_SEED_CTX_KG_COLLECTION": "X"}
        ) is True

    for key in ("KG_COLLECTION", "SHARED_KG_COLLECTION", "KG_BASE_DIR",
                "KG_SYNC_PROJECT_ROOT"):
        assert key not in captured["env"], (
            f"{key} reaches the driver from the caller's shell — the handlers "
            "must resolve the target from --folder"
        )
    assert captured["env"]["VCT_KG_SEED_CTX_KG_COLLECTION"] == "X", (
        "an explicitly carried context must survive the strip"
    )


def test_the_last_kg_sync_at_mirror_is_parity_locked():
    """N-4: the two writers of `last_kg_sync_at` must name ONE row.

    A comment is not a lock: each side was pinned separately with no equality
    assertion, so the literal could drift apart unnoticed.
    """
    # (the key names live in the ONE home now — deferral_retry re-exports none)
    assert install._APP_STATE_KEY_LAST_KG_SYNC_AT == kg_context_triple.APP_STATE_KEY_LAST_KG_SYNC_AT


def test_a_failed_row_is_attempted_once_per_driver(seed_env, monkeypatch):
    """NF-1: the FAILED case is what the `attempted` set exists for.

    A FAILED row STAYS in the ledger — that is the retry — so every later
    settle pass sees it again. Without the filter one driver would burn its
    whole ``MAX_ATTEMPTS`` cap in a single session of a down backend, and the
    row would go permanently SKIPPED after one driver instead of after three
    sessions' worth of genuine retries.
    """
    from vco_lib import deferral_retry as dr

    proj, _shared = _both_rows_world(seed_env)
    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)

    calls: "list[int]" = []

    def _runner(argv, cwd):
        calls.append(1)
        return 1  # FAILED: the row stays owed, so a later pass would see it

    results = dr.dispatch(seed_env, backend_probe=lambda *a, **k: True,
                          runner=_runner, python="python", single_instance=False)

    assert len(calls) == 1, (
        "one driver must attempt an owed row ONCE, even though it stays owed — "
        f"got {len(calls)} child run(s)"
    )
    assert [r.status for r in results] == [dr.FAILED], results
    assert dr.attempt_count(seed_env, proj) == 1, (
        "three settle passes over a still-owed row must not consume the whole "
        "MAX_ATTEMPTS cap in one driver run"
    )
    assert dr.owed_condition_ids(seed_env) == [proj], (
        "the FAILED row is still owed — the next session retries it"
    )


def test_a_failed_row_is_not_re_attempted_when_a_new_row_arrives(seed_env, monkeypatch):
    """NF-1, the sharp case: an owed FAILED row PLUS a row written later.

    After pass 1 the ledger holds BOTH — the failed project row (still owed,
    because that is the retry) and the shared row install.py wrote during that
    pass. The `attempted` filter is what keeps pass 2 to the NEW row. Without
    it the driver re-runs the failed one, so a down backend plus install's two
    enqueues would consume the whole ``MAX_ATTEMPTS`` cap in one driver run and
    leave the row permanently SKIPPED.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib.deferral_emit import DeferralEntry, emit, resolve_conditions

    proj, shared = _both_rows_world(seed_env)
    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Shared_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)

    served: "list[str]" = []

    def _runner(argv, cwd):
        cid = shared if os.environ.get("KG_COLLECTION") == "Shared_KG" else proj
        served.append(cid)
        if cid == proj:
            if len(served) == 1:
                # …install.py's step 7d, seconds into the failing project seed
                emit(seed_env, DeferralEntry(
                    condition_id=shared, title="shared seed owed", detected="d",
                    why_deferred="w", command_to_apply="c"))
            return 1  # FAILED: the project row STAYS owed
        resolve_conditions(seed_env, (shared,))  # the shared seed succeeds
        return 0

    results = dr.dispatch(seed_env, backend_probe=lambda *a, **k: True,
                          runner=_runner, python="python", single_instance=False)

    assert served == [proj, shared], (
        f"pass 2 must serve only the NEW row; the child ran for {served!r}"
    )
    assert dr.attempt_count(seed_env, proj) == 1, (
        "the still-owed FAILED row was re-attempted in the settle pass"
    )
    assert dr.attempt_count(seed_env, shared) == 1
    assert [r.status for r in results] == [dr.FAILED, dr.RETRIED], results
    assert dr.owed_condition_ids(seed_env) == [proj], "still owed: the retry"


def test_install_does_not_stamp_the_triple_on_a_foreground_whole_tree_run(seed_env):
    """Gap 6: the whole-tree shapes belong to the child, even in the foreground.

    The spawn-failure fallback runs `sync_knowledge_graph.py --all` inline; the
    conftest's fake child does not record anything (the real one does). If
    install.py still wrote the triple here, a whole-tree run would have TWO
    writers with different evidence — the divergence Gap 6 asks to settle.
    """
    from vco_lib.launcher_db_writer import read_app_state_key

    h = _SeedHarness(seed_env)
    # a STALE stored profile, so "install.py wrote it" is observable as a change
    make_launcher_db(h.db_path, app_state={
        "last_installed_active_embedding": "arctic",
        "last_installed_kg_collection": "Old_KG",
        "last_installed_shared_kg_collection": "",
    })
    h.run(spawn_ok=False, update=False)  # fresh install → _sync_all → fg fallback

    assert any("sync_knowledge_graph.py" in str(c) for c in h.children), (
        "the fallback must have run the whole-tree seed"
    )
    assert read_app_state_key(
        h.db_path, kg_context_triple.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING) == "arctic", (
        "install.py stamped the triple for a whole-tree run — the CHILD owns it "
        "(one writer per fact)"
    )


def test_the_key_names_are_parity_locked_to_install_py():
    """Four rows, two naming sites. A comment is not a lock (Gap 6 / N-4)."""
    assert (install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING
            == kg_context_triple.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING)
    assert (install._APP_STATE_KEY_LAST_KG_COLLECTION
            == kg_context_triple.APP_STATE_KEY_LAST_KG_COLLECTION)
    assert (install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION
            == kg_context_triple.APP_STATE_KEY_LAST_SHARED_KG_COLLECTION)
    assert (install._APP_STATE_KEY_LAST_KG_SYNC_AT
            == kg_context_triple.APP_STATE_KEY_LAST_KG_SYNC_AT)


def test_the_retry_handler_pins_the_childs_collection_from_the_carried_context(
        tmp_path, monkeypatch):
    """Gap 3: the driver's own env is stripped, so the CARRIED value is the pin.

    Without it the child resolves its class through the hub alone and, in a
    hub-down window, `_resolve_collections` falls back to the literal
    "KnowledgeGraph" — a whole-tree walk into a class nobody reads.
    """
    from vco_lib import deferral_retry as dr

    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KG")
    monkeypatch.delenv("KG_COLLECTION", raising=False)  # the stripped driver env
    seen: dict = {}

    res = dr.retry_kg_seed(_shared_ctx(tmp_path, rc=0, seen=seen))

    assert res.status == dr.RETRIED
    assert seen["KG_COLLECTION"] == "Proj_KG", (
        "the child must be handed the carried collection — hub-down would "
        "otherwise walk into the literal KnowledgeGraph fallback"
    )
    assert os.environ.get("KG_COLLECTION") is None, "the pin must be restored"

    # …and with nothing carried (a session-start driver) it pins nothing, so the
    # hub stays the only source rather than a stale one.
    monkeypatch.delenv(dr.SEED_CTX_ENV_KG_COLLECTION, raising=False)
    monkeypatch.delenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, raising=False)
    seen.clear()
    dr.retry_kg_seed(_shared_ctx(tmp_path, rc=0, seen=seen))
    assert seen["KG_COLLECTION"] is None


def test_the_script_records_only_a_positively_resolved_collection():
    """Gap 6's third precondition, at the script's own call site.

    The sync script is the ONE writer every seeding entry point reaches, so its
    gate is the one that matters: a run that fell back to the literal
    "KnowledgeGraph" (nothing configured, hub down) must record NOTHING — the
    class it walked is not one the user reads.
    """
    from vco_lib import kg_context_triple as kt

    sync = _load_sync_module()
    recorded: "list[tuple]" = []

    def _capture(*args, **kwargs):
        recorded.append((args, kwargs))
        return True

    try:
        with mock.patch.object(kt, "record", side_effect=_capture):
            setattr(sync, "_KG_COLLECTION_RESOLVED", True)
            sync._record_context_triple(0)
            setattr(sync, "_KG_COLLECTION_RESOLVED", False)
            sync._record_context_triple(0)
    finally:
        setattr(sync, "_KG_COLLECTION_RESOLVED", True)

    assert len(recorded) == 1, (
        "a run that never positively resolved its collection recorded the "
        f"context anyway ({len(recorded)} write(s))"
    )


# ── the active-embedding chain: one home, two entry points ────────────────


def test_the_none_sentinel_variant_shares_the_one_chain(tmp_path, monkeypatch):
    """install.py's sentinel rides the SAME chain — no second copy to drift."""
    from vco_lib import kg_context_triple as kt

    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    empty = make_launcher_db(tmp_path / "empty.db", app_state={})
    assert kt.active_embedding_profile_or_none(db_path=empty) is None
    assert kt.active_embedding_profile(db_path=empty) == kt.DEFAULT_ACTIVE_EMBEDDING

    hw = make_launcher_db(tmp_path / "hw.db", app_state={
        "default_text_embedding": "snowflake-arctic-embed2:latest"})
    assert kt.active_embedding_profile_or_none(db_path=hw) == "arctic"

    both = make_launcher_db(tmp_path / "both.db", app_state={
        "embedding.active_profile": "openai",
        "default_text_embedding": "snowflake-arctic-embed2:latest"})
    assert kt.active_embedding_profile_or_none(db_path=both) == "openai", (
        "an explicit profile row must win over the hardware derive"
    )

    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    assert kt.active_embedding_profile_or_none(db_path=hw) == "qwen3", (
        "the env override must win over every DB leg"
    )


def test_install_resolves_the_hardware_pick_the_embedder_uses(tmp_path, monkeypatch):
    """The divergence that motivated this: install.py stopped one leg early.

    A hardware-pick-only box (no ``ACTIVE_EMBEDDING``, no
    ``embedding.active_profile``) used to resolve ``None`` → qwen3 in install.py
    while the embedder resolved arctic — the recorded context and the work
    disagreed. install.py now reads the one chain, so the seed/ingest context
    matches what the embedder uses, and the subprocess threader carries it.
    """
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    make_launcher_db(tmp_path / "launcher.db", app_state={
        "default_text_embedding": "snowflake-arctic-embed2:latest"})

    assert install._resolve_active_embedding_for_install() == "arctic", (
        "install.py must resolve what the embedder resolves"
    )
    env = install._subprocess_env_with_embedding()
    assert env["ACTIVE_EMBEDDING"] == "arctic", (
        "the seed child must be handed the profile install.py recorded"
    )
    assert env["EMBEDDING_MODEL"] == "snowflake-arctic-embed2:latest"

    # …and the sentinel still says "nothing configured" on a bare box.
    (tmp_path / "launcher.db").unlink()
    make_launcher_db(tmp_path / "launcher.db", app_state={})
    assert install._resolve_active_embedding_for_install() is None


# ===========================================================================
# v0.2.101 SF-1 / SF-2 — WHO may record, pinned through the real main() --all
#
# The harness is the existing one from the D17/D18 suite (a tmp project root,
# env isolation, the SHIPPED script loaded as a module, faked backends, and
# ``run_main``) — imported rather than copied so the two suites cannot drift
# about what "a clean --all run" means.
# ===========================================================================

from tests.test_v0292_d17_d18_kg_sync_failures_recovery import (  # noqa: E402
    _SyncTestBase,
    _write_node,
)


class ContextTripleFromMainTest(_SyncTestBase):
    """SF-2 (the call site) + SF-1 (its ROOT scope), end to end through main()."""

    def _run_all(self, mod, *, shared_seed: bool = False) -> dict:
        written: dict = {}
        if shared_seed:
            os.environ[kg_context_triple.SHARED_SEED_ENV] = "1"
        try:
            with mock.patch(
                "vco_lib.launcher_db_writer.write_app_state_key",
                side_effect=lambda db, key, value: written.__setitem__(key, value),
            ):
                code, out, err = self.run_main(mod, ["kg-sync", "--all"])
        finally:
            os.environ.pop(kg_context_triple.SHARED_SEED_ENV, None)
        assert code == 0, f"a clean --all must exit 0; stderr tail: {err[-400:]!r}"
        return written

    def _make_orchestrator_tree(self) -> None:
        (self.root / "vct-module.json").write_text(
            json.dumps({"id": "orchestrator"}), encoding="utf-8")

    def test_the_root_trees_clean_all_records_the_triple(self):
        """SF-2: the load-bearing call site, through the real main().

        Deleting ``_record_context_triple()`` from main()'s zero-failure
        ``--all`` branch leaves nothing written — which is what this asserts.
        """
        self._make_orchestrator_tree()
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        mod = self.load()
        self.install_working_backends(mod)

        written = self._run_all(mod)

        assert written.get(kg_context_triple.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), (
            "the ROOT's clean --all must record the profile it embedded with"
        )
        assert (written[kg_context_triple.APP_STATE_KEY_LAST_KG_COLLECTION]
                == mod.COLLECTION_NAME)
        assert written[kg_context_triple.APP_STATE_KEY_LAST_KG_SYNC_AT], (
            "the attempt record must ride along"
        )

    def test_a_registered_projects_clean_all_records_nothing(self):
        """SF-1: the row is machine-global and describes the ROOT's install.

        A registered project's own clean ``--all`` (launcher Sync, session-start
        repair, hand-run) used to stamp its class here, and the next root update
        read that as a context change and paid a full walk.
        """
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        mod = self.load()  # no orchestrator markers ⇒ a registered project
        self.install_working_backends(mod)

        assert self._run_all(mod) == {}, (
            "a non-root project recorded the root's context triple"
        )

    def test_a_shared_targeted_run_never_records_it(self):
        """The nit's guard: a shared pass is not about the project's context.

        Decided by an explicit marker, NOT by comparing collection names — on
        the orchestrator root ``KG_COLLECTION == SHARED_KG_COLLECTION`` by
        design, so a name test would also suppress the root's own recording.
        """
        self._make_orchestrator_tree()
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        mod = self.load()
        self.install_working_backends(mod)

        assert self._run_all(mod, shared_seed=True) == {}, (
            "a shared-targeted run recorded the per-project context triple"
        )


# ── the shared marker, pinned at BOTH spawners (batch-7 finding 1) ────────


def test_the_shared_foreground_child_carries_the_marker(tmp_path, shared_env):
    """Spawner 1 of 2: `install_weaviate.kg_seed_step` (the fg fallback child).

    The marker is what stops the child recording the PER-PROJECT triple for a
    shared pass; without this pin, deleting the assignment leaves the suite
    green and the guard one edit away from silent removal.
    """
    from vco_lib.kg_context_triple import SHARED_SEED_ENV

    h = _SharedSeedHarness(tmp_path, spawn_ok=False)  # refused spawn → fg child
    h.run()
    assert h.child_envs, "the foreground fallback child must have run"
    assert h.child_envs[0].get(SHARED_SEED_ENV) == "1", (
        "the shared pass's child must carry the marker, or it records the "
        "per-project context triple"
    )


def test_the_shared_retry_child_carries_the_marker_and_restores_it(tmp_path, monkeypatch):
    """Spawner 2 of 2: `deferral_retry.retry_kg_seed_shared`.

    Set in-process with save/restore, so it cannot leak into a per-project seed
    the same driver process handles next.
    """
    from vco_lib import deferral_retry as dr
    from vco_lib.kg_context_triple import SHARED_SEED_ENV

    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Shared_KG")
    monkeypatch.delenv(SHARED_SEED_ENV, raising=False)
    seen: dict = {}
    res = dr.retry_kg_seed_shared(_shared_ctx(tmp_path, rc=0, seen=seen))

    assert res.status == dr.RETRIED
    assert seen["shared_marker"] == "1", (
        "the shared retry's child must carry the marker"
    )
    assert SHARED_SEED_ENV not in os.environ, "the marker must be restored"


# ── the REAL root topology: KG == SHARED, no marker (batch-7 finding 2) ────


def test_the_root_with_equal_names_still_records():
    """The dogfood topology: on the orchestrator root the two names are EQUAL.

    A regression to `shared_targeted := (names equal) OR marker` passes every
    other test (they all run `KG != SHARED`) while silently stopping the real
    root's own seed from recording — so this variant pins the coincidence the
    marker exists to decode.
    """
    from vco_lib.kg_context_triple import (
        APP_STATE_KEY_LAST_ACTIVE_EMBEDDING,
        APP_STATE_KEY_LAST_KG_COLLECTION,
        APP_STATE_KEY_LAST_SHARED_KG_COLLECTION,
        SHARED_SEED_ENV,
    )

    # `runTest` is unittest's no-op method name: this borrows the harness's
    # setUp/tearDown + helpers, not one of its tests.
    case = ContextTripleFromMainTest("runTest")
    case.setUp()
    try:
        case._make_orchestrator_tree()
        _write_node(case.root / "knowledge" / "concepts" / "a.md", "Node A")
        with mock.patch.object(sys, "argv", ["kg-sync"]):  # no foreign --project-root
            mod = case.load()
        setattr(mod, "COLLECTION_NAME", "VCODev_KnowledgeGraph")
        setattr(mod, "SHARED_COLLECTION_NAME", "VCODev_KnowledgeGraph")  # equal, by design
        os.environ.pop(SHARED_SEED_ENV, None)  # no shared-target marker

        written: dict = {}
        with mock.patch(
            "vco_lib.launcher_db_writer.write_app_state_key",
            side_effect=lambda db, key, value: written.__setitem__(key, value),
        ):
            mod._record_context_triple(0)

        assert written.get(APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), (
            "the ROOT's own seed must record even though KG == SHARED"
        )
        assert written[APP_STATE_KEY_LAST_KG_COLLECTION] == "VCODev_KnowledgeGraph"
        assert written[APP_STATE_KEY_LAST_SHARED_KG_COLLECTION] == "VCODev_KnowledgeGraph"
    finally:
        case.tearDown()
        case.doCleanups()


# ── a FAILING --all records nothing (batch-7 finding 3) ───────────────────


def test_a_failing_all_run_records_nothing_through_main():
    """The missing half of SF-2, and the pin on the call's PLACEMENT.

    Driven through the real `main() --all` with failing backends: the helper is
    handed the run's REAL failure count, so the RULE refuses even if a later
    edit moves the call out of the `total_fail == 0` branch (the branch is a
    second guard, not the only one).
    """
    case = ContextTripleFromMainTest("runTest")
    case.setUp()
    try:
        case._make_orchestrator_tree()
        _write_node(case.root / "knowledge" / "concepts" / "a.md", "Node A")
        _write_node(case.root / "knowledge" / "concepts" / "b.md", "Node B")
        with mock.patch.object(sys, "argv", ["kg-sync"]):  # no foreign --project-root
            mod = case.load()
        case.install_failing_backends(mod)

        written: dict = {}
        with mock.patch(
            "vco_lib.launcher_db_writer.write_app_state_key",
            side_effect=lambda db, key, value: written.__setitem__(key, value),
        ):
            code, out, err = case.run_main(mod, ["kg-sync", "--all"])

        assert code == 1, f"per-node failures must exit 1; stderr: {err[-300:]!r}"
        assert written == {}, (
            "a run that left nodes unsynced recorded the context triple — the "
            "call must not sit where failures cannot stop it"
        )
    finally:
        case.tearDown()
        case.doCleanups()
