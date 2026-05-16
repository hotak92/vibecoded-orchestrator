# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Body-parity tests for the 4 .ps1 hooks ported in PR-32 (Group K Phase B).

The CI gate at `.github/scripts/check_hook_parity.py` checks the invocation
shape (every .sh has a .ps1 sibling, both are modified together) but does
NOT diff body content. Empirical evidence at PR-32 base: kg-update-nudge.sh
had 115 function/section markers vs 39 in .ps1; pre-edit-context-inject was
112 vs 71; check-no-fork-bomb was 54 vs 28; pre-vercel-token-guard was 27
vs 10. Most of those deltas were idiomatic (heredoc vs PowerShell here-string,
inline find-python vs probe-loop), but a handful were real logic gaps that
silently degraded the Windows hooks.

These tests assert PRESENCE of the key incident-fix logic on the .ps1 side
by looking for fingerprint strings — not exact phrasing. They're necessarily
fuzzy (text-based) and accept any of several reasonable wordings. Don't
tighten these to exact string matches — the goal is to catch logic
regressions, not cosmetic edits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_HOOKS = REPO_ROOT / "templates" / "hooks"


def _read(name: str, suffix: str, src: str = "templates") -> str:
    # PR-39 (v0.2.12, 2026-05-16): src param retained for API stability;
    # only "templates" is meaningful now. The .claude/ mirror was deleted —
    # install.py renders it from templates/ at install time.
    if src != "templates":
        raise ValueError(
            f"unsupported src={src!r}; only 'templates' is valid post-PR-39"
        )
    return (TEMPLATES_HOOKS / f"{name}{suffix}").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# kg-update-nudge.ps1 — 5 cumulative incident fixes from the .sh history
# --------------------------------------------------------------------------


def test_kg_update_nudge_ps1_uses_stdout_for_nudge() -> None:
    """The v3 stdout-vs-stderr fix: UserPromptSubmit hooks surface STDOUT
    as <system-reminder>, not stderr. The .ps1 must emit the nudge via
    Python `print()` (not `sys.stderr.write` / `[Console]::Error.WriteLine`).
    """
    body = _read("kg-update-nudge", ".ps1")
    assert "print(msg)" in body, (
        "kg-update-nudge.ps1 must use `print(msg)` for the nudge text — "
        "UserPromptSubmit hooks surface stdout (not stderr) as a "
        "system-reminder. Search-first marker fingerprint: 'print(msg)'."
    )


def test_kg_update_nudge_ps1_handles_silent_failure_in_transcript_parse() -> None:
    """v3 silent-failure handling: transcript JSON parse errors must
    `sys.exit(0)` rather than propagating an exception (which would
    crash the hook and surface a noisy traceback to the user).
    """
    body = _read("kg-update-nudge", ".ps1")
    # Look for the try/except around json.loads(INPUT) → sys.exit(0)
    assert "json.JSONDecodeError" in body and "sys.exit(0)" in body, (
        "kg-update-nudge.ps1 must swallow json.JSONDecodeError on the "
        "stdin payload and `sys.exit(0)` silently — see v3 silent-failure "
        "fix in the .sh sibling."
    )


def test_kg_update_nudge_ps1_implements_search_first_workflow() -> None:
    """v6+ search-first workflow: the transcript scan via TranscriptScanner
    must happen BEFORE the threshold check. Detect by asserting the
    TranscriptScanner import + scan call appear ABOVE the threshold check.
    """
    body = _read("kg-update-nudge", ".ps1")
    scan_idx = body.find("TranscriptScanner()")
    fire_idx = body.find("should_fire")
    assert scan_idx > 0, (
        "kg-update-nudge.ps1 missing TranscriptScanner usage — search-first "
        "workflow not present."
    )
    assert fire_idx > 0, "kg-update-nudge.ps1 missing should_fire branch logic."
    assert scan_idx < fire_idx, (
        "kg-update-nudge.ps1: TranscriptScanner().scan(...) must run BEFORE "
        "the should_fire threshold check (search-first workflow). "
        f"Got scan_idx={scan_idx} > fire_idx={fire_idx}."
    )


def test_kg_update_nudge_ps1_resets_baseline_on_kg_write() -> None:
    """25k interval baseline reset: a KG-write event (Write/Edit to
    knowledge/**/*.md OR store_knowledge_node call) must reset the
    counter so subsequent nudges measure work since LAST write.
    """
    body = _read("kg-update-nudge", ".ps1")
    # The reset is in the `elif is_post_tool and is_knowledge_update:` branch.
    assert "is_knowledge_update" in body, (
        "kg-update-nudge.ps1 missing the is_knowledge_update branch — "
        "counter won't reset after KG writes."
    )
    assert "store_knowledge_node" in body, (
        "kg-update-nudge.ps1 missing store_knowledge_node detection — "
        "the MCP-write path won't trigger a baseline reset."
    )
    # fired_once must be cleared on reset so the next nudge starts fresh.
    assert '"fired_once"' in body and "False" in body, (
        "kg-update-nudge.ps1 must clear fired_once on baseline reset."
    )


def test_kg_update_nudge_ps1_has_transcript_escape_hatch() -> None:
    """Transcript-based escape hatch: if the assistant writes a
    `[No KG update needed: <reason>]` marker in its top-level text,
    treat it as a baseline-reset event (saves the agent from having
    to write a placeholder KG file just to silence the nudge).
    """
    body = _read("kg-update-nudge", ".ps1")
    assert "NO_KG_UPDATE_MARKER_RE" in body, (
        "kg-update-nudge.ps1 missing the NO_KG_UPDATE_MARKER_RE regex — "
        "transcript-based escape hatch not present."
    )
    assert "No KG update needed" in body, (
        "kg-update-nudge.ps1 missing the literal marker text — escape "
        "hatch can't match assistant turns."
    )
    assert "escape_hatch_active" in body, (
        "kg-update-nudge.ps1 missing the escape_hatch_active branch — "
        "marker detection won't trigger a baseline reset."
    )


# --------------------------------------------------------------------------
# pre-edit-context-inject.ps1 — dedup correctness + cache replay
# --------------------------------------------------------------------------


def test_pre_edit_context_inject_ps1_has_dedup_filter() -> None:
    """The Filter-Seen function dedupes KG/codegraph blocks by title.
    Asserts the function exists and uses the "KG:|CODE:" header regex
    that PR #186 introduced for hook-format compatibility.
    """
    body = _read("pre-edit-context-inject", ".ps1")
    assert "function Filter-Seen" in body, (
        "pre-edit-context-inject.ps1 missing Filter-Seen function — "
        "session-level dedup of injected KG/codegraph nodes broken."
    )
    # The PR #186 regex: ^(KG|CODE):\s+(.+)$
    assert "(KG|CODE):" in body, (
        "pre-edit-context-inject.ps1 missing the (KG|CODE): header regex "
        "— hook-format dedup won't match producer output."
    )


def test_pre_edit_context_inject_ps1_caches_raw_pre_dedup() -> None:
    """PR #186 fix #3: cache stores RAW per-result blocks (pre-dedup) so
    replays apply CURRENT seen-list state. Caching post-dedup output
    would perma-suppress titles eligible to re-appear after /compact.
    """
    body = _read("pre-edit-context-inject", ".ps1")
    assert "$KgRaw" in body and "$CodeRaw" in body, (
        "pre-edit-context-inject.ps1 missing $KgRaw/$CodeRaw raw-cache "
        "captures — replays won't apply current dedup state."
    )


def test_pre_edit_context_inject_ps1_cache_replay_runs_dedup() -> None:
    """Cache hit path must re-run Filter-Seen on the cached blob so
    already-shown titles stay suppressed across edits within the TTL.
    """
    body = _read("pre-edit-context-inject", ".ps1")
    # Find the CacheHit branch and look for Filter-Seen invocation inside it.
    cache_hit_idx = body.find("$CacheHit")
    filter_call_after_cache = body.find("Filter-Seen $CacheBlob")
    assert cache_hit_idx > 0, (
        "pre-edit-context-inject.ps1 missing $CacheHit branch."
    )
    assert filter_call_after_cache > 0, (
        "pre-edit-context-inject.ps1 cache-replay branch must call "
        "Filter-Seen on the cached blob — dedup state would otherwise "
        "be ignored on cache hits."
    )


def test_pre_edit_context_inject_ps1_filters_whitespace_only_pre_amble() -> None:
    """PR #186 fix #2: blank/separator lines in the input must NOT pass
    through the `else` branch, otherwise HAS_KG reads whitespace as
    truthy and an empty system-reminder block surfaces to the LLM.
    """
    body = _read("pre-edit-context-inject", ".ps1")
    # The fix is `if ($line -match '\S')` inside the else branch.
    assert "-match '\\S'" in body or "-match \"\\S\"" in body, (
        "pre-edit-context-inject.ps1 Filter-Seen must gate the else-branch "
        "pass-through on '\\S' (non-whitespace) — otherwise blank "
        "separators leak through and trigger empty system-reminder blocks."
    )


# --------------------------------------------------------------------------
# check-no-fork-bomb.ps1 — env-scrub + fork-bomb detection
# --------------------------------------------------------------------------


def test_check_no_fork_bomb_ps1_scrubs_secrets_before_work() -> None:
    """PR-32 port: the .ps1 must scrub sensitive env vars before any
    subprocess spawning, matching the .sh sibling (line 33). Defence-in-
    depth — same contract that test_hooks_disable_guard.py enforces on
    the .sh side.
    """
    body = _read("check-no-fork-bomb", ".ps1")
    required_secrets = [
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "VERCEL_TOKEN",
    ]
    for secret in required_secrets:
        assert secret in body, (
            f"check-no-fork-bomb.ps1 must scrub {secret!r} from the "
            f"environment before any subprocess spawning."
        )
    # The scrub must happen before VCT_DISABLE_HOOKS guard (which itself
    # must happen before any real work — Stop-Process, Get-Process, etc.).
    scrub_idx = body.find("Remove-Item")
    disable_idx = body.find("VCT_DISABLE_HOOKS")
    work_idx = body.find("Get-Process")
    assert scrub_idx > 0 and disable_idx > 0 and work_idx > 0
    assert scrub_idx < disable_idx < work_idx, (
        "check-no-fork-bomb.ps1 ordering: env-scrub → VCT_DISABLE_HOOKS → "
        f"work. Got scrub={scrub_idx}, disable={disable_idx}, "
        f"work={work_idx}."
    )


def test_check_no_fork_bomb_ps1_detects_and_kills_lean_ctx() -> None:
    """Body parity: the .ps1 must (1) count lean-ctx processes, (2)
    compare against the threshold, (3) Stop-Process -Force above the
    threshold. PowerShell-idiomatic equivalents of `pgrep -x` + `pkill`.
    """
    body = _read("check-no-fork-bomb", ".ps1")
    assert "Get-Process -Name lean-ctx" in body, (
        "check-no-fork-bomb.ps1 missing Get-Process -Name lean-ctx — "
        "fork-bomb count step absent."
    )
    assert "Stop-Process" in body and "-Force" in body, (
        "check-no-fork-bomb.ps1 missing Stop-Process -Force — fork-bomb "
        "kill step absent."
    )
    assert "LEAN_CTX_FORK_BOMB_THRESHOLD" in body, (
        "check-no-fork-bomb.ps1 missing the LEAN_CTX_FORK_BOMB_THRESHOLD "
        "override env var — threshold not configurable for testing."
    )


# --------------------------------------------------------------------------
# pre-vercel-token-guard.ps1 — token leak prevention
# --------------------------------------------------------------------------


def test_pre_vercel_token_guard_ps1_blocks_token_flag() -> None:
    """Body parity: detect `vercel ... --token=...` invocations and
    block them with exit 2 + explanatory stderr.
    """
    body = _read("pre-vercel-token-guard", ".ps1")
    # Match both the vercel binary and the --token flag patterns.
    assert "vercel" in body and "--token" in body, (
        "pre-vercel-token-guard.ps1 missing vercel/--token detection."
    )
    assert "exit 2" in body, (
        "pre-vercel-token-guard.ps1 must exit 2 to block the tool call — "
        "exit 0 would let the leak through."
    )


def test_pre_vercel_token_guard_ps1_only_fires_on_bash_tool() -> None:
    """The guard must only inspect Bash tool invocations (Edit/Write
    don't have a `command` field; running the regex against arbitrary
    file content would produce spurious blocks).
    """
    body = _read("pre-vercel-token-guard", ".ps1")
    assert '$ToolName -ne "Bash"' in body or "ne 'Bash'" in body, (
        "pre-vercel-token-guard.ps1 must early-exit when tool_name != 'Bash'."
    )


def test_pre_vercel_token_guard_ps1_scrubs_secrets() -> None:
    """Defence-in-depth env-scrub — matches .sh line 4."""
    body = _read("pre-vercel-token-guard", ".ps1")
    for secret in ("VERCEL_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        assert secret in body, (
            f"pre-vercel-token-guard.ps1 must scrub {secret!r} from env."
        )


# --------------------------------------------------------------------------
# Cross-file: .claude/ mirrors must match templates/ byte-for-byte
# --------------------------------------------------------------------------


# PR-39 (v0.2.12, 2026-05-16): the former
# `test_ps1_template_and_claude_mirror_are_identical` parametric test was
# removed alongside the .claude/hooks/ duplicate. There's nothing to drift
# FROM — templates is the single source of truth, install.py renders
# .claude/ from it at install time.
