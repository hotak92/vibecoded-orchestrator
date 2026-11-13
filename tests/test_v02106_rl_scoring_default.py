# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL scoring's default follows the bundled weights (owner ruling 2026-11-12).

The decision lives once, in ``vco_lib/rl_scoring_default.py``; the Rust side
(``vct-launcher-core/src/rl_scoring_default.rs``) calls its CLI, whose JSON
line is pinned here. Both ways: a manifest naming a ``current`` for the active
profile → default on; none → off; an explicit choice beats the default in both
directions; a broken manifest or an unknown profile → off.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env

from vco_lib import rl_scoring_default as rsd

REPO = Path(__file__).resolve().parents[1]
SHA = "a" * 64


def _manifest(root: Path, profiles: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "profiles": profiles}), encoding="utf-8"
    )
    return root


@pytest.fixture
def bundled_qwen3(tmp_path: Path) -> Path:
    return _manifest(
        tmp_path / "w",
        {
            "qwen3": {"current": "0.3.0", "versions": {"0.3.0": {"sha256": SHA}}},
            "arctic": {"current": None, "versions": {}},
        },
    )


def test_a_bundled_model_for_the_active_profile_defaults_on(bundled_qwen3: Path) -> None:
    assert rsd.rl_scoring_default("qwen3", bundled_qwen3) is True
    got = rsd.bundled_model_ships("qwen3", bundled_qwen3)
    assert got.bundled_model and got.default_on and got.error is None


def test_no_model_for_the_active_profile_defaults_off(bundled_qwen3: Path) -> None:
    assert rsd.rl_scoring_default("arctic", bundled_qwen3) is False


def test_explicit_off_beats_a_bundled_model(bundled_qwen3: Path) -> None:
    assert rsd.rl_scoring_enabled(False, "qwen3", bundled_qwen3) is False
    assert rsd.rl_scoring_enabled(None, "qwen3", bundled_qwen3) is True


def test_explicit_on_without_weights_is_on(bundled_qwen3: Path) -> None:
    assert rsd.rl_scoring_enabled(True, "arctic", bundled_qwen3) is True
    assert rsd.rl_scoring_enabled(None, "arctic", bundled_qwen3) is False


def test_unknown_profile_is_off(bundled_qwen3: Path) -> None:
    got = rsd.bundled_model_ships("openai", bundled_qwen3)
    assert not got.default_on and got.error is None


def test_unreadable_or_inconsistent_manifest_is_off_with_the_reason(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    got = rsd.bundled_model_ships("qwen3", broken)
    assert not got.default_on and got.error and "unreadable" in got.error

    # `current` naming a version the manifest does not list.
    bad = _manifest(tmp_path / "bad", {"qwen3": {"current": "9.9.9", "versions": {}}})
    got = rsd.bundled_model_ships("qwen3", bad)
    assert not got.default_on and got.error

    missing = tmp_path / "missing"
    got = rsd.bundled_model_ships("qwen3", missing)
    assert not got.default_on and got.error and "no bundled-weights manifest" in got.error


def test_the_shipped_manifest_defaults_off_for_every_profile_today() -> None:
    assert rsd.WEIGHTS_ROOT == REPO / "claude_mcp_servers" / "rl_reranker" / "weights"
    for profile in ("qwen3", "arctic"):
        got = rsd.bundled_model_ships(profile)
        assert got.error is None, got
        assert got.default_on is False, f"{profile}: no trained model ships yet"


def test_rl_doctor_reports_the_default_and_its_cause(
    monkeypatch: pytest.MonkeyPatch, bundled_qwen3: Path
) -> None:
    from claude_mcp_servers.scripts import rl_doctor

    monkeypatch.setattr(rsd, "WEIGHTS_ROOT", bundled_qwen3)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    got = rl_doctor._probe_scoring_default()
    assert got["status"] == "on" and got["default_on"] is True
    assert "trained model bundled for qwen3" in got["detail"]
    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    got = rl_doctor._probe_scoring_default()
    assert got["status"] == "off" and "no trained model bundled yet for arctic" in got["detail"]


def test_cli_json_line_is_the_rust_wire_shape() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "vco_lib.rl_scoring_default", "--profile", "qwen3", "--json"],
        cwd=REPO,
        env=child_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    # The fields `RlScoringDefault` (Rust) deserializes.
    assert payload == {
        "profile": "qwen3",
        "bundled_model": False,
        "default_on": False,
        "error": None,
    }
