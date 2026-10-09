# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (OD-4 + its 2026-10-09 refinement): the OpenAI embedding slot
resolves the project's DEDICATED ``vco_openai_api_key`` first, then VCO's
shared ``openai_api_key``.

The owner's refined ruling (2026-10-09): a project gives VCO its own OpenAI
key through a DEDICATED per-project secret ``vco_openai_api_key``, added
from the project's Secrets panel. Resolution: project
``vco_openai_api_key`` → shared ``openai_api_key`` (VCO's slot, shared
scope only — the hub's ``slot_exclusive`` guard on that name stays). A
project's own OpenAI keys — ``$OPENAI_API_KEY``, a per-project
``openai_api_key``, the project ``.env`` — are NEVER read for VCO (the
2026-09-26 ruling stands). ``key_paused`` on either leg ⇒ the slot is off
with one WARNING naming the panel; ``key_not_active`` falls through to the
shared slot.

Every test runs with the hub pinned to the discard port (or a faked
``_hub_get`` standing in for the hub's ``/env`` answer) and the file store
in a tmp dir; the canary never reaches a verdict, a warning or an
assertion message.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vco_lib import agent_secrets, config_projection, openai_key  # noqa: E402

PROJECT_KEY = "sk-project-not-a-real-key-8b41"
SHARED_KEY = "sk-shared-not-a-real-key-3f07"

#: The hint copy the launcher's per-project Secrets panel shows for the
#: dedicated key — pinned here so the panel and the docs cannot drift.
PANEL_HINT = (
    "used by VCO's OpenAI embedding slot for this project; falls back to "
    "the shared VCO key"
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No hub (discard port), a tmp file store, a clean resolver cache."""
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("VCT_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("VCT_HUB_PORT", "9")
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    openai_key._resolved.clear()
    openai_key._warned_env_var = False
    openai_key._warned_paused = False
    openai_key._warned_leg1 = False
    yield secrets
    openai_key._resolved.clear()
    openai_key._warned_env_var = False
    openai_key._warned_paused = False
    openai_key._warned_leg1 = False


def _project_dir(tmp_path: Path) -> Path:
    """A registered-shaped project folder (``.vct-project`` marker)."""
    project = tmp_path / "acme-app"
    project.mkdir(exist_ok=True)
    (project / ".vct-project").write_text("acme-app\n")
    return project


def _shared_copy(stores: Path) -> Path:
    return stores / "shared" / openai_key.OPENAI_SECRET_NAME


def _project_copy(stores: Path, name: str) -> Path:
    return stores / "projects" / "acme-app" / name


def _fake_hub(monkeypatch: pytest.MonkeyPatch, answer) -> None:
    """Stand in for the hub's ``/env``: ``answer(key, project)`` → value,
    ``None`` ⇒ ``SecretNotFound`` (the honest nothing-declared miss), an
    exception instance ⇒ raised as-is (a paused key)."""

    def _hub_get(key: str, project: Optional[str]):
        result = answer(key, project)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise agent_secrets.SecretNotFound(f"key {key!r} not found")
        return result

    monkeypatch.setattr(agent_secrets, "_hub_get", _hub_get)


# ─── the arms (OD-4 refinement) ─────────────────────────────────────────


def test_the_projects_dedicated_key_is_chosen_over_the_shared_one(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm 1: a project holding the DEDICATED ``vco_openai_api_key``
    (keychain row via the hub, exactly what the Secrets panel writes) embeds
    with IT, not with the shared value."""
    _fake_hub(monkeypatch, lambda key, project: (
        PROJECT_KEY if key == openai_key.VCO_PROJECT_SECRET_NAME
        else SHARED_KEY if key == openai_key.OPENAI_SECRET_NAME
        else None
    ))
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == PROJECT_KEY


def test_the_dedicated_file_store_copy_answers_too(
    stores: Path, tmp_path: Path,
) -> None:
    """Arm 1b: the same name in the file store's per-project leg
    (``vct set --project acme-app --key vco_openai_api_key``) answers with
    no hub in sight."""
    _project_copy(stores, openai_key.VCO_PROJECT_SECRET_NAME).parent.mkdir(parents=True)
    _project_copy(stores, openai_key.VCO_PROJECT_SECRET_NAME).write_text(PROJECT_KEY)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == PROJECT_KEY


def test_a_per_project_openai_api_key_is_ignored_shared_used(
    stores: Path, tmp_path: Path,
) -> None:
    """Arm 2 — the Sept ruling's guard: a per-project ``openai_api_key``
    under the SLOT's own name is NEVER read for VCO. Only the shared copy
    answers (keychain or file store), even when the project holds a
    per-project copy of that very name."""
    _shared_copy(stores).parent.mkdir(parents=True)
    _shared_copy(stores).write_text(SHARED_KEY)
    for name in (openai_key.OPENAI_SECRET_NAME, "OPENAI_API_KEY"):
        _project_copy(stores, name).parent.mkdir(parents=True, exist_ok=True)
        _project_copy(stores, name).write_text(PROJECT_KEY)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == SHARED_KEY


def test_the_shared_key_answers_when_the_project_has_no_dedicated_key(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm 3: ``key_not_active`` / nothing-declared for
    ``vco_openai_api_key`` falls through to the shared slot — a project
    without a dedicated key keeps embedding exactly as before."""
    _fake_hub(monkeypatch, lambda key, project: (
        SHARED_KEY if key == openai_key.OPENAI_SECRET_NAME else None
    ))
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == SHARED_KEY


def test_a_paused_dedicated_key_disables_the_slot_even_with_shared_present(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Arm 4: the DEDICATED key EXISTS but is paused (``key_paused``) — the
    slot is OFF with one WARNING naming the panel. Falling through to the
    shared slot here would be routing around a deliberate owner decision."""
    def answer(key, project):
        if key == openai_key.VCO_PROJECT_SECRET_NAME:
            return agent_secrets.SecretPaused(
                f"key {key!r} exists but is paused"
            )
        return SHARED_KEY if key == openai_key.OPENAI_SECRET_NAME else None

    _fake_hub(monkeypatch, answer)
    project = str(_project_dir(tmp_path))
    assert openai_key.resolve_openai_api_key(project) == ""
    err = capsys.readouterr().err
    assert openai_key.VCO_PROJECT_SECRET_NAME in err, "the WARNING names the key"
    assert "PAUSED" in err
    assert "Preferences → Secrets" in err, "the WARNING names the panel"
    assert "second copy" in err, "the WARNING warns against forking the value"
    # W5C-2 (coordinator-confirmed semantics): the WARNING must SAY that the
    # slot stays off even with a healthy shared key, and why — plus how to
    # resume. Anything less reads as "no key configured".
    assert "even though the shared VCO key may be healthy" in err
    assert "route around" in err, "it names the no-fallback rule"
    assert "Resume" in err, "it says how to resume"
    assert PROJECT_KEY not in err and SHARED_KEY not in err

    # Once per process, not once per call.
    assert openai_key.resolve_openai_api_key(project) == ""
    assert capsys.readouterr().err == ""


def test_a_leg1_read_failure_falls_back_with_one_warning(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """W5C-3: a genuine READ failure of the dedicated leg (locked keychain,
    hub error, timeout) is not an absence — the resolver still falls back
    to the shared slot (the run must not break), but ONE WARNING names the
    failure class and the fallback, so a keychain problem is not hidden
    behind a healthy shared answer. Once per process; never a value."""
    def answer(key, project):
        if key == openai_key.VCO_PROJECT_SECRET_NAME:
            return agent_secrets.KeychainLocked("keychain is locked")
        return SHARED_KEY if key == openai_key.OPENAI_SECRET_NAME else None

    _fake_hub(monkeypatch, answer)
    project = str(_project_dir(tmp_path))
    assert openai_key.resolve_openai_api_key(project) == SHARED_KEY, (
        "a read failure falls back to the shared slot — the run must not break"
    )
    err = capsys.readouterr().err
    assert "could not READ" in err
    assert openai_key.VCO_PROJECT_SECRET_NAME in err
    assert "KeychainLocked" in err, "the WARNING names the failure class"
    assert "falling back to the shared VCO key" in err
    assert "do not store a second copy" in err, "no divergence remedy"
    assert PROJECT_KEY not in err and SHARED_KEY not in err

    # Once per process, not once per call.
    assert openai_key.resolve_openai_api_key(project) == SHARED_KEY
    assert capsys.readouterr().err == ""


def test_a_leg1_absence_falls_back_silently(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The other half of W5C-3: ``key_not_active`` for the dedicated name
    is a PROVEN absence — the ordinary machine without a dedicated key must
    fall through to the shared slot with NO warning on every run."""
    def answer(key, project):
        if key == openai_key.OPENAI_SECRET_NAME:
            return SHARED_KEY
        return None  # key_not_active for the dedicated name

    _fake_hub(monkeypatch, answer)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == SHARED_KEY
    err = capsys.readouterr().err
    assert err == "", "an ordinary absence is silent — only read failures warn"


def test_a_paused_shared_key_disables_the_slot(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Arm 4b: the SHARED key paused (project has no dedicated key) ⇒ the
    slot is off, same one WARNING."""
    def answer(key, project):
        if key == openai_key.OPENAI_SECRET_NAME:
            return agent_secrets.SecretPaused(
                f"key {key!r} exists but is paused"
            )
        return None

    _fake_hub(monkeypatch, answer)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == ""
    err = capsys.readouterr().err
    assert openai_key.OPENAI_SECRET_NAME in err and "PAUSED" in err


def test_a_lower_tier_copy_still_serves_while_the_keychain_key_is_paused(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallthrough semantics unchanged (v0.2.101 Q7): a pause refuses the
    KEYCHAIN leg only; an independent lower-tier copy (here the shared file
    store) still answers — no warning, no off-slot."""
    def answer(key, project):
        if key == openai_key.OPENAI_SECRET_NAME:
            return agent_secrets.SecretPaused(
                f"key {key!r} exists but is paused"
            )
        return None

    _fake_hub(monkeypatch, answer)
    _shared_copy(stores).parent.mkdir(parents=True)
    _shared_copy(stores).write_text(SHARED_KEY)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == SHARED_KEY


def test_no_key_anywhere_answers_empty_without_crashing(
    stores: Path, tmp_path: Path,
) -> None:
    """Arm 5: nothing stored anywhere ⇒ ``""``, no exception — the OpenAI
    slot is simply off."""
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == ""


# ─── what the ruling does NOT open ──────────────────────────────────────


def test_the_projects_own_env_file_is_still_not_a_source(
    stores: Path, tmp_path: Path,
) -> None:
    """No new store: tier 3 (the project's ``.env``) stays excluded. A line
    naming the dedicated key — or either spelling of the old name — never
    answers for the OpenAI slot."""
    project = _project_dir(tmp_path)
    (project / ".env").write_text(
        f"vco_openai_api_key={PROJECT_KEY}\n"
        f"openai_api_key={PROJECT_KEY}\nOPENAI_API_KEY={PROJECT_KEY}\n"
    )
    assert openai_key.resolve_openai_api_key(str(project)) == ""
    _shared_copy(stores).parent.mkdir(parents=True)
    _shared_copy(stores).write_text(SHARED_KEY)
    openai_key._resolved.clear()  # the resolver answers once per project
    assert openai_key.resolve_openai_api_key(str(project)) == SHARED_KEY


def test_the_env_var_is_still_not_a_source(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new env name: ``$OPENAI_API_KEY`` keeps NOT answering, whatever is
    exported in the project's shell."""
    monkeypatch.setenv("OPENAI_API_KEY", PROJECT_KEY)
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == ""
    _shared_copy(stores).parent.mkdir(parents=True)
    _shared_copy(stores).write_text(SHARED_KEY)
    openai_key._resolved.clear()  # the resolver answers once per project
    assert openai_key.resolve_openai_api_key(str(_project_dir(tmp_path))) == SHARED_KEY


# ─── the consumers and the write invariant ──────────────────────────────


def test_for_project_threads_the_project_identity_into_the_resolver(
    stores: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EmbeddingService.for_project`` resolves its OpenAI key with the
    project's root as the resolver's project argument — the lookup scope of
    the dedicated key, not merely the requester identity."""
    from vco_lib import embedding_service

    project = _project_dir(tmp_path)
    seen: list[str | None] = []

    def _record(project_arg=None):
        seen.append(project_arg)
        return ""

    monkeypatch.setattr(openai_key, "resolve_openai_api_key", _record)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "http://127.0.0.1:9")
    try:
        embedding_service.EmbeddingService.for_project(project)
    except embedding_service.NoEmbeddingBackendError:
        pass  # expected with both backends pinned to the discard port
    assert seen and seen[0] == str(project.resolve()), (
        "for_project must pass the project root to the resolver"
    )


def test_the_env_projection_has_no_channel_for_the_openai_key_value() -> None:
    """Write invariant: the ``.env`` projection's canonical key set is
    closed and contains neither spelling of either OpenAI key name, so no
    projected file can carry the value the matrix resolves."""
    keys = config_projection.list_canonical_keys()
    for name in (
        openai_key.OPENAI_SECRET_NAME,
        openai_key.VCO_PROJECT_SECRET_NAME,
        openai_key.OPENAI_ENV_VAR,
    ):
        assert name not in keys, name
