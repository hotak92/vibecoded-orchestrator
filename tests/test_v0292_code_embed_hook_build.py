# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 BLOCKER-1 — the SessionStart bring-up hooks rebuild and report.

Drives the REAL hook scripts (``ensure-code-embed-service.sh`` and its
``.ps1`` sibling, R42: parity by writing) against a fake container runtime and
a fake compose binary, and reads the argv they actually produced. A hook that
creates the container from a stale image is the same delivery defect one layer
down from ``install.py``, so the flag has to be proven where it is emitted,
not where it is written.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
IS_WINDOWS = os.name == "nt"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Fixture:
    """A fake podman + a fake compose whose argv we can read back.

    The fake runtime reports the container as MISSING until the fake compose
    "creates" it (a marker file), which is what the hook's post-``--build``
    existence check reads — so the retry arm is exercised by a compose that
    fails, not by a stub that always says "created".
    """

    def __init__(self, tmp: Path, compose_fails_on_build: bool = False):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.compose_log = tmp / "compose.log"
        self.marker = tmp / "container.created"
        self.compose_dir = tmp / "compose"
        self.compose_dir.mkdir(exist_ok=True)

        (self.bin / "podman").write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            case "$1" in
              info) exit 0 ;;
              container)
                if [ "$2" = "inspect" ]; then
                  [ -f "{self.marker}" ] || exit 1
                  echo "running"
                  exit 0
                fi ;;
              compose) exit 1 ;;
            esac
            exit 0
            """))
        fail_arm = (
            'if printf "%s\\n" "$@" | grep -q -- "--build"; then exit 1; fi\n'
            if compose_fails_on_build else ""
        )
        (self.bin / "vco-fake-compose").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.compose_log}"\n'
            f"{fail_arm}"
            f'touch "{self.marker}"\n'
            "exit 0\n"
        )
        for f in ("podman", "vco-fake-compose"):
            (self.bin / f).chmod(0o755)

    def env(self, port: int) -> dict:
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(REPO_ROOT),
            "VCT_COMPOSE_CMD": "vco-fake-compose",
            "VCT_COMPOSE_DIR": str(self.compose_dir),
            "VCT_CODE_EMBED_CONTAINER": "vco_code_embed_test",
            "CODE_EMBED_PORT": str(port),
            "TMPDIR": str(self.tmp),
            # Hermeticity pin (2026-09-07 CI red): the hooks resolve their
            # runtime via `python -m vco_lib.containers resolve`, probing
            # podman-then-docker INCLUDING compose availability. The fake
            # podman above answers `version`/`info` but refuses `compose`,
            # so on a machine with no podman-compose but a usable docker
            # (GitHub runners) the resolver hands the hook REAL docker —
            # `container inspect` then never sees this fixture's marker and
            # the compose-invocation accounting goes machine-dependent (CI:
            # `2 != 1 : ['up -d --build code_embed', 'up -d code_embed']`,
            # and `--build` firing for a weaviate-only outage). Pinning the
            # runtime onto the fake podman makes RUNTIME — and therefore
            # which binary answers every inspect — determined by the test.
            "VCT_CONTAINER_RUNTIME": "podman",
        })
        env.pop("VCT_DISABLE_HOOKS", None)
        env.pop("VCO_VENV_PYTHON", None)
        # The venv-resolution INPUTS the sourced helper reads; scrub so an
        # ambient launcher shell cannot steer RUN_PY at a different tree.
        env.pop("VCT_VENV", None)
        env.pop("VCT_INSTALL_ROOT", None)
        return env

    def compose_invocations(self) -> list:
        if not self.compose_log.exists():
            return []
        return [ln for ln in self.compose_log.read_text().splitlines() if ln.strip()]


@unittest.skipIf(IS_WINDOWS, "bash hook; the .ps1 sibling is covered below")
class BashHookTests(unittest.TestCase):
    def _run(self, fixture: _Fixture, port: int):
        return subprocess.run(
            ["bash", str(HOOKS / "ensure-code-embed-service.sh")],
            env=fixture.env(port), capture_output=True, text=True, timeout=180,
            cwd=str(REPO_ROOT),
        )

    def test_compose_leg_passes_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp))
            proc = self._run(fx, _free_port())
            calls = fx.compose_invocations()
            self.assertTrue(calls, f"compose was never invoked:\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("up -d --build code_embed", calls[0])
            # A successful build must NOT trigger the compatibility retry.
            self.assertEqual(len(calls), 1, calls)
            self.assertNotIn("was NOT rebuilt", proc.stdout)

    def test_a_compose_that_rejects_build_still_brings_the_service_up_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp), compose_fails_on_build=True)
            proc = self._run(fx, _free_port())
            calls = fx.compose_invocations()
            self.assertEqual(len(calls), 2, f"{calls}\n{proc.stdout}")
            self.assertIn("--build", calls[0])
            self.assertNotIn("--build", calls[1])
            self.assertIn("was NOT rebuilt", proc.stdout)


@unittest.skipIf(IS_WINDOWS, "bash hook; the .ps1 sibling mirrors it")
class EnsureContainersBuildGateTests(unittest.TestCase):
    """`ensure-containers` rebuilds ONLY when code_embed is among the missing.

    The gate matters both ways: without it a stale image is baked into a
    freshly created container (the defect), and an unconditional `--build`
    would rebuild a 6 GB CUDA image on a session-start hook every time any
    container went away (the over-correction).
    """

    def _run(self, missing: str):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            compose_log = tmp / "compose.log"
            (bin_dir / "podman").write_text(textwrap.dedent(f"""\
                #!/usr/bin/env bash
                if [ "$1" = "info" ]; then exit 0; fi
                if [ "$1" = "inspect" ]; then
                  case "$2" in
                    {missing}) exit 1 ;;
                  esac
                  # `inspect <name> --format <fmt>`: the format is $4.
                  case "$4" in
                    *State.Pid*) echo 1 ;;
                    *) echo running ;;
                  esac
                  exit 0
                fi
                if [ "$1" = "compose" ]; then exit 1; fi
                exit 0
                """))
            (bin_dir / "vco-fake-compose").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{compose_log}"\n'
                "exit 0\n"
            )
            for f in ("podman", "vco-fake-compose"):
                (bin_dir / f).chmod(0o755)
            orch = tmp / "orch"       # no scripts/ ⇒ no wrapper ⇒ direct compose
            orch.mkdir()
            compose_dir = tmp / "compose"
            compose_dir.mkdir()
            env = dict(os.environ)
            env.update({
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(REPO_ROOT),
                "VCT_ORCHESTRATOR_ROOT": str(orch),
                "VCT_COMPOSE_CMD": "vco-fake-compose",
                "VCT_COMPOSE_DIR": str(compose_dir),
                "VCT_REQUIRED_CONTAINERS": "vco_weaviate vco_ollama vco_code_embed",
                "TMPDIR": str(tmp),
                # Hermeticity pin — same reason as _Fixture.env: keep
                # vco_lib.containers resolve on THIS fixture's fake podman
                # regardless of what runtimes the host machine offers.
                "VCT_CONTAINER_RUNTIME": "podman",
            })
            env.pop("VCT_DISABLE_HOOKS", None)
            env.pop("VCT_VENV", None)
            env.pop("VCT_INSTALL_ROOT", None)
            proc = subprocess.run(
                ["bash", str(HOOKS / "ensure-containers.sh")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            calls = (compose_log.read_text().splitlines()
                     if compose_log.exists() else [])
            return proc, [c for c in calls if c.strip()]

    def test_missing_code_embed_rebuilds(self):
        proc, calls = self._run("vco_code_embed")
        self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("--build", calls[0])

    def test_missing_weaviate_alone_does_not_rebuild(self):
        proc, calls = self._run("vco_weaviate")
        self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("--build", calls[0])


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"),
                     "PowerShell not available")
class EnsureContainersBuildGatePs1Tests(unittest.TestCase):
    """R42: the .ps1 gate is proven by RUNNING it, not by reading the .sh."""

    @property
    def shell(self):
        return shutil.which("pwsh") or shutil.which("powershell")

    def _run(self, missing: str):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            compose_log = tmp / "compose.log"
            (bin_dir / "podman").write_text(textwrap.dedent(f"""\
                #!/usr/bin/env bash
                if [ "$1" = "info" ]; then exit 0; fi
                if [ "$1" = "inspect" ]; then
                  case "$2" in
                    {missing}) exit 1 ;;
                  esac
                  case "$4" in
                    *State.Pid*) echo 1 ;;
                    *) echo running ;;
                  esac
                  exit 0
                fi
                if [ "$1" = "compose" ]; then exit 1; fi
                exit 0
                """))
            (bin_dir / "vco-fake-compose").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{compose_log}"\n'
                "exit 0\n"
            )
            for f in ("podman", "vco-fake-compose"):
                (bin_dir / f).chmod(0o755)
            orch = tmp / "orch"
            orch.mkdir()
            compose_dir = tmp / "compose"
            compose_dir.mkdir()
            env = dict(os.environ)
            env.update({
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(REPO_ROOT),
                "VCT_ORCHESTRATOR_ROOT": str(orch),
                "VCT_COMPOSE_CMD": "vco-fake-compose",
                "VCT_COMPOSE_DIR": str(compose_dir),
                "VCT_REQUIRED_CONTAINERS": "vco_weaviate vco_ollama vco_code_embed",
                "TMPDIR": str(tmp),
                "TEMP": str(tmp),
                # Hermeticity pin — same reason as _Fixture.env (keep the
                # resolver on this fixture's fake podman; see that comment).
                "VCT_CONTAINER_RUNTIME": "podman",
            })
            env.pop("VCT_DISABLE_HOOKS", None)
            env.pop("VCT_VENV", None)
            env.pop("VCT_INSTALL_ROOT", None)
            proc = subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HOOKS / "ensure-containers.ps1")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            calls = (compose_log.read_text().splitlines()
                     if compose_log.exists() else [])
            return proc, [c for c in calls if c.strip()]

    def test_missing_code_embed_rebuilds(self):
        proc, calls = self._run("vco_code_embed")
        self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("--build", calls[0])

    def test_missing_weaviate_alone_does_not_rebuild(self):
        proc, calls = self._run("vco_weaviate")
        self.assertEqual(len(calls), 1, f"{calls}\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("--build", calls[0])


class _HealthHandler(BaseHTTPRequestHandler):
    payload: dict = {"status": "ok"}

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


class _HealthService:
    def __init__(self, payload: dict):
        _HealthHandler.payload = payload
        self.httpd = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipIf(IS_WINDOWS, "bash hook; the .ps1 sibling is covered below")
class BashStalenessReportTests(unittest.TestCase):
    """A RUNNING service that is stale must say so at session start.

    This is the only surface between updates, so it is the one that decides
    whether a user learns their code graph is being embedded through a
    truncating service.
    """

    def _run_against(self, payload):
        with tempfile.TemporaryDirectory() as tmp, _HealthService(payload) as svc:
            fx = _Fixture(Path(tmp))
            fx.marker.touch()  # runtime reports the container as running
            proc = subprocess.run(
                ["bash", str(HOOKS / "ensure-code-embed-service.sh")],
                env=fx.env(svc.port), capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            return proc

    def test_pre_v0292_image_is_reported(self):
        proc = self._run_against({"status": "ok", "dim": 2048})
        self.assertIn("Already running", proc.stdout)
        self.assertIn("predates v0.2.92", proc.stdout, proc.stdout + proc.stderr)

    def test_a_current_service_prints_nothing_extra(self):
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib import code_embed_image

        sha = code_embed_image.checkout_source_sha(REPO_ROOT)
        proc = self._run_against({"status": "ok", "source_sha": sha})
        self.assertIn("Already running", proc.stdout)
        self.assertNotIn("predates", proc.stdout)
        self.assertNotIn("OLDER source", proc.stdout)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"),
                     "PowerShell not available")
class PowerShellHookTests(unittest.TestCase):
    """R42: the .ps1 sibling is proven by RUNNING it, not by reading it."""

    @property
    def shell(self):
        return shutil.which("pwsh") or shutil.which("powershell")

    def test_compose_leg_passes_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp))
            env = fx.env(_free_port())
            env["TEMP"] = str(fx.tmp)
            proc = subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HOOKS / "ensure-code-embed-service.ps1")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            calls = fx.compose_invocations()
            self.assertTrue(calls, f"compose was never invoked:\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("up -d --build code_embed", calls[0])
            self.assertEqual(len(calls), 1, calls)

    def test_a_compose_that_rejects_build_still_brings_the_service_up(self):
        """Parity with the bash retry arm — proven by running, not by reading."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp), compose_fails_on_build=True)
            env = fx.env(_free_port())
            env["TEMP"] = str(fx.tmp)
            proc = subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HOOKS / "ensure-code-embed-service.ps1")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            calls = fx.compose_invocations()
            self.assertEqual(len(calls), 2, f"{calls}\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("--build", calls[0])
            self.assertNotIn("--build", calls[1])
            self.assertIn("was NOT rebuilt", proc.stdout)

    def test_staleness_is_reported_for_a_running_pre_v0292_service(self):
        with tempfile.TemporaryDirectory() as tmp, \
                _HealthService({"status": "ok", "dim": 2048}) as svc:
            fx = _Fixture(Path(tmp))
            fx.marker.touch()
            env = fx.env(svc.port)
            env["TEMP"] = str(fx.tmp)
            proc = subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HOOKS / "ensure-code-embed-service.ps1")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            self.assertIn("Already running", proc.stdout)
            self.assertIn("predates v0.2.92", proc.stdout, proc.stdout + proc.stderr)

    def test_single_token_compose_is_invoked_once_not_twice(self):
        """The splitter fix: `podman-compose` must not become its own argument."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(Path(tmp))
            env = fx.env(_free_port())
            env["TEMP"] = str(fx.tmp)
            subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HOOKS / "ensure-code-embed-service.ps1")],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT),
            )
            calls = fx.compose_invocations()
            self.assertTrue(calls)
            self.assertNotIn("vco-fake-compose", calls[0],
                             "the compose name leaked into its own argv (1..0 range bug)")


if __name__ == "__main__":
    unittest.main()
