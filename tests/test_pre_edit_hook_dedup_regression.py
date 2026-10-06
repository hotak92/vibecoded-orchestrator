# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""pre-edit hook dedup/replay regression suite.

History: this file pinned the v0.2.21 §25b dedup fix (producers must be asked
for --hook-format; the seen-store append must exist; the header regex must
match the producer format) and the v0.2.77 Part-9 cache-serves-before-search
fix, driving the REAL hook through ``tests/common/pre_edit_hook_sandbox.py``.

v0.2.101 Wave 2 RETARGET: ``pre-edit-context-inject.sh`` became a thin
wrapper around ``hook_context_router.py`` (PLAN-V02101 §C3). The properties
this suite guards are unchanged, but their mechanisms moved:

  * the producer --hook-format contract is now the ROUTER's argv (pinned
    behaviourally by tests/test_v02101_router_surfaces.py and
    tests/test_v02101_edit_query_rework.py);
  * the seen-store append + header regex live in the router's Python filter,
    byte-compatible with _lib/seen-store.sh (pinned by
    tests/test_v02101_inject_gates.py::TestSeenStoreParity);
  * the per-file REPLAY CACHE stayed in the wrapper (§C3 "keep its per-file
    replay cache") — its miss→hit behaviour and the no-relaunch guarantee
    are still driven HERE, end-to-end through the real hook + real router +
    stub producers.

So this file keeps the behavioural contracts (seen-file growth, second-edit
suppression, replay-without-relaunch, cold-path output) and one NEW
retirement pin (the wrapper must go through the router, never back to
direct producer calls). The four static --hook-format/append/regex pins
were retired WITH the code lines they guarded — superseded by the
behavioural pins named above.
"""
from __future__ import annotations

import json
import platform
import shutil
import stat
from pathlib import Path

import pytest

from tests.common.pre_edit_hook_sandbox import (
    build_sandbox,
    install_dual_driver,
    install_router,
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
    """Sandboxed VCT_INSTALL_ROOT layout: the REAL hook + the REAL router +
    the REAL dual-search mechanism (imported by the router) + stub producers.

    v0.2.101: install_router joined the layout — the hook resolves
    hook_context_router.py from $VCT_INSTALL_ROOT, and the router imports
    hook_dual_search from beside itself, so both real files ship into the
    sandbox. The router's ``vco_lib`` imports resolve from the checkout via
    invoke_hook's PYTHONPATH pin.
    """
    env = build_sandbox(tmp_path)
    install_dual_driver(env)
    install_router(env)
    yield env


def _make_editable(target: Path, body: str = None) -> str:
    """A real file the router's enclosing-symbol extraction can walk."""
    target.write_text(
        body if body is not None else
        "def shared_target():\n    sentinel = 1\n    return sentinel\n",
        encoding="utf-8",
    )
    return str(target)


# --------------------------------------------------------------------------
# Behavioural contracts (the §25b + v0.2.77 properties, router-era)
# --------------------------------------------------------------------------


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression — .ps1 covered by check_hook_parity + router_surfaces ps1 rows")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_seen_nodes_file_grows_after_first_edit(hook_env, tmp_path):
    """§25b smoke, router era: after one Edit that fires real producers, the
    seen_inject store must contain the KG title key AND the CODE full_name
    key — written by the ROUTER's Python filter in the shell-compatible
    format (the wrapper's replay path reads the same file)."""
    write_stub_producers(
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
    target = _make_editable(tmp_path / "foo.py")

    result = invoke_hook(hook_env, "sess-grow-test", target,
                         old_string="sentinel = 1")
    assert result.returncode == 0, f"hook failed: stderr={result.stderr!r}"

    seen_file = hook_env["state_dir"] / "seen_inject_sess-grow-test.txt"
    assert seen_file.exists(), "seen file must be touched even when empty"
    content = seen_file.read_text(encoding="utf-8")
    assert content, "seen file must accumulate keys after first emit (§25b smoke)"
    assert "Sample Node A" in content, (
        f"KG title not recorded by the router's filter (content={content!r})"
    )
    assert "sample.module.func_a" in content, (
        f"CODE full_name not recorded by the router's filter (content={content!r})"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_second_edit_suppresses_already_seen_titles(hook_env, tmp_path):
    """Dedup contract: a second Edit (different file, same session) must
    suppress titles seen on the first Edit — across the router's Python
    filter (run 1) and both the router's filter and the wrapper's replay
    filter (run 2). One key each, no duplicate writes."""
    write_stub_producers(
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
    foo = _make_editable(tmp_path / "foo.py")
    bar = _make_editable(tmp_path / "bar.py")

    first = invoke_hook(hook_env, "sess-dedup-test", foo, old_string="sentinel = 1")
    assert first.returncode == 0
    assert first.stdout, "first edit must emit at least one block"
    assert "Sample Node B" in first.stdout

    # Second Edit: different file path → bypasses the per-file cache → fresh
    # router run → stub producers emit the SAME lines. Dedup against the
    # seen file must suppress every block.
    second = invoke_hook(hook_env, "sess-dedup-test", bar, old_string="sentinel = 1")
    assert second.returncode == 0
    assert second.stdout == "" or second.stdout.isspace(), (
        f"second edit must emit nothing (dedup contract); "
        f"got stdout={second.stdout!r}"
    )

    seen = (hook_env["state_dir"] / "seen_inject_sess-dedup-test.txt").read_text("utf-8")
    assert seen.count("Sample Node B") == 1, (
        f"duplicate KG key write in seen file: {seen!r}"
    )
    assert seen.count("sample.module.func_b") == 1, (
        f"duplicate CODE key write in seen file: {seen!r}"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_no_results_lines_dedup_correctly(hook_env, tmp_path):
    """Steady state: a producer `KG: no-results | ...` identifier line is
    deduped like any block — recorded once, suppressed on the second Edit.

    v0.2.101 note: the CODE-side `no-results` sentinel is UNREACHABLE on
    this surface now — the edit profile's CG leg is an exact structure
    lookup that stays SILENT on a miss (no sentinel), and rl_kg_search's
    injection-profile mode also suppresses its no-results line (the router
    reads silence as "below the floor"). The stub-driven KG line below
    still pins the block-dedup mechanism the sentinels used to exercise."""
    write_stub_producers(
        hook_env,
        kg_lines=["KG: no-results | query='whatever' | limit=1"],
        code_lines=[],
    )

    first = invoke_hook(hook_env, "sess-noresults-test", str(tmp_path / "a.py"))
    assert first.returncode == 0
    assert "no-results" in first.stdout, (
        f"first edit must surface the no-results identifier "
        f"(stdout={first.stdout!r})"
    )

    seen = hook_env["state_dir"] / "seen_inject_sess-noresults-test.txt"
    content = seen.read_text("utf-8")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    assert any(ln.startswith("no-results#") for ln in lines), (
        f"expected the per-chunk KG 'no-results#<hash>' key; got {lines!r}"
    )

    second = invoke_hook(hook_env, "sess-noresults-test", str(tmp_path / "b.py"))
    assert second.returncode == 0
    assert second.stdout == "" or second.stdout.isspace(), (
        f"second edit must be fully suppressed (got {second.stdout!r})"
    )


def _write_counting_kg_producer(env, marker: Path) -> None:
    """MODULE-shaped KG producer (the router loads it in-process and calls
    ``main()`` with the pinned argv): appends one byte to ``marker`` per
    invocation, then emits one canned KG block when --hook-format is set."""
    rl = env["scripts_dir"] / "rl_kg_search.py"
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('query')\n"
        "    ap.add_argument('--limit', type=int, default=1)\n"
        "    ap.add_argument('--hook-format', action='store_true')\n"
        "    ap.add_argument('--injection-profile', default=None)\n"
        "    ap.add_argument('--task-type', default=None)\n"
        "    ap.add_argument('--transcript', default=None)\n"
        "    args = ap.parse_args()\n"
        # Record the invocation UNCONDITIONALLY (before the --hook-format
        # gate) so we count every launch, hook-format or not.
        f"    open({str(marker)!r}, 'a').write('x')\n"
        "    if not args.hook_format:\n"
        "        return 0\n"
        "    print('KG: Cache Probe Node | concept | score=0.90 | FULL NODE:')\n"
        "    print('probe body line')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cg = env["cg_dir"] / "code-graph-query"
    cg.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    cg.chmod(cg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_cache_hit_served_without_relaunching_search(hook_env, tmp_path):
    """v0.2.77 Part 9 task 1 ACT test (router era): a SECOND identical-payload
    Edit (same session, same file) within the TTL must be served from the
    per-file replay cache WITHOUT re-spawning the router (hence without
    re-launching the KG producer). The cache log must record miss→hit."""
    marker = tmp_path / "kg_launch_marker"
    _write_counting_kg_producer(hook_env, marker)

    target = str(tmp_path / "probe.py")
    session = "sess-cache-act"

    first = invoke_hook(hook_env, session, target)
    assert first.returncode == 0, f"first hook run failed: {first.stderr!r}"
    assert marker.exists(), "KG search must have launched on the cold (miss) run"
    launches_after_first = len(marker.read_text("utf-8"))
    assert launches_after_first == 1, (
        f"expected exactly 1 KG launch on the cold run, got {launches_after_first}"
    )

    second = invoke_hook(hook_env, session, target)
    assert second.returncode == 0, f"second hook run failed: {second.stderr!r}"
    launches_after_second = len(marker.read_text("utf-8"))
    assert launches_after_second == 1, (
        "cache HIT must NOT relaunch the router/KG search — expected the "
        f"marker to stay at 1, got {launches_after_second}."
    )

    log = hook_env["state_dir"] / "preedit_cache_log.jsonl"
    assert log.exists(), "pre-edit cache log must be written (hit/miss observability)"
    entries = [
        json.loads(ln) for ln in log.read_text("utf-8").splitlines() if ln.strip()
    ]
    statuses = [e["status"] for e in entries if e.get("session") == session]
    assert statuses == ["miss", "hit"], (
        f"expected miss->hit for session {session}; got {statuses!r}"
    )


@pytest.mark.skipif(_IS_WINDOWS, reason=".sh hook regression")
@pytest.mark.skipif(not _has_bash(), reason="bash required for shell hook")
def test_cache_miss_cold_path_output_unchanged(hook_env, tmp_path):
    """Leave-alone control: the COLD (cache-miss) path still emits the
    pre-edit context block exactly as before — envelope header + producer
    blocks."""
    write_stub_producers(
        hook_env,
        kg_lines=[
            "KG: Cold Path Node | concept | score=0.88 | FULL NODE:",
            "cold body",
        ],
        code_lines=[],
    )
    result = invoke_hook(hook_env, "sess-cold-path", str(tmp_path / "cold.py"))
    assert result.returncode == 0, f"hook failed: {result.stderr!r}"
    assert "Cold Path Node" in result.stdout, (
        f"cold miss path must still emit the KG block (stdout={result.stdout!r})"
    )
    assert "[Pre-edit context for cold.py]" in result.stdout, (
        "cold miss path must still emit the standard context header"
    )


# --------------------------------------------------------------------------
# Static guards (router era)
# --------------------------------------------------------------------------


def test_sh_hook_trace_diagnostic_gate_present() -> None:
    """`VCO_HOOK_TRACE=1` enables a `set -x` trace dump to a tempfile,
    documented as the standard diagnostic for any future dedup-style
    regression (cf. plan §6 investigation path). Keeping the gate is part
    of the v0.2.22 contract — the wrapper rework must not drop it."""
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


def test_sh_hook_goes_through_the_router_not_direct_producers() -> None:
    """v0.2.101 retirement pin (replaces the four static --hook-format /
    seen-append / regex pins, which guarded code lines the rework removed):
    the wrapper must resolve and run hook_context_router.py, and must NOT
    call rl_kg_search.py / code-graph-query directly any more — a direct
    producer call beside the router would double-inject and bypass the
    §2.1 gates."""
    body = HOOK_SRC.read_text(encoding="utf-8")
    executable = [
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    ]
    exe_body = "\n".join(executable)
    assert "hook_context_router.py" in exe_body, (
        "the wrapper must run the router (executable line, not a comment)"
    )
    assert "rl_kg_search" not in exe_body, (
        "direct KG producer call crept back into the wrapper — the router "
        "owns the producer argv (--hook-format/--injection-profile/--task-type)"
    )
    assert "code-graph-query" not in exe_body, (
        "direct code-graph producer call crept back into the wrapper"
    )
