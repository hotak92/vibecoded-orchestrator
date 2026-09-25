#!/usr/bin/env bash
# Build + install the launcher CLI `vct-cli` to ~/.local/bin/
# Re-run safely; copies the latest release binary on top of any existing one.
#
# Names this CLI has had: `vct` (before v0.1.0 — now the bash secrets tool at
# tools/vct-secrets/vct), `vco` (v0.1.0 to v0.2.96 — now the orchestrator's
# Python CLI: `vco doctor`, `vco project move`, ...), `vct-cli` (v0.2.97+).
# A copy an earlier run of this script left at ~/.local/bin/vco or
# ~/.local/bin/vct is removed only when it provably IS this CLI (rule:
# vco_lib/launcher_cli_identity.py); anything else there is left alone.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_OUT="$HOME/.local/bin"

# shellcheck source-path=SCRIPTDIR source=retire-old-names.sh
. "$DIR/retire-old-names.sh"

echo "[vct-cli] Building release binary..."
(cd "$DIR" && cargo build --release)

mkdir -p "$BIN_OUT"

vct_cli_retire_old_names "$BIN_OUT"

cp "$DIR/target/release/vct-cli" "$BIN_OUT/vct-cli"
chmod +x "$BIN_OUT/vct-cli"

echo "[vct-cli] Installed: $BIN_OUT/vct-cli"
echo
case ":$PATH:" in
    *:"$BIN_OUT":*) echo "[vct-cli] $BIN_OUT is already on PATH." ;;
    *) echo "[vct-cli] WARNING: $BIN_OUT is not on PATH. Add it to your shell rc:"
       echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

echo
echo "[vct-cli] Try: vct-cli --help"
