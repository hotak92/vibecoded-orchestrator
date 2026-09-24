# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 removal of ``VCT_HUB_LEGACY_GLOBAL_ENV`` — driven caller pins.

Owner ruling 2026-09-24: verify consumers, rewire to the project-scoped
token, then remove the switch. The hub half is pinned by the Rust suite
(``launcher/src-tauri/vct-hub/src/auth.rs`` tests: global token refused
on ``/env`` + ``/config`` even with the flag set, startup notice text).
This module drives the OTHER half — the bundled resolvers that present
tokens to those per-project routes — proving each prefers the scoped
``hub.token.<project_id>`` over the coarse global ``hub.token`` (that is
the "rewire" the ruling asked for; it landed in v0.2.76 Part 4 and these
tests keep it from regressing), that their 403 diagnostics name the
removal, and that no shipped text still teaches setting the variable.

Why drive the resolver helpers instead of scanning for wiring: the
lesson from v0.2.96's credited-mechanism sweep — a name in a comment
satisfies a source scan while the code does something else. Each test
below executes the real shipped function against a staged state dir.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import vco_lib.agent_secrets as agent_secrets
import vco_lib.project_config as project_config

REPO = Path(__file__).resolve().parents[1]

SCOPED_TOKEN = "scoped-token-a1b2c3d4"
GLOBAL_TOKEN = "global-token-e5f6a7b8"
PROJECT_ID = "11111111-2222-3333-4444-555555555555"


def _stage_state_dir(tmp_path: Path) -> Path:
    """Stage ``<dir>/hub.token.<id>`` + ``<dir>/hub.token`` (0600)."""
    state = tmp_path / "state"
    state.mkdir()
    scoped = state / f"hub.token.{PROJECT_ID}"
    scoped.write_text(SCOPED_TOKEN + "\n", encoding="utf-8")
    os.chmod(scoped, 0o600)
    (state / "hub.token").write_text(GLOBAL_TOKEN + "\n", encoding="utf-8")
    return state


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = '{"error": {"code": "global_token_refused"}}'

    def json(self) -> dict:
        return {"error": {"code": "global_token_refused", "message": "refused"}}


# ─── Python resolver: vco_lib/project_config.py::_project_token ─────────


def test_python_token_helper_prefers_scoped_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both files on disk, no env pin → the SCOPED token is presented."""
    state = _stage_state_dir(tmp_path)
    monkeypatch.setattr(project_config, "vct_root_dir", lambda: state)
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    assert (
        project_config._project_token(PROJECT_ID, GLOBAL_TOKEN) == SCOPED_TOKEN
    )


def test_python_token_helper_env_pin_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env pin keeps ONE token across every route (tests/dev parity).

    Contract (docstring): ``VCT_HUB_TOKEN`` already won inside
    ``_discover_hub``, so the caller's ``global_token`` argument IS the
    env token — the helper's job here is to NOT consult the scoped file.
    This is the presentation the hub's lazy-mint rescue converts: the
    pinned global token draws a 403, the resolver retries once against
    the on-disk token, the hub mints the scoped file on first request.
    """
    state = _stage_state_dir(tmp_path)
    monkeypatch.setattr(project_config, "vct_root_dir", lambda: state)
    monkeypatch.setenv("VCT_HUB_TOKEN", "pinned-env-token")
    assert (
        project_config._project_token(PROJECT_ID, GLOBAL_TOKEN) == GLOBAL_TOKEN
    )


def test_python_token_helper_global_fallback_when_scoped_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoped file missing (hub restarted, mid-session project) → global.

    The hub's lazy-mint turns exactly this presentation into a scoped
    token on the first request — that is why the removal is safe.
    """
    state = _stage_state_dir(tmp_path)
    (state / f"hub.token.{PROJECT_ID}").unlink()
    monkeypatch.setattr(project_config, "vct_root_dir", lambda: state)
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    assert (
        project_config._project_token(PROJECT_ID, GLOBAL_TOKEN) == GLOBAL_TOKEN
    )


# ─── 403 diagnostics name the removal (driven through the public fns) ───


def test_config_resolver_403_message_names_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve()`` on a 403 raises Forbidden naming hub.token.<pid> + the
    v0.2.97 removal — the remediation a user actually needs post-removal."""
    monkeypatch.setattr(
        project_config,
        "_get_with_401_retry",
        lambda *a, **kw: _FakeResponse(403),
    )
    # The suite pins VCT_DISABLE_HUB_RESOLVER=1 (tests/conftest.py) to
    # keep resolution hermetic; this test drives the hub-403 branch, so
    # it must clear that gate for its own call only.
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    with pytest.raises(project_config.Forbidden) as excinfo:
        project_config.resolve(PROJECT_ID)
    msg = str(excinfo.value)
    assert f"hub.token.{PROJECT_ID}" in msg
    assert "removed in v0.2.97" in msg


def test_env_resolver_403_message_names_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_hub_get`` (the /env resolver behind agent_secrets.get) same."""
    monkeypatch.setattr(
        agent_secrets,
        "_get_with_401_retry",
        lambda *a, **kw: _FakeResponse(403),
    )
    with pytest.raises(agent_secrets.Forbidden):
        try:
            agent_secrets._hub_get("SOME_KEY", PROJECT_ID)
        except agent_secrets.Forbidden as exc:
            msg = str(exc)
            assert f"hub.token.{PROJECT_ID}" in msg
            assert "removed in v0.2.97" in msg
            raise


# ─── Shell resolver: vct_project_config.sh::hub_token (extracted+driven) ─


_SH_FN = re.compile(r"^hub_token\(\) \{.*?^\}", re.M | re.S)


def _run_bash_hub_token(state: Path, args: list[str], env_pin: str | None) -> str:
    src = (REPO / "templates" / "scripts" / "vct_project_config.sh").read_text(
        encoding="utf-8"
    )
    m = _SH_FN.search(src)
    assert m, "hub_token() not found in vct_project_config.sh"
    # _emit_warning is only called on the unreadable-global path, which
    # these tests never take; shadow it anyway so an unexpected call is
    # visible rather than a hard "command not found".
    script = (
        "_emit_warning() { echo \"WARN:$*\" >&2; }\n"
        + m.group(0)
        + f"\nhub_token {' '.join(args)}\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VCT_HUB_TOKEN", "VCT_STATE_DIR")
    }
    env["VCT_STATE_DIR"] = str(state)
    if env_pin is not None:
        env["VCT_HUB_TOKEN"] = env_pin
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_bash_hub_token_prefers_scoped(tmp_path: Path) -> None:
    state = _stage_state_dir(tmp_path)
    assert _run_bash_hub_token(state, [PROJECT_ID], None) == SCOPED_TOKEN


def test_bash_hub_token_no_id_uses_global(tmp_path: Path) -> None:
    """The by-path lookup passes no id → global (not a per-project route)."""
    state = _stage_state_dir(tmp_path)
    assert _run_bash_hub_token(state, [], None) == GLOBAL_TOKEN


def test_bash_hub_token_env_pin_wins(tmp_path: Path) -> None:
    state = _stage_state_dir(tmp_path)
    assert (
        _run_bash_hub_token(state, [PROJECT_ID], "pinned-env-token")
        == "pinned-env-token"
    )


# ─── PowerShell resolver: vct_project_config.ps1::Get-HubToken ──────────


_PS1_FN = re.compile(r"^function Get-HubToken \{.*?^\}", re.M | re.S)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_pwsh_get_hub_token_prefers_scoped(tmp_path: Path) -> None:
    src = (
        REPO / "templates" / "scripts" / "vct_project_config.ps1"
    ).read_text(encoding="utf-8-sig")
    m = _PS1_FN.search(src)
    assert m, "Get-HubToken not found in vct_project_config.ps1"
    state = _stage_state_dir(tmp_path)
    harness = (
        # Shadow the rate-limited emitter (only used on the
        # unreadable-global path these tests never take).
        "function Emit-Warning { param($ErrorKind, $Detail, $StderrLine) }\n"
        + m.group(0)
        + "\n"
        + f"$Env:VCT_STATE_DIR = '{state}'\n"
        + "Remove-Item Env:VCT_HUB_TOKEN -ErrorAction SilentlyContinue\n"
        + f"(Get-HubToken -ProjectId '{PROJECT_ID}').Trim()\n"
        + "Remove-Item Env:VCT_STATE_DIR -ErrorAction SilentlyContinue\n"
    )
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == SCOPED_TOKEN


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_pwsh_get_hub_token_env_pin_wins(tmp_path: Path) -> None:
    src = (
        REPO / "templates" / "scripts" / "vct_project_config.ps1"
    ).read_text(encoding="utf-8-sig")
    m = _PS1_FN.search(src)
    assert m, "Get-HubToken not found in vct_project_config.ps1"
    state = _stage_state_dir(tmp_path)
    harness = (
        "function Emit-Warning { param($ErrorKind, $Detail, $StderrLine) }\n"
        + m.group(0)
        + "\n"
        + f"$Env:VCT_STATE_DIR = '{state}'\n"
        + "$Env:VCT_HUB_TOKEN = 'pinned-env-token'\n"
        + "(Get-HubToken -ProjectId '" + PROJECT_ID + "').Trim()\n"
        + "Remove-Item Env:VCT_STATE_DIR -ErrorAction SilentlyContinue\n"
        + "Remove-Item Env:VCT_HUB_TOKEN -ErrorAction SilentlyContinue\n"
    )
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "pinned-env-token"


# ─── No shipped text teaches setting the removed variable ───────────────


def test_no_shipped_text_teaches_setting_the_removed_flag() -> None:
    """``VCT_HUB_LEGACY_GLOBAL_ENV=1`` (the set-command form) may appear
    ONLY in CHANGELOG.md history entries. templates/ + docs/ + README
    must not carry it — a user who finds it there sets a variable that
    has done nothing since v0.2.97 removed it."""
    offenders: list[str] = []
    for base in ("templates", "docs"):
        for p in (REPO / base).rglob("*"):
            if not p.is_file() or "_archive" in p.parts:
                continue
            if "VCT_HUB_LEGACY_GLOBAL_ENV=1" in p.read_text(
                encoding="utf-8", errors="replace"
            ):
                offenders.append(str(p.relative_to(REPO)))
    readme = REPO / "README.md"
    if readme.exists() and "VCT_HUB_LEGACY_GLOBAL_ENV=1" in readme.read_text(
        encoding="utf-8", errors="replace"
    ):
        offenders.append("README.md")
    assert not offenders, (
        "Shipped text still carries the set-command form of the removed "
        f"VCT_HUB_LEGACY_GLOBAL_ENV flag: {offenders}. The flag was "
        "removed in v0.2.97 — replace the advice with the remediation "
        "(present hub.token.<project_id>) or, if this is history, move "
        "it to CHANGELOG.md."
    )
