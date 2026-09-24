# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Compose-substitution env helpers (v0.2.54 gpu-audit C-4).

This module writes to ``infrastructure/.env`` (the docker-compose
project's env file), a DIFFERENT surface from the canonical project
``.env`` that ``vco_lib.env_template.apply_env_template`` owns: compose
reads it for the shared services' build knobs, it has no VCO-managed
block and no per-project keys. Contract decision (v0.2.97): this module
is that surface's one writer. The
``test_no_direct_writes_to_dotenv_outside_contract`` lint flags any
``.env`` write; this module is on its ``_OTHER_DOTENV_SURFACE_WRITERS``
allowlist (a separate surface, not a pending migration).

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


# ─── One key the launcher owns (v0.2.97 review R5 F40) ──────────────────

#: Keys of ``infrastructure/.env`` the LAUNCHER sets (not install.py): the
#: volumes page's bind-mount root, read by the compose override as
#: ``${VCT_VOLUMES_PATH}``. Closed set — the setter refuses anything else, so
#: this surface keeps one writer per key.
LAUNCHER_INFRA_ENV_KEYS: frozenset[str] = frozenset({"VCT_VOLUMES_PATH"})


def set_infrastructure_env_key(infra_dir: Path, key: str, value: str) -> str:
    """Set ``key=value`` in ``<infra_dir>/.env``: the first line assigning
    ``key`` (THE line grammar, :func:`vco_lib.envfile.parse_env_line` — so
    ``export KEY=`` counts, and keeps its ``export``) is replaced in place,
    else the line is appended. Every other byte is kept: each line keeps its
    own ending (a CRLF file stays CRLF — review R6 F52), a file with no
    trailing newline still has none, and an existing file keeps its mode
    (:func:`vco_lib.atomic.atomic_rewrite_text`). A new file is ``KEY=V\\n``.
    Returns ``"set"`` / ``"unchanged"``. The launcher's volumes page used to
    do this with its own Rust read-modify-write — a second writer of this
    file the single-writer lint could not see until it learned the shape.

    Raises ``ValueError`` for a key outside :data:`LAUNCHER_INFRA_ENV_KEYS`
    or a value with a line break, ``OSError`` on a failed write.
    """
    from vco_lib.atomic import atomic_rewrite_text
    from vco_lib.envfile import parse_env_line

    if key not in LAUNCHER_INFRA_ENV_KEYS:
        raise ValueError(f"{key} is not a launcher-owned infrastructure/.env key")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key}: a value cannot contain a line break")
    infra_env = infra_dir / ".env"
    prior = ""
    if infra_env.is_file():
        with infra_env.open(encoding="utf-8", newline="") as handle:
            prior = handle.read()
    lines = prior.splitlines(keepends=True)
    for i, line in enumerate(lines):
        pair = parse_env_line(line)
        if pair is not None and pair[0] == key:
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            indent = body[: len(body) - len(body.lstrip())]
            export = "export " if body.lstrip().startswith("export ") else ""
            lines[i] = f"{indent}{export}{key}={value}{ending}"
            break
    else:
        eol = "\r\n" if "\r\n" in prior else "\n"
        if not prior:
            lines.append(f"{key}={value}\n")
        elif prior.endswith("\n"):
            lines.append(f"{key}={value}{eol}")
        else:
            lines.append(f"{eol}{key}={value}")
    text = "".join(lines)
    if text == prior:
        return "unchanged"
    infra_dir.mkdir(parents=True, exist_ok=True)
    atomic_rewrite_text(infra_env, text)
    return "set"


def _main(argv: "list[str] | None" = None) -> int:
    """``python -m vco_lib.compose_env set --infra-dir D --key K --value V``
    — one JSON object on stdout (``{"ok": true, "action": …}`` or
    ``{"ok": false, "error": …, "message": …}``)."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m vco_lib.compose_env")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set", help="set one launcher-owned key in infrastructure/.env")
    p_set.add_argument("--infra-dir", required=True)
    p_set.add_argument("--key", required=True, choices=sorted(LAUNCHER_INFRA_ENV_KEYS))
    p_set.add_argument("--value", required=True)
    args = parser.parse_args(argv)
    try:
        action = set_infrastructure_env_key(Path(args.infra_dir), args.key, args.value)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": "set_failed", "message": str(exc)}))
        return 4
    print(json.dumps({"ok": True, "action": action, "key": args.key}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
