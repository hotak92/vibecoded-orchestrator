# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read a Claude login from the macOS login Keychain (v0.2.105).

On macOS Claude Code keeps its OAuth login in the login Keychain and writes
``<config dir>/.credentials.json`` only when the Keychain cannot be used. A
reader of the file alone therefore saw no login on a Mac — for the user's own
account and for every added one. :class:`model_router.auth.OAuthReader` falls
back to the item below on macOS only.

Item naming — read from Claude Code 2.1.312's shipped bundle (2026-11-03)
-------------------------------------------------------------------------
``security find-generic-password -a <account> -w -s <service>``, where

* ``service`` = ``"Claude Code" + "-credentials" + suffix``; the suffix is
  empty when ``CLAUDE_CONFIG_DIR`` is not set, else ``"-"`` + the first 8 hex
  digits of ``sha256`` of the config-dir string (Unicode NFC) exactly as
  ``CLAUDE_CONFIG_DIR`` names it (``rwe(lcr)`` with ``lcr="-credentials"``;
  ``OAUTH_FILE_SUFFIX`` is ``""`` for the production OAuth config);
* ``account`` = ``$USER``, else the login name, and ``claude-code-user`` when
  that is not ``[A-Za-z0-9._-]+`` (``Qq()``);
* the secret is the same JSON document as the credentials file
  (``{"claudeAiOauth": {"accessToken": …, "expiresAt": …}}``).

The value is parsed in memory and never logged, printed or stored: callers
log the item's NAME, whether it was found, and its expiry only. ``security``
is run without a shell, with stdin closed and a short timeout: an item that
is missing, a Keychain that is locked (a non-interactive read is refused) or a
``security`` that cannot run all answer "no login here", which the gateway
turns into its loud fallback — never a failed chat.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from typing import Optional

#: The only binary used on a Mac (an absolute path: never a PATH lookup that
#: another program could shadow). Off macOS the reader is never asked; the
#: test suite runs a stand-in found on PATH (see :func:`security_binary`).
SECURITY_BIN = "/usr/bin/security"
#: Must match Claude Code's ``"Claude Code" + OAUTH_FILE_SUFFIX + "-credentials"``.
SERVICE_BASE = "Claude Code-credentials"
#: ``security`` exits with this when the item does not exist (errSecItemNotFound).
EXIT_ITEM_NOT_FOUND = 44
#: Bound on one ``security`` run (Claude Code's own existence probe uses 2 s).
TIMEOUT_S = 3.0
#: How long one Keychain answer is reused (Claude Code caches its own read
#: for 30 s, ``qgi=30000``): the request path asks at most this often.
CACHE_S = 30.0

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

FOUND = "found"
NOT_FOUND = "not_found"
LOCKED = "locked"
UNAVAILABLE = "unavailable"


def is_macos(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform) == "darwin"


def service_name(config_dir: Optional[str]) -> str:
    """The Keychain service Claude Code uses for a login whose
    ``CLAUDE_CONFIG_DIR`` is ``config_dir`` (``None``/empty = not set)."""
    if not config_dir:
        return SERVICE_BASE
    digest = hashlib.sha256(
        unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()
    return f"{SERVICE_BASE}-{digest[:8]}"


def account_name(environ: Optional[dict] = None) -> str:
    """The Keychain account Claude Code uses: ``$USER``, else the login name,
    else (or when not a plain name) ``claude-code-user``."""
    env = os.environ if environ is None else environ
    name = env.get("USER") or ""
    if not name:
        try:
            import getpass

            name = getpass.getuser()
        except Exception:  # noqa: BLE001 — Claude Code falls back the same way
            name = ""
    return name if name and _ACCOUNT_RE.match(name) else "claude-code-user"


def security_binary() -> Optional[str]:
    """``/usr/bin/security`` when it exists (every Mac), else a ``security``
    on PATH (the test suite's stand-in), else ``None``."""
    if os.path.isfile(SECURITY_BIN):
        return SECURITY_BIN
    return shutil.which("security")


@dataclass(frozen=True)
class KeychainLogin:
    """One Keychain answer. ``token``/``expires_at_ms`` only when ``status``
    is :data:`FOUND` and the item held a token; ``detail`` is a log-safe
    reason (never any part of the secret)."""

    status: str
    service: str
    token: Optional[str] = None
    expires_at_ms: int = 0
    detail: str = ""


def _classify_failure(code: int, stderr: str) -> str:
    """Mirrors Claude Code's own classifier (``Qgi``) for the states the
    gateway must tell apart."""
    if code == EXIT_ITEM_NOT_FOUND:
        return NOT_FOUND
    text = (stderr or "").lower()
    if "could not be found" in text or "errsecitemnotfound" in text:
        return NOT_FOUND
    if ("interaction is not allowed" in text or "interactionnotallowed" in text
            or "locked" in text or "unlock" in text):
        return LOCKED
    return UNAVAILABLE


def read_login(config_dir: Optional[str], *, environ: Optional[dict] = None,
               timeout_s: float = TIMEOUT_S) -> KeychainLogin:
    """Read the login Claude Code stored for ``config_dir``. Never raises."""
    service = service_name(config_dir)
    binary = security_binary()
    if binary is None:
        return KeychainLogin(UNAVAILABLE, service, detail="no `security` command")
    argv = [binary, "find-generic-password", "-a", account_name(environ),
            "-w", "-s", service]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return KeychainLogin(LOCKED, service,
                             detail=f"`security` did not answer within {timeout_s:.0f}s")
    except OSError as exc:
        return KeychainLogin(UNAVAILABLE, service,
                             detail=f"`security` could not run ({type(exc).__name__})")
    if proc.returncode != 0:
        status = _classify_failure(proc.returncode, proc.stderr)
        return KeychainLogin(status, service, detail=f"`security` exited {proc.returncode}")
    raw = (proc.stdout or "").strip()
    try:
        doc = json.loads(raw)
        section = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    except ValueError:
        section = None
    if not isinstance(section, dict):
        return KeychainLogin(NOT_FOUND, service, detail="the item holds no Claude login")
    token = section.get("accessToken")
    try:
        expires = int(section.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expires = 0
    if not isinstance(token, str) or not token:
        return KeychainLogin(NOT_FOUND, service, expires_at_ms=expires,
                             detail="the item holds no access token")
    return KeychainLogin(FOUND, service, token=token, expires_at_ms=expires)


__all__ = [
    "CACHE_S", "FOUND", "KeychainLogin", "LOCKED", "NOT_FOUND", "SECURITY_BIN",
    "SERVICE_BASE", "UNAVAILABLE", "account_name", "is_macos", "read_login",
    "security_binary", "service_name",
]
