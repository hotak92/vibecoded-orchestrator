# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R5FIX (review R5-8) — no shipped .ps1 writes a UTF-8 BOM.

Windows PowerShell 5.1 (what the Windows settings template launches with
``powershell -File``) writes a BOM for ``-Encoding utf8`` on Set-Content /
Add-Content / Out-File, and for ``[Text.Encoding]::UTF8`` handed to a .NET
file writer. A Python reader opening the file as ``utf-8`` then sees U+FEFF
before the first line, and ``json.loads`` of that line fails. pwsh 7 writes
no BOM, so a runtime byte check on a pwsh-7 box cannot see the defect: the
lint below asserts on the CODE SHAPE, and the helper test proves the one
BOM-free writer (``_lib/utf8-nobom.ps1``) on the interpreter at hand.

A line may keep such a write only with an explicit ``BOM-OK: <reason>``
comment on the same line.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "templates" / "hooks" / "_lib" / "utf8-nobom.ps1"

#: `-Encoding utf8` / `UTF8` / `utf-8` / `utf8BOM` / `utf8NoBOM` (the last is
#: pwsh-7-only and fails outright on 5.1 — banned for the same reason).
_CMDLET_UTF8 = re.compile(r"-Encoding\s+['\"]?utf-?8(?:bom|nobom)?\b", re.I)
#: .NET writers handed the BOM-emitting UTF8 singleton.
_DOTNET_UTF8 = re.compile(
    r"(?:WriteAllText|AppendAllText|WriteAllLines|AppendAllLines|StreamWriter)\b"
    r".*\[(?:System\.)?Text\.Encoding\]::UTF8\b", re.I)
_JUSTIFIED = re.compile(r"#.*\bBOM-OK:\s*\S")


def bom_offenders(root: Path) -> list[str]:
    """``path:line`` of every shipped .ps1 code line that can write a BOM."""
    hits: list[str] = []
    for base in (root / "templates" / "hooks", root / "templates" / "scripts"):
        for ps1 in sorted(base.rglob("*.ps1")):
            text = ps1.read_bytes().decode("utf-8-sig", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if not (_CMDLET_UTF8.search(line) or _DOTNET_UTF8.search(line)):
                    continue
                if _JUSTIFIED.search(line):
                    continue
                hits.append(f"{ps1.relative_to(root)}:{n}")
    return hits


def test_no_shipped_ps1_writes_a_bom() -> None:
    offenders = bom_offenders(REPO_ROOT)
    assert not offenders, (
        "BOM-writing .ps1 lines (Windows PowerShell 5.1 prepends U+FEFF; a Python "
        "json.loads reader then loses the first row). Use Set-VcoUtf8Content / "
        "Add-VcoUtf8Content from templates/hooks/_lib/utf8-nobom.ps1, or justify the "
        f"line with `# BOM-OK: <reason>`: {offenders}")


@pytest.mark.parametrize("line,flagged", [
    ('Set-Content -Path $f -Value $x -Encoding UTF8', True),
    ('Add-Content -LiteralPath $f -Value $x -Encoding utf8 -ErrorAction Stop', True),
    ("Out-File -FilePath $f -Encoding 'utf-8'", True),
    ('Set-Content $f $x -Encoding utf8NoBOM', True),
    ('[IO.File]::WriteAllText($f, $x, [Text.Encoding]::UTF8)', True),
    ('Set-Content -Path $f -Value $x -Encoding utf8  # BOM-OK: read only by Get-Content', False),
    ('Set-Content -Path $f -Value $x -Encoding ascii', False),
    ('$b = [System.Text.Encoding]::UTF8.GetBytes($s)', False),  # bytes, no preamble
    ('# Set-Content -Encoding utf8: writes a BOM on 5.1', False),
])
def test_lint_shape(tmp_path: Path, line: str, flagged: bool) -> None:
    d = tmp_path / "templates" / "hooks"
    d.mkdir(parents=True)
    (tmp_path / "templates" / "scripts").mkdir()
    (d / "probe.ps1").write_text(line + "\n", encoding="utf-8")
    assert bool(bom_offenders(tmp_path)) is flagged


# ── the helper itself (runs on the pwsh at hand) ──────────────────────────

PWSH = shutil.which("pwsh")


def _pwsh(script: str, cwd: Path) -> subprocess.CompletedProcess:
    if not PWSH:
        pytest.skip("pwsh required")
    return subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120, env=child_env())


def test_helper_writes_no_bom_and_keeps_cmdlet_semantics(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    lib = str(HELPER).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
. '{lib}'
Set-Location -LiteralPath '{str(out).replace("'", "''")}'
Set-VcoUtf8Content -Path 'set.json' -Value ('{{"k":"v' + [char]0x00e9 + '"}}')
Set-VcoUtf8Content -Path 'lines.txt' -Value @('a', 'b')
Set-VcoUtf8Content -Path 'nonl.txt' -Value @('a', 'b') -NoNewline
Add-VcoUtf8Content -Path 'new.jsonl' -Value '{{"n":1}}'
Add-VcoUtf8Content -Path 'new.jsonl' -Value '{{"n":2}}'
Set-VcoUtf8Content -Path 'empty.txt' -Value $null
"""
    r = _pwsh(script, tmp_path)
    assert r.returncode == 0, r.stderr
    eol = os.linesep.encode()  # [Environment]::NewLine, as Set-Content writes
    set_bytes = (out / "set.json").read_bytes()
    assert not set_bytes.startswith(b"\xef\xbb\xbf")
    assert json.loads(set_bytes.decode("utf-8")) == {"k": "vé"}
    assert (out / "lines.txt").read_bytes() == b"a" + eol + b"b" + eol
    assert (out / "nonl.txt").read_bytes() == b"ab"
    rows = (out / "new.jsonl").read_bytes()
    assert not rows.startswith(b"\xef\xbb\xbf"), "the CREATING append writes no BOM"
    assert [json.loads(x) for x in rows.decode("utf-8").splitlines()] == [{"n": 1}, {"n": 2}]
    assert (out / "empty.txt").read_bytes() == b""


def test_helper_throws_so_callers_choose(tmp_path: Path) -> None:
    """A failed write throws (callers that had SilentlyContinue wrap it)."""
    lib = str(HELPER).replace("'", "''")
    missing = str(tmp_path / "no" / "such" / "dir" / "f.txt").replace("'", "''")
    r = _pwsh(f". '{lib}'; try {{ Set-VcoUtf8Content -Path '{missing}' -Value x; 'wrote' }} "
              f"catch {{ 'threw' }}", tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "threw"


def test_helper_is_ascii() -> None:
    HELPER.read_bytes().decode("ascii")
