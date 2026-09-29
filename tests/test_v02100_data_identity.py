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
    assert out.row is not None
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
    assert out.row is not None
    assert out.row.port == 11435


# ─── `--service ollama=vco` / use-vco-copy keep the recorded mount ──────


def test_service_flag_vco_keeps_the_recorded_mount():
    row = _ollama_row(_bind("/some/models"))
    out = sr.decide(sr.ServiceInputs(service="ollama", existing=row,
                                     choice=sr.Choice("ollama", "vco", None),
                                     port_free=lambda _p: True))
    assert out.row is not None
    assert out.row.data_mount == _bind("/some/models")
    assert not out.clear_mount


def test_vco_copy_never_inherits_an_adopted_containers_mount(tmp_path):
    adopted = se.EndpointRow(service="ollama", mode="adopted_container", port=11434,
                             source="user_cli", container_name="their_ollama",
                             data_mount=_bind("/their/models"))
    out = sr.decide(sr.ServiceInputs(service="ollama", existing=adopted,
                                     choice=sr.Choice("ollama", "vco", None),
                                     taken_ports=frozenset({11434}), port_free=lambda _p: True))
    assert out.row is not None
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
                                   run=box.run, out=printed.append, db_path=db)
    assert refused == {"ollama"}
    assert [e.condition_id for e in ledger.entries] == [di.CID_RECREATE_REFUSED]
    assert any("[refuse-recreate] ollama" in p for p in printed)
    assert box.calls("rm") == [] and box.calls("up") == []


def test_the_callers_row_and_rows_are_not_mutated_but_the_result_carries_the_record(world):
    """W2R-01 (b): the guard never edits the caller's objects — it RETURNS the
    row as it left it (``GuardResult.row``: the recorded mount), which is what
    a batch must thread into the next service's projection."""
    root, infra, models, db = world
    row = _ollama_row()
    se.write_rows([row], db_path=db)
    rows = dict(se.load_rows(db))
    before = dict(rows)
    box = Box(infra, live=_live_bind(models))
    g = di.guard_recreate("ollama", runtime="podman", infra_dir=infra,
                          compose_argv=["podman", "compose"], row=row, rows=rows,
                          db_path=db, run=box.run)
    assert g.ok and row.data_mount is None and replace(row) == row
    assert rows == before and rows["ollama"].data_mount is None
    assert g.row is not None and g.row.data_mount == _bind(models)
    assert replace(g.row, data_mount=None) == row


def test_a_refused_guard_returns_the_callers_row_unchanged(world):
    root, infra, models, db = world
    se.write_rows([_ollama_row(VOLUME)], db_path=db)
    row = se.load_rows(db)["ollama"]
    g = _guard(Box(infra, live=_live_bind(models)), infra, db, row)
    assert g.verdict == di.REFUSE_DIFFERENT and g.row == row


# ─── W2R-01: a batch never drops a sibling's recorded data knob ─────────

EMBED_DEST = "/cache"


class TwoBox:
    """A fake runtime + compose for ``vco_ollama`` AND ``vco_code_embed``.
    ``compose config`` renders BOTH services from the ``infrastructure/.env``
    on disk exactly like the base file's knobs (docker compose v2 long
    syntax); ``compose up`` snapshots that ``.env`` — what the one up really
    ran with."""

    KNOBS = {"ollama": ("VCT_OLLAMA_DATA_SOURCE", "ollama_data", "vco_ollama_data", OLLAMA_DEST),
             "code_embed": ("VCT_CODE_EMBED_CACHE_SOURCE", "code_embed_cache",
                            "vco_code_embed_cache", EMBED_DEST)}

    def __init__(self, infra: Path, live: dict, names: Optional[dict] = None):
        self.infra = infra
        self.live = live                       # service -> [inspect .Mounts entries]
        self.names = names or {"ollama": "vco_ollama", "code_embed": "vco_code_embed"}
        self.argv: list[list[str]] = []
        self.configs: list[dict] = []          # parsed renders, in call order
        self.env_at_up: list[dict] = []

    def env_file(self) -> dict:
        path = self.infra / ".env"
        return dict(parse_env_lines(path.read_text(encoding="utf-8"))) if path.is_file() else {}

    def _render(self) -> dict:
        env = self.env_file()
        services, volumes = {}, {}
        for service, (knob, key, default, dest) in self.KNOBS.items():
            source = env.get(knob, "")
            vol = ({"type": "bind", "source": source, "target": dest, "bind": {"create_host_path": True}}
                   if source else {"type": "volume", "source": key, "target": dest, "volume": {}})
            services[service] = {"volumes": [vol]}
            volumes[key] = {"name": default}
        return {"name": "infrastructure", "services": services, "volumes": volumes}

    def run(self, argv, **_kw):
        import yaml

        argv = [str(a) for a in argv]
        self.argv.append(argv)
        if argv[-1] == "config":
            doc = self._render()
            self.configs.append(doc)
            return _cp(argv, 0, yaml.safe_dump(doc))
        if argv[1:3] == ["compose", "version"]:
            return _cp(argv, 0, "Docker Compose version v2.30.0\n")
        if "up" in argv:
            self.env_at_up.append(self.env_file())
            return _cp(argv)
        if argv[1] == "inspect":
            name, fmt = argv[-1], argv[argv.index("--format") + 1]
            service = next((s for s, n in self.names.items() if n == name), None)
            if service is None or service not in self.live:
                return _cp(argv, 125, "", f"Error: no such container {name}")
            if fmt == "{{.Id}}":
                return _cp(argv, 0, "abc123\n")
            if fmt == "{{json .Mounts}}":
                return _cp(argv, 0, json.dumps(self.live[service]))
        if argv[1] == "rm":
            return _cp(argv)
        raise AssertionError(f"unexpected argv {argv}")

    def calls(self, word: str) -> list[list[str]]:
        if word == "up":
            return [a for a in self.argv if "up" in a and a[-1] != "config"]
        return [a for a in self.argv if len(a) > 1 and a[1] == word]


def _embed_row(mount: Optional[dict] = None) -> se.EndpointRow:
    return se.EndpointRow(service="code_embed", mode="vco_managed", port=11440, data_mount=mount,
                          container_name="vco_code_embed", source="install_probe")


@pytest.fixture()
def two(world, tmp_path):
    root, infra, models, db = world
    cache = tmp_path / "hf-cache"
    (cache / "models--x").mkdir(parents=True)
    live = {"ollama": _live_bind(models),
            "code_embed": [{"Type": "bind", "Source": str(cache), "Destination": EMBED_DEST}]}
    return infra, models, cache, db, TwoBox(infra, live)


def _both_knobs(env: dict, models, cache) -> bool:
    return (env.get("VCT_OLLAMA_DATA_SOURCE") == str(models)
            and env.get("VCT_CODE_EMBED_CACHE_SOURCE") == str(cache))


def _render_binds(doc: dict) -> dict:
    return {s: (c["volumes"][0]["type"], c["volumes"][0]["source"]) for s, c in doc["services"].items()}


def test_a_two_service_up_batch_on_null_rows_keeps_both_knobs_for_the_one_compose_up(two):
    """The owner's damaged-machine shape: BOTH rows vco_managed with a NULL
    mount, both containers on binds. The hooks' `up` verb guards ollama,
    then code_embed, then runs ONE compose up — which must see both knobs."""
    infra, models, cache, db, box = two
    se.write_rows([_ollama_row(), _embed_row()], db_path=db)
    out: list[str] = []
    res = sl.up_services(["ollama", "code_embed"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=out.append)
    assert res.cleared == ["ollama", "code_embed"] and res.returncode == 0, out
    assert len(box.env_at_up) == 1 and _both_knobs(box.env_at_up[0], models, cache), box.env_at_up
    # the render read for the LAST service shows BOTH binds
    assert _render_binds(box.configs[-1]) == {"ollama": ("bind", str(models)),
                                              "code_embed": ("bind", str(cache))}
    rows = se.load_rows(db)
    assert rows["ollama"].data_mount == _bind(models)
    assert rows["code_embed"].data_mount == {"kind": "bind", "source": str(cache),
                                             "destination": EMBED_DEST}


def test_a_two_service_step5_batch_on_null_rows_keeps_both_knobs(two):
    """The same through install.py step 5's `guard_compose_set`."""
    infra, models, cache, db, box = two
    se.write_rows([_ollama_row(), _embed_row()], db_path=db)
    rows = se.load_rows(db)
    refused = di.guard_compose_set(["ollama", "code_embed"], runtime="podman", infra_dir=infra,
                                   compose_argv=["podman", "compose"], rows=rows, run=box.run,
                                   out=lambda _l: None, db_path=db)
    assert refused == set()
    assert _both_knobs(box.env_file(), models, cache), box.env_file()
    assert _render_binds(box.configs[-1])["ollama"] == ("bind", str(models))
    assert rows["ollama"].data_mount is None  # the caller's mapping is not mutated


def test_a_two_service_batch_on_recorded_rows_leaves_the_file_as_it_is(two):
    """Leave-alone: the rows already carry both mounts — nothing is recorded,
    the projected .env is identical before and after the second guard."""
    infra, models, cache, db, box = two
    se.write_rows([_ollama_row(_bind(models)), _embed_row(
        {"kind": "bind", "source": str(cache), "destination": EMBED_DEST})], db_path=db)
    from vco_lib import compose_env

    compose_env.write_service_keys(infra, se.load_rows(db), runtime="podman")
    before = (infra / ".env").read_bytes()
    out: list[str] = []
    res = sl.up_services(["ollama", "code_embed"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=out.append)
    assert res.returncode == 0 and not any("recorded" in line for line in out), out
    assert (infra / ".env").read_bytes() == before
    assert _both_knobs(box.env_at_up[0], models, cache)


def test_the_second_guard_of_a_batch_projects_the_first_guards_record(two):
    """W2R-01 (b), isolated from the writer: the rows map handed to the
    SECOND guard already carries the mount the first one recorded."""
    infra, models, cache, db, box = two
    se.write_rows([_ollama_row(), _embed_row()], db_path=db)
    seen: list = []
    real = di.guard_recreate

    def spy(service, **kw):
        seen.append((service, dict(kw["rows"])))
        return real(service, **kw)

    with mock.patch.object(di, "guard_recreate", spy):
        sl.up_services(["ollama", "code_embed"], compose_dir=infra,
                       compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                       run=box.run, out=lambda _l: None)
        di.guard_compose_set(["ollama", "code_embed"], runtime="podman", infra_dir=infra,
                             compose_argv=["podman", "compose"],
                             rows={"ollama": _ollama_row(), "code_embed": _embed_row()},
                             run=box.run, out=lambda _l: None, db_path=db)
    by_call = [(svc, rows["ollama"].data_mount) for svc, rows in seen]
    assert by_call == [("ollama", None), ("code_embed", _bind(models))] * 2, by_call


def test_a_sibling_outside_the_batch_keeps_its_knob(two):
    """W2R-01 (a), the writer: only code_embed is composed while ollama's
    row is still NULL — ollama's knob, already in the block, survives the
    projection (no batch threading can help here: ollama is not in it)."""
    infra, models, cache, db, box = two
    from vco_lib import compose_env

    compose_env.write_service_keys(infra, {"ollama": _ollama_row(_bind(models))}, runtime="podman")
    se.write_rows([_ollama_row(), _embed_row()], db_path=db)
    res = sl.up_services(["code_embed"], compose_dir=infra, compose_argv=["podman", "compose"],
                         runtime="podman", db_path=db, run=box.run, out=lambda _l: None)
    assert res.returncode == 0
    assert _both_knobs(box.env_at_up[0], models, cache), box.env_at_up


# ─── W2R-01 (a): the writer never drops a data knob by omission ─────────


def _keys(infra: Path) -> dict:
    return dict(parse_env_lines((infra / ".env").read_text(encoding="utf-8")))


@pytest.mark.parametrize("sibling", [
    pytest.param(lambda: _ollama_row(), id="vco_managed-without-a-mount"),
    pytest.param(lambda: None, id="no-row"),
    pytest.param(lambda: se.EndpointRow(service="ollama", mode="adopted_external", port=11434,
                                        source="user_cli"), id="not-vco_managed"),
])
def test_an_in_block_data_knob_survives_a_row_that_states_none(tmp_path, sibling):
    from vco_lib import compose_env

    compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row(_bind("/srv/my models"))},
                                   runtime="podman")
    rows = {"code_embed": _embed_row()}
    row = sibling()
    if row is not None:
        rows["ollama"] = row
    result = compose_env.write_service_keys(tmp_path, rows, runtime="podman")
    got = _keys(tmp_path)
    assert got["VCT_OLLAMA_DATA_SOURCE"] == "/srv/my models"
    assert result.carried["VCT_OLLAMA_DATA_SOURCE"] == "/srv/my models"
    assert (tmp_path / ".env").read_text().count("VCT_OLLAMA_DATA_SOURCE=") == 1


def test_a_stated_mount_replaces_the_knob_pair(tmp_path):
    """Act: the row states the OTHER half (a volume) → the bind knob leaves."""
    from vco_lib import compose_env

    compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row(_bind("/srv/m"))},
                                   runtime="podman")
    result = compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row(VOLUME)},
                                            runtime="podman")
    got = _keys(tmp_path)
    assert got["VCT_OLLAMA_VOLUME_NAME"] == DEFAULT_VOLUME
    assert "VCT_OLLAMA_DATA_SOURCE" not in got and not result.carried


def test_a_row_less_service_keeps_its_port_keys_and_a_present_rows_unstated_port_leaves(tmp_path):
    from vco_lib import compose_env

    weav = se.EndpointRow(service="weaviate", mode="vco_managed", port=18081, grpc_port=15052,
                          source="user_cli")
    compose_env.write_service_keys(tmp_path, {"weaviate": weav, "ollama": _ollama_row()},
                                   runtime="podman")
    compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row()}, runtime="podman")
    got = _keys(tmp_path)
    assert (got["WEAVIATE_PORT"], got["WEAVIATE_GRPC_PORT"]) == ("18081", "15052")
    adopted = se.EndpointRow(service="weaviate", mode="adopted_external", port=8080,
                             source="user_cli")
    compose_env.write_service_keys(tmp_path, {"weaviate": adopted}, runtime="podman")
    assert "WEAVIATE_PORT" not in _keys(tmp_path)


def test_a_carried_knob_never_duplicates_an_outside_line(tmp_path):
    from vco_lib import compose_env

    compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row(_bind("/srv/block"))},
                                   runtime="podman")
    env = tmp_path / ".env"
    env.write_text(env.read_text() + "VCT_OLLAMA_DATA_SOURCE=/srv/outside\n", encoding="utf-8")
    result = compose_env.write_service_keys(tmp_path, {"ollama": _ollama_row()}, runtime="podman")
    text = env.read_text()
    assert text.count("VCT_OLLAMA_DATA_SOURCE=") == 1 and "/srv/outside" in text
    assert "VCT_OLLAMA_DATA_SOURCE" not in result.carried


# ─── migrate_managed_service: a restore never writes the NULL row back ──


def test_a_guard_refusal_restores_the_observed_mount_not_the_null_row(tmp_path):
    """The guard projected the live mount into .env, then refused (`compose
    config` failed). The restore that follows must project the OBSERVED
    mount — never the NULL row it started from (W2R-01 (b))."""
    from tests.test_v0297_service_lifecycle import EmbedWorld
    from types import SimpleNamespace
    from vco_lib import compose_env, containers

    cache = tmp_path / "hf-cache"
    (cache / "models--x").mkdir(parents=True)
    w = EmbedWorld(tmp_path, cache={"kind": "bind", "source": str(cache)})
    w.config_rc = 1
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_embed_row()], db_path=db)       # the NULL row
    projected: list = []
    real_write = compose_env.write_service_keys

    def spy(infra, rows, **kw):
        projected.append(rows.get("code_embed"))
        return real_write(infra, rows, **kw)

    with mock.patch.object(compose_env, "write_service_keys", spy), \
            mock.patch.object(containers, "_resolve_runtime", return_value="podman"):
        result = sl.migrate_managed_service(
            w.root, _embed_row(), runtime="podman", run=w.run, fetch=w.fetch, db_path=db,
            commit=lambda rows: se.write_rows(list(rows), db_path=db),
            resolution=SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand"),
            log=lambda _m: None, health_timeout_s=0.0, health_interval_s=0.0)
    assert result.status == "refused" and "compose config" in result.reason, result.reason
    assert len(projected) == 2, projected           # the guard's projection, then the restore
    live = {"kind": "bind", "source": str(cache), "destination": EMBED_DEST}
    assert projected[-1] is not None and projected[-1].data_mount == live, projected
    env = dict(parse_env_lines((w.infra / ".env").read_text(encoding="utf-8")))
    assert env["VCT_CODE_EMBED_CACHE_SOURCE"] == str(cache)


# ─── W2R-04: the `compose config` render corpus ─────────────────────────

CORPUS = json.loads((REPO_ROOT / "tests" / "fixtures" / "compose_config_render_corpus.json")
                    .read_text(encoding="utf-8"))


def _fill(value, infra: Path):
    if isinstance(value, dict):
        return {k: _fill(v, infra) for k, v in value.items()}
    if isinstance(value, str):
        return value.replace("{infra}", str(infra))
    return value


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["name"])
def test_compose_config_render_corpus(tmp_path, case):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    render = case["render"]

    def run(argv, **_kw):
        assert argv[-1] == "config", argv
        return _cp(argv, 0, render)

    eff = di.effective_compose_mount(infra, case["service"], ["docker", "compose"],
                                     files=[infra / "docker-compose.yml"],
                                     project=case["project"] or "", run=run)
    if "expect_error" in case:
        assert eff.error, eff
        for needle in case["expect_error"]:
            assert needle in eff.error, (needle, eff.error)
        # fail CLOSED: an unreadable render never proves a recreate
        live = di.LiveMount(di.LIVE_PRESENT, "c", _bind("/srv/anything"))
        assert di.recreate_preserves_data(case["service"], None, live, eff)[0] == di.REFUSE_UNKNOWN
        return
    assert eff.error == "", eff.error
    assert eff.mount == _fill(case["expect_mount"], infra)
    spec = di._sa.mount_from_inspect(_fill(case["live"], infra))
    live = di.LiveMount(di.LIVE_PRESENT, "c", di._as_row_mount(spec))
    assert di.recreate_preserves_data(case["service"], None, live, eff)[0] == case["expect_verdict"]


def test_the_corpus_covers_every_shape_the_review_named():
    names = " | ".join(c["name"] for c in CORPUS["cases"])
    for shape in ("docker compose v2", "podman-compose", "Windows", "WSL2", "name:", "FAIL CLOSED"):
        assert shape in names, shape


def test_a_stored_mount_keeps_the_runtimes_own_spelling():
    """The comparison folds Windows spellings; the ROW never does."""
    live = {"Type": "bind", "Source": "/run/desktop/mnt/host/c/Users/u/m", "Destination": OLLAMA_DEST}
    row = di._as_row_mount(di._sa.mount_from_inspect(live))
    assert row is not None and row["source"] == "/run/desktop/mnt/host/c/Users/u/m"
    assert di.mount_key(row) == di.mount_key(_bind("C:\\Users\\u\\m"))


# ─── W2R-05: a container under a historical alias is PRESENT ────────────


def test_an_alias_named_container_is_present_never_nothing_to_lose(two):
    infra, models, cache, db, box = two
    from vco_lib import containers

    alias = containers.all_known_names("ollama")[1]
    box.names = {"ollama": alias, "code_embed": "vco_code_embed"}
    live = di.capture_live_mount("ollama", "podman", run=box.run,
                                 names=di.container_names("ollama", _ollama_row(container_name=None)))
    assert live.state == di.LIVE_PRESENT and live.ref == alias
    se.write_rows([_ollama_row(container_name=None)], db_path=db)
    # not being removed: composing vco_ollama beside it is refused, nothing touched
    res = sl.up_services(["ollama"], compose_dir=infra, compose_argv=["podman", "compose"],
                         runtime="podman", db_path=db, run=box.run, out=lambda _l: None)
    assert res.refused == ["ollama"] and box.calls("rm") == [] and box.calls("up") == []
    assert "two containers on one data location" in res.entries[0].detected
    refused = di.guard_compose_set(["ollama"], runtime="podman", infra_dir=infra,
                                   compose_argv=["podman", "compose"], rows=se.load_rows(db),
                                   run=box.run, out=lambda _l: None, db_path=db)
    assert refused == {"ollama"}


def test_an_alias_named_zombie_is_removed_then_recreated_on_its_data(two):
    infra, models, cache, db, box = two
    from vco_lib import containers

    alias = containers.all_known_names("ollama")[1]
    box.names = {"ollama": alias, "code_embed": "vco_code_embed"}
    se.write_rows([_ollama_row(container_name=None)], db_path=db)
    res = sl.up_services(["ollama"], recreate=["ollama"], compose_dir=infra,
                         compose_argv=["podman", "compose"], runtime="podman", db_path=db,
                         run=box.run, out=lambda _l: None)
    assert res.returncode == 0 and res.removed == [alias]
    assert box.env_at_up[0]["VCT_OLLAMA_DATA_SOURCE"] == str(models)


# ─── W2R-09: an unreadable registry never recreates an existing container ─


def test_an_unreadable_registry_refuses_a_live_container(world):
    """The data WOULD come along (the knob is on disk, the render binds the
    models) — but whether VCO owns the container is unknown: do nothing."""
    root, infra, models, db = world
    (infra / ".env").write_text(f"VCT_OLLAMA_DATA_SOURCE={models}\n", encoding="utf-8")
    box = Box(infra, live=_live_bind(models))
    out: list[str] = []
    with mock.patch.object(se, "load_rows", side_effect=se.ServiceRegistryUnavailable("locked")):
        res = sl.up_services(["ollama"], recreate=["ollama"], compose_dir=infra,
                             compose_argv=["podman", "compose"], runtime="podman", run=box.run,
                             out=out.append)
    assert res.refused == ["ollama"] and res.returncode == 3
    assert box.calls("rm") == [] and box.calls("up") == []
    assert any("could not be read" in line and "locked" in line for line in out), out


def test_an_unreadable_registry_still_creates_a_missing_container(world):
    root, infra, models, db = world
    box = Box(infra, exists=False)
    with mock.patch.object(se, "load_rows", side_effect=se.ServiceRegistryUnavailable("locked")):
        res = sl.up_services(["ollama"], compose_dir=infra, compose_argv=["podman", "compose"],
                             runtime="podman", run=box.run, out=lambda _l: None)
    assert res.cleared == ["ollama"] and res.returncode == 0 and len(box.calls("up")) == 1


# ─── W2R-10: the printed inspect recipe names the runtime asked ─────────


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_the_refusal_recipe_names_the_guards_runtime(world, runtime):
    root, infra, models, db = world
    se.write_rows([_ollama_row(VOLUME)], db_path=db)
    box = Box(infra, live=_live_bind(models))
    g = di.guard_recreate("ollama", runtime=runtime, infra_dir=infra,
                          compose_argv=[runtime, "compose"], row=se.load_rows(db)["ollama"],
                          db_path=db, run=box.run)
    cmd = g.deferral_entry().command_to_apply
    other = "docker" if runtime == "podman" else "podman"
    assert f"{runtime} inspect --format" in cmd and f"{other} inspect" not in cmd


def test_a_result_that_never_learned_its_runtime_prints_no_guess():
    g = di.GuardResult("ollama", di.REFUSE_UNKNOWN, "x")
    assert "<podman|docker> inspect" in g.deferral_entry().command_to_apply
    assert "docker inspect" in g.deferral_entry(runtime="docker").command_to_apply
