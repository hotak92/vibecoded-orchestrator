# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-OS cache-layer parity for pre-edit-context-inject hook (PR-38, v0.2.12).

The .ps1 sibling has had a working KG-result cache layer since 2026-05-09
(commit 93346c9): file-based, TTL-gated, with dedup re-applied on replay so
nodes seen since the cache was written get filtered out rather than being
perma-baked into the cached blob.

The .sh sibling never had it. PR-35 (commit 0cbdbcc on integration/v0.2.12)
removed dead cache-replay code in .sh that referenced never-set
$CACHE_HIT / $CACHE_BLOB variables, confirming the asymmetry.

PR-38 (this work) ports the .ps1 cache layer to .sh so Linux/macOS users
get the same perf benefit Windows users have had: back-to-back edits in
the same area don't re-run expensive KG/code-graph queries when the input
hash matches a cached blob within TTL.

These tests assert PRESENCE of the four ported sections by looking for
fingerprint strings (text-based, not exact-string matches — same style as
tests/test_hook_ps1_body_parity.py).

Hard constraints honored:
  - No subprocess execution of the hook itself (it depends on Python venv +
    network-attached Weaviate + RL server). Tests are static body-parity
    assertions, mirroring the .ps1-side test discipline in
    test_hook_ps1_body_parity.py.
  - Drift gate is enforced separately (.github/scripts/check_template_drift.py)
    so templates/ and .claude/ stay byte-identical.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_HOOKS = REPO_ROOT / "templates" / "hooks"
CLAUDE_HOOKS = REPO_ROOT / ".claude" / "hooks"


def _read(name: str, suffix: str, src: str = "templates") -> str:
    base = TEMPLATES_HOOKS if src == "templates" else CLAUDE_HOOKS
    return (base / f"{name}{suffix}").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Section 1: cache file path + TTL setup
# --------------------------------------------------------------------------


def test_pre_edit_sh_defines_cache_file_path() -> None:
    """CACHE_FILE must be computed as $CACHE_BASE/$FILE_HASH (per-file
    cache key). Without this every edit would share one cache entry.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert 'CACHE_FILE="$CACHE_DIR/$FILE_HASH"' in body, (
        "pre-edit-context-inject.sh missing CACHE_FILE=$CACHE_DIR/$FILE_HASH "
        "— per-file cache key broken (every file would share one entry)."
    )
    assert "CACHE_TTL=600" in body, (
        "pre-edit-context-inject.sh missing CACHE_TTL=600 — matches the "
        ".ps1 sibling's 10-minute TTL constant."
    )


def test_pre_edit_sh_creates_cache_dir_idempotently() -> None:
    """mkdir -p $CACHE_DIR must be present and soft-fail (|| true) so a
    read-only TMPDIR doesn't crash the hook.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert 'mkdir -p "$CACHE_DIR"' in body, (
        "pre-edit-context-inject.sh missing mkdir -p $CACHE_DIR — cache "
        "writes would fail on first-run when the dir doesn't exist."
    )


# --------------------------------------------------------------------------
# Section 2: cache read with TTL + CACHE_HIT/CACHE_BLOB capture
# --------------------------------------------------------------------------


def test_pre_edit_sh_sets_cache_hit_and_cache_blob() -> None:
    """PR-38 port: the .sh must SET $CACHE_HIT and $CACHE_BLOB on a cache
    hit (this is what PR-35 confirmed was MISSING — the old branch read
    them without anyone ever setting them).
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert "CACHE_HIT=1" in body, (
        "pre-edit-context-inject.sh missing CACHE_HIT=1 assignment — cache "
        "replay branch will never fire. This is the bug PR-35 surfaced and "
        "PR-38 is fixing."
    )
    assert 'CACHE_BLOB=$(cat "$CACHE_FILE"' in body, (
        "pre-edit-context-inject.sh missing CACHE_BLOB=$(cat $CACHE_FILE) "
        "— cache contents never captured for replay."
    )


def test_pre_edit_sh_uses_cross_os_stat_for_mtime() -> None:
    """Cache mtime must work on both Linux (GNU coreutils `stat -c %Y`)
    and macOS (BSD `stat -f %m`). Without the fallback every macOS install
    silently treats the cache as expired and never gets a hit.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert "stat -c '%Y'" in body, (
        "pre-edit-context-inject.sh missing GNU stat -c '%Y' for cache "
        "mtime on Linux."
    )
    assert "stat -f '%m'" in body, (
        "pre-edit-context-inject.sh missing BSD stat -f '%m' for cache "
        "mtime on macOS — Mac users would never see a cache hit."
    )


def test_pre_edit_sh_compares_age_against_ttl() -> None:
    """The cache hit must gate on FILE_AGE < CACHE_TTL, not just on file
    existence. Otherwise stale caches replay forever.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert 'FILE_AGE=$(( $(date +%s) - CACHE_MTIME ))' in body, (
        "pre-edit-context-inject.sh missing FILE_AGE computation — cache "
        "TTL check inert."
    )
    assert '"$FILE_AGE" -lt "$CACHE_TTL"' in body, (
        "pre-edit-context-inject.sh missing FILE_AGE -lt CACHE_TTL gate — "
        "stale cache entries would replay past their TTL."
    )


# --------------------------------------------------------------------------
# Section 3: cache replay branch that re-runs dedup
# --------------------------------------------------------------------------


def test_pre_edit_sh_cache_replay_runs_dedup() -> None:
    """Parity with .ps1's `if ($CacheHit) { $filteredCache = Filter-Seen ...}`:
    the .sh cache-hit branch must call _filter_seen on $CACHE_BLOB so
    titles seen since the cache was written get suppressed on replay.
    """
    body = _read("pre-edit-context-inject", ".sh")
    cache_hit_idx = body.find('"$CACHE_HIT" == "1"')
    filter_call_after = body.find('_filter_seen "$CACHE_BLOB"')
    assert cache_hit_idx > 0, (
        "pre-edit-context-inject.sh missing CACHE_HIT == 1 branch — "
        "cache layer not ported."
    )
    assert filter_call_after > 0, (
        "pre-edit-context-inject.sh cache-replay branch must call "
        '_filter_seen on $CACHE_BLOB — dedup state would be ignored on '
        "cache hits and already-seen nodes would re-leak to the LLM."
    )
    assert cache_hit_idx < filter_call_after or _filter_seen_defined_before(body, cache_hit_idx), (
        "pre-edit-context-inject.sh: CACHE_HIT branch must come AFTER "
        "_filter_seen is defined (the function is referenced inside the "
        "branch)."
    )


def _filter_seen_defined_before(body: str, idx: int) -> bool:
    """True if the `_filter_seen()` function definition appears before
    character offset `idx` in the body. Used to confirm the cache-replay
    branch can call the function.
    """
    return body.find("_filter_seen() {") < idx and body.find("_filter_seen() {") > 0


def test_pre_edit_sh_filter_seen_is_block_atomic() -> None:
    """CRITICAL invariant (.ps1 parity): a KG/CODE result is an ATOMIC
    block — header line + body lines. Dedup must suppress the WHOLE
    block (if title is seen) or emit the WHOLE block (if not).
    Line-by-line filtering would leak orphan body fragments.

    Asserts the .sh _filter_seen function uses the block accumulator
    pattern (current_title / current_block / _flush_block) rather than
    a simpler line-by-line filter.

    The cache-replay branch added in PR-38 reuses this same function
    on $CACHE_BLOB, so cached blocks stay atomic across replays.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert 'current_title=""' in body, (
        "pre-edit-context-inject.sh _filter_seen must use a "
        "current_title accumulator — line-by-line filtering would leak "
        "orphan body fragments."
    )
    assert 'current_block=""' in body, (
        "pre-edit-context-inject.sh _filter_seen must use a "
        "current_block accumulator — blocks must be flushed atomically."
    )
    assert "_flush_block()" in body, (
        "pre-edit-context-inject.sh _filter_seen must use a _flush_block "
        "helper — blocks must be flushed atomically at boundaries."
    )
    # The header-line regex must match the .ps1 sibling (KG|CODE):
    assert "^(KG|CODE):" in body, (
        "pre-edit-context-inject.sh _filter_seen missing the (KG|CODE): "
        "header regex — block boundary detection broken."
    )


def test_pre_edit_sh_cache_replay_silent_when_everything_seen() -> None:
    """If _filter_seen returns whitespace-only output (all titles
    already shown), the replay branch must `exit 0` silently rather
    than emitting an empty `[Pre-edit context for ...]:` block.
    """
    body = _read("pre-edit-context-inject", ".sh")
    # The .ps1 uses `if (-not $trimmed) { exit 0 }`. The .sh uses a `case`
    # statement on `*[![:space:]]*)` to detect non-whitespace. Either pattern
    # is acceptable; the goal is to assert SOMETHING checks for the
    # whitespace-only case in the cache-replay branch.
    cache_hit_idx = body.find('"$CACHE_HIT" == "1"')
    # Look within ~30 lines of the CACHE_HIT branch for the whitespace check.
    snippet = body[cache_hit_idx:cache_hit_idx + 2000] if cache_hit_idx > 0 else ""
    has_whitespace_check = (
        "*[![:space:]]*" in snippet
        or "[^[:space:]]" in snippet
        or "-z " in snippet  # alternative idiom: test FILTERED is empty
    )
    assert has_whitespace_check, (
        "pre-edit-context-inject.sh cache-replay branch must check for "
        "whitespace-only filtered output (all titles seen → silent exit) — "
        "otherwise an empty context block leaks to the LLM."
    )


# --------------------------------------------------------------------------
# Section 4: cache write at end of live-path (RAW, pre-dedup)
# --------------------------------------------------------------------------


def test_pre_edit_sh_writes_raw_cache_at_end_of_live_path() -> None:
    """The cache file must store RAW per-result blocks (pre-dedup) so
    replays apply CURRENT seen-list state. Caching post-dedup would
    perma-suppress titles legitimately re-eligible after /compact.
    """
    body = _read("pre-edit-context-inject", ".sh")
    assert 'KG_RAW="$KG_RESULT"' in body, (
        "pre-edit-context-inject.sh missing KG_RAW capture (pre-dedup) — "
        "cache would store post-dedup output and perma-suppress titles."
    )
    assert 'CODE_RAW="$CODE_RESULT"' in body, (
        "pre-edit-context-inject.sh missing CODE_RAW capture (pre-dedup)."
    )
    # The cache write itself (`echo "$RAW_CACHE" > "$CACHE_FILE"`) must be
    # present in BOTH the empty-output branch and the emit branch.
    write_count = body.count('"$RAW_CACHE" > "$CACHE_FILE"')
    assert write_count >= 2, (
        "pre-edit-context-inject.sh must write RAW_CACHE to CACHE_FILE in "
        "both the empty-output exit-early branch AND the emit branch — "
        f"found {write_count} sites, need 2."
    )


def test_pre_edit_sh_cache_write_is_soft_fail() -> None:
    """A failed cache write (e.g. disk full) must not crash the hook — it
    should still emit context. Mirrors the .ps1's `try { Set-Content ... }
    catch { }` pattern.
    """
    body = _read("pre-edit-context-inject", ".sh")
    # The .sh idiom is `... > "$CACHE_FILE" 2>/dev/null || true`. Either
    # `|| true` or stderr-redirect counts.
    assert '> "$CACHE_FILE" 2>/dev/null || true' in body, (
        "pre-edit-context-inject.sh cache write must be soft-fail "
        "(2>/dev/null || true) — otherwise a read-only TMPDIR crashes "
        "the hook and the edit gets blocked."
    )


# --------------------------------------------------------------------------
# Cross-mirror parity: templates/ == .claude/ byte-identical
# --------------------------------------------------------------------------


def test_pre_edit_sh_templates_matches_claude_mirror() -> None:
    """Drift gate sanity: templates/hooks/pre-edit-context-inject.sh and
    .claude/hooks/pre-edit-context-inject.sh must be byte-identical.
    Defended in CI by .github/scripts/check_template_drift.py but
    re-asserted here so a developer test run catches drift before push.
    """
    templates_body = _read("pre-edit-context-inject", ".sh", src="templates")
    claude_body = _read("pre-edit-context-inject", ".sh", src="claude")
    assert templates_body == claude_body, (
        "pre-edit-context-inject.sh DRIFT: templates/hooks/ and "
        ".claude/hooks/ copies are not byte-identical. Run "
        "`cp templates/hooks/pre-edit-context-inject.sh "
        ".claude/hooks/pre-edit-context-inject.sh` to resync."
    )
