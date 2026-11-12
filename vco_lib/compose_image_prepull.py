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

import re
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


#: Google's official Docker Hub mirror (pull-through cache of Docker Hub).
MIRROR_HOST = "mirror.gcr.io"
#: Registries served by Docker Hub — the ones whose rate limit the mirror avoids.
_DOCKER_HUB_HOSTS = ("docker.io", "registry-1.docker.io", "index.docker.io", "cr.weaviate.io")
_RATE_LIMITED = re.compile(r"toomanyrequests|\b429\b|pull rate limit", re.IGNORECASE)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def is_rate_limited(stderr: str) -> bool:
    """A registry refusal by quota (Docker Hub: ``toomanyrequests``, HTTP 429,
    "pull rate limit") — the one failure the mirror fallback answers."""
    return bool(_RATE_LIMITED.search(stderr or ""))


def mirror_ref(image: str) -> Optional[str]:
    """The SAME repository path + tag/digest on :data:`MIRROR_HOST`, for a
    Docker-Hub-served image; ``None`` for any other registry (ghcr.io, quay.io,
    localhost, …). ``cr.weaviate.io/semitechnologies/weaviate:T`` →
    ``mirror.gcr.io/semitechnologies/weaviate:T``; ``docker.io/ns/img:T`` →
    ``mirror.gcr.io/ns/img:T``; ``img:T`` / ``docker.io/img:T`` →
    ``mirror.gcr.io/library/img:T``."""
    ref = (image or "").strip()
    if not ref:
        return None
    first, _, rest = ref.partition("/")
    has_host = bool(rest) and ("." in first or ":" in first or first == "localhost")
    if has_host and first not in _DOCKER_HUB_HOSTS:
        return None
    path = rest if has_host else ref
    if "/" not in path.split("@", 1)[0].split(":", 1)[0]:
        path = f"library/{path}"
    return f"{MIRROR_HOST}/{path}"


def _capture(run: Optional[RunFn], argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return int(getattr(proc, "returncode", 1)), str(getattr(proc, "stdout", "") or "")


def _manifest_digests(run: Optional[RunFn], runtime: str, ref: str) -> set[str]:
    """Every manifest digest ``<runtime> manifest inspect <ref>`` names (a
    multi-arch list names one per platform); empty when the engine cannot
    read it (no such subcommand, the registry refuses, no network)."""
    rc, out = _capture(run, [runtime, "manifest", "inspect", ref], 120.0)
    return set(_DIGEST.findall(out)) if rc == 0 else set()


def _local_digests(run: Optional[RunFn], runtime: str, ref: str) -> set[str]:
    rc, out = _capture(run, [runtime, "image", "inspect", "--format",
                             "{{json .RepoDigests}}", ref], 60.0)
    return set(_DIGEST.findall(out)) if rc == 0 else set()


def _pull_from_mirror(runtime: str, image: str, refusal: str, *, run: Optional[RunFn],
                      timeout: float, note: Optional[Callable[[str], None]]) -> tuple[bool, str]:
    """The rate-limit fallback (owner ruling 2026-11-12): pull the same path +
    tag from :data:`MIRROR_HOST`, check it against the original registry's
    digests when the engine can read them (refused on a mismatch), then tag it
    under *image* so compose finds it."""
    mirror = mirror_ref(image)
    if mirror is None:
        return False, refusal
    rc, err = _run(run, [runtime, "pull", mirror], timeout)
    if rc != 0:
        return False, f"{refusal}; mirror {mirror} also failed: {err or f'exit {rc}'}"
    origin = _manifest_digests(run, runtime, image)
    if origin:
        pulled = _manifest_digests(run, runtime, mirror) | _local_digests(run, runtime, mirror)
        if not origin & pulled:
            _run(run, [runtime, "rmi", mirror], 60.0)
            return False, (f"{refusal}; mirror {mirror} refused: its digest does not match "
                           f"{image}'s")
    rc, err = _run(run, [runtime, "tag", mirror, image], 60.0)
    if rc != 0:
        return False, f"{refusal}; mirror {mirror} pulled but `{runtime} tag` failed: {err}"
    if note is not None:
        check = ("digest matches the original registry's" if origin else
                 "the original registry's digest was not readable, so it is unverified")
        note(f"{image} pulled from Google's Docker Hub mirror {mirror} after the registry "
             f"refused ({refusal}); {check}")
    return True, ""


def ensure_image(runtime: str, image: str, *, run: Optional[RunFn] = None,
                 timeout: float = 900.0,
                 note: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """``(True, "")`` when *image* is present locally or pulls; else
    ``(False, <last stderr line>)``. ``image inspect`` answers the same way
    on docker and podman. A pull refused by a rate limit is retried from
    :data:`MIRROR_HOST` (:func:`_pull_from_mirror`); *note* receives the line
    naming the mirror used. Any other failure is returned as it was."""
    rc, _ = _run(run, [runtime, "image", "inspect", image], 60.0)
    if rc == 0:
        return True, ""
    rc, err = _run(run, [runtime, "pull", image], timeout)
    if rc == 0:
        return True, ""
    err = err or f"`{runtime} pull` exited {rc}"
    if is_rate_limited(err):
        return _pull_from_mirror(runtime, image, err, run=run, timeout=timeout, note=note)
    return False, err


def split_recreates(services: Sequence[str], *, runtime: str, root: Path,
                    run: Optional[RunFn] = None, timeout: float = 900.0,
                    pinned: Optional[Callable[[str, Path], Optional[str]]] = None,
                    note: Optional[Callable[[str], None]] = None,
                    ) -> tuple[list[str], list[tuple[str, str, str]]]:
    """``(services to recreate, [(service, image, error), ...] to keep)``.
    A service with no pinned image, or one VCO does not pull, is recreated as
    planned (nothing to check). *note* gets each mirror-fallback line."""
    if pinned is None:
        from vco_lib.service_adoption import pinned_image as pinned  # noqa: PLC0415
    keep_recreating: list[str] = []
    refused: list[tuple[str, str, str]] = []
    for svc in services:
        image = pinned(svc, root) if svc in PULLED_SERVICES else None
        if not image:
            keep_recreating.append(svc)
            continue
        ok, err = ensure_image(runtime, image, run=run, timeout=timeout, note=note)
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
                                        root=root, run=run, timeout=timeout,
                                        note=_mirror_reporter(log_event))
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


def _mirror_reporter(log_event: Optional[Callable[..., None]]) -> Callable[[str], None]:
    """Print + install-log the line naming the mirror an image came from."""
    def note(line: str) -> None:
        print(f"  [i] {line}")
        if log_event is not None:
            log_event("5/10", "info", "image pulled from the Docker Hub mirror",
                      data={"detail": line, "mirror": MIRROR_HOST})
    return note


def prepull_starts(services_to_start: Sequence[str], *, runtime: str, root: Path,
                   run: Optional[RunFn] = None, timeout: float = 900.0,
                   log_event: Optional[Callable[..., None]] = None,
                   pinned: Optional[Callable[[str, Path], Optional[str]]] = None) -> None:
    """Step 5, services it STARTS (a fresh install's Weaviate / Ollama): pull
    their pinned image first through :func:`ensure_image`, so a registry rate
    limit is answered by the mirror before ``compose up`` would hit it. A
    failure changes nothing — ``compose up`` then pulls and reports exactly as
    before. Never raises."""
    if not services_to_start or not runtime:
        return
    try:
        if pinned is None:
            from vco_lib.service_adoption import pinned_image as pinned  # noqa: PLC0415
        note = _mirror_reporter(log_event)
        for svc in services_to_start:
            image = pinned(svc, root) if svc in PULLED_SERVICES else None
            if image:
                ensure_image(runtime, image, run=run, timeout=timeout, note=note)
    except Exception as exc:  # noqa: BLE001 — best effort; compose up still runs
        print(f"  [!] image pre-pull before start skipped ({exc})")
