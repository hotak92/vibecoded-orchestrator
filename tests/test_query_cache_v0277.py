# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 2 — shared TTL result-cache for injection queries.

Drives the real `templates/hooks/_lib/query-cache.sh` functions through bash
and asserts:
  - put/get round-trip within TTL (fresh hit returns the stored blob)
  - a distinct key MISSES (returns non-zero, no output)
  - stale entry (age >= TTL) MISSES so the caller re-queries
  - an EMPTY result is NEVER cached (v0.2.101 §9 poison fix — an empty blob is
    indistinguishable from a timed-out/killed leg and suppressed retries for
    the whole TTL; detailed coverage in test_v02101_query_cache_poison_fix.py)
  - the key is deterministic + namespaced by surface (cg vs kg don't collide)
  - (v0.2.101 wave-2 review SF-2: the codegraph_query_block rows were
    RETIRED with the function — _lib/codegraph-query.{sh,ps1} lost its
    last hook callers; the router's Python cache + the kgi/cgi namespaces
    own the cross-surface latency win now, pinned by
    tests/test_v02101_router_surfaces.py)
  - the .ps1 sibling exists (hook-os-parity CI gate EXCLUDES _lib/)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
QC_SH = LIB_DIR / "query-cache.sh"
QC_PS1 = LIB_DIR / "query-cache.ps1"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _has_bash(), reason="bash required")


def _run(snippet: str, tmp_path: Path, project_root: Path | None = None) -> subprocess.CompletedProcess:
    py = shutil.which("python3") or "python3"
    root = project_root or tmp_path
    script = (
        f'export PY="{py}"\n'
        f'export PROJECT_ROOT="{root}"\n'
        f'. "{QC_SH}"\n'
        f"{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(tmp_path)
    )


def test_query_cache_ps1_sibling_exists() -> None:
    assert QC_SH.exists(), "query-cache.sh missing"
    assert QC_PS1.exists(), (
        "query-cache.ps1 sibling MISSING — check_hook_parity.py EXCLUDES _lib/, "
        "so this must be hand-verified here."
    )


def test_put_get_roundtrip_within_ttl(tmp_path: Path) -> None:
    r = _run(
        'K="$(vco_query_cache_key cg "some symbol" proj 2)"\n'
        'vco_query_cache_put "$K" "CODE: foo | body"\n'
        'if OUT="$(vco_query_cache_get "$K")"; then echo "HIT:$OUT"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "HIT:CODE: foo | body" in r.stdout, r.stdout


def test_distinct_key_misses(tmp_path: Path) -> None:
    r = _run(
        'K1="$(vco_query_cache_key cg "sym one" "" 2)"\n'
        'vco_query_cache_put "$K1" "CODE: one"\n'
        'K2="$(vco_query_cache_key cg "sym two" "" 2)"\n'
        'if vco_query_cache_get "$K2" >/dev/null; then echo "HIT"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, r.stdout


def test_stale_entry_misses(tmp_path: Path) -> None:
    # TTL of 1s + touch the file 5s into the past → must be a miss.
    r = _run(
        'export VCO_QUERY_CACHE_TTL=1\n'
        'K="$(vco_query_cache_key cg stalesym "" 2)"\n'
        'vco_query_cache_put "$K" "CODE: stale"\n'
        'F="$(vco_query_cache_dir)/$K"\n'
        'touch -d "5 seconds ago" "$F" 2>/dev/null || touch -t "$(date -d \'5 seconds ago\' +%Y%m%d%H%M.%S 2>/dev/null)" "$F" 2>/dev/null || true\n'
        'if vco_query_cache_get "$K" >/dev/null; then echo "HIT"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, r.stdout


def test_empty_result_is_never_cached(tmp_path: Path) -> None:
    # v0.2.101 §9 (kickoff probe Cause 1b): an empty put writes NOTHING, and
    # a get on a pre-existing empty entry is a MISS — an empty blob cannot be
    # told apart from a timed-out leg, and caching it poisoned the key for
    # the whole TTL. Detailed coverage: test_v02101_query_cache_poison_fix.py.
    r = _run(
        'K="$(vco_query_cache_key cg emptysym "" 2)"\n'
        'vco_query_cache_put "$K" ""\n'
        'if OUT="$(vco_query_cache_get "$K")"; then echo "HIT[$OUT]"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, r.stdout


def test_key_is_deterministic_and_surface_namespaced(tmp_path: Path) -> None:
    r = _run(
        'A="$(vco_query_cache_key cg foo bar 2)"\n'
        'B="$(vco_query_cache_key cg foo bar 2)"\n'
        'C="$(vco_query_cache_key kg foo bar 2)"\n'
        '[ "$A" = "$B" ] && echo "STABLE" || echo "UNSTABLE"\n'
        '[ "$A" != "$C" ] && echo "NAMESPACED" || echo "COLLIDE"',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "STABLE" in r.stdout and "NAMESPACED" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# WP-E (v0.2.92) — prompt_id cache-key scoping + --transcript threading for
# vco_kg_search_cached (the surviving shell wrapper — pre-tool-use's KG path).
# own docstring, cache-key differentiation across prompt_id is explicitly THIS
# file's job, not the enrichment module's — these tests are that coverage.
# ---------------------------------------------------------------------------


def test_kg_search_cached_prompt_id_differentiates_cache_key(tmp_path: Path) -> None:
    """Same query, same prompt_id => 2nd call is a cache HIT (no live re-run).
    Same query, DIFFERENT prompt_id => fresh MISS (live re-run) — this is the
    cross-turn correctness the enrichment feature needs: two turns issuing the
    identical short trigger must not collide on one cache entry, because the
    embedded (enriched) text differs per turn even though the raw trigger text
    passed on this bash boundary does not.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "venv_calls"
    venv = tmp_path / "fake_venv"
    venv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf x >> "{marker}"\n'
        'echo "KG: result for $*"\n',
        encoding="utf-8",
    )
    venv.chmod(0o755)
    rl_script = tmp_path / "rl_kg_search.py"
    rl_script.write_text("# stub\n", encoding="utf-8")

    r = _run(
        f'vco_kg_search_cached "{venv}" "{rl_script}" "some query" 1 "promptA" "" >/dev/null\n'
        f'vco_kg_search_cached "{venv}" "{rl_script}" "some query" 1 "promptA" "" >/dev/null\n'
        f'vco_kg_search_cached "{venv}" "{rl_script}" "some query" 1 "promptB" "" >/dev/null\n'
        "echo done",
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 2, (
        "expected exactly 2 live venv invocations (promptA's 2nd call must be "
        f"a cache hit; promptB is a fresh prompt_id and must miss); got {len(calls)}"
    )


def test_kg_search_cached_threads_transcript_path_never_contents(tmp_path: Path) -> None:
    """--transcript <path> reaches the producer's argv verbatim, but the
    transcript file's CONTENTS are never read into this shell — only the path
    travels. Privacy proof for R31 / vco_lib/transcript_context.py's discipline.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "venv_argv"
    venv = tmp_path / "fake_venv"
    venv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{marker}"\n'
        'echo "KG: ok"\n',
        encoding="utf-8",
    )
    venv.chmod(0o755)
    rl_script = tmp_path / "rl_kg_search.py"
    rl_script.write_text("# stub\n", encoding="utf-8")

    transcript = tmp_path / "transcript.jsonl"
    secret_marker = "SECRET_THINKING_TEXT_MUST_NOT_LEAK"
    transcript.write_text(
        f'{{"type": "assistant", "text": "{secret_marker}"}}\n', encoding="utf-8"
    )

    r = _run(
        f'vco_kg_search_cached "{venv}" "{rl_script}" "another query" 1 "p1" "{transcript}"',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    argv_log = marker.read_text("utf-8") if marker.exists() else ""
    assert "--transcript" in argv_log, argv_log
    assert str(transcript) in argv_log, argv_log
    assert secret_marker not in argv_log, "transcript CONTENTS leaked into producer argv"
    assert secret_marker not in r.stdout, "transcript CONTENTS leaked into hook stdout"
    assert secret_marker not in r.stderr, "transcript CONTENTS leaked into hook stderr"


def test_kg_search_cached_without_new_args_omits_transcript_flag(tmp_path: Path) -> None:
    """A caller that omits prompt_id/transcript_path (the pre-WP-E call shape,
    4 positional args) reproduces today's exact argv — no --transcript flag
    appended — the zero-functionality-change contract the docstring promises.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "venv_argv"
    venv = tmp_path / "fake_venv"
    venv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{marker}"\n'
        'echo "KG: ok"\n',
        encoding="utf-8",
    )
    venv.chmod(0o755)
    rl_script = tmp_path / "rl_kg_search.py"
    rl_script.write_text("# stub\n", encoding="utf-8")

    r = _run(
        f'vco_kg_search_cached "{venv}" "{rl_script}" "legacy query" 1',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    argv_log = marker.read_text("utf-8") if marker.exists() else ""
    assert "--transcript" not in argv_log, argv_log

# v0.2.101 wave-2 review SF-2: the vco_dual_search_cached and
# codegraph_query_block WP-E rows that lived here were RETIRED with those
# functions (zero live callers after the router rework). The properties they
# pinned survive at the new call sites: prompt_id-scoped cache keys and
# transcript-as-PATH threading are router-side now —
# tests/test_v0292_query_enrichment.py::TestHookCallSitesForwardTranscript
# (literal pins) and tests/test_v02101_router_surfaces.py::
# TestBashWrapper::test_kg_leg_receives_transcript_path (behavioural).
