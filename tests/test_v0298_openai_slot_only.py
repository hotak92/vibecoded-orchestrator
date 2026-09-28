# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.98 item 1: VCO's consumers read VCO's OWN OpenAI slot — and the
pre-v0.2.98 env-var cohort is told, in the durable ledger, that it must.

Owner ruling 2026-09-26: a project's secrets are the project's. VCO's
embeddings, gateway, code graph and hooks resolve the shared
``openai_api_key`` slot and nothing else — never ``$OPENAI_API_KEY``, never a
per-project binding, never the project's ``.env``. The consumer half of that
rule is pinned by ``tests/test_v0297_openai_key_store.py`` (the resolver's
scope, red-proofed) and by the embedding suites.

What this file pins is the half that keeps the change from being a SILENT
loss: a machine whose VCO embeddings were paid for by an exported
``$OPENAI_API_KEY`` stops embedding on update, and the only channel that
reaches the user is the install-time ledger entry. Two claims, both
red-proofable:

* the predicate reports the gap only when it can PROVE it (variable set, slot
  empty, probe answered) and never carries a value;
* the emitter fires only where an OpenAI key is what VCO is meant to embed
  with, and the entry it writes is registered, self-clearing and value-free.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.common.vco_openai_slot import no_vco_openai_slot, vco_openai_slot  # noqa: E402
from vco_lib import agent_secrets, deferral_registry, openai_key  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

CID = "openai_key_env_var_no_longer_read"
CANARY = "sk-env-canary-not-a-real-key-4b81"
SLOT_KEY = "sk-slot-not-a-real-key-9f30"


def _report() -> DeferralReport:
    return DeferralReport()


def _entry(report: DeferralReport):
    return report.entry_for(CID)


# --------------------------------------------------------------------------
# The predicate — prove it, or say nothing
# --------------------------------------------------------------------------


def test_an_unset_variable_is_not_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cohort is defined by the VARIABLE. Nothing exported, nothing owed —
    including on a machine whose slot is empty for the ordinary reason that
    the user never configured OpenAI."""
    monkeypatch.delenv(openai_key.OPENAI_ENV_VAR, raising=False)
    with no_vco_openai_slot():
        assert openai_key.env_var_no_longer_read() == ""


def test_a_populated_slot_is_not_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave-alone case: the variable is set but VCO's slot answers, so VCO
    has its key and the user owes nothing."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)
    with vco_openai_slot(SLOT_KEY):
        assert openai_key.env_var_no_longer_read() == ""


def test_the_gap_is_reported_by_name_and_never_by_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report says WHICH two facts hold. It must not carry the value, and
    must not smuggle it in as a length or a fragment."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)
    with no_vco_openai_slot():
        evidence = openai_key.env_var_no_longer_read()
    assert evidence
    assert openai_key.OPENAI_ENV_VAR in evidence
    assert openai_key.OPENAI_SECRET_NAME in evidence
    assert CANARY not in evidence
    assert str(len(CANARY)) not in evidence


def test_an_unprovable_probe_makes_no_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conservative default on a best-effort path: when the store cannot be
    asked (permission refusal, hub unreachable with fallback disabled), the
    answer is "no claim", never "the slot is empty" — an entry invented from a
    failed probe is worse than none."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)

    def _explode(*_a, **_k):
        raise agent_secrets.AccessDenied("key_not_active (test)")

    monkeypatch.setattr(agent_secrets, "get", _explode)
    assert openai_key.env_var_no_longer_read() == ""


# --------------------------------------------------------------------------
# The emitter — only where an OpenAI key is what VCO embeds with
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["qwen3", "arctic", "", None])
def test_a_non_openai_backend_is_never_nagged(
    monkeypatch: pytest.MonkeyPatch, backend,
) -> None:
    """A machine on the default backend that exports the variable for
    unrelated tools owes nothing: VCO never read it for VCO's benefit there.
    The runtime warning covers the moment such a machine switches backends, so
    the install-time entry stays out of the way."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)
    report = _report()
    with no_vco_openai_slot():
        openai_key.emit_env_var_deferral(report, backend)
    assert _entry(report) is None


def test_the_env_var_cohort_is_told_once_the_gap_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cohort the plan names: OpenAI backend, exported variable, empty
    slot. The entry reaches the ledger with the remedy, and nothing in it is
    or contains the key."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)
    report = _report()
    with no_vco_openai_slot():
        openai_key.emit_env_var_deferral(report, "openai")

    entry = _entry(report)
    assert entry is not None, "the cohort must never be a silent loss"
    assert entry.condition_id == CID
    assert openai_key.OPENAI_SECRET_NAME in entry.command_to_apply
    rendered = json.dumps({
        "title": entry.title,
        "detected": entry.detected,
        "why_deferred": entry.why_deferred,
        "command_to_apply": entry.command_to_apply,
    })
    assert CANARY not in rendered


def test_a_populated_slot_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clear path, at the emitter: the same run that finds the key in
    VCO's slot writes no entry — which is what makes the next update drop one
    that is already there (owned-drop-when-absent)."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)
    report = _report()
    with vco_openai_slot(SLOT_KEY):
        openai_key.emit_env_var_deferral(report, "openai")
    assert _entry(report) is None


def test_the_emitter_soft_fails_into_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic must never be the thing that breaks an update: any failure
    inside the emitter is swallowed (and logged), and the run continues."""
    monkeypatch.setenv(openai_key.OPENAI_ENV_VAR, CANARY)

    def _explode(*_a, **_k):
        raise RuntimeError("probe exploded (test)")

    monkeypatch.setattr(openai_key, "env_var_no_longer_read", _explode)
    report = _report()
    openai_key.emit_env_var_deferral(report, "openai")
    assert _entry(report) is None


# --------------------------------------------------------------------------
# The registration — a cid with a lifecycle, or the gate fails
# --------------------------------------------------------------------------


def test_the_cid_is_registered_install_owned_and_self_clearing() -> None:
    """Ownership is what makes the entry disappear on the next run instead of
    becoming an immortal ledger row: install.py re-detects it every run and
    finalize rebuilds the file from that run's report."""
    spec = deferral_registry.condition(CID)
    assert spec is not None, "the cid must be registered or the gate fails"
    assert spec.condition_class == "action_required"
    assert spec.owner == "install.py"
    assert spec.clear_probe == "owned-drop-when-absent"
    assert CID in deferral_registry.install_owned_ids()


# --------------------------------------------------------------------------
# The second env path — an admission test that must agree with the write path
# --------------------------------------------------------------------------


def _write_set(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The text write-set as the SSOT computes it, with the fan-out forced on.

    ``configured_text_models`` is env-only on purpose (it is the chunk-budget
    SSOT), so its inputs are the environment plus whatever the OpenAI slot
    resolves — never a live service instance.
    """
    from vco_lib import embedding_service

    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", embedding_service.DEFAULT_TEXT_MODEL)
    monkeypatch.setenv(embedding_service.DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, "1")
    monkeypatch.delenv(embedding_service.DUAL_EMBEDDING_ARCTIC_SECONDARY_ENV,
                       raising=False)
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    return embedding_service.configured_text_models()


def test_a_second_env_path_cannot_admit_the_openai_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$OPENAI_EMBEDDING_API_KEY`` used to be ORed into the write-set's
    admission test. The write path then embedded with ``self.openai_api_key``
    — resolved from VCO's slot — so an env-only key produced a fan-out entry
    with no write behind it. The admission test must name the key VCO will
    actually use."""
    monkeypatch.setenv("OPENAI_EMBEDDING_API_KEY", CANARY)
    with no_vco_openai_slot():
        assert "text-embedding-3-small" not in _write_set(monkeypatch)


def test_the_slot_is_still_admitted_when_it_holds_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half, so the assertion above cannot pass because the
    OpenAI secondary was removed altogether."""
    with vco_openai_slot(SLOT_KEY):
        assert "text-embedding-3-small" in _write_set(monkeypatch)
