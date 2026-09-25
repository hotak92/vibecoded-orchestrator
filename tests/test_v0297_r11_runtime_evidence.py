# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R11 — L1, L2, L3, L7: a bind-mounted data layout against a
stale runtime record, and the one home of VCO's compose project.

* L1 — probing a bind folder never raises: an unsearchable parent (EACCES from
  ``stat``) or an unlistable folder means "cannot tell", which is data (not
  provably empty). Before, ``Path.is_dir()`` re-raised EACCES out of
  ``reconcile()`` and took ``containers.resolve`` — every session hook, the
  boot wrapper, install.py — down with it.
* L2 — under a bind layout the OTHER runtime's CONTAINERS are evidence: running
  ones switch (the stack lives there), stopped ones are data under both; only a
  leftover VOLUME keeps R10 J2's "the folder is the data".
* L3 — every service a folder + the recorded runtime not installed: switching
  strands no named volume, so install re-records and read-only surfaces drive
  the other runtime.
* L7 — ``containers.own_compose_project`` is the one reader of VCO's compose
  project.

The fixture sections run by BOTH languages are
``tests/fixtures/runtime_data_evidence_cases.json`` ``kind`` / ``bind_verdict``
/ ``all_bind`` / ``bind_probe`` (Rust: ``runtime_evidence.rs`` tests) and the
parity fixture's ``runtime_txt_stale_record_*`` rows. Every runtime is a FAKE;
every install root is a tmp directory.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from tests.test_v0297_r9_runtime_locations import Machine, _root
from vco_lib import containers
from vco_lib import runtime_reconcile as rr

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "runtime_data_evidence_cases.json")
                     .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The shared fixture (the Rust suite runs the same rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", FIXTURE["kind"], ids=[c["name"] for c in FIXTURE["kind"]])
def test_data_kind_matches_the_shared_fixture(case: dict):
    names = rr.volume_names_from(case["env_file"], case["env"])
    got = rr.data_kind(set(case["containers"]), set(case["running"]), set(case["volumes"]), names,
                       own_project=case["own_project"], label_of=case["labels"].get,
                       corroborate_overrides=True)
    assert got == case["expect"]


@pytest.mark.parametrize("case", FIXTURE["bind_verdict"],
                         ids=[c["name"] for c in FIXTURE["bind_verdict"]])
def test_bind_verdict_matches_the_shared_fixture(case: dict):
    assert rr.bind_verdict(case["other"], pinned_missing=case["pinned_missing"],
                           all_bind=case["all_bind"],
                           here_running=case["here_running"]) == case["expect"]


@pytest.mark.parametrize("case", FIXTURE["all_bind"], ids=[c["name"] for c in FIXTURE["all_bind"]])
def test_all_services_bind_matches_the_shared_fixture(case: dict):
    assert rr.all_services_bind(case["env_file"], case["env"]) is case["expect"]


def _restore_modes(paths: list[Path]) -> None:
    for p in paths:
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass


def bind_probe_setup(tmp_path: Path, setup: str, chmodded: list[Path]) -> Path:
    """The filesystem shape a ``bind_probe`` row names; ``pytest.skip`` when
    this process cannot produce it (root ignores permissions; Windows has no
    POSIX modes)."""
    parent = tmp_path / "parent"
    parent.mkdir()
    folder = parent / "data"
    if setup == "missing":
        return folder
    if setup == "file":
        folder.write_text("x", encoding="utf-8")
        return folder
    folder.mkdir()
    if setup == "empty":
        return folder
    (folder / "blob").write_text("x", encoding="utf-8")
    if setup == "non_empty":
        return folder
    if os.name != "posix":
        pytest.skip("POSIX permission bits")
    target = parent if setup == "unsearchable_parent" else folder
    os.chmod(target, 0)
    chmodded.append(target)
    try:
        if setup == "unsearchable_parent":
            os.stat(folder)
        else:
            os.listdir(folder)
    except PermissionError:
        return folder
    pytest.skip("this process bypasses permission bits (root)")
    raise AssertionError("unreachable")


@pytest.mark.parametrize("case", FIXTURE["bind_probe"], ids=[c["name"] for c in FIXTURE["bind_probe"]])
def test_bind_probe_matches_the_shared_fixture(case: dict, tmp_path: Path):
    """L1: every shape answers — never an exception."""
    chmodded: list[Path] = []
    try:
        folder = bind_probe_setup(tmp_path, case["setup"], chmodded)
        assert rr.bind_folder_holds_data(folder) is case["expect"]
    finally:
        _restore_modes(chmodded)


# ---------------------------------------------------------------------------
# L1 — an unsearchable bind folder is "cannot tell", never an exception
# ---------------------------------------------------------------------------


def _bind_env(root: Path, **sources: Path) -> None:
    keys = {"weaviate": "VCT_WEAVIATE_DATA_SOURCE", "ollama": "VCT_OLLAMA_DATA_SOURCE",
            "code_embed": "VCT_CODE_EMBED_CACHE_SOURCE"}
    (root / "infrastructure").mkdir(parents=True, exist_ok=True)
    (root / "infrastructure" / ".env").write_text(
        "".join(f"{keys[k]}={v}\n" for k, v in sources.items()), encoding="utf-8")


def _deny_stat_under(monkeypatch, folder: Path) -> None:
    """``stat`` of ``folder`` (or anything under it) raises EACCES, as it does
    when a parent directory is not searchable by this user."""
    real_stat = os.stat
    prefix = str(folder)

    def stat_(path, *a, **kw):
        if str(path).startswith(prefix):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "stat", stat_)


def test_an_eacces_bind_probe_never_raises_out_of_reconcile(tmp_path, monkeypatch):
    """L1, simulated EACCES (runs as root and on every OS): the record names
    podman, which is not installed; docker answers with a leftover volume.
    The folder cannot be probed, so it counts as data — the record is refused
    with the bind reason, read-only, and nothing raises."""
    root = _root(tmp_path, "podman")
    folder = tmp_path / "locked" / "weaviate"
    _bind_env(root, weaviate=folder)
    _deny_stat_under(monkeypatch, folder)
    m = Machine(installed={"docker"}, up={"docker"}, volumes={"docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and res.decline == "bind_data", res.detail
    assert str(folder) in res.detail


def test_an_eacces_bind_probe_keeps_the_resolver_answering(tmp_path, monkeypatch):
    """L1 through ``containers.resolve`` (what every session hook runs): a
    refused pin — exit code 3 — never a traceback (exit 1)."""
    root = _root(tmp_path, "podman")
    folder = tmp_path / "locked" / "weaviate"
    _bind_env(root, weaviate=folder)
    _deny_stat_under(monkeypatch, folder)
    m = Machine(installed={"docker"}, up={"docker"}, volumes={"docker": ["vco_weaviate_data"]})
    res = containers.resolve(env={}, which=m.which, run=m.run, warn=lambda _m: None,
                             install_root=root, probe_compose=False)
    assert res.state is containers.RuntimeState.ABSENT, res.reason
    assert containers.RESOLVE_EXIT_CODES[res.state] == 3
    assert "(not switched: " in res.reason and str(folder) in res.reason
    # The folder was JUDGED (data: not provably empty), not an error swallowed.
    assert "reconcile failed" not in res.reason
    assert f"bind-mounted folder {folder}" in res.reason


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_real_unsearchable_parent_keeps_install_and_read_only_answering(tmp_path):
    """L1 on a real filesystem: the bind folder's parent is mode 0."""
    root = _root(tmp_path, "podman")
    locked = tmp_path / "locked"
    folder = locked / "weaviate"
    folder.mkdir(parents=True)
    (folder / "classifications.db").write_text("x", encoding="utf-8")
    _bind_env(root, weaviate=folder)
    os.chmod(locked, 0)
    try:
        try:
            os.stat(folder)
            pytest.skip("this process bypasses permission bits (root)")
        except PermissionError:
            pass
        m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                    volumes={"podman": [], "docker": ["vco_weaviate_data"]})
        res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True)
        assert res.outcome is rr.Outcome.KEPT and res.runtime == "podman", res.detail
        m2 = Machine(installed={"docker"}, up={"docker"}, volumes={"docker": ["vco_weaviate_data"]})
        res2 = rr.reconcile(root, env={}, which=m2.which, run=m2.run, rewrite=False)
        assert res2.outcome is rr.Outcome.UNUSABLE and res2.decline == "bind_data"
    finally:
        _restore_modes([locked])


# ---------------------------------------------------------------------------
# L2 — the other runtime's CONTAINERS outrank the bind-folder rule
# ---------------------------------------------------------------------------


def _weaviate_folder(tmp_path: Path, root: Path) -> Path:
    folder = tmp_path / "srv-weaviate"
    folder.mkdir()
    (folder / "classifications.db").write_text("data", encoding="utf-8")
    _bind_env(root, weaviate=folder)
    return folder


@pytest.mark.parametrize("rewrite", [False, True], ids=["read_only", "install"])
def test_a_stack_running_under_the_other_runtime_on_the_folder_is_followed(tmp_path, rewrite):
    """L2 (i), case (a): podman recorded but not installed; docker is RUNNING
    VCO's containers on the bind folder. Before: every surface refused while
    docker served the data. Now read-only drives docker and install
    re-records it (informational)."""
    root = _root(tmp_path, "podman")
    folder = _weaviate_folder(tmp_path, root)
    m = Machine(installed={"docker"}, up={"docker"},
                containers_={"docker": ["vco_weaviate", "vco_ollama"]},
                running={"docker": ["vco_weaviate", "vco_ollama"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=rewrite)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "docker", res.detail
    assert "running VCO's containers" in res.detail and str(folder) in res.detail
    if rewrite:
        assert containers.read_runtime_txt(root) == "docker"
        assert [e.condition_id for e in res.entries] == [rr.CID_RECORD_RECONCILED]
        assert res.entries[0].severity == "info"
    else:
        assert containers.read_runtime_txt(root) == "podman" and not res.entries


def test_stopped_containers_beside_the_folder_are_refused_read_only(tmp_path):
    """L2 (ii), case (a), read-only: docker holds VCO containers that are NOT
    running; either runtime could serve the folder — nothing is driven, and
    the refusal says why in the shared wording."""
    root = _root(tmp_path, "podman")
    folder = _weaviate_folder(tmp_path, root)
    m = Machine(installed={"docker"}, up={"docker"}, containers_={"docker": ["vco_weaviate"]},
                volumes={"docker": ["vco_weaviate_data"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert res.decline == "data_under_both"
    assert res.detail == rr.unusable_detail(
        "podman", str(containers.runtime_txt_path(root)), "missing", "data_under_both",
        bind=str(folder))
    assert not res.entries and containers.read_runtime_txt(root) == "podman"


def test_install_records_data_under_both_for_stopped_containers_beside_the_folder(tmp_path):
    """L2 (ii), case (a), install: an action_required
    ``container_runtime_data_under_both`` entry, the record kept, and the rest
    of the run goes on without containers."""
    root = _root(tmp_path, "podman")
    folder = _weaviate_folder(tmp_path, root)
    m = Machine(installed={"docker"}, up={"docker"}, containers_={"docker": ["vco_weaviate"]})
    args = argparse.Namespace(no_containers=False, container=None)

    class _Report:
        def __init__(self):
            self.entries = []

        def add_entry(self, e):
            self.entries.append(e)

    report = _Report()
    res = rr.apply_at_install(root, args, report, start_daemon=m.start,
                              log_event=lambda *a, **k: None, out=lambda _s: None,
                              env={}, which=m.which, run=m.run)
    assert res is not None and res.outcome is rr.Outcome.UNUSABLE
    [entry] = report.entries
    assert entry.condition_id == rr.CID_DATA_UNDER_BOTH
    assert str(folder) in entry.detected and "started nothing" in entry.detected
    assert "--container podman" in entry.command_to_apply
    assert "--container docker" in entry.command_to_apply
    assert args.no_containers is True
    note = rr.containers_skipped_note(args)
    assert "the two choices" in note and "clears by itself" not in note
    assert containers.read_runtime_txt(root) == "podman"


def test_install_follows_a_stack_running_under_the_other_runtime_when_both_answer(tmp_path):
    """L2 (i), case (c) — the review's scenario: podman recorded, installed,
    up, holds nothing; docker RUNS VCO's containers on the folder. Before:
    KEPT podman, so the next session composed a second stack onto the same
    folder. Now install re-records docker."""
    root = _root(tmp_path, "podman")
    _weaviate_folder(tmp_path, root)
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                containers_={"podman": [], "docker": ["vco_weaviate"]},
                running={"docker": ["vco_weaviate"]}, volumes={"podman": [], "docker": []})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "docker", res.detail
    assert containers.read_runtime_txt(root) == "docker"
    assert [e.condition_id for e in res.entries] == [rr.CID_RECORD_RECONCILED]


@pytest.mark.parametrize("shape", ["stopped_there", "running_both"])
def test_install_asks_when_both_runtimes_hold_vco_containers_beside_the_folder(tmp_path, shape):
    """L2 (ii), case (c): stopped VCO containers under docker, or VCO
    containers running under BOTH — install keeps the recorded podman and
    writes ``container_runtime_data_under_both``."""
    root = _root(tmp_path, "podman")
    _weaviate_folder(tmp_path, root)
    running = {"docker": []} if shape == "stopped_there" else {
        "docker": ["vco_weaviate"], "podman": ["vco_ollama"]}
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                containers_={"podman": ["vco_ollama"], "docker": ["vco_weaviate"]},
                running=running)
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True)
    assert res.outcome is rr.Outcome.DATA_UNDER_BOTH and res.runtime == "podman", res.detail
    assert [e.condition_id for e in res.entries] == [rr.CID_DATA_UNDER_BOTH]
    assert "VCO kept using podman" in res.entries[0].detected
    assert containers.read_runtime_txt(root) == "podman"


def test_the_both_probe_follows_the_bind_arm(tmp_path, monkeypatch):
    """The clear probe of ``container_runtime_data_under_both`` answers the
    way the reconcile decides: True while docker holds stopped VCO containers
    beside the folder, False once only a leftover volume is left (R10 J2 used
    to answer False for ANY bind layout)."""
    for key in rr.DATA_SOURCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = _root(tmp_path, "podman")
    _weaviate_folder(tmp_path, root)
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                containers_={"podman": [], "docker": ["vco_weaviate"]})
    entry = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True).entries[0]
    assert rr.data_still_under_both(entry, which=m.which, run=m.run) is True
    m.containers["docker"] = []
    m.volumes["docker"] = ["vco_weaviate_data"]
    assert rr.data_still_under_both(entry, which=m.which, run=m.run) is False


# ---------------------------------------------------------------------------
# L3 — every service a folder: a switch strands nothing
# ---------------------------------------------------------------------------


def _all_folders(tmp_path: Path, root: Path, *, create: bool = True) -> None:
    folders = {k: tmp_path / f"srv-{k}" for k in ("weaviate", "ollama", "code_embed")}
    if create:
        for f in folders.values():
            f.mkdir()
            (f / "blob").write_text("x", encoding="utf-8")
    _bind_env(root, **folders)


@pytest.mark.parametrize("create", [True, False], ids=["folders_hold_data", "folders_not_there"])
@pytest.mark.parametrize("rewrite", [True, False], ids=["install", "read_only"])
def test_every_service_in_a_folder_switches_a_missing_record(tmp_path, rewrite, create):
    """L3: podman recorded, not installed; docker answers with nothing of
    VCO's; every service (the code_embed cache included) is a folder. Before:
    install refused and handed the user `--container docker`. Now install
    re-records docker (informational) and read-only drives it."""
    root = _root(tmp_path, "podman")
    _all_folders(tmp_path, root, create=create)
    m = Machine(installed={"docker"}, up={"docker"})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=rewrite)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "docker", res.detail
    assert "no named volume behind" in res.detail
    if rewrite:
        assert containers.read_runtime_txt(root) == "docker"
        assert [e.condition_id for e in res.entries] == [rr.CID_RECORD_RECONCILED]
    else:
        assert containers.read_runtime_txt(root) == "podman"


def test_one_service_on_a_named_volume_keeps_the_bind_rule(tmp_path):
    """L3's boundary: Weaviate and Ollama are folders, the code_embed cache is
    still a named volume — J2/J8 apply as before (refused, action_required)."""
    root = _root(tmp_path, "podman")
    folders = {k: tmp_path / f"srv-{k}" for k in ("weaviate", "ollama")}
    for f in folders.values():
        f.mkdir()
        (f / "blob").write_text("x", encoding="utf-8")
    _bind_env(root, **folders)
    m = Machine(installed={"docker"}, up={"docker"}, volumes={"docker": ["vco_code_embed_cache"]})
    res = rr.reconcile(root, env={}, which=m.which, run=m.run, rewrite=True)
    assert res.outcome is rr.Outcome.UNUSABLE and res.decline == "bind_data", res.detail
    assert [e.condition_id for e in res.entries] == [rr.CID_UNUSABLE]
    assert containers.read_runtime_txt(root) == "podman"


# ---------------------------------------------------------------------------
# L7 — one reader of VCO's compose project
# ---------------------------------------------------------------------------


def test_own_compose_project_reads_the_installer_compose(tmp_path):
    root = tmp_path / "clone"
    (root / "infrastructure").mkdir(parents=True)
    assert containers.own_compose_project(root) == "infrastructure"  # no file: the dir name
    (root / "infrastructure" / "docker-compose.yml").write_text("name: vco\nservices: {}\n",
                                                                encoding="utf-8")
    assert containers.own_compose_project(root) == "vco"
    assert containers.own_compose_project(None) == ""
    assert rr.vco_compose_project(root) == "vco"


def test_the_guard_and_the_reconcile_ask_the_one_reader(tmp_path, monkeypatch):
    """Two of the former inline copies, proved by behaviour (the ONE reader is
    replaced and the caller sees its answer), not by a source scan. The other
    three (service_adoption ×2, service_lifecycle, service_reconcile) are
    exercised by their own suites."""
    from vco_lib import install_services_guard

    seen: list[str] = []

    def fake_of(compose_file):
        seen.append(str(compose_file))
        return "from-the-one-reader"

    monkeypatch.setattr(containers, "compose_project_of", fake_of)
    root = tmp_path / "clone"
    (root / "infrastructure").mkdir(parents=True)
    assert containers.own_compose_project(root) == "from-the-one-reader"
    assert rr.vco_compose_project(root) == "from-the-one-reader"
    # The guard reads the compose file it is handed.
    monkeypatch.setattr(containers, "find_existing_container", lambda *_a, **_k: None)
    install_services_guard.foreign_owned_services(
        ["weaviate"], "podman", root / "infrastructure", root / "infrastructure" / "x.yml")
    assert seen[-1] == str(root / "infrastructure" / "x.yml")
