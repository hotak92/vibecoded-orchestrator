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

Helpers still owned by ``project_init`` (conditional sections, the managed
region merge, atomic writes, adoption backups) are reached through the module
object at CALL time (``_pi.name``), so a test that patches them on
``project_init`` patches them here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from vco_lib import project_init as _pi
from vco_lib.template_divergence import (
    meaningfully_differs,
    normalise_for_diff,
    remove_stale_root_claude_md_sidecar,
)

__all__ = [
    "install_project_level_templates",
    "managed_body",
    "render_claude_md",
    "render_project_template",
    "rerender_managed_claude_md",
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
    (the same pipeline as ``render_claude_md``) — before this, a template
    change or a moved clone never reached an existing project through the
    update path, only a ``.reference.md`` sidecar did. Content outside the
    markers is the user's and is preserved verbatim. A managed body that is
    exactly the previous render (it equals the sidecar the previous run
    wrote) is replaced silently; any other body is backed up to
    ``.claude/backups/bundle-adoptions/<ts>/CLAUDE.md`` first (the adoption
    rule) and, when that backup cannot be written, left untouched and flagged
    for review. A ``CLAUDE.md`` WITHOUT the markers is the user's own file and
    keeps the sidecar-and-review behaviour.
    """
    out: dict = {
        "live_created": [],
        "reference_written": [],
        "diverged": [],
        "managed_rerendered": [],
        "managed_backups": [],
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
        # Phase 1.5.B: conditional sections, then the registry pass — both in
        # `_render_project_template`, the ONE pipeline `render_claude_md` also
        # uses. The project folder is the module resolver's project_id (Phase
        # 1.1's launcher DB keys on the sanitised folder path).
        try:
            active = _pi.resolve_active_modules(str(folder))
        except Exception:
            # Defensive: any unexpected resolver failure falls back to
            # defaults so install never breaks.
            active = set(_pi._DEFAULT_ACTIVE_MODULES)
        substituted = render_project_template(
            raw.decode("utf-8", errors="replace"),
            label=str(live_rel),
            folder=folder,
            orchestrator_root=orchestrator_root,
            project_name=project_name,
            active_modules=active,
            sink=sink,
            swallow_template_error=True,
        ).encode("utf-8")

        live_target = folder / live_rel
        if not live_target.exists():
            # Missing project-level file → install the stub.
            # For CLAUDE.md specifically, wrap the substituted body in
            # the VCO-managed-region markers so future re-renders can
            # safely replace only the managed body (preserving any
            # user-added content below the closing marker).
            if live_rel == Path("CLAUDE.md"):
                wrapped = _pi.merge_managed_region(
                    existing_claude_md="",
                    new_managed_body=substituted.decode("utf-8", errors="replace"),
                )
                substituted = wrapped.encode("utf-8")
            if not dry_run:
                try:
                    _redirect = _pi._write_file_atomic(live_target, substituted)
                    if _redirect is not None:
                        out["symlink_redirects"].append((live_target, _redirect))
                except OSError:
                    # Best-effort: skip this template if the write fails;
                    # don't fail the whole install.
                    continue
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
            managed_current = rerender_managed_claude_md(
                folder, live_target, ref_target,
                substituted.decode("utf-8", errors="replace"),
                dry_run=dry_run, out=out,
            )
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


def rerender_managed_claude_md(
    folder: Path,
    live_target: Path,
    ref_target: Path,
    body: str,
    *,
    dry_run: bool,
    out: dict,
) -> bool:
    """Replace the managed body of an existing marked ``CLAUDE.md``.

    Returns True when, after this call, the live managed body IS ``body``
    (re-rendered now, or already current). Returns False — and leaves the file
    untouched — when the file has no markers (the user's own file), cannot be
    read, or when a body that differs from the previous render could not be
    backed up first (never destroy bytes without a captured copy).
    """
    try:
        existing_bytes = live_target.read_bytes()
        existing = existing_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    current_body = managed_body(existing)
    if current_body is None:
        return False
    try:
        merged = _pi.merge_managed_region(existing_claude_md=existing, new_managed_body=body)
    except _pi.TemplateError:
        return False
    if merged == existing:
        return True
    if dry_run:
        out["managed_rerendered"].append("CLAUDE.md")
        return True
    # Untouched = the body equals the previous render, which the previous run
    # wrote as the sidecar. No sidecar (first update after a fresh install
    # whose file was never edited cannot be told apart from an edited one) ⇒
    # back up: a spare backup is cheap, a lost edit is not.
    previous_render: Optional[str] = None
    try:
        if ref_target.is_file():
            previous_render = ref_target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        previous_render = None
    untouched = previous_render is not None and (
        normalise_for_diff(current_body) == normalise_for_diff(previous_render.strip("\n"))
    )
    if not untouched:
        try:
            backup_rel = _pi._backup_bytes_for_adoption(
                folder, "CLAUDE.md", _pi._adopt_backup_timestamp(), existing_bytes,
            )
        except Exception as exc:  # noqa: BLE001 — no captured copy ⇒ no rewrite
            _pi._log_auto(f"CLAUDE.md managed region kept: backup failed ({exc})")
            return False
        out["managed_backups"].append(backup_rel)
    try:
        _redirect = _pi._write_file_atomic(live_target, merged.encode("utf-8"))
    except OSError as exc:
        _pi._log_auto(f"CLAUDE.md managed region re-render failed: {exc}")
        return False
    if _redirect is not None:
        out["symlink_redirects"].append((live_target, _redirect))
        return False
    out["managed_rerendered"].append("CLAUDE.md")
    return True


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
#   3. ``_apply_template_subs`` resolves ``{{PROJECT_NAME}}`` etc.
#   4. ``merge_managed_region`` replaces the body inside the markers
#      while preserving any user-added content outside.
#   5. Atomic write via ``_write_file_atomic``.
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
        project_id: Project id/slug used to look up
            ``project_modules`` rows. Defaults to ``str(folder)`` so the
            stub resolver (no DB) returns default-on modules — matches
            the install-time behaviour.
        db_path: Override the default ``~/.vct/launcher.db`` resolution
            (used by tests).

    Returns:
        A result dict::

            {
              "wrote_path": "<abs path>",
              "active_modules": [<sorted module names>],
              "managed_region_present_before": bool,
              "rendered_bytes": <int>,
            }

    Raises:
        FileNotFoundError: ``templates/CLAUDE.md.template`` missing on
            the orchestrator clone.
        _pi.TemplateError: malformed conditional tag or out-of-order markers
            in the existing CLAUDE.md.
        OSError: write failure (atomic-write: no partial file on disk).
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

    # Resolve active modules. Default project_id to the folder path so
    # the stub resolver (no DB) returns the default-on set — matches
    # install-time behaviour.
    effective_project_id = project_id if project_id is not None else str(folder)
    active = _pi.resolve_active_modules(effective_project_id, db_path=db_path)

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

    # Read existing CLAUDE.md (may not exist).
    target = folder / "CLAUDE.md"
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
    else:
        existing = ""

    had_markers = (
        _pi.MANAGED_REGION_OPEN in existing and _pi.MANAGED_REGION_CLOSE in existing
    )

    merged = _pi.merge_managed_region(
        existing_claude_md=existing,
        new_managed_body=rendered_body,
    )

    merged_bytes = merged.encode("utf-8")
    _pi._write_file_atomic(target, merged_bytes)

    return {
        "wrote_path": str(target),
        "active_modules": sorted(active),
        "managed_region_present_before": had_markers,
        "rendered_bytes": len(merged_bytes),
    }
