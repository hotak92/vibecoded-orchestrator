# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 R8 G1 / G5 / G6 — VCO's own runtime record is RECONCILED, not died on.

``state/install/runtime.txt`` is VCO's record of the runtime the install put the
data on; ``VCT_CONTAINER_RUNTIME`` is the user's pin. Before R8, an update on a
machine whose record had gone stale (Docker Desktop uninstalled, podman
installed; or the record's daemon down) exited 1 behind a prompt that named the
WRONG runtime as "installed but its daemon isn't responding", with no ledger
entry. ``vco_lib.runtime_reconcile`` decides instead:

  (a) recorded runtime not installed, other answers (data there, or none
      anywhere) → the record is rewritten + ``informational_record``;
  (b) recorded runtime installed but down → documented start tried; still down →
      ``action_required`` + the run continues without containers;
  (c) both answer: data under the recorded one → kept; under BOTH →
      ``action_required``; a ``--container`` choice is never overridden.

Every runtime here is a FAKE (a ``which`` / ``run`` pair, or bash stubs on a
private PATH): no podman or docker command ever runs, and every install root is
a tmp directory — the real ``runtime.txt`` and ``~/.vct`` are never touched.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import containers
from vco_lib import deferral_probes
from vco_lib import deferral_registry
from vco_lib import runtime_reconcile as rr
from vco_lib.deferral_report import DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A fake machine: which runtimes are installed, which answer, what they hold.
# ---------------------------------------------------------------------------


class FakeMachine:
    """``installed`` / ``up`` / ``data`` per runtime; ``start_ok`` makes the
    documented start bring a down runtime up. Records every argv it is asked."""

    def __init__(self, *, installed=(), up=(), data=(), start_ok=()):
        self.installed = set(installed)
        self.up = set(up)
        self.data = set(data)
        self.start_ok = set(start_ok)
        self.calls: list[list[str]] = []
        self.started: list[str] = []

    def which(self, name: str) -> Optional[str]:
        return f"/fake/{name}" if name in self.installed else None

    def run(self, argv, **_kw):
        self.calls.append(list(argv))
        rt, *rest = argv
        ok = rt in self.installed
        out = ""
        if rest[:1] == ["info"]:
            ok = ok and rt in self.up
        elif rest[:2] == ["ps", "-a"] or rest[:2] == ["volume", "ls"]:
            ok = ok and rt in self.up
            if ok and rt in self.data:
                out = "vco_weaviate\n" if rest[0] == "ps" else "vco_weaviate_data\n"
            elif ok:
                out = "someone_elses_ollama\n"
        return subprocess.CompletedProcess(argv, 0 if ok else 1, stdout=out, stderr="")

    def start(self, runtime: str):
        self.started.append(runtime)
        if runtime in self.start_ok:
            self.up.add(runtime)
            return True, "started"
        return False, f"{runtime} would not start"


def _root(tmp_path: Path, recorded: Optional[str]) -> Path:
    root = tmp_path / "clone"
    (root / "state" / "install").mkdir(parents=True)
    if recorded:
        containers.runtime_txt_path(root).write_text(recorded + "\n", encoding="utf-8")
    return root


def _reconcile(root: Path, m: FakeMachine, *, env=None, rewrite=True):
    return rr.reconcile(root, env=env or {}, which=m.which, run=m.run,
                        start_daemon=m.start, rewrite=rewrite)


def _record(root: Path) -> Optional[str]:
    return containers.read_runtime_txt(root)


# ---------------------------------------------------------------------------
# (a) the recorded runtime is gone
# ---------------------------------------------------------------------------


def test_a_docker_recorded_with_only_podman_holding_the_data_is_re_recorded(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"}, up={"podman"}, data={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "podman"
    assert _record(root) == "podman"
    [entry] = res.entries
    assert entry.condition_id == rr.CID_RECORD_RECONCILED
    assert entry.resolved_disposition == "informational_record"
    assert "docker" in entry.detected and "podman" in entry.detected
    # The auto-resolution trail (B-F9) carries the same event.
    assert (root / ".claude" / "logs" / "auto-resolutions.jsonl").is_file()


def test_a_docker_recorded_with_no_vco_data_anywhere_is_re_recorded(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"}, up={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.REWRITTEN and _record(root) == "podman"


def test_a_gone_record_with_the_other_runtime_down_starts_it_first(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"}, data={"podman"}, start_ok={"podman"})
    res = _reconcile(root, m)
    assert m.started == ["podman"]
    assert res.outcome is rr.Outcome.REWRITTEN and _record(root) == "podman"


def test_a_gone_record_with_nothing_usable_is_action_required_and_kept(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert _record(root) == "docker"
    [entry] = res.entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert entry.resolved_disposition == "action_required"
    assert "--container podman" in entry.command_to_apply


def test_a_gone_record_whose_other_runtime_cannot_be_listed_is_not_rewritten(tmp_path):
    """"Could not look" is never "empty": without positive evidence nothing moves."""
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"}, up={"podman"})
    real_run = m.run

    def run(argv, **kw):
        if argv[1:3] == ["volume", "ls"]:
            return subprocess.CompletedProcess(argv, 125, stdout="", stderr="boom")
        return real_run(argv, **kw)

    res = rr.reconcile(root, env={}, which=m.which, run=run, start_daemon=m.start)
    assert res.outcome is rr.Outcome.UNUSABLE and _record(root) == "docker"


# ---------------------------------------------------------------------------
# (b) the recorded runtime is installed but down
# ---------------------------------------------------------------------------


def test_b_podman_recorded_but_down_is_never_switched_even_with_docker_holding_data(tmp_path):
    root = _root(tmp_path, "podman")
    m = FakeMachine(installed={"podman", "docker"}, up={"docker"}, data={"docker"})
    res = _reconcile(root, m)
    assert m.started == ["podman"], "the documented start must be tried first"
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert _record(root) == "podman"
    [entry] = res.entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert "podman" in entry.command_to_apply and "start" in entry.command_to_apply.lower()
    assert entry.dismiss_fields == {"runtime": "podman", "via": containers.PIN_VIA_RUNTIME_TXT,
                                    "root": str(root)}
    # No `ps` / `volume ls` against a runtime that is down — and none against
    # docker either: a down record is never compared, it is never switched.
    assert not [c for c in m.calls if c[1:2] in (["ps"], ["volume"])]


def test_b_a_down_record_that_the_documented_start_brings_up_is_kept(tmp_path):
    root = _root(tmp_path, "podman")
    m = FakeMachine(installed={"podman"}, start_ok={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.KEPT and res.runtime == "podman" and not res.entries


def test_b_the_users_env_pin_stays_strict_and_names_itself(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman", "docker"}, up={"docker"}, data={"docker"})
    res = _reconcile(root, m, env={"VCT_CONTAINER_RUNTIME": "podman"})
    assert res.outcome is rr.Outcome.UNUSABLE
    assert "VCT_CONTAINER_RUNTIME" in res.detail
    assert _record(root) == "docker", "an env pin never rewrites VCO's record"
    assert "unset VCT_CONTAINER_RUNTIME" in res.entries[0].command_to_apply


# ---------------------------------------------------------------------------
# (c) both runtimes answer
# ---------------------------------------------------------------------------


def test_c_data_under_the_recorded_runtime_keeps_the_record(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman", "docker"}, up={"podman", "docker"}, data={"docker"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.KEPT and not res.entries and _record(root) == "docker"


def test_c_data_under_both_keeps_the_record_and_asks(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman", "docker"}, up={"podman", "docker"}, data={"docker", "podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.DATA_UNDER_BOTH and res.runtime == "docker"
    assert _record(root) == "docker"
    [entry] = res.entries
    assert entry.condition_id == rr.CID_DATA_UNDER_BOTH
    assert entry.resolved_disposition == "action_required"
    assert entry.dismiss_fields == {"recorded": "docker", "root": str(root)}


def test_c_a_confirmed_choice_is_never_second_guessed(tmp_path):
    root = _root(tmp_path, None)
    rr.record_explicit_choice(root, "docker")
    assert _record(root) == "docker" and rr.read_confirmed(root) == "docker"
    m = FakeMachine(installed={"podman", "docker"}, up={"podman", "docker"}, data={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.KEPT and _record(root) == "docker"


def test_c_a_record_holding_nothing_while_the_other_holds_the_data_is_re_recorded(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman", "docker"}, up={"podman", "docker"}, data={"podman"})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.REWRITTEN and _record(root) == "podman"


def test_the_read_only_form_decides_the_same_and_changes_nothing(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman"}, data={"podman"}, start_ok={"podman"})
    m.up.add("podman")
    res = _reconcile(root, m, rewrite=False)
    assert res.outcome is rr.Outcome.REWRITTEN and res.runtime == "podman"
    assert _record(root) == "docker" and not res.entries and m.started == []
    assert not (root / ".claude").exists()


def test_vco_volume_names_match_the_installer_compose():
    text = (REPO_ROOT / "infrastructure" / "docker-compose.yml").read_text(encoding="utf-8")
    defaults = tuple(re.findall(r"name:\s*\$\{VCT_[A-Z_]+_VOLUME_NAME:-([a-z_]+)\}", text))
    assert defaults == rr.VCO_VOLUME_NAMES


# ---------------------------------------------------------------------------
# install.py glue
# ---------------------------------------------------------------------------


class _Report:
    def __init__(self):
        self.entries = []

    def add_entry(self, e):
        self.entries.append(e)


def test_apply_at_install_defers_containers_instead_of_failing(tmp_path):
    root = _root(tmp_path, "podman")
    m = FakeMachine(installed={"podman"})
    args = argparse.Namespace(no_containers=False, container=None)
    report, events = _Report(), []
    res = rr.apply_at_install(root, args, report, start_daemon=m.start,
                              log_event=lambda *a, **k: events.append(a),
                              out=lambda _s: None, env={}, which=m.which, run=m.run)
    assert res is not None and res.outcome is rr.Outcome.UNUSABLE
    assert args.no_containers is True and "podman" in args.containers_deferred
    assert [e.condition_id for e in report.entries] == [rr.CID_UNUSABLE]
    note = rr.containers_skipped_note(args)
    assert "deferred" in note and "You skipped" not in note


def test_apply_at_install_records_an_explicit_container_choice_as_confirmed(tmp_path):
    root = _root(tmp_path, "podman")
    args = argparse.Namespace(no_containers=False, container="docker")
    assert rr.apply_at_install(root, args, None, start_daemon=None,
                               log_event=lambda *a, **k: None) is None
    assert _record(root) == "docker" and rr.read_confirmed(root) == "docker"


_STUB_TEMPLATE = """#!/usr/bin/env bash
case "$1" in
  version) exit 0 ;;
  info) {info} ;;
  ps) {ps} ;;
  volume) {volume} ;;
  compose) exit 0 ;;
esac
exit 0
"""


def _fake_path(tmp_path: Path, stubs: dict) -> Path:
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("bash", "env"):
        real = shutil.which(name)
        assert real
        (fake / name).symlink_to(real)
    for name, body in stubs.items():
        (fake / name).write_text(body, encoding="utf-8")
        (fake / name).chmod(0o755)
    return fake


def _stub(up: bool, data: bool) -> str:
    return _STUB_TEMPLATE.format(
        info="exit 0" if up else "exit 125",
        ps=("echo vco_weaviate; exit 0" if data else "exit 0") if up else "exit 125",
        volume=("echo vco_weaviate_data; exit 0" if data else "exit 0") if up else "exit 125",
    )


def _install_root(tmp_path: Path, recorded: str) -> Path:
    root = _root(tmp_path, recorded)
    (root / "vco_lib").mkdir()
    (root / ".claude").mkdir()
    return root


def _detect_args() -> argparse.Namespace:
    return argparse.Namespace(
        container=None, no_containers=False, cpu_only=True, gpu=False, openai_key="",
        no_gpu_check=False, gpu_vram_threshold_gb=8.0,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime stubs")
def test_install_detect_system_re_records_a_stale_docker_record(tmp_path, monkeypatch):
    """G1's own scenario, through install.py's `_detect_system`: docker
    recorded, only podman installed and holding VCO's data. Before R8 the pin
    was refused, `container_cmd` was "" and step 6 exited 1 behind a prompt
    naming podman "installed but its daemon isn't responding"."""
    import install

    root = _install_root(tmp_path, "docker")
    fake = _fake_path(tmp_path, {"podman": _stub(up=True, data=True)})
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(root))
    monkeypatch.delenv("VCT_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_try_start_podman_daemon", lambda: (False, "not in tests"))
    monkeypatch.setattr(install, "_try_start_docker_daemon", lambda: (False, "not in tests"))
    report = _Report()
    args = _detect_args()
    sysinfo = install._detect_system(args, report)
    assert sysinfo.container_cmd == "podman"
    assert _record(root) == "podman"
    assert [e.condition_id for e in report.entries] == [rr.CID_RECORD_RECONCILED]
    assert args.no_containers is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime stubs")
def test_install_detect_system_defers_containers_when_the_recorded_runtime_stays_down(
        tmp_path, monkeypatch):
    import install

    root = _install_root(tmp_path, "podman")
    fake = _fake_path(tmp_path, {"podman": _stub(up=False, data=False),
                                 "docker": _stub(up=True, data=True)})
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(root))
    monkeypatch.delenv("VCT_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    tried: list[str] = []
    monkeypatch.setattr(install, "_try_start_podman_daemon",
                        lambda: (tried.append("podman"), (False, "socket would not start"))[1])
    monkeypatch.setattr(install, "_try_start_docker_daemon", lambda: (False, "unused"))
    report = _Report()
    args = _detect_args()
    sysinfo = install._detect_system(args, report)
    assert tried == ["podman"]
    assert sysinfo.container_cmd == ""
    assert args.no_containers is True, "step 6 must be skipped, not `return 1`"
    assert [e.condition_id for e in report.entries] == [rr.CID_UNUSABLE]
    assert _record(root) == "podman", "a down record is never switched to docker"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime stubs")
def test_the_prompt_never_calls_the_other_runtime_installed_but_not_responding(
        tmp_path, monkeypatch, capsys):
    """R8 G1's false prompt: pinned docker is missing, podman is installed.
    Before, `_prompt_install_container_runtime` printed "podman is installed but
    its daemon isn't responding" (podman was fine)."""
    import install

    fake = _fake_path(tmp_path, {"podman": _stub(up=True, data=False)})
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setenv("VCT_CONTAINER_RUNTIME", "docker")
    args = argparse.Namespace(yes=True, quiet=True)
    assert install._prompt_install_container_runtime(args) is False
    out = capsys.readouterr().out
    assert "podman is installed but its daemon isn't responding" not in out
    assert "pinned to docker by VCT_CONTAINER_RUNTIME" in out
    assert "--container podman" in out


# ---------------------------------------------------------------------------
# Registry + clear probes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cid,klass,probe", [
    (rr.CID_RECORD_RECONCILED, "informational_record", None),
    (rr.CID_UNUSABLE, "action_required", "container_runtime_still_unusable"),
    (rr.CID_DATA_UNDER_BOTH, "action_required", "container_runtime_data_still_under_both"),
])
def test_each_condition_is_registered_with_its_lifecycle(cid, klass, probe):
    spec = deferral_registry.condition(cid)
    assert spec is not None and spec.condition_class == klass
    assert deferral_registry.disposition_for(cid) == klass
    assert deferral_probes.registry_probe_name(cid) == probe
    if probe:
        assert probe in deferral_probes.PROBES


def test_the_unusable_probe_clears_once_the_pinned_runtime_answers(tmp_path, monkeypatch):
    root = _root(tmp_path, "podman")
    m = FakeMachine(installed={"podman"})
    entry = _reconcile(root, m).entries[0]
    assert rr.unusable_still_applies(entry, env={}, which=m.which, run=m.run) is True
    m.up.add("podman")
    assert rr.unusable_still_applies(entry, env={}, which=m.which, run=m.run) is False


def test_the_both_probe_clears_on_a_confirmed_choice(tmp_path):
    root = _root(tmp_path, "docker")
    m = FakeMachine(installed={"podman", "docker"}, up={"podman", "docker"}, data={"docker", "podman"})
    entry = _reconcile(root, m).entries[0]
    assert rr.data_still_under_both(entry, which=m.which, run=m.run) is True
    rr.record_explicit_choice(root, "docker")
    assert rr.data_still_under_both(entry, which=m.which, run=m.run) is False


# ---------------------------------------------------------------------------
# G6 — the boot wrapper's exit 3 reaches the ledger
# ---------------------------------------------------------------------------


def test_record_boot_refusal_writes_the_installed_clones_ledger(tmp_path):
    root = _root(tmp_path, "docker")
    assert rr.record_boot_refusal(root, "the container runtime is pinned to docker ...", env={}) is True
    [entry] = DeferralReport.read(root).entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert entry.dismiss_fields["root"] == str(root) and entry.dismiss_fields["runtime"] == "docker"


def test_record_boot_refusal_never_writes_a_development_checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    assert rr.record_boot_refusal(root, "x", env={}) is False
    assert not (root / ".claude").exists()


def test_the_module_is_importable_before_the_venv_exists():
    """It runs from install.py's pre-venv `_detect_system`."""
    from tests.common.import_chain import requirements_import_roots, third_party_reachable_from

    reach = third_party_reachable_from("vco_lib.runtime_reconcile")
    assert not (set(reach) & set(requirements_import_roots())), reach


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
