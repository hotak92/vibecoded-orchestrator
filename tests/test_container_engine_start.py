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

import io
import json
import os
import tempfile
import threading
import time
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
        # R19 F1: starting requires a RECORDED runtime — the default
        # resolution carries one (docker, recorded and installed).
        self.resolve_fn = mock.MagicMock(
            return_value=_resolution(_c.RuntimeState.ABSENT, installed="docker",
                                     requested="docker", requested_installed=True))
        self.manifest_reader = mock.MagicMock(return_value=False)

    def _ensure(self, **kw):
        kw.setdefault("resolve_fn", self.resolve_fn)
        kw.setdefault("start_docker", self.start_docker)
        kw.setdefault("heal_podman", self.heal_podman)
        kw.setdefault("manifest_reader", self.manifest_reader)
        kw.setdefault("pref_reader", lambda key: None)
        kw.setdefault("update_in_progress", lambda: False)
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

    def test_no_recorded_runtime_starts_nothing(self):
        """R19 F1: with NO pin at all (fresh pre-runtime.txt install, both
        runtimes on PATH) NOTHING is started — never the first binary on
        PATH, never an unattended podman machine init."""
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.ABSENT, installed="podman")
        ok, detail = self._ensure()
        self.assertFalse(ok)
        self.assertIn("no runtime recorded", detail)
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_install_manifest_no_containers_starts_nothing(self):
        """R19 F1: the persisted --no-containers choice (state/
        install-manifest.json skipped.containers) refuses outright, even
        with a recorded runtime that is down."""
        self.manifest_reader.return_value = True
        ok, detail = self._ensure()
        self.assertFalse(ok)
        self.assertIn("--no-containers", detail)
        self.resolve_fn.assert_not_called()
        self.start_docker.assert_not_called()
        self.heal_podman.assert_not_called()

    def test_preferences_toggle_off_is_a_noop_before_any_probe(self):
        """R19 F4: the launcher.db toggle off disables the starter — the
        surface a GUI-started launcher/hub can actually reach."""
        ok, detail = self._ensure(pref_reader=lambda key: "false")
        self.assertFalse(ok)
        self.assertIn("Preferences toggle", detail)
        self.assertIn(ces.APP_STATE_ENGINE_AUTOSTART, detail)
        self.resolve_fn.assert_not_called()
        self.start_docker.assert_not_called()

    def test_an_update_in_progress_skips(self):
        """R19 F7: the SAME update-in-progress gate the hooks honour."""
        ok, detail = self._ensure(update_in_progress=lambda: True)
        self.assertFalse(ok)
        self.assertIn("update is in progress", detail)
        self.start_docker.assert_not_called()

    def test_recorded_podman_takes_the_podman_branch(self):
        self.resolve_fn.return_value = _resolution(_c.RuntimeState.ABSENT, installed="docker",
                                                   requested="podman", requested_installed=True)
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

    def test_windows_cmd_child_gets_create_no_window(self):
        """R19 F3 / R21 L2: on Windows the ``cmd`` console child is spawned
        with ``CREATE_NO_WINDOW`` (0x08000000) so a windowless parent (the
        Rust spawn) never flashes a console. Red-proofed by mutation:
        removing ``**_windows_no_window_kwargs()`` from the Popen call
        makes this fail."""
        with tempfile.TemporaryDirectory() as td:
            env, exe = self._windows_env(Path(td))
            seen: list = []

            def _recording_popen(argv, **kw):
                seen.append(kw)
                return mock.Mock()

            # `os.name` is what `_windows_no_window_kwargs` branches on —
            # but a global patch makes every `Path()` INSIDE the starter
            # a WindowsPath, which cannot instantiate on POSIX. So drive
            # the REAL helper with os.name="nt" for exactly its call (it
            # builds no Path) and let the call site pass its kwargs on.
            real_kwargs = ces._windows_no_window_kwargs

            def _nt_kwargs():
                with mock.patch("os.name", "nt"):
                    return real_kwargs()

            with mock.patch.object(ces, "_windows_no_window_kwargs", side_effect=_nt_kwargs):
                ok, _ = ces.try_start_docker_daemon(
                    system="Windows", env=env,
                    which=lambda n: f"/bin/{n}" if n == "docker" else None,
                    popen=_recording_popen, reachable=lambda: False,
                    wait_s=0.0)
            self.assertTrue(ok)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].get("creationflags"), ces.CREATE_NO_WINDOW)

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

    def test_the_default_binary_lookup_is_the_resolvers(self):
        """R19 F5: one lookup home — the default `which` is the resolver's
        tool_search_dirs.which (PATH + the usual install locations), so a
        short-PATH graphical launch the resolver calls "installed" is not
        refused here as "not on PATH"."""
        seen = {}
        monkey_which = lambda name: seen.setdefault(name, f"/opt/big/{name}")  # noqa: E731
        with mock.patch.object(ces._tsd, "which", monkey_which):
            ok, detail = ces.try_start_docker_daemon(
                system="Darwin", run=lambda argv, **kw: mock.Mock(returncode=0, stderr=""),
                reachable=lambda: False, monotonic=lambda: 0.0, sleep=lambda s: None,
                wait_s=0.0)
        self.assertTrue(ok)
        self.assertEqual(seen, {"docker": "/opt/big/docker"})

    def test_unset_localappdata_never_probes_a_cwd_relative_exe(self):
        """R19 F8: an unset LOCALAPPDATA used to turn the second candidate
        into a cwd-relative `Docker/Docker Desktop.exe` — skipped now."""
        with tempfile.TemporaryDirectory() as td:
            # A decoy at the cwd-relative location must never be picked:
            # run from a temp cwd that HOLDS one.
            decoy_dir = Path(td) / "cwd"
            (decoy_dir / "Docker").mkdir(parents=True)
            (decoy_dir / "Docker" / "Docker Desktop.exe").write_bytes(b"x")
            prev_cwd = os.getcwd()
            os.chdir(decoy_dir)
            try:
                ok, detail = ces.try_start_docker_daemon(
                    system="Windows", which=lambda n: f"/bin/{n}" if n == "docker" else None,
                    env={"ProgramFiles": str(Path(td) / "none"), "LOCALAPPDATA": ""},
                    popen=lambda *a, **kw: (_ for _ in ()).throw(
                        AssertionError("no spawn expected")),
                    reachable=lambda: False, wait_s=0.0)
            finally:
                os.chdir(prev_cwd)
        self.assertFalse(ok)
        self.assertIn("not found at the canonical install locations", detail)

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
        self.assertIn("docker binary not found", detail)

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


class RealLauncherDbReaderTests(unittest.TestCase):
    """R21 M1: the Preferences toggle through the REAL launcher.db reader.

    Every unit test above injects ``pref_reader``; this one drives the
    DEFAULT reader (``vco_lib.launcher_db_reader.read_app_state_value``,
    resolved inside :func:`ensure_engine` when no reader is injected)
    against a real sqlite launcher.db holding the exact row the Rust
    Preferences command writes (``app_state_set_bool`` stores the TEXT
    ``"false"``), through ``main()`` with nothing injected — the path the
    hub, the launcher setup and the session hooks actually take.
    """

    def test_a_real_db_with_the_toggle_off_stops_the_cli_before_any_probe(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            conn = sqlite3.connect(db)
            try:
                # The schema of migrations/008_app_state.sql; the row is the
                # one `Db::app_state_set_bool("engine.autostart", false)`
                # writes.
                conn.execute(
                    "CREATE TABLE app_state (key TEXT PRIMARY KEY, "
                    "value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
                conn.execute(
                    "INSERT INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, 0)",
                    (ces.APP_STATE_ENGINE_AUTOSTART, "false"))
                conn.commit()
            finally:
                conn.close()

            env = dict(os.environ)
            # One temp state root for BOTH the single-flight lock path and
            # the launcher.db discovery (VCT_LAUNCHER_DB_PATH unset).
            env["VCT_STATE_DIR"] = td
            env.pop("VCT_LAUNCHER_DB_PATH", None)
            env.pop(ces.VCT_CONTAINER_ENGINE_AUTOSTART, None)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(
                        ces._c, "resolve",
                        side_effect=AssertionError(
                            "the resolver must not be reached when the "
                            "toggle is off")):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = ces.main(["ensure", "--wait", "0", "--json"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertIn("Preferences toggle", out["detail"])
        self.assertIn(ces.APP_STATE_ENGINE_AUTOSTART, out["detail"])


class PrefEnabledTests(unittest.TestCase):
    """R19 F4: the launcher.db Preferences toggle, launcher_ensure's shape."""

    def test_absent_row_and_errors_read_as_the_default_on(self):
        self.assertTrue(ces.pref_enabled(lambda key: None))

        def boom(key):
            raise RuntimeError("db locked")

        self.assertTrue(ces.pref_enabled(boom))

    def test_truthiness_rule_matches_the_rust_writer(self):
        self.assertTrue(ces.pref_enabled(lambda key: "true"))
        self.assertTrue(ces.pref_enabled(lambda key: "1"))
        self.assertFalse(ces.pref_enabled(lambda key: "false"))
        self.assertFalse(ces.pref_enabled(lambda key: "0"))
        self.assertFalse(ces.pref_enabled(lambda key: ""))

    def test_the_app_state_key_matches_the_rust_sides(self):
        """Cross-language pin: the launcher WRITES the row (commands/
        engine_autostart_pref.rs), the core pre-check and this module READ
        it — the three literals cannot drift."""
        repo = Path(__file__).resolve().parents[1]
        launcher_rs = (repo / "launcher" / "src-tauri" / "src" / "commands"
                       / "engine_autostart_pref.rs").read_text(encoding="utf-8")
        core_rs = (repo / "launcher" / "src-tauri" / "vct-launcher-core" / "src"
                   / "services" / "engine_autostart.rs").read_text(encoding="utf-8")
        self.assertIn(f'"{ces.APP_STATE_ENGINE_AUTOSTART}"', launcher_rs)
        self.assertIn(f'"{ces.APP_STATE_ENGINE_AUTOSTART}"', core_rs)


class InstallSkippedContainersTests(unittest.TestCase):
    """R19 F1: reading the persisted --no-containers choice."""

    def _manifest(self, tmp_path, payload):
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "install-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        return tmp_path

    def test_true_when_the_install_skipped_containers(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._manifest(Path(td), {"skipped": {"containers": True}})
            self.assertTrue(ces.install_skipped_containers(root))

    def test_false_when_containers_were_installed(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._manifest(Path(td), {"skipped": {"containers": False}})
            self.assertFalse(ces.install_skipped_containers(root))

    def test_false_when_missing_or_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(ces.install_skipped_containers(Path(td)))  # no manifest
            root = self._manifest(Path(td), {})  # no skipped block
            self.assertFalse(ces.install_skipped_containers(root))
            (root / "state" / "install-manifest.json").write_text("{ not json", encoding="utf-8")
            self.assertFalse(ces.install_skipped_containers(root))


class SingleFlightTests(unittest.TestCase):
    """R19 F2: ONE per-user claim — concurrent callers produce ONE starter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patcher = mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(Path(self._tmp.name))})
        self._patcher.start()

    def test_the_lock_path_is_per_user_under_the_state_root(self):
        self.assertEqual(ces.engine_start_lock_path(),
                         Path(self._tmp.name) / "state" / "engine-start.lock")

    def test_a_held_claim_makes_the_loser_stand_down(self):
        from vco_lib.atomic import exclusive_file_lock

        with mock.patch.object(ces, "ensure_engine") as ensure:
            with exclusive_file_lock(ces.engine_start_lock_path(), timeout_s=5):
                # Another CLI run holds the claim right now.
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = ces.main(["ensure", "--wait", "0"])
        self.assertEqual(rc, 0)
        ensure.assert_not_called()
        self.assertIn("already in progress", buf.getvalue())

    def test_two_racing_runs_start_the_engine_exactly_once(self):
        started = []
        barrier = threading.Barrier(2)

        def slow_ensure(**kw):
            started.append("once")
            time.sleep(0.4)  # the "engine boot" window
            return True, "daemon responsive"

        def run_cli():
            barrier.wait()  # both threads reach main() together
            with mock.patch.object(ces, "ensure_engine", side_effect=slow_ensure):
                ces.main(["ensure", "--wait", "0", "--json"])

        threads = [threading.Thread(target=run_cli) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(started, ["once"])


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
