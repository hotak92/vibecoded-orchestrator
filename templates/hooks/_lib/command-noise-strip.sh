# shellcheck shell=bash
# _lib/command-noise-strip.sh
# The ONE bash-side home for the D-3 "command-noise strip": turn a raw bash
# command into a clean KG query by dropping noise tokens (flags, paths, shell
# operators, bare cwd dots) so a bare `cd`/`ls` yields little query signal
# instead of injecting directory-keyword KG.
#
# Why a shared helper (CLAUDE.md "one concern, one home" + coordinator SF-1/N-3):
# the strip logic was inlined in pre-bash-context-inject.sh, its .ps1, AND a
# re-inlined copy in the test — three copies that WILL drift. This file is the
# single bash home; pre-bash-context-inject.sh sources it and the test exercises
# THIS function (not a re-inlined copy). MUST MATCH command-noise-strip.ps1.
#
# Sourced, never executed — no shebang. Library, not a hook (NOT in settings.json).

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_COMMAND_NOISE_STRIP_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_COMMAND_NOISE_STRIP_SOURCED=1

# vco_strip_command_noise <command>
# Echo the noise-stripped query. Requires $PY (set by _lib/find-python.sh). With
# no interpreter, echoes the input unchanged (soft-fail — the caller still has a
# usable, if noisier, query). The Python token rules live HERE, once.
vco_strip_command_noise() {
    local cmd="$1"
    if [ -z "${PY:-}" ]; then
        printf '%s' "$cmd"
        return 0
    fi
    printf '%s' "$cmd" | "$PY" -c "
import re, sys
cmd = sys.stdin.read()
toks = []
for t in cmd.split():
    # drop short/long flags
    if t.startswith('-'):
        continue
    # drop shell operators / redirections / bare cwd dots / lone punctuation
    if t in ('|', '||', '&&', ';', '>', '>>', '<', '2>', '2>&1', '&', '.', '..', '*'):
        continue
    # drop bare path-looking tokens (contain a slash) UNLESS they carry a
    # code-file extension (those ARE meaningful — keep the basename).
    if '/' in t:
        base = t.rstrip('/').split('/')[-1]
        if re.search(r'\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)\$', base):
            toks.append(base)
        continue
    toks.append(t)
print(' '.join(toks).strip())
" 2>/dev/null || printf '%s' "$cmd"
}
