# shellcheck shell=bash
# _lib/container-names.sh
# Canonical container-name registry for VCO infrastructure.
#
# Single source of truth so the SessionStart hook and the bundled compose
# file cannot disagree. Any rename of `vco_weaviate`/`vco_ollama`/
# `vco_code_embed` MUST happen here AND in:
#   - vco_lib/containers.py (CANONICAL_CONTAINERS + HISTORICAL_ALIASES)
#   - infrastructure/docker-compose.yml (container_name fields)
#   - infrastructure/podman-compose*.yml (container_name fields)
#   - templates/hooks/_lib/container-names.ps1 (Windows mirror)
#   - launcher/src-tauri/src/types.rs (ServiceConfig::command for code_embed)
#   - launcher/src-tauri/src/commands/volumes.rs (volume_role mapping)
#
# v0.2.15 rename: vct_code_embed → vco_code_embed for naming consistency.
# The legacy `vct_code_embed` name is recognised on existing installs by
# `vco_lib/containers.py::HISTORICAL_ALIASES` (used by
# `find_existing_container`) and by templates/hooks/verify-container-ports
# which row-expands across every known historical name. New installs get
# the canonical `vco_code_embed`.
#
# Usage (from any hook):
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     # shellcheck source=_lib/container-names.sh
#     . "$SCRIPT_DIR/_lib/container-names.sh"
#     for c in "${VCO_REQUIRED_CONTAINERS[@]}"; do ...; done
#
# Users can override the list by setting VCT_REQUIRED_CONTAINERS in their
# shell or .claude/env (space-separated). When unset, the canonical list
# below is used.
#
# This file is sourced, never executed, so it has no shebang. It is a
# library, not a hook — it is NOT registered in settings.json.template.

# Canonical container names. These match the `container_name:` fields in
# infrastructure/docker-compose.yml. If those names are changed, this file
# must be updated in lockstep (see header comment for the full rename list).
VCO_WEAVIATE_CONTAINER="vco_weaviate"
VCO_OLLAMA_CONTAINER="vco_ollama"
VCO_CODE_EMBED_CONTAINER="vco_code_embed"   # v0.2.15 rename (was
                                             # vct_code_embed). Legacy
                                             # name lives in
                                             # vco_lib/containers.py
                                             # HISTORICAL_ALIASES so
                                             # existing installs keep
                                             # working.

# Free-tier required set (no neo4j_claude — that's RL/instinct-tier only).
# code_embed is included because it's the canonical name even when the
# service is gpu-profile-gated; the hook tolerates a missing container.
if [ -n "${VCT_REQUIRED_CONTAINERS:-}" ]; then
    # shellcheck disable=SC2206
    read -ra VCO_REQUIRED_CONTAINERS <<<"$VCT_REQUIRED_CONTAINERS"
else
    VCO_REQUIRED_CONTAINERS=(
        "$VCO_WEAVIATE_CONTAINER"
        "$VCO_OLLAMA_CONTAINER"
        "$VCO_CODE_EMBED_CONTAINER"
    )
fi

export VCO_WEAVIATE_CONTAINER VCO_OLLAMA_CONTAINER VCO_CODE_EMBED_CONTAINER
