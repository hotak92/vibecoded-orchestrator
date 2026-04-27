# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Ollama MCP Server for Claude Code

Provides local LLM tools:
- chat: Run inference with local models (FREE)
- read_document: Read, summarize, or extract specific information from files using local models

Date: 2026-03-12
"""

import os
import json
import logging
import asyncio
import platform
import shutil
import subprocess
import requests
from typing import Optional
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Vision capability probe ---------------------------------------------------
#
# `read_image(describe=True)` runs a local vision model via Ollama. On a low-VRAM
# GPU the model OOMs; with no GPU + low RAM there is no way to run it at all.
# We probe available compute *once* at module load and gate the description tier
# accordingly. The image-as-base64 path is unchanged — Claude's own vision still
# works regardless.
#
# Per-model thresholds (q4_K_M quantization assumed — Ollama default). Numbers
# are PRACTICAL floors (weights + KV cache + image features + framebuffer
# headroom), not just file-size. CPU-mode RAM is ~1.5–2× the q4 file size to
# cover weights + KV cache + Python/Ollama overhead.
#
# Sources (measured numbers, not estimates):
#   - knowledge/models/qwen2-5-vl-7b.md           — Qwen2.5-VL-7B q8 ~8 GB → q4 ~4–5 GB
#   - knowledge/models/qwen3-vl-8b.md             — Qwen3-VL-8B  q8 ~9 GB → q4 ~5–6 GB
#   - Ollama library file sizes (ollama.com/library/<model>/tags):
#       * qwen3-vl:8b     ≈ 6.0 GB (q4) → ~7–8 GB VRAM with KV cache
#       * llama3.2-vision:11b ≈ 7.9 GB → ~9–10 GB VRAM practical floor
#       * llama3.2-vision:90b ≈ 55 GB  → ~64+ GB VRAM practical floor
#       * gemma3:4b       ≈ 3.3 GB → ~5 GB VRAM practical floor
#   - r/LocalLLaMA reports (Llama 3.2-Vision 11B q4: 8 GB VRAM card OOMs with
#     full KV cache; 10–12 GB recommended). Hence 8 GB floor here, with the
#     understanding that running it at small image-budget (256×256) is borderline.
VISION_MODEL_REQUIREMENTS = {
    # Ollama tags Martino uses in this repo:
    "qwen3.5:9b":             {"vram_gb": 7.5,  "ram_gb": 12.0,  "file_gb": 6.0},
    "qwen3.5:7b":             {"vram_gb": 6.0,  "ram_gb": 10.0,  "file_gb": 4.7},
    "qwen3.5:4b":             {"vram_gb": 4.0,  "ram_gb": 7.0,   "file_gb": 2.6},
    # Sibling tags users may swap to:
    "qwen3-vl:8b":            {"vram_gb": 7.5,  "ram_gb": 12.0,  "file_gb": 6.0},
    "qwen2.5vl:7b":           {"vram_gb": 6.0,  "ram_gb": 10.0,  "file_gb": 4.7},
    "llama3.2-vision:latest": {"vram_gb": 9.0,  "ram_gb": 16.0,  "file_gb": 7.9},
    "llama3.2-vision:11b":    {"vram_gb": 9.0,  "ram_gb": 16.0,  "file_gb": 7.9},
    "llama3.2-vision:90b":    {"vram_gb": 64.0, "ram_gb": 110.0, "file_gb": 55.0},
    "gemma3:4b":              {"vram_gb": 5.0,  "ram_gb": 8.0,   "file_gb": 3.3},
    "gemma4:e4b":             {"vram_gb": 5.0,  "ram_gb": 8.0,   "file_gb": 3.3},
}
# Default threshold for unknown models (conservative — assume 8B-ish multimodal).
_DEFAULT_VISION_REQUIREMENTS = {"vram_gb": 7.5, "ram_gb": 14.0, "file_gb": 6.0}


def _resize_budget_pixels(capability: dict, model: str) -> int:
    """
    Total-pixel cap for read_image based on available memory and the model's
    footprint. Image features dominate VRAM during inference (per-tile encoder
    activations + cross-attention KV); halving pixel area roughly halves that
    overhead.

    GPU tiers — VRAM headroom OVER the model footprint determines image budget:
        ≥12 GB free         → 1024×1024  (1,048,576 px)
        ≥ 8 GB free         →  ~720×720  (  524,288 px)
        ≥ 6 GB free         →  512×512   (  262,144 px)
        otherwise (tight)   →  256×256   (   65,536 px)
    CPU tiers — RAM determines what we can chew without paging:
        ≥32 GB              → 1024×1024
        ≥16 GB              →  ~720×720
        ≥ 8 GB              →  512×512
        otherwise           →  256×256
    """
    vram_gb = capability.get("vram_gb") or 0
    ram_gb = capability.get("ram_gb") or 0

    if capability.get("preferred_backend") == "gpu":
        if vram_gb >= 12:
            return 1_048_576
        if vram_gb >= 8:
            return 524_288
        if vram_gb >= 6:
            return 262_144
        return 65_536
    # CPU fallback (or "none" — caller will skip; budget is still fine to compute)
    if ram_gb >= 32:
        return 1_048_576
    if ram_gb >= 16:
        return 524_288
    if ram_gb >= 8:
        return 262_144
    return 65_536


def _detect_total_ram_gb() -> float:
    """Return total system RAM in GB. Tries psutil → OS-specific fallbacks → 8.0."""
    # Preferred: psutil (already a hard dep via requirements.txt)
    try:
        import psutil  # type: ignore
        return float(psutil.virtual_memory().total) / 1e9
    except Exception:
        pass

    sys_name = platform.system()
    try:
        if sys_name == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # "MemTotal:       64888888 kB"
                        kb = int(line.split()[1])
                        return kb * 1024 / 1e9
        elif sys_name == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                return int(out.stdout.strip()) / 1e9
        elif sys_name == "Windows":
            out = subprocess.run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                capture_output=True, text=True, timeout=4,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        return int(line) / 1e9
    except Exception:
        pass

    # Last-resort default. 8 GB is conservative-but-functional: enough for the
    # smallest vision models, and makes the gating useful instead of nuking it.
    return 8.0


def _detect_max_vram_gb() -> Optional[float]:
    """
    Return max VRAM (GB) of the largest single GPU available, or None if no GPU
    is detectable. Tries nvidia-smi → rocm-smi → Apple Silicon (unified memory).
    """
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                values = []
                for line in out.stdout.strip().splitlines():
                    line = line.strip()
                    if line and line.replace(".", "", 1).isdigit():
                        # nvidia-smi reports MiB
                        values.append(float(line) / 1024.0)
                if values:
                    return max(values)
        except Exception:
            pass

    # AMD / ROCm
    if shutil.which("rocm-smi"):
        try:
            out = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram", "--csv"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                # rocm-smi CSV: header line, then rows. Total VRAM in bytes.
                best = 0.0
                for line in out.stdout.strip().splitlines()[1:]:
                    parts = [p.strip() for p in line.split(",")]
                    for p in parts:
                        if p.isdigit():
                            best = max(best, int(p) / 1e9)
                if best > 0:
                    return best
        except Exception:
            pass

    # Apple Silicon: unified memory — treat all RAM as VRAM-equivalent.
    try:
        if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            return _detect_total_ram_gb()
    except Exception:
        pass

    return None


def _detect_vision_capability() -> dict:
    """
    Probe available compute for local vision inference. Result is cached for the
    process lifetime via ``_VISION_CAPABILITY``. NEVER raises — degrades to a
    "no GPU + assume 8 GB RAM" snapshot if every probe fails.

    Returns a dict with:
        has_gpu: bool
        vram_gb: float | None         — max VRAM of largest GPU
        ram_gb: float                  — total system RAM
        preferred_backend: "gpu"|"cpu"|"none"
        smallest_vram_required_gb: float — threshold for default vision model
        smallest_ram_required_gb: float  — threshold to run default model on CPU
    """
    try:
        ram_gb = _detect_total_ram_gb()
    except Exception:
        ram_gb = 8.0
    try:
        vram_gb = _detect_max_vram_gb()
    except Exception:
        vram_gb = None

    default_req = VISION_MODEL_REQUIREMENTS.get("qwen3.5:9b", _DEFAULT_VISION_REQUIREMENTS)
    smallest_vram = default_req["vram_gb"]
    smallest_ram = default_req["ram_gb"]

    has_gpu = vram_gb is not None and vram_gb > 0
    if has_gpu and (vram_gb or 0) >= smallest_vram:
        backend = "gpu"
    elif ram_gb >= smallest_ram:
        backend = "cpu"
    else:
        backend = "none"

    return {
        "has_gpu": has_gpu,
        "vram_gb": vram_gb,
        "ram_gb": round(ram_gb, 2),
        "preferred_backend": backend,
        "smallest_vram_required_gb": smallest_vram,
        "smallest_ram_required_gb": smallest_ram,
    }


# Cached at module load. Re-probing per call is wasteful — VRAM/RAM totals
# don't change at runtime (only available, which is irrelevant for sizing).
try:
    _VISION_CAPABILITY: dict = _detect_vision_capability()
    logger.info(f"Vision capability probe: {_VISION_CAPABILITY}")
except Exception as e:  # pragma: no cover — _detect_vision_capability shouldn't raise
    logger.warning(f"Vision capability probe failed entirely: {e}; falling back to safe default")
    _VISION_CAPABILITY = {
        "has_gpu": False, "vram_gb": None, "ram_gb": 8.0,
        "preferred_backend": "cpu",
        "smallest_vram_required_gb": _DEFAULT_VISION_REQUIREMENTS["vram_gb"],
        "smallest_ram_required_gb": _DEFAULT_VISION_REQUIREMENTS["ram_gb"],
    }


def _list_installed_ollama_models(timeout: float = 2.0) -> list:
    """Return list of installed model names from Ollama /api/tags. [] on failure."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def _model_fits(model: str, capability: dict) -> bool:
    """True if `capability` has enough VRAM (or, failing that, RAM) for `model`."""
    req = VISION_MODEL_REQUIREMENTS.get(model, _DEFAULT_VISION_REQUIREMENTS)
    vram = capability.get("vram_gb") or 0.0
    ram = capability.get("ram_gb") or 0.0
    if capability.get("has_gpu") and vram >= req["vram_gb"]:
        return True
    if ram >= req["ram_gb"]:
        return True
    return False


def _select_vision_model(requested: str, capability: dict) -> tuple:
    """
    Decide whether to run the requested model, swap to a smaller installed one,
    or skip the description tier entirely.

    Returns (model_to_use, fallback_reason).
    fallback_reason is None when no swap occurred. model_to_use is None when
    nothing fits and the description tier should be skipped.
    """
    if _model_fits(requested, capability):
        return requested, None

    # Try smaller installed models, ordered by VRAM requirement ascending.
    installed = set(_list_installed_ollama_models())
    candidates = sorted(
        [
            (name, req) for name, req in VISION_MODEL_REQUIREMENTS.items()
            if name != requested and name in installed
        ],
        key=lambda kv: kv[1]["vram_gb"],
    )
    for name, _req in candidates:
        if _model_fits(name, capability):
            return name, (
                f"auto-fallback from {requested}: insufficient memory for it "
                f"(have ~{capability.get('vram_gb') or 0:.1f} GB VRAM, "
                f"{capability.get('ram_gb', 0):.1f} GB RAM)"
            )

    return None, (
        f"insufficient memory: {capability.get('vram_gb') or 0:.1f} GB VRAM, "
        f"{capability.get('ram_gb', 0):.1f} GB RAM available; "
        f"need ~{VISION_MODEL_REQUIREMENTS.get(requested, _DEFAULT_VISION_REQUIREMENTS)['vram_gb']} GB VRAM "
        f"or ~{VISION_MODEL_REQUIREMENTS.get(requested, _DEFAULT_VISION_REQUIREMENTS)['ram_gb']} GB RAM "
        f"for {requested}, and no smaller installed model fits"
    )

# Initialize FastMCP server
mcp = FastMCP(
    "ollama",
    instructions=(
        "Local LLM inference tools — completely FREE (runs on-device). "
        "Use chat() for quick analysis, rewrites, and reasoning tasks instead of wasting Claude API tokens. "
        "Use read_document() to summarize or extract specific information from files locally. "
        "For large files (>100k chars), read_document automatically switches to chunked scanning mode. "
        "Use read_image() to load image files — returns base64 for Claude's vision + optional local description. "
        "Default model: qwen3:0.6b (fast). For complex reasoning: qwen3:latest (8B)."
    )
)

# Global config
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")


@mcp.tool()
def chat(
    prompt: str,
    model: str = "qwen3:0.6b",
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 500
) -> str:
    """
    Run inference with local Ollama model

    Args:
        prompt: User prompt
        model: Model to use (default: qwen3:0.6b for fast inference)
        system_prompt: Optional system prompt
        temperature: Sampling temperature 0.0-1.0 (default: 0.7)
        max_tokens: Maximum tokens to generate (default: 500)

    Returns:
        JSON string with model response
    """
    try:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens
                }
            }
        )

        if response.status_code != 200:
            return json.dumps({
                "success": False,
                "error": f"Failed to chat: {response.text}"
            }, indent=2)

        result = response.json()
        message = result.get("message", {})
        content = message.get("content", "")

        # Get metrics
        total_duration_ns = result.get("total_duration", 0)
        eval_count = result.get("eval_count", 0)
        eval_duration_ns = result.get("eval_duration", 0)

        tokens_per_sec = (eval_count / (eval_duration_ns / 1_000_000_000)) if eval_duration_ns > 0 else 0

        return json.dumps({
            "success": True,
            "model": model,
            "response": content,
            "metrics": {
                "total_duration_ms": total_duration_ns / 1_000_000,
                "output_tokens": eval_count,
                "tokens_per_sec": round(tokens_per_sec, 1)
            }
        }, indent=2)

    except Exception as e:
        logger.error(f"Error in chat: {e}")
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
def read_document(
    file_path: str,
    model: str = "qwen3:latest",
    task: str = "summarize",
    context_lines: int = 50,
) -> str:
    """
    Read and process a document using a local Ollama model.

    For files up to ~100k chars: processes the whole document at once.
    For larger files: automatically switches to chunked scanning mode,
    processing the file in segments to find relevant information.

    Args:
        file_path: Absolute path to the document (txt, md, py, json, etc.)
        model: Ollama model (qwen3:latest [8.2B, default], qwen3-coder:latest [30.5B for code],
               devstral:24b [23.6B for code], olmo-3:7b [7.3B, faster])
        task: What to do — "summarize", "extract_key_points", "analyze_structure",
              or a natural language instruction like "find the database connection string"
              or "extract all error handling logic". For targeted extraction from large
              files, a specific instruction triggers chunked scanning mode automatically.
        context_lines: Lines per chunk when scanning large files (default: 50)

    Returns:
        JSON string with processing result. For chunked mode, includes findings with
        line ranges. For whole-file mode, includes a single result string.

    Examples:
        read_document("/path/to/doc.md", task="summarize")
        read_document("/path/to/code.py", task="explain what this code does")
        read_document("/path/to/large.py", task="find the database connection string")
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return json.dumps({"success": False, "error": f"File not found: {file_path}"}, indent=2)
        if not path.is_file():
            return json.dumps({"success": False, "error": f"Path is not a file: {file_path}"}, indent=2)

        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            try:
                content = path.read_text(encoding='latin-1')
            except Exception as e:
                return json.dumps({"success": False, "error": f"Failed to read file (encoding issue): {str(e)}"}, indent=2)

        content_length = len(content)
        _WHOLE_FILE_LIMIT = 100_000  # ~25k tokens

        # --- Whole-file mode ---
        if content_length <= _WHOLE_FILE_LIMIT:
            task_prompts = {
                "summarize": f"Summarize the following document concisely, highlighting the main points:\n\n{content}",
                "extract_key_points": f"Extract and list the key points from this document:\n\n{content}",
                "analyze_structure": f"Analyze and describe the structure of this document:\n\n{content}",
            }
            prompt = task_prompts.get(task, f"{task}\n\nDocument:\n{content}")

            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 2000}
                },
                timeout=120
            )

            if response.status_code != 200:
                return json.dumps({"success": False, "error": f"Ollama request failed: {response.text}"}, indent=2)

            result = response.json()
            message = result.get("message", {})
            eval_count = result.get("eval_count", 0)
            eval_duration_ns = result.get("eval_duration", 0)
            tokens_per_sec = (eval_count / (eval_duration_ns / 1_000_000_000)) if eval_duration_ns > 0 else 0

            return json.dumps({
                "success": True,
                "file_path": str(path),
                "file_size_chars": content_length,
                "mode": "whole_file",
                "task": task,
                "model": model,
                "result": message.get("content", ""),
                "metrics": {
                    "output_tokens": eval_count,
                    "tokens_per_sec": round(tokens_per_sec, 1),
                    "duration_sec": round(result.get("total_duration", 0) / 1_000_000_000, 2)
                }
            }, indent=2)

        # --- Chunked scanning mode (large files) ---
        lines = content.split('\n')
        chunks = []
        for i in range(0, len(lines), context_lines):
            chunk = '\n'.join(lines[i:i + context_lines])
            if chunk.strip():
                chunks.append({
                    "content": chunk,
                    "start_line": i + 1,
                    "end_line": min(i + context_lines, len(lines))
                })

        findings = []
        for chunk_info in chunks:
            chunk_content = chunk_info["content"]
            prompt = f"""Does this text contain information about: {task}

Text:
{chunk_content}

If yes, extract and explain the relevant information. If no, respond with "NOT FOUND"."""

            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 500}
                },
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                response_content = result.get("message", {}).get("content", "").strip()
                if response_content and "NOT FOUND" not in response_content.upper():
                    findings.append({
                        "location": f"Lines {chunk_info['start_line']}-{chunk_info['end_line']}",
                        "information": response_content,
                        "context_snippet": chunk_content[:200] + "..." if len(chunk_content) > 200 else chunk_content
                    })

        if not findings:
            return json.dumps({
                "success": True,
                "file_path": str(path),
                "file_size_chars": content_length,
                "mode": "chunked_scan",
                "task": task,
                "found": False,
                "message": "No relevant information found in the document."
            }, indent=2)

        return json.dumps({
            "success": True,
            "file_path": str(path),
            "file_size_chars": content_length,
            "mode": "chunked_scan",
            "task": task,
            "model": model,
            "found": True,
            "findings_count": len(findings),
            "findings": findings
        }, indent=2)

    except requests.Timeout:
        return json.dumps({"success": False, "error": "Request timed out. Try a smaller file or reduce context_lines."}, indent=2)
    except Exception as e:
        logger.error(f"Error in read_document: {e}")
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@mcp.tool()
def read_image(
    file_path: str,
    max_total_pixels: int = 1_048_576,
    describe: bool = False,
    vision_model: Optional[str] = None,
    description_prompt: str = "Describe this image concisely.",
) -> str:
    """
    Read an image file and return it as a base64 content block for Claude's vision.

    The image is returned as a base64-encoded data URL that Claude can see directly.
    Optionally, get a text description via a local vision model (Ollama) — useful
    when the calling model doesn't support vision or to save tokens.

    Memory-aware gating
    -------------------
    The base64 image path is ALWAYS returned (Claude's own vision works regardless).
    The optional ``describe=True`` tier is gated by available compute:
      * GPU when VRAM ≥ per-model threshold
      * CPU when RAM ≥ per-model threshold (Ollama auto-falls back)
      * Otherwise: skip with ``description_skipped_reason`` and ``description: null``

    If the requested model doesn't fit but a smaller installed VLM does, the call
    auto-swaps; ``vision_model_used`` reflects the swap. Models are NEVER auto-pulled.

    The ``max_total_pixels`` argument is treated as an UPPER bound. The resize cap
    is automatically clamped down on low-memory systems to 256×256 / 512×512 /
    ~720×720 / 1024×1024 based on free VRAM (or RAM in CPU mode). The clamp is
    logged as ``image_budget_clamped_from`` in the response.

    Supports: PNG, JPEG, GIF, WebP, BMP, TIFF, SVG.

    Args:
        file_path: Absolute path to the image file
        max_total_pixels: Max total pixel count (width * height); image is downscaled
            preserving aspect ratio if larger. Default 1,048,576 (~1024×1024 equivalent).
            On low-VRAM/RAM systems this is clamped down automatically.
        describe: If True, also return a text description from a local vision model
        vision_model: Ollama model for description. Default ``qwen3.5:9b`` — unified
            text+vision (no separate ``-vl`` tag). Override with ``OLLAMA_VISION_MODEL``
            env var if the user has different hardware. Smallest reasonable choice for
            tight hardware: ``qwen3.5:4b`` (or ``gemma3:4b`` if available).
        description_prompt: Prompt for the vision model when describe=True
    """
    import base64
    import struct

    # Default vision model: env override → arg → built-in default
    if vision_model is None:
        vision_model = os.getenv("OLLAMA_VISION_MODEL", "qwen3.5:9b")

    path = Path(file_path)
    if not path.exists():
        return json.dumps({"success": False, "error": f"File not found: {file_path}"})

    # Magic byte MIME detection (don't trust file extensions)
    MAGIC_BYTES = {
        b'\x89PNG':       "image/png",
        b'\xff\xd8\xff':  "image/jpeg",
        b'GIF87a':        "image/gif",
        b'GIF89a':        "image/gif",
        b'RIFF':          "image/webp",  # RIFF....WEBP
        b'BM':            "image/bmp",
        b'II\x2a\x00':    "image/tiff",
        b'MM\x00\x2a':    "image/tiff",
    }

    raw = path.read_bytes()
    if len(raw) == 0:
        return json.dumps({"success": False, "error": "File is empty"})

    # Detect MIME from magic bytes
    mime_type = None
    for magic, mime in MAGIC_BYTES.items():
        if raw[:len(magic)] == magic:
            # Extra check for WebP: RIFF....WEBP
            if magic == b'RIFF' and raw[8:12] != b'WEBP':
                continue
            mime_type = mime
            break

    # Fallback: SVG detection (text-based)
    if mime_type is None:
        try:
            text_start = raw[:500].decode("utf-8", errors="ignore").lower()
            if "<svg" in text_start:
                mime_type = "image/svg+xml"
        except Exception:
            pass

    if mime_type is None:
        # Last resort: extension-based
        ext_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".tiff": "image/tiff", ".tif": "image/tiff", ".svg": "image/svg+xml",
        }
        mime_type = ext_map.get(path.suffix.lower())

    if mime_type is None:
        return json.dumps({"success": False, "error": f"Unknown image format: {path.suffix}"})

    # Size check (Anthropic API limit: ~5MB base64 ≈ 3.75MB raw)
    MAX_RAW_SIZE = 3_750_000
    if len(raw) > MAX_RAW_SIZE:
        return json.dumps({
            "success": False,
            "error": f"Image too large: {len(raw)/1_000_000:.1f}MB (max ~3.75MB for API). Resize first.",
        })

    # --- Memory-aware image-budget clamp ---------------------------------------
    # Cap user-supplied max_total_pixels by what the system can actually chew.
    # The argument is the UPPER bound; the auto-budget can shrink it but never
    # raise it. Models picked for the description tier may differ; we use the
    # user's vision_model intent here (the tier picker may swap below).
    auto_budget = _resize_budget_pixels(_VISION_CAPABILITY, vision_model)
    effective_pixels = max_total_pixels
    image_budget_clamped_from: Optional[int] = None
    if max_total_pixels and auto_budget < max_total_pixels:
        image_budget_clamped_from = max_total_pixels
        effective_pixels = auto_budget
        logger.info(
            f"read_image: max_total_pixels clamped from {max_total_pixels} "
            f"to {auto_budget} due to memory tier "
            f"(backend={_VISION_CAPABILITY['preferred_backend']}, "
            f"vram_gb={_VISION_CAPABILITY.get('vram_gb')}, "
            f"ram_gb={_VISION_CAPABILITY.get('ram_gb')})"
        )

    # Optional resize using PIL (if available). Bound by total-pixel count
    # rather than max-dimension so wide/tall images don't blow up VRAM
    # during local vision inference.
    import math
    resized = False
    new_w = new_h = None
    if effective_pixels and mime_type not in ("image/svg+xml",):
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            total = w * h
            if total > effective_pixels:
                ratio = math.sqrt(effective_pixels / total)
                new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                fmt = "PNG" if mime_type == "image/png" else "JPEG"
                img.save(buf, format=fmt, quality=85)
                raw = buf.getvalue()
                mime_type = "image/png" if fmt == "PNG" else "image/jpeg"
                resized = True
                new_w, new_h = new_size
                logger.info(
                    f"read_image: resized {path.name} from {w}x{h} ({total} px) "
                    f"to {new_size[0]}x{new_size[1]} ({new_size[0]*new_size[1]} px)"
                )
        except ImportError:
            pass  # PIL not available — send original size

    b64 = base64.b64encode(raw).decode("ascii")

    result = {
        "success": True,
        "file_path": str(path),
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "resized": resized,
        "base64_data_url": f"data:{mime_type};base64,{b64}",
        "memory_capability": {
            "has_gpu": _VISION_CAPABILITY["has_gpu"],
            "vram_gb": _VISION_CAPABILITY["vram_gb"],
            "ram_gb": _VISION_CAPABILITY["ram_gb"],
            "preferred_backend": _VISION_CAPABILITY["preferred_backend"],
        },
        "image_budget_pixels": effective_pixels,
    }
    if image_budget_clamped_from is not None:
        result["image_budget_clamped_from"] = image_budget_clamped_from
    if resized and new_w is not None:
        result["resized_to"] = {"w": new_w, "h": new_h}

    # --- Optional: text description from local vision model -------------------
    if describe:
        # Gate by capability + auto-swap to a smaller installed model if needed
        chosen, fallback_reason = _select_vision_model(vision_model, _VISION_CAPABILITY)

        if chosen is None:
            result["description"] = None
            result["description_skipped_reason"] = fallback_reason
            result["vision_model_used"] = None
            return json.dumps(result, indent=2)

        if chosen != vision_model:
            result["vision_model_used"] = f"{chosen} ({fallback_reason})"
        else:
            result["vision_model_used"] = chosen

        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": chosen,
                    "messages": [{
                        "role": "user",
                        "content": description_prompt,
                        "images": [b64],
                    }],
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 300},
                },
                timeout=30,
            )
            if resp.status_code == 200:
                desc = resp.json().get("message", {}).get("content", "").strip()
                result["description"] = desc
            else:
                result["description"] = None
                result["description_error"] = (
                    f"Vision model returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            result["description"] = None
            result["description_error"] = str(e)

    return json.dumps(result, indent=2)


if __name__ == "__main__":
    logger.info(f"Starting Ollama MCP Server")
    logger.info(f"Ollama URL: {OLLAMA_URL}")
    asyncio.run(mcp.run_stdio_async())
