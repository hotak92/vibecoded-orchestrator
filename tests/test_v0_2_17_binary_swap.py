"""v0.2.17 tests for the binary-swap auto-restart path.

Three concerns covered here, none of which require Weaviate or the Tauri
runtime:

1. `_maybe_emit_running_stale_deferral` correctly detects the git-pull
   case where source_version > install-manifest version and emits the
   `launcher_restart_required` deferral.

2. The `VCT_AUTO_RESTART_LAUNCHER=1` env var suppresses the deferral
   emit (the Rust caller will auto-restart instead).

3. Skip semantics when source_version == install-manifest version
   (no change to apply).

The Rust-side pre-pull rename + auto-restart helpers (in
`installer.rs`) are covered by `cargo test` integration via the
launcher's test crate, not by this Python suite.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture
def install_mod():
    """Import the install.py module under test. Cached per-session by
    importlib so subsequent fixture instantiations are free."""
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(
        "install_v0_2_17", repo_root / "install.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeDeferralReport:
    """Captures deferral entries so the test can assert what was emitted."""

    def __init__(self) -> None:
        self.entries: List[Any] = []

    def add_entry(self, entry: Any) -> None:
        self.entries.append(entry)

    def condition_ids(self) -> List[str]:
        return [getattr(e, "condition_id", "?") for e in self.entries]


def _write_manifest(install_root: Path, version: str) -> None:
    (install_root / "state").mkdir(parents=True, exist_ok=True)
    (install_root / "state" / "install-manifest.json").write_text(
        json.dumps({"version": version, "schema_version": 1})
    )


def _write_vct_module(install_root: Path, version: str) -> None:
    (install_root / "vct-module.json").write_text(
        json.dumps({"version": version, "id": "orchestrator", "schema_version": "1"})
    )


def _write_fake_dist_binary(install_root: Path) -> Path:
    """Create a placeholder file at the dist binary path. Used to make
    `dist_path.is_file()` True without actually running the launcher."""
    # We don't know the subdir/fname at the test layer — let the install
    # module compute it.
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import importlib
    spec = importlib.util.spec_from_file_location(
        "install_for_path", repo_root / "install.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    subdir, fname = m._launcher_binary_relative_path()
    dist_dir = install_root / "launcher" / "dist" / subdir
    dist_dir.mkdir(parents=True, exist_ok=True)
    dist_path = dist_dir / fname
    dist_path.write_text("not a real binary; just needs to be a file")
    return dist_path


@pytest.fixture
def fake_install(tmp_path: Path):
    """A tmp install_root with manifest + vct-module + dist binary file
    pre-created. Caller writes versions to set up the scenario."""
    return tmp_path


class TestRunningStaleDeferral:
    """v0.2.17 plan 0.0 — git-pull case detection."""

    def test_emits_when_source_version_newer_than_manifest(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """The canonical end-user upgrade case: source is 0.2.17,
        last install was 0.2.16, launcher PID is set. Should emit."""
        _write_vct_module(fake_install, "0.2.17")
        _write_manifest(fake_install, "0.2.16")
        dist_path = _write_fake_dist_binary(fake_install)
        # Pretend we're invoked from the launcher's update_orchestrator
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")
        # Don't set VCT_AUTO_RESTART_LAUNCHER — emit IS expected
        monkeypatch.delenv("VCT_AUTO_RESTART_LAUNCHER", raising=False)

        report = _FakeDeferralReport()
        install_mod._maybe_emit_running_stale_deferral(
            fake_install,
            dist_path=dist_path,
            deferral_report=report,
        )
        assert "launcher_restart_required" in report.condition_ids(), (
            f"expected launcher_restart_required, got {report.condition_ids()}"
        )

    def test_skips_when_source_equals_manifest(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """No version change → no emit."""
        _write_vct_module(fake_install, "0.2.17")
        _write_manifest(fake_install, "0.2.17")
        dist_path = _write_fake_dist_binary(fake_install)
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")
        monkeypatch.delenv("VCT_AUTO_RESTART_LAUNCHER", raising=False)

        report = _FakeDeferralReport()
        install_mod._maybe_emit_running_stale_deferral(
            fake_install,
            dist_path=dist_path,
            deferral_report=report,
        )
        assert report.entries == [], (
            f"expected no entries, got {report.condition_ids()}"
        )

    def test_skips_when_deferral_report_is_none(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """Safe to call with no report channel."""
        _write_vct_module(fake_install, "0.2.17")
        _write_manifest(fake_install, "0.2.16")
        dist_path = _write_fake_dist_binary(fake_install)
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")

        # Just confirms no exception
        install_mod._maybe_emit_running_stale_deferral(
            fake_install,
            dist_path=dist_path,
            deferral_report=None,
        )

    def test_skips_when_dist_path_missing(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """No binary at canonical path → no emit (defensive)."""
        _write_vct_module(fake_install, "0.2.17")
        _write_manifest(fake_install, "0.2.16")
        # Don't write a dist binary
        fake_dist = fake_install / "launcher" / "dist" / "nonexistent" / "vct-launcher"

        report = _FakeDeferralReport()
        install_mod._maybe_emit_running_stale_deferral(
            fake_install,
            dist_path=fake_dist,
            deferral_report=report,
        )
        assert report.entries == []

    def test_emits_even_without_launcher_pid_env(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """Manual `python install.py --update` from terminal: no
        VCT_LAUNCHER_PID env, but we still emit so a future
        launcher-start picks up the banner."""
        _write_vct_module(fake_install, "0.2.17")
        _write_manifest(fake_install, "0.2.16")
        dist_path = _write_fake_dist_binary(fake_install)
        monkeypatch.delenv("VCT_LAUNCHER_PID", raising=False)
        monkeypatch.delenv("VCT_AUTO_RESTART_LAUNCHER", raising=False)

        report = _FakeDeferralReport()
        install_mod._maybe_emit_running_stale_deferral(
            fake_install,
            dist_path=dist_path,
            deferral_report=report,
        )
        assert "launcher_restart_required" in report.condition_ids()


# NB: VCT_AUTO_RESTART_LAUNCHER env-gate semantics live inside
# `_refresh_dist_binary_after_rebuild`, not inside the
# `_maybe_emit_running_stale_deferral` helper. The wrapper checks
# the env, and only invokes the helper when the env is unset.
#
# v0.2.18 0.0: a regression test for the wrapper's routing logic
# DOES live here now — the v0.2.17 routing assumed `src.is_file()`
# implied the cargo path would emit, but the cargo path also bails
# silently on the mtime gate. See TestWrapperRoutingStaleSrc below.


class TestWrapperRoutingStaleSrc:
    """v0.2.18 0.0 regression — the git-pull-case helper must run
    even when a stale ``target/release/vct-launcher-temp`` exists,
    because the cargo path bails silently on the mtime gate and
    will not emit the deferral itself."""

    def _write_stale_cargo_src(self, install_root: Path) -> Path:
        """Drop a `vct-launcher-temp` file at the cargo-output path
        with an mtime far in the past, simulating a stale local
        build artifact from a prior `cargo build` run.
        """
        src = (
            install_root
            / "launcher"
            / "src-tauri"
            / "target"
            / "release"
            / "vct-launcher-temp"
        )
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("stale cargo artifact from May 16")
        # mtime way in the past so any dist file we create after this
        # will be newer (defeats Gate 2 in the cargo path).
        old_ts = 1_700_000_000.0  # 2023-11-14
        os.utime(src, (old_ts, old_ts))
        return src

    def test_stale_cargo_src_does_not_swallow_deferral(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """End-user upgrade path: ``git pull`` lands a fresh binary
        at ``launcher/dist/<arch>/vct-launcher`` AND there's a stale
        ``target/release/vct-launcher-temp`` on disk from a prior
        local build.

        Pre-v0.2.18 0.0: the wrapper's ``if not src.is_file()`` gate
        routed to "cargo path will emit" — but the cargo path's
        Gate 2 (``src_mtime <= dist_mtime``) bails silently, leaving
        the deferral unemitted. Result: the launcher's W4 restart
        banner never fired and the user kept executing the old
        binary.

        Post-fix: the helper runs unconditionally, so the deferral
        emits regardless of whether the stale cargo src exists.
        """
        _write_vct_module(fake_install, "0.2.17.1")
        _write_manifest(fake_install, "0.2.17")
        dist_path = _write_fake_dist_binary(fake_install)
        # The stale cargo artifact that used to deflect the routing.
        stale_src = self._write_stale_cargo_src(fake_install)
        assert stale_src.is_file()  # sanity
        # Pretend we're invoked from the launcher's update_orchestrator
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")
        monkeypatch.delenv("VCT_AUTO_RESTART_LAUNCHER", raising=False)
        monkeypatch.delenv("VCT_FORCE_RESTART_DEFERRAL", raising=False)

        report = _FakeDeferralReport()
        install_mod._refresh_dist_binary_after_rebuild(
            fake_install,
            no_swap=False,
            install_start_ts=None,
            deferral_report=report,
        )

        assert "launcher_restart_required" in report.condition_ids(), (
            "Regression: stale src deflected the routing and the "
            "git-pull-case deferral was lost. "
            f"Got: {report.condition_ids()}"
        )

    def test_auto_restart_env_still_suppresses_emit_with_stale_src(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """With ``VCT_AUTO_RESTART_LAUNCHER=1`` (Rust caller does the
        restart), the deferral should be suppressed even if the
        running-stale conditions are otherwise true — including when
        a stale cargo src exists. Verifies the v0.2.18 0.0 routing
        change didn't break the auto-restart suppression."""
        _write_vct_module(fake_install, "0.2.17.1")
        _write_manifest(fake_install, "0.2.17")
        _write_fake_dist_binary(fake_install)
        self._write_stale_cargo_src(fake_install)
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")
        monkeypatch.setenv("VCT_AUTO_RESTART_LAUNCHER", "1")

        report = _FakeDeferralReport()
        install_mod._refresh_dist_binary_after_rebuild(
            fake_install,
            no_swap=False,
            install_start_ts=None,
            deferral_report=report,
        )

        assert "launcher_restart_required" not in report.condition_ids(), (
            "VCT_AUTO_RESTART_LAUNCHER=1 should suppress the deferral; "
            f"got: {report.condition_ids()}"
        )

    def test_no_double_emit_when_cargo_path_also_swaps(
        self, install_mod, fake_install: Path, monkeypatch
    ):
        """When BOTH paths would emit (fresh cargo build AND
        running-stale conditions met), only one ``launcher_restart_required``
        entry should land in the report — the helper short-circuits
        on ``source_version == last_installed_version`` once the
        cargo path has bumped the manifest.

        For this test we don't actually invoke the cargo path (no real
        swap happens) — instead we verify the helper itself is
        idempotent: calling it twice in a row produces only one entry,
        which is the same property that protects against double-emit
        in the wrapper.
        """
        _write_vct_module(fake_install, "0.2.17.1")
        _write_manifest(fake_install, "0.2.17")
        dist_path = _write_fake_dist_binary(fake_install)
        monkeypatch.setenv("VCT_LAUNCHER_PID", "12345")
        monkeypatch.delenv("VCT_AUTO_RESTART_LAUNCHER", raising=False)

        report = _FakeDeferralReport()
        # First call: emits.
        install_mod._maybe_emit_running_stale_deferral(
            fake_install, dist_path=dist_path, deferral_report=report,
        )
        # Simulate the cargo path having bumped the manifest to match
        # source (which is what a successful swap+install does).
        _write_manifest(fake_install, "0.2.17.1")
        # Second call: should be a no-op (source==installed now).
        install_mod._maybe_emit_running_stale_deferral(
            fake_install, dist_path=dist_path, deferral_report=report,
        )
        assert report.condition_ids().count("launcher_restart_required") == 1, (
            "Helper should be idempotent once manifest matches source; "
            f"got: {report.condition_ids()}"
        )
