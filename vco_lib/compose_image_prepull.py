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

A-021 (v0.2.107): before that pull, the image has to FIT. A pinned-image bump
of Ollama pulled ~10 GB while the old image was still in use and took a test
machine's system drive from 11.4 GB free to 0 (under Docker Desktop the image
store is a growing disk image on the host drive, so the engine does not fail
first — the user's disk does). :func:`pull_space_shortfall` compares the free
space where the engine stores images with an estimate of what the pull needs;
when it does not fit, the service is not recreated (it keeps running on its
current image) and the run records ``service_image_no_disk_space`` naming both
numbers and the place to free. When either number is unknown the pull runs as
before.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: The deferral a refused pull records (``vco_lib/deferral_conditions.toml``).
CID_PULL_FAILED = "service_image_pull_failed"
#: The deferral a pull refused for disk space records (A-021).
CID_NO_DISK_SPACE = "service_image_no_disk_space"
#: Services whose pinned image comes from a registry.
PULLED_SERVICES: tuple[str, ...] = ("weaviate", "ollama")

#: A-021 — what a pull needs, estimated WITHOUT a network call: the on-disk
#: (uncompressed) size of the image of the SAME repository already installed
#: (the one the running container uses — a new tag of a repository is about
#: the size of the old one) times this factor, plus the doctor's free-space
#: floor (``VCT_DISK_SPACE_MIN_FREE_GB``, 2 GiB by default). The factor covers
#: the compressed layer blobs that sit next to the extracted layers while the
#: pull runs (about half the extracted size for these images). It is an
#: estimate: layers the new tag shares with the old one need no space, so the
#: real need is usually lower — the check refuses only a pull that this
#: estimate says cannot fit.
PULL_SPACE_FACTOR = 1.5
_GIB = 1024 ** 3

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


# ---------------------------------------------------------------------------
# A-021: does the pull fit?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpaceShortfall:
    """A pull refused because it does not fit (both numbers in bytes)."""

    service: str
    image: str
    need_bytes: int
    free_bytes: int
    where: str
    basis: str  # what the estimate was made from


def decide_pull_fits(need_bytes: Optional[int], free_bytes: Optional[int]) -> bool:
    """The gate: refuse ONLY when both numbers are known and free < need.
    Unknown → pull as before (a probe that cannot see must not block)."""
    if need_bytes is None or free_bytes is None:
        return True
    return free_bytes >= need_bytes


def _repo_key(ref: str) -> str:
    """Repository of an image reference, normalised across engines: no tag or
    digest, no ``docker.io`` / ``registry-1.docker.io`` / ``index.docker.io``
    host, no ``library/`` (docker lists ``ollama/ollama``, podman
    ``docker.io/ollama/ollama``)."""
    ref = (ref or "").strip().split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    last = last.split(":", 1)[0]
    ref = f"{head}/{last}" if head else last
    first, _, rest = ref.partition("/")
    if rest and first in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        ref = rest
    if ref.startswith("library/"):
        ref = ref[len("library/"):]
    return ref


def installed_repo_size(runtime: str, image: str, *, run: Optional[RunFn] = None) -> Optional[tuple[int, str]]:
    """``(bytes, ref)`` of the largest local image of *image*'s repository
    (any tag), or ``None`` when there is none / the engine cannot list them.
    Local only: ``image ls`` + ``image inspect --format {{.Size}}``."""
    rc, out = _capture(run, [runtime, "image", "ls", "--format",
                             "{{.Repository}}:{{.Tag}} {{.ID}}"], 60.0)
    if rc != 0:
        return None
    want = _repo_key(image)
    best: Optional[tuple[int, str]] = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or _repo_key(parts[0]) != want:
            continue
        rc, size = _capture(run, [runtime, "image", "inspect", "--format",
                                  "{{.Size}}", parts[1]], 60.0)
        size = size.strip()
        if rc == 0 and size.isdigit() and (best is None or int(size) > best[0]):
            best = (int(size), parts[0])
    return best


def _desktop_vm_dirs(is_podman: bool) -> list[Path]:
    """Host folders holding the engine VM's growing disk image (Docker Desktop,
    podman machine) — where an image pull lands on the HOST under Windows and
    macOS. Never the VM's virtual size: the host drive is what fills."""
    home = Path.home()
    if is_podman:
        return [home / ".local" / "share" / "containers" / "podman" / "machine"]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return [Path(local) / "Docker" / "wsl"] if local else []
    if sys.platform == "darwin":
        return [home / "Library" / "Containers" / "com.docker.docker" / "Data"]
    return []


def engine_storage_path(runtime: str, *, run: Optional[RunFn] = None) -> Optional[Path]:
    """The HOST path whose filesystem receives a pulled image, or ``None``.

    The engine's own image store first (podman ``Store.GraphRoot``, docker
    ``DockerRootDir`` from ``<runtime> info``) when it exists on this host —
    native Linux. Under a VM engine that path is inside the VM, so the host
    folder of the VM's disk image is used instead (:func:`_desktop_vm_dirs`).
    Never the repository's disk: the two are often different drives."""
    is_podman = "podman" in Path(runtime).name.lower()
    fmt = "{{.Store.GraphRoot}}" if is_podman else "{{.DockerRootDir}}"
    rc, out = _capture(run, [runtime, "info", "--format", fmt], 60.0)
    root = out.strip() if rc == 0 else ""
    if root and os.path.isabs(root) and Path(root).is_dir():
        return Path(root)
    for candidate in _desktop_vm_dirs(is_podman):
        if candidate.is_dir():
            return candidate
    return None


def _free_bytes(path: Path) -> Optional[int]:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _floor_bytes() -> int:
    try:
        from vco_lib.doctor import disk_min_free_bytes  # noqa: PLC0415 — the one floor

        return disk_min_free_bytes()[0]
    except Exception:  # noqa: BLE001
        return 2 * _GIB


def pull_space_shortfall(service: str, runtime: str, image: str, *,
                         run: Optional[RunFn] = None,
                         free: Optional[Callable[[Path], Optional[int]]] = None,
                         ) -> Optional[SpaceShortfall]:
    """A :class:`SpaceShortfall` when pulling *image* would not fit, else
    ``None`` — including whenever the size or the free space cannot be read
    (then the pull runs as before). Never raises."""
    try:
        installed = installed_repo_size(runtime, image, run=run)
        if installed is None:
            return None
        where = engine_storage_path(runtime, run=run)
        if where is None:
            return None
        free_b = (free or _free_bytes)(where)
        need = int(installed[0] * PULL_SPACE_FACTOR) + _floor_bytes()
        if free_b is None or decide_pull_fits(need, free_b):
            return None
        return SpaceShortfall(service=service, image=image, need_bytes=need,
                              free_bytes=int(free_b), where=str(where),
                              basis=f"{installed[1]}, {installed[0] / _GIB:.1f} GB installed")
    except Exception:  # noqa: BLE001 — a probe fault never blocks the pull
        return None


def split_no_space(services: Sequence[str], *, runtime: str, root: Path,
                   run: Optional[RunFn] = None,
                   pinned: Optional[Callable[[str, Path], Optional[str]]] = None,
                   free: Optional[Callable[[Path], Optional[int]]] = None,
                   ) -> tuple[list[str], list[SpaceShortfall]]:
    """``(services to go on with, shortfalls)``: a service whose pinned image
    is NOT present locally and whose pull does not fit is taken out. A present
    image needs no pull and is never checked."""
    if pinned is None:
        from vco_lib.service_adoption import pinned_image as pinned  # noqa: PLC0415
    go_on: list[str] = []
    short: list[SpaceShortfall] = []
    for svc in services:
        image = pinned(svc, root) if svc in PULLED_SERVICES else None
        if image and _run(run, [runtime, "image", "inspect", image], 60.0)[0] != 0:
            sf = pull_space_shortfall(svc, runtime, image, run=run, free=free)
            if sf is not None:
                short.append(sf)
                continue
        go_on.append(svc)
    return go_on, short


def _gb(n: int) -> str:
    return f"{n / _GIB:.1f} GB"


def no_space_entry(short: Sequence[SpaceShortfall], *, runtime: str, rerun: str):
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    lines = "\n".join(
        f"  - {s.service}: `{s.image}` needs about {_gb(s.need_bytes)} free on {s.where}; "
        f"{_gb(s.free_bytes)} free (estimate from {s.basis})" for s in short)
    gap = max(s.need_bytes - s.free_bytes for s in short)
    return DeferralEntry(
        condition_id=CID_NO_DISK_SPACE,
        title="A service's new image does not fit on the disk; it was not updated",
        detected=f"The update did not download the pinned image of:\n{lines}",
        why_deferred=(
            "Downloading it would have filled the drive the container engine stores images "
            "on. The service keeps running on its current image and its data is untouched. "
            f"Free at least {_gb(gap)} on that drive (`{runtime} system df` shows what the "
            f"engine holds; `{runtime} image prune` removes images no container uses), then "
            "re-run the update; every update checks again."),
        command_to_apply=rerun,
        severity="warning",
        kg_node_refs=[],
    )


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
    """Step 5's call: check that each new image fits (A-021), pull first, then
    remove (in place) every service whose image does not fit or could not be
    pulled from *services_to_recreate*, print why, and record
    :data:`CID_NO_DISK_SPACE` / :data:`CID_PULL_FAILED`. Never raises."""
    if not services_to_recreate or not runtime:
        return
    try:
        fits, short = split_no_space(list(services_to_recreate), runtime=runtime,
                                     root=root, run=run)
    except Exception as exc:  # noqa: BLE001 — a probe fault keeps the old plan
        print(f"  [!] image disk-space check failed ({exc}); continuing as planned")
        fits, short = list(services_to_recreate), []
    if short:
        services_to_recreate[:] = fits
        for s in short:
            print(f"  [!] {s.service}: {s.image} needs about {_gb(s.need_bytes)} free on "
                  f"{s.where}, {_gb(s.free_bytes)} is free. Not downloading it; keeping the "
                  f"running container. Free space and re-run the update.")
        if log_event is not None:
            log_event("5/10", "warn", "image pull refused: not enough disk space",
                      data={"refused": [[s.service, s.image, s.need_bytes, s.free_bytes,
                                         s.where] for s in short]})
        if deferral_report is not None:
            deferral_report.add_entry(no_space_entry(short, runtime=runtime, rerun=rerun))
    if not services_to_recreate:
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
