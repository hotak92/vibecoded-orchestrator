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
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import compose_env  # noqa: E402
from vco_lib import install_weaviate as _install_weaviate  # noqa: E402
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

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.spawns: list = []
        self.children: list = []
        self.pruned: list = []
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
        self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
        return True

    def _run_child(self, cmd, **kwargs):
        self.children.append(tuple(str(c) for c in cmd))

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

        _spawn = self._spawn if spawn_ok else (lambda *a, **k: False)

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
            install._seed_weaviate(_make_args(update=update), deferral_report=report)


@pytest.fixture()
def seed_env(tmp_path, monkeypatch):
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

    owed = [e for e in report.entries if e.condition_id == install_cid()]
    assert len(owed) == 1, "the enqueue must not double-emit the owed row"
    assert owed[0].condition_id == _install_weaviate.SEED_OWED_WORK_CONDITION_ID


def test_spawn_failure_falls_back_to_the_foreground_seed(seed_env):
    """A driver that cannot launch must never leave the seed silently unwritten."""
    h = _SeedHarness(seed_env)
    h.run(spawn_ok=False)
    assert h.spawns == []
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


def test_retry_handler_stamps_only_after_a_proven_clear(tmp_path, monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    monkeypatch.setenv(dr.SEED_CTX_ENV_KG_COLLECTION, "Proj_KnowledgeGraph")
    monkeypatch.setenv(dr.SEED_CTX_ENV_SHARED_KG_COLLECTION, "Shared_KG")
    ctx = _retry_context(tmp_path)

    stamps: list = []
    with mock.patch.object(dr, "condition_cleared", return_value=True), \
            mock.patch.object(dr, "stamp_seed_context",
                              side_effect=lambda c, **k: stamps.append(c) or True):
        result = dr.retry_kg_seed(ctx)
    assert result.status == dr.RETRIED
    assert stamps == [{"active_embedding": "qwen3",
                       "kg_collection": "Proj_KnowledgeGraph",
                       "shared_kg_collection": "Shared_KG"}], (
        "the triple must be stamped exactly once, with the carried context"
    )

    # Not proven cleared → no stamp (a zero exit is not proof: `--all`'s
    # no-backend path exits 0 too).
    stamps.clear()
    with mock.patch.object(dr, "condition_cleared", return_value=False), \
            mock.patch.object(dr, "stamp_seed_context",
                              side_effect=lambda c, **k: stamps.append(c) or True):
        dr.retry_kg_seed(ctx)
    assert stamps == []


def test_retry_handler_does_not_stamp_on_a_nonzero_exit(tmp_path, monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.setenv(dr.SEED_CTX_ENV_ACTIVE_EMBEDDING, "qwen3")
    ctx = _retry_context(tmp_path)
    ctx = dr.RetryContext(
        folder=ctx.folder, condition_id=ctx.condition_id,
        backend_probe=ctx.backend_probe, runner=lambda argv, cwd: 1, python="python",
    )
    stamps: list = []
    with mock.patch.object(dr, "condition_cleared", return_value=True), \
            mock.patch.object(dr, "stamp_seed_context",
                              side_effect=lambda c, **k: stamps.append(c) or True):
        result = dr.retry_kg_seed(ctx)
    assert result.status == dr.FAILED
    assert stamps == []


def test_stamp_seed_context_writes_the_three_app_state_keys():
    from vco_lib import deferral_retry as dr

    written: dict = {}
    ok = dr.stamp_seed_context(
        {"active_embedding": "arctic", "kg_collection": "A_KG",
         "shared_kg_collection": "S_KG"},
        write_key=lambda k, v: written.__setitem__(k, v),
    )
    assert ok is True
    assert written == {
        dr.APP_STATE_KEY_LAST_ACTIVE_EMBEDDING: "arctic",
        dr.APP_STATE_KEY_LAST_KG_COLLECTION: "A_KG",
        dr.APP_STATE_KEY_LAST_SHARED_KG_COLLECTION: "S_KG",
    }
    # No carried context (e.g. a session-start driver) ⇒ no stamp at all.
    assert dr.stamp_seed_context(None, write_key=lambda k, v: written.__setitem__(k, v)) is False


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
        self.report = _report()

    def run(self):
        def _spawn(folder, *, python="", extra_env=None):
            self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
            return self.spawn_ok

        def _child(cmd, **kwargs):
            self.children.append(tuple(str(c) for c in cmd))

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
    owed = [e for e in h.report.entries
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
    assert h.spawns == [] and h.report.entries == []


# ── the shared handler ─────────────────────────────────────────────────────


def _shared_ctx(folder: Path, *, rc: int, seen: dict | None = None):
    from vco_lib import deferral_retry as dr

    (folder / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("# stub\n")

    def _runner(argv, cwd):
        if seen is not None:
            seen["argv"] = [str(a) for a in argv]
            seen["KG_COLLECTION"] = os.environ.get("KG_COLLECTION")
            seen["KG_BASE_DIR"] = os.environ.get("KG_BASE_DIR")
            seen["KG_SYNC_PROJECT_ROOT"] = os.environ.get("KG_SYNC_PROJECT_ROOT")
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
