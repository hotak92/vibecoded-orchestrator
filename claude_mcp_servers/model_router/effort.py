# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-(vendor, model) effort translation — the data table and the one rewrite.

Claude Code carries its reasoning-effort selector in ONE of two places, and
which one depends on the build:

* current builds send ``output_config.effort`` (a string: ``low`` / ``medium``
  / ``high`` / ``xhigh`` / ``max``) alongside an adaptive ``thinking`` block;
* older builds send only ``thinking.budget_tokens`` (an integer).

The gateway used to forward BOTH untouched. That is correct for the
first-party route and for every vendor model whose accepted effort set is a
superset of what the client sends — but it is a hard failure on one measured
route (parity audit 2026-10-03, gap 1): ``claude-gw/qwen/glm-5.3`` at effort
``medium`` — the owner's DEFAULT subagent effort — returns

    HTTP 400  {"code":"InvalidParameter",
               "message":"'reasoning_effort' must be one of: 'low','high','max'"}

(live capture ``tests/fixtures/model_router/medium-qwen-glm.body``), relayed
verbatim, because QwenCloud's Anthropic endpoint accepts only ``low``/``high``/
``max`` for glm-5.3 and ERRORS on anything else. The same ``medium`` succeeds
on the z.ai route (``medium-zai-glm.body``) and on qwen3.8-max
(``medium-qwen-max.body``) — so the accepted set is a property of the
(vendor, model) PAIR, never of the model name alone. glm-5.3 is glm-5.3, but
z.ai answers ``medium`` and QwenCloud rejects it.

The rule this module implements, and the owner rules behind it
----------------------------------------------------------------
* **Only rewrite a value the vendor would REJECT.** An accepted value is left
  BYTE-IDENTICAL — the gateway is a proxy, and a cosmetic normalisation of a
  value the vendor already understands is a change to the client's request for
  no gain. ``translate`` therefore lists ONLY rejected values; everything else
  passes through untouched.
* **Never re-route to a different model.** Effort translation changes how hard
  the SAME model thinks, never which model answers.
* **A vendor-side COLLAPSE is not a rejection.** QwenCloud DOCUMENTS that it
  maps some values onto others itself (deepseek ``low``/``medium`` → ``high``;
  qwen3.8 ``high``/``max`` → ``xhigh``). The endpoint ACCEPTS those values and
  does the mapping, so the gateway must NOT rewrite them — doing so would
  second-guess the vendor and could land on a different tier than the vendor
  would have chosen. Collapses are recorded in :attr:`EffortPolicy.collapses`
  as DATA with a citation, never as a rewrite.
* **Data, not code.** Adding or correcting a route's effort behaviour is a row
  in :data:`EFFORT_POLICIES`, mirroring the vendor-registry discipline in
  :mod:`model_router.vendors`. The logic (:func:`translate_effort`) names no
  vendor and no model; it reads the table.

``thinking.budget_tokens`` (the older form) is deliberately NOT translated.
Every vendor measured either ignores it (z.ai: probe-identical at 512 and 8192
— ``zai-budget-{512,8192}.body``) or deprecates it in favour of
``output_config.effort`` (QwenCloud, DeepSeek). None REJECTS it, so there is
no failure to prevent and no honest integer→integer mapping to apply; the
gateway leaves it exactly as the client sent it.

Vendor-doc backing (re-checked 2026-10-03/04; the URL is cited per row):

* QwenCloud Anthropic Messages API, ``output_config.effort`` (verbatim):
  "The valid values and default values vary by model.
  - **glm-5.3**: Default ``max``. Valid: ``low``, ``high``, ``max``. Passing
    any other value returns an error.
  - **glm-5.2, deepseek-v4-pro, deepseek-v4-flash**: Default ``max``. Valid:
    ``high``, ``max``. ``low`` and ``medium`` are mapped to ``high``, and
    ``xhigh`` is mapped to ``max``.
  - **qwen3.8-max**: Default ``xhigh``. Valid: ``xhigh``, ``medium``, ``low``.
    ``max`` and ``high`` are mapped to ``xhigh``."
  https://docs.qwencloud.com/api-reference/chat/anthropic
* z.ai GLM-5.3: ``reasoning_effort`` low/high/max, default max.
  https://docs.z.ai/guides/llm/glm-5.3
  https://docs.z.ai/guides/capabilities/thinking
  (``medium`` is NOT in the documented set but was ACCEPTED live — HTTP 200 —
  so it is recorded as accepted and never rewritten.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: The effort strings Claude Code can put in ``output_config.effort``. The
#: union across builds and routes; a value outside it is left untouched (the
#: gateway invents no translation for a value it has no evidence about).
EFFORT_VALUES: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class EffortPolicy:
    """What one (vendor, model) route accepts, and what it rejects.

    Attributes:
        accepted: effort values the endpoint ACCEPTS. These are left
            byte-identical — see the module rule. Documentation plus the
            target set for :attr:`translate`; the rewrite itself keys only on
            ``translate``.
        translate: rejected value → the nearest accepted replacement. ONLY
            values with evidence of rejection belong here (a live 400 or an
            explicit "returns an error" in the vendor doc). Empty for a route
            that rejects nothing.
        collapses: VENDOR-SIDE mappings the endpoint performs itself (it
            accepts the key and answers at the value). Recorded as data with a
            citation; NEVER applied as a rewrite. A collapsed value is also an
            accepted value, so it appears in :attr:`accepted` too.
        doc_url: the vendor page the row was read from.
        note: the evidence — live capture names and the doc wording — so the
            next reader can re-check the row without re-probing.
    """

    accepted: frozenset[str]
    translate: Mapping[str, str] = field(default_factory=dict)
    collapses: Mapping[str, str] = field(default_factory=dict)
    doc_url: str = ""
    note: str = ""


#: The per-(vendor_id, model_id) effort table. ``model_id`` is the BARE id the
#: route forwards (``Route.forward_model``), never the namespaced client
#: spelling. A (vendor, model) pair with no row is left untouched — the
#: conservative default, and correct for the first-party route and for every
#: vendor model whose accepted set already covers what the client sends.
EFFORT_POLICIES: Mapping[tuple[str, str], EffortPolicy] = {
    # ── QwenCloud Anthropic endpoint ────────────────────────────────────
    # The ONE measured hard failure (gap 1): glm-5.3 via qwen rejects medium.
    ("qwen", "glm-5.3"): EffortPolicy(
        accepted=frozenset({"low", "high", "max"}),
        translate={"medium": "high", "xhigh": "max"},
        doc_url="https://docs.qwencloud.com/api-reference/chat/anthropic",
        note=(
            "QwenCloud doc: glm-5.3 valid effort = low/high/max (default max), "
            "'Passing any other value returns an error.' Live: medium -> HTTP "
            "400 \"'reasoning_effort' must be one of: 'low','high','max'\" "
            "(fixture medium-qwen-glm.body). medium->high and xhigh->max are "
            "the nearest accepted values; low/high/max pass through untouched."
        ),
    ),
    # deepseek-v4.1-flash follows the QwenCloud deepseek family: valid
    # high/max, and the endpoint itself maps low/medium->high, xhigh->max.
    # Those are VENDOR collapses (accepted, mapped server-side), so NOTHING is
    # rewritten — recorded as data per the module rule.
    ("qwen", "deepseek-v4.1-flash"): EffortPolicy(
        accepted=frozenset({"low", "medium", "high", "xhigh", "max"}),
        translate={},
        collapses={"low": "high", "medium": "high", "xhigh": "max"},
        doc_url="https://docs.qwencloud.com/api-reference/chat/anthropic",
        note=(
            "QwenCloud deepseek family: valid high/max (default max); low and "
            "medium are MAPPED TO high and xhigh TO max BY THE ENDPOINT, so "
            "all five values are accepted and the gateway rewrites none. "
            "Live: medium -> 200 (fixture medium-qwen-deepseek.body)."
        ),
    ),
    ("qwen", "qwen3.8-max"): EffortPolicy(
        accepted=frozenset({"low", "medium", "high", "xhigh", "max"}),
        translate={},
        collapses={"high": "xhigh", "max": "xhigh"},
        doc_url="https://docs.qwencloud.com/api-reference/chat/anthropic",
        note=(
            "QwenCloud doc: qwen3.8-max valid xhigh/medium/low (default "
            "xhigh); high and max are MAPPED TO xhigh BY THE ENDPOINT — a "
            "vendor collapse, not a rejection, so nothing is rewritten. "
            "Live: medium -> 200 (fixture medium-qwen-max.body)."
        ),
    ),
    ("qwen", "qwen3.8-flash"): EffortPolicy(
        accepted=frozenset({"low", "medium", "high", "xhigh", "max"}),
        translate={},
        collapses={"high": "xhigh", "max": "xhigh"},
        doc_url="https://docs.qwencloud.com/api-reference/chat/anthropic",
        note=(
            "Same Qwen3.8 family as qwen3.8-max (the doc names qwen3.8-max "
            "explicitly; the parity audit groups max/flash on one effort "
            "scale, xhigh default). translate is EMPTY, so even if flash's "
            "scale differed slightly the gateway would rewrite nothing — the "
            "safe direction."
        ),
    ),
    # ── z.ai GLM route ──────────────────────────────────────────────────
    # The doc says low/high/max, but medium was ACCEPTED live on both models,
    # so nothing is rewritten. xhigh on z.ai is UNVERIFIED (no capture): per
    # 'only rewrite a value the vendor would reject' it is left untouched
    # rather than guessed at — a wrong guess is its own failure.
    ("zai", "glm-5.3"): EffortPolicy(
        accepted=frozenset({"low", "medium", "high", "max"}),
        translate={},
        doc_url="https://docs.z.ai/guides/llm/glm-5.3",
        note=(
            "z.ai documents reasoning_effort low/high/max (default max), but "
            "medium was ACCEPTED live -> HTTP 200 (fixture "
            "medium-zai-glm.body), so nothing is rewritten. xhigh is "
            "UNVERIFIED on this route (no capture) and is deliberately left "
            "untranslated."
        ),
    ),
    ("zai", "glm-5.3-flash"): EffortPolicy(
        accepted=frozenset({"low", "medium", "high", "max"}),
        translate={},
        doc_url="https://docs.z.ai/guides/llm/glm-5.3",
        note=(
            "As glm-5.3 on z.ai: medium ACCEPTED live -> HTTP 200 (fixture "
            "medium-zai-flash.body). reasoning_effort is a GLM-5.2+ feature "
            "(z.ai thinking guide); nothing is rewritten."
        ),
    ),
}


def effort_policy(vendor_id: str, model_id: str) -> Optional[EffortPolicy]:
    """The :class:`EffortPolicy` for a route, or ``None`` when it has none."""
    return EFFORT_POLICIES.get((vendor_id, model_id))


def translate_effort(
    payload: Any, vendor_id: str, model_id: str,
) -> tuple[Any, bool]:
    """Rewrite a REJECTED ``output_config.effort`` for one (vendor, model).

    Returns ``(payload, changed)``. ``changed`` is False — and ``payload`` is
    the SAME object, not a copy — whenever there is nothing to do: no policy
    for the route, no ``output_config.effort``, a non-string effort, or a
    value the vendor accepts (which stays byte-identical). The body is only
    re-serialised by the caller when ``changed`` is True.

    ``thinking.budget_tokens`` is never touched (see the module docstring):
    the vendors ignore or deprecate it, none rejects it, and there is no
    honest integer mapping to apply.

    Vendor-neutral: the (vendor, model) key selects the data; this function
    names neither.
    """
    if not isinstance(payload, dict):
        return payload, False
    policy = EFFORT_POLICIES.get((vendor_id, model_id))
    if policy is None or not policy.translate:
        return payload, False
    output_config = payload.get("output_config")
    if not isinstance(output_config, dict):
        return payload, False
    effort = output_config.get("effort")
    if not isinstance(effort, str):
        return payload, False
    replacement = policy.translate.get(effort)
    if replacement is None:
        # Accepted (or a value with no evidence either way): byte-identical.
        return payload, False
    new_config = dict(output_config)
    new_config["effort"] = replacement
    new_payload = dict(payload)
    new_payload["output_config"] = new_config
    return new_payload, True


__all__ = [
    "EFFORT_POLICIES",
    "EFFORT_VALUES",
    "EffortPolicy",
    "effort_policy",
    "translate_effort",
]
