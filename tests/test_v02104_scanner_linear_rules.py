"""v0.2.104 R7 N2: every scanner rule is linear, with an unchanged match set.

Finding (R7 friction review): rules written ``A.*B`` restart at every ``A`` and
run to the end of the one-line scan surface, so a command repeating one verb
40 000 times took 27 s (``curl x``) to 84 s (``pip install x``) — past the
hook's 2 s scanner bound, i.e. no verdict. ``env_exfil_curl_data`` and
``env_exfil_multihop`` were cubic (2 000 ``curl -d`` took 50 s).

The fix rewrites each rule (last-start tempering or a first-occurrence chain,
see the comment above ``_span`` in bash_security.py). This file pins:

* EQUIVALENCE — each rewritten rule against its frozen pre-fix pattern
  (``OLD`` below) on seeded random strings built from that rule's own words,
  separators and noise. The scan surfaces are whitespace-collapsed one-liners,
  so the domain is newline-free text (tabs and runs of spaces are still
  generated). ``VCO_SCANNER_DIFF_N`` raises the per-rule sample size.
* TIMING — each rewritten rule on 40 000 repeats of its adversarial shapes,
  and ``check_command`` end to end, under 1.5 s.
"""
from __future__ import annotations

import os
import random
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "templates" / "scripts" / "bash_security.py"
sys.path.insert(0, str(SCANNER.parent))
import bash_security as bs  # noqa: E402  # pyright: ignore[reportMissingImports]  # resolved by the sys.path.insert above

#: The pre-fix patterns (v0.2.104 base 825fa164), verbatim.
OLD: dict[str, str] = {
    'dd_device': '\\bdd\\b.*\\bof=/dev/',
    'curl_pipe_shell': '(curl|wget)\\s[^|]*\\|\\s*(ba)?sh\\b',
    'base64_pipe_shell': 'base64\\s+-d.*\\|\\s*(ba)?sh\\b',
    'env_exfil_curl': '(?:^|[|&;(])\\s*(env|printenv|set)\\b[^;&|\\n]*\\|\\s*(curl|wget|nc|ncat)\\b',
    'env_exfil_curl_data': '(curl|wget).*(-d|--data).*(?:\\$\\(|`)\\s*(env|printenv|cat\\s+/proc)',
    'env_exfil_multihop': '(?:^|[|&;(])\\s*(env|printenv|set)\\b[^;&\\n]*\\|[^;&\\n]*\\b(curl|wget|nc|ncat)\\b',
    'read_ssh_keys': 'cat\\s+~?/?(\\.ssh/(id_|authorized_keys|known_hosts|config)|\\bssh\\b.*private)',
    'env_grep_secrets': '(env|printenv|set)\\s*\\|.*grep.*(KEY|TOKEN|SECRET|PASS|CRED)',
    'read_env_files': 'cat\\s+(?![^;&|\\n]*>)[^;&|\\n]*\\.(env|credentials|netrc|pgpass)\\b',
    'chmod_world_writable': 'chmod\\s+(-[a-zA-Z]+\\s+)*777\\b',
    'chown_root': 'chown\\s+(-[a-zA-Z]+\\s+)*root\\b',
    'pip_install_url': 'pip\\s+install\\s+.*https?://(?!.*pypi\\.org)',
    'pip_install_git': 'pip\\s+install\\s+.*git\\+',
    'npm_install_url': 'npm\\s+install\\s+.*https?://',
    'read_bash_history': 'cat\\s+.*\\.(bash_history|zsh_history|history)',
    'ln_s_etc': 'ln\\s+-s.*/(etc/passwd|etc/shadow|proc/)',
    'reverse_shell': '(bash\\s+-i\\s+>&|/dev/tcp/|nc\\s+-[a-zA-Z]*e\\s|ncat\\s+-[a-zA-Z]*e\\s|python.*socket.*connect)',
    'ps_download_exec': '\\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring|downloadfile|net\\.webclient|start-bitstransfer)\\b.*\\b(?:iex|invoke-expression)\\b|\\b(?:iex|invoke-expression)\\b.*\\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring|downloadfile|net\\.webclient|start-bitstransfer)\\b',
    'ps_base64_exec': 'frombase64string.*\\b(?:iex|invoke-expression)\\b|\\b(?:iex|invoke-expression)\\b.*frombase64string',
    'ps_encoded_command': '\\b(?:powershell|pwsh)(?:\\.exe)?\\b[^;|]*\\s-(?:e|ec|en|enc|enco|encodedcommand)\\s+[\'\\"]?[A-Za-z0-9+/=]{16,}',
    'ps_env_secret_read': '\\$\\{?env:\\w*(?:KEY|TOKEN|SECRET|PASS|CRED)\\w*\\b(?!\\s*=(?!=))|getenvironmentvariable\\(\\s*[\'\\"]\\w*(?:KEY|TOKEN|SECRET|PASS|CRED)\\w*\\b|\\b(?:get-content|gc|cat|type|get-item|gi|get-childitem|gci|ls|dir)\\s+[^;|]*\\benv:[\\\\/]?[\\w*]*(?:KEY|TOKEN|SECRET|PASS|CRED)',
    'ps_env_grep_secrets': '\\b(?:get-content|gc|cat|type|get-item|gi|get-childitem|gci|ls|dir)\\s+[^;|]*\\benv:.*\\|.*(?:KEY|TOKEN|SECRET|PASS|CRED)',
    'ps_env_exfil': '\\benv:.*\\|.*\\b(?:(?:iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring|downloadfile|net\\.webclient|start-bitstransfer)|curl|wget)\\b',
    'ps_read_ssh_keys': '\\b(?:get-content|gc|cat|type)\\s+[^;|]*\\.ssh[\\\\/](?:id_|authorized_keys|known_hosts|config)',
    'ps_read_env_files': '\\b(?:get-content|gc|cat|type)\\s+[^;|>]*\\.(?:env|credentials|netrc|pgpass)\\b',
    'ps_remove_root': '\\b(?:remove-item|ri|rm|del|erase|rd|rmdir)\\s+[^;|]*?(?:(?<![\\w.\\\\/])[a-z]:[\\\\/]?\\*?(?=[\\s\'\\"]|$)|[a-z]:[\\\\/](?:windows|users|program files(?: \\(x86\\))?)[\\\\/]?\\*?(?=[\\s\'\\"]|$)|\\$env:(?:userprofile|systemroot|windir|homedrive)[\\\\/]?\\*?(?=[\\s\'\\"]|$)|\\$home[\\\\/]?\\*?(?=[\\s\'\\"]|$))',
}

_SH = ["sh", "bash", "| sh", "|bash", "shx", "ba"]
_PS_FETCH = ["iwr", "irm", "invoke-webrequest", "invoke-restmethod", "downloadstring",
             "downloadfile", "net.webclient", "start-bitstransfer"]
_PS_READ = ["get-content", "gc", "cat", "type", "get-item", "gi", "get-childitem", "gci", "ls", "dir"]

#: Per rule: the words its old pattern is made of, plus near-misses.
FRAGMENTS: dict[str, list[str]] = {
    "dd_device": ["dd", "xdd", "ddx", "of=/dev/", "of=/dev/sda", "of=", "/dev/", "if=x"],
    "curl_pipe_shell": ["curl", "wget", "curl ", "wget\t", "url", *_SH],
    "base64_pipe_shell": ["base64", "base64 -d", "base64  -d", "-d", *_SH],
    "env_exfil_curl": ["env", "printenv", "set", "reset", "curl", "wget", "nc", "ncat",
                       "(env", "( set", "| curl", "|nc", "env |"],
    "env_exfil_curl_data": ["curl", "wget", "-d", "--data", "--d", "$(", "`", "env",
                            "printenv", "cat /proc", "cat  /proc", "cat", "curl -d",
                            "$(env", "`printenv", "$( cat /proc"],
    "env_exfil_multihop": ["env", "printenv", "set", "reset", "curl", "wget", "nc", "ncat",
                           "(env", "| printenv", "| curl", "|nc", "env |", "| base64 |", "&&"],
    "read_ssh_keys": ["cat", "cat ", "~", "/", ".ssh/", "id_", "authorized_keys",
                      "known_hosts", "config", "ssh", "private", "xssh", "cat ~/.ssh/id_",
                      "cat ssh", "cat /ssh"],
    "env_grep_secrets": ["env", "printenv", "set", "env |", "env|", "grep", "KEY", "token",
                         "SECRET", "PASS", "CRED"],
    "read_env_files": ["cat", "cat ", ".env", ".credentials", ".netrc", ".pgpass", ".envx",
                       ">", ">>", "x.env.local"],
    "chmod_world_writable": ["chmod", "chmod ", "-R", "-chmod", "-xchmod", "-chmodx", "777",
                             "7777", "-f", "-"],
    "chown_root": ["chown", "chown ", "-R", "-chown", "-xchown", "root", "rootx", "-"],
    "pip_install_url": ["pip", "install", "pip install", "https://", "http://", "pypi.org",
                        "pypi", ".org", "pypixorg"],
    "pip_install_git": ["pip", "install", "pip install", "git+", "git", "+"],
    "npm_install_url": ["npm", "install", "npm install", "https://", "http://", "http:/"],
    "read_bash_history": ["cat", "cat ", ".bash_history", ".zsh_history", ".history",
                          "history", "."],
    "ln_s_etc": ["ln", "-s", "ln -s", "/etc/passwd", "/etc/shadow", "/proc/", "etc/passwd"],
    "reverse_shell": ["bash -i >&", "bash", "-i", ">&", "/dev/tcp/", "nc", "ncat", "-e",
                      "-xe", "python", "socket", "connect", "python3"],
    "ps_download_exec": [*_PS_FETCH, "iex", "invoke-expression", "xiex", "-", "."],
    "ps_base64_exec": ["frombase64string", "iex", "invoke-expression", "xiex", "base64"],
    "ps_encoded_command": ["powershell", "pwsh", ".exe", "powershell.exe", "-e", "-enc",
                           "-encodedcommand", "-ec", "AAAAAAAAAAAAAAAA", "QUJD", "AAAAAAAA", "+/=",
                           "powershell -enc AAAAAAAAAAAAAAAA", " -e ", "pwsh.exe", " -enc '"],
    "ps_env_secret_read": ["$env:", "${env:", "API_KEY", "TOKEN", "=", "==",
                           "getenvironmentvariable(", *_PS_READ, "env:", "env:\\", "env:/",
                           "*", "GH_TOKEN", "gc env:KEY", "$env:API_KEY", "getenvironmentvariable('X_TOKEN'"],
    "ps_env_grep_secrets": [*_PS_READ, "env:", "|", "KEY", "token", "select-string",
                            "gc env:", "dir env:", "| KEY"],
    "ps_env_exfil": ["env:", "$env:", "|", "iwr", "irm", "curl", "wget", "net.webclient", "xcurl"],
    "ps_read_ssh_keys": ["get-content", "gc", "cat", "type", ".ssh/", ".ssh\\", "id_",
                         "authorized_keys", "known_hosts", "config", "gc .ssh/id_", "cat ~/.ssh/config"],
    "ps_read_env_files": ["get-content", "gc", "cat", "type", ".env", ".netrc",
                          ".credentials", ".pgpass", ">"],
    "ps_remove_root": ["remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "c:", "c:\\",
                       "c:/", "c:\\windows", "d:/users", "c:\\program files (x86)",
                       "$env:userprofile", "$home", "*", "x.c:", "-recurse"],
}
_NOISE = [" ", "  ", "\t", ";", "|", "&", "(", ")", ">", "'", '"', "-", "/", "x", "a.b",
          "$", "`", "=", ":", "\\", "~", "7", "A"]
_SEPS = ["", "", " ", " ", "  ", "\t", "; ", " | ", "&&", "|"]

NEW = {name: pat for name, pat, _ in bs._RULES + bs._PS_RULES}


def test_old_table_covers_exactly_the_rewritten_rules() -> None:
    """Every rule listed in OLD was rewritten; every other rule is untouched
    (its pattern is not in this file because it never was quadratic)."""
    assert set(OLD) == set(FRAGMENTS)
    for name, old in OLD.items():
        assert name in NEW, name
        assert NEW[name].pattern != old, f"{name} not rewritten"
        assert NEW[name].flags & re.IGNORECASE


def _sample(rng: random.Random, frags: list[str]) -> str:
    parts = []
    for _ in range(rng.randint(1, 14)):
        tok = rng.choice(frags) if rng.random() < 0.6 else rng.choice(_NOISE)
        if rng.random() < 0.1:
            tok = tok.upper()
        parts.append(tok)
        parts.append(rng.choice(_SEPS))
    return "".join(parts)


@pytest.mark.parametrize("name", sorted(OLD))
def test_rewritten_rule_matches_exactly_what_the_old_one_did(name: str) -> None:
    n = int(os.environ.get("VCO_SCANNER_DIFF_N", "8000"))
    rng = random.Random(f"r7-n2-{name}")
    old = re.compile(OLD[name], re.IGNORECASE)
    new = NEW[name]
    hits = 0
    for _ in range(n):
        s = _sample(rng, FRAGMENTS[name])
        want = bool(old.search(s))
        assert bool(new.search(s)) == want, (name, s)
        # the collapsed form is what check_command actually scans
        c = " ".join(s.split())
        assert bool(new.search(c)) == bool(old.search(c)), (name, c)
        hits += want
    # not vacuous: both outcomes are exercised
    assert 0.02 * n < hits < 0.98 * n, (name, hits)


#: Per rule: shapes that made the OLD pattern super-linear when repeated.
ADVERSARIAL: dict[str, list[str]] = {
    "dd_device": ["dd a"],
    "curl_pipe_shell": ["curl x", "wget x"],
    "base64_pipe_shell": ["base64 -d f"],
    "env_exfil_curl": ["(set x", "(env"],
    "env_exfil_curl_data": ["curl -d", "wget -d", "curl x"],
    "env_exfil_multihop": ["| printenv", "(env |"],
    "read_ssh_keys": ["cat ssh"],
    "env_grep_secrets": ["printenv |", "env | grep"],
    "read_env_files": ["cat x", "cat a.env >"],
    "chmod_world_writable": ["-chmod"],
    "chown_root": ["-chown"],
    "pip_install_url": ["pip install x", "pip install http://pypi.org"],
    "pip_install_git": ["pip install x"],
    "npm_install_url": ["npm install x"],
    "read_bash_history": ["cat x"],
    "ln_s_etc": ["ln -s a b"],
    "reverse_shell": ["python socket", "python x"],
    "ps_download_exec": ["invoke-expression x", "iwr x"],
    "ps_base64_exec": ["frombase64string x", "iex x"],
    "ps_encoded_command": ["powershell -enc"],
    "ps_env_secret_read": ["get-childitem x"],
    "ps_env_grep_secrets": ["get-childitem x", "gc env: |"],
    "ps_env_exfil": ["env: |"],
    "ps_read_ssh_keys": ["get-content x"],
    "ps_read_env_files": ["get-content x"],
    "ps_remove_root": ["remove-item x"],
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_rewritten_rule_is_fast_on_40k_repeats(name: str) -> None:
    """RED pre-fix: e.g. `curl x` x40k took 27 s, `pip install x` 84 s."""
    assert set(ADVERSARIAL) == set(OLD)
    pat = NEW[name]
    for tok in ADVERSARIAL[name]:
        for sep in ("; ", " "):
            s = sep.join([tok] * 40000)
            t0 = time.monotonic()
            pat.search(s)
            assert time.monotonic() - t0 < 1.5, (name, tok, sep)


@pytest.mark.parametrize("tok", [
    "curl x", "wget x", "pip install x", "npm install x", "dd a", "base64 -d f",
    "ln -s a b", "cat x", "curl -d", "| printenv", "(set x", "-chmod", "python socket",
])
def test_check_command_is_fast_on_40k_repeats(tok: str) -> None:
    cmd = "; ".join([tok] * 40000)
    t0 = time.monotonic()
    bs.check_command(cmd)
    assert time.monotonic() - t0 < 1.5, tok


@pytest.mark.parametrize("tok", ["invoke-expression x", "get-childitem x", "env: |", "remove-item x"])
def test_check_powershell_is_fast_on_40k_repeats(tok: str) -> None:
    cmd = "; ".join([tok] * 40000)
    t0 = time.monotonic()
    bs.check_powershell(cmd)
    assert time.monotonic() - t0 < 1.5, tok


# --- the ps1 hook's raw net (.NET backtracks like Python `re`) ---------------

PS1_HOOK = REPO / "templates" / "hooks" / "pre-tool-use.ps1"
#: Pre-fix raw-net patterns of pre-tool-use.ps1 (download piped to a shell,
#: base64 piped to a shell), verbatim.
PS1_RAW_OLD = {
    "fetch": "(?i)(curl|wget)\\s[^|]+\\|\\s*(ba)?sh\\b",
    "base64": "(?i)base64\\s+-d.*\\|\\s*(ba)?sh\\b",
}
PS1_RAW_FRAGMENTS = {
    "fetch": ["curl", "wget", "curl ", "wget\t", "curl\n", "x", *_SH],
    "base64": ["base64", "base64 -d", "base64\n-d", "-d", *_SH],
}


def _ps1_raw_patterns() -> dict[str, str]:
    text = PS1_HOOK.read_text(encoding="utf-8")
    body = text[text.index("function Get-PtuRawInjection"):]
    found = re.findall(r"\$Text -match '([^']+)'", body)[:3]
    return {"fetch": found[0], "base64": found[2]}


@pytest.mark.parametrize("which", sorted(PS1_RAW_OLD))
def test_ps1_raw_net_matches_exactly_what_it_did(which: str) -> None:
    """The raw net reads the RAW command, so newlines are in the domain here."""
    new_src = _ps1_raw_patterns()[which]
    assert new_src != PS1_RAW_OLD[which]
    old, new = re.compile(PS1_RAW_OLD[which]), re.compile(new_src)
    rng = random.Random(f"r7-n2-ps1-{which}")
    n = int(os.environ.get("VCO_SCANNER_DIFF_N", "8000"))
    hits = 0
    for _ in range(n):
        s = _sample(rng, PS1_RAW_FRAGMENTS[which]).replace("&&", rng.choice(["\n", "&&"]))
        want = bool(old.search(s))
        assert bool(new.search(s)) == want, (which, s)
        hits += want
    assert 0.02 * n < hits < 0.98 * n, (which, hits)


_PWSH = shutil.which("pwsh")
_PWSH_DIFF = r"""
param([string]$Json, [string]$Old, [string]$New)
$arr = Get-Content -Raw -Encoding utf8 $Json | ConvertFrom-Json
$div = 0; $hits = 0
foreach ($s in $arr) {
    $o = [bool]($s -match $Old); $n = [bool]($s -match $New)
    if ($o) { $hits++ }
    if ($o -ne $n) { $div++ }
}
Write-Output ("{0} {1}" -f $hits, $div)
"""


@pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("which", sorted(PS1_RAW_OLD))
def test_ps1_raw_net_matches_exactly_under_dotnet(which: str, tmp_path: Path) -> None:
    """The same old-vs-new equivalence, evaluated by the engine the hook really
    runs under (.NET via pwsh), on the newline/CRLF-bearing raw domain."""
    rng = random.Random(f"r8-pwsh-{which}")
    samples = [
        _sample(rng, PS1_RAW_FRAGMENTS[which]).replace("&&", rng.choice(["\n", "\r\n", "&&"]))
        for _ in range(int(os.environ.get("VCO_SCANNER_PWSH_N", "3000")))
    ]
    data = tmp_path / "samples.json"
    data.write_text(json.dumps(samples), encoding="utf-8")
    script = tmp_path / "diff.ps1"
    script.write_text(_PWSH_DIFF, encoding="utf-8")
    out = subprocess.run(
        [str(_PWSH), "-NoProfile", "-NonInteractive", "-File", str(script),
         "-Json", str(data), "-Old", PS1_RAW_OLD[which], "-New", _ps1_raw_patterns()[which]],
        capture_output=True, text=True, timeout=300, check=True, env=child_env(),
    ).stdout.split()
    hits, div = int(out[-2]), int(out[-1])
    assert div == 0, (which, div)
    assert 0.02 * len(samples) < hits < 0.98 * len(samples), (which, hits)


@pytest.mark.parametrize("which", sorted(PS1_RAW_OLD))
def test_ps1_raw_net_is_fast_on_40k_repeats(which: str) -> None:
    new = re.compile(_ps1_raw_patterns()[which])
    for tok in ("curl x", "base64 -d f", "curl curl"):
        s = "; ".join([tok] * 40000)
        t0 = time.monotonic()
        new.search(s)
        assert time.monotonic() - t0 < 1.5, (which, tok)
