---
title: Scientific Paper Triage Schema
type: concept
tags: [research-methods, literature-review, systematic-review, paper-triage, critical-appraisal, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Scientific Paper Triage Schema

## Overview

Reading 30+ PDFs cover-to-cover before deciding which deserve careful study wastes weeks. A structured extraction schema turns a folder of papers into a comparable table, lets the researcher rank them for relevance, and surfaces methodological problems that should temper trust in each paper's claims. This node defines the canonical extraction columns, the critical-appraisal checklist, and the cross-paper synthesis steps. Complementary methodological context: [[Common Statistical Pitfalls]], [[Experimental Design Framework]].

## Extraction Schema

One row per paper. Fields below are field-agnostic; add discipline-specific columns (e.g. cell type, tumour model, instrument) as needed.

| Field | Source location | Notes |
|---|---|---|
| `citation` | Title + first authors + year + venue | Format consistently across the set |
| `doi` | First page | Cite by DOI when reporting |
| `study_type` | Methods | RCT, observational cohort, case-control, cross-sectional, in vitro, in silico, simulation, theoretical |
| `domain` | Title + abstract | Bio / physics / chem / etc.; sub-specialty |
| `population_or_system` | Methods | Species, cell line, cohort definition, simulation domain |
| `n_total` | Methods or Table 1 | Total sample size |
| `n_per_group` | Methods | Often hidden — look for the actual experimental-unit count |
| `experimental_unit` | Inferred from design | Animals, patients, cells, runs — **the thing that was randomised** |
| `pseudoreplication_risk` | Inferred | YES if technical replicates are being treated as biological n |
| `controls` | Methods | Positive / negative / sham / matched; specify each |
| `randomisation` | Methods | Method + tool (computer-generated, block, stratified, none, not stated) |
| `blinding` | Methods | Single / double / triple / open-label / not stated |
| `primary_outcome` | Pre-specified in Methods | Should be one; many papers fail to designate |
| `effect_size` | Results | Actual numbers with CI in natural units (not just p-values) |
| `p_values` | Results | Reported p-values; flag if effect-size is absent |
| `multiple_comparison_correction` | Methods + Results | Bonferroni / Holm / BH / FDR / none |
| `statistical_methods` | Methods | Test names + assumptions checked |
| `code_data_availability` | Data availability statement | URL + DOI; flag "available on request" (= rarely shared) |
| `preregistration` | Methods or abstract | OSF / AsPredicted / clinicaltrials.gov ID |
| `figures_supporting_claim` | Results | Figure number(s) per primary claim |
| `claim_summary` | Abstract + conclusion | Plain-language one-line for each primary claim |
| `claim_warrants_skepticism` | Synthesis | Yes/no + reason |
| `recommended_read_priority` | Synthesis | HIGH / MEDIUM / LOW for the user's stated scope |

Always tab-separated for fields that may contain commas. Always state **what is missing**, not what is implied — write `n_per_group: NOT_REPORTED` rather than guessing.

## Critical-Appraisal Checklist

For each paper, run the fixed scepticism checklist and record each hit:

- **Sample size vs claim**. If $n=4$/group and the claim is a population-level inference, flag.
- **Effect size missing**. If only p-values are reported, flag.
- **No multiple-comparison correction** with >5 outcomes. Flag.
- **Pseudoreplication**. Cells × animals × dishes treated as a flat n. Flag. See [[Common Statistical Pitfalls]].
- **Selection bias / survivorship**. Cohort definition that drops dropouts; analysing only the cells that survived imaging. Flag.
- **Figure doesn't show the effect**. Bar charts with $\pm$ SEM (not SD) hiding overlap; scatter plot supporting a "strong correlation" claim despite weak trend; significance asterisks attached to differences that look tiny. Flag.
- **Implausible precision**. Reported $r = 0.99$ in a noisy biological assay; CIs too narrow given n. Flag.
- **No data sharing**. Always flag — affects reproducibility regardless of paper quality.
- **One-tailed test without preregistered direction**. Flag — usually an indication of post-hoc rationalisation.
- **HARKing**. Hypothesis "predicted" in introduction matches a serendipitous result in figures. See [[Common Statistical Pitfalls]] (#10).
- **Reproducibility-crisis flags**: very large effect; single small underpowered study; no preregistration; controversial claim. Flag for replication priority.
- **Retraction status** for older papers — quickly check via Retraction Watch or the publisher.

Each flag gets a **one-sentence specific reason**, not a blanket "skip this paper". Distinguish methodology critique from finding critique: "underpowered" is methodology; "wrong" is a finding only the user (with field knowledge) can judge.

## Cross-Paper Synthesis

After per-paper extraction:

1. **Theme clusters**. Group papers by sub-topic — by semantic similarity, or by `(method × organism × outcome)` triples.
2. **Effect-size synthesis**. For papers measuring similar things, present effect sizes side-by-side. Forest-plot-style table at minimum; an actual forest plot if the set is dense.
3. **Gap analysis**. Cells of the `(method × population × outcome)` matrix that are sparsely populated → under-explored areas, possible high-impact directions.
4. **Contradictions**. Papers reporting opposite-direction effects on the same outcome. Investigate the methodological difference — is the contradiction real, or an artifact of design choices?
5. **Priority reads**. Top 5-10 papers given the user's stated scope, with one-line rationale each.

## Output Files

Recommended structure for a triage deposit:

```
.claude/references/triage/
├── triage_table.tsv          # one row per paper, schema above
├── flagged_claims.md         # per-claim scepticism notes
├── synthesis.md              # cross-paper narrative
└── per_paper/<doi-slug>.md   # optional long-form per-paper notes
```

## Bounds — What This Schema Does NOT Do

- **Does not replace reading the paper**. It produces a structured filter to choose what to read carefully.
- **Does not assess scientific novelty** — requires field expertise the extraction process cannot reliably synthesise from PDFs alone.
- **Does not extract data values from figures**. PDFs with figures-only data require manual extraction (`webplotdigitizer`) or a specialist tool; record "data only in figure, manual extraction needed".
- **Does not assess the validity of underlying claims** — only flags methodological concerns. The user, with field knowledge, judges the science.

## Hard Rules

1. **Cite exact figure / table / page numbers** for every claim. "Fig 2C, p. 5" not "the figure shows...".
2. **Quote effect sizes verbatim from the paper**. Don't paraphrase numbers; copy them. Convert to a common unit for synthesis only if needed, retaining the original.
3. **State what is missing**. If the paper doesn't report n per group, write `n_per_group: NOT_REPORTED`, not a guess.
4. **Flag, don't judge**. "This claim warrants scepticism because [reason]" — let the user decide.
5. **Note retraction status** when triaging older papers. Many "famous" papers have been retracted quietly.

## Reporting Standards for Systematic Reviews

When the triage feeds into a systematic review or meta-analysis, follow **PRISMA 2020** (Page et al. 2021) for the reporting framework: prespecified inclusion/exclusion criteria, search strategy, PRISMA flow diagram, risk-of-bias assessment, and effect-size synthesis with heterogeneity statistics ($I^2$, $\tau^2$).

For preclinical animal-study meta-analyses, follow **CAMARADES** and **SYRCLE's risk-of-bias tool**.

## References

- Page et al. (2021): "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews". *BMJ* 372:n71.
- Bramer et al. (2017): "De-duplication of database search results for systematic reviews in EndNote". *Journal of the Medical Library Association*.
- Errington et al. (2021): "Investigating the replicability of preclinical cancer biology". *eLife* 10:e71601. Context for why critical-pass flags matter.
- Hooijmans et al. (2014): SYRCLE's risk of bias tool for animal studies. *BMC Medical Research Methodology* 14:43.
- Higgins et al. (2019): *Cochrane Handbook for Systematic Reviews of Interventions* (v6). Canonical guide.

[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Experimental Design Framework]]
[[relatedTo::Reproducible Research Workflows]]
