# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Compose-substitution env helpers (v0.2.54 gpu-audit C-4).

env_template: legacy_caller_pending_migration

This module writes to ``infrastructure/.env`` (the docker-compose
project's env file), a DIFFERENT surface from the canonical project
``.env`` that ``vco_lib.env_template.apply_env_template`` owns. The
``test_no_direct_writes_to_dotenv_outside_contract`` lint flags any
``.env`` write; this marker pins the module on the
``_LEGACY_ENV_TEMPLATE_WRITERS`` allowlist while the compose-env
surface gets its own contract decision (Phase 0.D follow-up). Before
this v0.2.54 extraction the same write lived in install.py and was
already on the allowlist.

Extracted from install.py per the search-before-add /
extract-before-duplicate discipline.

The two helpers:

- :func:`compose_substitution_env` — derive the ``${...}`` keys that
  docker-compose.yml references but that install.py COMPUTES (not the
  caller's environment): ``CODE_EMBED_BACKEND``, (NVIDIA-only)
  ``CODE_EMBED_DOCKERFILE``, and (v0.2.77 F2) the host-derived
  ``CODE_EMBED_MAX_CONCURRENT`` cap.
- :func:`write_infrastructure_env` — persist those keys to
  ``<infra_dir>/.env`` with managed-line replacement so user lines
  survive re-runs.

Pure functions — caller provides ``embed_config`` + ``infra_dir``;
no install.py module-level state.
"""

from __future__ import annotations

import os
from pathlib import Path


def compose_substitution_env(embed_config: dict) -> dict[str, str]:
    """Keys docker-compose.yml substitutes that install.py COMPUTES
    (rather than inheriting from the caller's environment).

    Pre-v0.2.54 these were written ONLY to ``PROJECT_ROOT/.env``
    (one level above ``infrastructure/``) which compose never reads,
    so ``${CODE_EMBED_DOCKERFILE:-Dockerfile}`` always resolved to the
    CPU multi-arch default — even on NVIDIA hosts. The v0.2.54
    gpu-audit C-4 fix moves the writes to ``infrastructure/.env`` so
    compose actually sees them.
    """
    env: dict[str, str] = {}
    backend = str(embed_config.get("code_backend", "") or "")
    if backend:
        env["CODE_EMBED_BACKEND"] = backend
    # CUDA Dockerfile only for NVIDIA hosts (AMD ROCm / Apple / CPU stay
    # on the multi-arch CPU default — there is no ROCm code-embed image).
    if embed_config.get("gpu_vendor") == "nvidia":
        env["CODE_EMBED_DOCKERFILE"] = "Dockerfile.cuda"
    # v0.2.77 F2: the host-derived code-embed concurrency cap. Pre-fix this
    # was written ONLY to PROJECT_ROOT/.env (which compose never reads) and
    # docker-compose.yml hardcoded "4" — so the derived cap never reached
    # the containerized service and it kept shedding 503s at 4 regardless
    # of host capacity (5c incident). Routing it through infrastructure/.env
    # (the file compose ACTUALLY reads) closes that gap; the compose value
    # is now ${CODE_EMBED_MAX_CONCURRENT:-4}. Honour-explicit-config: an env
    # the user already exported is respected by NOT overriding it here (the
    # user's value flows through compose's own env inheritance), matching
    # code_embed_max_concurrent_env_lines' suppression rule.
    cap = embed_config.get("code_embed_max_concurrent")
    if cap is not None and "CODE_EMBED_MAX_CONCURRENT" not in os.environ:
        env["CODE_EMBED_MAX_CONCURRENT"] = str(int(cap))
    return env


def write_infrastructure_env(
    infra_dir: Path,
    embed_config: dict,
) -> tuple[bool, str]:
    """Persist the compose-substitution keys to ``<infra_dir>/.env``
    (the compose project dir — the file compose ACTUALLY reads), so
    every later compose invocation (the boot wrapper, hooks'
    ensure-containers, a user's manual ``podman-compose up -d``) sees
    the same substitutions as install.py's own compose-up.

    Merge semantics: lines for keys we manage are replaced; all other
    user lines are preserved.

    Returns ``(ok, message)`` so the caller can render its own log
    line. ``ok=False`` with a non-empty message means an OSError
    occurred during write (caller decides whether to warn or escalate).
    A successful no-op (no managed keys to write) returns
    ``(True, "")``.
    """
    managed = compose_substitution_env(embed_config)
    if not managed:
        return True, ""
    infra_env = infra_dir / ".env"
    try:
        existing_lines: list[str] = []
        if infra_env.is_file():
            existing_lines = infra_env.read_text(encoding="utf-8").splitlines()
        kept = [
            ln for ln in existing_lines
            if not any(ln.strip().startswith(f"{k}=") for k in managed)
            and not ln.strip().startswith("# Managed-by-install.py")
        ]
        out = kept + [
            "# Managed-by-install.py: compose ${...} substitution keys for the",
            "# Managed-by-install.py: code-embed image build. Re-running install",
            "# Managed-by-install.py: rewrites these lines; edit via install flags.",
        ] + [f"{k}={v}" for k, v in sorted(managed.items())]
        infra_env.parent.mkdir(parents=True, exist_ok=True)
        infra_env.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True, ""
    except OSError as exc:
        return False, str(exc)
