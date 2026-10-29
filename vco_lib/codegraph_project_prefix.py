# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE answer to "which code-graph collection prefix does project X use?"
(v0.2.105).

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

The hub's ``/config`` answer carries TWO code-graph fields:

* ``code_graph_project`` — the launcher SLUG (``orchestrator-root``), kept for
  the access-matrix joins since v0.2.23. It is NOT a class prefix: the
  analyzer sanitizes it to ``Orchestrator_root`` and writes a whole second,
  zombie ``Orchestrator_root_Code*`` class set beside the real one.
* ``code_graph_collection_prefix`` — ``project_codegraph_bindings.
  collection_prefix``, the real write target (``VibeCodedOrchestrator``).

Several Python paths turned a project into an analyzer ``--project`` value from
the slug or the registered NAME instead of the binding: the owed-work retries
(:mod:`vco_lib.deferral_retry`), the summary backfill
(:mod:`vco_lib.summary_health`), the update's resync spawn (``install.py``) and
the move's printed rebuild command (:mod:`vco_lib.project_move`). On a project
whose slug or name does not sanitize to its binding prefix (the orchestrator
root, or any renamed project) they re-walked the source into the zombie set, so
``codegraph_embed_resync_pending`` could never converge. They all resolve here
now.

Resolution order (:func:`resolve`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. the hub's ``code_graph_collection_prefix`` for the folder (the launcher's
   live answer);
2. ``CODE_GRAPH_PROJECT`` in the project's Claude Code settings ``env``
   (``config_projection`` pins the binding prefix there);
3. the binding row read straight from ``launcher.db``
   (:meth:`vco_lib.project_identity.ProjectIdentity.authoritative_codegraph_prefix`);
4. a NAME, only when no binding exists — ``PROJECT_NAME`` from the settings
   env, the registered name, or the folder basename (the analyzer's own
   ``--project`` default), with the collision and unreadable-registry refusals
   documented on :func:`_derived_name`. Consumers sanitize it exactly as the
   analyzer does (``sanitize_collection_prefix`` is idempotent on a prefix).

Tiers 1-3 are AUTHORITATIVE; a caller that must not act on a guess passes
``authoritative_only=True``.

Third-party auto-heal (:func:`heal_slug_prefix_divergence`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run by every bundle update (``project_init.install_project_bundle`` — the one
path the launcher runs for every project and, since v0.2.85, for the root):

* (a) a pending ``codegraph_embed_resync_pending`` entry whose command targets
  a prefix other than the binding is rewritten to target the binding, so the
  human-facing command stops recreating the zombie set (the retry handler
  re-resolves through :func:`resolve_codegraph_prefix` either way);
* (b) zombie ``<sanitized slug or name>_Code*`` classes that exist in Weaviate
  and belong to NO binding are reported in the NEW
  ``codegraph_slug_prefix_zombie_classes`` deferral with their row counts and
  a consented, re-validated drop command. NOTHING is dropped automatically
  (CLAUDE.md rule 1). The entry clears on the first update whose read of the
  live schema finds none.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCE_HUB",
    "SOURCE_SETTINGS",
    "SOURCE_BINDING",
    "SOURCE_DERIVED",
    "ZOMBIE_CONDITION_ID",
    "PrefixResolution",
    "resolve",
    "resolve_codegraph_prefix",
    "resolve_for_project_id",
    "zombie_prefix_candidates",
    "heal_resync_entry",
    "detect_zombie_classes",
    "heal_slug_prefix_divergence",
]

SOURCE_HUB = "hub"
SOURCE_SETTINGS = "settings"
SOURCE_BINDING = "binding"
SOURCE_DERIVED = "derived"
_AUTHORITATIVE = frozenset({SOURCE_HUB, SOURCE_SETTINGS, SOURCE_BINDING})

#: The condition this module emits (declared in ``deferral_conditions.toml``).
ZOMBIE_CONDITION_ID = "codegraph_slug_prefix_zombie_classes"
#: The resync condition whose stale command (a) rewrites.
_RESYNC_CONDITION_ID = "codegraph_embed_resync_pending"

#: The five code-graph entity suffixes — must match
#: ``vco_lib.project_init._CODEGRAPH_SUFFIXES``.
_CODE_SUFFIXES: tuple[str, ...] = (
    "_CodeFunction", "_CodeModule", "_CodeClass", "_CodeAPI", "_CodeInteraction",
)


@dataclass(frozen=True)
class PrefixResolution:
    """``prefix`` ("" when nothing answered) and the tier that answered."""

    prefix: str
    source: str = ""

    @property
    def authoritative(self) -> bool:
        return bool(self.prefix) and self.source in _AUTHORITATIVE


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _hub_prefix(folder: Path) -> str:
    """Tier 1: the hub's ``code_graph_collection_prefix``; "" on any failure
    (hub down, folder not registered, resolver disabled by the test gate)."""
    try:
        from vco_lib.project_config import resolve as _hub_resolve

        return (_hub_resolve(folder).code_graph_collection_prefix or "").strip()
    except Exception:  # noqa: BLE001 — the hub is one tier of four
        return ""


def _settings_env(folder: Path) -> dict:
    """The project's merged settings ``env`` through the ONE reader; ``{}``
    when it cannot be read."""
    try:
        from vco_lib import knowledge_residue as _kres

        env = _kres.project_settings_env(Path(folder))
        return env if isinstance(env, dict) else {}
    except Exception:  # noqa: BLE001 — unreadable env ⇒ try the next tier
        return {}


def _env_str(env: dict, key: str) -> str:
    val = env.get(key)
    return val.strip() if isinstance(val, str) else ""


def _derived_name(folder: Path, env: dict, snapshot: Any, registered: Any) -> str:
    """Tier 4 — a NAME, used only when no binding answered.

    * ``PROJECT_NAME`` from the settings env, else the registered name;
    * an unregistered folder on a readable registry → the basename (the
      analyzer's own ``--project`` default) UNLESS another registered project
      answers to that name — that collision would re-index this source into
      that project's rows, so "" instead;
    * no ``launcher.db`` at all (free tier / CLI-only) → the basename;
    * a ``launcher.db`` that exists but could not be read → "": an unreadable
      registry cannot say whether a binding exists, so any name is a guess.
    """
    from vco_lib import project_identity as _pid

    if not snapshot.resolvable:
        from vco_lib.launcher_db_reader import _discover_db_path

        if _discover_db_path() is not None:
            return ""
        return _env_str(env, "PROJECT_NAME") or Path(folder).name.strip()
    pn = _env_str(env, "PROJECT_NAME")
    if pn:
        return pn
    if registered is not None:
        return (registered.name or "").strip()
    basename = Path(folder).name.strip()
    if not basename:
        return ""
    taken = {_pid.normalise_for_match(p.name or "")
             for p in snapshot.projects if (p.name or "").strip()}
    return "" if _pid.normalise_for_match(basename) in taken else basename


def resolve(folder: Path | str) -> PrefixResolution:
    """Resolve ``folder``'s code-graph collection prefix (module docstring
    for the order). Never raises; ``PrefixResolution("")`` when nothing
    answers — the caller then does nothing rather than guess.
    """
    folder = Path(folder)
    hub = _hub_prefix(folder)
    if hub:
        return PrefixResolution(hub, SOURCE_HUB)
    env = _settings_env(folder)
    cg = _env_str(env, "CODE_GRAPH_PROJECT")
    if cg:
        return PrefixResolution(cg, SOURCE_SETTINGS)
    try:
        from vco_lib import project_identity as _pid

        snap = _pid.resolve_snapshot()
        registered = snap.identity_for_folder(folder) if snap.resolvable else None
        if registered is not None:
            bound = registered.authoritative_codegraph_prefix()
            if bound:
                return PrefixResolution(bound, SOURCE_BINDING)
        name = _derived_name(folder, env, snap, registered)
    except Exception:  # noqa: BLE001 — could not look is never a verdict
        return PrefixResolution("")
    return PrefixResolution(name, SOURCE_DERIVED if name else "")


def resolve_codegraph_prefix(folder: Path | str, *,
                             authoritative_only: bool = False) -> str:
    """The prefix (or "" when unresolved). With ``authoritative_only`` a
    tier-4 name is refused too — for callers that have their own default."""
    res = resolve(folder)
    if authoritative_only and not res.authoritative:
        return ""
    return res.prefix


def resolve_for_project_id(project_id: str, *, fallback: str = "",
                           db_path: Optional[Path] = None) -> str:
    """The binding prefix of a ``projects.id`` (or name/slug), else
    ``fallback``. For callers that hold a project row, not a folder."""
    try:
        from vco_lib.launcher_db_reader import get_codegraph_prefix

        return get_codegraph_prefix(project_id, db_path=db_path) or fallback
    except Exception:  # noqa: BLE001
        return fallback


# ---------------------------------------------------------------------------
# Third-party auto-heal
# ---------------------------------------------------------------------------


def _sanitize(name: str) -> str:
    from vco_lib.codegraph_naming import sanitize_collection_prefix

    return sanitize_collection_prefix(name or "").strip()


def _norm(name: str) -> str:
    from vco_lib.project_identity import normalise_for_match

    return normalise_for_match(name or "")


def zombie_prefix_candidates(identity: Any, bound_prefix: str) -> list[str]:
    """The prefixes a slug/name-derived walk would have written for
    ``identity``: the sanitized slug and registered name, minus anything that
    normalises to ``bound_prefix``. Order-stable, deduplicated."""
    out: list[str] = []
    seen = {_norm(bound_prefix)}
    for raw in (getattr(identity, "slug", "") or "", getattr(identity, "name", "") or ""):
        cand = _sanitize(raw) if raw.strip() else ""
        key = _norm(cand)
        if cand and key and key not in seen:
            seen.add(key)
            out.append(cand)
    return out


def _command_project(command: str) -> str:
    """The ``--project`` value of a rendered command, or ""."""
    try:
        tokens = shlex.split(command or "", comments=True)
    except ValueError:
        return ""
    for i, tok in enumerate(tokens[:-1]):
        if tok == "--project":
            return tokens[i + 1]
    return ""


def heal_resync_entry(folder: Path, prefix: str, *, log: Any = None) -> bool:
    """(a) Rewrite a pending ``codegraph_embed_resync_pending`` entry whose
    command targets another prefix so it targets ``prefix``. Returns True when
    the ledger was rewritten. Leave-alone when the entry is absent, already
    targets ``prefix`` (case-insensitively, as Weaviate class names compare),
    or names no ``--project`` at all. Soft-fail."""
    if not prefix:
        return False
    try:
        from vco_lib.deferral_emit import WriteGate, locked_report

        gate = WriteGate()
        gate.write = False
        old = ""
        with locked_report(folder, gate=gate) as report:
            entry = next((e for e in report.entries
                          if e.condition_id == _RESYNC_CONDITION_ID), None)
            if entry is None:
                return False
            old = _command_project(entry.command_to_apply)
            if not old or _sanitize(old).lower() == prefix.lower():
                return False
            new_arg = f"--project {shlex.quote(prefix)}"
            command = entry.command_to_apply
            for form in (f"--project {shlex.quote(old)}", f"--project '{old}'",
                         f"--project {old}"):
                if form in command:
                    command = command.replace(form, new_arg, 1)
                    break
            detected = (
                f"{entry.detected} [Corrected by a later update: this entry "
                f"targeted the prefix '{_sanitize(old)}' derived from the "
                f"project's launcher name, not its bound code-graph prefix "
                f"'{prefix}'. The counts above describe that other class set; "
                f"the retry now re-measures the bound one.]"
            )
            report.add_entry(replace(entry, command_to_apply=command, detected=detected))
            gate.write = True
        _log(log, "warn",
             f"codegraph resync entry re-targeted from '{old}' to the bound prefix '{prefix}'")
        return True
    except Exception as exc:  # noqa: BLE001 — a heal never breaks an update
        _log(log, "warn", f"codegraph resync entry heal failed: {exc}")
        return False


def detect_zombie_classes(
    candidates: Sequence[str],
    *,
    keep_set: set[str],
    list_classes: Callable[[], list[str]],
    count_objects: Callable[[str], Optional[int]],
) -> list[dict]:
    """(b) Live ``<candidate>_Code*`` classes that NO binding owns.

    ``keep_set`` holds the normalised binding + extra-path-owner prefixes; a
    candidate in it is somebody's live graph and never reported. Raises
    whatever ``list_classes`` raises (an unreadable schema is not "none").
    Each hit: ``{"class_name", "prefix", "object_count"}`` (count ``None`` =
    could not be read).
    """
    wanted: dict[str, tuple[str, str]] = {}
    for cand in candidates:
        if not cand or _norm(cand) in keep_set:
            continue
        for sfx in _CODE_SUFFIXES:
            wanted[f"{cand}{sfx}".lower()] = (cand, sfx)
    if not wanted:
        return []
    hits: list[dict] = []
    for cls in sorted(list_classes()):
        match = wanted.get(cls.lower())
        if match is None:
            continue
        hits.append({"class_name": cls, "prefix": match[0],
                     "object_count": count_objects(cls)})
    return hits


def _zombie_entry(bound_prefix: str, slug: str, hits: list[dict], weaviate_url: str):
    from vco_lib.deferral_report import DeferralEntry

    listed = "; ".join(
        f"{h['class_name']} ({h['object_count'] if isinstance(h['object_count'], int) else 'count unknown'} rows)"
        for h in hits
    )
    prefixes = sorted({h["prefix"] for h in hits})
    lines = [
        "# For your Claude assistant: these classes duplicate this project's code",
        "# graph under a prefix derived from its launcher name; nothing reads them.",
        "# Show the user the list and counts, and run the drop ONLY when they agree.",
        "# Never drop a class whose prefix a project is bound to (the command",
        "# re-checks that itself and refuses).",
    ]
    for h in hits:
        cnt = h["object_count"]
        lines.append(f"#   - {h['class_name']}  "
                     f"({cnt if isinstance(cnt, int) else 'count unknown'} rows)")
    for p in prefixes:
        lines.append(
            "python -m vco_lib.project_init drop-orphan-code-collections "
            f"--weaviate-url {shlex.quote(weaviate_url)} --prefix {shlex.quote(p)} --confirm"
        )
    return DeferralEntry(
        condition_id=ZOMBIE_CONDITION_ID,
        title="Duplicate code-graph classes under the project's launcher name",
        detected=(
            f"This project's code graph is bound to the prefix '{bound_prefix}', "
            f"but Weaviate also holds code-graph classes under the prefix VCO "
            f"derived from its launcher name '{slug}': {listed}. Earlier VCO "
            "versions re-walked the project into them; no search reads them and "
            "they use memory and disk."
        ),
        why_deferred=(
            "Dropping a Weaviate class is irreversible and VCO never deletes user "
            "data on its own. The command below drops only classes with the named "
            "prefix that no project is bound to, after re-reading the live bindings "
            "and the live schema; it refuses when it cannot read either. This entry "
            "clears on the next update that finds the classes gone."
        ),
        command_to_apply="\n".join(lines),
        severity="warning",
    )


def heal_slug_prefix_divergence(
    folder: Path | str,
    *,
    log: Any = None,
    weaviate_url: Optional[str] = None,
    keep_set: Optional[tuple[set[str], bool]] = None,
    list_classes: Optional[Callable[[], list[str]]] = None,
    count_objects: Optional[Callable[[str], Optional[int]]] = None,
) -> dict:
    """Run (a) and (b) for ONE registered project (the bundle update's
    folder). Never raises; returns what it did for the install log.

    Acts only when the folder is a registered project with an AUTHORITATIVE
    binding prefix that differs from its sanitized slug or name — every other
    project has nothing a slug-derived walk could have diverged into.
    The ``keep_set`` / ``list_classes`` / ``count_objects``
    seams exist for tests; production passes none.
    """
    folder = Path(folder)
    out: dict = {"prefix": "", "resync_retargeted": False, "zombies": [],
                 "emitted": False, "resolved": False, "skipped": ""}
    try:
        from vco_lib import project_identity as _pid

        snap = _pid.resolve_snapshot()
        ident = snap.identity_for_folder(folder) if snap.resolvable else None
        bound = ident.authoritative_codegraph_prefix() if ident is not None else None
        if ident is None or not bound:
            out["skipped"] = "no registered binding for this folder"
            return out
        out["prefix"] = bound
        candidates = zombie_prefix_candidates(ident, bound)
        if not candidates:
            out["skipped"] = "slug and name already match the binding"
            return out
        out["resync_retargeted"] = heal_resync_entry(folder, bound, log=log)

        if keep_set is None:
            from vco_lib.launcher_db_reader import codegraph_binding_keep_set

            prefixes, resolvable = codegraph_binding_keep_set()
            keep_set = ({_norm(p) for p in prefixes if p}, resolvable)
        normed_keep, keep_ok = keep_set
        if not keep_ok:
            out["skipped"] = "binding keep-set unreadable — zombie check skipped"
            return out
        from vco_lib import weaviate_helpers as _wh

        url = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
        lister = list_classes or (lambda: _wh.probe_class_listing(url).require())
        counter = count_objects or (lambda cls: _wh.http_count_objects(cls, url))
        try:
            hits = detect_zombie_classes(candidates, keep_set=normed_keep,
                                         list_classes=lister, count_objects=counter)
        except Exception as exc:  # noqa: BLE001 — unreadable schema ≠ none
            out["skipped"] = f"live schema unreadable ({exc}) — entry left as is"
            return out
        out["zombies"] = hits
        from vco_lib import deferral_emit as _de

        if hits:
            entry = _zombie_entry(bound, ident.slug or ident.name, hits, url)
            out["emitted"] = bool(_de.emit(folder, entry, log=log, keep_first_detected=True))
            _log(log, "warn", f"{len(hits)} zombie code-graph class(es) beside "
                              f"'{bound}' — consented drop recorded in UPDATE_DEFERRED.md")
        else:
            out["resolved"] = _de.resolve_conditions(folder, (ZOMBIE_CONDITION_ID,), log=log) > 0
        return out
    except Exception as exc:  # noqa: BLE001 — a heal never breaks an update
        out["skipped"] = f"heal raised: {exc}"
        _log(log, "warn", f"codegraph slug-prefix heal raised: {exc}")
        return out


def _log(log: Any, level: str, msg: str) -> None:
    if log is None:
        getattr(logger, "warning" if level == "warn" else "info")(msg)
        return
    try:
        log("4.bundle.codegraph_prefix_heal", level, msg)
    except Exception:  # noqa: BLE001 — logging never gates
        logger.debug("codegraph prefix heal: log failed: %s", msg)
