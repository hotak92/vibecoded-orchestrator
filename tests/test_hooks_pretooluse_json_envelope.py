# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests that PreToolUse hooks wrap LLM-bound stdout in the
``hookSpecificOutput.additionalContext`` JSON envelope.

PreToolUse hook contract (Claude Code v2.1.x)
---------------------------------------------
Plain stdout from PreToolUse hooks is silently discarded by Claude Code's
hook runner. Only the structured JSON envelope:

    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "additionalContext": "...up to 10000 chars..."
      }
    }

reaches the LLM as a system-reminder. Hooks that print plaintext on the
allow/exit-0 path produce no observable effect — their work is dead.

History
-------
* PR #168 fixed ``pre-edit-context-inject.sh`` (the .sh sibling). The
  .ps1 sibling (Windows) was missed and continued to print plaintext —
  KG injection silently dead on Windows installs until 0.1.7.
* The ``pre-tool-use`` "KG search suggestion" branch (section 5,
  Edit/Write only) had the same bug on BOTH .sh AND .ps1 — never fixed
  in PR #168. Fixed alongside the .ps1 sweep for 0.1.7 fork-readiness.

What this test pins
-------------------
For each PreToolUse hook that emits LLM-bound stdout on the exit-0 path:

* The hook source must contain the literal string ``hookSpecificOutput``
  (the canonical envelope key — typo'd or missing = bug).
* The hook source must reference ``additionalContext``.
* The hook source must reference the truncation cap of ``10000`` chars.
* On the .ps1 side, the envelope must be assembled via ``ConvertTo-Json``
  (PowerShell's stdlib JSON encoder — manual string concatenation is
  fragile around quotes/newlines).
* On the .sh side, the envelope must be assembled via
  ``json.dumps`` (Python stdlib — same rationale).

Hooks NOT in scope (intentionally excluded)
-------------------------------------------
* PostToolUse hooks (``kg-summary-generator``, ``code-graph-incremental``,
  ``post-file-edit``, etc.): stdout doesn't reach the LLM via
  additionalContext on this event. Feedback would require ``{"decision":
  "block", "reason": "..."}`` instead — different contract.
* UserPromptSubmit hooks (``kg-update-nudge`` UserPromptSubmit branch,
  ``user-prompt-submit-reminder``): plain stdout IS injected as context
  by Claude Code per spec ("Plain text stdout → injected as context").
  No envelope needed.
* Exit-2 (blocking) branches in PreToolUse hooks (SSRF guard, shell
  injection scan, Build Anchor): stderr/stdout from blocked actions
  reach the LLM as feedback per spec; envelope not required.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hook directories. PR-39 (v0.2.12, 2026-05-16): templates/hooks/ is the
# single source of truth. Before PR-39 .claude/hooks/ was a byte-identical
# mirror shipped in the repo (and was in this list); install.py now
# renders .claude/ from templates/ at install time, so only templates/
# is canonical at the git layer.
HOOKS_DIRS = [
    REPO_ROOT / "templates" / "hooks",
]

# Hooks that fire on PreToolUse and emit LLM-bound stdout on the exit-0
# (allow) path. These MUST use the JSON envelope.
PRETOOLUSE_LLM_BOUND_HOOKS = [
    # KG + code-graph context for the file being edited.
    "pre-edit-context-inject",
    # Section 5 KG search suggestion (Edit/Write user-prompt concept match).
    "pre-tool-use",
    # V52-M (v0.2.52): >500-char bash KG injection. Sources _lib/emit-context.sh
    # → emit_additional_context which produces the hookSpecificOutput.
    "pre-bash-context-inject",
]


def _hook_path(hook_dir: Path, name: str, ext: str) -> Path:
    return hook_dir / f"{name}.{ext}"


def _envelope_text(hook_dir: Path, hook_path: Path, ext: str) -> str:
    """Return hook source text PLUS the shared emit-context helper if
    the hook sources it.

    Background (2026-05-10): the JSON envelope assembly was extracted
    from each hook into ``_lib/emit-context.{sh,ps1}`` so the
    whitespace-only-content guard could be applied uniformly. After the
    refactor, the literal strings ``hookSpecificOutput`` /
    ``additionalContext`` / ``10000`` / ``json.dumps`` /
    ``ConvertTo-Json`` no longer live in each hook's body — they live
    in the helper. The test must validate the FULL envelope-emission
    path, not just the hook's own bytes.

    Resolution: if the hook sources the helper, concatenate the
    helper's bytes onto the hook's bytes before running the literal
    asserts. Static check only — we're asking "across the hook plus
    the helper it explicitly sources, do all the invariants appear".
    """
    text = hook_path.read_text()
    helper_marker = "_lib/emit-context." + ext
    if helper_marker in text:
        helper = hook_dir / "_lib" / f"emit-context.{ext}"
        if helper.exists():
            text = text + "\n# --- helper: emit-context ---\n" + helper.read_text()
    return text


# --- Static envelope checks -------------------------------------------


@pytest.mark.parametrize(
    "hook_dir,name",
    [(d, n) for d in HOOKS_DIRS for n in PRETOOLUSE_LLM_BOUND_HOOKS],
    ids=lambda x: str(x) if not isinstance(x, Path) else x.name,
)
def test_sh_pretooluse_hook_uses_json_envelope(hook_dir: Path, name: str) -> None:
    """The .sh sibling must wrap LLM-bound stdout in the hookSpecificOutput envelope.

    Static check: the source file must reference all three envelope
    invariants — the wrapper key, the additionalContext field, and the
    10000-char truncation cap.
    """
    if not hook_dir.is_dir():
        pytest.fail(f"hook dir missing from repo (CI env regression): {hook_dir}")
    path = _hook_path(hook_dir, name, "sh")
    if not path.exists():
        pytest.fail(f"expected hook missing: {path}")

    text = _envelope_text(hook_dir, path, "sh")

    assert "hookSpecificOutput" in text, (
        f"{path}: missing literal `hookSpecificOutput` — PreToolUse hooks "
        f"that emit LLM-bound stdout must wrap output in the JSON envelope, "
        f"otherwise Claude Code silently discards it. Either inline the "
        f"envelope OR source `_lib/emit-context.sh` and call "
        f"`emit_additional_context`. See PR #168 + 2026-05-10 helper extraction."
    )
    assert "additionalContext" in text, (
        f"{path}: missing literal `additionalContext` — required envelope "
        f"field for LLM context injection."
    )
    assert "10000" in text, (
        f"{path}: missing literal `10000` truncation cap. The PreToolUse "
        f"additionalContext field has a 10k char limit; hooks must truncate "
        f"to stay within budget."
    )
    assert "json.dumps" in text, (
        f"{path}: envelope assembly should go through `json.dumps` (Python "
        f"stdlib). Manual string concatenation is fragile around quotes "
        f"and newlines in the additionalContext payload."
    )


@pytest.mark.parametrize(
    "hook_dir,name",
    [(d, n) for d in HOOKS_DIRS for n in PRETOOLUSE_LLM_BOUND_HOOKS],
    ids=lambda x: str(x) if not isinstance(x, Path) else x.name,
)
def test_ps1_pretooluse_hook_uses_json_envelope(hook_dir: Path, name: str) -> None:
    """The .ps1 sibling must wrap LLM-bound stdout in the hookSpecificOutput envelope.

    Same invariants as the .sh test, plus ``ConvertTo-Json`` is the
    expected encoder (PowerShell stdlib).
    """
    if not hook_dir.is_dir():
        pytest.fail(f"hook dir missing from repo (CI env regression): {hook_dir}")
    path = _hook_path(hook_dir, name, "ps1")
    if not path.exists():
        pytest.fail(f"expected hook missing: {path}")

    text = _envelope_text(hook_dir, path, "ps1")

    assert "hookSpecificOutput" in text, (
        f"{path}: missing literal `hookSpecificOutput` — PreToolUse hooks "
        f"that emit LLM-bound stdout must wrap output in the JSON envelope, "
        f"otherwise Claude Code silently discards it. Either inline the "
        f"envelope OR source `_lib/emit-context.ps1` and call "
        f"`Emit-AdditionalContext`. Pre-2026-05-08 the .ps1 side missed "
        f"this fix from PR #168 and KG injection on Windows was effectively "
        f"dead."
    )
    assert "additionalContext" in text, (
        f"{path}: missing literal `additionalContext` — required envelope "
        f"field for LLM context injection."
    )
    assert "10000" in text, (
        f"{path}: missing literal `10000` truncation cap. The PreToolUse "
        f"additionalContext field has a 10k char limit; hooks must truncate "
        f"to stay within budget."
    )
    assert "ConvertTo-Json" in text, (
        f"{path}: envelope assembly should go through `ConvertTo-Json` "
        f"(PowerShell stdlib). Manual string concatenation is fragile "
        f"around quotes and newlines in the additionalContext payload."
    )


# --- Negative checks: plaintext stdout regression guard ---------------


@pytest.mark.parametrize(
    "hook_dir",
    HOOKS_DIRS,
    ids=lambda d: d.name if d else "",
)
def test_pre_edit_context_inject_no_unwrapped_get_content(hook_dir: Path) -> None:
    """``pre-edit-context-inject.ps1`` must not call ``Get-Content $CacheFile -Raw``
    bare on the cache-hit path.

    That was the original Windows bug — the cache-hit branch wrote the
    cached plaintext directly to stdout, where Claude Code's hook runner
    silently discarded it. Cache-hit must go through the JSON envelope
    helper instead. Explicit regression guard: `Get-Content $CacheFile`
    is fine when followed by a JSON-emitting helper, but not as the last
    statement before ``exit 0``.
    """
    if not hook_dir.is_dir():
        pytest.fail(f"hook dir missing from repo (CI env regression): {hook_dir}")
    path = hook_dir / "pre-edit-context-inject.ps1"
    if not path.exists():
        pytest.fail(f"expected hook file missing from repo (CI env regression): {path}")

    text = path.read_text()
    # The buggy form was:
    #     if ($age -lt $CacheTtl) {
    #         Get-Content $CacheFile -Raw
    #         exit 0
    #     }
    # Detect: a line containing `Get-Content $CacheFile` immediately
    # followed (next non-blank/comment line) by `exit 0` with no
    # intervening Emit-ContextJson / ConvertTo-Json invocation.
    bad_pattern = re.compile(
        r"Get-Content\s+\$CacheFile\s+-Raw\s*\n\s*exit\s+0",
        re.IGNORECASE,
    )
    if bad_pattern.search(text):
        pytest.fail(
            f"{path}: cache-hit path emits raw `Get-Content $CacheFile -Raw` "
            f"to stdout without wrapping in the JSON envelope. This is the "
            f"original Windows-side KG injection bug — Claude Code silently "
            f"discards plain stdout from PreToolUse hooks. Wrap via "
            f"Emit-ContextJson (or equivalent ConvertTo-Json envelope)."
        )


@pytest.mark.parametrize(
    "hook_dir",
    HOOKS_DIRS,
    ids=lambda d: d.name if d else "",
)
def test_pre_edit_context_inject_no_unwrapped_write_output(hook_dir: Path) -> None:
    """``pre-edit-context-inject.ps1`` must not call ``Write-Output $outStr``
    on the final-output path.

    Same regression guard for the post-search emission path (~line 196
    of the pre-fix file). The fix routes final output through
    Emit-ContextJson, so a bare ``Write-Output $outStr`` is the smoking
    gun for a regression.
    """
    if not hook_dir.is_dir():
        pytest.fail(f"hook dir missing from repo (CI env regression): {hook_dir}")
    path = hook_dir / "pre-edit-context-inject.ps1"
    if not path.exists():
        pytest.fail(f"expected hook file missing from repo (CI env regression): {path}")

    text = path.read_text()
    bad_pattern = re.compile(r"^\s*Write-Output\s+\$outStr\b", re.MULTILINE)
    if bad_pattern.search(text):
        pytest.fail(
            f"{path}: final-emission path uses bare `Write-Output $outStr` — "
            f"this dumps plaintext, which Claude Code discards on PreToolUse. "
            f"Route through Emit-ContextJson (or equivalent ConvertTo-Json "
            f"envelope)."
        )


# --- Out-of-scope hooks: confirm they're correctly NOT using envelope ---


def test_userpromptsubmit_kg_update_nudge_uses_plain_stdout() -> None:
    """``kg-update-nudge`` is a UserPromptSubmit (+ PostToolUse) hook.

    Per the Claude Code v2.1.x hook contract, UserPromptSubmit accepts
    plain text stdout as context (no envelope required). This test pins
    that we don't accidentally introduce the envelope wrapper here —
    that would actually break it (the literal JSON would be injected as
    context instead of the human-readable nudge).

    See: knowledge/research/claude-code-leak-hooks-internals.md §1
    "UserPromptSubmit (matcher: none, always fires) [...] Plain text
    stdout → injected as context."
    """
    for hook_dir in HOOKS_DIRS:
        for ext in ("sh", "ps1"):
            path = hook_dir / f"kg-update-nudge.{ext}"
            if not path.exists():
                continue
            text = path.read_text()
            assert "hookSpecificOutput" not in text, (
                f"{path}: includes `hookSpecificOutput` envelope — "
                f"kg-update-nudge fires on UserPromptSubmit, where plain "
                f"stdout already reaches the LLM as context. Wrapping in "
                f"the PreToolUse envelope would make the literal JSON "
                f"appear in the user's context window."
            )


# --- All-PreToolUse-hooks audit (forward-looking guard) ----------------


def _read_settings_template_pretooluse_hooks() -> set[str]:
    """Return the set of hook FILENAMES (not stems) registered as
    PreToolUse in templates/settings.json.{linux,windows}.template.

    Catches future drift: if a new PreToolUse hook is added that emits
    LLM-bound stdout, this test surface lets us add it to
    PRETOOLUSE_LLM_BOUND_HOOKS without forgetting.
    """
    import json

    out: set[str] = set()
    for fname in (
        "templates/settings.json.linux.template",
        "templates/settings.json.windows.template",
    ):
        p = REPO_ROOT / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        hooks = data.get("hooks", {})
        for matcher in hooks.get("PreToolUse", []):
            for h in matcher.get("hooks", []):
                cmd = h.get("command", "")
                # The command is typically `bash $CLAUDE_PROJECT_DIR/.claude/hooks/<file>.sh`
                # or `pwsh -NoProfile -File <...>/<file>.ps1`. Extract trailing filename.
                m = re.search(r"([\w\-]+\.(?:sh|ps1))(?:\s|$)", cmd)
                if m:
                    out.add(m.group(1))
    return out


def test_no_uncovered_pretooluse_hooks() -> None:
    """If a NEW PreToolUse hook is added to settings templates, this test
    fails until either (a) it's added to PRETOOLUSE_LLM_BOUND_HOOKS, or
    (b) it's added to the EXEMPT set below as a hook that does NOT emit
    LLM-bound stdout.

    Forward-looking guard: PreToolUse hooks that print plaintext are
    silently broken on the LLM-context path. Force a triage decision
    when a new one lands.
    """
    # Hooks confirmed NOT to emit LLM-bound stdout from the wrapper itself.
    # Added when reviewing PreToolUse hooks for envelope correctness.
    EXEMPT_NO_LLM_OUTPUT = {
        # Token-leak guard: rejects vercel CLI invocations with --token=...
        # via exit 2 + stderr. Stderr from blocked actions reaches LLM
        # via Claude Code's blocked-tool feedback path; no envelope
        # required.
        "pre-vercel-token-guard.sh",
        "pre-vercel-token-guard.ps1",
        # lean-ctx rewrite shims (PR-1, v0.2.11): the .sh / .ps1 are
        # thin wrappers that `exec lean-ctx hook rewrite`. The JSON
        # envelope (hookSpecificOutput.updatedInput.command) is produced
        # by the lean-ctx binary itself, not by the wrapper file —
        # so the wrapper file legitimately doesn't contain the
        # envelope-text invariants this test checks for. Envelope
        # correctness is the lean-ctx upstream project's responsibility.
        "lean-ctx-rewrite.sh",
        "lean-ctx-rewrite.ps1",
        # Phase 1.5 diagrams path guard: rejects malformed diagram paths
        # with exit 2 + stderr corrective message (handed to Claude via
        # the blocked-tool feedback path). Exit 0 is silent (no LLM-bound
        # stdout). Same envelope-exempt shape as pre-vercel-token-guard.
        "pre-diagram-path-validation.sh",
        "pre-diagram-path-validation.ps1",
    }

    registered = _read_settings_template_pretooluse_hooks()
    in_scope = {f"{n}.{ext}" for n in PRETOOLUSE_LLM_BOUND_HOOKS for ext in ("sh", "ps1")}

    uncovered = registered - in_scope - EXEMPT_NO_LLM_OUTPUT
    assert not uncovered, (
        "New PreToolUse hook(s) registered in settings templates without "
        "envelope-correctness review:\n  - "
        + "\n  - ".join(sorted(uncovered))
        + "\n\n"
        "If the hook emits LLM-bound stdout on exit-0, add it to "
        "PRETOOLUSE_LLM_BOUND_HOOKS in this test (and ensure it uses "
        "the hookSpecificOutput envelope).\n"
        "If the hook only emits stderr / blocked-action feedback / "
        "doesn't print to stdout, add it to EXEMPT_NO_LLM_OUTPUT."
    )
