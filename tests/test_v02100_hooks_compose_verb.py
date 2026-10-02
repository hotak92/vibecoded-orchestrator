# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-07 — the container hooks compose ONLY through the guarded
Python verb, and ONLY from the orchestrator's own infrastructure/.

* `_lib/compose-dir.{sh,ps1}` (review L1-F17, owner ruling Q3): a compose
  directory whose parent is not the orchestrator clone (no ``vct-module.json``
  with id ``orchestrator``) is REFUSED with a message naming
  ``VCT_ORCHESTRATOR_ROOT``. Both siblings run the same scenario table.
* `ensure-containers.{sh,ps1}` driven for real (the fake runtime/compose of
  ``tests/test_v0297_lifecycle_hooks.py``): a project copy creates nothing
  (act) and an orchestrator root composes (leave-alone); a VCO-managed zombie
  whose data identity is NOT proven is never removed.
* `ensure-code-embed-service.sh`: a real build failure is not retried without
  ``--build`` (the retry needs positive "unsupported" evidence — L1-F07).

No real container runtime, no network.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_v0297_lifecycle_hooks import (
    DOGFOOD_ROWS, HOOKS, IS_WINDOWS, PWSH, SENTINEL_MANAGED, _row, _run_hook, _run_verify,
    _Shells, _TmpCase,
)
from tests.common.ports import free_port
from tests.test_v0292_code_embed_hook_build import _Fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_SH = HOOKS / "_lib" / "compose-dir.sh"
LIB_PS1 = HOOKS / "_lib" / "compose-dir.ps1"


def _orch(root: Path) -> Path:
    (root / "infrastructure").mkdir(parents=True)
    (root / "vct-module.json").write_text(json.dumps({"id": "orchestrator"}), encoding="utf-8")
    return root


def _scenarios(tmp: Path) -> list[tuple[str, Path, dict]]:
    orch = _orch(tmp / "orch")
    project = tmp / "project"
    (project / "infrastructure").mkdir(parents=True)
    module = tmp / "module"
    (module / "infrastructure").mkdir(parents=True)
    (module / "vct-module.json").write_text(json.dumps({"id": "rl-retrieval"}), encoding="utf-8")
    empty = tmp / "empty"
    empty.mkdir()
    return [
        ("orchestrator root via env", project, {"VCT_ORCHESTRATOR_ROOT": str(orch)}),
        ("project copy, nothing set", project, {}),
        ("a module repo is not the orchestrator", module, {}),
        ("explicit compose dir inside the clone", project,
         {"VCT_COMPOSE_DIR": str(orch / "infrastructure")}),
        ("no candidate at all", empty, {}),
        ("the clone itself", orch, {}),
    ]


def _clean_env(extra: dict) -> dict:
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_COMPOSE_DIR", "VCT_INFRASTRUCTURE_DIR", "VCT_ORCHESTRATOR_ROOT")}
    env.update(extra)
    return env


def _bash_resolve(repo_root: Path, env: dict) -> tuple[str, str]:
    script = (f'. "{LIB_SH}"; vco_resolve_compose_dir "$1"; rc=$?; '
              'printf "%s\\n%s\\n%s\\n" "$COMPOSE_DIR" "$COMPOSE_DIR_REFUSAL" "$rc"')
    out = subprocess.run(["bash", "-c", script, "x", str(repo_root)], env=_clean_env(env),
                         capture_output=True, text=True, timeout=30, check=True).stdout
    lines = out.split("\n")
    return lines[0], lines[1]


def _pwsh_resolve(repo_root: Path, env: dict) -> tuple[str, str]:
    script = (f'. "{LIB_PS1}"; $r = Resolve-VcoComposeDir -RepoRoot $env:VCO_T_ROOT; '
              'Write-Output $r.Dir; Write-Output $r.Refusal')
    out = subprocess.run([PWSH, "-NoProfile", "-Command", script],
                         env=_clean_env({**env, "VCO_T_ROOT": str(repo_root)}),
                         capture_output=True, text=True, timeout=60, check=True).stdout
    lines = out.split("\n")
    return lines[0].rstrip("\r"), (lines[1].rstrip("\r") if len(lines) > 1 else "")


class ComposeDirLibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_wp07_cd_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @unittest.skipIf(IS_WINDOWS, "bash sibling")
    def test_bash_scenarios(self):
        got = {name: _bash_resolve(root, env) for name, root, env in _scenarios(self.tmp)}
        orch_infra = str(self.tmp / "orch" / "infrastructure")
        self.assertEqual(got["orchestrator root via env"], (orch_infra, ""))
        self.assertEqual(got["explicit compose dir inside the clone"], (orch_infra, ""))
        self.assertEqual(got["the clone itself"], (orch_infra, ""))
        self.assertEqual(got["no candidate at all"], ("", ""))
        for refused in ("project copy, nothing set", "a module repo is not the orchestrator"):
            directory, refusal = got[refused]
            self.assertEqual(directory, "", refused)
            self.assertIn("VCT_ORCHESTRATOR_ROOT", refusal)
            self.assertIn("nothing was created", refusal)

    @unittest.skipUnless(PWSH and not IS_WINDOWS, "needs both bash and PowerShell")
    def test_the_ps1_sibling_answers_exactly_like_the_sh(self):
        for name, root, env in _scenarios(self.tmp):
            with self.subTest(name):
                self.assertEqual(_pwsh_resolve(root, env), _bash_resolve(root, env))


class EnsureContainersRootSentinelTests(_TmpCase, _Shells):
    """Act + leave-alone on the real hook, both shells."""

    ROWS = [_row("weaviate"), _row("ollama"), _row("code_embed")]
    STATES = {"vco_weaviate": "missing", "vco_ollama": "running", "vco_code_embed": "running"}

    def test_a_project_copy_of_the_compose_files_creates_nothing(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(self.ROWS, self.STATES)
                (m.tmp / "vct-module.json").unlink()          # a PROJECT, not the clone
                proc = _run_hook(m, shell)
                self.assertEqual(m.compose_calls(), [], proc.stdout + proc.stderr)
                self.assertFalse(m.config_log.exists(), "compose was asked even for config")
                self.assertIn("VCT_ORCHESTRATOR_ROOT", proc.stdout, proc.stderr)
                self.assertIn("refusing to run compose", proc.stdout)

    def test_the_orchestrator_root_composes_the_missing_service(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(self.ROWS, self.STATES)
                proc = _run_hook(m, shell)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
                self.assertEqual(calls[0][-1], "weaviate")
                self.assertNotIn("refusing to run compose", proc.stdout)


class EnsureContainersGuardedZombieTests(_TmpCase, _Shells):
    def test_a_managed_zombie_whose_data_is_not_proven_is_never_removed(self):
        """The row records a bind; the zombie container mounts the default
        volume (the fake inspect's answer): which holds the data is unknown,
        so the verb refuses — no rm, no compose, a ledger row."""
        rows = [DOGFOOD_ROWS[0], DOGFOOD_ROWS[1],
                _row("code_embed", data_mount={"kind": "bind", "source": "/srv/hf-cache",
                                               "destination": "/cache"})]
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(rows, {"vco_weaviate": "running", "vco_ollama": "running",
                                        "vco_code_embed": "zombie"})
                proc = _run_hook(m, shell)
                rt = m.runtime_calls()
                self.assertFalse([c for c in rt if c[0] == "rm"], f"{rt}\n{proc.stdout}\n{proc.stderr}")
                self.assertEqual(m.compose_calls(), [])
                self.assertIn("recreate refused", proc.stdout)
                self.assertNotIn("recovered zombie container", proc.stdout)


class VerifyContainerPortsRootSentinelTests(_TmpCase, _Shells):
    """The watchdog's zombie recovery: act (a project copy → nothing removed,
    the refusal named) + leave-alone (the clone → the guarded verb recreates)."""

    def test_a_project_copy_never_removes_the_zombie(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(SENTINEL_MANAGED, {"vco_weaviate": "zombie"})
                (m.tmp / "vct-module.json").unlink()
                proc = _run_verify(m, shell)
                self.assertFalse([c for c in m.runtime_calls() if c[0] == "rm"],
                                 f"{m.runtime_calls()}\n{proc.stdout}\n{proc.stderr}")
                self.assertEqual(m.compose_calls(), [])
                self.assertIn("VCT_ORCHESTRATOR_ROOT", proc.stdout, proc.stderr)

    def test_the_clone_recreates_through_the_guarded_verb(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(SENTINEL_MANAGED, {"vco_weaviate": "zombie"})
                proc = _run_verify(m, shell)
                rt = m.runtime_calls()
                self.assertIn(["rm", "--force", "vco_weaviate"], rt, proc.stdout + proc.stderr)
                self.assertTrue(m.config_log.exists(), "the data guard's `compose config` never ran")
                self.assertEqual(len(m.compose_calls()), 1, proc.stdout)


@unittest.skipIf(IS_WINDOWS, "bash hook")
class CodeEmbedBuildFailureTests(unittest.TestCase):
    def test_a_real_build_failure_is_reported_not_retried_without_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp))
            fake = fx.bin / "vco-fake-compose"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{fx.compose_log}"\n'
                'echo "failed to solve: process \\"/bin/sh -c pip install\\" did not complete" >&2\n'
                "exit 1\n")
            fake.chmod(0o755)
            proc = subprocess.run(["bash", str(HOOKS / "ensure-code-embed-service.sh")],
                                  env=fx.env(free_port()), capture_output=True, text=True,
                                  timeout=180, cwd=str(REPO_ROOT))
            calls = fx.compose_invocations()
            self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("--build", calls[0])
            self.assertIn("failed to solve", proc.stdout)
            self.assertNotIn("was NOT rebuilt", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
