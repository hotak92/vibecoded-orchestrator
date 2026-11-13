# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Startup precedence between the bundled checkpoint and local ones (v0.2.106).

The rule (``rl_server.choose_local_checkpoint``): the bundled ``current``
wins over a local checkpoint with no training history (a fresh
initialisation such as ``rl_model_<p>_local.pt``) or an older base; a local
checkpoint with its own training on top of the CURRENT bundled base (or a
newer base) is kept. Before v0.2.106 the server picked the lexically
greatest file name, so ``..._local.pt`` (sorting after ``..._0.x.y.pt``)
shadowed freshly shipped weights.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from rl_reranker import bundled_weights as bw  # noqa: E402
from rl_reranker.rl_model import RLModel, read_checkpoint_meta  # noqa: E402
from rl_reranker.rl_server import RLServer, choose_local_checkpoint  # noqa: E402

EMB = 1024


def _ship(root: Path, profile: str, version: str) -> Path:
    rel = bw.weights_relpath(profile, version)
    path = root / Path(*rel.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    m = RLModel.load_or_create(state_path=path, emb_dim=EMB, device="cpu",
                               weights_version=version, embedding_source=profile)
    m.mark_as_release_base()
    m.save()
    profiles = {"qwen3": {"current": None, "versions": {}},
                "arctic": {"current": None, "versions": {}}}
    profiles[profile] = {"current": version, "versions": {
        version: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}
    (root / bw.MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "profiles": profiles}), encoding="utf-8")
    return path


def _local(path: Path, profile: str, *, weights_version: str = "", base: str = "",
           steps: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    m = RLModel.load_or_create(state_path=path, emb_dim=EMB, device="cpu",
                               weights_version=weights_version, embedding_source=profile)
    m._base_weights_version = base
    m.record_local_train_steps(steps)
    m.save()
    return path


def _server(tmp_path, monkeypatch, profile, weights_root):
    monkeypatch.setenv("ACTIVE_EMBEDDING", profile)
    monkeypatch.delenv("RL_MODEL_TAG", raising=False)
    monkeypatch.delenv("RL_EMB_DIM", raising=False)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("RL_DEVICE", "cpu")
    return RLServer(project_root=tmp_path / "project",
                    log_path=str(tmp_path / "logs" / "rl_events.jsonl"), port=0,
                    weights_root=weights_root)


async def _health(srv):
    server = TestServer(srv._app)
    await server.start_server()
    client = TestClient(server)
    try:
        resp = await client.get("/health")
        return await resp.json()
    finally:
        await client.close()
        await server.close()


# ─── server startup ────────────────────────────────────────────────────


def test_bundled_wins_over_a_fresh_local_checkpoint(tmp_path, monkeypatch):
    """The reported bug: an untrained ``_local.pt`` shadowed shipped weights."""
    root = tmp_path / "weights"
    _ship(root, "qwen3", "0.3.0")
    state = tmp_path / "project" / "state"
    _local(state / "rl_model_qwen3_local.pt", "qwen3")
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    assert srv._state_path.name == "rl_model_qwen3_0.3.0.pt"
    health = asyncio.run(_health(srv))
    assert health["trained"] is True
    assert health["weights"]["loaded_version"] == "0.3.0"
    # The local file is left alone (never deleted).
    assert (state / "rl_model_qwen3_local.pt").is_file()


def test_bundled_wins_over_a_trained_checkpoint_on_an_older_base(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship(root, "qwen3", "0.3.0")
    state = tmp_path / "project" / "state"
    _local(state / "rl_model_qwen3_0.2.9_finetuned.pt", "qwen3",
           weights_version="0.2.9", base="0.2.9", steps=500)
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    assert srv._state_path.name == "rl_model_qwen3_0.3.0.pt"


def test_local_training_on_the_current_base_is_kept(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship(root, "qwen3", "0.3.0")
    state = tmp_path / "project" / "state"
    kept = _local(state / "rl_model_qwen3_0.3.0_finetuned.pt", "qwen3",
                  weights_version="0.3.0", base="0.3.0", steps=12)
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    assert srv._state_path == kept
    assert srv._rl._rl_model is not None
    assert srv._rl._rl_model.local_train_steps == 12


def test_a_newer_base_is_kept(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship(root, "arctic", "0.3.0")
    state = tmp_path / "project" / "state"
    kept = _local(state / "rl_model_arctic_0.4.0.pt", "arctic",
                  weights_version="0.4.0", base="0.4.0")
    srv = _server(tmp_path, monkeypatch, "arctic", root)
    assert srv._state_path == kept


def test_without_bundled_weights_the_old_rule_stands(tmp_path, monkeypatch):
    state = tmp_path / "project" / "state"
    local = _local(state / "rl_model_qwen3_local.pt", "qwen3")
    _local(state / "rl_model_qwen3_0.2.9.pt", "qwen3", weights_version="0.2.9")
    srv = _server(tmp_path, monkeypatch, "qwen3", None)
    assert srv._state_path == local


def test_project_head_with_a_fresh_checkpoint_uses_the_bundled_base(tmp_path, monkeypatch):
    from rl_reranker.rl_server import _per_project_state_dir

    root = tmp_path / "weights"
    _ship(root, "qwen3", "0.3.0")
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    pdir = _per_project_state_dir(srv._state_dir, "p-123")
    _local(pdir / "rl_model_qwen3_local.pt", "qwen3")
    head = asyncio.run(srv._get_or_load_project_head("p-123"))
    assert head.is_base_fallback is True
    assert head.rl_model.weights_version == "0.3.0"
    assert srv._routing_counters["project_head_superseded_by_bundled"] == 1
    # v0.2.106: started from the base, the project trains and saves under its
    # OWN dir, named for that base — never over the base file other projects
    # start from.
    assert head.state_path == pdir / "rl_model_qwen3_0.3.0.pt"
    assert head.state_path != srv._state_path
    assert head.learner is not None and head.project_dir == pdir


def test_project_head_trained_on_the_current_base_is_kept(tmp_path, monkeypatch):
    from rl_reranker.rl_server import _per_project_state_dir

    root = tmp_path / "weights"
    _ship(root, "qwen3", "0.3.0")
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    pdir = _per_project_state_dir(srv._state_dir, "p-456")
    own = _local(pdir / "rl_model_qwen3_0.3.0.pt", "qwen3",
                 weights_version="0.3.0", base="0.3.0", steps=3)
    head = asyncio.run(srv._get_or_load_project_head("p-456"))
    assert head.is_base_fallback is False
    assert head.state_path == own


# ─── the pure rule ──────────────────────────────────────────────────────


def _meta(base="", wv="", steps=0):
    return {"base_weights_version": base, "weights_version": wv, "local_train_steps": steps}


@pytest.mark.parametrize("meta,expected", [
    (_meta(), False),                                  # fresh init
    (_meta(wv="local"), False),                        # local sentinel
    (_meta(base="0.2.0", wv="0.2.0", steps=99), False),  # older base
    (_meta(base="0.3.0", wv="0.3.0", steps=0), False),   # current base, untrained copy
    (_meta(base="0.3.0", wv="0.3.0", steps=1), True),    # current base + own training
    (_meta(wv="0.3.0", steps=4), True),                  # pre-field base falls back to weights_version
    (_meta(base="0.3.1", wv="0.3.1"), True),             # newer base
    (None, False),                                       # unreadable
])
def test_choose_local_checkpoint_rule(tmp_path, meta, expected):
    cand = tmp_path / "rl_model_qwen3_x.pt"
    cand.write_bytes(b"x")
    got = choose_local_checkpoint([cand], "0.3.0", read_meta=lambda p: meta)
    assert (got == cand) is expected


def test_choose_prefers_newest_base_then_most_steps(tmp_path):
    a, b, c = (tmp_path / f"rl_model_qwen3_{n}.pt" for n in "abc")
    for p in (a, b, c):
        p.write_bytes(b"x")
    metas = {a: _meta(base="0.3.0", steps=5), b: _meta(base="0.3.0", steps=50),
             c: _meta(base="0.3.0", steps=0)}
    assert choose_local_checkpoint([a, b, c], "0.3.0", read_meta=metas.get) == b
    metas[c] = _meta(base="0.4.0")
    assert choose_local_checkpoint([a, b, c], "0.3.0", read_meta=metas.get) == c


def test_training_on_a_launcher_managed_copy_does_not_count(tmp_path):
    """Ownership comes from the shared table: a marked copy is the launcher's."""
    from vco_lib.rl_weights_ownership import managed_marker_path

    cand = tmp_path / "rl_model_qwen3_0.3.0_dl.pt"
    cand.write_bytes(b"x")
    meta = _meta(base="0.3.0", wv="0.3.0", steps=7)
    assert choose_local_checkpoint([cand], "0.3.0", read_meta=lambda p: meta) == cand
    managed_marker_path(cand).write_bytes(b"vct-managed")
    assert choose_local_checkpoint([cand], "0.3.0", read_meta=lambda p: meta) is None
    # a managed copy of a NEWER release still wins on its version
    newer = _meta(base="0.4.0", wv="0.4.0")
    assert choose_local_checkpoint([cand], "0.3.0", read_meta=lambda p: newer) == cand


def test_ownership_classes_follow_the_table(tmp_path):
    from vco_lib import rl_weights_ownership as own

    plain = tmp_path / "a.pt"
    plain.write_bytes(b"x")
    marked = tmp_path / "b.pt"
    marked.write_bytes(b"x")
    own.managed_marker_path(marked).write_bytes(b"vct-managed")
    assert own.classify(plain) == "user"
    assert own.classify(marked) == "managed"
    assert own.classify(tmp_path / "absent.pt") is None
    assert own.managed_marker_path(plain).name == "a.pt.vct-managed"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_checkpoint_is_managed(tmp_path):
    from vco_lib import rl_weights_ownership as own

    plain = tmp_path / "a.pt"
    plain.write_bytes(b"x")
    link = tmp_path / "c.pt"
    link.symlink_to(plain)
    assert own.classify(link) == "managed"


def test_ownership_table_parses_and_rejects_bad_tables():
    from vco_lib import rl_weights_ownership as own

    doc = own.parse_table(own.table_path().read_text(encoding="utf-8"))
    assert doc["ownership"] == {"symlink": "managed", "file_with_marker": "managed",
                                "file_without_marker": "user"}
    with pytest.raises(ValueError):
        own.parse_table("format_version = 2\n")
    with pytest.raises(ValueError):
        own.parse_table('format_version = 1\nmanaged_marker_suffix = "x/y"\n')
    with pytest.raises(ValueError):
        own.parse_table('format_version = 1\nmanaged_marker_suffix = ".m"\n'
                        '[ownership]\nsymlink = "nobody"\nfile_with_marker = "managed"\n'
                        'file_without_marker = "user"\n')


def test_excluded_seed_copy_is_never_chosen(tmp_path):
    seed = tmp_path / "rl_model_qwen3_0.3.0.pt"
    seed.write_bytes(b"x")
    got = choose_local_checkpoint([seed], "0.3.0", exclude=(seed,),
                                  read_meta=lambda p: _meta(base="0.3.0", steps=9))
    assert got is None


# ─── the metadata the rule reads ───────────────────────────────────────


def test_training_history_round_trips_and_recorded_steps_count(tmp_path):
    """v0.2.106: online steps are recorded by the per-project learner
    (``record_local_train_steps``); the per-node ``update`` is gone."""
    path = tmp_path / "rl_model_qwen3_0.3.0.pt"
    m = RLModel.load_or_create(state_path=path, emb_dim=16, device="cpu",
                               weights_version="0.3.0", embedding_source="qwen3")
    m.mark_as_release_base()
    m.record_local_train_steps(2)
    m.save()
    meta = read_checkpoint_meta(path)
    assert meta is not None
    assert meta["base_weights_version"] == "0.3.0"
    assert meta["local_train_steps"] == 2
    again = RLModel.load_or_create(state_path=path, emb_dim=16, device="cpu",
                                   embedding_source="qwen3")
    assert again.base_weights_version == "0.3.0"
    assert again.local_train_steps == 2


def test_old_checkpoints_without_the_fields_read_as_no_history(tmp_path):
    path = tmp_path / "old.pt"
    torch.save({"weights_version": "0.2.6", "arch_version": "v2", "emb_dim": 16}, path)
    meta = read_checkpoint_meta(path)
    assert meta is not None
    assert meta["base_weights_version"] == ""
    assert meta["local_train_steps"] == 0


def test_unreadable_checkpoint_meta_is_none(tmp_path):
    path = tmp_path / "junk.pt"
    path.write_bytes(os.urandom(64))
    assert read_checkpoint_meta(path) is None
