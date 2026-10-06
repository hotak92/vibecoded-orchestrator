# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Opt-in agent/skill PACKS — the one home of pack behaviour (v0.2.101,
catalogue plan §3).

The definitions live in ONE committed table, ``templates/packs/packs.toml``;
this module is the only parser and the only behaviour:

* :func:`load_packs` — parse + validate the table. A broken table is a
  BROKEN INSTALL: every failure raises :class:`PacksTableError` loudly; no
  fallback list, no silent skip.
* :func:`iter_pack_ops` — bundle ops for pack members. Members ship to the
  SAME destinations as default agents/skills (``.claude/agents/<name>.md``,
  ``.claude/skills/<name>/…`` + companions) through the SAME materializer
  transforms, so every engine rule — hash compare, adoption, skip-disabled,
  ``--skip-kind agents|skills``, orphan handling — applies unchanged.
* :func:`remove_packs` — the ``--remove-pack`` pass. Mirrors
  ``bundle_leftovers.retire_compose_copies``: an unmodified member (manifest
  hash match) is deleted outright; an edited one is backed up under
  ``.claude/backups/bundle-adoptions/<ts>/`` FIRST and only then deleted —
  never a silent delete, and on any backup failure the member is LEFT IN
  PLACE with a warning (the ``_backup_and_remove`` contract). Disabled-side
  copies (``{agents,skills}.disabled/``) are removed under the same rule.
* ``python -m vco_lib.packs status --folder <path> --json`` — the launcher's
  Packs-tab bridge (plan §3.6). The wire contract is the ONE committed
  fixture ``tests/fixtures/packs_status_contract.json``: success is
  ``{"ok": true, "packs": [{"name", "description", "members", "installed"}]}``
  (``members`` = agent file stems + skill directory names; ``installed`` =
  recorded in the project manifest's ``packs`` map), refusal is
  ``{"ok": false, "error": str, "message": str}`` — a loud error, never a
  degraded listing. The Rust side
  (``launcher/src-tauri/src/commands/packs_cmd.rs``) parses only this JSON
  and never mirrors the table.

Manifest integration (plan §3.2): the bundle manifest gains an additive
``packs`` map ``{name: {"installed_at": <ISO>}}``; readers default it to
``{}`` for older manifests (the ``preserved_files`` forward-compat pattern —
schema_version stays 2 per the tree's additive-key precedent). A plain
``install-bundle --update`` enumerates every recorded pack, so installed
packs stay current with zero extra flags.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from vco_lib.bundle_kinds import bundle_op_kind, disabled_counterpart
from vco_lib.paths import to_posix_rel

__all__ = [
    "PACKS_TABLE_REL",
    "Pack",
    "PackChoices",
    "PackRemoval",
    "PacksTableError",
    "default_orchestrator_root",
    "effective_packs",
    "iter_pack_ops",
    "load_packs",
    "main",
    "member_names",
    "note_packs_installed",
    "refusal_payload",
    "remove_packs",
    "root_from_argv",
    "status_payload",
]

#: The one committed pack table, relative to the orchestrator root.
PACKS_TABLE_REL = "templates/packs/packs.toml"

#: Refusal `error` codes for the status contract (fixture-pinned).
ERR_TABLE_UNREADABLE = "packs_table_unreadable"
ERR_TABLE_INVALID = "packs_table_invalid"

_PACK_PREFIX = "templates/packs/"


class PacksTableError(RuntimeError):
    """The pack table is missing, unparseable, or violates its invariants.

    A broken table is a broken install — callers surface this loudly (the
    status CLI as a refusal reply, the bundle engine as an ``errors[]`` row);
    nobody degrades to an empty catalogue silently.
    """


@dataclass(frozen=True)
class Pack:
    """One ``[pack.<name>]`` block of the table."""

    name: str
    description: str
    #: Member paths relative to ``templates/packs/`` (table order).
    members: tuple[str, ...]


@dataclass
class PackRemoval:
    """What one ``--remove-pack`` pass did (the ``LeftoverOutcome`` shape)."""

    #: ``(dest_rel, backup_rel | None, detail)`` per removed file.
    removed: list = field(default_factory=list)
    #: ``(dest_rel, error)`` per file that could NOT be removed.
    errors: list = field(default_factory=list)
    #: Human lines for the result envelope's ``notes``.
    notes: list = field(default_factory=list)
    #: Warnings for the result envelope (skipped kinds, failed backups).
    warnings: list = field(default_factory=list)
    #: Pack names whose record was dropped from the manifest.
    packs_dropped: list = field(default_factory=list)


def default_orchestrator_root() -> Path:
    """The orchestrator root for a bare ``python -m vco_lib.packs`` call.

    One home: the same module-relative walk the bundle engine's CLI uses
    (``project_init._find_orchestrator_root_from_module`` — vct-module.json
    upward search). Lazy import: this module is imported BY project_init's
    engine path, and the CLI subprocess pays for the engine module once.
    """
    from vco_lib.project_init import _find_orchestrator_root_from_module

    return _find_orchestrator_root_from_module()


def _member_name(member: str) -> str:
    """The installed NAME of a member path: agent stem or skill dir name."""
    parts = Path(member).parts
    return parts[2][:-3] if parts[1] == "agents" else parts[2]


def member_names(pack: Pack) -> list[str]:
    """Status-contract ``members``: agent file stems + skill directory
    names, in table order (the fixture pins two packs' exact lists)."""
    return [_member_name(m) for m in pack.members]


def load_packs(orchestrator_root: Path) -> dict[str, Pack]:
    """Parse + validate ``templates/packs/packs.toml`` (table order kept).

    Raises :class:`PacksTableError` — loudly, never an empty catalogue —
    when the table is missing/unparseable, a member is malformed or absent
    on disk, a member appears in two packs (by path OR by installed name),
    or a member name collides with a DEFAULT-shipped agent/skill (a double
    delivery would make two template trees claim one destination).
    """
    root = Path(orchestrator_root)
    table_path = root / PACKS_TABLE_REL
    try:
        parsed = tomllib.loads(table_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PacksTableError(
            f"{PACKS_TABLE_REL} is missing or unreadable ({e}) — the install "
            "is broken; re-run the orchestrator update") from e
    except tomllib.TOMLDecodeError as e:
        raise PacksTableError(
            f"{PACKS_TABLE_REL} is unparseable ({e}) — the install is broken; "
            "re-run the orchestrator update") from e

    raw_packs = parsed.get("pack")
    if not isinstance(raw_packs, dict):
        raise PacksTableError(
            f"{PACKS_TABLE_REL} has no [pack.<name>] tables — the install is "
            "broken; re-run the orchestrator update")

    packs: dict[str, Pack] = {}
    seen_paths: dict[str, str] = {}   # member path -> pack
    seen_names: dict[str, str] = {}   # installed name -> pack
    for name, raw in raw_packs.items():
        if not isinstance(raw, dict):
            raise PacksTableError(f"[pack.{name}] must be a table")
        description = str(raw.get("description", ""))
        members = raw.get("members", [])
        if not isinstance(members, list) or not members or \
                any(not isinstance(m, str) or not m for m in members):
            raise PacksTableError(
                f"[pack.{name}] needs a non-empty `members` list of paths")
        for member in members:
            parts = Path(member).parts
            if (len(parts) < 3 or parts[0] != name
                    or parts[1] not in ("agents", "skills")
                    or (parts[1] == "agents"
                        and (len(parts) != 3 or not parts[2].endswith(".md")))
                    or (parts[1] == "skills"
                        and (len(parts) != 4 or parts[3] != "SKILL.md"))):
                raise PacksTableError(
                    f"[pack.{name}] member {member!r} is malformed — expected "
                    f"{name}/agents/<agent>.md or {name}/skills/<skill>/SKILL.md")
            if not (root / _PACK_PREFIX / member).is_file():
                raise PacksTableError(
                    f"[pack.{name}] member {member!r} does not exist under "
                    f"{_PACK_PREFIX} — every member MUST exist")
            if member in seen_paths:
                raise PacksTableError(
                    f"member {member!r} is in two packs "
                    f"({seen_paths[member]} and {name})")
            installed_name = _member_name(member)
            if installed_name in seen_names:
                raise PacksTableError(
                    f"member name {installed_name!r} is claimed by two packs "
                    f"({seen_names[installed_name]} and {name}) — one "
                    "destination can only have one owner")
            seen_paths[member] = name
            seen_names[installed_name] = name
        packs[name] = Pack(name=name, description=description,
                           members=tuple(members))

    # No double delivery with the DEFAULT catalogue (the destinations collide
    # even though the template paths differ).
    default_names: dict[str, str] = {}
    for agents_dir in ("templates/agents/free", "templates/agents/module-gateway"):
        src = root / agents_dir
        if src.is_dir():
            for f in src.glob("*.md"):
                default_names[f.stem] = agents_dir
    skills_src = root / "templates" / "skills"
    if skills_src.is_dir():
        for d in skills_src.iterdir():
            if d.is_dir():
                default_names[d.name] = "templates/skills"
    for installed_name, pack_name in seen_names.items():
        if installed_name in default_names:
            raise PacksTableError(
                f"pack {pack_name!r} member {installed_name!r} is also shipped "
                f"by default ({default_names[installed_name]}) — a member must "
                "not be delivered twice")
    return packs


def effective_packs(
    manifest_packs: Iterable[str], add: Iterable[str], remove: Iterable[str],
) -> list[str]:
    """The pack set one run enumerates: recorded ∪ requested − removed
    (plan §3.3/§3.4). Pure; sorted for a deterministic op order."""
    return sorted((set(manifest_packs) | set(add)) - set(remove))


def _skill_member_files(source_dir: Path) -> list[Path]:
    """Every file of a skill member directory (companions ship with the
    SKILL.md), `__pycache__` excluded — the default-skills loop's rule."""
    return [
        f for f in sorted(source_dir.rglob("*"))
        if f.is_file() and "__pycache__" not in f.relative_to(source_dir).parts
    ]


def member_dest_rels(orchestrator_root: Path, pack: Pack) -> list[str]:
    """The bundle dest_rels (POSIX) a pack's members install to — the
    ``--remove-pack`` candidate set's table half (the manifest half is the
    entries whose ``source`` is under the pack's template dir)."""
    root = Path(orchestrator_root)
    out: list[str] = []
    for member in pack.members:
        parts = Path(member).parts
        if parts[1] == "agents":
            out.append(f".claude/agents/{parts[2]}")
        else:
            src = root / _PACK_PREFIX / member
            for f in _skill_member_files(src.parent):
                out.append(to_posix_rel(str(
                    Path(".claude") / "skills" / parts[2]
                    / f.relative_to(src.parent))))
    return out


def iter_pack_ops(
    orchestrator_root: Path,
    project_root: Optional[Path],
    pack_names: Sequence[str],
    *,
    project_name: Optional[str] = None,
    sink: "Any" = None,
) -> list:
    """Bundle ops for every member of ``pack_names`` (plan §3.2).

    dest_rel shapes are IDENTICAL to the default kinds, so
    ``_classify_bundle_op_kind`` / ``_bundle_op_kind`` classify pack ops as
    ordinary agents/skills and ``_file_action``, adoption, skip-disabled and
    ``--skip-kind`` all apply unchanged. Same ``{{…}}`` materializer as the
    default agents/skills (``.md`` files only; skill companions byte-copy).
    Unknown names are ignored HERE (the engine validates against the table
    and reports loudly — this stays a pure enumerator).
    """
    from vco_lib import materialize as _mz
    from vco_lib.project_init import _BundleFileOp

    root = Path(orchestrator_root)
    table = load_packs(root)
    context = _mz.MaterializeContext(root, project_root, project_name=project_name)

    def _subs(dest_rel: str) -> "_mz.Transform":
        return _mz.Transform(dest_rel, _mz.BUNDLE_MARKDOWN_SPEC, context, sink=sink)

    ops: list = []
    for pack_name in pack_names:
        pack = table.get(pack_name)
        if pack is None:
            continue
        for member in pack.members:
            parts = Path(member).parts
            src = root / _PACK_PREFIX / member
            if parts[1] == "agents":
                dest_rel = str(Path(".claude") / "agents" / parts[2])
                files = [(src, dest_rel)]
            else:
                files = [
                    (f, str(Path(".claude") / "skills" / parts[2]
                            / f.relative_to(src.parent)))
                    for f in _skill_member_files(src.parent)
                ]
            for source_abs, dest in files:
                ops.append(_BundleFileOp(
                    dest_rel=dest,
                    source_abs=source_abs,
                    source_rel=to_posix_rel(str(source_abs.relative_to(root))),
                    transform=_subs(dest) if source_abs.suffix == ".md" else None,
                    always_overwrite=False,
                ))
    return ops


def remove_packs(
    folder: Path,
    orchestrator_root: Path,
    manifest: dict,
    names: Iterable[str],
    table: dict[str, Pack],
    *,
    skip_kinds: frozenset = frozenset(),
    dry_run: bool = False,
    backup_ts: Callable[[], str],
) -> PackRemoval:
    """The ``--remove-pack`` pass (plan §3.5) — mirrors
    ``bundle_leftovers.retire_compose_copies`` exactly.

    For each member of each named pack: manifest-hash match → delete; bytes
    differ (or no entry) → back up under
    ``.claude/backups/bundle-adoptions/<ts>/`` THEN delete; on any backup
    failure → leave in place + warning row (never remove bytes without the
    promised copy). A member sitting at ``{agents,skills}.disabled/`` is
    removed under the same rule (the disable choice does not make a VCO file
    the user's). Members whose kind is skipped this run are left entirely
    alone (file AND manifest entry — the skip-kind contract).

    Mutates ``manifest`` in place: removed members' ``files`` entries and
    fully-removed packs' ``packs`` records are dropped; the caller's manifest
    write commits the result. In ``dry_run`` nothing is mutated — the report
    carries what WOULD happen. Emits one ``pack_removed``
    ``informational_record`` ledger row per run that removed something.
    """
    from vco_lib.bundle_leftovers import _backup_and_remove
    from vco_lib.hashing import sha256_file

    folder = Path(folder)
    outcome = PackRemoval()
    names = sorted(set(names))
    if not names:
        return outcome
    # Manifest keys can be Windows-shaped; removal compares POSIX.
    files: dict = manifest.get("files") or {}
    key_by_posix = {to_posix_rel(k): k for k in files}
    _raw_packs = manifest.get("packs")
    packs_map: dict = _raw_packs if isinstance(_raw_packs, dict) else {}

    for name in names:
        pack = table.get(name)
        candidates: list[str] = (member_dest_rels(orchestrator_root, pack)
                                 if pack else [])
        # The manifest half: entries whose shipped source is this pack's tree
        # (survives a member renamed in the table since install).
        prefix = f"{_PACK_PREFIX}{name}/"
        for rel_posix, key in key_by_posix.items():
            if to_posix_rel(str((files.get(key) or {}).get("source", ""))) \
                    .startswith(prefix) and rel_posix not in candidates:
                candidates.append(rel_posix)
        errors_here = 0
        skipped_here: set = set()
        for rel in sorted(candidates):
            kind = bundle_op_kind(rel)
            if kind is not None and kind in skip_kinds:
                skipped_here.add(kind)
                continue
            entry = files.get(key_by_posix.get(rel, rel)) or {}
            prior_hash = entry.get("sha256", "")
            manifest_key = key_by_posix.get(rel, rel)
            targets = [rel]
            dis = disabled_counterpart(rel)
            if dis:
                targets.append(dis)
            rel_failed = False
            for target_rel in targets:
                target = folder / target_rel
                if target.is_symlink() or not target.is_file():
                    continue
                try:
                    modified = prior_hash == "" or sha256_file(target) != prior_hash
                except OSError as exc:
                    outcome.errors.append((target_rel, f"{type(exc).__name__}: {exc}"))
                    errors_here += 1
                    rel_failed = True
                    continue
                detail = (f"pack `{name}` — your edited copy: backed up, then "
                          "removed" if modified else
                          f"pack `{name}` — unmodified VCO copy: removed")
                if dry_run:
                    outcome.removed.append((target_rel, None, detail))
                    continue
                try:
                    backup_rel = _backup_and_remove(folder, target_rel,
                                                    backup_ts(), backup=modified)
                except Exception as exc:  # noqa: BLE001 — left in place, warned
                    outcome.errors.append(
                        (target_rel, f"{type(exc).__name__}: {exc}"))
                    errors_here += 1
                    rel_failed = True
                    continue
                outcome.removed.append((target_rel, backup_rel, detail))
            if not dry_run and not rel_failed:
                files.pop(manifest_key, None)
                key_by_posix.pop(rel, None)
        if dry_run:
            continue
        if errors_here:
            outcome.warnings.append(
                f"pack `{name}`: {errors_here} member file(s) could not be "
                "removed (see warnings) — the pack record is kept so the next "
                "--remove-pack retries")
        elif skipped_here:
            outcome.warnings.append(
                f"pack `{name}`: kind(s) {sorted(skipped_here)} were skipped "
                "this run — those members (and their manifest entries) are "
                "left untouched; the pack record is kept")
        else:
            packs_map.pop(name, None)
            outcome.packs_dropped.append(name)
    if not dry_run:
        manifest["packs"] = packs_map
        manifest["files"] = files
    if outcome.removed and not dry_run:
        _emit_pack_removed(folder, names, outcome)
    return outcome


def _emit_pack_removed(folder: Path, names: Sequence[str],
                       outcome: PackRemoval) -> None:
    """One ``informational_record`` ledger row for the run (plan §3.5)."""
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralEntry

    lines = "\n".join(
        f"- `{rel}` — {detail}" + (f"; backup: `{bk}`" if bk else "")
        for rel, bk, detail in outcome.removed
    )
    _de.emit(folder, DeferralEntry(
        condition_id="pack_removed",
        title=f"Pack(s) removed: {', '.join(names)}",
        detected=f"{len(outcome.removed)} file(s):\n{lines}",
        why_deferred=(
            "You asked for this pack to be removed; the removal is complete. "
            "Unmodified members were deleted; members you had edited were "
            "backed up under .claude/backups/bundle-adoptions/ first, so no "
            "bytes were lost."),
        command_to_apply=(
            "# Nothing to do. To bring the pack back:\n"
            "#   python -m vco_lib.project_init install-bundle --folder "
            "<this project> --update --pack <name>\n"
            "# To restore one edited file from its backup:\n"
            "#   cp <backup> <file>"),
        severity="info",
    ))


def note_packs_installed(folder: Path, installed: Sequence[str],
                         *, log: Optional[Callable[..., None]] = None) -> None:
    """The paired resolution of ``pack_removed`` (its registry
    ``clear_probe``): re-installing a pack is the success path that retires
    the removal record. Best-effort, like every deferral write."""
    if not installed:
        return
    from vco_lib import deferral_emit as _de

    _de.record_auto_resolution(
        folder, "pack_removed", "pack_reinstalled",
        f"installed pack(s): {', '.join(sorted(installed))}", log=log)
    _de.resolve_conditions(folder, ["pack_removed"], log=log)


def status_payload(orchestrator_root: Path, folder: Path) -> dict:
    """The ``status --json`` success reply (fixture-pinned contract).

    Raises :class:`PacksTableError` — the CLI turns it into the refusal
    reply; a broken table never degrades into an empty listing.
    """
    table = load_packs(Path(orchestrator_root))
    from vco_lib.project_init import _read_manifest

    manifest = _read_manifest(Path(folder))
    recorded = manifest.get("packs")
    installed = recorded if isinstance(recorded, dict) else {}
    return {
        "ok": True,
        "packs": [
            {
                "name": pack.name,
                "description": pack.description,
                "members": member_names(pack),
                "installed": pack.name in installed,
            }
            for pack in table.values()
        ],
    }


def refusal_payload(error: str, message: str) -> dict:
    """The ``status --json`` refusal reply (fixture-pinned contract)."""
    return {"ok": False, "error": error, "message": message}


def root_from_argv(argv: Optional[Sequence[str]] = None) -> Path:
    """The orchestrator root a pack CLI invocation will actually use: the
    ``--orchestrator-root`` on the command line when present (the launcher
    always passes it), else the module-walk default. Root-correct ``choices``
    for ``--pack``/``--remove-pack``: a non-default clone's table gates its
    own pack names, never the module-walk clone's."""
    args = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(args):
        if a == "--orchestrator-root" and i + 1 < len(args):
            return Path(args[i + 1]).resolve()
        if a.startswith("--orchestrator-root="):
            return Path(a.split("=", 1)[1]).resolve()
    return default_orchestrator_root()


class PackChoices:
    """Lazy ``choices=`` container for ``--pack`` / ``--remove-pack``.

    argparse checks membership at PARSE time, so the table is read only when
    an install-bundle invocation actually carries a pack flag (and once per
    container, cached). A broken table refuses every name — loudly: the reason
    goes to stderr before argparse prints its generic invalid-choice error.
    The container never raises at parser-BUILD time, so a broken table cannot
    brick the other project_init subcommands.

    ``root_resolver`` defaults to :func:`root_from_argv` (honours an explicit
    ``--orchestrator-root``); tests inject a fixed root.
    """

    def __init__(self, root_resolver: Optional[Callable[[], Path]] = None) -> None:
        self._resolve = root_resolver or root_from_argv
        self._names: Optional[tuple[str, ...]] = None

    def _load(self) -> tuple[str, ...]:
        if self._names is None:
            try:
                self._names = tuple(load_packs(self._resolve()))
            except PacksTableError as exc:
                sys.stderr.write(f"[vct] {exc}\n")
                self._names = ()
        return self._names

    def __iter__(self):
        return iter(self._load())

    def __contains__(self, value: object) -> bool:
        return value in self._load()


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m vco_lib.packs status --folder <path> [--json]
    [--orchestrator-root <path>]`` — the launcher bridge verb."""
    parser = argparse.ArgumentParser(prog="python -m vco_lib.packs")
    sub = parser.add_subparsers(dest="command", required=True)
    p_status = sub.add_parser(
        "status",
        help="Emit the pack catalogue + per-project install state as JSON.",
    )
    p_status.add_argument("--folder", required=True,
                          help="Project folder whose manifest is read.")
    p_status.add_argument("--orchestrator-root", default=None,
                          help="Orchestrator clone root (default: walk up "
                               "from this module looking for vct-module.json).")
    p_status.add_argument("--json", action="store_true",
                          help="Emit the contract JSON on stdout (the "
                               "launcher bridge always sets this).")
    args = parser.parse_args(argv)

    root = (Path(args.orchestrator_root).resolve() if args.orchestrator_root
            else default_orchestrator_root())
    try:
        payload = status_payload(root, Path(args.folder).resolve())
    except PacksTableError as exc:
        error = (ERR_TABLE_UNREADABLE if "unparseable" in str(exc)
                 or "unreadable" in str(exc) or "no [pack." in str(exc)
                 else ERR_TABLE_INVALID)
        reply = refusal_payload(error, str(exc))
        print(json.dumps(reply))
        return 1
    if args.json:
        print(json.dumps(payload))
        return 0
    for row in payload["packs"]:
        state = "installed" if row["installed"] else "not installed"
        print(f"{row['name']} ({state}) — {row['description']}")
        for member in row["members"]:
            print(f"  - {member}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
