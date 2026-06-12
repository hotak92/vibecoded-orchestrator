# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# resolve-powershell.ps1 — pick the PowerShell executable for child spawns.
#
# v0.2.54 Track G (G-6): several hooks spawned helper scripts with a
# hardcoded `pwsh` (PowerShell 7+). The hooks THEMSELVES are launched via
# `powershell` (5.1, ships with every Windows 10+) per
# settings.json.windows.template — so on machines without PowerShell 7
# the hook body ran fine but every child spawn (`kg-sync.ps1`,
# `vct_access_check.ps1`, `code-graph-incremental.ps1`, ...) failed
# silently: KG sync, write-gate, dup-detection and code-graph updates
# were all lost with no error surfaced.
#
# Dot-source this file, then use $PsExe instead of a literal "pwsh":
#
#   . (Join-Path $ScriptDir "_lib/resolve-powershell.ps1")
#   & $PsExe -NoProfile -File $helper @args
#   Start-Process -FilePath $PsExe -ArgumentList @('-NoProfile','-File',$helper)
#
# Preference order: pwsh (7+, faster startup, the flavour the helpers are
# tested against) -> powershell (5.1 fallback — the helpers stick to
# 5.1-compatible syntax per the hook portability discipline).
$PsExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
