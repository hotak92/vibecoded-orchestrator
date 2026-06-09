---
title: Experimental Design Framework
type: concept
tags: [research-methods, experimental-design, controls, randomisation, blinding, preregistration, statistics, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Experimental Design Framework

## Overview

A defensible experimental design answers nine questions *before* data collection begins. Skipping any one of them is a leading cause of irreproducible findings, retractions, and wasted lab effort. This node lays out the design pipeline as a checklist; specific test selection lives in [[Hypothesis Testing Decision Tree]] and the failure modes it guards against in [[Common Statistical Pitfalls]].

## The Nine-Question Pipeline

### 1. Refine the hypothesis into a falsifiable quantitative claim

A usable hypothesis names **subject, intervention, comparator, outcome, magnitude (SESOI), and timing**. Vague hypotheses ("compound X has anti-tumour effects") cannot be powered, controlled, or analysed. Sharpen to: "In C57BL/6J mice bearing MC38 tumours, oral compound X (50 mg/kg/day × 14 days) reduces tumour volume at day 14 by ≥30% vs vehicle, with α=0.05 and 80% power."

The **SESOI** (smallest effect of interest) is non-negotiable: it is the smallest effect that would change practice. Without it you cannot power the study and cannot interpret a "no significant difference" result.

### 2. Identify the experimental unit

The unit is whatever is *independently randomised*. Get this wrong and the entire analysis is wrong — see the pseudoreplication entry in [[Common Statistical Pitfalls]]. Common examples:

- Mouse experiment with treatment in food/water: **cage** is the unit.
- Mouse experiment with per-mouse injection: **mouse** is the unit.
- Field experiment: usually the **plot**, not the plant.
- Cluster-randomised trial: the **clinic / village**, not the patient.
- Cell experiments: the **biological replicate** (independent culture); technical replicates are averaged or modelled as nested random effects.

State the unit explicitly in the design document and in the Methods section of the paper.

### 3. Enumerate the controls

Controls are domain-specific but always include at least:

| Domain | Standard controls |
|---|---|
| Pharmacology | Vehicle (same volume, route, timing); positive control with known effect |
| Antibody / IF | Isotype-matched IgG; secondary-only; tissue known positive AND negative |
| CRISPR / gene KO | Non-targeting guide; safe-harbour control (e.g. AAVS1); rescue with re-expressed cDNA |
| Field ecology | Adjacent untouched plot; baseline pre-measurement; sham disturbance |
| Behavioural | Sham operation; matched handling time; counter-balanced order |
| Imaging | Unstained / autofluorescence; FMO; calibration beads |
| Observational | Pre-specified covariate adjustment; instrumental variable if available; sensitivity analysis |
| ML benchmark | Held-out test set never touched until final; matched-budget baseline |

Match control n to treatment n; without the right control a positive result is uninterpretable.

### 4. Compute power and sample size

Run an explicit power calculation, not a "30/group should be fine" hand-wave. Inputs: SESOI, expected variability (from pilot or literature), α (default 0.05), power target (default 0.80; 0.90 for high-stakes), test type (two-tailed default). Specific formulas and tools in [[Power Analysis and Sample Size Estimation]].

For preclinical and clinical work, the **ethical minimum** binds: don't expose subjects to risk in a study underpowered to answer the question.

### 5. Specify randomisation

State the method, tool, and any structure:

- **Method**: simple, block, stratified, minimisation, cluster.
- **Tool**: `randomizr` (R), `numpy.random.default_rng().permutation`, REDCap, sealed envelopes.
- **Block size**: 4-6 typical for balanced clinical trials; smaller for rapid recruitment.
- **Stratification variables**: known prognostic factors (age, sex, severity).
- **Allocation concealment**: who knows the assignment, when. Generate the allocation file in advance and commit (encrypted if blinded) to the project repo.

### 6. Specify blinding

| Level | Description | Notes |
|---|---|---|
| Open-label | Everyone knows | High bias risk; only when no alternative |
| Single-blind | Subject blinded | Reduces subject bias |
| Double-blind | Subject + experimenter blinded | Strongest, sometimes infeasible (visible side effects) |
| Triple-blind | + blinded outcome assessment | Best for subjective outcomes |
| Blinded analysis | Statistician analyses arms coded A/B until lock | Cheap; powerful protection against analysis bias |

Even when treatment blinding is impossible (some surgical procedures, ecology), **blinded outcome assessment** is almost always achievable and should be specified.

### 7. Confounders — list and handle each

Build an explicit list:

- Biological: age, sex, weight, batch / cage / day-of-week, time-of-day (circadian).
- Operator: who pipetted, who scored, which microscope.
- Reagent batch: lot numbers of cell lines, antibodies, media.
- Environmental: temperature, humidity, incubator position.
- Observational only: SES, comorbidities, medications, healthcare access.

For each, choose one strategy:

- **Eliminate by design** (matching, randomisation, standardisation).
- **Stratify** in the analysis (blocks, mixed model with random effect).
- **Adjust** as covariate (regression).
- **Report as sensitivity analysis** (does the conclusion change if we exclude / include this group?).

Draw the **causal DAG** (use `dagitty.net` for visual). The DAG dictates which variables to adjust for (back-door criterion) and which to *avoid* (colliders introduce spurious correlations — see [[Common Statistical Pitfalls]]).

### 8. Preregister the primary analysis

Required sections for OSF / AsPredicted / clinicaltrials.gov:

1. **Hypothesis** — quantitative, falsifiable.
2. **Design** — between/within, blinding, allocation.
3. **Sampling plan** — n, stopping rule, recruitment source.
4. **Variables** — independent, dependent, covariates; measurement methods.
5. **Analysis plan** — primary test, secondary tests, multiple-comparison handling, equivalence test if "no difference" might be claimed.
6. **Inference criteria** — what decision at what p-value / effect size / CI?
7. **Exploratory analyses** — explicitly marked as such; will not be reported as confirmatory.

For confirmatory work, preregistration is non-negotiable; for genuinely exploratory work, say so and label findings exploratory in the eventual paper.

### 9. Lock the statistical analysis plan (SAP)

Before data collection: the primary outcome and test, assumption-check diagnostics with thresholds, the fallback test if assumptions fail, multiple-comparison correction, effect-size reporting (units + CI method), missing-data handling, pre-specified subgroup analyses, and any stopping rules with alpha spending.

A locked SAP makes the analysis confirmatory rather than an exploratory fishing trip. See [[Hypothesis Testing Decision Tree]] for test selection.

## Feasibility and Ethics Gate

Before declaring the design final, confirm:

- **Sample-size feasibility**: subjects available; in the time horizon; within budget; under current ethics approval.
- **Pilot data status**: if SESOI variance is unknown, run a small pilot **to estimate variance only** — do not preregister the pilot's outcome as confirmatory.
- **3Rs** (replace, reduce, refine) for animals; IRB consent for humans; biosafety for pathogens; data protection for personal data.
- **Sex as a biological variable** (NIH policy since 2016): include both unless biologically justified otherwise.

## Reporting Standards by Field

Follow the relevant guideline at publication:

- **ARRIVE 2.0** — preclinical animal studies.
- **CONSORT** — randomised clinical trials.
- **STROBE** — observational studies.
- **PRISMA** — systematic reviews / meta-analyses.
- **MIBBI family** (MIQE, MINSEQE, MIAME, MIAPE) — molecular biology / omics.
- **BIDS** — neuroimaging.

## References

- Festing & Altman (2002): "Guidelines for the design and statistical analysis of experiments using laboratory animals". *ILAR Journal* 43(4):244-258.
- Percie du Sert et al. (2020): ARRIVE 2.0 guidelines. *PLOS Biology* 18(7):e3000410.
- Lakens (2017): Equivalence testing for psychology research. *SPPS* 8(4):355-362.
- Nieuwenhuis et al. (2011): "Erroneous analyses of interactions in neuroscience". *Nature Neuroscience* 14:1105-1107.
- Pearl (2009): *Causality* (2nd ed.). DAGs and back-door criterion.
- NIH (2016): Sex as a Biological Variable policy (NOT-OD-15-102).
- OSF preregistration templates: https://osf.io/templates

[[relatedTo::Hypothesis Testing Decision Tree]]
[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Power Analysis and Sample Size Estimation]]
[[relatedTo::Reproducible Research Workflows]]
