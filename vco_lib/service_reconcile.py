# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Decide the ``service_endpoints`` rows from live evidence, and import what
past VCO versions wrote (v0.2.97, plan §4b / §4e).

``install.py`` step [5b] is a thin call into :func:`reconcile`; the GUI and the
CLI reach the same code through ``python -m vco_lib.service_endpoints
candidates | adopt | use-vco-copy | hand-to-vco | reconcile``. Rows are
written only through :mod:`vco_lib.service_endpoints` (the one writer).

**The ranking** (per service; :func:`decide`, pure and table-tested):

1. A live, compatible candidate that holds VCO data (Weaviate: VCO classes;
   Ollama: VCO models) and is the endpoint the continuity evidence names
   (what the root project's projected env pointed clients at) → take it.
2. A single live candidate holding VCO data → take it.
3. Several → the continuity pick, else the one on VCO's canonical port, else
   the installer-owned one; ``service_endpoint_ambiguous`` lists them all.
4. Live and compatible, no VCO data (a third-party instance) → the §4b offer:
   an interactive run asks (default: use it); ``--on-conflict`` answers for a
   script; unattended, an **Ollama** is adopted
   (``service_adopted_without_prompt``) and a **Weaviate** is NOT (owner
   ruling Q1): VCO writes a *disabled* ``vco_managed`` row on a free port —
   nothing starts, nothing is written into the other instance, clients fail
   loudly rather than land somewhere unconfirmed — and records
   ``service_adoption_confirmation_required`` naming both choices.
5. Nothing live → keep the previously effective endpoint (continuity, else the
   highest-precedence legacy statement, else the compiled default). A stopped
   container publishing it is kept by name. ``verified_at`` stays NULL, and an
   endpoint VCO cannot start is reported (``service_endpoint_unreachable``).

What the container is decides the mode: VCO's own container under the
installer's compose project → ``vco_managed``; VCO's own container under the
legacy compose home, or someone else's container → ``adopted_container``
(started/stopped by name, never recreated); a process or remote host →
``adopted_external``. code-embed is always ``vco_managed``; a code-embed
container another compose project owns is recreated WITH its cache by
``service_lifecycle.migrate_code_embed`` (plan §4c).

**Legacy sources** (read only here; no resolver reads them any more):
``~/.vct/services.toml``, the app_state ``*.port_override`` keys,
``vct-config.toml`` beside a launcher/hub binary (the shipped default URL is
"no statement"), ``VCT_*_URL`` / ``WEAVIATE_URL`` / ``*_PORT`` in the
installer's environment, install.py's old alt-port override file (identified
by its header), and — as continuity — the root project's projected
``.claude/settings.json`` / ``.claude/env``. After a successful import
services.toml and the override file are RENAMED (never deleted), consumed
``port_override`` keys are deleted, and legacy-default
``project_kg_bindings.weaviate_url`` literals become NULL.

Every probe is an injectable seam (``run`` / ``fetch`` / ``tcp_open`` /
``port_free``); tests never touch a runtime, a port or the real launcher.db.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from vco_lib import containers as _containers
from vco_lib import kg_binding_heal as _kg_binding_heal
from vco_lib import service_detection as _det
from vco_lib import service_adoption as _service_adoption
from vco_lib import service_endpoints as _se
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "CID_ADOPTED_WITHOUT_PROMPT",
    "CID_AMBIGUOUS",
    "CID_CONFIG_DRIFT",
    "CID_CONFIRMATION_REQUIRED",
    "CID_LEGACY_UNIMPORTED",
    "CID_MIGRATED",
    "CID_REGISTRY_UNAVAILABLE",
    "CID_UNREACHABLE",
    "Choice",
    "LegacyEvidence",
    "Outcome",
    "ReconcileResult",
    "ServiceInputs",
    "Statement",
    "decide",
    "ensure_registry",
    "gather_legacy",
    "parse_service_flag",
    "reconcile",
    "verify",
]

CID_REGISTRY_UNAVAILABLE = "service_registry_unavailable"
CID_UNREACHABLE = "service_endpoint_unreachable"
CID_AMBIGUOUS = "service_endpoint_ambiguous"
CID_LEGACY_UNIMPORTED = "legacy_service_statement_unimported"
CID_CONFIG_DRIFT = "adopted_service_config_drift"
CID_ADOPTED_WITHOUT_PROMPT = "service_adopted_without_prompt"
CID_MIGRATED = "service_endpoints_migrated"
CID_CONFIRMATION_REQUIRED = "service_adoption_confirmation_required"

SERVICES = _se.SERVICES
MIGRATED_SUFFIX = ".migrated-v0297"
RETIRED_SUFFIX = ".retired-v0297"
#: What the release archive ships UNCOMMENTED in ``vct-config.toml`` (plan §1
#: row 4) — the default, not a statement anybody made.
SHIPPED_VCT_CONFIG_WEAVIATE_URL = "http://localhost:8081"
#: The first line install.py's alt-port override file always carried.
ALT_PORT_OVERRIDE_HEADER = "Auto-generated by install.py — alt-port mappings"
_OVERRIDE_NAMES: tuple[str, ...] = ("docker-compose.override.yml", "compose.override.yaml")
#: Container-side data mount per service, and the behaviour-critical env an
#: ADOPTED container is compared against — one home: vco_lib.service_adoption.
DATA_MOUNT_TARGETS: Mapping[str, str] = _service_adoption.CONTAINER_MOUNT_TARGETS
_OLLAMA_CANONICAL_ENV: Mapping[str, str] = _service_adoption.CANONICAL_ENV_TARGETS["ollama"]
_WEAVIATE_DRIFT_KEYS: tuple[str, ...] = _service_adoption.WEAVIATE_RECLAIM_ENV_KEYS

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
PortFreeFn = Callable[[int], bool]
PromptFn = Callable[[str, _det.Candidate], str]
LogFn = Callable[[str], None]


def default_port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_host(host: str) -> str:
    return "localhost" if host in ("127.0.0.1", "localhost") else host


def parse_url(value: str) -> Optional[tuple[str, str, int]]:
    """``scheme://host[:port]`` → ``(scheme, host, port)``; ``None`` if unusable."""
    s = (value or "").strip()
    if "://" not in s:
        return None
    scheme, rest = s.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        return None
    authority = rest.split("/", 1)[0]
    if authority.startswith("["):
        host = authority[: authority.find("]") + 1] if "]" in authority else ""
    else:
        host = authority.rsplit(":", 1)[0] if ":" in authority else authority
    port = _se.port_of_url(s)
    if not host or port is None:
        return None
    return scheme, host, port


# ─── legacy evidence ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Statement:
    """One endpoint a past VCO version (or the user) stated."""

    service: str
    source: str               # e.g. "services.toml", "app_state:weaviate.port_override"
    value: str                # what was stated, verbatim
    host: str
    port: int
    scheme: str = "http"
    grpc_port: Optional[int] = None
    legacy_mode: Optional[str] = None   # services.toml: parallel | adopt | refuse
    container_name: Optional[str] = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return (_norm_host(self.host), self.port)

    @property
    def kind(self) -> str:
        return self.source.split(":", 1)[0]


@dataclass
class LegacyEvidence:
    #: Legacy statements, highest precedence first.
    statements: list[Statement] = field(default_factory=list)
    #: What the root project's projected env pointed clients at.
    continuity: dict[str, Statement] = field(default_factory=dict)
    services_toml: Optional[Path] = None
    override_files: list[Path] = field(default_factory=list)
    port_override_keys: dict[str, str] = field(default_factory=dict)

    def for_service(self, service: str) -> list[Statement]:
        return [s for s in self.statements if s.service == service]


def _stmt_from_url(service: str, source: str, value: str, **kw: Any) -> Optional[Statement]:
    parsed = parse_url(value)
    if parsed is None:
        return None
    scheme, host, port = parsed
    return Statement(service, source, value, host, port, scheme=scheme, **kw)


def _stmt_from_port(service: str, source: str, value: str, **kw: Any) -> Optional[Statement]:
    v = (value or "").strip()
    if not v.isdigit() or not 1 <= int(v) <= 65535:
        return None
    return Statement(service, source, v, "localhost", int(v), **kw)


def _services_toml_statements(path: Path) -> list[Statement]:
    from vco_lib.service_adoption import read_services_toml  # noqa: PLC0415

    out: list[Statement] = []
    for entry in read_services_toml(path).get("services", []) or []:
        service = str(entry.get("name", ""))
        mode = str(entry.get("mode", ""))
        if service not in SERVICES or mode not in ("parallel", "adopt", "refuse"):
            continue
        container = str(entry.get("container_name") or "") or None
        stmt: Optional[Statement] = None
        if mode == "parallel" and entry.get("parallel_port") is not None:
            stmt = _stmt_from_port(service, "services.toml", str(entry["parallel_port"]),
                                   legacy_mode=mode, container_name=container)
        elif entry.get("external_url"):
            stmt = _stmt_from_url(service, "services.toml", str(entry["external_url"]),
                                  legacy_mode=mode, container_name=container)
        if stmt is not None:
            out.append(stmt)
    return out


def _port_override_statements(db_path: Optional[Path]) -> tuple[list[Statement], dict[str, str]]:
    from vco_lib.launcher_db_reader import _open_db_readonly  # noqa: PLC0415

    conn = _open_db_readonly(_se._resolve_db_path(db_path))
    if conn is None:
        return [], {}
    out: list[Statement] = []
    keys: dict[str, str] = {}
    try:
        for service, key in _se.RETIRED_APP_STATE_KEYS.items():
            try:
                row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
            except sqlite3.Error:
                return [], {}
            if row is None:
                continue
            keys[service] = key
            raw = str(row[0])
            try:  # the launcher stores app_state values JSON-encoded
                raw = str(json.loads(raw))
            except ValueError:
                pass
            stmt = _stmt_from_port(service, f"app_state:{key}", raw)
            if stmt is not None:
                out.append(stmt)
    finally:
        conn.close()
    return out, keys


def default_vct_config_dirs(orchestrator_root: Path, env: Mapping[str, str]) -> list[Path]:
    """Every place a launcher/hub binary (and so a ``vct-config.toml``) lives."""
    root = Path(orchestrator_root)
    dirs = [root, root / "launcher" / "dist"]
    dist = root / "launcher" / "dist"
    if dist.is_dir():
        dirs.extend(sorted(p for p in dist.iterdir() if p.is_dir()))
    home = env.get("USERPROFILE") if os.name == "nt" else None
    home = home or env.get("HOME") or ""
    if home:
        dirs.append(Path(home) / ".vct" / "bin")
    hub_bin = (env.get("VCT_HUB_BIN") or "").strip()
    if hub_bin:
        dirs.append(Path(hub_bin).parent)
    return list(dict.fromkeys(dirs))


def _vct_config_statements(dirs: Iterable[Path]) -> list[Statement]:
    import tomllib  # noqa: PLC0415

    out: list[Statement] = []
    for d in dirs:
        path = Path(d) / "vct-config.toml"
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for service, key in (("weaviate", "weaviate_url"), ("ollama", "ollama_url")):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if service == "weaviate" and value.strip().rstrip("/") == SHIPPED_VCT_CONFIG_WEAVIATE_URL:
                continue  # the shipped default is not a statement
            stmt = _stmt_from_url(service, f"vct-config.toml:{path}", value)
            if stmt is not None:
                out.append(stmt)
    return out


def _env_statements(env: Mapping[str, str]) -> list[Statement]:
    out: list[Statement] = []
    grpc = (env.get("WEAVIATE_GRPC_PORT") or "").strip()
    grpc_port = int(grpc) if grpc.isdigit() else None
    for name, service, is_url in (
        ("VCT_WEAVIATE_URL", "weaviate", True),
        ("VCT_OLLAMA_URL", "ollama", True),
        ("WEAVIATE_URL", "weaviate", True),
        ("WEAVIATE_PORT", "weaviate", False),
        ("OLLAMA_URL", "ollama", True),
        ("OLLAMA_PORT", "ollama", False),
        ("CODE_EMBED_PORT", "code_embed", False),
    ):
        value = (env.get(name) or "").strip()
        if not value:
            continue
        kw: dict[str, Any] = {"grpc_port": grpc_port} if service == "weaviate" else {}
        maker = _stmt_from_url if is_url else _stmt_from_port
        stmt = maker(service, f"env:{name}", value, **kw)
        if stmt is not None:
            out.append(stmt)
    return out


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip("'\"")
    return out


def _continuity(orchestrator_root: Path) -> dict[str, Statement]:
    """The endpoints the root project's projected env pointed clients at."""
    root = Path(orchestrator_root)
    settings = root / ".claude" / "settings.json"
    env: dict[str, str] = {}
    source = ""
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("env"), dict):
            env = {str(k): str(v) for k, v in data["env"].items()}
            source = f"continuity:{settings}"
    except (OSError, ValueError):
        env = {}
    if not env:
        env = _read_env_file(root / ".claude" / "env")
        source = f"continuity:{root / '.claude' / 'env'}"
    grpc = (env.get("WEAVIATE_GRPC_PORT") or env.get("GRPC_PORT") or "").strip()
    out: dict[str, Statement] = {}
    for service, keys in (
        ("weaviate", ("WEAVIATE_URL",)),
        ("ollama", ("OLLAMA_URL",)),
        ("code_embed", ("CODE_EMBED_SERVICE_URL", "CODE_EMBED_URL")),
    ):
        for key in keys:
            value = env.get(key, "")
            kw: dict[str, Any] = {}
            if service == "weaviate" and grpc.isdigit():
                kw["grpc_port"] = int(grpc)
            stmt = _stmt_from_url(service, source, value, **kw) if value else None
            if stmt is not None:
                out[service] = stmt
                break
    return out


_OVERRIDE_PORT_RE = re.compile(r"""^\s*-\s*["']?(?:[\d.]+:)?(\d+):(\d+)["']?\s*$""")
_OVERRIDE_SERVICE_RE = re.compile(r"^  ([a-z_]+):\s*$")


def _override_statements(infra_dir: Path) -> tuple[list[Statement], list[Path]]:
    """install.py's alt-port override (header-identified; a file without the
    header is user-authored and never read or touched)."""
    out: list[Statement] = []
    files: list[Path] = []
    for name in _OVERRIDE_NAMES:
        path = Path(infra_dir) / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if ALT_PORT_OVERRIDE_HEADER not in text.split("\n", 1)[0]:
            continue
        files.append(path)
        current = ""
        http: dict[str, int] = {}
        grpc: Optional[int] = None
        for line in text.splitlines():
            m = _OVERRIDE_SERVICE_RE.match(line)
            if m:
                current = m.group(1)
                continue
            m = _OVERRIDE_PORT_RE.match(line)
            if not m or current not in SERVICES:
                continue
            host_port, cont_port = int(m.group(1)), int(m.group(2))
            if current == "weaviate" and cont_port == _det.WEAVIATE_CONTAINER_GRPC_PORT:
                grpc = host_port
            elif cont_port == _det.CONTAINER_PORTS[current]:
                http[current] = host_port
        for service, port in http.items():
            out.append(Statement(service, f"compose-override:{path}", str(port), "localhost", port,
                                 grpc_port=grpc if service == "weaviate" else None,
                                 legacy_mode="parallel"))
    return out, files


def gather_legacy(
    *,
    orchestrator_root: Path,
    db_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    services_toml_path: Optional[Path] = None,
    vct_config_dirs: Optional[Sequence[Path]] = None,
) -> LegacyEvidence:
    """Read every legacy source (no network, no writes). Precedence (first
    wins when nothing is live): services.toml > port_override > the alt-port
    override file > vct-config.toml > VCT_*_URL > WEAVIATE_URL / *_PORT."""
    env = os.environ if env is None else env
    root = Path(orchestrator_root)
    if services_toml_path is None:
        from vco_lib.service_adoption import services_toml_path as _stp  # noqa: PLC0415

        services_toml_path = _stp()
    ev = LegacyEvidence()
    if Path(services_toml_path).is_file():
        ev.services_toml = Path(services_toml_path)
        ev.statements.extend(_services_toml_statements(ev.services_toml))
    overrides, ev.port_override_keys = _port_override_statements(db_path)
    ev.statements.extend(overrides)
    override_stmts, ev.override_files = _override_statements(root / "infrastructure")
    ev.statements.extend(override_stmts)
    dirs = default_vct_config_dirs(root, env) if vct_config_dirs is None else vct_config_dirs
    ev.statements.extend(_vct_config_statements(dirs))
    ev.statements.extend(_env_statements(env))
    ev.continuity = _continuity(root)
    return ev


# ─── the decision (pure) ────────────────────────────────────────────────


@dataclass(frozen=True)
class Choice:
    """An explicit per-service answer: ``--service <svc>=adopt:container:<name>``,
    ``=adopt:url:<url>`` or ``=vco[:<port>]``."""

    service: str
    kind: str                  # adopt_container | adopt_url | vco
    value: Optional[str] = None


#: Grammar DATA of the ``--service`` flag, locked by the ONE committed table
#: ``tests/fixtures/service_flag_grammar_cases.json`` (A>B>C tier C: the
#: onboarding wizard runs before the orchestrator clone exists, so the
#: launcher cannot call Python and mirrors this parser — the table, a parity
#: test on both sides and the MUST MATCH comments are the lock).
#: MUST MATCH launcher/src-tauri/src/commands/installer.rs::parse_service_choice.
SERVICE_FLAG_URL_MAX = 512
SERVICE_FLAG_PORT_MIN = 1
SERVICE_FLAG_PORT_MAX = 65535


def _port_text_ok(text: str) -> bool:
    """``vco:<port>`` port text: ASCII digits in 1..=65535 (``str.isdigit``
    alone would take Unicode digits and any length)."""
    return (text.isascii() and text.isdigit()
            and SERVICE_FLAG_PORT_MIN <= int(text) <= SERVICE_FLAG_PORT_MAX)


def parse_service_flag(flag: str) -> Choice:
    """Parse one ``<svc>=<choice>``; raise ``ValueError`` with a usable message.

    MUST MATCH launcher/src-tauri/src/commands/installer.rs::parse_service_choice
    (the wizard's mirror). Every case in
    ``tests/fixtures/service_flag_grammar_cases.json`` runs through BOTH.
    """
    service, sep, rest = (flag or "").partition("=")
    service = service.strip()
    if not sep or service not in SERVICES:
        raise ValueError(f"--service {flag!r}: expected <weaviate|ollama|code_embed>=<choice>")
    choice: Optional[Choice] = None
    if rest.startswith("adopt:container:") and rest[len("adopt:container:"):]:
        choice = Choice(service, "adopt_container", rest[len("adopt:container:"):])
    elif rest.startswith("adopt:url:"):
        url = rest[len("adopt:url:"):]
        if (len(url) > SERVICE_FLAG_URL_MAX
                or any(c.isspace() or unicodedata.category(c) == "Cc" for c in url)
                or parse_url(url) is None):
            raise ValueError(
                f"--service {flag!r}: adopt:url: needs an http(s)://host[:port] URL of at most "
                f"{SERVICE_FLAG_URL_MAX} characters, with no whitespace")
        choice = Choice(service, "adopt_url", url)
    elif rest == "vco" or (rest.startswith("vco:") and _port_text_ok(rest[4:])):
        choice = Choice(service, "vco", rest[4:] or None)
    else:
        raise ValueError(
            f"--service {flag!r}: choice must be adopt:container:<name>, adopt:url:<url> "
            "or vco[:<port>]")
    if service == "code_embed" and choice.kind != "vco":
        raise ValueError("--service code_embed: code-embed is always VCO's own (use vco[:<port>])")
    return choice


@dataclass
class ServiceInputs:
    service: str
    candidates: list[_det.Candidate] = field(default_factory=list)
    existing: Optional[_se.EndpointRow] = None
    statements: list[Statement] = field(default_factory=list)
    continuity: Optional[Statement] = None
    choice: Optional[Choice] = None
    interactive: bool = False
    on_conflict: Optional[str] = None
    has_gpu: Optional[bool] = None
    #: Host ports already spoken for (every candidate's, the other rows').
    taken_ports: frozenset[int] = frozenset()
    port_free: PortFreeFn = default_port_free
    prompt: Optional[PromptFn] = None
    containers: Sequence[_det.ContainerInfo] = ()
    now_ms: int = 0


@dataclass
class Outcome:
    service: str
    row: Optional[_se.EndpointRow] = None
    how: str = ""
    lines: list[str] = field(default_factory=list)
    abort: Optional[str] = None
    chosen: Optional[_det.Candidate] = None
    migrate_code_embed: bool = False
    pending: Optional[_det.Candidate] = None
    ambiguous: list[_det.Candidate] = field(default_factory=list)
    unreachable: Optional[str] = None
    adopted_without_prompt: Optional[_det.Candidate] = None
    consumed: list[Statement] = field(default_factory=list)
    unimported: list[Statement] = field(default_factory=list)


def _free_port(start: int, inp: ServiceInputs, *, avoid: Iterable[int] = ()) -> Optional[int]:
    skip = set(inp.taken_ports) | set(avoid)
    for port in range(start, 65001):
        if port not in skip and inp.port_free(port):
            return port
    return None


def _mount_of(service: str, container: Optional[_det.ContainerInfo]) -> Optional[dict[str, str]]:
    if container is None:
        return None
    m = container.mount_at(DATA_MOUNT_TARGETS[service])
    return m.to_row() if m else None


def _enabled(inp: ServiceInputs) -> bool:
    if inp.service != "code_embed":
        return True
    if inp.has_gpu is None:
        return inp.existing.enabled if inp.existing is not None else True
    return bool(inp.has_gpu)


def _row_for(inp: ServiceInputs, c: _det.Candidate, *, source: str, confirmed: bool) -> _se.EndpointRow:
    service = inp.service
    grpc = c.grpc_port if service == "weaviate" else None
    if service == "weaviate" and grpc is None:
        grpc = _se.DEFAULT_WEAVIATE_GRPC_PORT
    verified = inp.now_ms if c.live else None
    container = c.container
    common: dict[str, Any] = dict(
        port=c.port, source=source, grpc_port=grpc, confirmed_by_user=confirmed,
        verified_at=verified, data_mount=_mount_of(service, container),
        compose_project=(container.compose_project or None) if container else None,
    )
    if service == "code_embed" or (container is not None and c.ownership == "installer"):
        return _se.EndpointRow(
            service=service, mode="vco_managed", host="localhost",
            container_name=_containers.canonical_name(service), enabled=_enabled(inp), **common)
    if container is not None:
        return _se.EndpointRow(service=service, mode="adopted_container", host="localhost",
                               container_name=container.name, **common)
    return _se.EndpointRow(service=service, mode="adopted_external", scheme=c.scheme,
                           host=c.host, **common)


def _managed_row(inp: ServiceInputs, port: int, *, source: str, grpc: Optional[int] = None,
                 enabled: Optional[bool] = None, confirmed: bool = False,
                 mount: Optional[Mapping[str, str]] = None) -> _se.EndpointRow:
    if inp.service == "weaviate" and grpc is None:
        grpc = _se.DEFAULT_WEAVIATE_GRPC_PORT
    return _se.EndpointRow(
        service=inp.service, mode="vco_managed", host="localhost", port=port, source=source,
        grpc_port=grpc if inp.service == "weaviate" else None,
        container_name=_containers.canonical_name(inp.service),
        enabled=_enabled(inp) if enabled is None else enabled,
        confirmed_by_user=confirmed, data_mount=dict(mount) if mount else None,
    )


def _free_managed_row(inp: ServiceInputs, *, source: str, enabled: Optional[bool] = None,
                      confirmed: bool = False, avoid: Iterable[int] = ()) -> Optional[_se.EndpointRow]:
    """VCO's own copy on the canonical port if free, else the next free one
    (Weaviate's gRPC port moves with it)."""
    default = _se.DEFAULT_PORTS[inp.service]
    port = _free_port(default, inp, avoid=avoid)
    if port is None:
        return None
    grpc = None
    if inp.service == "weaviate":
        grpc = _free_port(_se.DEFAULT_WEAVIATE_GRPC_PORT + (port - default), inp, avoid=[port])
        if grpc is None:
            return None
    return _managed_row(inp, port, source=source, grpc=grpc, enabled=enabled, confirmed=confirmed)


def _source_for(inp: ServiceInputs, c: _det.Candidate, fallback: str = "install_probe") -> str:
    if c.ownership == "legacy_vco" and c.container is not None:
        return "migrated:legacy_compose"
    ep = (_norm_host(c.host), c.port)
    for stmt in inp.statements:
        if stmt.endpoint == ep:
            return f"migrated:{stmt.kind}"
    return fallback


def _account_statements(inp: ServiceInputs, out: Outcome) -> None:
    """Which statements this decision consumed (live, or became the row) and
    which lost without being verifiable."""
    live = {(_norm_host(c.host), c.port) for c in inp.candidates if c.live}
    chosen = (_norm_host(out.row.host), out.row.port) if out.row is not None else None
    for stmt in inp.statements:
        if stmt.endpoint in live or stmt.endpoint == chosen:
            out.consumed.append(stmt)
        else:
            out.unimported.append(stmt)


def _pick(cands: Sequence[_det.Candidate], inp: ServiceInputs) -> _det.Candidate:
    if inp.continuity is not None:
        for c in cands:
            if (_norm_host(c.host), c.port) == inp.continuity.endpoint:
                return c
    for c in cands:
        if _norm_host(c.host) == "localhost" and c.port == _se.DEFAULT_PORTS[inp.service]:
            return c
    for c in cands:
        if c.ownership == "installer":
            return c
    return cands[0]


def _pick_third_party(cands: Sequence[_det.Candidate], inp: ServiceInputs) -> _det.Candidate:
    order = [_se.DEFAULT_PORTS[inp.service], *_det.PROBE_PORTS[inp.service]]
    for port in order:
        for c in cands:
            if c.port == port and _norm_host(c.host) == "localhost":
                return c
    return cands[0]


def decide(inp: ServiceInputs) -> Outcome:
    """The row for one service. Pure (every probe already happened; the
    prompt and ``port_free`` are injected)."""
    if inp.choice is not None:
        out = _decide_choice(inp)
    elif inp.existing is not None:
        out = _decide_existing(inp)
    elif inp.service == "code_embed":
        out = _decide_code_embed(inp)
    else:
        out = _decide_fresh(inp)
    if inp.existing is None and out.row is not None:
        _account_statements(inp, out)
    return out


def _decide_fresh(inp: ServiceInputs) -> Outcome:
    out = Outcome(inp.service)
    live = [c for c in inp.candidates if c.live]
    data = [c for c in live if c.compatible and c.has_vco_data]
    if data:
        pick = _pick(data, inp)
        out.row = _row_for(inp, pick, source=_source_for(inp, pick),
                           confirmed=pick.ownership == "legacy_vco")
        out.chosen, out.how = pick, "vco_data"
        if len(data) > 1:
            out.ambiguous = list(data)
        out.lines.append(f"  [{inp.service}] {_se.describe(inp.service, out.row)} — holds VCO data")
        return out
    third = [c for c in live if c.compatible]
    if third:
        return _decide_third_party(inp, out, _pick_third_party(third, inp))
    return _decide_nothing_live(inp, out)


def _decide_third_party(inp: ServiceInputs, out: Outcome, tp: _det.Candidate) -> Outcome:
    svc = inp.service
    answer = inp.on_conflict
    via_flag = answer is not None
    if answer is None and inp.interactive and inp.prompt is not None:
        answer = inp.prompt(svc, tp)
    if answer == "abort":
        out.abort = f"{svc}: stopped at the adoption choice for {tp.url}"
        return out
    if answer == "adopt":
        out.row = _row_for(inp, tp, source="user_cli", confirmed=True)
        out.chosen, out.how = tp, "third_party_adopted"
        out.lines.append(f"  [{svc}] using your {svc} at {tp.url} (confirmed"
                         f"{' by --on-conflict adopt' if via_flag else ''})")
        return out
    if answer == "alt-port":
        row = _free_managed_row(inp, source="user_cli", confirmed=True, avoid=[tp.port])
        if row is None:
            out.abort = f"{svc}: no free port for VCO's own copy"
            return out
        out.row, out.how = row, "vco_copy"
        out.lines.append(f"  [{svc}] your {svc} at {tp.url} left alone; VCO runs its own on port {row.port}")
        return out
    if svc == "ollama":
        out.row = _row_for(inp, tp, source="install_probe", confirmed=False)
        out.chosen, out.how, out.adopted_without_prompt = tp, "third_party_adopted", tp
        out.lines.append(f"  [ollama] using the Ollama already at {tp.url} (no second copy started)")
        return out
    # Plan §4b decision policy, rule 3, as amended by owner ruling Q1
    # ("Adopt Ollama, ask for Weaviate"): a third-party Weaviate with no VCO
    # data is NOT adopted unattended. With no answer VCO starts nothing that
    # would duplicate it and records an action_required deferral naming both
    # choices — never a silent duplicate, never a silent adoption.
    # THE WAITING STATE (accepted by the orchestrator, 2026-09-24): the row is
    # `vco_managed` with `enabled=0`, parked on a FREE port (never the other
    # instance's). `enabled=0` keeps it out of every compose service list and
    # the watchdog; clients resolve to a port nothing answers on, so they fail
    # loudly instead of writing into an unconfirmed instance; and
    # `service_adoption_confirmation_required` names `adopt` / `use-vco-copy`.
    # A later run that can answer (a TTY, --on-conflict) settles it
    # (_decide_existing).
    row = _free_managed_row(inp, source="install_probe", enabled=False, avoid=[tp.port])
    if row is None:
        out.abort = f"{svc}: no free port to park VCO's own copy while it waits for your choice"
        return out
    out.row, out.how, out.pending = row, "awaiting_confirmation", tp
    out.lines.append(
        f"  [weaviate] found a Weaviate at {tp.url} without VCO data — waiting for your "
        "choice (see UPDATE_DEFERRED.md); nothing started, nothing written into it")
    return out


def _stopped_container_at(inp: ServiceInputs, port: int) -> Optional[_det.Candidate]:
    for c in inp.candidates:
        if c.container is not None and not c.container.running and c.port == port:
            return c
    return None


def _port_blocked(inp: ServiceInputs, port: int) -> bool:
    """Something answers on localhost:*port* that VCO cannot use as this service."""
    return any(
        c.port == port and _norm_host(c.host) == "localhost" and c.probe is not None
        and c.probe.answered and not (c.live and c.compatible)
        for c in inp.candidates
    )


def _decide_nothing_live(inp: ServiceInputs, out: Outcome) -> Outcome:
    """Rank 5: nothing usable answers. Keep the previously effective endpoint
    (continuity, else the highest-precedence statement); a stopped container
    publishing it is kept by name. With no evidence at all: VCO's own copy on
    the canonical port (a stopped VCO container there is reused), else the
    next free port."""
    svc = inp.service
    eff = inp.continuity or (inp.statements[0] if inp.statements else None)
    if eff is not None and _port_blocked(inp, eff.port):
        eff = None  # something unusable answers there — do not keep pointing at it
    local = eff is None or _norm_host(eff.host) == "localhost"
    target = eff.port if eff is not None else _se.DEFAULT_PORTS[svc]
    stopped = _stopped_container_at(inp, target) if local and svc != "code_embed" else None
    if stopped is not None and eff is None and stopped.ownership == "third_party":
        stopped = None  # someone else's stopped container is not ours to pick
    if stopped is not None:
        out.row = _row_for(inp, stopped, source=_source_for(
            inp, stopped, f"migrated:{eff.kind}" if eff else "install_probe"),
            confirmed=stopped.ownership == "legacy_vco")
        out.how = "kept_stopped_container"
    elif eff is not None and (svc == "code_embed" or (local and (
            eff.legacy_mode == "parallel" or eff.port == _se.DEFAULT_PORTS[svc]))):
        port = eff.port if local else _se.DEFAULT_PORTS[svc]
        out.row = _managed_row(inp, port, source=f"migrated:{eff.kind}", grpc=eff.grpc_port)
        out.how = "kept_previous_managed"
    elif eff is not None:
        out.row = _se.EndpointRow(
            service=svc, mode="adopted_external", scheme=eff.scheme, host=eff.host,
            port=eff.port, source=f"migrated:{eff.kind}",
            grpc_port=(eff.grpc_port or _se.DEFAULT_WEAVIATE_GRPC_PORT) if svc == "weaviate" else None,
            autostart=eff.legacy_mode != "refuse")
        out.how = "kept_previous_external"
        out.unreachable = f"{eff.value} ({eff.source}) does not answer"
    else:
        row = _free_managed_row(inp, source="install_probe")
        if row is None:
            out.abort = f"{svc}: no free port for VCO's own copy"
            return out
        out.row, out.how = row, "fresh"
        note = "" if row.port == _se.DEFAULT_PORTS[svc] else " (the canonical port is taken)"
        out.lines.append(f"  [{svc}] VCO's own on port {row.port}{note}")
        return out
    out.lines.append(f"  [{svc}] nothing answers yet; keeping {_se.describe(svc, out.row)}")
    return out


def _decide_code_embed(inp: ServiceInputs) -> Outcome:
    out = Outcome("code_embed")
    with_container = [c for c in inp.candidates if c.container is not None and c.port]
    pick = (next((c for c in with_container if c.live), None)
            or next((c for c in with_container if c.container and c.container.running), None)
            or next(iter(with_container), None))
    if pick is not None:
        out.row = _row_for(inp, pick, source=_source_for(inp, pick), confirmed=False)
        out.chosen, out.how = pick, "vco_container"
        out.migrate_code_embed = bool(out.row.enabled and pick.ownership != "installer")
        out.lines.append(f"  [code_embed] {_se.describe('code_embed', out.row)}"
                         + (" — recreated under the installer with its cache" if out.migrate_code_embed else ""))
        return out
    return _decide_nothing_live(inp, out)


def _container_drift(inp: ServiceInputs, row: _se.EndpointRow) -> Optional[_se.EndpointRow]:
    """The same pinned container now publishing a different port (or mount)."""
    c = next((x for x in inp.containers if x.name == row.container_name), None)
    if c is None:
        return None
    port = c.host_ports.get(_det.CONTAINER_PORTS[inp.service])
    grpc = c.host_ports.get(_det.WEAVIATE_CONTAINER_GRPC_PORT) if inp.service == "weaviate" else None
    changes: dict[str, Any] = {}
    if port and port != row.port:
        changes["port"] = port
    if grpc and grpc != row.grpc_port:
        changes["grpc_port"] = grpc
    mount = _mount_of(inp.service, c)
    if mount and mount != (dict(row.data_mount) if row.data_mount else None):
        changes["data_mount"] = mount
    if (c.compose_project or None) != row.compose_project and c.compose_project:
        changes["compose_project"] = c.compose_project
    if not changes:
        return None
    return replace(row, source="live_reconcile", **changes)


def _decide_existing(inp: ServiceInputs) -> Outcome:
    svc = inp.service
    row = inp.existing
    assert row is not None
    out = Outcome(svc, row=row, how="existing")
    live_at_row = next((c for c in inp.candidates if c.live and c.port == row.port
                        and _norm_host(c.host) == _norm_host(row.host)), None)
    if row.mode == "adopted_container":
        present = any(x.name == row.container_name for x in inp.containers)
        if not present and inp.containers:
            out.unreachable = f"your container {row.container_name} no longer exists"
        drifted = _container_drift(inp, row) if present else None
        if drifted is not None:
            out.row, out.how = drifted, "live_reconcile"
            out.lines.append(f"  [{svc}] {row.container_name} moved → {_se.describe(svc, drifted)}")
    elif row.mode == "adopted_external":
        if live_at_row is None:
            out.unreachable = f"{_se.render_url(svc, row)} does not answer"
    elif svc == "weaviate" and not row.enabled:
        third = [c for c in inp.candidates if c.live and c.compatible and not c.has_vco_data]
        if third and (inp.on_conflict is not None or (inp.interactive and inp.prompt is not None)):
            # A later run that CAN answer (a TTY, or --on-conflict) settles it.
            return _decide_third_party(inp, Outcome(svc), _pick_third_party(third, inp))
        if third:
            out.pending, out.how = _pick_third_party(third, inp), "awaiting_confirmation"
        else:
            out.row, out.how = replace(row, enabled=True, source="live_reconcile"), "pending_cleared"
            out.lines.append("  [weaviate] the other Weaviate is gone — VCO runs its own")
    else:
        c = next((x for x in inp.candidates if x.container is not None and x.port == row.port
                  and x.container.running), None)
        if svc != "code_embed" and c is not None and c.ownership == "legacy_vco":
            out.row = _row_for(inp, c, source="migrated:legacy_compose", confirmed=True)
            out.how = "legacy_container_adopted"
        elif svc == "code_embed":
            cur = row
            if inp.has_gpu is not None and cur.enabled != bool(inp.has_gpu):
                cur = replace(cur, enabled=bool(inp.has_gpu))
            if c is not None:
                # The row KEEPS the cache identity after the container is gone
                # (plan §4c.1): refreshed only from a live container.
                mount = _mount_of(svc, c.container)
                if mount and mount != (dict(cur.data_mount) if cur.data_mount else None):
                    cur = replace(cur, data_mount=mount, source="live_reconcile")
                out.migrate_code_embed = bool(cur.enabled and c.ownership != "installer")
            out.row = cur
    final = out.row if out.row is not None else row
    if final.verified_at is None and live_at_row is not None and final.port == row.port:
        final = replace(final, verified_at=inp.now_ms)
    out.row = final
    return out


def _find_choice_candidate(inp: ServiceInputs) -> Optional[_det.Candidate]:
    ch = inp.choice
    assert ch is not None
    if ch.kind == "adopt_container":
        return next((c for c in inp.candidates
                     if c.container is not None and c.container.name == ch.value), None)
    parsed = parse_url(ch.value or "")
    if parsed is None:
        return None
    _scheme, host, port = parsed
    return next((c for c in inp.candidates
                 if c.port == port and _norm_host(c.host) == _norm_host(host)), None)


def _decide_choice(inp: ServiceInputs) -> Outcome:
    svc = inp.service
    ch = inp.choice
    assert ch is not None
    out = Outcome(svc, how="explicit")
    if ch.kind == "vco":
        if ch.value:
            port = int(ch.value)
            if port in inp.taken_ports or not inp.port_free(port):
                if not (inp.existing and inp.existing.mode == "vco_managed" and inp.existing.port == port):
                    out.abort = f"{svc}: port {port} is taken"
                    return out
            grpc = None
            if svc == "weaviate":
                grpc = _free_port(_se.DEFAULT_WEAVIATE_GRPC_PORT + (port - _se.DEFAULT_PORTS[svc]),
                                  inp, avoid=[port])
            out.row = _managed_row(inp, port, source="user_cli", grpc=grpc, confirmed=True)
        else:
            row = _free_managed_row(inp, source="user_cli", confirmed=True)
            if row is None:
                out.abort = f"{svc}: no free port for VCO's own copy"
                return out
            out.row = row
        out.lines.append(f"  [{svc}] {_se.describe(svc, out.row)} (your choice)")
        return out
    cand = _find_choice_candidate(inp)
    if cand is None or not cand.live:
        out.abort = f"{svc}: {ch.value} is not running / not answering as {svc}"
        return out
    if not cand.compatible:
        out.abort = f"{svc}: {ch.value} cannot be used — {cand.reason}"
        return out
    out.row = _row_for(inp, cand, source="user_cli", confirmed=True)
    out.chosen = cand
    out.lines.append(f"  [{svc}] {_se.describe(svc, out.row)} (your choice)")
    return out


# ─── drift (adopted containers lacking VCO's behaviour-critical env) ────


def compose_reclaim_env(compose_text: str) -> dict[str, str]:
    """The Weaviate reclaim env values a compose file sets (a flat regex scan
    — the keys are unique across services; no YAML dependency on the install
    path). The ONE home: install.py's reclaim-drift gate calls this too."""
    found: dict[str, str] = {}
    for key in _WEAVIATE_DRIFT_KEYS:
        m = re.search(rf'^\s*{re.escape(key)}\s*:\s*["\']?([^"\'\n#]+?)["\']?\s*(?:#.*)?$',
                      compose_text, re.MULTILINE)
        if m:
            found[key] = m.group(1).strip()
    return found


def _compose_env_values(orchestrator_root: Path) -> dict[str, str]:
    try:
        text = (Path(orchestrator_root) / "infrastructure" / "docker-compose.yml").read_text(encoding="utf-8")
    except OSError:
        return {}
    return compose_reclaim_env(text)


def config_drift(service: str, container: _det.ContainerInfo,
                 orchestrator_root: Path) -> dict[str, tuple[Optional[str], str]]:
    """``key → (live, wanted)`` for every behaviour-critical key *container*
    (an adopted one) does not carry at VCO's value."""
    wanted = _compose_env_values(orchestrator_root) if service == "weaviate" else (
        dict(_OLLAMA_CANONICAL_ENV) if service == "ollama" else {})
    return {k: (container.env.get(k), v) for k, v in sorted(wanted.items())
            if container.env.get(k) != v}


# ─── registry bootstrap ─────────────────────────────────────────────────


def registry_available(db_path: Optional[Path] = None) -> bool:
    try:
        conn = _se._open_rw(_se._resolve_db_path(db_path))
    except _se.ServiceRegistryUnavailable:
        return False
    conn.close()
    return True


def ensure_registry(orchestrator_root: Path, *, run: Optional[RunFn] = None,
                    db_path: Optional[Path] = None) -> tuple[bool, str]:
    """``vct-hub --ensure-db`` (the one schema owner creates + migrates
    launcher.db), then confirm the table is there. ``(ok, why-not)``."""
    from vco_lib.hub_ensure import find_hub_binary  # noqa: PLC0415

    binary = find_hub_binary(repo_root=Path(orchestrator_root))
    if binary is None:
        return False, "no vct-hub binary found (launcher/dist/<arch>/, $VCT_HUB_BIN, ~/.vct/bin)"
    try:
        proc = (run or subprocess.run)([str(binary), "--ensure-db"], capture_output=True,
                                       text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`{binary} --ensure-db` could not run: {exc}"
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
        return False, f"`{binary} --ensure-db` exited {proc.returncode} (a hub binary older than v0.2.97?): {tail}"
    if not registry_available(db_path):
        return False, (f"`{binary} --ensure-db` succeeded but {_se._resolve_db_path(db_path)} has no "
                       "service_endpoints table (the binary predates migration 047)")
    return True, ""


# ─── the reconcile ──────────────────────────────────────────────────────


@dataclass
class ReconcileResult:
    rows: dict[str, _se.EndpointRow] = field(default_factory=dict)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    entries: list[DeferralEntry] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    registry_ok: bool = True
    #: rows could not be written: they are this run's in-memory pins.
    pinned: bool = False
    abort: Optional[str] = None
    imported: bool = False
    written: list[str] = field(default_factory=list)
    apply_report: Optional[_se.ApplyChangeReport] = None
    detection: Optional[_det.Detection] = None

    @property
    def weaviate_pending(self) -> bool:
        o = self.outcomes.get("weaviate")
        return bool(o is not None and o.pending is not None)

    @property
    def migrate_code_embed(self) -> bool:
        o = self.outcomes.get("code_embed")
        return bool(o is not None and o.migrate_code_embed)

    def disposition(self, service: str) -> str:
        """What step 5's compose run does with *service*: ``recreate`` (VCO's
        own container, running under the installer's project — re-applied
        from the current compose config, the v0.2.61 behaviour), ``start``
        (VCO's own, nothing answering), or ``skip`` (adopted, disabled,
        awaiting a choice, being migrated, or answered by something VCO
        cannot see as its own container)."""
        row = self.rows.get(service)
        o = self.outcomes.get(service)
        if row is None or row.mode != "vco_managed" or not row.enabled or o is None:
            return "skip"
        if o.migrate_code_embed:
            return "skip"
        cands = self.detection.for_service(service) if self.detection is not None else []
        at_port = [c for c in cands if c.port == row.port and _norm_host(c.host) == "localhost"]
        if any(c.live and c.container is not None and c.ownership == "installer" for c in at_port):
            return "recreate"
        if any(c.probe is not None and c.probe.answered for c in at_port):
            return "skip"
        return "start"


def _interactive_prompt(service: str, cand: _det.Candidate) -> str:
    """The §4b offer, on a TTY. Default: use it."""
    print()
    print(f"  Found a {service} at {cand.url} that VCO did not start"
          + (f" (container {cand.container.name})" if cand.container else "") + ".")
    if service == "weaviate":
        print("  Using it means VCO adds its own (project-prefixed) collections to it.")
    print(f"    [1] Use it (recommended)\n    [2] Run VCO's own {service} on a free port\n    [3] Abort")
    try:
        ans = input(f"  Choice for {service} [1/2/3, default 1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "adopt"
    return {"2": "alt-port", "3": "abort"}.get(ans, "adopt")


def reconcile(
    *,
    phase: str,
    orchestrator_root: Path,
    runtime: Optional[str],
    db_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    has_gpu: Optional[bool] = None,
    interactive: bool = False,
    on_conflict: Optional[str] = None,
    choices: Sequence[Choice] = (),
    ensure_db: Optional[Callable[[], tuple[bool, str]]] = None,
    run: Optional[RunFn] = None,
    fetch: Optional[_det.FetchFn] = None,
    tcp_open: Optional[_det.TcpFn] = None,
    port_free: Optional[PortFreeFn] = None,
    prompt: Optional[PromptFn] = None,
    now_ms: Optional[int] = None,
    services_toml_path: Optional[Path] = None,
    vct_config_dirs: Optional[Sequence[Path]] = None,
    installer_project: Optional[str] = None,
    apply_kwargs: Optional[Mapping[str, Any]] = None,
) -> ReconcileResult:
    """Decide, write and follow up the three rows. ``phase``: ``install`` /
    ``update`` (legacy import when rows are missing; prompts allowed when
    ``interactive``) or ``session`` (existing rows only: live drift, no
    import, no prompt)."""
    root = Path(orchestrator_root)
    env = os.environ if env is None else env
    now = _now_ms() if now_ms is None else now_ms
    result = ReconcileResult()
    by_service = {c.service: c for c in choices}

    if ensure_db is not None:
        ok, why = ensure_db()
        if not ok:
            result.entries.append(_registry_unavailable_entry(why))
            result.lines.append(f"  ! service registry: {why}")
            # A table an earlier (current) hub created is still writable.
            result.registry_ok = registry_available(db_path)
    existing = _se.load_rows(db_path) if result.registry_ok else {}
    importing = phase != "session" and any(s not in existing for s in SERVICES)
    legacy = LegacyEvidence()
    if phase != "session":
        legacy = gather_legacy(orchestrator_root=root, db_path=db_path, env=env,
                               services_toml_path=services_toml_path,
                               vct_config_dirs=vct_config_dirs)
    if phase == "session" and not existing:
        result.lines.append("  no service_endpoints rows yet — `python install.py --update` writes them")
        return result

    if installer_project is None:
        infra = root / "infrastructure"
        try:
            text = (infra / "docker-compose.yml").read_text(encoding="utf-8")
        except OSError:
            text = ""
        installer_project = _containers.compose_project_name(infra, text)
    extra: list[_det.Endpoint] = []
    for stmt in [*legacy.statements, *legacy.continuity.values()]:
        extra.append(_det.Endpoint(stmt.service, stmt.host, stmt.port, stmt.scheme,
                                   stmt.grpc_port, origin=stmt.source))
    for row in existing.values():
        extra.append(_det.Endpoint(row.service, row.host, row.port, row.scheme, row.grpc_port,
                                   origin="row"))
    detection = _det.detect(runtime=runtime, installer_project=installer_project,
                            extra_endpoints=extra, run=run, fetch=fetch, tcp_open=tcp_open)
    result.detection = detection

    taken = {c.port for s in SERVICES for c in detection.for_service(s) if c.port}
    taken |= {port for c in detection.containers for port in c.host_ports.values()}
    for service in SERVICES:
        others = {r.port for s, r in result.rows.items() if s != service}
        others |= {r.grpc_port for r in result.rows.values() if r.grpc_port}
        inp = ServiceInputs(
            service=service, candidates=detection.for_service(service),
            existing=existing.get(service),
            statements=legacy.for_service(service) if service not in existing else [],
            continuity=legacy.continuity.get(service) if service not in existing else None,
            choice=by_service.get(service), interactive=interactive and phase != "session",
            on_conflict=on_conflict if phase != "session" else None, has_gpu=has_gpu,
            taken_ports=frozenset((taken | others) - ({existing[service].port} if service in existing else set())),
            port_free=port_free or default_port_free,
            prompt=prompt or (_interactive_prompt if interactive else None),
            containers=detection.containers, now_ms=now,
        )
        out = decide(inp)
        result.outcomes[service] = out
        result.lines.extend(out.lines)
        if out.abort:
            result.abort = out.abort
            return result
        if out.row is not None:
            result.rows[service] = out.row

    result.imported = importing and (legacy_has_anything(legacy) or bool(legacy.continuity))
    if result.registry_ok:
        try:
            # Session phase: rows only — the hook that runs it is killed after
            # 8 s and the chain (reprojection + MCP registration) can take a
            # minute. The propagated digest stays behind, and the next update
            # or launcher-start reconcile runs the chain (commit_rows' heal).
            wr, report = _se.commit_rows(
                [result.rows[s] for s in SERVICES if s in result.rows],
                orchestrator_root=root, db_path=db_path, now_ms=now,
                out=lambda line: result.lines.append(f"  {line}"),
                propagate=phase != "session",
                **dict(apply_kwargs or {}),
            )
            result.written, result.apply_report = wr.written, report
        except (_se.ServiceRegistryUnavailable, _se.InvalidEndpointRow) as exc:
            result.registry_ok = False
            if not any(e.condition_id == CID_REGISTRY_UNAVAILABLE for e in result.entries):
                result.entries.append(_registry_unavailable_entry(str(exc)))
            result.lines.append(f"  ! could not record the endpoints: {exc}")
    result.pinned = not result.registry_ok

    if phase != "session":
        if result.registry_ok and importing and (legacy_has_anything(legacy) or legacy.continuity):
            _retire_imported_sources(legacy, result, db_path=db_path)
        unimported = _still_unimported(legacy, result.rows, result.outcomes, existing,
                                        result.detection)
        if unimported:
            result.entries.append(_legacy_unimported_entry(unimported, result.rows))
    _autostart_adopted(result, runtime, run)
    result.entries.extend(_outcome_entries(result, root))
    return result


def legacy_has_anything(ev: LegacyEvidence) -> bool:
    return bool(ev.statements or ev.services_toml or ev.override_files or ev.port_override_keys)


#: Sources that stay in place after the import and are the USER's own
#: statements — re-checked on every later run (the projected transport in the
#: installer's env is not one: it is merely stale output).
_PERSISTENT_KINDS: frozenset[str] = frozenset({"app_state", "vct-config.toml"})
_PERSISTENT_ENV: frozenset[str] = frozenset({"env:VCT_WEAVIATE_URL", "env:VCT_OLLAMA_URL"})


def _still_unimported(legacy: LegacyEvidence, rows: Mapping[str, _se.EndpointRow],
                      outcomes: Mapping[str, Outcome],
                      existing: Mapping[str, _se.EndpointRow],
                      detection: Optional[_det.Detection]) -> list[Statement]:
    """Statements that lost and could not be verified. On the import run this
    is the decision's own accounting; on every later run the PERSISTENT user
    statements (a kept port_override key, a vct-config.toml, an exported
    VCT_*_URL) are re-checked against the row, so the entry clears once the
    user settles it."""
    out: list[Statement] = []
    for service in SERVICES:
        o = outcomes.get(service)
        if service not in existing and o is not None:
            out.extend(o.unimported)
            continue
        row = rows.get(service)
        if row is None:
            continue
        live = {(_norm_host(c.host), c.port)
                for c in (detection.for_service(service) if detection else []) if c.live}
        for stmt in legacy.for_service(service):
            if stmt.kind not in _PERSISTENT_KINDS and stmt.source not in _PERSISTENT_ENV:
                continue
            if stmt.endpoint != (_norm_host(row.host), row.port) and stmt.endpoint not in live:
                out.append(stmt)
    return out


def _retire_imported_sources(legacy: LegacyEvidence, result: ReconcileResult, *,
                             db_path: Optional[Path]) -> None:
    """After a successful import: rename (never delete) services.toml and the
    alt-port override; delete the port_override keys that were consumed;
    NULL the legacy-default binding URLs. Each step soft-fails into a line."""
    if not all(s in result.rows for s in SERVICES):
        return
    consumed_sources = {st.source for o in result.outcomes.values() for st in o.consumed}
    retired: list[str] = []
    for path in [p for p in [legacy.services_toml] if p] + list(legacy.override_files):
        suffix = MIGRATED_SUFFIX if path == legacy.services_toml else RETIRED_SUFFIX
        target = path.with_name(path.name + suffix)
        try:
            path.rename(target)  # a rename, never a rewrite: the bytes stay as the user left them
            retired.append(f"{path} → {target.name}")
        except OSError as exc:
            result.lines.append(f"  ! could not rename {path}: {exc}")
    keys = [k for k in legacy.port_override_keys.values() if f"app_state:{k}" in consumed_sources]
    nulled = 0
    try:
        conn = _se._open_rw(_se._resolve_db_path(db_path))
    except _se.ServiceRegistryUnavailable:
        conn = None
    if conn is not None:
        try:
            with conn:
                for key in keys:
                    conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
                # Binding-table heal has ONE Python writer (kg_binding_heal).
                nulled = _kg_binding_heal.null_legacy_default_binding_urls(conn)
        except sqlite3.Error as exc:
            result.lines.append(f"  ! could not clean legacy launcher.db values: {exc}")
        finally:
            conn.close()
    sources = sorted({st.source for o in result.outcomes.values() for st in o.consumed})
    result.entries.append(_migrated_entry(result.rows, sources, retired, keys, nulled))


def _autostart_adopted(result: ReconcileResult, runtime: Optional[str], run: Optional[RunFn]) -> None:
    """Start (by NAME — never recreate) an adopted container that is stopped."""
    if not runtime or result.detection is None:
        return
    for service, row in result.rows.items():
        if row.mode != "adopted_container" or not row.autostart:
            continue
        c = result.detection.container_named(row.container_name or "")
        if c is None or c.running:
            continue
        try:
            proc = (run or subprocess.run)([runtime, "start", c.name], capture_output=True,
                                           text=True, timeout=60)
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        result.lines.append(f"  [{service}] {'started' if ok else 'could not start'} your container {c.name}")


# ─── verification (after VCO's lifecycle ran) ───────────────────────────


def verify(
    rows: Optional[Mapping[str, _se.EndpointRow]],
    *,
    db_path: Optional[Path] = None,
    write: bool = True,
    fetch: Optional[_det.FetchFn] = None,
    wait_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    now_ms: Optional[int] = None,
) -> list[DeferralEntry]:
    """Probe every enabled row once VCO has started what it starts. Stamp
    ``verified_at`` on a row that answers for the first time; report the
    rest (``service_endpoint_unreachable``). Bounded wait per service."""
    if rows is None:  # re-read: step 5 (e.g. the code-embed move) may have changed a row
        rows = _se.load_rows(db_path)
    fetch = fetch or _det.default_fetch
    now = _now_ms() if now_ms is None else now_ms
    dead: list[tuple[str, str, str]] = []
    stamp: list[_se.EndpointRow] = []
    for service in SERVICES:
        row = rows.get(service)
        if row is None or not row.enabled:
            continue
        url = _se.render_url(service, row)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            probe = _det.probe_endpoint(service, url, fetch=fetch)
            if probe.answered and probe.is_service:
                break
            if time.monotonic() >= deadline:
                break
            sleep(2.0)
        if probe.answered and probe.is_service:
            if row.verified_at is None:
                stamp.append(replace(row, verified_at=now))
        else:
            dead.append((service, url, probe.detail or "does not answer"))
    if stamp and write:
        try:
            _se.write_rows(stamp, db_path=db_path, now_ms=now)
        except (_se.ServiceRegistryUnavailable, _se.InvalidEndpointRow):
            pass
    return [_unreachable_entry(dead, rows)] if dead else []


# ─── ledger entries ─────────────────────────────────────────────────────

_CLI = "python -m vco_lib.service_endpoints"


def _registry_unavailable_entry(why: str) -> DeferralEntry:
    return DeferralEntry(
        condition_id=CID_REGISTRY_UNAVAILABLE,
        title="Service endpoints could not be recorded in launcher.db",
        detected=(
            f"{why}. This run configured compose, `.claude/settings.json` and the MCP "
            "registration from the endpoints it detected, but no later process can read "
            "them from launcher.db."
        ),
        why_deferred=(
            "The `service_endpoints` table is created only by the hub's migration runner "
            "(`vct-hub --ensure-db`); install.py never creates schema itself."
        ),
        command_to_apply=(
            "# Make the launcher/hub binaries current (the launcher's update does this), then:\n"
            "python install.py --update\n"
            "# The next run re-derives every row from what is running; nothing is lost."
        ),
        severity="warning",
    )


def _unreachable_entry(dead: Sequence[tuple[str, str, str]],
                       rows: Mapping[str, _se.EndpointRow]) -> DeferralEntry:
    lines = "\n".join(f"  - {s}: {url} — {why} ({_se.describe(s, rows.get(s))})"
                      for s, url, why in dead)
    first = dead[0][0]
    return DeferralEntry(
        condition_id=CID_UNREACHABLE,
        title=f"{len(dead)} service endpoint(s) do not answer",
        detected=f"VCO recorded these endpoints, and nothing answers there:\n{lines}",
        why_deferred=(
            "VCO does not switch its clients to a different instance on its own — the "
            "one it recorded may hold your data. It keeps pointing where it pointed."
        ),
        command_to_apply=(
            f"{_CLI} candidates            # what is running, and which holds VCO data\n"
            f"{_CLI} adopt --service {first} --url <url>     # use a running instance\n"
            f"{_CLI} use-vco-copy --service {first}          # or run VCO's own copy\n"
            f"{_CLI} reconcile             # re-check after starting it yourself"
        ),
        severity="warning",
        dismiss_fields={"service": ",".join(s for s, _u, _w in dead),
                        "endpoint": ",".join(u for _s, u, _w in dead)},
    )


def _ambiguous_entry(service: str, chosen: _det.Candidate,
                     cands: Sequence[_det.Candidate]) -> DeferralEntry:
    lines = "\n".join(
        f"  - {c.url}" + (f" (container {c.container.name})" if c.container else "")
        + f": {', '.join(c.probe.vco_markers[:4]) if c.probe else ''}"
        + ("   ← chosen" if c is chosen else "")
        for c in cands)
    return DeferralEntry(
        condition_id=CID_AMBIGUOUS,
        title=f"More than one {service} holds VCO data — VCO picked one",
        detected=f"{len(cands)} running {service} instances hold VCO data:\n{lines}",
        why_deferred="Only you know which one is current; VCO will not merge them.",
        command_to_apply=(
            f"# Keep the chosen one (silences this entry):\n"
            f"{_CLI} adopt --service {service} --url {chosen.url}\n"
            f"# Or pick another:\n{_CLI} adopt --service {service} --url <url>"
        ),
        severity="warning",
        dismiss_fields={"service": service,
                        "candidates": ",".join(sorted(c.url for c in cands))},
    )


def _legacy_unimported_entry(stmts: Sequence[Statement],
                             rows: Mapping[str, _se.EndpointRow]) -> DeferralEntry:
    lines = "\n".join(
        f"  - {s.source}: {s.value} (not answering) — using {_se.describe(s.service, rows.get(s.service))}"
        for s in stmts)
    return DeferralEntry(
        condition_id=CID_LEGACY_UNIMPORTED,
        title=f"{len(stmts)} older endpoint setting(s) were not applied",
        detected=f"These settings name endpoints that do not answer, so VCO kept other evidence:\n{lines}",
        why_deferred=(
            "Nothing reads these settings any more; the launcher DB is the one record. "
            "They are kept in place so you can see them."
        ),
        command_to_apply=(
            f"# If one of them is right, start that service and point VCO at it:\n"
            f"{_CLI} adopt --service {stmts[0].service} --url <url>\n"
            f"{_CLI} show                  # what VCO uses now"
        ),
        severity="warning",
        dismiss_fields={"source": ",".join(sorted({s.source for s in stmts})),
                        "value": ",".join(sorted({s.value for s in stmts}))},
    )


def _outcome_entries(result: ReconcileResult, root: Path) -> list[DeferralEntry]:
    entries: list[DeferralEntry] = []
    dead = [(s, _se.render_url(s, result.rows.get(s)), o.unreachable)
            for s, o in result.outcomes.items() if o.unreachable]
    if dead:
        entries.append(_unreachable_entry([(s, u, w or "") for s, u, w in dead], result.rows))
    for s, o in result.outcomes.items():
        if o.ambiguous and o.chosen is not None:
            entries.append(_ambiguous_entry(s, o.chosen, o.ambiguous))
    tp = next((o for o in result.outcomes.values() if o.adopted_without_prompt), None)
    if tp is not None and tp.adopted_without_prompt is not None:
        c = tp.adopted_without_prompt
        entries.append(DeferralEntry(
            condition_id=CID_ADOPTED_WITHOUT_PROMPT,
            title=f"VCO is using the {tp.service} that was already running",
            detected=(f"An unattended install found {tp.service} at {c.url}"
                      + (f" (container {c.container.name})" if c.container else "")
                      + " and uses it instead of starting a second copy."),
            why_deferred="Recorded for your information; nothing is pending.",
            command_to_apply=f"# To run VCO's own copy instead:\n{_CLI} use-vco-copy --service {tp.service}",
            severity="info",
        ))
    pend = result.outcomes.get("weaviate")
    if pend is not None and pend.pending is not None:
        c = pend.pending
        adopt = (f"{_CLI} adopt --service weaviate --container {c.container.name}"
                 if c.container else f"{_CLI} adopt --service weaviate --url {c.url}")
        entries.append(DeferralEntry(
            condition_id=CID_CONFIRMATION_REQUIRED,
            title="A Weaviate is already running — choose whether VCO should use it",
            detected=(f"Weaviate {c.probe.version if c.probe else ''} at {c.url}"
                      + (f" (container {c.container.name})" if c.container else "")
                      + " holds no VCO data. VCO started nothing and wrote nothing into it; "
                      "its clients point at a parked, disabled endpoint until you choose."),
            why_deferred=("Using it means VCO adds its own collections to an instance you run; "
                          "running a second one duplicates it. Either is your call."),
            command_to_apply=(
                f"# Use this Weaviate (VCO adds its project-prefixed collections):\n{adopt}\n"
                f"# Or let VCO run its own Weaviate on another port:\n"
                f"{_CLI} use-vco-copy --service weaviate\n"
                "# Then: python install.py --update"
            ),
            severity="warning",
            dismiss_fields={"service": "weaviate", "candidate": c.url},
        ))
    drift_items: list[tuple[str, str, dict]] = []
    det = result.detection
    for s, row in result.rows.items():
        if row.mode != "adopted_container" or det is None:
            continue
        c = det.container_named(row.container_name or "")
        if c is None:
            continue
        drift = config_drift(s, c, root)
        if drift:
            drift_items.append((s, c.name, drift))
    if drift_items:
        detail = "\n".join(
            f"  - {s} ({name}): " + ", ".join(f"{k}={live!r} (VCO sets {want!r})"
                                              for k, (live, want) in d.items())
            for s, name, d in drift_items)
        entries.append(DeferralEntry(
            condition_id=CID_CONFIG_DRIFT,
            title="A container VCO uses lacks some of VCO's tuning",
            detected=f"These containers run without VCO's behaviour-critical settings:\n{detail}",
            why_deferred=("VCO never recreates a container it adopted. The service works; it "
                          "just does not carry these fixes."),
            command_to_apply=(
                "# Let VCO manage the container (recreated under VCO's compose with the SAME\n"
                "# data mount, verified, rolled back on failure):\n"
                + "\n".join(f"{_CLI} hand-to-vco --service {s}" for s, _n, _d in drift_items)
                + "\n# Or keep it as it is and dismiss this entry."
            ),
            severity="info",
            dismiss_fields={"service": ",".join(s for s, _n, _d in drift_items),
                            "drift_keys": ",".join(sorted({k for _s, _n, d in drift_items for k in d}))},
        ))
    return entries


def _migrated_entry(rows: Mapping[str, _se.EndpointRow], sources: Sequence[str],
                    retired: Sequence[str], keys: Sequence[str], nulled: int) -> DeferralEntry:
    lines = [f"  - {_se.describe(s, rows.get(s))}" for s in SERVICES]
    extra = [f"  - read: {s}" for s in sources]
    extra += [f"  - renamed: {r}" for r in retired]
    extra += [f"  - removed launcher.db key: {k}" for k in keys]
    if nulled:
        extra.append(f"  - cleared {nulled} stale per-project Weaviate URL(s)")
    return DeferralEntry(
        condition_id=CID_MIGRATED,
        title="Service endpoints moved into launcher.db",
        detected="The endpoints are now recorded once, in launcher.db:\n" + "\n".join(lines + extra),
        why_deferred="Recorded for your information; nothing is pending.",
        command_to_apply=f"{_CLI} show",
        severity="info",
    )


# ─── clear probes (read-only; wrapped by vco_lib.deferral_probes) ───────


def _entry_services(entry: Any) -> list[str]:
    fields = getattr(entry, "dismiss_fields", None) or {}
    raw = str(fields.get("service", "") or "")
    return [s for s in raw.split(",") if s in SERVICES]


def probe_unreachable(entry: Any, *, fetch: Optional[_det.FetchFn] = None,
                      db_path: Optional[Path] = None) -> Optional[bool]:
    services = _entry_services(entry)
    rows = _se.load_rows(db_path)
    if not services or not rows:
        return None
    fetch = fetch or _det.default_fetch
    for s in services:
        row = rows.get(s)
        if row is None:
            return None
        probe = _det.probe_endpoint(s, _se.render_url(s, row), fetch=fetch)
        if not (probe.answered and probe.is_service):
            return True
    return False


def probe_ambiguous(entry: Any, *, runtime: Optional[str] = None, run: Optional[RunFn] = None,
                    fetch: Optional[_det.FetchFn] = None, tcp_open: Optional[_det.TcpFn] = None,
                    db_path: Optional[Path] = None) -> Optional[bool]:
    services = _entry_services(entry)
    rows = _se.load_rows(db_path)
    if not services or not rows:
        return None
    for s in services:
        row = rows.get(s)
        if row is None:
            return None
        if row.confirmed_by_user:
            continue
        det = _det.detect(runtime=runtime if runtime is not None else _runtime(),
                          run=run, fetch=fetch, tcp_open=tcp_open, services=(s,))
        if len([c for c in det.for_service(s) if c.has_vco_data]) > 1:
            return True
    return False


def probe_config_drift(entry: Any, *, orchestrator_root: Path, runtime: Optional[str] = None,
                       run: Optional[RunFn] = None, db_path: Optional[Path] = None) -> Optional[bool]:
    rows = _se.load_rows(db_path)
    if not rows:
        return None
    containers = _det.list_containers(runtime if runtime is not None else _runtime(), run=run)
    if not containers:
        return None
    for s, row in rows.items():
        if row.mode != "adopted_container":
            continue
        c = next((x for x in containers if x.name == row.container_name), None)
        if c is not None and config_drift(s, c, orchestrator_root):
            return True
    return False


def probe_confirmation_pending(entry: Any, *, db_path: Optional[Path] = None) -> Optional[bool]:
    row = _se.load_rows(db_path).get("weaviate")
    if row is None:
        return None  # no row is "could not look" for a clear probe, not a verdict
    return _se.awaits_choice("weaviate", row)


def _runtime() -> Optional[str]:
    try:
        res = _containers.resolve(probe_compose=False)
    except Exception:  # noqa: BLE001 - no runtime is a detection with no containers
        return None
    return res.runtime


# ─── user verbs (the CLI in vco_lib.service_endpoints delegates here) ───


def _current_holds_data(service: str, rows: Mapping[str, _se.EndpointRow],
                        fetch: Optional[_det.FetchFn]) -> bool:
    row = rows.get(service)
    if row is None or service != "weaviate":
        return False
    probe = _det.probe_endpoint(service, _se.render_url(service, row), fetch=fetch or _det.default_fetch)
    return bool(probe.vco_markers)


def adopt_endpoint(service: str, *, container: Optional[str] = None, url: Optional[str] = None,
                   orchestrator_root: Path, accept_empty_kg: bool = False,
                   db_path: Optional[Path] = None, runtime: Optional[str] = None,
                   run: Optional[RunFn] = None, fetch: Optional[_det.FetchFn] = None,
                   tcp_open: Optional[_det.TcpFn] = None, out: LogFn = print,
                   apply_kwargs: Optional[Mapping[str, Any]] = None) -> int:
    """``adopt --service S (--container NAME | --url URL)`` — point VCO at a
    running instance after the compatibility gate."""
    if service == "code_embed":
        out("code-embed is always VCO's own; nothing to adopt")
        return 2
    choice = Choice(service, "adopt_container" if container else "adopt_url", container or url)
    rows = _se.load_rows(db_path)
    extra = []
    if url and parse_url(url):
        scheme, host, port = parse_url(url)  # type: ignore[misc]
        extra.append(_det.Endpoint(service, host, port, scheme, origin="user"))
    det = _det.detect(runtime=runtime if runtime is not None else _runtime(), run=run, fetch=fetch,
                      tcp_open=tcp_open, extra_endpoints=extra, services=(service,))
    inp = ServiceInputs(service=service, candidates=det.for_service(service),
                        existing=rows.get(service), choice=choice, containers=det.containers,
                        now_ms=_now_ms())
    decision = decide(inp)
    if decision.abort or decision.row is None:
        out(decision.abort or "nothing to adopt")
        return 1
    chosen = decision.chosen
    if (not accept_empty_kg and chosen is not None and not chosen.has_vco_data
            and _current_holds_data(service, rows, fetch)):
        out("The Weaviate VCO uses now holds VCO data and this one holds none. Re-run with "
            "--accept-empty-kg to switch (the KG re-seeds from knowledge/, the code graph re-analyses).")
        return 1
    return _commit(decision.row, orchestrator_root, db_path, out, apply_kwargs)


def use_vco_copy(service: str, *, port: Optional[int] = None, orchestrator_root: Path,
                 accept_empty_kg: bool = False, db_path: Optional[Path] = None,
                 fetch: Optional[_det.FetchFn] = None, port_free: Optional[PortFreeFn] = None,
                 out: LogFn = print, apply_kwargs: Optional[Mapping[str, Any]] = None) -> int:
    """``use-vco-copy --service S [--port N]`` — VCO runs its own copy."""
    rows = _se.load_rows(db_path)
    if not accept_empty_kg and _current_holds_data(service, rows, fetch):
        out("The Weaviate VCO uses now holds VCO data. Re-run with --accept-empty-kg to switch to "
            "a new, empty VCO Weaviate (the KG re-seeds from knowledge/, the code graph re-analyses).")
        return 1
    taken = {r.port for s, r in rows.items() if s != service} | {
        r.grpc_port for r in rows.values() if r.grpc_port}
    inp = ServiceInputs(service=service, existing=rows.get(service),
                        choice=Choice(service, "vco", str(port) if port else None),
                        taken_ports=frozenset(taken), port_free=port_free or default_port_free,
                        now_ms=_now_ms())
    decision = decide(inp)
    if decision.abort or decision.row is None:
        out(decision.abort or "no row decided")
        return 1
    rc = _commit(decision.row, orchestrator_root, db_path, out, apply_kwargs)
    if rc == 0:
        out(f"VCO's own {service} starts at the next session start (or `python install.py --update`).")
    return rc


def _commit(row: _se.EndpointRow, root: Path, db_path: Optional[Path], out: LogFn,
            apply_kwargs: Optional[Mapping[str, Any]]) -> int:
    try:
        _se.commit_rows([row], orchestrator_root=root, db_path=db_path, out=out,
                        **dict(apply_kwargs or {}))
    except (_se.ServiceRegistryUnavailable, _se.InvalidEndpointRow) as exc:
        out(f"could not record the endpoint: {exc}")
        return 1
    return 0


MigrateFn = Callable[..., Any]


def move_endpoint(
    service: str, *, orchestrator_root: Path, port: Optional[int] = None,
    url: Optional[str] = None, grpc_port: Optional[int] = None,
    accept_empty_kg: bool = False, db_path: Optional[Path] = None,
    runtime: Optional[str] = None, run: Optional[RunFn] = None,
    fetch: Optional[_det.FetchFn] = None, tcp_open: Optional[_det.TcpFn] = None,
    port_free: Optional[PortFreeFn] = None, migrate: Optional[MigrateFn] = None,
    out: LogFn = print, apply_kwargs: Optional[Mapping[str, Any]] = None,
) -> int:
    """``move --service S …`` (plan §4g). Every change goes through
    :func:`vco_lib.service_endpoints.commit_rows`, so the infra ``.env``, the
    re-projection and the MCP registration follow.

    * **An ADOPTED Weaviate / Ollama is never moved by VCO** (it is somebody
      else's container). ``move`` follows the owner's new endpoint: the pinned
      container's newly published port (``adopted_container``), or a
      ``--url`` (``adopted_external``) — re-pointed only after that endpoint
      answers as the service, passes the compatibility gate, and (Weaviate)
      does not trade VCO data for an empty instance without
      ``--accept-empty-kg`` (I6).
    * **VCO's own (``vco_managed``) Weaviate / Ollama / code_embed** moves by
      being RE-CREATED on ``--port`` (Weaviate: and its gRPC port —
      ``--grpc-port``, else it keeps its offset from the HTTP port) with the
      SAME data mount (``service_lifecycle.migrate_managed_service``: the
      mount checked before anything stops and again after, the service
      answering on the new port — Weaviate with the same class list — and a
      rollback on failure). The row ends where the service answers.
    """
    root = Path(orchestrator_root)
    fetch = fetch or _det.default_fetch
    rows = _se.load_rows(db_path)
    row = rows.get(service)
    if row is None:
        out(f"no {service} row yet — `python install.py --update` records it first")
        return 1
    if row.mode == "vco_managed":
        return _move_managed(row, rows, port=port, url=url, grpc_port=grpc_port, root=root,
                             db_path=db_path, runtime=runtime, fetch=fetch, port_free=port_free,
                             migrate=migrate, out=out, apply_kwargs=apply_kwargs)
    rt = runtime if runtime is not None else _runtime()
    if row.mode == "adopted_container":
        det = _det.detect(runtime=rt, run=run, fetch=fetch, tcp_open=tcp_open, services=(service,))
        c = det.container_named(row.container_name or "")
        if c is None:
            out(f"your container {row.container_name} is not there — nothing to follow")
            return 1
        published = c.host_ports.get(_det.CONTAINER_PORTS[service])
        parsed_url = parse_url(url or "")
        wanted = port or (parsed_url[2] if parsed_url else None)
        if published is None or (wanted is not None and wanted != published):
            out(f"{c.name} publishes :{published}, not :{wanted} — VCO follows the container, "
                "it does not move it")
            return 1
        cand = next((x for x in det.for_service(service)
                     if x.port == published and x.container is not None
                     and x.container.name == c.name), None)
        if cand is None or not cand.live or not cand.compatible:
            out(f"{c.name} on :{published} does not answer as a usable {service}"
                + (f" ({cand.reason})" if cand is not None and cand.reason else ""))
            return 1
        new = replace(row, port=published, grpc_port=cand.grpc_port if service == "weaviate" else None,
                      compose_project=c.compose_project or row.compose_project,
                      data_mount=_mount_of(service, c) or row.data_mount,
                      source="user_cli", verified_at=_now_ms())
    else:  # adopted_external
        target = url or (f"{row.scheme}://{row.host}:{port}" if port else None)
        parsed = parse_url(target or "")
        if parsed is None:
            out("`move` on an external endpoint needs --url (or --port on the same host)")
            return 2
        scheme, host, new_port = parsed
        det = _det.detect(runtime=rt, run=run, fetch=fetch, tcp_open=tcp_open, services=(service,),
                          extra_endpoints=[_det.Endpoint(service, host, new_port, scheme, grpc_port,
                                                         origin="user")])
        cand = next((x for x in det.for_service(service) if x.port == new_port
                     and _norm_host(x.host) == _norm_host(host)), None)
        if cand is None or not cand.live or not cand.compatible:
            out(f"{target} does not answer as a usable {service}"
                + (f" ({cand.reason})" if cand is not None and cand.reason else ""))
            return 1
        if (service == "weaviate" and not accept_empty_kg and not cand.has_vco_data
                and _current_holds_data(service, rows, fetch)):
            out(f"{_se.render_url(service, row)} holds VCO data and {target} holds none. Re-run with "
                "--accept-empty-kg if that is really where your Weaviate is now.")
            return 1
        new = replace(row, scheme=scheme, host=host, port=new_port,
                      grpc_port=(grpc_port or cand.grpc_port or row.grpc_port) if service == "weaviate" else None,
                      source="user_cli", verified_at=_now_ms())
    if (new.scheme, new.host, new.port, new.grpc_port) == (row.scheme, row.host, row.port, row.grpc_port):
        out(f"{service} already at {_se.render_url(service, row)}")
        return 0
    return _commit(new, root, db_path, out, apply_kwargs)


def _derive_grpc_port(row: _se.EndpointRow, new_port: int, taken: set[int],
                      free: PortFreeFn) -> Optional[int]:
    """Weaviate's gRPC port for a move to *new_port* when ``--grpc-port`` was
    not given: it keeps its offset from the HTTP port (8081/50052 →
    18081/60052, the rule a fresh VCO copy uses), else the next free one."""
    start = _se.render_grpc_port(row) + (new_port - row.port)
    for candidate in range(max(start, 1), 65536):
        if candidate != new_port and candidate not in taken and free(candidate):
            return candidate
    return None


def _move_managed(row: _se.EndpointRow, rows: Mapping[str, _se.EndpointRow], *,
                  port: Optional[int], url: Optional[str], grpc_port: Optional[int],
                  root: Path, db_path: Optional[Path], runtime: Optional[str],
                  fetch: _det.FetchFn, port_free: Optional[PortFreeFn],
                  migrate: Optional[MigrateFn], out: LogFn,
                  apply_kwargs: Optional[Mapping[str, Any]]) -> int:
    """``move`` on a ``vco_managed`` row: VCO's own container is re-created on
    the new port(s) with the SAME data mount
    (``service_lifecycle.migrate_managed_service``)."""
    service = row.service
    weaviate = service == "weaviate"
    usage = f"`move --service {service} --port <N>`" + (" [--grpc-port <N>]" if weaviate else "")
    if url or (port is None and not (weaviate and grpc_port is not None)):
        out(f"VCO's own {service} moves by port only: {usage}")
        return 2
    if grpc_port is not None and not weaviate:
        out(f"--grpc-port is Weaviate's; VCO's own {service} moves with {usage}")
        return 2
    free = port_free or default_port_free
    taken = {r.port for s, r in rows.items() if s != service} | {
        r.grpc_port for s, r in rows.items() if s != service and r.grpc_port}
    new_port = port if port is not None else row.port
    current_grpc = _se.render_grpc_port(row) if weaviate else None
    new_grpc = current_grpc
    if weaviate and grpc_port is not None:
        new_grpc = grpc_port
    elif weaviate and new_port != row.port:
        new_grpc = _derive_grpc_port(row, new_port, taken, free)
        if new_grpc is None:
            out(f"no free gRPC port for Weaviate on :{new_port} — pass --grpc-port <N>")
            return 1
    if (new_port, new_grpc) == (row.port, current_grpc):
        out(f"{service} already at {_se.render_url(service, row)}"
            + (f" (gRPC {current_grpc})" if weaviate else ""))
        return 0
    wanted = [p for p, before in ((new_port, row.port), (new_grpc, current_grpc))
              if p is not None and p != before]
    if weaviate and new_port == new_grpc:
        out(f"Weaviate's HTTP and gRPC ports must differ (both :{new_port})")
        return 1
    for p in wanted:
        if p in taken or not free(p):
            out(f"port {p} is taken")
            return 1
    new = replace(row, port=new_port, grpc_port=new_grpc if weaviate else row.grpc_port,
                  source="user_cli", verified_at=None)
    where = f":{new_port}" + (f" (gRPC :{new_grpc})" if weaviate else "")

    def commit(batch: Sequence[_se.EndpointRow]) -> Any:
        return _se.commit_rows(batch, orchestrator_root=root, db_path=db_path, out=out,
                               **dict(apply_kwargs or {}))

    if migrate is None:
        from vco_lib.service_lifecycle import migrate_managed_service as migrate  # noqa: PLC0415
    result = migrate(root, new, runtime=runtime or _runtime() or "podman", log=out,
                     db_path=db_path, commit=commit)
    status = getattr(result, "status", "failed")
    if status == "not_needed":  # no container yet: the next compose up creates it on the new port
        rc = _commit(new, root, db_path, out, apply_kwargs)
        if rc == 0:
            out(f"{service} recorded on {where}; it starts there at the next session start "
                "(or `python install.py --update`).")
        return rc
    if status == "migrated":
        out(f"{service} re-created on {where} with its data.")
        return 0
    # refused (nothing stopped) or failed (rolled back): the row must say where
    # the service ACTUALLY answers — never a port nothing listens on.
    reason = getattr(result, "reason", "")
    at_new = _det.probe_endpoint(service, _se.render_url(service, new), fetch=fetch)
    # A Weaviate is followed only when it visibly holds VCO data (I6): one
    # whose recreate failed its class-list check must not become the endpoint.
    follows = (status == "failed" and at_new.answered and at_new.is_service
               and (service != "weaviate" or bool(at_new.vco_markers)))
    keep = new if follows else row
    _commit(keep, root, db_path, out, apply_kwargs)
    out(f"{service} NOT moved ({status}): {reason}. It stays on :{keep.port}.")
    return 1


def hand_to_vco(service: str, *, orchestrator_root: Path, runtime: Optional[str] = None,
                out: LogFn = print) -> int:
    """``hand-to-vco --service S`` — the opt-in ownership transfer (owner
    ruling Q2): VCO's compose takes the adopted container over, with the
    SAME data mount, verified, rolled back on failure
    (:func:`vco_lib.service_adoption.adopt_services`)."""
    result = _service_adoption.adopt_services(
        Path(orchestrator_root), services=(service,), runtime=runtime or _runtime() or "podman")
    for line in result.lines:
        out(line)
    return 0 if service in result.adopted else 1


def cli(verb: str, args: argparse.Namespace) -> int:
    """Handlers for the row-changing / detecting verbs of
    ``python -m vco_lib.service_endpoints``."""
    root = Path(getattr(args, "root", None) or Path(__file__).resolve().parent.parent)
    db = getattr(args, "db_path", None)
    if verb == "candidates":
        det = _det.detect(runtime=_runtime(), services=(args.service,) if args.service else SERVICES)
        if args.json:
            print(json.dumps({"schema": 1, "candidates": det.to_json()}, indent=2, sort_keys=True))
            return 0
        for s in SERVICES:
            for c in det.for_service(s):
                where = f" container {c.container.name} ({c.container.state})" if c.container else ""
                data = f" VCO data: {', '.join(c.probe.vco_markers[:3])}" if c.has_vco_data and c.probe else ""
                verdict = "usable" if c.compatible else f"not usable: {c.reason}"
                print(f"{s}: {c.url}{where} — {verdict}{data}")
        return 0
    if verb == "adopt":
        return adopt_endpoint(args.service, container=args.container, url=args.url,
                              orchestrator_root=root, accept_empty_kg=args.accept_empty_kg, db_path=db)
    if verb == "use-vco-copy":
        return use_vco_copy(args.service, port=args.port, orchestrator_root=root,
                            accept_empty_kg=args.accept_empty_kg, db_path=db)
    if verb == "hand-to-vco":
        return hand_to_vco(args.service, orchestrator_root=root)
    if verb == "move":
        return move_endpoint(args.service, orchestrator_root=root, port=args.port, url=args.url,
                             grpc_port=args.grpc_port, accept_empty_kg=args.accept_empty_kg,
                             db_path=db)
    if verb == "reconcile":
        result = reconcile(phase=args.phase, orchestrator_root=root, runtime=_runtime(), db_path=db)
        # With --json, stdout is a machine contract (the launcher parses it):
        # the human lines go to stderr.
        human = sys.stderr if args.json else sys.stdout
        for line in result.lines:
            print(line, file=human)
        if result.entries:
            from vco_lib.deferral_emit import emit_entries  # noqa: PLC0415

            emit_entries(root, result.entries)
        if args.json:
            print(json.dumps({
                "schema": 1,
                "rows": {s: r.to_json() for s, r in result.rows.items()},
                "abort": result.abort,
                "weaviate_awaiting_confirmation": result.weaviate_pending,
                "entries": [e.condition_id for e in result.entries],
            }, indent=2, sort_keys=True))
        return 1 if result.abort else 0
    print(f"unknown verb {verb}", file=sys.stderr)
    return 2
