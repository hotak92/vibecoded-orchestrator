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
  - PR-39 (v0.2.12, 2026-05-16) removed the .claude/ ↔ templates/ duplication
    that the former check_template_drift.py gate enforced. templates/hooks/ is
    now the single source of truth; install.py renders .claude/hooks/ from it
    at install time. The cross-mirror parity test at the end of this file was
    deleted alongside the gate.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_HOOKS = REPO_ROOT / "templates" / "hooks"


def _read(name: str, suffix: str, src: str = "templates") -> str:
    # PR-39: src parameter retained for API stability; "claude" is no longer
    # a meaningful source (templates is the only source of truth).
    if src != "templates":
        raise ValueError(
            f"unsupported src={src!r}; only 'templates' is valid post-PR-39"
        )
    return (TEMPLATES_HOOKS / f"{name}{suffix}").read_text(encoding="utf-8")


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
    # P3 (v0.2.91): the per-file TTL is no longer a hardcoded 600 — it is
    # DERIVED from the shared query cache's default (900) with the same
    # VCO_QUERY_CACHE_TTL override, so the two caches can never drift into the
    # 600-900 s "double miss" window again. RED-PROOF: this assertion fails on
    # the pre-P3 source (which carried `CACHE_TTL=600`).
    # (v0.2.101) the retired _lib/query-cache.sh's
    # _VCO_QUERY_CACHE_TTL_DEFAULT middle rung is gone with the lib:
    # VCO_QUERY_CACHE_TTL is the ONE override (this replay cache + the router).
    assert 'CACHE_TTL="${VCO_QUERY_CACHE_TTL:-900}"' in body, (
        "pre-edit-context-inject.sh must derive CACHE_TTL from the shared "
        "query-cache default (900 s) with VCO_QUERY_CACHE_TTL as the one "
        "override — a hardcoded 600 re-opens the P3 double-miss window."
    )
    # The RUNG must be gone from the expression — a prose retirement note
    # naming the old marker is fine (established style); a second default
    # source is not.
    assert "${_VCO_QUERY_CACHE_TTL_DEFAULT:-900}" not in body, (
        "pre-edit-context-inject.sh still carries the retired "
        "_VCO_QUERY_CACHE_TTL_DEFAULT middle rung — its reader "
        "(_lib/query-cache.sh) was deleted in v0.2.101."
    )
    assert "CACHE_TTL=600" not in body, (
        "pre-edit-context-inject.sh still carries the pre-P3 hardcoded "
        "CACHE_TTL=600."
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
    """Parity with .ps1's `if ($CacheHit) { Invoke-VcoFilterSeenBlocks ...}`:
    the .sh cache-hit branch must run the SHARED seen-store filter on
    $CACHE_BLOB so titles seen since the cache was written get suppressed
    on replay. v0.2.101: the hook's inline _filter_seen delegator was
    retired with the wrapper rework — the replay calls the one home
    (vco_filter_seen_blocks from _lib/seen-store.sh) directly, which the
    hook must therefore SOURCE before the branch runs."""
    body = _read("pre-edit-context-inject", ".sh")
    cache_hit_idx = body.find('"$CACHE_HIT" == "1"')
    filter_call_after = body.find('vco_filter_seen_blocks "$CACHE_BLOB"')
    source_idx = body.find('_lib/seen-store.sh')
    assert cache_hit_idx > 0, (
        "pre-edit-context-inject.sh missing CACHE_HIT == 1 branch — "
        "cache layer not ported."
    )
    assert filter_call_after > 0, (
        "pre-edit-context-inject.sh cache-replay branch must call the "
        'shared vco_filter_seen_blocks on $CACHE_BLOB — dedup state would '
        "be ignored on cache hits and already-seen nodes would re-leak."
    )
    assert 0 < source_idx < cache_hit_idx, (
        "the seen-store lib must be sourced BEFORE the cache-hit branch "
        "that calls vco_filter_seen_blocks."
    )


def test_pre_edit_sh_filter_seen_is_block_atomic() -> None:
    """CRITICAL invariant (.ps1 parity): a KG/CODE result is an ATOMIC
    block — header line + body lines. Dedup must suppress the WHOLE
    block (if title is seen) or emit the WHOLE block (if not).
    Line-by-line filtering would leak orphan body fragments.

    v0.2.101 retarget: the block accumulator no longer lives in the hook —
    the wrapper replay calls the SHARED home (_lib/seen-store.sh's
    vco_filter_seen_blocks) and the router's Python filter mirrors it
    (byte-compatible keys, pinned by
    tests/test_v02101_inject_gates.py::TestSeenStoreParity). This row now
    pins the block-atomic pattern in that shared home, which BOTH the
    replay path and (via the parity pin) the router path must keep."""
    body = _read("pre-edit-context-inject", ".sh")
    assert "_lib/seen-store.sh" in body, (
        "the wrapper must source the shared seen-store home for its replay "
        "filter"
    )
    lib = (Path(__file__).resolve().parent.parent / "templates" / "hooks"
           / "_lib" / "seen-store.sh").read_text(encoding="utf-8")
    assert 'cur_first=""' in lib and 'cur_block=""' in lib, (
        "seen-store.sh must use title/block accumulators — line-by-line "
        "filtering would leak orphan body fragments."
    )
    assert "_vco_flush()" in lib, (
        "seen-store.sh must flush blocks atomically at boundaries."
    )
    assert "^(KG|CODE):" in lib, (
        "seen-store.sh missing the (KG|CODE): header regex — block "
        "boundary detection broken."
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
    # Look from the CACHE_HIT branch to the END of the cache-replay section.
    # (P2 re-review nit 1: a fixed +2000 window left only ~271 chars of
    # headroom and already broke once when a comment grew — scan generously
    # to the next section boundary instead of a brittle char count.)
    if cache_hit_idx > 0:
        nxt = body.find("# ---", cache_hit_idx + 1)
        snippet = body[cache_hit_idx:nxt if nxt > 0 else len(body)]
    else:
        snippet = ""
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
    """The cache file must store the RAW router output (pre-REPLAY-dedup)
    so replays apply CURRENT seen-list state — caching what the replay
    already filtered would perma-suppress titles legitimately re-eligible
    after /compact. v0.2.101 shape: ONE write site at the end of the miss
    path (`printf … "$INJECT" > "$CACHE_FILE"`), gated on non-whitespace
    output so an EMPTY result is never cached (§9 discipline — the old
    dual write site existed because the pre-router hook had to reassemble
    KG_RAW/CODE_RAW itself; the router's stdout IS the raw blob now)."""
    body = _read("pre-edit-context-inject", ".sh")
    assert '"$INJECT" > "$CACHE_FILE"' in body, (
        "pre-edit-context-inject.sh must cache the router's RAW stdout — "
        "the replay path depends on pre-dedup content."
    )
    miss_idx = body.find('"$VENV" "$ROUTER" edit')
    write_idx = body.find('"$INJECT" > "$CACHE_FILE"')
    assert 0 < miss_idx < write_idx, (
        "the cache write must sit at the END of the live (miss) path, "
        "after the router run."
    )
    # §9: the write is inside the non-whitespace branch (empty is never cached).
    case_idx = body.rfind("*[![:space:]]*)", 0, write_idx)
    assert case_idx > miss_idx, (
        "the cache write must be gated on non-whitespace router output — "
        "an empty result must never be cached (§9)."
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
# Cross-mirror parity: REMOVED in PR-39 (v0.2.12, 2026-05-16).
#
# Before PR-39, .claude/hooks/ was a byte-identical mirror of templates/hooks/
# shipped in the public repo. check_template_drift.py enforced parity in CI,
# and this section sanity-checked that gate locally. PR-39 deleted the
# duplicate: install.py now renders .claude/ from templates/ at install time,
# templates is the only source, and there's nothing to drift FROM.
# --------------------------------------------------------------------------
