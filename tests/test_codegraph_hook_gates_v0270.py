# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream C2 — code-graph hook gate retirement pins + shared helpers.

v0.2.101 Wave 2: the shell gate FUNCTIONS this file used to drive
(`codegraph_bash_gate` / `codegraph_pattern_gate` / `codegraph_extract_symbol`
in `_lib/codegraph-query.sh`) are RETIRED with their last legacy callers —
the one home is `vco_lib/inject_intent.py`, driven by
`hook_context_router.py`. The original positive/negative corpora (incl. the
named risk cases: `git log a.b.c`, `grep foo.bar`, bare dotted path, cd, ls)
live on against that one home in
`tests/test_v02101_inject_intent_classifier.py` and
`tests/test_p1e_codegraph_extract_symbol.py`.

What remains here: the RETIREMENT pins (no injection hook sources the shell
gate lib; the pre-bash threshold/gate and the pre-tool-use Read/Grep
branches cannot creep back), the post-file-edit resync delegation, the floor
mirror note and the G5 analyzer worktree guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from tests.common.child_env import child_env  # noqa: E402
from tests.conftest import resolve_analyzer_python  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
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


# --------------------------------------------------------------------------
# Gate retirement pins (v0.2.101 Wave 2) — the shell gate corpus tests that
# lived here were retired WITH the gate functions; see the module docstring
# for where the corpora live on.
# --------------------------------------------------------------------------
def test_surfaces_source_the_shared_codegraph_helper() -> None:
    """v0.2.01 injection redesign: NO injection hook sources
    _lib/codegraph-query.sh any more — identifier/pattern gating, symbol
    extraction and the exact-symbol lookups live in vco_lib/inject_intent,
    driven by claude_mcp_servers/scripts/hook_context_router.py (one home).
    This row pins the retirement for all three former consumers so a shell
    side-gate cannot creep back beside the Python classifier (the class of
    wrong-shape bug the P1e history documents). Behavioural pins:
    test_v02101_pretool_use_read_branch_removed.py (Read/Grep branches),
    test_v02101_router_surfaces.py + test_v02101_edit_query_rework.py
    (bash/edit/write wrappers)."""
    for name in ("pre-edit-context-inject.sh", "pre-bash-context-inject.sh",
                 "pre-tool-use.sh"):
        body = (HOOKS / name).read_text(encoding="utf-8")
        assert '. "$SCRIPT_DIR/_lib/codegraph-query.sh"' not in body, (
            f"{name} must not source _lib/codegraph-query.sh — the router owns "
            "code-graph querying now"
        )


def test_surfaces_source_the_shared_seen_store() -> None:
    """Injectors that dedupe SHELL-SIDE must source _lib/seen-store.sh so no
    surface bypasses dedup. v0.2.101: pre-bash-context-inject.sh became a thin
    router wrapper — its dedupe is the ROUTER's (same seen-store files,
    enforced in claude_mcp_servers/scripts/hook_context_router.py and pinned
    by tests/test_v02101_inject_gates.py::TestSeenStoreParity) — so it no
    longer sources the lib here. pre-tool-use keeps it for the reads ledger."""
    for name in ("pre-edit-context-inject.sh", "pre-tool-use.sh"):
        body = (HOOKS / name).read_text(encoding="utf-8")
        assert "_lib/seen-store.sh" in body, f"{name} must source _lib/seen-store.sh"


def test_prebash_codegraph_branch_gated_before_threshold() -> None:
    """v0.2.101 §C1 RETIRED both mechanisms this row used to order: the
    500-char threshold (VCT_BASH_KG_THRESHOLD_CHARS) and the inline
    codegraph_bash_gate branch. pre-bash-context-inject.sh is now a thin
    router wrapper — intent classification (READ/EDIT/SEARCH/MECHANICAL)
    replaces the threshold, and the router's exact-symbol CG leg replaces the
    gate (behavioural pins: tests/test_v02101_router_surfaces.py and
    tests/test_v02101_inject_intent_classifier.py). This row pins the
    retirement so the dead gates cannot creep back beside the classifier."""
    body = (HOOKS / "pre-bash-context-inject.sh").read_text(encoding="utf-8")
    assert "VCT_BASH_KG_THRESHOLD_CHARS=" not in body.replace(
        "VCT_BASH_KG_THRESHOLD_CHARS — classification replaces it", ""), (
        "the 500-char threshold knob must stay retired (classification owns the gate)")
    assert "codegraph_bash_gate " not in body, (
        "the inline codegraph_bash_gate branch must stay retired")


def test_dead_injection_libs_stay_retired() -> None:
    """v0.2.101 wave-2 review SF-2 (owner rule: retire dead shipped code
    NOW, never wave-flag it): four helpers lost their last callers in the
    router rework and are deleted, not parked —

      * _lib/codegraph-query.{sh,ps1}   (whole lib: query_block + cli locator;
        its header still claimed three sourcing hooks — a false promise)
      * _lib/command-noise-strip.{sh,ps1} (the pre-bash query build was the
        only consumer; queries come from targets/symbols now)
      * vco_dual_search_cached / Invoke-VcoDualSearchCached (pre-edit was the
        last caller; hook_dual_search.py itself LIVES — the router imports
        its run_legs mechanism)
      * vco_bash_write_prebash / Get-VcoBashWritePreBash (inject_intent uses
        the Python prebash_query_parts directly; the shell delegator had
        zero callers, tests included)

    This row pins the absence so a partial re-add (or a bundle round-trip
    resurrecting one flavour) goes red."""
    lib = HOOKS / "_lib"
    for name in ("codegraph-query.sh", "codegraph-query.ps1",
                 "command-noise-strip.sh", "command-noise-strip.ps1",
                 # wave-3 (review nit-6): pre-tool-use §5 was the last
                 # consumer of the shell query cache — retired with it. The
                 # §9 never-cache-empty contract lives in the router's
                 # Python cache (test_v02101_query_cache_poison_fix.py).
                 "query-cache.sh", "query-cache.ps1"):
        assert not (lib / name).exists(), (
            f"{name} must stay retired (zero live callers since v0.2.101)"
        )
    for name, dead, comment_prefix in (
        ("bash-write-targets.sh", "vco_bash_write_prebash", "#"),
        ("bash-write-targets.ps1", "Get-VcoBashWritePreBash", "#"),
    ):
        body = (lib / name).read_text(encoding="utf-8-sig")
        # Executable lines only — the retirement NOTES name the functions on
        # purpose (a tombstone comment is documentation, not a caller).
        executable = "\n".join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith((comment_prefix, "<#"))
        )
        assert dead not in executable, (
            f"{name}: {dead} must stay retired — zero live callers"
        )
    # The router's in-process mechanism is the survivor: hook_dual_search.py
    # must still exist (run_legs is the merged path now).
    assert (REPO_ROOT / "claude_mcp_servers" / "scripts"
            / "hook_dual_search.py").exists()


def test_pretooluse_has_read_and_grep_codegraph_branches() -> None:
    """v0.2.101 §C2/§C6: pre-tool-use's Read(code) and Grep(symbol) code-graph
    injection branches are RETIRED — grep-context-inject.{sh,ps1} (router
    surface `grep`) and read-context-inject.{sh,ps1} (PostToolUse Read) are
    their one homes. This row pins the RETIREMENT so the dead branch cannot
    creep back alongside the router hooks (double injection); the behavioural
    proof (stub CLI never spawned, no envelope) lives in
    tests/test_v02101_pretool_use_read_branch_removed.py."""
    body = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    assert "_cg_inject" not in body, (
        "the shared _cg_inject helper must stay retired — its callers are gone"
    )
    assert 'TOOL_NAME" == "Grep"' not in body, (
        "the Grep injection branch must stay retired (grep-context-inject owns it)"
    )
    assert "codegraph_pattern_gate" not in body, (
        "the identifier gate moved to vco_lib/inject_intent (router surface `grep`)"
    )


def test_post_file_edit_resync_fires_on_code_edit() -> None:
    """Surface 3 (resync): v0.2.73 (FIX-B) moved the per-edit 'code' debounce to
    the END-OF-TURN batched drain. post-file-edit.sh now only APPENDS each code
    edit to the drain queue; the drain (stop-codegraph-drain.sh) resolves the
    code_graph_collection_prefix per canonical root and runs the analyzer batch.
    """
    pfe = (HOOKS / "post-file-edit.sh").read_text(encoding="utf-8")
    # v0.2.95 (lane F10): the append moved into the ONE routing home that
    # post-file-edit.sh and post-bash-file-sh both call, so a CLI write reaches
    # the drain queue too. The hook must still delegate; the lib must still
    # append. (The behavioural end-to-end proof lives in
    # tests/test_v0295_bash_write_sync.py.)
    assert "vco_route_touched_path" in pfe, (
        "post-file-edit.sh must delegate routing to _lib/route-touched-path.sh"
    )
    route_lib = (HOOKS / "_lib" / "route-touched-path.sh").read_text(encoding="utf-8")
    assert "codegraph_drain_" in route_lib, (
        "the routing home must append code edits to the codegraph drain queue"
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
def _analyzer_env(env: dict, tmp_path: Path) -> dict:
    """``child_env`` for an analyzer spawn, with its ledger root contained.

    CONTAINMENT (v0.2.94). ``child_env`` pins ``$VCT_ORCHESTRATOR_ROOT`` at the
    checkout so the child imports THIS tree's ``vco_lib``. The analyzer reads
    that SAME variable for a second, unrelated purpose — the root whose
    deferral ledger it reconciles (``main()``: ``install_root`` →
    ``EmbeddingService.for_project`` → ``_clear_failure_deferral``) — and
    reconciling a ledger rewrites the ``vco-deferral-reminder`` block in
    ``<root>/CLAUDE.md``. Left at the checkout, every spawn below that gets
    past the G5 guard edited a TRACKED file (the stray ``M CLAUDE.md`` seen in
    ``git status`` during a suite run).

    ``child_env``'s own ``KG_BASE_DIR`` pin does NOT cover this case: the
    analyzer passes the root EXPLICITLY to ``for_project()``, and an explicit
    root outranks both env vars in ``_detect_project_root``.

    ``child_env`` documents this opt-out, and the ``PYTHONPATH`` it also sets
    still resolves ``vco_lib`` from the checkout, so the import pin the helper
    exists for is preserved. Applied to EVERY spawn here, including the three
    that refuse at the guard: which ones reach the reconcile is a property of
    the guard under test, and a test should not depend on the thing it is
    testing to stay out of the working tree.
    """
    sentinel = tmp_path / "orchestrator_root_sentinel"
    sentinel.mkdir(parents=True, exist_ok=True)
    return child_env(env, VCT_ORCHESTRATOR_ROOT=str(sentinel))


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
        cwd=str(wt), capture_output=True, text=True, timeout=60,
        env=_analyzer_env(env, tmp_path),
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
            cwd=str(wt), capture_output=True, text=True, timeout=60,
            env=_analyzer_env(env, tmp_path),
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
        cwd=str(legit), capture_output=True, text=True, timeout=60,
        env=_analyzer_env(env, tmp_path),
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
        cwd=str(plain), capture_output=True, text=True, timeout=60,
        env=_analyzer_env(env, tmp_path),
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
            cwd=str(plain), capture_output=True, text=True, timeout=60,
            env=_analyzer_env(env, tmp_path),
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
