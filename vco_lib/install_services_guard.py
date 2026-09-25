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

v0.2.97 narrowed rule 1's LEDGER ROW to code-embed. A Weaviate/Ollama
container another compose project created is ADOPTED by design now
(``vco_lib.service_reconcile``: VCO starts/stops it by name and never
recreates it), so it never reaches step 5's recreate list; the only
automatic ownership change left is code-embed's recreate-with-cache
(``service_lifecycle.migrate_code_embed``), and
``services_foreign_compose_identity`` means "that recreate refused". The
guard itself still refuses ANY foreign container it is handed — a safety net,
not a design path.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence

from vco_lib import containers as _containers
from vco_lib import code_embed_image as _code_embed_image
from vco_lib import deferral_emit as _deferral_emit
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "GuardOutcome",
    "foreign_owned_services",
    "emit_foreign_compose_identity_deferral",
    "emit_code_embed_migration_refused",
    "apply_recreate_guard",
    "compose_failure_is_survivable",
    "emit_compose_up_failed_deferral",
    "print_compose_failure_hints",
    "compose_failure_followup",
    "override_f_chain",
]

LogEvent = Callable[..., None]

CID_FOREIGN_IDENTITY = "services_foreign_compose_identity"
CID_COMPOSE_UP_FAILED = "services_compose_up_failed"

#: The two auto-load override names the storage-UX generator writes
#: (storage_ux.rs; the C-RT-5 two-name convention). An explicit ``-f`` chain
#: disables compose's auto-load, so install.py's step-5 compose invocation
#: appends :func:`override_f_chain` to actually consume a generated
#: override.
_OVERRIDE_FILE_NAMES: tuple[str, ...] = (
    "compose.override.yaml",
    "docker-compose.override.yml",
)


def override_f_chain(infra_dir: Path) -> list[str]:
    """``-f <file>`` argv fragments for every override present in
    ``infra_dir`` (v0.2.96 WP-4).  Empty on a stock install — the caller's
    argv is unchanged, so nothing outside the adoption flow can observe
    this."""
    out: list[str] = []
    for name in _OVERRIDE_FILE_NAMES:
        if (infra_dir / name).is_file():
            out.extend(["-f", str(infra_dir / name)])
    return out


def foreign_owned_services(
    services: list[str], runtime: str, infra_dir: Path, compose_file: Path,
    identities: Optional[dict] = None,
) -> dict[str, str]:
    """Which of ``services`` have an existing container ANOTHER compose
    identity created (svc → reason). Read-only; any probe that cannot
    positively read the container as ours counts it as NOT ours.

    ``identities``, when passed, is filled with the per-service
    :class:`vco_lib.containers.ComposeIdentity` that produced the verdict
    (``None`` when unreadable) — the v0.2.96 deferral remedy derives the
    owning-name rebuild command from it.  Optional out-param: existing
    callers and mocks that pass nothing behave exactly as before."""
    if not services:
        return {}
    own = _containers.compose_project_of(compose_file)
    out: dict[str, str] = {}
    for svc in dict.fromkeys(services):
        try:
            ref = _containers.find_existing_container(svc, runtime)
        except Exception:  # noqa: BLE001 — unknown alias / probe error → nothing to protect
            ref = None
        if not ref:
            continue  # no container exists — compose creates it fresh
        identity = _containers.compose_identity_of(ref, runtime)
        why = _containers.foreign_compose_identity(identity, own)
        if why:
            out[svc] = f"container '{ref}' {why}"
            if identities is not None:
                identities[svc] = identity
    return out


def build_foreign_compose_identity_entry(
    foreign: dict[str, str], runtime: str, infra_dir: Path,
    identities: Optional[dict] = None,
) -> DeferralEntry:
    """The ledger row for the services step 5 refused to recreate.

    v0.2.96: the remedy now leads with the guarded, mount-reconciling
    adoption command (``vco_lib.service_adoption``); Option A derives the
    OWNING-name rebuild through
    :func:`vco_lib.code_embed_image.rebuild_command` (Task 3a — a build
    under the installer's project name produces an image the running
    container never loads); the manual Option B text stays as the
    documented fallback.  ``identities`` (from
    :func:`foreign_owned_services`) enables the derived Option A; without
    it the generic placeholder shape from v0.2.93 is kept, so existing
    callers behave exactly as before.
    """
    names = " ".join(sorted(foreign))
    root = infra_dir.parent
    detail = "\n".join(f"  - {svc}: {why}" for svc, why in sorted(foreign.items()))
    identity = None
    if identities:
        identity = next(
            (v for v in identities.values()
             if v is not None and (getattr(v, "working_dir", "") or "").strip()),
            None,
        )
    if identity is not None:
        option_a_cmd = _code_embed_image.rebuild_command(
            root, f"{runtime} compose", identity, services=sorted(foreign),
        )
        option_a = (
            "# Option A — keep the current ownership and rebuild the image right\n"
            "# there, under the OWNING project's name (the only name the running\n"
            "# container loads):\n"
            f"#   {option_a_cmd}"
        )
    else:
        option_a = (
            "# Option A — keep the current ownership and apply the change there:\n"
            "#   cd <working dir shown above>\n"
            f"#   {runtime} compose -f <config files shown above> up -d --force-recreate {names}"
        )
    return DeferralEntry(
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
                "# RECOMMENDED — the guarded, mount-reconciling adoption (v0.2.96):\n"
                "# verifies every mount/env difference against the RUNNING container,\n"
                "# generates the reconciling infrastructure/compose.override.yaml,\n"
                "# then moves one service at a time with per-service rollback:\n"
                f"#   python -m vco_lib.service_adoption adopt-services --root {root}\n"
                f"{option_a}\n"
                "# Option B — the manual path (fallback): FIRST confirm every data\n"
                "# volume / bind mount is identical in both compose files:\n"
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


def emit_foreign_compose_identity_deferral(
    deferral_report, foreign: dict[str, str], runtime: str, infra_dir: Path,
    identities: Optional[dict] = None,
) -> Optional[DeferralEntry]:
    """Add the row to the RUN-scoped report; returns it so a hard stop can
    still persist it (the run report is only written by ``finalize()`` at the
    end of a COMPLETED run — review R1 finding 4)."""
    if deferral_report is None or not foreign:
        return None
    entry = build_foreign_compose_identity_entry(foreign, runtime, infra_dir,
                                                 identities=identities)
    deferral_report.add_entry(entry)
    return entry


def emit_code_embed_migration_refused(
    deferral_report, *, status: str, reason: str, runtime: str, infra_dir: Path,
) -> Optional[DeferralEntry]:
    """``services_foreign_compose_identity`` (v0.2.97 meaning): the automatic
    code-embed recreate-with-cache (``service_lifecycle.migrate_code_embed``)
    refused, or failed and rolled back. The running container is untouched
    (refused) or back under its previous owner (failed)."""
    if deferral_report is None:
        return None
    root = infra_dir.parent
    entry = DeferralEntry(
        condition_id=CID_FOREIGN_IDENTITY,
        title="code-embed could not be moved under VCO's compose with its cache",
        detected=(
            "code-embed runs under another compose project (or on an outdated image), and "
            "the automatic re-create under the installer's compose — same cache, verified "
            f"before and after — did not go through ({status}): {reason}"
        ),
        why_deferred=(
            "VCO never starts code-embed on an empty cache when a filled one exists; it "
            "refuses (nothing stopped) or rolls back to the previous owner instead. The "
            "service keeps running as it was, without the new image or compose tuning."
        ),
        command_to_apply=(
            "# Fix what the reason above names (for a bind cache: the host directory must\n"
            "# exist and hold the model cache), then re-run — the move is retried:\n"
            "python install.py --update\n"
            "# What VCO recorded for code-embed (port, cache mount):\n"
            "python -m vco_lib.service_endpoints show\n"
            f"# Or silence this entry:\n#   python -m vco_lib.project_init dismiss-deferral --folder {root} "
            f"--condition-id {CID_FOREIGN_IDENTITY}"
        ),
        severity="warning",
        kg_node_refs=[],
    )
    deferral_report.add_entry(entry)
    return entry


class GuardOutcome(NamedTuple):
    services_to_recreate: list[str]
    recreate_for_rebuild: list[str]
    build_services: list[str]
    foreign: dict[str, str]
    #: rows added to the run report by this guard — persisted through the
    #: locked on-disk writer if step 5 later hard-stops (see
    #: :func:`compose_failure_followup`).
    entries: tuple[DeferralEntry, ...]


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
) -> GuardOutcome:
    """Drop foreign-owned services from the three compose lists.

    Prints one ``[skip-recreate]`` line per service, writes the ledger row and
    the install event. Returns the filtered lists plus the foreign map (empty
    when nothing was foreign — the lists come back unchanged).
    """
    identities: dict = {}
    foreign = foreign_owned_services(
        services_to_recreate, runtime, infra_dir, compose_file,
        identities=identities,
    )
    if not foreign:
        return GuardOutcome(
            services_to_recreate, recreate_for_rebuild, build_services, {}, (),
        )
    for svc, why in sorted(foreign.items()):
        print(f"  [skip-recreate] {svc}: {why}")
    # v0.2.97: only code-embed's refusal is a ledger row. A foreign-owned
    # Weaviate/Ollama is adopted by design; the next run's reconcile records
    # it as `adopted_container` (never recreated).
    ledgered = {s: w for s, w in foreign.items() if s == "code_embed"}
    if ledgered:
        print(
            "      Left running as-is; the compose tuning / image rebuild did not "
            f"reach it. See UPDATE_DEFERRED.md ({CID_FOREIGN_IDENTITY})."
        )
    if set(foreign) - set(ledgered):
        print("      (a Weaviate/Ollama another compose project runs is used as it is — "
              "`python -m vco_lib.service_endpoints show`)")
    entry = emit_foreign_compose_identity_deferral(
        deferral_report, ledgered, runtime, infra_dir,
        identities={s: v for s, v in identities.items() if s in ledgered},
    )
    log_event(
        "5/10", "skip-recreate",
        "running container(s) owned by another compose identity — not recreated",
        data={"services": dict(foreign)},
    )
    return GuardOutcome(
        [s for s in services_to_recreate if s not in foreign],
        [s for s in recreate_for_rebuild if s not in foreign],
        [s for s in build_services if s not in foreign],
        foreign,
        (entry,) if entry is not None else (),
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
    install_root: Optional[Path] = None,
    persist_on_hard_stop: Sequence[DeferralEntry] = (),
) -> bool:
    """After a FAIL: ``True`` → the caller continues the update (row written);
    ``False`` → the caller keeps its hard stop.

    On the hard stop the run report is never written (``finalize()`` runs
    only at the end of a completed run), so any row this step already owes
    the user — the foreign-identity row above — is persisted here through
    the locked on-disk writer (review R1 finding 4). Soft-fail: a ledger
    write error must not mask the compose error the user is about to see.
    """
    if not compose_failure_is_survivable(args, detected, has_gpu):
        if install_root is not None and persist_on_hard_stop:
            try:
                _deferral_emit.emit_entries(install_root, tuple(persist_on_hard_stop))
            except Exception as exc:  # noqa: BLE001 — never mask the compose error
                print(f"  (could not persist the ledger row(s) before stopping: {exc})")
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
