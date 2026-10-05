# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 — the ONE merged async PostToolUse dispatcher.

THE DEFECT (measured, metadata-only, on this maintainer's transcripts):
the settings templates registered EIGHT async PostToolUse hooks across six
scripts. One Bash tool call spawned up to 3 async processes, and every
async run that SPOKE (any stdout/stderr) or DIED (timeout) wrote an
``async_hook_response`` attachment of ~660 B into the session transcript.
One 1.48 GB transcript held 700,330 such records / 462.3 MB — 99.2 % of
them exit-127 relative-path failures of pre-v0.2.97 installs (fixed by the
v0.2.97 anchored commands), the rest driven by (a) an E2BIG class —
``kg-update-nudge.sh`` passed the full hook stdin as ONE env var, and a
payload above MAX_ARG_STRLEN (128 KB) made bash print "Argument list too
long" (271 records, exit 0), and (b) the multiplier — up to 3 records per
tool call because 3 registrations fired.

THE FIX (plan-of-record, PLAN-V0300:1012 "merge async hooks"): ONE async
PostToolUse registration (matcher ``*``, timeout 15) running
``post-tool-use-async.{sh,ps1}``. The dispatcher reads stdin ONCE to a temp
file, routes by ``tool_name`` (plus a ``git-commit-prefix`` gate that
carries the retired ``if: Bash(git commit *)`` registration key) to the
UNCHANGED per-concern scripts, runs the matched children CONCURRENTLY (the
wall-time shape the separate async registrations had), and guarantees
silence: child stdout is discarded, child stderr / non-zero exits condense
to one line per failure in ``<VCO metrics dir>/post-tool-use-async.log``.
The E2BIG class is fixed at the source: ``kg-update-nudge.{sh,ps1}`` now
hand the payload to their embedded Python via a temp file whose PATH rides
the env var, never the payload itself.

WHAT THESE TESTS PIN (each was RED on the pre-fix tree — the red-proof for
the lane; mutation re-proofs are named per test):

1. settings-template invariant — at most ONE ``async: true`` PostToolUse
   registration per OS template, naming ``post-tool-use-async``, matcher
   ``*``, timeout >= 15; the six superseded stems are gone from PostToolUse;
   the SYNC PostToolUse registrations are untouched. (Red before the fix:
   8 async entries per template.) Mutation: re-add one retired async
   registration to either template → red.
2. routing table parity + derived coverage — the ``.sh`` and ``.ps1``
   ROUTE_TABLE declarations are byte-identical, and their stem set equals
   EXACTLY the v0.2.101 PostToolUse retirement rows in
   ``vco_lib/hook_retirements.py`` (derived, not hand-copied: a sub-hook
   retired into the dispatcher without a routing row is red, and a routing
   row without a retirement is red — this is the residual-risk guard the
   lane brief demands against the shared-component-extraction shape).
3. driven routing (bash, and pwsh where available) — with stub sub-hooks
   recording invocation + stdin checksum, each tool name invokes EXACTLY
   the set DERIVED from the routing table (gate applied), every child
   received the exact stdin bytes, and the dispatcher itself is silent
   (empty stdout AND stderr) and exits 0. Mutation: delete a routing branch
   from the dispatcher → the derived expectation goes red.
4. failure silence — a child that writes stderr and exits non-zero still
   leaves the dispatcher silent; the failure condenses to ONE line in the
   metrics-dir log naming the child, its exit code and its flattened
   stderr. (This is the "records only on failure, and only ONE record"
   promise.)
5. E2BIG regression — the REAL ``kg-update-nudge.sh`` with a >128 KB
   payload produces EMPTY stderr, exit 0, and writes its state row (the
   Python body actually ran). Red before the fix: bash's "Argument list
   too long" on stderr AND no state row (the interpreter never started).

Cleanup discipline: the dispatcher's temp stdin file and per-child capture
files are removed after the run (asserted against a pinned TMPDIR).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hook_retirements as hr  # noqa: E402

HOOKS = REPO_ROOT / "templates" / "hooks"
DISPATCHER_SH = HOOKS / "post-tool-use-async.sh"
DISPATCHER_PS1 = HOOKS / "post-tool-use-async.ps1"
NUDGE_SH = HOOKS / "kg-update-nudge.sh"
NUDGE_PS1 = HOOKS / "kg-update-nudge.ps1"

TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}

#: The six scripts whose async PostToolUse REGISTRATIONS the dispatcher
#: supersedes (the scripts themselves still ship — the dispatcher routes to
#: them). Used for the template-absence assertion; the routing-coverage
#: assertion DERIVES its expectation from the retirement table instead.
SUPERSEDED_STEMS = (
    "post-edit-outcome",
    "post-bash-context-record",
    "kg-summary-generator",
    "post-git-commit-kg-sync",
    "post-file-delete",
    "kg-update-nudge",
)

#: PostToolUse registrations that must stay SYNCHRONOUS and registered
#: (they deliver stdout envelopes or gate follow-up behaviour the harness
#: reads in-turn — none of them is part of the async merge).
SYNC_KEPT_STEMS = (
    "post-file-edit",
    "post-bash-file-sync",
    "post-tool-security",
    "py-compile-check",
    "post-mcp-retrieval-record",
)

ROUTE_ROW_RE = re.compile(r"^([A-Za-z0-9_*][A-Za-z0-9_*-]*)\|([a-z0-9][a-z0-9-]*)\|([a-z-]+)$")


def _route_table_sh() -> list[tuple[str, str, str]]:
    """Parse the .sh ROUTE_TABLE declaration into (tool, stem, gate) rows."""
    text = DISPATCHER_SH.read_text(encoding="utf-8")
    m = re.search(r"^ROUTE_TABLE='\n(.*?)^'\n", text, re.S | re.M)
    assert m, f"{DISPATCHER_SH.name}: ROUTE_TABLE declaration not found"
    return _parse_rows(m.group(1))


def _route_table_ps1() -> list[tuple[str, str, str]]:
    """Parse the .ps1 $RouteTable string into (tool, stem, gate) rows.

    The container is a plain multi-line single-quoted string, NOT a
    ``@'...'@`` here-string: the dogfood partition gate
    (tests/test_v0291_dogfood_deferral_selfclear.py) AST-parses every
    single-quoted here-string in a shipped .ps1 as embedded Python and
    reports a parse failure as a hit (loud by design), so a non-Python
    here-string in a hook is forbidden territory.
    """
    text = DISPATCHER_PS1.read_text(encoding="utf-8")
    m = re.search(r"\$RouteTable = '\r?\n(.*?)\r?\n'", text, re.S)
    assert m, f"{DISPATCHER_PS1.name}: $RouteTable declaration not found"
    return _parse_rows(m.group(1))


def _parse_rows(block: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in block.splitlines():
        if not line.strip():
            continue
        m = ROUTE_ROW_RE.match(line.strip())
        assert m, f"routing-table row is not tool|stem|gate shaped: {line!r}"
        assert m.group(3) in ("-", "git-commit-prefix"), (
            f"unknown gate {m.group(3)!r} in row {line!r}"
        )
        rows.append((m.group(1), m.group(2), m.group(3)))
    assert rows, "routing table is empty"
    return rows


def _expected_subhooks(
    rows: list[tuple[str, str, str]],
    tool_name: str,
    command: str | None,
    disabled: "set[str] | None" = None,
) -> set[str]:
    """DERIVE the sub-hook set a payload must invoke from the routing table.

    Never hand-copied: the table is the one declaration, and this function
    is the same match the dispatcher performs (tool equality or ``*``; the
    ``git-commit-prefix`` gate keyed on ``tool_input.command`` being
    ``git commit`` exactly or followed by a space — the word-boundary form
    of the retired ``if: Bash(git commit *)`` key, review N-1; minus any
    stem disabled via ``VCO_ASYNC_DISABLED_HOOKS``, review SF-2).
    """
    c = command.lstrip() if command else ""
    is_commit = c == "git commit" or c.startswith("git commit ")
    out: set[str] = set()
    for tool, stem, gate in rows:
        if tool != "*" and tool != tool_name:
            continue
        if gate == "git-commit-prefix" and not is_commit:
            continue
        if disabled and stem in disabled:
            continue
        out.add(stem)
    return out


def _posttooluse_registrations(template: Path) -> list[dict]:
    doc = json.loads(template.read_text(encoding="utf-8"))
    return [
        {"matcher": group.get("matcher"), **hook}
        for group in doc["hooks"]["PostToolUse"]
        for hook in group.get("hooks", [])
    ]


# ---------------------------------------------------------------------------
# 1. settings-template invariant (RED before the fix: 8 async entries)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_exactly_one_async_posttooluse_registration_names_the_dispatcher(os_name):
    ext = "sh" if os_name == "linux" else "ps1"
    regs = _posttooluse_registrations(TEMPLATES[os_name])
    async_regs = [r for r in regs if r.get("async")]
    assert len(async_regs) == 1, (
        f"{os_name}: PostToolUse must carry exactly ONE async registration "
        f"(the merged dispatcher); found {len(async_regs)}: "
        f"{[r.get('command') for r in async_regs]}"
    )
    only = async_regs[0]
    assert f"post-tool-use-async.{ext}" in only.get("command", ""), only
    assert only.get("matcher") == "*", (
        "the dispatcher must fire on every tool call (kg-update-nudge's "
        f"retired registration needed `*`); matcher is {only.get('matcher')!r}"
    )
    assert int(only.get("timeout", 0)) >= 15, (
        "one registration now bounds the whole concurrent fan-out (the "
        "largest retired per-hook budget was 10s); timeout must be >= 15, "
        f"got {only.get('timeout')!r}"
    )


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_superseded_async_stems_are_gone_from_posttooluse(os_name):
    ext = "sh" if os_name == "linux" else "ps1"
    regs = _posttooluse_registrations(TEMPLATES[os_name])
    for stem in SUPERSEDED_STEMS:
        offenders = [
            r.get("command") for r in regs
            if f"{stem}.{ext}" in r.get("command", "")
        ]
        assert not offenders, (
            f"{os_name}: {stem} is still REGISTERED under PostToolUse "
            f"({offenders!r}); since v0.2.101 the dispatcher routes to it — "
            "a direct registration re-adds the per-tool-call async record"
        )


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_sync_posttooluse_registrations_are_untouched(os_name):
    ext = "sh" if os_name == "linux" else "ps1"
    regs = _posttooluse_registrations(TEMPLATES[os_name])
    for stem in SYNC_KEPT_STEMS:
        mine = [r for r in regs if f"{stem}.{ext}" in r.get("command", "")]
        assert len(mine) == 1, (
            f"{os_name}: sync registration {stem} must survive the merge "
            f"exactly once; found {len(mine)}"
        )
        assert not mine[0].get("async"), (
            f"{os_name}: {stem} must stay synchronous (its stdout envelope "
            "is read in-turn; an async hook's stdout cannot deliver it)"
        )


def test_kg_update_nudge_keeps_its_non_posttooluse_registrations():
    """Event-scoping proof on the template side: the nudge's SYNC
    registrations (UserPromptSubmit + SessionStart compact) are not part of
    the async merge and must still be there."""
    doc = json.loads(TEMPLATES["linux"].read_text(encoding="utf-8"))
    events_with_nudge = [
        event
        for event, groups in doc["hooks"].items()
        for g in groups
        for h in g.get("hooks", [])
        if "kg-update-nudge.sh" in h.get("command", "")
    ]
    assert "UserPromptSubmit" in events_with_nudge, events_with_nudge
    assert "SessionStart" in events_with_nudge, events_with_nudge


# ---------------------------------------------------------------------------
# 2. dispatcher pair: existence, syntax, table parity, derived coverage
# ---------------------------------------------------------------------------


def test_dispatcher_siblings_exist_and_pass_syntax():
    assert DISPATCHER_SH.is_file(), f"missing {DISPATCHER_SH}"
    assert DISPATCHER_PS1.is_file(), f"missing {DISPATCHER_PS1}"
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run(
            [bash, "-n", str(DISPATCHER_SH)], capture_output=True, text=True
        )
        assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_dispatcher_ps1_is_ascii_only():
    """The .ps1 must stay ASCII (or carry a UTF-8 BOM per
    tests/test_ps1_utf8_bom.py); ASCII-only is the simpler contract."""
    raw = DISPATCHER_PS1.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        pytest.fail(
            "post-tool-use-async.ps1 contains non-ASCII bytes without a BOM "
            f"plan: {exc}. Keep it ASCII-only (Windows PowerShell 5.1)."
        )


def test_route_tables_are_identical_across_siblings():
    """One routing declaration, mirrored byte-for-byte (the A>B>C 'shared
    config' tier: both dispatchers PARSE the same committed table)."""
    assert _route_table_sh() == _route_table_ps1(), (
        "the .sh and .ps1 routing tables diverged — a sub-hook would fire "
        "on one OS only"
    )


def test_route_table_stems_equal_the_v02101_retirement_rows():
    """DERIVED coverage (the lane brief's residual-risk guard).

    The set of stems the dispatcher routes must equal EXACTLY the set of
    PostToolUse hook-script retirements declared for v0.2.101 — so adding a
    sub-hook to the retirement table without a routing row (a silently
    dropped capability, the shape a green suite cannot otherwise see) OR a
    routing row without a retirement (a stale registration surviving on
    existing installs) both go red here.
    """
    retired = {
        entry.target.rsplit(".", 1)[0]
        for entry in hr.RETIRED_REGISTRATIONS
        if entry.event == "PostToolUse"
        and entry.kind == hr.KIND_HOOK_SCRIPT
        and entry.retired_in == "v0.2.101"
    }
    routed = {stem for _tool, stem, _gate in _route_table_sh()}
    assert routed == retired, (
        "dispatcher routing table and the v0.2.101 PostToolUse retirement "
        f"rows disagree:\n  routed but not retired: {sorted(routed - retired)}\n"
        f"  retired but not routed: {sorted(retired - routed)}"
    )
    assert retired == set(SUPERSEDED_STEMS), (
        "the retirement table's v0.2.101 PostToolUse rows must cover exactly "
        f"the six merged scripts; got {sorted(retired)}"
    )


def test_route_table_is_the_pre_v02101_registration_shape():
    """GOLDEN pin beside the derived coverage test.

    The derived test (below) ties the table to the retirement rows, so a
    sub-hook can never be dropped — but it cannot see BOTH siblings being
    consistently re-routed to the WRONG tool. This golden map is the
    registration shape the v0.2.100 templates shipped, transcribed once
    from the lane brief: post-edit-outcome on Edit|Write,
    post-bash-context-record + post-file-delete on Bash,
    post-git-commit-kg-sync on Bash(git commit *) only, kg-summary-
    generator on Edit/Write/store_knowledge_node, kg-update-nudge on
    everything. A routing edit that moves a sub-hook to another tool must
    update BOTH this map and the retirement table — two declarations
    instead of one silent drift.
    """
    no_commit = {
        "post-bash-context-record", "post-file-delete", "kg-update-nudge",
    }
    with_commit = no_commit | {"post-git-commit-kg-sync"}
    golden = {
        ("Bash", "ls -la"): no_commit,
        ("Bash", "git commit -m x"): with_commit,
        # N-1 word boundary: bare `git commit` fires the retired
        # `if: Bash(git commit *)` gate; `git commit-tree` (different
        # porcelain command) and a non-prefix compound do NOT.
        ("Bash", "git commit"): with_commit,
        ("Bash", "git commit-tree abc123"): no_commit,
        ("Bash", "cd sub && git commit -m x"): no_commit,
        ("Edit", None): {
            "post-edit-outcome", "kg-summary-generator", "kg-update-nudge",
        },
        ("Write", None): {
            "post-edit-outcome", "kg-summary-generator", "kg-update-nudge",
        },
        ("mcp__weaviate-kg__store_knowledge_node", None): {
            "kg-summary-generator", "kg-update-nudge",
        },
        ("Read", None): {"kg-update-nudge"},
        ("", None): {"kg-update-nudge"},
    }
    rows = _route_table_sh()
    for (tool, command), expected in golden.items():
        assert _expected_subhooks(rows, tool, command) == expected, (
            f"routing for tool={tool!r} command={command!r} drifted from the "
            "pre-v0.2.101 registration shape"
        )
    assert _route_table_ps1() == rows


def test_every_v02101_retirement_row_names_the_dispatcher_as_replacement():
    rows = [
        entry for entry in hr.RETIRED_REGISTRATIONS
        if entry.event == "PostToolUse"
        and entry.kind == hr.KIND_HOOK_SCRIPT
        and entry.retired_in == "v0.2.101"
    ]
    assert rows, "no v0.2.101 PostToolUse retirement rows declared"
    for entry in rows:
        assert "post-tool-use-async" in entry.replacement, entry
        assert "dispatcher" in entry.reason, entry


def test_retirement_rows_cover_both_os_extensions():
    targets = {
        entry.target for entry in hr.RETIRED_REGISTRATIONS
        if entry.event == "PostToolUse"
        and entry.kind == hr.KIND_HOOK_SCRIPT
        and entry.retired_in == "v0.2.101"
    }
    for stem in SUPERSEDED_STEMS:
        assert f"{stem}.sh" in targets, targets
        assert f"{stem}.ps1" in targets, targets


# ---------------------------------------------------------------------------
# 3+4. driven routing / silence / stdin preservation / failure logging
# ---------------------------------------------------------------------------

STUB_SH = """#!/usr/bin/env bash
# test stub for {stem} — records invocation + stdin checksum
md5=$(md5sum | cut -d' ' -f1)
printf '%s %s\\n' "{stem}" "$md5" >> "$VCO_TEST_MARKER"
if [ "{stem}" = "${{VCO_TEST_FAIL_STEM:-}}" ]; then
    printf 'boom from {stem}\\n' >&2
    exit 3
fi
exit 0
"""

# The .ps1 stub normalizes EXACTLY ONE platform artifact and nothing else:
# PowerShell's Start-Process -RedirectStandardInput hands the child the
# file's bytes plus a trailing newline (observed on pwsh/Linux; the same
# Start-Process code path on Windows PS 5.1). Every real sub-hook parses
# stdin as JSON, where trailing whitespace is inert — so the dispatcher's
# .ps1 leg keeps the ONE spawn home (_lib/resolve-powershell.ps1) instead
# of re-implementing byte-faithful stdin plumbing. The stub TrimEnd's the
# artifact (payloads here never end in a newline, so the checksum stays
# exact) and writes a PER-STEM marker file (concurrent Add-Content to one
# file interleaves — a race this test must not flake on).
STUB_PS1 = """$in = [Console]::In.ReadToEnd()
$in = $in.TrimEnd("`r", "`n")
$md5 = [System.BitConverter]::ToString(
    [System.Security.Cryptography.MD5]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($in))
).Replace("-", "").ToLowerInvariant()
[System.IO.File]::WriteAllText(
    ($env:VCO_TEST_MARKER + ".{stem}"), ("{stem} " + $md5 + "`n"))
if ($env:VCO_TEST_FAIL_STEM -eq "{stem}") {{
    [Console]::Error.WriteLine("boom from {stem}")
    exit 3
}}
exit 0
"""

PARSE_SRC_RE = re.compile(r"import json, sys", re.S)


def _needs_bash() -> bool:
    return shutil.which("bash") is not None and shutil.which("md5sum") is not None


@pytest.fixture()
def sh_rig(tmp_path):
    """A fixture hooks-dir holding the REAL dispatcher + _lib and stub
    sub-hooks for every stem the routing table names (derived, so a table
    row without a stub would KeyError the run instead of silently passing).
    """
    if not _needs_bash():
        pytest.skip("bash/md5sum unavailable")
    if not DISPATCHER_SH.is_file():
        pytest.fail(f"dispatcher missing: {DISPATCHER_SH}")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(DISPATCHER_SH, hooks / DISPATCHER_SH.name)
    shutil.copytree(HOOKS / "_lib", hooks / "_lib")
    stems = {stem for _t, stem, _g in _route_table_sh()}
    for stem in stems:
        stub = hooks / f"{stem}.sh"
        stub.write_text(STUB_SH.format(stem=stem), encoding="utf-8")
        stub.chmod(0o755)
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    tmpdir = tmp_path / "tmpdir"
    for d in (home, proj, tmpdir):
        d.mkdir()
    marker = tmp_path / "marker.txt"
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "VCT_STATE_DIR": str(home / ".vct"),
        "VCT_CLAUDE_DIR": str(home / ".claude"),
        "TMPDIR": str(tmpdir),
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCO_TEST_MARKER": str(marker),
    })
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("KG_NUDGE_OFF", None)
    env.pop("VCO_TEST_FAIL_STEM", None)

    class Rig:
        pass

    rig = Rig()
    rig.hooks = hooks
    rig.home = home
    rig.tmpdir = tmpdir
    rig.marker = marker
    rig.env = env
    rig.rows = _route_table_sh()

    def run(payload: dict, fail_stem: str | None = None, raw_input: str | None = None):
        env = dict(rig.env)
        if fail_stem:
            env["VCO_TEST_FAIL_STEM"] = fail_stem
        body = raw_input if raw_input is not None else json.dumps(payload)
        if rig.marker.exists():
            rig.marker.unlink()
        proc = subprocess.run(
            ["bash", str(hooks / DISPATCHER_SH.name)],
            input=body, capture_output=True, text=True, env=env,
            timeout=60, cwd=str(proj),
        )
        invoked: dict[str, str] = {}
        if rig.marker.exists():
            for line in rig.marker.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    stem, md5 = line.split()
                    invoked[stem] = md5
        return proc, invoked, body

    rig.run = run
    return rig


def _payload(tool_name: str, command: str | None = None, **tool_input) -> dict:
    ti = dict(tool_input)
    if command is not None:
        ti["command"] = command
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "v02101-dispatcher-test",
        "tool_name": tool_name,
        "tool_input": ti,
        "tool_response": {},
    }


#: (case id, payload). The EXPECTED set is derived per case from the table.
#: The two git-boundary cases pin review N-1: bare `git commit` fires the
#: gate, `git commit-tree` (a different porcelain command) must NOT.
ROUTING_CASES = [
    ("bash-plain", _payload("Bash", command="ls -la")),
    ("bash-git-commit", _payload("Bash", command="git commit -m 'x'")),
    ("bash-git-commit-bare", _payload("Bash", command="git commit")),
    ("bash-git-commit-tree", _payload("Bash", command="git commit-tree abc123")),
    ("bash-git-commit-with-cd", _payload("Bash", command="cd sub && git commit -m x")),
    ("edit", _payload("Edit", file_path="/p/src/a.py", old_string="a", new_string="b")),
    ("write", _payload("Write", file_path="/p/knowledge/n.md", content="# n")),
    ("mcp-store", _payload("mcp__weaviate-kg__store_knowledge_node",
                           file_path="knowledge/n.md")),
    ("unknown-tool", _payload("Read", file_path="/p/src/a.py")),
]


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
@pytest.mark.parametrize("case_id,payload", ROUTING_CASES, ids=[c[0] for c in ROUTING_CASES])
def test_sh_dispatcher_routes_exactly_the_derived_set(sh_rig, case_id, payload):
    proc, invoked, body = sh_rig.run(payload)
    expected = _expected_subhooks(
        sh_rig.rows, payload.get("tool_name", ""),
        (payload.get("tool_input") or {}).get("command"),
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert set(invoked) == expected, (
        f"{case_id}: dispatcher invoked {sorted(invoked)} but the routing "
        f"table derives {sorted(expected)}"
    )
    # stdin preservation: every child received the EXACT payload bytes.
    want_md5 = hashlib.md5(body.encode("utf-8")).hexdigest()
    for stem, got in invoked.items():
        assert got == want_md5, f"{case_id}: {stem} received corrupted stdin"
    # silence: the transcript-record driver is any stdout/stderr at all.
    assert proc.stdout == "", f"{case_id}: dispatcher wrote stdout: {proc.stdout!r}"
    assert proc.stderr == "", f"{case_id}: dispatcher wrote stderr: {proc.stderr!r}"
    # temp-file hygiene: nothing left behind in the pinned TMPDIR.
    leftovers = list(sh_rig.tmpdir.iterdir())
    assert leftovers == [], f"{case_id}: temp files leaked: {leftovers}"


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_failure_is_silent_and_logged_once(sh_rig):
    """A child that speaks and dies must NOT reach the harness: one line in
    the metrics-dir log instead (the 'records only on failure' promise)."""
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = sh_rig.run(payload, fail_stem="post-file-delete")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == "" and proc.stderr == "", (
        f"a failing child leaked to the harness: {proc.stdout!r} {proc.stderr!r}"
    )
    assert "post-file-delete" in invoked, "the failing child must still have run"
    logs = list(sh_rig.home.rglob("post-tool-use-async.log"))
    assert logs, "no failure log was written under the metrics home"
    content = logs[0].read_text(encoding="utf-8")
    assert "post-file-delete" in content, content
    assert "exit=3" in content, content
    assert "boom from post-file-delete" in content.replace("\n", " ") or (
        "boom from post-file-delete" in content
    ), content


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_empty_stdin_is_a_silent_noop(sh_rig):
    proc, invoked, _body = sh_rig.run({}, raw_input="")
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""
    assert invoked == {}


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_malformed_json_only_runs_the_wildcard_row(sh_rig):
    """Unparseable stdin must not crash the dispatcher nor invent a tool:
    only the `*` route fires (kg-update-nudge soft-fails on it internally),
    exactly like the retired `*` registration did."""
    proc, invoked, _body = sh_rig.run({}, raw_input="{not json")
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""
    expected = _expected_subhooks(sh_rig.rows, "", None)
    assert set(invoked) == expected, (invoked, expected)


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_skips_stems_disabled_via_the_env_file(sh_rig):
    """SF-2 driven leg: VCO_ASYNC_DISABLED_HOOKS in <project>/.claude/env
    (the lean-ctx knob channel; the launcher writes it with the existing
    set_claude_env_value command) makes the dispatcher skip those stems —
    the per-registration toggle the eight retired async entries had
    survives the merge."""
    env_dir = Path(sh_rig.env["CLAUDE_PROJECT_DIR"]) / ".claude"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env").write_text(
        "# user knobs\nVCO_ASYNC_DISABLED_HOOKS=post-file-delete,kg-update-nudge\n",
        encoding="utf-8",
    )
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = sh_rig.run(payload)
    expected = _expected_subhooks(
        sh_rig.rows, "Bash", "ls -la",
        disabled={"post-file-delete", "kg-update-nudge"},
    )
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""
    assert set(invoked) == expected
    assert "post-file-delete" not in invoked
    assert "kg-update-nudge" not in invoked


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_skips_stems_disabled_via_process_env(sh_rig):
    """The key also works as a plain environment variable (shell export or
    settings.json env channel), with surrounding spaces and unknown stems
    tolerated; an exact-stem match only — `post-file` must not disable
    `post-file-delete`."""
    sh_rig.env["VCO_ASYNC_DISABLED_HOOKS"] = " post-file , no-such-hook ,post-bash-context-record"
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = sh_rig.run(payload)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert "post-bash-context-record" not in invoked
    assert "post-file-delete" in invoked, "substring match would over-disable"
    assert "kg-update-nudge" in invoked


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_payload_survives_a_stdin_eating_env_file(sh_rig):
    """N-A: `.claude/env` is user-editable and the dispatcher SOURCES it. A
    stdin-consuming line in it (`read …`) used to eat the hook payload
    before `cat > "$TMP_INPUT"` — routing then silently degraded to the `*`
    row. The source now runs with `</dev/null`: a hostile or hand-edited
    env file can affect the knob and nothing else."""
    env_dir = Path(sh_rig.env["CLAUDE_PROJECT_DIR"]) / ".claude"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env").write_text(
        "read -r _pta_eaten || true\n"
        "VCO_ASYNC_DISABLED_HOOKS=post-file-delete\n",
        encoding="utf-8",
    )
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = sh_rig.run(payload)
    expected = _expected_subhooks(
        sh_rig.rows, "Bash", "ls -la", disabled={"post-file-delete"}
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert set(invoked) == expected, (
        "the payload must survive the env-file source (full routed set "
        f"minus the disabled stem); got {sorted(invoked)}"
    )


@pytest.mark.skipif(not _needs_bash(), reason="bash/md5sum unavailable")
def test_sh_dispatcher_reads_a_quoted_env_value(sh_rig):
    """N-B (sh leg): a hand-edited quoted value behaves like the bare one —
    `source` strips the quotes; the .ps1 sibling must accept the same
    spellings (pinned on its own leg below)."""
    env_dir = Path(sh_rig.env["CLAUDE_PROJECT_DIR"]) / ".claude"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env").write_text(
        'VCO_ASYNC_DISABLED_HOOKS="post-file-delete,kg-update-nudge"\n',
        encoding="utf-8",
    )
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = sh_rig.run(payload)
    expected = _expected_subhooks(
        sh_rig.rows, "Bash", "ls -la",
        disabled={"post-file-delete", "kg-update-nudge"},
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert set(invoked) == expected


# ── the same driven proof for the .ps1 sibling, where pwsh exists ──────────

def _have_pwsh() -> bool:
    return shutil.which("pwsh") is not None


@pytest.fixture()
def ps_rig(tmp_path):
    if not _have_pwsh():
        pytest.skip("pwsh unavailable")
    if not DISPATCHER_PS1.is_file():
        pytest.fail(f"dispatcher missing: {DISPATCHER_PS1}")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(DISPATCHER_PS1, hooks / DISPATCHER_PS1.name)
    shutil.copytree(HOOKS / "_lib", hooks / "_lib")
    stems = {stem for _t, stem, _g in _route_table_ps1()}
    for stem in stems:
        (hooks / f"{stem}.ps1").write_text(
            STUB_PS1.format(stem=stem), encoding="ascii"
        )
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    tmpdir = tmp_path / "tmpdir"
    for d in (home, proj, tmpdir):
        d.mkdir()
    marker = tmp_path / "marker.txt"
    marker.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "VCT_STATE_DIR": str(home / ".vct"),
        "VCT_CLAUDE_DIR": str(home / ".claude"),
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCO_TEST_MARKER": str(marker),
    })
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("KG_NUDGE_OFF", None)
    env.pop("VCO_TEST_FAIL_STEM", None)

    class Rig:
        pass

    rig = Rig()
    rig.hooks = hooks
    rig.home = home
    rig.marker = marker
    rig.env = env
    rig.rows = _route_table_ps1()

    def run(payload: dict, fail_stem: str | None = None):
        env = dict(rig.env)
        if fail_stem:
            env["VCO_TEST_FAIL_STEM"] = fail_stem
        # ASCII-only payloads: the .ps1 stdin roundtrip goes through
        # [Console]::In (InputEncoding) — keep the checksum comparison exact.
        body = json.dumps(payload, ensure_ascii=True)
        for old in tmp_path.glob("marker.txt*"):
            old.unlink()
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(hooks / DISPATCHER_PS1.name)],
            input=body, capture_output=True, text=True, env=env,
            timeout=120, cwd=str(proj),
        )
        invoked: dict[str, str] = {}
        for mark in sorted(tmp_path.glob("marker.txt.*")):
            raw = mark.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) != 2:
                    raise AssertionError(
                        f"malformed marker line {line!r}; raw marker: {raw!r}; "
                        f"dispatcher stdout={proc.stdout!r} "
                        f"stderr={proc.stderr!r}"
                    )
                invoked[parts[0]] = parts[1]
        return proc, invoked, body

    rig.run = run
    return rig


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
@pytest.mark.parametrize(
    "case_id,payload",
    [c for c in ROUTING_CASES if c[0] != "bash-git-commit-with-cd"],
    ids=[c[0] for c in ROUTING_CASES if c[0] != "bash-git-commit-with-cd"],
)
def test_ps1_dispatcher_routes_exactly_the_derived_set(ps_rig, case_id, payload):
    proc, invoked, body = ps_rig.run(payload)
    expected = _expected_subhooks(
        ps_rig.rows, payload.get("tool_name", ""),
        (payload.get("tool_input") or {}).get("command"),
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert set(invoked) == expected, (
        f"{case_id}: .ps1 dispatcher invoked {sorted(invoked)} but the "
        f"routing table derives {sorted(expected)}"
    )
    want_md5 = hashlib.md5(body.encode("utf-8")).hexdigest()
    for stem, got in invoked.items():
        assert got == want_md5, f"{case_id}: {stem} received corrupted stdin (.ps1)"
    assert proc.stdout.strip() == "", f"{case_id}: .ps1 wrote stdout: {proc.stdout!r}"
    assert proc.stderr.strip() == "", f"{case_id}: .ps1 wrote stderr: {proc.stderr!r}"


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
def test_ps1_dispatcher_failure_is_silent_and_logged_once(ps_rig):
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = ps_rig.run(payload, fail_stem="post-file-delete")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "" and proc.stderr.strip() == "", (
        f"a failing child leaked to the harness (.ps1): "
        f"{proc.stdout!r} {proc.stderr!r}"
    )
    assert "post-file-delete" in invoked
    logs = list(ps_rig.home.rglob("post-tool-use-async.log"))
    assert logs, "no failure log was written under the metrics home (.ps1)"
    content = logs[0].read_text(encoding="utf-8")
    assert "post-file-delete" in content, content
    assert "exit=3" in content, content


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
def test_ps1_dispatcher_skips_stems_disabled_via_the_env_file(ps_rig):
    """SF-2 driven leg on the Windows sibling — the .claude/env scan idiom
    lean-ctx-rewrite.ps1 uses (targeted regex, last match wins), written
    here with CRLF line endings to pin the Windows file shape."""
    env_dir = Path(ps_rig.env["CLAUDE_PROJECT_DIR"]) / ".claude"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env").write_bytes(
        b"# user knobs\r\nVCO_ASYNC_DISABLED_HOOKS=post-file-delete\r\n"
    )
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = ps_rig.run(payload)
    expected = _expected_subhooks(
        ps_rig.rows, "Bash", "ls -la", disabled={"post-file-delete"}
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "" and proc.stderr.strip() == ""
    assert set(invoked) == expected
    assert "post-file-delete" not in invoked


@pytest.mark.skipif(not _have_pwsh(), reason="pwsh unavailable")
@pytest.mark.parametrize("quoted", ['"post-file-delete"', "'post-file-delete'"])
def test_ps1_dispatcher_reads_a_quoted_env_value(ps_rig, quoted):
    """N-B (ps1 leg): the regex capture must strip ONE surrounding quote
    pair, so a hand-edited `VCO_ASYNC_DISABLED_HOOKS="a,b"` disables the
    same stems on Windows as the .sh `source` does — before the fix the
    quoted spelling was silently inert here (red-proof: the stems ran)."""
    env_dir = Path(ps_rig.env["CLAUDE_PROJECT_DIR"]) / ".claude"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env").write_text(
        f"VCO_ASYNC_DISABLED_HOOKS={quoted}\n", encoding="ascii"
    )
    payload = _payload("Bash", command="ls -la")
    proc, invoked, _body = ps_rig.run(payload)
    expected = _expected_subhooks(
        ps_rig.rows, "Bash", "ls -la", disabled={"post-file-delete"}
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "" and proc.stderr.strip() == ""
    assert set(invoked) == expected
    assert "post-file-delete" not in invoked


# ---------------------------------------------------------------------------
# SF-1 / N-3 / SF-2 static pins (the Windows-visible classes the driven
# Linux legs cannot see)
# ---------------------------------------------------------------------------


def test_ps1_log_lines_go_through_the_one_redirected_writer():
    """SF-1 structural leg.

    The driven leg (test_ps1_dispatcher_failure_is_silent_and_logged_once)
    proves the log line lands — but .NET does NOT enforce FileShare on
    Unix, so the Windows-only defect class (a SECOND append-open of the log
    colliding with the StreamWriter the silence redirect already holds,
    every structured line silently swallowed by a bare catch) is invisible
    to any test that can run here. This pin is the other leg: exactly ONE
    write path, through the redirected Console.Error. Mutation red-proof:
    restore `[System.IO.File]::AppendAllText` inside
    Write-VcoAsyncLogLine → this test goes red.
    """
    text = DISPATCHER_PS1.read_text(encoding="utf-8")
    # Code lines only — the header's DO-NOT warning legitimately NAMES the
    # forbidden call (same comment-skip the spawn-guard scan uses; a pin a
    # comment can trip would train the next editor to delete the warning).
    offenders = [
        f"{n}: {ln.strip()}"
        for n, ln in enumerate(text.splitlines(), 1)
        if "AppendAllText" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        "post-tool-use-async.ps1 re-grew a second log writer: on Windows "
        "the FileStream held by the Console-stderr redirect uses "
        "FileShare.Read, and a separate append-open throws IOException — "
        "silently dropping every failure line (review SF-1). Offenders: "
        f"{offenders}"
    )
    m = re.search(
        r"function Write-VcoAsyncLogLine \{(.*?)\n\}", text, re.S
    )
    assert m, "Write-VcoAsyncLogLine not found"
    body = m.group(1)
    assert "[Console]::Error.WriteLine($Line)" in body, (
        "log lines must go through the ONE redirected Console.Error writer"
    )
    assert "VcoAsyncLogReady" in body, (
        "the writer must be gated on the redirect having succeeded — "
        "otherwise a failed SetError sends log lines to the harness"
    )


def test_sh_traps_termination_signals_for_temp_cleanup():
    """N-3: the harness timeout's signal path must still clean the payload
    temp file (PostToolUse stdin can carry file contents). SIGKILL remains
    untrappable — documented in the hook header, not solvable in-process."""
    text = DISPATCHER_SH.read_text(encoding="utf-8")
    assert "trap 'exit 0' TERM INT HUP" in text
    assert "SIGKILL" in text, "the untrappable-kill limit must stay documented"


def test_both_siblings_read_the_same_disable_key():
    """SF-2 parity: one knob name, both OSes (the values semantics are
    pinned by the driven legs above)."""
    for p in (DISPATCHER_SH, DISPATCHER_PS1):
        assert "VCO_ASYNC_DISABLED_HOOKS" in p.read_text(
            encoding="utf-8", errors="replace"
        ), f"{p.name} lost the per-sub-hook disable key"


# ---------------------------------------------------------------------------
# 5. E2BIG regression on the REAL kg-update-nudge.sh (RED before the fix)
# ---------------------------------------------------------------------------


def _nudge_env(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["VCT_STATE_DIR"] = str(home / ".vct")
    env["VCT_CLAUDE_DIR"] = str(home / ".claude")
    env.pop("VCT_VENV", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("KG_NUDGE_OFF", None)
    return env


@pytest.mark.skipif(not _needs_bash(), reason="bash unavailable")
def test_kg_update_nudge_survives_a_payload_above_max_arg_strlen(tmp_path):
    """200 KB payload (> Linux MAX_ARG_STRLEN = 128 KB) through the REAL
    hook: empty stderr, exit 0, and the state row PROVES the embedded Python
    ran. Before the fix bash printed `<hook>: line N: <python>: Argument
    list too long` (env-var passing at kg-update-nudge.sh:112) and the
    interpreter never started — both halves are asserted so the test cannot
    pass vacuously. Mutation red-proof: restore `KG_NUDGE_INPUT="$INPUT"`
    env passing → stderr non-empty AND no state row."""
    home = tmp_path
    (home / "knowledge").mkdir()
    kg_file = home / "knowledge" / "node.md"
    kg_file.write_text("# node\n", encoding="utf-8")
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "e2big-regression",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(kg_file)},
        # the bloat driver: a huge tool_response riding the hook stdin
        "tool_response": {"output": "x" * 200_000},
    }
    result = subprocess.run(
        ["bash", str(NUDGE_SH)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=_nudge_env(home),
        timeout=60, cwd=str(home),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "", (
        "kg-update-nudge.sh spoke on stderr for a large payload (the E2BIG "
        f"transcript-record class): {result.stderr.strip()[:300]!r}"
    )
    state = home / ".vct" / "metrics" / "kg_update_tokens.jsonl"
    assert state.is_file(), "the embedded Python never ran (no state row)"
    sessions = [
        json.loads(ln).get("session_id")
        for ln in state.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert "e2big-regression" in sessions, sessions


def test_nudge_siblings_pass_the_payload_by_file_not_env():
    """Static pin of the fix SHAPE on both siblings (the .ps1 has the same
    class of ceiling: a Windows env var caps at 32,767 chars)."""
    sh_text = NUDGE_SH.read_text(encoding="utf-8")
    ps_text = NUDGE_PS1.read_text(encoding="utf-8", errors="replace")
    assert 'KG_NUDGE_INPUT="$INPUT"' not in sh_text, (
        "kg-update-nudge.sh still passes the payload AS an env var (E2BIG)"
    )
    assert "KG_NUDGE_INPUT_FILE" in sh_text, (
        "kg-update-nudge.sh must hand Python the payload's temp-file PATH"
    )
    assert "$env:_KG_NUDGE_INPUT " not in ps_text and (
        "$env:_KG_NUDGE_INPUT       =" not in ps_text
    ), "kg-update-nudge.ps1 still passes the payload AS an env var"
    assert "_KG_NUDGE_INPUT_FILE" in ps_text, (
        "kg-update-nudge.ps1 must hand Python the payload's temp-file PATH"
    )
