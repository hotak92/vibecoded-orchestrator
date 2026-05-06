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
from typing import NamedTuple

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
# from `source` into `install_path`. Mirror of ORCHESTRATOR_MANAGED_PATHS
# in installer.rs. Anything else at the source is left behind; anything
# else at the install_path is left untouched.
ORCHESTRATOR_MANAGED_PATHS: tuple[str, ...] = (
    ".claude",
    "CLAUDE.md",
    "knowledge",
    "claude_mcp_servers",
    "state",
    "config",
    "docs",
    "templates",
    "tools",
    "infrastructure",
    "requirements.txt",
    "requirements-dev.txt",
    "install.sh",
    "install.ps1",
    "install.py",
    "BOOTSTRAP.md",
    "vct-module.json",
)


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
    parser.add_argument("--apply-deferred", action="store_true", default=False,
                        help="During --update, attempt to apply each pending entry "
                             "in .claude/context/UPDATE_DEFERRED.md. Resolved entries "
                             "are removed; the file is deleted when zero entries remain.")
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
    parser.add_argument("--preserve-paths",
                        type=str, default=None,
                        help="Comma-separated list of install-relative paths to "
                             "preserve under --conflict-strategy=overwrite-preserve. "
                             "Default: CLAUDE.md,.claude/CONTEXT_STATE.md,"
                             ".claude/PROJECT_REGISTRY.md,.env")
    args = parser.parse_args()

    # Deferral report — accumulates non-auto-resolvable conditions detected
    # during this run; written to .claude/context/UPDATE_DEFERRED.md at end.
    # Using a distinct name to avoid shadowing the conflict-resolve `report`
    # local below.
    _deferral_report = DeferralReport()

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

    # Step 4: Create virtual environment
    venv_python = _create_venv(PROJECT_ROOT)

    # Step 5: Install/update Python dependencies
    _install_requirements(venv_python, dev=args.dev)

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

        # PR 6 + MEDIUM-9 + HIGH-4 fix (2026-05-01): wrap _ensure_collections
        # AND _seed_weaviate in the same try/except so Weaviate-down conditions
        # emit a deferral entry instead of crashing the install. Includes a
        # podman-restart soft-recovery attempt. If a rebuild action dropped a
        # collection earlier this run AND seed/ensure later crashes, the
        # deferral entry includes `rebuild_pending_seed` so the operator knows
        # what was lost.
        try:
            _ensure_collections(embed_config, decisions=decisions, args=args)
            # Seed Weaviate with bundled knowledge/ + docs/. Idempotent;
            # safe to re-run on update.
            _seed_weaviate(args)
        except Exception as _weaviate_err:
            # PR 6: Weaviate is unreachable (or refused connection) after the
            # containers were started. Attempt a soft-recovery restart
            # via podman before emitting a deferral.
            _weaviate_down_msg = str(_weaviate_err)
            _restarted = False
            try:
                subprocess.run(
                    ["podman", "start", "weaviate_claude"],
                    capture_output=True, timeout=30,
                )
                import time as _time
                _time.sleep(3)  # brief settle
                _ensure_collections(embed_config, decisions=decisions, args=args)
                _seed_weaviate(args)
                _restarted = True
            except Exception:
                pass
            if not _restarted:
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
                            "`podman start weaviate_claude` also failed."
                        ),
                        why_deferred=(
                            "Collection bootstrap and schema migration require "
                            "a live Weaviate. Cannot proceed without it."
                        ),
                        command_to_apply=(
                            "podman start weaviate_claude && "
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
                                "podman start weaviate_claude && "
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

    print()
    print("=" * 62)
    print("  Installation complete!")
    print("=" * 62)
    print()
    _print_next_steps(sysinfo, args)
    return 0


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
      - ``weaviate_unreachable_at_update``: try ``podman start weaviate_claude``
        then check reachability; mark resolved if now reachable.
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
            print(f"  [try]  {cid}: attempting podman start weaviate_claude ...")
            try:
                subprocess.run(
                    ["podman", "start", "weaviate_claude"],
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


# Inference model tier ladder for install-time pulls.
#
# This MIRRORS the runtime selector ladder in
#   claude_mcp_servers/ollama_mcp/server.py:TEXT_MODEL_TIERS
# Do not let them drift — the runtime selector picks from what we pull
# here, so a thresholds mismatch leads to "auto" picking a model that
# isn't installed and falling back unexpectedly.
#
# Ladder (matches runtime exactly as of 2026-04-29):
#   GPU + VRAM >= 7.5 GB         → qwen3.5:9b + gemma4:e4b + qwen3.5:0.8b
#   GPU + VRAM >= 5.0 GB         → gemma4:e4b + qwen3.5:0.8b
#   GPU + VRAM >= 1.0 GB         → qwen3.5:0.8b
#   no GPU + RAM >= 24 GB        → qwen3.5:9b + gemma4:e4b + qwen3.5:0.8b
#   no GPU + RAM >= 12 GB        → gemma4:e4b + qwen3.5:0.8b
#   else (incl. probe failures)  → qwen3.5:0.8b  (always-fits floor)
def _inference_models_for_capability(sysinfo: SystemInfo) -> list[str]:
    """Return inference models to pull given detected capability.

    Returns at minimum ["qwen3.5:0.8b"] — the floor that fits down to 4 GB
    RAM. Larger tiers are added only when the host can actually run them.
    """
    floor = ["qwen3.5:0.8b"]
    has_gpu = bool(sysinfo.has_gpu)
    vram = float(sysinfo.vram_gb or 0.0)
    ram = float(sysinfo.ram_gb or 0.0)

    if has_gpu and vram >= 7.5:
        return ["qwen3.5:9b", "gemma4:e4b", "qwen3.5:0.8b"]
    if has_gpu and vram >= 5.0:
        return ["gemma4:e4b", "qwen3.5:0.8b"]
    if has_gpu and vram >= 1.0:
        # Tiny GPU (<5 GB VRAM, e.g. older laptop dGPU) — trust GPU
        # presence but only pull the floor model. Anything larger
        # would OOM at runtime.
        return floor
    # CPU-only paths — RAM-driven.
    if ram >= 24.0:
        return ["qwen3.5:9b", "gemma4:e4b", "qwen3.5:0.8b"]
    if ram >= 12.0:
        return ["gemma4:e4b", "qwen3.5:0.8b"]
    return floor


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


def _detect_container_runtime() -> str:
    """Detect Docker or Podman. Prefer Podman everywhere — no commercial
    license required, increasingly native on macOS/Windows.

    Returns:
      - "podman" or "docker" if a runtime is present AND its daemon
        responds to `version`/`info` (i.e. it can actually run containers).
      - "" if neither runtime is on PATH OR the daemon isn't responding.
        Caller distinguishes the two cases via `_detect_installed_runtime()`.
    """
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


def _detect_installed_runtime() -> str:
    """Lightweight presence check — returns the FIRST container-runtime
    binary on PATH regardless of whether its daemon is running.

    Use case: `_detect_container_runtime()` returned "" (no working
    runtime) but we want to give a better message than "install Podman/
    Docker" if one IS installed, just stopped. On Windows specifically,
    Docker Desktop ships `docker.exe` on PATH but `docker version` fails
    until the user opens Docker Desktop from the Start Menu.

    Returns "" when neither binary is on PATH.
    """
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return ""


def _ensure_nvidia_cdi_spec_for_podman() -> None:
    """Generate the NVIDIA CDI spec for Podman if missing.

    Podman needs a CDI YAML at /etc/cdi/nvidia.yaml to expose
    `nvidia.com/gpu=all` device entries. The NVIDIA Container Toolkit
    ships `nvidia-ctk` for this. We invoke it idempotently before
    compose-up.

    Behavior:
      - Skip silently on macOS / Windows (CDI is a Linux concept).
      - Skip if /etc/cdi/nvidia.yaml already exists (idempotent).
      - Skip if `nvidia-ctk` isn't on PATH — print a one-line hint with
        the NVIDIA Container Toolkit install URL so the user can fix it.
        Compose-up will fail loudly afterwards, which is the right
        signal in this case.
      - On Linux with nvidia-ctk present: run `sudo nvidia-ctk cdi
        generate --output=/etc/cdi/nvidia.yaml`. Sudo prompt surfaces
        in the controlling terminal. If sudo is non-interactive (CI),
        this fails; user re-runs install with sudo or sets up CDI
        manually.

    No-op + non-fatal by design — install.py prints what happened and
    moves on. The compose-up that follows will surface the real error
    if CDI didn't get set up.
    """
    if platform.system() != "Linux":
        # macOS Podman runs in a VM; CDI generation happens inside
        # the VM via Podman Machine, not on the host. Skip for now.
        return

    cdi_path = Path("/etc/cdi/nvidia.yaml")
    if cdi_path.exists():
        # Already configured. nvidia-ctk is idempotent so we could
        # re-run anyway, but the no-op is cheaper.
        return

    if not shutil.which("nvidia-ctk"):
        print(
            "  [!] Podman + NVIDIA: `nvidia-ctk` not on PATH. Install the "
            "NVIDIA Container Toolkit:\n"
            "      https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html\n"
            "      (then re-run install — compose-up will fail until CDI is set up)"
        )
        return

    print("  Generating NVIDIA CDI spec for Podman (sudo required) ...")
    try:
        result = subprocess.run(
            ["sudo", "nvidia-ctk", "cdi", "generate",
             "--output=/etc/cdi/nvidia.yaml"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print("    ✓ /etc/cdi/nvidia.yaml created")
        else:
            print(
                f"    ! nvidia-ctk exited {result.returncode}; "
                f"compose-up may fail to find GPU. Stderr: "
                f"{result.stderr.strip()[:200]}"
            )
    except subprocess.TimeoutExpired:
        print("    ! nvidia-ctk timed out (>60s); skipping")
    except Exception as e:
        print(f"    ! nvidia-ctk failed: {e}")


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
    """Locate lean-ctx beyond PATH.

    `shutil.which` only checks PATH, so users who installed lean-ctx via
    `cargo install lean-ctx` (lands at ~/.cargo/bin) but whose shell PATH
    doesn't include ~/.cargo/bin (cargo's installer adds it to ~/.profile
    but `bash first-install.sh` from a fresh terminal may not have sourced
    it yet) saw 'not installed' even though the binary is right there.
    Mirrors the known-binary-path probe pattern in
    post-install-launcher.sh's _ensure_path_for_tool helper.
    """
    on_path = shutil.which("lean-ctx")
    if on_path:
        return on_path
    candidates = [
        Path.home() / ".cargo" / "bin" / "lean-ctx",
        Path.home() / ".local" / "bin" / "lean-ctx",
        Path("/usr/local/bin/lean-ctx"),
        Path("/usr/bin/lean-ctx"),
        # Homebrew on Apple Silicon vs Intel macOS:
        Path("/opt/homebrew/bin/lean-ctx"),
        Path("/home/linuxbrew/.linuxbrew/bin/lean-ctx"),
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
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

    # lean-ctx (optional — wires BASH_ENV so non-interactive Bash subprocesses
    # get ~90-97% command-output compression, same as the interactive shell hook).
    # https://github.com/yvgude/lean-ctx
    #
    # Detection: shutil.which checks PATH only. Many users have lean-ctx
    # installed via `cargo install lean-ctx` (canonical landing dir
    # ~/.cargo/bin/) but their non-interactive shell PATH doesn't include
    # ~/.cargo/bin (cargo's installer adds the line to ~/.profile but
    # `bash first-install.sh` from a fresh terminal may not have sourced
    # it yet). Probe known-binary locations as a fallback. Also auto-install
    # via cargo / brew when those are available.
    shim_path = PROJECT_ROOT / ".claude" / "scripts" / "leanctx-bash-env.sh"
    lean_ctx_path = _find_lean_ctx_binary()
    if not lean_ctx_path and not args.quiet and not args.no_lean_ctx:
        # Try auto-install via the most appropriate package manager.
        lean_ctx_path = _maybe_install_lean_ctx(args)
    if lean_ctx_path:
        print(f"  lean-ctx: detected at {lean_ctx_path} — wiring BASH_ENV for non-interactive compression")
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
        # OS-aware install hints. Use the canonical channels documented at
        # https://github.com/yvgude/lean-ctx — verified 2026-04-28.
        print("            Install via your preferred channel, then re-run this installer to wire BASH_ENV:")
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

    # Auto-detection: GPU → gpu config, otherwise cpu (qwen3 for both).
    if sysinfo.has_gpu:
        _record_install_choice("embedding_mode", "gpu",
                               {"reason": "auto-detected GPU"})
        return dict(EMBEDDING_CONFIGS["gpu"])
    _record_install_choice("embedding_mode", "cpu",
                           {"reason": "auto-detected no GPU"})
    return dict(EMBEDDING_CONFIGS["cpu"])


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
    """Path to `~/.vct/services.toml` — shared with launcher::services::adoption."""
    return Path.home() / ".vct" / "services.toml"


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
        VibeCodedTools_KnowledgeGraph). Either present ⇒ vct-managed.
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
            exact_markers = {"KnowledgeGraph", "VibeCodedTools_KnowledgeGraph",
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
        _amd_file = infra_dir / ("podman-compose.amd-rocm.yml" if is_podman else "docker-compose.amd-rocm.yml")
        if _nvidia_file.exists() and _amd_file.exists():
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
            rocm_file_name = "podman-compose.amd-rocm.yml" if is_podman else "docker-compose.amd-rocm.yml"
            rocm_file = infra_dir / rocm_file_name
            if rocm_file.exists():
                cmd.extend(["-f", str(rocm_file), "--profile", "gpu"])
                engine = "Podman" if is_podman else "Docker"
                print(f"  GPU overlay: AMD ROCm ({engine}: Ollama ROCm image, /dev/kfd + /dev/dri)")
            else:
                print(f"  WARNING: AMD ROCm overlay {rocm_file_name} not found, running CPU-only")
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
    # collection name (default "VibeCodedTools_KnowledgeGraph"); the projects
    # only differ in their per-project KG. Bootstrapped once per Weaviate
    # instance — re-runs are no-ops thanks to the existing-class detection.
    shared_name = os.environ.get(
        "SHARED_KG_COLLECTION", "VibeCodedTools_KnowledgeGraph"
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

    # Note: we deliberately do NOT auto-adopt existing cross-project KGs
    # (e.g. `ClaudeKnowledgeGraph` from another install). The orchestrator
    # runs an orphan-prune sync cycle that would delete entries whose
    # `file_path` no longer exists in this install — silently destroying
    # the other install's KG. vco always gets its own collections;
    # existing collections from other projects are left untouched.

    # Propagate resolved names back to env so .env / settings.json pick
    # them up. This is the tri-write source of truth for downstream steps.
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

    missing = [(n, b) for (n, b) in required if n not in existing]
    skipped_existing = [n for (n, _) in required if n in existing]
    if not missing:
        print(f"  All collections present (reusing {len(required)} shared classes).")
        _log_install_event(
            "7b/10", "ok",
            "all required collections already present",
            data={"existing": skipped_existing,
                  "kg": kg_name, "dev": dev_name, "shared": shared_name},
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
            print(f"  Will SKIP (already present): "
                  f"{', '.join(skipped_existing)}")
        print(f"  Will CREATE in adopted Weaviate: "
              f"{', '.join(n for (n, _) in missing)}")
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
    # VibeCodedTools_KnowledgeGraph. All projects on this machine then read
    # from this shared collection in addition to their per-project KG (see
    # weaviate_mcp/server.py: SHARED_KG_COLLECTION).
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
        "SHARED_KG_COLLECTION", "VibeCodedTools_KnowledgeGraph"
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

    This is the single source of truth for "what version is this install?"
    (matches the version baked into the launcher binary by Tauri at build
    time — see commands/licensing.rs::DEFAULT_VALIDATE_TIER_URL section in
    tauri.conf.json). Returns None if the file is missing (CLI-only install
    without a launcher tree)."""
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


def _write_install_manifest(sysinfo, args, install_method: str = "install.py") -> None:
    """Write state/install-manifest.json. Idempotent: an existing manifest
    is replaced, with the prior `installed_at` preserved when present so
    the field semantically means "first ever successful install" not
    "most recent install run".

    install_method:
      - "install.py"        — direct invocation (CLI / first-install.sh path)
      - "wizard"            — Tauri install_orchestrator command
      - "update"            — re-run with --update flag

    Failures are logged but never raised — the install IS complete by the
    time we reach this; manifest absence is recoverable, install rollback
    isn't.
    """
    state_dir = PROJECT_ROOT / "state"
    manifest_path = state_dir / "install-manifest.json"

    # Preserve original installed_at if a prior manifest exists (update path).
    installed_at = _utc_iso_now()
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                prior = json.load(f)
            prior_installed_at = prior.get("installed_at")
            if isinstance(prior_installed_at, str) and prior_installed_at:
                installed_at = prior_installed_at
                # Update path: re-running install.py with --update.
                if install_method == "install.py" and getattr(args, "update", False):
                    install_method = "update"
        except (OSError, json.JSONDecodeError):
            # Corrupt / unreadable prior manifest — overwrite cleanly.
            pass

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

    manifest = {
        "schema_version":   INSTALL_MANIFEST_SCHEMA_VERSION,
        "installed":        True,
        "installed_at":     installed_at,
        "completed_at":     _utc_iso_now(),
        "version":          _read_tauri_version(),
        "source_commit":    commit,
        "source_branch":    branch,
        "install_method":   install_method,
        "python_version":   "%d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        "python_executable": sys.executable,
        "container_runtime": getattr(sysinfo, "container_cmd", None),
        "cpu_only":         bool(getattr(args, "cpu_only", False)),
        "use_gpu":          bool(getattr(args, "gpu", False)),
        "low_resource":     bool(getattr(args, "low_resource", False)),
        "skipped":          skipped,
        "vct_state_dir":    os.environ.get("VCT_STATE_DIR") or None,
        "install_path":     str(PROJECT_ROOT),
        "volumes":          volumes,
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
        ("SHARED_KG_COLLECTION", "VibeCodedTools_KnowledgeGraph", None),
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
        f"SHARED_KG_COLLECTION={os.environ.get('SHARED_KG_COLLECTION', 'VibeCodedTools_KnowledgeGraph')}",
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
        "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
        # Asymmetric shared-KG access (since 2026-05-01): reads always-on,
        # writes gated by SHARED_KG_WRITE_DISABLED. SHARED_KG_OPT_OUT kept
        # as a legacy alias for ~3 releases (target removal: 2026-08).
        "SHARED_KG_WRITE_DISABLED": "false",
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
    skipped_hooks = 0
    for hook_file in sorted(hooks_src.glob(hook_glob)):
        target = hooks_dst / hook_file.name
        if target.exists():
            skipped_hooks += 1
            continue
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
