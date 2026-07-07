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

# High-signal live-credential SHAPES (regexes, grep -E). Unlike BLOCKLIST
# (historical fixed strings), these catch NEW leaks by structure. Used by
# the dist/ passes below (v0.2.75 P2c) — the paths the main pass excludes.
# ONE list feeds both the dist text pass and the dist-binary strings pass;
# do not fork it.
TOKEN_SHAPES=(
  # GitHub classic PAT: ghp_ + 36 alnum.
  "ghp_[A-Za-z0-9]{36}"

  # GitHub fine-grained PAT: github_pat_ + 22 alnum + _ + 59 alnum.
  # MUST-MATCH anchor: this is the canonical token-shape anchor that
  # templates/hooks/post-tool-security.sh (hooks plan D-13) must match —
  # keep the two regexes identical so they cannot drift.
  # NOTE: the looser shape github_pat_[A-Za-z0-9_]{60,} false-positives
  # on Rust release binaries — rustc concatenates static strings into
  # unseparated rodata tables, so command names like
  # get_github_pat_preview + neighbours fuse into 100+-char word runs
  # starting with "github_pat_". The exact-format shape matches every
  # real token while skipping identifier soup (verified 2026-07-07
  # against all 9 dist binaries).
  "github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}"

  # OpenAI-style secret key. Alnum-only tail unless a known prefix
  # (proj/svcacct/admin) follows — the bare sk-[A-Za-z0-9_-]{20,} shape
  # false-positives on locale asset chunk names in the vendored
  # Excalidraw bundle (e.g. sk-SK-<hash> for the Slovak locale).
  "sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}"

  # PEM private key header (RSA/EC/OPENSSH/blank variants).
  "-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

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
