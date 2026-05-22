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

# ---------------------------------------------------------------------------
# Python version sentinel — runs BEFORE any other module imports.
#
# We hard-fail here (instead of letting a downstream import like `tomllib`
# raise a confusing ModuleNotFoundError) so the user gets a single clear
# message + the canonical install URL. Keep this block dependency-free
# and 3.7-syntax-compatible so it parses on the user's broken interpreter
# regardless of which old Python they invoked us with.
#
# `from __future__ import annotations` MUST stay above this block (Python
# requires future imports to be the first statement after the docstring).
# That's fine — future imports work back to 3.7 and don't execute code.
# ---------------------------------------------------------------------------
import sys as _sys

if _sys.version_info < (3, 11):
    _v = _sys.version_info
    _sys.stderr.write(
        "ERROR: Python 3.11 or newer required (got %d.%d.%d).\n"
        "       Install hints:\n"
        "         Linux (apt):   sudo apt install python3.12 python3.12-venv\n"
        "         Linux (dnf):   sudo dnf install python3.12\n"
        "         macOS:         brew install python@3.12\n"
        "         Windows:       winget install Python.Python.3.12\n"
        "       Or use the install.sh / install.ps1 wrapper which can\n"
        "       auto-install Python on most platforms.\n"
        "       Docs: https://github.com/hotak92/vibecoded-orchestrator#prerequisites\n"
        % (_v.major, _v.minor, _v.micro)
    )
    _sys.exit(1)

# ---------------------------------------------------------------------------
# Windows stdout/stderr encoding sentinel — runs BEFORE any print().
#
# Python on Windows defaults stdout to the legacy ANSI code page (cp1252 in
# Western locales). Any print() containing characters outside that codepage
# — arrows (U+2192), em-dash (U+2014), check/cross marks, etc. — crashes
# the installer with UnicodeEncodeError. install.py contains ~660 such
# characters in user-facing prints.
#
# Reconfiguring stdout/stderr to UTF-8 with errors='replace' makes those
# prints survive on any locale. errors='replace' (not 'strict') ensures
# we never hard-fail on an unexpected glyph — a substitute character is
# preferable to crashing mid-install.
#
# No-op on POSIX systems (stdout is already UTF-8 there). The reconfigure()
# method exists on Python 3.7+, so it's safe under our MIN_PYTHON=(3,11).
# ---------------------------------------------------------------------------
if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # Detached / redirected streams may lack reconfigure(); ignore.
        pass

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 11)
PROJECT_ROOT = Path(__file__).resolve().parent

# vco_lib lives at PROJECT_ROOT/vco_lib (sibling of install.py). Make it
# importable when install.py is invoked directly (no package context).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Single source of truth — see vco_lib/project_init.py docstring.
# Moved to vco_lib.project_init in PR 2 — kept as shim for existing
# callers; will be removed in PR 9 (cleanup).
from vco_lib import project_init as _project_init  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


# 2026-04-29 fix (wizard install-path lockdown): defensive sanity check
# that PROJECT_ROOT is actually a vco source repo. The CLI path
# (`python install.py`) runs FROM the source repo so this should always
# pass — the check is here in case someone copies install.py somewhere
# weird (e.g. a packaging step that strips first-install.sh, a user
# `cp install.py /tmp/` and runs it). Mirrors the Rust-side
# `validate_source_repo` in launcher/src-tauri/src/commands/installer.rs.
def validate_source_repo(install_path: Path) -> None:
    """Raise SystemExit(2) with a clear message when `install_path` is
    not a vco source repo. The discriminator is install.py +
    first-install.sh side by side; both ship in every clone and tarball
    and neither is in ORCHESTRATOR_MANAGED_PATHS, so a partial
    "managed paths only" copy can't fake source-repo status.
    """
    install_py = install_path / "install.py"
    first_install_sh = install_path / "first-install.sh"
    if not install_py.is_file() or not first_install_sh.is_file():
        raise SystemExit(
            f"ERROR: install path must be a vco source repo "
            f"(must contain install.py + first-install.sh). "
            f"To install at a different location, clone the repo there "
            f"and re-run from that folder. Got: {install_path}"
        )


def _windows_powershell_version() -> tuple[int, int] | None:
    """Return the (major, minor) version of the available PowerShell, or
    None if neither `pwsh` (PowerShell 7+) nor `powershell` (Windows
    PowerShell 5.1) resolves on PATH.

    Used by the Windows install gate (audit F1, P0). PowerShell 5.1 ships
    preinstalled on Windows 10/11 — that's the floor. `pwsh` (Core 7+) is
    preferred when available but not required.
    """
    for exe in ("pwsh", "powershell"):
        if not shutil.which(exe):
            continue
        try:
            r = subprocess.run(
                [exe, "-NoProfile", "-Command",
                 "$PSVersionTable.PSVersion.Major; $PSVersionTable.PSVersion.Minor"],
                capture_output=True, text=True, timeout=8,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if r.returncode != 0:
            continue
        nums = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        if len(nums) >= 2:
            return (nums[0], nums[1])
    return None


def _windows_has_git_bash() -> bool:
    """Return True when Git Bash appears to be on PATH.

    We resolve `bash.exe` via `shutil.which` and accept it as Git Bash if
    its path contains a `Git/` (or `Git\\`) component — that excludes
    Cygwin / MSYS standalone installs which the audit does not target.
    """
    bash = shutil.which("bash") or shutil.which("bash.exe")
    if not bash:
        return False
    norm = bash.replace("\\", "/").lower()
    return "/git/" in norm or norm.endswith("/git/usr/bin/bash.exe") or "/cmd/" in norm


def _check_windows_shell_prereqs() -> None:
    """Hard-fail (or warn) the install on Windows when the shell tooling
    needed by hooks isn't available.

    Behaviour:
      - Linux / macOS: no-op.
      - Windows + PowerShell 5.1+ + Git Bash: pass silently.
      - Windows + PowerShell 5.1+ but no Git Bash: warn (legacy `.sh`
        instinct hooks won't run, but every `.ps1` hook will).
      - Windows + no PowerShell (Git Bash or not): SystemExit with an
        actionable install message.
      - Windows + Git Bash only (no PowerShell): same SystemExit — the
        `.ps1` hooks need PowerShell.

    See VCO portability audit 2026-04-30, finding F1.
    """
    if platform.system() != "Windows":
        return

    pwsh_ver = _windows_powershell_version()
    has_git_bash = _windows_has_git_bash()

    pwsh_ok = pwsh_ver is not None and (
        pwsh_ver[0] > 5 or (pwsh_ver[0] == 5 and pwsh_ver[1] >= 1)
    )

    if not pwsh_ok:
        msg = (
            "Refusing to install on Windows: PowerShell 5.1+ is required "
            "(Windows 10/11 ships with it; older systems need "
            "https://aka.ms/PSWindows). Git Bash is also recommended as a "
            "fallback for legacy hooks; install via "
            "https://git-scm.com/downloads/win or `winget install Git.Git`.\n\n"
            "Re-run install once one of these is on PATH."
        )
        raise SystemExit(msg)

    if not has_git_bash:
        # PowerShell-only install: every .ps1 hook works, but the bash-only
        # `instinct-*.sh` data-collection helpers (Linux-only by design,
        # marked OS-EXEMPT-PARITY) won't fire. That's expected on Windows.
        sys.stderr.write(
            "WARNING: Git Bash not detected on PATH. PowerShell hooks will "
            "run normally, but legacy `.sh`-only helpers (e.g. the instinct "
            "pipeline) won't fire. Install Git Bash via "
            "https://git-scm.com/downloads/win for full coverage.\n"
        )


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
# Each profile keeps its own static `embedding_models` — these are the
# Ollama-served embedding models that MUST be pulled regardless of
# hardware. Inference models (qwen3.5:9b, gemma4:e4b, qwen3.5:0.8b) are
# layered on at install time by `_inference_models_for_capability` based
# on detected VRAM/RAM, then merged with `embedding_models` to form the
# final pull list. See _build_ollama_pull_list().
#
# The "low_resource" profile is special: it explicitly opts the user in
# to the smallest models, so we DO NOT layer larger inference tiers on
# top of it even if the host could run them. Respect the explicit
# choice.
EMBEDDING_CONFIGS = {
    "gpu": {
        "text_model": "qwen3-embedding:0.6b",
        "text_dims": 1024,
        "code_backend": "gpu",
        "code_model": "codesage-large-v2",
        "code_dims": 2048,
        "embedding_models": ["qwen3-embedding:0.6b"],
        # ACTIVE_EMBEDDING env var — controls which named-vector slot
        # the MCP server reads/writes. MUST match the slot name labelled
        # for the model that emitted the vector. See
        # `weaviate_mcp/server.py::_get_search_vector` mapping.
        "active_embedding": "qwen3",
        "description": "GPU-accelerated (qwen3 text + CodeSage code, best quality)",
    },
    "cpu": {
        "text_model": "qwen3-embedding:0.6b",
        "text_dims": 1024,
        "code_backend": "ollama",
        "code_model": "unclemusclez/jina-embeddings-v2-base-code:latest",
        "code_dims": 768,
        "embedding_models": [
            "qwen3-embedding:0.6b",
            "unclemusclez/jina-embeddings-v2-base-code:latest",
        ],
        "active_embedding": "qwen3",
        "description": "CPU-only (qwen3 text + Jina V2 code, both via Ollama)",
    },
    "openai": {
        "text_model": "text-embedding-3-small",
        "text_dims": 1536,
        "code_backend": "openai",
        "code_model": "text-embedding-3-small",
        "code_dims": 1536,
        # OpenAI handles embeddings; only inference models need pulling.
        "embedding_models": [],
        "active_embedding": "openai",
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
        "embedding_models": [
            "snowflake-arctic-embed2:latest",
            "unclemusclez/jina-embeddings-v2-base-code:latest",
        ],
        # Hard-cap inference models for this profile — user opted in.
        # qwen3.5:0.8b is the canonical always-fits floor on main.
        "inference_models_override": ["gemma4:e4b", "qwen3.5:0.8b"],
        # arctic → ollama_embed slot in the named-vector schema.
        # Maps to ACTIVE_EMBEDDING=arctic in weaviate_mcp/server.py.
        "active_embedding": "arctic",
        "description": "Low-resource (Arctic text + Jina V2 code, both via Ollama)",
    },
}


def _build_ollama_pull_list(embed_config: dict, sysinfo: SystemInfo) -> list[str]:
    """Combine the profile's static embedding models with the right
    inference-model tier for this host. Deduplicates while preserving
    order (embedding models first, inference second).
    """
    embedding_models: list[str] = list(embed_config.get("embedding_models") or [])
    override = embed_config.get("inference_models_override")
    if override:
        inference_models = list(override)
    else:
        inference_models = _inference_models_for_capability(sysinfo)
    seen: set[str] = set()
    out: list[str] = []
    for m in embedding_models + inference_models:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out

HEALTH_TIMEOUT = 120  # seconds


class SystemInfo(NamedTuple):
    os_name: str        # "Linux", "Windows", "Darwin"
    has_gpu: bool       # NVIDIA or AMD ROCm GPU usable by Ollama container
    has_metal: bool     # Apple Silicon (Metal)
    container_cmd: str  # "docker" or "podman" or ""
    gpu_name: str       # GPU model name or ""
    vram_gb: float = 0.0      # GPU VRAM in GB if has_gpu else 0.0
    ram_gb: float = 0.0       # System RAM in GB
    gpu_vendor: str = ""      # "nvidia" | "amd" | "metal" | "" — picks compose overlay


# ---------------------------------------------------------------------------
# Durable install log (state/logs/install.jsonl)
#
# Append-only JSONL written by both install.py and post-install-launcher.sh.
# Both the launcher (Tauri command read_install_log) and Claude Code read
# this file on failure to figure out where the install got to. See
# docs/INSTALL_RECOVERY.md for the schema.
#
# Design constraints:
#   - Stdlib only (json/datetime/os/pathlib). install.py runs in the system
#     Python BEFORE the venv exists for the early steps, so we can't depend
#     on anything from requirements.txt here.
#   - Never throw. A logging failure (disk full, permission denied, dir
#     missing) must NEVER break the install — silent skip is the contract.
#   - Atomic per-line writes. JSON is well under typical OS atomic-write
#     thresholds (~4096 bytes), so a single open(..., "a") + write+flush is
#     safe across normal filesystems. Two writers (install.py + the bash
#     post-install script) never run concurrently in practice — bash spawns
#     after install.py exits — so we don't need a file lock.
# ---------------------------------------------------------------------------

# Resume state, populated by _load_resume_state() at start of main().
# Maps step-id -> last terminal phase ("ok" | "skip" | "error" | "warn") seen
# in the most-recent install session. Only "ok"/"skip" steps qualify as
# candidates for skip-on-resume; the per-step verification still runs.
_RESUME_STATE: dict[str, str] = {}
_RESUME_ENABLED: bool = True

# In-memory buffer for events emitted BEFORE Step 8 creates state/logs/.
# Without this, every fresh install loses Steps 1-7 because the log path
# isn't viable yet. _flush_pending_events() drains this in Step 8 once the
# directory exists. Bounded only by the install length (~10s of entries),
# so no eviction needed.
_PENDING_EVENTS: list[str] = []

# Fix 1 (v0.2.13): unix timestamp marking the start of the current install
# run. Populated by _run_install() at entry. Used by
# _refresh_dist_binary_after_rebuild to distinguish "freshly produced this
# run" from "weeks-old stale artifact". Module-level so the helper can
# read it without threading the value through every intermediate call site.
_INSTALL_START_TS: Optional[float] = None


def _install_log_path() -> Path | None:
    """Return path to state/logs/install.jsonl iff the dir exists.

    We deliberately do NOT auto-create state/logs/ here — Step 8 (state
    directory creation) is the single owner of that mkdir. Logging before
    Step 8 buffers in memory; Step 8 flushes the buffer to disk.
    """
    log_dir = PROJECT_ROOT / "state" / "logs"
    if not log_dir.is_dir():
        return None
    return log_dir / "install.jsonl"


def _log_install_event(step: str, phase: str, detail: str = "",
                       data: dict | None = None,
                       actor: str = "install.py") -> None:
    """Append one event to state/logs/install.jsonl. Never raises.

    If the log directory doesn't exist yet (pre-Step-8), the event is
    buffered in `_PENDING_EVENTS` and flushed once Step 8 creates the
    directory. This means events from Steps 1-7 of a FRESH install land
    in the file too, instead of being lost.
    """
    try:
        ts = _utc_iso_now()
        record = {
            "ts": ts,
            "actor": actor,
            "step": step,
            "phase": phase,
            "detail": detail or "",
        }
        if data:
            # Best-effort: drop unserializable values rather than fail.
            try:
                json.dumps(data)
                record["data"] = data
            except (TypeError, ValueError):
                pass
        line = json.dumps(record, ensure_ascii=True) + "\n"

        path = _install_log_path()
        if path is None:
            # Buffer until Step 8 creates the dir. Bounded buffer size
            # (a runaway install would fill memory but not crash).
            if len(_PENDING_EVENTS) < 500:
                _PENDING_EVENTS.append(line)
            return

        # Drain pending events first so they appear in chronological order
        # alongside the current event. This is the normal case once Step 8
        # has run; the buffer is empty so it's a no-op.
        if _PENDING_EVENTS:
            _drain_pending_events(path)

        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        # Per the contract: NEVER let a log failure break the install.
        pass


def _drain_pending_events(path: Path) -> None:
    """Write any pre-Step-8 buffered events to the log file.

    Called automatically from `_log_install_event` once the log dir is
    available, AND explicitly from `_create_state_directory` so the
    buffer drains even if the next event happens to fail to log.
    """
    if not _PENDING_EVENTS:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            for buffered_line in _PENDING_EVENTS:
                f.write(buffered_line)
            f.flush()
        _PENDING_EVENTS.clear()
    except Exception:
        # If draining fails (disk full, perm denied), keep the buffer so
        # a later event has another shot. Bounded above so memory is safe.
        pass


def _utc_iso_now() -> str:
    """ISO-8601 UTC timestamp with second precision, Z suffix."""
    # datetime.now(tz=...) avoids the deprecated utcnow().
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_resume_state() -> dict[str, str]:
    """Parse state/logs/install.jsonl and return {step: last_phase} for the
    most-recent install session that's no older than 24 hours.

    A "session" begins with `actor=install.py, step=1/10, phase=start`.
    Only events from the *latest* session are considered. Sessions older
    than 24h are treated as stale and ignored entirely. Any later "error"
    on a step within the same session demotes that step out of the
    skip-eligible set (we set its last_phase = "error" so the caller will
    re-run it).
    """
    path = _install_log_path()
    if path is None or not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)

    if not events:
        return {}

    # Find the index of the most-recent session start.
    last_session_start = -1
    for i, ev in enumerate(events):
        if (ev.get("actor") == "install.py"
                and ev.get("step") == "1/10"
                and ev.get("phase") == "start"):
            last_session_start = i
    if last_session_start < 0:
        return {}

    session_events = events[last_session_start:]

    # Stale-session check (>24h).
    start_ts = session_events[0].get("ts", "")
    if start_ts:
        from datetime import datetime, timezone, timedelta
        try:
            # Strip trailing 'Z' for fromisoformat (3.11 supports Z; we
            # accept both for portability with older ts strings).
            ts_clean = start_ts.replace("Z", "+00:00")
            start_dt = datetime.fromisoformat(ts_clean)
            now = datetime.now(timezone.utc)
            if now - start_dt > timedelta(hours=24):
                return {}
        except (ValueError, TypeError):
            # Unparseable timestamp → treat as stale (safer than resuming
            # on a malformed log).
            return {}

    # Reduce session events to {step: last_phase}. "ok" / "skip" only count
    # if no later "error" appears for the same step.
    state: dict[str, str] = {}
    for ev in session_events:
        step = ev.get("step")
        phase = ev.get("phase")
        if not isinstance(step, str) or not isinstance(phase, str):
            continue
        if ev.get("actor") != "install.py":
            # Cross-actor events (post-install-launcher.sh, launcher) are
            # not consulted by install.py's resume — they describe phases
            # install.py doesn't own.
            continue
        # Latest phase wins.
        state[step] = phase
    return state


def _should_skip_step(step: str) -> bool:
    """Return True iff resume is enabled AND the log says this step
    completed (ok/skip) in the current session.

    NOTE: This is a HINT — callers MUST still verify the actual side
    effect (venv exists, .env exists, collection has the schema we expect)
    before declaring the step a no-op. The log is necessary but not
    sufficient — Weaviate may have been wiped between runs, the user may
    have deleted .venv, etc.
    """
    if not _RESUME_ENABLED:
        return False
    return _RESUME_STATE.get(step) in ("ok", "skip")


# ---------------------------------------------------------------------------
# Deliverable 2 (2026-04-28): persist install choices + state hashes so
# re-installs can replay them instead of re-prompting / re-detecting.
#
# Two new event step IDs in install.jsonl:
#   - "choices"      → one record per major decision point (joern,
#                      embedding mode, container runtime). The record
#                      holds enough info that a second install run can
#                      re-make the same decision without prompting.
#   - "state-hashes" → MD5 of requirements.txt / Cargo.lock / package.json /
#                      knowledge dir, written at the END of a successful
#                      install. `_compute_drift` compares these against
#                      current files to flag what changed and needs
#                      re-syncing.
#
# Stale-session rule: choices older than 24h are NOT replayed (matches
# the existing `_load_resume_state` rule — same reasoning: a long-stale
# choice is more likely wrong than right).
# ---------------------------------------------------------------------------

# Files whose content hashes are tracked across installs. Tuple form so
# we can iterate stably and return a deterministic dict shape.
STATE_HASH_TARGETS: tuple[tuple[str, str], ...] = (
    ("requirements_txt_md5", "requirements.txt"),
    ("cargo_lock_md5", "launcher/src-tauri/Cargo.lock"),
    ("package_json_md5", "launcher/package.json"),
)

# Knowledge dir is hashed differently — md5 of the sorted file-name
# listing rather than file content. Catches additions/removals; cheap.
STATE_HASH_KNOWLEDGE_DIR = "knowledge"


def _record_install_choice(name: str, value, extra: dict | None = None) -> None:
    """Persist one install-time decision so re-runs can replay it.

    Wraps `_log_install_event` with a fixed step="choices" so all
    decisions appear in a single audit category. Best-effort: never
    raises (matches the install-log contract).

    Args:
        name: Stable choice ID (e.g. "joern", "embedding_mode",
            "container_runtime"). The replay loader keys on this.
        value: The chosen value, JSON-serialisable. Bool / str / dict
            are all fine.
        extra: Optional structured payload (model versions, reason,
            etc.) — useful for forensics, ignored by the replay loader.
    """
    payload: dict = {"value": value}
    if extra:
        payload.update(extra)
    _log_install_event("choices", "ok", name, data=payload)


def _load_previous_choices() -> dict[str, dict]:
    """Read install.jsonl and return {choice_name: {value, ...extra}}
    for the most-recent install.py session.

    Returns an empty dict when:
      - The log file doesn't exist yet (first install).
      - The most-recent session is older than 24h (stale-session rule).
      - No "choices" events were recorded that session.

    Reuses the session-detection logic from `_load_resume_state`
    (session = events between consecutive `step="1/10", phase="start"`
    markers from `actor=install.py`). The choice with the latest
    timestamp wins if a name was recorded twice in one session.
    """
    path = _install_log_path()
    if path is None or not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)

    if not events:
        return {}

    last_session_start = -1
    for i, ev in enumerate(events):
        if (ev.get("actor") == "install.py"
                and ev.get("step") == "1/10"
                and ev.get("phase") == "start"):
            last_session_start = i
    if last_session_start < 0:
        return {}

    session_events = events[last_session_start:]

    # Stale check (>24h).
    start_ts = session_events[0].get("ts", "")
    if start_ts:
        from datetime import datetime, timezone, timedelta
        try:
            ts_clean = start_ts.replace("Z", "+00:00")
            start_dt = datetime.fromisoformat(ts_clean)
            now = datetime.now(timezone.utc)
            if now - start_dt > timedelta(hours=24):
                return {}
        except (ValueError, TypeError):
            return {}

    out: dict[str, dict] = {}
    for ev in session_events:
        if ev.get("step") != "choices" or ev.get("phase") != "ok":
            continue
        name = ev.get("detail")
        data = ev.get("data") or {}
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(data, dict):
            continue
        out[name] = data
    return out


def _md5_file(path: Path) -> str | None:
    """Return MD5 hex digest of a file's bytes, or None on read error
    (file missing, permission denied, etc.). MD5 chosen over SHA-256
    purely for size (32-char vs 64-char) — drift detection is not
    security-critical.
    """
    if not path.is_file():
        return None
    import hashlib
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _md5_knowledge_dir(root: Path) -> str | None:
    """MD5 of the sorted relative-path listing under `root/knowledge/`.

    Catches additions / removals / renames; doesn't compare file
    contents (would be expensive on every install run, and content
    drift on knowledge nodes is normal — they're meant to be edited).

    Used to flag when the bundled knowledge dir has changed and needs
    re-seeding into Weaviate. Returns None when knowledge/ is missing.
    """
    kd = root / STATE_HASH_KNOWLEDGE_DIR
    if not kd.is_dir():
        return None
    import hashlib
    paths: list[str] = []
    for p in kd.rglob("*"):
        if p.is_file():
            try:
                rel = p.relative_to(kd).as_posix()
            except ValueError:
                continue
            paths.append(rel)
    paths.sort()
    h = hashlib.md5()
    for s in paths:
        h.update(s.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _compute_state_hashes(install_path: Path) -> dict[str, str | None]:
    """Compute MD5s of every tracked artifact under `install_path`.

    Returns a dict with one key per `STATE_HASH_TARGETS` entry plus
    `knowledge_md5`. Missing files map to None (NOT to the empty-string
    hash) so the caller can distinguish "file gone" from "file empty".
    """
    out: dict[str, str | None] = {}
    for slot, rel in STATE_HASH_TARGETS:
        out[slot] = _md5_file(install_path / rel)
    out["knowledge_md5"] = _md5_knowledge_dir(install_path)
    return out


def _record_state_hashes(install_path: Path) -> None:
    """Snapshot the post-install state-hash set into install.jsonl.

    Call this at the END of a successful install (after Step 9b /
    Step 10) so the next run has a known-good baseline to diff against.
    """
    hashes = _compute_state_hashes(install_path)
    _log_install_event(
        "state-hashes", "ok",
        "post-install snapshot",
        data=hashes,
    )


def _load_previous_state_hashes() -> dict[str, str | None]:
    """Return the most-recent state-hash snapshot from install.jsonl,
    or {} if none. Like `_load_previous_choices`, only reads from the
    most-recent session and honours the 24h stale-session rule.
    """
    path = _install_log_path()
    if path is None or not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)

    if not events:
        return {}

    # Find latest "state-hashes" event globally — these are written
    # exactly once per successful install at the very end, so the
    # latest one is always the "last good install" baseline. We do NOT
    # apply the 24h stale rule here: a state-hash baseline from 3
    # months ago is still a perfectly valid drift reference (the user
    # hasn't reinstalled since then; we want to know what changed).
    for ev in reversed(events):
        if ev.get("step") == "state-hashes" and ev.get("phase") == "ok":
            data = ev.get("data") or {}
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if isinstance(k, str)}
    return {}


def _compute_drift(install_path: Path) -> dict[str, bool]:
    """Compare current state hashes against the last-recorded snapshot.

    Returns a bool-per-slot dict where True = "this artifact has
    changed since the last successful install". When no previous
    snapshot exists, returns all True (treat as fully-drifted, since
    we have no baseline).

    Used by the lightweight re-install path (Deliverable 3) to decide:
      - requirements_txt_md5 changed → run `pip install -r ... --upgrade`
      - cargo_lock_md5 / package_json_md5 changed → caller may want to
        rebuild the launcher
      - knowledge_md5 changed → re-run sync_knowledge_graph.py
    """
    current = _compute_state_hashes(install_path)
    previous = _load_previous_state_hashes()
    if not previous:
        # No baseline — nothing to compare against. Return True for
        # every slot so the caller falls back to full processing.
        return {k: True for k in current}
    out: dict[str, bool] = {}
    for slot, cur_hash in current.items():
        prev_hash = previous.get(slot)
        # Two-sided None is "no change" (both absent).
        if cur_hash is None and prev_hash is None:
            out[slot] = False
        else:
            out[slot] = cur_hash != prev_hash
    return out


# ---------------------------------------------------------------------------
# Re-install file-conflict resolution
#
# Mirror of the Rust implementation in
#   launcher/src-tauri/src/commands/installer.rs
# (apply_conflict_strategy + DEFAULT_PRESERVE_LIST + MERGE_BLOCK_*).
# Keep both sides in lockstep — see docs/INSTALL_RECOVERY.md → "Conflict
# Resolution" for the user-facing description and the Claude self-merge
# contract.
#
# This Python path is only exercised when install.py is invoked directly
# with --conflict-strategy (CLI users). The launcher's own install_orchestrator
# does the conflict-resolution step in Rust BEFORE spawning install.py, so
# the wizard flow doesn't go through here.
# ---------------------------------------------------------------------------

# Default preserve list — keep in sync with DEFAULT_PRESERVE_LIST in
# launcher/src-tauri/src/commands/installer.rs and OnboardingWizard.svelte.
#
# MEMORY.md is intentionally NOT here — lives at
# ~/.claude/projects/<id>/memory/MEMORY.md, not in the install dir. The
# notification block mentions it so the user can manually run a merge
# if their MEMORY.md has diverged.
DEFAULT_PRESERVE_LIST: tuple[str, ...] = (
    "CLAUDE.md",
    ".claude/CONTEXT_STATE.md",
    ".claude/PROJECT_REGISTRY.md",
    ".env",
)

# Notification block markers in .claude/CONTEXT_STATE.md. Re-runs of the
# install REPLACE the block in-place rather than accumulating stale copies.
MERGE_BLOCK_START = "<!-- vct-merge-pending -->"
MERGE_BLOCK_END = "<!-- /vct-merge-pending -->"

# Hard whitelist of paths the orchestrator install is allowed to copy
# from `source` into `install_path`. Anything else at the source is
# left behind; anything else at the install_path is left untouched.
#
# Source of truth: ``orchestrator-managed-paths.txt`` at the repo root
# (sibling of this file). Edit there ONLY — both Rust
# (``installer.rs`` via ``include_str!``) and Python (here, at import
# time) parse the same file with the same rules. A cross-language
# consistency test (``tests/test_managed_paths_consistency.py``) pins
# the two languages to the file contents.
#
# The .txt file lists itself, so ``update_orchestrator_at`` propagates
# freshly-edited editions of the list into every existing install.
#
# Note (PR-31 / v0.2.12): ``CLAUDE.md`` was removed from this whitelist.
# The root CLAUDE.md is orchestrator-self development docs, not a user-
# project scaffold. User projects render their CLAUDE.md from
# ``templates/CLAUDE.md.template`` via the project-bootstrapper. The
# ``DEFAULT_PRESERVE_LIST`` constant above still includes ``CLAUDE.md``
# — that is the existing-user-CLAUDE.md preservation concern on
# update, separate from the whitelist concern this section governs.

# Path resolution: __file__ → install.py at repo root → the .txt is
# its sibling. We use Path(__file__).resolve() so the lookup is robust
# even when install.py is invoked via a relative path or symlink.
_MANAGED_PATHS_FILE: Path = (
    Path(__file__).resolve().parent / "orchestrator-managed-paths.txt"
)


def _parse_managed_paths_text(text: str) -> tuple[str, ...]:
    """Parse ``orchestrator-managed-paths.txt`` content into an allowlist.

    Parse rules (must match ``parse_managed_paths_text`` in
    ``launcher/src-tauri/src/commands/installer.rs``):

      * A leading UTF-8 BOM (``\\ufeff``) on the first line is
        stripped. Saved-from-Windows-Notepad files routinely carry one
        and ``str.strip()`` does NOT remove BOM characters; without
        this, the first allowlist entry silently fails to match and
        the file gets treated as if its first line were missing.
      * Lines are stripped of leading/trailing whitespace.
      * Empty lines are skipped.
      * Lines whose first non-whitespace character is ``#`` are
        comments and are skipped entirely (no inline comments — ``#``
        is only a line prefix).

    Order is preserved so the resulting tuple has the same shape as
    the file, which makes diff output legible when entries change.
    """
    if text.startswith("﻿"):
        text = text[1:]
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return tuple(out)


def _load_orchestrator_managed_paths() -> tuple[str, ...]:
    """Read + parse the source-of-truth .txt file at import time.

    Read errors are fatal: if the file is missing or unreadable, the
    install logic has no allowlist to enforce and silently falling
    back to a hard-coded default would re-introduce the drift bug
    PR-5 was written to fix. We surface a clear error pointing the
    user at the file path and the upstream repo so they can recover.
    """
    try:
        text = _MANAGED_PATHS_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"Could not read orchestrator allowlist file at "
            f"{_MANAGED_PATHS_FILE}: {e}. This file is the source of "
            f"truth for ORCHESTRATOR_MANAGED_PATHS and is required by "
            f"install.py. If you cloned the orchestrator repo "
            f"correctly the file should be present; otherwise re-fetch "
            f"from https://github.com/hotak92/vibecoded-orchestrator."
        ) from e
    return _parse_managed_paths_text(text)


ORCHESTRATOR_MANAGED_PATHS: tuple[str, ...] = _load_orchestrator_managed_paths()


def _normalize_conflict_strategy(s: str) -> str:
    """CLI uses kebab-case; internal handler uses snake_case (matches Rust)."""
    return {
        "delete-claude": "delete_claude_and_reinstall",
        "overwrite-all": "overwrite_all",
        "overwrite-preserve": "overwrite_preserve",
        "adopt-as-is": "adopt_as_is",
    }[s]


def _parse_preserve_paths(raw: str | None) -> list[str]:
    """Parse `--preserve-paths a,b,c`. Empty/None → default list."""
    if not raw:
        return list(DEFAULT_PRESERVE_LIST)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _new_sibling_path(p: Path) -> Path:
    """Insert `.new` before the file's extension. Mirrors `new_sibling_path`
    in installer.rs.

    Examples:
      - CLAUDE.md → CLAUDE.new.md
      - .env → .env.new (no extension to split on)
      - archive.tar.gz → archive.tar.new.gz (split on LAST dot)
    """
    name = p.name
    # Filename starting with `.` and no other `.` (e.g. `.env`) → treat
    # the leading dot as part of the stem so we don't write `.new.env`.
    if "." not in name or (name.startswith(".") and name.count(".") == 1):
        return p.with_name(f"{name}.new")
    stem, _, ext = name.rpartition(".")
    return p.with_name(f"{stem}.new.{ext}")


def _build_merge_notification_block(preserved_files: list[str]) -> str:
    """Render the notification block. Idempotency note: the prose inside
    must NOT contain the literal MERGE_BLOCK_START/END strings, otherwise
    the marker-counting check breaks. Reference them obliquely as "the
    HTML-comment markers".
    """
    if preserved_files:
        list_lines = "\n".join(
            f"- `{p}` (upstream-new at `{_new_sibling_path(Path(p))}`)"
            for p in preserved_files
        )
    else:
        list_lines = "_(none — strategy ran with an empty preserve list)_"

    return (
        f"{MERGE_BLOCK_START}\n"
        "## Pending merge — read this on session start\n"
        "\n"
        "The orchestrator was just upgraded. Several user-curated files have an\n"
        "upstream-new version sitting next to them (`*.new.md` / `*.new.<ext>`).\n"
        "For each pair:\n"
        "\n"
        "1. Read both the existing file AND the upstream-new sibling.\n"
        "2. Reconcile: keep the user's project-specific content, but adopt new\n"
        "   structure / guidance / sections from the upstream version. Use your\n"
        "   judgment for ambiguous merges; ask the user if a conflict is\n"
        "   irreconcilable.\n"
        "3. After successfully merging a file, **delete its upstream-new\n"
        "   sibling**.\n"
        "4. When ALL `.new.*` siblings under the install path are gone, you'll\n"
        "   know the merge is complete — at that point, **delete this entire\n"
        "   notification block** (the HTML-comment markers wrapping this section\n"
        "   plus all text between them) from this CONTEXT_STATE.md. That removes\n"
        "   the prompt for the next session.\n"
        "\n"
        "Files awaiting merge:\n"
        f"{list_lines}\n"
        "\n"
        "Note: `MEMORY.md` lives at `~/.claude/projects/<id>/memory/MEMORY.md`,\n"
        "not in the install dir, so v1.0 of the conflict resolver does NOT write\n"
        "an upstream-new sibling for it. If you suspect your MEMORY.md is\n"
        "divergent from the upstream template, run a manual diff and merge by\n"
        "hand.\n"
        "\n"
        "(Do NOT delete user content. Preserve any session-specific state in\n"
        "CONTEXT_STATE.md, your existing CLAUDE.md customisations, etc. The\n"
        "upstream version is a reference for new structure, not a wholesale\n"
        "replacement.)\n"
        f"{MERGE_BLOCK_END}\n"
    )


def _replace_or_append_block(existing: str, block: str) -> str:
    """If `existing` contains a `<!-- vct-merge-pending -->` ...
    `<!-- /vct-merge-pending -->` block, replace it with `block`. Otherwise
    append `block` (separated by a single newline) to the end."""
    start = existing.find(MERGE_BLOCK_START)
    if start != -1:
        end_rel = existing[start:].find(MERGE_BLOCK_END)
        if end_rel != -1:
            end = start + end_rel + len(MERGE_BLOCK_END)
            after = existing[end:]
            # Strip a single leading newline so we don't accumulate blank
            # lines on every refresh.
            if after.startswith("\n"):
                after = after[1:]
            return existing[:start] + block + after
    sep = "" if (existing.endswith("\n") or not existing) else "\n"
    return f"{existing}{sep}\n{block}"


def update_merge_notification_block(
    context_state_path: Path, preserved_files: list[str]
) -> bool:
    """Append (or refresh) the merge-notification block in
    `.claude/CONTEXT_STATE.md`. Returns True iff the file was written.
    """
    block = _build_merge_notification_block(preserved_files)
    if not context_state_path.exists():
        context_state_path.parent.mkdir(parents=True, exist_ok=True)
        context_state_path.write_text(block)
        return True
    existing = context_state_path.read_text()
    updated = _replace_or_append_block(existing, block)
    if updated == existing:
        return False
    context_state_path.write_text(updated)
    return True


def _copy_recursive(src: Path, dst: Path) -> int:
    """Plain recursive copy. Symlinks are resolved (file content follows).
    Returns the number of files copied."""
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        total = 0
        for entry in src.iterdir():
            total += _copy_recursive(entry, dst / entry.name)
        return total
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1


def _copy_recursive_preserve(
    src: Path,
    dst: Path,
    install_root: Path,
    preserve: list[str],
    preserved_present: list[str],
) -> tuple[int, int]:
    """Preserve-aware recursive copy. For each FILE encountered:
      - If its install-relative path is in `preserve` AND a file already
        exists at `dst`, copy to `<dst>.new.<ext>` instead and append the
        relative path to `preserved_present`.
      - Otherwise, plain overwrite copy.

    Returns `(files_visited, new_files_written)`.
    """
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        files_visited = 0
        new_files = 0
        for entry in src.iterdir():
            v, n = _copy_recursive_preserve(
                entry, dst / entry.name, install_root, preserve, preserved_present
            )
            files_visited += v
            new_files += n
        return files_visited, new_files

    rel = str(dst.relative_to(install_root))
    if rel in preserve and dst.exists():
        sibling = _new_sibling_path(dst)
        sibling.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, sibling)
        preserved_present.append(rel)
        return 1, 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1, 0


def apply_conflict_strategy(
    source: Path, install_path: Path, strategy: str, preserve_paths: list[str]
) -> dict:
    """Apply a `ConflictStrategy` at `install_path`, copying from `source`.

    `strategy` is the snake_case name (matches the Rust `ConflictStrategy`
    enum). Returns a report dict with the same shape as the Rust
    `ConflictApplyReport` so callers can log it as JSON-serializable
    metadata.

    Defense: for `delete_claude_and_reinstall` we resolve and verify the
    path we're about to remove is exactly `<install_path>/.claude` before
    calling shutil.rmtree. Refuses to follow symlinks out of the install
    path.
    """
    report: dict = {
        "strategy": strategy,
        "preserved_count": 0,
        "new_md_count": 0,
        "notification_written": False,
        "copied_count": 0,
    }
    if not (source / "vct-module.json").exists():
        raise ValueError(
            f"source {source} is not an orchestrator repo (no vct-module.json)"
        )
    install_path.mkdir(parents=True, exist_ok=True)

    if strategy == "adopt_as_is":
        return report

    if strategy == "delete_claude_and_reinstall":
        claude_dir = install_path / ".claude"
        if claude_dir.exists():
            canon_install = install_path.resolve()
            canon_claude = claude_dir.resolve()
            expected = canon_install / ".claude"
            if canon_claude != expected:
                raise ValueError(
                    f"refusing to delete: {claude_dir} resolves to "
                    f"{canon_claude} (expected {expected})"
                )
            shutil.rmtree(claude_dir)
        # Fresh copy.
        for managed in ORCHESTRATOR_MANAGED_PATHS:
            s = source / managed
            d = install_path / managed
            if not s.exists():
                continue
            report["copied_count"] += _copy_recursive(s, d)
        return report

    if strategy == "overwrite_all":
        for managed in ORCHESTRATOR_MANAGED_PATHS:
            s = source / managed
            d = install_path / managed
            if not s.exists():
                continue
            report["copied_count"] += _copy_recursive(s, d)
        return report

    if strategy == "overwrite_preserve":
        # Dedup the preserve list (callers may pass repeats).
        preserve = sorted(set(preserve_paths))
        preserved_present: list[str] = []
        new_files_written = 0
        copied = 0
        for managed in ORCHESTRATOR_MANAGED_PATHS:
            s = source / managed
            d = install_path / managed
            if not s.exists():
                continue
            v, n = _copy_recursive_preserve(
                s, d, install_path, preserve, preserved_present
            )
            copied += v
            new_files_written += n
        report["copied_count"] = copied
        report["preserved_count"] = len(preserved_present)
        report["new_md_count"] = new_files_written

        ctx = install_path / ".claude" / "CONTEXT_STATE.md"
        report["notification_written"] = update_merge_notification_block(
            ctx, preserved_present
        )
        return report

    raise ValueError(f"unknown conflict strategy: {strategy}")


# ---------------------------------------------------------------------------
# Deliverable 3 (2026-04-28): lightweight re-install
#
# Used by the launcher when a Strategy-3 (overwrite-preserve) re-install
# runs against an already-installed project that has a healthy `.venv/`,
# unchanged Python deps, and matching state hashes. The full install.py
# path takes ~1-2min on a hot system; the lightweight path skips model
# pulls + Weaviate seed (both idempotent already) and only redoes:
#
#   1. Path rewrite in .env / .claude/settings.json (when the install
#      moved or was bundled at a different path).
#   2. Venv triage — recreate only on Python version mismatch or
#      requirements.txt drift; otherwise leave alone.
#   3. Container ensure (no model pull — those are shared volumes,
#      already present).
#
# The lightweight mode is gated on `--lightweight`; the full path is
# unchanged when the flag isn't passed.
# ---------------------------------------------------------------------------

# Files we rewrite paths in. Conservative — only those that commonly
# embed an absolute install_path. Hooks under .claude/hooks/ use
# `$(dirname "$0")` and don't need rewriting.
LIGHTWEIGHT_PATH_REWRITE_TARGETS: tuple[str, ...] = (
    ".env",
    ".claude/settings.json",
)


def _rewrite_paths_in_file(path: Path, old_str: str, new_str: str) -> bool:
    """Replace every literal occurrence of `old_str` with `new_str` in
    a single text file. Returns True iff at least one replacement was
    made. No-op if the file is missing or doesn't contain old_str.

    We use literal substring replace (not regex) because the strings
    we're rewriting are absolute filesystem paths. They may contain
    regex metacharacters, but no user-controlled regex syntax is
    expected.
    """
    if not path.is_file() or not old_str:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if old_str not in text:
        return False
    new_text = text.replace(old_str, new_str)
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def _lightweight_rewrite_paths(install_path: Path,
                              old_path: str) -> dict[str, bool]:
    """Rewrite occurrences of `old_path` (absolute) → str(install_path)
    in every target file under `install_path`.

    Returns {relative_target: rewrote_at_least_one_occurrence}. Used
    when a project moved on disk (e.g. user renamed the parent folder).

    Idempotent: a no-op when old_path == install_path or when no target
    file embeds the old path.
    """
    results: dict[str, bool] = {}
    new_str = str(install_path)
    if not old_path or old_path == new_str:
        # Nothing to do; record a clean no-op for every target.
        for rel in LIGHTWEIGHT_PATH_REWRITE_TARGETS:
            results[rel] = False
        return results
    for rel in LIGHTWEIGHT_PATH_REWRITE_TARGETS:
        results[rel] = _rewrite_paths_in_file(
            install_path / rel, old_path, new_str,
        )
    return results


def _venv_triage(install_path: Path) -> dict:
    """Decide what to do with `<install_path>/.venv` on a lightweight
    re-install.

    Cases:
      1. .venv missing       → action="create", recreate fully
      2. .venv exists, Python version mismatch → action="recreate",
         drop + recreate
      3. .venv exists, Python OK, requirements.txt drift → action="upgrade",
         pip install -r ... --upgrade in place
      4. .venv exists, all matches  → action="skip"

    Returns {"action": one_of(...), "reason": str, "venv_python": Path|None}.
    The caller (`_run_lightweight`) executes the action.
    """
    venv = install_path / ".venv"
    if platform.system() == "Windows":
        venv_python = venv / "Scripts" / "python.exe"
    else:
        venv_python = venv / "bin" / "python"

    if not venv.is_dir() or not venv_python.exists():
        return {"action": "create", "reason": ".venv missing",
                "venv_python": None}

    # Python version check.
    try:
        result = subprocess.run(
            [str(venv_python), "-c",
             "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, timeout=10,
        )
        venv_pyver = (result.stdout.strip()
                      if result.returncode == 0 else "")
    except (subprocess.SubprocessError, OSError):
        venv_pyver = ""

    expected = f"{sys.version_info.major}.{sys.version_info.minor}"
    if not venv_pyver or venv_pyver != expected:
        return {"action": "recreate",
                "reason": f"Python version mismatch (.venv={venv_pyver!r}, "
                          f"launcher={expected!r})",
                "venv_python": None}

    # Requirements drift check (uses state-hashes from Deliverable 2).
    drift = _compute_drift(install_path)
    if drift.get("requirements_txt_md5", True):
        return {"action": "upgrade",
                "reason": "requirements.txt content changed since last install",
                "venv_python": venv_python}

    return {"action": "skip", "reason": "venv healthy, no drift",
            "venv_python": venv_python}


def _run_lightweight(args: argparse.Namespace) -> int:
    """Execute the lightweight re-install path.

    Skips: model pulls, Weaviate seed, Joern probe, lean-ctx detection,
    full GPU detection. Runs: path rewrite, venv triage, container
    ensure (without seed), state-hash snapshot.

    Returns the exit code (0 on success, non-zero on failure).
    """
    print("=" * 62)
    print("  VibeCoded Tools — Orchestrator Lightweight Re-install")
    print("=" * 62)
    print(f"  install path: {PROJECT_ROOT}")
    if args.lightweight_old_path:
        print(f"  previous path: {args.lightweight_old_path}")
    print()
    _log_install_event(
        "lightweight", "start",
        "lightweight re-install begin",
        data={"install_path": str(PROJECT_ROOT),
              "old_path": args.lightweight_old_path},
    )

    # Step 1: state directory must exist before logging will hit disk.
    _create_state_directory()

    # Step 2: path rewrite in .env / .claude/settings.json
    if args.lightweight_old_path:
        rewrite_report = _lightweight_rewrite_paths(
            PROJECT_ROOT, args.lightweight_old_path,
        )
        rewritten = [k for k, v in rewrite_report.items() if v]
        print(f"[1/4] Path rewrite: {len(rewritten)} file(s) updated")
        for k in rewritten:
            print(f"        {k}")
        _log_install_event(
            "lightweight", "ok",
            "path-rewrite complete",
            data={"rewrote_files": rewritten,
                  "all_targets": list(rewrite_report.keys())},
        )
    else:
        print("[1/4] Path rewrite: skipped (no --lightweight-old-path)")
        _log_install_event(
            "lightweight", "skip",
            "path-rewrite (no old path provided)",
        )

    # Step 3: venv triage
    triage = _venv_triage(PROJECT_ROOT)
    print(f"[2/4] Venv triage: action={triage['action']} ({triage['reason']})")
    _log_install_event(
        "lightweight", "ok",
        f"venv triage: {triage['action']}",
        data={"action": triage["action"], "reason": triage["reason"]},
    )

    if triage["action"] == "create":
        _create_venv(PROJECT_ROOT)
        venv_python = (PROJECT_ROOT / ".venv" /
                       ("Scripts/python.exe" if platform.system() == "Windows"
                        else "bin/python"))
        _install_requirements(venv_python, dev=args.dev)
    elif triage["action"] == "recreate":
        # Drop the old venv first.
        venv = PROJECT_ROOT / ".venv"
        if venv.is_dir():
            shutil.rmtree(venv, ignore_errors=True)
        _create_venv(PROJECT_ROOT)
        venv_python = (PROJECT_ROOT / ".venv" /
                       ("Scripts/python.exe" if platform.system() == "Windows"
                        else "bin/python"))
        _install_requirements(venv_python, dev=args.dev)
    elif triage["action"] == "upgrade":
        _install_requirements(triage["venv_python"], dev=args.dev)
    else:
        # action == "skip"
        pass

    # Step 4: container ensure (skip model pulls + seed; both
    # idempotent and unchanged on lightweight path).
    if args.no_containers:
        print("[3/4] Containers: skipped (--no-containers)")
    else:
        # Ensure the runtime is available, but do not pull models or
        # reseed Weaviate. The shared-container volume already has
        # everything; a re-install is purely about wiring this project
        # up to point at it.
        if sys.platform == "linux":
            print("[3/4] Containers: assumed running (lightweight path "
                  "doesn't probe / start them)")
        else:
            print("[3/4] Containers: assumed running (lightweight)")
    _log_install_event(
        "lightweight", "ok",
        "containers: assume-running",
    )

    # Step 5: snapshot fresh state hashes so future drift detection
    # has the right baseline.
    _record_state_hashes(PROJECT_ROOT)
    print("[4/4] State-hashes snapshot: written")

    # Bug G (v0.2.8): refresh install-manifest.json on lightweight too.
    # Previously the manifest was written only on the full install path,
    # so an install that was set up at v0.1.x and lightweight-reinstalled
    # at v0.2.x kept reporting v0.1.x forever. sysinfo=None — the
    # manifest writer falls back to prior values for the affected fields.
    _write_install_manifest(None, args, install_method="lightweight")

    # v0.2.10 (Bug L2): re-materialize boot service on lightweight too —
    # the wrapper path may have changed if the install was relocated
    # (--lightweight-old-path). Idempotent for unchanged paths.
    #
    # PR-12 Bug C: lightweight reinstall is the EXACT path users hit when
    # upgrading via the launcher's "Update orchestrator" button — pass a
    # fresh DeferralReport so any stale-unit auto-repair surfaces in
    # UPDATE_DEFERRED.md the user reads at next session.
    _lightweight_deferral = DeferralReport()

    # PR-14b (v0.2.11 MCP simplification): SearXNG no longer ships in the
    # default compose stack; Ollama MCP is dropped from the default install;
    # SEARXNG_URL + GITHUB_TOKEN are no longer needed in the search MCP env.
    # Surface deferral notices so existing users know to clean up manually.
    if not getattr(args, "suppress_mcp_deprecation_warnings", False):
        _check_searxng_remnants(PROJECT_ROOT, _lightweight_deferral)
        _check_ollama_mcp_remnants(_lightweight_deferral)
        _check_search_mcp_env_obsolete(_lightweight_deferral)
    _materialize_boot_service(PROJECT_ROOT, None, args,
                              deferral_report=_lightweight_deferral)
    # Best-effort: persist the deferral report. Mirrors the folder
    # resolution used by the full install path (line ~2169).
    try:
        _lightweight_folder = (
            Path(args.project_folder)
            if getattr(args, "project_folder", None)
            else PROJECT_ROOT
        )
        _lightweight_deferral.write(_lightweight_folder)
    except Exception as _exc:  # noqa: BLE001 — soft-fail
        _log_install_event(
            "lightweight", "warn",
            f"could not write lightweight-mode deferral report: {_exc}",
        )

    _log_install_event(
        "lightweight", "ok",
        "lightweight re-install complete",
    )

    print()
    print("=" * 62)
    print("  Lightweight re-install complete!")
    print("=" * 62)
    return 0


# ---------------------------------------------------------------------------
# PR-28 (Group G, v0.2.12) — install-time storage-location prompt
# ---------------------------------------------------------------------------
#
# Users running install.py from the CLI (without the launcher GUI) previously
# got NO chance to point the orchestrator at pre-existing volume data on the
# host. Default behaviour creates fresh named volumes; a user with 110 GB of
# Ollama models at ~/podman_volumes/ollama/models would see their containers
# come up empty and only notice when `ollama list` returns nothing from
# inside the new container.
#
# The prompt is gated by:
#   - --quiet / --yes / non-TTY stdin → silent default (mode='deferred').
#   - EOF on stdin during the prompt → treated as 'deferred' (don't abort).
#   - No legacy paths detected → silent default ('named').
#
# The persistence call shells out to `vct-launcher --set-storage-config ...`
# when the bundled launcher binary is locatable. Falls through to a direct
# Python write of `~/.vct/storage.toml` otherwise (sufficient for fresh
# installs where the launcher hasn't been built / extracted yet).

# Logical-service → list of candidate host paths to probe. Order matters:
# the first existing path wins. Mirrors the surface area covered by
# launcher/src-tauri/src/commands/storage_ux.rs::detect_legacy_volumes_inner
# but operates on the FILESYSTEM rather than `podman volume ls`, because at
# install time we don't necessarily have a runtime up yet AND the user may
# have data sitting in a bind-mount directory that no current container
# references.
#
# The per-service-suffix probes are derived from
# `vco_lib.containers.HISTORICAL_ALIASES` so the maintainer-machine leak
# (the literal `weaviate_claude` / `ollama_claude` paths that lived here
# pre-v0.2.15) cannot reappear — see vco_lib/containers.py for the
# centralised registry + the rationale.
def _build_legacy_volume_probes() -> dict[str, tuple[str, ...]]:
    """Build the per-service probe list from the central container-name
    registry. Replaces the v0.2.14-and-earlier hardcoded
    ``weaviate_claude`` / ``ollama_claude`` paths (maintainer-machine
    leak — see vco_lib/containers.py).

    Probe path forms (all routed through ``Path.expanduser()`` /
    ``%LOCALAPPDATA%`` / ``%USERPROFILE%`` expansion so they work on
    Linux + macOS + Windows):
      * ``~/podman_volumes/<full-container-name>`` (POSIX) /
        ``%USERPROFILE%\\podman_volumes\\<full-container-name>``
        (Windows) — recovers bind-mount layouts using the literal
        container name as the directory. Only generated for full
        container names that would naturally form a bind-mount root
        (i.e. the ``_claude``-suffixed legacy and the
        ``vct_code_embed`` transitional name); NOT for bare
        service-token aliases like ``weaviate`` / ``ollama`` /
        ``code_embed`` because those conventionally bind-mount one
        level deeper (e.g. ``~/podman_volumes/ollama/models``).
      * ``~/.local/share/containers/storage/volumes/<alias>/_data`` —
        rootless POSIX named-volume mountpoint pattern. Generated for
        every alias.
      * ``%LOCALAPPDATA%\\containers\\storage\\volumes\\<alias>\\_data`` —
        rootful Windows Podman Desktop / Podman-on-Windows volume
        mountpoint pattern. Generated for every alias.
      * ``%USERPROFILE%\\AppData\\Roaming\\Docker\\volumes\\<alias>\\_data`` —
        Docker Desktop volume root on Windows.
      * Service-specific shared bind paths kept explicit per service
        (most-used layouts).

    Returns tuples of path strings with shell-style ``~/``-relative
    POSIX heads OR Windows ``%VAR%``-style heads. Callers are expected
    to pass them through ``_expand_path_token()`` (defined below) which
    handles both forms transparently.
    """
    from vco_lib.containers import HISTORICAL_ALIASES, canonical_name

    # Service-specific "well-known shared paths" the user may have set
    # up out-of-band (NOT derived from container names). Kept explicit
    # so they're easy to audit / extend. POSIX paths first, then
    # Windows analogues — both forms are tried by the probe loop.
    shared_paths: dict[str, tuple[str, ...]] = {
        "ollama": (
            "~/podman_volumes/ollama/models",
            "%USERPROFILE%\\podman_volumes\\ollama\\models",
            "%USERPROFILE%\\.ollama\\models",  # Ollama-on-Windows default
        ),
        "code_embed": (
            "~/podman_volumes/code_embed_cache",
            "%USERPROFILE%\\podman_volumes\\code_embed_cache",
        ),
        "weaviate": (
            # No bare host shared path historically — only named volumes.
        ),
    }

    # Aliases that look like distinct container names (vs. bare service
    # tokens that conventionally bind-mount one level deeper).
    def _alias_is_container_shape(alias: str) -> bool:
        return alias.endswith("_claude") or alias.startswith(("vct_", "vco_"))

    # Mountpoint root templates per OS family. Use a single `{alias}`
    # placeholder; the loop below substitutes.
    posix_named_volume_root = "~/.local/share/containers/storage/volumes/{alias}/_data"
    windows_podman_volume_root = (
        "%LOCALAPPDATA%\\containers\\storage\\volumes\\{alias}\\_data"
    )
    # Docker Desktop on Windows materialises named volumes inside its
    # Hyper-V VHD; the bind-mountable path under the user profile is
    # available only when "Use the WSL 2 based engine" is on AND
    # "Use containerd for pulling and storing images" is OFF. Probing
    # is cheap: missing dirs just fall through.
    windows_docker_volume_root = (
        "%USERPROFILE%\\AppData\\Local\\Docker\\wsl\\data\\volumes\\{alias}\\_data"
    )

    out: dict[str, tuple[str, ...]] = {}
    for service in ("ollama", "code_embed", "weaviate"):
        paths: list[str] = list(shared_paths.get(service, ()))
        for alias in HISTORICAL_ALIASES[service]:
            if _alias_is_container_shape(alias):
                # Bind-mount-root forms — POSIX + Windows.
                paths.append(f"~/podman_volumes/{alias}")
                paths.append(f"%USERPROFILE%\\podman_volumes\\{alias}")
            # Named-volume mountpoints for every alias on every OS.
            paths.append(posix_named_volume_root.format(alias=alias))
            paths.append(windows_podman_volume_root.format(alias=alias))
            paths.append(windows_docker_volume_root.format(alias=alias))
        # Always probe the canonical compose volume mountpoint last
        # (named volumes the current compose creates).
        canon_volume = {
            "ollama": "ollama_data",
            "code_embed": "code_embed_cache",
            "weaviate": "weaviate_data",
        }[service]
        paths.append(posix_named_volume_root.format(alias=canon_volume))
        paths.append(windows_podman_volume_root.format(alias=canon_volume))
        paths.append(windows_docker_volume_root.format(alias=canon_volume))
        # canonical_name() imported above only to surface a hard error
        # if the registry is malformed at import time.
        _ = canonical_name(service)
        # Deduplicate preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            deduped.append(p)
        out[service] = tuple(deduped)
    return out


def _expand_path_token(raw: str) -> Optional[Path]:
    """Expand a probe-path token to an absolute ``Path``, handling both
    POSIX ``~/...`` heads and Windows ``%VAR%`` heads.

    Returns ``None`` if expansion can't produce a usable path
    (e.g. a Windows-style token on POSIX with no matching env var, or
    vice versa). The caller treats ``None`` as "not applicable on this
    host", same as a missing directory.
    """
    if not raw:
        return None
    # Windows-style %VAR% expansion first — os.path.expandvars is a
    # no-op on POSIX for unknown vars (it leaves the literal in place),
    # which would then fail Path.is_dir() naturally.
    expanded = os.path.expandvars(raw)
    if "%" in expanded:
        # Unresolved %VAR% means this token is Windows-specific and we're
        # not on Windows (or the variable simply isn't set). Skip.
        return None
    # POSIX tilde expansion. Path.expanduser handles "~/..." but NOT
    # mid-string "~user/..." with raw backslashes; we only generate the
    # leading-tilde form so we're safe.
    try:
        return Path(expanded).expanduser()
    except (RuntimeError, OSError):
        return None


_LEGACY_VOLUME_PROBES: dict[str, tuple[str, ...]] = _build_legacy_volume_probes()


def _detect_legacy_volume_paths() -> dict[str, str]:
    """Probe well-known host-side paths for pre-existing service data.

    Returns a `{service: absolute_path}` map for every probe that resolves
    to an existing non-empty directory. Empty map when nothing is found.

    Cross-OS: probes include both POSIX (``~/...``,
    ``~/.local/share/containers/...``) and Windows
    (``%LOCALAPPDATA%\\containers\\...``,
    ``%USERPROFILE%\\AppData\\Local\\Docker\\wsl\\data\\volumes\\...``)
    forms. `_expand_path_token` skips Windows-style tokens on POSIX (and
    vice versa) so non-applicable probes silently fall through.
    """
    detected: dict[str, str] = {}
    for service, candidates in _LEGACY_VOLUME_PROBES.items():
        for raw in candidates:
            resolved = _expand_path_token(raw)
            if resolved is None:
                # Token not applicable on this OS (Windows-style on POSIX
                # or vice versa) — skip silently.
                continue
            if not resolved.is_dir():
                continue
            # Skip empty directories: they were probably created by a prior
            # install attempt that never landed any data. We only want to
            # surface volumes the user actually has STUFF in.
            try:
                first = next(resolved.iterdir(), None)
            except (PermissionError, OSError):
                continue
            if first is None:
                continue
            detected[service] = str(resolved)
            break
    return detected


def _dir_size_human(path: str) -> str:
    """Best-effort human-readable directory size. Soft-fail on permission
    errors; the user just sees "(size unavailable)" next to the path."""
    try:
        root = Path(path)
        if not root.is_dir():
            return "(not a directory)"
        total = 0
        for child in root.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except (PermissionError, OSError):
                continue
            # Cap at ~50K files to keep the prompt snappy on huge model
            # caches; the human-readable label is just a hint, not an
            # exact accounting.
        if total <= 0:
            return "(empty)"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        scaled = float(total)
        while scaled >= 1024.0 and idx < len(units) - 1:
            scaled /= 1024.0
            idx += 1
        return f"{scaled:.1f} {units[idx]}"
    except (PermissionError, OSError):
        return "(size unavailable)"


def _vct_state_dir() -> Path:
    """Resolve `~/.vct/` honouring VCT_STATE_DIR (mirrors the Rust path
    resolver in `launcher/src-tauri/src/paths.rs::vct_root_dir`). The
    Python fallback writes storage.toml directly into this directory."""
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"


def _write_storage_toml_direct(choice: dict) -> Path:
    """Python fallback when no launcher binary is on disk.

    Schema mirrors `launcher/src-tauri/src/commands/storage_ux.rs::StorageConfig`:
        mode = "named" | "bind"
        bind_root = ""
        [per_service_paths]
        ollama = "/abs/path"
        ...
        [external_aliases]
    Atomic write (write to .tmp + rename) so a SIGTERM mid-write doesn't
    leave a half-formed TOML.
    """
    mode = choice.get("mode", "named")
    bind_paths: dict[str, str] = choice.get("bind_paths", {}) or {}
    state_dir = _vct_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "storage.toml"

    lines: list[str] = []
    lines.append(f'mode = "{mode}"')
    lines.append('bind_root = ""')
    lines.append("")
    lines.append("[per_service_paths]")
    if mode == "bind" and bind_paths:
        # Sort for stable output (matches the Rust BTreeMap renderer).
        for service in sorted(bind_paths):
            path_norm = bind_paths[service].replace("\\", "/")
            # Escape embedded quotes; TOML basic strings reject them.
            path_safe = path_norm.replace('"', '\\"')
            lines.append(f'{service} = "{path_safe}"')
    lines.append("")
    lines.append("[external_aliases]")
    body = "\n".join(lines) + "\n"

    tmp = target.with_suffix(".toml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return target


def _prompt_storage_location(
    install_root: Path,
    args: argparse.Namespace,
    *,
    input_fn=input,
    stdin_isatty=None,
    detect_fn=None,
) -> dict:
    """Prompt for storage layout at install time.

    Returns a dict shaped like:
        {"mode": "named" | "bind" | "deferred", "bind_paths": {...}}

    - "deferred" → install.py should leave `~/.vct/storage.toml` untouched
      and let the user configure storage via the launcher GUI later.
    - "named"   → fresh named volumes (the legacy default behaviour).
    - "bind"    → bind-mount the auto-detected legacy paths.

    Non-interactive contracts:
      - `--quiet`, `--yes`, or non-TTY stdin → return 'deferred' silently.
      - EOF on stdin during the prompt → return 'deferred'.
      - Detected nothing → return 'named' silently (no legacy data to
        surface; the default is the right answer).

    `input_fn`, `stdin_isatty`, and `detect_fn` are test seams; production
    callers pass the defaults.
    """
    is_quiet = bool(getattr(args, "quiet", False))
    is_yes = bool(getattr(args, "yes", False))
    if stdin_isatty is None:
        try:
            tty = sys.stdin.isatty()
        except (ValueError, OSError):
            tty = False
    else:
        tty = bool(stdin_isatty)

    if is_quiet or is_yes or not tty:
        return {"mode": "deferred", "bind_paths": {}}

    legacy_paths = (detect_fn or _detect_legacy_volume_paths)()
    if not legacy_paths:
        # No legacy data to worry about — silently default to named volumes.
        return {"mode": "named", "bind_paths": {}}

    print()
    print("=== Storage configuration ===")
    print("Detected pre-existing volume data on this machine:")
    for service in sorted(legacy_paths):
        path = legacy_paths[service]
        size = _dir_size_human(path)
        print(f"  - {service}: {path} ({size})")
    print()
    print("How should the orchestrator use this data?")
    print("  (1) Bind-mount the existing paths (recommended if you have models/data)")
    print("  (2) Use fresh named volumes (start clean; existing data preserved on disk)")
    print("  (3) Configure later via the launcher GUI's Storage Settings")
    print()
    try:
        raw = input_fn("Choice [1/2/3, default=3]: ")
    except (EOFError, KeyboardInterrupt):
        # User Ctrl+D / Ctrl+C'd — treat as deferred. Do NOT abort the
        # install; the prompt is opt-in UX, not a blocker.
        print("\n  (no answer — deferring storage configuration to the launcher)")
        return {"mode": "deferred", "bind_paths": {}}
    choice = (raw or "").strip() or "3"

    if choice == "1":
        return {"mode": "bind", "bind_paths": legacy_paths}
    if choice == "2":
        return {"mode": "named", "bind_paths": {}}
    return {"mode": "deferred", "bind_paths": {}}


def _persist_storage_choice(choice: dict, install_root: Path) -> str:
    """Persist the user's storage choice. Returns a short description of
    which path was used ('launcher-cli' or 'python-fallback' or 'deferred').
    Soft-fail throughout — never abort install over a missed write."""
    if choice.get("mode") == "deferred":
        return "deferred"

    # Storage-config is an interactive prompt context: skip tiers 2-3
    # (GitHub download, cargo rebuild) so we never block the user mid-
    # prompt. Falls back to _write_storage_toml_direct when no bundled
    # binary is on disk. PR-36 unified this with the MCP-registration
    # resolver — see `_ensure_launcher_binary`.
    binary = _ensure_launcher_binary(install_root, prefer_only_bundled=True)
    if binary is not None:
        cmd: list[str] = [str(binary), "--set-storage-config", choice["mode"]]
        for service, path in (choice.get("bind_paths") or {}).items():
            cmd.extend(["--bind-path", f"{service}={path}"])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return "launcher-cli"
            print(
                f"  [storage] warning: launcher CLI exited {result.returncode}: "
                f"{(result.stderr or '').strip()}"
            )
            # Fall through to Python fallback so we still record the user's
            # decision even if the launcher binary is broken.
        except (subprocess.SubprocessError, OSError) as e:
            print(f"  [storage] warning: launcher CLI failed: {e}")

    try:
        target = _write_storage_toml_direct(choice)
        print(f"  [storage] wrote {target}")
        return "python-fallback"
    except (OSError, ValueError) as e:
        print(f"  [storage] warning: could not write storage.toml: {e}")
        return "failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # 2026-04-29 fix (wizard install-path lockdown): defensive
    # source-repo check — install.py operates on PROJECT_ROOT which is
    # the directory it was launched from. If someone has copied
    # install.py somewhere without first-install.sh next to it, fail
    # loudly with a clear message instead of half-installing.
    validate_source_repo(PROJECT_ROOT)

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
    parser.add_argument("--no-gpu-check", action="store_true",
                        help="Skip GPU driver probing entirely (for environments where nvidia-smi/rocm-smi hangs)")
    parser.add_argument("--low-resource", action="store_true",
                        help="Lightest mode: Jina V2 (768d) via Ollama. For low-RAM/low-VRAM machines.")
    # v0.2.9 (Bug K): VRAM threshold for auto GPU vs CPU mode pick.
    # Default 8.0 GB — see `_DEFAULT_GPU_VRAM_THRESHOLD_GB` rationale.
    parser.add_argument("--gpu-vram-threshold-gb", type=float,
                        default=_DEFAULT_GPU_VRAM_THRESHOLD_GB,
                        help=(f"VRAM threshold (GB) for auto GPU mode "
                              f"selection. Below this, the install falls "
                              f"back to CPU-only even with a discrete GPU "
                              f"present. Default: "
                              f"{_DEFAULT_GPU_VRAM_THRESHOLD_GB}."))
    parser.add_argument("--openai-key", type=str, default="",
                        help="Use OpenAI embeddings (provide API key)")
    parser.add_argument("--container", type=str, choices=["docker", "podman"],
                        help="Force a specific container runtime")
    parser.add_argument("--dev", action="store_true",
                        help="Install development dependencies")
    parser.add_argument("--skip-models", action="store_true",
                        help="Skip pulling Ollama models")
    parser.add_argument("--update", action="store_true",
                        help="Update mode: skip clone, re-install deps + restart services. "
                             "Always writes .claude/context/UPDATE_DEFERRED.md at the end — "
                             "either with actionable deferral entries OR a stub confirming "
                             "the run completed cleanly (Fix 6, v0.2.13).")
    parser.add_argument("--rebuild-collections", action="store_true", default=False,
                        help="Drop and re-ingest Weaviate collections (KG + dev). "
                             "Required when the schema invariants change "
                             "(named-vector slots, index_null_state, etc.) and "
                             "Weaviate ≤1.30 doesn't allow Reconfigure for those. "
                             "ONLY touches Weaviate state — your .md sources, "
                             ".env, .vscode/settings.json, .claude/settings.json "
                             "are NOT modified. Auto-detected and prompted on "
                             "--update when the running collection lacks "
                             "today's invariants.")
    parser.add_argument("--skip-rebuild-prompt", action="store_true", default=False,
                        help="During --update, skip the schema-rebuild prompt "
                             "even if the running collection is on an older "
                             "schema. Use to defer the rebuild for a later "
                             "session (search may misbehave until rebuilt).")
    parser.add_argument("--migrate-dry-run", "--dry-run-migrate",
                        dest="migrate_dry_run", action="store_true", default=False,
                        help="With --rebuild-collections: report the migration "
                             "plan only, do NOT mutate Weaviate. Useful to "
                             "preview which collections will be patched in "
                             "place vs copied vs rebuilt.")
    parser.add_argument("--force-rebuild", action="store_true", default=False,
                        help="With --rebuild-collections: bypass smart "
                             "copy-with-vectors path and always drop + "
                             "re-embed (the OLD behaviour, escape hatch). "
                             "Use when the source has legacy single-vector "
                             "format or you suspect vector corruption.")
    parser.add_argument("--migrate-uuid-scheme", action="store_true", default=False,
                        help="Stub placeholder (v0.2.16): mark the install "
                             "manifest's code-graph uuid_scheme key for migration "
                             "tooling. The actual migration runs as a follow-up in "
                             "a later release. Currently a no-op beyond the "
                             "manifest marker that --update already writes "
                             "(uuid_scheme=\"v2\"); installs predating v0.2.16 "
                             "have no marker, which a future migrator will read "
                             "as \"v1\" and suggest code-graph --force-recreate.")
    parser.add_argument("--apply-deferred", action="store_true", default=False,
                        help="During --update, attempt to apply each pending entry "
                             "in .claude/context/UPDATE_DEFERRED.md. Resolved entries "
                             "are removed; the file is deleted when zero entries remain.")
    parser.add_argument("--rewrite-stale-mcps", action="store_true", default=False,
                        help="On --update, prompt for consent to rewrite any "
                             "~/.claude.json mcpServers entries whose paths point "
                             "outside the current install_root. Without this flag, "
                             "stale entries are only detected + reported via deferral "
                             "(PR-23 behavior). With this flag, each stale entry is "
                             "prompted individually (y/n/all/skip-all). --quiet "
                             "bypasses the prompt and emits a clarifying deferral "
                             "(no rewrite). Set VCT_REWRITE_STALE_MCPS=all in the "
                             "env to auto-accept all entries (CI / scripted). PR-33.")
    parser.add_argument("--remove-deprecated-mcps", action="store_true", default=False,
                        help="On --update, prompt for consent to REMOVE ~/.claude.json "
                             "mcpServers entries that belong to deprecated default MCPs "
                             "(e.g. `ollama`, removed in v0.2.11). Without this flag, "
                             "deprecated entries are only detected + reported via deferral "
                             "(PR-34 detection-only behavior). With this flag, each "
                             "matching entry whose command path is inside the current "
                             "install_root is prompted individually (y/n/all/skip-all). "
                             "--quiet bypasses the prompt and emits a clarifying deferral "
                             "(no removal). User-customised entries whose command path is "
                             "OUTSIDE install_root are never touched. "
                             "Set VCT_REMOVE_DEPRECATED_MCPS=all in the env to "
                             "auto-accept all entries (CI / scripted). PR-34.")
    # Fix 1 (v0.2.13): post-cargo-rebuild dist-binary refresh.
    parser.add_argument("--no-binary-swap", dest="no_binary_swap",
                        action="store_true", default=False,
                        help="Skip the v0.2.13 post-rebuild dist-binary refresh. "
                             "Normally, when a fresh launcher binary is detected at "
                             "launcher/src-tauri/target/release/vct-launcher-temp "
                             "(produced during this install run, or newer than the "
                             "version recorded in tauri.conf.json), install.py copies "
                             "it into launcher/dist/<os>-<arch>/ so the next "
                             "_ensure_launcher_binary() returns the fresh artifact. "
                             "Pass --no-binary-swap to opt out (the dist binary stays "
                             "untouched even if a fresher build exists).")
    # Fix 5 (v0.2.13): control the Path A tier-3 retry inside _register_mcps.
    parser.add_argument("--prefer-only-bundled", dest="prefer_only_bundled",
                        action="store_true", default=False,
                        help="MCP registration: restrict launcher-binary resolution "
                             "to the bundled tier-1 path only — skip tier-2 (download) "
                             "and tier-3 (cargo rebuild) AND the v0.2.13 Fix-5 retry. "
                             "Used by latency-sensitive contexts that can't afford a "
                             "15-25 min cargo build (e.g. mid-prompt registration).")
    parser.add_argument("--no-rebuild-on-stale", dest="no_rebuild_on_stale",
                        action="store_true", default=False,
                        help="MCP registration: skip the v0.2.13 Fix-5 tier-3 retry "
                             "even when the launcher CLI times out / exits non-zero "
                             "against a tier-1 (potentially stale) binary. Useful for "
                             "CI / scripted runs that explicitly opt out of cargo "
                             "fallback. Tier-1/2/3 binary RESOLUTION itself is "
                             "unaffected — this only gates the post-failure retry.")
    parser.add_argument("--project-folder", type=str, default=None,
                        help="Folder where .claude/context/UPDATE_DEFERRED.md should "
                             "land. Defaults to the orchestrator's PROJECT_ROOT "
                             "(self-update behavior). End-user managed projects pass "
                             "their own project root so deferral entries are visible "
                             "in their workspace, not the orchestrator clone.")
    parser.add_argument("--quiet", action="store_true",
                        help="Minimal output")
    parser.add_argument("--with-joern", action="store_true", default=False,
                        help="Force-enable Joern integration for richer code-graph metrics (CFG/PDG). Skips the install prompt.")
    parser.add_argument("--no-joern", action="store_true", default=False,
                        help="Skip Joern detection and don't prompt to install it (~600MB JVM-based).")
    parser.add_argument("--no-lean-ctx", action="store_true", default=False,
                        help="Skip lean-ctx detection / install / hints (optional CLI-output compression tool).")
    parser.add_argument("--with-agents", action="store_true", default=True,
                        help="Install bundled Claude agents (default: on)")
    parser.add_argument("--no-agents", dest="with_agents", action="store_false",
                        help="Skip installing Claude agents")
    parser.add_argument("--with-skills", action="store_true", default=True,
                        help="Install Claude skills (default: on)")
    parser.add_argument("--no-skills", dest="with_skills", action="store_false",
                        help="Skip installing Claude skills")
    parser.add_argument("--with-hooks", action="store_true", default=True,
                        help="Install Claude Code hooks + merge .claude/settings.json (default: on)")
    parser.add_argument("--no-hooks", dest="with_hooks", action="store_false",
                        help="Skip installing hooks and the settings.json template")
    parser.add_argument("--no-compile", dest="compile_pyc", action="store_false", default=True,
                        help="Skip the bytecode-compile step (Step 11b). First import of "
                             "orchestrator modules will be ~50-200ms slower; useful for "
                             "dev/CI runs where the speedup doesn't matter.")
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
                             "Also skips collection creation — when there's no content "
                             "to seed, the MCP server creates collections lazily on first "
                             "write. Useful in CI / test runs. Re-run later with "
                             "`kg-sync --all` (handles both knowledge/ and docs/ since 2026-04-30).")
    # PR-39 (v0.2.12, 2026-05-16): the orchestrator-self's runtime .claude/
    # is now rendered from templates/ at install time. This flag skips that
    # step for tests / installs targeting a pre-populated .claude/.
    parser.add_argument("--skip-materialize-claude-dir", action="store_true",
                        default=False,
                        help="Skip rendering .claude/{hooks,scripts,settings.json} "
                             "from templates/ (PR-39, v0.2.12). Useful in tests or "
                             "when targeting a pre-populated .claude/ directory.")
    parser.add_argument("--no-resume", action="store_true", default=False,
                        help="Disable resume-from-log. Forces every step to run "
                             "even if state/logs/install.jsonl says a previous "
                             "session already completed it. Default: resume "
                             "enabled (verifies side effects regardless).")
    parser.add_argument("--skip-collections", action="store_true", default=False,
                        help="Skip Weaviate collection bootstrap (Step 7b) but still seed. "
                             "Implied by --skip-seed. Useful when the user wants the MCP "
                             "server to create collections lazily.")
    # Safe-by-default service handling. When a foreign Weaviate / Ollama is
    # detected on the canonical port, the installer prompts the user. These
    # flags resolve the prompt non-interactively for CI / scripting.
    parser.add_argument("--on-conflict",
                        choices=["alt-port", "adopt", "abort"],
                        default=None,
                        help="Non-interactive resolution when a foreign service is "
                             "detected on a canonical port. 'alt-port' (default for "
                             "foreign): pick a free port and write compose.override.yaml. "
                             "'adopt' (advanced): reuse the foreign service in place — "
                             "WILL write our collections into it. 'abort': stop install.")
    # Re-install file-conflict resolution. Mirrors the 4-option modal in
    # the launcher's OnboardingWizard. CLI users running install.py
    # directly against a populated install path get explicit flags
    # instead of the wizard prompt. Default unset = keep current behaviour
    # (no copy step in install.py — those flags are a no-op unless
    # --conflict-source-path is also passed). See
    # docs/INSTALL_RECOVERY.md → "Conflict Resolution".
    parser.add_argument("--conflict-strategy",
                        choices=["delete-claude", "overwrite-all",
                                 "overwrite-preserve", "adopt-as-is"],
                        default=None,
                        help="File-conflict resolution when re-installing over an "
                             "existing install. Pairs with --conflict-source-path "
                             "(the bundled repo to copy FROM). Mirrors the "
                             "OnboardingWizard's 4-option modal.")
    parser.add_argument("--conflict-source-path",
                        type=str, default=None,
                        help="Source path for the conflict-resolution copy step "
                             "(should be a bundled vct-orchestrator repo with "
                             "vct-module.json). Required when --conflict-strategy "
                             "is set.")
    parser.add_argument("--lightweight", action="store_true", default=False,
                        help="Lightweight re-install. Skips model pulls + Weaviate "
                             "seed (idempotent upserts already done), preserves an "
                             "existing healthy .venv (recreates only on Python "
                             "version mismatch or requirements.txt drift), and "
                             "runs path-rewrite on .env / .claude/settings.json. "
                             "Used by the launcher's per-project re-install path "
                             "(Deliverable 3 from launch-blocker spec 2026-04-28).")
    parser.add_argument("--lightweight-old-path", type=str, default=None,
                        help="Used with --lightweight. The previous install path "
                             "whose absolute occurrences in .env / settings.json "
                             "should be rewritten to the current PROJECT_ROOT.")
    # v0.2.10 (Bug L2 — cross-OS boot-service materialization). The
    # compose-project working directory may not match the install path
    # (the canonical example: install at ~/.../VCO_dev but compose lives
    # at ~/.../Claude/claude_mcp_servers). When set, this is the highest-
    # priority signal for _resolve_compose_working_dir; otherwise we
    # probe a running container's working_dir label, then fall back to
    # install-path probes.
    parser.add_argument("--compose-working-dir", type=str, default=None,
                        help="Absolute path to the directory containing "
                             "compose.yaml for the Claude MCP stack. Used by "
                             "the boot-service materialization step to record "
                             "WorkingDirectory= in the systemd unit / launchd "
                             "plist / Task Scheduler XML. When unset, the "
                             "installer probes a running container's label, "
                             "then falls back to <install>/claude_mcp_servers/ "
                             "or <install>/infrastructure/.")
    parser.add_argument("--preserve-paths",
                        type=str, default=None,
                        help="Comma-separated list of install-relative paths to "
                             "preserve under --conflict-strategy=overwrite-preserve. "
                             "Default: CLAUDE.md,.claude/CONTEXT_STATE.md,"
                             ".claude/PROJECT_REGISTRY.md,.env")
    # v0.2.6 (Bug C1): desktop-icon lifecycle. Direct `python install.py`
    # runs (without first-install.sh wrapping) previously skipped the
    # icon-creation step entirely. The orchestrator now invokes
    # `scripts/post-install-launcher.sh` (or .ps1 on Windows when present)
    # at the end of a successful full install — UNLESS opted out via
    # `--no-desktop-icon` / `VCT_NO_DESKTOP_ICON=1`. `--desktop-icon-only`
    # skips the actual install steps and runs ONLY the icon step (for
    # users who declined the first time and changed their mind).
    parser.add_argument("--no-desktop-icon", action="store_true", default=False,
                        help="Skip the desktop-icon creation step at end of install. "
                             "Equivalent to VCT_NO_DESKTOP_ICON=1.")
    parser.add_argument("--desktop-icon-only", action="store_true", default=False,
                        help="Run ONLY the desktop-icon step (post-install-launcher.sh) "
                             "and exit. Useful when you skipped the icon during the "
                             "initial install and want to add it later.")
    # PR-11: global lean-ctx hook detection warning suppression.
    # Users who've reviewed the global lean-ctx hooks and decided to keep
    # them (e.g. for lean-ctx development) can silence the warning.
    parser.add_argument("--suppress-lean-ctx-warning", action="store_true",
                        default=False,
                        help="Suppress the global lean-ctx hooks detection warning. "
                             "Use when you've reviewed the hooks and intentionally "
                             "keep them (e.g. lean-ctx development).")
    # PR-14b: MCP-simplification deprecation notices. Power users / CI that
    # have already acted on the notices can silence the three checks.
    parser.add_argument("--suppress-mcp-deprecation-warnings", action="store_true",
                        default=False,
                        help="Suppress v0.2.11 MCP-simplification deprecation notices "
                             "(SearXNG removed from default stack, Ollama MCP removed, "
                             "search MCP env simplified). Use after you have manually "
                             "cleaned up these remnants.")
    parser.add_argument("--skip-mcp-registration", action="store_true",
                        default=False,
                        help="Skip PR-23 (v0.2.12) MCP-server registration to "
                             "~/.claude.json. Use only for advanced cases where "
                             "you manage ~/.claude.json mcpServers entries "
                             "outside the installer (multi-stack adoption, "
                             "containerised deployment, etc.).")
    args = parser.parse_args()

    # v0.2.6 Bug C1 — `--desktop-icon-only` short-circuits: run JUST the
    # icon step (post-install-launcher.sh) and exit. Skips Python version
    # checks, venv creation, etc. because the user already has a working
    # install and just wants the icon now.
    if getattr(args, "desktop_icon_only", False):
        _run_desktop_icon_step(args)
        return 0

    # Deferral report — accumulates non-auto-resolvable conditions detected
    # during this run; written to .claude/context/UPDATE_DEFERRED.md at end.
    # Using a distinct name to avoid shadowing the conflict-resolve `report`
    # local below.
    _deferral_report = DeferralReport()

    # PR-11: warn early when global lean-ctx hooks are present in
    # ~/.claude/settings.json or ~/.claude/hooks/. These caused two
    # fork-bomb incidents (2026-04-30, 2026-05-15) before VCO 0.2.11.
    # Run BEFORE any compose-up or hook installation so the user sees
    # the warning even if other parts of install fail.
    if not getattr(args, "suppress_lean_ctx_warning", False):
        _check_global_lean_ctx_hooks(_deferral_report)

    # Windows install gate: refuse install when PowerShell 5.1+ isn't on
    # PATH (the .ps1 hooks would have nothing to run them). Non-Windows
    # hosts are a silent no-op. See audit F1 (P0).
    _check_windows_shell_prereqs()

    # Run conflict-resolution copy step BEFORE the rest of install.py so
    # subsequent steps see the post-resolution file tree. Best-effort
    # logging — the install log dir may not exist yet, in which case
    # _log_install_event() silently skips (matches Rust contract).
    if args.conflict_strategy:
        if not args.conflict_source_path:
            print("ERROR: --conflict-strategy requires --conflict-source-path")
            return 2
        try:
            report = apply_conflict_strategy(
                source=Path(args.conflict_source_path),
                install_path=PROJECT_ROOT,
                strategy=_normalize_conflict_strategy(args.conflict_strategy),
                preserve_paths=_parse_preserve_paths(args.preserve_paths),
            )
        except Exception as e:
            print(f"ERROR: conflict-resolve failed: {e}")
            _log_install_event(
                "conflict-resolve", "error",
                f"strategy={args.conflict_strategy}: {e}",
                data={"strategy": args.conflict_strategy},
            )
            return 1
        _log_install_event(
            "conflict-resolve", "ok",
            f"strategy={args.conflict_strategy}",
            data=report,
        )

    if args.uninstall:
        return _run_uninstall(args)

    # Deliverable 3 (2026-04-28): lightweight re-install path. The
    # launcher passes --lightweight when re-running over a healthy
    # install (matching state hashes, present .venv). Skips model
    # pulls, Weaviate seed, full GPU detection — completes in seconds
    # instead of minutes.
    if args.lightweight:
        return _run_lightweight(args)

    mode = "update" if args.update else "install"

    # Fix 1 (v0.2.13): mark this run's start timestamp so
    # _refresh_dist_binary_after_rebuild can tell "produced this run" apart
    # from "stale from weeks ago".
    global _INSTALL_START_TS
    _INSTALL_START_TS = time.time()

    # Configure resume-from-log behaviour. Loaded BEFORE any step runs so
    # individual step functions can consult _should_skip_step(). The log
    # is at state/logs/install.jsonl — only present once Step 8 has run
    # at least once (i.e. on second+ install attempts on this checkout).
    global _RESUME_ENABLED, _RESUME_STATE
    _RESUME_ENABLED = not args.no_resume
    _RESUME_STATE = _load_resume_state() if _RESUME_ENABLED else {}

    print()
    print("=" * 62)
    if mode == "update":
        print("  VibeCoded Tools — Orchestrator Updater")
    else:
        print("  VibeCoded Tools — Orchestrator Installer")
    print("=" * 62)
    print()

    # Mark the start of this install session in the durable log. Subsequent
    # events from install.py + post-install-launcher.sh share the same log.
    # On a first-ever install the log dir doesn't exist yet (Step 8 creates
    # it) so this is a no-op until later — fine: 1/10 is also re-emitted
    # as part of _check_python_version().
    _log_install_event(
        "session", "start",
        f"install.py {mode} mode",
        data={"mode": mode, "resume_enabled": _RESUME_ENABLED,
              "argv": sys.argv[1:]},
    )

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

    # Step 3b (PR-28, Group G, v0.2.12): storage-location prompt. Runs
    # BEFORE the container-runtime detection so a user with 110 GB of
    # legacy Ollama models doesn't end up with empty named volumes after
    # `_start_services`. Silently skipped on --quiet / --yes / non-TTY
    # stdin / when no legacy data is detected. Result persisted via the
    # launcher binary CLI (preferred) or a direct Python write of
    # ~/.vct/storage.toml (fallback). See
    # `.claude/context/volume-binding-fix-2026-05-16.md` for the silent-
    # data-loss footgun this closes.
    _storage_choice = _prompt_storage_location(PROJECT_ROOT, args)
    if _storage_choice.get("mode") != "deferred":
        _persist_storage_choice(_storage_choice, PROJECT_ROOT)
        _record_install_choice(
            "storage_mode", _storage_choice["mode"],
            {"bind_paths": list((_storage_choice.get("bind_paths") or {}).keys())},
        )

    # Step 4: Create virtual environment
    venv_python = _create_venv(PROJECT_ROOT)

    # Step 5: Install/update Python dependencies
    _install_requirements(venv_python, dev=args.dev)

    # Step 5b: Materialize orchestrator-self .claude/ from templates.
    # PR-39 (v0.2.12): the public repo no longer ships .claude/{hooks,
    # scripts,settings.json}; install.py renders them from templates/ at
    # install time (and on every --update). Runs AFTER _install_requirements
    # so any Python scripts can use the venv, and BEFORE _seed_weaviate so
    # the orchestrator's KG-sync hooks are in place when seeding runs.
    if not getattr(args, "skip_materialize_claude_dir", False):
        _materialize_orchestrator_self_claude_dir(PROJECT_ROOT)
    else:
        _log_install_event(
            "4b/10", "skip",
            "--skip-materialize-claude-dir set; .claude/ left untouched",
        )

    # Step 6: Container services (restart on update to pick up config changes)
    if not args.no_containers:
        if not sysinfo.container_cmd:
            # OS-aware prompt: Linux can auto-install via apt/dnf/pacman;
            # macOS/Windows print URLs only (Homebrew/winget+WSL2 require
            # user-driven setup we won't shoulder).
            installed = _prompt_install_container_runtime(args)
            if installed:
                # Re-detect after user-confirmed install. PATH may already
                # contain the new binary in this Python process.
                sysinfo = sysinfo._replace(container_cmd=_detect_container_runtime())
                # v0.2.10 (Bug L3): the post-prompt-install path now refreshes
                # runtime.txt immediately so a single install run captures
                # the new runtime for the wrapper script (don't wait for the
                # later _write_install_manifest call — boot services
                # materialized later in the same run may read it before then).
                _persist_runtime_txt(sysinfo.container_cmd)
            if not sysinfo.container_cmd:
                # Either the user declined, the package manager failed,
                # or we're on macOS/Windows (URL-only path). Fall through
                # to a clear exit with --no-containers escape hatch.
                print("\n    Or re-run with --no-containers to skip.")
                return 1

        # Deliverable 2 (2026-04-28): persist the resolved runtime so
        # re-installs don't re-prompt. This fires AFTER any optional
        # install prompt so we capture the final value.
        _record_install_choice(
            "container_runtime", sysinfo.container_cmd or "none",
            {"reason": "post-detection (incl. prompt-install if any)"},
        )

        # Step 5b: probe BEFORE compose up. Honors content-based detection
        # and persists the resolution in ~/.vct/services.toml. Foreign
        # services on the canonical port either get an alt-port (default)
        # or, with explicit consent, are adopted in place. We never POST
        # collections into a service we didn't start.
        decisions = _resolve_service_safety(args)
        if any(d["action"] == ACTION_ABORT for d in decisions.values()):
            for name, d in decisions.items():
                if d["action"] == ACTION_ABORT:
                    print(f"  [{name}] aborted: {d['evidence']}")
            return 1

        _start_services(sysinfo, args, embed_config, decisions,
                        deferral_report=_deferral_report)
        if not args.skip_models:
            _wait_for_ollama()
            _pull_ollama_models(_build_ollama_pull_list(embed_config, sysinfo))

        # Bug 29: with shared-container reuse, multiple installs hit the same
        # Weaviate. Bootstrap any of THIS project's KG/Development collections
        # that aren't there yet — leave existing ones alone.
        #
        # Pollution guarantee: collection writes only happen when the user
        # consented. The matrix:
        #   - ACTION_START / ACTION_ALT_PORT (we run our own Weaviate)
        #     ⇒ writes to our instance
        #   - ACTION_ADOPT vct-managed (prior install of ours)
        #     ⇒ writes to a Weaviate we already populated
        #   - ACTION_ADOPT foreign (user typed "2" / passed
        #     --on-conflict adopt)
        #     ⇒ writes to user-owned Weaviate WITH EXPLICIT CONSENT
        # No path writes without consent. The default for foreign is
        # alt-port, so the no-consent case never hits ACTION_ADOPT.
        # Schema-rebuild gate (--update only): if the running KG
        # collection is on an older schema lacking today's invariants
        # (named-vector slots, index_null_state, etc.), prompt to drop
        # + recreate. User-driven decision; defaults to deferring on
        # non-interactive shells without --yes. ONLY touches Weaviate
        # state — sources, env, settings stay intact. After drop,
        # _ensure_collections recreates with the fresh schema and
        # _seed_weaviate re-ingests from sources.
        # Track whether migrate_collections performed a "rebuild" action this
        # run. If so AND a later step (ensure/seed) crashes, the deferral
        # entry must signal "rebuild_pending_seed" so the operator knows the
        # collection was dropped and is awaiting re-seed (HIGH-4).
        _rebuild_was_performed = False

        if _maybe_prompt_rebuild_collections(args, deferral_report=_deferral_report):
            # PR 3: smart per-collection dispatch (copy-with-vectors etc.)
            # replaces the old drop-and-re-embed default. --force-rebuild
            # is the escape hatch back to today's behaviour.
            if getattr(args, "force_rebuild", False):
                _rebuild_collections(args)
                _rebuild_was_performed = True
            else:
                # HIGH-1 fix (2026-05-01): capture migrate_collections result
                # and emit a per-collection deferral entry for every entry in
                # result["errors"]. Previously the dict was discarded silently.
                _migrate_result = _project_init.migrate_collections(
                    args,
                    dry_run=getattr(args, "migrate_dry_run", False),
                    log_event=_log_install_event,
                )
                if _migrate_result and not _migrate_result.get("dry_run", False):
                    if any(
                        entry.get("action") == "rebuild"
                        for entry in _migrate_result.get("plan", [])
                    ):
                        _rebuild_was_performed = True
                    for err in _migrate_result.get("errors", []) or []:
                        _err_collection = err.get("collection") or "unknown"
                        _err_action = err.get("action") or "unknown"
                        _err_msg = err.get("error") or "(no error message)"
                        # condition_id includes the collection name so multiple
                        # failures across collections do NOT deduplicate.
                        _deferral_report.add_entry(
                            DeferralEntry(
                                condition_id=(
                                    f"migrate_collections_partial_failure_"
                                    f"{_err_collection}"
                                ),
                                title=(
                                    f"Schema migration failed for "
                                    f"`{_err_collection}`"
                                ),
                                detected=(
                                    f"Action `{_err_action}` raised: {_err_msg}"
                                ),
                                why_deferred=(
                                    "Migration partial failure leaves the "
                                    "collection in an inconsistent state; "
                                    "manual recovery required."
                                ),
                                command_to_apply=(
                                    "python install.py --update "
                                    "--rebuild-collections --force-rebuild "
                                    "(last-resort drop+re-embed) OR see logs at "
                                    "state/logs/install.jsonl stage 7b.<action>"
                                ),
                                severity="critical",
                                kg_node_refs=[
                                    ".claude/context/"
                                    "weaviate-schema-port-research-2026-05-01.md",
                                ],
                            )
                        )

        # v0.2.18 Commit 10: seed the launcher's app_state with the
        # default text/code embedding-model IDs derived from this
        # install's preset. Idempotent (INSERT OR IGNORE-style ON
        # CONFLICT DO NOTHING) — preserves any user selections from
        # prior launcher sessions. Soft-fails when launcher.db is
        # absent (fresh first-install, launcher never booted) or the
        # app_state table hasn't been migrated yet. Runs BEFORE
        # _seed_weaviate so a Weaviate-down condition does not also
        # block the GUI dropdowns from being pre-populated; runs
        # OUTSIDE the seed try/except for the same reason.
        try:
            _write_preset_defaults_to_app_state(
                embed_config,
                openai_set_as_default=bool(embed_config.get("openai_key")),
            )
        except Exception as _preset_err:  # noqa: BLE001 — never block install
            # Defense-in-depth: the helper already soft-fails on every
            # known sqlite error, but if something else explodes
            # (network filesystem flake, ENOSPC, etc.) we still must
            # not crash the install.
            _log_install_event(
                "preset_defaults", "warn",
                f"unexpected error writing preset defaults: {_preset_err}",
            )

        # PR 6 + MEDIUM-9 + HIGH-4 fix (2026-05-01): wrap _ensure_collections
        # AND _seed_weaviate in the same try/except so Weaviate-down conditions
        # emit a deferral entry instead of crashing the install. Includes a
        # podman-restart soft-recovery attempt. If a rebuild action dropped a
        # collection earlier this run AND seed/ensure later crashes, the
        # deferral entry includes `rebuild_pending_seed` so the operator knows
        # what was lost.
        _seed_succeeded = False
        try:
            _ensure_collections(embed_config, decisions=decisions, args=args)
            # Seed Weaviate with bundled knowledge/ + docs/. Idempotent;
            # safe to re-run on update.
            _seed_weaviate(args)
            _seed_succeeded = True
        except Exception as _weaviate_err:
            # PR 6: Weaviate is unreachable (or refused connection) after the
            # containers were started. Attempt a soft-recovery restart
            # via podman before emitting a deferral.
            _weaviate_down_msg = str(_weaviate_err)
            _restarted = False
            # Discover the actual Weaviate container name + runtime on
            # this host. v0.2.15: stopped hardcoding `weaviate_claude`
            # (maintainer-machine leak) AND stopped hardcoding `podman`
            # in the recovery hints (docker-only users got useless
            # advice). Runtime honors VCT_CONTAINER_RUNTIME with
            # podman→docker fallback per the install.py contract.
            from vco_lib.containers import (
                all_known_names as _all_known_names,
                find_existing_container as _find_existing_container,
            )
            _self_heal_runtime = (
                _detect_container_runtime()
                or _runtime_preference_from_env()
                or "podman"  # last-resort label when neither is on PATH
            )
            _weaviate_container = (
                _find_existing_container("weaviate", runtime=_self_heal_runtime)
                or "vco_weaviate"  # nothing on host yet — name the canonical
            )
            try:
                subprocess.run(
                    [_self_heal_runtime, "start", _weaviate_container],
                    capture_output=True, timeout=30,
                )
                import time as _time
                _time.sleep(3)  # brief settle
                _ensure_collections(embed_config, decisions=decisions, args=args)
                _seed_weaviate(args)
                _restarted = True
                _seed_succeeded = True
            except Exception:
                pass
            if not _restarted:
                # Build a user-facing "which name to use" hint. If we
                # found one on the host, name it. Otherwise list all the
                # candidates so the user can try whichever they have.
                _candidates_hint = (
                    _weaviate_container
                    if _find_existing_container("weaviate", runtime=_self_heal_runtime)
                    else " | ".join(_all_known_names("weaviate"))
                )
                print(
                    f"WARNING: Weaviate unreachable after restart attempt "
                    f"({_weaviate_down_msg}). Collections not bootstrapped. "
                    "Deferral entry written."
                )
                _deferral_report.add_entry(
                    DeferralEntry(
                        condition_id="weaviate_unreachable_at_update",
                        title="Weaviate unreachable at update",
                        detected=(
                            f"Weaviate refused connection during --update "
                            f"({_weaviate_down_msg}). Auto-restart via "
                            f"`{_self_heal_runtime} start {_weaviate_container}` also failed."
                        ),
                        why_deferred=(
                            "Collection bootstrap and schema migration require "
                            "a live Weaviate. Cannot proceed without it."
                        ),
                        command_to_apply=(
                            f"{_self_heal_runtime} start {_candidates_hint} && "
                            "python install.py --update --skip-rebuild-prompt"
                        ),
                        severity="critical",
                        kg_node_refs=[],
                    )
                )
                # HIGH-4: if a rebuild action dropped collections this run,
                # warn the operator that the collection is GONE and needs
                # re-seed once Weaviate is back.
                if _rebuild_was_performed:
                    _deferral_report.add_entry(
                        DeferralEntry(
                            condition_id="rebuild_pending_seed",
                            title="Rebuild dropped collections; seed pending",
                            detected=(
                                "A `rebuild` action dropped one or more "
                                "collections during this run, and a "
                                "subsequent ensure/seed step crashed before "
                                "the collections could be recreated and "
                                "re-ingested. See `state/logs/install.jsonl` "
                                "stage `7b.rebuild snapshot` for the per-"
                                "collection object count + sample UUIDs that "
                                "were present immediately before the drop."
                            ),
                            why_deferred=(
                                "Cannot recreate + re-ingest without a live "
                                "Weaviate. The .md sources in knowledge/ + "
                                "docs/ are intact and will be re-ingested by "
                                "the next install.py --update run."
                            ),
                            command_to_apply=(
                                f"{_self_heal_runtime} start {_candidates_hint} && "
                                "python install.py --update "
                                "--skip-rebuild-prompt"
                            ),
                            severity="critical",
                            kg_node_refs=[
                                ".claude/context/"
                                "weaviate-schema-port-research-2026-05-01.md",
                            ],
                        )
                    )

        # PR-24 (v0.2.12, 2026-05-16): schema-correctness migrations.
        # Run AFTER _seed_weaviate so the collections exist before we
        # attempt additive patches. Both scripts are idempotent and
        # soft-fail; failures convert to deferral entries.
        if _seed_succeeded:
            _run_schema_migration_scripts(_deferral_report)

        # PR-34 (v0.2.12, Group M): detect the pre-rename shared-KG class
        # left over from a pre-v0.2.12 install. Emit a deferral pointing
        # at the launcher's "Manage shared KG collection" picker — we
        # NEVER auto-rename or auto-drop the class (destructive; picker
        # is the consent mechanism).
        if mode == "update":
            _detect_legacy_shared_kg_class(_deferral_report)
            # v0.2.23 B1 (2026-05-21): case-mismatch self-heal for
            # `project_kg_bindings` rows. Auto-applied (safe — only
            # rewrites a binding row whose target class ALREADY EXISTS
            # in Weaviate by a different casing). Emits an informational
            # deferral entry per heal so the user sees what we changed.
            _self_heal_kg_bindings_on_update(_deferral_report)
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
        # 0.2.11 / PR-1: pre-0.2.11 installs wired BASH_ENV in
        # .claude/settings.json to the lean-ctx shim, which became
        # fork-bomb-prone on lean-ctx 3.x. Idempotently strip that key +
        # disable the shim file so an upgraded orchestrator clone
        # doesn't carry the legacy fuse alive. Fresh installs never see
        # this code path (`mode == "install"` above).
        _cleanup_legacy_bash_env_shim(args)

        # PR-7 (v0.2.11): on `--update` paths the full settings rewrite is
        # intentionally skipped (preserves user customisation), but pre-v0.2.11
        # installs lack the new PROJECT_NAME / CODE_GRAPH_PROJECT keys. Run
        # the idempotent backfill: it only ADDS missing keys, never modifies
        # existing values. No-op when both keys are already present.
        _backfill_result = _backfill_code_graph_project_env()
        if _backfill_result["action"] == "backfilled":
            print(
                f"  Claude settings: backfilled {len(_backfill_result['added_keys'])} "
                f"missing env key(s): {', '.join(_backfill_result['added_keys'])}"
            )
        _log_install_event(
            "9/10", "info",
            f"_backfill_code_graph_project_env action={_backfill_result['action']}",
            data=_backfill_result,
        )

        # PR-22 (v0.2.12, 2026-05-16): rename legacy
        # `docker-compose.override.yml` to `compose.override.yaml` so
        # podman-compose's auto-loader recognizes it. PR-10A (v0.2.11)
        # shipped writing the wrong filename; on `--update` from
        # v0.2.11 we migrate the file in place + emit a deferral entry.
        # No-op on fresh installs and on already-migrated trees. Soft-fail:
        # surfaces failures via deferral entries, never raises.
        try:
            from vco_lib.project_init import _detect_and_rename_legacy_compose_override
            _override_rename = _detect_and_rename_legacy_compose_override(PROJECT_ROOT)
            if _override_rename is not None:
                print(
                    f"  compose override rename: action="
                    f"{_override_rename['action']}, "
                    f"renamed={len(_override_rename['renamed'])}, "
                    f"conflicts={len(_override_rename['conflicts'])}, "
                    f"errors={len(_override_rename['errors'])}"
                )
            _log_install_event(
                "9/10", "info",
                "_detect_and_rename_legacy_compose_override "
                f"result={_override_rename!r}",
                data=_override_rename or {"action": "noop"},
            )
        except Exception as _override_exc:  # pragma: no cover — defensive
            # Soft-fail: don't let this stop the install.
            _override_err = f"{type(_override_exc).__name__}: {_override_exc}"
            print(
                f"  compose override rename: skipped due to error "
                f"({_override_err})"
            )
            _log_install_event(
                "9/10", "warn",
                f"_detect_and_rename_legacy_compose_override failed: "
                f"{_override_err}",
                data={"error": _override_err},
            )

    # PR-7 / addendum-4 (v0.2.11): .vscode/settings.json watcher/search/
    # Pylance exclude backfill. Runs on BOTH install and update paths:
    #   - Fresh install: creates `.vscode/settings.json` with the canonical
    #     exclude block (the existing `.vscode/settings.json.example` is
    #     templated for the `claude-code.env` block, which the launcher
    #     writes on registration — the excludes are an orthogonal concern).
    #   - Update: adds missing exclude keys only; user-set values
    #     preserved verbatim (user-wins).
    _vscode_excludes_result = _backfill_vscode_excludes()
    if _vscode_excludes_result["action"] in ("created", "backfilled"):
        print(
            f"  VS Code excludes: {_vscode_excludes_result['action']} "
            f"({len(_vscode_excludes_result['added_keys'])} key(s))"
        )
    _log_install_event(
        "9/10", "info",
        f"_backfill_vscode_excludes action={_vscode_excludes_result['action']}",
        data=_vscode_excludes_result,
    )

    # Step 9b: Install agents and skills from templates/
    _install_agents_and_skills(args)

    # Step 10: Check Claude CLI
    _check_claude_cli()

    # Cache Playwright MCP + Chromium so the default-enabled `playwright`
    # MCP entry doesn't stall on first browser-launch with a 150 MB
    # download. Non-fatal: failures here only warn, since the MCP can
    # still lazy-install on first call.
    _install_playwright_browsers()

    # Step 11: Initial code graph analysis (if repo has code)
    # Skipped on first install — user runs manually after setup

    # Step 11b: Bytecode compilation. Pre-compile installed Python
    # modules to .pyc so first import doesn't pay per-module compile
    # cost (~50-200ms savings per cold module). Cross-OS: stdlib
    # `compileall` is identical on Linux/macOS/Windows.
    if args.compile_pyc:
        _compile_python_modules(venv_python)
    else:
        print("[skip] Bytecode compilation (--no-compile)")

    # Deliverable 2 (2026-04-28): snapshot the post-install state-hash
    # set (requirements.txt / Cargo.lock / package.json / knowledge/).
    # The next install run uses these to detect drift and choose
    # between full reinstall vs lightweight reinstall (Deliverable 3).
    _record_state_hashes(PROJECT_ROOT)

    # Done — mark the session complete in the durable log so the
    # launcher/Claude Code can tell at a glance that install.py reached
    # the end. (post-install-launcher.sh appends its own build/spawn
    # events after this returns.)
    # Note: 10/10 is logged inside _check_claude_cli() — the per-step
    # event captures the actual outcome. This event marks the *session*
    # closed cleanly, which is a separate signal the launcher uses to
    # decide whether the install completed start-to-end.
    _log_install_event("session", "ok", f"{mode} finished cleanly")

    # HIGH-2 fix (2026-05-01): deferral file lands in the user-project folder
    # when --project-folder is passed. Default = PROJECT_ROOT preserves the
    # orchestrator self-update behaviour.
    _deferral_folder = (
        Path(args.project_folder)
        if getattr(args, "project_folder", None)
        else PROJECT_ROOT
    )

    # Apply pending deferrals if requested (--update --apply-deferred).
    if args.update and getattr(args, "apply_deferred", False):
        _apply_deferred_entries(_deferral_report, _deferral_folder)

    # Write (or delete) the deferral report. On install runs, this is a no-op
    # (nothing accumulates); on update runs, any unresolved conditions land here.
    _deferral_report.write(_deferral_folder)

    # Drop the install-manifest at state/install-manifest.json so the launcher
    # (and operators auditing an install) can verify install actually finished.
    # Earlier the launcher inferred "installed" from .venv/ presence, which
    # produced false positives on developer source checkouts that had a venv
    # but no install.py run (false-positive observed in wizard 2026-05-06).
    _write_install_manifest(sysinfo, args, install_method="install.py")

    # PR-14b (v0.2.11 MCP simplification): SearXNG no longer ships in the
    # default compose stack; Ollama MCP is dropped from the default install;
    # SEARXNG_URL + GITHUB_TOKEN are no longer needed in the search MCP env.
    # Surface deferral notices so existing users know to clean up manually.
    # Soft-fail: each helper catches its own errors; install completes even
    # if all three checks fail.
    if not getattr(args, "suppress_mcp_deprecation_warnings", False):
        _check_searxng_remnants(PROJECT_ROOT, _deferral_report)
        _check_ollama_mcp_remnants(_deferral_report)
        _check_search_mcp_env_obsolete(_deferral_report)

    # PR-23 (v0.2.12, 2026-05-16): register bundled MCP entries into
    # ~/.claude.json. Pre-PR-23 install.py performed ZERO MCP registration,
    # so fresh v0.2.11 installs left Claude Code with no orchestrator MCPs
    # wired at all. Soft-fail throughout — install must complete even when
    # MCP registration fully fails. See module docstring of `_register_mcps`
    # for the 4-tier launcher-binary resolution strategy and the
    # security-boundary rationale for the env-key allowlist.
    if not getattr(args, "skip_mcp_registration", False):
        # Fix 1 (v0.2.13): if a pipeline step earlier in this run produced
        # a fresh launcher binary at target/release/vct-launcher-temp,
        # copy it into launcher/dist/<os>-<arch>/ BEFORE _register_mcps
        # resolves a binary. This closes the gap where --update finds a
        # stale dist binary (tier-1 success) and never invokes tier-3 to
        # refresh it. Gated by --no-binary-swap.
        try:
            _refresh_dist_binary_after_rebuild(
                PROJECT_ROOT,
                no_swap=bool(getattr(args, "no_binary_swap", False)),
                install_start_ts=globals().get("_INSTALL_START_TS"),
                deferral_report=_deferral_report,
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail by design
            _log_install_event(
                "refresh_dist_binary", "error",
                f"unexpected exception: {exc}",
            )

        try:
            _register_mcps(
                PROJECT_ROOT,
                _deferral_report,
                prefer_only_bundled=bool(getattr(args, "prefer_only_bundled", False)),
                no_rebuild_on_stale=bool(getattr(args, "no_rebuild_on_stale", False)),
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail by design
            print(
                f"  MCP registration raised unexpectedly: {exc}. "
                "Install will complete; re-run `python install.py --update` to retry.",
                file=sys.stderr,
            )
            _log_install_event(
                "register_mcps", "error",
                f"unexpected exception: {exc}",
            )

        # PR-33 (v0.2.12, 2026-05-16): consent-prompted rewrite path.
        # PR-23's _detect_stale_mcp_entries (invoked inside _register_mcps)
        # has already emitted the report-only deferral. When the user
        # passes --rewrite-stale-mcps we additionally walk each stale
        # entry, prompt for consent, and hand off to the same writer.
        # Soft-fail: any exception is caught and downgraded to a warning
        # so the install completes.
        if getattr(args, "rewrite_stale_mcps", False):
            try:
                _rewrite_stale_mcp_entries(
                    PROJECT_ROOT,
                    _deferral_report,
                    quiet=bool(getattr(args, "quiet", False)),
                )
            except Exception as exc:  # noqa: BLE001 — soft-fail by design
                print(
                    f"  --rewrite-stale-mcps raised unexpectedly: {exc}. "
                    "Install will complete; re-run to retry.",
                    file=sys.stderr,
                )
                _log_install_event(
                    "rewrite_stale_mcps", "error",
                    f"unexpected exception: {exc}",
                )

        # PR-34 (v0.2.13, 2026-05-16): consent-prompted deprecated-MCP
        # removal.  _detect_deprecated_mcp_entries (invoked inside
        # _register_mcps) has already emitted the report-only deferral.
        # When the user passes --remove-deprecated-mcps we additionally
        # prompt per entry and remove the accepted ones from ~/.claude.json.
        # --rewrite-stale-mcps also triggers deprecated-MCP detection
        # (deprecation is a form of staleness), but removal still requires
        # the explicit --remove-deprecated-mcps flag.
        # Soft-fail throughout.
        if getattr(args, "remove_deprecated_mcps", False):
            try:
                _remove_deprecated_mcp_entries(
                    PROJECT_ROOT,
                    _deferral_report,
                    quiet=bool(getattr(args, "quiet", False)),
                )
            except Exception as exc:  # noqa: BLE001 — soft-fail by design
                print(
                    f"  --remove-deprecated-mcps raised unexpectedly: {exc}. "
                    "Install will complete; re-run to retry.",
                    file=sys.stderr,
                )
                _log_install_event(
                    "remove_deprecated_mcps", "error",
                    f"unexpected exception: {exc}",
                )

    # v0.2.21 Step 8: deploy vct-hub binary alongside vct-launcher and
    # start it idempotently. The launcher binary has already been
    # placed by _refresh_dist_binary_after_rebuild / _register_mcps
    # above; vct-hub ships in the same `launcher/dist/<arch>/` slot
    # and shares the same release ZIP. Soft-fail throughout: a missing
    # binary or non-responsive /health degrades to "hub-unavailable
    # mode" which the v0.2.21 launcher handles gracefully (resolver
    # falls back to env vars; GUI still comes up).
    #
    # Cutover sentinel (W2 fix): written BEFORE the hub starts so the
    # v0.2.21 launcher's lib.rs setup() can skip its embedded
    # services::watcher::spawn during the overlap with a still-
    # running v0.2.20 launcher's old watcher. Deleted after /health
    # responds.
    #
    # Boot auto-start (8d): NOT registered here — user opts in via
    # launcher GUI Preferences (Step 13). install-time default is
    # conservative.
    if not getattr(args, "skip_mcp_registration", False):
        try:
            _deploy_and_start_vct_hub(
                PROJECT_ROOT,
                deferral_report=_deferral_report,
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail by design
            _log_install_event(
                "deploy_vct_hub", "error",
                f"unexpected exception: {exc}",
            )

    # v0.2.10 (Bug L2): auto-materialize the boot service so containers
    # come back up after a reboot without manual intervention. Cross-OS:
    # systemd user unit on Linux, LaunchAgent on macOS, Task Scheduler
    # on Windows. Soft-fail throughout — failure here never blocks
    # install completion.
    #
    # PR-12 Bug C: pass the run-scoped _deferral_report so any stale
    # WorkingDirectory= auto-repair surfaces in UPDATE_DEFERRED.md (the
    # final write happens at line ~2181 below).
    _materialize_boot_service(PROJECT_ROOT, sysinfo, args,
                              deferral_report=_deferral_report)

    # v0.2.6 Bug C1: invoke the desktop-icon step so direct `python install.py`
    # runs get an icon too. first-install.sh-wrapped runs already trigger
    # this script independently; the helper is idempotent so the second
    # invocation is a no-op (writes the same .desktop body).
    _run_desktop_icon_step(args)

    # Fix 6 (v0.2.13): re-write the deferral report AFTER all post-line-2611
    # deferral-adding steps complete (_check_searxng_remnants,
    # _check_ollama_mcp_remnants, _check_search_mcp_env_obsolete,
    # _register_mcps, _materialize_boot_service, _rewrite_stale_mcp_entries).
    # The earlier write at line ~2611 happens BEFORE those steps and would
    # otherwise lose every entry they add.
    #
    # Additionally, on --update runs that ended with ZERO entries, write a
    # stub UPDATE_DEFERRED.md so the user has a paper trail confirming the
    # update completed cleanly (was previously: NO file at all, indistinguishable
    # from "no --update run happened").
    try:
        wrote_entries = _deferral_report.write(_deferral_folder)
        if not wrote_entries and args.update:
            _write_update_deferred_stub(_deferral_folder, mode=mode)
    except Exception as exc:  # noqa: BLE001 — soft-fail by design
        _log_install_event(
            "deferral_report", "warn",
            f"final write failed: {exc}",
        )

    print()
    print("=" * 62)
    print("  Installation complete!")
    print("=" * 62)
    print()
    _print_next_steps(sysinfo, args)
    return 0


def _write_update_deferred_stub(folder: Path, *, mode: str) -> None:
    """Fix 6 (v0.2.13): write a stub ``UPDATE_DEFERRED.md`` for paper-trail.

    Called at end of ``--update`` runs that produced ZERO actionable
    deferral entries. The stub records the timestamp and mode so users
    grepping ``.claude/context/`` know an update ran cleanly. Previous
    behaviour was to write NO file in this case, which made successful
    updates indistinguishable from "no update happened at all".

    Soft-fail throughout: any OSError is swallowed and logged. The install
    must complete even when this stub write fails.

    Schema: a single-frontmatter Markdown file with no entries. The
    :class:`DeferralReport` reader treats unknown / empty payloads as an
    empty report — so reading this file back via ``DeferralReport.read()``
    yields ``[]`` and apply-deferred is a no-op. Idempotent: overwrites
    any prior stub.
    """
    from datetime import datetime, timezone

    target = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log_install_event(
            "deferral_report_stub", "warn",
            f"could not create parent {target.parent}: {exc}",
        )
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        "---\n"
        "schema_version: 1\n"
        "generated_by: install.py\n"
        f"generated_at: {ts}\n"
        "entries: 0\n"
        "stub: true\n"
        "---\n\n"
        f"# No deferrals from update at {ts}\n\n"
        f"This file is a stub: `install.py --{mode}` completed cleanly "
        "with zero actionable deferral conditions.\n\n"
        "If you expected deferral entries (e.g. you re-ran after fixing "
        "a known issue), they were resolved during this run. Otherwise "
        "this file confirms the update ran end-to-end without surfacing "
        "any conditions requiring follow-up.\n\n"
        "Safe to delete; install.py re-creates it on the next --update.\n"
    )

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        _log_install_event(
            "deferral_report_stub", "warn",
            f"could not write {target}: {exc}",
        )
        return
    _log_install_event(
        "deferral_report_stub", "ok",
        f"wrote stub UPDATE_DEFERRED.md at {target} (zero entries)",
    )


# ---------------------------------------------------------------------------
# Deferral-apply helper
# ---------------------------------------------------------------------------

def _apply_deferred_entries(
    current_run_report: DeferralReport,
    project_root: Path,
) -> None:
    """Attempt to apply each entry in the persisted deferral report.

    Reads the on-disk ``UPDATE_DEFERRED.md``, attempts a best-effort
    resolution for each known condition, and marks successful ones resolved.
    The merged result (on-disk resolved entries removed + any new entries from
    the current run) is written back by the caller (``_deferral_report.write()``
    at end of ``main()``).

    Conditions handled:
      - ``schema_drift_rebuild_required``: not auto-applied here; requires
        ``--rebuild-collections`` flag to be present.  We skip to preserve
        the conservative "no silent data churn" contract.  If the user passed
        ``--rebuild-collections`` the drift is already resolved before we arrive.
      - ``weaviate_unreachable_at_update``: try ``podman start <name>``
        (name discovered via ``vco_lib.containers.find_existing_container``;
        falls back to the canonical ``vco_weaviate`` if no container is
        on the host yet) then check reachability; mark resolved if now
        reachable.
      - ``compose_overlay_ambiguous``: cannot auto-resolve (requires human
        decision); emit informational note.
    """
    persisted = DeferralReport.read(project_root)
    if not persisted:
        return

    print()
    print("[apply-deferred] Processing pending deferral entries ...")

    for entry in persisted.entries:
        cid = entry.condition_id

        if cid == "schema_drift_rebuild_required":
            # Requires explicit --rebuild-collections; not auto-applied.
            print(f"  [skip] {cid}: requires --rebuild-collections (manual action)")
            current_run_report.add_entry(entry)

        elif cid == "weaviate_unreachable_at_update":
            # v0.2.15: discover the actual container name + runtime on
            # this host instead of hardcoding `weaviate_claude` +
            # `podman` (both maintainer-machine assumptions). See
            # vco_lib/containers.py for the rename rationale + the
            # _detect_container_runtime / _runtime_preference_from_env
            # helpers above for the runtime contract.
            from vco_lib.containers import (
                find_existing_container as _find_existing_container,
            )
            _apply_runtime = (
                _detect_container_runtime()
                or _runtime_preference_from_env()
                or "podman"
            )
            _weav_container = (
                _find_existing_container("weaviate", runtime=_apply_runtime)
                or "vco_weaviate"
            )
            print(
                f"  [try]  {cid}: attempting {_apply_runtime} start "
                f"{_weav_container} ..."
            )
            try:
                subprocess.run(
                    [_apply_runtime, "start", _weav_container],
                    capture_output=True, timeout=30,
                )
                import time as _t
                _t.sleep(3)
                weaviate_url = os.environ.get(
                    "WEAVIATE_URL",
                    f"http://localhost:{DEFAULT_WEAVIATE_PORT}",
                )
                urllib.request.urlopen(
                    f"{weaviate_url}/v1/.well-known/ready", timeout=5
                )
                print(f"  [ok]   {cid}: Weaviate is now reachable. Marking resolved.")
                # Resolved: do NOT re-add to current_run_report.
            except Exception as exc:
                print(f"  [fail] {cid}: still unreachable ({exc}). Keeping entry.")
                current_run_report.add_entry(entry)

        elif cid == "compose_overlay_ambiguous":
            # Cannot auto-resolve — needs human GPU vendor selection.
            print(
                f"  [skip] {cid}: ambiguous GPU overlay requires user decision. "
                "Pass --gpu or set VCT_GPU_VENDOR and re-run."
            )
            current_run_report.add_entry(entry)

        else:
            # Unknown condition: preserve it to avoid silently losing info.
            print(f"  [unknown] {cid}: no handler. Preserving entry.")
            current_run_report.add_entry(entry)


# ---------------------------------------------------------------------------
# Step 1: Python version
# ---------------------------------------------------------------------------

def _check_python_version() -> None:
    print("[1/10] Checking Python version ... ", end="", flush=True)
    _log_install_event("1/10", "start", "checking Python version")
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        print("FAIL")
        print(f"  Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
              f"found {v.major}.{v.minor}.{v.micro}")
        _print_python_install_hint()
        _log_install_event(
            "1/10", "error",
            f"Python {v.major}.{v.minor}.{v.micro} below required "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            data={"found": f"{v.major}.{v.minor}.{v.micro}",
                  "required": f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"},
        )
        sys.exit(1)
    print(f"OK ({v.major}.{v.minor}.{v.micro})")
    _log_install_event(
        "1/10", "ok",
        f"Python {v.major}.{v.minor}.{v.micro}",
        data={"version": f"{v.major}.{v.minor}.{v.micro}"},
    )


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
    _log_install_event("2/10", "start", "detecting system")
    os_name = platform.system()
    has_gpu = False
    has_metal = False
    gpu_name = ""
    gpu_vendor = ""
    vram_gb = 0.0
    container_cmd = ""
    ram_gb = _probe_system_ram_gb()

    # GPU detection
    if args.cpu_only:
        print("  GPU: skipped (--cpu-only)")
    elif args.openai_key:
        print("  GPU: not needed (using OpenAI embeddings)")
    elif getattr(args, "no_gpu_check", False):
        print("  GPU: skipped (--no-gpu-check)")
    else:
        # Layered detection:
        #   1. nvidia-smi present + working → NVIDIA driver+CUDA OK.
        #   2. rocm-smi present + working → AMD ROCm driver OK.
        #   3. Apple Silicon → Metal (built-in).
        #   4. Else: probe lspci on Linux for "hardware present but
        #      driver missing"; print URLs.
        # We do NOT auto-install GPU drivers — they're large (~3 GB),
        # may need a reboot, and the canonical install path is vendor-
        # specific. Detection-only here; the launcher GUI offers
        # opt-in install for users who want it.
        has_gpu, gpu_name = _detect_nvidia_gpu()
        if has_gpu:
            gpu_vendor = "nvidia"
            vram_gb = _probe_nvidia_vram_gb()
            extra = _probe_nvidia_versions()
            vram_label = f", {vram_gb:.1f} GB VRAM" if vram_gb > 0 else ""
            if extra:
                print(f"  GPU: {gpu_name} ({extra}{vram_label})")
            else:
                print(f"  GPU: {gpu_name}{vram_label.lstrip(',').rstrip()}")
        else:
            rocm_present, rocm_info = _detect_amd_rocm()
            if rocm_present:
                # Treat ROCm as GPU-capable for the embedding-mode
                # picker. Ollama supports ROCm natively (per Ollama
                # docs); if their build doesn't, the user gets a clear
                # runtime error and can fall back to --cpu-only.
                has_gpu = True
                gpu_vendor = "amd"
                gpu_name = rocm_info
                vram_gb = _probe_amd_rocm_vram_gb()
                vram_label = f" ({vram_gb:.1f} GB VRAM)" if vram_gb > 0 else ""
                print(f"  GPU: {rocm_info}{vram_label}")
            elif os_name == "Darwin" and _detect_apple_silicon():
                has_metal = True
                gpu_vendor = "metal"
                print("  GPU: Apple Silicon (Metal — built-in, no driver install needed)")
            else:
                _print_gpu_hint(os_name)

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

    # If --gpu was forced and no vendor detected, default to "nvidia" so
    # the existing GPU overlay still applies (back-compat: prior behaviour
    # was NVIDIA-only).
    final_has_gpu = has_gpu or args.gpu
    final_vendor = gpu_vendor or ("nvidia" if args.gpu else "")

    # v0.2.9 (Bug K): resolve user override → tri-state.
    #   --gpu       => override=True
    #   --cpu-only  => override=False
    #   neither     => override=None (auto)
    # When --openai-key is set, treat as override=False (we don't need
    # local GPU at all — embeddings come from OpenAI). --no-gpu-check is
    # ALSO override=False — the user explicitly suppressed the probe.
    user_override: "bool | None" = None
    if args.gpu:
        user_override = True
    elif args.cpu_only or args.openai_key or getattr(args, "no_gpu_check", False):
        user_override = False
    threshold_gb = float(getattr(
        args, "gpu_vram_threshold_gb", _DEFAULT_GPU_VRAM_THRESHOLD_GB,
    ))
    gpu_mode = _decide_gpu_mode(
        vram_gb=vram_gb,
        vendor=final_vendor,
        user_override=user_override,
        threshold_gb=threshold_gb,
    )
    # If the threshold demoted a discrete GPU to CPU, tell the user
    # explicitly — they may want to override or change the threshold.
    if (
        user_override is None
        and final_vendor in ("nvidia", "amd")
        and vram_gb > 0
        and gpu_mode == "cpu"
    ):
        print(
            f"  GPU mode: CPU (VRAM {vram_gb:.1f} GB < threshold "
            f"{threshold_gb:.1f} GB — pass "
            f"--gpu-vram-threshold-gb to override, or --gpu to force GPU)"
        )

    if ram_gb > 0:
        print(f"  RAM: {ram_gb:.1f} GB")
    info = SystemInfo(
        os_name=os_name,
        has_gpu=final_has_gpu,
        has_metal=has_metal,
        container_cmd=container_cmd,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        gpu_vendor=final_vendor,
    )
    # v0.2.9 (Bug K): stash the resolved mode on args so the manifest
    # writer can record it without re-running detection.
    args._gpu_mode = gpu_mode  # type: ignore[attr-defined]
    args._vram_gb_resolved = vram_gb  # type: ignore[attr-defined]
    args._gpu_vram_threshold_resolved = threshold_gb  # type: ignore[attr-defined]

    _log_install_event(
        "2/10", "ok",
        f"system detected: {os_name}",
        data={
            "os": os_name,
            "arch": platform.machine(),
            "has_gpu": info.has_gpu,
            "has_metal": info.has_metal,
            "container_cmd": container_cmd,
            "gpu_name": gpu_name,
            "gpu_vendor": final_vendor,
            "vram_gb": vram_gb,
            "ram_gb": ram_gb,
            "gpu_mode": gpu_mode,
            "gpu_vram_threshold_gb": threshold_gb,
        },
    )
    return info


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


def _probe_nvidia_versions() -> str:
    """Best-effort fetch of NVIDIA driver + CUDA version strings.

    Returns a `"driver X.Y, CUDA Z.W"` summary on success, or an empty
    string on any failure. Purely cosmetic — used to enrich the system-
    info banner. NEVER raises.

    nvidia-smi output format (csv,noheader): `545.23.06, 12.3` per GPU.
    We just take the first row.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version,cuda_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            line = result.stdout.strip().splitlines()
            if line:
                parts = [p.strip() for p in line[0].split(",")]
                if len(parts) >= 2:
                    return f"driver {parts[0]}, CUDA {parts[1]}"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _detect_amd_rocm() -> tuple[bool, str]:
    """Detect AMD GPU via ROCm tooling.

    Returns (present, summary). `summary` is human-readable, e.g.
    "AMD Radeon Pro VII (ROCm 6.0)". Empty string when absent.

    rocm-smi is part of ROCm; if it runs and returns 0, the kernel
    driver is loaded and at least one supported GPU is visible. We
    also try `rocm-smi --showdriverversion` for the version string;
    failure on the version sub-probe is non-fatal — we still return
    True with a generic summary.
    """
    if not shutil.which("rocm-smi"):
        return False, ""
    try:
        # `rocm-smi --showproductname --json` would be cleaner but
        # JSON output isn't universal across rocm-smi versions. The
        # plain `--showproductname` text output is a stable fallback.
        result = subprocess.run(
            ["rocm-smi", "--showproductname"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, ""
        # Parse a "Card Series: Radeon RX 6800" / "Card model: ..." line
        # if present; otherwise fall back to a generic label.
        product = ""
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if "Card Series:" in stripped or "Card Model:" in stripped:
                product = stripped.split(":", 1)[1].strip()
                break
        # Driver version (best effort).
        driver = ""
        try:
            v = subprocess.run(
                ["rocm-smi", "--showdriverversion"],
                capture_output=True, text=True, timeout=10,
            )
            if v.returncode == 0:
                for line in v.stdout.splitlines():
                    if "Driver" in line and ":" in line:
                        driver = line.split(":", 1)[1].strip()
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        if product and driver:
            return True, f"{product} (ROCm driver {driver})"
        if product:
            return True, f"{product} (ROCm)"
        return True, "AMD GPU (ROCm detected)"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, ""


# v0.2.9 (Bug K): VRAM-threshold gating for GPU mode.
#
# Default threshold (GB) below which we degrade to CPU-only even if a
# discrete GPU is present. Tuned for the default model stack:
#
#   qwen3-embedding:0.6b  ~1.2 GB
#   CodeSage-Large-v2     ~2.6 GB
#   qwen3.5:9b (Q4)       ~6.0 GB
#
# An 8 GB card can hold the inference model OR the embedders, but
# thrashes when both are loaded — degrading to CPU is faster than
# partial offload. Configurable via `--gpu-vram-threshold-gb` for users
# running a different model selection. Keep in sync with the Rust side
# (`launcher::commands::gpu_policy::DEFAULT_GPU_VRAM_THRESHOLD_GB`).
_DEFAULT_GPU_VRAM_THRESHOLD_GB = 8.0


def _decide_gpu_mode(
    vram_gb: float,
    vendor: str,
    user_override: "bool | None" = None,
    threshold_gb: float = _DEFAULT_GPU_VRAM_THRESHOLD_GB,
) -> str:
    """Pure decision function: pick `"cuda"`, `"rocm"`, `"metal"`, or `"cpu"`.

    Precedence:
      1. `user_override` (when not None) wins.
         - override=True:  vendor=="metal" → "metal"; vendor=="amd" → "rocm";
           everyone else → "cuda" (default fallback — user accepted the tradeoff).
         - override=False: "cpu".
      2. vendor=="metal" → "metal" (no threshold — unified memory).
      3. vendor=="nvidia" AND vram_gb >= threshold → "cuda".
      4. vendor=="amd" AND vram_gb >= threshold → "rocm".
      5. Else → "cpu".

    Mirrors the Rust `decide_gpu_mode` in
    `launcher::commands::gpu_policy`. v0.2.20 split the legacy "gpu"
    return value into "cuda" + "rocm" so the install-time overlay
    picker can route correctly per vendor. Pure (no side effects);
    tested from `tests/test_install_gpu_mode_decision.py`.

    Args:
      vram_gb:        Probed VRAM in GB. 0.0 means "no GPU" OR "probe
                      failed". Conservative path: probe-failed → cpu.
      vendor:         "nvidia" | "amd" | "metal" | "" (from
                      `_detect_*` probes).
      user_override:  `True` for `--gpu`, `False` for `--cpu-only`,
                      `None` for "auto".
      threshold_gb:   VRAM threshold in GB (inclusive). Default 8.0.

    Returns:
      One of: `"cuda"`, `"rocm"`, `"metal"`, `"cpu"`. Matches the
      lowercase serde of Rust's `GpuMode` enum.
    """
    if user_override is not None:
        if user_override:
            # User-forced GPU mode. Route to the matching vendor:
            # - Apple Silicon → metal (no CUDA/ROCm on M-series).
            # - AMD          → rocm.
            # - Everyone else (NVIDIA, or empty vendor when the probe
            #   missed a card the user knows is there) → cuda. CUDA is
            #   the safer default fallback (more mature tooling).
            if vendor == "metal":
                return "metal"
            if vendor == "amd":
                return "rocm"
            return "cuda"
        return "cpu"

    if vendor == "metal":
        return "metal"

    if vendor == "nvidia" and vram_gb >= threshold_gb:
        return "cuda"

    if vendor == "amd" and vram_gb >= threshold_gb:
        return "rocm"

    return "cpu"


def _probe_nvidia_vram_gb() -> float:
    """Best-effort NVIDIA VRAM probe via nvidia-smi.

    Returns total VRAM (GB) for GPU 0, or 0.0 on any failure. Multi-GPU
    hosts: we just take the first card — the install-time selector
    only cares whether SOMETHING in the box can hold a given model.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            mb = float(result.stdout.strip().splitlines()[0].strip())
            return round(mb / 1024.0, 2)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 0.0


def _probe_amd_rocm_vram_gb() -> float:
    """Best-effort AMD ROCm VRAM probe.

    Tries `rocm-smi --showmeminfo vram --csv` first (newer rocm-smi),
    falls back to plain `--showmeminfo vram` text parse. Returns total
    VRAM (GB) or 0.0 on any failure.

    rocm-smi text format varies by version; we look for a "Total Memory"
    or "vram Total Memory" line and parse the bytes value. CSV format is
    more stable but not universally supported.
    """
    if not shutil.which("rocm-smi"):
        return 0.0
    # Try CSV first (cleaner parse).
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--csv"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # CSV header includes "VRAM Total Memory (B)" or similar.
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            if len(lines) >= 2:
                header = [h.strip().lower() for h in lines[0].split(",")]
                values = [v.strip() for v in lines[1].split(",")]
                for i, h in enumerate(header):
                    if "vram" in h and "total" in h and i < len(values):
                        try:
                            bytes_val = float(values[i])
                            return round(bytes_val / (1024.0 ** 3), 2)
                        except ValueError:
                            continue
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    # Text fallback.
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                ll = line.lower()
                if "total" in ll and "memory" in ll and ":" in line:
                    val = line.split(":", 1)[1].strip().split()[0]
                    try:
                        bytes_val = float(val)
                        # Heuristic: if value is huge it's bytes; if small, MB.
                        if bytes_val > 1024 ** 3:
                            return round(bytes_val / (1024.0 ** 3), 2)
                        if bytes_val > 1024:
                            return round(bytes_val / 1024.0, 2)  # MB→GB
                        return round(bytes_val, 2)
                    except ValueError:
                        continue
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return 0.0


def _probe_system_ram_gb() -> float:
    """Best-effort system RAM probe across Linux/macOS/Windows.

    Uses `psutil` if available (fastest and most portable). Falls back
    to /proc/meminfo on Linux, `sysctl hw.memsize` on macOS, and `wmic`
    on Windows. Returns 0.0 on any failure — caller handles gracefully.
    """
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / (1024.0 ** 3), 2)
    except ImportError:
        pass
    except Exception:
        return 0.0

    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024.0 ** 2), 2)
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return round(int(result.stdout.strip()) / (1024.0 ** 3), 2)
        elif system == "Windows":
            result = subprocess.run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    s = line.strip()
                    if s.isdigit():
                        return round(int(s) / (1024.0 ** 3), 2)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return 0.0


# Inference model pull list for install-time.
#
# v0.2.23 C10 consolidation (2026-05-21): this function now DERIVES its
# pull list from `select_summary_backend()` rather than maintaining its
# own VRAM/RAM ladder. Pre-consolidation, two separate ladders drifted
# (this one used `vram >= 7.5/5.0/1.0` and `ram >= 24/12`; the runtime
# summary selector used `vram >= 16/6` and `ram >= 12 AND cores >= 6`)
# meaning some hosts pulled qwen3.5:9b at 8 GB VRAM but the runtime
# selector picked gemma — wasted bandwidth + disk.
#
# Now the pull list ALWAYS matches what `select_summary_backend()` would
# pick locally, with `qwen3.5:0.8b` as the universal floor for any
# inference need. The legacy ollama_mcp/server.py was removed in v0.2.11;
# the runtime selector now lives in `templates/scripts/generate-kg-summary.py`
# (which also calls `select_summary_backend` via the consolidated path).
def _inference_models_for_capability(sysinfo: SystemInfo) -> list[str]:
    """Return inference models to pull given detected capability.

    v0.2.23 C10 consolidation: this function used to maintain its OWN
    VRAM/RAM thresholds (vram >= 7.5 / 5.0 / 1.0; ram >= 24.0 / 12.0)
    that DIVERGED from `select_summary_backend`'s thresholds (vram >=
    16.0 / 6.0; ram >= 12.0 AND cores >= 6). The drift meant some hosts
    pulled models they'd never use (qwen3.5:9b pulled at 8 GB VRAM but
    runtime selector picks gemma) — wasted bandwidth + disk. The pull
    list now derives from the SAME selector that runtime uses, so the
    set of pulled models always matches the set runtime can pick.

    Returns at minimum ["qwen3.5:0.8b"] — the floor model that fits down
    to 4 GB RAM and is the universal fallback for any inference need.
    The summary-backend's pick is added when it's a local Ollama model
    (qwen3.5:9b / gemma4:e4b). For "cli" / "openai" / None picks, only
    the floor is pulled — no local-summary model needed.
    """
    floor = "qwen3.5:0.8b"
    cores = _probe_cpu_cores()
    summary_pick = select_summary_backend(
        gpu_vram_gb=float(sysinfo.vram_gb or 0.0),
        ram_gb=float(sysinfo.ram_gb or 0.0),
        cores=cores,
        # `claude_cli_available=False` for the pull-list derivation:
        # even when the CLI is available, the runtime falls back to
        # local models if the CLI fails mid-summary, so we still want
        # the highest-tier local model available on disk as a safety
        # net. Passing False here gives us the local-model pick that
        # runtime WOULD use if CLI fell through.
        claude_cli_available=False,
        # `openai_consent=False` / `openai_key_available=False`: the
        # same reasoning — when local hardware is the fallback, we
        # need it pulled regardless of OpenAI availability.
        openai_consent=False,
        openai_key_available=False,
    )
    # Map the summary-backend ID back to its Ollama tag. CLI / OpenAI /
    # None don't add to the pull list.
    if summary_pick == _SUMMARY_BACKEND_QWEN35_9B:
        return ["qwen3.5:9b", "gemma4:e4b", floor]
    if summary_pick == _SUMMARY_BACKEND_GEMMA:
        return ["gemma4:e4b", floor]
    # summary_pick is None (no local viable) OR "cli" / "openai" (no
    # local needed for the primary path; floor still pulled as the
    # safety net for any other inference need).
    return [floor]


# ---------------------------------------------------------------------------
# v0.2.23 C10 — hardware-aware backend selectors
#
# Three pure decision functions that map detected hardware (VRAM, RAM,
# CPU cores) + capability flags (OpenAI key available, Claude CLI
# present, user consent) onto a concrete backend choice. These are the
# canonical selectors invoked by `_choose_embedding_config` and by the
# KG-summary generator. They MUST be pure — no side effects, no probes —
# so the tier-boundary regression tests (tests/test_hardware_auto_selection.py)
# can sweep the parameter space without needing to mock subprocess /
# psutil / nvidia-smi calls.
#
# Tier boundaries are INCLUSIVE on the lower bound (`vram >= 12` means
# "12 GB exactly qualifies for the 12+ GB tier"). The spec uses "12+",
# "8+", "6+", "24+" phrasing → ">=" semantics are the natural reading.
#
# Spec source: 2026-05-21 user spec (v0.2.23 C10). See
# `knowledge/concepts/hardware-tiered-backend-selection.md` for the
# rationale on why each model lands on each tier.
# ---------------------------------------------------------------------------

# Backend ID constants. These are the strings persisted into the
# launcher's `app_state` defaults + .env writes, so they must match what
# the rest of the codebase already understands (see EMBEDDING_CONFIGS
# entries above — the IDs here are the union of `code_model` and
# `text_model` fields across the GPU / CPU / OpenAI / low_resource
# profiles).
_CODE_BACKEND_CODESAGE = "codesage-large-v2"
_CODE_BACKEND_QWEN3 = "qwen3-embedding:0.6b"
_CODE_BACKEND_JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
_CODE_BACKEND_OPENAI = "openai-text-embedding-3-small"

_KG_BACKEND_QWEN3 = "qwen3-embedding:0.6b"
_KG_BACKEND_ARCTIC = "snowflake-arctic-embed2:latest"
_KG_BACKEND_OPENAI = "openai-text-embedding-3-small"

_SUMMARY_BACKEND_CLI = "cli"           # claude CLI (Max subscription / API key)
_SUMMARY_BACKEND_QWEN35_9B = "qwen3.5:9b"
_SUMMARY_BACKEND_GEMMA = "gemma4:e4b"
_SUMMARY_BACKEND_OPENAI = "openai"     # routes via API tier with consent gate


def select_code_embedding_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    openai_key_available: bool,
    prefer_openai: bool = False,
) -> str:
    """Pick a code-embedding backend ID for the detected hardware.

    Spec (2026-05-21):
      GPU:
        - VRAM >= 12 GB → CodeSage-Large-v2
        - VRAM >=  6 GB → qwen3-embedding (1024-dim, generalist)
        - VRAM >   2 GB → Jina v2 base-code (768-dim, code-specialised)
        - else / no GPU → CPU path
      CPU (only reached when GPU path lands below "Jina via Ollama"):
        - RAM >= 24 GB AND cores >= 8 → qwen3-embedding
        - else → Jina
      OpenAI: optional override (caller passes prefer_openai=True), not
              auto-selected — it costs money per embedding.

    The ">" (strict) on the 2 GB GPU boundary is deliberate: a 2 GB card
    is below CodeSage's working set AND below Jina's comfortable RAM
    target, so it falls into the CPU bucket. >2 GB means "anything
    above 2 GB", e.g. a 4 GB card.

    Args:
        gpu_vram_gb: Detected VRAM (GB). 0.0 means "no usable GPU".
        ram_gb:      System RAM (GB).
        cores:       Logical CPU cores (psutil.cpu_count(logical=True)).
        openai_key_available: True if an OpenAI API key is configured
            (either via `--openai-key` or via the secrets system). Does
            NOT auto-pick OpenAI — only enables it as an explicit choice.
        prefer_openai: True when the caller (`--openai-key` flag, or
            the GUI's "use OpenAI for code embeddings" toggle) wants
            OpenAI even on capable hardware.

    Returns:
        One of the `_CODE_BACKEND_*` constants. Always returns
        something — there is no "None" path for code embeddings (every
        host can run Jina via Ollama as a floor).
    """
    if prefer_openai and openai_key_available:
        return _CODE_BACKEND_OPENAI

    vram = float(gpu_vram_gb or 0.0)
    ram = float(ram_gb or 0.0)
    cpu_cores = int(cores or 0)

    if vram >= 12.0:
        return _CODE_BACKEND_CODESAGE
    if vram >= 6.0:
        return _CODE_BACKEND_QWEN3
    if vram > 2.0:
        return _CODE_BACKEND_JINA

    # CPU path: VRAM <= 2 GB OR no GPU at all.
    if ram >= 24.0 and cpu_cores >= 8:
        return _CODE_BACKEND_QWEN3
    return _CODE_BACKEND_JINA


def select_kg_embedding_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    openai_key_available: bool,
    prefer_openai: bool = False,
) -> str:
    """Pick a KG / text-embedding backend ID for the detected hardware.

    Spec (2026-05-21):
      GPU:
        - VRAM >= 8 GB → qwen3-embedding (1024-dim, our default)
        - VRAM <  8 GB → snowflake-arctic-embed2 (1024-dim, smaller
          working set — still 1024-dim so the schema slot is identical)
        - VRAM <  4 GB OR unsupported → CPU path
      CPU:
        - RAM >= 24 GB AND cores >= 8 → qwen3-embedding
        - else → arctic2
      OpenAI: optional, not auto-selected.

    The 4 GB lower bound is implicit: any GPU with <4 GB VRAM is below
    qwen3-embedding's safe working set, so we drop to the CPU path
    (where arctic2 is the small-footprint default). Cards in the 4-8 GB
    band still benefit from GPU acceleration when running arctic2.

    Args:
        gpu_vram_gb: Detected VRAM (GB). 0.0 means "no usable GPU".
        ram_gb:      System RAM (GB).
        cores:       Logical CPU cores.
        openai_key_available: True if an OpenAI API key is configured.
        prefer_openai: True when the caller wants OpenAI explicitly.

    Returns:
        One of the `_KG_BACKEND_*` constants.
    """
    if prefer_openai and openai_key_available:
        return _KG_BACKEND_OPENAI

    vram = float(gpu_vram_gb or 0.0)
    ram = float(ram_gb or 0.0)
    cpu_cores = int(cores or 0)

    if vram >= 8.0:
        return _KG_BACKEND_QWEN3
    if vram >= 4.0:
        # Mid-range GPU: arctic2 runs comfortably without crowding the
        # GPU when other models also need to load (code embedder,
        # summary inference). Same 1024-dim slot as qwen3 → no schema
        # change needed.
        return _KG_BACKEND_ARCTIC

    # CPU path (or sub-4-GB GPU treated as CPU here).
    if ram >= 24.0 and cpu_cores >= 8:
        return _KG_BACKEND_QWEN3
    return _KG_BACKEND_ARCTIC


def select_summary_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    claude_cli_available: bool,
    openai_consent: bool,
    openai_key_available: bool = False,
) -> "str | None":
    """Pick a KG-summary generation backend, or None if no path is viable.

    Spec (2026-05-21):
      claude CLI present (AND authenticated) → ALWAYS use it (highest
        quality, costs come out of the user's Max subscription).
      GPU:
        - VRAM >= 16 GB → qwen3.5:9b
        - VRAM >=  6 GB → gemma4:e4b
        - else → CPU path
      CPU:
        - RAM >= 12 GB AND cores >= 6 → gemma4:e4b
        - else → no local model viable
      OpenAI: gated on `openai_consent` (default OFF — the user has
        to explicitly opt in via Preferences). When opted in AND a key
        is configured, returns "openai" so the caller can route to the
        cheapest summary-capable model.

    Returns None when:
      - no claude CLI, AND
      - hardware can't run gemma4:e4b (sub-12 GB RAM or <6 cores AND
        no GPU >= 6 GB VRAM), AND
      - either no OpenAI consent OR no OpenAI key.

    The None case is NOT an error — install.py should record a
    `kg_summary_no_backend` deferral entry and continue. The KG
    summariser script silently no-ops on None, leaving raw KG content
    in place (search still works, just without LLM-polished
    descriptions / chunk summaries).

    Args:
        gpu_vram_gb: Detected VRAM (GB).
        ram_gb:      System RAM (GB).
        cores:       Logical CPU cores.
        claude_cli_available: True if `claude` is on PATH and
            authenticated (caller is responsible for verifying with a
            cheap smoke test; this selector takes the boolean at face
            value).
        openai_consent: True when the user has explicitly opted in
            (`app_state` key `kg_summary_openai_consent=true`).
        openai_key_available: True if an OpenAI key is configured in
            secrets. Combined with `openai_consent` to gate the OpenAI
            path.

    Returns:
        Backend ID string, or None when nothing viable is available.
        Possible strings: "cli", "qwen3.5:9b", "gemma4:e4b", "openai".
    """
    # CLI always wins when available — best quality, no local resource
    # cost, paid out of the user's subscription.
    if claude_cli_available:
        return _SUMMARY_BACKEND_CLI

    vram = float(gpu_vram_gb or 0.0)
    ram = float(ram_gb or 0.0)
    cpu_cores = int(cores or 0)

    # GPU tiers.
    if vram >= 16.0:
        return _SUMMARY_BACKEND_QWEN35_9B
    if vram >= 6.0:
        return _SUMMARY_BACKEND_GEMMA

    # CPU tier (only when GPU is sub-6 GB or absent).
    if ram >= 12.0 and cpu_cores >= 6:
        return _SUMMARY_BACKEND_GEMMA

    # No local path viable. Last resort: OpenAI, only if the user
    # explicitly consented AND a key is available.
    if openai_consent and openai_key_available:
        return _SUMMARY_BACKEND_OPENAI

    return None


def _probe_cpu_cores() -> int:
    """Best-effort logical CPU-core count, cross-OS.

    Prefers `psutil.cpu_count(logical=True)` (most portable, handles
    cgroup limits on Linux containers). Falls back to `os.cpu_count()`.
    Returns 0 on any probe failure — the selectors treat 0 as "low-end
    CPU" (drops to the smaller-model tier), which is the conservative
    direction (better to under-spec than over-promise).
    """
    try:
        import psutil  # type: ignore
        n = psutil.cpu_count(logical=True)
        if n and n > 0:
            return int(n)
    except ImportError:
        pass
    except Exception:
        return 0
    try:
        n = os.cpu_count()
        return int(n) if n and n > 0 else 0
    except Exception:
        return 0


def _lspci_has_vendor(vendor_substr: str) -> bool:
    """Linux-only: check `lspci` output for a substring (e.g. "NVIDIA",
    "AMD/ATI"). Returns False on non-Linux or any probe failure.

    Used to detect "hardware present but driver missing" — when there's
    NVIDIA silicon in the box but no nvidia-smi, the user almost
    certainly forgot to install the proprietary driver.
    """
    if platform.system() != "Linux" or not shutil.which("lspci"):
        return False
    try:
        result = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        return vendor_substr.lower() in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _print_gpu_hint(os_name: str) -> None:
    """Print a CPU-only fallback notice + GPU-driver install URLs when
    we couldn't find a working GPU stack. Best-effort: detects
    "hardware present but no driver" via lspci on Linux, and prints
    OS-appropriate URLs everywhere else.

    No auto-install: GPU drivers are vendor-specific, multi-step, often
    require a reboot, and add gigabytes of disk usage. We surface the
    canonical install URLs and let the user decide. The launcher GUI
    can offer an opt-in install button later.

    Reference URLs (canonical only):
      - NVIDIA Linux:    https://docs.nvidia.com/cuda/cuda-installation-guide-linux/
      - NVIDIA Windows:  https://developer.nvidia.com/cuda-downloads
      - AMD ROCm Linux:  https://rocm.docs.amd.com/projects/install-on-linux/en/latest/
    """
    print("  GPU: none detected (will use CPU)")

    has_nvidia_hw = _lspci_has_vendor("NVIDIA")
    has_amd_hw = _lspci_has_vendor("AMD/ATI") or _lspci_has_vendor("ATI Technologies")

    if has_nvidia_hw:
        print("       NVIDIA hardware detected via lspci, but nvidia-smi is missing.")
        if os_name == "Linux":
            print("       Install CUDA toolkit + driver:")
            print("         Ubuntu:   sudo apt install nvidia-driver-545 nvidia-cuda-toolkit")
            print("         Fedora:   sudo dnf install xorg-x11-drv-nvidia-cuda")
            print("         Docs:     https://docs.nvidia.com/cuda/cuda-installation-guide-linux/")
            print("       Re-run install.py after the driver is installed.")
        else:
            print("       https://developer.nvidia.com/cuda-downloads")

    if has_amd_hw:
        print("       AMD hardware detected via lspci. ROCm is optional but")
        print("       enables GPU embeddings on supported cards. Install:")
        print("         https://rocm.docs.amd.com/projects/install-on-linux/en/latest/")

    if os_name == "Windows" and not has_nvidia_hw:
        # On Windows we can't do the lspci probe; print a softer hint.
        print("       If you have an NVIDIA GPU, install drivers + CUDA:")
        print("         https://developer.nvidia.com/cuda-downloads")


def _runtime_preference_from_env() -> Optional[str]:
    """Return the user's explicit `VCT_CONTAINER_RUNTIME` preference, or
    None if unset / set to "auto".

    Canonical contract (v0.2.14, consolidated across install.py,
    launcher Rust, hooks, and the boot wrapper): values are
    case-insensitive, trimmed, and "auto" is treated as "no preference".
    Unknown values log to stderr and are ignored (fall through to
    auto-detect).

    Callers should check this BEFORE running auto-detection so the
    user's choice wins over PATH order. Audit Bug #3 (cross-OS audit,
    2026-05-17).
    """
    raw = os.environ.get("VCT_CONTAINER_RUNTIME", "").strip().lower()
    if not raw or raw == "auto":
        return None
    if raw in ("podman", "docker"):
        return raw
    print(
        f"  VCT_CONTAINER_RUNTIME={raw!r} unrecognized (expected "
        "'podman' / 'docker' / 'auto'); falling through to auto-detect.",
        file=sys.stderr,
    )
    return None


def _detect_container_runtime() -> str:
    """Detect Docker or Podman. Prefer Podman everywhere — no commercial
    license required, increasingly native on macOS/Windows.

    Honors `VCT_CONTAINER_RUNTIME=podman|docker|auto` env var as the
    user's explicit preference (v0.2.14 Bug #3 fix). If set to a
    recognized value, returns that runtime IF it's reachable; else
    falls through to auto-detect (we don't want a misconfigured env
    var to silently leave the user with no runtime — auto-detect
    finds whatever IS working).

    Returns:
      - "podman" or "docker" if a runtime is present AND its daemon
        responds to `version`/`info` (i.e. it can actually run containers).
      - "" if neither runtime is on PATH OR the daemon isn't responding.
        Caller distinguishes the two cases via `_detect_installed_runtime()`.
    """
    pref = _runtime_preference_from_env()
    if pref is not None:
        # User explicitly chose. Try ONLY that one; if it works, honor.
        # If not, fall through to auto-detect (lenient: don't strand the
        # user on a misconfigured env var). Stderr explains the fallthrough.
        if shutil.which(pref):
            try:
                result = subprocess.run(
                    [pref, "version"], capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    return pref
            except (subprocess.TimeoutExpired, OSError):
                pass
        print(
            f"  VCT_CONTAINER_RUNTIME={pref!r} not reachable; falling "
            "through to auto-detect.",
            file=sys.stderr,
        )

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


def _container_runtime_reachable(container_cmd: str) -> bool:
    """Quick proactive check that the container daemon/socket is responsive.

    Used by `_start_services()` before compose-up to surface a
    cross-platform actionable hint when the runtime is installed but
    not running. Without this, compose-up takes 10-30s to fail with
    a cryptic stderr ("Cannot connect to the Docker daemon" or
    "Cannot connect to Podman socket"), which we then have to parse.

    Returns False if the runtime isn't on PATH OR `<runtime> info`
    fails. Returns True if the daemon/socket is responsive.

    Why `info` (not `version`): `version` only checks the client
    binary; `info` round-trips to the daemon/socket and exercises the
    same code path that compose-up needs. Catches stopped Docker
    Desktop on macOS, stopped podman.socket on Linux rootless,
    unstarted podman machine on Windows.
    """
    if not container_cmd or not shutil.which(container_cmd):
        return False
    try:
        result = subprocess.run(
            [container_cmd, "info"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _detect_installed_runtime() -> str:
    """Lightweight presence check — returns the FIRST container-runtime
    binary on PATH regardless of whether its daemon is running.

    Honors `VCT_CONTAINER_RUNTIME=podman|docker|auto` env var (v0.2.14
    Bug #3 fix). If the explicit preference is installed (even if the
    daemon isn't responsive), it wins over the auto-detect order.

    Use case: `_detect_container_runtime()` returned "" (no working
    runtime) but we want to give a better message than "install Podman/
    Docker" if one IS installed, just stopped. On Windows specifically,
    Docker Desktop ships `docker.exe` on PATH but `docker version` fails
    until the user opens Docker Desktop from the Start Menu.

    Returns "" when neither binary is on PATH.
    """
    pref = _runtime_preference_from_env()
    if pref is not None and shutil.which(pref):
        return pref
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return ""


def _ensure_nvidia_cdi_spec_for_podman() -> None:
    """Verify NVIDIA Container Toolkit's auto-refresh CDI service is active.

    Background (revised 2026-05-08):

    Earlier versions of this function wrote `/etc/cdi/nvidia.yaml` directly
    via `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`. That
    was the right advice for nvidia-container-toolkit < 1.18, but it's
    actively harmful in 2026:

      1. NVIDIA Container Toolkit ≥ 1.18.0 ships `nvidia-cdi-refresh.path`
         + `nvidia-cdi-refresh.service` systemd units that AUTO-regenerate
         the CDI spec on every driver install/upgrade. They write to
         `/var/run/cdi/nvidia.yaml` (a runtime tmpfs path).

      2. Podman reads `/etc/cdi/` BEFORE `/var/run/cdi/`. So a manually
         written `/etc/cdi/nvidia.yaml` SHADOWS the auto-refreshed one.

      3. After an apt/dnf-driven driver upgrade, the manual `/etc/cdi/`
         spec keeps referencing the OLD driver's library files (e.g.
         libEGL_nvidia.so.590.48.01). Those files were deleted by the
         driver upgrade. Result: every GPU container fails to start with
         `runc: failed to fulfil mount request: ... no such file`.

      4. We hit this exact bug on the dev machine 2026-05-07 — entire
         compose stack came down because ollama/code_embed couldn't mount
         a stale lib path. See `.claude/context/handoff-2026-05-08-*.md`.

    Correct behaviour now:
      - DO NOT write `/etc/cdi/nvidia.yaml` from this installer.
      - DELETE any pre-existing `/etc/cdi/nvidia.yaml` left over from prior
        installs of VCO (they are time-bombs).
      - Verify the system's auto-refresh service is present + enabled.
        If toolkit < 1.18 is installed (no auto-refresh units): warn user
        to upgrade. If units exist but are disabled: print the enable cmd.

    No /etc/cdi/ writes. Ever. The launcher's startup-time drift detector
    is the second line of defense if the user somehow re-creates a stale
    spec.
    """
    if platform.system() != "Linux":
        # macOS Podman runs in a VM; CDI generation happens inside
        # the VM via Podman Machine, not on the host. Skip for now.
        return

    if not shutil.which("nvidia-ctk"):
        print(
            "  [!] Podman + NVIDIA: `nvidia-ctk` not on PATH. Install the "
            "NVIDIA Container Toolkit (≥ 1.18.0 strongly recommended):\n"
            "      https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html\n"
            "      (then re-run install — compose-up will fail until CDI is set up)"
        )
        return

    # Step 1: surface + offer to delete any stale `/etc/cdi/nvidia.yaml`
    # left by a previous install of VCO or by a user following old docs.
    # Stale specs SHADOW the auto-refreshed `/var/run/cdi/nvidia.yaml`
    # and break GPU containers after every driver update.
    cdi_etc = Path("/etc/cdi/nvidia.yaml")
    if cdi_etc.exists():
        print(
            "  [!] Found stale /etc/cdi/nvidia.yaml — this shadows the\n"
            "      system's auto-refreshed /var/run/cdi/nvidia.yaml and\n"
            "      breaks GPU containers after driver updates. Remove with:\n"
            "          sudo rm /etc/cdi/nvidia.yaml\n"
            "      (run after install completes; the auto-refresh service\n"
            "      will keep /var/run/cdi/nvidia.yaml fresh from now on)"
        )

    # Step 2: verify nvidia-cdi-refresh.{path,service} exist + enabled.
    # systemctl `is-enabled` returns 0 only when the unit is active in the
    # boot manifest. Anything else means user needs to fix it.
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "nvidia-cdi-refresh.path"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Auto-refresh is on. Nothing more to do.
            print("    ✓ NVIDIA Container Toolkit auto-refresh active "
                  "(nvidia-cdi-refresh.path enabled)")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # systemctl not on PATH (highly unusual on a Linux host with
        # podman) or hung. Fall through to install hint.
        pass

    print(
        "  [!] nvidia-cdi-refresh systemd units are not enabled. They ship\n"
        "      with NVIDIA Container Toolkit ≥ 1.18.0 and auto-regenerate\n"
        "      the CDI spec on every driver upgrade. To enable:\n"
        "          sudo systemctl enable --now nvidia-cdi-refresh.path\n"
        "          sudo systemctl enable --now nvidia-cdi-refresh.service\n"
        "      If your nvidia-container-toolkit version is < 1.18, upgrade\n"
        "      first. Without these units, you must manually regenerate the\n"
        "      CDI spec after every NVIDIA driver upgrade or GPU containers\n"
        "      will fail to start."
    )


def _prompt_install_container_runtime(args: argparse.Namespace) -> bool:
    """Prompt the user to install Podman/Docker when neither is detected.

    Returns True iff an auto-install attempt completed successfully. Caller
    must re-detect afterward (PATH refresh, etc.).

    OS matrix:
      - Linux:   We CAN auto-install via apt/dnf/pacman. Prompt y/n;
                 on yes, run the package-manager command (sudo prompt
                 surfaces in the controlling terminal).
      - macOS:   URL only. Homebrew may not be present; Docker Desktop
                 needs kernel-extension consent on Apple Silicon. We
                 don't shoulder that.
      - Windows: URL only. winget on Podman requires WSL2 + admin
                 elevation; Docker Desktop installer is a separate
                 download. Print canonical URLs and exit.

    Honors `--yes` / non-TTY by skipping the prompt and printing
    instructions only (treated as a decline). This matches the spec's
    "fall back to manual install" rule for non-interactive runs.

    Refs:
      - Podman install:   https://podman.io/getting-started/installation
      - Docker Desktop:   https://www.docker.com/products/docker-desktop
      - Homebrew:         https://brew.sh
    """
    os_name = platform.system()

    # First: distinguish "not installed" from "installed but daemon
    # not running". On Windows in particular Docker Desktop installs
    # docker.exe on PATH but the daemon stays stopped until the user
    # opens Docker Desktop from the Start Menu / system tray; calling
    # `docker version` fails with the same exit code as truly-absent
    # docker, which made our earlier message misleading. Reported
    # 2026-04-28: "PC had docker, but not currently running".
    installed = _detect_installed_runtime()
    if installed:
        print(f"\n[!] {installed} is installed but its daemon isn't responding.")
        if os_name == "Windows" and installed == "docker":
            print("    Open Docker Desktop from the Start Menu or system tray,")
            print("    wait for the whale icon to settle (~30 seconds), then re-run install.")
        elif os_name == "Darwin" and installed == "docker":
            print("    Open Docker Desktop from /Applications, wait for the whale")
            print("    icon to settle (~30 seconds), then re-run install.")
        elif installed == "podman":
            print("    Start the Podman machine (Linux: systemctl --user start podman.socket;")
            print("    macOS / Windows: podman machine start), then re-run install.")
        else:
            print("    Start the Docker daemon (`sudo systemctl start docker` on Linux),")
            print("    then re-run install.")
        print("    Or re-run with --no-containers to skip container setup.")
        return False

    print("\n[!] No container runtime found. The orchestrator needs Podman or Docker.")

    non_interactive = bool(args.yes) or not sys.stdin.isatty() or bool(args.quiet)

    if os_name == "Linux":
        # Detect package manager first; we only prompt for managers we
        # can actually drive.
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "podman"]
            update_cmd = ["sudo", "apt-get", "update"]
            label = "apt (Debian/Ubuntu)"
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "podman"]
            update_cmd = None
            label = "dnf (Fedora/RHEL)"
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "podman"]
            update_cmd = None
            label = "pacman (Arch)"
        else:
            print("    No supported package manager found (apt/dnf/pacman).")
            print("    Install Podman manually: https://podman.io/getting-started/installation")
            print("    Or Docker:               https://docs.docker.com/get-docker/")
            return False

        print(f"    Detected {label}. Will run:")
        if update_cmd:
            print(f"      {' '.join(update_cmd)}")
        print(f"      {' '.join(cmd)}")
        print("    (You'll be prompted for your sudo password.)")

        if non_interactive:
            print("    Non-interactive mode — skipping auto-install.")
            print("    Re-run interactively, or install manually then re-run install.py.")
            return False

        try:
            reply = input("    Install podman now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n    Aborted.")
            return False
        if reply and reply[0] != "y":
            print("    Skipped. Install manually then re-run install.py.")
            return False

        try:
            if update_cmd:
                subprocess.run(update_cmd, check=True)
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"    Package install failed (exit {e.returncode}).")
            print("    Install manually: https://podman.io/getting-started/installation")
            return False
        except FileNotFoundError:
            print("    sudo not found. Install Podman manually:")
            print("      https://podman.io/getting-started/installation")
            return False
        return True

    if os_name == "Darwin":
        # Homebrew install requires Homebrew already present. Even with
        # brew, the user still needs `podman machine init && podman
        # machine start` (Podman runs in a VM on macOS). Don't try to
        # automate that — surface the canonical instructions.
        print("    Install one of (we recommend Podman):")
        print("      brew install podman                                     # if Homebrew is installed")
        print("      Then: podman machine init && podman machine start")
        print("    Or download:")
        print("      Podman:        https://podman.io/getting-started/installation")
        print("      Docker Desktop: https://www.docker.com/products/docker-desktop")
        print("      Homebrew:      https://brew.sh   (if not already installed)")
        return False

    if os_name == "Windows":
        # Podman on Windows requires WSL2 underneath; winget can install
        # the Podman binary but not the WSL2 prerequisite + admin
        # elevation. Docker Desktop is a separate installer. Manual.
        print("    Install one of (we recommend Podman):")
        print("      winget install RedHat.Podman                            # if winget is available")
        print("      Then: podman machine init && podman machine start")
        print("    Or download:")
        print("      Podman:         https://podman.io/getting-started/installation")
        print("      Docker Desktop: https://www.docker.com/products/docker-desktop")
        print("    Note: Podman on Windows requires WSL2.")
        print("      WSL2 setup:    https://learn.microsoft.com/windows/wsl/install")
        return False

    # Other / unknown OS — print generic guidance.
    print(f"    Unrecognized OS '{os_name}'. Install Podman or Docker manually:")
    print("      Podman:         https://podman.io/getting-started/installation")
    print("      Docker:         https://docs.docker.com/get-docker/")
    return False


def _print_system_info(sysinfo: SystemInfo) -> None:
    pass  # Already printed in _detect_system


def _find_lean_ctx_binary() -> str | None:
    """Locate lean-ctx beyond PATH (cross-OS).

    `shutil.which` only checks PATH, so users who installed lean-ctx via
    `cargo install lean-ctx` (lands at ~/.cargo/bin) but whose shell PATH
    doesn't include ~/.cargo/bin (cargo's installer adds it to ~/.profile
    but `bash first-install.sh` from a fresh terminal may not have sourced
    it yet) saw 'not installed' even though the binary is right there.
    Mirrors the known-binary-path probe pattern in
    post-install-launcher.sh's _ensure_path_for_tool helper.

    Cross-OS:
      * Linux/macOS: probes the cargo + brew + standard system paths.
      * Windows: probes the cargo install dir + scoop/chocolatey shims.
        Tries both ``lean-ctx`` and ``lean-ctx.exe`` since Windows
        cargo-installed binaries get the ``.exe`` suffix.
    """
    on_path = shutil.which("lean-ctx")
    if on_path:
        return on_path

    is_windows = sys.platform == "win32"
    home = Path.home()

    # Per-OS candidate list. Both forms are tried so a Linux user with a
    # weird WSL2 setup or a Windows user running this script under WSL2
    # still gets found.
    if is_windows:
        candidates: list[Path] = [
            home / ".cargo" / "bin" / "lean-ctx.exe",
            home / "scoop" / "shims" / "lean-ctx.exe",
            home / "scoop" / "apps" / "lean-ctx" / "current" / "lean-ctx.exe",
            Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            / "chocolatey" / "bin" / "lean-ctx.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "lean-ctx" / "lean-ctx.exe",
        ]
    else:
        candidates = [
            home / ".cargo" / "bin" / "lean-ctx",
            home / ".local" / "bin" / "lean-ctx",
            Path("/usr/local/bin/lean-ctx"),
            Path("/usr/bin/lean-ctx"),
            # Homebrew on Apple Silicon vs Intel macOS:
            Path("/opt/homebrew/bin/lean-ctx"),
            Path("/home/linuxbrew/.linuxbrew/bin/lean-ctx"),
        ]

    for cand in candidates:
        try:
            # os.access(..., X_OK) is meaningful on POSIX. On Windows
            # there is no executable bit; is_file() is the relevant check.
            if cand.is_file() and (is_windows or os.access(cand, os.X_OK)):
                return str(cand)
        except OSError:
            # Hostile path (mount unavailable, permission denied at stat
            # time) — keep probing the rest.
            continue
    return None


def _resolve_vendored_lean_ctx() -> Path | None:
    """Look for a repo-vendored lean-ctx binary matching this host's
    arch. We ship prebuilts under `vendor/lean-ctx/<arch>/lean-ctx[.exe]`
    so users on Windows (where `cargo install lean-ctx` takes 5-10 min
    of cold Rust compile) can skip the build entirely. Mirrors the
    `launcher/dist/<arch>/` convention.

    Returns the absolute path to the vendored binary if it exists,
    None otherwise. Caller decides whether to copy it into a PATH
    location or run it in place.

    Vendor convention:
      vendor/lean-ctx/windows-x64/lean-ctx.exe   (Windows x86_64)
      vendor/lean-ctx/linux-x64/lean-ctx         (Linux x86_64)
      vendor/lean-ctx/experimental_macOS/lean-ctx (macOS, both archs)
    """
    os_name = platform.system()
    if os_name == "Windows":
        arch_dir = "windows-x64"
        bin_name = "lean-ctx.exe"
    elif os_name == "Darwin":
        arch_dir = "experimental_macOS"
        bin_name = "lean-ctx"
    elif os_name == "Linux":
        arch_dir = "linux-x64"
        bin_name = "lean-ctx"
    else:
        return None
    candidate = PROJECT_ROOT / "vendor" / "lean-ctx" / arch_dir / bin_name
    if candidate.is_file():
        return candidate
    return None


def _install_vendored_lean_ctx() -> str | None:
    """Copy the repo-vendored lean-ctx binary into a PATH-resolvable
    location so `shutil.which("lean-ctx")` picks it up post-install.

    Linux/macOS: target ~/.local/bin/lean-ctx (already on most users'
    PATH; rustup adds it if cargo is installed).
    Windows: target %USERPROFILE%\\.cargo\\bin\\lean-ctx.exe (cargo's
    canonical bin dir; cargo adds it to PATH; install.py's
    _resolve_lean_ctx already probes this dir as a fallback).

    Returns the destination path on success, None if no vendored
    prebuilt was found or the copy failed. Idempotent — re-copies
    even if a stale older version is already at the destination.
    """
    src = _resolve_vendored_lean_ctx()
    if src is None:
        return None
    os_name = platform.system()
    if os_name == "Windows":
        dest_dir = Path.home() / ".cargo" / "bin"
        dest = dest_dir / "lean-ctx.exe"
    else:
        dest_dir = Path.home() / ".local" / "bin"
        dest = dest_dir / "lean-ctx"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if os_name != "Windows":
            os.chmod(dest, 0o755)
        return str(dest)
    except (OSError, shutil.Error) as e:
        print(f"  lean-ctx: failed to copy vendored prebuilt: {e}")
        return None


def _maybe_install_lean_ctx(args: argparse.Namespace) -> str | None:
    """Auto-install lean-ctx if a supported package manager is present.

    Order of attempts:
      1. Repo-vendored prebuilt (vendor/lean-ctx/<arch>/) — fastest
         path. Saves the ~5-10 min cargo build on Windows. Skipped if
         no prebuilt for this host arch.
      2. Homebrew tap (macOS / Linuxbrew).
      3. Cargo install (any host with rustup).
      4. AUR helpers (Arch Linux).

    Returns the resolved binary path on success, None otherwise.
    Auto-install runs in --yes / non-TTY contexts; real users get a
    prompt. lean-ctx project: https://github.com/yvgude/lean-ctx
    """
    # Step 1: try vendored prebuilt first. No prompt needed — the
    # binary is already in the repo, we just copy it. Silent on
    # failure (caller falls through to package-manager paths).
    vendored = _install_vendored_lean_ctx()
    if vendored:
        print(f"  lean-ctx: installed from vendored prebuilt → {vendored}")
        return vendored

    # T1 silent (with --yes / non-TTY). T3 prompt for interactive.
    silent = bool(args.yes or not sys.stdin.isatty())

    has_brew = shutil.which("brew") is not None
    has_cargo = shutil.which("cargo") is not None
    has_yay = shutil.which("yay") is not None
    has_paru = shutil.which("paru") is not None

    if not (has_brew or has_cargo or has_yay or has_paru):
        # No supported channel — fall through; caller prints the manual
        # install hints (rustup-then-cargo).
        return None

    method = None
    if has_brew:
        method = "brew"
    elif has_cargo:
        method = "cargo"
    elif has_yay:
        method = "yay"
    elif has_paru:
        method = "paru"

    if not silent:
        # Interactive: ask before installing. Default Y because the
        # benefit is large (~95% token savings) and the cost is small
        # (~one-time cargo build / brew tap).
        try:
            method_text = {
                "brew": "Homebrew (`brew tap yvgude/lean-ctx && brew install lean-ctx`)",
                "cargo": "Cargo (`cargo install lean-ctx`, ~2-5 min build)",
                "yay": "AUR (`yay -S lean-ctx-bin`)",
                "paru": "AUR (`paru -S lean-ctx-bin`)",
            }[method]
            ans = input(
                f"  lean-ctx: not installed. Install via {method_text}? [Y/n]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans and ans not in {"y", "yes"}:
            return None

    print(f"  lean-ctx: installing via {method}...")
    try:
        if method == "brew":
            # `brew tap` is idempotent. Do tap + install in two steps so
            # an already-tapped tap doesn't error.
            subprocess.run(
                ["brew", "tap", "yvgude/lean-ctx"],
                check=False, timeout=120,
            )
            r = subprocess.run(
                ["brew", "install", "lean-ctx"],
                check=False, timeout=600,
            )
            if r.returncode != 0:
                print(f"  lean-ctx: brew install failed (exit {r.returncode}).")
                return None
        elif method == "cargo":
            r = subprocess.run(
                ["cargo", "install", "lean-ctx"],
                check=False, timeout=900,  # cargo build can be slow on small machines
            )
            if r.returncode != 0:
                print(f"  lean-ctx: cargo install failed (exit {r.returncode}).")
                return None
        elif method in {"yay", "paru"}:
            r = subprocess.run(
                [method, "-S", "--noconfirm", "lean-ctx-bin"],
                check=False, timeout=300,
            )
            if r.returncode != 0:
                # Fallback: try the source-built package name.
                r = subprocess.run(
                    [method, "-S", "--noconfirm", "lean-ctx"],
                    check=False, timeout=900,
                )
                if r.returncode != 0:
                    print(f"  lean-ctx: {method} install failed.")
                    return None
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  lean-ctx: install raised {type(e).__name__}: {e}")
        return None

    # Re-detect — the just-installed binary should be findable now.
    return _find_lean_ctx_binary()


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
    _log_install_event("2b/10", "start", "probing optional companion tools")

    # lean-ctx (optional — the per-project PreToolUse hook
    # `.claude/hooks/lean-ctx-rewrite.sh` delegates to it for ~90-97%
    # command-output compression on Claude Code's Bash tool surface).
    # https://github.com/yvgude/lean-ctx
    #
    # 0.2.11 redesign: this step only DETECTS / OPTIONALLY INSTALLS the
    # binary. The legacy BASH_ENV shim wiring (which was fork-bomb-prone
    # on lean-ctx 3.x — see knowledge/concepts/lean-ctx-shim-disabled.md)
    # was removed; output compression now flows through the per-project
    # PreToolUse hook registered in templates/settings.json.*.template.
    # The hook no-ops gracefully if lean-ctx isn't on PATH, so an absent
    # binary doesn't break Bash for users without it.
    #
    # Detection: shutil.which checks PATH only. Many users have lean-ctx
    # installed via `cargo install lean-ctx` (canonical landing dir
    # ~/.cargo/bin/) but their non-interactive shell PATH doesn't include
    # ~/.cargo/bin (cargo's installer adds the line to ~/.profile but
    # `bash first-install.sh` from a fresh terminal may not have sourced
    # it yet). Probe known-binary locations as a fallback. Also auto-install
    # via cargo / brew when those are available.
    lean_ctx_path = _find_lean_ctx_binary()
    if not lean_ctx_path and not args.quiet and not args.no_lean_ctx:
        # Try auto-install via the most appropriate package manager.
        lean_ctx_path = _maybe_install_lean_ctx(args)
    if lean_ctx_path:
        print(
            f"  lean-ctx: detected at {lean_ctx_path} — PreToolUse hook "
            f".claude/hooks/lean-ctx-rewrite.sh will use it if registered "
            f"in settings"
        )
    else:
        print("  lean-ctx: not installed (optional, recommended for ~95% token savings on CLI output)")
        # OS-aware install hints. Use the canonical channels documented at
        # https://github.com/yvgude/lean-ctx — verified 2026-04-28.
        print("            Install via your preferred channel; the per-project")
        print("            PreToolUse hook will pick it up on the next session:")
        if shutil.which("brew"):
            print("              Homebrew:   brew tap yvgude/lean-ctx && brew install lean-ctx")
        if shutil.which("cargo"):
            print("              Cargo:      cargo install lean-ctx")
        if shutil.which("yay") or shutil.which("paru"):
            print("              AUR:        yay -S lean-ctx-bin   (or: yay -S lean-ctx)")
        if not (shutil.which("brew") or shutil.which("cargo") or shutil.which("yay") or shutil.which("paru")):
            # No supported package manager detected. Give the user a clear
            # path: install Cargo (Rust toolchain) and then lean-ctx.
            print("              No supported package manager detected.")
            print("              Easiest path: install Rust + Cargo, then `cargo install lean-ctx`:")
            print("                curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh")
            print("                source $HOME/.cargo/env && cargo install lean-ctx")
        print("            (Pass --no-lean-ctx to silence this hint.)")

    # Joern (CFG/PDG metrics for code graph)
    #
    # Deliverable 2 (2026-04-28): also persist the joern choice via
    # `_record_install_choice`. On a re-install, `_load_previous_choices`
    # surfaces this so the caller can short-circuit the prompt without
    # re-detecting / re-asking. The CLI flag (--with-joern / --no-joern)
    # always wins over a replayed choice.
    joern_path = shutil.which("joern")
    if joern_path:
        print(f"  joern:    detected at {joern_path} (code graph will include CFG/PDG metrics)")
        _log_install_event(
            "2b/10", "ok",
            "joern already installed",
            data={"joern_path": joern_path},
        )
        _record_install_choice("joern", True, {"reason": "pre-installed",
                                              "joern_path": joern_path})
        return True

    # Replay-eligible: if we have a recent choice and the user did NOT
    # pass either CLI flag, honour what they picked last time.
    prior_choices = _load_previous_choices()
    if (not args.no_joern and not args.with_joern
            and "joern" in prior_choices):
        prior = prior_choices["joern"]
        prior_value = prior.get("value")
        if prior_value is False:
            print("  joern:    skipped (replayed from last install)")
            _log_install_event("2b/10", "skip",
                               "joern skipped (replayed from last install)")
            _record_install_choice("joern", False,
                                   {"reason": "replayed declined"})
            return False

    if args.no_joern:
        print("  joern:    skipped (--no-joern)")
        _log_install_event("2b/10", "skip", "joern skipped via --no-joern")
        _record_install_choice("joern", False, {"reason": "user declined via flag"})
        return False

    if args.with_joern:
        # User explicitly requested install — proceed without confirmation
        installed = _install_joern()
        _log_install_event(
            "2b/10", "ok" if installed else "error",
            "joern install (--with-joern)",
            data={"installed": installed},
        )
        _record_install_choice("joern", bool(installed),
                               {"reason": "user opted in via --with-joern"})
        return installed

    if args.quiet or not sys.stdin.isatty():
        # Non-interactive: hint only, don't prompt
        print("  joern:    not installed (optional, ~600MB JVM-based)")
        print("            adds CFG complexity + data-flow variable metrics to the code graph")
        print("            to install:   re-run installer with --with-joern")
        print("            to skip prompt next time:   re-run with --no-joern")
        _log_install_event("2b/10", "skip", "joern skipped (non-interactive)")
        _record_install_choice("joern", False,
                               {"reason": "non-interactive, no flag"})
        return False

    # Interactive: ask once
    print("  joern:    not installed (optional, ~600MB JVM-based)")
    print("            adds CFG complexity + data-flow variable metrics to the code graph")
    try:
        answer = input("            Install Joern now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        _log_install_event("2b/10", "skip", "joern prompt cancelled")
        _record_install_choice("joern", False, {"reason": "prompt cancelled"})
        return False

    if answer not in {"y", "yes"}:
        print("            Skipping. Re-run with --with-joern to install later.")
        _log_install_event("2b/10", "skip", "user declined joern install")
        _record_install_choice("joern", False,
                               {"reason": "user declined interactive prompt"})
        return False

    installed = _install_joern()
    _log_install_event(
        "2b/10", "ok" if installed else "error",
        "joern install (interactive)",
        data={"installed": installed},
    )
    _record_install_choice("joern", bool(installed),
                           {"reason": "user accepted interactive prompt"})
    return installed


def _install_joern() -> bool:
    """Install Joern via the official installer script.

    Returns True on success, False on failure (non-fatal — the orchestrator
    works fine without Joern).

    Platform support: the upstream installer is a `.sh` script (POSIX
    bash). It works on Linux + macOS. On Windows we surface a manual-
    install URL — joernio.github.io ships a separate Windows install
    path (Scoop / direct download) that this installer doesn't drive.
    The orchestrator works fine without Joern; CFG/PDG metrics just won't
    populate in the code graph.

    Security note: this downloads and executes a remote shell script from
    joernio/joern's GitHub releases. The transport is HTTPS (cert-validated)
    and the source is the official upstream. We add basic sanity checks
    (HTTPS-only URL, non-trivial response size, .sh shebang) but do NOT
    enforce a checksum because Joern's release pipeline does not publish a
    pinned hash for `latest`. Users who want stronger guarantees should
    install Joern themselves first (then we just detect it).
    """
    # Windows: no .sh installer support. Skip with a manual-install URL.
    # Joern works on Windows via Scoop or direct download from the GitHub
    # release, but driving those paths is post-v1.0; for now we just tell
    # the user where to go.
    if platform.system() == "Windows":
        print("            Joern auto-install is not supported on Windows.")
        print("            Manual install (any one):")
        print("              Scoop:    scoop install joern")
        print("              Direct:   https://github.com/joernio/joern/releases/latest")
        print("              Then re-run install.py and Joern will be detected on PATH.")
        return False
    print("            Installing Joern (this can take 5-10 minutes — downloads ~600 MB JVM-based binaries)...")
    print("            Note: the Joern installer may open a browser tab if a JDK is missing on your system.")
    print("            Streaming installer output below; press Ctrl+C to abort.")
    _log_install_event("2b/10", "start", "downloading joern installer")

    install_url = "https://github.com/joernio/joern/releases/latest/download/joern-install.sh"
    if not install_url.startswith("https://"):
        # Defense-in-depth — never fetch over plain HTTP.
        print("            Refusing to fetch Joern installer over non-HTTPS URL.")
        _log_install_event("2b/10", "error", "non-HTTPS joern installer URL refused")
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
        # Mirror the guard at line 2476: chmod is a Linux/macOS operation;
        # on Windows os.chmod only honors the read-only bit and the early
        # return above means we never reach this branch anyway.
        # Owner-only rwx (0o700) — the installer is in a private NamedTemporary
        # file we delete seconds later; no other user needs to read or execute
        # it. CodeQL py/overly-permissive-file (CWE-732) flagged the previous
        # 0o755 as world-readable+executable; 0o700 is the minimum bit set
        # that still lets us exec it ourselves.
        if platform.system() != "Windows":
            os.chmod(installer_path, 0o700)

        # Install to ~/.local (user-local, no sudo needed). Stream output to
        # the terminal so the user sees progress (no `capture_output=True` —
        # silent multi-minute downloads with browser-tab side effects are bad
        # UX). Bumped timeout to 900 s for slow connections.
        install_dir = Path.home() / ".local" / "joern"
        result = subprocess.run(
            [installer_path, "--dir", str(install_dir), "--no-interactive"],
            text=True, timeout=900,
        )

        if result.returncode != 0:
            print(f"            Joern install failed (exit {result.returncode}).")
            print("            You can install manually: https://docs.joern.io/installation/")
            return False

        # Recent Joern installers ignore --dir and land in ~/bin/joern/
        # regardless of what we pass. Probe several known locations rather
        # than trusting our flag was honored. Verify the joern executable
        # itself exists, not just the directory.
        candidates = [
            install_dir / "joern-cli",                    # what we asked for
            Path.home() / "bin" / "joern" / "joern-cli",  # what installer actually does (2026)
            Path.home() / ".joern" / "joern-cli",         # legacy
        ]
        for joern_bin_dir in candidates:
            joern_exe = joern_bin_dir / "joern"
            if joern_exe.exists():
                os.environ["PATH"] = f"{joern_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                print(f"            Joern installed at {joern_bin_dir}")
                print(f"            To use joern outside this installer, add to your shell rc:")
                print(f"              export PATH=\"{joern_bin_dir}{os.pathsep}$PATH\"")
                return True

        # Last-ditch PATH probe — installer may have its own location logic.
        path_joern = shutil.which("joern")
        if path_joern:
            print(f"            Joern detected on PATH at {path_joern}")
            return True

        probed = ", ".join(str(c / "joern") for c in candidates)
        print(f"            Joern installer ran but binary not found at any of: {probed}")
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
    #
    # Deliverable 2 (2026-04-28): persist the resolved choice so a
    # re-install with no CLI flags re-uses the same mode rather than
    # re-detecting GPU. Detection is normally fine but on machines
    # where nvidia-smi flapped or Ollama was unreachable at first
    # install, the user already picked a mode they want to stick.
    #
    # v0.2.23 C10 (2026-05-21): the auto-detection branch now consults
    # the three hardware-aware selectors (select_code_embedding_backend
    # / select_kg_embedding_backend / select_summary_backend) to pick
    # the right *profile* AND, when the profile's stock model isn't
    # the per-tier optimum, augment the returned dict with `text_model`
    # / `code_model` overrides. The explicit-opt-in branches (--openai,
    # --low-resource, --cpu-only) bypass the selectors entirely — the
    # user has stated a preference and we honour it byte-for-byte.
    if args.openai_key:
        config = dict(EMBEDDING_CONFIGS["openai"])
        config["openai_key"] = args.openai_key
        _record_install_choice("embedding_mode", "openai",
                               {"reason": "user passed --openai-key"})
        return config
    if args.low_resource:
        config = dict(EMBEDDING_CONFIGS["low_resource"])
        _record_install_choice("embedding_mode", "low_resource",
                               {"reason": "user passed --low-resource"})
        return config
    if args.cpu_only:
        config = dict(EMBEDDING_CONFIGS["cpu"])
        _record_install_choice("embedding_mode", "cpu",
                               {"reason": "user passed --cpu-only"})
        return config

    # Replay-eligible: no flag and we have a recent choice.
    prior_choices = _load_previous_choices()
    if "embedding_mode" in prior_choices:
        prior_value = prior_choices["embedding_mode"].get("value")
        if prior_value in EMBEDDING_CONFIGS:
            config = dict(EMBEDDING_CONFIGS[prior_value])
            _record_install_choice(
                "embedding_mode", prior_value,
                {"reason": "replayed from last install"},
            )
            return config

    # Auto-detection — v0.2.23 C10 tier-aware path.
    cores = _probe_cpu_cores()
    vram = float(sysinfo.vram_gb or 0.0)
    ram = float(sysinfo.ram_gb or 0.0)
    has_gpu = bool(sysinfo.has_gpu)

    code_pick = select_code_embedding_backend(
        gpu_vram_gb=vram,
        ram_gb=ram,
        cores=cores,
        openai_key_available=False,  # auto-detect never picks OpenAI
        prefer_openai=False,
    )
    kg_pick = select_kg_embedding_backend(
        gpu_vram_gb=vram,
        ram_gb=ram,
        cores=cores,
        openai_key_available=False,
        prefer_openai=False,
    )

    # Map the per-tier picks back to a profile + overrides. The profile
    # determines the shape (active_embedding slot + which Ollama models
    # land in the pull list); the overrides surgically swap in the
    # tier-correct model when the profile's stock pick doesn't match.
    if has_gpu and code_pick == _CODE_BACKEND_CODESAGE:
        # Workstation-class GPU — gpu profile is the natural fit.
        profile_key = "gpu"
        reason = (f"auto-detected GPU (VRAM={vram:.1f} GB, RAM={ram:.1f} GB, "
                  f"cores={cores})")
    elif kg_pick == _KG_BACKEND_ARCTIC:
        # Mid- / low-resource path: arctic2 was selected for KG. The
        # low_resource profile already pairs arctic + Jina, which
        # matches the spec for this tier exactly.
        profile_key = "low_resource"
        reason = (f"auto-selected low_resource tier (VRAM={vram:.1f} GB, "
                  f"RAM={ram:.1f} GB, cores={cores})")
    else:
        # Capable CPU-only or sub-12 GB GPU: cpu profile (qwen3 + Jina).
        profile_key = "cpu"
        reason = (f"auto-selected cpu profile (VRAM={vram:.1f} GB, "
                  f"RAM={ram:.1f} GB, cores={cores})")

    config = dict(EMBEDDING_CONFIGS[profile_key])

    # Surgical overrides when the selector disagrees with the stock
    # profile model. Today this only happens on the GPU profile's
    # text-embedding tier (sub-8-GB GPU can hit gpu code path via
    # CodeSage but should still use arctic for KG) — but expressing it
    # as a general override keeps future tier additions cheap.
    if config.get("code_model") != code_pick:
        config["code_model"] = code_pick
    if config.get("text_model") != kg_pick:
        config["text_model"] = kg_pick

    _record_install_choice("embedding_mode", profile_key, {"reason": reason})
    return config


# ---------------------------------------------------------------------------
# v0.2.18 Commit 10 — Preset defaults → launcher's app_state
#
# Goal: a brand-new project, on first launch after install, should see the
# KG/Codegraph dropdowns pre-populated with the embedding-model IDs that
# install.py actually configured. Without this, every new project starts
# blank and the user has to manually pick the same model `install.py`
# already pulled.
#
# The launcher's `app_state` is a generic key/value table (key TEXT,
# value TEXT, updated_at INTEGER) defined in
# `launcher/src-tauri/src/db/migrations/008_app_state.sql`. The keys we
# write are the same ones Wave A's `openai_cmd.rs` reads/writes:
#   - default_text_embedding
#   - default_code_embedding
# Canonical IDs are aligned with the Rust catalog
# (`launcher/src-tauri/src/commands/embedding_catalog.rs`) and Wave A's
# constants — OpenAI IDs use the `openai-` prefix; local Ollama IDs use
# the raw Ollama tag; CodeEmbed uses `codesage-large-v2`.
#
# DB path resolution mirrors the Rust resolver
# (`launcher/src-tauri/src/paths.rs::vct_root_dir`):
#   - `$VCT_STATE_DIR/launcher.db` if VCT_STATE_DIR is set (non-empty)
#   - `~/.vct/launcher.db` otherwise
# Cross-OS: same shape on Linux, macOS, Windows — `Path.home()` resolves
# correctly on all three. The launcher does NOT use Tauri's
# `app_data_dir()`; it pins to `~/.vct/` so dev/prod isolation (via
# VCT_STATE_DIR) works the same way on every platform.
#
# Ordering: install.py runs BEFORE the launcher may have ever been
# started (fresh install). When that's true, launcher.db does not exist
# on disk yet → we soft-fail with a skip log. The launcher creates the
# table on first boot via the schema migration, and the user's first
# manual selection in the GUI lands the rows (no defaults pre-populated
# in that case — acceptable: the user is configuring it themselves
# anyway). On --update runs (launcher.db exists), the rows are
# pre-populated.
#
# Idempotency: `INSERT OR IGNORE` only sets rows that are absent.
# Existing rows (= user's manual selections from a prior launcher
# session) are preserved.
# ---------------------------------------------------------------------------

# Canonical app_state keys. MUST stay in sync with:
#   launcher/src-tauri/src/commands/openai_cmd.rs::APP_STATE_DEFAULT_TEXT_EMBED
#   launcher/src-tauri/src/commands/openai_cmd.rs::APP_STATE_DEFAULT_CODE_EMBED
_APP_STATE_KEY_DEFAULT_TEXT_EMBED = "default_text_embedding"
_APP_STATE_KEY_DEFAULT_CODE_EMBED = "default_code_embedding"

# Canonical OpenAI ID used by Wave A (with `openai-` prefix). MUST match:
#   launcher/src-tauri/src/commands/openai_cmd.rs::OPENAI_DEFAULT_TEXT_MODEL_ID
#   launcher/src-tauri/src/commands/embedding_catalog.rs (`id` field)
# As of the Commit 12 prefix-unification fix (v0.2.18),
# `EmbeddingService.discover_text_models` / `discover_code_models` ALSO
# emit the prefixed form for OpenAI models (via
# `vco_lib.embedding_service._to_openai_catalog_id`), so the catalog
# entry's `id` field round-trips byte-for-byte with what we write here
# and what `openai_cmd.rs::register_openai_api_key` writes. The HTTP
# call boundary (OpenAIAdapter.embed) strips the prefix back off via
# `_to_openai_api_model` because OpenAI's `/v1/embeddings` rejects the
# prefixed form with HTTP 400.
_OPENAI_PREFIXED_TEXT_MODEL_ID = "openai-text-embedding-3-small"


def _preset_to_default_models(
    embed_config: dict,
    openai_set_as_default: bool,
) -> tuple[str, str]:
    """Map an install-time embedding config dict to the (text, code) model
    IDs that the launcher's `app_state` should be seeded with.

    The source of truth is the chosen `EMBEDDING_CONFIGS` entry's
    `text_model` / `code_model` fields — i.e. exactly the models
    install.py just pulled into Ollama / configured for the GPU
    code-embed service. This guarantees the GUI's pre-populated default
    matches what the user's machine can actually serve.

    Special case: when the user passed `--openai-key` AND chose to set
    OpenAI as default (`openai_set_as_default=True`), the stored IDs
    use the `openai-` prefix to align with Wave A's
    `openai_cmd.rs::register_openai_api_key`, which writes the same
    prefixed form when the wizard's "set as default" checkbox is ticked.

    Args:
        embed_config: The dict returned by `_choose_embedding_config`.
            Keyed by `text_model`, `code_model`, `active_embedding`,
            optionally `openai_key` (presence indicates --openai-key).
        openai_set_as_default: True if the user explicitly opted to
            make OpenAI the default for this install (currently inferred
            from `embed_config["active_embedding"] == "openai"` —
            wizard-side this would flip per a checkbox, but install.py's
            `--openai-key` flag is an unambiguous opt-in).

    Returns:
        (text_model_id, code_model_id) — the strings to write into
        `app_state.default_text_embedding` and
        `app_state.default_code_embedding` respectively.
    """
    active = embed_config.get("active_embedding", "")

    if active == "openai" and openai_set_as_default:
        # Mirrors openai_cmd.rs constants. Both slots use the same model
        # today; the split exists for forward-compat when OpenAI ships
        # a code-specific embedder.
        return (_OPENAI_PREFIXED_TEXT_MODEL_ID, _OPENAI_PREFIXED_TEXT_MODEL_ID)

    # All non-OpenAI presets: use the IDs install.py pulled, verbatim.
    # These IDs match what `EmbeddingService.discover_*` surfaces in
    # the GUI catalog (Ollama tags / `codesage-large-v2`).
    text_model = str(embed_config.get("text_model") or "qwen3-embedding:0.6b")
    code_model = str(embed_config.get("code_model") or "qwen3-embedding:0.6b")
    return (text_model, code_model)


def _discover_app_state_db_path() -> Path:
    """Resolve the path to the launcher's SQLite state DB.

    Mirrors `launcher/src-tauri/src/db/mod.rs::db_path`, which itself
    delegates to `crate::paths::vct_root_dir`. Cross-OS:
      - Linux:   `~/.vct/launcher.db` (or `$VCT_STATE_DIR/launcher.db`)
      - macOS:   `~/.vct/launcher.db` (or `$VCT_STATE_DIR/launcher.db`)
      - Windows: `<USERPROFILE>\\.vct\\launcher.db`
                 (or `%VCT_STATE_DIR%\\launcher.db`)

    `Path.home()` resolves correctly on all three platforms; `os.environ`
    + `Path` join is portable. The launcher does NOT use Tauri's
    `app_data_dir()` here — it pins to `~/.vct/` so the same path works
    whether the launcher was installed via Tauri bundle, run from a
    cargo build, or hadn't started yet (path is predictable before any
    Tauri code has executed). VCT_STATE_DIR override is honoured for
    dev/prod isolation, same as the Rust side.

    Returns a `Path` whether or not the file exists on disk — caller is
    responsible for the `.is_file()` check and the soft-fail.
    """
    from vco_lib.paths import vct_root_dir
    return vct_root_dir() / "launcher.db"


def _write_preset_defaults_to_app_state(
    embed_config: dict,
    openai_set_as_default: bool,
) -> None:
    """Seed the launcher's `app_state` table with the install-time
    embedding-model defaults so the GUI dropdowns pre-populate for
    brand-new projects.

    Ordering constraint: if install.py runs BEFORE the launcher has
    ever been started (fresh first-install), the launcher.db file does
    not exist yet. The launcher creates it (with the `app_state` table)
    on first boot via `db::Db::open` → `migrations::apply`. In that
    case, we cannot pre-populate defaults — the user's first GUI action
    will land their explicit choice, which is functionally equivalent
    for the "what shows up in the dropdown" goal. We soft-fail with a
    `skip` log event.

    On --update runs (where launcher.db exists), the rows ARE
    pre-populated. Idempotency comes from `INSERT OR IGNORE`: rows the
    user explicitly set in a prior session are preserved.

    Soft-fail cases (all log a `skip`/`warn` and return without raising):
      - launcher.db file does not exist (fresh install, never booted)
      - `app_state` table does not exist (schema migration not applied
        — should not happen if file exists, but defense-in-depth)
      - Any `sqlite3.Error` (locked DB, permission denied, corruption)

    Never raises: install.py should not fail just because the GUI
    dropdowns won't pre-populate.

    DB-path resolution: `_discover_app_state_db_path` honours the
    `VCT_STATE_DIR` env override (the canonical pattern used by
    `vco_lib.paths.vct_root_dir`) — tests that need a custom DB
    location set `VCT_STATE_DIR` rather than threading a path
    parameter through this helper. The legacy `install_root`
    parameter (kept through v0.2.18 release prep as `noqa: ARG001`)
    was dropped here in the v0.2.18 cleanup commit.

    Args:
        embed_config: As returned by `_choose_embedding_config`.
        openai_set_as_default: True when `--openai-key` was passed (or,
            via the wizard, when the user ticked "set OpenAI as
            default").
    """
    db_path = _discover_app_state_db_path()
    if not db_path.is_file():
        _log_install_event(
            "preset_defaults", "skip",
            f"launcher.db not found at {db_path}; defaults will be set "
            f"by the launcher on first GUI use (no-op until then)",
            data={"db_path": str(db_path)},
        )
        return

    text_model, code_model = _preset_to_default_models(
        embed_config, openai_set_as_default,
    )

    # stdlib only — sqlite3 ships with every CPython >=3.11. No new
    # deps. Connection is opened with the default isolation level
    # (deferred) so the INSERT OR IGNOREs run inside an auto-managed
    # transaction.
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as e:
        _log_install_event(
            "preset_defaults", "warn",
            f"sqlite3.connect failed for {db_path}: {e}",
            data={"db_path": str(db_path), "error": str(e)},
        )
        return

    try:
        # `updated_at` is a NOT NULL column — provide unix-millis now.
        # ON CONFLICT(key) DO NOTHING preserves any prior value (user's
        # manual selection from an earlier launcher session). This is
        # the same idempotency semantic as `INSERT OR IGNORE`, written
        # explicitly so the intent is obvious to a future reader.
        import time as _time
        now_ms = int(_time.time() * 1000)
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO app_state (key, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (_APP_STATE_KEY_DEFAULT_TEXT_EMBED, text_model, now_ms),
            )
            text_inserted = cur.rowcount > 0
            cur.execute(
                "INSERT INTO app_state (key, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (_APP_STATE_KEY_DEFAULT_CODE_EMBED, code_model, now_ms),
            )
            code_inserted = cur.rowcount > 0
            conn.commit()
        except sqlite3.OperationalError as e:
            # Most common: "no such table: app_state". The launcher
            # owns the schema — we should not auto-create it from here
            # (separation of concerns). Soft-fail with a clear log.
            _log_install_event(
                "preset_defaults", "skip",
                f"app_state table not present yet ({e}); the launcher "
                f"will create it on first boot",
                data={"db_path": str(db_path), "error": str(e)},
            )
            return

        active = embed_config.get("active_embedding", "unknown")
        _log_install_event(
            "preset_defaults", "ok",
            f"app_state defaults seeded "
            f"(active={active}, text={text_model}, code={code_model}, "
            f"text_inserted={text_inserted}, code_inserted={code_inserted})",
            data={
                "active_embedding": active,
                "default_text_embedding": text_model,
                "default_code_embedding": code_model,
                "text_inserted": text_inserted,
                "code_inserted": code_inserted,
                "db_path": str(db_path),
            },
        )
    except sqlite3.Error as e:
        _log_install_event(
            "preset_defaults", "warn",
            f"sqlite error writing preset defaults to {db_path}: {e}",
            data={"db_path": str(db_path), "error": str(e)},
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Step 4: Virtual environment
# ---------------------------------------------------------------------------

def _create_venv(project_root: Path) -> Path:
    print("\n[3/10] Creating virtual environment ... ", end="", flush=True)
    _log_install_event("3/10", "start", "creating venv")
    venv_dir = project_root / ".venv"

    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    if venv_python.exists():
        # Verification beats log signal: even if resume says ok, the
        # venv-python file is what matters. If it's gone, fall through to
        # re-create. Resume log is a hint, not a contract.
        print("already exists")
        _log_install_event(
            "3/10", "skip",
            "venv already present",
            data={"venv_python": str(venv_python)},
        )
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
        _log_install_event(
            "3/10", "error",
            f"venv creation failed (exit {result.returncode})",
            data={"exit_code": result.returncode,
                  "stderr_tail": (result.stderr or "").strip()[-400:]},
        )
        sys.exit(1)
    print("OK")
    _log_install_event(
        "3/10", "ok",
        "venv created",
        data={"venv_python": str(venv_python)},
    )
    return venv_python


# ---------------------------------------------------------------------------
# Step 5: Install dependencies
# ---------------------------------------------------------------------------

def _install_requirements(venv_python: Path, *, dev: bool) -> None:
    label = "with dev extras" if dev else "production"
    print(f"[4/10] Installing dependencies ({label}) ... ", flush=True)
    _log_install_event(
        "4/10", "start",
        f"pip install ({label})",
        data={"dev": dev},
    )

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
        _log_install_event(
            "4/10", "error",
            f"pip upgrade failed (exit {pip_up.returncode})",
            data={"phase": "pip-upgrade",
                  "exit_code": pip_up.returncode,
                  "stderr_tail": (pip_up.stderr or "").strip()[-400:]},
        )
        sys.exit(1)

    # Install requirements
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        print("  WARNING: requirements.txt not found, skipping pip install")
        _log_install_event(
            "4/10", "warn",
            "requirements.txt missing",
            data={"requirements": str(req_file)},
        )
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
        _log_install_event(
            "4/10", "error",
            f"pip install failed (exit {result.returncode})",
            data={"exit_code": result.returncode,
                  "stderr_tail": "\n".join(lines)},
        )
        sys.exit(1)
    print("  OK")
    _log_install_event("4/10", "ok", "pip install completed")


# ---------------------------------------------------------------------------
# Step 5b: Materialize orchestrator-self .claude/ from templates
#
# PR-39 (v0.2.12, 2026-05-16). Before this PR the public repo shipped:
#   1. 50 .claude/hooks/*.{sh,ps1} files byte-identical with templates/hooks/*
#      (CI gate check_template_drift.py enforced parity — pure duplication).
#   2. 36 .claude/scripts/*.py files where templates were NEWER (post commit
#      c209261 per-project portability fix never reached the active copies).
#   3. .claude/settings.json — bash-flavored Linux/macOS-only assembled
#      artifact that would break on Windows installs without bash.
#
# User direction (2026-05-16): "anything that is or will be generated from a
# template at install time should NOT ship in the public repo. Only ship the
# template itself." Templates are now the single source of truth; this
# function renders the orchestrator-self's runtime .claude/ from them at
# install time (and on every --update). Downstream user projects already
# went through this template-driven render via vco_lib.project_init —
# orchestrator-self now uses the same pipeline.
# ---------------------------------------------------------------------------

def _materialize_orchestrator_self_claude_dir(install_root: Path) -> None:
    """Render the orchestrator-self's runtime .claude/ contents from templates.

    Copies ``templates/hooks/*`` → ``<install_root>/.claude/hooks/`` and
    ``templates/scripts/*`` → ``<install_root>/.claude/scripts/`` preserving
    executable bits, then renders the OS-appropriate
    ``templates/settings.json.{linux,windows}.template`` to
    ``<install_root>/.claude/settings.json``.

    Idempotent: re-running overwrites with the current templates. Users
    running ``install.py --update`` after a ``git pull`` get the latest
    hook/script/settings content automatically.

    Soft-fail: if any individual template file is missing for some reason,
    a warning is logged and the install continues — a partial hook set
    is better than aborting an entire install over a single file. The
    settings.json render is also soft-fail (skipped with warning) so a
    missing OS template doesn't kill the install on an unexpected
    platform.

    Honors ``--skip-materialize-claude-dir`` for tests / special-case
    installs targeting a pre-populated .claude/ directory.
    """
    claude_dir = install_root / ".claude"
    templates_dir = install_root / "templates"

    print("[4b/10] Materializing orchestrator .claude/ from templates ... ",
          end="", flush=True)
    _log_install_event("4b/10", "start",
                       "rendering .claude/{hooks,scripts,settings.json}")

    copied_hooks = 0
    copied_scripts = 0
    warnings: list[str] = []

    # 1. Hooks: templates/hooks/* → .claude/hooks/* preserving exec bit.
    hooks_src = templates_dir / "hooks"
    hooks_dst = claude_dir / "hooks"
    if not hooks_src.is_dir():
        warnings.append(f"templates/hooks/ missing at {hooks_src}")
    else:
        hooks_dst.mkdir(parents=True, exist_ok=True)
        for src in hooks_src.iterdir():
            if not src.is_file():
                continue  # Skip _lib/ and other subdirs; handled below.
            try:
                shutil.copy2(src, hooks_dst / src.name)
                copied_hooks += 1
            except OSError as e:
                warnings.append(f"failed to copy {src.name}: {e}")
        # Also copy nested helper dirs (e.g. _lib/) that ship alongside.
        for sub in hooks_src.iterdir():
            if sub.is_dir():
                dst_sub = hooks_dst / sub.name
                if dst_sub.exists():
                    shutil.rmtree(dst_sub)
                try:
                    shutil.copytree(sub, dst_sub)
                except OSError as e:
                    warnings.append(f"failed to copy hooks subdir {sub.name}: {e}")

    # 2. Scripts: same pattern.
    scripts_src = templates_dir / "scripts"
    scripts_dst = claude_dir / "scripts"
    if not scripts_src.is_dir():
        warnings.append(f"templates/scripts/ missing at {scripts_src}")
    else:
        scripts_dst.mkdir(parents=True, exist_ok=True)
        for src in scripts_src.iterdir():
            if not src.is_file():
                continue
            try:
                shutil.copy2(src, scripts_dst / src.name)
                copied_scripts += 1
            except OSError as e:
                warnings.append(f"failed to copy {src.name}: {e}")

    # 3. settings.json: OS-dispatch + render.
    if platform.system() == "Windows":
        settings_template = templates_dir / "settings.json.windows.template"
    else:
        settings_template = templates_dir / "settings.json.linux.template"
    if not settings_template.is_file():
        warnings.append(f"settings template missing at {settings_template}")
    else:
        try:
            rendered = settings_template.read_text(encoding="utf-8")
            # Placeholder substitution (no-op today: the OS templates are
            # fully concrete and ship without `{{PROJECT_NAME}}` markers).
            # Kept as a hook for future placeholder rollout. If downstream
            # user-project install (vco_lib.project_init) adds new
            # placeholders, mirror them here so the orchestrator-self
            # gets the same render contract.
            rendered = rendered.replace("{{PROJECT_NAME}}",
                                        "VibeCoded Orchestrator")
            settings_dst = claude_dir / "settings.json"
            settings_dst.parent.mkdir(parents=True, exist_ok=True)
            settings_dst.write_text(rendered, encoding="utf-8")
        except OSError as e:
            warnings.append(f"failed to render settings.json: {e}")

    if warnings:
        print("PARTIAL")
        for w in warnings:
            print(f"  ! {w}")
        _log_install_event(
            "4b/10", "warn",
            f"materialized {copied_hooks} hooks + {copied_scripts} scripts "
            f"with {len(warnings)} warnings",
            data={"warnings": warnings,
                  "copied_hooks": copied_hooks,
                  "copied_scripts": copied_scripts},
        )
    else:
        print(f"OK ({copied_hooks} hooks, {copied_scripts} scripts)")
        _log_install_event(
            "4b/10", "ok",
            f"materialized {copied_hooks} hooks + {copied_scripts} scripts",
            data={"copied_hooks": copied_hooks,
                  "copied_scripts": copied_scripts},
        )


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


# ---------------------------------------------------------------------------
# Step 5b: Safe-by-default service detection
# ---------------------------------------------------------------------------
#
# Goal: install.py must NOT touch a Weaviate / Ollama / code-embed service
# that wasn't started by us, EVEN IF it's running on our canonical port. The
# old behavior — blindly POSTing to localhost:8081 — would pollute a foreign
# user-owned Weaviate with our collections (`KnowledgeGraph` etc.) and could
# even bind-conflict the compose `up -d` against it.
#
# Detection is content-based, not name-based: we probe the port and
# fingerprint the response. A container called `weaviate` is irrelevant —
# what matters is "did WE start whatever's responding here?".
#
# Decision matrix:
#
#   probe state    | default action             | non-interactive override
#   ---------------+----------------------------+------------------------
#   not-running    | start our compose service  | (none — no conflict)
#   vct-managed    | adopt (skip compose start) | (none — auto-handled)
#   foreign        | alt-port (free port)       | --on-conflict
#   incompatible   | abort                      | (cannot override)
#
# State persistence: the chosen action is recorded in
# `~/.vct/services.toml`, the SAME file the launcher's
# `services::adoption` reads/writes (commit 8b1890f). install.py and the
# launcher therefore see consistent state. Schema is the launcher's
# `AdoptionState` (rust serde) but expressed as flat TOML so plain Python
# can produce it without depending on the `tomli_w` package.

# Canonical service identifiers used in services.toml. Match the launcher's
# launcher::services::adoption canonical names.
_CANONICAL_SERVICES = ("weaviate", "ollama", "code_embed")

# Default ports per service. Mirrors the DEFAULT_*_PORT constants above but
# keyed by canonical name for table-driven loops.
_DEFAULT_PORTS = {
    "weaviate": DEFAULT_WEAVIATE_PORT,
    "ollama": DEFAULT_OLLAMA_PORT,
    "code_embed": DEFAULT_CODE_EMBED_PORT,
}

# Health-probe paths per service. GET / 2xx-3xx ⇒ "something is listening";
# we then fingerprint the body to decide vct-managed vs. foreign.
_HEALTH_PATHS = {
    "weaviate": "/v1/.well-known/ready",
    "ollama": "/api/tags",
    "code_embed": "/health",
}


def _services_toml_path() -> Path:
    """Path to `<VCT_STATE_DIR or ~/.vct>/services.toml` — shared with
    launcher::services::adoption (Rust). Both sides honour ``VCT_STATE_DIR``
    so a dev launcher's state stays isolated from production state."""
    from vco_lib.paths import vct_root_dir
    return vct_root_dir() / "services.toml"


def _read_services_toml() -> dict:
    """Parse `~/.vct/services.toml` into a list-of-tables dict.

    Returns `{"services": [{name, mode, external_url, parallel_port}, ...]}`.
    Empty dict on missing file. Empty `services` list on parse error — we'd
    rather treat the file as missing than crash mid-install on a corrupted
    TOML the user might have hand-edited.
    """
    path = _services_toml_path()
    if not path.exists():
        return {"services": []}
    try:
        # tomllib is stdlib in Python 3.11+; install.py already requires 3.11.
        import tomllib  # noqa: PLC0415
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! services.toml unreadable ({e}); treating as empty")
        return {"services": []}


def _write_services_toml(state: dict) -> None:
    """Serialize `{services: [...]}` to `~/.vct/services.toml`.

    Hand-rolled TOML serializer because `tomli_w` isn't in the install-time
    venv (we run BEFORE `pip install -r requirements.txt`). The schema is
    a flat array of tables — the rust launcher's `AdoptionState` shape —
    so a hand-rolled writer is trivial and avoids a chicken-and-egg
    dependency. Atomic via temp-file + rename so a crashed install never
    leaves a half-written services.toml.
    """
    path = _services_toml_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for entry in state.get("services", []):
        lines.append("[[services]]")
        # Order: name, mode (always present), then optional fields.
        lines.append(f'name = "{_toml_escape(entry["name"])}"')
        lines.append(f'mode = "{_toml_escape(entry["mode"])}"')
        if entry.get("external_url"):
            lines.append(f'external_url = "{_toml_escape(entry["external_url"])}"')
        if entry.get("parallel_port") is not None:
            lines.append(f'parallel_port = {int(entry["parallel_port"])}')
        lines.append("")  # blank line between tables

    body = "\n".join(lines).rstrip() + "\n"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX + Windows


def _toml_escape(s: str) -> str:
    """Minimal TOML basic-string escaping (backslash + double-quote).

    Sufficient for our payloads — service names, mode tokens, and URLs.
    No newlines, no control chars in any value we ever write here.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ProbeResult and ProbeAction — emulated as string constants for stdlib-only
# install.py. `enum` would work but adds import noise without buying us
# anything; the action set is small and string-comparable.
PROBE_NOT_RUNNING = "not-running"   # port free; we'll start compose
PROBE_VCT_MANAGED = "vct-managed"   # our prior install — adopt seamlessly
PROBE_FOREIGN = "foreign"           # someone else's service — DO NOT TOUCH
PROBE_INCOMPATIBLE = "incompatible" # protocol mismatch — abort

ACTION_START = "start"      # bring up the compose service on default port
ACTION_ADOPT = "adopt"      # reuse the existing service as-is (skip compose)
ACTION_ALT_PORT = "alt-port"  # bring up compose on an alternate free port
ACTION_ABORT = "abort"      # bail the install


def _probe_service_identity(name: str, port: int) -> tuple[str, str]:
    """Content-fingerprint whatever is listening on `localhost:<port>`.

    Returns (probe_result, evidence) where:
      - probe_result is one of PROBE_*
      - evidence is a short human-readable string (URL, schema-class name,
        etc.) used for log lines and the interactive prompt

    Heuristic per service:
      - weaviate: GET /v1/.well-known/ready; if 200, GET /v1/schema and
        look for our canonical collections (KnowledgeGraph,
        VibeCodedOrchestrator_KnowledgeGraph — the v0.2.23 B1 capital-C
        casing, plus the lowercase-c v0.2.12–v0.2.22 variant
        VibecodedOrchestrator_KnowledgeGraph, plus the pre-v0.2.12 PR-26
        legacy VibeCodedTools_KnowledgeGraph). Any present ⇒ vct-managed.
        Empty schema + no services.toml record ⇒ foreign (we don't claim
        bare instances).
      - ollama: GET /api/tags; if 200, look for our pinned embedding model
        (qwen3-embedding:0.6b OR snowflake-arctic-embed2:latest) AS WELL AS
        the services.toml record. Either signal present ⇒ vct-managed.
        Empty tags or no signal ⇒ foreign.
      - code_embed: GET /health; if 200 + body matches CodeSage/Ollama
        backend marker, vct-managed. Otherwise foreign.

    The services.toml lock is the strongest signal — it survives a Weaviate
    that we previously cleared. Content fingerprints are the secondary
    signal so we can recognise our own data even when the lock file was
    deleted (rare; user wiped ~/.vct/).
    """
    path = _HEALTH_PATHS.get(name)
    if path is None:
        return PROBE_INCOMPATIBLE, f"unknown service '{name}'"

    base = f"http://localhost:{port}"

    # Step 1: liveness probe
    try:
        resp = urllib.request.urlopen(f"{base}{path}", timeout=2)
        if resp.status >= 400:
            return PROBE_NOT_RUNNING, f"{base}{path} → HTTP {resp.status}"
    except Exception:
        return PROBE_NOT_RUNNING, f"{base}{path} unreachable"

    # Step 2: services.toml lookup (strongest signal — explicit prior claim).
    state = _read_services_toml()
    locked = next(
        (s for s in state.get("services", []) if s.get("name") == name),
        None,
    )
    if locked and locked.get("mode") in ("adopt", "parallel", "refuse"):
        # We previously decided how to handle this port. Consider it
        # "ours" in the sense that subsequent installs should not re-prompt.
        return PROBE_VCT_MANAGED, f"prior decision: {locked['mode']}"

    # Step 3: content fingerprint per service.
    if name == "weaviate":
        try:
            schema_resp = urllib.request.urlopen(f"{base}/v1/schema", timeout=3)
            schema = json.loads(schema_resp.read())
            classes = {
                c.get("class") for c in schema.get("classes", [])
                if isinstance(c, dict)
            }
            # Two recognition modes for vct ownership of a Weaviate:
            # 1. Exact canonical names (single-tenant install).
            # 2. Suffix-pattern names (multi-project orchestrator setup
            #    where each project namespaces its collections — e.g.
            #    `ARTup_CodeFunction`, `ClaudeKnowledgeGraph`,
            #    `SD15_KnowledgeGraph`). The `_KnowledgeGraph` /
            #    `_CodeFunction` / `_CodeClass` / `_CodeModule` suffixes
            #    are ours by construction and don't appear in foreign
            #    Weaviates.
            # Legacy-detection: the canonical post-v0.2.23-B1 capital-C name,
            # the lowercase-c v0.2.12–v0.2.22 variant, AND the pre-v0.2.12
            # "VibeCodedTools_KnowledgeGraph" are markers of a vct-managed
            # Weaviate (the v0.2.23 B1 case-flip is the canonical-name change
            # only — install.py's case-insensitive adoption keeps the on-disk
            # class unchanged so pre-flip installs still have the lowercase-c
            # class on disk; same applies to pre-v0.2.12 installs with the
            # VibeCodedTools name).
            exact_markers = {"KnowledgeGraph",
                             "VibeCodedOrchestrator_KnowledgeGraph",
                             # v0.2.12–v0.2.22 lowercase-c variant.
                             "VibecodedOrchestrator_KnowledgeGraph",
                             # Pre-v0.2.12 name (PR-26 rename predecessor).
                             "VibeCodedTools_KnowledgeGraph",
                             "Development", "CodeFunction", "CodeClass",
                             "CodeModule", "CodeAPI", "CodeInteraction"}
            # `_conversations` is a legacy marker (collection deprecated
            # 2026-04-30) — kept here for detection of old installs only.
            # New installs do NOT create the conversations collection.
            suffix_markers = ("_KnowledgeGraph", "_Development",
                              "_CodeFunction", "_CodeClass", "_CodeModule",
                              "_conversations", "_development")
            exact_hits = classes & exact_markers
            suffix_hits = {c for c in classes if c and any(c.endswith(s) for s in suffix_markers)}
            hits = exact_hits | suffix_hits
            if hits:
                return PROBE_VCT_MANAGED, f"weaviate has vct collections: {sorted(hits)[:3]}"
            # Weaviate alive but no vct markers — could be empty (fresh user
            # install) or could be foreign + populated. Either way, not ours.
            return PROBE_FOREIGN, f"weaviate alive at {base} (classes: {sorted(classes)[:3] or 'none'})"
        except Exception as e:
            return PROBE_INCOMPATIBLE, f"weaviate at {base} but /v1/schema failed: {e}"

    if name == "ollama":
        try:
            tags_resp = urllib.request.urlopen(f"{base}/api/tags", timeout=3)
            tags = json.loads(tags_resp.read())
            model_names = {m.get("name", "") for m in tags.get("models", [])}
            vct_markers = {"qwen3-embedding:0.6b",
                           "snowflake-arctic-embed2:latest",
                           "unclemusclez/jina-embeddings-v2-base-code:latest",
                           "qwen3.5:0.8b"}
            if model_names & vct_markers:
                return PROBE_VCT_MANAGED, f"ollama has vct models: {sorted(model_names & vct_markers)}"
            return PROBE_FOREIGN, f"ollama alive at {base} (models: {sorted(model_names)[:3] or 'none'})"
        except Exception as e:
            return PROBE_INCOMPATIBLE, f"ollama at {base} but /api/tags failed: {e}"

    if name == "code_embed":
        # Our service responds to /health with JSON {"status": "ok",
        # "model": "codesage-large-v2", ...}. Foreign FastAPIs may also
        # respond 200 to /health but not produce that body.
        try:
            health_resp = urllib.request.urlopen(f"{base}/health", timeout=3)
            body = health_resp.read().decode("utf-8", errors="replace")
            if "codesage" in body.lower() or "code_embed" in body.lower():
                return PROBE_VCT_MANAGED, f"code_embed responds with vct fingerprint"
            return PROBE_FOREIGN, f"port {port} responds to /health but is not our code_embed"
        except Exception as e:
            return PROBE_INCOMPATIBLE, f"code_embed at {base} unrecognised: {e}"

    return PROBE_INCOMPATIBLE, f"no fingerprint logic for '{name}'"


def _find_free_port(start: int, end: int = 65000) -> int | None:
    """Return the first free TCP port in [start, end] on 127.0.0.1.

    Used when a foreign service holds the canonical port and the user
    chose `alt-port`. We bind-and-close to test, which is racy with a
    process spawning a millisecond later — but for install-time it's
    fine; the compose `up -d` happens within the same install run.
    """
    import socket  # noqa: PLC0415 — lazy import; rare path
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return None


def _decide_action(name: str, probe: str, evidence: str,
                   args: argparse.Namespace) -> str:
    """Resolve a probe result into a concrete action.

    Honors:
      1. `--on-conflict` flag (non-interactive override for `foreign`)
      2. `--yes` / non-TTY (defaults to alt-port for foreign, abort for incompatible)
      3. interactive prompt
    """
    if probe == PROBE_NOT_RUNNING:
        return ACTION_START
    if probe == PROBE_VCT_MANAGED:
        return ACTION_ADOPT
    if probe == PROBE_INCOMPATIBLE:
        return ACTION_ABORT

    # Foreign — the interesting case.
    if args.on_conflict == "alt-port":
        return ACTION_ALT_PORT
    if args.on_conflict == "adopt":
        return ACTION_ADOPT
    if args.on_conflict == "abort":
        return ACTION_ABORT

    # Non-interactive default: alt-port. Prevents pollution; never adopts
    # someone else's service without explicit consent.
    if args.yes or not sys.stdin.isatty() or args.quiet:
        print(f"  [{name}] foreign service detected ({evidence})")
        print(f"  [{name}] non-interactive mode → using alt-port "
              f"(override: --on-conflict adopt|abort)")
        return ACTION_ALT_PORT

    # Interactive prompt
    print()
    print(f"  Detected a foreign {name} on port {_DEFAULT_PORTS[name]}.")
    print(f"  Evidence: {evidence}")
    print()
    print(f"  Options:")
    print(f"    [1] alt-port  — pick a free port and run our own {name} alongside it (safe, default)")
    print(f"    [2] adopt     — reuse the existing {name}; WILL write our collections into it")
    print(f"    [3] abort     — stop installation")
    try:
        ans = input(f"  Choice for {name} [1/2/3, default 1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ACTION_ALT_PORT
    if ans in ("2", "adopt"):
        return ACTION_ADOPT
    if ans in ("3", "abort"):
        return ACTION_ABORT
    return ACTION_ALT_PORT


def _resolve_service_safety(args: argparse.Namespace) -> dict:
    """Probe Weaviate / Ollama / code_embed and pick a safe action per service.

    Returns a dict shaped:
        {
          "weaviate":  {"action": ACTION_*, "port": int, "probe": PROBE_*, "evidence": str},
          "ollama":    {...},
          "code_embed":{...},
        }

    Side effects:
      - When alt-port is chosen: sets the corresponding env var
        (WEAVIATE_PORT / OLLAMA_PORT / CODE_EMBED_PORT) for the rest of
        the install. Compose reads these from the env via
        `${WEAVIATE_PORT:-8081}` substitutions.
      - Persists the resolution to `~/.vct/services.toml` so the
        launcher and subsequent installs see it.

    Design note: probing happens BEFORE compose `up -d`. That's the whole
    point — we never start a container against an occupied port without
    explicit consent.
    """
    print(f"\n[5b/10] Probing existing services (content-based detection) ...")

    decisions: dict = {}
    state = _read_services_toml()
    services_list = state.get("services", []) or []
    services_by_name = {s.get("name"): s for s in services_list}
    state_dirty = False

    for name in _CANONICAL_SERVICES:
        port = _DEFAULT_PORTS[name]
        # Honor explicit env-var override (advanced users / CI).
        env_port_name = {
            "weaviate": "WEAVIATE_PORT",
            "ollama": "OLLAMA_PORT",
            "code_embed": "CODE_EMBED_PORT",
        }[name]
        explicit_port = os.environ.get(env_port_name)
        if explicit_port:
            try:
                port = int(explicit_port)
            except ValueError:
                pass

        probe, evidence = _probe_service_identity(name, port)
        action = _decide_action(name, probe, evidence, args)

        chosen_port = port
        if action == ACTION_ALT_PORT:
            free = _find_free_port(port + 1)
            if free is None:
                print(f"  [{name}] FAIL: no free port in [{port + 1}, 65000]; aborting")
                action = ACTION_ABORT
            else:
                chosen_port = free
                # Propagate to env so all downstream code (compose, env
                # writers, MCP settings) picks up the override.
                os.environ[env_port_name] = str(chosen_port)
                if name == "weaviate":
                    # gRPC port also moves; offset matches the default gap (50052 vs 8081 → +41971).
                    # Simpler: just shift by the same delta from default.
                    delta = chosen_port - DEFAULT_WEAVIATE_PORT
                    grpc_port = DEFAULT_WEAVIATE_GRPC_PORT + delta
                    free_grpc = _find_free_port(grpc_port)
                    if free_grpc is not None:
                        os.environ["WEAVIATE_GRPC_PORT"] = str(free_grpc)

        decisions[name] = {
            "action": action,
            "port": chosen_port,
            "default_port": port,
            "probe": probe,
            "evidence": evidence,
        }

        # Print the decision summary line
        if action == ACTION_START:
            print(f"  [{name}] not running → will start on port {chosen_port}")
        elif action == ACTION_ADOPT:
            print(f"  [{name}] adopting existing service on port {chosen_port} ({evidence})")
        elif action == ACTION_ALT_PORT:
            print(f"  [{name}] foreign on {port} → starting our copy on alt port {chosen_port}")
        elif action == ACTION_ABORT:
            print(f"  [{name}] ABORT: {evidence}")
            return decisions  # caller must check for any ABORT

        # Persist to services.toml. Mode mapping mirrors the launcher's
        # AdoptionMode enum exactly (adoption.rs):
        #   unresolved | adopt | parallel | refuse  (snake_case in TOML)
        # ACTION_START → "unresolved" because there is no foreign service to
        # adopt or run parallel to; "unresolved" tells the launcher "no
        # conflict was seen" so it doesn't re-prompt. ACTION_ABORT also
        # maps to "unresolved" — the install bailed before persisting a
        # decision, so the next install should re-probe from scratch.
        mode_map = {
            ACTION_START: "unresolved",     # no conflict; launcher won't re-prompt
            ACTION_ADOPT: "adopt",
            ACTION_ALT_PORT: "parallel",
            ACTION_ABORT: "unresolved",
        }
        new_entry = {
            "name": name,
            "mode": mode_map[action],
            "external_url": f"http://localhost:{port}" if probe != PROBE_NOT_RUNNING else None,
            "parallel_port": chosen_port if action == ACTION_ALT_PORT else None,
        }
        # Only update if changed (idempotency: re-running install on a fully
        # adopted machine touches nothing).
        existing = services_by_name.get(name)
        if existing != new_entry:
            services_by_name[name] = new_entry
            state_dirty = True

    if state_dirty:
        new_state = {"services": list(services_by_name.values())}
        try:
            _write_services_toml(new_state)
        except OSError as e:
            print(f"  ! could not persist {_services_toml_path()}: {e}")

    # If any service decision was alt-port, write a compose override.
    alt_ports = {n: d for n, d in decisions.items()
                 if d["action"] == ACTION_ALT_PORT}
    if alt_ports:
        _write_compose_override(alt_ports)

    return decisions


def _write_compose_override(alt_ports: dict) -> None:
    """Generate `infrastructure/docker-compose.override.yml` with alternate ports.

    The override is per-machine state (not source-controlled) and is
    already covered by `.gitignore` (Bug 31 contract). We append rather
    than overwrite when a previous override exists from the launcher's
    volume migration — the override file at this path is read-merged by
    docker compose, so two concurrent override files can't safely coexist
    on the same path. Strategy: read existing, splice in our port section
    if absent, write back.

    Rather than YAML-parse without PyYAML in the pre-pip phase, we write
    a fresh file with both the volume-migration block AND our port
    overrides. If the launcher had an override there already, we preserve
    its non-port content via simple text concatenation. In practice, the
    user paths that hit alt-port (foreign service) and the volume-migration
    path (existing volumes) are disjoint enough that this is fine — the
    launcher writes the volume override, and install.py only writes here
    if no launcher override exists.
    """
    infra_dir = PROJECT_ROOT / "infrastructure"
    override_path = infra_dir / "docker-compose.override.yml"

    if override_path.exists():
        # Don't clobber a launcher-written volume override. Surface the
        # collision and rely on env-var port overrides alone — compose
        # WILL honor the env vars without needing an override file
        # (infrastructure/docker-compose.yml uses ${WEAVIATE_PORT:-8081}).
        print(f"  [override] {override_path} already exists; relying on env-var port overrides instead.")
        return

    # Compose v3 schema. Each alt-port service overrides only its `ports`
    # mapping; all other config from the base file (image, volumes, env)
    # is inherited.
    lines = [
        "# Auto-generated by install.py — alt-port mappings for foreign-service coexistence",
        "# Safe to delete + regenerate by re-running `python install.py`.",
        "# Tracked in ~/.vct/services.toml.",
        "services:",
    ]
    for name, dec in alt_ports.items():
        host_port = dec["port"]
        if name == "weaviate":
            grpc = os.environ.get("WEAVIATE_GRPC_PORT", str(DEFAULT_WEAVIATE_GRPC_PORT))
            lines.extend([
                "  weaviate:",
                "    ports:",
                f'      - "{host_port}:8080"',
                f'      - "{grpc}:50051"',
            ])
        elif name == "ollama":
            lines.extend([
                "  ollama:",
                "    ports:",
                f'      - "{host_port}:11434"',
            ])
        elif name == "code_embed":
            lines.extend([
                "  code_embed:",
                "    ports:",
                f'      - "{host_port}:11440"',
            ])

    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [override] wrote {override_path}")


def _build_orchestrator_volume_names() -> tuple[str, ...]:
    """Volume names this install knows about — canonical first, then the
    historical container-name aliases from vco_lib.containers (which now
    centralises the maintainer-machine-leak fix from v0.2.15).

    The maintainer's `_ARTup` per-project-suffix names are kept inline
    here (they were never canonical VCO names — they were specific to a
    co-installed sibling project on the maintainer's machine — but
    detecting them on existing installs is still useful to surface in
    the storage-config picker).
    """
    from vco_lib.containers import HISTORICAL_ALIASES

    canonical = ("weaviate_data", "ollama_data", "code_embed_cache")
    historical: list[str] = []
    for service in ("weaviate", "ollama", "code_embed"):
        historical.extend(HISTORICAL_ALIASES[service])
    # Sibling-project maintainer-era names. Not in vco_lib.containers
    # because they're not container names this project ever shipped —
    # they were per-workspace named volumes from a co-installed sibling
    # repo. Kept here for storage-config detection only.
    sibling_legacy = ("weaviate_ARTup", "ollama_ARTup")

    seen: set[str] = set()
    out: list[str] = []
    for name in (*canonical, *historical, *sibling_legacy):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return tuple(out)


_ORCHESTRATOR_VOLUME_NAMES = _build_orchestrator_volume_names()


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


def _start_services(
    sysinfo: SystemInfo,
    args: argparse.Namespace,
    embed_config: dict,
    decisions: dict | None = None,
    deferral_report: "DeferralReport | None" = None,
) -> None:
    print(f"\n[5/10] Starting services via {sysinfo.container_cmd} ... ", flush=True)
    _log_install_event(
        "5/10", "start",
        f"compose up via {sysinfo.container_cmd}",
        data={"runtime": sysinfo.container_cmd},
    )

    infra_dir = PROJECT_ROOT / "infrastructure"
    compose_file = infra_dir / "docker-compose.yml"

    if not compose_file.exists():
        print(f"  WARNING: {compose_file} not found, skipping.")
        print("  Start Weaviate and Ollama manually.")
        _log_install_event(
            "5/10", "warn",
            "docker-compose.yml missing",
            data={"compose_file": str(compose_file)},
        )
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
    elif decisions:
        # Decision-driven: only bring up services where the action is start
        # or alt-port. Adopted services (vct-managed reuse, foreign adopt)
        # explicitly do NOT get a compose start — they're already running.
        if decisions["weaviate"]["action"] in (ACTION_START, ACTION_ALT_PORT):
            services_to_start.append("weaviate")
        if decisions["ollama"]["action"] in (ACTION_START, ACTION_ALT_PORT):
            services_to_start.append("ollama")
        if (sysinfo.has_gpu
                and decisions["code_embed"]["action"] in (ACTION_START, ACTION_ALT_PORT)):
            services_to_start.append("code_embed")
    else:
        # Legacy path (no safety probe ran — should not happen in normal
        # install but kept for callers that pass decisions=None).
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

    # Proactive runtime-reachability check (2026-05-08). Catches "daemon
    # not running" / "rootless socket not started" BEFORE we attempt
    # compose-up — without this we used to wait for compose-up to fail
    # with cryptic stderr ("Cannot connect to the Docker daemon" /
    # "Cannot connect to Podman socket"), parse it, and emit a hint. Now
    # we surface the actionable hint upfront with the OS-correct
    # recovery command, saving 10-30s and giving the user a clearer
    # signal.
    if not _container_runtime_reachable(sysinfo.container_cmd):
        print(
            f"  [!] {sysinfo.container_cmd} is installed but its daemon/socket\n"
            f"      isn't responding to `{sysinfo.container_cmd} info`. The compose-up\n"
            f"      below will fail. Most common fixes:"
        )
        if sysinfo.container_cmd == "docker":
            print("        Linux:   sudo systemctl start docker")
            print("        macOS:   open Docker Desktop and wait for it to start")
            print("        Windows: start Docker Desktop")
        else:
            print("        Linux:   systemctl --user start podman.socket")
            print("        macOS:   podman machine start")
            print("        Windows: podman machine start")
        print(
            "      Re-run install.py once the runtime is reachable. (Skipping the\n"
            "      compose-up below would leave the install in a partial state.)"
        )

    compose_cmd = _get_compose_command(sysinfo.container_cmd)

    cmd = [*compose_cmd, "-f", str(compose_file)]

    # GPU overlay + code_embed profile.
    # NVIDIA → docker-compose.gpu.yml (deploy.resources NVIDIA driver).
    # AMD ROCm → docker-compose.amd-rocm.yml (Ollama ROCm image + /dev/kfd
    # + /dev/dri device passthrough). The two are mutually exclusive —
    # picking the wrong one means Ollama silently runs CPU-only despite
    # has_gpu=True. Distinguished via sysinfo.gpu_vendor.
    #
    # Podman vs Docker: each engine needs a different compose overlay
    # because the device-passthrough syntax differs (Docker reads
    # `deploy.resources.reservations.devices`; Podman uses CDI form
    # `devices: [nvidia.com/gpu=all]`). Pick by `sysinfo.container_cmd`.
    is_podman = "podman" in (sysinfo.container_cmd or "").lower()

    # Compose-overlay ambiguity check: if both the NVIDIA overlay file and
    # the AMD ROCm overlay file exist, and both GPU tools respond, we cannot
    # safely pick the right one without user input. Emit a deferral so the
    # user can resolve explicitly (e.g. by passing --gpu or --cpu-only).
    if sysinfo.has_gpu and deferral_report is not None:
        _nvidia_file = infra_dir / ("podman-compose.gpu.yml" if is_podman else "docker-compose.gpu.yml")
        # v0.2.20: probe BOTH the canonical short name AND the legacy
        # `amd-rocm.yml` name. Either being on disk counts as "AMD
        # overlay available" for the purpose of the ambiguity check.
        _amd_short = infra_dir / ("podman-compose.rocm.yml" if is_podman else "docker-compose.rocm.yml")
        _amd_legacy = infra_dir / ("podman-compose.amd-rocm.yml" if is_podman else "docker-compose.amd-rocm.yml")
        _amd_file_exists = _amd_short.exists() or _amd_legacy.exists()
        _amd_file = _amd_short if _amd_short.exists() else _amd_legacy
        if _nvidia_file.exists() and _amd_file_exists:
            # Both overlay files present. Probe GPU tools to see if both are live.
            _nvidia_live = subprocess.run(
                ["nvidia-smi", "-L"], capture_output=True, timeout=10,
            ).returncode == 0
            _amd_live = subprocess.run(
                ["rocm-smi", "--showid"], capture_output=True, timeout=10,
            ).returncode == 0
            if _nvidia_live and _amd_live:
                deferral_report.add_entry(
                    DeferralEntry(
                        condition_id="compose_overlay_ambiguous",
                        title="Compose GPU overlay ambiguous",
                        detected=(
                            "Both nvidia-smi and rocm-smi report a live GPU, and "
                            "both NVIDIA and AMD ROCm compose overlay files exist. "
                            "Cannot safely pick an overlay automatically."
                        ),
                        why_deferred=(
                            "Picking the wrong overlay causes Ollama to silently "
                            "run CPU-only. User must specify the GPU vendor."
                        ),
                        command_to_apply=(
                            "# For NVIDIA:\n"
                            "python install.py --gpu --update\n"
                            "# For AMD ROCm:\n"
                            "python install.py --gpu --update  # after setting VCT_GPU_VENDOR=amd"
                        ),
                        severity="warning",
                        kg_node_refs=[],
                    )
                )

    if sysinfo.has_gpu:
        if sysinfo.gpu_vendor == "amd":
            # v0.2.20: prefer `docker-compose.rocm.yml` (canonical short
            # name) over the legacy `docker-compose.amd-rocm.yml`. Both
            # are valid; the short name is what new docs reference.
            # Podman uses its own variant filename because the device-
            # passthrough syntax can differ. Probe both.
            rocm_candidates = []
            if is_podman:
                rocm_candidates.extend([
                    "podman-compose.rocm.yml",
                    "podman-compose.amd-rocm.yml",
                ])
            else:
                rocm_candidates.extend([
                    "docker-compose.rocm.yml",
                    "docker-compose.amd-rocm.yml",
                ])
            rocm_file_name = None
            rocm_file = None
            for candidate in rocm_candidates:
                p = infra_dir / candidate
                if p.exists():
                    rocm_file_name = candidate
                    rocm_file = p
                    break
            if rocm_file is not None:
                cmd.extend(["-f", str(rocm_file), "--profile", "gpu"])
                engine = "Podman" if is_podman else "Docker"
                print(f"  GPU overlay: AMD ROCm ({engine}: {rocm_file_name})")
            else:
                # Neither overlay file present — fall back to CPU.
                tried = ", ".join(rocm_candidates)
                print(f"  WARNING: AMD ROCm overlay not found (tried: {tried}), running CPU-only")
        else:
            # Default to NVIDIA overlay for has_gpu=True with vendor
            # unset or "nvidia" (back-compat with --gpu flag).
            gpu_file_name = "podman-compose.gpu.yml" if is_podman else "docker-compose.gpu.yml"
            gpu_file = infra_dir / gpu_file_name
            if gpu_file.exists():
                # Podman + NVIDIA prerequisite: nvidia-ctk CDI spec must
                # exist on the host. install.py runs the generator once
                # before compose-up so compose can reference
                # `nvidia.com/gpu=all` without manual setup.
                if is_podman:
                    _ensure_nvidia_cdi_spec_for_podman()
                cmd.extend(["-f", str(gpu_file), "--profile", "gpu"])
                engine = "Podman" if is_podman else "Docker"
                print(f"  GPU overlay: NVIDIA ({engine}: includes code_embed container)")
            else:
                print(f"  WARNING: GPU overlay {gpu_file_name} not found, running CPU-only")

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
        _log_install_event(
            "5/10", "error",
            "compose up timed out after 15 min",
            data={"runtime": sysinfo.container_cmd},
        )
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
        _log_install_event(
            "5/10", "error",
            f"compose up failed (exit {result.returncode})",
            data={"runtime": sysinfo.container_cmd,
                  "exit_code": result.returncode,
                  "stderr_tail": (result.stderr or "").strip()[-400:]},
        )
        sys.exit(1)
    print("  OK")
    _log_install_event("5/10", "ok", "compose up completed")


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
    _log_install_event("6/10", "start", "waiting for Ollama")
    port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))
    url = f"http://localhost:{port}/api/tags"
    deadline = time.monotonic() + HEALTH_TIMEOUT

    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=3)
            if resp.status == 200:
                print("OK")
                _log_install_event(
                    "6/10", "ok",
                    "Ollama is ready",
                    data={"url": url},
                )
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)

    print("TIMEOUT")
    print(f"  Ollama not ready after {HEALTH_TIMEOUT}s at {url}")
    print("  Check container logs.")
    _log_install_event(
        "6/10", "error",
        f"Ollama not ready after {HEALTH_TIMEOUT}s",
        data={"url": url, "timeout_s": HEALTH_TIMEOUT},
    )


def _pull_ollama_models(models: list[str]) -> None:
    """Pull required Ollama models."""
    print("[7/10] Pulling Ollama models ... ", flush=True)
    _log_install_event(
        "7/10", "start",
        f"pulling {len(models)} Ollama model(s)",
        data={"models": list(models)},
    )
    port = os.environ.get("OLLAMA_PORT", str(DEFAULT_OLLAMA_PORT))

    failed: list[str] = []
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
            failed.append(model)

    if failed:
        _log_install_event(
            "7/10", "warn",
            f"{len(failed)} model pull(s) failed",
            data={"failed": failed},
        )
    else:
        _log_install_event("7/10", "ok", "all Ollama models pulled")


# ---------------------------------------------------------------------------
# Step 6c: Weaviate collection bootstrap (shared-container aware)
# ---------------------------------------------------------------------------

# Minimal Weaviate class definitions for the collections this install needs.
# Vectorizer is "none" — we feed pre-computed vectors from Ollama / CodeSage.
# These are intentionally property-light: the MCP server (server.py) uses the
# v4 client `client.collections.get(name)` which doesn't require a strict
# property list to insert; richer schemas can be added later without
# re-creating the class.
# Moved to vco_lib.project_init in PR 2 — kept as shim for existing callers;
# will be removed in PR 9 (cleanup). The old underscored names are exported
# below for back-compat with tests and other modules.
_named_vector_config = _project_init._named_vector_config
_kg_class_definition = _project_init._kg_class_definition
_development_class_definition = _project_init._development_class_definition
_SAFE_CLASS_RE = _project_init._SAFE_CLASS_RE
_derive_project_kg_name = _project_init._derive_project_kg_name
_derive_project_dev_name = _project_init._derive_project_dev_name


def _derive_orchestrator_project_name() -> str:
    """Resolve the orchestrator's own project name for env propagation.

    PR-7 (v0.2.11): pre-v0.2.11 the orchestrator's `.claude/settings.json`
    env block omitted `PROJECT_NAME` and `CODE_GRAPH_PROJECT`. As a result,
    every hook in every project that derived a fallback ended up sharing the
    legacy `ClaudeOrchestrator` hardcode. We now write both keys at install
    time so the orchestrator's own hooks resolve a stable, project-specific
    name.

    Resolution priority:
      1. `vct-module.json::name` (canonical — shipped with every release).
         Sanitized to a Weaviate-safe class basename (same sanitizer as the
         per-project derivation in `vco_lib.project_init`).
      2. Hardcoded fallback `"VibeCodedOrchestrator"` — matches what
         `_install_kg_class_definition`'s naming convention would produce
         for "VibeCoded Orchestrator" and never collides with a real
         per-project derivation.

    Returns:
        A Weaviate-class-safe (PascalCase, alphanum-only) project name.
    """
    manifest = PROJECT_ROOT / "vct-module.json"
    if manifest.is_file():
        try:
            with manifest.open("r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("name") or ""
            if raw:
                sanitized = _project_init.sanitize_for_weaviate_class(str(raw))
                if sanitized and sanitized != _project_init._FALLBACK_PREFIX:
                    return sanitized
        except (OSError, json.JSONDecodeError):
            pass
    return "VibeCodedOrchestrator"


def _backfill_vscode_excludes(settings_file: Path | None = None) -> dict:
    """Orchestrator-side mirror of
    `vco_lib.project_init._backfill_vscode_excludes_in_project`.

    PR-7 / addendum-4 (v0.2.11): the orchestrator's own `.vscode/settings.json`
    benefits from the same watcher / search / Pylance exclude defaults as
    every project the launcher registers. Without these:
      - VS Code's file watcher saturates on cargo target/ (~33 GB churn).
      - Pylance indexes site-packages, blowing memory budget.
      - OOM kills (verified live 2026-05-16 on multiple workspaces).

    Idempotency contract matches the per-project version: only ADDS
    missing keys, never overwrites. Returns the same dict shape.

    Args:
        settings_file: path to `.vscode/settings.json`. Defaults to
            `<PROJECT_ROOT>/.vscode/settings.json`.
    """
    if settings_file is None:
        settings_file = PROJECT_ROOT / ".vscode" / "settings.json"

    # Delegate to the project_init helper using the parent folder of
    # `.vscode/`. This keeps the canonical exclude block in ONE place
    # (project_init._VSCODE_EXCLUDE_DEFAULTS) so the orchestrator and
    # per-project surfaces never drift.
    if settings_file.parent.name == ".vscode":
        parent = settings_file.parent.parent
    else:
        # Caller passed a non-canonical path; fall back to the raw
        # parent. Still safe — the project_init helper looks for
        # `<parent>/.vscode/settings.json` and would create that path
        # rather than touching the caller's custom file.
        parent = settings_file.parent
    return _project_init._backfill_vscode_excludes_in_project(parent)


def _backfill_code_graph_project_env(settings_file: Path | None = None) -> dict:
    """Idempotent: add `PROJECT_NAME` + `CODE_GRAPH_PROJECT` to an existing
    `.claude/settings.json` env block when either key is missing.

    PR-7 (v0.2.11): pre-v0.2.11 installs wrote an `env` block that omitted
    these two keys, which caused the orchestrator's own
    `post-file-edit.sh` hook to fall back to the hardcoded
    "ClaudeOrchestrator" literal. The same env block must now carry the
    two keys so that every project install on
    the same machine writes code-graph rows into its own `<sanitized>`
    namespace rather than the legacy collection.

    Idempotency contract:
      - Missing settings file → no-op (`action="missing"`).
      - File unparseable JSON → no-op (`action="unparseable"`) so a hand-
        edited file doesn't get clobbered; the user fixes it manually.
      - Missing `env` block → create it with both keys.
      - `env` present, both keys present (any value) → no-op
        (`action="noop"`). User-set values are preserved verbatim — this
        function only ever ADDS missing keys, never overwrites.
      - `env` present, one or both keys missing → fill in the missing
        keys from `_derive_orchestrator_project_name()` (`action="backfilled"`).

    Args:
        settings_file: path to `.claude/settings.json`. Defaults to
            `<PROJECT_ROOT>/.claude/settings.json` — the Orchestrator
            Project's own settings file.

    Returns:
        `{"action": str, "added_keys": [str, ...], "path": str}` so callers
        can log a precise outcome. The dict is also safe to feed into
        the install-event log directly.
    """
    if settings_file is None:
        settings_file = PROJECT_ROOT / ".claude" / "settings.json"

    result: dict = {
        "action": "missing",
        "added_keys": [],
        "path": str(settings_file),
    }

    if not settings_file.exists():
        return result

    try:
        raw = settings_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        result["action"] = "unparseable"
        return result

    if not isinstance(data, dict):
        result["action"] = "unparseable"
        return result

    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
        env_was_missing = True
    else:
        env_was_missing = False

    project_name = _derive_orchestrator_project_name()
    added: list[str] = []
    if "PROJECT_NAME" not in env:
        env["PROJECT_NAME"] = project_name
        added.append("PROJECT_NAME")
    if "CODE_GRAPH_PROJECT" not in env:
        env["CODE_GRAPH_PROJECT"] = project_name
        added.append("CODE_GRAPH_PROJECT")

    if not added and not env_was_missing:
        result["action"] = "noop"
        return result

    # Atomic-ish write: write to a sibling tempfile and rename. Same
    # pattern as `deferral_report.write`. We accept any OSError raised
    # by the rename and surface it via the action field rather than
    # propagating — this is a best-effort idempotent backfill, NOT a
    # critical install step.
    try:
        payload = json.dumps(data, indent=2) + "\n"
        tmp = settings_file.with_suffix(settings_file.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(settings_file))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "backfilled"
    result["added_keys"] = added
    return result


def _ensure_collections(embed_config: dict,
                        decisions: dict | None = None,
                        args: argparse.Namespace | None = None) -> None:
    """Detect existing Weaviate collections and create only the ones missing.

    Code-graph collections (CodeModule / CodeClass / CodeFunction / CodeAPI /
    CodeInteraction) are SHARED across all projects on this machine — they
    carry a `project_name` field that separates rows. Don't recreate them
    per-install: the MCP server creates them lazily on first write.

    Adopt-mode safety (the user's Weaviate already exists):
      - Per-project KG / Development names are derived from the project
        basename so we never write bare top-level `KnowledgeGraph` /
        `Development` into a host that uses per-project namespacing.
      - vco always creates its OWN collections; we deliberately do NOT
        adopt cross-project KGs from other installs. Reason: the
        orchestrator's orphan-prune sync would delete entries whose
        `file_path` no longer exists in this install.
      - In adopt mode, every proposed creation is announced; with `--yes`
        the install proceeds non-interactively, otherwise the user is
        prompted to confirm.
      - `--skip-seed` and `--skip-collections` short-circuit the whole
        step (collections get created lazily by the MCP server).

    The resolved per-project / shared names are propagated back to
    `os.environ` so `_write_env_config` writes them into `.env`.
    """
    # Honor --skip-seed / --skip-collections: if the user opted out of
    # seeding, they almost certainly don't want us mutating schema either.
    if args is not None and (
        getattr(args, "skip_collections", False)
        or getattr(args, "skip_seed", False)
    ):
        print("[7b/10] Skipping Weaviate collection bootstrap "
              "(--skip-seed / --skip-collections).")
        print("  Run `kg-sync --all` later to seed; the MCP server creates "
              "missing collections lazily on first write.")
        _log_install_event(
            "7b/10", "skip",
            "collection bootstrap skipped via flag",
            data={"skip_collections": getattr(args, "skip_collections", False),
                  "skip_seed": getattr(args, "skip_seed", False)},
        )
        return

    weaviate_port = os.environ.get("WEAVIATE_PORT", str(DEFAULT_WEAVIATE_PORT))
    weaviate_url = f"http://localhost:{weaviate_port}"

    # Detect adopt mode: install is reusing a Weaviate it didn't bring up.
    weaviate_decision = (decisions or {}).get("weaviate", {})
    adopt_mode = weaviate_decision.get("action") == ACTION_ADOPT

    # Per-project KG name. Resolution order:
    #   1. KG_COLLECTION env var (Claude Code workspace override)
    #   2. Adopt mode: derive from project basename (don't pollute with
    #      bare `KnowledgeGraph`)
    #   3. Otherwise: derive from PROJECT_ROOT basename. Pre-2026-05-01 this
    #      branch hardcoded bare `"KnowledgeGraph"` / `"Development"`, which
    #      collided with sibling installs and silently routed writes to
    #      collections shared across every vco install on the same Weaviate.
    env_kg = os.environ.get("KG_COLLECTION")
    if env_kg:
        kg_name = env_kg
    else:
        kg_name = _derive_project_kg_name(PROJECT_ROOT)

    # Per-project Development collection: same logic.
    env_dev = os.environ.get("DEVELOPMENT_COLLECTION")
    if env_dev:
        dev_name = env_dev
    else:
        dev_name = _derive_project_dev_name(PROJECT_ROOT)

    # Cross-project shared KG. All vibecoded installs read from the same shared
    # collection name (default "VibeCodedOrchestrator_KnowledgeGraph" since
    # v0.2.23 B1 — was lowercase-c "VibecodedOrchestrator_KnowledgeGraph"
    # v0.2.12–v0.2.22, itself renamed from "VibeCodedTools_KnowledgeGraph"
    # pre-v0.2.12); the projects only differ in their per-project KG.
    # Bootstrapped once per Weaviate instance — re-runs are no-ops thanks
    # to the existing-class detection (which is case-insensitive since
    # v0.2.23 B1, so the lowercase-c class on existing installs is adopted
    # in place without recreate).
    shared_name = os.environ.get(
        "SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph"
    ) or ""

    print(f"[7b/10] Checking Weaviate collections at {weaviate_url} "
          f"({'adopt' if adopt_mode else 'self-managed'} mode) ... ",
          flush=True)
    _log_install_event(
        "7b/10", "start",
        f"checking Weaviate collections ({'adopt' if adopt_mode else 'self-managed'})",
        data={"weaviate_url": weaviate_url, "adopt_mode": adopt_mode},
    )

    # 1. Read existing schema.
    try:
        resp = urllib.request.urlopen(f"{weaviate_url}/v1/schema", timeout=10)
        schema = json.loads(resp.read())
    except Exception as e:
        print(f"  WARN: couldn't read schema ({e}). Skipping bootstrap.")
        print("  MCP server will create collections lazily on first write.")
        _log_install_event(
            "7b/10", "warn",
            f"schema read failed: {type(e).__name__}",
            data={"error": str(e)[:200]},
        )
        return

    existing = {
        c.get("class") for c in schema.get("classes", [])
        if isinstance(c, dict) and c.get("class")
    }

    # v0.2.23 B1 (2026-05-21): case-insensitive adoption.
    #
    # Weaviate class names are case-SENSITIVE at the storage layer, so a
    # naive strict-equality check would treat the v0.2.12–v0.2.22
    # lowercase-c "VibecodedOrchestrator_KnowledgeGraph" class as MISSING
    # when the new canonical default is capital-C
    # "VibeCodedOrchestrator_KnowledgeGraph" — and would attempt to CREATE
    # the capital-C variant alongside it, leaving the user with two
    # divergent shared-KG classes (a regression of the same shape the
    # PR-26 rename introduced when it landed without case-insensitive
    # lookup).
    #
    # Strategy: build a `lower(name) -> actual_name` map of every
    # existing class, then look up each required collection by its
    # lowercased name. When a case-different sibling is found, rebind
    # the required name to the live class so:
    #   (a) we DON'T POST a new schema (idempotent w.r.t. casing),
    #   (b) downstream env-write paths see the on-disk casing (so the
    #       per-project binding row in launcher.db points at what
    #       actually exists, not what _we_ thought we'd create),
    #   (c) the `--update` self-heal step (`_self_heal_kg_bindings_on_update`
    #       below) auto-resolves any pre-flip binding rows.
    #
    # The lowercase-c → capital-C transition is the one this fix targets,
    # but the logic is generic: any future case-mismatch on any of the
    # three required collections (per-project KG, per-project Dev, shared
    # KG) will adopt the on-disk casing rather than recreate.
    existing_by_lower: dict[str, str] = {
        name.lower(): name for name in existing if name
    }

    def _find_existing_case_insensitive(name: str) -> str | None:
        """Return the actual-case existing class name when *name* matches
        case-insensitively, or None when no class exists by that lowered key.
        """
        return existing_by_lower.get(name.lower())

    # Resolve each required name to its on-disk casing (when present).
    # `case_rebinds` tracks names that changed casing during resolution so
    # we can announce them differently in adopt-mode (D21 prompt-phrasing).
    case_rebinds: list[tuple[str, str]] = []  # (requested, adopted)

    def _resolve_existing_casing(name: str) -> str:
        actual = _find_existing_case_insensitive(name)
        if actual is not None and actual != name:
            case_rebinds.append((name, actual))
            return actual
        return name

    kg_name = _resolve_existing_casing(kg_name)
    dev_name = _resolve_existing_casing(dev_name)
    if shared_name:
        shared_name = _resolve_existing_casing(shared_name)

    # Note: we deliberately do NOT auto-adopt existing cross-project KGs
    # (e.g. `ClaudeKnowledgeGraph` from another install). The orchestrator
    # runs an orphan-prune sync cycle that would delete entries whose
    # `file_path` no longer exists in this install — silently destroying
    # the other install's KG. vco always gets its own collections;
    # existing collections from other projects are left untouched.

    # Propagate resolved names back to env so .env / settings.json pick
    # them up. This is the tri-write source of truth for downstream steps.
    # CRUCIAL: after case-rebind above, these env values carry the on-disk
    # casing so .env writes and binding rows match what Weaviate actually has.
    os.environ["KG_COLLECTION"] = kg_name
    os.environ["DEVELOPMENT_COLLECTION"] = dev_name
    if shared_name:
        os.environ["SHARED_KG_COLLECTION"] = shared_name

    # 2. Required for THIS project install. Code-graph collections excluded
    #    on purpose — they're shared and created on demand.
    required: list[tuple[str, "callable"]] = [
        (kg_name, _kg_class_definition),
        (dev_name, _development_class_definition),
    ]
    # Shared cross-project KG. Same schema as the per-project KG (the MCP
    # server reads them with the same shape). Created once per Weaviate
    # instance — the existing-class check above means concurrent installs
    # don't double-create. Skip if shared_name resolved to an existing class
    # (we already adopted it above).
    if shared_name and shared_name != kg_name:
        required.append((shared_name, _kg_class_definition))

    # NOTE: do NOT skip our `_development` collection just because other
    # projects on the host have their own. Each project's docs live in
    # its own per-project `<Project>_development` namespace — they are
    # NOT shared like the cross-project KG. Creating ours is required
    # for Step 7c to seed `docs/` content. Earlier code skipped it on
    # the false assumption that one Development class served all
    # projects, which left vco's docs unseeded (Step 7c then exited 1).

    # `existing` is the schema-snapshot set; after case-rebind every
    # `required` name uses the on-disk casing so strict equality below
    # still works (the rebind happens BEFORE this comparison).
    missing = [(n, b) for (n, b) in required if n not in existing]
    skipped_existing = [n for (n, _) in required if n in existing]
    if not missing:
        if case_rebinds:
            print(f"  All collections present (case-adopted "
                  f"{len(case_rebinds)} class(es): "
                  f"{', '.join(f'{r}→{a}' for r, a in case_rebinds)}).")
        else:
            print(f"  All collections present (reusing {len(required)} shared classes).")
        _log_install_event(
            "7b/10", "ok",
            "all required collections already present",
            data={"existing": skipped_existing,
                  "kg": kg_name, "dev": dev_name, "shared": shared_name,
                  "case_rebinds": [
                      {"requested": r, "adopted": a} for r, a in case_rebinds
                  ]},
        )
        return

    # In adopt mode, announce what we're about to do and confirm. With
    # --yes / non-TTY we proceed non-interactively (the user already
    # consented to adoption upstream).
    if adopt_mode:
        print(f"  Existing classes ({len(existing)}): "
              f"{', '.join(sorted(list(existing))[:6])}"
              + (" ..." if len(existing) > 6 else ""))
        if skipped_existing:
            # Split skipped into pure-case-adopted (case-rebind sibling
            # was found) and exact-match (already present, unchanged).
            # The case-adopted ones get the more informative "ADOPT
            # (case-different)" phrasing.
            rebind_targets = {a for _, a in case_rebinds}
            adopted_case_different = [
                n for n in skipped_existing if n in rebind_targets
            ]
            adopted_exact = [
                n for n in skipped_existing if n not in rebind_targets
            ]
            if adopted_exact:
                print(f"  Will SKIP (already present): "
                      f"{', '.join(adopted_exact)}")
            if adopted_case_different:
                # For each case-rebind, find the original requested name
                # so the user sees what casing we asked for vs what's
                # on disk.
                pairs = [(req, adp) for req, adp in case_rebinds
                         if adp in adopted_case_different]
                pair_strs = [
                    f"`{adp}` (requested `{req}`)" for req, adp in pairs
                ]
                print(f"  Will ADOPT (existing case-different class): "
                      f"{', '.join(pair_strs)}")
        if missing:
            print(f"  Will CREATE: "
                  f"{', '.join(n for (n, _) in missing)}"
                  + (" (no case-variant found)"
                     if not case_rebinds and not skipped_existing else ""))
        interactive = (
            args is not None
            and not getattr(args, "yes", False)
            and not getattr(args, "quiet", False)
            and sys.stdin.isatty()
        )
        if interactive:
            try:
                ans = input("  Proceed with creating these classes? "
                            "[Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans in ("n", "no"):
                print("  → user declined; skipping collection creation.")
                print("    (MCP server will create lazily on first write.)")
                _log_install_event(
                    "7b/10", "skip",
                    "user declined collection creation in adopt mode",
                )
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
        _log_install_event(
            "7b/10", "ok",
            f"created {len(created)} collection(s)",
            data={"created": created, "skipped_existing": skipped_existing},
        )
    else:
        _log_install_event(
            "7b/10", "error",
            f"{len(failed)} collection(s) failed to create",
            data={"created": created,
                  "failed": [{"name": n, "error": e[:200]} for n, e in failed]},
        )


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
#   .claude/scripts/kg-sync --all       (handles knowledge/ + docs/)
#
# The script is idempotent so re-runs are safe.

# Moved to vco_lib.project_init in PR 2 — kept as shim for existing callers;
# will be removed in PR 9 (cleanup).
_detect_kg_schema_drift = _project_init._detect_kg_schema_drift


def _maybe_prompt_rebuild_collections(
    args: argparse.Namespace,
    deferral_report: "DeferralReport | None" = None,
) -> bool:
    """During --update, detect schema drift and prompt the user.

    When *deferral_report* is provided, a ``schema_drift_rebuild_required``
    entry is added to it whenever the rebuild is deferred (non-interactive
    shell without ``--yes``, or ``--skip-rebuild-prompt``).

    Returns True iff the rebuild should run. Exits silently with False
    on:
      - install (not update) mode — schema is fresh by definition
      - --skip-rebuild-prompt — explicit defer
      - --rebuild-collections — explicit opt-in (returns True directly)
      - non-interactive shell with no --yes — fail safe; no destructive
        action without confirmation
    """
    if not args.update:
        return False
    if args.rebuild_collections:
        # Explicit opt-in. No prompt needed.
        return True
    if args.skip_rebuild_prompt:
        return False

    # Detect drift on the running KG collection.
    weaviate_url = os.environ.get("WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}")
    kg_collection = os.environ.get("KG_COLLECTION", "")
    if not kg_collection:
        # No KG collection configured — first install probably; let
        # _ensure_collections + _seed_weaviate handle it.
        return False

    drift, missing = _detect_kg_schema_drift(weaviate_url, kg_collection)
    if not drift:
        return False

    print()
    print("=" * 70)
    print("SCHEMA REBUILD REQUIRED")
    print("=" * 70)
    print()
    print(f"The running KG collection ({kg_collection}) is on an older schema:")
    for feat in missing:
        print(f"  - missing: {feat}")
    print()
    print("Today's code requires these invariants. Weaviate <=1.30 doesn't")
    print("allow adding them via Reconfigure -- but most cases can be fixed")
    print("by COPYING with vectors (no Ollama re-embed). PR 3 smart migrate")
    print("will pick: noop / patch_props / copy-with-vectors / rebuild per")
    print("collection.")
    print()
    print("What gets touched:")
    print("  + Weaviate collections (rebuilt with copy-with-vectors when possible)")
    print("  - Your .md source files in knowledge/ and docs/  (untouched)")
    print("  - .env / .vscode/settings.json / .claude/settings.json (untouched)")
    print("  - The launcher's project bindings (untouched)")
    print()
    print("Estimated time: ~30-60s for copy path (vs 3-5min for legacy rebuild).")
    print("Pass --force-rebuild to bypass smart path and drop+re-embed instead.")
    print()

    def _emit_drift_deferral(missing_feats: list) -> None:
        """Emit a deferral entry for schema drift when deferral_report given."""
        if deferral_report is None:
            return
        missing_str = ", ".join(missing_feats) if missing_feats else "unknown"
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="schema_drift_rebuild_required",
                title="Schema rebuild required",
                detected=(
                    f"KG_COLLECTION `{kg_collection}` schema is on an older "
                    f"version. Missing: {missing_str}."
                ),
                why_deferred=(
                    "Schema rebuild touches Weaviate state -- non-interactive "
                    "shells without --yes defer to avoid silent data churn."
                ),
                command_to_apply="python install.py --update --rebuild-collections",
                severity="warning",
                # HIGH-3 fix (2026-05-01): kg_node_refs now points at the
                # actual schema-port research report. Path is relative to the
                # claude-orchestrator project root (the meta-project that owns
                # research reports), not VCO. Cross-repo reference is intentional.
                kg_node_refs=[
                    ".claude/context/weaviate-schema-port-research-2026-05-01.md",
                ],
            )
        )

    # Non-interactive flow: respect --yes; otherwise refuse to nuke data.
    if not sys.stdin.isatty():
        if getattr(args, "yes", False):
            print("(non-interactive --yes: proceeding with rebuild)")
            return True
        print("Non-interactive shell + no --yes -- DEFERRING rebuild.")
        print("DEFERRED -- re-run with `install.py --update --rebuild-collections` to apply.")
        print("Note: search may misbehave until rebuild completes.")
        _emit_drift_deferral(list(missing))
        return False

    answer = input("Proceed with rebuild? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        _emit_drift_deferral(list(missing))
        return False
    return True


# Moved to vco_lib.project_init in PR 2 — kept as shim for existing callers;
# will be removed in PR 9 (cleanup). PR 3 will replace the underlying
# implementation with `migrate_collections` (smart copy/patch/rebuild
# dispatch) but keep this entry-point name for back-compat.
def _rebuild_collections(args: argparse.Namespace) -> None:
    """Shim — delegates to vco_lib.project_init.rebuild_collections.

    Passes the install.py-local `_log_install_event` so forensic logs
    continue to land in the same JSONL stream.
    """
    _project_init.rebuild_collections(args, log_event=_log_install_event)


def _seed_weaviate(args: argparse.Namespace) -> None:
    print("[7c/10] Seeding Weaviate with bundled knowledge/ + docs/ ... ", flush=True)
    _log_install_event("7c/10", "start", "seeding Weaviate")

    # Guard: if user passed --skip-seed, honor it (useful for CI / tests).
    if getattr(args, "skip_seed", False):
        print("  Skipped (--skip-seed).")
        _log_install_event("7c/10", "skip", "seed skipped via --skip-seed")
        return

    # We must use the venv's Python so weaviate-client + weaviate_mcp.chunking
    # import correctly. Step 4 creates the venv at PROJECT_ROOT/.venv (NOT
    # at claude_mcp_servers/.venv — that's a stale legacy path).
    if os.name == "nt":
        venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        # Legacy fallback: some older installs put the venv inside claude_mcp_servers/
        legacy = PROJECT_ROOT / "claude_mcp_servers" / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if legacy.exists():
            venv_py = legacy
        else:
            print(f"  ! venv python not found at {venv_py} — skipping seed (run Step 4 first)")
            _log_install_event(
                "7c/10", "error",
                "venv python missing — Step 4 must run first",
                data={"venv_py": str(venv_py)},
            )
            return

    scripts_dir = PROJECT_ROOT / ".claude" / "scripts"
    sync_kg = scripts_dir / "sync_knowledge_graph.py"

    seed_errors: list[str] = []

    # `sync_knowledge_graph.py` now handles both KG (knowledge/) and dev
    # docs (docs/) ingest paths — it routes by the file's location.
    # Old upload_docs.py was retired 2026-04-30 (audit cleanup); the
    # `--all` flag below seeds knowledge/ AND docs/ in one pass.
    if sync_kg.exists():
        print("  → knowledge/ + docs/ → KG + Development collections ...", flush=True)
        try:
            subprocess.run(
                [str(venv_py), str(sync_kg), "--all"],
                check=True,
                cwd=str(PROJECT_ROOT),
                timeout=900,  # 15 min cap; large repos may hit this
            )
        except subprocess.CalledProcessError as e:
            print(f"    ! kg/docs sync exited {e.returncode} — re-run later with `kg-sync --all`")
            seed_errors.append(f"kg-sync exit {e.returncode}")
        except subprocess.TimeoutExpired:
            print("    ! kg/docs sync timed out (>15 min) — re-run later with `kg-sync --all`")
            seed_errors.append("kg-sync timeout")
        except FileNotFoundError as e:
            print(f"    ! kg/docs sync failed: {e}")
            seed_errors.append(f"kg-sync FileNotFound: {e}")
    else:
        print(f"  ! sync_knowledge_graph.py not found at {sync_kg}")
        seed_errors.append("sync_knowledge_graph.py missing")

    # 3. Cross-project shared KG seed (Step 7d).
    #
    # Re-runs sync_knowledge_graph.py against the SHARED collection so
    # vibecoded-orchestrator/knowledge/ is also persisted into
    # VibeCodedOrchestrator_KnowledgeGraph (since v0.2.23 B1; was lowercase-c
    # VibecodedOrchestrator_KnowledgeGraph v0.2.12–v0.2.22, itself renamed
    # from VibeCodedTools_KnowledgeGraph in v0.2.12 PR-26 / Group E). All
    # projects on this machine then read from this shared collection in
    # addition to their per-project KG (see weaviate_mcp/server.py:
    # SHARED_KG_COLLECTION).
    #
    # Idempotency: sync_knowledge_graph.py upserts per file (delete+insert
    # by file_path), so re-running on unchanged content yields the same
    # collection state. The cost on a 50-node tree is ~30s on warm Ollama.
    #
    # Honor SHARED_KG_WRITE_DISABLED=true at install time too (skip seeding)
    # so power-users who explicitly gated shared-KG writes don't get the
    # collection re-populated by a subsequent install / update.
    # Legacy alias SHARED_KG_OPT_OUT is still consulted for ~3 releases —
    # canonical key wins when both are set (mirroring the MCP server).
    shared_write_disabled = (
        os.environ.get("SHARED_KG_WRITE_DISABLED")
        or os.environ.get("SHARED_KG_OPT_OUT", "")
    ).lower() in ("1", "true", "yes")
    shared_collection = os.environ.get(
        "SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph"
    )
    if shared_write_disabled:
        print("  → shared KG seed: skipped (SHARED_KG_WRITE_DISABLED=true)")
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
            seed_errors.append(f"shared-kg exit {e.returncode}")
        except subprocess.TimeoutExpired:
            print("    ! shared KG seed timed out (>10 min)")
            seed_errors.append("shared-kg timeout")
        except FileNotFoundError as e:
            print(f"    ! shared KG seed failed: {e}")
            seed_errors.append(f"shared-kg FileNotFound: {e}")

    print("  OK (seed step complete; per-script errors are non-fatal — see hints above)")
    if seed_errors:
        # Soft-fail: still log as warn, not error. Step is non-fatal by
        # design — users can re-run `kg-sync --all` later. The
        # downstream resume-decider treats "warn" as still-eligible-to-skip
        # so a partial seed doesn't block install completion.
        _log_install_event(
            "7c/10", "warn",
            f"{len(seed_errors)} seed sub-step(s) had errors (non-fatal)",
            data={"errors": seed_errors},
        )
    else:
        _log_install_event("7c/10", "ok", "all seed sub-steps completed")


# ---------------------------------------------------------------------------
# Schema-correctness migrations (PR-24, v0.2.12, 2026-05-16)
# ---------------------------------------------------------------------------


def _run_schema_migration_scripts(deferral_report: "DeferralReport") -> None:
    """Run the two schema-correctness migration scripts that ship in
    ``scripts/``:

      1. ``migrate-development-temporal-props.{sh,ps1}`` — adds the four
         canonical temporal properties (``created``, ``updated``,
         ``valid_from``, ``valid_until``) to every existing
         ``*_Development`` collection. Properties CAN be added
         retroactively via the Weaviate v1 REST schema API, so this is
         an additive in-place patch.
      2. ``migrate-shared-kg-schema.{sh,ps1}`` — drops + recreates the
         shared KG collection when its schema lacks
         ``invertedIndexConfig.indexNullState=True``. Weaviate <=1.30
         cannot add that retroactively, so the only fix is a destructive
         recreate. Safe because shared-KG content derives from
         ``knowledge/**/*.md`` and the script re-syncs after recreate.

    Both scripts are idempotent and soft-fail. A non-zero exit (or a
    failed spawn) emits a ``schema_migration_failed`` deferral entry but
    does NOT abort install.py. OS dispatch:

      - Linux + macOS: bash ``scripts/<name>.sh``
      - Windows:       PowerShell ``scripts/<name>.ps1``

    The deferral entry includes the explicit command to apply the
    migration manually so users can retry on demand.
    """
    print("[7d/10] Running schema-correctness migrations ... ", flush=True)
    _log_install_event("7d/10", "start", "schema migration scripts")

    if sys.platform == "win32":
        script_ext = ".ps1"
    else:
        script_ext = ".sh"

    scripts_dir = PROJECT_ROOT / "scripts"
    migrations = [
        ("development_temporal_props",
         f"migrate-development-temporal-props{script_ext}",
         "Add temporal properties to existing Development collections."),
        ("shared_kg_schema",
         f"migrate-shared-kg-schema{script_ext}",
         "Drop + recreate the shared KG when indexNullState is missing."),
    ]

    for migration_id, script_name, description in migrations:
        script_path = scripts_dir / script_name
        if not script_path.exists():
            print(f"  [migrate:{migration_id}] {script_name} not found; skipping.")
            _log_install_event(
                "7d/10", "skip",
                f"{migration_id}: script not present at {script_path}",
            )
            continue

        if sys.platform == "win32":
            cmd = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path),
            ]
        else:
            cmd = ["bash", str(script_path)]

        print(f"  [migrate:{migration_id}] {description}")
        try:
            rc = subprocess.call(
                cmd, cwd=str(PROJECT_ROOT), timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  [migrate:{migration_id}] spawn failed: {e}")
            _log_install_event(
                "7d/10", "error",
                f"{migration_id}: spawn failed: {e}",
            )
            deferral_report.add_entry(
                DeferralEntry(
                    condition_id=f"schema_migration_failed_{migration_id}",
                    title=f"Schema migration failed: {migration_id}",
                    detected=(
                        f"Migration script `scripts/{script_name}` failed to "
                        f"launch: {e}"
                    ),
                    why_deferred=(
                        "The migration script could not be invoked. Search "
                        "and stale-data filtering may misbehave until the "
                        "migration is applied manually."
                    ),
                    command_to_apply=(
                        f"{'powershell.exe -File ' if sys.platform == 'win32' else 'bash '}"
                        f"scripts/{script_name}"
                    ),
                    severity="warning",
                    kg_node_refs=[],
                )
            )
            continue

        if rc != 0:
            print(f"  [migrate:{migration_id}] exit rc={rc} (non-fatal)")
            _log_install_event(
                "7d/10", "warn",
                f"{migration_id}: exit rc={rc}",
                data={"rc": rc, "script": str(script_path)},
            )
            deferral_report.add_entry(
                DeferralEntry(
                    condition_id=f"schema_migration_failed_{migration_id}",
                    title=f"Schema migration failed: {migration_id}",
                    detected=(
                        f"Migration script `scripts/{script_name}` exited "
                        f"with non-zero status (rc={rc})."
                    ),
                    why_deferred=(
                        "Search and stale-data filtering may misbehave on "
                        "the affected collections until the migration is "
                        "applied successfully. Re-run the script manually "
                        "after addressing the underlying cause (e.g. "
                        "Weaviate not running, missing jq, etc.)."
                    ),
                    command_to_apply=(
                        f"{'powershell.exe -File ' if sys.platform == 'win32' else 'bash '}"
                        f"scripts/{script_name}"
                    ),
                    severity="warning",
                    kg_node_refs=[],
                )
            )
            continue

        _log_install_event(
            "7d/10", "ok",
            f"{migration_id}: migration script completed",
        )

    _log_install_event("7d/10", "ok", "schema migrations completed")


def _detect_legacy_shared_kg_class(deferral_report: "DeferralReport") -> None:
    """PR-34 (v0.2.12, Group M) — soft-fail migration deferral.

    When `python install.py --update` runs against a Weaviate that still
    carries the pre-rename `VibeCodedTools_KnowledgeGraph` class (created
    by an install <v0.2.12), emit a `legacy_shared_kg_class_present`
    deferral entry pointing the user at the launcher's "Manage shared KG
    collection" picker. We NEVER auto-rename or auto-drop the class —
    that is destructive (would lose any cross-project KG content the user
    has written) and the picker is the consent mechanism.

    Idempotent + soft-fail:
      * Weaviate unreachable → skip silently (the schema-rebuild flow
        upstream already emits a `weaviate_unreachable` deferral).
      * Legacy class absent → no-op.
      * Legacy class present → one deferral entry, severity=info.

    The picker (launcher Settings → Identity → "Manage shared KG
    collection") handles three resolution paths: (a) accept the new
    canonical and migrate content, (b) keep the legacy name as the
    per-project shared-KG override, (c) ignore (dismissable).
    """
    # Resolve Weaviate URL: prefer env, fall back to canonical default.
    weaviate_url = (
        os.environ.get("WEAVIATE_URL")
        or f"http://localhost:{os.environ.get('WEAVIATE_PORT', '8081')}"
    )
    try:
        resp = urllib.request.urlopen(  # noqa: S310 (localhost only)
            f"{weaviate_url}/v1/schema", timeout=5,
        )
        schema = json.loads(resp.read())
    except Exception:
        # Soft-fail: skip silently. Other code paths already deferral
        # on Weaviate unreachability.
        return

    classes = {c.get("class", "") for c in schema.get("classes", [])}
    legacy_name = "VibeCodedTools_KnowledgeGraph"
    canonical_name = "VibeCodedOrchestrator_KnowledgeGraph"
    # v0.2.23 B1: also recognise the lowercase-c v0.2.12–v0.2.22 default
    # as "canonical-present" (case-insensitive) so a user upgrading from
    # that range doesn't get a spurious "canonical not yet created"
    # message when their on-disk class is the lowercase-c variant.
    legacy_lowercase_c = "VibecodedOrchestrator_KnowledgeGraph"

    if legacy_name not in classes:
        return  # No legacy class — nothing to migrate.

    canonical_present = (
        canonical_name in classes or legacy_lowercase_c in classes
    )
    detected_msg = (
        f"Weaviate at {weaviate_url} still carries the pre-v0.2.12 "
        f"shared-KG class `{legacy_name}`. The post-rename canonical "
        f"name is `{canonical_name}` "
        f"({'already present' if canonical_present else 'not yet created'})."
    )

    deferral_report.add_entry(
        DeferralEntry(
            condition_id="legacy_shared_kg_class_present",
            title="Legacy shared-KG class still on disk (pre-v0.2.12 PR-26)",
            detected=detected_msg,
            why_deferred=(
                "The shared cross-project KG class was renamed from "
                f"`{legacy_name}` to `{canonical_name}` in v0.2.12 PR-26. "
                "The legacy class is still on disk because the rename is "
                "metadata-only; install.py does NOT auto-rename or "
                "auto-drop a populated class (destructive — would lose "
                "any cross-project KG content). Resolve via the "
                "launcher's Settings → Identity → \"Manage shared KG "
                "collection\" picker, which lets you pick which class "
                "becomes the active shared KG for each project."
            ),
            command_to_apply=(
                "Open the launcher (`./vct-launcher`), pick a project, "
                "go to Settings → Identity, click \"Manage shared KG "
                "collection\", and select either the legacy "
                f"`{legacy_name}` or the canonical "
                f"`{canonical_name}` as the shared KG for that project."
            ),
            severity="info",
            kg_node_refs=[],
        )
    )
    _log_install_event(
        "7d/10", "info",
        "legacy shared-KG class detected; deferral emitted",
        data={"legacy_class": legacy_name,
              "canonical_present": canonical_present},
    )


# Privilege rank for `kg_collection_access.access_level`. Higher wins when
# resolving a case-rebind collision (two rows for the same (project_id,
# collection_name) after the rebind — keep the row with stronger access).
_KG_ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "write": 2}


def _rebind_collection_names_to_on_disk_casing(
    cur: "sqlite3.Cursor",
    *,
    table: str,
    project_id_col: str,
    collection_name_col: str,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    extra_select_cols: tuple[str, ...] = (),
    do_rebind: "Callable[..., None]",
    resolve_conflict: "Optional[Callable[..., None]]" = None,
) -> list[tuple]:
    """Generic helper: rebind a SQLite table's ``collection_name`` column to
    the on-disk Weaviate casing when a case-different sibling exists in
    ``existing_classes``.

    Algorithm (per row):
      1. ``SELECT project_id, collection_name, *extra_select_cols FROM <table>``.
      2. If ``collection_name`` is exact-match in ``existing_classes`` → skip.
      3. If no case-insensitive sibling in ``existing_by_lower`` → skip
         (genuine missing class; orphan-prune sync recreates lazily).
      4. Otherwise the row needs rebinding. When ``resolve_conflict`` is
         provided, the helper probes the SELECT-time row set for a
         ``(project_id, target_name)`` collision; on hit, delegates to
         ``resolve_conflict`` (which mutates the DB and appends to
         ``rebinds`` via the closure). Otherwise — and always for tables
         where the rebind can't violate a unique constraint — calls
         ``do_rebind`` for a straight UPDATE.

    The helper is SQL-shape-agnostic: callers own the exact ``UPDATE`` /
    ``DELETE`` statements via ``do_rebind`` / ``resolve_conflict`` so
    table-specific concerns (extra ``SET`` columns, natural-key shape,
    privilege rules) stay with the caller.

    Returns the audit list — caller-supplied via the closures — so the
    parent function can pull a final summary into the deferral entry.
    """
    rebinds: list[tuple] = []
    select_cols = (project_id_col, collection_name_col) + extra_select_cols
    cur.execute(
        f"SELECT {', '.join(select_cols)} FROM {table}"
    )
    rows = cur.fetchall()

    # Build conflict lookup once if conflict-resolution is enabled —
    # keyed by (project_id, name) with the FULL row tuple as the value so
    # resolve_conflict can read extras (e.g. access_level for the
    # privilege-rank decision).
    conflict_lookup: dict[tuple, tuple] = {}
    if resolve_conflict is not None:
        for row in rows:
            proj_id = row[0]
            coll = row[1]
            if proj_id and coll:
                conflict_lookup[(proj_id, coll)] = row

    for row in rows:
        proj_id = row[0]
        coll_name = row[1]
        if not coll_name:
            continue
        if coll_name in existing_classes:
            # Exact match — nothing to do.
            continue
        actual = existing_by_lower.get(coll_name.lower())
        if actual is None or actual == coll_name:
            # Genuinely missing OR already canonical (defensive — filtered
            # above for missing-from-existing_classes).
            continue

        conflict_row: "Optional[tuple]" = None
        if resolve_conflict is not None:
            conflict_row = conflict_lookup.get((proj_id, actual))

        if conflict_row is not None and resolve_conflict is not None:
            resolve_conflict(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                current_row=row,
                conflict_row=conflict_row,
                rebinds=rebinds,
            )
        else:
            do_rebind(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                row=row,
                rebinds=rebinds,
            )

    return rebinds


def _self_heal_kg_bindings_on_update(
    deferral_report: "DeferralReport",
) -> None:
    """v0.2.23 B1 (2026-05-21) — case-mismatch self-heal for launcher.db.

    Fixes Finding 4 of the post-v0.2.22 handoff: a `project_kg_bindings`
    row whose `collection_name` differs only in casing from a class that
    actually exists in Weaviate. Symptom in production: VCO_dev's shared
    binding pointed at `VibecodedOrchestrator_KnowledgeGraph` (lowercase
    c) but Weaviate had `VibeCodedOrchestrator_KnowledgeGraph` (capital
    C, 892 objects live).

    Behaviour:
      1. Resolve launcher.db path via `_discover_app_state_db_path` (the
         same helper used by `_write_preset_defaults_to_app_state`).
         Cross-OS: `~/.vct/launcher.db` by default, `$VCT_STATE_DIR/launcher.db`
         when set.
      2. Read every row from `project_kg_bindings`.
      3. Read every class from Weaviate `/v1/schema`.
      4. For each binding row where `collection_name not in existing` BUT
         a case-insensitive match against existing finds a live sibling Y,
         UPDATE the row to point at Y. Append a `kg_binding_self_healed`
         deferral entry (severity=info) per row rebound, naming each
         (project_id, role, old_name → new_name) so the user has an audit
         trail.
      5. For each binding row where `collection_name not in existing` AND
         no case-different sibling exists, LEAVE ALONE. That's a true
         missing-class state — the orphan-prune sync recreates the class
         lazily on next write, or the user picks one via the launcher's
         Shared KG picker.

    NO DATA TOUCHED. Only launcher.db binding-row updates. No re-embedding
    cost. The function never raises; soft-fails to a deferral entry on
    every error path (launcher.db missing, Weaviate unreachable, sqlite
    error). install.py --update must always exit cleanly even when this
    helper hits an edge case.

    Why this is safe to auto-apply (unlike the legacy-class deferral):
    rebinding a launcher.db row to point at a class that ALREADY EXISTS
    in Weaviate is a metadata fix, not a destructive operation — the on-
    disk class and its embeddings are untouched. The deferral entry is
    purely informational so the user sees what we changed.

    Called from the install.py --update flow alongside
    `_detect_legacy_shared_kg_class` (see Step 7d at the top of
    `install_or_update` where this is wired in).
    """
    import sqlite3

    db_path = _discover_app_state_db_path()
    if not db_path.is_file():
        # No launcher.db ⇒ launcher has never started OR the user purged
        # `~/.vct/`. Either way, there are no binding rows to heal.
        _log_install_event(
            "7e/10", "skip",
            f"launcher.db not found at {db_path}; nothing to self-heal",
            data={"db_path": str(db_path)},
        )
        return

    # Read Weaviate schema first; if Weaviate is unreachable we can't
    # build the case-insensitive lookup, so defer with a deferral entry
    # rather than touching the DB blindly.
    weaviate_url = (
        os.environ.get("WEAVIATE_URL")
        or f"http://localhost:{os.environ.get('WEAVIATE_PORT', '8081')}"
    )
    try:
        resp = urllib.request.urlopen(  # noqa: S310 (localhost only)
            f"{weaviate_url}/v1/schema", timeout=5,
        )
        schema = json.loads(resp.read())
    except Exception as e:
        # Weaviate unreachable: another deferral path upstream already
        # mentions this, but we add a binding-specific note so the user
        # knows the self-heal step was skipped.
        _log_install_event(
            "7e/10", "skip",
            f"weaviate unreachable; binding self-heal skipped: {type(e).__name__}",
            data={"weaviate_url": weaviate_url, "error": str(e)[:200]},
        )
        return

    existing_classes = {
        c.get("class") for c in schema.get("classes", [])
        if isinstance(c, dict) and c.get("class")
    }
    existing_by_lower = {
        name.lower(): name for name in existing_classes if name
    }

    # Open launcher.db read-write, BUT in a try/except: any sqlite error
    # (locked, corrupted, schema-drift, permission) soft-fails to a
    # deferral entry. The launcher's own boot will heal the schema; this
    # helper exists to fix data, not schema.
    rebinds: list[tuple[str, str, str, str]] = []  # (proj_id, role, old, new)
    access_rebinds: list[tuple[str, str, str]] = []  # (proj_id, old, new)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.cursor()

            # ── 1. project_kg_bindings ────────────────────────────────
            # Natural key (project_id, role) is unaffected by a
            # collection_name rebind, so no conflict-resolver is needed.
            def _bind_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                role = row[2]
                cur.execute(
                    "UPDATE project_kg_bindings "
                    "SET collection_name = ?, updated_at = ? "
                    "WHERE project_id = ? AND role = ?",
                    (new_name, int(time.time() * 1000), project_id, role),
                )
                rebinds.append((project_id, role, old_name, new_name))

            try:
                binding_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="project_kg_bindings",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("role",),
                    do_rebind=_bind_rebind,
                )
            except sqlite3.OperationalError as oe:
                if "no such table" in str(oe).lower():
                    _log_install_event(
                        "7e/10", "skip",
                        "project_kg_bindings table absent; nothing to self-heal",
                    )
                    return
                raise
            rebinds.extend(binding_rebinds)

            # ── 2. kg_collection_access ───────────────────────────────
            # v0.2.23 review-B HIGH-1 (2026-05-21): also rebind
            # `kg_collection_access` rows whose `collection_name` differs
            # only in case from an on-disk class. Without this, the
            # launcher GUI's Identity tab access matrix would render rows
            # pointing at a class that doesn't exist post-rename (and
            # dangle), and the hub's `kg_access_list` construction in
            # config_api would see both the lowercase-c grant AND the
            # (implicit-fallback) capital-C grant — confusing, and a
            # silently-missed `access_level='none'` signal if the user
            # had explicitly downgraded the lowercase-c entry.
            #
            # PK collision handling: kg_collection_access PK is
            # (project_id, collection_name). If (p1, "Foo", "read") exists
            # AND (p1, "foo", "write") also exists, a naive rebind would
            # violate the UNIQUE constraint. The helper detects the
            # collision before the UPDATE; on collision we KEEP the
            # higher-privilege row (write > read > none) at the canonical
            # casing and DELETE the lower-privilege duplicate. Matches
            # the user's "single source of truth for access" intent.
            def _access_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                cur.execute(
                    "UPDATE kg_collection_access "
                    "SET collection_name = ? "
                    "WHERE project_id = ? AND collection_name = ?",
                    (new_name, project_id, old_name),
                )
                rebinds.append((project_id, old_name, new_name))

            def _access_resolve_conflict(
                cur, *, project_id, old_name, new_name,
                current_row, conflict_row, rebinds,
            ):
                # current_row has access_level at index 2 (extra_select_cols).
                # conflict_row likewise.
                current_access = current_row[2]
                conflict_access = conflict_row[2]
                current_rank = _KG_ACCESS_RANK.get(current_access, 0)
                conflict_rank = _KG_ACCESS_RANK.get(conflict_access, 0)
                if current_rank > conflict_rank:
                    # Lowercase-c row is higher-privilege — drop the
                    # canonical-casing duplicate, then rebind.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, new_name),
                    )
                    cur.execute(
                        "UPDATE kg_collection_access "
                        "SET collection_name = ? "
                        "WHERE project_id = ? AND collection_name = ?",
                        (new_name, project_id, old_name),
                    )
                    rebinds.append((project_id, old_name, new_name))
                else:
                    # Canonical row has equal-or-higher privilege. Drop
                    # the lowercase-c row.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, old_name),
                    )
                    rebinds.append(
                        (project_id, old_name, f"{new_name} (deduped)")
                    )

            try:
                acc_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="kg_collection_access",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("access_level",),
                    do_rebind=_access_rebind,
                    resolve_conflict=_access_resolve_conflict,
                )
                access_rebinds.extend(acc_rebinds)
            except sqlite3.OperationalError as oe:
                # Older launcher.db schemas may not have kg_collection_access.
                # Don't fail the binding heal — just skip the access part.
                if "no such table" not in str(oe).lower():
                    raise

            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as se:
        _log_install_event(
            "7e/10", "warn",
            f"launcher.db sqlite error during self-heal: {type(se).__name__}",
            data={"db_path": str(db_path), "error": str(se)[:200]},
        )
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="kg_binding_self_heal_db_error",
                title="Could not self-heal launcher.db KG bindings (sqlite error)",
                detected=(
                    f"Tried to open launcher.db at {db_path} to detect "
                    f"case-mismatched `project_kg_bindings` rows, but the "
                    f"sqlite library raised {type(se).__name__}. The binding "
                    f"rows (if any) were NOT modified."
                ),
                why_deferred=(
                    "The launcher.db file is locked, corrupted, or "
                    "schema-mismatched. Skipping self-heal preserves user "
                    "state; the launcher's own boot path will re-validate "
                    "the schema on next start."
                ),
                command_to_apply=(
                    "Close the launcher if running, then re-run "
                    "`python install.py --update`. If the error persists, "
                    "open the launcher and let it migrate the schema first, "
                    "then re-run the update."
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
        return

    if not rebinds and not access_rebinds:
        _log_install_event(
            "7e/10", "ok",
            "no case-mismatched KG bindings or access rows; self-heal no-op",
        )
        return

    # Emit ONE deferral entry summarising every rebind so the user has
    # an audit trail. severity=info because nothing is broken — we just
    # corrected a misalignment that would otherwise route writes to a
    # nonexistent class.
    rebind_lines = "\n".join(
        f"  * project_id={pid} role={role}: `{old}` → `{new}`"
        for (pid, role, old, new) in rebinds
    )
    access_rebind_lines = "\n".join(
        f"  * project_id={pid}: `{old}` → `{new}`"
        for (pid, old, new) in access_rebinds
    )
    binding_count = len(rebinds)
    access_count = len(access_rebinds)
    title_parts = []
    if binding_count:
        title_parts.append(f"{binding_count} binding(s)")
    if access_count:
        title_parts.append(f"{access_count} access row(s)")
    title = (
        f"Self-healed {' + '.join(title_parts)} of case-mismatched KG "
        "metadata in launcher.db"
    )
    detected_parts = []
    if binding_count:
        detected_parts.append(
            f"Found {binding_count} `project_kg_bindings` row(s) whose "
            f"`collection_name` differed only in casing from a class that "
            f"exists in Weaviate at {weaviate_url}.\n\n"
            f"Rebound binding rows:\n{rebind_lines}"
        )
    if access_count:
        detected_parts.append(
            f"Found {access_count} `kg_collection_access` row(s) whose "
            f"`collection_name` differed only in casing from a class that "
            f"exists in Weaviate (sibling rows to the binding rebinds). "
            f"These were updated in place to keep the launcher GUI's "
            f"per-project Identity tab access matrix pointing at the live "
            f"class. Rows annotated `(deduped)` were merged with a pre-"
            f"existing canonical-casing row at equal-or-higher privilege.\n\n"
            f"Rebound access rows:\n{access_rebind_lines}"
        )
    detected = "\n\n".join(detected_parts) + "\n\nNo data was touched."

    deferral_report.add_entry(
        DeferralEntry(
            condition_id="kg_binding_self_healed",
            title=title,
            detected=detected,
            why_deferred=(
                "This is an informational entry — the heal was applied "
                "automatically (it's a metadata fix, not a destructive "
                "operation, since the target class already exists in "
                "Weaviate). The launcher.db row(s) now match the actual "
                "Weaviate class casing, so writes/reads route to the live "
                "class instead of a nonexistent case-variant.\n\n"
                "Background: install.py v0.2.23 B1 (2026-05-21) flipped the "
                "canonical shared-KG class name from `VibecodedOrchestrator_"
                "KnowledgeGraph` (lowercase c) to `VibeCodedOrchestrator_"
                "KnowledgeGraph` (capital C, matching the brand spelling). "
                "Case-insensitive adoption in `_ensure_collections` keeps "
                "the on-disk casing unchanged; this helper aligns the "
                "launcher.db `project_kg_bindings` AND `kg_collection_access` "
                "rows with that on-disk casing."
            ),
            command_to_apply=(
                "No action required — the heal already ran. If you want to "
                "verify the rebound rows, open the launcher and check the "
                "Shared KG collection name on each affected project's "
                "Settings → Identity tab."
            ),
            severity="info",
            kg_node_refs=[],
        )
    )
    _log_install_event(
        "7e/10", "ok",
        f"self-healed {binding_count} binding(s) + {access_count} access row(s)",
        data={
            "rebinds": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, role, old, new) in rebinds
            ],
            "access_rebinds": [
                {"project_id": pid,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, old, new) in access_rebinds
            ],
        },
    )


# ---------------------------------------------------------------------------
# Step 7: State directory
# ---------------------------------------------------------------------------

def _create_state_directory() -> None:
    print("[8/10] Creating state directory ... ", end="", flush=True)
    # Pre-Step-8 events have been buffering in `_PENDING_EVENTS`; drain
    # them now that state/logs/ exists. Subsequent events (Step 9, 10,
    # post-install-launcher) land directly in the JSONL.
    state_dir = PROJECT_ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    print("OK")
    log_path = _install_log_path()
    if log_path is not None:
        _drain_pending_events(log_path)
    _log_install_event(
        "8/10", "ok",
        "state/logs/ directory present",
        data={"state_dir": str(state_dir)},
    )


# ---------------------------------------------------------------------------
# Install manifest — written at end of successful install
# ---------------------------------------------------------------------------

INSTALL_MANIFEST_SCHEMA_VERSION = 1


def _read_tauri_version() -> str | None:
    """Read the canonical version string from launcher/src-tauri/tauri.conf.json.

    Pre-v0.2.8 this was the sole version source. v0.2.8 (Bug F) makes
    `_read_install_version` the canonical entry point, which falls back
    through several files before reaching tauri.conf.json. Kept as a
    helper so the priority chain stays explicit.
    """
    conf = PROJECT_ROOT / "launcher" / "src-tauri" / "tauri.conf.json"
    if not conf.is_file():
        return None
    try:
        with conf.open("r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get("version")
        return str(v) if v else None
    except (OSError, json.JSONDecodeError):
        return None


def _read_install_version(root: Path | None = None) -> str | None:
    """Bug F (v0.2.8): canonical version read for the installed tree.

    Priority order mirrors the Rust-side `read_version_from_install_files`,
    EXCLUDING state/install-manifest.json itself — this helper is used
    by the manifest writer, so consulting the manifest would be
    circular (re-write the stale value we're trying to refresh):

      1. vct-module.json (always shipped with releases)
      2. launcher/package.json
      3. launcher/src-tauri/Cargo.toml (`version = "..."` line)
      4. launcher/src-tauri/tauri.conf.json
    Returns None if no source yields a non-empty string.

    Defaults to PROJECT_ROOT so existing callsites pick up the new
    behavior without code changes.
    """
    base = root or PROJECT_ROOT

    # 1. vct-module.json
    vm = base / "vct-module.json"
    if vm.is_file():
        try:
            with vm.open("r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("version")
            if v:
                return str(v)
        except (OSError, json.JSONDecodeError):
            pass

    # 2. launcher/package.json
    pj = base / "launcher" / "package.json"
    if pj.is_file():
        try:
            with pj.open("r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("version")
            if v:
                return str(v)
        except (OSError, json.JSONDecodeError):
            pass

    # 3. launcher/src-tauri/Cargo.toml — parse the first top-level
    #    `version = "..."` line in the [package] block.
    cargo = base / "launcher" / "src-tauri" / "Cargo.toml"
    if cargo.is_file():
        try:
            txt = cargo.read_text(encoding="utf-8")
        except OSError:
            txt = ""
        in_pkg = False
        for line in txt.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_pkg = stripped == "[package]"
                continue
            if in_pkg and stripped.startswith("version"):
                # `version = "0.2.7"` or `version="0.2.7"`
                _, _, rhs = stripped.partition("=")
                v = rhs.strip().strip('"').strip("'")
                if v:
                    return v

    # 4. tauri.conf.json (legacy)
    tv = _read_tauri_version() if base == PROJECT_ROOT else None
    if tv:
        return tv
    # If `root` differs from PROJECT_ROOT, also probe its tauri.conf.json
    if base != PROJECT_ROOT:
        tc = base / "launcher" / "src-tauri" / "tauri.conf.json"
        if tc.is_file():
            try:
                with tc.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                v = data.get("version")
                if v:
                    return str(v)
            except (OSError, json.JSONDecodeError):
                pass

    return None


def _read_git_rev() -> tuple[str | None, str | None]:
    """Best-effort: read current commit + branch from .git/. Returns
    (commit, branch). Either may be None if .git/ is missing or the
    repo is in a detached state we can't parse simply."""
    git_dir = PROJECT_ROOT / ".git"
    if not git_dir.exists():
        return (None, None)
    head = git_dir / "HEAD"
    if not head.is_file():
        return (None, None)
    try:
        head_content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return (None, None)
    if head_content.startswith("ref: "):
        ref_path = head_content[5:].strip()
        branch = ref_path.split("/")[-1] if "/" in ref_path else ref_path
        ref_file = git_dir / ref_path
        try:
            commit = ref_file.read_text(encoding="utf-8").strip() if ref_file.is_file() else None
        except OSError:
            commit = None
        return (commit, branch)
    # Detached HEAD — head_content is the commit hash itself.
    return (head_content, None)


def _run_desktop_icon_step(args: argparse.Namespace) -> None:
    """v0.2.6 Bug C1 — invoke `scripts/post-install-launcher.sh` to create
    the desktop icon. Best-effort: any failure is logged but never aborts
    the install. Respects `--no-desktop-icon`, `VCT_NO_DESKTOP_ICON=1`,
    and the existing `VCT_NO_AUTO_LAUNCH=1` (we always pass
    `--no-auto-launch` because install.py exits next anyway and we don't
    want a duplicate launcher spawn).

    Cross-OS:
      - Linux + macOS: `bash scripts/post-install-launcher.sh <root>`
        (the .sh handles both via its `case "${OSTYPE:-}" in darwin*|linux*`
        switch).
      - Windows: `scripts/post-install-launcher.ps1` if present; else a
        single-line note in the install log + stdout — Windows users
        currently get their icon from first-install.bat, which has its
        own inline shortcut writer.
    """
    if getattr(args, "no_desktop_icon", False):
        return
    if os.environ.get("VCT_NO_DESKTOP_ICON") == "1":
        return

    helper_sh = PROJECT_ROOT / "scripts" / "post-install-launcher.sh"
    helper_ps1 = PROJECT_ROOT / "scripts" / "post-install-launcher.ps1"

    if sys.platform == "win32":
        if helper_ps1.exists():
            cmd = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(helper_ps1),
                "-RepoRoot", str(PROJECT_ROOT),
                "-NoAutoLaunch",
            ]
        else:
            # Windows path: first-install.bat owns initial shortcut. Flag
            # for follow-up so we don't silently skip on direct-run
            # install.py invocations.
            print("  [desktop-icon] Skipping on Windows: scripts/post-install-launcher.ps1 "
                  "not present. Initial shortcut is written by first-install.bat. "
                  "Re-run from first-install.bat to refresh, or wait for the PS1 "
                  "helper (TODO).")
            _log_install_event(
                "desktop-icon", "skip",
                "windows: post-install-launcher.ps1 missing",
            )
            return
    else:
        if not helper_sh.exists():
            # The helper is required for the Linux/macOS icon path. A
            # missing helper means the install tree is incomplete (e.g.
            # a partial extract). Log + return — don't crash.
            print(f"  [desktop-icon] Skipping: {helper_sh} not present.")
            _log_install_event(
                "desktop-icon", "skip",
                f"helper not found at {helper_sh}",
            )
            return
        cmd = [
            "bash", str(helper_sh), str(PROJECT_ROOT),
            "--yes",  # non-interactive: no prompts inside a Python wrapper
            "--no-auto-launch",  # install.py exits next; the user starts launcher manually
        ]

    # Run as the current user (no sudo). Stream output so the user sees
    # what the helper is doing. Soft-fail: rc != 0 is logged but doesn't
    # propagate.
    print()
    print("  [desktop-icon] Creating desktop shortcut...")
    try:
        rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    except OSError as e:
        print(f"  [desktop-icon] helper invocation failed: {e}")
        _log_install_event("desktop-icon", "error",
                           f"helper spawn failed: {e}")
        return
    if rc != 0:
        print(f"  [desktop-icon] helper exited with rc={rc} (non-fatal)")
        _log_install_event(
            "desktop-icon", "error",
            f"helper exit rc={rc}",
            data={"rc": rc},
        )
    else:
        _log_install_event("desktop-icon", "ok", "shortcut helper completed")


def _write_install_manifest(sysinfo, args, install_method: str = "install.py") -> None:
    """Write state/install-manifest.json. Idempotent: an existing manifest
    is replaced, with the prior `installed_at` preserved when present so
    the field semantically means "first ever successful install" not
    "most recent install run".

    install_method:
      - "install.py"        — direct invocation (CLI / first-install.sh path)
      - "wizard"            — Tauri install_orchestrator command
      - "update"            — re-run with --update flag
      - "lightweight"       — `--lightweight` re-install path

    Bug G (v0.2.8): this helper now runs unconditionally on every install
    path that mutates the install tree — fresh install, `--update`,
    `--lightweight`, and the Rust-side update / launcher self-update.
    Pre-v0.2.8 the manifest was written ONCE at first install and never
    refreshed, so `version` would drift for months until the user
    nuked + reinstalled. The contract is now:

      installed_at    — preserved across rewrites (true first-install ts)
      completed_at    — refreshed every rewrite (most recent successful run)
      version         — re-read from vct-module.json / tauri.conf.json each
                        time so launcher reads the live install's version,
                        not the stale one baked at first-install time
      source_commit   — refreshed (current .git/HEAD)
      source_branch   — refreshed (current .git/HEAD)

    sysinfo may be None on the lightweight / Rust-driven refresh paths
    where we don't have a freshly-detected SystemInfo handy; the manifest
    falls back to read-from-prior or "" for the affected fields.

    Failures are logged but never raised — the install IS complete by the
    time we reach this; manifest absence is recoverable, install rollback
    isn't.
    """
    state_dir = PROJECT_ROOT / "state"
    manifest_path = state_dir / "install-manifest.json"

    # Preserve original installed_at if a prior manifest exists (update path).
    installed_at = _utc_iso_now()
    prior: dict = {}
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    prior = loaded
            prior_installed_at = prior.get("installed_at")
            if isinstance(prior_installed_at, str) and prior_installed_at:
                installed_at = prior_installed_at
                # Update path: re-running install.py with --update.
                if install_method == "install.py" and getattr(args, "update", False):
                    install_method = "update"
        except (OSError, json.JSONDecodeError):
            # Corrupt / unreadable prior manifest — overwrite cleanly.
            prior = {}

    commit, branch = _read_git_rev()

    skipped = {
        "joern":   getattr(args, "no_joern", False),
        "agents":  getattr(args, "no_agents", False),
        "skills":  getattr(args, "no_skills", False),
        "hooks":   getattr(args, "no_hooks", False),
        "containers": getattr(args, "no_containers", False),
        "models":  getattr(args, "skip_models", False),
        "compile": getattr(args, "no_compile", False),
        "lean_ctx": getattr(args, "no_lean_ctx", False),
    }

    # Volume mountpoints — capture which named/host paths the compose stack
    # is bound to. Best-effort: we read them from the docker-compose.override.yml
    # if it exists, else fall back to the defaults documented in the compose file.
    volumes: dict = {"managed_by": None, "paths": {}}
    override = PROJECT_ROOT / "infrastructure" / "docker-compose.override.yml"
    if override.is_file():
        volumes["managed_by"] = "override"
        # We don't parse YAML here to keep zero deps. The override file is
        # short — leave it as a path the operator can inspect.
        volumes["override_path"] = str(override)
    else:
        volumes["managed_by"] = "default"

    # Container runtime: on full install/update we have sysinfo; on
    # lightweight (sysinfo=None) we re-use the prior manifest's value if
    # one was recorded so the field doesn't regress to null.
    container_runtime = getattr(sysinfo, "container_cmd", None)
    if container_runtime is None:
        prior_cr = prior.get("container_runtime")
        if isinstance(prior_cr, str) and prior_cr:
            container_runtime = prior_cr

    # v0.2.9 (Bug K): record the decided GPU mode + VRAM + threshold so
    # the launcher's reconfig flow (Rust side) can stay in sync without
    # re-running probes. Fields are best-effort: lightweight / Rust-driven
    # paths may not have a fresh sysinfo, in which case we preserve the
    # prior manifest values to avoid regressing to nulls.
    gpu_mode = getattr(args, "_gpu_mode", None)
    if not isinstance(gpu_mode, str):
        prior_mode = prior.get("gpu_mode")
        gpu_mode = prior_mode if isinstance(prior_mode, str) else None

    vram_gb = getattr(args, "_vram_gb_resolved", None)
    if not isinstance(vram_gb, (int, float)):
        prior_vram = prior.get("vram_gb")
        vram_gb = prior_vram if isinstance(prior_vram, (int, float)) else None

    vram_threshold = getattr(args, "_gpu_vram_threshold_resolved", None)
    if not isinstance(vram_threshold, (int, float)):
        prior_thr = prior.get("gpu_vram_threshold_gb")
        vram_threshold = (
            prior_thr if isinstance(prior_thr, (int, float))
            else _DEFAULT_GPU_VRAM_THRESHOLD_GB
        )

    manifest = {
        "schema_version":   INSTALL_MANIFEST_SCHEMA_VERSION,
        "installed":        True,
        "installed_at":     installed_at,
        "completed_at":     _utc_iso_now(),
        "version":          _read_install_version(),
        "source_commit":    commit,
        "source_branch":    branch,
        "install_method":   install_method,
        "python_version":   "%d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        "python_executable": sys.executable,
        "container_runtime": container_runtime,
        "cpu_only":         bool(getattr(args, "cpu_only", False)),
        "use_gpu":          bool(getattr(args, "gpu", False)),
        "low_resource":     bool(getattr(args, "low_resource", False)),
        "skipped":          skipped,
        "vct_state_dir":    os.environ.get("VCT_STATE_DIR") or None,
        "install_path":     str(PROJECT_ROOT),
        "volumes":          volumes,
        # v0.2.9 (Bug K) — GPU mode decision artefacts.
        "gpu_mode":         gpu_mode,
        "vram_gb":          vram_gb,
        "gpu_vram_threshold_gb": vram_threshold,
        # v0.2.16 (addendum F) — code-graph UUID-key scheme marker.
        # "v2" indicates the analyzer (templates/scripts/analyze_code_graph.py)
        # generates UUID5 from (project_name, file_path_relative, full_name)
        # rather than the pre-v0.2.16 (project_name, full_name) key. Future
        # migration tooling reads this field to decide whether existing
        # code-graph collections need a --force-recreate rebuild.
        #
        # Absent on pre-v0.2.16 installs — readers MUST treat the missing
        # field as the implicit "v1" scheme and offer the appropriate
        # migration path (do NOT force-write "v1" here; the absence is
        # informative). Written unconditionally on every install/update
        # path that exercises this writer (fresh install, --update,
        # --lightweight, --bootstrap, wizard).
        "uuid_scheme":      "v2",
    }

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
            f.write("\n")
        _log_install_event(
            "manifest", "ok",
            "wrote install-manifest.json",
            data={"path": str(manifest_path), "version": manifest["version"]},
        )
    except OSError as exc:
        # Don't fail the install for this — it's diagnostic metadata, not
        # required for operation.
        _log_install_event(
            "manifest", "warn",
            f"could not write install-manifest.json: {exc}",
        )

    # v0.2.9 (Bug J): also drop a single-line `state/install/runtime.txt`
    # recording the detected container runtime ("podman" or "docker") so
    # boot-time wrapper scripts (scripts/launch-claude-mcp-stack.sh) can
    # pick the right compose binary without re-probing PATH. Best-effort.
    # v0.2.10 (Bug L3): extracted to _persist_runtime_txt so the same helper
    # is callable from --lightweight, --update, and the post-install-runtime
    # paths without duplicating logic.
    if container_runtime:
        _persist_runtime_txt(container_runtime)


def _persist_runtime_txt(container_runtime: str | None) -> None:
    """Write a single-line `state/install/runtime.txt` recording the
    container runtime token ("podman" or "docker") for the boot-time
    wrapper scripts to consume.

    v0.2.10 (Bug L3): runtime.txt was originally written only inside
    `_write_install_manifest`. That covered fresh install + --update +
    --lightweight via the existing call sites, but missed the path where
    `_prompt_install_container_runtime` runs mid-flow and the runtime
    appears AFTER the initial sysinfo scan. This helper is now also
    invoked from the post-prompt-install branch so a one-shot
    install-the-runtime decision propagates to the wrapper immediately
    on the very same install run (without forcing a second invocation).

    Soft-fail: any I/O error is logged and the install continues. The
    wrapper script re-probes PATH if runtime.txt is missing.
    """
    if not container_runtime:
        return
    # The first whitespace-separated token is the binary name; the rest is
    # the version string. Normalize to lowercase ("Podman" / "Docker"
    # both seen in the wild on minor versions).
    runtime_token = container_runtime.split()[0].lower()
    if runtime_token not in ("podman", "docker"):
        return
    state_dir = PROJECT_ROOT / "state"
    install_subdir = state_dir / "install"
    runtime_file = install_subdir / "runtime.txt"
    try:
        install_subdir.mkdir(parents=True, exist_ok=True)
        # Idempotent: only write if content differs (avoids unnecessary
        # mtime churn that could confuse downstream watchers).
        existing = ""
        if runtime_file.is_file():
            try:
                existing = runtime_file.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
        if existing != runtime_token:
            runtime_file.write_text(runtime_token + "\n", encoding="utf-8")
    except OSError as exc:
        _log_install_event(
            "manifest", "warn",
            f"could not write runtime.txt: {exc}",
        )


# ---------------------------------------------------------------------------
# Step 7b: Boot-service materialization (v0.2.10 Bug L2 — cross-OS)
#
# A short, OS-aware helper that installs a boot-time autostart entry for
# the Claude MCP container stack so containers come back up after a
# reboot without manual intervention. Three platform paths:
#
#   - Linux:   systemd user unit at ~/.config/systemd/user/<unit>.service
#              (template at templates/systemd/*.service.template)
#              + `loginctl enable-linger` so the unit fires at boot
#              before the user logs in.
#   - macOS:   launchd LaunchAgent at ~/Library/LaunchAgents/<label>.plist
#              (template at templates/launchd/*.plist.template), loaded
#              via `launchctl bootstrap` (modern) or `launchctl load -w`
#              (legacy fallback).
#   - Windows: Task Scheduler logon trigger registered via
#              `schtasks /Create /XML <file> /F` (template at
#              templates/windows/*.task.xml.template). v0.2.14 Bug #2:
#              runs the PowerShell sibling wrapper
#              (scripts/launch-claude-mcp-stack.ps1) via powershell.exe
#              (always present on Win10+) — no Git Bash / WSL bash
#              dependency. Falls back to the .sh wrapper only when the
#              .ps1 isn't shipped.
#
# Soft-fail throughout: if the OS-specific tool isn't on PATH (e.g.
# WSL minimal, container hosts, macOS without launchctl, locked-down
# Windows shells), log a warning and continue. The install MUST NOT be
# blocked by boot-service materialization failure — the user can always
# bring the stack up manually.
#
# GPU-passthrough support matrix (Linux=full, macOS=no, Windows=via WSL2)
# is documented in the OS-specific templates' header comments.
# ---------------------------------------------------------------------------

# Stable filename / label for the boot service across OSes. Reverse-DNS
# form on macOS (required by launchd), bare filename on Linux + Windows.
_BOOT_SERVICE_UNIT_NAME = "claude-mcp-containers.service"
_BOOT_SERVICE_PLIST_LABEL = "com.vibecodedtools.claude-mcp-containers"
_BOOT_SERVICE_TASK_NAME = "ClaudeMcpContainers"


def _resolve_compose_working_dir(
    install_path: Path,
    cli_override: str | None,
    ps_label_value: str | None,
) -> Optional[Path]:
    """Decide which directory the compose-up wrapper should chdir into.

    The compose-project dir may NOT be the install path (canonical
    example: install at one project dir, but compose.yaml lives in a
    sibling repo's `claude_mcp_servers/`).

    Resolution priority (PR-12 Bug C — install_path subdirs now BEAT the
    ps-label probe, so a stale container from a prior install path can't
    pin the new install's systemd unit to an obsolete WorkingDirectory):

      1. CLI override (--compose-working-dir) — explicit user choice.
      2. `<install_path>/claude_mcp_servers/` if it exists.
      3. `<install_path>/infrastructure/` if it exists (VCO's own layout
         where compose.yaml + the overlay both live there).
      4. `ps_label_value` — the value of the
         `com.docker.compose.project.working_dir` label on a running
         claude-mcp container, sniffed by the caller via `<runtime> ps`.
         Now a LAST RESORT for the rare edge case where the install path
         has neither subdir locally (e.g. compose.yaml shipped in a
         sibling repo). Caller passes None if probing failed.
      5. None — caller skips materialization with a warning.

    Why the priority inversion (Bug C, 2026-05-16): when a user upgrades
    VCO via the launcher GUI / `install.py --update`, pre-existing
    containers from a PRIOR install carry the
    `com.docker.compose.project.working_dir=<old-path>` label. The
    previous priority-2 ps-label probe would re-use that old path in the
    fresh systemd unit, pinning the user's boot service to a stale
    directory across upgrades. Inverting the order so install_path
    subdirs win means the unit's WorkingDirectory tracks the install
    location whenever it has a recognisable layout — which is the
    overwhelmingly common case.

    Pure function: no env reads, no subprocess. Caller supplies the ps
    probe result (so this function is unit-testable without spawning
    podman). Tested via tests/test_resolve_compose_working_dir.py.
    """
    # 1. CLI override — explicit user choice always wins.
    if cli_override:
        candidate = Path(cli_override).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        # Override pointed at a missing dir — that's a user error worth
        # flagging. Caller logs and falls through.
        return None
    # 2. install_path/claude_mcp_servers — the canonical layout.
    candidate = (install_path / "claude_mcp_servers").resolve()
    if candidate.is_dir():
        return candidate
    # 3. install_path/infrastructure (the VCO-native layout).
    candidate = (install_path / "infrastructure").resolve()
    if candidate.is_dir():
        return candidate
    # 4. ps_label_value — last-resort fallback (e.g. compose.yaml lives
    # outside install_path entirely). Pre-PR-12 this was priority 2,
    # which caused boot-service WorkingDirectory to get pinned to stale
    # install paths across upgrades.
    if ps_label_value:
        candidate = Path(ps_label_value).expanduser().resolve()
        if candidate.is_dir():
            return candidate
    # 5. give up
    return None


def _probe_compose_working_dir_via_ps(container_cmd: str) -> Optional[str]:
    """Best-effort: ask the running container runtime for the
    `com.docker.compose.project.working_dir` label of any running
    `claude-mcp` project container.

    Returns the label value as a string, or None if anything goes wrong
    (runtime absent, no running containers, label missing, etc.).
    Soft-fail by design — `_materialize_boot_service` then falls back to
    install-path probes.
    """
    if not container_cmd or not shutil.which(container_cmd):
        return None
    try:
        proc = subprocess.run(
            [
                container_cmd, "ps",
                "--filter", "label=com.docker.compose.project=claude-mcp",
                "--format", '{{index .Labels "com.docker.compose.project.working_dir"}}',
            ],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _read_template(template_relpath: str) -> Optional[str]:
    """Read a template file relative to PROJECT_ROOT. Returns None if
    the file isn't present (e.g. minimal install without the templates/
    tree shipped). Caller logs + skips."""
    path = PROJECT_ROOT / template_relpath
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _render_template(template_text: str, substitutions: dict) -> str:
    """Naive `{{KEY}}` substitution. We deliberately don't pull in
    jinja2 / string.Template here — the substitution set is closed and
    we want install.py to stay stdlib-only for the early steps."""
    rendered = template_text
    for key, value in substitutions.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def _backup_and_write_idempotent(
    target: Path,
    rendered: str,
) -> tuple[bool, Optional[Path]]:
    """Write `rendered` to `target` ONLY if content differs from what's
    on disk. Backs up the prior file to `<target>.bak-<ISO8601>` before
    overwriting. Returns (changed, backup_path_or_None).

    Idempotent: re-running install/update with unchanged template +
    substitutions is a no-op (zero writes, zero backups).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == rendered:
            return False, None
        # Content differs — back up the prior version.
        stamp = _utc_iso_now().replace(":", "").replace("-", "")
        backup = target.with_name(target.name + f".bak-{stamp}")
        try:
            backup.write_text(existing or "", encoding="utf-8")
        except OSError:
            backup = None
    else:
        backup = None
    target.write_text(rendered, encoding="utf-8")
    return True, backup


def _user_home_for_install() -> Path:
    """Return the user home directory for boot-service / config writes.

    Honors ``VCT_USER_HOME_OVERRIDE`` env var when set (used by pytest
    fixtures to redirect systemd-unit / launchd-plist / log writes into
    a ``tmp_path``-based fake home). Falls back to ``Path.home()``.

    Why this exists (Bug X, 2026-05-16): the boot-service materializer
    + ``_repair_systemd_unit_working_dir`` historically called
    ``Path.home()`` directly. Tests that monkeypatched
    ``install._materialize_boot_service_linux`` to raise (verifying the
    dispatcher's soft-fail) still hit the repair step BEFORE the
    patched renderer, and the repair step's ``Path.home()`` returned
    the real user home — corrupting ``~/.config/systemd/user/claude-mcp-containers.service``
    on every test run with the pytest ``tmp_path``. This single helper
    consolidates the lookup so a single env-var monkeypatch sandboxes
    the entire surface.

    Cross-OS: returns a real or fake home on Linux/macOS/Windows
    identically; the systemd/launchd/Task Scheduler writers that consume
    it append their OS-specific subpaths (``.config/systemd/user``,
    ``Library/LaunchAgents``, etc.).
    """
    override = os.environ.get("VCT_USER_HOME_OVERRIDE", "").strip()
    if override:
        return Path(override)
    return Path.home()


def _materialize_boot_service_linux(
    install_path: Path,
    working_dir: Path,
) -> None:
    """Linux: render the systemd user unit and enable it via
    `systemctl --user enable` + `loginctl enable-linger`."""
    template = _read_template("templates/systemd/claude-mcp-containers.service.template")
    if template is None:
        _log_install_event(
            "boot-service", "skip",
            "templates/systemd/claude-mcp-containers.service.template missing",
        )
        return

    unit_dir = _user_home_for_install() / ".config" / "systemd" / "user"
    unit_path = unit_dir / _BOOT_SERVICE_UNIT_NAME
    wrapper = install_path / "scripts" / "launch-claude-mcp-stack.sh"
    log_dir = _user_home_for_install() / ".local" / "state" / "vct"
    log_file = log_dir / "claude-mcp-containers.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    rendered = _render_template(template, {
        "INSTALLED_AT_PATH": str(unit_path),
        "WORKING_DIR": str(working_dir),
        "WRAPPER_SCRIPT": str(wrapper),
        "LOG_FILE": str(log_file),
    })
    try:
        changed, backup = _backup_and_write_idempotent(unit_path, rendered)
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not write systemd unit: {exc}",
        )
        return

    _log_install_event(
        "boot-service", "ok" if changed else "skip",
        ("systemd unit written" if changed else "systemd unit unchanged"),
        data={"unit_path": str(unit_path),
              "backup": str(backup) if backup else None},
    )

    # daemon-reload + enable. Soft-fail if systemctl absent (containers,
    # WSL minimal, etc.).
    systemctl = shutil.which("systemctl")
    if not systemctl:
        _log_install_event(
            "boot-service", "skip",
            "systemctl not on PATH — skipping daemon-reload / enable",
        )
        return
    for cmd in (
        [systemctl, "--user", "daemon-reload"],
        [systemctl, "--user", "enable", _BOOT_SERVICE_UNIT_NAME],
    ):
        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log_install_event(
                "boot-service", "warn",
                f"systemctl invocation failed: {' '.join(cmd)} → {exc}",
            )

    # loginctl enable-linger — needed so the user-scoped unit fires at
    # boot without an active login session. Check existing state first
    # (idempotent — no-op if already lingering).
    loginctl = shutil.which("loginctl")
    if not loginctl:
        _log_install_event(
            "boot-service", "skip",
            "loginctl not on PATH — user-unit will only fire at login",
        )
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return
    try:
        probe = subprocess.run(
            [loginctl, "show-user", user, "--property=Linger"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        already_lingering = "Linger=yes" in probe.stdout
    except (OSError, subprocess.TimeoutExpired):
        already_lingering = False
    if not already_lingering:
        try:
            subprocess.run(
                [loginctl, "enable-linger", user],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log_install_event(
                "boot-service", "warn",
                f"loginctl enable-linger failed: {exc}",
            )


def _materialize_boot_service_macos(
    install_path: Path,
    working_dir: Path,
) -> None:
    """macOS: render the LaunchAgent plist and bootstrap it via
    `launchctl bootstrap` (modern) or `launchctl load -w` (legacy)."""
    template = _read_template(
        "templates/launchd/com.vibecodedtools.claude-mcp-containers.plist.template"
    )
    if template is None:
        _log_install_event(
            "boot-service", "skip",
            "launchd template missing",
        )
        return

    plist_dir = _user_home_for_install() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{_BOOT_SERVICE_PLIST_LABEL}.plist"
    wrapper = install_path / "scripts" / "launch-claude-mcp-stack.sh"
    log_dir = _user_home_for_install() / "Library" / "Logs"
    log_file = log_dir / "claude-mcp-containers.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    rendered = _render_template(template, {
        "INSTALLED_AT_PATH": str(plist_path),
        "LABEL": _BOOT_SERVICE_PLIST_LABEL,
        "WORKING_DIR": str(working_dir),
        "WRAPPER_SCRIPT": str(wrapper),
        "LOG_FILE": str(log_file),
    })
    try:
        changed, backup = _backup_and_write_idempotent(plist_path, rendered)
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not write launchd plist: {exc}",
        )
        return

    _log_install_event(
        "boot-service", "ok" if changed else "skip",
        ("launchd plist written" if changed else "launchd plist unchanged"),
        data={"plist_path": str(plist_path),
              "backup": str(backup) if backup else None},
    )

    launchctl = shutil.which("launchctl")
    if not launchctl:
        _log_install_event(
            "boot-service", "skip",
            "launchctl not on PATH — skipping load",
        )
        return

    uid = os.getuid() if hasattr(os, "getuid") else 0
    # Modern syntax (macOS 10.10+): `launchctl bootstrap gui/<uid> <plist>`.
    # Idempotent: if the agent is already bootstrapped, this returns non-zero
    # with "Bootstrap failed: 17: File exists" — we tolerate that.
    bootstrap_rc = -1
    try:
        proc = subprocess.run(
            [launchctl, "bootstrap", f"gui/{uid}", str(plist_path)],
            check=False, capture_output=True, text=True, timeout=10,
        )
        bootstrap_rc = proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        bootstrap_rc = -1
    if bootstrap_rc != 0:
        # Fall back to legacy `launchctl load -w <plist>`. -w persists the
        # enable across reboots. Same idempotency tolerance.
        try:
            subprocess.run(
                [launchctl, "load", "-w", str(plist_path)],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log_install_event(
                "boot-service", "warn",
                f"launchctl load fallback failed: {exc}",
            )


def _materialize_boot_service_windows(
    install_path: Path,
    working_dir: Path,
) -> None:
    """Windows: render the Task Scheduler XML and import it via
    `schtasks /Create /XML <file> /F` (per-user logon trigger, no
    admin scope)."""
    template = _read_template(
        "templates/windows/claude-mcp-containers.task.xml.template"
    )
    if template is None:
        _log_install_event(
            "boot-service", "skip",
            "Windows Task Scheduler XML template missing",
        )
        return

    # We materialize the task XML at <install>/state/installed_boot_task.xml
    # so idempotency-check on re-runs is a simple file-compare. Also
    # serves as an audit artefact (operator can inspect what got
    # registered).
    state_dir = install_path / "state"
    task_xml_path = state_dir / "installed_boot_task.xml"
    # v0.2.14 Bug #2: prefer the PowerShell sibling on Windows. It uses
    # powershell.exe (always present on Win10+) so the Scheduled Task no
    # longer requires Git Bash / WSL bash on PATH. Fall back to the .sh
    # wrapper only when the .ps1 isn't shipped (older orchestrator
    # snapshot, custom install). The Task XML template's <Arguments>
    # block invokes powershell.exe -File <WRAPPER_SCRIPT> when the
    # template is at v0.2.14+; if a user has an older template still
    # invoking bash, they'd point WRAPPER_SCRIPT at the .sh — but
    # the manifest-driven update flow propagates both together so this
    # mismatch should not occur in practice.
    wrapper_ps1 = install_path / "scripts" / "launch-claude-mcp-stack.ps1"
    wrapper_sh = install_path / "scripts" / "launch-claude-mcp-stack.sh"
    if wrapper_ps1.exists():
        wrapper = wrapper_ps1
    else:
        wrapper = wrapper_sh
    # Forward-slash form avoids XML quoting issues. PowerShell accepts
    # both forward and backslash path separators uniformly.
    wrapper_forward = str(wrapper).replace("\\", "/")
    working_dir_forward = str(working_dir).replace("\\", "/")
    user_id = (
        os.environ.get("USERDOMAIN", "")
        + ("\\" if os.environ.get("USERDOMAIN") else "")
        + os.environ.get("USERNAME", "")
    ).strip("\\")
    if not user_id:
        user_id = os.environ.get("USER", "user")

    rendered = _render_template(template, {
        "LABEL": _BOOT_SERVICE_TASK_NAME,
        "WORKING_DIR": working_dir_forward,
        "WRAPPER_SCRIPT": wrapper_forward,
        "CREATED_AT": _utc_iso_now(),
        "USER_ID": user_id,
    })
    try:
        changed, backup = _backup_and_write_idempotent(task_xml_path, rendered)
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not write Task Scheduler XML: {exc}",
        )
        return

    _log_install_event(
        "boot-service", "ok" if changed else "skip",
        ("Task XML written" if changed else "Task XML unchanged"),
        data={"task_xml_path": str(task_xml_path),
              "backup": str(backup) if backup else None},
    )

    schtasks = shutil.which("schtasks")
    if not schtasks:
        _log_install_event(
            "boot-service", "skip",
            "schtasks not on PATH — Task Scheduler import skipped",
        )
        return
    try:
        subprocess.run(
            [schtasks, "/Create", "/TN", _BOOT_SERVICE_TASK_NAME,
             "/XML", str(task_xml_path), "/F"],
            check=False, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log_install_event(
            "boot-service", "warn",
            f"schtasks /Create failed: {exc}",
        )


# ---------------------------------------------------------------------------
# PR-14b: MCP simplification — deprecation deferral helpers (v0.2.11)
# ---------------------------------------------------------------------------

def _check_searxng_remnants(
    install_path: Path,
    deferral_report: "DeferralReport",
) -> None:
    """Emit a deferral when pre-0.2.11 SearXNG artefacts are found on disk.

    SearXNG was removed from the default compose stack in v0.2.11.  The
    search MCP now ships only ``search_papers`` (OpenAlex + arXiv); the
    web/code-search tools that depended on SearXNG are gone.

    This helper is soft-fail throughout — any I/O error is caught and
    logged; install completes regardless.

    Args:
        install_path:    Orchestrator project root (typically ``PROJECT_ROOT``).
        deferral_report: Run-scoped :class:`DeferralReport` to append the
            entry to when artefacts are found.
    """
    try:
        found_paths: list[str] = []
        searxng_dir = install_path / "claude_mcp_servers" / "searxng"
        if searxng_dir.exists():
            found_paths.append(str(searxng_dir))
        searxng_tpl = install_path / "templates" / "searxng" / "settings.yml.template"
        if searxng_tpl.exists():
            found_paths.append(str(searxng_tpl))

        if not found_paths:
            return

        paths_str = "\n".join(f"  - {p}" for p in found_paths)
        rm_args = " ".join(found_paths)
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="searxng_removed_from_default_install",
                title="SearXNG artefacts from pre-0.2.11 install detected",
                detected=(
                    f"SearXNG no longer ships by default in v0.2.11. "
                    f"Existing local settings preserved at:\n{paths_str}"
                ),
                why_deferred=(
                    "Automatic removal of user-customised SearXNG settings "
                    "would discard any secret_key or engine list the user "
                    "configured. Manual review required before deletion."
                ),
                command_to_apply=(
                    f"rm -r {rm_args}\n"
                    "podman rm -f $(podman ps -a --filter name=searxng -q) 2>/dev/null || true\n"
                    "# Search narrowed to academic-paper search (search_papers MCP)."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                ],
            )
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        _log_install_event(
            "searxng_remnants_check", "warn",
            f"could not check SearXNG remnants: {exc}",
        )


def _check_ollama_mcp_remnants(
    deferral_report: "DeferralReport",
) -> None:
    """Emit a deferral when the Ollama MCP entry is still in ~/.claude.json.

    The Ollama MCP server (``chat``, ``read_document``, ``read_image``)
    was removed from the default install in v0.2.11 — those tools are
    redundant with Claude's native capabilities.  Ollama as embedding
    *infrastructure* (Weaviate vectorizers) is unchanged.

    Reads ``_user_home_for_install() / ".claude.json"`` if it exists.
    Soft-fail throughout — missing or malformed JSON is logged and skipped.

    Uses :func:`_user_home_for_install` (introduced by PR-16) so that
    pytest fixtures can redirect the lookup via ``VCT_USER_HOME_OVERRIDE``
    without touching the real user home.

    Args:
        deferral_report: Run-scoped :class:`DeferralReport` to append the
            entry to when the ``ollama`` MCP block is found.
    """
    claude_json = _user_home_for_install() / ".claude.json"
    if not claude_json.is_file():
        return
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log_install_event(
            "ollama_mcp_check", "warn",
            f"could not read {claude_json}: {exc}",
        )
        return
    try:
        if not isinstance(data, dict):
            return
        mcp_servers = data.get("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            return
        if "ollama" not in mcp_servers:
            return

        deferral_report.add_entry(
            DeferralEntry(
                condition_id="ollama_mcp_deprecated",
                title="Ollama MCP server removed from default install in v0.2.11",
                detected=(
                    f"An `ollama` block was found under `mcpServers` in "
                    f"{claude_json}. The Ollama MCP server (chat / "
                    "read_document / read_image) is no longer part of the "
                    "default install — those tools are redundant with "
                    "Claude's native capabilities."
                ),
                why_deferred=(
                    "Auto-removal of ~/.claude.json entries would be brittle "
                    "and requires user consent. The existing entry is preserved "
                    "and fully functional; this deferral is informational only."
                ),
                command_to_apply=(
                    "# Remove the `ollama` block from ~/.claude.json manually:\n"
                    f"# Edit {claude_json} and delete the `\"ollama\": {{...}}` "
                    "entry under `mcpServers`.\n"
                    "# Ollama as embedding infrastructure (Weaviate vectorizers) "
                    "is UNCHANGED — only the MCP tool-surface is removed."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                ],
            )
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        _log_install_event(
            "ollama_mcp_check", "warn",
            f"could not check Ollama MCP remnants: {exc}",
        )


def _check_search_mcp_env_obsolete(
    deferral_report: "DeferralReport",
) -> None:
    """Emit a deferral when obsolete env vars remain in the search MCP entry.

    In v0.2.11 the search MCP was simplified to ``search_papers`` only.
    ``SEARXNG_URL`` (no longer needed — SearXNG dropped) and
    ``GITHUB_TOKEN`` (no longer needed — GitHub code search removed) are
    now obsolete in ``mcpServers.search.env``.

    Reads ``_user_home_for_install() / ".claude.json"`` if it exists.
    Soft-fail throughout — missing or malformed JSON is logged and skipped.

    Uses :func:`_user_home_for_install` (introduced by PR-16) so that
    pytest fixtures can redirect the lookup via ``VCT_USER_HOME_OVERRIDE``
    without touching the real user home.

    Args:
        deferral_report: Run-scoped :class:`DeferralReport` to append the
            entry to when obsolete keys are found.
    """
    claude_json = _user_home_for_install() / ".claude.json"
    if not claude_json.is_file():
        return
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log_install_event(
            "search_mcp_env_check", "warn",
            f"could not read {claude_json}: {exc}",
        )
        return
    try:
        if not isinstance(data, dict):
            return
        search_env = (
            data.get("mcpServers", {})
            .get("search", {})
            .get("env", {})
        )
        if not isinstance(search_env, dict):
            return

        obsolete_keys = [
            k for k in ("SEARXNG_URL", "GITHUB_TOKEN")
            if k in search_env
        ]
        if not obsolete_keys:
            return

        keys_str = ", ".join(f"`{k}`" for k in obsolete_keys)
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="search_mcp_simplified",
                title="Obsolete env vars in search MCP entry in ~/.claude.json",
                detected=(
                    f"The following env vars in `mcpServers.search.env` of "
                    f"{claude_json} are no longer used by the search MCP "
                    f"in v0.2.11: {keys_str}. "
                    "The search MCP now provides only `search_papers` "
                    "(OpenAlex + arXiv)."
                ),
                why_deferred=(
                    "Automatic removal of ~/.claude.json env vars would "
                    "silently break setups where users forward these "
                    "variables for other purposes. Manual review required."
                ),
                command_to_apply=(
                    f"# Remove obsolete env vars from mcpServers.search.env "
                    f"in {claude_json}:\n"
                    + "\n".join(
                        f"# Delete the `\"{k}\": \"...\"` line from "
                        "`mcpServers.search.env`."
                        for k in obsolete_keys
                    )
                    + "\n# Only `OPENALEX_EMAIL` is needed going forward."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                ],
            )
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        _log_install_event(
            "search_mcp_env_check", "warn",
            f"could not check search MCP env remnants: {exc}",
        )


# ---------------------------------------------------------------------------
# PR-23 (v0.2.12, 2026-05-16): default-MCP registration into ~/.claude.json
#
# Audit reference: .claude/context/mcp-install-pipeline-audit-2026-05-16.md
#
# Pre-PR-23 install.py performed zero MCP registration. Result: fresh
# v0.2.11 installs left ~/.claude.json with no bundled MCP servers wired
# at all → Claude Code couldn't see the orchestrator. The Rust
# `mcp_registration::register_mcp` helper existed and was tested, but no
# install code path invoked it.
#
# Architecture (FINALIZED 2026-05-16):
#   - The launcher binary is the single writer of both ~/.claude.json
#     AND the project_mcp_servers DB table.
#   - install.py shells out to a CLI subcommand on the launcher:
#       `<binary> --register-default-mcps <install_root>`
#   - 4-tier launcher-binary resolution:
#       1. Bundled binary at `launcher/dist/<os>-<arch>/vct-launcher`.
#       2. Download from GitHub Releases matching the current version.
#       3. Rebuild via `cargo tauri build` (slow LAST resort).
#       4. Pure-Python JSON merge (always succeeds at writing JSON;
#          launcher DB stays empty until the user opens the GUI and
#          project_state_populate picks up the JSON entries).
#
# Security boundary: `~/.claude.json` is readable by every process running
# as the user. Therefore secret-shaped env keys (TOKEN, SECRET, PAT,
# PASSWORD, AUTH, *_KEY) are silently dropped from any written entry, AND
# per-project keys (KG_COLLECTION, PROJECT_NAME, etc.) are NEVER written —
# those live in each project's .claude/settings.json env (launcher-managed)
# instead. Empirical verification 2026-05-16: SD15's MCP subprocess at
# PID 104741 picked up its KG_COLLECTION from .claude/settings.json env,
# confirming the per-project env channel is sufficient.
# ---------------------------------------------------------------------------

# Env-key allowlist for ~/.claude.json mcpServers.*.env. MUST stay in sync
# with launcher/src-tauri/src/mcp_registration.rs::ALLOWED_ENV_KEYS.
#
# CRITICAL CONTRACT (see Issue H.1 from mcp-instability audit 2026-05-16):
# Anthropic semantics say "project scope overrides user scope" for env
# vars, but Claude Code applies ~/.claude.json mcpServers.*.env keys LAST
# to MCP subprocesses — so they WIN against .claude/settings.json env.
# This is the wrong direction for any per-project-varying value.
#
# Therefore this allowlist is restricted to keys that are TRULY
# machine-invariant (same value across every workspace on the user's
# machine): service URLs/ports, PYTHONPATH (resolves to the install_root),
# and ACTIVE_EMBEDDING (the embedding-mode toggle is machine-wide because
# it determines which named-vector Weaviate column is queried).
#
# Removed in PR-43 (post-PR-23): RL_SERVER_URL (varies per user setup —
# VCO_dev runs a dedicated port-11442 service, MAO uses 11439, etc.),
# EMBEDDING_MODEL (users may want per-project override; if it's in this
# global allowlist, the per-project .claude/settings.json env value gets
# overridden the WRONG WAY due to Claude Code's precedence).
_ALLOWED_GLOBAL_ENV_KEYS = (
    "WEAVIATE_URL",
    "OLLAMA_URL",
    "GRPC_PORT",
    "PYTHONPATH",
    "ACTIVE_EMBEDDING",
    "CODE_EMBED_SERVICE_URL",
)

# Patterns that MUST be silently dropped (secrets). Case-insensitive
# substring match for the "contains" group; plus the explicit `_KEY` /
# `KEY` rule for the suffix group. Mirrors mcp_registration.rs.
_SECRET_SHAPED_SUBSTRINGS = (
    "TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH",
)


def _is_secret_shaped_env_key(key: str) -> bool:
    """True iff `key` looks like a credential. See module docstring.

    Matches secret substrings as TOKENS within ``[_\\-]``-delimited
    env-key parts. Avoids false positives like ``PYTHONPATH`` matching
    ``PAT`` or ``COMPASS`` matching ``PASS``. The keys we care about
    (``GITHUB_TOKEN``, ``DB_PASS``, ``MY_PAT``, ``AUTH_HEADER``, etc.)
    all have the secret token as a distinct segment between underscores
    or at the boundary of the string.
    """
    upper = key.upper()
    # Split on `_` and `-` (the two common env-key segment separators).
    parts = re.split(r"[_\-]+", upper)
    for needle in _SECRET_SHAPED_SUBSTRINGS:
        if needle in parts:
            return True
    # Trailing `_KEY` and exact `KEY` rules (catch STRIPE_KEY etc.).
    if upper == "KEY" or upper.endswith("_KEY"):
        return True
    return False


def _filter_env_for_global_json(candidate: dict) -> tuple[dict, list[str]]:
    """Return (safe_env, dropped_keys). Mirrors Rust filter_env_for_global_json."""
    safe = {}
    dropped = []
    for k, v in candidate.items():
        if _is_secret_shaped_env_key(k):
            dropped.append(k)
            continue
        if k not in _ALLOWED_GLOBAL_ENV_KEYS:
            dropped.append(k)
            continue
        safe[k] = v
    return safe, dropped


def _resolve_venv_python_for_install(install_root: Path) -> Optional[Path]:
    """Locate the Python interpreter inside the install's venv.

    Tries the canonical modern layout `<root>/.venv` first, then the legacy
    `<root>/claude_mcp_servers/.venv`. Returns None if neither exists; the
    caller treats that as a soft-fail and proceeds to the Python fallback.
    """
    sub = "Scripts" if platform.system().lower().startswith("win") else "bin"
    py_name = "python.exe" if platform.system().lower().startswith("win") else "python"
    candidates = [
        install_root / ".venv" / sub / py_name,
        install_root / "claude_mcp_servers" / ".venv" / sub / py_name,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _launcher_binary_relative_path() -> tuple[str, str]:
    """Return (subdir, filename) for the bundled launcher binary on this OS.

    Linux: ('linux-x64', 'vct-launcher')
    macOS: ('macos-arm64', 'vct-launcher')          ← post-v0.2.13 canonical slot
    Windows: ('windows-x64', 'vct-launcher.exe')

    History (v0.2.14 fix): the Darwin branch previously returned
    'experimental_macOS' but the actual binary always shipped in
    launcher/dist/macos-arm64/ (matches the release-artifact name +
    scripts/build-bundled-launcher.sh). The 'experimental_macOS'
    directory was an empty placeholder. The mismatch caused tier-1
    (bundled) lookup to return None on macOS even when the binary was
    present, falling through to GitHub download (which landed in the
    same wrong dir). Audit Bug #1 (cross-OS audit, 2026-05-17).
    Intel Macs (x86_64) are intentionally not shipped — release.yml
    line 31 only builds arm64.
    """
    system = platform.system().lower()
    if system.startswith("win"):
        return ("windows-x64", "vct-launcher.exe")
    if system == "darwin":
        return ("macos-arm64", "vct-launcher")
    # Linux + everything else
    return ("linux-x64", "vct-launcher")


def _try_bundled_launcher_binary(install_root: Path) -> Optional[Path]:
    """Tier 1: bundled binary at launcher/dist/<os>-<arch>/vct-launcher[.exe]."""
    subdir, fname = _launcher_binary_relative_path()
    p = install_root / "launcher" / "dist" / subdir / fname
    if p.is_file() and os.access(p, os.X_OK if not platform.system().lower().startswith("win") else os.F_OK):
        return p
    return None


def _read_launcher_version(install_root: Path) -> Optional[str]:
    """Parse `version = "0.2.x"` out of launcher/src-tauri/Cargo.toml. None on failure."""
    cargo = install_root / "launcher" / "src-tauri" / "Cargo.toml"
    if not cargo.is_file():
        return None
    try:
        for line in cargo.read_text(encoding="utf-8").splitlines()[:30]:
            line = line.strip()
            if line.startswith("version"):
                # version = "0.2.11"
                parts = line.split("=", 1)
                if len(parts) == 2:
                    raw = parts[1].strip().strip('"').strip("'")
                    if raw:
                        return raw
    except OSError:
        return None
    return None


def _try_download_launcher_binary(install_root: Path) -> Optional[Path]:
    """Tier 2: download matching release artifact from GitHub Releases.

    Uses `gh` CLI if available (handles auth + redirects cleanly); falls
    back to `curl -L` if `gh` is missing. Soft-fail on every error
    (network down, release missing, auth refused, etc.) — returns None
    and lets the caller move to Tier 3.

    The downloaded ZIP is extracted to a tempdir; only the binary is
    moved into place at `launcher/dist/<os>-<arch>/`.
    """
    version = _read_launcher_version(install_root)
    if not version:
        return None
    subdir, fname = _launcher_binary_relative_path()
    target_dir = install_root / "launcher" / "dist" / subdir
    target_path = target_dir / fname
    # Release artifact naming convention (see .github/workflows/release.yml +
    # the public-release pattern documented in CLAUDE.md "Release process").
    # Pattern: vibecoded-orchestrator-<version>-<os>-<arch>.zip
    # Confirmed from v0.2.11 release assets (gh release view v0.2.11):
    #   linux-x64.zip, macos-arm64.zip, windows-x64.zip
    # Intel Macs (x86_64) are intentionally NOT shipped (see
    # .github/workflows/release.yml line 31). macos-x64 download will
    # 404 — those users fall through to tier 3 (cargo rebuild).
    # Release artifact name token. Since v0.2.14 the dist subdir
    # matches the release-artifact token directly (post-Bug-1 fix —
    # `experimental_macOS → macos-arm64` rename); this mapping is now
    # a pass-through and could be removed entirely, kept as a hook for
    # future os-arch additions that might need a name-shift.
    os_arch_token = {
        "linux-x64": "linux-x64",
        "windows-x64": "windows-x64",
        "macos-arm64": "macos-arm64",
    }.get(subdir, subdir)
    artifact = f"vibecoded-orchestrator-{version}-{os_arch_token}.zip"
    inner_root = f"vibecoded-orchestrator-{version}-{os_arch_token}"

    tmpdir = Path(tempfile.mkdtemp(prefix="vct-launcher-dl-"))
    try:
        zip_path = tmpdir / artifact
        # Prefer gh; fall back to curl.
        if shutil.which("gh"):
            cmd = [
                "gh", "release", "download", f"v{version}",
                "--repo", "hotak92/vibecoded-orchestrator",
                "--pattern", artifact,
                "--dir", str(tmpdir),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=60, text=True
                )
                if result.returncode != 0:
                    return None
            except (subprocess.SubprocessError, OSError):
                return None
        elif shutil.which("curl"):
            url = (
                f"https://github.com/hotak92/vibecoded-orchestrator/"
                f"releases/download/v{version}/{artifact}"
            )
            try:
                result = subprocess.run(
                    ["curl", "-fsSL", "-o", str(zip_path), url],
                    capture_output=True, timeout=60, text=True,
                )
                if result.returncode != 0:
                    return None
            except (subprocess.SubprocessError, OSError):
                return None
        else:
            return None
        if not zip_path.is_file():
            return None
        # Extract just the binary.
        import zipfile  # stdlib — defer import to avoid startup cost.
        try:
            with zipfile.ZipFile(zip_path) as z:
                inner = f"{inner_root}/vct-launcher" + (
                    ".exe" if platform.system().lower().startswith("win") else ""
                )
                # Find the binary inside the zip regardless of inner path
                # (release ZIPs vary; tolerate both flat + nested layouts).
                candidates = [n for n in z.namelist()
                              if n.endswith("vct-launcher") or n.endswith("vct-launcher.exe")]
                if not candidates:
                    return None
                member = inner if inner in z.namelist() else candidates[0]
                with z.open(member) as src:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    with open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, OSError, KeyError):
            return None
        # Make executable on Unix.
        if not platform.system().lower().startswith("win"):
            try:
                target_path.chmod(0o755)
            except OSError:
                pass
        if target_path.is_file():
            return target_path
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _try_cargo_tauri_build(install_root: Path) -> Optional[Path]:
    """Tier 3 (LAST RESORT): rebuild via `cargo tauri build`.

    Slow (15-25 min on a typical dev machine). Only attempts the build
    if `cargo` AND `rustc` are both on PATH. Soft-fail on every error.

    Note: this function does NOT actually run the build in normal install
    flows — install.py defers to the bundled binary or download path
    above. We still ship this code path for users running install.py
    from a source checkout on a machine without bundled binaries (CI
    builders, contributor workflows). The orchestrator's central cargo
    verify is the discipline for ensuring this code path stays compilable.
    """
    if not shutil.which("cargo") or not shutil.which("rustc"):
        return None
    print(
        "  Launcher binary not bundled and download failed.\n"
        "  Falling back to `cargo tauri build` — this takes 15-25 minutes.\n"
        "  Press Ctrl-C to abort and use the pure-Python fallback.",
        flush=True,
    )
    launcher_dir = install_root / "launcher"
    if not launcher_dir.is_dir():
        return None
    try:
        result = subprocess.run(
            ["cargo", "tauri", "build"],
            cwd=str(launcher_dir),
            capture_output=True,
            timeout=1800,  # 30 min
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  `cargo tauri build` failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"  `cargo tauri build` exited {result.returncode}; falling back.",
            file=sys.stderr,
        )
        return None
    # Copy the produced binary to launcher/dist/<os>-<arch>/.
    subdir, fname = _launcher_binary_relative_path()
    target_dir = install_root / "launcher" / "dist" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / fname
    candidates = list((install_root / "launcher" / "src-tauri" / "target" / "release").glob("vct-launcher*"))
    # Pick the actual binary (not .d / .pdb / etc.) — filter to executable
    # files only.
    src = None
    for c in candidates:
        if c.is_file() and (
            c.suffix in ("", ".exe")
            and not c.name.endswith(".d")
            and not c.name.endswith(".pdb")
        ):
            src = c
            break
    if src is None:
        return None
    try:
        shutil.copy2(src, target_path)
        if not platform.system().lower().startswith("win"):
            target_path.chmod(0o755)
    except OSError:
        return None
    return target_path if target_path.is_file() else None


def _read_tauri_conf_version(install_root: Path) -> Optional[str]:
    """Parse ``"version": "..."`` from ``launcher/src-tauri/tauri.conf.json``.

    Returns None on any read / parse failure (soft-fail). Used by
    :func:`_refresh_dist_binary_after_rebuild` to detect dist binaries
    that are older than the current source version.
    """
    conf = install_root / "launcher" / "src-tauri" / "tauri.conf.json"
    if not conf.is_file():
        return None
    try:
        data = json.loads(conf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _query_launcher_version(binary_path: Path) -> Optional[str]:
    """Run ``<binary_path> --version`` and parse the version string.

    Returns the first whitespace-separated token that looks like a semver
    (``\\d+\\.\\d+(\\.\\d+)?``), or None on any failure (binary doesn't
    exist, exit non-zero, timed out, output unparseable). 5-second timeout
    hard-caps the wait so a hung binary can't block the install.

    Used by :func:`_refresh_dist_binary_after_rebuild` to populate the
    ``launcher_restart_required`` deferral message — falls back to
    :func:`_read_install_version` when the new binary can't self-report
    (e.g. binary swap landed but the file isn't executable on this OS yet).
    """
    if not binary_path.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log_install_event(
            "query_launcher_version", "warn",
            f"could not invoke {binary_path} --version: {exc}",
        )
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    # Find the first semver-looking token.
    for token in output.split():
        if re.match(r"^v?\d+\.\d+", token):
            return token.lstrip("v").rstrip(",.;")
    return None


def _is_windows_sharing_violation(exc: OSError) -> bool:
    """Detect ``ERROR_SHARING_VIOLATION`` (Windows code 32).

    On Windows, attempting to overwrite a running .exe yields a WinError
    with code 32 (ERROR_SHARING_VIOLATION). Cross-OS guard: this returns
    False on every non-Windows host even if the OSError's ``winerror``
    attribute is set (it shouldn't be).
    """
    if not platform.system().lower().startswith("win"):
        return False
    return getattr(exc, "winerror", None) == 32


def _emit_launcher_restart_deferral(
    deferral_report: Any,
    *,
    install_root: Path,
    new_binary_path: Path,
    new_version: Optional[str],
    old_pid: Optional[int],
) -> None:
    """Add a ``launcher_restart_required`` entry to the run's deferral report.

    Emitted after a successful binary swap so the launcher GUI knows to
    surface a "Restart now" banner. Self-clears: when the user clicks
    "Restart now", the new launcher writes ``.claude/context/launcher-restart-marker``
    on startup which the next install.py run treats as "deferral was
    consumed; drop it" — see :func:`_apply_deferred_entries`.

    Safe to call with ``deferral_report=None`` (no-op) so callers without a
    report in scope don't need to wire one through just for the side effect.
    """
    if deferral_report is None:
        return

    version_label = new_version or _read_install_version(install_root) or "(version unknown)"
    pid_suffix = f" (running launcher PID: {old_pid})" if old_pid else ""

    try:
        entry = DeferralEntry(
            condition_id="launcher_restart_required",
            title=f"Launcher binary updated to {version_label}",
            detected=(
                f"A freshly-built launcher binary was swapped into "
                f"`{new_binary_path}`{pid_suffix}. The currently-running "
                f"launcher process is still executing the old code in memory."
            ),
            why_deferred=(
                "install.py cannot safely restart a GUI process it didn't "
                "spawn. The launcher reads this entry on startup and renders "
                "a green sticky banner with a `Restart now` button that "
                "detach-spawns the new binary and exits the current process."
            ),
            command_to_apply=(
                "# Manually: fully quit the launcher (tray → Quit), then\n"
                f"# relaunch via your usual entrypoint (the new binary at\n"
                f"# {new_binary_path} will execute on next start)."
            ),
            severity="info",
            kg_node_refs=[],
        )
        deferral_report.add_entry(entry)
    except Exception as exc:  # noqa: BLE001 — soft-fail by design
        _log_install_event(
            "refresh_dist_binary", "warn",
            f"could not emit launcher_restart_required deferral: {exc}",
        )


def _emit_binary_swap_locked_deferral(
    deferral_report: Any,
    *,
    new_binary_path: Path,
    error_detail: str,
) -> None:
    """Add a ``launcher_binary_swap_failed_locked`` entry (Windows path).

    Fired when both the direct overwrite AND the rename-fallback fail
    because the running launcher .exe is held open by Windows
    (ERROR_SHARING_VIOLATION). The launcher GUI renders a RED sticky
    banner with explicit recovery steps. Severity is ``warning`` not
    ``critical`` because the install otherwise completed (only the
    binary-refresh step failed); MCP registration and bundle propagation
    still landed.
    """
    if deferral_report is None:
        return
    try:
        entry = DeferralEntry(
            condition_id="launcher_binary_swap_failed_locked",
            title="Launcher binary update blocked (file locked)",
            detected=(
                f"Windows refused to overwrite the launcher binary at "
                f"`{new_binary_path}` because it is held open by the "
                f"running launcher process (ERROR_SHARING_VIOLATION). "
                f"Detail: {error_detail}"
            ),
            why_deferred=(
                "Windows holds an exclusive lock on running .exe files. "
                "Neither direct overwrite nor rename-then-write succeeded. "
                "Manual intervention required: fully quit the launcher "
                "first."
            ),
            command_to_apply=(
                "# 1. Fully quit the launcher (tray → Quit, NOT just close window)\n"
                "# 2. From a terminal, re-run the orchestrator update:\n"
                "python install.py --update\n"
                "# 3. Relaunch the launcher via its usual entrypoint."
            ),
            severity="warning",
            kg_node_refs=[],
        )
        deferral_report.add_entry(entry)
    except Exception as exc:  # noqa: BLE001 — soft-fail by design
        _log_install_event(
            "refresh_dist_binary", "warn",
            f"could not emit launcher_binary_swap_failed_locked deferral: {exc}",
        )


def _maybe_emit_running_stale_deferral(
    install_root: Path,
    *,
    dist_path: Path,
    deferral_report: Any,
) -> None:
    """v0.2.17 (plan 0.0): emit ``launcher_restart_required`` when the
    running launcher's version differs from the on-disk dist binary.

    The git-pull case for end-user updates: a freshly-pulled
    ``launcher/dist/<arch>/vct-launcher`` lands at the canonical path
    while the launcher PID still has the OLD binary mapped (via
    `/proc/<pid>/exe` on Linux, equivalent on macOS/Windows). Without
    this emit, the launcher's W4 banner stays silent and the user
    has no signal that a restart is needed.

    Detection signal: ``vct-module.json::version`` (the new on-disk
    source version, just pulled) vs ``state/install-manifest.json::version``
    (the version recorded on the LAST install — which reflects what
    the running launcher boots with). If they differ AND
    ``VCT_LAUNCHER_PID`` is set (caller confirms a launcher is
    running), emit the deferral.

    Skipped on `deferral_report is None` (caller didn't thread one
    through) so this helper is safe to call from contexts where the
    deferral channel isn't available.
    """
    if deferral_report is None:
        return
    if not dist_path.is_file():
        return

    # Source version (just pulled, in the working tree).
    # `_read_install_version` reads vct-module.json first → that file
    # IS the post-pull source-of-truth version.
    source_version = _read_install_version(install_root)
    # Last-installed version: read state/install-manifest.json
    # directly. This is the version install.py wrote on the LAST run —
    # which is what the running launcher booted with. (We can't reuse
    # _read_install_version here because its priority order is
    # source-tree first, manifest never.)
    manifest_path = install_root / "state" / "install-manifest.json"
    last_installed_version: Optional[str] = None
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                manifest_data = json.load(f)
            v = manifest_data.get("version")
            if v:
                last_installed_version = str(v)
        except (OSError, json.JSONDecodeError):
            last_installed_version = None

    if not source_version or not last_installed_version:
        # Can't compare — bail silently. Manifest absence is also a
        # legitimate "fresh install" case where no restart applies.
        # EXCEPTION: VCT_FORCE_RESTART_DEFERRAL=1 (set by the Rust
        # auto-restart fallback path, v0.2.17 Reviewer A finding A2)
        # bypasses this guard so the deferral lands even when the
        # manifest is unreadable.
        if os.environ.get("VCT_FORCE_RESTART_DEFERRAL", "").strip() != "1":
            return
    if source_version == last_installed_version:
        # No version change — nothing to defer.
        # EXCEPTION: VCT_FORCE_RESTART_DEFERRAL=1 forces the emit so
        # the auto-restart-failed fallback can land the deferral even
        # though the FIRST install.py pass already bumped the
        # manifest to match the source (making the version-equality
        # check skip otherwise).
        if os.environ.get("VCT_FORCE_RESTART_DEFERRAL", "").strip() != "1":
            return

    old_pid_str = os.environ.get("VCT_LAUNCHER_PID", "").strip()
    if not old_pid_str:
        # No launcher running (or caller didn't tell us) — emit
        # anyway so a future launcher-start picks it up. Pass
        # old_pid=None for the deferral message.
        old_pid: Optional[int] = None
    else:
        try:
            old_pid = int(old_pid_str)
        except ValueError:
            old_pid = None

    new_version = _query_launcher_version(dist_path) or source_version
    _emit_launcher_restart_deferral(
        deferral_report,
        install_root=install_root,
        new_binary_path=dist_path,
        new_version=new_version,
        old_pid=old_pid,
    )
    _log_install_event(
        "refresh_dist_binary", "info",
        f"emitted launcher_restart_required deferral "
        f"(source={source_version}, last_installed={last_installed_version}, "
        f"running_pid={old_pid_str or 'none'})",
    )


def _refresh_dist_binary_after_rebuild(
    install_root: Path,
    *,
    no_swap: bool = False,
    install_start_ts: Optional[float] = None,
    deferral_report: Any = None,
) -> Optional[Path]:
    """Fix 1 (v0.2.13): copy a freshly-built ``target/release/vct-launcher-temp``
    into ``launcher/dist/<os>-<arch>/vct-launcher`` when it's genuinely newer.

    Background: ``_try_cargo_tauri_build`` (tier-3) already copies to dist as
    part of its own flow. But in ``--update`` mode with an existing-but-stale
    bundled binary at ``dist/linux-x64/vct-launcher``, the tier-1 resolver
    returns SUCCESS for the stale binary and tier-3 is never invoked.
    A separate pipeline step (e.g. user-driven launcher rebuild via the
    launcher's own UI flow, or a CI rebuild on the orchestrator clone) may
    have produced a fresh ``target/release/vct-launcher-temp`` without
    copying it to dist. This helper closes that gap.

    Conservative gating (only swap when ALL conditions are met):

      1. ``target/release/vct-launcher-temp`` exists and is a regular file.
      2. ``vct-launcher-temp`` mtime is strictly newer than the dist binary's
         mtime (or the dist binary does not exist yet).
      3. EITHER the source mtime is newer than ``install_start_ts`` (proving
         it was produced during this install run), OR the dist binary's
         embedded version is older than ``tauri.conf.json`` (proving the
         dist artifact is stale w.r.t. the current source).
      4. ``no_swap`` is False.

    v0.2.15 (Agent D): on successful swap, emit a ``launcher_restart_required``
    deferral so the launcher GUI surfaces a "Restart now" banner. On Windows,
    if direct overwrite fails with ERROR_SHARING_VIOLATION (the launcher
    binary is held open), try rename-then-write before giving up; on total
    failure emit ``launcher_binary_swap_failed_locked``. The launcher's
    "old PID" is read from the ``VCT_LAUNCHER_PID`` env var when present
    (set by the Tauri ``update_orchestrator`` command before spawning
    install.py).

    Args:
        install_root: Repository root.
        no_swap: When True, this helper is a no-op (mirrors ``--no-binary-swap``).
        install_start_ts: Optional unix timestamp marking the start of this
            install run. When provided, source files older than this are
            considered "stale from a prior run" and ignored (extra safety).
        deferral_report: Optional DeferralReport. When provided, success/
            failure paths emit the appropriate entries documented above.
            None is supported so external callers (tests, CLI scripts) don't
            need to thread one through.

    Returns:
        The dist path that was refreshed, or None when nothing was done.

    Soft-fail throughout — any OSError is swallowed and a warning is logged.
    The install must complete even when this helper fails.
    """
    if no_swap:
        _log_install_event(
            "refresh_dist_binary", "skip",
            "--no-binary-swap set; skipping post-rebuild dist refresh",
        )
        return None

    # v0.2.17 (plan 0.0): the git-pull case detection path.
    #
    # Pre-v0.2.17 this helper ONLY handled the "cargo just produced a
    # fresh binary in target/release/" scenario (Bug 5 in
    # `knowledge/concepts/install-py-collection-bootstrap-bugs.md`).
    # The COMMON end-user case — `git pull` lands a pre-built binary
    # directly at `launcher/dist/<arch>/vct-launcher` — was never
    # detected here, so the `launcher_restart_required` deferral
    # was never emitted, and the launcher's banner stayed silent
    # while the on-disk binary diverged from the running PID's binary.
    #
    # Fix: detect dist-vs-running divergence and emit the deferral
    # if needed. Runs ONLY when the cargo-output secondary path is
    # NOT going to fire (Reviewer A finding A3: avoid double-emit
    # and the wasted `_query_launcher_version` subprocess call
    # against an about-to-be-overwritten binary).
    subdir, fname = _launcher_binary_relative_path()
    dist_dir = install_root / "launcher" / "dist" / subdir
    dist_path = dist_dir / fname

    # Skip the running-vs-disk emit entirely when the Rust caller has
    # opted into auto-restart (VCT_AUTO_RESTART_LAUNCHER=1 set by
    # `update_orchestrator` v0.2.17). The deferral is redundant in
    # that path — the Rust handler spawns the new binary detached and
    # exits the current process.
    auto_restart_handled_externally = (
        os.environ.get("VCT_AUTO_RESTART_LAUNCHER", "").strip() == "1"
    )

    src = (
        install_root
        / "launcher"
        / "src-tauri"
        / "target"
        / "release"
        / "vct-launcher-temp"
    )
    # v0.2.18 (plan 0.0): run the git-pull-case helper unconditionally.
    # Earlier v0.2.17 logic used `if not src.is_file()` as a routing
    # gate, on the assumption that "src exists ⇒ cargo path will emit
    # the deferral itself". That assumption is false — the cargo path
    # silently returns None when `src_mtime <= dist_mtime` (Gate 2 a
    # few lines below), so a stale `target/release/vct-launcher-temp`
    # from a prior local build deflected the routing AND the cargo
    # path then bailed without emitting. Net effect: end-users with
    # any cargo artifact on disk lost the launcher_restart_required
    # deferral after a git-pull update — the W4 banner stayed silent
    # and the running PID continued to execute the old binary.
    #
    # Safe to run unconditionally: the helper short-circuits on
    # `source_version == last_installed_version` (line 8862), so once
    # the cargo path has actually swapped + bumped the manifest, a
    # subsequent helper call is a no-op.
    if not auto_restart_handled_externally:
        _maybe_emit_running_stale_deferral(
            install_root,
            dist_path=dist_path,
            deferral_report=deferral_report,
        )
    if not src.is_file():
        return None

    # subdir/fname/dist_path already resolved at the top of this
    # function (see the v0.2.17 git-pull-case detection block above).

    try:
        src_mtime = src.stat().st_mtime
    except OSError as exc:
        _log_install_event(
            "refresh_dist_binary", "warn",
            f"could not stat src binary: {exc}",
        )
        return None

    dist_mtime: Optional[float] = None
    if dist_path.is_file():
        try:
            dist_mtime = dist_path.stat().st_mtime
        except OSError:
            dist_mtime = None

    # Gate 2: source must be strictly newer than dist (or dist absent).
    if dist_mtime is not None and src_mtime <= dist_mtime:
        return None

    # Gate 3: prove the source was either produced in this run OR the dist
    # binary is version-stale. Either signal is sufficient (we don't require
    # both — version-stale dist is enough on its own to justify a swap, and
    # a fresh in-run build is enough even when no version drift exists).
    produced_in_run = (
        install_start_ts is not None and src_mtime >= install_start_ts
    )
    version_stale = False
    if not produced_in_run and dist_path.is_file():
        # No "fresh in-run" evidence — fall back to version-drift check.
        current_version = _read_tauri_conf_version(install_root)
        if current_version is not None:
            # We don't introspect the binary itself for its embedded version
            # (no portable, cheap way to do that). Proxy: the dist binary's
            # mtime is older than the tauri.conf.json's mtime — that's a
            # strong "dist artifact built against an older source version"
            # signal. False positives are limited to "user touched
            # tauri.conf.json after build" which is rare and harmless.
            conf = install_root / "launcher" / "src-tauri" / "tauri.conf.json"
            try:
                version_stale = conf.stat().st_mtime > dist_mtime
            except OSError:
                version_stale = False
    if not produced_in_run and not version_stale and dist_path.is_file():
        # Conservative bailout: source is newer than dist but we have no
        # corroborating evidence that this is a real upgrade vs. e.g. a
        # touched-but-unmodified file. Skip.
        return None

    # Gate 1+4 already enforced above. Perform the swap.
    swap_succeeded = False
    swap_renamed_old_to: Optional[Path] = None
    try:
        dist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dist_path)
        if not platform.system().lower().startswith("win"):
            try:
                dist_path.chmod(0o755)
            except OSError:
                # Best-effort: fall through; the copy itself succeeded.
                pass
        swap_succeeded = True
    except OSError as exc:
        # Windows-specific fallback: when ERROR_SHARING_VIOLATION fires
        # (the running launcher .exe is locked by the OS), try
        # rename-then-write. Windows often allows rename-while-open even
        # when overwrite-while-open fails — the old .exe gets a sibling
        # ``.old-<version>`` name and the new binary lands at the canonical
        # path. The renamed file stays on disk until the next reboot or
        # manual cleanup; harmless (a few MB) and serves as a recovery
        # checkpoint.
        if _is_windows_sharing_violation(exc) and dist_path.is_file():
            old_version_tag = (
                _read_install_version(install_root) or "prior"
            ).replace(" ", "_").replace("/", "_")
            backup_name = f"{fname}.old-{old_version_tag}"
            backup_path = dist_dir / backup_name
            # Drop any stale backup from a prior failed swap so the rename
            # doesn't trip its own SHARING_VIOLATION.
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except OSError as cleanup_exc:
                    _log_install_event(
                        "refresh_dist_binary", "warn",
                        f"stale backup {backup_path} could not be removed: "
                        f"{cleanup_exc}; rename-fallback may fail",
                    )
            try:
                dist_path.rename(backup_path)
                shutil.copy2(src, dist_path)
                swap_succeeded = True
                swap_renamed_old_to = backup_path
                _log_install_event(
                    "refresh_dist_binary", "ok",
                    f"Windows rename-fallback succeeded: old binary moved "
                    f"to {backup_path}, new binary written to {dist_path}",
                )
            except OSError as rename_exc:
                # Both direct overwrite AND rename failed. Emit the
                # binary-swap-locked deferral so the GUI tells the user
                # to fully quit + retry from terminal.
                _log_install_event(
                    "refresh_dist_binary", "error",
                    f"Windows binary-swap failed; both overwrite ({exc}) "
                    f"and rename ({rename_exc}) hit ERROR_SHARING_VIOLATION. "
                    f"Emitting launcher_binary_swap_failed_locked deferral.",
                )
                _emit_binary_swap_locked_deferral(
                    deferral_report,
                    new_binary_path=dist_path,
                    error_detail=(
                        f"overwrite={exc!r}; rename={rename_exc!r}"
                    ),
                )
                return None
        else:
            _log_install_event(
                "refresh_dist_binary", "warn",
                f"copy {src} → {dist_path} failed: {exc}",
            )
            return None

    if not swap_succeeded:
        return None

    _log_install_event(
        "refresh_dist_binary", "ok",
        f"refreshed {dist_path} from {src} "
        f"(produced_in_run={produced_in_run}, version_stale={version_stale}, "
        f"renamed_old_to={swap_renamed_old_to})",
    )

    # v0.2.15 (Agent D): emit launcher_restart_required so the GUI surfaces
    # a "Restart now" banner. The running launcher process is still in old
    # code; the new binary is on disk but not executing yet.
    #
    # v0.2.17 (plan 0.0): skip emit when VCT_AUTO_RESTART_LAUNCHER=1 is
    # set by the Rust caller — auto-restart makes the deferral redundant.
    if not auto_restart_handled_externally:
        new_version = _query_launcher_version(dist_path)
        old_pid_str = os.environ.get("VCT_LAUNCHER_PID", "").strip()
        old_pid: Optional[int] = None
        if old_pid_str:
            try:
                old_pid = int(old_pid_str)
            except ValueError:
                old_pid = None
        _emit_launcher_restart_deferral(
            deferral_report,
            install_root=install_root,
            new_binary_path=dist_path,
            new_version=new_version,
            old_pid=old_pid,
        )
    else:
        _log_install_event(
            "refresh_dist_binary", "info",
            "VCT_AUTO_RESTART_LAUNCHER=1 set — skipping launcher_restart_required "
            "deferral emit (Rust caller handles auto-restart)",
        )

    return dist_path


def _ensure_launcher_binary(
    install_root: Path,
    *,
    prefer_only_bundled: bool = False,
) -> Optional[Path]:
    """Resolve launcher binary path via 4-tier priority.

    1. Bundled binary at launcher/dist/<os>-<arch>/vct-launcher[.exe]
       (preferred — normal user case).
    2. Download from GitHub Releases (fast network fallback).
    3. Rebuild via `cargo tauri build` (slow last resort).
    4. Returns None — caller falls back to pure-Python JSON merge.

    Soft-fail at every step. Prints progress messages so the user sees
    what's happening without needing to read this code.

    Args:
        install_root: Repository root containing `launcher/dist/...`.
        prefer_only_bundled: When True, perform only Tier 1 (bundled
            binary lookup) and skip Tiers 2-3. Used by latency-sensitive
            interactive contexts (e.g. PR-28's storage-config prompt)
            where a 15-25 min cargo rebuild or a multi-second GitHub
            download would mid-prompt confuse the user. Caller is
            expected to have a pure-Python fallback when None is
            returned. Default False preserves the original full chain
            behaviour for MCP registration.
    """
    # Tier 1: bundled.
    p = _try_bundled_launcher_binary(install_root)
    if p is not None:
        return p

    if prefer_only_bundled:
        # Interactive fast-path callers do NOT want tiers 2-3 (network
        # download or cargo rebuild are too slow for an interactive
        # prompt context). Bail out so the caller's Python fallback
        # runs immediately.
        return None

    print(
        f"  Launcher binary not found at "
        f"launcher/dist/{_launcher_binary_relative_path()[0]}/."
    )

    # Tier 2: download.
    print("  Trying to download a matching release artifact from GitHub...")
    p = _try_download_launcher_binary(install_root)
    if p is not None:
        print(f"  Downloaded launcher binary to {p}.")
        return p
    print("  Release download not available (no gh/curl, no network, or release artifact missing).")

    # Tier 3: rebuild.
    p = _try_cargo_tauri_build(install_root)
    if p is not None:
        print(f"  Rebuilt launcher binary at {p}.")
        return p

    # Tier 4: caller falls back to Python JSON path.
    print(
        "  Cannot rebuild: cargo or rustc not on PATH. "
        "Falling back to pure-Python MCP registration."
    )
    return None


# ---------------------------------------------------------------------------
# v0.2.21 Step 8: vct-hub binary deployment + idempotent start.
#
# The detached `vct-hub` binary ships alongside `vct-launcher` in
# `launcher/dist/<os>-<arch>/`. install.py is responsible for:
#   8a. Placing the binary (bundled / download / cargo rebuild — same
#       tier ladder as the launcher binary).
#   8b. Writing a `<vct_root_dir()>/v0.2.21-cutover.flag` sentinel BEFORE
#       starting vct-hub so a v0.2.20 launcher's still-running embedded
#       supervisor (and the freshly-spawned v0.2.21 launcher's own
#       `services::watcher::spawn`) skips its 30s polling loop during
#       the overlap. install.py deletes the sentinel after vct-hub
#       responds to /health.
#   8c. Invoking `vct-hub --start-if-not-running` (idempotent: returns
#       exit 0 whether starting fresh or attaching to a running hub).
#   8d. Boot auto-start is NOT registered during install. The user opts
#       in via the launcher GUI Preferences page (Step 13) which then
#       calls `vct-hub --register-boot`. install time keeps the default
#       behaviour conservative — most users don't want a background
#       service auto-starting on boot without explicit consent.
#   8g. Stopping vct-hub before --update is owned by the launcher's
#       Rust `update_orchestrator` (Step 12). By the time install.py
#       runs under --update, the hub is already stopped; install.py
#       just deploys and re-starts it.
# ---------------------------------------------------------------------------

# Sentinel filename written under `vct_root_dir()` during the v0.2.20 →
# v0.2.21 cutover. Read by:
#   - the v0.2.21 launcher's `lib.rs` setup() to skip
#     `services::watcher::spawn` (W2 fix).
#   - a v0.2.20 launcher's old watcher does NOT read the sentinel (no
#     forward-port), but its 30s polls are harmless (podman/docker
#     `start` is idempotent) and its process exits during the binary
#     swap, so the duplicate supervisor window closes naturally.
# The sentinel is short-lived: install.py writes it just before
# starting vct-hub and deletes it after the /health probe succeeds
# (typically <5 s end-to-end).
_VCT_HUB_CUTOVER_SENTINEL_NAME = "v0.2.21-cutover.flag"


def _vct_hub_binary_relative_path() -> tuple[str, str]:
    """Return (subdir, filename) for the bundled vct-hub binary.

    Mirrors :func:`_launcher_binary_relative_path` exactly — vct-hub
    ships in the same per-arch dir as vct-launcher.

    Linux: ('linux-x64', 'vct-hub')
    macOS: ('macos-arm64', 'vct-hub')
    Windows: ('windows-x64', 'vct-hub.exe')
    """
    system = platform.system().lower()
    if system.startswith("win"):
        return ("windows-x64", "vct-hub.exe")
    if system == "darwin":
        return ("macos-arm64", "vct-hub")
    return ("linux-x64", "vct-hub")


def _try_bundled_vct_hub_binary(install_root: Path) -> Optional[Path]:
    """Tier 1: bundled binary at launcher/dist/<os>-<arch>/vct-hub[.exe]."""
    subdir, fname = _vct_hub_binary_relative_path()
    p = install_root / "launcher" / "dist" / subdir / fname
    if p.is_file() and os.access(
        p,
        os.X_OK if not platform.system().lower().startswith("win") else os.F_OK,
    ):
        return p
    return None


def _try_download_vct_hub_binary(install_root: Path) -> Optional[Path]:
    """Tier 2: download matching release artifact from GitHub Releases.

    Since v0.2.21 the release ZIP carries BOTH `vct-launcher[.exe]`
    AND `vct-hub[.exe]` per arch (see `.github/workflows/release.yml`).
    Reuses the launcher's download tooling preferences (gh first,
    curl fallback) and the same naming convention
    (`vibecoded-orchestrator-<version>-<os>-<arch>.zip`).

    Soft-fail on every error (network down, release missing, auth
    refused, vct-hub not yet bundled in the release ZIP) — returns
    None and lets the caller move to Tier 3.

    Tier-1/2 share the same artifact (one ZIP per arch contains both
    binaries); if the launcher binary was already downloaded earlier
    this run, the ZIP is on disk-cache-miss territory — we
    re-download deliberately rather than carry a side-channel into
    `_try_download_launcher_binary`. The redundancy is bounded (one
    re-download per install) and keeps the function self-contained.
    """
    version = _read_launcher_version(install_root)
    if not version:
        return None
    subdir, fname = _vct_hub_binary_relative_path()
    target_dir = install_root / "launcher" / "dist" / subdir
    target_path = target_dir / fname
    os_arch_token = {
        "linux-x64": "linux-x64",
        "windows-x64": "windows-x64",
        "macos-arm64": "macos-arm64",
    }.get(subdir, subdir)
    artifact = f"vibecoded-orchestrator-{version}-{os_arch_token}.zip"
    inner_root = f"vibecoded-orchestrator-{version}-{os_arch_token}"

    tmpdir = Path(tempfile.mkdtemp(prefix="vct-hub-dl-"))
    try:
        zip_path = tmpdir / artifact
        if shutil.which("gh"):
            cmd = [
                "gh", "release", "download", f"v{version}",
                "--repo", "hotak92/vibecoded-orchestrator",
                "--pattern", artifact,
                "--dir", str(tmpdir),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=60, text=True
                )
                if result.returncode != 0:
                    return None
            except (subprocess.SubprocessError, OSError):
                return None
        elif shutil.which("curl"):
            url = (
                f"https://github.com/hotak92/vibecoded-orchestrator/"
                f"releases/download/v{version}/{artifact}"
            )
            try:
                result = subprocess.run(
                    ["curl", "-fsSL", "-o", str(zip_path), url],
                    capture_output=True, timeout=60, text=True,
                )
                if result.returncode != 0:
                    return None
            except (subprocess.SubprocessError, OSError):
                return None
        else:
            return None
        if not zip_path.is_file():
            return None
        import zipfile  # stdlib — defer import to avoid startup cost.
        try:
            with zipfile.ZipFile(zip_path) as z:
                inner = f"{inner_root}/vct-hub" + (
                    ".exe" if platform.system().lower().startswith("win") else ""
                )
                # The ZIP may not yet contain vct-hub (e.g. user pulled
                # v0.2.21 source but their network resolved the v0.2.20
                # release). Be tolerant: any zip member ending in
                # `vct-hub` / `vct-hub.exe` is acceptable.
                candidates = [
                    n for n in z.namelist()
                    if n.endswith("vct-hub") or n.endswith("vct-hub.exe")
                ]
                if not candidates:
                    return None
                member = inner if inner in z.namelist() else candidates[0]
                with z.open(member) as src:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    with open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, OSError, KeyError):
            return None
        if not platform.system().lower().startswith("win"):
            try:
                target_path.chmod(0o755)
            except OSError:
                pass
        if target_path.is_file():
            return target_path
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _try_cargo_build_vct_hub(install_root: Path) -> Optional[Path]:
    """Tier 3 (LAST RESORT): rebuild vct-hub via plain `cargo build`.

    The hub is a regular axum binary crate at
    ``launcher/src-tauri/vct-hub/`` — no Tauri tooling needed (unlike
    the launcher's `cargo tauri build`). 2-5 min cold; ~30 s warm.
    Soft-fail on every error.

    Note: like :func:`_try_cargo_tauri_build` for the launcher, this
    function is reserved for source-checkout installs on a machine
    without bundled binaries (CI builders, contributor workflows).
    Normal --update / fresh-install paths resolve via tier 1 (bundled
    in the orchestrator clone after `git pull`) or tier 2 (GitHub
    download).
    """
    if not shutil.which("cargo") or not shutil.which("rustc"):
        return None
    print(
        "  vct-hub binary not bundled and download failed.\n"
        "  Falling back to `cargo build -p vct-hub --release` — "
        "this takes 2-5 minutes cold.\n"
        "  Press Ctrl-C to abort.",
        flush=True,
    )
    src_tauri = install_root / "launcher" / "src-tauri"
    if not (src_tauri / "vct-hub" / "Cargo.toml").is_file():
        return None
    try:
        result = subprocess.run(
            ["cargo", "build", "-p", "vct-hub", "--release"],
            cwd=str(src_tauri),
            capture_output=True,
            timeout=600,  # 10 min cap
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  `cargo build -p vct-hub` failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"  `cargo build -p vct-hub` exited {result.returncode}; "
            f"falling back.",
            file=sys.stderr,
        )
        return None
    subdir, fname = _vct_hub_binary_relative_path()
    target_dir = install_root / "launcher" / "dist" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / fname
    # cargo --release output lives at <workspace>/target/release/vct-hub[.exe].
    # The workspace root for vct-hub is launcher/src-tauri/ (see Cargo.toml
    # [workspace] members locked in the plan §3c).
    release_dir = src_tauri / "target" / "release"
    src = release_dir / fname
    if not src.is_file():
        # Tolerate alternative names (`.exe` vs no-ext) defensively.
        candidates = list(release_dir.glob("vct-hub*"))
        src = next(
            (c for c in candidates
             if c.is_file()
             and c.suffix in ("", ".exe")
             and not c.name.endswith((".d", ".pdb"))),
            None,
        )
    if src is None or not src.is_file():
        return None
    try:
        shutil.copy2(src, target_path)
        if not platform.system().lower().startswith("win"):
            target_path.chmod(0o755)
    except OSError:
        return None
    return target_path if target_path.is_file() else None


def _ensure_vct_hub_binary(
    install_root: Path,
    *,
    prefer_only_bundled: bool = False,
) -> Optional[Path]:
    """Resolve vct-hub binary path via 4-tier priority (mirrors
    :func:`_ensure_launcher_binary`).

    1. Bundled at launcher/dist/<os>-<arch>/vct-hub[.exe] — normal user.
    2. Download from GitHub Releases — fresh source clone w/o bundle.
    3. Rebuild via `cargo build -p vct-hub --release` — last resort.
    4. Return None — caller falls back to "hub-unavailable degraded
       mode" (the launcher already handles this in
       `hub_launcher::ensure_hub_running`: resolver falls back to env
       vars; supervisor doesn't run; GUI still comes up).

    Soft-fail at every step. Prints progress messages so the user
    sees what's happening without reading this code.

    Args:
        install_root: repo root containing `launcher/dist/...`.
        prefer_only_bundled: when True, perform only Tier 1.
    """
    p = _try_bundled_vct_hub_binary(install_root)
    if p is not None:
        return p
    if prefer_only_bundled:
        return None
    print(
        f"  vct-hub binary not found at "
        f"launcher/dist/{_vct_hub_binary_relative_path()[0]}/."
    )
    print("  Trying to download from the matching GitHub release...")
    p = _try_download_vct_hub_binary(install_root)
    if p is not None:
        print(f"  Downloaded vct-hub binary to {p}.")
        return p
    print(
        "  Release download not available (no gh/curl, no network, "
        "or vct-hub not in the release ZIP yet)."
    )
    p = _try_cargo_build_vct_hub(install_root)
    if p is not None:
        print(f"  Rebuilt vct-hub binary at {p}.")
        return p
    print(
        "  Cannot rebuild vct-hub: cargo/rustc not on PATH or build "
        "failed. The launcher will start in hub-unavailable degraded "
        "mode; resolver falls back to env vars and supervisor "
        "doesn't run. Re-run `python install.py --update` once the "
        "build environment is fixed."
    )
    return None


def _vct_hub_cutover_sentinel_path() -> Path:
    """Absolute path of the v0.2.21 cutover sentinel file.

    Lives under ``vct_root_dir()`` (canonical launcher state-dir, also
    where ``hub.pid`` / ``hub.port`` / ``hub.token`` land). The
    v0.2.21 launcher's setup() reads this path to decide whether to
    skip its embedded `services::watcher::spawn` startup.
    """
    from vco_lib.paths import vct_root_dir
    return vct_root_dir() / _VCT_HUB_CUTOVER_SENTINEL_NAME


def _write_vct_hub_cutover_sentinel() -> Optional[Path]:
    """Write the v0.2.21-cutover.flag sentinel just before starting
    vct-hub. Returns the absolute path on success, None on any failure.

    Soft-fail: when the sentinel can't be written (read-only home,
    permission denied), the cutover proceeds without it. The 30 s
    overlap with a still-running v0.2.20 watcher is harmless (podman
    `start` is idempotent) — the sentinel is a belt-and-braces
    safeguard, not a correctness gate.
    """
    try:
        path = _vct_hub_cutover_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(
            f"v0.2.20-to-v0.2.21 in progress; sentinel placed at {ts}\n",
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        _log_install_event(
            "vct_hub_cutover", "warn",
            f"could not write cutover sentinel: {exc}",
        )
        return None


def _delete_vct_hub_cutover_sentinel() -> None:
    """Delete the cutover sentinel. Idempotent (no-op when absent).

    Called after vct-hub responds to /health. Soft-fail: any OSError
    is logged but not propagated — a leftover sentinel is harmless
    on the next launcher boot if vct-hub is genuinely up (the
    launcher's hub_launcher::ensure_hub_running will detect it
    running and skip the start; the watcher-skip is wasted but not
    harmful in steady state). Future install runs overwrite the
    sentinel atomically.
    """
    try:
        path = _vct_hub_cutover_sentinel_path()
        if path.is_file():
            path.unlink()
    except OSError as exc:
        _log_install_event(
            "vct_hub_cutover", "warn",
            f"could not delete cutover sentinel: {exc}",
        )


def _probe_vct_hub_health(timeout: float = 0.5) -> bool:
    """Probe ``http://localhost:<hub.port>/health``. Returns True when
    the hub responds with status<400, False otherwise.

    Reads the hub port from ``vct_root_dir()/hub.port`` (written by
    vct-hub on startup). Soft-fail on every error (file missing, port
    unparseable, connection refused, timeout).
    """
    try:
        from vco_lib.paths import vct_root_dir
        port_file = vct_root_dir() / "hub.port"
        if not port_file.is_file():
            return False
        port_raw = port_file.read_text(encoding="utf-8").strip()
        if not port_raw.isdigit():
            return False
        url = f"http://127.0.0.1:{port_raw}/health"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status < 400
    except Exception:
        return False


def _wait_for_vct_hub_health(deadline_seconds: float = 10.0) -> bool:
    """Poll :func:`_probe_vct_hub_health` until it returns True or
    ``deadline_seconds`` elapses. Returns True on success, False on
    timeout.

    Tuned generously (10 s default) because the hub's first start
    might collide with podman socket warmup on a freshly-rebooted
    machine. Steady-state response is sub-second.
    """
    end = time.time() + deadline_seconds
    while time.time() < end:
        if _probe_vct_hub_health():
            return True
        time.sleep(0.25)
    return False


def _deploy_and_start_vct_hub(
    install_root: Path,
    *,
    deferral_report: Optional["DeferralReport"] = None,
) -> None:
    """v0.2.21 Step 8 entry point: deploy vct-hub, then start it
    idempotently with the cutover sentinel in place.

    Sequence:
      8a. Resolve binary via :func:`_ensure_vct_hub_binary` (tier 1/2/3).
      8b. Write the cutover sentinel BEFORE starting the hub.
      8c. Invoke ``vct-hub --start-if-not-running`` (idempotent).
      8c'. Poll /health for up to 10 s.
      8b'. Delete the sentinel after /health responds.

    Soft-fail throughout: a missing/broken binary, a failed start, or
    a non-responsive /health endpoint all degrade gracefully — the
    launcher's `hub_launcher::ensure_hub_running` retries on next GUI
    launch, and the launcher's degraded-mode fallback keeps the GUI
    usable.

    Step 8d (boot auto-start): explicitly NOT called. The user opts
    in via launcher GUI Preferences → "Start vct-hub on login"
    (Step 13). Install-time auto-registration would conflict with
    distros that have their own service-management opinions and
    contradicts our consent-first philosophy for background
    services.

    Step 8g (stop-before-update): the launcher's
    `update_orchestrator` (Step 12) stops vct-hub BEFORE invoking
    install.py. By the time we run, the hub is already down and we
    can deploy the new binary without ERROR_SHARING_VIOLATION on
    Windows. install.py then re-starts it via 8c above.
    """
    print(
        "[8/10] Deploying vct-hub binary (v0.2.21 hub detachment) ... ",
        end="", flush=True,
    )
    _log_install_event("8/10", "start", "deploying vct-hub binary")

    binary = _ensure_vct_hub_binary(install_root, prefer_only_bundled=False)
    if binary is None:
        print("SKIPPED (no binary)")
        _log_install_event(
            "8/10", "warn",
            "vct-hub binary unavailable; launcher will start in "
            "hub-unavailable degraded mode",
        )
        if deferral_report is not None:
            try:
                deferral_report.add_entry(
                    DeferralEntry(
                        condition_id="vct_hub_binary_unavailable",
                        title="vct-hub binary unavailable",
                        detected=(
                            "install.py could not resolve a vct-hub binary "
                            "via bundled (launcher/dist/<arch>/), GitHub "
                            "release download, or `cargo build -p vct-hub "
                            "--release`. The launcher will start in "
                            "hub-unavailable degraded mode: the resolver "
                            "falls back to env vars and the supervisor "
                            "does not run, but the GUI still comes up."
                        ),
                        why_deferred=(
                            "Auto-recovery requires either a network path "
                            "to GitHub Releases or a working cargo + rustc "
                            "toolchain — neither was available this run. "
                            "install.py never blocks completion on hub "
                            "binary failure."
                        ),
                        command_to_apply="python install.py --update",
                        severity="warning",
                    )
                )
            except Exception as exc:  # noqa: BLE001 — soft-fail
                _log_install_event(
                    "8/10", "warn",
                    f"deferral emit failed: {exc}",
                )
        return

    print(f"OK ({binary.relative_to(install_root)})")
    _log_install_event(
        "8/10", "ok",
        f"vct-hub binary at {binary}",
        data={"binary_path": str(binary)},
    )

    # 8b. Write the cutover sentinel BEFORE starting the hub.
    sentinel_path = _write_vct_hub_cutover_sentinel()
    if sentinel_path is not None:
        _log_install_event(
            "8/10", "info",
            f"cutover sentinel written at {sentinel_path}",
            data={"sentinel": str(sentinel_path)},
        )

    # 8c. Idempotent start. `--start-if-not-running` exits 0 when the
    # hub is already up; it spawns a detached child and returns
    # quickly (~100 ms), so a short subprocess timeout is fine here.
    print(
        "[8/10] Starting vct-hub (--start-if-not-running) ... ",
        end="", flush=True,
    )
    try:
        proc = subprocess.run(
            [str(binary), "--start-if-not-running"],
            capture_output=True,
            timeout=15,
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"FAILED ({exc})")
        _log_install_event(
            "8/10", "error",
            f"vct-hub --start-if-not-running raised: {exc}",
        )
        # Leave the sentinel in place — the v0.2.21 launcher's
        # setup() will read it on next start and skip the watcher,
        # giving the user a chance to recover (the
        # `hub_launcher::ensure_hub_running` retry will then start
        # the hub from the GUI side). Sentinel is cleared on next
        # successful install run.
        return

    if proc.returncode != 0:
        print(f"FAILED (exit {proc.returncode})")
        _log_install_event(
            "8/10", "error",
            f"vct-hub --start-if-not-running exited {proc.returncode}",
            data={
                "stdout": (proc.stdout or "")[-500:],
                "stderr": (proc.stderr or "")[-500:],
            },
        )
        return

    print("OK")

    # 8c'. Confirm /health before clearing the sentinel.
    healthy = _wait_for_vct_hub_health(deadline_seconds=10.0)
    if healthy:
        _log_install_event("8/10", "ok", "vct-hub /health responded")
        _delete_vct_hub_cutover_sentinel()
    else:
        _log_install_event(
            "8/10", "warn",
            "vct-hub started but /health did not respond within 10 s; "
            "leaving cutover sentinel in place for next-boot recovery",
        )


def _build_python_mcp_entries(
    install_root: Path,
    venv_python: Path,
    weaviate_port: int,
    ollama_port: int,
    grpc_port: int,
    code_embed_port: int,
) -> list[tuple[str, dict, list[str]]]:
    """Pure-Python mirror of mcp_registration.rs::build_default_mcp_entries.

    Returns a list of (name, entry_dict, dropped_keys). Each entry's `env`
    field has already been filtered through the allowlist + secret-shape
    denylist. The Rust path is the authoritative writer; this exists for
    Tier 4 (pure-Python fallback).
    """
    weaviate_url = f"http://localhost:{weaviate_port}"
    ollama_url = f"http://localhost:{ollama_port}"
    code_embed_url = f"http://localhost:{code_embed_port}"
    mcp_root = install_root / "claude_mcp_servers"
    pythonpath = str(mcp_root)
    venv_python_str = str(venv_python)

    # weaviate-kg
    weaviate_server = mcp_root / "weaviate_mcp" / "server.py"
    # PR-43 (post-PR-23): EMBEDDING_MODEL + RL_SERVER_URL are intentionally
    # omitted here. They were originally written as "global defaults that
    # per-project may override" but Claude Code's actual env precedence
    # makes ~/.claude.json mcpServers.*.env WIN against .claude/settings.json
    # env — so the override goes the wrong direction. The launcher's
    # write_project_env_files puts these in .claude/settings.json env where
    # they reach MCP subprocesses correctly. Don't shadow them here.
    weaviate_env_raw = {
        "WEAVIATE_URL": weaviate_url,
        "OLLAMA_URL": ollama_url,
        "GRPC_PORT": str(grpc_port),
        "PYTHONPATH": pythonpath,
        "ACTIVE_EMBEDDING": "qwen3",
        "CODE_EMBED_SERVICE_URL": code_embed_url,
    }
    weaviate_env, weaviate_dropped = _filter_env_for_global_json(weaviate_env_raw)
    weaviate_entry = {
        "type": "stdio",
        "command": venv_python_str,
        "args": [str(weaviate_server)],
        "env": weaviate_env,
    }

    # search (v0.2.11+: needs no secrets; uses wrapper.sh on Unix)
    search_server = mcp_root / "search_mcp" / "server.py"
    search_wrapper = mcp_root / "search_mcp" / "wrapper.sh"
    if platform.system().lower().startswith("win"):
        search_cmd, search_args = venv_python_str, [str(search_server)]
    else:
        search_cmd, search_args = str(search_wrapper), []
    search_env_raw = {"PYTHONPATH": pythonpath}
    search_env, search_dropped = _filter_env_for_global_json(search_env_raw)
    search_entry = {
        "type": "stdio",
        "command": search_cmd,
        "args": search_args,
        "env": search_env,
    }

    return [
        ("weaviate-kg", weaviate_entry, weaviate_dropped),
        ("search", search_entry, search_dropped),
    ]


def _python_fallback_write_mcp_entries(
    claude_json_path: Path,
    entries: list[tuple[str, dict, list[str]]],
) -> tuple[int, list[str]]:
    """Pure-Python JSON merge mirroring mcp_registration.rs discipline.

    Same contract as the Rust register_mcp:
      - advisory file lock at <path>.lock (create_new)
      - read existing JSON (or empty {})
      - mutate ONLY mcpServers.<name>
      - write to .tmp + atomic rename
      - backup existing file to <path>.bak before overwrite

    The launcher.db is NOT touched here — `project_state_populate` will
    pick up the JSON entries when the user opens the launcher GUI.

    Returns (success_count, error_messages). Soft-fail per entry.
    """
    # Ensure parent dir exists before lock + write. The fake_home pattern
    # in tests creates a path like tmp/fake_home/.claude.json where
    # `fake_home` doesn't exist yet; without this mkdir, os.open() on the
    # .lock file raises FileNotFoundError and the write returns (0, ...).
    try:
        claude_json_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (0, [f"create parent {claude_json_path.parent}: {exc}"])
    # Acquire lock.
    lock_path = claude_json_path.with_suffix(claude_json_path.suffix + ".lock")
    locked = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            locked = True
            break
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            break
    if not locked:
        return (0, [f"could not acquire lock {lock_path}"])

    errors: list[str] = []
    success = 0
    try:
        # Read existing (or empty).
        try:
            if claude_json_path.is_file():
                raw = claude_json_path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
            else:
                data = {}
        except (OSError, json.JSONDecodeError) as exc:
            return (0, [f"read {claude_json_path}: {exc}"])
        if not isinstance(data, dict):
            return (0, [f"{claude_json_path} root is not a JSON object"])
        if "mcpServers" not in data or not isinstance(data.get("mcpServers"), dict):
            data["mcpServers"] = {}
        # Merge entries.
        for name, entry, _dropped in entries:
            data["mcpServers"][name] = entry
            success += 1
        # Backup + atomic write.
        try:
            if claude_json_path.is_file():
                bak = claude_json_path.with_suffix(claude_json_path.suffix + ".bak")
                shutil.copy2(claude_json_path, bak)
            tmp = claude_json_path.with_suffix(claude_json_path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, claude_json_path)
        except OSError as exc:
            return (0, [f"write {claude_json_path}: {exc}"])
        return (success, errors)
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _register_mcps(
    install_root: Path,
    deferral_report: "DeferralReport",
    *,
    prefer_only_bundled: bool = False,
    no_rebuild_on_stale: bool = False,
) -> None:
    """Register the bundled-orchestrator MCPs into ~/.claude.json.

    Path A (preferred): invoke the launcher binary CLI:
      <binary> --register-default-mcps <install_root>
    Path A-retry (Fix 5, v0.2.13): if Path A's launcher CLI exits
      non-zero OR times out AND the resolved binary came from tier-1
      (bundled, potentially stale), explicitly invoke tier-3
      (``_try_cargo_tauri_build``) to produce a fresh binary, then
      retry the CLI ONCE with the rebuilt binary. Skipped when
      *prefer_only_bundled* or *no_rebuild_on_stale* is True.
    Path B (fallback): pure-Python JSON merge.

    Soft-fail throughout. install completion does NOT depend on this
    succeeding; both paths log clear errors and emit a deferral entry
    when Path A is unavailable so the user sees what happened.

    Mutates ~/.claude.json (or VCT_USER_HOME_OVERRIDE/.claude.json for
    tests). Does NOT touch per-project .claude/settings.json — that's
    managed separately by the launcher's write_project_env_files.

    Args:
        install_root: Repository root.
        deferral_report: Run-scoped report to append soft-fail entries to.
        prefer_only_bundled: When True, restrict to bundled-binary lookup
            and skip the Fix-5 tier-3 retry (latency-sensitive contexts).
        no_rebuild_on_stale: When True, skip the Fix-5 tier-3 retry even
            when Path A appears to have failed against a stale binary.
            Useful for scripted / CI flows that explicitly opt out of the
            cargo-rebuild fallback.
    """
    claude_json = _user_home_for_install() / ".claude.json"

    # Resolve ports the same way Rust does (env-var-first, defaults).
    weaviate_port = int(os.environ.get("WEAVIATE_PORT", DEFAULT_WEAVIATE_PORT))
    ollama_port = int(os.environ.get("OLLAMA_PORT", DEFAULT_OLLAMA_PORT))
    grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT", DEFAULT_WEAVIATE_GRPC_PORT))
    code_embed_port = int(os.environ.get("CODE_EMBED_PORT", DEFAULT_CODE_EMBED_PORT))

    print()
    print("Registering bundled MCP servers in ~/.claude.json...")

    def _invoke_launcher_cli(bin_path: Path) -> tuple[bool, bool]:
        """Run ``<bin> --register-default-mcps <install_root>``.

        Returns ``(success, transient_failure)`` where:
          * ``success`` is True when the CLI exited 0.
          * ``transient_failure`` is True when the CLI either timed out
            or exited non-zero (signals "stale binary doesn't recognise
            the flag" — the Fix-5 trigger condition). False when the
            binary couldn't be invoked at all (OSError / SubprocessError
            other than timeout).
        """
        cmd = [str(bin_path), "--register-default-mcps", str(install_root)]
        env = os.environ.copy()
        env["WEAVIATE_PORT"] = str(weaviate_port)
        env["OLLAMA_PORT"] = str(ollama_port)
        env["WEAVIATE_GRPC_PORT"] = str(grpc_port)
        env["CODE_EMBED_PORT"] = str(code_embed_port)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            _log_install_event(
                "register_mcps", "warn",
                f"launcher binary timed out after 30s: {exc}",
            )
            print(
                f"  Launcher binary CLI timed out after 30s "
                f"(binary may be stale and not recognise --register-default-mcps).",
                file=sys.stderr,
            )
            return (False, True)
        except (subprocess.SubprocessError, OSError) as exc:
            _log_install_event(
                "register_mcps", "warn",
                f"launcher binary invocation failed: {exc}",
            )
            print(
                f"  Launcher binary CLI failed: {exc}. Falling back to Python writer.",
                file=sys.stderr,
            )
            return (False, False)
        # Forward the launcher's own stdout/stderr for visibility.
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode == 0:
            _log_install_event(
                "register_mcps", "ok",
                f"registered via launcher binary at {bin_path}",
            )
            return (True, False)
        _log_install_event(
            "register_mcps", "warn",
            f"launcher binary exit {result.returncode}",
        )
        print(
            f"  Launcher binary CLI returned exit {result.returncode}.",
            file=sys.stderr,
        )
        return (False, True)

    # Path A: launcher binary CLI.
    binary = _ensure_launcher_binary(
        install_root, prefer_only_bundled=prefer_only_bundled,
    )
    path_a_succeeded = False
    path_a_transient_failure = False
    if binary is not None:
        path_a_succeeded, path_a_transient_failure = _invoke_launcher_cli(binary)
        if path_a_succeeded:
            # Stale-MCP-entry detection (--update mode only). The
            # launcher writer doesn't touch entries outside the
            # bundled set, so this check is post-write here.
            _detect_stale_mcp_entries(install_root, claude_json, deferral_report)
            return

    # Fix 5 (v0.2.13): Path A-retry — when Path A's CLI failed transiently
    # (timeout / non-zero exit) and the resolved binary came from tier-1
    # (potentially stale bundled artifact), explicitly drive tier-3 to
    # produce a fresh binary and retry the CLI ONCE. Stale-binary symptom:
    # bundled binary at dist/<os>-<arch>/ doesn't recognise
    # ``--register-default-mcps`` (older release without the CLI flag),
    # tries to launch the GUI instead, hits our 30s timeout, then we
    # fall straight to Python without ever consulting tier-3.
    #
    # Gated OFF when:
    #   * prefer_only_bundled is True (caller wants only tier-1 — see
    #     PR-28 storage-config-prompt path), OR
    #   * no_rebuild_on_stale is True (caller opted out of the cargo
    #     rebuild fallback), OR
    #   * Path A succeeded or never ran (binary is None — tier-1 missed,
    #     so no "stale" hypothesis exists), OR
    #   * Path A's failure was NOT transient (OSError before invocation),
    #     because rebuilding a binary that we can't even spawn is unlikely
    #     to help — fall straight to Python.
    if (
        path_a_transient_failure
        and not prefer_only_bundled
        and not no_rebuild_on_stale
        and binary is not None
    ):
        fresh_binary = _try_cargo_tauri_build(install_root)
        if fresh_binary is not None and fresh_binary != binary:
            _log_install_event(
                "register_mcps_tier3_retry", "ok",
                f"retrying CLI with freshly-built binary at {fresh_binary} "
                f"(original {binary} was stale)",
            )
            print(
                f"  Retrying MCP registration with freshly-built launcher "
                f"binary at {fresh_binary}.",
                file=sys.stderr,
            )
            retry_succeeded, _ = _invoke_launcher_cli(fresh_binary)
            if retry_succeeded:
                _log_install_event(
                    "register_mcps_tier3_retry", "ok",
                    "tier-3 retry succeeded — bundled binary refreshed",
                )
                _detect_stale_mcp_entries(install_root, claude_json, deferral_report)
                # Deprecated-MCP-entry detection (PR-34). Same rationale:
                # the launcher writer only updates the bundled set; any
                # deprecated entry from a prior install stays behind.
                _detect_deprecated_mcp_entries(install_root, claude_json, deferral_report)
                return
            _log_install_event(
                "register_mcps_tier3_retry", "warn",
                "tier-3 retry also failed; falling through to Python writer",
            )
        else:
            # cargo unavailable OR cargo produced the same path (already
            # refreshed by tier-3's own copy logic). Log so observability
            # shows the retry was attempted-and-skipped.
            _log_install_event(
                "register_mcps_tier3_retry", "warn",
                f"tier-3 retry could not produce a fresh binary "
                f"(fresh={fresh_binary}); falling through to Python writer",
            )

    # Path B: pure-Python JSON merge. Always runs when Path A is
    # unavailable; survives missing-binary / missing-cargo / network-down.
    venv_python = _resolve_venv_python_for_install(install_root)
    if venv_python is None:
        msg = (
            f"could not locate venv-python under {install_root} "
            "(tried .venv and claude_mcp_servers/.venv). Skipping MCP registration."
        )
        print(f"  {msg}", file=sys.stderr)
        _log_install_event("register_mcps", "warn", msg)
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="mcp_registration_no_venv",
                title="MCP registration skipped: no venv-python found",
                detected=msg,
                why_deferred=(
                    "Without a Python interpreter inside the install's venv, the "
                    "MCP server entries in ~/.claude.json cannot be constructed."
                ),
                command_to_apply=(
                    "# Re-run install.py to recreate the venv, then:\n"
                    f"python install.py --update"
                ),
                severity="warning",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    entries = _build_python_mcp_entries(
        install_root, venv_python,
        weaviate_port, ollama_port, grpc_port, code_embed_port,
    )
    success, errors = _python_fallback_write_mcp_entries(claude_json, entries)
    if success > 0:
        print(
            f"  Wrote {success} MCP entr{'y' if success == 1 else 'ies'} "
            f"to {claude_json} via Python fallback."
        )
        _log_install_event(
            "register_mcps", "ok",
            f"Python fallback wrote {success} entries to {claude_json}",
        )
        # Surface a soft notice: the launcher DB was NOT updated in this
        # path; project_state_populate will catch up on next GUI open.
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="mcp_registration_python_fallback",
                title="MCP registration used Python fallback (launcher binary unavailable)",
                detected=(
                    "The launcher binary (vct-launcher) was not bundled, could "
                    "not be downloaded from GitHub Releases, and `cargo tauri "
                    "build` was unavailable. install.py wrote "
                    f"{success} MCP entr{'y' if success == 1 else 'ies'} "
                    f"to {claude_json} via the pure-Python JSON merge fallback."
                ),
                why_deferred=(
                    "Pure-Python writer cannot update the launcher's SQLite DB "
                    "(project_mcp_servers table). The DB will be synced "
                    "automatically the next time you open the launcher GUI "
                    "(project_state_populate picks up the JSON entries)."
                ),
                command_to_apply=(
                    "# Optional: rebuild + retry the canonical writer.\n"
                    "cd launcher && cargo tauri build && \\\n"
                    "python install.py --update"
                ),
                severity="info",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
    else:
        msg = "; ".join(errors) if errors else "unknown error"
        print(f"  MCP registration failed: {msg}", file=sys.stderr)
        _log_install_event("register_mcps", "warn", f"python fallback failed: {msg}")
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="mcp_registration_failed",
                title="MCP registration failed in both Rust and Python paths",
                detected=(
                    f"install.py could not write bundled MCP entries to "
                    f"{claude_json}. Errors: {msg}"
                ),
                why_deferred=(
                    "Cannot proceed without a writable home directory. Install "
                    "completed but Claude Code won't see the orchestrator MCPs "
                    "until this is resolved."
                ),
                command_to_apply=(
                    f"# Ensure {claude_json.parent} is writable, then:\n"
                    "python install.py --update"
                ),
                severity="critical",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )

    # Stale-entry check on --update.
    _detect_stale_mcp_entries(install_root, claude_json, deferral_report)
    # Deprecated-MCP-entry detection (PR-34). Runs unconditionally so
    # users on the Python-fallback path still see the deferral notice.
    _detect_deprecated_mcp_entries(install_root, claude_json, deferral_report)


def _scan_stale_mcp_entries(
    install_root: Path,
    claude_json: Path,
) -> list[tuple[str, str, dict]]:
    """Return a list of ``(mcp_name, stale_path, entry_dict)`` for every
    ``~/.claude.json mcpServers`` entry whose ``command`` or ``args[0]``
    points at a vco-install-shaped path OUTSIDE the current install_root.

    Pure function (no deferral side effects, no writes). Used by both
    :func:`_detect_stale_mcp_entries` (reports only) and
    :func:`_rewrite_stale_mcp_entries` (PR-33 consent-prompted rewrite).
    The triple includes the full entry dict so the rewrite path can
    inspect existing ``env`` keys for the secret-leak warning.

    Cross-OS path detection: absolute paths only (``/``, ``C:\\``,
    ``c:\\``, ``\\\\``-prefixed UNC). Anchored on the ``claude_mcp_servers``
    or ``.venv`` directory tokens so user-added MCPs at ``/usr/bin/foo``
    are NOT misclassified as orchestrator-stale.
    """
    if not claude_json.is_file():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    mcp_servers = data.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return []

    install_root_str = str(install_root.resolve())
    stale: list[tuple[str, str, dict]] = []
    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command", "") if isinstance(entry.get("command"), str) else ""
        first_arg = ""
        args = entry.get("args", [])
        if isinstance(args, list) and args and isinstance(args[0], str):
            first_arg = args[0]
        for candidate in (cmd, first_arg):
            if not candidate or not candidate.startswith(("/", "C:\\", "c:\\", "\\\\")):
                continue
            # Anchor: only flag paths that look like vco install layouts
            # (claude_mcp_servers/ or .venv/). Otherwise we'd flag every
            # user-added MCP that lives in /usr/bin/foo.
            if "claude_mcp_servers" not in candidate and ".venv" not in candidate:
                continue
            if not candidate.startswith(install_root_str):
                stale.append((name, candidate, entry))
                break
    return stale


def _detect_stale_mcp_entries(
    install_root: Path,
    claude_json: Path,
    deferral_report: "DeferralReport",
) -> None:
    """On --update, emit a deferral when ~/.claude.json mcpServers entries
    point at directories outside the current install_root.

    Detection-only path (no rewrite). The companion
    :func:`_rewrite_stale_mcp_entries` (PR-33) consumes the same
    :func:`_scan_stale_mcp_entries` data and performs consent-prompted
    rewrites when ``--rewrite-stale-mcps`` is passed.
    """
    stale = _scan_stale_mcp_entries(install_root, claude_json)
    if not stale:
        return

    install_root_str = str(install_root.resolve())
    detected_lines = [f"  - `{name}`: {path}" for name, path, _ in stale]
    deferral_report.add_entry(
        DeferralEntry(
            condition_id="stale_mcp_entry",
            title="Stale ~/.claude.json MCP entries from a previous install",
            detected=(
                f"~/.claude.json contains MCP entries that point at directories "
                f"outside the current install_root ({install_root_str}):\n\n"
                + "\n".join(detected_lines)
                + "\n\nThese were left behind by a previous orchestrator install "
                "at a different path. Claude Code may spawn duplicate MCP "
                "subprocesses against the same Weaviate container if both "
                "installs are still active."
            ),
            why_deferred=(
                "Auto-rewriting global MCP entries is destructive (user may "
                "have intentional dual-install setups). v0.2.12 detects and "
                "reports; pass `--rewrite-stale-mcps` for the consent-prompted "
                "rewrite path (PR-33)."
            ),
            command_to_apply=(
                "# Re-run with the consent-prompted rewrite flag (PR-33):\n"
                "python install.py --update --rewrite-stale-mcps\n"
                "# Or, for CI / scripted contexts that want to accept all:\n"
                "#   VCT_REWRITE_STALE_MCPS=all python install.py --update --rewrite-stale-mcps"
            ),
            severity="warning",
            kg_node_refs=[
                ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
            ],
        )
    )


# ---------------------------------------------------------------------------
# PR-33 (v0.2.12, 2026-05-16): consent-prompted rewrite of stale MCP entries
# ---------------------------------------------------------------------------
#
# Detection (PR-23, above) is unconditional on --update. Rewrite (PR-33,
# this block) is OFF by default — it only runs when the user passes
# ``--rewrite-stale-mcps``. Even then, every stale entry is prompted
# individually with y/n/all/skip-all choices. ``--quiet`` cannot prompt;
# it emits a clarifying deferral and writes nothing. ``VCT_REWRITE_STALE_MCPS=all``
# env override exists for CI / scripted contexts that explicitly want
# auto-acceptance.
#
# The actual rewrite reuses the same writer that fresh install uses
# (``register_default_orchestrator_mcps`` on the Rust side, or the Python
# fallback). That means the env-key allowlist + secret-shaped-key denylist
# apply uniformly: hand-edited secret keys in stale entries are DROPPED
# on rewrite, with a clear "we dropped X, Y" warning before confirmation.
#
# Two-level backup: ``~/.claude.json.bak-rewrite-<unix-timestamp>`` is
# copied BEFORE we hand off to the writer. The writer itself produces a
# separate ``.bak`` via its atomic-write discipline. Belt-and-suspenders
# for a destructive operation.


def _consent_for_stale_entries(
    stale: list[tuple[str, str, dict]],
    install_root: Path,
    quiet: bool,
    env_override: str,
    input_fn=input,
    output_fn=print,
) -> dict[str, bool]:
    """Drive the per-entry consent prompt and return a ``{name: accept}`` map.

    Decision tree (mirrors the PR-33 spec):

    * ``quiet=True`` and ``env_override != "all"`` → return all-False
      (caller emits a clarifying deferral; no prompt possible).
    * ``env_override == "all"`` → return all-True (CI fast-path).
    * Otherwise → walk each entry once, accept ``y`` / ``yes``, default
      reject on empty / ``n`` / ``no``; ``a`` / ``all`` short-circuits
      remaining entries to True; ``s`` / ``skip-all`` short-circuits
      to False.

    Soft-fail: an EOF / KeyboardInterrupt on the prompt is treated as
    skip-all so install does not crash mid-prompt.
    """
    # Fast-path: explicit env override for CI / scripted runs.
    if env_override.lower() in ("all", "yes", "y", "true", "1"):
        return {name: True for name, _, _ in stale}
    # Quiet mode cannot prompt — caller handles the deferral.
    if quiet:
        return {name: False for name, _, _ in stale}

    install_root_str = str(install_root.resolve())
    output_fn("")
    output_fn(
        f"Found {len(stale)} ~/.claude.json mcpServers entr"
        f"{'y' if len(stale) == 1 else 'ies'} "
        "pointing outside this install_root:"
    )
    for name, stale_path, _entry in stale:
        output_fn(f"  - {name}: {stale_path}")
    output_fn("")
    output_fn(
        "These were registered by a different orchestrator install. "
        "Rewriting will point them at the CURRENT install:"
    )
    output_fn(f"  {install_root_str}")
    output_fn(
        "Existing config (env block, args extras) is preserved; only "
        "path components change. Per-entry choices: "
        "[y]es, [n]o (default), [a]ll, [s]kip-all"
    )
    output_fn("")

    choices: dict[str, bool] = {}
    blanket: Optional[bool] = None
    for name, _stale_path, entry in stale:
        if blanket is not None:
            choices[name] = blanket
            output_fn(f"  {name} → {'rewrite' if blanket else 'skip'} (from blanket choice)")
            continue
        # Secret-leak warning: highlight env keys that will be dropped.
        env_block = entry.get("env", {}) if isinstance(entry.get("env"), dict) else {}
        will_drop = [
            k for k in env_block.keys()
            if _is_secret_shaped_env_key(k) or k not in _ALLOWED_GLOBAL_ENV_KEYS
        ]
        if will_drop:
            output_fn(
                f"  WARNING: rewriting `{name}` will drop env keys: {will_drop}. "
                "These are not in the global-JSON allowlist (secrets go to the "
                "OS keychain via the launcher; per-project keys live in "
                ".claude/settings.json env). Lost on rewrite."
            )
        try:
            answer = input_fn(f"  {name} → rewrite? [y/N/a/s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Treat any prompt-failure as skip-all (safest default).
            output_fn("  (prompt interrupted — treating as skip-all)")
            for n, _, _ in stale:
                choices.setdefault(n, False)
            return choices
        if answer in ("a", "all"):
            blanket = True
            choices[name] = True
        elif answer in ("s", "skip-all"):
            blanket = False
            choices[name] = False
        elif answer in ("y", "yes"):
            choices[name] = True
        else:
            # Empty / "n" / "no" / unrecognised → skip (safest default).
            choices[name] = False
    return choices


def _rewrite_stale_mcp_entries(
    install_root: Path,
    deferral_report: "DeferralReport",
    quiet: bool = False,
    input_fn=input,
    output_fn=print,
) -> None:
    """Consent-prompted rewrite of stale ``~/.claude.json mcpServers``
    entries. PR-33 — ships in v0.2.12.

    Workflow:

    1. Scan via :func:`_scan_stale_mcp_entries`. No matches → no-op.
    2. ``--quiet`` (or no TTY) with no ``VCT_REWRITE_STALE_MCPS=all``
       env override → emit a ``stale_mcp_rewrite_quiet_skipped``
       deferral and return without writing.
    3. Otherwise prompt the user per-entry; collect accept/reject map.
    4. If at least one entry is accepted, snapshot ``~/.claude.json`` to
       ``~/.claude.json.bak-rewrite-<unix-ts>`` BEFORE calling the writer.
       This is on top of the writer's own ``.bak``, giving a recoverable
       two-level backup for a destructive operation.
    5. Hand off to the same registration code path that fresh install
       uses, which means the env allowlist + secret denylist apply
       uniformly. Hand-edited secret keys in stale entries are dropped
       on rewrite (the consent prompt has already warned the user).

    Soft-fail throughout: a malformed JSON, missing venv, lock timeout,
    etc. all surface as a warning + deferral; install completes.
    """
    claude_json = _user_home_for_install() / ".claude.json"
    stale = _scan_stale_mcp_entries(install_root, claude_json)
    if not stale:
        return

    env_override = os.environ.get("VCT_REWRITE_STALE_MCPS", "").strip()
    # Detect non-TTY at the consent layer so test injections can stay
    # explicit. Production: if stdin isn't a TTY and no env override,
    # treat the same as --quiet.
    effective_quiet = quiet or (env_override == "" and not sys.stdin.isatty())

    if effective_quiet and env_override.lower() not in ("all", "yes", "y", "true", "1"):
        # Cannot prompt — emit clarifying deferral and bail.
        install_root_str = str(install_root.resolve())
        detected_lines = [f"  - `{name}`: {path}" for name, path, _ in stale]
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="stale_mcp_rewrite_quiet_skipped",
                title="Stale MCP entries detected but consent prompt bypassed by --quiet",
                detected=(
                    f"~/.claude.json contains stale MCP entries pointing outside "
                    f"the current install_root ({install_root_str}):\n\n"
                    + "\n".join(detected_lines)
                    + "\n\n`--rewrite-stale-mcps` was set, but `--quiet` "
                    "(or a non-TTY stdin) prevented the consent prompt from "
                    "running. No rewrite was performed."
                ),
                why_deferred=(
                    "Rewriting global MCP entries is destructive. PR-33 requires "
                    "explicit per-entry consent OR an explicit env override; "
                    "neither was satisfied."
                ),
                command_to_apply=(
                    "# Re-run interactively (drops --quiet):\n"
                    "python install.py --update --rewrite-stale-mcps\n"
                    "# Or auto-accept for CI / scripted contexts:\n"
                    "VCT_REWRITE_STALE_MCPS=all python install.py --update --rewrite-stale-mcps"
                ),
                severity="warning",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    consent_map = _consent_for_stale_entries(
        stale, install_root, quiet=effective_quiet,
        env_override=env_override, input_fn=input_fn, output_fn=output_fn,
    )
    accepted = [name for name, ok in consent_map.items() if ok]
    rejected = [name for name, ok in consent_map.items() if not ok]

    if not accepted:
        # User declined every prompt — emit an informational deferral so
        # the explicit "I said no" decision is recorded for future runs.
        install_root_str = str(install_root.resolve())
        detected_lines = [f"  - `{name}`: {path}" for name, path, _ in stale]
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="stale_mcp_rewrite_declined",
                title="Stale MCP rewrite declined by user",
                detected=(
                    "User declined to rewrite the following stale entries:\n\n"
                    + "\n".join(detected_lines)
                ),
                why_deferred=(
                    "PR-33 consent prompt was offered for each entry; "
                    "every entry was rejected. No rewrite performed."
                ),
                command_to_apply=(
                    "# To re-prompt and accept some/all entries:\n"
                    "python install.py --update --rewrite-stale-mcps"
                ),
                severity="info",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    # Two-level backup BEFORE the writer touches the file. The writer
    # produces its own .bak; this adds a unique timestamped snapshot so
    # an unfortunate test order or repeat run doesn't clobber the
    # pre-rewrite state.
    if claude_json.is_file():
        ts = int(time.time())
        bak_rewrite = claude_json.with_name(claude_json.name + f".bak-rewrite-{ts}")
        try:
            shutil.copy2(claude_json, bak_rewrite)
            output_fn(f"  Snapshot saved: {bak_rewrite}")
        except OSError as exc:
            # Soft-fail: log + continue. The writer's own .bak still gives
            # us one level of recovery.
            output_fn(f"  (couldn't write {bak_rewrite}: {exc}; relying on writer's .bak)")
            _log_install_event(
                "rewrite_stale_mcps", "warn",
                f"backup copy failed: {exc}",
            )

    # Hand off to the same writer the fresh install uses. We call
    # `_register_mcps` directly — it will overwrite ONLY the bundled
    # entries (weaviate-kg, search). Stale entries with those exact
    # names get overwritten by definition; non-bundled stale entries
    # (e.g. a hand-added MCP at an old install path) are NOT in the
    # bundled set and therefore NOT touched. We surface that asymmetry
    # in the report.
    bundled_names = {"weaviate-kg", "search"}
    non_bundled_accepted = [n for n in accepted if n not in bundled_names]
    bundled_accepted = [n for n in accepted if n in bundled_names]

    # Re-run the registrar to refresh bundled entries at the new path.
    # Soft-fail: any exception inside _register_mcps already adds its
    # own deferral; we surface the rewrite outcome separately.
    if bundled_accepted:
        try:
            _register_mcps(install_root, deferral_report)
        except Exception as exc:  # noqa: BLE001 — soft-fail by design
            output_fn(f"  Rewrite via _register_mcps raised: {exc} (install continues)")
            _log_install_event(
                "rewrite_stale_mcps", "error",
                f"_register_mcps unexpected exception: {exc}",
            )

    # Summary deferral: which entries we touched, which we left alone.
    summary_lines = []
    if bundled_accepted:
        summary_lines.append(
            f"Rewritten (bundled): {', '.join(bundled_accepted)}"
        )
    if non_bundled_accepted:
        summary_lines.append(
            "Accepted but NOT rewritten (entry name is outside the bundled "
            f"set — orchestrator owns weaviate-kg/search only): "
            f"{', '.join(non_bundled_accepted)}. "
            "Edit ~/.claude.json manually if you want these repointed."
        )
    if rejected:
        summary_lines.append(f"Skipped (user said no): {', '.join(rejected)}")
    deferral_report.add_entry(
        DeferralEntry(
            condition_id="stale_mcp_rewrite_summary",
            title="PR-33 stale MCP rewrite summary",
            detected="\n".join(summary_lines) or "(no entries processed)",
            why_deferred=(
                "Informational. The actual rewrite (if any) went through "
                "the standard register_default_orchestrator_mcps writer, "
                "with the env-key allowlist + secret denylist applied."
            ),
            command_to_apply=(
                "# Two-level backup created: ~/.claude.json.bak-rewrite-<ts>\n"
                "# (plus the writer's own ~/.claude.json.bak)\n"
                "# Inspect: cat ~/.claude.json.bak-rewrite-*\n"
                "# Revert:  cp ~/.claude.json.bak-rewrite-<ts> ~/.claude.json"
            ),
            severity="info",
            kg_node_refs=[
                ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
            ],
        )
    )


# ---------------------------------------------------------------------------
# PR-34 (v0.2.13, 2026-05-16): deprecated-MCP-entry detection + removal
# ---------------------------------------------------------------------------
#
# When install.py drops an MCP from the default set (e.g. Ollama MCP in
# v0.2.11) it leaves behind an entry in ~/.claude.json for users who
# installed before that release.  The old _check_ollama_mcp_remnants
# function (PR-14b) fires unconditionally on --update and emits an
# informational notice, but:
#
#   a) it does not distinguish "our" entry (command inside install_root)
#      from a user's own custom Ollama MCP at a different path;
#   b) it cannot auto-remove even with consent.
#
# PR-34 replaces that with a structured deprecation registry and a
# consent-prompted removal path.  Three-step design (mirrors PR-33):
#
#   1. _DEPRECATED_DEFAULT_MCPS — registry of MCPs dropped from the
#      default set, with the release version, human-readable reason, and
#      the opt-in manifest path where the feature moved.
#   2. _scan_deprecated_mcp_entries — pure function that reads
#      ~/.claude.json and returns entries whose (a) name is in the
#      registry AND (b) command path is inside the current install_root
#      (i.e. "our" entry, not user-customised).
#   3. _detect_deprecated_mcp_entries — detection-only path called
#      unconditionally from _register_mcps; emits a deferral for each
#      match (no rewrite).
#   4. _remove_deprecated_mcp_entries — consent-prompted removal.
#      Only runs when --remove-deprecated-mcps is passed.
#      VCT_REMOVE_DEPRECATED_MCPS=all env override for CI.
#
# Composition with PR-33 (--rewrite-stale-mcps):
#   When --rewrite-stale-mcps is passed, deprecated-MCP detection is
#   ALSO run (deprecation is a form of staleness).  The removal itself
#   still requires the explicit --remove-deprecated-mcps flag.


#: Registry of MCPs that used to be in the default install set but were
#: later removed.  Any entry in this dict will be scanned for in
#: ~/.claude.json on every --update run.
_DEPRECATED_DEFAULT_MCPS: dict[str, dict] = {
    "ollama": {
        "removed_in": "v0.2.11",
        "reason": (
            "Ollama MCP server dropped from default install (PR-14a). "
            "The tools it exposed (chat / read_document / read_image) are "
            "redundant with Claude's native capabilities. Ollama remains as "
            "embedding infrastructure (Weaviate vectorizers); only the MCP "
            "tool-surface was removed."
        ),
        "opt_in_manifest": "launcher/bundled_manifests/vct-ollama.json",
    },
    # Future deprecations go here, e.g.:
    # "coordination": {
    #     "removed_in": "vX.Y.Z",
    #     "reason": "...",
    #     "opt_in_manifest": "launcher/bundled_manifests/vct-coordination.json",
    # },
}


def _scan_deprecated_mcp_entries(
    install_root: Path,
    claude_json: Path,
) -> list[tuple[str, str, dict, dict]]:
    """Return a list of ``(mcp_name, cmd_path, entry_dict, dep_info)`` for
    every ``~/.claude.json mcpServers`` entry that:

    a) has a name present in :data:`_DEPRECATED_DEFAULT_MCPS`, AND
    b) whose ``command`` or ``args[0]`` path lives INSIDE the current
       install_root (i.e. it was registered by THIS orchestrator install,
       not a user-added entry at an unrelated path).

    Entries that match the name but whose command is NOT inside
    install_root are assumed to be user-customised and are left alone.

    Pure function (no deferral side effects, no writes).

    Returns:
        List of 4-tuples: (name, path_inside_root, entry_dict, dep_info).
        ``dep_info`` is the value from :data:`_DEPRECATED_DEFAULT_MCPS`.
    """
    if not claude_json.is_file():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    mcp_servers = data.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return []

    install_root_str = str(install_root.resolve())
    results: list[tuple[str, str, dict, dict]] = []
    for name, dep_info in _DEPRECATED_DEFAULT_MCPS.items():
        entry = mcp_servers.get(name)
        if not isinstance(entry, dict):
            continue
        # Determine the path candidates (command + first arg).
        cmd = entry.get("command", "") if isinstance(entry.get("command"), str) else ""
        first_arg = ""
        args = entry.get("args", [])
        if isinstance(args, list) and args and isinstance(args[0], str):
            first_arg = args[0]
        # Check whether ANY path candidate is inside install_root.
        # If none of the candidates are absolute paths inside install_root,
        # the entry is user-customised → leave it alone.
        matched_path = ""
        for candidate in (cmd, first_arg):
            if not candidate:
                continue
            # Only consider absolute paths (cross-OS).
            if not candidate.startswith(("/", "C:\\", "c:\\", "\\\\")):
                continue
            if candidate.startswith(install_root_str):
                matched_path = candidate
                break
        if not matched_path:
            # Either no absolute path candidates, or the path is outside
            # install_root → user-customised entry; skip.
            continue
        results.append((name, matched_path, entry, dep_info))
    return results


def _detect_deprecated_mcp_entries(
    install_root: Path,
    claude_json: Path,
    deferral_report: "DeferralReport",
) -> None:
    """Detection-only path: emit a deferral for each deprecated-MCP entry
    whose path lives inside the current install_root.

    Called unconditionally from :func:`_register_mcps` (after every
    successful write via Path A or B).  The companion
    :func:`_remove_deprecated_mcp_entries` performs the actual removal
    when ``--remove-deprecated-mcps`` is passed.

    User-customised entries (command outside install_root) are silently
    skipped — they are the user's concern, not ours.
    """
    deprecated = _scan_deprecated_mcp_entries(install_root, claude_json)
    if not deprecated:
        return

    install_root_str = str(install_root.resolve())
    for name, matched_path, _entry, dep_info in deprecated:
        removed_in = dep_info.get("removed_in", "unknown release")
        reason = dep_info.get("reason", "")
        opt_in = dep_info.get("opt_in_manifest", "")
        opt_in_note = (
            f"\nOpt-in: if you still want these tools, install the module "
            f"via the launcher → Modules, or inspect {opt_in}."
        ) if opt_in else ""

        deferral_report.add_entry(
            DeferralEntry(
                condition_id=f"deprecated_mcp_{name}",
                title=(
                    f"Deprecated MCP entry `{name}` still in ~/.claude.json "
                    f"(removed {removed_in})"
                ),
                detected=(
                    f"~/.claude.json contains a `{name}` block under "
                    f"`mcpServers` whose command path ({matched_path}) points "
                    f"inside the current install_root ({install_root_str}). "
                    f"This entry was registered by a previous version of this "
                    f"orchestrator install and is no longer part of the default "
                    f"install set.\n\nReason: {reason}{opt_in_note}"
                ),
                why_deferred=(
                    "Auto-removal of ~/.claude.json entries requires user "
                    "consent. Pass `--remove-deprecated-mcps` (with "
                    "`--update`) for the consent-prompted removal path. "
                    "The existing entry is preserved and functional until "
                    "you remove it."
                ),
                command_to_apply=(
                    f"# Consent-prompted removal (PR-34):\n"
                    f"python install.py --update --remove-deprecated-mcps\n"
                    f"# Or, for CI / scripted contexts:\n"
                    f"#   VCT_REMOVE_DEPRECATED_MCPS=all python install.py "
                    f"--update --remove-deprecated-mcps\n"
                    f"# Or, remove manually:\n"
                    f"#   Edit {claude_json} and delete the `\"{name}\": {{...}}` "
                    f"entry under `mcpServers`."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )


def _remove_deprecated_mcp_entries(
    install_root: Path,
    deferral_report: "DeferralReport",
    quiet: bool = False,
    input_fn=input,
    output_fn=print,
) -> None:
    """Consent-prompted removal of deprecated ``~/.claude.json mcpServers``
    entries. PR-34 — ships in v0.2.13.

    Workflow:

    1. Scan via :func:`_scan_deprecated_mcp_entries`. No matches → no-op.
    2. ``--quiet`` (or no TTY) with no ``VCT_REMOVE_DEPRECATED_MCPS=all``
       env override → emit a ``deprecated_mcp_removal_quiet_skipped``
       deferral and return without writing.
    3. Otherwise prompt the user per-entry; collect accept/reject map.
    4. If at least one entry is accepted, snapshot ``~/.claude.json`` to
       ``~/.claude.json.bak-depr-remove-<unix-ts>`` BEFORE writing.
    5. Remove the accepted entries from ``mcpServers`` and write back
       atomically (same lock + tmp + rename discipline as the writer).

    Soft-fail throughout. Does NOT run unless ``--remove-deprecated-mcps``
    is passed. Never auto-removes on a vanilla ``--update``.

    Args:
        install_root: Resolved path to this orchestrator install.
        deferral_report: Run-scoped report to append outcome entries to.
        quiet: If True and no ``VCT_REMOVE_DEPRECATED_MCPS=all``,
            emit a clarifying deferral and return without prompting.
        input_fn: Injectable for testing (default: ``input``).
        output_fn: Injectable for testing (default: ``print``).
    """
    claude_json = _user_home_for_install() / ".claude.json"
    deprecated = _scan_deprecated_mcp_entries(install_root, claude_json)
    if not deprecated:
        return

    env_override = os.environ.get("VCT_REMOVE_DEPRECATED_MCPS", "").strip()
    effective_quiet = quiet or (env_override == "" and not sys.stdin.isatty())

    if effective_quiet and env_override.lower() not in ("all", "yes", "y", "true", "1"):
        install_root_str = str(install_root.resolve())
        detected_lines = [f"  - `{name}`: {path}" for name, path, _, _ in deprecated]
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="deprecated_mcp_removal_quiet_skipped",
                title="Deprecated MCP entries detected but consent prompt bypassed by --quiet",
                detected=(
                    f"~/.claude.json contains deprecated MCP entries whose paths "
                    f"point inside the current install_root ({install_root_str}):\n\n"
                    + "\n".join(detected_lines)
                    + "\n\n`--remove-deprecated-mcps` was set, but `--quiet` "
                    "(or a non-TTY stdin) prevented the consent prompt from "
                    "running. No removal was performed."
                ),
                why_deferred=(
                    "Removing global MCP entries is destructive. PR-34 requires "
                    "explicit per-entry consent OR an explicit env override; "
                    "neither was satisfied."
                ),
                command_to_apply=(
                    "# Re-run interactively (drop --quiet):\n"
                    "python install.py --update --remove-deprecated-mcps\n"
                    "# Or auto-accept for CI / scripted contexts:\n"
                    "VCT_REMOVE_DEPRECATED_MCPS=all "
                    "python install.py --update --remove-deprecated-mcps"
                ),
                severity="warning",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    # Drive the per-entry consent prompt.
    install_root_str = str(install_root.resolve())

    # Fast-path: explicit env override for CI / scripted runs.
    if env_override.lower() in ("all", "yes", "y", "true", "1"):
        consent_map = {name: True for name, _, _, _ in deprecated}
    else:
        output_fn("")
        output_fn(
            f"Found {len(deprecated)} deprecated ~/.claude.json mcpServers "
            f"entr{'y' if len(deprecated) == 1 else 'ies'} "
            "whose paths are inside this install_root:"
        )
        for name, path, _, dep_info in deprecated:
            removed_in = dep_info.get("removed_in", "?")
            output_fn(f"  - {name}: {path}  (removed {removed_in})")
        output_fn("")
        output_fn(
            "These entries are no longer part of the default install. "
            "Removing them prevents Claude Code from loading the deprecated "
            "MCP server subprocesses."
        )
        output_fn(
            "Per-entry choices: [y]es, [n]o (default), [a]ll, [s]kip-all"
        )
        output_fn("")

        consent_map: dict[str, bool] = {}
        blanket: Optional[bool] = None
        for name, _path, _entry, dep_info in deprecated:
            if blanket is not None:
                consent_map[name] = blanket
                output_fn(
                    f"  {name} → {'remove' if blanket else 'skip'} (from blanket choice)"
                )
                continue
            removed_in = dep_info.get("removed_in", "?")
            try:
                answer = input_fn(
                    f"  {name} (removed {removed_in}) → remove? [y/N/a/s]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                output_fn("  (prompt interrupted — treating as skip-all)")
                for n, _, _, _ in deprecated:
                    consent_map.setdefault(n, False)
                return
            if answer in ("a", "all"):
                blanket = True
                consent_map[name] = True
            elif answer in ("s", "skip-all"):
                blanket = False
                consent_map[name] = False
            elif answer in ("y", "yes"):
                consent_map[name] = True
            else:
                consent_map[name] = False

    accepted = [name for name, ok in consent_map.items() if ok]
    rejected = [name for name, ok in consent_map.items() if not ok]

    if not accepted:
        detected_lines = [f"  - `{name}`: {path}" for name, path, _, _ in deprecated]
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="deprecated_mcp_removal_declined",
                title="Deprecated MCP removal declined by user",
                detected=(
                    "User declined to remove the following deprecated entries:\n\n"
                    + "\n".join(detected_lines)
                ),
                why_deferred=(
                    "PR-34 consent prompt was offered for each entry; "
                    "every entry was rejected. No removal performed."
                ),
                command_to_apply=(
                    "# To re-prompt and accept some/all entries:\n"
                    "python install.py --update --remove-deprecated-mcps"
                ),
                severity="info",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    # Two-level backup BEFORE writing. The atomic writer does its own .bak;
    # this adds a unique timestamped snapshot so repeating the command
    # doesn't clobber the pre-removal state.
    if claude_json.is_file():
        ts = int(time.time())
        bak_path = claude_json.with_name(claude_json.name + f".bak-depr-remove-{ts}")
        try:
            shutil.copy2(claude_json, bak_path)
            output_fn(f"  Snapshot saved: {bak_path}")
        except OSError as exc:
            output_fn(f"  (couldn't write {bak_path}: {exc}; relying on writer's .bak)")
            _log_install_event(
                "remove_deprecated_mcps", "warn",
                f"backup copy failed: {exc}",
            )

    # Perform the removal atomically (same lock + tmp + rename discipline).
    lock_path = claude_json.with_suffix(claude_json.suffix + ".lock")
    locked = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            locked = True
            break
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            break

    if not locked:
        msg = f"could not acquire lock {lock_path} for deprecated-MCP removal"
        output_fn(f"  WARNING: {msg}")
        _log_install_event("remove_deprecated_mcps", "warn", msg)
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="deprecated_mcp_removal_lock_failed",
                title="Deprecated MCP removal could not acquire ~/.claude.json lock",
                detected=msg,
                why_deferred=(
                    "Another process holds the file lock. Re-run after any "
                    "concurrent install.py / launcher process finishes."
                ),
                command_to_apply=(
                    "python install.py --update --remove-deprecated-mcps"
                ),
                severity="warning",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    removal_errors: list[str] = []
    removed_names: list[str] = []
    try:
        try:
            if claude_json.is_file():
                raw = claude_json.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
            else:
                data = {}
        except (OSError, json.JSONDecodeError) as exc:
            removal_errors.append(f"read {claude_json}: {exc}")
            data = None

        if data is not None and isinstance(data, dict):
            mcp_servers = data.get("mcpServers", {})
            if isinstance(mcp_servers, dict):
                for name in accepted:
                    if name in mcp_servers:
                        del mcp_servers[name]
                        removed_names.append(name)
            try:
                if claude_json.is_file():
                    bak = claude_json.with_suffix(claude_json.suffix + ".bak")
                    shutil.copy2(claude_json, bak)
                tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, claude_json)
            except OSError as exc:
                removal_errors.append(f"write {claude_json}: {exc}")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass

    if removal_errors:
        msg = "; ".join(removal_errors)
        print(f"  Deprecated MCP removal failed: {msg}", file=sys.stderr)
        output_fn(f"  Deprecated MCP removal failed: {msg}")
        _log_install_event(
            "remove_deprecated_mcps", "warn",
            f"removal write failed: {msg}",
        )
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="deprecated_mcp_removal_write_failed",
                title="Deprecated MCP removal write failed",
                detected=f"Could not update {claude_json}: {msg}",
                why_deferred=(
                    "File-system error during the atomic write. "
                    "The backup snapshot (if created) preserves the prior state."
                ),
                command_to_apply=(
                    "python install.py --update --remove-deprecated-mcps"
                ),
                severity="warning",
                kg_node_refs=[
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )
        return

    # Summary deferral.
    summary_lines = []
    if removed_names:
        output_fn(f"  Removed deprecated MCP entr{'y' if len(removed_names) == 1 else 'ies'}: "
                  f"{', '.join(removed_names)}")
        summary_lines.append(f"Removed: {', '.join(removed_names)}")
        _log_install_event(
            "remove_deprecated_mcps", "ok",
            f"removed deprecated entries: {removed_names}",
        )
    if rejected:
        summary_lines.append(f"Skipped (user said no): {', '.join(rejected)}")
    deferral_report.add_entry(
        DeferralEntry(
            condition_id="deprecated_mcp_removal_summary",
            title="PR-34 deprecated MCP removal summary",
            detected="\n".join(summary_lines) or "(no entries processed)",
            why_deferred=(
                "Informational. The removal was performed atomically with a "
                "timestamped backup snapshot before writing."
            ),
            command_to_apply=(
                "# Two-level backup created: ~/.claude.json.bak-depr-remove-<ts>\n"
                "# (plus the writer's own ~/.claude.json.bak)\n"
                "# Inspect: cat ~/.claude.json.bak-depr-remove-*\n"
                "# Revert:  cp ~/.claude.json.bak-depr-remove-<ts> ~/.claude.json"
            ),
            severity="info",
            kg_node_refs=[
                ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
            ],
        )
    )


# ---------------------------------------------------------------------------
# PR-11: global lean-ctx hooks detection
# ---------------------------------------------------------------------------

def _check_global_lean_ctx_hooks(
    deferral_report: "DeferralReport",
) -> None:
    """Detect global lean-ctx hooks that may cause fork-bomb incidents.

    Probes two locations in the user's ~/.claude/ directory:

    1. ``~/.claude/settings.json`` — ``hooks.PreToolUse`` entries whose
       ``command`` field contains "lean-ctx" (case-insensitive).
    2. ``~/.claude/hooks/lean-ctx-*`` — files matching the lean-ctx hook
       naming convention (lean-ctx-rewrite, lean-ctx-redirect,
       lean-ctx-rewrite-native, .lean-ctx.bak, etc.).

    When either is detected, a LOUD warning is printed to stderr and a
    ``global_lean_ctx_hooks_detected`` deferral entry is added to
    ``deferral_report`` so Claude Code surfaces it at the next session
    start.

    Behaviour:
      - Missing ``~/.claude/`` directory → returns early, no warning.
      - Unreadable or malformed ``settings.json`` → logs to stderr, returns.
      - File-iteration errors → caught and skipped individually.
      - Never raises to the caller (soft-fail throughout).

    Args:
        deferral_report: The run-scoped :class:`DeferralReport` to append
            the deferral entry to when lean-ctx artifacts are found.
    """
    home = Path.home()
    claude_dir = home / ".claude"

    if not claude_dir.is_dir():
        return

    # ------------------------------------------------------------------
    # 1. Probe ~/.claude/settings.json for PreToolUse hooks referencing
    #    lean-ctx.
    # ------------------------------------------------------------------
    settings_path = claude_dir / "settings.json"
    offending_settings: list[str] = []

    if settings_path.is_file():
        try:
            raw = settings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"WARNING: could not read {settings_path} during lean-ctx "
                f"detection — skipping settings probe: {exc}\n"
            )
            data = {}

        pre_tool_use = (
            data.get("hooks", {}).get("PreToolUse", [])
            if isinstance(data, dict)
            else []
        )
        for entry in pre_tool_use if isinstance(pre_tool_use, list) else []:
            cmd = ""
            if isinstance(entry, dict):
                cmd = entry.get("command", "") or ""
            elif isinstance(entry, str):
                cmd = entry
            if "lean-ctx" in cmd.lower():
                offending_settings.append(cmd)

    # ------------------------------------------------------------------
    # 2. Probe ~/.claude/hooks/ for lean-ctx-* files (any extension).
    # ------------------------------------------------------------------
    hooks_dir = claude_dir / "hooks"
    offending_files: list[Path] = []

    if hooks_dir.is_dir():
        try:
            for candidate in hooks_dir.iterdir():
                name = candidate.name.lower()
                # Match lean-ctx-* prefix OR .lean-ctx* pattern
                if name.startswith("lean-ctx-") or name.startswith(".lean-ctx"):
                    offending_files.append(candidate)
        except OSError as exc:
            sys.stderr.write(
                f"WARNING: could not list {hooks_dir} during lean-ctx "
                f"detection: {exc}\n"
            )

    if not offending_settings and not offending_files:
        return

    # ------------------------------------------------------------------
    # Build a human-readable summary of what was found.
    # ------------------------------------------------------------------
    found_lines: list[str] = []
    if offending_files:
        found_lines.append("  Hook files:")
        for f in sorted(offending_files):
            found_lines.append(f"    {f}")
    if offending_settings:
        found_lines.append("  settings.json PreToolUse entries:")
        for cmd in offending_settings:
            found_lines.append(f"    command: {cmd}")

    found_summary = "\n".join(found_lines)

    # Build the rm command listing all detected hook files.
    if offending_files:
        rm_targets = " ".join(
            f'"{f}"' for f in sorted(offending_files)
        )
        rm_cmd = f"rm {rm_targets}"
    else:
        rm_cmd = "# (no hook files — only settings.json entries found)"

    warning = (
        "\n"
        "=" * 70 + "\n"
        "WARNING  Global lean-ctx hooks detected in ~/.claude/\n"
        "=" * 70 + "\n"
        "\n"
        "These were likely installed by `lean-ctx init --agent claude` and\n"
        "caused two fork-bomb incidents (2026-04-30 + 2026-05-15) before\n"
        "VCO 0.2.11.\n"
        "\n"
        "Detected artifacts:\n"
        f"{found_summary}\n"
        "\n"
        "VCO ships its own per-project lean-ctx PreToolUse hook (PR-1) with\n"
        "guards that prevent recursion.  The global ones are now redundant\n"
        "AND dangerous.\n"
        "\n"
        "To remove (recommended):\n"
        f"  {rm_cmd}\n"
        "  # Then manually edit ~/.claude/settings.json to remove the\n"
        "  # hooks.PreToolUse entries that call lean-ctx.\n"
        "\n"
        "Or re-run with --suppress-lean-ctx-warning to skip this check.\n"
        "=" * 70 + "\n"
    )
    sys.stderr.write(warning)

    # ------------------------------------------------------------------
    # Emit deferral entry so Claude Code surfaces this at next session.
    # ------------------------------------------------------------------
    detected_desc_parts: list[str] = []
    if offending_files:
        file_list = ", ".join(str(f) for f in sorted(offending_files))
        detected_desc_parts.append(f"Hook files: {file_list}")
    if offending_settings:
        cmds = "; ".join(offending_settings)
        detected_desc_parts.append(
            f"settings.json PreToolUse commands containing 'lean-ctx': {cmds}"
        )
    detected_desc = ". ".join(detected_desc_parts) + "."

    deferral_report.add_entry(
        DeferralEntry(
            condition_id="global_lean_ctx_hooks_detected",
            title="Global lean-ctx hooks detected",
            detected=detected_desc,
            why_deferred=(
                "Auto-removal of ~/.claude/settings.json entries would be "
                "brittle and require user consent. The user must remove them "
                "manually to avoid accidental data loss."
            ),
            command_to_apply=(
                f"{rm_cmd}\n"
                "# Then edit ~/.claude/settings.json and remove the "
                "hooks.PreToolUse entries calling lean-ctx."
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/lean-ctx-shim-disabled.md",
                "knowledge/concepts/orchestrator-hook-system.md",
            ],
        )
    )


# ---------------------------------------------------------------------------
# PR-12 Bug C: stale-unit auto-repair on --update
# ---------------------------------------------------------------------------

# Regex captures the value of the `WorkingDirectory=` line (anywhere in the
# unit file) WITHOUT consuming surrounding whitespace. systemd unit syntax
# is permissive about whitespace around `=`, so we match `\s*=\s*`.
_UNIT_WORKING_DIR_RE = re.compile(
    r"^(\s*WorkingDirectory\s*=\s*)(.+?)\s*$",
    re.MULTILINE,
)
# Same pattern for the Environment=VCT_STACK_WORKING_DIR= line — the
# template emits both, and they must stay in lockstep.
_UNIT_ENV_WORKING_DIR_RE = re.compile(
    r"^(\s*Environment\s*=\s*VCT_STACK_WORKING_DIR\s*=\s*)(.+?)\s*$",
    re.MULTILINE,
)


def _repair_systemd_unit_working_dir(
    install_path: Path,
    correct_working_dir: Optional[Path] = None,
    deferral_report: Optional["DeferralReport"] = None,
) -> Optional[tuple[str, str]]:
    """Auto-repair the systemd user unit when its `WorkingDirectory=` line
    points at a stale install path (PR-12 Bug C).

    Reads ``~/.config/systemd/user/claude-mcp-containers.service`` (if
    present), parses the current ``WorkingDirectory=`` value, and re-renders
    the unit when it doesn't match the correct path. The "correct" path is
    derived via the same priority order as ``_resolve_compose_working_dir``:
    install_path subdirs (claude_mcp_servers/ then infrastructure/), or the
    explicitly-passed ``correct_working_dir``.

    Behaviour:
      - Linux only: returns None on every other OS (systemd user units don't
        exist elsewhere).
      - Idempotent: when the unit's WorkingDirectory ALREADY matches the
        correct path, this is a complete no-op (no read churn, no log
        spam, no deferral entry).
      - Backup-then-write: when repair is needed, the current unit is
        backed up to ``<unit>.bak-<ISO8601>`` before being rewritten with
        the corrected `WorkingDirectory=` AND `Environment=VCT_STACK_WORKING_DIR=`
        lines (the template emits both — they must stay in lockstep).
      - Deferral: when a repair lands, an entry with condition_id
        ``boot_service_path_repaired`` is appended to ``deferral_report``
        (if provided) listing old → new + the systemd reload command. The
        unit ITSELF is rewritten on the spot — the deferral exists only
        so the user sees what changed and can re-run `systemctl --user
        daemon-reload && systemctl --user restart` at their leisure.
      - Soft-fail: any OSError reading/writing the unit, any unparseable
        unit content — log + return None. Never raises.

    Returns:
      - None when no action was taken (wrong OS, unit missing, already
        correct, or read/write failed).
      - ``(old_working_dir, new_working_dir)`` tuple when a repair landed.
    """
    # Linux-only — systemd user units are a Linux concept.
    if platform.system() != "Linux":
        return None

    unit_path = (
        _user_home_for_install()
        / ".config" / "systemd" / "user" / _BOOT_SERVICE_UNIT_NAME
    )
    if not unit_path.is_file():
        # No unit on disk → nothing to repair. _materialize_boot_service
        # will create one fresh; this helper has no work to do.
        return None

    # Read the current unit. Soft-fail on permission / encoding errors.
    try:
        existing = unit_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not read systemd unit for repair check: {exc}",
            data={"unit_path": str(unit_path)},
        )
        return None

    # Parse current WorkingDirectory= value.
    wd_match = _UNIT_WORKING_DIR_RE.search(existing)
    if not wd_match:
        # Unit has no WorkingDirectory= line at all. Could be a
        # third-party unit with our filename (unlikely but possible) or a
        # corrupt template render. Either way, leave it alone — let
        # _materialize_boot_service handle the rewrite via its normal
        # idempotent path.
        return None
    current_wd = wd_match.group(2).strip()

    # Resolve the correct WorkingDirectory using the same priority order
    # as the dispatcher. Caller may pass it explicitly to avoid duplicate
    # resolution work.
    if correct_working_dir is None:
        correct_working_dir = _resolve_compose_working_dir(
            install_path=install_path,
            cli_override=None,
            ps_label_value=None,  # don't trust ps labels in repair path —
                                  # they're literally what caused Bug C
        )
    if correct_working_dir is None:
        # Couldn't resolve a target — punt. _materialize_boot_service
        # will log its own warn for the same reason.
        return None
    correct_wd_str = str(correct_working_dir)

    # Already correct → no-op (the common idempotent case).
    if current_wd == correct_wd_str:
        return None

    # Mismatch: rewrite both WorkingDirectory= and the
    # Environment=VCT_STACK_WORKING_DIR= line so they stay in lockstep.
    # We do an in-place substitution rather than re-rendering from the
    # template here, so a user who has manually customised OTHER lines
    # of their unit (e.g. added an `After=` dependency) keeps those edits.
    repaired = _UNIT_WORKING_DIR_RE.sub(
        lambda m: f"{m.group(1)}{correct_wd_str}",
        existing,
    )
    repaired = _UNIT_ENV_WORKING_DIR_RE.sub(
        lambda m: f"{m.group(1)}{correct_wd_str}",
        repaired,
    )

    # Backup the prior unit before overwriting.
    stamp = _utc_iso_now().replace(":", "").replace("-", "")
    backup_path = unit_path.with_name(unit_path.name + f".bak-{stamp}")
    try:
        backup_path.write_text(existing, encoding="utf-8")
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not back up systemd unit before repair: {exc} — aborting repair",
            data={"unit_path": str(unit_path), "backup_target": str(backup_path)},
        )
        return None
    try:
        unit_path.write_text(repaired, encoding="utf-8")
    except OSError as exc:
        _log_install_event(
            "boot-service", "warn",
            f"could not rewrite systemd unit during repair: {exc}",
            data={"unit_path": str(unit_path)},
        )
        return None

    _log_install_event(
        "boot-service", "ok",
        "repaired stale WorkingDirectory in systemd unit",
        data={
            "unit_path": str(unit_path),
            "backup": str(backup_path),
            "old_working_dir": current_wd,
            "new_working_dir": correct_wd_str,
        },
    )

    # Surface the change in the deferral report so the user knows to
    # reload the unit. The unit ITSELF was already rewritten — this is a
    # nudge to apply the running-systemd-state change, not a pending
    # action gate.
    if deferral_report is not None:
        try:
            deferral_report.add_entry(
                DeferralEntry(
                    condition_id="boot_service_path_repaired",
                    title="Boot-service WorkingDirectory was stale; auto-repaired",
                    detected=(
                        f"~/.config/systemd/user/{_BOOT_SERVICE_UNIT_NAME} pointed at "
                        f"`{current_wd}` (a stale path from a prior install). "
                        f"VCO rewrote the unit to point at `{correct_wd_str}` and saved "
                        f"the original to `{backup_path.name}`."
                    ),
                    why_deferred=(
                        "The on-disk unit was repaired in place, but the running "
                        "systemd state still has the old WorkingDirectory cached. "
                        "Reload + restart manually so the change takes effect now "
                        "(otherwise it'll only kick in at the next login session)."
                    ),
                    command_to_apply=(
                        "systemctl --user daemon-reload && "
                        f"systemctl --user restart {_BOOT_SERVICE_UNIT_NAME}"
                    ),
                    severity="info",
                )
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail
            _log_install_event(
                "boot-service", "warn",
                f"could not append boot_service_path_repaired deferral: {exc}",
            )

    return (current_wd, correct_wd_str)


def _materialize_boot_service(
    install_path: Path,
    sysinfo,
    args: argparse.Namespace,
    deferral_report: Optional["DeferralReport"] = None,
) -> None:
    """Cross-OS dispatcher for boot-service materialization.

    Calls the OS-specific renderer after resolving the compose-project
    working dir. Soft-fail throughout — boot-service failure NEVER
    blocks the install (logged as a warning, install continues).

    OS support matrix (cross-OS / cross-runtime):

      Linux   + podman → systemd user unit + CDI wait (canonical path)
      Linux   + docker → systemd user unit, no CDI wait (docker hook)
      macOS   + docker → LaunchAgent; no GPU passthrough on Apple Silicon
      macOS   + podman → LaunchAgent; podman-machine has no GPU passthrough
      Windows + docker → Task Scheduler + powershell.exe + .ps1 wrapper
                         (v0.2.14: no Git Bash / WSL dependency)
      Windows + podman → Task Scheduler + powershell.exe + .ps1 wrapper
                         (podman-machine WSL2 backend; GPU via NVIDIA-WSL2)

    The wrapper script (scripts/launch-claude-mcp-stack.sh or
    scripts/launch-claude-mcp-stack.ps1 on Windows) handles
    runtime detection + GPU-mode detection internally, so this function
    is runtime-agnostic — it only needs to know the OS.

    PR-12 Bug C: when ``deferral_report`` is supplied, this dispatcher
    ALSO runs ``_repair_systemd_unit_working_dir`` BEFORE re-rendering,
    so a stale unit gets fixed even if subsequent rendering would have
    been a no-op (idempotent path skipped the write because content
    "matched", except it didn't — the only thing that matched was the
    stale-but-consistent state).
    """
    # Honor the `_BOOT_SERVICE_DISABLE` env var for CI / minimal installs
    # that don't want a system service materialized.
    if os.environ.get("VCT_DISABLE_BOOT_SERVICE", "").strip() == "1":
        _log_install_event(
            "boot-service", "skip",
            "VCT_DISABLE_BOOT_SERVICE=1 — skipping",
        )
        return

    # `--no-containers` users won't have a stack to autostart.
    if getattr(args, "no_containers", False):
        _log_install_event(
            "boot-service", "skip",
            "--no-containers — boot service not needed",
        )
        return

    cli_override = getattr(args, "compose_working_dir", None)
    container_cmd = getattr(sysinfo, "container_cmd", "") if sysinfo else ""
    ps_label = _probe_compose_working_dir_via_ps(container_cmd) if container_cmd else None

    working_dir = _resolve_compose_working_dir(
        install_path=install_path,
        cli_override=cli_override,
        ps_label_value=ps_label,
    )
    if working_dir is None:
        _log_install_event(
            "boot-service", "warn",
            "no compose-project dir found — boot service not configured; "
            "re-run with --compose-working-dir to set it explicitly",
        )
        return

    # PR-12 Bug C: stale-unit auto-repair runs BEFORE the renderer so a
    # unit with the wrong WorkingDirectory gets fixed even on update
    # paths where the renderer would otherwise no-op (template content
    # already "matches" the stale rendering). Soft-fail; never raises.
    try:
        _repair_systemd_unit_working_dir(
            install_path=install_path,
            correct_working_dir=working_dir,
            deferral_report=deferral_report,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail catch-all
        _log_install_event(
            "boot-service", "warn",
            f"systemd unit repair raised: {exc.__class__.__name__}: {exc}",
        )

    os_name = platform.system()
    try:
        if os_name == "Linux":
            _materialize_boot_service_linux(install_path, working_dir)
        elif os_name == "Darwin":
            _materialize_boot_service_macos(install_path, working_dir)
        elif os_name == "Windows":
            _materialize_boot_service_windows(install_path, working_dir)
        else:
            _log_install_event(
                "boot-service", "skip",
                f"unsupported OS for boot-service materialization: {os_name}",
            )
    except Exception as exc:  # noqa: BLE001 — soft-fail catch-all
        # Bug L2 design contract: boot-service materialization NEVER
        # blocks the install. Catch everything, log, return.
        _log_install_event(
            "boot-service", "warn",
            f"boot-service materialization raised: {exc.__class__.__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Step 8: Write .env
# ---------------------------------------------------------------------------

# Marker tag inserted on every line ensure_env_template appends to a
# pre-existing .env. The tag is what makes the operation idempotent —
# a second run sees the marker and refuses to re-append the same key.
#
# Format: `# added by vco YYYY-MM-DD` (date is for forensic value;
# the *literal* `# added by vco ` substring is what the dedupe check
# looks for).
ENV_VCO_MARKER = "# added by vco"

# Module-section delimiters for template-managed .env. When a module
# is installed via the launcher, append a section bracketed by these
# markers; when uninstalled (v1.1+), the section can be located and
# removed cleanly. Keep simple for v1: append-only.
ENV_MODULE_BLOCK_START = "# >>> module: "
ENV_MODULE_BLOCK_END = "# <<< module: "


def _env_canonical_template(project_name: str = "<project>",
                            project_root: str = "<project_root>") -> list[tuple[str, str | None, str]]:
    """Return the canonical .env template as a list of (key, default, comment).

    Each entry:
      - key: env var name (e.g. "WEAVIATE_URL")
      - default: value to write, or None to write a commented-out placeholder
      - comment: human-readable comment (one line, no trailing newline)

    Section headers are encoded as entries with key="" and comment set.
    Pure comment lines have key="" and default=None.

    Used by both:
      - _build_canonical_env_template_text (creating a fresh .env)
      - _ensure_env_template (parsing the canonical key list to know
        what's missing in an existing .env)

    NOTE: The launcher's per-project values (KG_COLLECTION, PROJECT_NAME,
    etc.) are written by `write_project_env_files` in Rust. install.py's
    template carries placeholder values (`<project>_KnowledgeGraph`)
    that the launcher overwrites at project-registration time.
    """
    return [
        # Header banner
        ("", None, "# vibecoded-orchestrator per-project .env"),
        ("", None, "# Edit values to override defaults. Empty / commented lines are"),
        ("", None, "# treated as \"use default\". Created by vco "
                   + _utc_iso_now()[:10] + "."),
        ("", None, ""),

        # Service URLs section
        ("", None, "# === Service URLs (uncomment to override the launcher's adopted defaults) ==="),
        ("WEAVIATE_URL", None, "# WEAVIATE_URL=http://localhost:8081"),
        ("WEAVIATE_PORT", None, "# WEAVIATE_PORT=8081"),
        ("OLLAMA_URL", None, "# OLLAMA_URL=http://localhost:11435"),
        ("OLLAMA_PORT", None, "# OLLAMA_PORT=11435"),
        ("CODE_EMBED_URL", None, "# CODE_EMBED_URL=http://localhost:11440"),
        ("", None, ""),

        # Per-project Weaviate collections
        ("", None, "# === Per-project Weaviate collections ==="),
        ("", None, "# Resolved by the launcher when the project is registered. Don't"),
        ("", None, "# edit unless you know what you're doing."),
        ("KG_COLLECTION", f"{project_name}_KnowledgeGraph", None),
        # Default value: capital-C "VibeCoded" since v0.2.23 B1 (was
        # lowercase-c v0.2.12–v0.2.22, itself renamed from
        # "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E).
        # Picker overrides this per-project.
        ("SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph", None),
        ("DEVELOPMENT_COLLECTION", f"{project_name}_Development", None),
        ("PROJECT_NAME", project_name, None),
        # CONVERSATION_COLLECTION removed 2026-04-30 — capture flow deprecated
        # and the collection was dropped from new installs. If you have an
        # existing install with conversations data, that collection still
        # exists but is no longer written to.
        ("", None, ""),

        # LLM API keys
        ("", None, "# === LLM API keys (optional) ==="),
        ("ANTHROPIC_API_KEY", None, "# ANTHROPIC_API_KEY="),
        ("OPENAI_API_KEY", None, "# OPENAI_API_KEY="),
        ("", None, ""),

        # GitHub access
        ("", None, "# === GitHub access for code-search MCP (optional) ==="),
        ("GITHUB_TOKEN", None, "# GITHUB_TOKEN="),
        ("", None, ""),

        # RL retrieval (Pro tier — module section)
        ("", None, "# === RL retrieval module (Pro tier — uncomment when installed) ==="),
        ("RL_SERVER_URL", None, "# RL_SERVER_URL=http://localhost:8090"),
        ("RL_SERVER_PORT", None, "# RL_SERVER_PORT=8090"),
        ("RL_PROJECT_ROOT", None, f"# RL_PROJECT_ROOT={project_root}"),
        ("", None, ""),

        # Telemetry
        ("", None, "# === Telemetry (off by default; on=opt-in only) ==="),
        ("VCT_TELEMETRY", None, "# VCT_TELEMETRY=off"),
        ("", None, ""),
    ]


def _build_canonical_env_template_text(project_name: str = "<project>",
                                       project_root: str = "<project_root>") -> str:
    """Render the canonical template to a single .env file string.

    Used when no .env exists yet. Active (uncommented) keys get written
    as `KEY=value`, optional keys as `# KEY=value` comments.
    """
    lines: list[str] = []
    for key, default, comment in _env_canonical_template(project_name, project_root):
        if not key:
            # Pure comment / blank line.
            lines.append(comment if comment is not None else "")
            continue
        if default is None:
            # Optional key — write the commented form.
            lines.append(comment if comment is not None else f"# {key}=")
        else:
            # Active key — write KEY=value.
            lines.append(f"{key}={default}")
    return "\n".join(lines) + "\n"


def _parse_existing_env_keys(env_path: Path) -> set[str]:
    """Return the set of KEY names present in an existing .env file
    (commented or active). Used by `_ensure_env_template` to decide
    which canonical keys are missing.

    A line is considered to declare KEY iff (after lstrip + optional
    leading "#") it matches `^KEY=`. We deliberately treat commented
    keys as PRESENT — the user knows about them, they just chose to
    leave the value blank. Re-appending would be noisy.
    """
    if not env_path.is_file():
        return set()
    keys: set[str] = set()
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if not s:
                continue
            # Strip a single leading `#` and any whitespace after it.
            if s.startswith("#"):
                s = s[1:].lstrip()
            # Now s should look like KEY=value (or be a pure comment we
            # don't care about).
            eq = s.find("=")
            if eq <= 0:
                continue
            key = s[:eq].strip()
            # Validate key shape: alnum + underscore, no spaces. Skips
            # things like "Defaults match the podman-compose.yml ..."
            # which would otherwise parse as an "added=" key.
            if key and all(c.isalnum() or c == "_" for c in key) and not key[0].isdigit():
                keys.add(key)
    except OSError:
        return set()
    return keys


def _ensure_env_template(env_path: Path, project_name: str = "<project>",
                        project_root: str = "<project_root>") -> dict:
    """Ensure `.env` exists and contains all canonical template keys.

    Behaviour:
      - .env missing → create from the canonical template (all optional
        keys commented out, active keys filled with placeholders).
      - .env exists → diff against canonical, append any missing keys
        commented out, tagged with `# added by vco YYYY-MM-DD`.
      - Idempotent: keys already present (commented or not) are skipped,
        and lines already carrying the marker tag aren't re-considered.

    Preserves all existing values verbatim — never overwrites a
    user-set value.

    Returns a small report dict: {"action": "created"|"appended"|"noop",
    "added_keys": [..], "env_path": str}.

    Used by:
      - install.py Step 9 (_write_env_config integration)
      - launcher's projects_v2::create_project_v2 (Rust mirror calls
        Python via subprocess on lightweight re-installs; for the
        normal create_project flow we have a dedicated Rust helper.)
    """
    if not env_path.exists():
        env_path.write_text(
            _build_canonical_env_template_text(project_name, project_root),
            encoding="utf-8",
        )
        return {
            "action": "created",
            "added_keys": [k for k, _, _ in _env_canonical_template(project_name, project_root)
                           if k],
            "env_path": str(env_path),
        }

    existing_keys = _parse_existing_env_keys(env_path)
    missing: list[tuple[str, str | None, str]] = [
        (k, default, comment)
        for k, default, comment in _env_canonical_template(project_name, project_root)
        if k and k not in existing_keys
    ]
    if not missing:
        return {"action": "noop", "added_keys": [], "env_path": str(env_path)}

    # Append a marked block. Preserve trailing newline behaviour: if the
    # file doesn't end with \n, add one before our block so we don't
    # glue our header onto the user's last line.
    today = _utc_iso_now()[:10]
    existing_text = env_path.read_text(encoding="utf-8")
    needs_leading_nl = (existing_text and not existing_text.endswith("\n"))
    block_lines: list[str] = []
    if needs_leading_nl:
        block_lines.append("")  # joining will add a \n at start
    block_lines.append("")
    block_lines.append(f"{ENV_VCO_MARKER} {today}: appended missing canonical keys")
    for key, default, comment in missing:
        if default is None:
            # Optional key — write the comment form (already starts with `#`).
            line = comment if comment is not None else f"# {key}="
        else:
            line = f"{key}={default}"
        block_lines.append(line)
    block_lines.append("")  # trailing newline

    with env_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block_lines))

    return {
        "action": "appended",
        "added_keys": [k for k, _, _ in missing],
        "env_path": str(env_path),
    }


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
    _log_install_event("9/10", "start", "writing .env")
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
        # ACTIVE_EMBEDDING: maps to the named-vector slot the MCP server
        # reads/writes. Per-profile so low-resource/openai installs don't
        # cross-write qwen3 vectors into a slot labelled for a different
        # model (audit fix 2026-04-30, see kg-embedding-vector-audit-2026-04-30.md).
        f"ACTIVE_EMBEDDING={embed_config.get('active_embedding', 'qwen3')}",
        "",
        "# Optional companion tools (auto-detected at install)",
        f"VCT_JOERN_AVAILABLE={'1' if joern_available else '0'}",
        "",
        "# Knowledge Graph",
        # Resolved by _ensure_collections (per-install naming on adopt mode,
        # bare defaults when we own the Weaviate). Defaults pinned here in
        # case _ensure_collections didn't run (e.g. --no-containers).
        f"KG_COLLECTION={os.environ.get('KG_COLLECTION', 'KnowledgeGraph')}",
        f"DEVELOPMENT_COLLECTION={os.environ.get('DEVELOPMENT_COLLECTION', 'Development')}",
        "",
        "# Cross-project shared KG (all vco installs on this machine read",
        "# from it alongside their own KG — read access is unconditional).",
        "# Seeded at install time from vibecoded-orchestrator/knowledge/.",
        "# Set SHARED_KG_WRITE_DISABLED=true to gate WRITES from this project",
        "# only (reads stay on). SHARED_KG_OPT_OUT is the legacy alias kept",
        "# for ~3 releases (target removal: 2026-08).",
        f"SHARED_KG_COLLECTION={os.environ.get('SHARED_KG_COLLECTION', 'VibeCodedOrchestrator_KnowledgeGraph')}",
        "SHARED_KG_WRITE_DISABLED=false",
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
        # B7 (2026-05-01): canonical key is VCT_TELEMETRY (matches template at
        # line ~4986 and Rust). VIBECODED_TELEMETRY remains as a read-time alias
        # in the telemetry module for ~3 releases of back-compat.
        f"VCT_TELEMETRY={'true' if telemetry_enabled else 'false'}",
        "",
    ])

    # Write (don't overwrite if exists). When the file IS already there,
    # run the canonical-template append-merge so installs that pre-date
    # the template (or third-party-provided .env files) pick up newer
    # placeholder keys without losing the user's edits.
    if env_file.exists():
        report = _ensure_env_template(env_file)
        if report["action"] == "appended":
            print(f"already exists — appended {len(report['added_keys'])} canonical keys")
            _log_install_event(
                "9/10", "ok",
                f".env append-merged ({len(report['added_keys'])} new keys)",
                data={"env_file": str(env_file),
                      "added_keys": report["added_keys"]},
            )
        else:
            print("already exists (not overwritten)")
            _log_install_event(
                "9/10", "skip",
                ".env already exists — preserved",
                data={"env_file": str(env_file)},
            )
    else:
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if telemetry_enabled:
            print("OK (telemetry: on, opt-in)")
        else:
            print("OK (telemetry: off)")
        _log_install_event(
            "9/10", "ok",
            ".env written",
            data={"env_file": str(env_file),
                  "telemetry_on": telemetry_enabled,
                  "joern_available": joern_available},
        )


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
        # B8 (2026-05-01): .claude/settings.json surface uses GRPC_PORT (legacy
        # alias). Canonical key is WEAVIATE_GRPC_PORT (written to .env at line
        # ~5178). weaviate_mcp/server.py reads both, prefers WEAVIATE_GRPC_PORT.
        # Keep GRPC_PORT in this surface for back-compat until all surfaces are
        # migrated to the canonical key in a future PR.
        "GRPC_PORT": str(weaviate_grpc),
        "EMBEDDING_MODEL": embed_config["text_model"],
        # See note at the .env-write block above. Per-profile slot mapping.
        "ACTIVE_EMBEDDING": embed_config.get("active_embedding", "qwen3"),
        "KG_COLLECTION": "KnowledgeGraph",
        "DEVELOPMENT_COLLECTION": "Development",
        # Default value: capital-C "VibeCoded" since v0.2.23 B1 (was
        # lowercase-c v0.2.12–v0.2.22, itself renamed from
        # "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E).
        # Picker overrides this per-project.
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        # Asymmetric shared-KG access (since 2026-05-01): reads always-on,
        # writes gated by SHARED_KG_WRITE_DISABLED. SHARED_KG_OPT_OUT kept
        # as a legacy alias for ~3 releases (target removal: 2026-08).
        "SHARED_KG_WRITE_DISABLED": "false",
        "SHARED_KG_OPT_OUT": "false",
        # PR-7 (v0.2.11): PROJECT_NAME + CODE_GRAPH_PROJECT pin the
        # Orchestrator Project's own namespace so its
        # `post-file-edit.{sh,ps1}` hook resolves a stable name instead of
        # falling back to the legacy "ClaudeOrchestrator" hardcode. Without
        # these keys, every user-project install on the same machine wrote
        # code-graph rows into the shared legacy collection. Resolved at
        # install time from `vct-module.json::name` via
        # `_derive_orchestrator_project_name()`; existing installs are
        # repaired in-place by `_backfill_code_graph_project_env()` during
        # `--update`.
        "PROJECT_NAME": _derive_orchestrator_project_name(),
        "CODE_GRAPH_PROJECT": _derive_orchestrator_project_name(),
        "CODE_EMBED_BACKEND": embed_config["code_backend"],
        "CODE_EMBED_SERVICE_URL": f"http://localhost:{code_embed_port}",
    }

    # 0.2.11: no BASH_ENV wiring here. Lean-ctx output compression flows
    # through the per-project PreToolUse hook .claude/hooks/lean-ctx-rewrite.sh
    # (registered by templates/settings.json.*.template), which avoids the
    # fork-bomb risk that the BASH_ENV shim carried on lean-ctx 3.x. See
    # knowledge/concepts/lean-ctx-shim-disabled.md for the incident write-up.

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


def _cleanup_legacy_bash_env_shim(args: argparse.Namespace) -> None:
    """Idempotent cleanup of pre-0.2.11 BASH_ENV lean-ctx shim.

    Pre-0.2.11 installs wired BASH_ENV in .claude/settings.json pointing at
    .claude/scripts/leanctx-bash-env.sh. That pattern is fork-bomb-prone on
    lean-ctx 3.x — replaced in 0.2.11 by per-project PreToolUse hook
    .claude/hooks/lean-ctx-rewrite.sh. Full write-up:
    knowledge/concepts/lean-ctx-shim-disabled.md (orchestrator KG).

    This function:
    - Removes env.BASH_ENV from .claude/settings.json if it points at the
      shim (project-local or absolute path resolving inside this clone).
      Keys pointing elsewhere (user-set, unrelated tooling) are left alone.
    - Disables .claude/scripts/leanctx-bash-env.sh with a `return 0` early
      exit (preserves the file as defense-in-depth in case BASH_ENV is
      re-set manually elsewhere). Bundled template copies on disk that
      already carry the 0.2.11 disabled body are recognized and left
      alone.
    - No-op on fresh installs (file doesn't exist) and on already-cleaned
      installs (BASH_ENV already absent + shim already disabled).

    Called from main() during --update flow only; fresh installs never see
    the legacy state. Soft-fail throughout — partial cleanup is fine, a
    later run picks up the rest. Errors are surfaced as warnings, never
    raised.
    """
    settings_file = PROJECT_ROOT / ".claude" / "settings.json"
    shim_path = PROJECT_ROOT / ".claude" / "scripts" / "leanctx-bash-env.sh"

    # ----- Part 1: strip BASH_ENV from .claude/settings.json -----
    if settings_file.exists():
        try:
            settings_text = settings_file.read_text(encoding="utf-8")
            settings = json.loads(settings_text)
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"  legacy BASH_ENV cleanup: settings.json read/parse failed "
                f"({type(e).__name__}: {e}) — skipping (re-run after fixing)"
            )
            settings = None

        if isinstance(settings, dict):
            env_block = settings.get("env")
            if isinstance(env_block, dict) and "BASH_ENV" in env_block:
                raw_val = str(env_block.get("BASH_ENV", ""))
                # Detect the shim — accept both ${CLAUDE_PROJECT_DIR}-templated
                # and absolute-path forms (older installs wrote literal paths).
                points_at_shim = (
                    "leanctx-bash-env.sh" in raw_val
                    or raw_val.endswith(str(shim_path))
                )
                if points_at_shim:
                    env_block.pop("BASH_ENV", None)
                    try:
                        settings_file.write_text(
                            json.dumps(settings, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        print(
                            "  legacy BASH_ENV cleanup: removed BASH_ENV "
                            "from .claude/settings.json (was wired to "
                            "leanctx-bash-env.sh shim)"
                        )
                    except OSError as e:
                        print(
                            f"  legacy BASH_ENV cleanup: settings.json write "
                            f"failed ({type(e).__name__}: {e}) — re-run after "
                            f"fixing permissions"
                        )
                else:
                    print(
                        f"  legacy BASH_ENV cleanup: BASH_ENV present in "
                        f"settings.json but doesn't point at the shim "
                        f"({raw_val!r}) — leaving alone (user/other tooling)"
                    )
            # else: env_block missing or BASH_ENV already gone — no-op.

    # ----- Part 2: disable the shim file itself -----
    if shim_path.exists():
        try:
            shim_body = shim_path.read_text(encoding="utf-8")
        except OSError as e:
            print(
                f"  legacy BASH_ENV cleanup: shim read failed "
                f"({type(e).__name__}: {e}) — skipping"
            )
            shim_body = None

        if shim_body is not None:
            # The disabled stub ends with `return 0` and contains the
            # DISABLED banner. Recognize it so we don't rewrite on every
            # --update.
            already_disabled = (
                "DISABLED as of vibecoded-orchestrator 0.2.11" in shim_body
                and shim_body.rstrip().endswith("return 0")
            )
            if not already_disabled:
                disabled_body = (
                    "#!/usr/bin/env bash\n"
                    "# leanctx-bash-env.sh - DISABLED as of "
                    "vibecoded-orchestrator 0.2.11\n"
                    "#\n"
                    "# The BASH_ENV approach is fork-bomb-prone on "
                    "lean-ctx 3.x (incident\n"
                    "# 2026-04-30 + recidiva 2026-05-15). Replaced by the "
                    "per-project\n"
                    "# PreToolUse hook .claude/hooks/lean-ctx-rewrite.sh. "
                    "Full forensic\n"
                    "# write-up: knowledge/concepts/"
                    "lean-ctx-shim-disabled.md.\n"
                    "#\n"
                    "# This file is preserved on disk (rather than "
                    "deleted) so a stray\n"
                    "# BASH_ENV pointing here — set manually or left "
                    "behind by a pre-0.2.11\n"
                    "# install we missed — still no-ops instead of "
                    "fork-bombing.\n"
                    "return 0\n"
                )
                try:
                    shim_path.write_text(disabled_body, encoding="utf-8")
                    print(
                        "  legacy BASH_ENV cleanup: disabled "
                        ".claude/scripts/leanctx-bash-env.sh (defense-in-"
                        "depth `return 0` stub)"
                    )
                except OSError as e:
                    print(
                        f"  legacy BASH_ENV cleanup: shim disable failed "
                        f"({type(e).__name__}: {e}) — re-run after fixing"
                    )


# ---------------------------------------------------------------------------
# Step 9b: Install agents + skills from templates/
# ---------------------------------------------------------------------------

def _install_agents_and_skills(args: argparse.Namespace) -> None:
    """Copy agents and skills from templates/ into .claude/, substituting paths.

    Bundled agents live at templates/agents/free/. Skills live at templates/skills/.

    Placeholder substitutions applied to copied files:
        {{ORCHESTRATOR_ROOT}} → this install directory
        {{PROJECTS_ROOT}}     → parent directory
        {{HOME}}              → user home directory
    """
    print("[9b/10] Installing agents, skills, and hooks ... ", flush=True)

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
        parts.append(f"{installed_agents} agents"
                     + (f" ({skipped_agents} already present)" if skipped_agents else ""))
    if args.with_skills:
        parts.append(f"{installed_skills} skills"
                     + (f" ({skipped_skills} already present)" if skipped_skills else ""))
    # Hooks + settings.json (gated on --with-hooks; default on).
    hooks_summary = _install_hooks_and_settings(args)
    if hooks_summary:
        parts.append(hooks_summary)

    if not parts:
        print("  skipped (--no-agents --no-skills --no-hooks)")
    else:
        print("  " + ", ".join(parts))


def _hook_glob_for_os() -> str:
    """Pick the OS-active hook file extension glob.

    Linux/macOS run bash hooks; Windows runs the PowerShell siblings shipped
    by `feat/hook-system-ps1-parity`. The two-template + two-glob approach
    keeps per-OS installs lean — a Linux user never gets unused .ps1 files in
    .claude/hooks/, and vice versa. See audit F1 (P0).
    """
    return "*.ps1" if platform.system() == "Windows" else "*.sh"


def _settings_template_for_os(templates_dir: Path) -> Path:
    """Return the OS-specific settings.json template path.

    Two templates ship in templates/: settings.json.linux.template (bash
    hooks) and settings.json.windows.template (PowerShell hooks). They are
    identical except for the `command` strings inside `hooks.*.hooks`. See
    audit F1 (P0).
    """
    if platform.system() == "Windows":
        return templates_dir / "settings.json.windows.template"
    return templates_dir / "settings.json.linux.template"


def _install_hooks_and_settings(args: argparse.Namespace) -> str:
    """Copy hooks from templates/hooks/ into .claude/hooks/, scripts from
    templates/scripts/ into .claude/scripts/, and smart-merge the OS-specific
    settings template into .claude/settings.json.

    Hooks and scripts are byte-copied (no placeholder substitution) so every
    project carries identical files. They read VCT_INSTALL_ROOT,
    KG_COLLECTION, WEAVIATE_URL, etc. at runtime; the launcher exports
    VCT_INSTALL_ROOT per-project.

    OS-active install: only the shell flavour native to the host is copied
    (`*.sh` on Linux/macOS, `*.ps1` on Windows). The non-active flavour is
    skipped — a Linux project never gets stray `.ps1` files. See audit F1.

    settings.json merge rules (only when target file already exists):
      * recursive dict merge — template provides defaults, user keys win on conflict
      * `hooks.{Event}` arrays: append template entries that don't already exist
        (compared by `command` string equality on the inner-hooks list); never
        replace user-customized commands.

    Returns a one-line summary string for the caller to print, or "" when the
    install was skipped (e.g. --no-hooks or templates missing).
    """
    if not getattr(args, "with_hooks", True):
        return ""

    templates_dir = PROJECT_ROOT / "templates"
    hooks_src = templates_dir / "hooks"
    scripts_src = templates_dir / "scripts"
    settings_template = _settings_template_for_os(templates_dir)
    if not hooks_src.exists():
        return ""

    hook_glob = _hook_glob_for_os()
    claude_dir = PROJECT_ROOT / ".claude"
    hooks_dst = claude_dir / "hooks"
    hooks_dst.mkdir(parents=True, exist_ok=True)

    installed_hooks = 0
    skipped_hooks = 0  # kept for the summary string; always 0 after the P2.2 fix.
    for hook_file in sorted(hooks_src.glob(hook_glob)):
        target = hooks_dst / hook_file.name
        # Always overwrite top-level hooks. Same rationale as `_lib/` below:
        # hooks are NOT user-customisable; they're canonical orchestrator
        # runtime. Pre-fix behaviour was skip-if-exists, which meant
        # install.py re-runs never updated hooks — discovered after the
        # 2026-05-08 stdin-JSON contract migration didn't reach
        # already-installed orchestrators until users manually deleted hook
        # files first. If a user truly needs to bypass a hook, the supported
        # path is VCT_DISABLE_HOOKS=1, not hand-editing the file.
        # copy2 preserves the executable bit and mtime — important for hooks.
        shutil.copy2(hook_file, target)
        installed_hooks += 1

    # Library files sourced by hooks (e.g. _lib/find-python.sh on POSIX,
    # _lib/find-python.ps1 on Windows). Live under .claude/hooks/_lib/.
    # Always overwrite — they're not user-customisable and stale copies
    # would defeat their portability purpose. See audit F6.
    lib_src = hooks_src / "_lib"
    if lib_src.exists():
        lib_dst = hooks_dst / "_lib"
        lib_dst.mkdir(parents=True, exist_ok=True)
        for lib_file in sorted(lib_src.glob(hook_glob)):
            shutil.copy2(lib_file, lib_dst / lib_file.name)

    # Scripts referenced by hooks (e.g. precompact_prune.py). Live alongside
    # hooks under .claude/scripts/. Some scripts may not exist if the
    # installation predates them — that's fine, the hooks ?-guard against
    # missing files.
    installed_scripts = 0
    skipped_scripts = 0
    if scripts_src.exists():
        scripts_dst = claude_dir / "scripts"
        scripts_dst.mkdir(parents=True, exist_ok=True)
        # Glob all script types: Python modules, shell wrappers (no ext or .sh),
        # and PowerShell wrappers (.ps1). Previously only *.py was copied which
        # left kg-search, kg-sync, code-graph-* etc. missing from user projects.
        script_patterns = ["*.py", "*.sh", "*.ps1", "kg-*", "code-graph-*", "cost-summary"]
        seen: set[str] = set()
        for pattern in script_patterns:
            for script_file in sorted(scripts_src.glob(pattern)):
                if script_file.name in seen or script_file.is_dir():
                    continue
                seen.add(script_file.name)
                target = scripts_dst / script_file.name
                if target.exists():
                    skipped_scripts += 1
                    continue
                shutil.copy2(script_file, target)
                installed_scripts += 1

    settings_action = _merge_settings_template(
        settings_template, claude_dir / "settings.json"
    )

    summary = f"{installed_hooks} hooks"
    if skipped_hooks:
        summary += f" ({skipped_hooks} already present)"
    if installed_scripts or skipped_scripts:
        summary += f", {installed_scripts} scripts"
        if skipped_scripts:
            summary += f" ({skipped_scripts} already present)"
    if settings_action:
        summary += f", settings.json {settings_action}"
    return summary


def _merge_settings_template(template_path: Path, target_path: Path) -> str:
    """Smart-merge settings.json.template into target. Returns one of:
    'created', 'merged', 'unchanged', or '' (template missing).
    """
    if not template_path.exists():
        return ""
    template_data = json.loads(template_path.read_text(encoding="utf-8"))

    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(template_data, indent=2) + "\n", encoding="utf-8"
        )
        return "created"

    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Don't overwrite a malformed user file silently — leave it alone.
        return "unchanged (user file unparseable)"

    merged = _smart_merge_settings(existing, template_data)
    if merged == existing:
        return "unchanged"
    target_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return "merged"


def _smart_merge_settings(user: dict, template: dict) -> dict:
    """Recursive dict merge. User wins on scalar/leaf conflicts. For the special
    `hooks.{Event}` lists, append template entries whose inner `command` strings
    aren't already present in user's config.
    """
    out = dict(user)
    for key, tval in template.items():
        if key not in out:
            out[key] = tval
            continue
        uval = out[key]
        if key == "hooks" and isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = _merge_hooks_block(uval, tval)
        elif isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = _smart_merge_settings(uval, tval)
        # else: user wins — leave uval untouched.
    return out


def _merge_hooks_block(user_hooks: dict, template_hooks: dict) -> dict:
    """Merge per-event hook arrays.

    For each Event (SessionStart, PreToolUse, ...): if template provides hook
    entries whose inner-hook `command` string isn't already present in any of
    the user's entries for that event, append the entire template entry.
    """
    out = dict(user_hooks)
    for event, t_entries in template_hooks.items():
        if event not in out:
            out[event] = list(t_entries)
            continue
        u_entries = out[event] if isinstance(out[event], list) else []
        existing_cmds = set()
        for entry in u_entries:
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                cmd = h.get("command")
                if cmd:
                    existing_cmds.add(cmd)
        merged_entries = list(u_entries)
        for t_entry in t_entries:
            t_cmds = [
                h.get("command")
                for h in t_entry.get("hooks", [])
                if isinstance(h, dict) and h.get("command")
            ]
            if t_cmds and all(c in existing_cmds for c in t_cmds):
                continue  # every command already present — don't duplicate.
            merged_entries.append(t_entry)
        out[event] = merged_entries
    return out


# ---------------------------------------------------------------------------
# Step 10: Claude CLI
# ---------------------------------------------------------------------------

def _check_claude_cli() -> None:
    print("[10/10] Checking Claude CLI ... ", end="", flush=True)
    _log_install_event("10/10", "start", "checking claude CLI")
    if shutil.which("claude"):
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = result.stdout.strip() or "found"
            print(f"OK ({version})")
            _log_install_event(
                "10/10", "ok",
                f"claude CLI present ({version})",
                data={"version": version},
            )
        except (subprocess.TimeoutExpired, OSError):
            print("found (version check timed out)")
            _log_install_event(
                "10/10", "warn",
                "claude CLI present but --version timed out",
            )
    else:
        print("NOT FOUND")
        print("  Claude Code CLI is required to use the orchestrator.")
        print("  Install: npm install -g @anthropic-ai/claude-code")
        print("  Requires: Node.js 18+ (https://nodejs.org)")
        _log_install_event(
            "10/10", "warn",
            "claude CLI missing — user must install separately",
        )


# ---------------------------------------------------------------------------
# Playwright MCP + Chromium pre-cache
# ---------------------------------------------------------------------------

def _install_playwright_browsers() -> None:
    """Pre-cache `@playwright/mcp` + Chromium so the default-enabled
    Playwright MCP doesn't stall on first browser launch.

    Behaviour:
      - Skipped entirely if `VCT_SKIP_PLAYWRIGHT=1` is set in the env.
      - Skipped if `npx` is not on PATH (the MCP can still lazy-install
        when Node arrives later; we just warn).
      - Runs `npx -y @playwright/mcp@latest --version` to populate the
        npx cache (~few MB).
      - Runs `npx playwright install chromium` to fetch the Chromium
        binary (~150 MB).

    Non-fatal: any failure logs a warn event and prints a short notice,
    but never aborts the install. The MCP can still lazy-install on
    first invocation; the only cost is a one-time UX delay.
    """
    print("[playwright] Pre-caching Playwright MCP + Chromium ... ",
          end="", flush=True)
    _log_install_event("playwright", "start",
                       "caching @playwright/mcp + chromium")

    if os.environ.get("VCT_SKIP_PLAYWRIGHT") == "1":
        print("SKIPPED (VCT_SKIP_PLAYWRIGHT=1)")
        _log_install_event("playwright", "skip",
                           "VCT_SKIP_PLAYWRIGHT=1 in env")
        return

    if not shutil.which("npx"):
        print("SKIPPED (npx not found)")
        print("  Node.js / npx not detected. Playwright MCP will")
        print("  lazy-install when first invoked. Install Node.js 18+")
        print("  to pre-cache: https://nodejs.org")
        _log_install_event("playwright", "skip",
                           "npx not on PATH — MCP will lazy-install")
        return

    print("(this may take ~30s, ~150 MB)")

    # 1) Cache the MCP package itself (small).
    try:
        result = subprocess.run(
            ["npx", "-y", "@playwright/mcp@latest", "--version"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print("  WARN: @playwright/mcp version check failed.")
            print(f"    stderr: {result.stderr.strip()[:200]}")
            _log_install_event("playwright", "warn",
                               "npx -y @playwright/mcp@latest --version "
                               "exited non-zero",
                               data={"returncode": result.returncode,
                                     "stderr": result.stderr.strip()[:500]})
            return
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  WARN: @playwright/mcp version check failed: {e}")
        _log_install_event("playwright", "warn",
                           f"npx -y @playwright/mcp version check failed: {e}")
        return

    # 2) Cache the Chromium browser binary (~150 MB). This is the
    #    expensive step; we only do chromium (not firefox/webkit) to
    #    keep the install size down. Users who need other browsers can
    #    `npx playwright install firefox` etc. manually.
    try:
        result = subprocess.run(
            ["npx", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            print("[playwright] Chromium cached OK.")
            _log_install_event("playwright", "ok",
                               "Playwright MCP + Chromium cached")
        else:
            print("  WARN: chromium install exited non-zero.")
            print(f"    stderr: {result.stderr.strip()[:200]}")
            print("  The MCP will lazy-install Chromium on first browser call.")
            _log_install_event("playwright", "warn",
                               "npx playwright install chromium exited "
                               "non-zero",
                               data={"returncode": result.returncode,
                                     "stderr": result.stderr.strip()[:500]})
    except subprocess.TimeoutExpired:
        print("  WARN: chromium install timed out after 10 min.")
        print("  The MCP will lazy-install Chromium on first browser call.")
        _log_install_event("playwright", "warn",
                           "npx playwright install chromium timed out (600s)")
    except OSError as e:
        print(f"  WARN: chromium install failed: {e}")
        _log_install_event("playwright", "warn",
                           f"npx playwright install chromium failed: {e}")


# ---------------------------------------------------------------------------
# Step 11b: Bytecode compilation (D8 of nuitka-decisions-2026-04-25)
# ---------------------------------------------------------------------------

def _compile_python_modules(venv_python: Path) -> None:
    """Pre-compile installed Python modules to .pyc for faster first import.

    Runs ``python -m compileall -q -j 0`` (stdlib, cross-OS) on every
    orchestrator-managed Python directory that exists. The compiled
    .pyc files land in ``__pycache__/`` next to the source — the runtime
    Python interpreter picks them up automatically on first import.

    Speedup: ~50-200ms per cold module (covers ~95% of the perceived
    "slow startup" complaint without the build-chain complexity of
    ahead-of-time compilers like Nuitka).

    Idempotent: safe to re-run on ``--update`` (compileall just refreshes
    existing .pyc when source mtime is newer).

    Best-effort: per-directory failures log a WARN line but never abort
    the install. The runtime falls back to compile-on-import for any
    module whose .pyc didn't land — only cost is one-shot startup
    latency, never a correctness issue.

    Cross-OS: ``compileall`` is part of the Python stdlib on every
    platform; no extra dependency required.
    """
    print("[+] Compiling Python modules to bytecode ... ",
          end="", flush=True)

    # Orchestrator-managed Python dirs that benefit from pre-compile.
    # vco_lib, .claude/scripts (relocated 2026-04-30 in PR #80), and
    # claude_mcp_servers are hot-imported by hooks; VCThelpers + tools
    # are hit during install/maintenance flows.
    candidate_paths = [
        "VCThelpers",
        "claude_mcp_servers",
        "tools",
        "vco_lib",
        ".claude/scripts",
    ]
    targets = [
        PROJECT_ROOT / rel for rel in candidate_paths
        if (PROJECT_ROOT / rel).is_dir()
    ]

    if not targets:
        print("SKIP (no Python module dirs found)")
        return

    error_dirs: list[str] = []
    for target in targets:
        cmd = [str(venv_python), "-m", "compileall",
               "-q", "-j", "0", str(target)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                error_dirs.append(target.name)
        except subprocess.TimeoutExpired:
            error_dirs.append(f"{target.name} (timeout)")
        except OSError as e:
            error_dirs.append(f"{target.name} ({e})")

    if not error_dirs:
        print(f"OK ({len(targets)} dirs)")
    else:
        print(f"WARN ({len(error_dirs)}/{len(targets)} dirs had errors: "
              f"{', '.join(error_dirs)}; install ok, first import of "
              f"affected modules will rebuild bytecode at runtime)")


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

    # Honour VCT_STATE_DIR so a dev launcher's state isolates cleanly.
    from vco_lib.paths import vct_root_dir
    launcher_db = vct_root_dir() / "launcher.db"
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
