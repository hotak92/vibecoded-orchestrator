# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The usage status-line scripts and the launcher's Python bridge.

``templates/scripts/gateway-usage-statusline.{sh,ps1}`` print the line the
gateway renders on ``/usage/windows?format=line``; ``vco_lib.gateway_usage``
fetches the JSON snapshot for the launcher. All three are driven here against
a FAKE gateway on loopback — never the running one.

What is pinned:

* the line is printed verbatim (first line only), and NOTHING is printed on
  any failure: no token, a 401, nothing listening, a hung gateway — with the
  exit code 0 and stderr empty, because a status line must never show an
  error;
* the call is bounded well under a second (bash) even against a gateway that
  never answers;
* the host token reaches curl on STDIN, never in argv;
* the port each script contacts is the port
  ``vco_lib.vscode_settings.resolve_gateway_ports`` resolves from the same
  evidence (env pin, live port file, last-port record, default) — the scripts
  are a mirror of that chain and this is the parity lock.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from tests.common.child_env import child_env

REPO = Path(__file__).resolve().parents[1]
SCRIPT_SH = REPO / "templates" / "scripts" / "gateway-usage-statusline.sh"
SCRIPT_PS1 = REPO / "templates" / "scripts" / "gateway-usage-statusline.ps1"
_BASH = shutil.which("bash")
_PWSH = shutil.which("pwsh") or shutil.which("powershell")

TOKEN = "statusline-host-token-SYNTHETIC-0123456789abcdef"
LINE = "Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72% │ Qwen 1.2M tok/mo"


class _FakeGateway:
    """A loopback HTTP server answering ``/usage/windows`` like the gateway."""

    def __init__(self, *, status: int = 200, body: str = LINE, delay_s: float = 0.0,
                 json_body: Optional[dict] = None) -> None:
        self.status = status
        self.body = body
        self.delay_s = delay_s
        self.json_body = json_body
        self.seen: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib hook name
                outer.seen.append({"path": self.path,
                                   "authorization": self.headers.get("Authorization")})
                if outer.delay_s:
                    time.sleep(outer.delay_s)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self.send_response(401)
                    self.end_headers()
                    return
                if outer.json_body is not None and "format=line" not in self.path:
                    payload = json.dumps(outer.json_body).encode()
                    ctype = "application/json"
                else:
                    payload = outer.body.encode("utf-8")
                    ctype = "text/plain; charset=utf-8"
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    servers: list[_FakeGateway] = []

    def make(**kwargs) -> _FakeGateway:
        server = _FakeGateway(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _state(tmp_path: Path, *, token: Optional[str] = TOKEN, port: Optional[int] = None,
           last_port: Optional[int] = None) -> Path:
    state = tmp_path / "vct"
    state.mkdir(exist_ok=True)
    if token is not None:
        (state / "model-gateway.token").write_text(token + "\n", encoding="utf-8")
    if port is not None:
        (state / "model-gateway.port").write_text(f"{port}\n", encoding="utf-8")
    if last_port is not None:
        (state / "model-gateway.last-port").write_text(f"{last_port}\n", encoding="utf-8")
    return state


def _env(state: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_MODEL_GATEWAY_PORT", "VCT_GW_TMP_TOKEN")}
    env["VCT_STATE_DIR"] = str(state)
    env["HOME"] = str(state.parent)
    # A proxy that would swallow the call if the scripts honoured it.
    env["http_proxy"] = env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env.update(extra)
    return env


def _run_sh(state: Path, **extra: str) -> "tuple[subprocess.CompletedProcess, float]":
    started = time.monotonic()
    proc = subprocess.run(
        [_BASH, str(SCRIPT_SH)],
        input='{"session_id": "x"}', capture_output=True, text=True,
        env=_env(state, **extra), timeout=20,
    )
    return proc, time.monotonic() - started


def _run_ps1(state: Path, **extra: str) -> "tuple[subprocess.CompletedProcess, float]":
    started = time.monotonic()
    proc = subprocess.run(
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT_PS1)],
        input='{"session_id": "x"}', capture_output=True, text=True,
        encoding="utf-8", env=_env(state, **extra), timeout=60,
    )
    return proc, time.monotonic() - started


needs_bash = pytest.mark.skipif(_BASH is None or not shutil.which("curl"),
                                reason="bash and curl are required")
needs_pwsh = pytest.mark.skipif(_PWSH is None, reason="PowerShell not available on this host")


# ── bash ─────────────────────────────────────────────────────────────────
@needs_bash
def test_sh_prints_the_gateway_line_verbatim(gateway, tmp_path) -> None:
    server = gateway(body=LINE + "\nsecond line ignored\n")
    proc, _ = _run_sh(_state(tmp_path, port=server.port))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, LINE, "")
    assert server.seen[0]["path"] == "/usage/windows?format=line"
    assert server.seen[0]["authorization"] == f"Bearer {TOKEN}"


@needs_bash
@pytest.mark.parametrize("case", ["no_token", "empty_token", "unauthorised", "not_listening", "http_500"])
def test_sh_is_silent_on_every_failure(case, gateway, tmp_path) -> None:
    if case == "no_token":
        server = gateway()
        state = _state(tmp_path, token=None, port=server.port)
    elif case == "empty_token":
        server = gateway()
        state = _state(tmp_path, token="  ", port=server.port)
    elif case == "unauthorised":
        server = gateway()
        state = _state(tmp_path, token="wrong-token", port=server.port)
    elif case == "not_listening":
        server = None
        state = _state(tmp_path, port=_free_port())
    else:
        server = gateway(status=500, body="Traceback: boom")
        state = _state(tmp_path, port=server.port)
    proc, _ = _run_sh(state)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    if case in ("no_token", "empty_token"):
        assert server is not None and server.seen == []  # never even asked


@needs_bash
def test_sh_is_bounded_against_a_hung_gateway(gateway, tmp_path) -> None:
    server = gateway(delay_s=5.0)
    proc, elapsed = _run_sh(_state(tmp_path, port=server.port))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert elapsed < 1.5, elapsed


def _curl_shim(tmp_path: Path) -> "tuple[Path, Path]":
    """A fake ``curl`` that records argv and stdin, then prints nothing."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "curl-call.json"
    shim = shim_dir / "curl"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(log)!r}, 'w'))\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir, log


@needs_bash
def test_sh_hands_the_token_to_curl_on_stdin_never_argv(tmp_path) -> None:
    shim_dir, log = _curl_shim(tmp_path)
    state = _state(tmp_path, port=_free_port())
    proc, _ = _run_sh(state, PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    assert proc.returncode == 0
    call = json.loads(log.read_text(encoding="utf-8"))
    assert not any(TOKEN in arg for arg in call["argv"]), call["argv"]
    assert f"Authorization: Bearer {TOKEN}" in call["stdin"]
    assert "@-" in call["argv"]


def _contacted_port(tmp_path: Path, state: Path, **extra: str) -> int:
    shim_dir, log = _curl_shim(tmp_path)
    proc, _ = _run_sh(state, PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}", **extra)
    assert proc.returncode == 0
    url = next(a for a in json.loads(log.read_text(encoding="utf-8"))["argv"]
               if a.startswith("http://"))
    return int(url.split(":")[2].split("/")[0])


def _python_resolved_port(state: Path, **extra: str) -> int:
    code = ("from vco_lib.vscode_settings import resolve_gateway_ports;"
            "print(resolve_gateway_ports()[0])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=child_env(_env(state, **extra)), timeout=60, check=True)
    return int(out.stdout.strip())


@needs_bash
@pytest.mark.parametrize("case", [
    "env_pin", "garbage_pin_falls_to_port_file", "port_file_over_last_port",
    "last_port_only", "garbage_files_fall_to_default", "nothing_default",
])
def test_sh_port_resolution_matches_vco_lib(case, tmp_path) -> None:
    extra: dict[str, str] = {}
    if case == "env_pin":
        state = _state(tmp_path, port=40001, last_port=40002)
        extra["VCT_MODEL_GATEWAY_PORT"] = "40009"
    elif case == "garbage_pin_falls_to_port_file":
        state = _state(tmp_path, port=40001, last_port=40002)
        extra["VCT_MODEL_GATEWAY_PORT"] = "99999"
    elif case == "port_file_over_last_port":
        state = _state(tmp_path, port=40001, last_port=40002)
    elif case == "last_port_only":
        state = _state(tmp_path, last_port=40002)
    elif case == "garbage_files_fall_to_default":
        state = _state(tmp_path)
        (state / "model-gateway.port").write_text("eleven\n", encoding="utf-8")
        (state / "model-gateway.last-port").write_text("0\n", encoding="utf-8")
    else:
        state = _state(tmp_path)
    assert _contacted_port(tmp_path, state, **extra) == _python_resolved_port(state, **extra)


# ── PowerShell ───────────────────────────────────────────────────────────
@needs_pwsh
def test_ps1_prints_the_gateway_line_verbatim(gateway, tmp_path) -> None:
    server = gateway(body=LINE + "\r\nsecond line ignored\r\n")
    proc, _ = _run_ps1(_state(tmp_path, port=server.port))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, LINE, "")
    assert server.seen[0]["authorization"] == f"Bearer {TOKEN}"


@needs_pwsh
@pytest.mark.parametrize("case", ["no_token", "unauthorised", "not_listening"])
def test_ps1_is_silent_on_every_failure(case, gateway, tmp_path) -> None:
    if case == "no_token":
        state = _state(tmp_path, token=None, port=gateway().port)
    elif case == "unauthorised":
        state = _state(tmp_path, token="wrong-token", port=gateway().port)
    else:
        state = _state(tmp_path, port=_free_port())
    proc, _ = _run_ps1(state)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


@needs_pwsh
def test_ps1_is_bounded_against_a_hung_gateway(gateway, tmp_path) -> None:
    # Measured against the same script answering a fast gateway, so the
    # interpreter's own start-up is not charged to the request timeout.
    fast = gateway()
    (tmp_path / "fast").mkdir()
    _, baseline = _run_ps1(_state(tmp_path / "fast", port=fast.port))
    slow = gateway(delay_s=5.0)
    proc, elapsed = _run_ps1(_state(tmp_path, port=slow.port))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert elapsed - baseline < 1.5, (elapsed, baseline)


@needs_pwsh
@pytest.mark.parametrize("case", ["env_pin", "port_file_over_last_port", "last_port_only"])
def test_ps1_port_resolution_matches_vco_lib(case, gateway, tmp_path) -> None:
    target = gateway()
    decoy = gateway(body="WRONG PORT")
    extra: dict[str, str] = {}
    if case == "env_pin":
        state = _state(tmp_path, port=decoy.port, last_port=decoy.port)
        extra["VCT_MODEL_GATEWAY_PORT"] = str(target.port)
    elif case == "port_file_over_last_port":
        state = _state(tmp_path, port=target.port, last_port=decoy.port)
    else:
        state = _state(tmp_path, last_port=target.port)
    assert _python_resolved_port(state, **extra) == target.port
    proc, _ = _run_ps1(state, **extra)
    assert proc.stdout == LINE
    assert decoy.seen == []


# ── the launcher's Python bridge ─────────────────────────────────────────
def _run_bridge(state: Path, **extra: str) -> dict:
    proc = subprocess.run([sys.executable, "-m", "vco_lib.gateway_usage", "--timeout", "2"],
                          capture_output=True, text=True, env=child_env(_env(state, **extra)),
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
    return json.loads(proc.stdout)


SNAPSHOT = {"generated_at": "2026-09-23T04:00:00Z", "refreshing": False,
            "last_refresh_at": None, "refresh_interval_s": 240,
            "vendors": [{"id": "anthropic", "label": "Claude", "windows": []}]}


def test_bridge_returns_the_snapshot(gateway, tmp_path) -> None:
    server = gateway(json_body=SNAPSHOT)
    result = _run_bridge(_state(tmp_path, port=server.port))
    assert result == {"ok": True, "port": server.port, "snapshot": SNAPSHOT}
    assert server.seen[0]["path"] == "/usage/windows"


@pytest.mark.parametrize("case,reason", [
    ("no_token", "no_token"),
    ("unauthorised", "unauthorised"),
    ("not_listening", "unreachable"),
    ("not_a_gateway", "bad_answer"),
    ("outdated", "outdated_gateway"),
])
def test_bridge_failures_are_structured(case, reason, gateway, tmp_path) -> None:
    if case == "no_token":
        state = _state(tmp_path, token=None, port=gateway().port)
    elif case == "unauthorised":
        state = _state(tmp_path, token="wrong-token", port=gateway().port)
    elif case == "not_listening":
        state = _state(tmp_path, port=_free_port())
    elif case == "outdated":
        state = _state(tmp_path, port=gateway(status=404, body="no route").port)
    else:
        state = _state(tmp_path, port=gateway(json_body={"hello": "world"}).port)
    result = _run_bridge(state)
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result["message"]


@pytest.mark.parametrize("poisoned,names", [
    ("vco_lib.vscode_settings", "vco_lib.vscode_settings"),
    ("model_router.config", "gateway package"),
])
def test_bridge_broken_install_is_loud(poisoned, names, gateway, tmp_path) -> None:
    """F4: a failed import is a BROKEN INSTALL — its own reason, exit 1 and a
    stderr line — never folded into ``no_token``, which the card hides."""
    server = gateway(json_body=SNAPSHOT)
    state = _state(tmp_path, port=server.port)
    code = (
        "import sys\n"
        f"sys.modules[{poisoned!r}] = None  # import now raises ImportError\n"
        "from vco_lib.gateway_usage import main\n"
        "raise SystemExit(main(['--timeout', '2']))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=child_env(_env(state)), timeout=60)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    result = json.loads(proc.stdout)
    assert result["ok"] is False
    assert result["reason"] == "broken_install"
    assert names in result["message"]
    assert "install.py --update" in result["message"]
    assert "broken install" in proc.stderr
    assert server.seen == []  # it never got as far as asking the gateway
    assert TOKEN not in proc.stdout + proc.stderr


def test_bridge_gateway_states_stay_exit_zero(tmp_path) -> None:
    """The quiet side of F4: "nothing is listening" is an answer, not a crash."""
    result = _run_bridge(_state(tmp_path, port=_free_port()))  # asserts exit 0
    assert result["reason"] == "unreachable"
