# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Agent-facing secret resolution (v0.2.54 S-7).

Thin Python helper so agents (and any Python tooling) can resolve
secrets without shelling out to the ``vct`` CLI and fighting quoting /
``--trusted`` friction::

    from vco_lib.agent_secrets import get, exec_with_secrets

    token = get("github_pat")                       # shared scope
    token = get("api_key", project="/path/to/proj") # project scope

    exec_with_secrets(
        ["gh", "api", "user"],
        secrets={"github_pat": "GH_TOKEN"},          # key -> env var
    )

ONE RESOLUTION CHAIN (v0.2.73 unification — MUST MATCH the other two
implementations: ``templates/scripts/vct_secrets_resolve.sh`` and
``templates/scripts/vct_secrets_resolve.ps1``; keep tier order,
fall-through rules, and the tier-3 parsing rule identical across all
three):

1. **vct-hub first (tier 1)** — the launcher's keychain-backed resolver
   at ``GET /api/v1/projects/{id}/env?key=NAME``, per-(secret ×
   requester) active-flag gated (the launcher's permission matrix).
   Hub discovery (port + token) and the 401 retry-with-rediscovery ride
   on the same machinery as :mod:`vco_lib.project_config`
   (``$VCT_HUB_PORT`` / ``$VCT_HUB_TOKEN`` env → ``<vct_root>/hub.port``
   + ``hub.token`` → defaults). Canonical path for launcher-managed
   slots (``github_pat``, ``openai_api_key``, module secrets, and — as
   of the v0.2.73 hub fix — every GUI-saved user secret).

2. **File store (tier 2)** — when the hub is unreachable (no launcher
   running), the project isn't registered, or the key isn't active,
   fall back to the Phase-1 file store at ``$VCT_SECRETS_DIR`` (default
   ``~/.vct-secrets``): ``projects/<NAME>/<key>`` then
   ``shared/<key>``. Same resolution order as ``tools/vct-secrets/vct``.

3. **Project ``.env`` (tier 3, READ-ONLY, lowest priority)** — the
   requesting project's own root ``.env``. Parsing rule (identical ×3):
   line-oriented; accept ``KEY=VALUE`` and ``export KEY=VALUE``; strip
   one matching pair of single/double quotes; NO variable expansion, NO
   command substitution; first match wins. The value is never logged,
   cached to disk, or re-exported into any VCO-written file. The pause
   model does not apply — the user pauses by editing their own file.
   Which ``.env``: ``project=`` path → that folder (a file path
   normalizes to its parent dir); ``project=None`` → cwd; a file-store
   NAME (no separators) → cwd.

Tiers 2 and 3 together are gated by ``allow_file_fallback`` — disable
when only the keychain truth is acceptable.

``shared_only=True`` is the SCOPE switch (mirrored by ``--shared-only`` in
``vct_secrets_resolve.sh`` / ``vct_secrets_resolve.ps1``, whose comments
read "tier 1 UNCHANGED — the chain minus two legs"): tier 2 is reduced to
its ``shared/<key>`` leg and tier 3 is skipped entirely. Tier 1 is NOT
gated: the same hub route with the same requester identity, because the
hub's own per-(secret x requester) matrix decides what the hub may answer.
What keeps a project's key out of VCO's own slot is therefore the hub's
``slot_exclusive`` declaration for that key (``vct-module.json``), enforced
in the hub's module API — not this flag. VCO's own consumers resolve
through it so a project's key is never spent on VCO (owner ruling
2026-09-26).

Secrets NEVER touch argv, logs, or exception messages — errors name the
key and the tiers consulted, never the value.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

from vco_lib.project_config import (  # noqa: F401 — re-exported for callers
    SECRET_READ_TIMEOUT_SECONDS,
    Forbidden,
    HubNotResponding,
    HubUnreachable,
    ProjectNotFound,
    ResolverError,
    _get_with_401_retry,
    _resolve_project_id,
)

#: v0.2.102 (W6-AB): the longest ``Retry-After`` the resolver honours before
#: its ONE retry of a hub ``503 keychain_unavailable``. The hub sends 2 s
#: today; the cap keeps a misbehaving header from parking a caller (a hook,
#: the gateway's resolution thread) for minutes.
HUB_RETRY_AFTER_CAP_SECONDS: float = 5.0

#: Default wait before the retry when ``Retry-After`` is absent / unreadable —
#: the hub's own value (``KEYCHAIN_UNAVAILABLE_RETRY_AFTER_SECS`` in
#: ``vct-hub/src/modules_api.rs``).
_HUB_RETRY_AFTER_DEFAULT_SECONDS: float = 2.0

#: Indirection so tests can observe the retry wait without sleeping.
_sleep = time.sleep

__all__ = [
    "AccessDenied",
    "Forbidden",
    "HUB_RETRY_AFTER_CAP_SECONDS",
    "HubNotResponding",
    "HubUnreachable",
    "KeychainLocked",
    "KeychainUnavailable",
    "lookup_stored",
    "per_project_copy_owner",
    "ProjectNotFound",
    "SecretNotFound",
    "SecretPaused",
    "SecretStoreUnavailable",
    "exec_with_secrets",
    "get",
]


class SecretNotFound(ResolverError):
    """The key resolves nowhere (hub + file store both came up empty)."""


class SecretStoreUnavailable(ResolverError):
    """A secret store could not be ASKED, so the miss proves nothing —
    RETRYABLE (v0.2.102, W6-AB).

    Raised by :func:`get` when tier 1 was unavailable — the hub was running
    but did not answer in time (:class:`HubNotResponding`: read/connect
    timeout, a connection dropped after connect, a hub 5xx) or its OS keychain
    was temporarily unavailable (:class:`KeychainUnavailable`, a subclass) —
    AND tiers 2 + 3 did not hold the key either. The key may well exist in the
    OS keychain: the remedy is to RETRY, never to add the key again, and the
    message says exactly that. Deliberately NOT a :class:`SecretNotFound`
    subclass — "we could not look" must never read as "it is not there"
    (2026-10-13: a busy hub's 5 s read timeout surfaced as "no key found …
    Put it in the SHARED scope" for a key that was in the keychain).

    A REFUSED connection (no hub running at all) is not this error: the
    resolver falls through to tiers 2 + 3 at once and a full miss stays
    :class:`SecretNotFound`, whose message names the not-running launcher.
    """


class KeychainLocked(ResolverError):
    """The hub could not read the OS keychain (503 ``keychain_locked`` /
    ``keychain_error``) — v0.2.82 WP-4a.

    Distinct from :class:`AccessDenied` (``key_not_active``): a locked or
    read-erroring keychain is NOT an authorization decision, it is an
    *unavailability* of tier 1's backing store. The hub cannot honestly
    report whether the key exists — so a partial/absent answer here means
    "we could not look," not "the key isn't authorized here."

    ``keychain_locked`` (503): the whole store is locked — the hub refuses
    the full-env AND ``?key=`` forms before constructing any Entry.
    ``keychain_error`` (503): a per-key non-lock keychain read failed on a
    ``?key=`` lookup (the key may exist but is currently unreadable).

    Deliberately NOT a :class:`HubUnreachable` subclass — the hub was
    reachable and answered; only its keychain backing was unavailable. The
    diagnostic must name the lock/error state (unlock the login keychain,
    or open the launcher), not "hub down."

    :func:`get` still consults the file store (tier 2) and project ``.env``
    (tier 3) on this error: those are INDEPENDENT sanctioned stores, not a
    keychain fallback, so a locked keychain must not strand a file-store or
    ``.env`` key. This is honest, not a silent downgrade — the final
    all-miss message names both the locked-keychain state and the
    file-store miss.
    """


class KeychainUnavailable(KeychainLocked, SecretStoreUnavailable):
    """The hub's OS keychain was TEMPORARILY unavailable (503
    ``keychain_unavailable``, sent with ``Retry-After``) — v0.2.102.

    A keychain operation timed out or the hub's keychain worker is stuck on
    an unresponsive Secret Service. Unlike ``keychain_locked`` there is
    nothing for the user to unlock: the remedy is to retry after the hub's
    ``Retry-After`` seconds. A SUBCLASS of :class:`KeychainLocked` so every
    existing ``except KeychainLocked`` caller keeps catching it, and
    :func:`get` falls through to the file store and project ``.env`` on it
    exactly as for ``KeychainLocked``. Also a :class:`SecretStoreUnavailable`
    (W6-AB): the one retryable "could not look" family.

    ``retry_after`` carries the hub's ``Retry-After`` header verbatim (or
    ``None``) for :func:`_hub_get`'s one retry.
    """

    retry_after: Optional[str] = None


class AccessDenied(ResolverError):
    """The hub knows of no ACTIVE binding of this key for this project.

    Hub error code ``key_not_active``: no installed module / shared slot
    declares the key for this project, it is not granted, or (pre-v0.2.101
    hubs only) it is paused — a hub that old cannot distinguish the cases.
    :func:`get` still consults the file store on this error — the launcher
    gate governs keychain-managed slots, not the independent
    ``~/.vct-secrets`` file store. This exception surfaces only when the
    file store AND the project ``.env`` (tier 3) ALSO have no copy (or
    fallback is disabled). Fix in the launcher's SecretsPanel, ``vct set``,
    or the project's own ``.env``.
    """


class SecretPaused(AccessDenied):
    """The key EXISTS but is paused for this requester (v0.2.101).

    Hub error code ``key_paused`` (404 — same status as ``key_not_active``,
    distinct code): the key is declared in a bucket that would serve this
    requester, and the per-(scope, key, requester) active flag is off. The
    hub names neither the value nor the scope, and neither does this
    exception.

    A SUBCLASS of :class:`AccessDenied` so every existing
    ``except AccessDenied`` caller keeps catching it unchanged; the
    distinct type exists for the callers that must NOT treat a pause as
    an absence — :func:`vco_lib.openai_key.migrate_dotenv_openai_key`
    must not re-copy a paused key into the shared file store (audit
    P3-1). :func:`get` still consults the file store (tier 2) and the
    project ``.env`` (tier 3) on this error, exactly as for
    ``key_not_active``: those stores are independent of the launcher's
    active flag, so the resolver chain's behaviour is unchanged — only
    the classification of a full-chain miss is sharper.
    """


# ─── File-store fallback ────────────────────────────────────────────────


def _secrets_root() -> Path:
    raw = os.environ.get("VCT_SECRETS_DIR", "").strip()
    return Path(raw) if raw else (Path.home() / ".vct-secrets")


def _detect_file_project_name(project: Optional[str]) -> Optional[str]:
    """Map the ``project`` argument onto a file-store project NAME.

    - simple name (no path separators) → used verbatim;
    - path (or None → cwd) → walk up looking for a ``.vct-project``
      marker file (same contract as ``vct detect-project``);
    - nothing found → None (shared-only resolution).
    """
    if project and "/" not in project and "\\" not in project:
        return project
    cur = Path(project).resolve() if project else Path.cwd()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        marker = candidate / ".vct-project"
        if marker.is_file():
            try:
                name = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            return name or None
    return None


#: Per-project opt-out marker for the tier-2 SHARED fallback. A project
#: that holds ``projects/<NAME>/.no-shared-fallback`` resolves ONLY its own
#: ``projects/<NAME>/<key>`` files from the file store — ``shared/<key>`` is
#: not consulted for it.
#:
#: This filename is MIRRORED, byte-for-byte, in three sibling resolvers that
#: cannot call into Python on this path:
#:   * ``templates/scripts/vct_secrets_resolve.sh``  (VCT_NO_SHARED_FALLBACK_MARKER)
#:   * ``templates/scripts/vct_secrets_resolve.ps1`` ($VctNoSharedFallbackMarker)
#:   * ``tools/vct-secrets/vct``                     (VCT_NO_SHARED_FALLBACK_MARKER)
#: and it is WRITTEN by the launcher's shared-secrets toggle in
#: ``launcher/src-tauri/src/commands/secrets_cmd.rs``. The four spellings are
#: locked together by ``tests/test_no_shared_fallback_marker_parity.py``; the
#: BEHAVIOUR of each resolver is pinned by its own suite. Change one, change
#: all five.
NO_SHARED_FALLBACK_MARKER = ".no-shared-fallback"


def _shared_fallback_disabled(root: Path, name: Optional[str]) -> bool:
    """Whether ``name`` has opted out of the tier-2 shared fallback.

    False whenever no project name applies: the marker is per-project, and
    a caller with no project identity has nothing to opt out of. Also False
    for the ``shared`` pseudo-name — opting the shared scope out of itself
    is meaningless, and treating it otherwise would let a stray
    ``projects/shared/`` orphan silently disable every shared read.

    Must match ``vct_secrets_resolve.sh::shared_fallback_disabled``,
    ``vct_secrets_resolve.ps1::Test-SharedFallbackDisabled`` and
    ``vct::shared_fallback_disabled``.
    """
    if not name or name == "shared":
        return False
    return (root / "projects" / name / NO_SHARED_FALLBACK_MARKER).exists()


def _file_store_get(
    key: str, project: Optional[str], *, shared_only: bool = False,
) -> Optional[str]:
    """Resolve ``key`` from the file store; None when absent.

    Order: ``projects/<NAME>/<key>`` (when a project name applies) →
    ``shared/<key>``. Strips ONE trailing newline, matching
    ``vct exec`` semantics.

    ``shared_only`` drops the per-project leg: the shared slot is the only
    candidate (see :func:`get`).

    The ``shared/`` leg is SKIPPED when the resolved project holds the
    ``.no-shared-fallback`` marker (see :data:`NO_SHARED_FALLBACK_MARKER`).
    This is the tier-2 half of the launcher's per-project
    "Disable shared secrets" gate: without it the toggle silenced tier 1
    while tier 2 kept serving the very values the user opted out of.
    """
    root = _secrets_root()
    candidates: list[Path] = []
    name = _detect_file_project_name(project)
    if name and not shared_only:
        candidates.append(root / "projects" / name / key)
    if not _shared_fallback_disabled(root, name):
        candidates.append(root / "shared" / key)
    for path in candidates:
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            return raw[:-1] if raw.endswith("\n") else raw
    return None


# ─── Tier 3: the project's own .env (read-only) ─────────────────────────


def _parse_dotenv_value(text: str, key: str) -> Optional[str]:
    """Extract ``key`` from ``.env``-style ``text``; None when absent.

    Parsing rule (must match ``vct_secrets_resolve.sh::dotenv_get`` and
    ``vct_secrets_resolve.ps1::Get-DotenvValue``): line-oriented; accept
    ``KEY=VALUE`` and ``export KEY=VALUE``; strip one matching pair of
    single/double quotes; NO variable expansion, NO command
    substitution; first match wins. Never logs the value.

    The line-level parse is shared with the two managed-block env readers via
    ``vco_lib.envfile`` (v0.2.84 fix-pass — one concern, one home). A bare
    ``.env`` has no managed block, so no BEGIN/END markers are passed: the
    WHOLE file is parsed (unchanged semantics).
    """
    from vco_lib.envfile import env_value

    return env_value(text, key)


def _dotenv_dir(project: Optional[str]) -> Optional[Path]:
    """Which folder's ``.env`` tier 3 reads (must match the sh/ps1 rule).

    ``project=`` path → that folder (file path → its parent, same
    normalization as :func:`_detect_file_project_name`); ``None`` → cwd.

    A bare file-store NAME (no path separators) → **skip tier 3** (return
    None), matching ``vct_secrets_resolve.sh`` / ``.ps1`` (test
    ``test_tier3_skipped_for_bare_project_id``). A NAME does not identify a
    filesystem location — mapping it to ``cwd`` would resolve a DIFFERENT
    directory's ``.env`` than the named project (a caller in project X asking
    for ``project="Y"`` would read X's ``.env``), which is both wrong and a
    cross-project leak. Skipping is the safe, parity-correct behavior.
    """
    if project and "/" not in project and "\\" not in project:
        return None  # bare NAME → skip tier 3 (parity with sh/ps1)
    cur = Path(project).resolve() if project else Path.cwd()
    if cur.is_file():
        cur = cur.parent
    return cur


def _project_dotenv_get(key: str, project: Optional[str]) -> Optional[str]:
    """Tier 3: read ``key`` from the project's own root ``.env``.

    READ-ONLY and lowest priority. The value never leaves the
    requesting process (no hub transport, no cross-project reads, no
    re-export into any VCO-written file). Returns None on any miss or
    I/O error (soft-fail).
    """
    dotenv_dir = _dotenv_dir(project)
    if dotenv_dir is None:
        return None  # bare NAME → tier 3 skipped (parity with sh/ps1)
    env_path = dotenv_dir / ".env"
    if not env_path.is_file():
        return None
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_dotenv_value(text, key)


# ─── Hub path ───────────────────────────────────────────────────────────


def _retry_wait_seconds(retry_after: Optional[str]) -> float:
    """Seconds to wait before the one retry: the hub's ``Retry-After`` (whole
    or fractional seconds), the hub default when absent/unreadable, never
    negative, never above :data:`HUB_RETRY_AFTER_CAP_SECONDS`."""
    try:
        wait = float(retry_after) if retry_after is not None else _HUB_RETRY_AFTER_DEFAULT_SECONDS
    except (TypeError, ValueError):
        wait = _HUB_RETRY_AFTER_DEFAULT_SECONDS
    if wait != wait:  # NaN
        wait = _HUB_RETRY_AFTER_DEFAULT_SECONDS
    return max(0.0, min(wait, HUB_RETRY_AFTER_CAP_SECONDS))


def _hub_get(key: str, project: Optional[str]) -> str:
    """Resolve ``key`` via vct-hub. Raises on every failure mode.

    v0.2.102 (W6-AB): at most ONE retry, and only when the hub is running
    but could not answer yet — :class:`HubNotResponding` (a read timeout of
    :data:`SECRET_READ_TIMEOUT_SECONDS`, a dropped connection, a 5xx) or a
    ``503 keychain_unavailable`` (after its ``Retry-After``, capped). A slow
    keychain op keeps running on the hub's worker and its result lands in the
    hub's value cache, so the retry is usually answered at once. A REFUSED
    connection (no hub) and every definite answer (200, 404 codes, 401, 403,
    ``keychain_locked``/``keychain_error``) are never retried.
    """
    try:
        return _hub_get_attempt(key, project)
    except KeychainUnavailable as exc:
        _sleep(_retry_wait_seconds(exc.retry_after))
    except HubNotResponding:
        pass
    return _hub_get_attempt(key, project)


def _hub_get_attempt(key: str, project: Optional[str]) -> str:
    """One resolution attempt against the hub (see :func:`_hub_get`)."""
    project_arg = project if project else str(Path.cwd())
    # raises ProjectNotFound / HubUnreachable / HubNotResponding
    pid = _resolve_project_id(project_arg, read_timeout=SECRET_READ_TIMEOUT_SECONDS)

    resp = _get_with_401_retry(
        lambda port, _token: (
            f"http://127.0.0.1:{port}/api/v1/projects/{pid}/env"
        ),
        params={"key": key},
        # v0.2.76 Part 4 — /env is a per-project route; prefer the scoped
        # hub.token.<id> (falls back to the global hub.token). MUST MATCH
        # the sh/ps1 secrets resolvers passing the project id to hub_get.
        project_id=pid,
        # v0.2.102 (W6-AB): a keychain-backed read may legitimately take
        # well over the config routes' 5 s — see SECRET_READ_TIMEOUT_SECONDS.
        read_timeout=SECRET_READ_TIMEOUT_SECONDS,
    )
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError as exc:
            raise HubUnreachable(
                "hub /env returned 200 but body is not JSON"
            ) from exc
        value = body.get(key) if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            raise SecretNotFound(
                f"hub returned 200 for project={pid} but no {key!r} field"
            )
        return value[:-1] if value.endswith("\n") else value
    if resp.status_code == 401:
        raise HubUnreachable(
            "hub returned 401 unauthorized; launcher may have restarted (token rotated)"
        )
    if resp.status_code == 403:
        # v0.2.77 L3-F3: a 403 is a scoped-credential boundary REFUSAL on the
        # gated /env route (the flip default-denies the coarse global
        # hub.token; or a token for another project was presented). Classify
        # it as `Forbidden` (NOT HubUnreachable) so the diagnostic is honest —
        # mirrors the config resolver triplet. `get()` still consults the
        # file store on this error (the file store is a legitimate secrets
        # tier by design), but the message names the scoped-token remediation
        # instead of the misleading "hub unreachable".
        raise Forbidden(
            f"hub returned 403 forbidden for {key!r} (project={pid}): the "
            "global hub.token is refused on /env (per-project token required) "
            "or a token for another project was presented. Present the scoped "
            f"hub.token.{pid} (the resolver prefers it; the hub mints one "
            "on first request). The legacy VCT_HUB_LEGACY_GLOBAL_ENV escape "
            "hatch was removed in v0.2.97"
        )
    if resp.status_code == 503:
        # v0.2.82 WP-4a: the hub reached its keychain but could not read it.
        # `keychain_locked` — the whole OS keychain is locked (refused for
        # BOTH the full-env and `?key=` forms before any Entry is built);
        # `keychain_error` — a per-key non-lock keychain read failed on this
        # `?key=` lookup. Both are UNAVAILABILITY of tier 1's backing store,
        # NOT an authorization decision, so they map to the distinct
        # `KeychainLocked` (never `AccessDenied`/`key_not_active`). Any OTHER
        # 503 (e.g. a future `service_misconfigured`) stays HubUnreachable via
        # the tail. `get()` still consults the file store + .env on this error
        # (independent sanctioned stores — see KeychainLocked's docstring).
        code = None
        try:
            err = resp.json()
            if isinstance(err, dict):
                code = (err.get("error") or {}).get("code")
        except ValueError:
            code = None
        if code == "keychain_unavailable":
            # v0.2.102: temporary — a keychain op timed out / the hub's
            # worker is stuck. Retry, nothing to unlock.
            raw_retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
            retry_after = raw_retry_after or "a few"
            unavailable = KeychainUnavailable(
                f"hub could not read the OS keychain for {key!r} "
                f"(project={pid}, code='keychain_unavailable'): the keychain is "
                f"temporarily unavailable (a keychain operation timed out) — "
                f"retry in {retry_after} seconds"
            )
            unavailable.retry_after = raw_retry_after
            raise unavailable
        if code in ("keychain_locked", "keychain_error"):
            locked = code == "keychain_locked"
            raise KeychainLocked(
                f"hub could not read the OS keychain for {key!r} "
                f"(project={pid}, code={code!r}): "
                + (
                    "the login keychain is locked"
                    if locked
                    else "a per-key keychain read failed (the key may exist "
                    "but is currently unreadable)"
                )
                + " — unlock the login keychain or open the launcher to "
                "restore secret resolution"
            )
        # Fall through: any other 503 is a hub-side condition, not a keychain
        # unavailability — the 5xx arm below (HubNotResponding, retryable).
    if resp.status_code == 404:
        code = None
        try:
            err = resp.json()
            if isinstance(err, dict):
                code = (err.get("error") or {}).get("code")
        except ValueError:
            code = None
        if code == "project_not_found":
            raise ProjectNotFound(f"project {pid} not found in launcher.db")
        if code == "key_paused":
            # v0.2.101 (Q7 / audit P3-1): the key exists but its
            # per-(scope, key, requester) active flag is off. Distinct from
            # key_not_active so writers can refuse to "fix" a pause by
            # storing a second copy. Still a 404 — same family as
            # key_not_active, and a 403 on this route means the
            # scoped-token boundary, not a pause.
            raise SecretPaused(
                f"key {key!r} exists but is paused for project {pid} "
                "(resume it in the launcher's Secrets panel; the value is "
                "not served while paused)"
            )
        if code == "key_not_active":
            raise AccessDenied(
                f"key {key!r} not active for project {pid} "
                "(paused in the launcher, or not declared by any installed "
                "module / shared slot)"
            )
        raise SecretNotFound(
            f"hub 404 for key {key!r} (project={pid}, code={code!r})"
        )
    if 500 <= resp.status_code <= 599:
        # v0.2.102 (W6-AB): the hub is running but failed to answer — an
        # unavailability (retry), never evidence that the key is absent.
        # Still a HubUnreachable subclass, so the historical classification
        # every existing caller branches on is unchanged.
        raise HubNotResponding(
            f"hub returned status {resp.status_code} for /env lookup"
        )
    raise HubUnreachable(
        f"hub returned status {resp.status_code} for /env lookup"
    )


# ─── Public API ─────────────────────────────────────────────────────────


def get(
    key: str,
    *,
    project: Optional[str] = None,
    allow_file_fallback: bool = True,
    shared_only: bool = False,
    allow_dotenv: bool = True,
) -> str:
    """Read a secret value through the canonical three-tier chain.

    Chain (must match ``vct_secrets_resolve.sh`` / ``.ps1``): hub
    (keychain, tier 1) → file store (tier 2) → the project's own
    ``.env`` (tier 3, read-only, lowest priority).

    Args:
        key: secret key name (e.g. ``"github_pat"``).
        project: project id, registered path, or file-store project
            name. ``None`` → current working directory (hub by-path
            lookup / tier-3 ``.env`` folder) / shared scope (file
            store).
        allow_file_fallback: when False, only the hub answer counts —
            a hub miss raises instead of consulting ``~/.vct-secrets``
            or the project ``.env`` (tiers 2 AND 3 are both gated).
        shared_only: restrict the chain to the SHARED slot — tier 2 is
            ``shared/<key>`` only (the ``projects/<NAME>/`` leg is not
            consulted) and tier 3 is not consulted at all. Tier 1 is
            UNCHANGED (sh/ps1 parity: the chain minus two legs), the hub's
            own matrix gating what it may answer. ``project``
            stays the REQUESTER identity for the hub gate and for the
            requester's ``.no-shared-fallback`` opt-out; it never widens
            the lookup. (v0.2.102, OD-4: VCO's OpenAI-slot consumer left
            this mode for the matrix chain — see ``allow_dotenv``; the
            install-time env-var deferral probe still resolves this way,
            machine-scope with no project identity.)
        allow_dotenv: when False, tier 3 (the project's own ``.env``) is
            skipped while tiers 1-2 keep their full chain (v0.2.102, OD-4).
            For a caller that resolves a key through the secrets matrix
            (per-project scope first, then shared) WITHOUT making the
            project ``.env`` a store for that key — the OpenAI embedding
            slot resolves this way, so a ``.env`` line can never answer
            for it. Default True: the general chain is unchanged.

    Raises:
        SecretNotFound: key resolves nowhere (all tiers consulted).
        AccessDenied: the hub has no active binding for the key
            (undeclared / not granted; pre-v0.2.101 hubs also answer this
            for a pause) AND tiers 2 + 3 have no copy either (or fallback
            is disabled).
        SecretPaused: subclass of :class:`AccessDenied` — the key EXISTS
            but is paused for this requester (v0.2.101 ``key_paused``) AND
            tiers 2 + 3 have no copy either. Same fallthrough semantics;
            raised as the distinct type so callers can tell a deliberate
            pause from an absence.
        SecretStoreUnavailable: RETRYABLE (v0.2.102, W6-AB) — tier 1 was
            running but could not answer (timed out / dropped / 5xx, or the
            hub's ``keychain_unavailable``) AND tiers 2 + 3 had no copy (or
            fallback is disabled). The key may exist; never "add the key".
        HubUnreachable: hub down (connection refused / no token) AND
            fallback disabled.
        ProjectNotFound: project unknown AND fallback disabled.
        Forbidden: hub refused the bearer on /env (403 — scoped-token
            required / wrong project) AND fallback disabled. With fallback
            enabled the file store is consulted first (a legitimate secrets
            tier); a subsequent miss surfaces as SecretNotFound whose message
            names the 403.
        KeychainLocked: the hub could not read the OS keychain (503
            ``keychain_locked`` / ``keychain_error``) AND the file store +
            project ``.env`` also had no copy (or fallback is disabled). The
            file store IS still consulted on this error (an independent
            sanctioned store, not a keychain fallback — see
            :class:`KeychainLocked`); this exception surfaces only when that
            miss ALSO happens, and its message names both the locked-keychain
            state and the file-store miss.
    """
    if not key or not key.strip():
        raise SecretNotFound("empty key")
    key = key.strip()

    def tier2_desc() -> str:
        root = _secrets_root()
        return f"{root}/shared" if shared_only else str(root)

    def tier3_desc() -> str:
        # Honest by construction: never claim a tier was consulted when the
        # mode skipped it (a reader of the miss must be able to tell which
        # stores were actually checked).
        if shared_only:
            return "not consulted (shared-only mode)"
        d = _dotenv_dir(project)
        return (
            str(d / ".env") if d is not None
            else "skipped (bare project NAME — tier 3 needs a path)"
        )

    hub_error: Optional[ResolverError] = None
    try:
        return _hub_get(key, project)
    except (
        HubUnreachable,
        ProjectNotFound,
        SecretNotFound,
        AccessDenied,
        Forbidden,
        KeychainLocked,
    ) as exc:
        # AccessDenied (key_not_active) intentionally falls through to
        # the file store: an undeclared key and — pre-v0.2.101 hubs — a
        # paused one read the same here, and hard-failing would strand
        # every user-managed file-store key whenever the launcher is
        # running. v0.2.101: SecretPaused (key_paused, the distinct
        # exists-but-paused answer) falls through TOO, deliberately —
        # the file store and the project .env are independent of the
        # launcher's active flag, so the resolver chain's behaviour is
        # byte-identical; only the classification of a full-chain miss
        # is sharper. The launcher gate governs keychain slots;
        # ~/.vct-secrets is an independent store.
        #
        # v0.2.77 L3-F3: Forbidden (403) ALSO falls through to the file
        # store — for SECRETS the file store is a legitimate independent
        # tier by design, so a keychain-route refusal should not strand a
        # file-store key. Unlike the CONFIG resolvers (where a 403 must
        # propagate rather than env-fallback), the secrets file store is
        # user-owned, not the masked-misconfig env values. The distinct
        # `Forbidden` type keeps the DIAGNOSTIC honest (no "hub unreachable"
        # mislabel) while preserving the legitimate fallback.
        #
        # v0.2.82 WP-4a: KeychainLocked (503 keychain_locked/keychain_error)
        # ALSO falls through. A locked keychain is an UNAVAILABILITY of tier
        # 1's backing store, not an authorization decision — the file store
        # and project .env are INDEPENDENT sanctioned stores, so a locked
        # keychain must not strand a key that lives there. This is honest,
        # not a downgrade: the file store never depended on the keychain, and
        # the distinct `KeychainLocked` type keeps the diagnostic accurate so
        # a full-chain miss names the lock state (not "hub down").
        hub_error = exc

    if allow_file_fallback:
        value = _file_store_get(key, project, shared_only=shared_only)
        if value is not None:
            return value
        # Tier 3: the project's own .env — read-only, lowest priority.
        # `.env` last means a user migrating a key into the GUI gets the
        # managed copy immediately without deleting their .env line.
        # Skipped in shared-only mode: a project's `.env` is a project-scoped
        # store by definition, and this mode exists so it cannot answer.
        # `allow_dotenv=False` (v0.2.102, OD-4) skips tier 3 alone: a caller
        # that resolves through the secrets MATRIX (per-project → shared)
        # without making the project `.env` a store for its key — the OpenAI
        # embedding slot resolves that way, so a `.env` line can never
        # answer for it.
        if not shared_only and allow_dotenv:
            value = _project_dotenv_get(key, project)
            if value is not None:
                return value

    if isinstance(hub_error, AccessDenied):
        raise hub_error
    if isinstance(hub_error, KeychainUnavailable):
        # v0.2.102: temporary unavailability — say "retry", not "unlock".
        raise KeychainUnavailable(
            f"hub keychain temporarily unavailable; key {key!r} also absent "
            f"from file store ({tier2_desc()}) and project .env "
            f"({tier3_desc()}) (tiers 2+3 checked={allow_file_fallback}) — "
            "retry shortly. "
            f"Underlying hub state: {hub_error}"
        ) from hub_error
    if isinstance(hub_error, HubNotResponding):
        # v0.2.102 (W6-AB): the hub is RUNNING but did not answer in time (a
        # slow OS keychain behind it, a dropped connection, a 5xx) — we could
        # not look, so the miss proves nothing. Retryable, and the message
        # must not tell anyone to add a key that may well be stored.
        raise SecretStoreUnavailable(
            f"secret store unavailable: vct-hub is running but did not answer "
            f"the read of {key!r} in time (typically a busy OS keychain) and "
            f"the key is also absent from the file store ({tier2_desc()}) and "
            f"project .env ({tier3_desc()}) (tiers 2+3 "
            f"checked={allow_file_fallback}). The key may well be stored — "
            "retrying is safe. "
            f"Underlying hub state: {hub_error}"
        ) from hub_error
    if isinstance(hub_error, KeychainLocked):
        # v0.2.82 WP-4a: the keychain was unreadable AND the file store +
        # project .env also missed (or fallback was disabled). Surface the
        # distinct KeychainLocked so the caller can branch on the honest
        # state, with a message naming BOTH the lock and the file-store miss.
        raise KeychainLocked(
            f"hub keychain is locked; key {key!r} also absent from file store "
            f"({tier2_desc()}) and project .env ({tier3_desc()}) "
            f"(tiers 2+3 checked={allow_file_fallback}) — unlock the login "
            "keychain or open the launcher. "
            f"Underlying hub state: {hub_error}"
        ) from hub_error
    if isinstance(hub_error, SecretNotFound) or allow_file_fallback:
        _fix = (
            f"Fix: store it in the shared slot — launcher Preferences → "
            f"Secrets → Shared (this user), or "
            f"`vct set --shared --key {key}`"
            if shared_only else
            f"Fix: launcher Preferences → Secrets, "
            f"`vct set --shared --key {key}`, "
            f"or add the key to the project's .env"
        )
        if isinstance(hub_error, HubUnreachable):
            # v0.2.102 (W6-AB): with no hub running the OS keychain was never
            # consulted — a key stored there is invisible, not absent. Say so
            # BEFORE the add-it remedy, so the remedy stays true.
            _fix = (
                "The launcher hub is not running, so the OS keychain was not "
                "checked — if the key is stored there, start the launcher and "
                "retry. Otherwise: " + _fix[len("Fix: "):]
            )
        raise SecretNotFound(
            f"secret {key!r} not found (tier 1 hub: {hub_error}; tier 2 "
            f"file store: {tier2_desc()}; tier 3 project .env: "
            f"{tier3_desc()}; tiers 2+3 "
            f"checked={allow_file_fallback}; shared_only={shared_only}). "
            + _fix
        ) from hub_error
    raise hub_error


#: :func:`lookup_stored` states: where the value was found, or why not.
STORED_KEYCHAIN = "keychain"      # tier 1 answered with a value
STORED_FILE = "file_store"        # tier 2 held it
STORED_ABSENT = "absent"          # the hub answered "not stored" and tier 2 missed
STORED_PAUSED = "paused"          # the hub answered key_paused: the key EXISTS but
                                  # its active flag is off (v0.2.101) and tier 2
                                  # missed — a writer must NOT store a copy
STORED_HUB_DOWN = "hub_down"      # the hub could not be asked (or knows no such
                                  # project) and tier 2 missed
STORED_UNKNOWN = "unknown"        # the hub refused / could not read the keychain / did not answer in time
                                  # (403, locked) and tier 2 missed


def lookup_stored(key: str, *, project: Optional[str] = None) -> tuple[str, Optional[str]]:
    """``(state, value)`` for ``key`` in VCO's OWN stores only — the hub's
    keychain (tier 1), then the file store (tier 2). NEVER the project
    ``.env`` (tier 3): a caller weighing a ``.env`` value against the store
    (value evidence) must not compare that value with itself.

    ``value`` is set only for :data:`STORED_KEYCHAIN` / :data:`STORED_FILE`.
    The miss states tell a writer what it may do: ``absent`` (the hub
    answered — nothing stored there), ``paused`` (the hub answered
    :data:`STORED_PAUSED` — the key exists but is paused, so storing a
    copy would shadow a deliberate pause), ``hub_down`` (no hub answer),
    and ``unknown`` (the hub answered but could not say — a 403 or locked
    keychain reads the same as an undeclared one, so it must not be
    overwritten). Never raises; the value is never logged."""
    state = STORED_UNKNOWN
    try:
        return STORED_KEYCHAIN, _hub_get(key, project)
    except SecretPaused:
        state = STORED_PAUSED
    except SecretNotFound:
        state = STORED_ABSENT
    except HubNotResponding:
        # v0.2.102 (W6-AB): the hub is RUNNING but did not answer in time —
        # it may hold the key, so a writer must not plant a second copy.
        state = STORED_UNKNOWN
    except (HubUnreachable, ProjectNotFound):
        state = STORED_HUB_DOWN
    except (ResolverError, OSError, ValueError):
        state = STORED_UNKNOWN
    value = _file_store_get(key, project)
    if value is not None:
        return STORED_FILE, value
    return state, None


def per_project_copy_owner(key: str, value: str) -> Optional[str]:
    """The file-store project NAME whose per-project copy of ``key`` EQUALS
    ``value``, or ``None`` when no project holds an equal copy
    (v0.2.102, audit P3-2).

    Why a writer needs this: promoting a machine-level value into the SHARED
    file store while some project already holds the identical value as its
    own per-project secret would plant a second, broader-scope copy of the
    same value — the two can drift, and the drift class is exactly what the
    one-name-one-store rule exists to prevent. The skip decision needs only
    the project NAME, never the value: the comparison is constant-time and
    nothing is logged.

    Walks ``$VCT_SECRETS_DIR/projects/*/`` (sorted, so the answer is
    deterministic) and compares each ``<NAME>/<key>`` with
    :func:`hmac.compare_digest`. The shared copy is NOT a candidate — a
    shared copy equal to the value is not a per-project copy. A directory
    whose file is unreadable is skipped (soft-fail). Never raises on missing
    directories; never reads or returns a value.
    """
    import hmac as _hmac

    root = _secrets_root()
    projects_dir = root / "projects"
    try:
        names = sorted(p.name for p in projects_dir.iterdir() if p.is_dir())
    except OSError:
        return None
    needle = value.strip().encode("utf-8")
    for name in names:
        path = projects_dir / name / key
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stored = raw[:-1] if raw.endswith("\n") else raw
        if _hmac.compare_digest(needle, stored.strip().encode("utf-8")):
            return name
    return None


def exec_with_secrets(
    cmd: list[str],
    *,
    secrets: Mapping[str, str],
    project: Optional[str] = None,
    allow_file_fallback: bool = True,
    env: Optional[Mapping[str, str]] = None,
    **run_kwargs,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` with secrets injected as env vars (never argv).

    Args:
        cmd: argv list for :func:`subprocess.run` (``shell=False``).
        secrets: mapping ``secret_key -> ENV_VAR_NAME``.
        project / allow_file_fallback: as in :func:`get`.
        env: base environment (default: ``os.environ``). The injected
            vars are layered on top of a COPY — the parent process env
            is never mutated.
        **run_kwargs: forwarded to :func:`subprocess.run`
            (``check=``, ``capture_output=``, ``cwd=``, ...).

    All secrets are resolved up-front: a missing secret raises BEFORE
    the child runs (fail-fast, mirroring ``vct exec``).
    """
    if run_kwargs.pop("shell", False):
        raise ValueError("exec_with_secrets refuses shell=True (argv-only contract)")
    resolved: dict[str, str] = {}
    for secret_key, var_name in secrets.items():
        if not var_name or any(c in var_name for c in "=\0 "):
            raise ValueError(f"invalid env var name: {var_name!r}")
        resolved[var_name] = get(
            secret_key, project=project, allow_file_fallback=allow_file_fallback
        )
    child_env = dict(env if env is not None else os.environ)
    child_env.update(resolved)
    return subprocess.run(cmd, env=child_env, **run_kwargs)  # noqa: S603


if __name__ == "__main__":  # pragma: no cover — tiny manual probe
    # `python -m vco_lib.agent_secrets KEY [PROJECT]` — prints whether the
    # key resolves and FROM WHERE. Never prints the value.
    _key = sys.argv[1] if len(sys.argv) > 1 else "github_pat"
    _proj = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        _v = get(_key, project=_proj)
        print(f"resolved {_key!r}: {len(_v)} chars (value not shown)")
    except ResolverError as e:
        print(f"unresolved {_key!r}: {type(e).__name__}: {e}")
        sys.exit(1)
