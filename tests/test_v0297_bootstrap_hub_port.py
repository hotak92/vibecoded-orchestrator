# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The hub endpoints VCO REPORTS name the hub's real port (v0.2.97, lane T).

``install.py --bootstrap --json`` (``vct_hub_endpoints``) and
``vco_lib.secrets_bootstrap`` (``secrets.hub_env_endpoint``, part of the same
envelope) spelled ``127.0.0.1:7700`` as a literal. The hub's port is
configurable (``VCT_HUB_PORT``, the ``vct-hub-api`` setting) and the hub walks
past a taken port, so an agent reading the envelope on such a machine was sent
to the wrong port. Both now go through the ONE Python port reader,
``vco_lib.hub_ensure.resolve_hub_port`` (``$VCT_HUB_PORT`` → ``hub.port`` →
7700), the same one ``vco_lib.project_config._discover_hub`` uses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import hub_ensure

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap(tmp_path: Path, **env: str) -> dict:
    state = tmp_path / "vct-state"
    state.mkdir(exist_ok=True)
    cp = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install.py"), "--bootstrap", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=child_env(
            VCT_BOOTSTRAP_TEST_MODE="1",
            VCT_STATE_DIR=str(state),
            VCT_HUB_PORT=env.get("VCT_HUB_PORT", ""),
        ),
    )
    assert cp.returncode == 0, cp.stderr[-800:]
    return json.loads(cp.stdout)


def _assert_port(envelope: dict, port: int) -> None:
    hub = envelope["vct_hub_endpoints"]
    assert hub["base"] == f"http://127.0.0.1:{port}"
    assert hub["health"] == f"http://127.0.0.1:{port}/api/v1/health"
    assert envelope["secrets"]["hub_env_endpoint"] == (
        f"http://127.0.0.1:{port}/api/v1/projects/{{id}}/env"
    )


def test_bootstrap_reports_the_hub_port_file(tmp_path: Path) -> None:
    """No env pin: the port the running hub wrote to ``hub.port`` is reported."""
    (tmp_path / "vct-state").mkdir()
    (tmp_path / "vct-state" / "hub.port").write_text("7811\n", encoding="utf-8")
    _assert_port(_bootstrap(tmp_path), 7811)


def test_bootstrap_reports_the_env_pin(tmp_path: Path) -> None:
    _assert_port(_bootstrap(tmp_path, VCT_HUB_PORT="7822"), 7822)


def test_bootstrap_reports_the_default_with_nothing_set(tmp_path: Path) -> None:
    _assert_port(_bootstrap(tmp_path), 7700)


def test_resolve_hub_port_order_and_corrupt_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[str] = []

    def warn(kind: str, _detail: str) -> None:
        warned.append(kind)

    monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    assert hub_ensure.resolve_hub_port(tmp_path, warn) == 7700
    (tmp_path / "hub.port").write_text("7833", encoding="utf-8")
    assert hub_ensure.resolve_hub_port(tmp_path, warn) == 7833
    monkeypatch.setenv("VCT_HUB_PORT", "7844")
    assert hub_ensure.resolve_hub_port(tmp_path, warn) == 7844, "env wins (client pin)"
    monkeypatch.setenv("VCT_HUB_PORT", "junk")
    assert hub_ensure.resolve_hub_port(tmp_path, warn) == 7833, "an invalid pin falls through to the file"
    monkeypatch.delenv("VCT_HUB_PORT")
    (tmp_path / "hub.port").write_text("junk", encoding="utf-8")
    assert hub_ensure.resolve_hub_port(tmp_path, warn) == 7700
    assert warned == ["hub_port_invalid", "hub_port_invalid"]
