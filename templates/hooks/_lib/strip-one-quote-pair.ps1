# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# strip-one-quote-pair.ps1 -- THE one home for the env-file quote rule.
#
# Strip ONE surrounding quote pair: single or double, and only when the
# first and last characters are the SAME quote char. This is exactly
# vco_lib/envfile.py::_strip_one_quote_pair (the canonical env-file parse
# home) and the behavior the shell gives for free when a hook SOURCES
# <project>/.claude/env (`VCO_LEAN_CTX_DEFAULT="off"` and ='off' both
# arrive as `off`). A blanket trim of either quote kind (the pre-v0.2.101
# behavior on the Windows side) also collapses an UNBALANCED / mixed pair
# the POSIX side keeps, which is the cross-OS divergence this rule avoids.
#
# Consumers -- both scan the RAW env-file line, so both must apply the
# SAME rule to their knob:
#   * templates/hooks/lean-ctx-rewrite.ps1      (VCO_LEAN_CTX_DEFAULT,
#                                                VCO_LEAN_CTX_TEE_TTL_HOURS)
#   * templates/hooks/post-tool-use-async.ps1   (VCO_ASYNC_DISABLED_HOOKS)
#
# The POSIX siblings need NO equivalent helper: they SOURCE the file and
# the shell already removes one quote pair, so there is no .sh file to
# mirror. That "no POSIX counterpart by design" is declared by this name in
# PS1_ONLY_LIB in BOTH .github/scripts/check_hook_parity.py and
# tests/test_v0292_sibling_parity_blind_spots.py (the CI gate and the
# sibling ratchet must agree on what is allowed).
#
# Usage:
#   . "$PSScriptRoot/_lib/strip-one-quote-pair.ps1"
#   $value = Strip-OneQuotePair $raw

function Strip-OneQuotePair([string]$v) {
    if ($v -and $v.Length -ge 2) {
        $q0 = $v[0]
        $q1 = $v[$v.Length - 1]
        if ((($q0 -eq '"') -and ($q1 -eq '"')) -or (($q0 -eq "'") -and ($q1 -eq "'"))) {
            return $v.Substring(1, $v.Length - 2)
        }
    }
    return $v
}