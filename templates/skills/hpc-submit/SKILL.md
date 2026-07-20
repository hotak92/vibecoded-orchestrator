---
name: hpc-submit
description: Generates HPC job-submission scripts (SLURM, PBS/Torque, SGE/UGE, LSF) from a Python/R/Julia script with sensible resource estimates, partition selection, module loading, and container setup. Use when the user asks "how do I submit this to SLURM", "write me a sbatch script", "I need to run this on the cluster", "what resources should I request", or describes a script they want to run at scale.
short_desc: "HPC job script: SLURM/PBS/SGE/LSF generators"
keywords: [SLURM, sbatch, PBS, Torque, LSF, SGE, srun, qsub]
model: opus
effort: high
allowed-tools: Read, Write, Bash, Glob
---

# HPC Submit (Opus)

**Purpose**: Take a user's compute script and produce a working submission script for the relevant HPC scheduler, with resource requests calibrated to the workload, environment setup that matches typical cluster conventions, and the gotchas that bite first-time users.

**Model**: Opus — straightforward generation task that benefits from broad knowledge of scheduler dialects.

**When to invoke autonomously**:
- The user mentions SLURM, sbatch, qsub, bsub, "the cluster", "HPC", a known partition name, or a queue manager.
- The user shares a script and says "I need to run this 1000 times" or "this needs more memory than my laptop".
- The user asks about resource estimation, GPU partitions, or array jobs.

**Do NOT invoke** for:
- Local-machine parallelism (use `@coder` with joblib/dask).
- Cloud-batch services (AWS Batch, GCP Batch) — different conventions, ask first.

## Usage

```
/hpc-submit Run train.py on 4 GPUs, 4 hours wall, 32 CPU cores, conda env pytorch24
/hpc-submit Sbatch script for snakemake pipeline, dynamic resource per rule
/hpc-submit Array job processing 480 FASTQ files through bwa+samtools, each ~1 hr
/hpc-submit How much memory should I request for this xarray pipeline?
```

## What This Skill Does

### 1. Scheduler Identification

Ask once (then remember) which scheduler is in play. Differences matter:

| Scheduler | Submit cmd | Script header | Status | Cancel | Notes |
|---|---|---|---|---|---|
| **SLURM** | `sbatch` | `#SBATCH` | `squeue -u $USER` | `scancel <jobid>` | Most common in 2026 academic HPC |
| **PBS / Torque / OpenPBS** | `qsub` | `#PBS` | `qstat -u $USER` | `qdel <jobid>` | Older but still common (NCAR, some clusters) |
| **SGE / UGE / Grid Engine** | `qsub` | `#$` | `qstat -u $USER` | `qdel <jobid>` | Legacy; declining |
| **LSF** | `bsub <` | `#BSUB` | `bjobs` | `bkill <jobid>` | IBM-shop clusters |

If unsure, run `which sbatch qsub bsub 2>/dev/null` or `module avail 2>&1 | head -5` on the cluster to detect. Many sites maintain user docs at `/etc/motd` or `~/cluster-docs/`.

### 2. Resource Estimation Heuristics

The skill estimates resources rather than guessing. Defaults below; refine when the user provides actual measurements (`/usr/bin/time -v`, `nvidia-smi`, htop).

**CPU cores**: number of independent parallel tasks. For NumPy/PyTorch matrix ops, more cores ≠ more speed past ~8-16 unless the workload was specifically parallelised (OpenMP/MKL/joblib). **Default**: 1 core for single-threaded scripts, 8 for vectorised numerical, 16 for multi-process pipelines. Set `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS` to match `--cpus-per-task` to prevent thread thrashing.

**Memory**: peak RSS of the process. For Python with pandas/numpy/scikit-learn:
- Tabular data: peak memory ≈ 3-5× the input file size (for parsing + working copies).
- DataFrame `pd.read_csv` of $X$ GB → request $4X$ + 4 GB headroom.
- xarray/zarr with `chunks` set: memory is bounded by chunk size × number of workers.
- PyTorch training: model parameters in float32 → 4 bytes/param; plus optimizer state (Adam: 8 bytes/param), plus activations (workload-dependent, often dominant). Mixed-precision halves this for the forward pass but optimizer state is still float32.
- If unsure, ask the user to run `/usr/bin/time -v python script.py` locally on a subset and report "Maximum resident set size".

**Wall time**: estimate as 2-5× a small-scale benchmark, plus 10% safety margin. Most schedulers kill jobs at the wall limit with no warning; mid-job checkpointing is mandatory for long jobs.

**GPUs**: request as `--gres=gpu:N` (SLURM) or scheduler-specific. Match GPU model to needs (A100/H100/A6000/etc.). Per-GPU memory: list the typical VRAM in the cluster's docs. For inference, often 1 GPU suffices; for distributed training, request 2-8 on one node and use NCCL (or 16-64 across nodes with appropriate scheduler topology hints).

### 3. SLURM Template (Most Common)

```bash
#!/bin/bash
#SBATCH --job-name=myrun
#SBATCH --output=logs/%x_%j.out           # %x=jobname, %j=jobid
#SBATCH --error=logs/%x_%j.err
#SBATCH --time=04:00:00                   # HH:MM:SS or D-HH:MM
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G                         # per node; or --mem-per-cpu=4G
#SBATCH --partition=gpu                   # cluster-specific
#SBATCH --gres=gpu:a100:1                 # 1 A100 GPU; check cluster syntax
#SBATCH --mail-user=user@example.edu
#SBATCH --mail-type=END,FAIL
# Optional: account / QoS / nodelist if the site requires them
# #SBATCH --account=lab_pi
# #SBATCH --qos=normal

set -euo pipefail
mkdir -p logs

echo "Job $SLURM_JOB_ID started on $(hostname) at $(date)"
echo "Working directory: $(pwd)"
echo "SLURM_JOB_NODELIST=$SLURM_JOB_NODELIST"

# --- Environment setup ---
# Prefer pixi / conda / module — pick one and stick to it
module purge
module load cuda/12.4 cudnn/9.0          # cluster-specific names
source ~/miniforge3/etc/profile.d/conda.sh
conda activate pytorch24

# Pin thread counts to requested CPUs to avoid oversubscription
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
export OPENBLAS_NUM_THREADS=$SLURM_CPUS_PER_TASK

# --- Reproducibility ---
export PYTHONHASHSEED=0
export CUBLAS_WORKSPACE_CONFIG=:4096:8     # for deterministic cuDNN

# --- Run ---
srun --unbuffered python train.py \
    --config configs/run.yaml \
    --output-dir /scratch/$USER/$SLURM_JOB_ID

echo "Job $SLURM_JOB_ID finished at $(date)"
```

**Notes the skill emphasises**:
- `set -euo pipefail` — fail fast on errors, undefined vars, pipe failures.
- `srun` inside `sbatch` ensures correct task accounting and proper signal propagation on timeout.
- `--unbuffered` (or `python -u`) so stdout reaches log files in real time.
- Use `$TMPDIR` or `/scratch/$USER/$SLURM_JOB_ID` for fast local I/O; tmpfs is purged when job ends.
- Stage data from network filesystem to local scratch at job start, copy results back at end.
- For Python: avoid `python script.py` direct invocation in scripts where you might be sourcing from a different env later — use `$(which python) script.py` or the full path.

### 4. SLURM Array Jobs (Embarrassingly Parallel)

For processing 1000 inputs, do **not** submit 1000 jobs — submit one array of 1000 tasks. Schedulers handle this efficiently.

```bash
#SBATCH --job-name=align
#SBATCH --array=0-479%50                 # 480 tasks, max 50 concurrent
#SBATCH --time=01:30:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G

# Read the input for this task from a manifest file
SAMPLE=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" samples.txt)
echo "Task $SLURM_ARRAY_TASK_ID processing $SAMPLE"

bwa mem -t $SLURM_CPUS_PER_TASK ref.fa fastq/${SAMPLE}_R1.fq.gz fastq/${SAMPLE}_R2.fq.gz \
    | samtools sort -@ $SLURM_CPUS_PER_TASK -o aligned/${SAMPLE}.bam -
```

Use `%N` (e.g. `%50`) to throttle concurrency — full burst of 1000 simultaneous jobs can hammer shared filesystems and earn complaints from sysadmins.

### 5. PBS / Torque

```bash
#!/bin/bash
#PBS -N myrun
#PBS -l select=1:ncpus=8:mem=32gb:ngpus=1
#PBS -l walltime=04:00:00
#PBS -q gpu
#PBS -o logs/myrun.out
#PBS -e logs/myrun.err
#PBS -m abe -M user@example.edu

cd $PBS_O_WORKDIR
# ... rest as SLURM
```

Array: `#PBS -J 0-479` and `$PBS_ARRAY_INDEX`.

### 6. SGE / UGE

```bash
#!/bin/bash
#$ -N myrun
#$ -cwd
#$ -pe smp 8                              # parallel environment + slots
#$ -l h_rt=04:00:00
#$ -l mem_free=32G
#$ -q gpu.q
#$ -o logs/myrun.out
#$ -e logs/myrun.err
#$ -M user@example.edu -m beas
```

Array: `#$ -t 1-480` and `$SGE_TASK_ID` (1-indexed).

### 7. LSF

```bash
#!/bin/bash
#BSUB -J myrun
#BSUB -n 8                                # cores
#BSUB -R "rusage[mem=32G]"
#BSUB -W 04:00                            # HH:MM
#BSUB -q gpu
#BSUB -gpu "num=1"
#BSUB -o logs/myrun.%J.out
#BSUB -e logs/myrun.%J.err
```

### 8. Snakemake / Nextflow on HPC

Both have first-class scheduler profiles — generate a config rather than wrapping the engine in `sbatch`.

**Snakemake** — `--profile slurm` reads from `~/.config/snakemake/slurm/config.yaml`:

```yaml
cluster:
  mkdir -p logs/{rule} &&
  sbatch
    --partition={resources.partition}
    --cpus-per-task={threads}
    --mem={resources.mem_mb}
    --time={resources.runtime}
    --job-name={rule}
    --output=logs/{rule}/{wildcards}.out
default-resources:
  - partition=cpu
  - mem_mb=4000
  - runtime=60
restart-times: 1
max-jobs-per-second: 10
max-status-checks-per-second: 1
jobs: 100
keep-going: true
rerun-incomplete: true
use-conda: true
```

Then: `snakemake --profile slurm`.

**Nextflow** — `nextflow.config`:

```groovy
process {
    executor = 'slurm'
    queue = 'cpu'
    cpus = 8
    memory = '32 GB'
    time = '4h'

    withName: 'ALIGN' {
        queue = 'gpu'
        accelerator = 1
        memory = '64 GB'
    }
}

executor {
    queueSize = 100
    submitRateLimit = '10 sec'
}
```

Then: `nextflow run main.nf -profile slurm`.

### 9. Containers (Apptainer / Singularity)

For environment portability and reproducibility, prefer containers over module-loading on the host:

```bash
#SBATCH ... (resource lines as before)

module load apptainer                    # or singularity on older sites

apptainer exec --nv \
    --bind /scratch/$USER:/scratch \
    --bind /data:/data:ro \
    /shared/containers/pytorch_24.10.sif \
    python train.py --output /scratch/$SLURM_JOB_ID
```

`--nv` exposes NVIDIA GPUs. `--bind` mounts host paths into the container.

### 10. Common Mistakes the Skill Catches

| Mistake | Symptom | Fix |
|---|---|---|
| Requesting more memory than the node has | Job pends forever | Check `sinfo -o "%P %m %c %G"` for partition specs |
| Wall time too short | Job killed mid-run | Estimate from a small benchmark × 3 |
| Wall time too long | Long queue wait | Most schedulers prefer accurate estimates; partitions often penalise oversized requests |
| Forgetting `srun` inside `sbatch` for distributed jobs | Tasks share one rank, no parallelism | Wrap with `srun` |
| Not pinning `OMP_NUM_THREADS` | Thread oversubscription, slower than serial | Set to `$SLURM_CPUS_PER_TASK` |
| Writing to home from many tasks | Filesystem hangs | Use node-local `$TMPDIR` or scratch |
| Running 1000 separate jobs instead of an array | Hits per-user job limit | Use `--array` |
| `module load` after `conda activate` | Module wipes path | Always `module` before `conda activate` |
| Hardcoded paths in scripts | Breaks on the cluster | Use `$HOME`, `$SCRATCH`, `$SLURM_SUBMIT_DIR` |
| Forgetting to redirect stderr | Logs missing errors | `#SBATCH --error=...` or combine via `--output` |

## Output Format

```markdown
## HPC Submission Script

**Scheduler detected**: [SLURM | PBS | SGE | LSF | UNKNOWN — please confirm]
**Cluster docs** (if known): [link]

### Resource estimate

| Resource | Requested | Rationale |
|---|---|---|
| Cores | [N] | [why] |
| Memory | [X GB] | [why — based on data size, model size, etc.] |
| Wall time | [HH:MM] | [why — estimate from benchmark or model] |
| GPUs | [type × N] | [if applicable] |
| Partition / queue | [name] | [why] |

### Submission script

[language: bash]
[full script with comments]

### How to submit

```bash
mkdir -p logs
sbatch submit.sh
squeue -u $USER                    # check status
```

### Notes / gotchas

- [scheduler-specific notes]
- [environment-setup notes]
- [common-failure-mode notes]

### Iteration tips

- Start with a 30-minute test run on a small input subset.
- Monitor real resource use: `seff <jobid>` (SLURM) or `qstat -f <jobid>` (PBS).
- Adjust the script down to actual usage + 20% headroom.

### If you don't know the partition/queue names

Run on the cluster: `sinfo -o "%P %m %c %G"` (SLURM) or `qstat -Q` (PBS) or `bqueues` (LSF).
```

## Hard Rules

1. **Always include `mkdir -p logs`** before `sbatch` — most schedulers fail silently if the log directory doesn't exist.
2. **Always pin `OMP_NUM_THREADS`** to `$SLURM_CPUS_PER_TASK` (or equivalent) for any numpy/scipy/torch workload.
3. **Always use `set -euo pipefail`** in the submission script body.
4. **Always log job ID and hostname** at start; date at start and end.
5. **Never recommend running 100+ jobs without an array** — it's bad citizenship and often hits per-user limits.
6. **Always recommend a 5-30 minute test run before full submission** for any pipeline the user hasn't used before.
7. **Refuse to guess at site-specific things** like partition names, account codes, GPU types — ask, or insert `# TODO: confirm with cluster docs` markers.

## Integration with Knowledge Graph

Leans on [[Reproducible Research Workflows]] for the broader context of pipelines + environments + workflow engines. After helping a user, if the cluster has unusual quirks worth recording, write a per-project KG node `knowledge/concepts/hpc-<cluster-name>.md` capturing partition names, module conventions, scratch paths, and filesystem etiquette.

## Success Criteria

- Script syntactically correct for the scheduler.
- Resource requests are estimates, not random numbers, with rationale stated.
- Environment setup is explicit and reproducible.
- Thread counts are pinned to requested CPUs.
- Local scratch is used for I/O-heavy steps.
- The user can submit, monitor, cancel, and read logs with the commands provided.
- Common gotchas relevant to their workflow are called out.
