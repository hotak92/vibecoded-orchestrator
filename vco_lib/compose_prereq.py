# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.compose_prereq — "is there a compose provider?" as an install prerequisite.

v0.2.106. Install step 5 starts VCO's services with compose. A machine with
podman (or docker) but no compose implementation — podman 5+ ships ``podman
compose`` only as a thin delegator that needs ``docker-compose`` or
``podman-compose`` — passed every earlier check and died at step 5 with
podman's "looking up compose provider failed" (field report, macOS, fresh
install). This module turns that into a prerequisite the install checks up
front. It does NOT detect compose itself: :func:`vco_lib.compose_provider.detect`
(over :func:`vco_lib.containers.compose_command`, the one preference order) is
the only detector; this module adds the per-OS remediation and the three
consumers' shapes:

* :func:`probe` / :func:`missing_prereq` — the envelope's ``system.compose``
  block and the ``compose_provider`` row of its ``missing_prereqs`` (``install.py --bootstrap``; ``vco doctor``
  reads the same envelope);
* :func:`install_commands` — what ``install.py --bootstrap --install-missing``
  runs for it, per package manager;
* :func:`install_preflight` — install.py's own check right after step 2: a
  fresh install without a provider stops there (before the venv, the
  dependencies and step 5), an ``--update`` is warned (step 5 may reuse
  services that are already running);
* :func:`remediation` — also the remedy of step 5's own failure classifier
  (:mod:`vco_lib.compose_recovery`, cause ``compose_provider_missing``).
"""
from __future__ import annotations

import platform
import sys
from typing import Callable, Optional

__all__ = ["PREREQ_NAME", "remediation", "install_commands", "probe", "missing_prereq",
           "install_preflight"]

PREREQ_NAME = "compose_provider"

#: package manager -> the distribution package that provides ``podman-compose``
_PODMAN_COMPOSE_INSTALL = {
    "brew": ["brew install podman-compose"],
    "apt": ["sudo apt-get install -y podman-compose"],
    "dnf": ["sudo dnf install -y podman-compose"],
    "pacman": ["sudo pacman -S --noconfirm podman-compose"],
    "zypper": ["sudo zypper install -y podman-compose"],
    "apk": ["sudo apk add podman-compose"],
    "winget": ["winget install --id Docker.DockerCompose --silent"],
}


def _canonical_os(os_name: Optional[str]) -> str:
    """``macos`` / ``linux`` / ``windows`` from either spelling (bootstrap's
    canonical names or :func:`platform.system`)."""
    name = (os_name if os_name is not None else platform.system()).strip().lower()
    if name in ("darwin", "macos", "mac", "osx"):
        return "macos"
    if name.startswith("win"):
        return "windows"
    return "linux"


def remediation(runtime: str, os_name: Optional[str] = None) -> str:
    """The one human remedy for "no compose provider for ``runtime``"."""
    os_c = _canonical_os(os_name)
    if runtime == "docker":
        if os_c == "linux":
            return ("Install the Docker Compose v2 plugin: `sudo apt-get install -y "
                    "docker-compose-plugin` or `sudo dnf install -y docker-compose-plugin` "
                    "(Docker's package repository), or your distribution's docker-compose "
                    "package. Check with `docker compose version`, then re-run the installer.")
        return ("Docker Desktop ships Compose: update or reinstall Docker Desktop"
                + (" (or `brew install docker-compose`)" if os_c == "macos" else "")
                + ". Check with `docker compose version`, then re-run the installer.")
    if os_c == "macos":
        how = "`brew install podman-compose`"
    elif os_c == "windows":
        how = ("`winget install --id Docker.DockerCompose` (`podman compose` delegates to "
               "docker-compose)")
    else:
        how = ("your distribution's podman-compose package: `sudo apt-get install -y "
               "podman-compose` (Debian/Ubuntu), `sudo dnf install -y podman-compose` "
               "(Fedora/RHEL), `sudo pacman -S podman-compose` (Arch), `sudo zypper install "
               "podman-compose` (openSUSE), `sudo apk add podman-compose` (Alpine)")
    return (f"Install a compose provider for podman — {how}. Check with "
            "`podman-compose version` (or `podman compose version`), then re-run the installer.")


def install_commands(runtime: str, pkg_mgr: Optional[str]) -> list[str]:
    """What ``--install-missing`` runs for a missing provider. Empty when there
    is no unattended recipe (docker: the plugin comes with Docker Desktop or
    Docker's own repository — :func:`remediation` says so)."""
    if runtime != "podman":
        return []
    return list(_PODMAN_COMPOSE_INSTALL.get(pkg_mgr or "", []))


DetectFn = Callable[[str], object]


def _default_detect(runtime: str) -> object:
    from vco_lib import compose_provider as _cp  # noqa: PLC0415 — keeps bootstrap import light

    return _cp.detect(runtime)


def probe(runtime: Optional[str], *, detect: Optional[DetectFn] = None) -> Optional[dict]:
    """The bootstrap envelope's ``system.compose`` block for ``runtime``:
    ``{"runtime", "ok", "engine", "argv"}`` (``ok`` false = no compose at all —
    :func:`vco_lib.compose_provider.detect` returned ``None``). ``None`` when
    there is no runtime to ask about."""
    if runtime not in ("podman", "docker"):
        return None
    found = (detect or _default_detect)(runtime)
    return {
        "runtime": runtime,
        "ok": found is not None,
        "engine": getattr(found, "engine", None),
        "argv": list(getattr(found, "argv", ()) or ()) or None,
    }


def _human(runtime: str) -> str:
    if runtime == "docker":
        return ("No compose for docker: neither the `docker compose` plugin nor "
                "`docker-compose` was found, and install step 5 starts the services with it")
    return ("No compose provider for podman: neither `podman-compose` nor a provider "
            "`podman compose` can delegate to (docker-compose) was found, and install "
            "step 5 starts the services with it")


def missing_prereq(compose_probe: Optional[dict], *,
                   os_name: Optional[str] = None) -> Optional[dict]:
    """The ``missing_prereqs`` row for a :func:`probe` block, or ``None``
    (provider present, or no runtime — the runtime's own row covers that)."""
    if not isinstance(compose_probe, dict) or compose_probe.get("ok") is not False:
        return None
    runtime = str(compose_probe.get("runtime") or "podman")
    return {
        "name": PREREQ_NAME,
        "human": _human(runtime),
        "severity": "blocking",
        "install_hint": remediation(runtime, os_name),
    }


def install_preflight(runtime: str, *, update: bool, no_containers: bool = False,
                      os_name: Optional[str] = None, detect: Optional[DetectFn] = None,
                      log_event: Optional[Callable[..., None]] = None) -> bool:
    """install.py's check after step 2. ``True`` = carry on; ``False`` = a
    fresh install with no provider — the caller exits 1 before any step that
    changes the machine. An ``--update`` only warns: step 5 reuses services
    that already answer, and its own failure names the same remedy."""
    if not runtime or no_containers:
        return True
    row = missing_prereq(probe(runtime, detect=detect), os_name=os_name)
    if row is None:
        return True
    stream = sys.stdout if update else sys.stderr
    tag = "WARNING" if update else "ERROR"
    print(f"\n  {tag}: {row['human']}.", file=stream)
    print(f"  Fix: {row['install_hint']}", file=stream)
    if update:
        print("  Continuing the update: services that already run are reused; if step 5 "
              "has to start one, it will fail until a compose provider is installed.",
              file=stream)
    if log_event is not None:
        log_event("2/10", "warn" if update else "error", "no compose provider",
                  data={"runtime": runtime, "prereq": PREREQ_NAME})
    return update
