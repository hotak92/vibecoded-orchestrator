# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression guard for the v0.2.21/§25b pre-edit-context-inject dedup bug.

History (v0.2.21 audit, commit b09ca2a 2026-05-20):
    The pre-edit hook invoked `rl_kg_search.py` and `code-graph-query
    search` WITHOUT `--hook-format`. Both producers therefore emitted
    untagged human-readable output (no `KG:` / `CODE:` prefix). The
    hook's `_filter_seen` regex `^(KG|CODE):\\ (.+)$` (and the .ps1
    sibling's `^(KG|CODE):\\s+(.+)$`) never matched any line, so
    `current_title` stayed empty, so `_flush_block` never appended to
    `SEEN_NODES_FILE`. Live evidence: every per-session
    `.claude/state/seen_kg_titles_<sid>.txt` was 0 bytes despite dozens
    of Edit invocations.

This test ships a behavioural regression guard: it actually invokes the
`.sh` hook against in-process stub producers (no Weaviate, no Python
venv beyond `python3` on PATH) and asserts that:

  1. The first Edit emits a non-empty pre-edit context block (positive
     control — proves the hook actually fires + emits).
  2. `SEEN_NODES_FILE` accumulates the producer's titles after the first
     Edit (the bytes-on-disk smoke that the §25b bug killed).
  3. A second Edit with a DIFFERENT file path in the same session
     suppresses the now-seen titles (the dedup contract).
  4. The same is true when the producer emits the canonical
     `KG: no-results | ...` / `CODE: no-results | ...` lines that the
     real producers emit on empty Weaviate hits — covers the most
     common real-world steady state (cf. the post-fix per-session state
     files which contain `no-results\\n`, 11 bytes).

The test only exercises the `.sh` hook because:
  - It runs on Linux/macOS CI runners; the `.ps1` path requires
    PowerShell + Windows-conventional temp paths.
  - The body-parity test suite (`test_hook_ps1_body_parity.py` +
    `pre-edit-context-inject.ps1`'s own parity-confirmation header
    comment) covers structural parity of the `.ps1` mirror.
  - For an end-to-end .ps1 verification we'd need a Windows CI runner
    or pwsh-on-Linux, neither of which is in the v0.2.22 budget.

Diagnostic env var: `VCO_HOOK_TRACE=1` enables a `set -x` trace dumped
to `${TMPDIR:-/tmp}/preedit-trace-<ns>-<pid>.log` (commit `0b87ab6`,
v0.2.21). Useful when this test fails: rerun with
`VCO_HOOK_TRACE=1 pytest -k dedup_regression -s` and inspect the trace
file path printed to stderr.

Constraints respected:
  - No real Weaviate / Ollama / RL-server dependency: producers are
    bash stubs that emit one canned line each.
  - No network or filesystem outside ``tmp_path``.
  - Python 3.x on PATH (`/usr/bin/env python3`); no venv build.
  - Linux/macOS-only (Windows CI sees the `.ps1` body-parity coverage).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pre_edit_hook_sandbox import (
    build_sandbox,
    invoke_hook,
    write_stub_producers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC = REPO_ROOT / "templates" / "hooks" / "pre-edit-context-inject.sh"

_IS_WINDOWS = platform.system() == "Windows"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


@pytest.fixture
def hook_env(tmp_path: Path):
    """Build a sandboxed VCT_INSTALL_ROOT layout that satisfies the
    hook's path probes without touching the real project tree.

    v0.2.91: the layout + stub producers + invoker moved into
    ``tests/common/pre_edit_hook_sandbox.py`` when a second suite
    (``test_v0291_perf_quickwins.py``) needed the same rig — one home, two
    callers. This fixture is now a thin wrapper over ``build_sandbox``.
    """
    yield build_sandbox(tmp_path)


def _write_stub_producers(env, kg_lines: list[str], code_lines: list[str]) -> None:
    """Thin delegator to the shared harness (see the module note above)."""
    write_stub_producers(env, kg_lines, code_lines)


def _invoke_hook(env, session_id: str, file_path: str) -> subprocess.CompletedProcess:
    """Thin delegator to the shared harness (see the module note above)."""
    return invoke_hook(env, session_id, file_path)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by body-parity tests")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_seen_nodes_file_grows_after_first_edit(hook_env, tmp_path):
    """Bug §25b smoke: after one Edit that fires real producers, the
    seen_kg_titles file must contain the producer's titles.

    Pre-fix (commit b09ca2a) this file stayed 0 bytes because the
    producers weren't being asked for hook-format output, the regex
    didn't match any line, and `_flush_block` never appended.
    """
    _write_stub_producers(
        hook_env,
        kg_lines=[
            "KG: Sample Node A | concept | score=0.85 | FULL NODE:",
            "body line 1",
            "body line 2",
        ],
        code_lines=[
            "CODE: sample.module.func_a | CodeFunction | distance=0.20 |",
            "code body line",
        ],
    )

    result = _invoke_hook(hook_env, "sess-grow-test", str(tmp_path / "foo.py"))
    assert result.returncode == 0, f"hook failed: stderr={result.stderr!r}"

    # v0.2.70 Stream E: the unified store is seen_inject_<sid>.txt (renamed
    # from seen_kg_titles_<sid>.txt) and KG keys are now per-chunk
    # ("<title>#<sha1(body)>") while CODE keys stay per-entity (full_name).
    # The substring assertions still hold (the title/full_name prefix the key).
    seen_file = hook_env["state_dir"] / "seen_inject_sess-grow-test.txt"
    assert seen_file.exists(), "seen file must be touched even when empty"
    content = seen_file.read_text(encoding="utf-8")
    assert content, "seen file must accumulate keys after first emit (§25b smoke)"
    assert "Sample Node A" in content, (
        f"KG title not recorded — vco_filter_seen_blocks append broken "
        f"(content={content!r})"
    )
    assert "sample.module.func_a" in content, (
        f"CODE full_name not recorded — vco_filter_seen_blocks append broken "
        f"(content={content!r})"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by body-parity tests")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_second_edit_suppresses_already_seen_titles(hook_env, tmp_path):
    """Dedup contract: a second Edit (different file, same session)
    must suppress titles seen on the first Edit.

    The hook emits the PreToolUse JSON envelope only when there is at
    least one non-empty result block after dedup. So with both KG and
    CODE titles already in the seen file, the second invocation must
    produce empty stdout (no envelope at all).
    """
    _write_stub_producers(
        hook_env,
        kg_lines=[
            "KG: Sample Node B | concept | score=0.85 | FULL NODE:",
            "body content",
        ],
        code_lines=[
            "CODE: sample.module.func_b | CodeFunction | distance=0.25 |",
            "code body",
        ],
    )

    # First Edit: emits context, populates seen file.
    first = _invoke_hook(hook_env, "sess-dedup-test", str(tmp_path / "foo.py"))
    assert first.returncode == 0
    assert first.stdout, "first edit must emit at least one block"
    assert "Sample Node B" in first.stdout

    # Second Edit: different file path → bypasses per-file cache → fresh
    # producer call → producers emit the SAME lines (stubbed). Dedup
    # against seen file must suppress every block.
    second = _invoke_hook(hook_env, "sess-dedup-test", str(tmp_path / "bar.py"))
    assert second.returncode == 0
    assert second.stdout == "" or second.stdout.isspace(), (
        f"second edit must emit nothing (dedup contract); "
        f"got stdout={second.stdout!r}"
    )

    # Seen file should still contain both keys (no duplicate writes).
    # v0.2.70 Stream E: store renamed to seen_inject_<sid>.txt; KG key is
    # "<title>#<sha1(body)>" so the title appears exactly once.
    seen = (hook_env["state_dir"] / "seen_inject_sess-dedup-test.txt").read_text("utf-8")
    assert seen.count("Sample Node B") == 1, (
        f"duplicate KG key write in seen file: {seen!r}"
    )
    assert seen.count("sample.module.func_b") == 1, (
        f"duplicate CODE key write in seen file: {seen!r}"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by body-parity tests")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_no_results_lines_dedup_correctly(hook_env, tmp_path):
    """Real-world steady state: producers emit `KG: no-results | ...`
    and `CODE: no-results | ...` when Weaviate returns nothing
    (rl_kg_search.py / query_code_graph.py post-b09ca2a).

    Verify both lines land in the seen file on the first Edit, and the
    second Edit suppresses them — yielding the canonical 11-byte
    (`no-results\\n`) steady state observed in real per-session state
    files on this machine 2026-05-20.
    """
    _write_stub_producers(
        hook_env,
        kg_lines=["KG: no-results | query='whatever' | limit=1"],
        code_lines=[
            "CODE: no-results | collection=CodeFunction | "
            "project=orchestrator-root | query='whatever'"
        ],
    )

    # First Edit: emits at least the KG no-results block.
    first = _invoke_hook(hook_env, "sess-noresults-test", str(tmp_path / "a.py"))
    assert first.returncode == 0
    assert "no-results" in first.stdout, (
        f"first edit must surface the no-results identifier "
        f"(stdout={first.stdout!r})"
    )

    # v0.2.70 Stream E: store renamed to seen_inject_<sid>.txt. The keys are
    # now distinct per producer kind: the KG no-results block (no body line)
    # keys as "no-results#<sha1('')>"; the CODE no-results block keys per-entity
    # as the bare full_name "no-results". So both are recorded once each (no
    # longer the single collapsed "no-results\n" entry — that was the old
    # title-coarse behavior). The dedup CONTRACT (second edit suppressed) below
    # is the load-bearing assertion.
    seen = hook_env["state_dir"] / "seen_inject_sess-noresults-test.txt"
    content = seen.read_text("utf-8")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    assert "no-results" in content, (
        f"expected a 'no-results' key in seen file; got {content!r}"
    )
    # CODE key is the bare full_name.
    assert "no-results" in lines, (
        f"expected the bare CODE 'no-results' key; got {lines!r}"
    )
    # KG key is the per-chunk form (title#hash).
    assert any(ln.startswith("no-results#") for ln in lines), (
        f"expected a per-chunk KG 'no-results#<hash>' key; got {lines!r}"
    )

    # Second Edit: same session, different file. KG + CODE no-results
    # both deduped → empty stdout.
    second = _invoke_hook(hook_env, "sess-noresults-test", str(tmp_path / "b.py"))
    assert second.returncode == 0
    assert second.stdout == "" or second.stdout.isspace(), (
        f"second edit must be fully suppressed (got {second.stdout!r})"
    )


def _write_counting_kg_producer(env, marker: Path) -> None:
    """Install a KG producer that appends one byte to `marker` on every
    invocation, then emits one canned KG block. Lets a test count how
    many times the hook actually launched the (expensive) search — the
    load-bearing signal for the v0.2.77 Part-9 cache-serves-before-search
    fix. Also install a no-op code-graph producer so the code branch is
    inert (we only care about the KG search launch here).
    """
    rl = env["scripts_dir"] / "rl_kg_search.py"
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, argparse\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('query')\n"
        "ap.add_argument('--limit', type=int, default=1)\n"
        "ap.add_argument('--hook-format', action='store_true')\n"
        "args = ap.parse_args()\n"
        # Record the invocation UNCONDITIONALLY (before the --hook-format
        # gate) so we count every launch, hook-format or not.
        f"open({str(marker)!r}, 'a').write('x')\n"
        "if not args.hook_format:\n"
        "    sys.exit(0)\n"
        "print('KG: Cache Probe Node | concept | score=0.90 | FULL NODE:')\n"
        "print('probe body line')\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cg = env["cg_dir"] / "code-graph-query"
    cg.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    cg.chmod(cg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by body-parity tests")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_cache_hit_served_without_relaunching_search(hook_env, tmp_path):
    """v0.2.77 Part 9 task 1 ACT test: a SECOND identical-payload Edit
    (same session, same file) within the 10-min TTL must be served from
    the per-file cache WITHOUT re-launching the KG search subprocess.

    Pre-fix the cache-replay branch sat AFTER the search launch+wait, so a
    warm edit still paid the full search cost (audit 2026-07-11: warm
    1431 ms ≈ cold 1440 ms) and threw the fresh results away. This test
    pins the fix behaviourally: the KG producer's invocation marker must
    show exactly ONE launch across two invocations, and the cache log must
    record a `miss` then a `hit`.

    NOTE ON DEDUP: the pre-edit dedup store would normally suppress the
    already-seen node on the second (replay) invocation, yielding empty
    stdout. To make the replay EMIT (so we prove the replay path runs, not
    just that it exits), the two invocations use DIFFERENT session ids for
    the seen-store while sharing nothing else — but the per-file cache is
    keyed on (session, file). So instead we assert the load-bearing signal
    directly: the search subprocess launched exactly once, and the cache
    log shows miss->hit. Same session + same file guarantees the same
    cache path.
    """
    marker = tmp_path / "kg_launch_marker"
    _write_counting_kg_producer(hook_env, marker)

    target = str(tmp_path / "probe.py")
    session = "sess-cache-act"

    first = _invoke_hook(hook_env, session, target)
    assert first.returncode == 0, f"first hook run failed: {first.stderr!r}"
    assert marker.exists(), "KG search must have launched on the cold (miss) run"
    launches_after_first = len(marker.read_text("utf-8"))
    assert launches_after_first == 1, (
        f"expected exactly 1 KG launch on the cold run, got {launches_after_first}"
    )

    # Second identical invocation — must be served from cache.
    second = _invoke_hook(hook_env, session, target)
    assert second.returncode == 0, f"second hook run failed: {second.stderr!r}"
    launches_after_second = len(marker.read_text("utf-8"))
    assert launches_after_second == 1, (
        "cache HIT must NOT relaunch the KG search — expected the launch "
        f"marker to stay at 1, got {launches_after_second}. This is the "
        "v0.2.77 Part-9 regression: replay running after the search."
    )

    # Cache log observability: one miss then one hit for this session.
    log = hook_env["state_dir"] / "preedit_cache_log.jsonl"
    assert log.exists(), "pre-edit cache log must be written (hit/miss observability)"
    entries = [
        json.loads(ln) for ln in log.read_text("utf-8").splitlines() if ln.strip()
    ]
    statuses = [e["status"] for e in entries if e.get("session") == session]
    assert statuses == ["miss", "hit"], (
        f"expected miss->hit for session {session}; got {statuses!r}"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by body-parity tests")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_cache_miss_cold_path_output_unchanged(hook_env, tmp_path):
    """Leave-alone control for task 1: the COLD (cache-miss) path must
    still emit the pre-edit context block exactly as before — the reorder
    only changed WHEN the replay branch runs, not the miss-path output.
    """
    _write_stub_producers(
        hook_env,
        kg_lines=[
            "KG: Cold Path Node | concept | score=0.88 | FULL NODE:",
            "cold body",
        ],
        code_lines=[],
    )
    result = _invoke_hook(hook_env, "sess-cold-path", str(tmp_path / "cold.py"))
    assert result.returncode == 0, f"hook failed: {result.stderr!r}"
    assert "Cold Path Node" in result.stdout, (
        f"cold miss path must still emit the KG block (stdout={result.stdout!r})"
    )
    assert "[Pre-edit context for cold.py]" in result.stdout, (
        "cold miss path must still emit the standard context header"
    )


# --------------------------------------------------------------------------
# Static body-parity guards (so future edits don't silently drop the fix)
# --------------------------------------------------------------------------


def _producer_invocation_lines(body: str, needle: str) -> list[tuple[int, str]]:
    """Return (line_index, line) tuples where `needle` appears in an
    EXECUTABLE line (not a comment).

    The hook has extensive comments mentioning `rl_kg_search.py` and
    `code-graph-query` in design notes — we want to assert
    --hook-format is paired with the REAL invocation lines, not those
    comments. A line is considered a comment if its first
    non-whitespace character is `#`.
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(body.splitlines()):
        if needle not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append((i, line))
    return out


def test_sh_hook_passes_hook_format_to_rl_kg_search() -> None:
    """The b09ca2a fix passes --hook-format to rl_kg_search.py so the
    producer emits `KG: <title>` prefixes the hook's _filter_seen
    regex `^(KG|CODE):\\ (.+)$` can match.

    Dropping this flag is exactly the regression we're guarding against.
    """
    body = HOOK_SRC.read_text(encoding="utf-8")
    invocations = _producer_invocation_lines(body, "rl_kg_search.py")
    assert invocations, (
        "rl_kg_search.py invocation missing entirely (no executable line "
        "mentions the producer — only comments)."
    )
    lines = body.splitlines()
    for i, _ in invocations:
        # The producer call is a multi-line pipeline; --hook-format must
        # appear in the next ~3 lines (same continuation).
        window = "\n".join(lines[i: min(len(lines), i + 4)])
        if "--hook-format" in window:
            continue
        # v0.2.91 P2: the merged single-interpreter path passes the SCRIPT PATH
        # to vco_dual_search_cached, which forwards it to hook_dual_search.py —
        # and THAT is where --hook-format is applied. The flag guarantee for this
        # call-site is enforced by
        # test_v0291_perf_quickwins.py::test_dual_driver_passes_hook_format_to_both_legs
        # (which asserts the driver's argv), not by a textual window here.
        preceding = "\n".join(lines[max(0, i - 6): i + 4])
        assert "vco_dual_search_cached" in preceding, (
            f"rl_kg_search.py invocation at line {i + 1} missing --hook-format "
            f"in continuation window — silently breaks in-session dedup "
            f"(cf. v0.2.21 commit b09ca2a, plan §25b):\n{window}"
        )


def test_sh_hook_passes_hook_format_to_code_graph_query() -> None:
    """Mirror of the rl_kg_search guard for the code-graph producer.

    Without --hook-format, query_code_graph.py emits an unprefixed
    banner block on every search — bypassing the hook's
    `_filter_seen` and re-injecting the same code-graph hits on every
    Edit.
    """
    body = HOOK_SRC.read_text(encoding="utf-8")
    # We want the line that's specifically the producer INVOCATION
    # (`code-graph-query" search "$QUERY" ...`), not the various
    # mentions in comments.
    invocations = [
        (i, l) for i, l in _producer_invocation_lines(body, "code-graph-query")
        if "search " in l or " search" in l
    ]
    assert invocations, (
        "code-graph-query search invocation missing entirely (no executable "
        "line invokes the producer's `search` subcommand)."
    )
    lines = body.splitlines()
    for i, _ in invocations:
        window = "\n".join(lines[i: min(len(lines), i + 4)])
        assert "--hook-format" in window, (
            f"code-graph-query search invocation at line {i + 1} missing "
            f"--hook-format — silently breaks in-session dedup "
            f"(cf. v0.2.21 commit b09ca2a, plan §25b):\n{window}"
        )


def test_sh_hook_appends_titles_to_seen_nodes_file() -> None:
    """Static guard: the `_flush_block` codepath that appends
    `$current_title` to `$SEEN_NODES_FILE` must exist verbatim.

    The §25b investigation showed that without this append happening,
    the file stays 0 bytes and dedup is silently broken.
    """
    body = HOOK_SRC.read_text(encoding="utf-8")
    assert 'echo "$current_title" >> "$SEEN_NODES_FILE"' in body, (
        "pre-edit-context-inject.sh missing the SEEN_NODES_FILE append "
        "in _flush_block — dedup would silently break (cf. v0.2.21 §25b)."
    )


def test_sh_hook_filter_seen_regex_matches_producer_format() -> None:
    """The regex `^(KG|CODE):\\ (.+)$` must be present and match the
    producer's `KG: <title> | ...` / `CODE: <full_name> | ...` shape.

    If a future edit loosens this regex (or tightens it incorrectly),
    the producer output won't be recognised as headers and dedup
    silently fails again.
    """
    body = HOOK_SRC.read_text(encoding="utf-8")
    assert "^(KG|CODE):\\ (.+)$" in body, (
        "pre-edit-context-inject.sh missing the canonical header regex "
        "`^(KG|CODE):\\ (.+)$` — silently breaks dedup if producer "
        "format changes without the regex following."
    )


def test_sh_hook_trace_diagnostic_gate_present() -> None:
    """`VCO_HOOK_TRACE=1` enables a `set -x` trace dump to a tempfile,
    documented as the standard diagnostic for any future dedup-style
    regression (cf. plan §6 investigation path).

    Keeping the gate is part of the v0.2.22 contract — removing it would
    erase the on-machine root-cause tool for the next regression.
    """
    body = HOOK_SRC.read_text(encoding="utf-8")
    assert "VCO_HOOK_TRACE" in body, (
        "pre-edit-context-inject.sh dropped the VCO_HOOK_TRACE diagnostic "
        "gate — keep it so future agents can `set -x` the hook without "
        "modifying production code."
    )
    assert "preedit-trace-" in body, (
        "VCO_HOOK_TRACE trace file naming pattern lost; the documented "
        "diagnostic path `${TMPDIR}/preedit-trace-<ns>-<pid>.log` must "
        "stay stable so the plan/KG instructions remain valid."
    )


def test_ps1_hook_passes_hook_format_to_both_producers() -> None:
    """PowerShell sibling must mirror the .sh fix.

    The .ps1 hook's body-parity comment (top of the file) explicitly
    confirms the dedup-correctness fingerprints from PR #186 — this
    test asserts the --hook-format flag is in the .ps1 too, near the
    actual producer invocations (not just in the comment block).
    """
    ps1 = (REPO_ROOT / "templates" / "hooks" / "pre-edit-context-inject.ps1").read_text("utf-8")
    assert "--hook-format" in ps1, (
        "pre-edit-context-inject.ps1 missing --hook-format entirely — "
        "cross-OS dedup parity broken."
    )
    # Find the REAL invocation (PowerShell uses `& $VenvPy $RlScript $Query
    # --limit 1 --hook-format`). Comments use `#` and the top-of-file
    # parity block mentions producer names too, so we strip comment lines
    # before pattern matching.
    non_comment_lines = [
        l for l in ps1.splitlines()
        if not l.lstrip().startswith("#")
    ]
    non_comment_body = "\n".join(non_comment_lines)

    assert "$RlScript" in non_comment_body or "rl_kg_search" in non_comment_body, (
        "pre-edit-context-inject.ps1 missing the RL/KG producer "
        "invocation — cross-OS dedup parity broken."
    )
    # The PowerShell invocation line typically reads:
    #   & $VenvPy $RlScript $Query --limit 1 --hook-format 2>$null | ...
    # Find any non-comment line with both $RlScript and --hook-format
    # (or a fallback pair: `rl_kg_search` + `--hook-format`).
    kg_paired = any(
        ("$RlScript" in l or "rl_kg_search" in l) and "--hook-format" in l
        for l in non_comment_lines
    )
    assert kg_paired, (
        "pre-edit-context-inject.ps1 RL/KG producer invocation missing "
        "--hook-format on the same line — cross-OS dedup parity broken "
        "(cf. v0.2.21 commit b09ca2a)."
    )

    # Code-graph: PowerShell invokes via `$cgQueryPs1` / `$cgQuerySh`.
    cg_paired = any(
        ("code-graph-query" in l or "cgQuery" in l) and "--hook-format" in l
        for l in non_comment_lines
    )
    assert cg_paired, (
        "pre-edit-context-inject.ps1 code-graph-query invocation missing "
        "--hook-format on the same line — cross-OS dedup parity broken "
        "(cf. v0.2.21 commit b09ca2a)."
    )
