# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What on this machine belongs to a PROJECT, not to VCO (v0.2.102 W5-A, M3).

A project may run its own Weaviate / Ollama for its own runtime, next to the
development KG VCO keeps for it. VCO's detectors classify collections and
containers by NAME SHAPE and by schema similarity, and a project whose runtime
was built with VCO-shaped tooling is exactly where those heuristics misfire: a
runtime collection reads as an "unclaimed" leftover, a runtime container as a
stopped VCO service to heal or an instance to adopt. A declaration is the
explicit answer: "this is mine — leave it alone".

ONE reader for the declarations, used by every Python consumer; the writer is
the launcher (Identity tab → "Infrastructure this project owns", migration 049
``project_infra_declarations``) and the Rust side reads the same table
(``vct_launcher_core::db::infra_declarations``).

Kinds
-----
``external_collection``
    A Weaviate class the project owns. Skipped — and named as skipped — by the
    doctor's binding-evidence scan (:mod:`vco_lib.kg_binding_doctor`): it is
    never "unclaimed", never "fixture-shaped", never evidence the heal could
    re-point a binding at.
``unmanaged_container``
    A container VCO never heals, starts, re-creates, labels or adopts: the hub
    watchdog (``infra_watchdog.rs``), :mod:`vco_lib.compose_recovery`'s heals,
    :mod:`vco_lib.service_detection` (which feeds adoption) and the doctor.
``local_port``
    A loopback port one of the project's own services listens on. Projected
    into THAT project's env as :data:`ENV_SSRF_EXTRA_LOCAL_PORTS`, which the
    WebFetch SSRF guard (:mod:`vco_lib.ssrf_url`) allows; never probed for
    adoption by service detection.

Scope: a container or a collection is ONE object on the machine, so a
declaration by any registered project protects it machine-wide. The SSRF
allowance alone is per-project — it rides the declaring project's env.

Sources, unioned
----------------
1. ``launcher.db`` ``project_infra_declarations`` (read-only open through
   :mod:`vco_lib.launcher_db_reader`).
2. The env keys :data:`KIND_ENV` names — what ``config_projection`` writes into
   a project's ``.claude/settings.json`` ``env`` from (1), and the only channel
   a launcher-less install has. A comma / whitespace separated list. Only the
   Python readers see this channel: the hub watchdog reads ``launcher.db``
   alone (``Db::unmanaged_container_reasons``).

Read failures are explicit: :attr:`Declarations.resolvable` is ``False`` when a
``launcher.db`` EXISTS but could not be read. A consumer about to act on a
container (a heal) must then refuse — an unreadable "do not touch" list is not
an empty one. A machine with no ``launcher.db`` at all has nothing declared
there and stays resolvable.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

KIND_EXTERNAL_COLLECTION = "external_collection"
KIND_UNMANAGED_CONTAINER = "unmanaged_container"
KIND_LOCAL_PORT = "local_port"
#: Must match the CHECK in migration 049 and
#: ``vct_launcher_core::db::infra_declarations::KINDS``.
KINDS: tuple[str, ...] = (
    KIND_EXTERNAL_COLLECTION,
    KIND_UNMANAGED_CONTAINER,
    KIND_LOCAL_PORT,
)

ENV_EXTERNAL_COLLECTIONS = "VCT_EXTERNAL_COLLECTIONS"
ENV_UNMANAGED_CONTAINERS = "VCT_UNMANAGED_CONTAINERS"
ENV_SSRF_EXTRA_LOCAL_PORTS = "VCT_SSRF_EXTRA_LOCAL_PORTS"
#: The env key each kind is projected under (``config_projection``).
KIND_ENV: dict[str, str] = {
    KIND_EXTERNAL_COLLECTION: ENV_EXTERNAL_COLLECTIONS,
    KIND_UNMANAGED_CONTAINER: ENV_UNMANAGED_CONTAINERS,
    KIND_LOCAL_PORT: ENV_SSRF_EXTRA_LOCAL_PORTS,
}

TABLE = "project_infra_declarations"

#: Weaviate class names: a letter, then letters / digits / underscores.
#: Weaviate capitalises the first letter, so the stored form does too.
_CLASS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}")
#: Docker / podman container names.
_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_PORT_RE = re.compile(r"[0-9]+")
_SPLIT_RE = re.compile(r"[\s,]+")
#: The ASCII whitespace both validators trim (the Rust side trims the same set).
_TRIM = " \t\n\r\x0b\x0c"


def normalise(kind: str, value: object) -> Optional[str]:
    """The stored form of ``value`` for ``kind``, or ``None`` when invalid.

    MUST MATCH ``vct_launcher_core::db::infra_declarations::normalise`` (the
    GUI writer validates with it; this reader re-validates env input).
    """
    text = str(value if value is not None else "").strip(_TRIM)
    if not text:
        return None
    if kind == KIND_EXTERNAL_COLLECTION:
        if not _CLASS_RE.fullmatch(text):
            return None
        return text[0].upper() + text[1:]
    if kind == KIND_UNMANAGED_CONTAINER:
        return text if _CONTAINER_RE.fullmatch(text) else None
    if kind == KIND_LOCAL_PORT:
        if not _PORT_RE.fullmatch(text):  # ASCII digits only (str.isdigit is Unicode)
            return None
        port = int(text)
        return str(port) if 1 <= port <= 65535 else None
    return None


def parse_env_list(kind: str, raw: Optional[str]) -> list[str]:
    """The valid, normalised, de-duplicated values of a projected env list."""
    out: list[str] = []
    for token in _SPLIT_RE.split(raw or ""):
        value = normalise(kind, token)
        if value is not None and value not in out:
            out.append(value)
    return out


@dataclass(frozen=True)
class Declaration:
    """One declared value and who declared it."""

    kind: str
    value: str
    #: The declaring project's display name, or ``env:<KEY>`` for an env value.
    source: str
    project_id: str = ""


@dataclass(frozen=True)
class Declarations:
    """Every declaration this process can see (DB ∪ env)."""

    resolvable: bool
    rows: tuple[Declaration, ...] = ()

    def values(self, kind: str) -> frozenset[str]:
        return frozenset(d.value for d in self.rows if d.kind == kind)

    def sources(self, kind: str, value: str) -> tuple[str, ...]:
        """Who declared ``value`` (sorted, unique) — empty when nobody did."""
        return tuple(sorted({d.source for d in self.rows if d.kind == kind and d.value == value}))

    def why(self, kind: str, value: str) -> Optional[str]:
        """The human reason a consumer prints when it skips ``value``."""
        who = self.sources(kind, value)
        if not who:
            return None
        label = {
            KIND_EXTERNAL_COLLECTION: "declared external",
            KIND_UNMANAGED_CONTAINER: "declared unmanaged",
            KIND_LOCAL_PORT: "declared as a project's own local port",
        }.get(kind, "declared")
        return f"{label} by {', '.join(who)}"

    def for_project(self, project_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {k: [] for k in KINDS}
        for d in self.rows:
            if d.project_id == project_id and d.value not in out[d.kind]:
                out[d.kind].append(d.value)
        return {k: sorted(v, key=_sort_key(k)) for k, v in out.items()}


def _sort_key(kind: str):
    if kind == KIND_LOCAL_PORT:
        return lambda v: int(v)
    return lambda v: v


def project_values_on_conn(conn: sqlite3.Connection, project_id: str) -> dict[str, list[str]]:
    """This project's declared values per kind, read on an OPEN connection.

    For ``config_projection.project_env_from_db``, which already holds the
    read-only connection. A launcher.db older than migration 049 has no table
    and therefore no declarations: every kind is an empty list. Invalid stored
    values are dropped (the reader re-validates; it never trusts the row).
    """
    out: dict[str, list[str]] = {k: [] for k in KINDS}
    try:
        rows = conn.execute(
            f"SELECT kind, value FROM {TABLE} WHERE project_id = ?", (project_id,)
        ).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        kind, raw = row[0], row[1]
        value = normalise(kind, raw) if kind in out else None
        if value is not None and value not in out[kind]:
            out[kind].append(value)
    return {k: sorted(v, key=_sort_key(k)) for k, v in out.items()}


def _db_rows(db_path: Optional[Path]) -> Optional[list[Declaration]]:
    """Every declared row in launcher.db; ``[]`` when there is no launcher.db
    or no table yet; ``None`` when a launcher.db exists and could not be read."""
    from vco_lib.launcher_db_reader import _discover_db_path, _open_db_readonly

    path = Path(db_path) if db_path is not None else _discover_db_path()
    if path is None or not path.is_file():
        return []
    conn = _open_db_readonly(path)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                f"SELECT d.kind, d.value, d.project_id, p.name FROM {TABLE} d "
                "LEFT JOIN projects p ON p.id = d.project_id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            return None
        except sqlite3.Error:
            return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    out: list[Declaration] = []
    for kind, raw, project_id, name in rows:
        value = normalise(kind, raw) if kind in KINDS else None
        if value is None:
            continue
        source = f"project '{name}'" if name else f"project id {project_id}"
        out.append(Declaration(kind=kind, value=value, source=source, project_id=str(project_id or "")))
    return out


def _env_rows(env: Mapping[str, str]) -> list[Declaration]:
    out: list[Declaration] = []
    for kind, key in KIND_ENV.items():
        for value in parse_env_list(kind, env.get(key)):
            out.append(Declaration(kind=kind, value=value, source=f"env:{key}"))
    return out


def load(*, db_path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
         include_db: bool = True) -> Declarations:
    """Every declaration visible here — launcher.db ∪ the projected env.

    Never raises. ``resolvable`` is ``False`` only when a launcher.db exists
    and could not be read (see the module docstring).
    """
    env_map = os.environ if env is None else env
    rows: list[Declaration] = list(_env_rows(env_map))
    resolvable = True
    if include_db:
        try:
            db = _db_rows(db_path)
        except Exception:  # noqa: BLE001 — a reader defect is "could not read"
            db = None
        if db is None:
            resolvable = False
        else:
            rows.extend(db)
    return Declarations(resolvable=resolvable, rows=tuple(rows))


def unmanaged_containers(declarations: Optional[Declarations] = None) -> frozenset[str]:
    """Machine-wide declared-unmanaged container names."""
    decl = declarations if declarations is not None else load()
    return decl.values(KIND_UNMANAGED_CONTAINER)


def external_collections(declarations: Optional[Declarations] = None) -> frozenset[str]:
    """Machine-wide declared-external Weaviate class names."""
    decl = declarations if declarations is not None else load()
    return decl.values(KIND_EXTERNAL_COLLECTION)


def local_ports(declarations: Optional[Declarations] = None) -> frozenset[int]:
    """Machine-wide declared local ports (service detection never probes them)."""
    decl = declarations if declarations is not None else load()
    return frozenset(int(v) for v in decl.values(KIND_LOCAL_PORT))


def ssrf_extra_local_ports(env: Optional[Mapping[str, str]] = None) -> list[int]:
    """The ports :data:`ENV_SSRF_EXTRA_LOCAL_PORTS` allows for THIS process's
    project (env only, by design: the allowance is per-project)."""
    env_map = os.environ if env is None else env
    return [int(v) for v in parse_env_list(KIND_LOCAL_PORT, env_map.get(ENV_SSRF_EXTRA_LOCAL_PORTS))]


def env_value(values: Iterable[str]) -> str:
    """The projected env value for a list of declared values."""
    return ",".join(values)
