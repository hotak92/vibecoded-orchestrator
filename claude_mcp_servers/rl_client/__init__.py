# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Client adapter for the vct-rl-reranker container (paid module).

Free tier: no container running → all calls fall back to "no rerank"
(return inputs unchanged), preserving the existing
``feature_enabled('rl_retrieval')`` semantics. Local event logging
still happens (subject to user opt-out in Preferences) so when a user
upgrades to Pro the historical data is there to train on.

Wire contract (LOAD-BEARING — see ``schemas.py`` for full Pydantic
models): mirrors what ``paid-modules/vct-rl-reranker/rl_server.py``
accepts. Any drift between the schemas here and the server's HTTP
handlers will silently break the paid module.
"""
from .client import (
    RLClient,
    RLClientError,
    RLClientUnreachableError,
    _deprecation_warning,
)
from .schemas import (
    CacheNodesRequest,
    CacheNodesResponse,
    HealthResponse,
    NodeInput,
    RankedNode,
    RLUpdateRequest,
    RLUpdateResponse,
)
from .telemetry_writer import RLTelemetryWriter

__all__ = [
    "RLClient",
    "RLClientError",
    "RLClientUnreachableError",
    "RLTelemetryWriter",
    "_deprecation_warning",
    "CacheNodesRequest",
    "CacheNodesResponse",
    "HealthResponse",
    "NodeInput",
    "RankedNode",
    "RLUpdateRequest",
    "RLUpdateResponse",
]
