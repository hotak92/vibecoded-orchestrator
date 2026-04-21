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
import requests
from typing import Optional
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    max_dimension: int = 1568,
    describe: bool = False,
    vision_model: str = "llama3.2-vision:latest",
    description_prompt: str = "Describe this image concisely.",
) -> str:
    """
    Read an image file and return it as a base64 content block for Claude's vision.

    The image is returned as a base64-encoded data URL that Claude can see directly.
    Optionally, get a text description via a local vision model (Ollama) — useful
    when the calling model doesn't support vision or to save tokens.

    Supports: PNG, JPEG, GIF, WebP, BMP, TIFF, SVG.

    Args:
        file_path: Absolute path to the image file
        max_dimension: Max width/height in pixels (resized if larger, preserving aspect ratio). Default 1568 (Anthropic's recommended max).
        describe: If True, also return a text description from a local vision model
        vision_model: Ollama model for description (default: llama3.2-vision:latest)
        description_prompt: Prompt for the vision model when describe=True
    """
    import base64
    import struct

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

    # Optional resize using PIL (if available)
    resized = False
    if max_dimension and mime_type not in ("image/svg+xml",):
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            if w > max_dimension or h > max_dimension:
                ratio = min(max_dimension / w, max_dimension / h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                fmt = "PNG" if mime_type == "image/png" else "JPEG"
                img.save(buf, format=fmt, quality=85)
                raw = buf.getvalue()
                mime_type = "image/png" if fmt == "PNG" else "image/jpeg"
                resized = True
                logger.info(f"read_image: resized {path.name} from {w}x{h} to {new_size[0]}x{new_size[1]}")
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
    }

    # Optional: get text description from local vision model
    if describe:
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": vision_model,
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
                result["description_error"] = f"Vision model returned HTTP {resp.status_code}"
        except Exception as e:
            result["description_error"] = str(e)

    return json.dumps(result, indent=2)


if __name__ == "__main__":
    logger.info(f"Starting Ollama MCP Server")
    logger.info(f"Ollama URL: {OLLAMA_URL}")
    asyncio.run(mcp.run_stdio_async())
