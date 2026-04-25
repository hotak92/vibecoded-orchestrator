#!/usr/bin/env bash
# Build + install the `vct` CLI to ~/.local/bin/
# Re-run safely; copies the latest release binary on top of any existing one.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_OUT="$HOME/.local/bin"

echo "[vct-cli] Building release binary..."
(cd "$DIR" && cargo build --release)

mkdir -p "$BIN_OUT"
cp "$DIR/target/release/vct" "$BIN_OUT/vct"
chmod +x "$BIN_OUT/vct"

echo "[vct-cli] Installed: $BIN_OUT/vct"
echo
case ":$PATH:" in
    *:"$BIN_OUT":*) echo "[vct-cli] $BIN_OUT is already on PATH." ;;
    *) echo "[vct-cli] WARNING: $BIN_OUT is not on PATH. Add it to your shell rc:"
       echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

echo
echo "[vct-cli] Try: vct --help"
