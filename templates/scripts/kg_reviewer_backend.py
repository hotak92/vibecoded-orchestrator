#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Model/backend selection for the post-commit KG reviewer (v0.2.102 W6-K).

The reviewer is the background ``claude -p`` session ``post-git-commit-kg-sync``
spawns after every successful commit. It ran Haiku unconditionally; when
Haiku's tokens were exhausted the spawn failed silently into
``kg-commit-review.log`` and the commit went un-reviewed.

Owner ruling 2026-10-11 — keep it, and fall back, in this order:

1. ``haiku`` via the local ``claude`` CLI (the default; unavailable = the
   CLI is absent, or its tier is breaker-latched for a REAL signal —
   quota / 429 / auth — recorded by the ``record`` subcommand below);
2. GLM-5.3-Flash (z.ai) → DeepSeek-V4.1-Flash (QwenCloud) →
   Qwen3.8-Flash (QwenCloud) — each ONLY when the local model gateway
   actually serves it (the catalogue is ASKED via ``GET /v1/models``,
   authenticated with the daemon's host token; availability is never
   hard-coded), its tier is not latched, and the ``claude`` CLI the rung
   spawns is on PATH. The gateway spelling of each id
   (``claude-gw/glm-5.3-flash[1m]``, ``claude-gw/qwen/qwen3.8-flash[1m]``
   …) is DERIVED from model_router's vendor table, never hand-written
   (W6-N F-1: a hand-written ``claude-gw/qwen3.8-flash`` never matched);
3. OpenAI — only when the user enabled OpenAI for SUMMARIES (the
   ``kg_summary_openai_consent`` gate from :mod:`summary_backends`); this
   rung is DEGRADED: an API call reviews the diff and proposes node edits
   into the log, but cannot use the weaviate-kg MCP tools to apply them.

One chain, one home: the failure classification, the strike-gated breaker
file and the OpenAI consent/key resolution all come from
``templates/scripts/summary_backends.py`` — this module only adds the
reviewer's rung table and the gateway catalogue probe.

Every skipped rung is LOGGED with its reason (absent / not served by the
gateway / latched <reason>). The spawn env for a gateway model carries the
gateway's loopback host token (read from the token FILE inside this
process, exported only into the child's environment — never argv, never a
log line).

Usage (from the hook, both OS siblings)::

    kg_reviewer_backend.py select                 # JSON decision on stdout
    kg_reviewer_backend.py run --prompt-file P --log L [--max-turns N]
    kg_reviewer_backend.py record --model M --exit N --text-file T

``run`` selects, executes and records the outcome in one process so the
hook stays a thin wrapper (no JSON parsing in shell).

The turn budget is ONE knob (W6-N F-4): :data:`DEFAULT_MAX_TURNS`,
overridden by ``VCO_KG_REVIEW_MAX_TURNS`` (documented in
``docs/CONFIGURATION.md``) and read through :func:`resolve_max_turns` for
every rung's spawn; ``--max-turns`` is an explicit per-run override. The
hooks' broken-install branch (no python) mirrors the default.
"""
from __future__ import annotations

import argparse
import json
import os
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

#: The fallback rungs (owner order, 2026-10-11) as ``(vendor row id, bare
#: model id)`` — the vendor-neutral facts. The id the gateway publishes is
#: DERIVED (:func:`gateway_rungs`) from model_router's vendor table, the one
#: home of every gateway namespace: z.ai rides ``claude-gw/``, QwenCloud
#: ``claude-gw/qwen/``. W6-N F-1 (2026-10-12): the W6-K table hand-wrote
#: ``claude-gw/deepseek-v4.1-flash`` / ``claude-gw/qwen3.8-flash`` and
#: compared without the ``[1m]`` window spelling, so no rung ever matched
#: the live catalogue. Only these three flash models — the ruling names
#: them, and the catalogue check keeps the chain honest when the gateway
#: cannot serve one.
GATEWAY_RUNG_MODELS: "tuple[tuple[str, str], ...]" = (
    ("zai", "glm-5.3-flash"),
    ("qwen", "deepseek-v4.1-flash"),
    ("qwen", "qwen3.8-flash"),
)

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
    raw = os.environ.get(MAX_TURNS_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TURNS
    return value if value > 0 else DEFAULT_MAX_TURNS


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


_TIER_HAIKU = "kg_reviewer:haiku"


def _tier_for(model: str) -> str:
    """Breaker tier key. A gateway model keys on its id WITHOUT the
    ``[1m]`` hint, so a change of published spelling cannot split one
    model's breaker in two."""
    if model == "haiku":
        return _TIER_HAIKU
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


def _haiku_available(log: Callable[[str], None]) -> bool:
    if shutil.which("claude") is None:
        log("rung haiku: unavailable (no `claude` CLI on PATH)")
        return False
    latched = _sb.breaker_state(_TIER_HAIKU)
    if latched:
        log(f"rung haiku: latched ({latched.get('reason')}), "
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
) -> "dict[str, Any]":
    """Walk the chain; return the decision dict for the hook.

    Shape: ``{"mode": "claude" | "openai" | "skip", "model": str,
    "base_url": str | None, "reason": str}``. A gateway rung additionally
    carries ``base_url`` (the hook exports the auth token itself, from the
    gateway's token FILE — it never transits this process's stdout).
    """
    log = log or _log
    if _haiku_available(log):
        return {"mode": "claude", "model": "haiku", "base_url": None,
                "reason": "default"}
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
                        "reason": "haiku unavailable; gateway serves this "
                                  "model"}
    if _openai_available(log):
        return {"mode": "openai", "model": _sb._openai_model(),
                "base_url": None,
                "reason": "no claude/gateway rung available; OpenAI "
                          "summaries consent granted (degraded: review "
                          "only, no KG writes)"}
    log("every rung unavailable — commit not reviewed this time")
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


def _record_outcome(model: str, exit_code: "Optional[int]", text: str,
                    log: Callable[[str], None]) -> None:
    """Classify the finished run and latch/clear its tier (one home:
    summary_backends' breaker — the same real-signal machinery the summary
    ladder uses)."""
    tier = _tier_for(model) if model != "openai" else "kg_reviewer:openai"
    if exit_code == 0:
        _sb.clear_backend_trip(tier)
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
    text = ""
    try:
        text = Path(args.text_file).read_text(encoding="utf-8",
                                              errors="replace")[-4000:]
    except OSError:
        pass
    _record_outcome(args.model, args.exit, text, _log)
    return 0


def _consume_prompt_file(path: str) -> str:
    """Read the reviewer prompt file and unlink it once read.

    W6-K review F2 (2026-10-11): the ``.sh`` sibling stages the prompt
    through ``mktemp`` and nothing else ever removed it — one leaked file
    per commit until the tmp reaper. The consumer owns the cleanup: read
    into memory, then best-effort unlink (the ``.ps1`` sibling's log-dir
    staging file is consumed the same way; it is rewritten per commit).
    """
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    try:
        file.unlink()
    except OSError:
        pass  # best-effort: a leftover staging file never fails the review
    return text


def _cmd_run(args: argparse.Namespace) -> int:
    prompt = _consume_prompt_file(args.prompt_file)
    decision = select_reviewer_backend()
    model = decision["model"]
    started = time.time()
    if decision["mode"] == "skip":
        return 0
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
        _record_outcome("openai", exit_code, tail, _log)
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(f"\n[openai rung {started:.0f}] exit={exit_code}\n")
            if exit_code == 0:
                fh.write(tail)
        return exit_code

    env = dict(os.environ)
    if decision["base_url"]:
        env.update(_gateway_auth_env(decision["base_url"]))
    # The prompt rides on STDIN, never argv (the same shape
    # summary_backends.call_cli adopted): the Windows `claude` shim is a
    # .cmd re-parsed by the shell, and the prompt embeds `git diff` output
    # whose `---` tokens would be parsed as options; the command line is
    # also capped at 32 767 chars there. ``claude -p`` with no prompt
    # argument reads the prompt from stdin.
    # ``--output-format json`` so the run line can record the turns used
    # (W6-N F-4: the text output carried no count, so the turn budget could
    # not be read from the logs); the answer text is unpacked for the log.
    cmd = [
        "claude", "-p",
        "--model", model,
        "--max-turns", str(resolve_max_turns(args.max_turns)),
        "--output-format", "json",
        "--no-session-persistence",
        "--allowedTools",
        "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,"
        "mcp__weaviate-kg__store_knowledge_node,Write,Edit",
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, env=env, capture_output=True,
                              text=True, timeout=args.timeout_s)
    except subprocess.TimeoutExpired as exc:
        # W6-K review F4 (2026-10-11): a hung reviewer must not dump a raw
        # TimeoutExpired traceback into the detached log — classify it
        # (summary_backends reads "timeout" as a capacity strike), log it
        # and exit non-zero (124, the conventional timeout code).
        def _text(maybe: "Any") -> str:
            if isinstance(maybe, bytes):
                return maybe.decode("utf-8", errors="replace")
            return maybe or ""

        tail = (f"timeout after {args.timeout_s}s\n{exc}\n"
                + _text(exc.stdout) + _text(exc.stderr))[-4000:]
        _record_outcome(model, 124, tail, _log)
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(f"\n[kg-reviewer {started:.0f}] TIMED OUT after "
                     f"{args.timeout_s}s on {model}\n{tail}\n")
        return 124
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    _record_outcome(model, proc.returncode, tail, _log)
    with open(args.log, "ab") as fh:
        fh.write(_run_log_text(model, proc.returncode, proc.stdout or "",
                               started).encode("utf-8", errors="replace"))
        if proc.stderr:
            fh.write(proc.stderr.encode("utf-8", errors="replace"))
    return proc.returncode


def _run_log_text(model: str, exit_code: int, stdout: str,
                  started: float) -> str:
    """The log text of one finished ``claude -p --output-format json`` run:
    the answer, then one ``[kg-reviewer <ts>] model=… turns=N`` line — the
    per-run turn count the budget is tuned from. Output that is not the
    JSON result object (an older CLI, a crash) is logged verbatim with
    ``turns=?``."""
    try:
        result = json.loads(stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict):
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
        description="Post-commit KG reviewer backend chain (v0.2.102 W6-K)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select", help="print the chain decision as JSON")
    p_run = sub.add_parser("run", help="select + execute + record")
    p_run.add_argument("--prompt-file", required=True)
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
