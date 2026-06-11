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

Resolution strategy (mirrors the bash/ps1 template clients):

1. **vct-hub first** — the launcher's keychain-backed resolver at
   ``GET /api/v1/projects/{id}/env?key=NAME``. Hub discovery (port +
   token) and the 401 retry-with-rediscovery ride on the same machinery
   as :mod:`vco_lib.project_config` (``$VCT_HUB_PORT`` /
   ``$VCT_HUB_TOKEN`` env → ``<vct_root>/hub.port`` + ``hub.token`` →
   defaults). This is the canonical path for launcher-managed slots
   (``github_pat``, ``openai_api_key``, module secrets).

2. **File-store fallback** — when the hub is unreachable (no launcher
   running) or the project isn't registered, fall back to the Phase-1
   file store at ``$VCT_SECRETS_DIR`` (default ``~/.vct-secrets``):
   ``projects/<NAME>/<key>`` then ``shared/<key>``. Same resolution
   order as ``tools/vct-secrets/vct``. Disable with
   ``allow_file_fallback=False`` when only the keychain truth is
   acceptable.

Secrets NEVER touch argv, logs, or exception messages — errors name the
key, never the value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from vco_lib.project_config import (  # noqa: F401 — re-exported for callers
    HubUnreachable,
    ProjectNotFound,
    ResolverError,
    _get_with_401_retry,
    _resolve_project_id,
)

__all__ = [
    "AccessDenied",
    "HubUnreachable",
    "ProjectNotFound",
    "SecretNotFound",
    "exec_with_secrets",
    "get",
]


class SecretNotFound(ResolverError):
    """The key resolves nowhere (hub + file store both came up empty)."""


class AccessDenied(ResolverError):
    """The hub knows of no ACTIVE binding of this key for this project.

    Hub error code ``key_not_active``: the secret is paused in the
    launcher GUI, or no installed module / shared slot declares it for
    this project. The hub response does not distinguish the two cases
    (live-verified 2026-06-11), so :func:`get` still consults the file
    store on this error — the launcher gate governs keychain-managed
    slots, not the independent ``~/.vct-secrets`` file store. This
    exception surfaces only when the file store ALSO has no copy (or
    fallback is disabled). Fix in the launcher's SecretsPanel, or
    ``vct set``.
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
    """Read a secret value. Hub (keychain) first, file store second.

    Args:
        key: secret key name (e.g. ``"github_pat"``).
        project: project id, registered path, or file-store project
            name. ``None`` → current working directory (hub by-path
            lookup) / shared scope (file store).
        allow_file_fallback: when False, only the hub answer counts —
            a hub miss raises instead of consulting ``~/.vct-secrets``.

    Raises:
        SecretNotFound: key resolves nowhere.
        AccessDenied: the hub has no active binding for the key
            (paused / undeclared — the hub doesn't distinguish) AND the
            file store has no copy either (or fallback is disabled).
        HubUnreachable: hub down AND fallback disabled.
        ProjectNotFound: project unknown AND fallback disabled.
    """
    if not key or not key.strip():
        raise SecretNotFound("empty key")
    key = key.strip()

    hub_error: Optional[ResolverError] = None
    try:
        return _hub_get(key, project)
    except (HubUnreachable, ProjectNotFound, SecretNotFound, AccessDenied) as exc:
        # AccessDenied (key_not_active) intentionally falls through to
        # the file store: the hub can't tell "explicitly paused" from
        # "never declared" (live-verified 2026-06-11), and hard-failing
        # here would strand every user-managed file-store key whenever
        # the launcher is running. The launcher gate governs keychain
        # slots; ~/.vct-secrets is an independent store.
        hub_error = exc

    if allow_file_fallback:
        value = _file_store_get(key, project)
        if value is not None:
            return value

    if isinstance(hub_error, AccessDenied):
        raise hub_error
    if isinstance(hub_error, SecretNotFound) or allow_file_fallback:
        raise SecretNotFound(
            f"secret {key!r} not found (hub: {hub_error}; file store: "
            f"{_secrets_root()} checked={allow_file_fallback}). "
            f"Fix: launcher SecretsPanel, or "
            f"`vct set --project shared --key {key}`"
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
