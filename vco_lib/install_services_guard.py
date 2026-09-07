# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Step-5 guards for ``install.py``'s service start (v0.2.93).

Two rules learned in the field on 2026-09-07 (dogfood update to v0.2.92, every
service healthy and adopted, `install.py --update` dead at step 5/10 with the
manifest already bumped and hooks/hub/MCP/KG-seed/schema steps never run):

1. **Never ``--force-recreate`` (or ``--build`` for) a container another
   compose identity created.** v0.2.92 taught step 5 to recreate adopted
   vct-managed services so a changed compose config / rebuilt image reaches
   the running container. The dogfood machine's containers had been created
   from the legacy ``claude_mcp_servers/compose.yaml`` (project ``vibecoded``);
   compose under project ``infrastructure`` refused with a stale-network-label
   error and would otherwise have collided on the container name — and a
   "successful" recreate would have silently moved the user's bind-mounted
   volumes into fresh named ones. Compose stamps the creating project on every
   container; :func:`apply_recreate_guard` reads it first and leaves foreign
   containers exactly as they are, with a ledger row naming the two honest
   options. Conservative on every probe failure: not positively ours → not ours.

2. **A failed ``compose up`` must not kill an ``--update`` nothing depends
   on.** When every required service already answers, record the failure and
   continue; keep the hard stop for fresh installs or a required service down.

Kept out of ``install.py`` on purpose (the monolith ratchet + the "one concern,
one home" rule); ``install.py`` holds two thin call sites.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Optional

from vco_lib import containers as _containers
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "foreign_owned_services",
    "emit_foreign_compose_identity_deferral",
    "apply_recreate_guard",
    "compose_failure_is_survivable",
    "emit_compose_up_failed_deferral",
    "print_compose_failure_hints",
    "compose_failure_followup",
]

LogEvent = Callable[..., None]

CID_FOREIGN_IDENTITY = "services_foreign_compose_identity"
CID_COMPOSE_UP_FAILED = "services_compose_up_failed"


def foreign_owned_services(
    services: list[str], runtime: str, infra_dir: Path, compose_file: Path,
) -> dict[str, str]:
    """Which of ``services`` have an existing container ANOTHER compose
    identity created (svc → reason). Read-only; any probe that cannot
    positively read the container as ours counts it as NOT ours."""
    if not services:
        return {}
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except OSError:
        compose_text = ""
    own = _containers.compose_project_name(infra_dir, compose_text)
    out: dict[str, str] = {}
    for svc in dict.fromkeys(services):
        try:
            ref = _containers.find_existing_container(svc, runtime)
        except Exception:  # noqa: BLE001 — unknown alias / probe error → nothing to protect
            ref = None
        if not ref:
            continue  # no container exists — compose creates it fresh
        why = _containers.foreign_compose_identity(
            _containers.compose_identity_of(ref, runtime), own,
        )
        if why:
            out[svc] = f"container '{ref}' {why}"
    return out


def emit_foreign_compose_identity_deferral(
    deferral_report, foreign: dict[str, str], runtime: str, infra_dir: Path,
) -> None:
    """Ledger row for the services step 5 refused to recreate."""
    if deferral_report is None or not foreign:
        return
    names = " ".join(sorted(foreign))
    detail = "\n".join(f"  - {svc}: {why}" for svc, why in sorted(foreign.items()))
    deferral_report.add_entry(
        DeferralEntry(
            condition_id=CID_FOREIGN_IDENTITY,
            title=(
                f"{len(foreign)} running service(s) belong to another compose "
                "project — left untouched"
            ),
            detected=(
                f"Step 5 wanted to run `compose up --force-recreate` for {names} "
                "so the current compose config (and any rebuilt code_embed image) "
                "reaches the running container(s), but their labels say another "
                f"compose identity created them:\n{detail}\n"
                f"The installer drives `{infra_dir}`; under that project compose "
                "is either refused (network-label mismatch) or collides on the "
                "container name, and either way the whole update used to stop here."
            ),
            why_deferred=(
                "Recreating a container another compose project owns is not the "
                "installer's to do: it silently changes the volume layout that "
                "project chose and strands that project's own state. The services "
                "keep running exactly as they are; only the compose tuning and the "
                "image rebuild did NOT reach them."
            ),
            command_to_apply=(
                "# Option A — keep the current ownership and apply the change there:\n"
                "#   cd <working dir shown above>\n"
                f"#   {runtime} compose -f <config files shown above> up -d --force-recreate {names}\n"
                "# Option B — hand these services to the installer's compose (so future\n"
                "# updates apply cleanly). FIRST confirm every data volume / bind mount\n"
                "# is identical in both compose files:\n"
                f"#   {runtime} inspect <container> --format '{{{{json .Mounts}}}}'   # vs infrastructure/docker-compose.yml\n"
                "# then stop and remove ONLY those containers (never the volumes) and re-run:\n"
                "#   python install.py --update\n"
                "# Or silence this entry once decided:\n"
                "#   python -m vco_lib.project_init dismiss-deferral --folder . "
                f"--condition-id {CID_FOREIGN_IDENTITY}"
            ),
            severity="warning",
            kg_node_refs=[],
        )
    )


def apply_recreate_guard(
    *,
    services_to_recreate: list[str],
    recreate_for_rebuild: list[str],
    build_services: list[str],
    runtime: str,
    infra_dir: Path,
    compose_file: Path,
    deferral_report,
    log_event: LogEvent,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """Drop foreign-owned services from the three compose lists.

    Prints one ``[skip-recreate]`` line per service, writes the ledger row and
    the install event. Returns the filtered lists plus the foreign map (empty
    when nothing was foreign — the lists come back unchanged).
    """
    foreign = foreign_owned_services(
        services_to_recreate, runtime, infra_dir, compose_file,
    )
    if not foreign:
        return services_to_recreate, recreate_for_rebuild, build_services, {}
    for svc, why in sorted(foreign.items()):
        print(f"  [skip-recreate] {svc}: {why}")
    print(
        "      Left running as-is; the compose tuning / image rebuild did not "
        f"reach it. See UPDATE_DEFERRED.md ({CID_FOREIGN_IDENTITY})."
    )
    emit_foreign_compose_identity_deferral(deferral_report, foreign, runtime, infra_dir)
    log_event(
        "5/10", "skip-recreate",
        "running container(s) owned by another compose identity — not recreated",
        data={"services": dict(foreign)},
    )
    return (
        [s for s in services_to_recreate if s not in foreign],
        [s for s in recreate_for_rebuild if s not in foreign],
        [s for s in build_services if s not in foreign],
        foreign,
    )


def compose_failure_is_survivable(
    args: argparse.Namespace, detected: Optional[dict], has_gpu: bool,
) -> bool:
    """Pure: may step 5 continue after ``compose up`` failed?

    Only on ``--update``, and only when every REQUIRED service already answers
    (weaviate + ollama, plus code_embed on GPU hosts). A fresh install, or any
    required service down, keeps the hard stop. ``args.update`` must be the
    boolean ``True`` — a truthy stand-in (e.g. a Mock) never unlocks this.
    """
    if getattr(args, "update", False) is not True:
        return False
    required = ["weaviate_url", "ollama_url"]
    if has_gpu:
        required.append("code_embed_url")
    return all(bool((detected or {}).get(k)) for k in required)


def emit_compose_up_failed_deferral(
    deferral_report, exit_code: int, stderr_tail: str, manual_cmd: str,
) -> None:
    if deferral_report is None:
        return
    deferral_report.add_entry(
        DeferralEntry(
            condition_id=CID_COMPOSE_UP_FAILED,
            title="`compose up` failed during the update — services kept as they were",
            detected=(
                f"`compose up` exited {exit_code} on `--update` while every required "
                "service was already answering. Last lines of its stderr:\n"
                + "\n".join(f"  {ln}" for ln in stderr_tail.splitlines()[-8:])
            ),
            why_deferred=(
                "Nothing after step 5 depends on this compose run, so the update "
                "continued instead of stopping half-applied. The compose config / "
                "image change this run was carrying did NOT reach the running "
                "containers."
            ),
            command_to_apply=(
                "# Re-run compose by hand to see the full error, fix it, then re-run the update:\n"
                f"{manual_cmd}\n"
                "python install.py --update\n"
                "# Or silence this entry:\n"
                "#   python -m vco_lib.project_init dismiss-deferral --folder . "
                f"--condition-id {CID_COMPOSE_UP_FAILED}"
            ),
            severity="warning",
            kg_node_refs=[],
        )
    )


def print_compose_failure_hints(stderr: str, container_cmd: str) -> None:
    """The targeted hints under a FAIL: daemon down, port taken, stale network label."""
    low = (stderr or "").lower()
    if "cannot connect" in low or "daemon" in low:
        print("\n  Hint: container daemon not running.")
        if container_cmd == "docker":
            print("    Linux:  sudo systemctl start docker")
            print("    macOS:  open Docker Desktop")
            print("    Windows: start Docker Desktop")
        else:
            print("    Linux:  systemctl --user start podman.socket")
    if "address already in use" in low or "bind" in low:
        print("\n  Hint: a host port is already in use.")
        print("    Either stop the conflicting process, or set")
        print("    VCT_FORCE_SEPARATE_CONTAINERS=1 + override WEAVIATE_PORT /")
        print("    OLLAMA_PORT / CODE_EMBED_PORT to use distinct ports.")
    if "incorrect label" in low and "com.docker.compose.network" in low:
        print("\n  Hint: the compose network already exists but a different compose")
        print("    tool created it (label mismatch). If")
        print(f"    `{container_cmd} network inspect <name>` shows NO containers")
        print("    attached, remove that network and compose recreates it with the")
        print("    right labels. If containers ARE attached, they belong to another")
        print("    compose project — see UPDATE_DEFERRED.md.")


def compose_failure_followup(
    *,
    args: argparse.Namespace,
    detected: Optional[dict],
    has_gpu: bool,
    deferral_report,
    exit_code: int,
    stderr: str,
    manual_cmd: str,
    log_event: LogEvent,
) -> bool:
    """After a FAIL: ``True`` → the caller continues the update (row written);
    ``False`` → the caller keeps its hard stop."""
    if not compose_failure_is_survivable(args, detected, has_gpu):
        return False
    emit_compose_up_failed_deferral(
        deferral_report, exit_code, (stderr or "").strip()[-600:], manual_cmd,
    )
    print(
        "\n  Continuing: every required service is already answering, so the "
        "rest of this update does not depend on this compose run. Recorded in "
        f"UPDATE_DEFERRED.md ({CID_COMPOSE_UP_FAILED})."
    )
    log_event(
        "5/10", "continue",
        "compose up failed but all required services answer — update continues",
    )
    return True
