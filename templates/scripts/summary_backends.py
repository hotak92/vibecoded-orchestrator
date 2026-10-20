#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Shared LLM summary backend ladder (v0.2.73 M2 — extracted, one home).

Extracted VERBATIM from ``generate-kg-summary.py`` so the code-summary
generator (``generate-code-summary.py``) does not clone ~450 lines of ladder
logic (the M-2 "one concern, one home" extraction). Both generators are thin
callers of this module; ``generate-kg-summary.py``'s behaviour is unchanged.

Five-tier model selection (in order) — the ladder is BY DESIGN; nothing here
is a defect finding. v0.2.103 (8b, owner order 2026-10-14) put the two cloud
vendor tiers BETWEEN the CLI and local Ollama, so a machine with a vendor
subscription never loads a local generation model onto a GPU the code-embed
re-index may need:
  1. `claude` CLI on PATH      → best quality, requires CLI install (Max sub or API key)
                                 v0.2.23 C10: gated by a smoke-test, not just --version,
                                 so an installed-but-unauthenticated CLI doesn't get picked.
  2. z.ai GLM (cloud)          → v0.2.103 8b: the vendor's cheap/flash model, used
                                 ONLY when the z.ai key resolves on this machine (the
                                 model gateway's own VendorKeyResolver over its vendor
                                 registry). No key → the tier is silently not
                                 selectable (one debug line), never an error.
  3. QwenCloud (cloud)         → v0.2.103 8b: same shape, the qwen key. QwenCloud
                                 serves ONLY this summary fallback (owner ruling
                                 2026-10-14) — no lane, review or other consumer
                                 may grow on it.
  4. Ollama (local, FREE)      → http://localhost:11435, no extra dep beyond what
                                 the orchestrator already requires for embeddings.
                                 v0.2.103 8c: DEFERRED (tier reads unavailable, the
                                 caller records owed work) while a code-graph
                                 embedding walk is active or free VRAM is below the
                                 floor — the local model must not compete with the
                                 re-index for the GPU (field 2026-10-14: OOM).
  5. OpenAI API (opt-in)       → gated twice: an OpenAI key in VCO's shared slot
                                 AND `kg_summary_openai_consent` app_state key
                                 (default false). Set via launcher Preferences → KG
                                 Summaries. Bypass via `--force-api`. Costs apply.
  6. ANTHROPIC_API_KEY direct  → LEGACY opt-in fallback (kept by design): reached
                                 only when the env key is set AND tiers 1-5 were
                                 unavailable or consent-gated. Costs $$ per call —
                                 a cost warning is logged when the tier is selected.
  7. Silent skip               → friendly log line; the caller exits 0.

Env overrides (shared knobs — the ``KG_SUMMARY_*`` names stay canonical for
BOTH callers; the code generator adds a ``CODE_SUMMARY_BACKEND`` alias that
falls back to ``KG_SUMMARY_BACKEND`` via the ``env_keys`` parameter):
  KG_SUMMARY_BACKEND        → force "cli" | "zai" | "qwen" | "ollama" | "api" |
                              "openai" | "skip" (auto-detect default; "api" =
                              Anthropic, "openai" = OpenAI, "zai"/"qwen" = the
                              cloud vendor tiers). Unset: the launcher
                              Preferences → KG Summaries backend pin
                              (app_state kg_summary_backend_override)
  KG_SUMMARY_OLLAMA_MODEL   → Ollama model tag. Unset: the machine's generation
                              model, resolved by vco_lib.summary_model — the
                              launcher Preferences pick, else what install
                              recorded and pulled, else the hardware tier
                              (the strongest model whose free-VRAM floor the
                              card meets: qwen3.5:9b, else qwen3.5:4b;
                              gemma4:e4b on a capable CPU with no GPU;
                              qwen3.5:0.8b floor)
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
  VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN
                            → seconds a tier stays demoted after a QUOTA
                              (token/budget EXHAUSTION) or a TRUST failure
                              (default: 18000 — the owner's 5 h re-check
                              window; a 5 h or one-week limit resets on a
                              clock nobody here controls)
  VCO_SUMMARY_BREAKER_OTHER_COOLDOWN
                            → seconds a tier stays demoted after N consecutive
                              UNCLASSIFIED failures (default: 18000)
  VCO_SUMMARY_BREAKER_OTHER_STRIKES
                            → consecutive unclassified failures before a tier
                              is demoted (default: 3; a single unknown error
                              still never demotes — see REASON_OTHER)

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
     * ``quota`` (v0.2.96 WP-7) — token/budget EXHAUSTION ("usage limit
       reached", "out of credits"), DISTINCT from a transient 429: a quota
       outage lasts HOURS (the 5 h or one-week account window), so the
       v0.2.92 behaviour — riding the 900 s rate-limit cooldown — meant
       ~20 futile re-probes per 5 h outage. One occurrence demotes, for
       ``VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN`` (default 18 000 s — the
       owner's 5 h re-check window).
     * ``trust`` (v0.2.96 WP-7) — the CLI refused because the WORKSPACE is
       not trusted ("this workspace has not been trusted"). TERMINAL for
       the tier: no retry shape exists inside a headless run, so it shares
       the quota cooldown. Field report: 175+ per-symbol CLI calls, each
       dying on this, because trust classified as ``other`` and nothing
       ever tripped.
     * ``other`` — v0.2.96 WP-7: strike-gated like capacity, but with the
       LONG cooldown (``VCO_SUMMARY_BREAKER_OTHER_STRIKES`` default 3,
       ``VCO_SUMMARY_BREAKER_OTHER_COOLDOWN`` default 18 000 s). Before
       v0.2.96 ``other`` NEVER tripped — a breaker that demoted on every
       unknown error fell back when it should retry — but that also meant
       an UNCLASSIFIED failure storm never stopped (304 consecutive
       failures in one night). N consecutive unknowns is no longer "one
       weird error"; it is a storm, and the tier demotes. A SINGLE unknown
       still never demotes.
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

v0.2.100 WP-13 — what one ``claude -p`` call costs.

4. Isolation. A headless call used to start the CLI with everything an
   interactive session gets: every MCP server registered for the folder
   (each one a process boot) and every hook in the project's settings. A
   summary needs none of it. ``cli_isolation_args()`` adds
   ``--strict-mcp-config --mcp-config <empty config>`` (no MCP server
   starts) and ``--settings <file with disableAllHooks>`` (no hook runs) —
   EACH only when the installed CLI lists that flag in ``claude --help``,
   so an older CLI that lacks one still runs, without it, and says so once.
   The working directory is deliberately NOT moved to a neutral temp dir:
   a headless call in a folder the CLI has never trusted fails with "this
   workspace has not been trusted" (the v0.2.96 WP-7 field failure), and a
   fresh temp dir is never trusted — the flags remove what the folder would
   have loaded without making the call depend on a trust flag VCO must not
   write.
5. Batching. ``build_batch_prompt()`` / ``parse_batch_reply()`` are the one
   JSON envelope for "N items, one call" — the code-summary generator sends
   several entities per spawn and falls back to one call per entity for any
   item the reply does not answer validly.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ANTHROPIC_MODEL = "claude-haiku-5-5"
def _resolve_ollama_model() -> str:
    """The machine's local generation model (v0.2.102 W5-D).

    ``KG_SUMMARY_OLLAMA_MODEL`` wins without touching vco_lib; otherwise the
    ONE resolver, :func:`vco_lib.summary_model.resolve_model` (Preferences
    pick → install record → hardware tier, the same table install's pull list
    comes from). A ``vco_lib`` that cannot be imported means this interpreter
    is NOT the orchestrator's venv (whose ``pip install -e .`` puts vco_lib on
    ``sys.path``) — no install ever puts vco_lib into a user project's own
    venv. Said once on stderr naming the interpreter, and the empty model
    makes :func:`ollama_available` report the tier unavailable."""
    override = os.getenv("KG_SUMMARY_OLLAMA_MODEL", "").strip()
    if override:
        return override
    try:
        from vco_lib.summary_model import resolve_model
    except ImportError as exc:
        sys.stderr.write(
            f"[summary_backends] vco_lib is not importable from this interpreter "
            f"({sys.executable}): {exc}. VCO scripts must run under the "
            "orchestrator's own venv (the one install.py builds) — the local "
            "Ollama summary tier is unavailable\n")
        return ""
    return resolve_model()


def ollama_default_model() -> str:
    """``OLLAMA_DEFAULT_MODEL``, resolved on FIRST USE (v0.2.103).

    v0.2.102 resolved it at import time, so every per-node child printed the
    "vco_lib is not importable" stderr line even when the Ollama tier was
    never used — and, run under a user project's own venv, that line claimed a
    broken install whose remediation could never apply. Resolution (and the
    stderr line, when it genuinely applies) now happens only when the tier is
    actually consulted. Honours a test/runtime monkeypatch of the
    ``OLLAMA_DEFAULT_MODEL`` module attribute first."""
    override = globals().get("OLLAMA_DEFAULT_MODEL")
    if override is not None:
        return override
    model = _resolve_ollama_model()
    globals()["OLLAMA_DEFAULT_MODEL"] = model
    return model


def __getattr__(name: str):
    # PEP 562: `summary_backends.OLLAMA_DEFAULT_MODEL` stays a readable
    # module attribute for every importer (generate-kg-summary,
    # generate_node_formats, process_documents, tests) without eager
    # resolution — normal lookup lands here only until
    # `ollama_default_model()` (or a monkeypatch) materialises the global.
    if name == "OLLAMA_DEFAULT_MODEL":
        return ollama_default_model()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
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
#: v0.2.102 W5-D: the launcher Preferences → KG Summaries backend pin
#: ('' = auto-detect, or one of :data:`VALID_BACKENDS`). Written by the
#: launcher since v0.2.23 (preferences/loaders.ts — MUST MATCH); read by
#: `_forced_backend` after the env keys.
APP_STATE_KEY_BACKEND_OVERRIDE = "kg_summary_backend_override"

SYSTEM_PROMPT = (
    "You are a technical documentation summarizer. "
    "Write concise, specific, factual summaries. No filler words, no preamble. "
    "Start directly with the content."
)

# The valid KG_SUMMARY_BACKEND / CODE_SUMMARY_BACKEND force values. v0.2.103
# (8b) added the cloud vendor tiers "zai" and "qwen" (the model gateway's
# registry ids) between the CLI and local Ollama.
VALID_BACKENDS = {"cli", "zai", "qwen", "ollama", "api", "openai", "skip"}

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
ENV_BREAKER_QUOTA_COOLDOWN = "VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN"
ENV_BREAKER_OTHER_COOLDOWN = "VCO_SUMMARY_BREAKER_OTHER_COOLDOWN"
ENV_BREAKER_OTHER_STRIKES = "VCO_SUMMARY_BREAKER_OTHER_STRIKES"

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
#: v0.2.96 WP-7 (register issue 13). A quota outage lasts HOURS (the 5 h
#: or one-week account window), so the cooldown is the owner's stated
#: re-check cadence: 18 000 s = 5 h. `trust` shares it because trust is
#: TERMINAL for a headless run — the only clock that ends it is a human
#: re-accepting the workspace dialog, and retrying sooner costs a
#: subprocess per node for nothing.
DEFAULT_QUOTA_COOLDOWN_S = 18000.0
#: v0.2.96 WP-7 (register issue 14). An UNCLASSIFIED failure storm gets
#: the same long cooldown as quota: 304 consecutive `other` failures ran a
#: whole night because `other` never tripped. Same threshold shape as
#: capacity (N consecutive, so a single unknown still never demotes).
DEFAULT_OTHER_COOLDOWN_S = 18000.0
DEFAULT_OTHER_STRIKES = 3

#: Cross-process latch file, beside the machine's other VCT state. The
#: account cap this records is machine-wide, and the KG generator runs one
#: process per node — an in-process dict alone would never survive to the
#: next node.
BREAKER_FILENAME = "summary_backend_breaker.json"

REASON_RATE_LIMIT = "rate_limit"
REASON_CAPACITY = "capacity"
REASON_AUTH = "auth"
REASON_OTHER = "other"
REASON_QUOTA = "quota"
REASON_TRUST = "trust"

#: Reasons that demote a tier. v0.2.96 WP-7 added ``quota`` and ``trust``
#: (first strike, 5 h cooldown) and ``other`` (strike-gated, long cooldown)
#: — before it, quota rode rate_limit at 900 s and trust/other never tripped.
TRIPPING_REASONS = (
    REASON_RATE_LIMIT, REASON_AUTH, REASON_CAPACITY,
    REASON_QUOTA, REASON_TRUST, REASON_OTHER,
)

#: Reasons that demote only after N CONSECUTIVE occurrences, with the
#: strike count persisted in the same record as the latch (same
#: cross-process lifetime argument as ``BREAKER_FILENAME``). ``other``
#: joined in v0.2.96; the N differs by reason, so read it via
#: ``strikes_required(reason)``.
STRIKE_GATED_REASONS = (REASON_CAPACITY, REASON_OTHER)

#: v0.2.96 WP-7: reasons whose FRESH trip also notifies the degradation
#: ledger (best-effort detached ``python -m vco_lib.summary_health
#: note-degradation``; a no-op when no orchestrator root resolves).
NOTIFYING_REASONS = (REASON_QUOTA, REASON_TRUST)

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
#: v0.2.96 WP-7 — TRUST-shaped failures from a headless ``claude -p``. The
#: CLI's wording is "this workspace has not been trusted" (field-verified
#: 2026-09-20, 175+ occurrences). Matched BEFORE any status code and before
#: every other signal list: it is unambiguous, and misclassifying it as
#: ``other`` is the bug this list exists to fix.
_TRUST_SIGNALS = (
    "not been trusted", "untrusted workspace", "workspace trust",
    "trust dialog",
)
#: v0.2.96 WP-7 — QUOTA (token/budget EXHAUSTION) wording, split out of the
#: old rate-limit list. Deliberately does NOT contain "limit reached" /
#: "limit will reset": "rate limit reached" contains both substrings, so a
#: quota-first match order would swallow genuine TRANSIENT rate-limit
#: failures. The exhaustion phrases all carry their own unambiguous
#: vocabulary ("usage limit", "quota", "credits").
_QUOTA_SIGNALS = (
    "usage limit", "usage_limit", "quota", "insufficient_quota",
    "out of credits", "credit balance is too low",
)
#: v0.2.96 WP-7 — reduced to the TRANSIENT shapes only (429-class). Quota
#: wording moved to ``_QUOTA_SIGNALS``; a bare HTTP 429 body with no quota
#: vocabulary stays here.
_RATE_LIMIT_SIGNALS = (
    "rate limit", "rate_limit", "ratelimit",
    "too many requests", "429",
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


class ContentRefusal(NonAnswerResponse):
    """A REFUSAL-shaped non-answer: the model declined the content
    ("I cannot summarize this", "Sorry, …", "As an AI …").

    A subclass of ``NonAnswerResponse``, so every existing catch site keeps
    working; the KG summary generator catches it FIRST to record the node as
    refused (a flag in its per-node sidecar — owner ruling 2026-10-18: flag
    it, do not fall through to a lower tier, and the NEXT ordinary sync
    retries it). Non-refusal non-answers keep the old contract exactly.
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
    _CLI_ISOLATION.clear()
    _VENDOR_STATE.clear()
    _API_KEY_MEMO.clear()


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

    The reasons are kept DISTINCT because they need opposite responses,
    and collapsing them is how a circuit breaker becomes a bug:

    * ``trust`` — the headless CLI refused because the workspace is not
      trusted. TERMINAL for the tier: no retry inside a headless run can
      end it, so it opens the breaker with the LONGEST cooldown (the
      quota window) and the degradation notice names it. One occurrence.
    * ``quota`` — token/budget EXHAUSTION ("usage limit reached", "out of
      credits"). Retrying this tier cannot succeed until the account
      window rolls over — HOURS, not minutes — so one occurrence demotes
      with the 5 h cooldown (v0.2.96; it used to ride ``rate_limit``'s
      900 s, i.e. ~20 futile re-probes per outage).
    * ``rate_limit`` — a TRANSIENT 429. Retrying cannot succeed *right
      now*, so demote on the FIRST occurrence, but the window is short.
    * ``auth`` — the credential is wrong or expired. Retrying cannot
      succeed at all → demote on the first occurrence. Still time-boxed,
      never permanent: the user may re-login while a pass is running.
    * ``capacity`` — 529 / 503 / timeout. The endpoint is momentarily
      overloaded and RECOVERS BY ITSELF, so a single one must not demote
      the tier (this session saw a 529 and a healthy endpoint minutes
      later). Demotion takes N consecutive strikes and a short cooldown.
    * ``other`` — anything not positively identified. A single one NEVER
      trips, but v0.2.96 made it strike-gated: N CONSECUTIVE unknowns
      demote with a long cooldown, because an unclassified STORM must
      halt too (304 consecutive failures ran a whole night unfired).

    Match order: TRUST and QUOTA wording outrank everything a status code
    can say — a 429 whose body says the QUOTA is exhausted is a quota
    outage wearing a transient status, and that body is the only reason it
    was classified at all. Status codes then decide before the residual
    signal lists (auth → rate-limit → capacity).

    ``signal`` is the matched keyword or status code — never raw failure
    text, which on these paths can quote node content.
    """
    haystack = (text or "").lower()
    for signal in _TRUST_SIGNALS:
        if signal in haystack:
            return REASON_TRUST, signal
    for signal in _QUOTA_SIGNALS:
        if signal in haystack:
            return REASON_QUOTA, signal
    if status is not None:
        if status in _AUTH_STATUSES:
            return REASON_AUTH, str(status)
        if status in _RATE_LIMIT_STATUSES:
            return REASON_RATE_LIMIT, str(status)
        if status in _CAPACITY_STATUSES:
            return REASON_CAPACITY, str(status)
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
    """A non-negative float tunable; unset / empty / garbage / negative / nan
    → ``default``.

    DOCUMENTED COPY — must match ``v = vco_lib/env_values.py::env_float(name,
    None)`` followed by ``v if v is not None and v >= 0 else default`` (the
    ``>=`` sends nan to the default). Stdlib-only for the same reason as
    :func:`_int_env`; ``tests/test_v02102_env_values.py`` pins the parity.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _int_env(name: str, default: int) -> int:
    """A positive int tunable; unset / empty / garbage / below 1 → ``default``.

    DOCUMENTED COPY — must match ``vco_lib/env_values.py::env_int(name,
    default, minimum=1)``. This module is stdlib-only by contract (it runs as
    ``.claude/scripts/`` inside user projects, with no ``vco_lib`` on
    ``sys.path`` — see :func:`_save_breaker_file`), so it cannot import the
    one home; ``tests/test_v02102_env_values.py`` pins the parity.
    """
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

    Four durations, because the reasons differ in HOW they end:
    ``capacity`` recovers by itself (short), ``auth`` is ended by the user
    (short — see ``DEFAULT_AUTH_COOLDOWN_S``), ``rate_limit`` is ended by a
    clock nobody here controls (900 s), and ``quota`` / ``trust`` / the
    ``other`` STORM arm are ended by HOURS-long or human clocks (5 h).
    """
    if reason == REASON_CAPACITY:
        return _float_env(ENV_BREAKER_CAPACITY_COOLDOWN, DEFAULT_CAPACITY_COOLDOWN_S)
    if reason == REASON_AUTH:
        return _float_env(ENV_BREAKER_AUTH_COOLDOWN, DEFAULT_AUTH_COOLDOWN_S)
    if reason in (REASON_QUOTA, REASON_TRUST):
        return _float_env(ENV_BREAKER_QUOTA_COOLDOWN, DEFAULT_QUOTA_COOLDOWN_S)
    if reason == REASON_OTHER:
        return _float_env(ENV_BREAKER_OTHER_COOLDOWN, DEFAULT_OTHER_COOLDOWN_S)
    return _float_env(ENV_BREAKER_COOLDOWN, DEFAULT_BREAKER_COOLDOWN_S)


def capacity_strikes() -> int:
    """Consecutive ``capacity`` failures needed before a tier is demoted."""
    return _int_env(ENV_BREAKER_CAPACITY_STRIKES, DEFAULT_CAPACITY_STRIKES)


def other_strikes() -> int:
    """Consecutive UNCLASSIFIED failures needed before a tier is demoted.

    v0.2.96 WP-7: the ``other`` storm arm. Distinct knob from
    ``capacity_strikes`` because their storms are different phenomena —
    a 529-burst clears in minutes, an unclassified failure storm is by
    definition something nobody has diagnosed yet.
    """
    return _int_env(ENV_BREAKER_OTHER_STRIKES, DEFAULT_OTHER_STRIKES)


def strikes_required(reason: str) -> int:
    """Consecutive failures of *reason* that demote a tier (1 when not
    strike-gated)."""
    if reason == REASON_CAPACITY:
        return capacity_strikes()
    if reason == REASON_OTHER:
        return other_strikes()
    return 1


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

    STRIKE-GATED reasons (``capacity`` and, since v0.2.96, ``other``) need
    ``strikes_required(reason)`` CONSECUTIVE occurrences — one transient
    529 must not cost the user the best tier for 15 minutes, and a single
    unclassifiable failure must not either.

    The strike count is kept in the SAME record as the latch, on disk,
    for the same reason the latch is (v0.2.92 BLOCKER-2): the KG generator
    runs ONE PROCESS PER NODE, so a strike counter that lived only in this
    process was re-zeroed before the second 529 ever arrived and the
    capacity arm could never fire. A below-threshold record carries
    ``until: 0``, so it counts strikes without demoting anything.

    A FRESH latch on a ``NOTIFYING_REASONS`` failure (quota / trust) also
    spawns the degradation notice — see ``_notify_degradation``.
    """
    if not breaker_enabled() or reason not in TRIPPING_REASONS:
        return False
    now = _now()
    data = _load_breaker_file()
    if reason in STRIKE_GATED_REASONS:
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
        if strikes < strikes_required(reason):
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
    if reason in NOTIFYING_REASONS:
        _notify_degradation(tier, reason)
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


# ──────────────────────────────────────────────────────────────────────
# v0.2.96 WP-7 — degradation notice spawn (quota / trust trips)
# ──────────────────────────────────────────────────────────────────────
#: Handles of detached notify children, retained so the interpreter does
#: not GC a live Popen object into a zombie (the codegraph_resync
#: detached-children pattern).
_NOTIFY_CHILDREN: "list" = []


def _notify_orchestrator_root() -> "Path | None":
    """Best-effort orchestrator-root resolution for the notify spawn.

    In a user project this module runs from ``<project>/.claude/scripts/``
    (parents lead to the PROJECT, which has no ``vco_lib``), so only the
    ``VCT_ORCHESTRATOR_ROOT`` env can name the root there; inside the
    orchestrator checkout the ``templates/scripts`` path resolves by
    derivation. Returns None when neither yields a root carrying
    ``vco_lib`` — the caller then skips the notify entirely.
    """
    env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if env_root and (Path(env_root) / "vco_lib").is_dir():
        return Path(env_root)
    derived = Path(__file__).resolve().parent.parent.parent
    if (derived / "vco_lib").is_dir():
        return derived
    return None


def _notify_python(root: Path) -> "str | None":
    """The venv python beside *root*, else the running interpreter."""
    for candidate in (
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable or None


def _degradation_notify_argv(tier: str, reason: str) -> "list[str] | None":
    """``python -m vco_lib.summary_health note-degradation ...`` or None.

    The project root is the env the generators' spawners set
    (``KG_PROJECT_ROOT``), else the cwd the generator was launched with —
    the sync/resync spawn shapes both run with the project as cwd.
    """
    root = _notify_orchestrator_root()
    if root is None:
        return None
    python = _notify_python(root)
    if python is None:
        return None
    project_root = os.environ.get("KG_PROJECT_ROOT", "").strip() or os.getcwd()
    return [
        python, "-m", "vco_lib.summary_health", "note-degradation",
        "--tier", tier, "--reason", reason,
        "--project-root", project_root,
    ]


def _notify_degradation(tier: str, reason: str) -> None:
    """Best-effort DETACHED notice that the preferred tier just demoted
    on a quota/trust failure.

    This module cannot emit the ledger entry itself: it is stdlib-only by
    contract (it runs inside user projects' ``.claude/scripts/`` with no
    ``vco_lib`` on ``sys.path``) while the locked deferral emitter lives
    in ``vco_lib``. So the trip seam spawns the notifier detached and
    soft-fails on EVERY obstacle — the notify is observability, never a
    caller-visible failure (a summary run must not crash because a notice
    could not be delivered). Skipped entirely under pytest: unit tests
    must not spawn detached children.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    argv = _degradation_notify_argv(tier, reason)
    if argv is None:
        return
    try:
        import subprocess as _sub
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = _sub.Popen(
            argv,
            stdin=_sub.DEVNULL,
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
            **kwargs,
        )
        _NOTIFY_CHILDREN.append(proc)
    except Exception:  # noqa: BLE001 — observability only, never fatal
        pass


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
#: substitution (owner ruling 2026-10-18). So a legitimate summary that
#: opens with one of these prefixes (e.g. "Error: ..." as a heading) or is
#: shorter than ``NON_ANSWER_MIN_CHARS`` (e.g. "Caching") is refused while
#: its text stays byte-identical: the KG generator caches nothing usable
#: (a REFUSAL shape is flagged in the sidecar, other shapes are not cached
#: at all), so each ordinary sync RETRIES the node and refuses it again —
#: bounded per run, visible in the log, never a silent cache poison. Widen
#: these lists only with that cost in mind.
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

#: The REFUSAL subset of ``NON_ANSWER_PREFIXES`` (v0.2.104, owner ruling
#: 2026-10-18): the shapes where the model DECLINED the content, as opposed
#: to the degenerate conversational shapes ("Ready. What do you need
#: summarized?", "please provide …"). Only the declining shapes earn the
#: ``ContentRefusal`` subclass; every other non-answer keeps the old
#: contract. Same prefix anchoring and same asymmetry argument as
#: ``NON_ANSWER_PREFIXES`` — widen only with that cost in mind.
REFUSAL_PREFIXES = (
    "i cannot", "i can't", "i can not", "i am unable", "i'm unable",
    "i am not able", "i'm not able", "i do not have", "i don't have",
    "sorry", "i'm sorry", "i am sorry", "as an ai",
)


def _normalized_reply(text: "str | None") -> str:
    """The one normalization both non-answer predicates share (stripped,
    quote-fenced, whitespace-folded, lowercased)."""
    if text is None:
        return ""
    stripped = text.strip().strip("`\"'*# \t")
    return " ".join(stripped.split()).lower()


def is_non_answer(text: "str | None") -> bool:
    """True when *text* is a model NON-ANSWER rather than a summary.

    Recognises the empty reply, the too-short token, and the canned
    "Ready. What do you need summarized?" / refusal shapes that a field
    scan found in 43% of one project's stored summaries.
    """
    if text is None:
        return True
    normalized = _normalized_reply(text)
    if not normalized:
        return True
    if normalized in NON_ANSWER_EXACT:
        return True
    if len(normalized) < NON_ANSWER_MIN_CHARS:
        return True
    return normalized.startswith(NON_ANSWER_PREFIXES)


def is_content_refusal(text: "str | None") -> bool:
    """True when *text* is a REFUSAL-shaped non-answer (the model declined
    the content) rather than a degenerate conversational one. Always a
    subset of :func:`is_non_answer`."""
    return _normalized_reply(text).startswith(REFUSAL_PREFIXES)


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
#: v0.2.100 WP-13 — per-process cache of the isolation argv, keyed by the
#: resolved CLI path (a different binary may list different flags).
_CLI_ISOLATION: dict[str, list[str]] = {}
#: The one temp dir holding the two isolation files (created on first use,
#: removed at interpreter exit).
_ISOLATION_DIR: dict[str, Path] = {}

EMPTY_MCP_CONFIG = {"mcpServers": {}}
NO_HOOKS_SETTINGS = {"disableAllHooks": True}
CLI_HELP_TIMEOUT_S = 20
#: v0.2.103 (8g): how many times ``--help`` may be re-read while hunting the
#: complete text. Measured on Claude CLI 2.1.285: the CLI's own writer
#: INTERMITTENTLY truncates its help output at an ~8 KiB pipe boundary
#: (observed 8190 / 8191 / 16381 bytes where the full text is 22 336), so a
#: single read can lose everything past the first screenful — including the
#: ONLY place ``--strict-mcp-config`` is documented on that CLI generation.
#: Complete output is stable across runs, so two identical consecutive reads
#: are accepted at once; otherwise the longest read wins.
CLI_HELP_ATTEMPTS = 4


def _cli_lists_flag(help_text: str, flag: str) -> bool:
    """True when *flag* appears in ``--help`` as a whole option name
    (``--mcp-config`` must not match inside ``--strict-mcp-config``)."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def _isolation_dir() -> Path:
    cached = _ISOLATION_DIR.get("dir")
    if cached is not None and cached.is_dir():
        return cached
    path = Path(tempfile.mkdtemp(prefix="vco-summary-cli-"))
    (path / "empty-mcp.json").write_text(json.dumps(EMPTY_MCP_CONFIG), encoding="utf-8")
    (path / "no-hooks-settings.json").write_text(
        json.dumps(NO_HOOKS_SETTINGS), encoding="utf-8")
    _ISOLATION_DIR["dir"] = path
    atexit.register(shutil.rmtree, str(path), True)
    return path


def _cli_help_text(claude_path: str) -> str:
    """``claude --help`` output, or ``""`` when it cannot be read.

    v0.2.103 (8g) — why this RETRIES. Measured on Claude CLI 2.1.285 with
    ``subprocess.run(capture_output=True)`` (which reads to EOF, so the
    truncation is the CLI's own writer exiting early, not our reader):
    repeated reads returned 22 336 bytes, then 8 190, then 22 336 three
    times — an intermittent cut at an ~8 KiB pipe boundary. The full text
    is stable across runs, and every truncated read observed was a PREFIX
    of it, so "two identical consecutive reads" certifies completeness and
    "longest read wins" is never worse than the old single read.

    The second half of 8g: on that CLI generation ``--strict-mcp-config``
    has NO option row of its own — its only appearance is inside another
    option's description prose ("…add --strict-mcp-config to skip MCP
    servers too"), ~20 KB into the text, past every truncation point
    observed. So a single truncated read made ``cli_isolation_args``
    report the CLI as lacking a flag it documents and accepts — and every
    headless summary call started the user's MCP servers.
    """
    import subprocess as _sub

    best = ""
    previous = ""
    for _attempt in range(max(2, CLI_HELP_ATTEMPTS)):
        try:
            result = _sub.run(
                [claude_path, "--help"], capture_output=True, text=True,
                timeout=CLI_HELP_TIMEOUT_S, stdin=_sub.DEVNULL,
            )
        except (_sub.TimeoutExpired, OSError):
            return best
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if len(text) > len(best):
            best = text
        if text and text == previous:
            return text
        previous = text
    return best


#: v0.2.103 R2 (L-5): the isolation verdict is cached ACROSS processes in
#: the VCT state dir (the same home as the breaker latch), keyed by the CLI
#: binary's path + mtime + size — a CLI upgrade changes the stat, so the
#: verdict is re-derived exactly when the binary changed. Without this, a
#: one-process-per-node caller (the launcher's summary walk) re-ran
#: ``claude --help`` 2-4 times per node: 2-4 Node.js startups of ~1 s each
#: per summarised node.
CLI_ISOLATION_CACHE_FILENAME = "summary-cli-isolation-cache.json"


def _cli_isolation_cache_path() -> Path:
    return _vct_state_dir() / CLI_ISOLATION_CACHE_FILENAME


def _load_isolation_cache() -> dict:
    try:
        data = json.loads(_cli_isolation_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_isolation_cache(data: dict) -> None:
    """Persist the cache (tmp + replace). Soft-fails like the breaker file."""
    try:
        path = _cli_isolation_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only state dir must not break summary runs


def _cli_binary_fingerprint(claude_path: str) -> "str | None":
    try:
        st = os.stat(claude_path)
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def cli_isolation_args(claude_path: str) -> list[str]:
    """The argv that starts a headless call with no MCP server and no hook.

    Each flag is added only when the installed CLI lists it in ``--help``
    (verified once per process per binary); a missing flag is logged once
    and the call runs without it — never refused, never guessed.

    v0.2.103 R2 (L-5): the FLAG VERDICT is cached on disk (path + mtime +
    size key) beside the breaker latch, so the launcher's
    one-process-per-node summary walk stops paying 2-4 ``claude --help``
    spawns per node. Only the verdict is cached — never the argv, whose
    isolation-file paths point into THIS process's temp dir. A binary
    whose stat cannot be read is never cached (the key would be
    meaningless) — it simply re-probes, as before.
    """
    if claude_path in _CLI_ISOLATION:
        return list(_CLI_ISOLATION[claude_path])
    fingerprint = _cli_binary_fingerprint(claude_path)
    verdict: "dict[str, bool] | None" = None
    if fingerprint is not None:
        entry = _load_isolation_cache().get(claude_path)
        if (isinstance(entry, dict)
                and entry.get("fingerprint") == fingerprint
                and isinstance(entry.get("verdict"), dict)):
            verdict = {
                str(k): bool(v)
                for k, v in entry["verdict"].items() if k in ("strict_mcp", "settings")
            }
            if len(verdict) != 2:
                verdict = None
    if verdict is None:
        help_text = _cli_help_text(claude_path)
        verdict = {
            "strict_mcp": (_cli_lists_flag(help_text, "--strict-mcp-config")
                           and _cli_lists_flag(help_text, "--mcp-config")),
            "settings": _cli_lists_flag(help_text, "--settings"),
        }
        if fingerprint is not None:
            cache = _load_isolation_cache()
            cache[claude_path] = {"fingerprint": fingerprint, "verdict": verdict}
            _save_isolation_cache(cache)
    args: list[str] = []
    missing: list[str] = []
    try:
        if verdict["strict_mcp"]:
            args += ["--strict-mcp-config", "--mcp-config",
                     str(_isolation_dir() / "empty-mcp.json")]
        else:
            missing.append("--strict-mcp-config/--mcp-config (MCP servers will start)")
        if verdict["settings"]:
            args += ["--settings", str(_isolation_dir() / "no-hooks-settings.json")]
        else:
            missing.append("--settings (hooks will run)")
    except OSError as exc:
        # The isolation files could not be written: run un-isolated, say why.
        args = []
        missing = [f"isolation files ({exc})"]
    if missing:
        _log("  summary: the claude CLI at " + claude_path + " does not offer "
             + "; ".join(missing) + " — summary calls run without it")
    _CLI_ISOLATION[claude_path] = args
    return list(args)


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
             "--no-session-persistence", *cli_isolation_args(claude_path)],
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


#: v0.2.104 API-1 — the PROCESS-LOCAL memo of the Claude API-key fallback
#: (in memory only, never on disk; cleared by :func:`reset_backend_cache`):
#:
#: * ``"enabled"`` — the launcher switch, read once per process;
#: * ``"key"`` — the resolved key (``None`` = resolved, absent), read once
#:   per process — never per node;
#: * ``"key_first"`` — True after a quota-refused subscription call whose key
#:   retry SERVED: every later :func:`call_cli` of this process spawns with
#:   the key straight away (owner intent: "use the API key when the
#:   subscription is out"). The subscription is re-probed by the NEXT process
#:   (the next sync / hook run), which starts with an empty memo;
#: * ``"refused"`` — True once the key was refused (auth or quota on the
#:   key): no further key spawn in this process;
#: * ``"import_logged"`` — the broken-install line was printed.
_API_KEY_MEMO: dict = {}


def _memo_enabled(fallback_enabled) -> bool:
    if "enabled" not in _API_KEY_MEMO:
        try:
            _API_KEY_MEMO["enabled"] = bool(fallback_enabled())
        except Exception:
            # A broken reader is said ONCE per process (FINAL3 NIT): the
            # failure is memoised as "off", then raised so ``retry_env``
            # logs its one line; later nodes see False and stay silent.
            _API_KEY_MEMO["enabled"] = False
            raise
    return _API_KEY_MEMO["enabled"]


def _memo_key(resolve_key) -> "str | None":
    if "key" not in _API_KEY_MEMO:
        _API_KEY_MEMO["key"] = resolve_key()
    return _API_KEY_MEMO["key"]


def _quota_retry_env(base_env: dict) -> "dict | None":
    """The child env for the ONE quota retry on the Console API key, or None.

    v0.2.104 API-1: when the subscription is out of usage, the launcher
    setting for background jobs' API-key retry
    (``model_gateway.api_key_fallback``) allows this job ONE retry with
    VCO's shared ``ANTHROPIC_API_KEY_GATEWAY`` secret, handed to the child as
    ``ANTHROPIC_API_KEY`` in its environment only (never argv, never a file,
    never a log line). The decision lives in
    :func:`vco_lib.claude_cli_api_key.retry_env`; the switch and the key are
    read through this process's memo (:data:`_API_KEY_MEMO`), so a sync of
    many nodes reads each ONCE. A key already refused in this process means
    no retry.

    This module is stdlib-only at import (it runs from a project's
    ``.claude/scripts/``), so ``vco_lib`` is imported lazily. Every hook and
    launcher path that spawns it uses the orchestrator's venv, whose
    ``pip install -e .`` puts ``vco_lib`` on ``sys.path``; an ``ImportError``
    here is therefore a BROKEN install and is said once on stderr (the same
    rule as :func:`_resolve_ollama_model`), never swallowed silently.
    """
    if _API_KEY_MEMO.get("refused"):
        return None
    try:
        from vco_lib import claude_cli_api_key as _cak  # noqa: PLC0415
    except ImportError as exc:
        if not _API_KEY_MEMO.get("import_logged"):
            _API_KEY_MEMO["import_logged"] = True
            sys.stderr.write(
                f"[summary_backends] vco_lib is not importable from this "
                f"interpreter ({sys.executable}): {exc}. The subscription is "
                "out of usage, but the Claude API-key retry is unavailable — "
                "VCO scripts must run under the orchestrator's own venv (the "
                "one install.py builds)\n")
        return None
    return _cak.retry_env(
        base_env, _log,
        enabled=lambda: _memo_enabled(_cak.fallback_enabled),
        resolve=lambda: _memo_key(_cak.resolve_key),
    )


def _key_first_env(base_env: dict) -> "dict | None":
    """The keyed child env when this process already switched to the key
    (:data:`_API_KEY_MEMO` ``key_first``), else None."""
    if not _API_KEY_MEMO.get("key_first") or _API_KEY_MEMO.get("refused"):
        return None
    key = _API_KEY_MEMO.get("key")
    if not key:
        return None
    from vco_lib.claude_cli_api_key import child_env_with_key  # noqa: PLC0415

    return child_env_with_key(base_env, key)


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

    v0.2.100 WP-13: the argv also carries :func:`cli_isolation_args` (no MCP
    server, no hook), each flag only when this CLI lists it in ``--help``.

    v0.2.104 API-1: a failure the classifier reads as ``quota`` (the
    subscription out of usage) gets ONE retry with the shared
    ``ANTHROPIC_API_KEY_GATEWAY`` Console key when the launcher's
    background-job API-key switch is ON — see :func:`_quota_retry_env`.
    When that retry serves, the rest of THIS process goes straight to the key
    (:data:`_API_KEY_MEMO`): one subscription spawn per process during an
    outage, not one per node. A key refusal (auth, or quota on the key)
    ends that for the process and the call falls back to the subscription;
    a transient key failure (capacity, timeout) is raised like any CLI
    failure and the breaker handles it. Breaker semantics are unchanged:
    the ``cli`` tier is cleared by any served call (subscription or key)
    and latched by the subscription's quota refusal when the key could not
    serve. Every other failure class, the switch OFF, or no key resolvable:
    exactly the pre-fallback behaviour.
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
    # --no-session-persistence: without it every one-shot summary call
    # persists a transcript into the user's session list (943 machine-
    # generated transcripts / 1.05 GB measured on one install). Verified
    # on Claude CLI 2.1.258+; post-git-commit-kg-sync.{sh,ps1} already
    # use this flag.
    argv = [claude_path, "-p", "--model", "haiku", "--max-turns", "1",
            "--no-session-persistence", *cli_isolation_args(claude_path)]
    base_env = {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

    def _attempt(env: dict) -> "tuple[str | None, str, str, str]":
        """One spawn, classified: ``(answer, reason, signal, message)``.

        ``answer`` is None on any failure; ``message`` is then the
        ready-made :class:`BackendUnavailable` text (never the model's
        words beyond the short stderr snippet the pre-existing messages
        already carried).
        """
        result = subprocess.run(
            argv,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
        if result.returncode != 0:
            reason, signal = classify_backend_failure(
                text=f"{result.stderr}\n{result.stdout}",
            )
            return None, reason, signal, f"claude CLI failed: {result.stderr[:200]}"
        out = result.stdout.strip()
        # Exit 0 but the "answer" is a usage-limit / auth notice: a tier
        # failure wearing a success exit code. Caching it would poison the
        # sidecar AND hide the outage.
        reason, signal = _classify_exit_zero_text(out)
        if reason != REASON_OTHER:
            return None, reason, signal, (
                "claude CLI returned a backend notice instead of an answer "
                f"({reason})"
            )
        return out, REASON_OTHER, "", ""

    key_env = _key_first_env(base_env)
    if key_env is not None:
        # This process already switched to the key (its subscription was out
        # of usage and the key served): spend no subscription spawn.
        try:
            out, reason, signal, message = _attempt(key_env)
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailable(
                "cli", REASON_CAPACITY, "timeout",
                f"claude CLI timed out after {TIMEOUT}s (Claude API key)",
            ) from exc
        if out is not None:
            return out
        if reason not in (REASON_AUTH, REASON_QUOTA):
            raise BackendUnavailable("cli", reason, signal, message)
        _API_KEY_MEMO["refused"] = True
        _API_KEY_MEMO["key_first"] = False
        _log(f"  summary: the Claude API key was refused ({reason}) — back to "
             "the subscription for the rest of this run")
    try:
        out, reason, signal, message = _attempt(base_env)
    except subprocess.TimeoutExpired as exc:
        raise BackendUnavailable(
            "cli", REASON_CAPACITY, "timeout",
            f"claude CLI timed out after {TIMEOUT}s",
        ) from exc
    if out is None and reason == REASON_QUOTA:
        # v0.2.104 API-1 — the ONE fallback retry: only a quota-classified
        # failure triggers it, only when the launcher setting is ON, the
        # job's env may be retried and the shared key resolves
        # (``_quota_retry_env`` decides). The key reaches the child through
        # its environment alone.
        retry_env = _quota_retry_env(base_env)
        if retry_env is not None:
            original = (reason, signal, message)
            _log("  summary: subscription quota exhausted — retrying with "
                 "the Claude API key (ANTHROPIC_API_KEY_GATEWAY)")
            try:
                out, reason, signal, message = _attempt(retry_env)
            except subprocess.TimeoutExpired:
                out, reason = None, REASON_CAPACITY
            if out is not None:
                _API_KEY_MEMO["key_first"] = True
                _log("  summary: the Claude API key served — the rest of this "
                     "run uses it directly (the subscription is re-tried by "
                     "the next run)")
            else:
                if reason in (REASON_AUTH, REASON_QUOTA):
                    _API_KEY_MEMO["refused"] = True
                    _log("  summary: the Claude API key was refused — no "
                         "second retry; the quota outage handling proceeds")
                else:
                    _log("  summary: the API-key retry did not serve — no "
                         "second retry; the quota outage handling proceeds")
                # The breaker must still hear about the ORIGINAL quota
                # failure: on the subscription this tier is out, and the
                # ladder descends exactly as when no retry was attempted.
                reason, signal, message = original
    if out is None:
        raise BackendUnavailable("cli", reason, signal, message)
    return out


# ──────────────────────────────────────────────────────────────────────
# Backend: cloud vendor tiers (v0.2.103 8b — z.ai GLM, then QwenCloud)
# ──────────────────────────────────────────────────────────────────────
#: Owner order 2026-10-14: cloud providers BEFORE local Ollama, z.ai GLM
#: before QwenCloud. These are the model gateway's own registry ids
#: (``model_router.vendors.VENDORS`` keys) — the tier names double as the
#: ``KG_SUMMARY_BACKEND`` force values and breaker keys.
CLOUD_VENDOR_TIERS: tuple[str, ...] = ("zai", "qwen")

#: Resolved tier facts per vendor id (see :func:`_vendor_tier_state`), or an
#: empty dict caching "known not selectable" so the miss is probed (and its
#: ONE debug line logged) once per process, not once per node call.
_VENDOR_STATE: dict[str, dict] = {}


def _load_gateway_registry():
    """``(VENDORS, FALLBACK_LADDER, DEFAULT_ANTHROPIC_VERSION, resolver)``
    from the model gateway package, or ``None`` when it is not importable
    here (a per-project ``.claude/scripts/`` context with no gateway on
    ``sys.path``).

    Split out as its own function so tests can substitute a synthetic
    registry; the lazy import keeps this module importable without the
    gateway package (the stdlib-only contract, see ``_save_breaker_file``).
    """
    from model_router.secrets import VendorKeyResolver
    from model_router.vendors import (
        DEFAULT_ANTHROPIC_VERSION,
        FALLBACK_LADDER,
        VENDORS,
    )

    return VENDORS, FALLBACK_LADDER, DEFAULT_ANTHROPIC_VERSION, VendorKeyResolver()


def _vendor_tier_state(vendor_id: str) -> "dict | None":
    """The call facts for *vendor_id*'s summary tier, or ``None`` when the
    tier is not selectable on this machine.

    Every vendor fact comes from the model gateway's ONE registry — never a
    second copy here: base URL and auth-header shape from the ``Vendor``
    row, the key through ``VendorKeyResolver`` over ``VENDORS`` (the same
    resolution ``vco_lib.model_selection.vendor_key_probe`` uses), and the
    MODEL from the gateway's own fallback ladder
    (``model_router.vendors.FALLBACK_LADDER``): the rung that escalates TO
    this vendor names its cheap/flash model — glm-5.3-flash for z.ai,
    qwen3.8-flash for QwenCloud — and summaries are short, so the cheap
    rung is the right one. No key / no gateway package / no cheap rung →
    ``None`` after ONE debug line: a third-party user without that
    provider simply never sees the tier, never an error. A key VALUE never
    reaches a log line, a message or argv (only the vendor's short name
    does).
    """
    cached = _VENDOR_STATE.get(vendor_id)
    if cached is not None:
        return cached or None
    state = _vendor_tier_state_uncached(vendor_id)
    _VENDOR_STATE[vendor_id] = state or {}
    return state


def _vendor_tier_state_uncached(vendor_id: str) -> "dict | None":
    try:
        registry = _load_gateway_registry()
    except Exception as exc:  # noqa: BLE001 — one line, the tier is off, that's all
        _log(f"  summary: cloud tier '{vendor_id}' not selectable "
             f"(model gateway package unavailable: {type(exc).__name__})")
        return None
    if registry is None:  # a substituted (test) registry said "absent"
        return None
    vendors, ladder, anthropic_version, resolver = registry
    vendor = vendors.get(vendor_id)
    if vendor is None:
        return None
    # The summary model: the gateway's own CHEAP rung for this vendor. The
    # ladder's contract is that a rung's ``to_model_id`` is an id the vendor
    # row is known to serve, so this can never name an un-routable id.
    model = next(
        (rung.to_model_id for rung in ladder if rung.to_vendor_id == vendor_id),
        None,
    )
    if model is None:
        _log(f"  summary: cloud tier '{vendor_id}' not selectable "
             "(no cheap rung in the gateway fallback ladder)")
        return None
    try:
        key = resolver.resolve(vendor).key
    except Exception:  # noqa: BLE001 — resolution never selects the tier
        key = None
    if not key:
        _log(f"  summary: cloud tier '{vendor_id}' not selectable "
             f"(no {vendor.short_name or vendor_id} key resolved)")
        return None
    return {
        "key": key,
        "model": model,
        "upstream": vendor.upstream,
        "auth_header": vendor.auth_header,
        "auth_scheme": vendor.auth_scheme,
        "anthropic_version": anthropic_version,
    }


def _vendor_available(vendor_id: str) -> bool:
    return _vendor_tier_state(vendor_id) is not None


def zai_available() -> bool:
    """The z.ai GLM tier is selectable (its key resolves on this machine)."""
    return _vendor_available("zai")


def qwen_available() -> bool:
    """The QwenCloud tier is selectable (its key resolves on this machine)."""
    return _vendor_available("qwen")


def _vendor_summary_model(vendor_id: str) -> str:
    state = _vendor_tier_state(vendor_id)
    return str(state["model"]) if state else ""


def _call_vendor(vendor_id: str, prompt: str) -> str:
    """One Anthropic-messages request against *vendor_id*'s upstream.

    The request shape is the same ``/v1/messages`` payload the legacy
    ``api`` tier already builds (these vendors serve Anthropic-shaped
    APIs — that is why the gateway relays them for Claude Code); the URL,
    auth header and version header come from the registry row, so no
    vendor fact is duplicated here. HTTP errors propagate to
    :func:`call_llm`, whose breaker classifies them (401/403 auth, 429
    rate limit, 5xx capacity) and demotes exactly like any other tier.
    """
    state = _vendor_tier_state(vendor_id)
    if state is None:
        raise RuntimeError(
            f"cloud tier '{vendor_id}' has no resolved key at call time"
        )
    payload = json.dumps(
        {
            "model": state["model"],
            "max_tokens": 350,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{state['upstream'].rstrip('/')}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            state["auth_header"]: f"{state['auth_scheme']}{state['key']}",
            "anthropic-version": state["anthropic_version"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content", [])
    # ``type == "text"`` skips thinking/reasoning blocks a vendor may add.
    return "".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    ).strip()


def call_zai(prompt: str) -> str:
    return _call_vendor("zai", prompt)


def call_qwen(prompt: str) -> str:
    return _call_vendor("qwen", prompt)


# ──────────────────────────────────────────────────────────────────────
# Backend: Ollama
# ──────────────────────────────────────────────────────────────────────
# Per-model SAMPLING params for short technical summarization. Override
# via env: KG_SUMMARY_OLLAMA_OPTIONS='{"temperature":0.5,"top_p":0.9}'
#
# The context window is NOT set here (v0.2.102 W4-G): load-time options
# (num_ctx and friends) are owned by the ONE generate-body builder,
# `vco_lib.ollama_generate.generate_request_body`, so every VCO caller loads a
# model with the same window and Ollama keeps ONE resident copy. Its window
# (8192 since v0.2.103 8c — the smallest shared value that fits every
# generation prompt VCO sends; see that module) is machine-wide and
# overridable with VCO_OLLAMA_GENERATE_NUM_CTX; a num_ctx inside
# KG_SUMMARY_OLLAMA_OPTIONS is superseded by that knob (dropped, with a
# stderr note naming it). Our prompts are ~1.5k tokens (4000-char body
# truncation) + 350 num_predict.
#
# qwen3.5:* defaults to thinking-mode and emits <think>...</think> blocks
# unless suppressed. Ollama exposes a `think: false` toggle on /api/generate
# (added in 0.5+). We pass it AND post-strip any leaked think blocks defensively.
OLLAMA_MODEL_DEFAULTS: dict[str, dict] = {
    "qwen3.5": {
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "qwen3": {  # fallback for plain qwen3 tags
        "temperature": 0.5,
        "top_p": 0.8,
        "top_k": 20,
        "num_predict": 1024,
        "repeat_penalty": 1.1,
    },
    "gemma4": {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
        "num_predict": 1024,
    },
    "gemma3": {  # fallback for plain gemma3 tags
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 64,
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
        "num_predict": 1024,
    })


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (qwen3 family)."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _codegraph_walk_active() -> bool:
    """True when a code-graph embedding walk is provably running NOW.

    Two read-only signals, either sufficient (v0.2.103 8c):

    * the launcher's ``code_graph_builds`` ledger carries a row in
      ``running`` for ANY project — the GPU is machine-wide, so another
      project's walk competes just as hard. ``running`` only (v0.2.103 R2,
      L-5): a merely ``pending`` row is a queued build that has not started
      (launcher closed, ``resume_pending_builds`` not yet run) — deferring
      on it blocked local summaries while nothing used the GPU;
      ``vco_lib.collection_rename._refuse_if_in_flight`` keeps the stricter
      pending+running filter because a RENAME must not race a queued build's
      later spawn — the two guards answer different questions;
    * the code-embed service's ``/health`` reports ``in_flight`` requests
      or ``inference_busy`` (its own liveness fields, v0.2.102 r2).

    Every failure — missing DB/table, locked DB, unreachable service,
      malformed body — reads "not known" (False): the guard DEFERS only on
      positive evidence a walk is live, never on a probe that failed.
    """
    try:
        db_path = _vct_state_dir() / "launcher.db"
        if db_path.is_file():
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT 1 FROM code_graph_builds "
                    "WHERE status = 'running' LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return True
    except Exception:  # noqa: BLE001 — a failed probe is not evidence
        pass
    base = (os.getenv("CODE_EMBED_SERVICE_URL")
            or os.getenv("CODE_EMBED_URL")
            or "http://localhost:11440").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8") or "{}")
        if health.get("in_flight") or health.get("inference_busy"):
            return True
    except Exception:  # noqa: BLE001 — service down: nothing to defer for
        pass
    return False


#: Free-VRAM floor for the local tier (MB). v0.2.103 R2 (H-4): sized from
#: the RESOLVED model's own need — its on-disk weights size (Ollama
#: ``/api/tags`` ``size``) plus a KV-cache/activation margin — NOT one flat
#: constant for every tier. The old flat 8 192 MB floor (sized for
#: ``qwen3.5:9b``) disabled the local tier FOREVER on every 6-8 GB GPU,
#: even with no cloud key and no walk active. A machine whose free VRAM is
#: below the derived floor defers the node (owed work) rather than risk the
#: OOM the field saw. ``KG_SUMMARY_MIN_FREE_VRAM_MB`` overrides the derived
#: floor. The floor RULE itself lives in ONE place,
#: :func:`vco_lib.embedding_selection.summary_required_free_vram_mb`, which
#: the hardware tier table also asks — so the table never assigns a model
#: this guard would refuse to load (v0.2.103, owner 2026-10-15).
ENV_MIN_FREE_VRAM_MB = "KG_SUMMARY_MIN_FREE_VRAM_MB"

VRAM_PROBE_TIMEOUT_S = 5
OLLAMA_PROBE_TIMEOUT_S = 3


def _free_vram_mb() -> "int | None":
    """Free VRAM in MB (the BEST card), or ``None`` when unknown.

    ``nvidia-smi`` is the only probe — it ships with the NVIDIA driver on
    every OS VCO supports. Absent (non-NVIDIA machine), failing or slow →
    unknown, and the caller then proceeds only when no walk is active
    (the brief's cross-OS rule: a failed probe must not block the tier on
    its own).

    v0.2.103 R2 (L-5): the MAXIMUM across cards, not GPU 0's line — Ollama
    places the model where the VRAM is, so a machine whose GPU 0 is busy
    but GPU 1 is empty must not read as VRAM-starved.
    """
    import subprocess as _sub

    smi = shutil.which("nvidia-smi")
    if smi is None:
        return None
    try:
        result = _sub.run(
            [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=VRAM_PROBE_TIMEOUT_S,
            stdin=_sub.DEVNULL,
        )
    except (_sub.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    values: list[int] = []
    for ln in lines:
        try:
            values.append(int(float(ln.split()[0])))
        except (IndexError, ValueError):
            continue
    return max(values) if values else None


def _required_free_vram_mb(weights_mb: "int | None") -> int:
    """The floor for a model whose on-disk size is *weights_mb* MB — the
    ONE floor rule, :func:`vco_lib.embedding_selection.summary_required_free_vram_mb`
    (``None`` → its conservative default: an unknown model must not lower
    the bar silently).

    Raises ``ImportError`` when vco_lib is not importable from this
    interpreter; :func:`_ollama_local_deferred` turns that into a logged
    deferral rather than guessing a floor.
    """
    from vco_lib.embedding_selection import summary_required_free_vram_mb

    return summary_required_free_vram_mb(weights_mb)


def _min_free_vram_mb(weights_mb: "int | None" = None) -> int:
    """The effective floor: ``KG_SUMMARY_MIN_FREE_VRAM_MB`` when set,
    else the model-derived one."""
    override = _int_env(ENV_MIN_FREE_VRAM_MB, 0)
    if override >= 1:
        return override
    return _required_free_vram_mb(weights_mb)


def _ollama_model_resident(model: str) -> bool:
    """True when Ollama's ``/api/ps`` shows *model* already loaded.

    A resident model needs NO new VRAM allocation, so the free-VRAM floor
    must not count it against itself (H-4: a 16 GB GPU with the generation
    model idle-resident read as "below floor" forever). Any probe failure
    reads "not resident" — the caller then applies the ordinary floor.
    """
    want = model if ":" in model else f"{model}:latest"
    try:
        with urllib.request.urlopen(
            f"{OLLAMA_URL}/api/ps", timeout=OLLAMA_PROBE_TIMEOUT_S
        ) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 — a failed probe is not residency
        return False
    models = body.get("models") if isinstance(body, dict) else None
    names = {
        str(m.get(key)) for m in (models if isinstance(models, list) else [])
        if isinstance(m, dict) for key in ("name", "model") if m.get(key)
    }
    return want in names


def _ollama_local_deferred(weights_mb: "int | None" = None) -> "str | None":
    """Why the local tier must NOT load right now, or ``None``.

    v0.2.103 8c (field 2026-10-14: a local 32K-ctx generation model filled
    the 16 GB GPU beside the code-embedding re-index → OOM). DEFER — never
    kill anything, never time out long work: the tier reads unavailable,
    the ladder moves on, and the caller's existing no-backend outcome
    records the node as owed work for the next run. A FORCED
    ``KG_SUMMARY_BACKEND=ollama`` bypasses this guard by design — the
    forced path never probes availability (see ``select_backend``).

    v0.2.103 R2 (H-4): the free-VRAM floor is sized from the model's own
    need (``weights_mb``, from ``/api/tags``), and the check is SKIPPED
    entirely when ``/api/ps`` shows the model already resident — nothing
    new would be allocated.
    """
    if _codegraph_walk_active():
        return ("a code-graph embedding walk is active — the local model "
                "would compete with it for VRAM")
    if _ollama_model_resident(ollama_default_model()):
        return None  # already loaded: no new allocation to make room for
    free = _free_vram_mb()
    if free is None:
        return None  # unknown free VRAM: proceed (cross-OS rule above)
    try:
        floor = _min_free_vram_mb(weights_mb)
    except ImportError as exc:
        return (f"cannot size the free-VRAM floor: vco_lib is not importable "
                f"from this interpreter ({type(exc).__name__}) — set "
                f"{ENV_MIN_FREE_VRAM_MB} or run under the orchestrator venv")
    if free < floor:
        return (f"only {free} MB VRAM free "
                f"(< {floor} MB floor the resolved model needs)")
    return None


def ollama_available() -> bool:
    """The Ollama tier is usable only when Ollama answers AND already HOLDS
    the summary model (``OLLAMA_DEFAULT_MODEL``).

    v0.2.100 (owner 2026-09-29): install pulls ONLY the one text-generation
    model the machine's tier uses. A model that is not present here — a lower
    rung, or a tier this machine never pulled — degrades to the next backend
    (OpenAI / Anthropic API / skip); nothing is ever pulled at runtime (an
    ``/api/generate`` for an absent model is a 404, and a pull is multi-GB).

    v0.2.103 8c: the local tier is additionally DEFERRED while a code-graph
    embedding walk is active or free VRAM is below the model-derived floor —
    see :func:`_ollama_local_deferred`.

    v0.2.103 R2 (L-5): ``/api/tags`` is checked BEFORE the deferral
    reasons — a machine that simply lacks the model must log "does not hold
    <model> — not pulling it", not "deferred — low VRAM", and the model's
    on-disk size from the same ``/api/tags`` reply is what sizes the floor.
    """
    model = ollama_default_model()
    if not model:
        _log("  Ollama summary tier unavailable: no generation model resolved "
             "(vco_lib not importable from this interpreter — see the stderr "
             "line above)")
        return False
    want = model if ":" in model else f"{model}:latest"
    try:
        with urllib.request.urlopen(
            f"{OLLAMA_URL}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_S
        ) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False
    models = body.get("models") if isinstance(body, dict) else None
    size_mb: "int | None" = None
    held = False
    for m in (models if isinstance(models, list) else []):
        if not isinstance(m, dict):
            continue
        for key in ("name", "model"):
            if m.get(key) and str(m.get(key)) == want:
                held = True
                raw_size = m.get("size")
                if isinstance(raw_size, (int, float)) and raw_size > 0:
                    size_mb = int(raw_size) // (1024 * 1024)
                break
        if held:
            break
    if not held:
        _log(f"  Ollama answers but does not hold `{want}` — not pulling it at runtime; "
             "trying the next summary backend")
        return False
    defer_reason = _ollama_local_deferred(size_mb)
    if defer_reason is not None:
        _log(f"  Ollama summary tier deferred — {defer_reason}; "
             "the node stays owed for the next run")
        return False
    return True


def call_ollama(prompt: str, model: str = "") -> str:
    # v0.2.102 W4-G: the body comes from the ONE generate-body builder
    # (`vco_lib.ollama_generate`) — shared load-time options (num_ctx) so the
    # model stays one resident copy across every VCO caller, plus the shared
    # keep_alive policy. Deliberately unguarded: a missing vco_lib is a broken
    # install, and this tier must fail (the ladder moves on and logs it)
    # rather than load a second runner with divergent options.
    if not model:
        model = ollama_default_model()
    from vco_lib.ollama_generate import generate_request_body

    family = model.split(":", 1)[0].lower()
    body = generate_request_body(
        model,
        prompt=prompt,
        system=SYSTEM_PROMPT,
        options=_ollama_options_for(model),
        # Ollama 0.5+ recognizes `think`; older versions ignore it.
        think=False if family.startswith("qwen3") else None,
    )
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
    """Return True if an OpenAI key is configured (the shared
    `openai_api_key` secret — :func:`_openai_api_key`).

    The actual gating (consent + key) is composed by `select_backend`
    — this just answers "is a key present at all". The consent check
    is intentionally a separate step so the log message can distinguish
    "no key" from "key present but consent withheld" (the latter is
    actionable; the former just means OpenAI isn't an option).
    """
    return bool(_openai_api_key())


# v0.2.98 (owner ruling 2026-09-26): resolved through the shipped resolver
# in its SHARED-ONLY form, and `$OPENAI_API_KEY` is never read. VCO's own
# consumer must resolve ONLY VCO's slot — the shared `openai_api_key`
# secret (launcher keychain → ~/.vct-secrets/shared) — never a project
# scope, never a project `.env`, never the generic env var; a project's
# key is the project's. `install.py --openai-key` stores it there and
# never in `.env`. This script cannot import `vco_lib` (see
# `_save_breaker_file`), so it asks the SHIPPED resolver next to it —
# `vct_secrets_resolve.sh --shared-only <project> <key>`
# (`-SharedOnly <project> <key>` on Windows, where `pwsh -File` binds a
# leading `-`-token as a parameter), the sibling of `vco_lib.openai_key` /
# `vco_lib.agent_secrets.get` — once per process.
_OPENAI_SECRET_NAME = "openai_api_key"  # must match vco_lib.openai_key.OPENAI_SECRET_NAME
_openai_key_cache: list[str] = []

#: v0.2.102 (W6-AB): how long the shipped resolver may take before this
#: process gives up on it. The resolver waits up to 30 s per hub request
#: (``_SECRET_READ_TIMEOUT_S`` / ``$script:SecretReadTimeoutMs`` — a busy OS
#: keychain behind a WORKING hub can take that long), makes two requests
#: (by-path, then /env), and retries the pair ONCE after the hub's 2 s
#: Retry-After: 4 x 30 + 2 = 122 s worst case. The former 15 s killed a
#: slow-but-working resolver mid-answer. Must exceed that worst case
#: (pinned by tests/test_secret_read_timeout_parity.py).
_SHIPPED_RESOLVER_TIMEOUT_S = 150


def _resolve_secret_via_shipped_resolver(key: str) -> str:
    """The value the shipped resolver prints for ``key`` (stdout, exit 0), or
    ``""`` — asked in the resolver's SHARED-ONLY form, so only VCO's shared
    slot is consulted (never a project scope, never the project's `.env`).
    The value stays in this process: never logged, never in argv."""
    here = Path(__file__).resolve().parent
    project = os.environ.get("KG_PROJECT_ROOT", "").strip() or os.getcwd()
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        script = here / "vct_secrets_resolve.ps1"
        argv = (
            [shell, "-NoProfile", "-File", str(script), "-SharedOnly", project, key]
            if shell
            else None
        )
    else:
        shell = shutil.which("bash")
        script = here / "vct_secrets_resolve.sh"
        argv = (
            [shell, str(script), "--shared-only", project, key] if shell else None
        )
    if argv is None or not script.is_file():
        return ""
    import subprocess

    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_SHIPPED_RESOLVER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _openai_api_key() -> str:
    """The resolved shared ``openai_api_key`` slot, or ``""``.

    `$OPENAI_API_KEY` is deliberately NOT read (v0.2.98 owner ruling
    2026-09-26): VCO's own consumer resolves only VCO's shared slot — a
    project's key is the project's."""
    if not _openai_key_cache:
        _openai_key_cache.append(_resolve_secret_via_shipped_resolver(_OPENAI_SECRET_NAME))
    return _openai_key_cache[0]


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
    api_key = _openai_api_key()
    if not api_key:
        raise RuntimeError(
            "no OpenAI key: OPENAI_API_KEY is not set and the `openai_api_key` "
            "secret is not stored"
        )
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
    """First non-empty forced-backend value: *env_keys* in order, then the
    launcher Preferences pin (``app_state[kg_summary_backend_override]``,
    v0.2.102 W5-D — the operator's env wins over the GUI, as for every
    other summary knob).

    The KG generator passes the default ``("KG_SUMMARY_BACKEND",)``; the
    code generator passes ``("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND")``
    (its own alias first, falling back to the shared knob).
    """
    return _forced_backend_with_source(env_keys)[0]


def _forced_backend_with_source(env_keys: tuple[str, ...]) -> "tuple[str, str]":
    """``(value, source)`` behind :func:`_forced_backend`; source is
    ``"env"``, ``"Preferences"`` or ``""``."""
    for key in env_keys:
        value = os.getenv(key, "").lower().strip()
        if value:
            return value, "env"
    pinned = (_read_app_state_value(APP_STATE_KEY_BACKEND_OVERRIDE) or "").lower().strip()
    if pinned:
        return pinned, "Preferences"
    return "", ""


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

    Selection order (v0.2.23 C10; cloud tiers inserted v0.2.103 8b):
      1. Forced-backend env override (``env_keys``, first non-empty; if valid).
      2. claude CLI on PATH + smoke-test passes.
      3. z.ai GLM — only when the z.ai key resolves (the model gateway's
         own registry + resolver); no key → skipped silently.
      4. QwenCloud — same shape, the qwen key.
      5. Ollama reachable at OLLAMA_URL and not VRAM-deferred (8c).
      6. OpenAI (requires OPENAI_API_KEY AND consent — either via the
         launcher Preferences app_state key, or via the ``force_api``
         operator flag, i.e. the caller's --force-api).
      7. Anthropic API direct (legacy fallback, uses ANTHROPIC_API_KEY).
      8. Skip — log a friendly line; the caller exits 0.

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

    forced, forced_via = _forced_backend_with_source(env_keys)
    if forced in VALID_BACKENDS:
        # Consent gate still applies to forced=openai (defense-in-depth:
        # an env var alone shouldn't bypass user consent; --force-api
        # is the explicit operator override).
        if forced == "openai" and not force_api and not openai_consent_granted():
            source = (f"{env_keys[0]}=openai" if forced_via == "env" else
                      "the launcher Preferences backend pin is openai")
            _log(
                f"  {label}: {source} but consent not "
                "granted. Set kg_summary_openai_consent=true in launcher "
                "Preferences → KG Summaries, or pass --force-api. "
                "Skipping for this run."
            )
            _BACKEND_CACHE["choice"] = "skip"
            return "skip"
        _BACKEND_CACHE["choice"] = forced
        _log(f"  {label} backend: {forced} (forced via {forced_via})")
        return forced

    # `_tier_selectable` is checked BEFORE each probe so a demoted CLI
    # never pays the 20 s smoke test again — the KG generator runs one
    # process per node, so that probe would otherwise fire once per node.
    if _tier_selectable("cli", label) and cli_available():
        _BACKEND_CACHE["choice"] = "cli"
        _log(f"  {label} backend: cli (claude on PATH, smoke-test OK)")
        return "cli"
    for vendor_id in CLOUD_VENDOR_TIERS:
        # Resolved through the per-tier probe functions (``zai_available`` /
        # ``qwen_available``) so a substituted or pinned probe governs
        # selection exactly as for every other tier.
        probe = {"zai": zai_available, "qwen": qwen_available}.get(vendor_id)
        if probe is None:
            continue
        if _tier_selectable(vendor_id, label) and probe():
            _BACKEND_CACHE["choice"] = vendor_id
            _log(f"  {label} backend: {vendor_id} "
                 f"({_vendor_summary_model(vendor_id)}; subscription key)")
            return vendor_id
    if _tier_selectable("ollama", label) and ollama_available():
        _BACKEND_CACHE["choice"] = "ollama"
        _log(f"  {label} backend: ollama ({ollama_default_model()})")
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
        tier for tier in ("cli", *CLOUD_VENDOR_TIERS, "ollama", "openai", "api")
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
            f"  {label}: no backend available (no claude CLI, no cloud-vendor "
            f"key (z.ai / QwenCloud), no Ollama at {OLLAMA_URL} or VRAM-"
            f"deferred, no OPENAI_API_KEY, no ANTHROPIC_API_KEY). Skipping."
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
    if backend in CLOUD_VENDOR_TIERS:
        call = {"zai": call_zai, "qwen": call_qwen}[backend]
        return call(prompt)
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
    forbid: "tuple[str, ...] | frozenset[str]" = (),
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

    ``forbid`` (v0.2.103 R2, M-1): tier ids this PROMPT must not descend
    to, whatever the ladder's health. A batch envelope up to
    ``BATCH_CHAR_BUDGET`` (~48k chars ≈ 12-16k tokens) does not fit the
    shared Ollama ``num_ctx`` (8 192) — sending it there would silently
    truncate the front of the prompt, lose the envelope instructions, fail
    the parse and re-run every entity. A caller holding such a prompt
    passes ``forbid=("ollama",)`` and handles the raise by SPLITTING to
    per-item calls (whose prompts fit the window); the tier itself is
    healthy and its breaker is NOT tripped.
    """
    forced = _forced_backend(env_keys) in VALID_BACKENDS
    tried: set = set()
    while True:
        backend = select_backend(force_api=force_api, env_keys=env_keys,
                                 label=label)
        if backend == "skip" or backend in tried:
            raise RuntimeError("no backend available")
        if backend in forbid:
            raise RuntimeError(
                f"{label}: backend '{backend}' is excluded for this prompt "
                "(its context window cannot hold it); the caller splits "
                "the work instead"
            )
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
            # classify_backend_failure) and NO descent to a lower tier (a
            # refusal answered by a weaker model would be a silent
            # substitution — owner ruling 2026-10-18). A REFUSAL-shaped
            # reply raises the ContentRefusal subclass so the KG generator
            # can flag the node; every other non-answer raises the plain
            # class. The caller must not cache either. The message reports
            # the reply's SHAPE (length), never the
            # reply: the callers LOG this line (generate-kg-summary.py
            # writes it to the project log) and model output on this path
            # quotes node content — the same rule the breaker's ``signal``
            # field follows.
            raise (ContentRefusal if is_content_refusal(text) else NonAnswerResponse)(
                f"{label}: backend '{backend}' returned a non-answer "
                f"({len((text or '').strip())} chars) — not cached"
            )
        return text


# ──────────────────────────────────────────────────────────────────────
# v0.2.100 WP-13 — the batch envelope ("N items, one call")
# ──────────────────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def build_batch_prompt(instructions: str, blocks: "list[tuple[str, str]]") -> str:
    """One prompt for several items: *instructions*, then each block under
    an ``### ITEM <id>`` header, then the reply contract (one JSON object
    keyed by item id). The ids are the caller's; keep them short and plain."""
    parts = [instructions.strip(), ""]
    for item_id, text in blocks:
        parts.append(f"### ITEM {item_id}")
        parts.append(text.strip())
        parts.append("")
    ids = ", ".join(f'"{item_id}"' for item_id, _ in blocks)
    parts.append(
        "Reply with ONLY one JSON object and nothing else (no prose, no code "
        f"fence). Its keys are exactly the item ids ({ids}); each value is "
        "the answer for that item in the shape described above."
    )
    return "\n".join(parts)


def parse_batch_reply(text: "str | None", ids) -> dict:
    """The answers in a batch reply, keyed by item id.

    Tolerates a code fence and prose around the object (the outermost
    ``{ … }`` is parsed). Returns ``{}`` when no JSON object can be read;
    ids the reply does not carry are simply absent — the caller falls back
    to one call per missing item, so a partial reply loses nothing.
    """
    raw = (text or "").strip()
    raw = _FENCE_RE.sub("", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    wanted = {str(i) for i in ids}
    return {k: v for k, v in data.items() if k in wanted}
