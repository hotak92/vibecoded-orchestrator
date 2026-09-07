# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W7 — `_lib/metrics-dir.sh` == `_lib/metrics-dir.ps1` == `vco_lib.paths`.

The metrics path rule now exists in three languages: Python (the SSOT,
`vco_lib/paths.py`), bash and PowerShell. That is a class-C cross-language
mirror, which CLAUDE.md permits ONLY with an enforcing parity test — the
shell hooks cannot import `vco_lib` before they have resolved where to write,
and spawning an interpreter on every Stop event to ask is not affordable.

This is that test. It EXECUTES all three across the same matrix and compares
answers, so changing one flavour without the others reds here rather than in
a field report six weeks later. Twelve hook files were touched by W7; a
divergent `.ps1` sibling is the single most likely way this package could rot.

Matrix (each run against a fresh tmp tree):

  1. fresh install       — no archive at all
  2. archive with rows   — copy owed, so writers stay on the archive
  3. archive + sentinel  — copy verified, writers on the new home
  4. archive, no rows    — nothing to copy, writers on the new home

Tri-OS (R12/R14): the SHELL half runs natively here on Linux; the PowerShell
half runs wherever a `pwsh`/`powershell` binary exists (this is the same
runtime Windows and macOS use, so executing it on any host exercises the
Windows code path's logic); and the per-OS home resolution is asserted as a
SHAPE for all three. A missing PowerShell SKIPS the dynamic half and leaves
the static half — which reads both files and pins their rule text — always on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "templates" / "hooks" / "_lib"
_SH = _LIB / "metrics-dir.sh"
_PS1 = _LIB / "metrics-dir.ps1"

_PS = shutil.which("pwsh") or shutil.which("powershell")
needs_ps = pytest.mark.skipif(
    _PS is None,
    reason="no pwsh/powershell on PATH; the static half still guards drift",
)


# --------------------------------------------------------------------------- #
# scenario builder — one tree shape, three readers
# --------------------------------------------------------------------------- #


def _tree(tmp_path: Path, name: str, *, rows: bool, sentinel: bool,
          archive_dir: bool) -> "tuple[Path, Path]":
    root = tmp_path / name
    claude = root / "claude_home"
    state = root / "vct_root"
    if archive_dir:
        (claude / "metrics").mkdir(parents=True)
        if rows:
            (claude / "metrics" / "costs.jsonl").write_text(
                '{"n":1}\n', encoding="utf-8"
            )
    if sentinel:
        (state / "metrics").mkdir(parents=True)
        (state / "metrics" / ".migrated-from-claude.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
    return claude, state


SCENARIOS = {
    # name:            (archive_dir, rows, sentinel, expect_write_target)
    "fresh":           (False, False, False, "new"),
    "copy_owed":       (True,  True,  False, "archive"),
    "copy_verified":   (True,  True,  True,  "new"),
    "archive_empty":   (True,  False, False, "new"),
}


def _expected(claude: Path, state: Path, target: str) -> Path:
    return (state / "metrics") if target == "new" else (claude / "metrics")


def _ask_python(claude: Path, state: Path) -> dict:
    """The SSOT's answer, computed the way the shell helper's rule describes."""
    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(claude)
    env["VCT_STATE_DIR"] = str(state)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    code = (
        "import json,glob,os;"
        "from vco_lib.paths import vct_metrics_dir, legacy_claude_metrics_dir;"
        "h=vct_metrics_dir(); a=legacy_claude_metrics_dir();"
        "owed = a.is_dir() and not (h/'.migrated-from-claude.json').is_file()"
        " and bool(list(a.glob('*.jsonl')));"
        "print(json.dumps({'home':str(h),'legacy':str(a),"
        "'dir':str(a) if owed else str(h),'migrated':(not owed)}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_SH_PROBE = """
set -u
. "$1"
vco_resolve_metrics_dirs
printf '{"home":"%s","legacy":"%s","dir":"%s","migrated":%s}\\n' \\
    "$VCO_METRICS_HOME" "$VCO_LEGACY_METRICS_DIR" "$VCO_METRICS_DIR" \\
    "$([ "$VCO_METRICS_MIGRATED" = "1" ] && echo true || echo false)"
"""


def _ask_sh(claude: Path, state: Path) -> dict:
    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(claude)
    env["VCT_STATE_DIR"] = str(state)
    proc = subprocess.run(
        ["bash", "-c", _SH_PROBE, "bash", str(_SH)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: `pwsh -Command <string>` does NOT bind trailing argv into `$args`, so the
#: helper path is interpolated into the script rather than passed. (Getting
#: this wrong yields a script that silently dot-sources nothing and returns
#: nulls for everything — which is a vacuous pass, not a failure, if the
#: comparison were loose. It is not: the assertions compare full dicts.)
_PS_PROBE = """
. '{lib}'
Resolve-VcoMetricsDirs
$o = [ordered]@{{
    home     = $script:VcoMetricsHome
    legacy   = $script:VcoLegacyMetricsDir
    dir      = $script:VcoMetricsDir
    migrated = [bool]$script:VcoMetricsMigrated
}}
Write-Output ($o | ConvertTo-Json -Compress)
"""


def _ask_ps(claude: Path, state: Path) -> dict:
    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(claude)
    env["VCT_STATE_DIR"] = str(state)
    assert _PS is not None
    proc = subprocess.run(
        [_PS, "-NoProfile", "-Command", _PS_PROBE.format(lib=str(_PS1))],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["home"], (
        "the PowerShell probe returned nothing — the helper was not sourced"
    )
    return payload


# --------------------------------------------------------------------------- #
# the matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_bash_helper_matches_the_python_ssot(scenario, tmp_path):
    archive_dir, rows, sentinel, target = SCENARIOS[scenario]
    claude, state = _tree(
        tmp_path, scenario, rows=rows, sentinel=sentinel, archive_dir=archive_dir
    )
    py = _ask_python(claude, state)
    sh = _ask_sh(claude, state)

    assert sh == py, f"{scenario}: bash and Python disagree"
    assert Path(sh["dir"]) == _expected(claude, state, target), scenario


@needs_ps
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_powershell_helper_matches_the_python_ssot(scenario, tmp_path):
    archive_dir, rows, sentinel, target = SCENARIOS[scenario]
    claude, state = _tree(
        tmp_path, scenario, rows=rows, sentinel=sentinel, archive_dir=archive_dir
    )
    py = _ask_python(claude, state)
    ps = _ask_ps(claude, state)

    assert ps == py, f"{scenario}: PowerShell and Python disagree"
    assert Path(ps["dir"]) == _expected(claude, state, target), scenario


@needs_ps
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_two_shell_flavours_agree_with_each_other(scenario, tmp_path):
    """The lockstep assertion, stated directly rather than by transitivity.

    Twelve `.sh`/`.ps1` pairs moved in this package; a divergent sibling is
    the failure mode the plan named as most likely.
    """
    archive_dir, rows, sentinel, target = SCENARIOS[scenario]
    claude, state = _tree(
        tmp_path, scenario, rows=rows, sentinel=sentinel, archive_dir=archive_dir
    )
    assert _ask_sh(claude, state) == _ask_ps(claude, state), scenario


def test_the_write_target_gate_is_the_sentinel_not_the_directory(tmp_path):
    """"Writers switch only after a VERIFIED copy" — the sentinel is the record.

    An archive that merely exists is not permission; an archive with rows and
    no sentinel keeps writers where the history is.
    """
    claude, state = _tree(
        tmp_path, "gate", rows=True, sentinel=False, archive_dir=True
    )
    before = _ask_sh(claude, state)
    assert before["migrated"] is False
    assert Path(before["dir"]) == claude / "metrics"

    (state / "metrics").mkdir(parents=True, exist_ok=True)
    (state / "metrics" / ".migrated-from-claude.json").write_text("{}", "utf-8")

    after = _ask_sh(claude, state)
    assert after["migrated"] is True
    assert Path(after["dir"]) == state / "metrics"


# --------------------------------------------------------------------------- #
# read order (the mixed-state answer)
# --------------------------------------------------------------------------- #


_SH_READ_PROBE = """
set -u
. "$1"
vco_resolve_metrics_dirs
printf '%s\\n' "$(vco_metrics_read_file "$2" || printf '(none)')"
"""


def _read_file_sh(claude: Path, state: Path, name: str) -> str:
    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(claude)
    env["VCT_STATE_DIR"] = str(state)
    proc = subprocess.run(
        ["bash", "-c", _SH_READ_PROBE, "bash", str(_SH), name],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_reader_prefers_the_new_home_then_falls_back_to_the_archive(tmp_path):
    claude, state = _tree(
        tmp_path, "read", rows=True, sentinel=False, archive_dir=True
    )
    # Only the archive has it.
    assert _read_file_sh(claude, state, "costs.jsonl") == str(
        claude / "metrics" / "costs.jsonl"
    )
    # Once the new home has it too, the new home wins.
    (state / "metrics").mkdir(parents=True, exist_ok=True)
    (state / "metrics" / "costs.jsonl").write_text('{"n":1}\n', encoding="utf-8")
    assert _read_file_sh(claude, state, "costs.jsonl") == str(
        state / "metrics" / "costs.jsonl"
    )
    # Neither has this one.
    assert _read_file_sh(claude, state, "nope.jsonl") == "(none)"


# --------------------------------------------------------------------------- #
# static half — always on, even with no PowerShell
# --------------------------------------------------------------------------- #


def test_both_flavours_exist_and_name_each_other():
    assert _SH.is_file() and _PS1.is_file()
    sh = _SH.read_text(encoding="utf-8")
    ps = _PS1.read_text(encoding="utf-8")
    assert "metrics-dir.ps1" in sh, ".sh must name its sibling"
    assert "metrics-dir.sh" in ps, ".ps1 must name its sibling"
    for body in (sh, ps):
        assert "vco_lib/paths.py" in body, (
            "each flavour must name the Python SSOT it mirrors"
        )


def test_both_flavours_expose_the_same_four_answers():
    sh = _SH.read_text(encoding="utf-8")
    ps = _PS1.read_text(encoding="utf-8")
    for name in ("VCO_METRICS_DIR", "VCO_METRICS_HOME",
                 "VCO_LEGACY_METRICS_DIR", "VCO_METRICS_MIGRATED"):
        assert name in sh, f".sh must export {name}"
    for name in ("VcoMetricsDir", "VcoMetricsHome",
                 "VcoLegacyMetricsDir", "VcoMetricsMigrated"):
        assert name in ps, f".ps1 must set {name}"


def test_neither_flavour_writes_to_the_archive():
    """The archive is READ-ONLY to the shell side. Asserted structurally.

    Both files may name the archive variable (they must, to read it), but
    neither may pass it to a write. The cheap, honest check: no redirection
    into, and no mkdir of, the legacy variable.
    """
    sh = _SH.read_text(encoding="utf-8")
    ps = _PS1.read_text(encoding="utf-8")
    for bad in (">> \"$VCO_LEGACY_METRICS_DIR", "> \"$VCO_LEGACY_METRICS_DIR",
                "mkdir -p \"$VCO_LEGACY_METRICS_DIR"):
        assert bad not in sh, f".sh must not write the archive ({bad!r})"
    for bad in ("Add-Content -Path $script:VcoLegacyMetricsDir",
                "New-Item -ItemType Directory -Force -Path $script:VcoLegacyMetricsDir",
                "New-Item -ItemType Directory -Path $script:VcoLegacyMetricsDir"):
        assert bad not in ps, f".ps1 must not write the archive ({bad!r})"


@pytest.mark.parametrize(
    "flavour,needle",
    [
        ("sh", "VCT_STATE_DIR"),
        ("sh", "VCT_CLAUDE_DIR"),
        ("ps1", "VCT_STATE_DIR"),
        ("ps1", "VCT_CLAUDE_DIR"),
    ],
)
def test_both_flavours_honour_both_env_overrides(flavour, needle):
    body = (_SH if flavour == "sh" else _PS1).read_text(encoding="utf-8")
    assert needle in body


def test_powershell_home_resolution_is_windows_native():
    """macOS/Linux `pwsh` and Windows PowerShell must agree with Path.home().

    .NET's `UserProfile` folder is `%USERPROFILE%` on Windows and `$HOME` on
    Unix — the same mapping CPython's `Path.home()` makes — so ONE call covers
    all three OSes. The fallbacks exist for stripped environments.
    """
    ps = _PS1.read_text(encoding="utf-8")
    assert "GetFolderPath('UserProfile')" in ps
    assert "$env:USERPROFILE" in ps
    assert "$env:HOME" in ps


def test_bash_home_resolution_covers_the_windows_git_bash_case():
    """`$HOME` first, `%USERPROFILE%` second — Git Bash may expose only one."""
    sh = _SH.read_text(encoding="utf-8")
    assert "${HOME:-}" in sh
    assert "${USERPROFILE:-}" in sh


def test_neither_flavour_hardcodes_a_home_directory():
    """No `/home/`, no `C:\\Users`, no `~` expansion by hand."""
    for path in (_SH, _PS1):
        body = path.read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in body.splitlines() if not ln.strip().startswith("#")
        )
        for bad in ("/home/", "/Users/", "C:\\Users", "$HOME/.vct\"",
                    "expanduser"):
            assert bad not in code, f"{path.name} hardcodes {bad!r}"
