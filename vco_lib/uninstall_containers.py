# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall step [1b]: VCO containers ``compose down`` cannot reach.

Step [1] runs ``compose down`` against the CURRENT install's
``infrastructure/docker-compose.yml``. Compose only acts on the services in
the file it is given, so a container an OLDER VCO version created under a
name the current file no longer uses survives it. Field run 2026-10-16
(Windows 11, podman-compose): ``vct_code_embed`` — the pre-v0.2.15 name of
the code-embedding container, compose project ``infrastructure`` — from an
install folder no longer in use outlived the uninstall.

What this step removes, and the two gates it never skips:

* **an exact name list** — :func:`vco_lib.containers.vco_prefixed_container_names`,
  every ``vco_*`` / ``vct_*`` name VCO has shipped, read from the container
  naming SSOT. Generic names (``ollama``, ``weaviate`` …) are never on it.
* **VCO's compose labels** — the container's ``com.docker.compose.service``
  must be the VCO service that name belongs to. A same-named container that
  compose did not create, or created for another service, is reported and
  left alone.

It is never ``compose down --remove-orphans``: the project name
``infrastructure`` is generic, and an orphan sweep would remove whatever else
the user runs under it. ``<runtime> rm --force <name>`` removes the container
only; named volumes stay (they are print-only, step [2]). Never raises.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from vco_lib import containers as _containers

__all__ = ["LegacyContainer", "find", "plan_lines", "remove"]

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
IdentityFn = Callable[..., Optional[_containers.ComposeIdentity]]


@dataclass(frozen=True)
class LegacyContainer:
    name: str
    service: str
    project: str
    working_dir: str = ""


def _same_dir(a: str, b: Optional[Path]) -> bool:
    if not a or b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def find(
    runtime: str,
    *,
    current_compose_dir: Optional[Path] = None,
    identity: Optional[IdentityFn] = None,
    run: Optional[RunFn] = None,
) -> tuple[list[LegacyContainer], list[tuple[str, str]]]:
    """``(to_remove, left)`` — ``left`` is ``(name, reason)`` for a VCO name
    whose labels do not mark it as VCO's. A name that does not exist (or was
    not created by compose) is in neither list.

    The current install's own containers (canonical name, created from
    ``current_compose_dir``) are step [1]'s, not this step's, and are skipped.
    """
    read = identity or _containers.compose_identity_of
    remove: list[LegacyContainer] = []
    left: list[tuple[str, str]] = []
    for name, service in _containers.vco_prefixed_container_names():
        ident = read(name, runtime, run=run)
        if ident is None:
            continue  # absent, or not compose-created: not provably ours
        if ident.service != service:
            shown = ident.service or "(none)"
            left.append((name, f"compose service label is {shown!r}, not VCO's {service!r}"))
            continue
        if name == _containers.canonical_name(service) and _same_dir(ident.working_dir, current_compose_dir):
            continue  # the current stack — step [1] handles it
        remove.append(LegacyContainer(name, service, ident.project, ident.working_dir))
    return remove, left


def plan_lines(found: list[LegacyContainer], left: list[tuple[str, str]]) -> list[str]:
    if not found and not left:
        return ["  [1b] [skip] No legacy VCO containers from an older install"]
    out = ["  [1b] Remove legacy VCO containers `compose down` cannot reach (volumes stay):"]
    for c in found:
        where = f" in {c.working_dir}" if c.working_dir else ""
        out.append(f"       - {c.name}  (compose service {c.service!r}, project {c.project!r}{where})")
    out += [f"       LEFT IN PLACE: {name}  ({why})" for name, why in left]
    return out


def remove(
    runtime: str,
    *,
    current_compose_dir: Optional[Path] = None,
    identity: Optional[IdentityFn] = None,
    run: Optional[RunFn] = None,
) -> list[str]:
    """Re-probe (step [1] may already have removed some) and remove. Audit lines."""
    found, left = find(runtime, current_compose_dir=current_compose_dir, identity=identity, run=run)
    runner = run or _containers._tsd.run
    audit: list[str] = []
    for c in found:
        try:
            res = runner([runtime, "rm", "--force", c.name], capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            audit.append(f"WARN: could not remove legacy container {c.name}: {exc}")
            continue
        if res.returncode == 0:
            audit.append(f"removed legacy container {c.name} (compose service {c.service!r}; volumes kept)")
        else:
            err = (res.stderr or "").strip()[:200]
            audit.append(f"WARN: `{runtime} rm --force {c.name}` exited {res.returncode}: {err}")
    audit += [f"left legacy-named container {name} ({why})" for name, why in left]
    return audit
