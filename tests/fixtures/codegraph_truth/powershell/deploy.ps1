<#
.SYNOPSIS
Deploy helpers for the golden-fixture repo (PowerShell).
#>

function Invoke-Deploy {
    param(
        [string]$Target,
        [int]$Retries
    )

    # Nested function declared at 8-space indentation - the v0.2.75
    # deep-indent regression case (>=8 leading chars must not IndexError).
        function Write-Step {
            param([string]$Message)
            Write-Output "step: $Message"
        }

    Write-Step -Message "starting $Target"
    return $Retries
}

filter Get-Even {
    if ($_ % 2 -eq 0) { $_ }
}
