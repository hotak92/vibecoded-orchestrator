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
#     foreach ($c in $VcoRequiredContainers) { ... }
#
# Users can override the list by setting VCT_REQUIRED_CONTAINERS in their
# shell or .claude/env (space-separated). When unset, the canonical list
# below is used.

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

if ($env:VCT_REQUIRED_CONTAINERS) {
    $VcoRequiredContainers = $env:VCT_REQUIRED_CONTAINERS -split '\s+' | Where-Object { $_ }
} else {
    $VcoRequiredContainers = @($VcoWeaviateContainer, $VcoOllamaContainer, $VcoCodeEmbedContainer)
}

# Make available to the dot-sourcing scope.
Set-Variable -Name VcoWeaviateContainer  -Value $VcoWeaviateContainer  -Scope 1 -ErrorAction SilentlyContinue
Set-Variable -Name VcoOllamaContainer    -Value $VcoOllamaContainer    -Scope 1 -ErrorAction SilentlyContinue
Set-Variable -Name VcoCodeEmbedContainer -Value $VcoCodeEmbedContainer -Scope 1 -ErrorAction SilentlyContinue
Set-Variable -Name VcoRequiredContainers -Value $VcoRequiredContainers -Scope 1 -ErrorAction SilentlyContinue
