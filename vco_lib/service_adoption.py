# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Guarded, mount-reconciling adoption of foreign-owned compose services.

v0.2.96 WP-4.  The field shape (this machine, 2026-09-20 survey): the three
VCO services run healthy and answer on the canonical ports, but their
containers were created by the LEGACY compose home
(``claude_mcp_servers/compose.yaml``, project ``vibecoded``), while the
installer drives ``infrastructure/docker-compose.yml`` (project
``infrastructure``).  ``install_services_guard.apply_recreate_guard``
therefore refuses every ``--force-recreate``/``--build`` for them and defers
``services_foreign_compose_identity`` — correct, but it leaves a silently
truncating 2026-05-16 ``code_embed`` image serving forever, because every
image the installer builds is tagged with the INSTALLER's project name and
the foreign container never loads it.

This module is the one-command remedy the deferral points at::

    python -m vco_lib.service_adoption adopt-services --root <install_root>

What it does, per service (weaviate first — safest, identical volume):

1. **Read ownership** from the container's own compose labels; absolutise the
   owning ``config_files`` against the owning ``working_dir`` and require
   they exist on disk (constraints survey #1: the owning side's EFFECTIVE
   config is read from the machine, never from a repo).
2. **Read the LIVE state** of the container (mounts, env, networks,
   healthcheck, restart, port bindings) via ``inspect`` — the same
   live-vs-config comparison shape as install.py's Weaviate reclaim-drift
   gate, generalized to ``{{json .Mounts}}`` exactly as the guard's Option B
   sketch describes.
3. **Reconcile**: differences between the live state and the installer's
   EFFECTIVE config (base + existing overrides + GPU overlay, merged and
   env-substituted here in Python) are carried into a generated
   ``infrastructure/compose.override.yaml`` in the ``storage_ux.rs`` shapes
   (bind per service / external volume alias), mirrored to
   ``docker-compose.override.yml`` (the C-RT-5 two-name convention).  The
   reconciliation is then VERIFIED by recomputing the effective config with
   the override present — a live BIND must survive byte-for-byte as a bind
   (never become a named volume: the 110 GB ollama models bind is shared
   with another project), a live named volume must resolve to the SAME
   volume name.  Anything that cannot be positively reconciled stays
   foreign, named per-service in the output.
4. **Adopt per service**: stop → ``rm`` the CONTAINER only (never a volume,
   never a project ``down`` — the owning network must survive for the
   services that stay) → ``compose up`` under the installer's project with
   the generated override in the ``-f`` chain → health-verify → post-verify
   (compose project label, mounts identical, ports answering).  The mixed-
   provider stale-network-label refusal on the first recreate is expected
   and handled (empty network → remove → retry once).
5. **Roll back on failure**: a failed service is re-created under the OWNING
   invocation and later services are left untouched (constraints #9).

Every podman/compose interaction goes through the injectable ``run`` seam
and every HTTP probe through ``fetch``; nothing in this module runs at
import time, and the whole flow is unit-testable without a container
runtime (tests/test_v0296_service_adoption.py).

v0.2.97 (service endpoints, plan §4c): the launcher.db ``service_endpoints``
rows replace ``services.toml``. A successful adoption WRITES the adopted
services' rows (``vco_managed``, the container, its compose project and its
observed data mount) through ``vco_lib.service_endpoints`` — it no longer
drops ``services.toml`` rows. ``services=`` limits the flow to named
services: it is automatic for code_embed only
(``service_lifecycle.migrate_code_embed``) and an explicit opt-in ("Let VCO
manage this container") for Weaviate/Ollama. A service whose data-source knob
(``compose_env.DATA_KNOBS``) is set in ``infrastructure/.env`` gets NO mount
fragment in the generated override: the knob is the one home for its data
identity, and the verification gates check the config it produces.

The ``services.toml`` IO kept here serves only the v0.2.97 importer (and the
install.py wrappers it replaces); nothing resolves an endpoint from it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

# PyYAML is NOT imported at module scope. This module's own comment used to
# say it was "a hard dep of the orchestrator venv (not the install path)" —
# and that was false: `install.py` imports this module at MODULE SCOPE
# (install.py:165), which puts it on the bootstrap path, where the venv does
# not exist yet. `install.py --bootstrap` on a fresh clone therefore died with
# ModuleNotFoundError on every platform (caught by install-smoke, v0.2.96).
#
# The three functions that actually parse or emit YAML import it locally. They
# all run long after the venv exists; the ones install.py calls during
# bootstrap (the services.toml helpers) do not touch YAML at all.

from vco_lib import containers as _containers
from vco_lib import compose_env as _compose_env
from vco_lib.atomic import atomic_write_text
from vco_lib.paths import vct_root_dir

__all__ = [
    "ADOPTION_ORDER",
    "AdoptionResult",
    "LiveServiceState",
    "MountSpec",
    "adopt_services",
    "adopted_row",
    "existing_managed_override",
    "gpu_overlay_for_form",
    "knob_owned_services",
    "live_http_host_port",
    "merge_compose",
    "owning_config_files",
    "read_services_toml",
    "render_adoption_override",
    "services_toml_path",
    "write_services_toml",
    "main",
]

#: Adoption order — constraint #14: sequence per service, never parallel,
#: weaviate first (identical named volume = safest, highest value).
ADOPTION_ORDER: tuple[str, ...] = ("weaviate", "ollama", "code_embed")

#: Container-side mount target per service (mirrors storage_ux.rs's
#: ``container_mount_for`` and the base compose files).
CONTAINER_MOUNT_TARGETS: dict[str, str] = {
    "weaviate": "/var/lib/weaviate",
    "ollama": "/root/.ollama",
    "code_embed": "/cache",
}

#: The container port each service answers HTTP on (the compose stanzas'
#: right-hand side). Weaviate also publishes gRPC 50051, so "the first
#: published port" is NOT the health port — inspect lists ``50051/tcp``
#: before ``8080/tcp``.
HTTP_CONTAINER_PORTS: dict[str, str] = {
    "weaviate": "8080",
    "ollama": "11434",
    "code_embed": "11440",
}
WEAVIATE_GRPC_CONTAINER_PORT = "50051"

#: Canonical compose volume KEY per service (the base file's ``volumes:``
#: key whose ``name:`` the installer pins — storage_ux.rs
#: ``canonical_volume_for``).
CANONICAL_VOLUME_KEYS: dict[str, str] = {
    "weaviate": "weaviate_data",
    "ollama": "ollama_data",
    "code_embed": "code_embed_cache",
}

#: Env keys whose value is behavior-critical (constraints #2): the v0.2.77
#: hook-latency fix, the code-embed tuning, the Weaviate reclaim keys (the
#: same set install.py's drift gate pins), and the Home-L-only weaviate
#: knobs the survey's table marks behavioral.
WEAVIATE_RECLAIM_ENV_KEYS: tuple[str, ...] = (
    "PERSISTENCE_LSM_MAX_SEGMENT_SIZE",
    "TOMBSTONE_DELETION_MIN_PER_CYCLE",
    "TOMBSTONE_DELETION_MAX_PER_CYCLE",
    "TOMBSTONE_DELETION_CONCURRENCY",
)
BEHAVIOR_CRITICAL_ENV_KEYS: tuple[str, ...] = (
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_KV_CACHE_TYPE",
    "CODE_EMBED_BACKEND",
    "CODE_EMBED_MODEL",
    "CODE_EMBED_MAX_CONCURRENT",
    *WEAVIATE_RECLAIM_ENV_KEYS,
    "ENABLE_API_BASED_MODULES",
    "ENABLE_MODULES",
    "DISK_USE_WARNING_PERCENTAGE",
    "DISK_USE_IMMUTABLE_PERCENTAGE",
    "DISK_USE_READONLY_PERCENTAGE",
)

#: Env resolution rule (plan-review m-5): for the behavior-critical set the
#: target value is (1) the hard canonical value when VCO's own fix names one,
#: else (2) the INSTALLER's effective config value, else (3) the live
#: container's value.  The installer's values ARE VCO's shipped fixes
#: (0.2.61/73 reclaim tuning, 0.2.77 F2 concurrency cap) — preserving a
#: foreign home's PRE-fix value would preserve a regression, exactly the
#: ``OLLAMA_KEEP_ALIVE`` trap m-5 names.  The DATA plane (mounts) stays
#: live-authoritative; env is fix-authoritative with live as the floor: a
#: key the installer does not set but the live container has is carried so
#: the service keeps behaving as it does today.
CANONICAL_ENV_TARGETS: dict[str, dict[str, str]] = {
    "ollama": {"OLLAMA_KEEP_ALIVE": "24h"},
}

#: Marker both override generators use (storage_ux.rs
#: ``is_launcher_managed_override``); an existing override WITHOUT it is
#: user-authored and must never be clobbered.
_OVERRIDE_MANAGED_MARKER = "Auto-generated by VCT"
_OVERRIDE_HEADER = (
    "# Auto-generated by VCT (v0.2.96 service adoption).\n"
    "# Reconciles services adopted from a foreign compose project: carries\n"
    "# their live bind mounts / env so the installer's compose can own them\n"
    "# WITHOUT changing what they mount or how they behave. Edits will be\n"
    "# overwritten when adoption re-runs.\n"
)
_OVERRIDE_FILES: tuple[str, ...] = (
    "compose.override.yaml",
    "docker-compose.override.yml",
)

#: The mixed-provider stale-network-label refusal (install_services_guard
#: module docstring; field 2026-09-07): "network <name> was found but has
#: incorrect label com.docker.compose.network ...".
_NETWORK_LABEL_REFUSAL_RE = re.compile(
    r"network\s+(\S+)\s+was found but has incorrect label", re.IGNORECASE
)

#: GPU overlay by COMPOSE FORM (constraints #11 — normative where the plan
#: and it disagree): the subcommand form delegates to docker-compose v2,
#: which cannot parse the CDI ``devices: [nvidia.com/gpu=all]`` spec in
#: ``podman-compose.gpu.yml``; the standalone form IS podman-compose.  The
#: chosen chain is additionally verified to parse via ``compose config``
#: before anything is stopped.
GPU_OVERLAY_BY_FORM: dict[str, str] = {
    "subcommand": "docker-compose.gpu.yml",
    "standalone": "podman-compose.gpu.yml",
}

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
LogFn = Callable[[str], None]


# ===========================================================================
# ~/.vct/services.toml IO — the ONE home (moved from install.py, v0.2.96)
# ===========================================================================

def services_toml_path() -> Path:
    """Path to ``<VCT_STATE_DIR or ~/.vct>/services.toml`` — shared with
    launcher::services::adoption (Rust). Both sides honour ``VCT_STATE_DIR``
    so a dev launcher's state stays isolated from production state."""
    return vct_root_dir() / "services.toml"


def read_services_toml(path: Optional[Path] = None) -> dict:
    """Parse ``~/.vct/services.toml`` into a list-of-tables dict.

    Returns ``{"services": [{name, mode, external_url, parallel_port}, ...]}``.
    Empty dict on missing file. Empty ``services`` list on parse error — we'd
    rather treat the file as missing than crash mid-run on a corrupted TOML
    the user might have hand-edited (with the same one-line warning
    install.py has always printed).
    """
    target = path if path is not None else services_toml_path()
    if not target.exists():
        return {"services": []}
    try:
        # tomllib is stdlib in Python 3.11+; the orchestrator requires 3.11.
        import tomllib  # noqa: PLC0415

        return tomllib.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — unreadable ⇒ treat as empty
        print(f"  ! services.toml unreadable ({e}); treating as empty")
        return {"services": []}


def toml_escape(s: str) -> str:
    """Minimal TOML basic-string escaping (backslash + double-quote).

    Sufficient for our payloads — service names, mode tokens, and URLs.
    No newlines, no control chars in any value we ever write here.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_services_toml(state: dict, *, path: Optional[Path] = None) -> None:
    """Serialize ``{services: [...]}`` to ``~/.vct/services.toml``.

    Hand-rolled TOML serializer because ``tomli_w`` isn't in the install-time
    venv. The schema is a flat array of tables — the rust launcher's
    ``AdoptionState`` shape — so a hand-rolled writer is trivial and avoids a
    chicken-and-egg dependency. Atomic via temp-file + rename so a crash
    never leaves a half-written services.toml.
    """
    target = path if path is not None else services_toml_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for entry in state.get("services", []):
        lines.append("[[services]]")
        # Order: name, mode (always present), then optional fields.
        lines.append(f'name = "{toml_escape(entry["name"])}"')
        lines.append(f'mode = "{toml_escape(entry["mode"])}"')
        if entry.get("external_url"):
            lines.append(f'external_url = "{toml_escape(entry["external_url"])}"')
        if entry.get("parallel_port") is not None:
            lines.append(f'parallel_port = {int(entry["parallel_port"])}')
        lines.append("")  # blank line between tables

    body = "\n".join(lines).rstrip() + "\n"
    # v0.2.96: the ONE atomic-write home (governance test
    # test_no_live_os_replace_outside_the_home caught two hand-rolled
    # tmp+os.replace writers here on the WP-7a successor's full sweep).
    atomic_write_text(target, body)


# ===========================================================================
# Owning invocation helpers
# ===========================================================================

def owning_config_files(identity) -> list[Path]:
    """The owning compose files, ABSOLUTISED against the owning
    ``working_dir`` (podman-compose records ``config_files`` exactly as the
    user typed them — often relative).  Empty when nothing usable is
    recorded.  Same rule :func:`vco_lib.code_embed_image.rebuild_command`'s
    identity arm applies to its printed command."""
    working_dir = (getattr(identity, "working_dir", "") or "").strip()
    config_files = (getattr(identity, "config_files", "") or "").strip()
    if not working_dir:
        return []
    out: list[Path] = []
    for part in config_files.split(","):
        part = part.strip()
        if not part:
            continue
        candidate = Path(part)
        out.append(candidate if candidate.is_absolute()
                   else Path(working_dir) / candidate)
    return out


def owning_up_argv(argv_prefix: Sequence[str], identity, service: str,
                   ) -> list[str]:
    """The ROLLBACK invocation: re-create ``service`` under the OWNING
    project (constraints #9).  Structured-argv sibling of
    ``code_embed_image.rebuild_command``'s identity arm (which prints a
    shell string for humans; this is what the adoption executes)."""
    files = owning_config_files(identity)
    project = (getattr(identity, "project", "") or "").strip()
    argv = list(argv_prefix)
    for path in files:
        argv.extend(["-f", str(path)])
    if project:
        argv.extend(["-p", project])
    argv.extend(["--profile", "gpu", "up", "-d", service])
    if files:
        return argv
    # No usable file list — pin the directory by cwd instead (the caller
    # runs with cwd=working_dir).
    return [*argv_prefix, "up", "-d", service]


# ===========================================================================
# Live container state
# ===========================================================================

@dataclass(frozen=True)
class MountSpec:
    """One mount, normalised across podman/docker inspect shapes."""

    kind: str          # "bind" | "volume"
    source: str        # host path (bind) or volume NAME (volume)
    destination: str   # container-side path
    options: str       # "Z" when SELinux relabelled, else ""

    def render_short(self) -> str:
        """The compose short syntax for this mount (storage_ux.rs shape)."""
        if self.kind == "volume":
            return f"{self.source}:{self.destination}"
        suffix = f":{self.options}" if self.options else ""
        return f"{self.source}:{self.destination}{suffix}"

    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.source, self.destination)


@dataclass(frozen=True)
class LiveServiceState:
    """What the running container actually has (inspect, read-only)."""

    mounts: tuple[MountSpec, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    networks: tuple[str, ...] = ()
    healthcheck: Optional[dict] = None
    restart: str = ""
    host_ports: dict[str, str] = field(default_factory=dict)  # cont_port → host
    has_devices: bool = False


def _inspect_json(ref: str, fmt: str, runtime: str, run: RunFn) -> Optional[Any]:
    """One ``inspect --format`` call; ``None`` on any failure (could not
    look is not a verdict)."""
    try:
        res = run([runtime, "inspect", "--type", "container", "--format", fmt, ref],
                  capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    try:
        return json.loads(res.stdout.strip())
    except ValueError:
        return None


def mount_from_inspect(entry: Any) -> Optional[MountSpec]:
    """ONE ``inspect .Mounts`` entry (podman or docker shape) →
    :class:`MountSpec`; ``None`` for anything that is not a bind or a named
    volume with a source and a destination. The one parser of that shape —
    ``service_detection`` (the candidate detector) projects it to its
    row-shaped ``Mount`` (the SELinux relabel option is not part of a data
    source's identity: :meth:`MountSpec.key`)."""
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("Type", "") or "").lower()
    if kind not in ("bind", "volume"):
        return None
    options = entry.get("Options") or []
    if isinstance(options, str):
        options = [options]
    mode = str(entry.get("Mode", "") or "")
    relabel = any("Z" in str(o) for o in options) or "Z" in mode
    source = str(entry.get("Source", "") or "")
    if kind == "volume":
        # podman/docker: Name is the volume name; Source is the mountpoint.
        source = str(entry.get("Name", "") or "") or source
    destination = str(entry.get("Destination", "") or "")
    if not source or not destination:
        return None
    return MountSpec(kind, source, destination, "Z" if relabel else "")


def live_service_state(ref: str, runtime: str, run: RunFn) -> Optional[LiveServiceState]:
    """Read the live container's mounts/env/networks/healthcheck/restart/
    ports/devices.  ``None`` when any probe fails — the caller must treat
    that as "cannot positively reconcile", never as "fine"."""
    raw_mounts = _inspect_json(ref, "{{json .Mounts}}", runtime, run)
    raw_env = _inspect_json(ref, "{{json .Config.Env}}", runtime, run)
    raw_net = _inspect_json(ref, "{{json .NetworkSettings.Networks}}", runtime, run)
    raw_hc = _inspect_json(ref, "{{json .Config.Healthcheck}}", runtime, run)
    raw_restart = _inspect_json(ref, "{{json .HostConfig.RestartPolicy}}", runtime, run)
    raw_ports = _inspect_json(ref, "{{json .HostConfig.PortBindings}}", runtime, run)
    raw_dev = _inspect_json(ref, "{{json .HostConfig.Devices}}", runtime, run)
    if not isinstance(raw_mounts, list) or not isinstance(raw_env, list):
        return None

    mounts = tuple(
        m for m in (mount_from_inspect(e) for e in raw_mounts)
        if m is not None
    )
    env: dict[str, str] = {}
    for item in raw_env:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            env[k] = v
    networks: tuple[str, ...] = tuple(sorted(raw_net)) if isinstance(raw_net, dict) else ()
    restart = ""
    if isinstance(raw_restart, dict):
        restart = str(raw_restart.get("Name", "") or "")
    host_ports: dict[str, str] = {}
    if isinstance(raw_ports, dict):
        for cont_port, bindings in raw_ports.items():
            if isinstance(bindings, list) and bindings:
                first = bindings[0]
                if isinstance(first, dict) and first.get("HostPort"):
                    host_ports[str(cont_port).split("/")[0]] = str(first["HostPort"])
    has_devices = bool(raw_dev) if isinstance(raw_dev, list) else False
    return LiveServiceState(
        mounts=mounts, env=env, networks=networks,
        healthcheck=raw_hc if isinstance(raw_hc, dict) else None,
        restart=restart, host_ports=host_ports, has_devices=has_devices,
    )


# ===========================================================================
# Compose-side: parse / substitute / merge
# ===========================================================================

_SUBST_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _substitute_str(value: str, env: dict) -> str:
    def repl(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        got = env.get(name, "")
        if got != "":
            return got
        return default if default is not None else ""

    return _SUBST_RE.sub(repl, value)


def _substitute_tree(node: Any, env: dict) -> Any:
    if isinstance(node, str):
        return _substitute_str(node, env)
    if isinstance(node, dict):
        return {k: _substitute_tree(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_tree(v, env) for v in node]
    return node


def load_compose_doc(path: Path, env: dict) -> Optional[dict]:
    """Parse one compose file with ``${VAR}`` / ``${VAR:-default}``
    substitution.  ``None`` when unreadable/unparseable."""
    import yaml  # local: see the module header — not available at bootstrap

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    return _substitute_tree(raw, env)


#: service keys whose values are LISTS that compose REPLACES (never merges)
#: when a later file redefines them.
_LIST_REPLACE_SERVICE_KEYS = ("volumes", "ports", "devices", "expose")


def merge_compose(base: dict, override: dict) -> dict:
    """Merge two compose documents with the semantics the axes compared by
    adoption actually observe: maps merge recursively, service
    ``volumes``/``ports``/``devices`` lists are REPLACED (the storage_ux
    bind shape relies on exactly that), ``environment``/``networks`` maps
    merge per key."""
    out: dict = dict(base)
    for key, value in override.items():
        if key == "services" and isinstance(value, dict) \
                and isinstance(out.get("services"), dict):
            merged = dict(out["services"])
            for svc, svc_over in value.items():
                if not isinstance(svc_over, dict):
                    merged[svc] = svc_over
                    continue
                svc_base = merged.get(svc)
                if not isinstance(svc_base, dict):
                    merged[svc] = dict(svc_over)
                    continue
                merged_svc = dict(svc_base)
                for sk, sv in svc_over.items():
                    if sk in _LIST_REPLACE_SERVICE_KEYS or not isinstance(sv, (dict, list)):
                        merged_svc[sk] = sv
                    elif isinstance(merged_svc.get(sk), dict) and isinstance(sv, dict):
                        merged_svc[sk] = {**merged_svc[sk], **sv}
                    else:
                        merged_svc[sk] = sv
                merged[svc] = merged_svc
            out["services"] = merged
        elif key in ("volumes", "networks") and isinstance(value, dict) \
                and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_compose(out[key], value)
        else:
            out[key] = value
    return out


def infrastructure_env_for_substitution(infra_dir: Path) -> dict:
    """The substitution env compose itself would see: the process env plus
    ``infrastructure/.env`` (auto-read by compose from the project dir)."""
    env = dict(os.environ)
    dot_env = infra_dir / ".env"
    if dot_env.is_file():
        for line in dot_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def gpu_overlay_for_form(compose_form: Optional[str]) -> Optional[str]:
    """The GPU overlay filename for the compose FORM actually in use —
    constraints #11 (normative): picking by runtime NAME while the
    effective provider is the other form ships an overlay the provider
    cannot parse (``podman-compose.gpu.yml``'s CDI spec under
    docker-compose v2).  The caller still verifies the chain parses."""
    if not compose_form:
        return None
    return GPU_OVERLAY_BY_FORM.get(compose_form)


def _compose_entrypoints(need_gpu: bool, gpu_overlay: Optional[str],
                         infra_dir: Path, env: dict) -> tuple[list[Path], dict]:
    """The -f chain (base + existing overrides) and its merged config, plus
    the GPU overlay appended only when the machine actually runs GPU
    containers — selection by RUNTIME EVIDENCE (live container devices, or
    devices declared in the OWNING compose files read from the machine),
    file by compose FORM (constraints #11)."""
    files: list[Path] = []
    cfg: Optional[dict] = None
    for path in [infra_dir / "docker-compose.yml"] + [
        infra_dir / name for name in _OVERRIDE_FILES
    ]:
        if not path.is_file():
            continue
        doc = load_compose_doc(path, env)
        if doc is None:
            if path.name == "docker-compose.yml":
                return [], {}  # no usable base — nothing to adopt into
            continue  # an unparseable override is skipped, not fatal
        files.append(path)
        cfg = doc if cfg is None else merge_compose(cfg, doc)
    if need_gpu and gpu_overlay:
        overlay_path = infra_dir / gpu_overlay
        doc = load_compose_doc(overlay_path, env)
        if doc is not None:
            files.append(overlay_path)
            cfg = merge_compose(cfg, doc) if cfg is not None else doc
    return files, cfg or {}


# ===========================================================================
# Reconciliation
# ===========================================================================

@dataclass
class ServicePlan:
    """The per-service verdict before anything is touched."""

    service: str
    container: str = ""
    identity: Optional[Any] = None
    reason: str = ""                 # non-empty → stays foreign
    live: Optional[LiveServiceState] = None
    override_fragments: dict = field(default_factory=dict)
    #: the OWNING compose files (read from the machine) declare GPU devices
    #: for this service — the second runtime-evidence signal for including
    #: the GPU overlay (live container devices are the first).
    owning_declares_devices: bool = False

    @property
    def adoptable(self) -> bool:
        return not self.reason


#: A compose short-form mount is ``source:target[:opts]``, and on Windows the
#: source carries its own colon: ``C:\\volumes\\ollama:/root/.ollama:Z``. A bare
#: ``split(":")`` severs the drive letter and yields source ``"C"`` with the
#: rest of the path as the TARGET — so the installer compares a mount that
#: does not exist and reports a spurious drift on every Windows install.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _split_mount_entry(entry: str) -> list[str]:
    """Split ``source:target[:opts]`` without severing a Windows drive letter."""
    parts = entry.split(":")
    # Re-join a drive letter ONLY when doing so still leaves an absolute
    # container target. That is what separates a Windows bind from a
    # one-character VOLUME name: `C:\\vol:/data` has a target after the join
    # (`/data`), while `v:/data` does not — it is volume `v` mounted at
    # `/data`, and joining it would invent the target `.ollama` from the
    # tail of the host path.
    if (
        len(parts) >= 3
        and len(parts[0]) == 1
        and parts[0].isalpha()
        and parts[1][:1] in ("\\", "/")
        and parts[2][:1] == "/"
    ):
        parts = [f"{parts[0]}:{parts[1]}", *parts[2:]]
    return parts


def _is_bind_source(source: str) -> bool:
    """Is this mount source a HOST PATH rather than a named volume?

    POSIX absolute (``/``), home-relative (``~``) and project-relative
    (``.``) — plus a Windows drive path, which is what every bind on that
    platform looks like. Without the last case a real Windows bind is
    classified as a named VOLUME, and the adoption check then looks it up in
    the top-level ``volumes:`` mapping, finds nothing, and treats the
    service as unadoptable for a reason that is not true.
    """
    return source.startswith(("/", "~", ".")) or bool(_WINDOWS_DRIVE_RE.match(source))


def config_mounts(service_cfg: dict, top_volumes: dict) -> dict[str, MountSpec]:
    """The installer-side mounts for one service, by destination.  Resolves
    named volume keys through the top-level ``volumes:`` mapping (explicit
    ``name:`` wins — the base file pins all three)."""
    out: dict[str, MountSpec] = {}
    entries = service_cfg.get("volumes") or []
    if isinstance(entries, str):
        entries = [entries]
    for entry in entries:
        kind, source, dest, opts = "volume", "", "", ""
        if isinstance(entry, str):
            parts = _split_mount_entry(entry)
            if len(parts) >= 2:
                source, dest = parts[0], parts[1]
                opts = parts[2] if len(parts) > 2 else ""
                kind = "bind" if _is_bind_source(source) else "volume"
        elif isinstance(entry, dict):
            kind = str(entry.get("type", "volume") or "volume")
            source = str(entry.get("source", "") or "")
            dest = str(entry.get("target", "") or "")
            ro = entry.get("read_only")
            opts = "ro" if ro else ""
        else:
            continue
        if not source or not dest:
            continue
        if kind == "volume":
            spec = (top_volumes.get(source) or {}) if isinstance(top_volumes, dict) else {}
            if isinstance(spec, dict):
                source = str(spec.get("name") or source)
        out[dest] = MountSpec(kind, source, dest, opts)
    return out


def _fragment_volume_key(dest: str, service: str) -> str:
    for svc, target in CONTAINER_MOUNT_TARGETS.items():
        if dest == target:
            return CANONICAL_VOLUME_KEYS[svc]
    stem = re.sub(r"[^a-z0-9_]+", "_", dest.strip("/").lower()).strip("_")
    return f"adopted_{service}_{stem or 'data'}"


def knob_owned_services(env: dict) -> set[str]:
    """Services whose data source a ``compose_env.DATA_KNOBS`` key sets in
    the substitution *env* (``infrastructure/.env`` + process env). For
    them the knob — projected from the service_endpoints row — is the ONE
    home of the data identity, so adoption carries no mount fragment."""
    return {
        service for service, pair in _compose_env.DATA_KNOBS.items()
        if any(str(env.get(key, "") or "").strip() for key in pair)
    }


def reconcile_service(service: str, live: LiveServiceState, cfg_service: dict,
                      top_volumes: dict, *, knob_owned: bool = False,
                      ) -> tuple[dict, list[str]]:
    """Plan the override fragments that make the installer's EFFECTIVE
    config match the live container on every compared axis.

    ``knob_owned``: the service's data-source knob is set, so its mounts are
    NOT carried as a fragment (plan §4c.6 — one concern, one home: a mount
    stated in two files could disagree). The verification gates then judge
    the knob's config as-is, and refuse if it differs from the live mounts.

    Returns ``(fragments, refusals)``.  ``refusals`` is ALWAYS EMPTY as of
    WP-4 review MINOR-2 — reconciliation here is total by construction
    (binds survive as binds, volumes alias, env/healthcheck/restart/ports/
    networks carry the live values), so this function has no unreconcilable
    case to name.  The refusal duty belongs to the VERIFICATION gates
    (``_mount_problems`` run on the fragments-simulated config and again on
    the rendered override): they name the exact difference that cannot be
    positively reconciled — including the cases this function cannot model
    (an exotic live mount kind falls through as a volume mismatch and is
    refused there) — and a service with any refusal stays foreign
    (constraints #2/#3/#6).  The channel is kept (not deleted) so a future
    in-function refusal has one home to land in."""
    fragments: dict = {"mounts": [], "volume_aliases": {}, "environment": {},
                       "healthcheck": None, "restart": "", "ports": [],
                       "networks": {}}
    refusals: list[str] = []

    config_mounts_by_dest = config_mounts(cfg_service, top_volumes)
    live_dests = {m.destination for m in live.mounts}
    final_entries: list[str] = []
    aliases: dict[str, str] = {}
    mounts_differ = False

    for live_mount in live.mounts:
        dest = live_mount.destination
        current = config_mounts_by_dest.get(dest)
        if live_mount.kind == "bind":
            # A live BIND must survive byte-for-byte as a bind — never a
            # named volume (the 110 GB shared ollama models bind).
            final_entries.append(live_mount.render_short())
            if (current is None or current.kind != "bind"
                    or current.source != live_mount.source
                    or current.options != live_mount.options):
                mounts_differ = True
        else:
            key = _fragment_volume_key(dest, service)
            current_name = (current.source
                            if current is not None and current.kind == "volume"
                            else "")
            if current_name != live_mount.source:
                aliases[key] = live_mount.source
                mounts_differ = True
            final_entries.append(f"{key}:{dest}")

    # A base-declared mount the LIVE container does not have is carried as
    # ABSENT by the same volumes-list replacement (lists replace on merge,
    # so the override's list is the final list) — the live state is the
    # behavior to preserve, and the final-mounts verifier below proves it.
    if set(config_mounts_by_dest) - live_dests:
        mounts_differ = True

    if knob_owned:
        mounts_differ = False
        aliases = {}
    if mounts_differ:
        fragments["mounts"] = sorted(final_entries)

    # Env (see CANONICAL_ENV_TARGETS): canonical fix value, else the
    # installer's effective value, else the live container's value.
    canonical_for_service = CANONICAL_ENV_TARGETS.get(service, {})
    for key in BEHAVIOR_CRITICAL_ENV_KEYS:
        effective = str((cfg_service.get("environment") or {}).get(key, "") or "")
        canonical = canonical_for_service.get(key)
        target = canonical if canonical is not None else (effective
                                                          or live.env.get(key, ""))
        if target and str(target) != effective:
            fragments["environment"][key] = str(target)

    # Healthcheck presence (model_router's depends_on gates on it).
    if live.healthcheck is not None and not cfg_service.get("healthcheck"):
        test = live.healthcheck.get("Test")
        if isinstance(test, list) and test:
            hc: dict[str, Any] = {"test": test}
            for src, dst, scale in (
                ("Interval", "interval", 1e9), ("Timeout", "timeout", 1e9),
                ("StartPeriod", "start_period", 1e9),
            ):
                raw = live.healthcheck.get(src)
                if isinstance(raw, (int, float)) and raw > 0:
                    hc[dst] = f"{max(1, int(raw // scale))}s"
            retries = live.healthcheck.get("Retries")
            if isinstance(retries, int) and retries > 0:
                hc["retries"] = retries
            fragments["healthcheck"] = hc

    # Restart policy.
    if live.restart and str(cfg_service.get("restart", "") or "") != live.restart:
        fragments["restart"] = live.restart

    # Host ports: carry the live mapping when the effective config maps a
    # container port to a different host port.
    cfg_ports = cfg_service.get("ports") or []
    if isinstance(cfg_ports, str):
        cfg_ports = [cfg_ports]
    effective_ports: dict[str, str] = {}
    for entry in cfg_ports:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2:
                host, cont = parts[0], parts[-1].split("/")[0]
                effective_ports[cont] = host
        elif isinstance(entry, dict):
            cont = str(entry.get("target", "") or "").split("/")[0]
            host = str(entry.get("published", "") or "")
            if cont and host:
                effective_ports[cont] = host
    for cont_port, live_host in live.host_ports.items():
        if effective_ports.get(cont_port, live_host) != live_host:
            fragments["ports"].append(f"{live_host}:{cont_port}")

    # Networks: keep a leg on every network the live container is on, with
    # the service-name alias — the model_router DNS dependency.
    for net in live.networks:
        fragments["networks"][net] = None  # alias = bare service name

    fragments["volume_aliases"] = aliases
    return fragments, refusals


def _mount_problems(final_service: dict, final_top_volumes: dict,
                    live: LiveServiceState, service: str) -> list[str]:
    """The hard data-plane gate's COMPARISON CORE: require the mounts of an
    arbitrary FINAL effective service config to match the live ones exactly
    (binds byte-for-byte; named volumes by resolved name; nothing added the
    live container does not have).  Called on the fragments-simulated config
    (below) and again on the RENDERED override merged into the base chain
    (:func:`adopt_services`) — the docstring's "verified by recomputing the
    effective config with the override present" promise is the second call."""
    final = config_mounts(final_service, final_top_volumes)
    live_by_dest = {m.destination: m for m in live.mounts}
    problems: list[str] = []
    for dest, live_mount in live_by_dest.items():
        got = final.get(dest)
        if got is None:
            problems.append(f"mount {dest} lost by the reconciled config")
        elif got.kind != live_mount.kind:
            problems.append(
                f"mount {dest}: live {live_mount.kind} would become {got.kind} "
                f"(named-volume replacement attempt — refused)"
            )
        elif got.kind == "bind" and (got.source, got.options) != (live_mount.source, live_mount.options):
            problems.append(
                f"bind {dest}: live {live_mount.source}:{live_mount.options} would "
                f"become {got.source}:{got.options}"
            )
        elif got.kind == "volume" and got.source != live_mount.source:
            problems.append(
                f"volume {dest}: live {live_mount.source} would become {got.source}"
            )
    for dest in set(final) - set(live_by_dest):
        problems.append(
            f"mount {dest} absent live would be ADDED by the reconciled config"
        )
    return problems


def _verify_final_mounts(fragments: dict, cfg_service: dict, top_volumes: dict,
                         live: LiveServiceState, service: str, *,
                         knob_owned: bool = False) -> list[str]:
    """The hard data-plane gate, fragments side: recompute the effective
    config WITH the planned fragments and require the mounts to match the
    live ones exactly.  This is one gate the plan's red-proof mutates —
    accepting a mismatch here must fail the refusal test, not pass
    silently.  A ``knob_owned`` service has no mount fragment: its config
    is judged exactly as the knob renders it."""
    if knob_owned:
        return _mount_problems(cfg_service, top_volumes, live, service)
    simulated = dict(cfg_service)
    volume_entries = []
    for m in live.mounts:
        if m.kind == "bind":
            volume_entries.append(m.render_short())
        else:
            key = _fragment_volume_key(m.destination, service)
            volume_entries.append(f"{key}:{m.destination}")
    simulated["volumes"] = volume_entries
    top = dict(top_volumes)
    for key, name in (fragments.get("volume_aliases") or {}).items():
        top[key] = {"external": True, "name": name}
    return _mount_problems(simulated, top, live, service)


# ===========================================================================
# Override rendering (storage_ux.rs shapes)
# ===========================================================================

def existing_managed_override(infra_dir: Path) -> Optional[dict]:
    """The parsed managed override already in *infra_dir* (the first of the
    two auto-load names that exists and carries the managed marker), or
    ``None``. A user-authored file is never read as ours."""
    import yaml  # local: see the module header — not available at bootstrap

    for name in _OVERRIDE_FILES:
        target = infra_dir / name
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return None
        if _OVERRIDE_MANAGED_MARKER not in text:
            return None
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        return doc if isinstance(doc, dict) else None
    return None


def render_adoption_override(plans: Sequence[ServicePlan], *,
                             preserve: Optional[dict] = None,
                             replacing: Sequence[str] = ()) -> str:
    """Render ``infrastructure/compose.override.yaml`` for the adoptable
    plans — bind/external-alias/healthcheck/env stanzas per service in the
    storage_ux.rs generator shapes, plus the network-preservation block.

    ``preserve`` (the existing managed override) keeps the stanzas of every
    service NOT in ``replacing`` — a ``services=``-limited run (the
    code_embed migration) must not erase what an earlier adoption carried
    for the others. A replaced service's stanza and its canonical volume
    alias come only from this run's plan."""
    services_block: dict[str, Any] = {}
    networks_block: dict[str, Any] = {}
    volumes_block: dict[str, Any] = {}
    if isinstance(preserve, dict):
        replaced = set(replacing)
        replaced_keys = {CANONICAL_VOLUME_KEYS[s] for s in replaced if s in CANONICAL_VOLUME_KEYS}
        kept_services: dict[str, Any] = preserve.get("services") or {}
        kept_networks: dict[str, Any] = preserve.get("networks") or {}
        kept_volumes: dict[str, Any] = preserve.get("volumes") or {}
        for kept_name, stanza in kept_services.items():
            if kept_name not in replaced and isinstance(stanza, dict) and stanza:
                services_block[str(kept_name)] = stanza
        for net, spec in kept_networks.items():
            networks_block[str(net)] = spec
        for key, spec in kept_volumes.items():
            if key not in replaced_keys and not str(key).startswith(
                    tuple(f"adopted_{s}_" for s in replaced)):
                volumes_block[key] = spec
    for plan in plans:
        if not plan.adoptable or plan.live is None:
            continue
        frag = plan.override_fragments
        svc: dict[str, Any] = {}
        if frag.get("mounts"):
            svc["volumes"] = sorted(frag["mounts"])
        if frag.get("environment"):
            svc["environment"] = {
                k: v for k, v in sorted(frag["environment"].items())
                if v is not None
            }
        if frag.get("healthcheck"):
            svc["healthcheck"] = frag["healthcheck"]
        if frag.get("restart"):
            svc["restart"] = frag["restart"]
        if frag.get("ports"):
            svc["ports"] = sorted(frag["ports"])
        nets: dict[str, Any] = {}
        for net in sorted((frag.get("networks") or {})):
            nets[net] = {"aliases": [plan.service]}
            networks_block[net] = {"external": True}
        if nets:
            svc["networks"] = {"default": {}, **nets}
        if svc:
            services_block[plan.service] = svc
        for key, name in sorted((frag.get("volume_aliases") or {}).items()):
            volumes_block[key] = {"external": True, "name": name}
    body = {"services": services_block or {}}
    body["networks"] = networks_block or {}
    body["volumes"] = volumes_block or {}
    import yaml  # local: see the module header — not available at bootstrap

    text = yaml.safe_dump(body, default_flow_style=False, sort_keys=True)
    return _OVERRIDE_HEADER + "\n" + text


def write_adoption_override(infra_dir: Path, body: str) -> Optional[str]:
    """Write the override ATOMICALLY to BOTH auto-load names (the C-RT-5
    convention).  REFUSES (returns the reason) when an existing override is
    user-authored (no managed marker) — never clobber a user's file."""
    for name in _OVERRIDE_FILES:
        target = infra_dir / name
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            if existing.strip() and _OVERRIDE_MANAGED_MARKER not in existing:
                return (f"{target} exists and does not carry the managed-marker "
                        f"header — refusing to overwrite a user-authored override")
    for name in _OVERRIDE_FILES:
        target = infra_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, body)
    return None


# ===========================================================================
# Health probes
# ===========================================================================

FetchFn = Callable[[str, float], Optional[int]]

HEALTH_PATHS: dict[str, str] = {
    "weaviate": "/v1/.well-known/ready",
    "ollama": "/api/tags",
    "code_embed": "/health",
}


def _default_fetch(url: str, timeout: float) -> Optional[int]:
    from vco_lib.service_probe_http import open_probe  # noqa: PLC0415

    try:
        with open_probe(url, timeout) as resp:  # a redirect is an error, never followed
            return int(resp.status)
    except Exception:  # noqa: BLE001 — every failure is "not answering (yet)"
        return None


def live_http_host_port(service: str, live: Optional[LiveServiceState]) -> str:
    """The host port *live* publishes for *service*'s HTTP container port,
    else its first published port that is not Weaviate's gRPC one, else
    ``""``."""
    if live is None:
        return ""
    wanted = live.host_ports.get(HTTP_CONTAINER_PORTS.get(service, ""))
    if wanted:
        return str(wanted)
    for cont, host in live.host_ports.items():
        if not (service == "weaviate" and cont == WEAVIATE_GRPC_CONTAINER_PORT):
            return str(host)
    return ""


def host_port_for(service: str, plans: Sequence[ServicePlan],
                  defaults: Optional[dict[str, int]] = None) -> str:
    """The adopted host port for ``service`` — from the live state when
    known (the adopted container keeps its ports), else the canonical
    default."""
    for plan in plans:
        if plan.service == service and plan.live is not None:
            port = live_http_host_port(service, plan.live)
            if port:
                return port
    defaults = defaults or {"weaviate": 8081, "ollama": 11435, "code_embed": 11440}
    return str(defaults.get(service, 0))


def wait_healthy(service: str, host_port: str, fetch: FetchFn,
                 timeout_s: float = 90.0, interval_s: float = 2.0) -> bool:
    """Poll the service's canonical health endpoint until it answers 200."""
    url = f"http://localhost:{host_port}{HEALTH_PATHS[service]}"
    deadline = time.monotonic() + timeout_s
    while True:
        if fetch(url, 2.0) == 200:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


# ===========================================================================
# The adoption flow
# ===========================================================================

@dataclass
class AdoptionResult:
    adopted: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    #: every podman/compose argv executed, in order — the audit trail tests
    #: assert against (never a volume subcommand, never a project down).
    argv_log: list[list[str]] = field(default_factory=list)


def _run_logged(argv: list[str], run: RunFn, result: AdoptionResult, **kw):
    result.argv_log.append(list(argv))
    return run(argv, **kw)




def _collateral_warning(plans: Sequence[ServicePlan], runtime: str, run: RunFn,
                        log: LogFn) -> None:
    """Constraint #4: enumerate every OTHER container in the owning project
    — the services that stay (model_router, neo4j) — and warn that their
    service-name DNS aliases are preserved by the generated override."""
    identities = [p.identity for p in plans if p.identity is not None]
    if not identities:
        return
    project = getattr(identities[0], "project", "")
    if not project:
        return
    try:
        res = run(
            [runtime, "ps", "-a", "--format", "{{.Names}}",
             "--filter", f"label={_containers.COMPOSE_PROJECT_LABEL}={project}"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return
    if res.returncode != 0:
        return
    adopted_names = {p.container for p in plans}
    others = [n for n in (res.stdout or "").split() if n and n not in adopted_names]
    if others:
        log(
            f"  [collateral] the owning project '{project}' still runs: "
            + ", ".join(others)
        )
        log(
            "  [collateral] their service-name DNS aliases on the shared "
            "network are preserved by the generated override."
        )


def _network_label_handling(argv: list[str], runtime: str, run: RunFn,
                             result: AdoptionResult, stderr: str) -> bool:
    """The expected mixed-provider refusal on the first recreate: when
    compose refuses because a network carries another tool's labels, and
    that network provably has NO containers attached, remove it (a network,
    never a volume, never a project) and let the caller retry once."""
    m = _NETWORK_LABEL_REFUSAL_RE.search(stderr or "")
    if not m:
        return False
    network = m.group(1)
    try:
        res = _run_logged(
            [runtime, "network", "inspect", network],
            run, result, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if res.returncode != 0:
        return False
    try:
        info = json.loads((res.stdout or "").strip())
    except ValueError:
        return False
    entries = info if isinstance(info, list) else [info]
    attached = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cont = entry.get("Containers") or entry.get("containers") or {}
        if isinstance(cont, dict):
            attached += len(cont)
        elif isinstance(cont, list):
            attached += len(cont)
    if attached:
        return False  # somebody is on it — never remove
    try:
        rm = _run_logged([runtime, "network", "rm", network], run, result,
                         capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return rm.returncode == 0


def owning_service_config(identity) -> Optional[dict]:
    """The OWNING home's merged ``services:`` mapping, read from the machine
    (labels → working_dir + config_files — read-only evidence, constraints
    #1/#7: the owning files are never written).  ``None`` when the owning
    side cannot be parsed; the caller then falls back to the other runtime
    evidence instead of guessing."""
    files = owning_config_files(identity)
    working_dir = (getattr(identity, "working_dir", "") or "").strip()
    if not files or not working_dir:
        return None
    env = dict(os.environ)
    dot_env = Path(working_dir) / ".env"
    if dot_env.is_file():
        for line in dot_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    cfg: Optional[dict] = None
    for path in files:
        doc = load_compose_doc(path, env)
        if doc is None:
            return None
        cfg = doc if cfg is None else merge_compose(cfg, doc)
    services = (cfg or {}).get("services")
    return services if isinstance(services, dict) else None


def _declares_devices(service_cfg: dict) -> bool:
    """Does a compose service stanza declare GPU devices (CDI ``devices:``
    or ``deploy.resources.reservations.devices``)?"""
    if service_cfg.get("devices"):
        return True
    deploy = service_cfg.get("deploy")
    if isinstance(deploy, dict):
        resources = deploy.get("resources")
        if isinstance(resources, dict):
            reservations = resources.get("reservations")
            if isinstance(reservations, dict) and reservations.get("devices"):
                return True
    return False


def _plan_all(root: Path, runtime: str, run: RunFn, log: LogFn,
              resolution: Optional[Any] = None,
              services: Sequence[str] = ADOPTION_ORDER,
              container_refs: Optional[Mapping[str, str]] = None,
              ) -> tuple[list[ServicePlan], list[Path]]:
    """Read-only phase: per-service verdicts for *services* (in
    :data:`ADOPTION_ORDER`; a service not named is not planned, so it can
    never be touched).  Nothing is stopped here.

    ``container_refs``: service → the EXACT container to plan (``hand-to-vco``
    passes the adopted row's ``container_name``). A named container is never
    substituted by a canonically-named one — a stale ``vco_weaviate`` from an
    earlier install must not be taken over in place of the container the row
    says VCO uses — and a named container that does not exist refuses."""
    unknown = [s for s in services if s not in ADOPTION_ORDER]
    if unknown:
        raise ValueError(f"not adoptable services: {unknown} (known: {ADOPTION_ORDER})")
    infra_dir = root / "infrastructure"
    compose_file = infra_dir / "docker-compose.yml"
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except OSError:
        compose_text = ""
    own_project = _containers.compose_project_name(infra_dir, compose_text)

    compose_argv: list[str] = [runtime, "compose"]
    compose_form: Optional[str] = None
    if resolution is not None:
        if getattr(resolution, "compose", None):
            compose_argv = [str(p) for p in resolution.compose]
        compose_form = getattr(resolution, "compose_form", None)

    env = infrastructure_env_for_substitution(infra_dir)
    knob_owned = knob_owned_services(env)
    plans: list[ServicePlan] = []
    for service in (s for s in ADOPTION_ORDER if s in services):
        plan = ServicePlan(service=service)
        plans.append(plan)
        if container_refs is not None and service in container_refs:
            ref = container_refs[service] or None
            if not ref:
                plan.reason = "no container named — nothing to adopt"
                continue
            container_id = _inspect_json(ref, "{{json .Id}}", runtime, run)
            if not (isinstance(container_id, str) and container_id):
                plan.reason = f"container '{ref}' does not exist (or could not be inspected)"
                continue
        else:
            try:
                ref = _containers.find_existing_container(service, runtime)
            except Exception:  # noqa: BLE001 — probe failure → nothing to adopt
                ref = None
        if not ref:
            plan.reason = "no existing container — nothing to adopt"
            continue
        plan.container = ref
        try:
            identity = _containers.compose_identity_of(ref, runtime, run=run)
        except Exception:  # noqa: BLE001
            identity = None
        plan.identity = identity
        own_reason = _containers.foreign_compose_identity(identity, own_project)
        if own_reason is None:
            plan.reason = "already owned by this install's compose project"
            continue
        # Constraint #1: the owner's files must exist on disk — else there
        # is nothing to compare against and nothing to roll back to.
        owning_files = owning_config_files(identity) if identity is not None else []
        if identity is None or not owning_files:
            plan.reason = (
                "owning compose identity could not be read positively "
                f"({own_reason}) — refusing rather than guess"
            )
            continue
        if not all(p.is_file() for p in owning_files):
            plan.reason = (
                f"owning config files missing on disk "
                f"({', '.join(str(p) for p in owning_files)})"
            )
            continue
        live = live_service_state(ref, runtime, run)
        if live is None or not live.mounts:
            plan.reason = "could not positively read the live container's state"
            continue
        plan.live = live
        owning_services = owning_service_config(identity) or {}
        plan.owning_declares_devices = _declares_devices(
            owning_services.get(service) or {}
        )

    # GPU evidence: live container devices, or the OWNING files (machine
    # evidence) declaring devices for the service.  Either signal selects
    # the overlay; the FILE is chosen by compose FORM (constraints #11).
    gpu_services = [p for p in plans
                    if p.live is not None
                    and (p.live.has_devices or p.owning_declares_devices)]
    gpu_overlay = gpu_overlay_for_form(compose_form) if gpu_services else None
    overlay_missing = gpu_overlay is None or not (infra_dir / gpu_overlay).is_file()
    if gpu_services and overlay_missing:
        for plan in gpu_services:
            plan.reason = (
                f"the running service uses GPU devices but no overlay matches "
                f"the compose form '{compose_form}' — refusing rather than "
                f"silently dropping the GPUs"
            )
    files, cfg = _compose_entrypoints(bool(gpu_services) and not overlay_missing,
                                      gpu_overlay, infra_dir, env)
    top_volumes = cfg.get("volumes") or {}
    services_cfg = cfg.get("services") or {}

    # Verify the chosen chain (incl. the form-chosen overlay) PARSES under
    # the actual provider before anything is stopped (constraints #11).
    if files and gpu_overlay in {str(p.name) for p in files}:
        probe = [*compose_argv]
        for path in files:
            probe.extend(["-f", str(path)])
        probe.append("config")
        try:
            res = run(probe, capture_output=True, text=True, timeout=60)
            parses = res.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            parses = False
        if not parses:
            for plan in plans:
                if plan.live is not None and (plan.live.has_devices
                                              or plan.owning_declares_devices):
                    plan.reason = (
                        f"the compose-form GPU overlay ({gpu_overlay}) does not "
                        f"parse under the actual provider — refusing"
                    )
                    plan.live = None

    for plan in plans:
        if plan.live is None:
            continue
        cfg_service = services_cfg.get(plan.service) or {}
        owned_by_knob = plan.service in knob_owned
        fragments, refusals = reconcile_service(
            plan.service, plan.live, cfg_service, top_volumes,
            knob_owned=owned_by_knob,
        )
        mount_problems = _verify_final_mounts(fragments, cfg_service, top_volumes,
                                              plan.live, plan.service,
                                              knob_owned=owned_by_knob)
        refusals.extend(mount_problems)
        if refusals:
            plan.reason = "; ".join(sorted(set(refusals)))
            plan.override_fragments = {}
        else:
            plan.override_fragments = fragments
    return plans, files


def _up_under_installer(compose_argv: list[str], files: Sequence[Path],
                        project: str, service: str, runtime: str, run: RunFn,
                        result: AdoptionResult, timeout: int = 900,
                        build: bool = False) -> tuple[bool, str]:
    """``compose up -d [--build] --no-deps <service>`` under the installer's
    project, with the generated override in the -f chain.  Handles the
    mixed-provider stale-network-label refusal (empty network → rm → ONE
    retry).  ``build``: rebuild the image (code_embed, whose image is built
    from the checkout — a stale one is one reason to migrate it)."""
    argv = list(compose_argv)
    for path in files:
        argv.extend(["-f", str(path)])
    argv.extend(["-p", project, "--profile", "gpu", "up", "-d"])
    if build:
        argv.append("--build")
    argv.extend(["--no-deps", service])
    try:
        res = _run_logged(argv, run, result, capture_output=True, text=True,
                          timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "compose up timed out"
    except OSError as exc:
        return False, f"compose could not run: {exc}"
    if res.returncode == 0:
        return True, ""
    stderr = res.stderr or ""
    if "incorrect label" in stderr and "com.docker.compose.network" in stderr:
        if _network_label_handling(argv, runtime, run, result, stderr):
            try:
                res = _run_logged(argv, run, result, capture_output=True,
                                  text=True, timeout=timeout)
                if res.returncode == 0:
                    return True, ""
            except (subprocess.TimeoutExpired, OSError):
                pass
    return False, (stderr.strip().splitlines() or ["compose up failed"])[-1]


def _rollback_under_owner(plan: ServicePlan, compose_argv: Sequence[str],
                          runtime: str, run: RunFn, result: AdoptionResult) -> str:
    """Constraints #9: re-create the failed service under the OWNING
    invocation (data plane untouched — binds and named volumes live outside
    containers)."""
    identity = plan.identity
    working_dir = (getattr(identity, "working_dir", "") or "").strip()
    argv = owning_up_argv(compose_argv, identity, plan.service)
    cwd = working_dir or None
    try:
        res = _run_logged(argv, run, result, capture_output=True, text=True,
                          cwd=cwd, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"rollback under the owning project failed to run: {exc}"
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        return "rollback under the owning project FAILED: " + (
            tail[-1] if tail else f"exit {res.returncode}"
        )
    return ""


ExtraVerifyFn = Callable[[str, str], str]


def _post_verify(plan: ServicePlan, own_project: str, runtime: str, run: RunFn,
                 fetch: FetchFn, extra_verify: Optional[ExtraVerifyFn] = None) -> str:
    """Constraint #10: containers now labeled OUR project; mounts identical
    to the pre-adoption live mounts; the health endpoint answering; and
    ``extra_verify(service, host_port)`` (a caller's stricter check — the
    code_embed migration's "model loaded") returning no problem."""
    ref = plan.container
    try:
        identity = _containers.compose_identity_of(ref, runtime, run=run)
    except Exception:  # noqa: BLE001
        identity = None
    if identity is None or identity.project != own_project:
        return f"container '{ref}' is not labeled project '{own_project}' after up"
    live = live_service_state(ref, runtime, run)
    if live is None:
        return "could not re-read the adopted container's state"
    before = {m.destination: m.key() for m in (plan.live.mounts if plan.live else ())}
    after = {m.destination: m.key() for m in live.mounts}
    if before != after:
        return f"mounts changed across the adoption: {before} → {after}"
    host_port = live_http_host_port(plan.service, plan.live)
    if host_port and not wait_healthy(plan.service, host_port, fetch,
                                      timeout_s=90.0, interval_s=2.0):
        # WP-4 review MINOR-1: the verification gate's job is to REFUSE a
        # bad adoption, and a 2 s window manufactured failures on
        # slow-starting services — a false refusal whose rollback then
        # collides on the pinned container name. The module default
        # (90 s, the same budget the up-path health waits use) is the
        # honest window; a genuinely unhealthy service still refuses,
        # just after actually being given a chance.
        return f"health endpoint did not answer on :{host_port}"
    return ""


CommitRowsFn = Callable[[Sequence[Any]], Any]


def adopted_row(service: str, plan: ServicePlan, own_project: str, *,
                prior: Optional[Any], source: str, now_ms: int) -> Any:
    """The ``service_endpoints`` row an adoption leaves: ``vco_managed``,
    the container it now owns, the installer's compose project, the live
    data mount, and the ports the container keeps. A prior row keeps its
    scheme/enabled/autostart/confirmation."""
    from vco_lib import service_endpoints as _se  # noqa: PLC0415

    live = plan.live
    port_text = live_http_host_port(service, live)
    port = int(port_text) if port_text.isdigit() else (
        prior.port if prior is not None else _se.DEFAULT_PORTS[service])
    grpc: Optional[int] = None
    if service == "weaviate":
        grpc_text = (live.host_ports.get(WEAVIATE_GRPC_CONTAINER_PORT) if live else None) or ""
        grpc = int(grpc_text) if grpc_text.isdigit() else _se.render_grpc_port(prior)
    mount = None
    for m in (live.mounts if live is not None else ()):
        if m.destination == CONTAINER_MOUNT_TARGETS.get(service):
            mount = {"kind": m.kind, "source": m.source, "destination": m.destination}
    return _se.EndpointRow(
        service=service, mode="vco_managed", port=port, grpc_port=grpc,
        scheme=prior.scheme if prior is not None else _se.DEFAULT_SCHEME,
        host=prior.host if prior is not None and prior.host in ("localhost", "127.0.0.1")
        else _se.DEFAULT_HOST,
        container_name=plan.container, compose_project=own_project, data_mount=mount,
        enabled=prior.enabled if prior is not None else True,
        autostart=prior.autostart if prior is not None else True,
        confirmed_by_user=prior.confirmed_by_user if prior is not None else False,
        source=source, verified_at=now_ms,
    )


def _record_adopted_rows(root: Path, plans: Sequence[ServicePlan], adopted: Sequence[str],
                         own_project: str, *, commit_rows: Optional[CommitRowsFn],
                         db_path: Optional[Path], source: str, log: LogFn) -> None:
    """Write the adopted services' rows (the one writer,
    ``vco_lib.service_endpoints``), which runs the follow-up chain. A
    registry that is not there is a WARNING, never a failed adoption: the
    containers are already moved and verified, and the next install run
    re-derives the rows from them."""
    from vco_lib import service_endpoints as _se  # noqa: PLC0415

    try:
        prior = _se.load_rows(db_path)
    except Exception:  # noqa: BLE001 — unreadable ⇒ no prior row
        prior = {}
    now_ms = int(time.time() * 1000)
    rows = [adopted_row(p.service, p, own_project, prior=prior.get(p.service),
                        source=source, now_ms=now_ms)
            for p in plans if p.service in adopted]
    if not rows:
        return
    try:
        if commit_rows is not None:
            commit_rows(rows)
        else:
            _se.commit_rows(rows, orchestrator_root=root, db_path=db_path, out=log)
        log(f"  [adopt] recorded {', '.join(r.service for r in rows)} as VCO-managed "
            "in launcher.db (service_endpoints).")
    except (_se.ServiceRegistryUnavailable, _se.InvalidEndpointRow, OSError) as exc:
        log(f"  [adopt] WARNING: the service_endpoints rows could not be written ({exc}); "
            "the next `python install.py --update` records them from the running containers.")


def adopt_services(
    root: Path,
    *,
    services: Sequence[str] = ADOPTION_ORDER,
    runtime: str = "podman",
    run: Optional[RunFn] = None,
    fetch: Optional[FetchFn] = None,
    resolution: Optional[Any] = None,
    log: Optional[LogFn] = None,
    dry_run: bool = False,
    adopt_outcome_cb=None,
    build_services: Sequence[str] = (),
    extra_verify: Optional[ExtraVerifyFn] = None,
    commit_rows: Optional[CommitRowsFn] = None,
    db_path: Optional[Path] = None,
    row_source: str = "user_cli",
    container_refs: Optional[Mapping[str, str]] = None,
) -> AdoptionResult:
    """The whole guarded adoption.  See the module docstring for the flow
    and the constraint map.  ``run``/``fetch``/``resolution`` are injection
    seams (tests drive the entire flow without a container runtime);
    ``adopt_outcome_cb(plans, override_body)`` lets a caller observe the
    plan before execution (the CLI's --dry-run prints from it).

    ``services``: which services to adopt (default: all three, the explicit
    opt-in). Services not named are never planned, stopped or re-created,
    and their stanzas in an existing managed override are preserved.
    ``build_services``: rebuild these images at the up (code_embed).
    ``extra_verify(service, host_port)``: a stricter post-check whose
    problem triggers the same rollback. On success the adopted services'
    ``service_endpoints`` rows are written through ``commit_rows`` (default
    ``service_endpoints.commit_rows`` on ``db_path``) with ``row_source``.
    ``container_refs``: service → the exact container to adopt (see
    :func:`_plan_all`); without it the canonical/historical names are
    searched (the v0.2.96 bulk adoption)."""
    run = run or subprocess.run  # type: ignore[assignment]
    fetch = fetch or _default_fetch
    log = log or (lambda msg: print(msg))
    root = Path(root)
    result = AdoptionResult()

    plans, files = _plan_all(root, runtime, run, log, resolution=resolution,
                             services=services, container_refs=container_refs)
    if not files:
        result.refused = {p.service: p.reason or "unknown" for p in plans
                          if p.reason}
        result.lines.append("  [adopt] no usable compose chain — nothing done.")
        return result

    adoptable = [p for p in plans if p.adoptable]
    for plan in plans:
        if plan.reason:
            log(f"  [adopt:{plan.service}] stays foreign — {plan.reason}")
            result.refused[plan.service] = plan.reason
    if not adoptable:
        result.lines.append("  [adopt] nothing to adopt.")
        return result

    # Constraint #4: collateral warning (model_router / neo4j stay).
    _collateral_warning(plans, runtime, run, log)

    infra_dir = root / "infrastructure"
    override_body = render_adoption_override(
        plans, preserve=existing_managed_override(infra_dir),
        replacing=[p.service for p in plans],
    )
    if adopt_outcome_cb is not None:
        adopt_outcome_cb(plans, override_body)

    # The docstring's verification promise, RENDERED side: recompute the
    # effective config exactly as the up's -f chain will see it (base + the
    # GENERATED override text, which REPLACES the managed override files)
    # and re-run the hard data-plane gate against THAT — not just against
    # the plan fragments. A renderer bug (or an alias that cannot survive
    # rendering) refuses the service here, before anything is written or
    # stopped, instead of silently replacing a live mount at the up.
    env = infrastructure_env_for_substitution(infra_dir)
    cfg = None
    managed_names = set(_OVERRIDE_FILES)
    for path in files:
        if path.name in managed_names and path.parent == infra_dir:
            continue  # replaced by override_body below
        doc = load_compose_doc(path, env)
        if doc is None:
            continue
        cfg = doc if cfg is None else merge_compose(cfg, doc)
    # ABOVE the try, not inside it: the `except` clause names `yaml`, so an
    # import that failed in the try would leave the handler referencing an
    # unbound name and raise NameError instead of the error it exists to
    # catch. (pyright: reportPossiblyUnbound — the same shape this cycle
    # already fixed twice in install.py.)
    import yaml  # local: see the module header

    try:
        rendered = yaml.safe_load(override_body)
    except yaml.YAMLError:
        rendered = None
    cfg = merge_compose(cfg or {}, rendered if isinstance(rendered, dict) else {})
    rendered_top = cfg.get("volumes") or {}
    for plan in list(adoptable):
        if plan.live is None:  # unreachable for adoptable plans; keeps the types honest
            continue
        problems = _mount_problems(
            (cfg.get("services") or {}).get(plan.service) or {},
            rendered_top, plan.live, plan.service,
        )
        if problems:
            plan.reason = "; ".join(sorted(set(problems)))
            adoptable.remove(plan)
            result.refused[plan.service] = plan.reason
            log(f"  [adopt:{plan.service}] stays foreign — {plan.reason}")
    if not adoptable:
        result.lines.append("  [adopt] nothing adoptable after the rendered-"
                            "config verification — nothing touched.")
        return result

    if dry_run:
        result.lines.append("  [adopt] dry run — override NOT written, no"
                            " container touched.")
        return result

    refusal = write_adoption_override(infra_dir, override_body)
    if refusal:
        for plan in adoptable:
            result.refused[plan.service] = refusal
        result.lines.append(f"  [adopt] REFUSED: {refusal}")
        return result
    log(f"  [adopt] wrote {infra_dir / 'compose.override.yaml'} (+ sibling)")
    # ``files`` was computed BEFORE the override existed — the up's -f chain
    # must now include the freshly written override (an explicit -f chain
    # disables compose's auto-load, so omitting it would run the up without
    # the reconciliation we just verified).
    written = [infra_dir / name for name in _OVERRIDE_FILES]
    for path in written:
        if path not in files:
            files.append(path)

    # Constraint #13: the substitution keys must be present before the up.
    try:
        embed_config = _derive_embed_config()
        ok, msg = _compose_env.write_infrastructure_env(infra_dir, embed_config)
        if not ok:
            log(f"  [adopt] WARNING: could not refresh infrastructure/.env: {msg}")
    except Exception as exc:  # noqa: BLE001 — never block adoption on env write
        log(f"  [adopt] WARNING: infrastructure/.env refresh failed: {exc}")

    try:
        compose_text = (infra_dir / "docker-compose.yml").read_text(encoding="utf-8")
    except OSError:
        compose_text = ""
    own_project = _containers.compose_project_name(infra_dir, compose_text)
    compose_argv = [runtime, "compose"]
    if resolution is not None and getattr(resolution, "compose", None):
        compose_argv = [str(p) for p in resolution.compose]

    # Constraint #14: per-service, sequential, weaviate first — downtime is
    # real and bounded to one service at a time.
    for plan in adoptable:
        service, ref = plan.service, plan.container
        log(f"  [adopt:{service}] stop → remove container → up under "
            f"'{own_project}' → verify")
        try:
            res = _run_logged([runtime, "stop", ref], run, result,
                              capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"stop exited {res.returncode}")
            res = _run_logged([runtime, "rm", ref], run, result,
                              capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"rm exited {res.returncode}")
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            rollback_msg = _rollback_under_owner(plan, compose_argv, runtime, run, result)
            detail = f"{exc}" + (f"; {rollback_msg}" if rollback_msg else "")
            result.failed[service] = detail
            log(f"  [adopt:{service}] FAILED ({detail}) — rolled back under the "
                f"owning project; later services untouched.")
            break
        ok, err = _up_under_installer(compose_argv, files, own_project, service,
                                      runtime, run, result,
                                      build=service in build_services)
        if not ok:
            rollback_msg = _rollback_under_owner(plan, compose_argv, runtime, run, result)
            detail = err + (f"; {rollback_msg}" if rollback_msg else "")
            result.failed[service] = detail
            log(f"  [adopt:{service}] FAILED ({detail}) — rolled back under the "
                f"owning project; later services untouched.")
            break
        problem = _post_verify(plan, own_project, runtime, run, fetch, extra_verify)
        if problem:
            # The installer's container now holds the pinned name: remove
            # IT (the container only — its mount lives outside it) so the
            # owning invocation can re-create the original.
            for step in ([runtime, "stop", ref], [runtime, "rm", ref]):
                try:
                    _run_logged(step, run, result, capture_output=True, text=True, timeout=120)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            rollback_msg = _rollback_under_owner(plan, compose_argv, runtime, run, result)
            detail = problem + (f"; {rollback_msg}" if rollback_msg else "")
            result.failed[service] = detail
            log(f"  [adopt:{service}] FAILED ({detail}) — rolled back under the "
                f"owning project; later services untouched.")
            break
        result.adopted.append(service)
        log(f"  [adopt:{service}] adopted.")

    if result.adopted:
        # v0.2.97: the adopted services are VCO-managed now — say so in the
        # one store every lifecycle surface reads (superseding the v0.2.96
        # services.toml row drop, constraint #12).
        _record_adopted_rows(root, plans, result.adopted, own_project,
                             commit_rows=commit_rows, db_path=db_path,
                             source=row_source, log=log)
        result.lines.append(
            "  [adopt] done. The next `python install.py --update` now reaches "
            "these services (and rebuilds code_embed under the installer's "
            "image name)."
        )
    return result


def _derive_embed_config() -> dict:
    """Minimal embed config for ``compose_env.write_infrastructure_env``:
    GPU vendor from the host probe (the ONE home), backend accordingly."""
    vendor = ""
    try:
        from vco_lib import gpu_device as _gpu_device

        gpus = _gpu_device.enumerate_gpus()
        if gpus:
            vendor = str(getattr(gpus[0], "vendor", "") or "")
    except Exception:  # noqa: BLE001 — probe failure ⇒ no substitution keys
        vendor = ""
    return {"gpu_vendor": vendor, "code_backend": "gpu" if vendor else "ollama"}


# ===========================================================================
# CLI
# ===========================================================================

def _main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — CLI
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.service_adoption",
        description=(
            "Guarded, mount-reconciling adoption of foreign-owned compose "
            "services into the installer's compose project."
        ),
    )
    parser.add_argument("command", choices=["adopt-services"])
    parser.add_argument("--root", default=None,
                        help="install root (default: this checkout)")
    parser.add_argument("--runtime", default="podman",
                        help="container runtime (default: podman)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan + override, touch nothing")
    parser.add_argument("--service", action="append", choices=list(ADOPTION_ORDER),
                        default=None,
                        help="adopt only this service (repeatable; default: all three)")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    from vco_lib import containers as _c

    resolution: Optional[Any] = None
    try:
        resolution = _c.resolve()
    except Exception:  # noqa: BLE001 — probe never fails the command
        resolution = None
    result = adopt_services(root, services=tuple(args.service or ADOPTION_ORDER),
                            runtime=args.runtime, resolution=resolution,
                            dry_run=args.dry_run)
    for line in result.lines:
        print(line)
    if result.failed:
        return 1
    return 0


main = _main

if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(_main())
