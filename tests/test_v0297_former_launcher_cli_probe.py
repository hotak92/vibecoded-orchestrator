# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: the doctor reports an old copy of the launcher CLI on PATH under a
former name (``vco`` until v0.2.96, ``vct`` before v0.1.0), and never deletes
it.

A user who installed the Rust CLI with ``launcher/tools/vct-cli/install.sh``
and never re-runs it keeps ``~/.local/bin/vco``, which hides the Python
``vco`` — "run ``vco doctor``" then answers "unrecognized subcommand". The
doctor's full pass (the one every install/update runs) finds it through
:mod:`vco_lib.launcher_cli_identity` — the same rule ``install.sh`` uses — and
emits ``former_launcher_cli_on_path``; the registry clear probe re-runs that
reading.

Stand-in executables answer ``--version`` / ``-h`` / ``--help`` the way each
real program does.
"""
from __future__ import annotations

import os
import shlex
import shutil
import tomllib
from pathlib import Path

import pytest

from vco_lib import deferral_probes, doctor
from vco_lib import launcher_cli_identity as identity

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="stand-in executables are bash scripts",
)

OLD_RUST_VCO = (
    'case "$1" in --version) echo "vco 0.2.4";; '
    '-h) echo "vibecoded-orchestrator CLI — power-user / CI escape hatch.";; '
    "--help) echo \"Talks to the running VCT Launcher's local hub server\";; esac"
)
PYTHON_VCO = (
    'case "$1" in -h|--help) echo "usage: vco [-h] {doctor,project} ..."; '
    'echo "VibeCoded Orchestrator CLI. Each subcommand maps to an";; '
    '*) echo "vco: error: the following arguments are required: subcommand" >&2; exit 2;; esac'
)
SECRETS_VCT = (
    'case "$1" in --version) echo "vct 3.1.0";; '
    '-h|--help) echo "vct — per-project secret store";; esac'
)


def _program(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def install_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    (root / "vco_lib").mkdir(parents=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _path(monkeypatch: pytest.MonkeyPatch, *dirs: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join([*(str(d) for d in dirs), "/usr/bin", "/bin"]))


def _probe(root: Path) -> list:
    fn, scopes = doctor.PROBES["former_launcher_cli"]
    assert scopes == (doctor.SCOPE_FULL,)
    return fn(root, doctor.DoctorResolvers(), {})


def test_an_old_vco_on_path_is_reported_with_the_rm_command_and_left_on_disk(
    install_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_bin = tmp_path / "home" / ".local" / "bin"
    old = _program(local_bin / "vco", OLD_RUST_VCO)
    _program(tmp_path / "venv" / "bin" / "vco", PYTHON_VCO)
    _path(monkeypatch, local_bin, tmp_path / "venv" / "bin")

    [finding] = _probe(install_root)
    assert finding.status == doctor.STATUS_PROBLEM
    assert finding.condition_id == doctor.CID_FORMER_LAUNCHER_CLI
    assert finding.command == f"rm {shlex.quote(str(old))}"
    assert str(old) in finding.summary

    [entry] = doctor.deferral_entries_for(doctor.DoctorReport(install_root, "full", [finding]))
    assert entry.condition_id == "former_launcher_cli_on_path"
    assert entry.resolved_disposition == "action_required"
    assert "install.sh" in entry.why_deferred
    assert old.is_file(), "the doctor must never delete a binary"


def test_a_python_vco_and_a_secrets_vct_are_never_flagged(
    install_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _program(bin_dir / "vco", PYTHON_VCO)
    _program(bin_dir / "vct", SECRETS_VCT)
    _path(monkeypatch, bin_dir)

    [finding] = _probe(install_root)
    assert finding.status == doctor.STATUS_OK
    assert not doctor.deferral_entries_for(doctor.DoctorReport(install_root, "full", [finding]))


def test_a_project_folder_gets_no_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH is the machine's; the install root carries the one entry."""
    _program(tmp_path / "bin" / "vco", OLD_RUST_VCO)
    _path(monkeypatch, tmp_path / "bin")
    project = tmp_path / "project"
    project.mkdir()
    assert _probe(project) == []


def _entry_for(root: Path) -> object:
    [finding] = _probe(root)
    [entry] = doctor.deferral_entries_for(doctor.DoctorReport(root, "full", [finding]))
    return entry


def _clear(root: Path, entry: object):
    return deferral_probes.PROBES["former_launcher_cli_still_on_path"](
        deferral_probes.ProbeContext(folder=root, entry=entry)
    )


def test_the_clear_probe_holds_while_the_copy_exists_and_clears_once_it_is_gone(
    install_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "with space" / "bin"
    old = _program(bin_dir / "vco", OLD_RUST_VCO)
    _path(monkeypatch, bin_dir)
    entry = _entry_for(install_root)
    assert _clear(install_root, entry) is True

    old.unlink()  # the user's act
    assert _clear(install_root, entry) is False


def test_the_clear_probe_is_not_fooled_by_a_different_path(
    install_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher or install run whose PATH lacks ~/.local/bin must not clear
    an entry for a copy that is still on disk."""
    local_bin = tmp_path / "home" / ".local" / "bin"
    _program(local_bin / "vco", OLD_RUST_VCO)
    _path(monkeypatch, local_bin)
    entry = _entry_for(install_root)

    _path(monkeypatch, tmp_path / "elsewhere")
    assert _clear(install_root, entry) is True


def test_the_registry_row_names_the_real_clear_probe() -> None:
    rows = tomllib.loads(
        (REPO / "vco_lib" / "deferral_conditions.toml").read_text(encoding="utf-8")
    )["conditions"]
    row = rows["former_launcher_cli_on_path"]
    assert row["class"] == "action_required"
    assert row["owner"] == "vco_lib.doctor"
    assert row["clear_probe"] == "probe:py:former_launcher_cli_still_on_path"
    assert "former_launcher_cli_still_on_path" in deferral_probes.PROBES


def test_the_remedy_reads_back_to_the_paths_it_names() -> None:
    copies = [
        identity.FormerCopy("/home/a b/.local/bin/vco", "vco"),
        identity.FormerCopy("/opt/it's/vct", "vct"),
    ]
    assert identity.paths_in_remedy(identity.remedy(copies)) == [c.path for c in copies]


def test_the_python_vco_description_is_what_the_parser_prints() -> None:
    from vco_lib.cli.__main__ import _build_parser

    assert (_build_parser().description or "").startswith(identity.PYTHON_VCO_DESCRIPTION)


def test_a_described_path_is_the_whole_path(install_root: Path, tmp_path: Path) -> None:
    """With ``path_command`` injected (how the doctor's other tests describe a
    machine), only what it names is judged — nothing else on the developer's
    PATH is run."""
    old = _program(tmp_path / "bin" / "vco", OLD_RUST_VCO)
    names = {"vco": str(old)}
    res = doctor.DoctorResolvers(path_command=names.get)
    [finding] = doctor.probe_former_launcher_cli(install_root, res, {})
    assert finding.status == doctor.STATUS_PROBLEM
    assert finding.command == f"rm {shlex.quote(str(old))}"

    res = doctor.DoctorResolvers(path_command=lambda name: None)
    [finding] = doctor.probe_former_launcher_cli(install_root, res, {})
    assert finding.status == doctor.STATUS_OK
