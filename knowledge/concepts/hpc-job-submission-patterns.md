---
title: HPC Job Submission Patterns
type: concept
tags: [hpc, slurm, pbs, sge, lsf, scheduler, scientific-computing, reproducibility, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: 2026-05-22T00:00:00Z
status: archived
---

# HPC Job Submission Patterns

## Overview

Most academic and national-lab clusters in 2026 run **SLURM**; older sites still use **PBS/Torque/OpenPBS**, legacy installations use **SGE/UGE/Grid Engine**, and IBM shops use **LSF**. The patterns below cover the common cases: single-node CPU/GPU jobs, embarrassingly parallel array jobs, scheduler-aware workflow engines (Snakemake, Nextflow), and containerised reproducible runs. For broader reproducibility context see [[Reproducible Research Workflows]].

## Scheduler Cheat Sheet

| Scheduler | Submit | Header | Status | Cancel | Notes |
|---|---|---|---|---|---|
| **SLURM** | `sbatch` | `#SBATCH` | `squeue -u $USER` | `scancel <jobid>` | Most common in 2026 academic HPC |
| **PBS / Torque / OpenPBS** | `qsub` | `#PBS` | `qstat -u $USER` | `qdel <jobid>` | Older; still at some national labs (NCAR) |
| **SGE / UGE / Grid Engine** | `qsub` | `#$` | `qstat -u $USER` | `qdel <jobid>` | Legacy; declining |
| **LSF** | `bsub <` | `#BSUB` | `bjobs` | `bkill <jobid>` | IBM-shop clusters |

Detect on the cluster with `which sbatch qsub bsub 2>/dev/null` or `module avail 2>&1 | head -5`. Site-specific quirks (partition names, account codes, GPU types) live in `/etc/motd`, the site documentation portal, or `~/cluster-docs/`.

## Resource Estimation Heuristics

Estimate, don't guess. When the user has actual measurements (`/usr/bin/time -v`, `nvidia-smi`, htop), prefer those.

**CPU cores**: number of independent parallel tasks. For NumPy / PyTorch matrix ops, more cores ≠ more speed past ~8-16 unless the workload was specifically parallelised (OpenMP / MKL / joblib). Defaults: 1 core for single-threaded scripts, 8 for vectorised numerical, 16 for multi-process pipelines.

**Memory**:
- Tabular data (pandas / numpy): peak memory ≈ 3-5× input file size.
- `pd.read_csv` of $X$ GB → request $4X + 4$ GB headroom.
- xarray + zarr with `chunks` set: bounded by chunk size × workers.
- PyTorch training: 4 bytes/param (fp32) + 8 bytes/param (Adam optimiser state) + activations (workload-dependent, often dominant).
- If unsure, ask the user to run `/usr/bin/time -v python script.py` locally on a subset and report "Maximum resident set size".

**Wall time**: estimate as 2-5× a small-scale benchmark + 10% safety margin. Schedulers kill jobs at the wall limit with no warning; mid-job checkpointing is mandatory for long jobs.

**GPUs**: request with `--gres=gpu:N` (SLURM) or scheduler-specific. Match GPU model to needs (A100 / H100 / A6000); per-GPU VRAM is in cluster docs.

## SLURM Template (Most Common)

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

set -euo pipefail
mkdir -p logs

echo "Job $SLURM_JOB_ID started on $(hostname) at $(date)"
echo "SLURM_JOB_NODELIST=$SLURM_JOB_NODELIST"

# --- Environment setup ---
module purge
module load cuda/12.4 cudnn/9.0          # cluster-specific names
source ~/miniforge3/etc/profile.d/conda.sh
conda activate pytorch24

# Pin thread counts to requested CPUs (prevents oversubscription)
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

Key points: `set -euo pipefail` to fail fast; `srun` inside `sbatch` for correct task accounting and signal propagation; `--unbuffered` (or `python -u`) so logs reach disk in real time; use `$TMPDIR` or `/scratch/$USER/$SLURM_JOB_ID` for fast local I/O; stage data from network FS to scratch at job start, copy results back at end.

## Array Jobs (Embarrassingly Parallel)

For 1000 inputs, submit one array of 1000 tasks, not 1000 separate jobs.

```bash
#SBATCH --job-name=align
#SBATCH --array=0-479%50                 # 480 tasks, max 50 concurrent
#SBATCH --time=01:30:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G

SAMPLE=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" samples.txt)
bwa mem -t $SLURM_CPUS_PER_TASK ref.fa fastq/${SAMPLE}_R1.fq.gz fastq/${SAMPLE}_R2.fq.gz \
    | samtools sort -@ $SLURM_CPUS_PER_TASK -o aligned/${SAMPLE}.bam -
```

Use `%N` (e.g. `%50`) to throttle concurrency: a full burst of 1000 simultaneous jobs hammers shared filesystems and earns complaints from sysadmins.

PBS: `#PBS -J 0-479` and `$PBS_ARRAY_INDEX`.
SGE: `#$ -t 1-480` and `$SGE_TASK_ID` (1-indexed).
LSF: `#BSUB -J "myrun[1-480]"` and `$LSB_JOBINDEX`.

## Other Schedulers — Minimal Templates

### PBS / Torque

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
# ... environment + run as SLURM template
```

### SGE / UGE

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
```

### LSF

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

## Workflow Engines on HPC

Both Snakemake and Nextflow have first-class scheduler profiles. Use them rather than wrapping the engine in `sbatch`.

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
jobs: 100
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

## Containers (Apptainer / Singularity)

For full environment reproducibility, prefer containers over host module-loading:

```bash
module load apptainer                    # or singularity on older sites

apptainer exec --nv \
    --bind /scratch/$USER:/scratch \
    --bind /data:/data:ro \
    /shared/containers/pytorch_24.10.sif \
    python train.py --output /scratch/$SLURM_JOB_ID
```

`--nv` exposes NVIDIA GPUs. `--bind` mounts host paths into the container. Pin container images by digest, not tag.

## Common Mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Requesting more memory than the node has | Job pends forever | Check `sinfo -o "%P %m %c %G"` for partition specs |
| Wall time too short | Job killed mid-run | Estimate from a small benchmark × 3 |
| Wall time too long | Long queue wait; some partitions penalise oversized requests | Estimate accurately |
| Missing `srun` inside `sbatch` for distributed jobs | Tasks share one rank, no parallelism | Wrap with `srun` |
| Not pinning `OMP_NUM_THREADS` | Thread oversubscription, slower than serial | Set to `$SLURM_CPUS_PER_TASK` |
| Writing to home from many tasks | Filesystem hangs; sysadmin complaints | Use node-local `$TMPDIR` or scratch |
| 1000 separate jobs instead of array | Hits per-user job limit; abuse of scheduler | Use `--array` |
| `module load` after `conda activate` | Module wipes PATH; conda env breaks | Always `module` before `conda activate` |
| Hardcoded paths | Breaks on cluster | Use `$HOME`, `$SCRATCH`, `$SLURM_SUBMIT_DIR` |
| Missing `mkdir -p logs/` | sbatch fails silently | Create log dir before submission |
| Not checkpointing long jobs | Lose all work at wall limit | Checkpoint every 30-60 min |

## Monitoring and Iteration

After submission:

- SLURM: `squeue -u $USER`; post-mortem with `seff <jobid>` (efficiency) and `sacct -j <jobid> -o ReqMem,MaxRSS,ReqCPUS,TotalCPU,Elapsed`.
- PBS: `qstat -f <jobid>`.
- LSF: `bjobs -l <jobid>`.

Adjust the script down to actual usage + 20% headroom on the next run. Over-requesting causes long queue waits; under-requesting causes job kills.

Always run a 5-30 minute test on a small input subset before full submission. Verify outputs, then scale up.

## References

- SLURM documentation: https://slurm.schedmd.com/documentation.html
- Snakemake cluster execution: https://snakemake.readthedocs.io/en/stable/executing/cluster.html
- nf-core: https://nf-co.re/ — curated Nextflow pipelines with HPC profiles.
- Apptainer (formerly Singularity): https://apptainer.org/
- EasyBuild / Spack: https://easybuild.io/ / https://spack.io/ — cluster software-stack management.

[[relatedTo::Reproducible Research Workflows]]
[[relatedTo::Scientific Python Stack 2026]]
[[relatedTo::Diagnosing Non-Deterministic Results]]
