# Default code-embedding image — multi-arch CPU PyTorch base.
#
# v0.2.50 portability fix (audit F1, 2026-06-08): the prior version of this
# file used `pytorch:2.6.0-cuda12.6-cudnn9-runtime` unconditionally, which:
#   - Cannot pull on Apple Silicon (no linux/arm64 manifest for CUDA-tagged
#     PyTorch images).
#   - Carries ~2 GB of CUDA libraries that CPU-only users pay for in disk.
#   - Cannot be used on AMD ROCm (no CUDA runtime).
#
# This default image (`Dockerfile`) is now CPU-only and multi-arch
# (linux/amd64 + linux/arm64). NVIDIA hosts opt into the CUDA build by
# setting CODE_EMBED_DOCKERFILE=Dockerfile.cuda in their `.env` —
# install.py writes that variable automatically when nvidia-smi detection
# succeeds. See `infrastructure/docker-compose.yml::code_embed.build.dockerfile`.
#
# The runtime path in server.py already does
# `device = "cuda" if torch.cuda.is_available() else "cpu"`, so the
# CPU base image runs correctly even on a host that happens to have an
# NVIDIA GPU (it just won't use it — the user must opt into the CUDA
# image variant to get GPU acceleration).
FROM docker.io/pytorch/pytorch:2.6.0-cpu

WORKDIR /app

# Install Python dependencies (sentence-transformers pulls torch but base image has it)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Redirect ALL HuggingFace state (model weights + modules cache) to mounted volume.
# This ensures custom model code (trust_remote_code=True) is also cached persistently.
ENV HF_HOME=/cache

EXPOSE 11440

CMD ["python", "server.py"]
