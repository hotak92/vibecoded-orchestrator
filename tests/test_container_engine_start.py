# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the ONE home of the container-engine auto-start logic.

``vco_lib/container_engine_start.py`` (owner ruling 2026-10-15: the engine
"should be made to auto-start with launcher/hub/VS Code"). These tests pin
the contract with fakes only — no Docker Desktop, no podman machine, no
socket, no network is ever started:

* idempotent — an engine that answers is a no-op (no start invoked);
* opt-out — ``VCT_CONTAINER_ENGINE_AUTOSTART`` falsy → no-op before any probe;
* only the engine this install uses — a refused PIN is started, the other
  runtime never is; nothing installed → nothing started;
* the per-OS branch selection (Docker Desktop on Windows / ``open -a Docker``
  on macOS / Linux not automated) and the bounded liveness wait (liveness
  only — nothing is killed);
* the CLI verb and install.py's delegation to the same functions.

Red-proofed by mutation: each behaviour was verified failing against the
pre-fix tree (see the lane report).
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib import container_engine_start as ces  # noqa: E402
from vco_lib import containers as _c  # noqa: E402
from vco_lib import compose_provider as _cp  # noqa: E402


def _resolution(state, *, installed=None, requested=None, requested_installed=False,
                reason="because") -> _c.RuntimeResolution:
    return _c.RuntimeResolution(
        state=state,
        runtime="docker" if state is _c.RuntimeState.RESOLVED else None,
        installed=installed,
        compose=["docker", "compose"] if state is _c.RuntimeState.RESOLVED else None,
        compose_form="plugin" if state is _c.RuntimeState.RESOLVED else None,
        daemon_responsive=True if state is _c.RuntimeState.RESOLVED else False,
        reason=reason,
        requested=requested,
        requested_installed=requested_installed,
    )


class _Stopped:
    """A fake clock/sleep pair that never advances — the wait loop runs its
    body at most once per poll and the test controls readiness."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class AutostartEnabledTests(unittest.TestCase):
    def test_default_enabled(self):
        self.assertTrue(ces.autostart_enabled({}))
        self.assertTrue(ces.autostart_enabled({"VCT_CONTAINER_ENGINE_AUTOSTART": "1"}))

    def test_falsy_values_disable(self):
        for falsy in ("0", "false", "no", "off", "OFF", " No "):
            with self.subTest(falsy=falsy):
                self.assertFalse(
                    ces.autostart_enabled({"VCT_CONTAINER_ENGINE_AUTOSTART": falsy}))


class EnsureEngineTests(unittest.TestCase):
    def setUp(self):
        self.start_docker = mock.MagicMock(return_value=(True, "daemon responsive"))
        self.heal_podman = mock.MagicMock(
            return_value=_cp.HealResult(True, ["systemctl --user start podman.socket"],
                                        "socket started"))
        self.resolve_fn = mock.MagicMock(
            return_value=_resolution(_c.RuntimeState.ABSENT, installed="docker"))

    def _ensure(self, **kw):
        kw.setdefault("resolve_fn", self.resolve_fn)
        kw.setdefault("start_docker", self.start_docker)
        kw.setdefault("heal_podman", self.heal_podman)
        return ces.ensure_engine(**kw)

    def test_opt_out_is_a_noop_before_any_probe(self):
        ok, detail = self._ensure(env={"VCT_CONTAINER_ENGINE_AUTOSTART": "0"})
        self.assertFalse(ok)
        self.assertIn("disabled", detail)
        self.assertIn(ces.VCT_CONTAINER_ENGINE_AUTOSTART, detail)
        self.resolve_fn.assert_not_called()
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_engine_already_up_is_a_noop(self):
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.RESOLVED)
        ok, detail = self._ensure()
        self.assertTrue(ok)
        self.assertIn("already up", detail)
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_engine_down_starts_it_once(self):
        ok, _ = self._ensure()
        self.assertTrue(ok)
        self.start_docker.assert_called_once_with()
        self.heal_podman.assert_not_called()

    def test_unknown_state_starts_nothing(self):
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.UNKNOWN)
        ok, detail = self._ensure()
        self.assertFalse(ok)
        self.assertIn("unknown", detail)
        self.start_docker.assert_not_called()

    def test_nothing_installed_starts_nothing(self):
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.ABSENT, installed=None)
        ok, detail = self._ensure()
        self.assertFalse(ok)
        self.assertIn("no container engine installed", detail)
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_installed_podman_takes_the_podman_branch(self):
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.ABSENT, installed="podman")
        ok, _ = self._ensure()
        self.assertTrue(ok)
        self.heal_podman.assert_called_once_with()
        self.start_docker.assert_not_called()

    def test_a_down_pin_is_started_never_swapped(self):
        # podman pinned, podman's binary present, docker also on PATH — the
        # starter must start PODMAN (BLOCKER-4: never the other runtime).
        self.resolve_fn.return_value = _resolution(
            _c.RuntimeState.ABSENT, installed="docker",
            requested="podman", requested_installed=True)
        ok, _ = self._ensure()
        self.assertTrue(ok)
        self.heal_podman.assert_called_once_with()
        self.start_docker.assert_not_called()

    def test_a_missing_pin_starts_nothing_not_the_other_runtime(self):
        self.resolve_fn.return_value = _resolution(
            _c.RuntimeState.ABSENT, installed="docker",
            requested="podman", requested_installed=False)
        ok, detail = self._ensure()
        self.assertFalse(ok)
        self.assertIn("not installed", detail)
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_failure_soft_fails_with_one_log_line(self):
        self.start_docker.return_value = (False, "Docker Desktop.exe not found")
        logs: list[str] = []
        ok, detail = self._ensure(log=logs.append)
        self.assertFalse(ok)
        self.assertIn("not found", detail)
        self.assertEqual(len(logs), 1)
        self.assertIn("failed", logs[0])


class TryStartDockerDaemonTests(unittest.TestCase):
    """The moved install.py starter: per-OS branch + bounded liveness."""

    def _windows_env(self, tmp_path: Path):
        exe = tmp_path / "pf" / "Docker" / "Docker" / "Docker Desktop.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"x")
        return {"ProgramFiles": str(tmp_path / "pf"), "LOCALAPPDATA": ""}, exe

    def test_windows_shell_starts_docker_desktop_detached(self):
        import os
        import tempfile

        from vco_lib import install_companions as ic

        with tempfile.TemporaryDirectory() as td:
            env, exe = self._windows_env(Path(td))
            seen: list = []

            class _Spawned(Exception):
                pass

            def _recording_popen(argv, **kw):
                seen.append((argv, kw))
                raise _Spawned()

            clock = _Stopped()
            # A polluted parent env (the relaunch record install.py inherited
            # from an older run) must NOT reach the long-lived child — the
            # env-scrub contract the old install.py test pinned, kept at the
            # function's new home.
            record = {ic.ENV_RELAUNCHED: "1", ic.ENV_RELAUNCH_TOKEN: "feedface"}
            with mock.patch.dict(os.environ, record):
                with self.assertRaises(_Spawned):
                    ces.try_start_docker_daemon(
                        system="Windows", env=env,
                        which=lambda n: f"/bin/{n}" if n == "docker" else None,
                        popen=_recording_popen, reachable=lambda: False,
                        monotonic=clock.monotonic, sleep=clock.sleep,
                        wait_s=60.0, poll_sleep_s=2.0,
                    )
            self.assertEqual(len(seen), 1)
            argv, kw = seen[0]
            self.assertEqual(argv[-1], str(exe))
            self.assertIn("cmd", argv)
            # detached: no inherited pipes, scrubbed env
            self.assertEqual(kw.get("stdout"), ces.subprocess.DEVNULL)
            self.assertEqual(kw.get("stderr"), ces.subprocess.DEVNULL)
            for key in record:
                self.assertNotIn(key, kw.get("env", {}))

    def test_darwin_opens_docker_app(self):
        runs: list = []

        def _run(argv, **kw):
            runs.append(argv)
            return mock.Mock(returncode=0, stderr="", stdout="")

        clock = _Stopped()
        ok, detail = ces.try_start_docker_daemon(
            system="Darwin", run=_run, which=lambda n: f"/bin/{n}" if n == "docker" else None,
            reachable=lambda: True, monotonic=clock.monotonic, sleep=clock.sleep,
            wait_s=60.0, poll_sleep_s=2.0,
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "daemon responsive")
        self.assertEqual(runs, [["open", "-a", "Docker"]])

    def test_linux_is_not_automated(self):
        ok, detail = ces.try_start_docker_daemon(
            system="Linux", which=lambda n: f"/bin/{n}" if n == "docker" else None)
        self.assertFalse(ok)
        self.assertIn("not automated on Linux", detail)

    def test_no_docker_binary_is_a_plain_refusal(self):
        ok, detail = ces.try_start_docker_daemon(
            system="Windows", which=lambda n: None,
            env={"ProgramFiles": "/none", "LOCALAPPDATA": ""})
        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

    def test_the_wait_is_bounded_liveness_only(self):
        # reachable flips True after one poll: the loop must stop polling and
        # report — with a never-advancing fake clock a real deadline loop
        # would spin forever, so this also proves the loop honours readiness.
        clock = _Stopped()
        polls = {"n": 0}

        def _flips():
            polls["n"] += 1
            return polls["n"] > 1

        ok, detail = ces.try_start_docker_daemon(
            system="Darwin", run=lambda argv, **kw: mock.Mock(returncode=0, stderr=""),
            which=lambda n: f"/bin/{n}" if n == "docker" else None,
            reachable=_flips, monotonic=clock.monotonic, sleep=clock.sleep,
            wait_s=60.0, poll_sleep_s=2.0,
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "daemon responsive")
        self.assertEqual(len(clock.slept), 1)

    def test_wait_zero_launches_and_returns(self):
        clock = _Stopped()
        ok, detail = ces.try_start_docker_daemon(
            system="Darwin", run=lambda argv, **kw: mock.Mock(returncode=0, stderr=""),
            which=lambda n: f"/bin/{n}" if n == "docker" else None,
            reachable=lambda: False, monotonic=clock.monotonic, sleep=clock.sleep,
            wait_s=0.0,
        )
        self.assertTrue(ok)
        self.assertEqual(clock.slept, [])
        self.assertIn("launch attempted", detail)


class CliTests(unittest.TestCase):
    def test_ensure_verb_prints_and_exits_zero(self):
        with mock.patch.object(ces, "ensure_engine", return_value=(False, "nope")) as m, \
                mock.patch("builtins.print") as pr:
            rc = ces.main(["ensure"])
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("wait_s"), 60.0)
        pr.assert_called_once()
        self.assertIn("not started", pr.call_args.args[0])

    def test_ensure_verb_json(self):
        import io
        import json

        with mock.patch.object(ces, "ensure_engine", return_value=(True, "engine already up; nothing to start")):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = ces.main(["ensure", "--json", "--wait", "5"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()),
                         {"ok": True, "detail": "engine already up; nothing to start"})


class InstallDelegationTests(unittest.TestCase):
    """install.py keeps patchable names that delegate to the ONE home."""

    def test_docker_starter_delegates(self):
        import install

        self.assertIs(install._try_start_docker_daemon.__module__, "install")
        with mock.patch.object(ces, "try_start_docker_daemon", return_value=(False, "x")) as m:
            self.assertEqual(install._try_start_docker_daemon(), (False, "x"))
        m.assert_called_once_with()

    def test_podman_heal_delegates(self):
        import install

        # The alias is bound at import (install._heal_podman_reachability IS
        # ces.heal_podman_reachability); _try_start_podman_daemon looks the
        # install global up at call time, so patching install's name works —
        # exactly what the runtime_reconcile tests rely on.
        self.assertIs(install._heal_podman_reachability, ces.heal_podman_reachability)
        fake = mock.Mock(return_value=_cp.HealResult(False, [], "y"))
        with mock.patch.object(install, "_heal_podman_reachability", fake):
            self.assertEqual(install._try_start_podman_daemon(), (False, "y"))
        fake.assert_called_once_with()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
