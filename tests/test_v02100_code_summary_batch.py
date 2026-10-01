# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-13 — what the post-update code-summary step costs.

Pins, through a FAKE ``claude`` on PATH (``.sh`` on POSIX, ``.cmd`` on
Windows, both running one Python script that records every spawn):

* N entities cost at most ceil(N / batch) summary spawns on the CLI tier;
* every summary spawn starts with no MCP server and no hook
  (``--strict-mcp-config --mcp-config <empty>`` + ``--settings`` carrying
  ``disableAllHooks``) — and only when the CLI's ``--help`` lists the flags;
  a CLI that lacks them is still used, without them;
* an entity the batch reply does not answer falls back to its own calls;
* CodeClass rows are summarised although CodeClass declares no
  ``n_callers`` (properties come from each collection's own schema);
* a failed collection scan is named, exits 1, and keeps that collection's
  sidecar entries;
* the resync driver spawns the generator AFTER the analyzer exits, and the
  install-time spawn no longer starts it beside the analyzer.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import stat
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_resync as cr  # noqa: E402

_GEN_PATH = REPO_ROOT / "templates" / "scripts" / "generate-code-summary.py"

_ENV_CLEAR = (
    "KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND", "VCO_CODE_SUMMARY_BATCH_SIZE",
    "VCO_SUMMARY_BREAKER", "VCO_CODE_SUMMARY_MAX_PER_RUN",
)

# The fake CLI. Argv of every invocation goes to $FAKE_CLAUDE_LOG (one JSON
# line); `--help` lists the isolation flags unless FAKE_CLAUDE_MODE=bare; a
# batch prompt is answered per its "Wanted:" lines, minus the item ids in
# FAKE_CLAUDE_DROP; any other prompt gets one plain sentence.
_FAKE_CLAUDE = r'''
import json, os, re, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
if argv == ["--help"]:
    lines = ["Usage: claude [options] [prompt]", "  -p, --print", "  --model <model>",
             "  --max-turns <n>", "  --no-session-persistence"]
    if os.environ.get("FAKE_CLAUDE_MODE") != "bare":
        lines += ["  --mcp-config <configs...>  Load MCP servers from JSON files",
                  "  --strict-mcp-config        Only use MCP servers from --mcp-config",
                  "  --settings <file-or-json>  Load additional settings"]
    print("\n".join(lines))
    sys.exit(0)
if "say ok" in argv:
    print("ok")
    sys.exit(0)
prompt = sys.stdin.read()
blocks = re.split(r"^### ITEM ", prompt, flags=re.M)[1:]
if not blocks:
    print("Resolves the requested value and returns it to the caller.")
    sys.exit(0)
drop = set(filter(None, os.environ.get("FAKE_CLAUDE_DROP", "").split(",")))
out = {}
for block in blocks:
    item = block.split("\n", 1)[0].strip()
    if item in drop:
        continue
    name = re.search(r"^Name: (.*)$", block, re.M).group(1)
    wanted = re.search(r"^Wanted: (.*)$", block, re.M).group(1)
    ans = {"one_liner": "Batch one-liner for %s: resolves its value." % name}
    if "summary" in wanted:
        ans["summary"] = "Batch summary for %s: reads its inputs and returns the result." % name
    m = re.search(r"chunks ([0-9, ]+)", wanted)
    if m:
        ans["chunks"] = {c.strip(): "Chunk %s of %s covers one section." % (c.strip(), name)
                         for c in m.group(1).split(",")}
    out[item] = ans
print(json.dumps(out))
'''


def _install_fake_claude(tmp_path: Path, monkeypatch) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        (bindir / "claude.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        sh = bindir / "claude"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                      encoding="utf-8")
        sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return log


def _calls(log: Path) -> list:
    if not log.is_file():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]


def _summary_spawns(log: Path) -> list:
    """`-p` invocations that are summary calls (not `--help`, not the probe)."""
    return [a for a in _calls(log) if "-p" in a and "say ok" not in a]


# ── a fake Weaviate whose iterator refuses undeclared properties ──────────


class _Coll:
    def __init__(self, name, declared, rows, *, config_fails=False):
        self.name = name
        self._declared = list(declared)
        self._rows = rows
        self._config_fails = config_fails
        self.config = types.SimpleNamespace(get=self._config_get)

    def _config_get(self):
        if self._config_fails:
            raise RuntimeError("schema endpoint unavailable")
        return types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in self._declared])

    def iterator(self, return_properties):
        unknown = [p for p in return_properties if p not in self._declared]
        if unknown:  # Weaviate rejects the whole query, as the field run did
            raise RuntimeError(f"no such prop with name '{unknown[0]}' found in class")
        for row in self._rows:
            yield types.SimpleNamespace(
                properties={k: row[k] for k in return_properties if k in row})


class _Client:
    def __init__(self, colls):
        self._colls = {c.name: c for c in colls}
        self.collections = types.SimpleNamespace(
            exists=lambda name: name in self._colls,
            get=lambda name: self._colls[name])

    def close(self):
        pass


_FUNC_PROPS = ["full_name", "file_path", "signature", "doc", "language",
               "content_hash", "total_chunks", "n_callers", "chunk_num",
               "function_body"]
# CodeClass carries no n_callers — the property whose request used to fail
# the whole CodeClass scan.
_CLASS_PROPS = [p for p in _FUNC_PROPS if p not in ("n_callers", "function_body")] + [
    "class_body"]


def _func_rows(n):
    return [{"full_name": f"m.f{i}", "file_path": "src/m.py", "signature": "def f()",
             "doc": "", "language": "python", "content_hash": f"h{i}",
             "total_chunks": 1, "n_callers": n - i, "chunk_num": 0,
             "function_body": "x = compute()\n" * 30} for i in range(n)]


def _class_rows(n, *, multichunk=False):
    rows = [{"full_name": f"m.C{i}", "file_path": "src/c.py", "signature": "class C",
             "doc": "", "language": "python", "content_hash": f"c{i}",
             "total_chunks": 1, "chunk_num": 0,
             "class_body": "def method(self):\n    return 1\n" * 20} for i in range(n)]
    if multichunk and rows:
        rows[0]["total_chunks"] = 2
    return rows


@pytest.fixture()
def gen(monkeypatch, tmp_path):
    for key in _ENV_CLEAR:
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / "vct-state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.setenv("KG_PROJECT_ROOT", str(tmp_path))
    sys.modules.pop("_wp13_gen", None)
    spec = importlib.util.spec_from_file_location("_wp13_gen", _GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_wp13_gen"] = mod
    spec.loader.exec_module(mod)
    sb = mod._sb
    sb.reset_backend_cache()
    sb.reset_breaker(persisted=True)
    lines: list = []
    sb.set_logger(lines.append)
    monkeypatch.setattr(mod, "log", lines.append)
    mod._test_log = lines
    # Only the CLI tier exists: the ladder must not wander to a real Ollama.
    monkeypatch.setattr(sb, "ollama_available", lambda: False)
    monkeypatch.setattr(sb, "openai_available", lambda: False)
    monkeypatch.setattr(sb, "api_available", lambda: False)
    monkeypatch.setattr(mod, "_collection_prefix", lambda name: "Proj")
    monkeypatch.setattr(
        mod, "_fetch_chunk_bodies",
        lambda client, prefix, base, full_name: [(1, "part one body"), (2, "part two body")])
    return mod


def _wire(monkeypatch, mod, funcs, classes, *, class_config_fails=False):
    client = _Client([
        _Coll("Proj_CodeFunction", _FUNC_PROPS, funcs),
        _Coll("Proj_CodeClass", _CLASS_PROPS, classes, config_fails=class_config_fails),
    ])
    monkeypatch.setattr(mod, "_connect_weaviate", lambda: client)


def _sidecar(tmp_path):
    path = tmp_path / ".claude" / ".code_formats.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def test_n_entities_cost_at_most_ceil_n_over_batch_spawns(gen, monkeypatch, tmp_path):
    log = _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("VCO_CODE_SUMMARY_BATCH_SIZE", "4")
    _wire(monkeypatch, gen, _func_rows(10), _class_rows(3, multichunk=True))

    rc = gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False)

    assert rc == 0
    data = _sidecar(tmp_path)
    assert len(data) == 13
    spawns = _summary_spawns(log)
    assert 0 < len(spawns) <= math.ceil(13 / 4), (
        f"{len(spawns)} CLI spawns for 13 entities at batch 4")
    # Every entity came from a batch answer, kind-correct, chunks included.
    assert all(e["one_liner"].startswith("Batch one-liner") for e in data.values())
    big = data["src/c.py::m.C0"]
    assert big["collection"] == "CodeClass"
    assert set(big["chunk_summaries"]) == {"1", "2"}


def test_codeclass_rows_are_summarised_without_n_callers(gen, monkeypatch, tmp_path):
    _install_fake_claude(tmp_path, monkeypatch)
    _wire(monkeypatch, gen, [], _class_rows(3))

    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 0
    keys = set(_sidecar(tmp_path))
    assert keys == {"src/c.py::m.C0", "src/c.py::m.C1", "src/c.py::m.C2"}
    assert gen.row_properties("CodeClass", set(_CLASS_PROPS)).count("n_callers") == 0


def test_every_summary_spawn_starts_without_mcp_servers_or_hooks(gen, monkeypatch, tmp_path):
    log = _install_fake_claude(tmp_path, monkeypatch)
    _wire(monkeypatch, gen, _func_rows(3), [])

    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 0
    spawns = _summary_spawns(log)
    assert spawns
    probe = [a for a in _calls(log) if "say ok" in a]
    for argv in spawns + probe:
        assert "--strict-mcp-config" in argv
        cfg = Path(argv[argv.index("--mcp-config") + 1])
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"mcpServers": {}}
        settings = Path(argv[argv.index("--settings") + 1])
        assert json.loads(settings.read_text(encoding="utf-8")) == {"disableAllHooks": True}
    helps = [a for a in _calls(log) if a == ["--help"]]
    assert len(helps) == 1, "the flag probe runs once per process, not per spawn"


def test_a_cli_without_the_flags_is_still_used_without_them(gen, monkeypatch, tmp_path):
    log = _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bare")
    _wire(monkeypatch, gen, _func_rows(3), [])

    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 0
    assert len(_sidecar(tmp_path)) == 3
    spawns = _summary_spawns(log)
    assert spawns and not any("--strict-mcp-config" in a or "--settings" in a
                              for a in spawns)
    assert any("does not offer" in ln for ln in gen._test_log)


def test_an_entity_missing_from_the_batch_reply_falls_back_to_its_own_calls(
        gen, monkeypatch, tmp_path):
    log = _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("VCO_CODE_SUMMARY_BATCH_SIZE", "4")
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "e2")
    _wire(monkeypatch, gen, _func_rows(4), [])

    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 0
    data = _sidecar(tmp_path)
    assert len(data) == 4
    # e2 is the second entity by priority (n_callers desc) → m.f1.
    assert not data["src/m.py::m.f1"]["one_liner"].startswith("Batch")
    assert data["src/m.py::m.f0"]["one_liner"].startswith("Batch")
    # 1 batch call + one_liner + summary for the dropped entity.
    assert len(_summary_spawns(log)) == 3


def test_batch_size_one_turns_batching_off(gen, monkeypatch, tmp_path):
    log = _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("VCO_CODE_SUMMARY_BATCH_SIZE", "1")
    _wire(monkeypatch, gen, _func_rows(3), [])

    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 0
    assert len(_summary_spawns(log)) == 6, "one_liner + summary per entity"
    assert gen.resolve_batch_size({"VCO_CODE_SUMMARY_BATCH_SIZE": "0"}) == gen.DEFAULT_BATCH_SIZE
    assert gen.resolve_batch_size({"VCO_CODE_SUMMARY_BATCH_SIZE": "x"}) == gen.DEFAULT_BATCH_SIZE
    assert gen.resolve_batch_size({}) == gen.DEFAULT_BATCH_SIZE


def test_a_failed_scan_exits_1_names_it_and_keeps_its_entries(gen, monkeypatch, tmp_path):
    _install_fake_claude(tmp_path, monkeypatch)
    sidecar = tmp_path / ".claude" / ".code_formats.json"
    sidecar.parent.mkdir(parents=True)
    kept = {"src/c.py::m.Old": {"collection": "CodeClass", "one_liner": "Kept class."}}
    sidecar.write_text(json.dumps(kept), encoding="utf-8")
    _wire(monkeypatch, gen, _func_rows(2), _class_rows(1), class_config_fails=True)

    rc = gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False)

    assert rc == 1
    data = _sidecar(tmp_path)
    assert "src/c.py::m.Old" in data, "a failed scan must not GC that collection"
    assert "src/m.py::m.f0" in data, "the healthy collection still generates"
    assert any("SCAN FAILED for Proj_CodeClass" in ln for ln in gen._test_log)


def test_an_unreachable_weaviate_exits_1(gen, monkeypatch, tmp_path):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setattr(gen, "_connect_weaviate", lambda: None)
    assert gen.run("Proj", project_root=tmp_path, max_per_run=150, force=False) == 1


def test_batch_reply_parser_tolerates_fences_and_ignores_unknown_ids(gen):
    sb = gen._sb
    reply = '```json\n{"e1": {"one_liner": "a"}, "zz": 1}\n```'
    assert sb.parse_batch_reply(reply, ["e1", "e2"]) == {"e1": {"one_liner": "a"}}
    assert sb.parse_batch_reply("no json here", ["e1"]) == {}
    assert sb.parse_batch_reply("[1, 2]", ["e1"]) == {}


# ── the resync driver: the generator starts AFTER the analyzer ────────────


def _driver_tree(tmp_path: Path) -> Path:
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub analyzer\n", encoding="utf-8")
    (scripts / "generate-code-summary.py").write_text("# stub generator\n",
                                                      encoding="utf-8")
    return scripts / "analyze_code_graph.py"


def test_driver_spawns_the_summary_generator_after_the_analyzer(monkeypatch, tmp_path):
    analyzer = _driver_tree(tmp_path)
    events: list = []
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: {})
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_report_terminal_to_hub", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral", lambda *a, **k: None)
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **kw: (events.append(("run", list(argv))),
                            types.SimpleNamespace(returncode=0))[1])
    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (events.append(("popen", list(argv))),
                            types.SimpleNamespace(pid=77))[1])
    before = len(cr._DETACHED_CHILDREN)

    assert cr.run_resync_and_verify("MyProj", tmp_path, analyzer) == 0

    kinds = [(k, any(str(a).endswith("generate-code-summary.py") for a in argv))
             for k, argv in events]
    assert kinds.index(("run", False)) < kinds.index(("popen", True)), events
    summary_argv = next(a for k, a in events if k == "popen")
    assert summary_argv[-4:] == ["--project", "MyProj", "--project-root", str(tmp_path)]
    del cr._DETACHED_CHILDREN[before:]


def test_driver_does_not_spawn_the_generator_when_the_analyzer_never_started(
        monkeypatch, tmp_path):
    analyzer = _driver_tree(tmp_path)
    popens: list = []
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: {})
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_report_terminal_to_hub", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral", lambda *a, **k: None)

    def _boom(argv, **kw):
        raise OSError("cannot exec")

    monkeypatch.setattr(cr.subprocess, "run", _boom)
    monkeypatch.setattr(cr.subprocess, "Popen",
                        lambda argv, **kw: popens.append(list(argv)))

    assert cr.run_resync_and_verify("MyProj", tmp_path, analyzer) == 0
    assert popens == []


def test_the_install_time_spawn_no_longer_starts_the_generator(monkeypatch, tmp_path):
    state_dir = tmp_path / "vct-state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: "current")
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    repo = tmp_path / "repo"
    _driver_tree(repo)
    spawned: list = []
    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (spawned.append(list(argv)),
                            types.SimpleNamespace(pid=4321))[1])
    before = len(cr._DETACHED_CHILDREN)

    result = cr.spawn_background_resync(repo, "MyProj", python_exe=sys.executable,
                                        check_owed=False)

    assert result.status == "launched", result.message
    assert spawned, "the driver and its riders still spawn"
    assert not any(str(a).endswith("generate-code-summary.py")
                   for argv in spawned for a in argv)
    del cr._DETACHED_CHILDREN[before:]
