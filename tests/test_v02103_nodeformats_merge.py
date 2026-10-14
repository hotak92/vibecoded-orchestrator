# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103: ONE writer of `knowledge/.node_formats.json`.

Owner decision 2026-10-14: two scripts wrote the SAME sidecar —
``templates/scripts/generate-kg-summary.py`` (newer, per-edit, through the
shared summary ladder) and ``claude_mcp_servers/scripts/generate_node_formats.py``
(older, bulk ``--all``, Ollama-only). After every ``kg-sync --all`` the old
one loaded a 32K-ctx generation model on the GPU beside the code-embedding
re-index → GPU OOM in the field. The merge: the bulk mode lives in
``generate-kg-summary.py`` (every LLM call through
``summary_backends.call_llm``), the old script is retired, and every caller
spawns the merged generator.

Pinned here:

* the retired script is GONE from the shipped tree and nothing shipped
  references it (the printed recovery command included);
* bulk mode writes the SAME entry shape as the per-node mode;
* unchanged nodes are skipped by content hash on a re-run;
* every LLM call in bulk mode goes through ``summary_backends.call_llm``;
* bulk mode exits non-zero when no backend is available (the bulk callers
  record owed work off the exit code);
* BOTH callers (kg-sync's post-sync refresh, the schema-migration regen)
  spawn the merged generator with an argv its REAL argparse accepts —
  argv-shape-only assertions are not enough.

All synthetic: no Ollama, no Anthropic, no Weaviate, no network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"
RETIRED = REPO_ROOT / "claude_mcp_servers" / "scripts" / "generate_node_formats.py"
SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
RUST_SUMMARY_CMD = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "kg_summary.rs"

ARCHIVED_NODE_TEXT = (
    "---\n"
    "title: Old Node\n"
    "type: concept\n"
    "status: archived\n"
    "---\n"
    "\n"
    "Body prose of an archived node; the sync drops it from Weaviate.\n"
)

NODE_TEXT = (
    "---\n"
    "title: Merge Node\n"
    "type: concept\n"
    "---\n"
    "\n"
    "Body prose of the merge test node, long enough to be a summary target.\n"
)
FAKE_SUMMARY = "A plausible generated summary of the node."


def _load_generator(name_prefix: str = "_gks_merge"):
    """Fresh generator module (call sites pin KG_PROJECT_ROOT via
    monkeypatch when the single-file default root matters)."""
    spec = importlib.util.spec_from_file_location(
        f"{name_prefix}_{uuid.uuid4().hex}", GENERATOR
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_ladder(monkeypatch, mod, *, backend="cli"):
    """Point the generator's ladder at a fake tier; returns the prompts seen.
    The select stub also fills the backend cache, exactly as the real
    ``select_backend`` does — the sidecar entry's ``backend`` field reads it."""
    mod._sb.reset_backend_cache()
    prompts: list[str] = []

    def _select(**_kw):
        mod._sb._BACKEND_CACHE["choice"] = backend
        return backend

    monkeypatch.setattr(mod._sb, "select_backend", _select)
    monkeypatch.setattr(
        mod._sb, "call_llm", lambda p, **_kw: prompts.append(p) or FAKE_SUMMARY
    )
    return prompts


def _seed_project(root: Path, n: int = 1) -> Path:
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (kn / f"node-{i}.md").write_text(NODE_TEXT, encoding="utf-8")
    return root / "knowledge"


# ═══════════════════════════════════════════════════════════════════════════
# Retirement: gone from the shipped tree, nothing shipped references it
# ═══════════════════════════════════════════════════════════════════════════

def test_the_retired_script_is_gone_from_the_shipped_tree():
    assert not RETIRED.is_file(), (
        f"{RETIRED} still exists — the old Ollama-only sidecar writer must "
        "be retired (its bulk mode is merged into generate-kg-summary.py)"
    )


def test_no_shipped_code_references_the_retired_script():
    """Every CODE reference outside tests/ is gone — a shipped path naming a
    retired file is a false promise. (Docs, knowledge nodes and CHANGELOG
    history may keep naming it as provenance.)"""
    offenders: list[str] = []
    code_suffixes = {".py", ".rs", ".ts", ".tsx", ".toml", ".json",
                     ".sh", ".ps1", ".yml", ".yaml"}
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in code_suffixes:
            continue
        rel = path.relative_to(REPO_ROOT)
        parts = set(rel.parts)
        if parts & {"tests", ".claude", ".git", "node_modules"} or \
                str(rel).startswith(".claude"):
            continue
        if rel == Path("CHANGELOG.md"):
            continue  # release history keeps the name by design
        try:
            if "generate_node_formats.py" in path.read_text(encoding="utf-8",
                                                            errors="ignore"):
                offenders.append(str(rel))
        except OSError:
            continue
    assert offenders == [], f"stale references to the retired script: {offenders}"


def test_the_printed_recovery_command_names_the_merged_generator():
    """A printed command is shipped code: it must name the merged generator
    and actually work (its argv is parse-tested further below)."""
    src = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "generate-kg-summary.py --all --knowledge-dir" in src, (
        "the deferral's command_to_apply must run the merged generator"
    )
    assert "generate_node_formats" not in src, (
        "no reference to the retired generator may remain in kg-sync"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bulk mode behaviour
# ═══════════════════════════════════════════════════════════════════════════

def test_bulk_mode_writes_the_same_entry_shape_as_single_file_mode(
    tmp_path, monkeypatch, capsys
):
    bulk_root = tmp_path / "bulkproj"
    single_root = tmp_path / "singleproj"
    kdir_bulk = _seed_project(bulk_root)
    kdir_single = _seed_project(single_root)
    node_single = next(kdir_single.rglob("*.md"))

    # bulk run
    mod_bulk = _load_generator()
    _stub_ladder(monkeypatch, mod_bulk)
    monkeypatch.setattr(mod_bulk, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all",
                      "--knowledge-dir", str(kdir_bulk)]
    )
    with pytest.raises(SystemExit) as ei:
        mod_bulk.main()
    assert ei.value.code == 0, capsys.readouterr().out
    bulk_entry = json.loads(
        (kdir_bulk / ".node_formats.json").read_text(encoding="utf-8")
    )["knowledge/concepts/node-0.md"]

    # single-file run (the launcher / per-edit hook path — no SystemExit on
    # the success path; the module just returns)
    monkeypatch.setenv("KG_PROJECT_ROOT", str(single_root))
    mod_single = _load_generator()
    _stub_ladder(monkeypatch, mod_single)
    monkeypatch.setattr(mod_single, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node_single)])
    mod_single.main()
    single_entry = json.loads(
        (kdir_single / ".node_formats.json").read_text(encoding="utf-8")
    )["knowledge/concepts/node-0.md"]

    assert set(bulk_entry) == set(single_entry)
    assert bulk_entry["content_hash"] == single_entry["content_hash"]
    assert bulk_entry["backend"] == single_entry["backend"] == "cli"


def test_unchanged_nodes_are_skipped_on_a_bulk_rerun(tmp_path, monkeypatch, capsys):
    kdir = _seed_project(tmp_path / "proj", n=2)
    mod = _load_generator()
    _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    argv = ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)]

    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    first = capsys.readouterr().out
    assert "generated: 2" in first, first
    sidecar = (kdir / ".node_formats.json").read_text(encoding="utf-8")

    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    second = capsys.readouterr().out
    assert "generated: 0, skipped: 2" in second, second
    assert (kdir / ".node_formats.json").read_text(encoding="utf-8") == sidecar, (
        "a fully-skipped re-run must not rewrite the sidecar"
    )


def test_every_bulk_llm_call_goes_through_the_shared_ladder(
    tmp_path, monkeypatch, capsys
):
    kdir = _seed_project(tmp_path / "proj", n=1)
    mod = _load_generator()
    prompts = _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all",
                      "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    # description + summary — both answered by the ONE ladder entry point
    assert len(prompts) == 2, prompts
    assert all(p != "" for p in prompts)


def test_bulk_mode_exits_non_zero_when_no_backend_is_available(
    tmp_path, monkeypatch, capsys
):
    """The bulk callers (kg-sync refresh, schema-migration regen) turn the
    exit code into owed work; a silent exit-0 would mask stale summaries."""
    kdir = _seed_project(tmp_path / "proj", n=1)
    mod = _load_generator()
    _stub_ladder(monkeypatch, mod, backend="skip")
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all",
                      "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    assert not (kdir / ".node_formats.json").exists()


# ═══════════════════════════════════════════════════════════════════════════
# Caller wiring — the REAL parser must accept what each caller spawns
# ═══════════════════════════════════════════════════════════════════════════

def _load_sync(project_root: Path):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "TestProject_Development"
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    mod_name = f"_skg_merge_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SYNC_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_kg_sync_spawns_the_merged_generator_with_a_parseable_argv(
    tmp_path, monkeypatch
):
    """Drives the REAL ``_regen_node_formats_after_full_sync`` against a
    stub clone that records its argv, then feeds that exact argv to the
    merged generator's REAL argparse."""
    root = tmp_path / "proj"
    _seed_project(root, 1)
    mod = _load_sync(root)

    install_root = tmp_path / "orchestrator-clone"
    gen = install_root / "templates" / "scripts" / "generate-kg-summary.py"
    gen.parent.mkdir(parents=True)
    argv_record = tmp_path / "regen-argv.json"
    gen.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(argv_record)!r}).write_text(json.dumps(sys.argv))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    import vco_lib.python_exe as pex

    monkeypatch.setattr(pex, "resolve_install_root", lambda: install_root)

    mod._regen_node_formats_after_full_sync()

    assert argv_record.is_file(), "the refresh must spawn the generator"
    argv = json.loads(argv_record.read_text(encoding="utf-8"))
    assert argv[0].endswith("generate-kg-summary.py"), argv
    assert argv[1:] == ["--all", "--knowledge-dir", str(root / "knowledge")], argv
    # The REAL parser must accept it (not just the shape we asserted).
    gen_mod = _load_generator()
    parsed = gen_mod.build_parser().parse_args(argv[1:])
    assert parsed.all is True
    assert Path(parsed.knowledge_dir) == root / "knowledge"
    assert not parsed.force, "the automatic path must never pass --force"


def test_project_init_spawns_the_merged_generator_with_a_parseable_argv(
    tmp_path, monkeypatch
):
    """``_regenerate_node_formats`` (install/update regeneration) spawns the
    project's own merged generator in ONE bulk invocation."""
    from vco_lib import project_init

    folder = tmp_path / "proj"
    _seed_project(folder, 1)
    scripts = folder / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    gen = scripts / "generate-kg-summary.py"
    gen.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    recorded: list[list[str]] = []

    def _fake_run_quiet(argv, cwd):
        recorded.append(list(argv))
        return 0

    monkeypatch.setattr(project_init, "_subprocess_run_quiet", _fake_run_quiet)

    ok, detail = project_init._regenerate_node_formats(folder)

    assert len(recorded) == 1, recorded
    argv = recorded[0]
    assert argv[0].endswith("python3") or "python" in Path(argv[0]).name, argv
    assert Path(argv[1]) == gen, argv
    assert argv[2:] == ["--all", "--force"], argv
    # The REAL parser must accept it.
    gen_mod = _load_generator()
    parsed = gen_mod.build_parser().parse_args(argv[2:])
    assert parsed.all is True and parsed.force is True
    # The exit was 0 but the cache never changed → B1 defer (not a success).
    assert ok is False, detail


# ═══════════════════════════════════════════════════════════════════════════
# Archived nodes skip the summarization (owner decision 2026-10-14) — the
# ONE shared predicate the Weaviate sync uses
# ═══════════════════════════════════════════════════════════════════════════

def test_archived_node_by_frontmatter_skips_in_both_modes(
    tmp_path, monkeypatch, capsys
):
    """A frontmatter ``status: archived`` node is never summarised — in the
    per-file mode (what the launcher and the per-edit hook run) AND in the
    bulk ``--all`` mode — using the ONE shared predicate
    (``vco_lib.kg_node_status``), not a local copy."""
    root = tmp_path / "proj"
    kdir = root / "knowledge"
    node = kdir / "concepts" / "old.md"
    node.parent.mkdir(parents=True)
    node.write_text(ARCHIVED_NODE_TEXT, encoding="utf-8")

    # single-file mode: exit 0, nothing written, stable marker line
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load_generator()
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    assert "archived, skipping" in capsys.readouterr().out
    assert not (kdir / ".node_formats.json").exists()

    # bulk mode: same skip, counted as archived (never generated)
    mod2 = _load_generator()
    prompts = _stub_ladder(monkeypatch, mod2)
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod2.main()
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "archived, skipping" in out, out
    assert "generated: 0" in out and "archived: 1" in out, out
    assert prompts == [], "no LLM call may be made for an archived node"


def test_archived_node_by_path_skips_in_bulk(tmp_path, monkeypatch, capsys):
    """An ``archive/`` PATH segment (exact segment, per the shared predicate)
    skips the summarization the same way."""
    root = tmp_path / "proj"
    kdir = root / "knowledge"
    node = kdir / "archive" / "old.md"
    node.parent.mkdir(parents=True)
    node.write_text(NODE_TEXT, encoding="utf-8")

    mod = _load_generator()
    prompts = _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "archived, skipping" in out, out
    assert prompts == []


def test_active_node_is_still_summarised_alongside_archived_ones(
    tmp_path, monkeypatch, capsys
):
    """The leave-alone half: an ACTIVE node in the same tree still gets its
    summary while its archived sibling is skipped."""
    root = tmp_path / "proj"
    kdir = _seed_project(root, 1)  # knowledge/concepts/node-0.md (active)
    (kdir / "archive").mkdir()
    (kdir / "archive" / "old.md").write_text(ARCHIVED_NODE_TEXT, encoding="utf-8")

    mod = _load_generator()
    prompts = _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "generated: 1" in out and "archived: 1" in out, out
    assert len(prompts) == 2  # description + summary for the ACTIVE node only
    sidecar = json.loads((kdir / ".node_formats.json").read_text(encoding="utf-8"))
    assert "knowledge/concepts/node-0.md" in sidecar
    assert "knowledge/archive/old.md" not in sidecar


def test_stale_sidecar_entry_for_an_archived_node_is_dropped(
    tmp_path, monkeypatch, capsys
):
    """The sidecar must not keep SERVING summaries for a node the sync
    removed from Weaviate: a pre-existing entry for a now-archived node is
    removed in both modes."""
    root = tmp_path / "proj"
    kdir = root / "knowledge"
    node = kdir / "concepts" / "old.md"
    node.parent.mkdir(parents=True)
    node.write_text(ARCHIVED_NODE_TEXT, encoding="utf-8")
    sidecar = kdir / ".node_formats.json"
    sidecar.write_text(json.dumps({
        "knowledge/concepts/old.md": {
            "title": "Old Node", "description": "stale", "summary": "stale",
            "content_hash": "0" * 16, "backend": "cli",
        }
    }), encoding="utf-8")

    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load_generator()
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {}, (
        "the archived node's stale sidecar entry must be dropped in "
        "single-file mode"
    )

    # And the bulk half: re-seed a stale entry, run --all, it is gone again.
    sidecar.write_text(json.dumps({
        "knowledge/concepts/old.md": {"title": "Old Node", "summary": "stale"}
    }), encoding="utf-8")
    mod2 = _load_generator()
    _stub_ladder(monkeypatch, mod2)
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)]
    )
    with pytest.raises(SystemExit) as ei:
        mod2.main()
    assert ei.value.code == 0
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {}, (
        "the archived node's stale sidecar entry must be dropped in bulk mode"
    )


def test_the_archived_marker_matches_the_rust_launcher_constant():
    """Cross-language pin: the Python line's stable substring equals the
    launcher's ``ARCHIVED_MARKER`` (which counts the node as skipped)."""
    mod = _load_generator()
    rs = RUST_SUMMARY_CMD.read_text(encoding="utf-8")
    assert f'const ARCHIVED_MARKER: &str = "{mod.ARCHIVED_SKIP_MARKER}";' in rs, (
        "kg_summary.rs ARCHIVED_MARKER must equal the generator's "
        "ARCHIVED_SKIP_MARKER"
    )
