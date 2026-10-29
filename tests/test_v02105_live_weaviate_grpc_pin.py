"""v0.2.105 — the opt-in LIVE Weaviate tests never default their gRPC channel
to the live ``vco_weaviate``'s port 50052.

Three live round-trips (migrate, schema migration, embedding enrichment) built
their client with ``GRPC_PORT`` defaulting to 50052. Pointed at a disposable
Weaviate on other ports through ``WEAVIATE_URL`` alone, they sent REST to the
disposable instance and gRPC to the live one on the same host (seen once while
verifying the Weaviate 1.37.18 upgrade). ``tests/common/live_weaviate`` now
owns the gate and the client: an unnamed gRPC port skips the test before any
probe, and the client falls back to the dead sentinel port, never 50052.

The wiring tests drive each live class's own client-building code with the gRPC
variables UNSET and record the port the client was built with.
"""
from __future__ import annotations

from unittest import mock

import pytest

from tests.common import live_weaviate as lw

_GRPC_KEYS = ("GRPC_PORT", "WEAVIATE_GRPC_PORT")
_URL = "http://127.0.0.1:18089"


@pytest.fixture
def no_grpc_env(monkeypatch):
    for key in _GRPC_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── the helper ─────────────────────────────────────────────────────────────

def test_the_sentinel_is_the_suites_dead_port_not_the_live_one():
    from tests import conftest

    assert lw.DEAD_GRPC_PORT == conftest._UNROUTABLE_PORT != 50052


def test_an_unnamed_grpc_port_falls_back_to_the_sentinel():
    assert lw.explicit_grpc_port({}) is None
    assert lw.grpc_port({}) == lw.DEAD_GRPC_PORT


def test_a_named_grpc_port_is_used_grpc_port_first():
    assert lw.grpc_port({"GRPC_PORT": "15005"}) == 15005
    assert lw.grpc_port({"WEAVIATE_GRPC_PORT": "15006"}) == 15006
    assert lw.grpc_port({"GRPC_PORT": "15005", "WEAVIATE_GRPC_PORT": "15006"}) == 15005


def test_the_gate_skips_an_unpaired_run_before_any_probe():
    with mock.patch("urllib.request.urlopen") as probe:
        why = lw.skip_reason(_URL, {})
    assert why is not None and "GRPC_PORT" in why
    probe.assert_not_called()


def test_the_gate_probes_once_a_port_is_named():
    resp = mock.MagicMock(status=200)
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp) as probe:
        assert lw.skip_reason(_URL, {"GRPC_PORT": "15005"}) is None
    probe.assert_called_once()


def test_connect_pairs_the_rest_host_with_the_resolved_grpc_port():
    with mock.patch("weaviate.connect_to_custom") as conn:
        lw.connect(_URL, {})
    kw = conn.call_args.kwargs
    assert (kw["http_host"], kw["http_port"], kw["grpc_host"]) == ("127.0.0.1", 18089, "127.0.0.1")
    assert kw["grpc_port"] == lw.DEAD_GRPC_PORT


# ── the wiring: each live class's own client code, gRPC env unset ─────────

class _Stop(Exception):
    pass


def _recording_connect(ports):
    def connect(**kw):
        ports.append(kw["grpc_port"])
        return mock.MagicMock()
    return connect


def _instance(cls):
    """A real TestCase instance (its assert helpers set up), url pinned."""
    name = next(n for n in dir(cls) if n.startswith("test_"))
    inst = cls(name)
    inst.url = _URL
    return inst


@pytest.mark.usefixtures("no_grpc_env")
def test_the_migrate_live_test_never_dials_the_live_grpc_port():
    import tests.test_vco_lib_migrate as mod

    cls = mod.LiveMigrateIntegrationTest
    ports: list[int] = []
    with mock.patch("weaviate.connect_to_custom", side_effect=_recording_connect(ports)), \
            mock.patch.object(mod.project_init, "_create_class"), \
            mock.patch.object(mod.project_init, "_fetch_schema",
                              side_effect=lambda n, **_: None if n == cls.KG_NAME else {"class": n}), \
            mock.patch.object(mod.project_init, "migrate_collections", side_effect=_Stop):
        inst = _instance(cls)
        inst._populate_objects(cls.KG_NAME)
        inst._count_objects(cls.KG_NAME)
        for name in ("test_crash_recovery_recovers_data_when_name_deleted_mid_copy",
                     "test_crash_recovery_drops_orphan_when_name_already_intact"):
            with pytest.raises(_Stop):
                getattr(inst, name)()
    assert len(ports) == 5 and set(ports) == {lw.DEAD_GRPC_PORT}


@pytest.mark.usefixtures("no_grpc_env")
def test_the_schema_live_test_never_dials_the_live_grpc_port():
    import tests.test_weaviate_schema as mod

    ports: list[int] = []
    with mock.patch("weaviate.connect_to_custom", side_effect=_recording_connect(ports)):
        inst = _instance(mod.LiveSchemaMigrationTest)
        inst._populate_with_vectors()
        inst._count_objects_and_verify_qwen3()
    assert ports == [lw.DEAD_GRPC_PORT] * 2


@pytest.mark.usefixtures("no_grpc_env")
def test_the_enrichment_live_test_never_dials_the_live_grpc_port():
    import tests.test_embedding_enrichment as mod

    ports: list[int] = []
    with mock.patch("weaviate.connect_to_custom", side_effect=_recording_connect(ports)):
        inst = _instance(mod.LiveEnrichmentTest)
        inst._populate()
        inst._read_all()
    assert ports == [lw.DEAD_GRPC_PORT] * 2


@pytest.mark.usefixtures("no_grpc_env")
@pytest.mark.parametrize("module, gate", [
    ("tests.test_weaviate_schema", lambda m: m._weaviate_reachable(_URL)),
    ("tests.test_embedding_enrichment", lambda m: m._weaviate_reachable(_URL)),
    ("tests.test_vco_lib_migrate", lambda m: m._weaviate_reachable()),
])
def test_each_live_gate_stays_shut_without_a_named_grpc_port(module, gate, monkeypatch):
    import importlib

    mod = importlib.import_module(module)
    monkeypatch.setenv("WEAVIATE_URL", _URL)
    resp = mock.MagicMock(status=200)
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp):
        assert gate(mod) is False  # the server "answers", the gRPC port is unnamed
        monkeypatch.setenv("GRPC_PORT", "15005")
        assert gate(mod) is True
