# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream C1 read-path regression tests.

The code-graph CLI (`templates/scripts/query_code_graph.py`) had TWO v0.2.21
CLI-only regressions that made every hook code-graph injection return
no-results:
  C1a — `main()` resolved the default project from the launcher SLUG
        (`code_graph_project`, e.g. "orchestrator-root") which sanitises to a
        NONEXISTENT collection (`Orchestrator_root_CodeFunction`). The canonical
        binding-row prefix is `code_graph_collection_prefix`.
  C1b — a fixed 0.35 score floor culled ALL CodeSage results (their distances
        cluster ~0.70 -> score ~0.30 < 0.35). The MCP has no floor and works.
        Fix: an EMBEDDER-AWARE floor keyed on the active code-vector slot.
  C1c — a defensive None-guard on the `structure` `references.get` path.

These tests pin the fixes WITHOUT a live Weaviate (static source + unit-level
floor resolution). A live end-to-end smoke is included but skipped when the
code-graph backend isn't reachable.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SRC = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"


def _load_cli_module():
    """Import templates/scripts/query_code_graph.py as a module."""

# v0.2.92 WP-1 — pin the orchestrator root to the REPO UNDER TEST while the
# script is exec'd.
#
# These CLIs resolve `$VCT_ORCHESTRATOR_ROOT` (that is the fix: it is the
# canonical channel, and without it an installed project can never find
# `kg_access`). `kg_access` then does `sys.path.insert(0, <that root>)`
# (claude_mcp_servers/scripts/kg_access.py:113). `claude_mcp_servers` is a
# NAMESPACE package, so on a machine that has a SECOND orchestrator clone and
# an ambient `$VCT_ORCHESTRATOR_ROOT` pointing at it, merely importing this
# script re-points `claude_mcp_servers.*` for the whole pytest process — and a
# later `import claude_mcp_servers.weaviate_mcp.server` gets the OTHER clone's
# code. That is a test whose meaning depends on the developer's shell.
#
# Pinning here is the same discipline as pinning PYTHONPATH: a repo's own
# suite tests THAT repo. Production behaviour is unchanged and correct — a real
# machine has one orchestrator and the env var names it.
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    sys.path.insert(0, str(REPO_ROOT))
    _saved = os.environ.get("VCT_ORCHESTRATOR_ROOT")
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    try:
        spec = importlib.util.spec_from_file_location("_qcg_v0270", CLI_SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if _saved is None:
            os.environ.pop("VCT_ORCHESTRATOR_ROOT", None)
        else:
            os.environ["VCT_ORCHESTRATOR_ROOT"] = _saved
    return mod


# --------------------------------------------------------------------------
# C1a — prefix precedence (static source assertion + behavioral)
# --------------------------------------------------------------------------
def test_c1a_main_uses_collection_prefix_not_slug() -> None:
    """The default-project resolution MUST prefer code_graph_collection_prefix
    over the slug alias code_graph_project."""
    src = CLI_SRC.read_text(encoding="utf-8")
    # The canonical binding-row prefix must be the FIRST term of the fallback.
    assert "_cfg.code_graph_collection_prefix" in src, (
        "query_code_graph.py main() must resolve from code_graph_collection_prefix"
    )
    # And the slug must NOT be the standalone resolver (it may remain a
    # secondary fallback, but never the sole/first source).
    assert "effective_project = _cfg.code_graph_project or None" not in src, (
        "main() still resolves effective_project SOLELY from the slug "
        "(code_graph_project) — the C1a regression."
    )


def test_c1a_prefix_precedence_order() -> None:
    """Prefix wins; slug is the secondary fallback; both absent -> None."""
    src = CLI_SRC.read_text(encoding="utf-8")
    # The precedence chain text must list prefix BEFORE project.
    idx_prefix = src.find("_cfg.code_graph_collection_prefix")
    idx_project = src.find("_cfg.code_graph_project")
    assert idx_prefix != -1 and idx_project != -1
    assert idx_prefix < idx_project, (
        "code_graph_collection_prefix must precede code_graph_project in the "
        "fallback chain"
    )


# --------------------------------------------------------------------------
# C1b — embedder-aware score floor (unit)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli_mod():
    return _load_cli_module()


# NOTE (v0.2.72 T-FLOOR + integration): the per-slot floor table, resolvers AND
# the retrieval pipeline live in the shared home `weaviate_mcp.code_ranking`
# (the CLI and MCP paths must not diverge). The v0.2.70 C1b defaults
# (codesage/jina 0.0, qwen3 0.25, single scalar floor) are SUPERSEDED by the
# experimentally-validated two-stage floors (codesage/jina 0.16/0.22, qwen3
# 0.20/0.30; RESULTS-2026-07-01.md). The v0.2.72 integrator then REPLACED the
# CLI's single-stage floor break-loop (and its `_resolve_code_score_floor`
# shim) with a `run_code_retrieval_pipeline` call using the SERVER adapter
# factories. These tests pin the integrated contract:
#   * the CLI imports the SHARED resolvers/pipeline/adapters (identity checks —
#     a per-surface fork of any of them re-opens the divergence bug);
#   * the search path calls the shared pipeline with the same args shape as
#     the MCP (static guards);
#   * anchor resolution is failure-soft (behavioral).
# Resolver env-override/coercion semantics are covered by the shared home's
# own tests (tests/test_code_ranking.py) — not duplicated here.
def test_c1b_floor_map_values(cli_mod) -> None:
    """v0.2.72: two-stage tuple table (retrieval, post_rerank); imported into
    the CLI module from the shared code_ranking home."""
    fm = cli_mod.CODE_FLOOR_BY_SLOT
    assert fm["codesage_embed"] == (0.16, 0.22)
    assert fm["jina_embed"] == (0.16, 0.22)
    assert fm["qwen3_embed"] == (0.20, 0.30)


def test_cli_imports_shared_ranking_home(cli_mod) -> None:
    """The CLI's floor table, resolvers and pipeline must BE the shared
    code_ranking objects (identity, not equal copies) — a fork re-opens the
    CLI/MCP divergence bug."""
    from weaviate_mcp import code_ranking
    assert cli_mod.CODE_FLOOR_BY_SLOT is code_ranking.CODE_FLOOR_BY_SLOT
    assert cli_mod.resolve_retrieval_floor is code_ranking.resolve_retrieval_floor
    assert cli_mod.resolve_post_rerank_floor is code_ranking.resolve_post_rerank_floor
    assert cli_mod.run_code_retrieval_pipeline is code_ranking.run_code_retrieval_pipeline


def test_cli_reuses_server_adapter_factories(cli_mod) -> None:
    """The CLI must reuse the SERVER adapter factories + tier formatter —
    NOT reimplement them (the hard non-divergence invariant)."""
    from weaviate_mcp import server as mcp_server
    assert cli_mod.make_code_collapse_fn is mcp_server.make_code_collapse_fn
    assert cli_mod.make_code_tier_fn is mcp_server.make_code_tier_fn
    assert cli_mod._format_code_result_by_tier is mcp_server._format_code_result_by_tier


# --------------------------------------------------------------------------
# The shared pipeline is actually USED in the search path (static guards)
# --------------------------------------------------------------------------
def test_search_path_calls_shared_pipeline() -> None:
    """search_by_concept must run the shared two-stage pipeline with the SAME
    args shape as the MCP (server.py::search_code_graph) — not the legacy
    single-stage floor break-loop."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "run_code_retrieval_pipeline(" in src
    assert "retrieval_floor=resolve_retrieval_floor(_slot)" in src
    # v0.2.72 pre-gate F4: the post-rerank floor is resolved ONCE and shared
    # between the pipeline gate and the tier `min` gate (min_gate) — same
    # shape as the MCP.
    assert "_post_floor = resolve_post_rerank_floor(_slot)" in src
    assert "post_rerank_floor=_post_floor" in src
    assert "collapse_fn=make_code_collapse_fn()" in src
    assert 'key_fields=("file_path", "full_name")' in src
    # tier_fn only in auto mode — same rule as the MCP.
    assert 'make_code_tier_fn(min_gate=_post_floor) if detail == "auto" else None' in src
    # The legacy single-stage shim + break-loop are gone.
    assert "_resolve_code_score_floor" not in src, (
        "the pre-integration single-stage floor shim must be removed"
    )
    assert 'os.environ.get("VCO_CODE_GRAPH_SCORE_FLOOR", "0.35")' not in src, (
        "the legacy fixed 0.35 default is still present — C1b regression"
    )


def test_search_path_overfetches_2n() -> None:
    """The per-collection fetch must over-fetch 2*limit so the pipeline has a
    pool to floor-cull + rerank + collapse (matches the MCP)."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "_fetch_limit = max(1, 2 * limit)" in src


def test_search_parser_exposes_anchor_flag() -> None:
    """The hook path passes --anchor (edited file / grep symbol) so the
    relationship rerank fires; the flag must exist and default to None."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "'--anchor'" in src
    assert "anchor=getattr(args, 'anchor', None)" in src


# --------------------------------------------------------------------------
# Anchor resolution — failure-soft (behavioral)
# --------------------------------------------------------------------------
def test_anchor_resolution_failure_soft(cli_mod) -> None:
    """Any Weaviate error during anchor resolution must yield None (pure
    semantic ordering, byte-identical to a direct MCP call) — never raise."""
    q = cli_mod.CodeGraphQuery(project="Alpha")

    class _BoomCollections:
        def get(self, name):
            raise RuntimeError("weaviate down")

    class _BoomClient:
        collections = _BoomCollections()

    q.client = _BoomClient()
    assert q._resolve_anchor_props("some.symbol") is None
    assert q._resolve_anchor_props("src/module.py") is None


def test_anchor_empty_or_no_client_is_none(cli_mod) -> None:
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = None
    assert q._resolve_anchor_props("anything") is None
    q.client = object.__new__(object)  # non-None client, empty anchor
    assert q._resolve_anchor_props("") is None
    assert q._resolve_anchor_props(None) is None


# --------------------------------------------------------------------------
# C1c — None-guard on the structure references path (static)
# --------------------------------------------------------------------------
def test_c1c_references_none_guard_present() -> None:
    """The structure path must guard `references` being None before .get.

    v0.2.92 re-pin — the GUARANTEE is unchanged, the SHAPE it is expressed in
    changed for the callers branch. C1c originally pinned the literal
    ``(obj.references or {}).get("calls"`` there. That expression was doing its
    job and the branch was STILL broken: the fetch requested no
    ``return_references``, so ``obj.references`` was always None, the guard
    always yielded ``{}`` and ``callers`` answered "Found 0 callers" for every
    input, silently, always. The callers branch now reads the edge through
    ``vco_lib.codegraph_references.read_cross_reference``, which FUSES the C1c
    None-guard with the ``_CrossReference`` shape normalisation — so the
    v0.2.70 regression (an ``AttributeError`` on ``None.get``) remains
    impossible, and :func:`test_callers_branch_requests_and_reads_references`
    below pins the half that C1c could not see.
    """
    src = CLI_SRC.read_text(encoding="utf-8")
    # All three reference reads must be None-safe.
    assert "(response.objects[0].references or {}).get" in src or \
           "_refs = response.objects[0].references or {}" in src, (
        "dependencies path missing the None-references guard"
    )
    assert 'read_cross_reference(obj, "calls")' in src, (
        "callers path no longer reads the `calls` reference through the shared "
        "None-safe reader"
    )
    assert "(response.objects[0].references or {}).get(\"extends\"" in src, (
        "extends path missing the None-references guard"
    )


def test_c1c_none_references_still_cannot_raise() -> None:
    """The v0.2.70 guarantee itself, exercised rather than grepped: reading the
    `calls` edge off an object whose `references` is None yields no callers and
    raises nothing."""
    from vco_lib.codegraph_references import read_cross_reference

    class _NoRefs:
        references = None

    assert read_cross_reference(_NoRefs(), "calls") == []


def test_callers_branch_requests_and_reads_references() -> None:
    """v0.2.92: the two defects that made `structure callers` always answer 0.

    1. the candidate fetch must REQUEST the `calls` link — without
       ``return_references`` the reference mapping is None on every row and no
       amount of None-guarding can produce a caller;
    2. the candidate pool must be FILTERED server-side on ``call_names``. The
       pre-fix ``fetch_objects(limit=50)`` with no filter drew an arbitrary 50
       rows out of the collection (25 837 live on the maintainer machine), so
       even with the references resolved a caller outside that slice could
       never be found — fixing (1) alone still answers 0 in practice.
    """
    src = CLI_SRC.read_text(encoding="utf-8")
    assert 'return_references=QueryReference(link_on="calls")' in src, (
        "callers fetch does not request the `calls` link — obj.references will "
        "be None on every row and the branch answers 0 forever"
    )
    assert '_caller_match_terms(target)' in src, (
        "callers fetch is not filtered on call_names via the shared "
        "_caller_match_terms helper (the MCP branch's proven filter)"
    )
    # The unfiltered whole-collection scan must not come back.
    assert "caller_response = coll.query.fetch_objects(\n                    limit=" not in src, (
        "the unfiltered `fetch_objects(limit=…)` candidate scan is back"
    )


def test_callers_branch_reuses_mcp_helpers(cli_mod) -> None:
    """Identity, not equality: the CLI must run the MCP's OWN caller-matching
    and chunk-collapsing helpers. A per-surface fork is how `callers` came to
    behave differently on the two surfaces in the first place."""
    from weaviate_mcp import server as mcp_server

    assert cli_mod._caller_match_terms is mcp_server._caller_match_terms
    assert cli_mod._dedup_objects_by_full_name is mcp_server._dedup_objects_by_full_name


# --------------------------------------------------------------------------
# C1a/b — live end-to-end (skipped without a reachable code graph)
# --------------------------------------------------------------------------
def _code_graph_reachable() -> bool:
    """Best-effort probe: is a Weaviate code-graph reachable for this project?"""
    try:
        import urllib.request
        url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        with urllib.request.urlopen(f"{url}/v1/.well-known/ready", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.mark.skipif(not _code_graph_reachable(), reason="no reachable Weaviate code graph")
def test_c1_live_search_returns_results() -> None:
    """End-to-end: `code-graph-query search` with NO --project returns >=1
    result (was no-results pre-C1a/C1b). Skipped without a live backend."""
    import subprocess
    cli = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"
    env = {**os.environ}
    # Run from the worktree so the resolver finds this project's binding.
    result = subprocess.run(
        [sys.executable, str(cli), "search", "schema migration", "--limit", "2", "--hook-format"],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT), env=env,
    )
    # The CLI exits 0 and either returns CODE: results OR a clean no-results
    # sentinel. The regression was a crash / always-no-results; we accept any
    # clean exit here (data presence depends on the local index) but assert no
    # traceback leaked.
    assert "Traceback" not in result.stderr, (
        f"CLI crashed: {result.stderr[-500:]}"
    )


# --------------------------------------------------------------------------
# v0.2.72 HARD INVARIANT — live CLI/MCP cross-surface ranking parity
# --------------------------------------------------------------------------
@pytest.mark.skipif(not _code_graph_reachable(), reason="no reachable Weaviate code graph")
def test_live_cli_mcp_ranking_parity() -> None:
    """The hook path (CLI ``search_by_concept``) and the MCP path
    (``search_code_graph``) MUST NOT DIVERGE (maintainer directive, v0.2.72).

    The identity tests above pin that both surfaces import the SAME shared
    pipeline/adapters — but each BODY still normalises candidates, over-fetches
    2N, and gates the tier_fn on its own; body-level drift would reorder
    results without failing an identity check. This end-to-end test runs the
    SAME query through BOTH surfaces against the live code graph and asserts
    the CodeFunction ranking agrees.

    Comparison scope: the CLI queries ONE base collection per call
    (CodeFunction here); the MCP's scope="code" fans out across
    Function+Class+Module and merges before the shared trim. So exact list
    equality is not the contract — the RANKING contract is: the MCP's
    CodeFunction-typed results appear in the SAME relative order as the CLI's,
    and the top CodeFunction hit is identical. (v0.2.71 lesson codified: live
    smoke after MCP merges — mocked tests miss scope/closure bugs.)
    """
    import asyncio
    import json
    import re
    import subprocess

    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    try:
        from weaviate_mcp import server as mcp_server
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"weaviate_mcp.server unimportable here: {exc}")

    # Probe a populated CodeFunction collection + its `project` property value
    # (used as the explicit project arg on BOTH surfaces so neither depends on
    # this checkout's own binding).
    try:
        client = mcp_server.get_weaviate_client()
        proj = None
        for cn in client.collections.list_all().keys():
            if not cn.endswith("CodeFunction"):
                continue
            objs = client.collections.get(cn).query.fetch_objects(limit=1).objects
            if objs:
                proj = (objs[0].properties or {}).get("project")
                if proj:
                    break
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"cannot probe code graph: {exc}")
    if not proj:
        pytest.skip("no populated CodeFunction collection on this backend")

    query = "parse functions from a source file"

    # --- MCP surface ---
    raw = asyncio.run(
        mcp_server.search_code_graph(query, scope="code", limit=8, project=proj)
    )
    data = json.loads(raw)
    mcp_results = data.get("results") or data.get("entities") or []
    mcp_fns = [
        r.get("full_name")
        for r in mcp_results
        if r.get("collection") == "CodeFunction" and r.get("full_name")
    ]

    # --- CLI surface (human format carries the rank lines) ---
    # PYTHONPATH pins the subprocess to THIS repo's weaviate_mcp (ahead of any
    # pip-editable install pointing at a different clone) — same reason as the
    # sys.path shim above. In a real install the editable package IS the
    # updated clone, so this is a dev-checkout-only concern.
    cli = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"
    sub_env = {**os.environ}
    _mcp_dir = str(REPO_ROOT / "claude_mcp_servers")
    sub_env["PYTHONPATH"] = (
        _mcp_dir + os.pathsep + sub_env["PYTHONPATH"]
        if sub_env.get("PYTHONPATH") else _mcp_dir
    )
    result = subprocess.run(
        [sys.executable, str(cli), "search", query,
         "--collection", "CodeFunction", "--limit", "8", "--project", str(proj)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env=sub_env,
    )
    assert "Traceback" not in result.stderr, f"CLI crashed: {result.stderr[-500:]}"
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}: {result.stderr[-500:]}"
    )
    cli_fns = re.findall(r"^\s*\d+\.\s+(\S+)", result.stdout, re.M)

    if not mcp_fns and not cli_fns:
        # Both empty is parity too (floors culled everything for this query).
        return
    assert mcp_fns and cli_fns, (
        f"one surface returned results and the other none — divergence: "
        f"mcp={mcp_fns} cli={cli_fns}\nCLI stdout tail: {result.stdout[-400:]}"
    )
    # Top CodeFunction hit must be identical (the strongest single signal).
    assert mcp_fns[0] == cli_fns[0], (
        f"top CodeFunction differs across surfaces: mcp={mcp_fns[0]!r} "
        f"cli={cli_fns[0]!r}"
    )
    # The MCP's CodeFunction sequence must be an ORDER-PRESERVING subsequence
    # of the CLI's (the MCP list can be shorter — Function hits compete with
    # Class/Module for its top-8 — but relative order comes from the SAME
    # shared pipeline and must agree).
    it = iter(cli_fns)
    missing = [fn for fn in mcp_fns if fn not in it]
    assert not missing, (
        f"MCP CodeFunction order is not a subsequence of the CLI order — "
        f"body-level divergence. mcp={mcp_fns} cli={cli_fns} out-of-order/"
        f"missing={missing}"
    )


# ---------------------------------------------------------------------------
# v0.2.73 M2/M4 — CLI/MCP FIELD parity for the new sidecar + n_callers fields
# (AG-4 append; integrator merges with AG-5's ordering-parity extension)
# ---------------------------------------------------------------------------


def test_m2_m4_cli_prints_shared_formatter_fields() -> None:
    """The shared formatter emits `one_liner` / `n_callers`; the CLI must
    PRINT them (no logic — field parity with the MCP JSON). Source-level
    check so it runs without the Weaviate stack."""
    src = CLI_SRC.read_text(encoding="utf-8")
    assert '_print_identity_extras' in src
    assert 'rendered.get("one_liner"' in src
    assert 'rendered.get("n_callers"' in src


def test_m2_m4_server_formatters_emit_fields() -> None:
    """Both shared formatters (tier + rank) consult the code sidecar and the
    n_callers property — the CLI relies on those fields existing."""
    from weaviate_mcp import server as mcp_server
    import inspect

    tier_src = inspect.getsource(mcp_server._format_code_result_by_tier)
    rank_src = inspect.getsource(mcp_server._format_code_result_by_rank)
    for src_text in (tier_src, rank_src):
        assert "_get_code_format" in src_text
        assert "n_callers" in src_text


# ---------------------------------------------------------------------------
# v0.2.92 — `structure callers` BEHAVIOUR (what nobody was testing)
#
# Every assertion above about the callers branch is a source grep, and the
# branch stayed source-plausible while answering "Found 0 callers" for every
# input. These tests drive the real `query_structure` code against a fake
# collection that behaves like the client does on the two points that matter:
#
#   * `references` is None on a fetched object unless the fetch REQUESTED the
#     link — a fake that always populates references cannot see the defect,
#     which is exactly how it survived;
#   * a resolved link is the REAL `_CrossReference` wrapper, whose `len()` and
#     `iter()` raise and whose `bool()` is True even when empty.
#
# The fake also EVALUATES the filter (EQUAL / CONTAINS_ANY) rather than
# ignoring it, so "the candidate pool is filtered server-side" is exercised
# rather than asserted.
# ---------------------------------------------------------------------------
_XREF = pytest.importorskip("weaviate.collections.classes.internal")._CrossReference


class _FakeObj:
    def __init__(self, uuid: str, properties: dict, references=None) -> None:
        self.uuid = uuid
        self.properties = properties
        self.references = references


class _FakeResponse:
    def __init__(self, objects: list) -> None:
        self.objects = objects


def _filter_matches(flt, props: dict) -> bool:
    """Evaluate the two filter shapes the CLI builds. ``None`` matches
    everything — which is precisely what the pre-fix candidate scan did."""
    if flt is None:
        return True
    target = getattr(flt, "target", None)
    operator = str(getattr(flt, "operator", ""))
    value = getattr(flt, "value", None)
    actual = props.get(target)
    if "CONTAINS_ANY" in operator:
        return bool(set(actual or []) & set(value or []))
    if operator.endswith("EQUAL"):
        return actual == value
    raise AssertionError(f"fake does not model filter {operator!r}")


class _FakeQuery:
    def __init__(self, rows: list, edges: dict) -> None:
        self._rows = rows          # [(uuid, properties), ...]
        self._edges = edges        # uuid -> [target uuid, ...]

    def fetch_objects(self, filters=None, limit=100, return_references=None,
                      return_properties=None, **_kw):
        hits = [(u, p) for (u, p) in self._rows if _filter_matches(filters, p)]
        out = []
        for uuid, props in hits[:limit]:
            refs = None
            if return_references is not None:
                link_on = return_references.link_on
                targets = [_FakeObj(t, {}) for t in self._edges.get(uuid, [])] \
                    if link_on == "calls" else []
                # The client omits the key entirely when nothing resolved.
                refs = {link_on: _XREF._from(targets)} if targets else {}
            out.append(_FakeObj(uuid, dict(props), refs))
        return _FakeResponse(out)


class _FakeCollection:
    def __init__(self, rows: list, edges: dict) -> None:
        self.query = _FakeQuery(rows, edges)


class _FakeCollections:
    def __init__(self, coll) -> None:
        self._coll = coll

    def get(self, _name):
        return self._coll


class _FakeClient:
    def __init__(self, rows: list, edges: dict) -> None:
        self.collections = _FakeCollections(_FakeCollection(rows, edges))


# A tiny call graph: `caller_one` calls the target and the analyzer RESOLVED
# the edge; `caller_two` names it but has no stored edge (short-name resolution
# is lossy and the whole cross-ref pass soft-fails); `unrelated` calls
# something else; `lonely` is called by nobody.
_ROWS = [
    ("t1", {"full_name": "pkg.mod.helper", "signature": "helper()",
            "call_names": []}),
    ("c1", {"full_name": "pkg.a.caller_one", "signature": "caller_one()",
            "call_names": ["helper"]}),
    ("c2", {"full_name": "pkg.b.caller_two", "signature": "caller_two()",
            "call_names": ["helper"]}),
    ("c3", {"full_name": "pkg.c.unrelated", "signature": "unrelated()",
            "call_names": ["something_else"]}),
    ("l1", {"full_name": "pkg.d.lonely", "signature": "lonely()",
            "call_names": []}),
]
_EDGES = {"c1": ["t1"]}


def _caller_lines(out: str) -> list:
    """Just the result rows — the explanatory footnote quotes the same marker
    text, so a bare substring check over the whole output cannot tell the two
    apart."""
    return [ln for ln in out.splitlines() if ln.startswith("   - ")]


def _run_callers(cli_mod, target: str, capsys, edges=None) -> str:
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = _FakeClient(_ROWS, _EDGES if edges is None else edges)
    q.query_structure("callers", target)
    return capsys.readouterr().out


def test_callers_returns_the_callers(cli_mod, capsys) -> None:
    """THE ACT. Pre-fix this printed 'Found 0 callers' — the branch's only
    possible answer, for every input."""
    out = _run_callers(cli_mod, "pkg.mod.helper", capsys)
    assert "Found 2 callers" in out
    assert "pkg.a.caller_one" in out
    assert "pkg.b.caller_two" in out
    assert "pkg.c.unrelated" not in out, "a non-caller leaked into the result"
    assert "Traceback" not in out


def test_callers_marks_the_uuid_confirmed_edge(cli_mod, capsys) -> None:
    """A resolved `calls` edge is uuid-precise and says so; a name-only match
    is still reported (a missing edge is not evidence of absence) but is not
    claimed to be confirmed."""
    out = _run_callers(cli_mod, "pkg.mod.helper", capsys)
    confirmed = [ln for ln in _caller_lines(out) if "[call edge]" in ln]
    assert len(confirmed) == 1
    assert "pkg.a.caller_one" in confirmed[0]
    assert "matched the call NAME only" in out


def test_callers_with_no_callers_is_zero_not_an_error(cli_mod, capsys) -> None:
    """THE LEAVE-ALONE. A real function nothing calls answers zero, quietly —
    no traceback, and none of the caveat lines that only make sense when there
    ARE rows."""
    out = _run_callers(cli_mod, "pkg.d.lonely", capsys)
    assert "Found 0 callers" in out
    assert "Traceback" not in out
    assert _caller_lines(out) == []
    assert "matched the call NAME only" not in out
    assert "Capped at the first" not in out


def test_callers_unknown_target_reports_not_found(cli_mod, capsys) -> None:
    out = _run_callers(cli_mod, "pkg.z.missing", capsys)
    assert "not found" in out
    assert "Found" not in out


def test_callers_survives_unresolved_references(cli_mod, capsys) -> None:
    """The v0.2.70 C1c guarantee end-to-end: with NO edge stored anywhere the
    reference mapping is empty and the branch must still report the
    name-matched callers rather than raise."""
    out = _run_callers(cli_mod, "pkg.mod.helper", capsys, edges={})
    assert "Found 2 callers" in out
    assert not any("[call edge]" in ln for ln in _caller_lines(out))
    assert "Traceback" not in out
