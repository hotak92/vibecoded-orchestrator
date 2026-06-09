#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Duplicate Detection for Knowledge Graph

Finds potential duplicate nodes using:
1. Semantic similarity (>0.95 threshold)
2. Filename similarity (Levenshtein distance)
3. Title similarity

Usage:
    python detect_duplicates.py                    # Full scan
    python detect_duplicates.py --threshold 0.90   # Custom threshold
    python detect_duplicates.py --auto-merge       # Auto-merge high-confidence duplicates
"""

import os
import sys
from pathlib import Path
from typing import List, Dict, Tuple
from datetime import datetime
import re

# weaviate_mcp is pip-installed as an editable package by install.py (A1, v0.2.38).
# This script uses the weaviate client directly, not weaviate_mcp symbols,
# so no sys.path manipulation is needed for claude_mcp_servers/.
sys.path.insert(0, str(Path.home() / ".claude" / "workflow" / "scripts"))

# v0.2.52 (Known Issue 6, Sub-issue A): silence
# ``AuthlibDeprecationWarning`` from ``weaviate-client``'s transitive
# ``authlib`` dep.  See ``claude_mcp_servers/weaviate_mcp/server.py``
# for the canonical filter rationale.
import warnings as _dd_warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDeprecationWarning  # type: ignore
    _dd_warnings.filterwarnings("ignore", category=_AuthlibDeprecationWarning)
except ImportError:
    _dd_warnings.filterwarnings(
        "ignore",
        message=r".*authlib.*deprecated.*",
        category=DeprecationWarning,
    )

import weaviate
from weaviate.classes.query import Filter, MetadataQuery

# Configuration
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
DEFAULT_SIMILARITY_THRESHOLD = 0.95
PROJECT_ROOT = Path(__file__).parent.parent.parent


# v0.2.21 Step 18 (caller migration): resolve KG collection via the
# launcher's vct-hub. Falls back to env when the hub is unreachable.
def _resolve_kg_collection() -> str:
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        cfg = resolve(PROJECT_ROOT)
        return cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph")
    except Exception:
        return os.getenv("KG_COLLECTION", "KnowledgeGraph")


COLLECTION_NAME = _resolve_kg_collection()


def levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein distance between two strings"""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # cost of insertions, deletions or substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def normalize_title(title: str) -> str:
    """Normalize title for comparison"""
    # Remove special chars, lowercase, strip whitespace
    normalized = re.sub(r'[^a-zA-Z0-9\s]', '', title.lower())
    normalized = ' '.join(normalized.split())  # Normalize whitespace
    return normalized


def title_similarity(title1: str, title2: str) -> float:
    """Calculate title similarity (0.0 to 1.0)"""
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)

    if not norm1 or not norm2:
        return 0.0

    # Calculate Levenshtein similarity
    max_len = max(len(norm1), len(norm2))
    distance = levenshtein_distance(norm1, norm2)
    similarity = 1.0 - (distance / max_len)

    return similarity


class DuplicateDetector:
    """Detect potential duplicate nodes in knowledge graph"""

    def __init__(self, similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD):
        """Initialize detector"""
        self.threshold = similarity_threshold
        self.client = weaviate.connect_to_custom(
            http_host='localhost',
            http_port=8081,
            http_secure=False,
            grpc_host='localhost',
            grpc_port=GRPC_PORT,
            grpc_secure=False
        )
        self.collection = self.client.collections.get(COLLECTION_NAME)

    def close(self):
        """Close Weaviate connection"""
        try:
            self.client.close()
        except:
            pass

    def find_duplicates(self) -> List[Dict]:
        """
        Find all potential duplicates in knowledge graph

        Returns:
            List of duplicate groups with metadata
        """
        print(f"🔍 Scanning for duplicates (threshold: {self.threshold})")
        print(f"   Collection: {COLLECTION_NAME}\n")

        # Get all nodes (first chunks only - they have full metadata)
        # v0.2.46 V46-D: cursor-paginate so collections > 1000 nodes are
        # fully scanned (previously duplicates in nodes 1001+ were
        # silently missed).
        try:
            nodes = []
            cursor = None
            PAGE_SIZE = 1000
            chunk_filter = Filter.by_property("chunk_num").equal(1)
            while True:
                if cursor is not None:
                    page = self.collection.query.fetch_objects(
                        filters=chunk_filter,
                        limit=PAGE_SIZE,
                        after=cursor,
                        return_metadata=MetadataQuery(distance=True),
                        return_properties=["title", "file_path", "node_type", "tags"]
                    )
                else:
                    page = self.collection.query.fetch_objects(
                        filters=chunk_filter,
                        limit=PAGE_SIZE,
                        return_metadata=MetadataQuery(distance=True),
                        return_properties=["title", "file_path", "node_type", "tags"]
                    )
                if not page.objects:
                    break
                nodes.extend(page.objects)
                if len(page.objects) < PAGE_SIZE:
                    break
                cursor = page.objects[-1].uuid

            print(f"📊 Found {len(nodes)} nodes to analyze\n")

            duplicates = []
            checked_pairs = set()

            for i, node in enumerate(nodes):
                if (i + 1) % 10 == 0:
                    print(f"   Progress: {i+1}/{len(nodes)} nodes checked")

                # Get node metadata
                node_title = node.properties.get("title", "Unknown")
                node_path = node.properties.get("file_path", "")
                node_uuid = str(node.uuid)

                # Find semantically similar nodes
                similar = self.collection.query.near_object(
                    near_object=node_uuid,
                    limit=5,
                    return_metadata=MetadataQuery(distance=True),
                    return_properties=["title", "file_path", "node_type"]
                )

                for similar_node in similar.objects:
                    similar_uuid = str(similar_node.uuid)

                    # Skip self
                    if similar_uuid == node_uuid:
                        continue

                    # Skip if pair already checked
                    pair_key = tuple(sorted([node_uuid, similar_uuid]))
                    if pair_key in checked_pairs:
                        continue

                    checked_pairs.add(pair_key)

                    # Calculate similarities
                    distance = similar_node.metadata.distance
                    semantic_similarity = 1.0 - distance  # Convert distance to similarity

                    similar_title = similar_node.properties.get("title", "Unknown")
                    title_sim = title_similarity(node_title, similar_title)

                    # Check if this is a potential duplicate
                    if semantic_similarity >= self.threshold or title_sim >= self.threshold:
                        duplicates.append({
                            "node1": {
                                "uuid": node_uuid,
                                "title": node_title,
                                "path": node_path
                            },
                            "node2": {
                                "uuid": similar_uuid,
                                "title": similar_title,
                                "path": similar_node.properties.get("file_path", "")
                            },
                            "semantic_similarity": semantic_similarity,
                            "title_similarity": title_sim,
                            "confidence": max(semantic_similarity, title_sim)
                        })

            print(f"\n✅ Analysis complete\n")
            return duplicates

        except Exception as e:
            print(f"❌ Error during duplicate detection: {e}")
            import traceback
            traceback.print_exc()
            return []

    def generate_report(self, duplicates: List[Dict], output_file: Path):
        """Generate markdown report of duplicates"""
        if not duplicates:
            print("✅ No duplicates found!")
            return

        # Sort by confidence (highest first)
        duplicates.sort(key=lambda x: x["confidence"], reverse=True)

        report_lines = [
            "# Knowledge Graph Duplicate Detection Report",
            f"\n**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Threshold**: {self.threshold}",
            f"**Total Duplicates Found**: {len(duplicates)}\n",
            "---\n",
            "## Potential Duplicates\n"
        ]

        for i, dup in enumerate(duplicates, 1):
            confidence_pct = dup["confidence"] * 100
            semantic_pct = dup["semantic_similarity"] * 100
            title_pct = dup["title_similarity"] * 100

            report_lines.extend([
                f"### {i}. Duplicate Pair (Confidence: {confidence_pct:.1f}%)\n",
                f"**Node 1**: [{dup['node1']['title']}]({dup['node1']['path']})",
                f"**Node 2**: [{dup['node2']['title']}]({dup['node2']['path']})\n",
                f"**Similarities**:",
                f"- Semantic: {semantic_pct:.1f}%",
                f"- Title: {title_pct:.1f}%\n",
                f"**Recommendation**:",
                ""
            ])

            # Provide recommendation based on confidence
            if dup["confidence"] >= 0.98:
                report_lines.append("⚠️ **HIGH CONFIDENCE DUPLICATE** - Likely the same content, consider merging")
            elif dup["confidence"] >= 0.95:
                report_lines.append("⚠️ **PROBABLE DUPLICATE** - Review content, may need merging")
            elif dup["confidence"] >= 0.90:
                report_lines.append("ℹ️ **SIMILAR CONTENT** - May be related but distinct, review for consolidation")
            else:
                report_lines.append("ℹ️ **RELATED CONTENT** - Likely distinct but related topics")

            report_lines.append("\n---\n")

        # Add action items
        report_lines.extend([
            "\n## Action Items\n",
            "1. Review high-confidence duplicates (≥98%) first",
            "2. For each duplicate pair:",
            "   - Compare content in detail",
            "   - Decide: Merge, keep both, or update links",
            "3. If merging:",
            "   - Consolidate content in newer/better file",
            "   - Add `owl:sameAs` link in frontmatter (RDF pattern)",
            "   - Update all WikiLinks to point to canonical version",
            "   - Archive or delete duplicate",
            "4. Re-run detection after cleanup\n"
        ])

        # Write report
        output_file.write_text('\n'.join(report_lines))
        print(f"📝 Report written to: {output_file}")


def main():
    """Main entry point"""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Detect duplicate knowledge graph nodes")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD,
                       help="Similarity threshold (0.0-1.0)")
    parser.add_argument("--output", type=str, default=".claude/logs/duplicates_report.md",
                       help="Output report file")
    # v0.2.20 (Stream 2 follow-up, 2026-05-19): --json emits the duplicate
    # pairs as a single JSON document on stdout INSTEAD of the markdown
    # report. Used by the launcher's orchestrator_core::kg_check_duplicates
    # Tauri command (parses pairs into a modal). Stays mutually-exclusive
    # with the human-readable summary print to keep stdout machine-parsable.
    parser.add_argument("--json", action="store_true",
                       help="Emit duplicate pairs as JSON on stdout (machine-readable)")

    args = parser.parse_args()

    detector = DuplicateDetector(similarity_threshold=args.threshold)

    try:
        duplicates = detector.find_duplicates()

        if args.json:
            # Machine-readable mode: ONLY the JSON document on stdout.
            # Threshold is echoed back so the caller can verify the run
            # used the requested value (defense-in-depth for argument
            # passing through bash wrappers).
            payload = {
                "threshold": args.threshold,
                "count": len(duplicates),
                "pairs": duplicates,
            }
            json.dump(payload, sys.stdout)
            sys.stdout.write("\n")
            return

        if duplicates:
            output_path = PROJECT_ROOT / args.output
            output_path.parent.mkdir(parents=True, exist_ok=True)
            detector.generate_report(duplicates, output_path)

            print(f"\n📊 Summary:")
            print(f"   Total duplicates: {len(duplicates)}")
            high_confidence = sum(1 for d in duplicates if d["confidence"] >= 0.98)
            print(f"   High confidence (≥98%): {high_confidence}")
            print(f"   Probable (≥95%): {sum(1 for d in duplicates if 0.95 <= d['confidence'] < 0.98)}")
            print(f"\n💡 Next: Review {output_path}")
        else:
            print("✅ No duplicates detected - knowledge graph is clean!")

    finally:
        detector.close()


if __name__ == "__main__":
    main()
