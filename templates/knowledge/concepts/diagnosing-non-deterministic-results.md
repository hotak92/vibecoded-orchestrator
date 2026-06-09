---
title: Diagnosing Non-Deterministic Results
type: concept
tags: [reproducibility, debugging, scientific-computing, randomness, gpu, blas, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Diagnosing Non-Deterministic Results

## Overview

"I get different numbers each run" is the most common reproducibility complaint in computational science. The cause is rarely a single bug; it's usually one of seven well-documented sources of drift, each with a distinct fingerprint and fix. This node is the diagnostic ladder: characterise the magnitude of the drift first, then trace it back to its source. For the full reproducibility framework see [[Reproducible Research Workflows]]; for the audit workflow that this complements see paper-triage and audit-style workflows in the orchestrator's skill bundle.

## Step 1 — Characterise the Drift

Before guessing causes, measure the symptom. Diff the outputs of two independent runs and classify:

| Magnitude | Likely class |
|---|---|
| Bit-equivalent | The pipeline is deterministic; user is mistaken |
| $\sim 10^{-15}$ to $10^{-12}$ relative | Floating-point reduction order (BLAS, GPU) |
| $\sim 10^{-7}$ to $10^{-5}$ relative | Single-precision reduction, optimiser non-determinism |
| Cluster labels permuted, distances preserved | Stochastic labelling, not a bug |
| $> 1\%$ relative on key statistics | An RNG isn't seeded |
| Drift only on specific inputs | Numerical instability (near-singular matrix, division by near-zero) |
| Drift between machines | Library / hardware / OS difference |
| Drift between Python versions | Hash-randomisation (dict / set ordering before Python 3.7), or behaviour change in a library |

Show the user the diff before assuming a category — many "drift" complaints turn out to be (a) the user re-running a step that *should* be stochastic and not seeding it, or (b) cluster-label permutation that doesn't actually change the partition.

## Step 2 — Trace by Source

### 1. Unseeded RNG (most common; large drift)

Symptom: different numerical results on every run; orders of magnitude larger than floating-point error.

Diagnostic: grep for stochastic calls. Common offenders:
- `np.random.*` without prior `np.random.seed()` or modern `default_rng()`.
- `random.*` without `random.seed()`.
- `torch.randn`, `torch.rand`, `torch.manual_seed` missing.
- `sklearn` estimators with stochasticity (`KMeans`, `RandomForestClassifier`, `train_test_split`, `LogisticRegression(solver='saga')`) without `random_state=SEED`.
- `umap.UMAP(...)` and `sklearn.manifold.TSNE(...)` without `random_state=SEED`.
- `jax.random` PRNG keys not threaded through.

Fix: a master seed at the entry point, with explicit seeding of every RNG library:

```python
import numpy as np
import random
import torch

SEED = 20260519
random.seed(SEED)
np.random.seed(SEED)            # legacy global (still needed for some libs)
rng = np.random.default_rng(SEED)  # modern API, preferred

torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)
```

Every `sklearn` estimator: pass `random_state=SEED`.

### 2. Parallel-worker seeding (medium drift)

Symptom: drift only when workers > 1; results stable in serial mode.

Each parallel worker (DataLoader worker, joblib job, multiprocessing process) needs its own deterministic seed derived from the master seed.

Fix for PyTorch DataLoader:

```python
import torch

def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)

loader = torch.utils.data.DataLoader(
    dataset, batch_size=32,
    worker_init_fn=seed_worker,
    generator=torch.Generator().manual_seed(SEED),
)
```

For joblib:

```python
from joblib import Parallel, delayed

results = Parallel(n_jobs=8)(
    delayed(task)(i, seed=SEED + i)   # explicit per-task seed
    for i in range(n_tasks)
)
```

### 3. Floating-point reduction order (epsilon drift, $\sim 10^{-12}$)

Symptom: tiny relative differences on otherwise identical pipelines. Common when thread counts vary across runs.

Cause: floating-point addition is not associative. `(a + b) + c ≠ a + (b + c)` at machine epsilon. BLAS implementations parallelise reductions; the order of summation depends on the number of threads and the BLAS variant.

Fix:
- Pin `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS` to the same value across runs (often 1 for deterministic BLAS, or any fixed value).
- Compare with relative tolerance (`np.allclose(a, b, rtol=1e-10)`) rather than equality.
- For deterministic linear algebra, accept the performance hit.

### 4. GPU non-determinism (epsilon-to-percent drift)

Symptom: drift on GPU runs that disappear in CPU runs of the same code.

Causes: cuDNN's `benchmark=True` selects different algorithms based on input shape; atomic reductions in CUDA kernels have undefined accumulation order; NCCL collectives are non-deterministic by default; some optimised kernels (e.g. scatter operations) explicitly trade determinism for throughput.

Fix (PyTorch):

```python
import torch

torch.use_deterministic_algorithms(True)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
# Required for some deterministic algorithms:
import os
os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'
```

Expect a 10-30% throughput hit. Some operations have no deterministic implementation on GPU; PyTorch will raise unless you set `torch.use_deterministic_algorithms(True, warn_only=True)`. For full bit-equivalence on GPU, accept the speed hit, or rerun $k$ times and report distribution.

JAX: deterministic by default at fixed PRNG key; non-determinism enters via fp16 reductions and async dispatch — usually negligible.

### 5. Cluster-label permutation (no real drift)

Symptom: cluster IDs differ across runs but the *partition* is the same.

Diagnostic: compute `sklearn.metrics.adjusted_rand_score(labels_run1, labels_run2)`. If ARI is ~1.0, only labels are permuted; no actual drift. If ARI < 1.0, the clustering really differs.

Fix: don't compare raw labels; use partition-invariant metrics (ARI, NMI, V-measure). For visualisation, relabel via the Hungarian algorithm (`scipy.optimize.linear_sum_assignment`).

### 6. Numerical instability on specific inputs (input-dependent drift)

Symptom: drift only for some inputs; others are stable.

Cause: a near-singular matrix in `linalg.solve`, division by near-zero, log of near-zero, exp of large positive number causing overflow. Different runs hit the instability at different points (e.g. depending on regularisation noise).

Fix: identify the offending operation. Add explicit regularisation (`+ ε * I` for matrix inverses; clip values before `log`; use `logaddexp` / `softmax_with_loss` formulations for numerical stability). For SVD-based solvers, set `rcond` explicitly. For iterative methods, fix tolerance and `max_iter`. Use `np.linalg.cond(A)` to diagnose ill-conditioning.

### 7. Cross-machine drift (architecture / library variation)

Symptom: same code, different machines, different results — even with seeds and threading pinned.

Causes: BLAS vendor (MKL vs OpenBLAS vs Accelerate vs cuBLAS) computes reductions in different orders; different `glibc` versions affect `pow`/`log` last-bit behaviour; different CUDA / cuDNN versions select different kernels; CPU instruction-set extensions (AVX2 vs AVX-512) take different code paths.

Fix: use a container (Docker / Apptainer) pinned by image digest. Reproducibility-across-machines is a containers-or-bust problem.

## The Drift-Diagnosis Recipe

When the user reports drift:

1. **Get the diff**. Show me two runs, side by side. Is it $10^{-12}$ or $1\%$?
2. **Map magnitude to class** using the table in Step 1.
3. **Identify the source** using Step 2. Most often: unseeded RNG, then GPU non-determinism, then thread-count drift, then numerical instability.
4. **Fix the source, verify with a third run**. Re-run twice independently after the fix; results should now be identical (or epsilon-close, documented).
5. **Document the determinism guarantee in the README**: "Two independent runs with the same seed and thread count produce outputs that agree within $1 \times 10^{-10}$ relative tolerance on x86_64 Linux. GPU runs match within $1 \times 10^{-5}$ relative."

## Useful Tools

- **`hash` of pickled outputs**: `hashlib.sha256(pickle.dumps(result)).hexdigest()` — fast bit-equivalence check.
- **`difflib.unified_diff`** for text outputs.
- **`numpy.testing.assert_allclose`** for arrays; controls absolute and relative tolerance independently.
- **`sklearn.metrics.adjusted_rand_score`** for partition comparison.
- **`np.linalg.cond`** to diagnose ill-conditioning.
- **`PYTHONHASHSEED=0`** environment variable to make Python's hash randomisation deterministic (affects dict / set iteration order, mostly irrelevant since Python 3.7 made dicts insertion-ordered).

## When to Accept Drift

Some drift is unavoidable and not worth chasing:

- **Stochastic optimisation** (SGD, Adam) with non-deterministic GPU: rerun $k$ times, report mean ± SD across seeds.
- **Bootstrap / permutation tests**: drift in p-values across runs scales as $1/\sqrt{n_\text{bootstrap}}$. Increase $n_\text{bootstrap}$ until the drift is below practical relevance.
- **MCMC sampling**: every chain is stochastic. Report the **distribution** of posteriors across chains, not a single number. Trace plots and $\hat{R}$ are diagnostics; aim for $\hat{R} < 1.01$ across independent chains.

Document the determinism level the user can expect; don't promise bit-equivalence you can't deliver.

## References

- Goldberg (1991): "What every computer scientist should know about floating-point arithmetic". *ACM Computing Surveys* 23(1):5-48. Foundational reference on FP non-determinism.
- Salmon et al. (2011): "Parallel Random Numbers: As Easy as 1, 2, 3". *SC '11*. Counter-based RNGs that solve parallel-worker seeding cleanly (Threefry, Philox); now standard in NumPy's `default_rng`.
- NVIDIA cuDNN documentation: https://docs.nvidia.com/deeplearning/cudnn/developer-guide/index.html#reproducibility
- PyTorch reproducibility notes: https://pytorch.org/docs/stable/notes/randomness.html
- The Turing Way — Reproducible computational analyses: https://the-turing-way.netlify.app/

[[relatedTo::Reproducible Research Workflows]]
[[relatedTo::Scientific Python Stack 2026]]
[[relatedTo::HPC Job Submission Patterns]]
