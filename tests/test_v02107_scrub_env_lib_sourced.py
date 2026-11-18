# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 -- every hook scrubs secrets through ``_lib/scrub-env.{sh,ps1}``.

``_lib/scrub-env.sh`` called itself the single source of truth for the
secret env vars a hook clears, yet no ``.sh`` hook sourced it: all 51 carried
their own inline ``unset`` line, and the PowerShell side loaded its lib in 4
hooks of 51. Now every hook loads the lib and calls its function; the inline
list stays only as the fallback for a missing lib (a broken install), and
``tests/test_scrub_env_parity_v0273.py`` keeps that fallback equal to the
canonical list.

The behaviour legs run the scrub lines CUT FROM EACH REAL HOOK beside a
``_lib`` whose list carries one extra name: the extra name is cleared only
when the lib is the live source, and with the lib absent the inline fallback
still clears the canonical names.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
LIB_SH = HOOKS / "_lib" / "scrub-env.sh"
LIB_PS1 = HOOKS / "_lib" / "scrub-env.ps1"
SH_LOAD = '{ . "${BASH_SOURCE[0]%/*}/_lib/scrub-env.sh" && vct_scrub_secret_env; } 2>/dev/null ||'
EXTRA = "VCO_TEST_EXTRA_SECRET"
_PWSH = shutil.which("pwsh")


def _sh_hooks():
    return sorted(HOOKS.glob("*.sh"))


def _ps1_hooks():
    return sorted(HOOKS.glob("*.ps1"))


def _sh_block(hook: Path) -> str:
    lines = hook.read_text(encoding="utf-8").splitlines()
    i = lines.index(SH_LOAD)
    assert lines[i + 1].startswith("unset ") and "GITHUB_TOKEN" in lines[i + 1]
    return lines[i] + "\n" + lines[i + 1] + "\n"


def _ps1_block(hook: Path) -> str:
    """From the first line that loads scrub-env.ps1 to the end of its
    fallback (the `}` closing the if/else)."""
    lines = hook.read_text(encoding="utf-8-sig").splitlines()
    starts = [i for i, ln in enumerate(lines) if "_lib/scrub-env.ps1" in ln
              and not ln.lstrip().startswith("#")]
    assert starts, f"{hook.name}: does not load _lib/scrub-env.ps1"
    i = starts[0]
    # A wrapper that resolves the path first (`$ScrubLib = Join-Path ...`)
    # needs its $LibDir line too.
    j = i
    while j > 0 and re.match(r"^\$(LibDir|PSScriptRootLocal)\s*=", lines[j - 1]):
        j -= 1
    depth, out = 0, []
    for ln in lines[j:]:
        out.append(ln)
        if ln.lstrip().startswith("#"):
            continue
        depth += ln.count("{") - ln.count("}")
        if depth == 0 and len(out) > 1 and "{" in "".join(out) and ln.strip().endswith("}"):
            break
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("hook", _sh_hooks(), ids=lambda p: p.name)
def test_every_sh_hook_sources_the_lib_with_the_inline_fallback(hook):
    _sh_block(hook)


@pytest.mark.parametrize("hook", _ps1_hooks(), ids=lambda p: p.name)
def test_every_ps1_hook_loads_the_lib_with_the_inline_fallback(hook):
    block = _ps1_block(hook)
    assert "Invoke-VctScrubSecretEnv" in block, block
    assert "GITHUB_TOKEN" in block, f"{hook.name}: inline fallback missing"


def _lib_with_extra(tmp: Path, src: Path, sh: bool) -> None:
    lib = tmp / "_lib"
    lib.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8-sig")
    if sh:
        text = text.replace('VCT_SCRUB_SECRET_KEYS="', f'VCT_SCRUB_SECRET_KEYS="{EXTRA} ', 1)
    else:
        text = text.replace("$VctScrubSecretKeys = @(", f"$VctScrubSecretKeys = @('{EXTRA}', ", 1)
    assert EXTRA in text
    (lib / src.name).write_text(text, encoding="utf-8")


def _env():
    env = dict(os.environ)
    env.update({EXTRA: "x1", "GITHUB_TOKEN": "x2", "VERCEL_TOKEN": "x3"})
    return env


_PRINT_SH = 'printf "%s|%s|%s\\n" "${VCO_TEST_EXTRA_SECRET-unset}" "${GITHUB_TOKEN-unset}" "${VERCEL_TOKEN-unset}"\n'


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="bash")
@pytest.mark.parametrize("hook", _sh_hooks(), ids=lambda p: p.name)
@pytest.mark.parametrize("lib_present", [True, False], ids=["lib", "no-lib"])
def test_sh_scrub_uses_the_lib_and_falls_back(hook, lib_present, tmp_path):
    if lib_present:
        _lib_with_extra(tmp_path, LIB_SH, sh=True)
    script = tmp_path / "probe.sh"
    script.write_text("#!/usr/bin/env bash\n" + _sh_block(hook) + _PRINT_SH, encoding="utf-8")
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                         env=_env(), timeout=30).stdout.strip()
    extra, gh, vercel = out.split("|")
    assert (gh, vercel) == ("unset", "unset"), out
    assert extra == ("unset" if lib_present else "x1"), out


_PRINT_PS1 = ('[Console]::Out.WriteLine(@($env:VCO_TEST_EXTRA_SECRET, $env:GITHUB_TOKEN, '
              '$env:VERCEL_TOKEN | ForEach-Object { if ($null -eq $_) { "unset" } else { $_ } }) -join "|")\n')


@pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("hook", _ps1_hooks(), ids=lambda p: p.name)
@pytest.mark.parametrize("lib_present", [True, False], ids=["lib", "no-lib"])
def test_ps1_scrub_uses_the_lib_and_falls_back(hook, lib_present, tmp_path):
    if lib_present:
        _lib_with_extra(tmp_path, LIB_PS1, sh=False)
    script = tmp_path / "probe.ps1"
    script.write_text(_ps1_block(hook) + _PRINT_PS1, encoding="utf-8")
    res = subprocess.run([_PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(script)],
                         capture_output=True, text=True, env=_env(), timeout=60,
                         stdin=subprocess.DEVNULL)
    extra, gh, vercel = res.stdout.strip().split("|")
    assert (gh, vercel) == ("unset", "unset"), (res.stdout, res.stderr)
    assert extra == ("unset" if lib_present else "x1"), (res.stdout, res.stderr)
