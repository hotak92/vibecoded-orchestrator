#!/usr/bin/env bash
# Build + install the `vco` CLI to ~/.local/bin/
# Re-run safely; copies the latest release binary on top of any existing one.
#
# Renamed from `vct` to `vco` in v0.1.0 — `vct` is taken by the bash secrets
# tool at tools/vct-secrets/vct. If you have an old `~/.local/bin/vct` from
# a pre-rename install of this CLI, this script removes it (it shadowed the
# secrets script with our binary). The secrets `vct` lives at
# `~/.vct-secrets/vct` and is symlinked from `~/.local/bin/vct`; we restore
# that symlink if it was clobbered.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_OUT="$HOME/.local/bin"

echo "[vco-cli] Building release binary..."
(cd "$DIR" && cargo build --release)

mkdir -p "$BIN_OUT"

# Migration: if a pre-rename `~/.local/bin/vct` is a regular file (our old
# binary from before the rename), remove it. If it's a symlink (the secrets
# tool), leave it alone.
if [ -f "$BIN_OUT/vct" ] && [ ! -L "$BIN_OUT/vct" ]; then
    echo "[vco-cli] Removing pre-rename $BIN_OUT/vct (it shadowed your secrets tool)."
    rm -f "$BIN_OUT/vct"
    if [ -e "$HOME/.vct-secrets/vct" ] && [ ! -e "$BIN_OUT/vct" ]; then
        echo "[vco-cli] Restoring symlink: $BIN_OUT/vct -> $HOME/.vct-secrets/vct"
        ln -s "$HOME/.vct-secrets/vct" "$BIN_OUT/vct"
    fi
fi

cp "$DIR/target/release/vco" "$BIN_OUT/vco"
chmod +x "$BIN_OUT/vco"

echo "[vco-cli] Installed: $BIN_OUT/vco"
echo
case ":$PATH:" in
    *:"$BIN_OUT":*) echo "[vco-cli] $BIN_OUT is already on PATH." ;;
    *) echo "[vco-cli] WARNING: $BIN_OUT is not on PATH. Add it to your shell rc:"
       echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

echo
echo "[vco-cli] Try: vco --help"
