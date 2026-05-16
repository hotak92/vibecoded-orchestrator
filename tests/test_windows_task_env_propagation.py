# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for VCT_STACK_* env-var propagation in the Windows scheduled task.

Background: PR-22 (Group A) added `VCT_STACK_COMPOSE_OVERRIDE` to the
systemd unit template (`templates/systemd/claude-mcp-containers.service.template`)
joining the existing `VCT_STACK_WORKING_DIR` and `VCT_STACK_LOG_FILE`. The
matching Windows template (`templates/windows/claude-mcp-containers.task.xml.template`)
DID NOT propagate any of the three until PR-32 (this PR). Before PR-32 the
Windows scheduled task fired bash with none of the env vars the wrapper
script `launch-claude-mcp-stack.sh` expects, silently breaking compose-up
on first boot.

This test enforces the invariant going forward: every `VCT_STACK_*` env var
that the systemd unit template propagates must ALSO be propagated by the
Windows task template, via one of the accepted patterns:
  1. `<Variables>` block under `<Actions>/<Exec>`.
  2. Inline `$env:NAME='val'` in a PowerShell-wrapped `<Arguments>`.
  3. `cmd.exe /c "set NAME=val && ..."` wrapper in `<Arguments>`.

The test parses the XML using stdlib xml.etree (no new dependency) — but
strips top-level comments first, because the pre-existing comment block
contains `--user` (a literal double-dash inside a comment), which the
strict XML spec forbids but Windows Task Scheduler tolerates. The intent
of the test is to validate the `<Task>` element's structure, not the
comments.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_TASK_XML = (
    REPO_ROOT / "templates" / "windows" / "claude-mcp-containers.task.xml.template"
)
SYSTEMD_UNIT = (
    REPO_ROOT / "templates" / "systemd" / "claude-mcp-containers.service.template"
)


def _load_task_xml() -> ET.Element:
    """Return the parsed <Task> root element, with comments stripped."""
    raw = WINDOWS_TASK_XML.read_bytes().decode("utf-8")
    # Strip XML declaration — declares UTF-16 but on-disk file is UTF-8
    # (the file is re-encoded on import by Task Scheduler).
    raw = re.sub(r"^<\?xml[^?]*\?>\s*", "", raw, count=1)
    # Strip all comment blocks — the pre-existing comment contains `--user`
    # which the strict XML spec forbids inside comments but every real-world
    # XML toolchain (including Windows Task Scheduler) tolerates.
    raw = re.sub(r"<!--.*?-->", "", raw, count=0, flags=re.DOTALL)
    return ET.fromstring(raw)


def _systemd_vct_stack_vars() -> set[str]:
    """Extract the set of VCT_STACK_* env var names the systemd unit propagates."""
    if not SYSTEMD_UNIT.is_file():
        pytest.skip(f"systemd unit template not present at {SYSTEMD_UNIT}")
    body = SYSTEMD_UNIT.read_text(encoding="utf-8")
    return set(re.findall(r"Environment=(VCT_STACK_[A-Z_]+)=", body))


def _arguments_text() -> str:
    """Return the inner text of the task's <Arguments> element."""
    root = _load_task_xml()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    # <Task>/<Actions>/<Exec>/<Arguments>
    args = root.find("t:Actions/t:Exec/t:Arguments", ns)
    if args is None:
        pytest.fail("Task XML missing <Actions>/<Exec>/<Arguments>")
    return args.text or ""


def test_windows_task_xml_well_formed() -> None:
    """The task XML must be parseable (after the conventional comment-strip)."""
    root = _load_task_xml()
    assert root.tag.endswith("}Task"), (
        f"Expected root <Task>, got {root.tag!r} — XML structure broken."
    )


def test_windows_task_propagates_working_dir() -> None:
    """The Windows task must propagate VCT_STACK_WORKING_DIR to the wrapper."""
    args = _arguments_text()
    assert "VCT_STACK_WORKING_DIR" in args, (
        "VCT_STACK_WORKING_DIR not found in <Arguments> — wrapper script "
        "won't know where compose.yaml lives at boot time. "
        "Expected one of: 'set VCT_STACK_WORKING_DIR=...', "
        "'$env:VCT_STACK_WORKING_DIR=...', or a <Variables> entry."
    )


def test_windows_task_propagates_log_file() -> None:
    """The Windows task must propagate VCT_STACK_LOG_FILE to the wrapper."""
    args = _arguments_text()
    assert "VCT_STACK_LOG_FILE" in args, (
        "VCT_STACK_LOG_FILE not found in <Arguments> — wrapper script "
        "won't know where to write its boot log."
    )


def test_windows_task_propagates_compose_override() -> None:
    """The Windows task must propagate VCT_STACK_COMPOSE_OVERRIDE.

    Added by PR-22 (Group A) to the systemd unit; this test ensures Windows
    stays in lockstep. Without it, the compose-override mechanism (GPU
    overlay, custom volumes, etc.) silently no-ops on Windows.
    """
    args = _arguments_text()
    assert "VCT_STACK_COMPOSE_OVERRIDE" in args, (
        "VCT_STACK_COMPOSE_OVERRIDE not found in <Arguments> — Windows "
        "loses parity with the systemd unit on the compose-override "
        "mechanism added in PR-22."
    )


def test_windows_task_propagates_all_systemd_vct_stack_vars() -> None:
    """Lockstep invariant: every VCT_STACK_* in the systemd unit must
    also appear in the Windows task <Arguments>.

    This is the catch-all that prevents future drift: if someone adds a
    new VCT_STACK_FOO to the systemd template without mirroring on
    Windows, this test fires.
    """
    systemd_vars = _systemd_vct_stack_vars()
    if not systemd_vars:
        pytest.skip("systemd template declares no VCT_STACK_* vars — nothing to mirror")
    args = _arguments_text()
    missing = sorted(v for v in systemd_vars if v not in args)
    assert not missing, (
        f"Windows task XML missing VCT_STACK_* vars present in systemd unit: "
        f"{missing!r}. Add each to <Arguments> using one of the documented "
        f"patterns (cmd.exe `set`, PowerShell `$env:`, or <Variables>)."
    )


def test_windows_task_uses_recognized_env_propagation_pattern() -> None:
    """The env-var propagation must use one of three accepted patterns."""
    args = _arguments_text()
    root = _load_task_xml()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    has_variables_block = (
        root.find("t:Actions/t:Exec/t:Variables", ns) is not None
    )
    has_cmd_set_wrapper = bool(re.search(r"\bset\s+VCT_STACK_", args))
    has_powershell_env = "$env:VCT_STACK_" in args
    assert has_variables_block or has_cmd_set_wrapper or has_powershell_env, (
        "<Arguments> must use one of: <Variables> block, `cmd.exe /c \"set \"` "
        "wrapper, or PowerShell `$env:` assignment to propagate VCT_STACK_* vars. "
        f"Got: {args!r}"
    )


def test_windows_task_still_invokes_wrapper_script() -> None:
    """The env-var wrapper must still pass control to {{WRAPPER_SCRIPT}}.

    Regression guard: if someone refactors the <Arguments> to use a different
    pattern and accidentally drops the bash invocation, the task fires the
    env-setter but never runs the boot script — silent no-op.
    """
    args = _arguments_text()
    assert "{{WRAPPER_SCRIPT}}" in args, (
        "<Arguments> must still invoke the wrapper script (template "
        "substitution slot `{{WRAPPER_SCRIPT}}`)."
    )
