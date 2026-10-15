# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-N — four defects in the post-commit KG reviewer W6-K shipped.

* F-1 (HIGH): the gateway rungs were hand-spelled ``claude-gw/<bare>`` and
  compared exactly, but the live catalogue publishes
  ``claude-gw/glm-5.3-flash[1m]``, ``claude-gw/qwen/qwen3.8-flash[1m]`` and
  ``claude-gw/qwen/deepseek-v4.1-flash[1m]`` — no rung ever matched. The
  catalogue here is the REAL response shape (fixture), never the chain's
  own spelling.
* F-2 (LOW): without a ``claude`` CLI the gateway rungs (which spawn
  ``claude -p``) are skipped with a line, not chosen and failed at spawn.
* F-3 (MEDIUM): a ROOT commit was reviewed with an empty diff
  (``git diff HEAD~1..HEAD``); both hook siblings run on a scratch repo.
* F-4 (MEDIUM): the turn budget is one knob (default 30,
  ``VCO_KG_REVIEW_MAX_TURNS``) that reaches every spawn's argv.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"
AGENTS_DIR = REPO_ROOT / "templates" / "agents" / "module-gateway"
LIVE_SHAPE = (REPO_ROOT / "tests" / "fixtures" / "kg_reviewer"
              / "gateway-models-live-shape.json")

# v0.2.103 (owner ruling 2026-10-15): the fallback rungs are the comparable
# NON-flash gateway models; the F-1 spelling rules are pinned on them.
GLM = "claude-gw/glm-5.3[1m]"
KIMI = "claude-gw/kimi/k3[1m]"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for p in (str(SCRIPTS_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sb = _load("_w6n_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_w6n_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    monkeypatch.delenv("VCO_KG_REVIEW_MAX_TURNS", raising=False)
    yield mod
    sb.reset_backend_cache()


def _live_payload() -> dict:
    return json.loads(LIVE_SHAPE.read_text(encoding="utf-8"))


def _live_ids() -> frozenset:
    return frozenset(row["id"] for row in _live_payload()["data"])


class _Rig:
    def __init__(self, chain, monkeypatch, *, claude=True,
                 catalogue=frozenset(), base="http://127.0.0.1:11460",
                 consent=False):
        self.lines: list[str] = []
        self.chain = chain
        self.latched: dict[str, dict] = {}
        self.probed = 0
        # the chosen model (one machine-wide setting) pinned to haiku here
        monkeypatch.setenv("VCO_KG_REVIEW_MODEL", "haiku")
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude" if claude else None)

        def _cat(timeout_s=2.0):
            self.probed += 1
            return base, catalogue, None
        monkeypatch.setattr(chain, "gateway_catalogue", _cat)
        monkeypatch.setattr(chain._sb, "breaker_state", self.latched.get)
        monkeypatch.setattr(chain._sb, "openai_consent_granted",
                            lambda: consent)
        monkeypatch.setattr(chain._sb, "openai_available", lambda: True)

    def latch(self, tier: str) -> None:
        self.latched[tier] = {"reason": "quota", "remaining": 100}

    def decide(self):
        return self.chain.select_reviewer_backend(log=self.lines.append)


# ── F-1 ────────────────────────────────────────────────────────────────


def test_f1_live_catalogue_shape_serves_the_first_gateway_rung(
        chain, monkeypatch):
    """RED on HEAD: the hand-spelled rungs never matched the live ids."""
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    rig.latch("kg_reviewer:haiku")
    d = rig.decide()
    assert d["mode"] == "claude", (d, rig.lines)
    assert d["model"] == GLM, (d, rig.lines)
    assert d["base_url"] == "http://127.0.0.1:11460"


@pytest.mark.parametrize("latched, expected", [
    (("kg_reviewer:claude-gw/glm-5.3",), KIMI),
    (("kg_reviewer:claude-gw/glm-5.3", "kg_reviewer:claude-gw/kimi/k3"),
     None),
])
def test_f1_owner_order_walks_the_namespaced_rungs(
        chain, monkeypatch, latched, expected):
    """GLM-5.3 (z.ai, ``claude-gw/``) then Kimi K3 (``claude-gw/kimi/``);
    with both latched the walk skips — never to a served flash model."""
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    rig.latch("kg_reviewer:haiku")
    for tier in latched:
        rig.latch(tier)
    d = rig.decide()
    if expected is None:
        assert d["mode"] == "skip", (d, rig.lines)
    else:
        assert d["model"] == expected, rig.lines


def test_f1_plain_spelling_is_matched_and_1m_row_preferred(chain,
                                                           monkeypatch):
    """``[1m]`` is a client hint: a catalogue publishing only the plain id
    (WINDOW_ROWS_BOTH minus the 1M row) still serves the rung; with both
    spellings listed the ``[1m]`` row — the shipped agents' form — wins."""
    rig = _Rig(chain, monkeypatch, catalogue=frozenset(
        {"claude-gw/glm-5.3"}))
    rig.latch("kg_reviewer:haiku")
    assert rig.decide()["model"] == "claude-gw/glm-5.3"
    rig2 = _Rig(chain, monkeypatch, catalogue=frozenset(
        {"claude-gw/glm-5.3", GLM}))
    rig2.latch("kg_reviewer:haiku")
    assert rig2.decide()["model"] == GLM


def test_f1_hand_spelled_ids_are_not_accepted(chain, monkeypatch):
    """An id without the vendor's namespace (``claude-gw/k3`` is not the
    Kimi row) or on another vendor (GLM through the QwenCloud namespace)
    is NOT a rung."""
    rig = _Rig(chain, monkeypatch, catalogue=frozenset(
        {"claude-gw/k3", "claude-gw/qwen/glm-5.3"}))
    rig.latch("kg_reviewer:haiku")
    assert rig.decide()["mode"] == "skip", rig.lines


def test_f1_rung_ids_derive_from_model_router_and_match_shipped_agents(
        chain):
    """The rung spelling comes from the vendor table (one home) and equals
    the ``model:`` line of a shipped gateway agent for the same model."""
    from claude_mcp_servers.model_router.routing import with_1m
    from claude_mcp_servers.model_router.vendors import VENDORS
    agent_models = set()
    for md in AGENTS_DIR.glob("*.md"):
        m = re.search(r"^model:\s*(\S+)\s*$", md.read_text(encoding="utf-8"),
                      re.MULTILINE)
        if m:
            agent_models.add(m.group(1))
    rungs = chain.gateway_rungs()
    assert rungs == ("claude-gw/glm-5.3", "claude-gw/kimi/k3")
    for (vendor_id, bare), rung in zip(chain.GATEWAY_RUNG_MODELS, rungs):
        vendor = VENDORS[vendor_id]
        assert bare in (vendor.verified_ids or ()) + (vendor.static_ids or ()), (
            vendor_id, bare)
        assert with_1m(rung) in agent_models, (rung, sorted(agent_models))


class _LiveShapeGateway(http.server.BaseHTTPRequestHandler):
    token = "stub-host-token"

    def do_GET(self):
        if self.path != "/v1/models" or (
                self.headers.get("Authorization") != f"Bearer {self.token}"):
            self.send_error(401)
            return
        body = LIVE_SHAPE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def test_f1_end_to_end_through_a_live_shape_gateway(chain, monkeypatch,
                                                    tmp_path):
    """The REAL probe (urllib, bearer token) against a stub serving the
    live response bytes, then the walk: the GLM ``[1m]`` row is chosen."""
    import vco_lib.vscode_settings as _vs
    import claude_mcp_servers.model_router.config as _mrc
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _LiveShapeGateway)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(_vs, "resolve_gateway_ports", lambda: (port,))
        token_file = tmp_path / "model-gateway.token"
        token_file.write_text("stub-host-token\n", encoding="utf-8")
        monkeypatch.setattr(_mrc, "token_path", lambda: token_file)
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude")
        monkeypatch.setenv("VCO_KG_REVIEW_MODEL", "haiku")
        monkeypatch.setattr(chain._sb, "breaker_state",
                            lambda tier: ({"reason": "quota", "remaining": 1}
                                          if tier == "kg_reviewer:haiku"
                                          else None))
        lines: list[str] = []
        d = chain.select_reviewer_backend(log=lines.append)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert d["model"] == GLM, (d, lines)
    assert d["base_url"] == f"http://127.0.0.1:{port}"


# ── F-2 ────────────────────────────────────────────────────────────────


def test_f2_no_claude_cli_skips_the_gateway_rungs_with_a_line(chain,
                                                              monkeypatch):
    rig = _Rig(chain, monkeypatch, claude=False, catalogue=_live_ids(),
               consent=True)
    d = rig.decide()
    assert d["mode"] == "openai", (d, rig.lines)
    for rung in chain.gateway_rungs():
        assert any(f"rung {rung}: unavailable (no `claude` CLI on PATH"
                   in ln for ln in rig.lines), rig.lines
    assert rig.probed == 0, "no CLI: the catalogue probe is not even needed"


# ── F-4 ────────────────────────────────────────────────────────────────


def _capture_run(chain, monkeypatch, tmp_path, decision, argv_extra=()):
    """``run`` against a scratch repo with only the ``claude`` spawn faked
    (git itself runs for real: v0.2.103's run reads the commit and guards
    the tree)."""
    seen: dict = {}
    real_run = subprocess.run
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["-c", "user.name=t", "-c",
                                  "user.email=t@e.invalid", "commit", "-q",
                                  "--allow-empty", "-m", "c"]):
        real_run(["git", *args], cwd=repo, check=True, capture_output=True)

    def _fake_run(cmd, *a, **kw):
        if not (cmd and cmd[0] == "claude"):
            return real_run(cmd, *a, **kw)
        seen["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"type": "result", "subtype": "success",
                                       "num_turns": 7, "result": "done"}),
            stderr="")
    monkeypatch.setattr(chain, "select_reviewer_backend",
                        lambda log=None, model=None: decision)
    monkeypatch.setattr(chain, "subprocess", types.SimpleNamespace(
        run=_fake_run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(chain, "_gateway_auth_env",
                        lambda base: {"ANTHROPIC_BASE_URL": base})
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: None)
    log = tmp_path / "review.log"
    assert chain.main(["run", "--project-root", str(repo), "--log", str(log),
                       *argv_extra]) == 0
    return seen["cmd"], log.read_text(encoding="utf-8")


_DECISIONS = [
    {"mode": "claude", "model": "sonnet", "base_url": None, "reason": "t"},
    {"mode": "claude", "model": GLM, "base_url": "http://127.0.0.1:1",
     "reason": "t"},
    {"mode": "claude", "model": KIMI, "base_url": "http://127.0.0.1:1",
     "reason": "t"},
]


@pytest.mark.parametrize("decision", _DECISIONS, ids=lambda d: d["model"])
def test_f4_default_budget_reaches_every_rung_spawn(chain, monkeypatch,
                                                    tmp_path, decision):
    cmd, log = _capture_run(chain, monkeypatch, tmp_path, decision)
    assert cmd[cmd.index("--model") + 1] == decision["model"]
    assert cmd[cmd.index("--max-turns") + 1] == "30", cmd
    assert chain.DEFAULT_MAX_TURNS == 30
    # the run line records the turns the budget is tuned from
    assert f"model={decision['model']} exit=0 turns=7 subtype=success" in log


@pytest.mark.parametrize("decision", _DECISIONS, ids=lambda d: d["model"])
def test_f4_env_knob_reaches_every_rung_spawn(chain, monkeypatch, tmp_path,
                                              decision):
    monkeypatch.setenv("VCO_KG_REVIEW_MAX_TURNS", "45")
    cmd, _log = _capture_run(chain, monkeypatch, tmp_path, decision)
    assert cmd[cmd.index("--max-turns") + 1] == "45", cmd


@pytest.mark.parametrize("raw", ["0", "-3", "lots", ""])
def test_f4_bad_knob_values_fall_back_to_the_default(chain, monkeypatch,
                                                     raw):
    monkeypatch.setenv("VCO_KG_REVIEW_MAX_TURNS", raw)
    assert chain.resolve_max_turns() == 30
    assert chain.resolve_max_turns(12) == 12


def test_f4_non_json_output_is_logged_verbatim(chain):
    text = chain._run_log_text("haiku", 1, "Error: something", 0.0)
    assert text.startswith("Error: something")
    assert "turns=?" in text


# ── hook siblings on a scratch repo (F-3 + the F-4 knob on the hook) ──────


_FAKE_CLAUDE = """#!/usr/bin/env bash
out="$FAKE_CLAUDE_CAPTURE"
{ for a in "$@"; do printf 'ARG:%s\\n' "$a"; done
  printf 'STDIN:\\n'; cat; } > "$out.tmp"
mv "$out.tmp" "$out"
printf '{"type":"result","subtype":"success","num_turns":4,"result":"ok"}'
"""


def _scratch_repo(tmp_path: Path, *, commits: int) -> "tuple[Path, dict]":
    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                        "VCO_KG_REVIEW_MAX_TURNS", "VCO_KG_REVIEW_MODEL",
                        "VCT_LAUNCHER_DB_PATH",
                        # The hook resolves the PROJECT root by the one rule
                        # (CLAUDE_PROJECT_DIR first); the suite pins it to a
                        # scratch dir, so the scratch repo must not inherit it.
                        "CLAUDE_PROJECT_DIR")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "FAKE_CLAUDE_CAPTURE": str(tmp_path / "capture.txt"),
        "VCT_STATE_DIR": str(tmp_path / "state"),
        "STDERR_CAP_DISABLE": "1",
    })

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True)
    git("init", "-q")
    (repo / "first_file.txt").write_text("root-commit-marker-line\n",
                                         encoding="utf-8")
    git("add", "first_file.txt")
    git("commit", "-q", "-m", "root")
    if commits > 1:
        (repo / "second_file.txt").write_text("second-commit-marker\n",
                                              encoding="utf-8")
        git("add", "second_file.txt")
        git("commit", "-q", "-m", "second")
    return repo, env


def _wait_capture(env) -> str:
    path = Path(env["FAKE_CLAUDE_CAPTURE"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.2)
    raise AssertionError("the hook never spawned the (fake) claude CLI")


def _hook_cmd(sibling: str) -> "list[str]":
    if sibling == "sh":
        return ["bash", str(HOOKS_DIR / "post-git-commit-kg-sync.sh")]
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    return [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
            str(HOOKS_DIR / "post-git-commit-kg-sync.ps1")]


def _run_hook(sibling: str, repo: Path, env: dict) -> None:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "git commit -m x"},
                          "tool_response": "[main abc123] committed"})
    proc = subprocess.run(_hook_cmd(sibling), cwd=repo, env=env,
                          input=payload, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def _argv_and_prompt(capture: str) -> "tuple[list[str], str]":
    argv = [ln[4:] for ln in capture.splitlines() if ln.startswith("ARG:")]
    return argv, capture


def _install_chain(repo: Path, env: dict) -> None:
    """Ship the reviewer chain into the scratch project (what a bundle
    install does) and point it at this checkout's vco_lib. v0.2.103: the
    commit capture and prompt live in the chain, so every diff assertion
    runs through it (the hook's unguarded no-python spawn is retired)."""
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("kg_reviewer_backend.py", "summary_backends.py"):
        shutil.copy2(SCRIPTS_DIR / name, scripts / name)
    env["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_f3_root_commit_is_reviewed_with_its_diff(tmp_path, sibling):
    """RED on HEAD: a single-commit repo produced an EMPTY diff."""
    repo, env = _scratch_repo(tmp_path, commits=1)
    _install_chain(repo, env)
    _run_hook(sibling, repo, env)
    _argv, everything = _argv_and_prompt(_wait_capture(env))
    assert "first_file.txt" in everything
    assert "+root-commit-marker-line" in everything


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_f3_later_commit_still_diffs_against_its_parent(tmp_path, sibling):
    repo, env = _scratch_repo(tmp_path, commits=2)
    _install_chain(repo, env)
    _run_hook(sibling, repo, env)
    _argv, everything = _argv_and_prompt(_wait_capture(env))
    assert "+second-commit-marker" in everything
    assert "root-commit-marker-line" not in everything


# test_f4_hook_no_python_branch_mirrors_the_knob is retired with the branch
# it pinned (v0.2.103): a broken install no longer spawns an unguarded
# `claude` with a mirrored budget — it reports and does not review
# (tests/test_v02102_w6r_kg_reviewer_broken_install.py).


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
@pytest.mark.parametrize("raw, expected", [(None, "30"), ("44", "44"),
                                           ("zero", "30"), ("0", "30")])
def test_f4_hook_chain_branch_uses_the_chain_knobs(tmp_path, sibling, raw,
                                                   expected):
    """The hook passes NO budget or model of its own: the spawned claude
    gets the chain's DEFAULT_MAX_TURNS / VCO_KG_REVIEW_MAX_TURNS (RED on
    the W6-K HEAD: 10) and the machine-wide model (default sonnet)."""
    repo, env = _scratch_repo(tmp_path, commits=1)
    _install_chain(repo, env)
    if raw is not None:
        env["VCO_KG_REVIEW_MAX_TURNS"] = raw
    _run_hook(sibling, repo, env)
    argv, _ = _argv_and_prompt(_wait_capture(env))
    assert argv[argv.index("--model") + 1] == "sonnet", argv
    assert argv[argv.index("--max-turns") + 1] == expected, argv
