# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 2 — shared TTL result-cache for injection queries.

Drives the real `templates/hooks/_lib/query-cache.sh` functions through bash
and asserts:
  - put/get round-trip within TTL (fresh hit returns the stored blob)
  - a distinct key MISSES (returns non-zero, no output)
  - stale entry (age >= TTL) MISSES so the caller re-queries
  - an EMPTY result is cached and served as a HIT (rc 0, no output) so an empty
    symbol isn't re-queried within the TTL
  - the key is deterministic + namespaced by surface (cg vs kg don't collide)
  - codegraph_query_block serves the SECOND identical call from cache WITHOUT
    re-launching the CLI (the load-bearing latency win)
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
CG_SH = LIB_DIR / "codegraph-query.sh"


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


def test_empty_result_is_cached_as_hit(tmp_path: Path) -> None:
    # A cached empty result must be a HIT (rc 0) that emits nothing — so an
    # empty symbol is NOT re-queried within the TTL.
    r = _run(
        'K="$(vco_query_cache_key cg emptysym "" 2)"\n'
        'vco_query_cache_put "$K" ""\n'
        'if OUT="$(vco_query_cache_get "$K")"; then echo "HIT[$OUT]"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "HIT[]" in r.stdout, r.stdout


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


def _run_cg(snippet: str, tmp_path: Path, project_root: Path) -> subprocess.CompletedProcess:
    py = shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n'
        f'export PROJECT_ROOT="{project_root}"\n'
        f'. "{QC_SH}"\n'
        f'. "{CG_SH}"\n'
        f"{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(tmp_path)
    )


def test_codegraph_query_block_second_call_served_from_cache(tmp_path: Path) -> None:
    """The load-bearing win: codegraph_query_block issues the CLI once, then
    serves the identical second call from cache — the CLI must NOT run twice.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    # Stub CLI: append to the marker on every call, emit one canned CODE block
    # on --hook-format.
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf x >> "{marker}"\n'
        'echo "CODE: sample.func | CodeFunction | distance=0.20 | src=src/x.py"\n'
        'echo "body line"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)

    r = _run_cg(
        'OUT1="$(codegraph_query_block "sample.func" "" 2 "" "sample.func")"\n'
        'OUT2="$(codegraph_query_block "sample.func" "" 2 "" "sample.func")"\n'
        'echo "OUT1=[$OUT1]"\n'
        'echo "OUT2=[$OUT2]"',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    # Both calls returned the same non-empty block.
    assert "sample.func" in r.stdout, r.stdout
    # The CLI ran EXACTLY ONCE across the two identical calls.
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 1, (
        f"codegraph_query_block must serve the 2nd identical call from cache; "
        f"CLI ran {len(calls)} times (expected 1)."
    )


def test_codegraph_query_block_caches_empty_result(tmp_path: Path) -> None:
    """An empty CLI result is cached, so the second identical query does not
    re-launch the CLI (empty-symbol thrash avoidance)."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    # CLI emits nothing (empty result).
    cli.write_text(
        "#!/usr/bin/env bash\n" f'printf x >> "{marker}"\n' "exit 0\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    r = _run_cg(
        'codegraph_query_block "emptyq" "" 2 "" "emptyq" >/dev/null\n'
        'codegraph_query_block "emptyq" "" 2 "" "emptyq" >/dev/null\n'
        'echo done',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 1, (
        f"empty result must be cached; CLI ran {len(calls)} times (expected 1)."
    )
