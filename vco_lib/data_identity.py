# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.data_identity — prove a recreate keeps the service's data.

v0.2.100 (AD-4; review L1-F01/F08/F20/F21/F24, U11/U12/U19). A container
recreate (``compose up --force-recreate``, a zombie ``rm`` + ``up``, a port
move) replaces the container; its data survives only when the NEW container
mounts the same host path / named volume at the service's data destination
(``/var/lib/weaviate``, ``/root/.ollama``, ``/cache``). Compose decides that
mount from ``infrastructure/.env``'s data-source knobs, which are projected
from the ``service_endpoints`` row's ``data_mount``; a row whose mount was
NULL made the knob vanish, and the next recreate re-homed the service onto
compose's EMPTY default volume (on the owner's machine: 110 GB of Ollama
models nearly orphaned).

This module is the ONE home of that proof, called by every recreate path —
install.py step 5 (:func:`guard_compose_set`), the session hooks' zombie
branch and missing-container create (``python -m vco_lib.service_lifecycle
up``), and ``service_lifecycle.migrate_managed_service``:

* :func:`capture_live_mount` — the running container's data mount (inspect,
  read-only), telling "no such container" apart from "could not look";
* :func:`effective_compose_mount` — the mount compose WOULD give the service
  (``compose config``, read-only — the provider's own render);
* :func:`recreate_preserves_data` — the pure verdict;
* :func:`guard_recreate` — capture, RECORD the captured mount on the row and
  project it into ``infrastructure/.env`` (so compose sees it), then compare
  with the effective mount. It records BEFORE it can answer ``PRESERVES``.

Verdicts: ``PRESERVES`` (compose mounts exactly the data), ``NOTHING_TO_LOSE``
(no container and no recorded mount), ``REFUSE_UNKNOWN`` (something could not
be read), ``REFUSE_DIFFERENT`` (compose would mount something else, or the
recorded mount is not the live one). A refusal is never followed by an
``rm``: the caller leaves the container alone and records
``service_recreate_refused_data_unknown``.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from vco_lib import containers as _containers
from vco_lib import service_adoption as _sa
from vco_lib import service_endpoints as _se
from vco_lib import tool_search_dirs as _tsd
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "PRESERVES",
    "NOTHING_TO_LOSE",
    "REFUSE_UNKNOWN",
    "REFUSE_DIFFERENT",
    "CID_RECREATE_REFUSED",
    "LiveMount",
    "Effective",
    "GuardResult",
    "capture_live_mount",
    "effective_compose_mount",
    "recreate_preserves_data",
    "guard_recreate",
    "guard_compose_set",
    "mount_key",
]

PRESERVES = "preserves"
NOTHING_TO_LOSE = "nothing_to_lose"
REFUSE_UNKNOWN = "refuse_unknown"
REFUSE_DIFFERENT = "refuse_different"

CID_RECREATE_REFUSED = "service_recreate_refused_data_unknown"

LIVE_PRESENT = "present"
LIVE_ABSENT = "absent"
LIVE_UNKNOWN = "unknown"

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
LogFn = Callable[[str], None]

#: podman ("Error: no such container x") and docker ("Error: No such
#: container: x" / "No such object: x"). Anything else from a failed inspect
#: (daemon down, permission) is "could not look", never "absent".
_NO_SUCH = re.compile(r"\bno such (?:container|object)\b", re.IGNORECASE)


def destination(service: str) -> str:
    """The container-side data path of *service*."""
    return _sa.CONTAINER_MOUNT_TARGETS[service]


def mount_key(mount: Any) -> Optional[tuple[str, str, str]]:
    """``(kind, source, destination)`` of a row mount (dict) or a
    :class:`~vco_lib.service_adoption.MountSpec` — the data identity (an
    SELinux relabel option is not part of it)."""
    if mount is None:
        return None
    if isinstance(mount, Mapping):
        return (str(mount.get("kind") or ""), str(mount.get("source") or ""),
                str(mount.get("destination") or ""))
    return (str(mount.kind), str(mount.source), str(mount.destination))


def describe(mount: Any) -> str:
    key = mount_key(mount)
    return "no data mount" if key is None else f"{key[0]} {key[1]}"


def _as_row_mount(mount: Any) -> Optional[dict]:
    key = mount_key(mount)
    return None if key is None else {"kind": key[0], "source": key[1], "destination": key[2]}


# ─── the live mount ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveMount:
    """What the runtime says is mounted at the service's data destination.
    ``state``: ``present`` (a container exists — ``mount`` is its data mount,
    ``None`` when it has none), ``absent`` (no container under any of the
    names), ``unknown`` (the runtime could not be asked)."""

    state: str
    ref: str = ""
    mount: Optional[dict] = None
    detail: str = ""


def container_names(service: str, row: Optional[_se.EndpointRow] = None) -> list[str]:
    """The containers a recreate of *service* would replace: the row's
    container, then the name VCO's compose creates."""
    names: list[str] = []
    for name in ((row.container_name if row is not None else None) or "",
                 _containers.canonical_name(service)):
        if name and name not in names:
            names.append(name)
    return names


def capture_live_mount(service: str, runtime: str, *, run: Optional[RunFn] = None,
                       names: Optional[Sequence[str]] = None) -> LiveMount:
    """The data mount of *service*'s existing container (inspect, read-only)."""
    _run = run or _tsd.run
    dest = destination(service)
    unknown: list[str] = []
    for name in names or container_names(service):
        try:
            res = _run([runtime, "inspect", "--type", "container", "--format", "{{.Id}}", name],
                       capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError) as exc:
            unknown.append(f"{name}: {exc}")
            continue
        if res.returncode == 0 and (res.stdout or "").strip():
            raw = _sa._inspect_json(name, "{{json .Mounts}}", runtime, _run)
            if not isinstance(raw, list):
                return LiveMount(LIVE_UNKNOWN, name, None,
                                 f"the mounts of container '{name}' could not be read")
            for entry in raw:
                spec = _sa.mount_from_inspect(entry)
                if spec is not None and spec.destination == dest:
                    return LiveMount(LIVE_PRESENT, name, _as_row_mount(spec))
            return LiveMount(LIVE_PRESENT, name, None)
        if res.returncode == 0 or _NO_SUCH.search(res.stderr or ""):
            continue  # no such container under this name
        tail = (res.stderr or "").strip().splitlines()
        unknown.append(f"{name}: {tail[-1] if tail else f'inspect exited {res.returncode}'}")
    if unknown:
        return LiveMount(LIVE_UNKNOWN, detail="; ".join(unknown))
    return LiveMount(LIVE_ABSENT)


# ─── the effective compose mount ────────────────────────────────────────


@dataclass(frozen=True)
class Effective:
    """The mount compose would give the service. ``error`` non-empty: the
    render could not be read (``mount`` is then meaningless)."""

    mount: Optional[dict] = None
    error: str = ""
    argv: tuple[str, ...] = ()


#: The base compose file, by precedence: the installer's, then the names a
#: legacy compose home (``claude_mcp_servers/``) uses.
_BASE_NAMES = ("docker-compose.yml", "compose.yaml", "compose.yml")


def compose_files(infra_dir: Path) -> list[Path]:
    """The ``-f`` chain VCO's compose runs: the base file plus every override
    present (``install_services_guard.override_f_chain`` — an explicit chain
    disables compose's own override auto-load, so the overrides must be named)."""
    from vco_lib import install_services_guard as _guard  # noqa: PLC0415

    infra = Path(infra_dir)
    base = next((infra / n for n in _BASE_NAMES if (infra / n).is_file()), infra / _BASE_NAMES[0])
    chain = _guard.override_f_chain(infra)
    return [base, *(Path(chain[i + 1]) for i in range(0, len(chain), 2))]


def effective_compose_mount(infra_dir: Path, service: str, compose_argv: Sequence[str], *,
                            files: Optional[Sequence[Path]] = None,
                            project: Optional[str] = None,
                            run: Optional[RunFn] = None) -> Effective:
    """``<compose> -f … -p <project> --profile gpu config`` (read-only) and the
    data mount it renders for *service*."""
    infra = Path(infra_dir)
    chain: list[str] = []
    for path in (files if files is not None else compose_files(infra)):
        chain += ["-f", str(path)]
    proj = project if project is not None else _containers.own_compose_project(infra.parent)
    argv = [*compose_argv, *chain, *(["-p", proj] if proj else []), "--profile", "gpu", "config"]
    _run = run or _tsd.run
    try:
        res = _run(argv, capture_output=True, text=True, timeout=120, cwd=str(infra))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Effective(error=f"`compose config` could not run: {exc}", argv=tuple(argv))
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        return Effective(error="`compose config` failed: "
                         + (tail[-1] if tail else f"exit {res.returncode}"), argv=tuple(argv))
    try:
        import yaml  # noqa: PLC0415 — venv-time only (see service_adoption's header)
    except ImportError:
        return Effective(error="PyYAML is not importable in this interpreter (venv not active?)",
                         argv=tuple(argv))
    try:
        doc = yaml.safe_load(res.stdout or "")
    except yaml.YAMLError as exc:
        return Effective(error=f"`compose config` output is not YAML: {exc}", argv=tuple(argv))
    if not isinstance(doc, dict):
        return Effective(error="`compose config` printed no configuration", argv=tuple(argv))
    service_cfg = (doc.get("services") or {}).get(service)
    if not isinstance(service_cfg, dict):
        return Effective(error=f"`compose config` has no {service} service", argv=tuple(argv))
    mounts = _sa.config_mounts(service_cfg, doc.get("volumes") or {})
    return Effective(_as_row_mount(mounts.get(destination(service))), argv=tuple(argv))


# ─── the verdict (pure) ─────────────────────────────────────────────────


def _identity(service: str, row: Optional[_se.EndpointRow],
              live: LiveMount) -> tuple[Optional[str], str, Optional[dict]]:
    """``(early verdict or None, reason, the mount that must survive)``."""
    dest = destination(service)
    recorded = _as_row_mount(row.data_mount) if row is not None and row.data_mount else None
    if live.state == LIVE_UNKNOWN:
        return REFUSE_UNKNOWN, (f"the {service} container could not be inspected "
                                f"({live.detail or 'runtime did not answer'})"), None
    if live.state == LIVE_PRESENT:
        if live.mount is None:
            return REFUSE_UNKNOWN, (f"container '{live.ref}' has no {dest} mount — its data "
                                    "lives inside the container and a recreate would drop it"), None
        if recorded is not None and mount_key(recorded) != mount_key(live.mount):
            return REFUSE_DIFFERENT, (
                f"launcher.db records {describe(recorded)} at {dest}, but container "
                f"'{live.ref}' mounts {describe(live.mount)} — which one holds the data is "
                "not known"), None
        return None, "", dict(live.mount)
    if recorded is not None:
        return None, "", recorded
    return NOTHING_TO_LOSE, (f"no {service} container and no recorded data mount — "
                             "nothing to lose"), None


def recreate_preserves_data(service: str, row: Optional[_se.EndpointRow], live: LiveMount,
                            effective: Optional[Effective]) -> tuple[str, str]:
    """``(verdict, reason)`` for recreating *service* (module doc). Pure."""
    early, why, target = _identity(service, row, live)
    if early is not None:
        return early, why
    dest = destination(service)
    if effective is None or effective.error:
        return REFUSE_UNKNOWN, ((effective.error if effective else "the effective compose "
                                 "config was not read") + f" — the {dest} mount compose would "
                                f"use is not known (the data is {describe(target)})")
    if mount_key(effective.mount) != mount_key(target):
        return REFUSE_DIFFERENT, (f"`compose config` would mount {describe(effective.mount)} at "
                                  f"{dest}, not {describe(target)} where the data is")
    return PRESERVES, f"compose mounts {describe(target)} at {dest} — the data comes along"


# ─── the guard ──────────────────────────────────────────────────────────


@dataclass
class GuardResult:
    service: str
    verdict: str
    reason: str
    live: LiveMount = field(default_factory=lambda: LiveMount(LIVE_ABSENT))
    mount: Optional[dict] = None
    effective: Optional[Effective] = None
    #: the captured mount was written to launcher.db by this guard
    recorded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict in (PRESERVES, NOTHING_TO_LOSE)

    def deferral_entry(self, *, manual_cmd: str = "python install.py --update") -> DeferralEntry:
        return DeferralEntry(
            condition_id=CID_RECREATE_REFUSED,
            title=f"{self.service}: recreate refused — its data location is not proven",
            detected=f"{self.service}: {self.reason}.",
            why_deferred=("Recreating the container could start it on a different (possibly "
                          "empty) volume and orphan the data; VCO did not remove or recreate it."),
            command_to_apply=(
                "python -m vco_lib.service_endpoints show\n"
                f"podman inspect --format '{{{{json .Mounts}}}}' {self.live.ref or '<container>'}\n"
                "# make infrastructure/.env's data knob name the mount that holds the data "
                "(`python -m vco_lib.service_endpoints reconcile --phase update` records a "
                f"running container's mount), then re-run:\n{manual_cmd}"),
            severity="warning",
        )


Recorder = Callable[[_se.EndpointRow], Any]
EnvWriter = Callable[[Path, Mapping[str, _se.EndpointRow]], Any]


def _default_record(db_path: Optional[Path]) -> Recorder:
    def record(row: _se.EndpointRow) -> Any:
        return _se.write_rows([row], db_path=db_path)
    return record


def _default_write_env(runtime: str) -> EnvWriter:
    def write(infra: Path, rows: Mapping[str, _se.EndpointRow]) -> Any:
        from vco_lib import compose_env as _compose_env  # noqa: PLC0415

        return _compose_env.write_service_keys(infra, rows, runtime=runtime)
    return write


def guard_recreate(
    service: str,
    *,
    runtime: str,
    infra_dir: Path,
    compose_argv: "Sequence[str] | Callable[[], Sequence[str]]",
    row: Optional[_se.EndpointRow] = None,
    rows: Optional[Mapping[str, _se.EndpointRow]] = None,
    record_row: Optional[_se.EndpointRow] = None,
    record: Optional[Recorder] = None,
    write_env: Optional[EnvWriter] = None,
    db_path: Optional[Path] = None,
    run: Optional[RunFn] = None,
    compose_run: Optional[RunFn] = None,
    names: Optional[Sequence[str]] = None,
    files: Optional[Sequence[Path]] = None,
    project: Optional[str] = None,
) -> GuardResult:
    """May *service*'s container be (re)created without losing its data?

    *row* is the row compose will realise (its ports, and its ``data_mount``
    = the recorded mount); *record_row* the row the captured mount is stamped
    on (default *row*); *rows* the other services' rows for the ``.env``
    projection. Order: capture → early verdict → RECORD the live mount when
    the row lacks it → project the knob into ``infrastructure/.env`` →
    ``compose config`` → verdict. Nothing is stopped or removed here.
    *compose_argv* may be a callable: it is resolved only when ``compose
    config`` actually has to run."""
    infra = Path(infra_dir)
    live = capture_live_mount(service, runtime, run=run, names=names or container_names(service, row))
    early, why, target = _identity(service, row, live)
    result = GuardResult(service, early or "", why, live=live, mount=target)
    if early is not None:
        return result
    notes = result.notes
    if row is not None and live.mount is not None and mount_key(row.data_mount) != mount_key(live.mount):
        stamped = replace(record_row or row, data_mount=dict(live.mount), source="live_reconcile")
        try:
            (record or _default_record(db_path))(stamped)
            result.recorded = True
        except _se.ServiceRegistryUnavailable as exc:
            notes.append(f"the data mount could not be recorded in launcher.db ({exc})")
        except _se.InvalidEndpointRow as exc:
            result.verdict, result.reason = REFUSE_UNKNOWN, f"the {service} row is invalid: {exc}"
            return result
    if row is not None and row.mode == "vco_managed":
        projected = {**dict(rows or {}), service: replace(row, data_mount=target)}
        try:
            (write_env or _default_write_env(runtime))(infra, projected)
        except (OSError, ValueError) as exc:
            result.verdict, result.reason = REFUSE_UNKNOWN, (
                f"infrastructure/.env could not carry the {service} data knob: {exc}")
            return result
    argv = compose_argv() if callable(compose_argv) else compose_argv
    result.effective = effective_compose_mount(infra, service, argv, files=files,
                                               project=project, run=compose_run or run)
    effective_row = replace(row, data_mount=target) if row is not None else None
    if effective_row is None:
        # No row (registry unavailable): the target is the live mount itself.
        effective_row = _se.EndpointRow(service=service, mode="vco_managed", port=_se.DEFAULT_PORTS[service],
                                        source="live_reconcile", data_mount=target,
                                        grpc_port=_se.DEFAULT_WEAVIATE_GRPC_PORT
                                        if service == "weaviate" else None)
    result.verdict, result.reason = recreate_preserves_data(service, effective_row, live,
                                                            result.effective)
    return result


def guard_compose_set(
    services: Iterable[str],
    *,
    runtime: str,
    infra_dir: Path,
    compose_argv: "Sequence[str] | Callable[[], Sequence[str]]",
    rows: Mapping[str, _se.EndpointRow],
    compose_file: Optional[Path] = None,
    deferral_report: Any = None,
    log_event: Optional[Callable[..., None]] = None,
    run: Optional[RunFn] = None,
    out: LogFn = print,
) -> set[str]:
    """install.py step 5: the services of *services* compose may NOT touch.
    One :func:`guard_recreate` per named service (``--force-recreate`` applies
    to every named service, and compose recreates a stopped one whose config
    changed); each refusal prints why, is ledgered and logged."""
    refused: set[str] = set()
    project = _containers.compose_project_of(compose_file) if compose_file else None
    for service in dict.fromkeys(services):
        g = guard_recreate(service, runtime=runtime, infra_dir=infra_dir,
                           compose_argv=compose_argv, row=rows.get(service), rows=rows,
                           run=run, project=project)
        for note in g.notes:
            out(f"  [data] {service}: {note}")
        if g.ok:
            if g.recorded:
                out(f"  [data] {service}: recorded its live data mount ({describe(g.mount)})")
            continue
        refused.add(service)
        out(f"  [refuse-recreate] {service}: {g.reason}. Left as it is — nothing removed.")
        if deferral_report is not None:
            deferral_report.add_entry(g.deferral_entry())
        if log_event is not None:
            log_event("5/10", "refuse-recreate", g.reason,
                      data={"service": service, "verdict": g.verdict})
    return refused
