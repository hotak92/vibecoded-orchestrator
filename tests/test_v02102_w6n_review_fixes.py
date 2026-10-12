# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-N review fix round (V02102-W6N-W6O-REVIEW.md, "W6-N findings").

* HIGH-1: a failed ``claude -p --output-format json`` run was classified on
  its RAW JSON tail, whose ``duration_ms`` / token counts / uuids carry
  ``401`` / ``429`` / ``503`` digits — a false auth or rate-limit latch. The
  breaker now reads only the parsed ``result`` string plus stderr, and an
  ``error_max_turns`` run (the client's own turn budget, the backend served
  every turn) neither strikes nor latches.
* MEDIUM-2: the prompt-shaped definition covers the instruction files an
  ordinary project has (CLAUDE.md, AGENTS.md, Copilot / Cursor rules, output
  styles, skill supporting files) and ``.claude/settings.json`` edits that
  touch a ``prompt`` / ``agent`` hook; the VCO-only ``templates/**/agents``
  rule is gone.
* MEDIUM-3: under ``set -o pipefail`` a diff larger than the pipe buffer
  made ``git … | head`` die of SIGPIPE and ``set -e`` killed the hook before
  the reviewer spawned.
* LOW-5: the turn-budget knob parses identically in Python, ``.sh`` and
  ``.ps1`` (digits only, no sign / blanks / underscores, int64 range).
* NIT-6: the before-edit NOTE block is charged to the per-turn budget and
  capped like every other injected block.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tests.test_v02102_w6n_kg_reviewer_fixes import (
    SCRIPTS_DIR,
    _argv_and_prompt,
    _load,
    _run_hook,
    _scratch_repo,
    _wait_capture,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

HAIKU = {"mode": "claude", "model": "haiku", "base_url": None, "reason": "t"}


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The chain with its breaker file in ``tmp_path`` (the F-1..F-4 rig's
    fixture, loaded under this module's own names)."""
    for p in (str(SCRIPTS_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sb = _load("_w6nrf_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_w6nrf_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    monkeypatch.delenv("VCO_KG_REVIEW_MAX_TURNS", raising=False)
    yield mod
    sb.reset_backend_cache()

# The documented ``--output-format json`` result object (Claude Code
# headless docs / Agent SDK ``SDKResultMessage``): a success carries a
# ``result`` string; the error subtypes (``error_max_turns``,
# ``error_during_execution``) carry NO ``result``. Every one carries
# ``duration_ms`` / ``duration_api_ms`` / ``num_turns`` / ``session_id`` /
# ``total_cost_usd`` / ``usage`` — the numbers that used to be classified.
_NOISY_META = {
    "type": "result", "is_error": True,
    "duration_ms": 214031, "duration_api_ms": 42900,
    "num_turns": 30, "session_id": "4291-8a50-503c-529e-401f",
    "total_cost_usd": 0.0429,
    "usage": {"input_tokens": 4013, "output_tokens": 529,
              "cache_read_input_tokens": 50300},
}


def _run(chain, monkeypatch, tmp_path, *, exit_code: int, stdout: str,
         stderr: str = "") -> dict:
    """One ``run`` through the REAL breaker; returns the breaker file."""
    def _fake_run(cmd, **kw):
        import subprocess
        return subprocess.CompletedProcess(cmd, exit_code, stdout=stdout,
                                           stderr=stderr)
    monkeypatch.setattr(chain, "select_reviewer_backend",
                        lambda log=None: HAIKU)
    monkeypatch.setattr(chain.subprocess, "run", _fake_run)
    monkeypatch.delenv("VCO_SUMMARY_BREAKER", raising=False)
    pf = tmp_path / "prompt.txt"
    pf.write_text("review", encoding="utf-8")
    chain.main(["run", "--prompt-file", str(pf),
                "--log", str(tmp_path / "review.log")])
    path = tmp_path / "breaker.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _tier_record(chain, data: dict) -> "dict | None":
    return data.get(chain._tier_for("haiku"))


# ── HIGH-1 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("subtype", ["error_during_execution", "success"])
def test_high1_json_digits_never_latch_the_tier(chain, monkeypatch, tmp_path,
                                                subtype):
    """RED before the fix: the raw tail's ``401`` / ``429`` / ``503`` digits
    were read as an auth / rate-limit / capacity signal."""
    out = json.dumps({**_NOISY_META, "subtype": subtype})
    data = _run(chain, monkeypatch, tmp_path, exit_code=1, stdout=out)
    rec = _tier_record(chain, data)
    assert rec is None or (rec.get("reason") == "other"
                           and not rec.get("until")), data
    assert chain._sb.breaker_state(chain._tier_for("haiku")) is None


def test_high1_max_turns_is_not_a_backend_failure(chain, monkeypatch,
                                                  tmp_path):
    """The budget ran out on the CLIENT: no strike, no latch — and it
    proves the tier served, so a prior strike record is cleared."""
    tier = chain._tier_for("haiku")
    chain._sb.trip_backend(tier, "other")  # one earlier unknown strike
    assert _tier_record(chain, json.loads(
        (tmp_path / "breaker.json").read_text(encoding="utf-8")))
    out = json.dumps({**_NOISY_META, "subtype": "error_max_turns"})
    data = _run(chain, monkeypatch, tmp_path, exit_code=1, stdout=out)
    assert _tier_record(chain, data) is None, data
    log = (tmp_path / "review.log").read_text(encoding="utf-8")
    assert "subtype=error_max_turns" in log


def test_high1_text_mode_max_turns_line_is_not_a_failure(chain):
    """The no-JSON shape (an older CLI / the ``record`` path)."""
    served, _text = chain.run_failure_evidence(
        "Error: Reached max turns (30)\n", "")
    assert served is True


def test_high1_real_signal_in_result_still_latches(chain, monkeypatch,
                                                   tmp_path):
    """The classifier still sees the real error: the ``result`` string."""
    out = json.dumps({**_NOISY_META, "subtype": "success",
                      "result": 'API Error: 401 {"type":"error","error":'
                                '{"type":"authentication_error"}}'})
    data = _run(chain, monkeypatch, tmp_path, exit_code=1, stdout=out)
    rec = _tier_record(chain, data)
    assert rec and rec.get("reason") == "auth", data


def test_high1_real_signal_on_stderr_still_latches(chain, monkeypatch,
                                                   tmp_path):
    out = json.dumps({**_NOISY_META, "subtype": "error_during_execution"})
    data = _run(chain, monkeypatch, tmp_path, exit_code=1, stdout=out,
                stderr="Error: 429 Too Many Requests")
    rec = _tier_record(chain, data)
    assert rec and rec.get("reason") == "rate_limit", data


def test_high1_success_clears_the_tier(chain, monkeypatch, tmp_path):
    tier = chain._tier_for("haiku")
    chain._sb.trip_backend(tier, "other")
    out = json.dumps({**_NOISY_META, "is_error": False, "subtype": "success",
                      "result": "done"})
    assert _tier_record(chain, _run(chain, monkeypatch, tmp_path,
                                    exit_code=0, stdout=out)) is None


# ── MEDIUM-3 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_medium3_large_commit_still_spawns_the_reviewer(tmp_path, sibling):
    """RED on the .sh before the fix: ``git diff | head -300`` on a diff far
    bigger than the 64 KiB pipe buffer died of SIGPIPE, pipefail made the
    capture fail and ``set -e`` exited before the spawn."""
    import subprocess
    repo, env = _scratch_repo(tmp_path, commits=1)
    big = repo / "big_file.txt"
    big.write_text("".join(f"large-diff-line-{i:06d}-" + "x" * 60 + "\n"
                           for i in range(20000)), encoding="utf-8")
    assert big.stat().st_size > 64 * 1024 * 10
    for args in (("add", "big_file.txt"), ("commit", "-q", "-m", "big")):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True)
    _run_hook(sibling, repo, env)
    _argv, everything = _argv_and_prompt(_wait_capture(env))
    assert "+large-diff-line-000000-" in everything
    assert "large-diff-line-019999" not in everything  # still capped


# ── LOW-5 ────────────────────────────────────────────────────────────────

#: Values the three parsers used to disagree on: Python's int() took them,
#: the .sh case pattern / the .ps1 regex refused them (or .ps1 trimmed).
_EDGE = ["+5", " 5 ", "1_000", "5 ", "99999999999999999999"]


@pytest.mark.parametrize("raw", _EDGE)
def test_low5_python_refuses_what_the_mirrors_refuse(chain, monkeypatch,
                                                     raw):
    monkeypatch.setenv("VCO_KG_REVIEW_MAX_TURNS", raw)
    assert chain.resolve_max_turns() == 30


@pytest.mark.parametrize("raw, expected", [("12", 12), ("007", 7),
                                           ("9223372036854775807",
                                            9223372036854775807)])
def test_low5_python_accepts_plain_digits(chain, monkeypatch, raw, expected):
    monkeypatch.setenv("VCO_KG_REVIEW_MAX_TURNS", raw)
    assert chain.resolve_max_turns() == expected


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
@pytest.mark.parametrize("raw", ["+5", " 5 ", "1_000"])
def test_low5_hook_mirrors_agree_with_python(tmp_path, sibling, raw, chain,
                                             monkeypatch):
    """Parity pin: the no-python branch of each sibling resolves the SAME
    budget Python does for a value Python's int() alone would accept."""
    monkeypatch.setenv("VCO_KG_REVIEW_MAX_TURNS", raw)
    expected = str(chain.resolve_max_turns())
    repo, env = _scratch_repo(tmp_path, commits=1)
    env["VCO_KG_REVIEW_MAX_TURNS"] = raw
    _run_hook(sibling, repo, env)
    argv, _ = _argv_and_prompt(_wait_capture(env))
    assert argv[argv.index("--max-turns") + 1] == expected == "30", argv


# ── MEDIUM-2 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel, kind", [
    ("CLAUDE.md", "instruction file"),
    ("CLAUDE.local.md", "instruction file"),
    ("packages/api/CLAUDE.md", "instruction file"),
    ("AGENTS.md", "instruction file"),
    ("GEMINI.md", "instruction file"),
    (".github/copilot-instructions.md", "instruction file"),
    (".github/instructions/python.instructions.md", "instruction file"),
    (".github/chatmodes/plan.chatmode.md", "instruction file"),
    (".cursor/rules/style.mdc", "instruction file"),
    (".cursor/rules/team/backend.mdc", "instruction file"),
    (".claude/output-styles/terse.md", "output style"),
    (".claude/skills/fix-issue/reference.md", "skill"),
    (".claude/skills/fix-issue/templates/report.md", "skill"),
    (".claude/agents/code-reviewer.md", "agent definition"),
    (".claude/commands/review.md", "slash command"),
])
def test_medium2_instruction_files_are_prompt_shaped(rel, kind, tmp_path):
    from vco_lib.prompt_edit_nudge import is_prompt_shaped, prompt_kind
    assert prompt_kind(str(tmp_path / rel), str(tmp_path)) == kind
    assert is_prompt_shaped(rel, "") is True


@pytest.mark.parametrize("rel", [
    "templates/agents/free/expert-coder.md",   # the VCO-only rule, dropped
    "docs/CLAUDE-notes.md",
    "claude.md.bak",
    ".github/instructions/README.md",
    ".github/workflows/ci.yml",
    ".cursor/rules/notes.txt",
    ".claude/skills/fix-issue/scripts/run.py",
    ".claude/output-styles/README.md",
    ".claude/settings.json",                   # path alone: not a prompt
])
def test_medium2_non_prompt_files(rel, tmp_path):
    from vco_lib.prompt_edit_nudge import is_prompt_shaped
    assert is_prompt_shaped(str(tmp_path / rel), str(tmp_path)) is False


_PROMPT_HOOK = {"hooks": {"Stop": [{"hooks": [
    {"type": "prompt", "prompt": "Check the task is complete."}]}]}}
_COMMAND_HOOK = {"hooks": {"Stop": [{"hooks": [
    {"type": "command", "command": "echo done"}]}]}}


@pytest.mark.parametrize("tool_input, expected", [
    ({"content": json.dumps(_PROMPT_HOOK, indent=2)}, True),
    ({"content": json.dumps(_COMMAND_HOOK, indent=2)}, False),
    ({"old_string": "x", "new_string": '{"type": "agent", "prompt": "p"}'},
     True),
    ({"old_string": '"type":"prompt"', "new_string": '"type":"command"'},
     True),
    ({"old_string": '"model": "a"', "new_string": '"model": "b"'}, False),
    ({}, False),
])
def test_medium2_settings_hook_prompts(tool_input, expected, tmp_path):
    from vco_lib.prompt_edit_nudge import prompt_kind
    for name in ("settings.json", "settings.local.json"):
        fp = str(tmp_path / ".claude" / name)
        got = prompt_kind(fp, str(tmp_path), tool_input)
        assert (got == "hook prompt") is expected, (name, tool_input, got)
    # A settings.json outside .claude/ is never a hook config.
    assert prompt_kind(str(tmp_path / "settings.json"), str(tmp_path),
                       tool_input) is None


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "agents" / "agent-author.md").write_text(
        "---\nname: agent-author\n---\n", encoding="utf-8")
    return proj


def _drive(monkeypatch, proj: Path, surface: str, file_path: Path,
           tool_input: "dict | None" = None, legs=("", ""),
           sid: str = "sess-rf") -> str:
    import hook_context_router as router_mod  # pyright: ignore[reportMissingImports]  # on sys.path above

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.delenv("VCO_INJECT_PROFILE", raising=False)
    monkeypatch.setattr(router_mod, "_run_legs", lambda *a, **k: legs)
    tool = "Edit" if surface == "edit" else "Write"
    ti = {"file_path": str(file_path)}
    ti.update(tool_input or ({"old_string": "a", "new_string": "b"}
                             if surface == "edit" else {"content": "x"}))
    payload = {"session_id": sid, "prompt_id": "p1", "tool_name": tool,
               "cwd": str(proj), "tool_input": ti}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    try:
        assert router_mod.main([surface]) == 0
    finally:
        monkeypatch.undo()
    return buf.getvalue()


@pytest.mark.parametrize("rel", ["CLAUDE.md", ".cursor/rules/a.mdc"])
def test_medium2_router_nudges_before_instruction_file_edits(
        tmp_path, monkeypatch, rel):
    proj = _project(tmp_path)
    out = _drive(monkeypatch, proj, "edit", proj / rel)
    assert f"NOTE: prompt-shaped file {rel} | agent=agent-author" in out, out
    assert "instruction file" in out


def test_medium2_router_nudges_on_hook_prompt_settings_edit(tmp_path,
                                                            monkeypatch):
    proj = _project(tmp_path)
    settings = proj / ".claude" / "settings.json"
    out = _drive(monkeypatch, proj, "write", settings,
                 {"content": json.dumps(_PROMPT_HOOK)})
    assert "NOTE: prompt-shaped file .claude/settings.json" in out, out
    assert "hook prompt" in out
    quiet = _drive(monkeypatch, proj, "write", settings,
                   {"content": json.dumps(_COMMAND_HOOK)}, sid="sess-rf2")
    assert "NOTE:" not in quiet, quiet


# ── NIT-6 ────────────────────────────────────────────────────────────────


def _budget_file(proj: Path, sid: str = "sess-rf") -> Path:
    from vco_lib.inject_intent import budget_state_path
    path = budget_state_path(sid, "p1", str(proj))
    assert path, "a trusted session must have a budget file"
    return Path(path)


def test_nit6_note_is_charged_to_the_turn_budget(tmp_path, monkeypatch):
    """RED before the fix: the NOTE block was printed outside the budget —
    nothing was charged for it."""
    proj = _project(tmp_path)
    out = _drive(monkeypatch, proj, "edit", proj / "CLAUDE.md")
    assert "NOTE:" in out
    used = int(_budget_file(proj).read_text(encoding="utf-8").strip())
    assert used >= len(out.strip()), (used, out)


def test_nit6_note_degrades_to_its_header_past_the_budget(tmp_path,
                                                          monkeypatch):
    from vco_lib.inject_intent import PER_TURN_BUDGET_CHARS
    proj = _project(tmp_path)
    bf = _budget_file(proj)
    bf.parent.mkdir(parents=True, exist_ok=True)
    bf.write_text(str(PER_TURN_BUDGET_CHARS), encoding="utf-8")
    out = _drive(monkeypatch, proj, "edit", proj / "CLAUDE.md")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines == ["NOTE: prompt-shaped file CLAUDE.md | agent=agent-author"
                     ], out


def test_nit6_note_counts_against_the_kg_blocks_after_it(tmp_path,
                                                         monkeypatch):
    """The KG block that follows the NOTE in the SAME run sees the NOTE's
    charge: with room for the KG block alone but not for NOTE + KG, the KG
    block degrades to its titles one-liner."""
    from vco_lib.inject_intent import PER_TURN_BUDGET_CHARS
    proj = _project(tmp_path)
    kg = ("KG: Some Node | SUMMARY | src=knowledge/some.md\n"
          + "  " + "body " * 80 + "\n")
    bf = _budget_file(proj)
    bf.parent.mkdir(parents=True, exist_ok=True)
    bf.write_text(str(PER_TURN_BUDGET_CHARS - len(kg) - 20), encoding="utf-8")
    out = _drive(monkeypatch, proj, "edit", proj / "CLAUDE.md",
                 legs=(kg, ""))
    assert "NOTE: prompt-shaped file CLAUDE.md" in out, out
    assert "KG: Some Node | TITLES" in out, out
    assert "body body" not in out, out

