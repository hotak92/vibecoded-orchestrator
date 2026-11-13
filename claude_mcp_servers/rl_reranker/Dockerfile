# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# RL reranker — the container image is BUILT LOCALLY from the orchestrator
# checkout (like code_embed). Nothing is pulled from a private registry.
#
# The build context is the CHECKOUT ROOT, not this directory: the reranker
# imports files that live elsewhere in the repository and are NOT duplicated
# here (one copy, one home) — `claude_mcp_servers/rl_client/rl_logger.py`
# (as `rl_client.rl_logger`), `claude_mcp_servers/rl_client/rerank_wire.py`
# (the /cache_nodes feature format, shared with the client) and the
# stdlib-only `vco_lib` modules
# `rl_training_targets`, `atomic` (every atomic write), `log_setup`
# (root-logger setup, one-line log values) and `safe_paths` (request-supplied
# paths contained under the /data roots). The root `.dockerignore` whitelists
# exactly what this file COPYs, so the rest of the checkout never enters the
# context. Two entry points build it from this one file:
#
#   * the launcher, when the module is installed or updated (manifest
#     `install.method = "container_build"`, see vct-module.json): it runs
#     `<engine> build --build-arg VARIANT=<cpu|cuda|rocm> --file
#     claude_mcp_servers/rl_reranker/Dockerfile --tag
#     localhost/vco-rl-reranker:<version>-<variant> <checkout root>`;
#   * infrastructure/docker-compose.yml, service `rl_reranker` (profile `rl`),
#     for a standalone build / run outside the launcher.
#
# OCI-compliant, podman and docker alike: no BuildKit-only syntax.
#
# ─── Three image variants via ARG VARIANT ────────────────────────────
#   cpu  (default) — smallest image, runs everywhere
#   cuda           — NVIDIA
#   rocm           — AMD (linux/amd64 only)
# PyTorch's CUDA / ROCm / CPU wheels are vendor-specific and mutually
# exclusive, so each variant is its own image. The launcher picks the variant
# from `manifest.runtime.gpu_image_variants` and the host GPU snapshot.
#
# ─── Weights ─────────────────────────────────────────────────────────
# The whole `weights/` tree is copied in: `weights/manifest.json` names, per
# embedding profile, the trained checkpoint that ships (or none). The build
# VERIFIES the manifest against the files (sha256) and fails loudly on any
# inconsistency; a profile with no shipped checkpoint is valid and the server
# then reports "not trained". No version is hard-coded here — shipping new
# weights is a change to weights/ only.

ARG VARIANT=cpu

# ─── Stage 1: builder ────────────────────────────────────────────────
FROM python:3.12-slim AS builder
ARG VARIANT

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY claude_mcp_servers/rl_reranker/requirements.txt /build/requirements.txt

# Per-variant PyTorch wheel source. CPU and CUDA keep PyPI primary for the
# non-torch deps (--extra-index-url). ROCm installs torch + its triton from
# the ROCm channel with --no-deps first (otherwise pip's resolver pulls the
# PyPI CUDA-bundled torch and ~3 GB of nvidia-* wheels into an AMD image),
# then torch's runtime deps and the non-torch deps from PyPI.
RUN if [ "$VARIANT" = "cpu" ]; then \
        pip install --no-cache-dir --prefix=/install \
            --extra-index-url https://download.pytorch.org/whl/cpu \
            -r /build/requirements.txt ; \
    elif [ "$VARIANT" = "cuda" ]; then \
        pip install --no-cache-dir --prefix=/install \
            --extra-index-url https://download.pytorch.org/whl/cu121 \
            -r /build/requirements.txt ; \
    elif [ "$VARIANT" = "rocm" ]; then \
        pip install --no-cache-dir --prefix=/install --no-deps \
            --index-url https://download.pytorch.org/whl/rocm6.1 \
            torch==2.5.1 pytorch-triton-rocm==3.1.0 ; \
        pip install --no-cache-dir --prefix=/install \
            filelock 'networkx>=2.5,<4.0' 'jinja2>=3.1' 'sympy==1.13.1' fsspec typing-extensions \
            'numpy>=1.26,<3.0' 'aiohttp>=3.13.5,<4' ; \
    else \
        echo "ERROR: unknown VARIANT '$VARIANT' (expected cpu/cuda/rocm)" >&2 ; \
        exit 1 ; \
    fi

# Verify the bundled-weights layout before producing an image. The verifier
# is stdlib-only, so it runs here without the installed wheels.
COPY claude_mcp_servers/rl_reranker/bundled_weights.py /build/rl_reranker/bundled_weights.py
COPY claude_mcp_servers/rl_reranker/__init__.py        /build/rl_reranker/__init__.py
COPY claude_mcp_servers/rl_reranker/weights            /build/weights
RUN cd /build && python -m rl_reranker.bundled_weights verify /build/weights

# ─── Stage 2: runtime ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
ARG VARIANT

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# The image user. Must match `runtime.image_user` in vct-module.json: the
# launcher maps the HOST user onto this uid/gid (podman `--userns=keep-id`),
# so the server can write the host directories mounted at /data/state,
# /data/logs and /data/global and the host user keeps owning what it writes
# (tests/rl_reranker/test_module_persistence.py pins the parity).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --system --gid ${APP_GID} app \
    && useradd  --system --uid ${APP_UID} --gid ${APP_GID} \
                --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# The packages keep their repository names, so `python -m
# rl_reranker.rl_server` and every import resolve identically in the image and
# in the checkout's test suite (`claude_mcp_servers/` and the root on the path).
# `rl_client` and `vco_lib` carry ONLY the modules the reranker imports, under
# an empty `__init__.py` (the checkout's package inits import client/install
# code the image does not need); `vco_lib.paths` is deliberately absent — see
# `rl_client/rl_logger.py::default_rl_data_dir` (the server always passes an
# explicit log path).
RUN mkdir -p /app/rl_reranker /app/rl_client /app/vco_lib /app/weights /data/state /data/logs /data/global \
    && touch /app/rl_client/__init__.py /app/vco_lib/__init__.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/__init__.py                 /app/rl_reranker/__init__.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/rl_server.py                /app/rl_reranker/rl_server.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/retrieval_rl.py             /app/rl_reranker/retrieval_rl.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/rl_model.py                 /app/rl_reranker/rl_model.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/rl_model_v1.py              /app/rl_reranker/rl_model_v1.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/offline_trainer.py          /app/rl_reranker/offline_trainer.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/hub_event_loader.py         /app/rl_reranker/hub_event_loader.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/math_utils.py               /app/rl_reranker/math_utils.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/migrate_rl_log_v1_to_v2.py  /app/rl_reranker/migrate_rl_log_v1_to_v2.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/bundled_weights.py          /app/rl_reranker/bundled_weights.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/objective.py                /app/rl_reranker/objective.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/online_learning.py          /app/rl_reranker/online_learning.py
COPY --chown=app:app claude_mcp_servers/rl_reranker/ranking_metrics.py          /app/rl_reranker/ranking_metrics.py
COPY --chown=app:app claude_mcp_servers/rl_client/rl_logger.py                 /app/rl_client/rl_logger.py
COPY --chown=app:app claude_mcp_servers/rl_client/rerank_wire.py                /app/rl_client/rerank_wire.py
COPY --chown=app:app vco_lib/rl_training_targets.py                             /app/vco_lib/rl_training_targets.py
COPY --chown=app:app vco_lib/atomic.py                                          /app/vco_lib/atomic.py
COPY --chown=app:app vco_lib/log_setup.py                                       /app/vco_lib/log_setup.py
COPY --chown=app:app vco_lib/safe_paths.py                                      /app/vco_lib/safe_paths.py
COPY --chown=app:app vco_lib/rl_weights_ownership.py                            /app/vco_lib/rl_weights_ownership.py
COPY --chown=app:app vco_lib/rl_weights_ownership.toml                          /app/vco_lib/rl_weights_ownership.toml

# The verified weights tree (manifest + every listed checkpoint).
COPY --chown=app:app --from=builder /build/weights /app/weights

# The manifest + module DB migrations travel with the image too, so an image
# is self-describing (`podman cp` of /app/vct-module.json works as for any
# container module).
COPY --chown=app:app claude_mcp_servers/rl_reranker/vct-module.json /app/vct-module.json
COPY --chown=app:app claude_mcp_servers/rl_reranker/db /app/db

RUN chown -R app:app /app /data
USER app:app

# RL_WEIGHTS_DIR is the bundled-weights ROOT; the server picks the active
# profile's checkpoint from its manifest (bundled_weights.py).
ENV RL_PORT=11450 \
    RL_BIND_HOST=0.0.0.0 \
    RL_WEIGHTS_DIR=/app/weights \
    RL_PROJECT_ROOT=/data \
    RL_LOG_PATH=/data/logs/rl_events.jsonl \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 11450

LABEL org.opencontainers.image.source="https://github.com/hotak92/vibecoded-orchestrator" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      com.vibecodedtools.rl-reranker.variant="${VARIANT}"

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:11450/health || exit 1

ENTRYPOINT ["python", "-m", "rl_reranker.rl_server"]
