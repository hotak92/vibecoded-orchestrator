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
``(mtime_ns, size, ctime_ns)``: the steady-state ``/health`` cost is one
``stat``, and a CLI refresh (or a ``chmod``) invalidates the cache. The expiry verdict is
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
its own expiry has not passed AND it was last seen in the file within
``_RECENT_LOGIN_MS`` (five minutes). The token the gateway forwards is one the
file itself carried a moment earlier; a file that stays token-less past that
bound is a login that went away, and its bearer stops being forwarded. A
parse that FAILS never erases a token that was parsed successfully. A file
that states a logout — valid JSON with the OAuth section removed — or that
refuses this process (a permission error) is never read as a rewrite.

Two consequences fall out of that store, and both are promises about the 401
a user or a subagent reads:

* while a recent last-known-good token is unexpired, there is no 401 at all;
* when there is none, a mid-rewrite failure that follows a token seen minutes
  ago (or a file whose own ``expiresAt`` has not passed) is reported as
  TRANSIENT — a CLI is rewriting the login, retry shortly — and never as "run
  ``claude`` to log in", which would send a subagent on an errand that fixes
  nothing. A token that simply EXPIRED is not transient: it gets the login
  message at once, with no wait.

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
import threading
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


#: How a token-less read relates to a credentials file a CLI may be rewriting.
#: Returned by :meth:`OAuthReader._read_file` as its third element.
#:
#: * ``_MID_REWRITE`` — the file is THERE and a re-parse found no usable token
#:   in it (blank token, half-written JSON, a transient read error): the
#:   2026-10-28 rewrite signature. A recently seen last-known-good token may
#:   answer, and the failure is reported as transient.
#: * ``_GAP`` — the file is GONE. A logout, or a CLI that unlinks before it
#:   writes: no token is resurrected from a file that no longer exists, but a
#:   login seen minutes ago still makes the bounded re-read worth one try.
#: * ``_DEFINITE`` — the file states a fact no rewrite produces: the token
#:   expired, the OAuth section was REMOVED (what a logout writes), or the
#:   file's permissions refuse this process. Nothing is served and nothing is
#:   called transient.
_MID_REWRITE = "mid_rewrite"
_GAP = "gap"
_DEFINITE = "definite"


@dataclass(frozen=True)
class _ParsedFile:
    """One parse of the credentials file, keyed on its ``(mtime_ns, size,
    ctime_ns)`` — the ctime so a ``chmod`` (which changes neither of the
    others) is re-checked rather than answered from a parse made while the
    file was still readable.

    Immutable and published with ONE attribute assignment: a reader on another
    thread sees either the previous parse or this one, never a half-updated
    mixture (v0.2.105 fix round — the off-loop :meth:`OAuthReader.
    aread_with_brief_wait` made concurrent readers real, and a stamp stored
    before its parse let one of them reach the "parsed" branch with nothing
    parsed).
    """

    stamp: "tuple[int, int, int]"
    #: ``(token, expires_ms)`` when the parse found a token, else ``None``.
    parsed: "tuple[str, int] | None"
    #: ``(state, message)`` when it did not.
    problem: "tuple[str, str] | None"
    #: The ``expiresAt`` the parse saw, even when it found no token. Kept
    #: beside the problem so the "a login is being rewritten" verdict does not
    #: evaporate on the next (unchanged-file) call — a cold start during a
    #: rewrite has no last-known-good token to fall back on, and that is
    #: exactly when this matters.
    expiry_ms: int
    #: The file is valid JSON WITHOUT an OAuth section — what a logout leaves
    #: behind. A rewrite writes the section (with a blank token, in the
    #: incident), so this one is never read as a rewrite.
    section_removed: bool


class OAuthReader:
    """Cached reader for the Claude CLI credentials file.

    Thread-safe: the request path runs :meth:`read_with_brief_wait` on a worker
    thread while every other caller uses :meth:`read` on the event loop, so
    each :meth:`read` holds :attr:`_lock` for its whole stat-parse-decide
    sequence. The lock is never held across the bounded wait's sleeps, and the
    critical section is one ``stat`` plus, on a change, one small file read.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._file: _ParsedFile | None = None
        #: Success-only store: the last token this reader actually PARSED, so
        #: a rewrite that momentarily empties the file cannot erase it. Only
        #: a successful parse writes here — the same rule as the vendor key
        #: resolver's last-known-good cache, for the same reason.
        self._last_good: tuple[str, int] | None = None
        #: When a good token was last SEEN. Bounds BOTH the last-known-good
        #: service and the "was the login present minutes ago?" half of the
        #: transient verdict to :data:`_RECENT_LOGIN_MS`.
        self._last_good_seen_ms: Optional[int] = None
        #: Edge-trigger for the "serving the last-known-good login" line: one
        #: per transition, not one per request.
        self._last_good_logged = False

    @property
    def path(self) -> Path:
        return self._path

    def read(self, *, now_ms: Optional[int] = None) -> OAuthState:
        """Current OAuth state. One ``stat``; a full read only on change.

        Serves the last-known-good token when a re-parse finds none in a file
        that is mid-rewrite, while that token is unexpired AND was seen within
        :data:`_RECENT_LOGIN_MS`; reports a token-less read that follows a
        good one as TRANSIENT rather than as a logout. An expired token, a
        removed OAuth section and a permission refusal are never transient.
        Never waits: a caller that wants the bounded re-read asks for
        :meth:`read_with_brief_wait` explicitly, and ``/health`` never does.
        """
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            state, detail, kind = self._read_file(now)
            if state.token is not None:
                self._remember_good(state, now)
                return state
            if kind == _MID_REWRITE:
                last_good = self._serve_last_good(now)
                if last_good is not None:
                    return last_good
            if kind != _DEFINITE and (
                state.retry_soon or self._login_seen_recently(now)
            ):
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

    def _read_file(self, now: int) -> "tuple[OAuthState, str, str]":
        """One stat-driven read, plus the phrase describing a failure.

        The phrase is returned rather than re-derived from the message: the
        transient rewrite (below) must describe the SAME failure without
        quoting the login hint. The third element is one of
        :data:`_MID_REWRITE` / :data:`_GAP` / :data:`_DEFINITE` (see there).
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
                _GAP,
            )
        except OSError as exc:
            self._invalidate()
            return self._unreadable(exc)

        stamp = (st.st_mtime_ns, st.st_size, st.st_ctime_ns)
        cached = self._file
        if cached is None or cached.stamp != stamp:
            try:
                cached = self._parse(stamp)
            except PermissionError as exc:
                self._invalidate()
                return self._unreadable(exc)
            self._file = cached

        if cached.problem is not None:
            state, message = cached.problem
            detail = (
                "the credentials file cannot be read as JSON right now"
                if state == "unreadable"
                else "the credentials file has no access token right now"
            )
            return (
                OAuthState(
                    None,
                    cached.expiry_ms,
                    message,
                    state,
                    # A blank token beside an expiry that has NOT passed is
                    # the rewrite signature seen live on 2026-10-28.
                    retry_soon=bool(
                        not cached.section_removed
                        and cached.expiry_ms
                        and now <= cached.expiry_ms
                    ),
                ),
                detail,
                _DEFINITE if cached.section_removed else _MID_REWRITE,
            )
        if cached.parsed is None:  # pragma: no cover — _parse sets one of the two
            raise RuntimeError("credentials parse produced neither a token nor a problem")
        token, expires_ms = cached.parsed
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
                _DEFINITE,
            )
        return OAuthState(token, expires_ms, None, "present"), "", _DEFINITE

    def _parse(self, stamp: "tuple[int, int, int]") -> _ParsedFile:
        """Parse the file once. Raises ``PermissionError`` (a definite
        refusal, never a rewrite); every other read error is a problem."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except PermissionError:
            raise
        except OSError as exc:
            return self._problem(stamp, exc)
        try:
            doc = json.loads(raw)
            section_present = isinstance(doc, dict) and _OAUTH_SECTION in doc
            section = doc.get(_OAUTH_SECTION) or {}
        except (ValueError, AttributeError) as exc:
            return self._problem(stamp, exc)
        if not isinstance(section, dict):
            section = {}
        token = section.get("accessToken") or ""
        expires = section.get("expiresAt") or 0
        try:
            expires_int = int(expires)
        except (TypeError, ValueError):
            expires_int = 0
        if not isinstance(token, str) or not token:
            return _ParsedFile(
                stamp=stamp,
                parsed=None,
                problem=(
                    "absent",
                    (
                        f"the Claude credentials file {self._path} has no "
                        f"access token. {_LOGIN_HINT}"
                    ),
                ),
                expiry_ms=expires_int,
                section_removed=not section_present,
            )
        return _ParsedFile(
            stamp=stamp,
            parsed=(token, expires_int),
            problem=None,
            expiry_ms=expires_int,
            section_removed=False,
        )

    def _problem(self, stamp: "tuple[int, int, int]", exc: BaseException) -> _ParsedFile:
        return _ParsedFile(
            stamp=stamp,
            parsed=None,
            problem=(
                "unreadable",
                (
                    f"the Claude credentials file {self._path} is not "
                    f"readable JSON ({exc}). {_LOGIN_HINT}"
                ),
            ),
            expiry_ms=0,
            section_removed=False,
        )

    def _unreadable(self, exc: OSError) -> "tuple[OAuthState, str, str]":
        """A ``stat``/read error. A PERMISSION refusal is a definite state
        (someone changed the file's mode; no CLI rewrite does that); any other
        OS error may be the rewrite window."""
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
            _DEFINITE if isinstance(exc, PermissionError) else _MID_REWRITE,
        )

    def _remember_good(self, state: OAuthState, now: int) -> None:
        """Record a PARSED token as the last-known-good one."""
        self._last_good = (state.token or "", state.expires_at_ms)
        self._last_good_seen_ms = now
        self._last_good_logged = False

    def _serve_last_good(self, now: int) -> Optional[OAuthState]:
        """The last parsed token, while it is unexpired and RECENT.

        Bounded by :data:`_RECENT_LOGIN_MS` since the token was last seen in
        the file (v0.2.105 fix round): the store bridges a CLI's rewrite
        window — 3 min 20 s in the 2026-10-28 incident — and nothing longer.
        A file that stays token-less past that bound is a login that went
        away (a logout, an account switch), and the gateway must stop
        forwarding the old account's bearer. A cold start during a rewrite
        has no store at all; the file's own unexpired ``expiresAt`` drives
        the transient verdict there. An EXPIRED token is never served.
        """
        good = self._last_good
        if good is None or not self._login_seen_recently(now):
            return None
        token, expires_ms = good
        if not token or (expires_ms and now > expires_ms):
            return None
        if not self._last_good_logged:
            self._last_good_logged = True
            logger.info(
                "model-gateway: the Claude credentials file briefly carries no "
                "usable token (a CLI is rewriting it); serving the "
                "last-known-good login for at most %ds after it was last seen "
                "(expires_at_ms=%s). No token value is logged.",
                _RECENT_LOGIN_MS // 1000,
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
        was already rewriting. Reached only for a file that is mid-rewrite or
        momentarily gone — never for an expired token, a removed OAuth
        section or a permission refusal.
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
        self._file = None


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
