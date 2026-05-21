# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.22 Item #10 (a) — acceptance property (11) coverage.

Pins the documented machine-class → embedding-preset → model mapping from
the v0.2.21 ship plan §27 (acceptance property (11)).

Property (11) statement (verbatim from
``.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md`` lines 695-700):

  - **High-power (GPU desktop / capable workstation)** → CodeSage-Large
    (2048-dim) for code + qwen3-embedding:0.6b (1024-dim) for text.
  - **Mid-power (Apple Silicon / decent CPU, no CodeSage-capable GPU)** →
    qwen3-embedding for code (1024-dim) + qwen3-embedding for text (or
    arctic if qwen3 is too slow under the user's num_ctx). Both code +
    text on the same model is fine.
  - **Low-power (CPU-only laptop, modest RAM)** → qwen3-embedding for
    code + arctic-embed-l-v2.0 (1024-dim) for text.
  - **Very-low-power (memory-constrained machines)** → Jina-embeddings-
    v2-base-code (768-dim) for code + arctic-embed for text.
  - **Cloud-first** (user sets OpenAI key) → OpenAI embeddings for both.

In the v0.2.21 codebase the 5 plan classes collapse onto 4 concrete
``install.EMBEDDING_CONFIGS`` presets (the v0.2.21 shipped state):

  ============  =====================  ====================================
  Plan class    Preset name            Models (text / code)
  ============  =====================  ====================================
  High-power    ``"gpu"``              qwen3-embedding:0.6b /
                                       codesage-large-v2 (1024 / 2048)
  Mid-power     ``"cpu"`` (hybrid)     qwen3-embedding:0.6b /
                                       unclemusclez/jina-embeddings-v2-
                                       base-code:latest (1024 / 768)
  Low-power     ``"cpu"`` (hybrid)     same as mid-power — no separate
                                       preset (qwen3 + jina). Plan's
                                       "qwen3 code + arctic text" rough
                                       rule is a description, not the
                                       implementation today.
  Very-low      ``"low_resource"``     snowflake-arctic-embed2:latest /
                                       unclemusclez/jina-embeddings-v2-
                                       base-code:latest (1024 / 768)
  Cloud         ``"openai"``           text-embedding-3-small for both
                                       (1536 / 1536)
  ============  =====================  ====================================

These tests pin the mapping at THREE layers so a future refactor must
break all three tests with a clear "machine class X now picks model Y
instead of Z" message — drift across any one layer is caught:

  1. The static ``EMBEDDING_CONFIGS`` table itself (the data).
  2. The decision function ``_choose_embedding_config`` — given a
     ``SystemInfo`` + ``argparse.Namespace``, picks the right preset.
  3. End-to-end: setting the env vars install.py would write for a
     preset → ``EmbeddingService.for_project()`` constructs with the
     documented (slot, dim) per preset.

If you change the mapping intentionally, update this file AND the plan
§27 mapping AND the EMBEDDING_CONFIGS comments AND the GUI Identity tab.

Test environment isolation: no real Ollama / CodeEmbed / OpenAI calls.
All adapter probes are mocked. ``EmbeddingService.for_project`` is
exercised end-to-end including the readiness probes that gate
``NoEmbeddingBackendError``.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter
from vco_lib.embedding_providers.ollama import OllamaAdapter
from vco_lib.embedding_providers.openai import OpenAIAdapter, ValidationResult
from vco_lib.embedding_service import EmbeddingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The five plan classes mapped to the 4 v0.2.21 presets. Tests reference
# this dict by class name so test failures explicitly cite the plan class.
PLAN_CLASS_TO_PRESET: dict[str, str] = {
    "high_power": "gpu",
    "mid_power": "cpu",
    "low_power": "cpu",         # collapses with mid_power in v0.2.21
    "very_low_power": "low_resource",
    "cloud": "openai",
}

# Documented (text_model, text_dim, code_model, code_dim) per preset.
# Source-of-truth: install.EMBEDDING_CONFIGS in install.py — these values
# MUST match. The duplication here is intentional — it forces an explicit
# update to BOTH locations when the mapping changes, surfacing intent.
EXPECTED_PRESET_MAPPING: dict[str, tuple[str, int, str, int]] = {
    "gpu": (
        "qwen3-embedding:0.6b", 1024,
        "codesage-large-v2", 2048,
    ),
    "cpu": (
        "qwen3-embedding:0.6b", 1024,
        "unclemusclez/jina-embeddings-v2-base-code:latest", 768,
    ),
    "low_resource": (
        "snowflake-arctic-embed2:latest", 1024,
        "unclemusclez/jina-embeddings-v2-base-code:latest", 768,
    ),
    "openai": (
        "text-embedding-3-small", 1536,
        "text-embedding-3-small", 1536,
    ),
}

# Documented (text_slot, code_slot) per preset — what EmbeddingService
# .text_vector_slot / .code_vector_slot return when constructed with the
# env vars install.py writes for that preset.
EXPECTED_PRESET_SLOTS: dict[str, tuple[str, str]] = {
    "gpu": ("qwen3_embed", "codesage_embed"),
    # CPU preset writes CODE_EMBED_BACKEND=ollama, so the code path resolves
    # to whatever CODE_EMBED_MODEL says (jina) → jina_embed slot.
    "cpu": ("qwen3_embed", "jina_embed"),
    "low_resource": ("arctic2_embed", "jina_embed"),
    # OpenAI text model maps to the openai_text_embed slot via TEXT_SLOT_MAP.
    "openai": ("openai_text_embed", "openai_code_embed"),
}


def _make_args(
    *,
    cpu_only: bool = False,
    low_resource: bool = False,
    openai_key: str = "",
    no_gpu_check: bool = False,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace that _choose_embedding_config
    inspects. Mirrors the install.py argparse layout — only the fields the
    decision function reads are included.
    """
    return argparse.Namespace(
        cpu_only=cpu_only,
        low_resource=low_resource,
        openai_key=openai_key,
        no_gpu_check=no_gpu_check,
    )


def _make_sysinfo(*, has_gpu: bool, ram_gb: float = 64.0) -> install.SystemInfo:
    """Build a minimal SystemInfo. ``has_gpu`` toggles GPU presence;
    ``ram_gb`` overrides system RAM (default 64.0 to match historical
    test-fixture behaviour). Other fields are filled to satisfy the
    NamedTuple contract — v0.2.23 C10 made the CPU selector RAM- AND
    cores-aware, so tests that exercise the no-GPU path now must
    either tune ram_gb here OR mock `install._probe_cpu_cores`.
    """
    return install.SystemInfo(
        os_name="Linux",
        has_gpu=has_gpu,
        has_metal=False,
        container_cmd="podman",
        gpu_name="NVIDIA RTX 4090" if has_gpu else "",
        vram_gb=24.0 if has_gpu else 0.0,
        ram_gb=ram_gb,
        gpu_vendor="nvidia" if has_gpu else "",
    )


class _EnvScrub:
    """Context manager that scrubs the embedding-related env vars
    EmbeddingService.for_project reads, so each test has a known starting
    point. Restores prior values on exit.
    """

    _KEYS = (
        "ACTIVE_EMBEDDING",
        "EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_MODEL",
        "CODE_EMBED_BACKEND",
        "CODE_EMBED_MODEL",
        "OPENAI_API_KEY",
        "OLLAMA_URL",
        "CODE_EMBED_SERVICE_URL",
    )

    def __enter__(self) -> "_EnvScrub":
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *_a) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _patch_all_adapters_reachable(*, openai_valid: bool = False):
    """Patch the three adapter classes EmbeddingService.for_project
    constructs so the readiness probes pass without real HTTP traffic.
    Returns a tuple of mock objects for callers that want to inspect
    interactions.
    """
    ollama_m = MagicMock(spec=OllamaAdapter)
    ollama_m.is_reachable.return_value = True
    ollama_m.list_embedding_models.return_value = []
    ollama_m.list_models.return_value = []

    code_m = MagicMock(spec=CodeEmbedAdapter)
    code_m.is_reachable.return_value = True
    code_m.base_url = "http://localhost:11440"

    openai_m = MagicMock(spec=OpenAIAdapter)
    openai_m.validate.return_value = ValidationResult(
        valid=openai_valid, reason=None if openai_valid else "no key in test",
    )
    return ollama_m, code_m, openai_m


# ---------------------------------------------------------------------------
# Layer 1: pin the static EMBEDDING_CONFIGS table per machine class
# ---------------------------------------------------------------------------


class EmbeddingConfigsTableTests(unittest.TestCase):
    """Drift guard for the per-machine-class model mapping in
    install.EMBEDDING_CONFIGS. If a value drifts, the test fails with a
    message naming both the plan class AND the preset.
    """

    def _assert_preset(self, plan_class: str) -> None:
        preset_name = PLAN_CLASS_TO_PRESET[plan_class]
        cfg = install.EMBEDDING_CONFIGS[preset_name]
        expected_text, expected_tdim, expected_code, expected_cdim = (
            EXPECTED_PRESET_MAPPING[preset_name]
        )
        self.assertEqual(
            cfg["text_model"], expected_text,
            f"machine class {plan_class!r} (preset {preset_name!r}) now picks "
            f"text_model={cfg['text_model']!r} instead of {expected_text!r}. "
            f"Update plan §27 + this test together if intentional.",
        )
        self.assertEqual(
            cfg["text_dims"], expected_tdim,
            f"machine class {plan_class!r} text_dims drift: {cfg['text_dims']} "
            f"instead of {expected_tdim}",
        )
        self.assertEqual(
            cfg["code_model"], expected_code,
            f"machine class {plan_class!r} (preset {preset_name!r}) now picks "
            f"code_model={cfg['code_model']!r} instead of {expected_code!r}.",
        )
        self.assertEqual(
            cfg["code_dims"], expected_cdim,
            f"machine class {plan_class!r} code_dims drift: {cfg['code_dims']} "
            f"instead of {expected_cdim}",
        )

    def test_high_power_picks_codesage_and_qwen3(self):
        self._assert_preset("high_power")

    def test_mid_power_picks_qwen3_text_and_jina_code(self):
        self._assert_preset("mid_power")

    def test_low_power_collapses_with_mid_today(self):
        # Plan §27 describes a separate "low" class with qwen3 code +
        # arctic text. v0.2.21 collapses it onto the "cpu" preset (same
        # as mid). This test pins the collapse so future work that splits
        # the two classes (e.g. introducing an "arctic_text+qwen3_code"
        # preset) MUST update this test AND the plan together.
        self.assertEqual(PLAN_CLASS_TO_PRESET["low_power"],
                         PLAN_CLASS_TO_PRESET["mid_power"])
        self._assert_preset("low_power")

    def test_very_low_power_picks_arctic_and_jina(self):
        self._assert_preset("very_low_power")

    def test_cloud_picks_openai_for_both(self):
        self._assert_preset("cloud")

    def test_no_unexpected_presets_in_table(self):
        """Pin the preset set itself — adding a preset MUST update this
        file AND PLAN_CLASS_TO_PRESET so the mapping stays explicit."""
        self.assertEqual(
            set(install.EMBEDDING_CONFIGS.keys()),
            {"gpu", "cpu", "low_resource", "openai"},
            "EMBEDDING_CONFIGS keys drifted; update PLAN_CLASS_TO_PRESET",
        )


# ---------------------------------------------------------------------------
# Layer 2: pin _choose_embedding_config's machine-class-to-preset dispatch
# ---------------------------------------------------------------------------


class ChooseEmbeddingConfigDispatchTests(unittest.TestCase):
    """Verify ``install._choose_embedding_config`` picks the right preset
    given a (SystemInfo, args) combination representative of each plan
    class.

    The function consults _load_previous_choices() for "replay-eligible"
    cases — we patch it to return empty so the test sees the
    auto-detection path.
    """

    def setUp(self) -> None:
        # Suppress _record_install_choice (writes to install.jsonl) and
        # _load_previous_choices (reads it) so tests are hermetic.
        self._record_patch = mock.patch.object(
            install, "_record_install_choice", lambda *a, **k: None,
        )
        self._record_patch.start()
        self._load_patch = mock.patch.object(
            install, "_load_previous_choices", lambda: {},
        )
        self._load_patch.start()
        self.addCleanup(self._record_patch.stop)
        self.addCleanup(self._load_patch.stop)

    def _assert_class_picks_preset(
        self, plan_class: str, sysinfo: install.SystemInfo,
        args: argparse.Namespace,
    ) -> None:
        expected_preset = PLAN_CLASS_TO_PRESET[plan_class]
        cfg = install._choose_embedding_config(sysinfo, args)
        # Identify by the canonical text/code-model tuple — the dict the
        # function returns is a copy of EMBEDDING_CONFIGS[preset] with
        # optional openai_key added, so equality-of-mapping is the cleanest
        # way to assert "this is preset X".
        expected_text, _, expected_code, _ = EXPECTED_PRESET_MAPPING[expected_preset]
        self.assertEqual(
            cfg["text_model"], expected_text,
            f"machine class {plan_class!r} expected preset "
            f"{expected_preset!r} (text_model={expected_text!r}); got "
            f"{cfg.get('text_model')!r}",
        )
        self.assertEqual(cfg["code_model"], expected_code)

    def test_high_power_gpu_autodetect_picks_gpu_preset(self):
        """GPU detected, no CLI overrides → high-power class → gpu preset."""
        self._assert_class_picks_preset(
            "high_power",
            sysinfo=_make_sysinfo(has_gpu=True),
            args=_make_args(),
        )

    def test_mid_power_no_gpu_autodetect_picks_cpu_preset(self):
        """v0.2.23 C10 made the no-GPU path tier-aware. With 64GB RAM +
        8 cores, the new selectors surgically OVERRIDE the legacy
        `cpu` preset's code_model from Jina to qwen3 (per user spec:
        24+GB RAM AND 8+ cores → qwen3 on CPU). Assert explicit models
        rather than preset-mapping to stay accurate post-C10."""
        with patch.object(install, "_probe_cpu_cores", return_value=8):
            cfg = install._choose_embedding_config(
                _make_sysinfo(has_gpu=False, ram_gb=64.0),
                _make_args(),
            )
        # Tier-bumped surgical overrides (C10):
        self.assertEqual(cfg["text_model"], "qwen3-embedding:0.6b")
        self.assertEqual(cfg["code_model"], "qwen3-embedding:0.6b")

    def test_low_spec_cpu_autodetect_picks_arctic_and_jina(self):
        """v0.2.23 C10 — added: low-spec CPU (no GPU, <24GB OR <8 cores)
        now auto-picks arctic2 (text) + Jina (code). Was only reachable
        via explicit --low-resource pre-C10."""
        with patch.object(install, "_probe_cpu_cores", return_value=4):
            cfg = install._choose_embedding_config(
                _make_sysinfo(has_gpu=False, ram_gb=16.0),
                _make_args(),
            )
        self.assertEqual(cfg["text_model"], "snowflake-arctic-embed2:latest")
        self.assertEqual(
            cfg["code_model"],
            "unclemusclez/jina-embeddings-v2-base-code:latest",
        )

    def test_cpu_only_flag_forces_cpu_preset_even_with_gpu(self):
        """--cpu-only overrides GPU detection → cpu preset."""
        self._assert_class_picks_preset(
            "mid_power",  # cpu preset == mid/low class
            sysinfo=_make_sysinfo(has_gpu=True),
            args=_make_args(cpu_only=True),
        )

    def test_very_low_power_low_resource_flag_picks_low_resource_preset(self):
        """--low-resource → very-low-power class → low_resource preset."""
        self._assert_class_picks_preset(
            "very_low_power",
            sysinfo=_make_sysinfo(has_gpu=False),
            args=_make_args(low_resource=True),
        )

    def test_cloud_openai_key_picks_openai_preset(self):
        """--openai-key=... → cloud class → openai preset."""
        self._assert_class_picks_preset(
            "cloud",
            sysinfo=_make_sysinfo(has_gpu=True),  # even with GPU
            args=_make_args(openai_key="sk-fake-test"),
        )

    def test_openai_key_carried_through_into_config(self):
        """Cloud preset must propagate the openai_key into the returned
        config so downstream env-var emitters can write it."""
        sysinfo = _make_sysinfo(has_gpu=False)
        args = _make_args(openai_key="sk-fake-roundtrip")
        cfg = install._choose_embedding_config(sysinfo, args)
        self.assertEqual(cfg.get("openai_key"), "sk-fake-roundtrip")


# ---------------------------------------------------------------------------
# Layer 3: end-to-end — preset env vars → EmbeddingService instantiation
# ---------------------------------------------------------------------------


class PresetToEmbeddingServiceParityTests(unittest.TestCase):
    """End-to-end drift guard: for each preset, write the env vars install.py
    would write, instantiate EmbeddingService.for_project(), assert the
    resolved (slot, dim, model_id) matches the documented mapping.

    This is the test that catches the actual customer-visible failure:
    if install.py writes one model but EmbeddingService resolves a
    different slot/dim, embeddings land in the wrong named-vector and
    queries return zero hits.
    """

    def _env_for_preset(self, preset_name: str) -> dict[str, str]:
        """Replicate the env-var emission install.py does for a given
        preset (see install.py lines ~12364, 12491 for the canonical
        emission sites).
        """
        cfg = install.EMBEDDING_CONFIGS[preset_name]
        env = {
            "EMBEDDING_MODEL": cfg["text_model"],
            "CODE_EMBED_BACKEND": cfg["code_backend"],
            "CODE_EMBED_MODEL": cfg["code_model"],
            "ACTIVE_EMBEDDING": cfg.get("active_embedding", "qwen3"),
        }
        if preset_name == "openai":
            # install.py would set OPENAI_API_KEY too — required for the
            # openai readiness probe to fire.
            env["OPENAI_API_KEY"] = "sk-fake-test-key"
        return env

    def _assert_preset_constructs_correctly(
        self, plan_class: str, *, openai_valid: bool = False,
    ) -> None:
        preset_name = PLAN_CLASS_TO_PRESET[plan_class]
        expected_text, expected_tdim, expected_code, expected_cdim = (
            EXPECTED_PRESET_MAPPING[preset_name]
        )
        expected_tslot, expected_cslot = EXPECTED_PRESET_SLOTS[preset_name]

        env = self._env_for_preset(preset_name)
        ollama_m, code_m, oa_m = _patch_all_adapters_reachable(
            openai_valid=openai_valid,
        )

        with _EnvScrub(), patch.dict(os.environ, env, clear=False):
            with patch("vco_lib.embedding_service.OllamaAdapter",
                       return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter",
                       return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter",
                       return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(
                        svc.text_model_id, expected_text,
                        f"{plan_class} preset {preset_name}: "
                        f"text_model_id drift",
                    )
                    self.assertEqual(
                        svc.text_dim, expected_tdim,
                        f"{plan_class} preset {preset_name}: text_dim drift "
                        f"({svc.text_dim} vs expected {expected_tdim})",
                    )
                    self.assertEqual(
                        svc.text_vector_slot, expected_tslot,
                        f"{plan_class} preset {preset_name}: text_vector_slot "
                        f"drift ({svc.text_vector_slot} vs expected "
                        f"{expected_tslot})",
                    )
                    self.assertEqual(
                        svc.code_model_id, expected_code,
                        f"{plan_class} preset {preset_name}: code_model_id "
                        f"drift",
                    )
                    self.assertEqual(
                        svc.code_dim, expected_cdim,
                        f"{plan_class} preset {preset_name}: code_dim drift "
                        f"({svc.code_dim} vs expected {expected_cdim})",
                    )
                    self.assertEqual(
                        svc.code_vector_slot, expected_cslot,
                        f"{plan_class} preset {preset_name}: code_vector_slot "
                        f"drift ({svc.code_vector_slot} vs expected "
                        f"{expected_cslot})",
                    )
                finally:
                    svc.close()

    def test_high_power_end_to_end(self):
        self._assert_preset_constructs_correctly("high_power")

    def test_mid_power_end_to_end(self):
        self._assert_preset_constructs_correctly("mid_power")

    def test_very_low_power_end_to_end(self):
        self._assert_preset_constructs_correctly("very_low_power")

    def test_cloud_end_to_end(self):
        # Cloud preset requires a valid OpenAI key for the readiness
        # probe to pass. ``openai_valid=True`` makes the adapter mock
        # report success without an actual network call.
        self._assert_preset_constructs_correctly("cloud", openai_valid=True)


if __name__ == "__main__":
    unittest.main()
