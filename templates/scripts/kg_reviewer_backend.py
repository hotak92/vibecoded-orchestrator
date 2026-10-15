#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The post-commit KG reviewer: backend chain, run and guard (v0.2.103).

The reviewer is the background ``claude -p`` session ``post-git-commit-kg-sync``
spawns after every successful commit. The hook pair is a thin wrapper; this
script owns everything else (one home): the commit capture and prompt
(:mod:`vco_lib.kg_reviewer_prompt`), the model choice and fallback chain
(below + :mod:`vco_lib.kg_reviewer_model`), and the hard boundaries around a
run (:mod:`vco_lib.kg_reviewer_guard`: snapshot, post-run validator, isolated
``kg-sync:`` commit).

Model chain (owner rulings 2026-10-11, revised 2026-10-15):

1. the CHOSEN Claude model — ONE machine-wide setting, default ``sonnet``
   (Haiku / Sonnet / Opus): ``VCO_KG_REVIEW_MODEL``, else launcher
   Preferences -> KG commit reviewer, else the default. Unavailable = no
   ``claude`` CLI, or its tier is breaker-latched for a REAL signal (quota /
   429 / auth), recorded by the ``record`` subcommand below;
2. a gateway model of COMPARABLE capability — ``[fallback] gateway_rungs``
   in ``vco_lib/kg_reviewer_config.toml`` (GLM-5.3, then Kimi K3) — ONLY
   when the local model gateway actually serves it (the catalogue is ASKED
   via ``GET /v1/models``, authenticated with the daemon's host token), its
   tier is not latched, and the ``claude`` CLI the rung spawns is on PATH.
   Flash models are deliberately not rungs: too weak for this job, so the
   chain never degrades to one. The gateway spelling of each id is DERIVED
   from model_router's vendor table, never hand-written;
3. OpenAI — only when the user enabled OpenAI for SUMMARIES (the
   ``kg_summary_openai_consent`` gate from :mod:`summary_backends`); this
   rung is DEGRADED: an API call reviews the diff and proposes node edits
   into the log, but cannot write anything;
4. otherwise the review is SKIPPED with a log line.

The failure classification, the strike-gated breaker file and the OpenAI
consent/key resolution come from ``templates/scripts/summary_backends.py``.
Every skipped rung is LOGGED with its reason. The spawn env for a gateway
model carries the gateway's loopback host token (read from the token FILE
inside this process, exported only into the child's environment — never
argv, never a log line).

Usage (from the hook, both OS siblings)::

    kg_reviewer_backend.py select                 # JSON decision on stdout
    kg_reviewer_backend.py run --project-root R --commit C --log L [--max-turns N]
    kg_reviewer_backend.py record --model M --exit N --text-file T

The turn budget is ONE knob (W6-N F-4): :data:`DEFAULT_MAX_TURNS`,
overridden by ``VCO_KG_REVIEW_MAX_TURNS`` (documented in
``docs/CONFIGURATION.md``) and read through :func:`resolve_max_turns` for
every rung's spawn; ``--max-turns`` is an explicit per-run override.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
for _candidate in (
    os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip(),
    os.environ.get("VCT_INSTALL_ROOT", "").strip(),
    str(_HERE.parent.parent),
):
    if not _candidate:
        continue
    _root = Path(_candidate)
    if (_root / "claude_mcp_servers").is_dir():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break

import summary_backends as _sb  # noqa: E402 — sibling in .claude/scripts/

# vco_lib is a shipped dependency (the orchestrator venv imports it from the
# checkout): failing to import it is a BROKEN install, said loudly — never a
# silent fallback to an unguarded reviewer.
try:
    from vco_lib import kg_reviewer_guard as _guard  # noqa: E402
    from vco_lib import kg_reviewer_model as _model  # noqa: E402
    from vco_lib import kg_reviewer_prompt as _prompt  # noqa: E402
except ImportError as _exc:  # pragma: no cover — exercised by the hook tests
    print(f"kg_reviewer_backend: BROKEN INSTALL - vco_lib is not importable "
          f"({_exc}); the KG reviewer cannot run its guarded review. Repair "
          f"it with the orchestrator update.", file=sys.stderr)
    raise SystemExit(3)

#: The fallback rungs as ``(vendor row id, bare model id)`` — from the
#: shared rule table (``[fallback] gateway_rungs``), non-flash models of
#: comparable capability only (owner ruling 2026-10-15). The id the gateway
#: publishes is DERIVED (:func:`gateway_rungs`) from model_router's vendor
#: table, the one home of every gateway namespace.
GATEWAY_RUNG_MODELS: "tuple[tuple[str, str], ...]" = _model.gateway_rung_models()

#: The reviewer's turn budget — ONE knob for every rung's spawn (W6-N F-4).
#: 10 (the pre-W6-N value) truncated 46 of 89 logged runs in the maintainer
#: tree, 4 of 6 and 2 of 3 in two other projects ("Reached max turns (10)"),
#: so a typical review needs more than 10: search per touched area, read the
#: node, write it, and the same again for a doc. 30 gives the median review
#: two to three times the turns the truncated runs were cut at, and still
#: fits the 900 s timeout at ~20 s a turn. Every run now logs its turn count
#: (``turns=`` in the run line) so the next change to this number is read
#: from data. Overridden by :data:`MAX_TURNS_ENV`; the hooks' no-python
#: branch mirrors this default ("must match" comment there).
DEFAULT_MAX_TURNS = 30
MAX_TURNS_ENV = "VCO_KG_REVIEW_MAX_TURNS"
_DIGITS_RE = re.compile(r"[0-9]+")
_INT64_MAX = 2 ** 63 - 1

#: The wall-clock bound of one reviewer run (``--timeout-s`` default).
DEFAULT_TIMEOUT_S = 900


def resolve_max_turns(explicit: "Optional[int]" = None) -> int:
    """The turn budget for one run: ``explicit`` (``--max-turns``) when
    positive, else :data:`MAX_TURNS_ENV`, else :data:`DEFAULT_MAX_TURNS`.

    A non-positive or unparseable env value falls back to the default
    rather than becoming a 0-turn kill switch (the rule
    ``VCO_PROMPT_MIN_CHARS`` follows)."""
    if explicit is not None and explicit > 0:
        return explicit
    # Plain ASCII digits only, unstripped, within int64 — the grammar the
    # hooks' no-python mirrors accept (``.sh``: ``*[!0-9]*`` + ``-gt 0``;
    # ``.ps1``: ``^[0-9]+$`` + ``[int64]::TryParse``). ``int()`` alone would
    # also take ``+5``, `` 5 `` and ``1_000``, which the mirrors refuse;
    # a parity test pins all three (W6-N review LOW-5).
    raw = os.environ.get(MAX_TURNS_ENV, "")
    if not _DIGITS_RE.fullmatch(raw):
        return DEFAULT_MAX_TURNS
    value = int(raw)
    return value if 0 < value <= _INT64_MAX else DEFAULT_MAX_TURNS


def _model_router() -> "tuple[Any, Any, Any]":
    """``(VENDORS, advertised_id, strip_1m)`` from model_router — the one
    home for gateway id spelling. Raises ImportError on a broken install
    (the caller logs it as such)."""
    from claude_mcp_servers.model_router.routing import (
        advertised_id, strip_1m)
    from claude_mcp_servers.model_router.vendors import VENDORS
    return VENDORS, advertised_id, strip_1m


def gateway_rungs() -> "tuple[str, ...]":
    """The rung ids as the gateway spells them WITHOUT the window suffix
    (``claude-gw/qwen/qwen3.8-flash``) — derived, never hand-written."""
    vendors, advertised_id, _strip = _model_router()
    return tuple(advertised_id(vendors[vendor_id], bare, False)
                 for vendor_id, bare in GATEWAY_RUNG_MODELS)


def _served_id(rung: str, ids: frozenset) -> "Optional[str]":
    """The catalogue id that serves ``rung``, or None.

    The ``[1m]`` suffix is a CLIENT context hint (the gateway strips it
    before routing), and by default the gateway publishes a 1M model ONLY
    as ``<id>[1m]`` — so membership compares ids with the hint stripped.
    The LISTED spelling is returned so the spawn uses exactly what the
    gateway publishes; with both spellings listed the ``[1m]`` row wins,
    as in the shipped gateway agents' ``model:`` lines."""
    _v, _a, strip_1m = _model_router()
    matches = sorted((i for i in ids if strip_1m(i) == rung),
                     key=lambda i: (i == rung, i))
    return matches[0] if matches else None


def _tier_for(model: str) -> str:
    """Breaker tier key (``kg_reviewer:<model>``). A gateway model keys on
    its id WITHOUT the ``[1m]`` hint, so a change of published spelling
    cannot split one model's breaker in two."""
    if model in _model.model_choices() or model == "openai":
        return f"kg_reviewer:{model}"
    _v, _a, strip_1m = _model_router()
    return f"kg_reviewer:{strip_1m(model)}"


def _log(line: str) -> None:
    print(f"[kg-reviewer] {line}", file=sys.stderr)


def _gateway_token() -> str:
    """Read the local model gateway's loopback host token.

    ONE home for the read (W6-K review F1, 2026-10-11): the daemon's
    owner-only token FILE via ``model_router.config.token_path`` — the
    same read :func:`_gateway_auth_env` performs for the spawn env and
    this module now also uses to AUTHENTICATE the catalogue probe (the
    daemon 401s a tokenless ``GET /v1/models`` on every route).

    Raises on any failure (missing module, missing/unreadable/empty
    file); callers that can proceed without a token catch and degrade.
    """
    from claude_mcp_servers.model_router.config import token_path

    token = token_path().read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("the model gateway token file is empty")
    return token


def gateway_catalogue(
    timeout_s: float = 2.0,
) -> "tuple[Optional[str], frozenset, Optional[str]]":
    """Ask the local model gateway which models it serves.

    Returns ``(base_url, ids, refusal)`` — the probed base URL (None when
    no gateway answered), the union catalogue's model ids (empty on any
    failure), and a refusal description when a gateway ANSWERED but
    refused the probe (``HTTP 401``/``403`` — distinct from "no gateway
    answered", so the skip log line stays honest). Availability is
    EVIDENCE from the running daemon, never an assumption: the owner's
    ruling says "only those the local model gateway can actually serve".
    """
    try:
        from vco_lib.vscode_settings import resolve_gateway_ports
        ports = resolve_gateway_ports()
    except Exception:  # noqa: BLE001 — a missing vco_lib is "no gateway"
        return None, frozenset(), None
    try:
        headers = {"Authorization": f"Bearer {_gateway_token()}"}
    except Exception:  # noqa: BLE001 — probe anyway; ours then 401s and is
        headers = {}  # reported as a refusal, never as "no gateway"
    refusal: Optional[str] = None
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        request = urllib.request.Request(f"{base}/v1/models", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as exc:
            # The gateway answered and refused: remember it, try the next
            # port (a later port answering wins over an earlier refusal).
            refusal = f"HTTP {exc.code} from {base}"
            continue
        except Exception:  # noqa: BLE001 — unreachable port: try the next
            continue
        ids = frozenset(
            str(m.get("id") or "")
            for m in (payload.get("data") or payload.get("models") or [])
            if isinstance(m, dict)
        )
        return base, ids, None
    return None, frozenset(), refusal


def _claude_model_available(model: str, log: Callable[[str], None]) -> bool:
    if shutil.which("claude") is None:
        log(f"rung {model}: unavailable (no `claude` CLI on PATH)")
        return False
    latched = _sb.breaker_state(_tier_for(model))
    if latched:
        log(f"rung {model}: latched ({latched.get('reason')}), "
            f"{latched.get('remaining')}s remaining")
        return False
    return True


def _gateway_model_available(rung: str, base: Optional[str],
                             ids: frozenset,
                             log: Callable[[str], None],
                             refusal: Optional[str] = None) -> "Optional[str]":
    """The catalogue id to spawn for ``rung``, or None (with a log line)."""
    if base is None:
        if refusal:
            log(f"rung {rung}: unavailable (the local model gateway "
                f"refused the catalogue probe: {refusal})")
        else:
            log(f"rung {rung}: unavailable (no local model gateway answered)")
        return None
    served = _served_id(rung, ids)
    if served is None:
        log(f"rung {rung}: not served by the gateway catalogue "
            f"({len(ids)} ids) — skipped, availability is never assumed")
        return None
    latched = _sb.breaker_state(_tier_for(served))
    if latched:
        log(f"rung {served}: latched ({latched.get('reason')}), "
            f"{latched.get('remaining')}s remaining")
        return None
    return served


def _openai_available(log: Callable[[str], None]) -> bool:
    if not _sb.openai_consent_granted():
        log("rung openai: skipped (OpenAI for summaries not enabled — "
            "the kg_summary_openai_consent gate)")
        return False
    latched = _sb.breaker_state("kg_reviewer:openai")
    if latched:
        log(f"rung openai: latched ({latched.get('reason')}), "
            f"{latched.get('remaining')}s remaining")
        return False
    if not _sb.openai_available():
        log("rung openai: consent granted but no key resolves")
        return False
    return True


def select_reviewer_backend(
    log: Optional[Callable[[str], None]] = None,
    model: Optional[str] = None,
) -> "dict[str, Any]":
    """Walk the chain; return the decision dict for the hook.

    Shape: ``{"mode": "claude" | "openai" | "skip", "model": str,
    "base_url": str | None, "reason": str}``. A gateway rung additionally
    carries ``base_url`` (the run exports the auth token itself, from the
    gateway's token FILE — it never transits this process's stdout).
    ``model`` overrides the resolved choice (tests); production passes none.
    """
    log = log or _log
    if model is None:
        model, source = _model.resolve(log=log)
    else:
        source = "explicit"
    if _claude_model_available(model, log):
        return {"mode": "claude", "model": model, "base_url": None,
                "reason": f"chosen model ({source})"}
    try:
        rungs = gateway_rungs()
    except Exception as exc:  # noqa: BLE001 — a broken install, said so
        rungs = ()
        log(f"gateway rungs: unavailable (BROKEN INSTALL — model_router "
            f"is not importable: {exc})")
    if rungs and shutil.which("claude") is None:
        # W6-N F-2: every gateway rung spawns `claude -p` pointed at the
        # gateway, so without the CLI it would fail at spawn — skip each
        # with its line and let the walk reach the OpenAI rung.
        for rung in rungs:
            log(f"rung {rung}: unavailable (no `claude` CLI on PATH — the "
                f"gateway rungs spawn `claude -p`)")
        rungs = ()
    if rungs:
        base, ids, refusal = gateway_catalogue()
        for rung in rungs:
            served = _gateway_model_available(rung, base, ids, log, refusal)
            if served:
                return {"mode": "claude", "model": served, "base_url": base,
                        "reason": f"{model} unavailable; the gateway serves "
                                  f"this comparable model"}
    if _openai_available(log):
        return {"mode": "openai", "model": _sb._openai_model(),
                "base_url": None,
                "reason": "no claude/gateway rung available; OpenAI "
                          "summaries consent granted (degraded: review "
                          "only, no KG writes)"}
    log("every rung unavailable — commit not reviewed this time (no "
        "flash-model fallback, by design)")
    return {"mode": "skip", "model": "", "base_url": None,
            "reason": "no rung available"}


def _gateway_auth_env(base_url: str) -> "dict[str, str]":
    """The env that points a ``claude`` child at the local gateway.

    Mirrors what ``vco_lib.vscode_settings`` writes when the panel is
    pointed at the gateway (the ROUTING_KEYS set): base URL, the loopback
    host token (read from the daemon's owner-only token FILE here, passed
    as child ENV only), an explicitly emptied API key, and model discovery
    so the ``claude-gw/`` vendor ids resolve.
    """
    env: "dict[str, str]" = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    try:
        env["ANTHROPIC_AUTH_TOKEN"] = _gateway_token()
    except Exception as exc:  # noqa: BLE001 — no token, no gateway run
        raise RuntimeError(f"cannot read the model gateway token: {exc}") from exc
    return env


#: ``subtype`` of a ``claude -p --output-format json`` result whose run
#: ended on the CLIENT's ``--max-turns`` budget (Agent SDK
#: ``SDKResultMessage``; it carries no ``result`` field).
MAX_TURNS_SUBTYPE = "error_max_turns"
#: The text-mode equivalent (an older CLI, or a ``record`` text file).
_TEXT_MAX_TURNS_RE = re.compile(r"(?m)^Error: Reached max turns \(\d+\)\s*$")


def parse_run_result(stdout: str) -> "Optional[dict]":
    """The run's result object, or None when the output carries none (an
    older CLI, a crash, text mode). Accepts ``--output-format json`` (one
    object) and ``stream-json`` (one event per line; the LAST
    ``type == "result"`` event is that same object)."""
    try:
        result = json.loads(stdout or "")
    except ValueError:
        result = None
    if isinstance(result, dict):
        return result
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def run_failure_evidence(stdout: str, stderr: str) -> "tuple[bool, str]":
    """``(served, text)`` for one finished reviewer run.

    ``served`` is True when the run ended on the client's own turn budget
    (``subtype == "error_max_turns"``, or the text-mode "Reached max turns"
    line): the backend answered every turn, so it is not a backend failure
    — no strike, no latch (W6-N review HIGH-1).

    ``text`` is what the breaker may classify: the parsed ``result`` string
    plus stderr, NEVER the raw JSON. The result object's ``duration_ms``,
    token counts, cost and ``session_id`` carry ``401`` / ``429`` / ``503``
    digits that the substring classifier read as auth / rate-limit /
    capacity signals (about 1 failed run in 10 latched falsely). A
    non-JSON stdout (text mode) is the answer itself and is kept.
    """
    result = parse_run_result(stdout)
    if result is not None:
        if result.get("subtype") == MAX_TURNS_SUBTYPE:
            return True, ""
        answer = result.get("result")
        text = (answer if isinstance(answer, str) else "") + "\n" + (stderr or "")
    else:
        text = (stdout or "") + (stderr or "")
        if _TEXT_MAX_TURNS_RE.search(text):
            return True, ""
    return False, text.strip()[-4000:]


def _record_outcome(model: str, exit_code: "Optional[int]", text: str,
                    log: Callable[[str], None], *,
                    served: bool = False) -> None:
    """Classify the finished run and latch/clear its tier (one home:
    summary_backends' breaker — the same real-signal machinery the summary
    ladder uses). ``served`` (see :func:`run_failure_evidence`) marks a run
    the backend answered in full that ended on the client's turn budget: it
    clears the tier like a success instead of striking it."""
    tier = _tier_for(model)
    if exit_code == 0 or served:
        _sb.clear_backend_trip(tier)
        if exit_code != 0:
            log(f"run on {model} hit its turn budget (exit={exit_code}) — "
                f"not a backend failure; raise {MAX_TURNS_ENV} if this "
                f"recurs")
        return
    reason, signal = _sb.classify_backend_failure(text=text)
    tripped = _sb.trip_backend(tier, reason, signal=signal)
    log(f"run on {model} failed (exit={exit_code}, reason={reason}"
        f"{'/' + signal if signal else ''})"
        + (" — tier latched; the next commit falls back" if tripped else ""))


def _cmd_select() -> int:
    print(json.dumps(select_reviewer_backend()))
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    raw = ""
    try:
        raw = Path(args.text_file).read_text(encoding="utf-8",
                                             errors="replace")
    except OSError:
        pass
    served, text = run_failure_evidence(raw, "")
    _record_outcome(args.model, args.exit, text, _log, served=served)
    return 0


#: Tools the reviewer may use. Write/Edit carry a PATH-scoped permission rule
#: (Claude Code permission-rule syntax: a pattern without a leading ``/`` is
#: relative to the working directory, which :func:`_cmd_run` pins to the
#: project root), so the CLI itself refuses a write outside ``knowledge/``.
#: The post-run validator (:mod:`vco_lib.kg_reviewer_guard`) is the second,
#: independent layer.
#:
#: ``store_knowledge_node`` is deliberately NOT allowed (v0.2.103 R7): its
#: ``file_path`` can point anywhere (or at the shared KG) and the bytes it
#: writes cannot be replayed from the transcript, so the guard could not
#: prove which bytes are the reviewer's — and the guard never reverts or
#: commits bytes it cannot prove. Write/Edit carry their content, and the
#: project's PostToolUse KG sync embeds every knowledge/ write they make.
ALLOWED_TOOLS: "tuple[str, ...]" = (
    "Read", "Glob", "Grep",
    "mcp__weaviate-kg__hybrid_search",
    "Edit(knowledge/**)", "Write(knowledge/**)",
)
#: Explicit denials (deny rules win over any allow rule a project's own
#: settings grant): no shell, no notebook/web/agent tools, no KG store tool,
#: and no edits of the paths an earlier reviewer damaged or that git owns.
DISALLOWED_TOOLS: "tuple[str, ...]" = (
    "Bash", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
    "MultiEdit", "mcp__weaviate-kg__store_knowledge_node",
    "Edit(docs/**)", "Write(docs/**)",
    "Edit(.claude/**)", "Write(.claude/**)",
    "Edit(.git/**)", "Write(.git/**)",
)


def build_claude_cmd(model: str, max_turns: int) -> "list[str]":
    """The reviewer's ``claude -p`` argv (the prompt rides on stdin).

    ``--permission-mode dontAsk`` overrides a project's ``defaultMode`` (an
    ``acceptEdits`` / ``bypassPermissions`` default would otherwise
    auto-approve edits anywhere): every tool not pre-approved is denied
    without a prompt — the right mode for a session nobody watches (the
    CLI's choices are acceptEdits / auto / bypassPermissions / manual /
    dontAsk / plan; there is no ``default``). ``stream-json`` (which ``-p`` only emits
    with ``--verbose``) carries every tool call, which is how the guard
    attributes the writes to the reviewer; its last event is the same result
    object ``--output-format json`` prints, so the turn count is still read.
    """
    return [
        "claude", "-p",
        "--model", model,
        "--max-turns", str(max_turns),
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
    ]


def _file_logger(log_path: str) -> Callable[[str], None]:
    """Append ``[kg-reviewer <utc>] <line>`` to the reviewer log (the hook
    runs detached, so the log is where a user finds what happened)."""
    def _write(line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[kg-reviewer {stamp}] {line}\n")
        except OSError:
            print(f"[kg-reviewer] {line}", file=sys.stderr)
    return _write


def _project_root(raw: "Optional[str]") -> Path:
    if raw:
        return Path(raw).resolve()
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    top = proc.stdout.strip()
    return Path(top).resolve() if proc.returncode == 0 and top else Path.cwd()


def _cmd_run(args: argparse.Namespace) -> int:
    log = _file_logger(args.log)
    root = _project_root(args.project_root)
    try:
        commit = _prompt.collect_commit(root, args.commit or "HEAD")
    except Exception as exc:  # noqa: BLE001 — nothing to review, said so
        log(f"cannot read commit {args.commit or 'HEAD'} in {root}: {exc}")
        return 1
    if _guard.is_reviewer_commit_subject(commit.subject):
        # Recursion guard: the reviewer never reviews its own kg-sync commit.
        log(f"{commit.short} is a reviewer commit ({commit.subject!r}) - "
            f"not reviewed")
        return 0
    decision = select_reviewer_backend(log=log)
    model = decision["model"]
    started = time.time()
    if decision["mode"] == "skip":
        return 0
    prompt = _prompt.build_prompt(commit, root)
    if decision["mode"] == "openai":
        try:
            answer = _sb.call_openai(
                prompt
                + "\n\nNOTE: you are the degraded OpenAI rung of the KG "
                  "commit reviewer — you cannot edit the knowledge graph. "
                  "Output your proposed KG node updates as markdown; a "
                  "later claude-enabled run (or the user) applies them."
            )
            exit_code, tail = 0, answer
        except Exception as exc:  # noqa: BLE001
            exit_code, tail = 1, str(exc)
        _record_outcome("openai", exit_code, tail, log)
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(f"\n[openai rung {started:.0f}] exit={exit_code}\n")
            if exit_code == 0:
                fh.write(tail + "\n")
        return exit_code

    env = dict(os.environ)
    if decision["base_url"]:
        env.update(_gateway_auth_env(decision["base_url"]))
    # The prompt rides on STDIN, never argv (the Windows `claude` shim is a
    # .cmd re-parsed by the shell, the prompt embeds `git diff` output whose
    # `---` tokens would parse as options, and the command line is capped at
    # 32 767 chars there).
    cmd = build_claude_cmd(model, resolve_max_turns(args.max_turns))
    snap = _guard.take_snapshot(root)
    try:
        try:
            proc = subprocess.run(cmd, input=prompt, env=env, cwd=root,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=args.timeout_s)
            stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
            served, evidence = run_failure_evidence(stdout, stderr)
            _record_outcome(model, exit_code, evidence, log, served=served)
            with open(args.log, "ab") as fh:
                fh.write(_run_log_text(model, exit_code, stdout, started)
                         .encode("utf-8", errors="replace"))
                if stderr:
                    fh.write(stderr.encode("utf-8", errors="replace"))
        except subprocess.TimeoutExpired as exc:
            # A hung reviewer must not dump a raw traceback into the detached
            # log — classify it ("timeout" is a capacity strike), log it,
            # exit 124. The breaker reads only "timeout" plus the parsed
            # evidence, never str(exc) (its argv echo carries digits the
            # substring classifier could misread, W6-N review HIGH-1).
            def _text(maybe: "Any") -> str:
                if isinstance(maybe, bytes):
                    return maybe.decode("utf-8", errors="replace")
                return maybe or ""

            stdout, err = _text(exc.stdout), _text(exc.stderr)
            _served, evidence = run_failure_evidence(stdout, err)
            _record_outcome(model, 124, f"timeout\n{evidence}", log)
            exit_code = 124
            log(f"TIMED OUT after {args.timeout_s}s on {model}; its completed "
                f"writes are still validated below")
        # Every completed write is judged, whatever the exit: each Write/Edit
        # call lands whole, and leaving it unjudged in the tree is the worse
        # outcome (it is what `git commit -a` swept up before).
        writes = _guard.attributed_writes(stdout, root)
        if writes is None:
            log("writes not attributable (no stream-json events in the "
                "output)")
        # What the reviewer read verbatim: its Read/Grep results plus the
        # commit stat and diff it was handed (a flag quoted from the diff is
        # sourced too).
        sources = "\n".join((_guard.transcript_sources(stdout), commit.stat,
                             commit.diff))
        result = _guard.validate(snap, writes, log,
                                 resync=_guard.resync_with(root, log),
                                 sources=sources)
        if writes is not None:
            _guard.commit_reviewer_changes(
                snap, result.kept, reviewer_bytes=result.reviewer_bytes,
                short=commit.short, subject=commit.subject, model=model,
                attributed=True, log=log)
        else:
            log("commit skipped: the run's writes could not be attributed "
                "to the reviewer")
    finally:
        snap.cleanup()
    return exit_code


def _run_log_text(model: str, exit_code: int, stdout: str,
                  started: float) -> str:
    """The log text of one finished ``claude -p --output-format json`` run:
    the answer, then one ``[kg-reviewer <ts>] model=… turns=N`` line — the
    per-run turn count the budget is tuned from. Output that is not the
    JSON result object (an older CLI, a crash) is logged verbatim with
    ``turns=?``."""
    result = parse_run_result(stdout)
    if result is None:
        return (f"{stdout}\n[kg-reviewer {started:.0f}] model={model} "
                f"exit={exit_code} turns=?\n")
    answer = result.get("result")
    body = answer if isinstance(answer, str) else ""
    return (f"{body}\n[kg-reviewer {started:.0f}] model={model} "
            f"exit={exit_code} turns={result.get('num_turns', '?')} "
            f"subtype={result.get('subtype', '?')}\n")




def main(argv: "Optional[list[str]]" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kg_reviewer_backend.py",
        description="Post-commit KG reviewer: backend chain, guarded run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select", help="print the chain decision as JSON")
    p_run = sub.add_parser("run", help="review one commit: select, run, "
                                       "validate, commit the KG nodes")
    p_run.add_argument("--project-root", default=None,
                       help="the project (default: the git top level of "
                            "the working directory)")
    p_run.add_argument("--commit", default=None,
                       help="the commit to review (default: HEAD)")
    p_run.add_argument("--log", required=True)
    p_run.add_argument("--max-turns", type=int, default=None,
                       help=f"turn budget (default: ${MAX_TURNS_ENV}, "
                            f"else {DEFAULT_MAX_TURNS})")
    p_run.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p_rec = sub.add_parser("record", help="record a finished run's outcome")
    p_rec.add_argument("--model", required=True)
    p_rec.add_argument("--exit", type=int, required=True)
    p_rec.add_argument("--text-file", default="")
    args = ap.parse_args(argv)
    if args.cmd == "select":
        return _cmd_select()
    if args.cmd == "record":
        return _cmd_record(args)
    return _cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
