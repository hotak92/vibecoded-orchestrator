#!/usr/bin/env bash
# Pre-commit guard: refuse to commit known-leaked tokens.
#
# Run from repo root:
#   ./scripts/check-no-secrets.sh
#
# Wire as a git pre-commit hook with:
#   ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit
#
# The blocklist below is the historical-leak list — values that have
# already been exposed in this repo and that must NEVER reappear, even
# in documentation. Replace any new occurrence with a clear placeholder
# (e.g. `<YOUR_FOO>`) and document how to generate the real value.

set -euo pipefail

# Tokens / project refs that have leaked at some point in this repo's
# history. Treat each as compromised forever.
#
# v0.2.54 Track E (P0-8): the previous version of this file embedded the
# literal leaked-token values verbatim (so the file itself was a leak
# vector — anyone reading scripts/check-no-secrets.sh learned the exact
# secrets to grep for in older history). The blocklist now stores only
# PREFIX PATTERNS that are uniquely shaped enough to catch the secret
# without naming it. Reasoning:
#   - `wh_vct_ls_*` matches the Lemon Squeezy webhook prefix used by
#     this project's webhooks (LS uses `wh_` for webhooks; the
#     `_vct_ls_` infix is unique to our naming convention). Real
#     placeholders like `wh_vct_ls_<rotate_me>` are caught.
#   - `ltnlwh*` matches the 6-char Supabase project-ref prefix that
#     leaked. The full ref is 20 chars; 6 chars is enough to uniquely
#     identify it without re-stating the value.
# If a leaked-token shape ever becomes ambiguous (collides with a
# legitimate string), tighten the regex rather than expanding it back
# into a literal — the leak-script-as-leak-vector failure mode is the
# one this redesign prevents.
BLOCKLIST=(
  # Lemon Squeezy webhook signing secret leaked in launcher/docs (commit
  # 2f1cc88, 2026-03-07). Sanitized in oss/round3-secrets-rotation-and-admin.
  # Pattern matches `wh_vct_ls_<anything>` — the unique infix `_vct_ls_`
  # is project-specific and not present in legitimate code.
  "wh_vct_ls_"

  # Supabase project ref leaked alongside the webhook secret. The public
  # alias https://api.vibecodedtools.it/* should be used instead.
  # Pattern matches the first 6 chars of the 20-char ref — enough to
  # identify it without re-stating the full value.
  "ltnlwh"
)

# High-signal live-credential SHAPES, from the ONE vocabulary home.
#
# These are NOT defined here any more. They come from the credential-shape
# vocabulary SSOT vco_lib/credential_shapes.py via its bash mirror
# templates/hooks/_lib/credshapes.sh, `repo_scan` context. Previously this
# block was a private fork that drifted from the hook scanners until they no
# longer agreed on which vendors they could see.
#
# WHY `repo_scan` AND NOT ANOTHER CONTEXT — this passes over VENDORED bundles
# and COMPILED BINARIES, the most false-positive-hostile haystack there is, so
# it uses the most precision-biased context. Two collisions are proven present
# in this repo and both live under a `dist/` path this script scans:
#   * a Slovak locale chunk name `sk-SK-<hash>-<hash>` in the vendored
#     Excalidraw bundle, which a loose `sk-` tail matches;
#   * an `AKIA` + 16-upper-alnum run occurring BY CHANCE inside that bundle's
#     base64 WASM/font payload.
# That is why `repo_scan` narrows the `sk-` tail and omits the AWS / JWT /
# Atlassian shapes that the file-content scanners do carry. A false positive
# here fires on every commit and teaches people to ignore this script, which is
# how a real alert gets missed. Do not "unify" these with the content-scan
# patterns; see the SSOT docstring for the full per-context rationale.
#
# ONE list still feeds both the dist text pass and the dist-binary strings
# pass; do not fork it.
_CNS_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
_CNS_SHAPES_LIB="$_CNS_ROOT/templates/hooks/_lib/credshapes.sh"
if [ ! -r "$_CNS_SHAPES_LIB" ]; then
  # Fallback for an invocation whose CWD is not the repo root: resolve
  # relative to this script's own directory.
  _CNS_SHAPES_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/templates/hooks/_lib/credshapes.sh"
fi
if [ ! -r "$_CNS_SHAPES_LIB" ]; then
  # Loud-fail. A secret scanner that cannot load its vocabulary must NEVER
  # print "OK" — that is a silent all-clear over an unscanned tree.
  echo "check-no-secrets: FATAL — credential-shape vocabulary not found at" >&2
  echo "  $_CNS_SHAPES_LIB" >&2
  echo "  (expected templates/hooks/_lib/credshapes.sh in this checkout)." >&2
  exit 2
fi
# shellcheck source=../templates/hooks/_lib/credshapes.sh disable=SC1090,SC1091
. "$_CNS_SHAPES_LIB"
if ! credshapes_for_context repo_scan; then
  echo "check-no-secrets: FATAL — credshapes_for_context rejected 'repo_scan'" >&2
  exit 2
fi
TOKEN_SHAPES=("${CREDSHAPES_PATTERNS[@]}")
if [ "${#TOKEN_SHAPES[@]}" -eq 0 ]; then
  echo "check-no-secrets: FATAL — empty credential-shape list; refusing to" >&2
  echo "  report a clean tree that was never actually scanned." >&2
  exit 2
fi

# Files we don't want to scan (binaries, generated, vendored).
EXCLUDE_PATHS=(
  ":(exclude)CHANGELOG.md"
  ":(exclude)scripts/check-no-secrets.sh"
  ":(exclude).git/**"
  ":(exclude)**/node_modules/**"
  ":(exclude)**/target/**"
  ":(exclude)**/.next/**"
  ":(exclude)**/dist/**"
  ":(exclude)**/build/**"
)

# Determine the file set:
# - if invoked as a pre-commit hook → only the staged additions
# - otherwise → the full tracked tree
if [ -n "${GIT_INDEX_FILE:-}" ] || git rev-parse --verify HEAD >/dev/null 2>&1; then
  if [ -n "${1:-}" ] && [ "${1}" = "--staged" ]; then
    file_list=$(git diff --cached --name-only --diff-filter=ACMR)
  elif [ "${1:-}" = "--all" ]; then
    file_list=$(git ls-files -- "${EXCLUDE_PATHS[@]}")
  else
    file_list=$(git ls-files -- "${EXCLUDE_PATHS[@]}")
  fi
else
  echo "check-no-secrets.sh: not in a git repo, scanning current dir tree"
  file_list=$(find . -type f \( -name "*.md" -o -name "*.ts" -o -name "*.py" -o -name "*.rs" -o -name "*.toml" -o -name "*.json" -o -name "*.sh" \) | grep -v node_modules | grep -v target)
fi

violations=0

# Shared scan loop — the ONE home for "grep a pattern list over a file
# list and report violations". Both the main pass and the dist text
# pass (v0.2.75 P2c) call this; don't inline a second copy.
#   $1 = grep matcher mode: -F (fixed string) or -E (extended regex)
#   $2 = label for the report line
#   $3 = newline-separated file list
#   $4..$n = patterns
scan_files() {
  local mode="$1" label="$2" list="$3"
  shift 3
  local token matches f
  for token in "$@"; do
    # -l = filename only. Never print the matched content itself — a
    # real hit would re-leak the value into logs/CI output.
    # NOTE: test the OUTPUT, not the exit status — when xargs splits a
    # long list into batches and one batch has no match, xargs exits
    # 123 even though another batch DID match; keying on exit status
    # would silently drop that report.
    matches=$(printf '%s\n' "$list" | xargs -r grep -l "$mode" -- "$token" 2>/dev/null || true)
    if [ -n "$matches" ]; then
      printf 'BLOCKED (%s): pattern "%s" found in:\n' "$label" "$token" >&2
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        printf '  %s\n' "$f" >&2
      done <<< "$matches"
      violations=$((violations + 1))
    fi
  done
}

# ── Pass 1: tracked tree (minus EXCLUDE_PATHS), historical blocklist ──
scan_files -F "leaked token" "$file_list" "${BLOCKLIST[@]}"

# ── Pass 2 (v0.2.75 P2c): dist/ TEXT files ───────────────────────────
# The blanket **/dist/** exclusion above keeps binaries out of pass 1,
# but it also skipped TEXT files under dist/ — notably the tracked
# launcher/dist/**/metadata.json the release bot refreshes on every
# tag. Scan them explicitly here with the SAME BLOCKLIST plus the
# TOKEN_SHAPES regexes. Uses find (not git ls-files) so untracked
# files sitting in a dist/ dir are caught BEFORE anything commits them.
dist_text_list=$(find . \
    \( -name .git -o -name node_modules -o -name target \) -prune -o \
    -type f -path '*/dist/*' \
    \( -name '*.json' -o -name '*.md' -o -name '*.txt' -o -name '*.js' \
       -o -name '*.ts' -o -name '*.map' -o -name '*.html' -o -name '*.css' \
       -o -name '*.yml' -o -name '*.yaml' -o -name '*.toml' \) \
    -print 2>/dev/null || true)
if [ -n "$dist_text_list" ]; then
  scan_files -F "dist text: leaked token" "$dist_text_list" "${BLOCKLIST[@]}"
  scan_files -E "dist text: credential shape" "$dist_text_list" "${TOKEN_SHAPES[@]}"
fi

# ── Pass 3 (v0.2.75 P2c tier-2): strings over the dist binaries ──────
# Time-boxed high-signal sweep of the shipped vct-launcher / vct-hub /
# vct-updater binaries themselves (all arches, ~0.6 s total). Only the
# TOKEN_SHAPES regexes — the historical BLOCKLIST prefixes are too
# short to be meaningful against binary rodata. Reports shape + count
# only, never the matched bytes (avoid re-leaking a real hit).
if command -v strings >/dev/null 2>&1; then
  _strings_tmp="$(mktemp)"
  for bin in launcher/dist/*/vct-launcher launcher/dist/*/vct-hub \
             launcher/dist/*/vct-updater launcher/dist/*/vct-launcher.exe \
             launcher/dist/*/vct-hub.exe launcher/dist/*/vct-updater.exe; do
    [ -f "$bin" ] || continue
    strings -n 8 -- "$bin" > "$_strings_tmp" 2>/dev/null || true
    for shape in "${TOKEN_SHAPES[@]}"; do
      # grep -c (not -q): -q's early-exit SIGPIPEs the producer, which
      # pipefail would misread as "no match". -c reads all input.
      _n=$(grep -E -c -- "$shape" "$_strings_tmp" || true)
      if [ "${_n:-0}" -gt 0 ]; then
        printf 'BLOCKED (dist binary): credential shape "%s" matched %s string(s) in %s\n' \
          "$shape" "$_n" "$bin" >&2
        violations=$((violations + 1))
      fi
    done
  done
  rm -f "$_strings_tmp"
else
  echo "check-no-secrets: note — 'strings' not on PATH; skipping dist-binary tier-2 sweep" >&2
fi

if [ "$violations" -gt 0 ]; then
  echo "" >&2
  echo "Refusing to commit. Replace each occurrence with a placeholder" >&2
  echo "(e.g. <YOUR_LS_WEBHOOK_SIGNING_SECRET>) and document how to" >&2
  echo "generate a real value. See the project secrets rotation runbook." >&2
  exit 1
fi

echo "check-no-secrets: OK (no known-leaked tokens found)"
exit 0
