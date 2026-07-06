# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 pre-gate audit fixes — regression tests.

Covers the CONFIRMED findings from the two adversarial pre-gate audits of the
retrieval code:

  Correctness audit:
    F1  — empty-key collapse wiped CodeModule/CodeAPI results (adapter flatten
          fallback + all-empty identity guard in _collapse_to_one_per_node).
    F2  — content-identity dedup dropped distinct same-name entities
          (file_path in code_identity_key + body fields carried by the
          collapse adapter flatten).
    F3  — stale chunk rows lingered when an entity shrank (shrink detector in
          _write_one_object + scoped _delete_stale_chunk_rows).
    F4  — GUI post-rerank floor override was inert in auto mode
          (make_code_tier_fn(min_gate=...) derives the tier `min` at call time).
    F5  — peer-project hits assembled the WRONG project's chunks
          (_self_project_chunk_fetcher gates the fetcher to self rows).
    F6  — callers/type_users/composed_by returned one row per chunk
          (_dedup_objects_by_full_name).
    F7  — chunked functions lost call_names/call edges
          (_assemble_full_body_from_chunks reassembles before ast.parse).
    F8  — single-file dispatch drifted from the P5 exclusions
          (shared skip-suffix constants + ignore-dir gate in
          _single_file_dispatch).
    F9  — one-time prune of already-indexed ignore-set rows
          (codegraph_resync.prune_ignored_rows).
    F10 — anchor LIKE fallback tightening (generic-leaf skip + `::` leaves).
    F11 — minor sweep (iv: CLI/MCP score-normalisation parity; v: over-budget
          test no longer double-counts the docstring).

  Design audit:
    B1  — CLI dropped summary-tier content for functions/classes.
    B2  — --exclude-file culls the edited file's candidates pre-trim (root
          fix for the hook's decapitating grep -v).
    R1  — the stored `doc` is rendered; summary tier prefers doc.
    TIER-GATE recalibration (0.22/0.32/0.48/0.62, env-overridable).
    LAYER-FILTER trap — empty layer-filtered pool retries without the filter
          and notes it.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_CLI_PATH = _REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_SERVER_PATH = _REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
_CG_QUERY_SH = _REPO_ROOT / "templates" / "hooks" / "_lib" / "codegraph-query.sh"

sys.path.insert(0, str(_REPO_ROOT / "claude_mcp_servers"))
sys.path.insert(0, str(_REPO_ROOT))

from weaviate_mcp import server as srv  # noqa: E402
from weaviate_mcp import code_truncation as ct  # noqa: E402
from claude_mcp_servers.rl_client import content_dedup as cd  # noqa: E402
from vco_lib import codegraph_resync as resync  # noqa: E402


# ---------------------------------------------------------------------------
# module loaders (mirror test_codegraph_cli_readpath_v0270 / single_file_scope)
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        pytest.fail(f"module file missing: {path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("required dependency missing for module import")
    return mod


@pytest.fixture(scope="module")
def cli_mod() -> types.ModuleType:
    return _load_module(_CLI_PATH, "_v0272_qcg")


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_module(_ANALYZER_PATH, "_v0272_acg")


# ---------------------------------------------------------------------------
# F1a — collapse adapter: module + API rows keep per-collection identities.
# ---------------------------------------------------------------------------


def _cand(coll: str, props: dict, score: float) -> dict:
    return {"_c": coll, "_s": score, "_d": 1.0 - score, "_p": props,
            "_rerank": score}


def test_f1a_module_and_api_rows_all_survive_collapse():
    """Two modules + two APIs + one function → 5 distinct survivors. Pre-fix
    the module/API rows all flattened to the ("","") key and merged into ONE."""
    rows = [
        _cand("CodeModule", {"path": "src/m1.py", "module_summary": "s1"}, 0.9),
        _cand("CodeModule", {"path": "src/m2.py", "module_summary": "s2"}, 0.8),
        _cand("CodeAPI", {"method": "GET", "endpoint": "/a", "api_description": "d1"}, 0.7),
        _cand("CodeAPI", {"method": "POST", "endpoint": "/b", "api_description": "d2"}, 0.6),
        _cand("CodeFunction", {"file_path": "src/f.py", "full_name": "f.x",
                               "function_body": "pass"}, 0.5),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 5, "every module/API/function row must survive"
    # Modules key on their path; APIs on 'METHOD endpoint'.
    names = {r["full_name"] for r in out}
    assert {"src/m1.py", "src/m2.py", "GET /a", "POST /b", "f.x"} == names


def test_f1a_two_chunks_of_one_module_still_collapse():
    """The fallback keys must still MERGE what belongs together."""
    rows = [
        _cand("CodeModule", {"path": "src/m1.py", "module_summary": "s"}, 0.9),
        _cand("CodeModule", {"path": "src/m1.py", "module_summary": "s"}, 0.5),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 1
    assert out[0]["chunks_matched"] == 2


def test_f1b_all_empty_key_rows_never_bucket_together():
    """_collapse_to_one_per_node: rows with NO key fields stay distinct
    (identity-keyed, mirroring code_ranking's ("__id__", id) guard)."""
    rows = [
        {"combined_score": 0.9, "note": "a"},
        {"combined_score": 0.8, "note": "b"},
        {"combined_score": 0.7, "note": "c"},
    ]
    out = srv._collapse_to_one_per_node(
        rows, key_fields=("file_path", "full_name"), chunk_field="chunk_num",
        dedup_kind="code",
    )
    assert len(out) == 3, "empty-keyed rows must not merge into one bucket"


# ---------------------------------------------------------------------------
# F2 — content-identity dedup: file_path in the code identity key.
# ---------------------------------------------------------------------------


def test_f2_same_leaf_different_files_distinct_keys():
    a = {"full_name": "main.run", "file_path": "cli/main.py"}
    b = {"full_name": "main.run", "file_path": "worker/main.py"}
    assert cd.code_identity_key(a) != cd.code_identity_key(b)


def test_f2_pure_name_fallback_when_no_file_path():
    """Back-compat for hook-block dedup callers whose dicts carry no file_path."""
    assert cd.code_identity_key({"full_name": "main.run"}) == "main.run"


def test_f2_two_same_name_different_file_rows_survive_collapse():
    rows = [
        _cand("CodeFunction", {"full_name": "main.run", "file_path": "cli/main.py",
                               "function_body": "return 1"}, 0.9),
        _cand("CodeFunction", {"full_name": "main.run", "file_path": "worker/main.py",
                               "function_body": "return 2"}, 0.8),
    ]
    out = srv.make_code_collapse_fn()(rows)
    assert len(out) == 2, "distinct same-name entities in different files must both survive"


def test_f2_identical_body_same_name_still_dedups():
    """Cross-collection duplicate (same name + same body + same path OR no
    path at all) still collapses to one."""
    nodes = [
        {"full_name": "main.run", "function_body": "return 1"},
        {"full_name": "main.run", "function_body": "return 1"},
    ]
    assert len(cd.dedup_by_content_identity(nodes, kind="code")) == 1
    nodes_fp = [
        {"full_name": "main.run", "file_path": "src/m.py", "function_body": "x"},
        {"full_name": "main.run", "file_path": "src/m.py", "function_body": "x"},
    ]
    assert len(cd.dedup_by_content_identity(nodes_fp, kind="code")) == 1


def test_f2_collapse_adapter_carries_body_fields_for_fingerprint():
    """The flatten must expose the body at the top level so the content
    fingerprint is real (not empty → identity fallback)."""
    rows = [_cand("CodeFunction", {"full_name": "m.f", "file_path": "a.py",
                                   "function_body": "return 42"}, 0.9)]
    out = srv.make_code_collapse_fn()(rows)
    assert out[0].get("function_body") == "return 42"
    assert cd.code_content_text(out[0]) != ""


# ---------------------------------------------------------------------------
# F4 — floor-derived tier min gate.
# ---------------------------------------------------------------------------


def test_f4_min_gate_keeps_row_below_static_min():
    row = {"_s": 0.18, "_p": {"total_chunks": 1}}
    kept = srv.make_code_tier_fn(min_gate=0.15)([dict(row)])
    assert len(kept) == 1, "0.18 row must render when the floor is lowered to 0.15"
    assert kept[0]["_tier"] == "summary"


def test_f4_default_keeps_022_semantics():
    dropped = srv.make_code_tier_fn()([{"_s": 0.18, "_p": {}}])
    assert dropped == [], "default gate still discards below 0.22"
    kept = srv.make_code_tier_fn()([{"_s": 0.22, "_p": {}}])
    assert len(kept) == 1 and kept[0]["_tier"] == "summary"


def test_f4_both_surfaces_wire_min_gate_from_post_rerank_floor():
    """Static parity: MCP + CLI both derive min_gate from the resolved
    post-rerank floor (the hard invariant)."""
    for src_path in (_SERVER_PATH, _CLI_PATH):
        src = src_path.read_text(encoding="utf-8")
        assert "make_code_tier_fn(min_gate=_post_floor)" in src, (
            f"{src_path.name} must derive the tier min from the resolved floor"
        )
        assert "post_rerank_floor=_post_floor" in src, (
            f"{src_path.name} must pass the SAME resolved floor to the pipeline"
        )


# ---------------------------------------------------------------------------
# TIER-GATE recalibration.
# ---------------------------------------------------------------------------


def test_tier_recalibrated_constants():
    t = srv._CODE_TIER_THRESHOLDS
    assert t["min"] == 0.22
    assert t["single_chunk"] == 0.32
    assert t["three_chunks"] == 0.48
    assert t["full"] == 0.62
    # Boundary semantics under the new values.
    assert srv._get_result_verbosity_by_score(0.31, t) == "summary"
    assert srv._get_result_verbosity_by_score(0.32, t) == "single_chunk"
    assert srv._get_result_verbosity_by_score(0.48, t) == "three_chunks"
    assert srv._get_result_verbosity_by_score(0.62, t) == "full"


def test_tier_env_override_names_kept():
    """The CODE_TIER_* env names stay wired (import-time env reads).

    v0.2.73 (D-12): the tier thresholds moved from
    ``float(os.getenv("CODE_TIER_MIN", "0.22"))`` to the safe-parse wrapper
    ``_safe_float("CODE_TIER_MIN", "0.22")`` (a non-numeric env value now falls
    back to the default instead of crashing at import). The env override is
    still wired — accept EITHER accessor so this guard survives the hardening
    without going blind to a genuine removal.
    """
    src = _SERVER_PATH.read_text(encoding="utf-8")
    for key, default in (
        ("CODE_TIER_MIN", "0.22"),
        ("CODE_TIER_SINGLE_CHUNK", "0.32"),
        ("CODE_TIER_THREE_CHUNKS", "0.48"),
        ("CODE_TIER_FULL", "0.62"),
    ):
        wired = (
            f'os.getenv("{key}",' in src or f'_safe_float("{key}",' in src
        )
        assert wired and f'"{default}"' in src, (
            f"{key} env override (default {default}) must stay wired "
            f'(via os.getenv or _safe_float)'
        )


# ---------------------------------------------------------------------------
# F5 — peer rows must not assemble self-project chunks.
# ---------------------------------------------------------------------------


def test_f5_peer_row_gets_no_chunk_fetcher():
    fetcher = object()
    assert srv._self_project_chunk_fetcher(
        {"_src": "PeerProj"}, "SelfProj", fetcher) is None


def test_f5_self_and_bare_rows_keep_the_fetcher():
    fetcher = object()
    assert srv._self_project_chunk_fetcher({"_src": "SelfProj"}, "SelfProj", fetcher) is fetcher
    assert srv._self_project_chunk_fetcher({"_src": ""}, "SelfProj", fetcher) is fetcher
    assert srv._self_project_chunk_fetcher({}, "SelfProj", fetcher) is fetcher
    # No effective project (cross-tenant search) → no gating.
    assert srv._self_project_chunk_fetcher({"_src": "X"}, None, fetcher) is fetcher


def test_f5_both_surfaces_use_the_shared_gate():
    for src_path in (_SERVER_PATH, _CLI_PATH):
        src = src_path.read_text(encoding="utf-8")
        assert "_self_project_chunk_fetcher(" in src, (
            f"{src_path.name} must gate the chunk fetcher via the shared helper"
        )


# ---------------------------------------------------------------------------
# F6 — one row per full_name in chunk-replicated structural queries.
# ---------------------------------------------------------------------------


def _obj(props: dict):
    return types.SimpleNamespace(properties=props, uuid=f"u{id(props)}")


def test_f6_two_chunks_of_one_caller_collapse_to_canonical():
    objs = [
        _obj({"full_name": "m.caller", "chunk_num": 1, "signature": "s1"}),
        _obj({"full_name": "m.caller", "chunk_num": 0, "signature": "s0"}),
        _obj({"full_name": "m.other", "chunk_num": 0}),
    ]
    out = srv._dedup_objects_by_full_name(objs)
    assert len(out) == 2
    assert out[0].properties["full_name"] == "m.caller"
    assert out[0].properties["chunk_num"] == 0, "canonical chunk wins"
    assert out[1].properties["full_name"] == "m.other"


def test_f6_nameless_objects_are_kept_not_merged():
    objs = [_obj({}), _obj({})]
    assert len(srv._dedup_objects_by_full_name(objs)) == 2


def test_f6_wired_into_the_three_branches():
    src = _SERVER_PATH.read_text(encoding="utf-8")
    assert src.count("_dedup_objects_by_full_name(response.objects)") == 3, (
        "callers + composed_by + type_users must all dedup by full_name"
    )


# ---------------------------------------------------------------------------
# R1 + B1 — doc rendering + summary-tier content on the CLI.
# ---------------------------------------------------------------------------


def test_r1_doc_surfaces_in_tier_output():
    props = {"full_name": "m.f", "signature": "def f(x)", "doc": "Does the thing.",
             "function_body": "def f(x):\n    return x", "file_path": "src/m.py",
             "chunk_num": 0, "total_chunks": 1}
    out = srv._format_code_result_by_tier(props, "CodeFunction", "single_chunk", score=0.4)
    assert out.get("doc") == "Does the thing."


def test_r1_summary_tier_prefers_doc_over_body():
    props = {"full_name": "m.f", "signature": "def f(x)", "doc": "Does the thing.",
             "function_body": "def f(x):\n    return x", "file_path": "src/m.py"}
    out = srv._format_code_result_by_tier(props, "CodeFunction", "summary", score=0.3)
    assert out["summary"] == "Does the thing."
    # Without a doc → falls back to the body snippet (pre-fix behaviour).
    del props["doc"]
    out2 = srv._format_code_result_by_tier(props, "CodeFunction", "summary", score=0.3)
    assert "return x" in out2["summary"]


def test_b1_print_body_renders_summary_for_functions_and_classes(cli_mod, capsys):
    for coll, in_summary in (("CodeFunction", "fn summary text"),
                             ("CodeClass", "cls summary text")):
        rendered = {"collection": coll, "signature": "sig", "tier": "summary",
                    "summary": in_summary}
        cli_mod.CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
        outp = capsys.readouterr().out
        assert f"Summary: {in_summary}" in outp, f"{coll} summary content dropped"


def test_b1_summary_identical_to_doc_not_duplicated(cli_mod, capsys):
    rendered = {"collection": "CodeFunction", "signature": "sig", "tier": "summary",
                "doc": "same text", "summary": "same text"}
    cli_mod.CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
    outp = capsys.readouterr().out
    assert outp.count("same text") == 1, "doc-derived summary must not print twice"


# ---------------------------------------------------------------------------
# F10 — anchor LIKE fallback tightening.
# ---------------------------------------------------------------------------


class _CountingColl:
    """Records fetch_objects calls; returns configurable objects."""

    def __init__(self, counter: list, objects=None):
        self._counter = counter
        self._objects = objects or []
        self.query = types.SimpleNamespace(fetch_objects=self._fetch)

    def _fetch(self, filters=None, limit=None):
        self._counter.append(filters)
        return types.SimpleNamespace(objects=self._objects)


def _querier_with_counting_client(cli_mod, counter, objects=None):
    q = cli_mod.CodeGraphQuery(project=None)
    coll = _CountingColl(counter, objects)
    q.client = types.SimpleNamespace(
        collections=types.SimpleNamespace(get=lambda name: coll)
    )
    return q


def test_f10_generic_leaf_skips_like_fallback(cli_mod):
    """anchor 'run' → only the 2 exact attempts (x2 bases) fire; result None."""
    counter: list = []
    q = _querier_with_counting_client(cli_mod, counter)
    assert q._resolve_anchor_props("run") is None
    assert len(counter) == 4, "generic leaf must not add LIKE attempts (2 attempts x 2 bases)"


def test_f10_short_leaf_skips_like_fallback(cli_mod):
    counter: list = []
    q = _querier_with_counting_client(cli_mod, counter)
    assert q._resolve_anchor_props("go") is None
    assert len(counter) == 4


def test_f10_rust_double_colon_leaf_matches(cli_mod):
    """anchor 'mod::my_fn' → exact + exact + LIKE *.my_fn + LIKE *::my_fn."""
    counter: list = []
    q = _querier_with_counting_client(cli_mod, counter)
    assert q._resolve_anchor_props("mod::my_fn") is None
    assert len(counter) == 8, "leaf LIKE attempts (both separators) must be added"


def test_f10_normal_leaf_resolves(cli_mod):
    counter: list = []
    q = _querier_with_counting_client(
        cli_mod, counter,
        objects=[types.SimpleNamespace(
            properties={"full_name": "api.auth.validate_token", "chunk_num": 0})],
    )
    props = q._resolve_anchor_props("validate_token")
    assert props is not None and props["full_name"] == "api.auth.validate_token"


# ---------------------------------------------------------------------------
# B2 — --exclude-file culls the edited file's candidates pre-trim (CLI).
# ---------------------------------------------------------------------------


class _NVColl:
    def __init__(self, objs):
        self._objs = objs
        self.query = types.SimpleNamespace(
            near_vector=self._nv, fetch_objects=lambda **kw: types.SimpleNamespace(objects=[]),
        )

    def _nv(self, **kwargs):
        return types.SimpleNamespace(objects=self._objs)


def _run_cli_search(cli_mod, monkeypatch, capsys, exclude_file=None):
    objs = [
        types.SimpleNamespace(
            properties={"full_name": "a.f", "file_path": "src/a.py",
                        "signature": "def f()", "function_body": "def f():\n    pass"},
            metadata=types.SimpleNamespace(distance=0.25),
        ),
        types.SimpleNamespace(
            properties={"full_name": "b.g", "file_path": "src/b.py",
                        "signature": "def g()", "function_body": "def g():\n    pass"},
            metadata=types.SimpleNamespace(distance=0.30),
        ),
    ]
    monkeypatch.setattr(cli_mod, "generate_code_embedding", lambda text: [0.1] * 4)
    monkeypatch.setattr(cli_mod, "_active_code_vector_slot", lambda: "codesage_embed")
    monkeypatch.setattr(
        cli_mod, "_code_graph_collections_to_query",
        lambda self_project, bases=None: [("TP_CodeFunction", "TP")],
    )
    q = cli_mod.CodeGraphQuery(project="TP")
    coll = _NVColl(objs)
    q.client = types.SimpleNamespace(
        collections=types.SimpleNamespace(get=lambda name: coll)
    )
    q.search_by_concept("query", "CodeFunction", limit=5, detail="auto",
                        hook_format=True, exclude_file=exclude_file)
    return capsys.readouterr().out


def test_b2_excluded_file_candidates_culled_pre_trim(cli_mod, monkeypatch, capsys):
    out = _run_cli_search(cli_mod, monkeypatch, capsys, exclude_file="src/a.py")
    assert "a.f" not in out, "the excluded file's entities must be culled"
    assert "b.g" in out, "other files' entities must survive"


def test_b2_absolute_exclude_matches_relative_stored_path(cli_mod, monkeypatch, capsys):
    out = _run_cli_search(cli_mod, monkeypatch, capsys,
                          exclude_file="/abs/checkout/src/a.py")
    assert "a.f" not in out
    assert "b.g" in out


def test_b2_no_exclude_keeps_everything(cli_mod, monkeypatch, capsys):
    out = _run_cli_search(cli_mod, monkeypatch, capsys, exclude_file=None)
    assert "a.f" in out and "b.g" in out


# ---------------------------------------------------------------------------
# B2 — hook lib passes --exclude-file and no longer decapitates blocks.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_b2_hook_lib_forwards_exclude_file_no_grep_decapitation(tmp_path):
    """The stub CLI records its argv and emits a block whose BODY mentions the
    excluded path. Pre-fix, the helper's line-wise grep -v stripped those body
    lines (and any header line containing the path); post-fix the exclusion is
    the CLI's job and the raw block passes through intact."""
    proot = tmp_path / "proj"
    scripts = proot / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    cli = scripts / "code-graph-query"
    argv_log = proot / "argv.log"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > "{argv_log}"\n'
        'printf "CODE: other.fn | CodeFunction | distance=0.30 | src=src/other.py\\n"\n'
        'printf "  Body:\\n"\n'
        'printf "  reads src/edited.py at startup\\n"\n'
        'printf "\\n"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)

    script = (
        f'export PROJECT_ROOT="{proot}"\n'
        f'. "{_CG_QUERY_SH}"\n'
        'codegraph_query_block "sym_query" "" 2 "src/edited.py" "src/edited.py"\n'
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         timeout=30, cwd=str(tmp_path))
    assert res.returncode == 0, res.stderr
    argv = argv_log.read_text(encoding="utf-8")
    assert "--exclude-file src/edited.py" in argv, (
        "the hook lib must forward the exclusion to the CLI"
    )
    # The body line mentioning the excluded path is NOT stripped any more —
    # no orphaned-block decapitation.
    assert "reads src/edited.py at startup" in res.stdout
    assert "CODE: other.fn" in res.stdout


# ---------------------------------------------------------------------------
# LAYER-FILTER trap — retry without the filter + note.
# ---------------------------------------------------------------------------


def test_layer_filter_retry_and_note_wired():
    src = _SERVER_PATH.read_text(encoding="utf-8")
    assert "_gather_candidates(apply_layer=False)" in src, (
        "empty layer-filtered pool must re-gather without the filter"
    )
    assert "layer filter ignored" in src, "the response must carry the note"
    # Docstring advertises lowercase values (normalise the line wrap).
    assert "lowercase values: api, service, data, ui, utility" in " ".join(src.split())


# ---------------------------------------------------------------------------
# F11-iv — CLI/MCP score normalisation parity.
# ---------------------------------------------------------------------------


def test_f11iv_mcp_clamps_score_like_cli():
    srv_src = _SERVER_PATH.read_text(encoding="utf-8")
    cli_src = _CLI_PATH.read_text(encoding="utf-8")
    assert "max(0.0, 1.0 - distance)" in srv_src, "MCP must clamp like the CLI"
    assert "max(0.0, 1.0 - distance)" in cli_src
    assert "score = 1.0 - distance\n" not in srv_src, (
        "the unclamped normalisation must be gone from the MCP gather"
    )


# ---------------------------------------------------------------------------
# F11-v — over-budget test no longer double-counts the docstring.
# ---------------------------------------------------------------------------


def _borderline_function(model: str):
    """Build (signature, body) where head+deduped-body fits the budget but the
    OLD head+raw-body computation exceeded it (the docstring counted twice)."""
    max_chars = ct._max_chars_for_model(model)
    sig = "def borderline(x):"
    doc_text = '"""' + "d" * 300 + '"""'  # single-line docstring
    head_len = len(sig) + 1 + len(doc_text)
    margin = 100  # < duplicate-docstring size (~320) → old math went over
    body_rest_budget = max_chars - head_len - 1 - margin
    line = "    x = 1"
    n_lines = max(1, body_rest_budget // (len(line) + 1))
    body = sig + "\n    " + doc_text + "\n" + "\n".join([line] * n_lines)
    return sig, body


def test_f11v_borderline_entity_no_longer_chunks():
    model = "codesage/codesage-large-v2"
    sig, body = _borderline_function(model)
    max_chars = ct._max_chars_for_model(model)
    # Preconditions: old math over budget, deduped math in budget.
    head = ct._assemble_priority_head(sig, body, "python")
    assert len(head) + 1 + len(body) > max_chars, "fixture must trip the OLD test"
    deduped = ct._body_without_priority_lines(body, "python")
    assert len(head) + 1 + len(deduped) <= max_chars, "fixture must fit the NEW test"

    out = ct.chunk_or_truncate_for_embedding(sig, body, language="python", model=model)
    assert len(out) == 1, "borderline entity must stay a single object"
    assert "[chunk" not in out[0]


def test_f11v_genuinely_oversized_still_chunks():
    model = "codesage/codesage-large-v2"
    max_chars = ct._max_chars_for_model(model)
    sig = "def big(x):"
    body = sig + "\n" + "\n".join(["    y = %d" % i for i in range(max_chars // 4)])
    out = ct.chunk_or_truncate_for_embedding(sig, body, language="python", model=model)
    assert len(out) >= 2
    assert out[0].startswith("[chunk 1/")


# ---------------------------------------------------------------------------
# F3 — stale chunk rows deleted when an entity shrinks.
# ---------------------------------------------------------------------------


class _F3Data:
    def __init__(self):
        self.replace_calls = []
        self.insert_calls = []
        # v0.2.74: _delete_stale_chunk_rows now routes through the
        # tokenization-safe iterator + delete_by_id helper (full_name is
        # word-tokenized TEXT, so the old Filter.equal + delete_many could
        # over-delete a token-sharing sibling). Record delete_by_id calls.
        self.delete_by_id_calls = []

    def replace(self, uuid, **kw):
        self.replace_calls.append({"uuid": str(uuid), **kw})

    def insert(self, uuid, **kw):
        self.insert_calls.append({"uuid": str(uuid), **kw})

    def delete_by_id(self, uuid):
        self.delete_by_id_calls.append(str(uuid))


class _F3Coll:
    def __init__(self, name, existing_props=None, rows=None, props_present=None):
        self.name = name
        self.data = _F3Data()
        self._existing = existing_props
        # v0.2.74: rows the safe-delete helper iterates (each a dict of props,
        # keyed with a synthetic uuid). Default: none (empty scan).
        self._rows = list(rows or [])
        # Property names the class "has" (for the helper's config probe).
        _default_props = {"full_name", "file_path", "chunk_num", "project",
                          "project_source", "path", "content_hash"}
        self._props_present = set(props_present) if props_present is not None else _default_props
        self.fetch_kwargs = []
        self.query = types.SimpleNamespace(fetch_object_by_id=self._fetch)
        self.config = types.SimpleNamespace(get=self._config_get)

    def _fetch(self, uuid, return_properties=None):
        self.fetch_kwargs.append(return_properties)
        if self._existing is None:
            return None
        return types.SimpleNamespace(properties=self._existing)

    def _config_get(self):
        props = [types.SimpleNamespace(name=n) for n in self._props_present]
        return types.SimpleNamespace(properties=props)

    def iterator(self, return_properties=None):
        for i, row in enumerate(self._rows):
            yield types.SimpleNamespace(uuid=f"row-{i}", properties=dict(row))


def _bare_analyzer(analyzer_mod, project="TProj"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._current_source = ""
    return inst


def _record_stale_calls(analyzer):
    calls = []
    analyzer._delete_stale_chunk_rows = (
        lambda coll, fn, fp, src, n: calls.append((fn, fp, src, n))
    )
    return calls


def test_f3_three_chunks_restored_as_one_deletes_tail(analyzer_mod):
    """3-chunk entity re-stored as a single object → rows ::1/::2 (chunk_num
    >= 1) are deleted."""
    analyzer = _bare_analyzer(analyzer_mod)
    calls = _record_stale_calls(analyzer)
    coll = _F3Coll("TProj_CodeFunction",
                   existing_props={"content_hash": "old", "total_chunks": 3})
    props = {"full_name": "mod.f", "file_path": "src/m.py", "signature": "def f()",
             "function_body": "def f():\n    pass", "chunk_num": 0, "total_chunks": 1}
    analyzer._write_one_object(coll, "u-0", {"properties": props}, "mod.f")
    assert calls == [("mod.f", "src/m.py", "", 1)], (
        "shrink 3→1 must delete chunk_num >= 1 for exactly this identity"
    )


def test_f3_three_chunks_restored_as_two_deletes_only_last(analyzer_mod):
    """3-chunk entity re-stored as 2 chunks → only ::2 (chunk_num >= 2) goes."""
    analyzer = _bare_analyzer(analyzer_mod)
    calls = _record_stale_calls(analyzer)
    coll = _F3Coll("TProj_CodeFunction",
                   existing_props={"content_hash": "old", "total_chunks": 3})
    props = {"full_name": "mod.f", "file_path": "src/m.py", "signature": "def f()",
             "function_body": "[chunk 1/2]\n\ndef f():\n    pass",
             "chunk_num": 0, "total_chunks": 2}
    analyzer._write_one_object(coll, "u-0", {"properties": props}, "mod.f")
    assert calls == [("mod.f", "src/m.py", "", 2)]


def test_f3_non_canonical_chunk_write_never_triggers_cleanup(analyzer_mod):
    analyzer = _bare_analyzer(analyzer_mod)
    calls = _record_stale_calls(analyzer)
    coll = _F3Coll("TProj_CodeFunction",
                   existing_props={"content_hash": "old", "total_chunks": 3})
    props = {"full_name": "mod.f", "file_path": "src/m.py", "signature": "def f()",
             "function_body": "[chunk 2/2]\n\n    tail", "chunk_num": 1,
             "total_chunks": 2}
    analyzer._write_one_object(coll, "u-1", {"properties": props}, "mod.f")
    assert calls == [], "per-chunk fan-out writes must not re-trigger the cleanup"


def test_f3_growth_or_same_total_never_deletes(analyzer_mod):
    analyzer = _bare_analyzer(analyzer_mod)
    calls = _record_stale_calls(analyzer)
    for stored_total in (1, 2):
        coll = _F3Coll("TProj_CodeFunction",
                       existing_props={"content_hash": "old", "total_chunks": stored_total})
        props = {"full_name": "mod.f", "file_path": "src/m.py", "signature": "def f()",
                 "function_body": "b", "chunk_num": 0, "total_chunks": 2}
        analyzer._write_one_object(coll, "u-0", {"properties": props}, "mod.f")
    assert calls == [], "grow (1→2) and same-total (2→2) must leave rows alone"


def test_f3_module_write_does_not_request_total_chunks(analyzer_mod):
    """Non-chunkable collections must not request total_chunks in the
    point-read (an unknown return property would error the read into the
    fail-safe write path, re-introducing tombstones)."""
    analyzer = _bare_analyzer(analyzer_mod)
    calls = _record_stale_calls(analyzer)
    coll = _F3Coll("TProj_CodeModule", existing_props={"content_hash": "old"})
    props = {"path": "src/m.py", "module_summary": "s"}
    analyzer._write_one_object(coll, "u-m", {"properties": props}, "src/m.py")
    assert calls == []
    for rp in coll.fetch_kwargs:
        assert "total_chunks" not in (rp or []), (
            "CodeModule point-read must not request chunk props"
        )


def test_f3_delete_helper_scopes_and_soft_fails(analyzer_mod):
    analyzer = _bare_analyzer(analyzer_mod)
    # Tail rows of THIS entity (chunk_num >= 1) that must be deleted, plus a
    # token-sharing SIBLING that must SURVIVE (v0.2.74 over-delete guard:
    # full_name is word-tokenized, so an exact-string compare is required).
    # Rows carry project="TProj" (matches _bare_analyzer's project_name) so the
    # Python-side project scoping keeps them in candidacy.
    _P = "TProj"
    rows = [
        {"full_name": "mod.f", "file_path": "src/m.py", "chunk_num": 1, "project": _P},   # tail → delete
        {"full_name": "mod.f", "file_path": "src/m.py", "chunk_num": 2, "project": _P},   # tail → delete
        {"full_name": "mod.f", "file_path": "src/m.py", "chunk_num": 0, "project": _P},   # canonical → keep
        {"full_name": "f.mod", "file_path": "src/m.py", "chunk_num": 2, "project": _P},   # token-sharing sibling → KEEP
        {"full_name": "mod.f", "file_path": "other.py", "chunk_num": 2, "project": _P},   # different file → KEEP
    ]
    coll = _F3Coll("TProj_CodeFunction", rows=rows)
    analyzer._delete_stale_chunk_rows(coll, "mod.f", "src/m.py", "", 1)
    # Only the two exact tail rows (row-0, row-1) delete; canonical + sibling +
    # different-file survive (exact-compare, NOT tokenized).
    assert coll.data.delete_by_id_calls == ["row-0", "row-1"], (
        f"over-delete: expected only the exact tail rows, got "
        f"{coll.data.delete_by_id_calls}"
    )

    # Guards: empty name / zero min → no delete at all.
    coll2 = _F3Coll("TProj_CodeFunction", rows=rows)
    analyzer._delete_stale_chunk_rows(coll2, "", "src/m.py", "", 1)
    analyzer._delete_stale_chunk_rows(coll2, "mod.f", "src/m.py", "", 0)
    assert coll2.data.delete_by_id_calls == [], "empty name / min 0 → no delete"

    # Soft-fail: a delete_by_id error never raises out of the cleanup.
    coll3 = _F3Coll("TProj_CodeFunction", rows=rows)
    coll3.data.delete_by_id = lambda uuid: (_ for _ in ()).throw(RuntimeError("boom"))
    analyzer._delete_stale_chunk_rows(coll3, "mod.f", "src/m.py", "", 1)  # must not raise


# ---------------------------------------------------------------------------
# F7 — chunked function body reassembly for the calls pass.
# ---------------------------------------------------------------------------


class _F7Coll:
    def __init__(self, rows):
        self._rows = rows
        self.query = types.SimpleNamespace(fetch_objects=self._fetch)

    def _fetch(self, filters=None, limit=None):
        return types.SimpleNamespace(
            objects=[types.SimpleNamespace(properties=p) for p in self._rows]
        )


def test_f7_two_chunk_body_reassembles_and_parses(analyzer_mod):
    analyzer = _bare_analyzer(analyzer_mod)
    analyzer.functions_collection = _F7Coll([
        {"chunk_num": 1, "function_body": "[chunk 2/2]\n\n    b = 2\n    g()"},
        {"chunk_num": 0, "function_body": "[chunk 1/2]\n\ndef f():\n    a = 1"},
    ])
    body = analyzer._assemble_full_body_from_chunks(
        "m.f", {"file_path": "src/m.py"}, 2, fallback="FALLBACK",
    )
    assert "g()" in body, "the tail chunk's calls must be present"
    tree = ast.parse(body)  # must parse — this is what unlocks call extraction
    call_names = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "g" in call_names, "call_names from the tail chunk must be extractable"


def test_f7_fetch_error_falls_back_to_chunk_zero_body(analyzer_mod):
    analyzer = _bare_analyzer(analyzer_mod)

    class _Boom:
        query = types.SimpleNamespace(
            fetch_objects=lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )

    analyzer.functions_collection = _Boom()
    body = analyzer._assemble_full_body_from_chunks(
        "m.f", {"file_path": "src/m.py"}, 2, fallback="FALLBACK",
    )
    assert body == "FALLBACK"


def test_f7_wired_into_calls_pass(analyzer_mod):
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert "_assemble_full_body_from_chunks(" in src
    assert src.count("self._assemble_full_body_from_chunks(") >= 1


# ---------------------------------------------------------------------------
# F8 — single-file dispatch parity with the P5 exclusions.
# ---------------------------------------------------------------------------


def test_f8_dispatch_skips_walker_excluded_names(analyzer_mod):
    for name in ("x.bundle.js", "x.chunk.js", "x.min.js", "x.config.js",
                 "x.config.mjs", "x.d.ts", "x.bundle.ts", "x.chunk.ts",
                 "x.config.ts", "x.config.mts", "vite.config.js", "vite.config.ts"):
        assert analyzer_mod._dispatch_name_for_file(Path(name)) == "", name
    # Normal sources still dispatch.
    assert analyzer_mod._dispatch_name_for_file(Path("app.js")) == "javascript"
    assert analyzer_mod._dispatch_name_for_file(Path("app.ts")) == "typescript"
    assert analyzer_mod._dispatch_name_for_file(Path("app.py")) == "python"


def test_f8_walkers_and_dispatch_share_one_suffix_home(analyzer_mod):
    # v0.2.73 CG-1: the 14 inline `_find_<lang>_files` walkers were collapsed
    # onto the declarative `_FINDER_SPECS` table + one shared `_find_files_for`.
    # The F8 invariant is UNCHANGED — the JS/TS name-skip suffixes still have a
    # SINGLE home (`_JS_SKIP_SUFFIXES` / `_TS_SKIP_SUFFIXES`) shared by BOTH the
    # walker path (now via the FINDER_SPECS `name_skip_suffixes` column) AND the
    # single-file dispatch guard. Anchor on the stable constants + their
    # table/dispatch wiring, NOT the old inline `skip_suffixes = set(...)` line.
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    # The finder table routes JS/TS through the SAME suffix constants.
    assert "_JS_SKIP_SUFFIXES)" in src, "JS skip suffixes must feed _FINDER_SPECS"
    assert "_TS_SKIP_SUFFIXES)" in src, "TS skip suffixes must feed _FINDER_SPECS"
    # The shared walker consumes them as `name_skip_suffixes`.
    assert "name_skip_suffixes" in src
    # The single-file dispatch guard reads the SAME constants (one home).
    assert "for s in _JS_SKIP_SUFFIXES" in src
    assert "for s in _TS_SKIP_SUFFIXES" in src
    # Behavioural anchor (survives any future re-expression): .d.ts is skipped.
    assert ".d.ts" in analyzer_mod._TS_SKIP_SUFFIXES


def _single_file_analyzer(analyzer_mod):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "TProj"
    inst.index_dot_claude = False
    return inst


def _dispatch_stub(analyzer_mod):
    """A minimal lang_dispatch with the names the tests route to."""
    return [
        ("python", None, lambda f, root: None),
        ("javascript", None, lambda f, root: None),
    ]


def test_f8_only_file_under_wt_indexes_nothing(analyzer_mod, tmp_path):
    repo = tmp_path
    target = repo / ".wt" / "a" / "b.py"
    target.parent.mkdir(parents=True)
    target.write_text("def b():\n    return 2\n")
    analyzer = _single_file_analyzer(analyzer_mod)
    out = analyzer._single_file_dispatch(target, repo, _dispatch_stub(analyzer_mod), None)
    assert out == [], ".wt/ worktree files must never be single-file indexed"


def test_f8_only_file_under_vendor_bundle_indexes_nothing(analyzer_mod, tmp_path):
    repo = tmp_path
    target = repo / "vendor" / "x.bundle.js"
    target.parent.mkdir(parents=True)
    target.write_text("function v() {}\n")
    analyzer = _single_file_analyzer(analyzer_mod)
    out = analyzer._single_file_dispatch(target, repo, _dispatch_stub(analyzer_mod), None)
    assert out == [], "vendor bundles must never be single-file indexed"
    # Even a NON-bundle vendored source is skipped (dir rule, not suffix rule).
    target2 = repo / "vendor" / "lib.js"
    target2.write_text("function w() {}\n")
    assert analyzer._single_file_dispatch(target2, repo, _dispatch_stub(analyzer_mod), None) == []


def test_f8_normal_file_still_dispatches(analyzer_mod, tmp_path):
    repo = tmp_path
    target = repo / "src" / "ok.py"
    target.parent.mkdir(parents=True)
    target.write_text("def ok():\n    return 1\n")
    analyzer = _single_file_analyzer(analyzer_mod)
    out = analyzer._single_file_dispatch(target, repo, _dispatch_stub(analyzer_mod), None)
    assert len(out) == 1 and out[0][0] == "python"


# ---------------------------------------------------------------------------
# F9 — ignore-scoped prune of already-indexed noise rows.
# ---------------------------------------------------------------------------


class _PruneColl:
    def __init__(self, rows):
        # rows: list of (uuid, props)
        self._rows = rows
        self.data = types.SimpleNamespace(delete_many=self._delete)
        self.deleted_filters = []

    def iterator(self, return_properties=None):
        for uuid, props in self._rows:
            yield types.SimpleNamespace(uuid=uuid, properties=props)

    def _delete(self, where=None):
        self.deleted_filters.append(where)


class _PruneClient:
    def __init__(self, colls: dict):
        self._colls = colls
        self.collections = types.SimpleNamespace(
            get=lambda name: self._colls[name],
            exists=lambda name: name in self._colls,
        )

    def close(self):
        pass


def test_f9_prune_deletes_wt_and_bundle_rows_only():
    # Real UUID strings — weaviate's Filter.by_id().contains_any validates them.
    u = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(6)]
    fn_coll = _PruneColl([
        (u[1], {"file_path": ".wt/v0272-x/tests/test_a.py"}),
        (u[2], {"file_path": "src/app.py"}),
        (u[3], {"file_path": "launcher/vendor/editor/x.bundle.js"}),
        (u[4], {"file_path": "web/static/x.bundle.js"}),
    ])
    mod_coll = _PruneColl([
        (u[5], {"path": "node_modules/pkg/i.js"}),
        (u[0], {"path": "src/main.py"}),
    ])
    client = _PruneClient({
        "TProj_CodeFunction": fn_coll,
        "TProj_CodeModule": mod_coll,
    })
    counts = resync.prune_ignored_rows("TProj", client=client)
    assert counts["TProj_CodeFunction"] == 3, "u1 (.wt), u3 (vendor), u4 (bundle suffix)"
    assert counts["TProj_CodeModule"] == 1, "node_modules row"
    assert len(fn_coll.deleted_filters) == 1 and len(mod_coll.deleted_filters) == 1


def test_f9_normal_rows_untouched():
    fn_coll = _PruneColl([
        ("u1", {"file_path": "src/app.py"}),
        ("u2", {"file_path": "my_vendor_tools/x.py"}),  # NOT a `vendor` path part
    ])
    client = _PruneClient({"TProj_CodeFunction": fn_coll})
    counts = resync.prune_ignored_rows("TProj", client=client)
    assert counts.get("TProj_CodeFunction", 0) == 0
    assert fn_coll.deleted_filters == []


def test_f9_dot_claude_pruned_only_when_not_indexed():
    assert resync._path_is_ignored(".claude/scripts/x.py", index_dot_claude=True) is False
    assert resync._path_is_ignored(".claude/scripts/x.py", index_dot_claude=False) is True


def test_f9_path_part_not_substring():
    assert resync._path_is_ignored("vendor/x.js") is True
    assert resync._path_is_ignored("my_vendor_tools/x.js") is False
    assert resync._path_is_ignored(".wt/a/b.py") is True
    assert resync._path_is_ignored("src/wt/a.py") is False


def test_f9_prune_spawned_by_background_resync(monkeypatch, tmp_path):
    mod = resync
    monkeypatch.setattr(mod, "code_embed_service_healthy", lambda *a, **k: True)
    # v0.2.73 R-6: hermetic — the owed-probe would otherwise hit live
    # Weaviate on developer machines (absent TProj collections → not_owed).
    monkeypatch.setattr(mod, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_register_spawn_with_hub", lambda *a, **k: None)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")

    spawned = []

    class _P:
        pid = 99

    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or _P())
    result = mod.spawn_background_resync(tmp_path, "TProj", python_exe="python3")
    assert result.status == "launched"
    # v0.2.73: prune child + metadata-backfill child + resync driver child.
    assert len(spawned) == 3, "prune + backfill + driver children"
    prune_argv = spawned[0]
    assert "--prune-ignored" in prune_argv and "TProj" in prune_argv
    assert "--index-dot-claude" in prune_argv, "default must NOT prune .claude"


def test_f11i_canonical_source_param_removed():
    import inspect
    params = inspect.signature(resync.spawn_background_resync).parameters
    assert "canonical_source" not in params, (
        "F11-i: the dead canonical_source parameter must be gone"
    )
