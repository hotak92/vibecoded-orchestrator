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
* :func:`render_all` also reaps a rendered file's stale ``.from-upstream-``
  sidecars (v0.2.97, :func:`reap_stale_sidecars`), and the deferral clear
  probe classifies them with the same rule (:func:`is_rendered_sidecar_path`).

Failure mode
------------
A missing / malformed / wrong-version table is FATAL — :class:`RuntimeError`
naming the path. ``vco_lib`` ships with every healthy install, so an
unreadable table means a BROKEN install; degrading to an inline default would
re-introduce the two-language drift this table exists to remove.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
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
# Stale `.from-upstream-` sidecars of a rendered file (v0.2.97)
#
# Before v0.2.95 R1 the launcher's pre-pull 3-way merge conflicted on every
# rendered file at every release and parked upstream's tracked copy beside it
# as `<path>.from-upstream-<sha>`. For a rendered file that copy is the
# placeholder saying "install.py materializes this" — adopting it would REPLACE
# the rendered file, so there is never anything to adopt, yet they accumulated
# one per release. The launcher no longer writes them
# (`git_user_editable_merge.rs`, conflict arm); THIS is the one place that
# removes the old ones, run by `render_all` right after the re-render (which is
# the merge for a rendered file). It is the only implementation: the untracked
# sidecars never affect the pull, so the launcher does not need a pre-pull copy
# of the rule, and install.py runs this on every install and update — GUI or
# CLI, including the first update a pre-v0.2.97 launcher performs.
#
# DATA-SAFETY: a file is removed only on positive evidence that nothing is
# lost — its name is exactly `<basename>.from-upstream-<4..40 hex>` in the
# rendered file's own directory, it is a regular file (never a symlink or a
# directory), and its exact bytes are a blob git already holds (`git
# hash-object --no-filters` + `git cat-file -e`). A sidecar is by construction
# a byte copy of upstream's blob, so one a user edited is left alone. Each
# removal is recorded in the B-F9 auto-resolution trail with its blob id, so
# `git cat-file -p <oid>` restores it.
# ---------------------------------------------------------------------------

#: Filed under the condition whose parked sidecars these are.
SIDECAR_REAP_CONDITION = "orchestrator_user_modified_preserved"
SIDECAR_REAP_ACTION = "removed un-adoptable rendered-file sidecar"

_SIDECAR_MARKER = ".from-upstream-"
_SIDECAR_SHA_RE = re.compile(r"[0-9A-Fa-f]{4,40}")


@dataclass(frozen=True)
class ReapedSidecar:
    """One removed sidecar: root-relative POSIX path + the blob id of its bytes."""

    rel_path: str
    blob_oid: str


def _split_rel(rel_path: str) -> tuple[str, str]:
    """``(directory, basename)`` of a root-relative path, POSIX separators."""
    norm = rel_path.replace("\\", "/")
    head, sep, tail = norm.rpartition("/")
    return (head if sep else ""), tail


def is_sidecar_name_for(name: str, basename: str) -> bool:
    """Is ``name`` exactly ``<basename>.from-upstream-<sha>`` (the shape the
    launcher's ``sidecar_path_for`` writes)? Basename compare is case-folded
    like :func:`is_rendered_path`; the sha must be 4..40 hex digits, so a
    user's own ``CLAUDE.md.from-upstream-notes`` never qualifies."""
    prefix = basename + _SIDECAR_MARKER
    if len(name) <= len(prefix) or name[: len(prefix)].casefold() != prefix.casefold():
        return False
    return _SIDECAR_SHA_RE.fullmatch(name[len(prefix):]) is not None


def is_rendered_sidecar_path(rel_path: str) -> bool:
    """True when root-relative ``rel_path`` is a ``.from-upstream-`` sidecar of
    a RENDERED file. The ONE name rule: the reap below and the deferral clear
    probe (``deferral_probes.is_rendered_file_sidecar``) both ask it."""
    directory, name = _split_rel(rel_path)
    for entry in entries():
        entry_dir, entry_base = _split_rel(entry.path)
        if _normalise(directory) == _normalise(entry_dir) and is_sidecar_name_for(
            name, entry_base
        ):
            return True
    return False


def _blob_oid_if_known_to_git(install_root: Path, path: Path) -> str | None:
    """Blob id of ``path``'s exact bytes, ONLY when git already stores it."""
    from vco_lib.git_meta import run_git

    rc, oid, _err = run_git(install_root, ["hash-object", "--no-filters", "--", str(path)])
    if rc != 0 or not oid:
        return None
    rc, _out, _err = run_git(install_root, ["cat-file", "-e", oid])
    return oid if rc == 0 else None


def _record_reap(install_root: Path, entry: RenderedRootFile, reaped: ReapedSidecar) -> None:
    from vco_lib.deferral_emit import record_auto_resolution

    record_auto_resolution(
        install_root,
        SIDECAR_REAP_CONDITION,
        SIDECAR_REAP_ACTION,
        f"{reaped.rel_path} held upstream's tracked placeholder for a file "
        f"install.py renders from {entry.template}; adopting it would have "
        "replaced the rendered file, so there was nothing to adopt. The bytes "
        f"are still in git: `git cat-file -p {reaped.blob_oid}`",
    )


def reap_stale_sidecars(
    install_root: Path, entry: RenderedRootFile
) -> tuple[ReapedSidecar, ...]:
    """Remove (and record) the stale sidecars parked beside ``entry``'s file.

    Never raises: a per-file doubt or failure leaves that file in place.
    """
    directory, basename = _split_rel(entry.path)
    parent = install_root / directory if directory else install_root
    try:
        candidates = sorted(p for p in parent.iterdir() if is_sidecar_name_for(p.name, basename))
    except OSError:
        return ()
    reaped: list[ReapedSidecar] = []
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        oid = _blob_oid_if_known_to_git(install_root, path)
        if oid is None:
            continue  # bytes unknown to git (hand-edited?) — keep it
        try:
            path.unlink()
        except OSError:
            continue
        rel = f"{directory}/{path.name}" if directory else path.name
        item = ReapedSidecar(rel, oid)
        try:
            _record_reap(install_root, entry, item)
        except Exception:  # noqa: BLE001 — the outcome detail still reports it
            pass
        reaped.append(item)
    return tuple(reaped)


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
    ``reaped_sidecars`` names the stale ``.from-upstream-`` sidecars
    :func:`render_all` removed after a successful render (v0.2.97); ``detail``
    says so too, which is how the installer's print + log line shows it.
    """

    path: str
    status: str
    detail: str
    reaped_sidecars: tuple[str, ...] = ()

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


#: Statuses after which the file on disk IS the fresh render.
_RENDERED_OK = ("created", "auto_block_updated", "full_rewrite")


def render_all(install_root: Path) -> tuple[RenderOutcome, ...]:
    """Render every table entry into ``install_root``, in table order.

    After an entry renders successfully its stale ``.from-upstream-`` sidecars
    are reaped (see :func:`reap_stale_sidecars`) — the re-render is the merge
    for a rendered file, so this is the moment they are provably redundant.
    """
    outcomes: list[RenderOutcome] = []
    for entry in entries():
        outcome = render_entry(install_root, entry)
        if outcome.status in _RENDERED_OK:
            reaped = reap_stale_sidecars(install_root, entry)
            if reaped:
                names = ", ".join(r.rel_path for r in reaped)
                outcome = replace(
                    outcome,
                    detail=(
                        f"{outcome.detail}; removed {len(reaped)} un-adoptable "
                        f"upstream sidecar(s): {names} (recorded in "
                        ".claude/logs/auto-resolutions.jsonl)"
                    ),
                    reaped_sidecars=tuple(r.rel_path for r in reaped),
                )
        outcomes.append(outcome)
    return tuple(outcomes)


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
