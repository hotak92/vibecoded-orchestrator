# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-ALERTS2 (owner ruling 3) — the shared worktree gate tells the
model when the hub is down.

The gate (``_lib/worktree-gate.{sh,ps1}``, FIX-A') keeps leaning toward
indexing when ``resolve-project`` answers exit 1 (hub unreachable). The two
hooks that use it now also say so, ONCE per session per agent key, naming
``vct-hub --start-if-not-running``:

* ``stop-codegraph-drain.{sh,ps1}`` (async Stop) — an ``additionalContext``
  envelope (Claude Code hands an async hook's context over on the next turn);
* ``code-graph-incremental.{sh,ps1}`` (a CLI-arg script, no hook event) —
  stderr, keyed by the UTC hour since it has no session.

Act: hub down → one notice. Leave-alone: hub up (registered) → none; hub says
"not registered" (the skip) → none; second time in the same session → none.
A different session (or a subagent key) is told again.

Real git repo + linked worktree, the real hooks, a stub resolver standing in
for the hub's answer (exit 0 / 1 / 2), and a stub analyzer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
FLAVOURS = ["sh", "ps1"]
HINT = "vct-hub --start-if-not-running"


def _need(flavour: str) -> None:
    if not shutil.which("git"):
        pytest.skip("git required")
    if flavour == "sh" and not shutil.which("bash"):
        pytest.skip("bash required")
    if flavour == "ps1" and not shutil.which("pwsh"):
        pytest.skip("pwsh required")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _layout(tmp_path: Path, hub_rc: int) -> tuple[Path, Path, Path]:
    """(main repo, linked worktree, edited file in the worktree). The main
    repo carries both resolver stubs; ``resolve-project`` exits ``hub_rc``."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "seed.py").write_text("x = 0\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "seed")
    scripts = main / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    sh = scripts / "vct_project_config.sh"
    sh.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [ "$1" = "resolve-project" ]; then exit {hub_rc}; fi
        exit 4
    """))
    sh.chmod(0o755)
    (scripts / "vct_project_config.ps1").write_text(
        f"if ($args.Count -ge 1 -and $args[0] -eq '-ResolveProject') {{ exit {hub_rc} }}\nexit 4\n")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "lane")
    (wt / ".claude" / "state").mkdir(parents=True)
    edited = wt / "edited.py"
    edited.write_text("y = 1\n")
    return main, wt, edited


def _analyzer_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "analyzer_stub.py"
    stub.write_text("import sys\n")
    return stub


def _env(tmp_path: Path, project: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCT_HUB_TOKEN", "VCT_HUB_PORT")}
    env.update({
        "CLAUDE_PROJECT_DIR": str(project),
        "VCT_ANALYZER_SCRIPT": str(_analyzer_stub(tmp_path)),
        "VCT_PYTHON": sys.executable,
        "VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS": "0",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    })
    return env


def _argv(name: str, flavour: str, *args: str) -> list:
    script = HOOKS / f"{name}.{flavour}"
    if flavour == "sh":
        return ["bash", str(script), *args]
    return ["pwsh", *PWSH_BASE_FLAGS, "-File", str(script), *args]


def _drain(tmp_path: Path, wt: Path, edited: Path, flavour: str, session: str,
           agent_id: str = "") -> subprocess.CompletedProcess:
    (wt / ".claude" / "state" / f"codegraph_drain_{session}.txt").write_text(f"{edited}\n")
    payload = {"hook_event_name": "Stop", "session_id": session}
    if agent_id:
        payload["agent_id"] = agent_id
    return subprocess.run(_argv("stop-codegraph-drain", flavour), input=json.dumps(payload),
                          capture_output=True, text=True, env=_env(tmp_path, wt),
                          cwd=str(wt), timeout=120)


def _envelopes(stdout: str) -> list:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def _sentinels(wt: Path) -> list:
    return sorted(p.name for p in (wt / ".claude" / "state").glob("hub_unreachable_notice_*"))


# ── the gate itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("hub_rc,decision,flag", [
    (1, "INDEX", True),    # hub unreachable: index on doubt, flag it
    (0, "INDEX", False),   # registered
    (2, "SKIP", False),    # definitively unregistered
])
def test_gate_decision_and_flag(tmp_path: Path, flavour: str, hub_rc: int, decision: str,
                                flag: bool) -> None:
    """FIX-A' unchanged (exit 1 still INDEXES); only exit 1 raises the flag."""
    _need(flavour)
    main, wt, edited = _layout(tmp_path, hub_rc)
    if flavour == "sh":
        script = (f'. "{HOOKS}/_lib/worktree-gate.sh"\n'
                  f'if _worktree_gate_should_skip "{edited}" "{wt}" "{main}"; then echo SKIP; '
                  f'else echo INDEX; fi\necho "FLAG=${{VCO_WORKTREE_GATE_HUB_UNREACHABLE}}"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    else:
        script = (f". '{HOOKS}/_lib/worktree-gate.ps1'\n"
                  f"if (Test-EphemeralWorktreeEdit -EditedFile '{edited}' -RepoPath '{wt}' "
                  f"-CanonRoot '{main}') {{ 'SKIP' }} else {{ 'INDEX' }}\n"
                  "if ($script:WorktreeGateHubUnreachable) { 'FLAG=1' } else { 'FLAG=' }\n")
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", script],
                           capture_output=True, text=True, timeout=60)
    lines = r.stdout.split()
    assert decision in lines, r.stdout + r.stderr
    assert ("FLAG=1" in lines) is flag, r.stdout + r.stderr


def test_gate_flag_is_reset_when_the_lib_is_sourced(tmp_path: Path) -> None:
    """An exported VCO_WORKTREE_GATE_HUB_UNREACHABLE=1 never leaks into a run."""
    _need("sh")
    script = f'. "{HOOKS}/_lib/worktree-gate.sh"\necho "FLAG=${{VCO_WORKTREE_GATE_HUB_UNREACHABLE}}"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "VCO_WORKTREE_GATE_HUB_UNREACHABLE": "1"})
    assert r.stdout.strip() == "FLAG="


# ── stop-codegraph-drain: additionalContext once per session ───────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_drain_hub_down_warns_once_per_session(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    _main, wt, edited = _layout(tmp_path, hub_rc=1)
    r = _drain(tmp_path, wt, edited, flavour, "sessA")
    assert r.returncode == 0, r.stderr
    envs = _envelopes(r.stdout)
    assert len(envs) == 1, r.stdout + r.stderr
    hso = envs[0]["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    ctx = hso["additionalContext"]
    assert ctx.startswith("[VCO hub unreachable]") and HINT in ctx
    assert "stale VCT_HUB_TOKEN" in ctx and "stop-codegraph-drain" in ctx
    assert ctx.rstrip().endswith("(Shown once per session.)")
    assert _sentinels(wt) == ["hub_unreachable_notice_sessA"]
    # Same session, hub still down: indexed again, said NOTHING again.
    r2 = _drain(tmp_path, wt, edited, flavour, "sessA")
    assert r2.returncode == 0, r2.stderr
    assert _envelopes(r2.stdout) == [], r2.stdout
    # Another session is told; so is a subagent (its own agent key).
    r3 = _drain(tmp_path, wt, edited, flavour, "sessB")
    assert len(_envelopes(r3.stdout)) == 1
    r4 = _drain(tmp_path, wt, edited, flavour, "sessA", agent_id="a1b2c3")
    assert len(_envelopes(r4.stdout)) == 1
    assert _sentinels(wt) == ["hub_unreachable_notice_sessA", "hub_unreachable_notice_sessA__a1b2c3",
                              "hub_unreachable_notice_sessB"]


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("hub_rc", [0, 2])
def test_drain_hub_answering_says_nothing(tmp_path: Path, flavour: str, hub_rc: int) -> None:
    """Leave-alone: registered (0) or definitively unregistered (2) is the
    hub answering — no notice, no sentinel."""
    _need(flavour)
    _main, wt, edited = _layout(tmp_path, hub_rc=hub_rc)
    r = _drain(tmp_path, wt, edited, flavour, "sessA")
    assert r.returncode == 0, r.stderr
    assert _envelopes(r.stdout) == [] and HINT not in r.stderr
    assert _sentinels(wt) == []


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_drain_main_tree_edit_never_probes(tmp_path: Path, flavour: str) -> None:
    """Leave-alone: an edit in the session's own (main) tree needs no hub
    answer, so a down hub is not reported by the gate."""
    _need(flavour)
    main, _wt, _edited = _layout(tmp_path, hub_rc=1)
    (main / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    f = main / "own.py"
    f.write_text("z = 2\n")
    r = _drain(tmp_path, main, f, flavour, "sessA")
    assert r.returncode == 0, r.stderr
    assert _envelopes(r.stdout) == []


# ── code-graph-incremental: stderr once per (unkeyed) hour ─────────────────


def _incremental(tmp_path: Path, wt: Path, edited: Path, flavour: str) -> subprocess.CompletedProcess:
    return subprocess.run(_argv("code-graph-incremental", flavour, str(edited), str(wt)),
                          capture_output=True, text=True, env=_env(tmp_path, wt),
                          cwd=str(wt), timeout=120)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_incremental_hub_down_warns_once(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    _main, wt, edited = _layout(tmp_path, hub_rc=1)
    r = _incremental(tmp_path, wt, edited, flavour)
    assert r.returncode == 0, r.stderr
    assert r.stderr.count("[VCO hub unreachable]") == 1 and HINT in r.stderr
    assert "code-graph-incremental" in r.stderr
    assert "(Shown at most once an hour: this caller has no session.)" in r.stderr
    sent = _sentinels(wt)
    assert len(sent) == 1 and sent[0].startswith("hub_unreachable_notice_nosession-")
    r2 = _incremental(tmp_path, wt, edited, flavour)
    assert r2.returncode == 0, r2.stderr
    assert "[VCO hub unreachable]" not in r2.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_incremental_hub_up_says_nothing(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    _main, wt, edited = _layout(tmp_path, hub_rc=0)
    r = _incremental(tmp_path, wt, edited, flavour)
    assert r.returncode == 0, r.stderr
    assert "[VCO hub unreachable]" not in r.stderr and _sentinels(wt) == []


# ── the shared notice helper (both siblings) ───────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_notice_helper_keys_and_repeats_when_the_sentinel_cannot_be_written(
        tmp_path: Path, flavour: str) -> None:
    """A hostile key collapses to the hour bucket (never raw bytes in a file
    name); an unwritable state dir repeats the notice rather than losing it."""
    _need(flavour)
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "state").write_text("not a dir")  # sentinel cannot be written
    if flavour == "sh":
        script = (f'. "{HOOKS}/_lib/emit-context.sh"\n'
                  f'for i in 1 2; do vco_report_hub_unreachable "{HOOKS}" "{proj}" "s1" "T" ""; '
                  'printf "N=%s\\n" "${#VCO_HUB_DOWN_NOTICE}"; done\n'
                  f'vco_report_hub_unreachable "{HOOKS}" "{tmp_path}/q" "../evil" "T" ""\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    else:
        script = (f". '{HOOKS}/_lib/emit-context.ps1'\n"
                  f"1..2 | ForEach-Object {{ $n = Get-VcoHubUnreachableNotice -HooksDir '{HOOKS}' "
                  f"-ProjectRoot '{proj}' -AgentKey 's1' -Who 'T'; \"N=$($n.Length)\" }}\n"
                  f"$null = Get-VcoHubUnreachableNotice -HooksDir '{HOOKS}' -ProjectRoot "
                  f"'{tmp_path}/q' -AgentKey '../evil' -Who 'T'\n")
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", script],
                           capture_output=True, text=True, timeout=60)
    counts = [line for line in r.stdout.split() if line.startswith("N=")]
    assert len(counts) == 2 and all(c != "N=0" for c in counts), r.stdout + r.stderr
    names = [p.name for p in (tmp_path / "q" / ".claude" / "state").iterdir()]
    assert len(names) == 1 and names[0].startswith("hub_unreachable_notice_nosession-"), names
