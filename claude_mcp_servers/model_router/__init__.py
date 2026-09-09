# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""VCT model gateway — one local Anthropic-shaped endpoint, two model families.

Claude Code talks to exactly ONE Messages endpoint per session, and its
gateway-model discovery keeps only model ids whose string contains ``claude``
or ``anthropic`` (case-insensitive; Anthropic's own gateway-protocol doc states
this verbatim). A user holding BOTH a Claude subscription and a vendor
subscription therefore cannot see both catalogs in one ``/model`` picker.

This package is the gateway that lifts that ceiling::

    Claude Code (CLI or VS Code panel)
       |  ANTHROPIC_BASE_URL=http://127.0.0.1:<port> + the local host token
       v
    vct-model-gateway
       |- model claude-*          -> api.anthropic.com, OAuth read per request
       |                             from the Claude CLI's credentials file
       |- model claude-gw/<id>    -> the vendor named by the namespace, with
       |                             the prefix STRIPPED from the forwarded
       |                             body (the id names the model that answers)
       '- GET /v1/models          -> union catalog, per-family cache with a
                                     shipped static fallback

Honest naming is a design invariant, not a preference
-----------------------------------------------------
The ``claude-gw/`` prefix is a NAMESPACE, not a mislabel: the id names the
exact model that answers and the gateway rewrites it back before forwarding.
That distinction is load-bearing because the alternative is documented vendor
behaviour: Z.ai's own Claude Code documentation says its endpoint maps Claude
tier names onto GLM models server-side ("you see the Claude model in the
interface but the GLM model is actually used"), and a live probe against the
current endpoint confirms that ANY ``claude-*`` name returns HTTP 200 with the
response's ``model`` field reading ``glm-5.3-flash``. A picker that shows
Claude names against such an endpoint is showing labels, not models. So
:func:`model_router.routing.route` refuses to forward any id containing
``claude``/``anthropic`` to a vendor upstream — that is a local 400, not a
silent substitution.

Module map
----------
* :mod:`model_router.vendors` — the DATA registry. Adding a vendor is a row
  here; no other module carries a vendor-specific literal.
* :mod:`model_router.routing` — pure routing decisions (no I/O).
* :mod:`model_router.context_table` — the version-keyed chat-model context
  table (EXACT full-model-id keys only).
* :mod:`model_router.catalog` — the union ``/v1/models`` catalog.
* :mod:`model_router.auth` — local host token + the Claude OAuth reader.
* :mod:`model_router.secrets` — vendor keys, resolved at request time.
* :mod:`model_router.server` — the aiohttp application.
* :mod:`model_router.config` — path/port resolution.
* :mod:`model_router.fileperms` — owner-only file permissions, cross-OS.

This module deliberately imports nothing beyond the stdlib so that a bare
``import model_router`` is a valid post-install smoke check even in an
environment where ``aiohttp`` and ``vco_lib`` are absent. (The gateway itself
needs both; the smoke check is about packaging, not capability.)
"""

from __future__ import annotations

#: Reported by ``/health`` and ``vct-model-gateway --version``. Kept in step
#: with the distribution version by ``scripts/bump-version.sh`` and GATED by
#: ``scripts/check-version-pins.sh`` — it was neither until v0.2.94, which is
#: how a 0.2.93 install served ``"version": "0.2.92"`` from /health and made
#: "which gateway am I talking to?" unanswerable during an incident.
__version__ = "0.2.94"

__all__ = ["__version__"]
