# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V47-C (v0.2.46 Part 2 Gap C): detect secret-shaped keys in a project's
``.env`` file and offer to migrate them to the OS keychain.

This is the **pure-function** half of Gap C. The interactive prompt + hub
HTTP call live in ``install.py`` so they can use the existing logging,
TTY-detection, and ``DeferralReport`` plumbing. Everything in this module
is side-effect-light and unit-testable without standing up a launcher.

Why a separate module
~~~~~~~~~~~~~~~~~~~~~

``install.py`` is already ~18 kLOC. Adding ~250 lines of env-parsing,
sentinel-rewriting, and atomic-write logic inline would push it over
critical mass. The split mirrors how ``vco_lib/deferral_report.py``
factored out the deferral writer two cycles ago.

What this module does NOT do
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* No keychain writes. The keychain is the launcher's authoritative store;
  Python code talks to it via the vct-hub HTTP endpoint
  ``POST /api/v1/secrets/migrate`` (added in V47-C alongside this module).
* No TTY prompts. The caller decides whether to prompt (interactive
  install) or default to keep-in-env (``--yes`` / CI).
* No deferral writes. The caller passes the audit result into the
  existing ``DeferralReport`` plumbing.

Public API
~~~~~~~~~~

* :func:`is_secret_shaped_env_key` — same predicate as
  ``install.py::_is_secret_shaped_env_key`` (matches ``TOKEN``, ``SECRET``,
  ``PAT``, ``PASSWORD``, ``PASS``, ``AUTH``, and trailing ``_KEY``).
* :func:`audit_env_secrets` — parse a ``.env`` file, return list of
  ``(key, value)`` pairs that look like credentials and carry real values.
* :func:`rewrite_env_with_sentinels` — replace migrated values with the
  ``__vco_keychain__`` sentinel via atomic write.
* :func:`harden_env_perms` — on Linux/macOS, ``chmod 0o600`` if perms are
  too loose. Windows no-op.

Sentinel value
~~~~~~~~~~~~~~

The sentinel ``__vco_keychain__`` signals to downstream tooling that this
key has been migrated to the keychain. The bundled secrets-resolver
helper (``templates/scripts/vct_secrets_resolve.sh``) treats it as
"unresolved — go ask the hub". Anything else (empty, literal, ``changeme``)
is treated as "not migrated".

The sentinel is bracketed with double underscores so it can never collide
with a legitimate value (env values starting with ``__`` are excluded by
convention; legitimate API keys, tokens, and passwords never have that
exact shape).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Tuple

# Mirror install.py::_SECRET_SHAPED_SUBSTRINGS. Substring matches inside
# ``[_\\-]``-delimited segments — avoids false positives like ``PYTHONPATH``
# matching ``PAT`` or ``COMPASS`` matching ``PASS``.
_SECRET_SHAPED_SUBSTRINGS: Tuple[str, ...] = (
    "TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH",
)

#: Sentinel value written to ``.env`` after a key migrates to the
#: keychain. Downstream resolvers treat it as "unresolved — ask the hub".
#: Bracketed with double underscores so it can never collide with a
#: legitimate API-key, token, or password value.
KEYCHAIN_SENTINEL: str = "__vco_keychain__"

#: Placeholder values that are NOT real secrets — installers / templates
#: write these as documentation hints, the user is expected to overwrite
#: them with the actual value. Matching is case-insensitive and stripped
#: of surrounding whitespace + quotes.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "",
    "changeme",
    "change-me",
    "your-api-key-here",
    "your_api_key_here",
    "<your-api-key>",
    "<your_api_key>",
    "<your-key>",
    "<your-secret>",
    "<your-token>",
    "<your-password>",
    "<placeholder>",
    "placeholder",
    "todo",
    "fixme",
    "xxx",
    "yyy",
    "...",
    KEYCHAIN_SENTINEL,  # already migrated → skip on re-runs
})


def is_secret_shaped_env_key(key: str) -> bool:
    """Return True iff ``key`` looks like a credential.

    Mirrors ``install.py::_is_secret_shaped_env_key`` exactly. Kept in
    sync as a pure copy because (a) install.py owns the canonical
    implementation, (b) this module is imported by audit/test paths
    that should not transitively import the install.py module-level
    state (~18 kLOC + an argparse parser).

    Matches secret substrings as TOKENS within ``[_\\-]``-delimited
    env-key parts. Avoids false positives like ``PYTHONPATH`` matching
    ``PAT`` or ``COMPASS`` matching ``PASS``.
    """
    upper = key.upper()
    parts = re.split(r"[_\-]+", upper)
    for needle in _SECRET_SHAPED_SUBSTRINGS:
        if needle in parts:
            return True
    if upper == "KEY" or upper.endswith("_KEY"):
        return True
    return False


@dataclass(frozen=True)
class EnvSecret:
    """One ``(key, value)`` pair from ``.env`` that looks like a credential.

    The ``value`` field is intentionally NOT scrubbed — caller code in
    install.py decides whether to forward it to the hub or discard it.
    Tests assert on the value to confirm the parser behaviour.
    """

    key: str
    """Env var name, e.g. ``GITHUB_TOKEN``."""

    value: str
    """Value as parsed (quotes stripped, whitespace trimmed)."""

    line_number: int
    """1-based line number in the source ``.env``. Used by the rewriter to
    locate the line to replace."""


def _strip_inline_quotes(raw: str) -> str:
    """Strip a single pair of matching surrounding quotes from a value.

    ``KEY="foo"`` → ``foo``;  ``KEY='bar'`` → ``bar``;  ``KEY=baz`` → ``baz``.
    Does NOT interpret escape sequences — ``.env`` semantics are intentionally
    minimal here, matching ``register_secret_from_source`` in
    ``commands/secrets_import.rs``.
    """
    val = raw.strip()
    if len(val) >= 2:
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            return val[1:-1]
    return val


def _is_placeholder_value(value: str) -> bool:
    """True if ``value`` looks like an installer-written placeholder.

    Case-insensitive; stripped of surrounding whitespace + quotes.
    Placeholders should NOT be migrated (the user hasn't filled them in
    yet; migrating the literal text ``<your-api-key>`` to the keychain
    would be confusing and useless).
    """
    val = _strip_inline_quotes(value).strip().lower()
    return val in _PLACEHOLDER_VALUES


def audit_env_secrets(env_path: Path) -> List[EnvSecret]:
    """Parse ``.env`` and return entries whose key is secret-shaped AND
    whose value is non-placeholder.

    Returns an empty list when the file does not exist, is unreadable,
    or contains zero qualifying entries. Soft-fail by design — the
    caller treats "no candidates" as "no migration to offer".

    Comments (``#`` at start of trimmed line) are skipped. ``export KEY=val``
    is supported. Lines without an ``=`` are skipped (not flagged as malformed
    — ``.env`` files in the wild often contain non-KEY=VALUE content).
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []

    out: List[EnvSecret] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Support `export KEY=val` (POSIX shell idiom often found in .env).
        body = line[len("export "):].lstrip() if line.startswith("export ") else line
        eq = body.find("=")
        if eq <= 0:  # no `=`, or empty key
            continue
        key = body[:eq].strip()
        if not key or not is_secret_shaped_env_key(key):
            continue
        raw_value = body[eq + 1:]
        # Strip inline comment after an unquoted value:  KEY=val  # note
        # Quoted values may contain `#`; only trim post-value for unquoted.
        stripped = raw_value.strip()
        if not (stripped.startswith('"') or stripped.startswith("'")):
            hash_pos = stripped.find("#")
            if hash_pos >= 0:
                stripped = stripped[:hash_pos].rstrip()
        value = _strip_inline_quotes(stripped)
        if _is_placeholder_value(value):
            continue
        out.append(EnvSecret(key=key, value=value, line_number=lineno))
    return out


def rewrite_env_with_sentinels(
    env_path: Path,
    migrated_keys: Sequence[str],
) -> Tuple[int, List[str]]:
    """Replace migrated keys' values in ``.env`` with :data:`KEYCHAIN_SENTINEL`.

    Returns ``(num_replaced, missed)`` where ``missed`` is the list of
    keys that were not found in the file (defensive logging only — the
    caller usually trusts the audit step's output, but if the user edited
    the .env between audit and rewrite a key may have moved).

    Atomic-write semantics: a temp file is written in the same directory
    then ``os.replace()``'d into place. File mode is preserved across the
    swap (the temp file inherits ``env_path``'s mode pre-replace via an
    explicit ``shutil.copystat``).

    Each replaced line keeps its original key + ``export`` prefix (if
    any) + trailing comment (if any). Only the value bytes change:

    .. code-block:: text

       # before
       export OPENAI_API_KEY="sk-abc123"  # team key

       # after (with KEYCHAIN_SENTINEL = "__vco_keychain__")
       export OPENAI_API_KEY=__vco_keychain__  # team key

    Comments preserved; ``export`` prefix preserved; quotes stripped (the
    sentinel doesn't need quoting and quotes-around-sentinel would just
    add noise).
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"cannot read {env_path}: {exc}") from exc

    keyset = set(migrated_keys)
    seen: set[str] = set()
    out_lines: List[str] = []
    replaced_count = 0

    for raw in text.splitlines(keepends=False):
        line = raw
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        # Preserve leading whitespace + `export` prefix; identify the key.
        leading_ws_len = len(line) - len(stripped)
        leading_ws = line[:leading_ws_len]
        body = stripped
        export_prefix = ""
        if body.startswith("export "):
            export_prefix = "export "
            body = body[len("export "):].lstrip()
            # Re-measure leading_ws to include the gap absorbed by lstrip
            # (rare, but happens with `export   KEY=val`).
            export_prefix = "export "
        eq = body.find("=")
        if eq <= 0:
            out_lines.append(line)
            continue
        key = body[:eq].strip()
        if key not in keyset:
            out_lines.append(line)
            continue
        if key in seen:
            # Duplicate key — preserve as-is (user has a malformed .env;
            # we don't try to "fix" it beyond the first-occurrence
            # replacement).
            out_lines.append(line)
            continue
        seen.add(key)
        raw_value = body[eq + 1:]
        # Detect trailing inline comment on an unquoted value.
        trailing_comment = ""
        val_str = raw_value
        val_stripped = val_str.strip()
        if not (val_stripped.startswith('"') or val_stripped.startswith("'")):
            hash_pos = val_stripped.find("#")
            if hash_pos >= 0:
                trailing_comment = "  " + val_stripped[hash_pos:]
        new_line = (
            f"{leading_ws}{export_prefix}{key}={KEYCHAIN_SENTINEL}{trailing_comment}"
        )
        out_lines.append(new_line)
        replaced_count += 1

    missed = [k for k in keyset if k not in seen]

    # Preserve trailing newline if the original had one.
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"

    # Atomic write: write to sibling temp file, copystat, replace.
    parent = env_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=".env.vco-migrate-",
        dir=str(parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        # Preserve mode (especially important on Unix where the env file
        # may already be 0o600; we don't want a permissive temp file to
        # leak the sentinel-replaced file briefly into a world-readable
        # state during the swap).
        if env_path.exists():
            try:
                shutil.copystat(env_path, tmp_path)
            except OSError:
                # Best-effort — proceed even if copystat fails (e.g. on
                # filesystems that don't preserve all attrs).
                pass
        os.replace(tmp_path, env_path)
    except Exception:
        # Best-effort cleanup; re-raise to surface the failure.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    return replaced_count, missed


def harden_env_perms(env_path: Path) -> Tuple[bool, str]:
    """On Linux/macOS, ``chmod 0o600`` if perms allow group/other access.

    Returns ``(changed, message)``:
    * ``changed=True`` — perms were tightened.
    * ``changed=False`` — perms were already 0o600 or stricter, OR the
      file doesn't exist, OR this is Windows (no-op).

    The ``message`` is a short, log-friendly description of what
    happened. Caller is expected to log it via ``_log_install_event``
    if non-empty.

    Windows: the launcher's stance on file-mode hardening is "rely on
    the default user-profile ACL" — same posture as ``hub.token`` (see
    ``vct_hub/src/auth.rs::write_token_file`` comment chain). No
    ``icacls`` invocation here.
    """
    if not env_path.exists():
        return False, ""
    if platform.system().lower().startswith("win"):
        return False, "harden_env_perms: Windows — default ACL"

    try:
        st = env_path.stat()
    except OSError as exc:
        return False, f"harden_env_perms: stat failed: {exc}"

    mode = stat.S_IMODE(st.st_mode)
    # 0o600 = owner-rw, group=0, other=0. Anything that grants group or
    # other read OR write is loose enough to harden.
    loose_bits = mode & 0o077
    if loose_bits == 0:
        return False, ""  # already tight

    target_mode = mode & 0o700  # keep owner bits, drop group+other entirely
    if target_mode == 0:
        # Defensive — never strip the owner's own access.
        target_mode = 0o600
    try:
        os.chmod(env_path, target_mode)
    except OSError as exc:
        return False, f"harden_env_perms: chmod failed: {exc}"
    return True, f"harden_env_perms: {oct(mode)} -> {oct(target_mode)}"


__all__ = [
    "KEYCHAIN_SENTINEL",
    "EnvSecret",
    "audit_env_secrets",
    "harden_env_perms",
    "is_secret_shaped_env_key",
    "rewrite_env_with_sentinels",
]
