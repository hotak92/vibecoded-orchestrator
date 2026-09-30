# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 AD-6 — the Ollama model set is exactly what VCO uses.

The matrix lives in ``tests/fixtures/embedding_pull_plan_cases.json``: every
row builds the REAL install profile (``install.EMBEDDING_CONFIGS`` + the tier
overrides install applies) and asserts the EXACT model set — nothing extra,
nothing missing. Further tests pin the machine view (launcher.db dual-flag
cascade, recorded profile), the replayed ``embedding_mode``, the one shared
secondary-embedder rule, and the no-silent-switch code backend.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import embedding_pull_plan as epp  # noqa: E402

_CASES = json.loads((REPO_ROOT / "tests/fixtures/embedding_pull_plan_cases.json").read_text())


def _config(case: dict) -> dict:
    config = dict(install.EMBEDDING_CONFIGS[case["profile"]])
    ov = case.get("overrides") or {}
    if ov:
        install._apply_tier_overrides(
            config,
            code_pick=ov.get("code_model", config["code_model"]),
            kg_pick=ov.get("text_model", config["text_model"]),
        )
    return config


@pytest.mark.parametrize("case", _CASES["cases"], ids=[c["name"] for c in _CASES["cases"]])
def test_matrix_row_is_the_exact_model_set(case):
    config = _config(case)
    pp = epp.plan(
        kg_active=config["text_model"],
        dual_write_all=case.get("dual_write_all", False),
        dual_arctic_secondary=case.get("dual_arctic_secondary", False),
        code_backend=config["code_backend"],
        code_model=config["code_model"],
        capability_tier=_CASES["tiers"][case["tier"]],
        profile_override=config.get("inference_models_override"),
        code_embed_reachable=case.get("code_embed_reachable"),
    )
    assert list(pp.embedding) == case["embedding"]
    assert list(pp.inference) == case["inference"]
    assert pp.code_backend_unavailable is case["code_backend_unavailable"]
    assert list(pp.models) == case["embedding"] + [
        m for m in case["inference"] if m not in case["embedding"]
    ]


def test_matrix_covers_every_profile_both_code_embed_states_and_dual():
    profiles = {c["profile"] for c in _CASES["cases"]}
    assert profiles == set(install.EMBEDDING_CONFIGS)
    assert {c.get("code_embed_reachable") for c in _CASES["cases"]} >= {True, False}
    assert any(c.get("dual_arctic_secondary") and c.get("dual_write_all") for c in _CASES["cases"])


def test_profiles_no_longer_declare_a_pull_list():
    """The pull list has ONE home (the plan) — a profile states no models to pull."""
    for name, cfg in install.EMBEDDING_CONFIGS.items():
        assert "embedding_models" not in cfg, name


def test_tier_override_no_longer_grows_a_pull_list():
    config = dict(install.EMBEDDING_CONFIGS["low_resource"])
    install._apply_tier_overrides(config, code_pick="qwen3-embedding:0.6b",
                                  kg_pick=config["text_model"])
    assert "embedding_models" not in config
    assert config["code_model"] == "qwen3-embedding:0.6b" and config["code_dims"] == 1024


# ── one rule for "written" and "pulled" ────────────────────────────────────


@pytest.mark.parametrize("active", ["qwen3-embedding:0.6b", "snowflake-arctic-embed2:latest",
                                    "text-embedding-3-small"])
@pytest.mark.parametrize("write_all,arctic", [(False, False), (True, False), (True, True), (False, True)])
def test_embedding_service_write_set_uses_the_plan_rule(monkeypatch, active, write_all, arctic):
    from vco_lib import embedding_service as es

    monkeypatch.setattr(es, "resolve_active_text_model_id", lambda: active)
    monkeypatch.setattr("vco_lib.openai_key.resolve_openai_api_key", lambda *a, **k: "")
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true" if write_all else "false")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true" if arctic else "false")
    written = es.configured_text_models()
    assert written[0] == active
    assert tuple(written[1:]) == epp.kg_secondary_models(
        active, write_all=write_all, arctic_secondary=arctic)


# ── the machine view (recorded profile × launcher.db) ──────────────────────


def _record(root: Path, profile: str, tier=("qwen3.5:0.8b",)) -> None:
    epp.record_profile(root, install.EMBEDDING_CONFIGS[profile], list(tier))


def test_no_launcher_db_means_no_dual_opt_in(tmp_path):
    _record(tmp_path, "gpu")
    pp = epp.plan_from_machine(tmp_path, tmp_path / "absent.db")
    assert pp.models == ("qwen3-embedding:0.6b", "qwen3.5:0.8b")


def test_no_record_and_no_db_is_a_typed_refusal(tmp_path):
    with pytest.raises(epp.PlanUnavailable):
        epp.plan_from_machine(tmp_path, tmp_path / "absent.db")


def test_a_project_dual_opt_in_adds_the_second_kg_embedder(tmp_path):
    _record(tmp_path, "gpu")
    db = make_launcher_db(
        tmp_path,
        projects=[{"project_id": "p1", "name": "Alpha", "folder_path": tmp_path / "a"},
                  {"project_id": "p2", "name": "Beta", "folder_path": tmp_path / "b"}],
        app_state={"embedding.active_profile": "qwen3"},
        module_settings=[("p2", "orchestrator-core", "dual_embedding_write_all_slots", "true"),
                         ("p2", "orchestrator-core", "dual_embedding_arctic_secondary", "true")],
    )
    pp = epp.plan_from_machine(tmp_path, db)
    assert pp.embedding == ("qwen3-embedding:0.6b", "snowflake-arctic-embed2:latest")
    assert pp.inference == ("qwen3.5:0.8b",)


def test_without_any_opt_in_the_db_adds_nothing(tmp_path):
    _record(tmp_path, "cpu")
    db = make_launcher_db(
        tmp_path,
        projects=[{"project_id": "p1", "name": "Alpha", "folder_path": tmp_path / "a"}],
        app_state={"embedding.active_profile": "qwen3"},
        module_settings=[("p1", "orchestrator-core", "dual_embedding_arctic_secondary", "true")],
    )
    pp = epp.plan_from_machine(tmp_path, db)
    assert pp.embedding == ("qwen3-embedding:0.6b",
                            "unclemusclez/jina-embeddings-v2-base-code:latest")


def test_record_written_by_install_is_what_the_launcher_reads(tmp_path):
    cfg = dict(install.EMBEDDING_CONFIGS["cpu"])
    install._apply_tier_overrides(cfg, code_pick="qwen3-embedding:0.6b", kg_pick=cfg["text_model"])
    pp = epp.plan_for_install(tmp_path, cfg, capability_tier=["gemma4:e4b", "qwen3.5:0.8b"],
                              launcher_db=tmp_path / "absent.db")
    # a multi-rung tier (an older run's record) collapses to its ONE model
    assert pp.models == ("qwen3-embedding:0.6b", "gemma4:e4b")
    assert epp.plan_from_machine(tmp_path, tmp_path / "absent.db") == pp


def test_install_probes_code_embed_only_for_codesage(tmp_path):
    with mock.patch("vco_lib.ollama_pull.code_embed_state", return_value="down") as probe:
        pp = epp.plan_for_install(tmp_path, install.EMBEDDING_CONFIGS["gpu"],
                                  capability_tier=["qwen3.5:0.8b"],
                                  code_embed_url="http://localhost:11440",
                                  launcher_db=tmp_path / "absent.db")
    assert probe.call_count == 1 and probe.call_args.args == ("http://localhost:11440",)
    assert pp.code_backend_unavailable and pp.embedding == ("qwen3-embedding:0.6b",)
    with mock.patch("vco_lib.ollama_pull.code_embed_state") as probe:
        epp.plan_for_install(tmp_path, install.EMBEDDING_CONFIGS["cpu"],
                             capability_tier=[], launcher_db=tmp_path / "absent.db")
    probe.assert_not_called()


def test_cli_show_json(tmp_path, capsys):
    _record(tmp_path, "low_resource")
    rc = epp.main(["show", "--json", "--root", str(tmp_path),
                   "--launcher-db", str(tmp_path / "absent.db")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"]
    assert out["plan"]["models"] == ["snowflake-arctic-embed2:latest",
                                     "unclemusclez/jina-embeddings-v2-base-code:latest",
                                     "qwen3.5:0.8b"]


# ── replayed embedding_mode runs through the overrides → the plan ───────────


def test_replayed_mode_keeps_the_recorded_tier_picks(monkeypatch):
    monkeypatch.setattr(install, "_load_previous_choices", lambda: {
        "embedding_mode": {"value": "cpu", "code_model": "qwen3-embedding:0.6b",
                           "text_model": "qwen3-embedding:0.6b"}})
    recorded = []
    monkeypatch.setattr(install, "_record_install_choice", lambda *a, **k: recorded.append(a))
    args = mock.Mock(openai_key=None, low_resource=False, cpu_only=False)
    config = install._choose_embedding_config(mock.Mock(), args)
    assert config["code_model"] == "qwen3-embedding:0.6b" and config["code_dims"] == 1024
    assert recorded[0][2]["code_model"] == "qwen3-embedding:0.6b"
    pp = epp.plan(kg_active=config["text_model"], dual_write_all=False,
                  dual_arctic_secondary=False, code_backend=config["code_backend"],
                  code_model=config["code_model"], capability_tier=["qwen3.5:0.8b"],
                  profile_override=config.get("inference_models_override"))
    assert "unclemusclez/jina-embeddings-v2-base-code:latest" not in pp.models


# ── no silent embedder switch (L1-F13) ─────────────────────────────────────


def _code_down_service(monkeypatch, tmp_path, *, opt_in: bool):
    from vco_lib import embedding_service as es

    for k in ("CODE_EMBED_MODEL", "CODE_EMBED_BACKEND", "EMBEDDING_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / ".vct"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / ".claude-home"))
    if opt_in:
        monkeypatch.setenv(es.CODE_EMBED_ALLOW_FALLBACK_ENV, "1")
    else:
        monkeypatch.delenv(es.CODE_EMBED_ALLOW_FALLBACK_ENV, raising=False)
    monkeypatch.setattr("vco_lib.openai_key.resolve_openai_api_key", lambda *a, **k: "")
    ollama = mock.MagicMock(spec=es.OllamaAdapter)
    ollama.is_reachable.return_value = True
    ollama.list_models.return_value = [{"name": "qwen3-embedding:0.6b"}]
    ollama.embed.return_value = [0.1] * 4
    code = mock.MagicMock(spec=es.CodeEmbedAdapter)
    code.is_reachable.return_value = False
    code.base_url = "http://localhost:11440"
    oa = mock.MagicMock(spec=es.OpenAIAdapter)
    emitted = []
    monkeypatch.setattr("vco_lib.deferral_emit.emit", lambda folder, entry, **k: emitted.append(entry))
    monkeypatch.setattr("vco_lib.deferral_emit.resolve_conditions", lambda *a, **k: 0)
    with mock.patch.object(es, "OllamaAdapter", return_value=ollama), \
         mock.patch.object(es, "CodeEmbedAdapter", return_value=code), \
         mock.patch.object(es, "OpenAIAdapter", return_value=oa):
        svc = es.EmbeddingService.for_project(tmp_path)
    return es, svc, emitted, ollama


def test_code_embed_down_raises_typed_error_and_writes_no_row_at_construction(monkeypatch, tmp_path):
    """v0.2.100 W3R-04: construction runs at every session start while the
    container may still be loading its model — it must NOT write the ledger
    (the durable row is install.py step 7's, with its bounded wait)."""
    es, svc, emitted, ollama = _code_down_service(monkeypatch, tmp_path, opt_in=False)
    try:
        assert svc.code_model_id == "codesage-large-v2"      # not switched
        assert svc.code_vector_slot == "codesage_embed"
        assert emitted == []
        with pytest.raises(es.NoEmbeddingBackendError):
            svc.embed_code("def f(): pass")
        with pytest.raises(es.NoEmbeddingBackendError):
            svc.embed_code_batch(["def f(): pass"])
        ollama.embed.reset_mock()
        assert svc.embed_text("hello") == [0.1] * 4           # KG unaffected
    finally:
        svc.close()


def test_developer_opt_in_keeps_the_loud_fallback(monkeypatch, tmp_path, capsys):
    _es, svc, emitted, _o = _code_down_service(monkeypatch, tmp_path, opt_in=True)
    try:
        assert svc.code_vector_slot == "qwen3_embed"
        assert emitted == []
        assert "VCO_CODE_EMBED_ALLOW_FALLBACK=1" in capsys.readouterr().err
    finally:
        svc.close()
