# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the VCT_DISABLE_HOOKS escape hatch (commit 4221d6e).

Every shell hook under .claude/hooks/ must:
  1. Scrub sensitive environment variables FIRST (line ~2).
  2. Honour VCT_DISABLE_HOOKS=1 by exiting 0 immediately, BEFORE doing
     any real work (subprocess spawning, file IO, network).

This is a defence-in-depth contract: it lets developers run Claude Code
inside the orchestrator repo without our hooks firing on every tool call,
while still guaranteeing that any partial run cannot leak secrets to a
spawned subprocess.

We exercise the contract by:
  * statically asserting placement (env-scrub before guard before work)
  * dynamically running each hook with VCT_DISABLE_HOOKS=1 and asserting
    a fast, clean exit with no stdout/stderr noise.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# PR-39 (v0.2.12, 2026-05-16): templates/hooks/ is the single source of truth.
# .claude/hooks/ used to be a byte-identical mirror shipped in the repo; it's
# now rendered from templates/ at install time by install.py.
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"

# Hooks that legitimately produce stdout even when short-circuiting (e.g.
# session-start banners). Empty for now — populate if a hook fails the
# silence assertion for a documented reason.
ALLOW_STDOUT: set[str] = set()


def _hook_files() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.sh"))


def test_hooks_directory_exists() -> None:
    assert HOOKS_DIR.is_dir(), f"Expected {HOOKS_DIR} to exist"
    assert _hook_files(), "Expected at least one *.sh hook"


@pytest.mark.parametrize("hook_path", _hook_files(), ids=lambda p: p.name)
def test_guard_present_after_scrub_before_work(hook_path: Path) -> None:
    """Every hook must scrub env first, then check VCT_DISABLE_HOOKS, then work.

    Static check on file content — guards against future hooks being
    added without the escape hatch (or with the guard misplaced before
    env-scrub, which would leak secrets on disabled runs).
    """
    text = hook_path.read_text()
    lines = text.splitlines()

    scrub_idx = next(
        (i for i, line in enumerate(lines) if "unset" in line and "GITHUB_TOKEN" in line),
        None,
    )
    guard_idx = next(
        (i for i, line in enumerate(lines) if "VCT_DISABLE_HOOKS" in line and "exit 0" in line),
        None,
    )

    assert scrub_idx is not None, f"{hook_path.name}: missing env-scrub line"
    assert guard_idx is not None, f"{hook_path.name}: missing VCT_DISABLE_HOOKS guard"
    assert scrub_idx < guard_idx, (
        f"{hook_path.name}: VCT_DISABLE_HOOKS guard at line {guard_idx + 1} "
        f"appears BEFORE env-scrub at line {scrub_idx + 1}; this would leak "
        f"secrets when the guard is disabled mid-run."
    )

    # Guard must be near the top — within the first 10 non-blank, non-comment
    # lines after the shebang. Otherwise it's "after work" and defeats the
    # purpose.
    code_lines_before_guard = [
        ln for ln in lines[:guard_idx] if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(code_lines_before_guard) <= 3, (
        f"{hook_path.name}: guard appears after {len(code_lines_before_guard)} "
        f"code lines; should be within the first 3 (after shebang + scrub)."
    )


def _ps1_hook_files() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.ps1"))


@pytest.mark.parametrize(
    "hook_path", _ps1_hook_files(), ids=lambda p: p.name,
)
def test_ps1_scrub_before_guard(hook_path: Path) -> None:
    """v0.2.74 (M-2): the .sh scrub-order static check globbed only *.sh, so a
    PowerShell hook could reorder the secret-scrub AFTER the VCT_DISABLE_HOOKS
    guard (a leak on disabled runs) with nothing catching it. This mirrors the
    .sh check for the PowerShell idiom: a `$env:GITHUB_TOKEN` scrub (Remove-Item
    Env: or `$env:...=$null`) must precede the `$env:VCT_DISABLE_HOOKS` guard.
    Only asserts on .ps1 hooks that HAVE both a scrub + a guard (a hook with
    neither is out of scope)."""
    lines = hook_path.read_text(encoding="utf-8-sig").splitlines()
    scrub_idx = next(
        (i for i, ln in enumerate(lines)
         if "GITHUB_TOKEN" in ln and (
             "Remove-Item" in ln or "Env:" in ln or "$env:" in ln.lower()
             or "'GITHUB_TOKEN'" in ln or '"GITHUB_TOKEN"' in ln)),
        None,
    )
    guard_idx = next(
        (i for i, ln in enumerate(lines)
         if "VCT_DISABLE_HOOKS" in ln and ("exit 0" in ln or "exit0" in ln.replace(" ", ""))),
        None,
    )
    if scrub_idx is None or guard_idx is None:
        pytest.skip(f"{hook_path.name}: no scrub+guard pair to order-check")
    assert scrub_idx < guard_idx, (
        f"{hook_path.name}: VCT_DISABLE_HOOKS guard at line {guard_idx + 1} "
        f"appears BEFORE the secret env-scrub at line {scrub_idx + 1}; this "
        f"would leak secrets when the guard is disabled mid-run (.ps1 parity)."
    )


@pytest.mark.parametrize("hook_path", _hook_files(), ids=lambda p: p.name)
def test_guard_short_circuits_fast(hook_path: Path, tmp_path: Path) -> None:
    """With VCT_DISABLE_HOOKS=1, hook exits 0 quickly and quietly.

    "Quickly" = under 1.5s wall-clock (generous for cold disk + fork).
    "Quietly" = no stdout (stderr allowed: shell may print the trace if
    `set -x` is leaking, but we don't enforce stderr silence — too brittle
    across `set -e -u` differences).
    """
    env = {
        "VCT_DISABLE_HOOKS": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }

    start = time.monotonic()
    result = subprocess.run(
        ["bash", str(hook_path), "noop_tool", "", "{}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(tmp_path),
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"{hook_path.name}: exit code {result.returncode} with "
        f"VCT_DISABLE_HOOKS=1 (expected 0)\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert elapsed < 1.5, (
        f"{hook_path.name}: took {elapsed:.2f}s with VCT_DISABLE_HOOKS=1; "
        f"guard should short-circuit before any real work."
    )

    if hook_path.name not in ALLOW_STDOUT:
        # Strip ANSI/whitespace before asserting silence.
        clean_stdout = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).strip()
        assert clean_stdout == "", (
            f"{hook_path.name}: produced stdout with VCT_DISABLE_HOOKS=1:\n"
            f"{clean_stdout!r}"
        )


@pytest.mark.parametrize("hook_path", _hook_files(), ids=lambda p: p.name)
def test_hook_parses_cleanly(hook_path: Path) -> None:
    """`bash -n` must succeed for every hook (no syntax errors)."""
    result = subprocess.run(
        ["/bin/bash", "-n", str(hook_path)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, (
        f"{hook_path.name}: bash -n failed:\n{result.stderr}"
    )


@pytest.mark.parametrize("hook_path", _hook_files(), ids=lambda p: p.name)
def test_hook_uses_portable_tmpdir(hook_path: Path) -> None:
    """Regression for the cross-OS fix (commit ac30e5b).

    Hooks that write temp files must NOT hardcode `/tmp/...`. They should
    use `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}` (or a similar portable
    pattern) so macOS / sandboxed Linux installs work.

    We allow `/tmp` to appear inside a parameter expansion default
    (`${VAR:-/tmp}`) but forbid it as a literal prefix in real paths.
    """
    text = hook_path.read_text()
    # Strip lines that are comments or inside `${...:-/tmp}` expansions.
    suspicious: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Look for literal `/tmp/` not inside a `:-/tmp` default.
        # Crude but effective: remove `:-/tmp` and `:-/tmp/...` patterns first.
        cleaned = re.sub(r":-/tmp(/[^}]*)?", ":-PLACEHOLDER", line)
        # Also tolerate /tmp inside comments at end of line
        cleaned = cleaned.split("#", 1)[0]
        if re.search(r"(^|[\s=\"'`(])/tmp/", cleaned):
            suspicious.append(f"  line {i}: {line.rstrip()}")

    assert not suspicious, (
        f"{hook_path.name}: hardcoded /tmp/ paths found (use "
        f"${{TMPDIR:-${{XDG_RUNTIME_DIR:-/tmp}}}} instead):\n"
        + "\n".join(suspicious)
    )


def test_all_hooks_have_guard_count_matches() -> None:
    """Sanity check: count of hooks with the guard equals total hook count.

    Equivalent to the manual `grep -l VCT_DISABLE_HOOKS .claude/hooks/*.sh
    | wc -l` audit, but enforced in CI.
    """
    hooks = _hook_files()
    with_guard = [h for h in hooks if "VCT_DISABLE_HOOKS" in h.read_text()]
    assert len(with_guard) == len(hooks), (
        f"Hooks missing VCT_DISABLE_HOOKS guard: "
        f"{sorted(set(h.name for h in hooks) - set(h.name for h in with_guard))}"
    )


# ---------------------------------------------------------------------------
# v0.2.75 (audit E2.1 / F2.2): bounded SEMANTIC parity across .sh/.ps1 pairs.
#
# The CI hook-parity gate (`.github/scripts/check_hook_parity.py`) checks only
# that every `.sh` has a `.ps1` SIBLING — existence, not behaviour. E2.1 asked
# whether we can cheaply also assert the pair does the SAME conceptual work.
#
# What we CAN assert cheaply and reliably (implemented below): every sibling
# PAIR honours the same disable-guard token — if the `.sh` has a
# `VCT_DISABLE_HOOKS` guard, its `.ps1` must too (and vice versa). This is the
# highest-value structural invariant (a `.ps1` missing the guard would keep
# firing on `VCT_DISABLE_HOOKS=1` runs while the `.sh` short-circuits) and it
# is noise-free.
#
# What we deliberately do NOT force (the "(b)" option from the brief —
# per-hook env-var-name intersection): empirically too noisy across the 45
# real pairs. Two independent probes at v0.2.75:
#   * Broad env-name extraction ([A-Z_]{4,} referenced via $VAR / $env:VAR)
#     surfaced 250+ names present on ONE side only — but the overwhelming
#     majority are LOCAL shell variables (SCRIPT_DIR, HOOK_STDIN, PARSED, …)
#     whose naming idioms legitimately differ between bash and PowerShell,
#     plus OS-specific env (bash HOME/TMPDIR vs PowerShell USERPROFILE/TEMP/
#     LOCALAPPDATA). None of those are real cross-language contract drift.
#   * Even scoped to genuine external knobs (VCT_/VCO_/CLAUDE_/LEAN_CTX_
#     prefixes) the sides diverge for GOOD reasons: the bash hooks read
#     `VCO_VENV_PYTHON` where the PowerShell siblings read `VCT_VENV` /
#     `VCT_PYTHON` (same concept, per-language name), and container knobs
#     (`VCO_WEAVIATE_CONTAINER`, …) are read only by the bash container hook.
# A per-var-name assertion would therefore need a large, brittle allowlist
# that documents the divergence rather than catching bugs — negative value.
# So we keep (a) and record the (b) noise profile here instead of forcing it.
# ---------------------------------------------------------------------------


def _sibling_pairs() -> list[tuple[Path, Path]]:
    """All (`.sh`, `.ps1`) sibling pairs under templates/hooks/.

    Only pairs where BOTH files exist are returned — the existence gate is
    the CI script's job; this test focuses on behavioural parity of the
    pairs that are supposed to mirror each other.
    """
    out: list[tuple[Path, Path]] = []
    for sh in _hook_files():
        ps1 = sh.with_suffix(".ps1")
        if ps1.exists():
            out.append((sh, ps1))
    return out


def test_sibling_pairs_exist() -> None:
    """At least a substantial set of pairs must exist, so a globbing typo
    that silently drops every pair can't make the parity test vacuously
    pass."""
    pairs = _sibling_pairs()
    assert len(pairs) >= 20, (
        f"expected many .sh/.ps1 sibling pairs under {HOOKS_DIR}, "
        f"found {len(pairs)} — glob or directory layout changed?"
    )


@pytest.mark.parametrize(
    "sh_path,ps1_path",
    _sibling_pairs(),
    ids=lambda p: p.name,
)
def test_sibling_pair_shares_disable_guard(sh_path: Path, ps1_path: Path) -> None:
    """(a) Bounded semantic parity: a sibling pair must agree on whether it
    carries the `VCT_DISABLE_HOOKS` disable-guard.

    If the `.sh` guards on `VCT_DISABLE_HOOKS` (the norm — enforced hook-wide
    by `test_all_hooks_have_guard_count_matches`), the `.ps1` MUST too, else the
    Windows path keeps firing on disabled runs. The check is symmetric so a
    future guard-less `.sh` paired with a guarded `.ps1` also trips.
    """
    sh_text = sh_path.read_text(encoding="utf-8")
    ps1_text = ps1_path.read_text(encoding="utf-8-sig")
    sh_has = "VCT_DISABLE_HOOKS" in sh_text
    ps1_has = "VCT_DISABLE_HOOKS" in ps1_text
    assert sh_has == ps1_has, (
        f"{sh_path.name} / {ps1_path.name}: disable-guard mismatch — "
        f".sh has VCT_DISABLE_HOOKS={sh_has}, .ps1 has={ps1_has}. Both "
        f"siblings must honour the same disable-guard so a "
        f"VCT_DISABLE_HOOKS=1 run short-circuits on every OS."
    )
