"""v0.2.104 — an adopted container on an old image, and first-install noise.

G3 (field case 2026-10-16): the maintainer's Ollama container was ADOPTED
on an earlier install, so no update recreated it. v0.2.103 pinned Ollama 0.40.5
(older Ollama cannot pull the summary-tier models) but the adopted container
kept 0.20.2, and the ``adopted_service_config_drift`` entry named only env
drift; its clear probe answered "undetermined". Pinned here:

* image drift is part of the one drift rule (``service_reconcile.config_drift``)
  — an older image is listed, the same image (registry prefix / ROCm variant
  aside) is not, and a newer or unversioned one is never claimed;
* the clear probe inspects only the adopted rows' own containers, so an
  unrelated container vanishing between ``ps`` and ``inspect`` no longer
  leaves the entry undetermined; it decides both ways;
* the printed remedy, ``hand-to-vco``, now acts on the field shape: an
  ``adopted_container`` row whose container already carries VCO's own
  compose project (it refused that as "already owned").

§B (Windows first install): the "no usable row … run install.py --update"
warning fired mid-install before step [5b] wrote the rows, and a presence
probe printed "model-gateway: no key for vendor …" for every vendor of a
gateway that was never configured.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from vco_lib import service_adoption as sa
from vco_lib import service_detection as det
from vco_lib import service_endpoints as se
from vco_lib import service_reconcile as sr

REPO = Path(__file__).resolve().parents[1]

PIN_COMPOSE = """\
services:
  ollama:
    image: docker.io/ollama/ollama:0.40.5
    container_name: vco_ollama
"""


def _root(tmp_path: Path, compose: str = PIN_COMPOSE) -> Path:
    root = tmp_path / "orch"
    (root / "infrastructure").mkdir(parents=True)
    (root / "infrastructure" / "docker-compose.yml").write_text(compose, encoding="utf-8")
    return root


_CANON_ENV = {"OLLAMA_KEEP_ALIVE": "10m", "OLLAMA_MAX_QUEUE": "1000"}


def _ollama(image: str, env: dict | None = None) -> det.ContainerInfo:
    return det.ContainerInfo("vco_ollama", image, "running",
                             env=dict(_CANON_ENV if env is None else env))


# ─── the image comparison ────────────────────────────────────────────────


@pytest.mark.parametrize("live,pinned,expect", [
    ("docker.io/ollama/ollama:0.20.2", "docker.io/ollama/ollama:0.40.5", "0.40.5"),
    ("ollama/ollama:0.20.2", "docker.io/ollama/ollama:0.40.5", "0.40.5"),
    ("docker.io/ollama/ollama:0.20.2-rocm", "docker.io/ollama/ollama:0.40.5", "0.40.5"),
    ("docker.io/ollama/ollama:0.20.2@sha256:" + "a" * 64, "docker.io/ollama/ollama:0.40.5", "0.40.5"),
    # same image: registry prefix, ROCm variant, digest — never drift
    ("ollama/ollama:0.40.5", "docker.io/ollama/ollama:0.40.5", None),
    ("index.docker.io/ollama/ollama:0.40.5", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama:0.40.5-rocm", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama:0.40.5", "docker.io/ollama/ollama:0.40.5-rocm", None),
    # not provable: newer, unversioned, digest-only, another repository
    ("docker.io/ollama/ollama:0.41.0", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama:latest", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama@sha256:" + "b" * 64, "docker.io/ollama/ollama:0.40.5", None),
    ("ghcr.io/me/ollama:0.1.0", "docker.io/ollama/ollama:0.40.5", None),
    ("docker.io/ollama/ollama:0.20.2", None, None),
])
def test_image_drift_is_an_older_version_of_the_same_repository(live, pinned, expect):
    got = sr.image_drift(live, pinned)
    assert (got[1] if got else None) == expect
    if got:
        assert got[0] == live


def test_the_pinned_image_is_read_from_the_shipped_compose():
    assert sa.pinned_image("ollama", REPO) == "docker.io/ollama/ollama:0.40.5"
    assert (sa.pinned_image("weaviate", REPO) or "").startswith("cr.weaviate.io/semitechnologies/weaviate:")


def test_an_override_image_is_the_pin_a_hand_over_would_run(tmp_path):
    root = _root(tmp_path)
    (root / "infrastructure" / "compose.override.yaml").write_text(
        "services:\n  ollama:\n    image: docker.io/ollama/ollama:0.50.0\n", encoding="utf-8")
    assert sa.pinned_image("ollama", root) == "docker.io/ollama/ollama:0.50.0"


# ─── config_drift + the ledger entry ─────────────────────────────────────


def test_an_older_adopted_image_is_listed(tmp_path):
    drift = sr.config_drift("ollama", _ollama("docker.io/ollama/ollama:0.20.2"), _root(tmp_path))
    assert drift == {sr.IMAGE_DRIFT_KEY: ("docker.io/ollama/ollama:0.20.2", "0.40.5")}


@pytest.mark.parametrize("image", ["ollama/ollama:0.40.5", "docker.io/ollama/ollama:0.40.5",
                                   "docker.io/ollama/ollama:0.40.5-rocm"])
def test_the_same_image_is_not_listed(tmp_path, image):
    assert sr.config_drift("ollama", _ollama(image), _root(tmp_path)) == {}


def test_the_entry_names_the_image_and_why_and_keeps_the_remedies(tmp_path):
    result = sr.ReconcileResult(
        rows={"ollama": se.EndpointRow("ollama", "adopted_container", 11435, "user_cli",
                                       container_name="vco_ollama")},
        detection=det.Detection(containers=[_ollama("docker.io/ollama/ollama:0.20.2", env={})]))
    entry = next(e for e in sr._outcome_entries(result, _root(tmp_path))
                 if e.condition_id == sr.CID_CONFIG_DRIFT)
    assert ("ollama (vco_ollama): image docker.io/ollama/ollama:0.20.2, VCO pins 0.40.5 — "
            "the newer summary models need it") in entry.detected
    assert "OLLAMA_KEEP_ALIVE=None (VCO sets '10m')" in entry.detected
    assert "python -m vco_lib.service_endpoints hand-to-vco --service ollama" in entry.command_to_apply
    assert "dismiss this entry" in entry.command_to_apply
    assert "image" in entry.dismiss_fields["drift_keys"].split(",")


# ─── the clear probe ─────────────────────────────────────────────────────


def _inspect_obj(info: det.ContainerInfo) -> dict:
    return {"Name": info.name, "Config": {"Image": info.image, "Labels": {},
                                          "Env": [f"{k}={v}" for k, v in info.env.items()]},
            "State": {"Status": info.state}, "HostConfig": {}, "Mounts": []}


def _machine(ollama: det.ContainerInfo, *, transient_vanishes: bool = True):
    """``ps`` lists VCO's Ollama plus an unrelated container that is gone by
    the time it is inspected (another project's ``--rm`` run)."""
    calls: list[list[str]] = []

    def run(argv, **_kw):
        argv = list(argv)
        calls.append(argv)
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, "vco_ollama\nsomeones_test_run\n", "")
        names = argv[argv.index("container") + 1:]
        if transient_vanishes and "someones_test_run" in names:
            return subprocess.CompletedProcess(argv, 125, "", "no such container someones_test_run")
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            [_inspect_obj(ollama)] if "vco_ollama" in names else []), "")

    return run, calls


def _probe(tmp_path, run, *, services="ollama"):
    db = tmp_path / "launcher.db"
    from tests.common.launcher_db_fixture import make_launcher_db
    make_launcher_db(db)
    se.write_rows([se.EndpointRow("ollama", "adopted_container", 11435, "user_cli",
                                  container_name="vco_ollama")], db_path=db)
    entry = SimpleNamespace(dismiss_fields={"service": services, "drift_keys": "image"})
    return sr.probe_config_drift(entry, orchestrator_root=_root(tmp_path), runtime="podman",
                                 run=run, db_path=db)


def test_the_probe_decides_drift_although_an_unrelated_container_vanished(tmp_path):
    run, calls = _machine(_ollama("docker.io/ollama/ollama:0.20.2"))
    assert _probe(tmp_path, run) is True
    inspected = [c for c in calls if c[1] == "inspect"]
    assert inspected and all("someones_test_run" not in c for c in inspected)


def test_the_probe_decides_the_drift_is_over(tmp_path):
    run, _ = _machine(_ollama("docker.io/ollama/ollama:0.40.5"))
    assert _probe(tmp_path, run) is False


def test_the_probe_reports_no_drift_for_a_gone_container(tmp_path):
    def run(argv, **_kw):
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, "someones_test_run\n", "")
        raise AssertionError(f"nothing of ours to inspect: {argv}")
    assert _probe(tmp_path, run) is False


def test_the_probe_stays_undetermined_without_a_runtime(tmp_path):
    def run(argv, **_kw):
        raise OSError("no runtime")
    assert _probe(tmp_path, run) is None


# ─── hand-to-vco on VCO's own (adopted-row) container ────────────────────


def _own_project_world(tmp_path):
    from tests.test_v0296_service_adoption import OWN_PROJECT, World
    world = World(tmp_path)
    for name in world.projects:
        world.projects[name] = OWN_PROJECT
        world.working_dirs[name] = str(world.infra)
        world.config_files[name] = "docker-compose.yml"
    return world


def test_hand_to_vco_recreates_an_adopted_row_container_already_under_vcos_project(tmp_path):
    world = _own_project_world(tmp_path)

    def run(argv, **kw):  # hand-to-vco names the container: it is looked up by id first
        if list(argv)[1:2] == ["inspect"] and "{{json .Id}}" in argv:
            return subprocess.CompletedProcess(list(argv), 0, '"abc123"', "")
        return world.run(argv, **kw)

    result, _ = world.adopt(run=run, services=("ollama",), container_refs={"ollama": "vco_ollama"},
                            commit_rows=lambda rows: None)
    assert result.refused == {}
    assert result.adopted == ["ollama"]
    ups = [a for a in world.all_argv if "compose" in a and "up" in a]
    assert ups and ups[0][-1] == "ollama"


def test_bulk_adoption_still_leaves_vcos_own_containers_alone(tmp_path):
    world = _own_project_world(tmp_path)
    result, _ = world.adopt(services=("ollama",))
    assert result.adopted == []
    assert result.refused["ollama"] == "already owned by this install's compose project"


# ─── §B (1): no false "run install.py --update" before step [5b] ─────────


def _no_row_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and "no usable" in r.getMessage()]


def test_no_row_warning_is_silent_while_this_install_still_writes_the_rows(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(se, "_WARNED", set())
    monkeypatch.delenv(se.ROWS_PENDING_ENV, raising=False)
    absent = tmp_path / "absent.db"
    assert se.mark_rows_pending(absent) is True
    with caplog.at_level(logging.DEBUG, logger=se.__name__):
        se.machine_service_urls(absent)
    assert _no_row_warnings(caplog) == []
    with mock.patch.dict(os.environ):  # the transport pin is restored after
        se.pin_process_env({})  # step [5b] decided the rows: the grace ends
        assert se.ROWS_PENDING_ENV not in os.environ
        with caplog.at_level(logging.WARNING, logger=se.__name__):
            se.machine_service_urls(absent)
    assert _no_row_warnings(caplog), "an absent row after [5b] is reported again"


def test_a_machine_with_rows_gets_no_grace(tmp_path, monkeypatch):
    from tests.common.launcher_db_fixture import make_launcher_db
    monkeypatch.delenv(se.ROWS_PENDING_ENV, raising=False)
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([se.EndpointRow("ollama", "vco_managed", 11435, "install_probe",
                                  container_name="vco_ollama")], db_path=db)
    assert se.mark_rows_pending(db) is False
    assert se.ROWS_PENDING_ENV not in os.environ


def test_install_sets_the_grace_before_the_venv_hop_and_ends_it_at_5b(tmp_path, monkeypatch):
    import install

    monkeypatch.delenv(se.ROWS_PENDING_ENV, raising=False)
    monkeypatch.setattr(se, "load_rows", lambda db_path=None: {})
    seen = {}
    monkeypatch.setattr(install._install_companions, "continue_in_venv",
                        lambda *a, **k: seen.setdefault("env", os.environ.get(se.ROWS_PENDING_ENV)))
    flow = mock.Mock(carried_state=lambda: [])
    install._continue_in_venv_after_deps(Path("/nonexistent/python"), argparse.Namespace(),
                                         None, {}, flow)
    assert seen["env"] == "1", "the child (and its subprocesses) must inherit the grace"

    result = SimpleNamespace(lines=[], entries=[], abort=None, rows={}, pinned=False,
                             weaviate_pending=False, migrate_code_embed=False,
                             disposition=lambda _s: "start")
    from vco_lib import service_reconcile
    monkeypatch.setattr(service_reconcile, "reconcile", lambda **_kw: result)
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS",
                        {"rows": {}, "pinned": False, "weaviate_pending": False})
    for key in se.transport_env({}):
        monkeypatch.delenv(key, raising=False)
    args = argparse.Namespace(update=False, yes=True, quiet=True, on_conflict=None, service=[])
    assert install._reconcile_service_endpoints(
        args, mock.Mock(container_cmd="podman", has_gpu=False), mock.Mock()) is not None
    assert se.ROWS_PENDING_ENV not in os.environ


# ─── §B (2): a never-configured gateway prints no per-vendor warning ─────


def test_a_presence_probe_resolver_does_not_warn_on_a_miss(caplog):
    import model_router.secrets as mod
    from model_router.secrets import VendorKeyResolver
    from model_router.vendors import VENDORS

    vendor = next(iter(VENDORS.values()))
    with caplog.at_level(logging.DEBUG, logger=mod.logger.name):
        quiet = VendorKeyResolver(getter=lambda k, project=None: "", miss_log_level=logging.DEBUG)
        assert quiet.resolve(vendor).key is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        VendorKeyResolver(getter=lambda k, project=None: "").resolve(vendor)
    assert any("no key for vendor" in r.getMessage() for r in caplog.records), \
        "the daemon's default keeps the real warning"


def test_the_shipped_presence_probe_is_the_quiet_one(monkeypatch):
    import model_router.secrets as mod
    from vco_lib import model_selection as ms

    made = {}

    class Spy:
        def __init__(self, **kw):
            made.update(kw)

    monkeypatch.setattr(mod, "VendorKeyResolver", Spy)
    ms.vendor_key_state_probe()
    assert made.get("miss_log_level") == logging.DEBUG


def _providers(tmp_path, capsys, states, record=None):
    from vco_lib import model_selection as ms
    from vco_lib.model_selection import KeyState

    state_path = tmp_path / "model-guidance.json"
    if record is not None:
        state_path.write_text(json.dumps({"version": 1, "providers": record}), encoding="utf-8")
    ms.set_provider_override(None)
    ms.reachable_providers(state_probe=lambda v: states.get(v, KeyState.ABSENT),
                           state_path=state_path, sleeper=lambda _s: None)
    return capsys.readouterr().err


def test_never_configured_vendors_print_nothing(tmp_path, capsys):
    assert _providers(tmp_path, capsys, {}) == ""


def test_a_configured_vendors_missing_key_still_warns(tmp_path, capsys):
    from vco_lib.model_selection import KeyState, load_grid

    vendors = sorted({p for row in load_grid().rows for p in row.providers} - {"anthropic"})
    gone = vendors[0]
    err = _providers(tmp_path, capsys, {v: KeyState.PRESENT for v in vendors[1:]},
                     record={v: True for v in vendors})
    assert f"the key for vendor {gone!r} resolved before and no longer does" in err
    assert all(f"vendor {v!r} resolved before" not in err for v in vendors[1:])
    # said on the round that records the change, not on every later one
    assert _providers(tmp_path, capsys, {v: KeyState.PRESENT for v in vendors[1:]}) == ""


# ─── R2 M2: hand-to-vco on a ROCm host composes the ROCm overlay ──────────

_REPO_INFRA = Path(__file__).resolve().parents[1] / "infrastructure"


def _gpu_own_world(tmp_path, devices, owning_files):
    """VCO's own Ollama under VCO's project, with the SHIPPED overlays on disk
    and *devices* as its live ``HostConfig.Devices``."""
    world = _own_project_world(tmp_path)
    for name in ("docker-compose.gpu.yml", "docker-compose.rocm.yml"):
        (world.infra / name).write_text(
            (_REPO_INFRA / name).read_text(encoding="utf-8"), encoding="utf-8")
    world.config_files["vco_ollama"] = owning_files

    def run(argv, **kw):
        argv = list(argv)
        if argv[1:2] == ["inspect"] and "{{json .Id}}" in argv:
            return subprocess.CompletedProcess(argv, 0, '"abc123"', "")
        if argv[1:2] == ["inspect"] and "{{json .HostConfig.Devices}}" in argv \
                and argv[-1] == "vco_ollama":
            world.all_argv.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(devices), "")
        return world.run(argv, **kw)
    return world, run


def _hand_over_ollama(world, run):
    with mock.patch.object(sa, "_host_gpu_vendor",
                           side_effect=AssertionError("host probe not expected")):
        result, _ = world.adopt(run=run, services=("ollama",),
                                container_refs={"ollama": "vco_ollama"},
                                commit_rows=lambda rows: None)
    ups = [a for a in world.all_argv if "compose" in a and "up" in a]
    files = [up[i + 1] for up in ups for i, tok in enumerate(up) if tok == "-f"]
    return result, files


def _merged_ollama_image(files):
    env = sa.infrastructure_env_for_substitution(Path(files[0]).parent)
    cfg: dict = {}
    for f in files:
        doc = sa.load_compose_doc(Path(f), env) or {}
        cfg = sa.merge_compose(cfg, doc) if cfg else doc
    return cfg["services"]["ollama"]["image"]


_ROCM_DEVICES = [{"PathOnHost": "/dev/kfd", "PathInContainer": "/dev/kfd"},
                 {"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri"}]


def test_a_rocm_container_is_handed_over_under_the_rocm_overlay_and_image(tmp_path):
    world, run = _gpu_own_world(tmp_path, _ROCM_DEVICES,
                                "docker-compose.yml,docker-compose.rocm.yml")
    result, files = _hand_over_ollama(world, run)
    assert result.refused == {}
    assert result.adopted == ["ollama"]
    names = [Path(f).name for f in files]
    assert "docker-compose.rocm.yml" in names
    assert "docker-compose.gpu.yml" not in names
    assert _merged_ollama_image(files).endswith("-rocm")


def test_an_nvidia_container_is_still_handed_over_under_the_nvidia_overlay(tmp_path):
    world, run = _gpu_own_world(
        tmp_path, [{"PathOnHost": "/dev/nvidia0", "PathInContainer": "/dev/nvidia0"}],
        "docker-compose.yml,docker-compose.gpu.yml")
    result, files = _hand_over_ollama(world, run)
    assert result.adopted == ["ollama"]
    names = [Path(f).name for f in files]
    assert "docker-compose.gpu.yml" in names
    assert "docker-compose.rocm.yml" not in names
    assert not _merged_ollama_image(files).endswith("-rocm")


def test_a_cpu_container_is_handed_over_with_no_gpu_overlay(tmp_path):
    world, run = _gpu_own_world(tmp_path, [], "docker-compose.yml")
    result, files = _hand_over_ollama(world, run)
    assert result.adopted == ["ollama"]
    names = [Path(f).name for f in files]
    assert not any(n in names for n in ("docker-compose.gpu.yml", "docker-compose.rocm.yml"))


@pytest.mark.parametrize("devices, expected", [
    ([{"PathOnHost": "/dev/kfd"}, {"PathOnHost": "/dev/dri"}], "amd"),
    (["/dev/kfd:/dev/kfd"], "amd"),
    (["amd.com/gpu=all"], "amd"),
    ([{"PathOnHost": "/dev/nvidia0"}, {"PathOnHost": "/dev/nvidiactl"}], "nvidia"),
    (["nvidia.com/gpu=all"], "nvidia"),
    ([{"PathOnHost": "/dev/dri"}], None),        # Intel or AMD: no verdict
    (["/dev/kfd", "/dev/nvidia0"], None),        # both: ambiguous
    ([], None),
    (None, None),
])
def test_the_device_list_names_its_vendor_only_positively(devices, expected):
    from vco_lib import gpu_verify
    assert gpu_verify.vendor_of_devices(devices) == expected


def test_a_compose_stanza_names_its_vendor_from_devices_or_deploy_requests():
    from vco_lib import gpu_verify
    nvidia_deploy = {"deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]}}}}
    assert gpu_verify.vendor_of_compose_service(nvidia_deploy) == "nvidia"
    assert gpu_verify.vendor_of_compose_service({"devices": ["/dev/kfd", "/dev/dri"]}) == "amd"
    assert gpu_verify.vendor_of_compose_service({"image": "x"}) is None
    bare = {"deploy": {"resources": {"reservations": {"devices": [
        {"count": "all", "capabilities": ["gpu"]}]}}}}
    assert gpu_verify.vendor_of_compose_service(bare) is None


def test_the_vendor_choice_reads_the_containers_first_then_the_host():
    never = mock.Mock(side_effect=AssertionError("host not asked"))
    assert sa.choose_gpu_vendor(["amd", None], host_vendor=never) == ("amd", "")
    assert sa.choose_gpu_vendor([None, "nvidia"], host_vendor=never) == ("nvidia", "")
    # no container evidence: the host's selected card decides
    assert sa.choose_gpu_vendor([None], host_vendor=lambda: "amd") == ("amd", "")
    assert sa.choose_gpu_vendor([None], host_vendor=lambda: "nvidia") == ("nvidia", "")
    assert sa.choose_gpu_vendor([], host_vendor=lambda: "") == ("nvidia", "")
    vendor, reason = sa.choose_gpu_vendor(["amd", "nvidia"], host_vendor=never)
    assert vendor is None and "refusing" in reason


def test_mixed_vendor_gpu_services_are_refused_not_guessed(tmp_path):
    world, run = _gpu_own_world(tmp_path, _ROCM_DEVICES,
                                "docker-compose.yml,docker-compose.gpu.yml")
    # the owning file declares NVIDIA, the live container holds /dev/kfd
    gpu_doc = (world.infra / "docker-compose.gpu.yml").read_text(encoding="utf-8")
    assert "nvidia" in gpu_doc
    result, files = _hand_over_ollama(world, run)
    assert result.adopted == []
    assert "different GPU vendors" in result.refused["ollama"]
    assert files == []


# ─── R3 M1 / L3: a container naming BOTH vendors; the host probe is patchable ─

@pytest.mark.parametrize("devices, expected", [
    ([{"PathOnHost": "/dev/kfd"}, {"PathOnHost": "/dev/dri"}], ("amd",)),
    (["nvidia.com/gpu=all"], ("nvidia",)),
    (["/dev/kfd", "/dev/nvidia0"], ("amd", "nvidia")),   # both: evidence of both
    ([{"PathOnHost": "/dev/dri"}], ()),
    (None, ()),
])
def test_the_device_list_reports_every_vendor_it_names(devices, expected):
    from vco_lib import gpu_verify
    assert gpu_verify.vendors_of_devices(devices) == expected


def test_a_live_container_holding_both_vendors_devices_is_refused_on_hand_over(tmp_path):
    """Its own devices name AMD and NVIDIA: one chain carries one overlay, so
    the hand-over refuses instead of asking the host and dropping the other
    vendor's devices (the guard in ``_hand_over_ollama`` fails if the host
    probe is reached)."""
    both = _ROCM_DEVICES + [{"PathOnHost": "/dev/nvidia0", "PathInContainer": "/dev/nvidia0"}]
    world, run = _gpu_own_world(tmp_path, both, "docker-compose.yml")
    result, files = _hand_over_ollama(world, run)
    assert result.adopted == []
    assert "different GPU vendors" in result.refused["ollama"]
    assert files == []


def test_the_host_probe_is_resolved_when_called_so_a_patch_reaches_it():
    """L3: ``choose_gpu_vendor`` must look ``_host_gpu_vendor`` up at call
    time — a default argument bound at import would make every patch of the
    module attribute (the hand-over guard above included) inert."""
    with mock.patch.object(sa, "_host_gpu_vendor", return_value="amd") as probe:
        assert sa.choose_gpu_vendor([None]) == ("amd", "")
    probe.assert_called_once_with()
