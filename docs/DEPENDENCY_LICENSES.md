# Dependency Licensing Audit

**Generated**: 2026-07-21 (`pip-licenses` full-closure run against the reference install environment; direct + transitive + NVIDIA tables regenerated from the live `requirements.txt` closure)
**Target license for this project**: AGPL-3.0
**Auditor**: `pip-licenses` on the full transitive closure of `requirements.txt`, resolved from the reference install environment

## Summary

All direct and transitive Python dependencies carry licenses compatible with AGPL-3.0 (the core project's license). Nothing GPL-2-only, AGPL-3-only, or otherwise copyleft-incompatible is pulled in.

The NVIDIA CUDA libraries are a special case. They're proprietary, but transitive dependencies of `torch` that pip only installs on CUDA-capable systems. We don't redistribute them in our repo or installers; users who install `torch` with GPU support pull them directly from PyPI under NVIDIA's EULA, which NVIDIA itself enforces. Same situation as the CUDA Toolkit.

**Approval status**: ✅ Cleared for AGPL-3.0 core release.

## Compatibility rules applied

| License | AGPL-3 compat | Action |
|---|---|---|
| MIT / BSD-2/3 / Apache-2.0 / ISC / MPL-2.0 / PSF | YES | Ship as-is, include in NOTICE if required |
| LGPL (dynamic link) | YES | Ship as-is |
| AGPL / GPL (any version) | Same family → OK for our AGPL code, but a red flag for proprietary redistribution | Flagged `NO` for proprietary, `YES` in our context |
| Pure GPL-2-only | Requires review | Flagged `CHECK` — none found in closure |
| NVIDIA Proprietary | User EULA, not redistributed by us | Flagged `n/a` |

## Audit methodology

1. Start from declared direct dependencies in [`requirements.txt`](../requirements.txt).
2. Walk transitive dependency graph via `pip show` (recursive to stable set).
3. Query license metadata via `pip-licenses --format=json`.
4. Flag any license containing AGPL/GPL/Proprietary/LGPL keywords for manual review.
5. Document rationale for anything flagged.

Command used to reproduce (read-only — install the tools into a THROWAWAY venv, never into the install environment being audited, then point them at it with `--python`):

```bash
# 1. Isolated tool venv (does not touch the environment under audit)
python3 -m venv /tmp/piplic-venv
/tmp/piplic-venv/bin/pip install pip-licenses

# 2. Read the license metadata of the target install environment
/tmp/piplic-venv/bin/pip-licenses \
    --python /path/to/install/env/bin/python \
    --format=markdown --with-urls --order=name > /tmp/licenses-full.md

# 3. Restrict to the requirements.txt transitive closure
#    (walk each root's Requires-Dist over the installed metadata; drop
#    extras-only deps) so unrelated packages in a shared dev environment
#    don't leak into the audit.
```

## Infrastructure services (not Python deps)

Users install these via their container runtime; we don't redistribute binaries. Licensed separately from our Python code.

| Component | License | Redistribution status |
|---|---|---|
| Weaviate | BSD-3-Clause | User runs container from official image |
| Ollama | MIT | User runs container from official image |
| Podman | Apache-2.0 | Installed by user |
| Docker Engine | Apache-2.0 | Installed by user |
| Docker Desktop | Proprietary (Docker Inc.) | User chooses; we recommend Podman on Linux for no-commercial-license path |

## Python dependency licenses

### Direct dependencies (declared in `requirements.txt`)

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `aiohttp` | 3.13.5 | Apache-2.0 AND MIT | YES |  |
| `asyncpg` | 0.31.0 | Apache-2.0 | YES |  |
| `fastapi` | 0.136.1 | MIT | YES |  |
| `httpx` | 0.28.1 | BSD-3-Clause | YES |  |
| `mcp` | 1.27.0 | MIT | YES |  |
| `ollama` | 0.6.2 | MIT | YES |  |
| `psutil` | 7.2.2 | BSD-3-Clause | YES |  |
| `pydantic` | 2.13.4 | MIT | YES |  |
| `PyYAML` | 6.0.3 | MIT | YES |  |
| `requests` | 2.33.1 | Apache-2.0 | YES |  |
| `sentence-transformers` | 5.4.1 | Apache-2.0 | YES |  |
| `torch` | 2.11.0 | BSD-3-Clause | YES |  |
| `transformers` | 4.49.0 | Apache-2.0 | YES | Deliberately capped `<4.50.0` in `requirements.txt` — CodeSage-Large-v2 (Conv1D) is incompatible with transformers ≥ 4.50; do not bump past the cap. |
| `uvicorn` | 0.46.0 | BSD-3-Clause | YES |  |
| `watchdog` | 6.0.0 | Apache-2.0 | YES |  |
| `weaviate-client` | 4.21.0 | BSD-3-Clause | YES |  |

### Transitive dependencies (pulled in by direct deps)

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `aiohappyeyeballs` | 2.6.1 | PSF-2.0 | YES |  |
| `aiosignal` | 1.4.0 | Apache-2.0 | YES |  |
| `annotated-doc` | 0.0.4 | MIT | YES |  |
| `annotated-types` | 0.7.0 | MIT | YES |  |
| `anyio` | 4.13.0 | MIT | YES |  |
| `attrs` | 26.1.0 | MIT | YES |  |
| `Authlib` | 1.7.2 | BSD-3-Clause | YES |  |
| `certifi` | 2026.4.22 | MPL-2.0 | YES |  |
| `cffi` | 2.0.0 | MIT | YES |  |
| `charset-normalizer` | 3.4.7 | MIT | YES |  |
| `click` | 8.3.3 | BSD-3-Clause | YES |  |
| `cryptography` | 48.0.0 | Apache-2.0 OR BSD-3-Clause | YES |  |
| `cuda-pathfinder` | 1.5.4 | Apache-2.0 | YES | Open-source CUDA-library path helper (transitive via torch); NOT proprietary NVIDIA CUDA. |
| `filelock` | 3.29.0 | MIT | YES |  |
| `frozenlist` | 1.8.0 | Apache-2.0 | YES |  |
| `fsspec` | 2026.4.0 | BSD-3-Clause | YES |  |
| `grpcio` | 1.78.0 | Apache-2.0 | YES |  |
| `h11` | 0.16.0 | MIT | YES |  |
| `hf-xet` | 1.5.0 | Apache-2.0 | YES |  |
| `httpcore` | 1.0.9 | BSD-3-Clause | YES |  |
| `httpx-sse` | 0.4.3 | MIT | YES |  |
| `huggingface_hub` | 0.36.2 | Apache-2.0 | YES |  |
| `idna` | 3.13 | BSD-3-Clause | YES |  |
| `Jinja2` | 3.1.6 | BSD-3-Clause | YES |  |
| `joblib` | 1.5.3 | BSD-3-Clause | YES |  |
| `joserfc` | 1.6.5 | BSD-3-Clause | YES |  |
| `jsonschema` | 4.26.0 | MIT | YES |  |
| `jsonschema-specifications` | 2025.9.1 | MIT | YES |  |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | YES |  |
| `mpmath` | 1.3.0 | BSD-3-Clause | YES |  |
| `multidict` | 6.7.1 | Apache-2.0 | YES |  |
| `networkx` | 3.6.1 | BSD-3-Clause | YES |  |
| `numpy` | 2.4.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | YES |  |
| `packaging` | 26.2 | Apache-2.0 OR BSD-2-Clause | YES |  |
| `propcache` | 0.4.1 | Apache-2.0 | YES |  |
| `protobuf` | 6.33.6 | BSD-3-Clause | YES |  |
| `pycparser` | 3.0 | BSD-3-Clause | YES |  |
| `pydantic_core` | 2.46.4 | MIT | YES |  |
| `pydantic-settings` | 2.14.0 | MIT | YES |  |
| `PyJWT` | 2.12.1 | MIT | YES |  |
| `python-dotenv` | 1.2.2 | BSD-3-Clause | YES |  |
| `python-multipart` | 0.0.27 | Apache-2.0 | YES |  |
| `referencing` | 0.37.0 | MIT | YES |  |
| `regex` | 2026.4.4 | Apache-2.0 AND CNRI-Python | YES |  |
| `rpds-py` | 0.30.0 | MIT | YES |  |
| `safetensors` | 0.7.0 | Apache-2.0 | YES |  |
| `scikit-learn` | 1.8.0 | BSD-3-Clause | YES |  |
| `scipy` | 1.17.1 | BSD-3-Clause | YES |  |
| `setuptools` | 81.0.0 | MIT | YES |  |
| `sse-starlette` | 3.4.1 | BSD-3-Clause | YES |  |
| `starlette` | 1.0.0 | BSD-3-Clause | YES |  |
| `sympy` | 1.14.0 | BSD-3-Clause | YES |  |
| `threadpoolctl` | 3.6.0 | BSD-3-Clause | YES |  |
| `tokenizers` | 0.21.4 | Apache-2.0 | YES |  |
| `tqdm` | 4.67.3 | MPL-2.0 AND MIT | YES |  |
| `triton` | 3.6.0 | MIT | YES |  |
| `typing_extensions` | 4.15.0 | PSF-2.0 | YES |  |
| `typing-inspection` | 0.4.2 | MIT | YES |  |
| `urllib3` | 2.6.3 | MIT | YES |  |
| `validators` | 0.35.0 | MIT | YES |  |
| `yarl` | 1.23.0 | Apache-2.0 | YES |  |

### NVIDIA CUDA libraries (transitive via torch, GPU-only)

As of `torch` 2.11 the CUDA runtime is pulled in through the `cuda-toolkit`
meta-package (with `[cublas,cudart,cufft,cufile,cupti,curand,cusolver,cusparse,nvjitlink,nvrtc,nvtx]`
extras) plus a few directly-declared `nvidia-*-cu13` wheels — a packaging change
from the earlier era of ~15 individual `nvidia-*-cu12` packages. The extras
resolve to the same underlying proprietary CUDA-13 libraries; only those actually
resolved in the reference environment are listed below. All are Linux-and-GPU-only
(`platform_system == "Linux"`) and are pulled directly from PyPI under NVIDIA's
EULA — we never redistribute them.

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `cuda-bindings` | 13.2.0 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `cuda-toolkit` | 13.0.2 | Other/Proprietary License | n/a | NVIDIA CUDA meta-package (extras resolve to the proprietary CUDA-13 libs) — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cublas` | 13.1.0.3 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cudnn-cu13` | 9.19.0.56 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cusparselt-cu13` | 0.8.0 | NVIDIA Proprietary Software | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nccl-cu13` | 2.28.9 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nvshmem-cu13` | 3.4.5 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |


## Notes on specific packages

### `certifi` / `tqdm` (MPL-2.0)

MPL-2.0 is file-level copyleft. We do not modify `certifi` or `tqdm` source, so this does not impose any obligation beyond retaining their license notices.

### `tqdm` (dual MIT + MPL-2.0)

Dual-licensed. We elect MIT.

### NVIDIA CUDA library transitive deps

The NVIDIA CUDA packages listed above are proprietary, pulled in by `torch` when the user has a CUDA-capable GPU (Linux only). As of torch 2.11 they arrive via the `cuda-toolkit` meta-package extras plus a few directly-declared `nvidia-*-cu13` wheels, replacing the earlier flat set of `nvidia-*-cu12` packages; the exact resolved set depends on the torch wheel and platform. We never bundle any of them in our repo, installers, or Docker images. Users who install torch with GPU support receive them directly from PyPI under NVIDIA's EULA, which is a matter between the user and NVIDIA.

The open-source `cuda-pathfinder` helper (Apache-2.0) is a separate, non-proprietary transitive dep and is listed in the transitive table above, not here.

The `install.py --cpu-only` flag installs a CPU-only torch wheel that avoids these entirely.

## Re-auditing

Re-run this audit whenever `requirements.txt` gains a new entry, or before cutting a public release. The generated section (direct/transitive/NVIDIA tables) can be regenerated by the methodology above; the summary and notes sections are manually maintained.
