# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A secret the USER put in a committable settings file: reported, never removed.

v0.2.97. ``user_secret_values_retained_in_tree`` covers values VCO itself wrote
before v0.2.73 — and every env refresh removes those. A secret-SHAPED key the
launcher does not know (a hand-added ``MY_TOKEN`` in ``.claude/settings.json``
``env``) is different: VCO did not write it, so VCO must not delete it, but
the file is often version-controlled and the value can leak with the
repository. This condition, ``user_owned_secret_value_in_tree``, says exactly
that and hands the decision to the user: move the value into the secrets store
and delete the key, or dismiss (keep it deliberately).

One home for the condition: detection (:func:`found`), the bundle-update
emitter (:func:`emit_deferral`), and the read-only clear probe
(:func:`still_present`, registered in ``vco_lib.deferral_probes``). The
secret-name heuristic is the ONE home,
:func:`vco_lib.secrets_audit.is_secret_shaped_env_key`; no value is ever read
out — only whether one is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from vco_lib import jsonc_edit

__all__ = ["CID", "emit_deferral", "found", "still_present"]

CID = "user_owned_secret_value_in_tree"

#: The JSON env surfaces a user can hand-edit: ``(file, env-block key)``.
_SURFACES: tuple[tuple[str, str], ...] = (
    (".claude/settings.json", "env"),
    (".vscode/settings.json", "claude-code.env"),
)


def _vco_written_keys(folder: Path, known_keys: Optional[Iterable[str]]) -> set[str]:
    """Keys VCO itself writes (canonical, ``GITHUB_TOKEN`` included) or wrote
    before v0.2.73 (the launcher-known user secrets) — the sibling condition's
    and every env refresh's business, never this one's."""
    from vco_lib.config_projection import (
        known_user_secret_keys_for_folder,
        list_canonical_keys,
    )

    known = known_user_secret_keys_for_folder(folder) if known_keys is None else known_keys
    return set(known) | list_canonical_keys()


def found(folder: Path, *, known_keys: Optional[Iterable[str]] = None) -> dict[str, list[str]]:
    """``{file: [key NAMES]}`` — secret-shaped keys with a non-empty value that
    the user put in a JSON env block. Empty when there are none. Soft: an
    unreadable file contributes nothing."""
    from vco_lib.secrets_audit import is_secret_shaped_env_key

    folder = Path(folder)
    vco_written = _vco_written_keys(folder, known_keys)
    hits: dict[str, list[str]] = {}
    for rel, env_key in _SURFACES:
        loaded = jsonc_edit.load_object(folder / rel) if (folder / rel).is_file() else None
        block = loaded[0].get(env_key) if loaded is not None else None
        if not isinstance(block, dict):
            continue
        names = sorted(
            k for k, v in block.items()
            if k not in vco_written and isinstance(v, str) and v and is_secret_shaped_env_key(k)
        )
        if names:
            hits[rel] = names
    return hits


def still_present(folder: Path) -> bool:
    """The clear probe's question: does any such key still carry a value?"""
    return bool(found(folder))


def emit_deferral(folder: Path) -> None:
    """Record the condition in ``folder``'s deferral ledger when it applies.

    Honours the generic dismissal (``python -m vco_lib.project_init
    dismiss-deferral``): the dismiss key is the set of ``file:KEY`` names, so a
    user who keeps a key deliberately is not asked again — until a different
    set appears."""
    from vco_lib.deferral_dismissal import dismissal_suppresses
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    folder = Path(folder)
    hits = found(folder)
    if not hits:
        return
    fields = {"keys": sorted(f"{rel}:{name}" for rel, names in hits.items() for name in names)}
    if dismissal_suppresses(folder, CID, fields):
        return
    where = "; ".join(f"`{rel}`: {', '.join(names)}" for rel, names in hits.items())
    emit(folder, DeferralEntry(
        condition_id=CID,
        title="A secret-shaped key with a value sits in a committable settings file",
        detected=(
            f"{where} — each of these env keys looks like a secret and carries a "
            "value (key names only; VCO never reads a value out). VCO did not "
            "write them, so VCO will NOT remove them."
        ),
        why_deferred=(
            "These files are often committed to version control, where a secret "
            "leaks with the repository. Whether the value belongs there is your "
            "decision: move it into the secrets store and delete the key from the "
            "file, or keep it deliberately and dismiss this entry."
        ),
        command_to_apply=(
            "# 1. Store each value as a secret under the SAME key name:\n"
            "#    launcher: Projects -> this project -> Secrets -> add the key; or,\n"
            "#    without the launcher (file store), paste the value then Ctrl-D:\n"
            "#      vct set --project <this project's launcher name> --key <KEY> --confirm-tty\n"
            "#    Consumers resolve it at need (vct_secrets_resolve.sh / agent_secrets.get).\n"
            "# 2. Delete the key from the file named above. If it was committed,\n"
            "#    rotate the secret. This entry clears itself once the key is gone\n"
            "#    or its value is empty.\n"
            "# To keep it deliberately instead:\n"
            f"python -m vco_lib.project_init dismiss-deferral --folder {str(folder)!r} "
            f"--condition-id {CID}"
        ),
        severity="warning",
        dismiss_fields=fields,
    ))
