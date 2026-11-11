# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — `.ps1` hooks run their embedded Python bodies as temp FILES.

THE DEFECT (Windows 11 field report against shipped v0.2.105): Claude Code
runs ``.ps1`` hooks with Windows PowerShell 5.1 (``powershell -NoProfile
-File``). 5.1's native-command argument passing does NOT escape embedded
double quotes, so ``& $py -c $body`` truncated every multi-line body at its
first ``"`` — ``~/.vct/metrics/post-tool-use-async.log`` recorded
``kg-update-nudge exit=1 ... File "<string>", line 1 "KG-update ...
SyntaxError: unterminated string literal`` on EVERY tool call, and the
KG-update nudge (plus the deferral surface, retrieval-health line, stop
failure ledger, work counter, ...) never fired on Windows. pwsh 7.3+ passes
arguments correctly by default, which is why the Windows CI hook-parity
legs stayed green.

THE FIX: ONE shared helper, ``templates/hooks/_lib/run-python-program.ps1``
(``Invoke-VcoPythonProgram``), writes the body to a private BOM-free UTF-8
temp file and runs ``& $python <tmp> [args]`` — the command line leaves the
program-delivery path entirely, on 5.1 and pwsh 7 alike. Every migrated
site keeps its exact calling contract (args, stdin payload, stdout capture,
``2>$null``, ``$LASTEXITCODE``).

RED PROOF MECHANISM: Linux pwsh reproduces the 5.1 quoting loss when
``$PSNativeCommandArgumentPassing = 'Legacy'`` is set (verified: the REAL
nudge body via ``-c`` under Legacy produces the field report's exact
``SyntaxError: unterminated string literal``; under the pwsh-7 default it
runs clean). Every driven leg below runs with Legacy set, so it exercises
the 5.1-visible behaviour on this Linux host. For a red proof against the
pre-fix tree, export ``VCO_TEST_PS1_TEMPLATES=<dir>`` pointing at a
``templates/`` tree copied from the pre-fix commit: the driven legs then
fail with the SyntaxError / missing state row, and the inventory leg fails
on the surviving ``-c`` call sites.

WHAT IS COVERED WHERE (each migrated site's calling shape):
  * bare, stdout-to-hook, exit propagated .... kg-update-nudge.ps1 (driven
    end-to-end: emits the nudge at the threshold, writes its state row)
  * -Arguments + stdout capture .............. post-tool-use-async.ps1
    (driven: the dispatcher's parse step routes the exact derived set)
  * -Stdin payload + -Arguments .............. stop-failure-notify.ps1,
    user-prompt-submit-reminder.ps1 (unit-driven with the real bodies)
  * -Stdin payload, stdout captured .......... kg-summary-generator.ps1
  * every shipped body, per file ............. the body leg below runs
    EACH migrated hook's real Python body through the real helper under
    Legacy mode and asserts no SyntaxError reaches stderr
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.pwsh import PWSH_BASE_FLAGS  # noqa: E402
from tests.test_v02101_async_posttooluse_dispatcher import (  # noqa: E402
    STUB_PS1,
    _expected_subhooks,
    _parse_rows,
    _payload,
)

#: The templates tree under test — the repo's own by default; a pre-fix
#: copy for red proofs (see the module docstring).
TEMPLATES = Path(
    os.environ.get("VCO_TEST_PS1_TEMPLATES", str(REPO_ROOT / "templates"))
)
HOOKS = TEMPLATES / "hooks"

#: Every hook migrated to _lib/run-python-program.ps1, with the variables
#: holding its embedded Python bodies (extraction anchors for the body leg).
MIGRATED_BODIES: dict[str, list[str]] = {
    "kg-update-nudge.ps1": ["pythonScript"],
    "kg-summary-generator.ps1": ["code"],
    "post-tool-use-async.ps1": ["parseSrc"],
    "session-start-kg-loader.ps1": ["pyCode"],
    "session-start-deferral-surface.ps1": ["pyCode"],
    "stop-failure-notify.ps1": ["StopFailureCore"],
    "session-start-retrieval-health.ps1": ["pyCode", "kgFailCode"],
    "user-prompt-submit-reminder.ps1": ["scanSrc"],
}

#: post-bash-context-record.ps1 passes its body INLINE to -Body (no
#: variable), so it is extracted from the invocation itself.
INLINE_BODY_FILES = ("post-bash-context-record.ps1",)

MIGRATED_FILES = sorted([*MIGRATED_BODIES, *INLINE_BODY_FILES])

#: Any ``python -c <variable-or-here-string>`` left in a MIGRATED file.
DASH_C_RE = re.compile(r"-\s*c\s+(\$\w+|@')")


def _have_pwsh() -> bool:
    return shutil.which("pwsh") is not None


def _read_ps1(name: str) -> str:
    path = HOOKS / name
    return path.read_text(encoding="utf-8-sig", errors="strict")


#: The driver: sets the native-argument-passing mode (5.1 == 'Legacy'),
#: then invokes the hook with the caller's stdin/stdout/stderr.
DRIVER_PS1 = """param([string]$Hook, [string]$Mode)
if ($Mode -ne "Default") { $PSNativeCommandArgumentPassing = $Mode }
& $Hook
exit $LASTEXITCODE
"""


def _run_hook_legacy(hook: Path, payload: str, env: dict, tmp_path: Path,
                     mode: str = "Legacy") -> subprocess.CompletedProcess:
    driver = tmp_path / "vco-driver.ps1"
    driver.write_text(DRIVER_PS1, encoding="ascii")
    argv = [
        shutil.which("pwsh"), *PWSH_BASE_FLAGS, "-NonInteractive",
        "-File", str(driver), str(hook), mode,
    ]
    return subprocess.run(
        argv, input=payload, capture_output=True, text=True, env=env,
        timeout=120, cwd=str(tmp_path),
    )


# ── the reproduction mechanism (keeps the Legacy legs honest) ─────────────

LEGACY_MINUS_C_PS1 = """param([string]$BodyFile, [string]$Python, [string]$Mode)
$PSNativeCommandArgumentPassing = $Mode
$body = [System.IO.File]::ReadAllText($BodyFile)
& $Python -c $body
exit $LASTEXITCODE
"""


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
def test_legacy_mode_reproduces_the_5_1_quoting_break_on_dash_c(tmp_path):
    """Negative control: `& $py -c $body` under Legacy truncates the REAL
    nudge body at its first double quote — the field report's exact
    SyntaxError — while the same body runs clean under the pwsh-7 default.
    If this ever stops holding, the Legacy legs below no longer reproduce
    Windows PowerShell 5.1 and must be re-based, not deleted."""
    text = _read_ps1("kg-update-nudge.ps1")
    m = re.search(r"\$pythonScript = @'\n(.*?)\n'@", text, re.S)
    assert m, "kg-update-nudge.ps1: $pythonScript here-string not found"
    body_file = tmp_path / "nudge-body.py"
    body_file.write_text(m.group(1), encoding="utf-8")
    driver = tmp_path / "legacy-minus-c.ps1"
    driver.write_text(LEGACY_MINUS_C_PS1, encoding="ascii")

    def run(mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shutil.which("pwsh"), *PWSH_BASE_FLAGS, "-NonInteractive",
             "-File", str(driver), str(body_file), sys.executable, mode],
            capture_output=True, text=True, timeout=60,
        )

    broken = run("Legacy")
    assert broken.returncode != 0, (
        "Legacy mode no longer reproduces the 5.1 `-c` quoting loss; the "
        "driven legs in this module are no longer a valid 5.1 model"
    )
    assert "SyntaxError" in broken.stderr and "unterminated string literal" in (
        broken.stderr
    ), f"unexpected Legacy failure shape: {broken.stderr[:400]!r}"
    clean = run("Standard")
    assert clean.returncode == 0, (
        f"the nudge body itself is broken under Standard: {clean.stderr[:400]!r}"
    )


# ── every migrated site actually uses the helper (inventory) ──────────────

def test_migrated_hooks_invoke_the_one_helper_and_keep_no_dash_c():
    for name in MIGRATED_FILES:
        # Comment lines are SKIPPED: the v0.2.106 migration comments quote
        # the retired `& $PY -c $body` shape on purpose, and a scanner a
        # comment could trip would train the next editor to delete the
        # documentation (same comment-skip discipline as the async
        # dispatcher's AppendAllText pin).
        code_lines = [
            ln for ln in _read_ps1(name).splitlines()
            if not ln.lstrip().startswith("#")
        ]
        offenders = DASH_C_RE.findall("\n".join(code_lines))
        assert not offenders, (
            f"{name} still invokes python -c with a variable/here-string "
            f"body ({offenders}): Windows PowerShell 5.1 truncates it at "
            "the first embedded double quote. Route it through "
            "_lib/run-python-program.ps1 (Invoke-VcoPythonProgram)."
        )
        assert "Invoke-VcoPythonProgram" in "\n".join(code_lines), (
            f"{name} no longer runs its Python body through "
            "_lib/run-python-program.ps1"
        )


def test_the_helper_is_declared_ps1_only_in_both_gates():
    """The helper has no .sh sibling BY DESIGN (bash heredocs carry no
    command-line quoting), and both parity gates must agree about that."""
    parity = (
        REPO_ROOT / ".github" / "scripts" / "check_hook_parity.py"
    ).read_text(encoding="utf-8")
    ratchet = (
        REPO_ROOT / "tests" / "test_v0292_sibling_parity_blind_spots.py"
    ).read_text(encoding="utf-8")
    for gate_text, gate in ((parity, "check_hook_parity"), (ratchet, "ratchet")):
        assert '"run-python-program.ps1"' in gate_text, (
            f"{gate} lost the run-python-program.ps1 PS1_ONLY_LIB entry"
        )
    assert not (HOOKS / "_lib" / "run-python-program.sh").exists()


# ── each real shipped body runs intact through the helper under Legacy ────

HELPER_BODY_PS1 = """param([string]$LibDir, [string]$Python, [string]$BodyFile,
      [string]$StdinFile, [string]$Arg1, [string]$Mode)
$PSNativeCommandArgumentPassing = $Mode
. (Join-Path $LibDir "run-python-program.ps1")
$body = [System.IO.File]::ReadAllText($BodyFile)
$stdinText = [System.IO.File]::ReadAllText($StdinFile)
Invoke-VcoPythonProgram -Python $Python -Body $body -Stdin $stdinText `
    -Arguments @($Arg1, "<dir>", "proj")
exit $LASTEXITCODE
"""


def _extract_body(name: str, var: str) -> str:
    m = re.search(rf"\${var} = @'\n(.*?)\n'@", _read_ps1(name), re.S)
    # A miss returns "" (NOT a collection-time assert): a pre-fix tree must
    # fail the TEST with the migration message, not error at collection and
    # mask the driven legs' reds.
    return m.group(1) if m else ""


def _extract_inline_body(name: str) -> str:
    m = re.search(r"Invoke-VcoPythonProgram[^\n]*\n?-Body @'\n(.*?)\n'@",
                  _read_ps1(name), re.S)
    return m.group(1) if m else ""


def _bodies() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, vars_ in MIGRATED_BODIES.items():
        for var in vars_:
            out.append((f"{name}::${var}", _extract_body(name, var)))
    for name in INLINE_BODY_FILES:
        out.append((f"{name}::inline", _extract_inline_body(name)))
    return out


BODIES = _bodies()


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
@pytest.mark.parametrize("label", [lbl for lbl, _ in BODIES], ids=[
    lbl.replace("::", "--") for lbl, _ in BODIES
])
def test_every_migrated_body_runs_intact_through_the_helper_under_legacy(
        tmp_path, label):
    """Each shipped body runs through the REAL helper under Legacy mode:
    whatever the body does with its (absent) inputs, it must not die the
    5.1 quoting death (SyntaxError / unterminated string literal)."""
    body_text = dict(BODIES)[label]
    assert body_text, (
        f"{label}: the embedded Python body was not found — the hook no "
        "longer carries it in the expected here-string shape"
    )
    body_file = tmp_path / "body.py"
    body_file.write_text(body_text, encoding="utf-8")
    stdin_file = tmp_path / "stdin.json"
    stdin_file.write_text("{}\n", encoding="utf-8")
    driver = tmp_path / "helper-body.ps1"
    driver.write_text(HELPER_BODY_PS1, encoding="ascii")
    env = os.environ.copy()
    env.update({
        "VCO_DEFERRAL_PROJECT_DIR": str(tmp_path),
        "VCO_PTA_VCO_ROOT": str(REPO_ROOT),
        "KG_FAIL_JSONL": str(tmp_path / "absent.jsonl"),
        "KG_FAIL_ROOT": str(tmp_path),
        "KG_FAIL_MARKER": str(tmp_path / "absent.marker"),
    })
    proc = subprocess.run(
        [shutil.which("pwsh"), *PWSH_BASE_FLAGS, "-NonInteractive",
         "-File", str(driver), str(HOOKS / "_lib"), sys.executable,
         str(body_file), str(stdin_file), str(tmp_path / "absent.json"),
         "Legacy"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert "SyntaxError" not in combined and (
        "unterminated string literal" not in combined
    ), (
        f"{label}: the body died the 5.1 quoting death under Legacy mode "
        f"(rc={proc.returncode}): {combined[:400]!r}"
    )


# ── driven leg 1: the field-reported hook, end to end ─────────────────────

@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
def test_kg_update_nudge_emits_and_records_state_under_legacy_mode(tmp_path):
    """The exact field-report hook, in Legacy (== 5.1) mode, with a real
    transcript above the threshold: exit 0, the nudge text on stdout, no
    SyntaxError on stderr, and the state row proves the Python ran."""
    home = tmp_path / "home"
    tmpdir = tmp_path / "tmpdir"
    home.mkdir()
    tmpdir.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"assistant","requestId":"r1","message":{"id":"m1",'
        '"role":"assistant","usage":{"output_tokens":1000}}}\n',
        encoding="utf-8",
    )

    for mode in ("Legacy", "Default"):
        # A fresh home per mode: the nudge is once-per-baseline, so a second
        # run against the same state would legitimately stay silent.
        home = tmp_path / f"home-{mode}"
        tmpdir = tmp_path / f"tmpdir-{mode}"
        home.mkdir()
        tmpdir.mkdir()
        payload = json.dumps({
            "session_id": f"legacy-quoting-leg-{mode}",
            "prompt": "hello",
            "transcript_path": str(transcript),
        })
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "VCT_STATE_DIR": str(home / ".vct"),
            "VCT_CLAUDE_DIR": str(home / ".claude"),
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
            "CLAUDE_PROJECT_DIR": str(tmp_path),
            "KG_NUDGE_FIRST": "500",
            "KG_NUDGE_INTERVAL": "1000",
        })
        for key in ("VCT_VENV", "VCT_DISABLE_HOOKS", "KG_NUDGE_OFF",
                    "VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT"):
            env.pop(key, None)
        proc = _run_hook_legacy(HOOKS / "kg-update-nudge.ps1", payload, env,
                                tmp_path, mode=mode)
        assert proc.returncode == 0, (
            f"{mode}: rc={proc.returncode} stderr={proc.stderr[:400]!r}"
        )
        assert "SyntaxError" not in proc.stderr, proc.stderr[:400]
        assert "KG-update nudge" in proc.stdout, (
            f"{mode}: the nudge did not fire: stdout={proc.stdout[:200]!r} "
            f"stderr={proc.stderr[:200]!r}"
        )
        state = home / ".vct" / "metrics" / "kg_update_tokens.jsonl"
        assert state.is_file(), f"{mode}: the embedded Python never ran"
        rows = [
            json.loads(ln)
            for ln in state.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert any(
            r.get("session_id") == f"legacy-quoting-leg-{mode}" for r in rows
        )


# ── driven leg 2: the dispatcher's parse step (args + capture shape) ──────

@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
def test_dispatcher_parse_step_routes_the_derived_set_under_legacy(tmp_path):
    """post-tool-use-async.ps1's parse step (the -Arguments + captured-
    stdout shape) under Legacy mode: a Bash payload routes EXACTLY the
    derived set. Pre-fix, the SyntaxError emptied $fields and every payload
    degraded to the `*` row."""
    dispatcher = HOOKS / "post-tool-use-async.ps1"
    if not dispatcher.is_file():
        pytest.fail(f"dispatcher missing: {dispatcher}")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(dispatcher, hooks / dispatcher.name)
    shutil.copytree(HOOKS / "_lib", hooks / "_lib")
    text = dispatcher.read_text(encoding="utf-8-sig")
    m = re.search(r"\$RouteTable = '\r?\n(.*?)\r?\n'", text, re.S)
    assert m, "$RouteTable declaration not found"
    rows = _parse_rows(m.group(1))
    for _tool, stem, _gate in rows:
        (hooks / f"{stem}.ps1").write_text(
            STUB_PS1.format(stem=stem), encoding="ascii"
        )
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    tmpdir = tmp_path / "tmpdir"
    for d in (home, proj, tmpdir):
        d.mkdir()
    marker = tmp_path / "marker.txt"
    marker.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "VCT_STATE_DIR": str(home / ".vct"),
        "VCT_CLAUDE_DIR": str(home / ".claude"),
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCO_TEST_MARKER": str(marker),
        "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
    })
    for key in ("VCT_DISABLE_HOOKS", "VCO_ASYNC_DISABLED_HOOKS"):
        env.pop(key, None)

    payload = json.dumps(_payload("Bash", command="ls -la"))
    proc = _run_hook_legacy(hooks / dispatcher.name, payload, env, tmp_path)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "" and proc.stderr.strip() == "", (
        f"the dispatcher leaked to the harness: {proc.stdout!r} {proc.stderr!r}"
    )
    invoked = {p.name[len("marker.txt."):] for p in
               tmp_path.glob("marker.txt.*")}
    expected = _expected_subhooks(rows, "Bash", "ls -la")
    assert invoked == expected, (
        f"Legacy mode routing: invoked {sorted(invoked)}, the routing table "
        f"derives {sorted(expected)} (a SyntaxError in the parse step "
        "degrades every payload to the `*` row)"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
