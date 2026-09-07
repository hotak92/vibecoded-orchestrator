# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.containers — canonical container-name registry.

Single source of truth for the names VCO uses for its infrastructure
containers (Weaviate, Ollama, the code-embedding service). Centralised
here so that install.py, MCP servers, hooks, tests, and any future
launcher Python code can ask one module "what container do I look for?"
and stay in lockstep with the actual `container_name:` fields shipped
in infrastructure/docker-compose.yml.

Background — the "maintainer-machine leak" (v0.2.15 fix, 2026-05-17)
-------------------------------------------------------------------
Pre-v0.2.15, install.py + MCP servers + several hooks hardcoded
``weaviate_claude`` / ``ollama_claude`` / ``code_embed_claude`` as the
fallback container name to look for or restart. Those names only ever
existed on the maintainer's own pre-VCO machine (from a
``weaviate_<project>`` per-workspace era that never shipped). VCO has
ONLY ever shipped these names publicly:

  * v0.1.x : ``weaviate`` / ``ollama`` / ``code_embed`` (unprefixed)
  * v0.2.x : ``vco_weaviate`` / ``vco_ollama`` /
             ``vco_code_embed`` (or ``vct_code_embed`` — see below)

So the historical aliases for the legacy-volume / restart-attempt
fallbacks are the UNPREFIXED names. The ``_claude``-suffixed names are
kept at the END of the alias list for the deepest possible fallback
(some maintainer-era installs may still have them on disk) but
de-emphasised; they are NOT a canonical VCO naming convention.

Renamed in v0.2.15: ``vct_code_embed`` → ``vco_code_embed``
-----------------------------------------------------------
The code-embedding container shipped as ``vct_code_embed`` in v0.2.x
for historical reasons (the launcher's Rust ``ServiceConfig::command``
and ``volume_role`` mapping pinned it). v0.2.15 renames it to
``vco_code_embed`` for naming consistency with the rest of the stack.
``vct_code_embed`` is kept in ``HISTORICAL_ALIASES`` so existing
installs migrate cleanly (the find-existing-container path still
recognises it).

Usage
-----
``canonical_name("weaviate")`` → ``"vco_weaviate"``

``all_known_names("weaviate")`` → ``["vco_weaviate", "weaviate", "weaviate_claude"]``

``find_existing_container("weaviate")`` → ``"vco_weaviate"`` if that
container exists on the user's host; falls back through the alias list
in order; returns ``None`` if none of them exist. Honours
``VCT_CONTAINER_RUNTIME=podman|docker`` per the same contract
install.py uses for runtime selection — and that contract is a PIN:
a pinned runtime that is unusable is REFUSED with an actionable hint,
never swapped for the other one (v0.2.92 BLOCKER-4; the two runtimes
have separate named volumes, so a swap forks the data plane).

Runtime + compose resolution (v0.2.92, PLAN-EXTENSION §3.5 / R13)
-----------------------------------------------------------------
``resolve()`` is the ONE Python answer to "which container runtime, which
compose command, and is it actually usable?" — tri-state
(``RESOLVED`` / ``ABSENT`` / ``UNKNOWN``), injectable probes, and a CLI
(``python -m vco_lib.containers resolve --json``) that the session-start
hooks call instead of re-deriving the answer in bash and PowerShell. See
the section header below for the history and the Rust mirror contract.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

__all__ = [
    "CANONICAL_CONTAINERS",
    "HISTORICAL_ALIASES",
    "canonical_name",
    "all_known_names",
    "find_existing_container",
    "UnknownServiceError",
    # v0.2.92 (§3.5 / R13): runtime + compose resolution, the ONE Python home.
    "RuntimeState",
    "RuntimeResolution",
    "RUNTIME_CANDIDATES",
    "RESOLVE_EXIT_CODES",
    "runtime_preference_from_env",
    "runtime_candidate_order",
    "installed_runtime",
    "binary_works",
    "daemon_responsive",
    "compose_command",
    "resolve",
    "selinux_enforcing",
    "main",
]


# ---------------------------------------------------------------------------
# Canonical names — these MUST match the `container_name:` fields shipped
# in infrastructure/docker-compose.yml and claude_mcp_servers/compose.yaml.
# A drift between this table and either compose file means the SessionStart
# hook will try to start the wrong container.
# ---------------------------------------------------------------------------
CANONICAL_CONTAINERS: dict[str, str] = {
    "weaviate":   "vco_weaviate",
    "ollama":     "vco_ollama",
    "code_embed": "vco_code_embed",
}


# ---------------------------------------------------------------------------
# Historical aliases — container names users may have on disk from older
# VCO releases (or, for the ``_claude``-suffixed names, from the
# maintainer's own pre-VCO machine). Sorted MOST RECENT FIRST so the
# find-existing-container probe prefers the freshest legacy install over
# the deepest one.
#
# Per-service ordering:
#   weaviate:   canonical | v0.1.x unprefixed | maintainer-era
#   ollama:     canonical | v0.1.x unprefixed | maintainer-era
#   code_embed: canonical | v0.2.x vct-prefix | v0.1.x unprefixed | maintainer-era
#
# The canonical name IS NOT duplicated here — `all_known_names()` prepends
# it. This keeps the alias list a pure "things that ARE NOT the canonical
# name but might exist" registry.
# ---------------------------------------------------------------------------
HISTORICAL_ALIASES: dict[str, list[str]] = {
    "weaviate":   ["weaviate", "weaviate_claude"],
    "ollama":     ["ollama",   "ollama_claude"],
    "code_embed": ["vct_code_embed", "code_embed", "code_embed_claude"],
}


class UnknownServiceError(KeyError):
    """Raised when a caller asks for a service name not in the registry."""


def _validate_service(service: str) -> None:
    if service not in CANONICAL_CONTAINERS:
        known = ", ".join(sorted(CANONICAL_CONTAINERS))
        raise UnknownServiceError(
            f"Unknown VCO service {service!r}. Known: {known}."
        )


def canonical_name(service: str) -> str:
    """Return the canonical container name for ``service``.

    >>> canonical_name("weaviate")
    'vco_weaviate'
    >>> canonical_name("code_embed")
    'vco_code_embed'

    Raises ``UnknownServiceError`` if ``service`` is not in
    ``CANONICAL_CONTAINERS``.
    """
    _validate_service(service)
    return CANONICAL_CONTAINERS[service]


def all_known_names(service: str) -> list[str]:
    """Return every name a probe should check for ``service``.

    Order: canonical first, then ``HISTORICAL_ALIASES[service]`` in
    declaration order (most recent legacy → oldest legacy).

    Duplicates are filtered while preserving order, so an alias that
    happens to equal the canonical name (defensive — none currently do)
    only appears once.

    >>> all_known_names("weaviate")
    ['vco_weaviate', 'weaviate', 'weaviate_claude']
    """
    _validate_service(service)
    seen: set[str] = set()
    out: list[str] = []
    for name in (canonical_name(service), *HISTORICAL_ALIASES[service]):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _resolve_runtime(runtime: str) -> Optional[str]:
    """Resolve the runtime hint to an executable on PATH.

    The ``VCT_CONTAINER_RUNTIME`` env var overrides the caller-passed
    default (matching install.py's contract). Recognised values are
    ``podman``, ``docker``, and ``auto`` (or unset). ``auto`` triggers
    podman-first probing. Unknown values are ignored and we fall through
    to the caller-passed default.

    Returns the actual executable name (``podman`` or ``docker``) that
    is present on PATH, or ``None`` if neither is available.
    """
    # Unrecognised env values fall through to the caller's default without
    # a warning: this helper runs on hot probe paths (v0.2.92: same parser
    # as `resolve()`, `warn` silenced).
    pref = runtime_preference_from_env(warn=lambda _m: None)
    effective = pref if pref is not None else (runtime or "podman").strip().lower()

    if effective not in ("podman", "docker"):
        # Caller passed something weird. Probe both in podman-first order.
        for candidate in ("podman", "docker"):
            if shutil.which(candidate):
                return candidate
        return None

    if shutil.which(effective):
        return effective

    # Effective choice missing — probe the other.
    other = "docker" if effective == "podman" else "podman"
    if shutil.which(other):
        return other
    return None


def find_existing_container(
    service: str, runtime: str = "podman",
) -> Optional[str]:
    """Return the first container name from ``all_known_names(service)``
    that actually exists on the user's host, or ``None`` if none do.

    Uses ``<runtime> container exists <name>`` which returns exit 0 when
    the container exists (running OR stopped) and non-zero otherwise.
    Read-only probe; never mutates state.

    Runtime selection follows the same contract as install.py:
      * ``VCT_CONTAINER_RUNTIME`` env var (if set to ``podman`` or
        ``docker``) wins over the ``runtime`` argument.
      * ``runtime="podman"`` (default) is used when the env is unset or
        set to ``auto``.
      * If the chosen runtime isn't on PATH, the function probes the
        OTHER runtime as a fallback before giving up.

    Returns ``None`` when:
      * Neither podman nor docker is on PATH.
      * The runtime is present but ``<runtime> container exists`` fails
        for every alias.
      * ``service`` is not in the canonical registry — but in that case
        we raise ``UnknownServiceError`` instead of silently returning
        None, because a typo in the service name is a programming error,
        not a runtime condition.
    """
    # Validate first; bad service names are programmer errors.
    _validate_service(service)

    bin_name = _resolve_runtime(runtime)
    if bin_name is None:
        return None

    for name in all_known_names(service):
        try:
            res = subprocess.run(
                [bin_name, "container", "exists", name],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Don't let a single hung/missing probe poison the whole
            # search — try the next alias.
            continue
        if res.returncode == 0:
            return name
    return None

# ===========================================================================
# Runtime + compose resolution — the ONE Python home (v0.2.92, R13 / §3.5)
# ===========================================================================
#
# Before v0.2.92 this question — "which container runtime, and which compose
# invocation?" — was answered by FIVE independent detectors: install.py
# (`_detect_container_runtime` / `_detect_installed_runtime` /
# `_get_compose_command` / `_container_runtime_reachable`), this module's
# `_resolve_runtime` (presence only), and three hook pairs
# (`ensure-containers`, `ensure-code-embed-service`, `verify-container-ports`,
# each `.sh` + `.ps1`). Their COMPOSE preference orders had already diverged
# four ways (podman-compose-first in install.py, `podman compose`-first in
# ensure-containers, podman-compose-only in ensure-code-embed-service). The
# launcher's Rust `services/runtime.rs` is the fifth, and the one users
# exercise most.
#
# From v0.2.92: install.py's helpers are thin calls into the functions
# below, and the hook pairs run `python -m vco_lib.containers resolve --json`
# (class A of the A>B>C rule — a user-action / session-start path where a
# ~50 ms subprocess is fine) instead of mirroring the logic in bash and
# PowerShell. `runtime.rs` stays as a DECLARED CLASS-C MIRROR (a compiled
# binary cannot shell out to Python for every services-watcher tick); its
# decision function and `resolve()` below are pinned to ONE fixture,
# `tests/fixtures/container_runtime_parity.json`, read by both
# `tests/test_container_runtime_ssot.py` and `runtime.rs`'s tests.
#
# Tri-state, per PLAN-EXTENSION §4: RESOLVED (a usable runtime), ABSENT (no
# runtime binary on PATH, or every binary present refuses — a TRUE fact with
# an actionable message), UNKNOWN (a probe could not RUN — timeout / OSError
# — so nothing is known). ABSENT and UNKNOWN are different answers and
# callers must not collapse them.



class RuntimeState(str, Enum):
    RESOLVED = "resolved"
    ABSENT = "absent"
    UNKNOWN = "unknown"


#: Canonical candidate order when the user expressed no preference.
#: Podman first — no commercial licence, increasingly native on
#: macOS/Windows — then Docker. `runtime.rs` and `container_runtime.rs`
#: hold the same order.
RUNTIME_CANDIDATES: tuple[str, ...] = ("podman", "docker")

#: Exit codes of `python -m vco_lib.containers resolve`, one per state.
#: 1 and 2 are deliberately NOT used: 1 is what the interpreter exits with
#: on an unhandled exception (an `import vco_lib` failure in a broken
#: install) and 2 is argparse's usage error — a hook that `eval`s the
#: output must be able to tell "absent" from "the resolver crashed".
RESOLVE_EXIT_CODES: dict[RuntimeState, int] = {
    RuntimeState.RESOLVED: 0,
    RuntimeState.ABSENT: 3,
    RuntimeState.UNKNOWN: 4,
}

#: Subprocess timeouts. `version` is a client-side call; `info` and
#: `compose version` round-trip to the daemon / external compose provider
#: (Podman 4 delegates `podman compose` to docker-compose or podman-compose
#: and prints a banner first — a cold disk cache made 2 s too short in the
#: field, so the launcher uses 5 s and this module matches it).
VERSION_PROBE_TIMEOUT_S = 15
DAEMON_PROBE_TIMEOUT_S = 15
COMPOSE_PROBE_TIMEOUT_S = 5

WhichFn = Callable[[str], Optional[str]]
RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
WarnFn = Callable[[str], None]


def _default_warn(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


@dataclass(frozen=True)
class RuntimeResolution:
    """What :func:`resolve` decided, and why.

    ``runtime`` is set only when ``state`` is RESOLVED. ``installed`` is
    the first candidate binary on PATH regardless of whether it works —
    the hint install.py needs to say "Docker is installed but not running"
    instead of "install Docker". ``compose`` is the argv prefix
    (``["podman", "compose"]`` / ``["podman-compose"]``) or ``None`` when
    the runtime has no compose support or compose was not probed.
    """

    state: RuntimeState
    runtime: Optional[str]
    installed: Optional[str]
    compose: Optional[list[str]]
    compose_form: Optional[str]
    daemon_responsive: Optional[bool]
    reason: str
    #: The runtime ``VCT_CONTAINER_RUNTIME`` asked for (``None`` = no
    #: preference). A PIN IS HONOURED OR REFUSED, never quietly swapped —
    #: see the ruling in :func:`resolve`.
    requested: Optional[str] = None
    #: Whether the pinned runtime's binary is on PATH at all. Splits "you
    #: pinned podman and it is not installed" (install it, or repin) from
    #: "you pinned podman and it is installed but down" (start it) — two
    #: different user actions the old single "not available" conflated.
    requested_installed: bool = False
    #: The OTHER runtime, named ONLY when it is usable and the pinned one is
    #: not. It is what the user can repin to; the resolver deliberately does
    #: NOT drive it on its own (podman and docker have SEPARATE named volumes
    #: — see ``infrastructure/docker-compose.yml``, so driving the other one
    #: forks the data plane and brings up an EMPTY Weaviate).
    alternative_usable: Optional[str] = None
    #: Always ``False`` since v0.2.92 — but still COMPUTED (``pref is not
    #: None and candidate != pref``), so it is a live invariant rather than a
    #: constant: any change that reintroduces a fall-through flips it and
    #: ``tests/fixtures/container_runtime_parity.json`` fails on every row.
    substituted: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


def runtime_preference_from_env(
    env: Optional[Mapping[str, str]] = None, *, warn: Optional[WarnFn] = None,
) -> Optional[str]:
    """``VCT_CONTAINER_RUNTIME=podman|docker|auto`` → the user's explicit
    choice, or ``None`` for unset / ``auto`` / unrecognised (the latter
    warns once on stderr — a misconfigured env var must not strand the
    user with no runtime; auto-detect finds whatever IS working).

    Same contract as ``container_runtime.rs::runtime_preference_from_env``
    and the ``runtime.rs`` override branch (v0.2.14 Bug #3 / PR-43).
    """
    source = os.environ if env is None else env
    raw = (source.get("VCT_CONTAINER_RUNTIME") or "").strip().lower()
    if not raw or raw == "auto":
        return None
    if raw in RUNTIME_CANDIDATES:
        return raw
    (warn or _default_warn)(
        f"VCT_CONTAINER_RUNTIME={raw!r} unrecognized (expected "
        "'podman' / 'docker' / 'auto'); falling through to auto-detect."
    )
    return None


def runtime_candidate_order(preference: Optional[str]) -> list[str]:
    """A PIN IS THE WHOLE ORDER; no preference means the canonical order.

    v0.2.92 (BLOCKER-4): this used to return ``[preference, *RUNTIME_CANDIDATES]``
    — the pinned runtime first, then BOTH, so a pinned-but-unusable runtime
    fell through to the other one. That is now a refusal (:func:`resolve`),
    and the order is byte-for-byte what ``runtime.rs::candidate_order``
    returns for every arm.
    """
    if preference in RUNTIME_CANDIDATES:
        return [preference]
    return list(RUNTIME_CANDIDATES)


def installed_runtime(
    *, env: Optional[Mapping[str, str]] = None, which: Optional[WhichFn] = None,
) -> str:
    """The FIRST candidate binary on PATH regardless of whether its daemon
    runs (env preference wins when installed). ``""`` when none is."""
    _which = which or shutil.which
    pref = runtime_preference_from_env(env, warn=lambda _m: None)
    if pref is not None and _which(pref):
        return pref
    for cmd in RUNTIME_CANDIDATES:
        if _which(cmd):
            return cmd
    return ""


def _probe(
    argv: Sequence[str], timeout: int, run: RunFn,
) -> Optional[bool]:
    """``True`` exit 0, ``False`` non-zero, ``None`` when the probe could not
    run at all (timeout / OSError) — the UNKNOWN input."""
    try:
        res = run(list(argv), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return res.returncode == 0


def binary_works(runtime: str, *, run: Optional[RunFn] = None) -> Optional[bool]:
    """``<runtime> version`` — the client binary runs at all."""
    return _probe([runtime, "version"], VERSION_PROBE_TIMEOUT_S, run or subprocess.run)


def daemon_responsive(
    runtime: str, *, which: Optional[WhichFn] = None, run: Optional[RunFn] = None,
) -> Optional[bool]:
    """``<runtime> info`` — round-trips to the daemon / socket / machine,
    the same code path compose-up needs. Catches a stopped Docker Desktop
    on macOS, a stopped ``podman.socket`` on Linux, an unstarted podman
    machine on Windows. ``False`` when the binary is not on PATH."""
    if not runtime or not (which or shutil.which)(runtime):
        return False
    return _probe([runtime, "info"], DAEMON_PROBE_TIMEOUT_S, run or subprocess.run)


def compose_command(
    runtime: str,
    *,
    which: Optional[WhichFn] = None,
    run: Optional[RunFn] = None,
    home: Optional[Path] = None,
) -> Optional[tuple[list[str], str]]:
    """The compose invocation for ``runtime``: ``(argv_prefix, form)``.

    ONE order, the launcher's (``runtime.rs::detect_compose_form``):
      1. subcommand — ``<runtime> compose version`` exits 0
         (Podman 4.x+ / Docker 20.10+; Podman delegates to an external
         provider, which is fine — it is what the user has).
      2. standalone — ``<runtime>-compose`` on PATH, or in ``~/.local/bin``
         (pip-installed ``podman-compose`` lives there and a graphical
         launcher's PATH may not include it).
    ``None`` when neither exists — the caller decides whether that is
    fatal (the stack cannot come up) or a skip (a probe-only hook).
    """
    _which = which or shutil.which
    _run = run or subprocess.run
    if _probe([runtime, "compose", "version"], COMPOSE_PROBE_TIMEOUT_S, _run):
        return [runtime, "compose"], "subcommand"
    standalone = f"{runtime}-compose"
    if _which(standalone):
        return [standalone], "standalone"
    user_local = (home or Path.home()) / ".local" / "bin" / standalone
    if user_local.is_file():
        return [str(user_local)], "standalone"
    return None


@dataclass(frozen=True)
class _CandidateProbe:
    """What probing ONE runtime established.

    ``status`` is the ladder's verdict:

    * ``missing``    — not on PATH.
    * ``unknown``    — a probe could not RUN (timeout / OSError); nothing
      is known about this candidate.
    * ``refused``    — the client binary or its daemon answered NO.
    * ``no_compose`` — usable, but no compose invocation exists.
    * ``usable``     — drive it.
    """

    status: str
    daemon: Optional[bool]
    compose: Optional[list[str]]
    compose_form: Optional[str]
    why: str


def _evaluate_candidate(
    candidate: str,
    *,
    which: WhichFn,
    run: RunFn,
    probe_daemon: bool,
    probe_compose: bool,
    home: Optional[Path],
) -> _CandidateProbe:
    """The per-candidate probe ladder — ONE copy, used both for the
    candidates :func:`resolve` walks and for the "is the runtime you did
    NOT pin usable?" question the refusal hint has to answer."""
    if not which(candidate):
        return _CandidateProbe(
            "missing", None, None, None, f"{candidate} is not on PATH",
        )
    works = binary_works(candidate, run=run)
    if works is None:
        return _CandidateProbe(
            "unknown", None, None, None,
            f"`{candidate} version` timed out or could not be spawned",
        )
    if not works:
        return _CandidateProbe(
            "refused", None, None, None,
            f"`{candidate} version` failed (client binary refuses)",
        )
    daemon: Optional[bool] = None
    if probe_daemon:
        daemon = daemon_responsive(candidate, which=which, run=run)
        if daemon is None:
            return _CandidateProbe(
                "unknown", None, None, None,
                f"`{candidate} info` timed out or could not be spawned",
            )
        if not daemon:
            return _CandidateProbe(
                "refused", daemon, None, None,
                f"`{candidate} info` failed (daemon / machine / socket not running)",
            )
    if not probe_compose:
        return _CandidateProbe("usable", daemon, None, None, f"{candidate} usable")
    compose = compose_command(candidate, which=which, run=run, home=home)
    if compose is None:
        return _CandidateProbe(
            "no_compose", daemon, None, None,
            f"{candidate} usable but no compose support found "
            f"(neither `{candidate} compose` nor `{candidate}-compose`)",
        )
    argv, form = compose
    return _CandidateProbe(
        "usable", daemon, argv, form, f"{candidate} usable with {' '.join(argv)}",
    )


def _pin_refusal_reason(
    pref: str, pinned: _CandidateProbe, alternative: Optional[str],
) -> str:
    """The actionable hint a refused pin carries — names what was pinned,
    why it is unusable, whether the other runtime IS usable, and the two
    things the user can do about it."""
    head = f"VCT_CONTAINER_RUNTIME={pref} is set but {pinned.why}"
    if alternative:
        return (
            f"{head}; {alternative} is usable but VCO will NOT drive it for you "
            f"(podman and docker have SEPARATE named volumes, so the stack would "
            f"come up EMPTY on the other one) — start {pref}, or unset "
            f"VCT_CONTAINER_RUNTIME / set it to {alternative}"
        )
    other = next(c for c in RUNTIME_CANDIDATES if c != pref)
    return (
        f"{head}; {other} is not usable either — start {pref} "
        f"(or install it), then retry"
    )


def resolve(
    *,
    env: Optional[Mapping[str, str]] = None,
    which: Optional[WhichFn] = None,
    run: Optional[RunFn] = None,
    warn: Optional[WarnFn] = None,
    probe_daemon: bool = True,
    probe_compose: bool = True,
    home: Optional[Path] = None,
) -> RuntimeResolution:
    """Resolve the container runtime the caller should drive.

    Walks :func:`runtime_candidate_order`; a candidate is USABLE when it is
    on PATH, ``version`` exits 0, and (when ``probe_daemon``) ``info``
    exits 0. With ``probe_compose``, a usable candidate WITH compose beats
    a usable candidate without one (the launcher's rule — a Podman 3.x
    without compose cannot bring the stack up, so Docker with compose
    wins); when no candidate has compose, the first usable one is still
    RESOLVED with ``compose=None`` so an installer can print the real
    compose error instead of "no runtime".

    ``VCT_CONTAINER_RUNTIME`` PINS the runtime: the candidate order becomes
    that runtime alone, and a pinned runtime that is unusable is a REFUSAL
    (``ABSENT``) carrying ``requested`` / ``requested_installed`` /
    ``alternative_usable`` and an actionable ``reason`` — never a silent (or
    even a loud) fall-through to the other runtime.

    Ruled in v0.2.92 (BLOCKER-4, overturning the earlier merge-lane ASK #1
    ruling that kept a reported fall-through): the two runtimes have
    PER-RUNTIME NAMED VOLUMES (``infrastructure/docker-compose.yml``), so
    driving compose on the runtime the user did not pin does not "rescue"
    them — it brings up an EMPTY Weaviate on :8081 that every downstream
    heal then reads as their knowledge graph, while the launcher (strict
    since PR-43) reports no runtime at all. The old leniency argument
    conflated *misconfigured* (pinned runtime not installed) with
    *temporarily down* (installed, machine stopped after a reboot) — and the
    second is the common case, where a fall-through forks the data plane.
    ``runtime.rs::candidate_order`` has always been strict; both surfaces now
    agree, which is what ``env_pref_unusable_is_refused_not_substituted`` in
    the parity fixture pins.

    Probes are injectable (``which`` / ``run``) so the decision is unit-
    testable against ``tests/fixtures/container_runtime_parity.json``
    without podman or docker on the machine.
    """
    _which = which or shutil.which
    _run = run or subprocess.run
    _warn = warn or _default_warn
    pref = runtime_preference_from_env(env, warn=warn)
    installed = installed_runtime(env=env, which=_which) or None

    def _probe_one(candidate: str) -> _CandidateProbe:
        return _evaluate_candidate(
            candidate, which=_which, run=_run, probe_daemon=probe_daemon,
            probe_compose=probe_compose, home=home,
        )

    def _resolved(candidate: str, p: _CandidateProbe) -> RuntimeResolution:
        return RuntimeResolution(
            RuntimeState.RESOLVED, candidate, installed, p.compose,
            p.compose_form, p.daemon, p.why, requested=pref,
            requested_installed=pref is not None and probes[pref].status != "missing",
            # Computed, not hardcoded: with a one-element pinned order this
            # can no longer be True, and the fixture asserts that on every row.
            substituted=pref is not None and candidate != pref,
        )

    probes: dict[str, _CandidateProbe] = {}
    first_no_compose: Optional[str] = None
    saw_unknown = False
    refused: list[str] = []

    for candidate in runtime_candidate_order(pref):
        p = probes[candidate] = _probe_one(candidate)
        if p.status == "usable":
            return _resolved(candidate, p)
        if p.status == "unknown":
            saw_unknown = True
        elif p.status == "refused":
            refused.append(candidate)
        elif p.status == "no_compose" and first_no_compose is None:
            first_no_compose = candidate

    if first_no_compose is not None:
        return _resolved(first_no_compose, probes[first_no_compose])
    if saw_unknown:
        return RuntimeResolution(
            RuntimeState.UNKNOWN, None, installed, None, None, None,
            "a runtime probe timed out or could not be spawned — could not "
            "determine whether a container runtime is usable",
            requested=pref,
            requested_installed=pref is not None and probes[pref].status != "missing",
        )
    if pref is not None:
        # The pin is refused, not routed around. Probe the OTHER runtime once
        # so the hint can name it — that is the user's repin target, and the
        # difference between "start podman" and "install a runtime".
        other = next(c for c in RUNTIME_CANDIDATES if c != pref)
        alternative = other if _probe_one(other).status == "usable" else None
        reason = _pin_refusal_reason(pref, probes[pref], alternative)
        _warn(reason)
        return RuntimeResolution(
            RuntimeState.ABSENT, None, installed, None, None,
            False if probes[pref].status == "refused" else None,
            reason, requested=pref,
            requested_installed=probes[pref].status != "missing",
            alternative_usable=alternative,
        )
    if installed:
        return RuntimeResolution(
            RuntimeState.ABSENT, None, installed, None, None, False,
            f"{installed} is installed but not usable (daemon / machine / "
            "socket not running)" if refused else f"{installed} is on PATH but "
            "no candidate answered",
        )
    return RuntimeResolution(
        RuntimeState.ABSENT, None, None, None, None, None,
        "neither podman nor docker is on PATH",
    )


def selinux_enforcing(
    *,
    which: Optional[WhichFn] = None,
    run: Optional[RunFn] = None,
    sysfs: Optional[Path] = None,
    system: Optional[str] = None,
) -> bool:
    """True iff SELinux is currently in ``Enforcing`` mode.

    Detection chain (v0.2.53 L-P0-3, moved here from install.py in v0.2.92
    — install.py had TWO copies, this one and an inline ``getenforce`` in
    the bootstrap probe):
      1. ``getenforce`` prints ``Enforcing`` — canonical on the Fedora /
         RHEL family.
      2. ``/sys/fs/selinux/enforce`` reads ``1`` — minimal containers
         without selinux-utils.
      3. Anything else (binary missing, sysfs absent, non-Linux, errors)
         → ``False``. Conservative: only ``True`` when certain, because a
         wrong ``True`` adds ``:Z`` labels a non-SELinux kernel rejects.
    """
    if (system or platform.system()) != "Linux":
        return False
    _which = which or shutil.which
    _run = run or subprocess.run
    getenforce = _which("getenforce")
    if getenforce:
        try:
            res = _run([getenforce], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return (res.stdout or "").strip().lower() == "enforcing"
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    try:
        enforce_path = sysfs if sysfs is not None else Path("/sys/fs/selinux/enforce")
        if enforce_path.is_file():
            return enforce_path.read_text(encoding="utf-8").strip() == "1"
    except (OSError, UnicodeDecodeError):
        pass
    return False


# ---------------------------------------------------------------------------
# CLI — `python -m vco_lib.containers resolve [--json] [--no-daemon] [--no-compose]`
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.containers",
        description="Container-runtime + compose resolution (the ONE home; "
                    "hooks call this instead of mirroring the logic).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="Resolve runtime + compose; exit 0/1/2 = resolved/absent/unknown.")
    r.add_argument("--json", action="store_true", help="Print the resolution as JSON on stdout.")
    r.add_argument(
        "--shell", action="store_true",
        help="Print `VCO_RUNTIME_*` assignments for a bash `eval` (the .sh hooks); "
             "the .ps1 hooks use --json + ConvertFrom-Json.",
    )
    r.add_argument("--no-daemon", action="store_true", help="Skip the `<runtime> info` round-trip.")
    r.add_argument("--no-compose", action="store_true", help="Skip compose detection.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    import json

    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "resolve":
        res = resolve(probe_daemon=not args.no_daemon, probe_compose=not args.no_compose)
        if args.json:
            print(json.dumps(res.to_dict(), sort_keys=True))
        elif args.shell:
            import shlex

            argv = res.compose or []
            print(f"VCO_RUNTIME_STATE={shlex.quote(res.state.value)}")
            print(f"VCO_RUNTIME={shlex.quote(res.runtime or '')}")
            print(f"VCO_RUNTIME_INSTALLED={shlex.quote(res.installed or '')}")
            print(f"VCO_COMPOSE_CMD={shlex.quote(' '.join(argv))}")
            print("VCO_COMPOSE_ARGV=(" + " ".join(shlex.quote(a) for a in argv) + ")")
            print(f"VCO_RUNTIME_REASON={shlex.quote(res.reason)}")
            print(f"VCO_RUNTIME_REQUESTED={shlex.quote(res.requested or '')}")
            # These two have NO consumer today, and that is deliberate rather
            # than unwired: `--shell` and `--json` carry the SAME facts, so a
            # hook that wants to BRANCH on "is the other runtime usable?" can,
            # instead of re-parsing the human-readable reason string. The
            # session-start hooks currently only PRINT the reason, which
            # already contains both facts. If you are tempted to delete these
            # as dead: check `--json` first — dropping them would make the two
            # surfaces disagree, which is the split-brain BLOCKER-4 fixed.
            print(f"VCO_RUNTIME_REQUESTED_INSTALLED={'1' if res.requested_installed else '0'}")
            print(f"VCO_RUNTIME_ALTERNATIVE_USABLE={shlex.quote(res.alternative_usable or '')}")
        else:
            line = res.runtime or "-"
            if res.compose:
                line += " " + " ".join(res.compose)
            print(f"{res.state.value}: {line} ({res.reason})")
        return RESOLVE_EXIT_CODES[res.state]
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
