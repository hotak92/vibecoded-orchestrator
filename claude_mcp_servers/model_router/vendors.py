# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor registry — the ONLY module in this package that names a vendor.

The shipped scope is deliberately narrow: exactly the two routes that have been
proven end-to-end on a live machine — the Claude OAuth passthrough and the
Z.ai/GLM subscription route. Kimi, Qwen and OpenRouter are NOT shipped, not
even as disabled rows: a disabled row is untested code shipped as config.

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

from dataclasses import dataclass
from typing import Mapping

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
        catalog_path: path appended to ``upstream`` for the model list.
        auth_header / auth_scheme: how the resolved key is presented.
        docs_url: cited in user-facing copy about this vendor.
        alias_trap: True when the vendor's endpoint is DOCUMENTED to answer
            ``claude-*`` names with its own model. Purely informational —
            the honest-naming guard in :func:`model_router.routing.route`
            applies to every vendor unconditionally, because a vendor that has
            not documented an alias table can still add one tomorrow.
        static_ids: this vendor's OWN fallback catalog, served when a live
            fetch fails. Read by
            :meth:`model_router.catalog.CatalogService._resolve_static_tables`.
            Empty on every shipped row, which is the case that defers to
            ``static_catalog.json``; set it and the row wins outright (see
            that method for why the more specific declaration takes
            precedence). Its point is that adding a vendor stays a config
            change: a new row can carry its own fallback without anyone
            editing a shipped JSON file. Ids are advertised verbatim under
            the vendor's namespace and double as their own display names, so
            they must be the vendor's real model ids.
    """

    vendor_id: str
    display_suffix: str
    namespace: str
    upstream: str
    secret_keys: tuple[str, ...]
    bare_id_prefixes: tuple[str, ...]
    catalog_path: str = "/v1/models"
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer "
    docs_url: str = ""
    alias_trap: bool = False
    static_ids: tuple[str, ...] = ()
    display_name: str = ""


ANTHROPIC_FAMILY = AnthropicFamily(
    family_id="anthropic",
    upstream="https://api.anthropic.com",
    catalog_path="/v1/models",
    catalog_query="limit=100",
    docs_url="https://docs.anthropic.com/en/api/models-list",
)

#: The namespace shared by every shipped vendor. Kept as one value because the
#: user-visible ids (``claude-gw/glm-5.3``) are already in the field; a second
#: namespace would be a second thing for users to learn for no gain. A new row
#: MAY use its own namespace — nothing in the code assumes there is only one.
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
        docs_url="https://docs.z.ai/devguide/interface/claude-code",
        # Documented by the vendor: their Claude Code page states the endpoint
        # maps Claude tier names onto GLM models server-side. A live probe on
        # 2026-09-02 returned HTTP 200 with model=glm-5.3-flash for every
        # claude-* name tried, and a nonsense name 400'd — i.e. a deliberate
        # alias table, not a wildcard accept.
        alias_trap=True,
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


__all__ = [
    "ANTHROPIC_FAMILY",
    "AnthropicFamily",
    "CLAUDE_ID_MARKERS",
    "DEFAULT_ANTHROPIC_VERSION",
    "GATEWAY_NAMESPACE",
    "OAUTH_BETA",
    "RegistryError",
    "VENDORS",
    "Vendor",
    "validate_registry",
    "vendor_display_name",
]
