# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What VCO may do to the containers behind its three core services.

The launcher.db ``service_endpoints`` rows (``vco_lib.service_endpoints``)
say, per service, whether VCO's compose OWNS it (``vco_managed``), whether it
is somebody else's container VCO only starts and stops BY NAME
(``adopted_container``), or a URL with no lifecycle at all
(``adopted_external``). This module turns those rows into the lifecycle
decisions every caller acts on — the one home for them (A>B>C tier A: the
session hooks, the boot wrapper and install.py call it; the Rust watchdog and
launcher read the same rows):

* :func:`compose_services` — the ONLY services a compose invocation may name
  (plan invariant I1: a bare ``up -d`` does not exist anywhere, and an
  adopted service is never composed, recreated or removed);
* :func:`container_policies` / :func:`zombie_action` — per container, what to
  do when it is missing, stopped, or a podman zombie (running with a dead
  PID). A zombie ``vco_managed`` container is removed and re-created; a zombie
  ADOPTED container only gets its orphan runtime state cleaned and a
  ``start`` — never ``rm``, because a compose re-create would bring it back on
  the installer's default (empty) volume;
* :func:`compose_up_args` — the ``up`` argv for an explicit service list
  (``--no-deps`` always: code_embed's ``depends_on: ollama`` must never create
  an Ollama when Ollama is adopted);
* :func:`migrate_managed_service` — a ``vco_managed`` service re-created
  WITH its data (plan invariant I2): code-embed's migration, and the port
  ``move`` of VCO's own Weaviate / Ollama / code-embed. The live data mount
  is recorded in the row, projected into ``infrastructure/.env`` as the
  data-source knob, verified in the effective config (Python merge AND
  ``compose config``) before anything is stopped, and verified again on the
  new container after the up (same mount, the row's ports, the service
  answering — the same data inventory as before the recreate: Weaviate's
  class list, Ollama's model list); a mismatch refuses, or
  rolls back under the previous owner. :func:`migrate_code_embed` is its
  code-embed wrapper (install.py's entry).

CLI (the hooks and the boot wrapper)::

    python -m vco_lib.service_lifecycle plan --shell|--json [--required "a b"]
    python -m vco_lib.service_lifecycle compose-args --services "a b"
        [--build] [--gpu-mode gpu|cpu|unknown] --shell|--json
    python -m vco_lib.service_lifecycle session-reconcile [--timeout S]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib import service_endpoints as _se

__all__ = [
    "CACHE_DESTINATION",
    "ContainerPolicy",
    "MigrationResult",
    "compose_services",
    "compose_up_args",
    "container_policies",
    "lifecycle_plan",
    "migrate_code_embed",
    "migrate_managed_service",
    "zombie_action",
]

#: code_embed's model cache inside the container (the compose stanza's mount).
CACHE_DESTINATION = "/cache"

#: ``on_missing`` / ``on_zombie`` / ``on_stopped`` vocabularies.
MISSING_ACTIONS = ("compose", "report", "ignore")
ZOMBIE_ACTIONS = ("recreate", "start", "ignore")
STOPPED_ACTIONS = ("start", "ignore")

#: Every ``up`` VCO issues carries these, so a named service never pulls an
#: unnamed one in through ``depends_on``.
_UP_BASE = ("up", "-d")

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
LogFn = Callable[[str], None]
#: ``(url, timeout) -> parsed JSON body of a 200 answer, else None``.
FetchJsonFn = Callable[[str, float], Optional[dict]]


# ─── the compose service list (I1) ──────────────────────────────────────


def compose_services(rows: Mapping[str, "_se.EndpointRow"]) -> list[str]:
    """The compose services VCO may bring up: ``vco_managed`` + ``enabled``
    rows, plus services with NO row (before the first reconcile, VCO's own
    stack is the default). Never an ``adopted_*`` service. Order: weaviate,
    ollama, code_embed."""
    return list(_se.plan(rows)["managed_services"])


def _enables_gpu_profile(argv: Sequence[str]) -> bool:
    """Does *argv* already enable the ``gpu`` compose profile
    (``--profile gpu`` or ``--profile=gpu``)?"""
    tokens = [str(t) for t in argv]
    for i, token in enumerate(tokens):
        if token == "--profile=gpu":
            return True
        if token == "--profile" and i + 1 < len(tokens) and tokens[i + 1] == "gpu":
            return True
    return False


def compose_up_args(
    services: Sequence[str],
    *,
    build: bool = False,
    force_recreate: bool = False,
    gpu_mode: str = "unknown",
    prefix: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """``(args, dropped)``: the argv AFTER the compose prefix and ``-f``
    chain that brings up exactly *services*, and the services it had to drop.

    ``[]`` when nothing is left — the caller then runs NO compose command
    (an empty list must never become a bare ``up -d``). ``--no-deps`` is
    always present. code_embed is gated by the ``gpu`` compose profile, so
    naming it adds ``--profile gpu`` (podman-compose drops a profiled
    service it is not told to enable, even when named) — unless *prefix*,
    the argv these args will be appended to, already enables it (install.py
    adds it with the GPU overlay), so the profile appears exactly once. With
    ``gpu_mode="cpu"`` (the boot wrapper's CDI-timeout degrade) code_embed
    is dropped instead, as the whole-stack ``up`` without the profile always
    did. A name that is not a VCO service raises ``ValueError``."""
    if gpu_mode not in ("gpu", "cpu", "unknown"):
        raise ValueError(f"gpu_mode must be gpu|cpu|unknown, got {gpu_mode!r}")
    chosen: list[str] = []
    dropped: list[str] = []
    for service in services:
        if service not in _se.SERVICES:
            raise ValueError(f"{service!r} is not a VCO compose service ({', '.join(_se.SERVICES)})")
        if service in chosen:
            continue
        if service == "code_embed" and gpu_mode == "cpu":
            dropped.append(service)
            continue
        chosen.append(service)
    if not chosen:
        return [], dropped
    args: list[str] = []
    if "code_embed" in chosen and not _enables_gpu_profile(prefix):
        args += ["--profile", "gpu"]
    args += list(_UP_BASE)
    if build:
        args.append("--build")
    if force_recreate:
        args.append("--force-recreate")
    args.append("--no-deps")
    args += chosen
    return args, dropped


# ─── per-container policy (the zombie-recovery gate) ────────────────────


@dataclass(frozen=True)
class ContainerPolicy:
    """What lifecycle code may do to ONE container.

    ``service`` is ``""`` for a container named only by the user's
    ``VCT_REQUIRED_CONTAINERS`` that no row describes. ``mode`` is the row
    mode, or ``"unlisted"`` for such a container."""

    container: str
    service: str
    mode: str
    on_missing: str
    on_zombie: str
    on_stopped: str

    def to_json(self) -> dict[str, str]:
        return dataclasses.asdict(self)


_IGNORE = ("ignore", "ignore", "ignore")


def _policy_for(mode: str, enabled: bool, autostart: bool) -> tuple[str, str, str]:
    if mode == "vco_managed":
        return ("compose", "recreate", "start") if enabled else _IGNORE
    if mode == "adopted_container":
        # Started by name when VCO needs it; a zombie gets orphan-state
        # cleanup + start, NEVER rm; a missing one is reported, never
        # re-created (it is somebody else's).
        return ("report", "start", "start") if autostart else _IGNORE
    return _IGNORE


def container_policies(
    rows: Mapping[str, "_se.EndpointRow"],
    required: Optional[Sequence[str]] = None,
) -> list[ContainerPolicy]:
    """Every container lifecycle code looks at, with its policy.

    Without *required*: one entry per service that has a container (an
    ``adopted_external`` row has none). With *required* (the user's
    ``VCT_REQUIRED_CONTAINERS``): exactly those names, in that order — a
    name matching a service's container takes that service's policy; any
    other name is ``unlisted`` and is only ever STARTED (a missing one is
    reported), because VCO cannot know which compose service, if any, it
    belongs to."""
    p = _se.plan(rows)
    known: list[ContainerPolicy] = []
    for service in _se.SERVICES:
        entry = p["services"][service]
        container = str(entry.get("container") or "")
        if not container:
            continue
        on_missing, on_zombie, on_stopped = _policy_for(
            entry["mode"], bool(entry["enabled"]), bool(entry["autostart"]),
        )
        known.append(ContainerPolicy(container, service, entry["mode"],
                                     on_missing, on_zombie, on_stopped))
    if required is None:
        return known
    by_name = {pol.container: pol for pol in known}
    out: list[ContainerPolicy] = []
    for name in required:
        name = name.strip()
        if not name or any(pol.container == name for pol in out):
            continue
        out.append(by_name.get(name) or ContainerPolicy(
            name, "", "unlisted", "report", "start", "start"))
    return out


def zombie_action(rows: Mapping[str, "_se.EndpointRow"], service: str) -> str:
    """``recreate`` | ``start`` | ``ignore`` for *service*'s zombie container."""
    for pol in container_policies(rows):
        if pol.service == service:
            return pol.on_zombie
    return "ignore"


def lifecycle_plan(
    rows: Mapping[str, "_se.EndpointRow"],
    required: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """:func:`vco_lib.service_endpoints.plan` plus the compose service list
    and the per-container policies — everything the hooks act on, from ONE
    read of the rows."""
    out = dict(_se.plan(rows))
    out["compose_services"] = compose_services(rows)
    out["containers"] = [pol.to_json() for pol in container_policies(rows, required)]
    return out


def lifecycle_shell_lines(p: Mapping[str, Any]) -> list[str]:
    """:func:`lifecycle_plan` as bash assignments: SE-1's POSIX lines, then
    ``VCO_COMPOSE_SERVICES`` and one bash ARRAY per policy column
    (``VCO_LC_CONTAINER`` / ``_SERVICE`` / ``_MODE`` / ``_ON_MISSING`` /
    ``_ON_ZOMBIE`` / ``_ON_STOPPED``, index-aligned; ``-`` for no service)."""
    lines = _se.plan_shell_lines(p)
    lines.append(f"VCO_COMPOSE_SERVICES={shlex.quote(' '.join(p['compose_services']))}")
    columns = ("container", "service", "mode", "on_missing", "on_zombie", "on_stopped")
    for column in columns:
        values = [str(c[column]) or "-" for c in p["containers"]]
        lines.append(f"VCO_LC_{column.upper()}=({' '.join(shlex.quote(v) for v in values)})")
    return lines


# ─── recreate a vco_managed service WITH its data (I2) ──────────────────


@dataclass
class MigrationResult:
    """What :func:`migrate_managed_service` did.

    ``status``: ``migrated`` (re-created under the installer project on the
    same data mount, answering on the row's port), ``not_needed`` (no
    container to re-create), ``refused`` (nothing was stopped — ``reason``
    says why), ``failed`` (something was stopped and the recreate did not
    verify; ``reason`` includes the rollback outcome)."""

    status: str = "not_needed"
    reason: str = ""
    mount: Optional[dict] = None
    lines: list[str] = field(default_factory=list)
    argv_log: list[list[str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("migrated", "not_needed")


#: ``(host, port, timeout) -> bool`` — is a TCP port accepting connections.
TcpOpenFn = Callable[[str, int, float], bool]


def _default_fetch_json(url: str, timeout: float) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — localhost health probe
            if int(resp.status) != 200:
                return None
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001 — every failure is "not answering (yet)"
        return None
    return body if isinstance(body, dict) else None


def _find_container(row: "_se.EndpointRow", runtime: str, run: RunFn) -> Optional[str]:
    """The row's service container that exists: the row's name, then every
    known name (canonical + historical aliases). Probed through *run*."""
    from vco_lib import containers as _containers  # noqa: PLC0415

    names: list[str] = []
    for name in [row.container_name or "", *_containers.all_known_names(row.service)]:
        if name and name not in names:
            names.append(name)
    for name in names:
        try:
            res = run([runtime, "inspect", "--type", "container", "--format", "{{.Id}}", name],
                      capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if res.returncode == 0 and (res.stdout or "").strip():
            return name
    return None


def _mount_at(state: Any, destination: str) -> Any:
    for mount in getattr(state, "mounts", ()) or ():
        if mount.destination == destination:
            return mount
    return None


def _bind_holds_data(source: str) -> bool:
    path = Path(source)
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _effective_mount(infra: Path, service: str, destination: str, need_gpu: bool,
                     overlay: Optional[str]) -> tuple[list[Path], Any]:
    """The ``-f`` chain and the *destination* mount the INSTALLER's
    effective config gives *service*, merged in Python from the files on
    disk with the substitution env compose itself sees
    (``infrastructure/.env``)."""
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    env = _sa.infrastructure_env_for_substitution(infra)
    files, cfg = _sa._compose_entrypoints(need_gpu, overlay, infra, env)
    service_cfg = (cfg.get("services") or {}).get(service) or {}
    mounts = _sa.config_mounts(service_cfg, cfg.get("volumes") or {})
    return files, mounts.get(destination)


def _provider_mount(argv: list[str], infra: Path, service: str, destination: str, run: RunFn,
                    result: MigrationResult) -> tuple[Any, str]:
    """``compose config`` — the provider's OWN render — and the
    *destination* mount it gives *service*. ``(None, why)`` when it cannot
    be read."""
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    result.argv_log.append(list(argv))
    try:
        res = run(argv, capture_output=True, text=True, timeout=120, cwd=str(infra))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"`compose config` could not run: {exc}"
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        return None, "`compose config` failed: " + (tail[-1] if tail else f"exit {res.returncode}")
    import yaml  # noqa: PLC0415 — venv-time only (see service_adoption's header)

    try:
        doc = yaml.safe_load(res.stdout or "")
    except yaml.YAMLError as exc:
        return None, f"`compose config` output is not YAML: {exc}"
    if not isinstance(doc, dict):
        return None, "`compose config` printed no configuration"
    service_cfg = (doc.get("services") or {}).get(service)
    if not isinstance(service_cfg, dict):
        return None, f"`compose config` has no {service} service"
    mounts = _sa.config_mounts(service_cfg, doc.get("volumes") or {})
    return mounts.get(destination), ""


def _describe_mount(mount: Any, destination: str = CACHE_DESTINATION) -> str:
    if mount is None:
        return f"no {destination} mount"
    return f"{mount.kind} {mount.source}"


def _weaviate_classes(port: int, fetch: FetchJsonFn) -> Optional[frozenset[str]]:
    """The class names ``/v1/schema`` lists on *port*; ``None`` when it
    cannot be read (not answering, not JSON, no class list)."""
    body = fetch(f"http://localhost:{port}/v1/schema", 5.0)
    if not isinstance(body, dict):
        return None
    classes = body.get("classes")
    if classes is None:
        classes = []  # an empty schema
    if not isinstance(classes, list):
        return None
    return frozenset(str(c["class"]) for c in classes if isinstance(c, dict) and c.get("class"))


def _ollama_models(port: int, fetch: FetchJsonFn) -> Optional[frozenset[str]]:
    """The model names ``/api/tags`` lists on *port*; ``None`` when it cannot
    be read (not answering, not JSON, no model list)."""
    body = fetch(f"http://localhost:{port}/api/tags", 5.0)
    if not isinstance(body, dict):
        return None
    models = body.get("models")
    if models is None:
        models = []  # an Ollama with no models installed
    if not isinstance(models, list):
        return None
    return frozenset(str(m["name"]) for m in models if isinstance(m, dict) and m.get("name"))


#: What proves a recreate kept the data, per service: the set of names it
#: lists before the recreate must be the set it lists after (the "same
#: inventory before/after" check — Weaviate's classes, Ollama's models;
#: code-embed's proof is the health window instead).
_INVENTORY_LABEL: dict[str, str] = {"weaviate": "class list", "ollama": "model list"}
_INVENTORY_PATH: dict[str, str] = {"weaviate": "/v1/schema", "ollama": "/api/tags"}


def _service_inventory(service: str, port: int, fetch: FetchJsonFn) -> Optional[frozenset[str]]:
    """:func:`_weaviate_classes` / :func:`_ollama_models` behind one name —
    the inventory a recreate of *service* must keep; ``None`` for code_embed
    (not applicable) or when it cannot be read."""
    if service == "weaviate":
        return _weaviate_classes(port, fetch)
    if service == "ollama":
        return _ollama_models(port, fetch)
    return None


def _inventory_problem(service: str, port: int, before: Optional[frozenset[str]],
                       fetch: FetchJsonFn) -> str:
    """``""`` when *service*'s inventory on *port* is readable and unchanged
    since *before*; else why it is not."""
    after = _service_inventory(service, port, fetch)
    if after is None:
        return (f"{service.capitalize()}'s {_INVENTORY_LABEL[service]} "
                f"({_INVENTORY_PATH[service]} on :{port}) could not be read")
    if before is not None and after != before:
        missing = ", ".join(sorted(before - after)) or "none"
        added = ", ".join(sorted(after - before)) or "none"
        return (f"{service.capitalize()}'s {_INVENTORY_LABEL[service]} changed across the "
                f"recreate (missing: {missing}; new: {added})")
    return ""


def _answer_problem(row: "_se.EndpointRow", fetch: FetchJsonFn, tcp_open: TcpOpenFn,
                    inventory_before: Optional[frozenset[str]]) -> str:
    """Why *row*'s service does not (yet) answer as itself on the row's
    port — ``""`` when it does. Weaviate: ready, gRPC reachable, and the
    SAME class list as before the recreate. Ollama: ``/api/tags`` answering
    AND the same model list as before. code-embed: ``/health`` with the model
    loaded (an EMPTY cache would still be downloading — the window is the
    proof the cache came along)."""
    service, port = row.service, row.port
    base = f"http://localhost:{port}"
    if service == "weaviate":
        if fetch(f"{base}/v1/.well-known/ready", 2.0) is None:
            return f"Weaviate did not answer /v1/.well-known/ready on :{port}"
        grpc = _se.render_grpc_port(row)
        if not tcp_open("localhost", grpc, 2.0):
            return f"Weaviate's gRPC port :{grpc} is not reachable"
        return _inventory_problem(service, port, inventory_before, fetch)
    if service == "ollama":
        if fetch(f"{base}/api/tags", 2.0) is None:
            return f"Ollama did not answer /api/tags on :{port}"
        return _inventory_problem(service, port, inventory_before, fetch)
    body = fetch(f"{base}/health", 2.0)
    if isinstance(body, dict) and body.get("status") == "ok" and body.get("model_loaded", True):
        return ""
    return f"/health on :{port} did not report the model loaded"


def _wait_answering(row: "_se.EndpointRow", fetch: FetchJsonFn, tcp_open: TcpOpenFn,
                    inventory_before: Optional[frozenset[str]], timeout_s: float,
                    interval_s: float) -> str:
    """:func:`_answer_problem`, polled until it clears or *timeout_s* ends."""
    deadline = time.monotonic() + timeout_s
    while True:
        problem = _answer_problem(row, fetch, tcp_open, inventory_before)
        if not problem:
            return ""
        if time.monotonic() >= deadline:
            return f"{problem} within {int(timeout_s)} s"
        time.sleep(interval_s)


def _published_problem(row: "_se.EndpointRow", state: Any) -> str:
    """Does the container publish the row's port (and Weaviate's gRPC
    port)? ``""`` when it does."""
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    http = _sa.live_http_host_port(row.service, state)
    if http != str(row.port):
        return f"the new container publishes :{http or 'nothing'}, not :{row.port}"
    if row.service == "weaviate":
        grpc = str((getattr(state, "host_ports", None) or {}).get(_sa.WEAVIATE_GRPC_CONTAINER_PORT, ""))
        if grpc != str(_se.render_grpc_port(row)):
            return (f"the new container publishes gRPC :{grpc or 'nothing'}, "
                    f"not :{_se.render_grpc_port(row)}")
    return ""


def _default_commit(root: Path, db_path: Optional[Path]) -> Callable[[Sequence["_se.EndpointRow"]], Any]:
    def commit(rows: Sequence["_se.EndpointRow"]) -> Any:
        return _se.commit_rows(rows, orchestrator_root=root, db_path=db_path)
    return commit


def _same_identity(a: "_se.EndpointRow", b: "_se.EndpointRow") -> bool:
    mount_a = dict(a.data_mount) if a.data_mount else None
    mount_b = dict(b.data_mount) if b.data_mount else None
    return (mount_a, a.container_name) == (mount_b, b.container_name)


def migrate_managed_service(
    root: Path,
    row: "_se.EndpointRow",
    *,
    runtime: str = "podman",
    run: Optional[RunFn] = None,
    fetch: Optional[FetchJsonFn] = None,
    log: Optional[LogFn] = None,
    resolution: Optional[Any] = None,
    db_path: Optional[Path] = None,
    commit: Optional[Callable[[Sequence["_se.EndpointRow"]], Any]] = None,
    build: bool = True,
    health_timeout_s: float = 90.0,
    health_interval_s: float = 2.0,
    tcp_open: Optional[TcpOpenFn] = None,
) -> MigrationResult:
    """Re-create a ``vco_managed`` service under the installer's compose
    project, on the SAME data mount, answering where *row* says (plan §4c,
    §4g, invariant I2). The one home for every such recreate: code-embed's
    migration (foreign owner, stale image, port move) and the port move of
    VCO's own Weaviate / Ollama.

    *row* is the service's row AS IT SHOULD END: its ``port`` (and, for
    Weaviate, ``grpc_port``) is where the new container must answer.

    1. The live container's data mount (``/var/lib/weaviate``,
       ``/root/.ollama``, ``/cache``) is read (inspect). No container →
       ``not_needed``; no data mount, or a bind whose host path is
       missing/empty → ``refused``. The data inventory is read too (Weaviate's
       class list ``/v1/schema``, Ollama's model list ``/api/tags``);
       unreadable → ``refused``. The mount is
       recorded in the CURRENT row (``commit``; default
       ``service_endpoints.commit_rows``) when that row did not carry it,
       and ``infrastructure/.env`` gets the data-source knob and *row*'s
       port keys (``compose_env.write_service_keys``).
    2. BEFORE anything is stopped: the installer's effective config
       (Python merge of the files on disk + ``.env``) AND the provider's own
       ``compose config`` must both mount that same ``(kind, source,
       destination)``. Anything else → ``refused``, ``.env`` restored,
       nothing touched.
    3. A container owned by another compose project: code-embed goes
       through ``service_adoption.adopt_services(services=("code_embed",))``
       (stop → rm the container only → up under the installer → verify →
       rollback under the owning invocation) — on its CURRENT port only (the
       adoption keeps the live port, so a port change is ``refused`` until
       it runs under the installer); a Weaviate / Ollama is ``refused``
       (taking it over is the explicit ``hand-to-vco`` step, owner ruling
       Q2). One the installer already owns is re-created in
       place: ``up -d --force-recreate --no-deps <service>`` (``--build``
       for code-embed only — the others run images).
    4. AFTER the up: the container carries the installer's project label,
       its data-mount key tuple is unchanged, it publishes *row*'s port(s),
       and the service answers as itself within *health_timeout_s*
       (Weaviate: ready + gRPC reachable + the SAME class list; Ollama:
       ``/api/tags`` answering with the SAME model list; code-embed:
       ``/health`` with the model loaded). A
       failure restores ``infrastructure/.env`` and rolls back under the
       previous owner (the owning invocation for an adopted container; for
       an installer-owned one a re-up on the restored ``.env`` —
       ``--force-recreate`` when the ports moved, so it returns to them) and
       returns ``failed``.
    5. Only a verified recreate commits *row* (``commit``), which runs the
       ``apply_change`` follow-up chain (infra ``.env``, re-projection, MCP
       registration).
    """
    from vco_lib import compose_env as _compose_env  # noqa: PLC0415
    from vco_lib import containers as _containers  # noqa: PLC0415
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    service = row.service
    if service not in _se.SERVICES or row.mode != "vco_managed":
        raise ValueError("migrate_managed_service takes a vco_managed row")
    run = run or subprocess.run  # type: ignore[assignment]
    fetch = fetch or _default_fetch_json
    if tcp_open is None:
        from vco_lib.service_detection import default_tcp_open as tcp_open  # noqa: PLC0415
    probe_tcp: TcpOpenFn = tcp_open
    say: LogFn = log or print
    root = Path(root)
    infra = root / "infrastructure"
    destination = _sa.CONTAINER_MOUNT_TARGETS[service]
    rebuild = bool(build) and service == "code_embed"
    tag = f"  [{service}]"
    result = MigrationResult()
    env_state: dict[str, Any] = {"restore": None}

    def restore_env() -> str:
        """``infrastructure/.env`` back to the rows as they were: *row*'s
        ports were written for the recreate and must not outlive a refusal
        or a rollback."""
        rows_before = env_state["restore"]
        if rows_before is None:
            return ""
        try:
            _compose_env.write_service_keys(infra, rows_before, runtime=runtime)
        except (OSError, ValueError) as exc:
            return f"; infrastructure/.env could NOT be restored ({exc})"
        return ""

    def refuse(why: str) -> MigrationResult:
        result.status, result.reason = "refused", why + restore_env()
        say(f"{tag} migration refused — nothing was stopped: {result.reason}")
        return result

    # 1. identity capture ---------------------------------------------------
    ref = _find_container(row, runtime, run)
    if ref is None:
        result.lines.append(f"{tag} no container to migrate; the next compose up creates it.")
        return result
    live = _sa.live_service_state(ref, runtime, run)
    if live is None:
        return refuse(f"could not positively read container '{ref}' (inspect failed)")
    data = _mount_at(live, destination)
    if data is None:
        return refuse(
            f"container '{ref}' has no {destination} mount — its data lives inside the "
            "container, and a recreate would start with an empty one"
        )
    if data.kind == "bind" and not _bind_holds_data(data.source):
        return refuse(f"the data bind {data.source} is missing or empty on this host")
    inventory_before: Optional[frozenset[str]] = None
    live_port = _sa.live_http_host_port(service, live)
    if service in ("weaviate", "ollama"):
        inventory_before = (_service_inventory(service, int(live_port), fetch)
                            if live_port.isdecimal() else None)
        if inventory_before is None:
            return refuse(
                f"{service.capitalize()}'s {_INVENTORY_LABEL[service]} could not be read on "
                f":{live_port or '?'} — without it the recreate could not be verified"
            )
    mount = {"kind": data.kind, "source": data.source, "destination": data.destination}
    result.mount = mount
    canonical = _containers.canonical_name(service)
    try:
        db_rows = dict(_se.load_rows(db_path))
    except Exception:  # noqa: BLE001 — unreadable ⇒ no recorded rows this run
        db_rows = {}
    prior = db_rows.get(service)
    if prior is None:
        # No recorded row: the CURRENT endpoint is what the container
        # publishes — never *row*'s target port (that is what a refusal or a
        # rollback must restore away from).
        prior = row
        live_grpc = str(live.host_ports.get(_sa.WEAVIATE_GRPC_CONTAINER_PORT, ""))
        if live_port.isdecimal():
            prior = dataclasses.replace(prior, port=int(live_port))
        if service == "weaviate" and live_grpc.isdecimal():
            prior = dataclasses.replace(prior, grpc_port=int(live_grpc))
    identity_row = dataclasses.replace(prior, data_mount=mount,
                                       container_name=prior.container_name or canonical)
    new_row = dataclasses.replace(row, data_mount=mount,
                                  container_name=row.container_name or canonical)
    commit_fn = commit or _default_commit(root, db_path)
    if not _same_identity(identity_row, prior):
        try:
            commit_fn([identity_row])
        except _se.ServiceRegistryUnavailable as exc:
            say(f"{tag} WARNING: the data identity could not be recorded in launcher.db "
                f"({exc}); projecting it into infrastructure/.env for this run")
        except _se.InvalidEndpointRow as exc:
            return refuse(f"the {service} row is invalid: {exc}")
    try:
        _compose_env.write_service_keys(infra, {**db_rows, service: new_row}, runtime=runtime)
    except (OSError, ValueError) as exc:
        return refuse(f"infrastructure/.env could not carry the {service} keys: {exc}")
    env_state["restore"] = {**db_rows, service: identity_row}
    moved = live_port != str(new_row.port) or (
        service == "weaviate"
        and str(live.host_ports.get(_sa.WEAVIATE_GRPC_CONTAINER_PORT, ""))
        != str(_se.render_grpc_port(new_row)))

    # 2. pre-checks (nothing stopped yet) -----------------------------------
    if resolution is None:
        # The compose invocation + FORM decide the GPU overlay file; a caller
        # that did not resolve them gets the one resolver's answer (an
        # unresolvable runtime leaves it None: a GPU container then refuses).
        try:
            resolution = _containers.resolve()
        except Exception:  # noqa: BLE001 — a probe never fails the migration
            resolution = None
    compose_form = getattr(resolution, "compose_form", None) if resolution is not None else None
    compose_argv = [str(p) for p in (getattr(resolution, "compose", None) or [runtime, "compose"])]
    overlay = _sa.gpu_overlay_for_form(compose_form) if live.has_devices else None
    if live.has_devices and (overlay is None or not (infra / overlay).is_file()):
        return refuse(
            f"the running service uses GPU devices but no GPU overlay matches the compose "
            f"form '{compose_form}' — refusing rather than dropping the GPUs"
        )
    files, effective = _effective_mount(infra, service, destination, live.has_devices, overlay)
    if not files:
        return refuse(f"no usable compose file in {infra}")
    if effective is None or effective.key() != data.key():
        return refuse(
            f"the installer's effective config would mount {_describe_mount(effective, destination)} "
            f"at {destination}, not the live {_describe_mount(data, destination)}"
        )
    try:
        compose_text = (infra / "docker-compose.yml").read_text(encoding="utf-8")
    except OSError:
        compose_text = ""
    own_project = _containers.compose_project_name(infra, compose_text)
    chain: list[str] = []
    for path in files:
        chain += ["-f", str(path)]
    provider, why = _provider_mount(
        [*compose_argv, *chain, "-p", own_project, "--profile", "gpu", "config"],
        infra, service, destination, run, result,
    )
    if provider is None and why:
        return refuse(why)
    if provider is None or provider.key() != data.key():
        return refuse(
            f"`compose config` would mount {_describe_mount(provider, destination)} at "
            f"{destination}, not the live {_describe_mount(data, destination)}"
        )
    identity = _containers.compose_identity_of(ref, runtime, run=run)
    foreign = _containers.foreign_compose_identity(identity, own_project)
    if foreign is not None and service != "code_embed":
        return refuse(
            f"container '{ref}' {foreign} — re-creating it would take it over; "
            f"`python -m vco_lib.service_endpoints hand-to-vco --service {service}` is that "
            "explicit step"
        )
    if foreign is not None and moved:
        # The adoption keeps the live host port (its override pins it), so a
        # foreign container cannot be taken over AND moved in one recreate.
        return refuse(
            f"container '{ref}' {foreign} — it is first re-created under the installer on its "
            "current port (`python install.py --update` does that, with its cache); move it after"
        )
    described = _describe_mount(data, destination)

    def after_problem() -> str:
        after = _sa.live_service_state(ref, runtime, run)
        if after is None:
            return f"could not re-read container '{ref}' after the up"
        after_data = _mount_at(after, destination)
        if after_data is None or after_data.key() != data.key():
            return (f"the new container mounts {_describe_mount(after_data, destination)} at "
                    f"{destination}, not {described}")
        return (_published_problem(new_row, after)
                or _wait_answering(new_row, fetch, probe_tcp, inventory_before,
                                   health_timeout_s, health_interval_s))

    def commit_final() -> MigrationResult:
        final = dataclasses.replace(new_row, verified_at=int(time.time() * 1000))
        try:
            commit_fn([final])
        except (_se.ServiceRegistryUnavailable, _se.InvalidEndpointRow) as exc:
            say(f"{tag} WARNING: re-created and verified on :{final.port}, but launcher.db could "
                f"not record it ({exc}); the next `python install.py --update` records it from "
                "the running container")
        result.status = "migrated"
        result.lines.append(f"{tag} re-created under '{own_project}' on {described}, "
                            f"answering on :{final.port}.")
        return result

    # 3. recreate ----------------------------------------------------------
    if foreign is not None:  # code_embed only (see above)
        say(f"{tag} {ref} {foreign}; moving it under '{own_project}' on the same {destination}")

        def fetch_status(url: str, timeout: float) -> Optional[int]:
            return 200 if fetch(url, timeout) is not None else None

        def verify_adopted(adopted_service: str, _host_port: str) -> str:
            return after_problem() if adopted_service == service else ""

        adoption = _sa.adopt_services(
            root, services=(service,), runtime=runtime, run=run, fetch=fetch_status,
            resolution=resolution, log=say,
            build_services=(service,) if rebuild else (),
            extra_verify=verify_adopted, commit_rows=commit, db_path=db_path,
        )
        result.argv_log.extend(adoption.argv_log)
        if service in adoption.adopted:
            return commit_final()
        if service in adoption.failed:
            result.status = "failed"
            result.reason = adoption.failed[service] + restore_env()
            return result
        return refuse(adoption.refused.get(service, "the adoption did not run"))

    args, _dropped = compose_up_args([service], build=rebuild, force_recreate=True)
    up = [*compose_argv, *chain, "-p", own_project, *args]
    say(f"{tag} re-creating under '{own_project}' on {described} (:{new_row.port})")
    result.argv_log.append(list(up))
    problem = ""
    try:
        res = run(up, capture_output=True, text=True, timeout=1800, cwd=str(infra))
        if res.returncode != 0:
            tail = (res.stderr or "").strip().splitlines()
            problem = "compose up failed: " + (tail[-1] if tail else f"exit {res.returncode}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        problem = f"compose up could not run: {exc}"
    if not problem:
        after_identity = _containers.compose_identity_of(ref, runtime, run=run)
        if after_identity is None or after_identity.project != own_project:
            problem = f"container '{ref}' is not labelled project '{own_project}' after the up"
        else:
            problem = after_problem()
    if not problem:
        return commit_final()
    # Roll back under the previous owner — the installer itself — on the
    # restored .env, so the service is never left absent. When the ports
    # moved, --force-recreate brings it back to them (a provider that only
    # starts an existing container would otherwise leave it on the new port).
    env_note = restore_env()
    back_args, _ = compose_up_args([service], force_recreate=moved)
    back = [*compose_argv, *chain, "-p", own_project, *back_args]
    result.argv_log.append(list(back))
    try:
        res = run(back, capture_output=True, text=True, timeout=600, cwd=str(infra))
        rollback = "re-up under the installer project " + ("done" if res.returncode == 0 else "FAILED")
    except (subprocess.TimeoutExpired, OSError) as exc:
        rollback = f"re-up under the installer project could not run: {exc}"
    result.status, result.reason = "failed", f"{problem}; {rollback}{env_note}"
    say(f"{tag} migration FAILED: {result.reason}")
    return result


def migrate_code_embed(
    root: Path,
    row: "_se.EndpointRow",
    *,
    runtime: str = "podman",
    run: Optional[RunFn] = None,
    fetch: Optional[FetchJsonFn] = None,
    log: Optional[LogFn] = None,
    resolution: Optional[Any] = None,
    db_path: Optional[Path] = None,
    commit: Optional[Callable[[Sequence["_se.EndpointRow"]], Any]] = None,
    build: bool = True,
    health_timeout_s: float = 90.0,
    health_interval_s: float = 2.0,
) -> MigrationResult:
    """code-embed re-created with its cache (plan §4c) — install.py's entry
    when code-embed is foreign-owned or its image is stale. A thin wrapper:
    :func:`migrate_managed_service` is the one implementation."""
    if row.service != "code_embed" or row.mode != "vco_managed":
        raise ValueError("migrate_code_embed takes the code_embed row (always vco_managed)")
    return migrate_managed_service(
        root, row, runtime=runtime, run=run, fetch=fetch, log=log, resolution=resolution,
        db_path=db_path, commit=commit, build=build, health_timeout_s=health_timeout_s,
        health_interval_s=health_interval_s,
    )


# ─── the session reconcile, bounded (the session hooks' emit site) ──────

#: The whole budget the session hook gives ``reconcile --phase session``.
#: The hook itself is registered ``async`` with a 15 s timeout; the plan,
#: the inspects and any compose call must still fit after this.
SESSION_RECONCILE_TIMEOUT_S = 8.0
#: TEST-ONLY seam: a JSON argv list that replaces the reconcile command.
#: Session reconcile PROBES the canonical ports (8081, 11435, …) — a hook
#: test that ran the real one would touch whatever service listens there,
#: so the suite pins this (tests/conftest.py) to a fake.
SESSION_RECONCILE_ARGV_ENV = "VCO_SESSION_RECONCILE_ARGV"


def session_reconcile_argv() -> list[str]:
    """``python -m vco_lib.service_endpoints reconcile --phase session
    --json`` with this interpreter — or the test seam's argv."""
    override = os.environ.get(SESSION_RECONCILE_ARGV_ENV, "").strip()
    if override:
        parsed = json.loads(override)
        if not (isinstance(parsed, list) and parsed and all(isinstance(t, str) for t in parsed)):
            raise ValueError(f"{SESSION_RECONCILE_ARGV_ENV} must be a JSON list of strings")
        return parsed
    return [sys.executable, "-m", "vco_lib.service_endpoints", "reconcile", "--phase", "session",
            "--json"]


def run_session_reconcile(
    *,
    timeout_s: float = SESSION_RECONCILE_TIMEOUT_S,
    argv: Optional[Sequence[str]] = None,
    run: Optional[RunFn] = None,
) -> list[str]:
    """Run the session reconcile as a CHILD PROCESS with a hard time bound,
    and return the lines the session should see (usually none).

    A child, not an in-process call, because the bound must be enforceable:
    detection probes and ``podman ps`` can hang, and a thread cannot be
    stopped. On timeout only the direct child is killed (a grandchild — the
    MCP-registration refresh — finishes on its own). Soft-fail throughout:
    a timeout, a crash or unreadable output is one line, never an exception,
    and never blocks the hook. The reconcile itself writes its deferral
    entries (``service_endpoint_unreachable`` & co.) to UPDATE_DEFERRED.md;
    this only tells the session they exist."""
    try:
        command = list(argv) if argv is not None else session_reconcile_argv()
    except ValueError as exc:
        return [f"ensure-containers: service_endpoints reconcile not run: {exc}"]
    try:
        proc = (run or subprocess.run)(
            command, capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return [f"ensure-containers: service_endpoints reconcile did not finish within "
                f"{timeout_s:g} s — skipped this session; the next session retries it"]
    except OSError as exc:
        return [f"ensure-containers: service_endpoints reconcile could not run: {exc}"]
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        return [f"ensure-containers: service_endpoints reconcile exited {proc.returncode}"
                + (": " + " / ".join(tail) if tail else "")]
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return ["ensure-containers: service_endpoints reconcile printed no readable JSON"]
    entries = [str(e) for e in (payload.get("entries") or [])] if isinstance(payload, dict) else []
    if not entries:
        return []
    return [f"ensure-containers: service endpoints need attention: {', '.join(sorted(set(entries)))}"
            " — see UPDATE_DEFERRED.md in the orchestrator root"]


# ─── CLI ────────────────────────────────────────────────────────────────


def _cli_session_reconcile(args: argparse.Namespace) -> int:
    for line in run_session_reconcile(timeout_s=args.timeout):
        print(line)
    return 0


def _split_names(raw: Optional[str]) -> Optional[list[str]]:
    if raw is None:
        return None
    return [n for n in raw.split() if n]


def _cli_plan(args: argparse.Namespace) -> int:
    p = lifecycle_plan(_se.load_rows(args.db_path), _split_names(args.required))
    if args.json:
        print(json.dumps(p, indent=2, sort_keys=True))
    else:
        print("\n".join(lifecycle_shell_lines(p)))
    return 0


def _cli_compose_args(args: argparse.Namespace) -> int:
    try:
        up, dropped = compose_up_args(
            _split_names(args.services) or [], build=args.build, gpu_mode=args.gpu_mode,
        )
    except ValueError as exc:
        print(f"service_lifecycle: {exc}", file=sys.stderr)
        return 2
    for service in dropped:
        print(f"service_lifecycle: {service} dropped (gpu mode {args.gpu_mode})", file=sys.stderr)
    if args.json:
        print(json.dumps({"args": up, "dropped": dropped}))
    else:
        print(" ".join(shlex.quote(a) for a in up))
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.service_lifecycle",
        description="What VCO may do to the containers behind its core services (from launcher.db rows).",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_plan = sub.add_parser("plan", help="compose service list + per-container policies")
    fmt = p_plan.add_mutually_exclusive_group(required=True)
    fmt.add_argument("--shell", action="store_true", help="bash assignments (arrays)")
    fmt.add_argument("--json", action="store_true")
    p_plan.add_argument("--required", default=None,
                        help="space-separated container names (VCT_REQUIRED_CONTAINERS)")
    p_plan.add_argument("--db-path", type=Path, default=None)
    p_plan.set_defaults(handler=_cli_plan)

    p_up = sub.add_parser("compose-args", help="the `up` argv for an explicit service list")
    fmt2 = p_up.add_mutually_exclusive_group(required=True)
    fmt2.add_argument("--shell", action="store_true", help="one shell-quoted line")
    fmt2.add_argument("--json", action="store_true")
    p_up.add_argument("--services", required=True, help="space-separated compose service names")
    p_up.add_argument("--build", action="store_true")
    p_up.add_argument("--gpu-mode", default="unknown", choices=("gpu", "cpu", "unknown"))
    p_up.set_defaults(handler=_cli_compose_args)

    p_rec = sub.add_parser(
        "session-reconcile",
        help="`service_endpoints reconcile --phase session --json`, time-bounded; always exits 0")
    p_rec.add_argument("--timeout", type=float, default=SESSION_RECONCILE_TIMEOUT_S)
    p_rec.set_defaults(handler=_cli_session_reconcile)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(_main())
