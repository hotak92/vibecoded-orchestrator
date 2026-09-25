# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review round 7a — the service-endpoint fixes (F1–F4, F7–F11, O-A2).

Every reconcile case runs ``vco_lib.service_reconcile.reconcile`` end to end
against the FAKE machine of ``tests/test_v0297_service_reconcile.py`` (a
scripted runtime, scripted HTTP answers, a real-schema launcher.db under
``tmp_path``). The redirect cases use a loopback HTTP server this test
starts on an ephemeral port — never a real service. The F1 table cases live
in ``tests/fixtures/service_endpoint_migration_cases.json``; the hook legs of
F3/F10 in ``tests/test_v0297_lifecycle_hooks.py``.
"""
from __future__ import annotations

import builtins
import http.server
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from tests.test_v0297_service_reconcile import CASES, FakeMachine, run_case
from vco_lib import service_adoption as sa
from vco_lib import service_detection as det
from vco_lib import service_endpoints as se
from vco_lib import service_lifecycle as sl
from vco_lib import service_reconcile as sr

LEGACY_HOME = "/home/u/vco/claude_mcp_servers"


def _seed(tmp_path: Path, rows) -> Path:
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows(rows, db_path=db)
    return db


def _existing_case(**kw: Any) -> dict:
    """A case for a run that finds rows already written (no legacy import)."""
    case: dict[str, Any] = {"phase": "update", "has_gpu": False}
    case.update(kw)
    return case


def _cids(result) -> list[str]:
    return [e.condition_id for e in result.entries]


class _Formatted(FakeMachine):
    """The fake machine, answering ``inspect --format …`` the way a real
    runtime does: the container's id for an ``.Id`` template, exit 125 for a
    name that does not exist (the base fake answers ``[]``)."""

    def run(self, argv, **kw):
        if len(argv) > 1 and argv[1] == "inspect" and "--format" in argv:
            self.argv_log.append(list(argv))
            name = argv[-1]
            if name not in self.containers:
                return mock.Mock(returncode=125, stdout="", stderr=f"no such container {name}")
            fmt = argv[argv.index("--format") + 1]
            out = f'"id-{name}"' if "json .Id" in fmt else f"id-{name}" if ".Id" in fmt else ""
            return mock.Mock(returncode=0, stdout=out + "\n", stderr="")
        return super().run(argv, **kw)


class _Logged(FakeMachine):
    """The fake machine, recording every URL fetched."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.urls: list[str] = []

    def fetch(self, url, timeout):
        self.urls.append(url)
        return super().fetch(url, timeout)


# ─── F4: an interrupted question is not consent ─────────────────────────


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_an_interrupted_prompt_is_undecided_never_adopt(interrupt, capsys):
    cand = det.Candidate("weaviate", "localhost", 8081)
    with mock.patch.object(builtins, "input", side_effect=interrupt()):
        assert sr._interactive_prompt("weaviate", cand) == sr.UNDECIDED
    assert "no answer" in capsys.readouterr().out


def test_a_ctrl_c_at_the_weaviate_question_parks_it_instead_of_writing_into_it(tmp_path):
    case = next(c for c in CASES if c["id"] == "q1_third_party_weaviate_unattended")
    with mock.patch.object(builtins, "input", side_effect=KeyboardInterrupt()):
        result, db, *_ = run_case(tmp_path, case, interactive=True, prompt=sr._interactive_prompt)
    row = se.load_rows(db)["weaviate"]
    assert (row.mode, row.enabled) == ("vco_managed", False)  # parked, not adopted
    assert result.weaviate_pending
    assert "service_adoption_confirmation_required" in _cids(result)


# ─── F7: probes never follow a redirect; no userinfo in an endpoint ─────


class _Redirector(http.server.BaseHTTPRequestHandler):
    landed = 0

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/landed"):
            type(self).landed += 1
            body = b'{"models": [{"name": "qwen3-embedding:0.6b"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        port = int(getattr(self.server, "server_port"))
        self.send_header("Location", f"http://127.0.0.1:{port}/landed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        pass


@pytest.fixture
def redirector():
    _Redirector.landed = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_detector_probe_does_not_follow_a_redirect(redirector):
    got = det.default_fetch(f"{redirector}/api/tags", 3.0)
    assert got is not None and got.status == 302
    assert _Redirector.landed == 0
    probe = det.probe_endpoint("ollama", redirector, fetch=det.default_fetch)
    assert not probe.is_service and not probe.vco_markers  # "answers, but not as the service"


def test_the_lifecycle_and_adoption_probes_do_not_follow_a_redirect(redirector):
    assert sl._default_fetch_json(f"{redirector}/api/tags", 3.0) is None
    assert sa._default_fetch(f"{redirector}/health", 3.0) is None
    assert _Redirector.landed == 0


@pytest.mark.parametrize("url", ["http://user:pw@weaviate.lan:8081", "http://h:1@evil.example:11434",
                                 "https://token@ollama.example.com"])
def test_a_url_with_userinfo_is_not_an_endpoint(url):
    assert sr.parse_url(url) is None
    with pytest.raises(ValueError):
        sr.parse_service_flag(f"weaviate=adopt:url:{url}")


# ─── O-A2: one inspect-mount parser ─────────────────────────────────────


def test_the_detector_reads_mounts_through_the_one_parser(monkeypatch):
    seen: list[dict] = []

    def one(entry):
        seen.append(entry)
        return sa.MountSpec("bind", "/from/the/one/parser", "/root/.ollama", "Z")

    monkeypatch.setattr(sa, "mount_from_inspect", one)
    info = det.parse_inspect({"Name": "c", "Config": {"Image": "x", "Labels": {}}, "State": {},
                              "Mounts": [{"Type": "bind", "Source": "/a", "Destination": "/b"}]})
    assert info is not None and seen
    assert info.mounts == (det.Mount("bind", "/from/the/one/parser", "/root/.ollama"),)


@pytest.mark.parametrize("entry,key", [
    ({"Type": "bind", "Source": "/srv/m", "Destination": "/root/.ollama", "Mode": "Z"},
     ("bind", "/srv/m", "/root/.ollama")),
    ({"Type": "volume", "Name": "vco_weaviate_data", "Source": "/var/lib/x/_data",
      "Destination": "/var/lib/weaviate", "Options": ["rbind", "Z"]},
     ("volume", "vco_weaviate_data", "/var/lib/weaviate")),
    ({"Type": "tmpfs", "Destination": "/tmp"}, None),
])
def test_detection_and_adoption_agree_on_every_mount_shape(entry, key):
    spec = sa.mount_from_inspect(entry)
    assert (spec.key() if spec else None) == key
    info = det.parse_inspect({"Name": "c", "Config": {}, "State": {}, "Mounts": [entry]})
    assert info is not None
    assert [(m.kind, m.source, m.destination) for m in info.mounts] == ([key] if key else [])


# ─── F2: hand-to-vco takes over THE row's container, or nothing ─────────


class _AdoptSpy:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, root, **kw):
        self.calls.append(kw)
        return sa.AdoptionResult(adopted=list(kw.get("services", ())))


def _hand(tmp_path, db, machine, spy, service="weaviate"):
    lines: list[str] = []
    rc = sr.hand_to_vco(service, orchestrator_root=tmp_path, runtime="podman", db_path=db,
                        run=machine.run, adopt=spy, out=lines.append)
    return rc, lines


_VCO_WEAVIATE_STALE = {"name": "vco_weaviate", "image": "semitechnologies/weaviate:1.28.4",
                       "state": "exited", "project": "vibecoded", "working_dir": LEGACY_HOME,
                       "ports": {"8080": 8081, "50051": 50052}}
_THEIR_WEAVIATE = {"name": "their_weaviate", "image": "semitechnologies/weaviate:1.30.0",
                   "state": "running", "project": "theirs", "ports": {"8080": 8090, "50051": 50061}}


def test_hand_to_vco_never_takes_over_a_canonically_named_stale_container(tmp_path):
    """The row names their_weaviate; a stale vco_weaviate exists. The takeover
    must not touch vco_weaviate — and their_weaviate cannot be taken over
    under its own name (VCO's compose creates `vco_weaviate`), so: refused,
    nothing touched."""
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "adopted_container", 8090, "user_cli",
                                         grpc_port=50061, container_name="their_weaviate")])
    machine = _Formatted([_VCO_WEAVIATE_STALE, _THEIR_WEAVIATE])
    spy = _AdoptSpy()
    rc, lines = _hand(tmp_path, db, machine, spy)
    assert rc == 1 and spy.calls == [], lines
    assert "their_weaviate" in lines[-1] and "vco_weaviate" in lines[-1]
    assert se.load_rows(db)["weaviate"].container_name == "their_weaviate"


def test_hand_to_vco_passes_the_rows_container_to_the_adoption(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("weaviate", "adopted_container", 8081, "migrated:legacy_compose",
                                         grpc_port=50052, container_name="vco_weaviate")])
    machine = _Formatted([dict(_VCO_WEAVIATE_STALE, state="running")])
    spy = _AdoptSpy()
    rc, _lines = _hand(tmp_path, db, machine, spy)
    assert rc == 0
    assert spy.calls[0]["container_refs"] == {"weaviate": "vco_weaviate"}
    assert spy.calls[0]["services"] == ("weaviate",)


@pytest.mark.parametrize("rows,containers,why", [
    ([], [], "no row"),
    ([se.EndpointRow("ollama", "adopted_external", 11434, "user_cli")], [], "by URL"),
    ([se.EndpointRow("ollama", "adopted_container", 11435, "user_cli", container_name="vco_ollama")],
     [], "does not exist"),
])
def test_hand_to_vco_refuses_without_a_container_to_hand_over(tmp_path, rows, containers, why):
    db = make_launcher_db(tmp_path / "launcher.db")
    if rows:
        se.write_rows(rows, db_path=db)
    spy = _AdoptSpy()
    rc, lines = _hand(tmp_path, db, _Formatted(containers), spy, service="ollama")
    assert rc == 1 and spy.calls == [] and why in lines[-1], lines


def test_the_adoption_plans_exactly_the_named_container(tmp_path, monkeypatch):
    """`_plan_all` with a named container never searches the canonical names
    (a stale `vco_weaviate` would be found first)."""
    def no_search(*_a, **_k):
        raise AssertionError("searched by canonical name")

    monkeypatch.setattr(sa._containers, "find_existing_container", no_search)
    (tmp_path / "infrastructure").mkdir()
    (tmp_path / "infrastructure" / "docker-compose.yml").write_text("services: {}\n")
    machine = _Formatted([dict(_VCO_WEAVIATE_STALE, state="running")])
    plans, _files = sa._plan_all(tmp_path, "podman", machine.run, lambda _m: None,
                                 services=("weaviate",), container_refs={"weaviate": "vco_weaviate"})
    assert plans[0].container == "vco_weaviate"
    plans, _files = sa._plan_all(tmp_path, "podman", machine.run, lambda _m: None,
                                 services=("weaviate",), container_refs={"weaviate": "gone"})
    assert "does not exist" in plans[0].reason


def test_the_drift_remedy_names_hand_to_vco_only_where_it_can_act(tmp_path):
    root = tmp_path / "orch"
    (root / "infrastructure").mkdir(parents=True)
    (root / "infrastructure" / "docker-compose.yml").write_text(
        "      PERSISTENCE_LSM_MAX_SEGMENT_SIZE: 2GiB\n")
    result = sr.ReconcileResult(
        rows={"weaviate": se.EndpointRow("weaviate", "adopted_container", 8090, "user_cli",
                                         grpc_port=50061, container_name="their_weaviate")},
        detection=det.Detection(containers=[det.ContainerInfo("their_weaviate", "w", "running")]))
    entry = next(e for e in sr._outcome_entries(result, root) if e.condition_id == sr.CID_CONFIG_DRIFT)
    assert "hand-to-vco" not in entry.command_to_apply
    assert "their_weaviate (weaviate) is yours" in entry.command_to_apply


# ─── F9: a vco_managed port answered by someone else's instance ─────────


_VCO_W_STOPPED = {"name": "vco_weaviate", "image": "semitechnologies/weaviate:1.28.4", "state": "exited",
                  "project": "infrastructure", "service_label": "weaviate",
                  "ports": {"8080": 8081, "50051": 50052},
                  "mounts": [{"Type": "volume", "Name": "vco_weaviate_data", "Source": "/v/_data",
                              "Destination": "/var/lib/weaviate"}]}
_OTHER_W_ON_8081 = {"name": "other_weaviate", "image": "semitechnologies/weaviate:1.30.0",
                    "state": "running", "project": "otherapp", "working_dir": "/srv/other",
                    "ports": {"8080": 8081, "50051": 50051}}
_MANAGED_W = se.EndpointRow("weaviate", "vco_managed", 8081, "install_probe", grpc_port=50052,
                            container_name="vco_weaviate",
                            data_mount={"kind": "volume", "source": "vco_weaviate_data",
                                        "destination": "/var/lib/weaviate"})


@pytest.mark.parametrize("phase", ["update", "session"])
def test_a_third_party_weaviate_on_vcos_port_is_never_written_into(tmp_path, phase):
    db = _seed(tmp_path, [_MANAGED_W])
    machine = FakeMachine([_VCO_W_STOPPED, _OTHER_W_ON_8081],
                          {"localhost:8081": {"service": "weaviate", "version": "1.30.0",
                                              "classes": ["Articles"]}},
                          busy_ports=[8081, 50051, 50052])
    result, *_ = run_case(tmp_path, _existing_case(phase=phase), db=db, machine=machine)
    row = se.load_rows(db)["weaviate"]
    assert (row.mode, row.enabled) == ("vco_managed", False)
    assert row.port != 8081 and row.verified_at is None
    assert row.container_name == "vco_weaviate" and row.data_mount == _MANAGED_W.data_mount
    assert {"service_adoption_confirmation_required", "service_endpoint_unreachable"} <= set(_cids(result))
    assert "weaviate" not in se.plan(se.load_rows(db))["managed_services"]


def test_vcos_own_weaviate_holding_the_data_on_its_port_is_kept(tmp_path):
    db = _seed(tmp_path, [_MANAGED_W])
    machine = FakeMachine([dict(_VCO_W_STOPPED, state="running")],
                          {"localhost:8081": {"service": "weaviate", "version": "1.28.4",
                                              "classes": ["Alpha_KnowledgeGraph"]}})
    result, *_ = run_case(tmp_path, _existing_case(), db=db, machine=machine)
    row = se.load_rows(db)["weaviate"]
    assert (row.port, row.enabled, row.verified_at) == (8081, True, 1_700_000_000_000)
    assert "service_adoption_confirmation_required" not in _cids(result)


_MANAGED_O = se.EndpointRow("ollama", "vco_managed", 11435, "install_probe", container_name="vco_ollama")
_VCO_O_STOPPED = {"name": "vco_ollama", "image": "ollama/ollama:0.20.2", "state": "exited",
                  "project": "infrastructure", "service_label": "ollama", "ports": {"11434": 11435}}
_NATIVE_O = {"localhost:11435": {"service": "ollama", "models": ["llama3:8b"]}}


def test_a_foreign_ollama_on_the_port_of_vcos_stopped_one_is_reported_not_adopted(tmp_path):
    db = _seed(tmp_path, [_MANAGED_O])
    result, *_ = run_case(tmp_path, _existing_case(phase="session"), db=db,
                          machine=FakeMachine([_VCO_O_STOPPED], _NATIVE_O))
    row = se.load_rows(db)["ollama"]
    assert (row.mode, row.port, row.verified_at) == ("vco_managed", 11435, None)
    assert "service_endpoint_unreachable" in _cids(result)


def test_with_vcos_ollama_gone_the_one_on_its_port_is_adopted_with_a_record(tmp_path):
    db = _seed(tmp_path, [_MANAGED_O])
    machine = FakeMachine([dict(_VCO_O_STOPPED, name="unrelated", service_label="x",
                                image="busybox", ports={})], _NATIVE_O)
    # vco_ollama does not exist; the answer on 11435 has no container: with
    # the row's own container not known stopped, nothing is concluded …
    result, *_ = run_case(tmp_path, _existing_case(), db=db, machine=machine)
    assert se.load_rows(db)["ollama"].mode == "vco_managed"
    # … but a third-party CONTAINER publishing 11435 is positive evidence.
    machine = FakeMachine([{"name": "their_ollama", "image": "ollama/ollama:latest", "state": "running",
                            "project": "theirs", "ports": {"11434": 11435}}], _NATIVE_O)
    result, *_ = run_case(tmp_path, _existing_case(), db=db, machine=machine)
    row = se.load_rows(db)["ollama"]
    assert (row.mode, row.container_name, row.port) == ("adopted_container", "their_ollama", 11435)
    assert "service_adopted_without_prompt" in _cids(result)


# ─── F11: a stopped foreign code_embed is migrated on EVERY run ─────────


def test_a_stopped_foreign_code_embed_is_migrated_not_composed_into_a_name_conflict(tmp_path):
    db = _seed(tmp_path, [se.EndpointRow("code_embed", "vco_managed", 11440, "install_probe",
                                         container_name="vco_code_embed", enabled=True)])
    machine = FakeMachine([{"name": "vco_code_embed", "image": "localhost/vibecoded_code_embed:latest",
                            "state": "exited", "project": "vibecoded", "working_dir": LEGACY_HOME,
                            "service_label": "code_embed", "ports": {"11440": 11440},
                            "mounts": [{"Type": "bind", "Source": "/home/u/volumes/code_embed_cache",
                                        "Destination": "/cache"}]}])
    result, *_ = run_case(tmp_path, _existing_case(has_gpu=True), db=db, machine=machine)
    assert result.migrate_code_embed is True
    assert result.disposition("code_embed") == "skip"  # never `compose up` into the name conflict


# ─── F8: the session phase checks the rows, not the machine ─────────────


def test_the_session_reconcile_looks_only_at_the_recorded_rows(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, _root = run_case(tmp_path, case)
    logged = _Logged(list(machine.containers.values()), machine.http)
    second, *_ = run_case(tmp_path, case, db=db, machine=logged, recorder=rec, phase="session")
    assert second.abort is None
    ports = {u.split("://", 1)[1].split("/", 1)[0] for u in logged.urls}
    assert ports <= {"localhost:8081", "localhost:11435", "localhost:11440"}, ports
    assert not [u for u in logged.urls if u.endswith("/v1/schema")], logged.urls
    inspected = {n for argv in logged.argv_log if argv[1] == "inspect" for n in argv[2:]
                 if not n.startswith("--") and n != "container"}
    assert inspected == {"vco_weaviate", "vco_ollama", "vco_code_embed"}, inspected


def test_the_session_reconcile_never_settles_a_parked_weaviate(tmp_path):
    """A parked row waits on a Weaviate the narrowed check does not look at;
    "it is gone" is a full run's verdict, never a session's."""
    case = next(c for c in CASES if c["id"] == "q1_third_party_weaviate_unattended")
    _first, db, machine, rec, _root = run_case(tmp_path, case)
    before = se.load_rows(db)["weaviate"]
    run_case(tmp_path, case, db=db, machine=machine, recorder=rec, phase="session")
    after = se.load_rows(db)["weaviate"]
    assert (after.enabled, after.port) == (before.enabled, before.port) == (False, 8082)


# ─── F10: the session lock and the one-reconcile window ─────────────────


def test_a_busy_session_lock_runs_nothing(tmp_path, monkeypatch, capsys):
    import fcntl

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    lock = sl.session_lock_path()
    ran: list[Any] = []

    def child(_cmd: Any, **kw: Any) -> int:
        ran.append(kw["env"])
        return 0

    with open(lock, "a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        rc = sl.run_with_session_lock(["true"], wait_s=0.3, busy="BUSY", supervise=child)
    assert rc == 0 and ran == [] and "BUSY" in capsys.readouterr().out
    rc = sl.run_with_session_lock(["true"], wait_s=0.3, busy="BUSY", supervise=child)
    assert rc == 0 and ran and ran[0][sl.SESSION_LOCK_HELD_ENV] == "1"


def test_the_reconcile_stamp_is_written_only_when_the_reconcile_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    ok = mock.Mock(returncode=0, stdout='{"schema": 1, "entries": []}', stderr="")
    bad = mock.Mock(returncode=3, stdout="", stderr="boom")
    sl.run_session_reconcile(argv=["x"], run=lambda *a, **k: bad, on_complete=sl._stamp_session_reconcile)
    assert not sl.session_reconcile_fresh()
    sl.run_session_reconcile(argv=["x"], run=lambda *a, **k: ok, on_complete=sl._stamp_session_reconcile)
    assert sl.session_reconcile_fresh()
    assert not sl.session_reconcile_fresh(60, now=sl.session_stamp_path().stat().st_mtime + 61)


# ─── F1 (beyond the table): a stopped VCO container elsewhere ───────────


def test_vcos_own_stopped_container_on_a_moved_port_is_never_replaced_by_an_empty_copy(tmp_path):
    case = {"phase": "install", "has_gpu": False,
            "containers": [dict(_VCO_W_STOPPED, ports={"8080": 18081, "50051": 60052})]}
    result, db, machine, *_ = run_case(tmp_path, case)
    row = se.load_rows(db)["weaviate"]
    assert (row.mode, row.container_name, row.port) == ("vco_managed", "vco_weaviate", 18081)
    assert result.disposition("weaviate") == "start"


def test_two_stopped_vco_weaviates_are_ambiguous_not_silently_picked(tmp_path):
    other = dict(_VCO_W_STOPPED, name="weaviate_claude", project="vibecoded", working_dir=LEGACY_HOME,
                 ports={"8080": 18081, "50051": 60052})
    legacy = dict(_VCO_W_STOPPED, project="vibecoded", working_dir=LEGACY_HOME)
    live = {"localhost:8080": {"service": "weaviate", "version": "1.30.0", "classes": []}}
    case = {"phase": "install", "has_gpu": False,
            "containers": [legacy, other, dict(_OTHER_W_ON_8081, ports={"8080": 8080, "50051": 50051})],
            "http": live, "busy_ports": [8080, 50051]}
    result, db, *_ = run_case(tmp_path, case)
    assert se.load_rows(db)["weaviate"].container_name == "vco_weaviate"  # the canonical port wins
    assert "service_endpoint_ambiguous" in _cids(result)
    assert "service_adoption_confirmation_required" not in _cids(result)
    entry = next(e for e in result.entries if e.condition_id == "service_endpoint_ambiguous")
    assert "--container <name>" in entry.command_to_apply
