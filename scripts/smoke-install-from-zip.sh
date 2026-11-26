#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# smoke-install-from-zip.sh — install-from-the-release-zip smoke, in three
# subcommands so a workflow can run the OS's real first-install entry point
# (cmd.exe for first-install.bat) between them:
#
#   extract <zip> <dest-dir>   unzip into a fresh <dest-dir> (must not exist,
#                              or be empty) and print the single top folder
#                              the archive holds. Refuses a zip that does not
#                              hold exactly one top folder.
#   probe   <root> <target>    pre-install checks on the extracted tree: the
#                              layout an install needs is there, and
#                              `install.py --bootstrap --json` succeeds and
#                              finds the launcher + hub binaries at
#                              launcher/dist/<target>/. This is the step a
#                              v0.2.108 zip fails (no vco_lib/ — the
#                              bootstrap dies on its first import).
#   verify  <root> <target>    post-install: the install landed (.venv,
#                              state/, .env with KG_COLLECTION, one bundle
#                              file per kind) and the bootstrap envelope
#                              reports a completed install.
#
# Callers: .github/workflows/install-smoke-release-zip.yml (PR CI, all three
# OSes) and the release build job (the real zip, before upload). The zip
# itself comes from scripts/stage-release-archive.sh. Locked by
# tests/test_stage_release_archive.py.
#
# PYTHON overrides the interpreter used for the pre-install probe.

set -euo pipefail

die() { echo "::error::smoke-install-from-zip: $*" >&2; exit 1; }

pick_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
    for c in python3 python py; do
        if command -v "$c" >/dev/null 2>&1 \
           && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            echo "$c"; return
        fi
    done
    die "no Python 3.11+ found for the probe"
}

exe_suffix() {
    case "$1" in
        windows-x64) echo ".exe" ;;
        linux-x64|macos-arm64) echo "" ;;
        *) die "unknown target '$1' (expected linux-x64, windows-x64 or macos-arm64)" ;;
    esac
}

# Assert the bootstrap envelope in $1 (JSON file) with mode $2 (pre|post).
check_envelope() {
    "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8-sig") as fh:
    d = json.load(fh)
paths = d.get("paths") or {}
errors = []
if paths.get("launcher_binary_exists") is not True:
    errors.append(f"launcher_binary_exists={paths.get('launcher_binary_exists')!r} "
                  f"(looked at {paths.get('launcher_binary')!r})")
if paths.get("vct_hub_binary_exists") is not True:
    errors.append(f"vct_hub_binary_exists={paths.get('vct_hub_binary_exists')!r} "
                  f"(looked at {paths.get('vct_hub_binary')!r})")
if mode == "post":
    if paths.get("install_root_kind") != "completed_install":
        errors.append(f"install_root_kind={paths.get('install_root_kind')!r}, expected 'completed_install'")
    if paths.get("state_dir_exists") is not True:
        errors.append("state_dir_exists is not True after install")
    # The smoke installs with --no-containers: a container-runtime blocker is
    # expected on runners; any other blocker is a real failure.
    blockers = d.get("blocker_messages") or []
    other = [b for b in blockers
             if not any(k in b.lower() for k in ("podman", "docker", "container"))]
    if other:
        errors.append(f"non-container blockers: {other}")
for e in errors:
    print(f"::error::bootstrap envelope ({mode}-install): {e}")
sys.exit(1 if errors else 0)
PYEOF
}

cmd="${1:-}"
case "$cmd" in
extract)
    [ $# -eq 3 ] || die "usage: $0 extract <zip> <dest-dir>"
    zip="$2"; dest="$3"
    [ -f "$zip" ] || die "zip not found: $zip"
    if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        die "destination is not empty: $dest"
    fi
    mkdir -p "$dest"
    if command -v unzip >/dev/null 2>&1; then
        unzip -q "$zip" -d "$dest"
    elif command -v pwsh >/dev/null 2>&1; then
        wz="$zip"; wd="$dest"
        if command -v cygpath >/dev/null 2>&1; then wz="$(cygpath -w "$zip")"; wd="$(cygpath -w "$dest")"; fi
        ZIP_PATH="$wz" DEST_PATH="$wd" pwsh -NoProfile -NonInteractive -Command \
            'Expand-Archive -LiteralPath $env:ZIP_PATH -DestinationPath $env:DEST_PATH -Force'
    else
        die "neither 'unzip' nor 'pwsh' is available"
    fi
    tops=()
    for e in "$dest"/* "$dest"/.[!.]*; do
        [ -e "$e" ] && tops+=("$e")
    done
    [ "${#tops[@]}" -eq 1 ] && [ -d "${tops[0]}" ] \
        || die "the zip must hold exactly one top folder; found: ${tops[*]:-nothing}"
    (cd "${tops[0]}" && pwd)
    ;;
probe)
    [ $# -eq 3 ] || die "usage: $0 probe <root> <target>"
    root="$2"; target="$3"; x="$(exe_suffix "$target")"
    [ -d "$root" ] || die "not a directory: $root"
    missing=0
    for rel in install.py first-install.sh pyproject.toml vco_lib/__init__.py \
               scripts/lib migrations \
               "launcher/dist/$target/vct-launcher$x" \
               "launcher/dist/$target/vct-hub$x" \
               "launcher/dist/$target/vct-updater$x"; do
        if [ -e "$root/$rel" ]; then
            echo "  [ok] $rel"
        else
            echo "::error::extracted tree lacks $rel"
            missing=$((missing + 1))
        fi
    done
    [ "$missing" -eq 0 ] || die "$missing required entr(ies) missing from the extracted zip"
    PY="$(pick_python)"
    if ! (cd "$root" && "$PY" install.py --bootstrap --json > bootstrap-pre.json 2> bootstrap-pre.stderr); then
        cat "$root/bootstrap-pre.stderr" >&2 || true
        die "install.py --bootstrap --json failed in the extracted tree"
    fi
    check_envelope "$root/bootstrap-pre.json" pre || die "pre-install bootstrap envelope rejected"
    echo "Extracted zip layout OK; bootstrap probe finds the binaries."
    ;;
verify)
    [ $# -eq 3 ] || die "usage: $0 verify <root> <target>"
    root="$2"; target="$3"; x="$(exe_suffix "$target")"
    missing=0
    for d in .venv state; do
        if [ -d "$root/$d" ]; then echo "  [ok] $d/"; else echo "::error::MISSING directory: $d"; missing=$((missing + 1)); fi
    done
    if [ "$target" = "windows-x64" ]; then
        sentinels=(.claude/hooks/pre-tool-use.ps1 .claude/scripts/kg-sync.ps1 .claude/skills/rc-native/SKILL.md)
        PY="$root/.venv/Scripts/python.exe"
    else
        sentinels=(.claude/hooks/pre-tool-use.sh .claude/scripts/kg-sync .claude/scripts/kg-sync.ps1 .claude/skills/rc-native/SKILL.md)
        PY="$root/.venv/bin/python"
    fi
    for f in "${sentinels[@]}"; do
        if [ -f "$root/$f" ]; then echo "  [ok] $f"; else echo "::error::MISSING bundle file: $f"; missing=$((missing + 1)); fi
    done
    if [ -f "$root/.env" ] && grep -Eq '^KG_COLLECTION=.+' "$root/.env"; then
        echo "  [ok] .env has KG_COLLECTION"
    else
        echo "::error::.env missing or without a non-empty KG_COLLECTION="
        missing=$((missing + 1))
    fi
    [ "$missing" -eq 0 ] || die "$missing install artifact(s) missing — the install from the zip is incomplete"
    [ -x "$PY" ] || [ -f "$PY" ] || die "venv interpreter not found: $PY"
    if ! (cd "$root" && "$PY" install.py --bootstrap --json > bootstrap-post.json 2> bootstrap-post.stderr); then
        cat "$root/bootstrap-post.stderr" >&2 || true
        die "install.py --bootstrap --json (post-install) failed"
    fi
    check_envelope "$root/bootstrap-post.json" post || die "post-install bootstrap envelope rejected"
    echo "Install from the zip OK ($target, launcher/dist/$target/vct-launcher$x)."
    ;;
*)
    sed -n '2,/^# PYTHON overrides/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
