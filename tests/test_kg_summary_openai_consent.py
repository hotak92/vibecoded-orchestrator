# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 C10 — KG-summary OpenAI consent gate.

The `templates/scripts/generate-kg-summary.py` script reads the
`kg_summary_openai_consent` app_state row from the launcher SQLite DB
before allowing the OpenAI tier to be selected. This test exercises
the gate in isolation:

  - consent=false AND KG_SUMMARY_BACKEND=openai → script logs a clear
    message and selects 'skip' (NOT 'openai').
  - consent=true → openai is selectable (assuming key is present).
  - --force-api flag bypasses the gate entirely.
  - Missing launcher.db / missing app_state table / missing row are
    all treated as consent=false (the safe default).

We import the script directly via importlib because it lives under
`templates/scripts/` (not on the default sys.path) — and we exercise
its module-level helpers in isolation, not by running it as a
subprocess. That keeps the test fast and lets us inject env / DB
state surgically.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.common.launcher_db_fixture import (
    create_empty_launcher_db,
    set_app_state,
)


# Resolve the script path relative to the repo root. Each test loads
# the module fresh (via importlib) so module-level state (env reads,
# _BACKEND_CACHE) doesn't bleed across cases.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"


def _load_script_module(name: str = "kg_summary_under_test") -> Any:
    """Import generate-kg-summary.py as a module under a unique name.

    Unique name → no module-level state leak across tests. We delete
    the cached entry before re-importing to be defensive.
    """
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_app_state_db(
    db_path: Path,
    *,
    consent: "bool | None" = None,
    model: "str | None" = None,
) -> None:
    """Create launcher.db on the REAL launcher schema and seed app_state.

    v0.2.92 §3.4: ``app_state`` is a real launcher table, so it now comes from
    the shipped migrations rather than from a hand-rolled ``CREATE TABLE``
    that (unlike production) let ``value`` be NULL.
    """
    create_empty_launcher_db(db_path)
    if consent is not None:
        set_app_state(
            db_path, "kg_summary_openai_consent", "true" if consent else "false",
        )
    if model is not None:
        set_app_state(db_path, "kg_summary_openai_model", model)


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """VCT_STATE_DIR pointing at an empty tmp dir.

    The script reads launcher.db relative to VCT_STATE_DIR (or
    ~/.vct/launcher.db otherwise). Pinning VCT_STATE_DIR for the test
    isolates each case + lets us seed app_state surgically.
    """
    state_dir = tmp_path / "vct-state"
    state_dir.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    # Clean any forced backend env var that might be picked up.
    monkeypatch.delenv("KG_SUMMARY_BACKEND", raising=False)
    return state_dir


# ────────────────────────────────────────────────────────────────────
# Consent gate — DB row variants
# ────────────────────────────────────────────────────────────────────

class TestConsentRowReading:
    def test_missing_db_treated_as_no_consent(
        self, isolated_state: Path,
    ) -> None:
        """No launcher.db at all → consent=False."""
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_missing_row_treated_as_no_consent(
        self, isolated_state: Path,
    ) -> None:
        """DB exists, table exists, row absent → consent=False."""
        _seed_app_state_db(isolated_state / "launcher.db")
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_row_false_returns_false(self, isolated_state: Path) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=False)
        mod = _load_script_module()
        assert mod.openai_consent_granted() is False

    def test_row_true_returns_true(self, isolated_state: Path) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=True)
        mod = _load_script_module()
        assert mod.openai_consent_granted() is True

    def test_truthy_string_variants(self, isolated_state: Path) -> None:
        """The script accepts {true, 1, yes} (case-insensitive) as true."""
        for raw in ["true", "TRUE", "True", "1", "yes", "YES"]:
            db_path = isolated_state / "launcher.db"
            db_path.unlink(missing_ok=True)
            create_empty_launcher_db(db_path)
            set_app_state(db_path, "kg_summary_openai_consent", raw)
            mod = _load_script_module(name=f"kgu_{raw}")
            assert mod.openai_consent_granted() is True, (
                f"expected {raw!r} → True"
            )

    def test_non_truthy_string_variants(self, isolated_state: Path) -> None:
        """Anything else (including empty / random) is False."""
        for raw in ["false", "0", "no", "", "maybe", "off"]:
            db_path = isolated_state / "launcher.db"
            db_path.unlink(missing_ok=True)
            create_empty_launcher_db(db_path)
            set_app_state(db_path, "kg_summary_openai_consent", raw)
            mod = _load_script_module(name=f"kgu_neg_{raw or 'empty'}")
            assert mod.openai_consent_granted() is False, (
                f"expected {raw!r} → False"
            )


# ────────────────────────────────────────────────────────────────────
# select_backend — gate behaviour
# ────────────────────────────────────────────────────────────────────

class TestSelectBackendConsentGate:
    def test_forced_openai_without_consent_falls_through_to_skip(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KG_SUMMARY_BACKEND=openai + no consent + no --force-api → skip.

        The script must NOT honour the env-var forcing of OpenAI when
        consent has not been granted; that's the whole point of the
        gate. The user gets a clear log message and the script
        exits 0 (no error) — leaving the KG node un-summarised but
        otherwise intact.
        """
        # No DB seeding → consent is False (the safe default).
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        # Defense: ensure no force flag is leaked.
        mod._FORCE_API = False
        chosen = mod.select_backend()
        assert chosen == "skip", (
            f"expected forced openai + no consent → 'skip', got {chosen!r}"
        )

    def test_forced_openai_with_consent_picks_openai(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_app_state_db(isolated_state / "launcher.db", consent=True)
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        mod._FORCE_API = False
        chosen = mod.select_backend()
        assert chosen == "openai"

    def test_force_api_flag_bypasses_consent_gate(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--force-api operator override: even without consent, pick openai
        (when forced via env).
        """
        # No consent in DB.
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        mod = _load_script_module()
        mod._FORCE_API = True
        chosen = mod.select_backend()
        assert chosen == "openai", (
            f"--force-api should bypass consent gate; got {chosen!r}"
        )


# ────────────────────────────────────────────────────────────────────
# Model resolution
# ────────────────────────────────────────────────────────────────────

class TestOpenAIModelResolution:
    def test_default_model_when_unset(self, isolated_state: Path) -> None:
        """No env, no app_state row → default 'gpt-4o-mini'."""
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4o-mini"

    def test_app_state_row_overrides_default(self, isolated_state: Path) -> None:
        _seed_app_state_db(
            isolated_state / "launcher.db",
            model="gpt-4o",
        )
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4o"

    def test_env_var_overrides_app_state(
        self, isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KG_SUMMARY_OPENAI_MODEL beats both stored value and default."""
        _seed_app_state_db(
            isolated_state / "launcher.db",
            model="gpt-4o",
        )
        monkeypatch.setenv("KG_SUMMARY_OPENAI_MODEL", "gpt-4.1-mini")
        mod = _load_script_module()
        assert mod._openai_model() == "gpt-4.1-mini"


# ────────────────────────────────────────────────────────────────────
# v0.2.98 (owner ruling 2026-09-26): _openai_api_key resolves ONLY VCO's
# shared slot, through the shipped resolver's SHARED-ONLY form.
# `$OPENAI_API_KEY` is never read, nor a project scope, nor a project
# `.env` — a project's key is the project's.
# ────────────────────────────────────────────────────────────────────

_SB_PATH = _REPO_ROOT / "templates" / "scripts" / "summary_backends.py"


def _load_summary_backends(name: str = "sb_shared_only_under_test") -> Any:
    """Fresh summary_backends module (its `_openai_key_cache` is module
    state, so each case needs a clean load)."""
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _SB_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _dead_hub_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_resolver_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Tier 1 unreachable + isolated file store for the resolver subprocess
    `_resolve_secret_via_shipped_resolver` spawns."""
    store = tmp_path / "secrets-store"
    (store / "shared").mkdir(parents=True)
    (store / "projects").mkdir()
    monkeypatch.setenv("VCT_SECRETS_DIR", str(store))
    monkeypatch.setenv("VCT_HUB_PORT", str(_dead_hub_port()))
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KG_PROJECT_ROOT", raising=False)
    return store


class TestSharedOnlyOpenAIKeyResolution:
    def test_resolves_the_shared_slot(
        self, isolated_state: Path, isolated_resolver_env: Path,
    ) -> None:
        (isolated_resolver_env / "shared" / "openai_api_key").write_text(
            "sk-shared-canary\n", encoding="utf-8"
        )
        mod = _load_summary_backends()
        assert mod._openai_api_key() == "sk-shared-canary"

    def test_never_reads_the_openai_api_key_env_var(
        self, isolated_state: Path, isolated_resolver_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RED-PROOF case (d): pre-change `_openai_api_key` preferred
        `$OPENAI_API_KEY` — the generic env var a project may export. VCO's
        own consumer must resolve only VCO's slot."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-canary")
        mod = _load_summary_backends()
        assert mod._openai_api_key() == ""

    def test_never_reads_a_project_dotenv(
        self, isolated_state: Path, isolated_resolver_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A project `.env` holding the key must not serve VCO's consumer
        (pre-change the legacy positional resolver form fell through to
        tier 3 and returned it)."""
        proj = isolated_resolver_env.parent / "proj-with-key-env"
        proj.mkdir()
        (proj / ".env").write_text("openai_api_key=sk-proj-env\n", encoding="utf-8")
        monkeypatch.setenv("KG_PROJECT_ROOT", str(proj))
        mod = _load_summary_backends()
        assert mod._openai_api_key() == ""
