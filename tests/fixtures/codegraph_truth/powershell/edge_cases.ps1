<#
.SYNOPSIS
Hand-written definitions edge cases for the PowerShell truth gate.
function Commented-Out { }
#>

function Get-Plain {
    'plain'
}

function global:Set-Scoped {
    param([string]$Value)
    $script:last = $Value
}

function Invoke-Advanced {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [int]$Count = 1
    )
    Write-Output "closing brace in a string: }"
    Write-Output '}'
}

function Add-Pair($a, $b) { $a + $b }

function Open-NextLine
{
    'brace on the next line'
}

filter Select-Odd {
    if ($_ % 2) { $_ }
}

$doc = @"
function Fake-InHereString {
}
"@

function Outer-Work {
    function Inner-Helper {
        'inner'
    }
    Inner-Helper
}

$block = {
    'a script block is not a definition'
}

enum Shade {
    Light
    Dark
}

class Widget {
    [string]$Name

    Widget([string]$name) {
        $this.Name = $name
    }

    [string] Render() {
        return "<$($this.Name)>"
    }

    static [int] Count() {
        return 1
    }
}

class Gadget : Widget {
    Gadget() {
    }

    [void] Spin() {
    }
}
