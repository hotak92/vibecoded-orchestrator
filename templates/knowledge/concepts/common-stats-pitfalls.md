---
title: Common Statistical Pitfalls
type: concept
tags: [statistics, research-methods, reproducibility, p-hacking, multiple-comparisons, scientific-computing, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Common Statistical Pitfalls

## Overview

Most failures of statistical inference in published science come from a small, well-documented set of mistakes — not from exotic methods. This node catalogues the high-frequency offenders, their warning signs, and the corrections. It is a pre-submission checklist for any quantitative paper.

## The Big Six

### 1. Multiple Comparisons Without Correction

**The problem.** Run $k$ independent tests at $\alpha = 0.05$; the family-wise error rate (FWER) is $1 - (1-\alpha)^k$. At $k=20$ that's $\approx 0.64$ — better-than-even odds of at least one false positive.

**Detect.** A paper reports many univariate tests (e.g. "we tested 30 metabolites") and treats each $p < 0.05$ as a discovery.

**Correct.**
- **FWER control** (strict): Bonferroni ($\alpha / k$), Holm-Bonferroni (uniformly more powerful than Bonferroni, free upgrade).
- **FDR control** (less conservative, for screens with many true positives expected): Benjamini-Hochberg (BH). Standard in genomics. Report the BH-adjusted $q$-value.
- **Hierarchical / shrinkage** approaches (e.g. limma's empirical Bayes, Stan partial pooling) outperform raw correction when there's exchangeability across tests.

### 2. Garden of Forking Paths (Researcher Degrees of Freedom)

**The problem.** Even a single reported test is misleading if many analytical choices (which subset, which transform, which covariates, which outliers excluded) were tried first. The effective $k$ is the number of paths through the analysis decision tree, not the number of reported tests. Gelman & Loken (2014) showed this can inflate Type I error far above naive expectations.

**Detect.** No preregistration; exploratory and confirmatory analyses not separated; outlier rules decided after looking at the data; covariates included "for adjustment" without prespecification.

**Correct.**
- **Preregister** the primary analysis (OSF / AsPredicted / clinical trial registries). Mark anything else as exploratory.
- Adopt a **specification curve** or **multiverse analysis**: report effect size across all reasonable analytic choices, not just the best one (Steegen et al. 2016).
- For a confirmatory result, split the data: explore in a discovery set, lock the pipeline, test on a held-out replication set.

### 3. p-Hacking

**The problem.** Continuing to collect data, add/remove covariates, or switch tests until $p$ crosses 0.05. Even with good intent, this destroys the calibration of the p-value.

**Detect.** "p < 0.05" reported with no effect size, no CI, sample sizes that look like they stopped at the first significant result, suspiciously many $p$ values just below 0.05 (Simonsohn et al.'s p-curve diagnostic).

**Correct.**
- Decide stopping rule **before** collecting data, or use sequential designs with proper alpha spending (O'Brien-Fleming, Pocock).
- Report effect sizes and 95% CIs as primary, p-values as supporting.
- For Bayesian alternatives use a stopping rule on posterior precision rather than a p-value threshold.

### 4. Simpson's Paradox / Confounding by Aggregation

**The problem.** An association in aggregated data reverses or vanishes when you stratify by a hidden variable. Classic example: UC Berkeley admissions appeared sex-biased overall, but within each department men and women were admitted at similar rates; women applied disproportionately to more selective departments.

**Detect.** Strong aggregate effect with no plausible mechanism; or weak aggregate effect that you suspect is masking strong within-group effects. The presence of any plausible mediator/moderator that wasn't conditioned on.

**Correct.**
- Draw the causal **DAG** before analyzing. Use the back-door criterion to identify which variables to condition on (Pearl 2009).
- Stratify, or include the confounder as a covariate in regression. **But:** do not condition on a *collider* — that introduces spurious correlation.
- When in doubt, present both aggregated and stratified results.

### 5. Pseudoreplication

**The problem.** Treating non-independent observations as if they were independent inflates the effective sample size and shrinks p-values toward zero. Multiple cells from one animal, multiple sensors on one device, repeated technical injections — all are pseudoreplicates.

**Detect.** "n" reported as total measurements when biological/experimental unit is a higher level; degrees of freedom suspiciously large given the experimental design.

**Correct.**
- Identify the **experimental unit** (the thing that was independently randomized).
- Either **average within unit** before testing (loses some info but trivially correct), or **fit a mixed-effects model** with random intercept at the unit level — recovers the within-unit information properly.
- Lazic (2010) "The problem of pseudoreplication in neuroscientific studies" is the locus classicus.

### 6. Confusing Statistical Significance with Practical Importance

**The problem.** With huge $n$, trivial effect sizes are statistically significant. With tiny $n$, large real effects fail to reach significance. p-value answers "is there *any* effect" — not "is the effect meaningful".

**Detect.** No effect size reported; "significant difference" with no magnitude; or "no significant difference" interpreted as evidence of no effect (it's evidence of absence only if the study was powered to detect it — see equivalence testing).

**Correct.**
- Report **effect size with CI** as the primary inferential statistic (Cohen's $d$, $\eta^2$, hazard ratio, OR, mean difference with units).
- For "no effect" claims use **equivalence testing** (TOST) against a pre-specified smallest effect of interest (SESOI).
- Lakens (2017) on equivalence testing is the practical reference.

## Less Common but Devastating

### 7. Regression to the Mean

When subjects are selected for extreme baseline values, follow-up measurements drift toward the population mean *whether or not* the intervention works. A within-group pre-post comparison conflates real treatment effect with regression to mean. **Fix:** randomize, with a comparison group.

### 8. Survivorship Bias

Analyzing only the units that remained in the sample (cells that survived, papers that got published, customers who didn't churn) gives biased estimates of the unconditional process. **Fix:** explicitly model dropout / right-censoring; use survival models or inverse probability weighting.

### 9. Optional Stopping / Peeking

Looking at the data, doing an interim test, and continuing if $p > 0.05$ doubles or triples Type I error. **Fix:** Pre-specify interim analyses with appropriate alpha spending, or use Bayes factors which are well-calibrated under optional stopping.

### 10. HARKing — Hypothesizing After Results are Known

Reframing an exploratory finding as if it had been the prespecified hypothesis. Distorts the literature: nobody can tell which results were predictions and which were postdictions. **Fix:** preregistration and an explicit "exploratory analyses" section.

### 11. Misuse of Stepwise Variable Selection

Selecting predictors by significance ($p < 0.05$ to enter, $p > 0.10$ to leave) gives biased coefficients, anti-conservative CIs, and irreproducible models. **Fix:** Use LASSO / elastic net for sparsity with proper cross-validation, or model averaging.

### 12. Reporting Standardised Effects Where Raw Effects Are Comparable

Standardised effects (Cohen's $d$, partial $\eta^2$) depend on the variability of the sample; raw effects in natural units (mmHg, kg, °C) generalize better across studies. **Fix:** report raw effect with units as primary, standardized as supplement.

## Diagnostic Checklist (Pre-Submission)

For every quantitative claim, ask:

- [ ] Is the experimental unit clearly identified? Does the analysis respect it?
- [ ] Are multiple comparisons handled (Bonferroni / Holm / BH / hierarchical)?
- [ ] Was the analysis pre-specified, or is it exploratory? Labelled as such?
- [ ] Is the effect size reported with a CI?
- [ ] Are assumptions of the test checked (residual diagnostics, not raw-data normality)?
- [ ] Are confounders identified via a DAG and properly adjusted (not over-adjusted on colliders)?
- [ ] Is "no significant difference" supported by equivalence testing, not just $p > 0.05$?
- [ ] If selection on extremes occurred, is regression to mean modeled?
- [ ] Are technical replicates aggregated correctly?

## References

- Gelman & Loken (2014): "The garden of forking paths". *American Statistician*.
- Benjamini & Hochberg (1995): Controlling the FDR. *JRSS B*.
- Simmons, Nelson & Simonsohn (2011): "False-positive psychology". *Psychological Science*.
- Steegen et al. (2016): Multiverse analysis. *Perspectives on Psychological Science*.
- Lakens (2017): Equivalence testing (TOST). *Social Psychological and Personality Science*.
- Lazic (2010): Pseudoreplication. *BMC Neuroscience*.
- Pearl (2009): *Causality* (2nd ed.). DAGs and back-door criterion.
- Harrell (2015): *Regression Modeling Strategies* (2nd ed.). On stepwise selection.

[[relatedTo::Hypothesis Testing Decision Tree]]
[[relatedTo::Reproducible Research Workflows]]
[[relatedTo::Bayesian Inference]]
