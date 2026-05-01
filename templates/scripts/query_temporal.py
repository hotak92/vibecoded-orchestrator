#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Query knowledge graph with temporal filters.

Demonstrates point-in-time queries and temporal range searches.

Usage:
    python query_temporal.py --date 2026-01-20  # What existed on this date?
    python query_temporal.py --from 2026-01-15 --to 2026-01-28  # Date range
    python query_temporal.py --recent 7  # Last N days
    python query_temporal.py --status active  # Filter by status
    python query_temporal.py --type project --recent 14  # Combined filters
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
import yaml


def parse_frontmatter(file_path: Path) -> Optional[dict]:
    """
    Parse YAML frontmatter from markdown file.

    Args:
        file_path: Path to markdown file

    Returns:
        Dictionary of frontmatter fields, or None if no frontmatter
    """
    content = file_path.read_text()

    if not content.strip().startswith('---'):
        return None

    # Extract frontmatter
    parts = content.split('---', 2)
    if len(parts) < 3:
        return None

    try:
        return yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None


def is_valid_at_date(metadata: dict, target_date: str) -> bool:
    """
    Check if node was valid at the target date.

    Args:
        metadata: Frontmatter dictionary
        target_date: Date string in YYYY-MM-DD format

    Returns:
        True if node was valid at that date
    """
    valid_from = metadata.get('valid_from')
    valid_until = metadata.get('valid_until')

    if not valid_from:
        return True  # No validity info, assume always valid

    # Convert to comparable format
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    valid_from_date = datetime.strptime(str(valid_from), "%Y-%m-%d").date()

    # Check lower bound
    if target < valid_from_date:
        return False

    # Check upper bound (None means still valid)
    if valid_until:
        valid_until_date = datetime.strptime(str(valid_until), "%Y-%m-%d").date()
        if target > valid_until_date:
            return False

    return True


def matches_filters(metadata: dict, args) -> bool:
    """
    Check if node matches all specified filters.

    Args:
        metadata: Frontmatter dictionary
        args: Command-line arguments

    Returns:
        True if node matches all filters
    """
    # Status filter
    if args.status and metadata.get('status') != args.status:
        return False

    # Type filter
    if args.type and metadata.get('type') != args.type:
        return False

    # Tags filter (any tag matches)
    if args.tags:
        node_tags = metadata.get('tags', [])
        if not any(tag in node_tags for tag in args.tags):
            return False

    # Date filters
    if args.date:
        if not is_valid_at_date(metadata, args.date):
            return False

    if args.from_date or args.to_date:
        created = metadata.get('created')
        if created:
            created_date = datetime.strptime(str(created), "%Y-%m-%d").date()

            if args.from_date:
                from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
                if created_date < from_date:
                    return False

            if args.to_date:
                to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
                if created_date > to_date:
                    return False

    if args.recent:
        updated = metadata.get('updated')
        if updated:
            updated_date = datetime.strptime(str(updated), "%Y-%m-%d").date()
            cutoff = datetime.now().date() - timedelta(days=args.recent)
            if updated_date < cutoff:
                return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Query knowledge graph with temporal filters")
    parser.add_argument("--date", help="Point-in-time query: show nodes valid on this date (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date", help="Show nodes created from this date")
    parser.add_argument("--to", dest="to_date", help="Show nodes created until this date")
    parser.add_argument("--recent", type=int, help="Show nodes updated in last N days")
    parser.add_argument("--status", choices=["active", "archived", "deprecated"], help="Filter by status")
    parser.add_argument("--type", help="Filter by node type (project, concept, tool, etc.)")
    parser.add_argument("--tags", nargs="+", help="Filter by tags (any match)")
    parser.add_argument("--verbose", action="store_true", help="Show full metadata")
    args = parser.parse_args()

    # Find project root
    project_root = Path.cwd()
    while project_root != project_root.parent:
        if (project_root / ".claude").exists():
            break
        project_root = project_root.parent
    else:
        print("Error: Not in a Claude project")
        return 1

    knowledge_dir = project_root / "knowledge"
    if not knowledge_dir.exists():
        print(f"Error: Knowledge directory not found at {knowledge_dir}")
        return 1

    # Find all knowledge nodes
    files = list(knowledge_dir.rglob("*.md"))

    # Query
    results = []
    for file_path in files:
        metadata = parse_frontmatter(file_path)
        if not metadata:
            continue

        if matches_filters(metadata, args):
            results.append((file_path, metadata))

    # Display results
    if not results:
        print("No nodes match the specified filters.")
        return 0

    print(f"\n{'='*70}")
    print(f"Temporal Query Results")
    print(f"{'='*70}")

    if args.date:
        print(f"Point-in-time: {args.date}")
    if args.from_date or args.to_date:
        print(f"Date range: {args.from_date or 'beginning'} to {args.to_date or 'now'}")
    if args.recent:
        print(f"Updated in last {args.recent} days")
    if args.status:
        print(f"Status: {args.status}")
    if args.type:
        print(f"Type: {args.type}")
    if args.tags:
        print(f"Tags: {', '.join(args.tags)}")

    print(f"\nFound {len(results)} nodes:\n")

    # Group by type for better organization
    by_type = {}
    for file_path, metadata in results:
        node_type = metadata.get('type', 'unknown')
        if node_type not in by_type:
            by_type[node_type] = []
        by_type[node_type].append((file_path, metadata))

    # Display grouped results
    for node_type, nodes in sorted(by_type.items()):
        print(f"\n{node_type.upper()}S ({len(nodes)}):")
        print("-" * 70)

        for file_path, metadata in sorted(nodes, key=lambda x: x[1].get('title', '')):
            title = metadata.get('title', file_path.stem)
            created = metadata.get('created', 'unknown')
            updated = metadata.get('updated', 'unknown')
            status = metadata.get('status', 'unknown')

            print(f"  • {title}")
            print(f"    Created: {created} | Updated: {updated} | Status: {status}")

            if args.verbose:
                tags = metadata.get('tags', [])
                print(f"    Tags: {', '.join(tags)}")
                print(f"    Valid: {metadata.get('valid_from')} to {metadata.get('valid_until') or 'present'}")
                print(f"    File: {file_path.relative_to(knowledge_dir)}")

            print()

    print(f"{'='*70}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
