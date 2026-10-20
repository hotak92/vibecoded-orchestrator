# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Run one bounded headless Claude Code task on the Claude Console API key.

``python -m vco_lib.claude_api_run --prompt-file review.md --report out.md``

Typical use: a final code review with Fable, billed to Console API credits
instead of the Claude subscription. The run is an ordinary headless
``claude -p`` in ``--cwd`` (the user's MCP servers, hooks and project
settings stay in force), with three differences:

* **Billing.** The environment is built by :mod:`vco_lib.claude_cli_api_key`
  (the one home of "env for a ``claude -p`` on the API key"): the inherited
  ``CLAUDE_CODE_ENTRYPOINT`` is dropped (with ``claude-vscode`` the CLI
  ignores the key and stays on the login), VCO's own gateway routing is
  dropped so the run talks to ``api.anthropic.com``, and the key is set as
  ``ANTHROPIC_API_KEY`` in the CHILD's environment only. Any other routing
  (a cloud-provider switch, a foreign ``ANTHROPIC_BASE_URL``, the user's own
  ``ANTHROPIC_AUTH_TOKEN``) refuses the run before anything is spawned, as
  does a key that does not resolve (the message names the key, never its
  value).
* **Bounds.** ``--max-budget-usd`` is ALWAYS passed (default
  :data:`DEFAULT_MAX_BUDGET_USD`); ``--max-turns`` when given.
* **Tool policy** (:func:`tool_flags`). ``review`` (default): the built-in
  tools are limited to ``Read``, ``Grep``, ``Glob`` and ``Bash``; Bash is
  pre-approved only for read-only git (``diff``, ``log``, ``show``,
  ``status``, ``blame``, ``ls-files``, ``rev-parse``, ``merge-base``) and
  VCO's read-only KG / code-graph search tools are pre-approved;
  git's mutating subcommands and ``rm`` are denied outright (deny beats any
  allow rule in the user's settings); ``--permission-mode dontAsk`` refuses
  everything else instead of prompting. The model has NO write tool: the
  report file is written by this module from the run's final answer.
  ``edit``: the full built-in set with ``--permission-mode acceptEdits``
  (file edits in ``--cwd`` allowed; Bash still limited to the same
  read-only git rules plus the user's own allow rules) — for a bounded task
  that has to change files.

After the run a cost summary is printed (total $, per-model tokens incl.
cache read/write and $, turns, duration, the API error if any) and one JSON
line is appended to :data:`LEDGER_FILENAME` under the VCO metrics dir
(:func:`vco_lib.paths.vct_metrics_dir`, ``$VCT_STATE_DIR``-aware). The
ledger never carries the prompt or the key; on a FAILED run its ``error``
field holds one short line (at most 300 characters of the CLI's error result,
or the last 3 stderr lines when the output could not be parsed), key-scrubbed.

Exit codes: 0 the run answered without error; 1 the run failed (API error,
``is_error``, unparsable output, non-zero CLI exit); 2 usage; 3 refused
before spawning (blocker, key missing, no ``claude`` on PATH, unreadable
prompt file).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from vco_lib import claude_cli_api_key as _cak

DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_EFFORT = "high"
#: Conservative ceiling for one run; always passed to the CLI.
DEFAULT_MAX_BUDGET_USD = 25.0
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")
POLICY_CHOICES = ("review", "edit")
#: Ledger file under :func:`vco_lib.paths.vct_metrics_dir`.
LEDGER_FILENAME = "claude_api_runs.jsonl"

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_REFUSED = 3

#: Built-in tools available under the ``review`` policy.
REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash")

#: Read-only git subcommands pre-approved for Bash (both policies).
_READ_ONLY_GIT = (
    "diff", "log", "show", "status", "blame", "ls-files", "rev-parse",
    "merge-base",
)

#: Denied outright (deny rules win over any allow rule in user settings).
_DENIED_GIT = (
    "push", "commit", "reset", "checkout", "switch", "restore", "stash",
    "clean", "rebase", "merge", "cherry-pick", "revert", "am", "apply",
    "tag", "branch -d", "branch -D", "worktree", "config", "rm", "mv",
)


#: VCO's bundled read-only search tools (the weaviate-kg MCP server), so a
#: reviewer can consult the KG / code graph under ``dontAsk``.
_READ_ONLY_MCP = (
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
    "mcp__weaviate-kg__search_code_graph",
    "mcp__weaviate-kg__query_code_structure",
)


def _allowed_rules() -> list[str]:
    rules = ["Read", "Grep", "Glob", *_READ_ONLY_MCP]
    for sub in _READ_ONLY_GIT:
        rules.append(f"Bash(git {sub})")
        rules.append(f"Bash(git {sub} *)")
    return rules


def _denied_rules(policy: str) -> list[str]:
    rules = [f"Bash(git {sub} *)" for sub in _DENIED_GIT]
    rules += [f"Bash(git {sub})" for sub in _DENIED_GIT]
    rules += ["Bash(rm *)", "Bash(rm)"]
    if policy == "review":
        rules += ["Edit", "Write", "NotebookEdit"]
    return rules


def tool_flags(policy: str) -> list[str]:
    """The CLI flags implementing *policy* (see the module docstring)."""
    if policy not in POLICY_CHOICES:
        raise ValueError(f"unknown policy {policy!r}")
    flags: list[str] = []
    if policy == "review":
        flags += ["--tools", ",".join(REVIEW_TOOLS)]
        flags += ["--permission-mode", "dontAsk"]
    else:
        flags += ["--permission-mode", "acceptEdits"]
    flags += ["--allowedTools", *_allowed_rules()]
    flags += ["--disallowedTools", *_denied_rules(policy)]
    return flags


def build_argv(
    claude_bin: str,
    *,
    model: str,
    effort: str,
    max_budget_usd: float,
    max_turns: Optional[int],
    policy: str,
) -> list[str]:
    """argv for the run. The prompt goes on stdin, never argv."""
    argv = [
        claude_bin, "-p",
        "--output-format", "json",
        "--model", model,
        "--effort", effort,
        "--max-budget-usd", f"{max_budget_usd:g}",
    ]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    argv += tool_flags(policy)
    return argv


def prepare_env(
    base_env: Mapping[str, str], key_name: str, project: Optional[str] = None,
) -> "tuple[Optional[dict[str, str]], Optional[str]]":
    """``(child_env, None)`` or ``(None, refusal_text)``.

    The blocker is checked BEFORE the key is resolved, so a refused
    environment never touches the secret store. The refusal text names
    variables and the key NAME only.
    """
    why = _cak.retry_blocker(base_env)
    if why is not None:
        return None, f"not running on the Claude API key: {why}"
    key = _cak.resolve_key(key_name, project)
    if key is None:
        who = project or "the current directory"
        return None, (
            f"the Claude API key {key_name!r} does not resolve from the "
            f"Shared secrets slot for {who} — save it in the launcher's "
            "Secrets panel (Shared scope) under that exact name, and run "
            "from (or pass --project) a project registered in the launcher "
            "that the key is active for"
        )
    return _cak.child_env_with_key(base_env, key), None


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_result(stdout: str) -> "tuple[Optional[dict], Optional[str]]":
    """The CLI's JSON result object, or ``(None, why)``.

    ``--output-format json`` prints one object; tolerate leading noise by
    falling back to the last line that parses as an object.
    """
    text = (stdout or "").strip()
    if not text:
        return None, "the claude CLI printed nothing on stdout"
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj, None
    return None, "the claude CLI output is not a JSON result object"


def summarize(result: Optional[Mapping[str, Any]]) -> dict:
    """Cost/usage fields of a CLI result (no prompt, no result text)."""
    r = result or {}
    per_model: dict[str, dict] = {}
    mu = r.get("modelUsage")
    if isinstance(mu, Mapping):
        for name, u in mu.items():
            if not isinstance(u, Mapping):
                continue
            per_model[str(name)] = {
                "input_tokens": _int(u.get("inputTokens")),
                "output_tokens": _int(u.get("outputTokens")),
                "cache_read_tokens": _int(u.get("cacheReadInputTokens")),
                "cache_write_tokens": _int(u.get("cacheCreationInputTokens")),
                "cost_usd": _float(u.get("costUSD")),
            }
    costs = [u["cost_usd"] for u in per_model.values()
             if u["cost_usd"] is not None]
    return {
        "subtype": r.get("subtype"),
        "is_error": bool(r.get("is_error", False)),
        "api_error_status": r.get("api_error_status"),
        "total_cost_usd": _float(r.get("total_cost_usd")),
        "num_turns": _int(r.get("num_turns")),
        "duration_ms": _int(r.get("duration_ms")),
        "model_usage": per_model,
        # Cross-check: the CLI's total has been seen to leave out a small
        # side call (the session-title request); the summary shows both.
        "model_cost_sum_usd": round(sum(costs), 6) if costs else None,
    }


def _scrub(text: str, secret: Optional[str]) -> str:
    if secret and text:
        return text.replace(secret, "***")
    return text


def _error_text(result: Optional[Mapping[str, Any]], summary: dict,
                rc: int, stderr: str, parse_err: Optional[str]) -> Optional[str]:
    if parse_err:
        tail = stderr.strip().splitlines()[-3:] if stderr.strip() else []
        extra = (" — stderr: " + " | ".join(tail)) if tail else ""
        return f"{parse_err} (exit {rc}){extra}"
    if summary["is_error"] or summary["api_error_status"] is not None or rc != 0:
        msg = str((result or {}).get("result") or "").strip().splitlines()
        first = msg[0][:300] if msg else ""
        status = summary["api_error_status"]
        parts = [p for p in (
            f"API status {status}" if status is not None else "",
            first,
            f"CLI exit {rc}" if rc != 0 else "",
        ) if p]
        return "; ".join(parts) or "the run reported an error"
    return None


def format_summary(record: Mapping[str, Any]) -> str:
    lines = [
        f"claude-api-run: model {record['model']} · effort {record['effort']}"
        f" · budget ${record['max_budget_usd']:.2f} · policy {record['policy']}",
    ]
    cost = record.get("total_cost_usd")
    cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else "unknown"
    msum = record.get("model_cost_sum_usd")
    if isinstance(msum, (int, float)) and isinstance(cost, (int, float)) \
            and abs(msum - cost) > 1e-6:
        cost_s += f" (per-model sum ${msum:.4f})"
    lines.append(
        f"  cost {cost_s} · turns {record.get('num_turns', 0)} · duration "
        f"{(record.get('duration_ms') or 0) / 1000:.1f}s")
    for name, u in (record.get("model_usage") or {}).items():
        c = u.get("cost_usd")
        c_s = f"${c:.4f}" if isinstance(c, (int, float)) else "?"
        lines.append(
            f"  {name}: in {u['input_tokens']} · out {u['output_tokens']} · "
            f"cache read {u['cache_read_tokens']} · cache write "
            f"{u['cache_write_tokens']} · {c_s}")
    if record.get("error"):
        lines.append(f"  ERROR: {record['error']}")
    if record.get("report"):
        lines.append(f"  report: {record['report']}")
    if record.get("ledger"):
        lines.append(f"  ledger: {record['ledger']}")
    return "\n".join(lines)


def ledger_path() -> Path:
    from vco_lib.paths import vct_metrics_dir  # noqa: PLC0415

    return vct_metrics_dir() / LEDGER_FILENAME


def append_ledger(record: Mapping[str, Any]) -> Optional[Path]:
    """Append one line; soft-fail (stderr line, ``None``) on an I/O error."""
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"claude-api-run: could not append the ledger {path}: "
              f"{type(exc).__name__}", file=sys.stderr)
        return None
    return path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-api-run",
        description="Run one bounded headless claude -p task billed to the "
                    "Claude Console API key and report what it cost.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="the task prompt text")
    src.add_argument("--prompt-file", help="read the task prompt from a file")
    p.add_argument("--cwd", default=None,
                   help="directory the run works in (default: current)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORT_CHOICES)
    p.add_argument("--max-budget-usd", type=float,
                   default=DEFAULT_MAX_BUDGET_USD)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--project", default=None,
                   help="registered project that requests the key (the hub's "
                        "per-project gate; default: --cwd). The shipped "
                        "wrapper passes the project it is installed in")
    p.add_argument("--key-name", default=_cak.SECRET_KEY_NAME,
                   help="Shared-scope secret holding the Console API key "
                        f"(default {_cak.SECRET_KEY_NAME})")
    p.add_argument("--policy", default="review", choices=POLICY_CHOICES,
                   help="review (default): read-only tools + read-only git; "
                        "edit: file edits in --cwd allowed")
    p.add_argument("--report", default=None,
                   help="write the model's final answer to this file")
    p.add_argument("--json", action="store_true",
                   help="print the summary as one JSON object")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0:
        print("claude-api-run: --max-budget-usd must be > 0", file=sys.stderr)
        return 2
    if args.max_turns is not None and args.max_turns <= 0:
        print("claude-api-run: --max-turns must be > 0", file=sys.stderr)
        return 2

    if args.prompt_file is not None:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"claude-api-run: cannot read --prompt-file "
                  f"{args.prompt_file}: {type(exc).__name__}", file=sys.stderr)
            return EXIT_REFUSED
    else:
        prompt = args.prompt
    if not prompt.strip():
        print("claude-api-run: the prompt is empty", file=sys.stderr)
        return 2

    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"claude-api-run: --cwd {cwd} is not a directory",
              file=sys.stderr)
        return EXIT_REFUSED
    report = Path(args.report).resolve() if args.report else None

    if args.project and not Path(args.project).is_dir():
        print(f"claude-api-run: --project {args.project} is not a directory "
              "(pass the path of a project registered in the launcher)",
              file=sys.stderr)
        return 2
    project = str(Path(args.project).resolve()) if args.project else str(cwd)
    child_env, refusal = prepare_env(os.environ, args.key_name, project)
    if child_env is None:
        print(f"claude-api-run: refused — {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    secret = child_env.get(_cak.CHILD_ENV_VAR)

    claude_bin = shutil.which("claude", path=child_env.get("PATH"))
    if not claude_bin:
        print("claude-api-run: refused — the claude CLI is not on PATH",
              file=sys.stderr)
        return EXIT_REFUSED

    argv_run = build_argv(
        claude_bin, model=args.model, effort=args.effort,
        max_budget_usd=args.max_budget_usd, max_turns=args.max_turns,
        policy=args.policy)
    # No wall-clock timeout, deliberately: a Fable review can run for a long
    # time on a slow machine (VCO's shipped timeouts are long by design), and
    # cost — the thing a runaway would burn — is capped by --max-budget-usd,
    # which the CLI enforces. Ctrl-C stops the run.
    proc = subprocess.run(  # noqa: S603 — argv list, no shell
        argv_run, input=prompt, capture_output=True, text=True,
        cwd=str(cwd), env=child_env, check=False)
    stdout = _scrub(proc.stdout or "", secret)
    stderr = _scrub(proc.stderr or "", secret)

    result, parse_err = parse_result(stdout)
    summary = summarize(result)
    error = _error_text(result, summary, proc.returncode, stderr, parse_err)
    answer = _scrub(str((result or {}).get("result") or ""), secret)

    report_written: Optional[str] = None
    if report is not None and answer:
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(answer if answer.endswith("\n") else answer + "\n",
                              encoding="utf-8")
            report_written = str(report)
        except OSError as exc:
            error = (error + "; " if error else "") + (
                f"could not write the report {report}: {type(exc).__name__}")

    record: dict[str, Any] = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "cwd": str(cwd),
        "model": args.model,
        "effort": args.effort,
        "max_budget_usd": args.max_budget_usd,
        "max_turns": args.max_turns,
        "policy": args.policy,
        "key_name": args.key_name,
        "exit_code": proc.returncode,
        **summary,
        "error": error,
        "report": report_written,
    }
    written = append_ledger(record)

    shown = dict(record)
    shown["ledger"] = str(written) if written else None
    if args.json:
        if report_written is None:
            shown["result"] = answer
        print(json.dumps(shown, indent=2, sort_keys=True))
    else:
        if report_written is None and answer:
            print(answer)
        print(format_summary(shown), file=sys.stderr)
    return EXIT_RUN_FAILED if error else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
