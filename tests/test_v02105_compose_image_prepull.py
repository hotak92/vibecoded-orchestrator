"""v0.2.105 — step 5 pulls a service's pinned image before force-recreating it.

Verified on podman-compose 1.5 before this change: ``up --force-recreate``
whose image pull then fails REMOVES the running container and creates
nothing, leaving the service down. The pre-pull keeps such a service running
on its current image and ledgers ``service_image_pull_failed``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_v02100_install_services_up import World
from vco_lib import compose_image_prepull as pp
from vco_lib import install_services_up as isu

PINS = {"weaviate": "cr.weaviate.io/semitechnologies/weaviate:1.37.18",
        "ollama": "docker.io/ollama/ollama:0.40.5"}


def _pinned(svc, root):
    return PINS.get(svc)


class FakeRuntime:
    def __init__(self, present=(), pull_ok=()):
        self.present, self.pull_ok = set(present), set(pull_ok)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if argv[3] in self.present else 125, "", "no such image")
        if argv[1] == "pull":
            ok = argv[2] in self.pull_ok
            return subprocess.CompletedProcess(argv, 0 if ok else 125, "",
                                               "" if ok else "toomanyrequests: pull rate limit")
        raise AssertionError(argv)


def test_present_image_is_recreated_without_a_pull():
    rt = FakeRuntime(present=PINS.values())
    keep, refused = pp.split_recreates(["weaviate", "ollama"], runtime="podman", root=Path("/r"),
                                       run=rt, pinned=_pinned)
    assert keep == ["weaviate", "ollama"] and refused == []
    assert not [c for c in rt.calls if c[1] == "pull"]


def test_missing_image_is_pulled_then_recreated():
    rt = FakeRuntime(pull_ok={PINS["weaviate"]})
    keep, refused = pp.split_recreates(["weaviate"], runtime="docker", root=Path("/r"),
                                       run=rt, pinned=_pinned)
    assert keep == ["weaviate"] and refused == []
    assert ["docker", "pull", PINS["weaviate"]] in rt.calls


def test_unpullable_image_keeps_the_running_container():
    rt = FakeRuntime(present={PINS["ollama"]})
    keep, refused = pp.split_recreates(["weaviate", "ollama", "code_embed"], runtime="podman",
                                       root=Path("/r"), run=rt, pinned=_pinned)
    assert keep == ["ollama", "code_embed"]
    assert refused == [("weaviate", PINS["weaviate"], "toomanyrequests: pull rate limit")]


def test_a_locally_built_service_is_never_pulled():
    rt = FakeRuntime()
    keep, _ = pp.split_recreates(["code_embed"], runtime="podman", root=Path("/r"), run=rt,
                                 pinned=lambda s, r: "localhost/vco-code-embed:latest")
    assert keep == ["code_embed"] and rt.calls == []


class _Report:
    def __init__(self):
        self.entries = []

    def add_entry(self, e):
        self.entries.append(e)


def test_drop_mutates_the_plan_and_ledgers(monkeypatch, capsys):
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", _pinned)
    services, report = ["weaviate", "code_embed"], _Report()
    pp.drop_unpullable_recreates(services, runtime="podman", root=Path("/r"),
                                 deferral_report=report, rerun="python install.py --update",
                                 run=FakeRuntime())
    assert services == ["code_embed"]
    (entry,) = report.entries
    assert entry.condition_id == pp.CID_PULL_FAILED
    assert f"podman pull {PINS['weaviate']}" in entry.command_to_apply
    assert "Keeping the running container" in capsys.readouterr().out


def test_drop_leaves_the_plan_alone_when_every_image_is_there(monkeypatch):
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", _pinned)
    services, report = ["weaviate"], _Report()
    pp.drop_unpullable_recreates(services, runtime="podman", root=Path("/r"),
                                 deferral_report=report, rerun="x",
                                 run=FakeRuntime(present=PINS.values()))
    assert services == ["weaviate"] and report.entries == []


def test_a_probe_fault_keeps_the_original_plan(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("compose unreadable")
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", boom)
    services = ["weaviate"]
    pp.drop_unpullable_recreates(services, runtime="podman", root=Path("/r"),
                                 deferral_report=_Report(), rerun="x", run=FakeRuntime())
    assert services == ["weaviate"]


def test_step5_does_not_recreate_a_service_whose_image_cannot_be_pulled(tmp_path, monkeypatch):
    """The real caller: compose_up_step drops weaviate from the recreate argv."""
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", _pinned)
    w = World(tmp_path)
    w.plan.services_to_recreate = ["weaviate", "code_embed"]
    world_run = w.run

    def run(argv, **kw):
        if argv[1:3] == ["image", "inspect"] or argv[1] == "pull":
            return FakeRuntime()(argv)
        return world_run(argv, **kw)

    outcome, out = w.go(run=run)
    assert outcome == isu.OK
    argv = w.compose_calls[0]
    assert "weaviate" not in argv and argv[-1] == "code_embed"
    assert pp.CID_PULL_FAILED in w.ledger.cids


def test_step5_recreates_as_planned_when_the_image_pulls(tmp_path, monkeypatch):
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image", _pinned)
    w = World(tmp_path)
    w.plan.services_to_recreate = ["weaviate", "code_embed"]
    world_run = w.run
    fake = FakeRuntime(pull_ok={PINS["weaviate"]})

    def run(argv, **kw):
        if argv[1:3] == ["image", "inspect"] or argv[1] == "pull":
            return fake(argv)
        return world_run(argv, **kw)

    outcome, _ = w.go(run=run)
    assert outcome == isu.OK
    assert "weaviate" in w.compose_calls[0]
    assert pp.CID_PULL_FAILED not in w.ledger.cids
