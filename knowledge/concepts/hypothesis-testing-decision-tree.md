---
title: Hypothesis Testing Decision Tree
type: concept
tags: [statistics, hypothesis-testing, frequentist, research-methods, scientific-computing, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Hypothesis Testing Decision Tree

## Overview

Choosing the right statistical test is the single most common stumbling block for working scientists. The decision is governed by four orthogonal axes: (1) **outcome scale** (continuous, count, binary, ordinal, time-to-event), (2) **independence structure** (independent samples, paired/repeated, hierarchical/nested), (3) **number of groups or covariates**, and (4) **whether parametric assumptions hold** (normality of residuals, homoscedasticity, linearity). This node is a fast triage tool; for borderline cases prefer simulation-based or Bayesian alternatives over forcing a poor parametric fit.

## Decision Walk

### Step 1 — Identify the outcome variable

| Outcome | Examples | Typical family |
|---|---|---|
| Continuous, roughly Gaussian residuals | mass, length, fluorescence intensity | t-test / ANOVA / linear regression |
| Continuous, heavy-tailed or skewed | reaction times, gene-expression counts (log) | rank-based, or log-transform + parametric, or GLM |
| Count | reads per gene, photon counts, citations | Poisson / negative binomial GLM |
| Binary | survived/died, classified/not | logistic regression / chi-square / Fisher exact |
| Ordinal | Likert, histopathology grade | proportional-odds logistic / Mann-Whitney U |
| Time-to-event (with censoring) | survival, time-to-failure | Cox proportional hazards / Kaplan-Meier |
| Compositional (sums to 1) | microbiome relative abundance | log-ratio transform (CLR/ILR) + standard methods |
| Circular | wind direction, phase angles | Watson, Rayleigh, von Mises regression |

### Step 2 — Identify the design

- **Independent samples** (no link between observations across groups) → standard tests.
- **Paired / repeated measures** (same subject pre/post; littermates; technical replicates within sample) → paired-t, repeated-measures ANOVA, or mixed model with random intercept per subject. **Failing to model the pairing inflates Type I error.**
- **Nested / hierarchical** (cells within animals within litters; students within classrooms within schools) → mixed-effects (random intercepts/slopes per level). Treating pseudoreplicates as independent is one of the most common stats errors in biology.
- **Time series** (autocorrelated observations) → ARIMA, state-space models, or GAMs with autoregressive errors. Plain regression with `t` as a covariate underestimates standard errors.

### Step 3 — Number of groups / predictors

| Groups / predictors | Parametric path | Non-parametric / robust path |
|---|---|---|
| 1 group vs constant | one-sample t-test | Wilcoxon signed-rank |
| 2 independent groups | Welch's t-test (do not assume equal variance) | Mann-Whitney U / permutation test |
| 2 paired groups | paired t-test | Wilcoxon signed-rank |
| 3+ groups | one-way ANOVA + post-hoc (Tukey HSD) | Kruskal-Wallis + Dunn |
| 2+ factors | factorial ANOVA / linear model | aligned rank transform ANOVA |
| Continuous predictor | linear / GLM regression | GAM, quantile regression |
| Mixed continuous + categorical | linear / mixed model | GAM / random forest for prediction |

### Step 4 — Verify assumptions before reporting

| Assumption | How to check | What to do if violated |
|---|---|---|
| Approximate normality of residuals (not of raw data) | Q-Q plot of residuals; Shapiro-Wilk only as supplement | Transform, GLM with appropriate link, or non-parametric |
| Homoscedasticity | residuals vs fitted plot; Levene's test | Welch correction; robust SE (HC3); weighted least squares |
| Linearity (regression) | partial residual plots | add splines / polynomial / GAM |
| Independence | autocorrelation plot; design knowledge | mixed model / time-series structure |
| No influential outliers | Cook's distance, leverage | robust regression (Huber, M-estimators); investigate, don't blindly delete |

## Quick Reference Card

```
Continuous outcome
 ├── 2 independent groups → Welch t-test (default; not Student's)
 ├── 2 paired           → paired t-test
 ├── 3+ groups          → ANOVA + Tukey (or mixed model if nested)
 ├── 1 continuous predictor → linear regression
 └── multiple predictors    → multiple regression / GLM

Binary outcome
 ├── 2 groups           → chi-square (n>=5/cell) or Fisher exact (small)
 ├── adjusted           → logistic regression
 └── matched pairs      → McNemar

Count outcome
 └── Poisson GLM if mean ≈ variance, else negative binomial

Survival outcome
 ├── 2+ groups          → log-rank (univariate); Kaplan-Meier curves
 └── adjusted           → Cox proportional hazards (check PH assumption!)

Anything with nested / repeated structure
 └── mixed-effects model (random intercept at minimum)

When in doubt
 └── bootstrap or permutation test — assumption-light and intuitive
```

## When to Skip Frequentist Tests Entirely

Switch to Bayesian methods when:
- You have **informative priors** (previous studies, mechanistic constraints).
- You want **direct probability statements** about parameters ("P(effect > 0 | data) = 0.94"), not p-values.
- You're doing **sequential analysis** and stopping rules; frequentist methods require pre-specified stopping or alpha spending.
- Sample sizes are **very small** and the prior carries useful regularization.
- You need to **propagate uncertainty** through downstream calculations (e.g. compound model predictions).

See [[relatedTo::Bayesian Inference]] for the foundations, and `brms` / `PyMC` / `Stan` / `numpyro` as practical entry points.

## Common Misuses Flagged Here

1. **Using Student's t-test instead of Welch's** by default. Welch is robust to unequal variance with no power cost when variances *are* equal — make it the default.
2. **Testing normality of the raw outcome instead of the residuals**. The regression assumption is about residuals.
3. **Reporting p < 0.05 after picking the test that gave it** — see [[relatedTo::Common Statistical Pitfalls]].
4. **Treating technical replicates as biological replicates**. They are pseudoreplicates and must be averaged (or modeled as nested random effects), not stacked.
5. **Running multiple pairwise t-tests instead of ANOVA + post-hoc**. Inflates family-wise error.
6. **Ignoring zero-inflation in count data** — fit a zero-inflated negative binomial (`pscl::zeroinfl`, `statsmodels.ZeroInflatedNegativeBinomialP`).

## References

- Wasserstein & Lazar (2016): The ASA Statement on p-Values.
- Gelman & Hill (2007): *Data Analysis Using Regression and Multilevel/Hierarchical Models*. The canonical mixed-models reference.
- Harrell (2015): *Regression Modeling Strategies* (2nd ed.). Indispensable for survival, logistic, and ordinal.
- Cumming (2014): The New Statistics — effect sizes and CIs over p-values. *Psychological Science*.
- Halsey et al. (2015): "The fickle P value generates irreproducible results", *Nature Methods*.

[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Bayesian Inference]]
[[relatedTo::Reproducible Research Workflows]]
