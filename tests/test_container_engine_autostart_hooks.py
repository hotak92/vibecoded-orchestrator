# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the session hooks spawn the engine auto-start CLI.

Owner ruling 2026-10-15: the container engine "should be made to auto-start
with launcher/hub/VS Code (if opening a VCO project)". The SessionStart hook
``ensure-containers`` is the VS Code leg: when the runtime resolver answers
``absent`` — most often "installed but the engine is down" (Docker Desktop
not launched after a reboot, podman machine stopped, socket down) — the hook
now fires ``python -m vco_lib.container_engine_start ensure`` detached before
its ``exit 0``.

These tests drive the REAL ``templates/hooks/ensure-containers.{sh,ps1}``
with a FAKE python (never the host interpreter: nothing here may resolve,
heal or start a real engine) that answers the resolver verbs and records
every argv it is handed, including the session-lock ``run-detached`` re-exec
(the detached re-run is where the resolve branch runs). PowerShell is run
for real where ``pwsh`` exists (the repo's standing parity practice).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HOOK_SH = PROJECT_ROOT / "templates" / "hooks" / "ensure-containers.sh"
HOOK_PS1 = PROJECT_ROOT / "templates" / "hooks" / "ensure-containers.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")
IS_WINDOWS = os.name == "nt"

#: The fake interpreter. It answers the verbs the hook reaches before its
#: ``absent`` exit:
#:   * ``vco_lib.containers resolve`` — the ABSENT-with-docker-installed
#:     verdict the engine-down scenario produces (``--shell`` for bash,
#:     ``--json`` for PowerShell, exit 3 both times — the resolver's real
#:     contract);
#:   * ``vco_lib.service_lifecycle run-detached ... -- <cmd>`` — the session
#:     lock's detached re-exec: exec the command with the detached markers
#:     set (both siblings' gate variables — the real relay sets both);
#:   * ``vco_lib.container_engine_start ensure`` — record argv, exit 0; with
#:     ``FAKE_ENGINE_STDERR`` set, first write that text to stderr and exit 1
#:     (the FINAL2 L-2 "venv too broken to import vco_lib" leg);
#:   * everything else (``runtime_reconcile record-boot-refusal``) — record
#:     argv, exit 0.
FAKE_PY = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_PY_LOG"
verb="$2"
case "$verb" in
  vco_lib.containers)
    for a in "$@"; do
      if [ "$a" = "--json" ]; then
        printf '%s\\n' "$FAKE_RESOLVE_JSON"
        exit 3
      fi
    done
    printf '%s\\n' "$FAKE_RESOLVE_SHELL"
    exit 3 ;;
  vco_lib.service_lifecycle)
    while [ $# -gt 0 ]; do
      if [ "$1" = "--" ]; then shift; break; fi
      shift
    done
    export VCO_SESSION_LOCK_HELD=1 VCO_SESSION_DETACHED=1
    exec "$@" ;;
  vco_lib.container_engine_start)
    # FINAL2 L-2 leg: with FAKE_ENGINE_STDERR set, behave like a venv too
    # broken to import vco_lib — traceback text on stderr, exit 1.
    if [ -n "${FAKE_ENGINE_STDERR:-}" ]; then
      printf '%s\\n' "$FAKE_ENGINE_STDERR" >&2
      exit 1
    fi
    exit 0 ;;
esac
exit 0
"""

RESOLVE_SHELL_DOWN = """\
VCO_RUNTIME_STATE=absent
VCO_RUNTIME=
VCO_RUNTIME_INSTALLED=docker
VCO_COMPOSE_CMD=
VCO_COMPOSE_ARGV=()
VCO_RUNTIME_REASON=fake: docker is installed but the daemon is not answering
VCO_RUNTIME_REQUESTED=
VCO_RUNTIME_REQUESTED_VIA=
VCO_RUNTIME_REQUESTED_INSTALLED=0
VCO_RUNTIME_ALTERNATIVE_USABLE=
VCO_RUNTIME_RECONCILED=0
"""

RESOLVE_SHELL_NOT_INSTALLED = RESOLVE_SHELL_DOWN.replace(
    "VCO_RUNTIME_INSTALLED=docker", "VCO_RUNTIME_INSTALLED=")

RESOLVE_JSON_DOWN = json.dumps({
    "state": "absent", "runtime": None, "installed": "docker", "compose": None,
    "compose_form": None, "daemon_responsive": False,
    "reason": "fake: docker is installed but the daemon is not answering",
    "requested": None, "requested_via": None, "requested_installed": False,
    "alternative_usable": None, "record_reconciled": False,
})

RESOLVE_JSON_NOT_INSTALLED = json.loads(RESOLVE_JSON_DOWN)
RESOLVE_JSON_NOT_INSTALLED["installed"] = None
RESOLVE_JSON_NOT_INSTALLED["reason"] = "fake: no container runtime installed"
RESOLVE_JSON_NOT_INSTALLED = json.dumps(RESOLVE_JSON_NOT_INSTALLED)

ENGINE_LINE = "-m vco_lib.container_engine_start ensure"

#: What the fake "broken venv" writes on stderr (FINAL2 L-2 leg): the hook
#: spawners must append it to the outcome log, not discard it.
ENGINE_STDERR_TEXT = "Traceback (most recent call last): No module named vco_lib"


def _wait_for_line(log: Path, needle: str, timeout_s: float = 15.0) -> bool:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if log.exists() and needle in log.read_text():
            return True
        time.sleep(0.1)
    return False


class _HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_engine_hook_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "pyargv.log"
        venv_bin = self.tmp / "fakevenv" / "bin"
        venv_bin.mkdir(parents=True)
        self.fake_py = venv_bin / "python"
        self.fake_py.write_text(FAKE_PY, encoding="utf-8")
        self.fake_py.chmod(0o755)

    def _env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("VCO_VENV_PYTHON", "VCT_VENV", "VCT_INSTALL_ROOT",
                            "VCO_SESSION_LOCK_HELD", "VCO_SESSION_DETACHED",
                            "VCT_DISABLE_HOOKS", "VCT_ORCHESTRATOR_ROOT",
                            "VCT_COMPOSE_DIR")}
        env.update({
            "VCT_VENV": str(self.tmp / "fakevenv"),
            "FAKE_PY_LOG": str(self.log),
            "VCT_STATE_DIR": str(self.tmp / "state"),
            "TMPDIR": str(self.tmp),
            "TEMP": str(self.tmp),
        })
        env.update(extra)
        return env

    def _run_sh(self, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(HOOK_SH)], env=self._env(**extra),
                              capture_output=True, text=True, timeout=90,
                              cwd=str(self.tmp))

    def _run_ps1(self, **extra) -> subprocess.CompletedProcess:
        from tests.common.pwsh import PWSH_BASE_FLAGS

        return subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-ExecutionPolicy", "Bypass",
                               "-File", str(HOOK_PS1)],
                              env=self._env(**extra), capture_output=True, text=True,
                              timeout=180, cwd=str(self.tmp))

    def _engine_started(self, wait_s: float = 15.0) -> bool:
        """The hook spawns the starter DETACHED (``( ... & )`` / Start-Process)
        and returns at once, so the fake interpreter's argv line can land after
        the hook exits — under a loaded full-suite run it did (field flake,
        lane V02103-FINAL2). Wait for it (bounded); a negative test passes a
        short wait so a late spawn is still caught."""
        _wait_for_line(self.log, ENGINE_LINE, timeout_s=wait_s)
        return self.log.exists() and any(
            ln.strip() == ENGINE_LINE for ln in self.log.read_text().splitlines())

    def _engine_outcome_log(self) -> Path:
        """The CLI's OWN outcome log (sole ownership — the spawners never
        write it): resolved under the same ``VCT_STATE_DIR`` the test sets
        for the hook."""
        return Path(self._env()["VCT_STATE_DIR"]) / "logs" / "engine-start.log"

    def _engine_stderr_log(self) -> Path:
        """The spawners' per-spawn stderr file (R31 M-1): the child's stderr
        is TRUNCATED into it on every spawn."""
        return Path(self._env()["VCT_STATE_DIR"]) / "logs" / "engine-start.stderr.log"

    def _engine_stderr_captured(self, wait_s: float = 15.0) -> bool:
        stderr_log = self._engine_stderr_log()
        _wait_for_line(stderr_log, ENGINE_STDERR_TEXT, timeout_s=wait_s)
        return stderr_log.exists() and ENGINE_STDERR_TEXT in stderr_log.read_text()


@unittest.skipIf(
    IS_WINDOWS,
    "bash .sh leg; Windows runs the .ps1 sibling: "
    "tests/test_container_engine_autostart_hooks.py::Ps1HookWiringTests::test_engine_down_fires_the_autostart_cli")
class BashHookWiringTests(_HookCase):
    def test_engine_down_fires_the_autostart_cli(self):
        proc = self._run_sh(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                            FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("skipping", proc.stdout)
        self.assertTrue(self._engine_started(),
                        "engine down: the hook must fire the auto-start CLI; "
                        f"log:\n{self.log.read_text() if self.log.exists() else '<none>'}")

    def test_nothing_installed_fires_nothing(self):
        proc = self._run_sh(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_NOT_INSTALLED,
                            FAKE_RESOLVE_JSON=RESOLVE_JSON_NOT_INSTALLED)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self._engine_started(wait_s=2.0),
                         "nothing installed: no engine start may be spawned; "
                         f"log:\n{self.log.read_text() if self.log.exists() else '<none>'}")

    def test_a_broken_venv_traceback_lands_in_the_spawners_stderr_file(self):
        # R31 M-1: a venv too broken to import vco_lib writes nothing to the
        # outcome log itself — the hook's spawn must capture the child's
        # stderr in the spawners' OWN per-spawn file, never into the CLI's
        # rotating engine-start.log.
        proc = self._run_sh(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                            FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN,
                            FAKE_ENGINE_STDERR=ENGINE_STDERR_TEXT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self._engine_stderr_captured(),
                        "the broken-venv traceback must reach "
                        f"{self._engine_stderr_log()}; it was discarded by the spawn")
        outcome = self._engine_outcome_log()
        self.assertFalse(outcome.exists() and ENGINE_STDERR_TEXT in outcome.read_text(),
                         "the spawner must NOT redirect into the CLI's own "
                         f"rotating log {outcome}")

    def test_the_stderr_file_is_truncated_on_every_spawn(self):
        # R31 M-1: the per-spawn stderr file is OVERWRITTEN (2>), so it is
        # bounded by construction — a previous spawn's content must not
        # survive the next one.
        stderr_log = self._engine_stderr_log()
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        stderr_log.write_text("sentinel from a previous spawn\n", encoding="utf-8")
        proc = self._run_sh(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                            FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN,
                            FAKE_ENGINE_STDERR=ENGINE_STDERR_TEXT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self._engine_stderr_captured(),
                        "the fresh traceback must reach "
                        f"{self._engine_stderr_log()}")
        self.assertNotIn("sentinel from a previous spawn",
                         stderr_log.read_text(),
                         "the per-spawn stderr file must be TRUNCATED on every spawn")


@unittest.skipIf(not PWSH, "pwsh not available")
class Ps1HookWiringTests(_HookCase):
    def test_engine_down_fires_the_autostart_cli(self):
        proc = self._run_ps1(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                             FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self._engine_started(),
                        "engine down: the .ps1 hook must fire the auto-start CLI; "
                        f"log:\n{self.log.read_text() if self.log.exists() else '<none>'}")

    def test_nothing_installed_fires_nothing(self):
        proc = self._run_ps1(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_NOT_INSTALLED,
                             FAKE_RESOLVE_JSON=RESOLVE_JSON_NOT_INSTALLED)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self._engine_started(wait_s=2.0),
                         "nothing installed: no engine start may be spawned; "
                         f"log:\n{self.log.read_text() if self.log.exists() else '<none>'}")

    def test_a_broken_venv_traceback_lands_in_the_spawners_stderr_file(self):
        # R31 M-1 (parity with the .sh sibling): Start-Process discards the
        # child's output, so the spawn must route the child's stderr into
        # the spawners' OWN per-spawn file — never the CLI's rotating log.
        proc = self._run_ps1(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                             FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN,
                             FAKE_ENGINE_STDERR=ENGINE_STDERR_TEXT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self._engine_stderr_captured(),
                        "the broken-venv traceback must reach "
                        f"{self._engine_stderr_log()}; it was discarded by the spawn")
        outcome = self._engine_outcome_log()
        self.assertFalse(outcome.exists() and ENGINE_STDERR_TEXT in outcome.read_text(),
                         "the spawner must NOT redirect into the CLI's own "
                         f"rotating log {outcome}")

    def test_the_direct_fallback_runs_when_the_wrapper_spawn_failed(self):
        # R31 L-4: Start-VcoDetachedPwsh NEVER throws — it reports a failed
        # spawn on stderr and returns nothing. $EngineSpawned must reflect
        # the REAL spawn, so the direct Start-Process fallback runs when the
        # wrapper path failed (it used to be skipped exactly then). Drive
        # the real hook bytes from a copied hooks dir whose _lib copy is
        # patched to make Start-VcoDetachedPwsh fail like a real spawn
        # failure (return nothing).
        hooks_dir = self.tmp / "hooks"
        shutil.copytree(PROJECT_ROOT / "templates" / "hooks", hooks_dir)
        lib = hooks_dir / "_lib" / "resolve-powershell.ps1"
        # Plain utf-8 append: the file already has its BOM; a mid-file BOM
        # would break the child's parser.
        with lib.open("a", encoding="utf-8") as fh:
            fh.write(
                "\n# test override: simulate a failed detached spawn (returns\n"
                "# nothing, exactly like Start-VcoDetachedProcess on failure).\n"
                "function Start-VcoDetachedPwsh { param([string]$Command, "
                "[string]$PowerShellExe, [switch]$PassThru) return $null }\n")
        from tests.common.pwsh import PWSH_BASE_FLAGS

        env = self._env(FAKE_RESOLVE_SHELL=RESOLVE_SHELL_DOWN,
                        FAKE_RESOLVE_JSON=RESOLVE_JSON_DOWN,
                        FAKE_ENGINE_STDERR=ENGINE_STDERR_TEXT)
        proc = subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-ExecutionPolicy", "Bypass",
                               "-File", str(hooks_dir / "ensure-containers.ps1")],
                              env=env, capture_output=True, text=True,
                              timeout=180, cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self._engine_started(),
                        "the direct fallback must spawn the auto-start CLI when "
                        "the wrapper spawn failed; log:\n"
                        f"{self.log.read_text() if self.log.exists() else '<none>'}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
