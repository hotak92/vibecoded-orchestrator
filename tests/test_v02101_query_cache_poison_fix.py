# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 §9 (kickoff probe ADDED item) — the query cache must NEVER cache
an empty or timed-out result.

RED on the base tree: today `vco_query_cache_put` stores empty blobs and
`codegraph_query_block` caches the output of a killed CLI (probe evidence:
a 0-byte entry served a fast EMPTY hit for 900 s after a `timeout 4` kill —
reviews/V02101-INJECTION-KICKOFF-PROBES-2026-10-05.md, Cause 1b).

New contract (both OS siblings):
  * ACT   — put("") writes nothing; a CLI exit != 0 (timeout=124, error)
            writes nothing even when it emitted partial output;
  * ACT   — a pre-existing EMPTY cache entry (poisoned by an older version)
            is a MISS and is removed (self-heal);
  * LEAVE-ALONE — a non-empty put/get round-trip, TTL staleness, key
            determinism and the once-only CLI behaviour on a genuine hit are
            unchanged.
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
        f'. "{CG_SH}"\n'
        f"{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        timeout=60, cwd=str(tmp_path),
    )


def _cache_dir(root: Path) -> Path:
    return root / ".claude" / "state" / "query_cache"


def _make_stub_cli(proj: Path, body: str) -> None:
    (proj / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    cli.chmod(0o755)


# --- ACT: empty results are never cached ------------------------------------


def test_put_empty_writes_nothing(tmp_path: Path) -> None:
    r = _run(
        'K="$(vco_query_cache_key cg emptysym "" 2)"\n'
        'vco_query_cache_put "$K" ""\n'
        'D="$(vco_query_cache_dir)"\n'
        '[ -e "$D/$K" ] && echo WROTE || echo CLEAN',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "CLEAN" in r.stdout, r.stdout


def test_empty_cache_entry_is_a_miss_and_self_heals(tmp_path: Path) -> None:
    """A 0-byte entry poisoned by an older version must read as a MISS (so
    the query retries live) and be removed."""
    r = _run(
        'K="$(vco_query_cache_key cg poison "" 2)"\n'
        'D="$(vco_query_cache_dir)"\n'
        'mkdir -p "$D"; : > "$D/$K"\n'          # simulate the poisoned entry
        'if OUT="$(vco_query_cache_get "$K")"; then echo "HIT[$OUT]"; else echo "MISS"; fi\n'
        '[ -e "$D/$K" ] && echo LEFT || echo REMOVED',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, r.stdout
    assert "REMOVED" in r.stdout, r.stdout


def test_codegraph_query_block_empty_cli_result_not_cached(tmp_path: Path) -> None:
    """Probe Cause 1b: the CLI returns nothing (killed / no hits) — no cache
    entry may be written, so the NEXT identical query retries live."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    _make_stub_cli(proj, f'printf x >> "{marker}"\nexit 0\n')
    r = _run(
        'codegraph_query_block "emptyq" "" 2 "" "emptyq" >/dev/null\n'
        'codegraph_query_block "emptyq" "" 2 "" "emptyq" >/dev/null\n'
        'D="$(vco_query_cache_dir)"\n'
        'ls "$D" | wc -l',
        tmp_path, proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 2, (
        f"an EMPTY result must NOT be cached; the CLI must run again "
        f"(ran {len(calls)} times, expected 2)")
    assert r.stdout.strip().splitlines()[-1] == "0", "no cache entries may exist"


def test_codegraph_query_block_failed_cli_not_cached(tmp_path: Path) -> None:
    """A timed-out/killed leg (exit 124) or an errored leg (exit 1) may have
    emitted PARTIAL output — it must never reach the cache."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    _make_stub_cli(
        proj,
        'echo "CODE: partial.fn | CodeFunction | distance=0.1"\nexit 124\n',
    )
    r = _run(
        'codegraph_query_block "partialq" "" 2 "" "partialq" >/dev/null\n'
        'D="$(vco_query_cache_dir)"\n'
        'ls "$D" | wc -l',
        tmp_path, proj,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "0", (
        "a non-zero CLI exit (timeout/kill) must not poison the cache key")


# --- LEAVE-ALONE: the non-empty contract is unchanged ------------------------


def test_nonempty_roundtrip_unchanged(tmp_path: Path) -> None:
    blob = "CODE: mod.fn | CodeFunction | distance=0.2 | src=mod.py"
    r = _run(
        f'K="$(vco_query_cache_key cg mod.fn "" 2)"\n'
        f'vco_query_cache_put "$K" "{blob}"\n'
        f'if OUT="$(vco_query_cache_get "$K")"; then echo "HIT[$OUT]"; else echo "MISS"; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert f"HIT[{blob}]" in r.stdout, r.stdout


def test_nonempty_hit_still_serves_once(tmp_path: Path) -> None:
    """The load-bearing latency win is untouched: a genuine non-empty result
    is cached and the second identical call does NOT re-launch the CLI."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    _make_stub_cli(
        proj,
        f'printf x >> "{marker}"\n'
        'echo "CODE: sample.func | CodeFunction | distance=0.20 | src=src/x.py"\n',
    )
    r = _run(
        'codegraph_query_block "sample.func" "" 2 "" "sample.func" >/dev/null\n'
        'codegraph_query_block "sample.func" "" 2 "" "sample.func" >/dev/null\n'
        'echo done',
        tmp_path, proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 1, f"non-empty result must still cache; CLI ran {len(calls)}×"


def test_stale_entry_still_a_miss(tmp_path: Path) -> None:
    r = _run(
        'K="$(vco_query_cache_key cg stale "" 2)"\n'
        'vco_query_cache_put "$K" "CODE: x | CodeFunction | distance=0.1"\n'
        'D="$(vco_query_cache_dir)"; touch -d "@1000000000" "$D/$K" 2>/dev/null '
        '|| touch -t 200109090146 "$D/$K"\n'
        'if vco_query_cache_get "$K" >/dev/null; then echo HIT; else echo MISS; fi',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, r.stdout


def test_kg_search_cached_empty_not_written(tmp_path: Path) -> None:
    """vco_kg_search_cached shares the chokepoint: an empty producer output
    must not create an entry."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    stub_script = tmp_path / "rl_stub.py"
    stub_script.write_text("import sys\n", encoding="utf-8")  # prints nothing
    py = shutil.which("python3") or "python3"
    r = _run(
        f'vco_kg_search_cached "{py}" "{stub_script}" "q" 1 >/dev/null\n'
        'D="$(vco_query_cache_dir)"\n'
        'ls "$D" | wc -l',
        tmp_path, proj,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "0"


# --- .ps1 sibling parity ------------------------------------------------------


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
class TestPs1Parity:
    def _run_ps1(self, snippet: str, tmp_path: Path) -> subprocess.CompletedProcess:
        script = (
            f'$env:CLAUDE_PROJECT_DIR = "{tmp_path}"\n'
            f'. "{QC_PS1}"\n'
            f'{snippet}\n'
        )
        return subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=120,
                              cwd=str(tmp_path))

    def test_put_empty_writes_nothing(self, tmp_path: Path) -> None:
        r = self._run_ps1(
            '$K = Get-VcoQueryCacheKey "cg" "emptysym" "" "2"\n'
            'Set-VcoQueryCache $K ""\n'
            '$D = Get-VcoQueryCacheDir\n'
            'if (Test-Path -LiteralPath (Join-Path $D $K)) { "WROTE" } else { "CLEAN" }',
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert "CLEAN" in r.stdout, r.stdout

    def test_empty_entry_is_miss_and_removed(self, tmp_path: Path) -> None:
        r = self._run_ps1(
            '$K = Get-VcoQueryCacheKey "cg" "poison" "" "2"\n'
            '$D = Get-VcoQueryCacheDir\n'
            'New-Item -ItemType File -Path (Join-Path $D $K) -Force | Out-Null\n'
            '$qc = Get-VcoQueryCache $K\n'
            'if ($qc.Hit) { "HIT" } else { "MISS" }\n'
            'if (Test-Path -LiteralPath (Join-Path $D $K)) { "LEFT" } else { "REMOVED" }',
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert "MISS" in r.stdout, r.stdout
        assert "REMOVED" in r.stdout, r.stdout

    def test_nonempty_roundtrip_unchanged(self, tmp_path: Path) -> None:
        r = self._run_ps1(
            '$K = Get-VcoQueryCacheKey "cg" "mod.fn" "" "2"\n'
            'Set-VcoQueryCache $K "CODE: mod.fn | CodeFunction | distance=0.2"\n'
            '$qc = Get-VcoQueryCache $K\n'
            'if ($qc.Hit) { "HIT[$($qc.Value)]" } else { "MISS" }',
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert "HIT[CODE: mod.fn | CodeFunction | distance=0.2]" in r.stdout, r.stdout

    def test_query_block_timeout_not_cached(self, tmp_path: Path) -> None:
        """Invoke-VcoCodegraphQueryBlock: a job that exceeds the inner 4 s
        bound is killed — its (empty) output must not be cached. Uses a 6 s
        stub, so this test costs ~4 s."""
        proj = tmp_path
        (proj / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
        (proj / ".claude" / "state").mkdir(parents=True, exist_ok=True)
        cli = proj / ".claude" / "scripts" / "code-graph-query"
        cli.write_text("#!/usr/bin/env bash\nsleep 6\n", encoding="utf-8")
        cli.chmod(0o755)
        cg_ps1 = LIB_DIR / "codegraph-query.ps1"
        script = (
            f'$env:CLAUDE_PROJECT_DIR = "{proj}"\n'
            f'. "{QC_PS1}"\n'
            f'. "{cg_ps1}"\n'
            '$null = Invoke-VcoCodegraphQueryBlock -Query "slowq" -Limit 2\n'
            '$D = Get-VcoQueryCacheDir\n'
            "@((Get-ChildItem -LiteralPath $D -File | Measure-Object).Count)"
        )
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1] == "0", (
            "a timed-out code-graph leg must not poison the cache key")
