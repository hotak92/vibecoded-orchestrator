"""Every shipped compose file pins the SAME Ollama image version, and that
version can pull the models VCO's tiers use (v0.2.103 F3).

Field case (Windows, 2026-10-16): the 0.20.2 pin refused ``qwen3.5:0.8b``
(registry HTTP 412: the manifest needs a newer Ollama), so a fresh install
could not pull its own summary-tier model. 0.40.5 was verified to pull and
run every tier model (qwen3.5 0.8b/4b/9b, gemma4:e4b) and every embedder
(qwen3-embedding, arctic, jina-code).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (
    "infrastructure/docker-compose.yml",
    "infrastructure/docker-compose.rocm.yml",
    "infrastructure/podman-compose.rocm.yml",
    "infrastructure/docker-compose.amd-rocm.yml",
    "infrastructure/podman-compose.amd-rocm.yml",
    "claude_mcp_servers/compose.yaml",
)
#: Oldest Ollama verified to pull every tier model (see module docstring).
MIN_VERSION = (0, 40, 5)
_IMAGE = re.compile(r"^\s*image:\s*docker\.io/ollama/ollama:(\d+)\.(\d+)\.(\d+)(-rocm)?\s*$", re.M)


def _pins():
    out = {}
    for rel in COMPOSE_FILES:
        found = _IMAGE.findall((REPO / rel).read_text(encoding="utf-8"))
        assert found, f"{rel}: no pinned docker.io/ollama/ollama:<x.y.z> image line"
        out[rel] = found
    return out


def test_every_compose_file_pins_one_ollama_version():
    versions = {tuple(int(p) for p in m[:3]) for found in _pins().values() for m in found}
    assert len(versions) == 1, f"Ollama pins diverge across compose files: {sorted(versions)}"


def test_the_pin_can_pull_the_tier_models():
    for rel, found in _pins().items():
        for m in found:
            assert tuple(int(p) for p in m[:3]) >= MIN_VERSION, (
                f"{rel}: Ollama {'.'.join(m[:3])} is older than the oldest version verified to "
                f"pull the tier models ({'.'.join(map(str, MIN_VERSION))})")


def test_rocm_overlays_use_the_rocm_variant():
    for rel, found in _pins().items():
        if "rocm" in rel:
            assert all(m[3] == "-rocm" for m in found), f"{rel}: ROCm overlay must use the -rocm image"
