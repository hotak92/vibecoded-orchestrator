---
name: repro-audit
description: Audits a scientific project for reproducibility — environment pinning, seed setting, data hashing, notebook discipline, workflow orchestration, and provenance capture. Use when the user asks "is this project reproducible", "what's missing for a reviewer to rerun this", "audit my repo before submission", "why do I get different results each run", or before submitting code with a manuscript.
keywords: [reproducibility, reproducible, seed setting, environment pinning, manuscript submission, provenance capture, preregistration]
model: opus
effort: high
allowed-tools: Read, Write, Bash, Glob, Grep
---

# Repro Audit (Opus)

**Purpose**: Walk a project repository against the four-layer reproducibility checklist (environment pinning, data versioning, workflow orchestration, deterministic randomness) and produce a specific, actionable remediation list. Designed to be run before manuscript submission, before sharing code with collaborators, or after the first "I can't reproduce your result" email.

**Model**: Opus 4.7 — broad knowledge of ecosystems (Python, R, Julia, conda, container) and ability to read code at scale.

**When to invoke autonomously**:
- The user mentions "I'm about to submit", "share with reviewers", "reproducibility", or "rerun".
- The user reports non-deterministic behaviour: "I get different numbers each time".
- The user has a Jupyter notebook with no environment file.
- The user is preparing a Zenodo deposit or DOI for code.

**Do NOT invoke** for:
- New projects with no code yet (use the [[Reproducible Research Workflows]] template instead).
- Pure-statistics issues (use `/stats-consult`).
- Code review for correctness (use `@code-review-expert`).

## Usage

```
/repro-audit Audit this repo before I submit to bioRxiv
/repro-audit Why do I get different UMAP coordinates every time I rerun?
/repro-audit Check if a reviewer could rerun my analysis from this folder
/repro-audit Is my notebook pipeline reproducible?
```

## What This Skill Does

### 1. Inventory Pass

First, characterise the project. Run (or ask the user to run):

```bash
ls -la                                    # top-level layout
find . -maxdepth 2 -name '*.py' -o -name '*.R' -o -name '*.jl' -o -name '*.ipynb' | head -50
find . -maxdepth 2 -name 'requirements*.txt' -o -name 'environment.yml' -o -name 'pixi.toml' \
       -o -name 'pyproject.toml' -o -name 'uv.lock' -o -name 'renv.lock' \
       -o -name 'Project.toml' -o -name 'Manifest.toml' -o -name 'Dockerfile' -o -name 'apptainer.def'
git status                                # is there uncommitted state?
git log -5 --oneline                      # commit history?
cat README* 2>/dev/null | head -40
```

### 2. Four-Layer Checklist

The skill runs through each layer and reports findings as PASS / WARN / FAIL.

#### Layer 1 — Environment Pinning

| Check | Pass criterion | Common failure |
|---|---|---|
| Lockfile present | `uv.lock`, `pixi.lock`, `renv.lock`, `Manifest.toml`, or container digest | Only a loose `requirements.txt` from `pip freeze` |
| Lockfile is current | Generated date < 30 days, or matches the latest commit touching deps | Outdated lockfile from before the last refactor |
| Platform info captured | Lockfile includes platform tags (`linux-64`, `osx-arm64`, etc.) | Single-platform lockfile shared with cross-platform collaborators |
| Python version pinned | Major + minor specified | `python>=3` or no constraint |
| System deps documented | `apt`/`brew`/`conda` dependencies listed (compilers, BLAS, CUDA) | Missing system libraries; "but it worked on my machine" |
| Container image (if used) | Pinned by digest, not tag (`@sha256:...`) | `FROM ubuntu:latest` — moving target |
| Activation reproducible | `README` shows the exact env-creation command | "Install the usual stuff" |

#### Layer 2 — Data Versioning

| Check | Pass criterion | Common failure |
|---|---|---|
| Raw data not in git | `.gitignore`'d, with documented external location | Multi-GB blobs committed; git LFS used but untracked |
| Data hashes recorded | SHA256 of input files in repo (via `pooch`, DVC, or manifest file) | "Download from the GEO link" — no integrity check |
| Provenance per input | URL or generating command recorded for each file | Mystery files with no origin |
| Data path configuration | Paths in a config file, not hardcoded | `/home/scientist/My Documents/data.csv` in source |
| Intermediate outputs reproducible | Pipeline can regenerate them from raw data | Outputs committed but not regeneratable |
| Sensitive data handled | PHI / restricted access documented separately, never committed | Patient identifiers in CSVs |

#### Layer 3 — Workflow Orchestration

| Check | Pass criterion | Common failure |
|---|---|---|
| Run order is documented | Workflow engine OR a shell `run_all.sh` OR a documented order in README | "Run notebook 1, then 3, then 2, then 5" — fragile |
| Workflow is deterministic | Snakemake DAG, Nextflow workflow, or equivalent | Series of bash scripts where order of side effects matters |
| Notebooks execute top-to-bottom | Last full-execute is committed; `nbconvert --execute` succeeds in CI | Hidden-state cells; non-monotonic execution counts |
| Notebooks stripped or cached | `nbstripout` pre-commit OR jupytext mirror OR Quarto | Outputs in git with random metadata diffs |
| CI runs the pipeline | At least a smoke test of the full workflow in GitHub Actions / GitLab CI | No CI; "trust me, it works" |
| Tests exist | Unit tests for key library functions; fixture data with hashes | Zero tests |

#### Layer 4 — Deterministic Randomness

| Check | Pass criterion | Common failure |
|---|---|---|
| Master seed defined at entry point | `SEED = ...` set explicitly | Implicit `np.random` calls; no seed |
| All RNG libraries seeded | `random`, `numpy`, `torch` (CPU and CUDA), `tensorflow`, `jax` | One seeded, others not |
| `numpy` uses `Generator` not legacy global | `rng = np.random.default_rng(SEED)` | `np.random.seed(SEED)` everywhere (legacy) |
| Estimators receive `random_state` | Every sklearn estimator with stochasticity has `random_state=SEED` | Implicit randomness in `train_test_split`, `KMeans`, `UMAP`, etc. |
| GPU determinism (if applicable) | `torch.use_deterministic_algorithms(True)` and `cuDNN` flags | Best-effort drift accepted but not documented |
| Parallel-worker seeding | Each `DataLoader` worker / joblib job gets its own seed derived from master | Workers all use the same or no seed |
| UMAP / t-SNE / clustering reproducibility | Numerical methods are deterministic with seed + thread count fixed | UMAP without `n_jobs=1`; t-SNE with random init each time |
| Re-run yields identical output | Two independent runs produce byte-identical (or numerically-close) outputs | Different UMAP coords each time |

### 3. Notebook-Specific Audit

Notebooks are the dominant reproducibility hazard. Specific checks:

- Run `jupyter nbconvert --to notebook --execute --inplace notebooks/*.ipynb` (or `jupytext --execute`). If any cell errors, FAIL.
- Inspect execution counts: should be monotonically increasing 1, 2, 3, ... within a single notebook. Out-of-order counts → cells have been re-run out of sequence.
- Check for `display(...)` / `print(...)` of pandas dataframes whose row order depends on hash randomisation: set `PYTHONHASHSEED=0` or sort explicitly.
- Look for time-dependent calls: `datetime.now()`, `time.time()` baked into output filenames or seeds.
- Look for environment leakage: `os.environ['SOMETHING']` referenced but not documented.
- Promote anything used >1× into `src/` as importable functions.

### 4. Provenance and Citation

| Check | Pass criterion |
|---|---|
| `README.md` with one-line "how to run" | Yes, including env setup |
| `CITATION.cff` or `CITATION` file | Yes, with authors + version |
| License file | OSI-approved license (MIT, Apache-2.0, BSD-3-Clause, GPL family) |
| Code archived | GitHub release → Zenodo DOI for the version cited in the manuscript |
| Software Heritage ID | Optional but recommended for long-term archival |
| `pyproject.toml` `version` matches the release tag | Yes |

### 5. Reviewer-Eye Smoke Test

After the audit, simulate the reviewer experience:

1. Fresh checkout of the public release tag.
2. Create the documented environment.
3. Run the documented "reproduce" command (one-liner per the README).
4. Compare outputs to manuscript figures/tables.

If any of steps 1-4 fails, the project is not reproducible. The audit's deliverable is the list of concrete fixes to make all four steps succeed.

## Output Format

```markdown
## Reproducibility Audit — [Project Name]

**Date**: [yyyy-mm-dd]
**Repo**: [path or URL]
**Audited commit**: [hash]

### Summary

[Top-line: REPRODUCIBLE / PARTIAL / NOT REPRODUCIBLE]
[1-paragraph plain-language summary]

### Layer 1 — Environment

| Check | Status | Detail |
|---|---|---|
| ... | ✓/⚠/✗ | ... |

**Findings**: [bullet list of specific gaps]

**Fixes**:
1. [specific command/file to add — e.g. "Run `pixi init` and migrate `requirements.txt` to `pixi.toml` with version pins"]
2. ...

### Layer 2 — Data
[same structure]

### Layer 3 — Workflow
[same structure]

### Layer 4 — Randomness
[same structure]

### Notebook Audit
- Notebooks found: [list]
- Top-to-bottom execution: [pass/fail per notebook]
- Specific issues: [list]

### Provenance & Citation
[checklist with status]

### Reviewer Smoke Test

Steps to make this reproducible:
1. [highest-impact fix]
2. [next]
3. ...

Estimated effort: [X hours]

### Recommended Final State

After fixes, the README should contain a one-liner that any reviewer can run:

```bash
git clone <repo> && cd <repo>
pixi run reproduce
# OR
docker run --rm -v $PWD/results:/results <image_digest>
```

And running it should regenerate all manuscript figures/tables.

### What's Already Good

[bullet list — credit existing good practice, important for morale]
```

## Diagnosing "Different Results Each Run"

When the user reports drift between runs, the audit asks:

1. **Show the diff.** Are outputs different by floating-point epsilon (~$10^{-12}$), by clustering label permutation (the cluster IDs differ but the partition is the same), or by a large numerical fraction (>1%)?
2. **Floating-point epsilon**: usually thread-count / reduction-order in BLAS. Pin `OMP_NUM_THREADS`. For full determinism on GPU, the operations must be deterministic — `torch.use_deterministic_algorithms(True)`.
3. **Permuted labels**: clustering algorithms don't guarantee label order. Compare via `sklearn.metrics.adjusted_rand_score`, not raw labels.
4. **Large drift**: an RNG isn't seeded. Trace which library produced the output and seed it.
5. **Drift only on a specific input**: numerical instability — a near-singular matrix or division by near-zero. Add a regularisation / `rcond` parameter, or refactor.
6. **Drift across machines**: BLAS vendor (MKL vs OpenBLAS vs Accelerate), or different glibc, or different CUDA version. Use a container.

## Hard Rules

1. **A passing audit produces a one-liner reproduction command.** If it doesn't fit on one line, it's not reproducible by a stranger.
2. **Tag findings with severity** — submission-blocking, reviewer-blocking, polish — so the user knows what to fix first.
3. **Always credit existing good practice.** Audits are demoralising; finding three positives mid-list keeps the user reading.
4. **Never claim the project is reproducible without running the smoke test.** Inferring from the presence of files is not sufficient.
5. **Be specific about fixes.** "Add a lockfile" → "Run `uv lock`; commit `uv.lock`; update README to say `uv sync` then `uv run python script.py`".
6. **Recommend the cheapest sufficient fix first.** Not every project needs Snakemake — a numbered `run_all.sh` is often enough for a small one.

## Integration with Knowledge Graph

Leans on:
- [[Reproducible Research Workflows]] for the canonical layered model.
- [[Scientific Python Stack 2026]] for tool recommendations.
- [[Common Statistical Pitfalls]] for "this analysis isn't reproducible because the path through it wasn't fixed".

After the audit, if the project ships with a domain-specific gotcha worth recording (e.g. "this tool has non-determinism we worked around with X"), save a per-project KG node under `knowledge/concepts/repro-<topic>.md`.

## Success Criteria

- Every check produces ✓/⚠/✗ with a specific, copyable fix when not ✓.
- A prioritised remediation list with effort estimate.
- Reviewer smoke test either runs cleanly or has a numbered list of blockers.
- The user can hand the audit to a collaborator and have them act on it without further questions.
