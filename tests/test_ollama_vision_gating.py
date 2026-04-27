# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Tests for memory-aware gating in claude_mcp_servers.ollama_mcp.server.

Covers:
  - _detect_vision_capability returns sane data on the host machine
  - threshold-comparison: 5.5 / 4.5 GB VRAM → fallback or skip,
                          16 GB RAM + no GPU → CPU OK,
                          4 GB RAM + no GPU  → skip-with-reason
  - _resize_budget_pixels tier behaviour
  - read_image() doesn't crash on insufficient VRAM (no Ollama call made)
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# Make the package importable from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_mcp_servers.ollama_mcp import server  # noqa: E402


# --- _detect_vision_capability host probe -----------------------------------------

def test_detect_vision_capability_returns_sane_data():
    """Probe must return a dict with all expected keys and reasonable types."""
    cap = server._detect_vision_capability()
    assert isinstance(cap, dict)
    assert set(cap.keys()) >= {
        "has_gpu", "vram_gb", "ram_gb", "preferred_backend",
        "smallest_vram_required_gb", "smallest_ram_required_gb",
    }
    assert isinstance(cap["has_gpu"], bool)
    assert cap["vram_gb"] is None or cap["vram_gb"] >= 0
    assert cap["ram_gb"] >= 1.0  # any modern machine has >1 GB
    assert cap["preferred_backend"] in {"gpu", "cpu", "none"}
    assert cap["smallest_vram_required_gb"] > 0
    assert cap["smallest_ram_required_gb"] > 0


def test_detect_vision_capability_never_raises_on_failure(monkeypatch):
    """If both probe paths blow up, the function still returns a safe default."""
    monkeypatch.setattr(server, "_detect_total_ram_gb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(server, "_detect_max_vram_gb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    cap = server._detect_vision_capability()
    # Each inner call is wrapped in its own try/except → ram falls back to 8.0,
    # vram falls back to None.
    assert cap["ram_gb"] == 8.0
    assert cap["vram_gb"] is None
    assert cap["has_gpu"] is False
    # 8 GB RAM < 12 GB threshold for qwen3.5:9b → preferred_backend is "none"
    assert cap["preferred_backend"] == "none"


# --- _model_fits / _select_vision_model thresholds --------------------------------

def _cap(has_gpu: bool, vram_gb, ram_gb: float, backend: str = None):
    if backend is None:
        if has_gpu and vram_gb is not None and vram_gb >= 7.5:
            backend = "gpu"
        elif ram_gb >= 12.0:
            backend = "cpu"
        else:
            backend = "none"
    return {
        "has_gpu": has_gpu,
        "vram_gb": vram_gb,
        "ram_gb": ram_gb,
        "preferred_backend": backend,
        "smallest_vram_required_gb": 7.5,
        "smallest_ram_required_gb": 12.0,
    }


def test_qwen35_9b_fits_with_8gb_vram():
    # Practical threshold for qwen3.5:9b is 7.5 GB → 8 GB GPU passes.
    assert server._model_fits("qwen3.5:9b", _cap(True, 8.0, 16.0)) is True


def test_qwen35_9b_does_not_fit_55gb_vram_low_ram():
    # 5.5 GB VRAM is below 7.5 GB threshold → GPU path fails.
    # 8 GB RAM is below 12 GB threshold → CPU path also fails.
    assert server._model_fits("qwen3.5:9b", _cap(True, 5.5, 8.0)) is False


def test_qwen35_7b_fits_with_6gb_vram():
    # qwen3.5:7b threshold is 6.0 GB.
    assert server._model_fits("qwen3.5:7b", _cap(True, 6.0, 12.0)) is True


def test_no_gpu_high_ram_falls_back_to_cpu():
    # 0 GB GPU + 16 GB RAM → CPU OK for qwen3.5:9b (12 GB threshold).
    assert server._model_fits("qwen3.5:9b", _cap(False, None, 16.0)) is True


def test_no_gpu_low_ram_does_not_fit():
    # 0 GB GPU + 4 GB RAM → can't run anything.
    assert server._model_fits("qwen3.5:9b", _cap(False, None, 4.0)) is False
    assert server._model_fits("qwen3.5:4b", _cap(False, None, 4.0)) is False


def test_select_swaps_to_smaller_installed_model(monkeypatch):
    """If requested doesn't fit but a smaller installed VLM does, swap."""
    monkeypatch.setattr(
        server, "_list_installed_ollama_models",
        lambda: ["qwen3.5:9b", "qwen3.5:4b", "some-other:13b"],
    )
    # 4.5 GB VRAM: too small for 9b (7.5 GB) and 7b (6 GB), but qwen3.5:4b (4 GB) fits.
    cap = _cap(True, 4.5, 8.0, backend="gpu")
    chosen, reason = server._select_vision_model("qwen3.5:9b", cap)
    assert chosen == "qwen3.5:4b"
    assert reason is not None and "auto-fallback" in reason


def test_select_returns_none_when_nothing_fits(monkeypatch):
    monkeypatch.setattr(server, "_list_installed_ollama_models", lambda: [])
    cap = _cap(False, None, 4.0)
    chosen, reason = server._select_vision_model("qwen3.5:9b", cap)
    assert chosen is None
    assert "insufficient memory" in reason


def test_select_keeps_requested_when_it_fits():
    cap = _cap(True, 16.0, 64.0)
    chosen, reason = server._select_vision_model("qwen3.5:9b", cap)
    assert chosen == "qwen3.5:9b"
    assert reason is None


# --- _resize_budget_pixels tiers --------------------------------------------------

def test_resize_budget_high_vram_full_size():
    cap = _cap(True, 16.0, 64.0, backend="gpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:9b") == 1_048_576


def test_resize_budget_8gb_vram_720():
    cap = _cap(True, 8.0, 16.0, backend="gpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:9b") == 524_288


def test_resize_budget_6gb_vram_512():
    cap = _cap(True, 6.0, 16.0, backend="gpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:7b") == 262_144


def test_resize_budget_low_vram_256():
    cap = _cap(True, 4.0, 8.0, backend="gpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:4b") == 65_536


def test_resize_budget_cpu_high_ram_full_size():
    cap = _cap(False, None, 32.0, backend="cpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:4b") == 1_048_576


def test_resize_budget_cpu_low_ram_256():
    cap = _cap(False, None, 4.0, backend="cpu")
    assert server._resize_budget_pixels(cap, "qwen3.5:4b") == 65_536


# --- read_image: gating + no-crash on insufficient memory -------------------------

@pytest.fixture
def tiny_png(tmp_path):
    """Minimal valid PNG (1x1 transparent pixel)."""
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000005000100020a2db40000000049454e44ae426082"
    )
    p = tmp_path / "tiny.png"
    p.write_bytes(png_bytes)
    return p


def test_read_image_no_describe_does_not_call_ollama(tiny_png, monkeypatch):
    """Without describe=True, Ollama must never be hit."""
    called = []
    def _fake_post(*a, **kw):
        called.append((a, kw))
        raise AssertionError("read_image must not POST when describe=False")
    monkeypatch.setattr(server.requests, "post", _fake_post)

    out = json.loads(server.read_image(str(tiny_png), describe=False))
    assert out["success"] is True
    assert "base64_data_url" in out
    assert "description" not in out
    assert called == []


def test_read_image_skips_description_when_no_memory(tiny_png, monkeypatch):
    """describe=True + no GPU + low RAM → description: null + reason; no Ollama call."""
    monkeypatch.setattr(
        server, "_VISION_CAPABILITY",
        _cap(False, None, 4.0, backend="none"),
    )
    monkeypatch.setattr(server, "_list_installed_ollama_models", lambda: [])

    called = []
    def _fake_post(*a, **kw):
        called.append((a, kw))
        raise AssertionError("Description tier must be skipped on low memory")
    monkeypatch.setattr(server.requests, "post", _fake_post)

    out = json.loads(server.read_image(str(tiny_png), describe=True))
    assert out["success"] is True
    assert out["description"] is None
    assert "description_skipped_reason" in out
    assert "insufficient memory" in out["description_skipped_reason"]
    assert out["vision_model_used"] is None
    assert called == []


def test_read_image_clamps_pixel_budget_on_low_memory(tiny_png, monkeypatch):
    """User-supplied max_total_pixels is clamped down on tight hardware."""
    monkeypatch.setattr(
        server, "_VISION_CAPABILITY",
        _cap(True, 4.0, 8.0, backend="gpu"),  # → 65_536 px budget
    )
    out = json.loads(server.read_image(
        str(tiny_png),
        max_total_pixels=1_048_576,
        describe=False,
    ))
    assert out["image_budget_pixels"] == 65_536
    assert out["image_budget_clamped_from"] == 1_048_576


def test_read_image_does_not_clamp_when_user_value_is_smaller(tiny_png, monkeypatch):
    """If user explicitly asks for a small budget, don't expand it."""
    monkeypatch.setattr(
        server, "_VISION_CAPABILITY",
        _cap(True, 16.0, 64.0, backend="gpu"),  # → 1_048_576 budget
    )
    out = json.loads(server.read_image(
        str(tiny_png),
        max_total_pixels=10_000,
        describe=False,
    ))
    assert out["image_budget_pixels"] == 10_000
    assert "image_budget_clamped_from" not in out


def test_read_image_env_var_override(tiny_png, monkeypatch):
    """OLLAMA_VISION_MODEL env var changes the default vision model used."""
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "qwen3.5:4b")
    monkeypatch.setattr(
        server, "_VISION_CAPABILITY",
        _cap(True, 16.0, 64.0, backend="gpu"),
    )
    captured = {}

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"message": {"content": "a tiny image"}}

    def _fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["model"] = json["model"]
        return _Resp()

    monkeypatch.setattr(server.requests, "post", _fake_post)

    out = json.loads(server.read_image(str(tiny_png), describe=True))
    assert captured.get("model") == "qwen3.5:4b"
    assert out["description"] == "a tiny image"
    assert out["vision_model_used"] == "qwen3.5:4b"
