# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-B / W6-B2 — the code-embedding model has ONE resolver, and a
selection is APPLIED automatically and additively.

History: the launcher's Preferences → "Code Graph Embeddings" keys were written
since v0.2.23 and read by nothing (audit F1). W6-B gave them a reader; its
review blocked it (HIGH-1: the install-projected ``CODE_EMBED_BACKEND``
outranked the pin on every root; HIGH-2: a pin pointed searches at a named
vector the stored objects are not in). W6-B2 built :mod:`vco_lib.code_embed_model`;
the owner's 2026-10-10 ruling then made a selection apply itself:

* TARGET — ``CODE_EMBED_MODEL`` env → the project's Codegraph-tab selection →
  the Preferences pin → ``default_code_embedding`` → ``CODE_EMBED_BACKEND``
  (a default only) → codesage;
* RECORD — what the graph's COMPLETE slot holds (the launcher binding row);
  searches use it; writes embed the target (and carry the record) while the
  target's slot fills; when the slot is complete the record flips. Switching
  back to a still-complete slot is an instant flip. Nothing is dropped.
* OpenAI EMBEDDINGS (KG + code) need their own consent, separate from the
  summaries consent (:mod:`vco_lib.openai_consent`).

Every test drives real code paths with fakes; nothing reaches a live Weaviate
(the suite pins ``WEAVIATE_URL`` at the unroutable sentinel).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.common.launcher_db_fixture import make_launcher_db, set_app_state
from vco_lib import code_embed_model as cem
from vco_lib import openai_consent
from vco_lib.embedding_service import (
    DEFAULT_CODE_MODEL,
    DEFAULT_TEXT_MODEL,
    EmbeddingService,
    NoEmbeddingBackendError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
CODESAGE_RECORD = cem.CodeModelRecord(DEFAULT_CODE_MODEL, 2048, "codesage_embed")
QWEN3_RECORD = cem.CodeModelRecord(DEFAULT_TEXT_MODEL, 1024, "qwen3_embed")
CONSENT = openai_consent.APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT


def pick(env=None, state=None, project_target=None):
    return cem.resolve_code_model_pick(env or {}, app_state=(state or {}).get,
                                       project_target=project_target)


def resolve(env=None, state=None, record=None, role="query"):
    return cem.resolve_code_model(env=env or {}, app_state=(state or {}).get,
                                  record=record, record_reader=lambda _p: None, role=role)


@pytest.fixture
def launcher_db(tmp_path, monkeypatch):
    """A real-schema launcher.db with one registered project ``Acme`` whose
    code-graph binding is the never-built seed (codesage/2048, no provenance)."""
    db = make_launcher_db(tmp_path, projects=[{
        "project_id": "p1", "name": "Acme", "folder_path": str(tmp_path / "acme"),
        "codegraph_prefix": "Acme", "codegraph_embedding_model": DEFAULT_CODE_MODEL,
        "codegraph_embedding_dim": 2048,
    }])
    (tmp_path / "acme").mkdir()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.delenv("CODE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("CODE_EMBED_BACKEND", raising=False)
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    return db


def _state(db, **rows):
    for k, v in rows.items():
        set_app_state(db, k, v)


def _row(db):
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    r = dict(conn.execute("SELECT * FROM project_codegraph_bindings WHERE project_id='p1'").fetchone())
    conn.close()
    r["config"] = json.loads(r.get("config_json") or "null") or {}
    return r


def _stamp_record(db, model, dim, *, config=None):
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE project_codegraph_bindings SET embedding_model=?, embedding_dim=?, "
                 "last_analyzed_at=1, config_json=? WHERE project_id='p1'",
                 (model, dim, json.dumps(config or {})))
    conn.commit()
    conn.close()


class FakeColl:
    def __init__(self, rows):  # rows: list of {slot: vec}
        self.rows = rows

    def iterator(self, include_vector=None, return_properties=None):
        return [types.SimpleNamespace(vector=r) for r in self.rows]


class FakeClient:
    def __init__(self, colls):
        self.colls = colls
        self.collections = self

    def exists(self, name):
        return name in self.colls

    def get(self, name):
        return self.colls[name]

    def close(self):
        pass


# ═══════════════════════════ the target ═══════════════════════════


class TestTarget:
    def test_keys_match_the_launcher(self):
        loaders = (REPO_ROOT / "launcher" / "src" / "lib" / "preferences"
                   / "loaders.ts").read_text(encoding="utf-8")
        for key in (cem.APP_STATE_KEY_CODE_EMBED_OVERRIDE, cem.APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL,
                    cem.APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL, CONSENT):
            assert f"'{key}'" in loaders
        kg = (REPO_ROOT / "launcher" / "src" / "lib" / "project-state" / "kg-bindings.ts"
              ).read_text(encoding="utf-8")
        assert f"'{cem.TARGET_CONFIG_KEY}'" in kg

    def test_nothing_configured_is_codesage(self):
        assert pick() == (DEFAULT_CODE_MODEL, "builtin")

    @pytest.mark.parametrize("pin,model", [
        ("codesage", DEFAULT_CODE_MODEL), ("qwen3", DEFAULT_TEXT_MODEL), ("jina", _JINA),
    ])
    def test_pins(self, pin, model):
        assert pick(state={"code_embed_backend_override": pin}) == (model, "preferences")

    def test_ollama_and_openai_pins_use_their_model_rows(self):
        assert pick(state={"code_embed_backend_override": "ollama",
                           "code_embed_ollama_model": "my-code:latest"}) == ("my-code:latest", "preferences")
        assert pick(state={"code_embed_backend_override": "ollama"})[1] == "builtin"
        assert pick(state={"code_embed_backend_override": "openai"})[0] == "text-embedding-3-small"

    def test_install_default_row(self):
        assert pick(state={"default_code_embedding": _JINA}) == (_JINA, "default")

    def test_HIGH1_install_projected_backend_never_outranks_a_selection(self):
        for backend in ("gpu", "ollama", "openai", "service"):
            assert pick(env={"CODE_EMBED_BACKEND": backend},
                        state={"code_embed_backend_override": "qwen3"}) == (DEFAULT_TEXT_MODEL, "preferences")
            assert pick(env={"CODE_EMBED_BACKEND": backend},
                        state={"default_code_embedding": _JINA}) == (_JINA, "default")
        assert pick(env={"CODE_EMBED_BACKEND": "ollama"}) == (DEFAULT_TEXT_MODEL, "install-env")

    def test_precedence_env_then_project_then_pin(self):
        state = {"code_embed_backend_override": "qwen3"}
        assert pick(state=state, project_target=_JINA) == (_JINA, "project")
        assert pick(env={"CODE_EMBED_MODEL": "x-model"}, state=state, project_target=_JINA) == ("x-model", "env")


class TestHigh1ForProjectOnTheRoot:
    def test_install_written_env_block_does_not_block_the_pin(self, launcher_db):
        sys.path.insert(0, str(REPO_ROOT))
        import install  # noqa: PLC0415 — the real renderer, not a guess

        env_block = install._build_vco_settings_defaults(dict(install.EMBEDDING_CONFIGS["gpu"]))["env"]
        assert env_block["CODE_EMBED_BACKEND"] == "gpu"
        _state(launcher_db, code_embed_backend_override="qwen3")
        with _patched_adapters(), patch.dict(os.environ, dict(env_block, CODE_GRAPH_PROJECT="")):
            svc = EmbeddingService.for_project()
            try:
                assert (svc.code_model_id, svc.code_vector_slot) == (DEFAULT_TEXT_MODEL, "qwen3_embed")
            finally:
                svc.close()


# ═══════════════════════════ record vs target ═══════════════════════════


class TestResolution:
    def test_seed_is_not_a_record_and_a_build_is(self, launcher_db):
        assert cem.read_code_model_record("Acme") is None
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        assert cem.read_code_model_record("acme") == CODESAGE_RECORD

    def test_a_project_selection_is_a_target_never_a_record(self, launcher_db):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048, config={"code_model_target": _JINA})
        res = cem.resolve_code_model(codegraph_prefix="Acme", env={})
        assert res.recorded == CODESAGE_RECORD and res.target == _JINA
        assert res.target_source == "project" and res.filling

    def test_searches_keep_the_record_while_writes_fill_the_target(self):
        state = {"code_embed_backend_override": "qwen3"}
        q = resolve(state=state, record=CODESAGE_RECORD)
        assert (q.model, q.slot, q.filling) == (DEFAULT_CODE_MODEL, "codesage_embed", True)
        w = resolve(state=state, record=CODESAGE_RECORD, role="write")
        assert (w.model, w.slot, w.carry) == (DEFAULT_TEXT_MODEL, "qwen3_embed", DEFAULT_CODE_MODEL)

    def test_same_model_is_no_fill(self):
        w = resolve(state={"code_embed_backend_override": "codesage"}, record=CODESAGE_RECORD, role="write")
        assert (w.model, w.filling, w.carry) == (DEFAULT_CODE_MODEL, False, None)

    def test_no_record_uses_the_target_for_both_roles(self):
        for role in ("query", "write"):
            r = resolve(state={"code_embed_backend_override": "qwen3"}, role=role)
            assert (r.model, r.filling) == (DEFAULT_TEXT_MODEL, False)


class TestForProjectRoles:
    def test_query_role_keeps_the_recorded_slot(self, launcher_db):
        """Red on 2cc19a9c: a qwen3 pin moved the search slot over a
        codesage-indexed graph (silent empty search)."""
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, code_embed_backend_override="qwen3")
        with _patched_adapters(), patch.dict(os.environ, {"CODE_GRAPH_PROJECT": "Acme"}):
            svc = EmbeddingService.for_project()
            try:
                assert svc.code_vector_slot == "codesage_embed"
                assert svc.code_model_resolution.filling
            finally:
                svc.close()

    def test_write_role_embeds_the_target_and_carries_the_record(self, launcher_db):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, code_embed_backend_override="qwen3")
        with _patched_adapters() as a, patch.dict(os.environ, {"CODE_GRAPH_PROJECT": "Acme"}):
            a.ollama.embed.return_value = [0.1] * 1024
            a.code.embed.return_value = [0.2] * 2048
            svc = EmbeddingService.for_project(code_model_role="write")
            try:
                assert svc.code_vector_slot == "qwen3_embed"
                slots = svc.embed_code_all_configured("def f(): pass")
                assert set(slots) == {"qwen3_embed", "codesage_embed"}
            finally:
                svc.close()


class TestGpuLessInstall:
    def test_gpu_less_install_embeds_through_the_install_default(self, launcher_db):
        """The v0.2.100 4e3f13cf regression: no code_embed service + no env →
        a user project asked for CodeSage and its code embeds failed. Now the
        install default (`default_code_embedding`, the hardware ladder's
        Ollama pick) is the target and embeds through Ollama. Red on 2cc19a9c."""
        _state(launcher_db, default_code_embedding=DEFAULT_TEXT_MODEL)
        with _patched_adapters(code_ready=False) as a, patch.dict(os.environ, {"CODE_GRAPH_PROJECT": "Acme"}):
            a.ollama.embed.return_value = [0.3] * 1024
            svc = EmbeddingService.for_project()
            try:
                assert svc.code_model_id == DEFAULT_TEXT_MODEL
                assert svc.code_backend_ready()
                assert svc.embed_code("def f(): pass") == [0.3] * 1024
            finally:
                svc.close()


class TestComposeServiceModel:
    """Owner decision 6: the code_embed SERVICE's model follows its backend —
    an ``ollama``-backend service (CPU / AMD-GPU hosts) asks Ollama for the
    tier table's model, never the hardcoded CodeSage Ollama does not serve."""

    def test_amd_gpu_install_config_writes_an_ollama_served_service_model(self):
        sys.path.insert(0, str(REPO_ROOT))
        import install  # noqa: PLC0415
        from vco_lib.compose_env import compose_substitution_env

        cfg = dict(install.EMBEDDING_CONFIGS["cpu"])        # the AMD route's profile shape
        install._apply_tier_overrides(cfg, code_pick=install._CODE_BACKEND_QWEN3,
                                      kg_pick=install._KG_BACKEND_QWEN3)
        cfg["gpu_vendor"] = "amd"
        env = compose_substitution_env(cfg)
        assert env["CODE_EMBED_BACKEND"] == "ollama"
        assert env["CODE_EMBED_MODEL"] == cfg["code_model"] == DEFAULT_TEXT_MODEL
        assert "codesage" not in env["CODE_EMBED_MODEL"]

    def test_gpu_backend_keeps_codesage_and_openai_writes_no_model(self):
        from vco_lib.compose_env import compose_substitution_env

        assert compose_substitution_env({"code_backend": "gpu", "code_model": "codesage-large-v2"})[
            "CODE_EMBED_MODEL"] == "codesage/codesage-large-v2"
        assert "CODE_EMBED_MODEL" not in compose_substitution_env(
            {"code_backend": "openai", "code_model": "text-embedding-3-small"})
        cpu = compose_substitution_env({"code_backend": "ollama", "code_model": _JINA})
        assert cpu["CODE_EMBED_MODEL"] == _JINA

    def test_the_compose_file_substitutes_the_model(self):
        """Red on 2cc19a9c: docker-compose.yml hardcoded the CodeSage id."""
        import yaml

        doc = yaml.safe_load((REPO_ROOT / "infrastructure" / "docker-compose.yml").read_text())
        env = doc["services"]["code_embed"]["environment"]
        assert env["CODE_EMBED_MODEL"] == "${CODE_EMBED_MODEL:-codesage/codesage-large-v2}"


# ═══════════════════════════ the analyzer's fill ═══════════════════════════


class TestAnalyzerFill:
    def test_recorded_slot_missing_is_refused_not_422(self, launcher_db, monkeypatch):
        _stamp_record(launcher_db, DEFAULT_TEXT_MODEL, 1024)
        monkeypatch.setattr(cem, "_declared_slots", lambda name, url: {"codesage_embed"})
        plan = cem.prepare_analyzer_run("Acme", scoped=False, force_recreate=False)
        assert plan.refusal and "qwen3_embed" in plan.refusal

    def test_a_selection_is_filled_automatically_and_additively(self, launcher_db, monkeypatch, capsys):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, code_embed_backend_override="qwen3")
        monkeypatch.setattr(cem, "_declared_slots", lambda name, url: {"codesage_embed"})
        monkeypatch.setattr(cem, "_object_count", lambda name, url: 10)
        added = []
        monkeypatch.setattr("vco_lib.weaviate_schema.add_named_vector_slot",
                            lambda name, slot, **kw: added.append((name, slot.name)) or "created")
        plan = cem.prepare_analyzer_run("Acme", scoped=False, force_recreate=False)
        assert not plan.refusal and plan.filling and plan.rewalk
        assert (plan.model, plan.role, plan.record) == (DEFAULT_TEXT_MODEL, "write", CODESAGE_RECORD)
        assert {n for n, _ in added} == {f"Acme_{b}" for b in cem._CODE_BASENAMES}
        out = capsys.readouterr().out
        assert "filling up to 50 object(s)" in out and "existing vectors are kept" in out
        assert "cost-bearing" not in out
        scoped = cem.prepare_analyzer_run("Acme", scoped=True, force_recreate=False)
        assert scoped.filling and not scoped.rewalk

    def test_settle_flips_only_when_the_slot_is_complete(self, launcher_db):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        half = FakeClient({"Acme_CodeFunction": FakeColl([{"qwen3_embed": [1.0]}, {"codesage_embed": [1.0]}])})
        assert cem.settle_fill("Acme", DEFAULT_TEXT_MODEL, client_factory=lambda: half) == "filling"
        row = _row(launcher_db)
        assert row["embedding_model"] == DEFAULT_CODE_MODEL
        assert row["config"]["code_model_fill"]["filled"] == 1
        assert row["config"]["code_model_fill"]["total"] == 2
        full = FakeClient({"Acme_CodeFunction": FakeColl([{"qwen3_embed": [1.0]}] * 2)})
        assert cem.settle_fill("Acme", DEFAULT_TEXT_MODEL, client_factory=lambda: full) == "complete"
        row = _row(launcher_db)
        assert (row["embedding_model"], row["embedding_dim"]) == (DEFAULT_TEXT_MODEL, 1024)
        assert "code_model_fill" not in row["config"]

    def test_finish_settles_a_whole_run_and_hands_a_scoped_one_to_the_full_walk(
            self, launcher_db, monkeypatch):
        plan = cem.AnalyzerRunPlan(record=CODESAGE_RECORD, model=DEFAULT_TEXT_MODEL,
                                   target=DEFAULT_TEXT_MODEL, filling=True, rewalk=True)
        full = FakeClient({"Acme_CodeClass": FakeColl([{"qwen3_embed": [1.0]}])})
        assert cem.finish_analyzer_run("Acme", plan, None, scoped=False,
                                       client_factory=lambda: full) == "complete"
        spawned = []
        monkeypatch.setattr(cem, "spawn_fill_walk",
                            lambda root, prefix, **kw: spawned.append((root, prefix)) or "launched")
        assert cem.finish_analyzer_run("Acme", plan, None, scoped=True, repo_root="/r") == "launched"
        assert spawned == [("/r", "Acme")]

    def test_the_bundle_update_owes_the_fill_to_the_resync_driver(self, launcher_db, monkeypatch):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, code_embed_backend_override="qwen3")
        from vco_lib import codegraph_resync

        assert cem.fill_owed("Acme")
        assert codegraph_resync.CODE_MODEL_FILL_OWED in codegraph_resync._one_time_audits_owed(
            "Acme", None, False)
        _state(launcher_db, code_embed_backend_override="codesage")
        assert not cem.fill_owed("Acme")


class TestOnlyNeededReembeds:
    """Same model + same content → SKIP; a fill → EMBED only rows whose target
    slot is empty (the P299-B5 vector-aware fingerprint on the write slot)."""

    @staticmethod
    def _decide(slot, stored_vectors, stored_hash="h", content_hash="h"):
        from vco_lib import codegraph_guards as guards

        obj = types.SimpleNamespace(properties={"content_hash": stored_hash, "embed_revision": 9},
                                    vector=stored_vectors)
        coll = types.SimpleNamespace(query=types.SimpleNamespace(
            fetch_object_by_id=lambda uid, **kw: obj))
        fp = guards.read_object_fingerprint(coll, "u", "embed_revision", vector_slot=slot)
        rev = 0 if fp.get("vector_present") is False else fp["embed_revision"]
        return guards.classify_row(fp["content_hash"], rev, content_hash,
                                   current_revision=9, floor_revision=1)

    def test_unchanged_row_same_model_is_skipped(self):
        from vco_lib.codegraph_guards import RowAction

        slot = resolve(record=CODESAGE_RECORD, role="write").slot
        assert self._decide(slot, {"codesage_embed": [0.1]}) is RowAction.SKIP

    def test_a_fill_embeds_only_rows_missing_the_target_slot(self):
        from vco_lib.codegraph_guards import RowAction

        slot = resolve(state={"code_embed_backend_override": "qwen3"},
                       record=CODESAGE_RECORD, role="write").slot
        assert self._decide(slot, {"codesage_embed": [0.1]}) is RowAction.EMBED
        assert self._decide(slot, {"codesage_embed": [0.1], "qwen3_embed": [0.2]}) is RowAction.SKIP

    def test_stale_content_is_reembedded_even_when_the_slot_is_filled(self):
        from vco_lib.codegraph_guards import RowAction

        slot = resolve(record=CODESAGE_RECORD, role="write").slot
        assert self._decide(slot, {"codesage_embed": [0.1]}, content_hash="changed") is RowAction.EMBED


class TestSwitchingAndUpgrade:
    def test_switching_back_to_a_complete_slot_is_an_instant_flip(self, launcher_db, monkeypatch):
        _stamp_record(launcher_db, DEFAULT_TEXT_MODEL, 1024)       # now on qwen3 …
        _state(launcher_db, code_embed_backend_override="codesage")  # … back to codesage
        full = FakeClient({"Acme_CodeFunction": FakeColl([{"codesage_embed": [1.0], "qwen3_embed": [1.0]}])})
        monkeypatch.setattr(cem, "_default_client", lambda: full)
        with patch.object(EmbeddingService, "for_project", side_effect=AssertionError("no embedding")):
            assert cem.reconcile_folder_code_embedding(launcher_db.parent / "acme") == "complete"
        assert cem.read_code_model_record("Acme") == CODESAGE_RECORD

    def test_stale_pin_on_upgrade_is_applied_additively_nothing_dropped(self, launcher_db, monkeypatch):
        """A pin left from the years nothing read it meets a graph indexed
        before v0.2.102 (no record): the update stamps the record off live data
        and starts the fill — the existing vectors stay and keep serving."""
        _state(launcher_db, code_embed_backend_override="qwen3")
        monkeypatch.setattr(cem, "_sample_vectors", lambda name, url: {"codesage_embed": 2048})
        partial = FakeClient({"Acme_CodeFunction": FakeColl([{"codesage_embed": [1.0]}])})
        monkeypatch.setattr(cem, "_default_client", lambda: partial)
        folder = launcher_db.parent / "acme"
        assert cem.reconcile_folder_code_embedding(folder) == "filling"
        row = _row(launcher_db)
        assert row["embedding_model"] == DEFAULT_CODE_MODEL         # searches stay on codesage
        assert row["config"]["code_model_fill"]["target"] == DEFAULT_TEXT_MODEL
        assert not (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()  # no nag row


# ═══════════════════════════ OpenAI: two consents ═══════════════════════════


class TestOpenAIEmbeddingsConsent:
    STATE = {"code_embed_backend_override": "openai", "code_embed_openai_model": "text-embedding-3-large"}

    def test_a_summaries_consent_never_grants_embeddings(self):
        assert not openai_consent.embeddings_consent_granted({"kg_summary_openai_consent": "true"}.get)

    def test_truthiness_and_the_derived_default(self, monkeypatch):
        for raw, want in (("true", True), ("1", True), ("YES", True), ("false", False), ("x", False)):
            assert openai_consent.embeddings_consent_granted({CONSENT: raw}.get) is want
        monkeypatch.setenv("ACTIVE_EMBEDDING", "openai")
        assert openai_consent.embeddings_consent_granted({}.get)      # explicit prior opt-in
        assert not openai_consent.embeddings_consent_granted({CONSENT: "false"}.get)
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        assert not openai_consent.embeddings_consent_granted({}.get)

    def test_an_absent_row_follows_the_profile_and_nothing_is_written(self, launcher_db, monkeypatch):
        """No default is written on the user's behalf: a written "false" would
        later override a fresh "Use OpenAI as the default embedding provider"
        opt-in (the full suite caught exactly that)."""
        from vco_lib.launcher_db_reader import read_app_state_value

        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        cem.reconcile_folder_code_embedding(launcher_db.parent / "acme")
        assert read_app_state_value(CONSENT) is None
        assert not openai_consent.embeddings_consent_granted()
        monkeypatch.setenv("ACTIVE_EMBEDDING", "openai")
        assert openai_consent.embeddings_consent_granted()

    def test_code_refusal_names_the_embeddings_toggle(self):
        res = resolve(state=self.STATE)
        msg = str(res.refusal)
        assert res.refusal is not None and "Allow OpenAI for embeddings" in msg and CONSENT in msg
        assert "kg_summary_openai_consent" not in msg
        assert resolve(state=dict(self.STATE, **{CONSENT: "true"})).refusal is None

    def test_a_refused_target_is_not_filled_and_the_record_keeps_serving(self, launcher_db, monkeypatch):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, **self.STATE)
        monkeypatch.setattr(cem, "ensure_slot_on_collections", lambda *a, **k: None)
        plan = cem.prepare_analyzer_run("Acme", scoped=False, force_recreate=False)
        assert not plan.refusal and not plan.filling and plan.model == DEFAULT_CODE_MODEL
        assert not cem.fill_owed("Acme")

    def test_without_a_record_a_refused_target_is_refused_loudly(self, launcher_db, monkeypatch):
        _state(launcher_db, **self.STATE)
        plan = cem.prepare_analyzer_run("Acme", scoped=False, force_recreate=False)
        assert plan.refusal and "Allow OpenAI for embeddings" in plan.refusal

    def test_with_consent_the_fill_states_count_and_cost_first(self, launcher_db, monkeypatch, capsys):
        _stamp_record(launcher_db, DEFAULT_CODE_MODEL, 2048)
        _state(launcher_db, **dict(self.STATE, **{CONSENT: "true"}))
        monkeypatch.setattr(cem, "ensure_slot_on_collections", lambda *a, **k: None)
        monkeypatch.setattr(cem, "_object_count", lambda name, url: 7)
        plan = cem.prepare_analyzer_run("Acme", scoped=False, force_recreate=False)
        assert plan.filling and plan.model == "text-embedding-3-large"
        out = capsys.readouterr().out
        assert "up to 35 object(s) through the OpenAI API (cost-bearing)" in out

    def test_every_code_leg_raises_without_consent(self, launcher_db):
        _state(launcher_db, **self.STATE)
        with _patched_adapters() as a, patch.dict(os.environ, {"CODE_GRAPH_PROJECT": ""}):
            svc = EmbeddingService.for_project()
            try:
                assert svc.code_vector_slot == "openai_code_embed"
                for call in (lambda: svc.embed_code("x"), lambda: svc.embed_code_batch(["x"]),
                             lambda: svc.embed_code_query("x")):
                    with pytest.raises(NoEmbeddingBackendError, match="Allow OpenAI for embeddings"):
                        call()
                a.openai.embed.assert_not_called()
                a.openai.embed_batch.assert_not_called()
            finally:
                svc.close()

    def test_the_kg_text_openai_slots_go_through_the_same_consent(self, launcher_db, monkeypatch):
        """KG text: an OpenAI active profile with the toggle explicitly off is
        refused (naming the toggle); the openai_text_embed secondary is
        skipped. With the toggle on both run."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "openai")
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
        _state(launcher_db, **{CONSENT: "false"})
        with _patched_adapters(text_model=None) as a, patch.dict(os.environ, {"CODE_GRAPH_PROJECT": ""}), \
                patch("vco_lib.openai_key.resolve_openai_api_key", return_value="sk-test"):
            svc = EmbeddingService.for_project()
            try:
                assert not svc.text_backend_ready()
                with pytest.raises(NoEmbeddingBackendError, match="Allow OpenAI for embeddings"):
                    svc.embed_text("hello")
                a.openai.embed.assert_not_called()
            finally:
                svc.close()


# ═══════════════════ every decision point → the one resolver ═══════════════════


class TestEveryCallerUsesTheResolver:
    SENTINEL = "sentinel-code-model:1"

    @pytest.fixture
    def spy(self, monkeypatch):
        calls = []

        def fake(**kw):
            calls.append(kw)
            return cem.CodeModelResolution(self.SENTINEL, "recorded", self.SENTINEL, "default",
                                           None, False, role=kw.get("role", "query"))
        monkeypatch.setattr(cem, "resolve_code_model", fake)
        return calls

    def test_for_project(self, spy):
        with _patched_adapters():
            svc = EmbeddingService.for_project(codegraph_prefix="Acme", code_model_role="write")
            try:
                assert svc.code_model_id == self.SENTINEL
            finally:
                svc.close()
        assert spy[-1]["codegraph_prefix"] == "Acme" and spy[-1]["role"] == "write"

    def test_code_truncation_default(self, spy):
        from weaviate_mcp import code_truncation

        assert code_truncation._default_code_model() == self.SENTINEL
        assert spy[-1]["role"] == "write"

    def test_rl_telemetry_label(self, spy, monkeypatch):
        from weaviate_mcp import rl_enrichment

        monkeypatch.setattr(rl_enrichment.server, "_get_embedding_service", lambda: None)
        assert rl_enrichment._resolve_code_embedding_triple("codesage_embed")[2] == self.SENTINEL

    def test_for_project_callers_pass_their_graph_and_role(self, monkeypatch):
        seen = []

        def fake_for_project(cls, *a, **kw):
            seen.append((kw.get("codegraph_prefix"), kw.get("code_model_role", "query")))
            raise NoEmbeddingBackendError("stop", attempted_backends=[], error_per_backend={},
                                          capture=False)
        monkeypatch.setattr(EmbeddingService, "for_project", classmethod(fake_for_project))
        from vco_lib import codegraph_resync

        assert codegraph_resync._active_code_slot("Acme") is None
        from weaviate_mcp import embeddings, server

        monkeypatch.setattr(server, "CODE_GRAPH_PROJECT", "Mcp", raising=False)
        monkeypatch.setattr(server, "_cached_embed_service", None)
        monkeypatch.setattr(server, "_embed_service_construction_failed_at", -1e18)
        embeddings._get_embedding_service()
        sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))
        import query_code_graph as qcg  # noqa: PLC0415

        monkeypatch.setattr(qcg, "_embedding_codegraph_prefix", "Cli")
        monkeypatch.setattr(qcg, "_cached_embedding_service", None)
        qcg._get_or_create_embedding_service()
        from vco_lib import embedding_enrichment as ee

        monkeypatch.setattr(ee, "_http_get_schema",
                            lambda c, u: {"vectorConfig": {"qwen3_embed": {}, "codesage_embed": {}}})
        with pytest.raises(NoEmbeddingBackendError):
            ee.enrich_collection_vectors("Acme_CodeFunction", "qwen3_embed")
        assert seen == [("Acme", "query"), ("Mcp", "query"), ("Cli", "query"), ("Acme", "write")]

    def test_enrichment_names_the_remedy_that_adds_a_code_slot(self, monkeypatch):
        from vco_lib import embedding_enrichment as ee

        monkeypatch.setattr(ee, "_http_get_schema",
                            lambda c, u: {"vectorConfig": {"codesage_embed": {}}})
        with pytest.raises(ee.SlotNotInSchemaError) as exc:
            ee.enrich_collection_vectors("Acme_CodeFunction", "qwen3_embed")
        msg = str(exc.value)
        assert "code-graph-analyze . --project Acme" in msg and "migrate-collections" not in msg

    def test_analyzer_main_refuses_with_exit_6_and_passes_the_plan(self, tmp_path):
        proc, log = _run_analyzer(tmp_path, refusal="slot missing", rewalk=False)
        assert proc.returncode == 6, proc.stderr[-2000:]
        assert "slot missing" in proc.stderr and not log.exists()
        proc, log = _run_analyzer(tmp_path, refusal="", rewalk=True)
        assert proc.returncode == 0, proc.stderr[-2000:]
        rec = json.loads(log.read_text())
        assert rec["for_project"] == {"codegraph_prefix": "Acme", "code_model_role": "write"}
        assert rec["prepare"] == {"scoped": False, "force_recreate": False}
        assert rec["finished"] == {"scoped": False} and rec["force_rewalk"] is True


# ═══════════════════════════ helpers ═══════════════════════════


class _patched_adapters:
    """Patch the three adapters ``for_project`` probes; yields the mocks."""

    def __init__(self, *, code_ready=True, text_model="qwen3-embedding:0.6b"):
        self.code_ready = code_ready
        self.text_model = text_model

    def __enter__(self):
        from vco_lib.embedding_providers import CodeEmbedAdapter, OllamaAdapter, OpenAIAdapter
        from vco_lib.embedding_providers.openai import ValidationResult

        self.ollama = MagicMock(spec=OllamaAdapter)
        self.ollama.is_reachable.return_value = True
        self.ollama.list_models.return_value = []
        self.code = MagicMock(spec=CodeEmbedAdapter)
        self.code.is_reachable.return_value = self.code_ready
        self.openai = MagicMock(spec=OpenAIAdapter)
        self.openai.validate.return_value = ValidationResult(valid=True, reason=None)
        env = {"EMBEDDING_MODEL": self.text_model} if self.text_model else {}
        self._ps = [patch("vco_lib.embedding_service.OllamaAdapter", return_value=self.ollama),
                    patch("vco_lib.embedding_service.TruncationAwareOllamaAdapter",
                          return_value=self.ollama, create=True),
                    patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=self.code),
                    patch("vco_lib.embedding_service.OpenAIAdapter", return_value=self.openai),
                    patch.dict(os.environ, env)]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._ps):
            p.stop()
        return False


def _run_analyzer(tmp_path: Path, *, refusal: str, rewalk: bool):
    from tests.common.child_env import child_env

    tmp_path = tmp_path / ("fill" if rewalk else "refuse")
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n")
    log = tmp_path / "log.json"
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f"""\
        import json, sys, importlib.util
        spec = importlib.util.spec_from_file_location("acg", r"{_ANALYZER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rec = {{}}
        def prepare(prefix, *, scoped, force_recreate, **kw):
            rec["prepare"] = {{"scoped": scoped, "force_recreate": force_recreate}}
            return mod._cem.AnalyzerRunPlan(record=None, model="m", rewalk={rewalk!r},
                                            refusal={refusal!r})
        mod._cem.prepare_analyzer_run = prepare
        def finish(p, plan, svc, *, scoped, **kw):
            rec["finished"] = {{"scoped": scoped}}
        mod._cem.finish_analyzer_run = finish
        C = mod.CodeGraphAnalyzer
        C.connect = lambda self: (setattr(self, "client", object()) or True)
        C.close = lambda self: None
        C.create_collections = lambda self, force=False, *, repo_path=None: None
        C.create_cross_references = lambda self, changed_files=None: {{"calls": 0, "extends": 0, "imports": 0}}
        def analyze(self, *a, **kw):
            rec["force_rewalk"] = self.force_rewalk
            return {{"modules": 1, "classes": 0, "functions": 1, "apis": 0, "files_analyzed": 1,
                    "files_skipped": 0, "insert_errors": 0, "stale_pruned": 0, "prune_failures": 0,
                    "chunk_plan_failures": 0}}
        C.analyze_repository = analyze
        class _Svc:
            code_vector_slot = "codesage_embed"; code_model_id = "m"; code_dim = 8
            text_vector_slot = "qwen3_embed"; text_model_id = "t"; text_dim = 8
            def code_backend_ready(self): return True
            def text_backend_ready(self): return True
            def close(self): return None
        def fp(cls, *a, **kw):
            rec["for_project"] = {{k: v for k, v in kw.items() if k != "code_model_record"}}
            return _Svc()
        mod.EmbeddingService.for_project = classmethod(fp)
        sys.argv = ["analyze_code_graph.py", r"{repo}", "--project", "Acme",
                    "--deferral-root", r"{repo}"]
        code = mod.main()
        if "for_project" in rec:
            open(r"{log}", "w").write(json.dumps(rec))
        sys.exit(code)
    """))
    proc = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                          timeout=120, env=child_env(VCT_JOERN_AVAILABLE="0"))
    return proc, log
