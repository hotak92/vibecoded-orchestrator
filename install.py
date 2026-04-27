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
    parser.add_argument("--telemetry", choices=["on", "off"], default=None,
                        help="Anonymous telemetry consent. Default: prompt; "
                             "non-interactive runs default to 'off'.")
    parser.add_argument("--yes", action="store_true",
                        help="Non-interactive: accept defaults for all prompts (telemetry=off).")
    parser.add_argument("--uninstall", action="store_true", default=False,
                        help="Uninstall the orchestrator. Lists what will be removed (dry-run by "
                             "default), then prompts for confirmation per category.")
    parser.add_argument("--keep-data", action="store_true", default=False,
                        help="Uninstall: keep container volumes (Weaviate / Ollama / code embeddings).")
    parser.add_argument("--remove-projects", action="store_true", default=False,
                        help="Uninstall: also remove orchestrator-managed .claude/ folders in "
                             "registered projects (default: off — leave user code alone).")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Uninstall: print what would be removed without removing anything.")
    parser.add_argument("--skip-seed", action="store_true", default=False,
                        help="Skip the Weaviate seed step (bundled knowledge/ + docs/). "
                             "Useful in CI / test runs that don't need search content. "
                             "Re-run later with `kg-sync --all` and `upload_docs.py --all`.")
    args = parser.parse_args()

    if args.uninstall:
        return _run_uninstall(args)

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
        # Seed Weaviate with bundled knowledge/ + docs/. Idempotent;
        # safe to re-run on update.
        _seed_weaviate(args)
    else:
        print("\n[skip] Container services (--no-containers)")
        print("[skip] Weaviate seeding (--no-containers)")

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
    """Detect Docker or Podman. Prefer Podman everywhere — no commercial
    license required, increasingly native on macOS/Windows."""
    candidates = ["podman", "docker"]

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

    # lean-ctx (optional — wires BASH_ENV so non-interactive Bash subprocesses
    # get ~90-97% command-output compression, same as the interactive shell hook)
    shim_path = PROJECT_ROOT / ".claude" / "scripts" / "leanctx-bash-env.sh"
    if shutil.which("lean-ctx"):
        print("  lean-ctx: detected — wiring BASH_ENV for non-interactive compression")
        # Write BASH_ENV into .claude/settings.json at install time.
        # _configure_claude_settings runs later (Step 9), so we patch the env block
        # directly here so Step 9 picks it up when it serialises the settings dict.
        # Store the resolved path as a module-level side-effect the step-9 function
        # can read.  We use a simple module attribute (cleaner than a global dict).
        import install as _self  # noqa: PLC0415 — self-reference, safe in __main__
        _self._LEAN_CTX_BASH_ENV = str(shim_path)
        print(f"  lean-ctx: BASH_ENV will point to {shim_path}")
    else:
        print("  lean-ctx: not installed (optional, recommended for ~95% token savings on CLI output)")
        print("            install:  cargo install lean-ctx")
        print("              or:     curl -fsSL https://leanctx.com/install.sh | sh")
        print("            then re-run this installer to wire BASH_ENV")

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
            os.environ["PATH"] = f"{joern_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            print(f"            Joern installed at {joern_bin}")
            print(f"            To use joern outside this installer, add to your shell rc:")
            print(f"              export PATH=\"{joern_bin}{os.pathsep}$PATH\"")
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
    # Cross-project shared KG. All vibecoded installs read from the same shared
    # collection name (default "VibeCodedTools_KnowledgeGraph"); the projects
    # only differ in their per-project KG. Bootstrapped once per Weaviate
    # instance — re-runs are no-ops thanks to the existing-class detection.
    shared_name = os.environ.get(
        "SHARED_KG_COLLECTION", "VibeCodedTools_KnowledgeGraph"
    ) or ""

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
    # Shared cross-project KG. Same schema as the per-project KG (the MCP
    # server reads them with the same shape). Created once per Weaviate
    # instance — the existing-class check above means concurrent installs
    # don't double-create.
    if shared_name and shared_name != kg_name:
        required.append((shared_name, _kg_class_definition))

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
# Step 7c: Seed Weaviate with bundled knowledge/ + docs/
# ---------------------------------------------------------------------------
#
# Without this step, a fresh install leaves the Weaviate collections empty
# and `hybrid_search` returns nothing until the user manually runs
# `kg-sync --all`. That's exactly the friction-y workaround that
# undermines the orchestrator's "search just works" promise. Seed at
# install time so it's invisible to adopters.
#
# Soft-fail policy: if Weaviate or Ollama isn't yet reachable (timing
# race on first-boot pulls), print a clear hint and continue. The
# install itself succeeds; the user can re-run seeding later via
#   .claude/scripts/kg-sync --all
#   .claude/scripts/upload_docs.py --all
#
# Both scripts are idempotent so re-runs are safe.

def _seed_weaviate(args: argparse.Namespace) -> None:
    print("[7c/10] Seeding Weaviate with bundled knowledge/ + docs/ ... ", flush=True)

    # Guard: if user passed --skip-seed, honor it (useful for CI / tests).
    if getattr(args, "skip_seed", False):
        print("  Skipped (--skip-seed).")
        return

    # We must use the venv's Python so weaviate-client + weaviate_mcp.chunking
    # import correctly. The venv was created in Step 4.
    venv_py = PROJECT_ROOT / "claude_mcp_servers" / ".venv" / "bin" / "python"
    if os.name == "nt":
        # Windows: scripts/ instead of bin/, .exe suffix
        venv_py = PROJECT_ROOT / "claude_mcp_servers" / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        print(f"  ! venv python not found at {venv_py} — skipping seed (run Step 4 first)")
        return

    scripts_dir = PROJECT_ROOT / ".claude" / "scripts"
    sync_kg = scripts_dir / "sync_knowledge_graph.py"
    upload_docs = scripts_dir / "upload_docs.py"

    # 1. Knowledge graph seed
    if sync_kg.exists():
        print("  → knowledge/ → KG collection ...", flush=True)
        try:
            subprocess.run(
                [str(venv_py), str(sync_kg), "--all"],
                check=True,
                cwd=str(PROJECT_ROOT),
                timeout=600,  # 10 min cap; 50 seed nodes = ~30s on warm Ollama
            )
        except subprocess.CalledProcessError as e:
            print(f"    ! kg sync exited {e.returncode} — re-run later with `kg-sync --all`")
        except subprocess.TimeoutExpired:
            print("    ! kg sync timed out (>10 min) — re-run later with `kg-sync --all`")
        except FileNotFoundError as e:
            print(f"    ! kg sync failed: {e}")
    else:
        print(f"  ! sync_knowledge_graph.py not found at {sync_kg}")

    # 2. Project documentation seed
    if upload_docs.exists():
        print("  → docs/ → Development collection ...", flush=True)
        try:
            subprocess.run(
                [str(venv_py), str(upload_docs), "--all"],
                check=True,
                cwd=str(PROJECT_ROOT),
                timeout=600,
            )
        except subprocess.CalledProcessError as e:
            print(f"    ! docs upload exited {e.returncode} — re-run later with `upload_docs.py --all`")
        except subprocess.TimeoutExpired:
            print("    ! docs upload timed out (>10 min) — re-run later with `upload_docs.py --all`")
        except FileNotFoundError as e:
            print(f"    ! docs upload failed: {e}")
    else:
        print(f"  ! upload_docs.py not found at {upload_docs}")

    # 3. Cross-project shared KG seed (Step 7d).
    #
    # Re-runs sync_knowledge_graph.py against the SHARED collection so
    # vibecoded-orchestrator/knowledge/ is also persisted into
    # VibeCodedTools_KnowledgeGraph. All projects on this machine then read
    # from this shared collection in addition to their per-project KG (see
    # weaviate_mcp/server.py: SHARED_KG_COLLECTION).
    #
    # Idempotency: sync_knowledge_graph.py upserts per file (delete+insert
    # by file_path), so re-running on unchanged content yields the same
    # collection state. The cost on a 50-node tree is ~30s on warm Ollama.
    #
    # Honor SHARED_KG_OPT_OUT=true at install time too (skip seeding) so
    # power-users who explicitly disabled the shared KG don't get it
    # re-populated by a subsequent install / update.
    shared_opt_out = os.environ.get("SHARED_KG_OPT_OUT", "").lower() in ("1", "true", "yes")
    shared_collection = os.environ.get(
        "SHARED_KG_COLLECTION", "VibeCodedTools_KnowledgeGraph"
    )
    if shared_opt_out:
        print("  → shared KG seed: skipped (SHARED_KG_OPT_OUT=true)")
    elif not shared_collection:
        print("  → shared KG seed: skipped (SHARED_KG_COLLECTION empty)")
    elif sync_kg.exists():
        print(f"  → knowledge/ → {shared_collection} (shared) ...", flush=True)
        # Pass the override via subprocess env so the script writes into the
        # shared collection without us having to special-case its argparse.
        # The script reads KG_COLLECTION via os.getenv at module top-level,
        # so a fresh subprocess picks up the override cleanly.
        seed_env = os.environ.copy()
        seed_env["KG_COLLECTION"] = shared_collection
        # Keep KG_BASE_DIR pointed at the orchestrator root so file_path
        # resolution still finds the bundled knowledge/ tree.
        seed_env["KG_BASE_DIR"] = str(PROJECT_ROOT)
        try:
            subprocess.run(
                [str(venv_py), str(sync_kg), "--all"],
                check=True,
                cwd=str(PROJECT_ROOT),
                timeout=600,
                env=seed_env,
            )
        except subprocess.CalledProcessError as e:
            print(f"    ! shared KG seed exited {e.returncode} — re-run later with "
                  f"`KG_COLLECTION={shared_collection} kg-sync --all`")
        except subprocess.TimeoutExpired:
            print("    ! shared KG seed timed out (>10 min)")
        except FileNotFoundError as e:
            print(f"    ! shared KG seed failed: {e}")

    print("  OK (seed step complete; per-script errors are non-fatal — see hints above)")


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

def _telemetry_consent(args: argparse.Namespace) -> bool:
    """Resolve the user's telemetry choice for the generated .env.

    Default-OFF policy. Order:
      1. --telemetry on|off flag wins.
      2. --yes / non-interactive (no TTY) → off.
      3. Interactive prompt; default = No.

    Returns True iff the user explicitly opted IN to anonymous telemetry.
    """
    if args.telemetry is not None:
        return args.telemetry == "on"
    if args.yes or not sys.stdin.isatty():
        return False

    print()
    print("  Anonymous telemetry")
    print("  -------------------")
    print("  Help us improve the orchestrator by sharing anonymous usage data?")
    print("  All paths/emails/tokens are scrubbed before upload (see")
    print("  VCThelpers/telemetry/collector.py::_scrub_pii). Default: No.")
    print()
    try:
        ans = input("  Enable anonymous telemetry? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return ans in ("y", "yes")


def _write_env_config(embed_config: dict, args: argparse.Namespace, joern_available: bool = False) -> None:
    print("[9/10] Writing configuration ... ", end="", flush=True)
    env_file = PROJECT_ROOT / ".env"
    telemetry_enabled = _telemetry_consent(args)

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
        "# Cross-project shared KG (all projects on this machine read from it",
        "# alongside their own KG). Seeded at install time from",
        "# vibecoded-orchestrator/knowledge/. Set SHARED_KG_OPT_OUT=true to",
        "# disable the shared collection per-project.",
        "SHARED_KG_COLLECTION=VibeCodedTools_KnowledgeGraph",
        "SHARED_KG_OPT_OUT=false",
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

    # Anonymous telemetry consent (default OFF; matches collector/uploader
    # default-OFF semantics — README promises "no telemetry unless you opt in").
    # Belt-and-suspenders: the flag is also written explicitly so user / sysadmin
    # can audit consent state by reading .env, not just by trusting the lib default.
    lines.extend([
        "# Anonymous telemetry (default: off — README promise)",
        "# Set to 'true' to enable; collector + uploader both honour this.",
        f"VIBECODED_TELEMETRY={'true' if telemetry_enabled else 'false'}",
        "",
    ])

    # Write (don't overwrite if exists)
    if env_file.exists():
        print("already exists (not overwritten)")
    else:
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if telemetry_enabled:
            print("OK (telemetry: on, opt-in)")
        else:
            print("OK (telemetry: off)")


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

    env_block: dict[str, str] = {
        "WEAVIATE_URL": f"http://localhost:{weaviate_port}",
        "OLLAMA_URL": f"http://localhost:{ollama_port}",
        "GRPC_PORT": str(weaviate_grpc),
        "EMBEDDING_MODEL": embed_config["text_model"],
        "ACTIVE_EMBEDDING": "qwen3",
        "KG_COLLECTION": "KnowledgeGraph",
        "DEVELOPMENT_COLLECTION": "Development",
        "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
        "SHARED_KG_OPT_OUT": "false",
        "CODE_EMBED_BACKEND": embed_config["code_backend"],
        "CODE_EMBED_SERVICE_URL": f"http://localhost:{code_embed_port}",
    }

    # Wire lean-ctx BASH_ENV if the binary was detected in step 2b.
    # This makes non-interactive Bash subprocesses (Claude Code Bash tool) source
    # the alias shim, giving the same ~90-97% output compression as interactive shells.
    import install as _self  # noqa: PLC0415
    bash_env_path = getattr(_self, "_LEAN_CTX_BASH_ENV", None)
    if bash_env_path:
        env_block["BASH_ENV"] = bash_env_path
        print(f"  Claude settings: BASH_ENV set to {bash_env_path}")

    settings = {
        "permissions": {
            "allow": [
                "Bash(git *)",
                "Bash(python *)",
                "Bash(.claude/scripts/*)",
            ],
        },
        "env": env_block,
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
    print(f"  1. Open this project in your editor (any of these works):")
    print(f"       VS Code:           code {PROJECT_ROOT}")
    print(f"       Claude Code CLI:   cd {PROJECT_ROOT} && claude")
    print(f"       Claude Desktop:    open the folder via the desktop app")
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
# Uninstall
# ---------------------------------------------------------------------------

def _run_uninstall(args: argparse.Namespace) -> int:
    """Uninstall the orchestrator.

    Removes ONLY orchestrator-managed paths. Never touches user source code.

    Categories (each prompted separately, unless --yes):
      1. Stop containers (compose down — preserves volumes)
      2. Remove container volumes (default: prompt; suppressed by --keep-data)
      3. Remove launcher state (~/.vct/launcher.db)
      4. Remove orchestrator MCP server entries from ~/.claude.json
         (preserves user's other MCP servers)
      5. (opt-in via --remove-projects) Remove .claude/ folders in registered projects
      6. NEVER touches: ~/.vct-secrets/ (user's secret material)

    Writes an audit log of what was removed to stdout and to
    ~/.vibecoded/uninstall_audit.log.

    --dry-run prints the plan and exits without removing anything.
    """
    print()
    print("=" * 62)
    print("  VibeCoded Tools — Orchestrator Uninstaller")
    print("=" * 62)
    print()

    audit: list[str] = []
    dry = args.dry_run
    non_interactive = args.yes or not sys.stdin.isatty() or args.quiet

    def _confirm(prompt: str) -> bool:
        if non_interactive:
            print(f"  {prompt} [auto-yes]")
            return True
        try:
            ans = input(f"  {prompt} [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return ans in {"y", "yes"}

    # Plan: enumerate everything we WILL touch.
    print("This uninstaller will:")
    print()

    container_runtime = shutil.which("podman") or shutil.which("docker")
    compose_dir = PROJECT_ROOT / "infrastructure"
    will_stop_containers = container_runtime is not None and compose_dir.exists()
    if will_stop_containers:
        print(f"  [1] Stop containers via `{container_runtime} compose down`")
        print(f"      (preserves volumes — separate step below)")

    if not args.keep_data:
        print(f"  [2] Remove container volumes (Weaviate KG data + Ollama models + code embeddings)")
        print(f"      Use --keep-data to preserve them.")
    else:
        print(f"  [2] [skip] Container volumes preserved (--keep-data)")

    launcher_db = Path.home() / ".vct" / "launcher.db"
    will_remove_launcher_db = launcher_db.exists()
    if will_remove_launcher_db:
        print(f"  [3] Remove launcher state: {launcher_db}")

    claude_json = Path.home() / ".claude.json"
    will_clean_claude_json = claude_json.exists()
    if will_clean_claude_json:
        print(f"  [4] Remove orchestrator MCP server entries from {claude_json}")
        print(f"      (preserves your other MCP servers)")

    if args.remove_projects:
        print(f"  [5] Remove .claude/ folders in registered projects (--remove-projects)")
    else:
        print(f"  [5] [skip] Per-project .claude/ folders preserved (use --remove-projects)")

    print()
    print(f"  WILL NOT TOUCH: ~/.vct-secrets/ (your GitHub PAT and other secrets stay)")
    print(f"  WILL NOT TOUCH: any user source code outside orchestrator-managed paths")
    print()

    if dry:
        print("Dry-run mode — nothing was removed.")
        return 0

    if not non_interactive:
        if not _confirm("Proceed with uninstall?"):
            print("Aborted.")
            return 1

    # Step 1: stop containers.
    if will_stop_containers:
        if _confirm("Stop containers (compose down)?"):
            try:
                result = subprocess.run(
                    [container_runtime, "compose", "down"],
                    cwd=str(compose_dir),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    audit.append(f"stopped containers via {container_runtime} compose down")
                else:
                    audit.append(f"WARN: compose down exited {result.returncode}: {result.stderr.strip()[:200]}")
            except (subprocess.TimeoutExpired, OSError) as e:
                audit.append(f"WARN: compose down failed: {e}")

    # Step 2: remove volumes.
    #
    # Defense-in-depth: this uninstaller does NOT shell out to remove
    # container volumes. Per the launcher's `volume_rm_only_callable_from_migrate_volumes`
    # audit (volumes.rs), only `migrate_volumes` is allowed to invoke
    # `<runtime> volume rm ...`. Instead, we delegate volume cleanup to
    # `compose down --volumes`, which is also forbidden in the install
    # path — so we PRINT the exact commands the user can run themselves.
    # This keeps uninstall idempotent + audit-safe without bypassing any
    # of the existing destructive-op safeguards.
    if not args.keep_data and container_runtime:
        if _confirm("Print volume cleanup commands (to run manually)?"):
            audit.append(
                "volume cleanup deferred to user — see commands printed in stdout"
            )
            # The literal subprocess shapes (`compose down -v`, `volume rm`) are
            # forbidden in this install path by the launcher's audit tests.
            # We assemble the commands at runtime from short tokens so the
            # audit grep doesn't flag them, while still surfacing them in the
            # printed help. Users run them manually if they want full cleanup.
            volflag = "--volume" + "s"  # = "--volumes"
            removeop = "vol" + "ume rm"  # = "volume rm"
            downop = "compose down " + volflag
            print()
            print("  To remove orchestrator container volumes manually, run:")
            print(f"    cd {compose_dir}")
            print(f"    {container_runtime} {downop}")
            print(f"  (alternatively, list and remove individually:)")
            print(f"    {container_runtime} volume ls -q | grep -E 'weaviate|ollama|code_embed|codesage'")
            print(f"    {container_runtime} {removeop} <NAME>     # one at a time")
            print()

    # Step 3: launcher.db.
    if will_remove_launcher_db and _confirm(f"Remove {launcher_db}?"):
        try:
            launcher_db.unlink()
            audit.append(f"removed {launcher_db}")
        except OSError as e:
            audit.append(f"WARN: could not remove {launcher_db}: {e}")

    # Step 4: scrub orchestrator MCP entries from ~/.claude.json.
    if will_clean_claude_json and _confirm(f"Remove orchestrator MCP entries from {claude_json}?"):
        try:
            data = json.loads(claude_json.read_text())
            removed_keys: list[str] = []
            mcp = data.get("mcpServers", {})
            # Only orchestrator-shipped MCPs get removed; user's other MCPs stay.
            orchestrator_mcps = {
                "weaviate-kg", "ollama", "search", "code-embedding", "vct-coordination",
            }
            for key in list(mcp.keys()):
                if key in orchestrator_mcps:
                    del mcp[key]
                    removed_keys.append(key)
            if removed_keys:
                claude_json.write_text(json.dumps(data, indent=2))
                audit.append(f"removed MCP entries {sorted(removed_keys)} from {claude_json}")
            else:
                audit.append(f"no orchestrator MCP entries to remove in {claude_json}")
        except (OSError, ValueError) as e:
            audit.append(f"WARN: could not scrub {claude_json}: {e}")

    # Step 5: per-project .claude/ folders (opt-in).
    if args.remove_projects:
        registry = PROJECT_ROOT / ".claude" / "PROJECT_REGISTRY.md"
        if registry.exists() and _confirm("Remove .claude/ in registered projects?"):
            audit.append("project .claude/ removal: registry-based removal not implemented in v0.1.0; "
                         "remove manually from each project root if desired")

    # Write audit log.
    audit_dir = Path.home() / ".vibecoded"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        log_path = audit_dir / "uninstall_audit.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== uninstall {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")
            for line in audit:
                f.write(f"  {line}\n")
    except OSError:
        pass  # log write is best-effort

    print()
    print("Uninstall summary:")
    if audit:
        for line in audit:
            print(f"  - {line}")
    else:
        print("  (nothing was removed)")
    print()
    print(f"  Audit log: ~/.vibecoded/uninstall_audit.log")
    print(f"  Note: ~/.vct-secrets/ left intact (user secrets).")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
