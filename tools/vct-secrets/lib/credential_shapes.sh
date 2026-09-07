# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# shellcheck shell=bash
#
# credential_shapes.sh — the `vct` bash MIRROR of the credential-SHAPE
# vocabulary SSOT, carrying the `argv_name` context ONLY.
#
# SSOT: vco_lib/credential_shapes.py (`patterns_for_context("argv_name")`).
# Python is the source of truth (A>B>C). `vct` runs standalone from
# ~/.vct-secrets/vct in git-credential-helper context with NO venv / PYTHONPATH
# guarantee, so a `python -m` call would silently no-op the guard exactly where
# it is most needed — the same constraint that makes lib/secret_shape.sh a
# mirror rather than a shell-out. Locked to the SSOT by
# tests/fixtures/credential_shape_parity.json.
#
# WHY A SECOND BASH MIRROR EXISTS (it is not sloppiness): the hooks mirror lives
# at templates/hooks/_lib/credshapes.sh and deploys to <project>/.claude/hooks/_lib/,
# while this one deploys to ~/.vct-secrets/lib/. Those trees are disjoint at
# runtime, and a relative symlink between them would dangle under the documented
# `cp -a tools/vct-secrets/. ~/.vct-secrets/` deployment. This file therefore
# carries only the ONE context `vct` uses, so it is a projection of the
# vocabulary, not a duplicate of the hooks mirror.
#
# WHY argv_name IS CALIBRATED DIFFERENTLY FROM THE FILE-SCAN CONTEXTS — this is
# the part a future editor is most likely to "fix" and must not:
#
#   * The haystack is ONE complete, hand-typed identifier, so every pattern is
#     whole-string anchored (^...$). A file-scanning context must search WITHIN
#     lines and therefore inherits false-positive pressure that simply cannot
#     arise here.
#   * Because of that anchoring, the `sk-` arm is deliberately WIDE
#     (^sk-[A-Za-z0-9_-]{20,}$) rather than the narrowed file-scan form. The
#     file-scan narrowing exists ONLY to avoid matching vendored Excalidraw
#     locale asset names of the form sk-SK-<hash>-<hash> during repo-wide
#     scanning (a real, still-present corpus in this repo). An asset name cannot
#     occupy a --key argument, and the narrow form would MISS the whole
#     sk-<vendor>-<tail> family — i.e. the shape that motivated the guard.
#   * The GitHub prefix families keep EXACT lengths (gh?_ + 36, github_pat_
#     22+59) rather than the command-scan context's loose {8,}. A loose form
#     would refuse a plausible human name like `github_pat_personal`. Detecting
#     a TRUNCATED token is not worth that false-positive rate — a truncated
#     token is not a live credential.
#
# The predicate NEVER prints, logs or echoes the name it is handed.
#
# Portability: POSIX-ish bash (Linux / macOS / WSL2); patterns are ERE for
# `grep -E`. No GNU-only constructs.

# The `argv_name` alternation — every arm whole-string anchored, so the
# alternation as a whole stays whole-string. Mirrors, in SSOT declaration
# order: github_token, github_fine_grained_pat, aws_access_key_id, gitlab_pat,
# slack_token, atlassian_token, jwt, sk_vendor_key.
_CREDSHAPES_ARGV_NAME_RE='^gh[pousr]_[A-Za-z0-9]{36}$|^github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}$|^AKIA[0-9A-Z]{16}$|^glpat-[A-Za-z0-9_-]{20,}$|^xox[bpoas]-[A-Za-z0-9_-]{20,}$|^ATATT[A-Za-z0-9_-]{20,}$|^eyJ[A-Za-z0-9_-]{20,}$|^sk-[A-Za-z0-9_-]{20,}$'

# _credshapes_is_argv_name_credential NAME
#   exit 0 when NAME has the shape of a live credential, 1 otherwise.
#   Pure predicate: no side effects, no logging, safe to call anywhere.
#   LC_ALL=C so the character classes are byte-wise and locale-independent.
_credshapes_is_argv_name_credential() {
    printf '%s' "${1:-}" | LC_ALL=C grep -qE "$_CREDSHAPES_ARGV_NAME_RE"
}
