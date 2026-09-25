# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R10 — J2, J4, J6, J8: where VCO's data is, and what a refused
runtime record says about it.

* J8 — a ``VCT_*_VOLUME_NAME`` override is a name the USER picked: under the
  runtime a stale record would be switched TO it counts only when corroborated
  there (a VCO container, or the volume's compose project label naming VCO's
  project). The ``vco_*`` defaults count on their own.
* J2 — a bind-mounted data folder (``VCT_*_DATA_SOURCE=<dir>``) is VCO's data
  on the host, under neither runtime: while it exists and is not empty, no
  surface switches the record on the strength of the other runtime's leftover
  volume. (c) keeps it; (a) refuses read-only and records ``action_required``
  at install.
* J6 — WHY a stale record was not switched comes from ONE table
  (``vco_lib/runtime_reconcile_messages.toml``) that the Rust refusal renders
  too; ``tests/fixtures/runtime_data_evidence_cases.json`` runs both.
* J4 — the refusal re-emitted every session keeps its first ``detected_at``,
  has one title, and an unchanged entry does not rewrite the ledger.

Every runtime is a FAKE (``which``/``run`` doubles or shell stubs on a private
PATH, ``VCT_TOOL_SEARCH_DIRS`` pinned); every install root is a tmp directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_v0297_r9_runtime_locations import (
    _SHELLS,
    Machine,
    _clone,
    _fake_bin,
    _root,
    _run_wrapper,
    _stub,
    _wrapper_env,
)
from vco_lib import containers
from vco_lib import runtime_reconcile as rr
from vco_lib.deferral_report import DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "runtime_data_evidence_cases.json")
                     .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The shared fixture (the Rust suite runs the same JSON)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", FIXTURE["compose_project"],
                         ids=[c["name"] for c in FIXTURE["compose_project"]])
def test_compose_project_matches_the_shared_fixture(case: dict):
    got = containers.compose_project_name(Path("/x") / case["compose_dir"], case["compose_text"])
    assert got == case["expect"]


@pytest.mark.parametrize("case", FIXTURE["bind_sources"],
                         ids=[c["name"] for c in FIXTURE["bind_sources"]])
def test_bind_sources_match_the_shared_fixture(case: dict):
    assert list(rr.bind_sources_from(case["env_file"], case["env"])) == case["expect"]


@pytest.mark.parametrize("case", FIXTURE["evidence"], ids=[c["name"] for c in FIXTURE["evidence"]])
def test_switch_evidence_matches_the_shared_fixture(case: dict):
    names = rr.volume_names_from(case["env_file"], case["env"])
    got = rr.data_evidence(set(case["containers"]), set(case["volumes"]), names,
                           own_project=case["own_project"],
                           label_of=lambda v: case["labels"].get(v),
                           corroborate_overrides=True)
    assert got is case["expect"]


@pytest.mark.parametrize("case", FIXTURE["not_switched"],
                         ids=[c["name"] for c in FIXTURE["not_switched"]])
def test_the_not_switched_wording_matches_the_shared_fixture(case: dict):
    got = rr.unusable_detail(case["pinned"], case["source"], "missing", case["decline"],
                             bind=case["bind"])
    assert got == case["expect"]


def test_the_data_source_keys_are_compose_envs_knobs():
    from vco_lib.compose_env import DATA_KNOBS

    assert rr.DATA_SOURCE_KEYS == tuple(pair[0] for pair in DATA_KNOBS.values())


def test_vcos_compose_project_is_the_identity_guards():
    """The label VCO's own volumes carry is the project the compose identity
    guard derives for ``infrastructure/`` — read from the real compose file."""
    infra = REPO_ROOT / "infrastructure"
    text = (infra / "docker-compose.yml").read_text(encoding="utf-8")
    assert rr.vco_compose_project(REPO_ROOT) == containers.compose_project_name(infra, text)
    assert rr.vco_compose_project(None) == ""


# ---------------------------------------------------------------------------
# J8 — a user-picked volume name is not evidence about the OTHER runtime
# ---------------------------------------------------------------------------


class LabelledMachine(Machine):
    """A :class:`Machine` whose ``volume inspect`` answers compose labels."""

    def __init__(self, *, labels=None, **kw):
        super().__init__(**kw)
        self.labels = dict(labels or {})

    def run(self, argv, **kw):
        if list(argv[1:3]) == ["volume", "inspect"]:
            self.calls.append(list(argv))
            label = self.labels.get((argv[0], argv[-1]), "")
            import subprocess  # noqa: PLC0415

            return subprocess.CompletedProcess(argv, 0, stdout=label + "\n", stderr="")
        return super().run(argv, **kw)


def _infra_env(root: Path, text: str) -> None:
    (root / "infrastructure").mkdir(parents=True, exist_ok=True)
    (root / "infrastructure" / ".env").write_text(text, encoding="utf-8")


def _adopted_ollama(root: Path) -> None:
    _infra_env(root, "VCT_OLLAMA_VOLUME_NAME=ollama\n")


def test_a_user_named_volume_under_the_other_runtime_does_not_switch_read_only(tmp_path):
    """J8 (a): the user adopted a podman volume named `ollama`; this process
    cannot find podman; docker answers and has an UNRELATED `ollama` volume.
    Before R10 the name alone was positive evidence → the boot wrapper and the
    resolver drove docker (and compose created the other volumes EMPTY there)."""
    root = _root(tmp_path, "podman")
    _adopted_ollama(root)
    m = LabelledMachine(installed={"docker"}, up={"docker"},
                        containers_={"docker": ["someone_elses_ollama"]},
                        volumes={"docker": ["ollama"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None, res.detail
    assert "holds none of VCO's containers or volumes" in res.detail
    assert containers.read_runtime_txt(root) == "podman"


def test_a_user_named_volume_does_not_re_record_a_working_runtime(tmp_path):
    """J8 (c): both runtimes answer, the recorded podman holds nothing of
    VCO's, docker holds only an unrelated volume that happens to carry the
    override name — the record stays."""
    root = _root(tmp_path, "podman")
    _adopted_ollama(root)
    m = LabelledMachine(installed={"podman", "docker"}, up={"podman", "docker"},
                        volumes={"podman": [], "docker": ["ollama"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True)
    assert res.outcome is rr.Outcome.KEPT, res.detail
    assert containers.read_runtime_txt(root) == "podman" and not res.entries


def test_a_user_named_volume_labelled_by_vcos_compose_project_is_evidence(tmp_path):
    """Corroborated: compose created that volume for VCO's own project under
    docker — it IS VCO's data there, so the stale record reconciles."""
    root = _root(tmp_path, "podman")
    _adopted_ollama(root)
    project = rr.vco_compose_project(root)
    m = LabelledMachine(installed={"docker"}, up={"docker"},
                        volumes={"docker": ["ollama"]},
                        labels={("docker", "ollama"): project})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "docker", res.detail
    assert ["docker", "volume", "inspect", "--format",
            '{{index .Labels "com.docker.compose.project"}}', "ollama"] in m.calls


def test_the_recorded_runtime_still_counts_its_adopted_volume(tmp_path):
    """The corroboration applies to the switch TARGET only: under the recorded
    runtime an adopted (unlabelled) volume still holds the data (R9 H6), which
    only ever keeps the record."""
    root = _root(tmp_path, "podman")
    _adopted_ollama(root)
    m = LabelledMachine(installed={"podman"}, up={"podman"}, volumes={"podman": ["ollama"]})
    assert rr.vco_data_under("podman", run=m.run, install_root=root, env={}) is True
    assert rr.vco_data_under("podman", run=m.run, install_root=root, env={},
                             corroborate_overrides=True) is False


# ---------------------------------------------------------------------------
# J2 — a bind-mounted data folder is VCO's data under the recorded runtime
# ---------------------------------------------------------------------------


def _bind_layout(root: Path, *, empty: bool = False) -> Path:
    folder = root.parent / "srv-weaviate"
    folder.mkdir()
    if not empty:
        (folder / "classifications.db").write_text("data", encoding="utf-8")
    _infra_env(root, f"VCT_WEAVIATE_DATA_SOURCE={folder}\nVCT_WEAVIATE_VOLUME_NAME=\n")
    return folder


@pytest.mark.parametrize("rewrite", [True, False], ids=["install", "read_only"])
def test_a_bind_mount_layout_keeps_the_record_against_a_leftover_volume(tmp_path, rewrite):
    """J2 (c): Weaviate's data was relocated to a folder, `compose down`
    removed the containers; docker still has a `vco_weaviate_data` from an
    earlier trial. Before R10: "podman holds none of VCO's data, docker does"
    → re-recorded (install) / driven (read-only) docker."""
    root = _root(tmp_path, "podman")
    folder = _bind_layout(root)
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                volumes={"podman": [], "docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=rewrite)
    assert res.outcome is rr.Outcome.KEPT and res.runtime == "podman", res.detail
    assert str(folder) in res.detail
    assert containers.read_runtime_txt(root) == "podman" and not res.entries


def test_an_empty_bind_folder_is_not_data(tmp_path):
    """Only a folder that holds something is evidence: an empty one leaves the
    (c) decision to the runtimes' listings, as before."""
    root = _root(tmp_path, "podman")
    _bind_layout(root, empty=True)
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                volumes={"podman": [], "docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "docker"


def test_a_bind_mount_layout_refuses_read_only_when_the_record_is_not_installed(tmp_path):
    root = _root(tmp_path, "podman")
    folder = _bind_layout(root)
    m = Machine(installed={"docker"}, up={"docker"},
                containers_={"docker": ["vco_weaviate"]}, volumes={"docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert res.detail == rr.unusable_detail(
        "podman", str(containers.runtime_txt_path(root)), "missing", "bind_data", bind=str(folder))
    assert not res.entries and containers.read_runtime_txt(root) == "podman"


def test_install_keeps_a_bind_mount_record_and_asks(tmp_path):
    root = _root(tmp_path, "podman")
    _bind_layout(root)
    m = Machine(installed={"docker"}, start_ok={"docker"},
                volumes={"docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, start_daemon=m.start, rewrite=True)
    assert res.outcome is rr.Outcome.UNUSABLE
    [entry] = res.entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert "python install.py --update --container docker" in entry.command_to_apply
    assert m.started == [], "nothing is started for a switch that will not happen"
    assert containers.read_runtime_txt(root) == "podman"


# docker answers and lists a LEFTOVER default-named Weaviate volume; logs argv.
_DOCKER_LEFTOVER = ('#!/usr/bin/env bash\necho "$*" >> "$HOME/docker.argv"\n'
                    'case "$1" in ps) echo someone_elses_ollama ;; volume) echo vco_weaviate_data ;;\n'
                    '  info) echo "Server: Docker Engine - Community" ;; esac\n'
                    'exit 0\n')


@pytest.mark.parametrize("launcher,wrapper", _SHELLS)
def test_the_boot_wrapper_never_switches_a_bind_mount_layout(tmp_path, launcher, wrapper):
    """J2 through the boot wrapper (read-only; it reconciles only when the
    recorded runtime is not usable): the record names podman, which this boot
    cannot find; podman's Weaviate data is a bind-mounted folder; docker
    answers with a leftover `vco_weaviate_data`. Before R10 that leftover was
    positive evidence and the wrapper composed under DOCKER (the same compose
    file bind-mounts the folder, so the data was served by the runtime the
    record did not name — and the next session's hooks collided with it). Now:
    exit 3, the refusal in the ledger, docker never asked to compose."""
    root, script = _clone(tmp_path, wrapper)
    containers.runtime_txt_path(root).write_text("podman\n", encoding="utf-8")
    _bind_layout(root)
    fake = _fake_bin(tmp_path, with_timeout=True)
    _stub(fake / "docker", _DOCKER_LEFTOVER)
    home = tmp_path / "home"
    home.mkdir()
    proc = _run_wrapper(launcher(script), _wrapper_env(tmp_path, fake), tmp_path)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    docker_log = (home / "docker.argv").read_text(encoding="utf-8") if (home / "docker.argv").exists() else ""
    assert "compose" not in docker_log, docker_log
    [entry] = DeferralReport.read(root).entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert containers.read_runtime_txt(root) == "podman"


# ---------------------------------------------------------------------------
# J4 — a re-emitted refusal keeps its first `detected_at` and its one title
# ---------------------------------------------------------------------------


def _sidecar(root: Path) -> Path:
    return root / ".claude" / "context" / "UPDATE_DEFERRED.json"


def _backdate(root: Path, when: str) -> None:
    data = json.loads(_sidecar(root).read_text(encoding="utf-8"))
    data["entries"][0]["detected_at"] = when
    _sidecar(root).write_text(json.dumps(data), encoding="utf-8")


def test_a_re_emitted_refusal_keeps_when_it_was_first_detected(tmp_path):
    root = _root(tmp_path, "docker")
    assert rr.record_boot_refusal(root, "pinned to docker", env={}, source="boot") is True
    _backdate(root, "2026-09-01T00:00:00Z")
    # The next session's hooks record the same condition (a different surface).
    assert rr.record_boot_refusal(root, "pinned to docker", env={}, source="session") is True
    [entry] = DeferralReport.read(root).entries
    assert entry.detected_at == "2026-09-01T00:00:00Z"
    assert entry.title == "Container runtime docker is not usable — containers were not started"
    assert entry.detected.startswith("A session-start hook started nothing")


def test_an_unchanged_refusal_does_not_rewrite_the_ledger(tmp_path):
    root = _root(tmp_path, "docker")
    assert rr.record_boot_refusal(root, "pinned to docker", env={}, source="session") is True
    _backdate(root, "2026-09-01T00:00:00Z")  # also re-serialises it: ANY rewrite shows
    before = _sidecar(root).read_bytes()
    assert rr.record_boot_refusal(root, "pinned to docker", env={}, source="session") is True
    assert _sidecar(root).read_bytes() == before, "the ledger changed with nothing new to say"


def test_a_different_pin_is_a_new_detection(tmp_path):
    root = _root(tmp_path, "docker")
    assert rr.record_boot_refusal(root, "pinned to docker", env={}) is True
    _backdate(root, "2026-09-01T00:00:00Z")
    assert rr.record_boot_refusal(root, "pinned to podman", env={"VCT_CONTAINER_RUNTIME": "podman"}) is True
    [entry] = DeferralReport.read(root).entries
    assert entry.detected_at != "2026-09-01T00:00:00Z"
    assert entry.dismiss_fields["runtime"] == "podman"
