# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pull a service's pinned image BEFORE install force-recreates it (v0.2.105).

Why: ``compose up --force-recreate <svc>`` removes the running container
first and lets the engine pull the new image while creating the replacement.
When that pull fails — a registry rate limit (Docker Hub, which also serves
``cr.weaviate.io``, refuses anonymous pulls past a quota), no network — the old
container is already gone and nothing replaces it: the service stays DOWN
until a later run succeeds (verified under podman-compose 1.5: the container
was removed and the create failed). An image bump (Weaviate 1.28.4 → 1.37.18)
makes every existing install hit that path once.

So step 5 pulls the pinned image of each service it is about to recreate
first. A service whose image is present (or pulls) is recreated as planned; a
service whose image cannot be pulled is NOT recreated this run — it keeps
running on its current image — and the run records
``service_image_pull_failed`` so the next update retries. Only the services
VCO pulls from a registry are checked (a locally built image, code_embed, is
the build step's business).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: The deferral a refused pull records (``vco_lib/deferral_conditions.toml``).
CID_PULL_FAILED = "service_image_pull_failed"
#: Services whose pinned image comes from a registry.
PULLED_SERVICES: tuple[str, ...] = ("weaviate", "ollama")

RunFn = Callable[..., Any]


def _run(run: Optional[RunFn], argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    err = (getattr(proc, "stderr", "") or "").strip().splitlines()
    return int(getattr(proc, "returncode", 1)), (err[-1] if err else "")


def ensure_image(runtime: str, image: str, *, run: Optional[RunFn] = None,
                 timeout: float = 900.0) -> tuple[bool, str]:
    """``(True, "")`` when *image* is present locally or pulls; else
    ``(False, <last stderr line>)``. ``image inspect`` answers the same way
    on docker and podman."""
    rc, _ = _run(run, [runtime, "image", "inspect", image], 60.0)
    if rc == 0:
        return True, ""
    rc, err = _run(run, [runtime, "pull", image], timeout)
    return rc == 0, ("" if rc == 0 else (err or f"`{runtime} pull` exited {rc}"))


def split_recreates(services: Sequence[str], *, runtime: str, root: Path,
                    run: Optional[RunFn] = None, timeout: float = 900.0,
                    pinned: Optional[Callable[[str, Path], Optional[str]]] = None,
                    ) -> tuple[list[str], list[tuple[str, str, str]]]:
    """``(services to recreate, [(service, image, error), ...] to keep)``.
    A service with no pinned image, or one VCO does not pull, is recreated as
    planned (nothing to check)."""
    if pinned is None:
        from vco_lib.service_adoption import pinned_image as pinned  # noqa: PLC0415
    keep_recreating: list[str] = []
    refused: list[tuple[str, str, str]] = []
    for svc in services:
        image = pinned(svc, root) if svc in PULLED_SERVICES else None
        if not image:
            keep_recreating.append(svc)
            continue
        ok, err = ensure_image(runtime, image, run=run, timeout=timeout)
        if ok:
            keep_recreating.append(svc)
        else:
            refused.append((svc, image, err))
    return keep_recreating, refused


def deferral_entry(refused: Sequence[tuple[str, str, str]], *, runtime: str, rerun: str):
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    lines = "\n".join(f"  - {svc}: `{image}` — {err}" for svc, image, err in refused)
    pulls = " && ".join(f"{runtime} pull {image}" for _, image, _ in refused)
    return DeferralEntry(
        condition_id=CID_PULL_FAILED,
        title="A service's new image could not be downloaded; it was not updated",
        detected=f"The update could not pull the pinned image of:\n{lines}",
        why_deferred=("Recreating the container would have removed the running one with "
                      "no image to replace it, so it was left running on its current "
                      "image. Its data is untouched. The next update retries the pull."),
        command_to_apply=f"{pulls} && {rerun}",
        severity="warning",
        kg_node_refs=[],
    )


def drop_unpullable_recreates(services_to_recreate: list, *, runtime: str, root: Path,
                              deferral_report: Any, rerun: str,
                              run: Optional[RunFn] = None, timeout: float = 900.0,
                              log_event: Optional[Callable[..., None]] = None) -> None:
    """Step 5's call: pull first, then remove (in place) every service whose
    image could not be pulled from *services_to_recreate*, print why, and
    record :data:`CID_PULL_FAILED`. Never raises."""
    if not services_to_recreate or not runtime:
        return
    try:
        keep, refused = split_recreates(list(services_to_recreate), runtime=runtime,
                                        root=root, run=run, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a probe fault keeps the old plan
        print(f"  [!] image pre-pull check failed ({exc}); recreating as planned")
        return
    if not refused:
        return
    services_to_recreate[:] = keep
    for svc, image, err in refused:
        print(f"  [!] {svc}: could not download {image} ({err}). Keeping the running "
              f"container; the next update retries.")
    if log_event is not None:
        log_event("5/10", "warn", "image pull refused; recreate skipped",
                  data={"refused": [list(r) for r in refused]})
    if deferral_report is not None:
        deferral_report.add_entry(deferral_entry(refused, runtime=runtime, rerun=rerun))
