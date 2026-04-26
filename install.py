#!/usr/bin/env python3
"""
VibeCoded Tools — Orchestrator Installer (Cross-Platform)

Usage:
    python install.py [options]

Options:
    --no-containers     Skip Docker/Podman service setup
    --gpu               Enable GPU support for Ollama + code embeddings
    --cpu-only          Force CPU-only (skip GPU detection)
    --openai-key KEY    Use OpenAI embeddings instead of local models
    --container CMD     Force container runtime: docker | podman
    --dev               Install development dependencies
    --skip-models       Skip pulling Ollama models (manual later)
    --quiet             Minimal output

Requirements:
    - Python 3.11+
    - Docker or Podman (for Weaviate + Ollama containers)
    - Claude Code CLI (npm install -g @anthropic-ai/claude-code)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 11)
PROJECT_ROOT = Path(__file__).resolve().parent

# Default ports (configurable via .env)
DEFAULT_WEAVIATE_PORT = 8081
DEFAULT_WEAVIATE_GRPC_PORT = 50052
DEFAULT_OLLAMA_PORT = 11435
DEFAULT_CODE_EMBED_PORT = 11440

# Embedding model configurations.
#
# Per-model token/chunking limits live in
#   claude_mcp_servers/weaviate_mcp/chunking.py:MODEL_TOKEN_LIMITS
# and code-side in
#   claude_mcp_servers/weaviate_mcp/code_truncation.py:CODE_MODEL_TOKEN_LIMITS
# That is the single source of truth — do not re-declare chunk sizes here.
EMBEDDING_CONFIGS = {
    "gpu": {
        "text_model": "qwen3-embedding:0.6b",
        "text_dims": 1024,
        "code_backend": "gpu",
        "code_model": "codesage-large-v2",
        "code_dims": 2048,
        "ollama_models": ["qwen3-embedding:0.6b", "qwen3:0.6b"],
        "description": "GPU-accelerated (qwen3 text + CodeSage code, best quality)",
    },
    "cpu": {
        "text_model": "qwen3-embedding:0.6b",
        "text_dims": 1024,
        "code_backend": "ollama",
        "code_model": "unclemusclez/jina-embeddings-v2-base-code:latest",
        "code_dims": 768,
        "ollama_models": [
            "qwen3-embedding:0.6b",
            "unclemusclez/jina-embeddings-v2-base-code:latest",
            "qwen3:0.6b",
        ],
        "description": "CPU-only (qwen3 text + Jina V2 code, both via Ollama)",
    },
    "openai": {
        "text_model": "text-embedding-3-small",
        "text_dims": 1536,
        "code_backend": "openai",
        "code_model": "text-embedding-3-small",
        "code_dims": 1536,
        "ollama_models": ["qwen3:0.6b"],  # still need inference model
        "description": "OpenAI API (fastest, requires API key)",
    },
    # Lightest mode for low-RAM / low-VRAM machines.
    # Text uses Snowflake Arctic Embed v2 (smaller than qwen3, still 1024d, Apache 2.0).
    # Code uses Jina V2 base-code (768d, specialized for code).
    # Both run via Ollama (no GPU code-embed service).
    # Picks: opt-in via --low-resource (not auto-selected — explicit choice).
    "low_resource": {
        "text_model": "snowflake-arctic-embed2:latest",
        "text_dims": 1024,
        "code_backend": "ollama",
        "code_model": "unclemusclez/jina-embeddings-v2-base-code:latest",
        "code_dims": 768,
        "ollama_models": [
            "snowflake-arctic-embed2:latest",
            "unclemusclez/jina-embeddings-v2-base-code:latest",
            "qwen3:0.6b",
        ],
        "description": "Low-resource (Arctic text + Jina V2 code, both via Ollama)",
    },
}

HEALTH_TIMEOUT = 120  # seconds


class SystemInfo(NamedTuple):
    os_name: str        # "Linux", "Windows", "Darwin"
    has_gpu: bool       # NVIDIA GPU detected
    has_metal: bool     # Apple Silicon (Metal)
    container_cmd: str  # "docker" or "podman" or ""
    gpu_name: str       # GPU model name or ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VibeCoded Tools — Orchestrator Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-containers", action="store_true",
                        help="Skip Docker/Podman service setup")
    parser.add_argument("--gpu", action="store_true",
                        help="Enable GPU support for Ollama + code embeddings")
    parser.add_argument("--cpu-only", action="store_true",
                        help="Force CPU-only mode (skip GPU detection)")
    parser.add_argument("--low-resource", action="store_true",
                        help="Lightest mode: Jina V2 (768d) via Ollama. For low-RAM/low-VRAM machines.")
    parser.add_argument("--openai-key", type=str, default="",
                        help="Use OpenAI embeddings (provide API key)")
    parser.add_argument("--container", type=str, choices=["docker", "podman"],
                        help="Force a specific container runtime")
    parser.add_argument("--dev", action="store_true",
                        help="Install development dependencies")
    parser.add_argument("--skip-models", action="store_true",
                        help="Skip pulling Ollama models")
    parser.add_argument("--update", action="store_true",
                        help="Update mode: skip clone, re-install deps + restart services")
    parser.add_argument("--quiet", action="store_true",
                        help="Minimal output")
    parser.add_argument("--with-joern", action="store_true", default=False,
                        help="Force-enable Joern integration for richer code-graph metrics (CFG/PDG). Skips the install prompt.")
    parser.add_argument("--no-joern", action="store_true", default=False,
                        help="Skip Joern detection and don't prompt to install it (~600MB JVM-based).")
    parser.add_argument("--with-agents", action="store_true", default=True,
                        help="Install free-tier Claude agents (default: on)")
    parser.add_argument("--no-agents", dest="with_agents", action="store_false",
                        help="Skip installing Claude agents")
    parser.add_argument("--with-mao-agents", action="store_true",
                        help="Install MAO-tier specialist agents (requires MAO license)")
    parser.add_argument("--with-skills", action="store_true", default=True,
                        help="Install Claude skills (default: on)")
    parser.add_argument("--no-skills", dest="with_skills", action="store_false",
                        help="Skip installing Claude skills")
    args = parser.parse_args()

    mode = "update" if args.update else "install"

    print()
    print("=" * 62)
    if mode == "update":
        print("  VibeCoded Tools — Orchestrator Updater")
    else:
        print("  VibeCoded Tools — Orchestrator Installer")
    print("=" * 62)
    print()

    # Step 1: Check Python
    _check_python_version()
    _check_prerequisites()

    # Step 2: Detect system
    sysinfo = _detect_system(args)
    _print_system_info(sysinfo)

    # Step 2b: Optional companion tools (lean-ctx for context compression)
    joern_available = _detect_optional_companions(args)

    # Step 3: Determine embedding configuration
    embed_config = _choose_embedding_config(sysinfo, args)
    print(f"\n  Embedding mode: {embed_config['description']}")

    # Step 4: Create virtual environment
    venv_python = _create_venv(PROJECT_ROOT)

    # Step 5: Install/update Python dependencies
    _install_requirements(venv_python, dev=args.dev)

    # Step 6: Container services (restart on update to pick up config changes)
    if not args.no_containers:
        if not sysinfo.container_cmd:
            print("\n[!] No container runtime found. Install Docker or Podman.")
            print("    Docker: https://docs.docker.com/get-docker/")
            print("    Podman: https://podman.io/getting-started/installation")
            print("    Or re-run with --no-containers to skip.")
            return 1
        _start_services(sysinfo, args, embed_config)
        if not args.skip_models:
            _wait_for_ollama()
            _pull_ollama_models(embed_config["ollama_models"])
        # Bug 29: with shared-container reuse, multiple installs hit the same
        # Weaviate. Bootstrap any of THIS project's KG/Development collections
        # that aren't there yet — leave existing ones alone.
        _ensure_collections(embed_config)
    else:
        print("\n[skip] Container services (--no-containers)")

    # Step 7: Create state directory
    _create_state_directory()

    # Step 8: Write .env configuration (skip on update — don't overwrite user changes)
    if mode == "install":
        _write_env_config(embed_config, args, joern_available=joern_available)
    else:
        print("[skip] .env configuration (preserved during update)")

    # Step 9: Configure Claude Code settings (skip on update)
    if mode == "install":
        _configure_claude_settings(embed_config)
    else:
        print("[skip] Claude settings (preserved during update)")

    # Step 9b: Install agents and skills from templates/
    _install_agents_and_skills(args)

    # Step 10: Check Claude CLI
    _check_claude_cli()

    # Step 11: Initial code graph analysis (if repo has code)
    # Skipped on first install — user runs manually after setup

    # Done
    print()
    print("=" * 62)
    print("  Installation complete!")
    print("=" * 62)
    print()
    _print_next_steps(sysinfo, args)
    return 0


# ---------------------------------------------------------------------------
# Step 1: Python version
# ---------------------------------------------------------------------------

def _check_python_version() -> None:
    print("[1/10] Checking Python version ... ", end="", flush=True)
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        print("FAIL")
        print(f"  Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
              f"found {v.major}.{v.minor}.{v.micro}")
        _print_python_install_hint()
        sys.exit(1)
    print(f"OK ({v.major}.{v.minor}.{v.micro})")


def _print_python_install_hint() -> None:
    os_name = platform.system()
    if os_name == "Linux":
        print("  Install: sudo apt install python3.12  (Ubuntu/Debian)")
        print("           sudo dnf install python3.12  (Fedora)")
    elif os_name == "Darwin":
        print("  Install: brew install python@3.12")
    elif os_name == "Windows":
        print("  Install: winget install Python.Python.3.12")
        print("       Or: https://python.org/downloads/")
    print("  Download: https://python.org")


def _check_prerequisites() -> None:
    """Warn (don't block) about optional prerequisites.

    Hard requirements (Python, container runtime) are checked elsewhere.
    This function surfaces *soft* prereqs that the rest of the install
    expects to be available later, so the user can install them now rather
    than discover them mid-run.
    """
    os_name = platform.system()
    missing: list[tuple[str, str]] = []  # (tool, install hint)

    # The python venv module is built-in on most distros, but Debian/Ubuntu
    # ships it as a separate package. Detect early.
    if os_name == "Linux":
        try:
            r = subprocess.run(
                [sys.executable, "-c", "import venv"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                missing.append(("python3-venv", "sudo apt install python3-venv  # Debian/Ubuntu"))
        except (subprocess.TimeoutExpired, OSError):
            pass

    # ensurepip / pip availability inside the soon-to-be-created venv.
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import ensurepip"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            if os_name == "Linux":
                missing.append(("python3-pip / ensurepip",
                                "sudo apt install python3-pip  # Debian/Ubuntu"))
            else:
                missing.append(("ensurepip",
                                "Reinstall Python from python.org or your package manager"))
    except (subprocess.TimeoutExpired, OSError):
        pass

    if missing:
        print()
        print("  WARNING: missing optional prerequisites:")
        for tool, hint in missing:
            print(f"    - {tool}: {hint}")
        print("  Continuing — these are needed only for specific install paths.")
        print()


# ---------------------------------------------------------------------------
# Step 2: System detection
# ---------------------------------------------------------------------------

def _detect_system(args: argparse.Namespace) -> SystemInfo:
    print("[2/10] Detecting system ... ", flush=True)
    os_name = platform.system()
    has_gpu = False
    has_metal = False
    gpu_name = ""
    container_cmd = ""

    # GPU detection
    if args.cpu_only:
        print("  GPU: skipped (--cpu-only)")
    elif args.openai_key:
        print("  GPU: not needed (using OpenAI embeddings)")
    else:
        has_gpu, gpu_name = _detect_nvidia_gpu()
        if has_gpu:
            print(f"  GPU: {gpu_name}")
        elif os_name == "Darwin" and _detect_apple_silicon():
            has_metal = True
            print("  GPU: Apple Silicon (Metal — Ollama uses natively)")
        else:
            print("  GPU: none detected (will use CPU)")

    # Container runtime
    if args.container:
        container_cmd = args.container
        print(f"  Container: {container_cmd} (forced)")
    else:
        container_cmd = _detect_container_runtime()
        if container_cmd:
            print(f"  Container: {container_cmd}")
        elif not args.no_containers:
            print("  Container: none found")

    print(f"  OS: {os_name} ({platform.machine()})")

    return SystemInfo(
        os_name=os_name,
        has_gpu=has_gpu or args.gpu,
        has_metal=has_metal,
        container_cmd=container_cmd,
        gpu_name=gpu_name,
    )


def _detect_nvidia_gpu() -> tuple[bool, str]:
    """Check for NVIDIA GPU via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, ""


def _detect_apple_silicon() -> bool:
    """Check if running on Apple Silicon."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _detect_container_runtime() -> str:
    """Detect Docker or Podman. Prefer Podman on Linux, Docker elsewhere."""
    os_name = platform.system()
    # Order: Linux prefers Podman (no commercial license needed)
    candidates = ["podman", "docker"] if os_name == "Linux" else ["docker", "podman"]

    for cmd in candidates:
        if shutil.which(cmd):
            try:
                result = subprocess.run(
                    [cmd, "version"], capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    return cmd
            except (subprocess.TimeoutExpired, OSError):
                continue
    return ""


def _print_system_info(sysinfo: SystemInfo) -> None:
    pass  # Already printed in _detect_system


def _detect_optional_companions(args: argparse.Namespace) -> bool:
    """Check for optional companion tools that the orchestrator can leverage when present.

    Two checks:
    1. lean-ctx (Rust binary at ~/.cargo/bin/) — token-compression helper, hint only.
    2. joern (JVM-based code-property-graph tool) — when present, the code graph
       analyzer adds CFG complexity metrics + data-flow variable lists per function
       (`cfg_summary`, `data_flow_vars` fields on CodeFunction). When absent, we
       skip those fields cleanly. If absent + interactive + not --no-joern, we
       prompt the user once.

    Returns True if Joern is available (whether pre-existing or freshly installed),
    so callers can flip --cfg/--pdg defaults.
    """
    print("\n[2b/10] Optional companions ...")

    # lean-ctx (hint only — global tool, opt-in install)
    if shutil.which("lean-ctx"):
        print("  lean-ctx: detected (per-user global; integrations may use it)")
    else:
        print("  lean-ctx: not installed (optional, recommended for token savings)")
        print("            install:  cargo install lean-ctx")
        print("            then re-run this installer")

    # Joern (CFG/PDG metrics for code graph)
    joern_path = shutil.which("joern")
    if joern_path:
        print(f"  joern:    detected at {joern_path} (code graph will include CFG/PDG metrics)")
        return True

    if args.no_joern:
        print("  joern:    skipped (--no-joern)")
        return False

    if args.with_joern:
        # User explicitly requested install — proceed without confirmation
        return _install_joern()

    if args.quiet or not sys.stdin.isatty():
        # Non-interactive: hint only, don't prompt
        print("  joern:    not installed (optional, ~600MB JVM-based)")
        print("            adds CFG complexity + data-flow variable metrics to the code graph")
        print("            to install:   re-run installer with --with-joern")
        print("            to skip prompt next time:   re-run with --no-joern")
        return False

    # Interactive: ask once
    print("  joern:    not installed (optional, ~600MB JVM-based)")
    print("            adds CFG complexity + data-flow variable metrics to the code graph")
    try:
        answer = input("            Install Joern now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer not in {"y", "yes"}:
        print("            Skipping. Re-run with --with-joern to install later.")
        return False

    return _install_joern()


def _install_joern() -> bool:
    """Install Joern via the official installer script.

    Returns True on success, False on failure (non-fatal — the orchestrator
    works fine without Joern).

    Security note: this downloads and executes a remote shell script from
    joernio/joern's GitHub releases. The transport is HTTPS (cert-validated)
    and the source is the official upstream. We add basic sanity checks
    (HTTPS-only URL, non-trivial response size, .sh shebang) but do NOT
    enforce a checksum because Joern's release pipeline does not publish a
    pinned hash for `latest`. Users who want stronger guarantees should
    install Joern themselves first (then we just detect it).
    """
    print("            Installing Joern (this can take a few minutes)...")

    install_url = "https://github.com/joernio/joern/releases/latest/download/joern-install.sh"
    if not install_url.startswith("https://"):
        # Defense-in-depth — never fetch over plain HTTP.
        print("            Refusing to fetch Joern installer over non-HTTPS URL.")
        return False

    installer_path: str | None = None
    try:
        # Download with explicit timeout (urlretrieve has no default timeout).
        with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as tmp:
            installer_path = tmp.name
        with urllib.request.urlopen(install_url, timeout=60) as resp:
            data = resp.read()
        # Sanity-check the payload looks like a shell script.
        if len(data) < 256:
            print(f"            Joern installer suspiciously small ({len(data)} bytes); aborting.")
            return False
        if not data.lstrip().startswith(b"#!"):
            print("            Joern installer does not start with a shebang; aborting.")
            return False
        Path(installer_path).write_bytes(data)
        os.chmod(installer_path, 0o755)

        # Install to ~/.local (user-local, no sudo needed).
        install_dir = Path.home() / ".local" / "joern"
        result = subprocess.run(
            [installer_path, "--dir", str(install_dir), "--no-interactive"],
            capture_output=True, text=True, timeout=600,
        )

        if result.returncode != 0:
            tail = (result.stderr or "").strip()[-300:]
            print(f"            Joern install failed: {tail}")
            print("            You can install manually: https://docs.joern.io/installation/")
            return False

        joern_bin = install_dir / "joern-cli"
        if joern_bin.exists():
            os.environ["PATH"] = f"{joern_bin}:{os.environ.get('PATH', '')}"
            print(f"            Joern installed at {joern_bin}")
            print(f"            To use joern outside this installer, add to your shell rc:")
            print(f"              export PATH=\"{joern_bin}:$PATH\"")
            return shutil.which("joern") is not None

        print(f"            Joern installer ran but joern-cli not at {joern_bin}")
        return False

    except (urllib.error.URLError, subprocess.TimeoutExpired, OSError) as e:
        print(f"            Joern install failed: {e}")
        print("            You can install manually: https://docs.joern.io/installation/")
        return False
    finally:
        if installer_path:
            Path(installer_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 3: Embedding configuration
# ---------------------------------------------------------------------------

def _choose_embedding_config(sysinfo: SystemInfo, args: argparse.Namespace) -> dict:
    # Explicit opt-ins win over auto-detection.
    if args.openai_key:
        config = dict(EMBEDDING_CONFIGS["openai"])
        config["openai_key"] = args.openai_key
        return config
    if args.low_resource:
        return dict(EMBEDDING_CONFIGS["low_resource"])
    if args.cpu_only:
        return dict(EMBEDDING_CONFIGS["cpu"])
    # Auto-detection: GPU → gpu config, otherwise cpu (qwen3 for both).
    if sysinfo.has_gpu:
        return dict(EMBEDDING_CONFIGS["gpu"])
    return dict(EMBEDDING_CONFIGS["cpu"])


# ---------------------------------------------------------------------------
# Step 4: Virtual environment
# ---------------------------------------------------------------------------

def _create_venv(project_root: Path) -> Path:
    print("\n[3/10] Creating virtual environment ... ", end="", flush=True)
    venv_dir = project_root / ".venv"

    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    if venv_python.exists():
        print("already exists")
        return venv_python

    # Don't use check=True with capture_output — we want to surface stderr on failure.
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("FAIL")
        print("  Failed to create venv. stderr:")
        for line in (result.stderr or "").strip().splitlines()[-20:]:
            print(f"    {line}")
        print()
        print("  Common causes:")
        if platform.system() == "Linux":
            print("    - Missing python3-venv: sudo apt install python3-venv  (Debian/Ubuntu)")
            print("                            sudo dnf install python3-venv  (Fedora)")
        print("    - Disk full or no write permission to:")
        print(f"      {venv_dir}")
        sys.exit(1)
    print("OK")
    return venv_python


# ---------------------------------------------------------------------------
# Step 5: Install dependencies
# ---------------------------------------------------------------------------

def _install_requirements(venv_python: Path, *, dev: bool) -> None:
    label = "with dev extras" if dev else "production"
    print(f"[4/10] Installing dependencies ({label}) ... ", flush=True)

    # Upgrade pip — surface errors instead of swallowing them via check=True
    pip_up = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        capture_output=True, text=True,
    )
    if pip_up.returncode != 0:
        print("  FAIL (pip upgrade)")
        for line in (pip_up.stderr or "").strip().splitlines()[-15:]:
            print(f"    {line}")
        print()
        print("  Hint: check your network connection and PyPI availability.")
        print("        If behind a corporate proxy, set http_proxy/https_proxy.")
        sys.exit(1)

    # Install requirements
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        print("  WARNING: requirements.txt not found, skipping pip install")
        return

    cmd = [str(venv_python), "-m", "pip", "install", "-r", str(req_file)]
    if dev:
        req_dev = PROJECT_ROOT / "requirements-dev.txt"
        if req_dev.exists():
            cmd = [str(venv_python), "-m", "pip", "install",
                   "-r", str(req_file), "-r", str(req_dev)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  FAIL")
        # Show last 30 lines of error
        lines = result.stderr.strip().splitlines()[-30:]
        for line in lines:
            print(f"  {line}")
        sys.exit(1)
    print("  OK")


# ---------------------------------------------------------------------------
# Step 6: Container services
# ---------------------------------------------------------------------------

def _probe_http(url: str, timeout: float = 2.0) -> str | None:
    """Probe a URL with HEAD/GET. Returns the URL if reachable + status<400, else None.

    Used to detect already-running shared services (Weaviate / Ollama / code_embed)
    so we don't try to start a duplicate container that would bind-conflict on the
    same host port.
    """
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        if resp.status < 400:
            return url
    except Exception:
        pass
    return None


def _detect_existing_services(weaviate_port: int = DEFAULT_WEAVIATE_PORT,
                              ollama_port: int = DEFAULT_OLLAMA_PORT,
                              code_embed_port: int = DEFAULT_CODE_EMBED_PORT) -> dict:
    """Probe the three default service endpoints. Returns a dict with the URL
    on success (str) or None when not reachable, for each of weaviate / ollama /
    code_embed."""
    return {
        "weaviate_url": _probe_http(
            f"http://localhost:{weaviate_port}/v1/.well-known/ready"
        ),
        "ollama_url": _probe_http(
            f"http://localhost:{ollama_port}/api/tags"
        ),
        "code_embed_url": _probe_http(
            f"http://localhost:{code_embed_port}/health"
        ),
    }


_ORCHESTRATOR_VOLUME_NAMES = (
    # Canonical (current compose)
    "weaviate_data",
    "ollama_data",
    "code_embed_cache",
    # Historical project-suffixed names
    "weaviate_claude",
    "weaviate_ARTup",
    "ollama_claude",
    "ollama_ARTup",
    "vct_code_embed",
)


def _detect_existing_volume_paths() -> dict:
    """Bug 31: read-only probe for existing orchestrator volumes.

    Mirrors the Rust `detect_existing_volumes` in
    launcher/src-tauri/src/commands/installer.rs — both the launcher and
    the headless install.py honor the same Bug 32 contract: when
    existing volumes are detected, do NOT generate a bind-mount override.

    Returns a dict mapping volume_name -> {"mountpoint": str,
    "size_gb": float | None}. Empty dict when no runtime is installed
    or no orchestrator volumes are found.

    Calls only `<runtime> volume inspect <name>` which is read-only.
    Never invokes `volume rm` / `volume prune` / `compose down`.
    """
    volumes: dict[str, dict] = {}
    runtime = None
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            runtime = cmd
            break
    if runtime is None:
        return volumes
    for name in _ORCHESTRATOR_VOLUME_NAMES:
        try:
            r = subprocess.run(
                [runtime, "volume", "inspect", name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode != 0:
            continue
        try:
            data = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            continue
        if not data or "Mountpoint" not in data[0]:
            continue
        mountpoint = data[0]["Mountpoint"]
        # Best-effort size probe via `du -sk` (kibibytes). Failure is fine.
        size_gb: float | None = None
        try:
            du = subprocess.run(
                ["du", "-sk", mountpoint],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if du.returncode == 0:
                kb_str = du.stdout.split()[0]
                size_gb = int(kb_str) / (1024 * 1024)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        volumes[name] = {"mountpoint": mountpoint, "size_gb": size_gb}
    return volumes


def _start_services(sysinfo: SystemInfo, args: argparse.Namespace,
                    embed_config: dict) -> None:
    print(f"\n[5/10] Starting services via {sysinfo.container_cmd} ... ", flush=True)

    infra_dir = PROJECT_ROOT / "infrastructure"
    compose_file = infra_dir / "docker-compose.yml"

    if not compose_file.exists():
        print(f"  WARNING: {compose_file} not found, skipping.")
        print("  Start Weaviate and Ollama manually.")
        return

    # Bug 31 contract: when existing orchestrator volumes are detected,
    # we surface them so the user knows their data will be reused, and
    # we do NOT (re)generate any bind-mount override. The launcher GUI
    # is the only path that may generate a docker-compose.override.yml;
    # headless install.py keeps things conservative.
    existing_volumes = _detect_existing_volume_paths()
    if existing_volumes:
        print(f"  Existing orchestrator volumes detected — keeping in place:")
        for name, info in existing_volumes.items():
            size = (
                f" ({info['size_gb']:.1f} GB)" if info.get("size_gb") is not None else ""
            )
            print(f"    [reuse] {name} -> {info['mountpoint']}{size}")

    # Bug 29: shared containers across installs.
    # Before running `compose up -d` (which would bind to host ports), probe
    # the default ports. If a service is already up, reuse it — installs share
    # one Weaviate / Ollama / code_embed per machine. Per-install isolation
    # comes from KG_COLLECTION namespacing inside the shared Weaviate.
    #
    # Escape hatch: VCT_FORCE_SEPARATE_CONTAINERS=1 forces a full `up -d`
    # regardless of what's already running (advanced — caller is responsible
    # for resolving port conflicts via WEAVIATE_PORT/OLLAMA_PORT overrides).
    weaviate_port = int(os.environ.get("WEAVIATE_PORT", DEFAULT_WEAVIATE_PORT))
    ollama_port = int(os.environ.get("OLLAMA_PORT", DEFAULT_OLLAMA_PORT))
    code_embed_port = int(os.environ.get("CODE_EMBED_PORT", DEFAULT_CODE_EMBED_PORT))

    force_separate = os.environ.get("VCT_FORCE_SEPARATE_CONTAINERS") == "1"
    detected = _detect_existing_services(weaviate_port, ollama_port, code_embed_port)

    if not force_separate:
        any_detected = any(v is not None for v in detected.values())
        if any_detected:
            print("  Detected already-running services:")
            for label, url in (
                ("Weaviate", detected["weaviate_url"]),
                ("Ollama", detected["ollama_url"]),
                ("code_embed", detected["code_embed_url"]),
            ):
                if url:
                    print(f"    [reuse] {label}: {url}")
                else:
                    print(f"    [start] {label}: not detected")

    # Determine which compose services need to start.
    # If --gpu, we additionally bring up code_embed (gated on the gpu profile +
    # overlay file). On CPU-only setups the service uses Ollama as code embed
    # backend and code_embed is intentionally skipped.
    services_to_start: list[str] = []
    if force_separate:
        # No detection — bring everything compose declares up.
        services_to_start = []  # empty list => `up -d` with no service args
    else:
        if not detected["weaviate_url"]:
            services_to_start.append("weaviate")
        if not detected["ollama_url"]:
            services_to_start.append("ollama")
        if sysinfo.has_gpu and not detected["code_embed_url"]:
            services_to_start.append("code_embed")

    # All required services already up — nothing to do.
    if not force_separate and not services_to_start:
        print("  All required services already running — reusing them.")
        print("  (Set VCT_FORCE_SEPARATE_CONTAINERS=1 for separate per-install containers.)")
        return

    compose_cmd = _get_compose_command(sysinfo.container_cmd)

    cmd = [*compose_cmd, "-f", str(compose_file)]

    # GPU overlay + code_embed profile
    if sysinfo.has_gpu:
        gpu_file = infra_dir / "docker-compose.gpu.yml"
        if gpu_file.exists():
            cmd.extend(["-f", str(gpu_file), "--profile", "gpu"])
            print("  GPU overlay: enabled (includes code_embed container)")
        else:
            print("  WARNING: GPU overlay file not found, running CPU-only")

    cmd.extend(["up", "-d"])
    # When subset detection said only some services are missing, pass them
    # explicitly so compose doesn't try to recreate already-running ones.
    if services_to_start:
        cmd.extend(services_to_start)
        print(f"  Starting only: {', '.join(services_to_start)}")

    # 15 min cap: first-run pulls of weaviate + ollama images can take a while
    # on slow links, but a hung daemon should not block us forever.
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(infra_dir), timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("  FAIL (timed out after 15 min)")
        print(f"  Container daemon may be hung. Try manually:")
        print(f"    cd {infra_dir}")
        print(f"    {' '.join(compose_cmd)} up -d")
        sys.exit(1)
    if result.returncode != 0:
        print("  FAIL")
        for line in (result.stderr or "").strip().splitlines()[-10:]:
            print(f"  {line}")
        print("\n  Try starting manually:")
        print(f"    cd {infra_dir}")
        print(f"    {' '.join(compose_cmd)} up -d")
        # Common cause: daemon not running. Surface it.
        stderr_lower = (result.stderr or "").lower()
        if "cannot connect" in stderr_lower or "daemon" in stderr_lower:
            print("\n  Hint: container daemon not running.")
            if sysinfo.container_cmd == "docker":
                print("    Linux:  sudo systemctl start docker")
                print("    macOS:  open Docker Desktop")
                print("    Windows: start Docker Desktop")
            else:
                print("    Linux:  systemctl --user start podman.socket")
        # Common cause: bind: address already in use → user already has a
        # service on this port that we somehow didn't probe (different
        # protocol, late startup, …). Tell them about the escape hatch.
        if "address already in use" in stderr_lower or "bind" in stderr_lower:
            print("\n  Hint: a host port is already in use.")
            print("    Either stop the conflicting process, or set")
            print("    VCT_FORCE_SEPARATE_CONTAINERS=1 + override WEAVIATE_PORT /")
            print("    OLLAMA_PORT / CODE_EMBED_PORT to use distinct ports.")
        sys.exit(1)
    print("  OK")


def _get_compose_command(container_cmd: str) -> list[str]:
    """Return the compose command as a list of args."""
    if container_cmd == "podman":
        # Prefer standalone podman-compose if present
        if shutil.which("podman-compose"):
            return ["podman-compose"]
        # Try `podman compose` plugin
        try:
            result = subprocess.run(
                ["podman", "compose", "version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return ["podman", "compose"]
        except (subprocess.TimeoutExpired, OSError):
            pass
        # Last-resort fallback (user will see error if neither works)
        return ["podman", "compose"]

    # Docker: try v2 plugin first, then standalone
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    except (subprocess.TimeoutExpired, OSError):
        pass

    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def _wait_for_ollama() -> None:
    """Wait for Ollama to be ready."""
    print("[6/10] Waiting for Ollama ... ", end="", flush=True)
    port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))
    url = f"http://localhost:{port}/api/tags"
    deadline = time.monotonic() + HEALTH_TIMEOUT

    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=3)
            if resp.status == 200:
                print("OK")
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)

    print("TIMEOUT")
    print(f"  Ollama not ready after {HEALTH_TIMEOUT}s at {url}")
    print("  Check container logs.")


def _pull_ollama_models(models: list[str]) -> None:
    """Pull required Ollama models."""
    print("[7/10] Pulling Ollama models ... ", flush=True)
    port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))

    for model in models:
        print(f"  Pulling {model} ... ", end="", flush=True)
        try:
            data = json.dumps({"name": model}).encode()
            req = urllib.request.Request(
                f"http://localhost:{port}/api/pull",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=600)
            # Read streaming response to completion
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
            print("OK")
        except (urllib.error.URLError, OSError) as e:
            print(f"WARN ({e})")
            print(f"    Pull manually: curl -X POST "
                  f"http://localhost:{port}/api/pull "
                  f"-d '{{\"name\": \"{model}\"}}'")


# ---------------------------------------------------------------------------
# Step 6c: Weaviate collection bootstrap (shared-container aware)
# ---------------------------------------------------------------------------

# Minimal Weaviate class definitions for the collections this install needs.
# Vectorizer is "none" — we feed pre-computed vectors from Ollama / CodeSage.
# These are intentionally property-light: the MCP server (server.py) uses the
# v4 client `client.collections.get(name)` which doesn't require a strict
# property list to insert; richer schemas can be added later without
# re-creating the class.
def _kg_class_definition(name: str) -> dict:
    return {
        "class": name,
        "description": "VibeCoded Tools knowledge graph collection",
        "vectorizer": "none",
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "node_type", "dataType": ["text"]},
            {"name": "tags", "dataType": ["text[]"]},
            {"name": "links", "dataType": ["text[]"]},
            {"name": "typed_links", "dataType": ["text[]"]},
            {"name": "status", "dataType": ["text"]},
        ],
    }


def _development_class_definition(name: str) -> dict:
    return {
        "class": name,
        "description": "VibeCoded Tools project documentation collection",
        "vectorizer": "none",
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
        ],
    }


def _ensure_collections(embed_config: dict) -> None:
    """Detect existing Weaviate collections and create only the ones missing.

    Code-graph collections (CodeModule / CodeClass / CodeFunction / CodeAPI /
    CodeInteraction) are SHARED across all projects on this machine — they
    carry a `project_name` field that separates rows. Don't recreate them
    per-install: the MCP server creates them lazily on first write.
    """
    weaviate_port = os.environ.get("WEAVIATE_PORT", str(DEFAULT_WEAVIATE_PORT))
    weaviate_url = f"http://localhost:{weaviate_port}"
    kg_name = os.environ.get("KG_COLLECTION", "KnowledgeGraph")
    dev_name = os.environ.get("DEVELOPMENT_COLLECTION", "Development")

    print(f"[7b/10] Checking Weaviate collections at {weaviate_url} ... ", flush=True)

    # 1. Read existing schema.
    try:
        resp = urllib.request.urlopen(f"{weaviate_url}/v1/schema", timeout=10)
        schema = json.loads(resp.read())
    except Exception as e:
        print(f"  WARN: couldn't read schema ({e}). Skipping bootstrap.")
        print("  MCP server will create collections lazily on first write.")
        return

    existing = {
        c.get("class") for c in schema.get("classes", [])
        if isinstance(c, dict) and c.get("class")
    }

    # 2. Required for THIS project install. Code-graph collections excluded
    #    on purpose — they're shared and created on demand.
    required = [
        (kg_name, _kg_class_definition),
        (dev_name, _development_class_definition),
    ]

    missing = [(n, b) for (n, b) in required if n not in existing]
    if not missing:
        print(f"  All collections present (reusing {len(required)} shared classes).")
        return

    # 3. POST each missing class definition.
    created: list[str] = []
    failed: list[tuple[str, str]] = []
    for name, builder in missing:
        body = json.dumps(builder(name)).encode()
        req = urllib.request.Request(
            f"{weaviate_url}/v1/schema",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            created.append(name)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
            # 422 with "already exists" is benign on race with another install.
            if e.code == 422 and "already exists" in err_body.lower():
                created.append(f"{name} (already)")
            else:
                failed.append((name, f"HTTP {e.code}: {err_body}"))
        except Exception as e:
            failed.append((name, str(e)))

    for c in created:
        print(f"  + created collection {c}")
    for n, err in failed:
        print(f"  ! failed to create {n}: {err}")
    if not failed:
        print("  OK")


# ---------------------------------------------------------------------------
# Step 7: State directory
# ---------------------------------------------------------------------------

def _create_state_directory() -> None:
    print("[8/10] Creating state directory ... ", end="", flush=True)
    state_dir = PROJECT_ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    print("OK")


# ---------------------------------------------------------------------------
# Step 8: Write .env
# ---------------------------------------------------------------------------

def _write_env_config(embed_config: dict, args: argparse.Namespace, joern_available: bool = False) -> None:
    print("[9/10] Writing configuration ... ", end="", flush=True)
    env_file = PROJECT_ROOT / ".env"

    # Bug 29: these URLs always point at localhost. With shared containers
    # there is exactly one Weaviate / Ollama / code_embed per machine; every
    # install — wherever it lives on disk — reaches them via 127.0.0.1 on the
    # default ports. Per-install isolation comes from KG_COLLECTION (set by
    # the launcher's projects_v2::write_project_env_files), NOT from
    # different host endpoints.
    weaviate_port = os.environ.get("WEAVIATE_PORT", str(DEFAULT_WEAVIATE_PORT))
    weaviate_grpc = os.environ.get("WEAVIATE_GRPC_PORT", str(DEFAULT_WEAVIATE_GRPC_PORT))
    ollama_port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))
    code_embed_port = os.environ.get("CODE_EMBED_PORT", str(DEFAULT_CODE_EMBED_PORT))

    lines = [
        "# VibeCoded Tools — Orchestrator Configuration",
        "# Generated by install.py — edit as needed",
        "",
        "# Weaviate",
        f"WEAVIATE_URL=http://localhost:{weaviate_port}",
        f"WEAVIATE_PORT={weaviate_port}",
        f"WEAVIATE_GRPC_PORT={weaviate_grpc}",
        "",
        "# Ollama",
        f"OLLAMA_URL=http://localhost:{ollama_port}",
        f"OLLAMA_PORT={ollama_port}",
        "",
        "# Embedding models",
        f"EMBEDDING_MODEL={embed_config['text_model']}",
        f"EMBEDDING_DIMS={embed_config['text_dims']}",
        f"CODE_EMBED_BACKEND={embed_config['code_backend']}",
        f"CODE_EMBED_MODEL={embed_config['code_model']}",
        f"CODE_EMBED_DIMS={embed_config['code_dims']}",
        f"CODE_EMBED_SERVICE_URL=http://localhost:{code_embed_port}",
        f"ACTIVE_EMBEDDING=qwen3",
        "",
        "# Optional companion tools (auto-detected at install)",
        f"VCT_JOERN_AVAILABLE={'1' if joern_available else '0'}",
        "",
        "# Knowledge Graph",
        "KG_COLLECTION=KnowledgeGraph",
        "DEVELOPMENT_COLLECTION=Development",
        "",
    ]

    if embed_config.get("openai_key"):
        lines.extend([
            "# OpenAI (for embeddings)",
            f"OPENAI_API_KEY={embed_config['openai_key']}",
            "EMBEDDING_PROVIDER=openai",
            "",
        ])
    else:
        lines.extend([
            "# Embedding provider",
            "EMBEDDING_PROVIDER=ollama",
            "",
        ])

    # Write (don't overwrite if exists)
    if env_file.exists():
        print("already exists (not overwritten)")
    else:
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("OK")


# ---------------------------------------------------------------------------
# Step 9: Configure Claude Code
# ---------------------------------------------------------------------------

def _configure_claude_settings(embed_config: dict) -> None:
    """Create .claude/settings.json with MCP server configuration."""
    settings_dir = PROJECT_ROOT / ".claude"
    settings_dir.mkdir(exist_ok=True)

    settings_file = settings_dir / "settings.json"
    if settings_file.exists():
        print("  Claude settings: already configured")
        return

    # Build the env block for weaviate-kg MCP
    weaviate_port = os.environ.get("WEAVIATE_PORT", str(DEFAULT_WEAVIATE_PORT))
    weaviate_grpc = os.environ.get("WEAVIATE_GRPC_PORT", str(DEFAULT_WEAVIATE_GRPC_PORT))
    ollama_port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))
    code_embed_port = os.environ.get("CODE_EMBED_PORT", str(DEFAULT_CODE_EMBED_PORT))

    settings = {
        "permissions": {
            "allow": [
                "Bash(git *)",
                "Bash(python *)",
                "Bash(.claude/scripts/*)",
            ],
        },
        "env": {
            "WEAVIATE_URL": f"http://localhost:{weaviate_port}",
            "OLLAMA_URL": f"http://localhost:{ollama_port}",
            "GRPC_PORT": str(weaviate_grpc),
            "EMBEDDING_MODEL": embed_config["text_model"],
            "ACTIVE_EMBEDDING": "qwen3",
            "KG_COLLECTION": "KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "Development",
            "CODE_EMBED_BACKEND": embed_config["code_backend"],
            "CODE_EMBED_SERVICE_URL": f"http://localhost:{code_embed_port}",
        },
    }

    settings_file.write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 9b: Install agents + skills from templates/
# ---------------------------------------------------------------------------

def _install_agents_and_skills(args: argparse.Namespace) -> None:
    """Copy agents and skills from templates/ into .claude/, substituting paths.

    Free-tier agents live at templates/agents/free/; MAO-tier at templates/agents/mao/
    (gated on --with-mao-agents). Skills live at templates/skills/.

    Placeholder substitutions applied to copied files:
        {{ORCHESTRATOR_ROOT}} → this install directory
        {{PROJECTS_ROOT}}     → parent directory
        {{HOME}}              → user home directory
    """
    print("[9b/10] Installing agents and skills ... ", flush=True)

    templates_dir = PROJECT_ROOT / "templates"
    claude_dir = PROJECT_ROOT / ".claude"
    agents_dst = claude_dir / "agents"
    skills_dst = claude_dir / "skills"

    subs = {
        "{{ORCHESTRATOR_ROOT}}": str(PROJECT_ROOT),
        "{{PROJECTS_ROOT}}": str(PROJECT_ROOT.parent),
        "{{HOME}}": str(Path.home()),
    }

    def _copy_with_subs(src: Path, dst: Path) -> None:
        content = src.read_text(encoding="utf-8")
        for key, val in subs.items():
            content = content.replace(key, val)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    installed_agents = 0
    skipped_agents = 0
    if args.with_agents:
        agents_dst.mkdir(parents=True, exist_ok=True)
        free_src = templates_dir / "agents" / "free"
        if free_src.exists():
            for agent_file in sorted(free_src.glob("*.md")):
                target = agents_dst / agent_file.name
                if target.exists():
                    skipped_agents += 1
                    continue
                _copy_with_subs(agent_file, target)
                installed_agents += 1

    installed_mao = 0
    if args.with_mao_agents:
        agents_dst.mkdir(parents=True, exist_ok=True)
        mao_src = templates_dir / "agents" / "mao"
        if mao_src.exists():
            for agent_file in sorted(mao_src.glob("*.md")):
                target = agents_dst / agent_file.name
                if target.exists():
                    continue
                _copy_with_subs(agent_file, target)
                installed_mao += 1

    installed_skills = 0
    skipped_skills = 0
    if args.with_skills:
        skills_src = templates_dir / "skills"
        if skills_src.exists():
            skills_dst.mkdir(parents=True, exist_ok=True)
            for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
                target = skills_dst / skill_dir.name
                if target.exists():
                    skipped_skills += 1
                    continue
                target.mkdir(parents=True, exist_ok=True)
                for f in skill_dir.rglob("*"):
                    rel = f.relative_to(skill_dir)
                    out = target / rel
                    if f.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                    elif f.suffix == ".md":
                        _copy_with_subs(f, out)
                    else:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, out)
                installed_skills += 1

    parts = []
    if args.with_agents:
        parts.append(f"{installed_agents} free agents"
                     + (f" ({skipped_agents} already present)" if skipped_agents else ""))
    if args.with_mao_agents:
        parts.append(f"{installed_mao} MAO agents")
    if args.with_skills:
        parts.append(f"{installed_skills} skills"
                     + (f" ({skipped_skills} already present)" if skipped_skills else ""))
    if not parts:
        print("  skipped (--no-agents --no-skills)")
    else:
        print("  " + ", ".join(parts))


# ---------------------------------------------------------------------------
# Step 10: Claude CLI
# ---------------------------------------------------------------------------

def _check_claude_cli() -> None:
    print("[10/10] Checking Claude CLI ... ", end="", flush=True)
    if shutil.which("claude"):
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = result.stdout.strip() or "found"
            print(f"OK ({version})")
        except (subprocess.TimeoutExpired, OSError):
            print("found (version check timed out)")
    else:
        print("NOT FOUND")
        print("  Claude Code CLI is required to use the orchestrator.")
        print("  Install: npm install -g @anthropic-ai/claude-code")
        print("  Requires: Node.js 18+ (https://nodejs.org)")


# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------

def _print_next_steps(sysinfo: SystemInfo, args: argparse.Namespace) -> None:
    if sysinfo.os_name == "Windows":
        activate = r".venv\Scripts\activate"
    else:
        activate = "source .venv/bin/activate"

    print("Next steps:")
    print()
    print(f"  1. Open this project in VS Code:")
    print(f"     code {PROJECT_ROOT}")
    print()
    print(f"  2. Start a Claude Code session (the orchestrator activates automatically):")
    print(f"     claude")
    print()
    print(f"  3. Or activate the venv for manual scripts:")
    print(f"     {activate}")
    print()

    if not shutil.which("claude"):
        print("  IMPORTANT: Install Claude Code CLI first:")
        print("     npm install -g @anthropic-ai/claude-code")
        print()

    if args.no_containers:
        print("  NOTE: You skipped container setup. Start Weaviate and Ollama")
        print("  manually before using the orchestrator.")
        print()

    print("  Documentation: docs/")
    print("  Troubleshooting: docs/TROUBLESHOOTING.md")
    print("  Report issues: https://github.com/hotak92/vibecoded-orchestrator/issues")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
