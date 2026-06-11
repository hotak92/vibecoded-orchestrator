#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Scaffold a saved Claude Code workflow into `.claude/workflows/<name>.mjs`.

Saved workflows (Claude Code v2.1.154+) become `/<name>` slash commands and
run deterministic multi-agent orchestration. This generator ships four stock
patterns plus a generic scaffold; the emitted file is meant to be EDITED —
each template marks the prompts to customize with `EDIT ME`.

Usage:
    generate-workflow <name> [--template <stock>] [--force] [--description TEXT]
    generate-workflow --list-templates

When <name> equals a stock template name and --template is omitted, that
stock pattern is used; otherwise the generic scaffold is emitted.

Stock templates: dependency-update-check, code-review-loop, release-prep,
weekly-housekeeping.

Exit codes: 0 ok; 1 validation error; 2 target exists (use --force).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# NOTE on format: `export const meta` + top-level script body is the form
# verified against the live Workflow runtime (2026-06-11). meta.keywords is
# tolerated by the runtime and consumed by the keyword-suggest hook.

_GENERIC = """export const meta = {{
  name: '{name}',
  description: '{description}',
  whenToUse: 'EDIT ME: one line on when this workflow applies.',
  keywords: [{keywords}],
  phases: [{{ title: 'Work' }}, {{ title: 'Synthesize' }}],
}}

phase('Work')
// EDIT ME: replace with your real fan-out. pipeline() for independent
// per-item stages (default), parallel() only when you need ALL results
// together before continuing.
const items = ['EDIT ME: item-1', 'EDIT ME: item-2']
const results = await parallel(items.map((it) => () =>
  agent(`EDIT ME: do the work for ${{it}}. Return raw findings, not prose.`,
        {{ label: `work:${{it}}`, phase: 'Work' }})
))

phase('Synthesize')
const report = await agent(
  'EDIT ME: merge these results into a report: ' + JSON.stringify(results.filter(Boolean)),
  {{ label: 'synthesize' }}
)
return {{ report }}
"""

_DEPENDENCY_UPDATE_CHECK = """export const meta = {{
  name: '{name}',
  description: 'Check project dependencies for available updates, breaking changes, and security advisories',
  whenToUse: 'Periodically, or before a release — surfaces outdated/vulnerable dependencies with an upgrade plan.',
  keywords: ['dependency update', 'outdated dependencies', 'security advisories', 'bump dependencies', 'check for updates'],
  phases: [{{ title: 'Inventory' }}, {{ title: 'Assess' }}, {{ title: 'Report' }}],
}}

phase('Inventory')
const inventory = await agent(
  'Inventory this project\\'s dependency manifests (package.json, Cargo.toml, pyproject.toml, ' +
  'requirements.txt, go.mod — whichever exist). For each manifest run the native outdated check ' +
  '(npm outdated, cargo update --dry-run, pip list --outdated) via Bash and collect: package, ' +
  'current version, latest version. Return raw structured data.',
  {{ label: 'inventory' }}
)

phase('Assess')
const assessment = await agent(
  'For each outdated dependency below, assess: is the jump major/minor/patch, are there known ' +
  'breaking changes (check the package changelog if quick), is there a security advisory. ' +
  'Classify each as safe-bump / needs-review / breaking. Data: ' + JSON.stringify(inventory),
  {{ label: 'assess' }}
)

phase('Report')
const report = await agent(
  'Write a dependency-update report to .claude/context/audits/dependency-update-<YYYY-MM-DD>.md ' +
  '(derive the date with `date +%F` via Bash; create the dir if needed). Group by classification, ' +
  'lead with security advisories, end with a copy-pasteable upgrade command list for the safe bumps. ' +
  'Assessment: ' + JSON.stringify(assessment) + ' Return the report path and a 3-line summary.',
  {{ label: 'report' }}
)
return {{ report }}
"""

_CODE_REVIEW_LOOP = """export const meta = {{
  name: '{name}',
  description: 'Multi-dimension review of the current uncommitted/branch diff with adversarial verification',
  whenToUse: 'Before merging a branch or committing a large change set.',
  keywords: ['review my changes', 'review this branch', 'review the diff', 'pre-merge review'],
  phases: [{{ title: 'Review' }}, {{ title: 'Verify' }}],
}}

const FINDINGS = {{
  type: 'object',
  properties: {{
    findings: {{ type: 'array', items: {{ type: 'object', properties: {{
      title: {{ type: 'string' }}, file: {{ type: 'string' }},
      severity: {{ type: 'string', enum: ['P0', 'P1', 'P2'] }},
      detail: {{ type: 'string' }},
    }}, required: ['title', 'file', 'severity', 'detail'] }} }},
  }},
  required: ['findings'],
}}
const VERDICT = {{
  type: 'object',
  properties: {{ isReal: {{ type: 'boolean' }}, why: {{ type: 'string' }} }},
  required: ['isReal', 'why'],
}}

const DIMENSIONS = [
  {{ key: 'bugs', prompt: 'correctness bugs, broken edge cases, regressions' }},
  {{ key: 'security', prompt: 'injection, secrets in code, unsafe input handling' }},
  {{ key: 'consistency', prompt: 'style drift, duplicated logic, dead code introduced' }},
]

// Pipeline: each dimension's findings go to verification as soon as that
// dimension finishes — no barrier.
const results = await pipeline(
  DIMENSIONS,
  (d) => agent(
    `Review the current git diff (run \\`git diff HEAD\\` and \\`git diff --stat HEAD\\` via Bash) ` +
    `for ${{d.prompt}} ONLY. Read surrounding file context before reporting. Verified findings only.`,
    {{ label: `review:${{d.key}}`, phase: 'Review', schema: FINDINGS }}
  ),
  (review) => parallel((review?.findings ?? []).map((f) => () =>
    agent(
      `Adversarially verify this review finding — try to REFUTE it by reading the actual code. ` +
      `Default to isReal=false when uncertain. Finding: ${{JSON.stringify(f)}}`,
      {{ label: `verify:${{f.file}}`, phase: 'Verify', schema: VERDICT }}
    ).then((v) => ({{ ...f, verdict: v }}))
  ))
)

const confirmed = results.filter(Boolean).flat().filter((f) => f?.verdict?.isReal)
log(`${{confirmed.length}} confirmed findings`)
return {{ confirmed }}
"""

_RELEASE_PREP = """export const meta = {{
  name: '{name}',
  description: 'Pre-release audit: changelog completeness, version consistency, CI state, leftover TODO/FIXME markers',
  whenToUse: 'Before tagging a release.',
  keywords: ['prep the release', 'release checklist', 'pre-release audit', 'ready to tag', 'release prep'],
  phases: [{{ title: 'Audit' }}, {{ title: 'Report' }}],
}}

const CHECKS = [
  'CHANGELOG: is the [Unreleased] section empty / moved into a version block? Does the latest block match the version files?',
  'VERSIONS: find every version declaration (package.json, Cargo.toml, pyproject.toml, VERSION) and verify they agree.',
  'MARKERS: grep the diff since the last tag for TODO(next-release) / FIXME / XXX markers added but not closed.',
  'CI: if the gh CLI is available, list recent workflow runs and flag any red conclusion on the release branch.',
]

phase('Audit')
const results = await parallel(CHECKS.map((c, i) => () =>
  agent(`Run this pre-release check against the repo and return raw findings (PASS/FAIL + evidence): ${{c}}`,
        {{ label: `check:${{i}}`, phase: 'Audit' }})
))

phase('Report')
const report = await agent(
  'Merge these pre-release check results into a go/no-go report at ' +
  '.claude/context/audits/release-prep-<YYYY-MM-DD>.md (date via `date +%F`, create dir if needed). ' +
  'Lead with the verdict, then per-check detail. Results: ' + JSON.stringify(results.filter(Boolean)),
  {{ label: 'report' }}
)
return {{ report }}
"""

_WEEKLY_HOUSEKEEPING = """export const meta = {{
  name: '{name}',
  description: 'Weekly hygiene sweep: stale docs, oversized context files, knowledge-graph drift, orphaned TODOs',
  whenToUse: 'Weekly, or when the project starts feeling cluttered.',
  keywords: ['housekeeping pass', 'hygiene sweep', 'clean up the project', 'stale docs check', 'weekly maintenance'],
  phases: [{{ title: 'Sweep' }}, {{ title: 'Report' }}],
}}

const SWEEPS = [
  {{ key: 'docs', prompt: 'Find stale documentation: docs/ files not modified in 90+ days that reference code which has since changed, duplicate docs covering the same topic, and root-level files that belong in docs/.' }},
  {{ key: 'context', prompt: 'Check .claude/ hygiene: CONTEXT_STATE.md over 500 lines, stale plans in .claude/context/plans/ that look completed but not archived, oversized state files.' }},
  {{ key: 'kg', prompt: 'If a knowledge/ dir exists: find nodes whose frontmatter status is active but content references long-shipped versions, and wikilinks pointing at files that no longer exist.' }},
  {{ key: 'todos', prompt: 'Grep the codebase for TODO/FIXME/XXX markers older than the last release tag (use git blame on a sample) and list the 10 stalest.' }},
]

phase('Sweep')
const results = await parallel(SWEEPS.map((s) => () =>
  agent(s.prompt + ' Return raw findings with file paths.', {{ label: `sweep:${{s.key}}`, phase: 'Sweep' }})
))

phase('Report')
const report = await agent(
  'Merge these housekeeping findings into a prioritized cleanup checklist at ' +
  '.claude/context/audits/housekeeping-<YYYY-MM-DD>.md (date via `date +%F`, create dir if needed). ' +
  'Findings: ' + JSON.stringify(results.filter(Boolean)) + ' Return the path + the top 5 items.',
  {{ label: 'report' }}
)
return {{ report }}
"""

STOCK_TEMPLATES: dict[str, str] = {
    "dependency-update-check": _DEPENDENCY_UPDATE_CHECK,
    "code-review-loop": _CODE_REVIEW_LOOP,
    "release-prep": _RELEASE_PREP,
    "weekly-housekeeping": _WEEKLY_HOUSEKEEPING,
}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    cur = Path.cwd()
    for cand in (cur, *cur.parents):
        if (cand / ".claude").is_dir():
            return cand
    return cur


def render(name: str, template_key: str | None, description: str | None) -> str:
    if template_key is None and name in STOCK_TEMPLATES:
        template_key = name
    if template_key is not None:
        if template_key not in STOCK_TEMPLATES:
            raise ValueError(
                f"unknown template '{template_key}' — available: {', '.join(sorted(STOCK_TEMPLATES))}"
            )
        return STOCK_TEMPLATES[template_key].format(name=name)
    desc = (description or "EDIT ME: one-line description (shown in /workflows and used for routing)").replace("'", "\\'")
    keywords = ", ".join(f"'{k}'" for k in (f"run {name}", name.replace("-", " ")))
    return _GENERIC.format(name=name, description=desc, keywords=keywords)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="generate-workflow",
        description="Scaffold a saved Claude Code workflow (.claude/workflows/<name>.mjs).",
    )
    p.add_argument("name", nargs="?", help="workflow name (becomes the /<name> slash command)")
    p.add_argument("--template", help="stock pattern to use (see --list-templates)")
    p.add_argument("--description", help="description for the generic scaffold")
    p.add_argument("--force", action="store_true", help="overwrite an existing workflow file")
    p.add_argument("--list-templates", action="store_true")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_templates:
        for k in sorted(STOCK_TEMPLATES):
            print(k)
        return 0
    if not args.name:
        p.print_usage(sys.stderr)
        return 1
    if not _NAME_RE.match(args.name):
        print(f"error: invalid name '{args.name}' (lowercase letters, digits, hyphens; 2-64 chars)",
              file=sys.stderr)
        return 1

    try:
        content = render(args.name, args.template, args.description)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    target = project_root() / ".claude" / "workflows" / f"{args.name}.mjs"
    if target.exists() and not args.force:
        print(f"refused: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    used = args.template or (args.name if args.name in STOCK_TEMPLATES else "generic scaffold")
    print(f"wrote {target} ({used})")
    print(f"invoke as /{args.name} once your Claude Code session reloads; "
          f"search the file for EDIT ME markers first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
