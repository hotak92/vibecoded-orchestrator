# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 reviews R18-04 / R18-05: the SSRF guard's URL decision.

One case table (``tests/fixtures/ssrf_cases.json``) runs against BOTH shell
implementations — ``templates/hooks/_lib/ssrf-allowlist.sh`` and its ``.ps1``
mirror — so the two OSes cannot disagree on a URL again (R18-05 found
``LOCALHOST`` blocked on Windows and allowed on POSIX). The table covers the
backslash authority confusion (R18-04), upper-case / percent-encoded /
numeric / IPv6 host spellings (R18-05) and the legitimate moved-port allows.

A few cases also go end-to-end through ``pre-tool-use.{sh,ps1}`` (exit code +
stderr), plus the lib-missing fail-closed branch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "templates" / "hooks"
LIB_SH = HOOKS / "_lib" / "ssrf-allowlist.sh"
LIB_PS1 = HOOKS / "_lib" / "ssrf-allowlist.ps1"
TABLE = json.loads((REPO / "tests" / "fixtures" / "ssrf_cases.json").read_text(encoding="utf-8"))
_ENV_KEYS = ("WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "CODE_EMBED_SERVICE_URL",
             "VCT_HUB_PORT", "VCT_DISABLE_HOOKS", "PY")
#: Tools the .sh lib shells out to; the no-Python PATH links exactly these.
_SH_TOOLS = ("tr", "od", "grep", "dirname")
HAVE_PWSH = shutil.which("pwsh") is not None


def _no_python_path(tmp_path: Path) -> str:
    """A PATH directory holding the lib's shell tools and NO python/python3/py."""
    bindir = tmp_path / "nopy-bin"
    bindir.mkdir(exist_ok=True)
    for tool in _SH_TOOLS:
        src = shutil.which(tool)
        assert src, tool
        link = bindir / tool
        if not link.exists():
            link.symlink_to(src)
    return str(bindir)


def _env(tmp_path: Path, extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env.update({"HOME": str(tmp_path / "home"), "VCT_STATE_DIR": str(tmp_path / "state")})
    extra = dict(extra)
    if extra.pop("__NO_PYTHON__", None):
        env["PATH"] = _no_python_path(tmp_path)
    env.update(extra)
    return env


def _verdicts_sh(urls: list, env: dict, root: Path) -> list:
    script = ('. "$1"; root="$2"; shift 2\n'
              'for u in "$@"; do vco_ssrf_verdict "$u" "$root"; done\n')
    res = subprocess.run([shutil.which("bash") or "bash", "-c", script, "_", str(LIB_SH), str(root), *urls],
                         capture_output=True, text=True, timeout=120, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


def _verdicts_ps1(urls: list, env: dict, root: Path, tmp_path: Path) -> list:
    data = tmp_path / "urls.json"
    data.write_text(json.dumps(urls, ensure_ascii=False), encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        "param([string]$Lib, [string]$Root, [string]$Data)\n"
        ". $Lib\n"
        "$urls = [System.IO.File]::ReadAllText($Data, [System.Text.Encoding]::UTF8) | ConvertFrom-Json\n"
        "foreach ($u in $urls) { [Console]::Out.WriteLine((Get-VcoSsrfVerdict -Url $u -ProjectRoot $Root)) }\n",
        encoding="utf-8")
    res = subprocess.run([shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(driver), "-Lib", str(LIB_PS1),
                          "-Root", str(root), "-Data", str(data)],
                         capture_output=True, text=True, timeout=300, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


_IMPLS = [pytest.param("sh", id="sh")]
if HAVE_PWSH:
    _IMPLS.append(pytest.param("ps1", id="ps1"))


@pytest.mark.parametrize("impl", _IMPLS)
@pytest.mark.parametrize("env_name", sorted(TABLE["envs"]))
def test_every_case_in_the_shared_table(impl: str, env_name: str, tmp_path: Path) -> None:
    cases = [c for c in TABLE["cases"] if c["env"] == env_name]
    assert cases
    root = tmp_path / "proj"  # no resolver in it: the hub port is the 7700 default
    root.mkdir()
    env = _env(tmp_path, TABLE["envs"][env_name])
    urls = [c["url"] for c in cases]
    got = (_verdicts_sh(urls, env, root) if impl == "sh"
           else _verdicts_ps1(urls, env, root, tmp_path))
    assert len(got) == len(cases), got
    wrong = [f"{c['url']!r}: want {c['expect']}, got {g} ({c['why']})"
             for c, g in zip(cases, got) if g != c["expect"]]
    assert not wrong, "\n".join(wrong)


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
    # Owner ruling on non-ASCII hosts: converted to IDNA, blocked only
    # without Python or when the conversion fails.
    by = {(c["env"], c["url"]): c["expect"] for c in TABLE["cases"]}
    assert by[("moved", "http://b\u00fccher.de/")] == "pass"
    assert by[("moved", "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/")] == "block"
    assert by[("moved", "http://\uff11\uff10\uff0e\uff10\uff0e\uff10\uff0e\uff11/")] == "block"
    assert by[("nopython", "http://b\u00fccher.de/")] == "block"


# ─── end-to-end through the hook ────────────────────────────────────────

_HOOKS = [pytest.param("sh", id="sh")]
if HAVE_PWSH:
    _HOOKS.append(pytest.param("ps1", id="ps1"))


def _run_hook(impl: str, hooks_dir: Path, tmp_path: Path, url: str, extra: dict):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    argv = (["bash", str(hooks_dir / "pre-tool-use.sh")] if impl == "sh" else
            ["pwsh", "-NoProfile", "-File", str(hooks_dir / "pre-tool-use.ps1")])
    payload = json.dumps({"tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "x"},
                          "session_id": "r18"})
    env = _env(tmp_path, extra)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    return subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=120,
                          env=env, cwd=str(proj))


_MOVED = TABLE["envs"]["moved"]


@pytest.mark.parametrize("impl", _HOOKS)
@pytest.mark.parametrize("url,code", [
    ("http://10.0.0.1:22\\@localhost:18081/", 2),   # R18-04
    ("http://LOCALHOST:6379/", 2),                   # R18-05 (was allowed by .sh)
    ("http://2130706433/", 2),                       # R18-05
    ("http://[::ffff:7f00:1]/", 2),                  # R18-05
    ("http://localhost:18081/v1/meta", 0),           # the moved service
    ("https://example.com/", 0),                     # public
])
def test_hook_end_to_end(impl: str, url: str, code: int, tmp_path: Path) -> None:
    res = _run_hook(impl, HOOKS, tmp_path, url, _MOVED)
    assert res.returncode == code, (res.returncode, res.stderr)
    if code == 2:
        assert "SSRF guard" in res.stderr
        assert "localhost:18081" in res.stderr


@pytest.mark.parametrize("impl", _HOOKS)
def test_hook_fails_closed_when_the_lib_is_missing(impl: str, tmp_path: Path) -> None:
    """A partial install without the lib cannot judge a URL: every WebFetch is
    blocked, and the message says how to restore it."""
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    for lib in ("ssrf-allowlist.sh", "ssrf-allowlist.ps1"):
        (hooks / "_lib" / lib).unlink()
    res = _run_hook(impl, hooks, tmp_path, "https://example.com/", {})
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "ssrf-allowlist" in res.stderr and "bundle update" in res.stderr
