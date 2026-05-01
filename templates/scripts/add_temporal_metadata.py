#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Add temporal metadata to knowledge graph nodes.

Extracts created/updated dates from git history and adds YAML frontmatter
to all knowledge nodes with temporal tracking fields.

Usage:
    python add_temporal_metadata.py [--dry-run] [--file PATH]

    --dry-run: Show what would be done without modifying files
    --file PATH: Process single file instead of all nodes
"""

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


def get_git_dates(file_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract created and updated timestamps from git history.
    Falls back to filesystem timestamps if file not in git.

    Args:
        file_path: Path to the file

    Returns:
        Tuple of (created_timestamp, updated_timestamp) in ISO 8601 format (hour precision)
    """
    try:
        # Get creation timestamp (first commit that added this file)
        result = subprocess.run(
            ["git", "log", "--follow", "--diff-filter=A", "--format=%aI", "--", str(file_path)],
            capture_output=True,
            text=True,
            check=True
        )
        created_iso = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else None
        if created_iso:
            dt = datetime.fromisoformat(created_iso)
            created = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")
        else:
            created = None

        # Get last update timestamp (most recent commit)
        result = subprocess.run(
            ["git", "log", "--follow", "-1", "--format=%aI", "--", str(file_path)],
            capture_output=True,
            text=True,
            check=True
        )
        updated_iso = result.stdout.strip()
        if updated_iso:
            dt = datetime.fromisoformat(updated_iso)
            updated = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")
        else:
            updated = None

        # If not in git history, use filesystem timestamps
        if not created or not updated:
            stat = file_path.stat()
            # Use ctime (creation on some systems) or mtime as fallback (hour precision)
            if not created:
                dt = datetime.fromtimestamp(stat.st_ctime)
                created = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")
            if not updated:
                dt = datetime.fromtimestamp(stat.st_mtime)
                updated = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")

        return created, updated
    except (subprocess.CalledProcessError, ValueError) as e:
        # Fall back to filesystem timestamps on any error (hour precision)
        stat = file_path.stat()
        dt_created = datetime.fromtimestamp(stat.st_ctime)
        created = dt_created.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")
        dt_updated = datetime.fromtimestamp(stat.st_mtime)
        updated = dt_updated.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")
        return created, updated


def extract_title_and_tags(content: str) -> Tuple[Optional[str], list]:
    """
    Extract title and tags from markdown content.

    Args:
        content: File content

    Returns:
        Tuple of (title, tags_list)
    """
    lines = content.split('\n')
    title = None
    tags = []

    for line in lines[:10]:  # Check first 10 lines
        # Extract title from # heading
        if line.startswith('# ') and not title:
            title = line[2:].strip()
        # Extract inline tags
        if line.strip().startswith('#') and not line.startswith('# '):
            # Parse tags like "#tag1 #tag2 #tag3"
            tag_matches = re.findall(r'#([a-zA-Z0-9_-]+)', line)
            tags.extend(tag_matches)

    return title, tags


def determine_node_type(file_path: Path) -> str:
    """
    Determine node type from directory structure.

    Args:
        file_path: Path to the file

    Returns:
        Node type string
    """
    parts = file_path.parts
    if 'projects' in parts:
        return 'project'
    elif 'concepts' in parts:
        return 'concept'
    elif 'tools' in parts:
        return 'tool'
    elif 'research' in parts:
        return 'research'
    elif 'patterns' in parts:
        return 'pattern'
    elif 'models' in parts:
        return 'model'
    elif 'hardware' in parts:
        return 'hardware'
    else:
        return 'concept'  # default


def determine_status(tags: list, content: str) -> str:
    """
    Determine status from tags and content.

    Args:
        tags: List of tags
        content: File content

    Returns:
        Status string
    """
    if 'archived' in tags or 'deprecated' in tags:
        return 'archived'
    elif 'idea' in tags:
        return 'active'  # Ideas are active (to be implemented)
    elif 'in-progress' in tags:
        return 'active'
    elif 'implemented' in tags:
        return 'active'  # Still active/valid knowledge
    else:
        return 'active'  # Default to active


def has_frontmatter(content: str) -> bool:
    """Check if content already has YAML frontmatter."""
    return content.strip().startswith('---')


def add_frontmatter(file_path: Path, dry_run: bool = False, update: bool = False) -> bool:
    """
    Add or update temporal metadata frontmatter in a knowledge node.

    Args:
        file_path: Path to the markdown file
        dry_run: If True, only show what would be done
        update: If True, update existing frontmatter instead of skipping

    Returns:
        True if file was modified, False otherwise
    """
    # Get relative path for display
    try:
        rel_path = file_path.relative_to(Path.cwd())
    except ValueError:
        rel_path = file_path.name

    # Read current content
    content = file_path.read_text()

    # Check for existing frontmatter
    has_fm = has_frontmatter(content)

    if has_fm and not update:
        print(f"  ⏭️  Already has frontmatter: {rel_path}")
        return False

    # Extract existing frontmatter content (if updating)
    content_body = content
    if has_fm and update:
        parts = content.split('---', 2)
        if len(parts) >= 3:
            content_body = parts[2].strip()

    # Extract metadata (use content_body to avoid re-parsing old frontmatter)
    title, tags = extract_title_and_tags(content_body)
    node_type = determine_node_type(file_path)
    status = determine_status(tags, content_body)
    created, updated = get_git_dates(file_path)

    # Use created timestamp as valid_from
    valid_from = created

    # Build frontmatter
    frontmatter = "---\n"
    frontmatter += f'title: "{title or file_path.stem}"\n'
    frontmatter += f'type: {node_type}\n'
    frontmatter += f'tags: [{", ".join(tags)}]\n'
    frontmatter += f'created: {created or "unknown"}\n'
    frontmatter += f'updated: {updated or "unknown"}\n'
    frontmatter += f'valid_from: {valid_from or "unknown"}\n'
    frontmatter += f'valid_until: null\n'
    frontmatter += f'status: {status}\n'
    frontmatter += "---\n\n"

    # Combine frontmatter + content
    new_content = frontmatter + content_body

    action = "update" if (has_fm and update) else "add"

    if dry_run:
        print(f"  🔍 Would {action} frontmatter: {rel_path}")
        print(f"     Title: {title}, Type: {node_type}, Status: {status}")
        print(f"     Created: {created}, Updated: {updated}")
    else:
        file_path.write_text(new_content)
        print(f"  ✅ {'Updated' if action == 'update' else 'Added'} frontmatter: {rel_path}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Add temporal metadata to knowledge nodes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without modifying files")
    parser.add_argument("--file", type=Path, help="Process single file instead of all nodes")
    parser.add_argument("--update", action="store_true", help="Update existing frontmatter (regenerate temporal fields)")
    args = parser.parse_args()

    # Find project root
    project_root = Path.cwd()
    while project_root != project_root.parent:
        if (project_root / ".claude").exists():
            break
        project_root = project_root.parent
    else:
        print("Error: Not in a Claude project (no .claude directory found)")
        return 1

    knowledge_dir = project_root / "knowledge"
    if not knowledge_dir.exists():
        print(f"Error: Knowledge directory not found at {knowledge_dir}")
        return 1

    # Get files to process
    if args.file:
        files = [args.file] if args.file.exists() else []
        if not files:
            print(f"Error: File not found: {args.file}")
            return 1
    else:
        files = list(knowledge_dir.rglob("*.md"))

    if not files:
        print("No markdown files found to process")
        return 0

    print(f"\n{'='*60}")
    print(f"Temporal Metadata Migration")
    print(f"{'='*60}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Files to process: {len(files)}")
    print(f"{'='*60}\n")

    # Process files
    modified_count = 0
    skipped_count = 0

    for file_path in sorted(files):
        if add_frontmatter(file_path, args.dry_run, args.update):
            modified_count += 1
        else:
            skipped_count += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Modified: {modified_count}")
    print(f"  Skipped (already has frontmatter): {skipped_count}")
    print(f"  Total: {len(files)}")
    print(f"{'='*60}\n")

    if args.dry_run:
        print("This was a DRY RUN. Run without --dry-run to apply changes.")
    else:
        print("✅ Migration complete!")
        print("\nNext steps:")
        print("  1. Review changes: git diff")
        print("  2. Test queries: python .claude/scripts/query_temporal.py")
        print("  3. Sync to Weaviate: .claude/scripts/kg-sync --all")

    return 0


if __name__ == "__main__":
    exit(main())
