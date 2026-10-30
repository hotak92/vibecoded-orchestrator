# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105: the hub stays up mid-session, the gateway revives it, and a
subagent launch on a gateway model is refused when its vendor key cannot
resolve.

* ``vco_lib.hub_ensure.revive_if_down`` — the ONE revive home: leave a live
  hub alone, stand aside during an update, rate-limit, spawn + log.
* the per-prompt hook calls it (``user-prompt-submit-reminder.{sh,ps1}``).
* the gateway's key resolver calls its reviver when the hub REFUSED the read
  (``model_router.secrets``), and not otherwise.
* ``GET /vendor-keys`` answers names only.
* ``vco_lib.gateway_preflight`` refuses only a DEFINITE "cannot", and the
  Agent|Task hook passes its deny through before the KG leg.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import NoReturn

import pytest

from model_router.secrets import VendorKeyResolver, _is_hub_unreachable
from model_router.vendors import Vendor
from tests.common.pwsh import PWSH_BASE_FLAGS
from tests.test_model_router_server import FAKE_VENDOR_KEY, GatewayTestBase
from vco_lib import gateway_preflight as gp
from vco_lib import hub_ensure as he

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")
needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")


# --------------------------------------------------------------------------- #
# revive_if_down
# --------------------------------------------------------------------------- #

class _Ensure:
    def __init__(self, state=he.EnsureState.STARTED, reason="invoked"):
        self.calls = 0
        self.state = state
        self.reason = reason

    def __call__(self, **kwargs):
        self.calls += 1
        return he.EnsureResult(state=self.state, binary="/x/vct-hub", reason=self.reason)


@pytest.fixture
def vct_root(tmp_path, monkeypatch):
    root = tmp_path / "vct"
    root.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    assert he.vct_root_dir() == root
    return root


def test_revive_leaves_a_live_hub_alone(vct_root, monkeypatch):
    monkeypatch.setattr(he, "is_running", lambda: True)
    ensure = _Ensure()
    res = he.revive_if_down(source="t", ensure=ensure, update_in_progress=lambda: False)
    assert res.state is he.ReviveState.ALREADY_RUNNING
    assert ensure.calls == 0
    assert not (vct_root / "state" / he.REVIVE_STAMP_FILE).exists(), "a live hub writes nothing"


def test_revive_starts_a_dead_hub_and_logs_it(vct_root, monkeypatch):
    monkeypatch.setattr(he, "is_running", lambda: False)
    ensure = _Ensure()
    res = he.revive_if_down(source="prompt", ensure=ensure, update_in_progress=lambda: False)
    assert res.state is he.ReviveState.STARTED
    assert ensure.calls == 1
    log = (vct_root / "logs" / he.REVIVE_LOG_FILE).read_text("utf-8")
    assert "source=prompt state=started" in log


def test_revive_stands_aside_during_an_update(vct_root, monkeypatch):
    monkeypatch.setattr(he, "is_running", lambda: False)
    ensure = _Ensure()
    res = he.revive_if_down(source="t", ensure=ensure, update_in_progress=lambda: True)
    assert res.state is he.ReviveState.UPDATE_IN_PROGRESS
    assert ensure.calls == 0


def test_revive_reads_the_real_update_gate(vct_root, monkeypatch):
    """The default gate is vco_lib.update_gate (in-JSON deadline)."""
    from vco_lib import update_gate

    monkeypatch.setattr(he, "is_running", lambda: False)
    update_gate.write_lockfile(phase="install_py")
    ensure = _Ensure()
    assert he.revive_if_down(source="t", ensure=ensure).state is he.ReviveState.UPDATE_IN_PROGRESS
    update_gate.delete_lockfile()
    assert he.revive_if_down(source="t", ensure=ensure).state is he.ReviveState.STARTED


def test_revive_is_rate_limited_machine_wide(vct_root, monkeypatch):
    monkeypatch.setattr(he, "is_running", lambda: False)
    ensure = _Ensure()
    now = [10_000.0]
    clock = lambda: now[0]  # noqa: E731
    gate = lambda: False  # noqa: E731
    assert he.revive_if_down(source="a", ensure=ensure, clock=clock, update_in_progress=gate).state \
        is he.ReviveState.STARTED
    now[0] += 30
    assert he.revive_if_down(source="b", ensure=ensure, clock=clock, update_in_progress=gate).state \
        is he.ReviveState.RATE_LIMITED
    assert ensure.calls == 1
    now[0] += he.REVIVE_MIN_INTERVAL_S
    assert he.revive_if_down(source="c", ensure=ensure, clock=clock, update_in_progress=gate).state \
        is he.ReviveState.STARTED
    assert ensure.calls == 2


def test_revive_reports_a_failed_start(vct_root, monkeypatch):
    monkeypatch.setattr(he, "is_running", lambda: False)
    ensure = _Ensure(state=he.EnsureState.BINARY_NOT_FOUND, reason="vct-hub binary not found")
    res = he.revive_if_down(source="gateway", ensure=ensure, update_in_progress=lambda: False)
    assert res.state is he.ReviveState.FAILED
    assert "binary not found" in (vct_root / "logs" / he.REVIVE_LOG_FILE).read_text("utf-8")


def test_revive_never_raises(vct_root, monkeypatch):
    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(he, "is_running", boom)
    assert he.revive_if_down(source="t").state is he.ReviveState.FAILED


def test_revive_cli_exits_zero_and_speaks_json(vct_root, monkeypatch, capsys):
    monkeypatch.setattr(he, "is_running", lambda: True)
    assert he.main(["revive", "--source", "cli", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "already_running"


# --------------------------------------------------------------------------- #
# The per-prompt hook calls the revive (wiring, through a recording python)
# --------------------------------------------------------------------------- #

_RECORDER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{out}"
exit 0
"""


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    out = tmp_path / "argv.log"
    py = venv / "bin" / "python"
    py.write_text(_RECORDER.format(out=out), encoding="utf-8")
    py.chmod(0o755)
    return venv, out


def _prompt_env(tmp_path: Path, venv: Path) -> dict:
    from tests.common.child_env import child_env

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    env = child_env(CLAUDE_PROJECT_DIR=str(proj), VCT_VENV=str(venv), TMPDIR=str(tmp_path))
    env.pop("VCT_DISABLE_HOOKS", None)
    return env


def _wait_for(path: Path, needle: str, timeout: float = 10.0) -> str:
    import time

    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text("utf-8") if path.exists() else ""
        if needle in text:
            return text
        time.sleep(0.05)
    return text


@needs_bash
def test_sh_prompt_hook_asks_the_revive_home(tmp_path):
    venv, out = _fake_venv(tmp_path)
    proc = subprocess.run(
        ["bash", str(HOOKS / "user-prompt-submit-reminder.sh")],
        input=json.dumps({"session_id": "s1"}), capture_output=True, text=True,
        env=_prompt_env(tmp_path, venv), timeout=60)
    assert proc.returncode == 0, proc.stderr
    log = _wait_for(out, "vco_lib.hub_ensure revive")
    assert "-m vco_lib.hub_ensure revive --source prompt --quiet" in log, log


@needs_bash
def test_sh_prompt_hook_outside_a_project_asks_nothing(tmp_path):
    venv, out = _fake_venv(tmp_path)
    env = _prompt_env(tmp_path, venv)
    env["CLAUDE_PROJECT_DIR"] = ""
    proc = subprocess.run(
        ["bash", str(HOOKS / "user-prompt-submit-reminder.sh")],
        input="{}", capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "hub_ensure revive" not in _wait_for(out, "hub_ensure revive", timeout=1.0)


@needs_pwsh
def test_ps1_prompt_hook_asks_the_revive_home(tmp_path):
    venv, out = _fake_venv(tmp_path)
    proc = subprocess.run(
        ["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / "user-prompt-submit-reminder.ps1")],
        input=json.dumps({"session_id": "s1"}), capture_output=True, text=True,
        env=_prompt_env(tmp_path, venv), timeout=120)
    assert proc.returncode == 0, proc.stderr
    log = _wait_for(out, "vco_lib.hub_ensure revive")
    assert "-m vco_lib.hub_ensure revive --source prompt --quiet" in log, log


# --------------------------------------------------------------------------- #
# The gateway resolver revives the hub when the hub REFUSED the read
# --------------------------------------------------------------------------- #

ACME = Vendor(
    vendor_id="acme",
    display_suffix=" · Acme",
    namespace="claude-acme/",
    upstream="https://api.acme.example",
    secret_keys=("acme_primary_key", "acme_legacy_key"),
    bare_id_prefixes=("acme-",),
)


def _hub_down_miss(key: str, project: str | None = None) -> NoReturn:
    """What agent_secrets.get raises with the hub down and the file store empty."""
    from vco_lib.agent_secrets import SecretNotFound
    from vco_lib.project_config import HubNotRunning

    try:
        raise HubNotRunning("connection refused")
    except HubNotRunning as hub:
        raise SecretNotFound(f"secret {key!r} not found") from hub


def _hub_running_but_refusing(key: str, project: str | None = None) -> NoReturn:
    """A RUNNING hub that answered 401 (e.g. a rotated token): not a dead hub."""
    from vco_lib.agent_secrets import SecretNotFound
    from vco_lib.project_config import HubUnreachable

    try:
        raise HubUnreachable("hub answered 401")
    except HubUnreachable as hub:
        raise SecretNotFound(f"secret {key!r} not found") from hub


def _plain_miss(key: str, project: str | None = None) -> NoReturn:
    from vco_lib.agent_secrets import SecretNotFound

    raise SecretNotFound(f"secret {key!r} not found")


class GatewayReviveTests(unittest.TestCase):
    def test_a_refused_hub_read_starts_the_hub_once_per_resolution(self) -> None:
        calls: list[int] = []
        resolver = VendorKeyResolver(getter=_hub_down_miss, hub_reviver=lambda: calls.append(1) or "started")
        with self.assertLogs("model_router.secrets", level="WARNING") as logs:
            result = resolver.resolve(ACME)
        self.assertIsNone(result.key)
        self.assertEqual(len(calls), 1, "two key names failed, one revive")
        self.assertTrue(any("vct-hub was not running" in m for m in logs.output))

    def test_a_plain_miss_starts_nothing(self) -> None:
        calls: list[int] = []
        VendorKeyResolver(getter=_plain_miss, hub_reviver=lambda: calls.append(1) or "started").resolve(ACME)
        self.assertEqual(calls, [])

    def test_no_reviver_is_the_library_default(self) -> None:
        result = VendorKeyResolver(getter=_hub_down_miss).resolve(ACME)
        self.assertIsNone(result.key)

    def test_a_raising_reviver_never_breaks_resolution(self) -> None:
        def boom() -> str:
            raise RuntimeError("no")

        result = VendorKeyResolver(getter=_hub_down_miss, hub_reviver=boom).resolve(ACME)
        self.assertIsNone(result.key)

    def test_refusal_detection_walks_the_cause_chain(self) -> None:
        # The revive trigger is the gateway's ONE hub-down predicate
        # (`_is_hub_unreachable`): only a hub that is not running counts.
        from vco_lib.project_config import HubNotRunning, HubUnreachable

        self.assertTrue(_is_hub_unreachable(HubNotRunning("x")))
        self.assertFalse(_is_hub_unreachable(HubUnreachable("hub answered 401")))
        try:
            _hub_down_miss("k")
        except Exception as exc:  # noqa: BLE001
            self.assertTrue(_is_hub_unreachable(exc))
        try:
            _plain_miss("k")
        except Exception as exc:  # noqa: BLE001
            self.assertFalse(_is_hub_unreachable(exc))

    def test_a_running_hub_that_refuses_is_not_revived(self) -> None:
        calls: list[int] = []
        VendorKeyResolver(
            getter=_hub_running_but_refusing, hub_reviver=lambda: calls.append(1) or "started"
        ).resolve(ACME)
        self.assertEqual(calls, [], "a 401 from a live hub must not start a second hub")

    def test_the_daemon_wires_the_default_reviver(self) -> None:
        from model_router.config import GatewayConfig
        from model_router.secrets import default_hub_reviver
        from model_router.server import Gateway

        cfg = GatewayConfig(host="127.0.0.1", port=0, token="t", credentials_file=Path("/nonexistent"))
        gw = Gateway(cfg)
        self.assertIs(gw.keys._hub_reviver, default_hub_reviver)


# --------------------------------------------------------------------------- #
# GET /vendor-keys — names only
# --------------------------------------------------------------------------- #

class VendorKeysRouteTests(GatewayTestBase):
    async def _get(self, model: str, client=None) -> dict:
        resp = await (client or self.client).get(
            "/vendor-keys", params={"model": model}, headers=self.auth())
        self.assertEqual(resp.status, 200)
        return await resp.json()

    async def test_a_resolvable_vendor_is_usable_and_the_value_never_appears(self) -> None:
        resp = await self.client.get(
            "/vendor-keys", params={"model": "glm-5.3"}, headers=self.auth())
        raw = await resp.text()
        self.assertNotIn(FAKE_VENDOR_KEY, raw)
        body = json.loads(raw)
        self.assertEqual(body["route"], "vendor")
        self.assertEqual(body["vendor"], self.vendor.vendor_id)
        self.assertIs(body["usable"], True)
        self.assertIn(body["state"], ("resolved", "cached"))

    async def test_a_missing_key_is_definitely_unusable(self) -> None:
        client = await self.make_client(key_getter=_plain_miss)
        body = await self._get("glm-5.3", client)
        self.assertIs(body["usable"], False)
        self.assertIn("no key found", body["problem"])

    async def test_a_transient_store_is_unknown_not_unusable(self) -> None:
        def busy(key, project=None):
            from vco_lib.agent_secrets import SecretStoreUnavailable

            raise SecretStoreUnavailable("busy")

        client = await self.make_client(key_getter=busy)
        body = await self._get("glm-5.3", client)
        self.assertIsNone(body["usable"])
        self.assertTrue(body["transient"])

    async def test_a_first_party_model_needs_no_vendor_key(self) -> None:
        body = await self._get("claude-opus-4-1")
        self.assertEqual(body["route"], "anthropic")
        self.assertIs(body["usable"], True)

    async def test_an_unknown_id_is_refused_by_the_router(self) -> None:
        body = await self._get("no-such-model-xyz")
        self.assertEqual(body["route"], "refused")
        self.assertIs(body["usable"], False)

    async def test_the_route_needs_the_host_token(self) -> None:
        resp = await self.client.get("/vendor-keys", params={"model": "glm-5.3"})
        self.assertEqual(resp.status, 401)


# --------------------------------------------------------------------------- #
# vco_lib.gateway_preflight
# --------------------------------------------------------------------------- #

def _agents(tmp_path: Path, name: str, model: str) -> Path:
    proj = tmp_path / "proj"
    agents = proj / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: x\nmodel: {model}\n---\nbody\n", encoding="utf-8")
    return proj


def _payload(agent: str, model: str | None = None) -> dict:
    ti = {"prompt": "Task: x", "subagent_type": agent}
    if model:
        ti["model"] = model
    return {"tool_name": "Agent", "tool_input": ti}


def _ok(body: dict) -> dict:
    return {"ok": True, "port": 1, "payload": body}


def _vendor_missing(model: str) -> dict:
    return _ok({"route": "vendor", "vendor": "qwen", "usable": False, "state": "missing",
                "problem": "no key found for vendor 'qwen'. The launcher hub is not running, "
                           "so the OS keychain was not checked"})


@pytest.fixture
def user_claude(tmp_path, monkeypatch):
    home = tmp_path / "userclaude"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(home))
    return home


def test_preflight_refuses_a_gateway_lane_whose_key_cannot_resolve(tmp_path, user_claude):
    proj = _agents(tmp_path, "deepseek-researcher", "claude-gw/qwen/deepseek-v4.1-flash[1m]")
    asked: list[str] = []
    env = gp.run(_payload("deepseek-researcher"), project_dir=proj,
                 fetch=lambda m: asked.append(m) or _vendor_missing(m), environ={})
    assert asked == ["qwen/deepseek-v4.1-flash[1m]"]
    assert env is not None
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "vendor 'qwen'" in hso["permissionDecisionReason"]
    assert "retry this launch" in hso["permissionDecisionReason"], "hub-down hint"


def test_preflight_lets_a_resolvable_lane_through(tmp_path, user_claude):
    proj = _agents(tmp_path, "glm-reviewer", "claude-gw/glm-5.3[1m]")
    env = gp.run(_payload("glm-reviewer"), project_dir=proj, environ={},
                 fetch=lambda m: _ok({"route": "vendor", "vendor": "zai", "usable": True}))
    assert env is None


@pytest.mark.parametrize("answer", [
    {"ok": False, "reason": "unreachable", "message": "no gateway"},
    {"ok": False, "reason": "outdated_gateway", "message": "old"},
    _ok({"route": "vendor", "vendor": "qwen", "usable": None, "transient": True}),
    _ok({"route": "vendor", "vendor": "qwen", "usable": None, "state": "pending"}),
])
def test_preflight_never_refuses_on_uncertainty(tmp_path, user_claude, answer):
    proj = _agents(tmp_path, "kimi-reviewer", "claude-gw/kimi/k3[1m]")
    assert gp.run(_payload("kimi-reviewer"), project_dir=proj, environ={}, fetch=lambda m: answer) is None


def test_preflight_refuses_an_id_the_router_refuses(tmp_path, user_claude):
    proj = _agents(tmp_path, "typo-lane", "claude-gw/glm-5.33")
    env = gp.run(_payload("typo-lane"), project_dir=proj, environ={},
                 fetch=lambda m: _ok({"route": "refused", "usable": False, "problem": "no backend"}))
    assert env and env["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_preflight_skips_first_party_and_unknown_agents(tmp_path, user_claude):
    def never(model):
        raise AssertionError("the gateway must not be asked")

    proj = _agents(tmp_path, "expert-coder", "opus")
    assert gp.run(_payload("expert-coder"), project_dir=proj, environ={}, fetch=never) is None
    assert gp.run(_payload("no-such-agent"), project_dir=proj, environ={}, fetch=never) is None
    # A first-party override beats a gateway definition.
    proj = _agents(tmp_path, "glm-implementer", "claude-gw/glm-5.3[1m]")
    assert gp.run(_payload("glm-implementer", "opus"), project_dir=proj, environ={}, fetch=never) is None
    # Not an Agent call at all.
    assert gp.run({"tool_name": "Bash", "tool_input": {}}, project_dir=proj, environ={}, fetch=never) is None


def test_preflight_kill_switch(tmp_path, user_claude):
    proj = _agents(tmp_path, "deepseek-researcher", "claude-gw/qwen/deepseek-v4.1-flash[1m]")
    assert gp.run(_payload("deepseek-researcher"), project_dir=proj,
                  environ={gp.ENV_KILL_SWITCH: "0"}, fetch=_vendor_missing) is None


def test_preflight_finds_user_level_and_renamed_definitions(tmp_path, user_claude):
    (user_claude / "agents" / "lane-file.md").write_text(
        "---\nname: my-lane\nmodel: claude-gw/qwen/qwen3.8-max[1m]\n---\n", encoding="utf-8")
    asked: list[str] = []
    gp.run(_payload("my-lane"), project_dir=tmp_path / "noproj", environ={},
           fetch=lambda m: asked.append(m) or _ok({"route": "vendor", "usable": True}))
    assert asked == ["qwen/qwen3.8-max[1m]"]


def test_preflight_cli_prints_the_deny_envelope(tmp_path, user_claude, monkeypatch, capsys):
    proj = _agents(tmp_path, "deepseek-researcher", "claude-gw/qwen/deepseek-v4.1-flash[1m]")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.delenv(gp.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr("vco_lib.gateway_client.get_json", lambda route, **kw: _vendor_missing(""))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_payload("deepseek-researcher"))))
    assert gp.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --------------------------------------------------------------------------- #
# The Agent|Task hook passes the deny through, before the KG leg
# --------------------------------------------------------------------------- #

_PREFLIGHT_STUB = """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "vco_lib.gateway_preflight" ]; then
    cat >/dev/null
    printf '%s' "$PREFLIGHT_STUB_OUT"
    exit 0
fi
echo "router-called" >> "{marker}"
exit 0
"""


def _hook_env(tmp_path: Path, preflight_out: str) -> tuple[dict, Path]:
    from tests.common.child_env import child_env

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    marker = tmp_path / "router.log"
    py = venv / "bin" / "python"
    py.write_text(_PREFLIGHT_STUB.format(marker=marker), encoding="utf-8")
    py.chmod(0o755)
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    env = child_env(CLAUDE_PROJECT_DIR=str(proj), VCT_VENV=str(venv),
                    PREFLIGHT_STUB_OUT=preflight_out)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    return env, marker


_DENY = json.dumps(gp.deny_envelope("Not launched: test"))


@needs_bash
def test_sh_agent_hook_passes_the_deny_through_and_skips_the_kg_leg(tmp_path):
    env, marker = _hook_env(tmp_path, _DENY)
    proc = subprocess.run(["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
                          input=json.dumps(_payload("x")), capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not marker.exists(), "no KG leg for a refused launch"


@needs_bash
def test_sh_agent_hook_without_a_deny_runs_the_kg_leg(tmp_path):
    env, marker = _hook_env(tmp_path, "")
    proc = subprocess.run(["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
                          input=json.dumps(_payload("x")), capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "permissionDecision" not in proc.stdout
    assert marker.exists(), "the KG leg still runs"


@needs_pwsh
def test_ps1_agent_hook_passes_the_deny_through_and_skips_the_kg_leg(tmp_path):
    env, marker = _hook_env(tmp_path, _DENY)
    proc = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / "agent-brief-kg-inject.ps1")],
                          input=json.dumps(_payload("x")), capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not marker.exists()


@needs_pwsh
def test_ps1_agent_hook_without_a_deny_runs_the_kg_leg(tmp_path):
    env, marker = _hook_env(tmp_path, "")
    proc = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / "agent-brief-kg-inject.ps1")],
                          input=json.dumps(_payload("x")), capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "permissionDecision" not in proc.stdout
    assert marker.exists()
