# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which Claude login serves a first-party request (v0.2.105).

The registry (ids, labels and Claude Code configuration directories — never a
credential) is owned by :mod:`vco_lib.gateway_accounts`; the launcher exports
it to ``<vct_root>/model-gateway/accounts.json`` and this module reads that
file, mtime-cached, through the SAME parser.

Selection, per request:

* the request carries ``x-vco-account: <id>`` (a project sets it through
  ``ANTHROPIC_CUSTOM_HEADERS``) → that account;
* no header → the registry's default account (``primary`` unless the user
  chose another);
* ``primary`` is the login the gateway has always used. With no registry file
  every request is served exactly as before.

When the selected account cannot be used — unknown id, malformed header,
registry unreadable, not logged in, login expired, refresh failed — the
request is NEVER failed (owner ruling 2026-11-03). It is served by the
fallback account (the default when that is a different, usable account, else
``primary``) and the fallback is made LOUD three ways:

1. a WARNING in the gateway log (once per account and reason, then every
   :data:`WARN_REPEAT_S` while it lasts) and a per-request ``account_fallback``
   field on the access line and the ledger row;
2. ``/health`` ``accounts`` — per-account state, fallback count, last reason —
   which the launcher shows per account and per project;
3. the MODEL is told: one short notice is appended at the END of the last user
   turn of the first eligible request of each conversation served by a
   fallback, and never again in that conversation (:class:`NoticeLedger`).

How "first request of a conversation" is recognised
---------------------------------------------------
Evidence (probe P1, 2026-11-03, claude 2.1.312, captured on a loopback
listener): every request carries the header ``x-claude-code-session-id``
(also inside ``metadata.user_id``). The session id alone is not a
conversation: a session's subagents, title and summary calls share it. So the
key is ``(session id, digest of the FIRST message of the request)``: the first
message of a conversation never changes as the conversation grows, and each
subagent starts its own. A request is ELIGIBLE for the notice only when it is
a ``/v1/messages`` call (not ``count_tokens``) whose body was parsed, carries
tools (side calls — titles, quota probes — carry none) and ends with a user
turn; an ineligible request leaves the notice pending for the next eligible
one. Compaction rewrites the first message, so a compacted conversation is a
new key and is told once more — which is right, because the summary dropped
the earlier notice. A new fallback EPISODE (the account recovered, then failed
again) is a new key too.

The notice is appended as the LAST content block of the last message, so
every byte the prompt cache keys on (tools, system, all earlier messages and
the earlier blocks of the last one) is unchanged; the client does not store
it, so the next request is byte-identical to what it would have been.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from vco_lib.gateway_accounts import (
    PRIMARY_ID,
    SELECTOR_HEADER,
    AccountsError,
    Registry,
    login_commands,
    parse_registry,
    valid_id,
)

from .auth import OAuthReader, OAuthState

logger = logging.getLogger(__name__)

#: A persisting fallback re-WARNs this often (the first one is immediate).
WARN_REPEAT_S = 600.0
#: How many conversations the notice ledger remembers (FIFO). A conversation
#: evicted from it would be told again — bounded memory beats a perfect
#: memory of conversations nobody is still having.
NOTICE_MEMORY = 4096

# Fallback reasons (machine words; the access line, the ledger and /health).
REASON_NOT_REGISTERED = "not_registered"
REASON_MALFORMED = "malformed_header"
REASON_REGISTRY_UNREADABLE = "registry_unreadable"


@dataclass(frozen=True)
class AccountChoice:
    """The answer for one request."""

    #: The account the request asked for (header, else the default). For a
    #: malformed header this is a log-safe rendering of what was sent.
    requested: str
    #: ``header`` or ``default``.
    source: str
    #: The account whose login serves the request.
    served: str
    #: ``None`` when ``served == requested``; else why it fell back (a reason
    #: word above, or the login state: ``absent``/``expired``/``unreadable``).
    fallback_reason: Optional[str] = None

    @property
    def fell_back(self) -> bool:
        return self.fallback_reason is not None

    def access_fields(self) -> str:
        parts = [f"account={self.served}", f"account_source={self.source}"]
        if self.fell_back:
            parts.append(f"account_requested={self.requested}")
            parts.append(f"account_fallback={self.fallback_reason}")
        return " ".join(parts)


class RegistryLoader:
    """``accounts.json``, re-parsed only when its stat stamp changes. Keeps
    the last good registry over a corrupt rewrite and says so."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: Optional[tuple[int, int]] = None
        self._registry = Registry()
        self._loaded_once = False
        self.error: Optional[str] = None
        self.source = "absent"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> Registry:
        with self._lock:
            try:
                st = os.stat(self._path)
            except FileNotFoundError:
                self._stamp = None
                self._registry = Registry()
                self.source = "absent"
                self.error = None
                return self._registry
            except OSError as exc:
                self.error = f"cannot stat {self._path}: {exc.strerror or exc}"
                return self._registry
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp == self._stamp:
                return self._registry
            self._stamp = stamp
            try:
                with open(self._path, encoding="utf-8") as fh:
                    reg = parse_registry(json.load(fh))
            except (OSError, ValueError, AccountsError) as exc:
                self.error = f"{self._path} could not be read: {exc}"
                logger.warning(
                    "model-gateway: account registry %s is unreadable (%s); %s",
                    self._path, exc,
                    "keeping the last good copy" if self._loaded_once
                    else "serving every request with the primary account",
                )
                return self._registry
            self._registry = reg
            self._loaded_once = True
            self.source = "file"
            self.error = None
            return reg

    def ever_loaded(self) -> bool:
        return self._loaded_once


@dataclass
class _AccountStats:
    fallbacks: int = 0
    last_fallback_at: Optional[float] = None
    last_fallback_reason: Optional[str] = None
    last_warned: dict[str, float] = field(default_factory=dict)
    #: Bumped each time the account goes from usable to unusable.
    episode: int = 0
    was_unusable: bool = False


class NoticeLedger:
    """Which conversations have already been told about a fallback."""

    def __init__(self, capacity: int = NOTICE_MEMORY) -> None:
        self._seen: "OrderedDict[tuple, None]" = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def claim(self, key: tuple) -> bool:
        """``True`` exactly once per key: the caller injects the notice."""
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True

    def release(self, key: tuple) -> None:
        """Undo a claim whose injection could not be applied."""
        with self._lock:
            self._seen.pop(key, None)


def conversation_key(session: Optional[str], payload: Mapping[str, Any]) -> Optional[str]:
    """``<session>:<digest of the first message>`` or ``None`` when the body
    has no first message to recognise the conversation by."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    try:
        first = json.dumps(messages[0], sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    digest = hashlib.sha256(first.encode("utf-8")).hexdigest()[:24]
    return f"{session or '-'}:{digest}"


def notice_eligible(payload: Mapping[str, Any]) -> bool:
    """A conversation turn the model will act on: it carries tools and ends
    with a user message. Title / quota side calls carry no tools."""
    tools = payload.get("tools")
    messages = payload.get("messages")
    if not isinstance(tools, list) or not tools:
        return False
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "user"


def append_notice(payload: Mapping[str, Any], notice: str) -> dict:
    """A copy of ``payload`` with ``notice`` appended as the LAST content of
    the LAST user message. Everything before it is left exactly as it was
    (the prompt-cache prefix). The input is not modified."""
    out = dict(payload)
    messages = list(payload["messages"])
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content},
            {"type": "text", "text": notice},
        ] if content else [{"type": "text", "text": notice}]
    elif isinstance(content, list):
        last["content"] = list(content) + [{"type": "text", "text": notice}]
    else:
        raise ValueError("the last message has no content to append to")
    messages[-1] = last
    out["messages"] = messages
    return out


def notice_text(*, requested: str, requested_label: str, served: str,
                served_label: str, reason: str, config_dir: Optional[str]) -> str:
    why = {
        REASON_NOT_REGISTERED: "it is not registered on this machine",
        REASON_MALFORMED: "the project's account setting is malformed",
        REASON_REGISTRY_UNREADABLE: "the account list could not be read",
        "absent": "it is not logged in",
        "expired": "its login has expired",
        "unreadable": "its login file cannot be read",
    }.get(reason, f"its login is unusable ({reason})")
    if config_dir:
        fix = (f"To fix it, the user runs `{login_commands(config_dir)['posix']}` "
               "in a terminal; the next request then uses that account again.")
    else:
        fix = ("To fix it, the user picks a registered account in the VCO launcher "
               "(Project -> Identity -> Claude account).")
    return (
        f"[VCO model gateway notice] This project asked for the Claude account "
        f"'{requested}' ({requested_label}), but {why}. This conversation is "
        f"therefore being served by, and billed to, the account '{served}' "
        f"({served_label}). {fix} Mention this to the user once, briefly; "
        f"you will not see this notice again in this conversation."
    )


ReaderFactory = Callable[[Path], OAuthReader]


class AccountService:
    """The registry, one credentials reader per account, the fallback rule
    and the loud side of it. One per gateway."""

    def __init__(
        self,
        primary_reader: OAuthReader,
        registry_path: Path,
        *,
        reader_factory: ReaderFactory = OAuthReader,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.primary_reader = primary_reader
        self.loader = RegistryLoader(registry_path)
        self._factory = reader_factory
        self._readers: dict[tuple[str, str], OAuthReader] = {}
        self._clock = clock
        self._stats: dict[str, _AccountStats] = {}
        self.notices = NoticeLedger()
        self._lock = threading.Lock()

    # ── lookup ───────────────────────────────────────────────────────────
    def registry(self) -> Registry:
        return self.loader.current()

    def reader(self, account_id: str, reg: Optional[Registry] = None) -> Optional[OAuthReader]:
        if account_id == PRIMARY_ID:
            return self.primary_reader
        reg = reg or self.registry()
        acct = reg.get(account_id)
        if acct is None:
            return None
        key = (acct.id, acct.config_dir)
        with self._lock:
            found = self._readers.get(key)
            if found is None:
                found = self._factory(acct.credentials_file())
                self._readers[key] = found
            return found

    def _stats_for(self, account_id: str) -> _AccountStats:
        with self._lock:
            return self._stats.setdefault(account_id, _AccountStats())

    # ── selection ────────────────────────────────────────────────────────
    def requested(self, headers: Mapping[str, str]) -> tuple[str, str, Optional[str]]:
        """``(requested id, source, early fallback reason)`` from the headers
        and the registry, before any login is read."""
        reg = self.registry()
        raw = headers.get(SELECTOR_HEADER)
        if raw is None or not raw.strip():
            if reg.default == PRIMARY_ID or reg.get(reg.default) is not None:
                return reg.default, "default", None
            return reg.default, "default", REASON_NOT_REGISTERED
        value = raw.strip()
        if not valid_id(value):
            return repr(value[:40]), "header", REASON_MALFORMED
        if value == PRIMARY_ID or reg.get(value) is not None:
            return value, "header", None
        if self.loader.error and not self.loader.ever_loaded():
            return value, "header", REASON_REGISTRY_UNREADABLE
        return value, "header", REASON_NOT_REGISTERED

    def fallback_for(self, requested: str) -> str:
        """The default when it is a different, usable account; else primary."""
        reg = self.registry()
        default = reg.default
        if default not in (requested, PRIMARY_ID):
            reader = self.reader(default, reg)
            if reader is not None and reader.read().present:
                return default
        return PRIMARY_ID

    def note_usable(self, account_id: str) -> None:
        self._stats_for(account_id).was_unusable = False

    def note_fallback(self, choice: AccountChoice) -> int:
        """Record a fallback, WARN when due; returns the account's episode."""
        stats = self._stats_for(choice.requested)
        now = self._clock()
        reason = choice.fallback_reason or "-"
        with self._lock:
            if not stats.was_unusable:
                stats.episode += 1
                stats.was_unusable = True
            stats.fallbacks += 1
            stats.last_fallback_at = now
            stats.last_fallback_reason = reason
            last = stats.last_warned.get(reason)
            due = last is None or now - last >= WARN_REPEAT_S
            if due:
                stats.last_warned[reason] = now
            episode = stats.episode
        if due:
            reg = self.registry()
            acct = reg.get(choice.requested)
            hint = (f" Log it in: {login_commands(acct.config_dir)['posix']}"
                    if acct is not None else
                    " Pick a registered account in the launcher (Project -> Identity -> Claude account).")
            logger.warning(
                "model-gateway: Claude account %s is unusable (%s); serving with "
                "account %s instead — the work is billed to %s.%s",
                choice.requested, reason, choice.served, choice.served, hint,
            )
        return episode

    def notice_for(self, choice: AccountChoice) -> str:
        reg = self.registry()
        acct = reg.get(choice.requested)
        return notice_text(
            requested=choice.requested,
            requested_label=reg.label_of(choice.requested),
            served=choice.served,
            served_label=reg.label_of(choice.served),
            reason=choice.fallback_reason or "-",
            config_dir=acct.config_dir if acct is not None else None,
        )

    # ── /health ──────────────────────────────────────────────────────────
    def health(self, refresh_status: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict:
        reg = self.registry()
        rows = []
        for acct_id in reg.ids():
            reader = self.reader(acct_id, reg)
            state: Optional[OAuthState] = reader.read() if reader is not None else None
            acct = reg.get(acct_id)
            stats = self._stats.get(acct_id) or _AccountStats()
            expires_in = (
                int(state.expires_at_ms / 1000 - time.time())
                if state is not None and state.expires_at_ms else None
            )
            rows.append({
                "id": acct_id,
                "label": reg.label_of(acct_id),
                "config_dir": acct.config_dir if acct is not None
                else str(self.primary_reader.path.parent),
                "is_default": acct_id == reg.default,
                "oauth_state": state.state if state is not None else "absent",
                "oauth_expires_in_s": expires_in,
                "usable": bool(state is not None and state.present),
                "fallbacks": stats.fallbacks,
                "last_fallback_at": stats.last_fallback_at,
                "last_fallback_reason": stats.last_fallback_reason,
                "refresh": dict((refresh_status or {}).get(acct_id) or {}) or None,
            })
        return {
            "default": reg.default,
            "selector_header": SELECTOR_HEADER,
            "registry": {
                "path": str(self.loader.path),
                "source": self.loader.source,
                "error": self.loader.error,
            },
            "accounts": rows,
        }


__all__ = [
    "AccountChoice",
    "AccountService",
    "NoticeLedger",
    "RegistryLoader",
    "append_notice",
    "conversation_key",
    "notice_eligible",
    "notice_text",
]
