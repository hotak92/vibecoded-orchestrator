# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 (P1b): the ONE code-graph row classifier — parity + behavior.

Three locks:

1. **Mirror parity** — the analyzer template cannot import ``vco_lib`` at
   user sites, so ``analyze_code_graph.py`` carries a byte-identical copy of
   the classifier functions between ``# BEGIN MUST-MATCH
   codegraph-row-classify`` / ``# END MUST-MATCH`` markers. This test
   byte-compares the marked regions AND value-compares the shared constants
   (the analyzer derives them from its own walk tables). This parity test IS
   the lock — a drift in either copy fails CI.

2. **Rust delta parity** — the launcher's language-detection pre-check
   (``codegraph.rs::ignored_dirs()``) is a deliberate SUBSET-plus-IDE-dirs
   sibling of the Python union set (detection only needs a cheap yes/no; an
   over-detection from e.g. ``vendor/`` is harmless). The documented deltas
   are encoded here EXPLICITLY: any change on either side fails until the
   delta doc below is updated (never silence).

3. **Two-sided behavior** — per convergence class, the resync owed-probe and
   the analyzer's orphan-clear must agree ON THE SAME FIXTURE: what the
   probe stops counting, the purge deletes (pathless / ignored / transient /
   deleted-file), and what stays owed (reachable non-ignored stale rows —
   including ``embed_revision == 0``) is counted AND kept. The pre-v0.2.75
   disagreement classes were IMMORTAL: counted owed forever while nothing
   could ever re-stamp or delete them, so every ``install.py --update``
   re-triggered a whole-repo resync.

Pure unit — fakes only, no Weaviate.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_CLASSIFY_PATH = _REPO_ROOT / "vco_lib" / "codegraph_row_classify.py"
_RUST_CODEGRAPH = (
    _REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "codegraph.rs"
)

_BEGIN = "# BEGIN MUST-MATCH codegraph-row-classify"
_END = "# END MUST-MATCH codegraph-row-classify"


@pytest.fixture(scope="module")
def analyzer_mod():
    spec = importlib.util.spec_from_file_location(
        "_row_classify_parity_analyzer", str(_ANALYZER_PATH)
    )
    assert spec and spec.loader, f"analyzer missing: {_ANALYZER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def classify_mod():
    from vco_lib import codegraph_row_classify as m
    return m


# ─────────────────────── 1. mirror parity ───────────────────────


def _must_match_region(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    # Line-anchored: the markers are COMMENT LINES (column 0) — a prose
    # mention inside a docstring must not shadow the real region.
    begin = re.search(rf"^{re.escape(_BEGIN)}", text, flags=re.MULTILINE)
    end = re.search(rf"^{re.escape(_END)}", text, flags=re.MULTILINE)
    assert begin, f"{path}: missing line-anchored '{_BEGIN}' marker"
    assert end and end.start() > begin.start(), (
        f"{path}: missing line-anchored '{_END}' marker after the BEGIN"
    )
    # From the line AFTER the begin-marker line to the end-marker line
    # (exclusive). Byte parity is the contract, whitespace included.
    region = text[begin.start():end.start()]
    return region.split("\n", 1)[1]  # drop the begin-marker line itself


def test_mirror_region_is_byte_identical():
    """The lock: the analyzer's mirrored classifier == the vco_lib original,
    byte for byte (docstrings and comments included — a divergent comment is
    a divergent contract explanation)."""
    lib_region = _must_match_region(_CLASSIFY_PATH)
    analyzer_region = _must_match_region(_ANALYZER_PATH)
    assert lib_region == analyzer_region, (
        "MUST-MATCH region drifted between vco_lib/codegraph_row_classify.py "
        "and templates/scripts/analyze_code_graph.py. Edit BOTH copies "
        "identically (the analyzer cannot import vco_lib at user sites)."
    )


def test_shared_constants_value_parity(analyzer_mod, classify_mod):
    """The analyzer DERIVES its constants from its own walk tables; the
    values must equal the vco_lib module's (the classifier bodies read them
    late-bound, so value parity + region parity ⇒ behavior parity)."""
    assert analyzer_mod.CODEGRAPH_IGNORE_PARTS == classify_mod.CODEGRAPH_IGNORE_PARTS
    assert tuple(analyzer_mod.CODEGRAPH_SKIP_SUFFIXES) == tuple(
        classify_mod.CODEGRAPH_SKIP_SUFFIXES
    )
    assert (
        analyzer_mod.TRANSIENT_STATE_MARKER == classify_mod.TRANSIENT_STATE_MARKER
    )
    # The walk applies the SAME union (P1b-2: one derived ignore-set) …
    assert analyzer_mod._ALL_IGNORE_PARTS == classify_mod.CODEGRAPH_IGNORE_PARTS
    assert (
        analyzer_mod._ignore_dirs_for("python", index_dot_claude=True)
        == analyzer_mod._ALL_IGNORE_PARTS
    )
    assert (
        analyzer_mod._ignore_dirs_for("rust", index_dot_claude=True)
        == analyzer_mod._ALL_IGNORE_PARTS
    )
    # The per-project `.claude` gate still applies on top of the union.
    assert analyzer_mod._ignore_dirs_for("python", index_dot_claude=False) == (
        analyzer_mod._ALL_IGNORE_PARTS | frozenset({".claude"})
    )
    # … and the union subsumes every per-language extra (nothing walkable
    # that the classifier would call ignored, and vice versa).
    for extras in analyzer_mod._LANGUAGE_IGNORE_DIRS_EXTRAS.values():
        assert extras <= analyzer_mod._ALL_IGNORE_PARTS
    # The previously missing parts are now present (the immortal-row fix).
    for part in ("target", "coverage", "obj", "bin", ".gradle", ".vs", ".bundle"):
        assert part in classify_mod.CODEGRAPH_IGNORE_PARTS, part


def test_resync_aliases_point_at_shared_module(classify_mod):
    """The resync module consumes the shared set by IMPORT (no third copy)."""
    from vco_lib import codegraph_resync as cr

    assert cr._PRUNE_IGNORE_PARTS is classify_mod.CODEGRAPH_IGNORE_PARTS
    assert tuple(cr._PRUNE_SKIP_SUFFIXES) == tuple(classify_mod.CODEGRAPH_SKIP_SUFFIXES)
    assert cr._TRANSIENT_STATE_MARKER == classify_mod.TRANSIENT_STATE_MARKER
    assert cr._path_is_ignored is classify_mod.path_is_ignored
    assert cr._path_reachable_on_disk is classify_mod.path_reachable_on_disk
    assert cr.classify_row is classify_mod.classify_row


def test_transient_marker_matches_6_to_7_migration(classify_mod):
    """MUST-MATCH: migrations/codegraph_collection/6_to_7.py::_TRANSIENT_MARKER."""
    mig = _REPO_ROOT / "migrations" / "codegraph_collection" / "6_to_7.py"
    if not mig.is_file():
        pytest.skip("6_to_7 migration not present in this tree")
    m = re.search(r"_TRANSIENT_MARKER\s*=\s*[\"']([^\"']+)[\"']", mig.read_text())
    assert m, "6_to_7.py no longer defines _TRANSIENT_MARKER — update the lock"
    assert m.group(1) == classify_mod.TRANSIENT_STATE_MARKER


# ─────────────────────── 2. Rust delta parity ───────────────────────

# Documented, DELIBERATE deltas between the Python union ignore-set and the
# Rust language-detection list (codegraph.rs::ignored_dirs()). The Rust list
# exists only to answer "does this project contain any supported source
# files?" with a 3-level walk — over-detection is harmless there, so it can
# afford to be smaller; and it unconditionally skips IDE/config dirs the
# Python side either doesn't need (no supported sources inside) or gates
# per-project (`.claude`).
_RUST_ONLY = {
    ".idea", ".vscode",  # IDE workspace dirs — never hold user source
    ".claude",           # Python gates this per-project (index_dot_claude);
                         # detection always skips it (launcher-managed).
}
_PYTHON_ONLY = {
    ".env",                                  # env-file dir variant (venv family)
    "worktrees", ".wt",                      # git-worktree containers
    ".svelte-kit", ".cache", ".parcel-cache", ".turbo", ".angular",  # codegen/caches
    "vendor", ".bundle", "obj", "bin", ".vs",  # language-extras union (P1b-2)
}


def _rust_ignored_dirs() -> set:
    text = _RUST_CODEGRAPH.read_text(encoding="utf-8")
    m = re.search(
        r"fn ignored_dirs\(\)[^{]*\{(.*?)\n\}", text, flags=re.DOTALL
    )
    assert m, "codegraph.rs::ignored_dirs() not found — update the parity test"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_rust_ignored_dirs_delta_is_exactly_the_documented_one(classify_mod):
    rust = _rust_ignored_dirs()
    python = set(classify_mod.CODEGRAPH_IGNORE_PARTS)
    unexpected_rust_only = rust - python - _RUST_ONLY
    assert not unexpected_rust_only, (
        f"codegraph.rs::ignored_dirs() grew undocumented entries "
        f"{sorted(unexpected_rust_only)} — add them to the Python union set "
        f"or document them in _RUST_ONLY with a rationale."
    )
    unexpected_python_only = python - rust - _PYTHON_ONLY
    assert not unexpected_python_only, (
        f"Python ignore-set grew entries {sorted(unexpected_python_only)} "
        f"missing from codegraph.rs::ignored_dirs() — mirror them in Rust or "
        f"document them in _PYTHON_ONLY with a rationale."
    )
    # The documented deltas must stay REAL (no stale doc rows).
    assert _RUST_ONLY <= rust and not (_RUST_ONLY & python)
    assert _PYTHON_ONLY <= python and not (_PYTHON_ONLY & rust)


# ─────────────────────── 3. classifier behavior (both copies) ───────────────────────


@pytest.fixture(scope="module", params=["vco_lib", "analyzer"])
def classify_fn(request, analyzer_mod, classify_mod):
    """Run every behavior test against BOTH implementations."""
    if request.param == "vco_lib":
        return classify_mod.classify_row
    return analyzer_mod.classify_row


def _repo(tmp_path_factory, rels):
    root = tmp_path_factory.mktemp("classify-repo")
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# real\n")
    return root


@pytest.fixture(scope="module")
def repo_root(tmp_path_factory):
    return _repo(
        tmp_path_factory,
        ["src/live.py", "target/generated.py", "coverage/report.js"],
    )


def test_pathless_stale_row_is_purgeable(classify_fn, repo_root):
    """IMMORTAL class #1 pre-v0.2.75: no walk keys on an empty path — the
    stale-file set skipped it and the orphan-clear kept it."""
    assert classify_fn(
        {"embed_revision": None, "file_path": ""},
        repo_root, current_revision=1,
    ) == "purgeable"
    # Missing property entirely — same class.
    assert classify_fn(
        {"embed_revision": 0}, repo_root, current_revision=1,
    ) == "purgeable"


def test_ignored_path_with_existing_file_is_purgeable(classify_fn, repo_root):
    """IMMORTAL class #2: the file EXISTS but the walk never descends into
    the ignored dir, so the row could never re-stamp."""
    assert classify_fn(
        {"embed_revision": 0, "file_path": "target/generated.py"},
        repo_root, current_revision=1,
    ) == "purgeable"
    assert classify_fn(
        {"embed_revision": None, "file_path": "coverage/report.js"},
        repo_root, current_revision=1,
    ) == "purgeable"


def test_transient_state_rows_purgeable_regardless_of_revision(classify_fn, repo_root):
    """F2/F4 semantics kept: the marker itself is the proof — even a
    CURRENT-revision transient row is purgeable."""
    assert classify_fn(
        {"embed_revision": 1, "file_path": ".claude/state/tool_backups/x.py"},
        repo_root, current_revision=1,
    ) == "purgeable"


def test_deleted_file_orphan_is_purgeable(classify_fn, repo_root):
    """D1 semantics kept: stored path gone from disk → orphan."""
    assert classify_fn(
        {"embed_revision": 0, "file_path": "src/gone.py"},
        repo_root, current_revision=1,
    ) == "purgeable"


def test_reachable_non_ignored_stale_rows_stay_owed(classify_fn, repo_root):
    """LEAVE-ALONE case: a re-walk can converge these — never purge, always
    count. Includes the rev-0 class (vectorless sentinel / R-3 module-row
    invalidation): the documented v0.2.75 ruling is OWED — the per-file gate
    re-walks rev-0 files, and the live pair that never healed was a
    deterministic ps1-parser crash (fixed), not a gate bug."""
    for rev in (0, None, 99, "garbage"):
        assert classify_fn(
            {"embed_revision": rev, "file_path": "src/live.py"},
            repo_root, current_revision=1,
        ) == "owed", f"rev={rev!r}"


def test_current_revision_rows_not_owed_and_never_purged(classify_fn, repo_root):
    assert classify_fn(
        {"embed_revision": 1, "file_path": "src/live.py"},
        repo_root, current_revision=1,
    ) == "not_owed"
    # Even pathless / ignored rows at the CURRENT revision are left alone
    # (conservative: they don't block convergence; never delete converged).
    assert classify_fn(
        {"embed_revision": 1, "file_path": ""},
        repo_root, current_revision=1,
    ) == "not_owed"
    assert classify_fn(
        {"embed_revision": 1, "file_path": "target/generated.py"},
        repo_root, current_revision=1,
    ) == "not_owed"


def test_extra_path_rows_not_owed(classify_fn, repo_root):
    """B1 tenant scoping kept: a different source root owns the row."""
    assert classify_fn(
        {
            "embed_revision": 0,
            "file_path": "src/gone.py",
            "project_source": "/somewhere/else",
        },
        repo_root, current_revision=1,
        primary_sources={repo_root.as_posix()},
    ) == "not_owed"
    # Same row WITHOUT source scoping (primary_sources=None) → judged
    # normally (pre-fix behaviour when the caller has no root context).
    assert classify_fn(
        {
            "embed_revision": 0,
            "file_path": "src/gone.py",
            "project_source": "/somewhere/else",
        },
        repo_root, current_revision=1,
    ) == "purgeable"


def test_no_root_fails_open_to_owed_for_path_bearing_rows(classify_fn):
    """Without a positively-known root, the deleted-file rule is skipped —
    never authorise a purge on uncertainty; over-counting owed is the
    conservative error."""
    assert classify_fn(
        {"embed_revision": 0, "file_path": "src/whatever.py"},
        None, current_revision=1,
    ) == "owed"
    # Pathless/ignored need no disk probe → still purgeable without a root.
    assert classify_fn(
        {"embed_revision": 0, "file_path": ""}, None, current_revision=1,
    ) == "purgeable"


def test_index_dot_claude_gates_the_claude_dir(classify_fn, tmp_path):
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "h.py").write_text("# hook\n")
    props = {"embed_revision": 0, "file_path": ".claude/hooks/h.py"}
    # Orchestrator root (indexes .claude/): the row is real source → owed.
    assert classify_fn(
        props, tmp_path, current_revision=1, index_dot_claude=True,
    ) == "owed"
    # User project (excludes .claude/): the walk never re-stamps it → purgeable.
    assert classify_fn(
        props, tmp_path, current_revision=1, index_dot_claude=False,
    ) == "purgeable"


# ─────────────────────── 4. two-sided same-fixture coverage ───────────────────────


class _Obj:
    def __init__(self, uuid, props):
        self.uuid = uuid
        self.properties = props


class _FakeData:
    def __init__(self):
        self.deleted = []
        self.fail_uuids = set()

    def delete_by_id(self, uuid):
        if uuid in self.fail_uuids:
            raise RuntimeError(f"simulated delete failure for {uuid}")
        self.deleted.append(uuid)


class _FakeColl:
    def __init__(self, name, rows, prop_names, agg_count=None):
        self.name = name
        self._rows = rows
        self.data = _FakeData()
        props = [types.SimpleNamespace(name=n) for n in prop_names]
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(properties=props)
        )
        self.aggregate = types.SimpleNamespace(
            over_all=lambda **kw: types.SimpleNamespace(total_count=agg_count)
        )
        self.iter_calls = 0

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        return iter(self._rows)


def _bind(analyzer_mod, obj, names):
    cls = analyzer_mod.CodeGraphAnalyzer
    for name in names:
        setattr(obj, name, getattr(cls, name).__get__(obj, obj.__class__))


_FIXTURE_PROPS = ("path", "file_path", "embed_revision", "project_source")


def _mixed_fixture_rows(kind: str):
    """One row per convergence class, path-keyed on ``kind`` ('path' for
    CodeModule shape / 'file_path' for Function-Class shape)."""
    def row(uuid, rev, path, source=""):
        props = {"embed_revision": rev, kind: path}
        if source:
            props["project_source"] = source
        return _Obj(uuid, props)

    return [
        row("u-owed-rev0",   0,    "src/live.py"),           # owed (rev-0 heals)
        row("u-owed-null",   None, "src/live.py"),           # owed (pre-migration)
        row("u-current",     1,    "src/live.py"),           # not_owed
        row("u-pathless",    0,    ""),                      # purgeable (immortal #1)
        row("u-ignored",     0,    "target/generated.py"),   # purgeable (immortal #2)
        row("u-transient",   1,    ".claude/state/b/x.py"),  # purgeable (marker)
        row("u-orphan",      0,    "src/gone.py"),           # purgeable (deleted file)
        row("u-extra-path",  0,    "src/gone.py", "/other/root"),  # not_owed (B1)
    ]


_EXPECTED_PURGED = {"u-pathless", "u-ignored", "u-transient", "u-orphan"}


def test_two_sided_probe_and_purge_agree_on_the_same_fixture(
    analyzer_mod, classify_mod, tmp_path,
):
    """THE two-sided lock: on ONE fixture holding every convergence class,
    (a) the analyzer's orphan-clear deletes exactly the purgeable rows and
    keeps exactly the owed paths in the re-walk set, and (b) the resync
    owed-probe counts exactly the owed rows — so the deferral can self-clear
    the moment the purge lands + the owed rows re-walk (no immortal class
    remains on either side)."""
    from vco_lib import codegraph_resync as cr

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("# real\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "generated.py").write_text("# build output\n")

    # ── analyzer side: _build_stale_file_set (stale set + orphan-clear) ──
    modules = _FakeColl(
        "P_CodeModule", _mixed_fixture_rows("path"), _FIXTURE_PROPS, agg_count=7,
    )
    classes = _FakeColl("P_CodeClass", [], _FIXTURE_PROPS, agg_count=0)
    functions = _FakeColl("P_CodeFunction", [], _FIXTURE_PROPS, agg_count=0)

    stub = types.SimpleNamespace(
        modules_collection=modules,
        classes_collection=classes,
        functions_collection=functions,
        _analyze_repo_root=tmp_path,
        index_dot_claude=True,
    )
    _bind(
        analyzer_mod, stub,
        ("_build_stale_file_set", "_count_stale_rows_in_collection"),
    )

    stale_set = stub._build_stale_file_set()
    assert stale_set == frozenset({"src/live.py"}), (
        "exactly the owed rows' paths re-walk — no pathless '' entry, no "
        f"ignored/orphan/extra-path noise: {stale_set}"
    )
    assert set(modules.data.deleted) == _EXPECTED_PURGED, (
        "the orphan-clear must purge exactly the purgeable classes "
        f"(got {modules.data.deleted})"
    )

    # ── resync side: the owed-probe on the SAME rows ──
    probe_coll = _FakeColl(
        "P_CodeModule", _mixed_fixture_rows("path"), _FIXTURE_PROPS, agg_count=7,
    )
    owed = cr._count_stale_in_collection(
        probe_coll, 1,
        path_prop="path",
        reachable_fn=cr._make_reachability_filter(tmp_path),
        primary_sources={tmp_path.as_posix()},
        index_dot_claude=True,
    )
    assert owed == 2, (
        "the probe counts exactly the owed rows (rev-0 + NULL on the live "
        f"file); purgeable/not_owed classes are excluded — got {owed}"
    )


def test_deferral_self_clears_on_fixture_holding_both_immortal_classes(
    monkeypatch, tmp_path,
):
    """A collection holding ONLY the two pre-v0.2.75 immortal classes
    (pathless + ignored-with-existing-file) now probes to ZERO owed — the
    resync gate reports not_owed and the pending ledger entry can self-clear
    instead of re-triggering a whole-repo resync on every --update."""
    from vco_lib import codegraph_resync as cr

    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "generated.py").write_text("# build output\n")

    rows = [
        _Obj("u-pathless", {"embed_revision": 0, "file_path": ""}),
        _Obj("u-ignored", {"embed_revision": None,
                           "file_path": "target/generated.py"}),
    ]
    module = _FakeColl("Proj_CodeModule", [], _FIXTURE_PROPS, agg_count=0)
    klass = _FakeColl("Proj_CodeClass", [], _FIXTURE_PROPS, agg_count=0)
    func = _FakeColl("Proj_CodeFunction", rows, _FIXTURE_PROPS, agg_count=2)
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")

    client = types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda name: name in (
                "Proj_CodeModule", "Proj_CodeClass", "Proj_CodeFunction",
            ),
            get=lambda name: {
                "Proj_CodeModule": module,
                "Proj_CodeClass": klass,
                "Proj_CodeFunction": func,
            }[name],
        ),
        close=lambda: None,
    )

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts == {
        "Proj_CodeModule": 0, "Proj_CodeClass": 0, "Proj_CodeFunction": 0,
    }, f"both immortal classes must probe to zero owed: {counts}"


def test_purge_failure_keeps_row_and_counts_failure(analyzer_mod, tmp_path):
    """No false converged via a SILENT failed purge: a delete_by_id failure
    on a purgeable row leaves it in place and is COUNTED (the accounting
    chain then flips the build success→partial — asserted separately in the
    PRUNE_FAILURES tests)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("# real\n")

    modules = _FakeColl(
        "P_CodeModule", _mixed_fixture_rows("path"), _FIXTURE_PROPS, agg_count=7,
    )
    modules.data.fail_uuids = {"u-pathless"}
    stub = types.SimpleNamespace(
        modules_collection=modules,
        classes_collection=None,
        functions_collection=None,
        _analyze_repo_root=tmp_path,
        index_dot_claude=True,
    )
    _bind(
        analyzer_mod, stub,
        ("_build_stale_file_set", "_count_stale_rows_in_collection"),
    )
    stale_set = stub._build_stale_file_set()
    assert stale_set is not None
    assert "u-pathless" not in modules.data.deleted
    assert set(modules.data.deleted) == _EXPECTED_PURGED - {"u-pathless"}
