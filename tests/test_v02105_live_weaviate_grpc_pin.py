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

import os
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


# ── W-N1 (v0.2.105 fix round 2): the PRODUCTION legs read GRPC_PORT only ──
#
# The live classes do not only build their own client through the helper —
# they then drive production connects (vco_lib/weaviate_schema.py:504,
# vco_lib/embedding_enrichment.py:342, vco_lib/weaviate_helpers.py:680),
# each reading GRPC_PORT ONLY and defaulting to the live 50052. A run that
# named just WEAVIATE_GRPC_PORT passed the old gate and sent those legs to
# the LIVE server. The gate now promotes the named port into GRPC_PORT when
# it opens, and refuses 50052 itself without an explicit opt-in.

def _ready_200():
    resp = mock.MagicMock(status=200)
    resp.__enter__.return_value = resp
    return resp


@pytest.mark.usefixtures("no_grpc_env")
def test_a_lone_weaviate_grpc_port_promotes_itself_into_grpc_port(monkeypatch):
    """ACT: WEAVIATE_GRPC_PORT alone either skips or promotes itself — and
    the gate opens, so it must promote: the production legs' own read
    (``int(os.environ.get("GRPC_PORT", "50052"))``) must see 15005, not the
    live default."""
    monkeypatch.setenv("WEAVIATE_GRPC_PORT", "15005")
    with mock.patch("urllib.request.urlopen", return_value=_ready_200()):
        assert lw.skip_reason(_URL) is None
    assert os.environ["GRPC_PORT"] == "15005"
    assert int(os.environ.get("GRPC_PORT", "50052")) == 15005


@pytest.mark.usefixtures("no_grpc_env")
def test_promotion_never_overwrites_a_named_grpc_port(monkeypatch):
    """LEAVE-ALONE: GRPC_PORT is the variable the operator (and the shipped
    edges' order) put first — promotion must not touch a value it set."""
    monkeypatch.setenv("GRPC_PORT", "15005")
    monkeypatch.setenv("WEAVIATE_GRPC_PORT", "15006")
    with mock.patch("urllib.request.urlopen", return_value=_ready_200()):
        assert lw.skip_reason(_URL) is None
    assert os.environ["GRPC_PORT"] == "15005"


@pytest.mark.usefixtures("no_grpc_env")
def test_a_shut_gate_promotes_nothing(monkeypatch):
    """LEAVE-ALONE: when the server does not answer, the test skips and the
    environment is left exactly as the operator set it."""
    monkeypatch.setenv("WEAVIATE_GRPC_PORT", "15005")
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert lw.skip_reason(_URL) is not None
    assert "GRPC_PORT" not in os.environ


@pytest.mark.usefixtures("no_grpc_env")
def test_connect_also_promotes_a_lone_weaviate_grpc_port(monkeypatch):
    """A caller that builds a client WITHOUT consulting the gate first still
    pairs the production legs with the port it named."""
    monkeypatch.setenv("WEAVIATE_GRPC_PORT", "15005")
    with mock.patch("weaviate.connect_to_custom") as conn:
        lw.connect(_URL)
    assert conn.call_args.kwargs["grpc_port"] == 15005
    assert os.environ["GRPC_PORT"] == "15005"


def test_an_explicit_mapping_leaves_the_real_environment_alone(monkeypatch):
    """PURITY: every gate/client call that passes an explicit mapping (the
    unit tests, a child-env builder) mutates nothing — promotion is a
    real-environment side effect only."""
    monkeypatch.delenv("GRPC_PORT", raising=False)
    monkeypatch.setenv("WEAVIATE_GRPC_PORT", "15006")
    with mock.patch("urllib.request.urlopen", return_value=_ready_200()):
        assert lw.skip_reason(_URL, {"WEAVIATE_GRPC_PORT": "15005"}) is None
    assert os.environ["WEAVIATE_GRPC_PORT"] == "15006"
    assert "GRPC_PORT" not in os.environ


def test_the_live_grpc_port_is_refused_without_an_explicit_opt_in():
    """ACT: 50052 is the LIVE vco_weaviate's published gRPC port — naming it
    skips (before any probe) unless the operator opts in deliberately."""
    with mock.patch("urllib.request.urlopen") as probe:
        why = lw.skip_reason(_URL, {"GRPC_PORT": "50052"})
    assert why is not None
    assert "50052" in why and lw.ALLOW_LIVE_GRPC_ENV in why
    probe.assert_not_called()
    with mock.patch("urllib.request.urlopen") as probe:
        assert lw.skip_reason(_URL, {"WEAVIATE_GRPC_PORT": "50052"}) is not None
    probe.assert_not_called()


def test_the_live_grpc_port_runs_with_the_explicit_opt_in():
    """LEAVE-ALONE: the opt-in is real — an operator who genuinely targets
    the live server's gRPC port passes the gate (and still gets the
    readiness probe)."""
    env = {"GRPC_PORT": "50052", lw.ALLOW_LIVE_GRPC_ENV: "1"}
    with mock.patch("urllib.request.urlopen", return_value=_ready_200()) as probe:
        assert lw.skip_reason(_URL, env) is None
    probe.assert_called_once()


def test_the_opt_in_refusal_holds_for_a_non_truthy_value():
    with mock.patch("urllib.request.urlopen") as probe:
        why = lw.skip_reason(_URL, {"GRPC_PORT": "50052", lw.ALLOW_LIVE_GRPC_ENV: "0"})
    assert why is not None and "50052" in why
    probe.assert_not_called()


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
