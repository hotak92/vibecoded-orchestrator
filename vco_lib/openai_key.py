# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The OpenAI API key: one name, one store, one resolver (v0.2.97).

VCO never writes a secret VALUE into the project tree; resolvers read it at
need. Before v0.2.97 ``install.py --openai-key KEY`` wrote the key into the
orchestrator root's ``.env`` — and nothing read it back from there (no VCO
code loads a ``.env`` into the environment), while the launcher's slot for
it went unread by the Python consumers. This module closes both halves:

* **Name** — :data:`OPENAI_SECRET_NAME` = ``openai_api_key``, the slot
  ``vct-module.json`` ``bundled_secrets`` declares (scope ``shared``,
  module ``user``), which the launcher's ``register_openai_api_key`` writes
  and the hub's ``/env`` serves. No second name.
* **Store** — :func:`store_openai_api_key`: the launcher keychain through
  the hub (``POST /api/v1/secrets/migrate``, Shared scope — the same row the
  GUI writes) when the hub answers, else the file store
  ``$VCT_SECRETS_DIR/shared/openai_api_key`` through the ``vct`` CLI (its
  write guards apply).
* **Resolve** — :func:`resolve_openai_api_key`: ``$OPENAI_API_KEY`` when the
  caller's environment sets it, else the canonical chain
  (:func:`vco_lib.agent_secrets.get` — keychain, file store, the project's
  own ``.env``). Every Python reader goes through it.
* **Migrate** — :func:`migrate_dotenv_openai_key`: a pre-v0.2.97 root
  ``.env`` line VCO wrote (under its ``# OpenAI (for embeddings)`` header)
  is removed ONLY on value evidence — it equals what the store holds,
  after copying it into the file store when nothing is stored yet. A line
  the user wrote anywhere else is theirs and is not touched.

No function here logs, prints or returns a value.
"""

from __future__ import annotations

import hmac
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: The one name of the slot (``vct-module.json`` ``bundled_secrets``).
OPENAI_SECRET_NAME = "openai_api_key"
#: The environment variable the consumers (and the OpenAI SDKs) read.
OPENAI_ENV_VAR = "OPENAI_API_KEY"
#: The comment line ``install.py`` wrote right above the key before v0.2.97
#: — the provenance of a VCO-written ``.env`` line.
LEGACY_DOTENV_HEADER = "# OpenAI (for embeddings)"

_resolved: dict[str, str] = {}


class StoreFailed(RuntimeError):
    """Neither store accepted the key. The message names the stores and the
    reason, never the value."""


def resolve_openai_api_key(project: Optional[str] = None) -> str:
    """The OpenAI key for ``project`` (``None`` → the current directory), or
    ``""`` when none is configured anywhere.

    ``$OPENAI_API_KEY`` wins (an explicit per-process choice, and what the
    OpenAI tooling itself reads); otherwise the canonical three-tier chain
    answers once per project per process — a miss costs one bounded
    localhost request, not one per embedding call."""
    env = os.environ.get(OPENAI_ENV_VAR, "").strip()
    if env:
        return env
    cache_key = project or ""
    if cache_key not in _resolved:
        from vco_lib import agent_secrets

        try:
            _resolved[cache_key] = agent_secrets.get(OPENAI_SECRET_NAME, project=project).strip()
        except (agent_secrets.ResolverError, OSError, ValueError):
            _resolved[cache_key] = ""
    return _resolved[cache_key]


def _store_in_keychain(value: str) -> bool:
    """``True`` when the hub stored the value in the launcher keychain's
    shared ``openai_api_key`` row; ``False`` when the hub could not be asked
    or refused the item (an older hub accepts only UPPER_CASE names)."""
    from vco_lib.install_env_secret_scope import post_secrets_to_hub

    try:
        migrated, _failed, _scope = post_secrets_to_hub(
            [{"key": OPENAI_SECRET_NAME, "value": value}], project_id=None,
        )
    except RuntimeError:
        return False
    return OPENAI_SECRET_NAME in migrated


#: The file-store CLI, shipped in the same checkout as this package.
_VCT_CLI = Path(__file__).resolve().parents[1] / "tools" / "vct-secrets" / "vct"


def _vct_cli() -> Optional[list[str]]:
    bash = shutil.which("bash")
    return [bash, str(_VCT_CLI)] if bash and _VCT_CLI.is_file() else None


def _store_in_file_store(value: str) -> None:
    """``vct set --shared --key openai_api_key`` with the value on stdin (the
    CLI refuses a value on argv, and applies its write-time guards)."""
    argv = _vct_cli()
    if argv is None:
        raise StoreFailed(
            f"the hub did not answer and the file-store CLI ({_VCT_CLI}) "
            f"cannot run here (it needs bash)"
        )
    done = subprocess.run(
        [*argv, "set", "--shared", "--key", OPENAI_SECRET_NAME],
        input=value, capture_output=True, text=True, timeout=30,
    )
    if done.returncode != 0:
        # The CLI's own messages name keys and paths only.
        raise StoreFailed(f"vct set failed: {done.stderr.strip()[:300]}")


def store_openai_api_key(value: str, *, keychain: bool = True) -> str:
    """Store ``value`` under :data:`OPENAI_SECRET_NAME`; returns where it
    landed (``"keychain"`` / ``"file_store"``). ``keychain=False`` skips the
    keychain (a migration that must not overwrite or un-pause a keychain row
    it could not read). Raises :class:`StoreFailed`."""
    value = value.strip()
    if not value:
        raise StoreFailed("empty value — nothing to store")
    _resolved.clear()
    if keychain and _store_in_keychain(value):
        return "keychain"
    _store_in_file_store(value)
    return "file_store"


def describe_store(where: str) -> str:
    """Where a stored key lives, for a user-facing line (no value)."""
    if where == "keychain":
        return (
            "the launcher keychain (shared slot `openai_api_key` — "
            "Preferences → Special Secrets)"
        )
    root = os.environ.get("VCT_SECRETS_DIR", "").strip() or "~/.vct-secrets"
    return f"the file store ({root}/shared/{OPENAI_SECRET_NAME})"


def _is_clean_key(value: str) -> bool:
    """A parsed ``.env`` value that is one token an API key can be."""
    return bool(value) and not any(ch.isspace() or ch in "#'\"" for ch in value)


def migrate_dotenv_openai_key(root: Path) -> dict[str, str]:
    """Move a pre-v0.2.97 VCO-written ``OPENAI_API_KEY`` line out of
    ``<root>/.env``.

    Only the line directly under :data:`LEGACY_DOTENV_HEADER` is VCO's. It
    is removed when its value EQUALS what VCO's stores hold (keychain or
    file store; constant-time compare). When nothing is stored yet, the value
    is first copied into the FILE STORE — never the keychain, whose row may
    be paused rather than empty (the hub answers both the same way) — and
    removed once the copy reads back equal. Otherwise it stays and the
    status says why.

    The value is read with the ONE line grammar (``envfile.parse_env_line``:
    ``export``, one quote pair, CRLF) — what a reader of the line gets.

    Returns ``{"status": ..., "detail": ...}`` — status ``absent`` (no such
    line), ``migrated`` (removed; detail = where the value now lives),
    ``left_differs`` (the store holds another value), ``left_unverified``
    (it could not be stored or read back), ``left_unparsed`` (the value is
    not one clean token). Never a value.
    """
    from vco_lib import agent_secrets
    from vco_lib.env_template import remove_line_under

    outcome: dict[str, str] = {"status": "absent", "detail": ""}

    def proven(value: str) -> bool:
        # The line grammar already stripped `export` and ONE quote pair. What
        # is left must be a single token: an API key never holds whitespace,
        # a `#` or a quote, so anything that does (a trailing comment, a stray
        # or mismatched quote) did not parse CLEANLY — storing it would plant
        # a broken key. Leave the line and say why (review R5 F34).
        if not _is_clean_key(value):
            outcome.update(
                status="left_unparsed",
                detail=(
                    "its value is not one clean token (a trailing comment, a stray "
                    "quote or whitespace) — move it into the store by hand"
                ),
            )
            return False
        state, stored = agent_secrets.lookup_stored(OPENAI_SECRET_NAME, project=str(root))
        if stored is None:
            try:
                store_openai_api_key(value, keychain=False)
            except (StoreFailed, OSError, subprocess.SubprocessError) as exc:
                outcome.update(status="left_unverified", detail=f"could not store it: {exc}")
                return False
            state, stored = agent_secrets.lookup_stored(OPENAI_SECRET_NAME, project=str(root))
        if stored is not None and hmac.compare_digest(
            value.encode("utf-8"), stored.strip().encode("utf-8"),
        ):
            outcome.update(status="migrated", detail=describe_store(state))
            return True
        if stored is None:
            outcome.update(status="left_unverified", detail="the stored copy could not be read back")
        else:
            outcome.update(
                status="left_differs",
                detail=f"{describe_store(state)} holds a different key",
            )
        return False

    removed = remove_line_under(root, LEGACY_DOTENV_HEADER, OPENAI_ENV_VAR, remove_if=proven)
    if removed is None:
        return {"status": "absent", "detail": ""}
    _resolved.clear()
    return outcome


# The ONE argv redactor lives with the install companions (stdlib only, so
# install.py can use it before its venv exists); re-exported here.
from vco_lib.install_companions import (  # noqa: E402
    SECRET_ARGV_FLAGS,
    redact_secret_argv,
)


__all__ = [
    "LEGACY_DOTENV_HEADER",
    "OPENAI_ENV_VAR",
    "OPENAI_SECRET_NAME",
    "StoreFailed",
    "SECRET_ARGV_FLAGS",
    "describe_store",
    "migrate_dotenv_openai_key",
    "redact_secret_argv",
    "resolve_openai_api_key",
    "store_openai_api_key",
]
