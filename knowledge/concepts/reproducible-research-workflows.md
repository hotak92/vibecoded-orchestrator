---
title: Reproducible Research Workflows
type: concept
tags: [reproducibility, scientific-computing, workflow, snakemake, nextflow, uv, conda, pixi, dvc, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Reproducible Research Workflows

## Overview

"Reproducible" means a third party with the same inputs, the same code, and the same environment can recover the same outputs *bit-for-bit* or *within documented numerical tolerance*. Achieving this in 2026 requires four layers: (1) **environment pinning**, (2) **versioned data**, (3) **workflow orchestration** with explicit dependency graphs, and (4) **deterministic randomness**. Each layer has well-established tools; the friction is process, not technology.

## The Four Layers

### Layer 1 — Environment Pinning

The goal: anyone can recreate a byte-equivalent (or as close as the OS allows) software stack.

**Python (recommended hierarchy, 2026):**

1. **`uv`** (Astral) — fast resolver, lockfile-first, drop-in for pip/venv. The default for new Python projects now. Lockfile is `uv.lock`. Use `uv pip compile requirements.in > requirements.txt` for older flows, or `uv sync` for the full project layout.
2. **`pixi`** (Prefix.dev) — conda-compatible package manager with proper lockfiles across platforms. Use when you need non-Python dependencies (CUDA toolkit, R, compilers) alongside Python. Lockfile `pixi.lock` is the source of truth.
3. **`conda` / `mamba`** — still common, but lockfiles are awkward unless you use `conda-lock`. Migrate to `pixi` for new projects.
4. **`pip` + `pip freeze`** — last resort. `pip freeze > requirements.txt` is *not* a lockfile (no hashes, no platform tagging). Acceptable only for trivial scripts.

**R**: `renv` (project-local library + lockfile). MRAN snapshots are no longer maintained — pin via Posit Public Package Manager (PPM) snapshots instead.

**Julia**: `Project.toml` + `Manifest.toml` are first-class; commit both. `Pkg.instantiate()` recreates exactly.

**Container fallback**: when system libraries matter (CUDA, MPI, BLAS variant), put the whole thing in a Docker / Apptainer (formerly Singularity) image and pin by digest hash, not tag. `FROM ubuntu:22.04@sha256:abc...`.

### Layer 2 — Data Versioning

Code in git, data not. Data lives in object storage with content-addressable hashes:

- **DVC** (Data Version Control) — Git-LFS-style metadata in git, data on S3/GCS/Azure/SSH/local. Pairs naturally with workflow engines.
- **Git LFS** — fine for files up to ~GB; chokes above that. Hosting often charges for LFS bandwidth.
- **`pooch`** — Python library for downloading data files from URLs with SHA256 verification at import time. Lightweight; ideal for "make my test fixtures reproducible".
- **S3 versioning + manifest** — for large reference datasets, store a SHA256 manifest of expected files in the repo, verify on pipeline start.
- **OSF / Zenodo / Figshare** — for published datasets that need a DOI. Zenodo's GitHub integration auto-archives repo snapshots; cite the release DOI, not the repo URL.

**Rule**: every data file referenced by code must have a recorded content hash and a recorded provenance (URL or generating command).

### Layer 3 — Workflow Orchestration

Plain shell scripts fail two months later because nobody can remember the run order. A workflow engine gives you a DAG, partial reruns, parallelism, and reproducibility built in.

| Engine | Best for | Strengths | Weaknesses |
|---|---|---|---|
| **Snakemake** | Bioinformatics / general scientific Python | Python syntax, rule-based, conda integration, cluster + cloud profiles, `--use-conda`, `--use-singularity` | Steeper than `make`; rule files can grow gnarly |
| **Nextflow** | Genomics, regulated environments | Dataflow paradigm, native containers, nf-core curated pipelines, strong on AWS/Azure batch | Groovy / Nextflow DSL has its own learning curve |
| **CWL** (Common Workflow Language) | Cross-platform, regulated workflows | Standards-based, portable across engines | Verbose; tooling is functional but spartan |
| **Prefect / Dagster / Airflow** | Production data engineering | Modern UIs, observability, scheduling | Heavyweight for one-person science; designed for ops, not papers |
| **`make`** | Tiny pipelines | Universal, ancient, simple | No conda integration, fragile with whitespace, no clusters |

Pick **Snakemake** if your stack is Python-heavy; **Nextflow** if you're in bioinformatics with multi-language tools and need to run on HPC + cloud + local without changes. Both produce a DAG and a provenance log; both can use the same containers.

**Minimum-viable Snakemake skeleton:**

```python
# Snakefile
configfile: "config.yaml"

rule all:
    input: "results/figure.pdf"

rule preprocess:
    input: "data/raw/{sample}.fastq.gz"
    output: "results/clean/{sample}.fastq.gz"
    conda: "envs/preprocess.yaml"
    threads: 4
    shell: "fastp -i {input} -o {output} -w {threads}"

rule align:
    input: "results/clean/{sample}.fastq.gz"
    output: "results/aligned/{sample}.bam"
    conda: "envs/align.yaml"
    threads: 8
    shell: "bwa mem -t {threads} {config[reference]} {input} | samtools sort -o {output}"

rule plot:
    input: expand("results/aligned/{sample}.bam", sample=config["samples"])
    output: "results/figure.pdf"
    conda: "envs/plot.yaml"
    script: "scripts/plot.py"
```

Run on a SLURM cluster: `snakemake --profile slurm --jobs 100 --use-conda`.

### Layer 4 — Deterministic Randomness

Anything stochastic must be seeded explicitly *at every entry point*:

```python
import numpy as np
import random
import torch  # if relevant

SEED = 20260519
random.seed(SEED)
np.random.seed(SEED)
# Modern numpy: use Generator instead of legacy global
rng = np.random.default_rng(SEED)

# PyTorch
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)
torch.backends.cudnn.deterministic = True   # may slow training
torch.backends.cudnn.benchmark = False

# scikit-learn estimators: pass random_state=SEED explicitly
```

**Gotchas:**

- `numpy.random.seed()` is the *legacy* API. Prefer `default_rng(seed)` for new code; it's faster and statistically better.
- GPU determinism is best-effort. CUDA non-determinism in atomic reductions can leak in via cuDNN, NCCL, and some optimised kernels. For full bit-equivalence on GPU, use `torch.use_deterministic_algorithms(True)` and accept the speed hit, or rerun training $k$ times and report distribution.
- Parallel workers (DataLoader, joblib) need their own per-worker seeds derived from the master seed.
- Operating-system entropy (UUIDs, tempfile names) is not seeded by user RNG. If you need fully deterministic temp paths, set them explicitly.

## Notebooks: Use with Discipline

Jupyter / RMarkdown / Quarto are research tools; they are also reproducibility minefields. The pathology: cells executed out of order, hidden state, "Restart & Run All" never tested.

**Rules:**

1. **Execute top-to-bottom before committing.** Use `nbqa`, `nbstripout`, or a CI check that runs `jupyter nbconvert --to notebook --execute` and fails if any cell errors.
2. **Strip outputs from git** (`nbstripout` as a pre-commit hook) or use `jupytext` to mirror as `.py` / `.qmd` for clean diffs.
3. **Promote logic out of notebooks.** Notebooks are for narrative, not for library functions. Anything reused twice → move to `src/` and import.
4. **Quarto** is the modern choice for research notebooks that compile to HTML/PDF — first-class support for Python, R, Julia, and Observable; cached executions; cross-format output.

## Provenance Capture (Free, Automatic)

Every result file should be traceable to: code commit hash, container/env hash, input data hash, command line, wall-clock time, machine name. Most workflow engines do this automatically. Manually:

```python
# scripts/_provenance.py
import subprocess, hashlib, json, platform, sys
from datetime import datetime, timezone

def capture_provenance(output_dir):
    info = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
        "git_dirty": subprocess.call(["git", "diff-index", "--quiet", "HEAD"]) != 0,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.node(),
        "argv": sys.argv,
    }
    (output_dir / "provenance.json").write_text(json.dumps(info, indent=2))
```

Always: refuse to run if `git_dirty == True` and `--allow-dirty` flag not passed.

## Open Repositories and Preregistration

- **Preregistration**: OSF (osf.io), AsPredicted (aspredicted.org), clinicaltrials.gov, EU Clinical Trials Register, AEA RCT Registry for economics. Frozen primary analysis prevents the [[relatedTo::Common Statistical Pitfalls|garden of forking paths]].
- **Data deposition**: GEO/SRA (genomics), PRIDE (proteomics), MetaboLights (metabolomics), PDB (structural bio), CMIP (climate), ICTV (virus taxonomy), Zenodo (general).
- **Code archiving**: GitHub release → Zenodo via GitHub-Zenodo integration, get a DOI you can cite. Software Heritage gives a persistent identifier even without a release.
- **Preprints**: arXiv, bioRxiv, medRxiv, ChemRxiv, EarthArXiv, EngrXiv, SocArXiv. Cite the version that you actually relied on (DOI + version number).

## End-to-End Skeleton

A reproducible project layout that satisfies all four layers:

```
project/
├── pixi.toml              # env spec
├── pixi.lock              # env lockfile (commit!)
├── Snakefile              # workflow
├── config.yaml
├── envs/                  # per-rule conda envs
├── data/
│   ├── raw/.gitignore     # not in git; managed by DVC or pooch
│   └── data.dvc           # DVC pointer with hashes
├── src/                   # importable library code
├── scripts/               # pipeline entry points
├── results/.gitignore     # generated; reproducible from src + data
├── notebooks/             # narrative, stripped on commit
├── tests/                 # pytest; tests data fixtures via pooch hashes
├── .pre-commit-config.yaml
├── README.md              # one-paragraph + `pixi run snakemake all`
└── CITATION.cff           # how to cite this project
```

Plus: `pre-commit` hooks for `nbstripout`, `ruff`, dirty-git check.

## References

- Wilson et al. (2014): "Best practices for scientific computing". *PLOS Biology* 12(1):e1001745.
- Wilson et al. (2017): "Good enough practices in scientific computing". *PLOS Computational Biology* 13(6):e1005510.
- Mölder et al. (2021): "Sustainable data analysis with Snakemake". *F1000Research* 10:33.
- Di Tommaso et al. (2017): Nextflow. *Nature Biotechnology* 35:316-319.
- Stodden et al. (2016): "Enhancing reproducibility for computational methods". *Science* 354(6317):1240-1241.
- The Turing Way community: *The Turing Way* (continuously updated handbook on reproducible research). https://the-turing-way.netlify.app/

[[relatedTo::Common Statistical Pitfalls]]
[[relatedTo::Scientific Python Stack 2026]]
