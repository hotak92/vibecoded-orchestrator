# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Does a project's on-disk env hold what ``config_projection apply`` writes?

v0.2.97. ONE home for the comparison, read-only. Two callers asked the same
question with two different answers before this module existed:

* ``vco verify-env-projection`` (:mod:`vco_lib.cli.verify`) read
  ``.claude/settings.json`` with a strict ``json.load`` — a JSONC file (which
  Claude Code accepts and every VCO writer now edits in place) read as "no
  values", i.e. a false drift report — and it diffed ``.vscode/settings.json``,
  a surface ``apply`` does not write unless asked, so a project that had never
  opted into it could never verify clean;
* the ``project_move_env_reprojection_failed`` clear probe
  (:func:`vco_lib.project_move.env_reprojection_still_owed`).

WHICH SURFACES. Exactly the ones ``apply_project_env`` writes when no caller
names any — ``config_projection``'s own default list, read from there rather
than restated, so a surface added to (or dropped from) the default is checked
(or no longer checked) here in the same change.

WHAT "MATCH" MEANS, per surface, over the canonical keys
(:func:`vco_lib.config_projection.list_canonical_keys`) — the only keys the
projection owns; anything else in a surface is the user's and never looked at:

* a key the bundle carries must hold that value;
* a key the bundle omits must be absent (``apply`` deletes it — "signal to
  remove").

v0.2.104 L5: each JSON surface is compared over ITS keys only
(:func:`vco_lib.config_projection.surface_keys`) — the machine-local keys in
``.claude/settings.local.json``, every other canonical key in
``.claude/settings.json``; a machine key still in ``settings.json`` is drift
(``apply`` moves it).

A JSON surface is read with the ONE JSONC reader (:mod:`vco_lib.jsonc_edit`);
``.claude/env`` with the projection's own reader of its managed block. A
missing file is not unreadable — it holds nothing, so every value the bundle
carries is reported missing. A file that EXISTS but cannot be read is
UNREADABLE: the check says so and draws no conclusion, because "cannot look"
is neither a match nor a mismatch.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from vco_lib import config_projection as _cp
from vco_lib.jsonc_edit import read_object

__all__ = [
    "ABSENT",
    "MISSING",
    "EnvCheck",
    "applied_surfaces",
    "check_env_surfaces",
    "read_json_env_blocks",
    "surface_label",
]

#: ``actual`` of a drift row whose key is not in the surface at all.
MISSING = "<missing>"
#: ``expected`` of a drift row whose key the projection would remove.
ABSENT = "<absent>"

_SHELL_ENV_REL = ".claude/env"


@dataclass(frozen=True)
class EnvCheck:
    """``drift`` rows ``{surface, key, expected, actual}`` and ``unreadable``
    rows ``{surface, path, reason}``. :attr:`verdict` is the tri-state."""

    surfaces: tuple[str, ...]
    drift: list[dict] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)

    @property
    def verdict(self) -> Optional[bool]:
        """``True`` match, ``False`` drift, ``None`` could not look."""
        if self.unreadable:
            return None
        return not self.drift


def applied_surfaces() -> tuple[str, ...]:
    """The surfaces ``apply_project_env`` writes when the caller names none."""
    return tuple(_cp._DEFAULT_SURFACES)


def surface_label(surface: str) -> str:
    """The project-relative file a surface lives in (for reports)."""
    if surface in _cp._JSON_SURFACE_FILES:
        return _cp._JSON_SURFACE_FILES[surface][0]
    if surface == _cp._SURFACE_CLAUDE_ENV:
        return _SHELL_ENV_REL
    raise ValueError(f"unknown env surface {surface!r}")


def _surface_expectation(
    canonical_env: Mapping[str, str], surface: str, *, local_layer: bool = True,
) -> tuple[list[str], dict[str, str]]:
    """``(keys to compare, expected values)`` for ``surface`` (v0.2.104 L5).

    The expected values are the bundle's keys that ``surface`` carries
    (:func:`vco_lib.config_projection.surface_keys` — the writer's own split).
    ``settings.local.json`` is compared over its own keys only (any other key
    in it is the user's); a committable surface over EVERY canonical key, so a
    machine-local key still sitting in ``settings.json`` is drift (``apply``
    moves it). Order: the bundle's first, then the rest sorted.

    ``local_layer`` False (git tracks ``settings.local.json`` or cannot say —
    :func:`vco_lib.settings_local_env.layer_state`): ``apply`` keeps the
    pre-L5 layout, so ``settings.json`` is expected to carry every key and
    ``settings.local.json`` is not compared at all.
    """
    if not local_layer:
        if surface == _cp._SURFACE_CLAUDE_SETTINGS_LOCAL:
            return [], {}
        if surface == _cp._SURFACE_CLAUDE_SETTINGS:
            compared = _cp.list_canonical_keys()
            keys = [k for k in canonical_env if k in compared] + sorted(
                k for k in compared if k not in canonical_env)
            return keys, dict(canonical_env)
    carried = _cp.surface_keys(surface)
    expected = {k: v for k, v in canonical_env.items() if k in carried}
    compared = carried if surface == _cp._SURFACE_CLAUDE_SETTINGS_LOCAL else _cp.list_canonical_keys()
    keys = [k for k in canonical_env if k in compared] + sorted(
        k for k in compared if k not in canonical_env
    )
    return keys, expected


def _row(label: str, key: str, want: Optional[str], have: Any) -> dict:
    return {
        "surface": label,
        "key": key,
        "expected": ABSENT if want is None else want,
        "actual": MISSING if have is None else (have if isinstance(have, str) else repr(have)),
    }


def _check_json(folder: Path, surface: str, canonical_env: Mapping[str, str],
                out: EnvCheck, *, local_layer: bool = True) -> None:
    rel, env_key = _cp._JSON_SURFACE_FILES[surface]
    path = folder / rel
    block: dict = {}
    if path.exists():
        try:
            data, _raw = read_object(path)
        except (OSError, ValueError) as exc:
            out.unreadable.append({"surface": rel, "path": str(path), "reason": str(exc)})
            return
        found = data.get(env_key)
        block = found if isinstance(found, dict) else {}
    keys, expected = _surface_expectation(canonical_env, surface, local_layer=local_layer)
    if surface == _cp._SURFACE_CLAUDE_SETTINGS_LOCAL and keys:
        # A value the user put there is kept by ``apply`` (review R14B B6) —
        # the same rule, so it is not drift.
        from vco_lib.settings_local_env import user_kept_keys

        expected = {**expected, **user_kept_keys(folder, expected, keys)}
    for key in keys:
        want, have = expected.get(key), block.get(key)
        if have != want:
            out.drift.append(_row(rel, key, want, have))


def _check_shell_env(folder: Path, canonical_env: Mapping[str, str], out: EnvCheck) -> None:
    path = folder / _SHELL_ENV_REL
    if path.exists():
        try:
            path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # ValueError: not UTF-8
            out.unreadable.append(
                {"surface": _SHELL_ENV_REL, "path": str(path), "reason": str(exc)})
            return
    keys, expected = _surface_expectation(canonical_env, _cp._SURFACE_CLAUDE_ENV)
    for key in keys:
        want = expected.get(key)
        have = _cp._read_managed_env_canonical_value(path, key)
        if have != want:
            out.drift.append(_row(_SHELL_ENV_REL, key, want, have))


def check_env_surfaces(
    folder: Path,
    canonical_env: Mapping[str, str],
    *,
    surfaces: Optional[Sequence[str]] = None,
) -> EnvCheck:
    """Compare ``folder``'s env surfaces with ``canonical_env``.

    ``canonical_env`` is ``project_env_from_db(...)["canonical_env"]`` for
    the project whose root is ``folder``. ``surfaces`` defaults to
    :func:`applied_surfaces`. Read-only; never raises for a file it cannot
    read (that is an ``unreadable`` row), only for an unknown surface name.
    """
    names = tuple(surfaces) if surfaces is not None else applied_surfaces()
    out = EnvCheck(surfaces=names)
    folder = Path(folder)
    local_layer = True
    if _cp._SURFACE_CLAUDE_SETTINGS_LOCAL in names or _cp._SURFACE_CLAUDE_SETTINGS in names:
        from vco_lib.settings_local_env import USABLE, layer_state

        local_layer = layer_state(folder) == USABLE
    for surface in names:
        if surface in _cp._JSON_SURFACE_FILES:
            _check_json(folder, surface, canonical_env, out, local_layer=local_layer)
        elif surface == _cp._SURFACE_CLAUDE_ENV:
            _check_shell_env(folder, canonical_env, out)
        else:
            raise ValueError(f"unknown env surface {surface!r}")
    return out


# ---------------------------------------------------------------------------
# read-env: the env objects of a project's JSON surfaces, for the launcher
# ---------------------------------------------------------------------------


def read_json_env_blocks(folder: Path) -> dict:
    """The env object of every JSON env surface of ``folder``, read through
    the ONE JSONC reader.

    ``{surface: {"status": "ok"|"missing"|"unreadable", "path": str,
    "env": dict|None, "error": str}}`` — ``env`` is ``{}`` for a readable file
    with no (object) env block. Read-only, never raises.

    v0.2.97: the launcher's two readers of these blocks
    (``project_identity.rs`` ``redetect_project_identity`` and
    ``settings_json_watcher.rs``'s diff-guard) parsed them with a strict JSON
    parser, so a JSONC file read as a parse error; they ask this function
    instead (``python -m vco_lib.env_projection_check read-env``, through
    ``vco_lib_bridge::read_settings_env_blocks``).
    """
    out: dict = {}
    for surface, (rel, env_key) in _cp._JSON_SURFACE_FILES.items():
        path = Path(folder) / rel
        row: dict = {"status": "missing", "path": str(path), "env": None, "error": ""}
        try:
            if path.exists():
                data, _raw = read_object(path)
                found = data.get(env_key)
                row.update(status="ok", env=found if isinstance(found, dict) else {})
        except (OSError, ValueError) as exc:
            row.update(status="unreadable", error=str(exc))
        out[surface] = row
    return out


def _build_parser() -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(prog="python -m vco_lib.env_projection_check")
    sub = parser.add_subparsers(dest="cmd", required=True)
    read = sub.add_parser(
        "read-env", help="print the env objects of the JSON env surfaces (JSONC-aware)")
    read.add_argument("--project-folder", action="append", required=True,
                      help="a project folder; repeat for several")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """One JSON object on stdout on every path (the bridge's wire contract)."""
    args = _build_parser().parse_args(argv)
    folders = {str(f): read_json_env_blocks(Path(f)) for f in args.project_folder}
    print(json.dumps({"ok": True, "folders": folders}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
