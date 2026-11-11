# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Client adapter for the vct-rl-reranker container.

No container running → all calls fall back to "no rerank" (return inputs
unchanged). Local event logging still happens (subject to user opt-out in
Preferences) so the data is there to train on.

Wire contract (LOAD-BEARING — see ``schemas.py`` for full Pydantic
models): mirrors what ``claude_mcp_servers/rl_reranker/rl_server.py``
accepts. Any drift between the schemas here and the server's HTTP
handlers will silently break the reranker.
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
