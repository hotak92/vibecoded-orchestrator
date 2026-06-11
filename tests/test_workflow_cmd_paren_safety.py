# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Static parse-safety lint for `shell: cmd` steps in GitHub workflows (v0.2.54 G-1).

Why this exists: cmd.exe parses an entire `if ... ( ... )` compound before
executing it, and an unescaped `)` inside the block — including inside echo
TEXT — terminates the block early. The whole step then aborts at parse time
with the cryptic `"X was unexpected at this time."` and exit code 255.

This exact bug shipped three times in `.github/workflows/install-smoke-tri-os.yml`
(v0.2.53):

    echo ::warning::install.py --bootstrap not yet implemented (Track B pending)^; ...
    echo ::warning::launcher --check-only not yet implemented (Track C pending)^; ...
    echo ::error::%MISSING% install artifact(s) missing

all inside `( ... )` blocks — and kept the tri-OS install smoke red from the
day the workflow landed (first observed: run 27340118156, "; was unexpected
at this time.", exit 255). cmd.exe cannot be executed on the Linux CI runners
that run pytest, so this lint statically rejects the known-fatal pattern
instead. The LIVE counterpart is the workflow itself running on
windows-latest.

Second rule: invoking a `.bat`/`.cmd` from a cmd step without `call` CHAINS
to it — control never returns, so every subsequent line of the step
(errorlevel guards, diagnostics) is silently dead code. That bug made
installer-smoke.yml's "first-install.bat parses under cmd.exe" step
meaningless (the findstr guard never ran; CI run 27316431468).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _iter_cmd_run_blocks():
    """Yield (workflow_name, job_id, step_name, run_text) for shell: cmd steps."""
    for wf_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        jobs = data.get("jobs") or {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            job_default_shell = (
                (job.get("defaults") or {}).get("run", {}).get("shell")
            )
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                shell = step.get("shell", job_default_shell)
                run = step.get("run")
                if shell == "cmd" and isinstance(run, str):
                    yield (
                        wf_path.name,
                        job_id,
                        step.get("name", "<unnamed>"),
                        run,
                    )


def _strip_quoted_and_escaped(line: str) -> str:
    """Remove double-quoted spans and caret-escaped characters.

    Parens inside "..." or escaped with ^ do not participate in cmd.exe
    block matching; remove them so the scanner only sees parse-significant
    parens.
    """
    out = []
    in_quote = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "^" and not in_quote and i + 1 < len(line):
            i += 2  # caret escapes the next char (outside quotes)
            continue
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote:
            out.append(ch)
        i += 1
    return "".join(out)


def find_cmd_parse_hazards(run_text: str) -> list[str]:
    """Return a list of hazard descriptions for one cmd run block."""
    hazards: list[str] = []
    depth = 0
    lines = run_text.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    last_line = non_empty[-1] if non_empty else ""

    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped:
            continue
        bare = _strip_quoted_and_escaped(stripped)
        first_word = stripped.split(None, 1)[0].lower() if stripped.split() else ""

        # Rule 1: echo / REM text containing parse-significant parens while
        # inside a ( ... ) block. cmd.exe block matching scans these lines.
        if depth > 0 and first_word in ("echo", "echo.", "rem"):
            text = bare.split(None, 1)[1] if len(bare.split(None, 1)) > 1 else ""
            if "(" in text or ")" in text:
                hazards.append(
                    f"line {lineno}: unescaped paren in `{first_word}` text inside a "
                    f"( ) block — cmd.exe aborts the step at parse time "
                    f"('was unexpected at this time'): {stripped!r}"
                )
                continue  # don't double-count its parens in depth tracking

        # Rule 2: .bat/.cmd invocation without `call` that is not the final
        # line — everything after it is dead code (cmd chains, not calls).
        m = re.match(r"^([\w.\\/:%~\-]+\.(?:bat|cmd))\b", stripped, re.IGNORECASE)
        if m and stripped != last_line.strip():
            hazards.append(
                f"line {lineno}: `{m.group(1)}` invoked without `call` and is not "
                f"the last line — control never returns; subsequent guard lines "
                f"are dead code: {stripped!r}"
            )

        # Depth tracking on parse-significant parens.
        depth += bare.count("(") - bare.count(")")
        depth = max(depth, 0)

    return hazards


def test_no_cmd_parse_hazards_in_workflows():
    """Every shell: cmd step in every workflow must be free of the known-fatal
    cmd.exe parse hazards (paren-in-echo-inside-block, missing `call`)."""
    cmd_steps = list(_iter_cmd_run_blocks())
    assert cmd_steps, (
        "expected at least one shell: cmd step in .github/workflows/ — "
        "if they were all removed, delete this test too"
    )
    failures = []
    for wf, job, step, run_text in cmd_steps:
        for hazard in find_cmd_parse_hazards(run_text):
            failures.append(f"{wf} :: {job} :: {step} :: {hazard}")
    assert not failures, (
        "cmd.exe parse hazards found (these abort the whole step at parse "
        "time on windows-latest):\n  " + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# Detector self-tests: prove the lint catches the three bugs that shipped in
# v0.2.53 (regression-test the regression test).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "snippet",
    [
        # The bootstrap-pre Windows bug (run 27340118156, exit 255).
        (
            "py -3 install.py --bootstrap --json > pre.json 2> pre.stderr\n"
            "if errorlevel 1 (\n"
            "  findstr /I \"unknown\" pre.stderr >NUL && (\n"
            "    echo ::warning::not yet implemented (Track B pending)^; skipping\n"
            "    exit /b 0\n"
            "  )\n"
            ")\n"
        ),
        # The verify-artifacts bug: artifact(s) inside a block.
        (
            "if %MISSING% gtr 0 (\n"
            "  echo ::error::%MISSING% install artifact(s) missing\n"
            "  exit /b 1\n"
            ")\n"
        ),
    ],
)
def test_detector_catches_paren_in_echo_inside_block(snippet):
    hazards = find_cmd_parse_hazards(snippet)
    assert any("unescaped paren" in h for h in hazards), hazards


def test_detector_catches_missing_call():
    # The installer-smoke W-P1-3 bug (run 27316431468): .bat invoked without
    # `call`, so the guard lines below never executed.
    snippet = (
        "first-install.bat /help > bat-help.txt 2>&1\n"
        "set BAT_EC=%ERRORLEVEL%\n"
        "findstr /I /C:\"is not recognized\" bat-help.txt > nul\n"
        "exit /b 0\n"
    )
    hazards = find_cmd_parse_hazards(snippet)
    assert any("without `call`" in h for h in hazards), hazards


def test_detector_allows_safe_patterns():
    # Escaped parens, quoted parens, parens in echo at top level, and
    # `call something.bat` are all fine.
    snippet = (
        "echo top-level parens are fine (really)\n"
        "call first-install.bat /help > out.txt 2>&1\n"
        "if errorlevel 1 (\n"
        "  echo escaped parens ^(ok^) and quoted \")\" are safe\n"
        "  exit /b 1\n"
        ")\n"
        "echo done\n"
    )
    assert find_cmd_parse_hazards(snippet) == []
