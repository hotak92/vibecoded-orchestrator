---
title: Power Analysis and Sample Size Estimation
type: concept
tags: [statistics, power-analysis, sample-size, experimental-design, scientific-computing, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Power Analysis and Sample Size Estimation

## Overview

A study underpowered for its claimed effect is wasted resources at best, and exposes subjects to risk without ability to detect benefit at worst. Power analysis answers two complementary questions: **a priori** ("how many subjects do I need to detect an effect of size SESOI with probability $1-\beta$?") and **post hoc design** ("given my n, what is the minimum detectable effect?"). This node collects the formulas, the tools, and the failure modes. Test selection is in [[Hypothesis Testing Decision Tree]]; design context in [[Experimental Design Framework]].

## Inputs Every Power Calculation Needs

1. **SESOI** — smallest effect of interest in natural units (or as a standardised effect like Cohen's $d$, OR, HR).
2. **Variability estimate** — pooled SD from pilot or literature; required for standardising SESOI.
3. **Significance threshold** $\alpha$ — default 0.05, **adjusted down if multiple comparisons** (see [[Common Statistical Pitfalls]]).
4. **Power target** $1-\beta$ — default 0.80; use 0.90 for high-stakes confirmatory work.
5. **Test type** — two-tailed default; one-tailed only for a preregistered directional hypothesis.

Without an estimate of (2), any sample size is a guess. If no estimate exists, run a small pilot **to estimate variance only** — do not test the hypothesis from pilot data.

## Closed-Form Approximations

### Two independent groups, continuous outcome

For Welch's t-test at $\alpha=0.05$ two-sided, 80% power, the rule-of-thumb per-group sample size is:

$$n \approx \frac{16}{d^2}$$

where $d$ is Cohen's $d = \text{SESOI} / \sigma_\text{pooled}$.

| $d$ | Interpretation | $n$ per group (≈) |
|---|---|---|
| 0.2 | small | 400 |
| 0.5 | medium | 64 |
| 0.8 | large | 25 |
| 1.0 | very large | 16 |

The full formula is $n = 2(z_{\alpha/2} + z_\beta)^2 / d^2$; "16" is $\approx 2(1.96+0.84)^2 \approx 15.7$. For 90% power use $\approx 21/d^2$.

### Paired / within-subject

With within-subject correlation $\rho > 0$, pairing reduces the variance of the difference: $\sigma_D^2 = 2\sigma^2(1-\rho)$ instead of $2\sigma^2$ for two independent samples. For the **same power**:

$$n_\text{paired subjects} \approx (1 - \rho) \cdot n_\text{per group, unpaired}.$$

With $\rho = 0.7$, the paired design needs roughly 30% as many subjects as a single arm of the unpaired equivalent. Pairing only pays when the pairing structure genuinely reduces noise (matched littermates, same subject pre/post, identical-batch cell cultures). With $\rho \approx 0$ pairing adds no power and costs a degree of freedom.

### Two proportions

Use the normal approximation:

$$n \approx \frac{(z_{\alpha/2} + z_\beta)^2 \cdot [p_1(1-p_1) + p_2(1-p_2)]}{(p_1 - p_2)^2}$$

per group. For small $n$ or extreme proportions, use exact methods: `statsmodels.stats.proportion.samplesize_proportions_2indep_onetail` or G*Power. Continuous-data rules are inappropriate.

### Survival / time-to-event (log-rank)

Schoenfeld's formula gives the required **number of events** (not subjects):

$$E = \frac{(z_{\alpha/2} + z_\beta)^2}{p_1 p_2 \log^2(\text{HR})}$$

where $p_i$ are allocation fractions, HR is the hazard ratio. Convert events → subjects using the expected event rate and follow-up time. Use `lifelines.statistics.sample_size_necessary_under_cph` in Python or `gsDesign` / `Hmisc::cpower` in R.

## When Closed-Form Fails — Simulation

Closed-form approximations are unreliable for:

- **Mixed-effects** with cluster-level confounders (use `simr` in R, or custom Monte Carlo).
- **Longitudinal** with planned covariate adjustment.
- **Adaptive** designs with sample-size re-estimation.
- **Complex multiple-comparison** structures (hierarchical, FDR-based).
- **Non-standard outcomes** (zero-inflated, compositional, censored at multiple levels).

The simulation recipe:

```python
def simulate_power(n_per_group, sesoi, sd, n_sims=2000, alpha=0.05):
    """Monte Carlo power for two-group Welch t-test."""
    import numpy as np
    from scipy import stats
    rng = np.random.default_rng(20260519)
    significant = 0
    for _ in range(n_sims):
        a = rng.normal(0, sd, n_per_group)
        b = rng.normal(sesoi, sd, n_per_group)
        if stats.ttest_ind(a, b, equal_var=False).pvalue < alpha:
            significant += 1
    return significant / n_sims
```

Scan over candidate $n$ to find the smallest value yielding power $\geq 0.80$. For mixed models, simulate from the assumed random-effects structure, fit the model, count significant tests.

## Tooling

### Python

- **`statsmodels.stats.power`** — closed-form for t, F, proportion, χ² tests:
  ```python
  from statsmodels.stats.power import TTestIndPower
  TTestIndPower().solve_power(effect_size=0.5, alpha=0.05, power=0.80, alternative='two-sided')
  ```
- **`statsmodels.stats.proportion`** — exact and approximate for proportions.
- **`lifelines.utils`** — survival sample size.
- **Custom Monte Carlo** for mixed/complex — pair `simulate_power` with `joblib.Parallel` for scan speed.

### R

- **`pwr`** — closed-form, equivalent to `statsmodels.stats.power`.
- **`pwrss`** — broader coverage (including ANCOVA, mixed models).
- **`simr`** — simulation-based for `lme4` mixed models. Gold standard for hierarchical designs.
- **`gsDesign`** — group-sequential / adaptive trials.

### Standalone

- **G\*Power** — GUI, free, covers most fixed-effect designs; standard citation in clinical trials.
- **PASS** — commercial; broader coverage of regulated-environment designs.

## Failure Modes

1. **Post-hoc "achieved power" calculations** based on the observed effect are meaningless and discouraged by reporting guidelines (Goodman & Berlin 1994; Hoenig & Heisey 2001). If you want to characterise what you could have detected, report the **minimum detectable effect size** for your $n$ instead.
2. **Choosing $d$ from the literature without accounting for publication bias** inflates the assumed effect; expect the SESOI to be substantially smaller than published estimates. Use the lower bound of a credible interval, or a within-lab pilot estimate.
3. **Powering for the most easily significant outcome** instead of the primary outcome of interest — undermines confirmatory validity.
4. **Ignoring expected dropout** — recruit $n / (1 - \text{dropout rate})$, not $n$.
5. **Ignoring multiple-comparison adjustment** when powering — if you'll Bonferroni-correct across 10 outcomes, power for $\alpha = 0.005$, not $0.05$.
6. **Confusing "sample size" with "events" in survival** — events drive log-rank power, not subjects. Schedule follow-up long enough to accrue events.
7. **Powering at the wrong level** — for cluster-randomised trials, the effective $n$ is the number of clusters (adjusted by the intra-cluster correlation), not the number of individuals.

## When Power Says "Infeasible"

If the required $n$ exceeds what the lab can run, options are:

- **Increase precision**: tighter measurement (better assay, technical replicates within unit), more uniform subjects, paired design.
- **Increase the SESOI**: are you really aiming to detect a 5% change, or would 15% be practice-changing?
- **Increase α**: rarely defensible, but explicit α=0.10 with preregistered direction is honest in some screening contexts.
- **Switch to Bayesian**: with an informative prior from prior work, the posterior may achieve practical certainty with fewer subjects. See [[Bayesian Inference]].
- **Abandon the question**: the most honest answer is sometimes "this hypothesis is not testable with available resources".

## References

- Cohen (1988): *Statistical Power Analysis for the Behavioral Sciences* (2nd ed.). The canonical $d$-table and rules of thumb.
- Schoenfeld (1983): "Sample-size formula for the proportional-hazards regression model". *Biometrics* 39:499-503.
- Hoenig & Heisey (2001): "The abuse of power: the pervasive fallacy of power calculations for data analysis". *American Statistician* 55(1):19-24.
- Green & MacLeod (2016): "SIMR: an R package for power analysis of generalized linear mixed models". *Methods in Ecology and Evolution* 7:493-498.
- Lakens (2022): "Sample size justification". *Collabra: Psychology* 8(1):33267.

[[relatedTo::Hypothesis Testing Decision Tree]]
[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Experimental Design Framework]]
[[relatedTo::Bayesian Inference]]
