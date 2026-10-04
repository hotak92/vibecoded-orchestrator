# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Project-level templates: CLAUDE.md, CONTEXT_STATE.md, MEMORY.md.

Extracted from ``vco_lib/project_init.py`` in v0.2.100 (WP-18). The
managed-region re-render on ``install-bundle --update`` (owner: a template
change or a moved clone must REACH an existing project's CLAUDE.md, not only
its ``.reference.md`` sidecar) put that module past its line ratchet, so the
whole project-level-template half moved here; ``project_init`` keeps
same-name thin aliases (``_install_project_level_templates``,
``render_claude_md``, ``_render_project_template``) for its callers, the CLI
and the tests.

ONE render pipeline serves both entry points — the bundle install/update and
the launcher's ``re-render-claude-md`` — so they agree on every value,
``{{PROJECT_NAME}}`` included (``vco_lib.materialize.project_display_name``).

THE SPLIT (v0.2.100, review R18-01, owner decision): ``templates/CLAUDE.md.template``
carries the VCO-managed-region markers itself. Everything BETWEEN them is VCO's
and is re-rendered on every update (and by the launcher's module toggle).
Everything OUTSIDE them — the introduction, Project Overview, Tech Stack, Key
Paths, and whatever the user adds — is the USER SECTION: written only when the
project's CLAUDE.md is created, never touched afterwards. When a release
changes the user-section TEMPLATE, the user's text still stays; the new version
goes to a sidecar and one ``claude_md_user_section_review`` row asks the user
to compare (see :func:`reconcile_claude_md`).

Helpers still owned by ``project_init`` (conditional sections, the managed
region merge, atomic writes, adoption backups) are reached through the module
object at CALL time (``_pi.name``), so a test that patches them on
``project_init`` patches them here too.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from vco_lib import claude_md_sections
from vco_lib import project_init as _pi
from vco_lib import setup_sections
from vco_lib.template_divergence import (
    meaningfully_differs,
    normalise_for_diff,
    remove_stale_root_claude_md_sidecar,
)

__all__ = [
    "USER_SECTION_REVIEW_CID",
    "USER_SECTION_SIDECAR_REL",
    "USER_SECTION_STATE_REL",
    "compose_claude_md",
    "install_project_level_templates",
    "managed_body",
    "reconcile_claude_md",
    "render_claude_md",
    "render_project_template",
    "split_claude_md",
    "user_section_template_hash",
]


def render_project_template(
    template_text: str,
    *,
    label: str,
    folder: Path,
    orchestrator_root: Path,
    project_name: Optional[str],
    active_modules: Iterable[str],
    db_path: Optional[Path] = None,
    sink: "Any" = None,
    swallow_template_error: bool = False,
    context: "Any" = None,
) -> str:
    """ONE render pipeline for the project-level templates (CLAUDE.md,
    CONTEXT_STATE.md, MEMORY.md) — shared by the bundle install/update and the
    launcher's ``re-render-claude-md`` (v0.2.100 WP-18; the two used to build
    their own ``{{PROJECT_NAME}}``).

    Conditional sections first (``render_conditional_blocks``), then the
    ``vco_lib.materialize`` registry pass. ``{{PROJECT_NAME}}`` is the
    registered name when launcher.db knows the folder, else ``project_name``,
    else the basename (``materialize.project_display_name``).

    ``swallow_template_error``: the bundle path historically shipped the
    template's conditional tags raw when they were malformed (the reference
    sidecar surfaces it); the launcher path raises. Both behaviours are kept.
    """
    from vco_lib import materialize as _mz

    try:
        text = _pi.render_conditional_blocks(template_text, active_modules=set(active_modules))
    except _pi.TemplateError:
        if not swallow_template_error:
            raise
        text = template_text
    if context is None:  # the completeness gate passes a synthetic one
        context = _mz.MaterializeContext(
            orchestrator_root, folder, project_name=project_name, db_path=db_path,
        )
    result = _mz.render(text, _mz.LazyContext(context),
                        allowed=_mz.GLOBAL_KEYS, escape="none")
    if sink is not None:
        sink.record(label, result)
    elif not result.clean:
        _mz.warn(label, result)
    return result.text


def _emit_setup_deferral(
    folder: Path, pending: Tuple[setup_sections.SetupBlock, ...],
) -> None:
    """Emit/clear ``first_run_setup_pending`` for a PROJECT render (v0.2.101
    12a) — the project pair of the root path's default call in
    ``rendered_root_files.render_entry``. Self-gates on
    ``setup_sections.is_managed_install_root`` (a project the bundle has
    installed into always is — ``templates/scripts/**`` ships the cleanup
    script the row's remedy names). Best-effort: soft-fails internally."""
    setup_sections.emit_pending_deferral(
        folder, pending,
        template_name=setup_sections.PROJECT_TEMPLATE_NAME,
        root_label=setup_sections.PROJECT_ROOT_LABEL,
    )


def install_project_level_templates(
    folder: Path,
    *,
    orchestrator_root: Path,
    project_name: Optional[str],
    dry_run: bool,
    sink: "Any" = None,
) -> dict:
    """Install (or refresh) the three project-level template stubs.

    Returns a result dict for the install_project_bundle response::

        {
          "live_created":  [<rel>...],  # template stub installed as the
                                        # actual project file (was missing).
          "reference_written": [<rel>...],  # .reference.md sidecar refreshed.
          "diverged":      [<rel>...],  # existing file ≠ reference template.
        }

    Idempotent. On every run the reference sidecars are rewritten with
    the current shipping shape (atomic write, no-op when bytes match).

    v0.2.70 (Bug B / B-1): the `.claude/CONTEXT_STATE.md` live file and the
    `.claude/context/templates/*.reference.md` sidecars are written via
    `_write_file_atomic`, so when `.claude` itself is a symlink VCO refused to
    write through, those writes redirect to `.vco-new`. We surface every such
    redirect via the `symlink_redirects` key so `install_project_bundle` folds
    them into the SAME consolidated symlink deferral as the main file loop.
    (CLAUDE.md / MEMORY.md live at the project ROOT — never under `.claude/` —
    so their live writes don't redirect; only their `.reference.md` sidecars,
    which live under `.claude/`, can.)

    v0.2.100 WP-18 (owner): an existing project ``CLAUDE.md`` that carries the
    VCO-managed-region markers has its managed body RE-RENDERED on every run
    (:func:`reconcile_claude_md`, shared with ``render_claude_md``) — before
    this, a template change or a moved clone never reached an existing project
    through the update path, only a ``.reference.md`` sidecar did. The USER
    SECTION outside the markers (review R18-01: introduction, overview, tech
    stack, key paths, anything added) is written only at creation and never
    rewritten; a change to its template is surfaced as a review row with the
    new text in a sidecar. A ``CLAUDE.md`` WITHOUT the markers is the user's
    own file and keeps the sidecar-and-review behaviour.
    """
    out: dict = {
        "live_created": [],
        "reference_written": [],
        "diverged": [],
        "managed_rerendered": [],
        "managed_backups": [],
        "claude_md_migrated": [],
        "user_section_review": [],
        "symlink_redirects": [],  # list[tuple[Path, Path]] of (orig, vco_new)
    }

    templates_dir = orchestrator_root / "templates"
    # Reuses the EXISTING root-identity home (`_canonical_path_eq`, symlink-
    # and case-safe) rather than adding a second `folder == orchestrator_root`
    # comparison — §9.3 sweep 7 wants exactly one such idiom in this module.
    is_root_target = _pi._is_root_bundle_target(orchestrator_root, folder)

    for template_name, live_rel, ref_rel in _pi._PROJECT_LEVEL_TEMPLATES:
        # v0.2.92 WP-15 half 2 — ROOT EXCLUSION. On the orchestrator root,
        # `CLAUDE.md` is rendered by install.py from
        # `templates/ORCHESTRATOR-CLAUDE.md.template` (an 889-line document
        # with its own AUTO-region owner). This loop walks the PROJECT
        # template, so comparing them compared two DIFFERENT DOCUMENTS and
        # every orchestrator root was reported diverged by construction —
        # forever, with no user action able to clear it. Skip the entry
        # entirely (a skip, not a fork: the other two entries still run,
        # because `.claude/CONTEXT_STATE.md` and `MEMORY.md` ARE the
        # project-shaped files on the root and their reference IS this
        # template's render). The stale sidecar is removed below.
        if is_root_target and live_rel == Path("CLAUDE.md"):
            if not dry_run:
                remove_stale_root_claude_md_sidecar(folder, ref_rel, log=_pi._log_auto)
            continue

        src = templates_dir / template_name
        if not src.exists():
            # Templates not shipped on this orchestrator clone — skip
            # silently. The bundle pre-install gate (`orchestrator_root`
            # validation) covers the catastrophic case.
            continue

        try:
            raw = src.read_bytes()
        except OSError:
            continue
        raw_text = raw.decode("utf-8", errors="replace")
        # Phase 1.5.B: conditional sections, then the registry pass — both in
        # `render_project_template`, the ONE pipeline `render_claude_md` also
        # uses. v0.2.101 (plan D4): the active set comes from
        # `claude_md_sections.active_sections` (the ONE resolver both render
        # paths share), which resolves the folder to its launcher.db UUID
        # internally — the call this replaced passed `str(folder)` to a
        # resolver keyed on the UUID, so explicit `project_modules` rows
        # never reached the bundle-path render. It never raises (every probe
        # soft-fails to RENDER), so no defensive fallback is needed.
        active = claude_md_sections.active_sections(
            folder, needed=claude_md_sections.tagged_features(raw_text))
        substituted = render_project_template(
            raw_text,
            label=str(live_rel),
            folder=folder,
            orchestrator_root=orchestrator_root,
            project_name=project_name,
            active_modules=active,
            sink=sink,
            swallow_template_error=True,
        ).encode("utf-8")

        # SETUP-ONLY blocks (v0.2.101 12a): the project CLAUDE.md carries the
        # scoping nudge inside its managed region, so the project render path
        # honours the SAME acknowledgement lifecycle as the root (one home:
        # `setup_sections.apply_to_render`). The strip feeds the live write,
        # the reference sidecar AND the reconcile comparison below, so an
        # acknowledged removal survives the update byte-identically and is
        # never mistaken for a user edit. Only the CLAUDE.md arm — the other
        # two templates carry no SETUP-ONLY blocks.
        setup_pending: Optional[Tuple[setup_sections.SetupBlock, ...]] = None
        if live_rel == Path("CLAUDE.md"):
            _text, setup_pending = setup_sections.apply_to_render(
                folder, substituted.decode("utf-8", errors="replace"))
            substituted = _text.encode("utf-8")

        live_target = folder / live_rel
        if not live_target.exists():
            # Missing project-level file → install the stub.
            # For CLAUDE.md specifically: the template carries the
            # VCO-managed-region markers itself (the split, review R18-01) —
            # the user section outside them is written ONLY here, at creation.
            # A marker-less template is wrapped whole (pre-split shape).
            is_claude_md = live_rel == Path("CLAUDE.md")
            parts = split_claude_md(substituted.decode("utf-8", errors="replace"))
            if is_claude_md:
                substituted = compose_claude_md(*parts).encode("utf-8")
            if not dry_run:
                try:
                    _redirect = _pi._write_file_atomic(live_target, substituted)
                    if _redirect is not None:
                        out["symlink_redirects"].append((live_target, _redirect))
                except OSError:
                    # Best-effort: skip this template if the write fails;
                    # don't fail the whole install.
                    continue
                if is_claude_md:
                    record_user_section_created(folder, raw_text, parts[1])
                    if setup_pending is not None:
                        _emit_setup_deferral(folder, setup_pending)
            out["live_created"].append(str(live_rel))
            # Don't write the reference sidecar in this case — the live
            # file IS the reference at this moment, so a sidecar is
            # redundant. A future install run (after the user edits the
            # live file) will create the sidecar then.
            continue

        # Live file already exists. v0.2.100 WP-18: a marked CLAUDE.md has its
        # managed body re-rendered (see the docstring); read the PREVIOUS
        # sidecar first — it is the previous render, the evidence that tells
        # an untouched body from an edited one.
        ref_target = folder / ref_rel
        managed_current = False
        if live_rel == Path("CLAUDE.md"):
            managed_current = reconcile_claude_md(
                folder, live_target, ref_target,
                substituted.decode("utf-8", errors="replace"), raw_text,
                dry_run=dry_run, out=out, orchestrator_root=orchestrator_root,
                project_name=project_name,
            )
            # The deferral row follows the FILE: it is emitted only when the
            # live managed body IS this render (an unmarked user-owned file or
            # a failed backup leaves the row to the clear probe, which reads
            # the live CLAUDE.md generically) and never on a dry run.
            if managed_current and not dry_run and setup_pending is not None:
                _emit_setup_deferral(folder, setup_pending)
        if not dry_run:
            try:
                _redirect = _pi._write_file_atomic(ref_target, substituted)
                if _redirect is not None:
                    out["symlink_redirects"].append((ref_target, _redirect))
            except OSError:
                continue
        out["reference_written"].append(str(ref_rel))

        # Compare existing vs reference. "Meaningfully differs" =
        # anything beyond whitespace + trailing-newline normalisation
        # (per coordinator: keep the check simple).
        try:
            existing_text = live_target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Can't read — don't flag for review; the user has a bigger
            # problem than a template diff.
            continue
        reference_text = substituted.decode("utf-8", errors="replace")
        if managed_current:
            # The managed body IS the fresh render and everything outside the
            # markers is the user's own — there is nothing to review.
            continue
        # v0.2.92 WP-15 half 1 — COMPARE LIKE AGAINST LIKE. The rule (strip
        # VCO's OWN injected regions from BOTH sides, THEN normalise
        # whitespace) lives in `vco_lib.template_divergence`.
        if meaningfully_differs(existing_text, reference_text):
            out["diverged"].append(str(live_rel))

    return out


def managed_body(text: str) -> Optional[str]:
    """The body between the VCO-managed-region markers, or ``None`` when the
    file does not carry both (in order)."""
    open_idx = text.find(_pi.MANAGED_REGION_OPEN)
    close_idx = text.find(_pi.MANAGED_REGION_CLOSE)
    if open_idx < 0 or close_idx < 0 or close_idx < open_idx:
        return None
    return text[open_idx + len(_pi.MANAGED_REGION_OPEN):close_idx].strip("\n")


# ---------------------------------------------------------------------------
# The user-section / managed-region split (review R18-01)
# ---------------------------------------------------------------------------

#: Where the newly shipped user-section template is written for the user to
#: compare with their own text (only when that template changed).
USER_SECTION_SIDECAR_REL = (
    Path(".claude") / "context" / "templates" / "CLAUDE.md.user-section.reference.md"
)
#: The recorded hash of the user-section TEMPLATE the user's text was written
#: from (at creation) or last acknowledged against (a dismissed review row).
USER_SECTION_STATE_REL = (
    Path(".claude") / "context" / "templates" / "CLAUDE.md.user-section.json"
)
#: The review row (``vco_lib/deferral_conditions.toml``).
USER_SECTION_REVIEW_CID = "claude_md_user_section_review"
#: Recorded instead of a hash when a pre-split file's EDITED text was moved
#: into the user section: nothing has been acknowledged yet.
_MIGRATED_UNACKNOWLEDGED = "migrated-unacknowledged"


def split_claude_md(text: str) -> Tuple[str, str, str]:
    """``(top, body, bottom)`` of a CLAUDE.md or of its rendered template:
    the text before the opening marker, the managed body (no blank lines at
    its ends) and everything from just after the closing marker.

    A template WITHOUT markers is all VCO-managed (``("", text, "\\n")``) —
    the pre-split shape, which :func:`compose_claude_md` turns back into
    exactly what ``merge_managed_region("", text)`` produced."""
    open_idx = text.find(_pi.MANAGED_REGION_OPEN)
    close_idx = text.find(_pi.MANAGED_REGION_CLOSE)
    if open_idx < 0 or close_idx < open_idx:
        return "", text.replace("\r\n", "\n").strip("\n"), "\n"
    return (
        text[:open_idx],
        text[open_idx + len(_pi.MANAGED_REGION_OPEN):close_idx].strip("\n"),
        text[close_idx + len(_pi.MANAGED_REGION_CLOSE):],
    )


def compose_claude_md(top: str, body: str, bottom: str) -> str:
    """The inverse of :func:`split_claude_md`."""
    return (f"{top}{_pi.MANAGED_REGION_OPEN}\n{body}\n{_pi.MANAGED_REGION_CLOSE}"
            f"{bottom}")


def _user_part(top: str, bottom: str) -> str:
    return "\n".join(normalise_for_diff(top)) + "\n\x00\n" + "\n".join(normalise_for_diff(bottom))


def user_section_template_hash(raw_template: str) -> str:
    """sha256 of the RAW template's user section (outside the markers), before
    any placeholder is rendered — so a moved clone or a renamed project is not
    a template change; only a release that edits that text is. Whitespace-only
    differences do not count (:func:`normalise_for_diff`)."""
    top, _body, bottom = split_claude_md(raw_template)
    return hashlib.sha256(_user_part(top, bottom).encode("utf-8")).hexdigest()


def _read_state(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / USER_SECTION_STATE_REL).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _body_hash(body: str) -> str:
    return hashlib.sha256("\n".join(normalise_for_diff(body)).encode("utf-8")).hexdigest()


def _write_state(folder: Path, acknowledged: str, how: str,
                 managed_body_sha256: Optional[str] = None) -> None:
    """Record the user-section acknowledgement and — when given — the hash of
    the managed body VCO last wrote (the "untouched" evidence for the next
    update, so a never-edited region is not backed up for want of a reference
    sidecar). An omitted hash keeps the recorded one."""
    prior = _read_state(folder) or {}
    if managed_body_sha256 is None:
        managed_body_sha256 = prior.get("managed_body_sha256")
    payload = {
        "acknowledged_template_sha256": acknowledged,
        "managed_body_sha256": managed_body_sha256,
        "recorded_by": how,
        "note": ("VCO bookkeeping for CLAUDE.md: the user-section template this "
                 "project's text (outside the VCO_MANAGED markers) was written "
                 "from or last reviewed against, and the managed body VCO last "
                 "wrote. Deleting it makes the next update treat CLAUDE.md as a "
                 "pre-split file."),
    }
    if all(prior.get(k) == payload[k] for k in
           ("acknowledged_template_sha256", "managed_body_sha256")):
        return  # nothing new to record: no rewrite
    payload["recorded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _pi._write_file_atomic(folder / USER_SECTION_STATE_REL,
                           (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _reference_body(previous_render: Optional[str]) -> Optional[str]:
    """The managed body of the previous render (the reference sidecar): the
    text between its markers, or — for a pre-split sidecar — all of it."""
    if previous_render is None:
        return None
    body = managed_body(previous_render)
    return body if body is not None else previous_render.strip("\n")


def _same(a: str, b: Optional[str]) -> bool:
    return b is not None and normalise_for_diff(a) == normalise_for_diff(b)


def _join_bottom(bottom: str, after: str) -> str:
    """The template's text after the closing marker, followed by whatever the
    user already had there (never dropped)."""
    if not after.strip():
        return bottom
    if not bottom.strip():
        return after
    return bottom.rstrip("\n") + "\n\n" + after.lstrip("\n")


def _notice(message: str) -> None:
    try:
        print(f"[vco] NOTICE: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a notice never breaks an update
        pass
    _pi._log_auto(message)


def _heading_lines(text: str) -> List[str]:
    """The Markdown heading lines of ``text`` (trailing whitespace dropped)."""
    return [ln.rstrip() for ln in text.splitlines() if ln.startswith("#")]


def _is_pre_split(current_body: str, fresh_body: str, fresh_top: str, *,
                  before: str, state: Optional[dict]) -> bool:
    """Is the live file in the PRE-SPLIT layout (the whole old template inside
    the markers)? Decided from the CONTENT (review R18F-01), because the
    bookkeeping can lie: the untracked state file survives a git checkout of
    a branch whose tracked CLAUDE.md is still pre-split, and a user line above
    the opening marker does not make a file split.

    * The managed body carries a heading of the user-section template (Project
      Overview, Tech Stack, Key Paths, …) that the current managed body does
      not: the old template told the user to write THERE — pre-split,
      whatever the state file or the text above the marker say.
    * Otherwise, no recorded state AND nothing of the user's above the marker
      is still pre-split (a split file always has its user section above) —
      unless the body already IS the current managed render.
    """
    fresh = set(_heading_lines(fresh_body))
    user_headings = {h for h in _heading_lines(fresh_top) if h not in fresh}
    live = {ln.rstrip() for ln in current_body.splitlines()}
    if user_headings & live:
        return True
    from vco_lib.deferral_report import strip_vco_owned_regions

    return (state is None and not strip_vco_owned_regions(before).strip()
            and not _same(current_body, fresh_body))


def reconcile_claude_md(
    folder: Path,
    live_target: Path,
    ref_target: Path,
    rendered: str,
    raw_template: str,
    *,
    dry_run: bool,
    out: dict,
    orchestrator_root: Optional[Path] = None,
    project_name: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Bring an EXISTING marked ``CLAUDE.md`` up to date. ONE function for
    the bundle update and the launcher's module toggle (review R18-01 (d)).

    * The managed region is re-rendered. A body that is exactly the previous
      render (``ref_target``, the sidecar the previous run wrote) is replaced
      silently; any other body is backed up under
      ``.claude/backups/bundle-adoptions/<ts>/CLAUDE.md`` first, and left
      untouched when that backup cannot be written.
    * The user section (outside the markers) is NEVER rewritten. When the
      user-section template differs from the one recorded at creation /
      last acknowledgement, the new version is written to
      :data:`USER_SECTION_SIDECAR_REL` and a ``claude_md_user_section_review``
      row is emitted (see :func:`_settle_user_section_review`).
    * A PRE-SPLIT file (before v0.2.100 the whole template sat inside the
      markers) is recognised from its CONTENT (:func:`_is_pre_split`), never
      from the bookkeeping alone, and migrated once: an untouched region — it
      equals the previous render, OR is a render of a template VCO released
      (``vco_lib.legacy_claude_md``: exact with this project's values, else
      structurally under any roots and name) — becomes the split layout
      (fresh user section above, fresh managed region); an edited one keeps
      every section the user wrote or edited VERBATIM, moved above the
      markers, drops only the sections equal to the released template's
      (VCO's text, re-rendered in the managed region), gets the fresh managed
      region below, is backed up WHOLE first, and the review row asks the
      user to check their section. Text above the opening marker and below
      the closing one is always kept. No user text ever leaves the live file.

    Returns True when the live managed body IS the fresh render afterwards.
    False (file untouched) for a file without markers — the user's own file —
    an unreadable one, or when a needed backup failed.
    """
    try:
        existing_bytes = live_target.read_bytes()
        existing = existing_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    current_body = managed_body(existing)
    if current_body is None:
        return False
    open_idx = existing.find(_pi.MANAGED_REGION_OPEN)
    close_idx = existing.find(_pi.MANAGED_REGION_CLOSE)
    before = existing[:open_idx]
    after = existing[close_idx + len(_pi.MANAGED_REGION_CLOSE):]
    top, body, bottom = split_claude_md(rendered)
    template_hash = user_section_template_hash(raw_template)

    previous_render: Optional[str] = None
    try:
        if ref_target.is_file():
            previous_render = ref_target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        previous_render = None
    state = _read_state(folder)
    untouched = _same(current_body, _reference_body(previous_render)) or (
        state is not None and state.get("managed_body_sha256") == _body_hash(current_body)
    )

    new_state: Tuple[str, str]
    if _is_pre_split(current_body, body, top, before=before, state=state):
        # PRE-SPLIT layout: the whole old template is the managed body. No
        # previous render to compare with (created, never updated) is NOT
        # evidence of an edit: try every released template first.
        from vco_lib.legacy_claude_md import (
            matches_a_shipped_render,
            strip_unedited_vco_sections,
        )

        if not untouched:
            matched = matches_a_shipped_render(
                current_body, folder=folder, orchestrator_root=orchestrator_root,
                names=(project_name,), db_path=db_path)
            if matched is not None:
                untouched = True
                out.setdefault("claude_md_legacy_match", []).append(matched)
        if untouched:
            merged = before + compose_claude_md(top, body, _join_bottom(bottom, after))
            new_state = (template_hash, "migrated-untouched")
        else:
            # Keep every section the user wrote or edited; drop only the
            # sections equal to a released template's (VCO's text, re-rendered
            # below in the managed region) so nothing is duplicated
            # (review R18F-05). The backup below holds the whole original.
            kept, dropped = strip_unedited_vco_sections(
                current_body, keep_headings=_heading_lines(top))
            if dropped:
                out.setdefault("claude_md_migration_dropped", []).extend(dropped)
            merged = (before + kept.rstrip("\n") + "\n\n"
                      + compose_claude_md("", body, after))
            new_state = (_MIGRATED_UNACKNOWLEDGED, "migrated-edited")
        out.setdefault("claude_md_migrated", []).append(new_state[1])
    else:
        # Split layout: the managed body carries none of the user-section
        # headings, and the user's text is outside the markers.
        merged = before + compose_claude_md("", body, after)
        new_state = ((template_hash, "state-rebuilt") if state is None else
                     (str(state.get("acknowledged_template_sha256") or ""),
                      str(state.get("recorded_by") or "")))

    if merged != existing:
        if dry_run:
            out["managed_rerendered"].append("CLAUDE.md")
        else:
            if not untouched:
                try:
                    backup_rel = _pi._backup_bytes_for_adoption(
                        folder, "CLAUDE.md", _pi._adopt_backup_timestamp(), existing_bytes,
                    )
                except Exception as exc:  # noqa: BLE001 — no captured copy ⇒ no rewrite
                    _pi._log_auto(f"CLAUDE.md managed region kept: backup failed ({exc})")
                    return False
                out["managed_backups"].append(backup_rel)
                _notice(
                    f"{live_target}: your edited pre-v0.2.100 text was kept, moved "
                    f"above the VCO_MANAGED markers (see UPDATE_DEFERRED.md); the "
                    f"previous file is backed up at {folder / backup_rel}"
                    if new_state[0] == _MIGRATED_UNACKNOWLEDGED else
                    f"{live_target}: the VCO-managed region differed from the last "
                    f"render and was replaced; the previous file is backed up at "
                    f"{folder / backup_rel}")
            try:
                _redirect = _pi._write_file_atomic(live_target, merged.encode("utf-8"))
            except OSError as exc:
                _pi._log_auto(f"CLAUDE.md managed region re-render failed: {exc}")
                return False
            if _redirect is not None:
                out["symlink_redirects"].append((live_target, _redirect))
                return False
            out["managed_rerendered"].append("CLAUDE.md")
    if not dry_run:
        try:
            _write_state(folder, *new_state, managed_body_sha256=_body_hash(body))
        except OSError as exc:
            _pi._log_auto(f"CLAUDE.md user-section state not recorded: {exc}")
        _settle_user_section_review(folder, template_hash, top, bottom, out=out)
    return True


def record_user_section_created(folder: Path, raw_template: str, managed: str) -> None:
    """A CLAUDE.md was just CREATED from this template (``managed`` = the body
    written between the markers): its user section IS the current
    user-section template, so there is nothing to review."""
    try:
        _write_state(folder, user_section_template_hash(raw_template), "created",
                     managed_body_sha256=_body_hash(managed))
    except OSError as exc:
        _pi._log_auto(f"CLAUDE.md user-section state not recorded: {exc}")


def _review_entry(folder: Path, template_hash: str, reason: str):
    from vco_lib.deferral_report import DeferralEntry

    sidecar = USER_SECTION_SIDECAR_REL.as_posix()
    if reason == "migrated":
        detected = (
            "This project's CLAUDE.md predates the split between YOUR section and "
            "VCO's managed region, and you had edited it. Your text was kept "
            "verbatim and moved ABOVE the `VCO_MANAGED` markers; VCO's current "
            "sections now sit between the markers below it. VCO sections you had "
            "not changed were not copied into your part (the managed region "
            "carries their current version); every section you edited or added "
            "was. The whole pre-migration file is backed up under "
            "`.claude/backups/bundle-adoptions/`."
        )
        todo = ("Check your section (above the markers): a VCO section you had "
                "edited is still there next to its current version in the managed "
                "region — keep what is yours, remove what the managed region now "
                f"covers — and compare the rest with `{sidecar}`, the current "
                "template for your section.")
    else:
        detected = (
            "This VCO update changed the TEMPLATE text of the part of CLAUDE.md "
            "that is yours (outside the `VCO_MANAGED` markers: introduction, "
            "Project Overview, Tech Stack, Key Paths). Your text was NOT changed. "
            f"The new template text is in `{sidecar}`."
        )
        todo = (f"Compare `{sidecar}` with your section of CLAUDE.md and copy over "
                "anything that applies to this project.")
    return DeferralEntry(
        condition_id=USER_SECTION_REVIEW_CID,
        title="Review your section of CLAUDE.md against the new template",
        detected=detected,
        why_deferred=(
            "VCO never rewrites the part of CLAUDE.md that belongs to you, so it "
            "cannot apply the template change itself. " + todo
        ),
        command_to_apply=(
            "# When you have compared the two, acknowledge it (from the orchestrator\n"
            "# root). The row stays away until the template changes again:\n"
            f"python -m vco_lib.project_init dismiss-deferral --folder "
            f"'{folder}' --condition-id {USER_SECTION_REVIEW_CID}"
        ),
        severity="info",
        dismiss_fields={"template_sha256": template_hash, "reason": reason},
    )


def _write_user_section_sidecar(folder: Path, top: str, bottom: str) -> None:
    text = (
        "<!-- VCO reference (not loaded by Claude Code): the template for YOUR "
        "section of CLAUDE.md, as this VCO version ships it. Compare it with the "
        "text outside the VCO_MANAGED markers in your CLAUDE.md; VCO never edits "
        "that text for you. Rewritten on every update while the review row is "
        "open. -->\n\n"
        + top.rstrip("\n") + "\n\n"
        "<!-- (the VCO-managed region sits here in CLAUDE.md; it is not part of "
        "your section) -->\n"
        + bottom
    )
    _pi._write_file_atomic(folder / USER_SECTION_SIDECAR_REL, text.encode("utf-8"))


def _settle_user_section_review(folder: Path, template_hash: str, top: str,
                                bottom: str, *, out: dict) -> None:
    """Emit or clear the ONE ``claude_md_user_section_review`` row.

    Paired resolution, no probe: the row is emitted while the recorded
    acknowledgement differs from the shipped user-section template, and
    resolved on the run that finds them equal. Acknowledging = dismissing the
    row (``dismiss-deferral``): the dismissal is keyed on the template hash, so
    this run sees it, RECORDS the current hash as acknowledged, and the row
    stays away until a release changes the template again. Soft-fail.
    """
    state = _read_state(folder) or {}
    acknowledged = state.get("acknowledged_template_sha256")
    reason = "migrated" if acknowledged == _MIGRATED_UNACKNOWLEDGED else "template_changed"
    fields = {"template_sha256": template_hash, "reason": reason}
    pending = acknowledged != template_hash
    try:
        if pending:
            from vco_lib.deferral_dismissal import dismissal_suppresses

            if dismissal_suppresses(folder, USER_SECTION_REVIEW_CID, fields):
                _write_state(folder, template_hash, "acknowledged")
                pending = False
        from vco_lib.deferral_emit import WriteGate, _first_detected, locked_report
        from vco_lib.deferral_report import _DEFERRED_JSON_REL, _DEFERRED_REL

        if not pending and not ((folder / _DEFERRED_REL).exists()
                                or (folder / _DEFERRED_JSON_REL).exists()):
            return  # nothing to clear, and a clean project never gets a ledger
        if pending:
            _write_user_section_sidecar(folder, top, bottom)
        gate = WriteGate()
        with locked_report(folder, gate=gate) as report:
            prior = report.entry_for(USER_SECTION_REVIEW_CID)
            if pending:
                entry = _first_detected(prior, _review_entry(folder, template_hash, reason))
                if prior == entry:
                    gate.write = False
                else:
                    report.add_entry(entry)
                    out.setdefault("user_section_review", []).append(reason)
            elif prior is not None:
                report.mark_resolved(USER_SECTION_REVIEW_CID)
            else:
                gate.write = False
    except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
        _pi._log_auto(f"CLAUDE.md user-section review not settled: {exc}")


# ---------------------------------------------------------------------------
# CLAUDE.md re-render entrypoint (Phase 1.5.B)
#
# Wired into the DiagramsTab toggle (Phase 1.3 — sibling): when the user
# flips a module toggle, the launcher's Tauri command
# ``set_project_module_enabled`` calls this CLI via subprocess (Option A
# pattern from Phase 0.B's config_projection — Rust shells out to Python
# for byte-layout authority over template rendering).
#
# Pipeline:
#   1. Read ``templates/CLAUDE.md.template`` from the orchestrator clone.
#   2. ``render_conditional_blocks`` strips per-module sections.
#   3. the ``vco_lib.materialize`` registry pass (``render_project_template``)
#      resolves ``{{PROJECT_NAME}}`` etc.
#   4. ``reconcile_claude_md`` (the SAME rules as the bundle update) replaces
#      the managed body — backing up an edited one — and never touches the
#      user section outside the markers; a missing file is created whole.
#   5. Atomic writes via ``_write_file_atomic``.
# ---------------------------------------------------------------------------


def render_claude_md(
    folder: Path,
    *,
    orchestrator_root: Path,
    project_name: str,
    project_id: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Re-render ``<folder>/CLAUDE.md`` from the orchestrator template,
    preserving any user content outside the VCO-managed-region markers.

    Used by the launcher's ``set_project_module_enabled`` Tauri command
    (via the ``re-render-claude-md`` CLI subcommand) when the user
    toggles a module on/off in DiagramsTab or any future per-module
    settings UI.

    Idempotent on the managed body: feeding the same active-modules set
    in twice produces byte-identical output.

    Args:
        folder: Target project folder containing (or about to contain)
            ``CLAUDE.md``.
        orchestrator_root: Orchestrator clone root (source of the
            ``templates/CLAUDE.md.template`` file).
        project_name: Display name used to resolve ``{{PROJECT_NAME}}``.
        project_id: launcher.db ``projects``-table UUID to read
            ``project_modules`` rows for, when the caller already holds it
            (the launcher passes the id it toggled). ``None`` resolves the
            UUID from ``folder`` (v0.2.101, plan D4 — the old
            ``str(folder)`` default could never match a UUID-keyed table).
        db_path: Override the default ``~/.vct/launcher.db`` resolution
            (used by tests).

    Returns:
        A result dict::

            {
              "wrote_path": "<abs path>",
              "active_modules": [<sorted feature-section names the resolver
                                 probed for this template>],
              "managed_region_present_before": bool,
              "rendered_bytes": <int>,
              "managed_backups": [<rel backup path>...],  # edited region
              "user_section_review": [<reason>...],       # row emitted
            }

    Raises:
        FileNotFoundError: ``templates/CLAUDE.md.template`` missing on
            the orchestrator clone.
        _pi.TemplateError: malformed conditional tag or out-of-order markers
            in the existing CLAUDE.md.
        OSError: write failure (atomic-write: no partial file on disk), or
            an edited managed region whose backup could not be written (the
            file is then left untouched).
    """
    template_path = orchestrator_root / "templates" / "CLAUDE.md.template"
    if not template_path.is_file():
        raise FileNotFoundError(
            f"CLAUDE.md template not found at {template_path}. The "
            f"orchestrator clone may be incomplete; re-run install.py "
            f"--update."
        )

    raw_bytes = template_path.read_bytes()
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    # Resolve the active sections through the ONE resolver (v0.2.101, plan
    # D2/D4): `project_id` stays an override for the launcher, which passes
    # the id it already holds; `None` resolves the folder to its launcher.db
    # UUID internally (the old `str(folder)` default could never match a
    # UUID-keyed table). `needed` keeps the probes to what this template can
    # use — the user template has no RL section, so the license validator is
    # never consulted on this path.
    active = claude_md_sections.active_sections(
        folder, db_path=db_path, project_id=project_id,
        needed=claude_md_sections.tagged_features(raw_text))

    # Pipeline: conditional → registry pass (the SAME function the bundle
    # update uses, so both callers agree on every value incl. PROJECT_NAME).
    rendered_body = render_project_template(
        raw_text,
        label="CLAUDE.md",
        folder=folder,
        orchestrator_root=orchestrator_root,
        project_name=project_name,
        active_modules=active,
        db_path=db_path,
    )

    # SETUP-ONLY lifecycle (v0.2.101 12a): the same acknowledgement strip the
    # bundle path and the root renderer apply (one home:
    # `setup_sections.apply_to_render`), before any write below.
    rendered_body, setup_pending = setup_sections.apply_to_render(folder, rendered_body)

    # ONE set of rules with the bundle update (review R18-01 (d)): an existing
    # marked file goes through `reconcile_claude_md` — the user section is
    # never touched, an edited managed body is backed up first. The reference
    # sidecar is refreshed too, so the next bundle update compares against
    # THIS render and does not mistake a toggle for a user edit.
    target = folder / "CLAUDE.md"
    ref_target = folder / Path(".claude") / "context" / "templates" / "CLAUDE.md.reference.md"
    existing = ""
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""

    had_markers = (
        _pi.MANAGED_REGION_OPEN in existing and _pi.MANAGED_REGION_CLOSE in existing
    )
    out: dict = {"managed_rerendered": [], "managed_backups": [],
                 "claude_md_migrated": [], "user_section_review": [],
                 "symlink_redirects": []}
    if had_markers:
        # Raises TemplateError on out-of-order / half markers, as before.
        _pi.merge_managed_region(existing_claude_md=existing, new_managed_body="")
        if not reconcile_claude_md(folder, target, ref_target, rendered_body, raw_text,
                                   dry_run=False, out=out,
                                   orchestrator_root=orchestrator_root,
                                   project_name=project_name, db_path=db_path):
            raise OSError(
                f"{target}: the VCO-managed region differs from the last render "
                f"and could not be backed up; left untouched"
            )
    elif existing.strip():
        # The user's own file (no markers): the managed region is prepended
        # and their text kept below it. It has no VCO user section to review.
        parts = split_claude_md(rendered_body)
        merged = _pi.merge_managed_region(
            existing_claude_md=existing, new_managed_body=parts[1],
        )
        _pi._write_file_atomic(target, merged.encode("utf-8"))
        record_user_section_created(folder, raw_text, parts[1])
    else:
        parts = split_claude_md(rendered_body)
        _pi._write_file_atomic(target, compose_claude_md(*parts).encode("utf-8"))
        record_user_section_created(folder, raw_text, parts[1])
    try:
        _pi._write_file_atomic(ref_target, rendered_body.encode("utf-8"))
    except OSError as exc:
        _pi._log_auto(f"CLAUDE.md reference sidecar not refreshed: {exc}")

    # The row follows the write (every branch above has written the live file
    # by here — a failure raises instead of reaching this point). `pending`
    # is None only on malformed markers: leave the ledger alone then.
    if setup_pending is not None:
        _emit_setup_deferral(folder, setup_pending)

    merged_bytes = target.read_bytes()
    return {
        "wrote_path": str(target),
        "active_modules": sorted(active),
        "managed_region_present_before": had_markers,
        "rendered_bytes": len(merged_bytes),
        "managed_backups": out["managed_backups"],
        "user_section_review": out["user_section_review"],
    }
