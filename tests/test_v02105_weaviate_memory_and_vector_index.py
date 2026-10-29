"""v0.2.105 — Weaviate memory limit, RQ-8 compression and per-collection
vector-cache cap.

* the host-sized ``mem_limit`` formula (floor / ceiling / rounding, unknown RAM
  and an exported value leave ``.env`` alone) and the install channel that
  carries it into ``infrastructure/.env``;
* the class definitions every create path uses carry the cache cap, and adding
  it never turns an existing collection's schema delta into a migration;
* the update step (``vco_lib.weaviate_vector_index``): what it changes, what it
  leaves alone, the load-before-PUT order, idempotency and its deferral;
* the shipped pins: one Weaviate version in every compose file and in CI, and
  the memory / compression env both compose files must carry for that version.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vco_lib import compose_env, project_init, weaviate_memory
from vco_lib import weaviate_vector_index as wvi
from vco_lib.install_embed_concurrency import finalize_embed_config_for_host

REPO = Path(__file__).resolve().parents[1]
INFRA_COMPOSE = REPO / "infrastructure" / "docker-compose.yml"
LEGACY_COMPOSE = REPO / "claude_mcp_servers" / "compose.yaml"
CI_SMOKE = REPO / ".github" / "workflows" / "installer-smoke.yml"
CAP = wvi.policy().cache_max_objects


# ── memory limit formula ───────────────────────────────────────────────────

@pytest.mark.parametrize("ram_gb, want", [
    (2.0, 2048),        # floor
    (4.0, 2048),        # 40% = 1638 MiB -> floor
    (8.0, 3072),        # 3276 -> rounded down to 256 MiB
    (16.0, 6400),
    (61.4, 25088),      # the 61 GB field host of #509
    (64.0, 26112),
    (128.0, 32768),     # ceiling
    (512.0, 32768),
])
def test_mem_limit_formula(ram_gb, want):
    assert weaviate_memory.mem_limit_mib(ram_gb) == want
    assert want % weaviate_memory.STEP_MIB == 0


@pytest.mark.parametrize("ram_gb", [None, 0, 0.0, -3, "x"])
def test_unknown_ram_gives_no_limit(ram_gb):
    assert weaviate_memory.mem_limit_mib(ram_gb) is None
    assert weaviate_memory.env_value(ram_gb, environ={}) is None


def test_env_value_is_written_in_compose_units():
    assert weaviate_memory.env_value(16.0, environ={}) == "6400m"


def test_an_exported_value_is_honoured_not_overwritten():
    assert weaviate_memory.env_value(16.0, environ={weaviate_memory.ENV_KEY: "0"}) is None


def test_compose_substitution_env_carries_the_limit(monkeypatch):
    monkeypatch.delenv(weaviate_memory.ENV_KEY, raising=False)
    env = compose_env.compose_substitution_env({"host_ram_gb": 32.0})
    assert env[weaviate_memory.ENV_KEY] == "13056m"


def test_compose_substitution_env_leaves_the_key_alone_without_ram(monkeypatch):
    monkeypatch.delenv(weaviate_memory.ENV_KEY, raising=False)
    assert weaviate_memory.ENV_KEY not in compose_env.compose_substitution_env({})


def test_compose_substitution_env_leaves_an_exported_value_alone(monkeypatch):
    monkeypatch.setenv(weaviate_memory.ENV_KEY, "4096m")
    assert weaviate_memory.ENV_KEY not in compose_env.compose_substitution_env({"host_ram_gb": 32.0})


def test_install_stamps_the_probed_ram_and_the_writer_persists_it(tmp_path, monkeypatch):
    """The real chain: install's host stamp -> the infrastructure/.env writer."""
    monkeypatch.delenv(weaviate_memory.ENV_KEY, raising=False)
    monkeypatch.delenv("VCO_OLLAMA_KEEP_ALIVE", raising=False)
    cfg: dict = {}
    sysinfo = SimpleNamespace(ram_gb=16.0, vram_gb=0.0, has_gpu=False, gpu_vendor="")
    finalize_embed_config_for_host(cfg, sysinfo)
    assert cfg["host_ram_gb"] == 16.0
    infra = tmp_path / "infrastructure"
    (infra).mkdir()
    (infra / ".env").write_text("USER_LINE=1\nVCT_WEAVIATE_MEM_LIMIT=999m\n", encoding="utf-8")
    ok, msg = compose_env.write_infrastructure_env(infra, cfg)
    assert ok, msg
    text = (infra / ".env").read_text(encoding="utf-8")
    assert "USER_LINE=1" in text
    assert "VCT_WEAVIATE_MEM_LIMIT=6400m" in text
    assert "999m" not in text


# ── class creation: the cache cap on every REST definition ────────────────

def _must(body):
    assert body is not None
    return body


def _slots(defn):
    return defn["vectorConfig"].values()


@pytest.mark.parametrize("builder, name", [
    (project_init.kg_class_definition, "Foo_KnowledgeGraph"),
    (project_init.development_class_definition, "Foo_Development"),
    (project_init.diagrams_class_definition, "Foo_Diagrams"),
])
def test_kg_shaped_definitions_carry_the_kg_cache_cap(builder, name):
    for slot in _slots(builder(name)):
        assert slot["vectorIndexType"] == "hnsw"
        assert slot["vectorIndexConfig"] == {"vectorCacheMaxObjects": CAP["kg"]}


def test_code_definitions_carry_the_code_cache_cap():
    for defn in project_init.code_class_definitions("Foo_").values():
        for slot in _slots(defn):
            assert slot["vectorIndexConfig"] == {"vectorCacheMaxObjects": CAP["code"]}


def test_hfresh_definitions_are_unchanged_by_the_cache_cap():
    for defn in project_init.code_class_definitions("Foo_", index_type="hfresh").values():
        for slot in _slots(defn):
            assert "vectorCacheMaxObjects" not in slot.get("vectorIndexConfig", {})


def test_no_definition_sends_rq_so_an_old_weaviate_still_accepts_it():
    """RQ for new collections is the server default (DEFAULT_QUANTIZATION);
    an RQ field in the body would be rejected by an adopted pre-1.32 server."""
    defs = [project_init.kg_class_definition("Foo_KnowledgeGraph"),
            *project_init.code_class_definitions("Foo_").values()]
    assert "rq" not in json.dumps(defs)


def test_adding_the_cap_never_turns_a_live_collection_into_a_migration():
    target = project_init.kg_class_definition("Foo_KnowledgeGraph")
    live = json.loads(json.dumps(target))
    for slot in live["vectorConfig"].values():  # a pre-v0.2.105 live schema
        slot["vectorIndexConfig"] = {"vectorCacheMaxObjects": wvi.WEAVIATE_UNLIMITED_CACHE,
                                     "rq": {"enabled": False}}
    assert not project_init._schema_delta(live, target).any()


def test_client_named_vector_carries_the_cap():
    nv = wvi.client_named_vector("qwen3_embed", "Foo_KnowledgeGraph")
    assert nv.name == "qwen3_embed"
    assert nv.vectorIndexConfig.vectorCacheMaxObjects == CAP["kg"]
    code = wvi.client_named_vector("codesage_embed", "Foo_CodeFunction")
    assert code.vectorIndexConfig.vectorCacheMaxObjects == CAP["code"]


# ── collection kind ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, kind", [
    ("Foo_CodeFunction", "code"), ("CodeModule", "code"), ("Foo_KnowledgeGraph", "kg"),
    ("Foo_development", "kg"), ("Foo_Diagrams", "kg"), ("VibeCodedOrchestrator_KnowledgeGraph", "kg"),
    ("DocumentChunks", None), ("Products", None), ("Foo_CodeBase", None),
])
def test_collection_kind(name, kind):
    assert wvi.collection_kind(name) == kind


@pytest.mark.parametrize("version, ok", [
    ("1.28.4", False), ("1.31.9", False), ("1.32.0", True), ("1.37.18", True),
    ("1.40.3-rc.1", True), ("", False), (None, False), ("garbage", False),
])
def test_server_supports_rq(version, ok):
    assert wvi.server_supports_rq(version) is ok


# ── plan_class_update: act and leave-alone ────────────────────────────────

def _live(name, *, rq=False, pq=False, cap=wvi.WEAVIATE_UNLIMITED_CACHE, index_type="hnsw",
          slots=("qwen3_embed", "ollama_embed")):
    return {"class": name, "properties": [{"name": "title", "dataType": ["text"]}],
            "vectorConfig": {s: {"vectorIndexType": index_type, "vectorizer": {"none": {}},
                                 "vectorIndexConfig": {"distance": "cosine",
                                                       "vectorCacheMaxObjects": cap,
                                                       "rq": {"enabled": rq, "bits": 8},
                                                       "pq": {"enabled": pq}}}
                             for s in slots}}


def test_plan_enables_rq_and_caps_an_unlimited_cache():
    body = _must(wvi.plan_class_update(_live("Foo_CodeFunction"), rq_supported=True))
    for slot in body["vectorConfig"].values():
        cfg = slot["vectorIndexConfig"]
        assert cfg["rq"] == {"enabled": True, "bits": 8, "rescoreLimit": 20}
        assert cfg["vectorCacheMaxObjects"] == CAP["code"]
        assert cfg["distance"] == "cosine"  # everything else is sent back as read


def test_plan_does_not_mutate_its_input():
    live = _live("Foo_KnowledgeGraph")
    before = json.dumps(live, sort_keys=True)
    wvi.plan_class_update(live, rq_supported=True)
    assert json.dumps(live, sort_keys=True) == before


def test_plan_leaves_a_conformant_class_alone():
    live = _live("Foo_KnowledgeGraph", rq=True, cap=CAP["kg"])
    assert wvi.plan_class_update(live, rq_supported=True) is None


def test_plan_leaves_a_non_vco_class_alone():
    assert wvi.plan_class_update(_live("Products"), rq_supported=True) is None


def test_plan_keeps_someone_elses_compression():
    body = _must(wvi.plan_class_update(_live("Foo_KnowledgeGraph", pq=True), rq_supported=True))
    for slot in body["vectorConfig"].values():  # cap still applied, PQ untouched, no RQ
        assert slot["vectorIndexConfig"]["pq"] == {"enabled": True}
        assert slot["vectorIndexConfig"]["rq"]["enabled"] is False


def test_plan_keeps_a_cache_cap_someone_set():
    body = _must(wvi.plan_class_update(_live("Foo_CodeFunction", cap=50_000), rq_supported=True))
    for slot in body["vectorConfig"].values():
        assert slot["vectorIndexConfig"]["vectorCacheMaxObjects"] == 50_000
        assert slot["vectorIndexConfig"]["rq"]["enabled"] is True


def test_plan_without_rq_support_only_caps_the_cache():
    body = _must(wvi.plan_class_update(_live("Foo_KnowledgeGraph"), rq_supported=False))
    for slot in body["vectorConfig"].values():
        assert slot["vectorIndexConfig"]["rq"]["enabled"] is False
        assert slot["vectorIndexConfig"]["vectorCacheMaxObjects"] == CAP["kg"]
    capped = _live("Foo_KnowledgeGraph", cap=CAP["kg"])
    assert wvi.plan_class_update(capped, rq_supported=False) is None


def test_plan_leaves_a_non_hnsw_index_alone():
    assert wvi.plan_class_update(_live("Foo_CodeFunction", index_type="hfresh"),
                                 rq_supported=True) is None


def test_plan_handles_a_legacy_single_vector_class():
    live = {"class": "Foo_KnowledgeGraph", "vectorIndexType": "hnsw",
            "vectorIndexConfig": {"vectorCacheMaxObjects": wvi.WEAVIATE_UNLIMITED_CACHE,
                                  "rq": {"enabled": False}}}
    body = _must(wvi.plan_class_update(live, rq_supported=True))
    assert body["vectorIndexConfig"]["rq"]["enabled"] is True
    assert body["vectorIndexConfig"]["vectorCacheMaxObjects"] == CAP["kg"]


# ── apply_policy / run_update_step with a fake Weaviate ───────────────────

class FakeWeaviate:
    def __init__(self, classes, version="1.37.18", put_status=200, down=False):
        self.classes = {c["class"]: c for c in classes}
        self.version, self.put_status, self.down = version, put_status, down
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, body: dict | None = None, timeout=30.0):
        self.calls.append((method, url.split("/v1/", 1)[1]))
        if self.down:
            raise ConnectionRefusedError("refused")
        path = url.split("/v1/", 1)[1]
        if path == "meta":
            return 200, json.dumps({"version": self.version}).encode()
        if path == "schema":
            return 200, json.dumps({"classes": list(self.classes.values())}).encode()
        if method == "GET" and path.startswith("objects?"):
            return 200, b'{"objects": []}'
        if method == "PUT":
            if self.put_status == 200 and body is not None:
                self.classes[body["class"]] = body
            return self.put_status, b'{"error": "nope"}'
        raise AssertionError(f"unexpected {method} {path}")


def test_apply_loads_each_class_before_its_put_and_skips_the_rest():
    fake = FakeWeaviate([_live("Foo_CodeFunction"), _live("Foo_KnowledgeGraph", rq=True, cap=CAP["kg"]),
                         _live("Products")])
    res = wvi.apply_policy("http://w:1", request=fake)
    assert res.ok and res.rq_supported
    assert res.updated == ["Foo_CodeFunction"]
    assert res.conformant == ["Foo_KnowledgeGraph"]
    writes = [c for c in fake.calls if c[0] == "PUT"]
    assert writes == [("PUT", "schema/Foo_CodeFunction")]
    i = fake.calls.index(("PUT", "schema/Foo_CodeFunction"))
    assert fake.calls[i - 1] == ("GET", "objects?class=Foo_CodeFunction&limit=1")
    assert not any("Products" in p for _, p in fake.calls)


def test_apply_is_idempotent():
    fake = FakeWeaviate([_live("Foo_CodeFunction"), _live("Foo_KnowledgeGraph")])
    assert len(wvi.apply_policy("http://w:1", request=fake).updated) == 2
    fake.calls.clear()
    res = wvi.apply_policy("http://w:1", request=fake)
    assert res.updated == [] and sorted(res.conformant) == ["Foo_CodeFunction", "Foo_KnowledgeGraph"]
    assert not [c for c in fake.calls if c[0] == "PUT"]


def test_apply_on_an_old_server_only_caps_the_cache():
    fake = FakeWeaviate([_live("Foo_CodeFunction")], version="1.28.4")
    res = wvi.apply_policy("http://w:1", request=fake)
    assert not res.rq_supported and res.updated == ["Foo_CodeFunction"]
    slot = fake.classes["Foo_CodeFunction"]["vectorConfig"]["qwen3_embed"]["vectorIndexConfig"]
    assert slot["rq"]["enabled"] is False and slot["vectorCacheMaxObjects"] == CAP["code"]


class _Report:
    def __init__(self):
        self.entries = []

    def add_entry(self, e):
        self.entries.append(e)


def test_update_step_records_a_deferral_when_weaviate_refuses(capsys):
    fake = FakeWeaviate([_live("Foo_CodeFunction")], put_status=422)
    report, events = _Report(), []
    res = wvi.run_update_step(report, weaviate_url="http://w:1", request=fake,
                              log_event=lambda *a, **k: events.append((a, k)))
    assert not res.ok and res.failed[0][0] == "Foo_CodeFunction"
    assert [e.condition_id for e in report.entries] == [wvi.CID_UPDATE_FAILED]
    assert "Foo_CodeFunction" in report.entries[0].detected
    assert events and events[0][0][1] == "warn"
    assert "1 failed" in capsys.readouterr().out


def test_update_step_records_a_deferral_when_weaviate_is_down():
    report = _Report()
    res = wvi.run_update_step(report, weaviate_url="http://w:1",
                              request=FakeWeaviate([], down=True))
    assert res.unreachable and [e.condition_id for e in report.entries] == [wvi.CID_UPDATE_FAILED]


def test_update_step_records_nothing_on_success():
    report, events = _Report(), []
    res = wvi.run_update_step(report, weaviate_url="http://w:1",
                              request=FakeWeaviate([_live("Foo_CodeFunction")]),
                              log_event=lambda *a, **k: events.append((a, k)))
    assert res.ok and report.entries == []
    assert events[0][0][1] == "ok" and events[0][1]["data"]["updated"] == ["Foo_CodeFunction"]


def test_the_deferral_is_registered_as_auto_retryable_and_owned():
    from vco_lib.deferral_registry import load_registry

    from vco_lib.deferral_registry import condition

    load_registry()  # the table parses with the new row
    row = condition(wvi.CID_UPDATE_FAILED)
    assert row is not None
    assert row.condition_class == "auto_retryable" and row.is_owned_by_install


def _quiet_schema_migrations(monkeypatch, install):
    """Stub the other steps of install's schema-migration chain (each is
    covered by its own tests) so only the wiring of step 7e is observed."""
    for name in ("_run_additive_temporal_props_migration", "_migrate_kg_named_vector_slots",
                 "_emit_lowercase_codegraph_cleanup_deferrals", "_log_install_event"):
        monkeypatch.setattr(install, name, lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("stubbed out")
    monkeypatch.setattr("vco_lib.launcher_db_reader.get_orchestrator_root_project_id", boom)
    monkeypatch.setattr("vco_lib.schema_migration_runner.resolve_codegraph_migration_inputs", boom)
    monkeypatch.setattr("vco_lib.codegraph_registry_reconcile.reconcile_codegraph_registry", boom)
    monkeypatch.setattr(install._wh, "weaviate_url_default", lambda: "http://w:1")


def test_install_runs_the_vector_index_step_with_the_schema_migrations(monkeypatch):
    import install
    from vco_lib.deferral_report import DeferralReport

    _quiet_schema_migrations(monkeypatch, install)
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "weaviate_pending", False)
    calls = []
    monkeypatch.setattr(install._weaviate_vector_index, "run_update_step",
                        lambda report, **kw: calls.append((report, kw)))
    report = DeferralReport()
    install._run_schema_migration_scripts(report)
    assert len(calls) == 1 and calls[0][0] is report
    assert calls[0][1]["weaviate_url"] == "http://w:1"


def test_install_skips_the_step_while_a_weaviate_choice_is_pending(monkeypatch):
    import install
    from vco_lib.deferral_report import DeferralReport

    _quiet_schema_migrations(monkeypatch, install)
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "weaviate_pending", True)
    calls = []
    monkeypatch.setattr(install._weaviate_vector_index, "run_update_step",
                        lambda report, **kw: calls.append(report))
    install._run_schema_migration_scripts(DeferralReport())
    assert calls == []


# ── shipped pins ───────────────────────────────────────────────────────────

_PIN = re.compile(r"^\s*image:\s*(?:cr\.weaviate\.io/)?semitechnologies/weaviate:(\d+)\.(\d+)\.(\d+)\s*$", re.M)


def _pins(path):
    return [tuple(int(x) for x in m) for m in _PIN.findall(path.read_text(encoding="utf-8"))]


def test_every_compose_file_and_ci_pin_one_weaviate_version():
    pins = {p for f in (INFRA_COMPOSE, LEGACY_COMPOSE, CI_SMOKE) for p in _pins(f)}
    assert len(pins) == 1, pins
    (version,) = pins
    assert version >= wvi.policy().rq_min_server_version, "RQ needs >= 1.32"


def _weaviate_env(path):
    from vco_lib.service_reconcile import compose_reclaim_env

    return compose_reclaim_env(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("compose", [INFRA_COMPOSE, LEGACY_COMPOSE])
def test_compose_carries_the_memory_and_compression_env(compose):
    env = _weaviate_env(compose)
    (version,) = set(_pins(compose))
    assert env["GOGC"] == "50"
    assert env["DEFAULT_QUANTIZATION"] == "rq-8"
    if version >= (1, 36, 6):  # eager single-tenant shard loading from 1.36.6
        assert env["LAZY_LOAD_SHARD_COUNT_THRESHOLD"] == "0"
    if version < (1, 38, 12):  # the read-only guard force-loads every lazy shard
        assert env["MEMORY_READONLY_PERCENTAGE"] == "0"
    text = compose.read_text(encoding="utf-8")
    assert re.search(r'^\s*LIMIT_RESOURCES:\s*"true"', text, re.M)
    assert re.search(r"^\s*mem_limit:\s*\$\{VCT_WEAVIATE_MEM_LIMIT:-0\}\s*$", text, re.M)
    assert re.search(r"^\s*restart:\s*unless-stopped", text, re.M)


def test_both_compose_files_agree_on_the_drift_keys():
    assert _weaviate_env(INFRA_COMPOSE) == _weaviate_env(LEGACY_COMPOSE)


def test_the_drift_gate_watches_the_new_keys():
    from vco_lib.service_adoption import WEAVIATE_RECLAIM_ENV_KEYS

    for key in ("GOGC", "MEMORY_READONLY_PERCENTAGE", "LAZY_LOAD_SHARD_COUNT_THRESHOLD",
                "DEFAULT_QUANTIZATION"):
        assert key in WEAVIATE_RECLAIM_ENV_KEYS


def test_the_rust_create_embeds_the_same_table():
    """Tier B parity: the launcher's KG create reads this exact file (the
    Rust test `every_kg_slot_carries_the_tables_cache_cap` checks the value)."""
    rs = (REPO / "launcher/src-tauri/src/commands/project_state_cmd.rs").read_text(encoding="utf-8")
    assert 'include_str!("../../../../vco_lib/weaviate_vector_index.toml")' in rs
    assert (REPO / "launcher/src-tauri/src/commands" / "../../../../vco_lib/weaviate_vector_index.toml").resolve() \
        == wvi.CONFIG_PATH
