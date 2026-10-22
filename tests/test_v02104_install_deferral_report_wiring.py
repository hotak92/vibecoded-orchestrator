# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the ``deferral_report`` pass-through from ``install.main()``
into step 7b is wired for real (D2 sweep item D2.4; R24 coordinator note 2).

R24 pinned the 7b CALLEE (``_ensure_collections`` records
``root_kg_blocked_by_case_sibling`` itself when the root's folder-derived
class is blocked by a populated case-sibling) but left the WIRING untested:
the ``main()`` lambda that hands the run's ``DeferralReport`` to 7b —
``_install_weaviate.collections_and_seed(lambda: _ensure_collections(...,
deferral_report=_deferral_report), ...)``. A dropped argument there would
silently lose the entry, and a source scan would not notice (the rule is:
never guard wiring with a source scan).

This suite drives the REAL ``install.main()`` to the REAL
``collections_and_seed`` call — every heavy step before/around it
monkeypatched, the 7b lambda and its callee real — with a fake Weaviate
holding a POPULATED case-sibling of the root's folder-derived class. The
entry must land in the run's report.

Scope note (the brief vs. the code): the brief described the observable as
``schema_migration_required`` "produced in the report through that call
path". That condition is emitted by ``vco_lib.bootstrap_schema_guard`` via
``project_init.bootstrap_collections`` — a path install.py's ``main()``
never calls (it is the launcher / install-bundle / ``bootstrap-collections``
CLI path, and its ledger write is already pinned by
``tests/test_v02103_bootstrap_schema_guard.py``). The populated-class guard
entry that DOES flow through install's 7b wiring is
``root_kg_blocked_by_case_sibling`` (the root's class blocked by a POPULATED
case-sibling — ``_count_weaviate_class_objects`` → 892 below), so that is
what this suite pins.

RED-PROOF
---------
Drop ``deferral_report=_deferral_report`` from the 7b lambda in main()
(restored from a saved copy, md5-verified): the test fails — no
``root_kg_blocked_by_case_sibling`` entry is recorded.
"""
from __future__ import annotations

import json
import sys

import pytest

import install
from vco_lib import install_companions as ic
from vco_lib import root_kg_identity as rki
from vco_lib.deferral_report import DeferralReport

from tests.common.env import process_state_fixture

# install.main() step 7b writes the project-identity env (KG_COLLECTION & co.) into
# os.environ for its child processes; restore the whole process state per test so the
# conftest W-IDENTITY guard (and every later test) sees the environment untouched.
_isolate_process_state = process_state_fixture("function")

DERIVED = "VibecodedOrchestrator_KnowledgeGraph"
CONST = "VibeCodedOrchestrator_KnowledgeGraph"


class _Stop7b(Exception):
    """Raised by the first post-seed step so the run stops right after the
    7b/seed phase — the wiring under test has executed by then."""


class _FakeWeaviate:
    """Answers /v1/schema from ``classes`` and records schema POSTs."""

    def __init__(self, classes):
        self.classes, self.posted = list(classes), []

    def __call__(self, method, url, body=None, timeout=None, **_k):
        if method == "GET" and url.endswith("/v1/schema"):
            return 200, json.dumps({"classes": [{"class": c} for c in self.classes]}).encode()
        if method == "POST" and url.endswith("/v1/schema"):
            self.posted.append((body or {}).get("class"))
            return 200, b"{}"
        return 404, b""


def _drive_main_to_7b(monkeypatch, tmp_path):
    """Stub every heavy step around the REAL 7b call chain in main().

    Returns (fake_weaviate, recorded_entries). The chain kept real:
    ``_install_weaviate.collections_and_seed`` ← main()'s REAL lambdas →
    the REAL ``_ensure_collections`` (with the Weaviate layer faked) → the
    run's REAL ``DeferralReport``.
    """
    root = tmp_path / "vibecoded-orchestrator"
    root.mkdir()
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    # The REAL main() takes the process singleton lock and sets the module
    # globals _MAIN_ENTRY_LOCK_HELD / _MAIN_ENTRY_LOCK_HANDLE, which nothing
    # resets in-process: a later test's _install_advisory_lock then yields
    # None (seen as 15 order-dependent failures in a single-process suite).
    # Stub the lock and pin both globals so monkeypatch restores them.
    monkeypatch.setattr(install, "_install_singleton_lock_or_die",
                        lambda *a, **k: None)
    monkeypatch.setattr(install, "_MAIN_ENTRY_LOCK_HELD", False)
    monkeypatch.setattr(install, "_MAIN_ENTRY_LOCK_HANDLE", None)
    # main() also writes launcher-DB app_state (e.g. the default code
    # model) through vct_root_dir(); give it a per-test state dir so those
    # rows never reach the session-shared test launcher.db that later tests
    # resolve their code model from.
    state = tmp_path / "vct_state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.setattr(install, "_ensure_running_under_mcp_venv", lambda: None)
    monkeypatch.setattr(install, "validate_source_repo", lambda r: None)
    monkeypatch.setattr(install, "_PENDING_EVENTS", [])
    monkeypatch.setattr(install, "_HANDOFF", ic.PostVenvHandoff())
    monkeypatch.setattr(install, "_resolve_adopt_project_mode",
                        lambda a, install_path=None: "adopt")
    monkeypatch.setattr(install, "_audit_and_offer_env_secret_migration",
                        lambda *a, **k: None)
    # Steps 1-5: checks, detection, prompts, venv — none may run for real.
    monkeypatch.setattr(install, "_check_python_version", lambda: None)
    monkeypatch.setattr(install, "_check_prerequisites", lambda: None)
    import types as _types
    monkeypatch.setattr(install, "_detect_system",
                        lambda a, r: _types.SimpleNamespace(container_cmd="podman"))
    monkeypatch.setattr(install, "_print_system_info", lambda s: None)
    monkeypatch.setattr(install, "_detect_optional_companions", lambda a: None)
    monkeypatch.setattr(install, "_choose_embedding_config",
                        lambda s, a: {"description": "x"})
    monkeypatch.setattr(install, "_finalize_embed_config_for_host", lambda *a, **k: None)
    monkeypatch.setattr(install, "_reconcile_install_active_embedding", lambda *a: None)
    monkeypatch.setattr(install, "_storage_choice_step", lambda a: {"mode": "named"})
    monkeypatch.setattr(install, "_create_venv", lambda r: tmp_path / "venv" / "bin" / "python")
    monkeypatch.setattr(install, "_install_requirements", lambda v, dev=False: None)
    monkeypatch.setattr(install, "_continue_in_venv_after_deps", lambda *a, **k: None)
    # Steps between the venv hand-off and the container block.
    monkeypatch.setattr(install, "_run_machine_migrations", lambda r: None)
    monkeypatch.setattr(install, "_run_root_claude_dir_install",
                        lambda a, deferral_report=None: {})
    monkeypatch.setattr(install._project_init, "materialize_root_knowledge",
                        lambda *a, **k: None)
    monkeypatch.setattr(install, "_materialize_vct_secrets_shared_readme", lambda p: None)
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "_emit_orchestrator_root_env_keys", lambda p: None)
    monkeypatch.setattr(install, "_scan_foreign_compose_files", lambda p: [])
    monkeypatch.setattr(install, "_recorded_code_model_before_step7", lambda: None)
    # The container block, up to (but excluding) the 7b call chain.
    monkeypatch.setattr(install, "_record_install_choice", lambda *a, **k: None)
    monkeypatch.setattr(install, "_reconcile_service_endpoints",
                        lambda a, s, r, **k: {})
    monkeypatch.setattr(install, "_start_services", lambda *a, **k: None)
    # The seed half of the chain: pretend the seed succeeded (SEEDED maps
    # from any non-sentinel return).
    monkeypatch.setattr(install, "_seed_weaviate", lambda *a, **k: None)
    # Stop right after the seed phase: the first step gated on success.
    def _halt(_report):
        raise _Stop7b()
    monkeypatch.setattr(install, "_run_schema_migration_scripts", _halt)

    # The Weaviate layer under the REAL _ensure_collections: one POPULATED
    # case-sibling of the root's folder-derived class (same fake shape as
    # tests/test_v02103_root_kg_identity.py).
    fake = _FakeWeaviate([CONST])
    monkeypatch.setattr(install._wh, "http_request", fake)
    monkeypatch.setattr(rki, "_live_class_names", lambda url: list(fake.classes))
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects",
                        lambda u, n: 892)
    monkeypatch.setattr(install, "_wait_for_weaviate_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        "vco_lib.launcher_db_reader.get_orchestrator_root_bindings",
        lambda: (None, None))
    for k in ("KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION",
              rki.OVERRIDE_ENV):
        monkeypatch.delenv(k, raising=False)
    # A polluted shell names another project's KG — the root must not take it.
    monkeypatch.setenv("KG_COLLECTION", "Other_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_COLLECTION", CONST)

    # Observe the run's report: spy on the class method so the entry is
    # captured no matter which instance main() builds.
    entries: list = []
    orig_add = DeferralReport.add_entry

    def _spy(self, entry):
        entries.append(entry)
        return orig_add(self, entry)

    monkeypatch.setattr(DeferralReport, "add_entry", _spy)
    return fake, entries


def test_main_threads_the_run_report_into_the_7b_guard(monkeypatch, tmp_path):
    """main() → collections_and_seed → the 7b lambda → _ensure_collections:
    a populated case-sibling blocking the root's class must land its
    consent entry in the RUN's deferral report."""
    folder = tmp_path / "ledger"
    folder.mkdir()
    fake, entries = _drive_main_to_7b(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["install.py", "--yes", "--no-resume", "--skip-models",
         "--suppress-lean-ctx-warning", "--project-folder", str(folder)],
    )
    with pytest.raises(_Stop7b):
        install.main()

    blocked = [e for e in entries if e.condition_id == "root_kg_blocked_by_case_sibling"]
    assert blocked, (
        "the 7b guard entry never reached the run report — the "
        "deferral_report=_deferral_report pass-through on main()'s 7b "
        "lambda is broken"
    )
    assert DERIVED not in fake.posted and CONST not in fake.posted, (
        "sanity: the root's class was NOT created beside its populated "
        "case-sibling (the guard the entry reports on really fired)"
    )
