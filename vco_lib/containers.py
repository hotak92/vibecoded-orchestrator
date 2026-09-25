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
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

# "Is the runtime installed?" is judged on PATH AND in the usual install
# locations (v0.2.97 R9 H1(b)): a short-PATH caller must never read a runtime in
# ~/bin or /opt/homebrew/bin as absent. Every default ``which`` / ``run`` below
# goes through it; an injected probe is the whole world, as before.
from vco_lib import tool_search_dirs as _tsd

__all__ = [
    "CANONICAL_CONTAINERS",
    "HISTORICAL_ALIASES",
    "canonical_name",
    "all_known_names",
    "find_existing_container",
    "classify_container_probe",
    "UnknownServiceError",
    "ComposeIdentity",
    "compose_project_name",
    "compose_identity_of",
    "foreign_compose_identity",
    # v0.2.92 (§3.5 / R13): runtime + compose resolution, the ONE Python home.
    "RuntimeState",
    "RuntimeResolution",
    "RUNTIME_CANDIDATES",
    "RESOLVE_EXIT_CODES",
    "runtime_preference_from_env",
    "runtime_candidate_order",
    "runtime_pin",
    "read_runtime_txt",
    "write_runtime_txt",
    "runtime_txt_path",
    "PIN_VIA_ENV",
    "PIN_VIA_RUNTIME_TXT",
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


def _resolve_runtime(runtime: str, install_root: object = None) -> Optional[str]:
    """The runtime a container LOOKUP uses — THE pin rule (:func:`runtime_pin`:
    ``VCT_CONTAINER_RUNTIME`` → ``state/install/runtime.txt`` → auto).

    * Pinned: the pinned runtime when its binary is installed (on PATH, or
      in the usual install locations — :func:`vco_lib.tool_search_dirs.which`), else ``None`` —
      never the other runtime. Podman and Docker keep separate containers and
      volumes; answering "found" from the runtime the user did NOT pin names a
      container next to empty volumes (plan invariants I1/I2), so a pinned
      runtime that is missing means "not found".
    * Unpinned: the caller's ``runtime`` when on PATH, else the other one
      (auto-detection); an unknown ``runtime`` probes podman first.

    ``install_root`` is the root whose runtime.txt :func:`runtime_pin` reads;
    ``None`` (the default) resolves it the default way.

    Returns the executable name (``podman`` / ``docker``) or ``None``.
    """
    pin = runtime_pin(install_root=_DEFAULT_ROOT if install_root is None else install_root,
                      warn=lambda _m: None)
    if pin is not None:
        pinned = pin[0]
        return pinned if _tsd.which(pinned) else None

    effective = (runtime or "podman").strip().lower()
    if effective not in ("podman", "docker"):
        # Caller passed something weird. Probe both in podman-first order.
        for candidate in ("podman", "docker"):
            if _tsd.which(candidate):
                return candidate
        return None

    if _tsd.which(effective):
        return effective

    # Unpinned and the caller's choice is missing — auto-detect the other.
    other = "docker" if effective == "podman" else "podman"
    if _tsd.which(other):
        return other
    return None


def find_existing_container(
    service: str, runtime: str = "podman", *, install_root: object = None,
) -> Optional[str]:
    """Return the first container name from ``all_known_names(service)``
    that actually exists on the user's host, or ``None`` if none do.

    Uses ``<runtime> container inspect --format '{{.Name}}' <name>`` —
    exit 0 means the container exists (running OR stopped), a non-zero
    exit with a "no such" stderr means it does not, and any OTHER
    non-zero exit is an error (:func:`classify_container_probe`). Docker
    has no ``container exists`` subcommand (podman-only), so the old
    ``container exists`` probe answered "not found" for EVERY Docker
    lookup. Read-only probe; never mutates state.

    Runtime selection is THE pin rule (:func:`runtime_pin`, via
    :func:`_resolve_runtime`):
      * pinned (``VCT_CONTAINER_RUNTIME=podman|docker``, else the runtime
        ``state/install/runtime.txt`` records) — only that runtime is asked;
        when it is missing or down the answer is ``None``, never a container
        of the OTHER runtime (its containers sit on other volumes);
      * unpinned — the ``runtime`` argument (default podman), and the other
        runtime only when that one is not on PATH.
    ``install_root`` is passed to :func:`runtime_pin` (``None`` = resolve it
    the default way).

    Returns ``None`` when:
      * The pinned runtime is missing, or its ``container inspect``
        probe fails (e.g. the daemon is down) for every alias.
      * Unpinned and neither podman nor docker is on PATH.
      * The runtime is present but no alias's probe answers "exists".
      * ``service`` is not in the canonical registry — but in that case
        we raise ``UnknownServiceError`` instead of silently returning
        None, because a typo in the service name is a programming error,
        not a runtime condition.

    A probe error (non-zero exit that is NOT a "no such" message —
    daemon down, CLI failure) is soft-failed like a timeout, never
    recorded as "not found": the two are different answers, exactly as
    :class:`RuntimeState` keeps ABSENT and UNKNOWN apart.
    """
    # Validate first; bad service names are programmer errors.
    _validate_service(service)

    bin_name = _resolve_runtime(runtime, install_root)
    if bin_name is None:
        return None

    for name in all_known_names(service):
        try:
            res = _tsd.run(
                [bin_name, "container", "inspect",
                 "--format", "{{.Name}}", name],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Don't let a single hung/missing probe poison the whole
            # search — try the next alias.
            continue
        if classify_container_probe(res) == "exists":
            return name
    return None


#: How a failed ``container inspect`` says "no such container". Podman
#: prints ``Error: no such container <name>``; Docker prints
#: ``Error: No such container: <name>`` (and ``no such object`` for
#: API-level misses). Case-insensitive on stderr+stdout so either
#: runtime's phrasing matches.
_NO_SUCH_CONTAINER_RE = re.compile(r"no such (?:container|object)", re.IGNORECASE)


def classify_container_probe(
    res: "subprocess.CompletedProcess[str]",
) -> str:
    """Classify a ``<runtime> container inspect`` probe result:
    ``"exists"`` / ``"not_found"`` / ``"error"``.

    * exit 0 → ``"exists"`` (running OR stopped — inspect reports both).
    * non-zero exit whose stderr/stdout says "no such container/object"
      → ``"not_found"`` — a POSITIVE answer that the name is absent.
    * any other non-zero exit → ``"error"`` — the probe could not run
      (daemon down, CLI failure); the name's absence is UNKNOWN, and
      callers must not fold it into ``"not_found"``.
    """
    if res.returncode == 0:
        return "exists"
    blob = f"{res.stderr or ''}\n{res.stdout or ''}"
    if _NO_SUCH_CONTAINER_RE.search(blob):
        return "not_found"
    return "error"

# ===========================================================================
# Compose identity — which compose project created a running container
# (v0.2.93)
# ===========================================================================
#
# v0.2.92 taught `install.py` step 5 to `--force-recreate` the vct-managed
# services it adopts, so a changed compose config or a rebuilt image reaches
# the running container on `--update`. That is only sound when the container
# was CREATED by the compose identity install.py drives
# (`<root>/infrastructure/docker-compose.yml`, project `infrastructure`).
# Field 2026-09-07 (dogfood update to v0.2.92): every service was healthy and
# adopted, but the containers had been created in July from the legacy
# `claude_mcp_servers/compose.yaml` (project `vibecoded`). Compose under
# project `infrastructure` refused with a stale-network-label error — and had
# the network been clean it would have hit a container-name conflict instead —
# so the whole update died at step 5 for services that needed nothing.
#
# Compose stamps the creating project on every container it makes. Reading
# that label is the cheap, positive way to know BEFORE acting; a container
# without it was not made by compose at all, so `--force-recreate` is not a
# tool that applies to it either. Conservative default on this best-effort
# path: anything that cannot be positively read as OURS is treated as NOT ours.

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"

_COMPOSE_NAME_KEY_RE = re.compile(r"^name:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


@dataclass(frozen=True)
class ComposeIdentity:
    """The compose project a container was created under, from its labels."""

    project: str
    working_dir: str = ""
    config_files: str = ""

    def describe(self) -> str:
        parts = [f"project '{self.project}'"]
        if self.working_dir:
            parts.append(f"in {self.working_dir}")
        if self.config_files:
            parts.append(f"({self.config_files})")
        return " ".join(parts)


def compose_project_name(compose_dir: Path, compose_text: str = "") -> str:
    """Pure: the project name compose derives for ``compose_dir``.

    A top-level ``name:`` key in the compose file wins; otherwise compose uses
    the directory basename normalised the way docker compose does it —
    lower-cased, every character outside ``[a-z0-9_-]`` dropped, leading
    ``-``/``_`` trimmed. A ``COMPOSE_PROJECT_NAME`` / ``-p`` override is the
    caller's business (install.py sets neither).
    """
    m = _COMPOSE_NAME_KEY_RE.search(compose_text or "")
    if m:
        return m.group(1)
    raw = Path(compose_dir).name.lower()
    return re.sub(r"[^a-z0-9_-]", "", raw).lstrip("-_")


def compose_identity_of(
    container: str,
    runtime: str = "podman",
    *,
    run: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
) -> Optional[ComposeIdentity]:
    """Read the compose identity labels of ``container``.

    Returns ``None`` when the container cannot be inspected OR carries no
    compose project label (it was not created by compose). Read-only and
    soft-failing on every error — the caller must treat ``None`` as "not
    known to be ours", never as "ours".
    """
    bin_name = _resolve_runtime(runtime)
    if bin_name is None:
        return None
    fmt = "\t".join(
        f'{{{{index .Config.Labels "{label}"}}}}'
        for label in (
            COMPOSE_PROJECT_LABEL,
            COMPOSE_WORKING_DIR_LABEL,
            COMPOSE_CONFIG_FILES_LABEL,
        )
    )
    argv = [bin_name, "inspect", "--type", "container", "--format", fmt, container]
    try:
        res = (run or _tsd.run)(
            argv, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    fields = (res.stdout or "").strip("\n").split("\t")
    project = fields[0].strip() if fields else ""
    if not project:
        return None
    return ComposeIdentity(
        project=project,
        working_dir=fields[1].strip() if len(fields) > 1 else "",
        config_files=fields[2].strip() if len(fields) > 2 else "",
    )


def foreign_compose_identity(
    found: Optional[ComposeIdentity], own_project: str,
) -> Optional[str]:
    """Pure: why ``found`` is NOT an identity ``own_project`` may recreate.

    ``None`` means the container is ours to ``--force-recreate``. Any string
    is the reason it is not: no compose labels (not compose-created), or
    created under a different project. Only the project is judged — the
    working directory and file list are reported, not compared, because a
    moved checkout keeps its project name while its paths change.
    """
    if found is None:
        return "carries no compose project label — it was not created by compose"
    if found.project != own_project:
        return (
            f"was created by compose {found.describe()}, "
            f"not by project '{own_project}'"
        )
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
    #: WHICH channel pinned ``requested``: :data:`PIN_VIA_ENV` or
    #: :data:`PIN_VIA_RUNTIME_TXT` (``None`` = no pin). R7b F5: the install's
    #: record is a pin on every surface, so a refusal must say which knob.
    requested_via: Optional[str] = None
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
    #: Always ``False`` from v0.2.92 until the v0.2.97 record reconcile —
    #: still COMPUTED (``pref is not None and candidate != pref``), so it is
    #: a live invariant rather than a constant. The ONE sanctioned way it is
    #: ``True`` is :attr:`record_reconciled` (case (a) of the install-record
    #: reconcile, see :func:`resolve`); any change that reintroduces a
    #: fall-through outside that arm flips it and
    #: ``tests/fixtures/container_runtime_parity.json`` fails on every row.
    substituted: bool = False
    #: True only when the RECORD pin was answered with the other runtime
    #: because the recorded one is not installed (not on PATH nor in the usual
    #: install locations) and the read-only record reconcile decided case (a)
    #: on POSITIVE evidence — the other runtime answers and holds VCO's
    #: containers/volumes (v0.2.97 R9 H1: "no VCO data anywhere" is install's
    #: decision alone, never a read-only one), and the record is not a
    #: user-confirmed choice (H2) — see :func:`resolve`. What
    #: lets a caller say what happened: the record stays stale on disk (the
    #: next update re-records it), so the user gets ONE explanatory line,
    #: not a refusal. An ENV pin is never reconciled.
    record_reconciled: bool = False

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


#: The two pin channels, named the way the Rust mirror names them
#: (``container_runtime.rs::RuntimePinSource::label``).
PIN_VIA_ENV = "VCT_CONTAINER_RUNTIME"
PIN_VIA_RUNTIME_TXT = "state/install/runtime.txt"

#: "Resolve the install root the default way" — distinct from ``None``, which
#: a caller passes to say "there is no install root, so no runtime.txt".
_DEFAULT_ROOT: object = object()


def runtime_txt_path(install_root: Path) -> Path:
    """``<install_root>/state/install/runtime.txt`` — written by install.py
    ``_persist_runtime_txt`` with the runtime the install put the data on."""
    return Path(install_root) / "state" / "install" / "runtime.txt"


def read_runtime_txt(install_root: Optional[Path]) -> Optional[str]:
    """The recorded runtime (``podman`` / ``docker``), or ``None`` when there
    is no install root, no file, an unreadable file or an unknown token.
    MUST MATCH ``container_runtime.rs::read_runtime_txt``."""
    if install_root is None:
        return None
    try:
        token = runtime_txt_path(install_root).read_text(encoding="utf-8").strip().lower()
    except (OSError, ValueError):
        return None
    return token if token in RUNTIME_CANDIDATES else None


def write_runtime_txt(install_root: Path, container_runtime: Optional[str]) -> Optional[str]:
    """THE writer of ``state/install/runtime.txt`` — install.py
    (``_persist_runtime_txt``) and the record reconcile
    (:mod:`vco_lib.runtime_reconcile`) both call it, so the file has one
    format. ``container_runtime`` may carry a version (``"Podman 5.0.1"``):
    the first token, lower-cased, is recorded. Idempotent (no write when the
    record already says it). Returns the recorded token, or ``None`` when the
    value names no runtime (nothing written). Raises ``OSError``."""
    parts = (container_runtime or "").split()
    token = parts[0].lower() if parts else ""
    if token not in RUNTIME_CANDIDATES:
        return None
    path = runtime_txt_path(install_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        existing = ""
    if existing != token:
        path.write_text(token + "\n", encoding="utf-8")
    return token


def runtime_pin(
    env: Optional[Mapping[str, str]] = None,
    *,
    install_root: object = _DEFAULT_ROOT,
    warn: Optional[WarnFn] = None,
) -> Optional[tuple[str, str, Optional[Path]]]:
    """THE pin rule (R7b F5, owner ruling): ``VCT_CONTAINER_RUNTIME`` →
    ``state/install/runtime.txt`` → no pin. Returns ``(runtime, via, file)``
    — ``via`` is :data:`PIN_VIA_ENV` or :data:`PIN_VIA_RUNTIME_TXT` and
    ``file`` the runtime.txt path for the latter — or ``None`` (auto-detect).

    Every surface applies this same precedence: this module (the session-start
    hooks, install.py, the doctor), ``services/runtime.rs`` (the launcher's
    infra stack), ``container_runtime.rs`` (module containers, storage and
    volumes — ``runtime_candidate_order`` / ``pinned_runtime``) and the hub
    supervisor. Before v0.2.97 only the last two read runtime.txt, so a machine
    whose install recorded docker had its stack brought up under podman by the
    hooks while the storage page migrated the docker copies.

    ``install_root`` defaults to :func:`vco_lib.python_exe.resolve_install_root`
    (the clone this vco_lib belongs to); pass ``None`` for "no install root".
    """
    pref = runtime_preference_from_env(env, warn=warn)
    if pref is not None:
        return pref, PIN_VIA_ENV, None
    if install_root is _DEFAULT_ROOT:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415 — stdlib-light, lazy

        root: Optional[Path] = resolve_install_root()
    else:
        root = Path(install_root) if install_root is not None else None  # type: ignore[arg-type]
    recorded = read_runtime_txt(root)
    if recorded is not None and root is not None:
        return recorded, PIN_VIA_RUNTIME_TXT, runtime_txt_path(root)
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
    install_root: object = _DEFAULT_ROOT,
) -> str:
    """The FIRST candidate binary on PATH regardless of whether its daemon
    runs (the pinned runtime — :func:`runtime_pin` — wins when installed).
    ``""`` when none is."""
    _which = which or _tsd.which
    pin = runtime_pin(env, install_root=install_root, warn=lambda _m: None)
    pref = pin[0] if pin is not None else None
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
    return _probe([runtime, "version"], VERSION_PROBE_TIMEOUT_S, run or _tsd.run)


def daemon_responsive(
    runtime: str, *, which: Optional[WhichFn] = None, run: Optional[RunFn] = None,
) -> Optional[bool]:
    """``<runtime> info`` — round-trips to the daemon / socket / machine,
    the same code path compose-up needs. Catches a stopped Docker Desktop
    on macOS, a stopped ``podman.socket`` on Linux, an unstarted podman
    machine on Windows. ``False`` when the binary is not on PATH."""
    if not runtime or not (which or _tsd.which)(runtime):
        return False
    return _probe([runtime, "info"], DAEMON_PROBE_TIMEOUT_S, run or _tsd.run)


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
    _which = which or _tsd.which
    _run = run or _tsd.run
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

    * ``missing``    — not on PATH nor in the usual install locations.
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
        # "not installed" as far as this process can tell: the default probe
        # (vco_lib.tool_search_dirs.which) looks on PATH AND in the usual
        # install locations (R9 H1(b)), and the message says what was looked at.
        return _CandidateProbe(
            "missing", None, None, None,
            f"{candidate} is not on PATH nor in the usual install locations",
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
    *, via: str = PIN_VIA_ENV, recorded_file: Optional[Path] = None,
) -> str:
    """The actionable hint a refused pin carries — names what was pinned and
    through WHICH channel, why it is unusable, whether the other runtime IS
    usable, and the two things the user can do about it. Shaped like
    ``container_runtime.rs::module_runtime_pin_refusal``."""
    if via == PIN_VIA_RUNTIME_TXT:
        where = str(recorded_file) if recorded_file is not None else PIN_VIA_RUNTIME_TXT
        head = (
            f"the install recorded {pref} as this machine's container runtime "
            f"({where}) but {pinned.why}"
        )
        # `install.py --container` re-records runtime.txt through the one
        # writer; a hand edit of the file is never the remedy (G1, v0.2.97).
        repin = (
            f"run `python install.py --update --container {alternative}` (it "
            f"re-records {where}), or set VCT_CONTAINER_RUNTIME={alternative} "
            f"to override the record"
        ) if alternative else ""
    else:
        head = f"VCT_CONTAINER_RUNTIME={pref} is set but {pinned.why}"
        repin = f"unset VCT_CONTAINER_RUNTIME / set it to {alternative}"
    if alternative:
        return (
            f"{head}; {alternative} is usable but VCO will NOT drive it for you "
            f"(podman and docker have SEPARATE named volumes, so the stack would "
            f"come up EMPTY on the other one) — start {pref}, or {repin}"
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
    install_root: object = _DEFAULT_ROOT,
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

    The PIN (:func:`runtime_pin`: ``VCT_CONTAINER_RUNTIME``, else the
    install's ``state/install/runtime.txt`` under ``install_root``) makes the
    candidate order that runtime alone, and a pinned runtime that is unusable is a REFUSAL
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

    v0.2.97 (R8 follow-up, owner rule "users never need a manual step"): the
    RECORD pin — and ONLY the record pin — is reconciled read-only. When
    ``state/install/runtime.txt`` names a runtime that is NOT installed (on
    PATH nor in the usual install locations, :mod:`vco_lib.tool_search_dirs`)
    and the read-only record reconcile (:func:`vco_lib.runtime_reconcile.reconcile`
    with ``rewrite=False``) decides case (a) on positive evidence (the other
    runtime answers AND holds VCO's containers/volumes; the record is not a
    user-confirmed ``--container`` choice), the
    resolver ANSWERS the other runtime — writing nothing (the next update
    re-records it) — with :attr:`record_reconciled` /
    :attr:`RuntimeResolution.substituted` set and a ``reason`` that says what
    happened. Without this, every session hook refused a stack that was
    already running under the other runtime until the next update happened
    to rewrite the record. Every other case keeps the strict v0.2.92
    behaviour: a recorded runtime that is installed but down stays refused,
    data under both runtimes stays refused, and the ENV pin is NEVER
    reconciled.
    """
    _which = which or _tsd.which
    _run = run or _tsd.run
    _warn = warn or _default_warn
    pin = runtime_pin(env, install_root=install_root, warn=warn)
    pref = pin[0] if pin is not None else None
    via = pin[1] if pin is not None else None
    installed = installed_runtime(env=env, which=_which, install_root=install_root) or None
    if install_root is _DEFAULT_ROOT:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415 — stdlib-light, lazy

        root: Optional[Path] = resolve_install_root()
    else:
        root = Path(install_root) if install_root is not None else None  # type: ignore[arg-type]

    def _probe_one(candidate: str) -> _CandidateProbe:
        return _evaluate_candidate(
            candidate, which=_which, run=_run, probe_daemon=probe_daemon,
            probe_compose=probe_compose, home=home,
        )

    def _resolved(candidate: str, p: _CandidateProbe, *, note: Optional[str] = None,
                  reconciled: bool = False) -> RuntimeResolution:
        return RuntimeResolution(
            RuntimeState.RESOLVED, candidate, installed, p.compose,
            p.compose_form, p.daemon, note or p.why, requested=pref, requested_via=via,
            requested_installed=pref is not None and probes[pref].status != "missing",
            # Computed, not hardcoded: with a one-element pinned order this
            # is True only on the record-reconcile arm, and the fixture
            # asserts that on every row.
            substituted=pref is not None and candidate != pref,
            record_reconciled=reconciled,
        )

    probes: dict[str, _CandidateProbe] = {}
    first_no_compose: Optional[str] = None
    saw_unknown = False
    refused: list[str] = []

    order = runtime_candidate_order(pref)
    record_note: Optional[str] = None
    record_refusal_note: Optional[str] = None
    if (via == PIN_VIA_RUNTIME_TXT and root is not None and pref is not None
            and not _which(pref)):
        # The record names a runtime that is NOT installed. Ask the ONE
        # record reconciler, READ-ONLY (no runtime.txt write, no daemon
        # start, no ledger entry): its case (a) on POSITIVE evidence — the
        # other runtime answers AND holds VCO's containers/volumes, and the
        # record is not a user-confirmed choice — is the only way the strict
        # pin is lifted here (R9 H1/H2). "No VCO data anywhere", a confirmed
        # record, installed-but-down, data under both, an unlistable other
        # runtime and every reconcile failure keep the refusal below (the
        # reconcile says UNUSABLE and its reason joins the refusal's). The
        # env pin never reaches this arm.
        from vco_lib.runtime_reconcile import Outcome as _Outcome  # noqa: PLC0415 — lazy: runtime_reconcile imports this module
        from vco_lib.runtime_reconcile import reconcile as _record_reconcile

        probes[pref] = _probe_one(pref)  # "missing" — the ladder's own verdict
        rec = _record_reconcile(root, env=env, which=_which, run=_run, rewrite=False)
        if (rec.outcome is _Outcome.REWRITTEN and rec.runtime in RUNTIME_CANDIDATES):
            order = [rec.runtime]
            record_note = rec.detail
        elif rec.outcome is _Outcome.UNUSABLE and rec.detail:
            record_refusal_note = rec.detail

    for candidate in order:
        p = probes[candidate] = _probe_one(candidate)
        if p.status == "usable":
            return _resolved(candidate, p, note=record_note,
                             reconciled=record_note is not None)
        if p.status == "unknown":
            saw_unknown = True
        elif p.status == "refused":
            refused.append(candidate)
        elif p.status == "no_compose" and first_no_compose is None:
            first_no_compose = candidate

    if first_no_compose is not None:
        return _resolved(first_no_compose, probes[first_no_compose],
                         note=record_note, reconciled=record_note is not None)
    if saw_unknown:
        return RuntimeResolution(
            RuntimeState.UNKNOWN, None, installed, None, None, None,
            "a runtime probe timed out or could not be spawned — could not "
            "determine whether a container runtime is usable",
            requested=pref, requested_via=via,
            requested_installed=pref is not None and probes[pref].status != "missing",
        )
    if pref is not None:
        # The pin is refused, not routed around. Probe the OTHER runtime once
        # so the hint can name it — that is the user's repin target, and the
        # difference between "start podman" and "install a runtime".
        other = next(c for c in RUNTIME_CANDIDATES if c != pref)
        alternative = other if _probe_one(other).status == "usable" else None
        reason = _pin_refusal_reason(
            pref, probes[pref], alternative,
            via=via or PIN_VIA_ENV, recorded_file=pin[2] if pin is not None else None,
        )
        if record_refusal_note:
            # Why the stale-record reconcile did NOT switch (R9 H1/H2).
            reason += f" (not switched: {record_refusal_note})"
        _warn(reason)
        return RuntimeResolution(
            RuntimeState.ABSENT, None, installed, None, None,
            False if probes[pref].status == "refused" else None,
            reason, requested=pref, requested_via=via,
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
        # R9 H5: a runtime (or its compose front-end) found only in the usual
        # install locations must also be DRIVABLE by the caller, which runs it
        # by name. `search_path` is the caller's PATH with those directories
        # appended — `None` when nothing needs adding.
        search_path = _tsd.reachable_path() if res.state is RuntimeState.RESOLVED else None
        if args.json:
            d = res.to_dict()
            d["search_path"] = search_path
            print(json.dumps(d, sort_keys=True))
        elif args.shell:
            import shlex

            if search_path:
                # The .sh hooks `eval` this output, so their later bare-name
                # `$RUNTIME` / compose calls resolve (one line, only when
                # needed; whatever PATH already reached keeps winning).
                print(f"PATH={shlex.quote(search_path)}; export PATH")
            argv = res.compose or []
            print(f"VCO_RUNTIME_STATE={shlex.quote(res.state.value)}")
            print(f"VCO_RUNTIME={shlex.quote(res.runtime or '')}")
            print(f"VCO_RUNTIME_INSTALLED={shlex.quote(res.installed or '')}")
            print(f"VCO_COMPOSE_CMD={shlex.quote(' '.join(argv))}")
            print("VCO_COMPOSE_ARGV=(" + " ".join(shlex.quote(a) for a in argv) + ")")
            print(f"VCO_RUNTIME_REASON={shlex.quote(res.reason)}")
            print(f"VCO_RUNTIME_REQUESTED={shlex.quote(res.requested or '')}")
            print(f"VCO_RUNTIME_REQUESTED_VIA={shlex.quote(res.requested_via or '')}")
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
            # v0.2.97 (R8 follow-up): the record pin was answered with the
            # other runtime (case (a) of the read-only record reconcile).
            # ensure-containers.{sh,ps1} branch on this to say what happened
            # (one stdout line, the reason carries it) instead of letting a
            # stale-but-reconciled record look like a broken state.
            print(f"VCO_RUNTIME_RECONCILED={'1' if res.record_reconciled else '0'}")
        else:
            line = res.runtime or "-"
            if res.compose:
                line += " " + " ".join(res.compose)
            print(f"{res.state.value}: {line} ({res.reason})")
        return RESOLVE_EXIT_CODES[res.state]
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
