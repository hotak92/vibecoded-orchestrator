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


# ---------------------------------------------------------------------------
# WP-E (v0.2.92) — prompt_id cache-key scoping + --transcript threading for
# vco_kg_search_cached / vco_dual_search_cached. Per test_v0292_query_enrichment.py's
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


def test_dual_search_cached_prompt_id_differentiates_and_threads_transcript(
    tmp_path: Path,
) -> None:
    """vco_dual_search_cached: prompt_id joins BOTH keys' derivation (only the
    KG leg is exercised here — cg_out="" disables the CG leg so the test does
    not need a code-graph CLI stub) and a single --transcript flag threads
    into the driver's argv exactly once, never containing transcript CONTENTS.
    """
    proj = tmp_path / "proj"
    scripts_dir = proj / "scripts"
    (proj / ".claude" / "state").mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    marker = tmp_path / "driver_calls"
    venv = tmp_path / "fake_venv"
    venv.write_text(
        "#!/usr/bin/env bash\n"
        "{\n"
        '  echo "===CALL==="\n'
        '  printf "%s\\n" "$@"\n'
        f'}} >> "{marker}"\n'
        'echo "<<<VCO-DUAL:KG>>>"\n'
        'echo "KG: result"\n',
        encoding="utf-8",
    )
    venv.chmod(0o755)
    rl_script = scripts_dir / "rl_kg_search.py"
    rl_script.write_text("# stub\n", encoding="utf-8")
    driver = scripts_dir / "hook_dual_search.py"
    driver.write_text("# stub\n", encoding="utf-8")

    transcript = tmp_path / "transcript.jsonl"
    secret_marker = "SECRET_TRANSCRIPT_CONTENTS_DUAL"
    transcript.write_text(secret_marker, encoding="utf-8")

    kg_out1 = tmp_path / "kg_out1"
    kg_out2 = tmp_path / "kg_out2"
    kg_out3 = tmp_path / "kg_out3"

    r = _run(
        f'vco_dual_search_cached "{kg_out1}" "" "{venv}" "{rl_script}" "dual query" 1 "" 2 "" "" "promptA" "{transcript}"\n'
        f'vco_dual_search_cached "{kg_out2}" "" "{venv}" "{rl_script}" "dual query" 1 "" 2 "" "" "promptA" "{transcript}"\n'
        f'vco_dual_search_cached "{kg_out3}" "" "{venv}" "{rl_script}" "dual query" 1 "" 2 "" "" "promptB" "{transcript}"\n'
        "echo done",
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr

    call_log = marker.read_text("utf-8") if marker.exists() else ""
    assert call_log.count("===CALL===") == 2, (
        "expected exactly 2 driver invocations (promptA's 2nd call must be a "
        f"cache hit; promptB is a fresh prompt_id and must miss):\n{call_log}"
    )
    assert "--transcript" in call_log, call_log
    assert str(transcript) in call_log, call_log
    assert secret_marker not in call_log, "transcript CONTENTS leaked into driver argv"
    assert secret_marker not in r.stdout, "transcript CONTENTS leaked into hook stdout"
    assert secret_marker not in r.stderr, "transcript CONTENTS leaked into hook stderr"

    # Both the fresh call and the cache-hit replay produced the same KG block.
    assert kg_out1.read_text("utf-8") == "KG: result"
    assert kg_out2.read_text("utf-8") == "KG: result"
    assert kg_out3.read_text("utf-8") == "KG: result"


def test_dual_search_cached_without_new_args_omits_transcript_flag(tmp_path: Path) -> None:
    """A caller passing only the original 10 positional args (no prompt_id/
    transcript_path) reproduces today's exact driver argv — no --transcript
    flag appended — matching the ZERO FUNCTIONALITY CHANGE contract documented
    above vco_dual_search_cached in query-cache.sh.
    """
    proj = tmp_path / "proj"
    scripts_dir = proj / "scripts"
    (proj / ".claude" / "state").mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    marker = tmp_path / "driver_calls"
    venv = tmp_path / "fake_venv"
    venv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{marker}"\n'
        'echo "<<<VCO-DUAL:KG>>>"\n'
        'echo "KG: result"\n',
        encoding="utf-8",
    )
    venv.chmod(0o755)
    rl_script = scripts_dir / "rl_kg_search.py"
    rl_script.write_text("# stub\n", encoding="utf-8")
    driver = scripts_dir / "hook_dual_search.py"
    driver.write_text("# stub\n", encoding="utf-8")

    kg_out = tmp_path / "kg_out"
    r = _run(
        f'vco_dual_search_cached "{kg_out}" "" "{venv}" "{rl_script}" "legacy dual query" 1 "" 2 "" ""',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    argv_log = marker.read_text("utf-8") if marker.exists() else ""
    assert "--transcript" not in argv_log, argv_log


# ---------------------------------------------------------------------------
# WP-E (v0.2.92) — prompt_id cache-key scoping + --transcript threading for
# the STANDALONE codegraph_query_block() path (distinct from
# vco_dual_search_cached above: this is the fallback used directly by
# pre-tool-use.sh's _cg_inject and pre-bash-context-inject.sh, and by
# pre-edit-context-inject.sh's partial-install fallback branch). Mirrors the
# KG-side tests above; see that section's docstring for the cross-turn
# correctness rationale.
# ---------------------------------------------------------------------------


def test_codegraph_query_block_prompt_id_differentiates_cache_key(tmp_path: Path) -> None:
    """Same query, same prompt_id => 2nd call is a cache HIT (CLI runs once).
    Same query, DIFFERENT prompt_id => fresh MISS (CLI runs again) — without
    this, two turns issuing the identical short trigger would collide on one
    cache entry despite the embedded (enriched) query differing per turn.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf x >> "{marker}"\n'
        'echo "CODE: sample.func | CodeFunction | distance=0.20 | src=src/x.py"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)

    r = _run_cg(
        'codegraph_query_block "sample.func" "" 2 "" "sample.func" "promptA" "" >/dev/null\n'
        'codegraph_query_block "sample.func" "" 2 "" "sample.func" "promptA" "" >/dev/null\n'
        'codegraph_query_block "sample.func" "" 2 "" "sample.func" "promptB" "" >/dev/null\n'
        "echo done",
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 2, (
        "expected exactly 2 live CLI invocations (promptA's 2nd call must be "
        f"a cache hit; promptB is a fresh prompt_id and must miss); got {len(calls)}"
    )


def test_codegraph_query_block_threads_transcript_path_never_contents(tmp_path: Path) -> None:
    """--transcript <path> reaches the code-graph CLI's argv verbatim, but the
    transcript file's CONTENTS never touch this shell — only the path
    travels. Privacy proof for R31 / the coordinator's condition that
    transcript text must not reach the CG cache key either (see the next
    test) or any log/argv/telemetry surface.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_argv"
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{marker}"\n'
        'echo "CODE: sample.func | CodeFunction | distance=0.20 | src=src/x.py"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    secret_marker = "TRANSCRIPT-CONTENTS-MUST-NOT-LEAK-CG"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(secret_marker, encoding="utf-8")

    r = _run_cg(
        f'codegraph_query_block "another.func" "" 2 "" "another.func" "p1" "{transcript}"',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    argv_log = marker.read_text("utf-8") if marker.exists() else ""
    assert "--transcript" in argv_log, argv_log
    assert str(transcript) in argv_log, argv_log
    assert secret_marker not in argv_log, "transcript CONTENTS leaked into CLI argv"
    assert secret_marker not in r.stdout, "transcript CONTENTS leaked into hook stdout"
    assert secret_marker not in r.stderr, "transcript CONTENTS leaked into hook stderr"


def test_codegraph_query_block_transcript_excluded_from_cache_key(tmp_path: Path) -> None:
    """Coordinator condition 4: transcript must not reach the CG cache key,
    for the same privacy reason it must not reach the KG one (only the PATH
    travels to argv; the key must not vary with — or embed — a transcript
    path/contents). Same query + prompt_id but a DIFFERENT transcript path
    must still be served from cache (2nd + 3rd calls are hits; only the
    first call is live).
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_calls"
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf x >> "{marker}"\n'
        'echo "CODE: sample.func | CodeFunction | distance=0.20 | src=src/x.py"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    transcript_a = tmp_path / "transcript_a.jsonl"
    transcript_a.write_text("a", encoding="utf-8")
    transcript_b = tmp_path / "transcript_b.jsonl"
    transcript_b.write_text("b", encoding="utf-8")

    r = _run_cg(
        f'codegraph_query_block "keyed.func" "" 2 "" "keyed.func" "promptZ" "{transcript_a}" >/dev/null\n'
        f'codegraph_query_block "keyed.func" "" 2 "" "keyed.func" "promptZ" "{transcript_b}" >/dev/null\n'
        f'codegraph_query_block "keyed.func" "" 2 "" "keyed.func" "promptZ" "" >/dev/null\n'
        "echo done",
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    calls = marker.read_text("utf-8") if marker.exists() else ""
    assert len(calls) == 1, (
        "same query+prompt_id with a DIFFERENT (or absent) transcript path "
        f"must still hit the same cache entry (transcript is not key material); "
        f"CLI ran {len(calls)} times (expected 1)."
    )


def test_codegraph_query_block_without_new_args_omits_transcript_flag(tmp_path: Path) -> None:
    """A caller passing only the original 5 positional args (no prompt_id/
    transcript) reproduces today's exact CLI argv — no --transcript flag
    appended — zero functionality change for callers not yet updated.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cli_argv"
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{marker}"\n'
        'echo "CODE: legacy.func | CodeFunction | distance=0.20 | src=src/y.py"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)

    r = _run_cg(
        'codegraph_query_block "legacy.func" "" 2 "" "legacy.func"',
        tmp_path,
        proj,
    )
    assert r.returncode == 0, r.stderr
    argv_log = marker.read_text("utf-8") if marker.exists() else ""
    assert "--transcript" not in argv_log, argv_log
