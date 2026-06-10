#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Test: post-install-launcher.sh probes BOTH libwebkit2gtk-4.1-dev AND
# libwebkit2gtk-4.0-dev when installing Tauri build deps via apt.
#
# Background — v0.2.53 (Track G2 / L-P0-2)
# ========================================
#
# Ubuntu 22.04 LTS ("Jammy") and Debian 12 ("Bookworm") ship only
# libwebkit2gtk-4.0-dev (no 4.1 variant). The prior hard-pin to 4.1
# made `apt install` fail with `E: Unable to locate package
# libwebkit2gtk-4.1-dev`, dropping the MODE=build path before it could
# complete on every Ubuntu LTS < 24.04 install.
#
# This test asserts the post-install-launcher.sh source contains the
# fallback probe (both 4.1 AND 4.0 referenced as alternative apt-cache
# show targets, with the appropriate libsoup fallback) so a future
# refactor can't silently remove the 4.0 path.
#
# This is a SOURCE-level test (grep-based, no apt invocation) so it
# works in CI without needing the actual apt index of every distro
# matrix.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
LAUNCHER_SH="$ROOT/scripts/post-install-launcher.sh"

if [ ! -f "$LAUNCHER_SH" ]; then
    echo "FAIL: $LAUNCHER_SH not found" >&2
    exit 1
fi

# 1. The script must probe BOTH libwebkit2gtk-4.1-dev AND libwebkit2gtk-4.0-dev
#    via apt-cache show. Hard-pinning either one alone is the regression.
if ! grep -q "apt-cache show libwebkit2gtk-4.1-dev" "$LAUNCHER_SH"; then
    echo "FAIL: post-install-launcher.sh does not probe libwebkit2gtk-4.1-dev via apt-cache" >&2
    exit 1
fi
if ! grep -q "apt-cache show libwebkit2gtk-4.0-dev" "$LAUNCHER_SH"; then
    echo "FAIL: post-install-launcher.sh does not probe libwebkit2gtk-4.0-dev via apt-cache" >&2
    echo "      Ubuntu 22.04 LTS + Debian 12 ship only 4.0-dev; fallback required." >&2
    exit 1
fi

# 2. The script must mirror webkit's choice for libjavascriptcoregtk
#    (the two MUST be in lock-step — mixing 4.1-webkit + 4.0-jscore is
#    a guaranteed link failure). Check both variants are referenced.
if ! grep -q "libjavascriptcoregtk-4.1-dev" "$LAUNCHER_SH"; then
    echo "FAIL: libjavascriptcoregtk-4.1-dev (paired with webkit-4.1) not referenced" >&2
    exit 1
fi
if ! grep -q "libjavascriptcoregtk-4.0-dev" "$LAUNCHER_SH"; then
    echo "FAIL: libjavascriptcoregtk-4.0-dev (paired with webkit-4.0) not referenced" >&2
    echo "      The 4.1/4.0 family must move together — mixing produces link errors." >&2
    exit 1
fi

# 3. The script should fall back libsoup-3.0-dev → libsoup2.4-dev for
#    older Debian/Ubuntu. Both variants must appear so the fallback
#    branch is wired up.
if ! grep -q "libsoup-3.0-dev" "$LAUNCHER_SH"; then
    echo "FAIL: libsoup-3.0-dev not referenced" >&2
    exit 1
fi
if ! grep -q "libsoup2.4-dev" "$LAUNCHER_SH"; then
    echo "FAIL: libsoup2.4-dev fallback not referenced — Debian 11 / very-old Ubuntu users will fail" >&2
    exit 1
fi

# 4. The 4.1 probe must come BEFORE the 4.0 probe in the source (prefer
#    modern variant when available). Verify via line numbers.
line_41=$(grep -n "apt-cache show libwebkit2gtk-4.1-dev" "$LAUNCHER_SH" | head -1 | cut -d: -f1)
line_40=$(grep -n "apt-cache show libwebkit2gtk-4.0-dev" "$LAUNCHER_SH" | head -1 | cut -d: -f1)
if [ "$line_41" -gt "$line_40" ]; then
    echo "FAIL: 4.0-dev probe at line $line_40 comes BEFORE 4.1-dev probe at line $line_41." >&2
    echo "      Order matters: prefer 4.1 when available (modern variant)." >&2
    exit 1
fi

# 5. The script must not hard-pin 4.1 with no fallback (regression check):
#    if there's ONLY a literal `(libwebkit2gtk-4.1-dev ...` array assignment
#    without an apt-cache probe gating it, that's the v0.2.52 bug.
#    We look for `deps_pkgs=(libwebkit2gtk-4.1-dev` directly — the post-fix
#    code uses `deps_pkgs=("$_webkit_pkg" ...` instead.
if grep -q 'deps_pkgs=(libwebkit2gtk-4.1-dev' "$LAUNCHER_SH"; then
    echo "FAIL: regression — deps_pkgs hard-pins libwebkit2gtk-4.1-dev with no fallback." >&2
    echo "      Use the \$_webkit_pkg variable populated by the apt-cache probe instead." >&2
    exit 1
fi

echo "PASS: libwebkit2gtk-4.1-dev/4.0-dev fallback wired correctly in post-install-launcher.sh"
exit 0
