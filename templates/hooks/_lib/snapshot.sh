# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/snapshot.sh — shared snapshot/diff helper for the V52-L.1
# SubagentStart/SubagentStop reconciliation pair.
#
# Background:
#   The SubagentStop reconciler needs to know which files the subagent
#   modified during its run so it can drive KG-sync, code-graph-queue,
#   credential-scan, and nudge-counter side effects. The SubagentStop
#   payload alone is not sufficient — the wire format doesn't reliably
#   ship a "files modified" list (and even when it does, content hashing
#   is the only way to detect modifications that don't go through the
#   Write/Edit tools — e.g. shell-out git commits, sed-style scripts).
#
#   Snapshot pattern: at SubagentStart, hash every file under the watched
#   paths and write the hashes to .claude/state/subagent-snapshot-<id>.json.
#   At SubagentStop, re-hash the same paths and emit the diff (added,
#   modified, deleted files). Standard belt-and-suspenders.
#
# Watched paths (default):
#   - knowledge/**/*.md
#   - docs/**/*.md
#   - <code-extensions> under common code dirs (src/, lib/, launcher/,
#     claude_mcp_servers/, .claude/scripts/, vco_lib/, etc.)
#
#   The path list is sourced from the project's
#   orchestrator-managed-paths.txt when present; otherwise falls back to a
#   conservative built-in list.
#
# Functions:
#   take_snapshot <agent_id> <project_root> [snapshot_dir]
#     Compute SHA-256 of every matching file. Write the snapshot to
#     <snapshot_dir>/subagent-snapshot-<agent_id>.json (default
#     <project_root>/.claude/state). Returns 0 on success, non-zero if
#     state dir cannot be created. Soft-fails on individual hash errors.
#
#   diff_snapshot <agent_id> <project_root> [snapshot_dir]
#     Print, one per line on stdout, paths of files that changed between
#     the snapshot and the current filesystem state. Includes added,
#     modified, and deleted files. Prints nothing if the snapshot is
#     missing (subagent started before the snapshot hook was installed,
#     or the state dir was wiped). Returns 0 always.
#
#   cleanup_snapshot <agent_id> <project_root> [snapshot_dir]
#     Delete the snapshot file. Soft-fail. Returns 0 always.
#
# Performance:
#   take_snapshot is bounded by `find ... -print0 | xargs sha256sum`. On
#   a typical project (~500 .md + ~2000 code files) this completes in
#   <500ms cold. diff_snapshot re-hashes the same paths plus compares
#   against the JSON snapshot — same order of magnitude.
#
# Soft-fail contract: every step is wrapped to never raise. If the
# python interpreter is unavailable, sha256sum is missing, find errors
# out, or the JSON write fails, the function returns silently. The
# caller is expected to be a hook that MUST exit 0 regardless.

# Default code-file extensions. Conservative subset — extend as needed.
# Mirrors the language coverage of code-graph-incremental.sh.
_SNAPSHOT_CODE_EXTS_DEFAULT='py|rs|ts|tsx|js|jsx|go|java|cs|c|cpp|h|hpp|rb|php|swift|kt|scala|sh|ps1|sql'

# Default directories to walk. Includes both "code-ish" dirs and the KG
# / docs dirs so a single snapshot covers everything the reconciler
# cares about.
_SNAPSHOT_DIRS_DEFAULT='knowledge docs src lib launcher claude_mcp_servers .claude/scripts vco_lib templates tests'

# Internal: enumerate files under $project_root that we want to track.
# Outputs newline-delimited relative paths. Tolerates missing dirs.
_snapshot_enumerate_files() {
    local project_root="$1"
    local dirs="${VCT_SNAPSHOT_DIRS:-$_SNAPSHOT_DIRS_DEFAULT}"
    local exts="${VCT_SNAPSHOT_CODE_EXTS:-$_SNAPSHOT_CODE_EXTS_DEFAULT}"
    local d full
    # Build the find expression incrementally: for each existing dir,
    # walk it for .md files (always) + code extensions. We hand-roll the
    # expression rather than relying on Bash 4+ globstar to keep the
    # helper portable to Bash 3.2 (macOS default shell).
    for d in $dirs; do
        full="$project_root/$d"
        [ -d "$full" ] || continue
        # `find` with -type f -print is portable. `find -iregex` defaults
        # to Emacs regex syntax on GNU find (the alternation operator is
        # `\|`, not `|`), and BSD find on macOS supports `-E` global for
        # POSIX-extended. The portable path is to use `-iregextype posix-
        # extended` on GNU and pre-set `-E` on the find invocation for
        # BSD. Since portability across both without a `find -E`/`find
        # -regextype` probe is fragile, we instead loop over each
        # extension and use `-iname '*.<ext>'` — slower (one find pass
        # per ext) but bit-for-bit portable across GNU/BSD/busybox find.
        # On a normal project this still runs in <500 ms thanks to the
        # OS file cache.
        find "$full" -type f -iname '*.md' 2>/dev/null
        # Hand-loop extensions to avoid find-regex portability issues.
        local _ext _IFS_save
        _IFS_save="$IFS"
        IFS='|'
        # shellcheck disable=SC2086
        set -- $exts
        IFS="$_IFS_save"
        for _ext in "$@"; do
            [ -z "$_ext" ] && continue
            find "$full" -type f -iname "*.${_ext}" 2>/dev/null
        done
    done
}

# Take a snapshot of the current filesystem state.
# $1 = agent_id (used in the snapshot filename)
# $2 = project_root (the directory whose files we hash)
# $3 = (optional) snapshot_dir; defaults to $project_root/.claude/state
#
# Output: writes a JSON file with {file_path: sha256, ...}. The file
# format is `{"files": {"path/to/file": "<hex sha256>", ...},
#             "created_at": "<iso>", "agent_id": "<id>",
#             "project_root": "<abs path>"}`.
take_snapshot() {
    local agent_id="$1"
    local project_root="$2"
    local snap_dir="${3:-$project_root/.claude/state}"

    [ -z "$agent_id" ] && return 1
    [ -z "$project_root" ] && return 1
    [ ! -d "$project_root" ] && return 1

    mkdir -p "$snap_dir" 2>/dev/null || return 1

    # Sanitize agent_id for filename use: alnum + dash + underscore only.
    local safe_id
    safe_id=$(printf '%s' "$agent_id" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64)
    [ -z "$safe_id" ] && return 1
    local snap_file="$snap_dir/subagent-snapshot-$safe_id.json"

    # Resolve python for the JSON writer. Same probe pattern as the rest
    # of the _lib/ helpers — soft-fail if no python.
    local py
    py="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
    [ -z "$py" ] && return 1

    # Hash every matched file. Use sha256sum on Linux, shasum -a 256 on
    # macOS (sha256sum isn't on macOS by default). If neither works,
    # write an empty snapshot — the diff at SubagentStop will simply
    # treat every file as "unchanged" and the reconciler degrades to
    # logging only (no false-positive cred-scan storm).
    local hasher
    if command -v sha256sum >/dev/null 2>&1; then
        hasher="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        hasher="shasum -a 256"
    else
        # Write an empty-files snapshot so the diff path can detect
        # "snapshot existed but was empty" vs "snapshot missing".
        printf '%s\n' '{"files":{},"created_at":"","agent_id":"","project_root":"","empty":true}' \
            > "$snap_file" 2>/dev/null
        return 0
    fi

    # Build the snapshot via a Python heredoc. We pipe the file list
    # in on stdin so the shell doesn't have to argv-explode tens of
    # thousands of paths (which can hit the kernel's ARG_MAX on some
    # large projects). Python reads stdin lines, runs the hasher in a
    # subprocess pool, accumulates hashes, writes JSON.
    #
    # We deliberately do NOT use xargs sha256sum directly because:
    #   1. We need atomic snapshot file write (json wrap) — easier in Py.
    #   2. Path normalization (relative to project_root) is cleaner in Py.
    #   3. xargs's argv chunking can lose ordering, complicating diffs.
    _snapshot_enumerate_files "$project_root" 2>/dev/null \
        | SNAP_FILE="$snap_file" \
          AGENT_ID="$agent_id" \
          PROJECT_ROOT="$project_root" \
          HASHER="$hasher" \
          "$py" -c '
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

SNAP_FILE = os.environ.get("SNAP_FILE", "")
AGENT_ID = os.environ.get("AGENT_ID", "")
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "")
if not SNAP_FILE or not PROJECT_ROOT:
    sys.exit(0)

files = {}
project_root = os.path.realpath(PROJECT_ROOT)
# Cap: snapshot at most 50k files. A normal project lives at <5k. A
# pathological CI worktree could hit the cap; we silently truncate and
# still write a valid snapshot (diff will skip files beyond the cap).
MAX_FILES = 50_000
processed = 0

for line in sys.stdin:
    path = line.rstrip("\r\n")
    if not path:
        continue
    if processed >= MAX_FILES:
        break
    # Hash inline rather than spawning an external subprocess per file
    # — much faster (no fork overhead). Read in 64 KB chunks so we do
    # not load multi-MB files into memory.
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        # Skip files larger than 5 MB — those are usually binaries
        # (pdf, images, model weights) we do not want to credential-
        # scan or KG-sync anyway. The hash above already ran, so we
        # do not save any work by skipping early — but excluding them
        # from the snapshot keeps the JSON small and the diff fast.
        # Heuristic only; uses post-read size.
        try:
            if os.path.getsize(path) > 5 * 1024 * 1024:
                continue
        except OSError:
            continue
        rel = os.path.relpath(path, project_root)
        # Skip files that resolve OUTSIDE project_root (symlinks pointing
        # elsewhere). Otherwise diff would emit spurious changes.
        if rel.startswith(".."):
            continue
        files[rel] = h.hexdigest()
        processed += 1
    except (OSError, IOError):
        continue

doc = {
    "version": 1,
    "agent_id": AGENT_ID,
    "project_root": project_root,
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "files": files,
}

# Atomic write: write to a temp file then rename. Avoids a partially-
# written snapshot if the process is killed mid-write.
tmp = SNAP_FILE + ".tmp"
try:
    with open(tmp, "w", encoding="utf-8") as out:
        json.dump(doc, out, separators=(",", ":"))
    os.replace(tmp, SNAP_FILE)
except OSError:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(0)
' 2>/dev/null

    [ -f "$snap_file" ]
}

# Compute the diff between the snapshot and the current filesystem.
# Emits one CHANGED-file relative path per line on stdout. Includes:
#   - Files present in current scan whose hash differs from snapshot
#   - Files present in current scan but absent from snapshot (added)
#   - Files present in snapshot but absent now (deleted) — these still
#     emit so the credential-scanner can confirm the file is gone and
#     the KG-sync layer can drop the stale node.
#
# $1 = agent_id
# $2 = project_root
# $3 = (optional) snapshot_dir
diff_snapshot() {
    local agent_id="$1"
    local project_root="$2"
    local snap_dir="${3:-$project_root/.claude/state}"

    [ -z "$agent_id" ] && return 0
    [ -z "$project_root" ] && return 0
    [ ! -d "$project_root" ] && return 0

    local safe_id
    safe_id=$(printf '%s' "$agent_id" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64)
    [ -z "$safe_id" ] && return 0
    local snap_file="$snap_dir/subagent-snapshot-$safe_id.json"

    [ ! -f "$snap_file" ] && return 0

    local py
    py="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
    [ -z "$py" ] && return 0

    # Re-enumerate + re-hash + diff in one Python pass. Reusing the same
    # 64 KB chunk hashing logic the snapshot uses keeps the two halves
    # bit-for-bit comparable.
    _snapshot_enumerate_files "$project_root" 2>/dev/null \
        | SNAP_FILE="$snap_file" \
          PROJECT_ROOT="$project_root" \
          "$py" -c '
import hashlib
import json
import os
import sys

SNAP_FILE = os.environ.get("SNAP_FILE", "")
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "")
if not SNAP_FILE or not PROJECT_ROOT:
    sys.exit(0)

try:
    with open(SNAP_FILE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
except (OSError, json.JSONDecodeError):
    sys.exit(0)

before = doc.get("files") or {}
project_root = os.path.realpath(PROJECT_ROOT)

after = {}
MAX_FILES = 50_000
processed = 0
for line in sys.stdin:
    path = line.rstrip("\r\n")
    if not path or processed >= MAX_FILES:
        if processed >= MAX_FILES:
            break
        continue
    try:
        if os.path.getsize(path) > 5 * 1024 * 1024:
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        rel = os.path.relpath(path, project_root)
        if rel.startswith(".."):
            continue
        after[rel] = h.hexdigest()
        processed += 1
    except (OSError, IOError):
        continue

# Diff: anything in `after` whose hash differs (or is new), plus
# anything in `before` that disappeared.
changed = set()
for path, h in after.items():
    if before.get(path) != h:
        changed.add(path)
for path in before:
    if path not in after:
        changed.add(path)

# Emit sorted for deterministic test output.
for p in sorted(changed):
    sys.stdout.write(p + "\n")
' 2>/dev/null
    return 0
}

# Delete the snapshot file. Always returns 0.
cleanup_snapshot() {
    local agent_id="$1"
    local project_root="$2"
    local snap_dir="${3:-$project_root/.claude/state}"

    [ -z "$agent_id" ] && return 0
    local safe_id
    safe_id=$(printf '%s' "$agent_id" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64)
    [ -z "$safe_id" ] && return 0
    local snap_file="$snap_dir/subagent-snapshot-$safe_id.json"

    rm -f "$snap_file" 2>/dev/null
    return 0
}
