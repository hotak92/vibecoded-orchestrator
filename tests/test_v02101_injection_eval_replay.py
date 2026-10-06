#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-E — offline injection replay evaluator (precision + recall).

PLAN-V02101 §3 WP-E / §5 Lane EVAL / §6 gate 7. Replays the INJECTION
pipeline over scrubbed fixture transcripts in OFFLINE mode: the classifier
and the router's plan builders run for real, while the network legs are
replaced by golden producer outputs bundled per fixture (``golden_producers.json``)
— no Weaviate, no Ollama, no subprocess, no network. Hermetic and CI-fast
(seconds).

Why this exists
---------------
The owner requirement (plan §0) makes the work TWO-SIDED: cut noise AND
close coverage gaps, measured as PRECISION and RECALL, not token savings
alone. The survey baseline is ~5% useful injections; this harness is the
regression gate that a future change cannot silently re-break — mutate one
classification and it goes red (see ``red_proof()`` / the pinned table /
``test_red_proof_detects_a_classifier_regression``).

What is measured (metric definitions, plan §3 WP-E)
---------------------------------------------------
* **Precision proxy** = referenced-injected / total-injected, aggregated
  across fixtures. An injected block counts "referenced" when its identity
  (a KG node title, or a CODE block's ``full_name`` / its final symbol
  segment) appears in a SUBSEQUENT assistant message or tool-call argument
  in the same session. (The plan's second, manual-labeling judge lives in
  the WP-E report — not reproducible in CI.)
* **Recall (retrieval-debt coverage)** = covered-debt / total-debt. A debt
  event is (a) an explicit ``hybrid_search`` / ``search_code_graph`` /
  ``query_code_structure`` tool call, or (b) a Grep / ``git grep`` whose
  pattern passes the identifier gate, occurring within 3 transcript records
  after a Read/Edit of the file the identifier belongs to (the fixture's
  ``entities`` map). Covered = the pipeline injects that entity at or before
  the debt event.
* **Noise** = injections attributed to a MECHANICAL-classified Bash command
  (must be 0) + characters injected per 100 tool calls.

The BEFORE leg (legacy pipeline)
--------------------------------
The legacy path is SHELL code (``templates/hooks/pre-bash-context-inject.sh``,
``pre-edit-context-inject.sh``, ``pre-tool-use.sh``) and its producers need
Weaviate — unreachable hermetically. So the before leg uses the COMMITTED
base code where it is reachable (``vco_lib.inject_intent.pattern_gate`` /
``extract_symbol`` — the Python ports of the legacy shell gates, per their
MUST MATCH block; the sibling ``bash_gate`` prefilter was RETIRED from that
module by the Wave-2 hook lane, so it is recorded locally here) plus a
RECORDED baseline for the legacy producer outputs and query shapes:

* pre-bash KG query = the raw capped command (the documented fallback the
  shell takes when the noise-strip helper is unavailable) — no Python mirror
  of ``vco_strip_command_noise``;
* pre-bash CG symbol = ``extract_symbol(command)``;
* pre-edit query = ``"<module-basename> <new_string[:200]>"`` (=
  ``pre-edit-context-inject.sh``'s ``QUERY="$MODULE_NAME $NEW_STRING_SNIPPET"``);
* Read = 0 injections (the legacy ``pre-tool-use.sh`` Read branch ran a 3 s
  hook timeout under a 4 s inner timeout under a 4.7-11.6 s cold CLI start —
  always killed; proven by §9 kickoff probe);
* Agent/SubagentStart = 0 injections (SubagentStart carries no prompt — the
  legacy query could never fire, plan §1).

The recorded legacy outputs are near-misses (right file, wrong symbol) and
irrelevant titles, modelling the survey's low-precision baseline. Provenance
and the raw numbers are in the WP-E report delivered to the coordinator.

Running
-------
* ``python tests/test_v02101_injection_eval_replay.py`` — prints the before/after table,
  exits non-zero when a threshold or a pinned case fails.
* ``pytest tests/test_v02101_injection_eval_replay.py`` — the same checks as
  tests. Collected by a bare recursive ``pytest`` (``test_*`` glob) since the
  v0.2.101 rename; 9 tests, ~0.4 s.
  — a scope decision for the coordinator, not this lane).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "claude_mcp_servers" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "injection_eval"

for _p in (str(SCRIPTS), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `hook_context_router` is a runtime SCRIPT (claude_mcp_servers/scripts), not
# an importable package — it resolves only via the sys.path insert above, so
# pyright cannot follow it. Per-line ignore with rationale (pyrightconfig.json's
# own contract for a NEW error inside the gate scope).
import hook_context_router as ROUTER  # noqa: E402  # pyright: ignore[reportMissingImports]
from vco_lib.inject_intent import (  # noqa: E402
    INTENT_MECHANICAL,
    INTENT_READ,
    BashIntent,
    extract_symbol,
    pattern_gate,
)

# --- thresholds (plan §3 WP-E: precision proxy >= 0.35, debt coverage >= 0.60) -
PRECISION_MIN = 0.35
RECALL_MIN = 0.60

#: Debts are searched this many transcript records back for a Read/Edit of the
#: identifier's file (plan §3 WP-E: "within 3 turns after a Read/Edit").
DEBT_WINDOW = 3

_GREP_TOOL_RE = re.compile(r"(^|[\s|])(grep|rg|ag|ack)(\s|$)")

#: Recorded-baseline port of the legacy ``codegraph_bash_gate`` (the pre-bash
#: code-graph prefilter). It was a committed port in ``vco_lib.inject_intent``
#: during Wave 1, but the Wave-2 hook lane RETIRED it there (WP-A1: "the
#: .sh/.ps1 gates delegate or are retired with their callers"), so the gate is
#: no longer reachable from the committed base and lives here as part of the
#: before-leg baseline (one release, per plan §3 WP-E "else a recorded
#: baseline"). ``pattern_gate`` stays imported — it is the ONE HOME the live
#: classifier (``_classify_search``) still calls, so it is not going away.
_BASH_GATE_CODE_FILE_RE = re.compile(
    r"(^|[\s/])[A-Za-z0-9_-]+\."
    r"(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto)"
    r"([^A-Za-z0-9]|$)"
)


def _legacy_bash_gate(command: str) -> bool:
    """The recorded legacy pre-bash code-graph prefilter (see block comment)."""
    if not command:
        return False
    if _BASH_GATE_CODE_FILE_RE.search(command):
        return True
    return bool(_GREP_TOOL_RE.search(command)) and pattern_gate(command)


_SURFACE_BY_TOOL = {
    "Bash": "bash",
    "Read": "read",
    "Edit": "edit",
    "Write": "write",
    "Grep": "grep",
    "Agent": "agent",
    "Task": "agent",
}

_EXPLICIT_RETRIEVAL = {"hybrid_search", "search_code_graph", "query_code_structure"}

_AGENT_MARKER = "[KG context for this task]:\n"


# ─── fixtures ─────────────────────────────────────────────────────────────────

#: Pinned classifications — the classifier's contract on the eval corpus.
#: A regression in ANY of these must fail the harness (the brief's red-proof:
#: "mutate one classification → red"). Kept as data so a new wrong-trigger
#: class is one line, not one function.
PINNED_CLASSIFICATIONS: Sequence[Tuple[str, str]] = (
    ("cargo clippy --workspace", INTENT_MECHANICAL),
    ("git status", INTENT_MECHANICAL),
    ("ls -la", INTENT_MECHANICAL),
    ("cat src/auth.py", "READ"),
    ("sed -n '1,20p' src/auth.py", "READ"),
    ("git show HEAD:src/auth.py", "READ"),
    ("sed -i 's/a/b/' src/auth.py", "EDIT"),
    ("grep -rn validate_token src/", "SEARCH"),
    ("rg parse_token lib/", "SEARCH"),
)

#: The intended after-mode query shape, pinned so a plan-builder regression
#: (e.g. a return to the legacy "<module> <snippet>" semantic query) goes red.
PINNED_READ_KG_QUERY = "auth src"
PINNED_EDIT_SYMBOL = "validate_token"


@dataclass(frozen=True)
class ToolCall:
    index: int  # record index of the assistant message carrying it
    name: str
    tool_input: Dict[str, object]
    result: str
    assistant_text: str


@dataclass
class Session:
    path: Path
    name: str
    sid: str
    calls: List[ToolCall]
    assistant_texts: Dict[int, str]
    tool_args: Dict[int, List[str]]
    n_records: int


@dataclass(frozen=True)
class Block:
    kind: str  # "KG" | "CODE"
    entity: str  # KG title | CODE full_name
    identity: Tuple[str, ...]  # tokens that count as "the block referenced"
    text: str


@dataclass(frozen=True)
class Injection:
    index: int
    entity: str
    chars: int
    surface: str


@dataclass(frozen=True)
class Debt:
    index: int
    entity: str
    source: str  # "explicit" | "grep"


@dataclass
class SessionResult:
    session: str
    injections: List[Injection]
    debts: List[Debt]
    mechanical_injections: int
    tool_calls: int


@dataclass
class ModeResult:
    mode: str
    injected: int
    referenced: int
    chars: int
    debts: int
    covered: int
    mechanical_injections: int
    tool_calls: int
    by_surface: Dict[str, int]

    @property
    def precision(self) -> float:
        return self.referenced / self.injected if self.injected else 0.0

    @property
    def recall(self) -> float:
        return self.covered / self.debts if self.debts else 0.0

    @property
    def chars_per_100_calls(self) -> float:
        return 100.0 * self.chars / self.tool_calls if self.tool_calls else 0.0

    @property
    def read_injections(self) -> int:
        """Injections on the Read surface — the §9 zero-injection case: 0 on
        the legacy pipeline (the 3 s timeout always killed it), > 0 after."""
        return self.by_surface.get("read", 0)


def _as_text(content: object) -> str:
    """Flatten a tool_result ``content`` (string or content-block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def load_session(path: Path) -> Session:
    """Parse one Claude Code-shaped JSONL transcript (tool-call skeleton only).

    Only the fields the metrics need are read — no thinking/output prose
    beyond the tool-call skeleton the harness authored (R31 scrub discipline).
    """
    records: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))

    results: Dict[str, str] = {}
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                results[str(item.get("tool_use_id") or "")] = _as_text(
                    item.get("content")
                )

    calls: List[ToolCall] = []
    assistant_texts: Dict[int, str] = {}
    tool_args: Dict[int, List[str]] = {}
    for idx, rec in enumerate(records):
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        texts: List[str] = []
        uses: List[dict] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif item.get("type") == "tool_use":
                uses.append(item)
        joined = "\n".join(t for t in texts if t)
        assistant_texts[idx] = joined
        tool_args[idx] = [
            json.dumps(u.get("input") or {}, ensure_ascii=False) for u in uses
        ]
        for u in uses:
            calls.append(
                ToolCall(
                    index=idx,
                    name=str(u.get("name") or ""),
                    tool_input=dict(u.get("input") or {}),
                    result=results.get(str(u.get("id") or ""), ""),
                    assistant_text=joined,
                )
            )

    sid = re.sub(r"[^A-Za-z0-9_-]", "", path.stem)
    return Session(
        path=path,
        name=path.stem,
        sid=sid,
        calls=calls,
        assistant_texts=assistant_texts,
        tool_args=tool_args,
        n_records=len(records),
    )


def load_golden() -> dict:
    return json.loads((FIXTURES / "golden_producers.json").read_text(encoding="utf-8"))


def materialize_tree(golden: dict, root: Path) -> None:
    """Write the fixture's synthetic sources under ``root`` so the router's
    disk-reading symbol extractors (``file_pub_symbols`` /
    ``edit_enclosing_symbols``) see a real tree. Deterministic, scrubbed."""
    for rel, text in golden.get("sources", {}).items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


# ─── block parsing ────────────────────────────────────────────────────────────

_KG_HEADER_RE = re.compile(r"^KG: (.+)$")
_CODE_HEADER_RE = re.compile(r"^CODE: (.+)$")
_BLOCK_SPLIT_RE = re.compile(r"\n(?=(?:KG|CODE): )")


def _blocks_from_text(text: str) -> List[Block]:
    out: List[Block] = []
    for chunk in _BLOCK_SPLIT_RE.split(text.strip("\n")):
        if not chunk.strip():
            continue
        first = chunk.split("\n", 1)[0]
        m_kg = _KG_HEADER_RE.match(first)
        m_code = _CODE_HEADER_RE.match(first)
        if m_kg:
            title = m_kg.group(1).split(" | ", 1)[0].strip()
            if title:
                out.append(Block("KG", title, (title,), chunk))
        elif m_code:
            full = m_code.group(1).split(" | ", 1)[0].strip()
            if full:
                tokens = {full, full.split(".")[-1]}
                out.append(
                    Block("CODE", full, tuple(sorted(t for t in tokens if t)), chunk)
                )
    return out


def _parse_stdout(surface: str, out: str) -> List[Block]:
    """Blocks the router emitted for a non-agent surface, or the KG blocks it
    appended to the agent brief inside the ``updatedInput`` envelope."""
    if surface == "agent":
        try:
            env = json.loads(out)
        except json.JSONDecodeError:
            return []
        prompt = str(
            ((env.get("hookSpecificOutput") or {}).get("updatedInput") or {}).get(
                "prompt"
            )
            or ""
        )
        idx = prompt.find(_AGENT_MARKER)
        if idx < 0:
            return []
        return _blocks_from_text(prompt[idx + len(_AGENT_MARKER) :])
    return _blocks_from_text(out)


# ─── driving the router in-process (golden legs, no network) ──────────────────


def _absolutize(tool_input: Dict[str, object], root: Path) -> Dict[str, object]:
    out = dict(tool_input)
    for key in ("file_path", "path"):
        val = out.get(key)
        if isinstance(val, str) and val and not os.path.isabs(val):
            out[key] = str(root / val)
    return out


def _make_fake_legs(after: dict, revision: str):
    def fake(plan, sid, prompt_id, project_root, transcript_path, deadline):  # noqa: ANN001, ANN202
        kg = ""
        if plan.kg_profile and plan.kg_query:
            kg = after["kg"].get(plan.kg_query, "")
        cg_parts: List[str] = []
        if plan.cg_profile:
            if plan.rev_gate:
                cg_parts.append(f"CODE-REV: {revision}\n")
            for symbol, _source in plan.cg_symbols:
                block = after["cg"].get(symbol, "")
                if block:
                    cg_parts.append(block)
        return kg, "".join(cg_parts)

    return fake


@contextlib.contextmanager
def _project_root_env(root: Path):
    """Pin ``CLAUDE_PROJECT_DIR`` to the session's own tree for the replay.

    ``hook_context_router._resolve_project_root`` reads that env var FIRST, so
    without this the router's seen-store / budget files land in whatever the
    ambient value names — under pytest that is the conftest scratch dir, where
    repeated ``run_eval`` calls would poison each other's dedupe ledger. The
    harness owns its state dir: exactly one session, one tree.
    """
    saved = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = str(root)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved


def _drive(surface: str, payload: dict) -> str:
    buf = io.StringIO()
    saved_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(payload))
        with contextlib.redirect_stdout(buf):
            ROUTER.main([surface])
    finally:
        sys.stdin = saved_stdin
    return buf.getvalue()


def replay_after(session: Session, golden: dict, root: Path) -> SessionResult:
    """Drive the REAL router (plan builders + gates + dedupe + budget) with
    the network legs replaced by golden producer outputs."""
    saved_legs = ROUTER._run_legs  # noqa: SLF001 — the golden seam (plan §3 WP-E)
    saved_rev = ROUTER._resolve_rev  # noqa: SLF001
    ROUTER._run_legs = _make_fake_legs(golden["after"], golden["revision"])  # noqa: SLF001
    ROUTER._resolve_rev = lambda rev, cwd: golden["revision"]  # noqa: SLF001, ARG005

    injections: List[Injection] = []
    mechanical = 0
    try:
        with _project_root_env(root):
            for call in session.calls:
                surface = _SURFACE_BY_TOOL.get(call.name)
                if surface is None:
                    continue
                tool_input = _absolutize(call.tool_input, root)
                payload = {
                    "session_id": session.sid,
                    "prompt_id": f"p{call.index}",
                    "transcript_path": str(session.path),
                    "cwd": str(root),
                    "tool_name": call.name,
                    "tool_input": tool_input,
                    "tool_response": {"content": call.result} if call.result else None,
                }
                out = _drive(surface, payload)
                blocks = _parse_stdout(surface, out)
                is_mech = (
                    surface == "bash"
                    and ROUTER.classify_bash(
                        str(tool_input.get("command") or ""), str(root)
                    ).intent
                    == INTENT_MECHANICAL
                )
                for block in blocks:
                    injections.append(
                        Injection(call.index, block.entity, len(block.text), surface)
                    )
                    if is_mech:
                        mechanical += 1
    finally:
        ROUTER._run_legs = saved_legs  # noqa: SLF001
        ROUTER._resolve_rev = saved_rev  # noqa: SLF001

    return SessionResult(
        session=session.name,
        injections=injections,
        debts=session_debts(session, golden),
        mechanical_injections=mechanical,
        tool_calls=len(session.calls),
    )


# ─── legacy (before) model ────────────────────────────────────────────────────


def _legacy_blocks(call: ToolCall, before: dict) -> List[str]:
    name = call.name
    tool_input = call.tool_input
    if name in ("Read", "Write"):
        return []  # legacy Read branch timed out (§9); Write had no surface
    if name in ("Agent", "Task"):
        return []  # SubagentStart carried no prompt (plan §1)
    if name == "Bash":
        command = str(tool_input.get("command") or "")
        blocks: List[str] = []
        kg_block = before["kg"].get(command[:500])
        if kg_block:
            blocks.append(kg_block)
        if _legacy_bash_gate(command):
            symbol = extract_symbol(command)
            cg_block = before["cg"].get(symbol) if symbol else None
            if cg_block:
                blocks.append(cg_block)
        return blocks
    if name == "Edit":
        file_path = str(tool_input.get("file_path") or "")
        module = os.path.splitext(os.path.basename(file_path))[0]
        query = f"{module} {str(tool_input.get('new_string') or '')[:200]}"
        return [b for b in (before["kg"].get(query), before["cg"].get(query)) if b]
    if name == "Grep":
        pattern = str(tool_input.get("pattern") or "")
        if not pattern_gate(pattern):
            return []
        symbol = extract_symbol(pattern) or pattern
        cg_block = before["cg"].get(symbol)
        return [cg_block] if cg_block else []
    return []


def replay_before(session: Session, golden: dict, root: Path) -> SessionResult:
    """The legacy pipeline model: committed gate ports + recorded outputs."""
    state = root / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    inject_file = str(state / f"seen_inject_{session.sid}.txt")
    reads_file = str(state / f"seen_reads_{session.sid}.txt")

    injections: List[Injection] = []
    mechanical = 0
    with _project_root_env(root):
        for call in session.calls:
            blocks = _legacy_blocks(call, golden["before"])
            if call.name == "Bash":
                command = str(call.tool_input.get("command") or "")
                if ROUTER.classify_bash(command, str(root)).intent == INTENT_MECHANICAL:
                    mechanical += len(blocks)
            if not blocks:
                continue
            filtered = ROUTER.filter_seen_blocks(
                "".join(blocks), inject_file, reads_file, str(root)
            )
            surface = _SURFACE_BY_TOOL.get(call.name, "")
            for block in _blocks_from_text(filtered):
                injections.append(
                    Injection(call.index, block.entity, len(block.text), surface)
                )

    return SessionResult(
        session=session.name,
        injections=injections,
        debts=session_debts(session, golden),
        mechanical_injections=mechanical,
        tool_calls=len(session.calls),
    )


# ─── debts + coverage ─────────────────────────────────────────────────────────


def _first_entity(text: str) -> str:
    for line in (text or "").splitlines():
        m = re.match(r"^(?:CODE|KG): ([^\n|]+?)(?:\s*\|.*)?$", line.strip())
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _touches_file(call: ToolCall, relpath: str) -> bool:
    if call.name in ("Read", "Edit", "Write"):
        file_path = str(call.tool_input.get("file_path") or "")
        return os.path.normpath(file_path).endswith(os.path.normpath(relpath))
    if call.name == "Bash":
        return relpath in str(call.tool_input.get("command") or "")
    return False


def _grep_identifier(call: ToolCall) -> Optional[str]:
    if call.name == "Grep":
        pattern = str(call.tool_input.get("pattern") or "")
        if pattern_gate(pattern):
            return extract_symbol(pattern) or pattern
        return None
    if call.name == "Bash":
        command = str(call.tool_input.get("command") or "")
        if _GREP_TOOL_RE.search(command) and pattern_gate(command):
            return extract_symbol(command) or None
        return None
    return None


def session_debts(session: Session, golden: dict) -> List[Debt]:
    entities: Dict[str, str] = golden.get("entities", {})
    debts: List[Debt] = []
    for call in session.calls:
        if call.name in _EXPLICIT_RETRIEVAL:
            entity = _first_entity(call.result)
            if entity:
                debts.append(Debt(call.index, entity, "explicit"))
            continue
        identifier = _grep_identifier(call)
        if not identifier:
            continue
        relpath = entities.get(identifier)
        if not relpath:
            continue
        window = range(max(0, call.index - DEBT_WINDOW), call.index)
        if any(_touches_file(c, relpath) for c in session.calls if c.index in window):
            debts.append(Debt(call.index, identifier, "grep"))
    return debts


def _entity_token_re(entity: str) -> "re.Pattern[str]":
    # whole-identifier match: "parse_header" must NOT be covered by
    # "parse_header_len" (the legacy near-miss class).
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(entity) + r"(?![A-Za-z0-9_])")


def _referenced(session: Session, index: int, identity: Sequence[str]) -> bool:
    for j in range(index + 1, session.n_records):
        corpus = (
            session.assistant_texts.get(j, "")
            + " "
            + " ".join(session.tool_args.get(j, []))
        )
        if any(tok and tok in corpus for tok in identity):
            return True
    return False


def _covered(debt: Debt, injections: Sequence[Injection]) -> bool:
    rx = _entity_token_re(debt.entity)
    return any(inj.index <= debt.index and rx.search(inj.entity) for inj in injections)


# ─── evaluation ───────────────────────────────────────────────────────────────


def _session_paths() -> List[Path]:
    return sorted(FIXTURES.glob("*.jsonl"))


def _aggregate(
    mode: str, results: Sequence[SessionResult], sessions: Sequence[Session]
) -> ModeResult:
    by_name = {s.name: s for s in sessions}
    injected = referenced = chars = debt_n = covered = mech = calls = 0
    by_surface: Dict[str, int] = {}
    for res in results:
        calls += res.tool_calls
        mech += res.mechanical_injections
        injected += len(res.injections)
        chars += sum(inj.chars for inj in res.injections)
        debt_n += len(res.debts)
        covered += sum(1 for d in res.debts if _covered(d, res.injections))
        for inj in res.injections:
            by_surface[inj.surface] = by_surface.get(inj.surface, 0) + 1
        session = by_name[res.session]
        identity_by_entity: Dict[str, Tuple[str, ...]] = {}
        # (rebuild identity tokens from the same parsing rules for the ref check)
        for inj in res.injections:
            tokens = (inj.entity, inj.entity.split(".")[-1])
            identity_by_entity[inj.entity] = tuple(sorted(set(tokens)))
        for inj in res.injections:
            if _referenced(session, inj.index, identity_by_entity[inj.entity]):
                referenced += 1
    return ModeResult(
        mode=mode,
        injected=injected,
        referenced=referenced,
        chars=chars,
        debts=debt_n,
        covered=covered,
        mechanical_injections=mech,
        tool_calls=calls,
        by_surface=by_surface,
    )


def run_eval(mode: str = "after") -> ModeResult:
    golden = load_golden()
    sessions = [load_session(p) for p in _session_paths()]
    results: List[SessionResult] = []
    # One fresh state dir per (session, mode): the seen-store dedupe is
    # per-session by design, and the legacy inject file must not mix with
    # the router's. Cleaned up afterwards so a full pytest run leaves no
    # /tmp litter.
    with tempfile.TemporaryDirectory(prefix=f"injeval-{mode}-") as tmp:
        for session in sessions:
            root = Path(tmp) / session.name
            root.mkdir(parents=True, exist_ok=True)
            materialize_tree(golden, root)
            if mode == "after":
                results.append(replay_after(session, golden, root))
            else:
                results.append(replay_before(session, golden, root))
    return _aggregate(mode, results, sessions)


def pinned_failures(tree_root: Path) -> List[str]:
    """Classifications that drifted from the pinned table (empty = all hold)."""
    bad: List[str] = []
    for command, expected in PINNED_CLASSIFICATIONS:
        got = ROUTER.classify_bash(command, str(tree_root)).intent
        if got != expected:
            bad.append(f"{command!r}: expected {expected}, got {got}")
    return bad


def pinned_plan_failures(golden: dict, tree_root: Path) -> List[str]:
    bad: List[str] = []
    payload = {
        "session_id": "pin",
        "prompt_id": "p",
        "cwd": str(tree_root),
        "tool_input": {"file_path": str(tree_root / "src" / "auth.py")},
        "tool_response": None,
    }
    read_plan = ROUTER._read_plan(payload, str(tree_root))  # noqa: SLF001
    if read_plan.kg_query != PINNED_READ_KG_QUERY:
        bad.append(
            f"read kg_query: expected {PINNED_READ_KG_QUERY!r}, got {read_plan.kg_query!r}"
        )
    edit_plan = ROUTER._edit_plan(  # noqa: SLF001
        {
            "cwd": str(tree_root),
            "tool_input": {
                "file_path": str(tree_root / "src" / "auth.py"),
                "old_string": "def validate_token(token):",
            },
        },
        str(tree_root),
    )
    symbols = [s for s, _f in edit_plan.cg_symbols]
    if PINNED_EDIT_SYMBOL not in symbols:
        bad.append(
            f"edit enclosing symbol: expected {PINNED_EDIT_SYMBOL!r}, got {symbols!r}"
        )
    return bad


def red_proof(tree_root: Path) -> bool:
    """True when a SIMULATED classifier regression is DETECTED (the harness
    must go red). Mutates one classification in-process, never on disk:
    removing the READ verb ``cat`` turns every ``cat <file>`` READ into
    MECHANICAL — a pinned case fails and Read-derived debt coverage collapses.
    """
    session_loader = [load_session(p) for p in _session_paths()]
    golden = load_golden()
    saved = ROUTER.classify_bash

    def broken(command: str, cwd: str = ""):
        bi = saved(command, cwd)
        if bi.intent == INTENT_READ and re.search(r"(^|[\s;&|(])cat(\s|$)", command):
            return BashIntent(
                intent=INTENT_MECHANICAL,
                targets=bi.targets,
                symbols=bi.symbols,
                write_snippet=bi.write_snippet,
                rev_paths=bi.rev_paths,
            )
        return bi

    ROUTER.classify_bash = broken  # noqa: SLF001
    try:
        bad_pins = pinned_failures(tree_root)
        with tempfile.TemporaryDirectory(prefix="injeval-red-") as tmp:
            after = run_eval_after_with(golden, session_loader, Path(tmp))
    finally:
        ROUTER.classify_bash = saved  # noqa: SLF001
    return (
        bool(bad_pins) or after.recall < RECALL_MIN or after.precision < PRECISION_MIN
    )


def run_eval_after_with(
    golden: dict, sessions: Sequence[Session], base: Path
) -> ModeResult:
    results: List[SessionResult] = []
    for session in sessions:
        root = base / session.name
        root.mkdir(parents=True, exist_ok=True)
        materialize_tree(golden, root)
        results.append(replay_after(session, golden, root))
    return _aggregate("after", results, sessions)


# ─── report ───────────────────────────────────────────────────────────────────


def format_report(results: Dict[str, ModeResult]) -> str:
    lines = [
        "WP-E injection replay (offline, scrubbed fixtures)",
        f"  fixtures: {', '.join(p.stem for p in _session_paths())}",
        "",
        f"  {'mode':<8}{'precision':>11}{'recall':>9}{'injected':>10}"
        f"{'refd':>7}{'debts':>7}{'covd':>6}{'mech':>6}{'chars/100calls':>16}",
    ]
    for mode in ("before", "after"):
        r = results[mode]
        lines.append(
            f"  {r.mode:<8}{r.precision:>11.3f}{r.recall:>9.3f}{r.injected:>10}"
            f"{r.referenced:>7}{r.debts:>7}{r.covered:>6}{r.mechanical_injections:>6}"
            f"{r.chars_per_100_calls:>16.1f}"
        )
    lines.append("")
    lines.append(
        "  injections by surface: "
        + "  ".join(
            f"{mode}="
            + (
                ",".join(
                    f"{k}:{v}" for k, v in sorted(results[mode].by_surface.items())
                )
                or "-"
            )
            for mode in ("before", "after")
        )
    )
    lines.append(
        "  §9 Read zero-injection gap: "
        f"before={results['before'].read_injections} after={results['after'].read_injections} "
        "Read-surface injections"
    )
    lines.append("")
    lines.append(
        f"  thresholds: precision >= {PRECISION_MIN}, recall >= {RECALL_MIN}, "
        "mechanical injections == 0"
    )
    return "\n".join(lines)


def check(tree_root: Path) -> Tuple[bool, str, Dict[str, ModeResult]]:
    golden = load_golden()
    materialize_tree(golden, tree_root)

    results = {"after": run_eval("after"), "before": run_eval("before")}
    report = format_report(results)
    problems: List[str] = []
    for msg in pinned_failures(tree_root):
        problems.append(f"pinned classification: {msg}")
    for msg in pinned_plan_failures(golden, tree_root):
        problems.append(f"pinned plan: {msg}")

    after = results["after"]
    if after.precision < PRECISION_MIN:
        problems.append(f"precision {after.precision:.3f} < {PRECISION_MIN}")
    if after.recall < RECALL_MIN:
        problems.append(f"recall {after.recall:.3f} < {RECALL_MIN}")
    if after.mechanical_injections != 0:
        problems.append(f"mechanical injections {after.mechanical_injections} != 0")
    if after.injected == 0:
        problems.append("no injections at all — fixtures or pipeline wired wrong")
    if after.read_injections == 0:
        problems.append("Read surface injected nothing — the §9 gap is not closed")

    detail = (
        report
        if not problems
        else report + "\n\nFAILURES:\n  - " + "\n  - ".join(problems)
    )
    return (not problems), detail, results


# ─── Gate 21 self-check (no literals: patterns read from the gate itself) ─────


def _gate21_patterns() -> List[str]:
    gate = REPO_ROOT / "scripts" / "check-pre-tag-privacy.sh"
    if not gate.is_file():
        return []
    text = gate.read_text(encoding="utf-8", errors="replace")
    patterns = re.findall(r'check_pattern\s+"[^"]*"\s+"([^"]+)"', text)
    block = re.search(r"known_names_blocklist=\((.*?)\)", text, re.S)
    if block:
        for name in re.findall(r'"([^"]+)"', block.group(1)):
            patterns.append(r"/(home|Users)/" + re.escape(name) + r"(/|$)")
    # The bare bug-reporter-name regex (Track v0.2.68) — read from the gate,
    # never copied here (a literal would itself be a leak).
    bare = re.search(r'bare_name_re="([^"]+)"', text)
    if bare:
        patterns.append("(?i)" + bare.group(1))  # the gate greps it with -i
    return patterns


def scrubbed_violations() -> List[str]:
    """Gate 21 (privacy) applied to the harness + its fixtures, using the
    gate's OWN pattern list (one home — no copied literals here)."""
    targets = [Path(__file__), *sorted(FIXTURES.glob("*"))]
    violations: List[str] = []
    for pattern in _gate21_patterns():
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        for target in targets:
            if not target.is_file():
                continue
            if rx.search(target.read_text(encoding="utf-8", errors="replace")):
                violations.append(f"{target.name} matches {pattern!r}")
    return violations


# ─── pytest entry points (collected when this file is named explicitly) ──────


def test_fixtures_and_harness_are_scrubbed() -> None:
    assert scrubbed_violations() == []


def test_pinned_classifications_hold(tmp_path: Path) -> None:
    golden = load_golden()
    materialize_tree(golden, tmp_path)
    assert pinned_failures(tmp_path) == []


def test_pinned_plan_shapes_hold(tmp_path: Path) -> None:
    golden = load_golden()
    materialize_tree(golden, tmp_path)
    assert pinned_plan_failures(golden, tmp_path) == []


def test_after_precision_meets_threshold() -> None:
    assert run_eval("after").precision >= PRECISION_MIN


def test_after_recall_meets_threshold() -> None:
    assert run_eval("after").recall >= RECALL_MIN


def test_mechanical_injections_are_zero() -> None:
    for mode in ("after", "before"):
        assert run_eval(mode).mechanical_injections == 0


def test_read_surface_zero_injection_gap_closed() -> None:
    """§9: the legacy Read branch produced 0 injections (its 3 s timeout was
    always killed by the 4.7-11.6 s cold CLI start); the new Read surface must
    inject."""
    before = run_eval("before")
    after = run_eval("after")
    assert before.read_injections == 0
    assert after.read_injections > 0


def test_before_outperformed_by_after() -> None:
    before = run_eval("before")
    after = run_eval("after")
    assert after.precision >= before.precision
    assert after.recall >= before.recall


def test_red_proof_detects_a_classifier_regression(tmp_path: Path) -> None:
    """A simulated classifier regression MUST be caught (brief: mutate one
    classification → red)."""
    golden = load_golden()
    materialize_tree(golden, tmp_path)
    assert red_proof(tmp_path) is True


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="injeval-pins-") as tmp:
        ok, detail, _results = check(Path(tmp))
    print(detail)
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
