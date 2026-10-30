# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 rm guard — what an agent's ``rm`` may delete.

Owner ruling 2026-10-29: hard-block the home directory itself and the
system paths; a deeper path under home is allowed, but a recursive or glob
delete is refused ONCE ("are you sure ...") and the identical retry is
allowed. The old ``rm_root`` rule blocked every rm under /home and had no
word boundary (``confirm /home`` matched).

One home: ``vco_lib/rm_guard.py`` (structural walk + confirm-once store),
called by ``templates/scripts/bash_security.py``; the scanner's flat
``rm_root`` regex is the text backstop for the HARD set only, pinned to the
structural tier by the parity test below. The hooks
``pre-tool-use.{sh,ps1}`` hand the scanner the session / agent / state dir.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vco_lib import rm_guard

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "templates" / "scripts"
HOOKS = REPO / "templates" / "hooks"
sys.path.insert(0, str(SCRIPTS))
import bash_security as bs  # noqa: E402  # pyright: ignore[reportMissingImports]

HOME = "/home/tester"

HARD_TARGETS = [
    "/", "/*", "/.", "~", "~/", "~/*", "~/.*", "$HOME", "$HOME/", "${HOME}",
    "${HOME}/*", "/home", "/home/", "/home/*", "/home/tester", "/home/tester/",
    "/home/tester/*", "/home/other", "/etc", "/etc/x", "/usr/lib", "/var/log/x",
    "/boot", "/dev/sda", "/root", "/root/x", "~bob", "/Users", "/Users/me",
    "/home/tester/a/../..",
]
DEEP_TARGETS = [
    "/home/tester/proj", "/home/tester/a/b", "~/proj", "~/proj/x", "$HOME/proj",
    "${HOME}/a/b", "/home/other/x", "~bob/x", "/Users/me/x",
]
FREE_TARGETS = ["build", "./a", "/tmp/x", "/opt/thing", "$DIR/x", "$HOMEX/y",
                "*.pyc", "../x"]


def _env(tmp_path: Path, session: str = "s1", agent: str = "") -> dict:
    return {rm_guard.ENV_SESSION: session, rm_guard.ENV_AGENT: agent,
            rm_guard.ENV_STATE_DIR: str(tmp_path / "state")}


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    """check_command reads the confirm context from os.environ (the hook
    sets it); HOME is pinned so `~` resolves predictably."""
    monkeypatch.setenv("HOME", HOME)

    def set_ctx(session="s1", agent=""):
        for k, v in _env(tmp_path, session, agent).items():
            monkeypatch.setenv(k, v)
    set_ctx()
    return set_ctx


# ── classification ─────────────────────────────────────────────────────


@pytest.mark.parametrize("t", HARD_TARGETS)
def test_hard_targets(t):
    assert rm_guard.classify_target(t, HOME) == rm_guard.HARD, t


@pytest.mark.parametrize("t", DEEP_TARGETS)
def test_deep_targets(t):
    assert rm_guard.classify_target(t, HOME) == "home_deep", t


@pytest.mark.parametrize("t", FREE_TARGETS)
def test_free_targets(t):
    assert rm_guard.classify_target(t, HOME) is None, t


def test_real_home_outside_slash_home_is_protected():
    assert rm_guard.classify_target("/data/me", "/data/me") == rm_guard.HARD
    assert rm_guard.classify_target("/data", "/data/me") == rm_guard.HARD
    assert rm_guard.classify_target("/data/me/p", "/data/me") == "home_deep"
    assert rm_guard.classify_target("/data/other", "/data/me") is None


@pytest.mark.parametrize("cmd,tiers", [
    ("rm -rf ~/proj/x", ["confirm"]),
    ("rm -r ~/proj/x", ["confirm"]),
    ("rm -R ~/proj/x", ["confirm"]),
    ("rm --recursive ~/proj/x", ["confirm"]),
    ("rm -fr /home/tester/a/b", ["confirm"]),
    ("rm ~/proj/x -rf", ["confirm"]),            # GNU rm permutes options
    ("rm ~/proj/*.log", ["confirm"]),            # a glob
    ("rm ~/proj/file.txt", []),                  # plain file
    ("rm -f /home/tester/a/b.txt", []),
    ("rm -- -r", []),                            # after --, a file named -r
    ("rm -rf build ~/p/q /tmp/z", ["confirm"]),
    ("rm -rf ./a /etc", ["hard"]),               # every operand is judged
    ("sudo rm -rf ~/p/q", ["confirm"]),
    ("/bin/rm -rf ~/p/q", ["confirm"]),
    ("command rm -rf ~/p/q", ["confirm"]),
    ("xargs rm -rf ~/p/q", ["confirm"]),
    ("timeout 5 rm -rf ~/p/q", ["confirm"]),
    ("ls && rm -rf ~/p/q; echo", ["confirm"]),
    ("bash -c 'rm -rf ~/p/q'", ["confirm"]),
    ("eval 'rm -rf ~/p/q'", ["confirm"]),
    ("x=$(rm -rf ~/p/q)", ["confirm"]),
    ("echo 'rm -rf ~/p/q'", []),                 # prose, not a command
    ("git commit -m 'rm -rf ~/p/q'", []),
    ("git rm -r ~/p/q", []),                     # git's rm, not rm
])
def test_scan(cmd, tiers):
    assert [f.tier for f in rm_guard.scan(cmd, HOME)] == tiers, cmd


# ── the old false positives (no word boundary) ─────────────────────────


@pytest.mark.parametrize("cmd", [
    "git worktree add -b v0.2.105-rmconfirm /home/u/x main",
    "echo confirm /home",
    "printf 'perform /etc check'",
    "./confirm /home/u",
    "farm /usr",
])
def test_old_false_positives_pass(cmd, guard_env):
    assert bs.check_command(cmd) == (True, ""), cmd


def test_flat_rule_needs_rm_as_a_word():
    pat = dict((n, p) for n, p, _ in bs._RULES)["rm_root"]
    assert not pat.search("confirm /home")
    assert not pat.search("git worktree add -b v-rmconfirm /home/u/x")
    assert pat.search("rm -rf /home")
    assert pat.search("/bin/rm -rf /home")


# ── parity: the flat backstop is exactly the structural HARD tier ──────


@pytest.mark.parametrize("t", HARD_TARGETS + DEEP_TARGETS + FREE_TARGETS)
def test_flat_backstop_matches_structural_hard_tier(t):
    # Exceptions, documented: the flat rule cannot resolve `..` or know the
    # real home dir, so those spellings are judged structurally only.
    if ".." in t:
        pytest.skip("`..` resolved structurally only")
    pat = dict((n, p) for n, p, _ in bs._RULES)["rm_root"]
    want = rm_guard.classify_target(t, HOME) == rm_guard.HARD
    for cmd in ("rm -rf %s" % t, "rm %s" % t, 'rm -rf "%s"' % t):
        assert bool(pat.search(cmd)) == want, cmd


def test_flat_backstop_is_linear():
    pat = dict((n, p) for n, p, _ in bs._RULES)["rm_root"]
    for tok in ("rm ~a", "rm -rf /home/u", "rm -x -y", "rm /homex", "rm $HOMEx"):
        s = "; ".join([tok] * 40000)
        t0 = time.monotonic()
        pat.search(s)
        assert time.monotonic() - t0 < 1.5, tok


def test_check_command_is_fast_on_a_huge_rm_command(guard_env):
    cmd = "; ".join(["rm -rf ~/a/b"] * 40000)
    t0 = time.monotonic()
    ok, reason = bs.check_command(cmd)
    assert time.monotonic() - t0 < 1.5
    assert not ok and "[rm_confirm]" in reason


# ── hard block: never confirmable ──────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "rm -rf /etc/x", "rm -rf ~", "rm -rf ~/", "rm -rf /home/user/", "rm -rf /",
    "rm -rf $HOME", "rm -rf /home/*", "rm -rf ~/*", "bash -c 'rm -rf ~'",
])
def test_hard_block_stays_blocked_on_retry(cmd, guard_env, tmp_path):
    for _ in range(3):
        ok, reason = bs.check_command(cmd)
        assert not ok and "[rm_root]" in reason, (cmd, reason)
    assert not (tmp_path / "state" / rm_guard.STATE_SUBDIR).exists() or \
        not os.listdir(tmp_path / "state" / rm_guard.STATE_SUBDIR)


# ── confirm once ───────────────────────────────────────────────────────

DEEP = "rm -rf /home/tester/proj/build"


def test_first_refused_second_allowed_third_refused(guard_env):
    ok, reason = bs.check_command(DEEP)
    assert not ok
    assert reason.startswith("[rm_confirm] Are you sure you want to delete all "
                             "the contents of /home/tester/proj/build?")
    assert "retry the exact same command" in reason
    assert bs.check_command(DEEP) == (True, "")
    ok, reason = bs.check_command(DEEP)
    assert not ok and "[rm_confirm]" in reason


def test_whitespace_only_difference_is_the_same_command(guard_env):
    assert not bs.check_command(DEEP)[0]
    assert bs.check_command("  rm  -rf   /home/tester/proj/build\n")[0]


def test_payload_recursion_consumes_one_confirmation(guard_env):
    cmd = "bash -c 'rm -rf ~/proj/build'"
    assert not bs.check_command(cmd)[0]
    assert bs.check_command(cmd)[0]
    assert not bs.check_command(cmd)[0]


def test_different_command_is_refused(guard_env):
    assert not bs.check_command(DEEP)[0]
    assert not bs.check_command("rm -rf /home/tester/proj/other")[0]


def test_different_session_is_refused(guard_env):
    assert not bs.check_command(DEEP)[0]
    guard_env(session="s2")
    assert not bs.check_command(DEEP)[0]


def test_subagent_retry_is_its_own(guard_env):
    guard_env(agent="agentA")
    assert not bs.check_command(DEEP)[0]
    guard_env(agent="agentB")
    assert not bs.check_command(DEEP)[0]
    guard_env(agent="")
    assert not bs.check_command(DEEP)[0]   # the main chat is its own too
    guard_env(agent="agentA")
    assert bs.check_command(DEEP)[0]


def test_expired_confirmation_is_refused(tmp_path):
    env = _env(tmp_path)
    t0 = 1_000_000.0
    assert rm_guard.confirm_once(DEEP, "s1", "", env[rm_guard.ENV_STATE_DIR],
                                 now=t0) == (False, "")
    late = t0 + rm_guard.CONFIRM_TTL_S + 1
    assert rm_guard.confirm_once(DEEP, "s1", "", env[rm_guard.ENV_STATE_DIR],
                                 now=late) == (False, "")
    # ... and that late attempt is itself a fresh first attempt
    assert rm_guard.confirm_once(DEEP, "s1", "", env[rm_guard.ENV_STATE_DIR],
                                 now=late + 5) == (True, "")


def test_within_ttl_is_allowed(tmp_path):
    d = str(tmp_path / "state")
    assert not rm_guard.confirm_once(DEEP, "s1", "", d, now=100.0)[0]
    assert rm_guard.confirm_once(DEEP, "s1", "", d,
                                 now=100.0 + rm_guard.CONFIRM_TTL_S - 1)[0]


def test_plain_file_rm_under_home_needs_no_confirm(guard_env, tmp_path):
    assert bs.check_command("rm /home/tester/proj/notes.txt") == (True, "")
    assert not (tmp_path / "state" / rm_guard.STATE_SUBDIR).exists()


def test_state_files_are_private_and_hold_no_command_text(guard_env, tmp_path):
    assert not bs.check_command(DEEP)[0]
    d = tmp_path / "state" / rm_guard.STATE_SUBDIR
    files = list(d.iterdir())
    assert len(files) == 1
    assert "proj" not in files[0].name and "build" not in files[0].name
    body = files[0].read_text()
    assert "proj" not in body and float(body) > 0
    if os.name == "posix":
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


# ── fail-closed ────────────────────────────────────────────────────────


def test_no_session_stays_refused(guard_env, monkeypatch):
    monkeypatch.setenv(rm_guard.ENV_SESSION, "")
    for _ in range(2):
        ok, reason = bs.check_command(DEEP)
        assert not ok and "no chat session id" in reason


def test_no_context_at_all_stays_refused(monkeypatch):
    for k in (rm_guard.ENV_SESSION, rm_guard.ENV_AGENT, rm_guard.ENV_STATE_DIR):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", HOME)
    assert not bs.check_command(DEEP)[0]
    assert not bs.check_command(DEEP)[0]


def test_unwritable_state_stays_refused(guard_env, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv(rm_guard.ENV_STATE_DIR, str(blocker))
    for _ in range(2):
        ok, reason = bs.check_command(DEEP)
        assert not ok and "could not be recorded or read" in reason, reason


def test_broken_install_refuses_home_rm_only(monkeypatch, guard_env):
    monkeypatch.setitem(sys.modules, "vco_lib.rm_guard", None)
    ok, reason = bs.check_command(DEEP)
    assert not ok and "[rm_guard]" in reason and "broken VCO install" in reason
    assert bs.check_command("rm -rf build")[0]
    assert bs.check_command("rm /tmp/x.txt")[0]


# ── the real hook (sh; ps1 where pwsh exists) ──────────────────────────

HAVE_PWSH = shutil.which("pwsh") is not None
_IMPLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    not HAVE_PWSH, reason="pwsh not installed"))]


def _hook_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "logs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "bash_security.py",
                 proj / ".claude" / "scripts" / "bash_security.py")
    return proj


def _run_hook(impl: str, proj: Path, cmd: str, sid: str = "sessRM",
              agent: str = "") -> subprocess.CompletedProcess:
    payload = {"session_id": sid, "tool_name": "Bash",
               "tool_input": {"command": cmd}}
    if agent:
        payload["agent_id"] = agent
    env = dict(os.environ)
    for k in ("VCT_DISABLE_HOOKS", "VCT_VENV", "VCT_INSTALL_ROOT",
              rm_guard.ENV_SESSION, rm_guard.ENV_AGENT, rm_guard.ENV_STATE_DIR):
        env.pop(k, None)
    env.update(CLAUDE_PROJECT_DIR=str(proj), PYTHONPATH=str(REPO),
               WEAVIATE_URL="http://127.0.0.1:9", HOME=HOME)
    if impl == "sh":
        argv = ["bash", str(HOOKS / "pre-tool-use.sh")]
    else:
        from tests.common.pwsh import PWSH_BASE_FLAGS
        argv = [shutil.which("pwsh") or "pwsh", *PWSH_BASE_FLAGS,
                "-NonInteractive", "-File", str(HOOKS / "pre-tool-use.ps1")]
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120, cwd=str(proj), env=env)


@pytest.mark.parametrize("impl", _IMPLS)
def test_hook_confirm_once_end_to_end(impl, tmp_path):
    proj = _hook_project(tmp_path)
    cmd = "rm -rf /home/tester/proj/build"
    r1 = _run_hook(impl, proj, cmd)
    assert r1.returncode == 2, r1.stderr
    assert "Are you sure you want to delete all the contents of" in r1.stderr
    r2 = _run_hook(impl, proj, cmd)
    assert r2.returncode == 0, r2.stderr
    r3 = _run_hook(impl, proj, cmd)
    assert r3.returncode == 2, r3.stderr
    # another session's retry is not this one's confirmation
    assert _run_hook(impl, proj, cmd, sid="sessOther").returncode == 2
    assert _run_hook(impl, proj, cmd, sid="sessThird").returncode == 2
    # a subagent's retry is its own
    assert _run_hook(impl, proj, cmd, sid="sessThird", agent="ag1").returncode == 2
    assert _run_hook(impl, proj, cmd, sid="sessThird", agent="ag1").returncode == 0


@pytest.mark.parametrize("impl", _IMPLS)
def test_hook_hard_block_and_plain_rm(impl, tmp_path):
    proj = _hook_project(tmp_path)
    for _ in range(2):
        r = _run_hook(impl, proj, "rm -rf ~/")
        assert r.returncode == 2 and "rm_root" in r.stderr, r.stderr
    assert _run_hook(impl, proj, "rm /home/tester/proj/a.txt").returncode == 0
    assert _run_hook(impl, proj,
                     "git worktree add -b v0.2.105-rmconfirm /home/tester/x main"
                     ).returncode == 0


def test_module_reload_keeps_rules():
    # bash_security is reloaded by other test files; the rm guard wiring
    # must survive a reload (it imports lazily).
    importlib.reload(bs)
    assert any(n == "rm_root" for n, _, _ in bs._RULES)
