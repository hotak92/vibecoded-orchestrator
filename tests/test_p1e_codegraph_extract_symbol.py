# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P1e (v0.2.75): codegraph symbol-extractor rejects garbage queries.

``templates/hooks/_lib/codegraph-query.sh::codegraph_extract_symbol`` (and
its ``.ps1`` sibling ``Get-VcoCodegraphSymbol``) used to:
  * match env-assignments (``LEAN_CTX_OFF=1`` via the snake_case rule),
  * match non-code paths (``/tmp/x.log`` via the dotted rule),
  * match grep regex/glob fragments,
  * and — worst — fall back to the WHOLE COMMAND TEXT when nothing matched,
    issuing garbage codegraph queries for e.g. ``git diff <sha>..HEAD``.

The fix rejects those word shapes and returns EMPTY when no discrete
symbol is isolable; the callers (pre-tool-use / pre-bash) then skip
injection entirely.

These are fixture-table tests: each row drives the real bash function and
asserts the extracted symbol. The .ps1 sibling is pwsh-gated.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "codegraph-query.sh"
LIB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "codegraph-query.ps1"

# (command_text, expected_symbol). Empty string = no injection.
CASES = [
    # env-prefixed command → the real file symbol, NOT the env assignment.
    ("LEAN_CTX_OFF=1 python3 install.py --update", "install.py"),
    # grep with a regex pattern → the file, not the pattern.
    ("grep -nE 'def .*foo' server.py", "server.py"),
    # log tail → no code symbol → no injection.
    ("tail -f /var/log/app.log", ""),
    ("cat /tmp/output.log", ""),
    # git diff sha..HEAD → no symbol (the sha-range is not code).
    ("git diff abc123..HEAD", ""),
    # env-assignment only → no symbol.
    ("LEAN_CTX_OFF=1", ""),
    # a real call-shape survives (grep for a function call; the quoted
    # `symbol(` word keeps `(` — deliberately NOT a rejected metachar).
    ("grep 'migrate_collections(' server.py", "migrate_collections("),
    # snake_case identifier survives.
    ("grep resolve_test_penalty code_ranking.py", "resolve_test_penalty"),
    # CamelCase identifier survives.
    ("rg OrderManager", "OrderManager"),
    # source path with dir survives (real source file).
    ("cat vco_lib/embedding_service.py", "vco_lib/embedding_service.py"),
    # non-code path with dir → skip.
    ("cat config/settings.yaml", ""),
    # a URL → skip.
    ("curl https://example.com/foo", ""),
    # redirect token → skip; but a real symbol later wins.
    ("run_thing 2> errors_out", "run_thing"),
]


def _extract_sh(text: str) -> str:
    """Source the lib and call codegraph_extract_symbol on `text`."""
    script = (
        f'. "{LIB_SH}"\n'
        'codegraph_extract_symbol "$1"\n'
    )
    res = subprocess.run(
        ["bash", "-c", script, "bash", text],
        capture_output=True, text=True, timeout=15,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bash lib")
@pytest.mark.parametrize("text,expected", CASES)
def test_extract_symbol_sh(text, expected):
    assert _extract_sh(text) == expected, (
        f"cmd={text!r} → expected {expected!r}"
    )


def _extract_ps1(text: str) -> str:
    # Pass the command text via env var (not argv) so pwsh's own parser
    # never sees redirect/metachar tokens like `2>` in the fixture strings.
    import os
    script = (
        f". '{LIB_PS1}'\n"
        "$t = [Environment]::GetEnvironmentVariable('P1E_TEXT')\n"
        "$out = Get-VcoCodegraphSymbol -Text $t\n"
        "[Console]::Out.Write($out)\n"
    )
    env = dict(os.environ)
    env["P1E_TEXT"] = text
    res = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.parametrize("text,expected", CASES)
def test_extract_symbol_ps1_parity(text, expected):
    assert _extract_ps1(text) == expected, (
        f"ps1 cmd={text!r} → expected {expected!r}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
