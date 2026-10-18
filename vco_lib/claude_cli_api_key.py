# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One Claude API-key retry for VCO's background ``claude -p`` jobs (v0.2.104, API-1).

VCO runs background jobs through ``claude -p`` on the user's Claude
subscription: the KG summary generator's CLI tier
(``templates/scripts/summary_backends.py``) and the post-commit KG reviewer
(``templates/scripts/kg_reviewer_backend.py``; code summaries ride the
summary ladder's CLI tier). When the subscription is OUT OF USAGE, the same
launcher setting that arms the model gateway's subscription -> API-key
fallback also allows each such job ONE retry with the Anthropic Console API
key the owner stored as the SHARED secret ``ANTHROPIC_API_KEY_GATEWAY`` (the
key the model gateway's own API-key route uses). ``claude -p`` reads
``ANTHROPIC_API_KEY`` from its environment, so the key is passed ONLY in the
child process environment — never argv, never a file, never a log line.

Three functions, one home (the callers are stdlib-oriented scripts that
lazy-import this module):

* :func:`fallback_enabled` — the launcher setting, soft-read: an absent or
  unreadable launcher DB is OFF (these jobs have no "previous value" to
  keep, unlike the long-running gateway, which reads the same row through
  the strict reader and keeps its last value on a locked DB).
* :func:`resolve_key` — the shared secret through the sanctioned resolver
  (``vco_lib.agent_secrets.get``, hub-first then the file store), ``None``
  on any miss; never raises to the caller.
* :func:`child_env_with_key` — the child env for the retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

#: The launcher ``app_state`` key (``~/.vct/launcher.db``) that turns the
#: subscription -> API-key fallback on. MUST MATCH
#: ``API_KEY_FALLBACK_APP_STATE_KEY`` in
#: ``claude_mcp_servers/model_router/config.py`` and
#: ``APP_STATE_KEY_GATEWAY_API_KEY_FALLBACK`` in
#: ``launcher/src/lib/preferences/loaders.ts`` (parity-tested in
#: ``tests/test_v02104_claude_cli_api_key_fallback.py``). Not imported from
#: the gateway: importing ``model_router`` from ``vco_lib`` would pull the
#: gateway package into every caller of this small helper.
APP_STATE_KEY = "model_gateway.api_key_fallback"

#: The shared vct-secrets slot holding the Console API key. MUST MATCH the
#: ``secret_keys`` of ``ANTHROPIC_API_KEY_ROUTE`` in
#: ``claude_mcp_servers/model_router/vendors.py`` (parity-tested). ONE name,
#: the owner's (2026-10-18): the generic ``ANTHROPIC_API_KEY`` /
#: ``anthropic_api_key`` slots are a PROJECT's own key and VCO only ever
#: spends its own slot.
SECRET_KEY_NAME = "ANTHROPIC_API_KEY_GATEWAY"

#: The env var the ``claude`` CLI reads its API key from.
CHILD_ENV_VAR = "ANTHROPIC_API_KEY"

#: Gateway routing keys removed from the retry's env. The retry must reach
#: ``api.anthropic.com`` DIRECTLY: a hook-spawned job inherits the panel's
#: gateway routing (``ANTHROPIC_BASE_URL`` pointing at the local gateway,
#: ``ANTHROPIC_AUTH_TOKEN`` the loopback bearer), and with those in force the
#: key would be sent to the gateway — which authenticates upstream with its
#: OWN credential — so the retry would repeat the quota refusal it was meant
#: to escape. With both removed and the key set, the CLI talks to Anthropic
#: with the key, which is the entire point of the retry.
_GATEWAY_ROUTING_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")


def fallback_enabled(db_path: "Optional[Path]" = None) -> bool:
    """Whether the subscription -> API-key fallback is ON for these jobs.

    Reads the SAME launcher setting the model gateway's fallback uses
    (``app_state[model_gateway.api_key_fallback]``; truthy = ``true``/``1``,
    the spelling the launcher's ``app_state_set_bool`` writes) through the
    SOFT reader: no row, no launcher DB, an unreadable DB — all OFF. A
    setting that bills the user must be positively ON, never on by accident,
    and a background job has no previously-read value worth keeping (the
    gateway's strict-reader/keep-last-value discipline does not apply to a
    process that starts fresh). ``db_path`` overrides launcher DB discovery
    for tests.
    """
    # Deferred: the soft reader opens SQLite, which nothing else in this
    # module needs at import time (the scripts that import this module are
    # stdlib-oriented and import it lazily themselves).
    from vco_lib.launcher_db_reader import read_app_state_value  # noqa: PLC0415

    raw = read_app_state_value(APP_STATE_KEY, db_path=db_path)
    return raw is not None and raw.strip().lower() in ("true", "1")


def resolve_key() -> Optional[str]:
    """The shared ``ANTHROPIC_API_KEY_GATEWAY`` secret, or ``None``.

    Resolved through the sanctioned chain (``vco_lib.agent_secrets.get``:
    hub/keychain first, then the ``~/.vct-secrets`` file store) restricted
    to the SHARED slot — the key is VCO's own, so a project-scoped binding
    or a project ``.env`` line never answers for it. Returns ``None`` on
    EVERY miss (absent, paused, denied, store unavailable, resolver itself
    broken): the callers treat ``None`` as "no retry, today's behaviour",
    so this function never raises to them. The value is returned stripped;
    it is never logged anywhere.
    """
    from vco_lib.agent_secrets import get as _get_secret  # noqa: PLC0415

    try:
        value = _get_secret(SECRET_KEY_NAME, shared_only=True, allow_dotenv=False)
    except Exception:  # noqa: BLE001 — every resolver error is a miss here
        return None
    if not value or not value.strip():
        return None
    return value.strip()


def child_env_with_key(env: Mapping[str, str], key: str) -> "dict[str, str]":
    """A COPY of *env* for the one fallback retry, with the key applied.

    ``ANTHROPIC_API_KEY`` is set to *key* and the gateway routing vars
    (:data:`_GATEWAY_ROUTING_VARS`) are removed so the retry reaches
    Anthropic directly (see their comment). The input mapping is never
    mutated — the caller's own environment (and ``os.environ``) stays
    exactly as it was, so the key exists only inside the one retried child
    process.
    """
    child = dict(env)
    for name in _GATEWAY_ROUTING_VARS:
        child.pop(name, None)
    child[CHILD_ENV_VAR] = key
    return child


__all__ = [
    "APP_STATE_KEY",
    "CHILD_ENV_VAR",
    "SECRET_KEY_NAME",
    "child_env_with_key",
    "fallback_enabled",
    "resolve_key",
]
