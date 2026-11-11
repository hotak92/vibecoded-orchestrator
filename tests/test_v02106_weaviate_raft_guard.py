# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — Weaviate 1.37's raft-replay orphan drop (field report: the root
KG / Development / Diagrams classes lost their index at the first 1.37 start
and at every restart after; docs/TROUBLESHOOTING.md "Weaviate collections
lose their data at every restart").

Everything here runs against FAKES (a scripted container runtime, a scripted
Weaviate REST) — no real podman, no network. The real-order proof against
disposable 1.28.4 / 1.37.18 containers is recorded in the CHANGELOG entry.

Covers, per work item:
1. the compose ships the snapshot settings, and a running container without
   them is drift (so VCO's recreate applies them);
2. ``upgrade_check`` / ``snapshot_with_container_image`` and their wiring into
   every recreate path (data_identity.guard_recreate → install step 5, the up
   verb, the port move; service_adoption → hand-to-vco);
3. the doctor findings (index missing, replay exposure);
4. the case-variant create guard and the delete records;
5. the uncapped-container finding.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import data_identity as di  # noqa: E402
from vco_lib import doctor  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_lifecycle as sl  # noqa: E402
from vco_lib import weaviate_case_history as wch  # noqa: E402
from vco_lib import weaviate_raft_guard as rg  # noqa: E402

pytestmark = pytest.mark.real_raft_guard

DEST = "/var/lib/weaviate"
OLD_IMAGE = "cr.weaviate.io/semitechnologies/weaviate:1.28.4"
NEW_IMAGE = "cr.weaviate.io/semitechnologies/weaviate:1.37.18"
URL = "http://wv.test:8080"

#: A real 1.37.18 ``/v1/cluster/statistics`` body (disposable container,
#: 2026-11-10), trimmed to the fields that matter.
REAL_STATS = {"statistics": [{"name": "node1", "raft": {
    "appliedIndex": "6", "commitIndex": "6", "lastLogIndex": "6", "lastLogTerm": "2",
    "lastSnapshotIndex": "0", "lastSnapshotTerm": "0", "state": "Leader"}}],
    "synchronized": True}


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def _stats(log: int, snap: int) -> str:
    return json.dumps({"statistics": [{"raft": {
        "lastLogIndex": str(log), "lastSnapshotIndex": str(snap), "appliedIndex": str(log)}}]})


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(rg, "_sleep", lambda _s: None)


# ─── item 1: the compose ────────────────────────────────────────────────


def _compose_weaviate_env() -> dict:
    import yaml

    doc = yaml.safe_load((REPO_ROOT / "infrastructure" / "docker-compose.yml")
                         .read_text(encoding="utf-8"))
    return {k: str(v) for k, v in doc["services"]["weaviate"]["environment"].items()}


def test_the_compose_ships_the_snapshot_settings_the_module_pins():
    env = _compose_weaviate_env()
    for key, value in rg.COMPOSE_SNAPSHOT_ENV.items():
        assert env.get(key) == value, key
    assert int(env["RAFT_SNAPSHOT_THRESHOLD"]) < 8192  # Weaviate's default never fires


def test_a_running_container_without_them_is_drift():
    from vco_lib.service_adoption import WEAVIATE_RECLAIM_ENV_KEYS
    from vco_lib.service_reconcile import compose_reclaim_env

    for key in rg.COMPOSE_SNAPSHOT_ENV:
        assert key in WEAVIATE_RECLAIM_ENV_KEYS
    parsed = compose_reclaim_env((REPO_ROOT / "infrastructure" / "docker-compose.yml")
                                 .read_text(encoding="utf-8"))
    for key, value in rg.COMPOSE_SNAPSHOT_ENV.items():
        assert parsed.get(key) == value


# ─── versions and statistics ────────────────────────────────────────────


@pytest.mark.parametrize("text,want", [
    (NEW_IMAGE, (1, 37, 18)), (OLD_IMAGE, (1, 28, 4)), ("1.28.4", (1, 28, 4)),
    ("semitechnologies/weaviate:1.32", (1, 32, 0)),
    ("cr.weaviate.io/semitechnologies/weaviate:1.37.18@sha256:abc", (1, 37, 18)),
    ("docker.io/semitechnologies/weaviate:latest", None),
    ("localhost:5000/weaviate", None), ("sha256:0123", None), ("", None), (None, None)])
def test_parse_version(text, want):
    assert rg.parse_version(text) == want


def test_orphan_drop_lines():
    assert not rg.has_orphan_drop((1, 28, 4))
    assert not rg.has_orphan_drop((1, 27, 0))
    assert rg.has_orphan_drop((1, 29, 0))  # unread lines count as affected
    assert rg.has_orphan_drop((1, 37, 18))
    assert rg.has_orphan_drop(None)


def test_the_real_statistics_body_parses():
    stats = rg.parse_raft_stats(REAL_STATS)
    assert stats == rg.RaftStats(6, 0, 6) and not stats.covered
    assert rg.RaftStats(6, 6, 6).covered
    assert rg.parse_raft_stats({"statistics": []}) is None


# ─── item 2: the pre-upgrade snapshot ───────────────────────────────────


class Runtime:
    """A scripted podman for ONE Weaviate container (``vco_weaviate``)."""

    def __init__(self, *, image=OLD_IMAGE, running="true", exec_meta=None,
                 run_rc=0, exists=True, memory="0", live_mount=None, one_off_running="true"):
        self.image, self.running, self.exists = image, running, exists
        self.one_off_running = one_off_running
        self.exec_meta = exec_meta
        self.run_rc = run_rc
        self.memory = memory
        self.live_mount = live_mount
        self.argv: list[list[str]] = []
        self.compose_source = ""

    def run(self, argv, **_kw):
        argv = [str(a) for a in argv]
        self.argv.append(argv)
        verb = argv[1]
        if argv[-1] == "config":
            import yaml

            src = self.compose_source or "weaviate_data"
            vol = ({"type": "bind", "source": src, "target": DEST} if src.startswith("/")
                   else {"type": "volume", "source": src, "target": DEST})
            return _cp(argv, 0, yaml.safe_dump({"services": {"weaviate": {"volumes": [vol]}},
                                                "volumes": {"weaviate_data": {}}}))
        if verb == "inspect":
            name = argv[-1]
            if name == "vco_weaviate" + rg.ONE_OFF_SUFFIX and "{{.State.Running}}" in argv:
                return _cp(argv, 0, self.one_off_running + "\n")
            if not self.exists or name != "vco_weaviate":
                return _cp(argv, 125, "", f"Error: no such container {name}")
            if "--format" not in argv:
                return _cp(argv, 0, json.dumps([{"Id": "abc123"}]))
            fmt = argv[argv.index("--format") + 1]
            answers = {
                "{{json .Mounts}}": json.dumps([self.live_mount] if self.live_mount else []),
                "{{.Config.Image}}": self.image,
                "{{.Image}}": "sha256:0ld1mage",
                "{{json .Config.Env}}": json.dumps(["CLUSTER_HOSTNAME=node1",
                                                    "RAFT_SNAPSHOT_THRESHOLD=8192"]),
                "{{.State.Running}}": self.running,
                "{{.HostConfig.Memory}}": self.memory,
            }
            return _cp(argv, 0, answers[fmt] + "\n")
        if verb == "exec":  # only /v1/meta is read over exec
            return (_cp(argv, 0, json.dumps({"version": self.exec_meta}))
                    if self.exec_meta else _cp(argv, 1, "", "not running"))
        if verb == "port":
            return _cp(argv, 0, "127.0.0.1:41234\n") if self.run_rc == 0 else _cp(argv, 1)
        if verb == "run":
            return _cp(argv, self.run_rc, "id\n" if self.run_rc == 0 else "",
                       "" if self.run_rc == 0 else "Error: image not known")
        if verb == "logs":
            return _cp(argv, 0, "", "panic: could not open raft.db\n")
        if verb in ("stop", "start", "rm"):
            return _cp(argv)
        raise AssertionError(f"unexpected argv {argv}")

    def verbs(self) -> list[str]:
        return [f"{a[1]} {a[-1]}" for a in self.argv
                if a[1] in ("stop", "start", "rm", "run")]


class OneOff:
    """The one-off container's REST on its loopback port (``_method_http``).
    *stats* are the successive ``(lastLogIndex, lastSnapshotIndex)`` answers;
    ``None`` = not answering yet; the last one repeats."""

    def __init__(self, *stats, marker_status=200):
        self.stats = list(stats)
        self.marker_status = marker_status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, body=None, timeout=15.0):
        assert url.startswith("http://127.0.0.1:41234"), url
        path = url[len("http://127.0.0.1:41234"):]
        self.calls.append((method, path))
        if method == "GET":
            item = self.stats.pop(0) if len(self.stats) > 1 else self.stats[0]
            if item is None:
                raise OSError("connection refused")
            return 200, _stats(*item).encode()
        if method == "POST":
            assert body == {"class": rg.MARKER_CLASS, "vectorizer": "none"}
            return self.marker_status, b"{}"
        assert (method, path) == ("DELETE", f"/v1/schema/{rg.MARKER_CLASS}")
        return 200, b""


@pytest.fixture()
def one_off(monkeypatch):
    def install(*stats, **kw):
        fake = OneOff(*stats, **kw)
        monkeypatch.setattr(rg, "_method_http", fake)
        return fake
    return install


def _check(box: Runtime, **kw) -> rg.UpgradeVerdict:
    kw.setdefault("service", "weaviate")
    kw.setdefault("live_state", "present")
    kw.setdefault("ref", "vco_weaviate")
    kw.setdefault("target_image", NEW_IMAGE)
    kw.setdefault("data_mount", None)
    return rg.upgrade_check(runtime="podman", run=box.run, **kw)


def test_moving_old_data_takes_a_snapshot_with_the_old_image_then_restores(one_off):
    rest = one_off(None, (7, 0), (9, 0), (9, 9))
    box = Runtime()
    v = _check(box)
    assert v.status == rg.PREPARED and v.ok, v.reason
    # the marker create + delete come right after the first answer
    assert [c for c in rest.calls if c[0] != "GET"] == [
        ("POST", "/v1/schema"), ("DELETE", f"/v1/schema/{rg.MARKER_CLASS}")]
    one_off = "vco_weaviate" + rg.ONE_OFF_SUFFIX
    assert box.verbs() == [f"rm {one_off}", "stop vco_weaviate", "run sha256:0ld1mage",
                           f"stop {one_off}", f"rm {one_off}", "start vco_weaviate"]
    run_argv = next(a for a in box.argv if a[1] == "run")
    assert run_argv[run_argv.index("--volumes-from") + 1] == "vco_weaviate"
    assert "RAFT_SNAPSHOT_THRESHOLD=1" in run_argv and "RAFT_SNAPSHOT_INTERVAL=1" in run_argv
    assert "RAFT_SNAPSHOT_THRESHOLD=8192" not in run_argv  # the old value is replaced
    assert "CLUSTER_HOSTNAME=node1" in run_argv            # same raft node identity
    assert "--memory" not in run_argv                      # no limit on the old one → none here
    assert run_argv[run_argv.index("-p") + 1] == "127.0.0.1::8080"  # loopback only


def test_the_target_is_the_log_index_after_the_marker_writes():
    # the start's no-op + configuration entries (6, 7) cannot be snapshotted
    # alone; the marker commands (8, 9) make it possible — 8 is not enough
    one_off_rest = OneOff((7, 0), (9, 0), (9, 8), (9, 9))
    with mock.patch.object(rg, "_method_http", one_off_rest):
        assert _check(Runtime()).status == rg.PREPARED
    assert [c[0] for c in one_off_rest.calls].count("GET") == 4


def test_a_refused_marker_refuses_and_restores(one_off):
    one_off((7, 0), marker_status=500)
    box = Runtime()
    v = _check(box)
    assert v.status == rg.REFUSED and rg.MARKER_CLASS in v.reason
    assert box.verbs()[-1] == "start vco_weaviate"


def test_the_old_containers_memory_limit_carries_to_the_one_off(one_off):
    one_off((3, 3))
    box = Runtime(memory="4294967296")
    _check(box)
    run_argv = next(a for a in box.argv if a[1] == "run")
    assert run_argv[run_argv.index("--memory") + 1] == "4294967296"


def test_a_stopped_old_container_is_not_started_afterwards(one_off):
    one_off((3, 3))
    box = Runtime(running="false")
    assert _check(box).status == rg.PREPARED
    assert not any(a[1] in ("stop", "start") and a[-1] == "vco_weaviate" for a in box.argv)


def test_no_snapshot_in_time_refuses_and_restores_the_old_container(monkeypatch, one_off):
    clock = iter([0.0, 10.0, 99999.0])
    monkeypatch.setattr(rg, "_monotonic", lambda: next(clock))
    one_off((6, 0))
    box = Runtime()
    v = _check(box)
    assert v.status == rg.REFUSED and "no raft snapshot" in v.reason
    assert box.verbs()[-1] == "start vco_weaviate"


def test_a_one_off_that_dies_refuses_at_once_with_its_last_log_line(one_off):
    one_off(None)
    box = Runtime(one_off_running="false")
    v = _check(box)
    assert v.status == rg.REFUSED and "could not open raft.db" in v.reason
    assert box.verbs()[-1] == "start vco_weaviate"


def test_a_one_off_that_cannot_start_refuses_and_restores():
    box = Runtime(run_rc=125)
    v = _check(box)
    assert v.status == rg.REFUSED and "could not start" in v.reason
    assert box.verbs()[-1] == "start vco_weaviate"


def test_an_unknown_running_version_refuses_without_touching_anything():
    box = Runtime(image="docker.io/semitechnologies/weaviate:latest")
    v = _check(box)
    assert v.status == rg.REFUSED and "not known" in v.reason
    assert box.verbs() == []


def test_the_user_can_confirm_an_unknown_version_but_not_override_a_known_move(one_off):
    box = Runtime(image="docker.io/semitechnologies/weaviate:latest")
    assert _check(box, environ={rg.CONFIRM_ENV: "1"}).status == rg.NOT_NEEDED
    one_off((3, 3))
    known = Runtime()  # 1.28.4: VCO takes the snapshot itself, confirmation or not
    assert _check(known, environ={rg.CONFIRM_ENV: "1"}).status == rg.PREPARED


def test_a_running_version_from_v1_meta_is_used_when_the_tag_has_none():
    box = Runtime(image="docker.io/semitechnologies/weaviate:latest", exec_meta="1.37.18")
    assert _check(box).status == rg.NOT_NEEDED and box.verbs() == []


@pytest.mark.parametrize("kw", [
    {"service": "ollama"},
    {"target_image": "cr.weaviate.io/semitechnologies/weaviate:1.28.4"},
])
def test_nothing_to_do_when_no_orphan_drop_is_in_reach(kw):
    box = Runtime()
    assert _check(box, **kw).status == rg.NOT_NEEDED and box.argv == []


def test_data_already_on_a_release_with_the_orphan_drop_is_a_restart_not_a_move():
    box = Runtime(image=NEW_IMAGE)
    assert _check(box).status == rg.NOT_NEEDED and box.verbs() == []


def test_no_container_and_no_raft_log_is_a_fresh_install(tmp_path):
    box = Runtime(exists=False)
    mount = {"kind": "bind", "source": str(tmp_path / "data"), "destination": DEST}
    assert _check(box, live_state="absent", ref="", data_mount=mount).status == rg.NOT_NEEDED


def test_no_container_but_a_raft_log_refuses_unless_the_user_confirms(tmp_path):
    (tmp_path / "data" / "raft").mkdir(parents=True)
    (tmp_path / "data" / "raft" / "raft.db").write_bytes(b"x")
    mount = {"kind": "bind", "source": str(tmp_path / "data"), "destination": DEST}
    box = Runtime(exists=False)
    v = _check(box, live_state="absent", ref="", data_mount=mount, environ={})
    assert v.status == rg.REFUSED and "holds a Weaviate raft log" in v.reason
    ok = _check(box, live_state="absent", ref="", data_mount=mount,
                environ={rg.CONFIRM_ENV: "1"})
    assert ok.status == rg.NOT_NEEDED


def test_a_missing_named_volume_is_fresh_and_an_unreadable_one_refuses():
    class VolBox(Runtime):
        def __init__(self, rc, err, out=""):
            super().__init__(exists=False)
            self.rc, self.err, self.out = rc, err, out

        def run(self, argv, **kw):
            if argv[1] == "volume":
                self.argv.append(list(argv))
                return _cp(argv, self.rc, self.out, self.err)
            return super().run(argv, **kw)

    mount = {"kind": "volume", "source": "infrastructure_weaviate_data", "destination": DEST}
    fresh = _check(VolBox(125, "Error: no such volume x"), live_state="absent", ref="",
                   data_mount=mount, environ={})
    assert fresh.status == rg.NOT_NEEDED
    unknown = _check(VolBox(125, "permission denied"), live_state="absent", ref="",
                     data_mount=mount, environ={})
    assert unknown.status == rg.REFUSED
    # Docker Desktop: the volume exists, its mountpoint lives in the VM
    hidden = _check(VolBox(0, "", "/var/lib/docker/volumes/nowhere-on-this-host/_data\n"),
                    live_state="absent", ref="", data_mount=mount, environ={})
    assert hidden.status == rg.REFUSED


# ─── item 2 wiring: data_identity.guard_recreate (install step 5, up, move)


@pytest.fixture()
def wv_world(tmp_path):
    root = tmp_path / "orch"
    infra = root / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "docker-compose.yml").write_text(
        f"services:\n  weaviate:\n    image: {NEW_IMAGE}\n", encoding="utf-8")
    data = tmp_path / "wvdata"
    (data / "raft").mkdir(parents=True)
    (data / "raft" / "raft.db").write_bytes(b"log")
    db = make_launcher_db(tmp_path / "launcher.db")
    bind = {"kind": "bind", "source": str(data), "destination": DEST}
    se.write_rows([se.EndpointRow(service="weaviate", mode="vco_managed", port=8081,
                                  grpc_port=50052, container_name="vco_weaviate",
                                  source="install_probe", data_mount=bind)], db_path=db)
    live = {"Type": "bind", "Source": str(data), "Destination": DEST}
    return infra, db, data, live


def _guard(box: Runtime, infra: Path, db: Path):
    rows = se.load_rows(db)
    return di.guard_recreate("weaviate", runtime="podman", infra_dir=infra,
                             compose_argv=["podman", "compose"], row=rows["weaviate"],
                             rows=rows, db_path=db, run=box.run)


def test_guard_recreate_takes_the_snapshot_before_a_move(wv_world, one_off):
    infra, db, data, live = wv_world
    one_off((4, 4))
    box = Runtime(live_mount=live)
    box.compose_source = str(data)
    g = _guard(box, infra, db)
    assert g.verdict == di.PRESERVES, g.reason
    assert "stop vco_weaviate" in box.verbs() and box.verbs()[-1] == "start vco_weaviate"


def test_guard_recreate_refuses_a_move_it_cannot_snapshot(wv_world):
    infra, db, data, live = wv_world
    box = Runtime(live_mount=live, image="docker.io/semitechnologies/weaviate:latest")
    box.compose_source = str(data)
    g = _guard(box, infra, db)
    assert g.verdict == di.REFUSE_RAFT and not g.ok
    entry = g.deferral_entry()
    assert entry.condition_id == rg.CID_UPGRADE_REFUSED
    assert "snapshot --container vco_weaviate --runtime podman" in entry.command_to_apply
    assert entry.dismiss_fields == {"service": "weaviate", "container": "vco_weaviate"}


def test_guard_compose_set_drops_the_refused_weaviate_and_ledgers_it(wv_world):
    infra, db, data, live = wv_world
    box = Runtime(live_mount=live, image="docker.io/semitechnologies/weaviate:latest")
    box.compose_source = str(data)

    class Ledger:
        def __init__(self):
            self.entries = []

        def add_entry(self, e):
            self.entries.append(e)

    ledger = Ledger()
    refused = di.guard_compose_set(["weaviate"], runtime="podman", infra_dir=infra,
                                   compose_argv=lambda: ["podman", "compose"],
                                   rows=se.load_rows(db), deferral_report=ledger,
                                   run=box.run, out=lambda _l: None, db_path=db)
    assert refused == {"weaviate"}
    assert [e.condition_id for e in ledger.entries] == [rg.CID_UPGRADE_REFUSED]


def test_the_up_verb_never_removes_a_zombie_whose_move_was_refused(wv_world):
    infra, db, data, live = wv_world
    box = Runtime(live_mount=live, running="false",
                  image="docker.io/semitechnologies/weaviate:latest")
    box.compose_source = str(data)
    res = sl.up_services(["weaviate"], compose_dir=infra, compose_argv=["podman", "compose"],
                         runtime="podman", recreate=["weaviate"], db_path=db, run=box.run,
                         out=lambda _l: None)
    assert res.refused == ["weaviate"] and res.removed == []
    assert [e.condition_id for e in res.entries] == [rg.CID_UPGRADE_REFUSED]
    assert not any(a[1] == "rm" and a[-1] == "vco_weaviate" for a in box.argv)


def test_no_container_on_a_folder_with_a_raft_log_refuses_the_create(wv_world, monkeypatch):
    infra, db, data, _live = wv_world
    monkeypatch.delenv(rg.CONFIRM_ENV, raising=False)
    se.write_rows([se.EndpointRow(service="weaviate", mode="vco_managed", port=8081,
                                  grpc_port=50052, container_name="vco_weaviate",
                                  source="install_probe", data_mount=None)], db_path=db)
    box = Runtime(exists=False)
    box.compose_source = str(data)
    g = _guard(box, infra, db)
    assert g.verdict == di.REFUSE_RAFT, g.reason


def test_service_adoption_refuses_weaviate_and_still_adopts_the_rest(monkeypatch, tmp_path):
    from tests import test_v0296_service_adoption as t296

    seen = []

    def refuse(**kw):
        seen.append(kw)
        if kw["service"] == "weaviate":
            return rg.UpgradeVerdict(rg.REFUSED, "test refusal")
        return rg.UpgradeVerdict(rg.NOT_NEEDED)

    monkeypatch.setattr(rg, "upgrade_check", refuse)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    world = t296.World(tmp_path)
    result, _lines = world.adopt()
    assert result.refused.get("weaviate") == "test refusal"
    assert result.adopted == ["ollama", "code_embed"]
    assert [e.condition_id for e in result.entries] == [rg.CID_UPGRADE_REFUSED]
    assert seen and seen[0]["ref"] and seen[0]["live_state"] == "present"
    assert seen[0]["target_image"] == "cr.weaviate.io/semitechnologies/weaviate:1.27.0"
    weaviate_ref = seen[0]["ref"]
    assert not any(a[1:3] == ["stop", weaviate_ref] or a[1:3] == ["rm", weaviate_ref]
                   for a in result.argv_log)


def test_prepare_for_compose_reads_the_live_container_and_the_pin(wv_world, one_off):
    infra, _db, _data, live = wv_world
    one_off((5, 5))
    box = Runtime(live_mount=live)
    v = rg.prepare_for_compose(infra.parent, runtime="podman", run=box.run)
    assert v.status == rg.PREPARED  # 1.28.4 live, 1.37.18 pinned → snapshot taken
    refused = rg.prepare_for_compose(
        infra.parent, runtime="podman",
        run=Runtime(live_mount=live, image="docker.io/semitechnologies/weaviate:latest").run)
    assert refused.status == rg.REFUSED


def test_the_prepare_cli_reply_is_the_shape_the_launcher_parses(monkeypatch, capsys):
    monkeypatch.setattr(rg, "prepare_for_compose",
                        lambda *a, **k: rg.UpgradeVerdict(rg.REFUSED, "version not known"))
    assert rg.main(["prepare", "--root", "/nowhere", "--json"]) == 1
    reply = json.loads(capsys.readouterr().out)
    assert reply == {"ok": False, "status": "refused", "message": "version not known",
                     "error": rg.CID_UPGRADE_REFUSED}
    monkeypatch.setattr(rg, "prepare_for_compose",
                        lambda *a, **k: rg.UpgradeVerdict(rg.NOT_NEEDED, ""))
    assert rg.main(["prepare", "--root", "/nowhere", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ─── item 3: detection ──────────────────────────────────────────────────


class FakeWeaviate:
    """A scripted Weaviate REST (``http(url, timeout)``)."""

    def __init__(self, *, classes=(), broken=(), version="1.37.18", log=6, snap=0):
        self.classes, self.broken = list(classes), set(broken)
        self.version, self.log, self.snap = version, log, snap
        self.hits: list[str] = []

    def __call__(self, url, timeout):
        self.hits.append(url)
        path = url.split("wv.test:8080", 1)[1]
        if path == "/v1/schema":
            return 200, json.dumps({"classes": [{"class": c} for c in self.classes]}).encode()
        if path == "/v1/meta":
            return 200, json.dumps({"version": self.version}).encode()
        if path == "/v1/cluster/statistics":
            return 200, _stats(self.log, self.snap).encode()
        m = re.fullmatch(r"/v1/schema/([^/]+)/shards", path)
        if m:
            if m.group(1) in self.broken:
                return 404, (b'{"error":[{"message":"cannot get shards status for a '
                             b'non-existing index for X: not found"}]}')
            return 200, b'[{"name":"s1","status":"READY"}]'
        raise AssertionError(path)


def test_scan_finds_the_broken_class_and_the_case_pair(tmp_path):
    root = tmp_path / "vibecoded-orchestrator"  # derives VibecodedOrchestrator_*
    fake = FakeWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph", "Other_Development"],
                        broken=["VibeCodedOrchestrator_KnowledgeGraph"])
    result = rg.scan(URL, root=root, http=fake)
    assert result is not None
    assert result.index_missing == ["VibeCodedOrchestrator_KnowledgeGraph"]
    assert ("VibecodedOrchestrator_KnowledgeGraph",
            "VibeCodedOrchestrator_KnowledgeGraph") in result.case_pairs
    assert result.exposed  # snapshot 0 < log 6
    # only a case-pair class's shards are read (a read can load a lazy shard)
    assert not any("Other_Development/shards" in h for h in fake.hits)


def test_a_snapshot_past_the_first_reading_ends_the_exposure_of_a_derived_name(
        tmp_path, monkeypatch):
    """Review A-L1: a derived name's delete index is unknown, so the first
    reading bounds it by the log index it sees. A snapshot that PREDATES that
    reading (snap 9 < log 11) is not proof the delete is held — exposed. Once
    a snapshot reaches the bound, a later restart's no-op + configuration
    entries (log runs ahead again) are not exposure."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "vibecoded-orchestrator"
    fake = FakeWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"], log=11, snap=9)
    result = rg.scan(URL, root=root, http=fake)
    assert result is not None and result.case_pairs and result.exposed
    fake.snap, fake.log = 11, 13  # a snapshot covered the bound; then a restart
    result = rg.scan(URL, root=root, http=fake)
    assert result is not None and result.case_pairs and not result.exposed


class SnapshottingWeaviate(FakeWeaviate):
    """A Weaviate with the shipped snapshot settings: its next snapshot
    covers the whole log after *catch_up* statistics reads (measured on
    1.37.18: an idle restart's own entries are snapshotted within a minute)."""

    def __init__(self, *, catch_up: int, **kw):
        super().__init__(**kw)
        self.catch_up, self.stat_reads = catch_up, 0

    def __call__(self, url, timeout):
        if url.endswith("/v1/cluster/statistics"):
            self.stat_reads += 1
            if self.stat_reads > self.catch_up:
                self.snap = self.log
        return super().__call__(url, timeout)


def test_a_first_reading_right_after_a_restart_is_not_exposure(tmp_path, monkeypatch):
    """Review H1: a default-clone root derives ``VibecodedOrchestrator_*``
    beside the live ``VibeCodedOrchestrator_*`` — a pair with no real delete.
    Its first reading, taken seconds after a restart (the update's doctor
    phase: the snapshot covers every schema command, the start's own two
    entries are not in it yet), must not report the start as a data-loss
    risk; neither may any later idle restart."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "vibecoded-orchestrator"
    fake = SnapshottingWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"],
                                log=42, snap=40, catch_up=3)
    result = rg.scan(URL, root=root, http=fake, settle_s=rg.SETTLE_S)
    assert result is not None and result.case_pairs
    assert result.recorded_index == {"VibecodedOrchestrator_KnowledgeGraph": 42}
    assert not result.exposed
    # an idle restart later: two more entries, not yet snapshotted
    fake.log, fake.stat_reads = 44, 0
    assert not rg.scan(URL, root=root, http=fake, check_indexes=False).exposed


def test_the_doctor_reading_waits_for_the_snapshot(tmp_path, monkeypatch):
    """The doctor's own scan (``default_scan``) carries the settle window."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    fake = SnapshottingWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"],
                                log=42, snap=40, catch_up=2)
    monkeypatch.setattr(rg, "_default_http", fake)
    monkeypatch.setattr(rg, "_weaviate_url", lambda: URL)
    result = rg.default_scan(tmp_path / "vibecoded-orchestrator")
    assert result is not None and result.case_pairs and not result.exposed


def test_a_log_no_snapshot_ever_covers_is_still_exposed(tmp_path, monkeypatch):
    """The field case: no snapshot settings, no snapshot at all. The settle
    window passes (a bounded number of reads) and the exposure stands."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    fake = SnapshottingWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"],
                                log=7, snap=0, catch_up=10**6)
    result = rg.scan(URL, root=tmp_path / "vibecoded-orchestrator", http=fake,
                     settle_s=rg.SETTLE_S)
    assert result is not None and result.exposed
    assert fake.stat_reads == 1 + int(rg.SETTLE_S // rg.SETTLE_POLL_S)
    # ... and a snapshot that holds the bound (the remedy: the shipped
    # settings) clears it on the next reading.
    fake.snap = fake.log
    assert not rg.scan(URL, root=tmp_path / "vibecoded-orchestrator", http=fake,
                       check_indexes=False).exposed


def test_a_release_without_the_orphan_drop_is_not_kept_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    fake = SnapshottingWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"],
                                version="1.28.4", log=7, snap=0, catch_up=10**6)
    rg.scan(URL, root=tmp_path / "vibecoded-orchestrator", http=fake, settle_s=rg.SETTLE_S)
    assert fake.stat_reads == 1


def test_a_marker_whose_delete_failed_is_named_in_the_refusal(monkeypatch):
    """Review A-L2: a created marker whose DELETE failed is named, with the
    note that the next attempt removes it; success and a refused create keep
    their own answers."""
    answers = {"POST": 200, "DELETE": 500}
    monkeypatch.setattr(rg, "_method_http",
                        lambda method, url, body=None, **k: (answers[method], b""))
    reason = rg._write_marker("http://127.0.0.1:1")
    assert reason is not None and "may remain in the schema" in reason
    assert rg.MARKER_CLASS in reason and "HTTP 500" in reason
    answers["DELETE"] = 200
    assert rg._write_marker("http://127.0.0.1:1") is None
    answers["POST"] = 503
    reason = rg._write_marker("http://127.0.0.1:1")
    assert reason is not None and "did not accept" in reason
    assert "may remain" not in reason


def test_a_bound_above_a_restarted_log_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    assert wch.delete_index_bounds(URL, ["A_Kg"], 500) == {"A_Kg": 500}
    assert wch.delete_index_bounds(URL, ["A_Kg"], 900) == {"A_Kg": 500}
    assert wch.delete_index_bounds(URL, ["A_Kg"], 4) == {"A_Kg": 4}  # new data folder


def test_a_recorded_delete_after_the_snapshot_is_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    fake = FakeWeaviate(classes=["Proj_Code"], log=12, snap=9)
    wch.record_deleted("PROJ_Code", URL, http=fake)  # recorded at index 12
    assert rg.scan(URL, http=fake, check_indexes=False).exposed
    fake.snap = 12
    assert not rg.scan(URL, http=fake, check_indexes=False).exposed


def test_the_deleted_class_ledger_is_evidence_too(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    fake = FakeWeaviate(classes=["Proj_Code"])
    wch.record_deleted("PROJ_Code", URL, http=fake)
    result = rg.scan(URL, root=None, http=fake, check_indexes=False)
    assert result is not None and result.case_pairs == [("PROJ_Code", "Proj_Code")]


def _root(tmp_path) -> Path:
    root = tmp_path / "vibecoded-orchestrator"
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    return root


def test_doctor_reports_both_findings_and_their_entries(tmp_path):
    root = _root(tmp_path)
    fake = FakeWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"],
                        broken=["VibeCodedOrchestrator_KnowledgeGraph"])
    res = doctor.DoctorResolvers(weaviate_raft_scan=lambda f: rg.scan(URL, root=f, http=fake))
    findings = doctor.probe_weaviate_raft(root, res, {})
    by_cid = {f.condition_id: f for f in findings}
    missing = by_cid[rg.CID_INDEX_MISSING]
    assert missing.is_problem and "bootstrap-collections" in missing.command
    order = missing.command
    assert order.index("status") < order.index("curl -X DELETE") < order.index("kg-sync")
    exposed = by_cid[rg.CID_REPLAY_EXPOSED]
    assert exposed.is_problem
    report = doctor.DoctorReport(folder=root, scope="full", findings=findings)
    entries = {e.condition_id: e for e in doctor.deferral_entries_for(report)}
    assert entries[rg.CID_INDEX_MISSING].dismiss_fields == {
        "classes": ["VibeCodedOrchestrator_KnowledgeGraph"]}
    assert entries[rg.CID_REPLAY_EXPOSED].dismiss_fields["pairs"]


def test_doctor_clean_reading_carries_both_cids_to_clear(tmp_path):
    root = _root(tmp_path)
    fake = FakeWeaviate(classes=["VibeCodedOrchestrator_KnowledgeGraph"], log=3, snap=3)
    res = doctor.DoctorResolvers(weaviate_raft_scan=lambda f: rg.scan(URL, root=f, http=fake))
    findings = doctor.probe_weaviate_raft(root, res, {})
    assert all(f.status == doctor.STATUS_OK for f in findings)
    report = doctor.DoctorReport(folder=root, scope="full", findings=findings)
    assert {rg.CID_INDEX_MISSING, rg.CID_REPLAY_EXPOSED} <= set(
        doctor.healthy_condition_ids(report))


def test_doctor_unreadable_weaviate_is_unknown_and_user_projects_get_nothing(tmp_path):
    res = doctor.DoctorResolvers(weaviate_raft_scan=lambda f: None)
    [f] = doctor.probe_weaviate_raft(_root(tmp_path), res, {})
    assert f.status == doctor.STATUS_UNKNOWN and not f.condition_id
    assert doctor.probe_weaviate_raft(tmp_path / "user-project", res, {}) == []


def test_the_probes_are_registered_full_scope():
    assert doctor.PROBES["weaviate_raft"][1] == (doctor.SCOPE_FULL,)
    assert doctor.PROBES["weaviate_mem_cap"][1] == (doctor.SCOPE_FULL,)
    for cid in (rg.CID_INDEX_MISSING, rg.CID_REPLAY_EXPOSED, rg.CID_UNCAPPED):
        assert cid in doctor.DOCTOR_OWNED_CIDS and cid in doctor.DOCTOR_SELF_RESOLVING_CIDS


# ─── item 4: the case-variant create guard ──────────────────────────────


@pytest.fixture()
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def test_record_then_guard_refuses_an_uncovered_case_variant(ledger_dir):
    fake = FakeWeaviate(log=7, snap=0)
    wch.record_deleted("VibecodedOrchestrator_KnowledgeGraph", URL, http=fake)
    stored = json.loads(wch.ledger_path().read_text(encoding="utf-8"))
    assert stored["instances"]["http://wv.test:8080"][
        "VibecodedOrchestrator_KnowledgeGraph"]["log_index"] == 7
    clock = iter([0.0, 1.0, 500.0])
    with pytest.raises(wch.CaseVariantCreateRefused) as err:
        wch.guard_create("VibeCodedOrchestrator_KnowledgeGraph", URL, http=fake,
                         sleep=lambda _s: None, monotonic=lambda: next(clock))
    assert "VibecodedOrchestrator_KnowledgeGraph" in str(err.value)


def test_guard_waits_for_the_snapshot_then_forgets_the_delete(ledger_dir):
    fake = FakeWeaviate(log=7, snap=0)
    wch.record_deleted("VibecodedOrchestrator_KnowledgeGraph", URL, http=fake)

    def snapshot_arrives(_s):
        fake.snap = 7

    wch.guard_create("VibeCodedOrchestrator_KnowledgeGraph", URL, http=fake,
                     sleep=snapshot_arrives, monotonic=lambda: 0.0)
    assert wch.deleted_names(URL) == []


def test_guard_ignores_the_same_spelling_and_releases_without_the_orphan_drop(ledger_dir):
    fake = FakeWeaviate(log=7, snap=0, version="1.28.4")
    wch.record_deleted("Proj_KnowledgeGraph", URL, http=fake)
    wch.guard_create("Proj_KnowledgeGraph", URL, http=fake)       # same name: revived
    wch.guard_create("PROJ_KnowledgeGraph", URL, http=fake)       # 1.28: no orphan drop
    assert wch.deleted_names(URL) == ["Proj_KnowledgeGraph"]      # kept for the move


def test_project_init_delete_records_and_create_refuses(ledger_dir, monkeypatch):
    from vco_lib import project_init as pi

    fake = FakeWeaviate(log=4, snap=0)
    monkeypatch.setattr(pi, "_http_request", lambda *a, **k: (200, b"{}"))
    monkeypatch.setattr(wch._rg, "_default_http", fake)
    pi._delete_class("VibecodedOrchestrator_Development", weaviate_url=URL)
    assert wch.deleted_names(URL) == ["VibecodedOrchestrator_Development"]
    monkeypatch.setattr(pi, "_fetch_schema", lambda *a, **k: None)
    monkeypatch.setattr(wch, "DEFAULT_WAIT_S", 0.0)
    posted = []
    monkeypatch.setattr(pi, "_http_request", lambda *a, **k: posted.append(a) or (200, b"{}"))
    with pytest.raises(wch.CaseVariantCreateRefused):
        pi._create_class({"class": "VibeCodedOrchestrator_Development"}, weaviate_url=URL)
    assert posted == []  # nothing reached POST /v1/schema


@pytest.mark.parametrize("snap,created", [(0, False), (9, True)])
def test_the_empty_case_sibling_regen_creates_only_once_the_delete_is_snapshotted(
        ledger_dir, monkeypatch, snap, created):
    """The bootstrap's case-conflict regen (an EMPTY sibling dropped, the
    target created in the other spelling) is the delete + case-variant create
    the 1.37 replay turns into a wipe. On 1.37 the create waits for a snapshot
    that holds the delete; without one it is refused and nothing is POSTed."""
    from vco_lib import project_init as pi

    fake = FakeWeaviate(log=9, snap=snap)
    monkeypatch.setattr(wch._rg, "_default_http", fake)
    monkeypatch.setattr(wch, "DEFAULT_WAIT_S", 0.0)
    monkeypatch.setattr(pi, "_snapshot_collection_for_rebuild",
                        lambda *a, **k: {"object_count": 0, "sample_uuids": []})
    posted: list = []

    def http(method, url, body=None, timeout=30.0):
        if method == "POST":
            posted.append(body["class"])
        return (404, b"{}") if method == "GET" else (200, b"{}")

    monkeypatch.setattr(pi, "_http_request", http)
    definition = {"class": "VibeCodedOrchestrator_Development"}
    if created:
        pi._drop_and_recreate("VibecodedOrchestrator_Development", definition,
                              weaviate_url=URL, log_event=None, reason="case")
        assert posted == ["VibeCodedOrchestrator_Development"]
        assert wch.deleted_names(URL) == []  # covered → forgotten
    else:
        with pytest.raises(wch.CaseVariantCreateRefused):
            pi._drop_and_recreate("VibecodedOrchestrator_Development", definition,
                                  weaviate_url=URL, log_event=None, reason="case")
        assert posted == []


def test_guarded_collection_create_refuses_and_record_collection_delete_records(
        ledger_dir, monkeypatch):
    from vco_lib import fixture_class_guard as fcg
    from vco_lib import weaviate_helpers as wh

    fake = FakeWeaviate(log=4, snap=0)
    monkeypatch.setattr(wch._rg, "_default_http", fake)
    monkeypatch.setattr(wh, "weaviate_url_default", lambda: URL)
    client = mock.MagicMock()
    fcg.record_collection_delete(client, "Proj_CodeFunction")
    client.collections.delete.assert_called_once_with("Proj_CodeFunction")
    assert wch.deleted_names(URL) == ["Proj_CodeFunction"]
    monkeypatch.setattr(wch, "DEFAULT_WAIT_S", 0.0)
    with pytest.raises(wch.CaseVariantCreateRefused):
        fcg.guarded_collection_create(client, "PROJ_CodeFunction")
    client.collections.create.assert_not_called()


def test_the_ledger_is_keyed_by_the_clients_own_url(ledger_dir, monkeypatch):
    """Review A-M2: a client connected to a URL other than the process
    default records its delete under ITS URL, and the create guard on that
    client reads it — the default URL's bucket is never involved."""
    from vco_lib import fixture_class_guard as fcg
    from vco_lib import weaviate_helpers as wh

    fake = FakeWeaviate(log=4, snap=0)
    monkeypatch.setattr(wch._rg, "_default_http", fake)
    monkeypatch.setattr(wh, "weaviate_url_default", lambda: "http://elsewhere.test:9999")
    client = mock.MagicMock()
    client._connection.url = URL
    fcg.record_collection_delete(client, "Proj_CodeFunction")
    assert wch.deleted_names(URL) == ["Proj_CodeFunction"]
    assert wch.deleted_names("http://elsewhere.test:9999") == []
    monkeypatch.setattr(wch, "DEFAULT_WAIT_S", 0.0)
    with pytest.raises(wch.CaseVariantCreateRefused):
        fcg.guarded_collection_create(client, "PROJ_CodeFunction")
    client.collections.create.assert_not_called()


def test_loopback_spellings_share_one_ledger_bucket(ledger_dir, monkeypatch):
    """Review A-L5: localhost / 127.0.0.1 / a trailing slash / letter case are
    one instance; a different port is another."""
    monkeypatch.setattr(wch._rg, "_default_http", FakeWeaviate(log=4, snap=0))
    wch.record_deleted("Proj_Kg", "http://localhost:8081/")
    assert wch.deleted_names("http://127.0.0.1:8081") == ["Proj_Kg"]
    assert wch.deleted_names("HTTP://LOCALHOST:8081") == ["Proj_Kg"]
    assert wch.deleted_names("http://[::1]:8081") == ["Proj_Kg"]
    assert wch.deleted_names("http://127.0.0.1:8082") == []


def test_a_ledger_written_under_the_old_key_form_is_still_read(ledger_dir):
    path = wch.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "instances": {
        "http://localhost:8081": {"Proj_Kg": {"deleted_at": "x", "log_index": 3}}}}),
        encoding="utf-8")
    assert wch.deleted_entries("http://127.0.0.1:8081") == {"Proj_Kg": 3}


def _run_residual_detector(monkeypatch, classes, count=5):
    import install

    schema = {"classes": [{"class": c} for c in classes]}

    class Resp:
        def read(self):
            return json.dumps(schema).encode()

    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: Resp())
    from vco_lib import project_init as pi

    monkeypatch.setattr(pi, "_http_count_objects", lambda *a, **k: count)
    report = mock.MagicMock()
    install._emit_lowercase_codegraph_cleanup_deferrals(report)
    return [c.args[0] for c in report.add_entry.call_args_list]


def test_the_lowercase_residual_entry_fires_and_prints_no_delete(monkeypatch):
    """Review A-M1: the real catalog suffixes reach the entry builder — a
    lower-cased residual (Weaviate capitalises the first letter) beside its
    minted sibling is reported, and the entry never prints a DELETE."""
    entries = _run_residual_detector(
        monkeypatch, ["MyProject_CodeFunction", "Myproject_codefunction",
                      "Myproject_CodeClass", "MyProject_CodeClass"])
    prefix = "lowercase_codegraph_residual_"
    by_name = {e.condition_id[len(prefix):]: e for e in entries
               if e.condition_id.startswith(prefix)}
    assert set(by_name) == {"Myproject_codefunction", "Myproject_CodeClass"}
    entry = by_name["Myproject_codefunction"]
    assert "`MyProject_CodeFunction`" in entry.detected
    assert "DELETE" not in entry.command_to_apply
    assert "Do NOT drop it" in entry.why_deferred


@pytest.mark.parametrize("classes", [
    ["MyProject_CodeFunction"],                              # no sibling
    ["MyProject_CodeFunction", "MYproject_CodeFunction"],    # neither is the other lowered
    ["Myproject_kg", "MyProject_Kg"],                         # not a code-graph class
    ["Foo_CodeFunction", "Bar_codefunction"],                 # different lower key
])
def test_the_lowercase_residual_detector_stays_quiet(monkeypatch, classes):
    assert _run_residual_detector(monkeypatch, classes) == []


def test_a_residual_with_real_data_is_not_reported(monkeypatch):
    assert _run_residual_detector(
        monkeypatch, ["MyProject_CodeFunction", "Myproject_codefunction"],
        count=500) == []


def test_lowercased_form_is_one_way():
    assert wch.is_lowercased_form("Myproject_codefunction", "MyProject_CodeFunction")
    assert wch.is_lowercased_form("myproject_CodeFunction", "MyProject_CodeFunction")
    assert not wch.is_lowercased_form("MyProject_CodeFunction", "Myproject_codefunction")
    assert not wch.is_lowercased_form("MyProject_CodeFunction", "MyProject_CodeFunction")
    assert not wch.is_lowercased_form("MYPROJECT_CodeFunction", "MyProject_CodeFunction")


# ─── item 5: the uncapped container ─────────────────────────────────────


def test_an_uncapped_container_is_a_problem_with_an_in_place_remedy(tmp_path):
    cap = rg.MemCap("vco_weaviate", 0, OLD_IMAGE)
    res = doctor.DoctorResolvers(weaviate_mem_cap=lambda: (cap, "podman", "8192m"))
    [f] = doctor.probe_weaviate_mem_cap(_root(tmp_path), res, {})
    assert f.is_problem and f.condition_id == rg.CID_UNCAPPED
    assert "podman update --memory 8192m --memory-swap 8192m vco_weaviate" in f.command
    assert "snapshot --container vco_weaviate" in f.command  # before any hand recreate
    report = doctor.DoctorReport(folder=_root(tmp_path), scope="full", findings=[f])
    [entry] = doctor.deferral_entries_for(report)
    assert entry.condition_id == rg.CID_UNCAPPED
    assert entry.dismiss_fields == {"container": "vco_weaviate"}


def test_a_capped_container_is_ok_and_carries_the_cid(tmp_path):
    cap = rg.MemCap("vco_weaviate", 8 * 1024 ** 3)
    res = doctor.DoctorResolvers(weaviate_mem_cap=lambda: (cap, "podman", None))
    [f] = doctor.probe_weaviate_mem_cap(_root(tmp_path), res, {})
    assert f.status == doctor.STATUS_OK and f.condition_id == rg.CID_UNCAPPED


def test_read_mem_cap_reads_hostconfig_memory():
    box = Runtime(memory="0")
    cap = rg.read_mem_cap("podman", run=box.run, names=["vco_weaviate"])
    assert cap == rg.MemCap("vco_weaviate", 0, OLD_IMAGE) and cap.uncapped


# ─── the registry ───────────────────────────────────────────────────────


def test_every_new_condition_is_registered_with_its_lifecycle():
    from vco_lib import deferral_probes, deferral_registry

    for cid, probe in ((rg.CID_INDEX_MISSING, "weaviate_collection_index_still_missing"),
                       (rg.CID_REPLAY_EXPOSED, "weaviate_raft_replay_still_exposed"),
                       (rg.CID_UNCAPPED, "weaviate_container_still_uncapped")):
        assert deferral_registry.disposition_for(cid) == "action_required"
        assert probe in deferral_probes.PROBES
    assert deferral_registry.disposition_for(rg.CID_UPGRADE_REFUSED) == "action_required"
    assert deferral_registry.disposition_for(
        "lowercase_codegraph_residual_" + "myproject_codefunction") == "environmental"
