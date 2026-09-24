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
#     [ "${#VCO_REQUIRED_CONTAINERS[@]}" -gt 0 ] && ... --required "${VCO_REQUIRED_CONTAINERS[*]}"
#
# v0.2.97: WHICH containers the session hook ensures — and what it may do to
# each — is decided by `python -m vco_lib.service_lifecycle plan`, from the
# launcher.db `service_endpoints` rows (an adopted Weaviate may be called
# something other than `vco_weaviate`, and must only ever be started by
# name). VCO_REQUIRED_CONTAINERS is therefore ONLY the user's override:
# set VCT_REQUIRED_CONTAINERS (space-separated) in the shell or .claude/env
# to narrow or extend the set; unset, the array is EMPTY and the plan's own
# list applies.
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

# The user's override only (see the header): empty unless
# VCT_REQUIRED_CONTAINERS is set — the service_endpoints plan decides the
# default set.
# shellcheck disable=SC2034  # read by the sourcing hook
VCO_REQUIRED_CONTAINERS=()
if [ -n "${VCT_REQUIRED_CONTAINERS:-}" ]; then
    # shellcheck disable=SC2206,SC2034
    read -ra VCO_REQUIRED_CONTAINERS <<<"$VCT_REQUIRED_CONTAINERS"
fi

export VCO_WEAVIATE_CONTAINER VCO_OLLAMA_CONTAINER VCO_CODE_EMBED_CONTAINER
