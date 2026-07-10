# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Install-time wiring for the v0.2.77 5c embedding-concurrency budget.

Extracted from install.py (the monolith ratchet caps inline additions) so the
installer only carries thin call-sites. Three pieces:

  * the two ``app_state`` keys the launcher (Rust) reads for the update-all
    admission gate + the code-embed cap mirror,
  * a pure producer of the ``CODE_EMBED_MAX_CONCURRENT`` ``.env`` line(s)
    (honour-explicit-config),
  * a pure ``app_state`` seed helper (idempotent ON CONFLICT DO NOTHING).

The budget MATH lives in ``vco_lib.embedding_selection`` (single source); this
module is only the install-time projection of the resolved knobs into the two
persistence channels (``.env`` + ``app_state``).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from vco_lib.embedding_selection import resolve_concurrency_for_config

# app_state keys. MUST stay in sync with the Rust constant
# UPDATE_ALL_MAX_PARALLEL_KEY in
# launcher/src-tauri/vct-launcher-core/src/db/app_state.rs.
APP_STATE_KEY_CODE_EMBED_MAX_CONCURRENT = "embedding.code_embed_max_concurrent"
APP_STATE_KEY_UPDATE_ALL_MAX_PARALLEL = "embedding.update_all_max_parallel"


def finalize_embed_config_for_host(
    config: dict,
    sysinfo,
    log: "Optional[Callable[[str], None]]" = None,
) -> None:
    """Stamp host-derived fields onto the resolved embedding config:

    (a) ``gpu_vendor`` (v0.2.50 F1) so the .env writer picks the right
        CODE_EMBED_DOCKERFILE variant (NVIDIA → Dockerfile.cuda; else default).
    (b) v0.2.77 5c task 2: the hardware-derived concurrency knobs
        (``code_embed_max_concurrent`` + ``update_all_max_parallel``) via the
        shared-pool budget, so the .env writer + app_state seed reflect the
        host, not the fixed default-4 that shed 503s in the 5c incident.

    ``sysinfo`` is any object exposing ``vram_gb`` / ``ram_gb`` / ``has_gpu`` /
    ``gpu_vendor`` (install.py's ``SystemInfo`` — duck-typed to avoid importing
    it here). Never raises: on any probe/backend error the concurrency knobs
    stay unset (downstream writers fall back to compiled-in defaults) and
    ``log`` (if given) is called once. ``gpu_vendor`` is set first so it lands
    regardless.
    """
    gpu_vendor = getattr(sysinfo, "gpu_vendor", "") or ""
    config["gpu_vendor"] = gpu_vendor
    try:
        budget = resolve_concurrency_for_config(
            code_backend=str(config.get("code_model") or ""),
            kg_backend=str(config.get("text_model") or ""),
            vram_gb=getattr(sysinfo, "vram_gb", 0.0),
            ram_gb=getattr(sysinfo, "ram_gb", 0.0),
            has_gpu=bool(getattr(sysinfo, "has_gpu", False)),
            gpu_vendor=gpu_vendor,
        )
        config["code_embed_max_concurrent"] = budget.code_embed_max_concurrent
        config["update_all_max_parallel"] = budget.update_all_max_parallel_projects
    except Exception as exc:  # noqa: BLE001 — never block install on a probe
        if log is not None:
            log(f"could not derive concurrency budget ({exc}); "
                f"downstream writers will use compiled-in defaults")


def code_embed_max_concurrent_env_lines(embed_config: dict) -> "list[str]":
    """Return the ``CODE_EMBED_MAX_CONCURRENT=<n>`` ``.env`` line, or ``[]``.

    Honour-explicit-config: if the user already exported
    ``CODE_EMBED_MAX_CONCURRENT`` we do NOT emit a line (their value stands).
    Omitted entirely when the budget could not be derived (the service then
    uses its own default of 4). Splat ``*code_embed_max_concurrent_env_lines(
    cfg)`` into the ``.env`` line list.
    """
    val = embed_config.get("code_embed_max_concurrent")
    if val is None or "CODE_EMBED_MAX_CONCURRENT" in os.environ:
        return []
    return [f"CODE_EMBED_MAX_CONCURRENT={int(val)}"]


def seed_concurrency_app_state(cur, embed_config: dict, now_ms: int) -> None:
    """Seed the two concurrency knobs into ``app_state`` via an open cursor.

    ON CONFLICT DO NOTHING → a GUI-tuned / prior-install value is preserved; a
    fresh install seeds the derived value. Only seeds a knob that was actually
    derived (absent → the Rust gate / code-embed service use their defaults).
    ``cur`` is an open sqlite3 cursor on launcher.db; the caller commits.
    """
    for key, cfg_key in (
        (APP_STATE_KEY_CODE_EMBED_MAX_CONCURRENT, "code_embed_max_concurrent"),
        (APP_STATE_KEY_UPDATE_ALL_MAX_PARALLEL, "update_all_max_parallel"),
    ):
        val = embed_config.get(cfg_key)
        if val is None:
            continue
        cur.execute(
            "INSERT INTO app_state (key, value, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
            (key, str(int(val)), now_ms),
        )
