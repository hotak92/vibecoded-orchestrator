# Dependency Licensing Audit

**Generated**: 2026-04-23
**Target license for this project**: AGPL-3.0
**Auditor**: `pip-licenses` on full transitive closure of `requirements.txt`

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

Command used to reproduce:

```bash
# From vibecoded-orchestrator root after install.py
.venv/bin/pip install pip-licenses pipdeptree
.venv/bin/pip-licenses --format=markdown --order=license > /tmp/licenses-full.md
.venv/bin/pipdeptree --packages $(grep -v '^#' requirements.txt | grep -v '^$' | sed 's/[>=<~!].*//' | tr '\n' ',')
```

## Infrastructure services (not Python deps)

Users install these via their container runtime; we don't redistribute binaries. Licensed separately from our Python code.

| Component | License | Redistribution status |
|---|---|---|
| Weaviate | BSD-3-Clause | User runs container from official image |
| Ollama | MIT | User runs container from official image |
| SearXNG (optional) | AGPL-3.0 | Same license family as us; user runs container |
| Podman | Apache-2.0 | Installed by user |
| Docker Engine | Apache-2.0 | Installed by user |
| Docker Desktop | Proprietary (Docker Inc.) | User chooses; we recommend Podman on Linux for no-commercial-license path |

## Python dependency licenses

### Direct dependencies (declared in `requirements.txt`)

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `aiohttp` | 3.13.3 | Apache-2.0 AND MIT | YES |  |
| `fastapi` | 0.129.0 | MIT | YES |  |
| `fastmcp` | 2.14.3 | Apache-2.0 | YES |  |
| `httpx` | 0.28.1 | BSD License | YES |  |
| `mcp` | 1.25.0 | MIT License | YES |  |
| `ollama` | 0.6.1 | MIT | YES |  |
| `psutil` | 7.2.1 | BSD-3-Clause | YES |  |
| `pydantic` | 2.12.5 | MIT | YES |  |
| `PyYAML` | 6.0.3 | MIT License | YES |  |
| `requests` | 2.32.5 | Apache Software License | YES |  |
| `sentence-transformers` | 5.4.0 | Apache Software License | YES |  |
| `torch` | 2.9.1 | BSD-3-Clause | YES |  |
| `transformers` | 4.57.5 | Apache Software License | YES |  |
| `uvicorn` | 0.40.0 | BSD-3-Clause | YES |  |
| `watchdog` | 6.0.0 | Apache Software License | YES |  |
| `weaviate-client` | 4.19.2 | BSD 3-clause | YES |  |

### Transitive dependencies (pulled in by direct deps)

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `aiohappyeyeballs` | 2.6.1 | Python Software Foundation License | YES |  |
| `aiosignal` | 1.4.0 | Apache Software License | YES |  |
| `annotated-doc` | 0.0.4 | MIT | YES |  |
| `annotated-types` | 0.7.0 | MIT License | YES |  |
| `anyio` | 4.12.1 | MIT | YES |  |
| `attrs` | 25.4.0 | MIT | YES |  |
| `Authlib` | 1.6.6 | BSD License | YES |  |
| `beartype` | 0.22.9 | MIT License | YES |  |
| `certifi` | 2026.1.4 | Mozilla Public License 2.0 (MPL 2.0) | YES |  |
| `cffi` | 2.0.0 | MIT | YES |  |
| `charset-normalizer` | 3.4.4 | MIT | YES |  |
| `click` | 8.3.1 | BSD-3-Clause | YES |  |
| `cloudpickle` | 3.1.2 | BSD License | YES |  |
| `cryptography` | 46.0.3 | Apache-2.0 OR BSD-3-Clause | YES |  |
| `cyclopts` | 4.4.5 | Apache-2.0 | YES |  |
| `deprecation` | 2.1.0 | Apache Software License | YES |  |
| `docstring_parser` | 0.17.0 | MIT License | YES |  |
| `docutils` | 0.22.4 | BSD License; GNU General Public License (GPL); Public Domain | YES |  |
| `exceptiongroup` | 1.3.1 | MIT License | YES |  |
| `fakeredis` | 2.33.0 | BSD-3-Clause | YES |  |
| `filelock` | 3.20.3 | Unlicense | YES |  |
| `frozenlist` | 1.8.0 | Apache-2.0 | YES |  |
| `fsspec` | 2026.1.0 | BSD-3-Clause | YES |  |
| `grpcio` | 1.76.0 | Apache Software License | YES |  |
| `h11` | 0.16.0 | MIT License | YES |  |
| `hf-xet` | 1.2.0 | Apache-2.0 | YES |  |
| `httpcore` | 1.0.9 | BSD-3-Clause | YES |  |
| `httpx-sse` | 0.4.3 | MIT | YES |  |
| `huggingface-hub` | 0.36.0 | Apache Software License | YES |  |
| `idna` | 3.11 | BSD-3-Clause | YES |  |
| `importlib_metadata` | 8.7.1 | Apache-2.0 | YES |  |
| `Jinja2` | 3.1.6 | BSD License | YES |  |
| `joblib` | 1.5.3 | BSD-3-Clause | YES |  |
| `jsonschema` | 4.26.0 | MIT | YES |  |
| `jsonschema-path` | 0.3.4 | Apache Software License | YES |  |
| `jsonschema-specifications` | 2025.9.1 | MIT | YES |  |
| `markdown-it-py` | 4.0.0 | MIT License | YES |  |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | YES |  |
| `mdurl` | 0.1.2 | MIT License | YES |  |
| `mpmath` | 1.3.0 | BSD License | YES |  |
| `multidict` | 6.7.1 | Apache License 2.0 | YES |  |
| `networkx` | 3.6.1 | BSD-3-Clause | YES |  |
| `numpy` | 2.2.6 | BSD License | YES |  |
| `openapi-pydantic` | 0.5.1 | MIT License | YES |  |
| `opentelemetry-api` | 1.39.1 | Apache-2.0 | YES |  |
| `opentelemetry-exporter-prometheus` | 0.60b1 | Apache-2.0 | YES |  |
| `opentelemetry-instrumentation` | 0.60b1 | Apache-2.0 | YES |  |
| `opentelemetry-sdk` | 1.39.1 | Apache-2.0 | YES |  |
| `opentelemetry-semantic-conventions` | 0.60b1 | Apache-2.0 | YES |  |
| `packaging` | 26.1 | Apache-2.0 OR BSD-2-Clause | YES |  |
| `pathable` | 0.4.4 | Apache Software License | YES |  |
| `platformdirs` | 4.5.1 | MIT | YES |  |
| `prometheus_client` | 0.24.0 | Apache-2.0 AND BSD-2-Clause | YES |  |
| `propcache` | 0.4.1 | Apache Software License | YES |  |
| `protobuf` | 6.33.4 | 3-Clause BSD License | YES |  |
| `py-key-value-aio` | 0.3.0 | Apache Software License | YES |  |
| `py-key-value-shared` | 0.3.0 | Apache Software License | YES |  |
| `pydantic_core` | 2.41.5 | MIT | YES |  |
| `pydantic-settings` | 2.12.0 | MIT | YES |  |
| `pydocket` | 0.16.6 | MIT License | YES |  |
| `Pygments` | 2.19.2 | BSD License | YES |  |
| `PyJWT` | 2.10.1 | MIT License | YES |  |
| `pyperclip` | 1.11.0 | BSD License | YES |  |
| `python-dotenv` | 1.2.1 | BSD-3-Clause | YES |  |
| `python-json-logger` | 4.0.0 | BSD License | YES |  |
| `python-multipart` | 0.0.21 | Apache-2.0 | YES |  |
| `redis` | 7.1.0 | MIT | YES |  |
| `referencing` | 0.36.2 | MIT | YES |  |
| `regex` | 2025.11.3 | Apache-2.0 AND CNRI-Python | YES |  |
| `rich` | 14.2.0 | MIT License | YES |  |
| `rich-rst` | 1.3.2 | MIT | YES |  |
| `rpds-py` | 0.30.0 | MIT | YES |  |
| `safetensors` | 0.7.0 | Apache Software License | YES |  |
| `scikit-learn` | 1.8.0 | BSD-3-Clause | YES |  |
| `scipy` | 1.17.0 | BSD License | YES |  |
| `shellingham` | 1.5.4 | ISC License (ISCL) | YES |  |
| `sortedcontainers` | 2.4.0 | Apache Software License | YES |  |
| `sse-starlette` | 3.1.2 | BSD-3-Clause | YES |  |
| `starlette` | 0.51.0 | BSD-3-Clause | YES |  |
| `sympy` | 1.14.0 | BSD License | YES |  |
| `threadpoolctl` | 3.6.0 | BSD License | YES |  |
| `tokenizers` | 0.22.2 | Apache Software License | YES |  |
| `tqdm` | 4.67.1 | MIT License; Mozilla Public License 2.0 (MPL 2.0) | YES |  |
| `triton` | 3.5.1 | MIT License | YES |  |
| `typer` | 0.19.2 | MIT License | YES |  |
| `typing_extensions` | 4.15.0 | PSF-2.0 | YES |  |
| `typing-inspection` | 0.4.2 | MIT | YES |  |
| `urllib3` | 2.6.3 | MIT | YES |  |
| `validators` | 0.35.0 | MIT License | YES |  |
| `websockets` | 16.0 | BSD-3-Clause | YES |  |
| `wrapt` | 1.17.3 | BSD License | YES |  |
| `yarl` | 1.22.0 | Apache Software License | YES |  |

### NVIDIA CUDA libraries (transitive via torch, GPU-only)

| Package | Version | License | AGPL-compatible | Notes |
|---|---|---|---|---|
| `nvidia-cublas-cu12` | 12.8.4.1 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cuda-cupti-cu12` | 12.8.90 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cuda-nvrtc-cu12` | 12.8.93 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cuda-runtime-cu12` | 12.8.90 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cudnn-cu12` | 9.10.2.21 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cufft-cu12` | 11.3.3.83 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cufile-cu12` | 1.13.1.3 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-curand-cu12` | 10.3.9.90 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cusolver-cu12` | 11.7.3.90 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cusparse-cu12` | 12.5.8.93 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-cusparselt-cu12` | 0.7.1 | NVIDIA Proprietary Software | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nccl-cu12` | 2.27.5 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nvjitlink-cu12` | 12.8.93 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nvshmem-cu12` | 3.3.20 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |
| `nvidia-nvtx-cu12` | 12.8.90 | Other/Proprietary License | n/a | NVIDIA CUDA — transitive via torch. We do NOT redistribute; user pip install pulls under NVIDIA EULA. |


## Notes on specific packages

### `docutils` (triple-licensed BSD/GPL/Public Domain)

`docutils` is licensed under `BSD License; GNU General Public License (GPL); Public Domain`. As a recipient we elect to use the BSD terms, which are AGPL-compatible. No action required.

### `certifi` / `tqdm` (MPL-2.0)

MPL-2.0 is file-level copyleft. We do not modify `certifi` or `tqdm` source, so this does not impose any obligation beyond retaining their license notices.

### `tqdm` (dual MIT + MPL-2.0)

Dual-licensed. We elect MIT.

### NVIDIA CUDA library transitive deps

The 15 `nvidia-*-cu12` packages listed above are proprietary, pulled in by `torch` when the user has a CUDA-capable GPU. We never bundle these in our repo, installers, or Docker images. Users who install torch with GPU support receive them directly from PyPI under NVIDIA's EULA, which is a matter between the user and NVIDIA.

The `install.py --cpu-only` flag installs a CPU-only torch wheel that avoids these entirely.

## Re-auditing

Re-run this audit whenever `requirements.txt` gains a new entry, or before cutting a public release. The generated section (direct/transitive/NVIDIA tables) can be regenerated by the methodology above; the summary and notes sections are manually maintained.
