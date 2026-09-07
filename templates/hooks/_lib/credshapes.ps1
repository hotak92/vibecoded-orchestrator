# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/credshapes.ps1 - Windows sibling of _lib/credshapes.sh, and the
# PowerShell MIRROR of the credential-SHAPE vocabulary SSOT.
#
# SSOT: vco_lib/credential_shapes.py. Python is the source of truth (A>B>C).
# These hooks ship into user projects with no venv / PYTHONPATH guarantee, so
# the data is re-implemented here rather than shelled out to; the mirror is
# locked to the SSOT by tests/fixtures/credential_shape_parity.json (exercised
# through pwsh by tests/test_credential_shapes_parity.py).
#
# THE FOUR CONTEXTS ARE NOT INTERCHANGEABLE. Each carries a different
# precision/recall bias earned from a different haystack:
#   repo_scan     whole tree incl. vendored bundles + binaries. PRECISION.
#   content_scan  one text file a user just edited. Recall-leaning.
#   command_scan  one shell command line. MAXIMUM recall.
#   argv_name     one hand-typed identifier, whole-string anchored.
# -Context is Mandatory and ValidateSet-constrained precisely so a caller
# cannot drift into the wrong bias by omitting it.
#
# Consumers: _lib/credscan.ps1 and post-tool-security.ps1 (both content_scan).
#
# Absence of a shape from a context is DELIBERATE. aws_access_key_id,
# atlassian_token and jwt are absent from repo_scan because short upper-alnum
# and 'eyJ' prefixes collide with base64 payloads inside vendored bundles
# (PROVEN in this repo: the Excalidraw base64 WASM/font chunk contains an
# AKIA + 16 upper-alnum run purely by chance).
#
# Never prints, logs or echoes a candidate value - labels and patterns only.
# Patterns are the ERE / Python / .NET intersection; usable with -match.

function Get-CredShapes {
    <#
    .SYNOPSIS
      Return the credential shapes serving a named precision context.
    .DESCRIPTION
      Emits one PSCustomObject per shape with Id, Label and Re properties, in
      the
      SSOT declaration order (label order is part of the contract). Mirrors
      vco_lib/credential_shapes.py::patterns_for_context.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('repo_scan', 'content_scan', 'command_scan', 'argv_name')]
        [string]$Context
    )


    switch ($Context) {
        'repo_scan' {
            @(
                [pscustomobject]@{ Id = 'github_token'; Label = 'GitHub token'; Re = 'gh[pousr]_[A-Za-z0-9]{36}' },
                [pscustomobject]@{ Id = 'github_fine_grained_pat'; Label = 'GitHub fine-grained PAT'; Re = 'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}' },
                [pscustomobject]@{ Id = 'gitlab_pat'; Label = 'GitLab PAT (glpat-)'; Re = 'glpat-[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'slack_token'; Label = 'Slack token (xox*)'; Re = 'xox[bpoas]-[A-Za-z0-9-]{20,}' },
                [pscustomobject]@{ Id = 'sk_vendor_key'; Label = 'Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)'; Re = 'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'pem_private_key'; Label = 'PEM private key'; Re = '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' }
            )
        }
        'content_scan' {
            @(
                [pscustomobject]@{ Id = 'github_token'; Label = 'GitHub token'; Re = 'gh[pousr]_[A-Za-z0-9]{36}' },
                [pscustomobject]@{ Id = 'github_fine_grained_pat'; Label = 'GitHub fine-grained PAT'; Re = 'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}' },
                [pscustomobject]@{ Id = 'aws_access_key_id'; Label = 'AWS access key ID (AKIA)'; Re = 'AKIA[0-9A-Z]{16}' },
                [pscustomobject]@{ Id = 'gitlab_pat'; Label = 'GitLab PAT (glpat-)'; Re = 'glpat-[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'slack_token'; Label = 'Slack token (xox*)'; Re = 'xox[bpoas]-[A-Za-z0-9-]{20,}' },
                [pscustomobject]@{ Id = 'atlassian_token'; Label = 'Atlassian API token'; Re = 'ATATT[A-Za-z0-9_=-]{20,}' },
                [pscustomobject]@{ Id = 'jwt'; Label = 'JWT / bearer-shaped token'; Re = 'eyJ[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'sk_vendor_key'; Label = 'Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)'; Re = 'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'pem_private_key'; Label = 'PEM private key'; Re = 'BEGIN [A-Z0-9 ]*PRIVATE KEY' },
                [pscustomobject]@{ Id = 'generic_secret_quoted'; Label = 'Generic secret'; Re = '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["''][a-zA-Z0-9+/=_\-]{32,}' },
                [pscustomobject]@{ Id = 'generic_secret_unquoted'; Label = 'Generic secret (unquoted)'; Re = '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*[a-zA-Z0-9+/=_\-]{32,}' },
                [pscustomobject]@{ Id = 'leak_probe'; Label = 'Hook leak-test marker'; Re = 'VCT_HOOK_LEAK_PROBE_a3f7c2' }
            )
        }
        'command_scan' {
            @(
                [pscustomobject]@{ Id = 'github_token'; Label = 'GitHub token'; Re = 'gh[pousr]_[A-Za-z0-9]{8,}' },
                [pscustomobject]@{ Id = 'github_fine_grained_pat'; Label = 'GitHub fine-grained PAT'; Re = 'github_pat_[A-Za-z0-9_]{8,}' },
                [pscustomobject]@{ Id = 'aws_access_key_id'; Label = 'AWS access key ID (AKIA)'; Re = 'AKIA[0-9A-Z]{16}' },
                [pscustomobject]@{ Id = 'gitlab_pat'; Label = 'GitLab PAT (glpat-)'; Re = 'glpat-[A-Za-z0-9_-]{8,}' },
                [pscustomobject]@{ Id = 'slack_token'; Label = 'Slack token (xox*)'; Re = 'xox[bpoas]-[A-Za-z0-9-]{8,}' },
                [pscustomobject]@{ Id = 'atlassian_token'; Label = 'Atlassian API token'; Re = 'ATATT[A-Za-z0-9_=-]{8,}' },
                [pscustomobject]@{ Id = 'jwt'; Label = 'JWT / bearer-shaped token'; Re = 'eyJ[A-Za-z0-9_-]{20,}' },
                [pscustomobject]@{ Id = 'sk_vendor_key'; Label = 'Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)'; Re = 'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{16,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{8,}' }
            )
        }
        'argv_name' {
            @(
                [pscustomobject]@{ Id = 'github_token'; Label = 'GitHub token'; Re = '^gh[pousr]_[A-Za-z0-9]{36}$' },
                [pscustomobject]@{ Id = 'github_fine_grained_pat'; Label = 'GitHub fine-grained PAT'; Re = '^github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}$' },
                [pscustomobject]@{ Id = 'aws_access_key_id'; Label = 'AWS access key ID (AKIA)'; Re = '^AKIA[0-9A-Z]{16}$' },
                [pscustomobject]@{ Id = 'gitlab_pat'; Label = 'GitLab PAT (glpat-)'; Re = '^glpat-[A-Za-z0-9_-]{20,}$' },
                [pscustomobject]@{ Id = 'slack_token'; Label = 'Slack token (xox*)'; Re = '^xox[bpoas]-[A-Za-z0-9_-]{20,}$' },
                [pscustomobject]@{ Id = 'atlassian_token'; Label = 'Atlassian API token'; Re = '^ATATT[A-Za-z0-9_-]{20,}$' },
                [pscustomobject]@{ Id = 'jwt'; Label = 'JWT / bearer-shaped token'; Re = '^eyJ[A-Za-z0-9_-]{20,}$' },
                [pscustomobject]@{ Id = 'sk_vendor_key'; Label = 'Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)'; Re = '^sk-[A-Za-z0-9_-]{20,}$' }
            )
        }
    }
}

function Get-CredShapeCombined {
    <#
    .SYNOPSIS
      One '|'-joined alternation of every pattern serving a context.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('repo_scan', 'content_scan', 'command_scan', 'argv_name')]
        [string]$Context
    )
    (Get-CredShapes -Context $Context | ForEach-Object { $_.Re }) -join '|'
}
