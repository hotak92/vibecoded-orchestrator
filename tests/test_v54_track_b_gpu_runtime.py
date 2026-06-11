# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track B — container/GPU runtime unification (install.py side).

Covers:
  * gpu-audit C-5 — `_apply_tier_overrides` keeps `code_dims` /
    `text_dims` / `active_embedding` / pull list in lockstep with the
    swapped model (pre-v0.2.54 only the model NAME was replaced, so
    ``CODE_EMBED_DIMS=768`` was written for the 1024-dim qwen3 pick).
  * gpu-audit C-2 — AMD >=12 GB hosts are routed OFF the "gpu"
    (CodeSage) profile: CodeSage has no ROCm build path; the previous
    behavior labeled CPU inference "GPU-accelerated".
  * gpu-audit C-3 — `VCT_GPU_VENDOR` now has a reader
    (`_gpu_vendor_preference_from_env`); the remediation messages that
    advertised it are no longer a no-op.
  * gpu-audit C-4 — compose ``${...}`` substitution keys
    (CODE_EMBED_BACKEND / CODE_EMBED_DOCKERFILE) are computed for the
    compose project dir (`infrastructure/.env`) + process env instead
    of the never-read PROJECT_ROOT/.env.
  * container-scout C-RT-4 / gpu-audit C-6 — `podman-compose.rocm.yml`
    actually ships, carries the rocBLAS workarounds, and is the
    candidate install.py probes first for podman+AMD.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


class ApplyTierOverridesTests(unittest.TestCase):
    """gpu-audit C-5 — dependent fields follow the model swap."""

    def test_code_model_swap_updates_code_dims(self):
        # The empirical bug shape: cpu profile (jina, 768) + qwen3 pick
        # (6-12 GB GPU host) left code_dims at 768 for a 1024-dim model.
        config = dict(install.EMBEDDING_CONFIGS["cpu"])
        install._apply_tier_overrides(
            config,
            code_pick="qwen3-embedding:0.6b",
            kg_pick=config["text_model"],
        )
        self.assertEqual(config["code_model"], "qwen3-embedding:0.6b")
        self.assertEqual(config["code_dims"], 1024)

    def test_text_model_swap_updates_dims_and_active_embedding(self):
        # gpu profile stock text model is qwen3 / slot "qwen3"; an
        # arctic KG pick must re-point the named-vector slot too.
        config = dict(install.EMBEDDING_CONFIGS["gpu"])
        install._apply_tier_overrides(
            config,
            code_pick=config["code_model"],
            kg_pick="snowflake-arctic-embed2:latest",
        )
        self.assertEqual(config["text_model"], "snowflake-arctic-embed2:latest")
        self.assertEqual(config["text_dims"], 1024)
        self.assertEqual(config["active_embedding"], "arctic")

    def test_override_appends_ollama_model_to_pull_list(self):
        # low_resource pull list is [arctic, jina]; a qwen3 code
        # override must land qwen3 in the pull list or the host embeds
        # against a model Ollama never pulled.
        config = dict(install.EMBEDDING_CONFIGS["low_resource"])
        install._apply_tier_overrides(
            config,
            code_pick="qwen3-embedding:0.6b",
            kg_pick=config["text_model"],
        )
        self.assertIn("qwen3-embedding:0.6b", config["embedding_models"])

    def test_override_does_not_mutate_shared_profile(self):
        # config is a SHALLOW copy of the module-level profile dict —
        # the pull-list append must be copy-on-write.
        before = list(install.EMBEDDING_CONFIGS["low_resource"]["embedding_models"])
        config = dict(install.EMBEDDING_CONFIGS["low_resource"])
        install._apply_tier_overrides(
            config,
            code_pick="qwen3-embedding:0.6b",
            kg_pick=config["text_model"],
        )
        self.assertEqual(
            install.EMBEDDING_CONFIGS["low_resource"]["embedding_models"],
            before,
            "tier override must not mutate the shared EMBEDDING_CONFIGS profile",
        )

    def test_noop_when_picks_match_stock(self):
        config = dict(install.EMBEDDING_CONFIGS["cpu"])
        snapshot = dict(config)
        install._apply_tier_overrides(
            config,
            code_pick=config["code_model"],
            kg_pick=config["text_model"],
        )
        self.assertEqual(config, snapshot)


class AmdVendorGateTests(unittest.TestCase):
    """gpu-audit C-2 — AMD >=12 GB must not land on the CodeSage profile."""

    def _choose(self, vendor: str, vram: float) -> dict:
        sysinfo = install.SystemInfo(
            os_name="Linux",
            has_gpu=True,
            has_metal=False,
            container_cmd="podman",
            gpu_name="Test GPU",
            vram_gb=vram,
            ram_gb=32.0,
            gpu_vendor=vendor,
        )
        args = mock.Mock(
            openai_key=None, low_resource=False, cpu_only=False, gpu=False,
        )
        with mock.patch.object(install, "_load_previous_choices", return_value={}), \
             mock.patch.object(install, "_record_install_choice"), \
             mock.patch.object(install, "_probe_cpu_cores", return_value=16):
            return install._choose_embedding_config(sysinfo, args)

    def test_amd_16gb_routed_off_codesage(self):
        config = self._choose("amd", 16.0)
        self.assertNotEqual(config["code_model"], "codesage-large-v2")
        self.assertEqual(config["code_backend"], "ollama")
        # The qwen3 tier replaces CodeSage for AMD; dims must follow (C-5).
        self.assertEqual(config["code_model"], "qwen3-embedding:0.6b")
        self.assertEqual(config["code_dims"], 1024)

    def test_nvidia_16gb_still_gets_codesage_gpu_profile(self):
        config = self._choose("nvidia", 16.0)
        self.assertEqual(config["code_model"], "codesage-large-v2")
        self.assertEqual(config["code_backend"], "gpu")
        self.assertEqual(config["code_dims"], 2048)


class GpuVendorEnvPreferenceTests(unittest.TestCase):
    """gpu-audit C-3 — VCT_GPU_VENDOR finally has a reader."""

    def _with_env(self, value):
        return mock.patch.dict(os.environ, {"VCT_GPU_VENDOR": value})

    def test_recognized_values(self):
        for raw, expected in [
            ("amd", "amd"), ("NVIDIA", "nvidia"), ("  Metal ", "metal"),
        ]:
            with self._with_env(raw):
                self.assertEqual(
                    install._gpu_vendor_preference_from_env(), expected,
                )

    def test_auto_and_empty_mean_no_preference(self):
        for raw in ("", "auto", "AUTO"):
            with self._with_env(raw):
                self.assertIsNone(install._gpu_vendor_preference_from_env())

    def test_unknown_value_ignored(self):
        with self._with_env("intel"):
            self.assertIsNone(install._gpu_vendor_preference_from_env())

    def test_unset_means_no_preference(self):
        env = {k: v for k, v in os.environ.items() if k != "VCT_GPU_VENDOR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(install._gpu_vendor_preference_from_env())


class ComposeSubstitutionEnvTests(unittest.TestCase):
    """gpu-audit C-4 — substitution keys reach compose."""

    def test_nvidia_gets_cuda_dockerfile(self):
        env = install._compose_substitution_env(
            {"code_backend": "gpu", "gpu_vendor": "nvidia"}
        )
        self.assertEqual(env.get("CODE_EMBED_DOCKERFILE"), "Dockerfile.cuda")
        self.assertEqual(env.get("CODE_EMBED_BACKEND"), "gpu")

    def test_amd_and_cpu_stay_on_default_dockerfile(self):
        for vendor in ("amd", "", "metal"):
            env = install._compose_substitution_env(
                {"code_backend": "ollama", "gpu_vendor": vendor}
            )
            self.assertNotIn(
                "CODE_EMBED_DOCKERFILE", env,
                f"vendor={vendor!r} must not opt into the CUDA Dockerfile",
            )
            self.assertEqual(env.get("CODE_EMBED_BACKEND"), "ollama")

    def test_write_infrastructure_env_lands_in_compose_project_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_root = Path(td)
            (fake_root / "infrastructure").mkdir()
            # Pre-existing user line must survive the managed rewrite.
            (fake_root / "infrastructure" / ".env").write_text(
                "WEAVIATE_PORT=9999\n", encoding="utf-8",
            )
            with mock.patch.object(install, "PROJECT_ROOT", fake_root):
                install._write_infrastructure_env(
                    {"code_backend": "gpu", "gpu_vendor": "nvidia"}
                )
            body = (fake_root / "infrastructure" / ".env").read_text(
                encoding="utf-8"
            )
            self.assertIn("CODE_EMBED_DOCKERFILE=Dockerfile.cuda", body)
            self.assertIn("CODE_EMBED_BACKEND=gpu", body)
            self.assertIn("WEAVIATE_PORT=9999", body, "user lines preserved")

    def test_write_infrastructure_env_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_root = Path(td)
            (fake_root / "infrastructure").mkdir()
            cfg = {"code_backend": "gpu", "gpu_vendor": "nvidia"}
            with mock.patch.object(install, "PROJECT_ROOT", fake_root):
                install._write_infrastructure_env(cfg)
                first = (fake_root / "infrastructure" / ".env").read_text(
                    encoding="utf-8"
                )
                install._write_infrastructure_env(cfg)
                second = (fake_root / "infrastructure" / ".env").read_text(
                    encoding="utf-8"
                )
            self.assertEqual(first, second)


class RocmOverlayShipsTests(unittest.TestCase):
    """C-RT-4 / C-6 — the preferred podman ROCm overlay actually ships."""

    OVERLAY = REPO_ROOT / "infrastructure" / "podman-compose.rocm.yml"

    def test_overlay_file_ships(self):
        self.assertTrue(
            self.OVERLAY.is_file(),
            "install.py prefers podman-compose.rocm.yml for podman+AMD "
            "(candidate list probes it FIRST); it must ship",
        )

    def test_overlay_carries_rocblas_workarounds(self):
        body = self.OVERLAY.read_text(encoding="utf-8")
        self.assertIn("SYS_PTRACE", body)
        self.assertIn("HSA_OVERRIDE_GFX_VERSION", body)
        self.assertIn("keep-groups", body)
        self.assertIn("/dev/kfd", body)
        self.assertIn("/dev/dri", body)

    def test_install_py_probes_short_name_first(self):
        # Static parity guard: the candidate list ordering in
        # _start_services must keep the canonical short name first so
        # this new file (not the legacy amd-rocm overlay) is selected.
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        podman_idx = src.find('"podman-compose.rocm.yml"')
        legacy_idx = src.find('"podman-compose.amd-rocm.yml"')
        self.assertGreater(podman_idx, 0)
        self.assertGreater(legacy_idx, podman_idx)


if __name__ == "__main__":
    unittest.main()
