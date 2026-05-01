#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Migrate Knowledge Nodes to Vocabulary Standard

Scans all knowledge nodes and suggests/applies fixes to comply with:
- knowledge/VOCABULARY.md
- knowledge/TAG_HIERARCHY.md

Usage:
    python migrate_to_vocabulary.py --check           # Check all nodes, report issues
    python migrate_to_vocabulary.py --fix             # Apply automatic fixes
    python migrate_to_vocabulary.py --file <path>     # Check/fix single file
    python migrate_to_vocabulary.py --interactive     # Interactive mode with prompts
"""

import sys
from pathlib import Path
from typing import List, Dict, Tuple
import re
import yaml
from datetime import datetime, timezone

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import validation function from sync script
sys.path.insert(0, str(Path(__file__).parent))
from sync_knowledge_graph import validate_node_against_vocabulary, parse_markdown_node

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


def suggest_tags(node_data: Dict, file_path: Path) -> Dict[str, List[str]]:
    """
    Suggest tags to add based on node content and type.

    Args:
        node_data: Parsed node data
        file_path: Path to node file

    Returns:
        Dict with suggested tags by category
    """
    suggestions = {
        "domain": [],
        "abstraction": [],
        "technology": [],
        "status": [],
        "project": []
    }

    node_type = node_data.get("node_type", "")
    tags = set(node_data.get("tags", []))
    content = node_data.get("content", "").lower()
    title = node_data.get("title", "").lower()

    # Suggest domain tags based on content/title
    domain_keywords = {
        "AI": ["artificial intelligence", "machine learning", "neural", "model", "inference"],
        "ML": ["machine learning", "training", "dataset", "model"],
        "NLP": ["natural language", "text", "tokenization", "embedding"],
        "CV": ["computer vision", "image", "visual", "detection"],
        "database": ["database", "query", "schema", "storage", "weaviate", "postgresql"],
        "workflow": ["workflow", "automation", "orchestration", "pipeline"],
        "tooling": ["tool", "utility", "script", "cli"],
        "infrastructure": ["infrastructure", "deployment", "container", "docker"],
        "frontend": ["frontend", "ui", "component", "react", "vue"],
        "backend": ["backend", "api", "server", "endpoint"],
        "security": ["security", "authentication", "authorization", "encryption"]
    }

    for domain, keywords in domain_keywords.items():
        if domain not in tags and any(kw in content or kw in title for kw in keywords):
            suggestions["domain"].append(domain)

    # Suggest abstraction level based on node type and content
    if node_type in {"project", "concept", "pattern"}:
        if "architecture" in content or "system design" in content or "overview" in content:
            if "mid-level-architecture" not in tags:
                suggestions["abstraction"].append("mid-level-architecture")
        elif "roadmap" in content or "plan" in content or "strategy" in content:
            if "high-level-plan" not in tags:
                suggestions["abstraction"].append("high-level-plan")
        elif "implementation" in content or "code" in content or "api" in content:
            if "low-level-implementation" not in tags:
                suggestions["abstraction"].append("low-level-implementation")

    # Suggest technology tags based on content
    tech_keywords = {
        "python": ["python", ".py"],
        "javascript": ["javascript", "js", "node"],
        "typescript": ["typescript", "ts"],
        "weaviate": ["weaviate"],
        "ollama": ["ollama"],
        "fastapi": ["fastapi"],
        "react": ["react"],
        "docker": ["docker", "container"],
        "RAG": ["retrieval augmented", "rag"],
        "MCP": ["model context protocol", "mcp"]
    }

    for tech, keywords in tech_keywords.items():
        if tech not in tags and any(kw in content or kw in title for kw in keywords):
            suggestions["technology"].append(tech)

    # Suggest status tag if missing
    status_tags = {"idea", "in-progress", "implemented", "tested", "deployed", "archived", "deprecated"}
    has_status = any(tag in status_tags for tag in tags)
    if not has_status:
        if node_type == "project":
            suggestions["status"].append("in-progress")  # Default for projects
        elif "deprecated" in content or "obsolete" in content:
            suggestions["status"].append("deprecated")
        elif "archive" in str(file_path):
            suggestions["status"].append("archived")

    # Suggest project tag based on file path
    rel_path = file_path.relative_to(KNOWLEDGE_ROOT)
    path_str = str(rel_path).lower()

    # Add project-specific tag patterns here as needed.
    # Example: {"my-project": ["my-project", "alias-keyword"]}
    project_patterns = {
        "claude-orchestrator": ["orchestrator", "meta-project"],
    }

    for project, patterns in project_patterns.items():
        if project not in tags and any(p in path_str or p in title for p in patterns):
            suggestions["project"].append(project)

    # Remove empty categories
    return {k: v for k, v in suggestions.items() if v}


def fix_tag_format(tag: str) -> str:
    """
    Fix tag format to comply with vocabulary.

    Args:
        tag: Original tag

    Returns:
        Fixed tag
    """
    # Replace spaces with hyphens
    tag = tag.replace(" ", "-")

    # Replace underscores with hyphens
    tag = tag.replace("_", "-")

    # Convert to lowercase unless it's an acronym
    if tag.isupper() or len(tag) <= 3:
        return tag  # Keep acronyms uppercase
    else:
        return tag.lower()


def migrate_node(file_path: Path, apply_fixes: bool = False, interactive: bool = False) -> Tuple[bool, List[str]]:
    """
    Check and optionally fix a single node.

    Args:
        file_path: Path to markdown file
        apply_fixes: Whether to apply automatic fixes
        interactive: Whether to prompt for user decisions

    Returns:
        (success, list of changes made)
    """
    changes = []

    try:
        content = file_path.read_text(encoding='utf-8')
        node_data = parse_markdown_node(content, file_path)

        # Validate
        warnings = validate_node_against_vocabulary(node_data, file_path)

        if not warnings:
            return True, []

        # Report warnings
        for warning in warnings:
            changes.append(f"⚠️  {warning}")

        # Get suggestions
        tag_suggestions = suggest_tags(node_data, file_path)

        # Apply fixes if requested
        if apply_fixes or interactive:
            # Parse frontmatter
            frontmatter_match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
            if not frontmatter_match:
                changes.append("❌ No frontmatter found - cannot apply fixes")
                return False, changes

            frontmatter_text = frontmatter_match.group(1)
            frontmatter = yaml.safe_load(frontmatter_text)
            original_tags = frontmatter.get("tags", [])
            new_tags = original_tags.copy()

            # Fix tag format
            for i, tag in enumerate(new_tags):
                fixed = fix_tag_format(tag)
                if fixed != tag:
                    new_tags[i] = fixed
                    changes.append(f"Fixed tag format: '{tag}' → '{fixed}'")

            # Add suggested tags
            if interactive:
                print(f"\n📝 {file_path.relative_to(KNOWLEDGE_ROOT)}")
                print(f"   Current tags: {', '.join(original_tags)}")
                print(f"\n   Suggestions:")
                for category, tags in tag_suggestions.items():
                    print(f"   - {category}: {', '.join(tags)}")

                response = input("\n   Apply suggestions? (y/n/select): ").strip().lower()

                if response == "y":
                    for category, tags in tag_suggestions.items():
                        for tag in tags:
                            if tag not in new_tags:
                                new_tags.append(tag)
                                changes.append(f"Added {category} tag: '{tag}'")
                elif response == "select":
                    for category, tags in tag_suggestions.items():
                        for tag in tags:
                            add = input(f"   Add '{tag}' ({category})? (y/n): ").strip().lower()
                            if add == "y" and tag not in new_tags:
                                new_tags.append(tag)
                                changes.append(f"Added {category} tag: '{tag}'")
            elif apply_fixes:
                # Auto-add first suggestion from each category
                for category, tags in tag_suggestions.items():
                    if tags and tags[0] not in new_tags:
                        new_tags.append(tags[0])
                        changes.append(f"Added {category} tag: '{tags[0]}'")

            # Update frontmatter if changes made
            if new_tags != original_tags:
                frontmatter["tags"] = new_tags

                # Reconstruct file
                new_frontmatter = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
                new_content = f"---\n{new_frontmatter}---\n{content[frontmatter_match.end():]}"

                file_path.write_text(new_content, encoding='utf-8')
                changes.append(f"✅ Updated file with {len(new_tags)} tags")

        return True, changes

    except Exception as e:
        changes.append(f"❌ Error: {e}")
        return False, changes


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Migrate knowledge nodes to vocabulary standard")
    parser.add_argument("--check", action="store_true", help="Check all nodes, report issues (no changes)")
    parser.add_argument("--fix", action="store_true", help="Apply automatic fixes to all nodes")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode with prompts")
    parser.add_argument("--file", type=str, help="Check/fix single file")

    args = parser.parse_args()

    if not any([args.check, args.fix, args.interactive, args.file]):
        parser.print_help()
        sys.exit(1)

    # Get files to process
    if args.file:
        file_path = Path(args.file)
        # Convert to absolute path if relative
        if not file_path.is_absolute():
            file_path = PROJECT_ROOT / file_path
        files = [file_path]
    else:
        files = list(KNOWLEDGE_ROOT.glob("**/*.md"))
        files = [f for f in files if f.is_file() and not f.name.startswith(".")]

    print(f"\n📊 Scanning {len(files)} knowledge nodes...\n")

    total_issues = 0
    total_changes = 0

    for file_path in files:
        success, changes = migrate_node(
            file_path,
            apply_fixes=args.fix,
            interactive=args.interactive
        )

        if changes:
            print(f"\n📄 {file_path.relative_to(KNOWLEDGE_ROOT)}")
            for change in changes:
                print(f"   {change}")
            total_issues += 1
            total_changes += len([c for c in changes if c.startswith("✅") or c.startswith("Added") or c.startswith("Fixed")])

    print(f"\n📊 Summary:")
    print(f"   Total nodes scanned: {len(files)}")
    print(f"   Nodes with issues: {total_issues}")
    print(f"   Changes applied: {total_changes}")

    if args.check:
        print(f"\n💡 Run with --fix to apply automatic fixes")
        print(f"   Or use --interactive for manual review")


if __name__ == "__main__":
    main()
