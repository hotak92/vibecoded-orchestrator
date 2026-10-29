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

A briefly EMPTY token is not a logout
-------------------------------------
Live incident 2026-10-28 00:07:06-00:10:26: every first-party request answered
401 ``claude_login_unavailable`` for three minutes. A Claude CLI process had
rewritten the credentials file with an EMPTY ``accessToken`` (the very branch
below that says "has no access token"), and three minutes later a CLI wrote a
fresh token. Nothing was wrong with the login; the gateway simply read the
file during the rewrite window.

So the reader keeps a LAST-KNOWN-GOOD token (the same shape as the vendor
key resolver's serve-stale store, and for the same reason) and serves it while
its own expiry has not passed. That is strictly safer than the alternative:
the token the gateway was about to forward is one the file itself carried a
moment earlier. It also keeps the cache invalidation honest, because a parse
that FAILS never erases a token that was parsed successfully.

Two consequences fall out of that store, and both are promises about the 401
a user or a subagent reads:

* while a last-known-good token is unexpired, there is no 401 at all;
* when there is none, a failure that follows a token seen minutes ago (or a
  file whose own ``expiresAt`` has not passed) is reported as TRANSIENT — a
  CLI is rewriting the login, retry shortly — and never as "run ``claude``
  to log in", which would send a subagent on an errand that fixes nothing.

The one bounded wait (:meth:`OAuthReader.aread_with_brief_wait`) exists for
the same reason and is deliberately NOT used by ``/health``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .fileperms import PermissionHardeningError, restrict_to_owner

logger = logging.getLogger(__name__)

#: Number of random bytes in a generated host token (hex-encoded to 64 chars).
_TOKEN_BYTES = 32

#: How the Claude CLI nests its OAuth material.
_OAUTH_SECTION = "claudeAiOauth"

_LOGIN_HINT = (
    "Run `claude` once in a terminal to log in (or refresh) — the CLI writes "
    "the credentials file this gateway reads."
)

#: How recent a good login has to be for a token-less read to be called
#: TRANSIENT rather than "you never logged in". The 2026-10-28 incident lasted
#: 3 minutes 20 seconds (a CLI rewriting the file); this covers that window
#: with margin, and is short enough that a genuinely logged-out machine is
#: told to log in within five minutes.
_RECENT_LOGIN_MS = 5 * 60 * 1000

#: Total budget of the ONE bounded re-read :meth:`OAuthReader.read_with_brief_wait`
#: may spend while a CLI is mid-rewrite, and the pause between its attempts.
#: Small on purpose: a request is waiting, and a rewrite this gateway has to
#: sit through is a milliseconds-to-low-seconds affair.
_REREAD_BUDGET_S = 2.0
_REREAD_INTERVAL_S = 0.4


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
        retry_soon: the failure is TRANSIENT — a Claude CLI is rewriting the
            credentials file — so the honest answer is "retry in a few
            seconds", not "log in". ``/health`` keeps reporting the machine
            ``state``; this field changes the PROSE and authorises the
            bounded wait, never the verdict.
        from_last_good: the token is one the file carried earlier and has not
            yet expired (the 2026-10-28 rewrite window). Recorded because it
            is a different observation from "the file says so right now".
    """

    token: Optional[str]
    expires_at_ms: int
    problem: Optional[str]
    state: str
    retry_soon: bool = False
    from_last_good: bool = False

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
        #: The ``expiresAt`` the last parse saw, even when that parse found no
        #: token. Cached beside the problem so the "a login is being
        #: rewritten" verdict does not evaporate on the next (unchanged-file)
        #: call — a cold start during a rewrite has no last-known-good token
        #: to fall back on, and that is exactly when this matters.
        self._parse_expiry_ms = 0
        #: Success-only store: the last token this reader actually PARSED, so
        #: a rewrite that momentarily empties the file cannot erase it. Only
        #: a successful parse writes here — the same rule as the vendor key
        #: resolver's last-known-good cache, for the same reason.
        self._last_good: tuple[str, int] | None = None
        #: When a good token was last SEEN. Drives the "was the login present
        #: minutes ago?" half of the transient verdict.
        self._last_good_seen_ms: Optional[int] = None
        #: Edge-trigger for the "serving the last-known-good login" line: one
        #: per transition, not one per request.
        self._last_good_logged = False

    @property
    def path(self) -> Path:
        return self._path

    def read(self, *, now_ms: Optional[int] = None) -> OAuthState:
        """Current OAuth state. One ``stat``; a full read only on change.

        Serves the last-known-good token when a re-parse finds none and that
        token has not expired (the 2026-10-28 rewrite window), and reports a
        token-less read that follows a good one as TRANSIENT rather than as a
        logout. Never waits: a caller that wants the bounded re-read asks for
        :meth:`read_with_brief_wait` explicitly, and ``/health`` never does.
        """
        now = int(time.time() * 1000) if now_ms is None else now_ms
        state, detail, rewritable = self._read_file(now)
        if state.token is not None:
            self._remember_good(state, now)
            return state
        if rewritable:
            last_good = self._serve_last_good(now)
            if last_good is not None:
                return last_good
        if state.retry_soon or self._login_seen_recently(now):
            return self._transient_state(state, detail)
        return state

    def read_with_brief_wait(self, *, now_ms: Optional[int] = None) -> OAuthState:
        """:meth:`read`, plus ONE bounded re-read while a CLI is rewriting.

        Called only on a request path that is about to answer 401, and only
        when the state says the failure is transient. Bounded twice over: it
        is entered only for a transient verdict, and it gives up after
        :data:`_REREAD_BUDGET_S` whatever the file says. Blocking by design —
        :meth:`aread_with_brief_wait` is the one that runs it off the loop.
        """
        state = self.read(now_ms=now_ms)
        if state.token is not None or not state.retry_soon:
            return state
        deadline = time.monotonic() + _REREAD_BUDGET_S
        while time.monotonic() < deadline:
            time.sleep(_REREAD_INTERVAL_S)
            state = self.read(now_ms=now_ms)
            if state.token is not None or not state.retry_soon:
                return state
        return state

    async def aread_with_brief_wait(
        self, *, now_ms: Optional[int] = None,
    ) -> OAuthState:
        """:meth:`read_with_brief_wait` off the event loop.

        ``/health`` and every other reader keep the synchronous :meth:`read`,
        so the liveness route cannot inherit this wait.
        """
        return await asyncio.to_thread(self.read_with_brief_wait, now_ms=now_ms)

    def _read_file(self, now: int) -> "tuple[OAuthState, str, bool]":
        """One stat-driven read, plus the phrase describing a failure.

        The phrase is returned rather than re-derived from the message: the
        transient rewrite (below) must describe the SAME failure without
        quoting the login hint.

        The third element answers "may a last-known-good token answer this?".
        True only when the file is THERE and a re-parse found no usable token
        in it — the 2026-10-28 rewrite signature. A file that is GONE is a
        different observation (a logout, or a
        CLI that unlinks before it writes): the reader keeps reporting it as
        absent rather than resurrecting a token from a file that no longer
        exists, and the bounded re-read covers the millisecond-wide version
        of that gap instead.
        """
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            self._invalidate()
            return (
                OAuthState(
                    None,
                    0,
                    (
                        f"no Claude login found (credentials file {self._path} "
                        f"does not exist). {_LOGIN_HINT}"
                    ),
                    "absent",
                ),
                "the credentials file does not exist right now",
                False,
            )
        except OSError as exc:
            self._invalidate()
            return (
                OAuthState(
                    None,
                    0,
                    (
                        f"cannot read the Claude credentials file {self._path} "
                        f"({exc}). {_LOGIN_HINT}"
                    ),
                    "unreadable",
                ),
                f"the credentials file cannot be read right now ({exc})",
                True,
            )

        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            self._parsed = None
            self._parse_problem = None
            #: The file's own expiry, kept beside a parse problem: a section
            #: that still says "this login runs until T" is evidence of a
            #: login being REWRITTEN, not of a logout, even when the token
            #: field is blank.
            self._parse_expiry_ms = 0
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
                try:
                    expires_int = int(expires)
                except (TypeError, ValueError):
                    expires_int = 0
                if not isinstance(token, str) or not token:
                    self._parse_expiry_ms = expires_int
                    self._parse_problem = (
                        "absent",
                        (
                            f"the Claude credentials file {self._path} has no "
                            f"access token. {_LOGIN_HINT}"
                        ),
                    )
                else:
                    self._parsed = (token, expires_int)

        if self._parse_problem is not None:
            state, message = self._parse_problem
            detail = (
                "the credentials file cannot be read as JSON right now"
                if state == "unreadable"
                else "the credentials file has no access token right now"
            )
            return (
                OAuthState(
                    None,
                    self._parse_expiry_ms,
                    message,
                    state,
                    # A blank token beside an expiry that has NOT passed is
                    # the rewrite signature seen live on 2026-10-28.
                    retry_soon=bool(
                        self._parse_expiry_ms and now <= self._parse_expiry_ms
                    ),
                ),
                detail,
                True,
            )
        assert self._parsed is not None  # noqa: S101 — invariant of the branch above
        token, expires_ms = self._parsed
        if expires_ms and now > expires_ms:
            return (
                OAuthState(
                    None,
                    expires_ms,
                    (
                        "your Claude login token has expired. " + _LOGIN_HINT
                    ),
                    "expired",
                ),
                "the login token in the credentials file has expired",
                False,
            )
        return OAuthState(token, expires_ms, None, "present"), "", False

    def _remember_good(self, state: OAuthState, now: int) -> None:
        """Record a PARSED token as the last-known-good one."""
        self._last_good = (state.token or "", state.expires_at_ms)
        self._last_good_seen_ms = now
        self._last_good_logged = False

    def _serve_last_good(self, now: int) -> Optional[OAuthState]:
        """The last parsed token, while its own expiry has not passed.

        Deliberately NOT limited to "seen within the last few minutes": a
        token the file carried is a real token until it expires, and answering
        401 with an unexpired one is the failure this store exists to prevent.
        An EXPIRED last-good token is never served.
        """
        good = self._last_good
        if good is None:
            return None
        token, expires_ms = good
        if not token or (expires_ms and now > expires_ms):
            return None
        if not self._last_good_logged:
            self._last_good_logged = True
            logger.info(
                "model-gateway: the Claude credentials file briefly carries no "
                "usable token (a CLI is rewriting it); serving the "
                "last-known-good login until it expires (expires_at_ms=%s). "
                "No token value is logged.",
                expires_ms or "unset",
            )
        return OAuthState(
            token, expires_ms, None, "present", from_last_good=True,
        )

    def _login_seen_recently(self, now: int) -> bool:
        seen = self._last_good_seen_ms
        return seen is not None and 0 <= now - seen <= _RECENT_LOGIN_MS

    def _transient_state(self, state: OAuthState, detail: str) -> OAuthState:
        """The TRANSIENT spelling of a token-less read.

        Never names a login command: the 2026-10-28 defect was that a
        subagent, mid-task, was told to go run ``claude`` for a file a CLI
        was already rewriting.
        """
        return OAuthState(
            None,
            state.expires_at_ms,
            (
                f"the Claude login is momentarily unavailable for this "
                f"request: {detail}. A Claude CLI is rewriting the credentials "
                f"file this gateway reads, and the login has NOT lapsed. This "
                f"is TRANSIENT — retry in a few seconds; there is nothing to "
                f"fix."
            ),
            state.state,
            retry_soon=True,
        )

    def _invalidate(self) -> None:
        """Forget the CACHED PARSE. Never the last-known-good token.

        The distinction is the whole fix: the file disappearing for a
        millisecond is exactly the shape a CLI's write produces, and dropping
        a token the login still owns because of it is what produced the
        2026-10-28 401s.
        """
        self._stamp = None
        self._parsed = None
        self._parse_problem = None
        self._parse_expiry_ms = 0


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


def host_token_stamp(path: Path) -> "dict[str, object] | None":
    """The identifying stamp of a host-token file: path, ``mtime_ns``, size.

    Issue 10's diagnostic. The daemon samples this ONCE at startup, right
    after ``ensure_host_token`` (see ``model_router.__main__._serve``), and
    ``/health`` later shows it beside a fresh stamp of the same path. The
    pair distinguishes the three ways a client with a valid-looking token
    can still 401 against a live daemon:

    * the LOADED path differs from the file the client read — starter and
      client resolved different ``VCT_STATE_DIR``s;
    * same path, different ``(mtime_ns, size)`` — the file was deleted and
      regenerated AFTER this daemon started, so the daemon authorises with
      a token no file on disk holds any more (remedy: restart the gateway);
    * same stamp — the client presented wrong content.

    ``None`` when the file cannot be stated (deleted, unreadable): the
    caller reports that as its own answer rather than guessing.

    The stamp is the ``(mtime_ns, size)`` pair the in-module precedent
    (:class:`OAuthReader`) already trusts to detect a rewritten file, and it
    deliberately contains NO content — a stamp that hashed the token would
    put a credential-adjacent value into ``/health``.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {
        "path": str(path),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


__all__ = [
    "OAuthReader",
    "OAuthState",
    "ensure_host_token",
    "host_token_stamp",
    "read_host_token",
    "token_matches",
]
