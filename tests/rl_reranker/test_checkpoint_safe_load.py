# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 CodeQL py/unsafe-deserialization: every checkpoint read in
``rl_model`` goes through ``load_checkpoint`` (``torch.load(...,
weights_only=True)``), which REFUSES a ``.pt`` carrying a pickled callable
— and every checkpoint this module writes still loads.

The red-proof is in the payload itself: the same file loaded with
``weights_only=False`` DOES run the callable (it creates a directory), so a
loader that dropped ``weights_only=True`` would fail these tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from rl_reranker import rl_model as rm  # noqa: E402
from rl_reranker.bundled_weights import (  # noqa: E402
    resolve_bundled_weights,
    sha256_file,
    weights_relpath,
)
from rl_reranker.rl_model import RLModel, load_checkpoint, read_checkpoint_meta  # noqa: E402

EMB = 32


class _Payload:
    """Pickles as ``os.mkdir(<marker>)`` — runs on any unrestricted load."""

    def __init__(self, marker: Path) -> None:
        self.marker = str(marker)

    def __reduce__(self):
        return (os.mkdir, (self.marker,))


def _malicious(path: Path, marker: Path, *, as_sidecar: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if as_sidecar:
        obj = {"name": "evil", "emb": _Payload(marker), "version": 1}
    else:
        obj = {"arch_version": "v2", "emb_dim": EMB, "state_dict": {}, "x": _Payload(marker)}
    torch.save(obj, str(path))
    return path


def test_the_payload_is_live_without_weights_only(tmp_path):
    """Guards the guard: an unrestricted load executes the payload."""
    marker = tmp_path / "pwned"
    evil = _malicious(tmp_path / "evil.pt", marker)
    torch.load(str(evil), map_location="cpu", weights_only=False)
    assert marker.exists()


def test_load_checkpoint_refuses_a_pickled_callable(tmp_path):
    marker = tmp_path / "pwned"
    evil = _malicious(tmp_path / "evil.pt", marker)
    with pytest.raises(Exception) as info:
        load_checkpoint(evil)
    assert "weights_only" in str(info.value) or "Unsupported" in str(info.value)
    assert not marker.exists()


def test_model_load_refuses_and_reinitialises(tmp_path):
    marker = tmp_path / "pwned"
    evil = _malicious(tmp_path / "state" / "rl_model_x.pt", marker)
    model = RLModel.load_or_create(state_path=evil, emb_dim=EMB)
    assert not marker.exists()
    assert model.weights_version == ""  # fresh init, nothing taken from the file


def test_checkpoint_meta_refuses(tmp_path):
    marker = tmp_path / "pwned"
    evil = _malicious(tmp_path / "evil.pt", marker)
    assert read_checkpoint_meta(evil) is None
    assert not marker.exists()


def test_sidecar_load_refuses_and_keeps_the_rest(tmp_path):
    state = tmp_path / "state" / "rl_model_x.pt"
    good = RLModel.load_or_create(state_path=state, emb_dim=EMB)
    good.get_type_idx("custom_type_a")
    good.save()
    marker = tmp_path / "pwned"
    _malicious(state.parent / "type_embs" / "zz_evil__0000.pt", marker, as_sidecar=True)
    back = RLModel.load_or_create(state_path=state, emb_dim=EMB)
    assert not marker.exists()
    assert "custom_type_a" in back._type_registry
    assert "evil" not in back._type_registry


def test_every_written_checkpoint_round_trips(tmp_path):
    """The network checkpoint, optimizer state, metadata and dynamic-type
    sidecars all load under ``weights_only=True`` (no allowlist needed)."""
    state = tmp_path / "state" / "rl_model_x.pt"
    m = RLModel.load_or_create(state_path=state, emb_dim=EMB, weights_version="0.2.106",
                               embedding_source="qwen3")
    idx = m.get_type_idx("custom_type_b")
    with torch.no_grad():
        m._net.type_embs.weight[idx].fill_(0.25)
    m.save()
    raw = load_checkpoint(state)
    assert raw["weights_version"] == "0.2.106" and raw["embedding_source"] == "qwen3"
    assert "opt_state" in raw and raw["state_dict"]

    back = RLModel.load_or_create(state_path=state, emb_dim=EMB, embedding_source="qwen3")
    assert back.weights_version == "0.2.106"
    row = back._type_registry["custom_type_b"]
    assert torch.allclose(back._net.type_embs.weight[row], torch.full_like(
        back._net.type_embs.weight[row], 0.25))
    for k, v in m._net.state_dict().items():
        if k != "type_embs.weight":
            assert torch.equal(v.cpu(), back._net.state_dict()[k].cpu()), k
    meta = read_checkpoint_meta(state)
    assert meta is not None and meta["weights_version"] == "0.2.106"

    # A project's online checkpoint: loaded from the base, saved under its
    # own dir (retarget), read back.
    proj = tmp_path / "state" / "projects" / "p1" / "rl_model_x_0.2.106.pt"
    back.retarget(proj)
    back.save()
    again = RLModel.load_or_create(state_path=proj, emb_dim=EMB, embedding_source="qwen3")
    assert again.weights_version == "0.2.106"
    assert "custom_type_b" in again._type_registry


def test_bundled_weights_load(tmp_path):
    """The shipped-weights path: manifest -> verified file -> model."""
    root = tmp_path / "weights"
    rel = weights_relpath("qwen3", "0.2.106")
    ckpt = root.joinpath(*rel.parts)
    m = RLModel.load_or_create(state_path=ckpt, emb_dim=EMB, weights_version="0.2.106")
    m.save()
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "profiles": {
            "qwen3": {"current": "0.2.106",
                      "versions": {"0.2.106": {"sha256": sha256_file(ckpt)}}},
            "arctic": {"current": None, "versions": {}},
        },
    }), encoding="utf-8")
    bundled = resolve_bundled_weights(root, "qwen3")
    assert bundled is not None
    loaded = RLModel.load_or_create(state_path=bundled.path, emb_dim=EMB)
    assert loaded.weights_version == "0.2.106"


def test_file_derived_values_are_logged_on_one_line(tmp_path, caplog):
    """RED-PROOF (CodeQL py/log-injection): strings read from a checkpoint
    (arch, embedding source) are logged through ``log_safe``."""
    path = tmp_path / "state" / "rl_model_x.pt"
    path.parent.mkdir(parents=True)
    torch.save({"arch_version": "v9\nINFO forged record", "emb_dim": EMB, "state_dict": {}},
               str(path))
    with caplog.at_level("WARNING", logger=rm.logger.name):
        RLModel.load_or_create(state_path=path, emb_dim=EMB)
    msgs = [r.getMessage() for r in caplog.records if "checkpoint arch" in r.getMessage()]
    assert msgs and all("\n" not in m for m in msgs)
    assert "v9\\nINFO forged record" in msgs[0]

    torch.save({"arch_version": "v2", "emb_dim": EMB, "state_dict": {},
                "embedding_source": "qwen3\r\nINFO forged"}, str(path))
    caplog.clear()
    with caplog.at_level("WARNING", logger=rm.logger.name):
        RLModel.load_or_create(state_path=path, emb_dim=EMB, embedding_source="arctic")
    msgs = [r.getMessage() for r in caplog.records if "embedding_source mismatch" in r.getMessage()]
    assert msgs and all("\n" not in m and "\r" not in m for m in msgs)


def test_rl_model_reads_checkpoints_only_through_load_checkpoint(monkeypatch, tmp_path):
    """Every read in the module routes through the one helper: replacing it
    with a refusing stub makes every loader treat the file as unreadable."""
    state = tmp_path / "state" / "rl_model_x.pt"
    m = RLModel.load_or_create(state_path=state, emb_dim=EMB, weights_version="0.2.106")
    m.get_type_idx("custom_type_c")
    m.save()
    calls: list[str] = []

    def _refuse(path):
        calls.append(Path(path).name)
        raise RuntimeError("refused")

    monkeypatch.setattr(rm, "load_checkpoint", _refuse)
    back = RLModel.load_or_create(state_path=state, emb_dim=EMB)
    assert back.weights_version == ""
    assert read_checkpoint_meta(state) is None
    assert "rl_model_x.pt" in calls
