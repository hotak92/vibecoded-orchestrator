# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.compose_mounts — the ONE parser of a compose service's mount entries.

v0.2.100 (F-W2-13 / F-W3-10). Two readers ask "what does this compose config
mount for this service?":

* :func:`vco_lib.data_identity.render_mount` — STRICT, on the provider's own
  ``compose config`` render, for the one data destination a recreate must
  preserve (fail closed on anything unreadable);
* :func:`vco_lib.service_adoption.config_mounts` — every destination of a
  Python-merged config, for the adoption's mount-equality gate.

Until this module each carried its own reading of the entry shapes (short
``source:target[:opts]`` strings, docker compose v2's long-syntax mappings,
Windows drive letters) and of the top-level ``volumes:`` map, and the two had
already drifted (one resolved an unnamed volume to ``<project>_<key>``, the
other to the bare key). The shape knowledge now lives here; the callers keep
only their policy (strict refusal vs. per-destination comparison).

Nothing here touches a runtime or the filesystem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "MountShapeError",
    "MountEntry",
    "split_mount_entry",
    "is_bind_source",
    "parse_mount_entry",
    "volume_real_name",
]


class MountShapeError(ValueError):
    """A mount entry / volume declaration whose shape this parser does not know.

    The message names the field (``where``) so a strict caller can refuse
    with a reason the user can act on."""


@dataclass(frozen=True)
class MountEntry:
    """One parsed ``services.<svc>.volumes`` entry, exactly as stated.

    ``kind`` is what the entry says (``bind`` / ``volume`` / ``tmpfs`` / …);
    ``source`` is ``""`` for an anonymous volume (compose creates a fresh,
    empty one); a named volume's ``source`` is the top-level KEY — resolve it
    with :func:`volume_real_name`."""

    kind: str
    source: str
    target: str
    options: str = ""


#: ``C:\\x`` / ``c:/x`` — a Windows drive path.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def split_mount_entry(entry: str) -> list[str]:
    """Split ``source:target[:opts]`` without severing a Windows drive letter.

    On Windows the source carries its own colon (``C:\\volumes\\ollama:/root/.ollama:Z``);
    a bare ``split(":")`` would yield source ``"C"``. The drive letter is
    re-joined ONLY when that still leaves an absolute container target — which
    separates a Windows bind from a one-character VOLUME name (``v:/data`` is
    volume ``v`` at ``/data``)."""
    parts = entry.split(":")
    if (
        len(parts) >= 3
        and len(parts[0]) == 1
        and parts[0].isalpha()
        and parts[1][:1] in ("\\", "/")
        and parts[2][:1] == "/"
    ):
        parts = [f"{parts[0]}:{parts[1]}", *parts[2:]]
    return parts


def is_bind_source(source: str) -> bool:
    """Compose's rule for a mount SOURCE: a host path is a bind, anything else
    names a volume. A path is POSIX absolute (``/``), home-relative (``~``),
    project-relative (``.``), a UNC/backslash path (``\\``) or a Windows drive
    path (``C:\\`` / ``C:/``)."""
    return source.startswith(("/", "~", ".", "\\")) or bool(_WINDOWS_DRIVE_RE.match(source))


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def parse_mount_entry(entry: Any, where: str) -> Optional[MountEntry]:
    """Parse one ``services.<svc>.volumes`` entry.

    * a string — compose's short syntax (podman-compose prints it as
      written): ``target`` alone is an anonymous volume; ``source:target[:opts]``
      is a bind when :func:`is_bind_source`, else a named volume;
    * a mapping — the long syntax (docker compose v2 normalises every entry to
      it): ``type``, ``source``, ``target``, ``read_only``. An unstated (or
      non-string) ``type`` is ``kind == ""`` — compose's default is a volume,
      but a strict reader of a provider render may refuse it.

    Returns ``None`` only for an empty string. Raises :class:`MountShapeError`
    naming *where* for a mapping without a string ``target`` and for anything
    that is neither a string nor a mapping."""
    if isinstance(entry, str):
        parts = split_mount_entry(entry)
        if not parts or not parts[0]:
            return None
        if len(parts) < 2:
            return MountEntry("volume", "", parts[0])
        source, target = parts[0], parts[1]
        opts = parts[2] if len(parts) > 2 else ""
        return MountEntry("bind" if is_bind_source(source) else "volume", source, target, opts)
    if isinstance(entry, dict):
        target = entry.get("target")
        if not isinstance(target, str) or not target:
            raise MountShapeError(f"{where} has no string `target` (keys: "
                                  f"{', '.join(sorted(map(str, entry))) or 'none'})")
        kind = entry.get("type")
        source = entry.get("source")
        return MountEntry(
            kind if isinstance(kind, str) else "",
            source if isinstance(source, str) else "",
            target,
            "ro" if entry.get("read_only") else "",
        )
    raise MountShapeError(f"{where} is a {_type_name(entry)}, not a mount string or mapping")


def volume_real_name(top_volumes: Any, key: str, where: str,
                     project: Optional[str]) -> str:
    """The real name of the top-level volume *key* — what ``inspect`` reports
    as a live mount's ``Name``.

    An explicit ``name:`` wins; ``external: true`` (or the legacy
    ``external: {name: …}``) names the volume itself; otherwise compose
    prefixes the project: ``<project>_<key>``. Raises :class:`MountShapeError`
    for an undeclared key, a malformed declaration, or an unprefixed name whose
    *project* is not known (the real name cannot be derived)."""
    top = {} if top_volumes is None else top_volumes
    if not isinstance(top, dict):
        raise MountShapeError(f"the top-level `volumes` is a {_type_name(top)}, not a map")
    if key not in top:
        raise MountShapeError(f"{where} names volume {key!r}, which the top-level `volumes` "
                              "map does not declare")
    spec = top[key]
    if spec is None:
        spec = {}  # podman-compose prints a bare `key:` declaration as null
    if not isinstance(spec, dict):
        raise MountShapeError(f"volumes.{key} is a {_type_name(spec)}, not a map")
    name = spec.get("name")
    if name is not None:
        if not isinstance(name, str) or not name:
            raise MountShapeError(f"volumes.{key}.name is a {_type_name(name)}, not a volume name")
        return name
    external = spec.get("external")
    if isinstance(external, dict):  # the legacy `external: {name: …}` form
        ext_name = external.get("name")
        if ext_name is not None and (not isinstance(ext_name, str) or not ext_name):
            raise MountShapeError(f"volumes.{key}.external.name is not a volume name")
        return ext_name or key
    if external:
        return key
    if not project:
        raise MountShapeError(f"volumes.{key} has no explicit `name:` and the compose project "
                              f"is not known, so its real name (<project>_{key}) cannot be "
                              "derived")
    return f"{project}_{key}"
