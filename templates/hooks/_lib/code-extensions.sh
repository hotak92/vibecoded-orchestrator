# shellcheck shell=bash
# code-extensions.sh — THE home for "is this path a code file?" (v0.2.95)
#
# Before this file the same extension alternation was written out in
# pre-edit-context-inject.sh, pre-bash-context-inject.sh (indirectly, via
# codegraph-query.sh), post-file-edit.sh, pre-tool-use.sh,
# code-graph-incremental.sh and stop-codegraph-drain.sh — a C-tier mirror
# per consumer, each with a "MUST MATCH the other two" comment that could
# only ever be enforced by a human reading all of them. Lane F10 added a
# further consumer (the Bash-write routing), which is the point at which
# the project's A>B>C rule requires ONE home instead of another copy.
#
# Consumers that source this file use `$VCO_CODE_EXT_RE` /
# `vco_is_code_file`. The remaining mirrors (pre-tool-use,
# code-graph-incremental, stop-codegraph-drain, command-noise-strip —
# the last embeds the pattern in an inline Python regex, a different
# language) are pinned to this literal by
# tests/test_v0295_code_extension_one_home.py, so a drift is a RED test
# rather than a silent divergence.
#
# The decision this encodes: which file extensions the code graph
# analyses. Adding a language means editing THIS line and the ratchet
# test's mirror list, nothing else.

# Extension alternation, as a bash ERE suitable for `[[ $p =~ $RE ]]`.
VCO_CODE_EXT_RE='\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'

# vco_is_code_file <path> — exit 0 when the path names a code file.
#
# Callers that cannot source this helper (a partial bundle install) must
# treat "helper absent" as NOT-code: skipping a code-graph queue append
# costs one stale entry that the next edit re-queues, while defaulting to
# "code" on an empty regex would match EVERY path.
vco_is_code_file() {
    [ -n "${1:-}" ] || return 1
    [[ "$1" =~ $VCO_CODE_EXT_RE ]]
}
