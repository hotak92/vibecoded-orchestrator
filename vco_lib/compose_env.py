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
from typing import Any, Mapping, Optional


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
    from vco_lib.atomic import atomic_rewrite_text  # noqa: PLC0415

    managed = compose_substitution_env(embed_config)
    if not managed:
        return True, ""
    infra_env = infra_dir / ".env"
    try:
        existing_lines: list[str] = []
        if infra_env.is_file():
            existing_lines = infra_env.read_text(encoding="utf-8").splitlines()
        # The service-endpoints block (write_service_keys) is ANOTHER writer's
        # region of this file: every line inside it is kept verbatim, so this
        # rewrite can never drop or edit a key the rows project — whatever
        # key names the two writers grow in future.
        kept: list[str] = []
        in_block = False
        for ln in existing_lines:
            marker = ln.strip()
            if marker == _SERVICE_BLOCK_BEGIN:
                in_block = True
            if in_block:
                kept.append(ln)
                if marker == _SERVICE_BLOCK_END:
                    in_block = False
                continue
            if any(marker.startswith(f"{k}=") for k in managed):
                continue
            if marker.startswith("# Managed-by-install.py"):
                continue
            kept.append(ln)
        out = kept + [
            "# Managed-by-install.py: compose ${...} substitution keys for the",
            "# Managed-by-install.py: code-embed image build. Re-running install",
            "# Managed-by-install.py: rewrites these lines; edit via install flags.",
        ] + [f"{k}={v}" for k, v in sorted(managed.items())]
        infra_env.parent.mkdir(parents=True, exist_ok=True)
        atomic_rewrite_text(infra_env, "\n".join(out) + "\n")
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


# ─── The service-endpoint keys (v0.2.97 SE-3) ────────────────────────────
#
# ``infrastructure/.env`` is where compose finds the ``${WEAVIATE_PORT}`` /
# ``${VCT_*_DATA_SOURCE}`` / … substitutions the base file declares. Before
# v0.2.97 nothing wrote them persistently (an alt port lived in one install
# run's ``os.environ``; the data-source knobs were documented as a hand edit),
# so every later compose run — the session hook, the boot wrapper, the
# watchdog — bound the defaults. They are now PROJECTED from the launcher.db
# ``service_endpoints`` rows (the one source of truth) by
# :func:`write_service_keys`, which ``service_endpoints.apply_change`` calls on
# every row change. This module stays the file's one writer.

#: Per-service host-port key compose substitutes (``vco_managed`` rows only).
SERVICE_PORT_KEYS: dict[str, str] = {
    "weaviate": "WEAVIATE_PORT",
    "ollama": "OLLAMA_PORT",
    "code_embed": "CODE_EMBED_PORT",
}
WEAVIATE_GRPC_PORT_KEY = "WEAVIATE_GRPC_PORT"

#: Per-service data-source knob pair ``(SOURCE, VOLUME_NAME)`` — MUST MATCH
#: ``infrastructure/docker-compose.yml``. A HOST PATH goes in SOURCE (compose
#: then mounts a bind); an existing named volume goes in VOLUME_NAME (SOURCE
#: stays unset so the stanza keeps its declared volume key). code_embed's
#: SOURCE knob is ``…_CACHE_SOURCE`` (documented and tested under that name).
DATA_KNOBS: dict[str, tuple[str, str]] = {
    "weaviate": ("VCT_WEAVIATE_DATA_SOURCE", "VCT_WEAVIATE_VOLUME_NAME"),
    "ollama": ("VCT_OLLAMA_DATA_SOURCE", "VCT_OLLAMA_VOLUME_NAME"),
    "code_embed": ("VCT_CODE_EMBED_CACHE_SOURCE", "VCT_CODE_EMBED_VOLUME_NAME"),
}

#: code_embed's CPU (``CODE_EMBED_BACKEND=ollama``) backend reaches Ollama by
#: the in-network name ``vco_ollama`` — which does not exist when Ollama is not
#: VCO's compose service. Then this key carries a URL that does resolve.
CODE_EMBED_OLLAMA_URL_KEY = "CODE_EMBED_OLLAMA_URL"
#: The address compose maps ``vco-host-gateway`` to inside code_embed
#: (``extra_hosts`` in docker-compose.yml). Docker needs the literal
#: ``host-gateway`` for it; podman reaches the host as
#: ``host.containers.internal`` natively and keeps the inert default, because
#: podman 4.x does not accept ``host-gateway`` (plan risk R4).
CODE_EMBED_HOST_GATEWAY_KEY = "VCT_CODE_EMBED_HOST_GATEWAY"
CODE_EMBED_HOST_GATEWAY_NAME = "vco-host-gateway"

#: Every key :func:`write_service_keys` may own.
SERVICE_KEYS: frozenset[str] = frozenset(
    list(SERVICE_PORT_KEYS.values())
    + [WEAVIATE_GRPC_PORT_KEY, CODE_EMBED_OLLAMA_URL_KEY, CODE_EMBED_HOST_GATEWAY_KEY]
    + [k for pair in DATA_KNOBS.values() for k in pair]
)

_SERVICE_BLOCK_BEGIN = (
    "# >>> VCO service endpoints: written from launcher.db service_endpoints; "
    "change them with `python -m vco_lib.service_endpoints` >>>"
)
_SERVICE_BLOCK_END = "# <<< VCO service endpoints <<<"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


class ServiceKeysResult:
    """What :func:`write_service_keys` did. ``values``: the managed keys now
    in the block. ``superseded``: ``{key: old_value}`` for lines OUTSIDE the
    block that assigned a key the rows now state (the row is the truth; the
    old value is returned so a caller can report it). ``notes``: why a key
    was not written. ``action``: ``"set"`` / ``"unchanged"``."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.superseded: dict[str, str] = {}
        self.notes: list[str] = []
        self.action = "unchanged"


def _env_value(value: str) -> str:
    """One ``.env`` value, quoted when it needs to be. Single quotes: compose
    and python-dotenv (podman-compose's reader) take them literally, so a
    ``$`` in a path is never interpolated."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"a value cannot contain a line break: {value!r}")
    if value and all(c.isalnum() or c in "/._-:+@%,=" for c in value):
        return value
    if "'" in value:
        raise ValueError(f"a value with a single quote cannot be written safely: {value!r}")
    return f"'{value}'"


def _host_alias_for(runtime: Optional[str]) -> Optional[str]:
    if runtime == "podman":
        return "host.containers.internal"
    if runtime == "docker":
        return CODE_EMBED_HOST_GATEWAY_NAME
    return None


def service_key_values(
    rows: "Mapping[str, Any]", *, runtime: Optional[str] = None,
) -> tuple[dict[str, str], list[str]]:
    """Pure: the managed keys *rows* state (``""`` = stated ABSENT), plus
    notes for keys it could not decide.

    A service with NO row states nothing — its keys are left alone, because
    the absence of a row is not a statement. Port keys and data knobs come
    from ``vco_managed`` rows only; a data knob only when the row carries an
    observed mount (a vco_managed row without one leaves any existing knob
    line alone rather than re-pointing the service at the default volume).
    ``CODE_EMBED_OLLAMA_URL`` is stated when the Ollama row is NOT
    ``vco_managed``; *runtime* picks the host alias for an adopted Ollama on
    this machine (``podman``/``docker``)."""
    from vco_lib.service_endpoints import render_grpc_port, render_url  # noqa: PLC0415

    values: dict[str, str] = {}
    notes: list[str] = []
    for service, port_key in SERVICE_PORT_KEYS.items():
        row = rows.get(service)
        if row is None or row.mode != "vco_managed":
            continue
        values[port_key] = str(int(row.port))
        if service == "weaviate":
            values[WEAVIATE_GRPC_PORT_KEY] = str(render_grpc_port(row))
        mount = row.data_mount
        if mount:
            source_key, volume_key = DATA_KNOBS[service]
            kind, source = mount.get("kind"), str(mount.get("source") or "")
            if kind == "bind" and source:
                values[source_key] = source
                values[volume_key] = ""
            elif kind == "volume" and source:
                values[volume_key] = source
                values[source_key] = ""
    ollama = rows.get("ollama")
    if ollama is not None and ollama.mode != "vco_managed":
        if ollama.host in _LOCAL_HOSTS:
            alias = _host_alias_for(runtime)
            if alias is None:
                notes.append(
                    f"{CODE_EMBED_OLLAMA_URL_KEY} not written: the container runtime is "
                    "unknown, so the host alias code_embed must use for this machine's "
                    "Ollama cannot be chosen"
                )
            else:
                values[CODE_EMBED_OLLAMA_URL_KEY] = f"{ollama.scheme}://{alias}:{int(ollama.port)}"
                values[CODE_EMBED_HOST_GATEWAY_KEY] = "host-gateway" if runtime == "docker" else ""
        else:
            values[CODE_EMBED_OLLAMA_URL_KEY] = render_url("ollama", ollama)
            values[CODE_EMBED_HOST_GATEWAY_KEY] = ""
    # When Ollama IS VCO's compose service the in-network default is right and
    # nothing is stated: a URL this writer put in its block earlier leaves with
    # the block rewrite, and a user line OUTSIDE the block (the documented
    # power-user override to an Ollama on the host) is kept.
    return values, notes


def write_service_keys(
    infra_dir: Path,
    rows: "Mapping[str, Any]",
    *,
    runtime: Optional[str] = None,
) -> ServiceKeysResult:
    """Project the managed ``infrastructure/.env`` keys from the
    ``service_endpoints`` *rows* (``{service: EndpointRow}``).

    The keys live in ONE marker-delimited block this function owns: every
    call rewrites the block from the rows, so a key the rows no longer state
    leaves the block. A line OUTSIDE the block that assigns a key the rows
    now state (a value, or stated-absent — the other half of a data-knob
    pair) is removed, and its old value reported in ``superseded``: the row
    is the source of truth, and compose must never see two assignments. An
    outside line for a key the rows do NOT state is left exactly as it is.
    Every other byte of the file is kept.

    *runtime* (``podman``/``docker``) is needed only for an adopted Ollama on
    this machine; ``None`` resolves it (``vco_lib.containers.resolve``) only
    in that case. Raises ``ValueError`` for an unwritable value, ``OSError``
    on a failed write.
    """
    from vco_lib.atomic import atomic_rewrite_text  # noqa: PLC0415
    from vco_lib.envfile import parse_env_line  # noqa: PLC0415

    ollama = rows.get("ollama")
    if (runtime is None and ollama is not None and ollama.mode != "vco_managed"
            and ollama.host in _LOCAL_HOSTS):
        try:
            from vco_lib import containers as _containers  # noqa: PLC0415

            runtime = _containers.resolve(probe_compose=False).runtime
        except Exception:  # noqa: BLE001 — unresolved ⇒ noted, never guessed
            runtime = None
    stated, notes = service_key_values(rows, runtime=runtime)
    result = ServiceKeysResult()
    result.notes = notes
    written = {k: v for k, v in stated.items() if v != ""}

    infra_env = infra_dir / ".env"
    prior = ""
    if infra_env.is_file():
        with infra_env.open(encoding="utf-8", newline="") as handle:
            prior = handle.read()
    eol = "\r\n" if "\r\n" in prior else "\n"
    kept: list[str] = []
    block_at: Optional[int] = None  # where an existing block stood: rewritten IN PLACE
    in_block = False
    for line in prior.splitlines():
        marker = line.strip()
        if marker == _SERVICE_BLOCK_BEGIN:
            in_block = True
            if block_at is None:
                block_at = len(kept)
            continue
        if in_block:
            if marker == _SERVICE_BLOCK_END:
                in_block = False
            continue
        pair = parse_env_line(line)
        if pair is not None and pair[0] in stated:
            if pair[1] != written.get(pair[0], ""):
                result.superseded[pair[0]] = pair[1]
            continue
        kept.append(line)
    block: list[str] = []
    if written:
        block = [_SERVICE_BLOCK_BEGIN]
        block.extend(f"{k}={_env_value(v)}" for k, v in sorted(written.items()))
        block.append(_SERVICE_BLOCK_END)
    if block_at is not None:
        lines = kept[:block_at] + block + kept[block_at:]
    else:
        while kept and not kept[-1].strip():
            kept.pop()
        lines = list(kept)
        if block:
            if lines:
                lines.append("")
            lines.extend(block)
    text = eol.join(lines) + eol if lines else ""
    result.values = written
    if text == prior:
        return result
    infra_dir.mkdir(parents=True, exist_ok=True)
    atomic_rewrite_text(infra_env, text)
    result.action = "set"
    return result


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
