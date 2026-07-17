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

Secrets NEVER touch argv, logs, or exception messages — errors name the
key and the tiers consulted, never the value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from vco_lib.project_config import (  # noqa: F401 — re-exported for callers
    Forbidden,
    HubUnreachable,
    ProjectNotFound,
    ResolverError,
    _get_with_401_retry,
    _resolve_project_id,
)

__all__ = [
    "AccessDenied",
    "Forbidden",
    "HubUnreachable",
    "KeychainLocked",
    "ProjectNotFound",
    "SecretNotFound",
    "exec_with_secrets",
    "get",
]


class SecretNotFound(ResolverError):
    """The key resolves nowhere (hub + file store both came up empty)."""


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


class AccessDenied(ResolverError):
    """The hub knows of no ACTIVE binding of this key for this project.

    Hub error code ``key_not_active``: the secret is paused in the
    launcher GUI, or no installed module / shared slot declares it for
    this project. The hub response does not distinguish the two cases
    (live-verified 2026-06-11), so :func:`get` still consults the file
    store on this error — the launcher gate governs keychain-managed
    slots, not the independent ``~/.vct-secrets`` file store. This
    exception surfaces only when the file store AND the project
    ``.env`` (tier 3) ALSO have no copy (or fallback is disabled). Fix
    in the launcher's SecretsPanel, ``vct set``, or the project's own
    ``.env``.
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


def _file_store_get(key: str, project: Optional[str]) -> Optional[str]:
    """Resolve ``key`` from the file store; None when absent.

    Order: ``projects/<NAME>/<key>`` (when a project name applies) →
    ``shared/<key>``. Strips ONE trailing newline, matching
    ``vct exec`` semantics.
    """
    root = _secrets_root()
    candidates: list[Path] = []
    name = _detect_file_project_name(project)
    if name:
        candidates.append(root / "projects" / name / key)
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


def _hub_get(key: str, project: Optional[str]) -> str:
    """Resolve ``key`` via vct-hub. Raises on every failure mode."""
    project_arg = project if project else str(Path.cwd())
    pid = _resolve_project_id(project_arg)  # raises ProjectNotFound / HubUnreachable

    resp = _get_with_401_retry(
        lambda port, _token: (
            f"http://127.0.0.1:{port}/api/v1/projects/{pid}/env"
        ),
        params={"key": key},
        # v0.2.76 Part 4 — /env is a per-project route; prefer the scoped
        # hub.token.<id> (falls back to the global hub.token). MUST MATCH
        # the sh/ps1 secrets resolvers passing the project id to hub_get.
        project_id=pid,
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
            f"hub.token.{pid}, or set VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub "
            "to reopen the one-release compat window"
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
        # unavailability. Keep the historical HubUnreachable classification.
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
        if code == "key_not_active":
            raise AccessDenied(
                f"key {key!r} not active for project {pid} "
                "(paused in the launcher, or not declared by any installed "
                "module / shared slot)"
            )
        raise SecretNotFound(
            f"hub 404 for key {key!r} (project={pid}, code={code!r})"
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

    Raises:
        SecretNotFound: key resolves nowhere (all tiers consulted).
        AccessDenied: the hub has no active binding for the key
            (paused / undeclared — the hub doesn't distinguish) AND
            tiers 2 + 3 have no copy either (or fallback is disabled).
        HubUnreachable: hub down AND fallback disabled.
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
        # the file store: the hub can't tell "explicitly paused" from
        # "never declared" (live-verified 2026-06-11), and hard-failing
        # here would strand every user-managed file-store key whenever
        # the launcher is running. The launcher gate governs keychain
        # slots; ~/.vct-secrets is an independent store.
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
        value = _file_store_get(key, project)
        if value is not None:
            return value
        # Tier 3: the project's own .env — read-only, lowest priority.
        # `.env` last means a user migrating a key into the GUI gets the
        # managed copy immediately without deleting their .env line.
        value = _project_dotenv_get(key, project)
        if value is not None:
            return value

    if isinstance(hub_error, AccessDenied):
        raise hub_error
    if isinstance(hub_error, KeychainLocked):
        # v0.2.82 WP-4a: the keychain was unreadable AND the file store +
        # project .env also missed (or fallback was disabled). Surface the
        # distinct KeychainLocked so the caller can branch on the honest
        # state, with a message naming BOTH the lock and the file-store miss.
        _tier3_dir = _dotenv_dir(project)
        _tier3_desc = (
            str(_tier3_dir / ".env") if _tier3_dir is not None
            else "skipped (bare project NAME — tier 3 needs a path)"
        )
        raise KeychainLocked(
            f"hub keychain is locked; key {key!r} also absent from file store "
            f"({_secrets_root()}) and project .env ({_tier3_desc}) "
            f"(tiers 2+3 checked={allow_file_fallback}) — unlock the login "
            "keychain or open the launcher. "
            f"Underlying hub state: {hub_error}"
        ) from hub_error
    if isinstance(hub_error, SecretNotFound) or allow_file_fallback:
        _tier3_dir = _dotenv_dir(project)
        _tier3_desc = (
            str(_tier3_dir / ".env") if _tier3_dir is not None
            else "skipped (bare project NAME — tier 3 needs a path)"
        )
        raise SecretNotFound(
            f"secret {key!r} not found (tier 1 hub: {hub_error}; tier 2 "
            f"file store: {_secrets_root()}; tier 3 project .env: "
            f"{_tier3_desc}; tiers 2+3 "
            f"checked={allow_file_fallback}). Fix: launcher SecretsPanel, "
            f"`vct set --project shared --key {key}`, or add the key to "
            f"the project's .env"
        ) from hub_error
    raise hub_error


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
