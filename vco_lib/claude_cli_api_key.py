# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One Claude API-key retry for VCO's background ``claude -p`` jobs (v0.2.104, API-1).

VCO runs background jobs through ``claude -p`` on the user's Claude
subscription: the KG summary generator's CLI tier
(``templates/scripts/summary_backends.py``) and the post-commit KG reviewer
(``templates/scripts/kg_reviewer_backend.py``; code summaries ride the
summary ladder's CLI tier). When the subscription is OUT OF USAGE, the
launcher setting ``model_gateway.api_key_fallback`` (Preferences → "Claude
API key for background jobs") allows each such job ONE retry with the
Anthropic Console API key the owner stored as the SHARED secret
``ANTHROPIC_API_KEY_GATEWAY`` (the name is historical — the model gateway
itself never reads it). ``claude -p`` reads ``ANTHROPIC_API_KEY`` from its
environment, so the key is passed ONLY in the child process environment —
never argv, never a file, never a log line. This module is the one home of
both the setting key and the secret name on the Python side.

One home for the decision (the callers are stdlib-oriented scripts that
lazy-import this module):

* :func:`fallback_enabled` — the launcher setting, soft-read: an absent or
  unreadable launcher DB is OFF (these jobs start fresh and have no
  "previous value" to keep).
* :func:`resolve_key` — VCO's own shared slot through the sanctioned
  resolver (``vco_lib.agent_secrets.get``, hub-first then the file store),
  ``None`` on any miss; never raises to the caller.
* :func:`retry_blocker` — why the job's environment must NOT be retried
  with the key (a cloud-provider switch, a non-VCO ``ANTHROPIC_BASE_URL``,
  a user's own ``ANTHROPIC_AUTH_TOKEN``), or ``None``.
* :func:`child_env_with_key` — the child env for the retry.
* :func:`retry_env` — the four above in their one order, for both callers.

The API-run tool (:mod:`vco_lib.claude_api_run`, a user-started headless
``claude -p`` billed to the key) reuses :func:`retry_blocker`,
:func:`resolve_key` and :func:`child_env_with_key` — this module stays the
one home of "the env for a ``claude -p`` on the Console key".
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

#: The launcher ``app_state`` key (``~/.vct/launcher.db``) that turns the
#: background jobs' subscription -> API-key retry on (the ``model_gateway.``
#: prefix is historical; the gateway does not read it). MUST MATCH
#: ``APP_STATE_KEY_GATEWAY_API_KEY_FALLBACK`` in
#: ``launcher/src/lib/preferences/loaders.ts`` (parity-tested in
#: ``tests/test_v02104_claude_cli_api_key_fallback.py``).
APP_STATE_KEY = "model_gateway.api_key_fallback"

#: The shared vct-secrets slot holding the Console API key — its one Python
#: home. MUST MATCH the ``slot_exclusive`` entry in ``vct-module.json``
#: ``bundled_secrets`` and the launcher SecretsPanel suggestion
#: (parity-tested in ``tests/test_v02104_claude_cli_api_key_fallback.py``).
#: ONE name, the owner's (2026-10-18): the generic ``ANTHROPIC_API_KEY`` /
#: ``anthropic_api_key`` slots are a PROJECT's own key and VCO only ever
#: spends its own slot.
SECRET_KEY_NAME = "ANTHROPIC_API_KEY_GATEWAY"

#: The env var the ``claude`` CLI reads its API key from.
CHILD_ENV_VAR = "ANTHROPIC_API_KEY"

#: Removed from the retry's env, ALWAYS (never replaced by another value).
#: Claude Code exports it to everything it spawns (``claude-vscode`` inside
#: the VS Code extension, ``cli`` in a terminal), so a hook-spawned job
#: inherits the panel's entrypoint. Measured 2026-10-20: with
#: ``CLAUDE_CODE_ENTRYPOINT=claude-vscode`` in its env, ``claude -p``
#: IGNORES ``ANTHROPIC_API_KEY`` and keeps using the OAuth login (a
#: deliberately invalid key still answered 200, on the subscription); with the
#: variable removed the key is used (the same invalid key -> 401 "Invalid API
#: key"). Anthropic also refuses Console API credits for requests that carry
#: the VS Code entrypoint. The retry IS a headless run, so it carries no
#: entrypoint of its own and the CLI applies its headless defaults; the
#: user-agent is left exactly as the CLI builds it.
ENTRYPOINT_ENV_VAR = "CLAUDE_CODE_ENTRYPOINT"

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
#: read as ON — when in doubt the retry is NOT attempted). Checked against the
#: live CLI 2026-10-20 (NIT-B): ``CLAUDE_CODE_USE_BEDROCK=0`` and ``=false``
#: (fresh ``CLAUDE_CONFIG_DIR``, no entrypoint/base URL/auth token, a
#: deliberately invalid Anthropic key) both printed "Invalid API key · Fix
#: external API key" — the key went to api.anthropic.com, not to Bedrock — so
#: the CLI reads them as OFF, as this tuple does.
_SWITCH_OFF_VALUES = ("", "0", "false", "no", "off")


class GatewayCheckUnavailable(Exception):
    """``vco_lib.vscode_settings`` (the gateway-URL rule) cannot be imported —
    a broken install, so whether ``ANTHROPIC_BASE_URL`` is the VCO gateway is
    UNKNOWN. Carries the exception class name for the blocker text."""


def _is_vco_gateway_url(url: str) -> bool:
    """Whether *url* is the local VCO model gateway, by the gateway's own
    rule (:func:`vco_lib.vscode_settings.is_vco_gateway_base_url`: an http(s)
    loopback URL on the port :func:`~vco_lib.vscode_settings.resolve_gateway_ports`
    resolves).

    Raises :class:`GatewayCheckUnavailable` when that module cannot be
    imported (R22 LOW-A): "cannot tell" is a different answer from "not the
    gateway", and :func:`retry_blocker` says so instead of blaming the URL.
    """
    try:
        from vco_lib.vscode_settings import (  # noqa: PLC0415
            is_vco_gateway_base_url,
        )
    except ImportError as exc:
        raise GatewayCheckUnavailable(type(exc).__name__) from exc
    return bool(is_vco_gateway_base_url(url))


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
        try:
            ours = _is_vco_gateway_url(base_url)
        except GatewayCheckUnavailable as exc:
            return ("cannot tell whether ANTHROPIC_BASE_URL is the VCO "
                    "gateway: vco_lib.vscode_settings is not importable "
                    f"({exc}) — not retried")
        if not ours:
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

    Reads the launcher setting Preferences writes
    (``app_state[model_gateway.api_key_fallback]``; truthy = ``true``/``1``,
    the spelling the launcher's ``app_state_set_bool`` writes) through the
    SOFT reader: no row, no launcher DB, an unreadable DB — all OFF. A
    setting that bills the user must be positively ON, never on by accident,
    and a background job starts fresh, so it has no previously-read value
    worth keeping. ``db_path`` overrides launcher DB discovery for tests.
    """
    # Deferred: the soft reader opens SQLite, which nothing else in this
    # module needs at import time (the scripts that import this module are
    # stdlib-oriented and import it lazily themselves).
    from vco_lib.launcher_db_reader import read_app_state_value  # noqa: PLC0415

    raw = read_app_state_value(APP_STATE_KEY, db_path=db_path)
    return raw is not None and raw.strip().lower() in ("true", "1")


def resolve_key(
    name: str = SECRET_KEY_NAME, project: Optional[str] = None,
) -> Optional[str]:
    """VCO's own ``ANTHROPIC_API_KEY_GATEWAY`` secret (shared slot), or ``None``.

    *name* selects another Shared-scope secret instead (the API-run tool's
    ``--key-name``, :mod:`vco_lib.claude_api_run`); the background jobs
    always use the default. Whatever the name, only the Shared slot answers.
    *project* is the REQUESTER identity the hub gates on (a registered
    project path; ``None`` = the current working directory, which is what
    the background jobs run in); it never widens the lookup.

    Resolved through the sanctioned chain (``vco_lib.agent_secrets.get``:
    hub/keychain first, then the ``~/.vct-secrets`` file store) with
    ``shared_only=True, allow_dotenv=False``: the file store's project leg
    and the project ``.env`` never answer. On the hub tier the key is
    declared ``slot_exclusive`` in ``vct-module.json`` ``bundled_secrets``,
    so the hub's ``/env`` serves it ONLY from VCO's shared slot — a secret a
    user saves under the same name in a project's own bucket (or grants from
    another project) never answers for it. Owner rule: a project's secrets
    are the project's; VCO spends only its own slot.

    Returns ``None`` on EVERY miss (absent, paused, denied, store
    unavailable, resolver itself broken): the callers treat ``None`` as "no
    retry, today's behaviour", so this function never raises to them. The
    value is returned stripped; it is never logged anywhere.
    """
    from vco_lib.agent_secrets import get as _get_secret  # noqa: PLC0415

    try:
        value = _get_secret(name, project=project, shared_only=True,
                            allow_dotenv=False)
    except Exception:  # noqa: BLE001 — every resolver error is a miss here
        return None
    if not value or not value.strip():
        return None
    return value.strip()


def child_env_with_key(env: Mapping[str, str], key: str) -> "dict[str, str]":
    """A COPY of *env* for the one fallback retry, with the key applied.

    ``ANTHROPIC_API_KEY`` is set to *key*; :data:`ENTRYPOINT_ENV_VAR` is
    always removed (with an inherited ``claude-vscode`` entrypoint the CLI
    ignores the key — see its comment); the gateway routing vars
    (:data:`_GATEWAY_ROUTING_VARS`) are removed ONLY when
    ``ANTHROPIC_BASE_URL`` is the local VCO gateway, so the retry reaches
    Anthropic directly (see their comment). Callers check
    :func:`retry_blocker` first — any other routing means no retry at all.
    The input mapping is never mutated — the caller's own environment (and
    ``os.environ``) stays exactly as it was, so the key exists only inside
    the one retried child process.
    """
    child = dict(env)
    child.pop(ENTRYPOINT_ENV_VAR, None)
    base_url = (child.get("ANTHROPIC_BASE_URL") or "").strip()
    try:
        ours = bool(base_url) and _is_vco_gateway_url(base_url)
    except GatewayCheckUnavailable:
        ours = False  # undecidable: the job's routing is left as it is
    if ours:
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
    "ENTRYPOINT_ENV_VAR",
    "GatewayCheckUnavailable",
    "PROVIDER_SWITCHES",
    "SECRET_KEY_NAME",
    "child_env_with_key",
    "fallback_enabled",
    "resolve_key",
    "retry_blocker",
    "retry_env",
]
