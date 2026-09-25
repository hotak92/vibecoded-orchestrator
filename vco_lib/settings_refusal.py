# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A settings file VCO cannot safely edit is left byte-identical — visibly.

v0.2.97. The env-projection writers (``vco_lib.config_projection`` and, through
it, the launcher) used to read a ``.claude/settings.json`` /
``.vscode/settings.json`` they could not parse as ``{}`` and write that back
with only their own ``env`` block in it: every hook, permission and other
setting in the user's file was destroyed, with a log line nobody reads as the
only trace. The owner rule is "don't auto-destroy user data": when the writer
cannot positively read the file, it does nothing to it — and a refusal nobody
sees is only half a fix, so it also leaves a deferral naming the file and what
the user must do.

This module is the ONE home for that:

* :func:`load_for_edit` — the read that decides between "edit it", "create
  it" and "leave it alone, because <reason>";
* :func:`edit_text_or_record` — the write-side twin for a read-modify-write:
  the bytes that make the file hold the new data (a JSONC file edited in
  place, comments kept), or — when that edit cannot be verified — the refusal
  recorded and ``None``;
* :func:`record` / :func:`clear_recorded` — the deferral a refusal leaves in
  the project's ``UPDATE_DEFERRED`` ledger, and its paired clear on the next
  successful write of the same surface;
* :func:`refusal_still_applies` — the read-only clear probe
  (``deferral_probes.settings_write_refusal_still_applies``), so the entry
  also ends when the user repairs the file and no write runs.

Pure standard library plus the deferral machinery; the exception callers
raise lives in ``config_projection`` (``SettingsWriteRefused``), which is where
the writers are.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from vco_lib import jsonc_edit
from vco_lib.deferral_emit import emit, resolve_conditions
from vco_lib.deferral_report import DeferralEntry, DeferralReport

__all__ = [
    "CID_PREFIX",
    "KIND_EDIT_REFUSED",
    "KIND_UNPARSEABLE",
    "Refusal",
    "clear_recorded",
    "condition_id",
    "edit_text_or_record",
    "load_for_edit",
    "record",
    "refusal_still_applies",
]

#: ``settings_write_refused_<surface>`` — one entry per surface, so a refusal
#: of ``.vscode/settings.json`` never overwrites the one for
#: ``.claude/settings.json``. Registered as a glob family in
#: ``deferral_conditions.toml``.
CID_PREFIX = "settings_write_refused_"

#: The file is not JSON(C), not UTF-8, not an object at the top level, or not
#: readable at all.
KIND_UNPARSEABLE = "unparseable"
#: The file IS JSONC, but the in-place edit could not be verified
#: (:class:`vco_lib.jsonc_edit.JsoncEditRefused`).
KIND_EDIT_REFUSED = "jsonc_edit_refused"


@dataclass(frozen=True)
class Refusal:
    """One file a writer left alone, and why."""

    path: Path
    kind: str
    reason: str

    def sentence(self) -> str:
        return f"{self.path} was NOT updated: {self.reason}"

    def as_json(self) -> dict:
        return {"path": str(self.path), "kind": self.kind, "reason": self.reason}


def condition_id(surface: str) -> str:
    return f"{CID_PREFIX}{surface}"


def _strict_parse_position(raw: str) -> str:
    """Where a strict parser stops, in the file's own line/column terms.

    :func:`jsonc_edit.loads` re-joins tokens before its final parse, so its
    positions do not match the file; the strict parser's do.
    """
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    return "the file ends early"


def load_for_edit(path: Path) -> Union[tuple[dict, str], Refusal, None]:
    """Read a JSON(C) settings file for a read-modify-write.

    ``None`` — the file does not exist; the writer may create it.
    ``(data, raw)`` — an object the writer may edit (``raw`` for an in-place
    JSONC edit). :class:`Refusal` — the file exists but cannot be read as an
    object; the writer must not touch it, and the refusal says why.
    """
    if not path.exists():
        return None
    try:
        return jsonc_edit.read_object(path)
    except UnicodeDecodeError:
        reason = "it is not UTF-8 text"
    except ValueError as exc:
        if str(exc).startswith("its top level"):
            reason = str(exc)
        else:
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                raw = ""
            reason = (
                "it is not valid JSON or JSONC (a strict parser stops at: "
                f"{_strict_parse_position(raw)})"
            )
    except OSError as exc:
        reason = f"it cannot be read ({exc.strerror or exc})"
    return Refusal(path, KIND_UNPARSEABLE, reason)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return str(path)


def record(
    project_root: Path,
    surface: str,
    refusal: Refusal,
    *,
    project_id: Optional[str] = None,
    retry_command: Optional[str] = None,
) -> None:
    """Leave the refusal in ``project_root``'s deferral ledger. Soft-fail:
    the ledger is observability, and the file is already safe.

    ``retry_command`` is the command that re-runs THIS writer; the default
    (with ``project_id``) is the env projection's ``apply``."""
    rel = _rel(project_root, refusal.path)
    try:
        digest = _sha256(refusal.path)
    except OSError:
        digest = ""
    retry = (
        f"{retry_command}\n" if retry_command
        else f"python -m vco_lib.config_projection apply --project-id {project_id}\n"
        if project_id
        else ""
    )
    emit(
        project_root,
        DeferralEntry(
            condition_id=condition_id(surface),
            title=f"`{rel}` was not updated — VCO left it untouched",
            detected=(
                f"VCO had to update `{rel}`, but {refusal.reason}. Nothing in the "
                "file was changed."
            ),
            why_deferred=(
                "Writing a settings file VCO cannot safely edit would replace "
                "everything in it — hooks, permissions, every other setting — with "
                "VCO's own block. VCO never does that: the file is yours, so the "
                "repair is too."
            ),
            command_to_apply=(
                f"# Repair `{rel}` by hand (the reason above names what is wrong), or\n"
                "# move it aside so VCO creates a fresh one holding only its block.\n"
                "# VCO retries the next time it updates this file; to retry now:\n"
                f"{retry}"
                "# This entry clears itself once the file can be edited."
            ),
            severity="warning",
            dismiss_fields={"path": rel, "kind": refusal.kind, "sha256": digest},
        ),
    )


def edit_text_or_record(
    project_root: Path,
    surface: str,
    path: Path,
    raw: str,
    data: dict,
    *,
    indent: int = 2,
    retry_command: Optional[str] = None,
) -> Optional[str]:
    """The text to write so ``path`` (whose current text is ``raw``) holds
    ``data`` — or ``None``, with the refusal recorded, when a JSONC file
    cannot be edited in place verifiably. Strict JSON is re-serialised
    (``indent``, trailing newline); JSONC is edited member by member with
    every comment kept (:func:`vco_lib.jsonc_edit.dumps_preserving`)."""
    text = jsonc_edit.dumps_preserving(raw, data, indent=indent)
    if text is None:
        record(project_root, surface, Refusal(
            path, KIND_EDIT_REFUSED,
            "it has comments or trailing commas (JSONC) and this change could "
            "not be made in place without risking other content; edit it by "
            "hand, or remove the comments",
        ), retry_command=retry_command)
    return text


def clear_recorded(project_root: Path, surface: str) -> None:
    """Paired clear: the surface was just written, so a recorded refusal of it
    is over. Reads the ledger first, so the common case (nothing recorded)
    writes nothing."""
    cid = condition_id(surface)
    try:
        if not DeferralReport.read(project_root).has_condition(cid):
            return
    except Exception:  # noqa: BLE001 — an unreadable ledger is not ours to fix here
        return
    resolve_conditions(project_root, [cid])


def refusal_still_applies(folder: Path, entry: Any) -> Optional[bool]:
    """Clear probe for ``settings_write_refused_*`` (tri-state, read-only).

    ``False`` only on positive evidence: the file is gone (the next write
    creates it) or — for an unparseable file — it now reads as an object.
    A JSONC edit that was refused is still refused while the bytes are the
    ones it was refused on (``True``); once they change, only the next write
    can tell (``None``, and its paired clear ends the entry).
    """
    fields = getattr(entry, "dismiss_fields", None) or {}
    rel, kind = fields.get("path"), fields.get("kind")
    if not rel or kind not in (KIND_UNPARSEABLE, KIND_EDIT_REFUSED):
        return None
    path = Path(folder) / rel
    try:
        loaded = load_for_edit(path)
        if loaded is None:
            return False
        if isinstance(loaded, Refusal):
            return True
        if kind == KIND_UNPARSEABLE:
            return False
        return True if _sha256(path) == fields.get("sha256") else None
    except OSError:
        return None
