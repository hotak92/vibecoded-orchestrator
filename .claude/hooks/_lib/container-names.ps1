# _lib/container-names.ps1
# Canonical container-name registry for VCO infrastructure (Windows mirror).
#
# Single source of truth so the SessionStart hook and the bundled compose
# file cannot disagree. Any rename of `vco_weaviate`/`vco_ollama`/
# `vct_code_embed` MUST happen here AND in:
#   - infrastructure/docker-compose.yml (container_name fields)
#   - infrastructure/podman-compose*.yml (container_name fields)
#   - templates/hooks/_lib/container-names.sh (POSIX mirror)
#   - launcher/src-tauri/src/types.rs (ServiceConfig::command for code_embed)
#   - launcher/src-tauri/src/commands/volumes.rs (volume_role mapping)
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
$VcoCodeEmbedContainer = "vct_code_embed"   # Historical; kept stable per
                                            # docker-compose.yml comment.

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
