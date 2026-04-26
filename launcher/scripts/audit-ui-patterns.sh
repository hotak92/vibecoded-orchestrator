#!/usr/bin/env bash
#
# Lint-style audit for UI patterns that we know break in Tauri's Linux
# WebKitGTK runtime (or that we want to standardize on for consistency).
# Not wired into CI — run on demand when reviewing UI changes:
#
#   bash launcher/scripts/audit-ui-patterns.sh
#
# Exits 0 with a report. Exits 1 if any forbidden patterns are found.

set -u

cd "$(dirname "$0")/.." || exit 2
SRC="src"
fail=0

echo "── UI pattern audit ──"

# 1. Native <select> — broken styling on Tauri/WebKitGTK
#    (tauri-apps/tauri#11755). Use $lib/components/Dropdown.svelte instead.
echo
echo "[1] Native <select> usage (must use <Dropdown> instead):"
hits=$(grep -rn --include="*.svelte" --exclude-dir=node_modules \
  -E "^[^/*]*<select[ >]" "$SRC" || true)
# Filter out the Dropdown component file itself + comments mentioning select.
hits=$(echo "$hits" | grep -v "Dropdown.svelte" || true)
hits=$(echo "$hits" | grep -v -E "^[^:]+:[0-9]+:\s*(//|/\*|\* |<!--)" || true)
if [ -n "$hits" ]; then
  echo "  FAIL — found native <select>:"
  echo "$hits" | sed 's/^/    /'
  fail=1
else
  echo "  ok"
fi

# 2. Modal-style overlays without max-height bound
echo
echo "[2] Modal containers without max-height (may crop on short viewports):"
modal_files=$(grep -rln --include="*.svelte" --exclude-dir=node_modules \
  -E "modal-backdrop|ow-back|cm-back|access-backdrop|dashboard-overlay" "$SRC" || true)
for f in $modal_files; do
  # Each modal file should declare max-height: <something>vh somewhere.
  if ! grep -q "max-height:" "$f"; then
    echo "  WARN — $f has a modal-style backdrop but no max-height"
    fail=1
  fi
done
[ "$fail" -eq 0 ] && echo "  ok"

# 3. Folder-picker buttons that don't import from $lib/dialog.
#    We match the "Browse…" / "Choose folder" / "Pick folder" labels;
#    "Browse" without ellipsis often means "browse a list" (e.g. KG
#    collection browser), which is unrelated.
echo
echo "[3] Folder-picker buttons not wired through \$lib/dialog:"
browse_files=$(grep -rln --include="*.svelte" --exclude-dir=node_modules \
  -E ">Browse…|>Browse\.\.\.|>Choose folder|>Pick folder" "$SRC" || true)
local_fail=0
for f in $browse_files; do
  if ! grep -q "from '\$lib/dialog'" "$f"; then
    echo "  WARN — $f has a folder-picker button but doesn't import \$lib/dialog"
    fail=1
    local_fail=1
  fi
done
[ "$local_fail" -eq 0 ] && echo "  ok"

echo
if [ "$fail" -ne 0 ]; then
  echo "── audit FAILED — see warnings above"
  exit 1
fi
echo "── audit clean"
