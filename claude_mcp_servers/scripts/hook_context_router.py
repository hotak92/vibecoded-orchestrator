#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-A3 — the ONE runner behind every injection surface.

Usage (the thin .sh/.ps1 wrappers of Wave 2 call exactly this)::

    hook_context_router.py <surface> [--intent-out FILE]   # hook JSON on stdin

    surfaces: bash | read | write | edit | grep | agent

Contract
--------
* stdin  — the full Claude Code hook payload (session_id, prompt_id,
  transcript_path, cwd, tool_name, tool_input, tool_response). The query text
  NEVER travels through argv (R31 privacy discipline).
* stdout — the injection text for the wrapper to pass to
  ``emit_additional_context`` (non-agent surfaces), or the full
  ``updatedInput`` JSON envelope (agent surface ONLY — it round-trips every
  original ``tool_input`` field and mutates only ``prompt``; it NEVER emits
  ``permissionDecision``). Empty stdout = inject nothing.
* exit   — ALWAYS 0. A retrieval failure is silence, never a blocked tool.

Mechanism (one interpreter, one home):
* intent/query building — ``vco_lib.inject_intent`` (pure core, §2.1 table);
* concurrent legs — ``hook_dual_search.run_legs`` (the SAME thread-routed
  stdout capture the pre-edit dual driver uses; imported, not re-spawned);
* KG leg  — ``rl_kg_search.main()`` in-process with ``--injection-profile``
  (the §2.1 floors/tiers) and the surface's ``--task-type`` (WP-D: the RL
  retrieval events keep flowing, partitioned per surface);
* CG leg  — ``query_code_graph.main()`` in-process, ``structure callers
  <symbol> --hook-format`` (EXACT symbol lookup — the redesign issues no
  semantic code-graph queries from injection surfaces), with
  ``--indexed-revision`` on revision-stamped policies and ``--source-file``
  for the same-language identity check;
* dedupe  — the SAME per-session files and key format as ``_lib/seen-store.sh``
  (``seen_inject_<sid>.txt`` / ``seen_reads_<sid>.txt``; KG key
  ``title#sha1(body)[:12]``, CODE key ``full_name``) — MUST MATCH that lib;
* budget  — per-injection 2 500-char soft cap + per-turn 6 000 chars keyed by
  ``prompt_id`` (``.claude/state/inject_budget_<sid>_<pid>``; past budget the
  blocks degrade to titles one-liners; missing prompt_id fails OPEN);
* cache   — the shared ``.claude/state/query_cache/`` dir with the shell's
  sha1(0x1f-joined) key algorithm under the router-owned namespaces
  (``kgi``/``cgi``) so profile-gated blocks can never be served to the legacy
  surfaces' keys (and vice-versa). Empty results are NEVER cached (§9).

Kill switch: ``VCO_INJECT_PROFILE=off`` → exit 0 silently (checked first,
beside the wrappers' own check).

Env seams (tests + Wave 2):
* ``VCO_ROUTER_KG_SCRIPT`` — path to the KG producer (default: sibling
  ``rl_kg_search.py``);
* ``VCO_CG_SCRIPT`` — path to ``query_code_graph.py`` (default:
  ``$CLAUDE_PROJECT_DIR/.claude/scripts/query_code_graph.py``, else this
  checkout's ``templates/scripts/query_code_graph.py``);
* ``VCO_INJECT_BUDGET_S`` (default 6) — whole-run inner bound (fits under
  the smallest shipped surface timeout; injection is the deliberately
  bounded, silence-safe class per the 2026-10-06 owner ruling);
* ``VCO_INJECT_LEG_TIMEOUT_S`` (default 4) — per-leg join bound;
* ``VCO_QUERY_CACHE_TTL`` (default 900) — shared with the shell cache;
* ``VCO_CG_INJECT_CAP`` (default 40) — the seen-store's per-session
  code-graph inject cap (SAME counter file, ``seen_cginject_count_<sid>.txt``).

``--intent-out FILE`` writes the classification JSON (bash surface: the
BashIntent; other surfaces: their resolved plan) — the Wave-2 pre-bash
wrapper's ``pre_bash`` outcome-event seam (WP-D 2: intent + targets in the
payload). Best-effort: an unwritable path never fails the run.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent          # claude_mcp_servers/scripts
_ORCH_ROOT = _HERE.parent.parent
for _p in (str(_ORCH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.inject_intent import (  # noqa: E402
    BUDGET_GC_AGE_S,
    CG_SESSION_CAP_DEFAULT,
    INTENT_EDIT,
    INTENT_MECHANICAL,
    INTENT_READ,
    INTENT_SEARCH,
    PER_TURN_BUDGET_CHARS,
    agent_task_section,
    budget_state_path,
    cap_block,
    cg_policy,
    classify_bash,
    clean_identifier,
    edit_enclosing_symbols,
    extract_symbol,
    file_pub_symbols,
    kg_gate,
    kg_query_for_targets,
    language_for_path,
    pattern_gate,
    sanitize_session_id,
    task_type_for,
    titles_one_liner,
)

import hook_dual_search as _hds  # noqa: E402 — sibling module (sys.path above)

SURFACES = ("bash", "read", "write", "edit", "grep", "agent")

_BASH_PROFILE_BY_INTENT = {
    INTENT_READ: "bash_read",
    INTENT_EDIT: "bash_edit",
    INTENT_SEARCH: "bash_search",
}

# (Wave-3: the dotted-extension FILENAME guard and the command-text symbol
# recovery moved INTO the classifier — inject_intent._recover_clean_symbol /
# _DOT_EXT_TOKEN_RE — one home, so the READ demotion rule can see what was
# recovered. The router keeps only the disk-based file_pub_symbols fallback.)


def _env_float(name: str, default: float) -> float:
    try:
        val = float((os.environ.get(name) or "").strip())
        return val if val > 0 else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        val = int((os.environ.get(name) or "").strip())
        return val if val > 0 else default
    except ValueError:
        return default


# --- payload parsing (the pre-bash-context-inject.sh:74-94 discipline) --------


def _first_content_string(obj) -> str:
    """Best-effort extraction of the Read tool_response text: walk the shape
    (it varies across harness versions) for the first plausible content
    string. Bounded — the response is only used for symbol extraction."""
    if isinstance(obj, str):
        return obj[:200_000]
    if isinstance(obj, dict):
        for key in ("content", "text", "file"):
            if key in obj:
                got = _first_content_string(obj[key])
                if got:
                    return got
        for val in obj.values():
            got = _first_content_string(val)
            if got:
                return got
    if isinstance(obj, list):
        for val in obj:
            got = _first_content_string(val)
            if got:
                return got
    return ""


def _resolve_project_root(payload: Dict) -> str:
    root = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if root:
        return root
    cwd = str(payload.get("cwd") or "").strip()
    return cwd or os.getcwd()


# --- query cache (router namespaces; same dir+algorithm as the shell lib) -----
# MUST MATCH _lib/query-cache.{sh,ps1}: sha1 over the args joined with 0x1f
# (each arg FOLLOWED by the separator — printf '%s\x1f' "$@"), stored under
# .claude/state/query_cache/, TTL VCO_QUERY_CACHE_TTL default 900. The
# namespaces ("kgi"/"cgi") are router-owned: profile-gated blocks must never
# be served under the legacy surfaces' keys. §9: an EMPTY result is NEVER
# written, and an empty stored file reads as a miss (self-heal).


def _cache_dir(project_root: str) -> Optional[Path]:
    try:
        d = Path(project_root) / ".claude" / "state" / "query_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def cache_key(*parts: str) -> str:
    joined = "".join(f"{p}\x1f" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def cache_get(project_root: str, key: str) -> Optional[str]:
    d = _cache_dir(project_root)
    if d is None or not key:
        return None
    f = d / key
    try:
        if not f.is_file():
            return None
        ttl = _env_int("VCO_QUERY_CACHE_TTL", 900)
        age = time.time() - f.stat().st_mtime
        if age >= ttl:
            return None
        blob = f.read_text(encoding="utf-8", errors="replace")
        if not blob:
            f.unlink(missing_ok=True)  # §9 self-heal of legacy poison
            return None
        return blob
    except OSError:
        return None


def cache_put(project_root: str, key: str, blob: str) -> None:
    if not key or not blob:  # §9: empty is NEVER cached
        return
    d = _cache_dir(project_root)
    if d is None:
        return
    # The atomic tmp+replace writer lives in ONE home (vco_lib.atomic —
    # tests/test_v0292_atomic_one_home.py ratchets hand-rolled copies).
    # fsync=False: the query cache is a performance layer, not a
    # crash-safety record — a lost entry is a stale miss, and the hook path
    # must not pay an fsync per injection.
    from vco_lib.atomic import atomic_write_text

    try:
        atomic_write_text(d / key, blob, fsync=False)
    except OSError:
        pass


# --- seen-store (Python parity with _lib/seen-store.sh) ------------------------
# MUST MATCH templates/hooks/_lib/seen-store.sh:
#   * file names  seen_inject_<sid>.txt / seen_reads_<sid>.txt under
#     .claude/state/ (vco_seen_store_path);
#   * KG key      "<title>#<sha1(NORMALIZED body)[:12]>" — normalized body =
#     trailing newline run collapsed to exactly one (empty stays empty);
#   * CODE key    "<full_name>" (first " | "-delimited header field);
#   * key fields  capped at 200 UTF-8 BYTES on a character boundary
#     (vco_cap_key_field);
#   * src rule    the LAST "| src=" occurrence, trailing whitespace trimmed;
#     reads-ledger match tries BOTH path shapes (as-is + the other of
#     absolute/repo-relative — vco_seen_src_matches);
#   * blind mode  an untrustworthy session ("" / "default") gets NO store:
#     every block passes, nothing is recorded.
# tests/test_v02101_inject_gates.py::TestSeenStoreParity drives BOTH
# implementations over one corpus.

_HEADER_RE = re.compile(r"^(KG|CODE): (.+)$")


def seen_store_path(kind: str, session_id: str, project_root: str) -> str:
    """Mirror of vco_seen_store_path: "" when the session is untrustworthy."""
    if not session_id or session_id == "default" or not project_root:
        return ""
    return os.path.join(project_root, ".claude", "state",
                        f"seen_{kind}_{session_id}.txt")


def cap_key_field(text: str) -> str:
    """Mirror of vco_cap_key_field: 200 UTF-8 bytes, cut on a character
    boundary (continuation bytes 0b10xxxxxx are backed off)."""
    b = text.encode("utf-8")
    if len(b) <= 200:
        return text
    cut = 200
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    return b[:cut].decode("utf-8", "ignore")


def normalize_block_body(body: str) -> str:
    """Mirror of vco_seen_normalize_body: strip the trailing newline RUN,
    restore exactly one (empty body stays empty)."""
    stripped = body.rstrip("\n")
    return stripped + "\n" if stripped else ""


def _seen_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh]
    except OSError:
        return []


def _src_matches(reads_file: str, src: str, project_root: str) -> bool:
    """Mirror of vco_seen_src_matches (both path shapes, exact match only)."""
    if not reads_file or not src:
        return False
    lines = _seen_lines(reads_file)
    if src in lines:
        return True
    if not project_root:
        return False
    if src.startswith("/"):
        root_prefix = project_root.rstrip("/") + "/"
        if src.startswith(root_prefix) and src[len(root_prefix):] in lines:
            return True
    else:
        if (project_root.rstrip("/") + "/" + src) in lines:
            return True
    return False


def filter_seen_blocks(text: str, inject_file: str, reads_file: str,
                       project_root: str = "") -> str:
    """Mirror of vco_filter_seen_blocks. Parses the KG:/CODE: block stream,
    suppresses already-seen / already-Read blocks, records emitted keys."""
    dedup_on = bool(inject_file)
    if dedup_on:
        try:
            with open(inject_file, "a", encoding="utf-8"):
                pass  # touch (the shell's `touch "$inject_file"` gate)
        except OSError:
            dedup_on = False

    out: List[str] = []
    cur_prefix = ""
    cur_first = ""
    cur_src = ""
    cur_block: List[str] = []
    cur_body: List[str] = []

    def _flush() -> None:
        nonlocal cur_prefix, cur_first, cur_src, cur_block, cur_body
        if not cur_prefix:
            cur_block, cur_body = [], []
            return
        if cur_prefix == "KG":
            body_norm = normalize_block_body("".join(cur_body))
            bh = hashlib.sha1(body_norm.encode("utf-8")).hexdigest()[:12]
            key = f"{cur_first}#{bh}"
        else:
            key = cur_first
        suppress = False
        if dedup_on:
            if key in _seen_lines(inject_file):
                suppress = True
            if not suppress and cur_src and reads_file and \
                    _src_matches(reads_file, cur_src, project_root):
                suppress = True
        if not suppress:
            out.extend(cur_block)
            if dedup_on:
                try:
                    with open(inject_file, "a", encoding="utf-8") as fh:
                        fh.write(key + "\n")
                except OSError:
                    pass
        cur_prefix, cur_first, cur_src = "", "", ""
        cur_block, cur_body = [], []

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the herestring's trailing newline, not a real line
    for line in lines:
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            cur_prefix = m.group(1)
            rest = m.group(2)
            cur_first = cap_key_field(rest.split(" | ", 1)[0])
            cur_src = ""
            if "| src=" in rest:
                cur_src = rest.split("| src=")[-1].rstrip()
            cur_block = [line + "\n"]
            cur_body = []
        elif cur_prefix:
            cur_block.append(line + "\n")
            cur_body.append(line + "\n")
        else:
            if line.strip():
                out.append(line + "\n")
    _flush()
    return "".join(out)


# --- per-session code-graph cap (the seen-store's counter file) ---------------
# MUST MATCH _lib/seen-store.sh vco_cg_inject_count_path / vco_cg_inject_cap /
# vco_cg_inject_capped / vco_cg_inject_record.


def _cg_count_path(session_id: str, project_root: str) -> str:
    if not session_id or session_id == "default" or not project_root:
        return ""
    return os.path.join(project_root, ".claude", "state",
                        f"seen_cginject_count_{session_id}.txt")


def _cg_cap() -> int:
    return _env_int("VCO_CG_INJECT_CAP", CG_SESSION_CAP_DEFAULT)


def _cg_capped(count_file: str) -> bool:
    if not count_file or not os.path.isfile(count_file):
        return False  # fail-open: a broken counter never blocks injection
    try:
        with open(count_file, "r", encoding="utf-8") as fh:
            n = int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return False
    return n >= _cg_cap()


def _cg_record(count_file: str) -> None:
    """Read-modify-write under the shared lock (GLM re-review nit-5 — the
    same race class the budget charge fixed). Fail-open: a lost increment
    only delays the session cap; it never blocks or crashes a hook."""
    if not count_file:
        return
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    try:
        with exclusive_file_lock(Path(count_file + ".lock"), timeout_s=0.5):
            n = 0
            try:
                with open(count_file, "r", encoding="utf-8") as fh:
                    n = int((fh.read() or "0").strip() or 0)
            except (OSError, ValueError):
                n = 0
            with open(count_file, "w", encoding="utf-8") as fh:
                fh.write(f"{n + 1}\n")
    except (OSError, ValueError, LockTimeout):
        pass


# --- per-turn budget ----------------------------------------------------------
# The state file is the SAME one _lib/inject-budget.{sh,ps1} resolve
# (vco_lib.inject_intent.budget_state_path is the one home for the shape).


def _budget_used(path: Optional[str]) -> int:
    if not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _budget_charge(path: Optional[str], chars: int) -> None:
    """Read-modify-write UNDER the shared cross-platform lock (GLM review
    nit-4): concurrent hook processes in one turn would otherwise race the
    counter and undercount. Bounded wait (0.5 s) — an unchargeable budget
    degrades to under-counting, never to blocking or crashing the hook."""
    if not path or chars <= 0:
        return
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with exclusive_file_lock(Path(path + ".lock"), timeout_s=0.5):
            used = _budget_used(path)  # READ before the "w" open truncates
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(used + chars))
    except (OSError, ValueError, LockTimeout):
        pass


def _budget_gc(project_root: str) -> None:
    """ opportunistic 1-day GC of budget files (bounded scan)."""
    try:
        state = Path(project_root) / ".claude" / "state"
        cutoff = time.time() - BUDGET_GC_AGE_S
        for f in list(state.glob("inject_budget_*"))[:200]:
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass


# --- plan building (per surface) ----------------------------------------------


def _kg_limit(profile: str) -> int:
    """KG --limit for a profile: its gate's row cap (3 by table; the gate is
    non-None whenever a plan sets kg_profile, but the helper keeps the
    Optional contract honest for the type checker and future callers)."""
    gate = kg_gate(profile)
    return (gate.max_rows if gate else 0) or 3


class _Plan:
    """What the router will query for one hook event (pure data)."""

    def __init__(self) -> None:
        self.kg_query: str = ""
        self.kg_profile: str = ""          # "" → no KG leg
        self.kg_limit: int = 3
        self.cg_symbols: List[Tuple[str, str]] = []   # (symbol, source_file)
        self.cg_profile: str = ""          # "" → no CG leg
        self.rev_pin: str = ""             # resolved sha the CG stamp must match
        self.rev_gate: bool = False        # enforce the stamp comparison
        self.intent_out: dict = {}


def _resolve_rev(rev: str, cwd: str) -> str:
    """``git rev-parse --verify <rev>^{commit}`` — "" when unresolvable.
    Timeout 5 s: a local git metadata call (milliseconds when healthy) sized
    with headroom for slow storage, per the 2026-10-06 owner ruling on
    shipped timeouts — generous for the operation's real cost class."""
    if not rev or not cwd:
        return ""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--verify", f"{rev}^{{commit}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    sha = (r.stdout or "").strip()
    return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", sha) else ""


def _bash_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    command = str((payload.get("tool_input") or {}).get("command") or "")
    bi = classify_bash(command, cwd)
    plan.intent_out = bi.to_dict()
    if bi.intent == INTENT_MECHANICAL:
        return plan  # no query, no injection, no RL retrieval event
    profile = _BASH_PROFILE_BY_INTENT[bi.intent]

    # KG leg (none on SEARCH — §2.1)
    if kg_gate(profile) is not None:
        if bi.write_snippet:
            query = bi.write_snippet[:200]
        else:
            query = kg_query_for_targets(bi.targets, cwd)
        if query:
            plan.kg_query = query
            plan.kg_profile = profile
            plan.kg_limit = _kg_limit(profile)

    # CG leg — exact symbols only (§2.1 "exact def+callers")
    policy = cg_policy(profile)
    if policy.enabled:
        symbols: List[str] = list(bi.symbols)
        # Wave-3: command-text symbol recovery moved INTO the classifier
        # (inject_intent._recover_clean_symbol — one home, so the READ
        # demotion rule can see what was recovered). What remains here is
        # the disk-dependent fallback: a source-file target's own top-level
        # symbols ("only if a clean symbol is recovered; else silent" —
        # recovery from the TARGET is the owner-rule shape: never from
        # command prose).
        if bi.intent in (INTENT_READ, INTENT_EDIT) and not symbols:
            cap = 2 if bi.intent == INTENT_READ else 3
            for t in bi.targets:
                abs_t = t if os.path.isabs(t) else os.path.join(cwd, t) if cwd else t
                if language_for_path(abs_t) and os.path.isfile(abs_t):
                    symbols.extend(file_pub_symbols(abs_t)[:cap])
                    if symbols:
                        break
        if policy.require_clean_symbol:
            symbols = [s for s in symbols if clean_identifier(s)]
        source_file = bi.targets[0] if bi.targets else ""
        plan.cg_symbols = [(s, source_file) for s in symbols[:3]]
        if plan.cg_symbols:
            plan.cg_profile = profile
        # Revision pin: `git show <rev>:<path>` reads — resolve the rev now;
        # an UNRESOLVABLE rev means the CG stamp can never be confirmed →
        # the CG leg stays silent (rev_gate with rev_pin "" drops everything).
        if policy.revision_stamp and bi.rev_paths:
            rev, _path = bi.rev_paths[0]
            plan.rev_gate = True
            plan.rev_pin = _resolve_rev(rev, cwd)
    return plan


def _read_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or "")
    if not file_path:
        return plan
    content = _first_content_string(payload.get("tool_response"))
    profile = "read_code" if language_for_path(file_path) else "read_docs"
    plan.intent_out = {"surface": "read", "file_path": file_path, "profile": profile}
    if kg_gate(profile) is not None:
        query = kg_query_for_targets([file_path], cwd)
        if query:
            plan.kg_query = query
            plan.kg_profile = profile
            plan.kg_limit = _kg_limit(profile)
    policy = cg_policy(profile)
    if policy.enabled:
        syms = file_pub_symbols(file_path, content or None)[:5]
        plan.cg_symbols = [(s, file_path) for s in syms]
        if plan.cg_symbols:
            plan.cg_profile = profile
    return plan


def _write_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or "")
    if not file_path:
        return plan
    plan.intent_out = {"surface": "write", "file_path": file_path}
    profile = "write"
    if kg_gate(profile) is not None:
        # KG keyed on module name + sibling dir (the path topic), never on
        # the content (§C3: a new file's own text is not a useful query).
        query = kg_query_for_targets([file_path], cwd)
        if query:
            plan.kg_query = query
            plan.kg_profile = profile
            plan.kg_limit = _kg_limit(profile)
    policy = cg_policy(profile)
    if policy.enabled and os.path.isfile(file_path):
        # A REWRITE: the on-disk file's symbols are in the graph. A brand-new
        # file's symbols are not indexed yet — no CG leg (querying them would
        # return nothing or, worse, a same-named stranger).
        syms = file_pub_symbols(file_path)[:3]
        plan.cg_symbols = [(s, file_path) for s in syms]
        if plan.cg_symbols:
            plan.cg_profile = profile
    return plan


def _edit_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or "")
    old_string = str(tool_input.get("old_string") or "")
    if not file_path:
        return plan
    profile = "edit"
    symbols = edit_enclosing_symbols(file_path, old_string) if old_string else []
    plan.intent_out = {"surface": "edit", "file_path": file_path,
                       "symbols": symbols}
    if kg_gate(profile) is not None:
        topic = kg_query_for_targets([file_path], cwd)
        parts = [p for p in (topic, " ".join(symbols)) if p]
        query = " ".join(parts)[:200]
        if query:
            plan.kg_query = query
            plan.kg_profile = profile
            plan.kg_limit = _kg_limit(profile)
    policy = cg_policy(profile)
    if policy.enabled and symbols:
        plan.cg_symbols = [(s, file_path) for s in symbols[:3]]
        plan.cg_profile = profile
    return plan


def _grep_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    tool_input = payload.get("tool_input") or {}
    pattern = str(tool_input.get("pattern") or "")
    plan.intent_out = {"surface": "grep", "pattern_is_identifier": bool(pattern_gate(pattern))}
    if not pattern or not pattern_gate(pattern):
        return plan
    sym = clean_identifier(extract_symbol(pattern)) or clean_identifier(pattern)
    if not sym:
        return plan  # a regex fragment is not an exact-lookup key (pub(crate) class)
    plan.intent_out["symbol"] = sym
    policy = cg_policy("grep")
    if policy.enabled:
        source = str(tool_input.get("path") or "")
        plan.cg_symbols = [(sym, source if os.path.isfile(source) else "")]
        plan.cg_profile = "grep"
    return plan  # no KG leg on grep (§2.1)


def _agent_plan(payload: Dict, cwd: str) -> _Plan:
    plan = _Plan()
    tool_input = payload.get("tool_input") or {}
    prompt = str(tool_input.get("prompt") or "") or str(tool_input.get("description") or "")
    task = agent_task_section(prompt)
    plan.intent_out = {"surface": "agent", "has_task": bool(task)}
    if not task:
        return plan
    profile = "agent_brief"
    gate = kg_gate(profile)
    if gate is not None:
        plan.kg_query = task[:400]
        plan.kg_profile = profile
        plan.kg_limit = gate.max_rows or 3
    return plan  # no CG leg on the agent surface (§2.1: keep the brief small)


_PLAN_BUILDERS = {
    "bash": _bash_plan,
    "read": _read_plan,
    "write": _write_plan,
    "edit": _edit_plan,
    "grep": _grep_plan,
    "agent": _agent_plan,
}


# --- legs ---------------------------------------------------------------------


def _resolve_kg_script() -> Path:
    env = (os.environ.get("VCO_ROUTER_KG_SCRIPT") or "").strip()
    if env:
        return Path(env)
    return _HERE / "rl_kg_search.py"


def _resolve_cg_script(project_root: str) -> Optional[Path]:
    env = (os.environ.get("VCO_CG_SCRIPT") or "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if project_root:
        p = Path(project_root) / ".claude" / "scripts" / "query_code_graph.py"
        if p.is_file():
            return p
    p = _ORCH_ROOT / "templates" / "scripts" / "query_code_graph.py"
    return p if p.is_file() else None


def _cg_argv(symbol: str, source_file: str, policy, profile: str) -> List[str]:
    argv = ["structure", "callers", symbol, "--hook-format"]
    if policy.revision_stamp:
        argv.append("--indexed-revision")
    if source_file:
        argv += ["--source-file", source_file]
        if policy.exclude_self_file:
            argv += ["--exclude-file", source_file]
    return argv


def _run_legs(plan: _Plan, sid: str, prompt_id: str, project_root: str,
              transcript_path: str, deadline: float) -> Tuple[str, str]:
    """Run the enabled (cache-missing) legs concurrently; return (kg, cg)
    raw text. Soft-fail everywhere: any producer problem yields ""."""
    kg_text = ""
    cg_text = ""
    kg_key = ""
    cg_keys: Dict[str, str] = {}

    if plan.kg_profile and plan.kg_query:
        kg_key = cache_key("kgi", plan.kg_profile, plan.kg_query,
                           str(plan.kg_limit), prompt_id)
        cached = cache_get(project_root, kg_key)
        if cached is not None:
            kg_text = cached
            kg_key = ""  # served; nothing to put back
    policy = cg_policy(plan.cg_profile) if plan.cg_profile else None
    cg_symbol_texts: List[str] = []
    if policy is not None and policy.enabled and plan.cg_symbols:
        count_file = _cg_count_path(sid, project_root)
        if _cg_capped(count_file):
            plan.cg_symbols = []  # session cap reached — skip the leg entirely
            plan.intent_out["cg_capped"] = True
        else:
            for symbol, source_file in plan.cg_symbols:
                key = cache_key("cgi", plan.cg_profile, symbol, source_file, prompt_id)
                cached = cache_get(project_root, key)
                if cached is not None:
                    cg_symbol_texts.append(cached)
                else:
                    cg_keys[symbol] = key
    else:
        plan.cg_symbols = []

    legs: Dict[str, Callable[[], None]] = {}

    if kg_key:
        kg_script = _resolve_kg_script()
        if kg_script.is_file():
            try:
                kg_mod = _hds._load_cg_module(kg_script)
                argv = [plan.kg_query, "--limit", str(plan.kg_limit),
                        "--hook-format", "--injection-profile", plan.kg_profile,
                        "--task-type", task_type_for(plan.kg_profile)]
                if transcript_path:
                    argv += ["--transcript", transcript_path]
                _hds._pin_argv(kg_mod, argv)

                def _kg_leg(mod=kg_mod) -> None:
                    # rl_kg_search.main is async; the VCO_ROUTER_KG_SCRIPT
                    # test seam may point at a SYNC stub producer — accept
                    # both (the documented producer contract is "a main()
                    # whose argv is pinned", not "an async main").
                    result = mod.main()
                    if inspect.iscoroutine(result):
                        asyncio.run(result)

                legs["kg"] = _kg_leg
            except BaseException as exc:  # noqa: BLE001 — never block the tool
                print(f"[hook_context_router] KG leg load failed: {exc!r}",
                      file=sys.stderr)

    if cg_keys:
        cg_script = _resolve_cg_script(project_root)
        if cg_script is not None:
            try:
                cg_mod = _hds._load_cg_module(cg_script)

                def _cg_leg(mod=cg_mod, items=tuple(cg_keys.items()), pol=policy) -> None:
                    for symbol, _key in items:
                        source_file = next(
                            (sf for sym, sf in plan.cg_symbols if sym == symbol), "")
                        _hds._pin_argv(mod, _cg_argv(symbol, source_file, pol, plan.cg_profile))
                        mod.main()

                legs["cg"] = _cg_leg
            except BaseException as exc:  # noqa: BLE001
                print(f"[hook_context_router] CG leg load failed: {exc!r}",
                      file=sys.stderr)

    if legs:
        remaining = deadline - time.monotonic()
        leg_timeout = min(_env_float("VCO_INJECT_LEG_TIMEOUT_S", 4.0),
                          max(remaining, 0.1))
        results = _hds.run_legs(legs, leg_timeout_s=leg_timeout)
        if kg_key:
            kg_text = (results.get("kg") or "").strip()
            if kg_text:
                cache_put(project_root, kg_key, kg_text + "\n")
                kg_text = kg_text + "\n"
        if cg_keys:
            raw = (results.get("cg") or "").strip()
            if raw:
                # One combined blob for the run (the per-symbol cache entries
                # are keyed on the symbol; a combined blob is cached under
                # each requested symbol's key ONLY when it was the sole
                # query — multi-symbol runs skip the cache to avoid
                # cross-symbol poisoning).
                cg_symbol_texts.append(raw + "\n")
                if len(cg_keys) == 1:
                    only_key = next(iter(cg_keys.values()))
                    cache_put(project_root, only_key, raw + "\n")
    cg_text = "".join(cg_symbol_texts)
    return kg_text, cg_text


# --- emission -----------------------------------------------------------------


def _cg_revision_stamp(cg_text: str) -> Optional[str]:
    for line in cg_text.splitlines():
        if line.startswith("CODE-REV:"):
            return line.split(":", 1)[1].strip()
    return None


def _strip_stamp_lines(text: str) -> str:
    return "".join(ln + "\n" for ln in text.splitlines()
                   if not ln.startswith("CODE-REV:"))


def _emit_agent(tool_input: Dict, prompt: str, kg_text: str) -> str:
    """The agent envelope: EVERY original tool_input field echoed verbatim,
    only `prompt` modified, NO permissionDecision (never auto-approve).
    Budget degradation already happened upstream (the per-block titles
    degrade) — this function must not re-transform the text."""
    updated = dict(tool_input)  # JSON round-trip preserves unknown fields
    injected = kg_text.strip()
    updated["prompt"] = f"{prompt}\n\n[KG context for this task]:\n{injected}"
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        }
    }, ensure_ascii=False)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except BaseException as exc:  # noqa: BLE001 — exit 0 ALWAYS; a retrieval
        # failure is silence, never a blocked tool call.
        print(f"[hook_context_router] soft-fail: {exc!r}", file=sys.stderr)
        return 0


def _main(argv: List[str]) -> int:
    surface = argv[0] if argv else ""
    if surface not in SURFACES:
        return 0
    intent_out = ""
    if "--intent-out" in argv:
        idx = argv.index("--intent-out")
        if idx + 1 < len(argv):
            intent_out = argv[idx + 1]

    # Kill switch first (beside the wrappers' own check): off → total silence.
    if (os.environ.get("VCO_INJECT_PROFILE") or "").strip().lower() == "off":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    sid = sanitize_session_id(str(payload.get("session_id") or ""))
    prompt_raw = str(payload.get("prompt_id") or "")
    prompt_id = prompt_raw if re.fullmatch(r"[A-Za-z0-9_-]+", prompt_raw) else ""
    transcript_path = str(payload.get("transcript_path") or "")
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    project_root = _resolve_project_root(payload)

    # VCT_SESSION_ID export discipline (pre-edit-context-inject.sh:156-169):
    # the RL telemetry's 3-layer session chain reads it from the env; skip
    # the "default" sentinel — rather empty than a fake-key cohort.
    if sid and sid != "default":
        os.environ.setdefault("VCT_SESSION_ID", sid)

    plan = _PLAN_BUILDERS[surface](payload, cwd)

    def _write_intent_out() -> None:
        """The Wave-2 wrapper's outcome-event seam (WP-D 2: intent +
        targets/symbols in the pre_bash payload). Written on EVERY exit path
        after the legs ran, so late facts (cg_capped) are included.
        Best-effort: an unwritable path never fails the run."""
        if intent_out and plan.intent_out:
            try:
                with open(intent_out, "w", encoding="utf-8") as fh:
                    json.dump(plan.intent_out, fh)
                    fh.write("\n")
            except OSError:
                pass

    if not plan.kg_profile and not plan.cg_profile:
        _write_intent_out()
        return 0  # MECHANICAL / no recoverable query → no spawn, no event

    _budget_gc(project_root)
    budget_path = budget_state_path(sid, prompt_id, project_root)
    used = _budget_used(budget_path)
    over_budget = used >= PER_TURN_BUDGET_CHARS

    # Inner bounds (env-tunable). OWNER RULING 2026-10-06: interactive
    # per-tool-call injection is the DELIBERATELY-BOUNDED, silence-safe
    # class (all its settings timeouts sit at 10 s since the wave-2 review's
    # SF-1) — but the ORDERING must hold with headroom: budget + startup +
    # emit must fit under the harness timeout on SLOW hardware too, or the
    # hook is killed mid-run and the injection is lost (the §9 always-killed
    # root cause). The plan-faithful 6/4 defaults leave ~4 s for interpreter
    # startup + producer imports + emit under the 10 s registrations (an
    # earlier 8/6 pairing left only ~2 s — measured cold producer imports of
    # 4.7-11.6 s on THIS high-end machine say 2 s is not enough headroom for
    # third-party hardware). A cold run that still exceeds the harness
    # timeout fails OPEN to silence — accepted for this bounded class, and
    # never a licence to tighten the LONG-operation timeouts elsewhere
    # (kg-sync / code-graph analysis / install / seed stay generous).
    deadline = time.monotonic() + _env_float("VCO_INJECT_BUDGET_S", 6.0)
    kg_text, cg_text = _run_legs(plan, sid, prompt_id, project_root,
                                 transcript_path, deadline)
    _write_intent_out()

    # Revision gate (wave-4 caveat (a)): a pinned `git show <rev>` read gets
    # CG context ONLY when the graph's stamp matches the resolved rev. An
    # unresolvable rev or an unknown/absent stamp → SILENT (never inject
    # HEAD-derived callers into a pinned-commit review).
    if plan.rev_gate and cg_text:
        stamp = _cg_revision_stamp(cg_text)
        confirmed = bool(plan.rev_pin) and bool(stamp) and stamp != "unknown" and (
            plan.rev_pin.startswith(stamp) or stamp.startswith(plan.rev_pin)
        )
        if not confirmed:
            cg_text = ""
    if cg_text:
        cg_text = _strip_stamp_lines(cg_text)

    combined = kg_text + ("\n" if kg_text and cg_text else "") + cg_text
    if not combined.strip():
        return 0

    inject_file = seen_store_path("inject", sid, project_root)
    reads_file = seen_store_path("reads", sid, project_root)
    filtered = filter_seen_blocks(combined, inject_file, reads_file, project_root)
    if not filtered.strip():
        return 0

    # Per-injection soft cap + per-turn budget (degrade, never drop: past
    # budget the blocks become titles one-liners, §2.1).
    blocks = [b for b in re.split(r"\n(?=(?:KG|CODE): )", filtered) if b.strip()]
    out_blocks: List[str] = []
    emitted = 0
    for block in blocks:
        text = cap_block(block.rstrip("\n"))
        if over_budget or used + emitted + len(text) > PER_TURN_BUDGET_CHARS:
            text = titles_one_liner(text) or text
            if not text:
                continue
        out_blocks.append(text)
        emitted += len(text) + 1

    final = "\n".join(out_blocks)
    if not final.strip():
        return 0

    if cg_text and any(b.startswith("CODE:") for b in out_blocks):
        _cg_record(_cg_count_path(sid, project_root))
    _budget_charge(budget_path, emitted)

    if surface == "agent":
        tool_input = payload.get("tool_input") or {}
        # GLM review nit-3: `description` is a QUERY-BUILDING fallback only
        # (§C4) — an envelope is emitted ONLY when there is a real `prompt`
        # field to append to; the router never synthesizes a field that did
        # not exist in the tool input.
        if "prompt" not in tool_input:
            return 0
        prompt = str(tool_input.get("prompt") or "")
        # The agent brief's own bound (≤1 500 chars) was applied inside the
        # producer (KgGate.max_chars); the budget degradation happened in the
        # per-block loop above.
        print(_emit_agent(tool_input, prompt, final))
        return 0

    print(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
