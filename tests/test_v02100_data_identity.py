# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-07 (AD-4; review L1-F01/F08/F20/F21/F24, U11/U12/U19) — a
service is never recreated onto an empty default volume.

Everything runs against FAKES: a scripted container runtime + compose
(``Box.run``: ``inspect`` answers from a dict, ``compose config`` renders the
Ollama mount from the ``infrastructure/.env`` ON DISK exactly like the base
file's ``${VCT_OLLAMA_DATA_SOURCE:-ollama_data}`` knob does) and a real-schema
launcher.db under ``tmp_path``. No real podman/docker, no network.

The owner's machine is the fixture: a ``vco_managed`` Ollama whose live
container binds a host directory of models (``/some/models`` stand-in, 110 GB
in the field) while compose's default is the EMPTY named volume
``vco_ollama_data``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import data_identity as di  # noqa: E402
from vco_lib import service_detection as det  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_lifecycle as sl  # noqa: E402
from vco_lib import service_reconcile as sr  # noqa: E402
from vco_lib.envfile import parse_env_lines  # noqa: E402

TABLE = json.loads((REPO_ROOT / "tests" / "fixtures" / "service_endpoint_parity.json").read_text())
OLLAMA_DEST = "/root/.ollama"
DEFAULT_VOLUME = "vco_ollama_data"


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def _ollama_row(mount: Optional[dict] = None, **kw) -> se.EndpointRow:
    kw.setdefault("container_name", "vco_ollama")
    kw.setdefault("source", "install_probe")
    return se.EndpointRow(service="ollama", mode="vco_managed", port=11435, data_mount=mount, **kw)


def _bind(path) -> dict:
    return {"kind": "bind", "source": str(path), "destination": OLLAMA_DEST}


VOLUME = {"kind": "volume", "source": DEFAULT_VOLUME, "destination": OLLAMA_DEST}


class Box:
    """A fake runtime + compose for the Ollama container ``vco_ollama``."""

    def __init__(self, infra: Path, *, live: Optional[list] = None, exists: bool = True,
                 inspect_error: str = "", config_ignores_env: bool = False):
        self.infra = infra
        self.live = live if live is not None else []
        self.exists = exists
        self.inspect_error = inspect_error
        self.config_ignores_env = config_ignores_env
        self.argv: list[list[str]] = []

    def env_file(self) -> dict:
        path = self.infra / ".env"
        return dict(parse_env_lines(path.read_text(encoding="utf-8"))) if path.is_file() else {}

    def _config(self) -> str:
        env = {} if self.config_ignores_env else self.env_file()
        source = env.get("VCT_OLLAMA_DATA_SOURCE", "")
        if source:
            vol = {"type": "bind", "source": source, "target": OLLAMA_DEST}
        else:
            vol = {"type": "volume", "source": "ollama_data", "target": OLLAMA_DEST}
        name = env.get("VCT_OLLAMA_VOLUME_NAME") or DEFAULT_VOLUME
        import yaml

        return yaml.safe_dump({"services": {"ollama": {"volumes": [vol]}},
                               "volumes": {"ollama_data": {"name": name}}})

    def run(self, argv, **_kw):
        argv = [str(a) for a in argv]
        self.argv.append(argv)
        if argv[-1] == "config":
            return _cp(argv, 0, self._config())
        if argv[1:3] == ["compose", "version"]:
            return _cp(argv, 0, "Docker Compose version v2.30.0\n")
        if "up" in argv:
            return _cp(argv)
        if argv[1] == "inspect":
            if self.inspect_error:
                return _cp(argv, 125, "", self.inspect_error)
            name, fmt = argv[-1], argv[argv.index("--format") + 1]
            if not self.exists or name != "vco_ollama":
                return _cp(argv, 125, "", f"Error: no such container {name}")
            if fmt == "{{.Id}}":
                return _cp(argv, 0, "abc123\n")
            if fmt == "{{json .Mounts}}":
                return _cp(argv, 0, json.dumps(self.live))
        if argv[1] == "rm":
            self.exists = False
            return _cp(argv)
        raise AssertionError(f"unexpected argv {argv}")

    def calls(self, word: str) -> list[list[str]]:
        if word == "config":
            return [a for a in self.argv if a[-1] == "config"]
        if word == "up":
            return [a for a in self.argv if "up" in a and a[-1] != "config"]
        return [a for a in self.argv if len(a) > 1 and a[1] == word]


@pytest.fixture()
def world(tmp_path):
    root = tmp_path / "orch"
    infra = root / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    models = tmp_path / "some" / "models"
    (models / "blobs").mkdir(parents=True)
    (models / "blobs" / "sha256-110GB-stand-in").write_text("x", encoding="utf-8")
    db = make_launcher_db(tmp_path / "launcher.db")
    return root, infra, models, db


def _live_bind(models) -> list:
    return [{"Type": "bind", "Source": str(models), "Destination": OLLAMA_DEST, "Mode": "Z"}]


def _guard(box: Box, infra: Path, db: Path, row: Optional[se.EndpointRow]):
    rows = se.load_rows(db)
    return di.guard_recreate("ollama", runtime="podman", infra_dir=infra,
                             compose_argv=["podman", "compose"], row=row, rows=rows,
                             db_path=db, run=box.run)


# ─── the owner's machine ────────────────────────────────────────────────


def test_owners_ollama_on_a_bind_is_preserved_and_projected_never_the_default_volume(world):
    root, infra, models, db = world
    row = _ollama_row()                       # the U12 state: the mount was lost (NULL)
    se.write_rows([row], db_path=db)
    box = Box(infra, live=_live_bind(models))
    g = _guard(box, infra, db, row)
    assert g.verdict == di.PRESERVES, g.reason
    # recorded BEFORE PRESERVES was answered
    assert g.recorded
    assert se.load_rows(db)["ollama"].data_mount == _bind(models)
    env = box.env_file()
    assert env["VCT_OLLAMA_DATA_SOURCE"] == str(models)
    assert "VCT_OLLAMA_VOLUME_NAME" not in env
    # the compose render was read AFTER the knob landed, and said "bind"
    assert g.effective is not None and g.effective.mount == _bind(models)
    assert box.calls("up") == [] and box.calls("rm") == []


def test_the_up_verb_removes_the_zombie_only_after_the_proof_and_composes_it_alone(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row()], db_path=db)
    box = Box(infra, live=_live_bind(models))
    out: list[str] = []
    res = sl.up_services(["ollama"], recreate=["ollama"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=out.append)
    assert res.returncode == 0, out
    assert res.removed == ["vco_ollama"]
    order = [a[1] if a[1] in ("rm", "inspect") else ("config" if a[-1] == "config" else "up")
             for a in box.argv if a[1:3] != ["compose", "version"]]
    assert order.index("config") < order.index("rm") < order.index("up"), order
    up = box.calls("up")[0]
    assert up[up.index("--no-deps") + 1:] == ["ollama"]
    assert box.env_file()["VCT_OLLAMA_DATA_SOURCE"] == str(models)


# ─── refusals: nothing removed, nothing composed ────────────────────────


def test_recorded_mount_differing_from_the_live_one_refuses_with_no_compose_call(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row(VOLUME)], db_path=db)
    box = Box(infra, live=_live_bind(models))
    g = _guard(box, infra, db, se.load_rows(db)["ollama"])
    assert g.verdict == di.REFUSE_DIFFERENT, g.reason
    assert box.calls("config") == []
    res = sl.up_services(["ollama"], recreate=["ollama"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=lambda _l: None)
    assert res.refused == ["ollama"] and res.returncode == 3
    assert box.calls("rm") == [] and box.calls("up") == [] and box.calls("config") == []
    assert [e.condition_id for e in res.entries] == [di.CID_RECREATE_REFUSED]
    # the recorded mount was not overwritten by the refusal
    assert se.load_rows(db)["ollama"].data_mount == VOLUME


def test_a_compose_that_would_mount_the_default_volume_refuses(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row()], db_path=db)
    box = Box(infra, live=_live_bind(models), config_ignores_env=True)
    res = sl.up_services(["ollama"], recreate=["ollama"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=lambda _l: None)
    assert res.refused == ["ollama"]
    assert box.calls("rm") == [] and box.calls("up") == []


@pytest.mark.parametrize("error", ["Error: cannot connect to Podman socket", "permission denied"])
def test_an_uninspectable_container_refuses_unknown(world, error):
    root, infra, models, db = world
    se.write_rows([_ollama_row()], db_path=db)
    box = Box(infra, live=_live_bind(models), inspect_error=error)
    g = _guard(box, infra, db, se.load_rows(db)["ollama"])
    assert g.verdict == di.REFUSE_UNKNOWN
    assert box.calls("config") == []


def test_a_container_without_a_data_mount_refuses(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row()], db_path=db)
    box = Box(infra, live=[])
    g = _guard(box, infra, db, se.load_rows(db)["ollama"])
    assert g.verdict == di.REFUSE_UNKNOWN and "inside the container" in g.reason


# ─── leave-alone: nothing to lose / recorded data comes along ───────────


def test_no_container_and_no_recorded_mount_is_nothing_to_lose(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row()], db_path=db)
    box = Box(infra, exists=False)
    res = sl.up_services(["ollama"], compose_dir=infra, compose_argv=["podman", "compose"],
                         runtime="podman", db_path=db, run=box.run, out=lambda _l: None)
    assert res.cleared == ["ollama"] and res.returncode == 0
    assert box.calls("config") == [] and len(box.calls("up")) == 1


def test_a_missing_container_with_a_recorded_bind_is_recreated_on_that_bind(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row(_bind(models))], db_path=db)
    box = Box(infra, exists=False)
    g = _guard(box, infra, db, se.load_rows(db)["ollama"])
    assert g.verdict == di.PRESERVES, g.reason
    assert box.env_file()["VCT_OLLAMA_DATA_SOURCE"] == str(models)


def test_the_pure_verdict_table():
    live = di.LiveMount(di.LIVE_PRESENT, "vco_ollama", _bind("/m"))
    same = di.Effective(_bind("/m"))
    other = di.Effective(VOLUME)
    assert di.recreate_preserves_data("ollama", _ollama_row(), live, same)[0] == di.PRESERVES
    assert di.recreate_preserves_data("ollama", _ollama_row(), live, other)[0] == di.REFUSE_DIFFERENT
    assert di.recreate_preserves_data("ollama", _ollama_row(), live, di.Effective(error="x"))[0] \
        == di.REFUSE_UNKNOWN
    assert di.recreate_preserves_data("ollama", _ollama_row(VOLUME), live, same)[0] == di.REFUSE_DIFFERENT
    absent = di.LiveMount(di.LIVE_ABSENT)
    assert di.recreate_preserves_data("ollama", _ollama_row(), absent, None)[0] == di.NOTHING_TO_LOSE
    assert di.recreate_preserves_data("ollama", _ollama_row(VOLUME), absent, di.Effective(VOLUME))[0] \
        == di.PRESERVES


# ─── never-NULL mounts (U12) ────────────────────────────────────────────


@pytest.mark.parametrize("case", TABLE["mount_write_cases"], ids=lambda c: c["name"])
def test_mount_write_cases(tmp_path, case):
    db = make_launcher_db(tmp_path / "launcher.db")
    if case["prior"] is not None:
        se.write_rows([_ollama_row(case["prior"]["data_mount"])], db_path=db)
    se.write_rows([_ollama_row(case["write"], source="live_reconcile")], db_path=db)
    assert se.load_rows(db)["ollama"].data_mount == case["expect"]


def test_only_an_explicit_clear_stores_null_over_a_recorded_mount(tmp_path):
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_ollama_row(VOLUME)], db_path=db)
    se.write_rows([_ollama_row(None, source="user_cli")], db_path=db, clear_mount=["weaviate"])
    assert se.load_rows(db)["ollama"].data_mount == VOLUME
    se.write_rows([_ollama_row(None, source="user_cli")], db_path=db, clear_mount=True)
    assert se.load_rows(db)["ollama"].data_mount is None


def test_u12_replay_no_null_mount_reaches_the_store(tmp_path):
    """The v0.2.98 adoption inputs replayed: a row + container on a bind,
    then a reconcile after quit (nothing live, nothing listed). Records the
    mechanism: every row the reconcile hands to ``write_rows`` for Ollama
    carries the mount, and the stored row keeps it."""
    from tests.test_v0297_service_reconcile import FakeMachine, run_case

    models = tmp_path / "models"
    models.mkdir()
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_ollama_row(project_mount := _bind(models))], db_path=db)
    handed: list = []
    real = se.write_rows

    def spy(rows, **kw):
        rows = list(rows)
        handed.extend(r for r in rows if r.service == "ollama")
        return real(rows, **kw)

    with mock.patch.object(se, "write_rows", spy):
        # run 1: the container is there, on the bind, VCO's project
        container = {"name": "vco_ollama", "image": "docker.io/ollama/ollama:latest",
                     "project": "infrastructure", "ports": {11434: 11435},
                     "mounts": [{"Type": "bind", "Source": str(models), "Destination": OLLAMA_DEST}]}
        run_case(tmp_path, {}, db=db, machine=FakeMachine([container]))
        # run 2: after quit — nothing listed, nothing answers
        run_case(tmp_path, {}, db=db, machine=FakeMachine([]))
        run_case(tmp_path, {"phase": "session"}, db=db, machine=FakeMachine([]))
    assert handed, "the reconcile wrote no ollama row"
    assert all(r.data_mount == project_mount for r in handed), [r.data_mount for r in handed]
    assert se.load_rows(db)["ollama"].data_mount == project_mount


# ─── the reconcile keeps VCO's own rows current (U19, L1-F08) ───────────


def _container(project: str, mount_source: str, **kw) -> det.ContainerInfo:
    return det.ContainerInfo(
        name="vco_ollama", image="ollama/ollama", state=kw.pop("state", "running"),
        labels={"com.docker.compose.project": project},
        host_ports={11434: 11435},
        mounts=(det.Mount("bind", mount_source, OLLAMA_DEST),))


@pytest.mark.parametrize("session", [False, True])
def test_container_drift_refreshes_a_vco_managed_row_project_and_mount(session):
    row = _ollama_row(VOLUME, compose_project="vibecoded")
    inp = sr.ServiceInputs(service="ollama", existing=row,
                           containers=[_container("infrastructure", "/some/models")],
                           containers_listed=True, session=session)
    out = sr.decide(inp)
    assert out.row.compose_project == "infrastructure"
    assert out.row.data_mount == _bind("/some/models")
    assert out.row.port == 11435 and out.row.mode == "vco_managed"


def test_a_vco_managed_row_is_not_moved_to_the_containers_port():
    row = _ollama_row(_bind("/m"))
    c = det.ContainerInfo(name="vco_ollama", image="ollama/ollama", state="running",
                          labels={"com.docker.compose.project": "infrastructure"},
                          host_ports={11434: 19999},
                          mounts=(det.Mount("bind", "/m", OLLAMA_DEST),))
    out = sr.decide(sr.ServiceInputs(service="ollama", existing=row, containers=[c],
                                     containers_listed=True))
    assert out.row.port == 11435


# ─── `--service ollama=vco` / use-vco-copy keep the recorded mount ──────


def test_service_flag_vco_keeps_the_recorded_mount():
    row = _ollama_row(_bind("/some/models"))
    out = sr.decide(sr.ServiceInputs(service="ollama", existing=row,
                                     choice=sr.Choice("ollama", "vco", None),
                                     port_free=lambda _p: True))
    assert out.row.data_mount == _bind("/some/models")
    assert not out.clear_mount


def test_vco_copy_never_inherits_an_adopted_containers_mount(tmp_path):
    adopted = se.EndpointRow(service="ollama", mode="adopted_container", port=11434,
                             source="user_cli", container_name="their_ollama",
                             data_mount=_bind("/their/models"))
    out = sr.decide(sr.ServiceInputs(service="ollama", existing=adopted,
                                     choice=sr.Choice("ollama", "vco", None),
                                     taken_ports=frozenset({11434}), port_free=lambda _p: True))
    assert out.row.data_mount is None and out.clear_mount
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([adopted], db_path=db)
    se.write_rows([out.row], db_path=db, clear_mount=[s for s in ["ollama"] if out.clear_mount])
    assert se.load_rows(db)["ollama"].data_mount is None


# ─── the Ollama holds-models guard (L1-F20) ─────────────────────────────


class _Tags:
    def __init__(self, models_by_port: dict):
        self.models = models_by_port

    def __call__(self, url: str, _t: float):
        port = int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        if url.endswith("/api/tags") and port in self.models:
            return det.HttpResponse(200, json.dumps({"models": [{"name": m} for m in self.models[port]]}))
        return None


def _adopted_ollama(port=11434) -> se.EndpointRow:
    return se.EndpointRow(service="ollama", mode="adopted_external", port=port, source="user_cli")


def test_use_vco_copy_refuses_leaving_an_ollama_that_holds_models(tmp_path):
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_adopted_ollama()], db_path=db)
    out: list[str] = []
    rc = sr.use_vco_copy("ollama", orchestrator_root=tmp_path, db_path=db,
                         fetch=_Tags({11434: ["llama3:70b"]}), port_free=lambda _p: True,
                         out=out.append, apply_kwargs={})
    assert rc == 1 and "--accept-empty" in out[-1]
    assert se.load_rows(db)["ollama"].mode == "adopted_external"


def test_use_vco_copy_with_accept_empty_switches(tmp_path):
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_adopted_ollama()], db_path=db)
    kw = {"write_infra_env": lambda *_a: None, "reproject": lambda _d: None,
          "register_mcps": lambda _r: True}
    rc = sr.use_vco_copy("ollama", orchestrator_root=tmp_path, db_path=db, accept_empty_kg=True,
                         fetch=_Tags({11434: ["llama3:70b"]}), port_free=lambda _p: True,
                         out=lambda _l: None, apply_kwargs=kw)
    assert rc == 0 and se.load_rows(db)["ollama"].mode == "vco_managed"


def test_use_vco_copy_on_vcos_own_ollama_is_not_a_switch_away(tmp_path):
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_ollama_row(_bind("/some/models"))], db_path=db)
    kw = {"write_infra_env": lambda *_a: None, "reproject": lambda _d: None,
          "register_mcps": lambda _r: True}
    rc = sr.use_vco_copy("ollama", orchestrator_root=tmp_path, db_path=db,
                         fetch=_Tags({11435: ["qwen3-embedding:0.6b"]}), port_free=lambda _p: True,
                         out=lambda _l: None, apply_kwargs=kw)
    assert rc == 0
    assert se.load_rows(db)["ollama"].data_mount == _bind("/some/models")


def test_the_cli_accepts_accept_empty():
    ns = se._build_arg_parser().parse_args(["use-vco-copy", "--service", "ollama", "--accept-empty"])
    assert ns.accept_empty_kg is True


# ─── install.py step 5: the guard's refusal reaches the compose argv ─────


def test_step5_drops_a_refused_service_from_the_compose_call(monkeypatch):
    import install

    seen = {}

    def fake_run(cmd, **_kw):
        if "up" in cmd:
            seen["cmd"] = list(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS",
                        {"rows": {}, "pinned": False, "weaviate_pending": False})
    calls = {}

    def fake_guard(services, **kw):
        calls["services"] = list(services)
        return {"ollama"}

    sysinfo = mock.Mock(container_cmd="podman", has_gpu=False, gpu_vendor=None)
    with mock.patch.object(install, "_detect_existing_services",
                           return_value={"weaviate_url": None, "ollama_url": None, "code_embed_url": None}), \
         mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
         mock.patch.object(install, "_write_infrastructure_env"), \
         mock.patch.object(install, "_get_compose_command", return_value=["podman", "compose"]), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         mock.patch.object(di, "guard_compose_set", side_effect=fake_guard), \
         mock.patch.object(install.subprocess, "run", side_effect=fake_run):
        install._start_services(sysinfo, argparse.Namespace(update=True), {}, decisions=None)
    assert calls["services"] == ["weaviate", "ollama"]
    up = seen["cmd"][seen["cmd"].index("--no-deps") + 1:]
    assert up == ["weaviate"]


def test_guard_compose_set_ledgers_and_prints_a_refusal(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row(VOLUME)], db_path=db)
    box = Box(infra, live=_live_bind(models))

    class Ledger:
        entries: list = []

        def add_entry(self, e):
            self.entries.append(e)

    ledger, printed = Ledger(), []
    refused = di.guard_compose_set(["ollama"], runtime="podman", infra_dir=infra,
                                   compose_argv=lambda: ["podman", "compose"],
                                   rows=se.load_rows(db), deferral_report=ledger,
                                   run=box.run, out=printed.append)
    assert refused == {"ollama"}
    assert [e.condition_id for e in ledger.entries] == [di.CID_RECREATE_REFUSED]
    assert any("[refuse-recreate] ollama" in p for p in printed)
    assert box.calls("rm") == [] and box.calls("up") == []


def test_row_and_rows_are_not_mutated_by_the_guard(world):
    root, infra, models, db = world
    row = _ollama_row()
    se.write_rows([row], db_path=db)
    box = Box(infra, live=_live_bind(models))
    g = _guard(box, infra, db, row)
    assert g.ok and row.data_mount is None and replace(row) == row
