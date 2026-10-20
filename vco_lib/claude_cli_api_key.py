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

One home for the decision (the callers are stdlib-oriented scripts that
lazy-import this module):

* :func:`fallback_enabled` — the launcher setting, soft-read: an absent or
  unreadable launcher DB is OFF (these jobs have no "previous value" to
  keep, unlike the long-running gateway, which reads the same row through
  the strict reader and keeps its last value on a locked DB).
* :func:`resolve_key` — VCO's own shared slot through the sanctioned
  resolver (``vco_lib.agent_secrets.get``, hub-first then the file store),
  ``None`` on any miss; never raises to the caller.
* :func:`retry_blocker` — why the job's environment must NOT be retried
  with the key (a cloud-provider switch, a non-VCO ``ANTHROPIC_BASE_URL``,
  a user's own ``ANTHROPIC_AUTH_TOKEN``), or ``None``.
* :func:`child_env_with_key` — the child env for the retry.
* :func:`retry_env` — the four above in their one order, for both callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

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

#: Gateway routing keys removed from the retry's env — ONLY when
#: ``ANTHROPIC_BASE_URL`` names the local VCO model gateway
#: (:func:`_is_vco_gateway_url`). A hook-spawned job inherits the panel's
#: gateway routing (``ANTHROPIC_BASE_URL`` pointing at the local gateway,
#: ``ANTHROPIC_AUTH_TOKEN`` the loopback bearer), and with those in force the
#: key would be sent to the gateway — which authenticates upstream with its
#: OWN credential — so the retry would repeat the quota refusal it was meant
#: to escape. With both removed and the key set, the CLI talks to
#: ``api.anthropic.com`` with the key, which is the entire point of the
#: retry. Any OTHER routing the user set is theirs: :func:`retry_blocker`
#: refuses the retry instead of overriding it.
_GATEWAY_ROUTING_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")

#: Claude Code's cloud-provider switches. With one set, the CLI authenticates
#: with that provider's credentials, not an Anthropic API key, so the retry
#: would repeat the same refusal with the key unused — it is not attempted.
PROVIDER_SWITCHES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

#: Values of a provider switch that mean "off" (anything else non-empty is
#: read as ON — when in doubt the retry is NOT attempted).
_SWITCH_OFF_VALUES = ("", "0", "false", "no", "off")


def _is_vco_gateway_url(url: str) -> bool:
    """Whether *url* is the local VCO model gateway, by the gateway's own
    rule (:func:`vco_lib.vscode_settings.is_vco_gateway_base_url`: an http(s)
    loopback URL on the port :func:`~vco_lib.vscode_settings.resolve_gateway_ports`
    resolves). An answer that cannot be computed is ``False`` — the URL is
    then treated as the user's own routing, which is left alone."""
    try:
        from vco_lib.vscode_settings import (  # noqa: PLC0415
            is_vco_gateway_base_url,
        )

        return bool(is_vco_gateway_base_url(url))
    except Exception:  # noqa: BLE001 — undecidable means "not ours"
        return False


def retry_blocker(env: Mapping[str, str]) -> Optional[str]:
    """Why the job running in *env* must not be retried with the key, or
    ``None`` when the retry may run.

    * a cloud-provider switch (:data:`PROVIDER_SWITCHES`) is set — the CLI
      authenticates with that provider, not an Anthropic key;
    * ``ANTHROPIC_BASE_URL`` is set and is NOT the local VCO gateway — a
      corporate proxy or another endpoint the user chose; sending the Console
      key around it (or through it) would override their routing;
    * ``ANTHROPIC_AUTH_TOKEN`` is set without the VCO gateway's base URL —
      the user's own bearer credential, which the CLI would keep using.

    The returned text names variables only, never their values (a base URL
    can carry credentials).
    """
    for name in PROVIDER_SWITCHES:
        if (env.get(name) or "").strip().lower() not in _SWITCH_OFF_VALUES:
            return (f"{name} is set — the claude CLI authenticates with that "
                    "cloud provider, not an Anthropic API key")
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    if base_url:
        if not _is_vco_gateway_url(base_url):
            return ("ANTHROPIC_BASE_URL points at an endpoint that is not the "
                    "local VCO model gateway — the job's own routing is left "
                    "alone")
        return None
    if (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
        return ("ANTHROPIC_AUTH_TOKEN is set without the VCO gateway's base "
                "URL — the job's own credential is left alone")
    return None


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
    """VCO's own ``ANTHROPIC_API_KEY_GATEWAY`` secret (shared slot), or ``None``.

    Resolved through the sanctioned chain (``vco_lib.agent_secrets.get``:
    hub/keychain first, then the ``~/.vct-secrets`` file store) with
    ``shared_only=True, allow_dotenv=False``: the file store's project leg
    and the project ``.env`` never answer. On the hub tier the key is
    declared ``slot_exclusive`` in ``vct-module.json`` ``bundled_secrets``,
    so the hub's ``/env`` serves it ONLY from VCO's shared slot — a secret a
    user saves under the same name in a project's own bucket (or grants from
    another project) never answers for it. The model gateway's resolver
    applies the same rule (``model_router.secrets``, ``shared_only`` on
    ``ANTHROPIC_API_KEY_ROUTE``). Owner rule: a project's secrets are the
    project's; VCO spends only its own slot.

    Returns ``None`` on EVERY miss (absent, paused, denied, store
    unavailable, resolver itself broken): the callers treat ``None`` as "no
    retry, today's behaviour", so this function never raises to them. The
    value is returned stripped; it is never logged anywhere.
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

    ``ANTHROPIC_API_KEY`` is set to *key*; the gateway routing vars
    (:data:`_GATEWAY_ROUTING_VARS`) are removed ONLY when
    ``ANTHROPIC_BASE_URL`` is the local VCO gateway, so the retry reaches
    Anthropic directly (see their comment). Callers check
    :func:`retry_blocker` first — any other routing means no retry at all.
    The input mapping is never mutated — the caller's own environment (and
    ``os.environ``) stays exactly as it was, so the key exists only inside
    the one retried child process.
    """
    child = dict(env)
    base_url = (child.get("ANTHROPIC_BASE_URL") or "").strip()
    if base_url and _is_vco_gateway_url(base_url):
        for name in _GATEWAY_ROUTING_VARS:
            child.pop(name, None)
    child[CHILD_ENV_VAR] = key
    return child


def retry_env(
    base_env: Mapping[str, str],
    log: Callable[[str], None],
    *,
    enabled: "Optional[Callable[[], bool]]" = None,
    resolve: "Optional[Callable[[], Optional[str]]]" = None,
) -> "Optional[dict[str, str]]":
    """The child env for the ONE key retry of a quota-refused job, or ``None``.

    The one order both callers follow: the launcher switch
    (:func:`fallback_enabled`) → the environment may be retried
    (:func:`retry_blocker`; a blocker is logged as ONE line through *log*,
    naming variables only) → the key resolves (:func:`resolve_key`) → the
    child env (:func:`child_env_with_key`). The switch is read before the
    key, so with the switch OFF no secret store is touched. *enabled* /
    *resolve* replace the two readers (``summary_backends`` passes its
    process-local memo so a long sync reads each once); ``None`` means the
    module's own functions, looked up at call time.
    """
    is_on = enabled if enabled is not None else fallback_enabled
    if not is_on():
        return None
    why = retry_blocker(base_env)
    if why is not None:
        log(f"not retrying with the Claude API key: {why}")
        return None
    get_key = resolve if resolve is not None else resolve_key
    value = get_key()
    if value is None:
        return None
    return child_env_with_key(base_env, value)


__all__ = [
    "APP_STATE_KEY",
    "CHILD_ENV_VAR",
    "PROVIDER_SWITCHES",
    "SECRET_KEY_NAME",
    "child_env_with_key",
    "fallback_enabled",
    "resolve_key",
    "retry_blocker",
    "retry_env",
]
