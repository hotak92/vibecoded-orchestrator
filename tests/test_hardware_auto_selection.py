# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tier-boundary tests for v0.2.23 C10 hardware-aware selectors.

The three selectors in `install.py` are pure decision functions that
take detected hardware (VRAM, RAM, CPU cores) + capability flags
(OpenAI key, Claude CLI, consent) and return a backend ID. Tests sweep
each spec'd tier boundary and confirm the right model name comes back.

Spec source: 2026-05-21 user spec. README hardware table lists the
exact tier → model mapping (Code / KG / Summaries sections).

Boundary semantics:
  - Most "X+" thresholds in the spec → inclusive `>=` in the code.
  - The 2 GB GPU code boundary is strict `>` (per spec "code, VRAM > 2 GB"
    → Jina; "≤ 2 GB" → CPU path).
  - **v0.2.49 strict-`>` boundaries** (after post-update validation on a contributor's
    24 GB Windows box, Bug M — qwen3-embedding on CPU-only Ollama
    was ~30s per embedding at the 24 GB boundary):
      * KG embedding: VRAM `>` 8 GB → qwen3 (was `>=`)
      * KG embedding: RAM `>` 24 GB AND cores >= 8 → qwen3 (was `>=`)
      * Code embedding: RAM `>` 24 GB AND cores >= 8 → qwen3 (was `>=`)
  - **v0.2.49 cores counting**: `_probe_cpu_cores` now returns
    PHYSICAL cores (was logical/SMT). See its docstring.
"""

from __future__ import annotations

import pytest

from install import (
    select_code_embedding_backend,
    select_kg_embedding_backend,
    select_summary_backend,
    _CODE_BACKEND_CODESAGE,
    _CODE_BACKEND_QWEN3,
    _CODE_BACKEND_JINA,
    _CODE_BACKEND_OPENAI,
    _KG_BACKEND_QWEN3,
    _KG_BACKEND_ARCTIC,
    _KG_BACKEND_OPENAI,
    _SUMMARY_BACKEND_CLI,
    _SUMMARY_BACKEND_QWEN35_9B,
    _SUMMARY_BACKEND_GEMMA,
    _SUMMARY_BACKEND_OPENAI,
    _decide_reconciled_active_embedding,
    _reconcile_install_active_embedding,
)


# ────────────────────────────────────────────────────────────────────
# Code embedding selector — tier ladder
# ────────────────────────────────────────────────────────────────────

class TestCodeEmbeddingSelector:
    """select_code_embedding_backend"""

    @pytest.mark.parametrize(
        "vram",
        [12.0, 16.0, 24.0, 80.0],
    )
    def test_gpu_12gb_plus_picks_codesage(self, vram: float) -> None:
        """VRAM >= 12 GB → CodeSage-Large-v2 (workstation-class)."""
        got = select_code_embedding_backend(
            gpu_vram_gb=vram, ram_gb=64.0, cores=16,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_CODESAGE

    @pytest.mark.parametrize("vram", [6.0, 8.0, 11.9])
    def test_gpu_6_to_12gb_picks_qwen3(self, vram: float) -> None:
        """6 GB <= VRAM < 12 GB → qwen3-embedding."""
        got = select_code_embedding_backend(
            gpu_vram_gb=vram, ram_gb=32.0, cores=8,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_QWEN3

    @pytest.mark.parametrize("vram", [2.5, 4.0, 5.9])
    def test_gpu_above_2gb_below_6gb_picks_jina(self, vram: float) -> None:
        """2 GB < VRAM < 6 GB → Jina v2 base-code."""
        got = select_code_embedding_backend(
            gpu_vram_gb=vram, ram_gb=16.0, cores=8,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_gpu_exactly_2gb_falls_to_cpu_path(self) -> None:
        """VRAM == 2 GB is below the strict `> 2 GB` threshold → CPU path.

        With low RAM/cores → Jina (the CPU floor).
        """
        got = select_code_embedding_backend(
            gpu_vram_gb=2.0, ram_gb=8.0, cores=4,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_no_gpu_high_ram_high_cores_picks_qwen3(self) -> None:
        """CPU + RAM > 24 GB AND cores >= 8 → qwen3-embedding."""
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=32.0, cores=12,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_QWEN3

    def test_no_gpu_exactly_24gb_ram_picks_jina(self) -> None:
        """v0.2.49: strict-> on RAM boundary. EXACTLY 24 GB → jina.

        Pre-v0.2.49 this tier-up'd to qwen3 at the 24 GB boundary, but
        post-update validation on a contributor's 24 GB Windows box (Bug M) showed
        qwen3-embedding on CPU-only Ollama is ~30s per embedding at
        the boundary. The strict-> rule moves 24 GB hosts to the
        lighter tier.
        """
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=24.0, cores=8,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_no_gpu_above_24gb_picks_qwen3(self) -> None:
        """v0.2.49: strict-> means 24.1 GB qualifies for qwen3."""
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=24.1, cores=8,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_QWEN3

    def test_no_gpu_high_ram_low_cores_falls_to_jina(self) -> None:
        """RAM qualifies but cores < 8 → fall through to Jina."""
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=64.0, cores=4,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_no_gpu_low_ram_high_cores_falls_to_jina(self) -> None:
        """Cores qualify but RAM <= 24 GB → fall through to Jina."""
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=16.0, cores=16,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_no_gpu_low_everything_falls_to_jina(self) -> None:
        """Minimum-spec host: Jina via Ollama is the universal floor."""
        got = select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            openai_key_available=False,
        )
        assert got == _CODE_BACKEND_JINA

    def test_openai_override_picks_openai_when_key_available(self) -> None:
        got = select_code_embedding_backend(
            gpu_vram_gb=24.0, ram_gb=64.0, cores=16,
            openai_key_available=True,
            prefer_openai=True,
        )
        assert got == _CODE_BACKEND_OPENAI

    def test_openai_override_without_key_ignored(self) -> None:
        """prefer_openai=True but no key → falls back to hardware-detect."""
        got = select_code_embedding_backend(
            gpu_vram_gb=24.0, ram_gb=64.0, cores=16,
            openai_key_available=False,
            prefer_openai=True,
        )
        assert got == _CODE_BACKEND_CODESAGE

    def test_openai_key_available_but_not_preferred_ignored(self) -> None:
        """Key present but prefer_openai=False → never auto-picks OpenAI."""
        got = select_code_embedding_backend(
            gpu_vram_gb=24.0, ram_gb=64.0, cores=16,
            openai_key_available=True,
            prefer_openai=False,
        )
        assert got == _CODE_BACKEND_CODESAGE


# ────────────────────────────────────────────────────────────────────
# KG / text embedding selector — tier ladder
# ────────────────────────────────────────────────────────────────────

class TestKgEmbeddingSelector:
    """select_kg_embedding_backend"""

    @pytest.mark.parametrize("vram", [8.1, 12.0, 24.0])
    def test_gpu_above_8gb_picks_qwen3(self, vram: float) -> None:
        """VRAM > 8 GB → qwen3-embedding (1024-dim).

        v0.2.49: strict-> on the 8 GB boundary. EXACTLY 8 GB now lands
        in arctic (see test_gpu_exactly_8gb_picks_arctic below). The
        margin matters when other GPU workloads (code embedder, summary
        inference) share VRAM at the 8 GB boundary.
        """
        got = select_kg_embedding_backend(
            gpu_vram_gb=vram, ram_gb=32.0, cores=8,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_QWEN3

    @pytest.mark.parametrize("vram", [4.0, 6.0, 7.9, 8.0])
    def test_gpu_4_to_8gb_picks_arctic(self, vram: float) -> None:
        """4 GB <= VRAM <= 8 GB → arctic2 (same dims, smaller footprint).

        v0.2.49: inclusive at 8 GB now (was strict-< 8 GB). The 8 GB
        boundary tier-up to qwen3 required strict-> after post-update validation
        showed boundary cards crowd VRAM when co-loading other models.
        """
        got = select_kg_embedding_backend(
            gpu_vram_gb=vram, ram_gb=16.0, cores=8,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_ARCTIC

    def test_gpu_below_4gb_treated_as_cpu(self) -> None:
        """VRAM < 4 GB drops to CPU path. With low RAM → arctic."""
        got = select_kg_embedding_backend(
            gpu_vram_gb=2.0, ram_gb=8.0, cores=4,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_ARCTIC

    def test_no_gpu_high_ram_high_cores_picks_qwen3(self) -> None:
        """CPU + RAM > 24 GB AND cores >= 8 → qwen3-embedding."""
        got = select_kg_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=32.0, cores=16,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_QWEN3

    def test_no_gpu_exactly_24gb_ram_picks_arctic(self) -> None:
        """v0.2.49: strict-> on RAM boundary. EXACTLY 24 GB → arctic.

        Pre-v0.2.49 this tier-up'd to qwen3 at the 24 GB boundary, but
        post-update validation on a contributor's 24 GB Windows box (Bug M) showed
        qwen3-embedding on CPU-only Ollama is ~30s per embedding at
        the boundary — unusable for KG indexing. The strict-> rule
        moves 24 GB hosts to arctic + jina (low_resource profile).
        """
        got = select_kg_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=24.0, cores=8,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_ARCTIC

    def test_no_gpu_above_24gb_picks_qwen3(self) -> None:
        """v0.2.49: strict-> means 24.1 GB qualifies for qwen3."""
        got = select_kg_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=24.1, cores=8,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_QWEN3

    def test_no_gpu_low_ram_picks_arctic(self) -> None:
        got = select_kg_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=8.0, cores=4,
            openai_key_available=False,
        )
        assert got == _KG_BACKEND_ARCTIC

    def test_openai_override_picks_openai(self) -> None:
        got = select_kg_embedding_backend(
            gpu_vram_gb=24.0, ram_gb=64.0, cores=16,
            openai_key_available=True,
            prefer_openai=True,
        )
        assert got == _KG_BACKEND_OPENAI


# ────────────────────────────────────────────────────────────────────
# Summary backend selector — tier ladder + None case
# ────────────────────────────────────────────────────────────────────

class TestSummaryBackendSelector:
    """select_summary_backend"""

    def test_claude_cli_always_wins_when_available(self) -> None:
        """CLI is the highest-quality + no-cost option → unconditional pick."""
        # Test against multiple hardware tiers — CLI should win every time.
        for vram, ram, cores in [
            (0.0, 4.0, 2),    # ultra-low-end
            (24.0, 64.0, 16), # workstation
            (8.0, 16.0, 8),   # mid-range
        ]:
            got = select_summary_backend(
                gpu_vram_gb=vram, ram_gb=ram, cores=cores,
                claude_cli_available=True,
                openai_consent=False,
            )
            assert got == _SUMMARY_BACKEND_CLI, (
                f"CLI should win at vram={vram}, ram={ram}, cores={cores}"
            )

    @pytest.mark.parametrize("vram", [16.0, 24.0, 80.0])
    def test_gpu_16gb_plus_picks_qwen35_9b(self, vram: float) -> None:
        """VRAM >= 16 GB → qwen3.5:9b (highest local quality)."""
        got = select_summary_backend(
            gpu_vram_gb=vram, ram_gb=32.0, cores=16,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got == _SUMMARY_BACKEND_QWEN35_9B

    @pytest.mark.parametrize("vram", [6.0, 8.0, 12.0, 15.9])
    def test_gpu_6_to_16gb_picks_gemma(self, vram: float) -> None:
        """6 GB <= VRAM < 16 GB → gemma4:e4b."""
        got = select_summary_backend(
            gpu_vram_gb=vram, ram_gb=8.0, cores=4,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got == _SUMMARY_BACKEND_GEMMA

    def test_no_gpu_high_ram_high_cores_picks_gemma(self) -> None:
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=16.0, cores=8,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got == _SUMMARY_BACKEND_GEMMA

    def test_no_gpu_exactly_12gb_ram_exactly_6_cores_picks_gemma(self) -> None:
        """Boundary: 12 GB RAM AND 6 cores → gemma viable."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=12.0, cores=6,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got == _SUMMARY_BACKEND_GEMMA

    def test_no_gpu_low_ram_no_consent_returns_none(self) -> None:
        """RAM < 12 GB AND no CLI AND no consent → None (no path viable)."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=8.0, cores=4,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got is None

    def test_no_gpu_low_cores_no_consent_returns_none(self) -> None:
        """Cores < 6 → can't run gemma → None."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=16.0, cores=4,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got is None

    def test_openai_consent_without_key_falls_through_to_none(self) -> None:
        """Consent granted but no key → still None (no usable backend)."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            claude_cli_available=False,
            openai_consent=True,
            openai_key_available=False,
        )
        assert got is None

    def test_openai_key_without_consent_returns_none(self) -> None:
        """Key available but consent withheld → still None."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            claude_cli_available=False,
            openai_consent=False,
            openai_key_available=True,
        )
        assert got is None

    def test_openai_consent_with_key_picks_openai_as_last_resort(self) -> None:
        """Sub-tier hardware + consent + key → openai is the last-resort path."""
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            claude_cli_available=False,
            openai_consent=True,
            openai_key_available=True,
        )
        assert got == _SUMMARY_BACKEND_OPENAI

    def test_local_gemma_preferred_over_openai_when_both_viable(self) -> None:
        """Hardware can run gemma → don't reach for the paid OpenAI tier.

        The spec puts OpenAI strictly after the local tiers; consent
        is a permission to USE OpenAI as a fallback, not a preference
        to ALWAYS use it.
        """
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=16.0, cores=8,
            claude_cli_available=False,
            openai_consent=True,
            openai_key_available=True,
        )
        assert got == _SUMMARY_BACKEND_GEMMA

    def test_zero_inputs_returns_none_or_safe_default(self) -> None:
        """Defensive: all-zero probes (probe failures) → None.

        This is the "minimum viable signal" path — when every detection
        helper returned 0 (because nvidia-smi flapped, psutil missing,
        etc.), we can't claim any local backend is viable.
        """
        got = select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=0.0, cores=0,
            claude_cli_available=False,
            openai_consent=False,
        )
        assert got is None


# ────────────────────────────────────────────────────────────────────
# CPU-cores probe — soft-fail behaviour
# ────────────────────────────────────────────────────────────────────

class TestCpuCoresProbe:
    def test_probe_returns_positive_int_on_normal_host(self) -> None:
        """psutil / os.cpu_count() should always return >= 1 on a real host."""
        from install import _probe_cpu_cores

        n = _probe_cpu_cores()
        # Test environment must have at least 1 core. 0 would mean both
        # psutil failed AND os.cpu_count() returned None — implausible
        # outside contrived containerised CI.
        assert isinstance(n, int)
        assert n >= 1


# ────────────────────────────────────────────────────────────────────
# v0.2.61 — stale ACTIVE_EMBEDDING reconciliation
# ────────────────────────────────────────────────────────────────────
#
# Background: a clone updated v0.2.51→v0.2.60 on a CPU / low-RAM box
# carried a STALE settings.json/.env ACTIVE_EMBEDDING=qwen3 written by an
# OLD install (pre hardware-aware selectors). On that hardware the KG
# selector picks arctic, but the stale qwen3 won at priority #1 in the
# resolver + the writers re-cemented it every update → KG sync timed out
# (qwen3-embedding on CPU-only Ollama ~30-60s per embedding).
#
# The reconcile is intentionally NARROW: it only fires on the exact
# stale-qwen3-vs-arctic shape and never touches a deliberate user choice
# (CLI flag / launcher.db) nor any other hardware tier.


class TestDecideReconciledActiveEmbedding:
    """Pure decision function: `_decide_reconciled_active_embedding`."""

    def test_stale_qwen3_cpu_arctic_reconciled(self) -> None:
        """(a) stale qwen3 + CPU-hardware (arctic) + no deliberate → reconcile."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="qwen3",
            hardware_active="arctic",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("arctic", True)

    def test_deliberate_choice_never_reconciled(self) -> None:
        """(b/c) a deliberate choice (CLI flag OR launcher.db) is honoured.

        Even on the stale-qwen3-vs-arctic shape, deliberate_choice=True
        means the user pinned the mode — return the env value untouched.
        """
        resolved, was = _decide_reconciled_active_embedding(
            env_active="qwen3",
            hardware_active="arctic",
            deliberate_choice=True,
        )
        assert (resolved, was) == ("qwen3", False)

    def test_gpu_hardware_qwen3_not_reconciled(self) -> None:
        """(d) GPU host: env qwen3 == hardware qwen3 → no-op (qwen3 correct)."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="qwen3",
            hardware_active="qwen3",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("qwen3", False)

    def test_empty_env_uses_hardware_pick(self) -> None:
        """No env value → use the hardware pick, but that's not a 'reconcile'."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="",
            hardware_active="arctic",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("arctic", False)

    def test_none_env_uses_hardware_pick(self) -> None:
        resolved, was = _decide_reconciled_active_embedding(
            env_active=None,
            hardware_active="qwen3",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("qwen3", False)

    def test_explicit_non_default_mismatch_honoured(self) -> None:
        """Row 5: env=arctic but hardware=qwen3 is a legit override → honour env.

        The user (or a prior deliberate install) chose arctic on a host the
        current detector would put on qwen3. NOT the known-failure shape, so
        we must NOT clobber it.
        """
        resolved, was = _decide_reconciled_active_embedding(
            env_active="arctic",
            hardware_active="qwen3",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("arctic", False)

    def test_explicit_openai_env_honoured(self) -> None:
        """env=openai on arctic hardware is an explicit override → honour env."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="openai",
            hardware_active="arctic",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("openai", False)

    def test_idempotent_after_reconcile(self) -> None:
        """Once the env holds arctic (post-reconcile), re-running is a no-op."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="arctic",
            hardware_active="arctic",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("arctic", False)

    def test_case_and_whitespace_normalised(self) -> None:
        """Mixed-case / padded stale value still matches the known shape."""
        resolved, was = _decide_reconciled_active_embedding(
            env_active="  QWEN3  ",
            hardware_active="ARCTIC",
            deliberate_choice=False,
        )
        assert (resolved, was) == ("arctic", True)


class _StubArgs:
    """Minimal argparse.Namespace stand-in for the reconcile wrapper."""

    def __init__(self, *, openai_key=None, low_resource=False, cpu_only=False):
        self.openai_key = openai_key
        self.low_resource = low_resource
        self.cpu_only = cpu_only


class TestReconcileInstallActiveEmbedding:
    """Impure wrapper: `_reconcile_install_active_embedding` (env + launcher.db)."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """Each test controls ACTIVE_EMBEDDING / EMBEDDING_MODEL explicitly."""
        monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        # Default: no launcher.db deliberate choice unless a test sets one.
        monkeypatch.setattr(
            "install._read_active_embedding_from_app_state",
            lambda: None,
        )
        yield

    def test_a_stale_qwen3_cpu_no_choice_reconciled_to_arctic(self, monkeypatch):
        """(a) stale qwen3 + CPU-hardware + no deliberate choice → arctic.

        Rewrites os.environ ACTIVE_EMBEDDING + EMBEDDING_MODEL to arctic.
        """
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "arctic"}  # low_resource profile pick
        did = _reconcile_install_active_embedding(embed_config, _StubArgs())
        assert did is True
        import os as _os
        assert _os.environ["ACTIVE_EMBEDDING"] == "arctic"
        assert _os.environ["EMBEDDING_MODEL"] == "snowflake-arctic-embed2:latest"

    def test_b_deliberate_launcher_db_choice_not_reconciled(self, monkeypatch):
        """(b) launcher.db embedding.active_profile set → NOT reconciled."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.setattr(
            "install._read_active_embedding_from_app_state",
            lambda: "qwen3",
        )
        embed_config = {"active_embedding": "arctic"}
        did = _reconcile_install_active_embedding(embed_config, _StubArgs())
        assert did is False
        import os as _os
        # env left untouched — the GUI choice wins byte-for-byte.
        assert _os.environ["ACTIVE_EMBEDDING"] == "qwen3"
        assert "EMBEDDING_MODEL" not in _os.environ

    def test_c_cli_flag_not_reconciled(self, monkeypatch):
        """(c) a CLI flag (--low-resource) → NOT reconciled."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "arctic"}
        did = _reconcile_install_active_embedding(
            embed_config, _StubArgs(low_resource=True),
        )
        assert did is False
        import os as _os
        assert _os.environ["ACTIVE_EMBEDDING"] == "qwen3"

    def test_c_cpu_only_flag_not_reconciled(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "arctic"}
        did = _reconcile_install_active_embedding(
            embed_config, _StubArgs(cpu_only=True),
        )
        assert did is False

    def test_c_openai_key_flag_not_reconciled(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "arctic"}
        did = _reconcile_install_active_embedding(
            embed_config, _StubArgs(openai_key="sk-test"),
        )
        assert did is False

    def test_d_gpu_hardware_qwen3_not_reconciled(self, monkeypatch):
        """(d) GPU host: env qwen3 + hardware qwen3 → no-op (qwen3 correct)."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "qwen3"}  # gpu/cpu profile pick
        did = _reconcile_install_active_embedding(embed_config, _StubArgs())
        assert did is False
        import os as _os
        assert _os.environ["ACTIVE_EMBEDDING"] == "qwen3"

    def test_no_env_value_is_noop(self, monkeypatch):
        """No inherited env → nothing to reconcile (writers use embed_config)."""
        embed_config = {"active_embedding": "arctic"}
        did = _reconcile_install_active_embedding(embed_config, _StubArgs())
        assert did is False
        import os as _os
        assert "ACTIVE_EMBEDDING" not in _os.environ

    def test_explicit_non_default_env_honoured(self, monkeypatch):
        """env=arctic on qwen3 hardware is a legit override → not reconciled."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
        embed_config = {"active_embedding": "qwen3"}
        did = _reconcile_install_active_embedding(embed_config, _StubArgs())
        assert did is False
        import os as _os
        assert _os.environ["ACTIVE_EMBEDDING"] == "arctic"

    def test_idempotent_second_run(self, monkeypatch):
        """After a reconcile sets env=arctic, a second call is a no-op."""
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        embed_config = {"active_embedding": "arctic"}
        assert _reconcile_install_active_embedding(embed_config, _StubArgs()) is True
        # env now arctic; re-run must not "re-reconcile".
        assert _reconcile_install_active_embedding(embed_config, _StubArgs()) is False
