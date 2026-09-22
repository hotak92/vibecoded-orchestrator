# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor keys, resolved at request time through the sanctioned chain.

Three properties this module exists to guarantee:

**A key never reaches argv.** ``/proc/<pid>/cmdline`` is world-readable on
Linux and the equivalent is queryable on Windows, which is why VCO has a
secrets primitive at all. Resolution happens in-process through
``vco_lib.agent_secrets.get`` (hub -> file store -> project ``.env``); no
subprocess is spawned, so there is no command line to leak.

**A key never reaches a log, an error or a file under a project tree.** The
value is returned to exactly one caller, which puts it in one outbound header.
:class:`KeyResult` carries the value and, separately, a ``problem`` string
built only from key NAMES and store locations. ``tests/test_model_router_secrets.py``
asserts that a synthetic value never appears in any message this module
produces, in the log record it emits, or in ``repr()`` of the result.

**Resolution is LAZY and never blocks the event loop.** The first vendor
request resolves the key, not startup — so the daemon comes up on a machine
whose hub is down, and picks the key up later without a restart.
``agent_secrets.get`` performs blocking HTTP and file I/O, so the server calls
:meth:`VendorKeyResolver.aresolve`, which runs it on a worker thread. A
blocking call in the handler would stall ``/health`` for every concurrent
request — the exact failure the non-blocking-health rule exists to prevent.

**The scope is SHARED by default; a project may override it.** (Owner ruling,
2026-09-17.) The gateway is a machine-level daemon, and the two stores that
hold a vendor key both resolve project-first-then-shared for whoever asks —
the hub's ``/env`` route walks per-project, shared, global (first wins, each
gated on the ASKING project), and the file store tries
``projects/<NAME>/<key>`` then ``shared/<key>``. So "shared by default,
overridable per project" needs no new mechanism: it needs the daemon to ask as
a REGISTERED project, which is what :func:`_default_install_root` supplies and
``VCT_MODEL_GATEWAY_SECRET_PROJECT`` replaces. Nothing here reaches past those
two stores, and the hub's auth surface is untouched by this.

The import of ``vco_lib.agent_secrets`` is deliberately deferred to first use
rather than done at module scope, so that importing this module (for the
packaging smoke check, or on a machine mid-install) cannot fail on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .vendors import Vendor

logger = logging.getLogger(__name__)

#: Failures are cached for at most this long, so a burst of parallel requests
#: on a keyless machine does not become a burst of resolver calls, while a key
#: the user adds in the GUI is still picked up within seconds.
NEGATIVE_TTL_CAP_S = 30

#: How long a key that PREVIOUSLY resolved keeps being served once resolution
#: starts failing (issue 12, the 2026-09-20 update). The update stops vct-hub
#: by design; the hub's per-project route is the only way to an OS-keychain
#: key; and nine 503 ``vendor_key_unavailable`` answers followed during the
#: ~80-minute hub-stop window for a key that was fine the whole time. The
#: resolver's single cache slot holds last-ATTEMPT, so the first failed
#: re-resolution erased the good key — the fix is a SECOND, success-only
#: ``_last_good`` store, and this is the bound it serves within. 6 h covers an
#: update's hub-stop window with room to spare while still expiring a key
#: REVOKED this morning within the evening. The env knob
#: (``VCT_MODEL_GATEWAY_KEY_STALE_MAX_S``) is read by
#: :mod:`model_router.config`, never here: this module deliberately imports
#: neither ``os`` nor ``subprocess``.
DEFAULT_SERVE_STALE_MAX_AGE_S = 6 * 3600

SecretGetter = Callable[..., str]

#: Maps a project PATH (or id) to the hub's project id. Raises on every
#: failure mode, exactly like the getter.
ScopeProber = Callable[[str], str]

#: Answers "which orchestrator clone does this process belong to", or ``None``
#: when none does. Injectable so a test can drive the DEFAULT scope without
#: standing up a real clone on disk.
InstallRootResolver = Callable[[], Optional[str]]

#: Env var that pins the scope, named in every message that asks the user to
#: change it. Spelled once here; ``vco_lib.boot_service`` holds the same name
#: for the RENDER side (``GATEWAY_SECRET_PROJECT_ENV``).
SCOPE_ENV = "VCT_MODEL_GATEWAY_SECRET_PROJECT"


@dataclass
class KeyResult:
    """Outcome of one resolution attempt.

    ``key`` is the only place a value ever appears. ``problem`` is built from
    key names and store paths exclusively.
    """

    key: Optional[str] = None
    problem: Optional[str] = None
    #: ``resolved`` / ``cached`` / ``stale`` / ``missing``. ``stale`` is the
    #: last-known-good key served through a FAILED resolution (issue 12) —
    #: still the same key value, only its provenance changed.
    state: str = "missing"
    #: Which declared key name answered. Names are not secrets.
    resolved_from: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - exercised via assertions
        # Never let a value reach a traceback, a pytest diff or a log record
        # that formats the object.
        return (
            f"KeyResult(state={self.state!r}, resolved_from={self.resolved_from!r}, "
            f"key={'<set>' if self.key else None!r}, problem={self.problem!r})"
        )


@dataclass
class ScopeStatus:
    """Whether this daemon's SECRET SCOPE can reach the keychain tier at all.

    The 2026-09-10 defect this exists to make visible: a boot unit runs with
    ``WorkingDirectory=<VCT_STATE_DIR>``, which is not a registered project, so
    ``agent_secrets`` skipped tier 1 (the hub's ``/env`` route — the ONLY route
    to an OS-keychain key) for every request. ``/health`` listed the configured
    vendors beside an empty ``vendor_keys_cached``, which reads like "no key
    configured yet" and was actually "this daemon cannot see any key you
    configure".

    Since v0.2.95 the default scope is this install's orchestrator root rather
    than the cwd (owner ruling 2026-09-17: gateway secrets are SHARED, a
    project may override), so the failure above needs a machine with no
    resolvable clone — but the field stays, because that machine exists and
    the daemon must say so rather than answer 503 in silence.

    ``resolvable`` is deliberately TRI-STATE. ``None`` means "not probed yet",
    which is not the same claim as ``False``: a probe that has not run is not
    evidence of absence, and reporting one as the other is the exact defect
    the doctor's "not evaluated" arms were written for.
    """

    #: The effective scope — the pin, else this install's orchestrator root
    #: (the v0.2.95 default, through which SHARED secrets resolve), else the
    #: daemon's cwd when no clone resolves at all.
    project: Optional[str] = None
    #: True = it maps to a hub project id; False = it does not; None = unknown.
    resolvable: Optional[bool] = None
    #: Always populated when ``resolvable`` is not True. Names and paths only.
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "resolvable": self.resolvable,
            "reason": self.reason,
        }


@dataclass
class _CacheEntry:
    result: KeyResult
    fetched_at: float


def _default_getter() -> SecretGetter:
    """Import ``vco_lib.agent_secrets.get`` on first use.

    A failure here means a broken install (``install.py`` pip-installs the
    root distribution before this one), so it is raised, not swallowed.
    """
    from vco_lib.agent_secrets import get as _get  # noqa: PLC0415 — deliberate

    return _get


def _default_install_root() -> Optional[str]:
    """The orchestrator clone this daemon is running out of, or ``None``.

    This is the DEFAULT secret scope (v0.2.95, owner ruling 2026-09-17:
    "gateway secrets should be shared in the VCO system, but user can override
    per-project"). It is not "a project whose keys the gateway wants" — it is
    the REQUESTER IDENTITY the shared scope has to be asked through:

      * tier 1, the hub's ``/api/v1/projects/{id}/env``, is the only route to
        an OS-keychain key and it takes a registered project id. Shared keys
        live in their own keychain bucket (``_user_shared_``), owned by no
        project, and the hub resolves them for WHOEVER asks — bucket order
        per-project, then shared, then global, first wins, each gated on the
        asking project as requester. So "shared by default, a project may
        override" is already the rule; it just needs a registered asker.
      * the orchestrator root is the one project that is always registered
        (the launcher auto-registers it under the ``orchestrator-root`` slug),
        which is what makes it the honest machine-level identity.

    Resolved at RUNTIME rather than baked into the boot unit: a path frozen at
    render time is the same defect as the hand-written systemd drop-in this
    replaces — it keeps naming an install that may since have moved. The
    daemon runs out of the install's own venv, so asking the running process
    is both current and self-healing. The render side is held to that too, not
    merely documented as agreeing with it:
    ``vco_lib.boot_service.resolve_gateway_secret_project`` renders an EMPTY
    assignment for this variable and never derives a value, so a non-empty
    ``VCT_MODEL_GATEWAY_SECRET_PROJECT`` in a unit is always a scope somebody
    CHOSE — which is why it is honoured ahead of this default rather than
    reconciled with it.

    Never raises: a machine with no resolvable clone falls back to the
    pre-v0.2.95 cwd behaviour, and :meth:`VendorKeyResolver.probe_scope` then
    reports that scope as unresolvable with the remediation in it.
    """
    try:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

        root = resolve_install_root()
    except Exception:  # noqa: BLE001 — a missing clone is an answer, not a crash
        return None
    return str(root) if root is not None else None


def _default_scope_prober() -> ScopeProber:
    """Import the path→project-id mapper ``agent_secrets`` itself uses.

    Imported FROM ``agent_secrets`` rather than from ``project_config``, even
    though that is where it is defined: this module must ask the question the
    secrets chain asks, and following the same import keeps the two from
    drifting apart if that chain ever changes its resolver.
    """
    from vco_lib.agent_secrets import _resolve_project_id  # noqa: PLC0415

    return _resolve_project_id


class VendorKeyResolver:
    """Per-vendor key resolution with a small time-bounded cache."""

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        ttl_s: int = 300,
        getter: Optional[SecretGetter] = None,
        clock: Callable[[], float] = time.monotonic,
        scope_prober: Optional[ScopeProber] = None,
        probe_scope_on_miss: bool = False,
        install_root: Optional[InstallRootResolver] = None,
        serve_stale_max_age_s: int = DEFAULT_SERVE_STALE_MAX_AGE_S,
    ) -> None:
        self._project = project
        self._ttl_s = max(1, int(ttl_s))
        self._getter = getter
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        #: Success-only store (issue 12). ``_cache`` above holds the last
        #: ATTEMPT — a failed re-resolution overwrites it — so the
        #: last-known-good key needs its own home that only a success writes.
        self._last_good: dict[str, _CacheEntry] = {}
        #: Vendors currently served from ``_last_good``; drives the
        #: EDGE-TRIGGERED WARN pair (enter stale once, leave stale once) and
        #: ``serving_stale_ids`` for ``/health``.
        self._serving_stale: set[str] = set()
        #: Vendors whose last-known-good key aged PAST the serve-stale bound.
        #: A third state, tracked separately because leaving stale service
        #: through the bound is not recovery — the vendor is answering
        #: failures again, and the operator who read that line is still owed
        #: the one that says it resolves again (it was never emitted: the
        #: bound path discarded the vendor from ``_serving_stale``, so
        #: :meth:`_leave_stale` had nothing to react to).
        self._past_stale_bound: set[str] = set()
        #: Vendors already named in a "no key" WARN. Edge-triggered for the
        #: same reason as the pair above: this line fired once per ATTEMPT,
        #: so an 80-minute hub stop wrote ~160 identical warnings — the 503
        #: storm rebuilt as a WARN storm, which is what issue 12 set out to
        #: stop.
        self._no_key_logged: set[str] = set()
        self._serve_stale_max_age_s = max(1, int(serve_stale_max_age_s))
        self._scope_prober = scope_prober
        #: Resolves the DEFAULT scope when nothing is pinned. Memoized on
        #: first use rather than called here, because ``create_app`` promises
        #: to start no I/O and this one stats the filesystem.
        self._install_root = install_root or _default_install_root
        self._derived_scope: Optional[str] = None
        self._derived_probed = False
        #: The last verdict written to the LOG, so a stable state is stated
        #: once (at startup) and a genuine change is stated again.
        self._logged_scope: Optional[tuple] = None
        #: Diagnose the SCOPE when a key comes back empty — the daemon turns
        #: this on; a caller that injects its own resolver (every test, and
        #: any embedder) gets the quiet default, because a probe reaches the
        #: hub and a library must not do that behind its caller's back.
        self._probe_on_miss = bool(probe_scope_on_miss)
        #: ``project=None`` deliberately: ``scope_status`` returns a fresh
        #: unprobed status whenever the cached one is about a different scope,
        #: so this reads identically to a pre-resolved one WITHOUT making the
        #: constructor touch the filesystem.
        self._scope: ScopeStatus = ScopeStatus(project=None)
        self._scope_at: Optional[float] = None

    # ── secret SCOPE ─────────────────────────────────────────────────────
    @property
    def effective_project(self) -> str:
        """The scope a key lookup will actually use.

        Three cases, in order, each reported by :meth:`scope_origin`:

        1. the PIN — ``VCT_MODEL_GATEWAY_SECRET_PROJECT``. A user who names a
           project means it, and that project's own key then outranks the
           shared one through the ordinary first-wins rule in every tier. This
           is the per-project override; there is no second mechanism.
        2. the INSTALL ROOT this daemon runs out of — the default. See
           :func:`_default_install_root` for why a machine-level daemon needs
           a registered project identity at all in order to read a SHARED key.
        3. this process's WORKING DIRECTORY, only when no clone resolves.
           ``agent_secrets._hub_get`` resolves ``project or Path.cwd()``, so
           this is what the daemon used to do unconditionally — and for a boot
           unit the cwd is the state root, which is not a registered project,
           so tier 1 was skipped and every OS-keychain key was unreachable
           (the 2026-09-10 "no key found for vendor …" defect).

        ``Path.cwd()`` rather than ``os.getcwd()``: identical answer, and this
        module deliberately imports neither ``os`` nor ``subprocess`` — the
        structural half of "a key never reaches argv", pinned by
        ``test_the_secrets_module_does_not_import_subprocess``.
        """
        if self._project:
            return self._project
        if not self._derived_probed:
            self._derived_probed = True
            self._derived_scope = self._install_root()
        return self._derived_scope or str(Path.cwd())

    def scope_origin(self) -> str:
        """``pin`` / ``install_root`` / ``cwd`` — where the scope came from.

        Messages that ask a user to change something must name what is in
        force now; "project 'X'" alone never said whether X was chosen or
        derived, and that is the difference between "your pin is wrong" and
        "this machine has no clone".
        """
        if self._project:
            return "pin"
        # Resolve through the property so the memo is populated exactly once.
        self.effective_project
        return "install_root" if self._derived_scope else "cwd"

    def describe_scope(self) -> str:
        """One truthful phrase for the scope in force. Names only."""
        origin = self.scope_origin()
        scope = self.effective_project
        if origin == "pin":
            return f"{scope!r}, pinned by {SCOPE_ENV}"
        if origin == "install_root":
            return (
                f"{scope!r}, this install's orchestrator root (the default: "
                "shared secrets resolve through it, and a project pinned with "
                f"{SCOPE_ENV} overrides them)"
            )
        return (
            f"{scope!r}, this process's working directory — no orchestrator "
            f"clone resolved, so NOTHING sets the default scope. Set {SCOPE_ENV} "
            "to a registered project, or re-run `python install.py`"
        )

    def _stale_pin_hint(self) -> str:
        """Name the value to set, when a PIN has gone stale. ``""`` otherwise.

        A pin is written into the boot unit at registration, and a re-render
        PRESERVES whatever the installed artefact already carried — it cannot
        tell a value the user chose from one a previous render derived. So an
        install root that MOVES leaves the unit naming the old path forever,
        which is the same shape as the hand-written systemd drop-in this
        release retires. The daemon cannot safely overrule an explicit pin (it
        may name a project that is merely unmounted today), but it can see
        that its own clone lives somewhere else and say which value would
        work. A remedy the user can copy is worth three lines.
        """
        if not self._project:
            return ""
        root = self._install_root()
        if not root or root == self._project:
            return ""
        return (
            f" This daemon is running out of {root!r}, which is a different "
            f"path from the pin — if the pin is a stale one baked by an "
            f"earlier registration, clearing {SCOPE_ENV} (or setting it to "
            "that path) restores the default."
        )

    def scope_status(self) -> ScopeStatus:
        """The cached scope verdict. Touches NO store — ``/health`` calls it.

        Never probes: a liveness route that could reach the hub is a liveness
        route that can hang when the hub is down, which is precisely what
        ``/health`` exists not to do.
        """
        current = self.effective_project
        if self._scope.project != current:
            # The cwd moved (or a pin was injected) — the cached verdict is
            # about a different question now.
            return ScopeStatus(project=current)
        return self._scope

    def probe_scope(self) -> ScopeStatus:
        """Resolve the scope to a hub project id. BLOCKING — never on the loop.

        No secret is fetched: this asks only whether the scope NAMES something
        the hub knows, which is the precondition the keychain tier needs. The
        answer is cached under the same TTL rules as a key (a failure expires
        fast, so a hub that comes up later flips the verdict without a
        restart).
        """
        now = self._clock()
        current = self.effective_project
        if self._scope_at is not None and self._scope.project == current:
            ttl = (
                self._ttl_s if self._scope.resolvable
                else min(self._ttl_s, NEGATIVE_TTL_CAP_S)
            )
            if now - self._scope_at < ttl:
                return self._scope

        prober = self._scope_prober
        if prober is None:
            try:
                prober = _default_scope_prober()
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                status = ScopeStatus(
                    project=current,
                    resolvable=False,
                    reason=(
                        "the vct-secrets resolver could not be imported "
                        f"({exc}). This is a broken install: re-run "
                        "`python install.py`."
                    ),
                )
                self._record_scope(status, now)
                return status
            self._scope_prober = prober

        try:
            project_id = prober(current)
        except Exception as exc:  # noqa: BLE001 — every failure is one answer
            status = ScopeStatus(
                project=current,
                resolvable=False,
                reason=(
                    f"{type(exc).__name__}: {exc}. Vendor keys held in the OS "
                    "keychain (everything the launcher's Secrets panel saves, "
                    "shared scope included) are reachable ONLY through the "
                    "hub's per-project route, so with an unresolvable scope "
                    "only the file store is consulted. The scope in force is "
                    f"{self.describe_scope()}. Either start the launcher (so "
                    "the hub can answer and the orchestrator root is "
                    f"registered), or set {SCOPE_ENV} to a registered "
                    f"project and restart the gateway.{self._stale_pin_hint()}"
                ),
            )
        else:
            status = ScopeStatus(
                project=current,
                resolvable=bool(project_id),
                reason=None if project_id else "the resolver returned no project id",
            )
        self._record_scope(status, now)
        return status

    def _record_scope(self, status: ScopeStatus, now: float) -> None:
        """Cache a verdict AND say it out loud the first time it is true.

        A daemon whose scope cannot reach the keychain answers every vendor
        request 503 and, before this, wrote nothing at all to its log until
        one arrived — the user's first evidence was a failure inside Claude
        Code with no local trace. The log is where they look, so the verdict
        goes there.

        EDGE-TRIGGERED on ``(project, resolvable)``: stated once at startup,
        again only if it genuinely changes (a hub that comes up later flips it
        without a restart, and that flip is worth a line). A probe re-run on a
        key miss every 30 s must not become a log nobody can read.
        """
        self._scope, self._scope_at = status, now
        fingerprint = (status.project, status.resolvable)
        if fingerprint == self._logged_scope:
            return
        self._logged_scope = fingerprint
        if status.resolvable:
            logger.info(
                "model-gateway: vendor-key scope %s resolves to a registered "
                "project — shared secrets are reachable",
                self.describe_scope(),
            )
        elif status.resolvable is False:
            logger.warning(
                "model-gateway: vendor-key scope %s DOES NOT resolve: %s "
                "Vendor requests will fail until this is fixed.",
                self.describe_scope(),
                status.reason,
            )

    async def aprobe_scope(self) -> ScopeStatus:
        """:meth:`probe_scope` off the event loop."""
        return await asyncio.to_thread(self.probe_scope)

    def cached_vendor_ids(self) -> tuple[str, ...]:
        """Vendor ids with a live key in cache. Used by ``/health``; touches
        no store, so it cannot block."""
        now = self._clock()
        return tuple(
            sorted(
                vendor_id
                for vendor_id, entry in self._cache.items()
                if entry.result.key and now - entry.fetched_at < self._ttl_s
            )
        )

    def serving_stale_ids(self) -> tuple[str, ...]:
        """Vendor ids currently answered from the last-known-good store.

        ``/health`` reads this beside :meth:`cached_vendor_ids`. A
        stale-served vendor is deliberately NOT listed as cached — its
        ordinary cache slot holds a failure — so this is the only surface
        where the issue-12 state is visible.
        """
        return tuple(sorted(self._serving_stale))

    def invalidate(self, vendor_id: Optional[str] = None) -> None:
        """Drop cached state — INCLUDING the last-known-good store.

        An explicit invalidation (a key was rotated or revoked on purpose)
        must not be undone by serve-stale, so it clears ``_last_good`` too.
        """
        if vendor_id is None:
            self._cache.clear()
            self._last_good.clear()
            self._serving_stale.clear()
            self._past_stale_bound.clear()
            self._no_key_logged.clear()
        else:
            self._cache.pop(vendor_id, None)
            self._last_good.pop(vendor_id, None)
            self._serving_stale.discard(vendor_id)
            self._past_stale_bound.discard(vendor_id)
            self._no_key_logged.discard(vendor_id)

    def _serve_stale(
        self, vendor: Vendor, now: float, *, fresh: KeyResult
    ) -> Optional[KeyResult]:
        """Answer a FAILED resolution with the last-known-good key (issue 12).

        Field evidence: the 2026-09-20 update stopped vct-hub by design, the
        hub's per-project route is the only way to an OS-keychain key, and
        nine 503 ``vendor_key_unavailable`` answers followed during the
        ~80-minute hub-stop window for a key that was fine the whole time.
        Only a SUCCESS ever writes ``_last_good``, so the failed
        re-resolution that overwrote the ordinary cache slot cannot erase
        it here.

        The serve is bounded — past ``_serve_stale_max_age_s`` the resolver
        answers failures again, so a key REVOKED this morning stops being
        served the same day. Both WARNs are EDGE-TRIGGERED per vendor: one
        line entering stale service, one line leaving it. Per-request
        warnings would rebuild the 503 storm as a WARN storm.
        """
        entry = self._last_good.get(vendor.vendor_id)
        if entry is not None and entry.result.key:
            age = now - entry.fetched_at
            # Inclusive on purpose, and the only inclusive bound in this
            # module: this asks "is the key WITHIN the serve-stale bound",
            # while the TTL checks elsewhere ask "is the entry YOUNGER than
            # its TTL" and are therefore exclusive. Two questions, two
            # comparisons — not a drift to normalise away.
            if age <= self._serve_stale_max_age_s:
                if vendor.vendor_id not in self._serving_stale:
                    self._serving_stale.add(vendor.vendor_id)
                    logger.warning(
                        "model-gateway: vendor %s key resolution failed; "
                        "serving the last-known-good key (resolved %ds ago, "
                        "serve-stale bound %ds). Fresh resolution said: %s. "
                        "Expected while the hub is stopped — an update stops "
                        "it by design — and self-heals on the next success.",
                        vendor.vendor_id,
                        int(age),
                        self._serve_stale_max_age_s,
                        fresh.problem or "no key found",
                    )
                return KeyResult(
                    key=entry.result.key,
                    state="stale",
                    resolved_from=entry.result.resolved_from,
                )
        if vendor.vendor_id in self._serving_stale:
            self._serving_stale.discard(vendor.vendor_id)
            self._past_stale_bound.add(vendor.vendor_id)
            logger.warning(
                "model-gateway: vendor %s has no fresh key and its "
                "last-known-good one is past the serve-stale bound (%ds); "
                "answering failures again",
                vendor.vendor_id,
                self._serve_stale_max_age_s,
            )
        return None

    def _leave_stale(self, vendor_id: str) -> None:
        """State a RECOVERY once, whichever failure state it ends.

        Two states end here, and until 2026-09-22 only the first did: a
        vendor served from ``_last_good``, and a vendor whose last-good key
        aged past the bound and was answering failures again. The second was
        the louder one to be left in — the operator read "answering failures
        again" and was never told it stopped.
        """
        recovered = (
            vendor_id in self._serving_stale
            or vendor_id in self._past_stale_bound
        )
        was_stale = vendor_id in self._serving_stale
        self._serving_stale.discard(vendor_id)
        self._past_stale_bound.discard(vendor_id)
        self._no_key_logged.discard(vendor_id)
        if recovered:
            logger.info(
                "model-gateway: vendor %s key resolves again%s",
                vendor_id,
                "; no longer serving the last-known-good one" if was_stale
                else " after answering failures",
            )

    def resolve(self, vendor: Vendor) -> KeyResult:
        """Blocking resolution. Callers on the event loop use :meth:`aresolve`."""
        now = self._clock()
        entry = self._cache.get(vendor.vendor_id)
        if entry is not None:
            ttl = self._ttl_s if entry.result.key else min(self._ttl_s, NEGATIVE_TTL_CAP_S)
            if now - entry.fetched_at < ttl:
                if entry.result.key:
                    return KeyResult(
                        key=entry.result.key,
                        state="cached",
                        resolved_from=entry.result.resolved_from,
                    )
                # A cached failure still serves the last-known-good key while
                # within the bound (issue 12): the negative cache exists to
                # stop resolver storms, not to 503 requests through them.
                stale = self._serve_stale(vendor, now, fresh=entry.result)
                if stale is not None:
                    return stale
                return entry.result

        result = self._resolve_uncached(vendor)
        self._cache[vendor.vendor_id] = _CacheEntry(result=result, fetched_at=now)
        if result.key:
            self._last_good[vendor.vendor_id] = _CacheEntry(result=result, fetched_at=now)
            self._leave_stale(vendor.vendor_id)
            return result
        stale = self._serve_stale(vendor, now, fresh=result)
        if stale is not None:
            return stale
        return result

    async def aresolve(self, vendor: Vendor) -> KeyResult:
        """Resolve off the event loop.

        A cache hit is answered inline (no thread hop) so the common path
        costs nothing; only a real store lookup is offloaded.
        """
        now = self._clock()
        entry = self._cache.get(vendor.vendor_id)
        if entry is not None and entry.result.key:
            if now - entry.fetched_at < self._ttl_s:
                return KeyResult(
                    key=entry.result.key,
                    state="cached",
                    resolved_from=entry.result.resolved_from,
                )
        return await asyncio.to_thread(self.resolve, vendor)

    def _resolve_uncached(self, vendor: Vendor) -> KeyResult:
        getter = self._getter
        if getter is None:
            try:
                getter = _default_getter()
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                logger.error(
                    "model-gateway: secrets resolver unavailable for vendor %s: %s",
                    vendor.vendor_id,
                    exc,
                )
                return KeyResult(
                    problem=(
                        "the vct-secrets resolver could not be imported "
                        f"({exc}). This is a broken install: re-run "
                        "`python install.py`."
                    ),
                )
            self._getter = getter

        # The EFFECTIVE scope, not the raw pin. With no pin `agent_secrets`
        # would fall back to `Path.cwd()` itself — which for a boot unit is
        # the state root, not a registered project, so tier 1 (the hub's
        # per-project `/env`, the ONLY route to an OS-keychain key) was
        # skipped and every shared key the user saved in the launcher was
        # unreachable. Passing the resolved default makes the DEFAULT scope
        # this daemon's own install root, through which the shared bucket
        # resolves. Resolved once per call so a moved cwd cannot make two
        # keys in the same request answer from two different scopes.
        scope_arg = self.effective_project
        failures: list[str] = []
        for key_name in vendor.secret_keys:
            try:
                value = getter(key_name, project=scope_arg)
            except Exception as exc:  # noqa: BLE001 — every resolver error is a miss
                # agent_secrets raises a family of typed errors (not found,
                # access denied, hub unreachable, keychain locked). All of them
                # mean "no key from this name"; the TYPE is preserved in the
                # message so the user sees whether it was absent or refused.
                # Relaying the exception text is safe on the value axis: every
                # message that family constructs is built from key NAMES, store
                # PATHS and hub state (read in vco_lib/agent_secrets.py), never
                # from a resolved value — and on this branch no value exists.
                failures.append(f"{key_name}: {type(exc).__name__}: {exc}")
                continue
            if value and value.strip():
                logger.info(
                    "model-gateway: resolved key %r for vendor %s",
                    key_name,
                    vendor.vendor_id,
                )
                return KeyResult(
                    key=value.strip(),
                    state="resolved",
                    resolved_from=key_name,
                )
            failures.append(f"{key_name}: resolved but empty")

        scope = self.describe_scope()
        # The scope is diagnosed only HERE, on the miss: a resolution that
        # answered needs no diagnosis, and the hub round-trip this costs is
        # exactly the one a user staring at "no key found" wants paid. It also
        # makes /health definitive from the first failed vendor request
        # onwards, without /health itself ever touching a store.
        scope_note = ""
        if self._probe_on_miss:
            verdict = self.probe_scope()
            if verdict.resolvable is False:
                scope_note = (
                    f" THE SCOPE ITSELF DOES NOT RESOLVE ({verdict.project!r}): "
                    f"{verdict.reason} Until that is fixed no key stored in the "
                    "OS keychain can be found, whatever its name."
                )
        # Name the two places a key can actually BE PUT, not merely the fact
        # that none was found. Both write the SHARED scope — the gateway's
        # default, and what the owner's 2026-09-17 ruling asks for; the
        # per-project override comes last because it is the exception.
        problem = (
            f"no key found for vendor {vendor.vendor_id!r}. Tried "
            f"{', '.join(repr(k) for k in vendor.secret_keys)} in {scope}. "
            "Put it in the SHARED scope: the launcher's Secrets panel with "
            "scope 'shared' (OS keychain), or `vct set --shared --key "
            f"{vendor.secret_keys[0]}` (file store, ~/.vct-secrets/shared/). "
            f"To use one project's OWN key instead, set {SCOPE_ENV}=<that "
            "project's path> for the gateway — a project key outranks the "
            "shared one. The gateway picks any of these up "
            "within its key-cache TTL without a restart. "
            f"Resolver detail: {'; '.join(failures) if failures else 'no attempt made'}"
            f"{scope_note}"
        )
        if vendor.vendor_id not in self._no_key_logged:
            self._no_key_logged.add(vendor.vendor_id)
            logger.warning(
                "model-gateway: no key for vendor %s (tried %s). Said once "
                "per vendor until it resolves again, not once per attempt.",
                vendor.vendor_id,
                ", ".join(vendor.secret_keys),
            )
        return KeyResult(problem=problem)


__all__ = [
    "DEFAULT_SERVE_STALE_MAX_AGE_S",
    "InstallRootResolver",
    "KeyResult",
    "NEGATIVE_TTL_CAP_S",
    "SCOPE_ENV",
    "ScopeProber",
    "ScopeStatus",
    "SecretGetter",
    "VendorKeyResolver",
]
