# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``rc-native`` skill and the Remote Control disclosure, as shipped.

Background (live-verified 2026-09-04, Claude Code 2.1.258): Remote Control
is ENDPOINT-gated — it initializes only in sessions talking directly to
``api.anthropic.com``. A panel pointed at the VCO model gateway sets
``ANTHROPIC_BASE_URL`` to the gateway, so ``/remote-control`` there always
fails with "Remote Control initialization failed". claude.ai OAuth does NOT
bypass the gate, and env-token auth (API key, ``setup-token``,
``CLAUDE_CODE_OAUTH_TOKEN``) fails a second, independent full-scope-login
gate. No gateway or panel configuration can change this; the supported
shape is a native-auth Remote Control session COEXISTING with the gateway
panel.

These tests pin what the shipped artefacts must say — the two probe
findings that cost real debugging time (CLI workspace trust is separate
from panel usage; the one-time consent prompt), the by-design reboot
behaviour, the cross-platform script pair — and, negatively, that nothing
in them promises a compatibility that does not exist (a "downgrade to get
it back" suggestion, or compat wording for gateway panels).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "templates" / "skills" / "rc-native" / "SKILL.md"
TROUBLESHOOTING = REPO / "docs" / "TROUBLESHOOTING.md"


def _text(path: Path) -> str:
    assert path.is_file(), f"{path} must ship with the orchestrator"
    return path.read_text(encoding="utf-8")


def _assert_no_downgrade_advice(text: str) -> None:
    """No shipped sentence may recommend downgrading below v2.1.196.

    Checked per SENTENCE, not per line: markdown rewrapping can put the word
    "downgrade" on a continuation line far from its "do not", and a
    line-based check would flag honest refusals (that false positive is how
    guidance gets deleted to make a test pass).
    """
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        low = sentence.lower()
        if "downgrad" in low:
            assert any(
                neg in low
                for neg in ("not", "never", "don't", "dont", "won't", "wont",
                            "can't", "cannot", "avoid")
            ), f"downgrade mentioned without a refusal: {sentence!r}"


# ---------------------------------------------------------------------------
# The skill
# ---------------------------------------------------------------------------


def test_skill_ships_with_valid_frontmatter():
    text = _text(SKILL)
    assert text.startswith("---\n"), "SKILL.md must open with frontmatter"
    fm = text.split("---\n", 2)[1]
    meta = {}
    for line in fm.splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    assert meta.get("name") == "rc-native"
    assert meta.get("description"), "no description = never auto-invoked"
    # v0.2.93: the launcher's populate validates this key; a bundled skill
    # without it warns in every project (field 2026-09-08).
    assert meta.get("model"), "rc-native must declare a model:"




def test_every_bundled_skill_declares_a_model():
    """v0.2.93 (field 2026-09-08): the rc-native skill shipped in v0.2.92
    without a ``model:`` key — the launcher's populate warns per project and
    the GUI renders Model as "—". Every bundled skill must declare one so the
    Agents/Skills tabs stay honest about what answers.
    """
    import re as _re
    skills_dir = Path(__file__).resolve().parent.parent / "templates" / "skills"
    missing = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        fm = skill_md.read_text(encoding="utf-8").split("---", 2)
        if len(fm) < 3 or not _re.search(r"^model:\s*\S+", fm[1], _re.MULTILINE):
            missing.append(skill_md.parent.name)
    assert not missing, f"bundled skills without model: frontmatter: {missing}"
def test_skill_auto_starts_the_backend_on_every_invocation():
    text = _text(SKILL).lower()
    assert "every invocation" in text or "auto-start" in text
    assert "idempotent" in text
    # stop is the named exception — restarting after an explicit stop would
    # fight the user.
    assert "stop" in text


def test_skill_encodes_the_workspace_trust_probe_finding():
    """Finding 1 of 2: CLI trust is separate from panel usage."""
    text = _text(SKILL)
    assert "Workspace not trusted" in text
    # The remedy is ONE interactive `claude` run — never a headless
    # workaround, never a trust-file edit.
    assert "interactive" in text.lower()
    # The counter-intuitive half of the finding: panel usage does NOT
    # record CLI trust.
    assert "panel" in text.lower()


def test_skill_encodes_the_consent_prompt_probe_finding():
    """Finding 2 of 2: the one-time "Enable Remote Control? (y/n)" prompt."""
    text = _text(SKILL)
    assert "Enable Remote Control" in text


def test_skill_states_the_reboot_behaviour():
    """The server not surviving a reboot is BY DESIGN, not a bug."""
    assert "reboot" in _text(SKILL).lower()


def test_skill_discloses_the_endpoint_gate_with_version():
    text = _text(SKILL)
    assert "api.anthropic.com" in text
    assert "v2.1.196" in text, "the gate's introduction version is load-bearing"


def test_skill_names_both_platform_siblings():
    """The feature exists on Windows too — the skill must say how.

    A POSIX-only skill would silently not exist for a whole platform (the
    project's field tester is on Windows), which is the "dead half" shape
    the two-never-parsed .ps1 hooks already shipped once (v0.2.80).
    """
    text = _text(SKILL)
    assert "rc-native.sh" in text, "the POSIX command must be named"
    assert "rc-native.ps1" in text, "the Windows command must be named"
    low = text.lower()
    assert "windows" in low
    assert "posix" in low or "linux" in low or "macos" in low
    # And it must not scope the feature down with a Windows carve-out.
    for bad in ("not supported on windows", "windows is unsupported",
                "posix only", "posix-only"):
        assert bad not in low, f"skill scopes the feature down: {bad!r}"


#: The stock-Windows invocation shape every rc-native.ps1 command line in
#: the shipped docs must use. `powershell.exe` (5.1) ships with every
#: Windows since 2009; `pwsh` is PowerShell 7, a SEPARATE install. The
#: `-ExecutionPolicy Bypass` matters as much as the interpreter name:
#: stock Windows' default execution policy is Restricted, which refuses
#: to run .ps1 files at all — an invocation without it fails before the
#: script's first line.
_STOCK_WINDOWS_INVOCATION = (
    "powershell -NoProfile -ExecutionPolicy Bypass -File"
)


def _interpreter_is_pwsh_before_script(line: str) -> bool:
    """True when a line uses `pwsh` AS THE INTERPRETER for rc-native.ps1
    (pwsh appears before the script path) — the unreachable-on-stock-
    Windows shape. A trailing prose mention of pwsh AFTER the path (the
    "PowerShell 7 users can substitute" note) is not an invocation."""
    m = re.search(r"\bpwsh\b", line)
    return (
        bool(m)
        and "rc-native.ps1" in line
        and m.start() < line.index("rc-native.ps1")
    )


def test_skill_windows_invocation_uses_the_stock_windows_interpreter():
    """M2 (delivery audit 2026-09-05): rc-native.ps1 supports Windows
    PowerShell 5.1, but the skill told users to launch it with `pwsh` —
    PowerShell 7, absent from stock Windows — so the feature existed and
    was unreachable (R42's shape one level up: a doc that names an
    interpreter the OS lacks narrows the feature as effectively as not
    writing the .ps1). Every invocation of rc-native.ps1 must be
    copy-pasteable on a stock Windows box: the `powershell` interpreter
    plus `-NoProfile -ExecutionPolicy Bypass -File`."""
    text = _text(SKILL)
    bad = [ln for ln in text.splitlines() if _interpreter_is_pwsh_before_script(ln)]
    assert not bad, (
        f"skill invokes rc-native.ps1 via pwsh, which stock Windows lacks: {bad}"
    )
    invocations = [
        ln
        for ln in text.splitlines()
        if "rc-native.ps1" in ln and ("start" in ln or "<command>" in ln)
    ]
    assert len(invocations) >= 2, (
        "expected the auto-start block and the command reference to both "
        "name the Windows invocation"
    )
    for ln in invocations:
        assert _STOCK_WINDOWS_INVOCATION in ln, (
            f"Windows invocation is not copy-pasteable on stock Windows "
            f"(needs '{_STOCK_WINDOWS_INVOCATION} …'): {ln!r}"
        )


def test_troubleshooting_rc_native_invocation_uses_the_stock_interpreter():
    """Same defect, same fix, one file over: TROUBLESHOOTING.md shows the
    start command too, and a user who finds the doc instead of the skill
    must get a command their Windows can run."""
    text = _text(TROUBLESHOOTING)
    bad = [ln for ln in text.splitlines() if _interpreter_is_pwsh_before_script(ln)]
    assert not bad, (
        f"TROUBLESHOOTING.md invokes rc-native.ps1 via pwsh, which stock "
        f"Windows lacks: {bad}"
    )
    if "rc-native.ps1" in text:  # the remedy block must be present at all
        assert _STOCK_WINDOWS_INVOCATION in text, (
            "TROUBLESHOOTING.md names rc-native.ps1 but never with the "
            "stock-Windows invocation shape"
        )


def test_skill_never_suggests_downgrading():
    """v2.1.195 and below allowed Remote Control through a gateway; auto-
    update reverts the downgrade and a month of fixes goes with it. No
    shipped guidance may recommend it."""
    _assert_no_downgrade_advice(_text(SKILL))


# ---------------------------------------------------------------------------
# The troubleshooting disclosure
# ---------------------------------------------------------------------------


def test_troubleshooting_maps_the_toast_to_the_endpoint_gate():
    text = _text(TROUBLESHOOTING)
    # The exact toast string the user sees and will search for.
    assert "Remote Control initialization failed" in text
    assert "api.anthropic.com" in text
    assert "ANTHROPIC_BASE_URL" in text


def test_troubleshooting_names_the_supported_coexistence_remedy():
    text = _text(TROUBLESHOOTING)
    assert "remote-control" in text
    # The remedy is a native session ALONGSIDE the gateway panel — not
    # "unset the gateway" and not a compat promise.
    assert "alongside" in text.lower() or "coexist" in text.lower()


def test_troubleshooting_lists_what_does_not_work():
    text = _text(TROUBLESHOOTING)
    low = text.lower()
    # OAuth does not bypass the endpoint gate — the tempting wrong answer.
    assert "claude.ai" in low or "oauth" in low
    # And never a downgrade suggestion, here either.
    _assert_no_downgrade_advice(text)


def test_troubleshooting_mentions_the_machine_scope_of_the_env_setting():
    """The 09-02 handoff's "untested caveat", now tested: a per-workspace
    ``.vscode/settings.json`` override CANNOT split the panel, because
    ``claudeCode.environmentVariables`` has VS Code scope ``machine``."""
    text = _text(TROUBLESHOOTING)
    low = text.lower()
    assert "per-workspace" in low, (
        "the doc must name the per-workspace idea the user will try first"
    )
    assert "machine" in low, "and say the setting's scope is machine-wide"


# ---------------------------------------------------------------------------
# The launcher's point-of-action warning (Part 1) — pinned from the Python
# side via the pure-function surface the Svelte route renders.
# ---------------------------------------------------------------------------


def test_point_panel_warning_text_is_available_for_the_gui():
    """The RC disclosure must exist in the launcher's warning surface.

    Reading the TS source for the sentinel strings (not running vitest
    from pytest) keeps this a single-repo contract test: if someone deletes
    or conditions the warning away, this reds even before ``npm run check``
    runs.
    """
    ts = (
        REPO / "launcher" / "src" / "lib" / "api" / "model_gateway.ts"
    ).read_text(encoding="utf-8")
    assert "Remote Control" in ts
    assert "api.anthropic.com" in ts
    assert "remote-control" in ts


def test_write_result_type_carries_values_healed():
    """R41: the wire contract declares the healed-slot field, so the GUI can
    surface it without a silent schema drift."""
    ts = (
        REPO / "launcher" / "src" / "lib" / "types" / "model-gateway.ts"
    ).read_text(encoding="utf-8")
    assert "values_healed" in ts


# ---------------------------------------------------------------------------
# The rc-native script pair — parity is a feature, not a courtesy
# ---------------------------------------------------------------------------

SCRIPT_SH = REPO / "templates" / "scripts" / "rc-native.sh"
SCRIPT_PS1 = REPO / "templates" / "scripts" / "rc-native.ps1"

#: Every command in the surface, present in BOTH siblings.
_RC_COMMANDS = ("start", "status", "url", "stop", "logs")

#: The gateway routing vars the start path strips from the server's env —
#: same list as claude-or native mode. A stale entry here means a gateway-
#: pointed session leaks into the "native" server.
_STRIP_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
)


@pytest.mark.parametrize("command", _RC_COMMANDS)
def test_both_siblings_expose_every_command(command: str):
    """Same command surface: a command that exists in one and not the other
    is a silent platform gap, invisible to the parity gate (which checks
    file presence, not behaviour)."""
    for path in (SCRIPT_SH, SCRIPT_PS1):
        text = _text(path)
        assert re.search(rf"'{command}'|\"{command}\"|\b{command}\)", text), (
            f"{path.name} does not dispatch the '{command}' command"
        )


@pytest.mark.parametrize("key", _STRIP_KEYS)
def test_both_siblings_strip_every_gateway_routing_var(key: str):
    for path in (SCRIPT_SH, SCRIPT_PS1):
        assert key in _text(path), (
            f"{path.name} does not strip {key} — the 'native' server would "
            "still be routed by the gateway"
        )


def test_both_siblings_detect_workspace_not_trusted_with_the_remedy():
    """The probe finding that cost real debugging time, encoded in the
    scripts themselves (not only the skill): the exact error string AND the
    one-interactive-run remedy, with the panel-does-not-record-trust fact."""
    for path in (SCRIPT_SH, SCRIPT_PS1):
        text = _text(path)
        assert "Workspace not trusted" in text, f"{path.name} misses the string"
        assert "interactive" in text.lower(), f"{path.name} misses the remedy"
        assert "panel" in text.lower(), (
            f"{path.name} must say panel usage does not record CLI trust"
        )


def test_both_siblings_answer_the_first_run_consent_prompt():
    """Finding 2 of 2: the one-time "Enable Remote Control? (y/n)" prompt is
    answered automatically on every start (unread is harmless)."""
    assert "printf \"y\\n\"" in _text(SCRIPT_SH)
    ps1 = _text(SCRIPT_PS1)
    assert "Enable Remote Control" in ps1
    assert "WriteLine('y')" in ps1


def test_both_siblings_keep_stdin_open_while_the_server_lives():
    """The never-EOFing stdin: the detached TUI exits on stdin EOF, so the
    holder must outlive the launcher. Bash holds the pipe with
    `sleep infinity`; PowerShell holds StandardInput open until WaitForExit
    returns — different mechanics, same contract."""
    assert "sleep infinity" in _text(SCRIPT_SH)
    ps1 = _text(SCRIPT_PS1)
    assert "WaitForExit" in ps1
    assert "RedirectStandardInput" in ps1


def test_both_siblings_kill_the_whole_tree_on_stop():
    """Bash: kill -- -PGID (process group). Windows has no process groups —
    the real equivalent is taskkill /T (tree kill), not a bare Stop-Process
    that would orphan the relay children."""
    assert "kill -- " in _text(SCRIPT_SH)
    ps1 = _text(SCRIPT_PS1)
    assert "taskkill" in ps1
    assert "/T" in ps1


def test_both_siblings_redact_the_join_url_in_logs():
    """The environment id in the URL is a capability; `logs` output must be
    paste-safe."""
    assert "environment=<redacted>" in _text(SCRIPT_SH)
    assert "environment=<redacted>" in _text(SCRIPT_PS1)


def test_ps1_param_block_is_the_first_statement():
    """The v0.2.92 parse-gate lesson: a `param()` below executable code
    fails the WHOLE file wholesale on every Windows install. The gate
    (tests/test_v0292_powershell_parses.py) catches syntax errors; this
    catches the structural trap specifically. Comments and the
    ``[CmdletBinding()]`` attribute above ``param()`` are legal and skipped."""
    # Strip the UTF-8 BOM first: '﻿#' is not a comment line to a
    # naive startswith('#') check.
    text = _text(SCRIPT_PS1).lstrip("\ufeff")
    before_param = text.split("param(", 1)[0]
    stripped = "\n".join(
        line for line in before_param.splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and not line.strip().startswith("[")
    )
    assert not stripped, (
        "executable code above param() — the file parses as command calls "
        "and dies at load time on Windows"
    )
