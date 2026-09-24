# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Review R6 F51: the context-size-check hook's cost on every session start.

Before: the ``.ps1`` spawned one full PowerShell child PER setting, and both
hooks could wait 2 x 5 s on a hub port that accepts and then hangs. Now:

* the shipped resolver pair has a ``resolve-many`` form (several keys, ONE
  process, the project looked up once, the hub not asked again once it has
  stopped answering) and a ``VCT_RESOLVE_MAX_TIME`` budget on its total hub
  time — the single-key contract is unchanged;
* each hook resolves whichever settings the environment does not set in ONE
  resolver spawn, with a 1 s budget; on timeout the defaults apply, silently;
  the 50..2000 bounds still apply to a resolved value.

Every run pins the hub to the discard port or to a local fake, and the state
dir / file store / Claude home to tmp dirs. No real hub, no network.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
SCRIPTS = REPO / "templates" / "scripts"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
KEYS = ("CONTEXT_STATE_MAX_LINES", "MEMORY_MAX_LINES")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh")


# ─── fixtures: fake hubs ────────────────────────────────────────────────


@contextmanager
def hanging_hub() -> Iterator[tuple[int, list[int]]]:
    """Accepts every connection and never answers. Yields (port, [count])."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)
    srv.settimeout(0.2)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            accepted.append(conn)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    count = [0]
    try:
        yield srv.getsockname()[1], count
    finally:
        count[0] = len(accepted)
        stop.set()
        t.join(timeout=2)
        for c in accepted:
            c.close()
        srv.close()


@contextmanager
def slow_hub(values: dict[str, str], delay_s: float) -> Iterator[int]:
    """A live hub that answers by-path + /env after ``delay_s``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: object, **k: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            time.sleep(delay_s)
            url = urlparse(self.path)
            if url.path.endswith("/projects/by-path"):
                status, body = 200, {"id": "p-r6"}
            else:
                key = parse_qs(url.query).get("key", [""])[0]
                if key in values:
                    status, body = 200, {key: values[key]}
                else:
                    status, body = 404, {"error": {"code": "key_not_active"}}
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


# ─── the resolver pair ──────────────────────────────────────────────────


def _resolver_env(tmp_path: Path, port: int, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("VCT_") and k not in KEYS}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update({
        "HOME": str(tmp_path / "home"),
        "VCT_HUB_PORT": str(port),
        "VCT_STATE_DIR": str(tmp_path / "state"),
        "VCT_SECRETS_DIR": str(tmp_path / "secrets"),
        "VCT_HUB_TOKEN": "canary-token-not-a-secret",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    })
    env.update(extra)
    return env


def _resolve(kind: str, tmp_path: Path, args: list[str], port: int = 9,
             **extra: str) -> tuple[subprocess.CompletedProcess, float]:
    if kind == "sh":
        cmd = [str(BASH), str(SCRIPTS / "vct_secrets_resolve.sh"), *args]
    else:
        cmd = [str(PWSH), "-NoProfile", "-File", str(SCRIPTS / "vct_secrets_resolve.ps1"), *args]
    start = time.monotonic()
    done = subprocess.run(cmd, capture_output=True, text=True,
                          env=_resolver_env(tmp_path, port, **extra), timeout=120)
    return done, time.monotonic() - start


def _project(tmp_path: Path, dotenv: str = "") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    if dotenv:
        (proj / ".env").write_text(dotenv)
    return proj


KINDS = [pytest.param("sh", marks=needs_bash), pytest.param("ps1", marks=needs_pwsh)]


@pytest.mark.parametrize("kind", KINDS)
def test_resolve_many_answers_every_key_through_the_chain(kind: str, tmp_path: Path) -> None:
    """Two keys, two tiers (file store, project .env), one process."""
    shared = tmp_path / "secrets" / "shared"
    shared.mkdir(parents=True)
    (shared / "MEMORY_MAX_LINES").write_text("150\n")
    proj = _project(tmp_path, "CONTEXT_STATE_MAX_LINES=\"120\"\n")
    done, _ = _resolve(kind, tmp_path, ["resolve-many", str(proj), *KEYS])
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["CONTEXT_STATE_MAX_LINES=120", "MEMORY_MAX_LINES=150"]


@pytest.mark.parametrize("kind", KINDS)
def test_resolve_many_prints_only_what_resolved_and_exits_with_the_first_miss(
    kind: str, tmp_path: Path,
) -> None:
    proj = _project(tmp_path, "MEMORY_MAX_LINES=90\n")
    done, _ = _resolve(kind, tmp_path, ["resolve-many", str(proj), *KEYS])
    # The hub is unreachable (discard port) → the missing key's code is 1.
    assert done.returncode == 1
    assert done.stdout.splitlines() == ["MEMORY_MAX_LINES=90"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("args", [["resolve-many", "/p"], ["resolve-many", "/p", "BAD-KEY"],
                                  ["resolve-many", "/p", "OK", "X=Y"]])
def test_resolve_many_refuses_a_bad_call(kind: str, tmp_path: Path, args: list[str]) -> None:
    """No key, or a key that is not env-var shaped (it could not round-trip
    through a KEY=VALUE line) → usage exit 64, nothing on stdout."""
    done, _ = _resolve(kind, tmp_path, args)
    assert done.returncode == 64, done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize("kind", KINDS)
def test_a_value_with_a_line_break_is_not_printed_as_a_line(kind: str, tmp_path: Path) -> None:
    shared = tmp_path / "secrets" / "shared"
    shared.mkdir(parents=True)
    (shared / "CONTEXT_STATE_MAX_LINES").write_text("1\nMEMORY_MAX_LINES=60")
    proj = _project(tmp_path, "MEMORY_MAX_LINES=90\n")
    done, _ = _resolve(kind, tmp_path, ["resolve-many", str(proj), *KEYS])
    assert done.returncode == 4
    assert done.stdout.splitlines() == ["MEMORY_MAX_LINES=90"]
    assert "line break" in done.stderr


@pytest.mark.parametrize("kind", KINDS)
def test_the_single_key_contract_is_unchanged(kind: str, tmp_path: Path) -> None:
    proj = _project(tmp_path, "CONTEXT_STATE_MAX_LINES=120\n")
    done, _ = _resolve(kind, tmp_path, [str(proj), "CONTEXT_STATE_MAX_LINES"])
    assert (done.returncode, done.stdout) == (0, "120")
    done, _ = _resolve(kind, tmp_path, [str(proj), "MEMORY_MAX_LINES"])
    assert done.returncode == 1 and done.stdout == ""


@pytest.mark.parametrize("kind", KINDS)
def test_the_budget_bounds_a_hanging_hub_and_asks_it_once(kind: str, tmp_path: Path) -> None:
    """A hub port that accepts and never answers: with a 1 s budget the whole
    resolve-many finishes in about 1 s of hub time (it was 5 s per request),
    the hub is connected to ONCE for both keys, and the .env still answers."""
    proj = _project(tmp_path, "CONTEXT_STATE_MAX_LINES=120\n")
    with hanging_hub() as (port, count):
        done, elapsed = _resolve(kind, tmp_path, ["resolve-many", str(proj), *KEYS],
                                 port=port, VCT_RESOLVE_MAX_TIME="1")
    assert done.stdout.splitlines() == ["CONTEXT_STATE_MAX_LINES=120"]
    assert done.returncode == 1
    assert count[0] == 1, f"the hub was asked {count[0]} times"
    # 1 s budget + interpreter start-up; the old ceiling was 2 x 5 s.
    assert elapsed < (2.5 if kind == "sh" else 4.0), elapsed


@needs_bash
def test_without_a_budget_a_slow_live_hub_is_still_waited_for(tmp_path: Path) -> None:
    """Leave-alone half: VCT_RESOLVE_MAX_TIME unset keeps the 5 s per-request
    cap, so a hub that takes 0.6 s per request answers; with a 1 s budget the
    same hub (two requests, 1.2 s) is given up on and the .env answers."""
    proj = _project(tmp_path, "CONTEXT_STATE_MAX_LINES=120\n")
    with slow_hub({"CONTEXT_STATE_MAX_LINES": "333"}, delay_s=0.6) as port:
        done, _ = _resolve("sh", tmp_path, ["resolve-many", str(proj), "CONTEXT_STATE_MAX_LINES"], port=port)
        assert done.stdout.splitlines() == ["CONTEXT_STATE_MAX_LINES=333"], done.stderr
        done, _ = _resolve("sh", tmp_path, ["resolve-many", str(proj), "CONTEXT_STATE_MAX_LINES"],
                           port=port, VCT_RESOLVE_MAX_TIME="1")
        assert done.stdout.splitlines() == ["CONTEXT_STATE_MAX_LINES=120"], done.stderr


@needs_pwsh
def test_the_powershell_resolver_reads_a_live_hub_through_resolve_many(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    with slow_hub({"CONTEXT_STATE_MAX_LINES": "333", "MEMORY_MAX_LINES": "222"}, delay_s=0.0) as port:
        done, _ = _resolve("ps1", tmp_path, ["resolve-many", str(proj), *KEYS], port=port)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["CONTEXT_STATE_MAX_LINES=333", "MEMORY_MAX_LINES=222"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_budget_that_is_not_a_number_is_ignored(kind: str, tmp_path: Path) -> None:
    proj = _project(tmp_path, "CONTEXT_STATE_MAX_LINES=120\n")
    done, _ = _resolve(kind, tmp_path, [str(proj), "CONTEXT_STATE_MAX_LINES"], VCT_RESOLVE_MAX_TIME="soon")
    assert (done.returncode, done.stdout) == (0, "120")
    assert "VCT_RESOLVE_MAX_TIME is not a number" in done.stderr


# ─── the hooks ──────────────────────────────────────────────────────────

# A stand-in resolver that records every invocation (one line each) and
# answers from a canned table — proves the hook's spawn count and parsing.
_SPY_SH = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SPY_LOG"
printf 'budget=%s\n' "${VCT_RESOLVE_MAX_TIME:-}" >> "$SPY_LOG"
[ "$1" = "resolve-many" ] || exit 9
shift 2
for k in "$@"; do
    v=$(grep "^$k=" "$SPY_ANSWERS" | head -1 | cut -d= -f2-)
    [ -n "$v" ] && printf '%s=%s\n' "$k" "$v"
done
exit 0
'''

_SPY_PS1 = r'''
Add-Content -Path $env:SPY_LOG -Value ($args -join ' ')
Add-Content -Path $env:SPY_LOG -Value ("budget=" + $env:VCT_RESOLVE_MAX_TIME)
if ($args[0] -ne 'resolve-many') { exit 9 }
foreach ($k in $args[2..($args.Count - 1)]) {
    foreach ($line in (Get-Content $env:SPY_ANSWERS)) {
        if ($line.StartsWith("$k=")) { [Console]::Out.Write("$line`n"); break }
    }
}
exit 0
'''


def _hook_project(tmp_path: Path, kind: str, spy: bool) -> Path:
    proj = tmp_path / "hookproj"
    hooks = proj / ".claude" / "hooks"
    scripts = proj / ".claude" / "scripts"
    shutil.copytree(HOOKS / "_lib", hooks / "_lib")
    shutil.copy2(HOOKS / f"context-size-check.{kind}", hooks)
    scripts.mkdir(parents=True)
    target = scripts / f"vct_secrets_resolve.{kind}"
    if spy:
        target.write_text(_SPY_SH if kind == "sh" else _SPY_PS1)
    else:
        shutil.copy2(SCRIPTS / f"vct_secrets_resolve.{kind}", target)
    return proj


def _run_hook(kind: str, proj: Path, tmp_path: Path, port: int = 9, **extra: str) -> tuple[str, float]:
    env = _resolver_env(tmp_path, port)
    env.pop("VCT_HUB_TOKEN")
    env.update({
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCT_CLAUDE_DIR": str(tmp_path / "claude-home"),
        "SPY_LOG": str(tmp_path / "spy.log"),
        "SPY_ANSWERS": str(tmp_path / "answers"),
    })
    env.update(extra)
    if kind == "sh":
        cmd = [str(BASH), str(proj / ".claude" / "hooks" / "context-size-check.sh")]
    else:
        cmd = [str(PWSH), "-NoProfile", "-File", str(proj / ".claude" / "hooks" / "context-size-check.ps1")]
    start = time.monotonic()
    done = subprocess.run(cmd, input="{}", capture_output=True, text=True, cwd=proj, env=env, timeout=120)
    elapsed = time.monotonic() - start
    assert done.returncode == 0, done.stderr
    return done.stdout, elapsed


def _spy_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "spy.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("kind", KINDS)
def test_the_hook_spawns_the_resolver_once_for_both_settings(kind: str, tmp_path: Path) -> None:
    proj = _hook_project(tmp_path, kind, spy=True)
    (tmp_path / "answers").write_text("CONTEXT_STATE_MAX_LINES=100\nMEMORY_MAX_LINES=60\n")
    (proj / ".claude" / "CONTEXT_STATE.md").write_text("x\n" * 120)
    out, _ = _run_hook(kind, proj, tmp_path)
    calls = _spy_calls(tmp_path)
    assert calls == [f"resolve-many {proj} CONTEXT_STATE_MAX_LINES MEMORY_MAX_LINES", "budget=1"], calls
    assert "threshold: 100 lines" in out


@pytest.mark.parametrize("kind", KINDS)
def test_the_hook_asks_only_for_what_the_environment_does_not_set(kind: str, tmp_path: Path) -> None:
    proj = _hook_project(tmp_path, kind, spy=True)
    (tmp_path / "answers").write_text("CONTEXT_STATE_MAX_LINES=100\nMEMORY_MAX_LINES=60\n")
    _run_hook(kind, proj, tmp_path, CONTEXT_STATE_MAX_LINES="400")
    assert _spy_calls(tmp_path) == [f"resolve-many {proj} MEMORY_MAX_LINES", "budget=1"]
    (tmp_path / "spy.log").unlink()
    _run_hook(kind, proj, tmp_path, CONTEXT_STATE_MAX_LINES="400", MEMORY_MAX_LINES="300")
    assert _spy_calls(tmp_path) == [], "both set → no resolver spawn at all"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("answer", "expected"), [
    ("100", "(threshold: 100 lines)"),
    ("50", "(threshold: 50 lines)"),
    ("2000", "(warning threshold: 1200 lines)"),
    ("49", "(threshold: 500 lines)"),
    ("2001", "(threshold: 500 lines)"),
    ("abc", "(threshold: 500 lines)"),
    ("-5", "(threshold: 500 lines)"),
])
def test_a_resolved_value_keeps_the_50_to_2000_bounds(kind: str, tmp_path: Path, answer: str, expected: str) -> None:
    """A value that arrives through the resolver is bounded exactly like an
    environment value: 50..2000, else the default 500."""
    proj = _hook_project(tmp_path, kind, spy=True)
    (tmp_path / "answers").write_text(f"CONTEXT_STATE_MAX_LINES={answer}\n")
    (proj / ".claude" / "CONTEXT_STATE.md").write_text("x\n" * 1300)
    out, _ = _run_hook(kind, proj, tmp_path)
    assert expected in out, out


@pytest.mark.parametrize("kind", KINDS)
def test_a_hanging_hub_costs_the_hook_about_a_second_and_the_defaults_apply(kind: str, tmp_path: Path) -> None:
    """The real resolver against a port that accepts and hangs: the hook
    finishes within the 1 s budget (+ interpreter start-up; it was up to
    2 x 5 s), prints nothing but its normal output, and uses the defaults."""
    proj = _hook_project(tmp_path, kind, spy=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "hub.token").write_text("canary-token-not-a-secret")
    (proj / ".claude" / "CONTEXT_STATE.md").write_text("x\n" * 320)
    with hanging_hub() as (port, _count):
        out, elapsed = _run_hook(kind, proj, tmp_path, port=port)
    assert "(warning threshold: 300 lines)" in out
    assert "vct-secrets-resolve" not in out
    assert elapsed < (3.0 if kind == "sh" else 6.0), elapsed
