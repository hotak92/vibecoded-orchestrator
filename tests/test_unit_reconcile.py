# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.unit_reconcile — stale systemd user-unit reconcile.

The reconcile step runs on ``install.py --update`` and retires user-level
systemd units that reference the current install root but whose entrypoint no
longer resolves (a ``python -m <module>`` whose module moved, or a plain exec
path that vanished). Motivating class: a per-project service unit outliving the
code it launches crashloops the missing entrypoint and grows its append log
without bound, and nothing in the update flow noticed.

Coverage matrix (the decision, not just the side effects — per the repo's
test-the-decision rule):

  ACT (retire):
    - broken ``python -m`` module (find_spec MISSING) → disabled + backed up +
      recorded, with the restore command in the record.
    - broken plain exec path (missing/non-executable) → retired.
    - oversized append log rotated alongside the unit backup.

  LEAVE-ALONE:
    - module resolves            (no action, module_resolves)
    - exec resolves              (no action, exec_resolves)
    - foreign unit (not our root) (no action, foreign_unit)
    - unparseable ExecStart       (no action, execstart_unparseable)
    - probe failed (interp gone)  (no action, probe_failed)
    - systemctl absent            (skip whole pass)
    - non-Linux                   (skip whole pass)
    - systemctl disable errors    (no action, systemctl_error)
    - under-threshold log         (log left in place)

  v0.3.0 review fixes (MAJOR/MINOR/NIT — each pins the FIXED decision):
    - L-1: `-m` classifier honours python-CLI semantics — a `-m` after a script
      positional, or under a non-python head, is NOT a module flag (no false
      retirement); a genuine `python -m gone` still retires.
    - L-2: module probe is TRI-STATE — a probe that could-not-run (env -S,
      PATH-relative interp, None from the resolver) is a leave-alone, never a
      retire; only a clean provably-missing retires.
    - L-3: exactly ONE live reconcile call site (main()'s --update); the dead
      lightweight call is gone and pinned out.
    - M-1: root reference is anchored — a sibling install (<root>2) stays
      foreign.
    - M-2: disable-succeeds-but-backup-fails emits a warning record + re-enable
      command (mutation never invisible).
    - M-3: symlinked append logs are never followed; same-named logs from two
      units in one pass get distinct (slug-prefixed) backups.
    - M-4: a durable RESTORE.txt sidecar sits next to every backup.
    - N-4: restore command shell-quotes spaced install-root paths.
    - N-5/N-8: shim loud-fails on ImportError; log level threads into the
      install-event phase.

Every test injects fakes for systemctl (never real), HOME, and the interpreter
module probe — NO real ``systemctl`` mutation ever runs. The destructive-action
gate (``UnitAction.acted``) is asserted both ways.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.unit_reconcile import (  # noqa: E402
    CONDITION_ID_PREFIX,
    LOG_ROTATE_THRESHOLD_BYTES,
    RETIRED_UNITS_REL,
    reconcile_stale_units,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def force_linux(monkeypatch):
    """Force the module's platform.system() to Linux so tests run on any host."""
    import vco_lib.unit_reconcile as ur

    monkeypatch.setattr(ur.platform, "system", lambda: "Linux")


@pytest.fixture
def env(tmp_path):
    """A hermetic (home, install_root) pair with the systemd unit dir made."""
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    install_root = tmp_path / "install"
    install_root.mkdir()
    return home, install_root, unit_dir


def _write_unit(unit_dir: Path, name: str, execstart: str,
                environment: str | None = None,
                std_append: str | None = None) -> Path:
    """Materialise a fake systemd unit file."""
    env_line = f"Environment={environment}\n" if environment else ""
    std_lines = ""
    if std_append is not None:
        std_lines = (
            f"StandardOutput=append:{std_append}\n"
            f"StandardError=append:{std_append}\n"
        )
    path = unit_dir / name
    path.write_text(
        "[Unit]\n"
        "Description=test unit\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_line}"
        f"ExecStart={execstart}\n"
        f"{std_lines}"
        "Restart=always\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    return path


class FakeSystemctl:
    """Injectable ``systemctl`` runner. Records argv; never touches real systemd."""

    def __init__(self, rc: int = 0, stderr: str = ""):
        self.rc = rc
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.rc, "", self.stderr


def _resolver(mapping):
    """Build an injectable module resolver from a {module: bool} mapping.

    Unknown modules default to False (not importable) — the caller lists the
    modules it wants to resolve TRUE.
    """
    def resolve(python, module, pythonpath):
        return bool(mapping.get(module, False))
    return resolve


def _reconcile(install_root, home, *, systemctl=None, resolver=None,
               available=True, deferral=None):
    """Convenience wrapper threading the standard fakes."""
    return reconcile_stale_units(
        install_root,
        deferral_report=deferral,
        home=home,
        systemctl_available=lambda: available,
        systemctl_runner=systemctl or FakeSystemctl(),
        resolve_module=resolver or _resolver({}),
        log=None,
    )


# ---------------------------------------------------------------------------
# ACT: broken module → retire
# ---------------------------------------------------------------------------


def test_act_broken_module_is_retired(force_linux, env):
    home, install_root, unit_dir = env
    py = str(install_root / ".venv" / "bin" / "python")
    # Make the configured interpreter exist so the probe is not short-circuited.
    Path(py).parent.mkdir(parents=True)
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server --port 9000",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": False}),  # provably broken
        deferral=deferral,
    )

    # DECISION: exactly one unit acted.
    acted = result.retired
    assert len(acted) == 1
    action = acted[0]
    assert action.acted is True
    assert action.unit_name == "rl-server-demo.service"
    assert action.condition_id == f"{CONDITION_ID_PREFIX}rl-server-demo"

    # systemctl disable --now was invoked with the right argv.
    assert systemctl.calls == [
        ["systemctl", "--user", "disable", "--now", "rl-server-demo.service"]
    ]

    # Unit file was MOVED (original gone), backup lands under .claude/state.
    assert not unit.exists()
    backup_dir = install_root / RETIRED_UNITS_REL
    backups = list(backup_dir.glob("rl-server-demo.service.*.bak"))
    assert len(backups) == 1
    assert action.backup_path == backups[0]
    # Backup preserves original content.
    assert "rl_server" in backups[0].read_text(encoding="utf-8")

    # Recorded in the deferral report with the restore command.
    entries = deferral.entries
    assert len(entries) == 1
    entry = entries[0]
    assert entry.condition_id == f"{CONDITION_ID_PREFIX}rl-server-demo"
    assert "rl-server-demo.service" in entry.detected
    assert str(backups[0]) in entry.command_to_apply
    assert "systemctl --user enable --now rl-server-demo.service" in entry.command_to_apply
    assert "daemon-reload" in entry.command_to_apply


def test_act_legacy_rl_server_dotted_module_with_pythonpath_is_retired(force_linux, env):
    """WP-Q item 4: the EXACT legacy RL serving unit shape retires.

    The pre-container ``rl-server-<project>.service`` units run
    ``<venv>/python -m rl_server.rl_server`` (DOTTED module) with the module
    path carried in ``Environment=PYTHONPATH=<root>/...``. Since the RL
    reranker moved to a container, ``rl_server.rl_server`` no longer resolves
    — this is precisely the crash-looping unit class this reconcile step
    retires. Pins the dotted-module + PYTHONPATH combination end-to-end (the
    existing broken-module test used the single-segment ``-m rl_server`` with
    no PYTHONPATH; this locks the real shape).
    """
    home, install_root, unit_dir = env
    py = str(install_root / ".venv" / "bin" / "python")
    Path(py).parent.mkdir(parents=True)
    Path(py).write_text("#!py\n")
    # The live shape: dotted module + PYTHONPATH pointing under the install
    # root (so the unit is classified OURS via the PYTHONPATH reference, then
    # its dotted module is probed under that PYTHONPATH).
    pythonpath = f"{install_root}/paid-modules/vct-rl-reranker"
    unit = _write_unit(
        unit_dir, "rl-server-vcodev.service",
        execstart=f"{py} -m rl_server.rl_server --port 11442",
        environment=f"PYTHONPATH={pythonpath}",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        # The dotted module is provably gone (container migration removed it).
        resolver=_resolver({"rl_server.rl_server": False}),
        deferral=deferral,
    )

    acted = result.retired
    assert len(acted) == 1, "the dotted rl_server.rl_server unit must retire"
    action = acted[0]
    assert action.acted is True
    assert action.unit_name == "rl-server-vcodev.service"
    assert action.condition_id == f"{CONDITION_ID_PREFIX}rl-server-vcodev"
    # disable --now fired with the right argv; the file was backup-moved.
    assert systemctl.calls == [
        ["systemctl", "--user", "disable", "--now", "rl-server-vcodev.service"]
    ]
    assert not unit.exists()
    backup_dir = install_root / RETIRED_UNITS_REL
    backups = list(backup_dir.glob("rl-server-vcodev.service.*.bak"))
    assert len(backups) == 1
    # Backup preserves the dotted module + PYTHONPATH.
    backup_text = backups[0].read_text(encoding="utf-8")
    assert "rl_server.rl_server" in backup_text
    assert "PYTHONPATH=" in backup_text
    # Recorded with the restore command.
    assert len(deferral.entries) == 1
    assert deferral.entries[0].condition_id == f"{CONDITION_ID_PREFIX}rl-server-vcodev"


def test_leave_legacy_rl_server_dotted_module_that_still_resolves(force_linux, env):
    """WP-Q item 4 (control): the SAME dotted shape is LEFT ALONE when the
    module still resolves — the act/leave-alone gate must key on the probe
    verdict, not the unit name shape. Prevents a false retirement of a
    still-working rl_server.rl_server unit."""
    home, install_root, unit_dir = env
    py = str(install_root / ".venv" / "bin" / "python")
    Path(py).parent.mkdir(parents=True)
    Path(py).write_text("#!py\n")
    pythonpath = f"{install_root}/paid-modules/vct-rl-reranker"
    unit = _write_unit(
        unit_dir, "rl-server-vcodev.service",
        execstart=f"{py} -m rl_server.rl_server --port 11442",
        environment=f"PYTHONPATH={pythonpath}",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server.rl_server": True}),  # resolves → keep
        deferral=deferral,
    )

    assert result.retired == [], "a resolving dotted module must NOT retire"
    assert systemctl.calls == [], "no disable when the module resolves"
    assert unit.exists()
    assert deferral.entries == []
    # The one recorded action is a leave-alone with reason module_resolves.
    assert len(result.actions) == 1
    assert result.actions[0].acted is False
    assert result.actions[0].reason == "module_resolves"


def test_act_broken_exec_path_is_retired(force_linux, env):
    home, install_root, unit_dir = env
    missing = str(install_root / "scripts" / "gone.sh")  # never created
    unit = _write_unit(
        unit_dir, "vco-worker.service",
        execstart=f"/usr/bin/env bash {missing}",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(install_root, home, systemctl=systemctl, deferral=deferral)

    assert len(result.retired) == 1
    assert result.retired[0].acted is True
    assert not unit.exists()
    assert len(deferral.entries) == 1


# ---------------------------------------------------------------------------
# ACT: log rotation
# ---------------------------------------------------------------------------


def test_act_rotates_oversized_log(force_linux, env, tmp_path):
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    log_path = tmp_path / "rl-server-demo.log"
    # Write an over-threshold log (10 MB + 1 byte).
    with open(log_path, "wb") as f:
        f.seek(LOG_ROTATE_THRESHOLD_BYTES + 1)
        f.write(b"\0")

    _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
        std_append=str(log_path),
    )
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    action = result.retired[0]
    # Oversized log was backup-MOVED (original gone).
    assert not log_path.exists()
    assert action.log_backup_path is not None
    assert action.log_backup_path.exists()
    assert action.log_backup_path.parent == install_root / RETIRED_UNITS_REL
    # Record mentions the rotated log.
    assert str(action.log_backup_path) in deferral.entries[0].detected


def test_leave_alone_under_threshold_log(force_linux, env, tmp_path):
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    log_path = tmp_path / "small.log"
    log_path.write_bytes(b"x" * 1024)  # 1 KB — under threshold

    _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
        std_append=str(log_path),
    )
    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
    )

    action = result.retired[0]
    # Unit still retired, but the small log is LEFT IN PLACE.
    assert action.acted is True
    assert log_path.exists()
    assert action.log_backup_path is None


# ---------------------------------------------------------------------------
# LEAVE-ALONE: resolvable entrypoints
# ---------------------------------------------------------------------------


def test_leave_alone_module_resolves(force_linux, env):
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": True}),  # resolves
    )

    assert result.retired == []
    assert result.actions[0].reason == "module_resolves"
    # No systemctl mutation, unit untouched.
    assert systemctl.calls == []
    assert unit.exists()


def test_leave_alone_exec_resolves(force_linux, env):
    home, install_root, unit_dir = env
    script = install_root / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    unit = _write_unit(
        unit_dir, "vco-worker.service",
        execstart=f"/usr/bin/env bash {script}",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(install_root, home, systemctl=systemctl)

    assert result.retired == []
    assert result.actions[0].reason == "exec_resolves"
    assert systemctl.calls == []
    assert unit.exists()


# ---------------------------------------------------------------------------
# LEAVE-ALONE: foreign / unparseable / probe-failed
# ---------------------------------------------------------------------------


def test_leave_alone_foreign_unit(force_linux, env):
    home, install_root, unit_dir = env
    # ExecStart references a DIFFERENT root — not ours.
    unit = _write_unit(
        unit_dir, "other-app.service",
        execstart="/opt/other/.venv/bin/python -m other_module",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({}),  # would be broken IF we probed it — we must not
    )

    assert result.retired == []
    assert result.actions[0].reason == "foreign_unit"
    assert systemctl.calls == []  # never touched a foreign unit
    assert unit.exists()


def test_leave_alone_unparseable_execstart(force_linux, env):
    home, install_root, unit_dir = env
    # A unit that references our root in a bare-word command we cannot classify
    # (no -m, not an absolute/relative path) — leave it alone.
    unit_dir_path = unit_dir
    path = unit_dir_path / "weird.service"
    path.write_text(
        "[Service]\n"
        # References the root via PYTHONPATH (so it's "ours") but ExecStart is
        # a bare word — unclassifiable → leave alone.
        f"Environment=PYTHONPATH={install_root}\n"
        "ExecStart=somecommand\n",
        encoding="utf-8",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(install_root, home, systemctl=systemctl)

    assert result.retired == []
    assert result.actions[0].reason == "execstart_unparseable"
    assert systemctl.calls == []
    assert path.exists()


def test_leave_alone_no_execstart_line(force_linux, env):
    home, install_root, unit_dir = env
    path = unit_dir / "noexec.service"
    path.write_text(f"[Service]\nEnvironment=PYTHONPATH={install_root}\n",
                    encoding="utf-8")
    result = _reconcile(install_root, home)
    assert result.retired == []
    assert result.actions[0].reason == "execstart_unparseable"
    assert path.exists()


def test_leave_alone_probe_failed_missing_interpreter(force_linux, env):
    """A broken-looking module whose CONFIGURED interpreter does not exist is a
    probe FAILURE (leave alone), not proof the module is gone."""
    home, install_root, unit_dir = env
    absent_py = str(install_root / ".venv" / "bin" / "python")  # never created
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{absent_py} -m rl_server",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": False}),
    )

    assert result.retired == []
    assert result.actions[0].reason == "probe_failed"
    assert systemctl.calls == []  # never disabled — could not verify
    assert unit.exists()


# ---------------------------------------------------------------------------
# LEAVE-ALONE: systemctl absent / non-Linux / systemctl error
# ---------------------------------------------------------------------------


def test_skip_when_systemctl_absent(force_linux, env):
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    result = _reconcile(
        install_root, home,
        available=False,  # systemctl not present
        resolver=_resolver({"rl_server": False}),
    )
    # Whole pass skipped — no per-unit actions.
    assert result.actions == []
    assert unit.exists()


def test_skip_on_non_linux(monkeypatch, env):
    import vco_lib.unit_reconcile as ur

    monkeypatch.setattr(ur.platform, "system", lambda: "Darwin")
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
    )
    assert result.actions == []
    assert unit.exists()


def test_leave_alone_on_systemctl_error(force_linux, env):
    """systemctl disable returns non-zero → do NOT move the file behind
    systemd's back; leave the unit as-is."""
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl(rc=1, stderr="Failed to disable unit")
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    # It TRIED to disable (systemctl invoked) but the error → leave-alone.
    assert systemctl.calls == [
        ["systemctl", "--user", "disable", "--now", "rl-server-demo.service"]
    ]
    assert result.retired == []
    assert result.actions[0].reason == "systemctl_error"
    # File NOT moved (recoverable), no record written.
    assert unit.exists()
    assert deferral.entries == []


# ---------------------------------------------------------------------------
# Robustness: symlink / unreadable / PYTHONPATH-referenced root
# ---------------------------------------------------------------------------


def test_leave_alone_symlinked_unit(force_linux, env, tmp_path):
    """A symlinked unit (system-unit alias / user redirect) is never touched."""
    home, install_root, unit_dir = env
    target = tmp_path / "elsewhere.service"
    target.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    link = unit_dir / "aliased.service"
    link.symlink_to(target)

    result = _reconcile(install_root, home)
    assert result.retired == []
    assert result.actions[0].reason == "symlink_left_alone"
    assert link.is_symlink()


def test_module_referenced_via_pythonpath_is_ours(force_linux, env):
    """A unit whose ExecStart uses a system python but whose PYTHONPATH points
    at our root IS ours — and its broken module is retired."""
    home, install_root, unit_dir = env
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart="/usr/bin/python3 -m rl_server",
        environment=f"PYTHONPATH={install_root}/src",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        # /usr/bin/python3 is absolute; it exists on the test host, so the
        # probe runs and returns broken.
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    assert len(result.retired) == 1
    assert not unit.exists()
    assert len(deferral.entries) == 1


def test_soft_fail_on_unreadable_unit(force_linux, env, monkeypatch):
    home, install_root, unit_dir = env
    py = str(install_root / "py")
    Path(py).write_text("#!py\n")
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    real_read = Path.read_text

    def boom(self, *a, **kw):
        if self == unit:
            raise OSError("permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)

    # Must not raise; unit is left alone with an 'unreadable' reason.
    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
    )
    assert result.retired == []
    assert result.actions[0].reason == "unreadable"
    assert unit.exists()


def test_no_unit_dir_is_noop(force_linux, tmp_path):
    """No ~/.config/systemd/user dir at all → clean no-op."""
    home = tmp_path / "home"
    home.mkdir()
    install_root = tmp_path / "install"
    install_root.mkdir()
    result = _reconcile(install_root, home)
    assert result.actions == []


# ---------------------------------------------------------------------------
# Integration: install.py thin shim wiring (RED-PROOF anchor)
# ---------------------------------------------------------------------------


def test_install_shim_gates_on_update_and_calls_reconcile(monkeypatch, tmp_path):
    """install._reconcile_stale_units_step is the thin wiring: it must call
    vco_lib.unit_reconcile.reconcile_stale_units ONLY on --update, threading
    the deferral report through.

    This is the RED-PROOF anchor for the call site: removing the call from
    main() (pre-fix shape) makes this fail (the spy is never invoked); it is
    green with the wiring in place.
    """
    import argparse
    import install  # type: ignore

    calls = {}

    def spy(install_root, *, deferral_report=None, **kw):
        calls["install_root"] = install_root
        calls["deferral_report"] = deferral_report
        from vco_lib.unit_reconcile import ReconcileResult
        return ReconcileResult()

    monkeypatch.setattr("vco_lib.unit_reconcile.reconcile_stale_units", spy)

    report = DeferralReport()

    # Fresh install (no --update) → shim is a no-op, reconcile NOT called.
    args_fresh = argparse.Namespace(update=False)
    install._reconcile_stale_units_step(tmp_path, args_fresh, report)
    assert calls == {}

    # --update → reconcile IS called with the report threaded through.
    args_update = argparse.Namespace(update=True)
    install._reconcile_stale_units_step(tmp_path, args_update, report)
    assert calls["install_root"] == tmp_path
    assert calls["deferral_report"] is report


def test_n5_shim_loud_fails_on_import_error(monkeypatch, tmp_path):
    """N-5: an ImportError of the shipped ``vco_lib.unit_reconcile`` module (a
    broken install) must ESCAPE the shim (loud-fail), NOT degrade to a swallowed
    warn. The import lives OUTSIDE the shim's soft-fail try for exactly this.
    """
    import argparse
    import install  # type: ignore
    import vco_lib.unit_reconcile as ur

    # Remove the symbol so `from ... import reconcile_stale_units` raises
    # ImportError inside the shim.
    monkeypatch.delattr(ur, "reconcile_stale_units", raising=True)

    args_update = argparse.Namespace(update=True)
    with pytest.raises(ImportError):
        install._reconcile_stale_units_step(tmp_path, args_update, None)


def test_n8_shim_maps_warning_level_to_warn_phase(monkeypatch, tmp_path):
    """N-8: the shim's log adapter must thread the reconcile log LEVEL into the
    install-event phase — a warning is logged as phase="warn", an info as "ok".
    A regression that flattens every line to "ok" fails this.
    """
    import argparse
    import install  # type: ignore

    events = []
    monkeypatch.setattr(
        install, "_log_install_event",
        lambda step, phase, detail="", **kw: events.append((phase, detail)),
    )

    def fake_reconcile(install_root, *, deferral_report=None, log=None, **kw):
        # Drive the adapter at both levels.
        log("an info line", level="info")
        log("a warning line", level="warning")
        from vco_lib.unit_reconcile import ReconcileResult
        return ReconcileResult()

    monkeypatch.setattr(
        "vco_lib.unit_reconcile.reconcile_stale_units", fake_reconcile
    )

    args_update = argparse.Namespace(update=True)
    install._reconcile_stale_units_step(tmp_path, args_update, None)

    phases = {detail: phase for phase, detail in events}
    assert phases["an info line"] == "ok"
    assert phases["a warning line"] == "warn"


def test_call_site_present_in_main_only():
    """RED-PROOF call-site pin: the reconcile shim must actually be CALLED from
    the ONE live update entry point — main()'s ``install.py --update`` flow,
    which backs the launcher's Settings→Updates "Update orchestrator" path
    (installer.rs::update_orchestrator spawns ``install.py --update``).

    L-3: the previous lightweight call site was DEAD CODE. The launcher's
    lightweight argv (installer.rs::build_lightweight_install_argv) carries NO
    ``--update``, so the shim's ``args.update`` gate made that call a permanent
    no-op. It was removed, leaving exactly one live wiring.

    Removing the main() call (the pre-fix / regressed shape) drops the count
    below 2 and this fails — the source-level guarantee that the reconcile step
    is wired in, not merely defined. Re-adding a lightweight call also fails
    (see the dead-leg assertion below), keeping the dead wiring from creeping
    back.
    """
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    # One definition + one live call site = exactly 2 mentions.
    assert src.count("_reconcile_stale_units_step") == 2, (
        "reconcile shim call count changed: expected exactly 2 mentions "
        "(1 def + 1 live main() call). Either the main() call was removed "
        "(reconcile no longer wired in) or a DEAD lightweight call crept back "
        "(L-3: the lightweight argv has no --update, so such a call no-ops)."
    )
    # The full-update call threads the run-scoped _deferral_report.
    assert "_reconcile_stale_units_step(PROJECT_ROOT, args, _deferral_report)" in src
    # The dead lightweight call (which gated on args.update and never fired on
    # the launcher's --lightweight argv) must NOT be present.
    assert (
        "_reconcile_stale_units_step(PROJECT_ROOT, args, _lightweight_deferral)"
        not in src
    ), (
        "the dead lightweight reconcile call site is back — the launcher's "
        "--lightweight argv has no --update so this call can never fire (L-3)"
    )


# ---------------------------------------------------------------------------
# L-1: `-m` classifier — only a real `python -m <mod>` BEFORE the first
# positional is a module invocation. A `-m` after a script path, or under a
# non-python head, is NOT a module flag (false-retirement guard).
# ---------------------------------------------------------------------------


def _make_python(install_root: Path, name: str = "python") -> str:
    """Create a fake python-named interpreter under the install root."""
    py = install_root / ".venv" / "bin" / name
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!py\n")
    return str(py)


def test_l1_python_script_with_trailing_dash_m_is_not_retired(force_linux, env):
    """`python server.py --mode x -m y` — the `-m y` is a SCRIPT arg, not the
    module flag. The unit must be classified as exec on server.py (which
    exists) and LEFT ALONE — never probed for a phantom module `y` and retired.

    This is the L-1 destructive false positive: a healthy unit whose script
    happens to pass `-m` to its own program was being RETIRED.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    # A real script under our root that EXISTS and is executable.
    script = install_root / "server.py"
    script.write_text("print('hi')\n")
    script.chmod(0o755)
    unit = _write_unit(
        unit_dir, "app.service",
        execstart=f"{py} {script} --mode x -m y",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        # If the classifier WRONGLY read `-m y`, it would probe module `y`
        # (default-False → broken) and retire. The mapping leaves `y` False to
        # PROVE the fix: we must not even reach the probe.
        resolver=_resolver({"y": False}),
    )

    assert result.retired == []
    assert result.actions[0].reason == "exec_resolves"
    assert systemctl.calls == []
    assert unit.exists()


def test_l1_non_python_head_with_dash_m_is_not_retired(force_linux, env):
    """`/root/bin/tool serve -m production` — `tool` is NOT a python
    interpreter, so its `-m` is a subcommand option, never the module flag.
    A missing `tool` binary is checked as an EXEC path (legitimate), NOT probed
    as a phantom module `production` and retired via a garbage subprocess.

    Here `tool` exists → exec_resolves → leave-alone. The key guarantee: the
    decision is driven by an exec check, not a false module probe.
    """
    home, install_root, unit_dir = env
    tool = install_root / "bin" / "tool"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    unit = _write_unit(
        unit_dir, "tool.service",
        execstart=f"{tool} serve -m production",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"production": False}),  # must NOT be consulted
    )

    assert result.retired == []
    assert result.actions[0].reason == "exec_resolves"
    assert systemctl.calls == []
    assert unit.exists()


def test_l1_genuine_python_dash_m_before_positional_still_retired(force_linux, env):
    """The fix must NOT over-correct: a genuine `python -m gone_module` (with
    `-m` before any positional) is STILL classified as a module and retired
    when the module is broken. Interpreter options before `-m` (`-W ignore`)
    are skipped correctly.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -W ignore -m gone_module --flag",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"gone_module": False}),  # provably broken
        deferral=deferral,
    )

    assert len(result.retired) == 1
    assert result.retired[0].acted is True
    assert not unit.exists()
    assert len(deferral.entries) == 1
    assert systemctl.calls == [
        ["systemctl", "--user", "disable", "--now", "rl.service"]
    ]


# ---------------------------------------------------------------------------
# L-2: probe tri-state — "could not run" (env -S, relative interp, timeout,
# rc≠0, find_spec raised) is a leave-alone, NEVER a retire. Only a CLEAN run
# that positively reports module-not-found retires.
# ---------------------------------------------------------------------------


def test_l2_env_dash_s_form_probe_error_is_left_alone(force_linux, env):
    """`/usr/bin/env -S python -m rl_server` (the systemd env -S idiom) — after
    env-unwrap the interpreter token is `-S`, which cannot be started (OSError).
    That is a PROBE FAILURE, not proof `rl_server` is gone → LEAVE ALONE.

    Uses the REAL default resolver (no injected resolver) so the tri-state is
    exercised end-to-end: the resolver returns None (OSError starting `-S`) and
    _probe_module propagates None → probe_failed.
    """
    home, install_root, unit_dir = env
    # Reference our root via PYTHONPATH so the unit is "ours", then the exec is
    # `env -S python -m rl_server` — env-unwrap → `-S python -m rl_server` →
    # head `-S` (not a python interp, not path-like) …
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart="/usr/bin/env -S python -m rl_server",
        environment=f"PYTHONPATH={install_root}/src",
    )
    systemctl = FakeSystemctl()

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        # NO resolve_module → the REAL default subprocess resolver runs.
        log=None,
    )

    assert result.retired == []
    # `-S` is neither a python interp nor path-like; `python`/`rl_server` aren't
    # path-like → the whole shape is unclassifiable → leave alone. Either way it
    # is NOT retired and systemctl is never touched.
    assert result.actions[0].reason in ("execstart_unparseable", "probe_failed")
    assert systemctl.calls == []
    assert unit.exists()


def test_l2_relative_interpreter_not_on_path_probe_error_left_alone(
    force_linux, env
):
    """A PATH-relative interpreter that this process cannot start (systemd's
    PATH differs) → the real resolver hits OSError → None → leave alone.

    `python -m rl_server` with a bogus interpreter name that is NOT on PATH:
    the default subprocess resolver raises OSError → returns None → probe_failed.
    """
    home, install_root, unit_dir = env
    # A bare, PATH-relative python interpreter (`python99`) that is a valid
    # python-interpreter NAME (basename matches `python[0-9.]*`) but is NOT on
    # this process's PATH — so it IS classified as a module invocation, but the
    # subprocess cannot start it. Reference our root via PYTHONPATH so the unit
    # is "ours".
    unit = _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart="python99 -m rl_server",
        environment=f"PYTHONPATH={install_root}/src",
    )
    systemctl = FakeSystemctl()

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        # REAL default resolver: `python99` is treated as a python interp so it
        # IS classified as a module — but the subprocess can't start it →
        # OSError → None → probe_failed (not a retire).
        log=None,
    )

    assert result.retired == []
    assert result.actions[0].reason == "probe_failed"
    assert systemctl.calls == []
    assert unit.exists()


def test_l2_injected_resolver_none_is_probe_failed_not_retire(force_linux, env):
    """A resolver returning None (tri-state "probe could not run") must yield
    probe_failed / leave-alone — the None must NOT be bool()-coerced to False
    and drive a retirement. This pins the exact L-2 coercion bug.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl()

    def resolver_returns_none(python, module, pythonpath):
        return None  # probe could not run reliably

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        resolve_module=resolver_returns_none,
        log=None,
    )

    assert result.retired == []
    assert result.actions[0].reason == "probe_failed"
    assert systemctl.calls == []
    assert unit.exists()


def test_l2_clean_missing_module_still_retired(force_linux, env):
    """Control: a resolver returning False (clean "provably missing") STILL
    retires — the fix must not turn every probe into a leave-alone.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        resolve_module=lambda p, m, pp: False,  # clean provably-missing
        deferral_report=deferral,
        log=None,
    )

    assert len(result.retired) == 1
    assert not unit.exists()
    assert len(deferral.entries) == 1


def test_l2_default_resolver_tristate_direct():
    """Unit-level pin on the default resolver's tri-state (L-2):
      - genuine interpreter + missing module → False (clean MISSING)
      - genuine interpreter + present module → True
      - unstartable interpreter (`-S`)       → None (probe failed)
    """
    import shutil as _shutil

    from vco_lib.unit_reconcile import _resolve_module_default

    py = _shutil.which("python3") or sys.executable
    assert _resolve_module_default(py, "no_such_module_zzz_qwerty", "") is False
    assert _resolve_module_default(py, "os", "") is True
    assert _resolve_module_default("-S", "rl_server", "") is None


# ---------------------------------------------------------------------------
# M-1: anchored root reference — a sibling install must not match.
# ---------------------------------------------------------------------------


def test_m1_sibling_install_is_foreign(force_linux, env):
    """A unit whose ExecStart references `<root>2` (a SIBLING install) must be
    classified FOREIGN — an unanchored substring test would have claimed it.
    """
    home, install_root, unit_dir = env
    sibling = Path(str(install_root) + "2")  # e.g. /tmp/.../install2
    py = sibling / ".venv" / "bin" / "python"
    unit = _write_unit(
        unit_dir, "sibling.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": False}),  # would retire IF matched
    )

    assert result.retired == []
    assert result.actions[0].reason == "foreign_unit"
    assert systemctl.calls == []
    assert unit.exists()


# ---------------------------------------------------------------------------
# M-2: disable succeeds + backup-move fails → warning record + re-enable cmd.
# ---------------------------------------------------------------------------


def test_m2_disable_without_backup_emits_record(force_linux, env, monkeypatch):
    """When `disable --now` succeeds but the unit-file backup-move fails, a
    warning-severity DeferralEntry must be emitted naming the disable and the
    re-enable command — the mutation must not be invisible.
    """
    import vco_lib.unit_reconcile as ur

    home, install_root, unit_dir = env
    py = _make_python(install_root)
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    # Force the unit-file backup-move to fail (returns None).
    monkeypatch.setattr(ur, "_backup_move", lambda src, dst_dir, name: None)

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    # Disable was attempted; overall action is NOT a retire (backup failed).
    assert systemctl.calls == [
        ["systemctl", "--user", "disable", "--now", "rl.service"]
    ]
    assert result.retired == []
    assert result.actions[0].reason == "backup_failed"
    # But the mutation IS recorded (M-2) — warning severity, re-enable command.
    assert len(deferral.entries) == 1
    entry = deferral.entries[0]
    assert entry.severity == "warning"
    assert "disable --now" in entry.detected
    assert "systemctl --user enable --now" in entry.command_to_apply
    assert "rl.service" in entry.command_to_apply
    # File left in place (recoverable) since the move failed.
    assert unit.exists()


# ---------------------------------------------------------------------------
# M-3: log rotation — symlink guard + collision-safe naming.
# ---------------------------------------------------------------------------


def test_m3_symlinked_log_is_not_followed(force_linux, env, tmp_path):
    """A symlinked `append:` log is NEVER followed/rotated (moving it would
    rotate the link TARGET and break the link). The unit is still retired; the
    symlinked log is left in place and no log backup is recorded.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root, name="py")
    real_log = tmp_path / "real.log"
    with open(real_log, "wb") as f:  # over-threshold target
        f.seek(LOG_ROTATE_THRESHOLD_BYTES + 1)
        f.write(b"\0")
    link = tmp_path / "linked.log"
    link.symlink_to(real_log)

    _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
        std_append=str(link),
    )
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    action = result.retired[0]
    assert action.acted is True  # unit still retired
    # The symlink and its target are both untouched.
    assert link.is_symlink()
    assert real_log.exists()
    assert action.log_backup_path is None  # symlinked log NOT rotated


def test_m3_same_named_logs_from_two_units_do_not_collide(
    force_linux, env, tmp_path, monkeypatch
):
    """Two units retired in the SAME pass with same-named (`server.log`) logs
    must not clobber each other's backup — the unit slug in the backup name
    keeps them distinct even under a frozen (same-second) stamp.
    """
    import vco_lib.unit_reconcile as ur

    home, install_root, unit_dir = env
    py = _make_python(install_root, name="py")

    # Freeze the stamp so both units share the second-stamp (the collision
    # precondition the slug prefix must defeat).
    monkeypatch.setattr(ur, "_utc_stamp", lambda: "20260101T000000Z")

    # Two units, each with its OWN directory but a same-named oversized log.
    logs = []
    for i in (1, 2):
        d = tmp_path / f"u{i}"
        d.mkdir()
        lp = d / "server.log"
        with open(lp, "wb") as f:
            f.seek(LOG_ROTATE_THRESHOLD_BYTES + 1)
            f.write(b"\0")
        logs.append(lp)
        _write_unit(
            unit_dir, f"unit{i}.service",
            execstart=f"{py} -m rl_server",
            std_append=str(lp),
        )

    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
    )

    # Both units retired, both logs rotated to DISTINCT backups (no overwrite).
    retired = result.retired
    assert len(retired) == 2
    backups = [a.log_backup_path for a in retired]
    assert all(b is not None for b in backups)
    assert backups[0] != backups[1]
    assert all(b.exists() for b in backups)
    # Both backup names carry the second-stamp but differ by unit slug.
    names = sorted(b.name for b in backups)
    assert names[0].startswith("unit1")
    assert names[1].startswith("unit2")


# ---------------------------------------------------------------------------
# M-4: durable RESTORE sidecar survives record self-clearing.
# ---------------------------------------------------------------------------


def test_m4_restore_sidecar_written_next_to_backup(force_linux, env):
    """A retirement writes a `<backup>.RESTORE.txt` sidecar next to the `.bak`
    containing the exact restore command — so the instructions survive the
    deferral record's one-cycle self-clear.
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    _write_unit(
        unit_dir, "rl-server-demo.service",
        execstart=f"{py} -m rl_server",
    )
    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
    )

    action = result.retired[0]
    sidecar = action.backup_path.parent / f"{action.backup_path.name}.RESTORE.txt"
    assert sidecar.exists()
    body = sidecar.read_text(encoding="utf-8")
    assert "rl-server-demo.service" in body
    assert "systemctl --user enable --now" in body
    assert str(action.backup_path) in body


# ---------------------------------------------------------------------------
# N-4: restore command shell-quotes paths (install root with spaces).
# ---------------------------------------------------------------------------


def test_n4_restore_command_shell_quotes_spaced_paths(force_linux, tmp_path):
    """An install root containing a space must produce a copy-paste-safe restore
    command (quoted backup path) — an unquoted `cp <path with space>` breaks.
    """
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    install_root = tmp_path / "install root with spaces"
    install_root.mkdir()
    unit_dir = home / ".config" / "systemd" / "user"
    py = _make_python(install_root)
    _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
    )
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home,
        resolver=_resolver({"rl_server": False}),
        deferral=deferral,
    )

    action = result.retired[0]
    cmd = deferral.entries[0].command_to_apply
    # The spaced backup path must be shell-quoted so `cp` sees one token.
    assert "install root with spaces" in str(action.backup_path)
    assert "'" in cmd  # shlex.quote wraps the spaced path in single quotes
    # And the sidecar carries the same quoted command.
    sidecar = action.backup_path.parent / f"{action.backup_path.name}.RESTORE.txt"
    assert "'" in sidecar.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R2-8: interpreter-run scripts must NOT be gated on the execute bit.
# A 644 script (the normal mode for an interpreter-run file) is HEALTHY —
# `python script.py` / `bash wrapper.sh` READ the file; only direct-exec heads
# need +x. The L-1 fix routed `python <script>` into the exec probe, and that
# probe wrongly required X_OK, turning healthy 644 scripts into false retires.
# ---------------------------------------------------------------------------


def test_r2_8_interpreter_run_644_script_is_left_alone(force_linux, env):
    """Reproduces the reviewer's live R2-8 repro: a HEALTHY unit
    `<root>/.venv/bin/python <root>/server.py --mode x -m y` whose script is
    chmod 644 must be LEFT ALONE (previously RETIRED on the missing exec bit).
    """
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    script = install_root / "server.py"
    script.write_text("print('hi')\n")
    script.chmod(0o644)  # interpreter-run: readable but NOT executable
    unit = _write_unit(
        unit_dir, "app.service",
        execstart=f"{py} {script} --mode x -m y",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(
        install_root, home,
        systemctl=systemctl,
        resolver=_resolver({"y": False}),  # must not even be consulted
    )

    assert result.retired == []
    assert result.actions[0].reason == "exec_resolves"
    assert systemctl.calls == []
    assert unit.exists()


def test_r2_8_bash_644_wrapper_is_left_alone(force_linux, env):
    """`bash <root>/wrapper.sh` with a 644 wrapper (git core.fileMode=false
    checkouts) is healthy — bash reads it. Leave alone."""
    home, install_root, unit_dir = env
    script = install_root / "scripts" / "wrapper.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)  # readable, not executable
    unit = _write_unit(
        unit_dir, "vco-worker.service",
        execstart=f"/usr/bin/env bash {script}",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(install_root, home, systemctl=systemctl)

    assert result.retired == []
    assert result.actions[0].reason == "exec_resolves"
    assert systemctl.calls == []
    assert unit.exists()


def test_r2_8_missing_interpreter_run_script_still_retired(force_linux, env):
    """Control: an interpreter-run script that DOES NOT EXIST is still a broken
    exec → retire. The R2-8 relaxation is only about the execute bit, not
    existence — a genuinely-absent script is a real breakage."""
    home, install_root, unit_dir = env
    py = _make_python(install_root)
    missing = install_root / "gone.py"  # never created
    unit = _write_unit(
        unit_dir, "app.service",
        execstart=f"{py} {missing}",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home, systemctl=systemctl, deferral=deferral,
    )

    assert len(result.retired) == 1
    assert result.retired[0].acted is True
    assert not unit.exists()


def test_r2_8_direct_exec_head_still_needs_execute_bit(force_linux, env):
    """Control: a DIRECT-exec head (`<root>/bin/worker`, no interpreter) that
    exists but is 644 is genuinely broken — the kernel cannot exec it. The X_OK
    gate MUST remain for direct-exec heads (only interpreter-run scripts are
    relaxed)."""
    home, install_root, unit_dir = env
    worker = install_root / "bin" / "worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("#!/bin/sh\n")
    worker.chmod(0o644)  # exists but NOT executable → kernel exec fails
    unit = _write_unit(
        unit_dir, "worker.service",
        execstart=f"{worker} --flag",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = _reconcile(
        install_root, home, systemctl=systemctl, deferral=deferral,
    )

    assert len(result.retired) == 1, "a 644 direct-exec head is a real breakage"
    assert result.retired[0].acted is True
    assert not unit.exists()


# ---------------------------------------------------------------------------
# R2-10: `bash|sh -c '<command string>'` is unparseable → leave alone.
# The quoted -c command string is one shlex token containing `/` → it would be
# _is_path_like → classified as an exec PATH → isfile fails → false retire.
# Mirror the python `-c` rule for shell heads.
# ---------------------------------------------------------------------------


def test_r2_10_bash_dash_c_command_string_is_left_alone(force_linux, env):
    """`bash -c '<root>/scripts/foo.sh --daemon'` references our root inside a
    -c COMMAND STRING (not a script path). It must be unparseable → leave alone,
    never classified as an exec on the whole command string."""
    home, install_root, unit_dir = env
    # The -c arg contains our root but is a command string, not a file path.
    unit = _write_unit(
        unit_dir, "shell.service",
        execstart=f"/bin/bash -c '{install_root}/scripts/foo.sh --daemon'",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(install_root, home, systemctl=systemctl)

    assert result.retired == []
    assert result.actions[0].reason == "execstart_unparseable"
    assert systemctl.calls == []
    assert unit.exists()


def test_r2_10_sh_dash_c_command_string_is_left_alone(force_linux, env):
    """Same for `sh -c` — the POSIX shell family all treat -c as an inline
    command string."""
    home, install_root, unit_dir = env
    unit = _write_unit(
        unit_dir, "shell.service",
        execstart=f"/usr/bin/env sh -c '{install_root}/bin/run.sh'",
    )
    systemctl = FakeSystemctl()

    result = _reconcile(install_root, home, systemctl=systemctl)

    assert result.retired == []
    assert result.actions[0].reason == "execstart_unparseable"
    assert systemctl.calls == []
    assert unit.exists()


# ---------------------------------------------------------------------------
# R2-13: a False verdict from a PATH-relative interpreter is untrustworthy
# (this process's PATH may differ from systemd's) → downgrade to probe_failed
# (leave alone). A True verdict stays trustworthy.
# ---------------------------------------------------------------------------


def test_r2_13_relative_interpreter_false_is_probe_failed(force_linux, env):
    """`python3 -m rl_server` (PATH-relative interpreter): an injected resolver
    that returns False must NOT drive a retire, because the False came from a
    different `python3` than the unit's own → leave alone."""
    home, install_root, unit_dir = env
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart="python3 -m rl_server",
        environment=f"PYTHONPATH={install_root}/src",  # marks the unit as ours
    )
    systemctl = FakeSystemctl()

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        resolve_module=_resolver({"rl_server": False}),  # clean False
        log=None,
    )

    assert result.retired == []
    assert result.actions[0].reason == "probe_failed"
    assert systemctl.calls == []
    assert unit.exists()


def test_r2_13_relative_interpreter_true_still_trusted(force_linux, env):
    """Control: a True verdict from a PATH-relative interpreter stays
    trustworthy (a resolvable python3 that HAS the module proves it's
    installed) → leave alone as module_resolves."""
    home, install_root, unit_dir = env
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart="python3 -m rl_server",
        environment=f"PYTHONPATH={install_root}/src",
    )
    systemctl = FakeSystemctl()

    result = reconcile_stale_units(
        install_root,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        resolve_module=_resolver({"rl_server": True}),  # resolves
        log=None,
    )

    assert result.retired == []
    assert result.actions[0].reason == "module_resolves"
    assert systemctl.calls == []
    assert unit.exists()


def test_r2_13_absolute_interpreter_false_still_retires(force_linux, env):
    """Control: a False verdict from an ABSOLUTE interpreter is trustworthy
    (that exact interpreter reported the module missing) → still retires. The
    R2-13 downgrade applies ONLY to non-absolute interpreters."""
    home, install_root, unit_dir = env
    py = _make_python(install_root)  # absolute path under our root
    unit = _write_unit(
        unit_dir, "rl.service",
        execstart=f"{py} -m rl_server",
    )
    systemctl = FakeSystemctl(rc=0)
    deferral = DeferralReport()

    result = reconcile_stale_units(
        install_root,
        deferral_report=deferral,
        home=home,
        systemctl_available=lambda: True,
        systemctl_runner=systemctl,
        resolve_module=_resolver({"rl_server": False}),  # clean, provable
        log=None,
    )

    assert len(result.retired) == 1
    assert result.retired[0].acted is True
    assert not unit.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
