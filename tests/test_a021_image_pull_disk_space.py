# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-021 — step 5 does not pull a new service image that does not fit.

Incident: a pinned Ollama bump pulled ~10 GB with 11.4 GB free and took the
host drive to 0. Now ``compose_image_prepull`` compares the free space where
the engine stores images with an estimate of the pull; free < need → no
``pull`` call, the running container is kept, a ``service_image_no_disk_space``
row names both numbers. Unknown size → pull as before; free ≥ need → pull.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vco_lib import compose_image_prepull as pp
from vco_lib import deferral_registry as dr

OLLAMA_NEW = "docker.io/ollama/ollama:0.40.5"
GIB = 1024 ** 3


def _pinned(svc, _root):
    return {"ollama": OLLAMA_NEW}.get(svc)


class Engine:
    """A fake engine with one older Ollama installed (docker's naming) and an
    image store at *graph_root*."""

    def __init__(self, graph_root: str, *, installed: bool = True,
                 old_size: int = int(9.7 * GIB), pull_ok: bool = True):
        self.graph_root, self.installed = graph_root, installed
        self.old_size, self.pull_ok = old_size, pull_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        cp = subprocess.CompletedProcess
        if argv[1:3] == ["image", "ls"]:
            rows = "ollama/ollama:0.20.2 sha256abc\nweaviate:1 sha256def\n" if self.installed else ""
            return cp(argv, 0, rows, "")
        if argv[1:3] == ["image", "inspect"]:
            if "--format" in argv and argv[-1] == "sha256abc":
                return cp(argv, 0, f"{self.old_size}\n", "")
            return cp(argv, 125, "", "no such image")
        if argv[1] == "info":
            return cp(argv, 0, self.graph_root + "\n", "")
        if argv[1] == "pull":
            return cp(argv, 0 if self.pull_ok else 125, "", "" if self.pull_ok else "boom")
        raise AssertionError(argv)

    def pulled(self) -> bool:
        return any(c[1] == "pull" for c in self.calls)


class Report:
    def __init__(self):
        self.entries = []

    def add_entry(self, e):
        self.entries.append(e)


@pytest.fixture
def drop(monkeypatch):
    """Run step 5's call with a fixed free-space reading."""
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", _pinned)

    def _run(services, engine, free_bytes, report):
        monkeypatch.setattr(pp, "_free_bytes", lambda _p: free_bytes)
        pp.drop_unpullable_recreates(services, runtime="docker", root=Path("/r"),
                                     deferral_report=report,
                                     rerun="python install.py --update", run=engine)
    return _run


def test_gate_decision_table():
    assert pp.decide_pull_fits(None, 1) is True
    assert pp.decide_pull_fits(10, None) is True
    assert pp.decide_pull_fits(10, 10) is True
    assert pp.decide_pull_fits(10, 9) is False


def test_free_below_need_refuses_without_a_pull_and_records_both_numbers(tmp_path, drop):
    engine = Engine(str(tmp_path))
    report = Report()
    services = ["ollama"]
    drop(services, engine, int(11.4 * GIB), report)

    assert services == [], "the service is not recreated: its container is kept"
    assert not engine.pulled(), "no pull call may be made when it does not fit"
    (entry,) = report.entries
    assert entry.condition_id == pp.CID_NO_DISK_SPACE
    text = entry.detected + entry.why_deferred
    assert "11.4 GB free" in text and str(tmp_path) in text
    need = int(9.7 * GIB * pp.PULL_SPACE_FACTOR) + 2 * GIB
    assert f"{need / GIB:.1f} GB" in text


def test_free_above_need_pulls(tmp_path, drop):
    engine = Engine(str(tmp_path))
    report = Report()
    services = ["ollama"]
    drop(services, engine, 100 * GIB, report)
    assert services == ["ollama"] and engine.pulled() and report.entries == []


def test_unknown_size_pulls_as_before(tmp_path, drop):
    engine = Engine(str(tmp_path), installed=False)  # no image of the repository
    report = Report()
    services = ["ollama"]
    drop(services, engine, 1, report)  # even with ~no free space
    assert services == ["ollama"] and engine.pulled() and report.entries == []


def test_unknown_storage_path_pulls_as_before(tmp_path, monkeypatch, drop):
    engine = Engine(str(tmp_path / "not-on-this-host"))
    monkeypatch.setattr(pp, "_desktop_vm_dirs", lambda _is_podman: [])
    report = Report()
    services = ["ollama"]
    drop(services, engine, 1, report)
    assert services == ["ollama"] and engine.pulled() and report.entries == []


def test_present_image_is_never_checked(tmp_path, drop):
    class Present(Engine):
        def __call__(self, argv, **kw):
            if argv[1:3] == ["image", "inspect"] and argv[-1] == OLLAMA_NEW:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return super().__call__(argv, **kw)

    engine = Present(str(tmp_path))
    services = ["ollama"]
    drop(services, engine, 1, Report())
    assert services == ["ollama"]
    assert not [c for c in engine.calls if c[1] in ("info", "pull") or c[1:3] == ["image", "ls"]]


def test_vm_engine_measures_the_host_folder_of_its_disk(tmp_path, monkeypatch):
    vm_dir = tmp_path / "Docker" / "wsl"
    vm_dir.mkdir(parents=True)
    monkeypatch.setattr(pp, "_desktop_vm_dirs", lambda _is_podman: [vm_dir])
    # Docker Desktop answers a path inside its VM, which this host does not have.
    engine = Engine("/var/lib/docker-inside-the-vm-xyz")
    assert pp.engine_storage_path("docker", run=engine) == vm_dir
    # The podman graph root is read with podman's own template.
    native = Engine(str(tmp_path))
    assert pp.engine_storage_path("podman", run=native) == tmp_path
    assert ["podman", "info", "--format", "{{.Store.GraphRoot}}"] in native.calls


@pytest.mark.parametrize("ref,key", [
    ("docker.io/ollama/ollama:0.40.5", "ollama/ollama"),
    ("ollama/ollama:0.20.2", "ollama/ollama"),
    ("docker.io/library/redis:7", "redis"),
    ("redis", "redis"),
    ("cr.weaviate.io/semitechnologies/weaviate:1.37.18", "cr.weaviate.io/semitechnologies/weaviate"),
    ("localhost:5000/img:1", "localhost:5000/img"),
])
def test_repo_key_normalises_engine_naming(ref, key):
    assert pp._repo_key(ref) == key


def test_condition_is_registered_owned_and_action_required():
    spec = dr.condition(pp.CID_NO_DISK_SPACE)
    assert spec is not None and spec.condition_class == "action_required"
    assert pp.CID_NO_DISK_SPACE in dr.install_owned_ids()


# ─── v0.2.108: the code_embed image BUILD gets the same check ─────────────

CE_SIZE = int(7.5 * GIB)


class BuildEngine:
    """A fake engine whose ``vco_code_embed`` container runs a previously
    built image of *size* bytes (``None`` = no container: a first build)."""

    def __init__(self, graph_root: str, *, size=CE_SIZE):
        self.graph_root, self.size = graph_root, size
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        cp = subprocess.CompletedProcess
        if argv[1] == "inspect" and "--type" in argv:
            if self.size is None or argv[-1] != "vco_code_embed":
                return cp(argv, 125, "", f"no such container {argv[-1]}")
            return cp(argv, 0, "sha256builtce\n", "")
        if argv[1:3] == ["image", "inspect"] and argv[-1] == "sha256builtce":
            return cp(argv, 0, f"{self.size}\n", "")
        if argv[1] == "info":
            return cp(argv, 0, self.graph_root + "\n", "")
        raise AssertionError(argv)


def _drop_build(engine, free_bytes, *, recreate=("code_embed",), rebuild=("code_embed",)):
    plan = {"build": ["code_embed"], "recreate": list(recreate), "rebuild": list(rebuild)}
    report = Report()
    pp.drop_unfittable_builds(plan["build"], plan["recreate"], plan["rebuild"],
                              runtime="podman", deferral_report=report,
                              rerun="python install.py --update", run=engine,
                              free=lambda _p: free_bytes)
    return plan, report


def test_a_rebuild_that_does_not_fit_loses_its_build_and_keeps_the_container(tmp_path, capsys):
    plan, report = _drop_build(BuildEngine(str(tmp_path)), int(10 * GIB))
    assert plan == {"build": [], "recreate": [], "rebuild": []}
    (entry,) = report.entries
    assert entry.condition_id == pp.CID_NO_DISK_SPACE
    text = entry.detected + entry.why_deferred
    need = int(CE_SIZE * pp.PULL_SPACE_FACTOR) + 2 * GIB
    assert f"{need / GIB:.1f} GB" in text and "10.0 GB free" in text and str(tmp_path) in text
    assert "vco_code_embed" in text and "download or build" in text
    assert "Not building it; keeping the running container" in capsys.readouterr().out


def test_a_rebuild_that_fits_is_left_alone(tmp_path):
    plan, report = _drop_build(BuildEngine(str(tmp_path)), 100 * GIB)
    assert plan == {"build": ["code_embed"], "recreate": ["code_embed"],
                    "rebuild": ["code_embed"]}
    assert report.entries == []


def test_a_first_build_with_no_previous_image_runs_as_before(tmp_path):
    plan, report = _drop_build(BuildEngine(str(tmp_path), size=None), 1)
    assert plan["build"] == ["code_embed"] and report.entries == []


def test_a_recreate_owed_to_a_config_change_survives_a_refused_build(tmp_path):
    # plan_rebuild adds no recreate when code_embed is already recreated for
    # its config: only `--build` goes, the config recreate stays
    plan, report = _drop_build(BuildEngine(str(tmp_path)), 1, rebuild=())
    assert plan == {"build": [], "recreate": ["code_embed"], "rebuild": []}
    assert len(report.entries) == 1


def test_step5_drops_the_build_before_compose_runs(tmp_path, monkeypatch):
    """Wiring: install step 5 (compose_up_step) runs the build check, so a
    rebuild that does not fit never reaches compose; one that fits does."""
    from tests.test_v02100_install_services_up import World

    for free_bytes, compose_ran in ((1, False), (100 * GIB, True)):
        root = tmp_path / str(free_bytes)
        root.mkdir()
        w = World(root)
        engine = BuildEngine(str(root))
        base_run = w.run

        def run(argv, _engine=engine, _base=base_run, **kw):
            if (argv[1] == "inspect" and "--type" in argv) \
                    or argv[1:] == ["info", "--format", "{{.Store.GraphRoot}}"] \
                    or argv[1:3] == ["image", "inspect"]:
                return _engine(argv)
            return _base(argv, **kw)

        monkeypatch.setattr(pp, "_free_bytes", lambda _p, _f=free_bytes: _f)
        w.go(run=run)
        assert bool(w.compose_calls) is compose_ran
        if compose_ran:
            assert "--build" in w.compose_calls[0]
            assert pp.CID_NO_DISK_SPACE not in w.ledger.cids
        else:
            assert pp.CID_NO_DISK_SPACE in w.ledger.cids
