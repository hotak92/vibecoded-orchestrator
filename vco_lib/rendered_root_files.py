# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Loader for ``rendered_root_files.toml`` — the cross-language table of
orchestrator-ROOT paths the install RENDERS (v0.2.95 R1).

Python side of a tier-(B) shared-config loader (CLAUDE.md "Share, don't
mirror, cross-language logic"). The Rust side lives in
``launcher/src-tauri/src/commands/git_user_editable_merge.rs`` (embedded with
``include_str!``); both parse the SAME ``vco_lib/rendered_root_files.toml``
with the SAME semantics, and ``tests/test_v0295_rendered_file_conflicts.py``
holds them in lockstep.

Who consumes what
-----------------
* :func:`render_all` ITERATES :func:`entries`, and install.py's step-4c shim
  (``_materialize_orchestrator_self_claude_md`` — a CLAUDE.md-specific name
  kept for its callers and tests while the table holds one entry) calls it.
  No second list is kept beside the renderer: the table IS what it renders,
  so "the rendered set" is enumerable from the renderer itself.
* The launcher's pre-pull ``resolve_rendered_files_keep_local`` classifies a
  divergent path with :func:`is_rendered_path` semantics (normalise ``\\`` to
  ``/``, case-insensitive compare) so a rendered file never reaches the
  divergence modal.

Failure mode
------------
A missing / malformed / wrong-version table is FATAL — :class:`RuntimeError`
naming the path. ``vco_lib`` ships with every healthy install, so an
unreadable table means a BROKEN install; degrading to an inline default would
re-introduce the two-language drift this table exists to remove.
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: The format version this loader understands. Bumped in lockstep with the
#: table and the Rust loader's ``SUPPORTED_FORMAT_VERSION``.
SUPPORTED_FORMAT_VERSION = 1

TABLE_PATH = Path(__file__).resolve().parent / "rendered_root_files.toml"


@dataclass(frozen=True)
class RenderedRootFile:
    """One rendered orchestrator-root path.

    Attributes mirror the table keys 1:1 (see the .toml header for the
    authoritative semantics).
    """

    path: str
    template: str
    begin_marker: str
    end_marker: str
    substitutions: tuple[str, ...] = field(default=())


def _normalise(rel_path: str) -> str:
    """Windows-safe, case-folded form used for every comparison.

    ``\\`` to ``/`` (the v0.2.81 B1 separator lesson) plus ``casefold`` so the
    table matches on case-folding filesystems (HFS+/APFS/NTFS). The Rust side
    normalises identically.
    """
    return rel_path.replace("\\", "/").casefold()


def _parse(text: str, *, source: str) -> tuple[RenderedRootFile, ...]:
    """Parse table TEXT into entries. Raises ``RuntimeError`` on any defect."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{source} is not valid TOML: {exc}") from exc

    version = data.get("format_version")
    if version != SUPPORTED_FORMAT_VERSION:
        raise RuntimeError(
            f"{source}: format_version {version!r} is not the supported "
            f"{SUPPORTED_FORMAT_VERSION} — loader and table must be bumped together"
        )

    state_file = data.get("state_file")
    if not isinstance(state_file, str) or not state_file:
        raise RuntimeError(f"{source}: state_file must be a non-empty string")

    rows = data.get("rendered", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{source}: [[rendered]] must hold at least one entry")

    parsed: list[RenderedRootFile] = []
    seen: set[str] = set()
    for row in rows:
        try:
            entry = RenderedRootFile(
                path=str(row["path"]),
                template=str(row["template"]),
                begin_marker=str(row["begin_marker"]),
                end_marker=str(row["end_marker"]),
                substitutions=tuple(str(s) for s in row.get("substitutions", [])),
            )
        except KeyError as exc:
            raise RuntimeError(
                f"{source}: [[rendered]] entry {row!r} is missing key {exc}"
            ) from exc
        key = _normalise(entry.path)
        if key in seen:
            raise RuntimeError(
                f"{source}: duplicate rendered path {entry.path!r} — one entry per path"
            )
        seen.add(key)
        parsed.append(entry)
    return tuple(parsed)


def _table_text() -> str:
    try:
        return TABLE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the rendered-root-files table at {TABLE_PATH}: {exc} — "
            "this means a BROKEN install (vco_lib ships with every install); "
            "re-run 'python install.py --update'"
        ) from exc


def entries() -> tuple[RenderedRootFile, ...]:
    """Every rendered orchestrator-root path, in table order."""
    return _parse(_table_text(), source=str(TABLE_PATH))


def state_file_rel_path() -> str:
    """Relative path (POSIX separators) of the reconcile hand-off state file.

    The launcher's pre-pull reconcile WRITES it; install.py's renderer READS
    and deletes it. Both sides take the path from the table so the two cannot
    drift into writing/reading different files.
    """
    data = tomllib.loads(_table_text())
    state = data.get("state_file")
    if not isinstance(state, str) or not state:
        raise RuntimeError(f"{TABLE_PATH}: state_file must be a non-empty string")
    return state


def rendered_paths() -> tuple[str, ...]:
    """Just the paths, table order (what the Rust classifier compares against)."""
    return tuple(e.path for e in entries())


def is_rendered_path(rel_path: str) -> bool:
    """True when ``rel_path`` (relative to the orchestrator root) is rendered."""
    target = _normalise(rel_path)
    return any(_normalise(e.path) == target for e in entries())


# ---------------------------------------------------------------------------
# Rendering + the launcher hand-off (v0.2.95 R1)
#
# The bodies live HERE rather than in install.py for the reason install.py's
# own ratchet states: the monolith must shrink, not grow. install.py keeps the
# thin step-4c shim (its print/log vocabulary and the DeferralEntry it owns);
# everything below is plain data-in/data-out and is unit-testable without
# importing the installer.
# ---------------------------------------------------------------------------

#: Placeholder names :func:`render_entry` knows how to resolve. A table entry
#: naming anything else fails that entry loudly — the renderer never writes a
#: file carrying a literal ``{{NAME}}``.
_KNOWN_SUBSTITUTIONS = ("ORCHESTRATOR_ROOT",)


@dataclass(frozen=True)
class RenderOutcome:
    """Result of rendering ONE entry.

    ``status`` is one of ``created`` / ``auto_block_updated`` / ``full_rewrite``
    / ``template_missing`` / ``unknown_substitution`` / ``failed``; ``detail``
    is the human-readable suffix the installer prints and logs.
    """

    path: str
    status: str
    detail: str

    @property
    def is_failure(self) -> bool:
        return self.status in ("failed", "unknown_substitution")


def render_entry(install_root: Path, entry: RenderedRootFile) -> RenderOutcome:
    """Render one entry into ``install_root``. Never raises.

    The template's placeholders are substituted, then the body is written
    between the entry's AUTO markers: on a re-render ONLY that block changes,
    so anything the user wrote outside the markers is preserved (that text is
    uncommitted and exists nowhere else, which is why it is protected all the
    way through the update).
    """
    template_path = install_root / Path(entry.template)
    target_path = install_root / Path(entry.path)

    if not template_path.is_file():
        return RenderOutcome(
            entry.path, "template_missing", f"SKIP (template missing: {entry.template})"
        )

    unknown = [s for s in entry.substitutions if s not in _KNOWN_SUBSTITUTIONS]
    if unknown:
        return RenderOutcome(
            entry.path,
            "unknown_substitution",
            f"FAILED (unknown substitution(s) {', '.join(unknown)} — nothing written)",
        )

    values = {"ORCHESTRATOR_ROOT": str(install_root)}
    try:
        rendered = template_path.read_text(encoding="utf-8")
        for name in entry.substitutions:
            rendered = rendered.replace("{{" + name + "}}", values[name])

        if target_path.is_file():
            existing = target_path.read_text(encoding="utf-8")
            begin_idx = existing.find(entry.begin_marker)
            end_idx = existing.find(entry.end_marker)
            if begin_idx >= 0 and end_idx > begin_idx:
                merged = (
                    existing[:begin_idx]
                    + rendered.rstrip()
                    + existing[end_idx + len(entry.end_marker):]
                )
                if merged != existing:  # avoid a no-op write that bumps mtime
                    target_path.write_text(merged, encoding="utf-8")
                return RenderOutcome(
                    entry.path, "auto_block_updated", "OK (AUTO block updated)"
                )
            # No markers: they ARE the "preserve me" contract, so without them
            # the template is the source of truth.
            target_path.write_text(rendered, encoding="utf-8")
            return RenderOutcome(
                entry.path, "full_rewrite", "OK (full rewrite — no AUTO markers found)"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(rendered, encoding="utf-8")
        return RenderOutcome(entry.path, "created", "OK (created)")
    except OSError as exc:
        return RenderOutcome(entry.path, "failed", f"FAILED ({exc})")


def render_all(install_root: Path) -> tuple[RenderOutcome, ...]:
    """Render every table entry into ``install_root``, in table order."""
    return tuple(render_entry(install_root, entry) for entry in entries())


def state_file_path(install_root: Path) -> Path:
    """Absolute path of the launcher's hand-off state file for this root."""
    return install_root / Path(state_file_rel_path())


def read_reconcile_state(install_root: Path) -> dict | None:
    """Parse the launcher's hand-off state file, or ``None`` when absent.

    Raises ``ValueError`` when the file exists but cannot be parsed, so the
    caller can leave it in place for the next run rather than silently
    discarding evidence of what the reconcile did.
    """
    path = state_file_path(install_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return payload


def consume_reconcile_state(install_root: Path) -> None:
    """Delete the hand-off state file (best-effort).

    Deleting it is what makes the ``rendered_file_upstream_changed`` row drain:
    install.py OWNS that condition, so the next ``--update`` does not re-detect
    it and the run's finalize drops it.
    """
    try:
        state_file_path(install_root).unlink()
    except OSError:
        pass


def build_upstream_changed_deferral_text(payload: dict) -> tuple[str, str, str, str] | None:
    """Build (title, detected, why_deferred, command_to_apply) from the state.

    Returns ``None`` when the record names no files — nothing happened, so no
    row should be written.
    """
    files = [str(f.get("path", "")) for f in payload.get("files", []) if f.get("path")]
    if not files:
        return None
    base = str(payload.get("base", ""))[:12]
    theirs = str(payload.get("theirs", ""))[:12]
    branch = str(payload.get("branch", "")) or "the pull branch"
    file_list = ", ".join(sorted(files))
    title = "Rendered file changed upstream; local copy kept and re-rendered"
    detected = (
        f"Upstream changed the tracked copy of {len(files)} file(s) this install "
        f"RENDERS: {file_list} (upstream range {base}..{theirs} on {branch}).\n\n"
        "The update resolved it without a divergence modal: the tracked blob was "
        "advanced to upstream's, YOUR rendered working-tree copy was left in place, "
        "and this run re-rendered the AUTO block from the NEW template. Content you "
        "wrote OUTSIDE the AUTO markers was preserved."
    )
    why_deferred = (
        "Nothing is pending — this row records an action already completed (class "
        "informational_record). It is listed so an upstream change to a file you also "
        "own stays visible, not because it needs you. It disappears on the next "
        "`python install.py --update`."
    )
    command_to_apply = (
        "# Nothing to run. The upstream history of the tracked copy is in your clone; "
        "inspect it with your usual log/diff commands."
    )
    return title, detected, why_deferred, command_to_apply
