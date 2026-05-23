# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test: every CLI switch declared in install.ps1's param() block
must be propagated to install.py via $installArgs (when applicable).

Background
----------
2026-05-23 incident: `first-install.bat --yes` on Windows did NOT trigger
non-interactive mode in install.py because install.ps1 had no `$Yes` param
and never appended `--yes` to its `$installArgs` list. The user typed
`--yes`, the .bat forwarded `--yes` to PowerShell, PowerShell silently
dropped it into `$args` (PS only auto-binds the `-Foo` form, not `--foo`),
and install.py ran with `args.yes = False` — hanging at the next prompt
in a non-interactive shell.

This test scans install.ps1 and ensures:
1. `$Yes` switch is declared in `param()`.
2. The .bat-style double-dash arg reconciliation block lifts `--yes`
   (and friends) into the bound switch variables.
3. `if ($Yes -or $NonInteractive)` propagates `--yes` to `$installArgs`.
4. `install.py` recognizes `VCT_NON_INTERACTIVE` env var as fallback
   (because the .ps1 documents it on line ~97 as the env-var path for
   non-interactive mode).
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = REPO_ROOT / "install.ps1"
PY = REPO_ROOT / "install.py"


def _read(path: pathlib.Path) -> str:
    # install.ps1 has UTF-8 BOM (per fix/ps1-utf8-bom-windows-ps5 v0.2.27);
    # utf-8-sig strips it transparently.
    return path.read_text(encoding="utf-8-sig")


def test_ps1_declares_yes_switch() -> None:
    """install.ps1's param() block must declare [switch]$Yes."""
    src = _read(PS1)
    assert re.search(r"\[switch\]\$Yes\b", src), (
        "install.ps1 param() block is missing [switch]$Yes. "
        "Without it, PS5.1 cannot bind `-Yes` from CLI and the .bat "
        "wrapper cannot lift `--yes` from $args into a bound variable. "
        "Result: install.py runs without --yes and hangs at prompts."
    )


def test_ps1_lifts_double_dash_yes_from_args() -> None:
    """The .bat-style reconciliation must catch `--yes` from $args."""
    src = _read(PS1)
    # Look for the foreach($a in $args) block + a case that sets $Yes for --yes
    assert re.search(r"foreach\s*\(\s*\$a\s+in\s+\$args\s*\)", src), (
        "install.ps1 is missing the foreach($a in $args) reconciliation loop "
        "that lifts --yes (Python-style) into $Yes (PS-style). Without this, "
        "first-install.bat --yes is silently ignored."
    )
    # Find a clause that maps --yes to $Yes
    yes_clause = re.search(r"'\^--yes\$.*?\{\s*\$Yes\s*=\s*\$true\s*\}", src, re.DOTALL)
    assert yes_clause, (
        "Reconciliation loop in install.ps1 is missing the `--yes` -> $Yes "
        "mapping. Add a switch case like: '^--yes$' { $Yes = $true }"
    )


def test_ps1_propagates_yes_to_installargs() -> None:
    """install.ps1 must append --yes to $installArgs when $Yes is true."""
    src = _read(PS1)
    # Match the propagation line: if ($Yes -or $NonInteractive) { $installArgs += "--yes" }
    pattern = re.search(
        r"if\s*\(\s*\$Yes\b.*?\)\s*\{\s*\$installArgs\s*\+=\s*[\"']--yes[\"']\s*\}",
        src,
    )
    assert pattern, (
        "install.ps1 must append '--yes' to $installArgs when $Yes is set, "
        "otherwise install.py runs without --yes even when the user explicitly "
        "passed --yes to first-install.bat. Expected line: "
        "`if ($Yes -or $NonInteractive) { $installArgs += \"--yes\" }`"
    )


def test_py_honors_vct_non_interactive_env() -> None:
    """install.py must lift VCT_NON_INTERACTIVE env into args.yes.

    install.ps1 line ~97 documents this env var as the non-interactive
    fallback. install.py must honor it for the contract to be intact.
    """
    src = _read(PY)
    # Look for: if not args.yes and os.environ.get("VCT_NON_INTERACTIVE")
    pattern = re.search(
        r"if\s+not\s+args\.yes\s+and\s+os\.environ\.get\(\s*[\"']VCT_NON_INTERACTIVE[\"']",
        src,
    )
    assert pattern, (
        "install.py must check os.environ.get('VCT_NON_INTERACTIVE') after "
        "parser.parse_args() and set args.yes = True if present. "
        "install.ps1 line ~97 promises this env var triggers non-interactive "
        "mode; install.py must honor the contract."
    )


def test_py_docker_timeout_configurable() -> None:
    """install.py must allow overriding the docker-compose-up timeout
    via VCT_INSTALL_DOCKER_TIMEOUT env var.

    2026-05-23: the 15-min hardcoded cap fired on residential DSL during
    cold-cache pulls of weaviate (~250MB) + ollama, even though the daemon
    was healthy and pulling in the background. An env-var escape hatch
    avoids forcing users to patch install.py to install on slow links.
    """
    src = _read(PY)
    pattern = re.search(
        r"VCT_INSTALL_DOCKER_TIMEOUT",
        src,
    )
    assert pattern, (
        "install.py must read VCT_INSTALL_DOCKER_TIMEOUT (seconds) and use "
        "it for the `docker compose up -d` subprocess timeout. The default "
        "15 min cap is too low for cold-cache pulls on slow residential links."
    )
