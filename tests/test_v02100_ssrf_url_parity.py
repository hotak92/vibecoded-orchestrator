# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 reviews R18-04 / R18-05 / R18F-02 / R18F-04 / R18F-08: the SSRF
guard's URL decision.

The decision is ONE Python implementation, ``vco_lib/ssrf_url.py``; the
hooks' ``_lib/ssrf-allowlist.{sh,ps1}`` only locate the interpreter, pass the
URL (stdin on POSIX, the hex of its UTF-8 bytes on PowerShell) and map the
verdict. One case table (``tests/fixtures/ssrf_cases.json``) runs at three
levels:

* through the module, in-process (exact verdicts);
* through each shell's thin caller (exact verdicts — this is where a shell
  that mangled the URL on its way to Python, R18F-02's class, shows up);
* end-to-end through both ``pre-tool-use`` hooks (exit 2 exactly for the
  ``block`` cases; ``allow`` and ``pass`` both let the call through).

Plus the fail-closed branches: the lib missing, no Python at all (the ``.sh``
hook used to exit before the guard, R18F-08), and a module that answers
nothing / something unrecognised / crashes (the empty-verdict hardening).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import ssrf_url

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "templates" / "hooks"
LIB_SH = HOOKS / "_lib" / "ssrf-allowlist.sh"
LIB_PS1 = HOOKS / "_lib" / "ssrf-allowlist.ps1"
TABLE = json.loads((REPO / "tests" / "fixtures" / "ssrf_cases.json").read_text(encoding="utf-8"))
_ENV_KEYS = ("WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "CODE_EMBED_SERVICE_URL",
             "VCT_HUB_PORT", "VCT_DISABLE_HOOKS", "PY", "VCT_VENV")
#: What pre-tool-use.sh (and the stderr cap it sources) shells out to before
#: the no-Python branch; the no-Python PATH links exactly these.
_SH_TOOLS = ("cat", "head", "grep", "dirname", "tr", "date", "mktemp", "tail", "rm", "sed")
HAVE_PWSH = shutil.which("pwsh") is not None
#: Absolute, so a child run with the no-Python PATH still starts.
_BASH = shutil.which("bash") or "bash"
_SHELLS = [pytest.param("sh", id="sh")]
if HAVE_PWSH:
    _SHELLS.append(pytest.param("ps1", id="ps1"))


def _cases(env_name: str) -> list:
    cases = [c for c in TABLE["cases"] if c["env"] == env_name]
    assert cases
    return cases


def _env(tmp_path: Path, extra: dict, *, python: str | None = sys.executable) -> dict:
    """A hook child's env: this checkout's vco_lib first on PYTHONPATH, the
    interpreter pinned through VCT_VENV (the resolver's first rung), no
    ambient service env, a throwaway state root (so the hub port is 7700)."""
    env = {k: v for k, v in child_env().items() if k not in _ENV_KEYS}
    env.update({"HOME": str(tmp_path / "home"), "VCT_STATE_DIR": str(tmp_path / "state")})
    if python:
        env["VCT_VENV"] = python
    env.update(extra)
    return env


def _mismatches(cases: list, got: list) -> list:
    assert len(got) == len(cases), got
    return [f"{c['url']!r}: want {c['expect']}, got {g} ({c['why']})"
            for c, g in zip(cases, got) if g != c["expect"]]


# ─── 1. the module ──────────────────────────────────────────────────────


@pytest.mark.parametrize("env_name", sorted(TABLE["envs"]))
def test_every_case_through_the_module(env_name: str, tmp_path: Path, monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    for key, value in TABLE["envs"][env_name].items():
        monkeypatch.setenv(key, value)
    cases = _cases(env_name)
    wrong = _mismatches(cases, [ssrf_url.verdict(c["url"]) for c in cases])
    assert not wrong, "\n".join(wrong)


def test_the_cli_reads_stdin_and_hex_alike(tmp_path: Path) -> None:
    """Both shells' transports reach the same bytes: raw stdin (.sh) and the
    hex of the UTF-8 bytes (.ps1). A non-ASCII host proves the bytes survive."""
    env = _env(tmp_path, TABLE["envs"]["moved"])
    for url, want in (("http://bücher.de/", "pass"), ("http://10.0.0.1:22\\@localhost:18081/", "block")):
        by_stdin = subprocess.run([sys.executable, "-m", "vco_lib.ssrf_url", "verdict"],
                                  input=url.encode("utf-8"), capture_output=True, timeout=60, env=child_env(env))
        by_hex = subprocess.run([sys.executable, "-m", "vco_lib.ssrf_url", "verdict",
                                 "--url-hex", url.encode("utf-8").hex()],
                                capture_output=True, timeout=60, env=child_env(env))
        for res in (by_stdin, by_hex):
            assert res.returncode == 0, res.stderr
            lines = res.stdout.decode().splitlines()
            assert lines[0] == want, lines
            if want == "block":
                assert "localhost:18081" in lines[1].split()


def test_the_table_covers_every_review_bypass() -> None:
    """The table must keep the specific spellings R18-04 / R18-05 named."""
    urls = {c["url"] for c in TABLE["cases"] if c["expect"] == "block"}
    for must in ("http://10.0.0.1:22\\@localhost:18081/", "http://LOCALHOST:6379/",
                 "http://local%68ost:6379/", "http://2130706433/", "http://0x7f000001/",
                 "http://[::]/", "http://[::ffff:7f00:1]/", "http://[0:0:0:0:0:0:0:1]:6379/",
                 "http://0.0.0.0:8081/"):
        assert must in urls, must
    kinds = {c["expect"] for c in TABLE["cases"]}
    assert kinds == {"allow", "block", "pass"}
    # Owner ruling on non-ASCII hosts: converted to IDNA, then judged.
    by = {(c["env"], c["url"]): c["expect"] for c in TABLE["cases"]}
    assert by[("moved", "http://bücher.de/")] == "pass"
    assert by[("moved", "http://ｌｏｃａｌｈｏｓｔ/")] == "block"
    assert by[("moved", "http://１０．０．０．１/")] == "block"


# ─── 2. each shell's thin caller ────────────────────────────────────────


def _verdicts_sh(urls: list, env: dict) -> list:
    script = ('. "$1"; hooks="$2"; shift 2\n'
              'for u in "$@"; do vco_ssrf_run "$u" "$hooks"; printf "%s\\n" "$_vco_ssrf_verdict"; done\n')
    res = subprocess.run([_BASH, "-c", script, "_", str(LIB_SH), str(HOOKS), *urls],
                         capture_output=True, text=True, timeout=300, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


def _verdicts_ps1(urls: list, env: dict, tmp_path: Path) -> list:
    data = tmp_path / "urls.json"
    data.write_text(json.dumps(urls, ensure_ascii=False), encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        "param([string]$Lib, [string]$Hooks, [string]$Data)\n"
        ". $Lib\n"
        "$urls = [System.IO.File]::ReadAllText($Data, [System.Text.Encoding]::UTF8) | ConvertFrom-Json\n"
        "foreach ($u in $urls) { [Console]::Out.WriteLine((Invoke-VcoSsrfCheck -Url $u -HooksDir $Hooks).Verdict) }\n",
        encoding="utf-8")
    res = subprocess.run([shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(driver), "-Lib", str(LIB_PS1),
                          "-Hooks", str(HOOKS), "-Data", str(data)],
                         capture_output=True, text=True, timeout=300, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("env_name", sorted(TABLE["envs"]))
def test_every_case_through_the_shell_caller(impl: str, env_name: str, tmp_path: Path) -> None:
    cases = _cases(env_name)
    env = _env(tmp_path, TABLE["envs"][env_name])
    urls = [c["url"] for c in cases]
    got = _verdicts_sh(urls, env) if impl == "sh" else _verdicts_ps1(urls, env, tmp_path)
    wrong = _mismatches(cases, got)
    assert not wrong, "\n".join(wrong)


# ─── 3. end-to-end through the hook ─────────────────────────────────────


def _run_hook(impl: str, hooks_dir: Path, proj: Path, url: str, env: dict):
    argv = ([_BASH, str(hooks_dir / "pre-tool-use.sh")] if impl == "sh" else
            [shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(hooks_dir / "pre-tool-use.ps1")])
    payload = json.dumps({"tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "x"},
                          "session_id": "r18"})
    env = dict(env, CLAUDE_PROJECT_DIR=str(proj))
    return subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=120,
                          env=env, cwd=str(proj))


def _proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("env_name", sorted(TABLE["envs"]))
def test_every_case_through_the_real_hook(impl: str, env_name: str, tmp_path: Path) -> None:
    """The whole table through pre-tool-use.{sh,ps1}: exit 2 exactly for the
    `block` cases (allow and pass both let the call through)."""
    cases = _cases(env_name)
    env = _env(tmp_path, TABLE["envs"][env_name])
    proj = _proj(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: _run_hook(impl, HOOKS, proj, c["url"], env), cases))
    wrong = []
    for c, res in zip(cases, results):
        want = 2 if c["expect"] == "block" else 0
        if res.returncode != want:
            wrong.append(f"{c['url']!r}: want exit {want}, got {res.returncode} ({c['why']}) {res.stderr[-300:]}")
    assert not wrong, "\n".join(wrong)


_MOVED = TABLE["envs"]["moved"]


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("url,code", [
    ("http://10.0.0.1:22\\@localhost:18081/", 2),   # R18-04
    ("http://LOCALHOST:6379/", 2),                   # R18-05 (was allowed by .sh)
    ("http://2130706433/", 2),                       # R18-05
    ("http://[::ffff:7f00:1]/", 2),                  # R18-05
    ("http://localhost:18081/v1/meta", 0),           # the moved service
    ("https://example.com/", 0),                     # public
    ("http://bücher.de/", 0),                   # R18F-02: IDNA on every OS
])
def test_hook_end_to_end(impl: str, url: str, code: int, tmp_path: Path) -> None:
    res = _run_hook(impl, HOOKS, _proj(tmp_path), url, _env(tmp_path, _MOVED))
    assert res.returncode == code, (res.returncode, res.stderr)
    if code == 2:
        assert "SSRF guard" in res.stderr
        assert "localhost:18081" in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_hook_fails_closed_when_the_lib_is_missing(impl: str, tmp_path: Path) -> None:
    """A partial install without the lib cannot judge a URL: every WebFetch is
    blocked, and the message says how to restore it."""
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    for lib in ("ssrf-allowlist.sh", "ssrf-allowlist.ps1"):
        (hooks / "_lib" / lib).unlink()
    res = _run_hook(impl, hooks, _proj(tmp_path), "https://example.com/", _env(tmp_path, {}))
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "ssrf-allowlist" in res.stderr and "bundle update" in res.stderr


def _no_python_path(tmp_path: Path) -> str:
    """A PATH directory holding the hook's shell tools and NO python/python3/py."""
    bindir = tmp_path / "nopy-bin"
    bindir.mkdir(exist_ok=True)
    for tool in _SH_TOOLS:
        src = shutil.which(tool)
        assert src, tool
        link = bindir / tool
        if not link.exists():
            link.symlink_to(src)
    return str(bindir)


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("url", [
    "https://example.com/", "http://localhost:8081/", "http://bücher.de/",
    "http://ｌｏｃａｌｈｏｓｔ/",
])
def test_no_python_blocks_every_webfetch(impl: str, url: str, tmp_path: Path) -> None:
    """R18F-08: without an interpreter the guard cannot judge anything, so
    EVERY WebFetch is blocked with the fix named — including on the .sh path,
    which used to exit before the guard and let everything through. (The
    install root has no venv here, so the resolver finds nothing either.)"""
    env = _env(tmp_path, {"PATH": _no_python_path(tmp_path)}, python=None)
    res = _run_hook(impl, HOOKS, _proj(tmp_path), url, env)
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "SSRF guard" in res.stderr and "Python" in res.stderr


def test_no_python_leaves_other_tools_alone(tmp_path: Path) -> None:
    """The no-Python branch fails closed for WebFetch ONLY."""
    env = _env(tmp_path, {"PATH": _no_python_path(tmp_path)}, python=None)
    env["CLAUDE_PROJECT_DIR"] = str(_proj(tmp_path))
    res = subprocess.run([_BASH, str(HOOKS / "pre-tool-use.sh")],
                         input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}),
                         capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path))
    assert res.returncode == 0, res.stderr


def _fake_python(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "fake-python"
    fake.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(fake)


@pytest.mark.skipif(os.name == "nt", reason="the fake interpreter is a POSIX shell script")
@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("body,expect_in_msg", [
    ("cat >/dev/null; exit 0", "gave no answer"),                        # empty verdict
    ("cat >/dev/null; echo ALLOW; exit 0", "unrecognised answer 'ALLOW'"),  # not an exact word
    ("cat >/dev/null; echo 'ModuleNotFoundError: No module named vco_lib.ssrf_url' >&2; exit 1",
     "ModuleNotFoundError"),                                             # broken install
])
def test_an_unusable_answer_blocks(impl: str, body: str, expect_in_msg: str, tmp_path: Path) -> None:
    """Hardening: only the exact words `allow` / `pass` let a WebFetch through.
    An empty answer, an unknown word or a crashed module blocks, and the
    message names the fix."""
    env = _env(tmp_path, {}, python=_fake_python(tmp_path, body))
    res = _run_hook(impl, HOOKS, _proj(tmp_path), "https://example.com/", env)
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert expect_in_msg in res.stderr, res.stderr
    assert "install.py --update" in res.stderr, res.stderr
