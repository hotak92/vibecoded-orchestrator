# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Named, READ-ONLY clear probes for deferral conditions (v0.2.91 WP-B).

A probe answers exactly one question about ONE condition: *does it still
apply?*

    True   — still applies. KEEP the entry.
    False  — the condition is provably over. RESOLVE the entry.
    None   — could not determine. KEEP the entry.

The tri-state matters. ``False`` deletes a record the user may be relying on, so
it is only ever returned on POSITIVE evidence; every "the check itself failed"
path returns ``None``, never ``False``. This is the same discipline the
``hub_restart_failed_after_abort`` handler settled on after the v0.2.89 review
(MAJOR-2): *never wrongly clear an actionable failure*.

Probes are READ-ONLY by construction — no process is started, nothing is
written, nothing is repaired. That is what let v0.2.91 (decision #5) promote the
re-probe pass out of ``--update --apply-deferred`` and onto EVERY ``--update``
and every bundle update: a read-only probe is safe to run unattended, so the
auto-resolution machinery finally runs in the field instead of behind a flag the
launcher never passed. Side-effectful remediation (``podman start`` and friends)
stays behind the flag.

Registration
------------
:data:`PROBES` maps a probe NAME to its function; the registry references it as
``clear_probe = "probe:py:<name>"``. ``tests/test_deferral_registry_completeness_v0291.py``
asserts every ``probe:py:`` name in the table resolves here — so a condition can
never again ship with a documented-but-nonexistent clear protocol.

Conditions whose only honest probe needs state Python cannot see (the RUNNING
launcher's version) declare ``probe:rs:<name>`` instead and are owned by the
launcher.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

#: Matches a backticked upstream sidecar path inside an entry's prose, e.g.
#: ``docs/X.md.from-upstream-5a9ae53``. The emitter
#: (git_user_editable_merge.rs::build_deferral_text) renders every preserved
#: path this way, and the JSON sidecar preserves the text losslessly, so the
#: list round-trips exactly.
_SIDECAR_RE = re.compile(r"`([^`\n]*\.from-upstream-[^`\n]+)`")

#: Matches the emitters' over-CAP trailer bullet, e.g. ``  - ... and 7 more``.
#: BOTH emitters render this exact shape — the Rust
#: ``git_user_editable_merge.rs::build_deferral_text`` (``"  - ... and {} more"``,
#: CAP = 100) and ``project_init._format_file_list_md`` (same cap, same string).
#: Its presence means the bullet list is INCOMPLETE: the tail beyond the cap is
#: never named, so :func:`upstream_sidecar_paths` cannot see those sidecars and
#: "all named ones are gone" stops being evidence that all of them are gone.
_TRUNCATED_LIST_RE = re.compile(r"^\s*-\s+\.\.\. and \d+ more\s*$", re.MULTILINE)

#: Directories the legacy-entry sidecar sweep never descends into. Sidecars are
#: parked NEXT TO the user-editable file the 3-way merge touched, and the
#: allowlist (``CLAUDE.md``, ``knowledge/**``, ``docs/**``, ``.claude/**``)
#: never reaches inside a VCS store, a virtualenv or a build output.
#:
#: These names are matched at any depth, which is only safe OUTSIDE the
#: allowlisted trees: ``docs/build/`` and ``knowledge/target/`` are ordinary
#: content directories whose files the merge does park sidecars beside. See
#: :func:`_sidecar_scan_prunes_here`, which stands the pruning down there.
_SIDECAR_SCAN_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "target", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".cargo", "site-packages",
})

#: Hard bound on the legacy sweep. A tree bigger than this is not walked to
#: completion, and a partial walk is NOT evidence of absence — the sweep
#: returns ``None`` (unknown) rather than a conclusion it did not earn.
_SIDECAR_SCAN_MAX_ENTRIES = 400_000

#: Trees the sidecar allowlist reaches with a ``**`` glob, so a skip-name can
#: legitimately occur INSIDE them as an ordinary content directory.
_SIDECAR_ALLOWLIST_TREES = ("knowledge", "docs")


def _sidecar_scan_prunes_here(root: Path, dirpath: str) -> bool:
    """Is ``dirpath`` a place where the skip-name pruning must stand DOWN?

    v0.2.95 F4 MINOR. :data:`_SIDECAR_SCAN_SKIP_DIRS` is matched by NAME at any
    depth, and its comment justifies that as "the set of trees the emitter
    provably cannot write into". That is true of a VCS store or a virtualenv
    anywhere; it is NOT true of ``build``, ``dist`` or ``target`` inside the
    allowlisted trees — ``docs/build/guide.md`` and ``knowledge/target/x.md``
    are ordinary user-editable files the 3-way merge parks sidecars beside, and
    the sweep skipped them, so a lone orphan there could still let the entry
    clear. The comment was part of the defect, which is why it moved too.

    Returns True once the walk is INSIDE ``knowledge/`` or ``docs/``, where
    every subdirectory is content and nothing is pruned. Outside them the
    pruning is unchanged (a repo's own ``node_modules`` / ``target`` is still
    skipped, which is what keeps the sweep bounded). Unresolvable paths prune
    normally: an unreadable path is not a reason to walk a build tree.
    """
    try:
        parts = Path(dirpath).resolve().relative_to(Path(root).resolve()).parts
    except (OSError, ValueError):
        return False
    return bool(parts) and parts[0] in _SIDECAR_ALLOWLIST_TREES


@dataclass
class ProbeContext:
    """Everything a probe may look at.

    ``extras`` carries caller-supplied facts a probe cannot derive on its own
    without duplicating knowledge that already has a home elsewhere — notably
    the OS→dist-subdir mapping, which lives in ``install._launcher_binary_relative_path``
    and must not be copied here. A probe whose required extra is ABSENT returns
    ``None`` (unknown), so a caller that cannot supply it simply leaves the
    entry alone.
    """

    folder: Path
    entry: Any = None
    extras: dict = field(default_factory=dict)


ProbeFn = Callable[[ProbeContext], Optional[bool]]


# ---------------------------------------------------------------------------
# Shared extractors (used by BOTH a clear probe and a dismiss-key field, so the
# two can never disagree about what "the preserved sidecars" means).
# ---------------------------------------------------------------------------


def upstream_sidecar_paths(entry: Any) -> tuple[str, ...]:
    """Repo-relative ``*.from-upstream-<sha>`` paths named by an entry.

    Reads BOTH ``detected`` and ``command_to_apply`` (the emitter renders the
    list in each, and the two must agree), de-duplicates, and returns them
    sorted for a stable dismissal key. Empty when the entry named none — e.g.
    an update where every allowlisted file auto-merged cleanly.
    """
    if entry is None:
        return ()
    found: set[str] = set()
    for attr in ("detected", "command_to_apply"):
        text = getattr(entry, attr, "") or ""
        for m in _SIDECAR_RE.finditer(text):
            candidate = m.group(1).strip()
            if candidate:
                found.add(candidate)
    return tuple(sorted(found))


def is_rendered_file_sidecar(rel_path: str) -> bool:
    """True when ``rel_path`` is a ``.from-upstream-`` sidecar of a RENDERED file.

    v0.2.97. A rendered root file (``vco_lib/rendered_root_files.toml`` —
    ``CLAUDE.md`` today) is materialized by install.py from its template, and
    upstream's tracked copy is only the placeholder saying so. A sidecar of it
    therefore holds nothing to adopt — adopting it would REPLACE the rendered
    file with the placeholder — so it is not outstanding work. The launcher no
    longer writes one, and install.py's re-render reaps the ones older
    launchers parked (``rendered_root_files.reap_stale_sidecars``); this is the
    probe-side half, so such a sidecar can never keep the entry alive.

    The name rule is the reap's own (``rendered_root_files.is_rendered_sidecar_path``),
    never re-stated here.
    """
    from vco_lib.rendered_root_files import is_rendered_sidecar_path

    return is_rendered_sidecar_path(rel_path)


def adoptable_upstream_sidecar_paths(entry: Any) -> tuple[str, ...]:
    """:func:`upstream_sidecar_paths` minus sidecars of RENDERED files.

    The set whose disappearance is the condition's lifecycle: see
    :func:`is_rendered_file_sidecar` for why a rendered file's sidecar is not
    in it.
    """
    return tuple(
        p for p in upstream_sidecar_paths(entry) if not is_rendered_file_sidecar(p)
    )


def dismiss_fields_for_sidecars(entry: Any) -> dict:
    """``dismiss_key`` payload for ``orchestrator_user_modified_preserved``.

    Same extractor as the clear probe, so a dismissal is keyed on exactly the
    set of sidecars whose disappearance would have cleared the entry anyway.
    """
    return {"preserved_sidecars": list(adoptable_upstream_sidecar_paths(entry))}


def any_upstream_sidecar_on_disk(root: Path) -> Optional[bool]:
    """Bounded, read-only sweep: does ANY adoptable ``*.from-upstream-*`` file exist?

    "Adoptable" excludes a RENDERED file's sidecar (v0.2.97, see
    :func:`is_rendered_file_sidecar`): it holds nothing to adopt, so its
    presence is not outstanding work and must not keep an entry alive.

    The fallback for an entry that names no sidecar paths of its own — the
    LEGACY shape this probe could not otherwise touch (an entry written before
    the emitter rendered the list, or one whose prose was hand-edited). Before
    this existed, ``upstream_sidecar_paths() == ()`` returned ``None`` forever:
    the entry was never resolvable, never re-classified, and never SAID so.
    "Nothing to probe" is not a lifecycle.

    Returns:
        True  — at least one sidecar is parked somewhere under ``root``.
        False — the walk COMPLETED inside the bound and found none.
        None  — the walk could not complete (``OSError``, or more than
                :data:`_SIDECAR_SCAN_MAX_ENTRIES` entries visited). A partial
                walk proves nothing about the part it did not see, so it must
                never read as absence — the same positive-evidence-only rule
                every other probe in this module follows.

    Read-only by construction: ``os.walk`` + name matching, no stat of the
    candidates, nothing opened, nothing written.
    """
    root = Path(root)
    visited = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if not _sidecar_scan_prunes_here(root, dirpath):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SIDECAR_SCAN_SKIP_DIRS
                ]
            visited += len(dirnames) + len(filenames)
            for name in filenames:
                if ".from-upstream-" in name:
                    rel = Path(os.path.relpath(os.path.join(dirpath, name), root))
                    if not is_rendered_file_sidecar(rel.as_posix()):
                        return True
            if visited > _SIDECAR_SCAN_MAX_ENTRIES:
                return None
    except OSError:
        return None
    return False


def sidecar_list_is_truncated(entry: Any) -> bool:
    """True when the entry's bullet list hit the emitter's 100-item cap.

    A capped list names only the first 100 preserved files; the rest exist on
    disk but appear nowhere in the entry. Any reader that treats the named set
    as EXHAUSTIVE — the clear probe being the one that matters — must first ask
    this question.
    """
    if entry is None:
        return False
    for attr in ("detected", "command_to_apply"):
        text = getattr(entry, attr, "") or ""
        if _TRUNCATED_LIST_RE.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# git helpers — tri-state wrappers over the shared dist-repair primitives.
# ---------------------------------------------------------------------------


def _git_is_usable(install_root: Path) -> bool:
    """True when ``install_root`` is a git work tree with a resolvable HEAD.

    ``dist_binary_repair.dist_dirty_paths`` returns ``[]`` on ANY git failure —
    fail-SAFE for its REPAIR caller (never manufacture a repair target out of a
    hiccup), but read as "clean" by a CLEAR probe that would then wrongly
    resolve. This precondition separates the two readings without duplicating
    the porcelain parsing.

    v0.2.92: delegates to :func:`vco_lib.git_meta.head_state` rather than
    running its own ``git rev-parse``. That module had shipped since v0.2.53
    with a docstring promising these call sites would migrate onto it and ZERO
    production consumers to show for it (R16 category 1); this is one of them,
    wired per R24.

    **The tri-state is merged here deliberately, and that is not the collapse
    the release is about.** ``head_state`` distinguishes NOT_APPLICABLE (no
    ``.git``, or a repo with no commits) from UNKNOWN (git absent, timed out) —
    but this precondition's single caller, :func:`_dist_dirty`, maps BOTH to
    ``None`` ("cannot conclude anything about dirtiness"), because neither one
    is evidence that the dist dir is clean. Merging with a stated reason is
    fine; merging because the type could not express the difference is the
    defect. If a future caller needs the difference, call ``head_state``
    directly — it is one line away and it still has it.
    """
    from vco_lib.git_meta import head_state

    return head_state(install_root).is_usable


def _dist_dirty(install_root: Path, dist_rel_dir: str) -> Optional[bool]:
    """Tri-state tracked-only dirtiness of ``dist_rel_dir``. ``None`` = unknown.

    TRACKED-ONLY (``git status`` minus ``??`` rows) mirrors the v0.2.91 MAJOR-1
    fix on the Rust side: an untracked ``.new`` sibling staged by the update
    flow is NOT divergence, and counting it kept the condition alive forever.
    """
    if not _git_is_usable(install_root):
        return None
    try:
        from vco_lib.dist_binary_repair import dist_dirty_paths
    except ImportError:  # pragma: no cover — vco_lib is always installed
        return None
    return bool(dist_dirty_paths(install_root, dist_rel_dir))


def _on_disk_launcher_version(install_root: Path, dist_rel_dir: str, binary_name: str):
    """``launcher_version`` from the dist metadata sidecar, or ``None``."""
    meta = install_root / dist_rel_dir / f"{binary_name}.metadata.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent/corrupt sidecar ⇒ unknown
        return None
    raw = data.get("launcher_version")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _version_parts(v: str) -> list[int]:
    """Thin delegate to the ONE home — see :mod:`vco_lib.version_compare`.

    Kept as a module-local name because this module's tests patch it.
    """
    from vco_lib.version_compare import version_parts

    return version_parts(v)


def _version_ge(a: str, b: str) -> bool:
    """``a >= b`` on the leading numeric components.

    Was a hand-written copy of install.py's ``_ge`` — its own docstring said
    "mirrors install.py's ``_ge``", which is a request for extraction rather
    than a design. Both now delegate to :mod:`vco_lib.version_compare`.
    """
    from vco_lib.version_compare import version_ge

    return version_ge(a, b)


def _staged_new_siblings(install_root: Path, dist_rel_dir: str) -> list[str]:
    """``*.new`` siblings waiting in the dist dir (the un-fired handoff's payload)."""
    d = install_root / dist_rel_dir
    try:
        return sorted(p.name for p in d.iterdir() if p.name.endswith(".new"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def orchestrator_sidecars_still_present(ctx: ProbeContext) -> Optional[bool]:
    """``orchestrator_user_modified_preserved`` — do any named sidecars remain?

    The entry lists every ``<path>.from-upstream-<sha>`` the pre-pull 3-way
    merge parked. The user's job is to accept each one (``mv`` over the local
    file) or delete it. Once none of them exists, the condition is objectively
    over — the entry describes work that has been done.

    Pre-v0.2.91 this cid had NO clear path at ALL: not install-owned, not in the
    bundle reconcile map, no ``resolve_conditions`` site anywhere, and the Rust
    emitter's zero-conflict run returns early without resolving. Doing exactly
    what its own ``command_to_apply`` said left the entry in place forever.

    Returns:
        True  — at least one named sidecar is still on disk, OR the entry named
                none and the bounded whole-root sweep found one anyway.
        False — the entry named sidecars, the list is COMPLETE, and every one
                of them is gone; OR the entry named none and the bounded sweep
                COMPLETED finding no sidecar anywhere under the root.
        None  — the list was truncated at the emitter's cap (see below), the
                folder is unreadable, or the fallback sweep could not complete.

    v0.2.91 dogfood fix — the LEGACY arm: an entry naming no sidecars used to
    return ``None`` unconditionally, i.e. NotProbed-forever with nothing in the
    ledger saying so. It now falls back to :func:`any_upstream_sidecar_on_disk`,
    which derives the answer from what the entry HAS (its root) instead of from
    a field it lacks. The fallback is deliberately conservative in the KEEP
    direction: a sidecar left by ANY merge keeps a list-less entry alive, which
    is the honest reading of "N user-editable files were preserved" when the
    entry cannot say which.

    Truncation (wave-2 MINOR-2): over 100 preserved files both emitters cut the
    bullet list and append ``  - ... and N more``. The tail is never named, so
    the "every named sidecar is gone" test can be satisfied while dozens of
    unnamed sidecars are still parked on disk — and clearing on that would
    delete a record of real outstanding work. Positive evidence only: a
    truncated list yields ``None`` (unknown, keep) unless a named sidecar is
    still present, which is positive evidence the other way (``True``).

    v0.2.95 F4 — the NAMED-SUBSET hole, the same shape one layer in. An entry
    is last-write-wins per condition_id, so run N's entry REPLACES run N-1's
    while run N-1's sidecars stay on disk: the maintainer's own install has
    ``CLAUDE.md.from-upstream-89a5530`` (named) beside
    ``CLAUDE.md.from-upstream-f1f5488`` (not named by any live entry). Clearing
    once the named ones are gone would retire the only record of the other and
    leave it parked and invisible — "cleared on a subset of real outstanding
    work", which is exactly what the truncation arm above exists to refuse. So
    the complete-list arm ends in the SAME bounded sweep the list-less arm
    uses: every sidecar this condition can create is accounted for, not the
    subset one entry happened to name.

    v0.2.97 — RENDERED files. Both arms look only at ADOPTABLE sidecars
    (:func:`adoptable_upstream_sidecar_paths`, and the same exclusion inside
    the sweep). A ``CLAUDE.md.from-upstream-<sha>`` is upstream's placeholder
    for a file install.py renders; it was never work the user owed, so an
    entry naming only such sidecars clears (the field case above: both
    ``CLAUDE.md`` sidecars were of this kind), while any genuine sidecar still
    keeps the entry exactly as before.
    """
    paths = adoptable_upstream_sidecar_paths(ctx.entry)
    if not paths:
        return any_upstream_sidecar_on_disk(ctx.folder)
    try:
        for rel in paths:
            candidate = ctx.folder / rel
            if candidate.exists() or candidate.is_symlink():
                return True
    except OSError:
        return None
    if sidecar_list_is_truncated(ctx.entry):
        return None
    return any_upstream_sidecar_on_disk(ctx.folder)


def launcher_dist_still_dirty(ctx: ProbeContext) -> Optional[bool]:
    """``launcher_binary_handoff_skipped_dirty`` — is the dist tree still off-HEAD?

    The condition records "new bytes are staged but nothing will move them".
    It is over once ``launcher/dist/<arch>/`` matches HEAD (tracked-only) AND no
    ``*.new`` sibling is left waiting — i.e. either the handoff eventually fired
    or the user ran the restore in the entry's own command block.

    Requires ``extras["dist_rel_dir"]``; without it the OS→subdir mapping would
    have to be duplicated here, so the probe declines (``None``) instead.
    """
    dist_rel_dir = ctx.extras.get("dist_rel_dir")
    if not dist_rel_dir:
        return None
    dirty = _dist_dirty(ctx.folder, dist_rel_dir)
    if dirty is None:
        return None
    if dirty:
        return True
    return bool(_staged_new_siblings(ctx.folder, dist_rel_dir))


def launcher_binary_stale_still_applies(ctx: ProbeContext) -> Optional[bool]:
    """``launcher_binary_stale`` — is the running image still not the on-disk one?

    Python sees two of the three freshness inputs (on-disk sidecar version, dist
    dirtiness) but NOT the third — the running launcher's compiled-in version.
    So this probe resolves ONLY when every observable signal says the delivery
    is complete AND no launcher process is holding an image we cannot identify:

      * git unusable, or the sidecar absent/unparseable   → None (unknown)
      * dist dirty vs HEAD (tracked-only)                 → True (still stale)
      * sidecar version < the source version              → True (still stale)
      * a launcher process is running                     → True (can't tell
        whether it is the new image; the launcher's own boot probe owns that
        call)
      * the process SCAN itself failed                    → None (unknown —
        v0.2.91 WP-D hardening; see :func:`_launcher_process_running`)
      * otherwise                                         → False (resolve)

    Why a Python-side clear is safe at all: the Rust emit is latched once per
    launcher PROCESS (``STALE_CONDITION_EMITTED``), but a NEW launcher process
    re-runs the at-rest probe and re-emits while the condition holds. So an
    over-eager clear here costs at most one boot of silence, whereas the
    alternative — no Python-side clear — is the immortal-entry class this
    release exists to close. The canonical clear remains the launcher's boot
    probe (``probe:rs:``-style, WP-F wiring). That asymmetry justified a
    RESIDUAL over-eager clear, never a systematic one — hence the scan is now
    tri-state.

    Requires ``extras["dist_rel_dir"]``, ``extras["launcher_binary_name"]`` and
    ``extras["source_version"]``.
    """
    dist_rel_dir = ctx.extras.get("dist_rel_dir")
    binary_name = ctx.extras.get("launcher_binary_name")
    source_version = ctx.extras.get("source_version")
    if not dist_rel_dir or not binary_name or not source_version:
        return None

    dirty = _dist_dirty(ctx.folder, dist_rel_dir)
    if dirty is None:
        return None
    if dirty:
        return True

    on_disk = _on_disk_launcher_version(ctx.folder, dist_rel_dir, binary_name)
    if not on_disk:
        return None
    if not _version_ge(on_disk, str(source_version)):
        return True

    running = _launcher_process_running(binary_name)
    if running is None:
        return None
    return True if running else False


def _process_scan_available() -> bool:
    """Can this machine's process table be scanned at all?

    ``dist_binary_repair`` scans via ``tasklist`` (Windows) or ``pgrep``/``ps``
    (POSIX). When NONE of those tools resolves, the scanner cannot distinguish
    "no launcher is running" from "I could not look" — it returns an empty list
    either way. Asking ``shutil.which`` first separates the two WITHOUT
    duplicating the scanner or its output parsing (which stays in one home).
    """
    if platform.system().lower().startswith("win"):
        return shutil.which("tasklist") is not None
    return shutil.which("pgrep") is not None or shutil.which("ps") is not None


def _launcher_process_running(binary_name: str) -> Optional[bool]:
    """Tri-state: is a launcher process visible to the OS process scan?

    ``True`` a launcher is running · ``False`` provably none · ``None`` the
    scan could not be performed.

    Delegates the actual scan to ``dist_binary_repair.scan_for_launcher_pid``
    (which carries the tasklist/ps split) rather than growing a second process
    scanner. That helper is fail-SAFE FOR ITS OWN CALLER — a handoff must never
    be armed against a hallucinated PID — so it collapses "none running" and
    "the scan failed" into ``None``. A CLEAR probe reads that collapse the
    wrong way round: it would treat an unusable process table as positive
    evidence that nothing is running and resolve an entry describing real
    outstanding work. v0.2.91 wave-2 review accepted that residual on the
    grounds that the launcher's next boot re-emits; WP-D removes it instead,
    because "a later boot fixes it" is not a reason to draw a conclusion the
    evidence does not support. Positive evidence only, everywhere.
    """
    if not _process_scan_available():
        return None
    try:
        from vco_lib.dist_binary_repair import scan_for_launcher_pid

        return scan_for_launcher_pid(binary_name) is not None
    except Exception:  # noqa: BLE001 — probe must never raise into the pass
        return None


def disk_space_still_low(ctx: ProbeContext) -> Optional[bool]:
    """``disk_space_low`` — is free space still under the floor?

    A thin wrapper over ``vco_lib.doctor.disk_space_below_floor``, which is the
    SAME measurement the doctor's ``disk_space`` probe emits from. One home for
    the rule: a separate re-implementation here could clear an entry the doctor
    would immediately re-emit (or keep one it would not).

    Returns:
        True  — at least one measured mount is still below the floor.
        False — every measured mount is above it: the condition is over, and
                the entry describes a machine state that has recovered.
        None  — nothing could be measured (an unreadable path, a stat failure).
                Positive evidence only, as everywhere else in this module.

    Note the floor itself is env-tunable (``VCT_DISK_SPACE_MIN_FREE_GB``), so
    raising it can legitimately RE-apply a condition a lower floor had cleared.
    That is the honest reading of a user-set threshold, not drift.
    """
    from vco_lib.doctor import disk_space_below_floor

    return disk_space_below_floor(ctx.folder)


def pid_is_alive(pid: int) -> bool:
    """Cross-OS "is this PID still running" probe.

    ONE home (v0.2.91 wave-3, MINOR-4): ``install.py``'s deferral re-probe
    handlers and :mod:`vco_lib.deferral_retry`'s single-instance guard both
    need it, and a second copy would be a second chance to get the Windows
    footgun wrong. On Windows ``os.kill(pid, 0)`` is NOT a probe — any
    non-CTRL signal value unconditionally ``TerminateProcess``-es the target —
    so that branch goes via ``OpenProcess`` + ``GetExitCodeProcess``
    (``STILL_ACTIVE == 259``). POSIX uses the conventional ``kill(pid, 0)``
    errno dance.

    Conservative on uncertainty: unknown → ``True`` (treat the process as
    alive). Both callers want that direction — install.py KEEPS the deferral
    entry rather than clearing it on a guess, and the retry driver DECLINES to
    start a second seed rather than racing one it cannot see.
    """
    if pid <= 0:
        return True  # unparseable / sentinel value — assume alive
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not handle:
                return False  # no such process (or no access → likely gone)
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code),
                )
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 — conservative fallback
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # uncertain → conservative


def kg_binding_evidence_still_mismatched(ctx: ProbeContext) -> Optional[bool]:
    """``kg_binding_evidence_mismatch`` — is a project's data still outside its binding?

    A thin wrapper over the SAME scan the doctor's ``kg_binding_evidence``
    probe emits from (:func:`vco_lib.kg_binding_doctor.scan_kg_binding_evidence`).
    One home for the rule — a separate re-implementation here could clear an
    entry the doctor would immediately re-emit (or keep one it would not).

    Returns:
        True  — at least one registered project's data still demonstrably
                lives in a class its primary binding does not name.
        False — the scan ran and every binding matches where its data lives:
                the user repaired it from the Identity tab (or the ghost's
                data moved on), and this entry describes a state that is over.
        None  — the scan could not LOOK (launcher.db unreadable, Weaviate
                unreachable). Positive evidence only, as everywhere else in
                this module: an unlookable state never reads as repaired.
    """
    from vco_lib.kg_binding_doctor import scan_kg_binding_evidence

    try:
        scan = scan_kg_binding_evidence()
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None
    if scan is None:
        return None
    return bool(scan.mismatches)


def parked_hook_conflict_still_present(ctx: ProbeContext) -> Optional[bool]:
    """``parked_hook_live_conflict`` — is a launcher-parked hook still running?

    A thin wrapper over :func:`vco_lib.parked_hooks.conflict_still_present`,
    the SAME detection the bundle update emits from, so the probe can never
    clear an entry the next update would re-emit. ``None`` when launcher.db or
    settings.json cannot be read.
    """
    from vco_lib.parked_hooks import conflict_still_present

    try:
        return conflict_still_present(Path(ctx.folder))
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def user_owned_secret_values_still_present(ctx: ProbeContext) -> Optional[bool]:
    """``user_owned_secret_value_in_tree`` — does a user-put secret-shaped key
    still carry a value? The SAME detection the emitter uses
    (:func:`vco_lib.user_owned_secrets.found`); ``None`` on any failure."""
    from vco_lib.user_owned_secrets import still_present

    try:
        return still_present(Path(ctx.folder))
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def settings_write_refusal_still_applies(ctx: ProbeContext) -> Optional[bool]:
    """``settings_write_refused_*`` — is the refused settings file still unfit?

    A thin wrapper over :func:`vco_lib.settings_refusal.refusal_still_applies`,
    the SAME read the writers refuse on, so the probe can never clear an entry
    the next write would re-emit. It reads the path the emitter recorded in
    ``dismiss_fields`` — ``None`` when an entry carries none (a Markdown-only
    ledger).
    """
    from vco_lib.settings_refusal import refusal_still_applies

    try:
        return refusal_still_applies(Path(ctx.folder), ctx.entry)
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def bash_env_cleanup_still_owed(ctx: ProbeContext) -> Optional[bool]:
    """``legacy_bash_env_cleanup_pending`` — does settings.json still carry the
    legacy lean-ctx ``BASH_ENV`` pointer (or stay unreadable)?

    A thin wrapper over :func:`vco_lib.project_init.legacy_bash_env_still_owed`,
    which uses the cleanup's own read (``settings_refusal.load_for_edit``) and
    shim rule, so the probe cannot clear what the next cleanup would re-emit.
    """
    from vco_lib.project_init import legacy_bash_env_still_owed

    try:
        return legacy_bash_env_still_owed(Path(ctx.folder))
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def env_reprojection_still_owed(ctx: ProbeContext) -> Optional[bool]:
    """``project_move_env_reprojection_failed`` — are the env surfaces still stale?

    A thin wrapper over :func:`vco_lib.project_move.env_reprojection_still_owed`,
    which compares ``.claude/settings.json`` / ``.claude/env`` against what
    ``config_projection apply`` derives from the project's current row — the
    same comparison ``vco project move --verify`` clears on. It reads the
    project id the emitter recorded in ``dismiss_fields``; ``None`` when the
    entry carries none, or the database / settings file cannot be read.
    """
    from vco_lib.project_move import env_reprojection_still_owed as still_owed

    try:
        return still_owed(Path(ctx.folder), ctx.entry)
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def kg_unclaimed_classes_still_present(ctx: ProbeContext) -> Optional[bool]:
    """``kg_unclaimed_populated_classes`` — is unclaimed data still unclaimed?

    A thin wrapper over the SAME scan the doctor's ``kg_binding_evidence``
    probe emits from (:func:`vco_lib.kg_binding_doctor.scan_kg_binding_evidence`)
    — one home for the rule, exactly like its sibling
    ``kg_binding_evidence_still_mismatched``. "Unclaimed" there means a
    populated ``*_KnowledgeGraph`` class no binding row names and no
    registered project's folder anchors (the removed-project leftover).

    Returns:
        True  — at least one populated class is still unclaimed.
        False — the scan ran and every populated class is accounted for:
                the user re-added the project, re-bound the class from the
                Identity tab, or the class was emptied outside VCO. The
                entry described a state that is over.
        None  — the scan could not LOOK (launcher.db unreadable, Weaviate
                unreachable). An unlookable state never reads as resolved.
    """
    from vco_lib.kg_binding_doctor import scan_kg_binding_evidence

    try:
        scan = scan_kg_binding_evidence()
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None
    if scan is None:
        return None
    return bool(scan.unclaimed)


#: name → probe. Referenced from the registry as ``probe:py:<name>``.
def code_embed_image_still_stale(ctx: ProbeContext) -> Optional[bool]:
    """``code_embed_image_stale`` — is the running service still on old source?

    A thin wrapper over :func:`vco_lib.code_embed_image.image_state`, which is
    the SAME reading the doctor's ``code_embed_image`` probe emits from and the
    same reading that decides whether the compose invocation gets
    ``--build``. One home for the rule: a
    separate re-implementation here could clear an entry the doctor would
    immediately re-emit.

    Returns:
        True  — the service still reports a digest that is not this checkout's
                (or reports none at all, which identifies a pre-v0.2.92 image).
        False — the digests match: the image has been rebuilt and the entry
                describes a condition that is over.
        None  — could not look (service down, the service could not hash
                itself). Positive evidence only: a silent service is never
                read as "fixed".

    A tree with no code-embed service source of its own is NOT a ``None``
    case, though it was until 2026-09-22: there is one service per machine,
    so the verdict resolves the INSTALL ROOT and answers from there. A
    per-project probe that returned ``None`` for every project left the
    entry standing on exactly the machines it was meant to clear.
    """
    from vco_lib import code_embed_image

    try:
        state = code_embed_image.image_state(ctx.folder)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if state.verdict == code_embed_image.STALE:
        return True
    if state.verdict == code_embed_image.CURRENT:
        return False
    return None


def gateway_exec_still_unrunnable(ctx: ProbeContext) -> Optional[bool]:
    """``gateway_registered_but_unrunnable`` — can the registration run YET?

    The SAME reading :func:`vco_lib.gateway_ensure.gateway_status` returns, so
    the probe that clears the entry and the two sites that emit it (the
    SessionStart ensure and the doctor) cannot disagree. It reads the argv back
    out of the INSTALLED artefact and runs it with ``--version``; nothing is
    started and nothing is written.

    Returns:
        True  — still registered, still unable to run.
        False — provably over: either the registration now runs, or the
                gateway is running, or it is no longer registered at all
                (an entry about a registration that does not exist describes
                nothing).
        None  — never reached today; kept as the honest answer if a future
                state cannot be classified, because "could not look" must not
                read as "fixed".
    """
    from vco_lib import gateway_ensure

    try:
        found = gateway_ensure.gateway_status()
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if found.state is gateway_ensure.GatewayState.REGISTERED_BUT_UNRUNNABLE:
        return True
    if found.state in (
        gateway_ensure.GatewayState.NOT_REGISTERED,
        gateway_ensure.GatewayState.RUNNING,
        gateway_ensure.GatewayState.REGISTERED_NOT_RUNNING,
        gateway_ensure.GatewayState.STARTED,
    ):
        return False
    return None


#: Socket timeout for the hub health read. Mirrors the timeout
#: ``install.py::_probe_vct_hub_health`` uses — the two must stay in step
#: because they read the SAME endpoint (see :func:`hub_answers_health`).
_HUB_HEALTH_TIMEOUT_SECONDS = 0.5


def hub_answers_health() -> Optional[bool]:
    """Does the hub answer ``/api/v1/health`` on the resolved port?

    The port comes from the ONE resolution chain
    (``vco_lib.access_resolver._hub_port``: ``$VCT_HUB_PORT`` →
    ``<state dir>/hub.port`` → 7700) — this module does not re-derive it.
    The GET is the vco_lib twin of ``install.py::_probe_vct_hub_health``
    (same endpoint, same intentionally-AUTH-FREE request, same
    ``status < 400`` bar); install.py's copy is unreachable from
    ``vco_lib`` and cannot be shared without importing the 24k-line
    installer, so the contract is pinned by name here and by behaviour
    tests in ``tests/test_v0295_deferral_reconcile_hub_restart.py``.

    Returns:
        True  — the hub answered with status < 400.
        False — the port answered with an error status, or nothing
                answered at all (refused / timeout / HTTP error).
        None  — the port itself could not be resolved (the check could not
                run; never read as "hub down" NOR as "hub up").
    """
    try:
        # Same-package seam on purpose: `_hub_port` is the one home of the
        # env > state-file > default chain (`access_resolver.py`); a second
        # copy here would be the fourth port resolver in the tree.
        from vco_lib.access_resolver import _hub_port

        port = _hub_port()
    except Exception:  # noqa: BLE001 — no port is not a verdict
        return None
    url = f"http://127.0.0.1:{port}/api/v1/health"
    try:
        with urllib.request.urlopen(
            url, timeout=_HUB_HEALTH_TIMEOUT_SECONDS
        ) as resp:
            return resp.status < 400
    except Exception:  # noqa: BLE001 — refused/timeout/HTTP-error = not answering
        return False


def hub_back_after_restart_failure(ctx: ProbeContext) -> Optional[bool]:
    """``hub_restart_failed_after_abort`` — is the hub back up?

    The entry records a fact about the PAST: the abort-path hub restart's
    health poll failed (``installer.rs`` emits it only after a conflict-aborted
    update left the hub not answering within 30 s). The STATE question —
    the only one a clear may key on (R26) — is whether that still describes
    the machine: does the hub answer NOW?

    The pre-v0.2.95 resolver lived only in ``install.py``'s re-probe pass and
    AND-ed a hub-sidecar version comparison, a conjunction that could never
    fire there: the pass ran BEFORE both the dist-binary refresh and the hub
    restart, i.e. before the executor outcomes it wanted to read — the row
    survived two further updates and a ``vco doctor`` run in the field. A
    caught-up BINARY is anyway not what the entry claims: it claims the hub
    did not come back. The hub answering ``/api/v1/health`` is positive
    evidence that it has (via :func:`hub_answers_health`, so the probe and
    every other health reader share one reading).

    Returns:
        True  — the hub is not answering → the recorded failure still
                stands. KEEP.
        False — the hub answered → the hub is back; the abort-time failure
                no longer describes the machine. CLEAR.
        None  — could not look (port resolution failed). KEEP.
    """
    try:
        answered = hub_answers_health()
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if answered is None:
        return None
    return not answered


def chunker_resync_still_owed(ctx: ProbeContext) -> Optional[bool]:
    """``chunker_preset_overhaul_pending`` — has the re-sync remedy been run?

    A thin wrapper over :func:`vco_lib.chunker_revision.resync_still_owed`,
    which owns the whole chunker-resync lifecycle (the sentinel the gate
    compares, the remedy text both emitters print, and the half-stamps the two
    remedy scripts write). One home for the rule: a second reading here could
    clear an entry the gate would immediately re-emit.

    v0.2.95 F1 — this cid was the last IMMORTAL one. It declared
    ``paired-resolution`` and named a clearing site that does not exist in
    either language, while its own emitters promise the user it
    "self-resolves on the next bundle update". The registry-completeness test
    validates only ``probe:py:`` names, so a sentinel family whose site is
    prose failed nothing.

    Returns:
        True  — a half of the remedy has not run under the current revision.
        False — both halves are recorded at the current revision: the entry
                describes work that has been done.
        None  — could not look (unreadable chunker revision or corrupt stamp).
    """
    from vco_lib import chunker_revision

    try:
        return chunker_revision.resync_still_owed(ctx.folder)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None


PROBES: dict[str, ProbeFn] = {
    "bash_env_cleanup_still_owed": bash_env_cleanup_still_owed,
    "chunker_resync_still_owed": chunker_resync_still_owed,
    "gateway_exec_still_unrunnable": gateway_exec_still_unrunnable,
    "hub_back_after_restart_failure": hub_back_after_restart_failure,
    "orchestrator_sidecars_still_present": orchestrator_sidecars_still_present,
    "launcher_dist_still_dirty": launcher_dist_still_dirty,
    "launcher_binary_stale_still_applies": launcher_binary_stale_still_applies,
    "disk_space_still_low": disk_space_still_low,
    "env_reprojection_still_owed": env_reprojection_still_owed,
    "kg_binding_evidence_still_mismatched": kg_binding_evidence_still_mismatched,
    "kg_unclaimed_classes_still_present": kg_unclaimed_classes_still_present,
    "parked_hook_conflict_still_present": parked_hook_conflict_still_present,
    "settings_write_refusal_still_applies": settings_write_refusal_still_applies,
    "user_owned_secret_values_still_present": user_owned_secret_values_still_present,
    "code_embed_image_still_stale": code_embed_image_still_stale,
}


def launcher_probe_extras(
    dist_subdir: str, binary_name: str, source_version: Optional[str] = None
) -> dict:
    """Build the ``extras`` the launcher-binary probes need.

    The caller supplies the OS-dependent facts because the OS →
    ``launcher/dist/<arch>/`` mapping has ONE home
    (``install._launcher_binary_relative_path``) and copying it here would make
    a fourth copy of a mapping that has already been wrong once (the v0.2.14
    macOS slot bug). Assembling the dict is this module's job; KNOWING the
    mapping is not.

    ``source_version`` is optional: without it
    :func:`launcher_binary_stale_still_applies` declines (returns ``None``)
    rather than comparing against a version it had to guess.
    """
    extras = {
        "dist_rel_dir": f"launcher/dist/{dist_subdir}",
        "launcher_binary_name": binary_name,
    }
    if source_version:
        extras["source_version"] = source_version
    return extras


def registry_probe_name(condition_id: str) -> Optional[str]:
    """The PYTHON probe name the registry declares for ``condition_id``.

    ``None`` for a sentinel clear mechanism (owned-drop / bundle-reconciled /
    paired-resolution / manual-dismiss) AND for a ``probe:rs:`` probe — a
    Rust-owned probe sees state Python cannot (the RUNNING launcher's version),
    so a Python pass must not pretend to evaluate it. Never raises: a registry
    problem degrades to "no probe", i.e. leave the entry alone.
    """
    try:
        from vco_lib.deferral_registry import condition

        spec = condition(condition_id)
    except Exception:  # noqa: BLE001 — a registry problem must not break a pass
        return None
    return spec.probe_name if spec is not None else None


def evaluate(folder: Path, entry: Any, extras: Optional[dict] = None):
    """Run the registry-declared probe for ``entry``, if it declares one.

    Returns the probe's tri-state (``True`` still applies / ``False`` provably
    over / ``None`` unknown), or ``None`` when the condition declares no Python
    probe — indistinguishable to the caller, which is correct: both mean
    "this pass has nothing to say, leave the entry alone".
    """
    cid = getattr(entry, "condition_id", "")
    name = registry_probe_name(cid)
    if name is None:
        return None
    return run_probe(
        name, ProbeContext(folder=Path(folder), entry=entry, extras=extras or {})
    )


def clear_mechanism_sentence(condition_id: str) -> str:
    """One honest sentence about HOW ``condition_id`` can end, from the registry.

    The sentinel families are not interchangeable and the difference matters to
    whoever reads the ledger: ``manual-dismiss`` means "this will sit here until
    YOU do something", while ``owned-drop-when-absent`` means "the next update
    removes it by itself". Rendering them identically (which the ledger did
    until this fix) is what let ~21 permanently-manual rows look exactly like
    rows VCO was about to clear.

    **This function answers a REGISTRY question and nothing else**, so its
    ``paired-resolution`` arm says what the registry declares — "auto, the
    emitting component clears it" — which is the mechanism, not a prediction
    that it will happen soon. Whether VCO has ACTUALLY been able to retry is a
    different question, answered from the attempt trail by
    :func:`retry_history_note` and appended by :func:`probe_status_sentence`,
    the leg that reaches the ledger and the GUI. Do not fold the trail read in
    here: this is called for conditions with no dispatcher handler at all, and
    it must stay usable without a folder.
    """
    try:
        from vco_lib.deferral_registry import condition

        spec = condition(condition_id)
    except Exception:  # noqa: BLE001 — registry trouble ⇒ say the honest thing
        spec = None
    if spec is None:
        return (
            "unregistered condition — VCO declares no lifecycle for it, so it "
            "is treated as action required and will never clear on its own."
        )
    rust = spec.rust_probe_name
    if rust:
        return (
            f"launcher-owned — the launcher's boot probe `{rust}` clears this "
            "entry; an install/update pass deliberately does not evaluate it."
        )
    return {
        "owned-drop-when-absent": (
            "auto — install.py re-detects this condition on every update and "
            "drops the entry on the first run that does not detect it."
        ),
        "bundle-reconciled": (
            "auto — the next bundle update recomputes this condition and "
            "clears the entry once it no longer applies."
        ),
        "paired-resolution": (
            "auto — the component that emitted this entry clears it when its "
            "owed work completes."
        ),
        "manual-dismiss": (
            "no automatic clear — this entry stays until you act on it or "
            "dismiss it explicitly; VCO will not remove it on its own."
        ),
    }.get(
        spec.clear_probe,
        f"clear mechanism `{spec.clear_probe}` — no Python probe evaluates it "
        "on this pass.",
    )


def retry_history_note(folder: Optional[Path], condition_id: str) -> str:
    """The retry-history correction for ``condition_id`` in ``folder``, or ``""``.

    v0.2.92, register item 26. :func:`clear_mechanism_sentence` renders the
    ``paired-resolution`` family as *"auto — the component that emitted this
    entry clears it when its owed work completes"*, which every surface shows
    the user as **"VCO retries this itself"**. That is true only while the
    backend the retry needs eventually comes back. A user whose code-embed
    service has been down since the entry appeared was reading a promise being
    kept in form and not in substance, with nothing on any surface saying so.

    :func:`vco_lib.deferral_retry.retry_disposition_note` is the reader that
    knows better — it counts the durable ``BLOCKED`` trail rows and the
    attempt cap. It was built for exactly this and left unwired, because the
    dispatcher that owns it correctly refused to write into an entry owned by
    another component (``codegraph_resync`` re-emits its condition every
    deferred run, last-write-wins, so a disposition written from the
    dispatcher would revert in silence). ``probe_status`` is this module's
    field to annotate, so the correction belongs here — this is the leg that
    reaches **the ledger**: ``deferral_report`` renders it as
    ``**Probe status**:`` in ``.claude/context/UPDATE_DEFERRED.md`` and carries
    it in the JSON sidecar, which is what a session-start read (and any agent
    following CLAUDE.md's ledger protocol) actually sees.

    **Not the launcher GUI, yet** — verified, not assumed:
    ``launcher/src/lib/deferral-ledger.ts:239`` switches on ``disposition``
    alone and hardcodes *"VCO retries this itself when the thing it needs comes
    back up."*; nothing in ``launcher/src`` reads ``probe_status``. That is
    register item 27 and it belongs to the launcher lane. Saying "and the GUI"
    here would be a fresh R16 promise in the act of fixing one.

    Gated on ``handler_name_for``: a condition with no dispatcher handler was
    never promised a retry, so quoting retry history at it would be a second
    kind of wrong sentence.

    Args:
        folder: Project folder whose attempt trail is read. ``None`` (the
            default when a caller has no folder) yields ``""`` — an empty note
            is a real answer here, not a failure.
        condition_id: The condition to summarise.

    Returns:
        The note, or ``""`` when there is nothing more accurate to say than
        the generic disposition. Never raises: a retry module that cannot be
        imported or a trail that cannot be read degrades to ``""``.
    """
    if folder is None:
        return ""
    try:
        from vco_lib.deferral_retry import handler_name_for, retry_disposition_note

        if handler_name_for(condition_id) is None:
            return ""
        return retry_disposition_note(Path(folder), condition_id) or ""
    except Exception:  # noqa: BLE001 — an annotation must never break a pass
        return ""


def probe_status_sentence(
    condition_id: str,
    probe_name: Optional[str],
    verdict: Optional[bool],
    folder: Optional[Path] = None,
) -> Optional[str]:
    """The ``DeferralEntry.probe_status`` text for one probed entry.

    ``None`` only for the resolved case (``verdict is False``), where the entry
    is about to be removed and a status would never be read.

    ``folder`` is optional and defaults to ``None`` so the pre-v0.2.92
    three-argument call shape keeps working; supply it (as
    :func:`probe_report` does) to get the retry-history correction appended —
    see :func:`retry_history_note`. Without it the sentence is the same
    generic one as before, which is honest but less specific: the note can
    only be earned by reading a trail, and a caller that gave us no folder has
    no trail to read.

    The note is appended to whichever base sentence applies, not only to the
    ``clear_mechanism_sentence`` arm. A cid with a wired handler whose retries
    have been blocked for three passes is equally mis-described by "still
    applies — the condition still holds", which invites the reader to wait for
    a retry that is not happening.
    """
    if verdict is False:
        return None
    if probe_name is None:
        base = clear_mechanism_sentence(condition_id)
    elif verdict is True:
        base = (
            f"still applies — clear probe `{probe_name}` re-checked this on the "
            "last update and the condition still holds."
        )
    else:
        base = (
            f"undetermined — clear probe `{probe_name}` could not decide on the "
            "last update, so the entry was KEPT. It is re-probed on every update "
            "and clears as soon as the probe can confirm the condition is over."
        )
    note = retry_history_note(folder, condition_id)
    return f"{base} {note}" if note else base


@dataclass
class ProbePass:
    """The outcome of probing a whole report, bucketed so nothing is silent.

    :attr:`probed` + :attr:`unprobed` partition the report EXACTLY — the
    property install.py's summary line asserts, so a pass can never quietly
    look at fewer entries than the ledger holds.
    """

    #: cid → tri-state verdict, for entries whose registry row names a Python probe.
    verdicts: dict = field(default_factory=dict)
    #: cid → the ``probe_status`` sentence this pass computed (``None`` = clear it).
    statuses: dict = field(default_factory=dict)
    #: cids whose probe returned a positive False ("provably over").
    resolvable: list = field(default_factory=list)
    #: cids that had a Python probe (in report order).
    probed: list = field(default_factory=list)
    #: cids with no Python probe — sentinel clear family, Rust probe, or unregistered.
    unprobed: list = field(default_factory=list)
    #: How many entries' ``probe_status`` this pass actually CHANGED.
    #:
    #: Load-bearing, not a statistic: :func:`probe_report` stamps the entries it
    #: is given, so a caller that compares "before" and "after" ON THOSE ENTRIES
    #: always sees equality and concludes nothing changed. (That is exactly how
    #: the bundle leg first silently skipped every annotation.) The pass records
    #: the delta itself, at the only moment the old value still exists.
    changed: int = 0

    @property
    def total(self) -> int:
        return len(self.probed) + len(self.unprobed)


def probe_report(
    folder: Path, report: Any, extras: Optional[dict] = None
) -> ProbePass:
    """Probe every entry ONCE, stamp ``probe_status``, and bucket the outcome.

    ONE home for "probe a whole report", shared by install.py's re-probe pass
    and project_init's bundle-update reconcile, so the two surfaces can never
    disagree about what a probe verdict means — and now also so they cannot
    disagree about what the LEDGER says a given entry's lifecycle is.

    Mutates only the in-memory entries (``probe_status``); the probes
    themselves stay read-only against the filesystem. Never raises: a broken
    probe degrades that one entry to "unknown", exactly as before.
    """
    out = ProbePass()
    try:
        entries = list(report.entries)
    except Exception:  # noqa: BLE001 — unreadable report ⇒ nothing to probe
        return out
    for entry in entries:
        cid = getattr(entry, "condition_id", "")
        try:
            name = registry_probe_name(cid)
            verdict = (
                run_probe(
                    name,
                    ProbeContext(
                        folder=Path(folder), entry=entry, extras=extras or {}
                    ),
                )
                if name is not None
                else None
            )
        except Exception:  # noqa: BLE001 — per-entry soft-fail
            name, verdict = None, None
        if name is None:
            out.unprobed.append(cid)
        else:
            out.probed.append(cid)
            out.verdicts[cid] = verdict
            if verdict is False:
                out.resolvable.append(cid)
        status = probe_status_sentence(cid, name, verdict, folder=Path(folder))
        out.statuses[cid] = status
        try:
            if getattr(entry, "probe_status", None) != status:
                out.changed += 1
            entry.probe_status = status
        except Exception:  # noqa: BLE001 — an exotic entry type must not break the pass
            pass
    return out


def apply_probe_statuses(report: Any, statuses: dict) -> int:
    """Copy a :attr:`ProbePass.statuses` map onto another report's entries.

    Lets a caller probe the report it READ and persist the annotations through
    a DIFFERENT (locked, re-read) report object without probing twice.
    Returns how many entries changed.
    """
    changed = 0
    try:
        entries = list(report.entries)
    except Exception:  # noqa: BLE001
        return 0
    for entry in entries:
        cid = getattr(entry, "condition_id", "")
        if cid not in statuses:
            continue
        new = statuses[cid]
        if getattr(entry, "probe_status", None) != new:
            try:
                entry.probe_status = new
                changed += 1
            except Exception:  # noqa: BLE001
                continue
    return changed


def apply_probe_verdict(
    folder: Path,
    entry: Any,
    run_report: Any,
    result: "ProbePass",
    resolved_ids: list,
    log: Callable[[str], None] = print,
) -> bool:
    """Settle one entry from its registry-declared probe. ``True`` = handled.

    The GENERIC head of the re-probe loop, shared out of install.py so the
    monolith keeps shrinking (the ratchet) and so the verdict→action mapping
    has one home: ``False`` resolves and leaves a trail line, ``True``/``None``
    keep. ``False`` when the condition declares no Python probe — the caller's
    hand-written branches own it.
    """
    cid = getattr(entry, "condition_id", "")
    name = registry_probe_name(cid)
    if name is None:
        return False
    verdict = result.verdicts.get(cid)
    if verdict is False:
        resolved_ids.append(cid)
        log(f"  [ok]   {cid}: probe `{name}` reports the condition no longer "
            "applies. Marking resolved.")
        run_report.mark_resolved(cid)
        record_probe_resolution(folder, cid, name)
    else:
        why = "still applies" if verdict else "could not determine it"
        log(f"  [skip] {cid}: probe `{name}` {why}. Keeping entry.")
        run_report.add_entry(entry)
    return True


def settle_unhandled_entry(
    folder: Path,
    entry: Any,
    run_report: Any,
    expired_ids: list,
    owned_ids,
    owned_prefixes=(),
    log: Callable[[str], None] = print,
) -> str:
    """The re-probe loop's TAIL: expire an owned record, else preserve.

    Two outcomes, and which one applies turns entirely on OWNERSHIP:

    * an install-owned record this run did not re-detect is DROPPED — see
      :func:`owned_record_is_expirable` for why the generic preserve was the
      v0.2.91 dogfood defect;
    * anything else is preserved verbatim, because install.py not re-detecting
      a FOREIGN condition says nothing about whether it still holds.

    Returns ``"expired"`` or ``"kept"``.
    """
    cid = getattr(entry, "condition_id", "")
    if owned_record_is_expirable(cid, run_report, owned_ids, owned_prefixes):
        log(f"  [expired] {cid}: install-owned record not re-detected this "
            "run — dropping it (owned-drop-when-absent).")
        expired_ids.append(cid)
        run_report.mark_resolved(cid)
        record_owned_record_expiry(folder, cid)
        return "expired"
    log(f"  [unknown] {cid}: no handler. Preserving entry.")
    run_report.add_entry(entry)
    return "kept"


def owned_record_is_expirable(
    condition_id: str, run_report: Any, owned_ids, owned_prefixes=()
) -> bool:
    """Should the re-probe pass DROP this persisted entry instead of keeping it?

    True only for an install-OWNED condition that no step of THIS run
    re-detected. Both halves are load-bearing:

    * **Owned** means ``clear_probe = "owned-drop-when-absent"`` — the A-2 seed
      deliberately does not import the cid, precisely so the run's single
      end-of-run write expires it. install.py's re-probe pass used to defeat
      that contract with its generic ``[unknown]`` fallback: it re-added the
      on-disk copy to the run report, and finalize wrote it straight back. The
      one-shot records WP-B promised would auto-expire
      (``kg_access_phantom_repaired``, ``codegraph_binding_repaired``,
      ``hard_cut_performed`` — all emitted at launcher BOOT, never inside an
      install run) therefore never expired at all: the v0.2.91 live dogfood
      found "No action needed — the access rows were already restored" still in
      the ledger after two consecutive updates.
    * **Not re-detected** is the other side. If any step of this run emitted the
      condition, the fresh entry is already in the run report and the record is
      KEPT — expiring it would delete a fact detected seconds earlier.

    FOREIGN conditions are never expirable here, whatever their clear family:
    install.py does not re-detect them, so "absent from the run report" says
    nothing about them.
    """
    try:
        from vco_lib.deferral_report import condition_is_owned

        if not condition_is_owned(condition_id, owned_ids, owned_prefixes):
            return False
        return not run_report.has_condition(condition_id)
    except Exception:  # noqa: BLE001 — unknown ⇒ keep the entry (conservative)
        return False


def record_owned_record_expiry(folder: Path, condition_id: str) -> None:
    """B-F9 trail for an install-owned record the re-probe pass dropped.

    Never raises — observability must not break a pass that already decided.
    """
    try:
        from vco_lib.deferral_emit import record_auto_resolution

        record_auto_resolution(
            Path(folder),
            condition_id,
            "expired_install_owned_record",
            "install.py owns this condition and did not re-detect it this run "
            "(owned-drop-when-absent)",
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass


def format_probe_pass_summary(
    persisted: Any,
    run_report: Any,
    result: "ProbePass",
    probe_resolved_ids,
    owned_expired_ids,
) -> str:
    """One accounting line: every persisted entry lands in exactly one bucket.

    v0.2.91 dogfood fix. The pass printed per-entry lines and nothing else, so
    "the pass reported 3 things and the ledger holds 4" was a discrepancy
    nobody could act on — the reader had to count lines and hope no branch had
    ``continue``d without printing. The counts are derived from the OUTCOME
    (what the run report ends up holding), not from per-branch counters, so a
    future branch that forgets to tally cannot make this line lie.
    """
    try:
        persisted_ids = [e.condition_id for e in persisted.entries]
        kept = [c for c in persisted_ids if run_report.has_condition(c)]
        by_handler = [
            c for c in persisted_ids
            if c not in kept
            and c not in probe_resolved_ids
            and c not in owned_expired_ids
        ]
        kept_unprobed = [c for c in kept if c in result.unprobed]
        return (
            f"  [summary] {len(persisted_ids)} ledger entr"
            f"{'y' if len(persisted_ids) == 1 else 'ies'} probed: "
            f"{len(probe_resolved_ids)} resolved by probe, "
            f"{len(owned_expired_ids)} expired (install-owned record), "
            f"{len(by_handler)} resolved by handler, {len(kept)} kept "
            f"({len(kept_unprobed)} of them have no Python clear probe — "
            "each entry's `Probe status` line in the ledger names why)."
        )
    except Exception as exc:  # noqa: BLE001 — a summary must never break the pass
        return f"  [summary] could not be computed ({exc})."


def resolvable_condition_ids(
    folder: Path, report: Any, extras: Optional[dict] = None
) -> list:
    """Condition ids in ``report`` whose probe says they are provably over.

    Thin view over :func:`probe_report`. Only a positive ``False`` lands in the
    list — that asymmetry is the safety property: a probe that could not run
    must never look like a resolution.
    """
    return list(probe_report(folder, report, extras).resolvable)


def record_probe_resolution(folder: Path, condition_id: str, probe_name: str) -> None:
    """B-F9 trail line for a probe-driven clear. Never raises.

    No silent mutations: a pass that removes a user-visible record must leave
    an auditable line behind saying which probe decided it and why.
    """
    try:
        from vco_lib.deferral_emit import record_auto_resolution

        record_auto_resolution(
            Path(folder),
            condition_id,
            "resolved_by_registry_probe",
            f"probe `{probe_name}` reported the condition no longer applies",
        )
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


def run_probe(name: str, ctx: ProbeContext) -> Optional[bool]:
    """Dispatch ``name``. Unknown name or a raising probe ⇒ ``None`` (keep).

    A probe that raises must never abort the pass that runs it — the pass is
    best-effort observability layered on top of an install run that already
    succeeded.
    """
    fn = PROBES.get(name)
    if fn is None:
        return None
    try:
        return fn(ctx)
    except Exception:  # noqa: BLE001 — a broken probe must not break the run
        return None


__all__ = [
    "PROBES",
    "ProbeContext",
    "ProbeFn",
    "ProbePass",
    "adoptable_upstream_sidecar_paths",
    "any_upstream_sidecar_on_disk",
    "apply_probe_statuses",
    "clear_mechanism_sentence",
    "dismiss_fields_for_sidecars",
    "format_probe_pass_summary",
    "is_rendered_file_sidecar",
    "owned_record_is_expirable",
    "probe_report",
    "probe_status_sentence",
    "record_owned_record_expiry",
    "disk_space_still_low",
    "hub_answers_health",
    "hub_back_after_restart_failure",
    "evaluate",
    "launcher_binary_stale_still_applies",
    "launcher_dist_still_dirty",
    "orchestrator_sidecars_still_present",
    "pid_is_alive",
    "record_probe_resolution",
    "registry_probe_name",
    "resolvable_condition_ids",
    "retry_history_note",
    "run_probe",
    "upstream_sidecar_paths",
]
