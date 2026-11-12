# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 (owner ruling 2026-11-12) — the opt-out for the local KG-summary
model (``vco_lib.local_summary_model``).

Pinned, each consumer both ways:

* the setting's precedence: ``VCT_LOCAL_SUMMARY_MODEL`` → launcher.db
  ``app_state[kg_summary_local_model_enabled]`` → ON; an unrecognised env
  value is ignored, never read as either answer;
* the pull plan (the ONE home every pull path reads — install's
  ``plan_for_install``, the launcher's ``ensure`` CLI, the deferral retry):
  OFF → no text-generation model, embeddings untouched;
* the summary ladder: OFF → the local Ollama tier is skipped WITHOUT asking
  Ollama, a pinned ``ollama`` backend skips instead of 404-ing;
* removal: refused unless confirmed, switched OFF, not in the plan, and VCO
  owns the Ollama; only then exactly that model is deleted;
* ``vco doctor`` reports it; the low-disk remediation names the switch.

No live service: every Ollama/container/DB fact is injected or a temp file.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import doctor  # noqa: E402
from vco_lib import embedding_pull_plan as epp  # noqa: E402
from vco_lib import local_summary_model as lsm  # noqa: E402

TIER = "qwen3.5:9b"
EMBED = "qwen3-embedding:0.6b"


@pytest.fixture(autouse=True)
def _no_machine_db(monkeypatch, tmp_path):
    """Nothing reads the real launcher.db; no switch from the outer shell."""
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(tmp_path / "no-such-launcher.db"))
    monkeypatch.delenv(lsm.ENV_SWITCH, raising=False)


def _db(tmp_path: Path, value: str | None) -> Path:
    app_state = {"embedding.active_profile": "qwen3"}
    if value is not None:
        app_state[lsm.APP_STATE_KEY] = value
    return make_launcher_db(tmp_path, projects=[], app_state=app_state)


# ── the setting ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("env,pref,expected", [
    ({}, None, (True, "default")),
    ({}, "false", (False, "preferences")),
    ({}, "true", (True, "preferences")),
    ({"VCT_LOCAL_SUMMARY_MODEL": "off"}, "true", (False, "env")),
    ({"VCT_LOCAL_SUMMARY_MODEL": "ON"}, "false", (True, "env")),
    ({"VCT_LOCAL_SUMMARY_MODEL": "0"}, None, (False, "env")),
])
def test_precedence_env_then_preferences_then_on(env, pref, expected):
    s = lsm.resolve(env, preferences=lambda: pref)
    assert (s.enabled, s.source) == expected


def test_an_unrecognised_env_value_is_ignored_not_guessed(capsys):
    s = lsm.resolve({"VCT_LOCAL_SUMMARY_MODEL": "nope"}, preferences=lambda: "false")
    assert (s.enabled, s.source, s.invalid_env) == (False, "preferences", "nope")
    assert "ignored" in capsys.readouterr().err


def test_the_preference_is_read_from_the_given_launcher_db(tmp_path):
    assert lsm.resolve({}, db_path=_db(tmp_path, "false")).enabled is False
    assert lsm.resolve({}, db_path=tmp_path / "absent.db").enabled is True


# ── consumer 1: the pull plan ──────────────────────────────────────────────


def _plan(local: bool) -> epp.PullPlan:
    return epp.plan(kg_active="qwen3", dual_write_all=False, dual_arctic_secondary=False,
                    code_backend="openai", code_model="text-embedding-3-small",
                    capability_tier=[TIER], profile_override=None, local_summary_model=local)


def test_plan_off_drops_only_the_generation_model():
    on, off = _plan(True), _plan(False)
    assert on.inference == (TIER,)
    assert off.inference == ()
    assert off.embedding == on.embedding == (EMBED,)
    assert any("turned off" in r and TIER in r for r in off.rationale)


def _record(root: Path) -> None:
    epp.record_profile(root, install.EMBEDDING_CONFIGS["cpu"], [TIER])


def test_machine_plan_reads_the_preferences_toggle(tmp_path):
    _record(tmp_path)
    assert TIER in epp.plan_from_machine(tmp_path, _db(tmp_path, "true")).models
    (tmp_path / "launcher.db").unlink()
    off = epp.plan_from_machine(tmp_path, _db(tmp_path, "false"))
    assert off.inference == () and EMBED in off.embedding


def test_machine_plan_env_override_beats_the_toggle(tmp_path, monkeypatch):
    _record(tmp_path)
    db = _db(tmp_path, "false")
    monkeypatch.setenv(lsm.ENV_SWITCH, "on")
    assert epp.plan_from_machine(tmp_path, db).inference == (TIER,)
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    assert epp.plan_from_machine(tmp_path, tmp_path / "absent.db").inference == ()


def test_install_path_honours_the_switch(tmp_path, monkeypatch):
    """install.py's step 7 (`plan_for_install`) and every update run."""
    cfg = install.EMBEDDING_CONFIGS["cpu"]
    pp = epp.plan_for_install(tmp_path, cfg, capability_tier=[TIER],
                              launcher_db=tmp_path / "absent.db")
    assert pp.inference == (TIER,)
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    pp = epp.plan_for_install(tmp_path, cfg, capability_tier=[TIER],
                              launcher_db=tmp_path / "absent.db")
    assert pp.inference == () and EMBED in pp.embedding
    # The profile is still recorded, so turning it back ON pulls it again.
    rec = epp.read_record(tmp_path)
    assert rec is not None and rec["capability_tier"] == [TIER]


def test_launcher_ensure_cli_plan_honours_the_switch(tmp_path, monkeypatch):
    """`python -m vco_lib.embedding_pull_plan show|ensure` (the launcher's
    "Download now" and the dual-flag toggles) derive the same plan."""
    _record(tmp_path)
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = epp.main(["show", "--json", "--root", str(tmp_path),
                       "--launcher-db", str(tmp_path / "absent.db")])
    assert rc == 0
    assert json.loads(buf.getvalue())["plan"]["inference"] == []


def test_the_deferral_retry_pulls_from_the_same_plan(tmp_path, monkeypatch):
    """`retry_owed_model_work` (the heal path) ensures `plan_from_machine`."""
    from vco_lib import ollama_pull

    _record(tmp_path)
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    asked: list = []
    monkeypatch.setattr(ollama_pull, "wait_ready", lambda *a, **k: None)
    monkeypatch.setattr(ollama_pull, "ensure", lambda base, models, **k: asked.append(
        tuple(models)) or ollama_pull.EnsureResult(present=list(models), pulled=[], failed={}))
    monkeypatch.setattr(ollama_pull, "verify_present", lambda base, models, **k: (list(models), []))
    outcome, _ = ollama_pull.retry_owed_model_work(
        tmp_path, seed=lambda: True, launcher_db=tmp_path / "absent.db")
    assert outcome == ollama_pull.DONE
    assert asked and TIER not in asked[0] and EMBED in asked[0]


# ── consumer 2: the summary ladder ─────────────────────────────────────────


def _load_sb():
    path = REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
    spec = importlib.util.spec_from_file_location("_sb_local_switch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Asked(Exception):
    pass


def _urlopen_spy(calls):
    def _open(url, *a, **k):
        calls.append(str(getattr(url, "full_url", url)))
        raise _Asked(url)
    return _open


def test_ladder_off_skips_ollama_without_asking_it(monkeypatch):
    sb = _load_sb()
    calls: list = []
    monkeypatch.setattr(sb.urllib.request, "urlopen", _urlopen_spy(calls))
    monkeypatch.setattr(sb, "OLLAMA_DEFAULT_MODEL", TIER, raising=False)
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    assert sb.ollama_available() is False
    assert calls == []


def test_ladder_on_still_asks_ollama(monkeypatch):
    sb = _load_sb()
    calls: list = []
    monkeypatch.setattr(sb.urllib.request, "urlopen", _urlopen_spy(calls))
    monkeypatch.setattr(sb, "OLLAMA_DEFAULT_MODEL", TIER, raising=False)
    monkeypatch.setenv(lsm.ENV_SWITCH, "on")
    with pytest.raises(_Asked):
        sb.ollama_available()
    assert calls and calls[0].endswith("/api/tags")


def test_ladder_reads_the_preferences_toggle(monkeypatch, tmp_path):
    sb = _load_sb()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(_db(tmp_path, "false")))
    assert "turned off" in (sb.local_summary_model_off() or "")
    monkeypatch.setenv(lsm.ENV_SWITCH, "on")
    assert sb.local_summary_model_off() is None


def test_a_pinned_ollama_backend_skips_when_the_switch_is_off(monkeypatch):
    sb = _load_sb()
    sb.reset_backend_cache()
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "ollama")
    monkeypatch.setenv(lsm.ENV_SWITCH, "off")
    assert sb.select_backend() == "skip"
    sb.reset_backend_cache()
    monkeypatch.setenv(lsm.ENV_SWITCH, "on")
    assert sb.select_backend() == "ollama"
    sb.reset_backend_cache()


def test_document_summary_does_not_call_ollama_when_off(monkeypatch):
    """process_documents' direct local summary honours the same switch."""
    src = (REPO_ROOT / "templates" / "scripts" / "process_documents.py").read_text()
    body = src[src.index("def generate_document_summary"):]
    gate = body.index("_sb.local_summary_model_off()")
    assert gate < body.index("requests.post(")


# ── remove ─────────────────────────────────────────────────────────────────


def _tags(models: dict):
    def get_json(url, timeout):
        assert url.endswith("/api/tags")
        return {"models": [{"name": m, "size": s} for m, s in models.items()]}
    return get_json


def _remove(tmp_path, *, confirmed=True, env=None, owns=True, plan=(EMBED,),
            held=None, code=200):
    _record(tmp_path)
    deleted: list = []

    def delete(url, payload, timeout):
        deleted.append((url, dict(payload)))
        return code

    res = lsm.remove(
        tmp_path, confirmed=confirmed, launcher_db=tmp_path / "absent.db",
        env={"VCT_LOCAL_SUMMARY_MODEL": "off"} if env is None else env,
        base_url="http://127.0.0.1:9", get_json=_tags({TIER: 6_000_000_000} if held is None else held),
        delete=delete, owner=lambda: (owns, "VCO's own container vco_ollama" if owns else "adopted"),
        plan_models=lambda: plan)
    return res, deleted


def test_remove_deletes_exactly_the_model_when_every_guard_holds(tmp_path):
    res, deleted = _remove(tmp_path)
    assert res["ok"] and res["removed"] and res["freed_bytes"] == 6_000_000_000
    assert deleted == [("http://127.0.0.1:9/api/delete", {"model": TIER, "name": TIER})]


@pytest.mark.parametrize("kwargs,reason", [
    ({"confirmed": False}, "not_confirmed"),
    ({"env": {"VCT_LOCAL_SUMMARY_MODEL": "on"}}, "still_enabled"),
    ({"owns": False}, "not_owned"),
    ({"plan": (EMBED, TIER)}, "in_plan"),
])
def test_remove_refuses_without_deleting(tmp_path, kwargs, reason):
    res, deleted = _remove(tmp_path, **kwargs)
    assert not res["ok"] and res["reason"] == res["code"] == reason and res["error"]
    assert deleted == []


def test_remove_of_an_absent_model_is_a_no_op(tmp_path):
    res, deleted = _remove(tmp_path, held={})
    assert res["ok"] and not res["removed"] and deleted == []


def test_a_refused_delete_is_reported(tmp_path):
    res, _ = _remove(tmp_path, code=500)
    assert not res["ok"] and res["reason"] == "delete_failed"


# ── status, vco doctor, low disk ───────────────────────────────────────────


def _status(tmp_path, env, held):
    _record(tmp_path)
    return lsm.status(tmp_path, launcher_db=tmp_path / "absent.db", env=env,
                      base_url="http://127.0.0.1:9", get_json=_tags(held),
                      owner=lambda: (True, "VCO's own container"))


def test_status_reports_setting_model_and_presence(tmp_path):
    st = _status(tmp_path, {"VCT_LOCAL_SUMMARY_MODEL": "off"}, {TIER: 6 * 1024 ** 3})
    assert st["ok"] and st["enabled"] is False and st["source"] == "env"
    assert st["model"] == TIER and st["present"] is True and st["size_bytes"] == 6 * 1024 ** 3
    assert st["vco_owns_ollama"] is True
    line = lsm.describe(st)
    assert "OFF" in line and "6.0 GB" in line and "remove it" in line


def test_status_with_ollama_down_is_unknown_not_absent(tmp_path):
    _record(tmp_path)

    def down(url, timeout):
        raise OSError("refused")

    st = lsm.status(tmp_path, launcher_db=tmp_path / "absent.db", env={}, base_url="http://x",
                    get_json=down, owner=lambda: (None, "n/a"))
    assert st["present"] is None and "presence unknown" in lsm.describe(st)


def test_vco_doctor_reports_the_setting(tmp_path):
    st = _status(tmp_path, {}, {})
    res = doctor.DoctorResolvers(local_summary_status=lambda: st)
    probe, scopes = doctor.PROBES["local_summary_model"]
    assert doctor.SCOPE_FULL in scopes
    [f] = probe(tmp_path, res, {})
    assert f.status == doctor.STATUS_OK and "local summary model ON" in f.summary
    [f] = probe(tmp_path, doctor.DoctorResolvers(local_summary_status=lambda: None), {})
    assert f.status == doctor.STATUS_UNKNOWN


def test_low_disk_remediation_names_the_switch():
    text = doctor._disk_remediation([{"path": "/data"}], 2.0)
    assert "VCT_LOCAL_SUMMARY_MODEL=off" in text and "Local summary model" in text


def test_summary_model_json_carries_ok_for_the_launcher_seam():
    """`get_summary_model_info` goes through `run_vco_lib_json`, which reads
    an answer without `"ok": true` as a refusal."""
    from vco_lib import summary_model

    buf = io.StringIO()
    with redirect_stdout(buf):
        summary_model.main(["--json"])
    assert json.loads(buf.getvalue())["ok"] is True
