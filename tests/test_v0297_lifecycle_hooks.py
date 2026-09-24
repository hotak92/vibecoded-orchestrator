# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 SE-3 — the session hook and the boot wrapper obey the rows.

Drives the REAL scripts — ``templates/hooks/ensure-containers.{sh,ps1}`` and
``scripts/launch-claude-mcp-stack.{sh,ps1}`` (R42: the .ps1 sibling is
proven by running it) — against a fake container runtime, a fake compose, a
fake ``runc`` and a temp launcher.db, and reads back every argv they issued.

Invariant I1: a compose call names ONLY ``vco_managed`` services, with
``--no-deps``; an adopted service is never composed, removed or re-created,
and an empty list is no compose call at all. The zombie gate: a zombie
ADOPTED container gets its orphan runtime state cleaned and a ``start`` —
never ``rm``.
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

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import service_endpoints as se

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
WRAPPER_SH = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.sh"
WRAPPER_PS1 = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.ps1"
IS_WINDOWS = os.name == "nt"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _row(service: str, mode: str = "vco_managed", **kw) -> se.EndpointRow:
    defaults = {"weaviate": 8081, "ollama": 11435, "code_embed": 11440}
    kw.setdefault("port", defaults[service])
    kw.setdefault("source", "install_probe")
    if service == "weaviate":
        kw.setdefault("grpc_port", 50052)
    return se.EndpointRow(service=service, mode=mode, **kw)


#: The dogfood shape: Weaviate + Ollama are containers another compose
#: project created, under the vco_* names, adopted; code_embed is VCO's.
DOGFOOD_ROWS = [
    _row("weaviate", "adopted_container", container_name="vco_weaviate",
         source="migrated:legacy_compose"),
    _row("ollama", "adopted_container", container_name="vco_ollama",
         source="migrated:legacy_compose"),
    _row("code_embed"),
]


#: The session reconcile stand-in (via the runner's test seam). It logs
#: "RECONCILE" into the runtime log — so its position relative to the hook's
#: `inspect` calls shows the ORDER — and, on request, sleeps, fails, reports
#: entries, or rewrites the weaviate row to `adopted_container` (what the real
#: reconcile does when a "VCO-managed" row's container belongs to the legacy
#: compose project).
FAKE_RECONCILE = """\
import json, os, sys, time
from dataclasses import replace
from pathlib import Path
with open(os.environ["FAKE_RECONCILE_LOG"], "a") as fh:
    fh.write("RECONCILE\\n")
time.sleep(float(os.environ.get("FAKE_RECONCILE_SLEEP", "0")))
if os.environ.get("FAKE_RECONCILE_ADOPT_WEAVIATE"):
    from vco_lib import service_endpoints as se
    db = Path(os.environ["VCT_STATE_DIR"]) / "launcher.db"
    row = se.load_rows(db)["weaviate"]
    se.write_rows([replace(row, mode="adopted_container", container_name="vco_weaviate",
                           source="live_reconcile")], db_path=db)
if os.environ.get("FAKE_RECONCILE_EXIT"):
    print("reconcile blew up", file=sys.stderr)
    sys.exit(int(os.environ["FAKE_RECONCILE_EXIT"]))
print(json.dumps({"schema": 1, "entries": os.environ.get("FAKE_RECONCILE_ENTRIES", "").split()}))
"""


class _Machine:
    """A temp state dir (launcher.db + rows) and a bin dir of fakes.

    ``states`` maps container → ``missing`` | ``running`` | ``zombie`` |
    ``exited``. A zombie reports ``running`` with PID 0 (dead to both
    hooks' liveness probes); ``running`` reports THIS test process's PID."""

    def __init__(self, tmp: Path, rows, states: dict):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        db = self.state / "launcher.db"
        make_launcher_db(db)
        if rows:
            se.write_rows(rows, db_path=db)
        self.rt_log = tmp / "runtime.log"
        self.compose_log = tmp / "compose.log"
        self.runc_log = tmp / "runc.log"
        self.compose_dir = tmp / "infrastructure"
        self.compose_dir.mkdir()
        (self.compose_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        # Built line by line: a shebang that is not at column 0 is no shebang,
        # and only bash (not pwsh) would still run such a file.
        script = [
            "#!/usr/bin/env bash",
            f'printf "%s\\n" "$*" >> "{self.rt_log}"',
            'case "$1" in',
            "  info|version) exit 0 ;;",
            "  compose) exit 1 ;;",
            "  ps)",
            '    case "$*" in',
        ]
        for name, st in states.items():
            if st in ("running", "zombie"):
                script.append(f'      *"name=^{name}\\$"*) echo {name} ;;')
        script += [
            "    esac",
            "    exit 0 ;;",
            "  inspect)",
            '    name="$2"',
            '    STATUS=""',
            '    case "$name" in',
        ]
        for name, st in states.items():
            if st == "missing":
                script.append(f"      {name}) exit 1 ;;")
            else:
                status = "running" if st in ("running", "zombie") else st
                pid = "0" if st == "zombie" else str(os.getpid())
                script.append(f"      {name}) STATUS={status}; PID={pid} ;;")
        script += [
            "      *) exit 1 ;;",
            "    esac",
            '    case "$4" in',
            '      *State.Pid*) echo "$PID" ;;',
            '      *.Id*) echo "id-$name" ;;',
            '      *) echo "$STATUS" ;;',
            "    esac",
            "    exit 0 ;;",
            "esac",
            "exit 0",
        ]
        (self.bin / "podman").write_text("\n".join(script) + "\n", encoding="utf-8")
        # A curl that never reaches anything: verify-container-ports probes
        # the literal default ports, and a test must never touch a real service.
        (self.bin / "curl").write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
        for fake, log in (("vco-fake-compose", self.compose_log), ("podman-compose", self.compose_log),
                          ("runc", self.runc_log)):
            (self.bin / fake).write_text(
                f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n', encoding="utf-8")
        # No GPU on the fake host (the wrapper would otherwise wait for CDI).
        (self.bin / "nvidia-smi").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        self.reconcile_script = tmp / "fake_reconcile.py"
        self.reconcile_script.write_text(FAKE_RECONCILE, encoding="utf-8")
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def env(self, **extra) -> dict:
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(REPO_ROOT),
            "VCT_STATE_DIR": str(self.state),
            "VCT_CONTAINER_RUNTIME": "podman",
            "VCT_COMPOSE_CMD": "vco-fake-compose",
            "VCT_COMPOSE_DIR": str(self.compose_dir),
            "VCT_ORCHESTRATOR_ROOT": str(self.tmp / "orch-without-wrapper"),
            "VCT_RUNC_ROOT": str(self.tmp),
            "VCO_VENV_PYTHON": sys.executable,
            "TMPDIR": str(self.tmp),
            "TEMP": str(self.tmp),
            "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
            "LOCALAPPDATA": str(self.tmp / "localappdata"),
            "VCT_STACK_WORKING_DIR": str(self.compose_dir),
            "VCT_STACK_LOG_FILE": str(self.tmp / "stack.log"),
            "VCT_STACK_RUNTIME_FILE": str(self.tmp / "no-runtime.txt"),
            "VCO_SESSION_RECONCILE_ARGV": json.dumps([sys.executable, str(self.reconcile_script)]),
            "FAKE_RECONCILE_LOG": str(self.rt_log),
        })
        for key in ("VCT_DISABLE_HOOKS", "VCT_LAUNCHER_DB_PATH", "VCT_REQUIRED_CONTAINERS",
                    "VCT_VENV", "VCT_INSTALL_ROOT", "VCO_COMPOSE_SERVICES", "VCT_STACK_BUILD"):
            env.pop(key, None)
        env.update(extra)
        return env

    def lines(self, path: Path) -> list[str]:
        return [ln for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []

    def compose_calls(self) -> list[list[str]]:
        return [ln.split() for ln in self.lines(self.compose_log)]

    def runtime_calls(self) -> list[list[str]]:
        return [ln.split() for ln in self.lines(self.rt_log)]


def _run_hook(machine: _Machine, shell: str, **env) -> subprocess.CompletedProcess:
    if shell == "bash":
        argv = ["bash", str(HOOKS / "ensure-containers.sh")]
    else:
        argv = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(HOOKS / "ensure-containers.ps1")]
    return subprocess.run(argv, env=machine.env(**env), capture_output=True, text=True,
                          timeout=180, cwd=str(REPO_ROOT))


def _run_wrapper(machine: _Machine, shell: str, args: list[str], **env) -> subprocess.CompletedProcess:
    if shell == "bash":
        argv = ["bash", str(WRAPPER_SH), *args]
    else:
        argv = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER_PS1), *args]
    return subprocess.run(argv, env=machine.env(**env), capture_output=True, text=True,
                          timeout=180, cwd=str(machine.tmp))


class _Shells:
    def shells(self):
        out = []
        if not IS_WINDOWS:
            out.append("bash")
        if PWSH:
            out.append("pwsh")
        return out


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_se3_hooks_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._n = 0

    def machine(self, rows, states: dict) -> _Machine:
        self._n += 1
        sub = self.tmp / f"m{self._n}"
        sub.mkdir()
        return _Machine(sub, rows, states)


def _assert_explicit_managed_only(case: unittest.TestCase, call: list[str],
                                  expected: list[str], forbidden=("weaviate", "ollama")):
    case.assertIn("up", call, call)
    case.assertIn("--no-deps", call, call)
    tail = call[call.index("--no-deps") + 1:]
    case.assertEqual(tail, expected, call)
    for service in forbidden:
        case.assertNotIn(service, call, call)


# ===========================================================================
# (1) the session hook composes only VCO-managed services
# ===========================================================================

class HookComposeListTests(_TmpCase, _Shells):
    def test_adopted_services_never_reach_compose(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "missing", "vco_ollama": "missing",
                                                "vco_code_embed": "missing"})
                proc = _run_hook(m, shell)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
                _assert_explicit_managed_only(self, calls[0], ["code_embed"])
                self.assertIn("--build", calls[0])
                # the adopted, missing containers are reported, not created
                self.assertIn("'vco_weaviate' does not exist", proc.stdout)
                self.assertIn("'vco_ollama' does not exist", proc.stdout)

    def test_only_the_missing_managed_services_are_named(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine([_row("weaviate"), _row("ollama"), _row("code_embed")],
                                 {"vco_weaviate": "missing", "vco_ollama": "running",
                                  "vco_code_embed": "running"})
                proc = _run_hook(m, shell)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
                _assert_explicit_managed_only(self, calls[0], ["weaviate"], forbidden=("ollama",))
                self.assertNotIn("--build", calls[0])

    def test_nothing_missing_means_no_compose_call(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "running", "vco_ollama": "exited",
                                                "vco_code_embed": "running"})
                proc = _run_hook(m, shell)
                self.assertEqual(m.compose_calls(), [], proc.stdout + proc.stderr)
                # the stopped adopted container is started BY NAME
                self.assertIn(["start", "vco_ollama"], m.runtime_calls())


# ===========================================================================
# (2) the zombie-recovery gate
# ===========================================================================

class HookZombieGateTests(_TmpCase, _Shells):
    def test_a_zombie_adopted_container_is_started_never_removed(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "zombie", "vco_ollama": "running",
                                                "vco_code_embed": "running"})
                proc = _run_hook(m, shell)
                rt = m.runtime_calls()
                self.assertFalse([c for c in rt if c[0] == "rm"], f"{rt}\n{proc.stdout}")
                self.assertIn(["start", "vco_weaviate"], rt)
                self.assertEqual(m.compose_calls(), [])
                self.assertIn("delete --force id-vco_weaviate", "\n".join(m.lines(m.runc_log)))

    def test_a_zombie_managed_container_is_removed_and_recomposed_alone(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "running", "vco_ollama": "running",
                                                "vco_code_embed": "zombie"})
                proc = _run_hook(m, shell)
                rt = m.runtime_calls()
                self.assertIn(["rm", "--force", "vco_code_embed"], rt, proc.stdout + proc.stderr)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, calls)
                _assert_explicit_managed_only(self, calls[0], ["code_embed"])
                self.assertIn("recovered zombie container 'vco_code_embed'", proc.stdout)


class HookSessionReconcileTests(_TmpCase, _Shells):
    """The hook runs `service_endpoints reconcile --phase session --json`
    BEFORE its lifecycle plan, bounded, and never lets it block the hook."""

    ALL_MANAGED = [_row("weaviate"), _row("ollama"), _row("code_embed")]

    def test_reconcile_runs_before_the_plan_and_the_hook_obeys_the_corrected_row(self):
        """The row said VCO-managed; reconcile finds the container is another
        project's and adopts it. A plan read BEFORE the reconcile would
        `rm --force` that zombie and re-create it on VCO's own volume."""
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(self.ALL_MANAGED, {"vco_weaviate": "zombie", "vco_ollama": "running",
                                                    "vco_code_embed": "running"})
                proc = _run_hook(m, shell, FAKE_RECONCILE_ADOPT_WEAVIATE="1")
                rt = m.runtime_calls()
                self.assertIn(["RECONCILE"], rt, proc.stdout + proc.stderr)
                first_inspect = next(i for i, c in enumerate(rt) if c[0] == "inspect")
                self.assertLess(rt.index(["RECONCILE"]), first_inspect, rt)
                self.assertFalse([c for c in rt if c[0] == "rm"], f"{rt}\n{proc.stdout}")
                self.assertIn(["start", "vco_weaviate"], rt)
                self.assertEqual(m.compose_calls(), [])

    def test_an_unreachable_entry_reaches_the_session(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "running", "vco_ollama": "running",
                                                "vco_code_embed": "running"})
                proc = _run_hook(m, shell, FAKE_RECONCILE_ENTRIES="service_endpoint_unreachable")
                self.assertIn("need attention: service_endpoint_unreachable", proc.stdout,
                              proc.stderr)

    def test_a_failing_reconcile_never_blocks_the_lifecycle(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "running", "vco_ollama": "running",
                                                "vco_code_embed": "missing"})
                proc = _run_hook(m, shell, FAKE_RECONCILE_EXIT="3")
                self.assertEqual(proc.returncode, 0)
                self.assertIn("reconcile exited 3", proc.stdout, proc.stderr)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}")
                _assert_explicit_managed_only(self, calls[0], ["code_embed"])

    def test_a_hung_reconcile_is_bounded_and_the_lifecycle_still_runs(self):
        import time

        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "running", "vco_ollama": "running",
                                                "vco_code_embed": "missing"})
                started = time.monotonic()
                proc = _run_hook(m, shell, FAKE_RECONCILE_SLEEP="60")
                elapsed = time.monotonic() - started
                # 8 s bound + interpreter start-up; well inside the hook's 15 s.
                self.assertLess(elapsed, 14, proc.stdout)
                self.assertIn("did not finish within 8 s", proc.stdout, proc.stderr)
                self.assertEqual(len(m.compose_calls()), 1, proc.stdout)


@unittest.skipIf(IS_WINDOWS, "bash hook")
class VerifyContainerPortsZombieGateTests(_TmpCase):
    """`verify-container-ports.sh` — the second zombie-recovery site.

    Bash only: the hook probes the LITERAL default ports (8081, …), and a
    fake `curl` on PATH keeps the bash probe off the network. The .ps1
    sibling probes with Invoke-WebRequest / TcpClient, which no PATH fake
    can intercept, so driving it here would contact whatever real service
    listens on 8081 — the lane rules forbid that; its gate mirrors this one
    line for line."""

    def _run(self, m: _Machine) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(HOOKS / "verify-container-ports.sh")],
                              env=m.env(), capture_output=True, text=True, timeout=120,
                              cwd=str(REPO_ROOT))

    def test_an_adopted_zombie_is_never_removed(self):
        m = self.machine(DOGFOOD_ROWS, {"vco_weaviate": "zombie"})
        proc = self._run(m)
        self.assertIn("zombie state(s) detected", proc.stdout, proc.stderr)
        self.assertFalse([c for c in m.runtime_calls() if c[0] == "rm"], m.runtime_calls())
        self.assertEqual(m.compose_calls(), [])
        self.assertIn("vco_weaviate is not VCO-managed", proc.stdout)

    def test_a_managed_zombie_is_recreated_alone_with_no_deps(self):
        m = self.machine([_row("weaviate"), _row("ollama"), _row("code_embed")],
                         {"vco_weaviate": "zombie"})
        proc = self._run(m)
        self.assertIn(["rm", "-f", "vco_weaviate"], m.runtime_calls(), proc.stdout)
        calls = m.compose_calls()
        self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}")
        _assert_explicit_managed_only(self, calls[0], ["weaviate"], forbidden=("ollama",))


# ===========================================================================
# (7) the boot / GPU wrapper
# ===========================================================================

class WrapperServiceListTests(_TmpCase, _Shells):
    def test_an_explicit_request_for_adopted_services_composes_nothing(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(DOGFOOD_ROWS, {})
                proc = _run_wrapper(m, shell, ["start", "weaviate", "ollama"])
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(m.compose_calls(), [], proc.stdout)
                self.assertIn("never composed", proc.stdout)

    def test_an_empty_caller_list_is_no_compose_call(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine([_row("weaviate"), _row("ollama"), _row("code_embed")], {})
                proc = _run_wrapper(m, shell, [], VCO_COMPOSE_SERVICES="")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(m.compose_calls(), [], proc.stdout)

    def test_boot_path_composes_managed_and_starts_adopted_by_name(self):
        rows = [DOGFOOD_ROWS[0], _row("ollama"), _row("code_embed")]
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine(rows, {})
                proc = _run_wrapper(m, shell, [])
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
                # CPU host (fake nvidia-smi fails): code_embed is dropped, as the
                # profile-less whole-stack `up` always left it out.
                _assert_explicit_managed_only(self, calls[0], ["ollama"], forbidden=("weaviate",))
                self.assertIn("docker-compose.yml", calls[0])
                self.assertIn(["start", "vco_weaviate"], m.runtime_calls())

    def test_without_a_working_dir_the_installer_compose_is_the_default(self):
        """The launcher calls the wrapper with no VCT_STACK_WORKING_DIR: the
        installer's infrastructure/ must win over the legacy compose home."""
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine([_row("weaviate"), _row("ollama"), _row("code_embed")], {})
                root = m.tmp / "orch"
                (root / "infrastructure").mkdir(parents=True)
                (root / "infrastructure" / "docker-compose.yml").write_text("services: {}\n")
                (root / "claude_mcp_servers").mkdir()
                (root / "claude_mcp_servers" / "compose.yaml").write_text("services: {}\n")
                env = m.env(VCT_ORCHESTRATOR_ROOT=str(root))
                env.pop("VCT_STACK_WORKING_DIR")
                argv = (["bash", str(WRAPPER_SH), "start"] if shell == "bash" else
                        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         str(WRAPPER_PS1), "start"])
                proc = subprocess.run(argv, env={**env, "VCO_COMPOSE_SERVICES": "weaviate"},
                                      capture_output=True, text=True, timeout=180, cwd=str(m.tmp))
                self.assertIn(f"working_dir={root / 'infrastructure'}", proc.stdout, proc.stderr)
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}")
                self.assertIn("docker-compose.yml", calls[0])
                _assert_explicit_managed_only(self, calls[0], ["weaviate"], forbidden=("ollama",))

    def test_a_caller_list_is_intersected_with_the_managed_services(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                m = self.machine([DOGFOOD_ROWS[0], _row("ollama"), _row("code_embed")], {})
                proc = _run_wrapper(m, shell, ["weaviate", "ollama"])
                calls = m.compose_calls()
                self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}")
                _assert_explicit_managed_only(self, calls[0], ["ollama"], forbidden=("weaviate",))
                # an explicit list never starts adopted containers
                self.assertNotIn(["start", "vco_weaviate"], m.runtime_calls())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
