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

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC = REPO_ROOT / "templates" / "hooks" / "pre-edit-context-inject.sh"

_IS_WINDOWS = platform.system() == "Windows"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


@pytest.fixture
def hook_env(tmp_path: Path):
    """Build a sandboxed VCT_INSTALL_ROOT layout that satisfies the
    hook's path probes without touching the real project tree.

    Returns a dict with paths the individual tests need to assemble
    stdin payloads + assert against state files.
    """
    install_root = tmp_path / "install"
    (install_root / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    (install_root / ".claude" / "scripts").mkdir(parents=True)
    (install_root / ".claude" / "state").mkdir(parents=True)
    (install_root / "templates" / "hooks" / "_lib").mkdir(parents=True)

    # Stub _lib helpers (the hook sources them when present, falls back
    # to no-ops when absent; here we ship explicit no-op + emit-context
    # shim so the JSON envelope reaches our captured stdout).
    (install_root / "templates" / "hooks" / "_lib" / "stderr-cap.sh").write_text(
        "# noop stderr-cap stub\n", encoding="utf-8"
    )
    (install_root / "templates" / "hooks" / "_lib" / "emit-context.sh").write_text(
        # Minimal emit_additional_context that wraps the context in the
        # PreToolUse JSON envelope on stdout. Mirrors the production
        # helper's contract (whitespace-only context → no emit).
        "emit_additional_context() {\n"
        '    local ctx="$1"; local phase="$2"\n'
        "    case \"$ctx\" in\n"
        "        *[![:space:]]*) ;;\n"
        "        *) return 0 ;;\n"
        "    esac\n"
        "    local json_ctx\n"
        "    json_ctx=$(printf '%s' \"$ctx\" | python3 -c "
        "'import sys,json; print(json.dumps(sys.stdin.read()))')\n"
        "    printf '{\"hookSpecificOutput\":{\"additionalContext\":%s,"
        '"hookEventName":"%s"}}\\n\' "$json_ctx" "$phase"\n'
        "}\n",
        encoding="utf-8",
    )
    (install_root / "templates" / "hooks" / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )

    # detect-project stub — the hook sources it but we don't need
    # multi-codebase detection for the dedup test.
    (install_root / ".claude" / "scripts" / "detect-project.sh").write_text(
        "detect_project_for_file() { echo \"\"; }\n", encoding="utf-8"
    )

    # Fake .venv pointing at system python3 (the hook resolves the venv
    # in the same parent dir as where the producers live).
    venv_bin = install_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    system_python = shutil.which("python3") or sys.executable
    os.symlink(system_python, venv_bin / "python")

    # Copy the production hook into the sandbox so its
    # `dirname "${BASH_SOURCE[0]}"`-based path probes resolve into our
    # stub _lib + scripts trees rather than the real repo.
    sandbox_hook = install_root / "templates" / "hooks" / "pre-edit-context-inject.sh"
    sandbox_hook.write_bytes(HOOK_SRC.read_bytes())
    sandbox_hook.chmod(0o755)

    yield {
        "install_root": install_root,
        "hook_path": sandbox_hook,
        "state_dir": install_root / ".claude" / "state",
        "scripts_dir": install_root / "claude_mcp_servers" / "scripts",
        "cg_dir": install_root / ".claude" / "scripts",
    }


def _write_stub_producers(env, kg_lines: list[str], code_lines: list[str]) -> None:
    """Install stub producers that emit the given lines on stdout
    ONLY when invoked with --hook-format (mirroring the real
    rl_kg_search.py / query_code_graph.py contract after b09ca2a).

    `kg_lines` lands as `rl_kg_search.py`; `code_lines` as
    `code-graph-query`.

    Why --hook-format gating in the stub matters: the BEHAVIOURAL
    regression test only catches the §25b bug if the stubs behave like
    the real producers — emitting the `KG:`/`CODE:` prefix only when
    asked. Without this gating, a hook that DROPPED --hook-format
    would still get prefixed stdout from the stub and the dedup test
    would falsely pass.

    Producer-invocation quirks:
      - KG producer is invoked through the venv Python (`"$VENV"
        rl_kg_search.py --hook-format`), so the stub MUST be a Python
        script. A bash shebang would be ignored and Python would try
        to parse the bash as Python (SyntaxError, empty stdout).
      - Code-graph producer is invoked via shell wrapper
        (`"$PROJECT_ROOT/.claude/scripts/code-graph-query"`), so a
        bash script with the execute bit is correct.
    """
    rl = env["scripts_dir"] / "rl_kg_search.py"
    cg = env["cg_dir"] / "code-graph-query"
    rl_lines_repr = ",\n    ".join(repr(l) for l in kg_lines) or "''"
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, argparse\n"
        "# Mirror the real producer's argparse so --hook-format is\n"
        "# accepted. When --hook-format is NOT passed, emit nothing\n"
        "# (matches the real producer's silent-on-empty pre-b09ca2a\n"
        "# behaviour — exposes hook regressions that drop the flag).\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('query')\n"
        "ap.add_argument('--limit', type=int, default=1)\n"
        "ap.add_argument('--hook-format', action='store_true')\n"
        "args = ap.parse_args()\n"
        "if not args.hook_format:\n"
        "    sys.exit(0)\n"
        "for _line in [\n    " + rl_lines_repr + ",\n]:\n"
        "    print(_line)\n",
        encoding="utf-8",
    )
    cg_lines_emit = "\n    ".join(f'printf "%s\\n" "{l}"' for l in code_lines)
    cg.write_text(
        "#!/usr/bin/env bash\n"
        "# Mirror the real code-graph-query's --hook-format gate: emit\n"
        "# the prefixed lines only when --hook-format is on argv. Lets\n"
        "# behavioural regression tests catch a hook that drops the flag.\n"
        '_has_hook_format=0\n'
        'for a in "$@"; do\n'
        '    if [ "$a" = "--hook-format" ]; then _has_hook_format=1; fi\n'
        'done\n'
        'if [ "$_has_hook_format" = "1" ]; then\n'
        '    ' + cg_lines_emit + "\n"
        "fi\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cg.chmod(cg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _invoke_hook(env, session_id: str, file_path: str) -> subprocess.CompletedProcess:
    """Call the hook with a synthetic Edit payload on stdin."""
    payload = {
        "tool_name": "Edit",
        "session_id": session_id,
        "tool_input": {
            "file_path": file_path,
            "new_string": "def f(): pass\n",
        },
    }
    # v0.2.29: pre-create the TMPDIR override path explicitly. Pre-v0.2.29
    # the hook's `CACHE_BASE="${TMPDIR:-/tmp}/claude_edit_cache_<sid>"` +
    # subsequent `mkdir -p "$CACHE_DIR"` had the SIDE-EFFECT of creating
    # this directory, which let later `mktemp` calls in the hook succeed.
    # v0.2.29 moves CACHE_BASE to `$PROJECT_ROOT/.claude/state/edit_cache_*`,
    # which no longer creates the legacy `install_root/tmp/` as a side
    # effect — so we create it here instead. Functionally equivalent to
    # the old behavior; just made explicit.
    tmpdir = env["install_root"] / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(env["hook_path"])],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            # Pin the install-root so the hook's venv + script-path
            # probes resolve to the sandbox.
            "VCT_INSTALL_ROOT": str(env["install_root"]),
            # Project root for emit-context.sh state — the hook computes
            # PROJECT_ROOT as $SCRIPT_DIR/../.. which from
            # templates/hooks/ resolves to the install_root. Same for
            # `.claude/state/seen_kg_titles_<sid>.txt` and
            # `.claude/state/edit_cache_<sid>/`.
            "TMPDIR": str(tmpdir),
        },
    )


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

    seen_file = hook_env["state_dir"] / "seen_kg_titles_sess-grow-test.txt"
    assert seen_file.exists(), "seen file must be touched even when empty"
    content = seen_file.read_text(encoding="utf-8")
    assert content, "seen file must accumulate titles after first emit (§25b smoke)"
    assert "Sample Node A" in content, (
        f"KG title not recorded — _filter_seen + _flush_block append broken "
        f"(content={content!r})"
    )
    assert "sample.module.func_a" in content, (
        f"CODE title not recorded — _filter_seen + _flush_block append broken "
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

    # Seen file should still contain both titles (no duplicate writes).
    seen = (hook_env["state_dir"] / "seen_kg_titles_sess-dedup-test.txt").read_text("utf-8")
    assert seen.count("Sample Node B") == 1, (
        f"duplicate title write in seen file: {seen!r}"
    )
    assert seen.count("sample.module.func_b") == 1, (
        f"duplicate title write in seen file: {seen!r}"
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

    seen = hook_env["state_dir"] / "seen_kg_titles_sess-noresults-test.txt"
    content = seen.read_text("utf-8")
    # Both KG and CODE share the title "no-results" — so the file has
    # exactly one entry, matching the 11-byte (`no-results\n`) real-world
    # state file size observed post-b09ca2a.
    assert content == "no-results\n", (
        f"expected exactly 'no-results\\n' in seen file; got {content!r}"
    )

    # Second Edit: same session, different file. KG + CODE no-results
    # both deduped → empty stdout.
    second = _invoke_hook(hook_env, "sess-noresults-test", str(tmp_path / "b.py"))
    assert second.returncode == 0
    assert second.stdout == "" or second.stdout.isspace(), (
        f"second edit must be fully suppressed (got {second.stdout!r})"
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
        assert "--hook-format" in window, (
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
