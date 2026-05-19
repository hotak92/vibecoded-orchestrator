---
title: Wear-the-Hat Pattern (Role and Discipline Impersonation)
type: pattern
tags: [pattern, agent-design, meta-agents, prompt-engineering, AI-assistance, consulting, scientific-computing, mid-level-architecture, best-practices]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Wear-the-Hat Pattern (Role and Discipline Impersonation)

## Overview

A parametric pattern: one agent specification adopts a named perspective per invocation — a role (junior dev, account manager, SRE), a discipline (molecular biology, condensed-matter physics, climate science), or any named hat with characteristic vocabulary, knowledge bounds, default methods, and accountability surface. The user supplies a **hat key**; the agent activates the corresponding defaults for the duration of one consultation, then takes the hat off.

The pattern collapses maintenance burden from O(hats) to O(1) — adding a new role or discipline is a one-line table edit — and produces outputs that are reviewable from a different seat than the one the reviewer normally occupies.

## Why It's Useful

**Coverage of perspectives**: A CTO who drafts a document themselves reviews it like a CTO. The same document drafted as a junior engineer would write it, then reviewed by the CTO, catches different problems — what a junior would miss (architectural context, decision history) AND what a CTO would miss when familiar with the system. Different seats, different bugs found.

**Cross-disciplinary translation**: Most real research is interdisciplinary. A biologist needs stats consultation framed in biology terms, then chemistry consultation framed in chemistry terms, then physics-informed-ML consultation framed for both. One agent with a discipline parameter handles all three; maintaining 30 separate agent files for 30 specialties is unmaintainable.

**Coverage during absence**: When an account manager is on holiday and a client email needs sending in their voice, or when a senior engineer's PR description is needed at 2 AM, role-shaped drafts move forward in the reviewer's normal review path rather than producing role-agnostic outputs that the reviewer must then transpose.

## When the Pattern Applies

- Many narrow specialties share a common workflow (consult → recommend → output) but differ in vocabulary, methods, conventions
- Producing a draft as a specific role so the reviewer can review from a different seat
- Walking through what a role / discipline would and wouldn't know, ask, push back on
- Cross-disciplinary translation between two named hats in one session
- Onboarding simulation — what does a new hire see on day one
- Hats are roughly enumerable — a known set with a long tail handled by user-supplied free-form characterisation

## When It Does Not Apply

- Variation between "instances" is too deep — e.g. coding assistants for Python vs Rust differ in reasoning style, not just defaults
- Only 2-3 variants — separate specifications are cheaper than a parameterised one
- The hat parameter would compose with too many other axes (hat × sub-specialty × seniority × audience) — at that point you need actual composition, not parameter substitution
- The work must be signed in the named person's actual name (see safety boundary below)

## The Hat-Key Library Pattern

A table maps shorthand keys to hat configurations. Two example libraries below cover most of the design space.

### Library A — Consulting / engineering archetypes

| Key | Hat | Knows | Doesn't know | Voice |
|---|---|---|---|---|
| `junior-dev` | Backend/full-stack engineer, 0-2 yrs | Language fundamentals, surface stack, how to read docs | Deep architecture context, prior-decision history, production failure modes | Descriptive, asks questions, lists assumptions |
| `senior-dev` | Engineer with 6-10 yrs, lead-level | Architecture decisions in context, multiple production incidents | Marketing positioning, customer politics | Terse, opinionated, references prior art |
| `pm` | Project manager / delivery lead | Client expectations, capacity, contract terms | Technical implementation details | Dependency-focused, dates and owners on everything |
| `account-manager` | Commercial owner of relationship | Client politics, what was sold, renewal date | Engineering tradeoffs at depth | Client-empathetic; internally honest about risk |
| `ops-engineer` | Platform / SRE / DevOps | Production topology, on-call pain, cloud bill drivers | Frontend UX subtleties | Paranoid about reliability, quantifies impact |
| `designer` | Product designer / UX | User flows, accessibility, how product is actually used | Backend/DB details | User-first, references behaviour, asks about edges |

For each role, the spec should also state: what they push back on (the scope question they would raise), and what they do not proactively do (their scope boundary).

### Library B — Scientific disciplines

| Key | Hat | Default methods | Default tools |
|---|---|---|---|
| `molecular-biology` | Wet-lab molecular biologist | Western, qPCR, cloning, CRISPR, IF | Snapgene, ImageJ, BioRender |
| `single-cell` | Single-cell biologist | scRNA/ATAC-seq, multi-omics, trajectory | scanpy, scvi-tools, Seurat |
| `genomics` | Bioinformatician | DESeq2/edgeR/limma, variant calling, GWAS | nf-core, bcftools, plink2 |
| `condensed-matter-physics` | Condensed-matter physicist | Transport, optics, scattering, DFT | VASP, Quantum ESPRESSO |
| `astrophysics` | Astrophysicist | Spectra, photometry, light curves, N-body | astropy, lightkurve, REBOUND |
| `climate-science` | Climate scientist | GCM output, observational datasets, attribution | xarray, ESMValTool |
| `computational-chemistry` | Computational chemist | DFT, MD, QM/MM, free energy | Gaussian/ORCA, GROMACS |
| `materials-science` | Materials scientist | DFT, MD, synthesis, characterisation | VASP, LAMMPS, pymatgen |
| `ecology` | Ecologist | Survey design, occupancy, community ecology | unmarked, vegan, spaMM |
| `statistics` | Statistician | See [[Hypothesis Testing Decision Tree]] | R, Stan, brms |

For unknown keys, the agent asks for a 2-3 sentence characterisation in the spawn prompt and operates on that.

## Behavioural Defaults Per Hat

When a hat is selected, the agent adjusts six axes:

1. **Terminology** — use the field's vocabulary, not generic terms. "Differential expression" not "data analysis"; "Resolution" means atomic in structural biology, angular in astronomy, spectral in spectroscopy.
2. **Default methods** — pick the field-standard method, note alternatives. Genomics defaults to DESeq2/edgeR; condensed-matter defaults to DFT; structural biology defaults to AF3 + cryoSPARC.
3. **Default software stack** — `Phenix` in crystallography, `cryoSPARC` for cryo-EM, `RAxML` for phylogenetics, `LAMMPS` for materials MD. Use the discipline-standard tool.
4. **Statistical / engineering conventions** — disciplines and roles differ. Genomics tolerates FDR with BH; clinical trials demand FWER control. Backend devs accept eventual consistency; financial-ops devs do not.
5. **Reporting standards** — MIQE (qPCR), MIBBI (omics), ARRIVE (preclinical animals), CONSORT (clinical trials), STROBE (observational), BIDS (neuroimaging), CF (climate netCDF). For role hats: PR description format, runbook structure, incident-report skeleton — match the in-team norm.
6. **Repositories, identifiers, output destinations** — point at the right deposit location or output channel (SRA for genomics raw, GEO for processed, PDB for structures; for roles, the right JIRA project, Slack channel, status page).

## The Knowledge-Bounds Discipline

Each hat has **explicit ignorance**, not just expertise. A `junior-dev` impersonation that casually references the company's 2-year-old architecture decision breaks the simulation — the junior wouldn't have that context. The value of the pattern depends on staying inside the bounds: what the hat knows, what they don't, what they'd ask about.

If asked mid-task "but what would the CTO do here?" while wearing the junior-dev hat, the right answer is in archetype voice ("I'd escalate to the CTO; here's what I'd put in the message") rather than breaking character into general CTO reasoning. Breaking character once contaminates the entire output.

## Cross-Disciplinary / Cross-Role Mode

The pattern's strongest use is across two hats in sequence:

1. Translate the user's framing into hat A's vocabulary; recommend hat A's methods.
2. Translate into hat B's vocabulary; recommend hat B's methods.
3. Reconcile when the recommendations differ.
4. Identify the rate-limiting expertise gap — "you need a [field/role] collaborator for [step] because [reason]".

Useful in research (chemist analysing biological data), in engineering (designer reviewing what a junior dev built), and in consulting (account manager reviewing what the technical lead drafted).

## Calibration Discipline (Honesty About Confidence)

A wear-the-hat agent must be honest about confidence per hat. State explicitly:

- **HIGH** for canonical / textbook material.
- **MEDIUM** for active research areas — common practice but the frontier is shifting.
- **LOW** — verify with a field expert / actual role-holder. Areas where the agent has been observed to drift.

The honest answer "this is at the edge of my reliable knowledge — verify with a [discipline] colleague" is the discipline. Without it, the pattern degrades into confident-sounding hallucination.

## Free-Form Hats

When a custom hat is specified (e.g. "junior frontend dev who only knows React, two months in, anxious about asking too many questions"):

1. Restate the hat in 2-3 sentences capturing voice + knowledge bounds + behavioural pattern.
2. Ask for confirmation.
3. Operate from the restated description.

The restatement step verifies the bounds are understood — and it's a reusable doc the user can paste into a future invocation.

## Safety Boundary — Strict (the identity-deception line)

The pattern's misuse is identity deception. The boundary is hard:

- **NEVER** produce work that will be signed in the named human's actual name without disclosing AI involvement to that human.
- **NEVER** for customer communication the customer will believe is from the named human (deception risk).
- **NEVER** as a substitute for a 1:1, a performance review, or any decision that affects the named human's career.
- **NEVER** for anything that requires real-time human judgement (legal review, hiring decision, escalation negotiation).

The pattern produces drafts for the named person's review path, not signed deliverables in their place.

### The non-negotiable impersonation footer

Every output produced under a *named-person* impersonation (as opposed to a generic role or discipline) ends with:

```
---
Impersonation note: This was produced by an AI agent acting as
<archetype>. Review accordingly. Do not present to clients or to
the actual named person without disclosing AI involvement.
```

The footer is the safety boundary. It does not stop someone from removing it deliberately, but it converts removal from "happens by inattention" to "happens by intent" — which is the right place for that decision.

### Refuse-and-redirect cases

- **Identity-deception requests** — "write this email as Maria so she doesn't know I'm sending it" → refuse, propose alternative ("I can draft what you'd want her to send; you discuss with her").
- **Self-review impersonation** — using the pattern to write your own performance self-review in someone else's voice → refuse.
- **Customer-apology-requiring-real-authority** — refuse; route to the human with the authority.

## Implementation Notes for Agent Authors

- **Single specification file** with a `hat library` section and `behavioural defaults per hat` section. User spawns with `hat=<key>` in the prompt; agent reads the table.
- **Model tier**: a mid-tier model often suffices — breadth across hats matters more than depth in one. Escalate when a consultation requires genuinely deep multi-step reasoning specific to one hat.
- **KG accumulation pays**: after a non-trivial consultation, write a KG node capturing the gotcha or pattern (`knowledge/concepts/<discipline>-<topic>.md` or `<role>-<topic>.md`). Future invocations benefit; the agent becomes more knowledgeable in hats used most.
- **Sub-specialty refinement**: when a key is ambiguous, ask for sub-specialty before proceeding. "Genomics" + "scRNA-seq" beats "genomics" alone; "senior-dev" + "backend/Python/data" beats "senior-dev" alone.
- **Resist convention-vs-best-practice tension**. Field conventions sometimes lag general best practice. Present both, note the trade-off, let the user decide.

## Anti-Patterns

- **Pretending deep expertise the agent doesn't have**. Some niches are beyond reliable AI calibration. Say so; don't generate confident-sounding nonsense.
- **Generic answers in disciplinary clothing**. If the recommendation would apply to any field/role, the hat isn't earning its keep. Specifics — concrete software, repository names, field-specific gotchas — are the value.
- **Wrong hat on confusing input**. Always confirm the hat at the start of the consultation; wrong hat = wrong advice.
- **No "limits of competence" statement**. Without explicit acknowledgement of what the hat does and doesn't reliably know, users over-trust the output.
- **Producing a `senior-dev` output that's actually CTO-level** (loss of differentiation — reviewer can't catch what a senior would).
- **Omitting the impersonation footer** for named-person hats.
- **Multiple hats in one output** — run separate invocations instead.
- **Breaking character mid-task** to answer "what would X do" outside hat voice.

## Related Patterns

- **Persona / role prompts** — wear-the-hat is a structured, persistent instance of the persona-prompt technique with explicit defaults rather than free-form vibe.
- **Tool-use agents** — orthogonal: a wear-the-hat agent uses tools, but the hat parameter governs *which defaults* the tool use applies, not which tools exist.
- **Multi-agent orchestration** — wear-the-hat agents can be spawned in parallel from a planner ("get the climate hat AND the statistics hat to review this analysis"); each hat is independent.

## References

- MIBBI consortium (Taylor et al. 2008, *Nature Biotechnology*) — standards for reporting molecular biology.
- Percie du Sert et al. (2020): ARRIVE 2.0 guidelines. *PLOS Biology* 18(7):e3000410.
- The wear-the-hat pattern generalises the "parameterised agent" approach in modern agent SDKs.

[[relatedTo::Contractor vs Employee Management]]
[[relatedTo::Client Engagement Lifecycle]]
[[relatedTo::Consulting Deliverable Skepticism]]
[[relatedTo::Prompt Engineering Patterns - 2026 Research]]
[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Scientific Python Stack 2026]]
