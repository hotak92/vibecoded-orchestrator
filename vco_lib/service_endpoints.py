# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where VCO's three core services (Weaviate, Ollama, code-embed) are reached.

The launcher DB's ``service_endpoints`` table (migration
``047_service_endpoints.sql``) holds one row per service: its mode, its
``scheme://host:port`` (+ gRPC port for Weaviate), and the identity of the
container and data mount behind it. This module is:

* **the reader** — :func:`read_rows` / :func:`load_rows`, and the machine
  resolvers (:func:`machine_weaviate_url` & co.) every Python caller projects
  env through (``config_projection``, ``project_init``, ``install.py``);
* **the ONE writer** — :func:`write_rows` / :func:`commit_rows`, which enforce
  the migration's CHECKs before writing, and :func:`apply_change`, the
  follow-up chain every change triggers (infra ``.env`` → reproject every
  project → refresh the MCP registration);
* **the CLI** — ``python -m vco_lib.service_endpoints show | resolve | plan``
  (plus ``candidates | adopt | use-vco-copy | move | hand-to-vco | reconcile``,
  whose logic lives in :mod:`vco_lib.service_reconcile`).

The rule (MUST MATCH ``launcher/src-tauri/vct-launcher-core/src/services/
service_endpoints.rs``): **row → compiled default.** An absent row (first
boot before the install finished, or a broken install) answers
``http://localhost:8081`` + gRPC 50052, ``:11435``, ``:11440`` and logs one
WARNING per process per service. Nothing else is a leg: ``services.toml``,
the app_state ``*.port_override`` keys, ``vct-config.toml``'s
``weaviate_url``, ``VCT_WEAVIATE_URL`` / ``VCT_OLLAMA_URL`` and the projected
transport (``WEAVIATE_URL``, ``*_PORT``, ``GRPC_PORT``, …) are read by no
resolver. Their values reach the rows once, through the v0.2.97 importer
(``vco_lib.service_reconcile``).

The render (``scheme://host[:port]``, ``:port`` omitted when it is the
scheme's default; the host used verbatim so an IPv6 literal keeps its
brackets) is the only cross-language mirror: the hub answers ``/config`` on
every hook call and cannot spawn Python per request. Both sides execute
``tests/fixtures/service_endpoint_parity.json``; change a rule there first.

Machine-scoped callers read rows. Project-scoped clients (MCPs, hooks,
scripts) read only the transport the projection renders from the rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORTS",
    "DEFAULT_SCHEME",
    "DEFAULT_WEAVIATE_GRPC_PORT",
    "MODES",
    "RETIRED_APP_STATE_KEYS",
    "RETIRED_ENV_INPUTS",
    "RETIRED_MACHINE_ENV",
    "SERVICES",
    "SOURCES",
    "ApplyChangeReport",
    "EndpointRow",
    "InvalidEndpointRow",
    "ServiceRegistryUnavailable",
    "WriteResult",
    "PROPAGATED_DIGEST_KEY",
    "apply_change",
    "awaits_choice",
    "propagated_digest",
    "rows_digest",
    "commit_rows",
    "describe",
    "load_rows",
    "machine_code_embed_url",
    "machine_grpc_port",
    "machine_ollama_url",
    "machine_port",
    "machine_service_urls",
    "machine_url",
    "machine_weaviate_url",
    "plan",
    "plan_shell_lines",
    "port_of_url",
    "read_row",
    "read_rows",
    "render_grpc_port",
    "render_port",
    "render_url",
    "transport_env",
    "urls_from_rows",
    "validate_row",
    "warn_absent",
    "weaviate_port_for_url",
    "write_rows",
]

_LOG = logging.getLogger(__name__)

#: The three core services, in display order. Each is also the compose
#: service name and the ``service_endpoints.service`` key.
SERVICES: tuple[str, ...] = ("weaviate", "ollama", "code_embed")

#: Compiled defaults — the answer for an absent row. MUST MATCH the Rust
#: ``DEFAULT_*`` constants and the compose defaults.
DEFAULT_PORTS: dict[str, int] = {"weaviate": 8081, "ollama": 11435, "code_embed": 11440}
DEFAULT_WEAVIATE_GRPC_PORT = 50052
DEFAULT_SCHEME = "http"
DEFAULT_HOST = "localhost"

#: ``service_endpoints.mode``. MUST MATCH the migration's CHECK.
MODES: tuple[str, ...] = ("vco_managed", "adopted_container", "adopted_external")
#: Fixed ``source`` values; ``migrated:<store>`` is the one open form.
SOURCES: tuple[str, ...] = ("install_probe", "user_gui", "user_cli", "live_reconcile")
_MIGRATED_PREFIX = "migrated:"
_SCHEMES: dict[str, int] = {"http": 80, "https": 443}
_MANAGED_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})
_MOUNT_KINDS: frozenset[str] = frozenset({"bind", "volume"})

#: Inputs that WERE resolver legs before v0.2.97 and are read by no resolver
#: now. Named so tests can pin that they are ignored.
RETIRED_ENV_INPUTS: tuple[str, ...] = (
    "VCT_WEAVIATE_URL",
    "VCT_OLLAMA_URL",
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "WEAVIATE_GRPC_PORT",
    "GRPC_PORT",
    "VCT_GRPC_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
    "CODE_EMBED_SERVICE_URL",
)
#: The subset a USER set by hand for the hub/launcher (not the projected
#: transport every project process legitimately carries): ``vco doctor``
#: warns while one is still exported — nothing reads it any more.
RETIRED_MACHINE_ENV: tuple[str, ...] = ("VCT_WEAVIATE_URL", "VCT_OLLAMA_URL", "VCT_GRPC_PORT")
RETIRED_APP_STATE_KEYS: dict[str, str] = {
    "weaviate": "weaviate.port_override",
    "ollama": "ollama.port_override",
    "code_embed": "code_embed.port_override",
}


class InvalidEndpointRow(ValueError):
    """A row the ``service_endpoints`` schema (or the writer's stricter
    shape rules) refuses. Raised BEFORE anything is written."""


class ServiceRegistryUnavailable(RuntimeError):
    """launcher.db, or its ``service_endpoints`` table, is not there to write
    to. The table is created only by the Rust migration runner
    (``vct-hub --ensure-db``); Python never creates schema."""


@dataclass(frozen=True)
class EndpointRow:
    """One ``service_endpoints`` row. ``data_mount`` is the parsed
    ``data_mount_json`` (``{"kind": "bind"|"volume", "source", "destination"}``).
    ``updated_at`` is stamped by the writer; a caller leaves it ``None``."""

    service: str
    mode: str
    port: int
    source: str
    scheme: str = DEFAULT_SCHEME
    host: str = DEFAULT_HOST
    grpc_port: Optional[int] = None
    container_name: Optional[str] = None
    compose_project: Optional[str] = None
    data_mount: Optional[Mapping[str, str]] = None
    enabled: bool = True
    autostart: bool = True
    confirmed_by_user: bool = False
    verified_at: Optional[int] = None
    updated_at: Optional[int] = None

    def to_json(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "mode": self.mode,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "grpc_port": self.grpc_port,
            "container_name": self.container_name,
            "compose_project": self.compose_project,
            "data_mount": dict(self.data_mount) if self.data_mount is not None else None,
            "enabled": self.enabled,
            "autostart": self.autostart,
            "source": self.source,
            "confirmed_by_user": self.confirmed_by_user,
            "verified_at": self.verified_at,
            "updated_at": self.updated_at,
        }


# ─── the render (pure; the mirror the parity table pins) ────────────────


def render_url(service: str, row: Optional[EndpointRow], *,
               default_port: Optional[int] = None) -> str:
    """The URL *row* addresses, or the compiled default for *service*.

    *default_port* replaces the compiled default port for an ABSENT row only
    (a Python caller's pinned ``*_port_default``); the Rust side never passes
    one and the parity table runs with it unset."""
    if row is None:
        port = DEFAULT_PORTS[service] if default_port is None else default_port
        return f"{DEFAULT_SCHEME}://{DEFAULT_HOST}:{port}"
    if _SCHEMES.get(row.scheme) == row.port:
        return f"{row.scheme}://{row.host}"
    return f"{row.scheme}://{row.host}:{row.port}"


def render_port(service: str, row: Optional[EndpointRow], *,
                default_port: Optional[int] = None) -> int:
    """The host port *row* states, or *service*'s compiled default."""
    if row is not None:
        return row.port
    return DEFAULT_PORTS[service] if default_port is None else default_port


def render_grpc_port(row: Optional[EndpointRow]) -> int:
    """Weaviate's gRPC port from its row, or 50052."""
    if row is not None and row.grpc_port is not None:
        return row.grpc_port
    return DEFAULT_WEAVIATE_GRPC_PORT


def port_of_url(url: str) -> Optional[int]:
    """The explicit port of *url*'s authority, else 443 for https / 80 for
    http; ``None`` for a URL with neither."""
    s = url.strip()
    scheme: Optional[str] = None
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = scheme.lower()
    authority = rest.split("/", 1)[0]
    tail = authority[authority.rfind("]") + 1:] if "]" in authority else authority
    if ":" in tail:
        port = tail.rsplit(":", 1)[1]
        if port and all("0" <= c <= "9" for c in port):
            value = int(port)
            return value if value <= 65535 else None
    return _SCHEMES.get(scheme) if scheme is not None else None


def weaviate_port_for_url(url: str) -> int:
    """The ``WEAVIATE_PORT`` that goes with a Weaviate URL."""
    port = port_of_url(url)
    return port if port is not None else DEFAULT_PORTS["weaviate"]


# ─── validation (the DDL's CHECKs, enforced before any write) ───────────


def _valid_host(host: str) -> bool:
    if not host or host != host.strip():
        return False
    if host.startswith("["):
        return host.endswith("]") and len(host) > 2 and all(
            c in "0123456789abcdefABCDEF:." for c in host[1:-1]
        )
    return all(c.isalnum() or c in "-._" for c in host)


def _valid_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def validate_row(row: EndpointRow) -> None:
    """Raise :class:`InvalidEndpointRow` unless *row* satisfies every CHECK of
    migration 047, plus the writer's shape rules (a well-formed host, a
    known ``source``, a well-formed data mount)."""
    problems: list[str] = []
    if row.service not in SERVICES:
        problems.append(f"service {row.service!r} not in {SERVICES}")
    if row.mode not in MODES:
        problems.append(f"mode {row.mode!r} not in {MODES}")
    if row.scheme not in _SCHEMES:
        problems.append(f"scheme {row.scheme!r} not http/https")
    if not isinstance(row.host, str) or not _valid_host(row.host):
        problems.append(f"host {row.host!r} is not a hostname, IPv4 or [IPv6] literal")
    if not _valid_port(row.port):
        problems.append(f"port {row.port!r} not in 1..65535")
    if row.grpc_port is not None and not _valid_port(row.grpc_port):
        problems.append(f"grpc_port {row.grpc_port!r} not in 1..65535")
    if row.mode == "adopted_container" and not (row.container_name or "").strip():
        problems.append("adopted_container needs container_name")
    if row.mode == "vco_managed" and row.host not in _MANAGED_HOSTS:
        problems.append(f"vco_managed host must be localhost/127.0.0.1, got {row.host!r}")
    if row.service == "weaviate" and row.grpc_port is None:
        problems.append("weaviate needs grpc_port")
    if row.service == "code_embed" and row.mode != "vco_managed":
        problems.append("code_embed is always vco_managed")
    src = row.source if isinstance(row.source, str) else ""
    if not (src in SOURCES or (src.startswith(_MIGRATED_PREFIX) and len(src) > len(_MIGRATED_PREFIX))):
        problems.append(f"source {row.source!r} not in {SOURCES} or migrated:<store>")
    if row.data_mount is not None:
        m = row.data_mount
        if not isinstance(m, Mapping) or m.get("kind") not in _MOUNT_KINDS or not all(
            isinstance(m.get(k), str) and m.get(k) for k in ("source", "destination")
        ):
            problems.append(f"data_mount {m!r} must be {{kind: bind|volume, source, destination}}")
    for name in ("enabled", "autostart", "confirmed_by_user"):
        if not isinstance(getattr(row, name), bool):
            problems.append(f"{name} must be a bool")
    if row.verified_at is not None and (not isinstance(row.verified_at, int) or isinstance(row.verified_at, bool)):
        problems.append("verified_at must be unix ms or None")
    if problems:
        raise InvalidEndpointRow(f"{row.service}: " + "; ".join(problems))


# ─── reading ────────────────────────────────────────────────────────────

_COLUMNS = (
    "service", "mode", "scheme", "host", "port", "grpc_port", "container_name",
    "compose_project", "data_mount_json", "enabled", "autostart", "source",
    "confirmed_by_user", "verified_at", "updated_at",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM service_endpoints"

_WARNED: set[str] = set()


def _warn_default_once(service: str, why: str) -> None:
    if service in _WARNED:
        return
    _WARNED.add(service)
    _LOG.warning(
        "service_endpoints: no usable %s row (%s); answering the compiled default %s "
        "(a successful install/update writes the row — run `python install.py --update` "
        "if this persists)",
        service, why, render_url(service, None),
    )


def warn_absent(rows: Mapping[str, EndpointRow]) -> None:
    """Warn (once per process per service) for every service *rows* lacks —
    for callers that read the rows themselves and then render."""
    for service in SERVICES:
        if service not in rows:
            _warn_default_once(service, "no row")


def _row_from_tuple(values: Sequence[Any]) -> EndpointRow:
    d = dict(zip(_COLUMNS, values))
    mount = None
    if d["data_mount_json"]:
        mount = json.loads(d["data_mount_json"])
    return EndpointRow(
        service=d["service"], mode=d["mode"], scheme=d["scheme"], host=d["host"],
        port=int(d["port"]),
        grpc_port=int(d["grpc_port"]) if d["grpc_port"] is not None else None,
        container_name=d["container_name"], compose_project=d["compose_project"],
        data_mount=mount, enabled=bool(d["enabled"]), autostart=bool(d["autostart"]),
        source=d["source"], confirmed_by_user=bool(d["confirmed_by_user"]),
        verified_at=d["verified_at"], updated_at=d["updated_at"],
    )


def read_rows(conn: Optional[sqlite3.Connection]) -> dict[str, EndpointRow]:
    """Every readable row on *conn*, by service. A ``None`` connection, a DB
    without the table (pre-047), or any read error is ``{}`` — every resolver
    then answers the default. A single unreadable row is skipped (logged)."""
    if conn is None:
        return {}
    try:
        raw = conn.execute(_SELECT).fetchall()
    except sqlite3.Error as exc:
        _LOG.debug("service_endpoints: read failed: %s", exc)
        return {}
    out: dict[str, EndpointRow] = {}
    for values in raw:
        try:
            row = _row_from_tuple(tuple(values))
        except (ValueError, TypeError, KeyError) as exc:
            _LOG.warning("service_endpoints: unreadable row %r skipped: %s", tuple(values)[:1], exc)
            continue
        out[row.service] = row
    return out


def read_row(conn: Optional[sqlite3.Connection], service: str) -> Optional[EndpointRow]:
    """*service*'s row on *conn*, or ``None`` (default applies; warned once)."""
    row = read_rows(conn).get(service)
    if row is None:
        _warn_default_once(service, "no row" if conn is not None else "no launcher.db")
    return row


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    if db_path is not None:
        return Path(db_path)
    from vco_lib.paths import launcher_db_path  # noqa: PLC0415

    return launcher_db_path()


def load_rows(db_path: Optional[Path] = None) -> dict[str, EndpointRow]:
    """Every row in launcher.db (default: :func:`vco_lib.paths.launcher_db_path`),
    read through a read-only connection. A missing DB is ``{}``."""
    from vco_lib.launcher_db_reader import _open_db_readonly  # noqa: PLC0415

    conn = _open_db_readonly(_resolve_db_path(db_path))
    try:
        return read_rows(conn)
    finally:
        if conn is not None:
            conn.close()


# ─── the machine resolvers (row → compiled default) ─────────────────────


def machine_url(conn: Optional[sqlite3.Connection], service: str, *,
                default_port: Optional[int] = None) -> str:
    return render_url(service, read_row(conn, service), default_port=default_port)


def machine_weaviate_url(conn: Optional[sqlite3.Connection]) -> str:
    """This machine's Weaviate URL — what the hub's ``/config`` serves."""
    return machine_url(conn, "weaviate")


def machine_ollama_url(conn: Optional[sqlite3.Connection]) -> str:
    return machine_url(conn, "ollama")


def machine_code_embed_url(conn: Optional[sqlite3.Connection]) -> str:
    return machine_url(conn, "code_embed")


def machine_port(conn: Optional[sqlite3.Connection], service: str) -> int:
    return render_port(service, read_row(conn, service))


def machine_grpc_port(conn: Optional[sqlite3.Connection]) -> int:
    return render_grpc_port(read_row(conn, "weaviate"))


def machine_service_urls(db_path: Optional[Path] = None) -> dict[str, Any]:
    """Every endpoint value at once, from launcher.db opened read-only — the
    one-call form for callers that hold no connection (install.py's
    ``.claude/settings.json`` defaults, the standalone project env)."""
    rows = load_rows(db_path)
    warn_absent(rows)
    return urls_from_rows(rows)


def urls_from_rows(rows: Mapping[str, EndpointRow]) -> dict[str, Any]:
    """:func:`machine_service_urls` for rows the caller already holds — e.g.
    install.py's in-memory pins when launcher.db could not be written."""
    w, o, c = rows.get("weaviate"), rows.get("ollama"), rows.get("code_embed")
    return {
        "weaviate_url": render_url("weaviate", w),
        "ollama_url": render_url("ollama", o),
        "code_embed_url": render_url("code_embed", c),
        "weaviate_port": render_port("weaviate", w),
        "weaviate_grpc_port": render_grpc_port(w),
        "ollama_port": render_port("ollama", o),
        "code_embed_port": render_port("code_embed", c),
    }


def transport_env(rows: Mapping[str, EndpointRow]) -> dict[str, str]:
    """The projected transport (the env names project-process clients read)
    for *rows*. install.py pins its OWN process env to this after step [5b],
    so every client leaf it runs (and every child it spawns) reaches the
    recorded endpoints, not whatever the invoking shell exported."""
    u = urls_from_rows(rows)
    return {
        "WEAVIATE_URL": u["weaviate_url"],
        "WEAVIATE_PORT": str(u["weaviate_port"]),
        "WEAVIATE_GRPC_PORT": str(u["weaviate_grpc_port"]),
        "GRPC_PORT": str(u["weaviate_grpc_port"]),
        "OLLAMA_URL": u["ollama_url"],
        "OLLAMA_PORT": str(u["ollama_port"]),
        "CODE_EMBED_URL": u["code_embed_url"],
        "CODE_EMBED_PORT": str(u["code_embed_port"]),
        "CODE_EMBED_SERVICE_URL": u["code_embed_url"],
    }


# ─── the plan (what the session hook / wrappers act on) ─────────────────


def awaits_choice(service: str, row: Optional[EndpointRow]) -> bool:
    """Does *service* wait for the user's choice? MUST MATCH the Rust
    ``service_endpoints::awaits_choice`` (pinned by the parity table's
    ``awaits_choice_cases``). No row yet → yes (nothing has been decided);
    Weaviate's "waiting for your choice" row — ``vco_managed`` with
    ``enabled=0``, the owner-ruling-Q1 parking state
    (``service_reconcile._decide_third_party``) → yes; anything else → no
    (a disabled Ollama/code_embed is a plain "not run", not a pending choice)."""
    if row is None:
        return True
    return service == "weaviate" and row.mode == "vco_managed" and not row.enabled


def plan(rows: Mapping[str, EndpointRow]) -> dict[str, Any]:
    """What lifecycle code acts on, per service, derived from *rows*.

    ``managed_services``: compose service names VCO may bring up — rows in
    ``vco_managed`` mode that are ``enabled``, plus services with NO row
    (the pre-reconcile default is VCO's own stack). ``adopted_containers``:
    the names of ``adopted_container`` rows with ``autostart`` — started by
    name, never recreated. ``adopted_external`` rows appear in neither."""
    from vco_lib.containers import CANONICAL_CONTAINERS  # noqa: PLC0415

    managed: list[str] = []
    adopted: list[str] = []
    per_service: dict[str, dict[str, Any]] = {}
    for service in SERVICES:
        row = rows.get(service)
        mode = row.mode if row is not None else "vco_managed"
        enabled = row.enabled if row is not None else True
        autostart = row.autostart if row is not None else True
        if mode == "vco_managed":
            container = (row.container_name if row is not None else None) or CANONICAL_CONTAINERS[service]
        else:
            container = row.container_name if row is not None else None
        if mode == "vco_managed" and enabled:
            managed.append(service)
        if mode == "adopted_container" and autostart and container:
            adopted.append(container)
        entry: dict[str, Any] = {
            "present": row is not None,
            "mode": mode,
            "container": container or "",
            "url": render_url(service, row),
            "port": render_port(service, row),
            "enabled": enabled,
            "autostart": autostart,
        }
        if service == "weaviate":
            entry["grpc_port"] = render_grpc_port(row)
        per_service[service] = entry
    return {"managed_services": managed, "adopted_containers": adopted, "services": per_service}


def plan_shell_lines(p: Mapping[str, Any]) -> list[str]:
    """:func:`plan` as POSIX shell assignments (every value ``shlex``-quoted)."""
    lines = [
        f"VCO_MANAGED_SERVICES={shlex.quote(' '.join(p['managed_services']))}",
        f"VCO_ADOPTED_CONTAINERS={shlex.quote(' '.join(p['adopted_containers']))}",
    ]
    for service, entry in p["services"].items():
        prefix = f"VCO_{service.upper()}_"
        for key in ("present", "mode", "container", "url", "port", "grpc_port", "enabled", "autostart"):
            if key not in entry:
                continue
            value = entry[key]
            if isinstance(value, bool):
                value = "1" if value else "0"
            lines.append(f"{prefix}{key.upper()}={shlex.quote(str(value))}")
    return lines


# ─── writing (the ONE writer) ───────────────────────────────────────────

#: Fields whose change reaches another surface (infra .env, project env, the
#: MCP registration, the compose service list). A change to only
#: ``verified_at`` / ``source`` / ``confirmed_by_user`` / ``compose_project`` /
#: ``autostart`` is recorded but triggers no follow-up chain.
_PROPAGATING_FIELDS: tuple[str, ...] = (
    "mode", "scheme", "host", "port", "grpc_port", "container_name", "data_mount", "enabled",
)


def _open_rw(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ServiceRegistryUnavailable(
            f"{db_path} does not exist — run `vct-hub --ensure-db` (install.py does) "
            "to create and migrate it"
        )
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_endpoints'"
        ).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise ServiceRegistryUnavailable(f"{db_path}: {exc}") from exc
    if found is None:
        conn.close()
        raise ServiceRegistryUnavailable(
            f"{db_path} has no service_endpoints table (schema older than migration 047) "
            "— run `vct-hub --ensure-db` with a current hub binary"
        )
    return conn


def _same(a: EndpointRow, b: EndpointRow, fields: Iterable[str]) -> bool:
    def norm(v: Any) -> Any:
        return dict(v) if isinstance(v, Mapping) else v
    return all(norm(getattr(a, f)) == norm(getattr(b, f)) for f in fields)


_COMPARED_FIELDS: tuple[str, ...] = tuple(
    f for f in EndpointRow.__dataclass_fields__ if f != "updated_at"
)


@dataclass
class WriteResult:
    """What :func:`write_rows` did. ``written``: services whose stored row
    changed at all. ``propagating``: the subset whose change must reach
    other surfaces (feed it to :func:`apply_change`)."""

    written: list[str] = field(default_factory=list)
    propagating: list[str] = field(default_factory=list)


def write_rows(rows: Iterable[EndpointRow], *, db_path: Optional[Path] = None,
               now_ms: Optional[int] = None) -> WriteResult:
    """Validate every row, then upsert the changed ones in ONE transaction.

    Nothing is written unless every row validates. An unchanged row is not
    rewritten (``updated_at`` keeps its value), so a re-run writes nothing.
    Raises :class:`InvalidEndpointRow` or :class:`ServiceRegistryUnavailable`.
    """
    batch = list(rows)
    seen: set[str] = set()
    for row in batch:
        validate_row(row)
        if row.service in seen:
            raise InvalidEndpointRow(f"{row.service}: given twice in one write")
        seen.add(row.service)
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    result = WriteResult()
    conn = _open_rw(_resolve_db_path(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = read_rows(conn)
        for row in batch:
            prior = current.get(row.service)
            if prior is not None and _same(prior, row, _COMPARED_FIELDS):
                continue
            mount = (
                json.dumps(dict(row.data_mount), sort_keys=True)
                if row.data_mount is not None else None
            )
            conn.execute(
                f"INSERT INTO service_endpoints ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                "ON CONFLICT(service) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "service"),
                (
                    row.service, row.mode, row.scheme, row.host, row.port, row.grpc_port,
                    row.container_name, row.compose_project, mount, int(row.enabled),
                    int(row.autostart), row.source, int(row.confirmed_by_user),
                    row.verified_at, stamp,
                ),
            )
            result.written.append(row.service)
            if prior is None or not _same(prior, row, _PROPAGATING_FIELDS):
                result.propagating.append(row.service)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        # The schema refused what validate_row accepted: the two drifted.
        raise InvalidEndpointRow(f"launcher.db refused the row: {exc}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


# ─── the follow-up chain (I5) ───────────────────────────────────────────

#: ``(infra_dir, rows) -> None`` — writes the managed ``infrastructure/.env``
#: keys. Production default: ``vco_lib.compose_env.write_service_keys``.
InfraEnvWriter = Callable[[Path, Mapping[str, EndpointRow]], None]
#: ``(db_path) -> Any`` — re-projects every registered project's env.
Reprojector = Callable[[Optional[Path]], Any]
#: ``(orchestrator_root) -> bool`` — refreshes the MCP registration.
Registrar = Callable[[Path], bool]


@dataclass
class ApplyChangeReport:
    changed: list[str] = field(default_factory=list)
    steps: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _default_infra_env_writer(infra_dir: Path, rows: Mapping[str, EndpointRow]) -> None:
    # compose_env stays the ONE writer of infrastructure/.env; the managed
    # service keys (ports, data-mount knobs, CODE_EMBED_OLLAMA_URL) are its
    # `write_service_keys`. Resolved at call time: a missing function is an
    # AttributeError here, loud, never a silent skip.
    from vco_lib import compose_env  # noqa: PLC0415

    compose_env.write_service_keys(infra_dir, rows)


def _default_reprojector(db_path: Optional[Path]) -> Any:
    from vco_lib.config_projection import reproject_all_registered_projects  # noqa: PLC0415

    return reproject_all_registered_projects(db_path=db_path)


def _default_registrar(orchestrator_root: Path) -> bool:
    """``vct-launcher --register-default-mcps <root>`` — the launcher binary
    is the one writer of ``~/.claude.json``; its registration reads the rows.
    No binary / non-zero exit / timeout → ``False`` (logged); the next
    install run retries."""
    from vco_lib.launcher_ensure import find_launcher_binary  # noqa: PLC0415

    binary = find_launcher_binary(repo_root=orchestrator_root)
    if binary is None:
        _LOG.warning("service_endpoints: no launcher binary found; MCP registration not refreshed")
        return False
    try:
        proc = subprocess.run(
            [str(binary), "--register-default-mcps", str(orchestrator_root)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.warning("service_endpoints: MCP registration refresh failed: %s", exc)
        return False
    if proc.returncode != 0:
        _LOG.warning(
            "service_endpoints: MCP registration refresh exited %s: %s",
            proc.returncode, (proc.stderr or "").strip()[-400:],
        )
        return False
    return True


def describe(service: str, row: Optional[EndpointRow]) -> str:
    """One human line for *service*'s current endpoint."""
    url = render_url(service, row)
    if row is None:
        return f"{service}: {url} (no row — compiled default)"
    who = {
        "vco_managed": "VCO-managed",
        "adopted_container": f"your container {row.container_name}",
        "adopted_external": "external",
    }[row.mode]
    extra = f", gRPC {render_grpc_port(row)}" if service == "weaviate" else ""
    return f"{service}: {url}{extra} ({who})"


#: app_state key holding the digest of the rows the follow-up chain last
#: propagated IN FULL (written by Python at the END of a successful
#: :func:`apply_change`). A chain cut short — the session hook's 8 s kill, a
#: crash, a failed step — leaves it behind the rows, and the next
#: :func:`commit_rows` that may propagate re-runs the chain even though no
#: row changed. That is what makes the chain durable: a row commit can never
#: strand the infra ``.env`` / project env / MCP registration on an old value.
PROPAGATED_DIGEST_KEY = "service_endpoints.propagated_digest"


def rows_digest(rows: Mapping[str, EndpointRow]) -> str:
    """Digest of what the follow-up chain carries — only the propagating
    fields, so a ``verified_at`` stamp or a ``source`` change never forces a
    re-run."""
    payload = {
        service: {
            f: (dict(v) if isinstance(v, Mapping) else v)
            for f in _PROPAGATING_FIELDS
            for v in [getattr(rows[service], f)]
        }
        for service in SERVICES if service in rows
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def propagated_digest(db_path: Optional[Path] = None) -> Optional[str]:
    """The digest recorded by the last complete chain, or ``None``."""
    from vco_lib.launcher_db_reader import _open_db_readonly  # noqa: PLC0415

    conn = _open_db_readonly(_resolve_db_path(db_path))
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                           (PROPAGATED_DIGEST_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]) if row is not None else None


def _record_propagated(db_path: Optional[Path], digest: str) -> None:
    try:
        conn = _open_rw(_resolve_db_path(db_path))
    except ServiceRegistryUnavailable as exc:
        _LOG.warning("service_endpoints: propagated digest not recorded: %s", exc)
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (PROPAGATED_DIGEST_KEY, digest, int(time.time() * 1000)),
            )
    except sqlite3.Error as exc:
        _LOG.warning("service_endpoints: propagated digest not recorded: %s", exc)
    finally:
        conn.close()


def apply_change(
    changed: Iterable[str],
    *,
    orchestrator_root: Path,
    db_path: Optional[Path] = None,
    write_infra_env: Optional[InfraEnvWriter] = None,
    reproject: Optional[Reprojector] = None,
    register_mcps: Optional[Registrar] = None,
    out: Callable[[str], None] = print,
) -> ApplyChangeReport:
    """The follow-up chain every row change triggers (plan §4a.5, I5):

    1. the managed ``infrastructure/.env`` keys (``write_infra_env``);
    2. every registered project's env re-projected (``reproject``);
    3. the MCP registration refreshed (``register_mcps``);
    4. one printed line per changed service.

    Each step is a seam (tests inject fakes; the defaults are production).
    A failing step is recorded in ``errors`` and logged, and the chain
    continues — a stale ``~/.claude.json`` must not keep the projects on the
    old endpoint. Nothing runs when *changed* is empty.
    """
    report = ApplyChangeReport(changed=[s for s in SERVICES if s in set(changed)])
    if not report.changed:
        return report
    root = Path(orchestrator_root)
    rows = load_rows(db_path)
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("infra_env", lambda: (write_infra_env or _default_infra_env_writer)(root / "infrastructure", rows)),
        ("reproject", lambda: (reproject or _default_reprojector)(db_path)),
        ("register_mcps", lambda: (register_mcps or _default_registrar)(root)),
    ]
    for name, step in steps:
        try:
            outcome = step()
        except Exception as exc:  # noqa: BLE001 - recorded + logged; the chain goes on
            report.errors[name] = f"{type(exc).__name__}: {exc}"
            report.steps[name] = "failed"
            _LOG.warning("service_endpoints: %s failed: %s", name, exc)
            continue
        if name == "register_mcps" and outcome is False:
            report.errors[name] = "registration not refreshed (see log); the next install run retries"
            report.steps[name] = "failed"
        else:
            report.steps[name] = "ok"
    for service in report.changed:
        line = describe(service, rows.get(service))
        report.lines.append(line)
        out(line)
    if report.ok:
        # LAST, and only for a complete chain: a chain cut short (killed,
        # crashed, a step failed) leaves the digest behind the rows.
        _record_propagated(db_path, rows_digest(rows))
    return report


def commit_rows(
    rows: Iterable[EndpointRow],
    *,
    orchestrator_root: Path,
    db_path: Optional[Path] = None,
    write_infra_env: Optional[InfraEnvWriter] = None,
    reproject: Optional[Reprojector] = None,
    register_mcps: Optional[Registrar] = None,
    out: Callable[[str], None] = print,
    now_ms: Optional[int] = None,
    propagate: bool = True,
) -> tuple[WriteResult, ApplyChangeReport]:
    """:func:`write_rows`, then :func:`apply_change` for the services whose
    change propagates. The one call a row-changing verb makes.

    Self-healing: when nothing changed but the rows' digest differs from the
    one the last complete chain recorded (:data:`PROPAGATED_DIGEST_KEY`), the
    chain runs for every recorded service anyway. ``propagate=False`` writes
    the rows and runs NO chain (the session phase, whose caller is killed
    after 8 s): the digest then stays behind and the next caller that may
    propagate converges."""
    result = write_rows(rows, db_path=db_path, now_ms=now_ms)
    if not propagate:
        return result, ApplyChangeReport()
    changed = list(result.propagating)
    if not changed:
        current = load_rows(db_path)
        if current and rows_digest(current) != propagated_digest(db_path):
            changed = [s for s in SERVICES if s in current]
    report = apply_change(
        changed, orchestrator_root=orchestrator_root, db_path=db_path,
        write_infra_env=write_infra_env, reproject=reproject,
        register_mcps=register_mcps, out=out,
    )
    return result, report


# ─── CLI ────────────────────────────────────────────────────────────────


def _show_payload(rows: Mapping[str, EndpointRow]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for service in SERVICES:
        row = rows.get(service)
        entry: dict[str, Any] = {
            "present": row is not None,
            "url": render_url(service, row),
            "port": render_port(service, row),
        }
        if service == "weaviate":
            entry["grpc_port"] = render_grpc_port(row)
        entry["row"] = row.to_json() if row is not None else None
        services[service] = entry
    return {"schema": 1, "services": services}


def _cli_show(args: argparse.Namespace) -> int:
    rows = load_rows(args.db_path)
    if args.json:
        print(json.dumps(_show_payload(rows), indent=2, sort_keys=True))
        return 0
    for service in SERVICES:
        print(describe(service, rows.get(service)))
    return 0


def _cli_resolve(args: argparse.Namespace) -> int:
    rows = load_rows(args.db_path)
    row = rows.get(args.service)
    value: Any
    if args.field == "url":
        value = render_url(args.service, row)
    elif args.field == "port":
        value = render_port(args.service, row)
    elif args.field == "grpc_port":
        if args.service != "weaviate":
            print("grpc_port exists only for weaviate", file=sys.stderr)
            return 2
        value = render_grpc_port(row)
    else:  # mode
        value = row.mode if row is not None else "vco_managed"
    print(value)
    return 0


def _cli_plan(args: argparse.Namespace) -> int:
    p = plan(load_rows(args.db_path))
    if args.json:
        print(json.dumps(p, indent=2, sort_keys=True))
    else:
        print("\n".join(plan_shell_lines(p)))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """The CLI's parser — also what the deferral-command sweep validates every
    printed ``python -m vco_lib.service_endpoints …`` remedy against."""
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.service_endpoints",
        description="Where Weaviate / Ollama / code-embed are reached (launcher.db service_endpoints).",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    def db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db-path", type=Path, default=None,
                       help="launcher.db (default: <vct_root_dir>/launcher.db)")

    p_show = sub.add_parser("show", help="every service's endpoint (and its row)")
    p_show.add_argument("--json", action="store_true")
    db_arg(p_show)
    p_show.set_defaults(handler=_cli_show)

    p_res = sub.add_parser("resolve", help="print one resolved value")
    p_res.add_argument("--service", required=True, choices=SERVICES)
    p_res.add_argument("--field", default="url", choices=("url", "port", "grpc_port", "mode"))
    db_arg(p_res)
    p_res.set_defaults(handler=_cli_resolve)

    p_plan = sub.add_parser("plan", help="what lifecycle code may act on")
    fmt = p_plan.add_mutually_exclusive_group(required=True)
    fmt.add_argument("--shell", action="store_true", help="POSIX shell assignments")
    fmt.add_argument("--json", action="store_true")
    db_arg(p_plan)
    p_plan.set_defaults(handler=_cli_plan)

    # The detecting / row-changing verbs live with the decision logic
    # (vco_lib.service_reconcile); this module stays the store + render.
    def reconcile_verb(name: str) -> Callable[[argparse.Namespace], int]:
        def handler(args: argparse.Namespace) -> int:
            from vco_lib import service_reconcile  # noqa: PLC0415

            return service_reconcile.cli(name, args)
        return handler

    def root_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", type=Path, default=None,
                       help="orchestrator root (default: this checkout)")

    p_cand = sub.add_parser("candidates", help="every Weaviate/Ollama/code-embed found, and whether VCO can use it")
    p_cand.add_argument("--service", choices=SERVICES, default=None)
    p_cand.add_argument("--json", action="store_true")
    p_cand.set_defaults(handler=reconcile_verb("candidates"))

    p_adopt = sub.add_parser("adopt", help="point VCO at a running Weaviate/Ollama")
    p_adopt.add_argument("--service", required=True, choices=("weaviate", "ollama"))
    which = p_adopt.add_mutually_exclusive_group(required=True)
    which.add_argument("--container", help="a container VCO then starts/stops by name, never recreates")
    which.add_argument("--url", help="a URL (a native process or another host)")
    p_adopt.add_argument("--accept-empty-kg", action="store_true",
                         help="switch away from a Weaviate that holds VCO data")
    db_arg(p_adopt)
    root_arg(p_adopt)
    p_adopt.set_defaults(handler=reconcile_verb("adopt"))

    p_copy = sub.add_parser("use-vco-copy", help="let VCO run its own copy of a service")
    p_copy.add_argument("--service", required=True, choices=SERVICES)
    p_copy.add_argument("--port", type=int, default=None)
    p_copy.add_argument("--accept-empty-kg", action="store_true",
                        help="switch away from a Weaviate that holds VCO data")
    db_arg(p_copy)
    root_arg(p_copy)
    p_copy.set_defaults(handler=reconcile_verb("use-vco-copy"))

    p_move = sub.add_parser(
        "move", help="follow an adopted Weaviate/Ollama to its new endpoint, or move VCO's own "
                     "Weaviate/Ollama/code-embed to a new port (re-created with its data)")
    p_move.add_argument("--service", required=True, choices=SERVICES)
    p_move.add_argument("--port", type=int, default=None)
    p_move.add_argument("--url", default=None, help="an external endpoint's new URL")
    p_move.add_argument("--grpc-port", type=int, default=None, help="Weaviate's gRPC port: at --url (adopted), or where VCO's own "
                        "Weaviate moves it (default: keeps its offset from --port)")
    p_move.add_argument("--accept-empty-kg", action="store_true",
                        help="follow a Weaviate to an endpoint that holds no VCO data")
    db_arg(p_move)
    root_arg(p_move)
    p_move.set_defaults(handler=reconcile_verb("move"))

    p_hand = sub.add_parser("hand-to-vco", help="let VCO's compose manage an adopted container (same data mount)")
    p_hand.add_argument("--service", required=True, choices=("weaviate", "ollama"))
    root_arg(p_hand)
    p_hand.set_defaults(handler=reconcile_verb("hand-to-vco"))

    p_rec = sub.add_parser("reconcile", help="re-check the rows against what is running")
    p_rec.add_argument("--phase", choices=("session", "update"), default="session")
    p_rec.add_argument("--json", action="store_true")
    db_arg(p_rec)
    root_arg(p_rec)
    p_rec.set_defaults(handler=reconcile_verb("reconcile"))

    return parser


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(_main())
