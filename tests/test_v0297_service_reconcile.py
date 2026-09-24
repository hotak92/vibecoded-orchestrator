# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 SE-2 — one detector, the evidence ranking, the legacy import.

Every case runs ``vco_lib.service_reconcile.reconcile`` end to end against a
FAKE machine: a scripted container runtime (``run``), scripted HTTP answers
(``fetch``), scripted TCP ports (``tcp_open``) and a scripted free-port check
(``port_free``). The launcher.db is a real-schema file under ``tmp_path``
(every migration applied — ``tests/common/launcher_db_fixture.py``). Nothing
here reaches a real runtime, a real port or the real ``~/.vct``.

The table lives in ``tests/fixtures/service_endpoint_migration_cases.json``
(plan §6 SE-2, cases a–k + the Q1 rulings); the procedural cases (h, i, j, l,
live drift, the install.py shim) are below it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import add_project, make_launcher_db  # noqa: E402
from vco_lib import service_detection as det  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_reconcile as sr  # noqa: E402

CASES = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "service_endpoint_migration_cases.json").read_text(encoding="utf-8")
)["cases"]


# ─── the fake machine ───────────────────────────────────────────────────


def _inspect_obj(spec: dict) -> dict:
    labels = {}
    if spec.get("project"):
        labels["com.docker.compose.project"] = spec["project"]
    if spec.get("working_dir"):
        labels["com.docker.compose.project.working_dir"] = spec["working_dir"]
    if spec.get("service_label"):
        labels["com.docker.compose.service"] = spec["service_label"]
    return {
        "Name": spec["name"],
        "Config": {"Image": spec["image"], "Labels": labels,
                   "Env": [f"{k}={v}" for k, v in (spec.get("env") or {}).items()]},
        "State": {"Status": spec.get("state", "running"),
                  "Running": spec.get("state", "running") == "running"},
        "HostConfig": {"PortBindings": {
            f"{cp}/tcp": [{"HostIp": "", "HostPort": str(hp)}]
            for cp, hp in (spec.get("ports") or {}).items()}},
        "Mounts": spec.get("mounts") or [],
    }


class FakeMachine:
    def __init__(self, containers=(), http=None, tcp_open=(), busy_ports=()):
        self.containers = {c["name"]: dict(c) for c in containers}
        self.http = dict(http or {})
        self.tcp = set(tcp_open)
        self.busy = set(busy_ports)
        self.started: list[str] = []
        self.argv_log: list[list[str]] = []

    # subprocess seam ---------------------------------------------------
    def run(self, argv, **_kw):
        self.argv_log.append(list(argv))
        verb = argv[1]
        if verb == "ps":
            out = "\n".join(self.containers) + ("\n" if self.containers else "")
            return mock.Mock(returncode=0, stdout=out, stderr="")
        if verb == "inspect":
            names = [a for a in argv[2:] if not a.startswith("--") and a != "container"]
            objs = [_inspect_obj(self.containers[n]) for n in names if n in self.containers]
            return mock.Mock(returncode=0, stdout=json.dumps(objs), stderr="")
        if verb == "start":
            self.started.append(argv[2])
            self.containers[argv[2]]["state"] = "running"
            return mock.Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected runtime call {argv}")

    # HTTP seam ---------------------------------------------------------
    def fetch(self, url: str, _timeout: float) -> Optional[det.HttpResponse]:
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        spec = self.http.get(hostport)
        if spec is None:
            return None
        path = "/" + path
        kind = spec["service"]
        if kind == "weaviate":
            if path == "/v1/.well-known/ready":
                return det.HttpResponse(200, "")
            if path == "/v1/meta":
                if spec.get("auth"):
                    return det.HttpResponse(401, "")
                return det.HttpResponse(200, json.dumps({"version": spec["version"]}))
            if path == "/v1/schema":
                if spec.get("auth"):
                    return det.HttpResponse(401, "")
                return det.HttpResponse(200, json.dumps(
                    {"classes": [{"class": c} for c in spec.get("classes", [])]}))
        if kind == "ollama" and path == "/api/tags":
            return det.HttpResponse(200, json.dumps({"models": [{"name": m} for m in spec["models"]]}))
        if kind == "code_embed" and path == "/health":
            return det.HttpResponse(200, spec["body"])
        return det.HttpResponse(404, "")

    def tcp_open(self, host: str, port: int, _t: float) -> bool:
        return f"{host}:{port}" in self.tcp

    def port_free(self, port: int) -> bool:
        return port not in self.busy


class Recorder:
    def __init__(self):
        self.infra: list[dict] = []
        self.reprojected = 0
        self.registered = 0

    def kwargs(self) -> dict:
        return {
            "write_infra_env": lambda _d, rows: self.infra.append(dict(rows)),
            "reproject": lambda _db: setattr(self, "reprojected", self.reprojected + 1),
            "register_mcps": lambda _root: bool(setattr(self, "registered", self.registered + 1)) or True,
        }


def _make_root(tmp_path: Path, case: dict) -> Path:
    root = tmp_path / "orch"
    infra = root / "infrastructure"
    infra.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "infrastructure" / "docker-compose.yml", infra / "docker-compose.yml")
    if case.get("continuity"):
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps({"env": case["continuity"]}))
    for name, text in (case.get("override_files") or {}).items():
        (infra / name).write_text(text, encoding="utf-8")
    return root


def _services_toml(tmp_path: Path, case: dict) -> Path:
    path = tmp_path / "state" / "services.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    migrated = path.with_name(path.name + sr.MIGRATED_SUFFIX)
    if case.get("services_toml") and not migrated.exists():
        from vco_lib.service_adoption import write_services_toml

        write_services_toml({"services": case["services_toml"]}, path=path)
    return path


def _vct_config_dirs(tmp_path: Path, case: dict) -> list[Path]:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    if case.get("vct_config"):
        (d / "vct-config.toml").write_text(case["vct_config"], encoding="utf-8")
    return [d]


def run_case(tmp_path: Path, case: dict, *, db: Optional[Path] = None, machine: Optional[FakeMachine] = None,
             recorder: Optional[Recorder] = None, **overrides: Any):
    root = tmp_path / "orch" if (tmp_path / "orch").is_dir() else _make_root(tmp_path, case)
    db = db or make_launcher_db(tmp_path / "launcher.db", app_state=case.get("app_state"))
    machine = machine or FakeMachine(case.get("containers", ()), case.get("http"),
                                     case.get("tcp_open", ()), case.get("busy_ports", ()))
    recorder = recorder or Recorder()
    kw: dict[str, Any] = dict(
        phase=case.get("phase", "update"), orchestrator_root=root, runtime="podman", db_path=db,
        env=case.get("env", {}), has_gpu=case.get("has_gpu", False),
        interactive=case.get("interactive", False), on_conflict=case.get("on_conflict"),
        run=machine.run, fetch=machine.fetch, tcp_open=machine.tcp_open,
        port_free=machine.port_free, now_ms=1_700_000_000_000,
        services_toml_path=_services_toml(tmp_path, case),
        vct_config_dirs=_vct_config_dirs(tmp_path, case), apply_kwargs=recorder.kwargs(),
    )
    kw.update(overrides)
    return sr.reconcile(**kw), db, machine, recorder, root


def _row_matches(row: se.EndpointRow, want: dict) -> list[str]:
    got = row.to_json()
    return [f"{k}: want {v!r} got {got.get(k)!r}" for k, v in want.items() if got.get(k) != v]


# ─── the table ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_migration_case(tmp_path, case):
    result, db, machine, _rec, root = run_case(tmp_path, case)
    expect = case["expect"]
    assert result.abort is None, result.abort
    stored = se.load_rows(db)
    for service, want in expect.get("rows", {}).items():
        assert service in stored, f"{service} row not written"
        problems = _row_matches(stored[service], want)
        assert not problems, f"{case['id']} {service}: {problems}"
    cids = [e.condition_id for e in result.entries]
    for cid in expect.get("entries", []):
        assert cid in cids, f"{case['id']}: expected {cid} in {cids}"
    for cid in expect.get("entries_absent", []):
        assert cid not in cids, f"{case['id']}: {cid} must not be emitted ({cids})"
    for service, disp in expect.get("disposition", {}).items():
        assert result.disposition(service) == disp, f"{case['id']} {service}"
    managed = se.plan(stored)["managed_services"]
    for service in expect.get("managed_services_absent", []):
        assert service not in managed, f"{case['id']}: {service} would be started by compose"
    if "migrate_code_embed" in expect:
        assert result.migrate_code_embed is expect["migrate_code_embed"]
    if "weaviate_pending" in expect:
        assert result.weaviate_pending is expect["weaviate_pending"]
    if expect.get("services_toml_renamed"):
        toml = tmp_path / "state" / "services.toml"
        assert not toml.exists()
        assert toml.with_name("services.toml" + sr.MIGRATED_SUFFIX).is_file()
    for name in expect.get("files_renamed", []):
        assert not (root / "infrastructure" / name).exists()
        assert (root / "infrastructure" / (name + sr.RETIRED_SUFFIX)).is_file()
    for name in expect.get("files_kept", []):
        assert (root / "infrastructure" / name).is_file()
    for key in expect.get("app_state_kept", []):
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT 1 FROM app_state WHERE key=?", (key,)).fetchone()
    assert machine.started == expect.get("started", [])
    # Never a state-changing runtime call other than a start-by-name.
    assert all(a[1] in ("ps", "inspect", "start") for a in machine.argv_log)
    # Every emitted entry is registered and carries its declared dismiss fields.
    from vco_lib import deferral_registry as dr

    for entry in result.entries:
        spec = dr.condition(entry.condition_id)
        assert spec is not None, entry.condition_id
        for field_name in spec.dismiss_key or ():
            assert entry.dismiss_fields.get(field_name), (entry.condition_id, field_name)


# ─── every printed remedy parses (a printed command is shipped code) ────


def _printed_endpoint_commands(entries) -> list[list[str]]:
    import shlex

    out = []
    for entry in entries:
        for line in entry.command_to_apply.splitlines():
            line = line.lstrip("# ").strip()
            if not line.startswith("python -m vco_lib.service_endpoints"):
                continue
            line = line.split("   #", 1)[0].split("  #", 1)[0]
            line = line.replace("<url>", "http://h:1")
            out.append(shlex.split(line)[3:])
    return out


def test_every_printed_service_endpoints_command_parses(tmp_path):
    parser = se._build_arg_parser()
    seen: set[str] = set()
    for case in CASES:
        sub = tmp_path / case["id"]
        sub.mkdir()
        result, *_ = run_case(sub, case)
        verify_entries = sr.verify(result.rows, write=False, fetch=lambda _u, _t: None, wait_s=0)
        for argv in _printed_endpoint_commands([*result.entries, *verify_entries]):
            seen.add(argv[0])
            with mock.patch.object(sys, "exit", side_effect=AssertionError(f"rejected: {argv}")):
                parser.parse_args(argv)
    # the sweep covered the verbs the remedies actually print
    assert {"adopt", "use-vco-copy", "hand-to-vco", "candidates", "reconcile", "show"} <= seen, seen


# ─── procedural cases ───────────────────────────────────────────────────


def test_h_registry_unavailable_pins_the_run(tmp_path):
    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")
    empty_db = tmp_path / "launcher.db"  # never created: the hub could not make it
    result, *_ = run_case(tmp_path, case, db=empty_db,
                          ensure_db=lambda: (False, "no vct-hub binary found"))
    assert result.pinned and not result.registry_ok
    assert "service_registry_unavailable" in [e.condition_id for e in result.entries]
    assert result.rows["ollama"].port == 11434  # the decision still exists, in memory
    assert not empty_db.exists()


def test_h_failed_ensure_db_is_recorded_even_when_an_older_table_is_writable(tmp_path):
    """A failing `vct-hub --ensure-db` is always reported; a table a current
    hub created earlier still takes the rows (no pins needed)."""
    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")
    result, db, *_ = run_case(tmp_path, case, ensure_db=lambda: (False, "exited 2"))
    assert "service_registry_unavailable" in [e.condition_id for e in result.entries]
    assert not result.pinned
    assert se.load_rows(db)["ollama"].port == 11434


def test_h_install_pins_reach_settings_defaults_and_registration(tmp_path, monkeypatch):
    """The shim keeps the in-memory rows as pins: the settings defaults and the
    MCP-registration hand-off carry the DETECTED endpoints, not the defaults."""
    import install

    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")
    root = _make_root(tmp_path, case)
    machine = FakeMachine(case.get("containers", ()), case.get("http"))
    real = sr.reconcile

    def fake_reconcile(**kw):
        kw.update(run=machine.run, fetch=machine.fetch, tcp_open=machine.tcp_open,
                  port_free=machine.port_free, env={}, db_path=tmp_path / "absent.db",
                  services_toml_path=tmp_path / "none.toml", vct_config_dirs=[],
                  ensure_db=lambda: (False, "hub binary predates migration 047"))
        return real(**kw)

    monkeypatch.setattr(sr, "reconcile", fake_reconcile)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": {}, "pinned": False, "weaviate_pending": False})
    for key in se.transport_env({}):
        monkeypatch.delenv(key, raising=False)
    args = argparse.Namespace(update=False, yes=True, quiet=True, on_conflict=None, service=[])
    sysinfo = mock.Mock(container_cmd="podman", has_gpu=False)
    report = mock.Mock()
    decisions = install._reconcile_service_endpoints(args, sysinfo, report)
    assert decisions is not None
    assert decisions["ollama"]["probe"] == install.PROBE_FOREIGN  # adopted → never touched
    assert decisions["weaviate"]["action"] == install.ACTION_START
    added = [c.args[0].condition_id for c in report.add_entry.call_args_list]
    assert "service_registry_unavailable" in added
    defaults = install._build_vco_settings_defaults(
        {"text_model": "qwen3-embedding:0.6b", "active_embedding": "qwen3", "code_backend": "ollama"})
    assert defaults["env"]["OLLAMA_URL"] == "http://localhost:11434"
    assert install._service_endpoint_urls()["ollama_port"] == 11434
    import os
    assert os.environ["OLLAMA_PORT"] == "11434"  # the run's own clients follow the pins


def test_i_compatibility_gate():
    ok, _ = det.compatibility("weaviate", det.Probe("weaviate", "u", True, True, (), "1.24.0"), 50051)
    assert ok
    ok, why = det.compatibility("weaviate", det.Probe("weaviate", "u", True, True, (), "1.23.9"), 50051)
    assert not ok and "below 1.24.0" in why
    ok, why = det.compatibility("weaviate", det.Probe("weaviate", "u", True, True, (), None, True), 50051)
    assert not ok and "authentication" in why
    ok, why = det.compatibility("weaviate", det.Probe("weaviate", "u", True, True, (), "1.28.4"), None)
    assert not ok and "gRPC" in why
    assert det.MIN_WEAVIATE_VERSION == (1, 24, 0)


def test_i_incompatible_candidates_are_never_adopted(tmp_path):
    base = {"id": "i", "phase": "install", "has_gpu": False, "expect": {}}
    for label, spec, containers in (
        ("old", {"service": "weaviate", "version": "1.20.0", "classes": []},
         [{"name": "w", "image": "semitechnologies/weaviate:1.20.0", "ports": {"8080": 8081, "50051": 50051}}]),
        ("auth", {"service": "weaviate", "version": "1.28.0", "classes": [], "auth": True},
         [{"name": "w", "image": "semitechnologies/weaviate:1.28.0", "ports": {"8080": 8081, "50051": 50051}}]),
        ("no-grpc", {"service": "weaviate", "version": "1.28.0", "classes": []},
         [{"name": "w", "image": "semitechnologies/weaviate:1.28.0", "ports": {"8080": 8081}}]),
    ):
        sub = tmp_path / label
        sub.mkdir()
        case = dict(base, http={"localhost:8081": spec}, containers=containers, busy_ports=[8081, 50051])
        result, db, *_ = run_case(sub, case, on_conflict="adopt")
        assert result.detection is not None
        cand = next(c for c in result.detection.for_service("weaviate") if c.port == 8081)
        assert not cand.compatible and cand.reason, label
        row = se.load_rows(db)["weaviate"]
        # Not adopted: VCO's own copy on a free port (the incompatible one holds 8081).
        assert row.mode == "vco_managed" and row.port == 8082, (label, row)


def test_j_legacy_default_binding_urls_become_null(tmp_path):
    case = next(c for c in CASES if c["id"] == "e_dead_port_override")
    db = make_launcher_db(tmp_path / "launcher.db", app_state=case["app_state"])
    add_project(db, project_id="p1", name="Alpha", folder_path=tmp_path / "alpha", kg_primary="Alpha_KnowledgeGraph")
    add_project(db, project_id="p2", name="Bar", folder_path=tmp_path / "bar", kg_primary="Bar_KnowledgeGraph")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE project_kg_bindings SET weaviate_url='http://localhost:8081/' WHERE project_id='p1'")
        conn.execute("UPDATE project_kg_bindings SET weaviate_url='http://kg.example:9000' WHERE project_id='p2'")
    run_case(tmp_path, case, db=db)
    with sqlite3.connect(db) as conn:
        urls = dict(conn.execute("SELECT project_id, weaviate_url FROM project_kg_bindings WHERE role='primary'"))
    assert urls["p1"] is None
    assert urls["p2"] == "http://kg.example:9000"


def test_l_second_run_writes_nothing(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    first, db, machine, rec, _root = run_case(tmp_path, case)
    assert first.written == ["weaviate", "ollama", "code_embed"]
    assert rec.reprojected == 1 and rec.registered == 1
    rows_before = {s: r.to_json() for s, r in se.load_rows(db).items()}
    second, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec)
    assert second.written == []
    assert rec.reprojected == 1 and rec.registered == 1  # no follow-up chain
    assert {s: r.to_json() for s, r in se.load_rows(db).items()} == rows_before
    cids = [e.condition_id for e in second.entries]
    assert "service_endpoints_migrated" not in cids  # one-shot record


def test_adopted_container_port_move_is_followed(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, _root = run_case(tmp_path, case)
    machine.containers["vco_ollama"]["ports"] = {"11434": 11500}
    machine.http["localhost:11500"] = machine.http.pop("localhost:11435")
    second, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec, phase="session")
    row = se.load_rows(db)["ollama"]
    assert (row.mode, row.container_name, row.port, row.source) == (
        "adopted_container", "vco_ollama", 11500, "live_reconcile")
    assert second.written == ["ollama"]


def test_vanished_adopted_container_is_reported_not_replaced(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, _root = run_case(tmp_path, case)
    del machine.containers["vco_weaviate"]
    machine.http.pop("localhost:8081")
    # Session start (`service_endpoints reconcile --phase session`, the Python
    # path the session hook calls): the missing container is a ledger entry.
    second, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec, phase="session")
    assert se.load_rows(db)["weaviate"].container_name == "vco_weaviate"  # I6: no switch
    assert "service_endpoint_unreachable" in [e.condition_id for e in second.entries]


def test_verify_stamps_and_reports(tmp_path):
    db = make_launcher_db(tmp_path / "launcher.db")
    rows = [se.EndpointRow("weaviate", "vco_managed", 8081, "install_probe", grpc_port=50052),
            se.EndpointRow("ollama", "vco_managed", 11435, "install_probe"),
            se.EndpointRow("code_embed", "vco_managed", 11440, "install_probe", enabled=False)]
    se.write_rows(rows, db_path=db)
    machine = FakeMachine(http={"localhost:8081": {"service": "weaviate", "version": "1.28.4", "classes": []}})
    entries = sr.verify(None, db_path=db, fetch=machine.fetch, wait_s=0, now_ms=5)
    stored = se.load_rows(db)
    assert stored["weaviate"].verified_at == 5
    assert stored["ollama"].verified_at is None
    assert [e.condition_id for e in entries] == ["service_endpoint_unreachable"]
    assert entries[0].dismiss_fields["service"] == "ollama"  # code_embed disabled: not probed


def test_interactive_prompt_default_is_use_it(tmp_path):
    case = next(c for c in CASES if c["id"] == "q1_third_party_weaviate_unattended")
    asked = []
    result, db, *_ = run_case(tmp_path, case, interactive=True,
                              prompt=lambda s, c: asked.append((s, c.url)) or "adopt")
    assert asked == [("weaviate", "http://localhost:8081")]
    assert se.load_rows(db)["weaviate"].mode == "adopted_container"
    assert not result.weaviate_pending


def test_confirmation_probe_clears_after_adopt(tmp_path):
    case = next(c for c in CASES if c["id"] == "q1_third_party_weaviate_unattended")
    _result, db, *_ = run_case(tmp_path, case)
    assert sr.probe_confirmation_pending(None, db_path=db) is True
    se.write_rows([se.EndpointRow("weaviate", "adopted_container", 8081, "user_cli", grpc_port=50051,
                                  container_name="their_weaviate", confirmed_by_user=True)], db_path=db)
    assert sr.probe_confirmation_pending(None, db_path=db) is False


def test_the_detector_parses_docker_and_podman_inspect():
    docker = {"Name": "/w", "Config": {"Image": "semitechnologies/weaviate:1.28.4", "Labels": {}},
              "State": {"Status": "running"},
              "HostConfig": {"PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8081"}]}},
              "Mounts": [{"Type": "volume", "Name": "data", "Source": "/var/x", "Destination": "/var/lib/weaviate"}]}
    podman = {"Name": "w", "ImageName": "docker.io/semitechnologies/weaviate:1.28.4",
              "Config": {"Labels": None}, "State": {"Running": True},
              "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "8081"}]}}, "Mounts": []}
    for obj in (docker, podman):
        c = det.parse_inspect(obj)
        assert c is not None and c.name == "w" and c.running and c.host_ports[8080] == 8081
        assert det.service_of_container(c) == "weaviate"
    parsed = det.parse_inspect(docker)
    mount = parsed.mount_at("/var/lib/weaviate") if parsed is not None else None
    assert mount is not None and mount.source == "data"


def test_markers_are_one_list():
    assert det.weaviate_vco_markers(["Foo_knowledgegraph", "Articles"]) == ("Foo_knowledgegraph",)
    assert det.weaviate_vco_markers(["ChatMessages", "DocumentChunks"]) == ()
    assert det.ollama_vco_markers(["qwen3-embedding:0.6b", "llama3:8b"]) == ("qwen3-embedding:0.6b",)


def test_service_flags():
    assert sr.parse_service_flag("weaviate=adopt:container:w") == sr.Choice("weaviate", "adopt_container", "w")
    assert sr.parse_service_flag("ollama=adopt:url:http://h:1") == sr.Choice("ollama", "adopt_url", "http://h:1")
    assert sr.parse_service_flag("code_embed=vco:11441") == sr.Choice("code_embed", "vco", "11441")
    for bad in ("code_embed=adopt:url:http://h:1", "neo4j=vco", "weaviate=maybe"):
        with pytest.raises(ValueError):
            sr.parse_service_flag(bad)


def test_service_flag_grammar_fixture():
    """The ONE committed ``--service`` grammar table (tier C mirror, A>B>C):
    every case must parse here exactly as the table says, and be rejected
    where it says so. The Rust mirror —
    launcher/src-tauri/src/commands/installer.rs::parse_service_choice — runs
    the SAME file (``service_flag_grammar_matches_the_committed_fixture``);
    a case that disagrees is a drift in one of the two implementations, never
    a reason to fork the table.
    """
    table = json.loads((REPO_ROOT / "tests" / "fixtures" / "service_flag_grammar_cases.json")
                       .read_text(encoding="utf-8"))
    assert table["accept"] and table["reject"]
    for case in table["accept"]:
        expect = case["expect"]
        assert sr.parse_service_flag(case["input"]) == sr.Choice(
            expect["service"], expect["kind"], expect.get("value")), case["input"]
    for case in table["reject"]:
        with pytest.raises(ValueError):
            sr.parse_service_flag(case["input"])


def test_force_separate_names_only_managed_services(tmp_path, monkeypatch):
    """I1: VCT_FORCE_SEPARATE_CONTAINERS=1 no longer issues a bare `up -d`."""
    import install

    rows = {
        "weaviate": se.EndpointRow("weaviate", "adopted_container", 8081, "user_cli", grpc_port=50052,
                                   container_name="w"),
        "ollama": se.EndpointRow("ollama", "vco_managed", 11435, "install_probe"),
        "code_embed": se.EndpointRow("code_embed", "vco_managed", 11440, "install_probe", enabled=False),
    }
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": rows, "pinned": False, "weaviate_pending": False})
    monkeypatch.setenv("VCT_FORCE_SEPARATE_CONTAINERS", "1")
    seen = {}

    def fake_run(cmd, **_kw):
        if "up" in cmd:
            seen["cmd"] = list(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    sysinfo = mock.Mock(container_cmd="podman", has_gpu=False, gpu_vendor=None)
    with mock.patch.object(install, "_detect_existing_services",
                           return_value={"weaviate_url": None, "ollama_url": None, "code_embed_url": None}), \
         mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
         mock.patch.object(install, "_write_infrastructure_env"), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         mock.patch.object(install.subprocess, "run", side_effect=fake_run):
        install._start_services(sysinfo, argparse.Namespace(update=True), {}, decisions={})
    up = seen["cmd"][seen["cmd"].index("up"):]
    assert up[-1] == "ollama" and "weaviate" not in up


def test_awaiting_confirmation_skips_collections(monkeypatch):
    import install

    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": {}, "pinned": False, "weaviate_pending": True})
    with mock.patch.object(install, "_wait_for_weaviate_ready") as ready:
        install._ensure_collections({}, decisions={}, args=argparse.Namespace())
        assert install._seed_weaviate(argparse.Namespace()) is None
    ready.assert_not_called()


def test_dual_ollama_uses_the_row_port(monkeypatch):
    """An adopted Ollama on 11434 IS the one VCO uses: the dual-daemon check
    compares against the row's port, not the compiled 11435."""
    import install

    rows = {"ollama": se.EndpointRow("ollama", "adopted_external", 11434, "install_probe")}
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": rows, "pinned": True, "weaviate_pending": False})
    seen = {}

    def probe(canonical_port, alternate_port):
        seen["canonical"] = canonical_port
        return None

    monkeypatch.setattr(install, "_probe_dual_ollama_instances", probe)
    install._emit_dual_ollama_deferral(mock.Mock())
    assert seen["canonical"] == 11434


def test_reconcile_json_keeps_stdout_machine_readable(tmp_path, monkeypatch, capsys):
    """The launcher parses `reconcile --json` stdout: human lines go to stderr."""
    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")
    db = make_launcher_db(tmp_path / "launcher.db")
    root = _make_root(tmp_path, case)
    machine = FakeMachine(case.get("containers", ()), case.get("http"))
    real = sr.reconcile

    def fake(**kw):
        kw.update(run=machine.run, fetch=machine.fetch, tcp_open=machine.tcp_open,
                  port_free=machine.port_free, env={}, services_toml_path=tmp_path / "none.toml",
                  vct_config_dirs=[], apply_kwargs=Recorder().kwargs())
        return real(**kw)

    monkeypatch.setattr(sr, "reconcile", fake)
    monkeypatch.setattr(sr, "_runtime", lambda: "podman")
    rc = se._main(["reconcile", "--phase", "update", "--json", "--db-path", str(db), "--root", str(root)])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert rc == 0 and payload["rows"]["ollama"]["port"] == 11434
    assert "service_adopted_without_prompt" in payload["entries"]
    assert "[ollama]" in out.err


def test_a_waiting_weaviate_is_settled_by_a_later_run_that_can_answer(tmp_path):
    """Q1: the unattended run parks Weaviate; the next run with an answer
    (--on-conflict adopt, or a TTY) adopts it — and without one it keeps
    waiting."""
    case = next(c for c in CASES if c["id"] == "q1_third_party_weaviate_unattended")
    first, db, machine, rec, _root = run_case(tmp_path, case)
    assert first.weaviate_pending
    again, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec)
    assert again.weaviate_pending and se.load_rows(db)["weaviate"].enabled is False
    settled, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec, on_conflict="adopt")
    row = se.load_rows(db)["weaviate"]
    assert (row.mode, row.container_name, row.confirmed_by_user) == ("adopted_container", "their_weaviate", True)
    assert not settled.weaviate_pending


def test_step5_compose_argv_names_services_with_no_deps(monkeypatch):
    """Step 5 builds its argv with `service_lifecycle.compose_up_args`:
    only the named services, always `--no-deps` (so compose never pulls an
    adopted dependency up behind the named one)."""
    import install

    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": {}, "pinned": False, "weaviate_pending": False})
    monkeypatch.delenv("VCT_FORCE_SEPARATE_CONTAINERS", raising=False)
    seen = {}

    def fake_run(cmd, **_kw):
        if "up" in cmd:
            seen["cmd"] = list(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    decisions = {
        "weaviate": {"action": install.ACTION_START, "probe": install.PROBE_NOT_RUNNING},
        "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
        "code_embed": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
    }
    sysinfo = mock.Mock(container_cmd="podman", has_gpu=False, gpu_vendor=None)
    with mock.patch.object(install, "_detect_existing_services",
                           return_value={"weaviate_url": None, "ollama_url": None, "code_embed_url": None}), \
         mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
         mock.patch.object(install, "_write_infrastructure_env"), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         mock.patch.object(install.subprocess, "run", side_effect=fake_run):
        install._start_services(sysinfo, argparse.Namespace(update=True), {}, decisions=decisions)
    up = seen["cmd"][seen["cmd"].index("up"):]
    assert "--no-deps" in up and up[-1] == "weaviate"
    assert "ollama" not in up and "code_embed" not in up


# ─── round 2: bounded reads ─────────────────────────────────────────────


class _Trickle:
    """A response that keeps sending: 1000 one-byte chunks, then EOF."""

    status = 200

    def __init__(self):
        self.left = 1000

    def read1(self, _n):
        if self.left == 0:
            return b""
        self.left -= 1
        return b"x"

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_a_trickling_body_is_cut_off_by_the_deadline(monkeypatch):
    """Uncapped in size, bounded in time: a body that keeps trickling past
    the read deadline is "could not look" (None), never a partial body."""
    ticks = iter(range(10_000))
    monkeypatch.setattr(det, "open_probe", lambda *_a, **_k: _Trickle())
    got = det.default_fetch("http://h:1/v1/schema", 1.0, deadline_s=5, clock=lambda: float(next(ticks)))
    assert got is None
    # the same body within the deadline arrives WHOLE (no size cap)
    fast = det.default_fetch("http://h:1/v1/schema", 1.0, deadline_s=10_000, clock=lambda: 0.0)
    assert fast is not None and len(fast.body) == 1000


def test_an_unreadable_schema_is_unknown_not_third_party():
    """A Weaviate whose /v1/schema did not arrive is neither "holds VCO data"
    nor "third-party without data" — it is not usable until it can be read."""
    def fetch(url, _t):
        if url.endswith("/v1/schema"):
            return None  # the bounded read gave up
        if url.endswith("/v1/meta"):
            return det.HttpResponse(200, json.dumps({"version": "1.28.4"}))
        return det.HttpResponse(200, "")

    probe = det.probe_endpoint("weaviate", "http://localhost:8081", fetch=fetch)
    assert probe.schema_unread and probe.is_service
    ok, why = det.compatibility("weaviate", probe, 50052)
    assert not ok and "/v1/schema" in why


# ─── round 2: move (plan §4g) ───────────────────────────────────────────


def _seed(tmp_path, rows):
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows(rows, db_path=db)
    return db


def _move(tmp_path, db, machine, rec, service, **kw):
    lines: list[str] = []
    rc = sr.move_endpoint(service, orchestrator_root=tmp_path, db_path=db, runtime="podman",
                          run=machine.run, fetch=machine.fetch, tcp_open=machine.tcp_open,
                          port_free=machine.port_free, out=lines.append,
                          apply_kwargs=rec.kwargs(), **kw)
    return rc, lines


_OLLAMA_C = {"name": "their_ollama", "image": "ollama/ollama:latest", "state": "running",
             "project": "theirs", "ports": {"11434": 11500}}


def test_move_follows_an_adopted_container_to_its_new_port(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("ollama", "adopted_container", 11434, "user_cli",
                                         container_name="their_ollama")])
    machine = FakeMachine([_OLLAMA_C], {"localhost:11500": {"service": "ollama", "models": []}})
    rec = Recorder()
    rc, _ = _move(tmp_path, db, machine, rec, "ollama")
    row = se.load_rows(db)["ollama"]
    assert rc == 0 and (row.port, row.container_name, row.source) == (11500, "their_ollama", "user_cli")
    assert rec.reprojected == 1  # the follow-up chain ran (infra .env, projection, registration)
    assert not any(a[1] in ("rm", "stop", "run", "compose") for a in machine.argv_log)


def test_move_never_moves_the_container_itself(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("ollama", "adopted_container", 11434, "user_cli",
                                         container_name="their_ollama")])
    machine = FakeMachine([_OLLAMA_C], {"localhost:11500": {"service": "ollama", "models": []}})
    rc, lines = _move(tmp_path, db, machine, Recorder(), "ollama", port=11600)
    assert rc == 1 and "does not move it" in lines[-1]
    assert se.load_rows(db)["ollama"].port == 11434


def test_move_on_an_external_endpoint_verifies_the_new_url(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("ollama", "adopted_external", 11434, "user_cli")])
    machine = FakeMachine(http={"localhost:11500": {"service": "ollama", "models": []}})
    rc, _ = _move(tmp_path, db, machine, Recorder(), "ollama", url="http://localhost:11999")
    assert rc == 1 and se.load_rows(db)["ollama"].port == 11434  # nothing answers there
    rc, _ = _move(tmp_path, db, machine, Recorder(), "ollama", url="http://localhost:11500")
    assert rc == 0 and se.load_rows(db)["ollama"].port == 11500


def test_move_never_trades_vco_data_for_an_empty_weaviate_silently(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "adopted_external", 18081, "user_cli",
                                         grpc_port=50061)])
    machine = FakeMachine(http={
        "localhost:18081": {"service": "weaviate", "version": "1.28.4", "classes": ["Alpha_KnowledgeGraph"]},
        "localhost:18090": {"service": "weaviate", "version": "1.28.4", "classes": []},
    }, tcp_open=["localhost:50052"])
    rc, lines = _move(tmp_path, db, machine, Recorder(), "weaviate", url="http://localhost:18090")
    assert rc == 1 and "--accept-empty-kg" in lines[-1]
    assert se.load_rows(db)["weaviate"].port == 18081
    rc, _ = _move(tmp_path, db, machine, Recorder(), "weaviate", url="http://localhost:18090",
                  accept_empty_kg=True)
    assert rc == 0 and (se.load_rows(db)["weaviate"].port, se.load_rows(db)["weaviate"].grpc_port) == (18090, 50052)


def test_move_recreates_vcos_own_weaviate_with_its_grpc_port(tmp_path):
    """Orchestrator decision 2026-09-24: "never moved" is about ADOPTED
    services. VCO's own Weaviate moves by recreate (lane Z); its gRPC port
    moves with it, keeping the offset unless --grpc-port says otherwise."""
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "vco_managed", 8081, "install_probe", grpc_port=50052)])
    migrate = _FakeMigrate("migrated")
    rc, _ = _move(tmp_path, db, FakeMachine(), Recorder(), "weaviate", port=18081, migrate=migrate)
    [asked] = migrate.calls
    assert (asked.port, asked.grpc_port) == (18081, 60052)
    assert rc == 0 and (se.load_rows(db)["weaviate"].port, se.load_rows(db)["weaviate"].grpc_port) == (18081, 60052)
    migrate = _FakeMigrate("migrated")
    rc, _ = _move(tmp_path, db, FakeMachine(), Recorder(), "weaviate", grpc_port=60099, migrate=migrate)
    assert rc == 0 and (migrate.calls[0].port, migrate.calls[0].grpc_port) == (18081, 60099)


def test_move_of_vcos_own_service_takes_ports_not_urls(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("ollama", "vco_managed", 11435, "install_probe")])
    migrate = _FakeMigrate("migrated")
    rc, lines = _move(tmp_path, db, FakeMachine(), Recorder(), "ollama", url="http://localhost:11500",
                      migrate=migrate)
    assert rc == 2 and "moves by port only" in lines[-1] and migrate.calls == []
    rc, _ = _move(tmp_path, db, FakeMachine(busy_ports=[11500]), Recorder(), "ollama", port=11500,
                  migrate=migrate)
    assert rc == 1 and migrate.calls == []
    assert se.load_rows(db)["ollama"].port == 11435


def test_a_refused_weaviate_move_leaves_the_row(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "vco_managed", 8081, "install_probe", grpc_port=50052)])
    rc, lines = _move(tmp_path, db, FakeMachine(), Recorder(), "weaviate", port=18081,
                      migrate=_FakeMigrate("refused", "class list unreadable"))
    assert rc == 1 and "class list unreadable" in lines[-1]
    assert (se.load_rows(db)["weaviate"].port, se.load_rows(db)["weaviate"].grpc_port) == (8081, 50052)


class _FakeMigrate:
    """service_lifecycle.migrate_code_embed's contract: it records the row it
    is given (commit) BEFORE the recreate, then reports a status."""

    def __init__(self, status, reason=""):
        self.status, self.reason, self.calls = status, reason, []

    def __call__(self, root, row, *, runtime, log, db_path, commit):
        self.calls.append(row)
        if self.status != "not_needed":
            commit([row])
        return mock.Mock(status=self.status, reason=self.reason)


def _code_embed_db(tmp_path):
    return _seed(tmp_path, [se.EndpointRow(
        "code_embed", "vco_managed", 11440, "install_probe", container_name="vco_code_embed",
        data_mount={"kind": "bind", "source": "/srv/cache", "destination": "/cache"})])


def test_move_code_embed_recreates_it_with_its_cache_on_the_new_port(tmp_path):
    db = _code_embed_db(tmp_path)
    migrate = _FakeMigrate("migrated")
    rc, _ = _move(tmp_path, db, FakeMachine(), Recorder(), "code_embed", port=11441, migrate=migrate)
    [asked] = migrate.calls
    assert (asked.port, dict(asked.data_mount)["source"]) == (11441, "/srv/cache")
    assert rc == 0 and se.load_rows(db)["code_embed"].port == 11441


def test_a_refused_code_embed_move_leaves_the_row_where_the_service_is(tmp_path):
    db = _code_embed_db(tmp_path)
    rc, lines = _move(tmp_path, db, FakeMachine(), Recorder(), "code_embed", port=11441,
                      migrate=_FakeMigrate("refused", "cache bind missing"))
    assert rc == 1 and se.load_rows(db)["code_embed"].port == 11440
    assert "cache bind missing" in lines[-1]


def test_a_failed_code_embed_move_follows_where_it_answers(tmp_path):
    db = _code_embed_db(tmp_path)
    machine = FakeMachine(http={"localhost:11441": {"service": "code_embed", "body": "codesage"}})
    rc, _ = _move(tmp_path, db, machine, Recorder(), "code_embed", port=11441,
                  migrate=_FakeMigrate("failed", "model not loaded; re-up done"))
    assert rc == 1 and se.load_rows(db)["code_embed"].port == 11441


def test_move_code_embed_without_a_container_records_the_port(tmp_path):
    db = _code_embed_db(tmp_path)
    rc, _ = _move(tmp_path, db, FakeMachine(), Recorder(), "code_embed", port=11441,
                  migrate=_FakeMigrate("not_needed"))
    assert rc == 0 and se.load_rows(db)["code_embed"].port == 11441


def test_move_code_embed_to_a_taken_port_is_refused(tmp_path):
    db = _code_embed_db(tmp_path)
    migrate = _FakeMigrate("migrated")
    rc, _ = _move(tmp_path, db, FakeMachine(busy_ports=[11441]), Recorder(), "code_embed",
                  port=11441, migrate=migrate)
    assert rc == 1 and migrate.calls == []


def test_move_is_a_cli_verb():
    args = se._build_arg_parser().parse_args(
        ["move", "--service", "weaviate", "--url", "http://h:1", "--grpc-port", "2", "--accept-empty-kg"])
    assert (args.service, args.url, args.grpc_port, args.accept_empty_kg) == ("weaviate", "http://h:1", 2, True)


# ─── round 2: vco doctor warns about retired endpoint env ───────────────


def test_doctor_warns_while_a_retired_endpoint_variable_is_exported(tmp_path):
    from vco_lib import doctor

    fn, scopes = doctor.PROBES["retired_endpoint_env"]
    assert scopes == (doctor.SCOPE_FULL,)
    quiet = fn(tmp_path, doctor.DoctorResolvers(environ=lambda: {"WEAVIATE_URL": "http://x:1"}), {})
    assert [f.status for f in quiet] == [doctor.STATUS_OK]  # projected transport is not judged
    [finding] = fn(tmp_path, doctor.DoctorResolvers(
        environ=lambda: {"VCT_WEAVIATE_URL": "http://kg.lan:9000", "VCT_GRPC_PORT": "50099"}), {})
    assert finding.status == doctor.STATUS_PROBLEM and finding.condition_id == ""
    assert "VCT_WEAVIATE_URL=http://kg.lan:9000" in finding.summary
    assert "unset VCT_GRPC_PORT VCT_WEAVIATE_URL" in finding.command
    adopt = next(line for line in finding.command.splitlines() if line.startswith("python -m"))
    args = se._build_arg_parser().parse_args(adopt.split()[3:])  # a printed command parses
    assert (args.service, args.url) == ("weaviate", "http://kg.lan:9000")


# ─── round 2 add-ons ────────────────────────────────────────────────────

PARITY = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "service_endpoint_parity.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", PARITY["awaits_choice_cases"], ids=lambda c: c["name"])
def test_awaits_choice_parity(case):
    """The shared table the Rust `awaits_choice` runs too."""
    raw = case["row"]
    row = None
    if raw is not None:
        row = se.EndpointRow(service=case["service"], source="install_probe", **raw)
    assert se.awaits_choice(case["service"], row) is case["expect"]


@pytest.mark.parametrize("case", PARITY["hand_to_vco_cases"], ids=lambda c: c["name"])
def test_hand_to_vco_row_rule_parity(case):
    """The row checks `hand-to-vco` makes before asking the runtime — the
    shared table the Rust `hand_to_vco_allowed` (which gates the Services
    page's "Let VCO manage it") runs too."""
    raw = case["row"]
    row = None
    if raw is not None:
        row = se.EndpointRow(service=case["service"], source="install_probe", **raw)
    assert (sr.hand_to_vco_refusal(case["service"], row) is None) is case["expect"]


def test_the_confirmation_probe_is_the_parity_rule(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "vco_managed", 8082, "install_probe",
                                         grpc_port=50053, enabled=False)])
    assert sr.probe_confirmation_pending(None, db_path=db) is True


def test_install_takes_no_port_from_env(monkeypatch):
    """Rows (or step [5b]'s pins) are the only port source in install.py:
    exported WEAVIATE_PORT / OLLAMA_PORT / CODE_EMBED_PORT change nothing."""
    import install

    rows = {"weaviate": se.EndpointRow("weaviate", "vco_managed", 18081, "user_cli", grpc_port=51081),
            "ollama": se.EndpointRow("ollama", "adopted_external", 11434, "user_cli"),
            "code_embed": se.EndpointRow("code_embed", "vco_managed", 11441, "user_cli")}
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": rows, "pinned": True, "weaviate_pending": False})
    for key, val in (("WEAVIATE_PORT", "1"), ("OLLAMA_PORT", "2"), ("CODE_EMBED_PORT", "3"),
                     ("WEAVIATE_GRPC_PORT", "4")):
        monkeypatch.setenv(key, val)
    class Stop(Exception):
        pass

    seen = {}

    def capture(*ports):
        seen["ports"] = ports
        raise Stop()

    with mock.patch.object(install, "_detect_existing_services", side_effect=capture), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         pytest.raises(Stop):
        install._start_services(mock.Mock(container_cmd="podman", has_gpu=False),
                                argparse.Namespace(update=True), {}, decisions={})
    assert seen["ports"] == (18081, 11434, 11441)
    urls = []

    def fake_urlopen(url, **_k):
        urls.append(url)
        raise OSError("nothing there")

    with mock.patch.object(install.urllib.request, "urlopen", side_effect=fake_urlopen), \
         mock.patch.object(install.time, "sleep", side_effect=Stop()), pytest.raises(Stop):
        install._wait_for_ollama()
    assert urls == ["http://localhost:11434/api/tags"]
    assert install._bootstrap_weaviate_endpoints()["base"] == "http://localhost:18081"


def test_the_dual_ollama_remedy_names_a_command_that_exists(monkeypatch):
    import install

    monkeypatch.setattr(install, "_probe_dual_ollama_instances", lambda **_k: (11434, 11435))
    report = mock.Mock()
    install._emit_dual_ollama_deferral(report, canonical_port=11435)
    entry = report.add_entry.call_args.args[0]
    cmds = [ln for ln in entry.command_to_apply.splitlines() if ln.startswith("python -m vco_lib.service_endpoints")]
    assert cmds, entry.command_to_apply
    args = se._build_arg_parser().parse_args(cmds[0].split()[3:])
    assert (args.service, args.url) == ("ollama", "http://localhost:11434")
    assert "OLLAMA_PORT=" not in entry.command_to_apply


def test_gpu_profile_appears_once_in_the_step5_argv(monkeypatch, tmp_path):
    """compose_up_args(prefix=cmd): the GPU overlay already enables the
    profile, so naming code_embed must not add a second `--profile gpu`."""
    import install

    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": {}, "pinned": False, "weaviate_pending": False})
    monkeypatch.delenv("VCT_FORCE_SEPARATE_CONTAINERS", raising=False)
    seen = {}

    def fake_run(cmd, **_kw):
        if "up" in cmd:
            seen["cmd"] = list(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    decisions = {s: {"action": install.ACTION_START, "probe": install.PROBE_NOT_RUNNING}
                 for s in ("weaviate", "ollama", "code_embed")}
    sysinfo = install.SystemInfo(os_name="Linux", has_gpu=True, has_metal=False, container_cmd="docker",
                                 gpu_name="RTX", vram_gb=24.0, ram_gb=64.0, gpu_vendor="nvidia")
    from vco_lib import code_embed_image

    state = code_embed_image.ImageState(code_embed_image.CURRENT, "current")
    with mock.patch.object(install, "_detect_existing_services",
                           return_value={"weaviate_url": None, "ollama_url": None, "code_embed_url": None}), \
         mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
         mock.patch.object(install, "_write_infrastructure_env"), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         mock.patch.object(install, "_get_compose_command", return_value=["docker", "compose"]), \
         mock.patch.object(code_embed_image, "image_state", return_value=state), \
         mock.patch.object(install.subprocess, "run", side_effect=fake_run):
        install._start_services(sysinfo, argparse.Namespace(update=True), {}, decisions=decisions)
    cmd = seen["cmd"]
    assert cmd.count("--profile") == 1 and "code_embed" in cmd and "--no-deps" in cmd


def test_a_chain_killed_mid_way_is_rerun_by_the_next_reconcile(tmp_path):
    """SE-3 item 2: the row committed, then the chain died (the session
    hook's 8 s kill) before it finished. The next reconcile sees no row
    change — and still re-runs the chain, because the propagated digest is
    behind the rows. Once it completes, the run after that runs nothing."""
    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")

    class Killed(BaseException):
        pass

    dying = Recorder()
    kw = dying.kwargs()

    def killed(_db):
        raise Killed()

    kw["reproject"] = killed
    with pytest.raises(Killed):
        run_case(tmp_path, case, apply_kwargs=kw)
    db = tmp_path / "launcher.db"
    assert se.load_rows(db)["ollama"].port == 11434  # the rows made it
    assert se.propagated_digest(db) is None           # the chain did not
    rec = Recorder()
    again, *_ = run_case(tmp_path, case, db=db, recorder=rec)
    assert again.written == [] and rec.reprojected == 1 and rec.registered == 1
    assert se.propagated_digest(db) == se.rows_digest(se.load_rows(db))
    third, *_ = run_case(tmp_path, case, db=db, recorder=rec)
    assert rec.reprojected == 1  # converged: nothing re-runs


def test_the_session_phase_commits_rows_but_leaves_the_chain_to_the_next_update(tmp_path):
    """Session start writes the drifted row and runs NO chain (its hook is
    killed after 8 s); the next update-phase reconcile runs it."""
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, _root = run_case(tmp_path, case)
    machine.containers["vco_ollama"]["ports"] = {"11434": 11500}
    machine.http["localhost:11500"] = machine.http.pop("localhost:11435")
    session, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec, phase="session")
    assert session.written == ["ollama"] and rec.reprojected == 1  # no chain at session start
    assert se.propagated_digest(db) != se.rows_digest(se.load_rows(db))
    update, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec)
    assert update.written == [] and rec.reprojected == 2

