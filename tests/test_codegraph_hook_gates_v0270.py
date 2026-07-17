# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream C2 — code-graph hook gate + shared-helper tests.

The pre-bash code-symbol gate is the #1 risk of Stream C: too loose blows the
latency budget on routine ls/cd/git; too tight re-creates the zero-injection
bug. These tests drive the REAL `_lib/codegraph-query.sh` gate functions through
bash and pin the positive AND negative cases (esp. the named negatives:
`git log a.b.c`, `grep foo.bar`, bare dotted path, cd, ls).

Also asserts the four code-graph surfaces all route through the SHARED helper
(no inline duplication) and that the shared seen-store is consulted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import resolve_analyzer_python  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
CG_SH = LIB_DIR / "codegraph-query.sh"
HOOKS = REPO_ROOT / "templates" / "hooks"


def _analyzer_python() -> str:
    """v0.2.84 (review T-2): the venv-resolved interpreter for spawning the REAL
    ``analyze_code_graph.py`` (which ``import weaviate`` at module load). Skips the
    test when no interpreter with weaviate-client is resolvable — bare system
    ``python3`` would exit 1 with "weaviate-client not installed" BEFORE the G5
    guard runs, yielding a misleading pass/fail. Shared resolver lives in
    ``tests/conftest.py`` (one-concern-one-home)."""
    py = resolve_analyzer_python()
    if py is None:
        pytest.skip(
            "no Python interpreter with weaviate-client importable "
            "($VCT_VENV / $VCT_INSTALL_ROOT/.venv / repo .venv / sys.executable) — "
            "the analyzer's module-level `import weaviate` would exit 1 before the "
            "G5 guard, so this test cannot observe the guard's real behavior"
        )
    return py


def _has_bash() -> bool:
    return shutil.which("bash") is not None


def _drop_test_collections(class_names) -> None:
    """Soft teardown: DELETE each `/v1/schema/<class>` on the live Weaviate.

    Mirrors the cleanup idiom in test_codegraph_single_file_scope.py (which
    reaches the client in-process); here the analyzer ran as a SUBPROCESS, so
    we issue the DELETE over HTTP instead. Fully soft — a missing class, a
    down Weaviate, or any transport error must never fail the teardown.
    """
    base = os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")
    for name in class_names:
        try:
            req = urllib.request.Request(f"{base}/v1/schema/{name}", method="DELETE")
            urllib.request.urlopen(req, timeout=5).close()  # noqa: S310 (local URL)
        except Exception:
            pass


pytestmark = pytest.mark.skipif(not _has_bash(), reason="bash required")


def _gate(fn: str, arg: str) -> bool:
    """Run a gate function from codegraph-query.sh; return True iff it fires (0)."""
    # v0.2.84 (review T-2): prefer the venv-resolved interpreter, but these gate
    # functions are PURE BASH (they never invoke `$PY`), so a plain system python3
    # is a fine fallback — no weaviate needed, no skip.
    py = resolve_analyzer_python() or shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n'
        f'. "{CG_SH}"\n'
        f'if {fn} {_q(arg)}; then echo FIRE; else echo SKIP; fi\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return "FIRE" in r.stdout


def _q(s: str) -> str:
    """Single-quote a bash argument safely."""
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# codegraph_bash_gate — pre-bash surface (POSITIVES)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    'grep -rn "migrate_collections" .',
    'rg "OrderManager"',
    'grep "def authenticate"',
    'cat src/foo.py',
    'rg foo_bar src/',
])
def test_bash_gate_fires_on_code_commands(cmd) -> None:
    assert _gate("codegraph_bash_gate", cmd), f"gate should FIRE on: {cmd}"


# --------------------------------------------------------------------------
# codegraph_bash_gate — pre-bash surface (NEGATIVES — the named risk cases)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    'ls -la',
    'cd /tmp',
    'git status',
    'git log a.b.c',     # dotted ref, NOT a C file — must NOT fire
    'cat notes.txt',
    'grep foo.bar',      # dotted, but not an identifier — must NOT fire
    'grep "TODO"',       # bare all-caps word — must NOT fire
    'python -m pytest',
    'echo hello',
    'curl http://localhost',
])
def test_bash_gate_skips_routine_commands(cmd) -> None:
    assert not _gate("codegraph_bash_gate", cmd), f"gate must SKIP on: {cmd}"


# --------------------------------------------------------------------------
# codegraph_pattern_gate — Grep surface
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pat", [
    'def authenticate',
    'OrderManager',
    'migrate_collections',
    'foo(',
])
def test_pattern_gate_fires_on_symbols(pat) -> None:
    assert _gate("codegraph_pattern_gate", pat), f"pattern gate should FIRE on: {pat}"


@pytest.mark.parametrize("pat", [
    'TODO',
    'hello',
    'foo.bar',
])
def test_pattern_gate_skips_non_symbols(pat) -> None:
    assert not _gate("codegraph_pattern_gate", pat), f"pattern gate must SKIP on: {pat}"


# --------------------------------------------------------------------------
# codegraph_extract_symbol — query isolation
# --------------------------------------------------------------------------
def test_extract_symbol_isolates_token() -> None:
    # v0.2.84 (review T-2): pure-bash extract fn (never uses `$PY`); venv-resolved
    # interpreter preferred, system python3 an acceptable fallback (no weaviate).
    py = resolve_analyzer_python() or shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n. "{CG_SH}"\n'
        'echo "[$(codegraph_extract_symbol \'grep -rn "migrate_collections" .\')]"\n'
        'echo "[$(codegraph_extract_symbol \'rg "OrderManager" src/\')]"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert "[migrate_collections]" in r.stdout, r.stdout
    assert "[OrderManager]" in r.stdout, r.stdout


# --------------------------------------------------------------------------
# Shared-helper extraction (no duplication; all surfaces source it)
# --------------------------------------------------------------------------
def test_surfaces_source_the_shared_codegraph_helper() -> None:
    """pre-edit, pre-bash, pre-tool-use must SOURCE _lib/codegraph-query.sh —
    no inline `code-graph-query search` outside the helper (except the
    partial-install fallback branch)."""
    for name in ("pre-edit-context-inject.sh", "pre-bash-context-inject.sh", "pre-tool-use.sh"):
        body = (HOOKS / name).read_text(encoding="utf-8")
        assert "_lib/codegraph-query.sh" in body, (
            f"{name} must source _lib/codegraph-query.sh"
        )


def test_surfaces_source_the_shared_seen_store() -> None:
    """All injectors must source _lib/seen-store.sh so no surface bypasses
    dedup."""
    for name in ("pre-edit-context-inject.sh", "pre-bash-context-inject.sh", "pre-tool-use.sh"):
        body = (HOOKS / name).read_text(encoding="utf-8")
        assert "_lib/seen-store.sh" in body, f"{name} must source _lib/seen-store.sh"


def test_prebash_codegraph_branch_gated_before_threshold() -> None:
    """The pre-bash codegraph branch must run BEFORE the 500-char KG threshold
    gate (so a short symbol command still injects codegraph)."""
    body = (HOOKS / "pre-bash-context-inject.sh").read_text(encoding="utf-8")
    idx_gate = body.find("codegraph_bash_gate")
    idx_threshold = body.find("Threshold gate")
    assert idx_gate != -1 and idx_threshold != -1
    assert idx_gate < idx_threshold, (
        "the codegraph_bash_gate branch must precede the KG threshold gate"
    )


def test_pretooluse_has_read_and_grep_codegraph_branches() -> None:
    body = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    # Read(code) branch injects codegraph.
    assert "_cg_inject" in body, "pre-tool-use must define the shared _cg_inject"
    assert 'TOOL_NAME" == "Grep"' in body, "pre-tool-use must add a Grep branch"
    assert "codegraph_pattern_gate" in body, "Grep branch must use codegraph_pattern_gate"


def test_post_file_edit_resync_fires_on_code_edit() -> None:
    """Surface 3 (resync): v0.2.73 (FIX-B) moved the per-edit 'code' debounce to
    the END-OF-TURN batched drain. post-file-edit.sh now only APPENDS each code
    edit to the drain queue; the drain (stop-codegraph-drain.sh) resolves the
    code_graph_collection_prefix per canonical root and runs the analyzer batch.
    """
    pfe = (HOOKS / "post-file-edit.sh").read_text(encoding="utf-8")
    # post-file-edit appends code edits to the drain queue (the new resync feed).
    assert "codegraph_drain_" in pfe, (
        "post-file-edit.sh must append code edits to the codegraph drain queue"
    )
    # The DRAIN now owns the prefix resolution for the code-graph write target.
    drain = (HOOKS / "stop-codegraph-drain.sh").read_text(encoding="utf-8")
    assert "code_graph_collection_prefix" in drain, (
        "stop-codegraph-drain.sh must resolve the codegraph prefix for the batch"
    )
    assert "--only-files-from" in drain, (
        "the drain must run the analyzer in batched multi-file mode"
    )


# --------------------------------------------------------------------------
# Floor values are mirrored 3-way (must-match comment present)
# --------------------------------------------------------------------------
def test_floor_values_documented_as_must_match() -> None:
    cli = (REPO_ROOT / "templates" / "scripts" / "query_code_graph.py").read_text(encoding="utf-8")
    assert "MUST MATCH" in cli and "3-way" in cli, (
        "the embedder-aware floor must carry the 3-way mirror MUST-MATCH note"
    )


# --------------------------------------------------------------------------
# G5 — analyzer worktree-pollution guard (folded into Stream C)
# --------------------------------------------------------------------------
def test_g5_guard_present_in_analyzer() -> None:
    src = (REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py").read_text(encoding="utf-8")
    assert "_WORKTREE_PATH_SEGMENTS" in src, "analyzer missing the G5 worktree guard"
    assert ".wt" in src and "vco-wt" in src and "worktrees" in src, (
        "G5 guard must detect the known worktree-container path segments"
    )


def test_g5_guard_refuses_worktree_basename_without_project(tmp_path: Path) -> None:
    """Behavioral: from a path under a worktree-container segment AND no
    --project / CODE_GRAPH_PROJECT, the analyzer must REFUSE (exit 1) before
    minting a `<Worktree>_Code*` pollution collection."""
    import os
    wt = tmp_path / "vco-wt" / "fakewt"
    wt.mkdir(parents=True)
    analyzer = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_GRAPH_PROJECT", "PROJECT_NAME")}
    r = subprocess.run(
        [_analyzer_python(), str(analyzer), "."],
        cwd=str(wt), capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 1, f"expected refusal exit 1; got {r.returncode}\n{r.stderr[-400:]}"
    assert "refusing to mint" in r.stderr, r.stderr[-400:]


def test_g5_guard_allows_explicit_project_from_worktree(tmp_path: Path) -> None:
    """With an explicit --project, the worktree path is fine (rows go to the
    canonical collection). The guard must NOT fire. (We can't run a full
    analyze without Weaviate, but the guard runs BEFORE connect — so a clean
    pass past the guard means it either connects or fails later, NOT exit-1
    with the 'refusing to mint' message.)"""
    wt = tmp_path / ".wt" / "agent-abc"
    wt.mkdir(parents=True)
    analyzer = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_GRAPH_PROJECT", "PROJECT_NAME")}
    try:
        r = subprocess.run(
            [_analyzer_python(), str(analyzer), ".", "--project", "CanonicalProj"],
            cwd=str(wt), capture_output=True, text=True, timeout=60, env=env,
        )
        assert "refusing to mint" not in r.stderr, (
            f"guard wrongly fired with an explicit --project: {r.stderr[-400:]}"
        )
    finally:
        # The analyzer runs PAST the G5 guard (explicit --project) and, when
        # Weaviate + the code-embed backend are up, mints the five
        # `CanonicalProj_Code*` classes via create_collections(). Without this
        # teardown they leak on the live instance (0-object empty classes,
        # since `.wt/agent-abc` has no source). Soft-drop them.
        _drop_test_collections(
            f"CanonicalProj_{suffix}"
            for suffix in ("CodeModule", "CodeClass", "CodeFunction",
                           "CodeAPI", "CodeInteraction")
        )


def test_g5_guard_does_not_false_refuse_legit_wt_named_project(tmp_path: Path) -> None:
    """N-5: a project legitimately named e.g. 'wt-foo' must NOT be false-refused.
    The guard uses EXACT path-segment membership ({'.wt','worktrees','vco-wt'}),
    so 'wt-foo' (not one of those segments) is fine and falls back to the
    repo-dir-name without the worktree refusal. Locks the segment-membership
    semantics so a future loosening to substring matching is caught."""
    import os
    legit = tmp_path / "wt-foo"   # a project DIR whose basename starts with 'wt'
    legit.mkdir(parents=True)
    analyzer = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_GRAPH_PROJECT", "PROJECT_NAME")}
    r = subprocess.run(
        [_analyzer_python(), str(analyzer), "."],
        cwd=str(legit), capture_output=True, text=True, timeout=60, env=env,
    )
    assert "refusing to mint" not in r.stderr, (
        f"guard FALSE-REFUSED a legitimately-named 'wt-foo' project: {r.stderr[-400:]}"
    )


def test_g5_guard_refuses_explicit_worktree_relative_project(tmp_path: Path) -> None:
    """Q1 (v0.2.73): the OTHER door — an EXPLICIT `--project vco-wt/bug1` (whose
    value itself carries a worktree-container segment) must be REFUSED, even
    from a NON-worktree cwd. This is the bypass that both the `Vco_wt_*` debris
    and the `CanonicalProj` test leak walked through. The analyzer must exit 1
    BEFORE connecting to Weaviate, so this test mints NOTHING (leak-free)."""
    plain = tmp_path / "plain-dir"   # deliberately NOT under a worktree segment
    plain.mkdir(parents=True)
    analyzer = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_GRAPH_PROJECT", "PROJECT_NAME")}
    r = subprocess.run(
        [_analyzer_python(), str(analyzer), ".", "--project", "vco-wt/bug1"],
        cwd=str(plain), capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 1, f"expected refusal exit 1; got {r.returncode}\n{r.stderr[-400:]}"
    assert "refusing to mint" in r.stderr, r.stderr[-400:]
    assert "vco-wt" in r.stderr, (
        f"refusal message must name the offending segment: {r.stderr[-400:]}"
    )


def test_g5_guard_allows_explicit_legit_wt_substring_project(tmp_path: Path) -> None:
    """Q1 companion: an explicit project name that merely CONTAINS the substring
    'wt' as part of a word ('SwiftlyTyped') is NOT a worktree segment, so the
    guard must NOT refuse it. (It runs past the guard; when Weaviate is up it
    would mint SwiftlyTyped_Code*, so we drop those in teardown.)"""
    plain = tmp_path / "plain-dir2"
    plain.mkdir(parents=True)
    analyzer = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_GRAPH_PROJECT", "PROJECT_NAME")}
    try:
        r = subprocess.run(
            [_analyzer_python(), str(analyzer), ".", "--project", "SwiftlyTyped"],
            cwd=str(plain), capture_output=True, text=True, timeout=60, env=env,
        )
        assert "refusing to mint" not in r.stderr, (
            f"guard FALSE-REFUSED an explicit legit 'wt'-substring project: {r.stderr[-400:]}"
        )
    finally:
        _drop_test_collections(
            f"SwiftlyTyped_{suffix}"
            for suffix in ("CodeModule", "CodeClass", "CodeFunction",
                           "CodeAPI", "CodeInteraction")
        )
