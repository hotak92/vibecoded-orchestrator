# _lib/container-names.ps1
# Canonical container-name registry for VCO infrastructure (Windows mirror).
#
# Single source of truth so the SessionStart hook and the bundled compose
# file cannot disagree. Any rename of `vco_weaviate`/`vco_ollama`/
# `vco_code_embed` MUST happen here AND in:
#   - vco_lib/containers.py (CANONICAL_CONTAINERS + HISTORICAL_ALIASES)
#   - infrastructure/docker-compose.yml (container_name fields)
#   - infrastructure/podman-compose*.yml (container_name fields)
#   - templates/hooks/_lib/container-names.sh (POSIX mirror)
#   - launcher/src-tauri/src/types.rs (ServiceConfig::command for code_embed)
#   - launcher/src-tauri/src/commands/volumes.rs (volume_role mapping)
#
# v0.2.15 rename: vct_code_embed -> vco_code_embed for naming consistency.
# The legacy `vct_code_embed` name is recognised on existing installs by
# `vco_lib/containers.py::HISTORICAL_ALIASES` (used by
# `find_existing_container`) and by templates/hooks/verify-container-ports
# which row-expands across every known historical name. New installs get
# the canonical `vco_code_embed`.
#
# Usage (from any .ps1 hook):
#     $LibDir = Join-Path $PSScriptRoot "_lib"
#     . (Join-Path $LibDir "container-names.ps1")
#     if ($VcoRequiredContainers.Count -gt 0) { ... --required ($VcoRequiredContainers -join ' ') }
#
# v0.2.97: WHICH containers the session hook ensures -- and what it may do to
# each -- is decided by `python -m vco_lib.service_lifecycle plan`, from the
# launcher.db `service_endpoints` rows (an adopted Weaviate may be called
# something other than `vco_weaviate`, and must only ever be started by
# name). $VcoRequiredContainers is therefore ONLY the user's override: set
# VCT_REQUIRED_CONTAINERS (space-separated) in the shell or .claude/env to
# narrow or extend the set; unset, it is EMPTY and the plan's own list
# applies. Mirror of container-names.sh.

# Canonical container names. These match the `container_name:` fields in
# infrastructure/docker-compose.yml. If those names are changed, this file
# must be updated in lockstep (see header comment for the full rename list).
$VcoWeaviateContainer = "vco_weaviate"
$VcoOllamaContainer   = "vco_ollama"
$VcoCodeEmbedContainer = "vco_code_embed"   # v0.2.15 rename (was
                                            # vct_code_embed). Legacy
                                            # name lives in
                                            # vco_lib/containers.py
                                            # HISTORICAL_ALIASES so
                                            # existing installs keep
                                            # working.

$VcoRequiredContainers = @()
if ($env:VCT_REQUIRED_CONTAINERS) {
    $VcoRequiredContainers = @($env:VCT_REQUIRED_CONTAINERS -split '\s+' | Where-Object { $_ })
}

# Make available to the dot-sourcing scope.
#
# `-Scope Script` (not `-Scope 1`): when this lib is dot-sourced (the only
# supported invocation, see header), its Script scope IS the caller's script
# scope, so `$VcoRequiredContainers` etc. are visible to the caller exactly as
# before. `-Scope 1` worked only by coincidence of having a parent scope to
# count back to, and was wrapped in `-ErrorAction SilentlyContinue` -- which
# silently swallowed any scope failure (e.g. if ever run with `-File`, where
# scope 1 does not exist). `-Scope Script` is valid under BOTH dot-source and
# `-File`, so a real failure surfaces instead of being masked.
Set-Variable -Name VcoWeaviateContainer  -Value $VcoWeaviateContainer  -Scope Script
Set-Variable -Name VcoOllamaContainer    -Value $VcoOllamaContainer    -Scope Script
Set-Variable -Name VcoCodeEmbedContainer -Value $VcoCodeEmbedContainer -Scope Script
Set-Variable -Name VcoRequiredContainers -Value $VcoRequiredContainers -Scope Script
