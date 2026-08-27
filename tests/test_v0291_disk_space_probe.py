# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 wave-4 — the doctor's DISK-SPACE probe.

The gap it closes (user-approved 2026-08-27, after a near-miss on a full disk):
VCO writes to two filesystems all day — the install root (clone, venvs, dist
binaries, KG markdown) and the vct state dir (``launcher.db``, hub lockfiles,
the RL event archive, logs) — and had NO check on either. Everything
downstream of a full disk fails in a way that does not name the cause: a
Weaviate write, a ``launcher.db`` commit, a dist-binary swap and a gzip archive
of RL rows each surface as their own local error, so the operator debugs four
symptoms instead of one condition.

The probe composes ``shutil.disk_usage`` into the existing doctor engine and
follows its tri-state discipline exactly: a path it cannot measure is
``unknown``, never ``ok``. Below the floor it emits the registry-classed
``disk_space_low`` condition THROUGH the locked emitter; above the floor the
same measurement RESOLVES that entry, so the promise "clears itself once space
comes back" is true at every invocation point.

Hermetic by construction: every test stubs ``shutil.disk_usage`` and pins
``VCT_STATE_DIR`` inside ``tmp_path``. Nothing here reads the real disk, and no
assertion depends on how full the machine running the suite happens to be.

RED-PROOF for the whole file: no disk probe existed before this change —
``vco_lib.doctor.PROBES`` had five entries and the registry had no
``disk_space_low`` row, so every test below fails at import/lookup on the
pre-change tree.
"""
from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_probes, deferral_registry, doctor  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402

_GIB = 1024 ** 3
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def install(tmp_path: Path, monkeypatch):
    """An install root + a state dir, both inside tmp_path, both isolated."""
    root = tmp_path / "install"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv(doctor.DISK_MIN_FREE_ENV, raising=False)
    return root


def _free(free_bytes: int):
    """A ``shutil.disk_usage`` stub reporting the same free space everywhere."""

    def _usage(_path):
        return _Usage(total=100 * _GIB, used=100 * _GIB - free_bytes, free=free_bytes)

    return _usage


def _stub_disk(monkeypatch, free_bytes: int) -> None:
    monkeypatch.setattr("shutil.disk_usage", _free(free_bytes))


def _hermetic_resolvers() -> doctor.DoctorResolvers:
    """Every NON-disk probe fed from a fake machine.

    The v0.2.89 lesson: a test that lets one probe reach the real environment
    is no longer testing the thing it named — and here it would also spend
    seconds per case on `npm list -g` subprocesses. ``disk_usage`` is left at
    its default so the ``shutil.disk_usage`` stub is what answers.
    """
    return doctor.DoctorResolvers(
        npx_probe=lambda names: {
            "npx_present": True,
            "npx_path": "/stub/bin/npx",
            "npm_present": True,
            "commands": {n: True for n in names},
        },
        mcp_entries=dict,
        pin_rows=list,
    )


def _report(install_root: Path, **kw) -> doctor.DoctorReport:
    kw.setdefault("resolvers", _hermetic_resolvers())
    return doctor.run_doctor(install_root, **kw)


def _finding(report: doctor.DoctorReport) -> doctor.Finding:
    matches = [f for f in report.findings if f.probe == "disk_space"]
    assert len(matches) == 1, [f.probe for f in report.findings]
    return matches[0]


# --------------------------------------------------------------------------
# 1. The verdict
# --------------------------------------------------------------------------


def test_below_the_floor_is_a_problem_naming_mount_free_and_floor(install, monkeypatch):
    _stub_disk(monkeypatch, 512 * 1024 * 1024)  # 0.5 GiB — under the 2 GiB floor
    finding = _finding(_report(install))
    assert finding.status == doctor.STATUS_PROBLEM
    assert finding.fix == doctor.FIX_DEFER, "freeing space deletes USER files"
    assert finding.condition_id == doctor.CID_DISK_SPACE_LOW
    assert "0.50 GiB" in finding.summary
    assert "2 GiB" in finding.summary, "the floor must be named in the finding"
    assert str(install) in finding.summary or any(
        str(install) == m["path"] for m in finding.detail["mounts"]
    )
    assert finding.detail["severity"] == "warning"
    assert finding.detail["min_free_bytes"] == 2 * _GIB
    assert finding.command, "a defer finding must name what the user should do"


def test_at_or_above_the_floor_is_ok_and_emits_nothing(install, monkeypatch):
    _stub_disk(monkeypatch, 2 * _GIB)  # exactly at the floor is NOT below it
    report = _report(install)
    finding = _finding(report)
    assert finding.status == doctor.STATUS_OK
    assert doctor.deferral_entries_for(report) == []


def test_critical_tier_below_256_mib(install, monkeypatch):
    _stub_disk(monkeypatch, 100 * 1024 * 1024)
    finding = _finding(_report(install))
    assert finding.detail["severity"] == "critical"
    assert "CRITICALLY low" in finding.summary
    entry = doctor.deferral_entries_for(
        doctor.DoctorReport(folder=install, scope="full", findings=[finding])
    )[0]
    assert entry.severity == "critical"


def test_env_override_moves_the_floor_both_ways(install, monkeypatch):
    _stub_disk(monkeypatch, 3 * _GIB)
    assert _finding(_report(install)).status == doctor.STATUS_OK

    monkeypatch.setenv(doctor.DISK_MIN_FREE_ENV, "10")
    finding = _finding(_report(install))
    assert finding.status == doctor.STATUS_PROBLEM
    assert finding.detail["min_free_bytes"] == 10 * _GIB

    # Fractional floors are legal (a small VM with a 500 MiB comfort margin).
    monkeypatch.setenv(doctor.DISK_MIN_FREE_ENV, "0.5")
    assert _finding(_report(install)).status == doctor.STATUS_OK


@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "0", "-4"])
def test_a_malformed_floor_falls_back_to_the_default_not_to_off(
    install, monkeypatch, bad
):
    """A fat-fingered override must not silently DISABLE the check — the same
    policy as VCO_CG_INJECT_CAP."""
    monkeypatch.setenv(doctor.DISK_MIN_FREE_ENV, bad)
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    finding = _finding(_report(install))
    assert finding.status == doctor.STATUS_PROBLEM
    assert finding.detail["min_free_bytes"] == int(
        doctor.DISK_MIN_FREE_GB_DEFAULT * _GIB
    )


# --------------------------------------------------------------------------
# 2. What it measures
# --------------------------------------------------------------------------


def test_both_the_install_root_and_the_state_dir_are_measured(install, monkeypatch):
    """Two filesystems in the common case, and they starve differently: the
    install root holds the clone/venvs/dist binaries, the state dir holds
    launcher.db, hub lockfiles, the RL archive and the logs."""
    # Distinct devices, simulated through the module's own identity seam (the
    # real one is st_dev; on a single-filesystem CI box both paths share one).
    monkeypatch.setattr(doctor, "_disk_device_key", lambda p: ("path", str(p)))
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    measured, _unmeasurable, _floor, _gib = doctor.measure_disk_space(install)
    labels = {m["label"] for m in measured}
    assert labels == {"install root", "vct state dir"}
    paths = {m["path"] for m in measured}
    assert str(install) in paths


def test_the_same_filesystem_is_reported_once(install, monkeypatch):
    """Deduped by st_dev: on most installs the clone and ~/.vct share a
    filesystem, and reporting one mount twice makes a single low-space
    condition read like two."""
    monkeypatch.setattr(doctor, "_disk_device_key", lambda _p: ("dev", 42))
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    measured, _u, _f, _g = doctor.measure_disk_space(install)
    assert len(measured) == 1, measured


def test_a_state_dir_that_does_not_exist_yet_measures_its_parent(
    install, tmp_path, monkeypatch
):
    """First run: ~/.vct has not been created. Its parent's filesystem is the
    one that would hold it, so walking up measures the RIGHT device instead of
    reporting 'unknown' for a perfectly measurable mount."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "never" / "created"))
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    measured, unmeasurable, _f, _g = doctor.measure_disk_space(install)
    assert measured, "a not-yet-created state dir must not defeat the probe"
    assert unmeasurable == []


def test_an_unmeasurable_path_is_unknown_never_ok(install, monkeypatch):
    """Tri-state discipline: 'I could not look' is not 'the disk is fine'."""

    def _boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr("shutil.disk_usage", _boom)
    report = _report(install)
    finding = _finding(report)
    assert finding.status == doctor.STATUS_UNKNOWN
    assert doctor.deferral_entries_for(report) == []
    assert report.ok, "unknown findings never fail the doctor"


# --------------------------------------------------------------------------
# 3. Emission through the locked emitter
# --------------------------------------------------------------------------


def test_low_space_emits_the_registered_condition_through_the_emitter(
    install, monkeypatch
):
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    report = _report(install)
    emitted = doctor.emit_findings(install, report)
    assert emitted == [doctor.CID_DISK_SPACE_LOW]

    entries = DeferralReport.read(install).entries
    assert [e.condition_id for e in entries] == ["disk_space_low"]
    entry = entries[0]
    assert entry.severity == "warning"
    assert entry.resolved_disposition == "environmental"
    assert "clears itself" in entry.why_deferred.lower()
    assert entry.dismiss_fields["mount_paths"], "dismiss identity must be populated"
    assert "df -h" in entry.command_to_apply


def test_a_sink_receives_the_entry_instead_of_a_second_writer(install, monkeypatch):
    """install.py passes its in-flight run report as the sink so the entry
    rides that run's single authoritative write, never a write behind
    finalize()'s back."""
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    sink = DeferralReport()
    emitted = doctor.emit_findings(install, _report(install), sink=sink)
    assert emitted == [doctor.CID_DISK_SPACE_LOW]
    assert [e.condition_id for e in sink.entries] == ["disk_space_low"]
    assert not (install / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()


# --------------------------------------------------------------------------
# 4. Clearing — both sides
# --------------------------------------------------------------------------


def _seed_entry(folder: Path) -> DeferralEntry:
    entry = DeferralEntry(
        condition_id=doctor.CID_DISK_SPACE_LOW,
        title="Low disk space — VCO writes may start failing",
        detected="install root /x has only 0.10 GiB free",
        why_deferred="seeded by the test",
        command_to_apply="df -h /x",
        severity="warning",
    )
    from vco_lib.deferral_emit import emit_entries

    emit_entries(folder, [entry])
    return entry


def test_clear_probe_keeps_the_entry_while_space_is_still_low(install, monkeypatch):
    _stub_disk(monkeypatch, 100 * 1024 * 1024)
    entry = _seed_entry(install)
    assert deferral_probes.evaluate(install, entry) is True
    assert deferral_probes.resolvable_condition_ids(
        install, DeferralReport.read(install)
    ) == []


def test_clear_probe_resolves_the_entry_once_space_recovers(install, monkeypatch):
    """The re-probe pass (every --update, and the bundle-update reconcile)
    consumes exactly this verdict."""
    _stub_disk(monkeypatch, 50 * _GIB)
    entry = _seed_entry(install)
    assert deferral_probes.evaluate(install, entry) is False
    assert deferral_probes.resolvable_condition_ids(
        install, DeferralReport.read(install)
    ) == ["disk_space_low"]


def test_clear_probe_is_unknown_when_nothing_can_be_measured(install, monkeypatch):
    def _boom(_path):
        raise OSError("nope")

    monkeypatch.setattr("shutil.disk_usage", _boom)
    entry = _seed_entry(install)
    assert deferral_probes.evaluate(install, entry) is None, (
        "positive evidence only — an unmeasurable disk must never clear an "
        "entry describing real outstanding work"
    )


def test_a_healthy_doctor_pass_resolves_the_entry_itself(install, monkeypatch):
    """So "clears itself once space comes back" is true at BOOT and at
    `vco doctor`, not only at the next --update re-probe pass."""
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    doctor.emit_findings(install, _report(install))
    assert [e.condition_id for e in DeferralReport.read(install).entries] == [
        "disk_space_low"
    ]

    _stub_disk(monkeypatch, 50 * _GIB)
    healthy = _report(install)
    assert doctor.healthy_condition_ids(healthy) == [doctor.CID_DISK_SPACE_LOW]
    doctor.emit_findings(install, healthy)
    assert [e.condition_id for e in DeferralReport.read(install).entries] == []


def test_the_self_resolve_never_runs_on_the_sink_path(install, monkeypatch):
    """With a sink, install.py's finalize() is still pending and re-merges from
    disk — a resolve landing in that window would be resurrected, and the entry
    would look immortal for one more cycle."""
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    doctor.emit_findings(install, _report(install))

    _stub_disk(monkeypatch, 50 * _GIB)
    doctor.emit_findings(install, _report(install), sink=DeferralReport())
    assert [e.condition_id for e in DeferralReport.read(install).entries] == [
        "disk_space_low"
    ], "the sink path must leave the on-disk ledger alone"


# --------------------------------------------------------------------------
# 5. Wiring
# --------------------------------------------------------------------------


def test_the_probe_runs_in_the_boot_scope_as_well_as_full():
    """One `shutil.disk_usage` call per distinct filesystem (the de-dupe runs
    BEFORE the measurement) is cheaper than any file read the other boot probes
    already do, and a machine that cannot write is exactly what a user needs to
    hear about BEFORE they start working."""
    _fn, scopes = doctor.PROBES["disk_space"]
    assert doctor.SCOPE_BOOT in scopes and doctor.SCOPE_FULL in scopes


def test_boot_scope_actually_produces_the_finding(install, monkeypatch):
    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    report = _report(install, scope=doctor.SCOPE_BOOT)
    assert _finding(report).status == doctor.STATUS_PROBLEM


def test_the_registry_row_declares_the_lifecycle():
    spec = deferral_registry.condition("disk_space_low")
    assert spec is not None, "the cid must be registered (completeness gate)"
    assert spec.condition_class == "environmental"
    assert spec.owner == "vco_lib.doctor"
    assert spec.probe_name == "disk_space_still_low"
    assert spec.dismiss_key == ("mount_paths",)
    assert not spec.is_owned_by_install, (
        "the doctor's disk emit can run OUTSIDE an install run (boot, CLI), so "
        "install-ownership would let that run's finalize drop a live condition"
    )
    assert deferral_probes.PROBES[spec.probe_name] is deferral_probes.disk_space_still_low


def test_the_dismiss_key_can_actually_be_populated(install, monkeypatch):
    """A key nobody can produce would hash to a constant and make ONE dismissal
    suppress the condition forever."""
    from vco_lib.deferral_dismissal import fields_for

    _stub_disk(monkeypatch, 512 * 1024 * 1024)
    # Entry-carried values win...
    report = _report(install)
    entry = doctor.deferral_entries_for(report)[0]
    assert fields_for(install, "disk_space_low", entry)["mount_paths"]
    # ...and an entry written by an older VCO (no dismiss_fields) still keys.
    bare = DeferralEntry(
        condition_id="disk_space_low", title="t", detected="d",
        why_deferred="w", command_to_apply="c",
    )
    assert fields_for(install, "disk_space_low", bare)["mount_paths"]
