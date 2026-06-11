#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Detect which saved Claude Code workflows this project would benefit from.

Heuristic v1: inspects the project for language/tooling/infra signals and
matches them against the stock workflow recipes that `generate-workflow`
can scaffold. Workflows already present under `.claude/workflows/` are
excluded from the recommendations.

Usage:
    detect-workflow-needs [--json] [--project-root PATH]

Output (text mode): one recommendation per line —
    <workflow-name>  <reason>
followed by the generate command to scaffold each. `--json` emits
{"recommendations": [{name, reason, generate_cmd}], "signals": {...}}.

Exit codes: 0 always (advisory tool; no recommendations is not an error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CODE_EXTENSIONS = {
    ".py": "python", ".rs": "rust", ".ts": "typescript", ".js": "javascript",
    ".svelte": "svelte", ".go": "go", ".java": "java", ".rb": "ruby",
    ".c": "c", ".cpp": "cpp", ".cs": "csharp",
}

DEPENDENCY_MANIFESTS = (
    "package.json", "Cargo.toml", "pyproject.toml", "requirements.txt",
    "go.mod", "Gemfile", "pom.xml",
)

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist",
              "build", "__pycache__", ".claude"}


def project_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    cur = Path.cwd()
    for cand in (cur, *cur.parents):
        if (cand / ".claude").is_dir():
            return cand
    return cur


def gather_signals(root: Path) -> dict:
    languages: dict[str, int] = {}
    code_files = 0
    # Bounded walk: don't recurse into vendored/build dirs; cap visits so a
    # giant monorepo doesn't make a "quick detection" take minutes.
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            visited += 1
            ext = Path(fn).suffix.lower()
            lang = CODE_EXTENSIONS.get(ext)
            if lang:
                languages[lang] = languages.get(lang, 0) + 1
                code_files += 1
        if visited > 20000:
            break

    manifests = [m for m in DEPENDENCY_MANIFESTS if (root / m).is_file()]
    existing_workflows = []
    wf_dir = root / ".claude" / "workflows"
    if wf_dir.is_dir():
        existing_workflows = sorted(
            p.stem for pattern in ("*.mjs", "*.js") for p in wf_dir.glob(pattern)
        )

    return {
        "languages": languages,
        "code_files": code_files,
        "dependency_manifests": manifests,
        "is_git_repo": (root / ".git").exists(),
        "has_changelog": (root / "CHANGELOG.md").is_file(),
        "has_version_file": any((root / v).is_file() for v in ("VERSION", "version.txt")),
        "has_tests_dir": any((root / t).is_dir() for t in ("tests", "test", "spec")),
        "has_knowledge_dir": (root / "knowledge").is_dir(),
        "docs_file_count": (
            sum(1 for _ in (root / "docs").rglob("*.md")) if (root / "docs").is_dir() else 0
        ),
        "existing_workflows": existing_workflows,
    }


def recommend(signals: dict) -> list[dict]:
    recs: list[dict] = []

    def add(name: str, reason: str) -> None:
        if name in signals["existing_workflows"]:
            return
        recs.append({
            "name": name,
            "reason": reason,
            "generate_cmd": f".claude/scripts/generate-workflow {name}",
        })

    if signals["dependency_manifests"]:
        add(
            "dependency-update-check",
            f"dependency manifests present ({', '.join(signals['dependency_manifests'])})",
        )
    if signals["is_git_repo"] and signals["code_files"] >= 10:
        add(
            "code-review-loop",
            f"git repo with {signals['code_files']} code files "
            f"({', '.join(sorted(signals['languages'], key=signals['languages'].get, reverse=True)[:3])})",
        )
    if signals["has_changelog"] or signals["has_version_file"]:
        add(
            "release-prep",
            "release artifacts present ("
            + ", ".join(
                x for x, ok in (
                    ("CHANGELOG.md", signals["has_changelog"]),
                    ("VERSION", signals["has_version_file"]),
                ) if ok
            )
            + ")",
        )
    if signals["has_knowledge_dir"] or signals["docs_file_count"] >= 10:
        add(
            "weekly-housekeeping",
            f"knowledge/docs corpus present (knowledge dir: {signals['has_knowledge_dir']}, "
            f"docs files: {signals['docs_file_count']})",
        )
    return recs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="detect-workflow-needs",
        description="Recommend saved workflows for this project (heuristic v1).",
    )
    p.add_argument("--json", action="store_true", help="emit structured JSON")
    p.add_argument("--project-root", help="override project root detection")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    root = project_root(args.project_root)
    signals = gather_signals(root)
    recs = recommend(signals)

    if args.json:
        print(json.dumps({"recommendations": recs, "signals": signals}, indent=2))
        return 0

    if not recs:
        print("no workflow recommendations — either the stock recipes don't match "
              "this project's signals, or the workflows already exist in .claude/workflows/")
        return 0
    print(f"recommended workflows for {root}:")
    for r in recs:
        print(f"  {r['name']:<26} {r['reason']}")
    print("\nscaffold with:")
    for r in recs:
        print(f"  {r['generate_cmd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
