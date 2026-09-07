# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Detached PowerShell spawns actually spawn, and actually outlive the hook.

v0.2.92 R2, continuing MAJOR-1.  Three cmdlet-level facts about
``Start-Process`` / ``Start-Job`` destroy a hook's detached work SILENTLY —
the child is never created (or is reaped), nothing is logged, and the feature
just quietly does not happen:

1. **A ``Start-Job`` child is torn down when the host process exits.** Hooks
   exit in milliseconds; the work they detach takes seconds.  The
   duplicate-scan branch of ``post-file-edit.ps1`` shipped this bug and it was
   fixed earlier in this cycle; ``post-file-edit.ps1``'s *diagram* branch —
   the index → snapshot pair — had the identical defect one branch over, so
   diagram indexing and snapshotting never completed on Windows.
2. **``-WindowStyle`` is rejected by non-Windows PowerShell editions.**  The
   parameter error aborts the whole ``Start-Process``.  Harmless in the field
   (those hooks run on Windows) but it makes every unguarded site UNTESTABLE
   on a Linux/macOS host, which is how defect 1 stayed invisible.
3. **``-RedirectStandardOutput`` and ``-RedirectStandardError`` may not name
   the same file, on ANY edition.**  The POSIX siblings write
   ``>> log 2>&1``, so transcribing that as two identical paths is the
   natural mistake — ``kg-summary-generator.ps1`` and
   ``post-git-commit-kg-sync.ps1`` both shipped it, meaning those spawns never
   started on Windows either.
4. **An array ``-ArgumentList`` is joined with spaces and the child
   RE-SPLITS it.**  Embedded double quotes are consumed and EMPTY elements
   vanish.  ``post-git-commit-kg-sync.ps1`` passed its whole review prompt
   (a here-string carrying ``git diff`` output) on the command line, and the
   Claude CLI rejected the diff's ``---`` token as an option
   (``error: unknown option '---'``, exit 1) on every non-empty commit —
   so the commit-review has never actually run.  The prompt now rides on
   STDIN, which also lifts it clear of the 32 767-char Windows command-line
   limit (the prompt carries the first 300 lines of the unified diff).

All three now have ONE home: ``Start-VcoDetachedProcess`` /
``Start-VcoDetachedPwsh`` in ``templates/hooks/_lib/resolve-powershell.ps1``.

These tests are DRIVEN, not scanned.  A guard asserting that the string
``Start-VcoDetachedPwsh`` appears in the hook source is satisfied by the very
comment explaining the fix — this cycle shipped guards that passed exactly
that way.  So the hook runs through its production entry point (JSON on
stdin, ``CLAUDE_PROJECT_DIR`` at a staged project, ``VCT_VENV`` pointing at a
shim interpreter) and the assertion is the OBSERVABLE consequence: a marker
file written by a child that deliberately sleeps PAST the hook's own exit.
A shim that returned instantly would let the ``Start-Job`` bug pass.

Interpreter-bound: skipped when no PowerShell is on PATH, refusing to skip
under CI (the posture ``tests/test_v0292_post_file_edit_dup_wiring_ps1.py``
established).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
HOOK_PS1 = HOOKS / "post-file-edit.ps1"
COMMIT_HOOK_PS1 = HOOKS / "post-git-commit-kg-sync.ps1"
PS_LIB = HOOKS / "_lib" / "resolve-powershell.ps1"

#: The shim interpreter waits this long before writing.  The real indexer is a
#: Weaviate upsert that always outlives the hook; a shim that answered
#: instantly would not distinguish a detached child from a reaped job.
#:
#: Chosen comfortably ABOVE a cold PowerShell hook run (~1-2 s here) so
#: "the hook returned before the child wrote" is a measurement rather than a
#: race: a hook that BLOCKED on the pair would take at least 2x this.
SHIM_DELAY_S = 5.0

#: Poll budget for the detached child to land its marker.
MARKER_TIMEOUT_S = 45.0


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _require_pwsh() -> str:
    exe = _powershell()
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — these spawn gates cannot run and "
            "must not be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _stage_shim_venv(tmp_path: Path, marker: Path, delay_s: float = SHIM_DELAY_S) -> Path:
    """A fake ``$VCT_VENV`` whose ``bin/python`` sleeps, then appends its argv.

    Appending (not overwriting) is what lets the ORDER of the two diagram CLIs
    be asserted — the index → snapshot sequencing is load-bearing (see the R2
    comment in the hook: a snapshot that runs before the indexer's upsert
    commits finds no row and the first version is lost forever).
    """
    venv = tmp_path / "shimvenv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(
        "#!/bin/sh\n"
        f"sleep {delay_s}\n"
        f'printf "%s\\n" "$*" >> "{marker}"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return venv


def _stage_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude" / "diagrams").mkdir(parents=True)
    (project / ".claude" / "state").mkdir(parents=True)
    (project / ".claude" / "logs").mkdir(parents=True)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"
    diagram.write_text("graph TD;\n  A-->B\n", encoding="utf-8")
    return project


def _run_hook(
    exe: str,
    project: Path,
    edited: Path,
    venv: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run the hook through its production entry point.

    stdout/stderr go to FILES rather than pipes, deliberately. A detached
    child INHERITS the parent's stdio handles, so with ``capture_output=True``
    ``subprocess.run`` would block until the grandchild exits — the pipe stays
    open even though the hook process is long gone. That confound would make
    the timing assertion below measure the child instead of the hook.
    (The inheritance itself is a real .sh/.ps1 parity gap — the POSIX siblings
    detach with ``>/dev/null 2>&1 &`` and hold nothing — but it is a separate
    concern from whether the child survives at all, which is what these tests
    are for.)
    """
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["VCT_VENV"] = str(venv)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("DIAGRAMS_COLLECTION", None)
    if extra_env:
        env.update(extra_env)
    payload = {"tool_input": {"file_path": str(edited)}}
    out_path = project.parent / "hook.out"
    err_path = project.parent / "hook.err"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.run(
            [exe, "-NoProfile", "-File", str(HOOK_PS1)],
            input=json.dumps(payload).encode(),
            stdout=out,
            stderr=err,
            env=env,
            timeout=120,
        )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def _await_lines(marker: Path, count: int, timeout: float = MARKER_TIMEOUT_S) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            lines = [
                ln for ln in marker.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            if len(lines) >= count:
                return lines
        time.sleep(0.2)
    return (
        [ln for ln in marker.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if marker.is_file()
        else []
    )


# ---------------------------------------------------------------------------
# Item 1 — the diagram index → snapshot pair outlives the hook
# ---------------------------------------------------------------------------


def test_diagram_index_and_snapshot_outlive_the_hook(tmp_path):
    """The whole point: the child must still be alive after the hook returns.

    The shim sleeps 1.5 s before writing anything, and the hook exits within
    milliseconds.  Under the previous ``Start-Job`` implementation the job's
    child is reaped with the host runspace, so NEITHER marker line is ever
    written — which is exactly what shipped.
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    marker = tmp_path / "diag-argv.txt"
    venv = _stage_shim_venv(tmp_path, marker)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"

    started = time.monotonic()
    result = _run_hook(exe, project, diagram, venv)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr

    # The hook must NOT have blocked on the child. Measured, not raced: the
    # pair would cost at least 2 x SHIM_DELAY_S if it ran inline.
    assert elapsed < SHIM_DELAY_S, (
        f"the hook took {elapsed:.1f}s — it waited for the diagram child, "
        "but this spawn is fire-and-forget"
    )

    lines = _await_lines(marker, 2)
    assert len(lines) == 2, (
        "the detached diagram child did not complete both CLIs after the hook "
        f"exited (a torn-down Start-Job writes nothing).\nlines={lines!r}\n"
        f"hook stdout={result.stdout!r}\nhook stderr={result.stderr!r}"
    )
    assert "vco_lib.diagram_indexer" in lines[0], f"unexpected first call: {lines[0]!r}"
    assert str(diagram) in lines[0], f"the indexer was not given the edited file: {lines[0]!r}"


def test_the_index_runs_before_the_snapshot(tmp_path):
    """Sequencing is load-bearing, and a detached spawn must not lose it.

    ``snapshot create`` queries ``project_diagrams WHERE file_path=?``.  Run
    before the indexer's UPSERT commits, it finds no row and the file's first
    version is lost forever (the R2 finding of 2026-05-25).  Two parallel
    ``Start-Process`` calls would satisfy "both ran" while reintroducing that
    loss, so assert the ORDER, not just the count.
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    marker = tmp_path / "diag-argv.txt"
    venv = _stage_shim_venv(tmp_path, marker)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"

    result = _run_hook(exe, project, diagram, venv)
    assert result.returncode == 0, result.stderr

    lines = _await_lines(marker, 2)
    assert len(lines) == 2, f"both CLIs did not run: {lines!r}"
    assert " index " in f" {lines[0]} ", f"first call was not the indexer: {lines[0]!r}"
    assert "snapshot create" in lines[1], f"second call was not the snapshot: {lines[1]!r}"
    # The shim sleeps; only a SEQUENTIAL child can produce a deterministic
    # order, so a parallel-spawn regression flips or interleaves these.


def test_the_diagrams_collection_kwarg_is_forwarded(tmp_path):
    """The indexer's Weaviate upsert silently skips without this kwarg.

    Bug-1 of the 2026-05-25 wiring audit.  Re-quoting the argv into an
    EncodedCommand child is exactly where an argument gets dropped, so pin it.
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    marker = tmp_path / "diag-argv.txt"
    venv = _stage_shim_venv(tmp_path, marker)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"

    result = _run_hook(
        exe, project, diagram, venv,
        extra_env={"DIAGRAMS_COLLECTION": "TestProj_Diagrams"},
    )
    assert result.returncode == 0, result.stderr

    lines = _await_lines(marker, 2)
    assert lines, f"no diagram child ran: stderr={result.stderr!r}"
    assert "--diagrams-collection TestProj_Diagrams" in lines[0], (
        f"the collection kwarg was lost in the child command: {lines[0]!r}"
    )


def test_a_path_with_a_quote_and_a_space_survives_the_encoded_command(tmp_path):
    """Re-quoting argv into a child command string is where paths break.

    The pair is emitted as an ``-EncodedCommand`` PowerShell expression, so
    every path is embedded in a single-quoted literal and must be
    quote-doubled. A folder like ``it's here`` is the shape that finds a
    missing escape — and the failure would be silent (a child that dies on a
    parse error writes nothing, exactly like the Start-Job bug).
    """
    exe = _require_pwsh()
    awkward = tmp_path / "it's a dir"
    awkward.mkdir()
    project = _stage_project(awkward)
    marker = tmp_path / "diag-argv.txt"
    venv = _stage_shim_venv(tmp_path, marker)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"

    result = _run_hook(exe, project, diagram, venv)
    assert result.returncode == 0, result.stderr

    lines = _await_lines(marker, 2)
    assert len(lines) == 2, (
        "the detached child did not run both CLIs from a path containing a "
        f"single quote.\nlines={lines!r}\nhook stderr={result.stderr!r}"
    )
    assert str(diagram) in lines[0], (
        f"the quoted path was mangled in the child command: {lines[0]!r}"
    )


def test_a_throttled_edit_launches_nothing(tmp_path):
    """The decision, not just the act.

    A fresh throttle stamp (< 60 s) must suppress the pair entirely.  Without
    this, a mutation that drops the throttle would leave the tests above green
    while turning a 60-second-throttled index into a per-keystroke one.
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    marker = tmp_path / "diag-argv.txt"
    venv = _stage_shim_venv(tmp_path, marker)
    diagram = project / ".claude" / "diagrams" / "flow.mmd"

    # Pre-stamp the throttle file the hook derives from the edited path.
    import hashlib

    digest = hashlib.md5(str(diagram).encode("utf-8")).hexdigest()
    stamp = project / ".claude" / "state" / f"diagram_idx_{digest}.ts"
    stamp.write_text(str(int(time.time())), encoding="utf-8")

    result = _run_hook(exe, project, diagram, venv)
    assert result.returncode == 0, result.stderr
    time.sleep(SHIM_DELAY_S + 2.0)
    assert not marker.is_file(), (
        "the diagram index ran despite a fresh 60s throttle stamp"
    )


# ---------------------------------------------------------------------------
# Item 2 — the shared guard, exercised directly
# ---------------------------------------------------------------------------


def _run_ps(exe: str, script: str, cwd: Path) -> subprocess.CompletedProcess:
    path = cwd / "drive.ps1"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [exe, "-NoProfile", "-File", str(path)],
        capture_output=True, text=True, timeout=120,
    )


def _stage_child_script(tmp_path: Path, name: str, body: str) -> Path:
    """An executable child for Start-Process to launch.

    A FILE rather than ``sh -c '<multi-word string>'``: PowerShell's
    ``Start-Process -ArgumentList`` joins the array with spaces and lets the
    child re-split it, so an argument containing spaces does not survive.
    Production spawns are ``<interpreter> <argv>`` too, so this is also the
    shape actually under test.
    """
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_guarded_spawn_succeeds_where_an_unguarded_one_is_rejected(tmp_path):
    """Both halves in one run, so the contrast is measured, not asserted.

    On this (non-Windows) host an explicit ``-WindowStyle Hidden`` MUST fail —
    that is the platform fact the guard exists for.  If that leg ever stops
    failing the test says so, rather than silently certifying a guard against
    a hazard that no longer exists.
    """
    exe = _require_pwsh()
    if os.name == "nt":
        pytest.skip("the rejection this guards against only occurs off Windows")
    marker = tmp_path / "guarded-ran.txt"
    child = _stage_child_script(
        tmp_path, "child.sh", f'printf "%s" guarded-ok > "{marker}"\n'
    )
    script = f"""
. '{PS_LIB}'
$unguardedFailed = $false
try {{
    Start-Process -FilePath '{child}' -WindowStyle Hidden -ErrorAction Stop | Out-Null
}} catch {{ $unguardedFailed = $true }}
Write-Output "unguarded_failed=$unguardedFailed"
Remove-Item -LiteralPath '{marker}' -ErrorAction SilentlyContinue
Start-VcoDetachedProcess -FilePath '{child}'
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "unguarded_failed=True" in res.stdout, (
        "an unguarded -WindowStyle Hidden did NOT fail on this host — the "
        f"hazard the guard exists for is unproven here.\n{res.stdout!r}"
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not marker.is_file():
        time.sleep(0.1)
    assert marker.is_file(), (
        f"the guarded spawn did not run: {res.stdout!r} {res.stderr!r}"
    )
    assert marker.read_text(encoding="utf-8").strip() == "guarded-ok"


def test_same_path_for_both_redirects_does_not_abort_the_spawn(tmp_path):
    """Quirk 3: identical redirect targets are rejected on EVERY edition.

    ``kg-summary-generator.ps1`` and ``post-git-commit-kg-sync.ps1`` both
    passed one log path twice, so those spawns never started anywhere — not
    only off Windows.  The helper diverts stderr to ``<path>.err`` rather than
    letting the process fail to launch.
    """
    exe = _require_pwsh()
    log = tmp_path / "child.log"
    err = tmp_path / "child.log.err"
    child = _stage_child_script(
        tmp_path, "noisy.sh", 'printf "%s" out\nprintf "%s" err 1>&2\n'
    )
    script = f"""
. '{PS_LIB}'
$rejected = $false
try {{
    Start-Process -FilePath '{child}' `
        -RedirectStandardOutput '{log}' -RedirectStandardError '{log}' -ErrorAction Stop | Out-Null
}} catch {{ $rejected = $true }}
Write-Output "same_path_rejected=$rejected"
Start-VcoDetachedProcess -FilePath '{child}' `
    -RedirectStandardOutput '{log}' -RedirectStandardError '{log}'
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "same_path_rejected=True" in res.stdout, (
        "PowerShell accepted identical redirect targets — the hazard this "
        f"guard exists for is unproven on this host.\n{res.stdout!r}"
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if log.is_file() and err.is_file() and log.read_text() and err.read_text():
            break
        time.sleep(0.1)
    assert log.is_file(), (
        f"the guarded spawn never started: {res.stdout!r} {res.stderr!r}"
    )
    assert err.is_file(), "stderr was not diverted to <path>.err"
    assert log.read_text(encoding="utf-8").strip() == "out"
    assert err.read_text(encoding="utf-8").strip() == "err"


def test_passthru_returns_a_waitable_process(tmp_path):
    """``subagent-stop-reconcile.ps1`` WAITS on its child (30 s cap).

    Routing it through the shared helper must keep ``-PassThru`` returning the
    process object, or the wait degrades to "fire and hope" and a kg-sync that
    hangs is never killed.
    """
    exe = _require_pwsh()
    child = _stage_child_script(tmp_path, "slow.sh", "sleep 0.3\n")
    script = f"""
. '{PS_LIB}'
$p = Start-VcoDetachedProcess -FilePath '{child}' -PassThru
if (-not $p) {{ Write-Output "no_process"; exit 0 }}
Write-Output ("has_id=" + ($p.Id -gt 0))
Write-Output ("exited=" + $p.WaitForExit(15000))
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "has_id=True" in res.stdout, f"-PassThru returned no process: {res.stdout!r}"
    assert "exited=True" in res.stdout, f"the process object was not waitable: {res.stdout!r}"


def test_a_spawn_that_cannot_start_soft_fails_but_says_so(tmp_path):
    """Soft-fail, not silent — the property that would have caught all three.

    Every caller is a best-effort background path, so a failed spawn must not
    throw or change the hook's exit code. But swallowing the reason is exactly
    how ``Start-Job``, the ``-WindowStyle`` rejection and the identical-redirect
    rejection each survived for releases: the work stopped happening and
    nothing said so.

    Both of Start-Process's error SHAPES are exercised, because they need
    different handling and only one is covered by ``-ErrorAction``: an invalid
    ``-WorkingDirectory`` is a NON-terminating error, while an unwritable
    ``-RedirectStandardOutput`` path is raised by ``ThrowTerminatingError``
    and would otherwise print PowerShell's whole coloured error block.

    (Deliberately NOT a missing executable: on Linux ``Start-Process`` hands
    an unknown path to ``gio open``, so the failure comes from a CHILD's
    stderr rather than from the cmdlet — a different mechanism that would
    make this test pass without the helper doing anything.)
    """
    exe = _require_pwsh()
    child = _stage_child_script(tmp_path, "ok.sh", "exit 0\n")
    script = f"""
. '{PS_LIB}'
$ErrorActionPreference = 'Stop'
$a = @(Start-VcoDetachedProcess -FilePath '{child}' -WorkingDirectory '{tmp_path}/no/such/dir')
Write-Output "after_nonterminating=True"
$b = @(Start-VcoDetachedProcess -FilePath '{child}' -RedirectStandardOutput '{tmp_path}/no/such/dir/o.log')
Write-Output "after_terminating=True"
Write-Output ("emitted_on_failure=" + ($a.Count + $b.Count))
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, (
        f"a failed spawn broke the caller: rc={res.returncode} {res.stderr!r}"
    )
    assert "after_nonterminating=True" in res.stdout, (
        f"a non-terminating spawn error stopped the caller: {res.stdout!r}"
    )
    assert "after_terminating=True" in res.stdout, (
        f"a TERMINATING spawn error escaped the helper: {res.stdout!r} {res.stderr!r}"
    )
    assert res.stderr.count("detached spawn FAILED") == 2, (
        f"both failures should be reported exactly once each: {res.stderr!r}"
    )
    assert "WorkingDirectory" in res.stderr and "o.log" in res.stderr, (
        f"the diagnostics do not name what actually went wrong: {res.stderr!r}"
    )
    # PowerShell's own error rendering must not ALSO appear — in a hook that
    # is multi-line noise on a path that is meant to be best-effort.
    assert "Start-Process:" not in res.stderr, (
        f"PowerShell's raw error block leaked through: {res.stderr!r}"
    )
    # ...and a FAILED non-PassThru spawn must still emit nothing on the
    # success stream. This is the branch where `$proc` holds a real `$null`
    # (the happy path holds AutomationNull, which PowerShell drops silently),
    # so an unconditional `return $proc` leaks `$null` into a hook's stdout
    # HERE and only here.
    assert "emitted_on_failure=0" in res.stdout, (
        f"a failed spawn wrote to the success stream: {res.stdout!r}"
    )


def test_a_successful_spawn_stays_quiet_and_returns_nothing(tmp_path):
    """LEAVE-ALONE: the diagnostic must not fire on the happy path.

    Also pins that the non-PassThru form emits NOTHING to the success stream
    — a stray process object there would land in a hook's stdout, which the
    PostToolUse contract turns into dropped-or-corrupt output.
    """
    exe = _require_pwsh()
    child = _stage_child_script(tmp_path, "quiet.sh", "exit 0\n")
    script = f"""
. '{PS_LIB}'
$out = @(Start-VcoDetachedProcess -FilePath '{child}')
Write-Output ("emitted=" + $out.Count)
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "emitted=0" in res.stdout, (
        f"the non-PassThru form wrote to the success stream: {res.stdout!r}"
    )
    assert "detached spawn FAILED" not in res.stderr, (
        f"a successful spawn emitted a failure diagnostic: {res.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Item 3 — QUIRK 3: argument elements survive the spawn roundtrip, and the
# commit-review prompt rides on stdin (MAJOR-R7-1)
# ---------------------------------------------------------------------------


def _await_stable_file(path: Path, timeout: float = MARKER_TIMEOUT_S) -> str:
    """Wait until a detached child's output file has stopped growing.

    The child writes its record and exits; two identical reads a beat apart
    mean it is done.  Returns the final content ('' if it never appeared).
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        cur = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if cur and cur == last:
            return cur
        last = cur
        time.sleep(0.3)
    return last


def test_argument_elements_survive_the_spawn_roundtrip(tmp_path):
    """QUIRK 3: an array ``-ArgumentList`` is JOINED and the child re-splits.

    ``Start-Process`` concatenates an array argument list with single spaces
    and hands the result to the child as ONE command line, which the child
    re-parses: an element containing whitespace loses its boundaries, an
    embedded double quote is consumed as a quoting character, a newline
    splits the element in two, and an EMPTY element vanishes entirely.  This
    is the reviewer's measurement (a Python argv-echo child on pwsh 7.4.6)
    reproduced as a driven test — the helper must encode each element per
    the CommandLineToArgvW rule and pass ONE joined string, or the child
    sees a mangled argv.

    The field consequence is why this is MAJOR, not cosmetic:
    ``post-git-commit-kg-sync.ps1`` passed its whole review prompt — a
    here-string carrying ``git diff`` output — on the command line, and the
    Claude CLI rejected the diff's ``---`` token as an option on every
    non-empty commit.
    """
    exe = _require_pwsh()
    out = tmp_path / "argv-roundtrip.json"
    child = tmp_path / "argv_echo.py"
    child.write_text(
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    # The five payload elements name the five ways a naive space-join
    # destroys an element: interior whitespace, an embedded double quote,
    # an embedded newline, a trailing backslash, and emptiness.
    script = f"""
. '{PS_LIB}'
Start-VcoDetachedProcess -FilePath '{sys.executable}' `
    -ArgumentList @('{child}', 'a b', 'say "hi"', "x`ny", 'trailing\\', '') `
    -RedirectStandardOutput '{out}'
"""
    res = _run_ps(exe, script, tmp_path)
    assert res.returncode == 0, res.stderr

    recorded = _await_stable_file(out, timeout=20)
    assert recorded, (
        "the argv-echo child never reported: the spawn itself is broken.\n"
        f"drive stdout={res.stdout!r}\ndrive stderr={res.stderr!r}"
    )
    expected = ["a b", 'say "hi"', "x\ny", "trailing\\", ""]
    assert json.loads(recorded) == expected, (
        "the child did not receive the argument elements verbatim — "
        "Start-Process joined the array and the child re-split it "
        f"(expected {expected!r}, got {recorded.strip()!r})"
    )


def _stage_commit_review_project(tmp_path: Path) -> Path:
    """A real two-commit repo so ``git diff HEAD~1..HEAD`` is non-empty.

    The second commit's diff carries a no-space marker token: it is the one
    fragment of the diff that survives the child's re-split UNCHANGED, so
    its presence in the recorded argv faithfully detects "the diff rode on
    the command line" even after the split mangles every spaced line of it.
    """
    project = tmp_path / "proj"
    (project / "knowledge").mkdir(parents=True)
    node = project / "knowledge" / "node.md"
    node.write_text("base\n", encoding="utf-8")

    def git(*args: str) -> None:
        env = os.environ.copy()
        # Hermetic: no global/system config (hooks, gpg signing, templates).
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        subprocess.run(
            ["git", *args], cwd=project, check=True, capture_output=True, env=env
        )

    git("init", "-q")
    git("config", "user.email", "hook-test@example.com")
    git("config", "user.name", "hook-test")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    node.write_text("base\nKG_PROMPT_DIFF_MARKER_7f3a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "second")
    return project


def _stage_claude_shim(tmp_path: Path, record: Path) -> Path:
    """A fake ``claude`` CLI that records its argv AND its stdin.

    Argv is written one element per line (``ARG:<element>``), then the stdin
    payload after a ``STDIN-START`` separator — so one record asserts both
    transports: the review prompt must arrive on stdin and must NOT appear
    on the command line.  ``$CLAUDE_SHIM_RECORD`` reaches the detached child
    through the environment Start-Process inherits.
    """
    bin_dir = tmp_path / "shimbin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf \'ARG:%s\\n\' "$a" >> "$CLAUDE_SHIM_RECORD"; done\n'
        'printf \'STDIN-START\\n\' >> "$CLAUDE_SHIM_RECORD"\n'
        'cat >> "$CLAUDE_SHIM_RECORD"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


#: The hook's CLI argv once the prompt rides on stdin: every existing flag
#: kept, in order, and ``-p`` with NO prompt operand after it.
EXPECTED_CLAUDE_ARGV = [
    "-p",
    "--model",
    "haiku",
    "--max-turns",
    "10",
    "--no-session-persistence",
    "--allowedTools",
    "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,"
    "mcp__weaviate-kg__store_knowledge_node,Write,Edit",
]


def test_commit_review_prompt_rides_on_stdin_not_the_command_line(tmp_path):
    """The commit-review prompt must not ride the command line at all.

    Two independent reasons, each fatal in the field:

    * the QUIRK-3 split above — the prompt embeds ``git diff`` output, and
      the Claude CLI parsed the diff's ``---`` token as an option
      (``error: unknown option '---'``, exit 1) on EVERY non-empty commit;
    * the Windows command line is capped at 32 767 chars, and this prompt
      carries the first 300 lines of the unified diff.

    So the hook writes the prompt to ``.claude/logs/kg-commit-review.prompt``
    and spawns ``claude -p`` with ``-RedirectStandardInput`` — the same stdin
    shape ``summary_backends.call_cli`` adopted (``claude -p`` reads the
    prompt from stdin when no positional is given).

    Driven through the hook's production entry point: a real two-commit
    repo, a fake ``claude`` first on PATH that records its argv AND stdin,
    and the assertion is the OBSERVABLE consequence at the child.
    """
    exe = _require_pwsh()
    project = _stage_commit_review_project(tmp_path)
    record = tmp_path / "claude-shim-record.txt"
    bin_dir = _stage_claude_shim(tmp_path, record)

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["CLAUDE_SHIM_RECORD"] = str(record)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    out_path = tmp_path / "commit-hook.out"
    err_path = tmp_path / "commit-hook.err"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.run(
            [exe, "-NoProfile", "-File", str(COMMIT_HOOK_PS1)],
            input=b"",
            stdout=out,
            stderr=err,
            cwd=project,
            env=env,
            timeout=120,
        )
    assert proc.returncode == 0, err_path.read_text(encoding="utf-8", errors="replace")

    text = _await_stable_file(record)
    assert text, (
        "the commit-review child never ran — the spawn is broken.\n"
        f"hook stdout={out_path.read_text(encoding='utf-8', errors='replace')!r}\n"
        f"hook stderr={err_path.read_text(encoding='utf-8', errors='replace')!r}"
    )
    argv_lines = [ln[4:] for ln in text.splitlines() if ln.startswith("ARG:")]
    stdin_body = text.split("STDIN-START\n", 1)[1] if "STDIN-START\n" in text else ""

    # The prompt — with the diff inside — arrived on STDIN.
    assert "### Diff (first 300 lines):" in stdin_body, (
        "the prompt did not arrive on the child's stdin"
    )
    assert "KG_PROMPT_DIFF_MARKER_7f3a" in stdin_body, (
        "the diff body did not arrive on the child's stdin"
    )
    # NO diff text rode the command line (the QUIRK-3 failure mode).
    assert not any("KG_PROMPT_DIFF_MARKER" in a for a in argv_lines), (
        f"diff text reached the CLI as argv: {[a for a in argv_lines if 'MARKER' in a]!r}"
    )
    assert not any("+++" in a or "---" in a for a in argv_lines), (
        "diff header tokens (+++/---) reached the CLI as argv — the CLI "
        "parses a leading '---' as an option and rejects the call"
    )
    # Every existing flag kept, and -p carries NO prompt operand.
    assert argv_lines == EXPECTED_CLAUDE_ARGV, (
        f"the CLI argv changed shape: {argv_lines!r}"
    )


def test_no_hook_spawns_a_detached_child_outside_the_shared_home():
    """Delivery-layer backstop for the guard's ONE home.

    Deliberately NOT the wiring proof (the driven tests above are).  This only
    asserts that no ``.ps1`` under ``templates/hooks/`` reaches for
    ``-WindowStyle`` itself — the guard cannot have one home while call sites
    keep their own copies, and a new hook is the likeliest place for a sixth
    to appear.
    """
    offenders = []
    for path in sorted(HOOKS.rglob("*.ps1")):
        if path == PS_LIB:
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # prose about the guard, not a use of it
            if "-WindowStyle" in line:
                offenders.append(f"{path.relative_to(REPO)}:{num}: {stripped}")
    assert not offenders, (
        "these hooks pass -WindowStyle directly instead of going through "
        "Start-VcoDetachedProcess / Start-VcoDetachedPwsh in "
        "_lib/resolve-powershell.ps1:\n  " + "\n  ".join(offenders)
    )
