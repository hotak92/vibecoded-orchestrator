#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Shared LLM summary backend ladder (v0.2.73 M2 — extracted, one home).

Extracted VERBATIM from ``generate-kg-summary.py`` so the code-summary
generator (``generate-code-summary.py``) does not clone ~450 lines of ladder
logic (the M-2 "one concern, one home" extraction). Both generators are thin
callers of this module; ``generate-kg-summary.py``'s behaviour is unchanged.

Four-tier model selection (in order):
  1. `claude` CLI on PATH      → best quality, requires CLI install (Max sub or API key)
                                 v0.2.23 C10: gated by a smoke-test, not just --version,
                                 so an installed-but-unauthenticated CLI doesn't get picked.
  2. Ollama (local, FREE)      → http://localhost:11435, no extra dep beyond what
                                 the orchestrator already requires for embeddings
  3. OpenAI API (opt-in)       → gated by `kg_summary_openai_consent` app_state key
                                 (default false). Set via launcher Preferences → KG
                                 Summaries. Bypass via `--force-api`. Costs apply.
  4. ANTHROPIC_API_KEY direct  → legacy opt-in fallback. Cost warning logged.
  5. Silent skip               → friendly log line; the caller exits 0.

Env overrides (shared knobs — the ``KG_SUMMARY_*`` names stay canonical for
BOTH callers; the code generator adds a ``CODE_SUMMARY_BACKEND`` alias that
falls back to ``KG_SUMMARY_BACKEND`` via the ``env_keys`` parameter):
  KG_SUMMARY_BACKEND        → force "cli" | "ollama" | "api" | "openai" | "skip"
                              (auto-detect default; "api" = Anthropic, "openai" = OpenAI)
  KG_SUMMARY_OLLAMA_MODEL   → Ollama model tag (default: qwen3.5:9b for 16GB+ VRAM,
                                                         gemma4:e4b for low-VRAM/CPU)
  KG_SUMMARY_OLLAMA_URL     → Ollama base URL (default: http://localhost:11435)
  KG_SUMMARY_OPENAI_MODEL   → OpenAI model name (default: gpt-4o-mini)
  KG_SUMMARY_TIMEOUT        → per-call timeout seconds (default: 180)

Caller integration contract:
  * ``set_logger(fn)`` — route this module's log lines through the caller's
    logger (the KG generator appends to .claude/logs/; default: print).
  * ``reset_backend_cache()`` — clear the per-process backend choice + CLI
    smoke-test cache. The KG generator calls it at import so re-imported
    script modules (the test-isolation pattern) start fresh.
  * ``select_backend(force_api=..., env_keys=..., label=...)`` /
    ``call_llm(prompt, ...)`` — the ladder. ``force_api`` is the operator
    --force-api override for the OpenAI consent gate.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OLLAMA_DEFAULT_MODEL = os.getenv("KG_SUMMARY_OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_URL = os.getenv("KG_SUMMARY_OLLAMA_URL", "http://localhost:11435").rstrip("/")
TIMEOUT = int(os.getenv("KG_SUMMARY_TIMEOUT", "180"))

# v0.2.23 C10 — OpenAI summary backend. Default model is the cheapest
# summary-capable OpenAI model as of 2026-05-21. Users can override via
# the launcher Preferences dropdown (writes app_state) or via env.
OPENAI_DEFAULT_MODEL = os.getenv("KG_SUMMARY_OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# v0.2.23 C10 — app_state keys consumed by the consent gate. Resolved
# at backend-selection time; the actual gate logic lives in
# `select_backend`. This module reads launcher.db directly (stdlib
# sqlite3, no Tauri dependency); a missing DB or table is treated as
# "consent never granted" → consent=False (the safe default).
APP_STATE_KEY_OPENAI_CONSENT = "kg_summary_openai_consent"
APP_STATE_KEY_OPENAI_MODEL = "kg_summary_openai_model"

SYSTEM_PROMPT = (
    "You are a technical documentation summarizer. "
    "Write concise, specific, factual summaries. No filler words, no preamble. "
    "Start directly with the content."
)

# The valid KG_SUMMARY_BACKEND / CODE_SUMMARY_BACKEND force values.
VALID_BACKENDS = {"cli", "ollama", "api", "openai", "skip"}

_BACKEND_CACHE: dict[str, str] = {}

# Caller-injectable logger (the KG generator's log() writes to a per-project
# log file; the code generator injects its own). Default: plain print.
_log = print


def set_logger(fn) -> None:
    """Route this module's log lines through *fn* (signature: (str) -> None)."""
    global _log
    _log = fn


def reset_backend_cache() -> None:
    """Clear the per-process backend choice + CLI smoke-test probe cache."""
    _BACKEND_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# Backend: Claude CLI
# ──────────────────────────────────────────────────────────────────────
def cli_available() -> bool:
    """Return True if `claude` is on PATH AND a smoke-test query succeeds.

    v0.2.23 C10: an installed-but-unauthenticated CLI (the `claude`
    binary exists but the user hasn't logged in / set ANTHROPIC_API_KEY)
    used to be picked as the backend, then every summary call would
    fail with an auth error and the script would not retry against
    Ollama. The smoke-test catches this case at backend-selection
    time.

    The smoke-test is cheap (~1-2 s with `--max-turns 1` against
    haiku). Result is cached in `_BACKEND_CACHE["cli_probe_ok"]` so
    repeated `select_backend()` calls don't re-probe.
    """
    if "cli_probe_ok" in _BACKEND_CACHE:
        return _BACKEND_CACHE["cli_probe_ok"] == "yes"
    if shutil.which("claude") is None:
        _BACKEND_CACHE["cli_probe_ok"] = "no"
        return False
    # Smoke-test: short prompt, tight timeout. We don't care about the
    # output content — just that the CLI returns non-empty stdout and
    # exit 0 (i.e. authenticated and reachable). Failure modes we want
    # to catch: auth error (returns stderr, exit non-zero), network
    # offline, model not available for the account, expired token.
    import subprocess as _sub
    claude_path = shutil.which("claude")
    try:
        result = _sub.run(
            [claude_path, "-p", "say ok", "--model", "haiku", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=20,  # Generous — first-call cold-start can be slow.
            env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
        )
        ok = (result.returncode == 0 and bool(result.stdout.strip()))
    except (_sub.TimeoutExpired, FileNotFoundError, OSError):
        ok = False
    _BACKEND_CACHE["cli_probe_ok"] = "yes" if ok else "no"
    if not ok:
        _log("  KG-summary: claude CLI present but smoke-test failed "
             "(unauthenticated or unreachable) — falling through")
    return ok


def call_cli(prompt: str) -> str:
    import subprocess

    # Resolve the absolute path so subprocess honors PATHEXT on Windows
    # (where `claude` may ship as `claude.cmd` / `claude.bat` via npm).
    # cli_available() already returned True via shutil.which, but Python's
    # subprocess.run won't apply PATHEXT to bare names on Windows.
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise RuntimeError("claude CLI not found on PATH at call time")

    full_prompt = SYSTEM_PROMPT + "\n\n" + prompt
    result = subprocess.run(
        [claude_path, "-p", full_prompt, "--model", "haiku", "--max-turns", "1"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
    return result.stdout.strip()


# ──────────────────────────────────────────────────────────────────────
# Backend: Ollama
# ──────────────────────────────────────────────────────────────────────
# Per-model generation params for short technical summarization. Override
# via env: KG_SUMMARY_OLLAMA_OPTIONS='{"temperature":0.5,"num_ctx":16000}'
#
# num_ctx 24576 (24k) comfortably fits ~3 × 8k chunks of input + system + output.
# Our prompts are ~1.5k tokens (4000-char body truncation) + 350 num_predict.
#
# qwen3.5:* defaults to thinking-mode and emits <think>...</think> blocks
# unless suppressed. Ollama exposes a `think: false` toggle on /api/generate
# (added in 0.5+). We pass it AND post-strip any leaked think blocks defensively.
OLLAMA_MODEL_DEFAULTS: dict[str, dict] = {
    "qwen3.5": {
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_ctx": 32768,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "qwen3": {  # fallback for plain qwen3 tags
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_ctx": 32768,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "gemma4": {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
        "num_ctx": 32768,
        "num_predict": 1024,
    },
    "gemma3": {  # fallback for plain gemma3 tags
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
        "num_ctx": 32768,
        "num_predict": 1024,
    },
}


def _ollama_options_for(model: str) -> dict:
    user_override = os.getenv("KG_SUMMARY_OLLAMA_OPTIONS")
    if user_override:
        try:
            return json.loads(user_override)
        except json.JSONDecodeError:
            pass
    family = model.split(":", 1)[0].lower()
    return OLLAMA_MODEL_DEFAULTS.get(family, {
        "temperature": 0.4,
        "top_p": 0.9,
        "num_ctx": 32768,
        "num_predict": 1024,
    })


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (qwen3 family)."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def call_ollama(prompt: str, model: str = OLLAMA_DEFAULT_MODEL) -> str:
    options = _ollama_options_for(model)
    body = {
        "model": model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": options,
    }
    family = model.split(":", 1)[0].lower()
    if family.startswith("qwen3"):
        body["think"] = False  # Ollama 0.5+ recognizes this; older versions ignore
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _strip_think_blocks(data.get("response", "").strip())


# ──────────────────────────────────────────────────────────────────────
# Backend: Anthropic API (direct)
# ──────────────────────────────────────────────────────────────────────
def api_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def call_api(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    payload = json.dumps(
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 350,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


# ──────────────────────────────────────────────────────────────────────
# Backend: OpenAI API (v0.2.23 C10 — gated by `kg_summary_openai_consent`)
# ──────────────────────────────────────────────────────────────────────
def openai_available() -> bool:
    """Return True if an `OPENAI_API_KEY` env var is set.

    The actual gating (consent + key) is composed by `select_backend`
    — this just answers "is a key present at all". The consent check
    is intentionally a separate step so the log message can distinguish
    "no key" from "key present but consent withheld" (the latter is
    actionable; the former just means OpenAI isn't an option).
    """
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _read_app_state_value(key: str) -> "str | None":
    """Read an app_state row from the launcher SQLite DB.

    Returns the row value as a string, or None when the DB / table /
    row is absent. Soft-fail on any sqlite error → returns None.

    Path resolution mirrors `vco_lib.paths.vct_root_dir`:
      1. `$VCT_STATE_DIR/launcher.db` if VCT_STATE_DIR is set
      2. `~/.vct/launcher.db` otherwise

    We don't pull in vco_lib here so this module stays usable from
    `templates/scripts/` (i.e. inside per-project installs that don't
    necessarily have the orchestrator's vco_lib on PYTHONPATH). The
    path-resolution logic is small enough to inline.
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        db_path = Path(custom) / "launcher.db"
    else:
        db_path = Path.home() / ".vct" / "launcher.db"
    if not db_path.is_file():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return str(row[0]) if row[0] is not None else None
    except Exception:
        # Locked DB, missing table, permission denied, corruption —
        # all treated the same way: row absent → caller picks the
        # default. This module never breaks the user's workflow.
        return None


def openai_consent_granted() -> bool:
    """Return True if the user has explicitly opted in to OpenAI summaries.

    Reads `app_state` key `kg_summary_openai_consent`. Truthy values:
    "true", "1", "yes" (case-insensitive). Anything else (including
    missing row) means "consent NOT granted".
    """
    raw = _read_app_state_value(APP_STATE_KEY_OPENAI_CONSENT)
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1", "yes"}


def _openai_model() -> str:
    """Resolve the OpenAI model to use.

    Priority: env var (operator override) → app_state row (Preferences
    GUI selection) → built-in default (`gpt-4o-mini`).
    """
    env_override = os.getenv("KG_SUMMARY_OPENAI_MODEL", "").strip()
    if env_override:
        return env_override
    stored = _read_app_state_value(APP_STATE_KEY_OPENAI_MODEL)
    if stored:
        return stored
    return OPENAI_DEFAULT_MODEL


def call_openai(prompt: str) -> str:
    """Call OpenAI chat/completions with the configured summary model.

    Uses the chat-completions endpoint (not the legacy completions
    one) because every summary-capable OpenAI model (gpt-4o-mini,
    gpt-4o, gpt-4.1-mini, …) is a chat model. System + user messages
    are sent in the standard two-turn shape.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    model = _openai_model()
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 350,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"OpenAI returned no choices: {data}")
    msg = choices[0].get("message", {})
    return str(msg.get("content", "")).strip()


# ──────────────────────────────────────────────────────────────────────
# Tier dispatch
# ──────────────────────────────────────────────────────────────────────
def _forced_backend(env_keys: tuple[str, ...]) -> str:
    """First non-empty forced-backend env value across *env_keys*.

    The KG generator passes the default ``("KG_SUMMARY_BACKEND",)``; the
    code generator passes ``("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND")``
    (its own alias first, falling back to the shared knob).
    """
    for key in env_keys:
        value = os.getenv(key, "").lower().strip()
        if value:
            return value
    return ""


def select_backend(
    *,
    force_api: bool = False,
    env_keys: tuple[str, ...] = ("KG_SUMMARY_BACKEND",),
    label: str = "KG-summary",
) -> str:
    """Pick the best backend on first call, cache for subsequent prompts.

    Selection order (v0.2.23 C10):
      1. Forced-backend env override (``env_keys``, first non-empty; if valid).
      2. claude CLI on PATH + smoke-test passes.
      3. Ollama reachable at OLLAMA_URL.
      4. OpenAI (requires OPENAI_API_KEY AND consent — either via the
         launcher Preferences app_state key, or via the ``force_api``
         operator flag, i.e. the caller's --force-api).
      5. Anthropic API direct (legacy fallback, uses ANTHROPIC_API_KEY).
      6. Skip — log a friendly line; the caller exits 0.

    When the OpenAI path is reachable in principle (key present) but
    consent has not been granted, this returns "skip" AND logs
    a clear "set kg_summary_openai_consent=true in Preferences or use
    --force-api" message — so the user knows there IS a backend
    available, it's just gated.
    """
    if "choice" in _BACKEND_CACHE:
        return _BACKEND_CACHE["choice"]

    forced = _forced_backend(env_keys)
    if forced in VALID_BACKENDS:
        # Consent gate still applies to forced=openai (defense-in-depth:
        # an env var alone shouldn't bypass user consent; --force-api
        # is the explicit operator override).
        if forced == "openai" and not force_api and not openai_consent_granted():
            _log(
                f"  {label}: {env_keys[0]}=openai but consent not "
                "granted. Set kg_summary_openai_consent=true in launcher "
                "Preferences → KG Summaries, or pass --force-api. "
                "Skipping for this run."
            )
            _BACKEND_CACHE["choice"] = "skip"
            return "skip"
        _BACKEND_CACHE["choice"] = forced
        _log(f"  {label} backend: {forced} (forced via env)")
        return forced

    if cli_available():
        _BACKEND_CACHE["choice"] = "cli"
        _log(f"  {label} backend: cli (claude on PATH, smoke-test OK)")
        return "cli"
    if ollama_available():
        _BACKEND_CACHE["choice"] = "ollama"
        _log(f"  {label} backend: ollama ({OLLAMA_DEFAULT_MODEL})")
        return "ollama"
    # OpenAI tier: key present AND (consent granted OR --force-api).
    if openai_available():
        if force_api or openai_consent_granted():
            _BACKEND_CACHE["choice"] = "openai"
            _log(
                f"  {label} backend: openai ({_openai_model()}) — "
                f"costs apply per summary"
            )
            return "openai"
        else:
            _log(
                f"  {label}: OPENAI_API_KEY is set but consent not "
                "granted. Set kg_summary_openai_consent=true in launcher "
                "Preferences → KG Summaries to enable, or pass "
                "--force-api. Falling through to anthropic / skip."
            )
    if api_available():
        _BACKEND_CACHE["choice"] = "api"
        _log(f"  {label} backend: api (ANTHROPIC_API_KEY) — costs apply")
        return "api"

    _BACKEND_CACHE["choice"] = "skip"
    _log(
        f"  {label}: no backend available (no claude CLI, no Ollama at "
        f"{OLLAMA_URL}, no OPENAI_API_KEY, no ANTHROPIC_API_KEY). Skipping."
    )
    return "skip"


def call_llm(
    prompt: str,
    *,
    force_api: bool = False,
    env_keys: tuple[str, ...] = ("KG_SUMMARY_BACKEND",),
    label: str = "KG-summary",
) -> str:
    backend = select_backend(force_api=force_api, env_keys=env_keys, label=label)
    if backend == "cli":
        return call_cli(prompt)
    if backend == "ollama":
        return call_ollama(prompt)
    if backend == "openai":
        return call_openai(prompt)
    if backend == "api":
        return call_api(prompt)
    raise RuntimeError("no backend available")
