# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 (service endpoints SSOT): the launcher DB's ``service_endpoints``
rows are the ONE answer to "where is Weaviate / Ollama / code-embed?".

* ``tests/fixtures/service_endpoint_parity.json`` is executed HERE against
  ``vco_lib.service_endpoints`` and in Rust against ``service_endpoints.rs``:
  the render, the absent-row default, and the ignored-input cases — every
  retired input (env vars, app_state port overrides, ``services.toml``,
  ``vct-config.toml``) put in place for real, the row still answering.
* The Python writer enforces migration 047's CHECKs: the tests load the real
  ``047_service_endpoints.sql`` and require ``validate_row`` and the schema to
  agree on every case (risk R1: writer / schema drift).
* ``apply_change`` runs its follow-up chain through injectable seams.
* The projection, ``_apply_standalone_env`` and the conftest-seeded DB resolve
  through the rows.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db, set_app_state
from vco_lib import service_endpoints as se
from vco_lib.config_projection import project_env_from_db
from vco_lib.service_adoption import write_services_toml

REPO = Path(__file__).resolve().parent.parent
TABLE = json.loads((REPO / "tests" / "fixtures" / "service_endpoint_parity.json").read_text())
MIGRATION_047 = (
    REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db" / "migrations"
    / "047_service_endpoints.sql"
)


def _row(service: str, spec: dict, **extra) -> se.EndpointRow:
    """A table row object → an :class:`se.EndpointRow` (DDL defaults)."""
    fields = {k: v for k, v in spec.items() if k in {f.name for f in dataclasses.fields(se.EndpointRow)}}
    fields.setdefault("source", "install_probe")
    fields.update(extra)
    return se.EndpointRow(service=service, **fields)


def _write(db: Path, *rows: se.EndpointRow) -> se.WriteResult:
    return se.write_rows(rows, db_path=db)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_launcher_db(tmp_path / "launcher.db")


# ─── the table ──────────────────────────────────────────────────────────


def test_constants_match_the_table() -> None:
    c = TABLE["constants"]
    assert c["default_ports"] == se.DEFAULT_PORTS
    assert c["weaviate_grpc_default"] == se.DEFAULT_WEAVIATE_GRPC_PORT
    assert (c["default_scheme"], c["default_host"]) == (se.DEFAULT_SCHEME, se.DEFAULT_HOST)
    assert tuple(c["modes"]) == se.MODES
    assert tuple(c["retired_inputs"]["env"]) == se.RETIRED_ENV_INPUTS
    assert c["retired_inputs"]["app_state_keys"] == list(se.RETIRED_APP_STATE_KEYS.values())


@pytest.mark.parametrize("case", TABLE["render_cases"], ids=lambda c: c["name"])
def test_render_case(case, db: Path) -> None:
    row = _row(case["service"], case["row"])
    assert se.render_url(case["service"], row) == case["expect_url"]
    assert se.render_port(case["service"], row) == case["expect_port"]
    if "expect_grpc_port" in case:
        assert se.render_grpc_port(row) == case["expect_grpc_port"]
    # The row is one the writer accepts and the schema stores; read back
    # through the machine resolver it renders the same.
    _write(db, row)
    conn = sqlite3.connect(db)
    try:
        assert se.machine_url(conn, case["service"]) == case["expect_url"]
        assert se.machine_port(conn, case["service"]) == case["expect_port"]
    finally:
        conn.close()


@pytest.mark.parametrize("case", TABLE["absent_row_cases"], ids=lambda c: c["name"])
def test_absent_row_case(case, db: Path) -> None:
    svc = case["service"]
    assert se.render_url(svc, None) == case["expect_url"]
    for conn in (None, sqlite3.connect(db)):
        assert se.machine_url(conn, svc) == case["expect_url"]
        assert se.machine_port(conn, svc) == case["expect_port"]
        if "expect_grpc_port" in case:
            assert se.machine_grpc_port(conn) == case["expect_grpc_port"]
    urls = se.machine_service_urls(db_path=db)
    assert urls[f"{svc}_url"] == case["expect_url"]
    assert urls[f"{svc}_port"] == case["expect_port"]


@pytest.mark.parametrize("case", TABLE["ignored_input_cases"], ids=lambda c: c["name"])
def test_ignored_input_case(case, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every retired input put in place where its old reader looked; the row
    (or, with no row, the default) still answers."""
    svc = case["service"]
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    for key, value in case["env"].items():
        monkeypatch.setenv(key, value)
    for key, value in case["app_state"].items():
        set_app_state(db, key, value)
    write_services_toml({"services": case["services_toml"]})
    if case["vct_config_toml"]:
        # Where the pre-v0.2.97 Python reader looked: beside $VCT_HUB_BIN.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "vct-config.toml").write_text(case["vct_config_toml"])
        monkeypatch.setenv("VCT_HUB_BIN", str(bindir / "vct-hub"))
    if case["row"] is not None:
        _write(db, _row(svc, case["row"]))

    named = {"weaviate": se.machine_weaviate_url, "ollama": se.machine_ollama_url,
             "code_embed": se.machine_code_embed_url}[svc]
    conn = sqlite3.connect(db)
    try:
        assert se.machine_url(conn, svc) == case["expect_url"]
        assert named(conn) == case["expect_url"]
        assert se.machine_port(conn, svc) == case["expect_port"]
        if "expect_grpc_port" in case:
            assert se.machine_grpc_port(conn) == case["expect_grpc_port"]
    finally:
        conn.close()
    urls = se.machine_service_urls(db_path=db)
    assert (urls[f"{svc}_url"], urls[f"{svc}_port"]) == (case["expect_url"], case["expect_port"])
    if "expect_grpc_port" in case:
        assert urls["weaviate_grpc_port"] == case["expect_grpc_port"]


def test_machine_weaviate_url_is_the_row_whatever_else_is_set(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red-proof (2), Python side: VCT_WEAVIATE_URL + WEAVIATE_URL + a
    services.toml ``parallel`` row + a ``port_override`` all set — the row."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_WEAVIATE_URL", "http://statement.invalid:1")
    monkeypatch.setenv("WEAVIATE_URL", "http://transport.invalid:2")
    write_services_toml({"services": [{"name": "weaviate", "mode": "parallel", "parallel_port": 18082}]})
    set_app_state(db, "weaviate.port_override", "18083")
    _write(db, se.EndpointRow(service="weaviate", mode="adopted_container", port=18090,
                              grpc_port=50060, container_name="their_weaviate", source="user_cli"))
    conn = sqlite3.connect(db)
    try:
        assert se.machine_weaviate_url(conn) == "http://localhost:18090"
        assert se.machine_grpc_port(conn) == 50060
    finally:
        conn.close()


# ─── writer ↔ schema (R1) ───────────────────────────────────────────────

_VALID = dict(service="weaviate", mode="vco_managed", port=8081, grpc_port=50052, source="install_probe")

#: (name, overrides, schema_refuses). ``schema_refuses`` False = a rule only
#: the writer enforces (the schema is looser there by design).
_CHECK_CASES = [
    ("unknown service", {"service": "qdrant"}, True),
    ("unknown mode", {"mode": "adopt"}, True),
    ("ftp scheme", {"scheme": "ftp"}, True),
    ("port 0", {"port": 0}, True),
    ("port 70000", {"port": 70000}, True),
    ("grpc 0", {"grpc_port": 0}, True),
    ("weaviate without grpc", {"grpc_port": None}, True),
    ("remote vco_managed", {"host": "gpu.lan"}, True),
    ("nameless adopted container", {"mode": "adopted_container"}, True),
    ("adopted code_embed", {"service": "code_embed", "grpc_port": None, "mode": "adopted_external"}, True),
    ("blank host", {"mode": "adopted_external", "host": ""}, False),
    ("host with a path", {"mode": "adopted_external", "host": "a/b"}, False),
    ("host with a port", {"mode": "adopted_external", "host": "a:1"}, False),
    ("unknown source", {"source": "guess"}, False),
    ("bare migrated: source", {"source": "migrated:"}, False),
    ("mount without kind", {"data_mount": {"source": "/x", "destination": "/cache"}}, False),
]


def _schema_accepts(row: se.EndpointRow) -> bool:
    conn = sqlite3.connect(":memory:")
    conn.executescript(MIGRATION_047.read_text())
    try:
        conn.execute(
            "INSERT INTO service_endpoints (service, mode, scheme, host, port, grpc_port, "
            "container_name, source, updated_at) VALUES (?,?,?,?,?,?,?,?,0)",
            (row.service, row.mode, row.scheme, row.host, row.port, row.grpc_port,
             row.container_name, row.source),
        )
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


@pytest.mark.parametrize("name,overrides,schema_refuses", _CHECK_CASES, ids=[c[0] for c in _CHECK_CASES])
def test_writer_refuses_what_the_schema_refuses(name, overrides, schema_refuses) -> None:
    row = se.EndpointRow(**{**_VALID, **overrides})
    with pytest.raises(se.InvalidEndpointRow):
        se.validate_row(row)
    assert _schema_accepts(row) is (not schema_refuses), name


@pytest.mark.parametrize("spec", [
    _VALID,
    {**_VALID, "mode": "adopted_container", "container_name": "their_weaviate"},
    {**_VALID, "mode": "adopted_external", "scheme": "https", "host": "[::1]", "port": 443},
    {**_VALID, "source": "migrated:services.toml"},
    {"service": "code_embed", "mode": "vco_managed", "port": 11440, "source": "install_probe",
     "data_mount": {"kind": "bind", "source": "/data/hf", "destination": "/cache"}},
], ids=["managed", "adopted-container", "ipv6-https", "migrated", "code-embed-bind"])
def test_writer_and_schema_accept_valid_rows(spec) -> None:
    row = se.EndpointRow(**spec)
    se.validate_row(row)
    assert _schema_accepts(row)


# ─── write_rows ─────────────────────────────────────────────────────────


def test_write_is_idempotent_and_distinguishes_propagating_changes(db: Path) -> None:
    row = se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="install_probe")
    first = se.write_rows([row], db_path=db, now_ms=1)
    assert (first.written, first.propagating) == (["ollama"], ["ollama"])
    assert se.write_rows([row], db_path=db, now_ms=2) == se.WriteResult()
    assert se.load_rows(db)["ollama"].updated_at == 1, "an unchanged row is not rewritten"
    verified = dataclasses.replace(row, verified_at=5)
    third = se.write_rows([verified], db_path=db, now_ms=3)
    assert (third.written, third.propagating) == (["ollama"], []), "verified_at reaches no surface"
    moved = dataclasses.replace(verified, port=11436)
    assert se.write_rows([moved], db_path=db, now_ms=4).propagating == ["ollama"]
    assert se.load_rows(db)["ollama"].port == 11436


def test_one_invalid_row_writes_nothing(db: Path) -> None:
    good = se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="install_probe")
    bad = se.EndpointRow(service="weaviate", mode="vco_managed", port=8081, source="install_probe")
    with pytest.raises(se.InvalidEndpointRow):
        se.write_rows([good, bad], db_path=db)
    assert se.load_rows(db) == {}


def test_data_mount_round_trips(db: Path) -> None:
    mount = {"kind": "volume", "source": "vco_code_embed_cache", "destination": "/cache"}
    se.write_rows([se.EndpointRow(service="code_embed", mode="vco_managed", port=11440,
                                  source="live_reconcile", data_mount=mount)], db_path=db)
    assert dict(se.load_rows(db)["code_embed"].data_mount) == mount


def test_writer_needs_the_migrated_table(tmp_path: Path) -> None:
    row = se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="install_probe")
    with pytest.raises(se.ServiceRegistryUnavailable):
        se.write_rows([row], db_path=tmp_path / "absent.db")
    old = tmp_path / "old.db"
    sqlite3.connect(old).execute("CREATE TABLE app_state (key TEXT)").connection.close()
    with pytest.raises(se.ServiceRegistryUnavailable):
        se.write_rows([row], db_path=old)
    assert se.load_rows(old) == {}, "a pre-047 DB reads as no rows"


# ─── apply_change (I5) through its seams ────────────────────────────────


def test_apply_change_runs_the_chain_in_order(db: Path, tmp_path: Path) -> None:
    se.write_rows([se.EndpointRow(service="ollama", mode="adopted_external", port=11434,
                                  source="install_probe")], db_path=db)
    calls: list = []
    printed: list[str] = []
    report = se.apply_change(
        ["ollama"], orchestrator_root=tmp_path / "orch", db_path=db,
        write_infra_env=lambda infra, rows: calls.append(("infra", infra, sorted(rows))),
        reproject=lambda path: calls.append(("reproject", path)),
        register_mcps=lambda root: calls.append(("register", root)) or True,
        out=printed.append,
    )
    assert calls == [
        ("infra", tmp_path / "orch" / "infrastructure", ["ollama"]),
        ("reproject", db),
        ("register", tmp_path / "orch"),
    ]
    assert report.ok and report.changed == ["ollama"]
    assert printed == ["ollama: http://localhost:11434 (external)"]


def test_apply_change_with_nothing_changed_runs_nothing(db: Path, tmp_path: Path) -> None:
    def boom(*_a):
        raise AssertionError("no step may run")
    report = se.apply_change([], orchestrator_root=tmp_path, db_path=db,
                             write_infra_env=boom, reproject=boom, register_mcps=boom, out=boom)
    assert report.steps == {} and report.ok


def test_a_failing_step_is_recorded_and_the_chain_goes_on(db: Path, tmp_path: Path) -> None:
    ran: list[str] = []

    def infra(*_a):
        raise OSError("disk full")
    report = se.apply_change(
        ["weaviate"], orchestrator_root=tmp_path, db_path=db,
        write_infra_env=infra,
        reproject=lambda _p: ran.append("reproject"),
        register_mcps=lambda _r: False,
        out=lambda _l: None,
    )
    assert ran == ["reproject"]
    assert report.steps == {"infra_env": "failed", "reproject": "ok", "register_mcps": "failed"}
    assert "disk full" in report.errors["infra_env"] and not report.ok


def test_commit_rows_propagates_only_propagating_changes(db: Path, tmp_path: Path) -> None:
    seen: list = []
    seams = dict(
        write_infra_env=lambda _i, _r: None,
        reproject=lambda _p: seen.append("reproject"),
        register_mcps=lambda _r: True,
        out=lambda _l: None,
    )
    row = se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="install_probe")
    se.commit_rows([row], orchestrator_root=tmp_path, db_path=db, **seams)
    se.commit_rows([dataclasses.replace(row, verified_at=9)], orchestrator_root=tmp_path, db_path=db, **seams)
    assert seen == ["reproject"], "a verified_at-only change runs no follow-up chain"


# ─── CLI ────────────────────────────────────────────────────────────────


def _seed_mixed(db: Path) -> None:
    se.write_rows([
        se.EndpointRow(service="weaviate", mode="adopted_container", port=8080, grpc_port=50051,
                       container_name="their_weaviate", source="user_gui"),
        se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="install_probe"),
        se.EndpointRow(service="code_embed", mode="vco_managed", port=21440, source="install_probe"),
    ], db_path=db)


def test_cli_plan_shell_names_only_vco_managed_services(db: Path, capsys) -> None:
    _seed_mixed(db)
    assert se._main(["plan", "--shell", "--db-path", str(db)]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["VCO_MANAGED_SERVICES"] == "code_embed"
    assert lines["VCO_ADOPTED_CONTAINERS"] == "their_weaviate"
    assert lines["VCO_WEAVIATE_MODE"] == "adopted_container"
    assert lines["VCO_WEAVIATE_GRPC_PORT"] == "50051"
    assert lines["VCO_OLLAMA_CONTAINER"] == "''"
    assert lines["VCO_CODE_EMBED_CONTAINER"] == "vco_code_embed"
    assert lines["VCO_CODE_EMBED_URL"] == "http://localhost:21440"


def test_cli_plan_with_no_rows_is_vcos_own_stack(tmp_path: Path, capsys) -> None:
    assert se._main(["plan", "--json", "--db-path", str(tmp_path / "none.db")]) == 0
    p = json.loads(capsys.readouterr().out)
    assert p["managed_services"] == list(se.SERVICES)
    assert p["adopted_containers"] == []
    assert p["services"]["weaviate"]["present"] is False


def test_cli_show_and_resolve(db: Path, capsys) -> None:
    _seed_mixed(db)
    assert se._main(["show", "--json", "--db-path", str(db)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["services"]["weaviate"]["url"] == "http://localhost:8080"
    assert shown["services"]["weaviate"]["row"]["container_name"] == "their_weaviate"
    assert se._main(["resolve", "--service", "ollama", "--field", "port", "--db-path", str(db)]) == 0
    assert capsys.readouterr().out.strip() == "11434"
    assert se._main(["resolve", "--service", "ollama", "--field", "grpc_port", "--db-path", str(db)]) == 2


# ─── the projection and the standalone env read the rows ────────────────


@pytest.fixture
def project_db(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "acme"
    folder.mkdir()
    db = make_launcher_db(tmp_path / "launcher.db", projects=[{
        "project_id": "p-1", "name": "Acme", "folder_path": str(folder), "slug": "acme",
    }])
    root = tmp_path / "orch"
    (root / "launcher" / "dist").mkdir(parents=True)
    return db, root


def _env(db: Path, root: Path, **kw) -> dict:
    return project_env_from_db("p-1", db_path=db, orchestrator_root=root, **kw)["canonical_env"]


def test_projection_without_rows_is_the_compiled_default(project_db) -> None:
    env = _env(*project_db)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://localhost:8081", "8081")
    assert (env["OLLAMA_URL"], env["OLLAMA_PORT"]) == ("http://localhost:11435", "11435")
    assert env["CODE_EMBED_SERVICE_URL"] == env["CODE_EMBED_URL"] == "http://localhost:11440"


def test_projection_emits_code_embed_service_url_from_the_row(project_db) -> None:
    """Red-proof (5): a non-default code-embed row reaches
    CODE_EMBED_SERVICE_URL — the name every code-embed client reads — equal
    to CODE_EMBED_URL."""
    db, root = project_db
    se.write_rows([
        se.EndpointRow(service="code_embed", mode="vco_managed", host="127.0.0.1", port=21440,
                       source="install_probe"),
        se.EndpointRow(service="weaviate", mode="adopted_external", host="weaviate.lan", port=8090,
                       grpc_port=50051, source="user_cli"),
        se.EndpointRow(service="ollama", mode="adopted_external", host="gpu.lan", port=11434,
                       source="user_cli"),
    ], db_path=db)
    env = _env(db, root)
    assert env["CODE_EMBED_SERVICE_URL"] == env["CODE_EMBED_URL"] == "http://127.0.0.1:21440"
    assert env["CODE_EMBED_PORT"] == "21440"
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://weaviate.lan:8090", "8090")
    assert (env["OLLAMA_URL"], env["OLLAMA_PORT"]) == ("http://gpu.lan:11434", "11434")


def test_projection_ignores_retired_inputs(project_db, monkeypatch) -> None:
    db, root = project_db
    set_app_state(db, "weaviate.port_override", "18081")
    monkeypatch.setenv("VCT_WEAVIATE_URL", "http://statement.invalid:1")
    (root / "launcher" / "dist" / "vct-config.toml").write_text('weaviate_url = "http://file.invalid:7"\n')
    assert _env(db, root)["WEAVIATE_URL"] == "http://localhost:8081"


def test_a_pinned_port_replaces_only_the_absent_rows_default(project_db) -> None:
    db, root = project_db
    assert _env(db, root, weaviate_port_default=18081)["WEAVIATE_URL"] == "http://localhost:18081"
    se.write_rows([se.EndpointRow(service="weaviate", mode="adopted_external", host="weaviate.lan",
                                  port=8090, grpc_port=50051, source="user_cli")], db_path=db)
    env = _env(db, root, weaviate_port_default=18081, ollama_port_default=21435)
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://weaviate.lan:8090", "8090")
    assert (env["OLLAMA_URL"], env["OLLAMA_PORT"]) == ("http://localhost:21435", "21435")


def test_a_pinned_url_is_used_as_given(project_db) -> None:
    env = _env(*project_db, weaviate_url_override="http://pinned:1234")
    assert (env["WEAVIATE_URL"], env["WEAVIATE_PORT"]) == ("http://pinned:1234", "1234")


def test_standalone_env_uses_the_machine_resolver(tmp_path: Path, monkeypatch) -> None:
    """Red-proof (6): ``_apply_standalone_env`` (no project row) writes the
    machine's rows, not literals."""
    from vco_lib.project_init import _apply_standalone_env

    db = make_launcher_db(tmp_path / "machine.db")
    se.write_rows([
        se.EndpointRow(service="weaviate", mode="adopted_external", host="weaviate.lan", port=8090,
                       grpc_port=50051, source="user_cli"),
        se.EndpointRow(service="ollama", mode="adopted_external", port=11434, source="user_cli"),
        se.EndpointRow(service="code_embed", mode="vco_managed", port=21440, source="install_probe"),
    ], db_path=db)
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    folder = tmp_path / "standalone"
    (folder / ".claude").mkdir(parents=True)
    result = _apply_standalone_env(folder, orchestrator_root=None, project_name="Standalone")
    assert result["action"] == "applied", result
    env = json.loads((folder / ".claude" / "settings.json").read_text())["env"]
    assert env["WEAVIATE_URL"] == "http://weaviate.lan:8090"
    assert env["WEAVIATE_PORT"] == "8090"
    assert env["OLLAMA_URL"] == "http://localhost:11434"
    assert env["CODE_EMBED_URL"] == env["CODE_EMBED_SERVICE_URL"] == "http://localhost:21440"
    assert env["CODE_EMBED_PORT"] == "21440"


# ─── the suite's own containment ────────────────────────────────────────


def test_the_suites_default_launcher_db_points_every_service_at_the_sentinel() -> None:
    """conftest W-ENDPOINTS: an unpinned machine resolver in this suite
    answers the unroutable port, never a live 8081/11435/11440."""
    urls = se.machine_service_urls()
    assert urls["weaviate_url"] == urls["ollama_url"] == urls["code_embed_url"] == "http://127.0.0.1:9"
    assert urls["weaviate_grpc_port"] == 9
