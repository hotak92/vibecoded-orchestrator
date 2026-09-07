# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Two credentials, two very different lifecycles.

**The local host token** authorises clients to use the gateway at all. It is
generated once, stored owner-only under the state root, and is the ONLY secret
that ever reaches a VS Code settings file — no vendor key and no OAuth token
leaves this process. Compared in constant time.

**The Claude OAuth token** is read from the Claude CLI's own credentials file,
per request. Not cached across writes on purpose: the CLI rewrites that file
whenever it refreshes, and a gateway holding a stale copy would 401 a user who
had just refreshed. The file is HARNESS-OWNED — this module opens it read-only
and never writes, copies or logs its contents.

Freshness without blocking ``/health``
--------------------------------------
``/health`` must answer while a vendor upstream is unreachable and while a
secret resolver is wedged, so it does no subprocess, no secret resolution and
no network call. It does need to say whether a Claude login is present, so
:class:`OAuthReader` caches the PARSED credentials keyed on the file's
``(mtime_ns, size)``: the steady-state ``/health`` cost is one ``stat``, and a
CLI refresh invalidates the cache by changing both. The expiry verdict is
recomputed on every call from the cached expiry, so a token that lapses while
the file is untouched is still reported as expired.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .fileperms import PermissionHardeningError, restrict_to_owner

#: Number of random bytes in a generated host token (hex-encoded to 64 chars).
_TOKEN_BYTES = 32

#: How the Claude CLI nests its OAuth material.
_OAUTH_SECTION = "claudeAiOauth"

_LOGIN_HINT = (
    "Run `claude` once in a terminal to log in (or refresh) — the CLI writes "
    "the credentials file this gateway reads."
)


@dataclass(frozen=True)
class OAuthState:
    """The Claude login, as it stands right now.

    Attributes:
        token: the bearer to forward, or ``None`` when unusable.
        expires_at_ms: epoch milliseconds from the credentials file; ``0``
            when the file does not carry one.
        problem: ``None`` when usable, else an ACTIONABLE message naming what
            the user must do. Never contains any part of a token.
        state: ``present`` / ``expired`` / ``absent`` / ``unreadable`` —
            surfaced in ``/health`` so a GUI can distinguish "you never logged
            in" from "your login lapsed" without parsing prose.
    """

    token: Optional[str]
    expires_at_ms: int
    problem: Optional[str]
    state: str

    @property
    def present(self) -> bool:
        return self.token is not None


class OAuthReader:
    """Cached reader for the Claude CLI credentials file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: tuple[int, int] | None = None
        self._parsed: tuple[str, int] | None = None
        self._parse_problem: tuple[str, str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def read(self, *, now_ms: Optional[int] = None) -> OAuthState:
        """Current OAuth state. One ``stat``; a full read only on change."""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            self._invalidate()
            return OAuthState(
                None,
                0,
                (
                    f"no Claude login found (credentials file {self._path} does "
                    f"not exist). {_LOGIN_HINT}"
                ),
                "absent",
            )
        except OSError as exc:
            self._invalidate()
            return OAuthState(
                None,
                0,
                (
                    f"cannot read the Claude credentials file {self._path} "
                    f"({exc}). {_LOGIN_HINT}"
                ),
                "unreadable",
            )

        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            self._parsed = None
            self._parse_problem = None
            try:
                raw = self._path.read_text(encoding="utf-8")
                section = json.loads(raw).get(_OAUTH_SECTION) or {}
            except (OSError, ValueError, AttributeError) as exc:
                self._parse_problem = (
                    "unreadable",
                    (
                        f"the Claude credentials file {self._path} is not "
                        f"readable JSON ({exc}). {_LOGIN_HINT}"
                    ),
                )
            else:
                token = section.get("accessToken") or ""
                expires = section.get("expiresAt") or 0
                if not isinstance(token, str) or not token:
                    self._parse_problem = (
                        "absent",
                        (
                            f"the Claude credentials file {self._path} has no "
                            f"access token. {_LOGIN_HINT}"
                        ),
                    )
                else:
                    try:
                        expires_int = int(expires)
                    except (TypeError, ValueError):
                        expires_int = 0
                    self._parsed = (token, expires_int)

        if self._parse_problem is not None:
            state, message = self._parse_problem
            return OAuthState(None, 0, message, state)
        assert self._parsed is not None  # noqa: S101 — invariant of the branch above
        token, expires_ms = self._parsed
        if expires_ms and now > expires_ms:
            return OAuthState(
                None,
                expires_ms,
                (
                    "your Claude login token has expired. " + _LOGIN_HINT
                ),
                "expired",
            )
        return OAuthState(token, expires_ms, None, "present")

    def _invalidate(self) -> None:
        self._stamp = None
        self._parsed = None
        self._parse_problem = None


def ensure_host_token(path: Path) -> str:
    """Return the host token, generating an owner-only file on first use.

    Raises:
        PermissionHardeningError: the file could not be made owner-only. The
            token is NOT returned in that case — serving with a
            world-readable token file would hand any local user the ability
            to proxy under this user's Claude login and vendor subscription.
    """
    existing = read_host_token(path)
    if existing:
        # An existing file may predate a permissions fix, or have been
        # rewritten by an editor. Re-assert; loud on failure.
        restrict_to_owner(path)
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(_TOKEN_BYTES)
    # Create with owner-only mode in the SAME syscall so there is no window
    # where the file exists with default permissions. On Windows the mode is
    # ignored, which is exactly why restrict_to_owner runs immediately after.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        restrict_to_owner(path)
    except PermissionHardeningError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return token


def read_host_token(path: Path) -> str:
    """Read an existing host token, or ``""`` when there is none."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def token_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison. Empty ``expected`` never matches."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


__all__ = [
    "OAuthReader",
    "OAuthState",
    "ensure_host_token",
    "read_host_token",
    "token_matches",
]
