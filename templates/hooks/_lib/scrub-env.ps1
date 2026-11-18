# _lib/scrub-env.ps1 -- canonical secret-env scrub list (HK-2, v0.2.73).
#
# PowerShell sibling of _lib/scrub-env.sh. THE single source of truth for
# the sensitive environment variables every .ps1 hook clears before
# spawning any subprocess. See the .sh header for the full rationale.
#
# MUST MATCH _lib/scrub-env.sh::$VCT_SCRUB_SECRET_KEYS and the inline
# Remove-Item Env: lists in every templates/hooks/*.ps1 (enforced by the
# parity gate tests/test_scrub_env_parity_v0273.py).

$VctScrubSecretKeys = @(
    'SUPABASE_KEY', 'SUPABASE_URL', 'GITHUB_TOKEN', 'GH_TOKEN',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AWS_SECRET_ACCESS_KEY',
    'AWS_ACCESS_KEY_ID', 'TELEGRAM_BOT_TOKEN', 'POSTGRES_PASSWORD',
    'VERCEL_TOKEN', 'CLAUDE_API_KEY'
)

# Every templates/hooks/*.ps1 dot-sources this file and calls
# Invoke-VctScrubSecretEnv; its inline list runs only when this file is
# missing (v0.2.107, the same shape as the .sh hooks with scrub-env.sh).
# Plain .NET, no cmdlet: setting a variable to $null removes it for this
# process and every child it starts (what `Remove-Item Env:X` does), and the
# hooks' fast paths pay no module autoload for it.
function Invoke-VctScrubSecretEnv {
    foreach ($k in $VctScrubSecretKeys) {
        [System.Environment]::SetEnvironmentVariable($k, $null)
    }
}
