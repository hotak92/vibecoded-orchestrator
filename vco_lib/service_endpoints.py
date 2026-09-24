# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where VCO's three core services (Weaviate, Ollama, code-embed) are reached.

The Python half of ONE rule. The hub's ``/config`` and the launcher's project
env projection answer it in Rust
(``launcher/src-tauri/vct-launcher-core/src/services/service_endpoints.rs``);
this module answers it for :func:`vco_lib.config_projection.project_env_from_db`
whenever no caller pins a value — ``install.py``, ``project_init``,
``project_move`` and every other Python caller that projects a project's env.
Before v0.2.97 those callers projected ``http://localhost:8081`` whatever the
machine said, while the hub served a different chain again.

Precedence, highest first (MUST MATCH the Rust module):

Weaviate URL
  1. the machine statement: ``VCT_WEAVIATE_URL``, else ``weaviate_url`` in
     ``vct-config.toml`` (next to the launcher / hub binary). Used as stated.
  2. ``app_state[weaviate.port_override]`` → ``http://localhost:<port>``.
  3. ``services.toml``: ``adopt`` → the adopted ``external_url``'s origin (the
     host is kept); ``parallel`` → ``http://localhost:<parallel_port>``.
  4. ``http://localhost:8081``.

Ports (Ollama, code-embed): override → adoption (``parallel_port`` or the
adopted URL's port) → the default. ``refuse`` / ``unresolved`` rows are not
addresses.

Ollama URL: the same shape as Weaviate's, with an env-only statement
(``VCT_OLLAMA_URL`` — Ollama has no ``vct-config.toml`` key) and the default
``http://localhost:11435`` (:func:`machine_ollama_url`).

``WEAVIATE_URL`` is deliberately NOT a leg. It is what the projection WRITES;
in a shell that sourced ``.claude/env`` it holds the previous projection, and
reading it back would re-project a stale value over a newer launcher choice.
Clients read it (:func:`vco_lib.weaviate_helpers.weaviate_url_default`); the
resolver that produces it must not.

Why a mirror rather than one implementation (the A>B>C rule): the hub answers
``/config`` on every hook call and cannot spawn Python per request. Both sides
therefore execute the SAME committed case table,
``tests/fixtures/service_endpoint_parity.json``; a rule changes there first.

``vct-config.toml`` sits next to whichever binary reads it. Python cannot see
the running binary, so it looks where the hub is started from
(:mod:`vco_lib.hub_ensure`'s chain, minus ``$PATH``): ``$VCT_HUB_BIN``'s
directory, ``<orchestrator>/launcher/dist/<arch>/``, ``<orchestrator>/launcher/dist/``,
``~/.vct/bin``. When the launcher spawns a ``vco_lib`` child (every
projection it drives), it hands over its OWN leg 1 as ``VCT_WEAVIATE_URL``
(``vco_lib_bridge::reinject_minimal_env``), so a launcher binary run from
anywhere else is still honoured there; legs 2-4 come from the same
``launcher.db`` and ``services.toml`` both sides read.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "CONFIG_FILE",
    "CONFIG_KEY",
    "OLLAMA_STATEMENT_ENV",
    "SERVICES",
    "STATEMENT_ENV",
    "adoption_row",
    "config_file_candidates",
    "machine_ollama_url",
    "machine_port",
    "machine_service_urls",
    "machine_weaviate_url",
    "machine_weaviate_url_statement",
    "parse_port_override",
    "port_of_url",
    "read_port_override",
    "resolve_ollama_url",
    "resolve_port",
    "resolve_weaviate_url",
    "weaviate_port_for_url",
]

#: MUST MATCH ``service_endpoints::STATEMENT_ENV`` (Rust).
STATEMENT_ENV = "VCT_WEAVIATE_URL"
#: The env var that states the machine's Ollama URL (the Ollama chain's
#: leg 1 — env-only; Ollama has no ``vct-config.toml`` key).
#: MUST MATCH ``service_endpoints::OLLAMA_STATEMENT_ENV`` (Rust).
OLLAMA_STATEMENT_ENV = "VCT_OLLAMA_URL"
#: The launcher's per-machine config file and the key it states the URL in.
CONFIG_FILE = "vct-config.toml"
CONFIG_KEY = "weaviate_url"

#: service → (app_state override key, default host port).
#: MUST MATCH ``CoreService`` in the Rust module and the parity table.
SERVICES: dict[str, tuple[str, int]] = {
    "weaviate": ("weaviate.port_override", 8081),
    "ollama": ("ollama.port_override", 11435),
    "code_embed": ("code_embed.port_override", 11440),
}


def parse_port_override(raw: Optional[str]) -> Optional[int]:
    """An app_state override as a port: ASCII digits only (surrounding
    whitespace ignored), 1..65535. Anything else is "no override"."""
    if raw is None:
        return None
    s = raw.strip()
    if not s or not all("0" <= c <= "9" for c in s):
        return None
    port = int(s)
    return port if 0 < port <= 65535 else None


def _origin_of(url: str) -> str:
    url = url.strip()
    if "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://{rest.split('/', 1)[0]}"
    return url.split("/", 1)[0]


def _explicit_port(url: str) -> Optional[int]:
    origin = _origin_of(url)
    authority = origin.split("://", 1)[1] if "://" in origin else origin
    if ":" not in authority:
        return None
    tail = authority.rsplit(":", 1)[1]
    if not tail or not all("0" <= c <= "9" for c in tail):
        return None
    port = int(tail)
    return port if port <= 65535 else None


def port_of_url(url: str) -> Optional[int]:
    """The URL's explicit port, else 443 for https / 80 for http."""
    port = _explicit_port(url)
    if port is not None:
        return port
    s = url.strip()
    if "://" not in s:
        return None
    scheme = s.split("://", 1)[0].lower()
    return {"https": 443, "http": 80}.get(scheme)


def adoption_row(services_state: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    """The ``services.toml`` row for *name*, or ``None``."""
    for row in services_state.get("services", []) or []:
        if isinstance(row, Mapping) and row.get("name") == name:
            return row
    return None


def resolve_port(service: str, port_override: Optional[str],
                 adoption: Optional[Mapping[str, Any]],
                 default_port: Optional[int] = None) -> int:
    """Port chain: override → adoption → default. Pure.

    *default_port* replaces the canonical default (the last leg only) — a
    caller's ``--ollama-port`` / ``ollama_port_default``; the Rust side never
    passes one, and the parity table runs with it unset."""
    port = parse_port_override(port_override)
    if port is not None:
        return port
    if adoption is not None:
        mode = adoption.get("mode")
        if mode == "parallel" and isinstance(adoption.get("parallel_port"), int):
            return int(adoption["parallel_port"])
        if mode == "adopt" and isinstance(adoption.get("external_url"), str):
            explicit = _explicit_port(adoption["external_url"])
            if explicit is not None:
                return explicit
    return SERVICES[service][1] if default_port is None else default_port


def resolve_weaviate_url(statement: Optional[str], port_override: Optional[str],
                         adoption: Optional[Mapping[str, Any]],
                         default_port: Optional[int] = None) -> str:
    """Weaviate URL chain (module docstring). Pure.

    *default_port* replaces 8081 in the last leg only (see
    :func:`resolve_port`)."""
    if statement is not None and statement.strip():
        return statement.strip().rstrip("/")
    return _url_below_statement("weaviate", port_override, adoption, default_port)


def resolve_ollama_url(statement: Optional[str], port_override: Optional[str],
                       adoption: Optional[Mapping[str, Any]]) -> str:
    """Ollama URL chain — the same shape as Weaviate's, with an env-only
    statement (``VCT_OLLAMA_URL``; Ollama has no ``vct-config.toml`` key).
    ``OLLAMA_URL`` is NOT a leg: it is what the projection WRITES. Pure."""
    if statement is not None and statement.strip():
        return statement.strip().rstrip("/")
    return _url_below_statement("ollama", port_override, adoption, None)


def _url_below_statement(service: str, port_override: Optional[str],
                         adoption: Optional[Mapping[str, Any]],
                         default_port: Optional[int]) -> str:
    """The legs below the statement, shared by the Weaviate and Ollama URL
    chains: port override → adoption → the service's default port."""
    port = parse_port_override(port_override)
    if port is not None:
        return f"http://localhost:{port}"
    if adoption is not None:
        mode = adoption.get("mode")
        url = adoption.get("external_url")
        if mode == "adopt" and isinstance(url, str) and url.strip():
            return _origin_of(url)
        if mode == "parallel" and isinstance(adoption.get("parallel_port"), int):
            return f"http://localhost:{int(adoption['parallel_port'])}"
    port = SERVICES[service][1] if default_port is None else default_port
    return f"http://localhost:{port}"


def weaviate_port_for_url(url: str) -> int:
    """The port for ``WEAVIATE_PORT`` that goes with a resolved URL."""
    port = port_of_url(url)
    return port if port is not None else SERVICES["weaviate"][1]


# ─── live-state readers ─────────────────────────────────────────────────


def config_file_candidates(orchestrator_root: Optional[Path],
                           environ: Optional[Mapping[str, str]] = None) -> list[Path]:
    """Where a ``vct-config.toml`` for this machine's binaries can sit."""
    env = os.environ if environ is None else environ
    dirs: list[Path] = []
    hub_bin = (env.get("VCT_HUB_BIN") or "").strip()
    if hub_bin:
        dirs.append(Path(hub_bin).parent)
    if orchestrator_root is not None:
        from vco_lib.hub_ensure import dist_arch_dir  # noqa: PLC0415 - stdlib-only, keep import local

        dist = Path(orchestrator_root) / "launcher" / "dist"
        arch = dist_arch_dir()
        if arch:
            dirs.append(dist / arch)
        dirs.append(dist)
    home = env.get("HOME") or env.get("USERPROFILE")
    if home:
        dirs.append(Path(home) / ".vct" / "bin")
    return [d / CONFIG_FILE for d in dirs]


def _read_config_file_url(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import tomllib  # noqa: PLC0415

        value = tomllib.loads(text).get(CONFIG_KEY)
    except Exception:  # noqa: BLE001 - a malformed file states nothing (Rust: same)
        return None
    return value if isinstance(value, str) and value else None


def machine_weaviate_url_statement(orchestrator_root: Optional[Path] = None,
                                   environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Leg 1: ``VCT_WEAVIATE_URL``, else the first ``vct-config.toml`` found."""
    env = os.environ if environ is None else environ
    value = env.get(STATEMENT_ENV) or ""
    if value.strip():
        return value
    for candidate in config_file_candidates(orchestrator_root, env):
        if candidate.is_file():
            stated = _read_config_file_url(candidate)
            if stated is not None:
                return stated
    return None


def read_port_override(conn: Optional[sqlite3.Connection], service: str) -> Optional[str]:
    """The raw ``app_state`` override for *service*; ``None`` on any miss."""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (SERVICES[service][0],)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _services_state(services_state: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if services_state is not None:
        return services_state
    from vco_lib.service_adoption import read_services_toml  # noqa: PLC0415

    return read_services_toml()


def machine_port(conn: Optional[sqlite3.Connection], service: str, *,
                 services_state: Optional[Mapping[str, Any]] = None) -> int:
    """This machine's port for *service* from launcher.db + services.toml."""
    state = _services_state(services_state)
    return resolve_port(service, read_port_override(conn, service),
                        adoption_row(state, service))


def machine_weaviate_url(conn: Optional[sqlite3.Connection], *,
                         orchestrator_root: Optional[Path] = None,
                         environ: Optional[Mapping[str, str]] = None,
                         services_state: Optional[Mapping[str, Any]] = None) -> str:
    """This machine's Weaviate URL — what the hub's ``/config`` serves."""
    state = _services_state(services_state)
    return resolve_weaviate_url(
        machine_weaviate_url_statement(orchestrator_root, environ),
        read_port_override(conn, "weaviate"),
        adoption_row(state, "weaviate"),
    )


def machine_service_urls(
    orchestrator_root: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """The machine's Weaviate + Ollama URLs and the three service ports,
    opening launcher.db read-only when it exists.

    The one-call form for callers that hold no connection (install.py's
    ``.claude/settings.json`` defaults): every value comes from the same
    chains as :func:`machine_weaviate_url` / :func:`machine_ollama_url` /
    :func:`machine_port`. A missing or unreadable launcher.db simply
    drops the override leg — the chains still resolve.
    """
    from vco_lib.launcher_db_reader import _open_db_readonly  # noqa: PLC0415

    conn = _open_db_readonly(db_path)
    try:
        return {
            "weaviate_url": machine_weaviate_url(conn, orchestrator_root=orchestrator_root),
            "ollama_url": machine_ollama_url(conn),
            "weaviate_port": machine_port(conn, "weaviate"),
            "ollama_port": machine_port(conn, "ollama"),
            "code_embed_port": machine_port(conn, "code_embed"),
        }
    finally:
        if conn is not None:
            conn.close()


def machine_ollama_url(conn: Optional[sqlite3.Connection], *,
                       environ: Optional[Mapping[str, str]] = None,
                       services_state: Optional[Mapping[str, Any]] = None) -> str:
    """This machine's Ollama URL: the ``VCT_OLLAMA_URL`` statement, the
    app_state override, and ``services.toml``. ``OLLAMA_URL`` (the
    projection's own output) is deliberately not a leg."""
    env = os.environ if environ is None else environ
    statement = env.get(OLLAMA_STATEMENT_ENV) or None
    state = _services_state(services_state)
    return resolve_ollama_url(
        statement,
        read_port_override(conn, "ollama"),
        adoption_row(state, "ollama"),
    )
