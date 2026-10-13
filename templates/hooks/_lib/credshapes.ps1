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
        [string]$Context,
        # The scanned file. When given, shapes restricted to another file kind
        # are dropped (mirror of patterns_for_file). Every content scanner
        # knows its path and must pass it.
        [string]$Path
    )

    $all = switch ($Context) {
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
                [pscustomobject]@{ Id = 'generic_secret_quoted'; Label = 'Generic secret'; Re = '(^|[^$A-Za-z0-9_{]|(^|[^$])\{)[A-Za-z0-9_.-]*([Ss][Ee][Cc][Rr][Ee][Tt]|[Aa][Pp][Ii]_[Kk][Ee][Yy]|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Pp][Ww][Dd]|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee])["'']?\??\s*(:\s*&?(''[A-Za-z_]+\s+)?[A-Za-z_][A-Za-z0-9_.:]*(<[A-Za-z0-9_., &''<>]*>|\[[A-Za-z0-9_., ]*\])?\s*|\s+[A-Za-z_][A-Za-z0-9_.]*\s*)?:?[:=]\s*([rRbBuUfF]{0,2}"""[^"${][^"]*"""|[rRbBuU]{0,2}''''''[^''][^'']*''''''|[rRfF]{1,2}''''''[^''{][^'']*''''''|[rRbBuUfF]{0,2}"[^"${][^"]*"|[rRbBuU]{0,2}''[^''][^'']*''|[rRfF]{1,2}''[^''{][^'']*'')' },
                [pscustomobject]@{ Id = 'generic_secret_backtick'; Label = 'Generic secret (backtick literal)'; Re = '(^|[^$A-Za-z0-9_{]|(^|[^$])\{)[A-Za-z0-9_.-]*([Ss][Ee][Cc][Rr][Ee][Tt]|[Aa][Pp][Ii]_[Kk][Ee][Yy]|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Pp][Ww][Dd]|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee])["'']?\??\s*(:\s*&?(''[A-Za-z_]+\s+)?[A-Za-z_][A-Za-z0-9_.:]*(<[A-Za-z0-9_., &''<>]*>|\[[A-Za-z0-9_., ]*\])?\s*|\s+[A-Za-z_][A-Za-z0-9_.]*\s*)?:?[:=]\s*`([^`$]|\$[^`{])+\$?`' },
                [pscustomobject]@{ Id = 'generic_secret_unquoted'; Label = 'Generic secret (unquoted)'; Re = '(^|[^$A-Za-z0-9_{]|(^|[^$])\{)[A-Za-z0-9_.-]*([Ss][Ee][Cc][Rr][Ee][Tt]|[Aa][Pp][Ii]_[Kk][Ee][Yy]|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Pp][Ww][Dd]|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee])["'']?\s*[:=]([^"''$` ]|\s+[^"''$`# ])|ENV\s+[A-Za-z0-9_]*([Ss][Ee][Cc][Rr][Ee][Tt]|[Aa][Pp][Ii]_[Kk][Ee][Yy]|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Pp][Ww][Dd]|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee])\s+["'']?[^"''$`# ]' },
                # The YAML value pattern holds LITERAL TAB characters (in its first
                # class and after each silent word), exactly like the SSOT text:
                # grep -E cannot read a backslash-t in brackets (v0.2.102 W6-LOWS V-a).
                [pscustomobject]@{ Id = 'generic_secret_unquoted_yaml'; Label = 'Generic secret (unquoted, YAML)'; Re = '(^|[^$A-Za-z0-9_{]|(^|[^$])\{)[A-Za-z0-9_.-]*([Ss][Ee][Cc][Rr][Ee][Tt]|[Aa][Pp][Ii]_[Kk][Ee][Yy]|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Pp][Ww][Dd]|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee])["'']?\s*[:=]\s*([^"''$`~*{\[nNtTfFyYoO 	#]|[nN]($|[^uUoO])|[nN][oO][^ 	]|[nN][uU]($|[^lL])|[nN][uU][lL]($|[^lL])|[nN][uU][lL][lL][^ 	]|[tT]($|[^rR])|[tT][rR]($|[^uU])|[tT][rR][uU]($|[^eE])|[tT][rR][uU][eE][^ 	]|[fF]($|[^aA])|[fF][aA]($|[^lL])|[fF][aA][lL]($|[^sS])|[fF][aA][lL][sS]($|[^eE])|[fF][aA][lL][sS][eE][^ 	]|[yY]($|[^eE])|[yY][eE]($|[^sS])|[yY][eE][sS][^ 	]|[oO]($|[^nNfF])|[oO][nN][^ 	]|[oO][fF]($|[^fF])|[oO][fF][fF][^ 	])' },
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
    if ($PSBoundParameters.ContainsKey('Path')) {
        # Drop each restricted shape whose kind is not this file's kind.
        $kind = Get-CredShapeFileKind -Path $Path
        $restricted = Get-CredShapeKindRestrictions
        return @($all | Where-Object {
            -not $restricted.ContainsKey($_.Id) -or $restricted[$_.Id] -eq $kind
        })
    }
    $all
}

function Get-CredShapeKindRestrictions {
    <#
    .SYNOPSIS
      Shape id -> the ONE file kind it serves. MUST MATCH the shapes'
      file_kinds in vco_lib/credential_shapes.py.
    #>
    $r = @{}
    $r['generic_secret_backtick'] = 'backtick_code'
    $r['generic_secret_unquoted'] = 'env_config'
    $r['generic_secret_unquoted_yaml'] = 'yaml'
    $r
}

function Get-CredShapeFileKind {
    <#
    .SYNOPSIS
      env_config / yaml / backtick_code / code for a path, from its
      basename only.
    .DESCRIPTION
      Owner ruling 2026-10-13 (W6-V review F2 + re-review 2). MUST MATCH
      the table in vco_lib/credential_shapes.py (ENV_CONFIG_* /
      YAML_NAME_SUFFIXES / BACKTICK_CODE_NAME_SUFFIXES, lower-cased
      basename, in that order); pinned by
      tests/test_credential_shapes_parity.py.
    #>
    param([string]$Path)
    $exact = @('.env', 'env', '.envrc', 'dockerfile', 'containerfile', '.bashrc', '.bash_profile', '.zshrc', '.zprofile', '.zshenv', '.profile', '.pypirc', '.npmrc')
    $prefixes = @('dockerfile.', 'containerfile.')
    $infixes = @('.env.')
    $suffixes = @('.env', '.ini', '.cnf', '.properties', '.conf', '.cfg', '.sh', '.bash', '.zsh', '.dockerfile')
    $yaml = @('.yaml', '.yml')
    $backtick = @('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts', '.vue', '.svelte', '.go')
    $name = (("" + $Path) -replace '\\', '/').Split('/')[-1].ToLowerInvariant()
    $ord = [System.StringComparison]::Ordinal
    if ($exact -contains $name) { return 'env_config' }
    foreach ($p in $prefixes) { if ($name.StartsWith($p, $ord)) { return 'env_config' } }
    foreach ($i in $infixes) { if ($name.IndexOf($i, $ord) -ge 0) { return 'env_config' } }
    foreach ($x in $suffixes) { if ($name.EndsWith($x, $ord)) { return 'env_config' } }
    foreach ($x in $yaml) { if ($name.EndsWith($x, $ord)) { return 'yaml' } }
    foreach ($x in $backtick) { if ($name.EndsWith($x, $ord)) { return 'backtick_code' } }
    'code'
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

function Get-CredShapesTestPlaceholderMarker {
    <#
    .SYNOPSIS
      The ONE explicit test-placeholder opt-out (owner ruling 2026-10-12).
    .DESCRIPTION
      A line carrying this text is a declared test fixture and a content
      scanner does not report it. No heuristic on how a value looks - only
      this marker, on the same line. MUST MATCH
      vco_lib/credential_shapes.py::TEST_PLACEHOLDER_MARKER (pinned by
      tests/test_credential_shapes_parity.py).
    #>
    'vco-secret-scan: test-placeholder'
}
