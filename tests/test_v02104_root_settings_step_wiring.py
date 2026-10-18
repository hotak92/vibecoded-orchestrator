# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — install.py ``main()`` step 9 is wired for real (review R18
§3.10 / LOW-4).

``main()`` hands step 9 to ``_root_settings_step``: the install arm writes
``.claude/settings.json`` (``_configure_claude_settings``, threading
idlecompact's ``settings_template_skipped`` from
``--skip-materialize-claude-dir``), the update arm only cleans the legacy
BASH_ENV shim, and BOTH arms then move this machine's service URLs/ports to
``.claude/settings.local.json`` (``route_root_machine_env``, L5). The callees
are unit-tested elsewhere (``test_v02104_l5_fix_round_r14b.py``,
``test_v02104_replace_all_keeps_template_keys.py``); this suite drives the
REAL ``install.main()`` through the REAL step 9 — every heavy step around it
stubbed by the d2small harness (``_drive_main_to_7b``) — and halts at the
first step after it. No source scan.

RED-PROOF
---------
Drop the ``_self_install.route_root_machine_env(...)`` call from
``_root_settings_step`` (restored from a saved copy, md5-verified): both
routing tests fail (the machine keys stay in ``settings.json``). Drop
``settings_template_skipped=...`` from its ``_configure_claude_settings``
call: the skip-materialize test fails (no file is written — the missing
template is reported instead).
"""
from __future__ import annotations

import json
import sys

import pytest

import install
from tests.test_v02104_install_deferral_report_wiring import _drive_main_to_7b

SENTINEL = "_vco_managed_keys"
MACHINE = {
    "WEAVIATE_URL": "http://localhost:18081",
    "OLLAMA_URL": "http://localhost:18435",
    "CODE_EMBED_SERVICE_URL": "http://localhost:18440",
    "GRPC_PORT": "58052",
}
DEFAULTS = {
    "env": {**MACHINE, "KG_COLLECTION": "Fake_KnowledgeGraph"},
    "permissions": {"allow": ["Read"]},
    SENTINEL: ["env", "hooks"],
}


class _StopAfterStep9(Exception):
    """Raised by the first step after step 9."""


def _halt(*_a, **_k):
    raise _StopAfterStep9()


def _drive_to_step9(monkeypatch, tmp_path):
    _drive_main_to_7b(monkeypatch, tmp_path)
    root = install.PROJECT_ROOT
    monkeypatch.setattr(install, "_run_schema_migration_scripts", lambda r: None)
    monkeypatch.setattr(install, "_trigger_codegraph_maintenance", lambda r: None)
    monkeypatch.setattr(install, "_detect_legacy_shared_kg_class", lambda r: None)
    monkeypatch.setattr(install, "_self_heal_kg_bindings_on_update", lambda r: None)
    monkeypatch.setattr(install, "_emit_orchestrator_root_schema_deferrals", lambda r: None)
    monkeypatch.setattr(install, "_verify_service_endpoints", lambda r: None)
    monkeypatch.setattr(install, "_create_state_directory", lambda: None)
    monkeypatch.setattr(install, "_write_env_config", lambda *a, **k: None)
    monkeypatch.setattr(install, "_update_env_config", lambda *a, **k: None)
    monkeypatch.setattr(install._openai_key, "emit_env_var_deferral", lambda *a, **k: None)
    monkeypatch.setattr(install, "_cleanup_legacy_bash_env_shim", lambda a: None)
    monkeypatch.setattr(install, "_build_vco_settings_defaults",
                        lambda c: json.loads(json.dumps(DEFAULTS)))
    # First step after step 9 in each arm.
    monkeypatch.setattr(install, "_backfill_code_graph_project_env", _halt)
    monkeypatch.setattr(install, "_backfill_vscode_excludes", _halt)
    (root / ".claude").mkdir()
    return root


def _run(monkeypatch, tmp_path, *extra):
    folder = tmp_path / "ledger"
    folder.mkdir()
    monkeypatch.setattr(
        sys, "argv",
        ["install.py", "--yes", "--no-resume", "--skip-models",
         "--suppress-lean-ctx-warning", "--project-folder", str(folder), *extra],
    )
    with pytest.raises(_StopAfterStep9):
        install.main()


def _env(path):
    return json.loads(path.read_text(encoding="utf-8")).get("env", {})


def _assert_routed(root):
    settings = _env(root / ".claude" / "settings.json")
    assert not set(MACHINE) & set(settings), (
        "machine keys still in settings.json — main() step 9 no longer "
        "reaches route_root_machine_env"
    )
    assert settings["KG_COLLECTION"] == "Fake_KnowledgeGraph"
    assert _env(root / ".claude" / "settings.local.json") == MACHINE


def test_install_arm_writes_then_routes_the_machine_env(monkeypatch, tmp_path):
    root = _drive_to_step9(monkeypatch, tmp_path)
    (root / ".claude" / "settings.json").write_text(
        json.dumps({SENTINEL: ["env", "hooks"], "env": {}}), encoding="utf-8")
    _run(monkeypatch, tmp_path)
    _assert_routed(root)  # KG_COLLECTION present: the managed-block merge ran first


def test_update_arm_routes_the_machine_env(monkeypatch, tmp_path):
    root = _drive_to_step9(monkeypatch, tmp_path)
    (root / ".claude" / "settings.json").write_text(json.dumps(DEFAULTS), encoding="utf-8")
    monkeypatch.setattr(install, "_configure_claude_settings", _halt)  # update never writes
    _run(monkeypatch, tmp_path, "--update")
    _assert_routed(root)


def test_skip_materialize_reaches_the_settings_write(monkeypatch, tmp_path):
    """idlecompact: ``--skip-materialize-claude-dir`` → the defaults alone
    (the root has no shipped template here, so without the pass-through the
    write refuses and no file appears)."""
    root = _drive_to_step9(monkeypatch, tmp_path)
    _run(monkeypatch, tmp_path, "--skip-materialize-claude-dir")
    data = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data[SENTINEL] == DEFAULTS[SENTINEL]
    assert "hooks" not in data, "defaults alone — no template keys"
    _assert_routed(root)
