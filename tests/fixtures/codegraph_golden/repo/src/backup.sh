#!/usr/bin/env bash
# Backup script for the golden-fixture repo (Shell, regex-parsed).
# Exercises both function syntaxes: `name()` and `function name`.

set -euo pipefail

source ./lib/common.sh

# POSIX-style: name() { ... }
prepare_dir() {
    local dir="$1"
    mkdir -p "$dir"
}

# ksh/bash-style: function name { ... }
function upload_archive {
    local target="$1"
    curl -fsSL -X POST "https://example.invalid/upload" --data-binary "@${target}"
}

run_backup() {
    prepare_dir "/data/backups"
    if [ -d "/data/backups" ]; then
        upload_archive "/data/backups/latest.tar"
    fi
}

run_backup
