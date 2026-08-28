# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Loader + typed accessors for ``deferral_conditions.toml`` — the deferral
lifecycle contract (v0.2.91 WP-B).

Python side of a tier-(B) shared-config loader (see CLAUDE.md "Share, don't
mirror, cross-language logic"). The Rust side lives at
``launcher/src-tauri/vct-launcher-core/src/deferral_registry.rs``; both parse
the SAME ``vco_lib/deferral_conditions.toml`` with the SAME lookup semantics,
and cross-language parity tests
(``tests/test_deferral_registry_parity_v0291.py`` +
``launcher/src-tauri/tests/deferral_registry_parity.rs``) keep them in
lockstep. This is the exact triangulation shape ``mcp_scan_rules.toml`` uses;
the B-leg is justified because the table is consumed on hot boot paths in both
languages.

What the registry replaces
--------------------------
* ``install._INSTALL_OWNED_CONDITION_IDS`` / ``_INSTALL_OWNED_CONDITION_PREFIXES``
  become DERIVED data (:func:`install_owned_ids` / :func:`install_owned_prefixes`)
  behind the accessors install.py already used.
* Per-condition lifecycle knowledge that previously existed only in scattered
  comments — now declared, and enforced by a completeness test that source-scans
  every emit site.

Failure mode
------------
A missing/unreadable/incompatible table is FATAL — ``RuntimeError`` naming the
path. ``vco_lib`` ships with every healthy install, so an unreadable table means
a BROKEN install; a silent hard-coded fallback would re-introduce the very
two-language drift this file eliminates.

Lookup semantics
----------------
:func:`condition` resolves a concrete ``condition_id`` in this order:

1. an EXACT table key;
2. ``match = "glob"`` patterns, tried by DESCENDING literal length (so
   ``stale_unit_retired_*_backup_failed`` beats ``stale_unit_retired_*``);
3. ``None`` — unregistered. Every reader treats unregistered as
   ``action_required`` (conservative): an unclassified condition must never be
   quietly demoted into a collapsed "records" fold.
"""
from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

#: The .toml sits next to this loader inside the ``vco_lib`` package so it
#: ships in the Python wheel automatically (mirrors ``mcp_scan_rules.toml``).
#: ``.resolve()`` so symlinks / relative-CWD invocations still land on it.
_DEFAULT_TABLE_PATH: Path = Path(__file__).resolve().parent / "deferral_conditions.toml"

#: The format version this loader knows how to read. A schema extension bumps
#: the .toml AND this constant AND the Rust mirror in one commit.
_SUPPORTED_FORMAT_VERSION = 1

#: The four disposition tiers. Order is worst→best for display grouping.
CLASSES: tuple[str, ...] = (
    "action_required",
    "auto_retryable",
    "environmental",
    "informational_record",
)

#: The disposition applied to any condition_id absent from the registry.
#: Conservative by design — see the module docstring.
DEFAULT_CLASS = "action_required"

#: Sentinel ``clear_probe`` values (anything else must be ``probe:py:<name>``
#: or ``probe:rs:<name>``).
CLEAR_SENTINELS: tuple[str, ...] = (
    "owned-drop-when-absent",
    "bundle-reconciled",
    "paired-resolution",
    "manual-dismiss",
)

#: Recognised ``emit_surfaces`` values. Unknown values are a schema error —
#: a typo'd surface would silently disable the behaviour it names.
EMIT_SURFACES: tuple[str, ...] = (
    "ledger",
    "auto_resolutions_jsonl",
    "gui_banner",
    "gui_badge",
    "gui_modal",
)

#: Recognised ``status`` values.
STATUSES: tuple[str, ...] = ("active", "retired")

#: Prefix for the optional ``retry_action`` field (v0.2.91 WP-H). Only
#: ``auto_retryable`` rows may carry one, and its handler must exist in
#: ``vco_lib.deferral_retry.HANDLERS`` (pinned by the retry tests).
#:
#: Python-side only BY DESIGN: retries are dispatched from Python, so the Rust
#: mirror tolerates-and-ignores this key exactly as it already does ``notes``
#: (``RawCondition`` has no ``deny_unknown_fields``; a parity test pins that).
#: Adding a Rust field would mean a second retry engine — the opposite of the
#: "registry data + ONE dispatcher" shape WP-H exists to enforce.
RETRY_PREFIX = "retry:py:"


@dataclass(frozen=True)
class ConditionSpec:
    """One registry row, resolved."""

    #: The table key (an exact condition_id, or an fnmatch pattern).
    pattern: str
    #: ``"exact"`` or ``"glob"``.
    match: str
    #: One of :data:`CLASSES`.
    condition_class: str
    #: Module / file that emits the condition.
    owner: str
    #: A :data:`CLEAR_SENTINELS` value or ``probe:{py,rs}:<name>``.
    clear_probe: str
    #: Subset of :data:`EMIT_SURFACES`.
    emit_surfaces: tuple[str, ...] = ()
    #: Ordered stable field names forming the dismissal identity (may be empty).
    dismiss_key: tuple[str, ...] = ()
    #: ``"active"`` or ``"retired"``.
    status: str = "active"
    #: ``retry:py:<handler>`` for an ``auto_retryable`` row VCO can re-attempt
    #: on its own, else ``""``.
    retry_action: str = ""
    #: Free-form rationale.
    notes: str = ""

    @property
    def is_owned_by_install(self) -> bool:
        return self.clear_probe == "owned-drop-when-absent"

    @property
    def probe_name(self) -> Optional[str]:
        """``<name>`` for a ``probe:py:<name>`` clear_probe, else ``None``.

        Rust-owned probes (``probe:rs:``) deliberately return ``None`` — the
        Python re-probe pass must not pretend it can evaluate them.
        """
        if self.clear_probe.startswith("probe:py:"):
            return self.clear_probe[len("probe:py:"):]
        return None

    @property
    def rust_probe_name(self) -> Optional[str]:
        if self.clear_probe.startswith("probe:rs:"):
            return self.clear_probe[len("probe:rs:"):]
        return None

    @property
    def retry_handler(self) -> Optional[str]:
        """``<handler>`` for a ``retry:py:<handler>`` row, else ``None``.

        ``None`` for every row that declares no ``retry_action`` — the
        dispatcher treats that as "not my business", which is what keeps the
        retry surface a declared list rather than "anything with a command".
        """
        if self.retry_action.startswith(RETRY_PREFIX):
            return self.retry_action[len(RETRY_PREFIX):]
        return None


@dataclass(frozen=True)
class _Registry:
    exact: dict[str, ConditionSpec] = field(default_factory=dict)
    #: Sorted by DESCENDING literal length so the most specific glob wins.
    globs: tuple[ConditionSpec, ...] = ()


def table_path() -> Path:
    """Absolute path to the on-disk table. Exposed for diagnostics + tests."""
    return _DEFAULT_TABLE_PATH


def _literal_length(pattern: str) -> int:
    """Number of non-wildcard characters — the glob specificity ranking."""
    return len(pattern.replace("*", ""))


def _spec_from_raw(pattern: str, raw: Any) -> ConditionSpec:
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"deferral_conditions.toml: [conditions.{pattern!r}] must be a table"
        )
    match = str(raw.get("match", "exact"))
    if match not in ("exact", "glob"):
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} has match={match!r}; "
            f"expected 'exact' or 'glob'"
        )
    cls = str(raw.get("class", ""))
    if cls not in CLASSES:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} has class={cls!r}; "
            f"expected one of {CLASSES}"
        )
    clear_probe = str(raw.get("clear_probe", ""))
    if clear_probe not in CLEAR_SENTINELS and not (
        clear_probe.startswith("probe:py:") or clear_probe.startswith("probe:rs:")
    ):
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} has clear_probe="
            f"{clear_probe!r}; expected one of {CLEAR_SENTINELS} or "
            f"'probe:py:<name>' / 'probe:rs:<name>'"
        )
    owner = str(raw.get("owner", ""))
    if not owner:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} is missing `owner`"
        )
    surfaces = raw.get("emit_surfaces", [])
    if not isinstance(surfaces, list) or not surfaces:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} needs a non-empty "
            f"`emit_surfaces` list"
        )
    for s in surfaces:
        if s not in EMIT_SURFACES:
            raise RuntimeError(
                f"deferral_conditions.toml: {pattern!r} declares unknown "
                f"emit surface {s!r}; expected values from {EMIT_SURFACES}"
            )
    dismiss = raw.get("dismiss_key", [])
    if not isinstance(dismiss, list) or any(
        not isinstance(k, str) or not k for k in dismiss
    ):
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} has a malformed "
            f"`dismiss_key` (expected a list of non-empty field names)"
        )
    status = str(raw.get("status", "active"))
    if status not in STATUSES:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} has status={status!r}; "
            f"expected one of {STATUSES}"
        )
    retry_action = str(raw.get("retry_action", ""))
    if retry_action:
        if not retry_action.startswith(RETRY_PREFIX):
            raise RuntimeError(
                f"deferral_conditions.toml: {pattern!r} has retry_action="
                f"{retry_action!r}; expected '{RETRY_PREFIX}<handler>'"
            )
        if cls != "auto_retryable":
            raise RuntimeError(
                f"deferral_conditions.toml: {pattern!r} declares a "
                f"retry_action but class={cls!r}. Only auto_retryable rows "
                f"may be retried — the class IS the consent record."
            )
    if match == "exact" and "*" in pattern:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} contains '*' but declares "
            f"match='exact' — set match='glob'"
        )
    if match == "glob" and "*" not in pattern:
        raise RuntimeError(
            f"deferral_conditions.toml: {pattern!r} declares match='glob' but "
            f"has no '*' wildcard"
        )
    return ConditionSpec(
        pattern=pattern,
        match=match,
        condition_class=cls,
        owner=owner,
        clear_probe=clear_probe,
        emit_surfaces=tuple(str(s) for s in surfaces),
        dismiss_key=tuple(str(k) for k in dismiss),
        status=status,
        retry_action=retry_action,
        notes=str(raw.get("notes", "")),
    )


def load_registry(path: Optional[Path] = None) -> _Registry:
    """Parse ``deferral_conditions.toml`` into the resolved registry.

    Args:
        path: Optional override (tests pass a tempfile). Production lets it
            default to the ``vco_lib/`` sibling.

    Raises:
        RuntimeError: table missing/unreadable, ``format_version`` mismatch, or
            any row failing schema validation.
        tomllib.TOMLDecodeError: malformed TOML, propagated unwrapped so the
            caller sees the exact stdlib parser error.
    """
    target = path if path is not None else _DEFAULT_TABLE_PATH
    try:
        raw_bytes = target.read_bytes()
    except OSError as e:
        raise RuntimeError(
            f"Could not read the deferral-conditions table at {target}: {e}. "
            f"This file is the cross-language source of truth for every "
            f"deferral condition's disposition and lifecycle; it ships with "
            f"every VCO install, so a missing copy means a BROKEN install. "
            f"Re-fetch from https://github.com/hotak92/vibecoded-orchestrator."
        ) from e

    parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    version = parsed.get("format_version")
    if version != _SUPPORTED_FORMAT_VERSION:
        raise RuntimeError(
            f"Deferral-conditions table at {target} has format_version "
            f"{version!r}, but this loader supports "
            f"{_SUPPORTED_FORMAT_VERSION}. Coordinate the schema bump across "
            f"the Rust mirror (deferral_registry.rs) and the parity tests."
        )
    conditions = parsed.get("conditions")
    if not isinstance(conditions, dict) or not conditions:
        raise RuntimeError(
            f"Deferral-conditions table at {target} has no [conditions] rows."
        )

    exact: dict[str, ConditionSpec] = {}
    globs: list[ConditionSpec] = []
    for pattern, raw in conditions.items():
        spec = _spec_from_raw(pattern, raw)
        if spec.match == "glob":
            globs.append(spec)
        else:
            exact[pattern] = spec
    globs.sort(key=lambda s: (-_literal_length(s.pattern), s.pattern))
    return _Registry(exact=exact, globs=tuple(globs))


@lru_cache(maxsize=1)
def _cached() -> _Registry:
    """Load-once cache for the default-path table (the hot path)."""
    return load_registry()


def _registry(path: Optional[Path]) -> _Registry:
    return load_registry(path) if path is not None else _cached()


# ── Typed accessors — callers use THESE, never the raw dict ────────────────


def condition(
    condition_id: str, *, path: Optional[Path] = None
) -> Optional[ConditionSpec]:
    """Resolve one concrete ``condition_id``: exact → longest glob → ``None``."""
    reg = _registry(path)
    hit = reg.exact.get(condition_id)
    if hit is not None:
        return hit
    for spec in reg.globs:
        if fnmatch.fnmatchcase(condition_id, spec.pattern):
            return spec
    return None


def disposition_for(condition_id: str, *, path: Optional[Path] = None) -> str:
    """The disposition tier for ``condition_id``.

    Unregistered ⇒ :data:`DEFAULT_CLASS` (``action_required``). Conservative on
    purpose: an unclassified condition must surface as work, never hide.
    """
    spec = condition(condition_id, path=path)
    return spec.condition_class if spec is not None else DEFAULT_CLASS


def clear_probe_for(condition_id: str, *, path: Optional[Path] = None) -> str:
    """The declared clear mechanism, or ``"manual-dismiss"`` when unregistered."""
    spec = condition(condition_id, path=path)
    return spec.clear_probe if spec is not None else "manual-dismiss"


def emit_surfaces_for(
    condition_id: str, *, path: Optional[Path] = None
) -> tuple[str, ...]:
    """Declared surfaces for ``condition_id`` (``("ledger",)`` when unregistered)."""
    spec = condition(condition_id, path=path)
    return spec.emit_surfaces if spec is not None else ("ledger",)


def dismiss_key_fields(
    condition_id: str, *, path: Optional[Path] = None
) -> tuple[str, ...]:
    """Ordered dismissal-identity field names; empty ⇒ manual dismissal."""
    spec = condition(condition_id, path=path)
    return spec.dismiss_key if spec is not None else ()


def retry_handler_for(
    condition_id: str, *, path: Optional[Path] = None
) -> Optional[str]:
    """The declared retry handler name, or ``None``.

    ``None`` covers unregistered ids, non-``auto_retryable`` rows, and rows
    with no ``retry_action`` — every one of which means the retry dispatcher
    must leave the condition alone.
    """
    spec = condition(condition_id, path=path)
    return spec.retry_handler if spec is not None else None


def is_action_required(condition_id: str, *, path: Optional[Path] = None) -> bool:
    """True when the condition asks a human to DO something."""
    return disposition_for(condition_id, path=path) == "action_required"


def install_owned_ids(*, path: Optional[Path] = None) -> frozenset[str]:
    """EXACT condition ids install.py owns (drop-when-absent, family A).

    Replaces the hand-maintained ``install._INSTALL_OWNED_CONDITION_IDS``.
    """
    reg = _registry(path)
    return frozenset(
        cid for cid, spec in reg.exact.items() if spec.is_owned_by_install
    )


def install_owned_prefixes(*, path: Optional[Path] = None) -> tuple[str, ...]:
    """Owned dynamically-suffixed PREFIX families, as literal prefixes.

    Only globs of the shape ``literal*`` (one trailing wildcard) map to a
    prefix. A glob with an interior wildcard — e.g.
    ``stale_unit_retired_*_backup_failed`` — is already covered for OWNERSHIP
    by its parent prefix, so it contributes nothing here (and must not, since
    ``condition_is_owned`` does a ``startswith`` test).

    Sorted for a stable equality pin in the parity tests.
    """
    reg = _registry(path)
    out: set[str] = set()
    for spec in reg.globs:
        if not spec.is_owned_by_install:
            continue
        if spec.pattern.count("*") == 1 and spec.pattern.endswith("*"):
            out.add(spec.pattern[:-1])
    return tuple(sorted(out))


def all_specs(*, path: Optional[Path] = None) -> tuple[ConditionSpec, ...]:
    """Every registered row (exact first, then globs), for tests + tooling."""
    reg = _registry(path)
    return tuple(reg.exact[k] for k in sorted(reg.exact)) + reg.globs


def registered_patterns(*, path: Optional[Path] = None) -> tuple[str, ...]:
    """Every table key, sorted — the completeness test's expected key set."""
    return tuple(sorted(s.pattern for s in all_specs(path=path)))


def matches_registered_pattern(
    condition_id: str, *, path: Optional[Path] = None
) -> bool:
    """True when ``condition_id`` resolves to a registered row."""
    return condition(condition_id, path=path) is not None


# NOTE (v0.2.91 dogfood fix): this is the REGISTRY-LEVEL partition — it answers
# "what tier does the registry assign these condition ids", and the
# cross-language parity tests use it for exactly that. It is NOT the partition
# for ENTRIES: it cannot see an entry's explicit `disposition`, so partitioning a
# ledger through it silently disagrees with the ledger, the GUI and the CLAUDE.md
# reminder. Entries go through `vco_lib.deferral_report.partition_entries`, and
# `tests/test_v0291_dogfood_deferral_selfclear.py` source-scans for regressions.
def split_by_disposition(
    condition_ids, *, path: Optional[Path] = None
) -> tuple[list[str], list[str]]:
    """Partition ids into ``(actionable, informational)``.

    "Actionable" = ``action_required`` OR ``auto_retryable`` — a retryable
    condition is still owed work, it is just work VCO can do itself.
    "Informational" = ``environmental`` + ``informational_record``.

    Order is preserved within each bucket so the caller's rendering is stable.
    """
    actionable: list[str] = []
    informational: list[str] = []
    for cid in condition_ids:
        if disposition_for(cid, path=path) in ("action_required", "auto_retryable"):
            actionable.append(cid)
        else:
            informational.append(cid)
    return actionable, informational


__all__ = [
    "CLASSES",
    "CLEAR_SENTINELS",
    "DEFAULT_CLASS",
    "EMIT_SURFACES",
    "RETRY_PREFIX",
    "STATUSES",
    "ConditionSpec",
    "all_specs",
    "clear_probe_for",
    "condition",
    "disposition_for",
    "dismiss_key_fields",
    "emit_surfaces_for",
    "install_owned_ids",
    "install_owned_prefixes",
    "is_action_required",
    "load_registry",
    "matches_registered_pattern",
    "registered_patterns",
    "retry_handler_for",
    "split_by_disposition",
    "table_path",
]
