# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The per-machine half of a project's Claude Code settings env (v0.2.104 L5).

VCO writes the env keys whose value describes THIS machine (absolute paths,
launcher ids, service URLs/ports — :data:`vco_lib.config_projection.MACHINE_LOCAL_ENV_KEYS`)
into ``.claude/settings.local.json`` ``env`` instead of the committable
``.claude/settings.json``. This module is the one home of the rules that
decide WHETHER and WHAT VCO may write there:

* **The layer must be private** (review R14B B1). ``settings.local.json`` is
  kept out of git by an exclude line, and an exclude line never un-tracks a
  file the user already committed. When git TRACKS the file, VCO writes no
  machine value into it — the project keeps the pre-L5 layout (every key in
  ``settings.json``) so routing works — and records the action-required
  :data:`CID_TRACKED` entry, whose remedy is ``git rm --cached``. When git's
  answer is not a clean yes/no (it refuses the repository — ``safe.directory``
  "dubious ownership" — or is broken) the layer is not used either (no guess):
  one WARNING log line and the environmental :data:`CID_UNKNOWN` entry name
  git's error and the remedy (review R18 LOW-3).
* **A value the user put there stays** (review R14B B6). Claude Code merges
  ``settings.local.json`` over ``settings.json``, so before L5 that file was
  the user's per-machine override channel. A machine key VCO finds there is
  VCO's only on value evidence: it equals what VCO projects now, or a value
  VCO itself wrote before — recorded in :data:`WRITTEN_REL` (git-excluded
  runtime state), or still standing in VCO's ``.claude/env`` managed block or
  ``settings.json`` (VCO's pre-L5 home), read BEFORE this apply writes them.
  Any other value is the user's: kept untouched and listed in the
  informational :data:`CID_USER_KEPT` entry.

The drift check (:mod:`vco_lib.env_projection_check`), the root install
(``install.py``) and the projection writer all ask this module, so the three
can never disagree about the file.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "CID_TRACKED",
    "CID_UNKNOWN",
    "CID_USER_KEPT",
    "LOCAL_REL",
    "NON_CANONICAL_MACHINE_ENV_KEYS",
    "WRITTEN_REL",
    "is_machine_env_key",
    "layer_probe",
    "layer_state",
    "layer_unknown_still",
    "move_machine_env_out_of_settings",
    "sync_tracked_entry",
    "tracked_still",
    "user_kept_keys",
    "user_kept_still",
    "write_local_env",
]

LOCAL_REL = ".claude/settings.local.json"
#: What VCO last wrote into ``settings.local.json`` ``env``, per key — the
#: evidence that a value there is VCO's. Under ``.claude/state/`` (VCO's
#: runtime namespace, git-excluded).
WRITTEN_REL = ".claude/state/settings-local-vco-values.json"

CID_TRACKED = "settings_local_json_tracked"
CID_UNKNOWN = "settings_local_layer_unknown"
CID_USER_KEPT = "settings_local_user_value_kept"

#: Machine-endpoint env keys VCO writes that are NOT canonical projection keys
#: (``install.py``'s root settings defaults carry ``GRPC_PORT``, the legacy
#: alias of ``WEAVIATE_GRPC_PORT``). Same value class as
#: :data:`vco_lib.config_projection.MACHINE_LOCAL_ENV_KEYS`.
NON_CANONICAL_MACHINE_ENV_KEYS: frozenset[str] = frozenset({"GRPC_PORT", "WEAVIATE_GRPC_PORT"})

USABLE = "usable"
TRACKED = "tracked"
UNKNOWN = "unknown"

_GIT_TIMEOUT_S = 15

_log = logging.getLogger(__name__)


def is_machine_env_key(key: str) -> bool:
    """Is ``key``'s value a machine path, id or endpoint (any VCO writer)?"""
    from vco_lib.config_projection import MACHINE_LOCAL_ENV_KEYS

    return key in MACHINE_LOCAL_ENV_KEYS or key in NON_CANONICAL_MACHINE_ENV_KEYS


# ─── B1: is the per-machine file private? ───────────────────────────────


def _git(folder: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        return subprocess.run(
            ["git", "-C", str(folder), *args], capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, check=False, env=env,
        )
    except FileNotFoundError:
        return None  # no git on this machine
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 128, "", "git could not run")


def _git_error(proc: subprocess.CompletedProcess, what: str) -> str:
    err = (proc.stderr or "").strip().splitlines()
    return err[0].strip() if err else f"git {what} exited {proc.returncode}"


def layer_probe(folder: Path) -> tuple[str, str]:
    """``(state, git_error)`` for ``folder``'s ``.claude/settings.local.json``:
    :data:`USABLE`, :data:`TRACKED` or :data:`UNKNOWN`, and — for
    :data:`UNKNOWN` only — git's own error line (else ``""``). Local only (no
    network); read-only.

    * not a git work tree, or no git on the machine → usable (nothing to commit
      it with; the exclude line covers the file once a repository appears);
    * ``git ls-files --error-unmatch`` exit 0 → tracked; exit 1 → usable;
    * anything else (git refuses the repository, broke, timed out) → unknown —
      the caller must not write a machine value it cannot prove stays private.
    """
    folder = Path(folder)
    inside = _git(folder, "rev-parse", "--is-inside-work-tree")
    if inside is None:
        return USABLE, ""
    if inside.returncode != 0:
        # 128 = not a git repository; anything else is git failing.
        if inside.returncode == 128 and "not a git repository" in (inside.stderr or "").lower():
            return USABLE, ""
        return UNKNOWN, _git_error(inside, "rev-parse")
    if inside.stdout.strip() != "true":
        return USABLE, ""  # inside a .git dir or a bare repo — no work tree to commit from
    probe = _git(folder, "ls-files", "--error-unmatch", "--", LOCAL_REL)
    if probe is None:
        return USABLE, ""
    if probe.returncode == 0:
        return TRACKED, ""
    if probe.returncode == 1:
        return USABLE, ""
    return UNKNOWN, _git_error(probe, "ls-files")


def layer_state(folder: Path) -> str:
    """The state half of :func:`layer_probe`."""
    return layer_probe(folder)[0]


def layer_unknown_still(folder: Path) -> bool:
    """Clear probe for :data:`CID_UNKNOWN`: does git still give no clean
    answer for ``folder``? The SAME probe the writer gates on."""
    return layer_state(folder) == UNKNOWN


def tracked_still(folder: Path) -> Optional[bool]:
    """Clear probe for :data:`CID_TRACKED`: ``True`` while git tracks the
    file, ``False`` once it does not, ``None`` when git cannot say."""
    state = layer_state(folder)
    return None if state == UNKNOWN else state == TRACKED


def sync_tracked_entry(folder: Path, state: str, *, project_id: Optional[str] = None,
                       git_error: str = "") -> None:
    """Emit :data:`CID_TRACKED` for a tracked file; clear it once the file is
    usable again (paired resolution). ``unknown`` leaves that entry as it is
    and records :data:`CID_UNKNOWN` instead (``git_error`` from
    :func:`layer_probe`), cleared once git answers. Best-effort: the ledger
    never fails a write."""
    if state == UNKNOWN:
        _log.warning(
            "settings.local.json: git gave no clean answer for %s (%s) — this "
            "machine's paths stay in .claude/settings.json", folder, git_error or "unknown error")
        _emit_unknown_entry(Path(folder), git_error, project_id)
        return
    try:
        from vco_lib.deferral_emit import emit, resolve_conditions
        from vco_lib.deferral_report import DeferralEntry, DeferralReport

        if DeferralReport.read(folder).has_condition(CID_UNKNOWN):
            resolve_conditions(folder, [CID_UNKNOWN])
        if state == USABLE:
            if DeferralReport.read(folder).has_condition(CID_TRACKED):
                resolve_conditions(folder, [CID_TRACKED])
            return
        if state != TRACKED:
            return
        retry = _refresh_line(project_id)
        emit(folder, DeferralEntry(
            condition_id=CID_TRACKED,
            title="`.claude/settings.local.json` is committed to git — VCO keeps this machine's paths out of it",
            detected=(
                "Git tracks `.claude/settings.local.json` in this project. VCO writes this "
                "machine's absolute paths (your home folder, so your username), launcher ids "
                "and local service ports into that file's `env` — Claude Code's per-machine "
                "settings, which are never meant to be committed. Because the file is "
                "committed, VCO writes no new values there: they stay in `.claude/settings.json`, "
                "as before v0.2.104, so the project keeps working."
            ),
            why_deferred=(
                "Taking a file out of git is your decision: `git rm --cached` makes the next "
                "commit delete it from the repository, and teammates who pull that commit "
                "lose their copy of it (back it up or tell them first). VCO already lists the "
                "file in `.git/info/exclude`, which keeps it out once it is untracked."
            ),
            command_to_apply=(
                "git rm --cached .claude/settings.local.json\n"
                "git commit -m \"Stop tracking the per-machine Claude Code settings\"\n"
                + retry
                + "# This entry clears itself once git no longer tracks the file."
            ),
            severity="warning",
        ), keep_first_detected=True)
    except Exception:  # noqa: BLE001 — the ledger is best-effort
        pass


def _refresh_line(project_id: Optional[str]) -> str:
    return (
        f"python -m vco_lib.config_projection apply --project-id {project_id}\n"
        if project_id else
        "# then refresh the project's env (launcher: Settings → Re-render this project's env)\n"
    )


def _emit_unknown_entry(folder: Path, git_error: str, project_id: Optional[str]) -> None:
    """Record :data:`CID_UNKNOWN` (environmental, review R18 LOW-3)."""
    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        reason = git_error or "git returned an unexpected error"
        emit(folder, DeferralEntry(
            condition_id=CID_UNKNOWN,
            title="Git gives no answer for this project, so its machine paths stay in `.claude/settings.json`",
            detected=(
                "VCO asks git whether `.claude/settings.local.json` is committed before it writes "
                "this machine's absolute paths, launcher ids and local service ports there. Git "
                f"did not answer cleanly: `{reason}`. VCO writes no machine value into a file it "
                "cannot prove stays private, so those values remain in the committable "
                "`.claude/settings.json`, as before v0.2.104 (the project keeps working)."
            ),
            why_deferred=(
                "The fix is a change to your git setup. The usual cause is git's `safe.directory` "
                "check (\"detected dubious ownership\": the folder is owned by another user, on a "
                "mounted drive or under WSL); a damaged `.git` is the other. Trusting a folder is "
                "your decision, so VCO does not do it."
            ),
            command_to_apply=(
                f"git -C \"{folder}\" status   # shows the same error\n"
                f"git config --global --add safe.directory \"{folder}\"   # only if you trust the folder\n"
                + _refresh_line(project_id)
                + "# This entry clears itself once git answers."
            ),
            severity="warning",
            dismiss_fields={"git_error": reason},
        ), keep_first_detected=True)
    except Exception:  # noqa: BLE001 — the ledger is best-effort
        pass


# ─── B6: whose value is it? ─────────────────────────────────────────────


def _local_env(folder: Path) -> dict[str, Any]:
    from vco_lib.jsonc_edit import load_object

    try:
        loaded = load_object(Path(folder) / LOCAL_REL)
    except Exception:  # noqa: BLE001
        return {}
    env = loaded[0].get("env") if loaded is not None else None
    return dict(env) if isinstance(env, dict) else {}


def _written(folder: Path) -> dict[str, str]:
    try:
        data = json.loads((Path(folder) / WRITTEN_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    values = data.get("values") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}


def _record_written(folder: Path, set_values: Mapping[str, str], dropped: Iterable[str]) -> None:
    from vco_lib.atomic import atomic_write_json

    values = _written(folder)
    values.update(set_values)
    for key in dropped:
        values.pop(key, None)
    try:
        path = Path(folder) / WRITTEN_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"schema": 1, "values": dict(sorted(values.items()))})
    except OSError:
        pass  # evidence is best-effort; .claude/env still carries VCO's values


def _vco_evidence(folder: Path, keys: Iterable[str]) -> dict[str, set[str]]:
    """Every value VCO is known to have written for each key: the record
    file, VCO's ``.claude/env`` managed block, and ``settings.json`` (VCO
    overwrote canonical keys there on every apply before L5)."""
    from vco_lib import config_projection as cp
    from vco_lib.claude_settings_env import SETTINGS_ENV_FILES

    written = _written(folder)
    try:
        from vco_lib.jsonc_edit import load_object

        loaded = load_object(Path(folder) / SETTINGS_ENV_FILES[0])
        settings_env = loaded[0].get("env") if loaded is not None else None
    except Exception:  # noqa: BLE001
        settings_env = None
    settings_env = settings_env if isinstance(settings_env, dict) else {}
    env_file = Path(folder) / ".claude" / "env"
    out: dict[str, set[str]] = {}
    for key in keys:
        seen = {written[key]} if key in written else set()
        managed = cp._read_managed_env_canonical_value(env_file, key)
        if managed is not None:
            seen.add(managed)
        if isinstance(settings_env.get(key), str):
            seen.add(settings_env[key])
        out[key] = seen
    return out


def user_kept_keys(
    folder: Path, projected: Mapping[str, str], owned: Iterable[str],
) -> dict[str, str]:
    """``{key: value}`` of the ``owned`` keys in ``settings.local.json`` whose
    value is the USER's: a string that is neither ``projected[key]`` nor a
    value VCO is known to have written (:func:`_vco_evidence`). Read-only."""
    local = _local_env(folder)
    owned = [k for k in owned if isinstance(local.get(k), str)]
    if not owned:
        return {}
    evidence = _vco_evidence(folder, owned)
    return {
        k: local[k] for k in owned
        if local[k] != projected.get(k) and local[k] not in evidence[k]
    }


def user_kept_still(folder: Path) -> Optional[bool]:
    """Clear probe for :data:`CID_USER_KEPT`: does a machine key in
    ``settings.local.json`` still hold a value that is not VCO's? Compared
    with VCO's last projection (its ``.claude/env`` managed block)."""
    from vco_lib import config_projection as cp

    try:
        env_file = Path(folder) / ".claude" / "env"
        projected = {}
        for key in cp.MACHINE_LOCAL_ENV_KEYS:
            value = cp._read_managed_env_canonical_value(env_file, key)
            if value is not None:
                projected[key] = value
        return bool(user_kept_keys(folder, projected, cp.MACHINE_LOCAL_ENV_KEYS))
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None


def _sync_kept_entry(folder: Path, kept: Mapping[str, str]) -> None:
    try:
        from vco_lib.deferral_emit import emit, resolve_conditions
        from vco_lib.deferral_report import DeferralEntry, DeferralReport

        if not kept:
            if DeferralReport.read(folder).has_condition(CID_USER_KEPT):
                resolve_conditions(folder, [CID_USER_KEPT])
            return
        names = ", ".join(f"`{k}`" for k in sorted(kept))
        emit(folder, DeferralEntry(
            condition_id=CID_USER_KEPT,
            title="Your own values in `.claude/settings.local.json` were kept",
            detected=(
                f"`.claude/settings.local.json` sets {names} to a value VCO did not write. "
                "Claude Code gives that file precedence, so your value is the one the "
                "session, its hooks and its MCP servers use; VCO left it as it is and keeps "
                "its own value for these keys in `.claude/env` only."
            ),
            why_deferred="Nothing is pending — this records a value VCO deliberately left alone.",
            command_to_apply=(
                "# Nothing to do. To let VCO manage a key again, delete it from the `env`\n"
                "# block of .claude/settings.local.json; the next env refresh writes VCO's\n"
                "# value and this entry clears itself."
            ),
            severity="info",
        ))
    except Exception:  # noqa: BLE001 — the ledger is best-effort
        pass


# ─── the writer ─────────────────────────────────────────────────────────


def write_local_env(
    folder: Path,
    values: Mapping[str, str],
    owned: Iterable[str],
    *,
    refusals: list,
    project_id: Optional[str] = None,
) -> Optional[list[str]]:
    """Write ``values`` into ``settings.local.json`` ``env`` and remove every
    other ``owned`` key VCO wrote there; keep the user's values
    (:func:`user_kept_keys`). Through the ONE JSON env writer (deep merge,
    JSONC in place, refusal recorded). Returns the keys written, or ``None``
    when the file was refused. The caller has checked :func:`layer_state`.
    """
    from vco_lib import config_projection as cp

    folder = Path(folder)
    owned = list(dict.fromkeys(owned))
    kept = user_kept_keys(folder, values, owned)
    write_values = {k: v for k, v in values.items() if k not in kept}
    write_owned = [k for k in owned if k not in kept]
    path = folder / LOCAL_REL
    if not write_values and not path.exists():
        return []
    keys = cp._write_json_surface(
        folder, cp._SURFACE_CLAUDE_SETTINGS_LOCAL, refusals, project_id,
        lambda: cp._write_json_env_block(path, write_values, write_owned, env_key="env"),
    )
    if keys is None:
        return None
    _record_written(folder, write_values, [k for k in write_owned if k not in write_values])
    _sync_kept_entry(folder, kept)
    return keys


def move_machine_env_out_of_settings(
    folder: Path, expected: Mapping[str, str],
) -> list[str]:
    """Move the machine keys of ``settings.json`` ``env`` whose value EQUALS
    ``expected`` (what VCO writes there — the evidence rule) into
    ``settings.local.json``. For a writer that owns ``settings.json`` env
    without the projection (``install.py``'s root defaults). Nothing moves
    while the layer is not :data:`USABLE`. Returns the keys moved. Raises
    :class:`vco_lib.config_projection.SettingsWriteRefused` when a file
    cannot be edited. The local file is written first, so a refusal on
    ``settings.json`` leaves the moved keys in BOTH files with identical
    values (harmless: the local value wins in the merge, and the next run
    retries the strip); a refusal on the local file changes nothing.
    """
    from vco_lib import config_projection as cp
    from vco_lib.claude_settings_env import SETTINGS_ENV_FILES
    from vco_lib.jsonc_edit import load_object

    folder = Path(folder)
    machine = {k: v for k, v in expected.items() if is_machine_env_key(k)}
    if not machine:
        return []
    try:
        loaded = load_object(folder / SETTINGS_ENV_FILES[0])
    except Exception:  # noqa: BLE001
        return []
    env = loaded[0].get("env") if loaded is not None else None
    if not isinstance(env, dict):
        return []
    present = {k: v for k, v in machine.items() if env.get(k) == v}
    if not present:
        return []
    state, git_error = layer_probe(folder)
    sync_tracked_entry(folder, state, git_error=git_error)
    if state != USABLE:
        return []
    refusals: list = []
    if write_local_env(folder, present, list(present), refusals=refusals) is None:
        raise cp.SettingsWriteRefused(refusals)
    removed, _left = cp.strip_env_keys_holding(
        folder, cp._SURFACE_CLAUDE_SETTINGS, present, list(present),
    )
    return removed
