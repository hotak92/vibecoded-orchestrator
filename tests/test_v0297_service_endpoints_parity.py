"""v0.2.97 (lane W): ONE rule for where the core services are reached.

The hub's ``/config`` (Rust, ``vct-launcher-core/src/services/service_endpoints.rs``)
and every project's env projection must name the same Weaviate. Before this
change the hub served ``LocalConfig`` (env → ``vct-config.toml`` → 8081) while
the projection wrote ``http://localhost:8081`` unless a caller pinned a port —
so an adopted external Weaviate, a port override or a ``vct-config.toml`` URL
reached one surface and not the other.

* ``tests/fixtures/service_endpoint_parity.json`` is executed HERE against
  ``vco_lib.service_endpoints`` and in Rust against ``service_endpoints.rs``;
  a divergence between the two implementations fails one side.
* ``project_env_from_db`` (what ``install.py`` and the launcher's apply both
  run) resolves an unpinned URL through that rule — including when the only
  statement is ``VCT_WEAVIATE_URL`` or a ``vct-config.toml``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db, set_app_state
from vco_lib import service_endpoints as se
from vco_lib.config_projection import project_env_from_db
from vco_lib.service_adoption import write_services_toml

REPO = Path(__file__).resolve().parent.parent
TABLE = json.loads((REPO / "tests" / "fixtures" / "service_endpoint_parity.json").read_text())


def _adoption(row):
    if row is None:
        return None
    return {k: v for k, v in row.items() if v is not None}


def test_constants_match_the_table() -> None:
    c = TABLE["constants"]
    assert c["statement_env"] == se.STATEMENT_ENV
    assert c["ollama_statement_env"] == se.OLLAMA_STATEMENT_ENV
    assert c["config_file"] == se.CONFIG_FILE
    assert c["config_key"] == se.CONFIG_KEY
    assert {k: (v["app_state_key"], v["default_port"]) for k, v in c["services"].items()} == se.SERVICES


@pytest.mark.parametrize("case", TABLE["weaviate_url_cases"], ids=lambda c: c["name"])
def test_weaviate_url_case(case) -> None:
    url = se.resolve_weaviate_url(case["statement"], case["port_override"], _adoption(case["adoption"]))
    assert url == case["expect_url"]
    assert se.weaviate_port_for_url(url) == case["expect_port"]


@pytest.mark.parametrize("case", TABLE["ollama_url_cases"], ids=lambda c: c["name"])
def test_ollama_url_case(case) -> None:
    url = se.resolve_ollama_url(case["statement"], case["port_override"], _adoption(case["adoption"]))
    assert url == case["expect_url"]
    assert (se.port_of_url(url) if se.port_of_url(url) is not None
            else se.SERVICES["ollama"][1]) == case["expect_port"]


@pytest.mark.parametrize("case", TABLE["port_cases"], ids=lambda c: c["name"])
def test_port_case(case) -> None:
    assert se.resolve_port(case["service"], case["port_override"], _adoption(case["adoption"])) == case["expect_port"]


# ─── the projection resolves through the rule ───────────────────────────


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An isolated machine: its own state dir, launcher.db and orchestrator
    root, and NO machine statement unless a test sets one. ``WEAVIATE_URL``
    stays at conftest's unroutable sentinel — the rule must not read it."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    monkeypatch.delenv(se.STATEMENT_ENV, raising=False)
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = tmp_path / "acme"
    folder.mkdir()
    db = tmp_path / "launcher.db"
    make_launcher_db(db, projects=[{
        "project_id": "p-1", "name": "Acme", "folder_path": str(folder), "slug": "acme",
    }])
    root = tmp_path / "orch"
    (root / "launcher" / "dist").mkdir(parents=True)
    return db, root


def _env(db: Path, root: Path, **kw) -> dict:
    return project_env_from_db("p-1", db_path=db, orchestrator_root=root, **kw)["canonical_env"]


def test_unpinned_projection_is_the_canonical_default(machine) -> None:
    db, root = machine
    env = _env(db, root)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://localhost:8081", "8081")
    assert (env["OLLAMA_URL"], env["OLLAMA_PORT"]) == ("http://localhost:11435", "11435")
    assert (env["CODE_EMBED_URL"], env["CODE_EMBED_PORT"]) == ("http://localhost:11440", "11440")


def test_adopted_external_weaviate_reaches_the_projection(machine) -> None:
    db, root = machine
    write_services_toml({"services": [
        {"name": "weaviate", "mode": "adopt", "external_url": "http://weaviate.lan:8090/v1/meta"},
        {"name": "ollama", "mode": "parallel", "parallel_port": 11436},
    ]})
    env = _env(db, root)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://weaviate.lan:8090", "8090")
    assert (env["OLLAMA_URL"], env["OLLAMA_PORT"]) == ("http://localhost:11436", "11436")


def test_port_overrides_reach_the_projection(machine) -> None:
    db, root = machine
    set_app_state(db, "weaviate.port_override", "18081")
    set_app_state(db, "code_embed.port_override", "21440")
    env = _env(db, root)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://localhost:18081", "18081")
    assert (env["CODE_EMBED_URL"], env["CODE_EMBED_PORT"]) == ("http://localhost:21440", "21440")


def test_the_machine_statement_wins_and_the_transport_is_not_read(machine, monkeypatch) -> None:
    db, root = machine
    set_app_state(db, "weaviate.port_override", "18081")
    monkeypatch.setenv("WEAVIATE_URL", "http://stale-projection:1")
    monkeypatch.setenv(se.STATEMENT_ENV, "http://vm.lan:9000/")
    env = _env(db, root)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://vm.lan:9000", "9000")


def test_vct_config_toml_beside_the_dist_binary_is_a_statement(machine) -> None:
    db, root = machine
    (root / "launcher" / "dist" / se.CONFIG_FILE).write_text('weaviate_url = "http://from-file:7000"\n')
    env = _env(db, root)
    assert env["WEAVIATE_URL"] == "http://from-file:7000"


def test_a_pinned_port_is_only_the_last_leg(machine) -> None:
    """``--weaviate-port`` (the launcher passes the port it resolved) is the
    port assumed when nothing on the machine names another; it never
    overrides an adoption or an override."""
    db, root = machine
    assert _env(db, root, weaviate_port_default=18081)["WEAVIATE_URL"] == "http://localhost:18081"
    write_services_toml({"services": [
        {"name": "weaviate", "mode": "adopt", "external_url": "http://weaviate.lan:8090"},
    ]})
    env = _env(db, root, weaviate_port_default=8090, ollama_port_default=21435)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://weaviate.lan:8090", "8090")
    assert env["OLLAMA_PORT"] == "21435"


def test_a_pinned_url_is_used_as_given(machine) -> None:
    db, root = machine
    set_app_state(db, "weaviate.port_override", "18081")
    env = _env(db, root, weaviate_url_override="http://pinned:1234")
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://pinned:1234", "1234")
