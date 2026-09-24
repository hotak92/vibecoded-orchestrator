# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What Weaviate / Ollama / code-embed services exist on this machine.

The ONE detector (v0.2.97, plan §4b). Before it there were three copies of
the "is this ours?" fingerprint — install.py's ``_probe_service_identity``
(``exact_markers`` / ``vct_markers``) and the launcher's
``services::picker::is_canonical_collection`` / ``is_canonical_model`` — and
every one of them probed only VCO's own ports, so a third-party Ollama on
upstream's ``11434`` or a Weaviate on ``8080`` was invisible and a second
stack started next to it (the Bug-29 outcome). This module is used by
``install.py`` (through :mod:`vco_lib.service_reconcile`) and by the GUI
(``python -m vco_lib.service_endpoints candidates --json``).

What it does, read-only:

* **Containers** — ``<rt> ps -a`` then ONE ``<rt> inspect`` for every name:
  image, state, labels, published ports, mounts, env. A container is a
  candidate for a service when its compose-service label names it, its image
  is that service's upstream image, or its name is one VCO has ever used
  (:func:`vco_lib.containers.all_known_names`).
* **Endpoints** — HTTP probes on VCO's ports AND upstream's defaults, on every
  port a candidate publishes, and on every extra endpoint the caller names
  (legacy statements, the current row). Fullness ("holds VCO data") is read on
  the candidate's OWN port — the launcher picker read it on the canonical port.
* **Fingerprints** — one marker list per service (below).
* **Compatibility** — a Weaviate is adoptable only at version >=
  :data:`MIN_WEAVIATE_VERSION`, with anonymous access, and with a reachable
  gRPC port (VCO's v4 client needs it). An Ollama is adoptable when
  ``/api/tags`` answers. code-embed is never adopted (it is always VCO's).

Every subprocess goes through ``run``, every HTTP GET through ``fetch``, every
TCP check through ``tcp_open`` — tests drive the whole detector with fakes and
never touch a runtime or a port. Nothing here writes, starts or stops
anything.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from vco_lib import containers as _containers
from vco_lib.weaviate_helpers import schema_class_names

__all__ = [
    "CONTAINER_PORTS",
    "HttpResponse",
    "MIN_WEAVIATE_VERSION",
    "OLLAMA_MODEL_MARKERS",
    "PROBE_PORTS",
    "WEAVIATE_EXACT_MARKERS",
    "WEAVIATE_SUFFIX_MARKERS",
    "Candidate",
    "ContainerInfo",
    "Detection",
    "Endpoint",
    "Mount",
    "Probe",
    "compatibility",
    "container_ownership",
    "default_fetch",
    "default_tcp_open",
    "detect",
    "list_containers",
    "ollama_vco_markers",
    "parse_inspect",
    "probe_endpoint",
    "service_of_container",
    "weaviate_vco_markers",
]

SERVICES: tuple[str, ...] = ("weaviate", "ollama", "code_embed")

#: The port each service listens on INSIDE its container (compose maps the
#: host port onto these; ``infrastructure/docker-compose.yml``).
CONTAINER_PORTS: dict[str, int] = {"weaviate": 8080, "ollama": 11434, "code_embed": 11440}
WEAVIATE_CONTAINER_GRPC_PORT = 50051

#: Host ports probed on localhost even when no container publishes them: VCO's
#: defaults first, then upstream's (a native Ollama app answers on 11434).
PROBE_PORTS: dict[str, tuple[int, ...]] = {
    "weaviate": (8081, 8080),
    "ollama": (11435, 11434),
    "code_embed": (11440,),
}

#: gRPC ports tried for a Weaviate that is not a container we can inspect:
#: VCO's default beside VCO's HTTP port, upstream's otherwise.
_GRPC_GUESSES: tuple[int, ...] = (50052, 50051)

#: The oldest Weaviate VCO can use: the collections VCO creates carry NAMED
#: vectors (one slot per embedding profile), which Weaviate introduced in
#: 1.24. Pinned by tests/test_v0297_service_reconcile.py.
MIN_WEAVIATE_VERSION: tuple[int, int, int] = (1, 24, 0)

#: Class names only VCO creates. Exact names cover single-tenant and legacy
#: installs (the capital-C canonical shared class, its v0.2.12–v0.2.22
#: lowercase-c variant, the pre-v0.2.12 name); suffixes cover the per-project
#: namespaced classes. Suffix match is case-insensitive: pre-v0.2.46 installs
#: created lowercase ``_development`` classes. Deliberately NOT here: generic
#: names such as ``ChatMessages`` / ``DocumentChunks`` / ``UnifiedMessages``
#: that the launcher picker once counted for "fullness" — a third-party
#: Weaviate can plausibly hold them, and "holds VCO data" decides whether VCO
#: adopts an instance without asking (plan §10 Q1), so a false positive here
#: is a silent adoption.
WEAVIATE_EXACT_MARKERS: frozenset[str] = frozenset({
    "KnowledgeGraph",
    "VibeCodedOrchestrator_KnowledgeGraph",
    "VibecodedOrchestrator_KnowledgeGraph",
    "VibeCodedTools_KnowledgeGraph",
    "ClaudeKnowledgeGraph",
    "Development",
    "CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction",
})
WEAVIATE_SUFFIX_MARKERS: tuple[str, ...] = (
    "_KnowledgeGraph", "_Development", "_Diagrams",
    "_CodeFunction", "_CodeClass", "_CodeModule", "_CodeAPI", "_CodeInteraction",
    "_conversations",
)
#: Models VCO pulls (embedding profiles + the KG-summary fallbacks).
OLLAMA_MODEL_MARKERS: frozenset[str] = frozenset({
    "qwen3-embedding:0.6b",
    "snowflake-arctic-embed2:latest",
    "unclemusclez/jina-embeddings-v2-base-code:latest",
    "qwen3.5:0.8b",
    "qwen3.5:9b",
    "gemma4:e4b",
})
_CODE_EMBED_BODY_MARKERS: tuple[str, ...] = ("codesage", "code_embed")

#: Upstream image repositories per service (matched as a substring of the
#: image reference, so registry prefixes and tags do not matter).
_IMAGE_REPOS: dict[str, tuple[str, ...]] = {
    "weaviate": ("semitechnologies/weaviate",),
    "ollama": ("ollama/ollama",),
}
_COMPOSE_SERVICE_LABELS: tuple[str, ...] = (
    "com.docker.compose.service", "io.podman.compose.service",
)
#: Directories of VCO's own legacy compose home. A container whose compose
#: working dir / config files live under one of these was created by VCO,
#: even though its compose project is not the installer's.
_LEGACY_VCO_COMPOSE_DIRS: tuple[str, ...] = ("claude_mcp_servers",)

_HEALTH_PATHS: dict[str, str] = {
    "weaviate": "/v1/.well-known/ready",
    "ollama": "/api/tags",
    "code_embed": "/health",
}

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str = ""


FetchFn = Callable[[str, float], Optional[HttpResponse]]
TcpFn = Callable[[str, int, float], bool]


#: Wall-clock budget for reading ONE response body. The body is never capped
#: in SIZE — a truncated ``/v1/schema`` would read VCO's own Weaviate as a
#: third-party one (and, under ruling Q1, park it) — but a service that
#: answers and then stalls or trickles must not stall the install. The socket
#: ``timeout`` bounds each read; this bounds their sum.
READ_DEADLINE_S = 20.0


def read_within(resp: Any, deadline_s: float, *,
                clock: Callable[[], float] = time.monotonic) -> Optional[bytes]:
    """The whole body of *resp*, or ``None`` if it did not arrive within
    *deadline_s* (could not look — never a partial body)."""
    end = clock() + deadline_s
    read = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    while True:
        if clock() > end:
            return None
        chunk = read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def default_fetch(url: str, timeout: float, *, deadline_s: float = READ_DEADLINE_S,
                  clock: Callable[[], float] = time.monotonic) -> Optional[HttpResponse]:
    """GET *url*; ``None`` when nothing answers or the body did not arrive in
    time. An HTTP error status is an answer (a 401 from ``/v1/meta`` is how an
    auth-enabled Weaviate shows)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local probe
            body = read_within(resp, deadline_s, clock=clock)
            if body is None:
                return None
            return HttpResponse(resp.status, body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, "")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def default_tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=timeout):
            return True
    except OSError:
        return False


# ─── containers ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mount:
    kind: str          # "bind" | "volume"
    source: str        # host path (bind) or volume name (volume)
    destination: str

    def to_row(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "destination": self.destination}


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    image: str
    state: str
    labels: Mapping[str, str] = field(default_factory=dict)
    #: container port → host port, for every published TCP port.
    host_ports: Mapping[int, int] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    @property
    def compose_project(self) -> str:
        return self.labels.get(_containers.COMPOSE_PROJECT_LABEL, "") or ""

    @property
    def compose_home(self) -> str:
        return " ".join(
            self.labels.get(k, "") or ""
            for k in (_containers.COMPOSE_WORKING_DIR_LABEL, _containers.COMPOSE_CONFIG_FILES_LABEL)
        )

    def mount_at(self, destination: str) -> Optional[Mount]:
        return next((m for m in self.mounts if m.destination == destination), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "image": self.image, "state": self.state,
            "compose_project": self.compose_project or None,
            "host_ports": {str(k): v for k, v in sorted(self.host_ports.items())},
            "mounts": [m.to_row() for m in self.mounts],
        }


def _mount_from(entry: Mapping[str, Any]) -> Optional[Mount]:
    kind = str(entry.get("Type", "") or "").lower()
    if kind not in ("bind", "volume"):
        return None
    source = str(entry.get("Source", "") or "")
    if kind == "volume":
        # Name is the volume name; Source is its mountpoint on the host.
        source = str(entry.get("Name", "") or "") or source
    destination = str(entry.get("Destination", "") or "")
    if not source or not destination:
        return None
    return Mount(kind, source, destination)


def parse_inspect(obj: Mapping[str, Any]) -> Optional[ContainerInfo]:
    """One ``inspect`` object (podman or docker shape) → :class:`ContainerInfo`."""
    name = str(obj.get("Name", "") or "").lstrip("/")
    if not name:
        return None
    config = obj.get("Config") or {}
    state = obj.get("State") or {}
    host_config = obj.get("HostConfig") or {}
    image = str(config.get("Image") or obj.get("ImageName") or obj.get("Image") or "")
    labels = {str(k): str(v) for k, v in (config.get("Labels") or {}).items()}
    status = state.get("Status") if isinstance(state, Mapping) else None
    if not status:
        status = "running" if isinstance(state, Mapping) and state.get("Running") else "exited"
    host_ports: dict[int, int] = {}
    for cont_port, bindings in (host_config.get("PortBindings") or {}).items():
        port_s, _, proto = str(cont_port).partition("/")
        if proto and proto != "tcp":
            continue
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            host_port = str((binding or {}).get("HostPort", "") or "")
            if port_s.isdigit() and host_port.isdigit():
                host_ports[int(port_s)] = int(host_port)
                break
    mounts = tuple(
        m for m in (_mount_from(e) for e in (obj.get("Mounts") or []) if isinstance(e, Mapping))
        if m is not None
    )
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            env[k] = v
    return ContainerInfo(name=name, image=image, state=str(status), labels=labels,
                         host_ports=host_ports, mounts=mounts, env=env)


def list_containers(runtime: Optional[str], *, run: Optional[RunFn] = None) -> list[ContainerInfo]:
    """Every container on *runtime* (running or not). ``[]`` without a runtime
    or on any failure — detection then rests on the HTTP probes alone."""
    if not runtime:
        return []
    run = run or subprocess.run
    try:
        res = run([runtime, "ps", "-a", "--format", "{{.Names}}"],
                  capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if res.returncode != 0:
        return []
    names = [n.strip() for n in (res.stdout or "").splitlines() if n.strip()]
    if not names:
        return []
    try:
        res = run([runtime, "inspect", "--type", "container", *names],
                  capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if res.returncode != 0:
        return []
    try:
        payload = json.loads(res.stdout or "[]")
    except ValueError:
        return []
    out = []
    for obj in payload if isinstance(payload, list) else []:
        if isinstance(obj, Mapping):
            info = parse_inspect(obj)
            if info is not None:
                out.append(info)
    return out


def service_of_container(c: ContainerInfo) -> Optional[str]:
    """The core service *c* is, or ``None``."""
    for label in _COMPOSE_SERVICE_LABELS:
        if c.labels.get(label) in SERVICES:
            return c.labels[label]
    image = c.image.lower()
    for service, repos in _IMAGE_REPOS.items():
        if any(repo in image for repo in repos):
            return service
    for service in SERVICES:
        if c.name in _containers.all_known_names(service):
            return service
    return None


def container_ownership(c: ContainerInfo, service: str, installer_project: str) -> str:
    """``installer`` (created by the installer's compose), ``legacy_vco``
    (VCO's own container under another compose home — the canonical ``vco_*``
    name, or a compose home under VCO's legacy directory), or ``third_party``."""
    if installer_project and c.compose_project == installer_project:
        return "installer"
    if c.name == _containers.canonical_name(service):
        return "legacy_vco"
    home = c.compose_home.replace("\\", "/")
    if any(f"/{d}/" in f"{home}/" or home.endswith(f"/{d}") for d in _LEGACY_VCO_COMPOSE_DIRS):
        return "legacy_vco"
    return "third_party"


# ─── HTTP fingerprints ──────────────────────────────────────────────────


def weaviate_vco_markers(classes: Iterable[str]) -> tuple[str, ...]:
    lowered = tuple(s.lower() for s in WEAVIATE_SUFFIX_MARKERS)
    hits = {c for c in classes if c in WEAVIATE_EXACT_MARKERS or c.lower().endswith(lowered)}
    return tuple(sorted(hits))


def ollama_vco_markers(models: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(models) & OLLAMA_MODEL_MARKERS))


def _version_tuple(version: str) -> Optional[tuple[int, int, int]]:
    parts = version.strip().lstrip("v").split("-", 1)[0].split(".")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


@dataclass(frozen=True)
class Probe:
    """What one endpoint answered."""

    service: str
    url: str
    answered: bool = False        # the service's health path answered 2xx
    is_service: bool = False      # the answer identifies it as *service*
    vco_markers: tuple[str, ...] = ()
    version: Optional[str] = None
    auth_required: bool = False
    #: Weaviate answered but its ``/v1/schema`` could not be read (in time):
    #: whether it holds VCO data is UNKNOWN, so it is not usable either way.
    schema_unread: bool = False
    detail: str = ""


def _json(resp: Optional[HttpResponse]) -> Any:
    if resp is None or not (200 <= resp.status < 300):
        return None
    try:
        return json.loads(resp.body or "null")
    except ValueError:
        return None


def probe_endpoint(service: str, base_url: str, *, fetch: FetchFn, timeout: float = 3.0) -> Probe:
    """Fingerprint whatever answers at *base_url* as *service*."""
    base = base_url.rstrip("/")
    health = fetch(f"{base}{_HEALTH_PATHS[service]}", timeout)
    answered = health is not None and 200 <= health.status < 300
    if service == "weaviate":
        meta_resp = fetch(f"{base}/v1/meta", timeout)
        auth = meta_resp is not None and meta_resp.status in (401, 403)
        meta = _json(meta_resp)
        version = str(meta.get("version")) if isinstance(meta, dict) and meta.get("version") else None
        is_service = version is not None or (answered and auth)
        if not (answered or is_service):
            return Probe(service, base, detail="nothing answers")
        classes: set[str] = set()
        unread = False
        if not auth:
            schema = _json(fetch(f"{base}/v1/schema", timeout))
            unread = not isinstance(schema, dict)
            classes = schema_class_names(schema)
        markers = weaviate_vco_markers(classes)
        return Probe(service, base, answered=answered or is_service, is_service=is_service,
                     vco_markers=markers, version=version, auth_required=auth,
                     schema_unread=unread,
                     detail=f"weaviate {version or '?'}; "
                            + ("schema unreadable" if unread else f"{len(classes)} class(es)"))
    if health is None:
        return Probe(service, base, detail="nothing answers")
    if service == "ollama":
        tags = _json(health)
        if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
            return Probe(service, base, answered=answered, detail="answers, but not as Ollama")
        models = [str(m.get("name", "")) for m in tags["models"] if isinstance(m, dict)]
        return Probe(service, base, answered=True, is_service=True,
                     vco_markers=ollama_vco_markers(models),
                     detail=f"ollama; {len(models)} model(s)")
    body = (health.body or "").lower()
    ours = answered and any(m in body for m in _CODE_EMBED_BODY_MARKERS)
    return Probe(service, base, answered=answered, is_service=ours,
                 vco_markers=("code_embed",) if ours else (),
                 detail="code-embed" if ours else "answers /health, but is not VCO's code-embed")


# ─── candidates ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    """An endpoint a caller wants probed (a legacy statement, the current
    row): not discovered, named."""

    service: str
    host: str
    port: int
    scheme: str = "http"
    grpc_port: Optional[int] = None
    origin: str = "named"


@dataclass(frozen=True)
class Candidate:
    service: str
    host: str
    port: int
    scheme: str = "http"
    grpc_port: Optional[int] = None
    container: Optional[ContainerInfo] = None
    probe: Optional[Probe] = None
    ownership: str = "unknown"       # installer | legacy_vco | third_party | unknown (no container)
    compatible: bool = False
    reason: str = ""
    origins: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def live(self) -> bool:
        return bool(self.probe and self.probe.answered and self.probe.is_service)

    @property
    def has_vco_data(self) -> bool:
        return bool(self.live and self.probe and self.probe.vco_markers)

    def to_json(self) -> dict[str, Any]:
        return {
            "service": self.service, "url": self.url, "host": self.host, "port": self.port,
            "grpc_port": self.grpc_port, "live": self.live, "has_vco_data": self.has_vco_data,
            "vco_markers": list(self.probe.vco_markers) if self.probe else [],
            "version": self.probe.version if self.probe else None,
            "compatible": self.compatible, "reason": self.reason,
            "ownership": self.ownership, "origins": list(self.origins),
            "container": self.container.to_json() if self.container else None,
        }


def compatibility(service: str, probe: Optional[Probe], grpc_port: Optional[int]) -> tuple[bool, str]:
    """``(adoptable, reason)`` — the gate a candidate must pass before VCO
    points its clients at it."""
    if probe is None or not probe.answered:
        return False, "not answering"
    if not probe.is_service:
        return False, f"the port answers, but not as {service}"
    if service == "code_embed":
        return True, ""
    if service == "ollama":
        return True, ""
    if probe.auth_required:
        return False, "requires authentication (VCO needs anonymous access)"
    if probe.schema_unread:
        return False, "its /v1/schema could not be read (in time) — whether it holds VCO data is unknown"
    parsed = _version_tuple(probe.version or "")
    if parsed is None:
        return False, "its version could not be read from /v1/meta"
    if parsed < MIN_WEAVIATE_VERSION:
        floor = ".".join(str(n) for n in MIN_WEAVIATE_VERSION)
        return False, f"version {probe.version} is below {floor} (VCO's collections use named vectors)"
    if grpc_port is None:
        return False, "no reachable gRPC port (VCO's Weaviate client needs it)"
    return True, ""


@dataclass
class Detection:
    containers: list[ContainerInfo] = field(default_factory=list)
    candidates: dict[str, list[Candidate]] = field(default_factory=dict)

    def for_service(self, service: str) -> list[Candidate]:
        return list(self.candidates.get(service, []))

    def container_named(self, name: str) -> Optional[ContainerInfo]:
        return next((c for c in self.containers if c.name == name), None)

    def to_json(self) -> dict[str, Any]:
        return {s: [c.to_json() for c in self.candidates.get(s, [])] for s in SERVICES}


def _grpc_for(host: str, port: int, hint: Optional[int], tcp_open: TcpFn) -> Optional[int]:
    """The gRPC port of a Weaviate VCO cannot inspect as a container: a stated
    one first, then upstream's beside upstream's HTTP port, else VCO's."""
    guesses = (hint, 50051, 50052) if port == 8080 else (hint, *_GRPC_GUESSES)
    for guess in dict.fromkeys(g for g in guesses if g):
        if tcp_open(host, guess, 1.0):
            return guess
    return None


def detect(
    *,
    runtime: Optional[str],
    installer_project: str = "",
    extra_endpoints: Sequence[Endpoint] = (),
    run: Optional[RunFn] = None,
    fetch: Optional[FetchFn] = None,
    tcp_open: Optional[TcpFn] = None,
    services: Sequence[str] = SERVICES,
) -> Detection:
    """Every candidate for every service in *services*, probed.

    One candidate per (host, port) that something answers on or a container
    publishes; a RUNNING container there owns what answers. A STOPPED
    container is listed on its own (never probed — whatever answers on its
    port is some other listener, listed separately)."""
    fetch = fetch or default_fetch
    tcp_open = tcp_open or default_tcp_open
    detection = Detection(containers=list_containers(runtime, run=run))
    for service in services:
        slots: dict[tuple[str, int], dict[str, Any]] = {}

        def slot(host: str, port: int, origin: str, *, scheme: str = "http",
                 grpc: Optional[int] = None, _slots: dict = slots) -> dict[str, Any]:
            entry = _slots.setdefault((host, port), {
                "running": None, "stopped": [], "origins": [], "scheme": scheme, "grpc": None})
            if grpc and not entry["grpc"]:
                entry["grpc"] = grpc
            if origin not in entry["origins"]:
                entry["origins"].append(origin)
            return entry

        unpublished: list[ContainerInfo] = []
        for c in detection.containers:
            if service_of_container(c) != service:
                continue
            host_port = c.host_ports.get(CONTAINER_PORTS[service])
            if host_port is None:
                unpublished.append(c)
                continue
            entry = slot("localhost", host_port, f"container:{c.name}")
            if c.running and entry["running"] is None:
                entry["running"] = c
            else:
                entry["stopped"].append(c)
        for port in PROBE_PORTS[service]:
            slot("localhost", port, f"port:{port}")
        for ep in extra_endpoints:
            if ep.service == service:
                host = "localhost" if ep.host == "127.0.0.1" else ep.host
                slot(host, ep.port, ep.origin, scheme=ep.scheme, grpc=ep.grpc_port)

        out: list[Candidate] = []
        for (host, port), entry in slots.items():
            probe = probe_endpoint(service, f"{entry['scheme']}://{host}:{port}", fetch=fetch)
            running: Optional[ContainerInfo] = entry["running"]
            if running is not None or probe.answered:
                out.append(_candidate(service, host, port, entry, running, probe,
                                      installer_project, tcp_open))
            for c in entry["stopped"]:
                out.append(_candidate(service, host, port, entry, c, None,
                                      installer_project, tcp_open))
        for c in unpublished:
            out.append(Candidate(service=service, host="localhost", port=0, container=c,
                                 ownership=container_ownership(c, service, installer_project),
                                 compatible=False, reason="publishes no host port",
                                 origins=(f"container:{c.name}",)))
        detection.candidates[service] = sorted(
            out, key=lambda c: (not c.live, not c.has_vco_data, c.port,
                                c.container.name if c.container else ""))
    return detection


def _candidate(service: str, host: str, port: int, entry: Mapping[str, Any],
               container: Optional[ContainerInfo], probe: Optional[Probe],
               installer_project: str, tcp_open: TcpFn) -> Candidate:
    grpc = None
    if service == "weaviate":
        if container is not None:
            grpc = container.host_ports.get(WEAVIATE_CONTAINER_GRPC_PORT)
        elif probe is not None and probe.answered:
            grpc = _grpc_for(host, port, entry.get("grpc"), tcp_open)
    if probe is None:
        ok, reason = False, "stopped"
    else:
        ok, reason = compatibility(service, probe, grpc)
    origins = tuple(o for o in entry["origins"]
                    if not o.startswith("container:")
                    or (container is not None and o == f"container:{container.name}"))
    return Candidate(
        service=service, host=host, port=port, scheme=entry.get("scheme", "http"),
        grpc_port=grpc, container=container, probe=probe,
        ownership=(container_ownership(container, service, installer_project)
                   if container is not None else "unknown"),
        compatible=ok, reason=reason, origins=origins,
    )
