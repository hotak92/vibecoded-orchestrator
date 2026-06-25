# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canonical-path regression tests for vco_lib.embedding_selection (v0.2.68).

The hardware-tier selectors + their shared CPU predicate were extracted
from install.py to ``vco_lib.embedding_selection`` in v0.2.68
(behaviour-preserving). The exhaustive tier sweep lives in
``tests/test_hardware_auto_selection.py`` and imports the selectors via
``install`` (the re-export path), guaranteeing the re-export keeps working.

This file locks the NEW canonical import path — it imports DIRECTLY from
``vco_lib.embedding_selection`` so the leaf module is independently covered
even if install.py's re-export shim is ever removed. It also asserts that
``install``'s re-exports are the SAME objects as the canonical definitions
(identity check), so the two import paths can never silently diverge.
"""

from __future__ import annotations

import sys
from pathlib import Path

# vco_lib lives at repo root (sibling of install.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from vco_lib.embedding_selection import (  # noqa: E402
    _cpu_meets,
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
)


class TestCpuMeetsCanonical:
    """`_cpu_meets` — strict-vs-inclusive RAM boundary, via canonical import."""

    def test_strict_ram_excludes_exact_24(self):
        # code/KG rule: ram > 24 (strict) — exactly 24 GB does NOT qualify.
        assert _cpu_meets(24.0, 8, min_ram=24.0, min_cores=8, strict_ram=True) is False

    def test_strict_ram_includes_above_24(self):
        assert _cpu_meets(24.1, 8, min_ram=24.0, min_cores=8, strict_ram=True) is True

    def test_inclusive_ram_includes_exact_12(self):
        # summary rule: ram >= 12 (inclusive) — exactly 12 GB DOES qualify.
        assert _cpu_meets(12.0, 6, min_ram=12.0, min_cores=6, strict_ram=False) is True

    def test_inclusive_ram_excludes_below_12(self):
        assert _cpu_meets(11.9, 6, min_ram=12.0, min_cores=6, strict_ram=False) is False

    def test_cores_inclusive_boundary(self):
        # cores comparison is ALWAYS inclusive (>=).
        assert _cpu_meets(64.0, 8, min_ram=24.0, min_cores=8, strict_ram=True) is True
        assert _cpu_meets(64.0, 7, min_ram=24.0, min_cores=8, strict_ram=True) is False

    def test_none_inputs_coerce_to_zero_and_fail(self):
        assert _cpu_meets(None, None, min_ram=12.0, min_cores=6, strict_ram=False) is False


class TestSelectorsCanonical:
    """One representative pick per selector, via the canonical import."""

    def test_code_high_vram_codesage(self):
        assert select_code_embedding_backend(
            gpu_vram_gb=16.0, ram_gb=32.0, cores=8, openai_key_available=False
        ) == _CODE_BACKEND_CODESAGE

    def test_code_cpu_floor_jina(self):
        assert select_code_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=8.0, cores=4, openai_key_available=False
        ) == _CODE_BACKEND_JINA

    def test_code_openai_override(self):
        assert select_code_embedding_backend(
            gpu_vram_gb=16.0, ram_gb=32.0, cores=8,
            openai_key_available=True, prefer_openai=True,
        ) == _CODE_BACKEND_OPENAI

    def test_kg_high_vram_qwen3(self):
        assert select_kg_embedding_backend(
            gpu_vram_gb=12.0, ram_gb=32.0, cores=8, openai_key_available=False
        ) == _KG_BACKEND_QWEN3

    def test_kg_mid_vram_arctic(self):
        assert select_kg_embedding_backend(
            gpu_vram_gb=6.0, ram_gb=16.0, cores=4, openai_key_available=False
        ) == _KG_BACKEND_ARCTIC

    def test_kg_openai_override(self):
        assert select_kg_embedding_backend(
            gpu_vram_gb=0.0, ram_gb=8.0, cores=2,
            openai_key_available=True, prefer_openai=True,
        ) == _KG_BACKEND_OPENAI

    def test_summary_cli_wins(self):
        assert select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=8.0, cores=2,
            claude_cli_available=True, openai_consent=False,
        ) == _SUMMARY_BACKEND_CLI

    def test_summary_high_vram_qwen35(self):
        assert select_summary_backend(
            gpu_vram_gb=16.0, ram_gb=32.0, cores=8,
            claude_cli_available=False, openai_consent=False,
        ) == _SUMMARY_BACKEND_QWEN35_9B

    def test_summary_cpu_gemma(self):
        assert select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=12.0, cores=6,
            claude_cli_available=False, openai_consent=False,
        ) == _SUMMARY_BACKEND_GEMMA

    def test_summary_openai_last_resort(self):
        assert select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            claude_cli_available=False,
            openai_consent=True, openai_key_available=True,
        ) == _SUMMARY_BACKEND_OPENAI

    def test_summary_none_when_nothing_viable(self):
        assert select_summary_backend(
            gpu_vram_gb=0.0, ram_gb=4.0, cores=2,
            claude_cli_available=False, openai_consent=False,
        ) is None


class TestReexportIdentity:
    """install.py re-exports must be the SAME objects as the canonical home."""

    def test_install_reexports_are_identical_objects(self):
        import install  # re-export shim

        assert install._cpu_meets is _cpu_meets
        assert install.select_code_embedding_backend is select_code_embedding_backend
        assert install.select_kg_embedding_backend is select_kg_embedding_backend
        assert install.select_summary_backend is select_summary_backend
        # Constants are immutable strings — assert value equality (the
        # re-export binds the same name to the same string object).
        assert install._CODE_BACKEND_CODESAGE == _CODE_BACKEND_CODESAGE
        assert install._CODE_BACKEND_QWEN3 == _CODE_BACKEND_QWEN3
        assert install._CODE_BACKEND_JINA == _CODE_BACKEND_JINA
        assert install._CODE_BACKEND_OPENAI == _CODE_BACKEND_OPENAI
        assert install._KG_BACKEND_QWEN3 == _KG_BACKEND_QWEN3
        assert install._KG_BACKEND_ARCTIC == _KG_BACKEND_ARCTIC
        assert install._KG_BACKEND_OPENAI == _KG_BACKEND_OPENAI
        assert install._SUMMARY_BACKEND_CLI == _SUMMARY_BACKEND_CLI
        assert install._SUMMARY_BACKEND_QWEN35_9B == _SUMMARY_BACKEND_QWEN35_9B
        assert install._SUMMARY_BACKEND_GEMMA == _SUMMARY_BACKEND_GEMMA
        assert install._SUMMARY_BACKEND_OPENAI == _SUMMARY_BACKEND_OPENAI
