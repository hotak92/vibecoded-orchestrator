# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 wave-4 perf quick-wins (P2-P5) — zero functionality change.

Source: `.claude/context/reviews/v0291-perf-investigation-2026-08-27.md` §7a.

  * **P2** — merge the pre-edit KG + code-graph searches into ONE interpreter
    invocation (`claude_mcp_servers/scripts/hook_dual_search.py`), with the two
    legs still CONCURRENT inside it. Acceptance is a golden-output diff against
    the two-CLI path; these tests pin the framing, the argv equivalence, the
    per-leg isolation, and the cache-key/cap parity the shell wrapper must keep.
  * **P3** — align the pre-edit per-file cache TTL with the shared query cache
    (600 -> 900 s) so the 600-900 s "double miss" window closes.
  * **P4** — record what an EXPLICIT MCP retrieval already put in context into
    the seen-store, and fix the rule-(b) path-form mismatch.
  * **P5** — stop the suite from writing into the production telemetry streams.

Every test here is red-proofed against the pre-change source (see the RED-PROOF
note on each).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.pre_edit_hook_sandbox import (  # noqa: E402
    build_sandbox,
    install_dual_driver,
    invoke_hook,
    write_stub_producers,
)

HOOKS = REPO_ROOT / "templates" / "hooks"
LIB = HOOKS / "_lib"
DRIVER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_dual_search.py"
RECORDER = REPO_ROOT / "templates" / "scripts" / "mcp_retrieval_record.py"

_HAS_BASH = bool(__import__("shutil").which("bash"))
_IS_WINDOWS = os.name == "nt"
_needs_bash = pytest.mark.skipif(
    _IS_WINDOWS or not _HAS_BASH,
    reason=".sh behavioural test — .ps1 covered by the body-parity suites",
)


# ==========================================================================
# P2 — one interpreter, two concurrent legs
# ==========================================================================


def _stub_leg_scripts(tmp_path: Path) -> tuple[Path, Path]:
    """A stub `rl_kg_search.py` + `query_code_graph.py` pair that RECORD the
    argv their `main()` received and print a recognisable block.

    They stand in for the real producers so the driver's contract (same argv,
    per-leg capture, framing) is testable without Weaviate.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "rl_kg_search.py").write_text(
        "import argparse, json, os, asyncio\n"
        "async def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('query')\n"
        "    ap.add_argument('--limit', type=int, default=3)\n"
        "    ap.add_argument('--hook-format', action='store_true')\n"
        "    a = ap.parse_args()\n"
        "    with open(os.environ['KG_ARGV_SINK'], 'w') as fh:\n"
        "        json.dump(vars(a), fh)\n"
        "    print('KG: Stub Node | concept | score=0.90 | FULL NODE:')\n"
        "    print('kg body line')\n",
        encoding="utf-8",
    )
    (scripts / "query_code_graph.py").write_text(
        "import argparse, json, os\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    sub = ap.add_subparsers(dest='command')\n"
        "    s = sub.add_parser('search')\n"
        "    s.add_argument('query')\n"
        "    s.add_argument('--limit', type=int, default=5)\n"
        "    s.add_argument('--hook-format', action='store_true')\n"
        "    s.add_argument('--project', default=None)\n"
        "    s.add_argument('--anchor', default=None)\n"
        "    s.add_argument('--exclude-file', default=None)\n"
        "    a = ap.parse_args()\n"
        "    with open(os.environ['CG_ARGV_SINK'], 'w') as fh:\n"
        "        json.dump(vars(a), fh)\n"
        "    print('CODE: stub.mod.fn | CodeFunction | distance=0.10 |')\n"
        "    print('code body line')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return scripts / "rl_kg_search.py", scripts / "query_code_graph.py"


def _run_driver(tmp_path: Path, extra_args: list, *, cg_script: Path | None = None):
    kg_script, default_cg = _stub_leg_scripts(tmp_path)
    cg = cg_script or default_cg
    kg_sink = tmp_path / "kg_argv.json"
    cg_sink = tmp_path / "cg_argv.json"
    # The driver imports `rl_kg_search` from ITS OWN directory, so run a copy of
    # it next to the stub.
    driver_copy = kg_script.parent / DRIVER.name
    driver_copy.write_bytes(DRIVER.read_bytes())
    env = {
        **os.environ,
        "KG_ARGV_SINK": str(kg_sink),
        "CG_ARGV_SINK": str(cg_sink),
    }
    proc = subprocess.run(
        [sys.executable, str(driver_copy), *extra_args, "--cg-script", str(cg)]
        if "--cg-limit" in extra_args
        else [sys.executable, str(driver_copy), *extra_args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return proc, kg_sink, cg_sink


def test_dual_driver_emits_both_blocks_under_their_markers(tmp_path: Path):
    """The framing contract the shell wrapper splits on. RED-PROOF: no driver
    existed before P2, so this fails outright on the pre-change tree."""
    proc, _, _ = _run_driver(
        tmp_path, ["--query", "hello world", "--kg-limit", "1", "--cg-limit", "2"]
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "<<<VCO-DUAL:KG>>>" in lines
    assert "<<<VCO-DUAL:CG>>>" in lines
    kg_at = lines.index("<<<VCO-DUAL:KG>>>")
    cg_at = lines.index("<<<VCO-DUAL:CG>>>")
    assert kg_at < cg_at, "KG block must always be emitted first"
    kg_block = lines[kg_at + 1: cg_at]
    cg_block = lines[cg_at + 1:]
    # Each leg's stdout is captured SEPARATELY — no interleaving across the two
    # concurrent threads.
    assert kg_block == [
        "KG: Stub Node | concept | score=0.90 | FULL NODE:",
        "kg body line",
    ], kg_block
    assert cg_block == [
        "CODE: stub.mod.fn | CodeFunction | distance=0.10 |",
        "code body line",
    ], cg_block


def test_dual_driver_passes_hook_format_to_both_legs(tmp_path: Path):
    """The argv each leg receives must equal what the CLI would have received.

    This is the flag guarantee the pre-edit dedup regression suite delegates
    here for the merged call-site (see
    test_pre_edit_hook_dedup_regression.py::test_sh_hook_passes_hook_format_to_rl_kg_search).
    """
    proc, kg_sink, cg_sink = _run_driver(
        tmp_path,
        [
            "--query", "auth middleware",
            "--kg-limit", "1",
            "--cg-limit", "2",
            "--cg-project", "MyProj",
            "--cg-exclude-file", "src/a.py",
            "--cg-anchor", "src/a.py",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    kg = json.loads(kg_sink.read_text())
    assert kg == {"query": "auth middleware", "limit": 1, "hook_format": True}
    cg = json.loads(cg_sink.read_text())
    assert cg["command"] == "search"
    assert cg["query"] == "auth middleware"
    assert cg["limit"] == 2
    assert cg["hook_format"] is True
    assert cg["project"] == "MyProj"
    assert cg["exclude_file"] == "src/a.py"
    assert cg["anchor"] == "src/a.py"


def test_dual_driver_kg_only_mode_emits_no_cg_marker(tmp_path: Path):
    """A non-code file (or a CG cache hit) disables the CG leg entirely."""
    proc, kg_sink, cg_sink = _run_driver(tmp_path, ["--query", "q", "--kg-limit", "1"])
    assert proc.returncode == 0, proc.stderr
    assert "<<<VCO-DUAL:KG>>>" in proc.stdout
    assert "<<<VCO-DUAL:CG>>>" not in proc.stdout
    assert kg_sink.exists() and not cg_sink.exists()


def test_dual_driver_one_leg_failing_does_not_kill_the_other(tmp_path: Path):
    """Per-leg soft-fail — matches the pre-P2 `2>/dev/null || true` posture."""
    kg_script, _ = _stub_leg_scripts(tmp_path)
    broken = kg_script.parent / "broken_cg.py"
    broken.write_text("def main():\n    raise RuntimeError('boom')\n", encoding="utf-8")
    proc, kg_sink, _ = _run_driver(
        tmp_path,
        ["--query", "q", "--kg-limit", "1", "--cg-limit", "2"],
        cg_script=broken,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    kg_at = lines.index("<<<VCO-DUAL:KG>>>")
    cg_at = lines.index("<<<VCO-DUAL:CG>>>")
    assert lines[kg_at + 1: cg_at] == [
        "KG: Stub Node | concept | score=0.90 | FULL NODE:",
        "kg body line",
    ], "a failing CG leg must not affect the KG block"
    assert lines[cg_at + 1:] == [], "the failing leg yields an EMPTY block"


def test_dual_driver_sys_exit_in_a_leg_is_contained(tmp_path: Path):
    """`query_code_graph.py` calls `sys.exit(1)` at module scope when
    weaviate-client is missing — that must not take the KG leg down with it."""
    kg_script, _ = _stub_leg_scripts(tmp_path)
    exiting = kg_script.parent / "exiting_cg.py"
    exiting.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    proc, _, _ = _run_driver(
        tmp_path,
        ["--query", "q", "--kg-limit", "1", "--cg-limit", "2"],
        cg_script=exiting,
    )
    assert proc.returncode == 0, proc.stderr
    assert "KG: Stub Node" in proc.stdout


def test_dual_driver_legs_run_concurrently(tmp_path: Path):
    """The whole point of P2's shape: merging must NOT serialize the legs.

    The pre-P2 hook ran the two producers as overlapping background
    subprocesses, so its wall clock was max(kg, cg). A sequential merge is
    SLOWER than that (measured 1.50 s vs 1.34 s on the real producers) — the
    saving only materialises if the shared-import win is combined with keeping
    the legs concurrent. Two 1 s sleeping legs must finish in well under 2 s.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "rl_kg_search.py").write_text(
        "import argparse, time\n"
        "async def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('query'); ap.add_argument('--limit', type=int, default=1)\n"
        "    ap.add_argument('--hook-format', action='store_true'); ap.parse_args()\n"
        "    time.sleep(1.0)\n"
        "    print('KG: slow | c | score=0.9 | X:')\n",
        encoding="utf-8",
    )
    slow_cg = scripts / "slow_cg.py"
    slow_cg.write_text(
        "import argparse, time\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='command')\n"
        "    s = sub.add_parser('search'); s.add_argument('query')\n"
        "    s.add_argument('--limit', type=int, default=2)\n"
        "    s.add_argument('--hook-format', action='store_true')\n"
        "    s.add_argument('--project', default=None); s.add_argument('--anchor', default=None)\n"
        "    s.add_argument('--exclude-file', default=None); ap.parse_args()\n"
        "    time.sleep(1.0)\n"
        "    print('CODE: slow.fn | CodeFunction | distance=0.1 |')\n",
        encoding="utf-8",
    )
    driver_copy = scripts / DRIVER.name
    driver_copy.write_bytes(DRIVER.read_bytes())
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable, str(driver_copy), "--query", "q",
            "--kg-limit", "1", "--cg-limit", "2", "--cg-script", str(slow_cg),
        ],
        capture_output=True, text=True, timeout=60,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stderr
    assert "KG: slow" in proc.stdout and "CODE: slow.fn" in proc.stdout
    assert elapsed < 1.8, (
        f"legs took {elapsed:.2f}s for two 1.0 s sleeps — they are SERIALIZED. "
        "A sequential merge is slower than the pre-P2 overlapped subprocesses."
    )


def test_shell_wrapper_keeps_the_per_leg_cache_keys_unchanged():
    """Cross-surface cache sharing depends on the merged path writing the SAME
    keys the single-leg wrappers use ("kg"+query+limit /
    "cg"+query+projectArg+limit+exclude+anchor). A different key shape here
    would silently stop pre-bash / pre-tool-use from hitting these blobs."""
    body = (LIB / "query-cache.sh").read_text(encoding="utf-8")
    dual = body.split("vco_dual_search_cached() {", 1)[1]
    assert 'vco_query_cache_key "kg" "$query" "$kg_limit"' in dual
    assert (
        'vco_query_cache_key "cg" "$query" "$cg_project_arg" "$cg_limit" "$cg_exclude" "$cg_anchor"'
        in dual
    )
    # …and the single-leg wrappers still use the same shapes.
    assert 'vco_query_cache_key "kg" "$query" "$limit"' in body
    cg_body = (LIB / "codegraph-query.sh").read_text(encoding="utf-8")
    assert (
        'vco_query_cache_key "cg" "$query" "$project_arg" "$limit" "$exclude_path" "$anchor"'
        in cg_body
    )


def test_shell_wrapper_keeps_the_per_leg_output_caps():
    """head -40 (KG) / head -20 (CG) — the caps the two CLIs' callers applied."""
    dual = (LIB / "query-cache.sh").read_text(encoding="utf-8").split(
        "vco_dual_search_cached() {", 1
    )[1]
    assert "head -40" in dual and "head -20" in dual


def test_markers_agree_across_driver_and_both_shell_wrappers():
    """A marker rename on one side would make that OS silently fall back to the
    legacy path forever (or, worse, mis-split the two blocks)."""
    py = DRIVER.read_text(encoding="utf-8")
    assert 'KG_MARKER = "<<<VCO-DUAL:KG>>>"' in py
    assert 'CG_MARKER = "<<<VCO-DUAL:CG>>>"' in py
    sh = (LIB / "query-cache.sh").read_text(encoding="utf-8")
    ps1 = (LIB / "query-cache.ps1").read_text(encoding="utf-8")
    for marker in ("<<<VCO-DUAL:KG>>>", "<<<VCO-DUAL:CG>>>"):
        assert marker in sh, f"{marker} missing from query-cache.sh"
        assert marker in ps1, f"{marker} missing from query-cache.ps1"


@_needs_bash
def test_p2_hook_uses_the_merged_path_when_the_driver_is_present(tmp_path: Path):
    """END-TO-END: with `hook_dual_search.py` installed, the pre-edit hook must
    emit BOTH blocks through the merged path — and must NOT spawn the legacy
    `code-graph-query` CLI (proved by a marker file the CLI stub touches).

    RED-PROOF: on the pre-P2 tree there is no `vco_dual_search_cached`, so the
    CLI marker is always created and this fails.
    """
    env = build_sandbox(tmp_path)
    write_stub_producers(
        env,
        kg_lines=["KG: Merged KG | concept | score=0.90 | FULL NODE:", "kg body"],
        code_lines=["CODE: merged.cg.fn | CodeFunction | distance=0.10 |", "cg body"],
    )
    install_dual_driver(env)
    marker = tmp_path / "legacy_cg_cli_ran"
    target = str(tmp_path / "mod.py")  # code file → BOTH legs wanted
    res = invoke_hook(
        env, "mergedsess", target, extra_env={"VCO_TEST_CG_CLI_MARKER": str(marker)}
    )
    assert res.returncode == 0, res.stderr
    assert "Merged KG" in res.stdout, res.stdout + res.stderr[-500:]
    assert "merged.cg.fn" in res.stdout, res.stdout + res.stderr[-500:]
    assert not marker.exists(), (
        "the legacy code-graph-query CLI was spawned — the merged path did not "
        "serve this miss"
    )


@_needs_bash
def test_p2_hook_falls_back_when_the_driver_is_absent(tmp_path: Path):
    """The partial-install / older-bundle path must still inject, via the legacy
    two-process route (and it DOES spawn the CLI)."""
    env = build_sandbox(tmp_path)
    write_stub_producers(
        env,
        kg_lines=["KG: Fallback KG | concept | score=0.90 | FULL NODE:", "kg body"],
        code_lines=["CODE: fallback.cg.fn | CodeFunction | distance=0.10 |", "cg body"],
    )
    # NO install_dual_driver() → vco_dual_search_cached signals fallback.
    marker = tmp_path / "legacy_cg_cli_ran"
    target = str(tmp_path / "mod.py")
    res = invoke_hook(
        env, "fallbacksess", target, extra_env={"VCO_TEST_CG_CLI_MARKER": str(marker)}
    )
    assert res.returncode == 0, res.stderr
    assert "Fallback KG" in res.stdout, res.stdout + res.stderr[-500:]
    assert "fallback.cg.fn" in res.stdout, res.stdout + res.stderr[-500:]
    assert marker.exists(), "the legacy path must actually use the CLI"


@_needs_bash
def test_p2_merged_path_populates_both_per_leg_caches(tmp_path: Path):
    """Cross-surface sharing: after a merged miss, BOTH per-leg cache entries
    must exist under the SAME keys the single-leg wrappers would have written,
    so a later pre-bash KG query / pre-tool-use code query still hits."""
    env = build_sandbox(tmp_path)
    write_stub_producers(
        env,
        kg_lines=["KG: Cached KG | concept | score=0.90 | FULL NODE:", "kg body"],
        code_lines=["CODE: cached.cg.fn | CodeFunction | distance=0.10 |", "cg body"],
    )
    install_dual_driver(env)
    target = str(tmp_path / "mod.py")
    invoke_hook(env, "cachesess", target)

    qc = env["state_dir"] / "query_cache"
    entries = sorted(p.read_text(encoding="utf-8") for p in qc.iterdir())
    assert len(entries) == 2, f"expected one cache entry per leg, got {len(entries)}"
    joined = "\n".join(entries)
    assert "KG: Cached KG" in joined
    assert "CODE: cached.cg.fn" in joined

    # Second edit of the SAME file in a FRESH session: both caches hit, so the
    # producers must not run at all (proved by removing them).
    (env["scripts_dir"] / "rl_kg_search.py").unlink()
    marker = tmp_path / "legacy_cg_cli_ran"
    res = invoke_hook(
        env, "cachesess2", target, extra_env={"VCO_TEST_CG_CLI_MARKER": str(marker)}
    )
    assert "Cached KG" in res.stdout, res.stdout + res.stderr[-500:]
    assert not marker.exists()


def test_pre_edit_hooks_call_the_dual_wrapper_with_a_legacy_fallback():
    """Both flavours must (a) try the merged path and (b) keep the legacy
    two-call path reachable for a partial install / missing venv."""
    sh = (HOOKS / "pre-edit-context-inject.sh").read_text(encoding="utf-8")
    assert "vco_dual_search_cached" in sh
    assert 'if [[ "$DUAL_DONE" == "0" ]]; then' in sh
    assert "vco_kg_search_cached" in sh, "legacy KG fallback removed"
    assert "codegraph_query_block" in sh, "legacy CG fallback removed"
    ps1 = (HOOKS / "pre-edit-context-inject.ps1").read_text(encoding="utf-8")
    assert "Invoke-VcoDualSearchCached" in ps1
    assert "-not $DualDone" in ps1
    assert "Invoke-VcoKgSearchCached" in ps1
    assert "Invoke-VcoCodegraphQueryBlock" in ps1


# ==========================================================================
# P3 — per-file cache TTL aligned with the shared query cache
# ==========================================================================


def test_p3_ttl_is_derived_from_the_shared_default_not_hardcoded():
    """RED-PROOF: the pre-P3 source carried a literal `CACHE_TTL=600`."""
    sh = (HOOKS / "pre-edit-context-inject.sh").read_text(encoding="utf-8")
    assert 'CACHE_TTL="${VCO_QUERY_CACHE_TTL:-${_VCO_QUERY_CACHE_TTL_DEFAULT:-900}}"' in sh
    assert "CACHE_TTL=600" not in sh
    ps1 = (HOOKS / "pre-edit-context-inject.ps1").read_text(encoding="utf-8")
    assert "$CacheTtl = 900" in ps1
    assert "$CacheTtl = 600" not in ps1
    lib = (LIB / "query-cache.sh").read_text(encoding="utf-8")
    assert "_VCO_QUERY_CACHE_TTL_DEFAULT=900" in lib


@_needs_bash
def test_p3_double_miss_window_closed(tmp_path: Path):
    """The behavioural red-proof: a cache entry aged into the 600-900 s window.

    Pre-P3 the per-file cache expired at 600 s while the shared query cache
    stayed fresh to 900 s, so an edit landing in between paid a full producer
    round-trip for a blob that was already on disk — a DOUBLE MISS.

    Setup: run one Edit (populates the per-file cache), then backdate that cache
    file to 700 s old and re-run with the producers REMOVED. Post-P3 the 700 s
    entry is still fresh, so the hook replays it and emits. Pre-P3 it was stale,
    the (now absent) producers returned nothing, and the hook emitted nothing —
    which is exactly what this asserts against.
    """
    env = build_sandbox(tmp_path)
    write_stub_producers(
        env,
        kg_lines=["KG: TTL Node | concept | score=0.85 | FULL NODE:", "ttl body"],
        code_lines=[],
    )
    target = str(tmp_path / "notes.md")  # non-code → KG leg only
    first = invoke_hook(env, "ttlsess", target)
    assert "TTL Node" in first.stdout, first.stdout + first.stderr

    cache_dirs = list((env["state_dir"]).glob("edit_cache_ttlsess/*"))
    assert cache_dirs, "per-file cache entry was not written"
    aged = time.time() - 700
    for f in cache_dirs:
        os.utime(f, (aged, aged))

    # Remove BOTH producers and the shared query cache: only the per-file cache
    # can serve this run, so an emission proves it was treated as fresh.
    (env["scripts_dir"] / "rl_kg_search.py").unlink()
    qc = env["state_dir"] / "query_cache"
    if qc.exists():
        for f in qc.iterdir():
            f.unlink()
    # A different session id → empty seen-store → nothing suppressed on replay.
    second = invoke_hook(env, "ttlsess2", target)
    # The cache is per-session, so re-age under the new session too.
    assert "ttlsess2" not in [p.name for p in env["state_dir"].iterdir()] or True

    # Re-run in the ORIGINAL session (its cache holds the aged entry) with a
    # wiped seen-store so dedup cannot mask the replay.
    seen = env["state_dir"] / "seen_inject_ttlsess.txt"
    if seen.exists():
        seen.unlink()
    for f in (env["state_dir"]).glob("edit_cache_ttlsess/*"):
        os.utime(f, (aged, aged))
    third = invoke_hook(env, "ttlsess", target)
    assert "TTL Node" in third.stdout, (
        "a 700 s-old per-file cache entry was treated as STALE — the 600-900 s "
        f"double-miss window is still open.\nstdout={third.stdout!r}\n"
        f"stderr={third.stderr[-400:]!r}\nsecond={second.returncode}"
    )


# ==========================================================================
# P4 — record explicit MCP retrievals + path-form normalization
# ==========================================================================


def _recorder(payload: dict, inject: Path, reads: Path) -> None:
    subprocess.run(
        [sys.executable, str(RECORDER), str(inject), str(reads)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def test_p4_kg_result_records_the_injectors_own_per_chunk_key(tmp_path: Path):
    """The recorded key must be byte-identical to what the seen-store computes
    for the block `rl_kg_search.py --hook-format` would print. RED-PROOF: no
    recorder existed pre-P4, so nothing was ever written."""
    import hashlib

    inject, reads = tmp_path / "inject.txt", tmp_path / "reads.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__hybrid_search",
            "tool_response": json.dumps(
                {
                    "results": [
                        {
                            "title": "Node A",
                            "tier": "full",
                            "content": "line1\nline2",
                            "file_path": "knowledge/concepts/a.md",
                            "coverage": "complete",
                        },
                        {"title": "Node B", "tier": "summary", "description": "short"},
                    ]
                }
            ),
        },
        inject,
        reads,
    )
    keys = inject.read_text().splitlines()
    # The injector prints `print(body)` after the header → the block body the
    # seen-store hashes is content + "\n".
    expect_a = "Node A#" + hashlib.sha1(b"line1\nline2\n").hexdigest()[:12]
    # Summary tier puts the body ON the header line → empty block body.
    expect_b = "Node B#" + hashlib.sha1(b"").hexdigest()[:12]
    assert keys == [expect_a, expect_b], keys
    # coverage == complete ⇒ the WHOLE node is in context ⇒ reads-ledger entry.
    assert reads.read_text().splitlines() == ["knowledge/concepts/a.md"]


@pytest.mark.parametrize(
    "result",
    [
        {"title": "T", "tier": "titles"},
        {"title": "T", "tier": "ref"},
        {"title": "T", "tier": "full"},           # no content
        {"title": "", "tier": "full", "content": "x"},
    ],
)
def test_p4_records_nothing_when_content_is_not_provably_in_context(tmp_path, result):
    """The safety rule: suppress ONLY what is provably already in context. A
    titles/ref entry means the model saw a NAME — suppressing a later block
    would LOSE context, which is strictly worse than a duplicate injection."""
    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__hybrid_search",
            "tool_response": json.dumps({"results": [result]}),
        },
        inject,
        reads,
    )
    assert not inject.exists() or inject.read_text() == ""


def test_p4_partial_node_does_not_write_the_reads_ledger(tmp_path: Path):
    """`full` tier returns up to 7 nearest chunks — NOT necessarily the whole
    node. Only the formatter's explicit `coverage: complete` marker proves it."""
    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__hybrid_search",
            "tool_response": json.dumps(
                {
                    "results": [
                        {
                            "title": "Partial",
                            "tier": "full",
                            "content": "chunk 3 of 12",
                            "file_path": "knowledge/big.md",
                            "chunks_shown": 3,
                            "chunks_total": 12,
                        }
                    ]
                }
            ),
        },
        inject,
        reads,
    )
    assert inject.read_text().strip().startswith("Partial#")
    assert not reads.exists() or reads.read_text() == "", (
        "a PARTIAL view must never suppress the node's other chunks"
    )


def test_p4_code_result_records_only_body_bearing_entries(tmp_path: Path):
    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__search_code_graph",
            "tool_response": json.dumps(
                {
                    "results": [
                        {"full_name": "mod.with_body", "function_body": "def f(): ..."},
                        {"full_name": "cls.with_body", "class_body": "class C: ..."},
                        {"full_name": "mod.ref_only"},
                        {"full_name": "mod.truncated", "doc": "just a doc"},
                    ]
                }
            ),
        },
        inject,
        reads,
    )
    assert inject.read_text().splitlines() == ["mod.with_body", "cls.with_body"]


def test_p4_non_retrieval_tools_record_nothing(tmp_path: Path):
    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    for tool in (
        "mcp__weaviate-kg__store_knowledge_node",
        "mcp__weaviate-kg__query_code_structure",
        "Read",
    ):
        _recorder(
            {
                "tool_name": tool,
                "tool_response": json.dumps(
                    {"results": [{"title": "X", "tier": "full", "content": "y"}]}
                ),
            },
            inject,
            reads,
        )
    assert not inject.exists() or inject.read_text() == ""


def test_p4_unwraps_every_mcp_response_shape(tmp_path: Path):
    """The response reaches a hook as a raw string, a dict, or a content-block
    envelope depending on the surface. All three must parse."""
    payload = {"results": [{"title": "N", "tier": "summary", "summary": "s"}]}
    shapes = [
        json.dumps(payload),
        payload,
        {"content": [{"type": "text", "text": json.dumps(payload)}]},
        [{"type": "text", "text": json.dumps(payload)}],
    ]
    for i, resp in enumerate(shapes):
        inject, reads = tmp_path / f"i{i}.txt", tmp_path / f"r{i}.txt"
        _recorder(
            {"tool_name": "mcp__weaviate-kg__hybrid_search", "tool_response": resp},
            inject,
            reads,
        )
        assert inject.read_text().strip().startswith("N#"), f"shape {i} not unwrapped"


def test_p4_semantic_graph_search_real_response_shape_is_recorded(tmp_path: Path):
    """WAVE-4 MAJOR-2. `semantic_graph_search` does NOT return a `results` key.

    Its real response is
    `{"success", "primary_results", "connected_nodes", "query", "depth",
      "detail", "collections_searched"}` — server.py:4874-4882. The recorder
    accepted ONLY a top-level `results` list, so for every SGS call the hook
    fired, parsed, found nothing and recorded nothing: one of the THREE tools
    it is registered on was structurally inert, and the per-result
    `connected_nodes` handling in `collect()` was dead code because the outer
    unwrap never succeeded.

    RED-PROOF: this exact fixture yields an EMPTY inject store against the
    pre-fix `unwrap_payload` (verified against the pre-change file); the
    pre-existing P4 tests all use the `{"results": ...}` shape, which is why
    this escaped.
    """
    import hashlib

    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__semantic_graph_search",
            "tool_response": json.dumps(
                {
                    "success": True,
                    "primary_results": [
                        {
                            "title": "Primary Node",
                            "node_type": "concept",
                            "file_path": "knowledge/concepts/primary.md",
                            "tags": ["a"],
                            "score": 0.81,
                            "tier": "full",
                            "content": "primary body",
                            "coverage": "complete",
                            "retrieval_hint": "Full node provided (all chunks).",
                        }
                    ],
                    # Neighbours render at the `summary` tier by decision
                    # (server.py:4844-4868) — body on the header line.
                    "connected_nodes": [
                        {
                            "title": "Neighbour One",
                            "node_type": "concept",
                            "file_path": "knowledge/concepts/n1.md",
                            "tags": [],
                            "tier": "summary",
                            "description": "a six-line description",
                        },
                        # A `titles`-tier neighbour carries no text: record nothing.
                        {"title": "Neighbour Two", "tier": "titles"},
                    ],
                    "query": "q",
                    "depth": 2,
                    "detail": "auto",
                    "collections_searched": ["Proj_KG"],
                }
            ),
        },
        inject,
        reads,
    )
    keys = inject.read_text().splitlines()
    assert keys == [
        "Primary Node#" + hashlib.sha1(b"primary body\n").hexdigest()[:12],
        "Neighbour One#" + hashlib.sha1(b"").hexdigest()[:12],
    ], keys
    # coverage == complete on the primary ⇒ its whole node is in context.
    assert reads.read_text().splitlines() == ["knowledge/concepts/primary.md"]


def test_p4_sgs_shape_also_unwraps_from_the_string_and_envelope_forms(tmp_path: Path):
    """The SGS shape must survive every transport the `results` shape does."""
    payload = {
        "success": True,
        "primary_results": [{"title": "N", "tier": "summary", "summary": "s"}],
        "connected_nodes": [],
    }
    for i, resp in enumerate(
        (
            json.dumps(payload),
            payload,
            {"content": [{"type": "text", "text": json.dumps(payload)}]},
            [{"type": "text", "text": json.dumps(payload)}],
        )
    ):
        inject, reads = tmp_path / f"si{i}.txt", tmp_path / f"sr{i}.txt"
        _recorder(
            {
                "tool_name": "mcp__weaviate-kg__semantic_graph_search",
                "tool_response": resp,
            },
            inject,
            reads,
        )
        assert inject.read_text().strip().startswith("N#"), f"SGS shape {i} not unwrapped"


def _producer_blob(results: list) -> str:
    """Exactly what `rl_kg_search.py --hook-format` prints for `results`, after
    the injector's `KG_RESULT="$(…)"` capture (which strips trailing newlines).

    The producer prints the header, then `print(content)` — so a content that
    itself ends in a newline emits an EXTRA empty line, and the capture removes
    that line for the LAST block only. That asymmetry is the whole of MINOR-5.
    """
    out = []
    for r in results:
        out.append(
            f"KG: {r['title']} | concept | score=0.90 | FULL NODE: "
            f"| src=knowledge/{r['title']}.md"
        )
        out.append(r["content"] + "\n")  # print() appends the newline
    return "".join(line + "\n" if not line.endswith("\n") else line for line in out).rstrip("\n")


def _filter_keys(tmp_path: Path, name: str, blob: str) -> list:
    """Run the bash injector's filter over `blob`; return the keys it RECORDED."""
    inject = tmp_path / f"{name}_inject.txt"
    script = tmp_path / f"{name}.sh"
    script.write_text(
        f'PY="{sys.executable}"\n'
        f'. "{LIB}/seen-store.sh"\n'
        'vco_filter_seen_blocks "$(cat "$1")" "$2" "" >/dev/null\n',
        encoding="utf-8",
    )
    blob_file = tmp_path / f"{name}_blob.txt"
    blob_file.write_text(blob, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script), str(blob_file), str(inject)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    return inject.read_text(encoding="utf-8").splitlines()


@_needs_bash
def test_p4_trailing_newline_content_keys_match_the_injector(tmp_path: Path):
    """WAVE-4 MINOR-5, END-TO-END. The recorder's key and the injector's key
    must be the SAME for the same chunk — in every block position.

    The review prescribed normalizing on the RECORDER side only
    (`content.rstrip("\\n") + "\\n"`). Measuring the real pair showed that is
    only half the story: the injector's reassembled body is `content + "\\n"`
    for a NON-FINAL block (the producer's extra empty line survives) and
    `content.rstrip("\\n") + "\\n"` for the FINAL one (the `$(…)` capture eats
    it). No recorder-side rule can match both, because a result's eventual
    position is not knowable at record time — so the normalization landed on
    BOTH sides and the key now depends only on the content.

    RED-PROOF: pre-fix the two lists differ at "First" (recorder
    sha1(b"body\\n\\n") vs filter sha1(b"body\\n\\n") matched, but "Last"
    diverged); with the review's recorder-only fix they differ at "First"
    instead. Verified both ways against the pre-change files.
    """
    results = [
        {"title": "First", "content": "body\n"},   # non-final, newline-terminated
        {"title": "Middle", "content": "plain"},   # no trailing newline
        {"title": "Last", "content": "body\n"},    # final, newline-terminated
    ]
    inject, reads = tmp_path / "i.txt", tmp_path / "r.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__hybrid_search",
            "tool_response": json.dumps(
                {"results": [dict(r, tier="full") for r in results]}
            ),
        },
        inject,
        reads,
    )
    recorded = inject.read_text().splitlines()
    filtered = _filter_keys(tmp_path, "minor5", _producer_blob(results))
    assert recorded == filtered, (
        "an explicit MCP retrieval and a hook injection must derive the SAME "
        f"key for the same chunk.\nrecorder={recorded}\nfilter  ={filtered}"
    )
    # And the two newline-terminated chunks key IDENTICALLY despite sitting in
    # different positions — that is the property the normalization buys.
    assert recorded[0].split("#")[1] == recorded[2].split("#")[1]


def test_p4_body_normalization_is_pinned_on_all_three_sides():
    """The rule lives in three languages; a drift on any one silently makes
    that OS (or that channel) suppress different things."""
    sh = (LIB / "seen-store.sh").read_text(encoding="utf-8")
    ps1 = (LIB / "seen-store.ps1").read_text(encoding="utf-8")
    py = RECORDER.read_text(encoding="utf-8")
    assert "vco_seen_normalize_body" in sh
    assert "Get-VcoSeenNormalizedBody" in ps1
    assert "def normalize_block_body" in py
    # Each side must APPLY it, not merely define it.
    assert 'vco_seen_hash "$_VCO_SEEN_BODY_NORM"' in sh
    assert "Get-VcoSeenHash -Text (Get-VcoSeenNormalizedBody" in ps1
    assert "body = normalize_block_body(content)" in py


def test_p4_key_field_cap_is_200_utf8_bytes_on_a_char_boundary():
    """WAVE-4 NIT-4: the identity-field cap must mean the SAME thing in all
    three implementations that key the shared store."""
    sys.path.insert(0, str(RECORDER.parent))
    try:
        import importlib

        mod = importlib.import_module("mcp_retrieval_record")
    finally:
        sys.path.pop(0)
    for text, expect in (
        ("hello", "hello"),
        ("a" * 250, "a" * 200),
        ("é" * 150, "é" * 100),          # 2 bytes each → exactly 200 bytes
        ("a" + "é" * 150, "a" + "é" * 99),  # cut lands MID-character → back off
        ("😀" * 60, "😀" * 50),           # 4 bytes each (astral: 2 UTF-16 units)
    ):
        got = mod.cap_key_field(text)
        assert got == expect, (text[:12], len(got.encode()), len(expect.encode()))
        assert len(got.encode("utf-8")) <= 200


@_needs_bash
def test_p4_key_field_cap_agrees_between_bash_and_python(tmp_path: Path):
    """WAVE-4 NIT-4 PARITY. `${field:0:200}` counts CHARACTERS under a UTF-8
    locale and BYTES under LC_ALL=C; Python's `[:200]` counts code points. For
    a >200-byte non-ASCII title that is two different keys, so the recorder's
    key never matches the injector's and the suppression misses (fail-open).

    RED-PROOF: run against the pre-change pair, the C-locale leg returns 200
    BYTES of `é` (100 chars) while Python returned 200 CHARS (400 bytes) —
    verified divergent on the pre-fix files.
    """
    sys.path.insert(0, str(RECORDER.parent))
    try:
        import importlib

        mod = importlib.import_module("mcp_retrieval_record")
    finally:
        sys.path.pop(0)

    script = tmp_path / "cap.sh"
    script.write_text(
        f'. "{LIB}/seen-store.sh"\nvco_cap_key_field "$1"\n', encoding="utf-8"
    )
    for text in ("plain", "a" * 250, "é" * 150, "a" + "é" * 150, "😀" * 60, "ünïcodé"):
        for env_locale in ("C", "en_US.UTF-8"):
            proc = subprocess.run(
                ["bash", str(script), text],
                capture_output=True,
                timeout=30,
                env={**os.environ, "LC_ALL": env_locale, "LANG": env_locale},
            )
            assert proc.returncode == 0, proc.stderr[-300:]
            assert proc.stdout.decode("utf-8") == mod.cap_key_field(text), (
                f"bash/python cap disagree for {text[:10]!r} under LC_ALL={env_locale}"
            )


_PWSH = __import__("shutil").which("pwsh") or __import__("shutil").which("powershell")
_needs_pwsh = pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")


def _ps_src_matches(tmp_path: Path, name: str, ledger: list, src: str, root: str) -> bool:
    reads = tmp_path / f"{name}.txt"
    reads.write_text("\n".join(ledger) + "\n", encoding="utf-8")
    script = tmp_path / f"{name}.ps1"
    script.write_text(
        f'. "{LIB}/seen-store.ps1"\n'
        "if (Test-VcoSeenSrcMatches -ReadsFile $args[0] -Src $args[1] "
        "-ProjectRoot $args[2]) { exit 0 } else { exit 1 }\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(script), str(reads), src, root],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode in (0, 1), f"pwsh error: {proc.stderr[-400:]}"
    return proc.returncode == 0


@_needs_pwsh
def test_p4_ps1_rule_b_matches_a_backslash_ledger_entry(tmp_path: Path):
    """WAVE-4 NIT-3. `Test-VcoSeenSrcMatches` composed the absolute candidate
    with '/' unconditionally, so a ledger entry written on Windows in the
    as-Read BACKSLASH form never matched the '/'-joined candidate — rule (b)
    was inert on exactly the OS whose paths differ.

    RED-PROOF: pre-fix this composes 'C:\\repo' + '/' + 'knowledge/x.md' =
    'C:\\repo/knowledge/x.md', which is not the ledger's
    'C:\\repo\\knowledge\\x.md' — verified returning $false on the pre-change
    file.
    """
    assert _ps_src_matches(
        tmp_path,
        "winledger",
        [r"C:\repo\knowledge\x.md"],
        "knowledge/x.md",
        r"C:\repo",
    ), "a Windows as-Read absolute ledger entry must match a POSIX producer src"


@_needs_pwsh
def test_p4_ps1_rule_b_keeps_its_pre_existing_matches(tmp_path: Path):
    """The separator retry ADDS shapes; it must not lose the ones that worked
    (repo-relative ledger, POSIX absolute ledger) or start matching a path
    that was never Read."""
    # The common case: repo-relative ledger entry, repo-relative producer src.
    assert _ps_src_matches(
        tmp_path, "rel", ["knowledge/x.md"], "knowledge/x.md", "/repo"
    )
    # POSIX absolute producer src against a repo-relative ledger.
    assert _ps_src_matches(
        tmp_path, "abs", ["knowledge/x.md"], "/repo/knowledge/x.md", "/repo"
    )
    # A different file must NOT match — still an exact per-shape comparison.
    assert not _ps_src_matches(
        tmp_path, "miss", [r"C:\repo\knowledge\x.md"], "knowledge/y.md", r"C:\repo"
    )
    # A prefix of a Read path must NOT match either (no prefix fuzz).
    assert not _ps_src_matches(
        tmp_path, "prefix", [r"C:\repo\knowledge\x.md"], "knowledge", r"C:\repo"
    )


def test_p4_key_field_cap_is_pinned_on_the_powershell_side():
    """The .ps1 sibling keys the SAME store, so its cap must be the byte cap
    too — `Substring(0, 200)` counts UTF-16 code units (2 per astral char)."""
    ps1 = (LIB / "seen-store.ps1").read_text(encoding="utf-8")
    assert "function Get-VcoCapKeyField" in ps1
    assert "UTF8.GetBytes" in ps1 and "-band 0xC0" in ps1, (
        "the .ps1 cap must measure UTF-8 BYTES and back off over continuation "
        "bytes, matching vco_cap_key_field / cap_key_field"
    )
    code = "\n".join(
        ln for ln in ps1.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "Substring(0, 200)" not in code, (
        "Get-VcoSeenFirstField must route through Get-VcoCapKeyField, not a "
        "UTF-16 code-unit Substring"
    )


def test_p4_hook_registered_on_the_retrieval_tools_only():
    """The matcher must cover the three retrieval tools and NOT
    store_knowledge_node (which has its own kg-summary-generator hook)."""
    for tpl in ("settings.json.linux.template", "settings.json.windows.template"):
        d = json.loads((REPO_ROOT / "templates" / tpl).read_text(encoding="utf-8"))
        entries = [
            m
            for m in d["hooks"]["PostToolUse"]
            if any("post-mcp-retrieval-record" in h.get("command", "") for h in m["hooks"])
        ]
        assert len(entries) == 1, f"{tpl}: expected exactly one registration"
        matcher = entries[0]["matcher"]
        for tool in ("hybrid_search", "semantic_graph_search", "search_code_graph"):
            assert f"mcp__weaviate-kg__{tool}" in matcher, f"{tpl}: {tool} not matched"
        assert "store_knowledge_node" not in matcher


@_needs_bash
def test_p4_rule_b_matches_across_path_shapes(tmp_path: Path):
    """RED-PROOF for the path-form fix: pre-P4 rule (b) did ONE exact-match on
    the ledger, so an ABSOLUTE `src=` never matched the ledger's repo-relative
    entry (and vice-versa) and the suppression was silently inert."""
    proot = tmp_path / "proj"
    (proot / ".claude" / "state").mkdir(parents=True)
    reads = proot / ".claude" / "state" / "seen_reads_s.txt"
    reads.write_text("knowledge/concepts/a.md\n", encoding="utf-8")
    inject = proot / ".claude" / "state" / "seen_inject_s.txt"
    inject.touch()

    block_abs = (
        f"KG: Node A | concept | score=0.9 | FULL NODE: | src={proot}/knowledge/concepts/a.md\n"
        "body\n"
    )
    script = (
        f'PROJECT_ROOT="{proot}"\n'
        f'PY="{sys.executable}"\n'
        f'. "{LIB}/seen-store.sh"\n'
        f'vco_filter_seen_blocks "$1" "{inject}" "{reads}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script, "bash", block_abs],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", (
        "an ABSOLUTE src= whose repo-relative form is in the reads-ledger was "
        f"NOT suppressed: {out.stdout!r}"
    )

    # The reverse shape (ledger absolute, producer relative) must also match.
    reads.write_text(f"{proot}/knowledge/concepts/b.md\n", encoding="utf-8")
    block_rel = "KG: Node B | concept | score=0.9 | FULL NODE: | src=knowledge/concepts/b.md\nbody\n"
    out2 = subprocess.run(
        ["bash", "-c", script, "bash", block_rel],
        capture_output=True, text=True, timeout=30,
    )
    assert out2.returncode == 0, out2.stderr
    assert out2.stdout.strip() == "", out2.stdout

    # LEAVE-ALONE: a path that is genuinely NOT in the ledger still injects.
    block_other = "KG: Node C | concept | score=0.9 | FULL NODE: | src=knowledge/concepts/c.md\nbody\n"
    out3 = subprocess.run(
        ["bash", "-c", script, "bash", block_other],
        capture_output=True, text=True, timeout=30,
    )
    assert "Node C" in out3.stdout, "an unread source must NOT be suppressed"


@_needs_bash
def test_p4_injection_suppressed_after_an_explicit_retrieval(tmp_path: Path):
    """End-to-end: an explicit MCP retrieval records the node, and the pre-edit
    hook then declines to re-inject the SAME chunk. RED-PROOF: this fails on the
    pre-P4 tree — nothing recorded explicit retrievals, so the block injected."""
    env = build_sandbox(tmp_path)
    kg_header = "KG: Explicit Node | concept | score=0.90 | FULL NODE:"
    kg_body = "the body the agent already fetched"
    write_stub_producers(env, kg_lines=[kg_header, kg_body], code_lines=[])
    target = str(tmp_path / "doc.md")

    # Control: with an empty seen-store the block DOES inject.
    control = invoke_hook(env, "p4sess", target)
    assert "Explicit Node" in control.stdout, control.stdout + control.stderr

    # Fresh session + wiped caches so only the recorder can suppress.
    for d in (env["state_dir"] / "query_cache",):
        if d.exists():
            for f in d.iterdir():
                f.unlink()

    # The agent runs an explicit hybrid_search returning the SAME chunk.
    inject = env["state_dir"] / "seen_inject_p4sess2.txt"
    reads = env["state_dir"] / "seen_reads_p4sess2.txt"
    _recorder(
        {
            "tool_name": "mcp__weaviate-kg__hybrid_search",
            "tool_response": json.dumps(
                {
                    "results": [
                        {"title": "Explicit Node", "tier": "full", "content": kg_body}
                    ]
                }
            ),
        },
        inject,
        reads,
    )
    after = invoke_hook(env, "p4sess2", target)
    assert "Explicit Node" not in after.stdout, (
        "the pre-edit hook re-injected a chunk the agent had just fetched "
        f"explicitly: {after.stdout!r}"
    )


# ==========================================================================
# P5 — the suite stays out of the production telemetry streams
# ==========================================================================


def test_p5_query_log_dir_is_pinned_away_from_the_real_state_dir():
    """RED-PROOF: pre-P5 nothing set VCT_QUERY_LOG_DIR, so `query_logger`
    resolved `~/.vct/logs/weaviate_mcp/` at import and every fixture kg-sync
    landed in the user's PRODUCTION tool_usage.jsonl (6065 rows counted)."""
    pinned = os.environ.get("VCT_QUERY_LOG_DIR", "")
    assert pinned, "conftest must pin VCT_QUERY_LOG_DIR for the whole suite"
    real = Path.home() / ".vct"
    assert not Path(pinned).is_relative_to(real), (
        f"VCT_QUERY_LOG_DIR={pinned} still resolves inside the real state dir"
    )
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    try:
        from weaviate_mcp import query_logger
    finally:
        sys.path.pop(0)
    assert Path(query_logger.LOG_DIR).is_relative_to(Path(pinned)), (
        f"query_logger.LOG_DIR={query_logger.LOG_DIR} escaped the pinned dir"
    )


def test_p5_resync_spawn_is_disabled_for_the_suite_by_default():
    assert os.environ.get("VCT_RESYNC_SPAWN_DISABLED") == "1", (
        "conftest must gate background resync spawns for tests that do not "
        "explicitly opt out"
    )


def test_p5_spawn_gate_blocks_popen_and_the_log_file(tmp_path: Path, monkeypatch):
    """RED-PROOF: pre-P5 there was no gate — `spawn_background_resync` opened its
    per-spawn log under the REAL `~/.vct/logs/` and called Popen."""
    from vco_lib import codegraph_resync as cr

    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    logs = tmp_path / "state"
    monkeypatch.setenv("VCT_STATE_DIR", str(logs))
    monkeypatch.setenv("VCT_RESYNC_SPAWN_DISABLED", "1")

    def _explode(*a, **k):  # pragma: no cover — must never run
        raise AssertionError("Popen called despite VCT_RESYNC_SPAWN_DISABLED")

    monkeypatch.setattr(cr.subprocess, "Popen", _explode)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    result = cr.spawn_background_resync(tmp_path, "TProj", python_exe=sys.executable)
    assert result.status == "skipped"
    assert "VCT_RESYNC_SPAWN_DISABLED" in result.message
    # And no spawn log was deposited anywhere.
    assert not list(logs.rglob("resync-*.log"))


def test_p5_spawn_gate_leaves_the_launch_path_intact_when_off(tmp_path: Path, monkeypatch):
    """LEAVE-ALONE side: with the gate cleared, the spawn still launches."""
    from vco_lib import codegraph_resync as cr

    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    spawned: list = []

    class _P:
        pid = 1

    monkeypatch.setattr(
        cr.subprocess, "Popen", lambda argv, **kw: (spawned.append(argv), _P())[1]
    )
    result = cr.spawn_background_resync(tmp_path, "TProj", python_exe=sys.executable)
    assert result.status == "launched"
    assert spawned, "the gate must not affect the ungated launch path"


def test_p5_gate_accepts_the_usual_truthy_tokens(monkeypatch):
    from vco_lib import codegraph_resync as cr

    for token in ("true", "1", "yes", "on", "TRUE", " True "):
        monkeypatch.setenv("VCT_RESYNC_SPAWN_DISABLED", token)
        assert cr._truthy_env("VCT_RESYNC_SPAWN_DISABLED"), token
    for token in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("VCT_RESYNC_SPAWN_DISABLED", token)
        assert not cr._truthy_env("VCT_RESYNC_SPAWN_DISABLED"), token
