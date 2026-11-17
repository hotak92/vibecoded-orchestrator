# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 — what a KG ``--all`` reports that is not a failure.

* A knowledge node with no title is a property of the user's FILE. As a run
  error it made ``generate-kg-summary.py --all`` exit 1 after every sync, so
  ``kg_node_formats_refresh_failed`` was re-raised each time and no retry could
  clear it. It is a named skip now (exit 0); a backend failure is still exit 1.
* The additive schema migration printed "Could not update schema: … already
  exists" on every ``--all`` and, worse, stopped at that property — the
  additions after it never ran.
* The refresh child's pipes were left to the garbage collector ("unclosed
  file" on every ``--all``).

All synthetic: no model, no Weaviate, no network.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import test_v02103_nodeformats_merge as NF  # noqa: E402
from tests import test_wiring_w8_shared_chunk_plan as W8  # noqa: E402

UNTITLED = "---\ntype: concept\n---\n\nprose with neither a title nor a heading.\n"


def _bulk(monkeypatch, capsys, kdir: Path, mod=None):
    mod = mod or NF._load_generator()
    if not getattr(mod, "_ladder_stubbed", False):
        NF._stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    return ei.value.code, capsys.readouterr().out


# ── a title-less node is a named skip ───────────────────────────────────────


def test_a_node_without_a_title_is_a_named_skip_and_the_run_exits_zero(
    tmp_path, monkeypatch, capsys,
):
    kdir = NF._seed_project(tmp_path / "proj", n=2)
    (kdir / "concepts" / "untitled.md").write_text(UNTITLED, encoding="utf-8")

    code, out = _bulk(monkeypatch, capsys, kdir)

    assert code == 0, out
    assert "generated: 2" in out and "errors: 0" in out and "no title: 1" in out, out
    assert "SKIP  knowledge/concepts/untitled.md (no title" in out, out
    assert "ERROR" not in out, out
    # named once more after the tally, so a long log still shows which files
    assert "Skipped 1 node(s) without a title" in out
    assert "knowledge/concepts/untitled.md" in out.split("Skipped 1 node(s)")[1]
    sidecar = json.loads((kdir / ".node_formats.json").read_text(encoding="utf-8"))
    assert "knowledge/concepts/untitled.md" not in sidecar
    assert len(sidecar) == 2


def test_only_title_less_nodes_still_exits_zero(tmp_path, monkeypatch, capsys):
    kdir = tmp_path / "proj" / "knowledge"
    (kdir / "concepts").mkdir(parents=True)
    (kdir / "concepts" / "untitled.md").write_text(UNTITLED, encoding="utf-8")
    code, out = _bulk(monkeypatch, capsys, kdir)
    assert code == 0, out
    assert "generated: 0" in out and "no title: 1" in out


def test_the_tally_line_is_unchanged_when_every_node_has_a_title(
    tmp_path, monkeypatch, capsys,
):
    kdir = NF._seed_project(tmp_path / "proj", n=1)
    code, out = _bulk(monkeypatch, capsys, kdir)
    assert code == 0, out
    assert "no title" not in out
    assert "errors: 0, archived: 0" in out


def test_a_backend_failure_is_still_exit_one(tmp_path, monkeypatch, capsys):
    """Leave-alone: a tier that failed is owed work, and exit 1 is how the
    sync learns it — also with a title-less node in the same run."""
    kdir = NF._seed_project(tmp_path / "proj", n=2)
    (kdir / "concepts" / "untitled.md").write_text(UNTITLED, encoding="utf-8")
    mod = NF._load_generator()
    mod._sb.reset_backend_cache()
    mod._sb._BACKEND_CACHE["choice"] = "cli"
    monkeypatch.setattr(mod._sb, "select_backend", lambda **_k: "cli")

    def _down(_prompt, **_kw):
        raise RuntimeError("summary backend transport failed")

    monkeypatch.setattr(mod._sb, "call_llm", _down)
    mod._ladder_stubbed = True

    code, out = _bulk(monkeypatch, capsys, kdir, mod)

    assert code == 1, out
    assert "errors: 2" in out and "no title: 1" in out, out
    assert "ERROR knowledge/concepts/node-0.md" in out


def test_the_sync_records_no_refresh_failure_for_a_title_less_node(
    tmp_path, monkeypatch,
):
    """Real order: kg-sync's post-walk refresh runs the REAL generator over a
    tree holding a title-less node. Before, its exit 1 re-raised
    ``kg_node_formats_refresh_failed`` after every sync."""
    root = tmp_path / "proj"
    kdir = NF._seed_project(root, 1)
    (kdir / "concepts" / "untitled.md").write_text(UNTITLED, encoding="utf-8")
    # every node with a title already has its entry: the run needs no backend
    gen = NF._load_generator()
    NF._stub_ladder(monkeypatch, gen)
    monkeypatch.setattr(gen, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sys, "argv", ["generate-kg-summary.py", "--all", "--knowledge-dir", str(kdir)])
    with pytest.raises(SystemExit):
        gen.main()

    mod = NF._load_sync(root, monkeypatch)
    import vco_lib.python_exe as pex

    monkeypatch.setattr(pex, "resolve_install_root", lambda *a, **k: REPO_ROOT)
    # the child must reach a selectable backend to get past the "no backend"
    # exit; `cli` is selected when a `claude` binary is on PATH — give it a
    # stub that is never called (every titled node is already cached).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ.get('PATH', '')}")
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "cli")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        mod._regen_node_formats_after_full_sync()
    out = buf.getvalue()
    if "no summary backend" in out:
        pytest.skip("no selectable summary backend in this environment")
    assert "no title: 1" in out, out
    cids = [e["condition_id"] for e in NF._deferred_entries(root)]
    assert "kg_node_formats_refresh_failed" not in cids, (cids, out[-1500:])


# ── the refresh child's pipes are closed ────────────────────────────────────


def test_the_refresh_closes_the_childs_pipes(tmp_path, monkeypatch):
    procs: list = []
    real_popen = subprocess.Popen

    def _spy(*a, **kw):
        proc = real_popen(*a, **kw)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _spy)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        NF._drive_regen_with_stub(tmp_path, monkeypatch, exit_code=0)
    piped = [p for p in procs if p.stdout is not None and p.stderr is not None]
    assert piped, "the refresh child was not spawned"
    for proc in piped:
        assert proc.stdout.closed and proc.stderr.closed


# ── the additive schema migration ───────────────────────────────────────────


class _Config:
    def __init__(self, listed, refuse=None, fail=None):
        self.listed = set(listed)
        self.refuse = set(refuse or ())  # the server has it; the read hid it
        self.fail = dict(fail or {})
        self.added: list = []
        self.refs_added: list = []

    def get(self):
        import types

        return types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in sorted(self.listed)],
            references=[],
        )

    def add_property(self, prop):
        if prop.name in self.fail:
            raise RuntimeError(self.fail[prop.name])
        if prop.name in self.refuse:
            raise RuntimeError(
                f"Collection configuration could not be updated: 422 property "
                f"name '{prop.name}' already exists")
        self.added.append(prop.name)

    def add_reference(self, ref):
        self.refs_added.append(ref.name)


def _ensure(tmp_path, config):
    import types

    pytest.importorskip("weaviate")
    mod = W8._load_sync_module(tmp_path)
    collection = types.SimpleNamespace(config=config)
    server = types.SimpleNamespace(client=types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda name: True, get=lambda name: collection)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = mod.ensure_collection_exists(server)
    return mod, ok, buf.getvalue()


@pytest.fixture
def sync_env():
    import os

    snapshot = {k: os.environ.get(k) for k in W8._ENV_KEYS}
    yield
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_an_already_existing_property_is_not_a_failed_schema_update(tmp_path, sync_env):
    """The field shape: the config read does not list the property, the add
    answers "already exists". Not a warning — and the additions AFTER it
    (another scalar, typed_links, external_links, linksTo) still happen."""
    probe, _ok, _out = _ensure(tmp_path, _Config(listed=()))
    every = list(probe._KG_NODE_SCALAR_PROPERTIES)
    assert every.index("secondary_truncated_slots") < every.index("truncation_measured_slots")
    listed = [n for n in every
              if n not in ("secondary_truncated_slots", "truncation_measured_slots")]
    config = _Config(listed=listed, refuse={"secondary_truncated_slots"})

    _mod, ok, out = _ensure(tmp_path, config)

    assert ok is True
    assert "Could not update schema" not in out, out
    assert "already exists" not in out, out
    assert "Adding property: secondary_truncated_slots" not in out, out
    assert config.added == ["truncation_measured_slots"] + [
        n for n in ("typed_links", "external_links") if n not in listed]
    assert "typed_links" in config.added
    assert config.refs_added == ["linksTo"]
    assert "✓ Schema up to date" in out


def test_a_property_that_is_listed_is_not_asked_for_again(tmp_path, sync_env):
    probe, _ok, _out = _ensure(tmp_path, _Config(listed=()))
    listed = list(probe._KG_NODE_SCALAR_PROPERTIES) + ["typed_links", "external_links"]
    config = _Config(listed=listed)
    _mod, ok, out = _ensure(tmp_path, config)
    assert ok is True and config.added == []
    assert "Adding property" not in out


def test_any_other_schema_error_still_warns(tmp_path, sync_env):
    """Leave-alone: a real failure keeps its warning and stops the block."""
    probe, _ok, _out = _ensure(tmp_path, _Config(listed=()))
    every = list(probe._KG_NODE_SCALAR_PROPERTIES)
    config = _Config(listed=every[:-1], fail={every[-1]: "503 schema is read-only"})
    _mod, ok, out = _ensure(tmp_path, config)
    assert ok is True
    assert "Could not update schema: 503 schema is read-only" in out
    assert config.added == []
