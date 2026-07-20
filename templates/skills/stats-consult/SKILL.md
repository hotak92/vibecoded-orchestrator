---
name: stats-consult
description: Recommends the appropriate statistical test given a data description and research question, including assumption checks, alternatives if violated, sample-size guidance, and effect-size reporting. Use when the user asks "what test should I use", "is this t-test the right choice", "how many subjects do I need", "what's the right way to analyse this dataset", or describes a dataset + hypothesis without a chosen method.
short_desc: choose statistical test + assumptions + power analysis
keywords: ["t-test", ANOVA, "power analysis", "Mann-Whitney", "chi-squared", Wilcoxon, "what test should I use", "analyze this dataset", "sample size guidance"]
model: opus
effort: high
allowed-tools: Read, Write, WebSearch, Bash
---

# Stats Consult (Opus)

**Purpose**: Triage a research question + dataset description into a defensible statistical analysis plan. Recommend test family, list assumptions to verify, give a sample-size estimate, and call out the alternatives if assumptions fail.

**Model**: Opus — strong reasoning for choosing between methods.

**When to invoke autonomously**:
- The user has data and a question but no chosen method.
- The user proposes a method and you suspect a better one (e.g. they wrote "t-test" on count data, or "ANOVA" on repeated measures).
- The user asks about power or sample size.
- The user describes "we did 30 tests and 4 were significant" without mentioning correction.

**Do NOT invoke** when:
- The user already has a working analysis and just wants help running a function (use `@coder`).
- The question is purely about a probability concept (Bayesian vs frequentist philosophy) — answer in conversation.

## Usage

```
/stats-consult I have continuous fluorescence intensity from 3 cell lines, 4 dishes per line, 20 cells per dish. Do they differ?
/stats-consult Comparing pre/post treatment in 12 patients on a Likert pain scale. What test?
/stats-consult I screened 80 metabolites with t-tests, 9 came out p<0.05 — what now?
/stats-consult How many mice per group to detect a 25% reduction in tumour volume with 80% power?
```

## What This Skill Does

### 1. Triage Pipeline

1. **Identify outcome variable scale** — continuous, count, binary, ordinal, time-to-event, compositional, or circular. Ask if unclear.
2. **Identify design** — independent samples, paired/repeated, nested/hierarchical, or time series.
3. **Count groups and predictors.**
4. **Pick the parametric default** from the [[Hypothesis Testing Decision Tree]] table.
5. **List the assumptions** the user must verify before reporting (residual plots, not raw-data normality).
6. **Name the non-parametric / robust alternative** if assumptions fail.
7. **Flag multiple-comparison issues** if there are >5 outcomes or >3 groups.
8. **Estimate sample size** when asked or when the design suggests an underpowered study.
9. **Specify effect-size reporting** in natural units, with 95% CI.

### 2. Power and Sample Size Guidance

Practical defaults the skill applies:

- For a two-group continuous comparison at α=0.05, 80% power, the rule-of-thumb sample size is:
  - $n \approx 16 / d^2$ per group, where $d$ is Cohen's $d$ (standardised effect).
  - $d = 0.2$ (small) → ~400/group; $d = 0.5$ (medium) → ~64/group; $d = 0.8$ (large) → ~25/group.
- For paired tests with within-subject correlation $\rho$: divide the unpaired $n$ by $1 - \rho$ → fewer subjects when measurements are correlated.
- For proportions, use `statsmodels.stats.proportion.samplesize_proportions_2indep_onetail` or G*Power's $z$-test for two proportions; do not use the continuous-data rule.
- For survival: use Schoenfeld's formula for log-rank $n = (z_{\alpha/2} + z_{\beta})^2 / (p_1 p_2 \log^2 \mathrm{HR})$, where $p_i$ are the allocation fractions.
- For mixed-effects: simulation-based power (e.g. `simr` in R, `pymer4` or custom) — closed-form is unreliable.

Always sanity-check the resulting $n$ against feasibility (animals available, recruitment rate, budget). If $n$ is impossible, recommend a more focused hypothesis or a within-subject design.

### 3. Multiple-Comparison Decisions

| Situation | Recommend |
|---|---|
| 2-10 planned comparisons, want strict control | **Holm-Bonferroni** (free upgrade over Bonferroni) |
| 10-1000 outcomes, accept some false positives | **Benjamini-Hochberg FDR** (standard in genomics) |
| Many exchangeable tests (gene expression, GWAS) | **Empirical Bayes shrinkage** (limma; brms) — outperforms BH |
| Pairwise group comparisons after omnibus ANOVA | **Tukey HSD** (parametric) or **Dunn** (after Kruskal-Wallis) |
| Repeated post-hoc on the same data with adaptive choices | **Permutation maxT** or **simulation** — only honest answer |

### 4. Assumption-Check Checklist (Always Returned)

For the recommended test, the skill returns the specific diagnostics:

- Continuous outcome: Q-Q plot of **residuals** (not raw data); residuals-vs-fitted for heteroscedasticity; Cook's distance for influential points.
- Count outcome: dispersion test (variance/mean ≈ 1 for Poisson, otherwise negative binomial); zero-inflation visual.
- Binary / logistic: Hosmer-Lemeshow goodness-of-fit (deprecated for large $n$; use calibration plot); separation check.
- Survival / Cox: Schoenfeld residuals against time for proportional-hazards check; log-log plot.
- Mixed models: Q-Q plot of conditional residuals and of random-effect BLUPs; intraclass correlation should be positive and large enough to matter.
- Linear regression: VIF for multicollinearity (>10 flag); partial residual plots for linearity.

### 5. When to Switch to Bayesian

Recommend Bayesian (`pymc`, `numpyro`, `brms`, `Stan`) when:
- Prior information from previous studies materially improves precision.
- Sequential analysis or interim looks are planned.
- Direct probability statements about the parameter are needed for decision-making.
- Sample size is tiny and the prior provides useful regularisation.
- Hierarchical / partial-pooling structure is natural.

Recommend frequentist when:
- A peer-reviewed convention exists in the field (e.g. clinical trial primary analysis) and reviewers will demand it.
- Prior elicitation would be contentious and the analysis must be "objective".
- Time and computation are constrained — frequentist methods are usually orders of magnitude faster.

## Output Format

The skill returns a structured plan:

```markdown
## Statistical Analysis Plan

**Question**: [restate user question in 1 sentence]

**Data shape**:
- Outcome: [e.g. continuous, fluorescence intensity in arbitrary units]
- Design: [e.g. nested — 20 cells in 4 dishes in 3 cell lines]
- Experimental unit: [e.g. dish — biological replicate]
- Effective n per group: [e.g. 4]

**Recommended test**: [name, e.g. linear mixed model with random intercept for dish, fixed effect for cell line]

**Why**: [1-3 sentence rationale referencing the design]

**Implementation**:
```python
# concrete code in the user's stack (Python statsmodels / R lme4 / etc.)
```

**Assumptions to verify**:
- [diagnostic 1 with how to compute]
- [diagnostic 2]
- ...

**If assumptions fail**: [specific fallback — e.g. log-transform outcome, or switch to GAM, or bootstrap CIs on the mixed model]

**Multiple-comparison handling**: [explicit method if applicable]

**Effect size to report**: [e.g. cell-line contrasts in raw units with 95% CIs from `confint()`; Cohen's d as supplement]

**Sample size check**: [is the study adequately powered? if no, what would be needed?]

**Reporting template**:
> "[Outcome] differed across [groups] (mixed model, F(df1, df2) = X, p = Y).
>  Tukey-adjusted contrast between group A and B: estimate = Z [95% CI low, high], p = W."

**Pitfalls flagged**: [e.g. "treating cells as the unit instead of dishes inflates n 20-fold → severe Type I"]
```

## Decision Tree

```
Did the user describe an outcome variable?
├─ NO  → Ask what they measured. The scale dictates everything.
└─ YES → Continue.

Is the design clearly identified (paired? nested? time series?)
├─ NO  → Ask. Wrong assumed independence is the most common error.
└─ YES → Continue.

Apply the decision tree from knowledge/concepts/hypothesis-testing-decision-tree.md

Did the user mention "we tested N things"?
├─ YES → Multiple-comparison handling is mandatory.
└─ NO  → Check the design for hidden multiplicity (multiple time points, multiple genes/metabolites).

Is the sample size obviously small for the claimed effect?
├─ YES → Run a power calculation; state the minimum detectable effect.
└─ NO  → Note effect-size reporting requirements.
```

## Hard Rules

1. **Welch's t-test is the default two-sample t-test** — not Student's. Robust to unequal variance with negligible cost when variances are equal.
2. **Test residual normality, not raw-data normality.** The assumption is on residuals.
3. **Never recommend a one-tailed test** unless the user has explicitly preregistered a directional hypothesis. Default to two-sided.
4. **Never recommend Pearson correlation** without first eyeballing the scatter — use Spearman if the relationship is monotonic but non-linear.
5. **Always report effect size with CI.** P-values without effect sizes are uninformative.
6. **For "no difference" claims, recommend equivalence testing** (TOST in `statsmodels.stats.weightstats.ttost_ind` or `parameters::equivalence_test` in R) against a pre-specified SESOI, not just "p > 0.05".
7. **Flag pseudoreplication aggressively.** Multiple cells from one animal, technical replicates, repeated injections — they are not independent biological replicates.

## Integration with Knowledge Graph

The skill leans on these KG nodes:
- [[Hypothesis Testing Decision Tree]] for the test-selection logic.
- [[Common Statistical Pitfalls]] for the failure modes to actively guard against.
- [[Bayesian Inference]] for when to switch paradigms.
- [[Scientific Python Stack 2026]] for tooling recommendations.

After the consultation, if you produced novel guidance worth keeping, save it as a per-project KG node under `knowledge/concepts/stats-<topic>.md`.

## Success Criteria

- Test recommendation matches the data shape AND the design.
- Assumption diagnostics named with how to compute them.
- A robust fallback is named in case assumptions fail.
- Multiple-comparison strategy is appropriate for the number of tests.
- Effect-size reporting is in natural units with CIs.
- Sample-size guidance, when relevant, includes the minimum detectable effect.
- The plan is reproducible: anyone with the data could run it and get the same answer.
