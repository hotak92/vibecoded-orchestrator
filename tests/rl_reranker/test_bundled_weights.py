# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundled-weights layout: manifest resolution per profile, and what the
server reports with and without a shipped checkpoint.

Three contracts are pinned here:

1. The SHIPPED manifest (``claude_mcp_servers/rl_reranker/weights``) is valid
   and every version it lists is present with a matching sha256 — the same
   check the image build runs. When the training lane commits weights this
   test is what keeps the manifest and the files in step.
2. ``resolve_bundled_weights``: absent root / manifest / profile / ``current``
   = None ("not trained"); a present but inconsistent manifest raises.
3. ``RLServer``: with no checkpoint shipped for the active profile it reports
   ``trained: false`` / ``model: "not trained"``; with one it seeds ``state/``
   (never overwriting), loads it and reports the shipped version. ``/reset``
   ``strategy: "bundled"`` re-seeds a project from it, 404 when none ships.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from rl_reranker import bundled_weights as bw  # noqa: E402
from rl_reranker.rl_model import RLModel  # noqa: E402
from rl_reranker.rl_server import RLServer  # noqa: E402

SHIPPED_ROOT = (
    Path(__file__).resolve().parents[2] / "claude_mcp_servers" / "rl_reranker" / "weights"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(root: Path, profiles: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / bw.MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "profiles": profiles}), encoding="utf-8"
    )


def _ship_checkpoint(root: Path, profile: str, version: str, emb_dim: int = 1024) -> Path:
    """Write a real RLModel checkpoint at the layout path and list it as current."""
    rel = bw.weights_relpath(profile, version)
    path = root / Path(*rel.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    model = RLModel.load_or_create(
        state_path=path, emb_dim=emb_dim, weights_version=version,
        embedding_source=profile,
    )
    model.save()
    profiles = {
        "qwen3": {"current": None, "versions": {}},
        "arctic": {"current": None, "versions": {}},
    }
    profiles[profile] = {"current": version, "versions": {version: {"sha256": _sha(path)}}}
    _write_manifest(root, profiles)
    return path


# ─── 1. the shipped manifest ─────────────────────────────────────────────


def test_shipped_manifest_verifies_and_declares_both_profiles():
    current = bw.verify(SHIPPED_ROOT)
    assert set(bw.SHIPPED_PROFILES) <= set(current), current


def test_shipped_manifest_resolves_for_each_profile_without_error():
    for profile in bw.SHIPPED_PROFILES:
        got = bw.resolve_bundled_weights(SHIPPED_ROOT, profile)
        expected = bw.load_manifest(SHIPPED_ROOT)["profiles"][profile]["current"]
        assert (got.version if got else None) == expected


# ─── 2. resolution rules ────────────────────────────────────────────────


def test_no_root_or_no_manifest_is_not_trained(tmp_path):
    assert bw.resolve_bundled_weights(None, "qwen3") is None
    assert bw.resolve_bundled_weights(tmp_path / "missing", "qwen3") is None
    assert bw.resolve_bundled_weights(tmp_path, "qwen3") is None  # dir, no manifest


def test_unlisted_profile_or_null_current_is_not_trained(tmp_path):
    _write_manifest(tmp_path, {"qwen3": {"current": None, "versions": {}}})
    assert bw.resolve_bundled_weights(tmp_path, "qwen3") is None
    assert bw.resolve_bundled_weights(tmp_path, "arctic") is None


def test_listed_current_resolves_per_profile(tmp_path):
    path = _ship_checkpoint(tmp_path, "arctic", "0.3.0")
    got = bw.resolve_bundled_weights(tmp_path, "arctic")
    assert got is not None
    assert (got.profile, got.version, got.path) == ("arctic", "0.3.0", path)
    # The OTHER profile stays not trained: weights never cross profiles.
    assert bw.resolve_bundled_weights(tmp_path, "qwen3") is None


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.__setitem__("schema_version", 2), "schema_version"),
        (lambda d: d["profiles"]["arctic"].__setitem__("current", "9.9.9"), "not one of"),
        (lambda d: d["profiles"]["arctic"]["versions"]["0.3.0"].__setitem__("sha256", "0" * 64),
         "does not match"),
        (lambda d: d["profiles"]["arctic"]["versions"].__setitem__("../../x", {"sha256": "0" * 64}),
         "invalid weights version"),
        (lambda d: d["profiles"].__setitem__("Bad/Name", {"current": None}), "invalid profile"),
    ],
)
def test_present_but_inconsistent_manifest_raises(tmp_path, mutate, needle):
    _ship_checkpoint(tmp_path, "arctic", "0.3.0")
    doc = json.loads((tmp_path / bw.MANIFEST_NAME).read_text())
    mutate(doc)
    (tmp_path / bw.MANIFEST_NAME).write_text(json.dumps(doc))
    with pytest.raises(bw.BundledWeightsError, match=needle.replace("(", r"\(")):
        bw.resolve_bundled_weights(tmp_path, "arctic")


def test_listed_but_missing_file_raises(tmp_path):
    path = _ship_checkpoint(tmp_path, "qwen3", "0.3.0")
    path.unlink()
    with pytest.raises(bw.BundledWeightsError, match="missing on disk"):
        bw.resolve_bundled_weights(tmp_path, "qwen3")


def test_unparseable_manifest_raises(tmp_path):
    (tmp_path / bw.MANIFEST_NAME).write_text("{not json")
    with pytest.raises(bw.BundledWeightsError, match="unreadable"):
        bw.resolve_bundled_weights(tmp_path, "qwen3")


def test_verify_cli_exit_codes(tmp_path, capsys):
    assert bw.main(["verify", str(SHIPPED_ROOT)]) == 0
    (tmp_path / bw.MANIFEST_NAME).write_text("{not json")
    assert bw.main(["verify", str(tmp_path)]) == 1
    assert "unreadable" in capsys.readouterr().err


# ─── 3. the server ───────────────────────────────────────────────────────


def _server(tmp_path, monkeypatch, profile: str, weights_root):
    monkeypatch.setenv("ACTIVE_EMBEDDING", profile)
    monkeypatch.delenv("RL_MODEL_TAG", raising=False)
    monkeypatch.delenv("RL_EMB_DIM", raising=False)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    return RLServer(
        project_root=tmp_path / "project",
        ollama_url="http://localhost:11435",
        log_path=str(tmp_path / "logs" / "rl_events.jsonl"),
        port=0,
        weights_root=weights_root,
    )


async def _request(srv, method: str, path: str, body=None):
    server = TestServer(srv._app)
    await server.start_server()
    client = TestClient(server)
    try:
        resp = await client.request(method, path, json=body)
        return resp.status, await resp.json()
    finally:
        await client.close()
        await server.close()


@pytest.mark.parametrize("weights_root", ["none", "shipped"])
def test_server_without_shipped_weights_reports_not_trained(tmp_path, monkeypatch, weights_root):
    root = None if weights_root == "none" else SHIPPED_ROOT
    if root is not None and bw.resolve_bundled_weights(root, "qwen3") is not None:
        pytest.skip("qwen3 weights now ship; the shipped-root case no longer applies")
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    status, health = asyncio.run(_request(srv, "GET", "/health"))
    assert status == 200
    assert health["ok"] is True
    assert health["trained"] is False
    assert health["model"] == "not trained"
    assert health["weights"] == {
        "profile": "qwen3", "loaded_version": None, "bundled_version": None,
    }


def test_server_with_shipped_weights_seeds_and_reports_trained(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship_checkpoint(root, "qwen3", "0.3.0")
    srv = _server(tmp_path, monkeypatch, "qwen3", root)
    seeded = tmp_path / "project" / "state" / "rl_model_qwen3_0.3.0.pt"
    assert seeded.is_file()
    status, health = asyncio.run(_request(srv, "GET", "/health"))
    assert status == 200
    assert health["trained"] is True
    assert health["model"] == "RLModel loaded"
    assert health["weights"] == {
        "profile": "qwen3", "loaded_version": "0.3.0", "bundled_version": "0.3.0",
    }


def test_seed_never_overwrites_an_existing_state_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship_checkpoint(root, "qwen3", "0.3.0")
    state = tmp_path / "project" / "state"
    state.mkdir(parents=True)
    existing = state / "rl_model_qwen3_0.3.0.pt"
    existing.write_bytes(b"user-owned bytes")
    # The pre-existing file is not a loadable checkpoint; RLModel falls back
    # to fresh weights for it, and the seed must not have replaced it.
    _server(tmp_path, monkeypatch, "qwen3", root)
    assert existing.read_bytes() == b"user-owned bytes"


def test_inconsistent_shipped_manifest_fails_server_start(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    path = _ship_checkpoint(root, "qwen3", "0.3.0")
    path.write_bytes(b"tampered")
    with pytest.raises(bw.BundledWeightsError):
        _server(tmp_path, monkeypatch, "qwen3", root)


def test_reset_bundled_404_when_nothing_ships(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch, "arctic", None)
    status, body = asyncio.run(_request(srv, "POST", "/reset", {"strategy": "bundled"}))
    assert status == 404
    assert "not trained" in body["error"]


def test_reset_bundled_reseeds_the_project_head(tmp_path, monkeypatch):
    root = tmp_path / "weights"
    _ship_checkpoint(root, "arctic", "0.3.0")
    srv = _server(tmp_path, monkeypatch, "arctic", root)
    status, body = asyncio.run(_request(srv, "POST", "/reset", {"strategy": "bundled"}))
    assert status == 200, body
    assert body["strategy"] == "bundled"
    assert body["job_id"] is None
    assert Path(body["weights_path"]).name == "rl_model_arctic_0.3.0.pt"
    assert srv._rl._rl_model.weights_version == "0.3.0"


def test_reset_rejects_unknown_strategy_naming_bundled(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch, "arctic", None)
    status, body = asyncio.run(_request(srv, "POST", "/reset", {"strategy": "nope"}))
    assert status == 400
    assert "'bundled'" in body["error"]
