---
title: Scientific Python Stack 2026
type: tool
tags: [python, scientific-computing, numpy, scipy, xarray, polars, pandas, anndata, scanpy, scikit-image, astropy, biopython, low-level-implementation]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Scientific Python Stack 2026

## Overview

Survey of the libraries that a working scientist actually reaches for in 2026, grouped by purpose. Choices that have shifted in the past 2-3 years are flagged explicitly. Default recommendations assume CPython 3.12+; many libraries now also run on PyPy or accept JAX/PyTorch arrays via the array-API standard.

## Numerical Core

| Library | Role | 2026 status |
|---|---|---|
| **`numpy`** (>= 2.0) | n-D arrays, linear algebra, random | NumPy 2.0 broke a small ABI but is now the baseline. Use `np.random.default_rng()`, not legacy `np.random.seed`. |
| **`scipy`** | Sparse, optimisation, special functions, signal, integrate, stats, FFT | Still indispensable. `scipy.stats` is the standard frequentist toolkit; `scipy.optimize.minimize` for general optimisation. |
| **`sympy`** | Symbolic math, integration, dimensional checks | First port of call for "is this derivation right?" |
| **`numba`** | JIT-compiled numpy loops | Use when vectorisation won't fit; near-C speed for `@njit` functions with array math. |
| **`cython`** | AOT compilation for performance-critical extensions | Heavier setup than `numba`; warranted for library code. |

## Array Containers Beyond NumPy

- **`xarray`** — labelled n-D arrays. The standard for any data that has named, indexed dimensions: climate (time × lat × lon × level), MRI (x × y × z × subject × session), spectroscopy (sample × wavelength). Pairs with `dask` for out-of-core.
- **`zarr`** — chunked, compressed n-D arrays on disk or cloud. NetCDF's modern successor; xarray reads/writes it natively. Use Zarr v3 for new datasets (the v2/v3 transition is in progress as of 2026).
- **`dask`** — task-graph parallelism for NumPy/pandas-shaped workloads larger than memory; `dask.distributed` adds cluster support.
- **`jax`** — for differentiable scientific computing, accelerated numerical methods, ODE-solving (`diffrax`), and physics-informed NNs. `jax.numpy` is a drop-in mental model.

## DataFrames

| Use case | Pick |
|---|---|
| In-memory, mixed-type tabular data | **`polars`** (default for new code) — Rust-backed, lazy execution, expression API, multi-threaded by default, ~3-30× faster than pandas on common workloads. |
| Compatibility / SciPy ecosystem / legacy code | **`pandas`** (>= 2.2) — still ubiquitous; PyArrow backend mostly closes the speed gap. |
| Multi-dataframe-library code | **`narwhals`** — write code that runs on pandas, polars, or pyarrow without if/else. |
| Bigger-than-RAM tabular | **`duckdb`** (SQL) or `polars` lazy or `dask.dataframe`. DuckDB is increasingly the right default — embedded, vectorised, reads parquet/CSV in place. |
| Cross-language exchange | **`pyarrow`** (Arrow IPC, Parquet) — interop with R (arrow), Julia, Rust, Go. |

## Plotting

- **`matplotlib`** — universal baseline; required for any journal-quality static figure.
- **`seaborn`** — statistical wrappers around matplotlib; quick exploratory plots with sensible defaults.
- **`plotnine`** — ggplot2 grammar-of-graphics in Python; same paradigm as R `ggplot2`.
- **`altair` / `vega`** — declarative, browser-native; ideal for interactive supplementary figures.
- **`plotly`** — interactive, web-based; common in dashboards (`dash`).
- **`bokeh`** — large-data interactive; pairs with `panel` for web apps.
- **`holoviews`** + **`hvplot`** — high-level API on top of bokeh/matplotlib; great for xarray-shaped data.

For figures with thousands of points: rasterise the data layer (`rasterized=True`) but keep axes/labels vector — typical journal compromise.

## Statistics and Modelling

- **`statsmodels`** — frequentist regression with proper inference (CIs, robust SE, mixed models via `mixedlm`, time series via `tsa`).
- **`scipy.stats`** — distributions, basic tests, bootstrap (`scipy.stats.bootstrap`), permutation tests (`scipy.stats.permutation_test`).
- **`pingouin`** — friendlier wrappers around common ANOVA / t-test / correlation analyses with effect sizes reported by default.
- **`pymc`** — Bayesian modelling with PyMC v5 (PyTensor backend). Stan-quality, more Pythonic.
- **`numpyro`** — Bayesian modelling on JAX; faster than PyMC on GPUs for some models; uses NUTS.
- **`stan`** via `cmdstanpy` — when you need state-of-the-art HMC and a stable, paper-citable backend.
- **`lifelines`** — survival analysis (Kaplan-Meier, Cox PH, Aalen additive); `scikit-survival` for ML-flavoured survival.
- **`bambi`** — high-level Bayesian regression (formulaic, brms-style) on top of PyMC.
- **`dowhy`**, **`econml`**, **`pgmpy`** — causal inference / graphical models.

## Machine Learning

- **`scikit-learn`** — classical ML baseline; pipelines, cross-validation, all the bread-and-butter estimators.
- **`pytorch`** — default deep-learning framework for research; `lightning` for training-loop boilerplate.
- **`jax`** + **`flax`** / **`equinox`** — competing DL stack with stronger functional programming guarantees, often preferred in scientific ML.
- **`xgboost`** / **`lightgbm`** / **`catboost`** — gradient-boosted trees; still the right answer for most tabular problems.
- **`torch-geometric`**, **`dgl`** — graph neural networks.
- **`huggingface/transformers`** — pretrained models for NLP; for biology there are domain-specific models (ESM-2/ESM-3 for proteins, scGPT/Geneformer for single-cell, etc.) often released through HuggingFace.
- **`optuna`** — hyperparameter search (Bayesian / TPE / pruning).

## Domain Libraries

### Bioinformatics & Single-Cell

- **`biopython`** — sequences, alignments, parsing FASTA/FASTQ/GenBank/PDB. Slower than specialist tools but ubiquitous for one-off scripts.
- **`anndata`** — annotated data structure (X + obs + var + obsm + uns) underpinning single-cell tools. Reads/writes `.h5ad`.
- **`scanpy`** — single-cell RNA-seq analysis (QC, normalisation, neighbours, UMAP/t-SNE, clustering, DE).
- **`scvi-tools`** — probabilistic models for single-cell (scVI, totalVI, scANVI); PyTorch-based.
- **`pyranges`** — fast genomic-interval operations; like bedtools in Python.
- **`pysam`** — SAM/BAM/CRAM/VCF I/O.

### Imaging

- **`scikit-image`** — general image processing; segmentation, morphology, filters.
- **`napari`** — n-D image viewer with a plugin ecosystem; standard for microscopy.
- **`itk` / `simpleitk`** — medical imaging registration and segmentation.
- **`nibabel`** — neuroimaging file formats (NIfTI, DICOM, GIFTI).
- **`pydicom`** — DICOM reading/writing.
- **`cellpose`**, **`stardist`** — modern segmentation models (cells, nuclei).

### Astronomy & Geophysics

- **`astropy`** — units, constants, time, coordinates, FITS I/O, cosmology. Use `astropy.units` for all dimensional code in astronomy.
- **`sunpy`** — solar physics.
- **`heliopy`** / **`spacepy`** — heliospheric / space physics.
- **`iris`** + **`cartopy`** — climate model output, geographic plotting. Increasingly replaced by `xarray` + `cartopy` directly.
- **`metpy`** — meteorology.
- **`obspy`** — seismology.

### Chemistry & Materials

- **`rdkit`** — cheminformatics (SMILES, fingerprints, 2D/3D structures).
- **`ase`** — Atomic Simulation Environment; calculator interface to DFT/MD codes.
- **`pymatgen`** — materials genomics, crystal structures, electronic structure analysis.
- **`openmm`** — high-performance MD engine with a clean Python API; GPU support.

## Workflow and Infrastructure

- **`snakemake`** / **`nextflow`** — see [[relatedTo::Reproducible Research Workflows]].
- **`pooch`** — downloading reference data with SHA256 verification.
- **`pint`** — units and dimensional analysis at runtime; integrates with pandas and xarray.
- **`tqdm`** — progress bars; pair with `joblib` for parallel loops.
- **`joblib`** — easy parallelism, caching of expensive function outputs.
- **`hydra`** + **`omegaconf`** — hierarchical experiment configuration; sweeps with one CLI flag.
- **`mlflow`** / **`wandb`** / **`aim`** — experiment tracking; pick one and stay consistent.

## What I'd Choose Today, for a Greenfield Project

1. **Project skeleton**: `uv` (Python pure) or `pixi` (mixed deps).
2. **Tabular data**: `polars` + `duckdb` for any computation; `pandas` only for last-mile interop with tools that haven't migrated.
3. **N-D scientific data**: `xarray` on `zarr` storage; `dask` for parallelism.
4. **Stats**: `statsmodels` + `scipy.stats` for frequentist; `pymc` or `numpyro` for Bayesian; `pingouin` for one-liner exploratory tests.
5. **ML**: `scikit-learn` for tabular baselines; `pytorch` + `lightning` for deep learning; `xgboost` for tabular competitions.
6. **Plotting**: `matplotlib` for the paper figure (`seaborn` for quick stats overlays); `altair` for an interactive supplement.
7. **Workflow**: `snakemake` for moderate complexity; `nextflow` if you're in genomics or need polyglot tasks.
8. **Units**: `pint` or `astropy.units` everywhere physical quantities cross function boundaries.

## What to Avoid in New Code

- Legacy `np.random.seed(...)` global — use `np.random.default_rng(seed)`.
- `pandas` `inplace=True` — slated for deprecation; chain instead.
- Raw `pip freeze` as a "lockfile" — no hashes, no platform info. Use `uv` or `pixi` lockfiles.
- `multiprocessing.Pool` with fork on macOS — broken since Python 3.8 changed default to spawn. Use `joblib`, `concurrent.futures`, or a workflow engine instead.
- `keras` (standalone) — use `tf.keras` or migrate to PyTorch; standalone keras was deprecated, then resurrected as Keras 3 with multi-backend support but most science code has settled on PyTorch.

## References

- The SciPy stack documentation (canonical): https://scipy.org/
- The PyData ecosystem map: https://pydata.org/
- Scientific Python Ecosystem Coordination (SPEC) documents: https://scientific-python.org/specs/ — see SPEC 0 (deprecation policy), SPEC 4 (using Polars), and SPEC 7 (random number policy).
- Harris et al. (2020): "Array programming with NumPy". *Nature* 585:357-362.
- Virtanen et al. (2020): "SciPy 1.0". *Nature Methods* 17:261-272.

[[relatedTo::Reproducible Research Workflows]]
[[relatedTo::Dimensional Analysis as a Debugging Tool]]
