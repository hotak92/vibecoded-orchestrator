# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-B2 — the code_embed container exists iff CodeSage is selected.

Owner rule 2026-10-10 (serving follows the model): CodeSage is served by its
own container; jina / qwen3 by Ollama; OpenAI by OpenAI. ONE decision
(``vco_lib.code_embed_serving.code_embed_in_stack``) drives install's compose
start, is projected as ``CODE_EMBED_IN_STACK`` into ``infrastructure/.env``
for the hub watchdog (Rust side pinned in ``infra_watchdog.rs`` tests) and
gates the session hook / boot wrapper plan (``service_lifecycle``). One case
per host class; NVIDIA >= 12 GB CodeSage and CPU-only keep working.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import code_embed_serving as ces  # noqa: E402
from vco_lib import compose_env  # noqa: E402

CODESAGE = "codesage/codesage-large-v2"
JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
QWEN3 = "qwen3-embedding:0.6b"


@pytest.fixture
def no_db(monkeypatch):
    """No launcher.db rows: only the install pick + env count."""
    from vco_lib import launcher_db_reader as ldr

    monkeypatch.setattr(ldr, "read_app_state_value", lambda key, db_path=None: None)
    monkeypatch.setattr(ldr, "list_codegraph_binding_rows", lambda: [])
    monkeypatch.delenv("CODE_EMBED_MODEL", raising=False)


def _marker(cfg: dict) -> str:
    return compose_env.compose_substitution_env(cfg)[ces.CODE_EMBED_IN_STACK_KEY]


# --- the decision, per host class -------------------------------------------


def test_nvidia_12gb_codesage_install_keeps_the_container(no_db):
    cfg = {"has_gpu": True, "gpu_vendor": "nvidia", "code_backend": "gpu", "code_model": CODESAGE}
    assert _marker(cfg) == "true"


@pytest.mark.parametrize("model,vendor", [(QWEN3, "nvidia"), (QWEN3, "amd"), (JINA, "nvidia")])
def test_gpu_host_with_ollama_served_code_models_runs_no_container(no_db, model, vendor):
    cfg = {"has_gpu": True, "gpu_vendor": vendor, "code_backend": "ollama", "code_model": model}
    assert _marker(cfg) == "false"


def test_cpu_only_host_never_runs_the_container(no_db, monkeypatch):
    monkeypatch.setenv("CODE_EMBED_MODEL", CODESAGE)  # even if asked for CodeSage
    cfg = {"has_gpu": False, "gpu_vendor": "", "code_backend": "ollama", "code_model": JINA}
    assert _marker(cfg) == "false"


def test_a_graph_recorded_in_codesage_keeps_the_container_on_a_jina_install(monkeypatch):
    from vco_lib import launcher_db_reader as ldr

    monkeypatch.delenv("CODE_EMBED_MODEL", raising=False)
    monkeypatch.setattr(ldr, "read_app_state_value", lambda key, db_path=None: None)
    rows = [{"last_analyzed_at": "2026-10-01", "embedding_model": "codesage-large-v2", "config": {}}]
    monkeypatch.setattr(ldr, "list_codegraph_binding_rows", lambda: rows)
    cfg = {"has_gpu": True, "gpu_vendor": "nvidia", "code_backend": "ollama", "code_model": JINA}
    assert _marker(cfg) == "true"
    # …but a never-built seed's placeholder serves nothing.
    rows[0]["last_analyzed_at"] = None
    assert _marker(cfg) == "false"
    # A fill TARGET of CodeSage needs the container too.
    rows[0]["config"] = {"code_model_target": "codesage-large-v2"}
    assert _marker(cfg) == "true"


def test_a_preferences_pin_of_codesage_counts(monkeypatch):
    from vco_lib import launcher_db_reader as ldr

    monkeypatch.delenv("CODE_EMBED_MODEL", raising=False)
    monkeypatch.setattr(ldr, "read_app_state_value",
                        lambda key, db_path=None: "codesage" if key == "code_embed_backend_override" else None)
    monkeypatch.setattr(ldr, "list_codegraph_binding_rows", lambda: [])
    cfg = {"has_gpu": True, "gpu_vendor": "nvidia", "code_backend": "ollama", "code_model": QWEN3}
    assert _marker(cfg) == "true"


def test_a_partial_config_answers_from_the_one_decision(no_db):
    """v0.2.103 (diag 1d): no resolved code model is NOT "any GPU host runs
    the container" — the model is the backend's pairing, and only CodeSage is
    in the stack."""
    assert compose_env.code_embed_container_in_stack({"has_gpu": True}) is False
    assert compose_env.code_embed_container_in_stack(
        {"has_gpu": True, "code_backend": "ollama"}) is False
    assert compose_env.code_embed_container_in_stack(
        {"has_gpu": True, "code_backend": "gpu"}) is True
    assert compose_env.code_embed_container_in_stack({"has_gpu": False, "code_backend": "gpu"}) is False


def test_a_partial_config_still_counts_a_codesage_graph(monkeypatch):
    from vco_lib import launcher_db_reader as ldr

    monkeypatch.delenv("CODE_EMBED_MODEL", raising=False)
    monkeypatch.setattr(ldr, "read_app_state_value", lambda key, db_path=None: None)
    monkeypatch.setattr(ldr, "list_codegraph_binding_rows", lambda: [
        {"last_analyzed_at": "2026-10-01T00:00:00Z", "embedding_model": CODESAGE}])
    assert compose_env.code_embed_container_in_stack({"has_gpu": True}) is True


# --- the projected marker drives the session hook / boot wrapper plan --------


def test_the_plan_drops_code_embed_when_the_marker_says_so(tmp_path):
    from vco_lib import service_lifecycle as sl

    infra = tmp_path / "infrastructure"
    infra.mkdir()
    with mock.patch("vco_lib.python_exe.resolve_install_root", return_value=tmp_path):
        assert sl.code_embed_out_of_stack() is False  # no marker: pre-v0.2.102 behaviour
        base = sl.compose_services({})
        assert "code_embed" in base
        (infra / ".env").write_text("CODE_EMBED_BACKEND=ollama\nCODE_EMBED_IN_STACK=false\n")
        assert sl.code_embed_out_of_stack() is True
        assert "code_embed" not in sl.compose_services({})
        pol = [p for p in sl.container_policies({}) if p.service == "code_embed"]
        assert all((p.on_missing, p.on_zombie, p.on_stopped) == ("ignore",) * 3 for p in pol)
        (infra / ".env").write_text("CODE_EMBED_IN_STACK=true\n")
        assert "code_embed" in sl.compose_services({})


def test_write_infrastructure_env_persists_the_marker(no_db, tmp_path):
    ok, _msg = compose_env.write_infrastructure_env(
        tmp_path, {"has_gpu": True, "gpu_vendor": "nvidia", "code_backend": "ollama",
                   "code_model": QWEN3})
    assert ok
    assert compose_env.read_code_embed_in_stack(tmp_path) is False


# --- install's compose start follows the same decision -----------------------


def _drive_install(embed_config, identity=None):
    """Drive the production ``install._start_services`` through the identity-
    guard suite's harness, with THIS embed config and a stale image.

    The default identity is OURS (v0.2.103): with the guard suite's foreign
    identity, ``apply_recreate_guard`` dropped code_embed for its own reason
    and hid the field defect (the image-rebuild path re-adding
    ``--build --force-recreate code_embed`` on an Ollama-served tier)."""
    from tests import test_v0293_compose_identity_guard as harness

    if identity is None:
        identity = harness.containers.ComposeIdentity(
            "infrastructure", str(REPO_ROOT / "infrastructure"))

    case = harness.StartServicesIdentityGuardTests("test_foreign_owned_services_are_left_alone_and_ledgered")
    harness.StartServicesIdentityGuardTests.setUpClass()
    install = case.install
    real = install._start_services
    decisions = {
        "code_embed": {"action": install.ACTION_START, "probe": ""},
        "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
        "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
    }

    def _with_config(sysinfo, args, _cfg, **kw):
        return real(sysinfo, args, embed_config, **kw)

    with mock.patch.object(install, "_start_services", _with_config):
        cmd, out, _entries = case._drive(identity, decisions=decisions)
    return cmd, out


def test_install_starts_code_embed_for_codesage_only(no_db):
    cmd, _out = _drive_install({"code_model": CODESAGE, "code_backend": "gpu"})
    assert "code_embed" in cmd and "--build" in cmd
    for model in (QWEN3, JINA):
        cmd, out = _drive_install({"code_model": model, "code_backend": "ollama"})
        assert cmd, out  # compose still ran (weaviate / ollama recreate)
        assert "code_embed" not in cmd and "--build" not in cmd, (model, cmd)
        assert "[rebuild]" not in out


def test_install_builds_no_code_embed_under_a_foreign_identity_either(no_db):
    from tests import test_v0293_compose_identity_guard as harness

    foreign = harness.containers.ComposeIdentity("vco", "/x", "docker-compose.yml")
    cmd, _out = _drive_install({"code_model": QWEN3, "code_backend": "ollama"}, foreign)
    assert "code_embed" not in cmd and "--build" not in cmd


# --- review F2: the resync / fill spawn gate is serving-aware ---------------


def _resolved(monkeypatch, model, carry=None):
    from vco_lib import code_embed_model as cem

    monkeypatch.setattr(cem, "resolve_code_model", lambda **kw: mock.Mock(model=model, carry=carry))


def test_cpu_only_jina_graph_passes_the_gate_without_any_container(monkeypatch):
    from vco_lib import codegraph_resync as cr
    from vco_lib import ollama_pull

    _resolved(monkeypatch, JINA)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: False)  # no container
    monkeypatch.setattr(ollama_pull, "verify_present", lambda base, models, **k: (list(models), []))
    assert cr.code_backend_down("Acme") == (None, False)
    assert cr.walk_uses_container("Acme") is False


def test_jina_graph_defers_when_ollama_lacks_the_model(monkeypatch):
    from vco_lib import codegraph_resync as cr
    from vco_lib import ollama_pull

    _resolved(monkeypatch, JINA)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(ollama_pull, "verify_present", lambda base, models, **k: ([], list(models)))
    label, used = cr.code_backend_down("Acme")
    assert "Ollama" in label and JINA in label and used is False


def test_codesage_graph_still_needs_the_container(monkeypatch):
    from vco_lib import codegraph_resync as cr

    _resolved(monkeypatch, "codesage-large-v2")
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: False)
    label, used = cr.code_backend_down("Acme")
    assert "code_embed container" in label and used is True
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    assert cr.code_backend_down("Acme") == (None, True)


def test_a_fill_carrying_codesage_onto_jina_probes_both(monkeypatch):
    from vco_lib import codegraph_resync as cr
    from vco_lib import ollama_pull

    _resolved(monkeypatch, JINA, carry="codesage-large-v2")
    monkeypatch.setattr(ollama_pull, "verify_present", lambda base, models, **k: (list(models), []))
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: False)
    label, used = cr.code_backend_down("Acme")
    assert "code_embed container" in label and used is True


def test_openai_graph_needs_no_probe(monkeypatch):
    from vco_lib import codegraph_resync as cr

    _resolved(monkeypatch, "text-embedding-3-small")
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: pytest.fail("no probe"))
    assert cr.code_backend_down("Acme") == (None, False)


def test_the_spawn_gate_uses_the_serving_aware_probe(monkeypatch, tmp_path):
    """Wiring: ``spawn_background_resync`` defers with the backend's label
    from :func:`code_backend_down` (red pre-fix: it probed the container)."""
    from vco_lib import codegraph_resync as cr

    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_backend_down",
                        lambda name, url=None: ("Ollama at http://x (model m not pulled)", False))
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: pytest.fail("no spawn"))
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")
    res = cr.spawn_background_resync(tmp_path, "Acme", python_exe=sys.executable,
                                     check_owed=False)
    assert res.status == "deferred"
    assert "Ollama at http://x" in res.message


# --- W6-B2 last round: a selection change brings the container up/down ------


def _infra(tmp_path, *, gpu: "str | None", backend="ollama", model=QWEN3, marker="false"):
    infra = tmp_path / "infrastructure"
    infra.mkdir(exist_ok=True)
    lines = [f"CODE_EMBED_BACKEND={backend}", f"CODE_EMBED_MODEL={model}",
             f"CODE_EMBED_IN_STACK={marker}", "CODE_EMBED_DOCKERFILE=Dockerfile.cuda"]
    if gpu is not None:
        lines.append(f"CODE_EMBED_HOST_GPU={gpu}")
    (infra / ".env").write_text("\n".join(lines) + "\n")
    return infra


def _refresh(infra, *, pin=None, default=QWEN3, rows=()):
    state = {"default_code_embedding": default, "code_embed_backend_override": pin}
    return compose_env.refresh_code_embed_marker(
        infra, app_state=lambda key: state.get(key), binding_rows=list(rows), environ={})


def test_selecting_codesage_on_a_gpu_host_turns_the_marker_on_with_codesage_pairing(tmp_path):
    infra = _infra(tmp_path, gpu="true")
    out = _refresh(infra, pin="codesage")
    assert out == {"ok": True, "in_stack": True, "changed": True}
    keys = compose_env._read_env_keys(infra)
    assert keys["CODE_EMBED_IN_STACK"] == "true"
    assert (keys["CODE_EMBED_BACKEND"], keys["CODE_EMBED_MODEL"]) == ("gpu", CODESAGE)
    assert keys["CODE_EMBED_DOCKERFILE"] == "Dockerfile.cuda"  # other lines kept
    assert _refresh(infra, pin="codesage")["changed"] is False  # idempotent


def test_a_codegraph_tab_target_of_codesage_counts_too(tmp_path):
    infra = _infra(tmp_path, gpu="true")
    rows = [{"last_analyzed_at": 1, "embedding_model": QWEN3,
             "config": {"code_model_target": "codesage-large-v2"}}]
    assert _refresh(infra, rows=rows)["in_stack"] is True


def test_deselecting_drops_only_the_marker(tmp_path):
    infra = _infra(tmp_path, gpu="true", backend="gpu", model=CODESAGE, marker="true")
    out = _refresh(infra, pin="jina", default=JINA)
    assert out["in_stack"] is False and out["changed"] is True
    keys = compose_env._read_env_keys(infra)
    assert keys["CODE_EMBED_IN_STACK"] == "false"
    # The pairing is left as it was: nothing about the container is touched.
    assert (keys["CODE_EMBED_BACKEND"], keys["CODE_EMBED_MODEL"]) == ("gpu", CODESAGE)


def test_a_non_gpu_host_never_runs_the_container(tmp_path):
    infra = _infra(tmp_path, gpu="false")
    assert _refresh(infra, pin="codesage")["in_stack"] is False
    assert compose_env._read_env_keys(infra)["CODE_EMBED_IN_STACK"] == "false"


def test_the_cli_runs_the_refresh(tmp_path, monkeypatch):
    infra = _infra(tmp_path, gpu="false")
    from vco_lib import launcher_db_reader as ldr

    monkeypatch.setattr(ldr, "read_app_state_value", lambda key, db_path=None: None)
    monkeypatch.setattr(ldr, "list_codegraph_binding_rows", lambda: [])
    assert compose_env._main(["refresh-code-embed-marker", "--infra-dir", str(infra)]) == 0


def test_every_selection_key_the_decision_reads_matches_the_hub_fingerprint():
    """The hub re-runs the refresh when launcher.db rows whose key contains
    ``code_emb`` change (vct-launcher-core ``code_model_inputs_fingerprint``) —
    so every app_state key the decision reads must match that pattern."""
    seen: list = []
    ces.code_models_in_use("", environ={}, app_state=lambda k: seen.append(k), binding_rows=[])
    assert seen and all("code_emb" in k for k in seen), seen
