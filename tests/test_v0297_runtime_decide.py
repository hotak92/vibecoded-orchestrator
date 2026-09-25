# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``decide`` contract (v0.2.97 R12 SSOT-1): ONE verdict, ONE fixture.

``tests/fixtures/runtime_decide_cases.json`` drives this file (Python:
:func:`vco_lib.runtime_reconcile.decide`) and is the contract lane SSOT-2
points the Rust surfaces at — the fixture's ``_comment`` spells out how a
Rust test reproduces each case with fake runtime stub scripts and a temp
install root, applying the same ``expect`` subset to the JSON
``decide --json`` prints.

Beyond the fixture rows, this file pins the R12 Python findings the verdict
embeds: M7 (one engine under two names), M3 (refusal text matches what was
probed), M8 (the ONE ``VCT_*_DATA_SOURCE`` precedence resolver), M1's
fixture lists, and M5's dead-parameter removal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from tests.common.child_env import child_env
from vco_lib import containers
from vco_lib import install_services_guard
from vco_lib import runtime_reconcile as rr

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "runtime_decide_cases.json").read_text(encoding="utf-8"))
EVIDENCE = json.loads(
    (Path(__file__).parent / "fixtures" / "runtime_data_evidence_cases.json")
    .read_text(encoding="utf-8"))

#: The schema-1 key set the module docstring documents — every verdict
#: carries all of them (values may be ``None``).
SCHEMA_KEYS = {
    "schema", "state", "runtime", "compose", "compose_form", "binary_path",
    "search_path", "installed", "requested", "requested_via",
    "requested_installed", "alternative_usable", "record_reconciled",
    "outcome", "not_switched_key", "not_switched", "same_engine", "refused",
    "refusal", "reason",
}


# ---------------------------------------------------------------------------
# The fake machine every contract case runs against
# ---------------------------------------------------------------------------


def _cp(argv, rc, out=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout=out, stderr="")


class FakeMachine:
    """Answers the probe grammar the fixture's ``runtimes`` map describes
    (see the fixture ``_comment`` for the argv grammar and the Rust
    reproduction recipe)."""

    def __init__(self, runtimes: dict):
        self.runtimes = runtimes
        self.calls: list[list[str]] = []

    def which(self, name: str, path: Optional[str] = None) -> Optional[str]:
        spec = self.runtimes.get(name)
        if spec is not None and spec.get("on_path", True):
            return f"/fake/{name}"
        if name.endswith("-compose"):
            rt = name[: -len("-compose")]
            if self.runtimes.get(rt, {}).get("standalone"):
                return f"/fake/{name}"
        return None

    def run(self, argv, **_kw):
        self.calls.append(list(argv))
        rt = argv[0]
        spec = self.runtimes.get(rt)
        if spec is None or not spec.get("on_path", True):
            return _cp(argv, 1)
        sub = argv[1:]
        up = spec.get("daemon_ok", True)
        if sub[:2] == ["compose", "version"]:
            return _cp(argv, 0 if spec.get("compose_subcommand", True) else 1)
        if sub[:1] == ["version"]:
            if not spec.get("version_ok", True):
                return _cp(argv, 1)
            return _cp(argv, 0, spec.get("version_out") or f"{rt} version 5.0.0\n")
        if sub[:1] == ["info"]:
            return _cp(argv, 0 if up else 1)
        if sub[:3] == ["ps", "-a", "--no-trunc"]:
            return _cp(argv, 0 if up else 1,
                       "".join(i + "\n" for i in spec.get("ps_a_ids", [])))
        if sub[:2] == ["ps", "-a"]:
            names = spec.get("ps_a", ["someone_elses_ollama"])
            return _cp(argv, 0 if up else 1, "".join(n + "\n" for n in names))
        if sub[:2] == ["ps", "--format"]:
            return _cp(argv, 0 if up else 1,
                       "".join(n + "\n" for n in spec.get("ps", [])))
        if sub[:2] == ["volume", "ls"]:
            vols = spec.get("volume_ls", ["someone_elses_data"])
            return _cp(argv, 0 if up else 1, "".join(v + "\n" for v in vols))
        return _cp(argv, 0)


def _root(tmp_path: Path, case: dict, data_dir: Path) -> Path:
    """The case's install root: record, confirmed, ``infrastructure/.env``
    (with ``{DATA}`` resolved) and the data folders."""
    root = tmp_path / "install-root"
    (root / "state" / "install").mkdir(parents=True)
    if case.get("record"):
        containers.runtime_txt_path(root).write_text(case["record"] + "\n", encoding="utf-8")
    if case.get("confirmed"):
        (root / rr.CONFIRMED_REL).write_text(case["confirmed"] + "\n", encoding="utf-8")
    if case.get("infra_env"):
        (root / "infrastructure").mkdir()
        (root / "infrastructure" / ".env").write_text(
            case["infra_env"].replace("{DATA}", str(data_dir)), encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    for folder in case.get("folders", []):
        p = Path(folder["path"].replace("{DATA}", str(data_dir)))
        p.mkdir(parents=True, exist_ok=True)
        for entry in folder.get("entries", []):
            (p / entry).write_text("x", encoding="utf-8")
    return root


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_decide_matches_the_contract_fixture(case: dict, tmp_path: Path):
    machine = FakeMachine(case["runtimes"])
    env = {"VCT_CONTAINER_RUNTIME": case["env_pin"]} if case.get("env_pin") else {}
    root = _root(tmp_path, case, tmp_path / "data")
    verdict = rr.decide(root, mode=case.get("mode", "read-only"),
                        purpose=case.get("purpose", "infra"), env=env,
                        which=machine.which, run=machine.run, home=tmp_path)
    assert verdict["schema"] == 1 and set(SCHEMA_KEYS) <= set(verdict), verdict
    if case.get("mode", "read-only") == "read-only" and case.get("record"):
        # READ-ONLY is a promise the fixture pins per case: the record never
        # moved (the install-mode cases are the ones that may re-record).
        assert containers.read_runtime_txt(root) == case["record"]
    exp = case["expect"]
    for key, value in exp.items():
        if key.endswith("_not_contains"):
            field = key[: -len("_not_contains")]
            for needle in value:
                assert needle not in verdict[field], (case["name"], key, verdict)
        elif key.endswith("_contains"):
            for needle in value:
                assert needle in verdict[key[: -len("_contains")]], (case["name"], key, verdict)
        else:
            assert verdict[key] == value, (case["name"], key, verdict[key], value)
    # The refusal contract: `refusal` is the full user-facing text whenever
    # the verdict is refused, `reason` always set.
    assert verdict["refused"] == (verdict["state"] != "resolved")
    assert verdict["reason"]
    if verdict["refused"]:
        assert verdict["refusal"] == verdict["reason"]


# ---------------------------------------------------------------------------
# R12 M7 — one engine, two names
# ---------------------------------------------------------------------------


def _probe_run(*, docker_version_out: Optional[str] = None, ids: Optional[dict] = None):
    ids = ids or {}

    def run(argv, **_kw):
        rt, *sub = argv
        if sub[:1] == ["version"]:
            out = docker_version_out if rt == "docker" and docker_version_out else f"{rt} version 5.0\n"
            return _cp(argv, 0, out)
        if sub[:3] == ["ps", "-a", "--no-trunc"]:
            return _cp(argv, 0, "".join(i + "\n" for i in ids.get(rt, [])))
        return _cp(argv, 0)

    return run


def test_m7_a_version_output_naming_the_other_engine_is_one_engine():
    run = _probe_run(docker_version_out="docker version 24.0 (podman 5.4 shim)\n")
    assert rr.runtimes_are_one_engine("podman", "docker", run=run) is True


def test_m7_shared_full_container_ids_are_one_engine():
    run = _probe_run(ids={"podman": ["a1b2c3d4e5f6a1b2c3d4e5f6"],
                          "docker": ["a1b2c3d4e5f6a1b2c3d4e5f6"]})
    assert rr.runtimes_are_one_engine("podman", "docker", run=run) is True


def test_m7_names_are_not_container_ids():
    """A listing that echoes NAMES for the ID query is not evidence — the
    intersection leg only counts hex-shaped full IDs (the fake `Machine`s
    elsewhere in the suite answer every `ps` with names)."""
    run = _probe_run(ids={"podman": ["vco_weaviate"], "docker": ["vco_weaviate"]})
    assert rr.runtimes_are_one_engine("podman", "docker", run=run) is None


def _both_runtimes_hold_vco(*, docker_version_out: Optional[str] = None,
                            docker_running: bool = False):
    specs = {
        "podman": {"ps_a": ["vco_ollama"], "ps": ["vco_ollama"]},
        "docker": {"ps_a": ["vco_weaviate"], "ps": ["vco_weaviate"] if docker_running else []},
    }

    def which(name: str, path: Optional[str] = None) -> Optional[str]:
        return f"/fake/{name}"

    def run(argv, **_kw):
        rt, *sub = argv
        spec = specs[rt]
        if sub[:1] == ["version"]:
            out = docker_version_out if rt == "docker" and docker_version_out else f"{rt} version 5.0\n"
            return _cp(argv, 0, out)
        if sub[:3] == ["ps", "-a", "--no-trunc"]:
            return _cp(argv, 0, "")
        if sub[:2] == ["ps", "-a"]:
            return _cp(argv, 0, "".join(n + "\n" for n in spec["ps_a"]))
        if sub[:2] == ["ps", "--format"]:
            return _cp(argv, 0, "".join(n + "\n" for n in spec["ps"]))
        if sub[:2] == ["volume", "ls"]:
            return _cp(argv, 0, "")
        return _cp(argv, 0)

    return which, run


def _bare_root(tmp_path: Path, recorded: str) -> Path:
    root = tmp_path / "clone"
    (root / "state" / "install").mkdir(parents=True)
    containers.runtime_txt_path(root).write_text(recorded + "\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("shim", [True, False], ids=["one_engine", "two_engines"])
def test_m7_one_engine_collapses_data_under_both(tmp_path, shim):
    """Case (c), non-bind: both runtimes hold stopped VCO containers. With a
    docker→podman shim that is ONE engine's listing read twice — KEPT,
    ``same_engine`` — and never a ``container_runtime_data_under_both``
    entry; two real engines stay DATA_UNDER_BOTH."""
    which, run = _both_runtimes_hold_vco(
        docker_version_out="docker version 24.0 (emulates the docker CLI using podman)\n"
        if shim else None)
    root = _bare_root(tmp_path, "podman")
    res = rr.reconcile(root, env={}, which=which, run=run, rewrite=True)
    if shim:
        assert res.outcome is rr.Outcome.KEPT and res.same_engine is True, res.detail
        assert not res.entries
        assert "one engine" in res.detail
    else:
        assert res.outcome is rr.Outcome.DATA_UNDER_BOTH and res.same_engine is False, res.detail
        assert [e.condition_id for e in res.entries] == [rr.CID_DATA_UNDER_BOTH]


def test_m2_both_entry_wording_matches_what_was_probed(tmp_path):
    """The DATA_UNDER_BOTH entry never claims more than the probes found:
    running containers say ``running`` (with ``as well`` only when the
    recorded runtime runs VCO's too), and the not-installed shape's remedy
    leads with reinstalling the recorded runtime."""
    which, run = _both_runtimes_hold_vco(docker_running=True)
    root = _bare_root(tmp_path, "podman")
    res = rr.reconcile(root, env={}, which=which, run=run, rewrite=True)
    assert res.outcome is rr.Outcome.DATA_UNDER_BOTH
    [entry] = res.entries
    assert "docker is running VCO containers as well" in entry.detected
    assert "or volumes" not in entry.detected
    assert entry.command_to_apply.startswith("# Keep podman")


def test_m2_kept_false_remedy_leads_with_reinstall(tmp_path):
    root = _bare_root(tmp_path, "docker")
    folder = tmp_path / "srv-w"
    folder.mkdir()
    (folder / "classifications.db").write_text("x", encoding="utf-8")
    (root / "infrastructure").mkdir()
    (root / "infrastructure" / ".env").write_text(
        f"VCT_WEAVIATE_DATA_SOURCE={folder}\nVCT_WEAVIATE_VOLUME_NAME=\n", encoding="utf-8")

    def which(name: str, path: Optional[str] = None) -> Optional[str]:
        return None if name == "docker" else f"/fake/{name}"

    def run(argv, **_kw):
        rt, *sub = argv
        if sub[:1] == ["version"]:
            return _cp(argv, 0, f"{rt} version 5.0\n")
        if sub[:3] == ["ps", "-a", "--no-trunc"]:
            return _cp(argv, 0, "")
        if sub[:2] == ["ps", "-a"]:
            # podman holds VCO containers that are NOT running (case (a),
            # L2 (ii): either runtime could serve the folder).
            return _cp(argv, 0, "vco_weaviate\n" if rt == "podman" else "someone_elses\n")
        if sub[:2] == ["ps", "--format"]:
            return _cp(argv, 0, "")
        if sub[:2] == ["volume", "ls"]:
            return _cp(argv, 0, "")
        return _cp(argv, 0)

    res = rr.reconcile(root, env={}, which=which, run=run, rewrite=True)
    assert res.outcome is rr.Outcome.UNUSABLE and res.decline == "data_under_both", res.detail
    [entry] = res.entries
    assert entry.command_to_apply.startswith("# Reinstall docker, then keep it:")
    assert "not running there" in entry.detected


# ---------------------------------------------------------------------------
# R12 M3 — the refusal names what was actually probed
# ---------------------------------------------------------------------------


def test_m3_the_switch_target_that_fails_its_probe_is_named(tmp_path):
    """The record reconcile switched the order to docker (podman missing,
    docker running VCO's containers) and docker's probe THEN failed: the
    refusal names driving DOCKER, never a pin refusal claiming podman was
    the runtime that did not answer."""
    root = _bare_root(tmp_path, "podman")
    info_left = {"n": 1}  # the reconcile's `info` succeeds; the probe's fails

    def which(name: str, path: Optional[str] = None) -> Optional[str]:
        return None if name == "podman" else f"/fake/{name}"

    def run(argv, **_kw):
        rt, *sub = argv
        if sub[:1] == ["info"]:
            info_left["n"] -= 1
            return _cp(argv, 0 if info_left["n"] >= 0 else 1)
        if sub[:2] == ["ps", "-a"]:
            return _cp(argv, 0, "vco_weaviate\n")
        if sub[:2] == ["ps", "--format"]:
            return _cp(argv, 0, "vco_weaviate\n")
        if sub[:2] == ["volume", "ls"]:
            return _cp(argv, 0, "vco_weaviate_data\n")
        return _cp(argv, 0)

    res = containers.resolve(env={}, which=which, run=run, warn=lambda _m: None,
                             home=tmp_path, install_root=root)
    assert res.state is containers.RuntimeState.ABSENT, res.reason
    assert "driving docker failed" in res.reason, res.reason
    assert "`docker info` failed" in res.reason, res.reason
    assert "re-record podman" in res.reason, res.reason
    assert "VCO will NOT drive it" not in res.reason  # not the generic pin refusal


@pytest.mark.parametrize("bind", [True, False], ids=["bind_decline", "no_bind_decline"])
def test_m3_a_bind_decline_never_claims_separate_named_volumes(tmp_path, monkeypatch, bind):
    """A refusal over a BIND layout explains itself with the folder (only
    the user knows which runtime serves it); the named-volumes rationale
    stays on the declines that actually rest on it."""
    for key in rr.DATA_SOURCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = _bare_root(tmp_path, "docker")
    if bind:
        folder = tmp_path / "srv-w"
        folder.mkdir()
        (folder / "classifications.db").write_text("x", encoding="utf-8")
        (root / "infrastructure").mkdir()
        (root / "infrastructure" / ".env").write_text(
            f"VCT_WEAVIATE_DATA_SOURCE={folder}\nVCT_WEAVIATE_VOLUME_NAME=\n", encoding="utf-8")

    def which(name: str, path: Optional[str] = None) -> Optional[str]:
        return None if name == "docker" else f"/fake/{name}"

    def run(argv, **_kw):
        rt, *sub = argv
        if rt == "docker":
            return _cp(argv, 1)
        if sub[:2] in (["ps", "-a"], ["ps", "--format"], ["volume", "ls"]):
            return _cp(argv, 0, "")
        return _cp(argv, 0)

    res = containers.resolve(env={}, which=which, run=run, warn=lambda _m: None,
                             home=tmp_path, install_root=root)
    assert res.state is containers.RuntimeState.ABSENT, res.reason
    if bind:
        assert "SEPARATE named volumes" not in res.reason, res.reason
        assert "bind-mounted folder" in res.reason, res.reason
    else:
        assert "SEPARATE named volumes" in res.reason, res.reason


def test_m3_the_unusable_ledger_row_matches_the_bind_decline(tmp_path, monkeypatch):
    """``container_runtime_unusable``'s why_deferred carries the same
    truth: a bind decline never claims the EMPTY-knowledge-graph outcome
    the named-volume rule would produce."""
    for key in rr.DATA_SOURCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = _bare_root(tmp_path, "docker")
    folder = tmp_path / "srv-w"
    folder.mkdir()
    (folder / "classifications.db").write_text("x", encoding="utf-8")
    (root / "infrastructure").mkdir()
    (root / "infrastructure" / ".env").write_text(
        f"VCT_WEAVIATE_DATA_SOURCE={folder}\nVCT_WEAVIATE_VOLUME_NAME=\n", encoding="utf-8")

    def which(name: str, path: Optional[str] = None) -> Optional[str]:
        return None if name == "docker" else f"/fake/{name}"

    def run(argv, **_kw):
        rt, *sub = argv
        if rt == "docker":
            return _cp(argv, 1)
        if sub[:2] in (["ps", "-a"], ["ps", "--format"], ["volume", "ls"]):
            return _cp(argv, 0, "")
        return _cp(argv, 0)

    res = rr.reconcile(root, env={}, which=which, run=run, rewrite=True)
    assert res.outcome is rr.Outcome.UNUSABLE and res.decline == "bind_data", res.detail
    [entry] = res.entries
    assert "SEPARATE named volumes" not in entry.why_deferred
    assert "bind-mounted folder" in entry.why_deferred


# ---------------------------------------------------------------------------
# R12 M8 / M1 — the fixture's Python-only semantics lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", EVIDENCE["bind_verdict_positive_only"],
                         ids=[r["name"] for r in EVIDENCE["bind_verdict_positive_only"]])
def test_bind_verdict_read_only_rows_match_the_shared_fixture(row: dict):
    got = rr.bind_verdict(
        row["other"], pinned_missing=row["pinned_missing"], all_bind=row["all_bind"],
        here_running=row["here_running"], positive_only=row["positive_only"])
    assert got == row["expect"], row["name"]


@pytest.mark.parametrize("row", EVIDENCE["bind_sources_effective"],
                         ids=[r["name"] for r in EVIDENCE["bind_sources_effective"]])
def test_bind_sources_read_the_one_precedence_resolver(row: dict):
    assert rr.bind_sources_from(row["env_file"], row["env"]) == tuple(row["expect"]), row["name"]
    assert rr.all_services_bind(row["env_file"], row["env"]) is row["expect_all_bind"], row["name"]
    effective = rr.effective_data_sources(row["env_file"], row["env"])
    assert all(k in rr.DATA_SOURCE_KEYS for k in effective), row["name"]


# ---------------------------------------------------------------------------
# R12 M5 — the guard probe's dead parameter is gone
# ---------------------------------------------------------------------------


def test_m5_foreign_owned_services_takes_no_infra_dir(tmp_path, monkeypatch):
    """The probe reads the identity off the container and the project off
    the compose FILE; ``infra_dir`` was never used (R12 M5)."""
    monkeypatch.setattr(containers, "find_existing_container", lambda *_a, **_k: None)
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    assert install_services_guard.foreign_owned_services(
        ["weaviate"], "podman", compose_file) == {}


# ---------------------------------------------------------------------------
# The CLI contract — exit codes and the JSON form, through a real child
# ---------------------------------------------------------------------------


def _stub_bin(tmp_path: Path, *, docker_up: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for rt in ("podman", "docker"):
        info_rc = "0" if (docker_up or rt == "podman") else "1"
        script = bin_dir / rt
        script.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            f"  info) exit {info_rc} ;;\n"
            "  compose) [ \"$2\" = version ] && exit 0; exit 1 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8")
        script.chmod(0o755)
    return bin_dir


def _run_cli(tmp_path: Path, bin_dir: Path, *extra: str):
    base = {k: v for k, v in os.environ.items() if not k.startswith("VCT_")}
    home = tmp_path / "home"
    home.mkdir()
    env = child_env(base, PATH=str(bin_dir), HOME=str(home))
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.runtime_reconcile", "decide", "--json", *extra],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))


def test_cli_decide_exit_0_and_schema_json_when_resolved(tmp_path):
    root = tmp_path / "install-root"
    (root / "state" / "install").mkdir(parents=True)  # no record: auto-detect
    proc = _run_cli(tmp_path, _stub_bin(tmp_path), "--root", str(root))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    verdict = json.loads(proc.stdout)
    assert verdict["schema"] == 1 and verdict["state"] == "resolved"
    assert verdict["runtime"] == "podman" and verdict["refused"] is False
    assert set(SCHEMA_KEYS) <= set(verdict)


def test_cli_decide_exit_3_when_refused(tmp_path):
    root = _bare_root(tmp_path, "docker")
    proc = _run_cli(tmp_path, _stub_bin(tmp_path, docker_up=False), "--root", str(root))
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    verdict = json.loads(proc.stdout)
    assert verdict["state"] == "absent" and verdict["refused"] is True
    assert verdict["refusal"] and "docker" in verdict["refusal"]
    assert containers.read_runtime_txt(root) == "docker"  # read-only: nothing moved


def test_cli_decide_rejects_an_unknown_mode(tmp_path):
    root = tmp_path / "install-root"
    (root / "state" / "install").mkdir(parents=True)
    proc = _run_cli(tmp_path, _stub_bin(tmp_path), "--root", str(root), "--mode", "bogus")
    assert proc.returncode == 2  # usage error — the CLI never guesses a mode
