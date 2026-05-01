#!/usr/bin/env bash
# detect-project.sh — Auto-detect which project a file belongs to.
#
# Given a file path, checks if it's under the current project root.
# If not, looks for a sibling project folder under the common parent
# (e.g. ~/dev/) and returns that project's name.
#
# Usage:
#   source detect-project.sh
#   PROJECT=$(detect_project_for_file "/path/to/file.py" "/current/project/root")
#   # Returns project name (e.g. "MyProject") or empty string for current project
#
# The returned name matches Weaviate collection prefixes (e.g. MyProject_CodeFunction).

detect_project_for_file() {
    local file_path="$1"
    local current_root="$2"

    # Normalize: strip trailing slash
    current_root="${current_root%/}"

    # If file is under current project root, no override needed
    if [[ "$file_path" == "$current_root"/* ]]; then
        echo ""
        return 0
    fi

    # Find the common parent directory (direct parent of project roots)
    local parent_dir
    parent_dir="$(dirname "$current_root")"

    # Check if the file is under a sibling folder of current_root
    if [[ "$file_path" == "$parent_dir"/* ]]; then
        # Extract the sibling folder name (first path component after parent)
        local relative="${file_path#$parent_dir/}"
        local sibling_name="${relative%%/*}"

        # Verify it's an actual directory (not a file in parent)
        if [ -d "$parent_dir/$sibling_name" ]; then
            echo "$sibling_name"
            return 0
        fi
    fi

    # File is not under any sibling project — return empty (use default)
    echo ""
    return 0
}
