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
  VCO_SUMMARY_BREAKER       → "off"/"0"/"false"/"no" disables the usage-limit
                              circuit breaker entirely (kill switch)
  VCO_SUMMARY_BREAKER_COOLDOWN
                            → seconds a tier stays demoted after a usage-limit
                              (rate-limit) failure (default: 900)
  VCO_SUMMARY_BREAKER_AUTH_COOLDOWN
                            → seconds a tier stays demoted after an AUTH
                              failure (default: 120 — the user is the fix,
                              and expects the next node to retry)
  VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN
                            → seconds after a sustained capacity failure
                              (529/503/timeout) (default: 60)
  VCO_SUMMARY_BREAKER_CAPACITY_STRIKES
                            → consecutive capacity failures before a tier is
                              demoted (default: 3; one transient 529 must not
                              cost the best tier)

Caller integration contract:
  * ``set_logger(fn)`` — route this module's log lines through the caller's
    logger (the KG generator appends to .claude/logs/; default: print).
  * ``reset_backend_cache()`` — clear the per-process backend choice + CLI
    smoke-test cache. The KG generator calls it at import so re-imported
    script modules (the test-isolation pattern) start fresh.
  * ``select_backend(force_api=..., env_keys=..., label=...)`` /
    ``call_llm(prompt, ...)`` — the ladder. ``force_api`` is the operator
    --force-api override for the OpenAI consent gate.

v0.2.92 WP-Q — the ladder gained the TRIGGER that moves DOWN it, and a
validity gate on what it returns. The tier order above is unchanged and
deliberate; what was missing was any way to LEAVE a tier that has stopped
working mid-run.

1. Usage-limit circuit breaker. ``cli_available()`` only ever cached the
   result of the INITIAL smoke test, so an account that hit its cap in the
   middle of a backfill kept spawning a real ``claude -p`` subprocess for
   every remaining node (field report: 426 headless sessions in one run).
   Request-time failures are now CLASSIFIED (`classify_backend_failure`)
   and a tier that cannot serve is LATCHED open for a cooldown:
     * ``rate_limit`` / ``auth`` — retrying the same tier cannot succeed,
       so ONE occurrence demotes it. Their COOLDOWNS differ because their
       fixes do: a rate limit ends on a clock nobody here controls
       (``VCO_SUMMARY_BREAKER_COOLDOWN``, default 900 s), while an auth
       failure ends when the USER re-authenticates and then expects the
       next node to use the tier (``VCO_SUMMARY_BREAKER_AUTH_COOLDOWN``,
       default 120 s).
     * ``capacity`` (529 / 503 / timeout) — TRANSIENT, it recovers on its
       own, so it takes ``VCO_SUMMARY_BREAKER_CAPACITY_STRIKES`` (default 3)
       CONSECUTIVE occurrences and gets a short cooldown
       (``VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN``, default 60 s). A
       permanent latch on a transient 529 would be its own bug.
     * ``other`` — never trips. A breaker that demotes on every unknown
       error falls back when it should retry.
   The latch is written to ``<vct-state-dir>/summary_backend_breaker.json``
   because the KG generator runs as ONE PROCESS PER NODE — an in-process
   latch alone would be re-armed 117 times over a 117-node pass. The
   consecutive-``capacity`` STRIKE COUNT lives in that same record for
   exactly the same reason (v0.2.92 BLOCKER-2): held in memory it was
   re-zeroed by every per-node process, so the third consecutive 529 never
   arrived and the capacity arm — the most common transient reason — could
   not fire at all. The
   account cap is machine-wide, so the state belongs beside the machine's
   other VCT state, never in a project tree. It EXPIRES rather than
   persisting: an endpoint that recovers must be usable again without the
   user deleting a file. Kill switch: ``VCO_SUMMARY_BREAKER=off``.
   Observability: every demotion, every skipped tier and every fallback
   prints a line naming the tier and the reason, and the sidecar entry's
   ``backend`` field records the tier that actually answered — a summary
   produced by Ollama is never reported as one produced by the CLI.
   A FORCED backend (``KG_SUMMARY_BACKEND=...``) is never auto-demoted:
   the operator asked for that tier by name, so the call fails loudly
   instead of silently substituting a different model.
2. Non-answer rejection. ``is_non_answer()`` recognises a model NON-ANSWER
   ("Ready. What do you need summarized?", a refusal, an empty reply) so it
   is never cached AS IF it were a summary. Field measurement: 43% of one
   project's stored summaries were non-answers, and because caching is
   content-hash idempotent, the hash gate then FROZE them permanently. The
   gate itself is correct and stays — the callers additionally treat a row
   that never held a valid summary as unsatisfied, so poisoned rows
   regenerate on the next ordinary run with no user action.
3. Root cause of those non-answers: ``call_cli`` passed the prompt as an
   ARGV element. On Windows ``claude`` is an npm ``.cmd`` shim, which
   CreateProcess runs through ``cmd.exe``, which re-parses the arguments —
   an embedded newline ends the command and every shell metacharacter in
   the node body is interpreted (the BatBadBut class). The prompt is
   ``SYSTEM_PROMPT + "\n\n" + body``, so what survived was the system
   prompt alone and the model answered "Ready. What do you need
   summarized?". The prompt now goes over STDIN, which no shell re-parses.
"""

from __future__ import annotations

import json
import os
import shutil
import time
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

# ──────────────────────────────────────────────────────────────────────
# v0.2.92 WP-Q — usage-limit circuit breaker
# ──────────────────────────────────────────────────────────────────────
# Env knobs. Every one of these has a reader below and a test proving that
# setting it changes observable behaviour (R24).
ENV_BREAKER_ENABLED = "VCO_SUMMARY_BREAKER"                 # "off"/"0"/"false" disables
ENV_BREAKER_COOLDOWN = "VCO_SUMMARY_BREAKER_COOLDOWN"       # seconds, rate_limit
ENV_BREAKER_AUTH_COOLDOWN = "VCO_SUMMARY_BREAKER_AUTH_COOLDOWN"
ENV_BREAKER_CAPACITY_COOLDOWN = "VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN"
ENV_BREAKER_CAPACITY_STRIKES = "VCO_SUMMARY_BREAKER_CAPACITY_STRIKES"

DEFAULT_BREAKER_COOLDOWN_S = 900.0
#: v0.2.92 WP-Q2 (coordinator ruling). `auth` still trips on the FIRST
#: occurrence — retrying the same tier with the same bad credential cannot
#: succeed — but it does NOT inherit the rate-limit cooldown, because the
#: two differ in who can end them. A rate limit is a clock the user cannot
#: move, so 900 s costs nothing. An auth failure is a clock the USER IS:
#: they re-run `claude login`, export the key, and expect the very next
#: node to use the tier again. A 15-minute latch after the fix is applied
#: reads as "the breaker is broken".
DEFAULT_AUTH_COOLDOWN_S = 120.0
DEFAULT_CAPACITY_COOLDOWN_S = 60.0
DEFAULT_CAPACITY_STRIKES = 3

#: Cross-process latch file, beside the machine's other VCT state. The
#: account cap this records is machine-wide, and the KG generator runs one
#: process per node — an in-process dict alone would never survive to the
#: next node.
BREAKER_FILENAME = "summary_backend_breaker.json"

REASON_RATE_LIMIT = "rate_limit"
REASON_CAPACITY = "capacity"
REASON_AUTH = "auth"
REASON_OTHER = "other"

#: Reasons that demote a tier. ``other`` is deliberately absent.
TRIPPING_REASONS = (REASON_RATE_LIMIT, REASON_AUTH, REASON_CAPACITY)

#: An EXIT-0 reply longer than this is never re-read as an error notice — a
#: genuine summary that happens to discuss rate limiting is long and starts
#: with content, while a notice printed in place of an answer is short.
ERROR_TEXT_MAX_CHARS = 400

#: An EXIT-0 reply must ALSO *begin* like a notice before it is re-read as
#: one. Length alone is not enough: "Retries on 429 responses." and "Times
#: out after 500 ms." are legitimate short summaries that a bare substring
#: scan would misread as an outage — demoting a healthy tier on the strength
#: of the content it was asked to summarise.
#: Each entry is a phrase a NOTICE opens with but a summary does not. Bare
#: words are deliberately absent: "Error handling: ...", "Rate limit
#: handling for the hub client.", "Unauthorized callers get a typed
#: refusal." are all legitimate summary openers.
_NOTICE_PREFIXES = (
    "claude ai usage limit",
    "usage limit reached", "usage limit exceeded",
    "rate limit exceeded", "rate limit reached", "rate_limit_error",
    "ratelimit", "you have exceeded", "too many requests",
    "quota exceeded", "insufficient_quota",
    "credit balance is too low", "out of credits",
    "invalid api key", "invalid x-api-key", "invalid_api_key",
    "authentication_error", "authentication failed",
    "please run /login", "not logged in",
    "overloaded_error", "api error", "api_error", "service unavailable",
    "error:",
)

_RATE_LIMIT_STATUSES = frozenset({429})
_AUTH_STATUSES = frozenset({401, 403})
_CAPACITY_STATUSES = frozenset({500, 502, 503, 504, 529})

# Signals are matched case-insensitively anywhere in the failure text. They
# are the ONLY thing recorded about a failure — never the raw text, which on
# these paths can quote user content.
_AUTH_SIGNALS = (
    "unauthorized", "forbidden", "authentication_error", "authentication failed",
    "invalid api key", "invalid x-api-key", "invalid_api_key", "invalid bearer",
    "please run /login", "not logged in", "oauth token", "token has expired",
    "permission_error", "401", "403",
)
_RATE_LIMIT_SIGNALS = (
    "usage limit", "usage_limit", "rate limit", "rate_limit", "ratelimit",
    "too many requests", "quota", "insufficient_quota", "out of credits",
    "credit balance is too low", "limit reached", "limit will reset", "429",
)
_CAPACITY_SIGNALS = (
    "overloaded", "service unavailable", "temporarily unavailable",
    "bad gateway", "gateway timeout", "timed out", "timeout",
    "connection refused", "connection reset", "500", "502", "503", "504", "529",
)

#: In-process latch. Merged with the file record (newest expiry wins) so a
#: sibling process that tripped a tier is honoured immediately. The record
#: also carries the consecutive-``capacity`` strike count (``strikes``) —
#: ONE store, because the count has the same cross-process lifetime as the
#: latch it leads to (v0.2.92 BLOCKER-2: as an in-process dict it was reset
#: by every per-node process and the third strike never arrived).
_BREAKER_MEM: dict[str, dict] = {}


class BackendUnavailable(RuntimeError):
    """A TIER-level failure: this backend cannot serve right now.

    Subclasses ``RuntimeError`` so every existing ``except RuntimeError`` /
    ``except Exception`` caller keeps its behaviour.
    """

    def __init__(self, tier: str, reason: str, signal: str, message: str,
                 *, already_open: bool = False) -> None:
        super().__init__(message)
        self.tier = tier
        self.reason = reason
        self.signal = signal
        #: True when the tier was ALREADY latched (so the caller must not
        #: extend the cooldown by re-tripping on its own guard).
        self.already_open = already_open


class NonAnswerResponse(RuntimeError):
    """The backend answered, but with a NON-ANSWER rather than a summary.

    A CONTENT condition, not a tier condition: it never trips the breaker
    (see ``classify_backend_failure``'s ``other`` arm for why). The caller
    must not cache the text.
    """


def _now() -> float:
    """Wall clock, as one seam so tests can advance it."""
    return time.time()

# Caller-injectable logger (the KG generator's log() writes to a per-project
# log file; the code generator injects its own). Default: plain print.
_log = print


def set_logger(fn) -> None:
    """Route this module's log lines through *fn* (signature: (str) -> None)."""
    global _log
    _log = fn


def reset_backend_cache() -> None:
    """Clear the per-process backend choice + CLI smoke-test probe cache.

    Also clears the IN-PROCESS breaker latch, so a freshly (re-)imported
    script module starts with no cached demotion. The PERSISTED latch is
    deliberately left alone: it is cross-process state (the KG generator
    runs one process per node), and wiping it on every import would defeat
    the mechanism entirely. ``reset_breaker(persisted=True)`` is the
    explicit "forget the latch on disk too" call.
    """
    _BACKEND_CACHE.clear()
    _BREAKER_MEM.clear()


def reset_breaker(*, persisted: bool = False) -> None:
    """Clear the breaker latch (and its strike counts). ``persisted=True``
    also removes the file."""
    _BREAKER_MEM.clear()
    if persisted:
        try:
            _breaker_path().unlink()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────
# Failure classification
# ──────────────────────────────────────────────────────────────────────
def classify_backend_failure(
    *, status: "int | None" = None, text: str = "",
) -> "tuple[str, str]":
    """Classify a backend failure as ``(reason, signal)``.

    The four reasons are kept DISTINCT because they need opposite
    responses, and collapsing them is how a circuit breaker becomes a bug:

    * ``rate_limit`` — the account's cap is spent. Retrying this tier
      cannot succeed until the window rolls over → demote on the FIRST
      occurrence, long cooldown.
    * ``auth`` — the credential is wrong or expired. Retrying cannot
      succeed at all → demote on the first occurrence. Still time-boxed,
      never permanent: the user may re-login while a pass is running.
    * ``capacity`` — 529 / 503 / timeout. The endpoint is momentarily
      overloaded and RECOVERS BY ITSELF, so a single one must not demote
      the tier (this session saw a 529 and a healthy endpoint minutes
      later). Demotion takes N consecutive strikes and a short cooldown.
    * ``other`` — anything not positively identified. NEVER trips. A
      breaker that fires on every unknown error falls back when it should
      retry, which is the opposite defect from the one it was built for.

    ``signal`` is the matched keyword or status code — never raw failure
    text, which on these paths can quote node content.
    """
    if status is not None:
        if status in _AUTH_STATUSES:
            return REASON_AUTH, str(status)
        if status in _RATE_LIMIT_STATUSES:
            return REASON_RATE_LIMIT, str(status)
        if status in _CAPACITY_STATUSES:
            return REASON_CAPACITY, str(status)
    haystack = (text or "").lower()
    if not haystack:
        return REASON_OTHER, ""
    for signal in _AUTH_SIGNALS:
        if signal in haystack:
            return REASON_AUTH, signal
    for signal in _RATE_LIMIT_SIGNALS:
        if signal in haystack:
            return REASON_RATE_LIMIT, signal
    for signal in _CAPACITY_SIGNALS:
        if signal in haystack:
            return REASON_CAPACITY, signal
    return REASON_OTHER, ""


# ──────────────────────────────────────────────────────────────────────
# Breaker state (in-process latch + cross-process file)
# ──────────────────────────────────────────────────────────────────────
def _vct_state_dir() -> Path:
    """The machine's VCT state dir. Mirrors ``vco_lib.paths.vct_root_dir``.

    Inlined rather than imported so this module stays usable from a
    per-project ``.claude/scripts/`` install that has no ``vco_lib`` on
    ``PYTHONPATH`` (the same reason ``_read_app_state_value`` inlines it —
    both now share this ONE resolver rather than two copies).
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"


def _breaker_path() -> Path:
    return _vct_state_dir() / BREAKER_FILENAME


def breaker_enabled() -> bool:
    """False when ``VCO_SUMMARY_BREAKER`` is off/0/false/no (kill switch)."""
    return os.getenv(ENV_BREAKER_ENABLED, "").strip().lower() not in {
        "0", "off", "false", "no",
    }


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def cooldown_for(reason: str) -> float:
    """Seconds a tier stays demoted after a *reason* failure.

    Three durations, because the three reasons differ in HOW they end:
    ``capacity`` recovers by itself (short), ``auth`` is ended by the user
    (short — see ``DEFAULT_AUTH_COOLDOWN_S``), ``rate_limit`` is ended by a
    clock nobody here controls (long).
    """
    if reason == REASON_CAPACITY:
        return _float_env(ENV_BREAKER_CAPACITY_COOLDOWN, DEFAULT_CAPACITY_COOLDOWN_S)
    if reason == REASON_AUTH:
        return _float_env(ENV_BREAKER_AUTH_COOLDOWN, DEFAULT_AUTH_COOLDOWN_S)
    return _float_env(ENV_BREAKER_COOLDOWN, DEFAULT_BREAKER_COOLDOWN_S)


def capacity_strikes() -> int:
    """Consecutive ``capacity`` failures needed before a tier is demoted."""
    return _int_env(ENV_BREAKER_CAPACITY_STRIKES, DEFAULT_CAPACITY_STRIKES)


def _load_breaker_file() -> dict:
    """The persisted latch, or ``{}`` — never raises."""
    try:
        raw = _breaker_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_breaker_file(data: dict) -> None:
    """Persist the latch (tmp + replace). Soft-fails: a read-only HOME must
    not break a summary run — the in-process latch still holds.

    DOCUMENTED COPY of ``vco_lib.atomic.atomic_write_text`` (v0.2.92
    duplication-merge, PLAN-EXTENSION §3.13). This module is imported by
    ``generate-kg-summary.py`` running as ``.claude/scripts/`` inside USER
    projects, where no orchestrator root has been resolved and ``vco_lib``
    is not on ``sys.path`` — it is dependency-light on purpose (stdlib
    only, see the module header). A soft ``try: from vco_lib.atomic …
    except ImportError: <inline>`` would be the quiet-degrade shape the
    one-home rule forbids, so the copy is declared instead and pinned by
    ``tests/test_v0292_atomic_one_home.py``. If this script ever gains the
    shared ``_resolve_orchestrator_root`` region, migrate this onto the home.
    """
    path = _breaker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def _record_until(record: object) -> float:
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get("until") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _record_strikes(record: object) -> int:
    """The consecutive-``capacity`` strike count carried by *record*."""
    if not isinstance(record, dict):
        return 0
    try:
        return int(record.get("strikes") or 0)
    except (TypeError, ValueError):
        return 0


def breaker_state(tier: str) -> "dict | None":
    """The OPEN breaker record for *tier*, or ``None`` when it is usable.

    Merges the in-process latch with the persisted one and honours the
    later expiry, so a sibling process's trip is seen immediately and a
    stale in-process record cannot mask a fresher one.
    """
    if not breaker_enabled():
        return None
    candidates = [
        record for record in (_BREAKER_MEM.get(tier), _load_breaker_file().get(tier))
        if isinstance(record, dict)
    ]
    if not candidates:
        return None
    record = max(candidates, key=_record_until)
    until = _record_until(record)
    now = _now()
    if until <= now:
        return None
    out = dict(record)
    out["remaining"] = max(0, int(until - now))
    return out


def trip_backend(tier: str, reason: str, signal: str = "") -> bool:
    """Latch *tier* open. Returns True when it is now demoted.

    ``capacity`` needs ``capacity_strikes()`` CONSECUTIVE occurrences — one
    transient 529 must not cost the user the best tier for 15 minutes.

    The strike count is kept in the SAME record as the latch, on disk,
    for the same reason the latch is (v0.2.92 BLOCKER-2): the KG generator
    runs ONE PROCESS PER NODE, so a strike counter that lived only in this
    process was re-zeroed before the second 529 ever arrived and the
    capacity arm could never fire. A below-threshold record carries
    ``until: 0``, so it counts strikes without demoting anything.
    """
    if not breaker_enabled() or reason not in TRIPPING_REASONS:
        return False
    now = _now()
    data = _load_breaker_file()
    if reason == REASON_CAPACITY:
        if max(_record_until(_BREAKER_MEM.get(tier)),
               _record_until(data.get(tier))) > now:
            # Already latched. The ladder never re-trips an open tier (it
            # raises ``already_open`` instead), but a direct caller could —
            # and a below-threshold strike record, which carries
            # ``until: 0``, would then REPLACE the live latch and re-open
            # the tier. Count nothing, clear nothing.
            return False
        strikes = max(
            _record_strikes(_BREAKER_MEM.get(tier)),
            _record_strikes(data.get(tier)),
        ) + 1
        if strikes < capacity_strikes():
            pending = {
                "reason": reason,
                "signal": str(signal or "")[:64],
                "strikes": strikes,
                "until": 0.0,        # counted, NOT latched
            }
            # Both stores, so an unwritable state dir still counts strikes
            # within this process (same soft-fail contract as the latch).
            _BREAKER_MEM[tier] = pending
            data[tier] = pending
            _save_breaker_file(data)
            return False
    cooldown = cooldown_for(reason)
    if cooldown <= 0:
        return False
    record = {
        "reason": reason,
        "signal": str(signal or "")[:64],
        "tripped_at": now,
        "until": now + cooldown,
    }   # no ``strikes``: the trip CONSUMES them, so the next demotion
        # needs a fresh run of consecutive failures.
    _BREAKER_MEM[tier] = record
    data[tier] = record
    _save_breaker_file(data)
    return True


def clear_backend_trip(tier: str) -> None:
    """Forget *tier*'s record — it just served a request.

    Dropping the record drops the latch AND the strike count with it (they
    are one record), which is what makes capacity strikes CONSECUTIVE.
    """
    _BREAKER_MEM.pop(tier, None)
    if not _breaker_path().exists():
        return
    data = _load_breaker_file()
    if tier in data:
        del data[tier]
        _save_breaker_file(data)


def _tier_selectable(tier: str, label: str) -> bool:
    """False + one explaining log line when *tier*'s breaker is open.

    Called BEFORE the availability probe so a demoted CLI never pays the
    20 s smoke test again (117 nodes = 117 processes = 117 probes).
    """
    record = breaker_state(tier)
    if record is None:
        return True
    signal = str(record.get("signal") or "")
    detail = record.get("reason")
    if signal:
        detail = f"{detail}/{signal}"
    _log(
        f"  {label}: backend '{tier}' unavailable — breaker open "
        f"({detail}, {record.get('remaining')}s remaining)"
    )
    return False


# ──────────────────────────────────────────────────────────────────────
# Response validity (v0.2.92 WP-Q defect 2)
# ──────────────────────────────────────────────────────────────────────
#: A reply shorter than this cannot be a summary of anything.
NON_ANSWER_MIN_CHARS = 8

#: Prefix-anchored, deliberately. A real technical summary may MENTION a
#: refusal phrase; it does not START with one. The anchoring is chosen for
#: the asymmetry of the two errors: a false NEGATIVE poisons a cache row
#: that the content-hash gate then freezes forever (invisible), while a
#: false POSITIVE refuses the node loudly (visible in the log, nothing
#: cached).
#:
#: Be precise about what a false positive actually costs, because the
#: earlier wording here ("one regeneration") was NOT true. ``call_llm``
#: raises ``NonAnswerResponse`` without demoting the tier and without
#: descending — deliberately, since a CONTENT condition must not demote a
#: healthy tier, and answering it with a weaker model would be a silent
#: substitution. So a legitimate summary that opens with one of these
#: prefixes (e.g. "Error: ..." as a heading) or is shorter than
#: ``NON_ANSWER_MIN_CHARS`` (e.g. "Caching") is refused on EVERY run: the
#: caller exits non-zero and the node stays unsummarised until its text
#: changes. Bounded, not looping — but permanent for that text, not a
#: retry. Widen these lists only with that cost in mind.
NON_ANSWER_PREFIXES = (
    "ready. what", "ready, what", "ready! what", "ready — what", "ready. how",
    "i cannot", "i can't", "i can not", "i am unable", "i'm unable",
    "i am not able", "i'm not able", "i do not have", "i don't have",
    "i don't see any", "i do not see any", "i notice you",
    "i'd be happy to", "i would be happy to",
    "sorry", "i'm sorry", "i am sorry", "as an ai",
    "how can i help", "how may i help", "what would you like",
    "what do you need", "please provide", "please share", "please paste",
    "no content", "there is no content",
    "you haven't provided", "you have not provided",
    "error:",
)
NON_ANSWER_EXACT = frozenset({"ok", "n/a", "na", "none", "null", "todo", "tbd"})


def is_non_answer(text: "str | None") -> bool:
    """True when *text* is a model NON-ANSWER rather than a summary.

    Recognises the empty reply, the too-short token, and the canned
    "Ready. What do you need summarized?" / refusal shapes that a field
    scan found in 43% of one project's stored summaries.
    """
    if text is None:
        return True
    stripped = text.strip().strip("`\"'*# \t")
    if not stripped:
        return True
    normalized = " ".join(stripped.split()).lower()
    if normalized in NON_ANSWER_EXACT:
        return True
    if len(normalized) < NON_ANSWER_MIN_CHARS:
        return True
    return normalized.startswith(NON_ANSWER_PREFIXES)


def _classify_exit_zero_text(text: str) -> "tuple[str, str]":
    """Classify an EXIT-0 reply that is really an error notice.

    Some backends print a usage-limit / auth notice to stdout and exit 0;
    cached verbatim that becomes a poisoned row AND hides the outage.

    TWO independent conditions, both required, because either alone
    misfires: the reply must be SHORT (``ERROR_TEXT_MAX_CHARS``) *and*
    BEGIN like a notice (``_NOTICE_PREFIXES``). A summary is content the
    model was asked to write ABOUT something; it does not open with an
    outage notice. Without the prefix condition, "Retries on 429
    responses." would demote a perfectly healthy tier.
    """
    if len(text) >= ERROR_TEXT_MAX_CHARS:
        return REASON_OTHER, ""
    normalized = " ".join(text.strip().split()).lower()
    if not normalized.startswith(_NOTICE_PREFIXES):
        return REASON_OTHER, ""
    return classify_backend_failure(text=normalized)


def _diagnose(exc: BaseException) -> "tuple[str, str]":
    """``(reason, signal)`` for any exception a backend call can raise."""
    if isinstance(exc, BackendUnavailable):
        return exc.reason, exc.signal
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 — body is best-effort context only
            body = ""
        return classify_backend_failure(
            status=getattr(exc, "code", None),
            text=f"{getattr(exc, 'reason', '') or ''} {body}",
        )
    if isinstance(exc, (urllib.error.URLError, OSError)):
        # Includes socket.timeout / ConnectionRefusedError: the endpoint is
        # not answering. Transient by nature → strike-gated capacity.
        return REASON_CAPACITY, "transport"
    return classify_backend_failure(text=str(exc))


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
    claude_path = shutil.which("claude")
    if claude_path is None:
        _BACKEND_CACHE["cli_probe_ok"] = "no"
        return False
    # Smoke-test: short prompt, tight timeout. We don't care about the
    # output content — just that the CLI returns non-empty stdout and
    # exit 0 (i.e. authenticated and reachable). Failure modes we want
    # to catch: auth error (returns stderr, exit non-zero), network
    # offline, model not available for the account, expired token.
    import subprocess as _sub
    try:
        # --no-session-persistence: without it every probe call persists a
        # transcript into the user's session list (~35 "say ok" sessions on
        # one install). Verified on Claude CLI 2.1.258+; the shipped
        # post-git-commit-kg-sync.{sh,ps1} hooks already use this flag.
        result = _sub.run(
            [claude_path, "-p", "say ok", "--model", "haiku", "--max-turns", "1",
             "--no-session-persistence"],
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
    """Run one headless ``claude -p`` turn. Prompt goes over STDIN.

    v0.2.92 WP-Q — the prompt used to be an ARGV element. On Windows
    ``claude`` is an npm ``.cmd`` shim; CreateProcess runs a batch file
    through ``cmd.exe``, which RE-PARSES the arguments. The prompt is
    ``SYSTEM_PROMPT + "\n\n" + body``, so the embedded newline ended the
    command and only the system prompt reached the model — which then
    answered "Ready. What do you need summarized?" and got cached as a
    summary. Every shell metacharacter in the node body (``&``, ``|``,
    ``^``, ``%``, ``>``) was likewise interpreted, which is the BatBadBut
    command-injection shape as well as a correctness bug.

    ``claude -p`` with no prompt argument reads the prompt from stdin
    (``--input-format text`` is the default for ``--print``), and stdin is
    a pipe no shell re-parses. It also removes the command-line length
    limit, which the 8 KB prompts here were approaching on ``cmd.exe``.
    """
    import subprocess

    # Resolve the absolute path so subprocess honors PATHEXT on Windows
    # (where `claude` may ship as `claude.cmd` / `claude.bat` via npm).
    # cli_available() already returned True via shutil.which, but Python's
    # subprocess.run won't apply PATHEXT to bare names on Windows.
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise RuntimeError("claude CLI not found on PATH at call time")

    full_prompt = SYSTEM_PROMPT + "\n\n" + prompt
    try:
        # --no-session-persistence: without it every one-shot summary call
        # persists a transcript into the user's session list (943 machine-
        # generated transcripts / 1.05 GB measured on one install). Verified
        # on Claude CLI 2.1.258+; post-git-commit-kg-sync.{sh,ps1} already
        # use this flag.
        result = subprocess.run(
            [claude_path, "-p", "--model", "haiku", "--max-turns", "1",
             "--no-session-persistence"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendUnavailable(
            "cli", REASON_CAPACITY, "timeout",
            f"claude CLI timed out after {TIMEOUT}s",
        ) from exc
    if result.returncode != 0:
        reason, signal = classify_backend_failure(
            text=f"{result.stderr}\n{result.stdout}",
        )
        raise BackendUnavailable(
            "cli", reason, signal,
            f"claude CLI failed: {result.stderr[:200]}",
        )
    out = result.stdout.strip()
    # Exit 0 but the "answer" is a usage-limit / auth notice: a tier
    # failure wearing a success exit code. Caching it would poison the
    # sidecar AND hide the outage.
    reason, signal = _classify_exit_zero_text(out)
    if reason != REASON_OTHER:
        raise BackendUnavailable(
            "cli", reason, signal,
            f"claude CLI returned a backend notice instead of an answer "
            f"({reason})",
        )
    return out


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
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
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

    Path resolution goes through `_vct_state_dir()`, which mirrors
    `vco_lib.paths.vct_root_dir` (`$VCT_STATE_DIR`, else `~/.vct`). That
    resolver is inlined rather than imported so this module stays usable
    from a per-project `.claude/scripts/` install with no `vco_lib` on
    PYTHONPATH — and it is ONE resolver shared with the breaker file, not
    a second copy of the same two lines.
    """
    db_path = _vct_state_dir() / "launcher.db"
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

    A tier whose circuit breaker is OPEN is skipped before its
    availability probe runs (v0.2.92 WP-Q) — that is what stops a
    rate-limited CLI paying the 20 s smoke test once per node. A cached
    choice that has since been demoted is discarded rather than returned.

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
    cached = _BACKEND_CACHE.get("choice")
    if cached is not None:
        # A tier demoted since we cached it must not be handed back — the
        # cache is a cost optimisation, not a promise that the tier still
        # works. "skip" has no breaker.
        if cached == "skip" or breaker_state(cached) is None:
            return cached
        _BACKEND_CACHE.pop("choice", None)

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

    # `_tier_selectable` is checked BEFORE each probe so a demoted CLI
    # never pays the 20 s smoke test again — the KG generator runs one
    # process per node, so that probe would otherwise fire once per node.
    if _tier_selectable("cli", label) and cli_available():
        _BACKEND_CACHE["choice"] = "cli"
        _log(f"  {label} backend: cli (claude on PATH, smoke-test OK)")
        return "cli"
    if _tier_selectable("ollama", label) and ollama_available():
        _BACKEND_CACHE["choice"] = "ollama"
        _log(f"  {label} backend: ollama ({OLLAMA_DEFAULT_MODEL})")
        return "ollama"
    # OpenAI tier: key present AND (consent granted OR --force-api).
    if _tier_selectable("openai", label) and openai_available():
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
    if _tier_selectable("api", label) and api_available():
        _BACKEND_CACHE["choice"] = "api"
        _log(f"  {label} backend: api (ANTHROPIC_API_KEY) — costs apply")
        return "api"

    _BACKEND_CACHE["choice"] = "skip"
    # The launcher matches the substring "no backend available" on stdout
    # (kg_summary.rs NO_BACKEND_MARKER) — both branches keep it. The second
    # branch exists because "install the claude CLI" is the WRONG advice for
    # a user whose CLI is installed and merely rate-limited.
    cooling = [
        tier for tier in ("cli", "ollama", "openai", "api")
        if breaker_state(tier) is not None
    ]
    if cooling:
        _log(
            f"  {label}: no backend available — {', '.join(cooling)} "
            f"cooling down after a usage-limit / capacity failure. "
            f"Nothing to install; retry after the cooldown. Skipping."
        )
    else:
        _log(
            f"  {label}: no backend available (no claude CLI, no Ollama at "
            f"{OLLAMA_URL}, no OPENAI_API_KEY, no ANTHROPIC_API_KEY). Skipping."
        )
    return "skip"


def _dispatch(backend: str, prompt: str) -> str:
    """Call one tier. Refuses a tier whose breaker is open."""
    record = breaker_state(backend)
    if record is not None:
        raise BackendUnavailable(
            backend,
            str(record.get("reason") or REASON_OTHER),
            str(record.get("signal") or ""),
            f"backend '{backend}' is cooling down "
            f"({record.get('reason')}, {record.get('remaining')}s remaining)",
            already_open=True,
        )
    if backend == "cli":
        return call_cli(prompt)
    if backend == "ollama":
        return call_ollama(prompt)
    if backend == "openai":
        return call_openai(prompt)
    if backend == "api":
        return call_api(prompt)
    raise RuntimeError("no backend available")


def call_llm(
    prompt: str,
    *,
    force_api: bool = False,
    env_keys: tuple[str, ...] = ("KG_SUMMARY_BACKEND",),
    label: str = "KG-summary",
) -> str:
    """Summarise *prompt*, descending the ladder when a tier stops serving.

    A tier-level failure (usage limit, auth, sustained capacity) DEMOTES
    the tier and the next one is tried for THIS prompt, so the node still
    gets a summary. Anything unclassified propagates unchanged — the
    breaker is not a retry loop.

    A FORCED backend is never auto-demoted: the operator named that tier,
    so substituting another model silently would be exactly the kind of
    unannounced behaviour change this release exists to remove. The
    failure is raised (and logged) instead.
    """
    forced = _forced_backend(env_keys) in VALID_BACKENDS
    tried: set = set()
    while True:
        backend = select_backend(force_api=force_api, env_keys=env_keys,
                                 label=label)
        if backend == "skip" or backend in tried:
            raise RuntimeError("no backend available")
        tried.add(backend)
        try:
            text = _dispatch(backend, prompt)
        except Exception as exc:  # noqa: BLE001 — classified below, then re-raised
            reason, signal = _diagnose(exc)
            already_open = (
                isinstance(exc, BackendUnavailable) and exc.already_open
            )
            demoted = already_open or trip_backend(backend, reason, signal)
            if demoted and not already_open:
                detail = f"{reason}/{signal}" if signal else reason
                _log(
                    f"  {label}: backend '{backend}' demoted — {detail}; "
                    f"cooling down {int(cooldown_for(reason))}s"
                )
            if forced or not demoted:
                raise
            _log(f"  {label}: falling through to the next backend tier")
            continue
        clear_backend_trip(backend)
        if is_non_answer(text):
            # CONTENT condition, not a tier condition: no demotion (see
            # classify_backend_failure). The caller must not cache it.
            # The message reports the reply's SHAPE (length), never the
            # reply: the callers LOG this line (generate-kg-summary.py
            # writes it to the project log) and model output on this path
            # quotes node content — the same rule the breaker's ``signal``
            # field follows.
            raise NonAnswerResponse(
                f"{label}: backend '{backend}' returned a non-answer "
                f"({len((text or '').strip())} chars) — not cached"
            )
        return text
