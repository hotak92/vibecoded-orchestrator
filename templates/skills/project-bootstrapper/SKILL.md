---
name: project-bootstrapper
description: Refines the seeded CLAUDE.md / ARCHITECTURE.md / knowledge nodes when a project's add-project bundle needs a human-led second pass — brownfield layouts, polyglot stacks. Use after the bundle ran, not before.
short_desc: refine bootstrap docs for projects with unusual structure
keywords: ["bootstrap project", "refine CLAUDE.md", "second pass", "seed knowledge", "brownfield", "polyglot", "project setup docs", "iterate on CLAUDE.md", "initial architecture doc"]
argument-hint: "[project path]"
model: sonnet
---

# Project Bootstrapper

Refine and iterate on the initial documentation set produced by the
orchestrator's add-project bundle flow. Not the primary bootstrap entry
point — that is the launcher GUI's "+ New/Existing Project" tabs (or
`python -m vco_lib.project_init install-bundle --folder /path` from the
CLI), which materializes `.claude/`, seeds CLAUDE.md, and optionally
analyzes an existing codebase.

## When to use this skill

The canonical flow covers most projects. Use this when:

- The bundle ran successfully but CLAUDE.md / generated docs do not match the
  project's actual structure (unusual layout the heuristic could not analyze).
- The user wants to iterate on initial CLAUDE.md / ARCHITECTURE.md drafts in
  chat before committing them.
- A polyglot codebase needs domain-specific KG seed nodes the bundle template
  cannot anticipate.
- The user wants interactive help applying the FIRST-SESSION scoping nudge
  (disable off-topic agents/skills for this project).

If the orchestrator install has not run yet, point the user at:

```
bash first-install.sh          # one-time orchestrator install (Linux/macOS)
first-install.bat              # Windows
python -m vco_lib.project_init install-bundle --folder /path/to/codebase
```

Or the launcher GUI "+ New Project" / "+ Existing Project" tab (preferred).

## Workflow

1. Confirm the add-project flow already ran — `.claude/` must exist with the
   bundle materialized.
2. Read the freshly-seeded `CLAUDE.md`, `CONTEXT_STATE.md`, and the project's
   existing README / source tree (Glob/Read to observe the real layout).
3. Interview the user on anything the heuristic could not determine: domain,
   primary stack, complexity, special needs (VRAM, content safety,
   multi-language, ...).
4. Refine `CLAUDE.md` and any bundle docs that need project-specific detail.
5. Draft an initial `docs/ARCHITECTURE.md` from the observed structure if the
   project benefits from one.
6. Seed 1–3 knowledge nodes (`knowledge/projects/<project>.md`,
   `knowledge/concepts/*.md`) for project-specific patterns worth recording.
7. For scoping help, walk the agent/skill catalog using the FIRST-SESSION
   block's guidance — disabling happens via the launcher's per-project tabs
   (files move to `.disabled/`, never deleted).

## Cross-references

- Canonical install guide: `docs/GETTING_STARTED.md` (orchestrator clone)
- Add-project CLI: `python -m vco_lib.project_init install-bundle --folder
  /path` (`--help` for flags)
- What the bundle drops: `templates/CLAUDE.md.template` and
  `templates/ORCHESTRATOR-CLAUDE.md.template` (orchestrator clone)
- KG conventions: `knowledge/TAG_HIERARCHY.md`

## Done means

- CLAUDE.md reflects the actual project, not the generic template.
- Seed knowledge nodes exist where they add value.
- The user can start substantive work without re-explaining the project
  every session.
