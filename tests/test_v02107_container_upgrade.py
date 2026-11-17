# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 lane CONTAINERS (audit A-004, A-062; owner ruling "recreate it to
get latest").

* VCO's own container recorded as "adopted" is recorded as VCO-managed; a
  foreign one never is (``service_reconcile.reclassify_own``).
* VCO's own Weaviate moves to the pinned image only under protection
  (``vco_lib.container_upgrade``): counts of every class, recreate guard,
  free-space check, volume copy, verify, rollback. Every destructive branch
  has its act and its leave-alone test.
* A foreign Weaviate gets the memory limit in place, re-applied after a
  restart (``weaviate_memory.ensure_foreign_limit``); never a recreate.
* The limit is sized from min(host, container-engine memory).

The container engine is FAKED throughout (``FakeEngine`` for the flow, a
recording ``run`` for the argv each engine verb builds): no test here creates,
stops or removes a real container.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_v0297_service_reconcile import CASES, run_case  # noqa: E402
from vco_lib import container_upgrade as cu  # noqa: E402
from vco_lib import deferral_registry  # noqa: E402
from vco_lib import service_detection as det  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_reconcile as sr  # noqa: E402
from vco_lib import weaviate_memory as wm  # noqa: E402

OLD = "cr.weaviate.io/semitechnologies/weaviate:1.28.4"
PINNED = "cr.weaviate.io/semitechnologies/weaviate:1.37.18"
NAME = "vco_weaviate"
VOLUME = det.Mount("volume", "vco_weaviate_data", "/var/lib/weaviate")
CLASSES = {"Alpha_KnowledgeGraph": 120, "Alpha_CodeFunction": 4000, "Alpha_Diagrams": 0}


def _container(image=OLD, state="running", mount=VOLUME, memory=0):
    return det.ContainerInfo(name=NAME, image=image, state=state, mounts=(mount,),
                             memory_limit=memory)


ROW = se.EndpointRow(service="weaviate", mode="vco_managed", port=8081, source="live_reconcile",
                     grpc_port=50052, container_name=NAME)


class FakeEngine:
    """The engine as the move sees it: one container, one data location, the
    backups made from it, and knobs for each way a step can fail."""

    def __init__(self, classes=CLASSES, *, used_kb=1_000_000, free_kb=5_000_000):
        self.image, self.running, self.exists = OLD, True, True
        self.mount = VOLUME
        self.data = dict(classes)          # what the data location holds
        self.backups: dict[str, dict] = {}
        self.calls: list[str] = []
        self.used_kb, self.free_kb = used_kb, free_kb
        self.unreadable: set[str] = set()   # classes whose count cannot be read
        self.fail: set[str] = set()         # verbs that fail
        self.guard_ok = True
        self.on_new_start = lambda eng: None  # what the new version does to the data
        self.new_mount = VOLUME
        self.missing_index: list[str] = []
        self.started_images: list[str] = []

    # -- the ContainerEngine surface ------------------------------------
    def counts(self, _url):
        self.calls.append("counts")
        if not (self.exists and self.running):
            return None, "the class list (/v1/schema) could not be read"
        for name in sorted(self.data):
            if name in self.unreadable:
                return None, f"the object count of class {name} could not be read"
        return dict(self.data), ""

    def stop(self, _name):
        self.calls.append("stop")
        if "stop" in self.fail:
            return False
        self.running = False
        return True

    def start(self, _name):
        self.calls.append("start")
        if "start" in self.fail or not self.exists:
            return False
        self.running = True
        return True

    def guard(self):
        self.calls.append("guard")
        return (True, "", []) if self.guard_ok else (False, "the raft snapshot was refused", [])

    def create_volume(self, name):
        self.calls.append(f"create_volume:{name}")
        if "create_volume" in self.fail:
            return False
        self.backups[name] = {}
        return True

    def remove_volume(self, name):
        self.calls.append(f"remove_volume:{name}")
        self.backups.pop(name, None)
        return True

    def measure_kb(self, _source, _backup, _image):
        self.calls.append("measure")
        return None if "measure" in self.fail else (self.used_kb, self.free_kb)

    def copy_data(self, _source, backup, _image):
        self.calls.append("copy")
        if "copy" in self.fail:
            return False
        self.backups[backup] = dict(self.data)
        return True

    def remove_container(self, _name):
        self.calls.append("remove_container")
        if "remove_container" in self.fail:
            return False
        self.exists = self.running = False
        return True

    def compose_up(self, image_override=None):
        self.calls.append(f"compose_up:{image_override or 'pinned'}")
        if image_override is None and "compose_up" in self.fail:
            return False, "pull failed"
        if image_override is not None and "compose_up_old" in self.fail:
            return False, "compose failed"
        self.image = image_override or PINNED
        self.exists = self.running = True
        self.started_images.append(self.image)
        if image_override is None:
            self.on_new_start(self)
        return True, "compose up"

    def wait_ready(self, _url, _timeout):
        self.calls.append("wait_ready")
        return self.exists and self.running and "ready" not in self.fail

    def inspect(self, _name):
        if not self.exists:
            return None
        return det.ContainerInfo(name=NAME, image=self.image,
                                 state="running" if self.running else "exited",
                                 mounts=(self.new_mount if self.image == PINNED else self.mount,))

    def indexes_missing(self, _url, _classes):
        self.calls.append("indexes")
        return None if "indexes" in self.fail else list(self.missing_index)

    def restore_data(self, _source, backup, _image):
        self.calls.append("restore")
        if "restore" in self.fail:
            return False
        self.data = dict(self.backups[backup])
        return True


@pytest.fixture()
def root(tmp_path):
    infra = tmp_path / "orch" / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "docker-compose.yml").write_text(
        (REPO_ROOT / "infrastructure" / "docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8")
    return tmp_path / "orch"


def _move(root, eng, tmp_path, *, container=None, environ=None):
    return cu.protect_weaviate_move(
        root=root, runtime="podman", row=ROW, container=container or _container(),
        engine=eng, environ=environ if environ is not None else {},
        state_file=tmp_path / "state" / "container-upgrade.json", now=lambda: 1_800_000_000.0)


def _cids(outcome):
    return [e.condition_id for e in outcome.entries]


# ─── the count helper's contract (swap point for lane SAFETY's helper) ───


def _http(payload=None, status=200, raises=False):
    def call(_method, _url, _body, _timeout):
        if raises:
            raise OSError("connection refused")
        return status, json.dumps(payload).encode()
    return call


@pytest.mark.parametrize("http", [
    _http({"errors": [{"message": "no such class"}], "data": None}),
    _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": None}}]}}}),
    _http({"data": {"Aggregate": {"Cls": []}}}),
    _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": True}}]}}}),
    _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": -1}}]}}}),
    _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": 3}}]}}}, status=500),
    _http(raises=True),
])
def test_a_count_that_cannot_be_read_is_none_never_zero(http):
    assert cu.class_count("http://localhost:1", "Cls", http=http) is None


def test_a_readable_count_is_returned_and_zero_is_a_real_zero():
    ok = _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": 0}}]}}})
    assert cu.class_count("http://localhost:1", "Cls", http=ok) == 0
    ok = _http({"data": {"Aggregate": {"Cls": [{"meta": {"count": 852104}}]}}})
    assert cu.class_count("http://localhost:1", "Cls", http=ok) == 852104


def test_unreadable_counts_after_the_move_are_a_mismatch_not_equality():
    assert cu.count_mismatches({"A": 0}, None)            # 0 == None must never pass
    assert cu.count_mismatches({"A": 0}, {"A": 0}) == []
    assert cu.count_mismatches({"A": 5, "B": 1}, {"A": 5}) == ["B: 1 before, absent after"]


def test_every_class_is_counted_and_one_unreadable_class_fails_the_whole_read():
    def http(method, url, body, _t):
        if method == "GET":
            return 200, json.dumps({"classes": [{"class": "A"}, {"class": "B"}]}).encode()
        cls = "A" if " A " in body["query"] else "B"
        if cls == "B":
            return 200, json.dumps({"errors": [{"message": "shard not ready"}]}).encode()
        return 200, json.dumps({"data": {"Aggregate": {"A": [{"meta": {"count": 7}}]}}}).encode()

    counts, why = cu.class_counts("http://localhost:1", http=http)
    assert counts is None and "class B" in why


# ─── recreate vs keep ────────────────────────────────────────────────────


def test_same_image_is_not_moved_and_the_engine_is_never_asked(root, tmp_path):
    eng = FakeEngine()
    out = _move(root, eng, tmp_path, container=_container(image=PINNED))
    assert out.status == cu.NOT_NEEDED and not out.blocks_recreate
    assert eng.calls == [] and out.entries == []


def test_an_older_own_container_is_moved_in_the_protected_order(root, tmp_path):
    eng = FakeEngine()
    out = _move(root, eng, tmp_path)
    assert out.status == cu.MOVED and not out.blocks_recreate
    backup = out.backup
    assert backup.startswith("vco-weaviate-backup-")
    # counts BEFORE the stop; guard (raft snapshot) and the copy BEFORE the
    # old container is removed and the new version first starts.
    assert eng.calls == [
        "counts", "stop", "guard", f"create_volume:{backup}", "measure", "copy",
        "remove_container", "compose_up:pinned", "wait_ready", "indexes", "counts"]
    assert eng.image == PINNED and eng.data == CLASSES
    assert eng.backups[backup] == CLASSES           # the copy is KEPT
    assert _cids(out) == [cu.CID_BACKUP_KEPT]
    assert f"podman volume rm {backup}" in out.entries[0].command_to_apply
    assert out.empty_classes == ["Alpha_Diagrams"]  # reported, never dropped
    assert "Alpha_Diagrams" in eng.data


def test_a_newer_live_version_is_never_downgraded(root, tmp_path):
    eng = FakeEngine()
    newer = "cr.weaviate.io/semitechnologies/weaviate:1.40.0"
    out = _move(root, eng, tmp_path, container=_container(image=newer))
    assert out.status == cu.REFUSED and out.blocks_recreate and eng.calls == []


def test_another_repository_or_an_unversioned_tag_is_not_judged():
    assert cu.image_move("registry.example/own/weaviate:1.20.0", PINNED) is None
    assert cu.image_move("cr.weaviate.io/semitechnologies/weaviate:latest", PINNED) is None
    assert cu.image_move(OLD, PINNED).direction == "upgrade"


# ─── opt-out and dry run ────────────────────────────────────────────────


def test_opt_out_keeps_the_old_container_untouched(root, tmp_path):
    eng = FakeEngine()
    out = _move(root, eng, tmp_path, environ={cu.MODE_ENV: "off"})
    assert out.status == cu.OPTED_OUT and out.blocks_recreate and eng.calls == []
    assert eng.image == OLD and eng.running


def test_dry_run_prints_what_would_be_done_and_does_nothing(root, tmp_path):
    eng = FakeEngine()
    out = _move(root, eng, tmp_path, environ={cu.MODE_ENV: "dry-run"})
    assert out.status == cu.DRY_RUN and out.blocks_recreate and eng.calls == []
    text = "\n".join(out.lines)
    assert OLD in text and PINNED in text and "volume vco_weaviate_data" in text
    assert "nothing is changed" in text


# ─── refusals: the old container is left exactly as found ───────────────


def test_an_unreadable_count_refuses_before_anything_is_stopped(root, tmp_path):
    eng = FakeEngine()
    eng.unreadable = {"Alpha_CodeFunction"}
    out = _move(root, eng, tmp_path)
    assert out.status == cu.REFUSED and out.blocks_recreate
    assert eng.calls == ["counts"] and eng.running and eng.image == OLD
    assert "Alpha_CodeFunction" in out.entries[0].detected
    assert _cids(out) == [cu.CID_REFUSED]


def test_a_refused_guard_starts_the_old_container_again_and_copies_nothing(root, tmp_path):
    eng = FakeEngine()
    eng.guard_ok = False
    out = _move(root, eng, tmp_path)
    assert out.status == cu.REFUSED and out.blocks_recreate
    assert eng.calls == ["counts", "stop", "guard", "start"]
    assert eng.running and eng.image == OLD and eng.exists and eng.backups == {}


def test_a_copy_that_does_not_fit_keeps_the_old_container_and_names_both_numbers(root, tmp_path):
    # 20 GiB of data, 21 GiB free: fits raw, not with the 10 % headroom.
    eng = FakeEngine(used_kb=20 * 1024 * 1024, free_kb=21 * 1024 * 1024)
    out = _move(root, eng, tmp_path)
    assert out.status == cu.REFUSED and out.blocks_recreate
    assert "copy" not in eng.calls and "remove_container" not in eng.calls
    assert not any(c.startswith("compose_up") for c in eng.calls)
    assert eng.calls[-1] == "start" and eng.running and eng.image == OLD
    assert eng.backups == {}  # the empty backup volume VCO created is removed again
    detected = out.entries[0].detected
    assert "20.0 GiB" in detected and "21.0 GiB" in detected and "22.0 GiB" in detected


def test_a_copy_that_fits_is_made(root, tmp_path):
    eng = FakeEngine(used_kb=20 * 1024 * 1024, free_kb=23 * 1024 * 1024)
    out = _move(root, eng, tmp_path)
    assert out.status == cu.MOVED and "copy" in eng.calls


def test_unmeasurable_space_refuses(root, tmp_path):
    eng = FakeEngine()
    eng.fail = {"measure"}
    out = _move(root, eng, tmp_path)
    assert out.status == cu.REFUSED and "copy" not in eng.calls
    assert eng.running and eng.image == OLD and eng.backups == {}


def test_an_incomplete_copy_refuses_and_never_removes_the_old_container(root, tmp_path):
    eng = FakeEngine()
    eng.fail = {"copy"}
    out = _move(root, eng, tmp_path)
    assert out.status == cu.REFUSED and "remove_container" not in eng.calls
    assert eng.running and eng.image == OLD and eng.backups == {}


def test_a_stopped_own_container_is_started_by_name_to_be_counted(root, tmp_path):
    eng = FakeEngine()
    eng.running = False
    out = _move(root, eng, tmp_path, container=_container(state="exited"))
    assert eng.calls[:3] == ["start", "wait_ready", "counts"] and out.status == cu.MOVED


def test_a_stopped_container_that_does_not_come_up_is_left_alone(root, tmp_path):
    eng = FakeEngine()
    eng.running = False
    eng.fail = {"start"}
    out = _move(root, eng, tmp_path, container=_container(state="exited"))
    assert out.status == cu.REFUSED and eng.calls == ["start"]


# ─── verify pass vs rollback ────────────────────────────────────────────


def _assert_rolled_back(eng, out, tmp_path):
    assert out.status == cu.ROLLED_BACK and out.blocks_recreate
    assert eng.image == OLD and eng.running            # the old image runs again …
    assert eng.data == CLASSES                         # … on the data as it was
    assert eng.started_images == [PINNED, OLD]
    tail = eng.calls[eng.calls.index("compose_up:pinned"):]
    assert tail[-5:] == ["stop", "remove_container", "restore", f"compose_up:{OLD}",
                         "wait_ready"] or tail[-6:-1] == [
        "stop", "remove_container", "restore", f"compose_up:{OLD}", "wait_ready"]
    assert eng.backups[out.backup] == CLASSES          # the copy survives the rollback
    assert _cids(out) == [cu.CID_ROLLED_BACK]
    state = json.loads((tmp_path / "state" / "container-upgrade.json").read_text())
    assert state["weaviate"]["from"] == OLD and state["weaviate"]["to"] == PINNED


def test_a_lost_object_rolls_back_to_the_old_image_on_the_copied_data(root, tmp_path):
    eng = FakeEngine()
    eng.on_new_start = lambda e: e.data.update(Alpha_CodeFunction=3999)
    out = _move(root, eng, tmp_path)
    _assert_rolled_back(eng, out, tmp_path)
    assert "Alpha_CodeFunction: 4000 before, 3999 after" in out.reason


def test_a_class_that_disappears_rolls_back(root, tmp_path):
    eng = FakeEngine()
    eng.on_new_start = lambda e: e.data.pop("Alpha_KnowledgeGraph")
    out = _move(root, eng, tmp_path)
    _assert_rolled_back(eng, out, tmp_path)


def test_a_class_without_its_index_rolls_back_even_with_equal_counts(root, tmp_path):
    # The 1.37 orphan drop removes one class's index; the other counts stay equal.
    eng = FakeEngine()
    eng.missing_index = ["Alpha_KnowledgeGraph"]
    out = _move(root, eng, tmp_path)
    _assert_rolled_back(eng, out, tmp_path)
    assert "without their index" in out.reason


def test_another_data_mount_after_the_recreate_rolls_back(root, tmp_path):
    # A recreate that lands on an empty default volume: 0 == 0 must not pass.
    eng = FakeEngine({"Alpha_Diagrams": 0})
    eng.new_mount = det.Mount("volume", "infrastructure_weaviate_data", "/var/lib/weaviate")
    out = cu.protect_weaviate_move(
        root=root, runtime="podman", row=ROW, container=_container(), engine=eng, environ={},
        state_file=tmp_path / "state" / "container-upgrade.json", now=lambda: 1_800_000_000.0)
    assert out.status == cu.ROLLED_BACK and "infrastructure_weaviate_data" in out.reason
    assert eng.image == OLD


def test_counts_that_cannot_be_read_after_the_move_roll_back(root, tmp_path):
    eng = FakeEngine()
    eng.on_new_start = lambda e: e.unreadable.add("Alpha_KnowledgeGraph")
    out = _move(root, eng, tmp_path)
    assert out.status in (cu.ROLLED_BACK, cu.ROLLBACK_FAILED) and out.blocks_recreate
    assert eng.image == OLD and "could not be read" in out.reason


def test_an_unchecked_index_rolls_back(root, tmp_path):
    eng = FakeEngine()
    eng.fail = {"indexes"}
    out = _move(root, eng, tmp_path)
    _assert_rolled_back(eng, out, tmp_path)


def test_a_new_version_that_never_becomes_ready_rolls_back(root, tmp_path):
    eng = FakeEngine()
    ready = iter([False, True])  # the pinned image: not ready; the old one: ready
    eng.wait_ready = lambda _u, _t: (eng.calls.append("wait_ready"), next(ready))[1]
    out = _move(root, eng, tmp_path)
    _assert_rolled_back(eng, out, tmp_path)


def test_a_failed_compose_rolls_back(root, tmp_path):
    eng = FakeEngine()
    eng.fail = {"compose_up"}
    out = _move(root, eng, tmp_path)
    assert out.status == cu.ROLLED_BACK and eng.image == OLD and eng.data == CLASSES
    assert eng.started_images == [OLD]


def test_a_restore_that_fails_is_reported_as_critical_with_the_manual_steps(root, tmp_path):
    eng = FakeEngine()
    eng.on_new_start = lambda e: e.data.update(Alpha_CodeFunction=1)
    eng.fail = {"restore"}
    out = _move(root, eng, tmp_path)
    assert out.status == cu.ROLLBACK_FAILED and out.blocks_recreate
    entry = out.entries[0]
    assert entry.condition_id == cu.CID_ROLLED_BACK and entry.severity == "critical"
    assert out.backup in entry.command_to_apply and "cp -a" in entry.command_to_apply
    assert eng.backups[out.backup] == CLASSES  # the copy is intact


def test_a_passing_verify_removes_nothing_and_records_no_failure(root, tmp_path):
    eng = FakeEngine()
    out = _move(root, eng, tmp_path)
    assert out.status == cu.MOVED and "restore" not in eng.calls
    assert not (tmp_path / "state" / "container-upgrade.json").exists()


# ─── no auto-heal loop ──────────────────────────────────────────────────


def test_a_failed_move_is_not_repeated_until_the_user_asks(root, tmp_path, monkeypatch):
    state_file = tmp_path / "state" / "container-upgrade.json"
    first = FakeEngine()
    first.on_new_start = lambda e: e.data.update(Alpha_CodeFunction=1)
    assert _move(root, first, tmp_path).status == cu.ROLLED_BACK

    second = FakeEngine()
    out = _move(root, second, tmp_path)
    assert out.status == cu.REFUSED and out.blocks_recreate and second.calls == []
    assert "retry --service weaviate" in out.entries[0].command_to_apply

    monkeypatch.setattr(cu, "state_path", lambda: state_file)
    assert cu.main(["retry", "--service", "weaviate"]) == 0
    third = FakeEngine()
    assert _move(root, third, tmp_path).status == cu.MOVED


# ─── the argv each engine verb builds (the real ContainerEngine) ────────


class Recorder:
    def __init__(self, stdout=""):
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        return mock.Mock(returncode=0, stdout=self.stdout, stderr="")


def test_the_container_is_removed_without_its_volumes(root):
    run = Recorder()
    cu.ContainerEngine("podman", root, run=run).remove_container(NAME)
    assert run.calls == [["podman", "rm", "--force", NAME]]
    assert "-v" not in run.calls[0] and "--volumes" not in run.calls[0]


def test_measure_and_copy_mount_the_data_read_only(root):
    run = Recorder("1000\n5000\n")
    eng = cu.ContainerEngine("docker", root, run=run)
    assert eng.measure_kb("vco_weaviate_data", "bk", OLD) == (1000, 5000)
    run.stdout = "42\n42\n"
    assert eng.copy_data("vco_weaviate_data", "bk", OLD) is True
    for argv in run.calls:
        assert argv[:3] == ["docker", "run", "--rm"]
        assert "vco_weaviate_data:/vco-from:ro" in argv and "bk:/vco-to" in argv
        assert OLD in argv  # the OLD image's own shell does the work
    assert "delete" not in " ".join(run.calls[1]) and "rm " not in run.calls[1][-1]


def test_a_copy_with_differing_file_counts_is_not_a_copy(root):
    eng = cu.ContainerEngine("podman", root, run=Recorder("42\n41\n"))
    assert eng.copy_data("vco_weaviate_data", "bk", OLD) is False


def test_restore_empties_only_the_data_location_and_reads_the_backup_read_only(root):
    run = Recorder("42\n42\n")
    assert cu.ContainerEngine("podman", root, run=run).restore_data("vco_weaviate_data", "bk", OLD)
    argv = run.calls[0]
    assert "bk:/vco-from:ro" in argv and "vco_weaviate_data:/vco-to" in argv
    assert argv[-1].startswith("find /vco-to -mindepth 1 -delete && cp -a /vco-from/. /vco-to/")


def test_unreadable_helper_output_is_none_not_zero(root):
    eng = cu.ContainerEngine("podman", root, run=Recorder("du: cannot read\n"))
    assert eng.measure_kb("vco_weaviate_data", "bk", OLD) is None


def test_the_rollback_override_names_the_old_image(root, tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "state_path", lambda: tmp_path / "state" / "container-upgrade.json")
    path = cu.ContainerEngine("podman", root, run=Recorder()).write_image_override(OLD)
    import yaml

    assert yaml.safe_load(path.read_text())["services"]["weaviate"]["image"] == OLD
    assert root not in path.parents  # never written into the checkout


def test_the_compose_call_names_only_weaviate_under_the_installers_project(root, monkeypatch, tmp_path):
    from vco_lib import compose_provider, compose_recovery, containers

    seen = {}

    def fake_up(argv, **kw):
        seen["argv"], seen["cwd"] = list(argv), kw["cwd"]
        return SimpleNamespace(ok=True, first_stderr="", stderr="")

    monkeypatch.setattr(containers, "compose_command", lambda _rt, **_kw: (["podman-compose"], "standalone"))
    monkeypatch.setattr(compose_recovery, "compose_up_with_recovery", fake_up)
    monkeypatch.setattr(compose_provider, "detect", lambda *_a, **_kw: None)
    monkeypatch.setattr(cu, "state_path", lambda: tmp_path / "state" / "container-upgrade.json")
    eng = cu.ContainerEngine("podman", root, run=Recorder())

    assert eng.compose_up(None)[0] is True
    argv = seen["argv"]
    assert argv[0] == "podman-compose" and seen["cwd"] == str(root / "infrastructure")
    assert argv[argv.index("-f") + 1] == str(root / "infrastructure" / "docker-compose.yml")
    assert argv[argv.index("-p") + 1] == "infrastructure"
    assert argv[-5:] == ["up", "-d", "--force-recreate", "--no-deps", "weaviate"]
    assert argv.count("-f") == 1                       # no image override on the way forward

    assert eng.compose_up(OLD)[0] is True              # the rollback appends the override LAST
    files = [seen["argv"][i + 1] for i, a in enumerate(seen["argv"]) if a == "-f"]
    assert files[0].endswith("docker-compose.yml")
    assert files[-1].endswith("container-upgrade-rollback-weaviate.yml")
    assert seen["argv"][-1] == "weaviate"


def test_the_guard_is_the_one_recreate_guard_without_its_compose_call(root, monkeypatch):
    from vco_lib import containers, service_lifecycle

    seen = {}

    def fake_up_services(services, **kw):
        seen.update(services=list(services), **kw)
        return SimpleNamespace(cleared=list(seen["verdict"]), entries=["entry"])

    monkeypatch.setattr(containers, "compose_command", lambda _rt, **_kw: (["podman-compose"], "standalone"))
    monkeypatch.setattr(service_lifecycle, "up_services", fake_up_services)
    eng = cu.ContainerEngine("podman", root, run=Recorder())
    seen["verdict"] = ["weaviate"]
    assert eng.guard() == (True, "", ["entry"])
    assert seen["services"] == ["weaviate"] and seen["guard_only"] is True
    assert seen["compose_dir"] == root / "infrastructure"
    seen["verdict"] = []
    cleared, why, entries = eng.guard()
    assert cleared is False and why and entries == ["entry"]   # the guard's own ledger row is kept


def test_no_compose_means_no_guard_and_no_recreate(root, monkeypatch):
    from vco_lib import containers

    monkeypatch.setattr(containers, "compose_command", lambda _rt, **_kw: None)
    eng = cu.ContainerEngine("podman", root, run=Recorder())
    assert eng.guard()[0] is False and eng.compose_up(None)[0] is False


# ─── classification: own vs foreign ─────────────────────────────────────


def _inputs(root, container, row, *, mount=VOLUME.to_row(), session=False):
    return sr.ServiceInputs(
        service=row.service, existing=row, containers=[container], containers_listed=True,
        installer_project="infrastructure", infra_dir=root / "infrastructure",
        installer_mount=mount, session=session, now_ms=1)


def _labelled(root, *, project="infrastructure", working_dir=None, mount=VOLUME, name=NAME):
    labels = {"com.docker.compose.project": project,
              "com.docker.compose.project.working_dir":
                  str(root / "infrastructure") if working_dir is None else working_dir}
    return det.ContainerInfo(name=name, image=OLD, state="running", labels=labels,
                             host_ports={8080: 8081, 50051: 50052}, mounts=(mount,))


ADOPTED = se.EndpointRow(service="weaviate", mode="adopted_container", port=8081,
                         source="migrated:legacy_compose", grpc_port=50052, container_name=NAME,
                         compose_project="infrastructure", data_mount=VOLUME.to_row())


def test_this_installs_own_container_recorded_as_adopted_becomes_managed(root):
    out = sr.decide(_inputs(root, _labelled(root), ADOPTED))
    assert out.how == "reclassified_own"
    assert (out.row.mode, out.row.container_name, out.row.port, out.row.grpc_port) == (
        "vco_managed", NAME, 8081, 50052)
    assert dict(out.row.data_mount) == VOLUME.to_row()
    assert out.row.compose_project == "infrastructure"


@pytest.mark.parametrize("why, kwargs", [
    ("another compose project", {"project": "otherapp"}),
    ("another clone's infrastructure/", {"working_dir": "/srv/second-clone/infrastructure"}),
    ("no working-directory label", {"working_dir": ""}),
    ("data on a bind the installer's compose does not mount",
     {"mount": det.Mount("bind", "/home/u/volumes/weaviate", "/var/lib/weaviate")}),
    ("another volume", {"mount": det.Mount("volume", "otherapp_weaviate", "/var/lib/weaviate")}),
])
def test_a_foreign_container_stays_adopted(root, why, kwargs):
    row = ADOPTED
    out = sr.decide(_inputs(root, _labelled(root, **kwargs), row))
    assert out.row.mode == "adopted_container", why
    assert out.how != "reclassified_own"


def test_an_unreadable_installer_mount_reclassifies_nothing(root):
    out = sr.decide(_inputs(root, _labelled(root), ADOPTED, mount=None))
    assert out.row.mode == "adopted_container"


def test_a_container_under_another_name_is_not_vcos_own(root):
    row = se.EndpointRow(service="weaviate", mode="adopted_container", port=8081,
                         source="install_probe", grpc_port=50052, container_name="my_weaviate")
    out = sr.decide(_inputs(root, _labelled(root, name="my_weaviate"), row))
    assert out.row.mode == "adopted_container"


def test_the_session_check_never_reclassifies(root):
    out = sr.decide(_inputs(root, _labelled(root), ADOPTED, session=True))
    assert out.row.mode == "adopted_container"


def test_the_installer_mounts_are_read_from_the_shipped_compose(root):
    mounts = sr.installer_data_mounts(root)
    assert mounts["weaviate"] == VOLUME.to_row()
    assert mounts["ollama"]["source"] == "vco_ollama_data"
    assert sr.installer_data_mounts(root / "missing") == dict.fromkeys(sr.SERVICES)


# ─── reconcile wiring: who is moved, who is only limited ────────────────


def _own_install(tmp_path):
    """The dogfood machine after one import run, then the Weaviate container
    relabelled as THIS install's compose created it (the audit's shape: VCO's
    own container, recorded as adopted)."""
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, root = run_case(tmp_path, case)
    assert se.load_rows(db)["weaviate"].mode == "adopted_container"
    machine.containers[NAME].update(project="infrastructure",
                                    working_dir=str(root / "infrastructure"))
    return case, db, machine, rec, root


def _outcome(status):
    return cu.MoveOutcome("weaviate", status=status, lines=[f"  [weaviate] {status}"])


def test_an_update_reclassifies_and_moves_vcos_own_weaviate(tmp_path):
    case, db, machine, rec, _root = _own_install(tmp_path)
    seen = []

    def mover(**kw):
        seen.append(kw)
        return _outcome(cu.MOVED)

    result, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec, mover=mover,
                          limiter=lambda **_kw: pytest.fail("VCO's own container is not limited in place"))
    assert se.load_rows(db)["weaviate"].mode == "vco_managed"
    assert len(seen) == 1 and seen[0]["container"].name == NAME
    assert seen[0]["row"].mode == "vco_managed"
    assert result.disposition("weaviate") == "recreate"   # step 5 re-applies the compose
    assert "  [weaviate] moved" in result.lines
    # No machine command stopped, removed or recreated anything in reconcile itself.
    assert not [a for a in machine.argv_log if a[1] in ("stop", "rm", "update", "run", "volume")]


@pytest.mark.parametrize("status", [cu.REFUSED, cu.ROLLED_BACK, cu.ROLLBACK_FAILED,
                                    cu.OPTED_OUT, cu.DRY_RUN])
def test_a_move_that_was_not_made_keeps_step_5_away_from_the_container(tmp_path, status):
    case, db, machine, rec, _root = _own_install(tmp_path)
    result, *_ = run_case(tmp_path, case, db=db, machine=machine, recorder=rec,
                          mover=lambda **_kw: _outcome(status))
    assert result.disposition("weaviate") == "skip"


def test_a_foreign_weaviate_is_never_moved_and_gets_the_limit_in_place(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    _first, db, machine, rec, _root = run_case(tmp_path, case)   # legacy compose home: foreign
    limited = []

    def limiter(**kw):
        limited.append(kw)
        return wm.ForeignLimit(wm.APPLIED, NAME, 1, "6144m", "  [weaviate] limit applied")

    result, *_ = run_case(
        tmp_path, case, db=db, machine=machine, recorder=rec, limiter=limiter,
        mover=lambda **_kw: pytest.fail("a foreign container must never reach the move"))
    assert se.load_rows(db)["weaviate"].mode == "adopted_container"
    assert result.disposition("weaviate") == "skip" and result.moves == {}
    assert len(limited) == 1 and limited[0]["row"].container_name == NAME
    assert "  [weaviate] limit applied" in result.lines


@pytest.mark.parametrize("change", [
    {"working_dir": "/srv/second-clone/infrastructure"},      # another clone, same project name
    {"mounts": [{"Type": "volume", "Name": "infrastructure_weaviate_data",
                 "Source": "/x/_data", "Destination": "/var/lib/weaviate"}]},  # not the installer's data
])
def test_a_managed_row_whose_container_is_not_this_installs_own_is_never_moved(tmp_path, change):
    case, db, machine, rec, _root = _own_install(tmp_path)
    run_case(tmp_path, case, db=db, machine=machine, recorder=rec,
             mover=lambda **_kw: _outcome(cu.MOVED))
    assert se.load_rows(db)["weaviate"].mode == "vco_managed"
    machine.containers[NAME].update(change)
    result, *_ = run_case(
        tmp_path, case, db=db, machine=machine, recorder=rec,
        mover=lambda **_kw: pytest.fail("not provably VCO's own: never moved"))
    assert result.moves == {}


def test_the_session_check_and_the_cli_reconcile_never_move_a_container(tmp_path):
    case, db, machine, rec, _root = _own_install(tmp_path)
    never = lambda **_kw: pytest.fail("no move outside install.py --update")  # noqa: E731
    run_case(tmp_path, case, db=db, machine=machine, recorder=rec, phase="session", mover=never,
             limiter=never)
    run_case(tmp_path, case, db=db, machine=machine, recorder=rec, protect_moves=False,
             mover=never)
    import inspect

    assert "protect_moves=False" in inspect.getsource(sr.cli)


def test_the_drift_entry_no_longer_says_vco_never_recreates_what_it_adopted(tmp_path):
    case = next(c for c in CASES if c["id"] == "a_dogfood_shape")
    result, *_ = run_case(tmp_path, case, limiter=lambda **_kw: wm.ForeignLimit(wm.ALREADY))
    entry = next(e for e in result.entries if e.condition_id == sr.CID_CONFIG_DRIFT)
    assert "not VCO's own" in entry.why_deferred and "memory limit" in entry.why_deferred


# ─── the limit: sized from min(host, engine) ────────────────────────────


def test_the_limit_is_sized_from_the_engines_memory_when_it_is_smaller():
    # The Windows notebook: 15.29 GiB host, 7.40 GiB Docker Desktop VM. It was 6144m.
    engine = 7948070912 / 1024 ** 3
    assert wm.mem_limit_mib(15.29) == 6144
    assert wm.mem_limit_mib(15.29, engine) == 2816
    assert wm.env_value(15.29, environ={}, engine_ram_gb=engine) == "2816m"


def test_an_unreadable_engine_gives_the_host_sized_value():
    assert wm.mem_limit_mib(16.0, None) == wm.mem_limit_mib(16.0) == 6400
    assert wm.env_value(16.0, environ={}, engine_ram_gb=None) == "6400m"


def test_a_native_engine_reporting_the_hosts_memory_changes_nothing():
    assert wm.mem_limit_mib(32.0, 32.0) == wm.mem_limit_mib(32.0)
    assert wm.mem_limit_mib(32.0, 64.0) == wm.mem_limit_mib(32.0)  # never above the host


def test_only_the_engine_known_still_sizes_a_limit():
    assert wm.mem_limit_mib(None, 8.0) == 3072
    assert wm.mem_limit_mib(None, None) is None


def test_both_numbers_are_named():
    line = wm.describe_budget(15.29, 7.40)
    assert "host 15.3 GiB" in line and "container engine 7.4 GiB" in line and "2816m" in line
    assert "not readable" in wm.describe_budget(16.0, None)


@pytest.mark.parametrize("runtime, first", [("docker", "{{.MemTotal}}"),
                                            ("podman", "{{.Host.MemTotal}}"),
                                            ("/usr/bin/docker", "{{.MemTotal}}")])
def test_the_engine_is_asked_in_its_own_format(runtime, first):
    run = Recorder("7948070912\n")
    assert wm.engine_memory_gb(runtime, run=run) == pytest.approx(7.402, abs=0.01)
    assert run.calls == [[runtime, "info", "--format", first]]


def test_an_engine_that_does_not_answer_is_unknown():
    assert wm.engine_memory_gb("docker", run=Recorder("<no value>\n")) is None
    assert wm.engine_memory_gb(None) is None

    def boom(*_a, **_kw):
        raise OSError("no such file")

    assert wm.engine_memory_gb("podman", run=boom) is None


def test_the_engine_memory_can_be_stated_by_hand(monkeypatch):
    monkeypatch.setattr(wm, "_probed_engine_memory_gb",
                        lambda: pytest.fail("a stated figure must not probe"))
    assert wm.resolve_engine_memory_gb({wm.ENGINE_ENV_KEY: "7.4"}) == 7.4
    assert wm.resolve_engine_memory_gb({wm.ENGINE_ENV_KEY: "0"}) is None
    assert wm.env_value(15.29, environ={wm.ENGINE_ENV_KEY: "7.4"}) == "2816m"


def test_the_users_own_limit_still_wins(monkeypatch):
    monkeypatch.setattr(wm, "_probed_engine_memory_gb", lambda: 4.0)
    assert wm.env_value(16.0, environ={wm.ENV_KEY: "0"}) is None


# ─── the limit on a container VCO did not create ────────────────────────


class LimitEngine:
    """``inspect`` / ``update`` for one container's memory limit."""

    def __init__(self, memory=0, update_ok=True):
        self.memory, self.update_ok = memory, update_ok
        self.updates: list[list[str]] = []
        self.other: list[list[str]] = []

    def __call__(self, argv, **_kw):
        if argv[1] == "inspect":
            return mock.Mock(returncode=0, stdout=f"{self.memory}\n", stderr="")
        if argv[1] == "update":
            self.updates.append(list(argv))
            if not self.update_ok:
                return mock.Mock(returncode=125, stdout="", stderr="Error: cgroup v1 not supported")
            self.memory = wm.parse_limit_bytes(argv[argv.index("--memory") + 1])
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.other.append(list(argv))
        return mock.Mock(returncode=1, stdout="", stderr="unexpected")


FOREIGN_ROW = SimpleNamespace(mode="adopted_container", container_name="their_weaviate")
ENV = {wm.ENV_KEY: "6144m"}


def _limit(run, *, row=FOREIGN_ROW, environ=ENV, dry_run=False, root=Path("/nonexistent")):
    return wm.ensure_foreign_limit(root=root, runtime="podman", row=row, run=run,
                                   environ=environ, dry_run=dry_run)


def test_an_uncapped_foreign_container_gets_the_limit_in_place():
    run = LimitEngine(memory=0)
    out = _limit(run)
    assert out.status == wm.APPLIED and not out.uncapped
    assert run.updates == [["podman", "update", "--memory", "6144m", "--memory-swap", "6144m",
                            "their_weaviate"]]
    assert run.other == []  # nothing else: no stop, no rm, no restart, no recreate
    assert "not recreated" in out.line


def test_the_limit_lost_at_a_restart_is_applied_again():
    run = LimitEngine(memory=0)
    assert _limit(run).status == wm.APPLIED
    assert _limit(run).status == wm.ALREADY and len(run.updates) == 1   # still there: no-op
    run.memory = 0                                                    # podman 4.9: gone after a restart
    assert _limit(run).status == wm.APPLIED and len(run.updates) == 2


def test_vcos_own_container_is_never_updated_in_place():
    run = LimitEngine(memory=0)
    own = SimpleNamespace(mode="vco_managed", container_name=NAME)
    assert _limit(run, row=own).status == wm.NOT_FOREIGN
    assert _limit(run, row=None).status == wm.NOT_FOREIGN
    assert _limit(run, row=SimpleNamespace(mode="adopted_external", container_name=None)).status \
        == wm.NOT_FOREIGN
    assert run.updates == []


def test_a_container_with_its_owners_limit_is_left_alone():
    run = LimitEngine(memory=16 * 1024 ** 3)
    out = _limit(run)
    assert out.status == wm.OWN_LIMIT and run.updates == [] and "left as it is" in out.line


@pytest.mark.parametrize("environ", [
    {wm.ENV_KEY: "6144m", wm.FOREIGN_LIMIT_ENV: "off"},   # the switch
    {wm.ENV_KEY: "0"},                                     # the user's "no limit"
])
def test_the_users_opt_out_leaves_it_uncapped(environ):
    run = LimitEngine(memory=0)
    out = _limit(run, environ=environ)
    assert out.status == wm.OPTED_OUT and run.updates == []


def test_no_sized_limit_means_nothing_is_guessed(tmp_path):
    run = LimitEngine(memory=0)
    out = _limit(run, environ={}, root=tmp_path)
    assert out.status == wm.UNKNOWN and run.updates == [] and out.uncapped


def test_the_limit_install_sized_is_read_from_the_env_file(tmp_path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / ".env").write_text(f"{wm.ENV_KEY}=2816m\n", encoding="utf-8")
    run = LimitEngine(memory=0)
    out = _limit(run, environ={}, root=tmp_path)
    assert out.status == wm.APPLIED and run.updates[0][3] == "2816m"


def test_dry_run_prints_the_command_and_does_not_run_it():
    run = LimitEngine(memory=0)
    out = _limit(run, dry_run=True)
    assert out.status == wm.WOULD_APPLY and run.updates == []
    assert "podman update --memory 6144m --memory-swap 6144m their_weaviate" in out.line


def test_a_refused_update_is_reported_with_the_manual_command():
    run = LimitEngine(memory=0, update_ok=False)
    out = _limit(run)
    assert out.status == wm.FAILED and out.uncapped
    assert "cgroup v1" in out.line and "podman update --memory 6144m" in out.line


def test_an_unreadable_limit_changes_nothing():
    def run(argv, **_kw):
        assert argv[1] == "inspect"
        return mock.Mock(returncode=125, stdout="", stderr="no such container")

    assert _limit(run).status == wm.UNKNOWN


@pytest.mark.parametrize("text, want", [("6144m", 6144 * 1024 ** 2), ("8g", 8 * 1024 ** 3),
                                        ("0", 0), ("1073741824", 1024 ** 3), ("lots", None),
                                        ("", None)])
def test_limit_sizes_parse(text, want):
    assert wm.parse_limit_bytes(text) == want


# ─── surfaces: inspect, hooks, registry ─────────────────────────────────


def test_the_containers_limit_is_parsed_and_reported():
    obj = {"Name": "/x", "Config": {"Image": OLD}, "State": {"Status": "running"},
           "HostConfig": {"Memory": 25769803776}}
    info = det.parse_inspect(obj)
    assert info.memory_limit == 25769803776
    assert info.to_json()["memory_limit_bytes"] == 25769803776
    obj["HostConfig"] = {}
    assert det.parse_inspect(obj).memory_limit is None


@pytest.mark.parametrize("hook", ["ensure-containers.sh", "ensure-containers.ps1"])
def test_both_hooks_re_apply_the_foreign_limit_at_every_ensure(hook):
    text = (REPO_ROOT / "templates" / "hooks" / hook).read_text(encoding="utf-8-sig")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    calls = [line for line in code if "vco_lib.weaviate_memory ensure-foreign-limit" in line]
    assert len(calls) == 1
    assert code.index(calls[0]) > max(i for i, line in enumerate(code) if "start" in line.lower()
                                      and "container" in line.lower() and i < code.index(calls[0]))


def test_the_cli_verb_the_hooks_call_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wm, "ensure_foreign_limit_for_install",
                        lambda root, dry_run=False: wm.ForeignLimit(wm.APPLIED, "c", 1, "1g", "  line"))
    assert wm.main(["ensure-foreign-limit", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "line"
    monkeypatch.setattr(wm, "ensure_foreign_limit_for_install",
                        lambda root, dry_run=False: wm.ForeignLimit(wm.FAILED, "c", 0, "1g", "  no"))
    assert wm.main(["ensure-foreign-limit", "--root", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == wm.FAILED


def test_the_conditions_are_registered_with_their_class():
    assert deferral_registry.disposition_for("weaviate_container_uncapped") == "environmental"
    assert deferral_registry.disposition_for(sr.CID_CONFIG_DRIFT) == "environmental"
    assert deferral_registry.disposition_for(cu.CID_REFUSED) == "action_required"
    assert deferral_registry.disposition_for(cu.CID_ROLLED_BACK) == "action_required"
    assert deferral_registry.disposition_for(cu.CID_BACKUP_KEPT) == "informational_record"
