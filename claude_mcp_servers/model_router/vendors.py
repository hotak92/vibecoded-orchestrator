# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor registry — the ONLY module in this package that names a vendor.

The shipped scope is deliberately narrow: the routes that have been proven
end-to-end on a live machine — the Claude OAuth passthrough, the Z.ai/GLM
subscription route, and the QwenCloud Token Plan route (owner order
2026-09-21; the pay-as-you-go QwenCloud endpoint is deliberately absent until
its model list is extracted and proven — see the ``qwen`` row) — and the Kimi
Code subscription route (owner order 2026-10-07, K3 only). That last row was
written from the vendor's docs and then probed live the same day (endpoint,
auth, model list); every field cites its source, and the row says in place
which claim still waits on a live probe (the model echo). OpenRouter, the
Moonshot pay-as-you-go platform and the mainland-China Kimi endpoint are NOT
shipped, not even as disabled rows: a disabled row is untested code shipped
as config.

What IS the deliverable is the SHAPE. Adding a vendor must be a row in
:data:`VENDORS`, never a code change. ``tests/test_model_router_routing.py``
pins that structurally: it reads the sources of ``routing.py``, ``catalog.py``
and ``secrets.py`` and fails if any vendor-specific literal appears in them,
and it exercises the whole routing/catalog surface against a synthetic
second vendor built only from a :class:`Vendor` row.

Two things intentionally live here rather than in ``routing.py``:

* :data:`CLAUDE_ID_MARKERS` — the substrings Claude Code's gateway-model
  discovery keeps. It is a client contract, not a routing heuristic, and the
  namespace design is built on top of it.
* :data:`ANTHROPIC_FAMILY` — the Claude/Anthropic route. It is not a
  :class:`Vendor` because its credential is an OAuth token read from the CLI's
  own credentials file rather than a key resolved from vct-secrets, and its
  catalog is fetched with a different auth header. Modelling it as a "vendor
  with a weird auth mode" would have pushed an ``if vendor.is_anthropic``
  branch into every consumer — the opposite of the registry's purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

#: Substrings Claude Code's gateway-model discovery keeps in a ``/v1/models``
#: response, matched case-insensitively (Anthropic's gateway-protocol doc,
#: verbatim: "Claude Code keeps an entry when its ``id`` contains ``claude``
#: or ``anthropic`` anywhere in the string ... and ignores the rest").
#: Every vendor namespace must therefore contain one of these, or the client
#: silently drops the whole vendor catalog. :func:`validate_registry` enforces
#: it so a new row cannot be added that discovery would throw away.
CLAUDE_ID_MARKERS: tuple[str, ...] = ("claude", "anthropic")

#: The Anthropic Messages API version header. Claude Code always sends its
#: own; a bare client (curl, a test, a minimal SDK) may not, and BOTH
#: upstreams reject a request without it. Defaulting it here turns a confusing
#: relayed 400 into a working request.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: The beta header the Claude OAuth route requires. Verified live: without it
#: an OAuth bearer is rejected; with it the request returns HTTP 200.
OAUTH_BETA = "oauth-2025-04-20"

#: How the vendor's OWN maximum is read out of a matched context-overflow
#: message. QwenCloud's refusal states the accepted input range,
#: ``"Range of input length should be [1, 983616]"`` (live 400, 2026-10-06),
#: so the second number of the bracketed pair IS the ceiling the client needs
#: to see. Kept beside :attr:`Vendor.context_overflow_markers` because the
#: marker finds the message and this reads it — one home for the pair, so a
#: new vendor's shape lands in the same place its markers do.
CONTEXT_OVERFLOW_MAX_RE = re.compile(r"\[\s*1\s*,\s*(\d+)\s*\]")


def context_overflow_maximum(
    vendor: "Vendor", body: bytes,
) -> Optional[int]:
    """The vendor's real context ceiling out of its NATIVE overflow refusal.

    ``None`` when this body is not one of ``vendor``'s context-overflow
    refusals (no marker matched, or the range did not parse) — absence means
    "relay the vendor's own answer untouched", never "guess a ceiling". The
    detection lives here, data-driven off the row, so a new vendor's shape is
    a registry change and not a handler edit.
    """
    if not vendor.context_overflow_markers:
        return None
    text = body.decode("utf-8", "replace")
    if not any(marker in text for marker in vendor.context_overflow_markers):
        return None
    match = CONTEXT_OVERFLOW_MAX_RE.search(text)
    if match is None:
        return None
    maximum = int(match.group(1))
    return maximum if maximum > 0 else None


@dataclass(frozen=True)
class AnthropicFamily:
    """The first-party Claude route (OAuth passthrough, no vct-secrets key)."""

    family_id: str
    upstream: str
    catalog_path: str
    #: Query appended to the catalog fetch. Anthropic pages ``/v1/models``.
    catalog_query: str
    docs_url: str


@dataclass(frozen=True)
class Vendor:
    """One third-party Anthropic-shaped upstream.

    Attributes:
        vendor_id: stable internal id; also the key in :data:`VENDORS`, the
            ``vendor`` field of a chat-model-context row, and the per-family
            key in ``/health``'s ``catalog_source``.
        display_suffix: appended to the ``display_name`` shown in the picker
            so a user can see WHICH subscription answers an entry.
        display_name: short human name for the vendor, used in error text the
            GATEWAY authors (today: the quota message, where "<name> quota
            exhausted" has to name a subscription the user recognises).
            Optional — :func:`vendor_display_name` falls back to the suffix
            with its separator stripped, so a row that omits it still reads
            correctly and adding a vendor stays a one-row change.
        namespace: prefix that makes the vendor's ids survive Claude Code's
            discovery filter. MUST contain a :data:`CLAUDE_ID_MARKERS` entry
            and MUST end with a separator, so ``split`` is unambiguous.
        upstream: base URL of the vendor's Anthropic-shaped API.
        secret_keys: vct-secrets key names tried IN ORDER. More than one
            because the same subscription key is stored under different names
            by different users; the first that resolves wins and the miss
            message names all of them.
        bare_id_prefixes: unprefixed id prefixes that also route here, so a
            user who types the vendor's real id on the CLI is not forced to
            spell the namespace. Compared case-insensitively.
        catalog_path: path appended to ``upstream`` for the model list, or
            ``None`` for a vendor whose own upstream has NO model-list
            endpoint. ``None`` together with ``catalog_url is None`` means no
            discovery at all: the catalog is exactly the declared
            ``static_ids``, served under the ``declared`` source without a
            key or a fetch (see
            :func:`model_router.catalog.CatalogService._vendor_entries`).
            Unused while ``catalog_url`` is set.
        catalog_url: ABSOLUTE URL of the model list, used INSTEAD of
            ``upstream + catalog_path`` when set. For a vendor whose list
            endpoint lives on another base than its messages endpoint: the
            row then rides the identical live path (``live`` source, family
            floors, the auto ``[1m]`` advert, the truth filter). A row that
            sets it MUST also declare ``static_ids`` — the keyless and
            fetch-failed moments have nothing else to serve, and
            :func:`validate_registry` enforces both halves.
        catalog_exclude_prefixes: id prefixes dropped from the LIVE list
            before anything else sees it — the non-chat modalities a list
            endpoint may carry (voice, image, a router alias), which would
            otherwise be picker rows that answer an error. Compared
            case-insensitively; applied to fetched entries only, never to
            the row's curated ``static_ids``. Exclusion is NOT withholding:
            an excluded id is not reported in ``hidden`` (see
            :func:`model_router.catalog._exclude_by_prefix`). Every entry
            must be non-blank — the empty prefix matches every id, so a
            blank entry would drop the entire live list.
        auth_header / auth_scheme: how the resolved key is presented.
        docs_url: cited in user-facing copy about this vendor.
        alias_trap: True when the vendor's endpoint is DOCUMENTED or MEASURED
            to answer ``claude-*`` names with its own model. Purely informational —
            the honest-naming guard in :func:`model_router.routing.route`
            applies to every vendor unconditionally, because a vendor that has
            not documented an alias table can still add one tomorrow.
        static_ids: this vendor's OWN catalog list, served under the
            ``declared`` source whenever the live list cannot be — no key
            resolved yet, or the fetch failed. Read by
            :meth:`model_router.catalog.CatalogService._resolve_static_tables`.
            Empty on a row whose fallback is the shipped snapshot, which is
            the case that defers to ``static_catalog.json`` (and answers
            under the ``static`` source); set it and the row wins outright
            (see that method for why the more specific declaration takes
            precedence). Its point is that adding a vendor stays a config
            change: a new row can carry its own fallback without anyone
            editing a shipped JSON file. Ids are advertised verbatim under
            the vendor's namespace and double as their own display names, so
            they must be the vendor's real model ids. Two row shapes lean on
            the list harder than a plain live vendor and are REQUIRED to
            declare it (:func:`validate_registry`): a row with no discovery
            at all (``catalog_path is None``, no ``catalog_url``), where it
            is not a fallback but the WHOLE catalog, and a row with a
            ``catalog_url`` override, where it is the keyless/fetch-failed
            fallback.
        verified_ids: the ids this registry has VERIFIED answer as
            themselves on this vendor's endpoint. When non-empty, the catalog
            drops every entry of this vendor whose bare id is not in the list
            BEFORE window resolution and the latest-only filter (see
            :func:`model_router.catalog._publish_family`), reporting the
            dropped ids as withheld. For a vendor whose endpoint lists ids
            that reroute server-side to other models: the picker must not
            offer an id that answers as something else.
        quota_url: ABSOLUTE URL of the vendor's subscription-quota endpoint,
            or ``None`` when the vendor publishes no programmatic quota. Read
            by :mod:`model_router.usage_windows`, which knows one dialect per
            vendor id (its ``_QUOTA_DIALECTS`` table): Z.ai's monitor JSON
            (``data.limits[]`` with ``unit`` / ``number`` / ``percentage`` /
            ``nextResetTime``), key presented RAW in ``Authorization`` with
            no scheme; Kimi's usages JSON (``limits[].detail`` for the 5h
            window, ``usages.*.used_ratio`` for the monthly ones), key as
            ``x-api-key`` plus the relayed client user-agent. Both
            UNDOCUMENTED by the vendor; both parsed defensively and every
            miss reads "unknown". A row with ``None`` gets the honest
            fallback instead: the gateway's own ledger tokens for the
            current month, labelled as tokens, never as a percentage.
        short_name: the compact label the usage status line and the
            launcher's usage card print for this vendor (``GLM``, ``Qwen``).
            Falls back to :func:`vendor_display_name` when empty.
        partial_message_start_usage: True when this vendor's STREAMING
            ``message_start`` was MEASURED to lack the real input/cache
            figures, which arrive only in the final ``message_delta``. Two
            shapes were captured live 2026-10-03 (parity audit gap 3,
            fixtures in ``tests/fixtures/model_router/``): z.ai reports
            ``{"input_tokens": 0, "output_tokens": 0}`` and no cache fields;
            the qwen endpoint reports a small non-zero ``input_tokens`` that
            under-reads the final figure (4 where the delta says 36) and no
            cache fields. Claude Code stamps each content block's transcript
            entry with the usage it knows at that moment, so an all-zero
            ``message_start`` writes an all-zero transcript entry and the
            VS Code agent row keeps "resetting"; a partial one under-reads
            the session. For a flagged row the SSE rewriter lifts
            ``message_start``'s ``input_tokens`` to a positive FLOOR estimate
            (never lowers it, never touches the cache fields — see
            :class:`model_router.tool_ids.SseIdRewriter` and
            ``server.py::_message_start_usage_estimate``); the final
            ``message_delta`` carries the vendor's real figures, and because
            the accumulator's merge rule is last-positive-wins those real
            POSITIVES replace the estimate, so the ledger records the
            vendor's own numbers. The one case where it does not: a delta
            that repeats a ZERO for ``input_tokens`` never overwrites the
            positive the splice planted in ``message_start``, so that row
            keeps the estimate. Today's two measured vendors both send
            positives in the delta (the captured fixtures show it), so this
            is a boundary of the claim, not a live case. A row that does not declare the flag is
            relayed byte-identically, and a flagged row whose event already
            reports at least the floor is too — the splice fires on the
            measured deficiency, not on the flag alone.
        forward_verified_only: True when this vendor's endpoint was MEASURED
            to answer ANY model name with HTTP 200 and to echo the requested
            name back in the response's ``model`` field — a wildcard alias, so
            neither a 200 nor the echo says which model answered. For such a
            row the router forwards ONLY ``verified_ids`` (see
            :func:`model_router.routing._vendor_route`): every other id —
            even one the vendor's own list carries, and even one inside a
            ``bare_id_prefixes`` family — is refused locally like an unknown
            id, and only verified ids are offered as suggestions. Without the
            flag ``verified_ids`` narrows the PICKER only (the zai shape, whose
            endpoint 400s a nonsense name). :func:`validate_registry` requires
            a non-empty ``verified_ids`` on a flagged row.
    """

    vendor_id: str
    display_suffix: str
    namespace: str
    upstream: str
    secret_keys: tuple[str, ...]
    bare_id_prefixes: tuple[str, ...]
    catalog_path: Optional[str] = "/v1/models"
    catalog_url: Optional[str] = None
    catalog_exclude_prefixes: tuple[str, ...] = ()
    #: Ids curated out of the PUBLISHED picker under both catalog filters
    #: (owner ruling 2026-09-22: the final advertised list). Hidden, not
    #: excluded: reported in ``_vct_catalog_hidden`` and routable by name,
    #: deferred to a later curation discussion rather than dropped.
    catalog_hide_ids: tuple[str, ...] = ()
    #: Ids the gateway does NOT provide at all (owner ruling 2026-10-01): not
    #: published, not in any declared list, and REFUSED by name like any
    #: unknown id (the refusal suggests the nearest served id). Unlike
    #: ``catalog_hide_ids`` (hidden but routable) a retired id stays refused
    #: even when the vendor's live list still carries it. Exact ids,
    #: compared case-insensitively.
    retired_ids: tuple[str, ...] = ()
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer "
    docs_url: str = ""
    alias_trap: bool = False
    static_ids: tuple[str, ...] = ()
    display_name: str = ""
    verified_ids: tuple[str, ...] = ()
    quota_url: Optional[str] = None
    short_name: str = ""
    partial_message_start_usage: bool = False
    forward_verified_only: bool = False
    #: Substrings identifying this vendor's NATIVE context-overflow refusal,
    #: whatever envelope it arrives in (JSON body, SSE ``event: error``). A
    #: vendor answers this shape when a request is larger than the model's
    #: REAL window — the case where the client, budgeting the 1M it assumes
    #: for any ``[1m]`` id, never compacted and is now stuck retrying a
    #: request the vendor will always refuse. :func:`server`'s translation
    #: reads these markers through :func:`context_overflow_maximum` and
    #: re-emits the refusal as Anthropic's ``prompt is too long`` envelope,
    #: which the client recognises and recovers from by compacting. EMPTY for
    #: a vendor whose overflow shape is not documented or captured — an empty
    #: tuple means "relay verbatim", the behaviour every unlisted vendor had
    #: before the translation existed.
    context_overflow_markers: tuple[str, ...] = ()


ANTHROPIC_FAMILY = AnthropicFamily(
    family_id="anthropic",
    upstream="https://api.anthropic.com",
    catalog_path="/v1/models",
    catalog_query="limit=100",
    docs_url="https://platform.claude.com/docs/en/about-claude/models/overview",
)

#: The namespace shared by every shipped vendor. Kept as one value because the
#: user-visible ids (``claude-gw/glm-5.3``) are already in the field; a second
#: namespace would be a second thing for users to learn for no gain. A new row
#: MAY use its own namespace — nothing in the code assumes there is only one —
#: and a row may EXTEND this one (``claude-gw/<vendor>/``): namespace
#: resolution picks the longest match, so the extension cannot be shadowed by
#: the shorter prefix while every panel rule that keys on ``claude-gw/`` still
#: holds for both.
GATEWAY_NAMESPACE = "claude-gw/"

VENDORS: Mapping[str, Vendor] = {
    "zai": Vendor(
        vendor_id="zai",
        display_suffix=" · Z.ai subscription",
        namespace=GATEWAY_NAMESPACE,
        upstream="https://api.z.ai/api/anthropic",
        display_name="Z.ai",
        secret_keys=("glm_api_key", "zai_api_key"),
        bare_id_prefixes=("glm",),
        catalog_path="/v1/models",
        docs_url="https://docs.z.ai/devpack/tool/claude",
        # Documented by the vendor: their Claude Code page states the endpoint
        # maps Claude tier names onto GLM models server-side. A live probe on
        # 2026-09-02 returned HTTP 200 with model=glm-5.3-flash for every
        # claude-* name tried, and a nonsense name 400'd — i.e. a deliberate
        # alias table, not a wildcard accept.
        #
        # Verified again 2026-09-20 (issue 9's manual half): a direct probe
        # with the same key asking for bare glm-5.3 got model=glm-5.3 echoed —
        # served honestly for non-aliased ids on the non-streaming shape.
        # Whether the STREAMING message_start carries a truthful model field
        # remains unverified, which is exactly why the automatic check below
        # must not fire on absence of the field: it compares whatever shape
        # IS present. server.py's model-echo assertion (the
        # model_echo_mismatches counter in /health) is the automatic version
        # of this manual probe and the tripwire for the next vendor-side
        # aliasing change.
        alias_trap=True,
        # Owner ruling 2026-09-21: only these two ids answer as themselves on
        # this endpoint; the older ids it still LISTS reroute server-side to
        # other models. The catalog therefore withholds every other listed id
        # (see `verified_ids` on Vendor) — a picker row that answers as a
        # different model is the alias trap wearing a list.
        verified_ids=("glm-5.3", "glm-5.3-flash"),
        # The owner retired glm-5.2 in v0.2.100: only glm-5.3 is provided.
        retired_ids=("glm-5.2",),
        # Probed live 2026-09-23: 5h (unit 3, number 5) and weekly (unit 6,
        # number 1) windows, both type CREDIT_LIMIT. The endpoint is
        # undocumented and its schema changed during 2026 (older answers
        # carried TOKENS_LIMIT and no weekly row) — see
        # model_router.usage_windows.parse_zai_quota for how both read.
        quota_url="https://api.z.ai/api/monitor/usage/quota/limit",
        short_name="GLM",
        # Measured on this machine's transcripts 2026-10-03: 110 of 598 GLM
        # assistant entries carried ALL-ZERO usage, because the endpoint's
        # streamed message_start reports input_tokens 0 / output_tokens 0 and
        # the real figures arrive only in message_delta (live capture:
        # tests/fixtures/model_router/zai-stream.body). usage.py's merge rule
        # already keeps the LEDGER correct for this shape; the flag fixes what
        # the CLIENT's transcript sees.
        partial_message_start_usage=True,
    ),
    "qwen": Vendor(
        vendor_id="qwen",
        display_suffix=" · QwenCloud Token Plan",
        display_name="QwenCloud",
        namespace="claude-gw/qwen/",
        upstream="https://token-plan.maas.qwencloudapi.com/apps/anthropic",
        # Both spellings: the resolver is name-EXACT and the launcher
        # keychain stores the name the user typed. The canonical name is
        # first; the uppercase spelling covers keys saved from the
        # vendor's own docs/env convention (QWEN_API_KEY) — the same
        # two-name pattern the zai row uses.
        secret_keys=("qwen_api_key", "QWEN_API_KEY"),
        bare_id_prefixes=("qwen", "deepseek"),
        # The anthropic app path above has NO /v1/models (probe 2026-09-22:
        # "Not support"), but the subscription's OpenAI-compatible base DOES
        # serve the same {"data": [{"id": ...}]} envelope the catalog already
        # parses. The row therefore rides the ordinary LIVE path through this
        # absolute-URL override, and catalog_path keeps its default (unused
        # while catalog_url is set).
        catalog_url=(
            "https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/models"
        ),
        # The live list carries non-chat ids (probe 2026-09-22: two
        # qwen-audio voice rows, two wan image rows, and the "auto" router
        # alias; qwen-asr listed for the shape the vendor's docs describe).
        # A chat picker row that answers an error is the deprecated-model
        # trap worn by a different list, so these never become rows.
        # Exclusion applies to the LIVE list only — the curated static_ids
        # below need no filter.
        # Dated deepseek flash snapshots are curated out (owner ruling
        # 2026-09-22): the versioned line (deepseek-v4.1-flash) is the
        # published flash; the prefix matches only dated builds, never it.
        catalog_exclude_prefixes=("qwen-audio", "qwen-asr", "wan", "auto",
                                "deepseek-v4-flash-"),
        catalog_hide_ids=("qwen3.7-plus", "deepseek-v4-pro"),
        # The owner retired glm-5.2 in v0.2.100 (every route): the live list
        # still carries it, the gateway neither publishes nor routes it.
        retired_ids=("glm-5.2",),
        docs_url="https://docs.qwencloud.com/developer-guides/clients-and-developer-tools/claude-code",
        # No claude-* alias table is documented for this endpoint (UNVERIFIED
        # either way); routing's honest-naming guard applies regardless, and
        # server.py's model_echo_mismatches counter is the tripwire for an
        # undocumented one.
        alias_trap=False,
        # The keyless / fetch-failed fallback, live-verified against the
        # compatible-mode list (probe 2026-09-22): the fifteen live ids minus
        # the six excluded (five non-chat modalities plus the dated deepseek
        # flash snapshot, owner-curated out) and glm-5.2 (retired, below). Publishing trims two more via
        # catalog_hide_ids below. validate_registry requires static_ids on
        # a catalog_url row.
        static_ids=(
            "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
            "qwen3.6-flash", "glm-5.3", "deepseek-v4.1-flash",
            "deepseek-v4-pro",
        ),
        # No quota_url: the Token Plan is monthly credits with no endpoint
        # and no quota headers (probed 2026-09-23), so usage shows the
        # gateway ledger's tokens for the month instead of a percentage.
        short_name="Qwen",
        # The NATIVE context-overflow refusal, live-captured 2026-10-06 (the
        # S16-10 incident): a qwen3.8-max subagent past the endpoint's real
        # ceiling answered HTTP 400 ``InvalidParameter`` with
        # ``"Range of input length should be [1, 983616]"`` — a shape Claude
        # Code's error classifier does NOT map to "prompt too long", so its
        # reactive compaction never fired and the agent was stuck (it had
        # budgeted 1M for the ``[1m]`` id and the vendor caps at 983,616).
        # Only the code and message fragments above are from the record; the
        # envelope around them (``error.code``/``error.message``) is the
        # compatible-mode shape. Matched on the MESSAGE substring so the
        # envelope can vary without re-learning the shape.
        context_overflow_markers=("Range of input length should be",),
        # Live capture 2026-10-03 (parity audit gap 3; fixtures
        # tests/fixtures/model_router/qwen-deepseek-stream.body,
        # qwen-max-stream.body): message_start reports a small non-zero
        # input_tokens that under-reads the final figure (4 at the start,
        # 36 in the delta on the same turn) and NO cache fields — the same
        # deficiency as z.ai's zeros, one degree milder. The floor splice
        # only ever LIFTS the figure, so a turn whose real input already
        # exceeds the floor is relayed byte-identically.
        partial_message_start_usage=True,
    ),
    # Kimi Code — the Kimi membership's coding subscription (owner order
    # 2026-10-07). Docs read 2026-10-07; the pages:
    #   overview   https://www.kimi.com/code/docs/
    #   models     https://www.kimi.com/code/docs/kimi-code/models.html
    #   Claude Code https://www.kimi.com/code/docs/third-party-tools/claude-code.html
    #   errors     https://www.kimi.com/code/docs/kimi-code/error-reference.html
    #   membership https://www.kimi.com/code/docs/kimi-code/membership.html
    # LIVE-PROBED 2026-10-07 with the owner's key (coordinator probe): the
    # messages endpoint, both auth headers, the model list, a Cloudflare
    # user-agent gate and a wildcard alias — each noted at its field below.
    "kimi": Vendor(
        vendor_id="kimi",
        display_suffix=" · Kimi Code subscription",
        display_name="Kimi Code",
        # Extends the shared namespace like the qwen row, so every panel rule
        # keyed on ``claude-gw/`` holds and the longest-match resolution keeps
        # the shorter namespace from shadowing it.
        namespace="claude-gw/kimi/",
        # The INTERNATIONAL Anthropic-compatible base (overview + models
        # pages: "Anthropic 兼容 ... 海外 https://api.kimi.ai/coding/"); the
        # gateway appends ``/v1/messages`` exactly as Claude Code does ("会自动
        # 拼接后续路径"), giving the documented example endpoint
        # ``.../coding/v1/messages`` (live 200, 2026-10-07, Anthropic-shaped
        # answers incl. ``thinking`` blocks). Trailing slash dropped because
        # the forwarded path starts with one. The mainland-China base
        # (api.kimi.com, also live 200) is a different storefront and is not
        # shipped.
        #
        # USER-AGENT GATE (live 2026-10-07): the endpoint sits behind a
        # Cloudflare rule that answers 403 "error code: 1010" to a library
        # user-agent (Python-urllib) and 200 to Claude Code's
        # ``claude-cli/...`` one. The gateway therefore relays the CLIENT's
        # own user-agent — on /v1/messages (the generic header relay) and on
        # the model-list fetch a client's /v1/models triggers
        # (``CatalogService.union(user_agent=...)``). It never invents one:
        # the vendor's terms make tampering with the client identity a
        # violation ("篡改客户端标识（User-Agent）将被视为违规", overview page).
        upstream="https://api.kimi.ai/coding",
        # ``KIMI_API_KEY`` is the name the owner's launcher keychain holds
        # (shared scope, 2026-10-07) and the Kimi Code CLI's own credential
        # name; ``kimi_api_key`` is the lowercase canonical spelling of the
        # zai/qwen rows — the same two-name pattern. ``MOONSHOT_API_KEY`` is
        # deliberately NOT tried: that name belongs to the Moonshot open
        # platform's pay-as-you-go key, which the FAQ says is a DIFFERENT key
        # for a different base URL
        # (https://www.kimi.com/code/docs/kimi-code/faq.html, "注意区分平台"),
        # so resolving it would send a platform key to the coding endpoint —
        # or borrow a project's own platform key for VCO's use.
        secret_keys=("kimi_api_key", "KIMI_API_KEY"),
        # ``k3`` typeable without the namespace. Any other ``k3…`` id lands
        # on this row and is refused there with ``claude-gw/kimi/k3`` as the
        # suggestion (``forward_verified_only``) — rather than falling
        # through to the generic unknown-model refusal.
        bare_id_prefixes=("k3",),
        # ``GET <upstream>/v1/models`` answers an OpenAI-style
        # ``{"data": [...]}`` list (live 200, 2026-10-07) — the envelope the
        # catalog already parses — so the row rides the ordinary LIVE path.
        # Its ``context_length`` is read as the publisher's window
        # (``CatalogService._parse_models``), below the cited seed row for k3
        # — which states the same 1048576 the live list reports (live
        # /v1/models probe 2026-10-07). ``supports_image_in`` (true for k3)
        # and ``think_efforts`` (low/high/max, default high) are not read;
        # the seed row and effort.py record them.
        catalog_path="/v1/models",
        # Claude Code is told to set ANTHROPIC_API_KEY (Claude Code page,
        # step 3), which it sends as ``X-Api-Key``. Live 2026-10-07: the
        # endpoint accepts BOTH ``x-api-key`` and ``Authorization: Bearer``;
        # the gateway uses the one the vendor's own Claude Code setup uses.
        auth_header="x-api-key",
        auth_scheme="",
        docs_url="https://www.kimi.com/code/docs/third-party-tools/claude-code.html",
        # A WILDCARD ALIAS, measured live 2026-10-07 (coordinator probe with
        # the owner's key): POST /v1/messages with model ``nonsense-model-xyz``
        # and with ``claude-sonnet-5-5`` both answered HTTP 200, and the
        # response's ``model`` field ECHOED whatever was requested. (The docs
        # claim otherwise — Claude Code page: a Claude default name "报模型不
        # 存在错误"; error reference: 401 "Your model id does not exist" — the
        # live endpoint does not behave that way.) So a 200 and the echo are
        # NOT evidence of which model answered, and server.py's model-echo
        # counter (model_echo_mismatches) can never fire for this vendor: it
        # must not be credited as verification here. The defence is the
        # router's: ``forward_verified_only`` below forwards only the
        # verified id and refuses everything else locally; the honest-naming
        # guard additionally refuses any Claude-marked id, as for every
        # vendor. One further DOCUMENTED substitution the gateway cannot
        # see: with thinking turned OFF the vendor answers k3 requests with
        # K2.8 Preview without thinking ("关闭 thinking 后路由到 K2.8 Preview
        # 无思考版"); the live list reports k3's thinking as "only" (always
        # on), which suggests the off switch does not apply to k3 — UNVERIFIED.
        alias_trap=True,
        forward_verified_only=True,
        # OWNER RULING 2026-10-07: the gateway offers K3 ONLY ("only need
        # K3"). The vendor serves four ids (models page, "共 4 个模型 ID"; the
        # live list carries the same four). ``k3`` is the one offered and
        # the one forwarded; ``k3-256k`` (the same model at a 256K window,
        # "K3 的 256K 上下文版本"), ``kimi-for-coding`` (K2.8 Preview) and
        # ``kimi-for-coding-highspeed`` (K2.7 Code HighSpeed) are WITHHELD
        # from the picker (reported in ``_vct_catalog_hidden``; the
        # VCT_MODEL_GATEWAY_CATALOG=all knob does not bring them back) and
        # REFUSED by the router (``forward_verified_only``). "Verified" here
        # means the owner's scope ruling plus the vendor's own list and docs
        # naming k3 as K3 — it cannot mean "answered as itself", because this
        # endpoint echoes any name (see alias_trap).
        verified_ids=("k3",),
        # No static_ids: a standard-discovery row's keyless / fetch-failed
        # fallback is the shipped snapshot (``static_catalog.json``, family
        # ``kimi``: the ids the live list carried on 2026-10-07), served
        # under the ``static`` source and narrowed by verified_ids like a
        # live answer.
        # LIVE-PROBED 2026-10-07 with the owner's key (coordinator probe):
        # ``GET https://api.kimi.ai/coding/v1/usages`` answered HTTP 200 with
        # ``x-api-key`` (raw), a client user-agent — the SAME Cloudflare gate
        # as the messages endpoint; a library UA gets 403 "error code: 1010"
        # — and ``anthropic-version``. The body is saved verbatim as
        # tests/fixtures/model_router/kimi-usages.json and read by
        # model_router.usage_windows.parse_kimi_usages. Undocumented by the
        # vendor, parsed defensively like every other quota source.
        quota_url="https://api.kimi.ai/coding/v1/usages",
        short_name="Kimi",
        # partial_message_start_usage stays False: the flag is a claim about
        # MEASURED streaming behaviour and no Kimi stream has been captured.
    ),
}


class RegistryError(ValueError):
    """A vendor row cannot work as written."""


def validate_registry(vendors: Mapping[str, Vendor] | None = None) -> None:
    """Raise :class:`RegistryError` if any row would silently misbehave.

    Called by ``vct-model-gateway --check`` and by the server factory, so a
    user who adds a row gets a named error at startup instead of a model that
    quietly never appears in the picker.
    """
    rows = VENDORS if vendors is None else vendors
    seen_namespaces: dict[str, str] = {}
    seen_prefixes: dict[str, str] = {}
    for key, vendor in rows.items():
        if key != vendor.vendor_id:
            raise RegistryError(
                f"registry key {key!r} does not match vendor_id "
                f"{vendor.vendor_id!r}",
            )
        if not vendor.namespace:
            raise RegistryError(f"vendor {key!r}: empty namespace")
        lowered = vendor.namespace.lower()
        if not any(marker in lowered for marker in CLAUDE_ID_MARKERS):
            raise RegistryError(
                f"vendor {key!r}: namespace {vendor.namespace!r} contains none "
                f"of {CLAUDE_ID_MARKERS!r}, so Claude Code's gateway-model "
                "discovery would drop every one of this vendor's entries",
            )
        if vendor.namespace[-1].isalnum():
            raise RegistryError(
                f"vendor {key!r}: namespace {vendor.namespace!r} must end with "
                "a separator (e.g. '/') so the model id splits unambiguously",
            )
        clash = seen_namespaces.get(vendor.namespace)
        if clash is not None and clash != vendor.vendor_id:
            # Two vendors CAN share a namespace only if their bare-id prefixes
            # disambiguate; sharing it outright makes routing order-dependent.
            raise RegistryError(
                f"vendors {clash!r} and {vendor.vendor_id!r} share namespace "
                f"{vendor.namespace!r} — routing would be order-dependent",
            )
        seen_namespaces.setdefault(vendor.namespace, vendor.vendor_id)
        if not vendor.secret_keys:
            raise RegistryError(f"vendor {key!r}: no secret_keys declared")
        if not vendor.upstream.startswith(("http://", "https://")):
            raise RegistryError(
                f"vendor {key!r}: upstream {vendor.upstream!r} is not an "
                "http(s) URL",
            )
        for prefix in vendor.bare_id_prefixes:
            if any(marker in prefix.lower() for marker in CLAUDE_ID_MARKERS):
                raise RegistryError(
                    f"vendor {key!r}: bare_id_prefix {prefix!r} contains a "
                    "Claude marker, which would hijack first-party Claude ids",
                )
            # Bare prefixes resolve LONGEST-FIRST, so two vendors can share a
            # prefix when one's is strictly longer (e.g. "glm" vs "glm-5.3").
            # An EXACT collision between two different vendors, though, makes
            # routing order-dependent: whichever row the mapping iterates last
            # (or first, on a rebuilt dict) silently wins the other's ids.
            # Compared case-insensitively like the router itself does.
            owner = seen_prefixes.get(prefix.lower())
            if owner is not None and owner != vendor.vendor_id:
                raise RegistryError(
                    f"vendors {owner!r} and {vendor.vendor_id!r} both declare "
                    f"bare_id_prefix {prefix!r} — bare-id routing would be "
                    "order-dependent",
                )
            seen_prefixes.setdefault(prefix.lower(), vendor.vendor_id)
        if (
            vendor.catalog_path is None
            and vendor.catalog_url is None
            and not vendor.static_ids
        ):
            raise RegistryError(
                f"vendor {key!r}: catalog_path is None (no model-list "
                "endpoint) but static_ids is empty — the picker would show "
                "nothing for this vendor",
            )
        if vendor.catalog_url is not None:
            if not vendor.catalog_url.startswith(("http://", "https://")):
                raise RegistryError(
                    f"vendor {key!r}: catalog_url {vendor.catalog_url!r} is "
                    "not an absolute http(s) URL — it REPLACES "
                    "upstream + catalog_path, so a relative path here would "
                    "be fetched as a malformed URL",
                )
            if not vendor.static_ids:
                # The override rides the live path, but a live path needs a
                # key and an answering endpoint; the keyless and
                # fetch-failed moments have nothing to serve unless the row
                # declares its own list.
                raise RegistryError(
                    f"vendor {key!r}: catalog_url overrides the model-list "
                    "fetch but static_ids is empty — with no key resolved, "
                    "or a failed fetch, the picker would show nothing for "
                    "this vendor",
                )
        if vendor.forward_verified_only and not vendor.verified_ids:
            raise RegistryError(
                f"vendor {key!r}: forward_verified_only is set but "
                "verified_ids is empty — the router would refuse every id",
            )
        if vendor.quota_url is not None and not vendor.quota_url.startswith(
            ("http://", "https://"),
        ):
            raise RegistryError(
                f"vendor {key!r}: quota_url {vendor.quota_url!r} is not an "
                "absolute http(s) URL",
            )
        for exclude_prefix in vendor.catalog_exclude_prefixes:
            if not exclude_prefix.strip():
                raise RegistryError(
                    f"vendor {key!r}: catalog_exclude_prefixes contains a "
                    "blank entry — every id starts with the empty string, "
                    "so it would exclude the entire live list",
                )
        for hide_id in vendor.catalog_hide_ids:
            # A hide entry is matched by EXACT id, so whitespace or a typo
            # hides nothing and says nothing — the curated id keeps appearing
            # in the picker and the row's author has no signal. The other
            # list fields are gated the same way; this one shipped without a
            # gate and that asymmetry is the whole defect.
            if not hide_id.strip():
                raise RegistryError(
                    f"vendor {key!r}: catalog_hide_ids contains a blank entry",
                )
            if hide_id != hide_id.strip():
                raise RegistryError(
                    f"vendor {key!r}: catalog_hide_ids entry {hide_id!r} has "
                    "surrounding whitespace — matching is exact, so it would "
                    "hide nothing, silently",
                )
            if vendor.static_ids and hide_id.lower() not in {
                declared.lower() for declared in vendor.static_ids
            }:
                # Only checkable when the row declares its own list: a live
                # list can legitimately carry an id the row has never seen.
                raise RegistryError(
                    f"vendor {key!r}: catalog_hide_ids entry {hide_id!r} is "
                    "not among this row's static_ids — a hide that matches "
                    "no declared id hides nothing",
                )
        for retired in vendor.retired_ids:
            if not retired.strip() or retired != retired.strip():
                raise RegistryError(
                    f"vendor {key!r}: retired_ids entry {retired!r} is blank "
                    "or has surrounding whitespace — matching is exact, so "
                    "it would retire nothing, silently",
                )
            offered = {
                i.lower() for i in (
                    *vendor.static_ids, *vendor.verified_ids,
                    *vendor.catalog_hide_ids,
                )
            }
            if retired.lower() in offered:
                raise RegistryError(
                    f"vendor {key!r}: retired_ids entry {retired!r} is also "
                    "declared as served (static/verified/hidden) — an id "
                    "cannot be both offered and retired",
                )
        for static_id in vendor.static_ids:
            if not static_id.strip():
                raise RegistryError(
                    f"vendor {key!r}: static_ids contains a blank entry",
                )
            if any(marker in static_id.lower() for marker in CLAUDE_ID_MARKERS):
                # This one is subtler than the bare-prefix check above and is
                # worth spelling out. A static id is advertised verbatim under
                # the vendor's namespace, so a Claude-marked entry would put a
                # row in the picker that `routing.route` is REQUIRED to refuse
                # by the honest-naming rule. The user would see a model, pick
                # it, and get a 400 — a fallback list that hands out ids the
                # router will not honour. Caught at startup instead.
                raise RegistryError(
                    f"vendor {key!r}: static_id {static_id!r} contains a "
                    "Claude marker, so it would be advertised and then "
                    "refused by the honest-naming guard when selected",
                )
        for verified_id in vendor.verified_ids:
            if not verified_id.strip():
                raise RegistryError(
                    f"vendor {key!r}: verified_ids contains a blank entry",
                )
            if any(marker in verified_id.lower() for marker in CLAUDE_ID_MARKERS):
                # Same shape as the static_id check above, but worse: a
                # Claude-marked verified id would be the ONLY entry of its
                # family kept by the truth filter — a picker row that
                # `routing.route` is REQUIRED to refuse. The filter would
                # actively select for an un-routable id.
                raise RegistryError(
                    f"vendor {key!r}: verified_id {verified_id!r} contains a "
                    "Claude marker, so the truth filter would keep the one "
                    "id the honest-naming guard refuses",
                )


def vendor_display_name(vendor: Vendor) -> str:
    """The name to put in gateway-authored user-facing text.

    Precedence: the explicit ``display_name``, else the ``display_suffix``
    with its leading separator and padding removed, else the ``vendor_id``.
    Never empty — an error message that names no vendor is the message this
    whole exercise replaced.
    """
    if vendor.display_name.strip():
        return vendor.display_name.strip()
    trimmed = vendor.display_suffix.strip().lstrip("·-–—|/ ").strip()
    return trimmed or vendor.vendor_id


@dataclass(frozen=True)
class FallbackRung:
    """One edge of the owner's model-fallback ladder (OD-9, v0.2.102 A4/F2).

    A rung names WHERE a qualifying quota refusal escalates TO, never the
    condition (the trigger lives in ``model_router.server`` beside the quota
    classifier it reads). Data, like every vendor fact, so the ladder is a
    table edit rather than a code change.

    Attributes:
        from_vendor_id: the route being left. ``""`` names the FIRST-PARTY
            Claude route, whose rung is keyed on a model MARKER rather than an
            exact id (any haiku-slot id), because the client spells that slot
            with whatever generation it ships.
        from_model: the exact model id of the route being left (matched
            case-insensitively); on the first-party rung, a SUBSTRING marker
            matched against the forwarded id.
        to_vendor_id: the registry key of the rung's upstream.
        to_model_id: the bare id the rung forwards. MUST be an id its vendor
            row is known to serve (``verified_ids``/``static_ids``) — a
            fallback to an id the router would refuse is a ladder that ends in
            a local 400 instead of an answer.
    """

    from_vendor_id: str
    from_model: str
    to_vendor_id: str
    to_model_id: str


#: The owner's fallback ladder (OD-9, 2026-10-07): a Claude haiku-slot request
#: whose first-party answer is a qualifying 429 escalates to glm-5.3-flash on
#: the z.ai subscription; a z.ai quota verdict on that rung escalates to
#: qwen3.8-flash on QwenCloud — the vendor→vendor edge walked only by an
#: attempt that ARRIVED at its rung by fallback (batch-6 B-1, coordinator
#: 2026-10-07; a user-picked vendor model keeps its own refusal). The last
#: rung has no entry, so its refusal IS
#: the client's answer — the ladder never fabricates a success. Read by
#: :func:`model_router.routing.fallback_route`, the vendor-neutral half; the
#: trigger (which refusals qualify) is OD-9, implemented in
#: ``model_router.server``. The haiku slot is where the incident bit
#: (P299:106/:406): a first-party 429 rendered as a bare "Claude API Error"
#: toast and the session sat idle while two healthy subscriptions served
#: nothing. Ollama and OpenAI routes are untouched — none ship, and the ladder
#: adds none.
FALLBACK_LADDER: tuple[FallbackRung, ...] = (
    FallbackRung(
        from_vendor_id="",
        from_model="haiku",
        to_vendor_id="zai",
        to_model_id="glm-5.3-flash",
    ),
    FallbackRung(
        from_vendor_id="zai",
        from_model="glm-5.3-flash",
        to_vendor_id="qwen",
        to_model_id="qwen3.8-flash",
    ),
)


__all__ = [
    "ANTHROPIC_FAMILY",
    "AnthropicFamily",
    "CLAUDE_ID_MARKERS",
    "CONTEXT_OVERFLOW_MAX_RE",
    "DEFAULT_ANTHROPIC_VERSION",
    "FALLBACK_LADDER",
    "FallbackRung",
    "GATEWAY_NAMESPACE",
    "OAUTH_BETA",
    "RegistryError",
    "VENDORS",
    "Vendor",
    "context_overflow_maximum",
    "validate_registry",
    "vendor_display_name",
]
